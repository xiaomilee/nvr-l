#!/usr/bin/env python3
"""归档裁剪（Aqara 式三层存储的执行层）。

三层寿命：台账（_ledger，永久）> 归档片段（_archive，默认 7 天）= 总回溯天数 > 全时录像（默认 3 天，嵌套在归档窗口最前面）。

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
import ledger
import os
import time
from datetime import date, timedelta

from runtime import under  # noqa: F401  (re-export 供 nvr_slim 路径校验)

DEFAULTS = {
    "archive_enable": True,   # 夜间任务是否做归档裁剪
    "archive_days": 7,        # 归档片段保留天数（= 总回溯天数）
    "archive_batch": 120,     # 单次运行最多裁多少个分片（跨夜分批）
}

LIMITS = {"archive_days": (1, 3650), "archive_batch": (10, 2000)}

DONE_NAME = ".done"

# 0 帧片段的体积上限。ffmpeg 裁一个「起止都落在源文件之外」的区间时不会报错，
# 它会写出一个只有 ftyp+moov、没有 mdat 的空 mp4（真机实测 262 字节）。
# 这类空壳既不占空间也不算失败，但会让整日的 .done 记为"已完成"，
# 且在回放页变成一串 0.2 秒的黑块 —— 一律当失败处理并清理。
EMPTY_PART_BYTES = 1024


def _unlink(p):
    try:
        os.remove(p)
    except OSError:
        pass


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
        full_days = cc.get("full_days", 3)
        try:
            full_days = int(full_days)
        except (TypeError, ValueError):
            full_days = 3
        # 分片长度按路取（每路可单独覆盖），别拿全局值套所有通道
        cam_seg = _cam_seg_sec(cc, seg_sec)
        mode = modes.get(cid) or "virtual"
        pol = {"cam": cid, "name": cam.get("name", cid), "mode": mode,
               "full_days": full_days,
               "archive": mode == "virtual"}
        out["policy"].append(pol)
        if mode != "virtual":
            continue                       # materialize 有 _clip，off 无台账
        for dname in dict.fromkeys([dn, cid]):
            base = os.path.join(root, dname)
            if not os.path.isdir(base):
                continue
            # 台账按「这一轮在扫的目录名」定位：升级过渡期 dir_name 与 id 两个
            # 目录都可能存在台账，各查各的（与 ledger_payload 的合并口径一致）。
            led_root = ledger_dirs(dname)
            led_names = set()
            if os.path.isdir(led_root):
                # _ledger/<cam>/<YYYY-MM-DD>.json —— 是文件，不是目录
                led_names = {n[:-5] for n in os.listdir(led_root)
                             if len(n) == 15 and n.endswith(".json")}
            sel = select_days(scan_fn(base), full_days, today,
                              _done_days(root, dname, led_root), led_names)
            for d in sel["no_ledger"]:
                out["no_ledger"].append({"cam": cid, "name": cam.get("name", cid),
                                         "dir": dname, "date": d})
            for d in sel["todo"]:
                item = {"cam": cid, "name": cam.get("name", cid),
                        "dir": dname, "date": d,
                        "seg_sec": cam_seg,
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
                                  _day_cover(item, day_base, cam_seg, d))
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


def _day_cover(item, day_base, seg_sec, day=None):
    cov = []
    for f in _day_files(day_base):
        st = _src_start(f)
        if st is not None:
            cov.append((st, _seg_end(os.path.join(day_base, f),
                                     day or item.get("date"), st, seg_sec)))
    return cov


def _scan_days(base):
    return [n for n in os.listdir(base)
            if len(n) == 10 and n[4] == "-" and n[7] == "-"
            and os.path.isdir(os.path.join(base, n))]


def _done_files(arc_dir):
    """.done 里记的产物片段数；读不出来返回 -1（当作有产物，不重做）。"""
    try:
        with open(done_path(arc_dir), "r", encoding="utf-8") as f:
            return int((json.load(f) or {}).get("files", -1))
    except (OSError, ValueError, TypeError, AttributeError):
        return -1


def _done_days(root, dname, led_root=None):
    """已收尾的日期集合。

    给了 led_root 就顺手自愈一次：.done 记着 files=0（「整天静默」的收尾），
    但台账里其实有活动窗口 —— 说明那次收尾读到了空台账、是误判，放回去重做。
    0.9.100 及以前 _ledger_events 读错键，每一天都会被这样误标成已归档，
    没有这个自愈，历史日期永远救不回来。
    """
    arc = os.path.join(root, "_archive", dname)
    if not os.path.isdir(arc):
        return set()
    out = set()
    for n in os.listdir(arc):
        d = os.path.join(arc, n)
        if not is_done(d):
            continue
        # 产物里还有 0 帧空壳 → 这日并没真正裁完（窗口与分片错配），放回去重做。
        # run() 会先清掉空壳再重切，所以这里只判断、不动文件。
        if _has_empty_parts(d):
            continue
        if led_root and _done_files(d) == 0 and _ledger_events(led_root, n):
            continue                      # 误标：台账有活动，重新归档
        out.add(n)
    return out


def _ledger_events(led_root, d):
    """台账 → 全天活动窗口 [(s, e)]（当天钟点秒，已按 GAP 合并）。

    台账 v1 的真实结构是
        {"v":1, "sources": {"<分片名>.mp4": {"start":…, "windows": [[s, e, cls], …]}}}
    窗口嵌在 sources 下面、元素是**列表**。

    0.9.100 及以前这里读的是顶层 data["windows"] 且按 w["s"]/w["e"] 取键，
    层级和元素类型两处都不对，于是永远返回空列表：归档把每一天都判成
    「整天静默」，写 .done(0) 之后不再重试 —— 表现就是连续运行多天、
    _archive 里一个文件都没有。0.9.101 修复。
    """
    p = os.path.join(led_root, d + ".json")
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    raw = []
    srcs = data.get("sources")
    if isinstance(srcs, dict):
        for v in srcs.values():
            if not isinstance(v, dict):
                continue
            for w in (v.get("windows") or []):
                try:
                    s, e = float(w[0]), float(w[1])
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
                if e > s:
                    raw.append((s, e))
    # 兼容更早的顶层写法（dict 与 [s, e] 两种历史形态），读到就并进去
    for w in (data.get("windows") or []):
        try:
            if isinstance(w, dict):
                s, e = float(w["s"]), float(w["e"])
            else:
                s, e = float(w[0]), float(w[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if e > s:
            raw.append((s, e))
    return [(s, e) for s, e in ledger.union(raw)]


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


def _cam_seg_sec(cc, default):
    """这一路的分片长度。每路可以单独覆盖 record_segment_sec。

    0.9.101 及以前 plan/run 只吃全局 seg_sec，把「配 60 秒」的通道当成 300 秒算：
    一个 18:40:22 的分片被估算成覆盖到 18:45:22，于是 18:45:00 的活动窗口被
    派给该片去裁——那片其实 18:41:22 就结束了，裁出来全是空壳。
    """
    try:
        v = float((cc or {}).get("segment_sec"))
        if v >= 5:
            return v
    except (TypeError, ValueError, AttributeError):
        pass
    return float(default or 300.0)


def _seg_end(path, day, st, seg_sec):
    """分片真实结束时刻（当天钟点秒）。

    用 seg_sec 估算是必要条件但不充分：ffmpeg 的 segment muxer 只在切到下一片时
    才关闭上一片，配置片长与实际片长会差（真机见过「配 120 实际 84」）。mtime
    就是最后一帧写下去的时刻，用它把上界收敛回来；只有 mtime 落在同一天且确实
    早于估算值时才采信，避免复制/备份把 mtime 刷成未来导致区间被放大。
    """
    en = min(86400.0, st + seg_sec)
    try:
        tm = time.localtime(os.path.getmtime(path))
        if time.strftime("%Y-%m-%d", tm) == day:
            mt = tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec
            if st < mt < en:
                en = float(mt)
    except OSError:
        pass
    return en


def _has_empty_parts(arc_dir):
    """该日产物里是否还有 0 帧空壳（只读，不动文件）。"""
    try:
        names = os.listdir(arc_dir)
    except OSError:
        return False
    for n in names:
        if n.lower().endswith(".mp4") and \
                _fsize(os.path.join(arc_dir, n)) < EMPTY_PART_BYTES:
            return True
    return False


def _purge_empty_parts(arc_dir):
    """清掉历史空壳（0 帧片段）及其 idx 侧车，返回是否清过。

    空壳是「窗口与分片错配」的痕迹：留着既占回放页的黑块，又让 write_split
    因为 idx 已存在而一直跳过重切。清过就必须让该日重做（调用方负责失效 .done）。
    """
    if not os.path.isdir(arc_dir):
        return False
    dropped = 0
    try:
        names = os.listdir(arc_dir)
    except OSError:
        return False
    for n in names:
        if not n.lower().endswith(".mp4"):
            continue
        p = os.path.join(arc_dir, n)
        if _fsize(p) < EMPTY_PART_BYTES:
            _unlink(p)
            dropped += 1
    if not dropped:
        return False
    for n in names:
        if n.lower().endswith(".idx.json"):
            _unlink(os.path.join(arc_dir, n))
    return True


# ---------------------------------------------------------------- 执行

def run(root, cams, cam_cfg, modes, ffmpeg, today=None, seg_sec=300.0,
        batch=120, scan_fn=None, on_log=None, on_step=None):
    """执行归档裁剪。返回结果摘要（与 retention.execute 风格一致）。

    单分片失败只记日志跳过（不写 idx，下次重试），不阻塞同日其它分片、
    不阻塞后续日期。分片总数到 batch 就停 —— 跨夜慢慢消化。

    on_step(step)：每处理完一个分片回调一次，供上层把进度暴露给界面。
    step = {cam,name,date,done,total,used,batch,files,bytes,days_done,days_total}

    幂等跳过的分片（idx 已在）**不消耗 batch 预算**：它只花一次 stat 的时间。
    0.9.102 及以前把它也算一片，于是「点第二次」时预算全被已归档分片吃光，
    真正往前推进的只剩个位数 —— 表现就是点了归档、界面转很久、却几乎没新文件。
    """
    today = today or date.today()
    p = plan(root, cams, cam_cfg, modes, today=today, seg_sec=seg_sec,
             scan_fn=scan_fn)
    log = on_log or (lambda msg: None)
    step_cb = on_step or (lambda step: None)
    archived_days, files, out_bytes = [], 0, 0
    failed, used = [], 0
    for item in p["todo"]:
        if used >= batch:
            break                          # 分批：剩下的下次跑
        dn, d = item["dir"], item["date"]
        day_base = os.path.join(root, dn, d)
        arc = archive_dir(root, dn, d)
        # 分片长度按这一路来（每路可单独覆盖），别用全局值套所有通道
        cam_seg = float(item.get("seg_sec") or seg_sec)
        # 先清历史空壳：它们让 write_split 因为 idx 已存在而重切不了，
        # 也让 .done 把这一日当成已完成。清过就把 .done 作废，整日重做。
        if _purge_empty_parts(arc):
            _unlink(done_path(arc))
            log("archive: %s/%s 清理空壳片段后重做" % (item["cam"], d))
        ev = _ledger_events(os.path.join(root, "_ledger", dn), d)
        segs = {}
        for f in _day_files(day_base):
            st = _src_start(f)
            if st is not None:
                segs[f] = (st, _seg_end(os.path.join(day_base, f), d, st, cam_seg))
        cuts = slice_windows(ev, segs)
        if not cuts:
            if ev:
                # 台账有活动窗口、却和任何一个分片都没有交集：分片缺失/已被清理/
                # 命名不可解析。绝不写 .done —— 写了这天就永久判死，宁可下次再试。
                log("archive: %s/%s 台账 %d 窗口但无可裁分片，跳过（不落 .done）"
                    % (item["cam"], d, len(ev)))
                continue
            # 真·整天静默：没有可裁的画面，直接收尾
            write_done(arc, 0)
            archived_days.append({**item, "files": 0, "empty": True})
            continue
        n_files, day_bytes, day_fail = 0, 0, 0
        planned = len(cuts)
        processed = 0
        for fname, wins in cuts.items():
            if used >= batch:
                break
            src = os.path.join(day_base, fname)
            out_base = os.path.join(arc, fname[:-4])
            r = _split(ffmpeg, src, out_base, wins, arc)
            if r.get("reason") == "exists":
                # 上一轮已经裁好（idx 在）：不再花预算，也不再算产出
                processed += 1
                step_cb({"cam": item["cam"], "name": item.get("name"),
                         "date": d, "done": processed, "total": planned,
                         "used": used, "batch": batch,
                         "files": files + n_files,
                         "bytes": out_bytes + day_bytes,
                         "days_done": len(archived_days),
                         "days_total": len(p["todo"])})
                continue
            used += 1
            processed += 1
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
            step_cb({"cam": item["cam"], "name": item.get("name"),
                     "date": d, "done": processed, "total": planned,
                     "used": used, "batch": batch,
                     "files": files + n_files,
                     "bytes": out_bytes + day_bytes,
                     "days_done": len(archived_days),
                     "days_total": len(p["todo"])})
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
