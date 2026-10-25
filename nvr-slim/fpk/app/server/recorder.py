#!/usr/bin/env python3
"""RTSP 拉流录制：主码流按分片落盘。

三个设计决定，都直接服务于「录完能不能马上压」：

1. **`-c copy` 转封装，不解码。**
   8 路 4MP 主码流合计只占几个百分点的单核，核显完整留给判定与预览。
   代价是画质参数在这一层无效 —— 录制只负责把原始码流完整、低开销地
   落到盘上，压缩发生在后一步。

2. **分片 5 分钟（可配 60~900 秒）。**
   分片越短，"录完 → 压制"的间隔越好控，热数据命中 page cache 的概率越高；
   太短则文件开合频繁、静止判定的上下文不足。5 分钟约合单路 150 MB，
   既在内存里待得住，也够判定看出"这五分钟到底有没有人"。

3. **只处理「已封口」的分片。**
   segment muxer 是顺序写的：看到下一个分片出现，就说明上一个已经写完。
   这条规则比 mtime 稳，也比 inotify 简单。正在写的那个分片 moov 尚未落地，
   碰它就是坏文件 —— 所有消费方必须经过 finished() 这道闸。

断流处理：RTSP 会在摄像头重启、网络抖动、交换机重启时断。
进程退出就按 5→10→20→30 秒退避重启，不无限快速重试把日志写爆。
跨天时主动重启一次，让输出目录跟着日期走。
"""
import datetime
import os
import signal
import subprocess
import time

import ffmpeg_cmd  # noqa: E402,F401  （保留：脚本内自检与外部调用方依赖该导出）
import runtime  # noqa: E402

# 退避封顶。原来是 30 秒 —— 真机上 5 路全部连不上时，每 6 秒就有一个进程
# 起来再死，日志 7 分钟刷出 250 行，用户看到的全是同一句话。封顶拉到 5 分钟：
# 摄像头真的断电了，5 路合计每 5 分钟才试一次；恢复上电后最多等 5 分钟。
MAX_BACKOFF = 300
STALL_SEC = 75          # 最新分片超过这么久没更新，就判定这一路"假活"

# 「卡住」重启的等待上限。0.9.73 起与「连不上」分开算。
# 卡住 ≠ 连不上：进程活着、socket 被阻塞，对端在但暂时不吐数据；重连之前
# 这段时间是**完全不录**的。用 5 分钟的连不上退避去等一个间歇断流的摄像头，
# 等于每次抖动白丢 5 分钟录像，而且重启越慢、暴露在断流里的时间越长。
# 真机 2026-10-26：四路每 75–90 秒卡一次，退避被封到 300 秒后，车库一天
# 出现 254 秒、422 秒的空洞。卡住就该尽快重连：5→10→20→30 封顶。
STALL_BACKOFF = 30


def backoff_wait(tries, reconnect=5):
    """「连不上」（进程退出）的等待：5→10→20→40→80→160→300 封顶。

    指数用 `min(t-1, 9)` 而不是 4：4 意味着 5×2⁴=80 秒就到顶了，
    MAX_BACKOFF=300 形同虚设（0.9.70 之前的真 bug）。
    抽成函数是为了让测试直接验真值 —— 测试里照抄公式等于没测。
    """
    t = max(1, int(tries))
    return min(MAX_BACKOFF, max(1, int(reconnect)) * (2 ** min(t - 1, 9)))


def stall_wait(tries, reconnect=5):
    """「卡住」重启的等待：5→10→20→30 封顶（见 STALL_BACKOFF 的注释）。"""
    t = max(1, int(tries))
    return min(STALL_BACKOFF, max(1, int(reconnect)) * (2 ** min(t - 1, 9)))

# 收尾宽限：SIGINT 之后给 ffmpeg 多久把 moov 写完，再补一次，最后才 SIGKILL。
# 真机 2026-10-26 车库实测：卡死的 ffmpeg 被直接 SIGKILL，12 个分片永久留在
# .part（合计约 75 MB 有效画面）。SIGINT 是可捕获的，segment muxer 会立刻
# 收尾；只有真正僵死（读盘卡住）的进程才需要走到 SIGKILL。
KILL_SOFT_SEC = 15
KILL_HARD_SEC = 10

# 残片抢救：静置这么久才动手（正在写的那一个绝不能碰），每轮最多处理几个。
# 抢救本身是 -c copy 重封装，不重编码，几十倍实时，但仍是顺序读盘，所以设上限。
PART_GRACE_SEC = 600
PART_RESCUE_PER_RUN = 1

# 封口前的静置时间。分片 MP4（0.9.71 起默认）**从第一秒起就有 moov**，
# 所以「有 moov」不再等于「写完了」—— 还必须确认文件已经不再增长。
# 30 秒足够覆盖任意写盘节奏，又远小于最短分片（60 秒），回放页最多晚半分钟
# 看到新片段。少了这道闸，正在写的分片会被改名成 .mp4，而 ffmpeg 还握着
# 原来的 fd 继续写，那个文件从此不再被任何扫描看见（等于无声丢一段录像）。
SEAL_GRACE_SEC = 30

# 拉流阶段的「可选输入选项」。任何一项被 ffmpeg 报 Unrecognized option，就由
# _absorb_bad_opts() 自动剔除并立刻重试（不等退避）—— 这是唯一安全的做法：
# 不预探测、不猜，只按 ffmpeg 自己说的话删。0.9.11 的 -rw_timeout、0.9.13 的
# -timeout 两次都是"以为通用其实精简版不认"，靠人工记住参数白名单不现实。
INPUT_OPTS = (
    # 探测耐心。摄像头建连后常要 1~3 秒才吐出第一个 IDR，默认 probe 大约
    # 只给 2 秒 / 5MB 就放弃；SPS/PPS 还没到手 → h264 parser 解不出参数集 →
    # 码流被判无效 → 输出侧 map 不出任何流 → ffmpeg 立刻以
    #   "Output file does not contain any stream / Error opening output files"
    # 退出（真机 0.9.14 五路全中，全都是启动后 2 秒死）。10 秒足够绝大多数
    # 家用摄像头吐第一个关键帧。
    ("-analyzeduration", "10000000"),
    ("-probesize", "10000000"),
)


# ---------------------------------------------------------------- 路径

def cam_dir(root, cam_id, day=None):
    day = day or datetime.date.today()
    return os.path.join(root, cam_id, day.isoformat())


def out_pattern(root, cam_id):
    """ffmpeg 的 strftime 会替换整个输出串里的 %，所以目录也能按日期生成。

    目录必须预先存在（ffmpeg 不建目录），由 ensure_day() 在开录前和跨天时补。
    输出落到 `.mp4.part`：正在写的分片带 .part 后缀，消费者只认 .mp4，
    于是「录到一半」的片段永远不会被当成可回放的录像；封口后由
    finalize_parts() 原子改名成 .mp4。
    """
    return os.path.join(root, cam_id, "%Y-%m-%d", "%H-%M-%S.mp4.part")


def _has_moov(path):
    """这个分片现在能不能播：**顶层 box 结构**里有没有 moov。

    两种文件都要认（0.9.71 起录制默认走分片 MP4）：

      分片 MP4（`+empty_moov`）：`ftyp + moov + (moof + mdat)…`，moov 在最前面、
        内容为空，索引靠每个 fragment 自带的 moof —— 从写下第一秒起就可播，
        不需要任何收尾；
      整片 MP4（老数据、或关掉 record_frag）：`ftyp + mdat + moov`，moov 在尾部，
        必须等 ffmpeg 正常退出才完整。

    为什么不用「在文件头/尾直接搜 b"moov"」：H.264 码流里出现这四个字节是纯随机
    事件，64 KB 里约 3 万分之一的命中率 —— 一旦命中，正在写的分片会被判成"已封口"
    并改名，而 ffmpeg 还握着 fd 往里写，直接毁掉一段录像。按 box 结构走一遍（每个
    box 只读 8 字节头）顶多几十次 seek，代价可以忽略。
    """
    try:
        size = os.path.getsize(path)
        if size < 16:
            return False
        with open(path, "rb") as f:
            off = 0
            for _ in range(64):          # 顶层 box 不会很多，同时防死循环
                if off + 8 > size:
                    return False
                f.seek(off)
                hdr = f.read(8)
                if len(hdr) < 8:
                    return False
                bsize = int.from_bytes(hdr[:4], "big")
                typ = hdr[4:8]
                if bsize == 1:           # largesize：后面再跟 8 字节真实长度
                    if off + 16 > size:
                        return False
                    ext = f.read(8)
                    if len(ext) < 8:
                        return False
                    bsize = int.from_bytes(ext, "big")
                if typ == b"moov":
                    return True
                if bsize == 0:           # 0 = 一直到文件尾，后面不可能再有 box
                    return False
                if bsize < 8 or off + bsize > size:
                    return False         # box 越界 = 还在写 / 被截断
                off += bsize
            return False
    except OSError:
        return False


def finalize_parts(root, cam_id, rescue=None, grace=PART_GRACE_SEC, budget=None):
    """把已封口的 `.mp4.part` 改名成 `.mp4`；没封口的交给 rescue 抢救。

    判定用 moov 而非「目录里有没有更新的分片」：后者在 ffmpeg 崩溃、下个
    进程又写了新分片后，会把上一个没封口的半截文件误当成完整文件。
    改名是原子的（os.replace），读者要么看到 .part 要么看到完整 .mp4。

    rescue(src, dst) -> bool 是残片抢救钩子（ffmpeg 重建索引）。没有 moov 的
    分片以前只是 `continue` 跳过 —— 不记录、不重试、永远躺在盘上，用户只能
    在文件管理器里看到一堆打不开的 .part。现在按三条纪律处理：

      1. **只碰静置超过 grace 秒的**：正在写的那个 mtime 是滚动的，绝不动它；
      2. **每轮限量**（budget 是可变单元素列表，跨摄像头共享）：抢救是顺序
         读盘，跟正在录的进程抢 IO，不能一次把 20 个一起啃；
      3. **失败改名 `.bad`**：不留原名，否则每个 tick 都会重试一次，
         20 个坏文件能把 CPU 和盘 IO 长期钉住。
    """
    base = os.path.join(root, cam_id)
    if not os.path.isdir(base):
        return
    try:
        days = sorted(d for d in os.listdir(base)
                      if len(d) == 10 and d[4] == "-" and d[7] == "-")
    except OSError:
        return
    for d in days:
        full = os.path.join(base, d)
        if not os.path.isdir(full):
            continue
        try:
            parts = sorted(n for n in os.listdir(full)
                           if n.lower().endswith(".mp4.part"))
        except OSError:
            continue
        # 抢救中途被杀（升级、断电、SIGKILL）会留下 `.rescuing`：它既不是录像
        # 也不是待救的残片，静置够久就删掉，否则会一直占着与源文件同等的空间。
        try:
            leftovers = [n for n in os.listdir(full) if n.endswith(".rescuing")]
        except OSError:
            leftovers = []
        for n in leftovers:
            lp = os.path.join(full, n)
            try:
                if time.time() - os.path.getmtime(lp) > grace:
                    os.remove(lp)
            except OSError:
                pass
        for n in parts:
            src = os.path.join(full, n)
            dst = src[:-5]                # 去掉 ".part"
            if os.path.isfile(dst):        # 同名 .mp4 已存在（上次改名失败残留）
                try:
                    os.remove(src)
                except OSError:
                    pass
                continue
            if _has_moov(src):             # 结构上已经可播
                # 分片 MP4 从第一秒起就有 moov，它和「记录结束」是两件事：
                # 必须再确认文件已静置，否则会把正在写的分片改名走。
                try:
                    if time.time() - os.path.getmtime(src) < SEAL_GRACE_SEC:
                        continue
                except OSError:
                    continue
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
                continue
            if not rescue:
                continue
            try:
                if time.time() - os.path.getmtime(src) < grace:
                    continue               # 还在写（或刚写完），下一轮再说
            except OSError:
                continue
            if budget is not None:
                if budget[0] <= 0:
                    continue
                budget[0] -= 1
            try:
                ok = rescue(src, dst)
            except Exception:              # 抢救是兜底，不能让它打断巡检
                ok = False
            if ok:
                # 救援只负责产出 dst，源残片由这里删 —— 留着的话，
                # 目录里会同时有 `01-10-00.mp4` 和 `01-10-00.mp4.part`，
                # 而下一个 tick 发现 .part 没 moov 又会抢救一遍，白烧 IO。
                try:
                    os.remove(src)
                except OSError:
                    pass
                continue
            try:
                os.replace(src, src + ".bad")
            except OSError:
                pass


def ensure_day(root, cam_id, day=None):
    d = cam_dir(root, cam_id, day)
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def list_segments(root, cam_id, cache=None):
    """某路所有分片，按「日期目录 + 文件名」排序 —— 文件名是 HH-MM-SS，天然有序。

    cache 是调用方持有的 {目录: (mtime_ns, [文件名])}：/api/status 每次都全量
    walk 一遍 90 天 × 288 片，几千次 listdir 纯属浪费，目录 mtime 没变就直接复用。
    """
    base = os.path.join(root, cam_id)
    out = []
    if not os.path.isdir(base):
        return out
    try:
        days = sorted(d for d in os.listdir(base)
                      if len(d) == 10 and d[4] == "-" and d[7] == "-")
    except OSError:
        return out
    for d in days:
        full = os.path.join(base, d)
        if not os.path.isdir(full):
            continue
        names = None
        if cache is not None:
            try:
                mt = os.stat(full).st_mtime_ns
            except OSError:
                continue
            hit = cache.get(full)
            if hit and hit[0] == mt:
                names = hit[1]
            else:
                try:
                    names = sorted(n for n in os.listdir(full) if n.lower().endswith(".mp4"))
                except OSError:
                    continue
                cache[full] = (mt, names)
        else:
            try:
                names = sorted(n for n in os.listdir(full) if n.lower().endswith(".mp4"))
            except OSError:
                continue
        for n in names:
            p = os.path.join(full, n)
            if os.path.isfile(p):
                out.append(p)
    return out


def finished(root, cam_id, seg_sec=300, now=None):
    """已封口的分片。

    末位分片是"正在写"的，一律跳过；但如果它已经很久没变化（录制停了、
    当天最后一片），也算封口 —— 否则收工后的最后一片永远不会被处理。
    """
    segs = list_segments(root, cam_id)
    if not segs:
        return []
    now = now if now is not None else time.time()
    head, tail = segs[:-1], segs[-1]
    try:
        idle = now - os.path.getmtime(tail)
    except OSError:
        idle = 0
    # 两个分片周期没动静，说明这一路已经不写了
    if idle > max(120, seg_sec + 60):
        return segs
    return head


# ---------------------------------------------------------------- 命令

def record_cmd(ffmpeg, url, pattern, seg_sec=300, transport="tcp", opts=(),
               frag=True):
    """拉流 → 分片落盘。

    -rtsp_transport tcp：UDP 在跨交换机时丢包会让花屏，监控盘上留不下证据。
    **不加任何"超时类"参数**：fnOS 自带的精简 ffmpeg 不认 -rw_timeout（0.9.11
    因此五路全挂），-timeout 同样有风险 —— 一旦这个选项不被识别，ffmpeg 立刻以
    "Unrecognized option" 退出，表现就是「快照能看、录像一条都没有」。读超时
    统一由 recorder.tick() 的软件看门狗兜底（见 STALL_SEC）。
    opts：额外的输入选项（必须放在 -i 之前）。见 INPUT_OPTS —— 探测耐心靠它，
          且每一项都会在 ffmpeg 明确表示不认时被自动剔除。
    -segment_atclocktime 1：分片边界对齐墙钟，文件名即片段起点，
                       回放时按时间定位不用查表。

    始终 -c copy：录像阶段**不做任何像素级处理**。以前为了"输出遮蔽区"在这里
    插 drawbox 并强制 libx264 重编码，代价是每路常驻一个编码器、CPU 直接翻倍，
    而用户真正需要的是"别把这块算成事件"——那属于检测忽略区，在判定阶段生效，
    与录像本身无关。

    frag=True（0.9.71 起默认）走**分片 MP4**：
        +empty_moov       moov 写在文件最前面，结构上不再依赖"收尾时补索引"
        +frag_keyframe    每个关键帧切一个 moof，落到盘上就是一段可独立解码的单元
        +default_base_moof  fragment 内偏移相对自身，不依赖前面的数据
    效果：进程被 SIGKILL、断电、拔盘 —— 已经写下去的每一段都能播，损失上限是
    最后一个 fragment（约一个 GOP，1~2 秒），而不是整个分片（最多 5 分钟）。
    这是"硬杀必坏"变成"硬杀也能播"的机制性解法，与 0.9.70 的收尾加固互补。

    代价（都是可接受量级）：每个 fragment 多一个 moof（几百字节，17 MB 的分片
    约占 0.3%）；没有 sidx，浏览器 seek 时要扫一遍文件头（17 MB 很快）。
    真的遇到播放器不兼容，把 record_frag 关掉即可退回原来的整片 moov 写法。
    """
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
    for o in (opts or ()):
        cmd.extend(o)
    cmd += ["-rtsp_transport", transport,
            "-i", url,
            # **必须显式 -map**（0.9.17 定案的真根因）：segment muxer 不声明
            # 任何编码，ffmpeg 的自动映射会退回「按输出文件名扩展名猜封装」
            # （av_guess_codec 对 segment 有特判）。`.mp4.part` 的 .part 后缀
            # 猜不出任何封装 → video_codec=NONE → 视频不被映射，-an 又挡掉
            # 音频 → 0 条流 → "Output file does not contain any stream" +
            # EINVAL 退出。从 0.9.8 到 0.9.16 的"一条录像都没有"全是它；
            # 快照/预览走别的命令所以看着正常，0.9.15 的探测耐心修复只是
            # 碰巧在试录里用了 .mp4 后缀才"看起来修好"。显式 -map 完全绕开
            # 这条猜测路径。摄像头真的没给视频流时会报 "matches no streams"，
            # 由 classify_error 给出人话提示。
            "-map", "0:v:0",
            "-c", "copy",
            "-an", "-sn", "-dn",
            "-f", "segment",
            "-segment_time", str(int(seg_sec)),
            "-segment_format", "mp4"]
    if frag:
        # **必须走 `-segment_format_options`，不能写 `-movflags`。**
        # `-movflags` 是 mp4 muxer 的私有选项，而这里最外层的 muxer 是 segment；
        # ffmpeg 把找不到归属的输出选项保留下来、**不报错**（av_opt_set_dict2 的
        # 行为），segment muxer 又只把 `-segment_format_options` 转发给子 muxer。
        # 所以 `-movflags +frag_keyframe…` 在这种命令里是**静默无效**的：录出来
        # 仍是整片 mp4，代码里却写着分片，测试也照样绿 —— 属于最难查的一类坑。
        cmd += ["-segment_format_options",
                "movflags=+frag_keyframe+empty_moov+default_base_moof"]
    cmd += ["-segment_atclocktime", "1",
            "-reset_timestamps", "1",
            "-strftime", "1",
            "-y", pattern]
    return cmd


# ffmpeg 不认某个选项时的两种说法，都要能抓到
_BAD_OPT_RES = (
    ("Unrecognized option '%s'", None),
    ("Option '%s' not found", None),
)
_RE_UNRECOGNIZED = None


def parse_bad_option(err):
    """"Unrecognized option 'x'" → "x"；识别不出返回 None。

    真机例子：`[AVFormatContext @ ...] Unrecognized option 'analyzeduration'.`
    去掉首尾可能的句号/引号，拿到的就是该从命令行里删掉的选项名。
    """
    import re as _re
    if not err:
        return None
    m = (_re.search(r"Unrecognized option '([^']+)'", err)
         or _re.search(r"Option '([^']+)' not found", err))
    return m.group(1) if m else None


# 错误分类 → (kind, 给用户看的一句话)。kind 是稳定的机器可读值，界面按它
# 决定显示什么建议；不要把中文塞进 kind，否则改文案就改坏了判断。
#
# 顺序有讲究：先判"选项不认"和"写盘"，这两类最容易和最像的现象混淆。
_ERR_RULES = (
    ("badopt", ("Unrecognized option", "Option not found"),
     "这台 NAS 的 ffmpeg 不支持某个参数，程序会自动去掉，稍等即可"),
    ("no_stream", ("does not contain any stream",),
     "摄像头没有吐出可识别的码流：多半是关键帧来得太慢，或编码格式这个 "
     "ffmpeg 不支持（H.265 尤其常见）。可在设置里把该路换成子码流试试"),
    ("no_video", ("matches no streams",),
     "这条地址上映射不出视频流：核对 RTSP URL 里的通道号/码流类型"
     "（很多摄像头主码流在 ch1、子码流在 ch2，写错就只有音频甚至全无）"),
    ("codec", ("Could not find tag", "dimensions not set", "codec not currently supported",
               "Could not write header"),
     "这个 ffmpeg 缺少该编码格式的封装支持（常见于 H.265 / HEVC），"
     "换子码流（一般是 H.264）通常可用"),
    ("rtsp_reject", ("SETUP failed", "Server returned 4", "Server returned 5",
                     "Unauthorized", "401", "403"),
     "摄像头拒绝了这个连接：密码错了、通道被占满，或同时连的路数超过上限"
     "（很多家用机只允许 1~2 路并发）"),
    ("dir", ("No such file or directory", "Permission denied", "Read-only file system"),
     "录像目录写不进去：路径不存在、没有写权限，或分区已只读"),
    ("disk_full", ("No space left on device",),
     "磁盘满了，先在设置里调低保留天数或换目录"),
    ("net", ("Connection timed out", "Connection refused", "No route to host",
             "Connection reset", "Network is unreachable"),
     "网络连不上摄像头：IP / 端口写错、设备断电，或被防火墙拦了"),
    ("output", ("Error opening output file", "Invalid argument"),
     "输出文件打不开：检查录像目录是否存在且可写"),
)


def classify_error(err):
    """把 ffmpeg 的 stderr 归成 (kind, hint)。认不出就 ('other', '')。

    做这件事的唯一目的是让页面能说「下一步该干什么」，而不是把
    "Output file does not contain any stream" 原样甩给用户。
    """
    e = err or ""
    for kind, needles, hint in _ERR_RULES:
        for n in needles:
            if n in e:
                return kind, hint
    return "other", ""


def hint_of(kind):
    """错误分类 → 给用户看的下一步建议。kind 为空返回空串。"""
    if not kind:
        return ""
    for k, _needles, hint in _ERR_RULES:
        if k == kind:
            return hint
    return ""


def _mask_url(s):
    """rtsp://user:pass@host/path → rtsp://user:***@host/path。

    密码必须遮掉（诊断文件可能被导出、贴进工单），但 host 和通道路径要留着 ——
    "连不上 192.0.2.37 的主码流"和"密码错"是完全不同的两件事。
    """
    s = str(s)
    if "@" not in s or "://" not in s:
        return s
    head, _, tail = s.partition("://")
    cred, at, rest = tail.rpartition("@")
    if not at:
        return s
    if ":" in cred:
        u, _, _p = cred.partition(":")
        return "%s://%s:***@%s" % (head, u, rest)
    return "%s://***@%s" % (head, rest)


# ---------------------------------------------------------------- 录制器

class Recorder:
    """所有通道共用一个监督线程，每路一个 ffmpeg 子进程。

    不用「一路一线程」是因为要判断的东西都是全局的（跨天、停服、重连计数），
    一个 2 秒轮询的线程足够，线程数也压得住。
    """

    def __init__(self, ffmpeg, root, cameras, segment_sec=300,
                 reconnect_sec=5, log=None, enabled=True, var_dir=None,
                 frag=True):
        self.ffmpeg = ffmpeg
        self.root = root
        self.cams = [c for c in (cameras or [])
                     if c.get("url") and c.get("enabled", True)]
        self.seg_sec = segment_sec
        self.reconnect = max(2, int(reconnect_sec or 5))
        self.log = log or (lambda m: None)
        self.enabled = enabled
        # 分片 MP4（0.9.71 默认开）：硬杀/断电后已写片段仍可播。关掉即退回
        # 整片 moov 的老写法，用于播放器不兼容时的现场回退。
        self.frag = bool(frag)
        self.procs = {}          # cam_id -> {"p": Popen, "day": str, "tries": int, "next": float, "sig": str}
        self.stopped = False
        self.last_error = {}
        self.last_kind = {}      # cam_id -> 错误分类（见 classify_error），界面据此给建议
        self.bad_opts = set()    # ffmpeg 明确不认的选项名，见 _absorb_bad_opts
        self._dup = {}           # cam_id -> [上一次的错误文本, 连续重复次数]，日志去重
        # 诊断落盘必须写在 fpk 的 var 目录（TRIM_PKGVAR），不能按源码相对路径
        # 推算 —— 装到 fnOS 上之后 app/ 在 @appcenter 下、var 在 @appdata 下，
        # 相对路径会指到安装目录深处，用户导出诊断时根本找不到。
        self._diag_dir = (os.path.join(var_dir, "record-diag") if var_dir else None)
        self._status_cache = {}  # (日期目录 -> (mtime_ns, [文件名]))，只给 status() 用

    # -- 命令行选项自愈 -------------------------------------------

    def eff_opts(self):
        """当前可用的一组输入选项（已剔除 ffmpeg 明确不认的）。

        比较前两边都剥掉前导横线：bad_opts 里存的是 ffmpeg 的原始说法
        （`probesize`），INPUT_OPTS 里写的是 `-probesize`，形态不一致就会
        静默失效 —— 刚才正是这么踩的，留一层容错当保险。
        """
        bad = set(b.lstrip("-") for b in self.bad_opts)
        return tuple(o for o in INPUT_OPTS if o[0].lstrip("-") not in bad)

    def _absorb_bad_opts(self, err):
        """从 ffmpeg 的 stderr 里揪出"不认某个选项"，剔除它并返回被剔除的名字。

        返回非空表示命令行已经变了，调用方应当**立刻**重试（不等退避）：
        等 30 秒再试同一串注定失败的参数毫无意义。

        注意名字形态：ffmpeg 说的是 `Unrecognized option 'probesize'`（**不带**
        前导横线），而我们的 INPUT_OPTS 里写的是 `-probesize`。必须两边都归一化
        再比，否则这条自愈逻辑会静默失效 —— 每次都以为"不在我可控的列表里"，
        然后照旧拿同一串注定失败的参数去重连。
        """
        name = parse_bad_option(err)
        if not name:
            return None
        key = name.lstrip("-")
        if key in self.bad_opts:
            return None
        # 分片封装没有"降级参数"可换，只能整体退回整片 MP4。这一项不被认时
        # ffmpeg 是**直接退出**而不是警告 —— 留着硬顶等于五路全挂，所以必须
        # 在第一次报错时就退，且退回后照常录像（只是可播性变差）。
        if key == "segment_format_options" and self.frag:
            self.frag = False
            self.bad_opts.add(key)
            self.log("这台机器的 ffmpeg 不认 -segment_format_options，录像已退回"
                     "整片 MP4（能正常录，但被强行终止的分片需要残片抢救）")
            return key
        if not any(o[0].lstrip("-") == key for o in INPUT_OPTS):
            # 不在我们可控的列表里（用户改坏了地址、或者别的参数），照旧上报
            return None
        self.bad_opts.add(key)
        self.log("这台机器的 ffmpeg 不认 -%s，已自动去掉该参数并立即重试"
                 "（不影响录像，只是探测会更急）" % key)
        return key

    def _diag(self, cam, cmd, err):
        """把「失败命令 + 完整 stderr 尾部」留一份到 var/record-diag/。

        几百次重连里只有最后一次会被 in-memory 的 last_error 记住，而
        "Output file does not contain any stream" 这种错误，光看一行根本判断不出
        是摄像头不给关键帧、还是 ffmpeg 少了 parser。用户导诊断时带上这个文件，
        排查就不用再靠猜。
        """
        try:
            if not self._diag_dir:
                return None
            if not os.path.isdir(self._diag_dir):
                os.makedirs(self._diag_dir, exist_ok=True)
            cid = cam.get("id") or "cam"
            path = os.path.join(self._diag_dir, "%s.log" % cid)
            # 只遮密码，保留地址与路径：诊断时要看的是"连的是哪台、哪条通道"
            masked = [_mask_url(x) for x in cmd]
            tail = err[-8000:] if err else ""
            with open(path, "w", encoding="utf-8") as f:
                f.write("# %s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                        cam.get("name") or cid))
                f.write("# cmd: %s\n\n" % " ".join(masked))
                f.write(tail or "(无 stderr 输出)")
            return path
        except OSError:
            return None

    def _log_dedup(self, cid, msg):
        """同一路连续同样的错误只打前 3 次，之后每 20 次汇总一条。

        真机上摄像头断电时，5 路 × 每 30 秒一条能把日志刷成几百行同一个句子，
        用户想找别的问题根本翻不动。
        """
        prev = self._dup.get(cid)
        if prev and prev[0] == msg:
            prev[1] += 1
            if prev[1] <= 3 or prev[1] % 20 == 0:
                self.log(msg if prev[1] <= 3 else "%s（同一错误已重复 %d 次）"
                         % (msg, prev[1]))
            return
        self._dup[cid] = [msg, 1]
        self.log(msg)

    # -- 生命周期 --------------------------------------------------

    def _sig(self, cam):
        """这一路的「启动参数指纹」：url / 存储目录 / 分片长度 / 封装模式。

        以前只比对分片长度和根目录，用户改了 RTSP 地址，
        跑着的 ffmpeg 仍按老地址录 —— 表现就是「设置页改了没反应」。
        分片长度现在也是**每路**可配（cam.segment_sec），所以必须进指纹：
        否则只在设置页改了某一路的秒数，reconcile 认为"没变"就永远不重启。
        frag 同理（0.9.71）：它决定 `-movflags`，改了不重启等于没改 ——
        用户以为关掉了分片 MP4，实际还在写，排查时会完全摸不着头脑。
        """
        return "%s|%s|%s|%s" % (cam.get("url") or "",
                                cam.get("dir_name") or cam.get("id") or "",
                                cam.get("segment_sec") or 0,
                                "frag" if self.frag else "plain")

    def seg_of(self, cam):
        """这一路实际使用的分片长度：单路覆盖值优先，否则跟随全局设置。"""
        v = (cam or {}).get("segment_sec")
        if v:
            return runtime.segment_sec(v)
        return self.seg_sec

    def set_cameras(self, cams):
        """设置页改了列表/地址后由调度线程喂进来，无需重启。"""
        self.cams = [c for c in (cams or [])
                     if c.get("url") and c.get("enabled", True)]

    def start(self):
        if not self.enabled:
            return
        if not self.ffmpeg:
            self.log("录制未启动：系统里没有 ffmpeg")
            return
        if not self.cams:
            self.log("录制未启动：没有配置带 RTSP 地址的通道")
            return
        for c in self.cams:
            ensure_day(self.root, c.get("dir_name") or c["id"])
        self._spawn_all()
        self.log("录制开始：%d 路，分片 %d 秒（默认），根目录 %s"
                 % (len(self.cams), self.seg_sec, self.root))

    def _spawn_all(self):
        for c in self.cams:
            self._spawn(c)

    def _spawn(self, cam, keep_tries=False):
        cid = cam["id"]
        dname = cam.get("dir_name") or cid           # 存储目录用摄像头名称
        sig = self._sig(cam)
        day_dir = ensure_day(self.root, dname)
        if not day_dir:
            # 目录建不出来就别起进程了：ffmpeg 会因为输出打不开立刻退出，
            # 用户只看到"一条录像都没有"，错误文本埋在 stderr 里没人翻。
            msg = "录像目录不可写：%s" % os.path.join(self.root, dname)
            self.last_error[cid] = msg
            self.last_kind[cid] = "dir"
            self._log_dedup(cid, "录制 %s 未启动：%s（请在设置里换成可写目录）"
                            % (cam.get("name") or cid, msg))
            self.procs[cid] = {"p": None, "day": datetime.date.today().isoformat(),
                               "tries": 0, "next": time.time() + MAX_BACKOFF,
                               "started_at": 0, "sig": sig}
            return
        # 顺带把明天的目录也建好：ffmpeg 的 strftime 只替换路径里的 %，**不会
        # mkdir**。零点一过它就去写 /.../2026-10-22/，目录不存在 → 当场退出，
        # 表现是"每天零点整所有通道同时断一次"，很容易被当成摄像头问题。
        ensure_day(self.root, dname,
                   datetime.date.today() + datetime.timedelta(days=1))
        cmd = record_cmd(self.ffmpeg, cam["url"], out_pattern(self.root, dname),
                         self.seg_of(cam), opts=self.eff_opts(), frag=self.frag)
        prev = self.procs.get(cid) or {}
        tries = (prev.get("tries", 0) if keep_tries else 0)
        stall = (prev.get("stall", 0) if keep_tries else 0)
        # 参数变了就是"用户刚改完设置"，应当立刻用新参数起进程，不能被上一次
        # 失败留下的退避窗口挡住（否则改完 url 要干等几十秒才生效）。
        # 参数没变才沿用旧的等待时间：摄像头一直连不上时，重启服务不会把退避清零。
        sig_changed = prev.get("sig") != sig
        wait_until = 0.0 if sig_changed else float(prev.get("next") or 0)
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 start_new_session=True)
        except OSError as e:
            self.last_error[cid] = "无法启动 ffmpeg：%s" % e
            self.log("录制 %s 启动失败：%s" % (cid, e))
            self.procs[cid] = {"p": None, "day": datetime.date.today().isoformat(),
                               "tries": tries + 1, "stall": stall,
                               "next": time.time() + MAX_BACKOFF,
                               "started_at": 0, "sig": sig}
            return
        self.procs[cid] = {"p": p, "day": datetime.date.today().isoformat(),
                           "tries": tries, "stall": stall, "next": wait_until,
                           "started_at": time.time(), "dead": None,
                           "sig": sig}
        self.last_error.pop(cid, None)
        self.last_kind.pop(cid, None)
        self._dup.pop(cid, None)          # 这一路活了，去重计数清零
        self.log("录制 %s 已启动 pid=%d" % (cam.get("name") or cid, p.pid))

    def _grace_close(self, p, why=""):
        """三段式收尾：SIGINT → 等 → 再 SIGINT → 等 → 最后才 SIGKILL。

        **这是"残片"问题的唯一正解。** SIGINT 可被 ffmpeg 捕获，segment
        muxer 会立刻写完 moov 并正常退出；SIGKILL 不可捕获，进程连收尾的
        机会都没有，于是 `.mp4.part` 永久缺 moov —— 文件在、能看见、但
        任何播放器都打不开，而且 finalize_parts 永远不会认它封口。

        真机 2026-10-26 车库：12 个残片全部出自这条硬杀路径。原来只有
        subprocess 的 wait(timeout=5) 一路，卡死的进程 5 秒就吃 SIGKILL。
        现在给 15+10 秒，绝大多数情况在第一次 SIGINT 就干净退出。

        返回 True 表示是优雅退出的（moov 大概率已落地）。
        """
        if p is None or p.poll() is not None:
            return True
        soft = KILL_SOFT_SEC
        # SIGINT 必须是"发出去可能失败"的：进程刚死时 send_signal 会抛。
        # SIGKILL 只在两次 SIGINT 都不奏效（真僵死）时才发。
        for attempt, wait in enumerate((soft, KILL_HARD_SEC), 1):
            try:
                p.send_signal(signal.SIGINT)
            except (OSError, ValueError):
                return True
            try:
                p.wait(timeout=wait)
                return True               # 自己退了 = 已收尾
            except subprocess.TimeoutExpired:
                if attempt == 1:
                    continue              # 再来一次 SIGINT
                break
            except (OSError, ValueError):
                return True
        try:
            p.kill()
            p.wait(timeout=3)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if why:
            self.log(why)
        return False

    def _kill(self, cid, why=""):
        """优雅收尾一路的 ffmpeg（SIGINT 让 segment muxer 写完 moov），并从进程表移除。"""
        st = self.procs.pop(cid, None)
        if not st:
            return False
        p = st.get("p")
        if p is None:
            return True
        if p.poll() is None:
            self._grace_close(p)
        if why:
            self.log(why)
        return True


    def reconfigure(self, cfg=None, cams=None):
        """设置推送后热生效：分片长度 / 启用开关 / 摄像头列表 / 录像目录 / 单路地址。

        分片长度是 ffmpeg 命令行参数，只能重启进程：按「跨天重启」的同款方式
        优雅收尾（SIGINT 让 segment muxer 写完 moov），再以新参数 spawn。
        单路（地址 / 遮蔽区 / 存储目录 / 单路分片长度）变了，只重启那一两路。

        这里刻意**只看全局分片长度有没有变**：单路长度变没变由 reconcile 的
        指纹比对负责。如果把"任一路长度变了"也当成全局变化，改一路会牵连所有
        通道一起重启 —— 每重启一次，正在写的那个分片就被切断，白白丢掉几秒。
        """
        cfg = cfg or {}
        if cams is not None:
            self.cams = list(cams)
        want_on = bool(cfg.get("record_enable", True))
        seg = runtime.segment_sec(cfg.get("record_segment_sec"))
        new_root = (str(cfg.get("record_dir") or "").strip() or self.root)
        # frag 未配置时保持当前值：老配置文件里没有这个键，不能因此被关掉
        want_frag = bool(cfg.get("record_frag", self.frag))
        seg_changed = seg != self.seg_sec
        on_changed = want_on != self.enabled
        root_changed = new_root != self.root
        frag_changed = want_frag != self.frag
        self.seg_sec = seg
        self.enabled = want_on
        self.frag = want_frag
        if not (seg_changed or on_changed or root_changed or frag_changed):
            # 单路参数（含单路分片长度）会由下一拍 reconcile 的指纹比对处理
            return
        if root_changed:
            self.root = new_root
            try:
                os.makedirs(self.root, exist_ok=True)
            except OSError:
                pass

        # 停掉现役进程：tick 会在下个 2 秒节拍用新参数重新拉起（enabled=False 则不再拉起）
        for cid in list(self.procs.keys()):
            self._kill(cid)
        if not want_on:
            self.log("录制已停用，现有录制进程已收尾")
        else:
            if root_changed:
                self.log("录像目录改为 %s，录制按新目录重启" % self.root)
            if seg_changed:
                self.log("分片长度改为 %d 秒，录制按新参数重启" % seg)
            if frag_changed:
                self.log("录像封装改为%s，录制按新参数重启"
                         % ("分片 MP4（断电也能播）" if self.frag else "整片 MP4"))

    def reconcile(self, cams):
        """每拍调用：把「设置里的这一路」与「正在跑的这一路」对齐。

        三件事，每一件以前都漏着：
          1. 路被删/被停用 → 进程还在写盘（永远不会有人收尸）
          2. url / 遮蔽区 / 存储目录改了 → 进程还按老参数录
          3. 新增的路径由 tick 的正常分支拉起，这里不动
        """
        if cams is not None:
            self.cams = list(cams)
        if self.stopped or not self.enabled:
            return
        want = {c["id"]: c for c in self.cams if c.get("url")}
        for cid in list(self.procs.keys()):
            if cid not in want:
                self._kill(cid, "录制 %s 已移除或停用，已停止该路进程" % cid)
                self.last_error.pop(cid, None)
                continue
            st = self.procs.get(cid) or {}
            p = st.get("p")
            if p is None or p.poll() is not None:
                continue                    # 没跑起来 / 已退出：交给 tick 的退避分支
            if st.get("sig") != self._sig(want[cid]):
                self._kill(cid, "录制 %s 参数已变（地址/目录/分片长度），按新配置重启" % cid)


    def stop(self, timeout=6.0):
        self.stopped = True
        for cid, st in list(self.procs.items()):
            p = st.get("p")
            if p is None or p.poll() is not None:
                continue
            # 先 SIGINT 让 ffmpeg 自己收尾（写完 moov），再退到 SIGKILL
            try:
                p.send_signal(signal.SIGINT)
            except (OSError, ValueError):
                pass
            deadline = time.time() + timeout
            while time.time() < deadline and p.poll() is None:
                time.sleep(0.15)
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass
        # 收尾后把已封口的 .part 改成 .mp4，停服前最后一个完整分片不丢
        for c in self.cams:
            finalize_parts(self.root, c.get("dir_name") or c["id"])
        self.procs.clear()
        self.log("录制已停止")

    # -- 监督 ------------------------------------------------------

    def _rescue_part(self, src, dst):
        """抢救一个没封口的分片：`-c copy` 重建索引，成功才落到正式文件名。

        返回 False 时 finalize_parts 会把它改名 `.bad` —— 失败必须留下痕迹，
        但不能留在原名上，否则每 2 秒的巡检都会重新试一次。

        输出先写 `dst + ".rescuing"` 再原子改名：抢救中途失败（超时、
        磁盘满、ffmpeg 被杀）时，`dst` 上不会出现半个能播的文件骗过消费者。
        临时名**故意不以 .mp4 结尾**（配 `-f mp4` 显式指定容器）：录像扫描
        `list_segments` / `_scan_day_dirs` 都按 `endswith(".mp4")` 认文件，
        临时名要是也叫 .mp4，会被当成正常分片塞进回放页和 `/api/recordings`。
        """
        if not self.ffmpeg:
            return False
        tmp = dst + ".rescuing"
        try:
            # 超时给 60 秒而不是"够大就行"：抢救跑在 2 秒一轮的巡检线程里，
            # 单次阻塞太久会把其他四路的看门狗一起拖住（它们靠这个线程重启）。
            # 一个 5 分钟分片约 150 MB，-c copy 在 NAS 上也就几秒，60 秒是
            # 给慢盘留的余量；真超过说明盘出问题，改名 .bad 让用户去看。
            r = subprocess.run(
                ffmpeg_cmd.rescue_cmd(self.ffmpeg, src, tmp),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            self._drop(tmp)
            return False
        ok = False
        if r.returncode == 0:
            try:
                # 只要索引能重建，输出必然有内容；4 KB 以下是"只有一个
                # 空 moov"的典型体积，视为没救回东西，别拿它冒充录像。
                ok = os.path.getsize(tmp) > 4096
            except OSError:
                ok = False
        if not ok:
            self._drop(tmp)
            err = ""
            try:
                err = " ".join((r.stderr or b"").decode("utf-8", "replace").split())[:160]
            except Exception:
                pass
            self.log("残片抢救失败 %s%s" % (os.path.basename(src),
                                          ("：" + err) if err else ""))
            return False
        try:
            os.replace(tmp, dst)
        except OSError:
            self._drop(tmp)
            return False
        try:
            mb = os.path.getsize(dst) / 1048576.0
        except OSError:
            mb = 0.0
        self.log("残片抢救成功 %s（%.1f MB）" % (os.path.basename(dst), mb))
        return True

    @staticmethod
    def _drop(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _newest_age(self, cam):
        """最新分片「多久没被写过」（秒）；一个分片都没有就返回 None。

        只 stat 当天的目录：跨天看门狗本来就该放它过去，新旧两种目录
        （摄像头名称 / 旧 id）都要看，否则升级后刚换目录的那一路会误判。
        """
        cid = cam["id"]
        newest = 0.0
        for d in dict.fromkeys([cam.get("dir_name") or cid, cid]):
            day = os.path.join(self.root, d, datetime.date.today().isoformat())
            try:
                names = [n for n in os.listdir(day)
                         if n.lower().endswith(".mp4") or n.lower().endswith(".part")]
            except OSError:
                continue
            for n in names:
                try:
                    newest = max(newest, os.stat(os.path.join(day, n)).st_mtime)
                except OSError:
                    continue
        if not newest:
            return None
        return max(0.0, time.time() - newest)

    def tick(self, now=None):
        """一次巡检：收尸、退避重连、跨天换目录。调用方按 2 秒节奏调。

        退避必须靠 `dead` 标记去重：一次死亡只记一次账，
        否则每轮巡检都会重新 spawn，摄像头下线时会把进程表刷成雪崩。
        """
        if not self.enabled or self.stopped:
            return
        if not self.ffmpeg:          # 没找到 ffmpeg（比如开发机）就整轮跳过，别刷异常
            return
        now = now if now is not None else time.time()
        today = datetime.date.today().isoformat()
        # 残片抢救预算：整轮（所有摄像头）共享一个额度，避免一次巡检里
        # 五路同时开啃、把磁盘 IO 从正在录的进程手里抢走。
        budget = [PART_RESCUE_PER_RUN]
        for cam in self.cams:
            cid = cam["id"]
            dname = cam.get("dir_name") or cid
            finalize_parts(self.root, dname, rescue=self._rescue_part,
                           budget=budget)   # 先封口/抢救，消费者才看得到完整 .mp4
            st = self.procs.get(cid)

            if st is None:
                self._spawn(cam)          # 首次，或启动失败过、退避已到期
                continue

            p = st.get("p")
            if p is None:                 # Popen 都没起来
                if now >= st.get("next", 0):
                    self._spawn(cam, keep_tries=True)
                continue

            code = p.poll()

            if code is None:
                # 跨天：主动重启一次，输出的日期目录才会跟着走
                if st["day"] != today:
                    self._grace_close(p)
                    self.procs.pop(cid, None)
                    self.log("录制 %s 跨天重启" % cid)
                    self._spawn(cam)
                    continue
                # 稳定运行满 5 分钟，把重试计数清零，避免白天抖动累积成 30 秒退避
                if now - st.get("started_at", now) > 300:
                    if st.get("tries"):
                        st["tries"] = 0
                    if st.get("stall"):
                        st["stall"] = 0
                # ---- 卡死看门狗 ----
                # ffmpeg 的 -rw_timeout 只能覆盖"读不到数据"的一部分情形；无线
                # 摄像头半死连接上它仍可能长期阻塞。这里再兜一层：进程在、但磁盘
                # 上最新分片已经 STALL_SEC 秒没长过，就判定为假活，杀掉重连。
                if now - st.get("started_at", now) > STALL_SEC + 30:
                    age = self._newest_age(cam)
                    # age is None = 这一路运行超过 STALL_SEC+30 秒却一个字节都没落盘
                    # （建连握手成功但拿不到码流）。以前这里直接放行，而删掉
                    # -timeout 之后就再没人管这种"假活"进程了 —— 必须在这里兜住。
                    if age is None or age > STALL_SEC:
                        # 卡住与「连不上」分开计数：卡住说明流本来是通的，
                        # 不该把连不上的长退避（300 秒）背到这一路身上 ——
                        # 那样每次抖动都要等 5 分钟，白丢录像（0.9.73）。
                        stall = st.get("stall", 0) + 1
                        wait = stall_wait(stall, self.reconnect)
                        self.log("录制 %s 卡住（%s 秒没有新数据），重启进程"
                                 "（第 %d 次，%d 秒后重连）"
                                 % (cam.get("name") or cid,
                                    "从未写入" if age is None else int(age),
                                    stall, wait))
                        # 这里以前是 p.kill() —— 直接 SIGKILL。而"卡住"绝大多数
                        # 只是 socket read 阻塞，进程本身是活的，SIGINT 能打断
                        # read 并让 segment muxer 写完 moov。硬杀的结果就是
                        # 这个分片永久停在 .part（真机 12 个残片全出自这里）。
                        closed = self._grace_close(p)
                        if not closed:
                            self.log("录制 %s 收尾超时（SIGINT 两次无响应），已强制结束"
                                     % (cam.get("name") or cid))
                        self.procs[cid] = {
                            "p": None, "day": st["day"],
                            "tries": st.get("tries", 0), "stall": stall,
                            "next": now + wait, "started_at": now, "dead": None,
                            "sig": st.get("sig"),
                        }
                        continue
                continue

            # ---- 进程已退出 ----
            if st.get("dead") == p.pid:
                if now >= st.get("next", 0):
                    self._spawn(cam, keep_tries=True)
                continue

            st["dead"] = p.pid
            st["tries"] = st.get("tries", 0) + 1
            err = ""
            try:
                err = (p.stderr.read(65536) or b"").decode("utf-8", "replace").strip()
            except (OSError, ValueError):
                pass

            # ---- 选项自愈：ffmpeg 说它不认某个参数，就删掉立刻重试 ----
            # 这是 0.9.11(-rw_timeout) / 0.9.13(-timeout) 两次事故的教训：
            # 精简版 ffmpeg 支持哪些参数无法预知，只能听它自己说。
            if self._absorb_bad_opts(err):
                st["tries"] = max(0, st["tries"] - 1)   # 这不是摄像头的错，不算一次失败
                st["next"] = 0                          # 0 = 下一轮巡检立刻重起
                continue

            kind, hint = classify_error(err)
            self.last_kind[cid] = kind
            if err:
                # 压成一行保留**整段**错误：ffmpeg 的 "Unrecognized option 'x'"
                # 和 "Error opening output" 都可能只在其中某一行，只留最后一行
                # 会把真正的病因丢掉，用户只能看到"中断"两个字。
                self.last_error[cid] = " ".join(err.split())[:300]
                self._diag(cam, record_cmd(self.ffmpeg, cam["url"],
                                           out_pattern(self.root, dname),
                                           self.seg_of(cam), opts=self.eff_opts()), err)
            # 指数封顶用的是 `min(tries-1, 9)` 而不是 4：4 意味着 5×2⁴=80 秒就
            # 到顶了（2 的指数不再增长），MAX_BACKOFF=300 形同虚设 —— 真机
            # 2026-10-26 车库日志里"卡住→重启"精确地每 80 秒一次，越杀越连不上，
            # 每次重启都新开一个分片，残片就是这么滚出来的。现在 5→10→20→40→
            # 80→160→300 封顶。公式收进 backoff_wait()，别再散落成两份。
            wait = backoff_wait(st["tries"], self.reconnect)
            st["next"] = now + wait
            self._log_dedup(cid, "录制 %s 中断（code=%s，%s），%d 秒后第 %d 次重连%s"
                            % (cam.get("name") or cid, code, kind or "未分类",
                               wait, st["tries"],
                               ("：" + self.last_error[cid]) if err else ""))

    def status(self):
        out = {"enabled": self.enabled, "segment_sec": self.seg_sec,
               "root": self.root, "channels": [],
               # 没 ffmpeg 是所有通道都"未运行"的最常见原因，
               # 单独给一个标记，界面才能说清楚，而不是让用户猜
               "ffmpeg_missing": not self.ffmpeg,
               "ffmpeg": self.ffmpeg or ""}
        # 分片列表只活在内存里，命中目录 mtime 就零成本；这里用独立的缓存，
        # 不跟 tick 的看门狗抢状态
        cache = self._status_cache
        for cam in self.cams:
            cid = cam["id"]
            st = self.procs.get(cid)
            p = st.get("p") if st else None
            running = bool(p and p.poll() is None)
            segs = list_segments(self.root, cam.get("dir_name") or cid, cache)
            last = segs[-1] if segs else None
            age = None
            if last:
                try:
                    age = int(time.time() - os.path.getmtime(last))
                except OSError:
                    age = None
            out["channels"].append({
                "id": cid, "name": cam.get("name") or cid,
                "state": "recording" if running else ("idle" if self.enabled else "off"),
                "pid": p.pid if p else None,
                "retry": st["tries"] if st else 0,
                "stall": st.get("stall", 0) if st else 0,
                "segments": len(segs),
                "segment_sec": self.seg_of(cam),
                "last_seg": os.path.basename(last) if last else None,
                "last_age_sec": age,
                "error": self.last_error.get(cid),
                # kind 是机器可读分类，hint 是给用户看的下一步建议。
                # 页面不再把 ffmpeg 的英文原文当正文，原文只在"详情"里展示。
                "kind": self.last_kind.get(cid),
                "hint": hint_of(self.last_kind.get(cid)),
            })
        out["bad_opts"] = sorted(self.bad_opts)
        return out
