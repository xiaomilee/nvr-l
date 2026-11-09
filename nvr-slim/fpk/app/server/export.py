#!/usr/bin/env python3
"""按时间范围批量导出：把一段时间内的录像分片打成一个 zip。

**为什么是「分片原样」而不是「按起止精确裁剪」**
  1. 录像是 `-c copy` 落下来的事实数据，任何裁剪都是二次解释。导出的用途是
     交接（物业 / 保险 / 警察）或自己归档，这里宁可在边界多给几十秒，也不动帧。
  2. 真要切只能用 `-c copy` 从关键帧切，切点最长差一个 GOP（默认几秒），
     仍然不精确；要做到帧级就得重编码，对一个只负责打包的动作来说失真代价
     与 CPU 代价都不划算。
  3. 多出来的部分不是黑箱：每片的真实起止与「窗外多出多少秒」都写进包里的
     `manifest.json` 与 `README.txt`。宁可写清楚"多给了 4 分钟"，也不做一次
     看起来精确、实际丢了开头几帧的切割。

**为什么用 chunked 而不是 Content-Length**
  zip 的每个条目要先算 crc32 才能写头，而 crc 得把文件读完才知道。为了一根
  进度条把几个 GB 读两遍不划算（机械盘上还要把 page cache 冲一遍），所以边读
  边写、用 HTTP/1.1 的分块编码。收尾的空块顺带说明"这一包是完整的"——中途
  出错时浏览器拿到的是失败，而不是一个看起来正常的半截 zip。

**为什么单次上限 1.9 GB**
  流式写入时 zipfile 走的是不可 seek 的路径（没法回头补头），因此不能启用
  zip64，总量必须留在 2 GB 以内。要导更大就缩小时间范围或减少机位；分卷下载
  是另一件事（见 ROADMAP 候选池），不在这里糊一个半成品。
"""
import os
import re
import time
import zipfile

# 单次导出的硬上限。三条都会在计划阶段就拦下来并说明原因 ——
# 让用户在大半夜等一个 4 GB 的包下到一半才失败，比一开始就说"太长了"糟得多。
MAX_SPAN_SEC = 7 * 86400
MAX_SPAN_DAYS = 7
MAX_FILES = 2000
MAX_BYTES = 1900 * 1024 * 1024

# 文件名里不能出现的字符（Windows 比 POSIX 严，按严的那边来：
# 包大概率被拷到 Windows 上解压）。
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 摄像机名可能很长（用户会写「一楼客厅东侧靠阳台」），目录名太深在
# Windows 上会碰到 260 字符路径限制。40 字符足够认出是哪一路。
_NAME_MAX = 40


def ts(text):
    """'YYYY-MM-DD HH:MM:SS' → epoch 秒（本地时区）。"""
    return time.mktime(time.strptime(str(text), "%Y-%m-%d %H:%M:%S"))


def epoch_at(day, sec):
    """某天的钟点秒 → epoch 秒。跨零点不会被夹断（23:58 起的分片可以到次日）。"""
    return ts(day + " 00:00:00") + float(sec)


def clock_text(sec):
    """钟点秒 → 'HH:MM:SS'（超过 24 小时按 24+ 折叠不回绕，只用于说明）。"""
    s = int(round(float(sec)))
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def select(segs, a, b):
    """挑出与 [a, b) 有交集的片段，并算出每片在窗外多出多少。

    segs 的每项需要 `start` / `end`（epoch 秒，end 不含）。边界是**半开区间**：
    刚好在 09:00:00 结束的那一片不算进 09:00 起的窗口 —— 与事件检索的时间窗
    语义保持一致，否则同一段录像在事件页和导出里会是两个答案。
    """
    out = []
    for s in segs or ():
        try:
            st, en = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if en <= st or en <= a or st >= b:
            continue
        overlap = min(en, b) - max(st, a)
        if overlap <= 0:
            continue
        e = dict(s)
        e["start_sec"] = round(st, 3)
        e["end_sec"] = round(en, 3)
        e["overlap_sec"] = round(overlap, 3)
        e["head_extra_sec"] = round(max(0.0, a - st), 3)   # 片头比窗口早多少
        e["tail_extra_sec"] = round(max(0.0, en - b), 3)   # 片尾比窗口晚多少
        out.append(e)
    out.sort(key=lambda x: (str(x.get("cam") or ""), float(x["start_sec"])))
    return out


def totals(entries):
    """整批的合计：文件数 / 字节 / 边界多出来的秒数（只算首尾，不是求和）。"""
    files = len(entries or ())
    size = sum(int(e.get("bytes") or 0) for e in entries or ())
    head = float(entries[0]["head_extra_sec"]) if files else 0.0
    tail = float(entries[-1]["tail_extra_sec"]) if files else 0.0
    return {"count": files, "bytes": size,
            "head_extra_sec": round(head, 1),
            "tail_extra_sec": round(tail, 1)}


def cap_problem(total, span_sec):
    """超上限时说清楚是哪一条、超多少。返回 None 表示通过。"""
    mb = 1024.0 * 1024.0
    if span_sec > MAX_SPAN_SEC:
        return ("时间范围 %.1f 天超过 %.0f 天上限，请缩小范围"
                % (span_sec / 86400.0, MAX_SPAN_DAYS))
    if total["count"] > MAX_FILES:
        return ("这次要导出 %d 个分片，超过 %d 个上限；请缩短时间范围或减少机位"
                % (total["count"], MAX_FILES))
    if total["bytes"] > MAX_BYTES:
        return ("这次要导出 %.1f GB，超过单次 %.1f GB 上限；请缩短时间范围或减少机位"
                % (total["bytes"] / (1024.0 ** 3), MAX_BYTES / (1024.0 ** 3)))
    return None


def slug(name, fallback="cam"):
    """摄像机名 → 安全的目录名。空名 / 全是坏字符时退回 fallback。"""
    s = _BAD_CHARS.sub("_", str(name or "").strip())
    s = s.strip(" .")                 # Windows 不接受结尾的点与空格
    if not s:
        s = fallback
    return s[:_NAME_MAX]


def folders(cams):
    """{cam_id: 包内目录名}。同名机位必须分开，否则两路的录像会叠在一个目录里。

    先按名字取，撞名的那几路（仅这几路）追加 id 区分 —— 只给冲突的加后缀，
    是为了不动大多数情况下那个干净的名字。
    """
    base = {}
    for c in cams:
        base[c["id"]] = slug(c.get("name") or c["id"], fallback=slug(c["id"]))
    seen = {}
    for cid, name in base.items():
        seen.setdefault(name.lower(), []).append(cid)
    out = {}
    for cid, name in base.items():
        holders = seen[name.lower()]
        out[cid] = name if len(holders) == 1 else "%s_%s" % (name, slug(cid))
    return out


def arc_name(folder, day, name):
    return "%s/%s/%s" % (folder, day, name)


def manifest(entries, req, version):
    """包里的机器可读清单。前端、脚本、以后的对账都读这一份，不去猜文件名。"""
    return {
        "generator": "nvr-l",
        "version": version,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "request": req,
        "counts": totals(entries),
        "notes": [
            "分片按原文件导出，未解码、未重编码、未裁剪。",
            "边界会多出最多一个分片；每片的 start_at/end_at 是它的真实起止。",
            "正在写入的那一片（.mp4.part）不在导出里：它还没有 moov，播不了。",
            "清单里没有的时段，是当时确实没录（断流 / 停机 / 已过保留期）。",
        ],
        "files": [{
            "path": e.get("arc") or "",
            "cam": e["cam"],
            "day": e["day"],
            "name": e["name"],
            "start_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(e["start_sec"])),
            "end_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(e["end_sec"])),
            "bytes": int(e.get("bytes") or 0),
            "overlap_sec": e["overlap_sec"],
            "head_extra_sec": e["head_extra_sec"],
            "tail_extra_sec": e["tail_extra_sec"],
        } for e in entries or ()],
    }


def readme(entries, req, version):
    """给人看的说明。放在包里第一层，解压第一眼就能读到。"""
    t = totals(entries)
    mb = t["bytes"] / (1024.0 * 1024.0)
    names = req.get("cam_names") or []
    lines = [
        "NVR-L 录像导出（版本 %s）" % version,
        "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "一、这次导出的是什么",
        "  时间窗口：%s ~ %s" % (req.get("from"), req.get("to")),
        "  机位：%s" % ("、".join(names) if names else "（未记录）"),
        "  文件：%d 个分片，共 %.1f MB" % (t["count"], mb),
        "",
        "二、时间边界（重要）",
        "  录像按 5 分钟分片落盘，导出的是**完整分片**，没有重新编码，也没有裁剪。",
        "  所以这一包在两头可能比你选的范围多一些：",
        "    片头早 %s，片尾晚 %s。" % (clock_text(t["head_extra_sec"]),
                                     clock_text(t["tail_extra_sec"])),
        "  为什么不做精确裁剪：裁剪只能落在关键帧上（仍会差几秒），要帧级精确就得",
        "  重编码整段画面。交接用的材料宁可多给几十秒，也不动原始画面。",
        "  每片的真实起止见 manifest.json。",
        "",
        "三、目录结构",
        "  <机位>/<日期>/<时-分-秒>.mp4    文件名就是这一片的起始时刻",
        "  manifest.json                   机器可读清单（起止、字节、重叠秒数）",
        "  README.txt                      本文件",
        "",
        "四、播放",
        "  任意播放器都能直接打开（H.264 + MP4）。片与片之间是连续的，",
        "  但每个文件是独立可播的，不需要拼接。",
        "",
        "五、清单里没有的时段",
        "  那是当时确实没有录像：断流、停机、或已过保留期。不是导出时漏掉了。",
        "  正在写入的最后一片（.mp4.part）也不在里面 —— 它还没封口，播不了。",
        "",
    ]
    return "\n".join(lines)


def write_zip(out, items):
    """把 [(包内路径, bytes 或 磁盘路径)] 写成一个 zip 流。

    `out` 允许不可 seek（socket）：zipfile 会自动改用数据描述符，也就是
    "先写数据、后补 crc 与长度"。返回写入的条目数。
    """
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=False) as zf:
        for arc, src in items:
            zi = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = 0o644 << 16
            if isinstance(src, (bytes, bytearray)):
                zf.writestr(zi, bytes(src))
            else:
                with open(src, "rb") as f:
                    with zf.open(zi, "w") as w:
                        while True:
                            chunk = f.read(256 * 1024)
                            if not chunk:
                                break
                            w.write(chunk)
            n += 1
    return n


class Chunked:
    """HTTP/1.1 分块编码的写入口。

    只需要 write / flush ——**故意不提供 tell**：zipfile 一旦发现能 tell 就会
    认为可以回头补头（seek），流式写的时候那一步必然失败。没有 tell，
    zipfile 直接走数据描述符那条正确路径。
    """

    def __init__(self, wfile):
        self.w = wfile
        self.n = 0

    def write(self, data):
        if not data:
            return 0
        self.w.write(b"%x\r\n" % len(data))
        self.w.write(data)
        self.w.write(b"\r\n")
        self.n += len(data)
        return len(data)

    def flush(self):
        try:
            self.w.flush()
        except (AttributeError, OSError):
            pass

    def finish(self):
        """收尾空块。写了它 = "这一包是完整的"；没写就是半截，客户端会报错。"""
        try:
            self.w.write(b"0\r\n\r\n")
            self.w.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
