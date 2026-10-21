#!/usr/bin/env python3
"""保留策略与定时清理。

核心决定：**按日期整目录删，每天固定时刻删一次**，不做"倒数 72 小时"的滚动窗口。

为什么不用滚动窗口：
1. 磁盘曲线是斜的 —— 用户永远不知道"现在还剩几天"，也不知道今晚会不会满。
2. 删除发生在任意时刻，可能在有人正回看某段时把文件抽走。
3. 和压缩管线抢同一块盘：删一半、压一半，I/O 互相踩。

按日期目录删的好处是三个可预测：用量可预测（每天固定量）、删除量可预测
（正好一天的量）、时间可预测（凌晨固定时刻）。代价是当天多留一点空间。

每个摄像头两类数据各有各的周期：
    全时录像   <root>/<cam>/<YYYY-MM-DD>/*.mp4     默认 4 天（体积大）
    缩时片段   <root>/_clip/<cam>/<YYYY-MM-DD>/*.mp4  默认 90 天（体积小）

"保留 N 天" = 今天 + 前 N-1 天。所以 N=1 表示只留今天。
"""
import os
import shutil
from datetime import date, datetime, timedelta

DEFAULTS = {
    "cleanup_at": "03:00",     # 每天几点清理（本地时间）
    "full_days": 4,            # 每路全时录像保留天数（可被摄像头级覆盖）
    "clip_days": 90,           # 每路缩时片段保留天数
}

KIND_DIR = {"full": "", "clip": "_clip"}
KIND_LABEL = {"full": "全时录像", "clip": "缩时片段"}
KIND_DAYS_KEY = {"full": "full_days", "clip": "clip_days"}

LIMITS = {"full_days": (1, 30), "clip_days": (1, 3650)}


# ---------------------------------------------------------------- 时间

def parse_day(name):
    """目录名必须是 YYYY-MM-DD，其它一律不碰（防止误删 _clip 之类的结构目录）。"""
    try:
        return datetime.strptime(name, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def cutoff(today, days):
    """保留 N 天 → 早于这个日期的目录要被删。"""
    days = max(1, int(days))
    return today - timedelta(days=days - 1)


def clamp_days(key, value):
    lo, hi = LIMITS.get(key, (1, 3650))
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = DEFAULTS.get(key, lo)
    return max(lo, min(hi, v))


def parse_hhmm(text, default="03:00"):
    try:
        h, m = str(text).split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return "%02d:%02d" % (h, m)
    except (ValueError, TypeError, AttributeError):
        pass
    return default


def next_run(cleanup_at, now=None):
    """下一次清理时间（今天还没到就今天，否则明天）。"""
    now = now or datetime.now()
    hh, mm = [int(x) for x in parse_hhmm(cleanup_at).split(":")]
    at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return at


def due(last_run_day, cleanup_at, now=None):
    """到点了吗：今天已过清理时刻，且今天还没跑过。"""
    now = now or datetime.now()
    hh, mm = [int(x) for x in parse_hhmm(cleanup_at).split(":")]
    at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < at:
        return False
    return last_run_day != now.date().isoformat()


# ---------------------------------------------------------------- 计划

def camera_retention(cam, cam_cfg):
    c = cam_cfg or {}
    return {
        "full_days": clamp_days("full_days", c.get("full_days", DEFAULTS["full_days"])),
        "clip_days": clamp_days("clip_days", c.get("clip_days", DEFAULTS["clip_days"])),
    }


def _dir_stats(path):
    size = 0
    files = 0
    protected = []
    for root, _dirs, names in os.walk(path):
        for n in names:
            fp = os.path.join(root, n)
            try:
                size += os.path.getsize(fp)
                files += 1
            except OSError:
                pass
            if n.endswith(".part") or n.endswith(".pending"):
                protected.append(fp)
    return size, files, protected


def plan(root, cameras, cam_cfg=None, today=None, kinds=("full", "clip")):
    """算一遍要删什么，不真删。返回的结构直接给 UI 预览用。

    三条硬保护：
      - 今天与未来的目录永不进删除列表
      - 名字不是 YYYY-MM-DD 的目录一律跳过
      - 目录里有 .part / .pending（正在写或待压缩）就跳过并报出来
    """
    today = today or date.today()
    cam_cfg = cam_cfg or {}
    out = {"date": today.isoformat(), "delete": [], "protected": [],
           "unknown": [], "freed_bytes": 0, "files": 0, "policy": []}

    for cam in cameras:
        cid = cam.get("id") or cam.get("name")
        if not cid:
            continue
        ret = camera_retention(cam, cam_cfg.get(cid))
        out["policy"].append({"cam": cid, "name": cam.get("name", cid), **ret})

        # 新目录（摄像头名称）+ 旧 id 目录：升级前的存量数据也要纳入清理
        dirs = list(dict.fromkeys([cam.get("dir_name") or cid, cid]))
        for kind in kinds:
            days = ret[KIND_DAYS_KEY[kind]]
            cut = cutoff(today, days)
            for dname in dirs:
                base = os.path.join(root, KIND_DIR[kind], dname) if KIND_DIR[kind] \
                    else os.path.join(root, dname)
                if not os.path.isdir(base):
                    continue
                for name in sorted(os.listdir(base)):
                    full = os.path.join(base, name)
                    if not os.path.isdir(full):
                        continue
                    d = parse_day(name)
                    if d is None:
                        out["unknown"].append(full)
                        continue
                    if d >= today:          # 今天和未来永不删
                        continue
                    if d >= cut:            # 还在保留期内
                        continue
                    size, files, prot = _dir_stats(full)
                    item = {
                        "path": full, "cam": cid, "name": cam.get("name", cid),
                        "kind": kind, "kind_label": KIND_LABEL[kind],
                        "date": d.isoformat(), "age_days": (today - d).days,
                        "keep_days": days, "bytes": size, "files": files,
                        # 指纹 = 目录名 + 文件数 + 总字节。用户看到的清单和真正要删的
                        # 必须是同一份：中间要是又写进来新文件，指纹就变，删除时跳过。
                        "fp": "%s:%d:%d" % (d.isoformat(), files, size),
                    }
                    if prot:
                        item["reason"] = "%d 个文件正在写或待处理" % len(prot)
                        out["protected"].append(item)
                        continue
                    out["delete"].append(item)
                    out["freed_bytes"] += size
                    out["files"] += files
    out["delete"].sort(key=lambda x: (x["cam"], x["kind"], x["date"]))
    return out


def execute(plan_obj, dry_run=False, on_item=None, only=None):
    """按计划删。整目录删（先文件后 rmdir），单个失败不影响其它目录。

    only: 可选的 {path: fp} 白名单。传了就只删清单里出现、且指纹没变的目录 ——
    用户在预览里看到的是哪几个，删的就是哪几个；期间新写进来的目录不会被顺手删掉。
    """
    done, failed, freed, files, skipped = [], [], 0, 0, []
    for item in plan_obj.get("delete", []):
        path = item["path"]
        if only is not None:
            want_fp = only.get(path)
            if want_fp is None:
                continue                       # 不在用户确认过的清单里
            if want_fp != item.get("fp"):
                skipped.append({**item, "error": "预览后目录内容已变化，已跳过"})
                continue
        if dry_run:
            done.append(item)
            freed += item.get("bytes", 0)
            files += item.get("files", 0)
            continue
        try:
            # 再检查一次：计划生成后可能有进程刚写了 .part 进来
            _size, _files, prot = _dir_stats(path)
            if prot:
                failed.append({**item, "error": "目录内出现 .part/.pending，已跳过"})
                continue
            shutil.rmtree(path)
            done.append(item)
            freed += item.get("bytes", 0)
            files += item.get("files", 0)
            if on_item:
                on_item(item)
        except OSError as e:
            failed.append({**item, "error": str(e)})
    return {
        "ok": not failed,
        "dry_run": bool(dry_run),
        "deleted": done,
        "failed": failed,
        "skipped": skipped,
        "freed_bytes": freed,
        "files": files,
        "freed_mb": round(freed / 1024.0 / 1024.0, 1),
    }


def pretty_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


if __name__ == "__main__":
    import json
    today = date(2026, 10, 20)
    print("保留 4 天 → 早于", cutoff(today, 4), "的目录要删")
    print("下次清理:", next_run("03:00", datetime(2026, 10, 20, 18, 0)).isoformat())
    print("18:00 到点?", due("2026-10-19", "03:00", datetime(2026, 10, 20, 18, 0)))
    print("03:00 到点且今天没跑?", due("2026-10-19", "03:00", datetime(2026, 10, 20, 3, 1)))
    print("今天已跑过?", due("2026-10-20", "03:00", datetime(2026, 10, 20, 3, 1)))
    print(json.dumps({"ok": True}, ensure_ascii=False))
