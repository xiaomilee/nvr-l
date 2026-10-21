#!/usr/bin/env python3
"""NVR 运行时基础件：配置、核显自检、并发闸门、低优先级启动。

只用标准库，飞牛自带的 python3 即可运行。
"""
import json
import os
import shutil
import subprocess
import time

VERSION = "0.9.12"

DEFAULTS = {
    "source_dir": "/vol1/nvr",
    "output_dir": "/vol1/nvr",
    "scan_mode": "h1",          # now | m15 | h1 | d1
    "process_mode": "fast",     # fast | fine
    "gpu": "auto",              # auto | qsv | off
    "concurrency": 2,
    "retention_days": 3,
    "night_only": False,
    "low_priority": True,
    "sprite_thumbs": True,
    "snapshot_interval": 10,    # 直播页快照刷新间隔（秒）
    "record_dir": "/vol1/@appshare/nvr-output",  # 录像与缩时片段根目录
    "cleanup_at": "03:00",            # 每天清理过期录像的时刻
    # ---- 录制（由本应用拉主码流落盘，不再依赖飞牛 NVR 的目录） ----
    "record_enable": True,
    "record_segment_sec": 300,        # 分片时长 60~900 秒
    "record_reconnect_sec": 5,        # 断流重连退避基数
    # ---- 录制与压制的衔接方式 ----
    "link_mode": "live",              # live=录完即压 | scheduled=定时批量
    "link_interval_h": 6,             # 定时模式间隔，1~24 小时
    "link_max_queue": 200,            # 待处理上限，超出则暂缓入队（背压）
    # ---- 服务端口 ----
    # 安装向导里选定后写在这里；TRIM_SERVICE_PORT 只是 manifest 的静态默认值，
    # 用户改过端口就以本字段为准。范围见 PORT_LIMITS。
    "service_port": 18081,
    "updated_at": None,
}

SCAN_LABEL = {"now": "立即", "m15": "每 15 分钟", "h1": "每小时", "d1": "每天 03:00"}
LINK_LABEL = {"live": "即刻压制", "scheduled": "定时压制"}

# 分片时长：太短则文件开合频繁、判定上下文不足；太长则热数据跑出 page cache
SEGMENT_LIMITS = (60, 900)
PORT_LIMITS = (1024, 65000)
LINK_INTERVAL_LIMITS = (1, 24)


def clamp_int(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def link_mode(v):
    return v if v in ("live", "scheduled") else "live"


def segment_sec(v):
    return clamp_int(v, SEGMENT_LIMITS[0], SEGMENT_LIMITS[1], 300)


def link_interval_h(v):
    return clamp_int(v, LINK_INTERVAL_LIMITS[0], LINK_INTERVAL_LIMITS[1], 6)


def clamp_port(v, default=18081):
    """端口：非法值回落默认；越界夹到 [1024, 65000]。"""
    return clamp_int(v, PORT_LIMITS[0], PORT_LIMITS[1], default)


def port_free(port, host="0.0.0.0"):
    """能不能在这个端口上监听。判定用真实 bind，比 lsof/ss 更可靠（也覆盖 TIME_WAIT 之外的占用）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(want, host="0.0.0.0", span=200):
    """从 want 开始往上找一个能监听的端口。找不到返回 want 本身（让上层报错）。"""
    want = clamp_port(want)
    for p in range(want, min(PORT_LIMITS[1], want + span) + 1):
        if port_free(p, host):
            return p
    return want


# ---------------------------------------------------------------- 配置

def config_path(var_dir):
    return os.path.join(var_dir, "config.json")

# 配置读取缓存：/api/live 每 10 秒轮询、health 每 30 秒探测，
# 每次都重新读盘 + json 解析纯属浪费。按 (mtime_ns, size) 判失效。
_CFG_CACHE = {"key": None, "cfg": None}


def load_config(var_dir):
    path = config_path(var_dir)
    key = None
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    if key is not None and _CFG_CACHE["key"] == key:
        return dict(_CFG_CACHE["cfg"])          # 拷贝出去，调用方随便改
    cfg = dict(DEFAULTS)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (ValueError, OSError):
            # 配置损坏不能让服务起不来，回落默认值并在日志里留痕
            cfg["_warning"] = "config.json 解析失败，已使用默认值"
    if key is not None:
        _CFG_CACHE["key"] = key
        _CFG_CACHE["cfg"] = cfg
    return cfg


def save_config(var_dir, cfg):
    path = config_path(var_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    _CFG_CACHE["key"] = None                    # 失效，下次读盘后重建
    return path


def ui_config_path(app_dir):
    return os.path.join(app_dir, "ui", "config")


def _write_ui_port(path, port):
    """改写单个入口文件里的 port，返回 True 表示已是目标值或改写成功。"""
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        for ent in (data.get(".url") or {}).values():
            if isinstance(ent, dict) and "port" in ent:
                if str(ent["port"]) != str(port):
                    ent["port"] = str(port)
                    changed = True
        if not changed:
            return True
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError):
        return False


def ui_port_candidates(app_dir):
    """桌面入口 `ui/config` 可能落在两处，两处都要改：
    - `/var/apps/<app>/ui/config`  ← 飞牛桌面实际读的就是这个（包根 ui/）
    - `/var/apps/<app>/target/ui/config` ← app.tgz 里同名文件的副本（TRIM_APPDEST）
    只改后者是 0.4.x「图标指向端口和实际端口对不上」的直接原因。
    """
    out = [ui_config_path(app_dir)]
    parent = os.path.dirname(os.path.abspath(app_dir))
    if os.path.basename(parent) not in ("", "target"):
        out.append(ui_config_path(parent))
    elif os.path.basename(parent) == "target":
        out.append(ui_config_path(os.path.dirname(parent)))
    uniq = []
    for p in out:
        if p not in uniq:
            uniq.append(p)
    return uniq


def set_ui_port(app_dir, port):
    """把桌面入口里的端口改成实际值（候选路径全改，任一成功即算成功）。"""
    ok = False
    for path in ui_port_candidates(app_dir):
        if _write_ui_port(path, port):
            ok = True
    return ok


def apply_port(var_dir=None, app_dir=None, port=18081):
    """端口落三处：config.json（下次启动用它）、ui/config（桌面图标）、var/port.txt（脚本读）。"""
    port = clamp_port(port)
    if var_dir:
        os.makedirs(var_dir, exist_ok=True)
        cfg = load_config(var_dir)
        cfg["service_port"] = port
        save_config(var_dir, cfg)
        try:
            with open(os.path.join(var_dir, "port.txt"), "w", encoding="utf-8") as f:
                f.write(str(port))
        except OSError:
            pass
    ok_ui = set_ui_port(app_dir, port) if app_dir else False
    return {"port": port, "ui_config": ok_ui}


def read_port(var_dir, default=18081):
    """脚本侧读实际端口：优先 var/port.txt（服务启动时写的真实值），其次配置。"""
    p = os.path.join(var_dir, "port.txt")
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v.isdigit():
                return clamp_port(v, default)
        except OSError:
            pass
    return clamp_port(load_config(var_dir).get("service_port"), default)


def merge_defaults(var_dir):
    """升级用：补齐新版默认值，保留用户已有配置。"""
    old = {}
    path = config_path(var_dir)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
        except (ValueError, OSError):
            old = {}
    cfg = dict(DEFAULTS)
    cfg.update(old)
    cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_config(var_dir, cfg)
    return cfg


# ---------------------------------------------------------------- 进程

def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # Windows 上 os.kill(pid, 0) 会真的调 TerminateProcess，绝对不能用
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def low_priority_prefix(cfg):
    """nice 19 + ionice best-effort，NVR 回放优先级永远高于我们。"""
    if not cfg.get("low_priority", True):
        return []
    prefix = []
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "2", "-n", "7"]
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    return prefix


# ---------------------------------------------------------------- 并发闸门

class SlotPool:
    """用目录原子 mkdir 做信号量，跨平台且不依赖 flock。

    每个槽位是一个目录，里面放 owner.json（pid/时间/用途）。
    进程崩了留下的僵尸槽位靠 pid 存活检查 + 超时回收。
    """

    def __init__(self, root, size):
        self.root = root
        self.size = max(1, min(int(size or 1), 8))
        os.makedirs(root, exist_ok=True)

    def _slot_dir(self, i):
        return os.path.join(self.root, "slot-%d.lock" % i)

    def _read_owner(self, path):
        try:
            with open(os.path.join(path, "owner.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _stale(self, path, ttl=6 * 3600):
        info = self._read_owner(path)
        pid = info.get("pid")
        if pid and not pid_alive(int(pid)):
            return True
        ts = info.get("ts") or 0
        if ts and (time.time() - ts) > ttl:
            return True
        # 连 owner.json 都没写进去（进程在 mkdir 后被 kill），按目录时间兜底
        if not info:
            try:
                if (time.time() - os.path.getmtime(path)) > 300:
                    return True
            except OSError:
                return True
        return False

    def _reclaim(self, path):
        for name in os.listdir(path):
            try:
                os.remove(os.path.join(path, name))
            except OSError:
                pass
        try:
            os.rmdir(path)
        except OSError:
            pass

    def acquire(self, owner="worker"):
        for i in range(self.size):
            path = self._slot_dir(i)
            if os.path.isdir(path) and self._stale(path):
                self._reclaim(path)
            try:
                os.mkdir(path)
            except FileExistsError:
                continue
            except OSError:
                return None
            with open(os.path.join(path, "owner.json"), "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "owner": owner, "ts": time.time()},
                          f, ensure_ascii=False)
            return i
        return None

    def release(self, i):
        path = self._slot_dir(i)
        if os.path.isdir(path):
            self._reclaim(path)

    def status(self):
        used, slots = [], []
        for i in range(self.size):
            path = self._slot_dir(i)
            if os.path.isdir(path) and not self._stale(path):
                info = self._read_owner(path)
                used.append(i)
                slots.append({"slot": i, "owner": info.get("owner", "?"),
                              "pid": info.get("pid"), "since": info.get("ts")})
            elif os.path.isdir(path):
                self._reclaim(path)
        return {"total": self.size, "used": len(used), "active": slots}


# ---------------------------------------------------------------- 核显自检

def _run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss" % timeout
    except OSError as e:
        return 126, str(e)


# 服务进程常以精简 PATH 启动，`shutil.which` 会漏掉 apt 装到 /usr/bin 的 ffmpeg。
# 这些绝对路径作为兜底，保证「用户已 apt install ffmpeg」时一定能被找到。
_FFMPEG_ABS = ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/ffmpeg/bin/ffmpeg")
_FFPROBE_ABS = ("/usr/bin/ffprobe", "/usr/local/bin/ffprobe", "/opt/ffmpeg/bin/ffprobe")


def _usable(path):
    """存在且可执行才算数：打包带的静态 ffmpeg 可能丢了执行位。"""
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_ffmpeg(app_dir):
    # 1) 自带二进制（优先：版本自洽，不依赖系统环境）
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = os.path.join(app_dir, "bin", name)
        if _usable(p):
            return p
    # 2) PATH
    w = shutil.which("ffmpeg")
    if w:
        return w
    # 3) 常见绝对路径
    for p in _FFMPEG_ABS:
        if _usable(p):
            return p
    return None


def find_ffprobe(app_dir):
    """与 ffmpeg 同目录优先：自己带的包版本自洽，判定结果才稳定。"""
    for name in ("ffprobe", "ffprobe.exe"):
        p = os.path.join(app_dir, "bin", name)
        if _usable(p):
            return p
    w = shutil.which("ffprobe")
    if w:
        return w
    for p in _FFPROBE_ABS:
        if _usable(p):
            return p
    return None


def gpu_report(app_dir, cfg=None):
    """返回三态：qsv / vaapi / none，附带每一项的原始结果，便于排错。"""
    cfg = cfg or {}
    sim = os.environ.get("NVR_SIMULATE_GPU", "").strip().lower()
    if sim in ("qsv", "vaapi", "none"):
        return {"state": sim, "reason": "模拟值（仅用于界面演示）", "simulated": True,
                "checks": {}, "ffmpeg": find_ffmpeg(app_dir)}

    if cfg.get("gpu") == "off":
        return {"state": "none", "reason": "已在设置中关闭硬件加速", "simulated": False,
                "checks": {}, "ffmpeg": find_ffmpeg(app_dir)}

    checks = {}
    ff = find_ffmpeg(app_dir)
    checks["ffmpeg_found"] = bool(ff)

    dri = "/dev/dri/renderD128"
    checks["device"] = os.path.exists(dri)
    if os.path.isdir("/dev/dri"):
        try:
            checks["dri_nodes"] = sorted(os.listdir("/dev/dri"))
        except OSError:
            checks["dri_nodes"] = []

    va = shutil.which("vainfo")
    checks["vainfo_found"] = bool(va)
    checks["vainfo_hevc"] = False
    if va:
        rc, out = _run([va], timeout=8)
        checks["vainfo_hevc"] = "HEVC" in out and "VAProfileHEVCMain" in out
        checks["vainfo_head"] = [l for l in out.splitlines() if "VAProfile" in l][:6]

    checks["qsv_available"] = False
    checks["vaapi_available"] = False
    if ff:
        rc, out = _run([ff, "-hide_banner", "-hwaccels"], timeout=8)
        low = out.lower()
        checks["qsv_available"] = "qsv" in low
        checks["vaapi_available"] = "vaapi" in low
        checks["hwaccels"] = [l.strip() for l in out.splitlines() if l.strip()
                              and not l.startswith("Hardware acceleration")]

    checks["qsv_encode"] = False
    checks["vaapi_encode"] = False
    if ff and checks["qsv_available"]:
        rc, out = _run([ff, "-hide_banner", "-loglevel", "error",
                        "-init_hw_device", "qsv=hw",
                        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
                        "-t", "1", "-c:v", "hevc_qsv", "-global_quality", "26",
                        "-f", "null", "-"], timeout=20)
        checks["qsv_encode"] = rc == 0
        if rc != 0:
            checks["qsv_error"] = out.strip()[-400:]
    if ff and checks["vaapi_available"] and not checks["qsv_encode"]:
        rc, out = _run([ff, "-hide_banner", "-loglevel", "error",
                        "-vaapi_device", "/dev/dri/renderD128",
                        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
                        "-t", "1", "-vf", "format=nv12,hwupload",
                        "-c:v", "hevc_vaapi", "-qp", "26", "-f", "null", "-"],
                       timeout=20)
        checks["vaapi_encode"] = rc == 0

    if checks["qsv_encode"]:
        return {"state": "qsv", "reason": "QSV 解码与 HEVC 硬件编码均可用",
                "simulated": False, "checks": checks, "ffmpeg": ff}
    if checks["vaapi_encode"]:
        return {"state": "vaapi", "reason": "QSV 不可用，已回落到 VA-API 编码",
                "simulated": False, "checks": checks, "ffmpeg": ff}
    if not checks["device"]:
        reason = "未发现 /dev/dri/renderD128，核显未透传或被内核驱动禁用"
    elif not checks["ffmpeg_found"]:
        reason = "未找到 ffmpeg"
    elif not checks["vainfo_found"]:
        reason = "缺少 vainfo，无法确认 VA-API 驱动（Gen12 需要 intel-media-driver）"
    elif not checks["vainfo_hevc"]:
        reason = "VA-API 驱动未提供 HEVC profile，可能装的是老的 i965 驱动"
    else:
        reason = "硬件编码自检失败，详见 checks 字段"
    return {"state": "none", "reason": reason, "simulated": False,
            "checks": checks, "ffmpeg": ff}


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--var", default=os.environ.get("TRIM_PKGVAR", "./var"))
    ap.add_argument("--app", default=os.environ.get("TRIM_APPDEST", "./app"))
    ap.add_argument("--source", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--record", default=None)
    ap.add_argument("--port", default=None)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("command",
                    choices=["gpu", "migrate", "slots", "launch-prefix", "init", "set",
                             "setport", "pinport"])
    a = ap.parse_args()

    if a.command == "gpu":
        print(json.dumps(gpu_report(a.app, load_config(a.var)), ensure_ascii=False, indent=2))
    elif a.command == "init":
        cfg = load_config(a.var)
        if a.source:
            cfg["source_dir"] = a.source
        if a.record:
            # 录制根目录：本应用自己写录像的地方，缩时片段落在它的 _clip 子目录
            cfg["record_dir"] = a.record
        if a.output:
            cfg["output_dir"] = a.output
        if a.port:
            cfg["service_port"] = clamp_port(a.port)
        cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_config(a.var, cfg)
        print(json.dumps({"ok": True, "source_dir": cfg["source_dir"],
                          "record_dir": cfg["record_dir"],
                          "output_dir": cfg["output_dir"],
                          "service_port": cfg["service_port"]}, ensure_ascii=False))
    elif a.command == "setport":
        want = clamp_port(a.port if a.port else load_config(a.var).get("service_port"))
        final = pick_port(want)
        res = apply_port(a.var, a.app, final)
        res["requested"] = want
        res["shifted"] = final != want
        print(json.dumps(res, ensure_ascii=False))
    elif a.command == "pinport":
        # 只把已知的最终端口写进桌面入口，不重新探测占用。
        # 用在启动阶段：安装期应用目录可能不可写，启动后通常已归属应用用户。
        port = clamp_port(a.port if a.port else read_port(a.var))
        ok = set_ui_port(a.app, port)
        print(json.dumps({"port": port, "ui_config": ok}, ensure_ascii=False))
    elif a.command == "set":
        cfg = load_config(a.var)
        for item in a.set:
            k, _, v = item.partition("=")
            if k in DEFAULTS:
                if isinstance(DEFAULTS[k], bool):
                    cfg[k] = v.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(DEFAULTS[k], int):
                    cfg[k] = int(v)
                else:
                    cfg[k] = v
        cfg["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_config(a.var, cfg)
        print(json.dumps(cfg, ensure_ascii=False))
    elif a.command == "migrate":
        cfg = merge_defaults(a.var)
        print(json.dumps({"ok": True, "concurrency": cfg["concurrency"]}, ensure_ascii=False))
    elif a.command == "slots":
        cfg = load_config(a.var)
        print(json.dumps(SlotPool(os.path.join(a.var, "slots"),
                                  cfg["concurrency"]).status(), ensure_ascii=False))
    elif a.command == "launch-prefix":
        print(" ".join(low_priority_prefix(load_config(a.var))))
