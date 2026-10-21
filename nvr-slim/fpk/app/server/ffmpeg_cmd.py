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


def prescan_cmd(ffprobe, src):
    """包大小预筛：包里字节数天然反映画面变化量，**不解码**。

    `packet=` 是容器层信息，不走解码器 —— 比 `frame=` 快一个数量级，
    也不依赖这颗 ffmpeg 编进了解码器（HEVC 的解码器缺失时 `frame=` 会
    一行都吐不出来，只剩 stderr 里的报错）。

    输出用 `compact=p=0:nk=0`（`size=1234|pts_time=0.000000`）：
    带字段名，解析不必赌 ffprobe 的字段顺序。
    """
    return [ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=size,pts_time",
            "-of", "compact=p=0:nk=0", src]


def prescan_frame_cmd(ffprobe, src):
    """兜底：老版本 ffprobe 不认 `packet=` 时退回 `frame=` 包大小。"""
    return [ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=pkt_size,pts_time",
            "-of", "compact=p=0:nk=0", src]


def probe_cmd(ffprobe, src):
    return [ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", src]


def redact_filter(cells, cols=16, rows=9):
    """遮蔽区格号 → drawbox 滤镜串（相对坐标，iw/ih 自适应实际分辨率）。

    格号 = row*cols + col（行在上，与前端 16×9 网格一致）。每个格画一个
    黑色实心框（t=fill）。返回空串表示没有遮蔽区，调用方应不加 -vf。
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
    """
    mode = enc.get("mode", "fine")
    cmd = _head(ffmpeg)
    if ss is not None:
        cmd += ["-ss", _num(ss)]
    if to is not None:
        cmd += ["-to", _num(to)]

    if mode == "fast":
        cmd += ["-i", src, "-c", "copy", "-avoid_negative_ts", "make_zero"]
        if enc.get("faststart", True):
            cmd += ["-movflags", "+faststart"]
        cmd += ["-map", "0:v:0", "-an", out]
        return cmd

    prof = enc.get("night" if night else "day", {})
    codec = enc.get("codec", "hevc")
    filt = _encode_filter(prof)
    cmd += ["-i", src]
    if hw == "qsv":
        # 编码支路允许一次下载：滤镜（去噪/缩放）在系统内存做，再交给 QSV 编码
        cmd += ["-hwaccel", "qsv"]
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
    cmd += ["-i", src]
    if hw == "qsv":
        cmd += ["-hwaccel", "qsv", "-vf", filt,
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
