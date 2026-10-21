#!/usr/bin/env python3
"""快照 worker：直播页与遮罩底图的真正数据源。

每路每 N 秒用 ffmpeg 从 RTSP 抓 1 帧存成 jpg。三个要点：
1. 先写 `.part` 临时文件再原子改名 —— HTTP 端永远不会读到半张图；
2. 每一轮都重新问 cams_fn 要最新摄像头列表 —— 设置页加/删/改地址不用重启就生效；
3. 拉不到流（地址错、掉线、认证失败）只记一次状态变化，不刷屏，也不拖累其它路。
"""
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import ffmpeg_cmd  # noqa: F401  （保留导出，脚本内自检与外部调用方依赖）


class SnapshotWorker:
    def __init__(self, ffmpeg, var_dir, cams_fn, interval_fn=None,
                 interval=10, timeout=10, workers=4, log=None):
        self.ffmpeg = ffmpeg
        self.var_dir = var_dir
        self.cams_fn = cams_fn
        self.interval_fn = interval_fn          # 每轮读一次，设置页改了立即生效
        self.interval = max(1, min(10, int(interval or 10)))
        self.timeout = int(timeout)
        self.workers = max(1, int(workers))
        self.log = log or (lambda m: None)
        self.stop_event = threading.Event()
        self._ok = {}                           # cam_id -> True/False/None（None=还没试过）
        self._fails = {}                        # cam_id -> 连续失败次数（用于换传输协议）
        self._udp_ok = {}                       # cam_id -> UDP 探测结果（None=没试过；False=不可用则固定 TCP）
        self._last = {}                         # cam_id -> 上一轮内容的去重键，避免日志里重复同一句
        self.thread = threading.Thread(target=self._loop, daemon=True, name="snapshots")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def status(self):
        """cam_id -> True 成功 / False 失败 / None 还没试过。给 /api/live 用。"""
        return dict(self._ok)

    def snap_dir(self):
        d = os.path.join(self.var_dir, "snapshots")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d

    # ------------------------------------------------------------ 主循环
    def _loop(self):
        # 第一轮提前抓，别让预览页空等一个整间隔
        first = True
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                self._round(parallel=first)
            except Exception as e:              # 单轮出错不能让线程退出
                self.log("快照轮异常：%r" % e)
            first = False
            itv = self.interval
            if self.interval_fn:
                try:
                    itv = max(1, min(10, int(self.interval_fn() or 10)))
                except (TypeError, ValueError):
                    pass
            wait = max(0.5, itv - (time.time() - t0))
            self.stop_event.wait(wait)

    def _round(self, parallel=False):
        try:
            cams = [c for c in (self.cams_fn() or [])]
        except Exception as e:
            self.log("读取摄像头列表失败：%r" % e)
            return
        targets = []
        for c in cams:
            if not isinstance(c, dict):
                continue
            if c.get("enabled") is False:
                continue
            main_url = str(c.get("url") or "").strip()
            sub_url = str(c.get("sub_url") or "").strip()
            # 预览优先子码流（省带宽）；子码流抓不到就回退主码流兜底，
            # 这样「子码流填错/不通」时预览仍然出得来，不会整路黑屏。
            urls = []
            for u in (sub_url, main_url):
                if u and u not in urls:
                    urls.append(u)
            if urls:
                targets.append((c.get("id"), urls))
        if not targets:
            return
        # 每轮都并行：单路最坏要 8 秒超时、再加子码流失败回退主码流又一 8 秒，
        # 4 路串行时一轮能跑 30 秒以上，interval=10 就名存实亡。
        # 只在线程池里等，抓帧本身仍是"一路一个 ffmpeg"，不会放大并发。
        if len(targets) > 1:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(targets))) as ex:
                list(ex.map(lambda t: self._grab_any(*t), targets))
        else:
            for t in targets:
                self._grab_any(*t)

    def _grab_any(self, cam_id, urls):
        """按候选地址逐个抓：子码流优先，失败就回退主码流再试一次。"""
        for i, url in enumerate(urls):
            if self._grab(cam_id, url):
                return True
            if i < len(urls) - 1:
                # 只在"首次回退"时记一条：每 10 秒刷一次会把 800 行环形日志全占满
                key = "%s|fallback" % cam_id
                if self._last.get(key) != urls[i]:
                    self._last[key] = urls[i]
                    self.log("子码流抓帧失败：%s，改用主码流再试" % cam_id)
        return False

    # ------------------------------------------------------------ 单路抓帧
    def _grab(self, cam_id, url):
        if not self.ffmpeg:                 # 没有 ffmpeg（比如开发机），整路跳过
            self._state(cam_id, False, "ffmpeg 未找到")
            return False
        out = os.path.join(self.snap_dir(), "%s.jpg" % cam_id)
        tmp = out + ".part"
        # 默认走 TCP（抗丢包，且 LIVE555 broadcast 类流只能用 TCP）。
        # UDP 只作一次性探测：TCP 连败几次且从未试过 UDP 时才试一次（有些老设备 TCP 不出图）；
        # UDP 失败就标记「该路 UDP 不可用」，之后固定回 TCP，避免对 broadcast 流周期性误伤。
        fails = self._fails.get(cam_id, 0)
        udp_state = self._udp_ok.get(cam_id)
        use_tcp = True
        if fails >= 3 and udp_state is None:
            use_tcp = False
        proto = "TCP" if use_tcp else "UDP"
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error"]
        if use_tcp:
            cmd += ["-rtsp_transport", "tcp"]
        # 输出写到 .part（原子改名用），扩展名推断不出格式会让 ffmpeg 报
        # "Error opening output files: Invalid argument"。显式指定 mjpeg+image2，
        # 不再依赖扩展名（这正是「手动测 .jpg 成功、程序写 .part 失败」的根因）。
        cmd += ["-i", url]
        # 快照就是快照：不加任何滤镜。以前这里为"输出遮蔽区"走 drawbox，
        # 逼着 ffmpeg 每抓一帧都过一次滤镜链；去掉后每路仍是 1 帧解码 + 直出。
        cmd += ["-frames:v", "1", "-c:v", "mjpeg", "-q:v", "5",
                "-f", "image2", "-y", tmp]
        ok = False
        stderr_tail = ""
        try:
            proc = subprocess.run(cmd, timeout=self.timeout,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE)
            if proc.returncode != 0:
                # 只取最后一行（ffmpeg 的最后一条 error 才是根因），去空白、限长
                tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
                stderr_tail = (tail[-1].strip() if tail else "")[:200]
        except subprocess.TimeoutExpired:
            stderr_tail = "拉流超时（%d 秒内没出图）" % self.timeout
        except OSError:
            self._state(cam_id, False, "ffmpeg 无法启动")
            return False
        if os.path.isfile(tmp) and os.path.getsize(tmp) > 1024:
            try:
                os.replace(tmp, out)            # 原子改名：读者要么看不到、要么看到完整 JPEG
                ok = True
            except OSError:
                pass
        else:
            self._rm(tmp)
        if ok:
            self._fails[cam_id] = 0
            if not use_tcp:
                self._udp_ok[cam_id] = True
        else:
            self._fails[cam_id] = fails + 1
            if not use_tcp:
                self._udp_ok[cam_id] = False   # UDP 探测失败：以后固定 TCP
        note = stderr_tail or ("连续失败 %d 次" % self._fails.get(cam_id, 0))
        self._state(cam_id, ok, "[%s] %s" % (proto, note))
        return ok

    def _state(self, cam_id, ok, note=""):
        if self._ok.get(cam_id) is ok:
            return
        self._ok[cam_id] = ok
        if ok:
            self.log("快照恢复：%s" % cam_id)
        else:
            self.log("快照失败：%s %s（地址错/掉线/认证失败都会这样）" % (cam_id, note))

    @staticmethod
    def _rm(p):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
