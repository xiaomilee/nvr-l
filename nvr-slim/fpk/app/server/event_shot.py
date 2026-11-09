# -*- coding: utf-8 -*-
"""事件快照：把检测器**确认那一刻**看到的那一帧留下来（0.9.129）。

为什么要有这一层
----------------
事件页原来只有一行字：`10:23:35 · 院子 · 8 秒 · 人员`。用户想判断"这条值不值得
点开"，只能先点进去播一遍；而播一遍要拉分片、seek、解码 —— 代价比"看一眼"高两个
数量级。这一层就是那一眼。

三条口径（改之前先看这三条）
----------------------------
1. **拍的是「当时」，不是「现在」。** 存的是检测这一拍读到的那张 jpg 的字节，
   时间取**这张图自己的 mtime**（取帧层落盘的时刻），不是事件起点、也不是当前
   时钟。绝不允许拿 `snapshots/<cam>.jpg` 当事件图充数 —— 那是几秒后的画面，
   属于"看起来对、其实没验证过"的假数据。没有就如实说没有。
2. **只加不改。** 存图发生在台账写入**之后**，检测线程只做一次内存读 + 一次
   非阻塞入队；写盘在独立单线程里。队列满就丢。不 wait、不建 RTSP、不碰
   `recorder.py`、不动判定与确认的任何一行 —— 图是附属物，事实链是主线。
3. **不留清不掉的东西。** 图随日期目录 7 天过期，每路每天硬上限 300 张，
   **不进事件索引表**：索引是台账的投影，图不是事实；把路径写进索引，清理之后
   就会出现一堆指向空气的行。

存储布局
--------
    var/event_shots/<cam>/<YYYY-MM-DD>/<HHMMSS>_<mmm>.jpg
    var/event_shots/<cam>/<YYYY-MM-DD>/<HHMMSS>_<mmm>.json   （检测框，0.9.130）

- 日期目录与文件名都取**图片时刻**，不取事件时刻：跨零点时图落在正确的那一天，
  查询跨天不会漏；事件被重判外推、被 merge 合并时文件名也不会跟着变。
- 写入走 `.part` + `os.replace`（与 `frames.FrameTap._write` 同一套原子替换），
  读的人永远拿不到半张图。
- 框是**旁挂**的：json 丢了只是"这张没框"，图还在；图不在时也绝不拿 json 去
  编一张不存在的快照。两件事不互相牵连。

本模块不 import detect / nvr_slim：它只认字节、时间与框，单测可以直接喂假数据。
"""
import json
import os
import queue
import re
import shutil
import threading
import time

# 保留 7 天：与录像保留期的默认值同量级。图是"那一眼"，不是档案。
KEEP_DAYS = 7
# 每路每天最多 300 张（单张按 150KB 估 ≈ 45MB/天/路）。到顶就不再存，
# 日志记一次 —— 静默丢会让"事件页突然全没图"变成无解的谜。
MAX_PER_DAY = 300
# 同一路两条事件至少隔 30 秒才收第二张：一条 8 秒事件在检测节拍里会被多次
# 确认（每次 _write_ledger），不限流就是同一个画面存十份。
MIN_GAP_SEC = 30.0
# 队列上限 8：写盘是一张 100KB 的顺序写，正常几百微秒；能积压说明磁盘在打摆。
# 满了宁可丢图也不阻塞检测线程 —— 那才是会连累录像的路径。
QUEUE_MAX = 8
# 查询窗口外扩：事件起点是判定出来的，图可能落在它前面或后面几秒。
# 15 秒 > 一个抽帧间隔（默认 10 秒），保证"确认那一帧"一定落在窗口里。
PICK_SLACK = 15.0
# 单张上限：取帧层出的是 q:v4 / 720p（真机 45~180KB）。超过 2MB 只可能是
# 读到了别的文件（或写坏），存进去只会白白占盘。
MAX_SHOT_BYTES = 2 * 1024 * 1024
# 列目录的缓存：一次列表请求可能问 200 条事件，同一天同一路只扫一次。
CACHE_TTL = 5.0
# 检测框旁挂文件的扩展名（0.9.130）。与 jpg 同名不同后缀，清理时一起随目录走。
META_SUFFIX = ".json"
# 一张图最多留 20 个框：真机上人/车同时出现也就几个；几十个框时用户看的是
# "有没有目标"，不是逐框盘点，留太多只会让浮层上全是线。
MAX_BOXES = 20

DAY_SEC = 86400.0
DIR_NAME = "event_shots"

_NAME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d{3})\.jpg$")
# 月/日都限了范围：`2026-13-09` 能过 `\d{2}`，但它不是日期，照着它建目录
# 只会攒出一堆永远不会被清理规则命中的垃圾。
_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
# 与 norm_cameras 放行的字符同一套。目录名直接来自摄像头 id，不校验就等于
# 让配置里的一个 `../` 把图写到别处去。
_CAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_CACHE = {}
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------- 命名与路径

def parse_name(name):
    """`HHMMSS_mmm.jpg` → 当天钟点秒（含毫秒）；认不出来返回 None（不抛）。

    只解析文件名、不 stat：文件名是这套存储唯一的索引，读它比读 inode 便宜。
    时分秒越界（24:00:00 之类）也算认不出来 —— 那种名字只可能是手写的。
    """
    m = _NAME_RE.match(str(name or ""))
    if not m:
        return None
    h, mi, s, ms = (int(m.group(1)), int(m.group(2)),
                    int(m.group(3)), int(m.group(4)))
    if h > 23 or mi > 59 or s > 59:
        return None
    return h * 3600.0 + mi * 60.0 + s + ms / 1000.0


def name_of(sec):
    """当天钟点秒 → `HHMMSS_mmm.jpg`。毫秒保留三位：同一秒内的两条事件
    （理论上不该有）也不会互相覆盖。"""
    v = max(0.0, min(DAY_SEC - 0.001, float(sec or 0.0)))
    total_ms = int(round(v * 1000.0))
    h, rest = divmod(total_ms, 3600000)
    mi, rest = divmod(rest, 60000)
    s, ms = divmod(rest, 1000)
    return "%02d%02d%02d_%03d.jpg" % (h, mi, s, ms)


def name_at(at):
    """epoch → 文件名（**本地时间**）。

    毫秒取 epoch 的小数部分，不从 `localtime` 拼：`tm_sec` 只有整秒，用它
    拼出来的文件名全是 `_000`，同一秒的两张图就会互相覆盖。
    """
    at = float(at or 0.0)
    lt = time.localtime(at)
    ms = int(round((at - int(at)) * 1000.0)) % 1000
    return "%02d%02d%02d_%03d.jpg" % (lt.tm_hour, lt.tm_min, lt.tm_sec, ms)


def root_of(var_dir):
    return os.path.join(var_dir, DIR_NAME)


def dir_of(var_dir, cam, date):
    """`var/event_shots/<cam>/<date>`；cam / date 不合法返回 None。"""
    cam, date = str(cam or ""), str(date or "")
    if not _CAM_RE.match(cam) or not _DATE_RE.match(date):
        return None
    return os.path.join(root_of(var_dir), cam, date)


def path_of(var_dir, cam, date, name):
    """文件名 → 绝对路径；cam / date / name 任一不合法返回 None。

    这一层是防穿目录的唯一入口：`..`、绝对路径、子目录名在这里就被正则挡住，
    调用方不需要（也不该）再自己拼一遍路径。
    """
    d = dir_of(var_dir, cam, date)
    if d is None or parse_name(name) is None:
        return None
    return os.path.join(d, str(name))


def meta_path_of(var_dir, cam, date, name):
    """`HHMMSS_mmm.jpg` → 同名 `.json` 的绝对路径；参数不合法返回 None。

    只换后缀、不拼新目录：jpg 能过 `path_of` 的校验，json 就一定也在同一个
    合法目录里，穿目录的口子在 `path_of` 一处就够了。
    """
    p = path_of(var_dir, cam, date, name)
    if p is None:
        return None
    return p[:-len(".jpg")] + META_SUFFIX


# ---------------------------------------------------------------- 框（检测输出）

def boxes_of(dets, limit=MAX_BOXES):
    """检测结果 → 落盘用的框清单（归一化 `cx,cy,w,h`，0~1）。

    为什么要存框：缩略图上"有没有人"是一回事，"检测器当时框住了什么"是另一回事。
    框是**推理输出**，不是事后补画的；存下来放大时才能原样叠回去 —— 这也是
    "数字必须有出处"在这一页的落点：用户看到框，就知道这条事件不是凭空来的。

    为什么要夹：postprocess 已经归一化，但贴边的框可能略出 0~1（比如 cx=0.999
    再加半个宽）。前端按百分比定位，超出去的框会跑到图外面。

    为什么限数量：一张 720p 图上几十个框时，用户要判断的是"有人吗"，不是
    逐框盘点；保留置信度最高的前 N 个。

    认不出来的条目直接跳过，绝不抛 —— 这个函数在检测线程里被调用。
    """
    out = []
    for d in dets or ():
        if not isinstance(d, dict):
            continue
        try:
            b = d.get("b") or ()
            cx, cy, bw, bh = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        except (TypeError, ValueError, IndexError):
            continue
        if not (bw > 0 and bh > 0):
            continue                    # 退化框：宽高为 0 画出来就是一根线
        try:
            score = max(0.0, min(1.0, float(d.get("s") or 0.0)))
        except (TypeError, ValueError):
            score = 0.0
        out.append({"c": str(d.get("c") or "")[:24], "s": round(score, 3),
                    "b": [round(max(0.0, min(1.0, cx)), 4),
                          round(max(0.0, min(1.0, cy)), 4),
                          round(min(1.0, bw), 4), round(min(1.0, bh), 4)]})
    out.sort(key=lambda x: (-x["s"], x["c"]))
    return out[:max(0, int(limit))]


def read_meta(var_dir, cam, date, name):
    """读旁挂框清单 → `{boxes, at}`；没有 / 坏了都返回 None（不抛）。

    坏 JSON 与前一张图的情况一样：**当作没存框**，不修、不重建、不报错。
    框是附属物里的附属物，为它去猜一份"看起来对"的数据就是评审点名的假数据。
    """
    p = meta_path_of(var_dir, cam, date, name)
    if p is None:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    boxes = boxes_of(raw.get("boxes"))
    at = raw.get("at")
    try:
        at = round(float(at), 3) if at is not None else None
    except (TypeError, ValueError):
        at = None
    return {"boxes": boxes, "at": at}


def _slack_date(date, days):
    """日期字符串 ± N 天。用 `time` 而不是 `datetime`：本模块其余部分也只
    依赖 time，跨零点那种边角不值得为此多一个 import 家族。"""
    try:
        t = time.mktime(time.strptime(str(date) + " 12:00:00", "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None
    return time.strftime("%Y-%m-%d", time.localtime(t + days * DAY_SEC))


# ---------------------------------------------------------------- 读（查询用）

def list_for(var_dir, cam, date):
    """某路某天的文件名清单（缓存 5 秒）。目录不在就是空列表，绝不抛。

    缓存的意义：一次 `/api/events` 可能问 200 条事件，而它们只涉及几个
    (cam, date) 组合。不缓存就是 200 次 listdir —— 在 NAS 的机械盘上这不是
    小数目。5 秒也足够短：刚存下的图下一轮刷新就能看见。
    """
    key = (str(var_dir), str(cam), str(date))
    now = time.time()
    with _CACHE_LOCK:
        got = _CACHE.get(key)
        if got is not None and now - got[0] < CACHE_TTL:
            return got[1]
    d = dir_of(var_dir, cam, date)
    names = []
    if d is not None:
        try:
            names = [n for n in os.listdir(d) if parse_name(n) is not None]
        except OSError:
            names = []          # 目录不存在 / 没权限：就是"没有图"，不是错误
    names.sort()
    with _CACHE_LOCK:
        _CACHE[key] = (now, names)
    return names


def cache_clear():
    """清掉列目录缓存（测试与"刚存了一张马上要读"的场合）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def pick(names, a, b):
    """窗口 [a, b]（当天钟点秒，闭区间）内**最早**的一张 → (name, sec)。

    为什么取最早：那是"最接近事件开始的那一眼"，也是用户最想确认的画面
    （人刚进门 / 车刚进院）。取最近一张会拿到事件中段，看起来像"后来才来的"。
    """
    best = None
    for n in names or ():
        sec = parse_name(n)
        if sec is None or sec < a or sec > b:
            continue
        if best is None or sec < best[1]:
            best = (n, sec)
    return best


def find(var_dir, cam, date, start, end, slack=PICK_SLACK):
    """事件窗口 → 一张图：`{name, cam, date, sec}`；没有返回 None。

    `sec` 是**相对事件日期**的当天秒，跨零点的图会 > 86400 —— 调用方拿它减
    `start` 就得到"事件开始后多少秒"，不必自己判断跨天。
    """
    try:
        a = float(start or 0.0) - float(slack)
        b = float(end or 0.0) + float(slack)
    except (TypeError, ValueError):
        return None
    lo, hi = a, b
    got = pick(list_for(var_dir, cam, date), lo, min(hi, DAY_SEC - 0.001))
    if got is not None:
        return {"name": got[0], "cam": str(cam), "date": str(date), "sec": got[1]}
    # 事件末尾跨过零点：图落在第二天，文件名是"第二天的钟点秒"。
    # 只在这一种情况下多扫一个目录，平时的查询成本不变。
    if hi > DAY_SEC - 0.001:
        nd = _slack_date(date, 1)
        if nd:
            got = pick(list_for(var_dir, cam, nd), 0.0, hi - DAY_SEC)
            if got is not None:
                return {"name": got[0], "cam": str(cam), "date": nd,
                        "sec": got[1] + DAY_SEC}
    return None


# ---------------------------------------------------------------- 写（存放用）

class ShotStore:
    """有界队列 + 单写线程。`offer()` 绝不阻塞、绝不抛。"""

    def __init__(self, var_dir, log=None, keep_days=KEEP_DAYS,
                 max_per_day=MAX_PER_DAY, min_gap=MIN_GAP_SEC,
                 queue_max=QUEUE_MAX):
        self.var_dir = var_dir
        self.root = root_of(var_dir)
        self.log = log or (lambda m: None)
        self.keep_days = max(1, int(keep_days))
        self.max_per_day = max(1, int(max_per_day))
        self.min_gap = max(0.0, float(min_gap))
        self.queue_max = max(1, int(queue_max))
        self.written = 0
        self.dropped = 0            # 队列满丢掉
        self.failed = 0             # 写盘失败
        self.rate_limited = 0       # 30 秒限流挡掉
        self.capped = 0             # 日上限挡掉
        self.meta = 0               # 带框的图（0.9.130）：有几张真的存下了框
        self.meta_failed = 0        # 框写盘失败（图还在，只是没有框）
        self._q = queue.Queue(maxsize=self.queue_max)
        self._stop = threading.Event()
        self._th = None
        self._lock = threading.Lock()
        self._last = {}             # cam -> 上一次收图的时刻（epoch）
        self._day_n = {}            # (cam, date) -> 当天已收张数
        self._cap_logged = set()    # 已就"日上限"记过日志的 (cam, date)
        self._swept = ""            # 上一次 sweep 的日期
        self._du = (0.0, 0)         # (算的时刻, 字节数)
        self._write_err = ""

    # -------------------------------------------------------------- 生命周期
    def start(self):
        if self._th is not None and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, daemon=True,
                                    name="event-shot")
        self._th.start()
        # 启动就清一次：停机几天再开机，不该先攒着旧图等"今天第一张"。
        try:
            self.sweep()
        except Exception as e:
            self.log("事件快照清理异常：%r" % e)

    def stop(self):
        self._stop.set()

    def alive(self):
        return bool(self._th and self._th.is_alive())

    # -------------------------------------------------------------- 收图
    def offer(self, cam, data, at=None, dets=None):
        """检测线程调用：限流 → 判日上限 → 入队。返回是否收下。

        这里**只做**：一次时间比较、一次计数、一次 put_nowait。不做磁盘写、
        不做目录创建 —— 那些都在写线程里。检测线程多花的每一毫秒，都是从
        "8 路一轮 ≤ 1.8 × 单路"预算里扣的。

        `dets` 是这一拍的保留框（`apply_regions` 之后的）。转换在这里做一次
        （`boxes_of` 只是几个 float 转换，几十微秒量级），写盘仍然留给写线程。
        """
        cam = str(cam or "")
        if not _CAM_RE.match(cam) or not data:
            return False
        if len(data) > MAX_SHOT_BYTES:
            return False
        at = float(at or time.time())
        boxes = boxes_of(dets)
        date = time.strftime("%Y-%m-%d", time.localtime(at))
        with self._lock:
            last = self._last.get(cam)
            if last is not None and at - last < self.min_gap:
                self.rate_limited += 1
                return False
            n = self._day_n.get((cam, date))
            if n is None:
                # 每个 (cam, date) 每个进程只数一次目录，之后靠内存计数。
                # 这一次 listdir 发生在当天第一张图上，不在热路径里。
                n = len(list_for(self.var_dir, cam, date))
            if n >= self.max_per_day:
                self.capped += 1
                if (cam, date) not in self._cap_logged:
                    self._cap_logged.add((cam, date))
                    self.log("事件快照已达每路每日上限（%d 张）：%s %s 之后的事件不再留图"
                             % (self.max_per_day, cam, date))
                return False
            self._last[cam] = at
            self._day_n[(cam, date)] = n + 1
            name = name_at(at)
        d = dir_of(self.var_dir, cam, date)
        try:
            self._q.put_nowait((d, cam, date, name, data, boxes, at))
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False
        return True

    # -------------------------------------------------------------- 写线程
    def _loop(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                continue
            d, cam, date, name, data, boxes, at = item
            try:
                os.makedirs(d, exist_ok=True)
                tmp = os.path.join(d, name + ".part")
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, os.path.join(d, name))
                with self._lock:
                    self.written += 1
                # 刚写的那张要让下一次读能看见：只丢这一个键，不清整张缓存
                # （同一请求里另外几路/几天的清单还能继续命中）。
                with _CACHE_LOCK:
                    _CACHE.pop((str(self.var_dir), cam, date), None)
                self._write_meta(d, name, boxes, at)
                self._maybe_sweep()
            except OSError as e:
                with self._lock:
                    self.failed += 1
                msg = "事件快照写盘失败：%s" % e
                if msg != self._write_err:      # 同一个错误只刷一次
                    self._write_err = msg
                    self.log(msg)

    def _write_meta(self, d, name, boxes, at):
        """框清单落盘（0.9.130）。**图已经写完了**，这里失败只丢框，不丢图。

        单独 try：外层那个 except 会把失败记成"图写失败"，而这张图其实是好的。
        用户看到的是"图在、没有框"，与"图都没了"必须分开计数。
        """
        if not boxes:
            return
        target = os.path.join(d, name[:-len(".jpg")] + META_SUFFIX)
        try:
            tmp = target + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"at": round(float(at or 0.0), 3), "boxes": boxes},
                          f, ensure_ascii=False)
            os.replace(tmp, target)
            with self._lock:
                self.meta += 1
        except (OSError, ValueError, TypeError) as e:
            with self._lock:
                self.meta_failed += 1
            msg = "事件快照框写盘失败：%s" % e
            if msg != self._write_err:
                self._write_err = msg
                self.log(msg)

    def _maybe_sweep(self):
        """每天第一次写顺带清一次过期目录。

        为什么不是定时器：多一个线程就多一处要停的东西，而"一天清一次"对
        7 天保留期完全够。清理放在写线程里做，检测线程一秒都不占。
        """
        today = time.strftime("%Y-%m-%d")
        if self._swept == today:
            return
        self._swept = today
        try:
            self.sweep()
        except Exception as e:
            self.log("事件快照清理异常：%r" % e)
        with self._lock:
            self._du = (0.0, 0)                 # 清完让占用重算

    def sweep(self, now=None):
        """删掉保留期之外的日期目录，返回删掉的目录数。

        `keep_days=7` 表示"留今天 + 前 6 天"：`cutoff = 今天 - 6 天`，
        比它更早的日期目录整个删掉。只删 `root/<cam>/<YYYY-MM-DD>` 这一层，
        形状不对的目录一律不碰（宁可留一点垃圾，也不能删错东西）。
        """
        now = now if now is not None else time.time()
        cutoff = time.strftime("%Y-%m-%d", time.localtime(
            now - (self.keep_days - 1) * DAY_SEC))
        removed = 0
        try:
            cams = os.listdir(self.root)
        except OSError:
            return 0
        for cam in cams:
            if not _CAM_RE.match(cam):
                continue
            cdir = os.path.join(self.root, cam)
            try:
                days = os.listdir(cdir)
            except OSError:
                continue
            for day in days:
                if not _DATE_RE.match(day) or day >= cutoff:
                    continue
                target = os.path.join(cdir, day)
                if not os.path.isdir(target):
                    continue
                try:
                    shutil.rmtree(target)
                    removed += 1
                except OSError as e:
                    self.log("清不掉旧事件快照 %s：%r" % (target, e))
        if removed:
            with _CACHE_LOCK:
                _CACHE.clear()
            self.log("清掉 %d 个过期事件快照目录（保留 %d 天）"
                     % (removed, self.keep_days))
        return removed

    # -------------------------------------------------------------- 状态
    def status(self):
        """诊断用：写了几张、丢了几张、占多大。占用量 60 秒算一次。"""
        now = time.time()
        with self._lock:
            at, size = self._du
            if now - at > 60.0:
                need = True
            else:
                need = False
            snap = {"written": self.written, "dropped": self.dropped,
                    "failed": self.failed, "rate_limited": self.rate_limited,
                    "capped": self.capped, "queued": self._q.qsize(),
                    "meta": self.meta, "meta_failed": self.meta_failed,
                    "running": self.alive(), "keep_days": self.keep_days,
                    "max_per_day": self.max_per_day,
                    "min_gap_sec": self.min_gap,
                    "dir": self.root, "error": self._write_err}
        if need:
            size = _du(self.root)
            with self._lock:
                self._du = (now, size)
        snap["bytes"] = size
        return snap


def _du(root):
    """目录占用字节数。只在 status() 里调用，且带 60 秒缓存。"""
    total = 0
    for base, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                continue
    return total


# ---------------------------------------------------------------- 对外形状

def shot_of(var_dir, ev, slack=PICK_SLACK):
    """事件 dict → 列表里那个 `shot` 字段（None 或 {url, at, offset}）。

    契约固定在这里，前端只判 `if(ev.shot)`：
      url    服务端给（这一页的既有契约就是"不自己拼地址"，图片同理）
      at     图片真实时刻 `HH:MM:SS`
      offset at - 事件起点（秒，负数表示图比事件起点还早）
    没有图就是 None，不是空对象 —— 空对象会让前端 `if(ev.shot)` 判真。
    """
    if not isinstance(ev, dict) or not ev.get("id"):
        return None
    hit = find(var_dir, ev.get("cam"), ev.get("date"),
               ev.get("start"), ev.get("end"), slack)
    if not hit:
        return None
    start = float(ev.get("start") or 0.0)
    at = hit["sec"]
    return {"url": "/api/events/" + str(ev["id"]) + "/shot",
            "at": _hhmmss(at),
            "offset": round(at - start, 1)}


def meta_of(var_dir, hit, start):
    """`find()` 的结果 → 框元数据（0.9.130）。

    只在**已经确认图存在**之后调用（路由先开了图才回这份 JSON），所以这里
    不再判断"有没有图"：图在、框可能不在（0.9.129 的老图，或框写盘失败）。
    两种情况都回 `boxes: []` + `has_boxes: false`，前端据此说清是哪一种。

    `offset` 与列表里的 `shot.offset` **同一个算式**：前端不重新减，
    两处各算一次迟早会分叉。
    """
    boxes = []
    raw = read_meta(var_dir, hit.get("cam"), hit.get("date"), hit.get("name"))
    if raw:
        boxes = raw["boxes"]
    at = float(hit.get("sec") or 0.0)
    return {"at": _hhmmss(at), "offset": round(at - float(start or 0.0), 1),
            "boxes": boxes, "count": len(boxes), "has_boxes": bool(boxes)}


def _hhmmss(sec):
    s = int(max(0, min(int(DAY_SEC) - 1, int(float(sec or 0)))))
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)
