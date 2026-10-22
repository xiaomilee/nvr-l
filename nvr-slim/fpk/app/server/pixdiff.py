#!/usr/bin/env python3
"""像素差判读引擎。

和码流曲线引擎的区别：码流曲线看的是「这块时间片有多少字节」，像素差看的是
「这块画面有多少像素真的变了」。前者便宜但会被码率策略骗（静态画面遇到 I 帧
照样一大包），后者才是"剔除静止画面"的正解。

为什么不在 Python 里逐像素算：
  fnOS 的应用包只带标准库，没有 numpy。320×180 灰度 ×2 fps × 5 分钟
  = 1700 万个像素比较，纯 Python 要十几秒且吃满一个核；更别说还要读
  rawvideo 管道、对齐帧边界。所以整条差分交给 ffmpeg 的 C 实现，
  Python 只解析「每帧一个数」——单帧成本降到一次 float 解析。

滤镜链（一次解码完成全部工作）：
  fps 抽样 → scale=320x180 → format=gray → drawbox 涂黑忽略区
  → boxblur 压噪声 → tblend 差分 → lut 按像素差门槛二值化
  → signalstats 求 YAVG → metadata=print

关键点说明：
1. 缩放**先于**差分。1080p 全分辨率上压缩噪声会让每个像素都"在动"，
   门槛怎么调都压不住；320×180 上一个人仍有 6×12 像素，判定够用，
   lut/signalstats 的成本也降到 1/36。
2. 忽略区用 drawbox 涂黑是**对的**（和码流曲线必须用 crop 相反）：
   两帧都涂黑后再差分，那块的差值恒为 0，真正做到了"不参与判定"；
   而码流曲线是被涂黑的区域自己会从无到有地产生大包，涂黑反而制造事件。
3. lut 把「差值 > 像素差门槛」变成 255、其余 0，signalstats 的 YAVG
   就是 255 × 变化像素占比 —— 门槛判定发生在**像素级**，不是整帧平均，
   这样一小块真变化不会被大面积静止稀释掉（帧平均最大的毛病）。
4. 整条链只用 fps/scale/format/drawbox/boxblur/tblend/lut/signalstats/
   metadata 这些十几年前就稳定的滤镜，不依赖特定 ffmpeg 版本的私有行为；
   仍然会先探测一次可用性，缺哪个就明确报告（绝不静默跑错）。
"""

import os
import re
import subprocess

# 判定画面的固定尺寸。16:9 与绝大多数摄像头一致；4:3 的机位会被轻微拉伸，
# 对"有没有变化"没有影响，换来的是忽略区格子和像素坐标的固定换算。
W, H = 320, 180
MASK_COLS, MASK_ROWS = 16, 9          # 与前端画布一致

# 必需滤镜：缺任何一个都不能跑像素判读
REQUIRED = ("fps", "scale", "format", "tblend", "lut", "signalstats", "metadata")
# 可选滤镜：缺了就退化成"不模糊 / 不涂忽略区"，并如实报告
OPTIONAL = ("boxblur", "drawbox")

_CACHE = {}                            # ffmpeg 路径 → (ok, 缺失列表, 可用集合)


def _run(cmd, timeout=120):
    """跑一条命令，返回 (stdout, stderr, 退出码)。异常也变成返回值。"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", str(e)[:200], -1
    return (r.stdout or b"").decode("utf-8", "replace"), \
           (r.stderr or b"").decode("utf-8", "replace"), r.returncode


def filters(ffmpeg):
    """探测这颗 ffmpeg 有哪些滤镜。结果按二进制路径缓存（一次进程只探一次）。

    用 ffmpeg -filters 而不是 -h filter=tblend：前者一次就列全，后者要跑九次。
    """
    key = os.path.abspath(ffmpeg or "")
    if key in _CACHE:
        return _CACHE[key]
    out, err, rc = _run([ffmpeg, "-hide_banner", "-filters"], timeout=20)
    names = set()
    for line in out.splitlines():
        parts = line.split()
        # 形如 " T.. tblend            V->V       Blend two video frames"
        if len(parts) >= 2 and parts[0].startswith(("T", "t", ".", "S", "P")):
            names.add(parts[1])
    missing = [f for f in REQUIRED if f not in names]
    extra = [f for f in OPTIONAL if f not in names]
    res = (not missing, missing, extra, names)
    _CACHE[key] = res
    return res


def available(ffmpeg):
    """能不能跑像素判读。返回 (ok, 原因)。原因直接可以展示给用户。"""
    if not ffmpeg:
        return False, "未找到 ffmpeg"
    ok, missing, extra, _ = filters(ffmpeg)
    if not ok:
        return False, "ffmpeg 缺少滤镜：%s" % "、".join(missing)
    if extra:
        # 可选滤镜缺了能跑，但要说明会少什么
        pass
    return True, ""


def _num(v, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def build_chain(a, rects=None):
    """构造滤镜串。a 是 params.resolve 出来的分析参数。

    rects：忽略区矩形 [(x, y, w, h)]，已经是像素坐标（见 condense.cell_rects）。
    """
    fps = _num(a.get("fps"), 2.0)
    fps = max(0.2, min(10.0, fps))
    pd = int(_num(a.get("pixel_delta"), 16))
    pd = max(1, min(80, pd))
    blur = int(_num(a.get("blur"), 0))
    blur = max(0, min(3, blur))

    chain = ["fps=%s" % ("%g" % fps),
             "scale=%d:%d" % (W, H),
             "format=gray"]
    for x, y, w, h in rects or []:
        chain.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=black:t=fill"
                     % (x, y, w, h))
    if blur > 0:
        # boxblur=半径:次数。半径 1~3 只压高频颗粒噪声，人形轮廓不受影响。
        chain.append("boxblur=%d:1" % blur)
    chain.append("tblend=all_mode=difference")
    # 单引号在 exec 数组里是字面量，ffmpeg 的滤镜解析器自己会剥掉
    chain.append("lut=y='if(gt(val,%d),255,0)'" % pd)
    chain.append("signalstats")
    chain.append("metadata=print:key=lavfi.signalstats.YAVG:file=-")
    return ",".join(chain)


def cmd(ffmpeg, src, a, rects=None, ss=None, dur=None, lavfi=None):
    """一次判读的命令。ss/dur 给了就只解这段（预筛之后的关键提速）。"""
    c = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
         "-an", "-sn", "-dn"]
    if lavfi:
        c += ["-f", "lavfi", "-i", lavfi]
    else:
        if ss:
            # -ss 放在 -i 前：输入侧 seek，跳到关键帧，不解码前面几万帧
            c += ["-ss", "%.3f" % float(ss)]
        if dur:
            c += ["-t", "%.3f" % float(dur)]
        c += ["-i", src]
    c += ["-vf", build_chain(a, rects), "-f", "null", "-"]
    return c


_RE_PTS = re.compile(r"pts_time:([0-9.]+)")
_RE_Y = re.compile(r"YAVG=([0-9.]+)")


def parse(stdout, offset=0.0):
    """解析 metadata=print 的输出 → [(t, 变化像素占比 0~1)]。

    输出形如：
        frame:12   pts:2400   pts_time:1.2
        lavfi.signalstats.YAVG=12.345678
    YAVG 是 0/255 二值图的均值，除以 255 就是"超过门槛的像素占比"。
    """
    out = []
    t = None
    for line in (stdout or "").splitlines():
        m = _RE_PTS.search(line)
        if m:
            t = float(m.group(1))
            continue
        m = _RE_Y.search(line)
        if m and t is not None:
            try:
                y = float(m.group(1))
            except ValueError:
                continue
            out.append((round(t + offset, 3), round(y / 255.0, 6)))
            t = None
    return out


def curve(ffmpeg, src, a, rects=None, ranges=None, timeout=300):
    """跑出像素差曲线。ranges=[(ss, dur)]，None 表示整段。

    返回 {"ok":bool, "curve":[...], "error":str}
    一个区间失败就整体失败：只拿到半段曲线去下"整段静止"的结论，
    等于把没读的那半段录像判成没内容——宁可回落到码流曲线。
    """
    ranges = ranges or [(None, None)]
    acc = []
    for ss, dur in ranges:
        out, err, rc = _run(cmd(ffmpeg, src, a, rects, ss, dur),
                            timeout=timeout)
        pts = parse(out, offset=float(ss or 0))
        if not pts:
            tail = (err or "").strip().splitlines()
            return {"ok": False, "curve": [],
                    "error": "像素判读无输出" + ("（%s）" % tail[-1][:150] if tail else "")}
        acc.extend(pts)
    acc.sort(key=lambda x: x[0])
    return {"ok": True, "curve": acc, "error": ""}


def self_test(ffmpeg, timeout=60):
    """用合成片源自检：不依赖用户的录像、不需要编码器。

    testsrc 是一条会动的测试图，只要有变化，变化占比就一定 > 0；
    解析到的帧数为 0 或全为 0，说明滤镜链在这颗 ffmpeg 上不成立。
    """
    ok, why = available(ffmpeg)
    if not ok:
        return {"ok": False, "reason": why, "frames": 0}
    a = {"fps": 2, "pixel_delta": 16, "blur": 1}
    out, err, rc = _run(cmd(ffmpeg, "", a,
                            lavfi="testsrc=size=320x180:rate=10:duration=3"),
                        timeout=timeout)
    pts = parse(out)
    if not pts:
        tail = (err or "").strip().splitlines()
        return {"ok": False, "frames": 0,
                "reason": "滤镜链跑不通" + ("（%s）" % tail[-1][:160] if tail else "")}
    peak = max(v for _, v in pts)
    if peak <= 0:
        return {"ok": False, "frames": len(pts), "peak": 0,
                "reason": "跑通了但变化恒为 0（像素差没起作用）"}
    _, _, extra, _ = filters(ffmpeg)
    return {"ok": True, "frames": len(pts), "peak": round(peak, 4),
            "missing_optional": extra,
            "reason": "" if not extra else "缺少可选滤镜：%s（模糊/忽略区会失效）"
                                             % "、".join(extra)}


if __name__ == "__main__":
    import json
    import sys
    ff = sys.argv[1] if len(sys.argv) > 1 else "ffmpeg"
    print(json.dumps(self_test(ff), ensure_ascii=False, indent=2))
