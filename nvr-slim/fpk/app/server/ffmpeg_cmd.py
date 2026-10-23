#!/usr/bin/env python3
"""ffmpeg / ffprobe 命令构建。

所有命令都在这里拼，UI 与调度只传参数对象，不拼字符串 —— 与 Windows 版
"界面层只调函数、不直接拼 ffmpeg" 的约定一致，测试只需测这一个文件。

三条链路：
    分析（analyze）  2fps / 320 宽 / 灰度 rawvideo 管道，中间帧绝不落盘
    预筛（prescan）  ffprobe 读 pkt_size 曲线，不解码，只受磁盘 I/O 限制
    导出（encode）   fast=流复制（关键帧对齐） | fine=QSV ICQ / libx265 重编码
"""
import json


# ---------------------------------------------------------------- 基础

def _head(ffmpeg, quiet=True):
    cmd = [ffmpeg]
    if quiet:
        cmd += ["-hide_banner", "-nostdin", "-v", "error"]
    return cmd


def _even(n):
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


# ---------------------------------------------------------------- 实时预览流

def live_stream_cmd(ffmpeg, url, fps=8, width=960, transport="tcp"):
    """MJPEG 实时预览：RTSP → mpjpeg 管道，给浏览器 <img> 直接吃。

    三条是硬要求，缺一条就会表现成"画面永远停在等待第一帧"：

    - **-an**：mpjpeg 只装视频流。子码流一旦带 G.711/G.726 音轨，ffmpeg 在
      写文件头阶段就直接失败退出，浏览器收到的是空响应。
    - **fps / width 必须可调**：宫格要 4 路以上同时播，若沿用单画面的
      8 帧 960 宽，一台弱 CPU 的 NAS 立刻被四份解码 + 编码拖住。前端按场景给。
    - **-fflags nobuffer**：不加的话 ffmpeg 默认预读缓冲，移动物体要晚 2~3 秒
      才出现，看起来就像"卡在几秒前的画面"。
    """
    try:
        fps = int(fps)
    except (TypeError, ValueError):
        fps = 8
    fps = max(1, min(15, fps))
    try:
        width = int(width)
    except (TypeError, ValueError):
        width = 960
    width = max(0, min(1920, width))
    vf = "fps=%d" % fps
    if width:
        vf += ",scale=%d:-2" % _even(width)
    return ([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
             "-fflags", "nobuffer", "-flags", "low_delay",
             "-rtsp_transport", transport,
             "-i", url,
             "-an", "-vf", vf, "-c:v", "mjpeg", "-q:v", "6",
             "-f", "mpjpeg", "-boundary_tag", "nvrframe", "pipe:1"])


# ---------------------------------------------------------------- 分析

def analysis_cmd(ffmpeg, src, a, hw="qsv", channel="luma", aspect=16 / 9.0):
    """抽帧打分用的命令：输出 rawvideo 到 stdout。

    hw="qsv" 时**必须**带 -hwaccel_output_format qsv，并且缩放用 vpp_qsv 在
    显存里完成，最后才 hwdownload。漏写 output_format，ffmpeg 会把每一帧
    （4MP 的 YUV420 约 8.3MB）拉回系统内存，CPU 占用直接翻倍 ——
    这是同类工具"开了硬解还是卡"的头号原因。

    channel="luma" 输出灰度；"chroma" 只输出 U 平面（彩色画面第二条判据）。
    """
    w = int(a.get("width", 320))
    fps = float(a.get("fps", 2.0))
    accent = []
    if a.get("temporal_median"):
        # 时间中值：只建议纯静态场景 + 雨雪树影时开，会削弱快速小目标
        accent.append("tmix=frames=%d" % int(a["temporal_median"]))
    if channel == "chroma":
        accent.append("extractplanes=u")

    cmd = _head(ffmpeg)
    if hw == "qsv":
        h = _even(w / float(aspect or 16 / 9.0))
        chain = ["vpp_qsv=w=%d:h=%d" % (w, h), "hwdownload", "format=nv12"]
        # 空域模糊放在缩放之后：小图上做，成本可以忽略
        if a.get("blur"):
            chain.append("boxblur=%d:1" % int(a["blur"]))
        chain.append("fps=%s" % _num(fps))
        chain += accent
        chain.append("format=gray")
        cmd += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
        cmd += ["-i", src, "-vf", ",".join(chain)]
    else:
        chain = ["fps=%s" % _num(fps), "scale=%d:-2" % w]
        if a.get("blur"):
            chain.append("boxblur=%d:1" % int(a["blur"]))
        chain += accent
        chain.append("format=gray")
        cmd += ["-i", src, "-vf", ",".join(chain)]

    cmd += ["-an", "-sn", "-dn", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    return cmd


def prescan_cmd(ffprobe, src, vf=None):
    """包大小预筛：包里字节数天然反映画面变化量，**不解码**。

    `packet=` 是容器层信息，不走解码器 —— 比 `frame=` 快一个数量级，
    也不依赖这颗 ffmpeg 编进了解码器（HEVC 的解码器缺失时 `frame=` 会
    一行都吐不出来，只剩 stderr 里的报错）。

    输出用 `compact=p=0:nk=0`（`size=1234|pts_time=0.000000`）：
    带字段名，解析不必赌 ffprobe 的字段顺序。

    vf：检测忽略区的 crop 链。给了就只统计**区域之外**那句话是错的 ——
    crop 出来的是区域**内部**，那是我们要丢掉的，所以调用方算的是
    「整帧 − 忽略区」= 全帧曲线减去各区域曲线之和（见 condense.analyze）。
    """
    cmd = [ffprobe]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=size,pts_time",
            "-of", "compact=p=0:nk=0", src]
    return cmd


def prescan_frame_cmd(ffprobe, src, vf=None):
    """兜底：老版本 ffprobe 不认 `packet=` 时退回 `frame=` 包大小。"""
    cmd = [ffprobe]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=pkt_size,pts_time",
            "-of", "compact=p=0:nk=0", src]
    return cmd


def probe_cmd(ffprobe, src):
    return [ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", src]


def redact_filter(cells, cols=16, rows=9):
    """遮蔽区格号 → drawbox 滤镜串（保留给导出/测试用）。

    **录像与快照链路已不再调用它**。"输出遮蔽区"这个功能整体移除了：
    在录制阶段打黑块必须放弃 -c copy 改走重编码，每路常驻一个编码器，
    CPU 直接翻倍；而它真正要解决的问题（某块区域别算成事件）属于
    「检测忽略区」，在判定阶段处理即可，与写盘无关。

    仍留着这个函数是因为：手动导出、离线排查时仍可能想给一段画面打码。
    """
    cells = [c for c in (cells or []) if isinstance(c, int)]
    if not cells:
        return ""
    boxes = []
    for c in cells:
        row, col = divmod(c, cols)
        if row >= rows:
            continue
        boxes.append(
            "drawbox=x=iw/%d*%d:y=ih/%d*%d:w=iw/%d:h=ih/%d:color=black:t=fill"
            % (cols, col, rows, row, cols, rows))
    return ",".join(boxes)


# ---------------------------------------------------------------- 导出

def encode_cmd(ffmpeg, src, out, enc, hw="qsv", night=False, ss=None, to=None):
    """导出保留段。

    mode="fast"：`-c copy`，速度约 8 倍实时、无损，但边界只能落关键帧
                 （GOP 2s 时误差可达 2~4s）。**画质参数在这种模式下全部失效。**
    mode="fine"：重编码，帧级精确。QSV 用 -global_quality（ICQ）不是 -crf。
    enc["speed"]>1：输出整体加速 N 倍（2 就是时间缩短一半）。流复制改不了
                 时间戳，所以倍速必须走重编码：setpts 压时间轴 + fps 限帧，
                 体积也大约按 1/N 缩小。fine 模式同样把 setpts 插到滤镜链最前。
    """
    mode = enc.get("mode", "fine")
    try:
        speed = float(enc.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    if speed < 1.0:
        speed = 1.0
    cmd = _head(ffmpeg)
    if ss is not None:
        cmd += ["-ss", _num(ss)]
    if to is not None:
        cmd += ["-to", _num(to)]

    if speed > 1.0 and mode == "fast":
        # 倍速 + 流复制是矛盾的：copy 连时间戳都不碰。这里退到软件重编码
        #（libx264 veryfast：任何一台 NAS 都编得动，画质对监控画面足够）。
        # 帧率封顶 spdup_fps（默认 15）：2 倍速的 25fps 源会变成 50fps，
        # 不封顶的话文件反而更大，倍速就白做了。
        fps_cap = max(5, min(60, int(enc.get("spdup_fps") or 15)))
        filt = "setpts=PTS/%s,fps=%d" % (_num(speed), fps_cap)
        cmd += ["-i", src, "-vf", filt, "-map", "0:v:0", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]
        if enc.get("faststart", True):
            cmd += ["-movflags", "+faststart"]
        cmd += ["-y", out]
        return cmd

    if mode == "fast":
        cmd += ["-i", src, "-c", "copy", "-avoid_negative_ts", "make_zero"]
        if enc.get("faststart", True):
            cmd += ["-movflags", "+faststart"]
        cmd += ["-map", "0:v:0", "-an", out]
        return cmd

    prof = enc.get("night" if night else "day", {})
    codec = enc.get("codec", "hevc")
    filt = _encode_filter(prof)
    if speed > 1.0:
        # fine 模式：setpts 放在 fps 之前 —— setpts 把时间轴压短，
        # _encode_filter 的 fps 封顶负责把"倍速后变密的帧"限回去。
        filt = "setpts=PTS/%s,%s" % (_num(speed), filt)
    # -hwaccel 是输入选项，必须放在 -i 之前：放到输出侧 ffmpeg 会报
    # "Error opening output files: Invalid argument"（0.9.38 真机活动切片
    # 零输出的元凶——QSV 那一级从此必挂）。三处导出命令同一修法。
    if hw == "qsv":
        cmd += ["-hwaccel", "qsv"]
    cmd += ["-i", src]
    if hw == "qsv":
        # 编码支路允许一次下载：滤镜（去噪/缩放）在系统内存做，再交给 QSV 编码
        vcodec = "hevc_qsv" if codec == "hevc" else "h264_qsv"
        cmd += ["-vf", filt, "-c:v", vcodec,
                "-global_quality", str(int(prof.get("quality", 26))),
                "-preset", "medium", "-look_ahead", "1", "-bf", "3"]
    else:
        vcodec = "libx265" if codec == "hevc" else "libx264"
        cmd += ["-vf", filt, "-c:v", vcodec,
                "-crf", str(int(prof.get("quality", 26))), "-preset", "medium"]
        if codec == "hevc":
            cmd += ["-tag:v", "hvc1"]     # 飞牛 App / iOS 播放器兼容

    if enc.get("audio") == "aac32":
        cmd += ["-c:a", "aac", "-b:a", "32k"]
    else:
        cmd += ["-an"]                    # 监控基本无音轨，留着只是白占体积

    if enc.get("faststart", True):
        cmd += ["-movflags", "+faststart"]
    cmd += ["-y", out]
    return cmd


def precise_cmd(ffmpeg, src, out, ss, to, hw="qsv", speed=1.0,
                quality=26, fps_cap=15):
    """活动切片的精确导出：重编码换帧级切点。

    -ss/-to 放在 -i 前（输入侧 seek）+ 重编码，起点不再受关键帧约束。
    输出固定 H.264：回放页走 <video> 标签，缩时片段里混进 HEVC 会有
    浏览器播不了的坑；流复制路径的源本来就是摄像头的 H.264，不冲突。

    hw="qsv"   解码编码都走核显，4MP 单路只占个位数核（会话满/驱动坏时
               由调用方回落 hw="none"，这里不做探测）；
    hw="none"  libx264 veryfast：任何 NAS 都编得动，画质对监控画面足够。
    speed>1    与 fast 路径同规则：setpts 压时间轴 + fps 封顶，否则 2×
               的 25fps 源会变成 50fps，文件反而更大。
    """
    try:
        speed = float(speed or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    if speed < 1.0:
        speed = 1.0
    cap = max(5, min(60, int(fps_cap or 15)))
    cmd = _head(ffmpeg)
    if ss is not None:
        cmd += ["-ss", _num(ss)]
    if to is not None:
        cmd += ["-to", _num(to)]
    # 同 encode_cmd：-hwaccel 是输入选项，必须在 -i 之前
    if hw == "qsv":
        cmd += ["-hwaccel", "qsv"]
    cmd += ["-i", src, "-map", "0:v:0", "-an"]
    if speed > 1.0:
        filt = "setpts=PTS/%s,fps=%d" % (_num(speed), cap)
    else:
        filt = "null"
    if hw == "qsv":
        cmd += ["-vf", filt,
                "-c:v", "h264_qsv", "-global_quality", str(int(quality)),
                "-preset", "medium"]
    else:
        cmd += ["-vf", filt,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(quality))]
    cmd += ["-movflags", "+faststart", "-y", out]
    return cmd


def copy_speed_cmd(ffmpeg, src, out, ss, to, speed=1.0):
    """终极兜底：转封装 + 时间戳缩放，**不经过任何编码器**。

    0.9.39 起 _export_precise 的最后一级。之前的兜底走 -c copy 没问题，
    但 speed>1 时 encode_cmd 会悄悄换成 libx264 重编码 —— 在没有 libx264
    的 ffmpeg（LGPL 构建、裁剪版）上三级回落全灭，表现就是活动切片
    零输出。itsscale 直接改输入时间戳实现倍速：全部帧保留、播放时长按
    倍数缩短，任何 ffmpeg 都能跑；代价是体积不缩（兜底要的是"有输出"，
    不是省空间）。切点仍受关键帧约束。
    """
    try:
        speed = float(speed or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(1.0, speed)
    cmd = _head(ffmpeg)
    if ss is not None:
        cmd += ["-ss", _num(ss)]
    if to is not None:
        cmd += ["-to", _num(to)]
    cmd += ["-itsscale", _num(1.0 / speed), "-i", src,
            "-map", "0:v:0", "-an", "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", "-y", out]
    return cmd


def _encode_filter(prof):
    """去噪 → 降帧 → 缩放。顺序不能反：先压噪，CRF/ICQ 才不会花在噪点上。"""
    chain = []
    dn = int(prof.get("denoise", 0))
    if dn > 0:
        # hqdn3d 的 luma/spatial 强度：1≈轻、3≈强
        s = {1: "1.5:1.0:4:3", 2: "2.5:1.5:6:5", 3: "4.0:2.5:8:6"}.get(dn, "1.5:1.0:4:3")
        chain.append("hqdn3d=%s" % s)
    fps = int(prof.get("fps", 12))
    if fps != 30:
        chain.append("fps=%d" % fps)
    scale = int(prof.get("scale", 100))
    if scale < 100:
        chain.append("scale=trunc(iw*%d/100/2)*2:-2" % scale)
    return ",".join(chain) if chain else "null"


# ---------------------------------------------------------------- 变速

def speed_cmd(ffmpeg, src, out, speed=2.0, mode="pts", enc=None, hw="qsv"):
    """播放加速。两种方式的结果完全不同，不能用一个开关假装同时做到：

    pts      只改时间戳（-itsscale），一帧不删：快、无损，**体积一分不减**
    reencode 重新采样丢帧：体积约按 1/倍速 缩小，代价是画质略降、有跳帧感
    """
    speed = max(1.0, float(speed))
    cmd = _head(ffmpeg)
    if mode == "pts":
        cmd += ["-itsscale", _num(1.0 / speed), "-i", src, "-c", "copy",
                "-avoid_negative_ts", "make_zero"]
        if (enc or {}).get("faststart", True):
            cmd += ["-movflags", "+faststart"]
        cmd += ["-y", out]
        return cmd

    prof = dict(enc or {}, denoise=0, scale=100)
    prof["fps"] = max(1, int(round(30 / speed)))
    filt = "setpts=PTS/%s,%s" % (_num(speed), _encode_filter(prof))
    if hw == "qsv":
        cmd += ["-hwaccel", "qsv"]
    cmd += ["-i", src]
    if hw == "qsv":
        cmd += ["-vf", filt,
                "-c:v", "hevc_qsv", "-global_quality", "28", "-preset", "medium"]
    else:
        cmd += ["-vf", filt, "-c:v", "libx265", "-crf", "28",
                "-preset", "medium", "-tag:v", "hvc1"]
    cmd += ["-an", "-movflags", "+faststart", "-y", out]
    return cmd


# ---------------------------------------------------------------- 工具

def _num(v):
    """把 2.0 输出成 2、2.5 保留 2.5，命令行更干净。"""
    f = float(v)
    return str(int(f)) if f == int(f) else ("%.3f" % f).rstrip("0").rstrip(".")


def pretty(cmd):
    return " ".join(('"%s"' % c if " " in c else c) for c in cmd)


if __name__ == "__main__":
    import params
    prof = params.new_profile("演示")
    a = params.resolve(prof, "mono")
    print("QSV 分析：\n ", pretty(analysis_cmd("ffmpeg", "in.mp4", a, hw="qsv")))
    print("软件分析：\n ", pretty(analysis_cmd("ffmpeg", "in.mp4", a, hw="none")))
    print("预筛：\n ", pretty(prescan_cmd("ffprobe", "in.mp4")))
    print("导出(快·流复制)：\n ", pretty(encode_cmd(
        "ffmpeg", "in.mp4", "o.mp4", dict(prof["encode"], mode="fast"), ss=10, to=40)))
    print("导出(细·夜间档)：\n ", pretty(encode_cmd("ffmpeg", "in.mp4", "o.mp4", prof["encode"], night=True)))
    print("2 倍速：\n ", pretty(speed_cmd("ffmpeg", "in.mp4", "o2.mp4", 2, "pts")))
    print(json.dumps({"ok": True}, ensure_ascii=False))
