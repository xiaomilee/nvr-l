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

AI 复核（0.9.137）：抽帧时同一次 ffmpeg 运行多出一份 PPM（原始像素），
若 AI 检测总闸开着、推理引擎就绪，就对这一帧再跑一次 YOLO —— 推得出人员/
车辆就把框随图旁挂（与 AI 确认事件同一份 .json 格式），放大时前端零改动
直接叠框。**事件的 kind 不变**：台账记的是"当时 AI 没确认"，事后复核只是
给图补证据，不改写历史（kind_of 只有一处实现，这条红线不动）。复核开着也
好关着也好，快照都照补 —— 图是本体，框是附属物的附属物。
"""
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque

import event_shot as es
import event
import ledger

try:
    # AI 复核（0.9.137）要用 detect 的纯函数（parse_ppm / CATEGORIES）。同目录
    # 模块，模块级只有 stdlib 导入（numpy 在函数内才 import），单独跑测试也能进。
    import detect
except ImportError:
    detect = None

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

# 最近几条处理轨迹（0.9.136）。为什么要有它：0.9.134/0.9.135 连续两版在测试机
# 全绿、生产机却没图，而 status 只给聚合计数 —— 看得出「没成功」，看不出「卡在
# 哪一步」。轨迹记的是每条事件的完整链路：解析出的目录、目录在不在、当天几个分片、
# 定位到哪个文件、ffmpeg 报什么错、落盘收没收。一次截图就能定位。
TRACE_MAX = 6


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


def scan_day(record_root, cam_dirs, date):
    """某路某天的分片清单 → (sealed, parts, dirs_seen, files_seen)。

    `sealed`/`parts` 是 {起点秒: 文件名}，与 nvr_slim._day_files 同口径；
    额外回两个**诊断**用数字：实际扫到了哪些目录、看过多少个 .mp4 条目。
    补图失败时最需要分清的就是「目录没找对」和「目录对了但没这一片」——
    聚合计数永远分不开，这两个数字能。
    """
    sealed, parts = {}, {}
    dirs_seen, files_seen = [], 0
    for dn in (cam_dirs or ()):
        d = os.path.join(record_root, dn, date)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        dirs_seen.append(dn)
        for n in names:
            low = n.lower()
            if not (low.endswith(".mp4") or low.endswith(".mp4.part")):
                continue
            files_seen += 1
            st = ledger.src_start(n)
            if st is None:
                continue
            if low.endswith(".mp4.part"):
                parts[st] = n
                continue
            if _CLIP_SLICE_RE.match(n):
                continue
            sealed[st] = n
    return sealed, parts, dirs_seen, files_seen


def _resolve(sealed, parts, record_root, cam_dirs, date, start):
    """分片清单已在手时的定位（避免为了诊断再扫一遍目录）。"""
    loc = event.resolve(start, sorted(sealed.keys()), sorted(parts.keys()))
    if loc.get("missing_reason"):
        return None
    name = loc["segment"]
    for dn in (cam_dirs or ()):
        p = os.path.join(record_root, dn, date, name)
        if os.path.isfile(p):
            return (p, float(loc.get("offset_sec") or 0.0))
    return None


def locate_segment(record_root, cam_dirs, date, start):
    """事件起点 → (分片绝对路径, 片内偏移秒) 或 None。

    复用 event.resolve：从文件名取钟点、未封口那片也算、mtime 判是否还在片内。
    分片枚举不依赖全局 cameras/record_dir，是个纯函数，单测可以喂假目录。
    """
    sealed, parts, _dirs, _files = scan_day(record_root, cam_dirs, date)
    return _resolve(sealed, parts, record_root, cam_dirs, date, start)


class MotionShotService(object):
    """运动事件补图：异步从分片抽帧，交给 ShotStore 落盘。

    构造时注入 ffmpeg 路径、录像根、机位存储目录函数、ShotStore 引用。
    查询路径调 request(ev)（轻量、幂等）；抽帧在独立线程里跑。
    """

    def __init__(self, ffmpeg, var_dir, record_root, cam_dirs_fn, shot_store,
                 log=None, queue_max=QUEUE_MAX, detect_ref=None):
        self.ffmpeg = ffmpeg or ""
        self.var_dir = var_dir
        self.record_root = record_root
        self.cam_dirs_fn = cam_dirs_fn
        self.shot_store = shot_store
        self.log = log or (lambda m: None)
        # AI 复核（0.9.137）：detect_ref() → {"engine", "conf_map"} 或 None。
        # 由 nvr_slim 注入一个"每次处理时现查"的函数：AI 检测总闸关、worker 没起、
        # 模型没加载，任一条不满足就返回 None —— 复核跳过，快照照补。不持有
        # Engine 引用本身：设备切换时 worker 会重建，持有旧引用就推理到旧会话。
        self.detect_ref = detect_ref
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
        self.redetected = 0   # 复核出框并随图旁挂的张数（0.9.137）
        self._detect_err = ""   # 最近一次复核跳过/失败的原因（诊断用）
        self._miss = {}       # (cam,date,start) -> 该时刻之前不再重试
        self._trace = deque(maxlen=TRACE_MAX)   # 最近几条处理轨迹（诊断用）
        self._ffmpeg_ok = None                  # None=没探过 / True / False
        self._ffmpeg_err = ""
        self._last_err = ""                     # 最近一次抽帧失败的原因

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

    # ---------------------------------------------------------- ffmpeg 可用性
    def ffmpeg_ready(self):
        """ffmpeg 能不能跑 → (bool, 说明)。结果缓存，不重复探测。

        为什么单独探：`STATE["ffmpeg_bin"]` 拿到的可能是个**不存在或不可执行**的
        路径（换机器、换系统镜像、包内路径变了）。那种情况下 ffmpeg 抽帧永远
        失败、failed 一路涨，但计数看不出「根本没这个程序」。先 `os.access` 试
        X_OK，能跑再 `-version` 确认不是空壳 —— 两层都便宜，各只做一次。
        """
        if self._ffmpeg_ok is not None:
            return self._ffmpeg_ok, self._ffmpeg_err
        p = self.ffmpeg
        if not p:
            self._ffmpeg_ok, self._ffmpeg_err = False, "没有配置 ffmpeg 路径"
            return self._ffmpeg_ok, self._ffmpeg_err
        cand = p
        if not os.path.isabs(cand) or not os.path.exists(cand):
            cand = shutil.which(p) or p        # 允许只配了个 "ffmpeg"，靠 PATH 找
        self.ffmpeg = cand
        if not os.path.isfile(cand):
            self._ffmpeg_ok, self._ffmpeg_err = False, "找不到这个文件：%s" % cand
            return self._ffmpeg_ok, self._ffmpeg_err
        if not os.access(cand, os.X_OK):
            self._ffmpeg_ok, self._ffmpeg_err = False, "没有执行权限：%s" % cand
            return self._ffmpeg_ok, self._ffmpeg_err
        try:
            r = subprocess.run([cand, "-version"], timeout=8,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                self._ffmpeg_ok = False
                self._ffmpeg_err = "-version 退出码 %d：%s" % (
                    r.returncode, (r.stderr or b"")[:120].decode("utf-8", "replace"))
            else:
                self._ffmpeg_ok, self._ffmpeg_err = True, cand
        except (OSError, subprocess.SubprocessError) as e:
            self._ffmpeg_ok, self._ffmpeg_err = False, "无法执行：%r" % e
        return self._ffmpeg_ok, self._ffmpeg_err

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
        """从分片抽一帧 → (jpg 字节, ppm 字节)；jpg None = 失败（不抛）。

        ppm 是给 AI 复核用的：推理吃的是原始像素，JPEG 已经编码过一次再解码
        一次纯属浪费 —— 干脆同一次 ffmpeg 运行里两个输出，jpg 给快照、ppm 给
        推理。ppm 没出来不影响快照（复核跳过、图照存）；jpg 没出来才算失败。
        失败原因写进 `self._last_err`，供诊断轨迹带走。0.9.135 之前这里一律
        静默返回 None，线上只能看到 failed 涨，看不出是「没 ffmpeg」「解不了
        这个编码」还是「时间点超出片长」—— 三种完全不同的处置方式。
        """
        ok, why = self.ffmpeg_ready()
        if not ok:
            self._last_err = why
            return None, None
        jpg, ppm = self._run_both(offset, path)
        if ppm:
            # 字节 → (w, h, rgb) 三元组（detect.parse_ppm 纯函数）。解析失败
            # （截断/不像 P6）就放弃复核，图不受影响。
            ppm = detect.parse_ppm(ppm) if ppm.startswith(b"P6") else None
        if jpg:
            return jpg, ppm
        # 两个输出都没出：退回单输出老路（某些精简版 ffmpeg 对双输出挑刺，
        # 但单输出 image2 + .jpg 仍然出图）。ppm 没了复核跳过，快照优先。
        base = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", "%.3f" % max(0.0, offset), "-i", path,
                "-frames:v", "1", "-q:v", "4", "-f", "image2"]
        # 先指定 mjpeg 编码器；失败再去掉 -c:v 重试一次：某些精简版 ffmpeg
        # 没有独立 mjpeg encoder，但 image2 + .jpg 仍然出图。两次都失败才算失败。
        for extra in (["-c:v", "mjpeg"], []):
            data = self._run_ffmpeg(base + extra)
            if data:
                return data, None
        return None, None

    _PPM_CAP = 16 * 1024 * 1024   # 1080p P6 ≈ 6MB，留一倍余量防呆

    def _run_both(self, offset, path):
        """一次 ffmpeg 运行出 jpg + ppm → (jpg, ppm)。jpg 为 None 时 ppm 也不可信。

        临时文件**必须用真扩展名**（.jpg / .ppm，不用 .part）：ffmpeg 8 靠
        扩展名猜输出格式，怪后缀会拒开输出（退出码 234），即使显式给了
        -f image2 也可能把第二个输出的编码器弄成 mjpeg（0.9.137 真机实测，
        产物头是 FF D8 而不是 P6）。这些是 /tmp 里前缀唯一的私有临时文件，
        写完即读即删，不需要 .part 那种原子性。
        """
        fd, jp = tempfile.mkstemp(suffix=".jpg", prefix="mshot-")
        os.close(fd)
        fd2, pp = tempfile.mkstemp(suffix=".ppm", prefix="mshot-")
        os.close(fd2)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-ss", "%.3f" % max(0.0, offset), "-i", path,
               "-frames:v", "1", "-q:v", "4", "-c:v", "mjpeg", "-y", jp,
               "-frames:v", "1", "-y", pp]
        try:
            proc = subprocess.run(cmd, timeout=20,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                tail = (proc.stderr or b"")[-300:].decode("utf-8", "replace").strip()
                self._last_err = "ffmpeg 退出码 %d：%s" % (
                    proc.returncode, tail.replace("\n", " ") or "(无输出)")
                return None, None
            js = os.path.getsize(jp) if os.path.isfile(jp) else 0
            ps = os.path.getsize(pp) if os.path.isfile(pp) else 0
            if js < 1024:
                self._last_err = "ffmpeg 没写出有效帧（jpg %d 字节）" % js
                return None, None
            if js > es.MAX_SHOT_BYTES:
                self._last_err = "这一帧超过上限（%d 字节）" % js
                return None, None
            with open(jp, "rb") as f:
                jpg = f.read(es.MAX_SHOT_BYTES + 1)
            ppm = None
            if ps > 0:
                with open(pp, "rb") as f:
                    ppm = f.read(self._PPM_CAP + 1)
                if len(ppm) > self._PPM_CAP or not ppm.startswith(b"P6"):
                    ppm = None       # 超限/不像 PPM：复核放弃，图不受影响
            return jpg, ppm
        except subprocess.TimeoutExpired:
            self._last_err = "ffmpeg 超时（20 秒）"
            return None, None
        except OSError as e:
            self._last_err = "ffmpeg 无法执行：%r" % e
            return None, None
        finally:
            for p in (jp, pp):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _run_ffmpeg(self, cmd):
        """跑一次 ffmpeg，产出字节或 None（失败原因写 _last_err）。"""
        fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="mshot-")
        os.close(fd)
        try:
            proc = subprocess.run(cmd + ["-y", tmp], timeout=20,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                tail = (proc.stderr or b"")[-300:].decode("utf-8", "replace").strip()
                self._last_err = "ffmpeg 退出码 %d：%s" % (
                    proc.returncode, tail.replace("\n", " ") or "(无输出)")
                return None
            size = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
            if size < 1024:
                self._last_err = "ffmpeg 没写出有效帧（%d 字节）" % size
                return None
            with open(tmp, "rb") as f:
                data = f.read(es.MAX_SHOT_BYTES + 1)
            if len(data) > es.MAX_SHOT_BYTES:
                self._last_err = "这一帧超过上限（%d 字节）" % len(data)
                return None
            return data
        except subprocess.TimeoutExpired:
            self._last_err = "ffmpeg 超时（20 秒）"
            return None
        except OSError as e:
            self._last_err = "ffmpeg 无法执行：%r" % e
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

    def _note(self, rec):
        """记一条处理轨迹（诊断用，永不抛）。"""
        try:
            rec["ts"] = time.strftime("%H:%M:%S")
            self._trace.append(rec)
        except Exception:
            pass

    # ---------------------------------------------------------- AI 复核（0.9.137）
    def detect_ready(self):
        """AI 复核现在能不能跑 → (engine, conf_map) 或 (None, 原因)。

        每次处理都现查（不缓存）总闸与 worker 状态：用户中途开/关 AI 检测，
        复核应跟着变，而不是等重启。推理输入是 PPM 解出的原始像素，不存在
        "图变了框错位"的问题（框与图是同一次抽帧的产物）。
        """
        if detect is None:
            return None, "detect 模块不可用"
        if self.detect_ref is None:
            return None, "未配置 AI 复核"
        try:
            ref = self.detect_ref()
        except Exception as e:
            return None, "复核引用异常：%r" % e
        if not ref:
            return None, "AI 检测总闸关闭或检测进程未运行"
        engine = ref.get("engine")
        conf_map = ref.get("conf_map") or {}
        if engine is None:
            return None, "推理引擎未就绪"
        return engine, conf_map

    def _redetect(self, ppm):
        """抽出的那一帧 → (dets, 说明)。dets None = 没复核出框（原因进轨迹）。

        只做类别白名单过滤，不做忽略区/只统计区：忽略区是"检测确认"的口径，
        复核是"图上有什么"的口径 —— 用户只想知道框里是不是人，不该被忽略区
        吃掉。没认出目标、引擎没就绪都如实写轨迹，绝不硬造框。
        """
        if ppm is None:
            self._detect_err = "无原始像素（ffmpeg 双输出失败，复核跳过）"
            return None, self._detect_err
        engine, conf_map = self.detect_ready()
        if engine is None:
            self._detect_err = conf_map
            return None, self._detect_err
        try:
            w, h, rgb = ppm
            raw = engine.infer(rgb, w, h, conf_map)
        except Exception as e:
            self._detect_err = "推理异常：%r" % e
            return None, self._detect_err
        if raw is None:
            self._detect_err = "推理不可用：%s" % (getattr(engine, "error", "") or "未知")
            return None, self._detect_err
        dets = [d for d in raw or ()
                if isinstance(d, dict) and d.get("c") in (detect.CATEGORIES or ())]
        if not dets:
            self._detect_err = "复核完成：没认出人员/车辆"
            return None, self._detect_err
        self._detect_err = ""
        return dets, "复核出 %d 框" % len(dets)

    def _handle(self, item):
        cam, date, start, end = item
        t = {"cam": cam, "date": date, "start": round(float(start), 1)}
        # 再判一次幂等（多个请求竞态：先到的抽到后，后到的就不必抽）
        if es.find(self.var_dir, cam, date, start, end):
            t["step"] = "skip"
            t["reason"] = "窗口内已有图"
            self._note(t)
            return
        dirs = list(self.cam_dirs_fn(cam) or ()) if self.cam_dirs_fn else []
        sealed, parts, dirs_seen, files_seen = scan_day(self.record_root, dirs, date)
        t["dirs"] = dirs
        t["dirs_seen"] = dirs_seen
        t["files"] = files_seen          # 当天扫到的 .mp4 条目数（含 .part 与切片）
        t["segs"] = len(sealed)          # 其中能当全时分片用的
        if not dirs_seen:
            self.missed += 1
            self._mark_miss(cam, date, start)
            t["step"] = "dir"
            t["reason"] = "录像目录不存在"
            self._note(t)
            return
        loc = _resolve(sealed, parts, self.record_root, dirs, date, start)
        if loc is None:
            self.missed += 1
            self._mark_miss(cam, date, start)
            t["step"] = "locate"
            t["reason"] = ("目录里有文件但没有可用分片" if files_seen == 0
                           else "事件起点所在分片已被清掉")
            self._note(t)
            return
        path, offset = loc
        t["seg"] = os.path.basename(path)
        t["offset"] = round(offset, 1)
        self._last_err = ""
        data, ppm = self._extract(path, offset)
        if not data:
            self.failed += 1
            t["step"] = "extract"
            t["reason"] = self._last_err or "抽帧失败（原因未知）"
            self._note(t)
            return
        self.extracted += 1
        t["bytes"] = len(data)
        # AI 复核（0.9.137）：对抽出的这一帧再跑一次推理。推得出框就随图旁挂
        # （放大可见「人员 0.87」式的框），推不出就只存图 —— 复核是附属物的
        # 附属物，任何失败都只写轨迹，绝不影响快照本身。
        dets, ai_why = self._redetect(ppm)
        t["ai"] = ai_why
        if dets:
            self.redetected += 1
            t["dets"] = len(dets)
        if self.shot_store is None:
            t["step"] = "extract"
            t["reason"] = "抽到了帧但没有落盘层（shot_store 未就绪）"
            self._note(t)
            return
        at = epoch_of(date, start)
        try:
            # gap=False：补的是**不同事件**，各自该有一张；不关这条闸，30 秒内
            # 的事件会被当成"同一条重复确认"整片丢掉（0.9.135 真机问题）。
            # dets（0.9.137）：复核出的框跟着图走，落盘层写成旁挂 .json，
            # 与 AI 确认事件的框同一份格式 —— 前端放大时零改动直接叠。
            if dets:
                ok = self.shot_store.offer(cam, data, at, dets, gap=False)
            else:
                ok = self.shot_store.offer(cam, data, at, gap=False)
        except TypeError:
            # 老签名 / 测试替身不带 gap：退回按位置调用，宁可被限流也别丢图。
            try:
                ok = self.shot_store.offer(cam, data, at)
            except Exception as e:
                self.rejected += 1
                t["step"] = "store"
                t["reason"] = "落盘异常：%r" % e
                self._note(t)
                self.log("运动事件快照落盘异常（%s）：%r" % (cam, e))
                return
        except Exception as e:
            self.rejected += 1
            t["step"] = "store"
            t["reason"] = "落盘异常：%r" % e
            self._note(t)
            self.log("运动事件快照落盘异常（%s）：%r" % (cam, e))
            return
        if ok:
            self.stored += 1
            t["step"] = "stored"
            t["reason"] = "已落盘"
        else:
            self.rejected += 1
            t["step"] = "store"
            t["reason"] = "落盘层拒收（日上限 / 队列满 / 图片超限）"
        self._note(t)

    # ---------------------------------------------------------- 诊断
    def status(self):
        """诊断快照。**不探盘、不跑 ffmpeg** —— 这个接口几秒轮询一次。

        ffmpeg 的真实可用性只在真正抽帧时探（`_ffmpeg_ok` 是那次的结果）：
        这里报 None 就是"还没抽过帧"，报 False 才会带具体原因。
        """
        return {"queued": self.queued, "dropped": self.dropped,
                "extracted": self.extracted, "stored": self.stored,
                "rejected": self.rejected, "failed": self.failed,
                "missed": self.missed, "miss_cached": len(self._miss),
                "redetected": self.redetected,
                "running": self.alive(), "qsize": self._q.qsize(),
                "record_root": self.record_root,
                "ffmpeg": {"path": self.ffmpeg, "ok": self._ffmpeg_ok,
                           "err": self._ffmpeg_err, "last_err": self._last_err},
                "detect": {"err": self._detect_err},
                "trace": [dict(x) for x in self._trace]}

    def _writable_target(self, path):
        """往上一层层找到第一个存在的目录 → (目录, 可写?)。"""
        p = path
        while p and not os.path.isdir(p):
            np = os.path.dirname(p)
            if np == p:
                break
            p = np
        try:
            return p, bool(os.access(p, os.W_OK))
        except OSError:
            return p, False

    def probe(self, cam, date, start=None):
        """按机位当场自检 → 一份人能读懂的报告。只读，不抽帧、不落盘。

        为什么需要：`stored=0` 这一个数字，可能是"目录找错了""分片没了""ffmpeg
        跑不动""目录写不进"四件事里的任何一件，而处理方式完全不同。这个接口
        把四件事一次问完，排障不用再猜。
        """
        out = {"cam": cam, "date": date, "record_root": self.record_root}
        try:
            ok, why = self.ffmpeg_ready()
            out["ffmpeg"] = {"path": self.ffmpeg, "ok": ok, "why": why}
            # 复核能力（0.9.137）：探一遍"现在这一刻"能不能跑推理，不真推理。
            engine, conf_map = self.detect_ready()
            out["redetect"] = {"ok": engine is not None,
                               "why": ("" if engine is not None else conf_map)}
            dirs = list(self.cam_dirs_fn(cam) or ()) if self.cam_dirs_fn else []
            out["dirs"] = dirs
            sealed, parts, dirs_seen, files_seen = scan_day(self.record_root, dirs, date)
            out["dirs_seen"] = dirs_seen
            out["dirs_tried"] = [os.path.join(self.record_root, d, date) for d in dirs]
            out["files"] = files_seen
            out["segs"] = len(sealed)
            out["parts"] = len(parts)
            out["seg_first"] = min(sealed) if sealed else None
            out["seg_last"] = max(sealed) if sealed else None
            if start is not None:
                st = float(start)
                loc = _resolve(sealed, parts, self.record_root, dirs, date, st)
                out["locate"] = {"start": st,
                                 "seg": os.path.basename(loc[0]) if loc else None,
                                 "offset": round(loc[1], 1) if loc else None}
            try:
                out["shots_today"] = len(es.list_for(self.var_dir, cam, date))
            except Exception as e:
                out["shots_today"] = None
                out["shots_err"] = repr(e)
            d = es.dir_of(self.var_dir, cam, date)
            where, can = self._writable_target(d)
            out["shot_dir"] = d
            out["shot_write_to"] = where
            out["shot_writable"] = can
        except Exception as e:
            out["error"] = repr(e)
        return out
