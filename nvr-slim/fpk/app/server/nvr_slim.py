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
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runtime  # noqa: E402
import params   # noqa: E402
import retention  # noqa: E402
import recorder  # noqa: E402
import condense  # noqa: E402
import link as link_mod  # noqa: E402
import snapshots  # noqa: E402

STATE = {
    "started_at": time.time(),
    "app_dir": ".",
    "var_dir": ".",
    "ui_dir": ".",
    "port": 18081,
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


def _record(line):
    line = _CRED.sub(r'\1***@', line)
    _LOG.append(line)
    print(line, flush=True)
    try:
        p = _log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_line(msg):
    """业务日志入口：统一加 [nvr] 前缀与时间戳。"""
    _record("[nvr] %s %s" % (time.strftime("%H:%M:%S"), msg))


def log_clear():
    """清空内存缓冲与落盘文件（用户点「清除日志」时用）。"""
    _LOG.clear()
    try:
        p = _log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass


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
        # 画面区域遮罩（16×9 网格：ignore=检测忽略区，redact=输出遮蔽区）
        m = c.get("masks")
        if isinstance(m, dict):
            out[-1]["masks"] = {
                "ignore": [int(x) for x in (m.get("ignore") or []) if isinstance(x, int) or str(x).isdigit()],
                "redact": [int(x) for x in (m.get("redact") or []) if isinstance(x, int) or str(x).isdigit()],
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
        })
        # 遮罩要一路透传到 worker（快照/录制），否则「设置页画了、画面没反应」
        m = c.get("masks")
        if isinstance(m, dict):
            out[-1]["masks"] = {
                "ignore": [int(x) for x in (m.get("ignore") or [])
                           if isinstance(x, int) or str(x).isdigit()],
                "redact": [int(x) for x in (m.get("redact") or [])
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
    cfg = runtime.load_config(STATE["var_dir"])
    return cfg.get("record_dir") or "/vol2/1000/NVR"


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
                    files.append({"n": n, "b": os.path.getsize(p)})
        except OSError:
            continue
        if files:
            days[d] = files
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
            for f in _scan_day_dirs(os.path.join(root, "_clip", dname)).items():
                merged = {x["n"]: x for x in cdays.get(f[0], [])}
                merged.update({x["n"]: x for x in f[1]})
                cdays[f[0]] = [merged[k] for k in sorted(merged)]
        per[c["id"]] = days
        clips[c["id"]] = cdays
        total += sum(len(v) for v in days.values())
    return {"ok": True, "root": root, "total": total,
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


def run_cleanup(dry_run=False, trigger="manual"):
    cfg, cams, cam_cfg = camera_cfg()
    p = retention.plan(record_dir(), cams, cam_cfg)
    res = retention.execute(p, dry_run=dry_run)
    res["trigger"] = trigger
    res["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
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
    if g["state"] == "none" and cfg.get("gpu") == "auto":
        effective = 1  # 硬件不可用时强制单路，别让四路软件编码把机器打死
    else:
        effective = cfg["concurrency"]
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
        "config": cfg,
        "limits": {
            "concurrency": effective,
            "configured": cfg["concurrency"],
            "downgraded": effective != cfg["concurrency"],
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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._send(200, {"ok": True, "version": runtime.VERSION,
                                    "uptime": int(time.time() - STATE["started_at"]),
                                    "ffmpeg": runtime.find_ffmpeg(STATE["app_dir"]),
                                    "ffprobe": runtime.find_ffprobe(STATE["app_dir"])})
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
        if "record_dir" in body and str(body.get("record_dir") or "").strip():
            cfg["record_dir"] = str(body["record_dir"]).strip()
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
        return self._send(200, {"ok": True, "retention": retention_payload()})

    def _live_stream(self, path):
        """MJPEG 实时流：ffmpeg 读子码流转 mpjpeg，原样泵给浏览器 <img>。

        按需拉起、断开即杀：只有有人点「播放」才占一路解码转发，
        优先用子码流，省 CPU 也省摄像头的主码流并发额度。
        """
        cam_id = path.rsplit("/", 1)[-1]
        cam = next((c for c in cameras() if c["id"] == cam_id), None)
        if cam is None:
            return self._send(404, {"ok": False, "error": "no such camera"})
        url = cam.get("sub_url") or cam.get("url")
        ff = STATE.get("ffmpeg_bin")
        if not url or not ff:
            return self._send(400, {"ok": False, "error": "no rtsp url or ffmpeg"})
        with _STREAM_LOCK:
            if STATE.get("live_streams", 0) >= 2:
                return self._send(503, {"ok": False, "error": "too many streams"})
            STATE["live_streams"] = STATE.get("live_streams", 0) + 1
        cmd = [ff, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-rtsp_transport", "tcp", "-i", url,
               "-c:v", "mjpeg", "-q:v", "6",
               "-f", "mpjpeg", "-boundary_tag", "nvrframe", "pipe:1"]
        p = None
        try:
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
            except OSError:
                return self._send(500, {"ok": False, "error": "cannot start ffmpeg"})
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=nvrframe")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                r, _, _ = select.select([p.stdout], [], [], 15.0)
                if not r:            # 15 秒无数据：流断了，收摊
                    break
                chunk = p.stdout.read(16384)
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
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        with open(full, "rb") as f:
            data = f.read()
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
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return
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

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._send(400, {"ok": False, "error": "invalid json"})

        if path == "/api/config":
            cfg = runtime.load_config(STATE["var_dir"])
            for k in runtime.DEFAULTS:
                if k in body:
                    cfg[k] = body[k]
            if "cameras" in body:
                cfg["cameras"] = norm_cameras(body.get("cameras"))
            try:
                cfg["concurrency"] = max(1, min(3, int(cfg["concurrency"])))
            except (TypeError, ValueError):
                cfg["concurrency"] = 2
            try:
                cfg["snapshot_interval"] = max(1, min(10, int(cfg["snapshot_interval"])))
            except (TypeError, ValueError):
                cfg["snapshot_interval"] = 10
            cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            runtime.save_config(STATE["var_dir"], cfg)
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
                except Exception as e:
                    _record("link.reconfigure 失败：%r" % e)
            return self._send(200, {"ok": True, "config": cfg})

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
            res = run_cleanup(dry_run=bool(body.get("dry_run", True)))
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
        full = os.path.normpath(os.path.join(STATE["ui_dir"], rel))
        root = os.path.normpath(STATE["ui_dir"])
        if not full.startswith(root) or not os.path.isfile(full):
            full = os.path.join(root, "index.html")
            if not os.path.isfile(full):
                return self._send(404, {"ok": False, "error": "ui not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        st = os.stat(full)
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        if self.headers.get("If-None-Match") == etag:
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
    ap.add_argument("--port", type=int, default=int(os.environ.get("TRIM_SERVICE_PORT", "18081")))
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
    rec_root = cfg.get("record_dir") or os.path.join(a.var, "rec")
    try:
        os.makedirs(rec_root, exist_ok=True)
    except OSError as e:
        _log_line("录像目录不可写 %s：%s" % (rec_root, e))

    rec = recorder.Recorder(ff, rec_root, cameras(),
                            segment_sec=cfg.get("record_segment_sec", 300),
                            reconnect_sec=cfg.get("record_reconnect_sec", 5),
                            log=_log_line,
                            enabled=bool(cfg.get("record_enable", True)))
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
