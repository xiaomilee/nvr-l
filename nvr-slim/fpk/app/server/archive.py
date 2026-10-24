#!/usr/bin/env python3
"""归档裁剪（Aqara 式三层存储的执行层）。

三层寿命：台账（_ledger，永久）> 归档片段（_archive，默认 90 天）> 全时录像（默认数天）。

核心思路：全时录像保留期短（体积大），到期前夜把其中的活动窗口按台账
裁出来存进 _archive（体积只有活动占比那么大），之后全时目录就可以放心被
日常清理删掉——回放页时间轴上，过期日期只剩归档片段可播 + 橙标记常在。

选日规则（宁缺勿滥）：
  1. 优先「明天 0 点就过期」的日期 —— 今天是它最后的机会；
  2. 然后回填更早的漏网日期（上次失败/停机错过），从最临期往回；
  3. 没有台账的临期日期直接跳过并记入「未归档」清单 —— 不在临期前夜
     现算判定（判定是 CPU 活，夜里现算会拖垮整批，而且临时算出来的
     结论没有经过重判消化，宁缺勿错）。

幂等：每个分片的产物以 <分片名>.idx.json 存在与否判定（write_split 自带）；
整日收尾写 .done 标记。失败窗口跳过不阻塞整日，没有 .done 的日期下次重跑。

跨夜分批：无核显机器一晚裁不完一天的量，archive_batch 限制单次运行的
分片数，剩下的下次继续（排序保证总是最临期的先被处理）。
"""
import json
import os
import time
from datetime import date, timedelta

from runtime import under  # noqa: F401  (re-export 供 nvr_slim 路径校验)

DEFAULTS = {
    "archive_enable": True,   # 夜间任务是否做归档裁剪
    "archive_days": 90,       # 归档片段保留天数
    "archive_batch": 120,     # 单次运行最多裁多少个分片（跨夜分批）
}

LIMITS = {"archive_days": (1, 3650), "archive_batch": (10, 2000)}

DONE_NAME = ".done"


def clamp(key, value):
    lo, hi = LIMITS.get(key, (1, 3650))
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = DEFAULTS.get(key, lo)
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 选日

def select_days(day_names, full_days, today, done_names, ledger_names):
    """一天该不该归档、什么顺序归档。

    day_names     该路全时目录下真实存在的日期目录名
    full_days     这一路全时保留天数（保留 N 天 = 今天 + 前 N-1 天）
    done_names    _archive 下已有 .done 的日期集合
    ledger_names  _ledger 下有台账的日期集合
    返回 {"todo": [date...], "no_ledger": [...]}，todo 按临期程度升序
    （最先过期、最急的排最前）。
    """
    cut = today - timedelta(days=max(1, int(full_days)) - 1)
    expiring, backfill, no_ledger = [], [], []
    for name in sorted(day_names):
        try:
            d = date.fromisoformat(name)
        except ValueError:
            continue
        if d >= today or name in done_names:
            continue                       # 未来日期 / 已归档
        if d > cut:
            continue                       # 还在保留期内（今天不会删），不用急
        if name not in ledger_names:
            no_ledger.append(name)         # 无台账，宁缺勿滥，明确跳过
            continue
        # d == cut：今天还在窗口内、明天 0 点进删除清单 → 最后的机会，最优先；
        # d < cut：已过期但还没归档（上次失败/停机错过）→ 回填
        (expiring if d == cut else backfill).append(name)
    return {"todo": expiring + backfill, "no_ledger": no_ledger}


def slice_windows(events, segments):
    """全天钟点秒窗口 × 分片区间 → 每个分片自己的相对秒窗口。

    events   [(s, e)] 全天钟点秒（台账 events_of 的输出，已合并）
    segments {分片名: (start_sec, end_sec)}（end 为排他的估算终点）
    返回 {分片名: [(ss, to), ...]}，只含有交集的分片；空分片不出现在结果里。
    """
    out = {}
    for name, (st, en) in segments.items():
        hits = []
        for s, e in events:
            ss = max(s, st)
            to = min(e, en)
            if to - ss > 0.01:
                hits.append((round(ss - st, 2), round(to - st, 2)))
        if hits:
            out[name] = hits
    return out


def archive_dir(root, dname, day):
    return os.path.join(root, "_archive", dname, day)


def done_path(arc_dir):
    return os.path.join(arc_dir, DONE_NAME)


def is_done(arc_dir):
    return os.path.isfile(done_path(arc_dir))


def write_done(arc_dir, n_files):
    try:
        os.makedirs(arc_dir, exist_ok=True)
        tmp = done_path(arc_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"files": n_files,
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        os.replace(tmp, done_path(arc_dir))
    except OSError:
        pass


# ---------------------------------------------------------------- 计划

def plan(root, cams, cam_cfg, modes, today=None, seg_sec=300.0,
         ledger_dirs=None, scan_fn=None):
    """算一遍要归档什么，不真裁。预览结构与 retention.plan 对齐（UI 同款交互）。"""
    today = today or date.today()
    ledger_dirs = ledger_dirs or (lambda dn: os.path.join(root, "_ledger", dn))
    scan_fn = scan_fn or (lambda base: _scan_days(base))
    cfg = cam_cfg or {}
    out = {"todo": [], "no_ledger": [], "done": [], "policy": [],
           "segments": 0, "bytes": 0, "est_bytes": 0}
    for cam in cams:
        cid = cam.get("id")
        if not cid:
            continue
        dn = cam.get("dir_name") or cid
        cc = cfg.get(cid) or {}
        full_days = cc.get("full_days", 4)
        try:
            full_days = int(full_days)
        except (TypeError, ValueError):
            full_days = 4
        mode = modes.get(cid) or "virtual"
        pol = {"cam": cid, "name": cam.get("name", cid), "mode": mode,
               "full_days": full_days,
               "archive": mode == "virtual"}
        out["policy"].append(pol)
        if mode != "virtual":
            continue                       # materialize 有 _clip，off 无台账
        led_root = ledger_dirs(dn)
        led_names = set()
        if os.path.isdir(led_root):
            # _ledger/<cam>/<YYYY-MM-DD>.json —— 是文件，不是目录
            led_names = {n[:-5] for n in os.listdir(led_root)
                         if len(n) == 15 and n.endswith(".json")}
        for dname in dict.fromkeys([dn, cid]):
            base = os.path.join(root, dname)
            if not os.path.isdir(base):
                continue
            sel = select_days(scan_fn(base), full_days, today,
                              _done_days(root, dname), led_names)
            for d in sel["no_ledger"]:
                out["no_ledger"].append({"cam": cid, "name": cam.get("name", cid),
                                         "dir": dname, "date": d})
            for d in sel["todo"]:
                item = {"cam": cid, "name": cam.get("name", cid),
                        "dir": dname, "date": d,
                        "segments": 0, "bytes": 0, "est_bytes": 0}
                day_base = os.path.join(base, d)
                ev = _ledger_events(led_root, d)
                for f in _day_files(day_base):
                    st = _src_start(f)
                    if st is None:
                        continue
                    item["segments"] += 1
                    item["bytes"] += _fsize(os.path.join(day_base, f))
                if ev:
                    covered = sum(e - s for s, e in
                                  _day_cover(item, day_base, seg_sec))
                    if covered > 0:
                        active = sum(e - s for s, e in ev)
                        item["est_bytes"] = int(item["bytes"] *
                                                min(1.0, active / covered))
                out["todo"].append(item)
                out["segments"] += item["segments"]
                out["bytes"] += item["bytes"]
                out["est_bytes"] += item["est_bytes"]
    # 不做全局重排：select_days 已保证每路「临期优先、回填靠后」的顺序，
    # run() 按 todo 顺序消费，batch 截断时最急的日期一定先被处理。
    return out


def _day_cover(item, day_base, seg_sec):
    cov = []
    for f in _day_files(day_base):
        st = _src_start(f)
        if st is not None:
            cov.append((st, min(86400.0, st + seg_sec)))
    return cov


def _scan_days(base):
    return [n for n in os.listdir(base)
            if len(n) == 10 and n[4] == "-" and n[7] == "-"
            and os.path.isdir(os.path.join(base, n))]


def _done_days(root, dname):
    arc = os.path.join(root, "_archive", dname)
    if not os.path.isdir(arc):
        return set()
    return {n for n in os.listdir(arc)
            if is_done(os.path.join(arc, n))}


def _ledger_events(led_root, d):
    p = os.path.join(led_root, d + ".json")
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [(float(w["s"]), float(w["e"]))
                for w in (data.get("windows") or [])
                if float(w.get("e", 0)) > float(w.get("s", 0))]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def _day_files(day_base):
    try:
        return [n for n in sorted(os.listdir(day_base))
                if n.lower().endswith(".mp4")]
    except OSError:
        return []


def _src_start(name):
    """HH-MM-SS(.ext) → 当天钟点秒；不认识的命名返回 None（跳过）。"""
    b = os.path.basename(name).split(".")[0]
    try:
        h, m, s = b.split("-")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


def _fsize(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


# ---------------------------------------------------------------- 执行

def run(root, cams, cam_cfg, modes, ffmpeg, today=None, seg_sec=300.0,
        batch=120, scan_fn=None, on_log=None):
    """执行归档裁剪。返回结果摘要（与 retention.execute 风格一致）。

    单分片失败只记日志跳过（不写 idx，下次重试），不阻塞同日其它分片、
    不阻塞后续日期。分片总数到 batch 就停 —— 跨夜慢慢消化。
    """
    today = today or date.today()
    p = plan(root, cams, cam_cfg, modes, today=today, seg_sec=seg_sec,
             scan_fn=scan_fn)
    log = on_log or (lambda msg: None)
    archived_days, files, out_bytes = [], 0, 0
    failed, used = [], 0
    for item in p["todo"]:
        if used >= batch:
            break                          # 分批：剩下的下次跑
        dn, d = item["dir"], item["date"]
        day_base = os.path.join(root, dn, d)
        arc = archive_dir(root, dn, d)
        ev = _ledger_events(os.path.join(root, "_ledger", dn), d)
        segs = {}
        for f in _day_files(day_base):
            st = _src_start(f)
            if st is not None:
                segs[f] = (st, min(86400.0, st + seg_sec))
        cuts = slice_windows(ev, segs)
        if not cuts:
            # 有台账但窗口为空（整天静默）：没有可裁的画面，直接收尾
            write_done(arc, 0)
            archived_days.append({**item, "files": 0, "empty": True})
            continue
        n_files, day_bytes, day_fail = 0, 0, 0
        planned = len(cuts)
        processed = 0
        for fname, wins in cuts.items():
            if used >= batch:
                break
            used += 1
            processed += 1
            src = os.path.join(day_base, fname)
            out_base = os.path.join(arc, fname[:-4])
            r = _split(ffmpeg, src, out_base, wins, arc)
            if r["ok"]:
                n_files += len(r.get("files") or [])
                for q in (r.get("files") or []):
                    day_bytes += _fsize(q)
            else:
                day_fail += 1
                failed.append({"cam": item["cam"], "date": d,
                               "file": fname, "error": r.get("error")})
                log("archive: 裁剪失败 %s/%s/%s: %s"
                    % (item["cam"], d, fname, r.get("error")))
        if not day_fail and processed >= planned:
            write_done(arc, n_files)       # 整日裁完且无失败才收尾
            archived_days.append({**item, "files": n_files,
                                  "bytes_out": day_bytes})
            files += n_files
            out_bytes += day_bytes
            log("archive: %s %s/%s 归档 %d 段" % (d, item["cam"], dn, n_files))
    return {"ok": not failed, "dry_run": False,
            "days": archived_days, "failed": failed,
            "segments_used": used, "batch": batch,
            "files": files, "bytes_out": out_bytes,
            "no_ledger": p["no_ledger"],
            "remaining": max(0, len(p["todo"]) - len(archived_days)),
            "plan": {"segments": p["segments"], "bytes": p["bytes"],
                     "est_bytes": p["est_bytes"], "policy": p["policy"]}}


def _split(ffmpeg, src, out_base, wins, arc_dir):
    """调 condense.write_split，输出目录不存在时先建。"""
    import condense
    os.makedirs(arc_dir, exist_ok=True)
    try:
        return condense.write_split(ffmpeg, src, out_base, wins,
                                    arc_dir, hw="qsv")
    except Exception as e:                 # ffmpeg 异常不能带死整批
        return {"ok": False, "error": str(e)}


def pretty_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
