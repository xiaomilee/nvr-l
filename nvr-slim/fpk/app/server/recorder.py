#!/usr/bin/env python3
"""RTSP 拉流录制：主码流按分片落盘。

三个设计决定，都直接服务于「录完能不能马上压」：

1. **`-c copy` 转封装，不解码。**
   8 路 4MP 主码流合计只占几个百分点的单核，核显完整留给判定与预览。
   代价是画质参数在这一层无效 —— 录制只负责把原始码流完整、低开销地
   落到盘上，压缩发生在后一步。

2. **分片 5 分钟（可配 60~900 秒）。**
   分片越短，"录完 → 压制"的间隔越好控，热数据命中 page cache 的概率越高；
   太短则文件开合频繁、静止判定的上下文不足。5 分钟约合单路 150 MB，
   既在内存里待得住，也够判定看出"这五分钟到底有没有人"。

3. **只处理「已封口」的分片。**
   segment muxer 是顺序写的：看到下一个分片出现，就说明上一个已经写完。
   这条规则比 mtime 稳，也比 inotify 简单。正在写的那个分片 moov 尚未落地，
   碰它就是坏文件 —— 所有消费方必须经过 finished() 这道闸。

断流处理：RTSP 会在摄像头重启、网络抖动、交换机重启时断。
进程退出就按 5→10→20→30 秒退避重启，不无限快速重试把日志写爆。
跨天时主动重启一次，让输出目录跟着日期走。
"""
import datetime
import os
import signal
import subprocess
import time

import ffmpeg_cmd  # noqa: E402
import runtime  # noqa: E402

MAX_BACKOFF = 30
STALL_SEC = 75          # 最新分片超过这么久没更新，就判定这一路"假活"


# ---------------------------------------------------------------- 路径

def cam_dir(root, cam_id, day=None):
    day = day or datetime.date.today()
    return os.path.join(root, cam_id, day.isoformat())


def out_pattern(root, cam_id):
    """ffmpeg 的 strftime 会替换整个输出串里的 %，所以目录也能按日期生成。

    目录必须预先存在（ffmpeg 不建目录），由 ensure_day() 在开录前和跨天时补。
    输出落到 `.mp4.part`：正在写的分片带 .part 后缀，消费者只认 .mp4，
    于是「录到一半」的片段永远不会被当成可回放的录像；封口后由
    finalize_parts() 原子改名成 .mp4。
    """
    return os.path.join(root, cam_id, "%Y-%m-%d", "%H-%M-%S.mp4.part")


def _has_moov(path):
    """轻量判断一个 mp4 是否已封口：segment muxer 未开 faststart，moov 在文件尾部。

    封口 = ffmpeg 已经把这个分片写完（moov box 落地）。只扫尾部 256KB，
    不整读文件，几百 KB 的一次 seek 对 2 秒一轮的巡检来说可以忽略。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size < 256:
                return False
            f.seek(max(0, size - 262144))
            tail = f.read()
    except OSError:
        return False
    return b"moov" in tail


def finalize_parts(root, cam_id):
    """把已封口的 `.mp4.part` 改名成 `.mp4`。

    判定用 moov 而非「目录里有没有更新的分片」：后者在 ffmpeg 崩溃、下个
    进程又写了新分片后，会把上一个没封口的半截文件误当成完整文件。
    改名是原子的（os.replace），读者要么看到 .part 要么看到完整 .mp4。
    """
    base = os.path.join(root, cam_id)
    if not os.path.isdir(base):
        return
    try:
        days = sorted(d for d in os.listdir(base)
                      if len(d) == 10 and d[4] == "-" and d[7] == "-")
    except OSError:
        return
    for d in days:
        full = os.path.join(base, d)
        if not os.path.isdir(full):
            continue
        try:
            parts = sorted(n for n in os.listdir(full)
                           if n.lower().endswith(".mp4.part"))
        except OSError:
            continue
        for n in parts:
            src = os.path.join(full, n)
            dst = src[:-5]                # 去掉 ".part"
            if os.path.isfile(dst):        # 同名 .mp4 已存在（上次改名失败残留）
                try:
                    os.remove(src)
                except OSError:
                    pass
                continue
            if _has_moov(src):             # 只有真正封口了才改名
                try:
                    os.replace(src, dst)
                except OSError:
                    pass


def ensure_day(root, cam_id, day=None):
    d = cam_dir(root, cam_id, day)
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def list_segments(root, cam_id):
    """某路所有分片，按「日期目录 + 文件名」排序 —— 文件名是 HH-MM-SS，天然有序。"""
    base = os.path.join(root, cam_id)
    out = []
    if not os.path.isdir(base):
        return out
    try:
        days = sorted(d for d in os.listdir(base)
                      if len(d) == 10 and d[4] == "-" and d[7] == "-")
    except OSError:
        return out
    for d in days:
        full = os.path.join(base, d)
        if not os.path.isdir(full):
            continue
        try:
            names = sorted(n for n in os.listdir(full) if n.lower().endswith(".mp4"))
        except OSError:
            continue
        for n in names:
            p = os.path.join(full, n)
            if os.path.isfile(p):
                out.append(p)
    return out


def finished(root, cam_id, seg_sec=300, now=None):
    """已封口的分片。

    末位分片是"正在写"的，一律跳过；但如果它已经很久没变化（录制停了、
    当天最后一片），也算封口 —— 否则收工后的最后一片永远不会被处理。
    """
    segs = list_segments(root, cam_id)
    if not segs:
        return []
    now = now if now is not None else time.time()
    head, tail = segs[:-1], segs[-1]
    try:
        idle = now - os.path.getmtime(tail)
    except OSError:
        idle = 0
    # 两个分片周期没动静，说明这一路已经不写了
    if idle > max(120, seg_sec + 60):
        return segs
    return head


# ---------------------------------------------------------------- 命令

def record_cmd(ffmpeg, url, pattern, seg_sec=300, transport="tcp", redact=None):
    """拉流 → 分片落盘。

    -rtsp_transport tcp：UDP 在跨交换机时丢包会让花屏，监控盘上留不下证据。
    -timeout 10000000：**建连**超时 10 秒。无线摄像头半死连接上读不到数据，
                       由 recorder.tick() 的软件看门狗（最新分片 STALL_SEC 秒没
                       动静就杀进程重连）兜底 —— 不依赖 ffmpeg 的 -rw_timeout
                       （部分精简编译的 ffmpeg 不认该参数，会直接报
                       "Option not found" 让整路起不来）。
    -segment_atclocktime 1：分片边界对齐墙钟，文件名即片段起点，
                       回放时按时间定位不用查表。

    redact（输出遮蔽区）：有遮蔽格时不能走 -c copy（drawbox 是像素滤镜，必须
    重编码），改为 libx264 重编码打黑块；没有遮蔽区的通道保持 -c copy 零成本。
    """
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
           "-rtsp_transport", transport,
           "-timeout", "10000000",
           "-i", url]
    vf = ffmpeg_cmd.redact_filter(redact)
    if vf:
        cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-an", "-sn", "-dn",
            "-f", "segment",
            "-segment_time", str(int(seg_sec)),
            "-segment_format", "mp4",
            "-segment_atclocktime", "1",
            "-reset_timestamps", "1",
            "-strftime", "1",
            "-y", pattern]
    return cmd


# ---------------------------------------------------------------- 录制器

class Recorder:
    """所有通道共用一个监督线程，每路一个 ffmpeg 子进程。

    不用「一路一线程」是因为要判断的东西都是全局的（跨天、停服、重连计数），
    一个 2 秒轮询的线程足够，线程数也压得住。
    """

    def __init__(self, ffmpeg, root, cameras, segment_sec=300,
                 reconnect_sec=5, log=None, enabled=True):
        self.ffmpeg = ffmpeg
        self.root = root
        self.cams = [c for c in (cameras or [])
                     if c.get("url") and c.get("enabled", True)]
        self.seg_sec = segment_sec
        self.reconnect = max(2, int(reconnect_sec or 5))
        self.log = log or (lambda m: None)
        self.enabled = enabled
        self.procs = {}          # cam_id -> {"p": Popen, "day": str, "tries": int, "next": float}
        self.stopped = False
        self.last_error = {}

    # -- 生命周期 --------------------------------------------------

    def set_cameras(self, cams):
        """设置页改了列表/地址后由调度线程喂进来，无需重启。"""
        self.cams = [c for c in (cams or [])
                     if c.get("url") and c.get("enabled", True)]

    def start(self):
        if not self.enabled:
            return
        if not self.ffmpeg:
            self.log("录制未启动：系统里没有 ffmpeg")
            return
        if not self.cams:
            self.log("录制未启动：没有配置带 RTSP 地址的通道")
            return
        for c in self.cams:
            ensure_day(self.root, c.get("dir_name") or c["id"])
        self._spawn_all()
        self.log("录制开始：%d 路，分片 %d 秒，根目录 %s"
                 % (len(self.cams), self.seg_sec, self.root))

    def _spawn_all(self):
        for c in self.cams:
            self._spawn(c)

    def _spawn(self, cam, keep_tries=False):
        cid = cam["id"]
        dname = cam.get("dir_name") or cid           # 存储目录用摄像头名称
        redact = []
        m = cam.get("masks") or {}
        if m.get("redact"):
            redact = [int(x) for x in m["redact"] if isinstance(x, int)]
        ensure_day(self.root, dname)
        cmd = record_cmd(self.ffmpeg, cam["url"], out_pattern(self.root, dname),
                         self.seg_sec, redact=redact)
        tries = self.procs.get(cid, {}).get("tries", 0) if keep_tries else 0
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 start_new_session=True)
        except OSError as e:
            self.last_error[cid] = "无法启动 ffmpeg：%s" % e
            self.log("录制 %s 启动失败：%s" % (cid, e))
            self.procs[cid] = {"p": None, "day": datetime.date.today().isoformat(),
                               "tries": tries + 1, "next": time.time() + MAX_BACKOFF,
                               "started_at": 0}
            return
        self.procs[cid] = {"p": p, "day": datetime.date.today().isoformat(),
                           "tries": tries, "next": 0.0,
                           "started_at": time.time(), "dead": None}
        self.last_error.pop(cid, None)
        self.log("录制 %s 已启动 pid=%d" % (cam.get("name") or cid, p.pid))

    def reconfigure(self, cfg=None, cams=None):
        """设置推送后热生效：分片长度 / 启用开关 / 摄像头列表 / 录像目录，不必重启应用。

        分片长度写在 ffmpeg 命令行里，只能重启进程：按「跨天重启」的同款
        方式优雅收尾（SIGINT 让 segment muxer 写完 moov），再以新参数 spawn。
        录像目录变了同样要重启，否则新的分片还会写进旧目录。
        """
        cfg = cfg or {}
        if cams is not None:
            self.cams = list(cams)
        want_on = bool(cfg.get("record_enable", True))
        seg = runtime.segment_sec(cfg.get("record_segment_sec"))
        new_root = (str(cfg.get("record_dir") or "").strip() or self.root)
        seg_changed = seg != self.seg_sec
        on_changed = want_on != self.enabled
        root_changed = new_root != self.root
        self.seg_sec = seg
        self.enabled = want_on
        if not (seg_changed or on_changed or root_changed):
            return
        if root_changed:
            self.root = new_root
            try:
                os.makedirs(self.root, exist_ok=True)
            except OSError:
                pass

        # 停掉现役进程：tick 会在下个 2 秒节拍用新参数重新拉起（enabled=False 则不再拉起）
        for cid, st in list(self.procs.items()):
            p = st.get("p")
            if p and p.poll() is None:
                try:
                    p.send_signal(signal.SIGINT)
                    p.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        p.kill()
                    except OSError:
                        pass
                self.procs.pop(cid, None)
        if not want_on:
            self.log("录制已停用，现有录制进程已收尾")
        else:
            if root_changed:
                self.log("录像目录改为 %s，录制按新目录重启" % self.root)
            if seg_changed:
                self.log("分片长度改为 %d 秒，录制按新参数重启" % seg)

    def stop(self, timeout=6.0):
        self.stopped = True
        for cid, st in list(self.procs.items()):
            p = st["p"]
            if p.poll() is not None:
                continue
            # 先 SIGINT 让 ffmpeg 自己收尾（写完 moov），再退到 SIGKILL
            try:
                p.send_signal(signal.SIGINT)
            except OSError:
                pass
            deadline = time.time() + timeout
            while time.time() < deadline and p.poll() is None:
                time.sleep(0.15)
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass
        # 收尾后把已封口的 .part 改成 .mp4，停服前最后一个完整分片不丢
        for c in self.cams:
            finalize_parts(self.root, c.get("dir_name") or c["id"])
        self.procs.clear()
        self.log("录制已停止")

    # -- 监督 ------------------------------------------------------

    def _newest_age(self, cam):
        """最新分片「多久没被写过」（秒）；一个分片都没有就返回 None。

        只 stat 当天的目录：跨天看门狗本来就该放它过去，新旧两种目录
        （摄像头名称 / 旧 id）都要看，否则升级后刚换目录的那一路会误判。
        """
        cid = cam["id"]
        newest = 0.0
        for d in dict.fromkeys([cam.get("dir_name") or cid, cid]):
            day = os.path.join(self.root, d, datetime.date.today().isoformat())
            try:
                names = [n for n in os.listdir(day)
                         if n.lower().endswith(".mp4") or n.lower().endswith(".part")]
            except OSError:
                continue
            for n in names:
                try:
                    newest = max(newest, os.stat(os.path.join(day, n)).st_mtime)
                except OSError:
                    continue
        if not newest:
            return None
        return max(0.0, time.time() - newest)

    def tick(self, now=None):
        """一次巡检：收尸、退避重连、跨天换目录。调用方按 2 秒节奏调。

        退避必须靠 `dead` 标记去重：一次死亡只记一次账，
        否则每轮巡检都会重新 spawn，摄像头下线时会把进程表刷成雪崩。
        """
        if not self.enabled or self.stopped:
            return
        if not self.ffmpeg:          # 没找到 ffmpeg（比如开发机）就整轮跳过，别刷异常
            return
        now = now if now is not None else time.time()
        today = datetime.date.today().isoformat()
        for cam in self.cams:
            cid = cam["id"]
            dname = cam.get("dir_name") or cid
            finalize_parts(self.root, dname)   # 先封口，消费者才看得到完整 .mp4
            st = self.procs.get(cid)

            if st is None:
                self._spawn(cam)          # 首次，或启动失败过、退避已到期
                continue

            p = st.get("p")
            if p is None:                 # Popen 都没起来
                if now >= st.get("next", 0):
                    self._spawn(cam, keep_tries=True)
                continue

            code = p.poll()

            if code is None:
                # 跨天：主动重启一次，输出的日期目录才会跟着走
                if st["day"] != today:
                    try:
                        p.send_signal(signal.SIGINT)
                        p.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            p.kill()
                        except OSError:
                            pass
                    self.procs.pop(cid, None)
                    self.log("录制 %s 跨天重启" % cid)
                    self._spawn(cam)
                    continue
                # 稳定运行满 5 分钟，把重试计数清零，避免白天抖动累积成 30 秒退避
                if st.get("tries") and now - st.get("started_at", now) > 300:
                    st["tries"] = 0
                # ---- 卡死看门狗 ----
                # ffmpeg 的 -rw_timeout 只能覆盖"读不到数据"的一部分情形；无线
                # 摄像头半死连接上它仍可能长期阻塞。这里再兜一层：进程在、但磁盘
                # 上最新分片已经 STALL_SEC 秒没长过，就判定为假活，杀掉重连。
                if now - st.get("started_at", now) > STALL_SEC + 30:
                    age = self._newest_age(cam)
                    if age is not None and age > STALL_SEC:
                        self.log("录制 %s 卡住（%d 秒没有新数据），重启进程"
                                 % (cam.get("name") or cid, int(age)))
                        try:
                            p.kill()
                        except OSError:
                            pass
                        try:
                            p.wait(timeout=3)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                        tries = st.get("tries", 0) + 1
                        wait = min(MAX_BACKOFF,
                                   self.reconnect * (2 ** min(tries - 1, 4)))
                        self.procs[cid] = {
                            "p": None, "day": st["day"], "tries": tries,
                            "next": now + wait, "started_at": now, "dead": None,
                        }
                        continue
                continue

            # ---- 进程已退出 ----
            if st.get("dead") == p.pid:
                if now >= st.get("next", 0):
                    self._spawn(cam, keep_tries=True)
                continue

            st["dead"] = p.pid
            st["tries"] = st.get("tries", 0) + 1
            wait = min(MAX_BACKOFF, self.reconnect * (2 ** min(st["tries"] - 1, 4)))
            st["next"] = now + wait
            err = ""
            try:
                err = (p.stderr.read(4096) or b"").decode("utf-8", "replace").strip()
            except (OSError, ValueError):
                pass
            if err:
                self.last_error[cid] = err.splitlines()[-1][:200]
            self.log("录制 %s 中断（code=%s），%d 秒后第 %d 次重连%s"
                     % (cid, code, wait, st["tries"],
                        ("：" + self.last_error[cid]) if err else ""))

    def status(self):
        out = {"enabled": self.enabled, "segment_sec": self.seg_sec,
               "root": self.root, "channels": []}
        for cam in self.cams:
            cid = cam["id"]
            st = self.procs.get(cid)
            p = st.get("p") if st else None
            running = bool(p and p.poll() is None)
            segs = list_segments(self.root, cam.get("dir_name") or cid)
            last = segs[-1] if segs else None
            age = None
            if last:
                try:
                    age = int(time.time() - os.path.getmtime(last))
                except OSError:
                    age = None
            out["channels"].append({
                "id": cid, "name": cam.get("name") or cid,
                "state": "recording" if running else ("idle" if self.enabled else "off"),
                "pid": p.pid if p else None,
                "retry": st["tries"] if st else 0,
                "segments": len(segs),
                "last_seg": os.path.basename(last) if last else None,
                "last_age_sec": age,
                "error": self.last_error.get(cid),
            })
        return out
