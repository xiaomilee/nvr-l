#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取帧：从**本机取流端口**按节奏解出一帧 JPEG，三处共用一份。

以前预览 / 移动侦测 / AI 各自 `ffmpeg -i rtsp://...` 抽帧 —— 每 5 秒一路两次
新建 RTSP 会话，5 路就是每分钟 120 次。现在它们全部改读 `snapshots/<id>.jpg`，
摄像头只看到 ingest 那一条连接（见 ingest.py 的说明）。

为什么用 `-skip_frame nokey`：解码 4MP@25fps 才是真正的 CPU 开销，而我们要的
只是「每 5 秒一张图」。只解关键帧，CPU 大约是全程解码的 1/20；帧率由
`fps=1/N` 再削一次。代价是**出帧节奏跟着摄像头 GOP 走**——GOP 2 秒时每 5 秒
必然有帧，GOP 超过 N 秒时就会漏帧。所以留了降级：连续取不到帧就摘掉
skip_frame 重试（记在 `_no_skip` 里，该路之后都不再用它）。

为什么写文件而不是共享内存：消费方是「HTTP 图片接口 + 两个 Python 线程」，
文件是最简单可靠的中转；写入走 `.part` + `os.replace`，读者永远读不到半张图。
"""
import os
import subprocess
import threading
import time

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
NO_FRAME_SEC = 30
# 上游多少秒没字节就算「断流」而不是「GOP 太长」。TS 流每秒都有包，
# 3 秒空白已经说明源头断了 —— 这时候摘 skip_frame 全帧解码毫无意义，
# 只是白烧 CPU（真机 2026-10-26 断流期间每路都在做这件事）。
UPSTREAM_QUIET_SEC = 3.0          # 30 秒没出图 → 重启这一路的取帧进程
MAX_BACKOFF = 60


def jpeg_split(buf, on_frame):
    """从字节流里切出完整的 JPEG。返回未消费的尾巴。

    MJPEG 就是 SOI..EOI 首尾相接，不需要解析任何头：找到 SOI 的位置，再找它
    后面的 EOI，中间那段就是一个完整帧。找不到 EOI 就保留从 SOI 起的所有字节
    等下一次 read —— 一帧 4MP 也就 200~600KB，暂存是可接受的。
    """
    while True:
        i = buf.find(SOI)
        if i < 0:
            return b""
        if i > 0:
            buf = buf[i:]                   # 丢掉帧头之前的垃圾（ffmpeg 偶尔会先吐换行）
        j = buf.find(EOI, 2)
        if j < 0:
            return buf
        on_frame(buf[:j + 2])
        buf = buf[j + 2:]


class FrameTap:
    """一路：一个 ffmpeg 常驻进程 → 一张 `snapshots/<id>.jpg`。"""

    def __init__(self, ffmpeg, cam_id, url_fn, out_path, interval=5, log=None,
                 upstream_idle_fn=None):
        self.ffmpeg = ffmpeg
        self.cid = cam_id
        self.url_fn = url_fn               # 每次重启用一次：取流端口可能变了
        self.out_path = out_path
        self.interval = max(1, min(30, int(interval or 5)))
        self.log = log or (lambda m: None)
        # 注入：返回上游（取流层）多少秒没字节；None = 不知道（走直连时）
        self.upstream_idle_fn = upstream_idle_fn
        self.proc = None
        self.ok = None                     # None=还没试过 / True / False
        self.frames = 0
        # seq = 成功落盘的**张数**，给消费者做「这张图我还没处理过」的判据。
        # frames 与 last_frame 在收到字节时就加，落盘失败也加，不能当判据。
        self.seq = 0
        self.last_frame = 0.0
        self.last_error = ""
        self._no_skip = False              # 该路的摄像头 GOP 太长，已放弃 skip_frame
        self._skip_tries = 0
        self._buf = b""
        self._stop = threading.Event()
        self._next_at = 0.0
        self._threads = []

    # -------------------------------------------------- 文件

    def _write(self, data):
        tmp = self.out_path + ".part"
        try:
            os.makedirs(os.path.dirname(self.out_path), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.out_path)   # 原子替换：HTTP 端不会读到半张图
            self.seq += 1                    # 只在真的换成新图后才加
            return True
        except OSError as e:
            self.last_error = "写快照失败：%s" % e
            return False

    def path_age(self):
        """这张图多久了（秒）。没有文件返回 None。"""
        try:
            return time.time() - os.path.getmtime(self.out_path)
        except OSError:
            return None

    # -------------------------------------------------- 进程

    def _cmd(self, url):
        c = [self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
        if not self._no_skip:
            c += ["-skip_frame", "nokey"]
        # scale=-2:720：取流喂的是**主码流**（4MP 级别），而这一张图要同时给
        # 手机预览（流量）、帧差（像素数越大越慢）和 AI（720p 已经够）。
        # 只缩不放大的写法要嵌表达式（min(720\,ih)），在没有本地 ffmpeg 的环境
        # 验不了，不如用一个确定合法的常量高度 —— 小码流被放大到 720p 只是白花
        # 几百 KB，而写错一个转义会让整路取帧起不来。
        c += ["-i", url, "-an", "-map", "0:v:0",
              "-vf", "fps=1/%d,scale=-2:720" % self.interval,
              "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "-"]
        return c

    def _spawn(self):
        if self._stop.is_set():
            return
        url = self.url_fn()
        if not url:
            self.last_error = "取流未就绪"
            self.ok = False
            self._next_at = time.time() + 5
            return
        try:
            p = subprocess.Popen(self._cmd(url), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        except OSError as e:
            self.ok = False
            self.last_error = "ffmpeg 无法启动：%s" % e
            self._next_at = time.time() + 10
            return
        self.proc = p
        self._buf = b""
        self._spawned_at = time.time()
        t = threading.Thread(target=self._read, args=(p,), daemon=True,
                             name="tap-%s" % self.cid)
        t.start()
        self._threads.append(t)

    def _read(self, p):
        out = p.stdout
        while not self._stop.is_set():
            try:
                chunk = out.read(32768)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            self._buf = jpeg_split(self._buf + chunk, self._on_frame)
        try:
            out.close()
        except (OSError, ValueError):
            pass

    def _on_frame(self, data):
        self.frames += 1
        self.last_frame = time.time()
        self.ok = True
        self.last_error = ""
        self._skip_tries = 0
        self._write(data)

    def _kill(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        if p.poll() is None:
            try:
                p.send_signal(2)
                p.wait(timeout=4)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    p.kill()
                except (OSError, ValueError):
                    pass

    # -------------------------------------------------- 巡检

    def tick(self, now=None):
        if self._stop.is_set():
            return
        now = now if now is not None else time.time()
        p = self.proc
        if p is None:
            if now >= self._next_at:
                self._spawn()
            return
        if p.poll() is not None:
            self._kill()
            self.ok = False
            self._next_at = now + 5
            return
        # 进程活着但一直不出图：GOP 可能比 interval 长得多。先摘 skip_frame 重试
        # 一次，还不行就按"取帧跟不上"处理，每 30 秒重启一次。
        idle = now - max(self.last_frame, getattr(self, "_spawned_at", now))
        if idle > NO_FRAME_SEC:
            # 先分清是谁的问题：上游断流时这边当然也不出图，那不是 GOP 长，
            # 摘 skip_frame 只会白烧 CPU，判死重启更是把消费者也一起打断。
            up = None
            if self.upstream_idle_fn is not None:
                try:
                    up = self.upstream_idle_fn(self.cid)
                except Exception:
                    up = None
            if up is not None and up > UPSTREAM_QUIET_SEC:
                self.last_frame = now     # 上游的事，交给上游的看门狗
                return
            if not self._no_skip:
                self._no_skip = True
                self.log("取帧 %s 连续 %d 秒没出图，改用全帧解码重试"
                         % (self.cid, int(idle)))
            else:
                self.ok = False
                self.last_error = "%.0f 秒没有新画面" % idle
            self._kill()
            self._next_at = now + 2
            self.last_frame = now          # 重置计时，别每轮都重启

    def start(self):
        self._next_at = 0
        self.tick()

    def stop(self):
        self._stop.set()
        self._kill()


class FrameTapManager:
    """按摄像头列表维护一组 FrameTap，对外形状与 SnapshotWorker.status() 对齐。"""

    def __init__(self, ffmpeg, var_dir, cams_fn, snap_path_fn,
                 interval_fn=None, log=None, upstream_idle_fn=None):
        self.ffmpeg = ffmpeg
        self.var_dir = var_dir
        self.cams_fn = cams_fn
        self.snap_path_fn = snap_path_fn
        self.interval_fn = interval_fn or (lambda: 5)
        self.log = log or (lambda m: None)
        self.upstream_idle_fn = upstream_idle_fn   # 由 nvr_slim 注入，见 FrameTap
        self.items = {}

    def start(self):
        self.reconcile(self.cams_fn())

    def reconcile(self, cams):
        want = {}
        for cam in (cams or []):
            if not isinstance(cam, dict) or cam.get("enabled") is False:
                continue
            want[cam["id"]] = cam
        for cid in list(self.items):
            if cid not in want:
                self.items.pop(cid).stop()
        itv = int(self.interval_fn() or 5)
        for cid, cam in want.items():
            tap = self.items.get(cid)
            if tap is not None and tap.interval == itv:
                continue
            if tap is not None:
                tap.stop()
            tap = FrameTap(self.ffmpeg, cid, self._url_fn(cid),
                           self.snap_path_fn(cid), interval=itv, log=self.log,
                           upstream_idle_fn=self.upstream_idle_fn)
            self.items[cid] = tap
            tap.start()

    def _url_fn(self, cid):
        return lambda: (self.url_for(cid) if self.url_for else None)

    # 由 nvr_slim 注入：返回该路的本机取流 URL（没有就 None）
    url_for = None

    def tick(self, now=None):
        for tap in list(self.items.values()):
            try:
                tap.tick(now)
            except Exception as e:
                self.log("取帧巡检异常（%s）：%r" % (tap.cid, e))

    def status(self):
        """与 SnapshotWorker.status() 同形状：cam_id → True/False/None。"""
        out = {}
        for cid, tap in self.items.items():
            age = tap.path_age()
            if tap.ok is True and age is not None and age <= max(30, tap.interval * 3):
                out[cid] = True
            elif tap.ok is False or (age is not None and age > max(60, tap.interval * 6)):
                out[cid] = False
            else:
                out[cid] = tap.ok
        return out

    def detail(self):
        return {cid: {"frames": t.frames, "seq": t.seq, "ok": t.ok,
                      "interval": t.interval, "age": t.path_age(),
                      "error": t.last_error}
                for cid, t in self.items.items()}

    def stop(self):
        for t in list(self.items.values()):
            t.stop()
        self.items.clear()
