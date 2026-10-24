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
        if e > s:
            wins.append([_round2(s), _round2(e)])
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


def events_of(data, gap=GAP_SEC):
    """台账 → 全天事件列表 [{"s","e","d"}]，窗口并集 + 密度按重叠时长加权。

    密度 d 取"对这个窗口有贡献的分片"的 active_ratio 加权均值：一个窗口
    横跨两个分片时，比单看任何一片都准。没贡献来源（理论不可能，防御一下）
    就给 0。
    """
    if not isinstance(data, dict):
        return []
    srcs = data.get("sources")
    if not isinstance(srcs, dict):
        return []
    pieces = []
    for v in srcs.values():
        if not isinstance(v, dict):
            continue
        try:
            pd = max(0.0, min(1.0, float(v.get("d") or 0)))
        except (TypeError, ValueError):
            pd = 0.0
        for w in (v.get("windows") or []):
            try:
                s, e = float(w[0]), float(w[1])
            except (TypeError, ValueError, IndexError):
                continue
            if e > s:
                pieces.append((s, e, pd))
    merged = union([(s, e) for s, e, _ in pieces], gap)
    out = []
    for s, e in merged:
        num = den = 0.0
        for ps, pe, pd in pieces:
            ov = min(e, pe) - max(s, ps)
            if ov > 0:
                num += pd * ov
                den += ov
        out.append({"s": s, "e": e, "d": round(num / den, 3) if den else 0.0})
    return out


def summary_of(data):
    """全天摘要：事件数、活动总秒数。头部一行「N 个活动 · 累计 mm:ss」直接吃它。"""
    ev = events_of(data)
    return {"count": len(ev),
            "active_sec": round(sum(e["e"] - e["s"] for e in ev), 2)}


def remove_cam(root, dname):
    """删摄像头时台账保留（历史是历史）——但目录被显式删除要能成功。此处只提供路径。"""
    return os.path.join(root, "_ledger", str(dname))
