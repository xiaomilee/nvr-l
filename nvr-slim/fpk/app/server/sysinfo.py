#!/usr/bin/env python3
"""系统资源与硬件编码能力：界面上所有"CPU / 内存 / 核显"数字的唯一来源。

以前这些数字是页面里写死的假值（"CPU 3%"、"96.4 MB"、"Intel UHD 730"），
用户换了机器、关掉核显、或者干脆没装 ffmpeg，界面照样报"核显可用"，
排错时完全被误导。这里改成真读 /proc，读不到就明确说"未采集"，不编。

三个口径要分清，混在一起会得出错误结论：
  proc_cpu  NVR 进程自己的 CPU（单核百分比：100 = 吃满一个核）
  sys_cpu   整机 CPU 利用率（多核均值 0~100）
  mem       私有内存 RSS（MB）—— 共享库不计入，比 VmSize 更接近"真实占用"

硬件编码：`caps` 里的 qsv/vaapi/nvenc 是**编码器可用性**，不等于"已经在用"。
录制走 -c copy 不编码；只有画了输出遮蔽区、或走"精细重编码"时才真正用核显。
所以调用方必须自己判断当前链路有没有编码动作，不能拿 caps 直接写"正在使用核显"。
"""
import os
import shutil
import subprocess
import threading
import time

_LOCK = threading.Lock()
_CACHE = {}          # key -> (ts, value)
# (上次采样时刻, 上次 busy jiffies, 上次 total jiffies)
# 三个字段缺一不可：sys_cpu_pct() 要读 [0]/[1]/[2]。以前这里写成二元组
# (0.0, 0.0)，首次调用 _TICK[2] 直接 IndexError('tuple index out of range')。
# 更糟的是赋值 _TICK = (...) 写在下标访问之后，异常一抛就永远执行不到 ——
# 于是**每次** /api/status 都抛同一个错，状态接口从此常驻 500，永不恢复。
_TICK = (0.0, 0.0, 0.0)
_BOOT = None


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def _uptime():
    return float((_read("/proc/uptime", "0 0").split() or ["0"])[0] or 0)


# ---------------------------------------------------------------- CPU

def _cpu_total():
    """整机 CPU 累计 (busy, total) jiffies。读不到返回 None（非 Linux）。"""
    line = (_read("/proc/stat").splitlines() or [""])[0]
    if not line.startswith("cpu "):
        return None
    try:
        v = [int(x) for x in line.split()[1:]]
    except ValueError:
        return None
    if len(v) < 4:
        return None
    idle = v[3] + (v[4] if len(v) > 4 else 0)      # idle + iowait
    total = sum(v)
    return total - idle, total


def sys_cpu_pct():
    """整机 CPU 利用率（0~100），两次采样求差。读不到返回 None。"""
    global _TICK
    cur = _cpu_total()
    if cur is None:
        return None
    busy, total = cur
    # 解包而不是逐个下标：以后谁再改 _TICK 的结构，这里会立刻 ValueError
    # 报出「字段对不上」，而不是留一个静默的 IndexError 让状态接口常驻 500。
    with _LOCK:
        _, pb, pt = _TICK
        _TICK = (time.time(), busy, total)
    # 上一次是本进程内第一次采样：没法算差值，用 /proc/stat 的累计均值兜底
    if not pt or total <= pt:
        return round(100.0 * busy / total, 1) if total else None
    dt = total - pt
    db = busy - pb
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * db / dt)), 1)


def proc_cpu_pct(pid=None):
    """NVR 进程自身 CPU（单核百分比）。用 /proc/<pid>/stat 的 utime+stime 增量。"""
    pid = int(pid or os.getpid())
    raw = _read("/proc/%d/stat" % pid)
    if not raw:
        return None
    # comm 字段可能含空格/括号，从最后一个 ')' 之后开始切
    tail = raw[raw.rfind(")") + 1:].split()
    if len(tail) < 13:
        return None
    try:
        ticks = int(tail[11]) + int(tail[12])       # utime + stime
    except (ValueError, IndexError):
        return None
    key = "proc_cpu:%d" % pid
    now = time.time()
    with _LOCK:
        prev = _CACHE.get(key)
        _CACHE[key] = (now, ticks)
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    if not prev:
        # 首次：用进程存活时长估平均值，比返回 0 更接近真实
        up = _uptime()
        if up > 0:
            return round(100.0 * (ticks / float(hz)) / up, 1)
        return None
    dt = now - prev[0]
    if dt <= 0:
        return None
    return round(100.0 * ((ticks - prev[1]) / float(hz)) / dt, 1)


# ---------------------------------------------------------------- 内存

def mem_mb(pid=None):
    """进程 RSS（MB）。优先 /proc/<pid>/statm 的 resident，缺失退 VmRSS。"""
    pid = int(pid or os.getpid())
    st = _read("/proc/%d/statm" % pid)
    if st:
        try:
            pages = int(st.split()[1])
            return round(pages * (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf")
                                  else 4096) / 1048576.0, 1)
        except (ValueError, IndexError):
            pass
    for line in _read("/proc/%d/status" % pid).splitlines():
        if line.startswith("VmRSS:"):
            try:
                return round(int(line.split()[1]) / 1024.0, 1)
            except (ValueError, IndexError):
                break
    return None


def mem_total_mb():
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            try:
                return round(int(line.split()[1]) / 1024.0)
            except (ValueError, IndexError):
                break
    return None


# ---------------------------------------------------------------- 磁盘

def disk(path):
    """录像目录所在分区的 (可用GB, 总GB)。目录不存在返回 (None, None)。"""
    try:
        u = shutil.disk_usage(path)
    except (OSError, FileNotFoundError):
        return None, None
    return round(u.free / 1073741824.0, 1), round(u.total / 1073741824.0, 1)


# ---------------------------------------------------------------- 硬件编码
#
# 判断顺序刻意做成"先看内核设备、再看 ffmpeg 有没有编进编码器、最后才真跑一次"：
#   /dev/dri/renderD128  不存在 → 一定没有核显（NAS 上最常见：没透传）
#   ffmpeg -encoders     没有 h264_qsv → 即使有设备也用不了
#   真跑 1 秒测试           前两步都过但驱动坏了/被占用，只有真跑才知道

CAP_TTL = 300.0          # 编码能力自检很贵（要跑 ffmpeg），5 分钟一次
CAP_ENCODERS = {
    "qsv":   ("h264_qsv", "hevc_qsv"),
    "vaapi": ("h264_vaapi", "hevc_vaapi"),
    "nvenc": ("h264_nvenc", "hevc_nvenc"),
}
CAP_DEVICE = {
    "qsv": "/dev/dri/renderD128",
    "vaapi": "/dev/dri/renderD128",
    "nvenc": "/dev/nvidia0",
}


def _encoders(ffmpeg):
    if not ffmpeg:
        return None
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (r.stdout or b"").decode("utf-8", "replace")
    return set(w for w in out.split() if "_" in w)


def hw_caps(ffmpeg, ttl=CAP_TTL):
    """返回 {"qsv":bool,"vaapi":bool,"nvenc":bool,"best":"qsv|vaapi|nvenc|none",
             "device":bool,"checked_at":ts,"error":str,"simulated":bool}。

    结果缓存 ttl 秒；ffmpeg 路径变了立刻重测（用户可能刚放了静态 ffmpeg 进去）。

    关于 NVR_SIMULATE_GPU：那是 runtime.gpu_report() 的演示开关，用来在没有真核显
    的机器上演示界面。这个函数**不伪造**结果，但必须把 "当前处于模拟态" 如实
    带出去 —— 否则页面上会同时出现"核显可用"和"没有 /dev/dri"两句互相打架的话。
    """
    key = "hw:%s" % (ffmpeg or "")
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]

    sim = (os.environ.get("NVR_SIMULATE_GPU") or "").strip().lower()

    res = {"qsv": False, "vaapi": False, "nvenc": False, "best": "none",
           "device": False, "checked_at": int(now), "error": "",
           "simulated": sim in ("qsv", "vaapi", "none")}
    if res["simulated"]:
        # 演示模式：不伪造探测结果，但要让界面能说清"这是模拟的"
        res["sim_env"] = sim
    if not ffmpeg:
        res["error"] = "未找到 ffmpeg，无法判断硬件编码能力"
        with _LOCK:
            _CACHE[key] = (now, res)
        return res

    if sys_platform() == "linux":
        res["render_node"] = os.path.exists("/dev/dri/renderD128")
        try:
            res["dri"] = sorted(os.listdir("/dev/dri")) if os.path.isdir("/dev/dri") else []
        except OSError:
            res["dri"] = []
    enc = _encoders(ffmpeg)
    if enc is None:
        res["error"] = "ffmpeg 无法执行，无法判断硬件编码能力"
        with _LOCK:
            _CACHE[key] = (now, res)
        return res
    for k, names in CAP_ENCODERS.items():
        res[k] = all(n in enc for n in names) if k != "nvenc" else names[0] in enc
    # 有编码器还要有设备，否则一定编不了
    if sys_platform() == "linux":
        for k in ("qsv", "vaapi"):
            if res[k] and not os.path.exists(CAP_DEVICE[k]):
                res[k] = False
        if res["nvenc"] and not os.path.exists(CAP_DEVICE["nvenc"]):
            res["nvenc"] = False
    for k in ("qsv", "vaapi", "nvenc"):
        if res[k]:
            res["best"] = k
            break
    with _LOCK:
        _CACHE[key] = (now, res)
    return res


def sys_platform():
    import platform
    return platform.system().lower()


def reset_cache():
    """测试用：清空所有缓存。"""
    with _LOCK:
        _CACHE.clear()


def snapshot(ffmpeg=None, pid=None, record_dir=None, gpu_report=None):
    """给 /api/status 与页面用的一份完整快照。读不到的字段一律 None，不编。

    gpu_report：runtime.gpu_report() 的返回值（可省）。带上它是为了和录制/压制
    实际用的那份判断保持一致 —— 页面说"核显可用"、实际却按软编跑，是最难查的错。
    """
    free, total = disk(record_dir) if record_dir else (None, None)
    hw = hw_caps(ffmpeg)
    if gpu_report:
        # 把 runtime 的结论并进 hw，页面只认这一个出口
        hw = dict(hw)
        hw["state"] = gpu_report.get("state")
        hw["reason"] = gpu_report.get("reason") or ""
        hw["effective"] = (gpu_report.get("state") not in (None, "", "none"))
    return {
        "proc_cpu": proc_cpu_pct(pid),
        "sys_cpu": sys_cpu_pct(),
        "mem_mb": mem_mb(pid),
        "mem_total_mb": mem_total_mb(),
        "disk_free_gb": free,
        "disk_total_gb": total,
        "hw": hw,
    }
