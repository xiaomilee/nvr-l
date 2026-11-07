# -*- coding: utf-8 -*-
"""录像可信度（health）：缺口、覆盖、每路状态口径。

为什么单独一层：以前 `/api/status` 里的 `cams.recording` 数的是「配了 RTSP 且启用」
的路数 —— 真机院子 R 每 25 秒断一次流、客厅半夜卡过半小时，这个数字从头到尾都是 5。
用户想知道「昨天凌晨到底录上没录上」，只能点开录像列表一屏一屏数，还被断流之间
的碎片骗（长度按名义分片算，看着是满的）。

这里把三件事拆开，各给各的数字，不给「看起来对」的估值：
  上游有字节   ingest.idle_sec（多少秒没收到数据）
  分片在增长   channel.last_age_sec（最近一个分片文件多久没被写过）
  今天覆盖     avail_spans 的实际空洞（真实的录像缺口）

本模块只做纯计算：不读盘、不起进程、不看时钟（now 由调用方传），
所以单测可以直接喂数字，边界情形一次覆盖完。
"""

import ledger

GAP_MIN_SEC = 60.0
# 为什么是 60 秒：分片是碎片化 fMP4，ffmpeg 每写一批 moof 就刷新 mtime，
# 正常录制时 age 长期在 0~3 秒；而 mtime 是秒级、写缓冲还有 1~3 秒抖动，
# 区间之间的小缝（AVAIL_MERGE_SEC 已并掉一部分）再往下设阈值只会变成噪声。
# 一分钟的空洞已经是「这段时间点进去没画面」，值得让人看见。

STALL_SEC = 60.0
# 分片多久没被写过算「卡住」。真机断流时 ffmpeg 进程还活着（poll() 为 None），
# 只是不再有输出 —— 只看进程存在就会一直报「录像中」。

STATE_LABEL = {
    "off": "未启用",
    "stopped": "已停止",
    "connecting": "连接中",
    "recording": "录像中",
    "stalled": "卡住",
    "recovering": "恢复中",
    "error": "异常",
}


def gaps_of(spans, now=None, min_sec=GAP_MIN_SEC, lo=0.0, hi=ledger.DAY_SEC):
    """可用区间 → 缺口列表。

    spans 是 `ledger.avail_spans` 的输出（同一天钟点秒、已合并小缝），
    所以这里只需要处理「开头、区间之间、结尾」三种洞。

    now 传了就只算到现在为止：今天还没到的时段不算缺口，否则每到下午就报
    「今天缺了 8 小时」——那是还没发生，不是丢了。
    """
    cap = float(hi)
    if now is not None:
        cap = min(cap, max(0.0, float(now)))
    cur = max(0.0, float(lo))
    out = []
    for sp in spans or ():
        try:
            s = max(0.0, float(sp[0]))
            e = min(float(sp[1]), cap)
        except (TypeError, IndexError, ValueError):
            continue
        if e <= cur:
            continue
        if s - cur >= float(min_sec):
            out.append({"start": round(cur, 1), "end": round(s, 1),
                        "duration": round(s - cur, 1)})
        cur = max(cur, e)
    if cap - cur >= float(min_sec):
        out.append({"start": round(cur, 1), "end": round(cap, 1),
                    "duration": round(cap - cur, 1)})
    return out


def summarize(spans, gaps, now=None):
    """一路（或全部）的覆盖摘要。

    covered 用区间实测时长累加，不用「末片起点 - 首片起点」：后者会把断流
    那段一起算成有画面（真机车库 50 分钟里断了 19.2 分钟）。
    """
    covered = 0.0
    for sp in spans or ():
        try:
            covered += max(0.0, float(sp[1]) - float(sp[0]))
        except (TypeError, IndexError, ValueError):
            continue
    gap_sec = sum(float(g.get("duration") or 0) for g in gaps or ())
    max_gap = max([float(g.get("duration") or 0) for g in gaps or ()] or [0.0])
    span_day = float(now) if now is not None else float(ledger.DAY_SEC)
    span_day = max(1.0, span_day)
    return {
        "covered_sec": round(covered, 1),
        "gap_sec": round(gap_sec, 1),
        "gap_count": len(gaps or ()),
        "max_gap_sec": round(max_gap, 1),
        # 覆盖率分母是「从 00:00 到现在」而不是 24 小时：上午看页面时
        # 拿满一天做分母，永远显示 30%，看着像坏了。
        "coverage": round(min(1.0, covered / span_day), 4),
    }


def state_of(ch, idle_sec=None, running=None, stall_sec=STALL_SEC):
    """一路录像的细分状态。

    ch：`recorder.status()["channels"]` 里的单路字典。
    idle_sec：上游取流多少秒没收到字节，None = 不知道（没走统一取流 / 走直连）。
    running：进程是否活着，None = 用 ch 里的 state 推。

    判定顺序刻意如此：
      1. 没启用 → off（用户自己关的，不该报异常）
      2. 进程活着但分片不增长 / 上游不来字节 → stalled（最危险的一种：
         界面以前把它显示成"录像中"，因为进程确实在）
      3. 进程不在、有失败次数 → recovering / connecting（还在重连循环里）
      4. 进程不在、没有失败次数、有 error → error（不会再自己起来了）
    """
    if not ch.get("enabled", True):
        return "off"
    if running is None:
        running = ch.get("state") in ("recording", "recovering")
    age = ch.get("last_age_sec")
    tries = int(ch.get("retry") or 0)
    stall = int(ch.get("stall") or 0)
    if not running:
        if stall > 0:
            return "recovering"
        if tries > 0:
            return "connecting"
        return "error" if ch.get("error") else "stopped"
    if age is not None and float(age) > float(stall_sec):
        return "stalled"
    if idle_sec is not None and float(idle_sec) > float(stall_sec):
        return "stalled"
    return "recording"


def reason_of(state, ch, idle_sec=None):
    """给界面的一句话原因。空字符串表示「没什么可说的」。"""
    if state == "off":
        return "未启用录像"
    if state == "error":
        return str(ch.get("error") or "录制进程已退出，且没有重试记录")
    if state == "connecting":
        return "正在重连上游（已试 %d 次）" % int(ch.get("retry") or 0)
    if state == "recovering":
        return "上次卡住后正在恢复（第 %d 次）" % int(ch.get("stall") or 0)
    if state == "stalled":
        age = ch.get("last_age_sec")
        idle = ("上游 %.0f 秒没数据" % float(idle_sec)) if idle_sec is not None else ""
        if age is not None:
            tail = "分片 %.0f 秒没增长" % float(age)
            return ("%s；%s" % (idle, tail)) if idle else tail
        return idle or "录制进程在跑，但没有输出"
    if state == "stopped":
        return "录像进程未运行"
    return ""


def gap_reason(gap, state_now, day_is_today=False):
    """给一段缺口配原因。

    这不是「猜」，是区分三种完全不同的责任：
      - 断在现在、这条路当前 stalled → 进程在跑但没数据，正在发生
      - 断在过去、该路当前正常 → 那段时间断过流/应用没开，已经恢复
      - 起点就是 00:00 → 通常是那天应用没启动，或保留策略只覆盖了后半段
    """
    st = float(gap.get("start") or 0)
    if st <= 1.0:
        return "当日 00:00 起就没有录像（应用未启动或该日录像已被清理）"
    if day_is_today and state_now == "stalled":
        return "录制进程在跑，但已停止输出（上游断流 / 摄像头无响应）"
    if state_now in ("connecting", "recovering"):
        return "断流后正在重连，这段时间没有画面"
    if state_now == "error":
        return "录制进程已退出，这段时间没有画面"
    return "该时段录像进程未运行（断流、重启或应用未启动），已恢复"


def day_report(cams_state, now=None):
    """多路汇总。cams_state 是每路一段 dict（含 gap_sec / covered_sec）。"""
    covered = sum(float(c.get("covered_sec") or 0) for c in cams_state)
    gap_sec = sum(float(c.get("gap_sec") or 0) for c in cams_state)
    return {
        "cams": len(cams_state),
        "covered_sec": round(covered, 1),
        "gap_sec": round(gap_sec, 1),
        "gap_count": sum(int(c.get("gap_count") or 0) for c in cams_state),
        "max_gap_sec": round(max([float(c.get("max_gap_sec") or 0)
                                  for c in cams_state] or [0.0]), 1),
    }
