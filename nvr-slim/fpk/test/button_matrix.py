#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置项验收表：验「后端真的变了」，而不是「界面看着变了」。

为什么需要这张表
----------------
设置页的草稿值（SET.draft）和服务端落盘值是两回事：控件一改，界面先改草稿，
250ms 防抖后才由 pushCfg() 推 /api/config；服务端还会用 runtime.coerce() 对
枚举与类型强校一遍，非法值悄悄回落默认。只断言 UI 上的值，等于只验了
「我把字打进了输入框」，验不到「保存成功」四个字背后的任何东西。

所以每行固定验四层，缺一层就不给 pass：

  1. POST 的 changed 里有这个 key —— 服务端承认它变了（被 coerce 打回就不在）；
  2. GET /api/config 回读值变了且等于写入值 —— 真的落盘，不是只改了内存；
  3. GET /api/status 或 /api/link 的派生值跟着变 —— 运行态/进程参数用了新值；
  4. /proc/<pid>/cmdline 里真有那个参数 —— 只有 ffmpeg 的 argv 能证明这一点。

为什么要有 partial 这一档
-------------------------
不是每个设置项都有运行态出口：cleanup_at 只影响夜间定时任务，服务端没有任何
接口把「下次几点跑」摆出来；ingest_enable 要重启服务才生效；record_frag 在
/api/link 的 record 里干脆没有字段。把这类行判成 pass 是撒谎，判成 fail 又会让
整张表永远红，于是单列 partial 并写明还差什么。

更要紧的是 **ffmpeg 的完整 argv 没有任何 HTTP 出口**：所有接口只给配置值、
pid、segment_sec、state，不给命令行。要不要读 /proc/<pid>/cmdline 由采集环境
决定，采不到就降 partial，绝不拿「派生值变了」冒充「进程参数变了」。

「失败不等于没有」在这张表里同样成立：快照没采到、观测点缺键一律 unknown，
不会因为「没报错」就算通过。

边界
----
这份模块是纯函数，输入输出都是 dict，不碰产品代码、不起服务、不改配置，
因此能直接进 build.sh --check。采集器（真机/本地，负责改值、等防抖、取快照、
还原）留到下一轮接；表本身先负责把「该验什么、怎么算过」钉死，并照出现状里
哪些按钮其实推不到后端（见 KNOWN_GAPS）。

跑法： python test/test_button_matrix.py
"""
import re

# 取不到值不能用 None 代替：updated_at 的默认值就是 None，拿 None 当「缺失」
# 会把合法的空值误判成采集失败。哨兵必须是独立对象。
MISSING = object()

STATES = ("pass", "partial", "fail", "unknown")


# ---------------------------------------------------------------- 快照取值

def probe(snap, path):
    """按点路径取快照里的值。'*' 表示遍历容器每一项的同一字段。

    '*' 同时认 list 与 dict：/api/link 的 record.channels 是数组，
    而 /api/status 的 frames 是 {cam_id: {...}} 字典，两种都得能一次取全。
    """
    return _probe(snap, path.split("."))


def _probe(cur, segs):
    if not segs:
        return cur
    seg, rest = segs[0], segs[1:]
    if seg == "*":
        if isinstance(cur, (list, tuple)):
            items = list(cur)
        elif isinstance(cur, dict):
            items = list(cur.values())
        else:
            return MISSING
        if not items:
            return MISSING          # 空数组不是「取到了空」，是根本没采到
        return [_probe(x, rest) for x in items]
    if not isinstance(cur, dict) or seg not in cur:
        return MISSING
    return _probe(cur[seg], rest)


def has_missing(value):
    """递归查哨兵：'*' 取出来的是列表，里头只要有一项缺键就不能算采全。"""
    if value is MISSING:
        return True
    if isinstance(value, (list, tuple)):
        return any(has_missing(x) for x in value)
    if isinstance(value, dict):
        return any(has_missing(x) for x in value.values())
    return False


# ---------------------------------------------------------------- 验收表

def _row(key, label, ui, landed, runtime=(), note="", proc="", restart=False):
    """一行的字段都是「验什么」的说明，不掺采集动作。

    runtime 只许写 status./link. 开头的路径：config.* 是配置回显，
    拿它当运行态观测等于把同一个值查两遍，正是清单要防的「只查 UI 值」。
    """
    return {"key": key, "label": label, "ui": ui, "landed": landed,
            "runtime": list(runtime), "runtime_note": note,
            "proc": proc, "restart_required": bool(restart)}


# 前端 pushCfg() 推的 19 个顶层 key 全部在表里，一个不漏。
# 其中若干项 UI 已下线或恒值，note 里如实写明，不假装「点得到按钮」。
ROWS = [
    _row("record_enable", "录制总开关", '[data-rec="enabled"]',
         "config.record_enable", ("link.record.enabled",),
         note="热生效会 kill/重拉录制进程，验收时务必还原，别把测试机留在关录状态。"
              "生效证据只认 link.record.enabled（Recorder 对象自己的开关）："
              "/api/link 顶层的 record_enable 是现读 config.json 补的回读字段；"
              "status.cams.recording 数的是「此刻真在跑的 ffmpeg」，没装 ffmpeg 或"
              "上游没数据时它照样是 0，把开关的生效与否和进程存活混成一件事。"),
    _row("record_dir", "录像保存目录", "#recDir",
         "config.record_dir", ("link.record.root",),
         note="服务端 restart_hint 明确提示需重启；且改目录会触发录像重启",
         restart=True),
    _row("record_segment_sec", "录制分片长度", "[data-linkseg]",
         "config.record_segment_sec",
         ("link.segment_sec", "link.record.segment_sec",
          "link.record.channels.*.segment_sec"),
         proc="-segment_time"),
    _row("snapshot_interval", "快照刷新间隔", "[data-rate]",
         "config.snapshot_interval", ("status.frames.*.interval",),
         note="frames 是 {cam_id: {...}} 字典，取流线程每轮重读配置，热生效"),
    _row("cleanup_at", "夜间任务时刻", "[data-cleanat]",
         "config.cleanup_at", (),
         note="只影响夜间定时任务，服务端没有「下次几点跑」的接口，只能验落盘"),
    _row("record_frag", "分片 MP4", "（UI 无入口，前端恒推 true）",
         "config.record_frag", (),
         note="/api/link 的 record 没有 frag 字段，reconfigure 只以重启进程间接体现；"
              "真要验得读 /proc 的 -segment_format_options",
         proc="-segment_format_options"),
    _row("link_mode", "录制与压制衔接", "（UI 无入口，前端恒推 live）",
         "config.link_mode", ("link.mode", "link.mode_label"),
         note="只能直接 POST 验收；改模式会动入队节奏，验完立刻还原"),
    _row("clip_style", "缩时切分方式", "（UI 已下线）",
         "config.clip_style", ("link.clip_style", "link.clip_style_label")),
    _row("condense_mode", "事件侦测总闸", '[data-ledger="1"]',
         "config.condense_mode", (),
         note="台账模式没有独立的运行态接口，只能验落盘"),
    _row("archive_enable", "归档裁剪开关", "[data-archen]",
         "config.archive_enable", (),
         note="夜间任务才读它，无实时出口"),
    _row("archive_days", "归档保留天数", "#archDaysInput",
         "config.archive_days", (),
         note="夜间清理才读它，无实时出口"),
    _row("archive_batch", "单次归档上限", "（UI 无入口）",
         "config.archive_batch", (),
         note="夜间任务才读它，无实时出口"),
    _row("archive_prefetch", "提前一天归档", "[data-archpre]",
         "config.archive_prefetch", (),
         note="夜间任务才读它，无实时出口"),
    _row("link_interval_h", "定时压制间隔", "（UI 无入口）",
         "config.link_interval_h", ("link.interval_h",)),
    _row("link_max_queue", "待处理队列上限", "（UI 无入口）",
         "config.link_max_queue", ("link.max_queue",)),
    _row("concurrency", "同时处理路数", "[data-conc]",
         "config.concurrency", ("status.limits.configured", "status.slots.total"),
         note="该控件默认不渲染（LK.clipOut=false），验收需直接 POST；"
              "另注意 STATE['pool'] 是启动时建的，/api/slot/acquire 不会跟着变"),
    _row("gpu", "硬件加速", "（无 UI，前端按 GS.gpu 推 auto/off）",
         "config.gpu", (),
         note="status.gpu 是硬件探测结论不是配置回显；limits.downgraded 还依赖"
              "现场有没有核显，都不适合当稳定观测点"),
    _row("detect", "AI 检测块", "[data-deten] / [data-detcls] 等",
         "config.detect.enable", ("status.detect.enabled", "status.detect.state"),
         note="detect.enable 有 _sync_detect_worker 热生效；"
              "detect.confirm 没进 UI，保存时由服务端沿用磁盘旧值"),
    _row("notify", "通知块", "[data-ntsw] / #ntWebhook 等",
         "config.notify.cooldown", (),
         note="status.notify 只有 recent/pending，改冷却不会立刻变"),
    _row("cameras", "摄像头列表", "[data-camname] / [data-camseg] 等",
         "config.cameras.*.name",
         ("link.record.channels.*.name",),
         note="锚点取「名字」不取「单路分片长度」：segment_sec 改成 0 表示跟随通用设置，"
              "键会从配置里消失，同一个观测点在改前/改后两种形态下键名不同，"
              "判「变没变」会把合法操作误判成采集失败；名字一定在，且它决定 "
              "dir_name，Recorder 的通道列表必须跟着换。landed 取 name 不取 url："
              "url 出接口前被抹成 ***，拿脱敏值判「变没变」会把所有地址改动都误判成没变。"
              "通道总数（status.cams.total）不随编辑变化，列进来只会制造假失败。"),
]


# 前端根本不推、或不能拿来自动改的 DEFAULTS 键。每项都要写清为什么不验：
# 「没进表」本身就是一个结论，不能悄悄略过。
NOT_TESTABLE = {
    "source_dir": "遗留字段，录制根目录已统一走 record_dir，UI 无入口",
    "output_dir": "遗留字段，同上",
    "scan_mode": "遗留字段，UI 无入口",
    "process_mode": "遗留字段，UI 无入口",
    "night_only": "见 KNOWN_GAPS：控件默认不渲染，pushCfg 也不带这个键",
    "low_priority": "见 KNOWN_GAPS：控件默认不渲染，且不作用于录制进程",
    "record_reconnect_sec": "断流退避基数，UI 无入口，改它会动录制链路的恢复行为",
    "ingest_enable": "UI 不推该键，且只在服务启动时读一次，改完必须重启才生效",
    "service_port": "改完要重启且端口会变，验收脚本会当场失联，只能人工验",
    "updated_at": "服务端保存配置时自动写的时间戳，不是设置项",
}


# 验收表照出来的现状问题：界面上有按钮、按下去也有反馈，但后端永远不会变。
# 这些不是「验收没过」，是「这一项压根没接线」，单列出来给设置页整理时处理，
# 本轮不改产品代码（清单外的大改动记进 ROADMAP「不在本轮范围」）。
KNOWN_GAPS = [
    {"key": "night_only", "label": "只在夜间跑重编码", "ui": '[data-gs="nightOnly"]',
     "why": "控件被 LK.clipOut 包住、默认不渲染；pushCfg 的 body 里没有这个键，"
            "值只留在 localStorage 的 gen.nightOnly"},
    {"key": "low_priority", "label": "低优先级·磁盘空闲才写", "ui": '[data-gs="lowPrio"]',
     "why": "同上只进 localStorage；另外 /api/status 的 limits.prefix 只是按配置"
            "拼出来的字符串，录制 ffmpeg 的 Popen 根本没加这个前缀"},
    {"key": None, "label": "AI 灵敏度", "ui": "[data-detsens]",
     "why": "pushCfg 的 detect 块不带 sensitivity，detect.norm_detect 也不产出"
            "该字段，界面三档切换后端永远收不到"},
    {"key": None, "label": "输出画质 / 切分方式 / 输出倍速 / 浓缩片段保留",
     "ui": "settings_editor.js 已注释下线",
     "why": "控件整体下线，但 pushCfg 仍推 clip_speed / clip_mode 两个默认常量，"
            "属历史残留，不是可验收的设置项"},
]


def covered_keys():
    """表里「已经给过说法」的 DEFAULTS 键：有验收行、有免验理由、或已知是假设置。"""
    out = {r["key"] for r in ROWS}
    out |= set(NOT_TESTABLE)
    out |= {g["key"] for g in KNOWN_GAPS if g.get("key")}
    return out


def coverage_gaps(defaults):
    """DEFAULTS 里既没验收行也没理由的键。空列表才算这张表没漏项。"""
    return sorted(set(defaults) - covered_keys())


def source_keys(live_js_path):
    """从 live.js 里宽松找「前端提到过的键」，用来守住表和代码不漂移。

    只做存在性守卫，不做精确解析：宽松匹配可能多抓（注释、嵌套字段都会进来），
    但调用方会与 DEFAULTS 求交，剩下的只可能是真正的顶层配置键。
    宁可多抓也不要因为缩进改了两个空格就假装「源码里没这项」。
    """
    with open(live_js_path, "r", encoding="utf-8") as f:
        text = f.read()
    return set(re.findall(r"\b([a-z][a-z0-9_]*)\s*:", text))


# ---------------------------------------------------------------- 判定

def verify_row(row, before, after, changed, expected=MISSING, proc=None):
    """判一行。before/after 是采集器拼的三份响应合起来的快照：

        {"config": GET /api/config 的 config,
         "status": GET /api/status,
         "link":   GET /api/link}

    changed 是 POST /api/config 回的 changed 数组；expected 是本次真正写进去
    的值（不给就只判「变了」）；proc 是可选的 /proc/<pid>/cmdline 文本。
    """
    checks = []
    fatal, fails, partials = [], [], []

    def note(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:160]})

    key = row["key"]

    if not isinstance(before, dict) or not isinstance(after, dict):
        return _result(row, "unknown", "改前/改后快照没采全，不判通过", checks)
    if changed is None:
        return _result(row, "unknown", "没拿到 POST 的 changed，无从判断服务端认没认", checks)

    # 1. 服务端承认变更
    ok = key in changed
    note("POST 承认变更", ok, "changed=%s" % (list(changed),))
    if not ok:
        fails.append("POST 的 changed 里没有 %s：可能是 coerce 把值打回了默认，"
                     "也可能前端签名去重根本没发请求" % key)

    # 2. 落盘回读
    b = probe(before, row["landed"])
    a = probe(after, row["landed"])
    if b is MISSING or a is MISSING or has_missing(a):
        fatal.append("落盘观测点 %s 在快照里缺键" % row["landed"])
        note("落盘回读", False, "缺键")
    else:
        ok = a != b
        if expected is not MISSING:
            ok = ok and a == expected
        note("落盘回读", ok, "%r → %r" % (b, a))
        if not ok:
            fails.append("落盘值 %r 没变成写入值 %r（或压根没变）"
                         % (a, "(未指定)" if expected is MISSING else expected))

    # 3. 运行态：这才是「不只查 UI 值」的那一层
    rts = row["runtime"]
    if not rts:
        note("运行态", False, "无出口")
        partials.append(row["runtime_note"] or "当前没有运行态出口，只验到落盘")
    for p in rts:
        bv = probe(before, p)
        av = probe(after, p)
        if bv is MISSING or av is MISSING or has_missing(av):
            fatal.append("运行态观测点 %s 在快照里缺键" % p)
            note("运行态 " + p, False, "缺键")
            continue
        ok = av != bv
        note("运行态 " + p, ok, "%r → %r" % (bv, av))
        if not ok:
            fails.append("运行态没跟着变：%s 仍是 %r" % (p, av))

    # 4. 进程参数：采不到就降档，绝不拿派生值冒充 argv
    if row["proc"]:
        if proc is None:
            note("进程参数", False, "未采 /proc/<pid>/cmdline")
            partials.append("进程 argv 未采集：所有 HTTP 接口都不输出 ffmpeg 命令行，"
                            "需拿 /api/link 的 pid 走 SSH 读 /proc")
        elif row["proc"] not in str(proc):
            fails.append("进程命令行里没有 %s" % row["proc"])
            note("进程参数", False, "未命中 " + row["proc"])
        else:
            note("进程参数", True, "命中 " + row["proc"])

    if row["restart_required"]:
        partials.append("该项需重启服务才热生效，本轮只验到落盘")

    if fatal:
        return _result(row, "unknown", "；".join(fatal), checks)
    if fails:
        return _result(row, "fail", "；".join(fails), checks)
    if partials:
        return _result(row, "partial", "；".join(partials), checks)
    return _result(row, "pass", "四层都验到了", checks)


def _result(row, state, reason, checks):
    return {"key": row["key"], "label": row["label"], "state": state,
            "reason": reason, "checks": checks}


def roll_up(items):
    """把逐项结果收成一页纸结论。

    单独抽出来是因为「一次跑全表」和「逐项改值再采」两条路必须给同一个口径：
    采集器（test/btn_harvest.py）逐项取快照、只能一项一项判，如果它自己再数一遍
    tally，迟早会和这里数出两个不一样的结论 —— 而看报告的人只会看那一行。
    """
    tally = {s: sum(1 for x in items if x["state"] == s) for s in STATES}
    return {"total": len(items), "tally": tally, "items": items,
            "ok": tally["fail"] == 0 and tally["unknown"] == 0}


def verify_all(rows, before, after, changed, expected=None, proc=None):
    """跑整张表。partial 算过（如实标注还差什么），fail/unknown 不算过。"""
    expected = expected or {}
    return roll_up([verify_row(r, before, after, changed,
                               expected.get(r["key"], MISSING), proc)
                    for r in rows])


def summary(report):
    """给采集器打印用的一页纸结论。false 的行逐条列原因，方便直接贴进汇报。"""
    t = report["tally"]
    lines = ["设置项验收：%d 项，通过 %d / 部分 %d / 失败 %d / 未知 %d，总判定 %s"
             % (report["total"], t["pass"], t["partial"], t["fail"], t["unknown"],
                "通过" if report["ok"] else "未通过")]
    for x in report["items"]:
        if x["state"] in ("fail", "unknown"):
            lines.append("  [%s] %s %s：%s"
                         % (x["state"].upper(), x["key"], x["label"], x["reason"]))
    return "\n".join(lines)
