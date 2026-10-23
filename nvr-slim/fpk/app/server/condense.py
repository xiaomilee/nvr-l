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
import json
import os
import subprocess
import time

import ffmpeg_cmd
import pixdiff


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


def probe_duration(ffprobe, src, timeout=20):
    """读一个文件的真实时长（秒）。读不到返回 None。

    为什么必须读文件而不能算：
    缩时片段是压缩过的成品，一个文件覆盖源录像的 5 分钟，但播放时长可能只有
    十几秒 —— 取决于 speed、prescan 窗口、当天有多少活动区间。前端要是拿
    "分片长度 300 秒"去画缩时的时间轴，界面上会显示两个半小时，实际 3 秒就
    播完，然后疯狂切文件。唯一可靠的来源是文件本身。
    """
    if not ffprobe:
        return None
    cmd = ffmpeg_cmd.probe_cmd(ffprobe, src)
    if not all(isinstance(x, str) and x for x in cmd):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        info = json.loads((r.stdout or b"").decode("utf-8", "replace") or "{}")
    except ValueError:
        return None
    fmt = info.get("format") or {}
    try:
        d = float(fmt.get("duration"))
    except (TypeError, ValueError):
        d = None
    if not d or d <= 0:
        # 有些封装 format.duration 缺失，退到第一条视频流的 duration
        for s in info.get("streams") or []:
            try:
                d = float(s.get("duration"))
            except (TypeError, ValueError):
                continue
            if d and d > 0:
                break
    if not d or d <= 0:
        return None
    return round(d, 3)


def read_packets(ffprobe, src, timeout=180, vf=None):
    """包大小曲线。只过容器，不解码。

    返回 (packets, err)：err 是 ffprobe 的 stderr 尾巴。
    以前这里把 stderr 丢掉、只回一个空列表，结果"读不到包信息"这种
    含糊话看了好几轮才知道是 ffprobe 在报错 —— 失败必须带证据。

    vf 用于检测忽略区：把某块区域单独 crop 出来，读取它自己的包大小曲线。
    """
    for cmd in (ffmpeg_cmd.prescan_cmd(ffprobe, src, vf),
                ffmpeg_cmd.prescan_frame_cmd(ffprobe, src, vf)):
        if not all(isinstance(x, str) and x for x in cmd):
            # ffprobe 没找到时 prescan_cmd 里会拼进 None，subprocess 直接抛
            # TypeError('expected str, bytes or os.PathLike object, not NoneType')，
            # 报错信息完全看不出是"缺 ffprobe"。这里提前说清楚。
            return [], "ffprobe 不可用（未找到可执行文件）"
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


# ---------------------------------------------------------------- 检测忽略区

MASK_COLS, MASK_ROWS = 16, 9      # 与前端 16×9 网格一致，格号 = row*16 + col


def region_filter(cells, cols=MASK_COLS, rows=MASK_ROWS):
    """忽略区格号 → ffmpeg crop 滤镜串（各区域切成独立小视频流）。

    为什么用 crop 而不是 drawbox：判定要的是"**丢掉**这块区域的画面变化"。
    drawbox 只是把像素涂黑，静物在遍历里仍然是一块巨大的零；而树的晃动被涂成
    黑块后反而是"黑块从无到有"，照样触发变化。crop 出去单独统计，才是真忽略。

    为什么不用 maskfun：它对滤镜顺序、像素格式、ffmpeg 版本都挑，且无法逐区域
    统计。crop + 独立的 pkt_size 曲线在"不解码"这条硬约束下最稳。

    相邻格合并成矩形：16×9 里画一片树影常常是十几个格，不合并会生成十几路
    ffprobe，磁盘读放大得离谱。
    """
    cells = sorted(set(int(c) for c in (cells or []) if isinstance(c, int)))
    cells = [c for c in cells if 0 <= c < cols * rows]
    if not cells:
        return ""
    grid = [[False] * cols for _ in range(rows)]
    for c in cells:
        grid[c // cols][c % cols] = True
    rects = _merge_rects(grid, cols, rows)
    parts = []
    for r0, r1, c0, c1 in rects:
        # 宽高取偶数：某些解码器/像素格式对奇数尺寸会报错
        parts.append("crop=w=trunc(iw*%d/%d/2)*2:h=trunc(ih*%d/%d/2)*2:x=iw*%d/%d:y=ih*%d/%d"
                     % (c1 - c0 + 1, cols, r1 - r0 + 1, rows,
                        c0, cols, r0, rows))
    return ",".join(parts)


def cell_rects(cells, cols=MASK_COLS, rows=MASK_ROWS, w=None, h=None):
    """忽略区格号 → 判定画面上的像素矩形 [(x, y, w, h)]（给 drawbox 用）。

    像素差路径和码流曲线路径对忽略区的处理**相反**：字节曲线必须 crop 出去，
    因为涂黑会让那块从无到有地产生大包；像素差则是两帧都涂黑后差分，
    差值恒为 0，涂黑才是正解。所以这里给的是 drawbox 坐标，不是 crop 滤镜。
    """
    cells = sorted(set(int(c) for c in (cells or []) if isinstance(c, int)))
    cells = [c for c in cells if 0 <= c < cols * rows]
    if not cells:
        return []
    w = int(w or pixdiff.W)
    h = int(h or pixdiff.H)
    grid = [[False] * cols for _ in range(rows)]
    for c in cells:
        grid[c // cols][c % cols] = True
    out = []
    for r0, r1, c0, c1 in _merge_rects(grid, cols, rows):
        x = int(round(c0 * w / float(cols)))
        y = int(round(r0 * h / float(rows)))
        rw = int(round((c1 - c0 + 1) * w / float(cols)))
        rh = int(round((r1 - r0 + 1) * h / float(rows)))
        out.append((x, y, max(1, rw), max(1, rh)))
    return out


def _merge_rects(grid, cols, rows):
    """贪心矩形合并：按行优先把能并的行拼成一个矩形。"""
    used = [[False] * cols for _ in range(rows)]
    out = []
    for r in range(rows):
        c = 0
        while c < cols:
            if not grid[r][c] or used[r][c]:
                c += 1
                continue
            c1 = c
            while c1 + 1 < cols and grid[r][c1 + 1] and not used[r][c1 + 1]:
                c1 += 1
            # 向下扩展：整行都必须选中且未用
            r1 = r
            while r1 + 1 < rows and all(grid[r1 + 1][k] and not used[r1 + 1][k]
                                        for k in range(c, c1 + 1)):
                r1 += 1
            for rr in range(r, r1 + 1):
                for k in range(c, c1 + 1):
                    used[rr][k] = True
            out.append((r, r1, c, c1))
            c = c1 + 1
    return out


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
    # 黑白/红外机位的压缩噪声整体比彩色高一档：用户显式选了"黑白"就多给一点
    # 容噪，否则夜间满屏细碎颗粒会被当成连续活动，整天切不出干净片段。
    if str(a.get("scene") or "") == "mono":
        mult *= 1.25
    floor = max(8192.0, base * 1.5)
    return max(base * mult, floor)


PIXEL_FLOOR = 5e-4          # 0.05% 像素（320×180 上约 29 个）


def pixel_threshold(curve, a):
    """像素差门槛：曲线的值已经是「变化像素占比」，所以用占比语义。

    与字节门槛的区别在量纲：字节数没有绝对参照（1080p 和 4MP 差一倍很正常），
    占比有 —— 0.5% 的像素在动，对任何机位都是"确实有事"。

    **零值必须参与分位计算**。只取非零值会犯一个致命的错：一段 5 分钟里只有
    10 秒有人走过时，非零值全是"活动帧"，它们的 20 分位就是活动强度本身，
    再乘倍率之后门槛比活动还高 —— 结果就是"明明有人在走，却判成整段静止"。
    含零后，安静时段占多数时基线自然压到 0，门槛退回绝对地板。
    """
    vals = [v for _, v in curve]
    base = _pct(vals, 0.20) if vals else 0.0
    mult = max(1.2, float(a.get("noise_mult", 2.0)))
    if str(a.get("scene") or "") == "mono":
        mult *= 1.25          # 红外机位噪点多，门槛再放宽一档
    floor = max(PIXEL_FLOOR, float(a.get("floor") or PIXEL_FLOOR))
    return max(base * mult, floor)


def pick_windows(curve, a, duration=None, th=None):
    """把曲线压成区间列表 [(start, end)]。

    merge_gap 合并的是"两段活动之间的短暂安静"（比如人走出去又回来），
    pre/post 补上判定必然存在的滞后，min_clip 去掉一闪而过的碎片。

    th：外部算好的门槛。像素差引擎自己有一套量纲（占比），不能套字节那套
    「20 分位 × 倍率 + 8KB 地板」，所以门槛由调用方给。
    """
    if not curve:
        return []
    if th is None:
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


def _export_one(ffmpeg, src, out, ss, to, timeout=900, speed=1.0):
    # speed>1 时 encode_cmd 会退到重编码（setpts 压时间轴），代价是 CPU，
    # 换来的是"回放时间缩短一半"——这是 0.9.18 起每路可配的选项。
    enc = {"mode": "fast", "faststart": True, "speed": float(speed or 1.0)}
    cmd = ffmpeg_cmd.encode_cmd(ffmpeg, src, out, enc, ss=ss, to=to)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _export_copy(ffmpeg, src, out, ss, to, speed, timeout, str(e))
    if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
        return True, ""
    return _export_copy(ffmpeg, src, out, ss, to, speed, timeout,
                        (r.stderr or b"").decode("utf-8", "replace").strip()[-300:])


def _export_copy(ffmpeg, src, out, ss, to, speed, timeout, err, force=False):
    """倍速重编码失败后的免编码器兜底（itsscale 转封装）。

    speed>1 才需要这层重试：speed==1 的主路径本来就是 -c copy，再跑
    copy_speed_cmd 是同一个命令。但 _export_precise 的回落链是
    QSV→x264→本函数——x264 也挂了（ffmpeg 缺 libx264，LGPL 构建）时
    必须有人接住，所以那种调用带 force=True 无条件执行。0.9.38 真机
    活动切片零输出的另一个元凶就是整条链全落在缺编码器的 ffmpeg 上。
    """
    try:
        sp = float(speed or 1.0)
    except (TypeError, ValueError):
        sp = 1.0
    if sp <= 1.0 and not force:
        return False, err
    cmd = ffmpeg_cmd.copy_speed_cmd(ffmpeg, src, out, ss, to, speed=sp)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
        return True, ""
    return False, ((r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
                   or err)


def concat_cmd(ffmpeg, list_file, out):
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c", "copy", "-fflags", "+genpts",
            "-movflags", "+faststart", "-y", out]


def write_one(ffmpeg, src, out, windows, tmpdir, timeout=900, speed=1.0):
    """把若干区间合成一个文件。

    单区间直接改名，不做二次封装 —— 多一次写盘对机械盘是实打实的浪费。
    多区间先逐段切到临时文件再 concat：看着多写一遍，但都是流复制，
    比用 filter_complex 把整段重编码一遍便宜得多。

    speed：>1 时每个区间在切出时就按倍速重编码（见 ffmpeg_cmd.encode_cmd）。
    倍速必须在**切段时**做而不是切完再整体变速 —— 后者要对成品再读再写一遍，
    机械盘上等于把这条链路的 I/O 翻倍。
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
        ok, err = _export_one(ffmpeg, src, out, ss, to, timeout, speed=speed)
        return {"ok": ok, "reason": "single", "error": err, "parts": 1}

    parts, lines = [], []
    for i, (ss, to) in enumerate(windows):
        p = os.path.join(tmpdir, "part-%02d.mp4" % i)
        ok, err = _export_one(ffmpeg, src, p, ss, to, timeout, speed=speed)
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
        try:
            os.rmdir(tmpdir)            # 目录空了就删掉，别让 var/tmp 永久堆着几千个空目录
        except OSError:
            pass
    return {"ok": ok, "reason": "concat", "error": err, "parts": len(parts)}


def _unlink(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------- 活动切片
#
# 0.9.38：split 模式。与 write_one（整段拼接）并存，同一套判定结果
# res["windows"]，只是导出方式不同：
#   join  多区间 concat 成一个文件（-c copy，关键帧切点，±1 个 GOP）
#   split 每个区间单独成片（重编码，帧级切点），外加一份 .idx.json 侧车
#         记录「片段 ↔ 真实时刻」映射，热力图/跳播按它对齐全天时间轴。

# QSV 失败的负缓存：第一次编不动（会话满/驱动坏/没透传）之后 10 分钟内
# 直接走 libx264，不再每片都白跑一次编码器初始化
_HW_FAIL = {}


def _hw_available(ffmpeg, hw, now=None):
    if hw != "qsv":
        return False
    now = now or time.time()
    bad = _HW_FAIL.get(ffmpeg)
    if bad and now - bad < 600:
        return False
    return True


def _hw_fail(ffmpeg):
    _HW_FAIL[ffmpeg] = time.time()


def _export_precise(ffmpeg, src, out, ss, to, hw, speed=1.0, timeout=900):
    """单区间精确导出：QSV → libx264 → 转封装 三级回落。

    第三级是兜底里的兜底：软编也失败（ffmpeg 缺 libx264、盘满）时，
    用 itsscale + -c copy 把片段交出来——不经过任何编码器，任何 ffmpeg
    都能跑；切点退回关键帧，粗一点总比没有强，错误原样带回去。
    """
    # 入口时硬编可用才尝试 QSV；失败立刻进负缓存，本轮直接落到软编
    attempts = [("qsv", True)] if _hw_available(ffmpeg, hw) else []
    attempts.append(("x264", False))
    err = ""
    for codec, use_hw in attempts:
        cmd = ffmpeg_cmd.precise_cmd(ffmpeg, src, out, ss, to,
                                     hw="qsv" if use_hw else "none",
                                     speed=speed)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            err = str(e)
            continue
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return True, "", codec
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        if use_hw:
            _hw_fail(ffmpeg)          # 记下失败，本进程内一段时间不再试硬编
    # 终极兜底：转封装 + 时间戳缩放（无编码器依赖，任何 ffmpeg 都能跑）
    ok, err2 = _export_copy(ffmpeg, src, out, ss, to, speed, timeout, err,
                            force=True)
    return ok, err2, "copy"


def write_split(ffmpeg, src, out_base, windows, tmpdir,
                hw="qsv", timeout=900, speed=1.0):
    """把若干区间各自切成独立片段 + 侧车索引。

    out_base：不带扩展名的输出前缀（…/_clip/<cam>/<date>/<HH-MM-SS>），
    产物 <out_base>.c01.mp4、.c02.mp4… 与 <out_base>.idx.json。
    片段名带 .cNN 后缀且按序号排序后仍按时间升序，回放页现有的
    目录扫描不用改一行就能按正确的先后次序列出来。

    幂等：idx 已存在就不再重切（item_key 的参数指纹管"该不该重跑"，
    这里只管"别把切好的覆盖掉"）。
    """
    windows = list(windows or [])
    if not windows:
        return {"ok": False, "reason": "no-window"}
    out_dir = os.path.dirname(out_base) or "."
    idx_path = out_base + ".idx.json"
    os.makedirs(out_dir, exist_ok=True)
    if os.path.isfile(idx_path):
        return {"ok": True, "reason": "exists", "parts": 0, "files": []}

    speed = max(1.0, float(speed or 1.0))
    files, entries = [], []
    for i, (ss, to) in enumerate(windows):
        out = "%s.c%02d.mp4" % (out_base, i + 1)
        ok, err, codec = _export_precise(ffmpeg, src, out, ss, to, hw,
                                         speed=speed, timeout=timeout)
        if not ok:
            # 半途失败：已写出的片段一并清掉，别留半套产物让回放页猜
            for q in files:
                _unlink(q)
            _unlink(idx_path)
            return {"ok": False, "reason": "part-failed", "error": err, "index": i}
        files.append(out)
        entries.append({"i": i + 1, "file": os.path.basename(out),
                        "ss": round(float(ss), 3), "to": round(float(to), 3),
                        "codec": codec})
    meta = {"src": os.path.basename(src),
            "speed": speed,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "windows": entries}
    try:
        tmp = idx_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, idx_path)
    except OSError as e:
        for q in files:
            _unlink(q)
        return {"ok": False, "reason": "idx", "error": str(e)}
    return {"ok": True, "reason": "split", "parts": len(files),
            "files": files, "codecs": [e["codec"] for e in entries]}


# ---------------------------------------------------------------- 编排

def analyze(ffprobe, src, a, timeout=180, ignore=None, ffmpeg=None):
    """一次判定：返回区间列表和整段是否基本静默。

    两种引擎，由 a["engine"] 决定（auto = 能用像素就用像素）：
      pixel  逐像素差：真正"剔除静止画面"，能抓到码流曲线漏掉的小变化，
             代价是要解码。用预筛把解码限制在候选区间。
      bytes  码流曲线：只读容器不解码，最省；但码率策略会骗人
             （静止画面遇到 I 帧照样一大包），且忽略区只能靠 crop 扣减。

    降级规则：像素引擎要的滤镜缺失、或这次跑不出曲线，就**整段**回落到
    码流曲线，并在 engine_note 里写明原因 —— 宁可慢一点、判得粗一点，
    也不能因为一个滤镜不可用就把有内容的片段当静止丢掉。

    ignore：检测忽略区的格号列表（16×9，格号 = row*16+col）。
    """
    engine = str(a.get("engine") or "auto").strip().lower()
    note = ""
    if engine in ("pixel", "auto"):
        if not ffmpeg:
            note = "像素判读不可用（未找到 ffmpeg），已回落到码流曲线"
        else:
            ok, why = pixdiff.available(ffmpeg)
            if not ok:
                note = "像素判读不可用（%s），已回落到码流曲线" % why
            else:
                res = _pixel_analyze(ffmpeg, ffprobe, src, a, ignore, timeout)
                if res.get("ok"):
                    return res
                note = "像素判读失败（%s），已回落到码流曲线" % (res.get("error") or "无输出")
    res = _bytes_analyze(ffprobe, src, a, timeout, ignore)
    res["engine"] = "bytes"
    res["engine_note"] = note
    return res


def _pixel_analyze(ffmpeg, ffprobe, src, a, ignore, timeout):
    """像素差路径。先用码流曲线预筛候选区间，只对这些区间解码。"""
    rects = cell_rects(ignore) if ignore else []
    margin = max(0.0, float(a.get("prescan_margin", 10)))
    prescan = bool(a.get("prescan", True))
    duration = None
    ranges = [(None, None)]
    scanned_full = True
    fps_used = a.get("fps", 2.0)
    note = ""

    if prescan:
        packets, _err = read_packets(ffprobe, src, timeout)
        if packets:
            duration = packets[-1][0]
            # 预筛只负责"别漏"，所以门槛放低：min_clip 降到 1 秒，
            # 否则 2 秒的小事件会在预筛阶段就被当碎片滤掉，像素判读根本看不到。
            cands = pick_windows(activity_curve(packets),
                                 dict(a, min_clip=1.0), duration)
            if cands:
                ranges = [(max(0.0, s - margin), (e - s) + 2 * margin)
                          for s, e in cands]
                scanned_full = False
            else:
                # 预筛一个候选都没有：码流曲线上连 I 帧都没抖过。这时仍然
                # 全段扫一遍像素，但把抽样帧率压到 1 fps —— 这些段落本来就是
                # 静态的代价，压一半成本；真有小变化也还在 1 秒粒度上抓得住。
                fps_used = min(_float(fps_used, 2.0), 1.0)
                note = "预筛无候选，全段像素判读（抽样 1 帧/秒）"
        else:
            # 连包信息都读不到（文件可能还在写）：全段像素判读，别下结论
            note = "预筛读不到包信息，全段像素判读"

    a2 = dict(a)
    a2["fps"] = fps_used
    run = pixdiff.curve(ffmpeg, src, a2, rects=rects, ranges=ranges,
                        timeout=max(timeout, 300))
    if not run["ok"]:
        return {"ok": False, "error": run.get("error") or "像素判读无输出"}

    curve = run["curve"]
    if not curve:
        return {"ok": False, "error": "像素判读无输出"}
    if duration is None:
        duration = curve[-1][0] + 1.0
    th = pixel_threshold(curve, a)
    windows = pick_windows(curve, a, duration, th=th)
    covered = sum(max(0.0, e - s) for s, e in windows)
    return {
        "ok": True,
        "windows": windows,
        "packets": len(curve),
        "frames": len(curve),
        "duration": round(duration or 0, 2),
        "bytes": 0,
        "threshold": round(th, 6),
        "active_ratio": round(covered / duration, 3) if duration else 0.0,
        "silent": not windows,
        "ignored_regions": len(rects),
        "error": "",
        "engine": "pixel",
        "engine_note": note or ("" if not scanned_full else "全段像素判读"),
        "prescan_ranges": len(ranges) if not scanned_full else 0,
    }


def _float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _bytes_analyze(ffprobe, src, a, timeout=180, ignore=None):
    """码流曲线路径（原实现，保持行为不变以便对照与降级）。"""
    packets, err = read_packets(ffprobe, src, timeout)
    if not packets:
        return {"windows": [], "packets": 0, "silent": None,
                "error": "读不到包信息" + ("（%s）" % err if err else
                                           "（文件可能损坏或仍在写入）")}
    curve = activity_curve(packets)
    ignored = 0
    if ignore:
        vf = region_filter(ignore)
        if vf:
            curve, ignored = _subtract_regions(ffprobe, src, curve, vf, timeout)
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
        "ignored_regions": ignored,
        "error": "",
    }


def _subtract_regions(ffprobe, src, curve, vf, timeout):
    """从整帧曲线里减掉忽略区的贡献。返回 (新曲线, 合并后的区域数)。"""
    pts, err = read_packets(ffprobe, src, timeout, vf=vf)
    if err or not pts:
        # 区域读不出来就**保持原曲线**（宁可多报事件，也不要漏掉真事件）
        return curve, 0
    sub = activity_curve(pts)
    regions = vf.count("crop=") or 1
    # 区域曲线可能比整帧短（末帧时间戳差异），按时间对齐补齐
    by_t = {}
    for t, b in sub:
        by_t[round(t, 3)] = by_t.get(round(t, 3), 0) + b
    out = []
    for t, b in curve:
        out.append((t, max(0.0, b - by_t.get(round(t, 3), 0))))
    return out, regions
