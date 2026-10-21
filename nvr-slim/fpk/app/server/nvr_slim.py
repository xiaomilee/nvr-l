#!/usr/bin/env python3
"""NVR 后端骨架。

当前只做三件真事：核显自检、配置读写、并发槽位——先把资源边界立住，
扫描与分析在 worker 里接（下一步）。其余接口返回结构化占位数据。
"""
import argparse
import json
import mimetypes
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runtime  # noqa: E402
import params   # noqa: E402
import retention  # noqa: E402
import recorder  # noqa: E402
import condense  # noqa: E402
import link as link_mod  # noqa: E402
import snapshots  # noqa: E402
import sysinfo  # noqa: E402
import ffmpeg_cmd  # noqa: E402

STATE = {
    "started_at": time.time(),
    "app_dir": ".",
    "var_dir": ".",
    "ui_dir": ".",
    "port": 18080,
    "pool": None,
    "gpu_cache": None,
    "ffmpeg_bin": None,
    "ffprobe_bin": None,
    "shutdown": False,
}

# MJPEG 实时流的并发闸：最多 2 路（子码流转发是实打实的 CPU 开销）
_STREAM_LOCK = threading.Lock()

# ---------------------------------------------------------------- 运行日志
# 诊断日志一条不落：内存环形缓冲（前端直接读）+ 落盘文件（重启后还能看）。
# 快照失败、录制异常、调度报错都走这里，真机「无画面」先看它。
_LOG = deque(maxlen=800)


def _log_path():
    return os.path.join(STATE["var_dir"], "log", "nvr.log")


# RTSP 地址里的 user:pass 一律脱敏成 ***，日志可放心发给开发者或贴出来
_CRED = re.compile(r'(rtsp://)[^@/\s]+@')


def _tb_where():
    """当前异常最内层一帧的「文件:行号 in 函数 → 」。

    以前异常只记 %r（"IndexError('tuple index out of range')"），既不知道在
    哪个文件，也不知道哪一行 —— 用户把日志发过来，能定位的范围是"整个后端"。
    记下最内层帧之后，一句话就能指到 sysinfo.py:67。
    """
    try:
        import traceback
        tb = traceback.extract_tb(sys.exc_info()[2])
        if not tb:
            return ""
        f = tb[-1]
        return "%s:%d in %s() → " % (os.path.basename(f.filename), f.lineno, f.name)
    except Exception:
        return ""


def _fmt_exc(e):
    return "%s: %s" % (type(e).__name__, e)


def _record(line):
    line = _CRED.sub(r'\1***@', line)
    _LOG.append(line)
    print(line, flush=True)
    # 常驻句柄：高频日志（请求行、快照失败）每行 open/close 一次，
    # 在 NAS 的机械盘上就是实打实的多余 I/O。
    # 但句柄必须跟着 var_dir 走（测试与切目录场景会改 STATE），路径变了就重开。
    global _LOG_FH, _LOG_FH_PATH
    p = _log_path()
    try:
        if _LOG_FH is None or _LOG_FH_PATH != p:
            try:
                if _LOG_FH is not None:
                    _LOG_FH.close()
            except (OSError, ValueError):
                pass
            os.makedirs(os.path.dirname(p), exist_ok=True)
            _LOG_FH = open(p, "a", encoding="utf-8", buffering=1)
            _LOG_FH_PATH = p
        _LOG_FH.write(line + "\n")
    except (OSError, ValueError):
        try:
            if _LOG_FH is not None:
                _LOG_FH.close()
        except (OSError, ValueError):
            pass
        _LOG_FH = None
        _LOG_FH_PATH = None


_LOG_FH = None
_LOG_FH_PATH = None


def log_line(msg):
    """业务日志入口：统一加 [nvr] 前缀与时间戳。"""
    _record("[nvr] %s %s" % (time.strftime("%H:%M:%S"), msg))


def log_clear():
    """清空内存缓冲与落盘文件（用户点「清除日志」时用）。"""
    global _LOG_FH, _LOG_FH_PATH
    _LOG.clear()
    try:
        if _LOG_FH is not None:
            _LOG_FH.close()
    except (OSError, ValueError):
        pass
    _LOG_FH = None
    _LOG_FH_PATH = None
    try:
        p = _log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass


def log_export_text():
    """诊断导出：内存里的全量日志 + 关键运行时状态，凭据统一脱敏。"""
    cfg = runtime.load_config(STATE["var_dir"])
    g = gpu()
    head = [
        "==== NVR 诊断导出 ====",
        "时间      : %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "版本      : %s" % runtime.VERSION,
        "PID       : %d" % os.getpid(),
        "监听      : %s:%s" % (STATE.get("listen"), STATE.get("port")),
        "ffmpeg    : %s" % (STATE.get("ffmpeg_bin") or "未找到"),
        "ffprobe   : %s" % (STATE.get("ffprobe_bin") or "未找到"),
        "核显      : %s（%s）" % (g.get("state"), g.get("reason")),
        "录像目录  : %s" % (cfg.get("record_dir") or "-"),
        "分片长度  : %s 秒" % cfg.get("record_segment_sec"),
        "衔接模式  : %s" % cfg.get("link_mode"),
        "并发      : %s" % cfg.get("concurrency"),
        "==== 通道 ====",
    ]
    for c in cameras():
        head.append("- %s [%s] url=%s sub=%s 录制=%s 分片=%s 秒 保留=%s/%s 天 忽略区=%s 格" % (
            c.get("name"), c.get("id"),
            runtime.redact_url(c.get("url")) or "(空)",
            runtime.redact_url(c.get("sub_url")) or "(空)",
            "开" if c.get("enabled", True) else "关",
            c.get("segment_sec") or "默认",
            c.get("full_days"), c.get("clip_days"),
            len((c.get("masks") or {}).get("ignore") or [])))
    body = ["==== 日志（最近 %d 条）====" % len(_LOG)] + list(_LOG)
    # 录制失败的命令行 + 完整 stderr：日志里只有一行摘要，排"为什么录不上"
    # 必须看到原始的 Stream mapping 和 ffmpeg 完整说法。这里一并带上，
    # 用户点一次"导出诊断"就够，不用再教他去某个目录捞文件。
    diag_dir = os.path.join(STATE["var_dir"], "record-diag")
    if os.path.isdir(diag_dir):
        for n in sorted(os.listdir(diag_dir)):
            if not n.endswith(".log"):
                continue
            try:
                with open(os.path.join(diag_dir, n), "r", encoding="utf-8",
                          errors="replace") as f:
                    body.append("\n==== 录制诊断：%s ====" % n)
                    body.append(f.read()[-12000:])
            except OSError:
                continue
    text = "\n".join(head + body)
    return _CRED.sub(r'\1***@', text)



def gpu(force=False):
    if force or STATE["gpu_cache"] is None:
        STATE["gpu_cache"] = runtime.gpu_report(STATE["app_dir"],
                                                runtime.load_config(STATE["var_dir"]))
    return STATE["gpu_cache"]


# ---------------------------------------------------------------- 直播快照
# 直播页默认只发光快照：每路每 N 秒解 1 帧存成 jpg，前端按 age 倒计时。
# 这样打开应用不占解码、不占带宽；用户点了某一路才真正拉子码流。

CAMERA_DEFAULTS = [
    {"id": "cam-01", "name": "车库入口", "online": True, "full_days": 4, "clip_days": 90},
    {"id": "cam-02", "name": "一楼大堂", "online": True, "full_days": 4, "clip_days": 90},
    {"id": "cam-03", "name": "货梯走廊", "online": True, "full_days": 2, "clip_days": 180},
    {"id": "cam-04", "name": "后门通道", "online": True, "full_days": 4, "clip_days": 90},
]


_CLIP_SPEEDS = (1, 2, 4, 8)


def clip_speed_of(v):
    """缩时倍速白名单：只认 1/2/4/8，其它一律回落 1。

    倍速走重编码，CPU 是真代价；放任意整数进来（比如 3、7）只会让用户
    误以为"倍数越怪省得越多"，实际收益和 2/4 没区别。
    """
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 1
    return n if n in _CLIP_SPEEDS else 1


def norm_cameras(raw):
    """设置页推送的摄像头列表 → 干净结构。id 只留安全字符，防止穿目录。"""
    out = []
    for i, c in enumerate(raw or []):
        if isinstance(c, str):
            c = {"name": c}
        elif not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid or any(not (ch.isalnum() or ch in "-_") for ch in cid):
            cid = "cam-%02d" % (i + 1)
        out.append({
            "id": cid,
            "name": str(c.get("name") or ("摄像头 %d" % (i + 1))),
            "url": str(c.get("url") or c.get("main") or "").strip(),
            "sub_url": str(c.get("sub_url") or c.get("sub") or "").strip(),
            "enabled": bool(c.get("enabled", True)),
        })
        if c.get("full_days") is not None:
            out[-1]["full_days"] = c.get("full_days")
        if c.get("clip_days") is not None:
            out[-1]["clip_days"] = c.get("clip_days")
        # 每路缩时压制总开关：关掉这一路就不再产出缩时片段（全时录像照常）。
        # 字符串 "false"/"off" 也要认：前端一不小心推成字符串是常事。
        out[-1]["condense"] = str(c.get("condense", True)).strip().lower() \
            not in ("0", "false", "off", "no")
        # 缩时倍速：只认 1/2/4/8，别的值一律回落 1（见 clip_speed_of）
        out[-1]["clip_speed"] = clip_speed_of(c.get("clip_speed"))
        # 单路分片长度：只在显式给了正数时才写，0/缺失表示"跟随通用设置"。
        # 写进来的必须是夹取过的值，否则前端传个 5 秒会让录制器疯狂切文件。
        if c.get("segment_sec"):
            out[-1]["segment_sec"] = runtime.segment_sec(c.get("segment_sec"))
        # 画面区域遮罩（16×9 网格：ignore=检测忽略区，区域内变化不算事件）
        m = c.get("masks")
        if isinstance(m, dict):
            out[-1]["masks"] = {
                "ignore": [int(x) for x in (m.get("ignore") or []) if isinstance(x, int) or str(x).isdigit()],
            }
    return out


_DIR_BAD = '\\/:*?"<>|'


def _safe_dir_name(name, fallback, used):
    """摄像头名称 → 安全的存储目录名。

    规则：去掉文件系统非法字符与控制字符、去掉首尾空白和点、截到 40 字；
    结果为空或与已有目录冲突时回退 id，仍冲突则加 -2/-3 后缀去重。
    """
    s = "".join(ch for ch in str(name or "")
                if ch.isprintable() and ch not in _DIR_BAD).strip().strip(".")
    s = s[:40].strip().rstrip(".")
    base = s or str(fallback)
    if base in used or base in (".", ".."):
        cand, n = base, 2
        while cand in used:
            cand = "%s-%d" % (base, n)
            n += 1
        base = cand
    used.add(base)
    return base


def cameras():
    cfg = runtime.load_config(STATE["var_dir"])
    raw = cfg.get("cameras") or CAMERA_DEFAULTS
    out = []
    used_dirs = set()
    for i, c in enumerate(raw):
        if isinstance(c, str):
            c = {"name": c}
        elif not isinstance(c, dict):
            continue
        cid = c.get("id") or "cam-%02d" % (i + 1)
        name = c.get("name") or "通道 %d" % (i + 1)
        out.append({
            "id": cid,
            "name": name,
            # 存储目录用摄像头名称（如「客厅」），id 仅作旧数据兼容
            "dir_name": _safe_dir_name(name, cid, used_dirs),
            "online": bool(c.get("online", c.get("enabled", True))),
            "enabled": c.get("enabled", True),
            # 录制源：主码流落盘，子码流给预览。留空表示这一路不录
            "url": str(c.get("url") or "").strip(),
            "sub_url": str(c.get("sub_url") or "").strip(),
            "full_days": retention.clamp_days("full_days",
                                              c.get("full_days", retention.DEFAULTS["full_days"])),
            "clip_days": retention.clamp_days("clip_days",
                                              c.get("clip_days", retention.DEFAULTS["clip_days"])),
            # 每路缩时压制总开关与倍速（见 norm_cameras 的说明）
            "condense": str(c.get("condense", True)).strip().lower()
                        not in ("0", "false", "off", "no"),
            "clip_speed": clip_speed_of(c.get("clip_speed")),
            # 单路分片长度（秒）。0/缺省 = 跟随「通用设置」里的 record_segment_sec。
            # 理由：分片长度是**写进 ffmpeg 命令行的物理参数**，不是界面偏好；
            # 想让"改了立刻生效"真正成立，就得让它成为每路可配、可被录制器比对的值。
            "segment_sec": runtime.segment_sec(c.get("segment_sec"))
                           if c.get("segment_sec") else 0,
        })
        # 检测忽略区要一路透传到压制判定（condense），否则「设置页画了、判定不理」
        m = c.get("masks")
        if isinstance(m, dict):
            out[-1]["masks"] = {
                "ignore": [int(x) for x in (m.get("ignore") or [])
                           if isinstance(x, int) or str(x).isdigit()],
            }
    return out or [dict(c) for c in CAMERA_DEFAULTS]


def cam_storage_dirs(c):
    """一路摄像头实际可能存数据的目录名：新 dir_name + 旧 id，去重保序。"""
    dn = c.get("dir_name") or c["id"]
    return list(dict.fromkeys([dn, c["id"]]))


def snapshot_path(cam_id):
    return os.path.join(STATE["var_dir"], "snapshots", cam_id + ".jpg")


def live_payload():
    """快照墙的状态：每路年龄 + 是否卡住 + 宫格列数。

    只返回元数据，图片走 /api/live/snapshot/<id>.jpg?t=<epoch>，
    带 t 是为了让浏览器每次刷新都真的取新图，而不是吃缓存。
    """
    cfg = runtime.load_config(STATE["var_dir"])
    interval = max(1, min(10, int(cfg.get("snapshot_interval") or 10)))
    now = time.time()
    cams = []
    snap_status = STATE["snapshots"].status() if STATE.get("snapshots") else {}
    for c in cameras():
        p = snapshot_path(c["id"])
        age = int(max(0, now - os.path.getmtime(p))) if os.path.isfile(p) else None
        cams.append({
            "id": c["id"],
            "name": c["name"],
            "online": c["online"],
            "age_sec": age,
            "stale": age is None or age > interval * 2.2,
            "snap": snap_status.get(c["id"]),   # True 成功 / False 失败 / None 没试过
            "snapshot": "/api/live/snapshot/%s.jpg?t=%d" % (c["id"], int(now)),
        })
    return {
        "ok": True,
        "interval": interval,
        "cameras": cams,
        "grid_cols": params.grid_cols(len(cams)),
        "hint": "快照每 %d 秒刷新，点任意一路开始实时播放" % interval,
    }


# ---------------------------------------------------------------- 保留与清理
# 每个摄像头两类数据各有周期：全时录像（体积大、天数短）/ 缩时片段（体积小、天数长）。
# 清理是**每天固定时刻按日期整目录删**，不是"倒数 72 小时"的滚动窗口。

def record_dir():
    """录像根目录。回退值必须和 runtime.DEFAULTS 用同一个来源。

    以前这里写死 `/vol2/1000/NVR`，而 `runtime.DEFAULTS["record_dir"]` 是
    `/vol1/@appshare/nvr-output`。正常情况下 load_config() 从 DEFAULTS 起步，
    回退永远走不到；但只要配置文件里 record_dir 被写成空串或 null（手改、
    旧版本迁移、写盘中断），两处就会分叉 —— 设置页显示 /vol1，录制器实际写
    /vol2，录像"凭空消失"且日志一切正常。这类问题排查成本极高，直接消灭分叉。
    """
    cfg = runtime.load_config(STATE["var_dir"])
    return cfg.get("record_dir") or runtime.DEFAULTS["record_dir"]


_RES_ERR = {"n": 0, "last": 0.0}


def _safe_res(gpu_report=None):
    """采集资源快照；任何子项出错都降级成"读不到"，不让 /api/status 整体失败。

    真机上曾经因为 sysinfo 里一个 IndexError（_TICK 初始化少一个字段），
    /api/status 每 3 秒 500 一次，页面上的摄像头列表、录制状态、看板一起空白。
    附加信息失败不该拖垮主接口 —— 这条边界必须在这里画清楚。
    """
    try:
        return sysinfo.snapshot(STATE.get("ffmpeg_bin"),
                                record_dir=record_dir(), gpu_report=gpu_report)
    except Exception as e:
        _RES_ERR["n"] += 1
        now = time.time()
        if now - _RES_ERR["last"] > 60:
            _RES_ERR["last"] = now
            _record("资源采集失败（累计 %d 次）：%s%s"
                    % (_RES_ERR["n"], _tb_where(), _fmt_exc(e)))
        return {"proc_cpu": None, "sys_cpu": None, "mem_mb": None,
                "mem_total_mb": None, "disk_free_gb": None, "disk_total_gb": None,
                "hw": {}, "error": _fmt_exc(e)}


_PROBE_LOCK = threading.Lock()


def _eff_opts():
    """当前生效的拉流输入选项（已剔除本机 ffmpeg 不认的）。"""
    rec = STATE.get("recorder")
    try:
        return rec.eff_opts() if rec is not None else recorder.INPUT_OPTS
    except AttributeError:
        return recorder.INPUT_OPTS


def _probe_one(cam, ff, fp, root=None, seg_sec=None):
    """读流信息 + 试录 8 秒，返回一份自证材料。

    root / seg_sec 缺省时取常驻录制器的真实值（测试时可显式传入打桩）。
    """
    cid = cam.get("id")
    url = cam.get("url") or ""
    rep = {"ok": True, "id": cid, "name": cam.get("name") or cid,
           "url": recorder._mask_url(url), "ffmpeg": ff, "ffprobe": fp or "",
           "streams": [], "probe_err": "", "probe_ok": False,
           "trial_ok": False, "trial_files": 0, "trial_bytes": 0,
           "trial_log": "", "trial_err": "", "kind": "", "verdict": "", "hint": ""}

    # ---- 1) 这条流里到底有什么 ----
    if fp:
        try:
            r = subprocess.run([fp, "-v", "error", "-rtsp_transport", "tcp",
                                "-show_streams", "-show_format", "-of", "json",
                                "-i", url], capture_output=True, timeout=25)
            rep["probe_ok"] = True
            raw = (r.stdout or b"").decode("utf-8", "replace")
            rep["probe_err"] = _CRED.sub(r"\1***@",
                                         (r.stderr or b"").decode("utf-8", "replace")
                                         ).strip()[-500:]
            try:
                js = json.loads(raw) if raw.strip() else {}
            except ValueError:
                js = {}
            for s in (js.get("streams") or []):
                rep["streams"].append({
                    "type": s.get("codec_type") or "",
                    "codec": s.get("codec_name") or "",
                    "profile": s.get("profile") or "",
                    "w": s.get("width"), "h": s.get("height"),
                    "fps": s.get("avg_frame_rate") or s.get("r_frame_rate") or "",
                })
            fmt = js.get("format") or {}
            rep["format"] = fmt.get("format_name") or ""
        except subprocess.TimeoutExpired:
            rep["probe_err"] = "ffprobe 25 秒没有返回（摄像头没响应 / 网络不通 / 通道被占）"
        except OSError as e:
            rep["probe_err"] = "ffprobe 无法执行：%s" % e

    # ---- 2) 真录 8 秒，看 ffmpeg 自己怎么说 ----
    # 试录必须走**正式录制的真实路径**：真实的录制根目录、摄像头目录、日期
    # 子目录、.part 后缀和分片长度，只差 -t 8 / -v info / 文件名前缀三个
    # 不影响结论的项。0.9.15 的真机教训：试录写到 var/probe/ 下成功了，
    # 5 路正式录制却全灭 —— 两者唯一差异只剩输出路径，而试录证明不了它。
    # 现在试录成功 = 正式路径整条链路可用；试录失败 = 报错就是正式录制的报错。
    rec = STATE.get("recorder")
    if root is None:
        root = rec.root if rec is not None else record_dir()
    if seg_sec is None:
        try:
            seg_sec = rec.seg_of(cam) if rec is not None else 300
        except Exception:
            seg_sec = 300
    dname = cam.get("dir_name") or cam.get("id")
    day_full = recorder.cam_dir(root, dname)
    if not recorder.ensure_day(root, dname):
        rep["trial_err"] = "录制目录不可写：%s" % day_full
        rep["kind"] = "dir"
        rep["verdict"] = "能连上，但录像目录写不进去"
        rep["hint"] = "检查设置里的保存目录是否存在、是否有写权限"
        return rep
    # 文件名加 __probe__ 前缀：路径形态（日期子目录、.part）与正式录制一致，
    # 但能和常驻录制正在写的分片区分开，跑完只清自己的产物，不碰真实录像。
    pattern = os.path.join(root, dname, "%Y-%m-%d", "__probe__-%H-%M-%S.mp4.part")
    try:
        cmd = list(recorder.record_cmd(ff, url, pattern, seg_sec,
                                       opts=_eff_opts()))
        # 诊断要看 Stream mapping，-v error 会把最关键的那几行吞掉
        i = cmd.index("-v")
        cmd[i + 1] = "info"
        # 试录只跑 8 秒：够拿到一个关键帧和一个分片，又不至于占着摄像头不放
        j = cmd.index("-c")
        cmd[j:j] = ["-t", "8"]
        r = subprocess.run(cmd, capture_output=True, timeout=40)
        err = (r.stderr or b"").decode("utf-8", "replace")
        err = _CRED.sub(r"\1***@", err)
        rep["trial_err"] = err.strip()[-4000:]
        rep["trial_rc"] = r.returncode
        # Stream mapping 那几行是"到底有没有输出流"的铁证，单独拎出来
        rep["mapping"] = [ln.strip() for ln in err.splitlines()
                          if "Stream mapping" in ln or "->" in ln][:6]
        files = []
        try:
            files = [n for n in os.listdir(day_full)
                     if n.startswith("__probe__-")]
        except OSError:
            pass
        rep["trial_files"] = len(files)
        rep["trial_bytes"] = 0
        for n in files:
            try:
                rep["trial_bytes"] += os.path.getsize(os.path.join(day_full, n))
            except OSError:
                pass
        rep["trial_ok"] = bool(files) and rep["trial_bytes"] > 2048
        # 试录产物当场清掉，不在录像时间线里留下 8 秒的"诊断碎片"
        for n in files:
            try:
                os.remove(os.path.join(day_full, n))
            except OSError:
                pass
    except subprocess.TimeoutExpired:
        rep["trial_err"] = "试录 40 秒没有结束（多半卡在建连或等关键帧）"
    except (OSError, ValueError) as e:
        rep["trial_err"] = "试录无法启动：%s" % e

    # ---- 3) 结论 ----
    rep["kind"], hint = recorder.classify_error(rep["trial_err"])
    has_video = any(s["type"] == "video" for s in rep["streams"])
    codecs = sorted(set(s["codec"] for s in rep["streams"]
                        if s["type"] == "video" and s["codec"]))
    if rep["trial_ok"]:
        rep["verdict"] = "这条通道可以正常录制（用正式录制目录试录成功，已生成分片）"
        rep["hint"] = ""
        # 试录成功但常驻录制没在录：摄像头和目录都被试录证明没问题，
        # 剩下的解释就是上一次失败的退避还没走完（最长 5 分钟）。
        if rec is not None:
            try:
                st = (rec.procs or {}).get(cam.get("id")) or {}
                alive = st.get("p") is not None and st["p"].poll() is None
                if not alive:
                    wait = max(0, int(st.get("next", 0) - time.time()))
                    rep["hint"] = (
                        "试录成功，说明摄像头和录制目录都没问题。"
                        "常驻录制此刻在重连等待（约 %d 秒后自动重试）；"
                        "若重试后状态仍未恢复，点「导出诊断」把 ffmpeg 原文发来"
                        % wait)
            except (AttributeError, OSError, ValueError):
                pass
    elif rep["kind"] == "no_stream" and not has_video:
        rep["verdict"] = "摄像头没有给出视频流"
        rep["hint"] = ("ffprobe 也没看到视频流：地址可能指向了音频通道或错误路径，"
                       "先核对 RTSP URL 里的通道号")
    elif rep["kind"] == "no_stream":
        rep["verdict"] = "摄像头有视频流，但 ffmpeg 拿不到可用的关键帧/参数集"
        rep["hint"] = ("先换这一路的子码流试试（子码流一般是 H.264 且关键帧更密）；"
                       "如果换子码流能录，说明主码流的编码或参数集这台 ffmpeg 处理不了")
    elif rep["kind"] == "codec":
        rep["verdict"] = "这台 ffmpeg 缺少该编码格式的封装支持"
        rep["hint"] = ("检测到的视频编码：%s。换子码流（通常 H.264）一般可用；"
                       "或者把这个静态 ffmpeg 换成完整构建" % ("、".join(codecs) or "未知"))
    elif rep["kind"] == "rtsp_reject":
        rep["verdict"] = "摄像头拒绝了这个连接"
        rep["hint"] = ("核对账号密码；确认没有别的软件（含本应用的其他通道）"
                       "正在连同一路 —— 很多家用机只允许 1~2 路并发")
    elif rep["kind"] == "dir":
        rep["verdict"] = "能连上，但录像目录写不进去"
        rep["hint"] = "检查设置里的保存目录是否存在、是否有写权限"
    elif not rep["trial_err"]:
        rep["verdict"] = "试录没有产出，也没拿到错误输出"
        rep["hint"] = "把这份诊断导出发给开发者"
    else:
        rep["verdict"] = "试录失败：" + (rep["kind"] or "未分类")
        rep["hint"] = hint
    return rep


def camera_cfg():
    cfg = runtime.load_config(STATE["var_dir"])
    cams = cameras()
    return cfg, cams, {c["id"]: c for c in cams}


def _scan_day_dirs(base):
    """扫一个「摄像头/日期/」目录树：{day: [{n,b}]}，只认 YYYY-MM-DD 目录下的 mp4。"""
    days = {}
    if not os.path.isdir(base):
        return days
    try:
        day_names = [d for d in os.listdir(base)
                     if len(d) == 10 and d[4] == "-" and d[7] == "-"]
    except OSError:
        return days
    for d in sorted(day_names):
        full = os.path.join(base, d)
        if not os.path.isdir(full):
            continue
        files = []
        try:
            for n in sorted(os.listdir(full)):
                if not n.lower().endswith(".mp4"):
                    continue
                p = os.path.join(full, n)
                if os.path.isfile(p):
                    files.append({"n": n, "b": os.path.getsize(p), "p": p})
        except OSError:
            continue
        if files:
            days[d] = files
    return days


# 时长缓存：key = 绝对路径, value = ((size, mtime_ns), seconds|None)
# 缩时片段一写完就不会再变，缓存命中率接近 100%；录像分片也只在封口后读一次。
_DUR_CACHE = {}
_DUR_LOCK = threading.Lock()


def file_duration(path, stat=None):
    """文件的真实播放时长（秒），读不到返回 None。

    只给「需要精确时间轴」的地方用（缩时片段）。带上 (size, mtime) 作为缓存键，
    文件被覆盖或正在写时会自然失效，不会返回过期值。
    """
    try:
        st = stat or os.stat(path)
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        return None
    with _DUR_LOCK:
        hit = _DUR_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    d = condense.probe_duration(STATE.get("ffprobe_bin"), path)
    with _DUR_LOCK:
        # 缓存别无限长：超过 2 万条就清掉一半（老录像淘汰，代价只是重读一次）
        if len(_DUR_CACHE) > 20000:
            for k in list(_DUR_CACHE)[:10000]:
                _DUR_CACHE.pop(k, None)
        _DUR_CACHE[path] = (key, d)
    return d


def _attach_durations(days):
    """给缩时片段补上真实播放时长。

    全时录像的分片长度就是 record_segment_sec，不必逐个 probe；缩时片段是压缩
    成品，时长与源分片无关，只能读文件本身（带 (size, mtime) 缓存）。
    """
    for files in days.values():
        for f in files:
            f["d"] = file_duration(f.pop("p", None) or "")
    return days


def recordings_payload():
    """磁盘上真实存在的录像：每路每天有哪些分片、多大。

    full（全时）与 clip（缩时，_clip/ 下）分开出清单 —— 回放页两种模式各吃各的。
    新目录（摄像头名称）与旧 id 目录都扫，升级前的存量数据不丢。
    """
    root = record_dir()
    per = {}
    clips = {}
    total = 0
    for c in cameras():
        days = {}
        cdays = {}
        for dname in cam_storage_dirs(c):
            for f in _scan_day_dirs(os.path.join(root, dname)).items():
                merged = {x["n"]: x for x in days.get(f[0], [])}
                merged.update({x["n"]: x for x in f[1]})
                days[f[0]] = [merged[k] for k in sorted(merged)]
            cbase = os.path.join(root, "_clip", dname)
            for f in _scan_day_dirs(cbase).items():
                merged = {x["n"]: x for x in cdays.get(f[0], [])}
                merged.update({x["n"]: x for x in f[1]})
                cdays[f[0]] = [merged[k] for k in sorted(merged)]
        # 缩时片段必须是真实时长：拿 segment_sec 冒充会让时间轴显示 2 个半小时，
        # 实际 3 秒播完，然后疯狂切文件。
        _attach_durations(cdays)
        # 内部用的绝对路径不出去（前端只需要 n/b/d，路径由 /api/record/file 拼）
        for files in list(days.values()) + list(cdays.values()):
            for x in files:
                x.pop("p", None)
        per[c["id"]] = days
        clips[c["id"]] = cdays
        total += sum(len(v) for v in days.values())
    # 回放页时间轴要用真实分片长度，不能在前端写死 300 秒
    return {"ok": True, "root": root, "total": total,
            "segment_sec": runtime.segment_sec(runtime.load_config(
                STATE["var_dir"]).get("record_segment_sec")),
            "cameras": per, "clips": clips}


def cleanup_state_path():
    return os.path.join(STATE["var_dir"], "cleanup-state.json")


def load_cleanup_state():
    p = cleanup_state_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_cleanup_state(obj):
    p = cleanup_state_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def retention_payload():
    cfg, cams, _ = camera_cfg()
    at = retention.parse_hhmm(cfg.get("cleanup_at"))
    p = retention.plan(record_dir(), cams, {c["id"]: c for c in cams})
    return {
        "ok": True,
        "cleanup_at": at,
        "next_run": retention.next_run(at).strftime("%Y-%m-%d %H:%M"),
        "last": load_cleanup_state(),
        "record_dir": record_dir(),
        "policy": p["policy"],
        "preview": {
            "delete_count": len(p["delete"]),
            "protected_count": len(p["protected"]),
            "files": p["files"],
            "freed_mb": round(p["freed_bytes"] / 1048576.0, 1),
        },
        "items": p["delete"][:50],
        "protected": p["protected"][:20],
        "unknown_dirs": len(p["unknown"]),
    }


def run_cleanup(dry_run=False, trigger="manual", only=None):
    cfg, cams, cam_cfg = camera_cfg()
    p = retention.plan(record_dir(), cams, cam_cfg)
    if only is not None:
        # only 来自前端，必须挡住目录穿越：只认 record_dir 之下、且确实出现在
        # 本次计划里的路径。否则一个伪造的 path 就能让它 rmtree 任意目录。
        root = record_dir()
        planned = {it["path"] for it in p["delete"]}
        only = {k: v for k, v in only.items()
                if k in planned and runtime.under(root, k)}
    res = retention.execute(p, dry_run=dry_run, only=only)
    res["trigger"] = trigger
    res["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    res["record_dir"] = record_dir()
    # UI 的「预览」面板读的是 preview 这组汇总字段。之前只有 /api/retention 的
    # GET 才有 preview，POST 直接返回 execute() 裸结果，前端拿到 undefined
    # 就永远显示"将删除 0 个目录"——按钮看着点了，其实什么都没算出来。
    res["preview"] = {
        "delete_count": len(p["delete"]),
        "protected_count": len(p["protected"]),
        "unknown_count": len(p["unknown"]),
        "files": p["files"],
        "freed_bytes": p["freed_bytes"],
        "freed_mb": round(p["freed_bytes"] / 1048576.0, 1),
    }
    res["policy"] = p["policy"]
    if dry_run:
        # 预览要能说清"删哪些"，不能只给个数字。
        # 这里返回全量 delete：前端要把这份清单原样带回来做二次确认，
        # 截断到 50 条的话第 51 个之后的目录就永远删不掉。
        res["items"] = p["delete"]
        res["protected"] = p["protected"][:20]
    if not dry_run:
        state = {
            "date": time.strftime("%Y-%m-%d"),
            "at": res["at"],
            "trigger": trigger,
            "freed_mb": res["freed_mb"],
            "files": res["files"],
            "deleted_dirs": len(res["deleted"]),
            "failed": len(res["failed"]),
        }
        save_cleanup_state(state)
        try:
            log_dir = os.path.join(STATE["var_dir"], "log")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "cleanup.log"), "a", encoding="utf-8") as f:
                f.write("%s [%s] 删除 %d 个目录 / %d 个文件 / %s%s\n" % (
                    res["at"], trigger, len(res["deleted"]), res["files"],
                    retention.pretty_bytes(res["freed_bytes"]),
                    ("，失败 %d" % len(res["failed"])) if res["failed"] else ""))
        except OSError:
            pass
        res["state"] = state
    return res


def cleanup_loop(stop_event):
    """每天到点跑一次；靠 state 里的日期去重，重启不会重复删。"""
    while not stop_event.wait(30):
        try:
            cfg = runtime.load_config(STATE["var_dir"])
            last = load_cleanup_state().get("date")
            if retention.due(last, cfg.get("cleanup_at")):
                run_cleanup(dry_run=False, trigger="schedule")
        except Exception as e:                     # 清理线程不能把服务带死
            log_line("cleanup error: %s" % e)


def status_payload():
    cfg = runtime.load_config(STATE["var_dir"])
    g = gpu()
    # 硬件不可用时强制单路，别让四路软件编码把机器打死。
    # gpu == "auto" 是"用户没指定"，只有这种才允许降级；用户显式选 qsv/off 时尊重用户。
    if g["state"] == "none" and cfg.get("gpu") == "auto":
        effective = 1
    else:
        effective = runtime.clamp_int(cfg.get("concurrency"), 1, 3, 2)
    pool = runtime.SlotPool(os.path.join(STATE["var_dir"], "slots"), effective)
    # 队列数字直接取调度器的实时计数，不再读 queue.json 占位文件
    lo = STATE.get("link")
    if lo is not None:
        ls = lo.status()
        queue = {"done": ls["counters"]["done"], "pending": ls["pending"],
                 "failed": ls["counters"]["failed"], "silent": ls["counters"]["silent"],
                 "mode": ls["mode"], "mode_label": ls["mode_label"],
                 "saved_ratio": ls["saved_ratio"], "stub": False}
    else:
        queue = {"done": 0, "pending": 0, "failed": 0, "stub": True}
    return {
        "app": "nvr",
        "version": runtime.VERSION,
        "pid": os.getpid(),
        "uptime": int(time.time() - STATE["started_at"]),
        "port": STATE["port"],
        "listen": STATE.get("listen", "0.0.0.0"),
        "gpu": g,
        # 真实资源占用：以前页面写死 "CPU 3% / 96.4 MB"，换了机器也照报旧值
        # gpu_report 一起带上，保证"页面说的"和"录制真正用的"是同一个结论。
        # 这里必须局部兜住：res 只是"存储剩余/目录"这类附加信息，它读失败
        # 不该让整个 /api/status 500 —— 那样摄像头列表、录制状态、看板会一起废掉，
        # 用户看到的是"页面转圈"，而真正的原因只是某个 /proc 字段读不到。
        "res": _safe_res(g),
        "cams": {"total": len(cameras()),
                 "recording": sum(1 for c in cameras() if c.get("url") and c.get("enabled", True))},
        "config": runtime.redact_cfg(cfg),
        "limits": {
            "concurrency": effective,
            "configured": runtime.clamp_int(cfg.get("concurrency"), 1, 3, 2),
            "downgraded": effective != runtime.clamp_int(cfg.get("concurrency"), 1, 3, 2),
            "prefix": " ".join(runtime.low_priority_prefix(cfg)) or "(无)",
        },
        "slots": pool.status(),
        "queue": queue,
        "link": ls if lo is not None else None,
        "scan_label": runtime.SCAN_LABEL.get(cfg.get("scan_mode"), "?"),
        "dirs": {"var": STATE["var_dir"], "ui": STATE["ui_dir"], "app": STATE["app_dir"]},
    }


# ---------------------------------------------------------------- 录制与压制衔接

def link_payload():
    """衔接状态：模式、队列、下次批量时刻、最近一条处理结果。

    设置页那张「录制与压制衔接」卡片只吃这一个接口。
    next_batch_at 与 last_item 一起给出来，用户不用翻日志就能判断
    "是还没到点"还是"处理失败了"—— 这两件事的表现都是"没出新片段"。
    """
    lo = STATE.get("link")
    rc = STATE.get("recorder")
    if lo is None:
        return {"ok": False, "error": "调度未启动"}
    d = lo.status()
    d["ok"] = True
    d["record"] = rc.status() if rc else None
    d["configured_cams"] = sum(1 for c in cameras() if c.get("url"))
    d["record_enable"] = bool(runtime.load_config(STATE["var_dir"]).get("record_enable", True))
    return d


def set_link(body):
    """改衔接策略。只动入队节奏与分片长度，不重建队列、不打断正在处理的任务。"""
    cfg = runtime.load_config(STATE["var_dir"])
    if "link_mode" in body:
        cfg["link_mode"] = runtime.link_mode(body.get("link_mode"))
    if "link_interval_h" in body:
        cfg["link_interval_h"] = runtime.link_interval_h(body.get("link_interval_h"))
    if "record_segment_sec" in body:
        cfg["record_segment_sec"] = runtime.segment_sec(body.get("record_segment_sec"))
    if "link_max_queue" in body:
        cfg["link_max_queue"] = runtime.clamp_int(body.get("link_max_queue"), 20, 2000, 200)
    if "record_enable" in body:
        cfg["record_enable"] = bool(body.get("record_enable"))
    cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    runtime.save_config(STATE["var_dir"], cfg)
    if STATE.get("link"):
        STATE["link"].reconfigure(cfg)
    return {"ok": True, "link": link_payload()}


class Handler(BaseHTTPRequestHandler):
    server_version = "nvr/" + runtime.VERSION

    # fnOS 桌面壳与 health.json 会高频探测，这些请求不落日志，省磁盘写
    QUIET = ("/api/health", "/api/live", "/api/status")

    # ---- 写操作的最低门槛 -------------------------------------------
    # 本服务监听 0.0.0.0（手机直连才通），所以任何接口都必须假设"同一局域网里
    # 有别人"。两件事在这里兜住：
    #   1) 只收回环/LAN 侧的请求，反代与 DNS rebinding 的 Host 头挡在门外
    #   2) 所有 POST 必须带自定义头 X-NVR-Request —— 浏览器跨站发请求时，
    #      非安全列表内的头会触发预检，而本服务不应答 CORS，于是写入类接口
    #      天然免疫 CSRF（text/plain 直接发 JSON 的老路走不通了）
    MAX_BODY = 4 * 1024 * 1024          # 配置整份推送也就几十 KB，4MB 足够

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        name = host.rsplit(":", 1)[0].strip("[]").lower()
        if name in ("localhost", "127.0.0.1", "::1"):
            return True
        # 局域网 IP / mDNS 名 / 用户自建域名都放行；挡的是 DNS rebinding 用的
        # 公网域名解析到内网地址这一类：只认 IPv4/IPv6 字面量与单段主机名
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", name):
            return True
        if ":" in name:                 # IPv6 字面量
            return True
        if name.endswith(".local") or name.endswith(".lan") or name.endswith(".fnos.net"):
            return True
        return False

    def _write_ok(self):
        """写操作的前置校验：一旦不满足直接回 403，不改任何状态。"""
        if self.headers.get("X-NVR-Request") != "1":
            self._send(403, {"ok": False,
                             "error": "缺少 X-NVR-Request 头；请用官方页面操作"})
            return False
        if not self._host_ok():
            self._send(403, {"ok": False, "error": "Host 不被允许（%s）"
                             % (self.headers.get("Host") or "-")})
            return False
        return True

    def log_message(self, fmt, *args):
        try:
            line = fmt % args
        except TypeError:
            return
        if any(q in line for q in self.QUIET):
            return
        _record("%s %s" % (time.strftime("%H:%M:%S"), line))


    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        # 标记"响应已开始"：do_GET/do_POST 的异常兜底靠它判断能不能再发一个
        # 500。已经发过头还再发，客户端收到的会是一个畸形的双响应。
        self._responded = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._responded = False
        try:
            return self._do_get()
        except Exception as e:
            _record("GET %s 异常：%s%s" % (urlparse(self.path).path, _tb_where(), _fmt_exc(e)))
            if self._responded:          # 头已经发出去了（MJPEG/下载中途出错），不能再发
                self.close_connection = True
                return
            try:
                self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            except Exception:
                pass

    def _do_get(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._send(200, {"ok": True, "version": runtime.VERSION,
                                    "uptime": int(time.time() - STATE["started_at"]),
                                    "ffmpeg": runtime.find_ffmpeg(STATE["app_dir"]),
                                    "ffprobe": runtime.find_ffprobe(STATE["app_dir"])})
        if path == "/api/config":
            # UI 的 syncFromServer 靠这个接口把「分片长度 / 衔接模式 / 快照间隔」
            # 拉回来。以前这里根本没路由，请求落到静态文件返回 index.html，
            # r.json() 抛错又被 catch 吞掉 —— 于是"以服务端为准"从未生效过。
            # 摄像头 url 里的凭据必须脱敏：这是把整份配置交给浏览器。
            return self._send(200, {"ok": True,
                                    "config": runtime.redact_cfg(
                                        runtime.load_config(STATE["var_dir"]))})
        if path == "/api/status":
            return self._send(200, status_payload())
        if path == "/api/gpu":
            return self._send(200, gpu(force=True))
        if path == "/api/slots":
            cfg = runtime.load_config(STATE["var_dir"])
            pool = runtime.SlotPool(os.path.join(STATE["var_dir"], "slots"),
                                    cfg["concurrency"])
            return self._send(200, pool.status())
        if path == "/api/live":
            return self._send(200, live_payload())
        if path == "/api/recordings":
            return self._send(200, recordings_payload())
        if path == "/api/record/probe":
            return self._record_probe(urlparse(self.path).query)
        if path == "/api/record/file":
            return self._record_file(urlparse(self.path).query)
        if path == "/api/retention":
            return self._send(200, retention_payload())
        if path == "/api/link":
            return self._send(200, link_payload())
        if path == "/api/probe":
            return self._probe_file(urlparse(self.path).query)
        if path == "/api/log":
            return self._send(200, {"ok": True, "version": runtime.VERSION,
                                    "lines": list(_LOG),
                                    "text": "\n".join(_LOG)})
        if path == "/api/log/export":
            # 诊断导出：一次下载整份（状态摘要 + 全量日志），凭据已脱敏。
            # 以前这个按钮只弹一个 toast，什么都没发生。
            text = log_export_text()
            self._responded = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            body = text.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             'attachment; filename="nvr-diag-%s.txt"'
                             % time.strftime("%Y%m%d-%H%M%S"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path.startswith("/api/live/snapshot/"):
            return self._snapshot(path)
        if path.startswith("/api/live/stream/"):
            return self._live_stream(path)
        return self._static(path)

    def _set_retention(self, body):
        """每路两个周期一起改：全时天数 + 缩时天数，外带清理时刻与录像根目录。"""
        cfg = runtime.load_config(STATE["var_dir"])
        if "cleanup_at" in body:
            cfg["cleanup_at"] = retention.parse_hhmm(body.get("cleanup_at"))
        new_root = str(body.get("record_dir") or "").strip()
        if new_root and new_root != cfg.get("record_dir"):
            cfg["record_dir"] = new_root
        wanted = {str(c.get("id")): c for c in (body.get("cameras") or []) if isinstance(c, dict)}
        cams = cameras()
        for c in cams:
            w = wanted.get(c["id"])
            if not w:
                continue
            c["full_days"] = retention.clamp_days("full_days", w.get("full_days", c["full_days"]))
            c["clip_days"] = retention.clamp_days("clip_days", w.get("clip_days", c["clip_days"]))
        cfg["cameras"] = cams
        cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        runtime.save_config(STATE["var_dir"], cfg)
        # 目录变了要通知录制器换路径，否则新分片还写进旧目录
        if STATE.get("recorder"):
            try:
                STATE["recorder"].reconfigure(cfg, cams=cameras())
            except Exception as e:
                _record("recorder.reconfigure 失败：%r" % e)
        if STATE.get("link"):
            try:
                STATE["link"].reconfigure(cfg)
            except Exception as e:
                _record("link.reconfigure 失败：%r" % e)
        return self._send(200, {"ok": True, "retention": retention_payload()})

    # 宫格要 4~6 路同时出画面，硬卡 2 路会让第 3 路起直接 503，
    # 前端就永远停在"等待第一帧"。上限按帧率/分辨率压成本，而不是按路数硬拦。
    MAX_LIVE_STREAMS = 8

    def _live_stream(self, path):
        """MJPEG 实时流：ffmpeg 读子码流转 mpjpeg，原样泵给浏览器 <img>。

        三个必须：
        - **-an**：mpjpeg 只装视频。子码流一旦带 G.711 音轨，ffmpeg 写文件头
          就会失败退出，前端表现正好是「永远等待第一帧」。这是单画面卡住的
          主要原因之一。
        - **fps/width 由请求方给**：宫格只要 4 帧 480 宽，单画面才要 8 帧 960 宽。
          以前一条流参数写死，宫格 4 路同时播会白吃四份解码 + 编码。
        - **断开即杀**：<img> 换 src 或页面退出时连接断开，这里抛异常走 finally，
          不留孤儿 ffmpeg。
        """
        u = urlparse(self.path)
        q = parse_qs(u.query)

        def _num(key, lo, hi, dft):
            try:
                v = int(float((q.get(key) or [dft])[0]))
            except (TypeError, ValueError):
                return dft
            return max(lo, min(hi, v))

        fps = _num("fps", 1, 15, 8)
        width = _num("w", 0, 1920, 960)
        cam_id = path.rsplit("/", 1)[-1]
        cam = next((c for c in cameras() if c["id"] == cam_id), None)
        if cam is None:
            return self._send(404, {"ok": False, "error": "no such camera"})
        url = cam.get("sub_url") or cam.get("url")
        ff = STATE.get("ffmpeg_bin")
        if not url or not ff:
            return self._send(400, {"ok": False, "error": "no rtsp url or ffmpeg"})
        with _STREAM_LOCK:
            if STATE.get("live_streams", 0) >= self.MAX_LIVE_STREAMS:
                return self._send(503, {"ok": False, "error": "too many streams"})
            STATE["live_streams"] = STATE.get("live_streams", 0) + 1
        cmd = ffmpeg_cmd.live_stream_cmd(ff, url, fps=fps, width=width)
        p = None
        try:
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
            except OSError:
                return self._send(500, {"ok": False, "error": "cannot start ffmpeg"})
            self._responded = True
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=nvrframe")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            fd = p.stdout.fileno()
            while True:
                # Windows 的 select 只接受 socket，开发机上直接阻塞读
                # （生产是 Linux，20 秒收摊的空闲判定照常生效）
                if os.name != "nt":
                    r, _, _ = select.select([fd], [], [], 20.0)
                    if not r:            # 20 秒无数据：流断了，收摊
                        break
                # 用 os.read 读"当前可用"的字节数。read(16384) 会一直等够 16KB
                # 才返回，低码率子码流上就变成几秒才推一次，画面一顿一顿。
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                self.wfile.write(chunk)   # 客户端断开时这里抛异常，走 finally
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            if p is not None:
                try:
                    p.kill()
                except OSError:
                    pass
            with _STREAM_LOCK:
                STATE["live_streams"] = max(0, STATE.get("live_streams", 1) - 1)

    def _snapshot(self, path):
        name = os.path.basename(path)
        cam_id = name[:-4] if name.endswith(".jpg") else name
        # 只允许安全字符，避免 ..%2f 之类穿目录
        if not cam_id or any(not (ch.isalnum() or ch in "-_") for ch in cam_id):
            return self._send(400, {"ok": False, "error": "bad camera id"})
        full = snapshot_path(cam_id)
        if not os.path.isfile(full):
            return self._send(404, {"ok": False, "error": "no snapshot yet"})
        st = os.stat(full)
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        # 快照没更新就回 304：多台手机同时轮询时，带宽与解码都省下来
        if self.headers.get("If-None-Match") == etag:
            self._responded = True
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        with open(full, "rb") as f:
            data = f.read()
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _probe_file(self, qs):
        """单文件探针自检：把 ffprobe 到底回了什么原原本本端出来。

        「读不到包信息」这类判断在真机上一旦翻车就很难查 —— 这里直接返回
        两条探测命令的退出码、输出行数、首行与 stderr，别再靠猜。
        """
        from urllib.parse import parse_qs
        q = parse_qs(qs)
        cam = (q.get("cam") or [""])[0]
        day = (q.get("day") or [""])[0]
        name = (q.get("name") or [""])[0]
        cam_rec = next((c for c in cameras() if c["id"] == cam), None)
        if cam_rec is None:
            return self._send(400, {"ok": False, "error": "bad cam"})
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or \
                not re.match(r"^\d{2}-\d{2}-\d{2}\.mp4$", name):
            return self._send(400, {"ok": False, "error": "bad day/name"})
        full = None
        for dname in cam_storage_dirs(cam_rec):
            cand = os.path.join(record_dir(), dname, day, name)
            if os.path.isfile(cand):
                full = cand
                break
        if full is None:
            return self._send(404, {"ok": False, "error": "no such file"})
        probe = STATE.get("ffprobe_bin")
        report = {"ok": True, "file": full, "size": os.path.getsize(full),
                  "ffprobe": probe, "runs": []}
        if not probe:
            report["error"] = "没有 ffprobe"
            return self._send(200, report)
        import condense
        import ffmpeg_cmd
        for label, cmd in (("packet", ffmpeg_cmd.prescan_cmd(probe, full)),
                           ("frame", ffmpeg_cmd.prescan_frame_cmd(probe, full))):
            t0 = time.time()
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=120)
                out = r.stdout.decode("utf-8", "replace")
                err = (r.stderr or b"").decode("utf-8", "replace").strip()
                report["runs"].append({
                    "kind": label, "rc": r.returncode,
                    "sec": round(time.time() - t0, 1),
                    "lines": len([x for x in out.splitlines() if x.strip()]),
                    "head": "\n".join(out.splitlines()[:3]),
                    "tail": "\n".join(out.splitlines()[-2:]),
                    "packets": len(condense.parse_probe_output(out)),
                    "stderr": err[-400:],
                })
            except (OSError, subprocess.TimeoutExpired) as e:
                report["runs"].append({"kind": label, "rc": None,
                                       "sec": round(time.time() - t0, 1),
                                       "error": str(e)[:200]})
        return self._send(200, report)

    def _record_probe(self, qs):
        """对一路摄像头跑一次真实探测，把「到底是谁的问题」定死。

        真机上"录不出来"至少三种原因，表现却完全一样（一条录像都没有）：

          1. 摄像头没给出可用码流（关键帧来得太晚 / SPS-PPS 缺失）
             → ffmpeg 报 "Output file does not contain any stream"
          2. 码流的编码格式这个 ffmpeg 不支持封装（H.265 很常见）
             → 报 "Could not find tag for codec" / "Could not write header"
          3. 摄像头拒绝连接（密码错 / 通道被占满 / 并发上限）
             → 报 "method SETUP failed" / 4xx / 5xx

        光看日志最后一行分不清，所以这里同时做两件事：
          - ffprobe：这条流里到底有什么（有没有视频流、什么编码、分辨率）
          - 试录 8 秒（带 -v info）：看 ffmpeg 的 Stream mapping 和第一段输出
        结论写进 verdict，用户不用懂 ffmpeg 也知道下一步该做什么。
        """
        from urllib.parse import parse_qs
        q = parse_qs(qs)
        cid = (q.get("id") or [""])[0]
        cam = next((c for c in cameras() if c.get("id") == cid), None)
        if cam is None:
            return self._send(400, {"ok": False, "error": "没有这个通道"})
        ff = STATE.get("ffmpeg_bin")
        if not ff:
            return self._send(400, {"ok": False,
                                    "error": "系统里没有 ffmpeg，无法诊断"})
        # 一次只允许一个诊断：每路试录都要占摄像头一个连接，并发点会把
        # 本来就紧张的通道数抢光，反而得出"连不上"的假结论。
        if not _PROBE_LOCK.acquire(False):
            return self._send(429, {"ok": False,
                                    "error": "上一次诊断还在跑，等几秒再试"})
        try:
            return self._send(200, _probe_one(cam, ff, STATE.get("ffprobe_bin")))
        finally:
            _PROBE_LOCK.release()

    def _record_file(self, qs):
        from urllib.parse import parse_qs
        q = parse_qs(qs)
        cam = (q.get("cam") or [""])[0]
        day = (q.get("day") or [""])[0]
        name = (q.get("name") or [""])[0]
        if not cam or any(not (ch.isalnum() or ch in "-_") for ch in cam):
            return self._send(400, {"ok": False, "error": "bad cam"})
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            return self._send(400, {"ok": False, "error": "bad day"})
        if not re.match(r"^\d{2}-\d{2}-\d{2}\.mp4$", name):
            return self._send(400, {"ok": False, "error": "bad name"})
        kind = (q.get("kind") or ["full"])[0]
        if kind not in ("full", "clip"):
            return self._send(400, {"ok": False, "error": "bad kind"})
        # cam 参数是摄像头 id；实际目录可能是名称（新）或 id（旧），逐个试探
        cam_rec = next((c for c in cameras() if c["id"] == cam), None)
        if cam_rec is None:
            return self._send(400, {"ok": False, "error": "bad cam"})
        full = None
        for dname in cam_storage_dirs(cam_rec):
            cand = os.path.join(record_dir(), "_clip" if kind == "clip" else "",
                                dname, day, name)
            if os.path.isfile(cand):
                full = cand
                break
        if full is None:
            return self._send(404, {"ok": False, "error": "no such file"})
        size = os.path.getsize(full)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            code = 206
        if start > end or start >= size:
            self._responded = True
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return
        self._responded = True
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remain = end - start + 1
                while remain > 0:
                    chunk = f.read(min(65536, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _drain(self, length):
        """把请求体读进垃圾桶：分块读、定长上限，绝不一次性进内存。"""
        remain = min(length, 64 * 1024 * 1024)   # 最多丢 64MB，再多就断连
        try:
            while remain > 0:
                chunk = self.rfile.read(min(65536, remain))
                if not chunk:
                    break
                remain -= len(chunk)
        except (OSError, ValueError):
            pass

    def do_POST(self):
        # 统一兜底：任何未捕获异常都必须变成一条 500 JSON，不能直接崩掉连接。
        # 以前 save_config 撞上 Windows 文件占用时抛 PermissionError，浏览器看到的是
        # "net::ERR_EMPTY_RESPONSE"，用户只知道"保存失败"，日志里也没有因果链。
        self._responded = False
        try:
            return self._do_post()
        except Exception as e:
            _record("POST %s 异常：%s%s" % (urlparse(self.path).path, _tb_where(), _fmt_exc(e)))
            if self._responded:
                self.close_connection = True
                return
            try:
                self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            except Exception:
                pass

    def _do_post(self):
        path = urlparse(self.path).path
        if not self._write_ok():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"ok": False, "error": "bad content-length"})
        if length < 0 or length > self.MAX_BODY:
            # 超限：丢到垃圾桶（不留在内存里），但要把 body 读完再回 413 ——
            # 直接断连会让部分客户端只看到"连接被重置"，反而不知道是超限。
            self._drain(length)
            self.close_connection = True
            return self._send(413, {"ok": False,
                                    "error": "body too large (> %d bytes)" % self.MAX_BODY})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"ok": False, "error": "invalid json"})
        if not isinstance(body, dict):
            return self._send(400, {"ok": False, "error": "body must be an object"})

        if path == "/api/config":
            cfg = runtime.load_config(STATE["var_dir"])
            changed = []
            for k in runtime.DEFAULTS:
                if k not in body:
                    continue
                v = runtime.coerce(k, body[k])
                if cfg.get(k) != v:
                    cfg[k] = v
                    changed.append(k)
            if "cameras" in body:
                cams = norm_cameras(body.get("cameras"))
                if cams or body.get("cameras") == []:
                    if cams != cfg.get("cameras"):
                        cfg["cameras"] = cams
                        changed.append("cameras")
            try:
                cfg["concurrency"] = max(1, min(3, int(cfg["concurrency"])))
            except (TypeError, ValueError):
                cfg["concurrency"] = 2
            try:
                cfg["snapshot_interval"] = max(1, min(10, int(cfg["snapshot_interval"])))
            except (TypeError, ValueError):
                cfg["snapshot_interval"] = 10
            if changed:
                cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                runtime.save_config(STATE["var_dir"], cfg)
                log_line("配置已更新：%s" % "、".join(changed))
            # 设置推送即热生效：分片长度 / 录制开关 / 摄像头列表不必等重启
            fresh = cameras()
            if STATE.get("recorder"):
                try:
                    STATE["recorder"].reconfigure(cfg, cams=fresh)
                except Exception as e:
                    _record("recorder.reconfigure 失败：%r" % e)
            if STATE.get("link"):
                try:
                    STATE["link"].reconfigure(cfg)
                    if changed:
                        STATE["link"].set_cameras(fresh)
                        STATE["link"].set_profiles(
                            (cfg.get("profiles") or {}) if isinstance(cfg.get("profiles"), dict) else {})
                except Exception as e:
                    _record("link.reconfigure 失败：%r" % e)
            # 回给浏览器的必须是脱敏版：明文 url 一旦回传，等于把摄像头密码发到前端
            return self._send(200, {"ok": True, "changed": changed,
                                    "config": runtime.redact_cfg(cfg),
                                    "restart_hint": ("部分参数需重启服务才生效"
                                                     if "record_dir" in changed else "")})

        if path == "/api/scan":
            # 「扫描」按钮的落地实现：不碰解码，只列磁盘上每个通道有没有数据、
            # 以及最近一天的分片数与体积。真机上「为什么回放页是空的」先看它。
            root = record_dir()
            rows = []
            for c in cameras():
                days, files, size = set(), 0, 0
                for dname in cam_storage_dirs(c):
                    for d, fs in (_scan_day_dirs(os.path.join(root, dname)) or {}).items():
                        days.add(d)
                        files += len(fs)
                        size += sum(x["b"] for x in fs)
                rows.append({"id": c["id"], "name": c["name"],
                             "dir": c.get("dir_name") or c["id"],
                             "days": len(days), "files": files,
                             "bytes": size, "has_url": bool(c.get("url")),
                             "enabled": bool(c.get("enabled", True))})
            total = {"files": sum(r["files"] for r in rows),
                     "bytes": sum(r["bytes"] for r in rows)}
            log_line("扫描完成：%s 下 %d 个分片" % (root, total["files"]))
            return self._send(200, {"ok": True, "root": root, "channels": rows,
                                    "total": total})

        if path == "/api/slot/acquire":
            pool = STATE["pool"]
            i = pool.acquire(body.get("owner", "demo"))
            if i is None:
                return self._send(429, {"ok": False, "error": "no free slot"})
            return self._send(200, {"ok": True, "slot": i, "status": pool.status()})

        if path == "/api/slot/release":
            STATE["pool"].release(int(body.get("slot", -1)))
            return self._send(200, {"ok": True, "status": STATE["pool"].status()})

        if path == "/api/retention/policy":
            return self._set_retention(body)

        if path == "/api/dir/check":
            # 保存目录的「检测并保存」：真实探测存在性 + 写权限（落临时文件再删）。
            # 不在这里顺手改配置 —— 改配置仍走 /api/config，前端拿到 ok 才推。
            res = runtime.probe_record_dir(str(body.get("path") or "").strip(),
                                           create=bool(body.get("create", True)))
            return self._send(200, res)

        if path == "/api/link/policy":
            return self._send(200, set_link(body))

        if path == "/api/link/run":
            lo = STATE.get("link")
            if lo is None:
                return self._send(503, {"ok": False, "error": "调度尚未启动"})
            lo.trigger()
            return self._send(200, {"ok": True, "link": link_payload()})

        if path == "/api/log/clear":
            log_clear()
            log_line("日志已被用户清空")
            return self._send(200, {"ok": True})

        if path == "/api/restart":
            # 自愈重启：先回 200，0.6 秒后 os.execv 用磁盘上的最新代码替换当前进程。
            # execv 不换 PID、不换 stdout/stderr（仍写回日志文件）；Python 的 socket
            # 默认带 CLOEXEC，exec 时监听端口自动释放，新进程能立刻重新 bind。
            self._send(200, {"ok": True, "restarting": True})

            def _restart():
                time.sleep(0.6)
                script = os.path.abspath(__file__)
                argv = [sys.executable, script] + sys.argv[1:]
                log_line("收到重启请求，正在用最新代码重启服务…")
                try:
                    os.execv(sys.executable, argv)
                except Exception as e:  # exec 失败只能退出，由外部（fnOS）拉起
                    log_line("重启失败：%s" % e)
                    os._exit(0)

            threading.Thread(target=_restart, daemon=True).start()
            return

        if path == "/api/retention/cleanup":
            dry = bool(body.get("dry_run", True))
            # 二次确认必须"删用户看到的那一份"：前端把 dry_run 返回的
            # [{path, fp}] 原样带回来，这里只删清单内且指纹没变的目录。
            # 不传 paths（比如定时任务）就按当前计划全量删。
            only = None
            if not dry and isinstance(body.get("paths"), list):
                only = {}
                for it in body["paths"]:
                    if not isinstance(it, dict):
                        continue
                    pth, fp = it.get("path"), it.get("fp")
                    if isinstance(pth, str) and isinstance(fp, str):
                        only[pth] = fp
                if not only:
                    return self._send(400, {"ok": False,
                                            "error": "paths 为空，拒绝执行删除"})
            res = run_cleanup(dry_run=dry, only=only)
            return self._send(200, res)

        if path == "/api/shutdown":
            self._send(200, {"ok": True})
            STATE["shutdown"] = True
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        return self._send(404, {"ok": False, "error": "not found"})

    # UI 静态文件很小（单 HTML + 图标），常驻内存：命中后不再碰磁盘
    _static_cache = {"mtime": None, "data": None, "etag": None}

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        root = os.path.normpath(os.path.abspath(STATE["ui_dir"]))
        full = os.path.normpath(os.path.join(root, rel))
        # 前缀判断必须带分隔符：root=.../ui 时 .../ui2/x 用 startswith 是能过的
        if not runtime.under(root, full) or not os.path.isfile(full):
            full = os.path.join(root, "index.html")
            if not os.path.isfile(full):
                return self._send(404, {"ok": False, "error": "ui not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        st = os.stat(full)
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self._responded = True
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        cache = self._static_cache
        if cache["etag"] != etag:
            with open(full, "rb") as f:
                cache["data"] = f.read()
            cache["etag"] = etag
        data = cache["data"]
        # index 每次协商缓存（升级后立即生效）；图标类资源可以放心缓存一天
        cc = "no-cache" if rel.endswith(".html") or rel == "index.html" else "public, max-age=86400"
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cc)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("TRIM_SERVICE_PORT", "18080")))
    # 监听地址：默认 0.0.0.0，局域网/手机直连 http://<NAS-IP>:18081 才是通的。
    # 只绑 127.0.0.1 时，飞牛网关转发没问题，但直连会 ERR_CONNECTION_REFUSED。
    ap.add_argument("--host", default=os.environ.get("TRIM_LISTEN_HOST", "0.0.0.0"))
    ap.add_argument("--var", default=os.environ.get("TRIM_PKGVAR",
                                                    os.path.join(os.getcwd(), "var")))
    ap.add_argument("--app", default=os.environ.get("TRIM_APPDEST",
                                                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    ap.add_argument("--ui", default=None)
    a = ap.parse_args()

    STATE["app_dir"] = a.app
    STATE["var_dir"] = a.var
    STATE["ui_dir"] = a.ui or os.path.join(a.app, "ui")
    STATE["listen"] = a.host
    os.makedirs(a.var, exist_ok=True)

    cfg = runtime.load_config(a.var)
    if not os.path.isfile(runtime.config_path(a.var)):
        runtime.save_config(a.var, cfg)

    # 端口由 cmd/main 解析（配置 > TRIM_SERVICE_PORT > 18081）后传进来，这里只认参数
    want = runtime.clamp_port(a.port)

    STATE["pool"] = runtime.SlotPool(os.path.join(a.var, "slots"), cfg["concurrency"])

    httpd = None
    last_err = None
    port = want
    for p in range(want, min(runtime.PORT_LIMITS[1], want + 40) + 1):
        try:
            httpd = ThreadingHTTPServer((a.host, p), Handler)
        except OSError as e:
            last_err = e
            continue
        port = p
        break
    if httpd is None:
        sys.stderr.write("[nvr] 无法监听 %s:%d —— %s\n"
                         "        用 `ss -lntp | grep %d` 查占用进程，"
                         "或在 应用设置 → 服务端口 里换一个端口后重启\n"
                         % (a.host, want, last_err, want))
        return 1

    # 端口被占时自动顺延：把实际端口写回配置与桌面入口，重启后仍然对得上
    if port != want:
        runtime.apply_port(a.var, a.app, port)
        STATE.setdefault("port_shift", None)
        STATE["port_shift"] = (want, port)
    else:
        try:
            with open(os.path.join(a.var, "port.txt"), "w", encoding="utf-8") as f:
                f.write(str(port))
        except OSError:
            pass
    a.port = port
    STATE["port"] = port
    httpd.daemon_threads = True
    httpd.request_queue_size = 16          # 多台设备同时打开预览时不排队拒绝
    httpd.block_on_close = False

    def _stop(signum, frame):
        STATE["shutdown"] = True
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    g = gpu()
    log_line("v%s pid=%d listen=%s:%d gpu=%s (%s)"
             % (runtime.VERSION, os.getpid(), a.host, a.port, g["state"], g["reason"]))
    try:
        import gc
        gc.freeze()      # 启动后把常驻对象移出可回收代，长跑时 GC 次数和停顿都更少
    except Exception:
        pass

    stop_event = threading.Event()
    STATE["stop_event"] = stop_event
    threading.Thread(target=cleanup_loop, args=(stop_event,), daemon=True,
                     name="cleanup").start()

    # ---------------------------------------------------------- 录制 + 衔接调度
    def _log_line(msg):
        log_line(msg)

    ff = g.get("ffmpeg") or runtime.find_ffmpeg(a.app)
    fp = runtime.find_ffprobe(a.app)
    STATE["ffmpeg_bin"] = ff
    STATE["ffprobe_bin"] = fp
    STATE["live_streams"] = 0
    if ff:
        _log_line("ffmpeg=%s" % ff)
    else:
        _log_line("未找到 ffmpeg：预览/录制/压制都将不可用"
                  "（把静态 ffmpeg 放到 app/bin，或 apt-get install ffmpeg）")
    if fp:
        _log_line("ffprobe=%s" % fp)
    else:
        _log_line("未找到 ffprobe：压制/缩时分析将不可用")
    # 启动时和 record_dir() 用同一个回退来源，不能各写一套（见 record_dir 注释）
    rec_root = cfg.get("record_dir") or runtime.DEFAULTS["record_dir"]
    try:
        os.makedirs(rec_root, exist_ok=True)
    except OSError as e:
        _log_line("录像目录不可写 %s：%s" % (rec_root, e))

    rec = recorder.Recorder(ff, rec_root, cameras(),
                            segment_sec=cfg.get("record_segment_sec", 300),
                            reconnect_sec=cfg.get("record_reconnect_sec", 5),
                            log=_log_line,
                            enabled=bool(cfg.get("record_enable", True)),
                            var_dir=a.var)
    lk = link_mod.Link(ff, fp, rec_root, a.var, cameras(), cfg, log=_log_line)
    STATE["recorder"] = rec
    STATE["link"] = lk
    snap = snapshots.SnapshotWorker(
        ff, a.var, cameras,
        interval_fn=lambda: (runtime.load_config(a.var).get("snapshot_interval") or 10),
        interval=cfg.get("snapshot_interval", 10), timeout=8, log=_log_line)
    STATE["snapshots"] = snap
    snap.start()

    rec.start()
    lk.start()

    def _pump(ev):
        """录制巡检与入队节拍共用一条线程：两者都是 2 秒级，拆开只会多一个线程。"""
        while not ev.wait(2.0):
            try:
                cams = cameras()            # 设置页改了列表/地址，不用重启就生效
                rec.set_cameras(cams)
                rec.reconcile(cams)         # 删掉的通道收尸、地址/遮蔽区变了的重启
                lk.set_cameras(cams)
                rec.tick()
                lk.tick()
            except Exception as e:               # 单次巡检出错不能让调度线程退出
                _log_line("调度异常：%r" % e)

    threading.Thread(target=_pump, args=(stop_event,), daemon=True,
                     name="pump").start()

    try:
        httpd.serve_forever(poll_interval=0.3)
    finally:
        stop_event.set()
        try:
            STATE.get("snapshots").stop()
        except Exception:
            pass
        try:
            lk.stop()
            rec.stop()
        except Exception:
            pass
        httpd.server_close()
        log_line("stopped")


if __name__ == "__main__":
    sys.exit(main() or 0)
