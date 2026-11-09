# -*- coding: utf-8 -*-
"""运动事件快照：从 recorder 分片抽一帧补进 event_shots（0.9.134）。

为什么需要这一层
----------------
event_shot.ShotStore 的图只来自 AI 确认那一拍（detect.py 调 offer）。纯帧差的
运动事件（KIND_MOTION，AI 没框住）从不存图，事件列表/详情里 ev.shot 是 None，
用户想"先看一眼"只能点开回放。这一层从 recorder 已录好的分片里抽一帧补上：

  - 时间是录像里真实存在的画面（锚定事件起点 ± slack），不是"现在"的假数据；
  - 复用现成分片与 event_shot 落盘，不新建 RTSP、不碰 recorder.py、不改判定；
  - 落进同一个 event_shots 目录后，shot_of 统一查询，前端零改动。

三条自我约束（与 event_shot 同血脉，改的时候一条都别松）：

  1. **只加不改**。事件查询路径里只做一次幂等判断 + 一次非阻塞入队；抽帧、
     写盘、清过期都在独立线程与 ShotStore 自己的线程里。绝不阻塞查询线程。
  2. **不抛**。抽帧失败（分片过期 / 没 ffmpeg / 磁盘满）只计数，绝不冒到查询
     路径上去 —— 那会把"补图失败"变成"事件列表 500"。
  3. **图是可清理的附属物**。直接复用 event_shot 的 7 天过期、每路每天 300 张、
     队列满丢、30 秒限流 —— 这套护栏本来就是为"附属图"设计的，运动事件图
     混在同一个目录、同一套规则下，治理一致。

调用方：nvr_slim 的 /api/events 对每条 kind=="motion" 且尚无图的事件调一次
request(ev)。request 幂等：窗口内已有图就跳过；否则入队。抽帧在后台线程跑。
"""
import os
import queue
import re
import subprocess
import tempfile
import threading
import time

import event_shot as es
import event
import ledger

# 活动切片（.cNN.mp4）不是全时录像分片：事件定位要开的是事实链上那一片，
# 切片是压缩产物，不是证据本身（同 nvr_slim._day_files 的口径）。
_CLIP_SLICE_RE = re.compile(r"^(.+\.c\d+(\.\d+)?)\.mp4$", re.IGNORECASE)

QUEUE_MAX = 32

# 定位不到分片的事件（分片已过期/被删）在这么久之内不再重试。
# 为什么需要：事件列表按时间**升序**返回，补图从最老的开始投；老事件的录像
# 早就滚掉了，永远抽不出来。没有这道负缓存，每次刷新都会把它们重新塞进队首，
# 把队列槽位吃光，最新的事件反而永远排不上（0.9.135 修的就是这种饥饿）。
MISS_TTL = 600.0
# 负缓存最多记多少条：超了就把过期的清掉再放，避免长跑进程里无限增长。
MISS_MAX = 4096


def epoch_of(date, start):
    """(本地日期, 当天秒) → epoch。文件名与目录都靠它落到正确的那一天。

    用 `mktime(localtime(date 00:00)) + start` 而不是 `time.time()`：后者会把
    图说成"现在"，跨午夜、跨时区都对不上；前者让 name_at(at) 解析回的钟点秒
    就是 start，与事件窗口严丝合缝。
    """
    try:
        base = time.mktime(time.strptime(str(date) + " 00:00:00",
                                        "%Y-%m-%d %H:%M:%S"))
        return base + float(start or 0.0)
    except (TypeError, ValueError):
        return time.time()


def locate_segment(record_root, cam_dirs, date, start):
    """事件起点 → (分片绝对路径, 片内偏移秒) 或 None。

    复用 event.resolve：从文件名取钟点、未封口那片也算、mtime 判是否还在片内。
    分片枚举不依赖全局 cameras/record_dir，是个纯函数，单测可以喂假目录。
    """
    sealed, parts = {}, {}
    for dn in (cam_dirs or ()):
        d = os.path.join(record_root, dn, date)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            low = n.lower()
            if not (low.endswith(".mp4") or low.endswith(".mp4.part")):
                continue
            st = ledger.src_start(n)
            if st is None:
                continue
            if low.endswith(".mp4.part"):
                parts[st] = n
                continue
            if _CLIP_SLICE_RE.match(n):
                continue
            sealed[st] = n
    loc = event.resolve(start, sorted(sealed.keys()), sorted(parts.keys()))
    if loc.get("missing_reason"):
        return None
    name = loc["segment"]
    for dn in (cam_dirs or ()):
        p = os.path.join(record_root, dn, date, name)
        if os.path.isfile(p):
            return (p, float(loc.get("offset_sec") or 0.0))
    return None


class MotionShotService(object):
    """运动事件补图：异步从分片抽帧，交给 ShotStore 落盘。

    构造时注入 ffmpeg 路径、录像根、机位存储目录函数、ShotStore 引用。
    查询路径调 request(ev)（轻量、幂等）；抽帧在独立线程里跑。
    """

    def __init__(self, ffmpeg, var_dir, record_root, cam_dirs_fn, shot_store,
                 log=None, queue_max=QUEUE_MAX):
        self.ffmpeg = ffmpeg or ""
        self.var_dir = var_dir
        self.record_root = record_root
        self.cam_dirs_fn = cam_dirs_fn
        self.shot_store = shot_store
        self.log = log or (lambda m: None)
        self._q = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._th = None
        self.queued = 0
        self.dropped = 0
        self.extracted = 0    # 抽到帧的张数（ffmpeg 成功出图）
        self.stored = 0       # 真正落盘的张数（ShotStore 收下才算，0.9.135）
        self.rejected = 0     # 抽到帧但落盘层没收（日上限 / 限流 / 写盘异常）
        self.failed = 0       # 抽帧失败（没 ffmpeg / 坏的片 / 超时）
        self.missed = 0       # 分片定位不到（过期 / 被删）
        self._miss = {}       # (cam,date,start) -> 该时刻之前不再重试

    # ---------------------------------------------------------- 生命周期
    def start(self):
        if self._th is not None and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, daemon=True,
                                    name="motion-shot")
        self._th.start()

    def stop(self):
        self._stop.set()

    def alive(self):
        return bool(self._th and self._th.is_alive())

    # ---------------------------------------------------------- 入队（查询路径）
    def _is_miss(self, cam, date, start):
        """这条事件刚确认过"分片没了"，还在冷却期里吗。"""
        try:
            key = (str(cam), str(date), round(float(start), 3))
        except (TypeError, ValueError):
            return False
        exp = self._miss.get(key)
        if exp is None:
            return False
        if exp <= time.time():
            self._miss.pop(key, None)
            return False
        return True

    def _mark_miss(self, cam, date, start):
        try:
            key = (str(cam), str(date), round(float(start), 3))
        except (TypeError, ValueError):
            return
        now = time.time()
        if len(self._miss) >= MISS_MAX:
            self._miss = dict((k, v) for k, v in self._miss.items() if v > now)
        self._miss[key] = now + MISS_TTL

    def request(self, ev, slack=es.PICK_SLACK):
        """事件 dict → 入队补图（幂等）。返回是否真的排了队。

        只做：一次 find（5 秒缓存，按 (cam,date) 聚合）+ 一次 put_nowait。
        不读分片、不抽帧、不落盘 —— 这些是后台线程的事，绝不拖累查询线程。
        """
        if not isinstance(ev, dict) or not ev.get("id"):
            return False
        cam = ev.get("cam")
        date = ev.get("date")
        try:
            start = float(ev.get("start") or 0.0)
            end = float(ev.get("end") or 0.0)
        except (TypeError, ValueError):
            return False
        # 负缓存：这一片的录像早就滚掉了，冷却期内别再投（否则过期老事件会
        # 反复占满队列，最新的事件永远排不上）。
        if self._is_miss(cam, date, start):
            return False
        # 幂等：窗口内已有图就跳过（图在就不必再抽，也不该再抽）
        if es.find(self.var_dir, cam, date, start, end, slack):
            return False
        try:
            self._q.put_nowait((str(cam or ""), str(date or ""), start, end))
        except queue.Full:
            self.dropped += 1
            return False
        self.queued += 1
        return True

    # ---------------------------------------------------------- 抽帧（后台线程）
    def _extract(self, path, offset):
        """从分片抽一帧 → 字节；任何异常都返回 None（不抛）。"""
        if not self.ffmpeg:
            return None
        fd, tmp = tempfile.mkstemp(suffix=".jpg.part", prefix="mshot-")
        os.close(fd)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-ss", "%.3f" % max(0.0, offset), "-i", path,
               "-frames:v", "1", "-q:v", "4", "-f", "image2",
               "-c:v", "mjpeg", "-y", tmp]
        try:
            proc = subprocess.run(cmd, timeout=20, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE)
            if proc.returncode != 0:
                return None
            if not os.path.isfile(tmp) or os.path.getsize(tmp) < 1024:
                return None
            with open(tmp, "rb") as f:
                data = f.read(es.MAX_SHOT_BYTES + 1)
            if len(data) > es.MAX_SHOT_BYTES:
                return None
            return data
        except (subprocess.TimeoutExpired, OSError):
            return None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            # 单条坏数据绝不允许打死整个 worker（0.9.134 真机核对：cam_dirs_fn
            # 契约不匹配曾让它在第一条就抛异常、线程静默退出，之后队列只塞不抽、
            # 全丢弃）。每一片的定位/抽帧/落盘都在这层保护里，异常一律计 failed
            # 并继续 —— 这正是模块开头「不抛」那条自我约束的落点。
            try:
                self._handle(item)
            except Exception as e:
                self.failed += 1
                try:
                    self.log("运动事件快照处理异常：%r" % e)
                except Exception:
                    pass

    def _handle(self, item):
        cam, date, start, end = item
        # 再判一次幂等（多个请求竞态：先到的抽到后，后到的就不必抽）
        if es.find(self.var_dir, cam, date, start, end):
            return
        dirs = self.cam_dirs_fn(cam) if self.cam_dirs_fn else ()
        loc = locate_segment(self.record_root, dirs, date, start)
        if loc is None:
            self.missed += 1
            self._mark_miss(cam, date, start)
            return
        path, offset = loc
        data = self._extract(path, offset)
        if not data:
            self.failed += 1
            return
        self.extracted += 1
        if self.shot_store is None:
            return
        at = epoch_of(date, start)
        try:
            # gap=False：补的是**不同事件**，各自该有一张；不关这条闸，30 秒内
            # 的事件会被当成"同一条重复确认"整片丢掉（0.9.135 真机问题）。
            ok = self.shot_store.offer(cam, data, at, gap=False)
        except TypeError:
            # 老签名 / 测试替身不带 gap：退回按位置调用，宁可被限流也别丢图。
            try:
                ok = self.shot_store.offer(cam, data, at)
            except Exception as e:
                self.rejected += 1
                self.log("运动事件快照落盘异常（%s）：%r" % (cam, e))
                return
        except Exception as e:
            self.rejected += 1
            self.log("运动事件快照落盘异常（%s）：%r" % (cam, e))
            return
        if ok:
            self.stored += 1
        else:
            self.rejected += 1

    # ---------------------------------------------------------- 诊断
    def status(self):
        return {"queued": self.queued, "dropped": self.dropped,
                "extracted": self.extracted, "stored": self.stored,
                "rejected": self.rejected, "failed": self.failed,
                "missed": self.missed, "miss_cached": len(self._miss),
                "running": self.alive(), "qsize": self._q.qsize()}
