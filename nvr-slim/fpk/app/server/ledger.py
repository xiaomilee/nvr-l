#!/usr/bin/env python3
"""活动台账：按「摄像头目录 / 日期」落一份 JSON，记录全天活动窗口。

这是 Aqara 式「标记与视频解耦」的地基：缩时不再（虚拟模式下）产出视频文件，
判定引擎的结论改记到台账里——时刻 + 时长 + 密度，每天每路几 KB，永久保留。
回放页时间轴的橙标记、事件列表、模拟缩时跳播，吃的是同一份台账。

为什么按分片存储、读出时再合并（而不是写入时就合并成全天窗口）：
  同一个分片可能因参数指纹变化被重判（改忽略区、改灵敏度）。写入侧只做
  「同 src 整体替换」，合并逻辑只在读出时做一次 —— 全天只有一个合并口径，
  不存在"写入合并了一半、下次重判又拆不开"的尴尬。一天最多 288 个分片、
  几百个窗口，读时合并的开销可以忽略。

窗口坐标：当天 0 点起的浮点秒（精度 ≤0.5s，两位小数）。检测源的物理精度
就是 0.5~1 秒（像素差 2fps 采样 / 码流曲线按秒聚合），毫秒是假精度；而
跳播落点受关键帧对齐（±1 GOP）限制，由 pre_buf 吸收，也轮不到毫秒出场。
"""
import json
import os
import re
import time

# 与 params.ANALYSIS_DEFAULTS["merge_gap"] 对齐：两段活动间隔不超过它就算同一事件
GAP_SEC = 5.0
DAY_SEC = 86400.0
AI_SRC_PREFIX = "ai:"

# ---------------------------------------------------------------- 读端三档时间尺度（0.9.69）
# 台账里同时躺着两条管线的窗口：帧差（源名 = 分片名）与 AI（源名 = "ai:分片名"）。
# 它们的「断续」长得完全不一样，用同一个 gap 合并必然一边错：
#
#   AI_GAP_SEC      AI 在场判定。抽帧每 5 秒一次、窗口前后各留 1 秒，正常间隙 3 秒；
#                   抓帧超时（10 秒）会漏掉一两帧，间隙拉到 10~20 秒。30 秒以内一律
#                   算「同一个人/车还在画面里」——一辆车停着不再裂成几十条 2 秒事件。
#                   没有封顶：真停了 10 分钟，那 10 分钟就是他/它待在画面里的时长。
#   MOTION_GAP_SEC  运动簇。帧差只在画面真变化时出窗口，人走到一半站住不动会断几秒，
#                   60 秒内的断续算同一段动静（车来了到车走了）。
#   MOTION_EDGE_SEC 运动簇与 AI 段之间允许的最大距离。车从画面边缘驶入到 YOLO 认出
#                   车型往往差十几秒，超过这个距离就不能算同一次进出。
AI_GAP_SEC = 30.0
MOTION_GAP_SEC = 60.0
MOTION_EDGE_SEC = 60.0


def _round2(v):
    return round(float(v) + 0.0, 2)


def valid_date(s):
    return isinstance(s, str) and len(s) == 10 and s[4] == "-" and s[7] == "-"


def ledger_path(root, dname, date):
    return os.path.join(root, "_ledger", dname, "%s.json" % date)


def load(root, dname, date):
    """读一份台账。不存在/损坏返回 None —— 损坏不抛错，宁可当日无标记。"""
    if not (valid_date(date) and dname and str(dname) not in ("", ".", "..")):
        return None
    try:
        with open(ledger_path(root, dname, date), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def src_start(name):
    """分片文件名（HH-MM-SS.mp4，切片产物 HH-MM-SS.cNN.mp4）→ 当天钟点秒。

    台账窗口 = 分片起点 + 判定窗口偏移。文件名是唯一可靠的钟点来源：
    文件 mtime 会被尾部写入、复制、备份搅乱，不能用作时间基准。
    """
    m = re.match(r"^(\d{2})-(\d{2})-(\d{2})", os.path.basename(name or ""))
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    if h > 23 or mi > 59 or s > 59:
        return None
    return h * 3600 + mi * 60 + s


def union(windows, gap=GAP_SEC):
    """窗口合并：排序后重叠取并集，gap 秒内的短间隙并入。

    跨分片边界的事件（人 5:59:58 走进画面、6:00:02 离开）必须连成一个窗口，
    否则事件列表里会出现两个"半截事件"，跳播也会在中间无故停一拍。
    """
    ws = []
    for w in (windows or []):
        try:
            s, e = float(w[0]), float(w[1])
        except (TypeError, ValueError, IndexError):
            continue
        if e > s:
            ws.append((s, e))
    ws.sort()
    out = []
    for s, e in ws:
        if out and s - out[-1][1] <= max(0.0, float(gap)):
            if e > out[-1][1]:
                out[-1][1] = e
        else:
            out.append([s, e])
    return [[_round2(s), _round2(e)] for s, e in out]


def merge(root, dname, date, src_name, start_sec, windows, density=0.0, engine=None):
    """一个分片的判定结论并入台账（原子写）。

    windows 是分片内秒，这里负责换算成当天钟点秒并夹进 [0, 86400]。
    判定失败不能调这个函数——失败必须走调用方的失败链路，绝不写空结论。

    windows 每项 [s, e] 或 [s, e, cls]：cls 是 AI 检测类别计数
    {"person": n, "two_wheeler": n, "vehicle": n}（0.9.60 起，帧差窗口不带）。
    """
    if not (valid_date(date) and dname and str(dname) not in ("", ".", "..")):
        return None
    try:
        start = float(start_sec)
    except (TypeError, ValueError):
        return None
    wins = []
    for w in (windows or []):
        try:
            s = max(0.0, min(DAY_SEC, start + float(w[0])))
            e = max(0.0, min(DAY_SEC, start + float(w[1])))
        except (TypeError, ValueError, IndexError):
            continue
        if e <= s:
            continue
        entry = [_round2(s), _round2(e)]
        if len(w) > 2 and isinstance(w[2], dict) and w[2]:
            entry.append({str(k): int(v) for k, v in w[2].items()
                          if str(k) and isinstance(v, (int, float))})
        wins.append(entry)
    try:
        d = max(0.0, min(1.0, float(density or 0)))
    except (TypeError, ValueError):
        d = 0.0
    path = ledger_path(root, dname, date)
    data = load(root, dname, date)
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("sources"), dict):
        data = {"v": 1, "cam": str(dname), "date": date, "sources": {}}
    data["sources"][str(src_name)] = {"start": _round2(start),
                                      "windows": wins, "d": round(d, 3)}
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if engine:
        data["engine"] = str(engine)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return data


def events_of(data, gap=None):
    """台账 → 全天事件列表 [{"s","e","d","cls","mv"}]。

    两件事一起做（0.9.69）：

    一、按源分档合并。AI 窗口用 AI_GAP_SEC（30 秒）合并成「在场段」——车停在那里
    就不再是几十条 2 秒事件；帧差窗口用 MOTION_GAP_SEC（60 秒）合并成「运动簇」。
    gap 传了具体数字时两档都用它（单测/特殊调用需要单一口径）。

    二、用帧差给 AI 段定边界（移动侦测作为人员/车辆检测的辅助判断条件）。
    车从画面边缘进来时 YOLO 还没认出车型，认出时车已经到画面中间——AI 段天然
    比真实在场短。所以每个 AI 段向两侧吸收 MOTION_EDGE_SEC 以内的运动簇：
    段起点前推到这个簇的起点（arrive），段终点后延到簇的终点（leave），
    输出的 s/e 就是真实进出时刻。没有帧差管线时边界不动，mv=-1 如实标注「无从判断」。

    mv：1 = 有帧差佐证（边界来自运动）· 0 = 无帧差佐证（AI 单独判定，可能误检）
        -1 = 这条台账根本没有帧差源（纯 AI 或旧数据）

    密度 d 取"对这个窗口有贡献的分片"的 active_ratio 加权均值：一个窗口横跨两个
    分片时，比单看任何一片都准。没贡献来源（理论不可能，防御一下）就给 0。

    cls：本窗口命中的 AI 检测类别计数，取贡献源各类别 max（重判不双算）。
    空 dict = 纯帧差窗口/旧数据，消费侧按「未分类」处理。
    """
    if not isinstance(data, dict):
        return []
    srcs = data.get("sources")
    if not isinstance(srcs, dict):
        return []
    ai_gap = AI_GAP_SEC if gap is None else max(0.0, float(gap))
    mv_gap = MOTION_GAP_SEC if gap is None else max(0.0, float(gap))
    pieces = []                     # (s, e, d, cls)
    ai_iv, mv_iv = [], []
    has_mv = False
    for name, v in srcs.items():
        if not isinstance(v, dict):
            continue
        is_ai = str(name).startswith(AI_SRC_PREFIX)
        if not is_ai:
            has_mv = True
        try:
            pd = max(0.0, min(1.0, float(v.get("d") or 0)))
        except (TypeError, ValueError):
            pd = 0.0
        for w in (v.get("windows") or []):
            try:
                s, e = float(w[0]), float(w[1])
            except (TypeError, ValueError, IndexError):
                continue
            if e <= s:
                continue
            cls = {}
            if len(w) > 2 and isinstance(w[2], dict):
                cls = {str(k): int(x) for k, x in w[2].items()
                       if isinstance(x, (int, float)) and int(x) > 0}
            pieces.append((s, e, pd, cls))
            (ai_iv if is_ai else mv_iv).append((s, e))
    ai_sess = union(ai_iv, ai_gap)
    mv_cl = union(mv_iv, mv_gap)
    # 事件骨架：[起, 止, mv]。AI 段先按运动簇向外定边界；没被任何 AI 段收编的
    # 运动簇单独成一条纯移动侦测事件。
    boxes = []
    for s, e in ai_sess:
        ns, ne, mv = s, e, (0 if has_mv else -1)
        for ms, me in mv_cl:
            if me < s - MOTION_EDGE_SEC or ms > e + MOTION_EDGE_SEC:
                continue
            ns, ne, mv = min(ns, ms), max(ne, me), 1
        boxes.append([ns, ne, mv])
    for ms, me in mv_cl:
        near = any(not (me < s - MOTION_EDGE_SEC or ms > e + MOTION_EDGE_SEC)
                   for s, e in ai_sess)
        if not near:
            boxes.append([ms, me, 1])
    boxes.sort()
    # 边界外推后两条 AI 段可能撞在一起（被同一个运动簇拉长）：合并成一条，cls 取 max。
    merged = []
    for s, e, mv in boxes:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] = max(merged[-1][2], mv)
        else:
            merged.append([s, e, mv])
    out = []
    for s, e, mv in merged:
        num = den = 0.0
        cls = {}
        for ps, pe, pd, pc in pieces:
            ov = min(e, pe) - max(s, ps)
            if ov <= 0:
                continue
            num += pd * ov
            den += ov
            for k, v in pc.items():
                cls[k] = max(cls.get(k, 0), int(v))
        out.append({"s": _round2(s), "e": _round2(e),
                    "d": round(num / den, 3) if den else 0.0,
                    "cls": cls, "mv": int(mv)})
    return out


def summary_of(data):
    """全天摘要：事件数、活动总秒数。头部一行「N 个活动 · 累计 mm:ss」直接吃它。"""
    ev = events_of(data)
    return {"count": len(ev),
            "active_sec": round(sum(e["e"] - e["s"] for e in ev), 2)}


def day_sec_now(now=None):
    """此刻的「当天钟点秒」。服务端时基，台账/可用性一律以它为准。

    前端时间轴红线用的是浏览器 Date（客户端时钟），与 NAS 时钟可能差几分钟；
    判断「这段是不是已经录到了」必须用**录像文件的主人**的时间，
    否则会出现「手机上 02:28、蓝块画到 02:31」这种自相矛盾的画面。
    """
    t = time.localtime(now)
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec


def seg_typ_of(starts, seg, min_count=4):
    """这一路**实际**的分片长度（秒）：用相邻起点的间隔中位数推断。

    为什么不能直接用配置值（0.9.72 真机踩到）：
      - 分片长度是**每路**可覆盖的（院子 R 60 秒、车库 120 秒、其余跟随全局 300），
        而入参只有一个全局 record_segment_sec，用它给 60 秒那一路兜底，
        末片会被画到 5 倍长（真机实测：院子 R 多画 4 分钟）；
      - 摄像头 GOP 不整除分片长度时实际会略微超出（车库配 120、实测中位 132），
        用配置值算"3 个周期的断录容差"会偏紧。
    文件名就是分片起点，所以 `起点[i+1] - 起点[i]` 就是第 i 片的真实时长 ——
    这是唯一不依赖配置的信息源。片数太少（< min_count）时中位数不可信
    （两片之间恰好断过录就会把断录间隔当成片长），退回配置值。
    """
    ss = sorted(set(float(s) for s in starts))
    if len(ss) < max(2, int(min_count)):
        return max(1.0, float(seg))
    gaps = [b - a for a, b in zip(ss, ss[1:]) if b > a]
    if not gaps:
        return max(1.0, float(seg))
    gaps.sort()
    med = gaps[len(gaps) // 2]
    # 中位数必须落在可信范围：太短（<5 秒，整分对齐产生的碎片）或太长
    # （> 10 倍配置值，说明全是断录间隔）都不认，退回配置值。
    if med < 5.0 or med > max(60.0, float(seg) * 10.0):
        return max(1.0, float(seg))
    return float(med)


def avail_spans(starts, seg, now_sec=None):
    """分片起点 → 画面可用区间 [[s, e], ...]（0.9.72 从 ledger_payload 抽出，便于单测）。

    规则：
      - **实际片长从起点间隔推断**（见 seg_typ_of），不信任配置的名义值；
      - 下一片紧跟着来（间隔 ≤ 3 个实际片长）→ 这一片录到的时刻就是下一片起点；
        3 个周期是给「改过分片长度」留的容差（600→300 时老片实际 600 秒，
        倍数正好 2），真断录（关机/故障，几十分钟起）才判成空洞；
      - 间隔更远 = 中间断录，只画到 st+实际片长，不许把空洞涂成有画面；
      - 末片没有下一片可参照，按实际片长兜底；
      - **传了 now_sec（当天）就一律夹紧到现在**：分片长度是名义值，录制中途停、
        或正在写的时候，按 st+seg 推算会把「还没录到」的时段画成有画面 ——
        真机反馈就是「真实时间还没到，回放页已经显示录制了视频」，用户点进去
        自然播不动（要么提示无录像、要么播 1 秒就收播）；
      - 起点本身已晚于现在（时间戳超前 = 时钟错乱）直接丢弃，不画一段假覆盖。
    """
    out = []
    ss = sorted(set(float(s) for s in starts))
    seg_t = seg_typ_of(ss, seg)
    far = seg_t * 3.0
    for i, st in enumerate(ss):
        if i + 1 < len(ss) and ss[i + 1] <= st + far:
            en = ss[i + 1]                  # 下一片紧跟着来 → 这一片确实录到了这里
        else:
            en = min(DAY_SEC, st + seg_t)   # 末片 / 断录之后：按实际片长兜底
        if now_sec is not None:
            en = min(en, float(now_sec))    # 名义长度 ≠ 已录长度，一律夹紧到现在
        if en <= st:
            continue
        if out and st <= out[-1][1]:
            out[-1][1] = max(out[-1][1], en)
        else:
            out.append([st, en])
    return out


def remove_cam(root, dname):
    """删摄像头时台账保留（历史是历史）——但目录被显式删除要能成功。此处只提供路径。"""
    return os.path.join(root, "_ledger", str(dname))
