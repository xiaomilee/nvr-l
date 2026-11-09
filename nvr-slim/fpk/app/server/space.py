#!/usr/bin/env python3
"""存储空间预警（0.9.131）。

为什么单独一层
--------------
保留策略（`retention.py`）回答"按天删什么"，清理预览回答"今晚会删哪些目录"，
两者都不回答**这块盘还撑得住吗**。盘写满那天 ffmpeg 报 ENOSPC、录制停摆，
而页面上只有"剩余 7.6 GB / 保留 3 天"三个数字，要用户自己心算 —— 没有人
会天天算。评审基线把「空间预警」列在 P0「存储管理」，正是这个理由。

为什么不能只算「剩余 ÷ 每天写入」
----------------------------------
测试机就是反例：55 GB 盘、剩 7.6 GB、每天写约 12 GB、保留 3 天。
`free / rate` = 0.6 天，可它已经稳定跑了几周 —— 每天 03:00 整目录删掉最旧的
一天，写入与删除基本相等。倒计时会天天喊"明天就满"，喊几次就没人信了。

所以这里算的是**稳态**：

    rate[kind] = 该层最近 7 个完整日的中位数（丢掉 0 字节的日子：
                 那天没录，不代表速率是 0）
    need       = Σ rate[kind] × 该层保留天数     （全时按 full_days，
                 归档按 archive_days —— 归档只装活动片段，体积远小于全时，
                 用全时的速率去乘归档天数会把需求放大好几倍）
    other      = 总容量 − 剩余 − 录像实测占用     （同一块盘上的其它数据）
    headroom   = 总容量 − other − need            （稳态余量）

`headroom < 0` 是"不管清理跑不跑都会满"；`headroom ≥ 0` 但很小是"再满一点就危险"；
`剩余 < 阈值` 是"现在就该处理"。三者给的建议不同，混成一句红字等于没说。

三条口径（改之前先看这三条）
----------------------------
1. **只读元数据**：只认目录名与大小，不解码、不读内容、不往录像目录写东西。
2. **算事实，不算猜测**：今天没走完就按已过时长外推，并**标明依据**；一个完整日
   都没有时回 `unknown`，绝不拿默认值编一个"还能录 30 天"。
3. **只报告，不处置**：自动删录像等于让"空间紧张"改写"事实数据"，那是红线。
   要删由用户在清理预览里确认。

本模块不 import nvr_slim，只借用 `retention` 的日期/时刻解析（同一处口径），
单测可以直接喂假数据。
"""
import math
import time
from datetime import date, datetime

import retention

GB = 1024.0 ** 3

DEFAULT_WARN_GB = 20          # 剩余低于这个值就预警；0 = 关闭
WARN_LIMITS = (0, 2000)       # 允许 0（关闭）到 2000 GB（大监控盘也够用）
RATE_DAYS = 7                 # 速率只看最近 7 个完整日
TODAY_MIN_SEC = 3600.0        # 今天至少跑了 1 小时才拿它外推
LOW_RATIO = 0.10              # 大盘兜底：剩余不足 10% 也算低
CRIT_RATIO = 0.03             # 剩余不足 3% 算告急
HEADROOM_RATIO = 0.05         # 稳态余量不足总量 5% 提醒
HEADROOM_MIN = 5 * GB         # 但至少给 5 GB 余量，小盘不能只按比例
CLEANUP_LATE_H = 2.0          # 清理过了时刻 2 小时还没跑 → 提醒
CLEANUP_VERY_LATE_H = 24.0    # 超过一天没跑 → 告急

KINDS = ("full", "archive", "clip")
KIND_LABEL = {"full": "全时", "archive": "归档", "clip": "缩时"}

_RANK = {"ok": 0, "off": 0, "unknown": 0, "warn": 1, "critical": 2}


def clamp_warn_gb(value):
    """阈值强校：只收整数，夹在 0~2000；非法值回默认（不是 0，别让脏值关掉预警）。"""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_WARN_GB
    lo, hi = WARN_LIMITS
    return max(lo, min(hi, n))


def gb(n, digits=1):
    """字节 → GB，保留一位。None/坏值回 None（前端显示 "—" 而不是 0）。"""
    try:
        return round(float(n) / GB, digits)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.0


def keep_map(value):
    """保留天数：调用方给 {kind: days} 或单个数（只算全时）。缺的层按 0 处理。"""
    if isinstance(value, dict):
        out = {}
        for k in KINDS:
            try:
                out[k] = max(0, int(value.get(k) or 0))
            except (TypeError, ValueError):
                out[k] = 0
        return out
    try:
        n = max(0, int(value or 0))
    except (TypeError, ValueError):
        n = 0
    return {"full": n, "archive": 0, "clip": 0}


def daily_rate(day_bytes, today=None, now=None):
    """{(日期 'YYYY-MM-DD'): 字节} → (每天字节, 依据天数, 依据说明)。

    三处刻意的取舍：

    - **只用完整日**（日期 < 今天）。今天只走了一半，直接算会把速率低估一半。
    - **丢 0 字节的日子**：那天可能整路没录（断电/停机），拿它进样本会把
      "每天 12 GB" 拉成 "每天 3 GB"，预警从此不响。
    - **取中位数不取平均**：补录、换盘、大扫除式的单日异常不该改变长期速率。

    一个完整日都没有时才退到"今天外推"，并在 `basis` 里标 0 天 + 说明，
    让界面如实写"今天已跑 6.2 小时"，而不是假装有 7 天样本。
    """
    today = today or date.today()
    now = float(now if now is not None else time.time())
    rows = []
    for k, v in (day_bytes or {}).items():
        d = retention.parse_day(k) if isinstance(k, str) else k
        try:
            b = int(v)
        except (TypeError, ValueError):
            continue
        if d is None or b <= 0:
            continue
        rows.append((d, b))
    rows.sort()
    done = [b for d, b in rows if d < today][-RATE_DAYS:]
    if done:
        vals = sorted(done)
        n = len(vals)
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
        return float(med), n, "最近 %d 个完整日" % n
    today_b = [b for d, b in rows if d == today]
    if today_b:
        b = max(today_b)
        midnight = datetime.combine(today, datetime.min.time()).timestamp()
        elapsed = max(0.0, now - midnight)
        if b > 0 and elapsed >= TODAY_MIN_SEC:
            return b * 86400.0 / elapsed, 0, "今天已跑 %.1f 小时" % (elapsed / 3600.0)
    return 0.0, 0, ""


def rates_of(days_by_kind, today=None, now=None):
    """({kind: 日速率}, 依据天数, 依据说明)。依据说明取样本最多的那一层。"""
    out = {}
    basis, label = 0, ""
    for k in KINDS:
        r, n, lb = daily_rate((days_by_kind or {}).get(k) or {}, today, now)
        out[k] = r
        if n > basis or (not label and lb):
            basis, label = n, lb
    return out, basis, label


def _reason(code, level, text):
    return {"code": code, "level": level, "text": text}


def evaluate(free_b, total_b, used_b, days_by_kind, keep_days, warn_gb=None,
             cleanup_overdue_h=None, cleanup_at="03:00", today=None, now=None):
    """算一份预警结论。返回值就是接口契约，字段名不许随手改。

    参数里的 `cleanup_overdue_h` 由调用方算（它才知道 cleanup-state 与时刻）：
    传 None = 不知道/还没到点；传数字 = 已经晚了这么多小时。
    """
    now = float(now if now is not None else time.time())
    warn_gb = clamp_warn_gb(warn_gb if warn_gb is not None else DEFAULT_WARN_GB)
    free_b, total_b, used_b = _num(free_b), _num(total_b), _num(used_b)
    rates, basis, basis_label = rates_of(days_by_kind, today=today, now=now)
    keep = keep_map(keep_days)
    rate = sum(rates.values())
    # 各层分开乘：归档只装活动片段，用全时的速率乘归档天数会放大好几倍
    need = sum(rates[k] * keep[k] for k in KINDS)
    # 非录像占用：总容量里既不是剩余、也不是我们能统计到的录像，那就是别的东西
    # （系统、桌面、别的应用）。算不出总体（statvfs 失败）时留 None，不瞎减。
    other = max(0.0, total_b - free_b - used_b) if total_b > 0 else None
    headroom = (total_b - other - need) if other is not None else None
    out = {
        "level": "ok",
        "free_gb": gb(free_b), "total_gb": gb(total_b), "used_gb": gb(used_b),
        "other_gb": gb(other) if other is not None else None,
        "rate_gb": gb(rate), "rate_basis": basis, "rate_label": basis_label,
        "rates_gb": dict((k, gb(rates[k])) for k in KINDS),
        "keep_days": keep["full"],
        "keep": keep,
        "keep_text": _keep_text(keep, rates),
        "need_gb": gb(need),
        "headroom_gb": gb(headroom) if headroom is not None else None,
        "warn_gb": warn_gb,
        # "不做清理最多还能写几天"：只作为参考数字给出来，不当结论 ——
        # 按天整目录删的策略下，它几乎总是远小于真实可运行时间。
        "free_days": (round(free_b / rate, 1) if rate > 0 else None),
        # 容量上限（按实测最多能留几天）。键始终在，值可能为 None ——
        # 前端按 `!= null` 判断，少一个键就会变成 undefined。
        "suggest_days": None,
        "reasons": [], "advice": "", "at": int(now),
    }
    if total_b <= 0:
        # 容量都读不到：这时候说"空间正常"是撒谎，说"告急"也是撒谎。
        out["level"] = "unknown"
        out["reasons"] = [_reason("no_disk", "unknown",
                                  "读不到磁盘容量（录像目录不可访问？），暂不判断空间")]
        return out
    if warn_gb == 0:
        out["level"] = "off"
        return out

    free_gb, total_gb = free_b / GB, total_b / GB
    warn_line = max(float(warn_gb), total_gb * LOW_RATIO) * GB
    crit_line = max(warn_gb * 0.5, total_gb * CRIT_RATIO) * GB
    # "这块盘按实测最多留几天"：只有知道速率才算得出，算不出就留空，
    # 不给一个凭默认值编出来的天数。注意它是**容量上限**，不是"还能录几天"。
    if rate > 0:
        room = max(0.0, total_b - (other if other is not None else 0.0))
        # 上限 3650 天：刚装好的盘、每天只写几 MB 时会算出五位数，
        # 那个数字进了文案只会让人以为程序坏了。
        out["suggest_days"] = min(3650, max(1, int(math.floor(room / rate))))
    reasons = []

    # ① 稳态装不下：这是唯一"无论清理跑不跑都会满"的情形，先说它。
    if headroom is not None and rate > 0 and headroom < 0:
        reasons.append(_reason(
            "capacity", "critical",
            "按实测每天 %s GB、保留 %s 需要 %s GB，加上非录像占用 %s GB，"
            "已超出这块盘的 %.1f GB —— 清理也追不上写入，最旧的录像会被一路挤掉"
            % (out["rate_gb"], out["keep_text"], out["need_gb"],
               out["other_gb"], total_gb)))
    elif headroom is not None and rate > 0 \
            and headroom < max(HEADROOM_MIN, total_b * HEADROOM_RATIO):
        reasons.append(_reason(
            "headroom", "warn",
            "稳态余量只剩 %s GB（每天写 %s GB × 保留 %s，加非录像占用 %s GB）"
            % (gb(headroom), out["rate_gb"], out["keep_text"], out["other_gb"])))

    # ② 现在就很低：这条与写入速率无关，statvfs 一句话就够。
    if free_b < crit_line:
        reasons.append(_reason(
            "free_critical", "critical",
            "只剩 %.1f GB（总量 %.1f GB），低于告急线 %.1f GB"
            % (free_gb, total_gb, crit_line / GB)))
    elif free_b < warn_line:
        reasons.append(_reason(
            "free_low", "warn",
            "只剩 %.1f GB，低于预警阈值 %.1f GB" % (free_gb, warn_line / GB)))

    # ③ 清理没按计划跑：空间不会自己回来，这比"剩得少"更该有人管。
    late = _num(cleanup_overdue_h)
    if late > CLEANUP_LATE_H:
        lvl = "critical" if late >= CLEANUP_VERY_LATE_H else "warn"
        reasons.append(_reason(
            "cleanup_stale", lvl,
            "每天 %s 的清理已经晚了 %.1f 小时还没跑（空间不会自己回来）"
            % (retention.parse_hhmm(cleanup_at), late)))

    if not reasons:
        if rate <= 0:
            out["level"] = "unknown"
            out["reasons"] = [_reason(
                "no_rate", "unknown",
                "还没有完整一天的写入数据（今天也不足 1 小时），暂不判断还能录几天")]
            return out
        out["level"] = "ok"
        return out

    level = "ok"
    for r in reasons:
        if _RANK.get(r["level"], 0) > _RANK.get(level, 0):
            level = r["level"]
    out["level"] = level
    out["reasons"] = reasons
    out["advice"] = _advice_of(reasons, out, cleanup_at)
    return out


def _keep_text(keep, rates=None):
    """"3 天" / "全时 3 天、归档 7 天"：只说真正在写的那几层。

    归档开但一天都没裁出东西（rates["archive"] = 0）时不写"归档 7 天" ——
    那会让"需要 36 GB"看起来像算错了（7 天怎么才 36 GB）。速率全为 0 时
    （还没数据）退回按配置写，别给一句空话。
    """
    kinds = [k for k in KINDS if keep.get(k)]
    if rates and sum(rates.values()) > 0:
        kinds = [k for k in kinds if rates.get(k, 0) > 0] or kinds
    parts = ["%s %d 天" % (KIND_LABEL[k], keep[k]) for k in kinds]
    if not parts:
        return "0 天"
    return parts[0] if len(parts) == 1 else "、".join(parts)


def _advice_of(reasons, out, cleanup_at):
    """建议只写一条，选最该先做的那件 —— 列五条建议等于让用户自己排序。"""
    codes = set(r["code"] for r in reasons)
    cap = out.get("suggest_days")
    if "capacity" in codes and cap:
        return ("把保留天数从 %d 天降到 %d 天以内，或先删掉一部分旧录像"
                "（「保留策略」里能预览要删哪些）"
                % (out["keep_days"], cap))
    if "cleanup_stale" in codes:
        return ("看一下诊断日志里夜间任务有没有被归档/压缩占住；"
                "也可以在「保留策略」里手动执行一次清理")
    if codes & {"free_critical", "free_low", "headroom"}:
        # 容量上限一并给出来：用户下一个问题必然是"那到底能留几天"。
        # 即便上限正好等于现在的配置也要说 —— "装得下但仍紧"是有效信息。
        tail = ("这块盘按实测最多留 %d 天，现在配的是 %d 天"
                % (cap, out["keep_days"])) if cap else ""
        return ("在「保留策略」里预览并清理过期目录；清理时刻是每天 %s，"
                "提前清一次能立刻腾出空间%s"
                % (retention.parse_hhmm(cleanup_at), ("。" + tail) if tail else ""))
    return ""


def text_of(payload):
    """一行日志文案：预警升级/恢复时写进 nvr.log，不重复刷屏。"""
    p = payload or {}
    level = p.get("level") or "ok"
    head = {"critical": "空间告急", "warn": "空间偏低", "unknown": "空间状态未知",
            "off": "空间预警已关闭", "ok": "空间正常"}[level]
    bits = ["剩余 %s GB / %s GB" % (p.get("free_gb"), p.get("total_gb"))]
    if p.get("rate_gb"):
        bits.append("每天约 %s GB（%s）" % (p.get("rate_gb"),
                                        p.get("rate_label") or "实测"))
    if p.get("need_gb") is not None:
        bits.append("保留 %s 需 %s GB" % (p.get("keep_text") or "—",
                                       p.get("need_gb")))
    if p.get("headroom_gb") is not None:
        bits.append("稳态余量 %s GB" % p.get("headroom_gb"))
    line = "%s：%s" % (head, "，".join(bits))
    why = [r.get("text") for r in (p.get("reasons") or []) if r.get("text")]
    if why:
        line += "；" + "；".join(why)
    if p.get("advice"):
        line += "。建议：" + p["advice"]
    return line
