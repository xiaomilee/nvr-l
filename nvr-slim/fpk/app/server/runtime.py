#!/usr/bin/env python3
"""NVR 运行时基础件：配置、核显自检、并发闸门、低优先级启动。

只用标准库，飞牛自带的 python3 即可运行。
"""
import json
import os
import re
import shutil
import subprocess
import time

VERSION = "0.9.91"

# RTSP 地址里的 user:pass@ 一律脱敏成 ***：日志、API 响应、诊断导出共用这一条规则
_CRED_RE = re.compile(r'(rtsp[s]?://)[^@/\s]+@')

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
    "snapshot_interval": 10,    # 直播页快照刷新间隔（秒）
    # 录制根目录。默认放在 vol2 的数据卷（大容量监控盘常用位置）；
    # 这是「不常改」的配置，界面默认只读，要点「修改」才展开编辑，
    # 保存时由 /api/dir/check 真实探测存在性与写权限。
    "record_dir": "/vol1/@appshare/nvr-output",  # 录像与缩时片段根目录：fnOS 自动建目录+授权的共享目录
    "cleanup_at": "03:00",            # 每天清理过期录像的时刻（也是归档裁剪时刻）
    # ---- 归档裁剪（Aqara 式三层存储：全时短留 > 归档长留 > 台账永久） ----
    # 夜间任务固定顺序：先归档（临期全时里的活动窗口裁进 _archive），后清理
    # （顺序反了明天过期的日期就来不及归档，会被当普通过期目录删掉）。
    "archive_enable": True,           # 夜间任务是否做归档裁剪
    "archive_days": 90,               # 归档片段保留天数（_archive）
    "archive_batch": 120,             # 单次归档最多裁多少个分片（跨夜分批消化）
    # ---- 录制（由本应用拉主码流落盘，不再依赖飞牛 NVR 的目录） ----
    "record_enable": True,
    "record_segment_sec": 300,        # 分片时长 60~900 秒
    "record_reconnect_sec": 5,        # 断流重连退避基数
    # ---- 统一取流（0.9.80）----
    # 每路只对摄像头建一条 RTSP 连接，再在本机用 MPEG-TS 广播给录制/取帧/AI。
    # 以前三处各自拉流：5 路每 5 秒就是 120 次新建会话，廉价摄像头会因此踢人。
    # 关掉即退回旧流程（各自直连）—— 留这条开关是为了现场能立刻回退。
    "ingest_enable": True,
    # 分片 MP4（0.9.71 默认开）：moov 写在文件头、每个关键帧一个 moof，
    # 进程被硬杀/断电后已写下的部分仍能播，损失上限是一个 GOP 而不是整片。
    # 关掉退回整片 moov（moov 在尾部，必须正常收尾才完整）。
    "record_frag": True,
    # ---- 录制与压制的衔接方式 ----
    "link_mode": "live",              # live=录完即压 | scheduled=定时批量
    "link_interval_h": 6,             # 定时模式间隔，1~24 小时
    "link_max_queue": 200,            # 待处理上限，超出则暂缓入队（背压）
    # ---- 缩时片段切分方式（通用默认值，每路可单独覆盖） ----
    # join  = 整段拼接：一个分片的活动区间拼成一个片段（流复制、无损、关键帧切点）
    # split = 活动切片：每个活动区间单独成片、重编码精确到帧，热力图定位更细
    "clip_style": "join",
    # ---- 缩时方案（Aqara 式标记与视频解耦） ----
    # virtual     = 虚拟缩时：判定只写活动台账（_ledger/），不产出视频文件；
    #               回放靠时间轴标记 + 全时视频按需 seek（零常驻编码）
    # materialize = 物化切片：既有 join/split 导出照旧，台账同步双写（统一地基）
    # off         = 只录全时，不判定
    "condense_mode": "virtual",
    # ---- 服务端口 ----
    # 安装向导里选定后写在这里；TRIM_SERVICE_PORT 只是 manifest 的静态默认值，
    # 用户改过端口就以本字段为准。范围见 PORT_LIMITS。
    # 默认端口 0.9.18 起改为 18080（向导里可改）；已装用户的 config.json
    # 里存的是旧值，merge_defaults 会保留，不受默认值变化影响。
    "service_port": 18080,
    # ---- AI 检测（人员 / 两轮车 / 车辆，0.9.60）----
    # 总闸默认关（opt-in）；嵌套块由 detect.norm_detect 强校（enum 白名单 +
    # 数值夹取 + trigger 至少一项），见 coerce 的 dict 分支。
    "detect": {
        "enable": False,
        "mode": "snap",
        "device": "auto",
        "classes": {"person": True, "two_wheeler": True, "vehicle": True},
        "conf": {"person": 0.50, "two_wheeler": 0.33, "vehicle": 0.40},
        "trigger": {"motion": True, "person": True, "vehicle": True},
        "live_fps": 5,
        "night": "throttle",
    },
    # ---- 通知（0.9.60 设计，P1 落地）----
    "notify": {
        "webhook": "",
        "motion": False,
        "person": False,
        "vehicle": False,
        "cooldown": 60,
    },
    "updated_at": None,
}

SCAN_LABEL = {"now": "立即", "m15": "每 15 分钟", "h1": "每小时", "d1": "每天 03:00"}
LINK_LABEL = {"live": "即刻压制", "scheduled": "定时压制"}

# 分片时长：太短则文件开合频繁、判定上下文不足；太长则热数据跑出 page cache
SEGMENT_LIMITS = (60, 900)
PORT_LIMITS = (1024, 65000)
LINK_INTERVAL_LIMITS = (1, 24)


# 离散取值的键：只认白名单，别让 true/false 混进来顶掉 "auto"/"off"
_ENUM_KEYS = {"gpu": ("auto", "qsv", "off"), "scan_mode": ("now", "m15", "h1", "d1"),
              "process_mode": ("fast", "fine"),
              "clip_style": ("join", "split"),
              "condense_mode": ("virtual", "materialize", "off")}


def coerce(key, value):
    """把外部推来的配置值收敛成 DEFAULTS 里的类型。

    这里必须按 DEFAULTS 的类型逐键校验 —— 以前直接 cfg[k] = body[k]，
    前端推个 `gpu: true` 就把 `"auto"` 顶掉了，而 gpu_report / status_payload
    都靠字符串比较，于是"硬件不可用自动降级单路"的保护静默失效。
    """
    default = DEFAULTS.get(key)
    if key in _ENUM_KEYS:
        if not isinstance(value, str):
            return default                    # 只收字符串，别让 dict/list 混成 "{'x': 1}"
        v = value.strip().lower()
        return v if v in _ENUM_KEYS[key] else default
    if isinstance(default, dict):
        # 嵌套配置块（detect / notify）：只收 dict，交给各自的 norm_* 强校
        # （enum 白名单 + 数值夹取 + trigger 至少一项），非法值回落默认。
        if key == "detect" and isinstance(value, dict):
            import detect
            return detect.norm_detect(value)[0]
        if key == "notify" and isinstance(value, dict):
            import detect
            return detect.norm_notify(value)
        return json.loads(json.dumps(default))   # 深拷贝默认值，防共享引用
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if not isinstance(value, str):
            return default
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if default is None:                     # updated_at 之类：只收字符串
        return str(value) if value is not None else None
    # 字符串键（cleanup_at / record_dir / source_dir…）：
    # str(None) 会得到 "None" 落盘，之后 retention.parse_hhmm("None") 天天抛异常，
    # 清理整条链全废。空值一律回落到默认值。
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return default
    v = value.strip()
    if not v:
        return default
    # 录像目录是唯一会真的拿去建目录/写文件的字符串键：形态不合法的一律
    # 回落默认值，别让 "nvr" 这种相对路径混进配置，之后录制器每 5 秒报一次错。
    if key == "record_dir":
        ok, _why = valid_record_dir(v)
        return v if ok else default
    return v


def redact_url(text):
    """把 RTSP 地址里的 user:pass@ 抹成 ***：日志与 API 响应共用一处规则。"""
    return _CRED_RE.sub(r'\1***@', str(text or ""))


def redact_cams(cams):
    """摄像头列表出接口前脱敏：id/name/掩码留着，url 里的凭据抹掉。"""
    out = []
    for c in cams or []:
        d = dict(c) if isinstance(c, dict) else c
        if isinstance(d, dict):
            for k in ("url", "sub_url"):
                if d.get(k):
                    d[k] = redact_url(d[k])
        out.append(d)
    return out


def redact_cfg(cfg):
    """整个配置对象出接口前脱敏：cfg["cameras"] 里的 url 也一并处理。"""
    d = dict(cfg or {})
    if isinstance(d.get("cameras"), list):
        d["cameras"] = redact_cams(d["cameras"])
    return d


def clamp_int(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def under(root, path):
    """path 是否真的落在 root 里面。

    只判断 startswith 是有洞的：root=/var/apps/nvr/ui 时
    /var/apps/nvr/ui2/x 也能通过。必须按「等于 root 或以 root+分隔符开头」比。
    """
    root = os.path.normpath(os.path.abspath(root))
    path = os.path.normpath(os.path.abspath(path))
    return path == root or path.startswith(root + os.sep)


# 录制目录的合法形态：绝对路径、不是盘根/系统目录。存这一份给 coerce 与
# /api/dir/check 共用，别让两处各写一套规则。
_DIR_DENY = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/run",
             "/sbin", "/sys", "/tmp", "/usr", "/var", "/root")


def valid_record_dir(path):
    """录像根目录合法性：绝对路径、不落在系统目录上。返回 (ok, 原因)。

    只做纯字符串判断、不用 os.path.normpath：生产在 Linux 上没问题，
    但开发机是 Windows，normpath 会把 "/vol2/x" 变成 "\\vol2\\x"，
    startswith("/") 就永远不成立 —— 测试没法跨平台跑。
    """
    p = str(path or "").strip()
    if not p:
        return False, "路径为空"
    if "\x00" in p:
        return False, "路径包含非法字符"
    # POSIX 绝对路径（/ 开头）或 Windows 盘符路径（C:\，仅开发机测试会用）
    is_win = len(p) >= 3 and p[1] == ":" and p[2] in ("\\", "/")
    if not p.startswith("/") and not is_win:
        return False, "必须是绝对路径（以 / 开头）"
    n = p.replace("\\", "/").rstrip("/")
    if not n or n.lower() in _DIR_DENY:
        return False, "不能使用系统目录或盘根"
    return True, ""


def probe_record_dir(path, create=True):
    """真实探测一个目录能不能用来存录像。

    三步都有独立结论，UI 才能告诉用户"差在哪一步"：
      1. 存在吗（不存在时可选自动创建）
      2. 是目录吗（存在但是个文件也不行）
      3. 真的写得进去吗 —— 落一个临时文件再删掉，比 os.access(X_OK) 可靠得多：
         ACL、只读挂载、磁盘满都会让 access 通过而写入失败。
    """
    p = str(path or "").strip()
    ok, why = valid_record_dir(p)
    if not ok:
        return {"ok": False, "path": p, "exists": False, "is_dir": False,
                "writable": False, "created": False, "error": why}
    n = os.path.normpath(p)
    created = False
    exists = os.path.isdir(n)
    if not exists and os.path.exists(n):
        return {"ok": False, "path": n, "exists": True, "is_dir": False,
                "writable": False, "created": False,
                "error": "该路径已被一个文件占用，不是目录"}
    if not exists:
        if not create:
            return {"ok": False, "path": n, "exists": False, "is_dir": False,
                    "writable": False, "created": False,
                    "error": "目录不存在（保存时可自动创建）"}
        try:
            os.makedirs(n, exist_ok=True)
            created = True
            exists = True
        except OSError as e:
            return {"ok": False, "path": n, "exists": False, "is_dir": False,
                    "writable": False, "created": False,
                    "error": "目录不存在且无法创建：%s" % e}
    # 写探针：真实落一个文件。文件名带随机后缀，避免并发探测互相踩。
    probe = os.path.join(n, ".nvr-write-probe-%d" % os.getpid())
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
        os.remove(probe)
    except OSError as e:
        return {"ok": False, "path": n, "exists": True, "is_dir": True,
                "writable": False, "created": created,
                "error": "目录存在但写不进去：%s（检查应用用户对该目录的权限/ACL）" % e}
    return {"ok": True, "path": n, "exists": True, "is_dir": True,
            "writable": True, "created": created, "error": ""}


def link_mode(v):
    return v if v in ("live", "scheduled") else "live"


# 每路切分方式的白名单："follow" 表示跟随通用 clip_style，不单独写枚举键
CLIP_MODES = ("follow", "join", "split")
CLIP_STYLE_LABEL = {"join": "整段拼接", "split": "活动切片"}


def clip_style(v):
    """通用切分方式：只认 join/split，其它回落 join（历史行为）。"""
    return v if v in ("join", "split") else "join"


def clip_mode(v):
    """每路切分方式：只认白名单，其它回落 follow。"""
    return v if v in CLIP_MODES else "follow"


def segment_sec(v):
    return clamp_int(v, SEGMENT_LIMITS[0], SEGMENT_LIMITS[1], 300)


def link_interval_h(v):
    return clamp_int(v, LINK_INTERVAL_LIMITS[0], LINK_INTERVAL_LIMITS[1], 6)


def clamp_port(v, default=18080):
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
    # 嵌套配置块归一（0.9.60）：旧配置/手改的坏值在这里自愈，
    # 与 coerce 的 dict 分支走同一套 norm_*，读写两侧一个口径。
    try:
        import detect
        cfg["detect"] = detect.norm_detect(cfg.get("detect"))[0]
        cfg["notify"] = detect.norm_notify(cfg.get("notify"))
    except Exception:
        cfg["detect"] = json.loads(json.dumps(DEFAULTS["detect"]))
        cfg["notify"] = json.loads(json.dumps(DEFAULTS["notify"]))
    if key is not None:
        _CFG_CACHE["key"] = key
        _CFG_CACHE["cfg"] = cfg
    return cfg


def save_config(var_dir, cfg):
    path = config_path(var_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        try:
            os.fsync(f.fileno())        # 断电时宁可慢一点，也别留半截配置
        except OSError:
            pass
    # Windows 上 os.replace 会因目标文件被杀毒/索引服务/别的进程短暂持有句柄
    # 而抛 PermissionError(WinError 5)。以前这里直接冒泡，整个 POST /api/config
    # 变成 500 空响应，前端只看到"保存失败"却不知道原因。重试几次足够躲过这种
    # 毫秒级的占用。
    last = None
    for i in range(6):
        try:
            os.replace(tmp, path)
            break
        except OSError as e:
            last = e
            time.sleep(0.02 * (i + 1))
    else:
        try:
            os.unlink(tmp)              # 别在 var 目录里留一个 .tmp 残骸
        except OSError:
            pass
        raise last
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


def apply_port(var_dir=None, app_dir=None, port=18080):
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


def read_port(var_dir, default=18080):
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
