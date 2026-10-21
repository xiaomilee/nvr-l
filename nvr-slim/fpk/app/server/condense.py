#!/usr/bin/env python3
"""分片判定与导出：从已封口的分片里挑出「有变化」的区间。

激进方案的成本控制核心是 **不解码、也不重编码**：

  判定   ffprobe 只读容器里的包大小（pkt_size），完全不碰像素。
         H.264 静止场景几乎不产生数据，有人走动帧突然变大 ——
         这条曲线本身就是最灵敏的"有没有事"的信号，成本只有磁盘顺序读。
  导出   活动区间用 -c copy 切出来，关键帧对齐，画质零损失。

代价有两个，都要认：
1. 切点只能落在关键帧上。GOP 2~4 秒时起点误差可达 2~4 秒，
   所以 pre_buf / post_buf 必须给够（默认 3 秒，正是为此）。
2. 不做降帧、去噪、降分辨率。要那套效果就切到 fine 模式重编码，
   成本高一个数量级，不适合"边录边压"。

为什么阈值用「20 分位 × 倍率」而不是中位数：
一段 5 分钟里有人走动时包会暴涨，中位数会被活动段拉高，阈值跟着漂，
结果是人越多越判不出来。20 分位取的是"这段里最安静那部分"的水平，
更接近真正的基线。
"""
import os
import subprocess

import ffmpeg_cmd


# ---------------------------------------------------------------- 读曲线

def parse_probe_output(text):
    """把 ffprobe 的输出解析成 [(pts_time, size)]。

    两条路都认：
      1. `compact=p=0:nk=0` → `size=1234|pts_time=0.000000`（带字段名，最稳）
      2. 老式 csv（无字段名）→ 逐字段猜数字，猜不出就跳过这一行
    字段名不赌顺序：ffprobe 的 csv 按它内部的字段定义顺序输出，不是你
    在 -show_entries 里写的顺序 —— 靠位置取值迟早踩坑。
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        size = t = None
        if "=" in line:
            for kv in line.split("|"):
                k, _, v = kv.partition("=")
                k, v = k.strip(), v.strip()
                if k in ("size", "pkt_size"):
                    try:
                        size = int(v)
                    except ValueError:
                        size = None
                elif k in ("pts_time", "pkt_pts_time", "best_effort_timestamp_time"):
                    if t is None:
                        try:
                            t = float(v)
                        except ValueError:
                            pass
        elif "," in line:
            for p in line.split(","):
                p = p.strip()
                if not p or p == "N/A":
                    continue
                if size is None:
                    try:
                        size = int(p)
                        continue
                    except ValueError:
                        pass
                if t is None:
                    try:
                        t = float(p)
                        continue
                    except ValueError:
                        pass
        if size is None or t is None:
            continue
        out.append((t, size))
    out.sort(key=lambda x: x[0])
    return out


def read_packets(ffprobe, src, timeout=180):
    """包大小曲线。只过容器，不解码。

    返回 (packets, err)：err 是 ffprobe 的 stderr 尾巴。
    以前这里把 stderr 丢掉、只回一个空列表，结果"读不到包信息"这种
    含糊话看了好几轮才知道是 ffprobe 在报错 —— 失败必须带证据。
    """
    for cmd in (ffmpeg_cmd.prescan_cmd(ffprobe, src),
                ffmpeg_cmd.prescan_frame_cmd(ffprobe, src)):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return [], str(e)[:200]
        packets = parse_probe_output(r.stdout.decode("utf-8", "replace"))
        if packets:
            return packets, ""
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        if err:
            return [], err.splitlines()[-1][:200]
    return [], ""


def activity_curve(packets, bin_sec=1.0):
    """按秒聚合字节数，返回 [(t, bytes)]，t 为该秒起点。"""
    if not packets:
        return []
    buckets = {}
    for t, size in packets:
        k = int(t // bin_sec)
        buckets[k] = buckets.get(k, 0) + size
    lo = min(buckets)
    hi = max(buckets)
    # 补齐空洞：摄像头断流时中间没有包，留 0 会被当成"静止"，
    # 补成 0 反而更保守 —— 断流段本来就不该算活动
    return [(i * bin_sec, buckets.get(i, 0)) for i in range(lo, hi + 1)]


# ---------------------------------------------------------------- 判定

def _pct(values, p):
    if not values:
        return 0
    s = sorted(values)
    i = int(round((len(s) - 1) * p))
    return s[max(0, min(len(s) - 1, i))]


def threshold(curve, a):
    """活动判定的字节门槛。

    双下限：既不能低于基线的 mult 倍，也不能低于一个绝对地板。
    只用比例会在"整段全静"时把噪声当活动；只用绝对值又管不住不同码率的机位。
    """
    sizes = [b for _, b in curve if b > 0]
    if not sizes:
        return 0
    base = _pct(sizes, 0.20)
    mult = max(1.2, float(a.get("noise_mult", 2.0)))
    floor = max(8192.0, base * 1.5)
    return max(base * mult, floor)


def pick_windows(curve, a, duration=None):
    """把曲线压成区间列表 [(start, end)]。

    merge_gap 合并的是"两段活动之间的短暂安静"（比如人走出去又回来），
    pre/post 补上判定必然存在的滞后，min_clip 去掉一闪而过的碎片。
    """
    if not curve:
        return []
    th = threshold(curve, a)
    flag = [(t, b > th) for t, b in curve]

    segs = []
    start = None
    for t, on in flag:
        if on and start is None:
            start = t
        elif not on and start is not None:
            segs.append([start, t])
            start = None
    if start is not None:
        segs.append([start, curve[-1][0] + 1.0])
    if not segs:
        return []

    gap = max(0.0, float(a.get("merge_gap", 5.0)))
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] <= gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)

    pre = max(0.0, float(a.get("pre_buf", 3.0)))
    post = max(0.0, float(a.get("post_buf", 3.0)))
    ext = [[max(0.0, s - pre), e + post] for s, e in merged]

    if duration:
        ext = [[min(s, duration), min(e, duration)] for s, e in ext]

    out = []
    for s, e in ext:
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])

    min_len = max(0.5, float(a.get("min_clip", 3.0)))
    return [(round(s, 2), round(e, 2)) for s, e in out if e - s >= min_len]


# ---------------------------------------------------------------- 导出

def clip_path(root, cam_id, src, clip_root="_clip"):
    """输出路径镜像源路径：<root>/_clip/<cam>/<date>/<同名>.mp4

    与 retention 的 KIND_DIR 约定一致，这样保留策略不用为缩时片段写第二套逻辑。
    """
    rel = os.path.relpath(src, os.path.join(root, cam_id))
    return os.path.join(root, clip_root, cam_id, rel)


def _export_one(ffmpeg, src, out, ss, to, timeout=900):
    enc = {"mode": "fast", "faststart": True}
    cmd = ffmpeg_cmd.encode_cmd(ffmpeg, src, out, enc, ss=ss, to=to)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0 or not os.path.isfile(out):
        return False, (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
    return True, ""


def concat_cmd(ffmpeg, list_file, out):
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c", "copy", "-fflags", "+genpts",
            "-movflags", "+faststart", "-y", out]


def write_one(ffmpeg, src, out, windows, tmpdir, timeout=900):
    """把若干区间合成一个文件。

    单区间直接改名，不做二次封装 —— 多一次写盘对机械盘是实打实的浪费。
    多区间先逐段切到临时文件再 concat：看着多写一遍，但都是流复制，
    比用 filter_complex 把整段重编码一遍便宜得多。
    """
    windows = list(windows or [])
    if not windows:
        return {"ok": False, "reason": "no-window"}
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return {"ok": True, "reason": "exists"}

    os.makedirs(tmpdir, exist_ok=True)

    if len(windows) == 1:
        ss, to = windows[0]
        ok, err = _export_one(ffmpeg, src, out, ss, to, timeout)
        return {"ok": ok, "reason": "single", "error": err, "parts": 1}

    parts, lines = [], []
    for i, (ss, to) in enumerate(windows):
        p = os.path.join(tmpdir, "part-%02d.mp4" % i)
        ok, err = _export_one(ffmpeg, src, p, ss, to, timeout)
        if not ok:
            for q in parts:
                _unlink(q)
            return {"ok": False, "reason": "part-failed", "error": err, "index": i}
        parts.append(p)
        lines.append("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))

    lst = os.path.join(tmpdir, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(lines)
    try:
        r = subprocess.run(concat_cmd(ffmpeg, lst, out), capture_output=True, timeout=timeout)
        ok = r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
        err = "" if ok else (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, err = False, str(e)
    finally:
        for q in parts:
            _unlink(q)
        _unlink(lst)
    return {"ok": ok, "reason": "concat", "error": err, "parts": len(parts)}


def _unlink(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------- 编排

def analyze(ffprobe, src, a, timeout=180):
    """一次判定：返回区间列表和整段是否基本静默。"""
    packets, err = read_packets(ffprobe, src, timeout)
    if not packets:
        return {"windows": [], "packets": 0, "silent": None,
                "error": "读不到包信息" + ("（%s）" % err if err else
                                           "（文件可能损坏或仍在写入）")}
    curve = activity_curve(packets)
    duration = packets[-1][0] if packets else None
    windows = pick_windows(curve, a, duration)
    total = sum(b for _, b in curve) or 1
    covered = 0.0
    for s, e in windows:
        covered += max(0.0, e - s)
    return {
        "windows": windows,
        "packets": len(packets),
        "duration": round(duration or 0, 2),
        "bytes": total,
        "threshold": int(threshold(curve, a)),
        "active_ratio": round(covered / duration, 3) if duration else 0.0,
        "silent": not windows,
        "error": "",
    }
