# -*- coding: utf-8 -*-
"""事件中心的事件模型：身份 / 生命周期 / 定位，三件事各只有一处实现。

为什么单独一层（评审点名的第二个工程问题）：现在事件在前端是"拼出来的"——
`/api/ledger` 每天每路吐一串 `{s,e,d,cls,mv}`，没有 id、没有状态、也没有
"这条能不能播"。想从事件跳到录像，前端只能拿日期 + 起始秒自己拼文件名；
跨分片、未封口、已清理这几种情况它一律不知道，于是用户点下去只能看到一个
播不动的黑框。

这里把三件事一次定死：

  身份  `event_id = <cam>_<YYYYMMDD>_<HHMMSS>`，只由「哪一路 + 何时开始」决定。
        延长结束时间、重判、重建索引、服务重启都不会产生新 id —— 前端书签和
        通知里的链接因此不会用一次就废。
  状态  DETECTED → CANDIDATE → CONFIRMED → ACTIVE → ENDED → INDEXED，只许前进。
        台账每 300 秒重判一次，同一段会被反复重算；允许回退的话，一条已结束的
        事件会在下一次重判时变回"进行中"，列表一直闪、永远刷不出稳定状态。
  定位  事件时间 → (分片, 片内偏移)。这是"点事件直接看"的唯一依据，时间与
        文件名之间的换算整个系统只出现在 `locate()` 里一处。

本模块纯计算：不读盘、不看时钟（now 由调用方传），单测可以直接喂数字。
"""
import re

import ledger

# 与 detect.CATEGORIES 同序（test_event 里有结构守卫钉住）：
# 这里不 import detect，是为了让事件模型不依赖推理引擎那条链
# （detect 迟早会 import numpy/onnx 相关的东西，事件列表不该被它拖下水）。
CATEGORIES = ("person", "two_wheeler", "vehicle")

STATES = ("detected", "candidate", "confirmed", "active", "ended", "indexed")

STATE_LABEL = {
    "detected": "已发现",
    "candidate": "待确认",
    "confirmed": "已确认",
    "active": "进行中",
    "ended": "已结束",
    "indexed": "已入库",
}

# 窗口终点距今不超过它就还算"进行中"。判定是按分片（默认 300 秒）出的，
# 刚停下的活动很容易在下一次判定之前又被续上；抢先把状态改成"已结束"
# 只会让列表每次刷新都抖一下。
LIVE_TAIL_SEC = 90.0

# 点事件从「事件起点前 10 秒」开始播。人/车通常在事件被确认之前就已经在
# 画面里了，从起点起播会看到"凭空出现"；10 秒也是看清一个进出动作的最短前摇。
PRE_SEC = 10.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^(?P<cam>.+)_(?P<date>\d{8})_(?P<sec>\d{6})$")


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 时间与身份

def valid_date(s):
    """日期字符串是否可用（YYYY-MM-DD），与 ledger.valid_date 同口径。"""
    return bool(_DATE_RE.match(str(s or "")))


def hhmmss(sec):
    """当天钟点秒 → HH:MM:SS；越界一律夹到 0~86399，绝不抛。"""
    s = int(max(0, min(86399, int(_num(sec)))))
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def iso_at(date, sec):
    """(日期, 当天钟点秒) → 'YYYY-MM-DD HH:MM:SS'。

    用本地时间字符串而不是 epoch：字符串比较就是时间序，索引里能直接
    range 查询，也不用把时区/夏令时算进来（中国无夏令时，本地时间单调）。
    """
    return "%s %s" % (date, hhmmss(sec))


def event_id(cam, date, start):
    """事件身份：`<cam>_<YYYYMMDD>_<HHMMSS>`；输入不合法返回 None。

    为什么不含结束时间、不含判定批次：结束时间是会变的（事件还在延长），
    批次号每次重判都不同 —— 任何一个进了 id，同一条事件就会在列表里
    变成两条，用户昨天点过的链接今天就打不开了。

    代价是「同一路同一秒只可能有一条事件」。这在本系统成立：台账一天最多
    几百个窗口，窗口之间至少隔 MOTION_GAP_SEC（60 秒），秒级撞车不可能；
    真撞了也只丢一条合并结果，不会多出假事件。
    """
    d = str(date or "")
    if not valid_date(d):
        return None
    return "%s_%s_%s" % (str(cam), d.replace("-", ""), hhmmss(start).replace(":", ""))


def parse_id(eid):
    """event_id → {cam, date, start}；认不出来返回 None（不抛）。

    按右往左切：`norm_cameras` 放行 `[A-Za-z0-9_-]`，用户自建的 id 完全可能是
    `cam_front`；从左边切会把 cam 切坏，鉴权/权限类 bug 都是这么来的。
    """
    m = _ID_RE.match(str(eid or ""))
    if not m:
        return None
    sec = m.group("sec")
    h, mi, s = int(sec[0:2]), int(sec[2:4]), int(sec[4:6])
    if h > 23 or mi > 59 or s > 59:
        return None
    d = m.group("date")
    return {"cam": m.group("cam"),
            "date": "%s-%s-%s" % (d[0:4], d[4:6], d[6:8]),
            "start": float(h * 3600 + mi * 60 + s)}


# ---------------------------------------------------------------- 生命周期

def state_rank(state):
    """状态序号。认不出来的状态给 -1：既不会被当成"已完成"，
    也不会在 next_state 里压住一个正常状态。"""
    try:
        return STATES.index(str(state or ""))
    except ValueError:
        return -1


def next_state(cur, want):
    """两个状态取更靠后的那个 —— 生命周期只许前进。

    唯一例外是 INDEXED 不许越过 ACTIVE：正在进行的窗口还没停，把它标成
    "已入库"等于说"这条已经定型可以放心引用"，而它的结束时间随时会变。
    等它真的结束了再入库。
    """
    if not cur:
        return want
    if not want:
        return cur
    if want == "indexed" and cur == "active":
        return cur
    return want if state_rank(want) > state_rank(cur) else cur


def categories_of(cls):
    """类别计数 → 固定顺序的类别列表（去 0、忽略不认识的键）。

    顺序固定是给界面用的：同一条事件的类别徽标顺序不能因为字典哈希顺序
    变化而抖动，否则每次刷新看起来都像"变了"。
    """
    if not isinstance(cls, dict):
        return []
    out = []
    for k in CATEGORIES:
        try:
            n = int(cls.get(k) or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            out.append(k)
    return out


# ---------------------------------------------------------------- 定位

MISSING_TEXT = {
    "camera_missing": "没有这个摄像头（可能已被删除或改名）",
    "no_recording": "该日没有录像（未开启全时录像，或已被保留策略清理）",
    "no_segment_before": "事件发生前没有可用的录像分片",
    # "已删除"和"那段时间根本没录上"用文件清单区分不出来（两种都是"没有这一片"），
    # 所以措辞把两种都说进去，而不是赌一个。
    "segment_missing": "事件所在分片不在了（可能已被清理，或那段时间没录上）",
    "not_sealed": "事件所在分片还在写入（尚未封口），稍后再看",
    "empty_segment": "事件所在分片是空文件（录制被中断）",
    "corrupt_segment": "事件所在分片结构不完整（缺少索引），无法播放",
}


def missing(reason, **extra):
    """不可播放的标准化回答：机器读 missing_reason，人读 missing_text。

    只给 code 不够 —— 前端要么自己维护一份翻译表（两份真相，必然漂移），
    要么把 code 直接甩给用户（"segment_missing" 谁看得懂）。
    """
    out = {"segment": None, "offset_sec": None,
           "missing_reason": reason,
           "missing_text": MISSING_TEXT.get(reason, "录像不可用")}
    out.update(extra)
    return out


def seg_name(start_sec):
    """分片起点（钟点秒）→ 文件名 `HH-MM-SS.mp4`。全系统只有这一处把时间翻成
    文件名 —— 分片名用短横线（ledger.src_start 的解析口径），事件 id 用冒号
    去掉的形式，两者别混。"""
    return hhmmss(start_sec).replace(":", "-") + ".mp4"


def segment_of(sec, starts):
    """覆盖这一秒的分片起点：≤ sec 的最近一个；早于当天第一片返回 None。

    用「最近一个 ≤ 目标」而不是"区间包含"：分片真实时长要靠 mtime 才知道，
    而定位只需要回答「要播这一秒，该打开哪个文件」，seek 交给播放器。
    """
    t = _num(sec, -1.0)
    best = None
    for s in starts or ():
        v = _num(s, -1.0)
        if 0.0 <= v <= t and (best is None or v > best):
            best = v
    return best


def locate(start, starts, pre=None):
    """事件起点 → {segment, offset_sec}；定位不到给 missing_reason。

    starts 是当天**已封口**的分片起点列表。未封口/已删除/空文件这些判断
    要读盘，放在调用方（它们才是知道磁盘状态的那一层），这里只回答
    "按时间该打开哪一片、偏移多少"。
    """
    return resolve(start, starts, None, pre)


def resolve(start, sealed, parts=None, pre=None):
    """事件起点 → 可播定位，或一条说得清原因的 missing。

    判定顺序（顺序本身是经验，别随手换）：

      1. **事件起点落在哪一片**，以它为准。这一片还是 `.part`（没有 moov、
         `<video>` 打不开）就如实说"还没封口"，并告诉界面写到哪一片了。
         为什么要先定这一片：只按"最近一个已封口分片"定位，事件落在正在写
         的那一片时会被算到**上一片的末尾** —— 播放器打开后立刻到片尾，
         用户看到的就是"点进去播一秒就停"，跟"录像坏了"一模一样。
      2. 已封口，就在**这一片内部**退 pre 秒当入点；退到片头之前就夹到片头，
         并标 `clamped`。为什么不做"跨片前摇"（打开上一片、从它末尾 5 秒处
         开播）：单个 `<video>` 播到片尾就停，用户得先看 5 秒再手动续下一片，
         而"事件本身"反而没播到。宁可少看最多 10 秒前摇，也要保证点开就能
         连续看到事件发生。
    """
    pre = PRE_SEC if pre is None else float(pre)
    base = max(0.0, _num(start))

    seg_start = segment_of(base, list(sealed or ()) + list(parts or ()))
    if seg_start is None:
        return missing("no_segment_before")
    p_seg = segment_of(base, parts)
    if p_seg is not None and p_seg == seg_start:
        return missing("not_sealed", pending_segment=seg_name(p_seg),
                       at=round(base, 2))

    t = base - pre
    clamped = t < seg_start
    return {"segment": seg_name(seg_start),
            "offset_sec": round(max(0.0, t - seg_start), 2),
            "pre_sec": round(max(0.0, base - max(t, seg_start)), 2),
            "clamped": bool(clamped),
            "missing_reason": None, "missing_text": ""}


# ---------------------------------------------------------------- 台账 → 事件

def from_ledger(cam, date, data, now_sec=None, starts=None):
    """一份台账 → 当天这一路的事件列表。

    合并口径不在这里重写：直接吃 `ledger.events_of`（AI 段吸收运动簇定边界、
    两档 gap、cls 取 max 都在那一处）。这条界线很重要 —— 事件**边界**是判定
    引擎的经验（真机喂出来的），事件**身份**是产品语义；混在一起改一次就得
    重验两件事，而重验边界只能靠真机盯一晚上。

    now_sec 只有"今天"才该传：历史日期没有"还在发生"这回事，传了会把昨天
    下午最后一条事件永远显示成进行中。
    """
    out = []
    for w in ledger.events_of(data):
        try:
            s, e = float(w["s"]), float(w["e"])
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        eid = event_id(cam, date, s)
        if not eid:
            continue
        cls = w.get("cls") if isinstance(w.get("cls"), dict) else {}
        try:
            mv = int(w.get("mv"))
        except (TypeError, ValueError):
            mv = -1
        seg = segment_of(s, starts) if starts else None
        out.append({
            "id": eid,
            "cam": str(cam),
            "date": str(date),
            "start": round(s, 2),
            "end": round(e, 2),
            "start_at": iso_at(date, s),
            "end_at": iso_at(date, e),
            "duration": round(e - s, 2),
            "categories": categories_of(cls),
            "cls": {str(k): int(v) for k, v in cls.items()
                    if isinstance(v, (int, float))},
            "density": round(_num(w.get("d"), 0.0), 3),
            # mv=-1 是"这一路没有帧差源，无从判断"，绝不能写成 False（=没有运动）。
            # 评审原话：失败不等于没有。
            "motion_confirmed": None if mv < 0 else bool(mv),
            # 台账只记类别计数，不记置信度。这里如实留 None：编一个 0.5 之类
            # 的估值，界面上就会显示一个没人算过的数字（"看起来对"最危险）。
            "confidence_max": None,
            "state": "active" if _is_active(e, now_sec) else "ended",
            # 事件起点落在哪一片先算出来存着：列表上就能标"这段已清理"，
            # 不必等用户点进去才发现播不了。口径与 resolve() 一致（都按
            # 事件起点所在的片），否则列表标一片、点进去打开另一片 ——
            # 那是最容易被当成 bug 的那种不一致。
            "source_segment": seg_name(seg) if seg is not None else None,
        })
    return out


def _is_active(end_sec, now_sec):
    if now_sec is None:
        return False
    return float(end_sec) >= _num(now_sec) - LIVE_TAIL_SEC


# ---------------------------------------------------------------- 合并（幂等）

def merge_one(old, new):
    """同一条事件的新观察并入旧记录 → (事件, 是否有变化)。

    三条口径：
      - 起点取更早、终点取更晚：重判可能把边界外推（运动簇定边界），事件也
        可能还在延长。id 只由起点推出，所以"变长"永远不会分裂成两条。
      - 类别计数按类别取 max：同一份判定结果被算两遍不该翻倍（与 ledger.merge
        的"重判不双算"同口径）。
      - 状态只许前进（见 next_state）。
    """
    a = dict(old or {})
    b = dict(new or {})
    out = dict(a)
    out["id"] = a.get("id") or b.get("id")
    out["cam"] = a.get("cam") or b.get("cam")
    date = a.get("date") or b.get("date")
    out["date"] = date

    s = min(_num(a.get("start")), _num(b.get("start")))
    e = max(_num(a.get("end")), _num(b.get("end")))
    out["start"], out["end"] = round(s, 2), round(e, 2)
    out["duration"] = round(e - s, 2)
    out["start_at"] = iso_at(date, s)
    out["end_at"] = iso_at(date, e)

    cls = {}
    for src in (a.get("cls"), b.get("cls")):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                cls[str(k)] = max(cls.get(str(k), 0), n)
    out["cls"] = cls
    out["categories"] = categories_of(cls)

    # 密度取 max：两边都是同一段画面的活动占比，重判/延长只会让它更接近真实；
    # 取平均会凭空造出一个谁都没算过的数。
    out["density"] = round(max(_num(a.get("density")),
                               _num(b.get("density"))), 3)

    # 有无帧差佐证：只要有一次拿到就算"有"。两边都是 None（无从判断）才保持
    # None —— 这正是"未知 ≠ 没有"。
    mc = [x for x in (a.get("motion_confirmed"), b.get("motion_confirmed"))
          if x is not None]
    out["motion_confirmed"] = True if any(mc) else (False if mc else None)

    conf = [x for x in (a.get("confidence_max"), b.get("confidence_max"))
            if x is not None]
    out["confidence_max"] = max(conf) if conf else None

    out["state"] = next_state(a.get("state"), b.get("state"))
    out["source_segment"] = a.get("source_segment") or b.get("source_segment")
    return out, out != a


def merge(existing, fresh):
    """按 id 幂等合并两批事件 → (列表, 是否变化)。

    幂等是硬要求：`/api/events` 每刷新一次就可能同步一天台账，同一份输入
    会被反复喂进来。连续跑两次，第二次必须 `changed=False` —— 否则索引
    永远"有新事件"，前端轮询永远在重绘，磁盘也白写。
    """
    by_id = {}
    for e in existing or []:
        if isinstance(e, dict) and e.get("id"):
            by_id[e["id"]] = e
    changed = False
    for n in fresh or []:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        if n["id"] in by_id:
            m, ch = merge_one(by_id[n["id"]], n)
            by_id[n["id"]] = m
            changed = changed or ch
        else:
            by_id[n["id"]] = dict(n)
            changed = True
    out = sorted(by_id.values(),
                 key=lambda e: (_num(e.get("start")), str(e.get("cam"))))
    return out, changed


def summary_of(events):
    """列表头一行「N 条 · 人员 x / 车辆 y」的口径。

    计数按类别列表算而不是按 cls 计数求和：一个人出现 5 帧是一条事件，
    不是 5 个人。cls 是判定用的原始计数，界面上给用户看的必须是事件条数。
    """
    cats = {k: 0 for k in CATEGORIES}
    for e in events or ():
        for k in e.get("categories") or ():
            if k in cats:
                cats[k] += 1
    return {"count": len(events or ()), "by_category": cats}
