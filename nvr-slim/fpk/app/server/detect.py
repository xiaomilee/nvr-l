#!/usr/bin/env python3
"""AI 检测引擎：人员 / 两轮车 / 车辆（YOLO11n ONNX + onnxruntime CPU）。

在整套系统里的位置（见《人员车辆检测-功能选项设计.md》§3）：
  快照节奏的抽帧 → 本模块推理 → 类别+框 → 忽略区过滤 → 台账窗口带 cls 计数
  → 消费侧（回放页 chips / 缩时触发闸 / 通知）按 detect.trigger 裁决。

四条设计底线：
1. **依赖缺失不崩不静默**：随包 onnxruntime/numpy wheel 与 NAS 的 Python 版本
   不匹配、或模型文件缺失时，ensure() 返回 (False, 原因)，界面状态行如实显示，
   检测整体关闭——绝不拖垮预览/录制/缩时任何既有链路。
2. **总闸语义**：全局 enable=false 时本模块根本不会被调度（DetectWorker 不启动），
   每路 on/off 一概无效；enable=true 且每路 follow 才用全局类别。
3. **消费侧裁决**：台账照实记录所有检出类别（回放筛选用），「算不算活动」由
   detect.trigger 在消费侧判定——AI 关闭时 trigger.motion 兜底，行为与旧版一致。
4. **纯函数与 IO 分离**：norm_*/trigger_ok/parse_ppm/letterbox/postprocess/
   windows_from_hits 全部无副作用，单测不需要模型；Engine/DetectWorker 才碰
   文件、进程、线程。

模型与运行时分发：
  app/share/models/yolo11n.onnx          ~10.7MB（COCO 80 类，opset 19）
  app/vendor/onnxruntime-1.20.1-cp31x.whl 随包（manylinux_2_28，glibc≥2.28）
  app/vendor/numpy-*.whl                  随包
  首次使用时按 sys.version_info 选对应 wheel，解压到 var_dir/ort/py3XX/ 再 import
  ——绕开「fnOS 只带标准库」与 wheel ABI 错配两个坑（解压有 .ok 标记，原子）。
"""
import json
import os
import re
import subprocess
import threading
import time
import zipfile

try:
    # 合并口径单一来源（ledger.AI_GAP_SEC）。必须在模块级导入：
    # _merge_windows 是 staticmethod，函数内 import 只会变成那个函数的局部名
    # ——0.9.65 就是栽在「模块级函数引用 main() 局部名」上。
    import ledger as _ledger
except ImportError:                    # 单独跑本文件自检时可能不在 sys.path 上
    _ledger = None

# ---------------------------------------------------------------- 类别体系

# COCO 80 类里本项目关心的 6 类 → 内部三分类。
# bicycle/motorcycle 合并为 two_wheeler：电动车没有独立 COCO 类，按外观会在
# 两类间漂移（有脚踏→bicycle，无脚踏宽体→motorcycle），产品层合并后漂移无害。
COCO_MAP = {
    0: "person",
    1: "two_wheeler",
    3: "two_wheeler",
    2: "vehicle",
    5: "vehicle",
    7: "vehicle",
}
CATEGORIES = ("person", "two_wheeler", "vehicle")

# 灵敏度三档 → 分品类阈值。两轮车系统性放低（远距离小目标，召回优先）。
CONF_PRESETS = {
    "high": {"person": 0.45, "two_wheeler": 0.28, "vehicle": 0.35},
    "std":  {"person": 0.50, "two_wheeler": 0.33, "vehicle": 0.40},
    "low":  {"person": 0.55, "two_wheeler": 0.38, "vehicle": 0.45},
}
CONF_LO, CONF_HI = 0.05, 0.90

DETECT_DEFAULTS = {
    "enable": False,                       # 总闸：false 时每路开了也不检
    "mode": "snap",                        # snap=快照节奏抽帧 | live=实时抽帧（P1）
    "device": "auto",                      # auto | cpu | gpu
    "classes": {"person": True, "two_wheeler": True, "vehicle": True},
    "conf": dict(CONF_PRESETS["std"]),     # 显式分品类阈值（P2 开放，先吃灵敏度）
    "trigger": {"motion": True, "person": True, "vehicle": True},
    "live_fps": 5,
    "night": "throttle",                   # full | throttle | off
}
NOTIFY_DEFAULTS = {
    "webhook": "",
    "motion": False,
    "person": False,
    "vehicle": False,
    "cooldown": 60,
}

DETECT_ENUMS = {
    "mode": ("snap", "live"),
    "device": ("auto", "cpu", "gpu"),
    "night": ("full", "throttle", "off"),
}
CAM_DETECT_MODES = ("follow", "on", "off")

MODEL_NAME = "yolo11n.onnx"
INPUT_SIZE = 640
NMS_IOU = 0.45


# ---------------------------------------------------------------- 配置强校

def _as_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "on", "yes"):
            return True
        if s in ("0", "false", "off", "no"):
            return False
    return default


def _clamp(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                      # NaN
        return default
    return max(lo, min(hi, f))


def norm_classes(d, default=None):
    """三键白名单归一。全关是合法输入（调用方按 off 处理并提示）。"""
    base = dict(default or DETECT_DEFAULTS["classes"])
    if not isinstance(d, dict):
        return base
    for k in CATEGORIES:
        if k in d:
            base[k] = _as_bool(d[k], base[k])
    return base


def norm_conf(d):
    out = {}
    for k in CATEGORIES:
        out[k] = round(_clamp((d or {}).get(k),
                              CONF_LO, CONF_HI,
                              DETECT_DEFAULTS["conf"][k]), 3)
    return out


def conf_from_sensitivity(sens):
    return dict(CONF_PRESETS.get(sens, CONF_PRESETS["std"]))


def norm_trigger(d):
    """缩时触发三勾选。全假非法（没有触发源=所有画面都算静止，缩时永远空）——
    回落默认值，由调用方记日志说明。"""
    t = {"motion": True, "person": True, "vehicle": True}
    if isinstance(d, dict):
        for k in t:
            if k in d:
                t[k] = _as_bool(d[k], t[k])
    if not any(t.values()):
        return dict(DETECT_DEFAULTS["trigger"]), False
    return t, True


def norm_detect(cfg):
    """整块强校：enum 白名单 + 数值夹取 + trigger 至少一项。返回 (规范值, 修正说明列表)。"""
    notes = []
    src = cfg if isinstance(cfg, dict) else {}
    out = {
        "enable": _as_bool(src.get("enable"), DETECT_DEFAULTS["enable"]),
        "classes": norm_classes(src.get("classes")),
        "conf": norm_conf(src.get("conf")),
        "live_fps": int(_clamp(src.get("live_fps"), 1, 10, DETECT_DEFAULTS["live_fps"])),
    }
    for key, allowed in DETECT_ENUMS.items():
        v = str(src.get(key) or "").strip().lower()
        if v not in allowed:
            v = DETECT_DEFAULTS[key]
            if src.get(key) not in (None, ""):
                notes.append("detect.%s 非法值已回落 %s" % (key, v))
        out[key] = v
    trig, ok = norm_trigger(src.get("trigger"))
    if not ok:
        notes.append("detect.trigger 全未勾选，已恢复默认（至少保留一项）")
    out["trigger"] = trig
    return out, notes


def norm_notify(cfg):
    src = cfg if isinstance(cfg, dict) else {}
    return {
        "webhook": str(src.get("webhook") or "").strip()[:500],
        "motion": _as_bool(src.get("motion"), NOTIFY_DEFAULTS["motion"]),
        "person": _as_bool(src.get("person"), NOTIFY_DEFAULTS["person"]),
        "vehicle": _as_bool(src.get("vehicle"), NOTIFY_DEFAULTS["vehicle"]),
        "cooldown": int(_clamp(src.get("cooldown"), 30, 600, NOTIFY_DEFAULTS["cooldown"])),
    }


def cam_detect_mode(cam, global_enable):
    """每路生效开关：全局关 → 一律 off（总闸语义）；否则 follow→全局类别。"""
    if not global_enable:
        return "off"
    m = str((cam or {}).get("detect") or "follow").strip().lower()
    return m if m in CAM_DETECT_MODES else "follow"


def cam_detect_on(cam, global_enable):
    return cam_detect_mode(cam, global_enable) != "off"


def norm_cam_classes(own, global_classes):
    """每路类别：detect_classes 是**完整三键**（UI 三开关初值拷全局），
    缺键一律 False——院子只勾「人员+车辆」时不能把没勾的两轮车补上。
    整个字段缺省/非 dict 才回落全局。"""
    if not isinstance(own, dict) or not own:
        return norm_classes(global_classes)
    return {k: _as_bool(own.get(k), False) for k in CATEGORIES}


def cam_classes(cam, global_classes):
    """每路生效类别：follow 用全局；显式配了（含全关）以每路为准。"""
    own = (cam or {}).get("detect_classes")
    if isinstance(own, dict) and own:
        return norm_cam_classes(own, global_classes)
    return norm_classes(global_classes)


# ---------------------------------------------------------------- 消费侧裁决

def trigger_ok(cls_counts, trigger):
    """一个台账窗口算不算活动。

    cls_counts: None=帧差窗口（无 AI 类别）；dict={person:n,...}=AI 窗口。
    命中任一勾选源即活动；全不勾的类别时段按静止画面剔除（不进缩时）。
    """
    t = trigger if isinstance(trigger, dict) else DETECT_DEFAULTS["trigger"]
    if cls_counts is None:
        return bool(t.get("motion"))
    if cls_counts.get("person", 0) > 0 and bool(t.get("person")):
        return True
    # vehicle 口径含 two_wheeler（两轮车合并进车辆，见 COCO_MAP 注释）
    if ((cls_counts.get("vehicle", 0) > 0 or cls_counts.get("two_wheeler", 0) > 0)
            and bool(t.get("vehicle"))):
        return True
    return False


def cls_hit_categories(cls_counts):
    """窗口命中的内部类别集合（回放 chips 用；vehicle 口径消费侧才合并）。"""
    if not isinstance(cls_counts, dict):
        return set()
    return {k for k in CATEGORIES if cls_counts.get(k, 0) > 0}


# ---------------------------------------------------------------- 忽略区过滤

def _cx_cy(box):
    x, y, w, h = box
    return x + w / 2.0, y + h / 2.0


def in_ignore(box, rects):
    """框中心落在任一忽略矩形内 → True（后端过滤，不改画面）。"""
    cx, cy = _cx_cy(box)
    for r in rects or []:
        try:
            x, y, w, h = (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
        except (TypeError, ValueError, IndexError):
            continue
        if w <= 0 or h <= 0:
            continue
        if x <= cx <= x + w and y <= cy <= y + h:
            return True
    return False


def filter_boxes(boxes, rects):
    return [b for b in (boxes or []) if not in_ignore(b, rects)]


# ---------------------------------------------------------------- PPM 解析

_RE_PPM = re.compile(rb"^P6\s+(\d+)\s+(\d+)\s+(\d+)\s")


def parse_ppm(data):
    """ffmpeg image2pipe ppm 输出 → (w, h, rgb bytes)。失败返回 None。

    只认第一帧（-frames:v 1 保证只有一帧，但管道里万一有多帧也只取第一帧，
    按头部声明的宽高计算载荷长度，不会把第二帧的字节吃进来）。
    """
    if not data or len(data) < 16 or not data.startswith(b"P6"):
        return None
    m = _RE_PPM.match(data)
    if not m:
        return None
    w, h, maxv = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if w <= 0 or h <= 0 or maxv != 255 or w > 8192 or h > 8192:
        return None
    body = data[m.end():m.end() + w * h * 3]
    if len(body) < w * h * 3:
        return None
    return w, h, body


# ---------------------------------------------------------------- 预处理/后处理

def letterbox(rgb, w, h, size=INPUT_SIZE):
    """等比缩放 + 灰边填充到 size×size，RGB 字节 → float32 NCHW /255。

    纯 numpy 双线性。 Surveillance 画面 16:9 为主，scale=1.0 时几乎无灰边。
    """
    import numpy as np
    a = np.frombuffer(rgb, dtype=np.uint8).reshape(h, w, 3).astype(np.float32)
    scale = min(size / float(w), size / float(h))
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    # 双线性采样坐标（像素中心对齐）
    xs = (np.arange(nw, dtype=np.float32) + 0.5) / scale - 0.5
    ys = (np.arange(nh, dtype=np.float32) + 0.5) / scale - 0.5
    xs = np.clip(xs, 0, w - 1)
    ys = np.clip(ys, 0, h - 1)
    x0 = np.floor(xs).astype(np.int32)
    y0 = np.floor(ys).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (xs - x0).reshape(1, nw, 1)
    fy = (ys - y0).reshape(nh, 1, 1)
    ia = a[np.ix_(y0, x0)]
    ib = a[np.ix_(y0, x1)]
    ic = a[np.ix_(y1, x0)]
    idd = a[np.ix_(y1, x1)]
    top = ia + (ib - ia) * fx
    bot = ic + (idd - ic) * fx
    out = top + (bot - top) * fy                       # (nh, nw, 3)
    canvas = np.full((size, size, 3), 114, dtype=np.float32)
    ox, oy = (size - nw) // 2, (size - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = out
    return (canvas / 255.0).transpose(2, 0, 1)[None]   # (1,3,H,W)


def nms(boxes, scores, iou_thr=NMS_IOU):
    """按分数降序的贪心 NMS。boxes 是 [cx,cy,w,h]。返回保留下标列表。"""
    import numpy as np
    if len(boxes) == 0:
        return []
    b = np.asarray(boxes, dtype=np.float32)
    s = np.asarray(scores, dtype=np.float32)
    x1, y1 = b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2
    x2, y2 = b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2
    area = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = s.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        union = area[i] + area[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0)
        order = rest[iou <= iou_thr]
    return keep


def postprocess(out, orig_w, orig_h, conf_map):
    """模型输出 (1,84,8400) → 检测列表 [{"c","s","b"}]，b=[cx,cy,w,h] 归一化 0~1。

    out: onnxruntime 返回的 ndarray（或可 np.asarray 的对象）。
    84 = 4 box + 80 类分数，通道优先；box 坐标在 640 输入空间。

    坐标回映射：letterbox 是「等比缩放 + 居中灰边」，直接除以 640 会把
    非 16:9 画面的框整体压扁、纵向偏移（忽略区过滤和遮罩预览都按归一化
    坐标比对方框中心）。这里按同一个 scale/偏移量反推回原图归一化坐标。
    """
    import numpy as np
    a = np.asarray(out)
    if a.ndim == 3:
        a = a[0]                       # (84, 8400)
    if a.shape[0] != 84 and a.shape[1] == 84:
        a = a.T                        # 防御：万一导出成 (8400,84)
    boxes = a[:4].T.astype(np.float32)          # (N,4) cx,cy,w,h
    scores = a[4:].T.astype(np.float32)         # (N,80)
    if boxes.shape[0] == 0:
        return []
    # 只保留关心的 COCO id，取每行最大分
    ids = sorted(COCO_MAP)
    sub = scores[:, ids]
    col = sub.argmax(axis=1)
    confs = sub[np.arange(len(sub)), col]
    best = confs.max() if len(confs) else 0.0
    if best <= min(conf_map.values()):
        return []
    # 分品类阈值：低于本类门槛的候选先剔掉
    thresh = np.array([conf_map[COCO_MAP[ids[c]]] for c in col], dtype=np.float32)
    alive = confs >= thresh
    if not alive.any():
        return []
    bx, cf, cl = boxes[alive], confs[alive], col[alive]
    keep = nms(bx.tolist(), cf.tolist())
    # letterbox 反推：640 输入空间 → 原图像素 → 归一化 0~1。
    # 这里必须和 letterbox() 用同一套算式（round + //2 居中），
    # 否则奇数尺寸会差半格，忽略区判定跟着漂。
    ow = float(orig_w) if orig_w else 1.0
    oh = float(orig_h) if orig_h else 1.0
    if ow <= 0 or oh <= 0:
        ow = oh = 1.0
    scale = min(INPUT_SIZE / ow, INPUT_SIZE / oh)
    nw = max(1, int(round(ow * scale)))
    nh = max(1, int(round(oh * scale)))
    ox = (INPUT_SIZE - nw) // 2
    oy = (INPUT_SIZE - nh) // 2
    dets = []
    for i in keep:
        cat = COCO_MAP[ids[int(cl[i])]]
        cx = (float(bx[i][0]) - ox) / scale / ow
        cy = (float(bx[i][1]) - oy) / scale / oh
        bw = float(bx[i][2]) / scale / ow
        bh = float(bx[i][3]) / scale / oh
        dets.append({"c": cat, "s": round(float(cf[i]), 3),
                     "b": [round(cx, 4), round(cy, 4), round(bw, 4), round(bh, 4)]})
    # 强的一致排序：类别 → 置信度
    dets.sort(key=lambda d: (d["c"], -d["s"]))
    return dets


# ---------------------------------------------------------------- 窗口构造

def windows_from_hits(hits, roll=1.0):
    """一次检出的多个框 → 台账窗口 [(s, e, {person:n,...})]。

    hits: [(t, category)]。同一时刻命中同类多次只计一次（一个人两个框是重复检）。
    窗口前后各留 roll 秒：YOLO 在 1fps 采样下可能漏 1 帧，留量保证事件连续。
    """
    by_t = {}
    for t, cat in hits or []:
        if cat not in CATEGORIES:
            continue
        by_t.setdefault(round(float(t), 2), set()).add(cat)
    out = []
    for t, cats in sorted(by_t.items()):
        counts = {k: 1 for k in CATEGORIES if k in cats}
        out.append((max(0.0, t - roll), t + roll, counts))
    return out


def merge_cls(a, b):
    """两个 cls 计数合并：同类取 max（重判不双算）。"""
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = max(int(out.get(k, 0)), int(v))
    return out


# ---------------------------------------------------------------- 运行时装载

def _py_tag():
    return "py%d%d" % (sys_version_info())


def sys_version_info():
    import sys
    return sys.version_info[0], sys.version_info[1]


def vendor_root(app_dir):
    return os.path.join(app_dir, "vendor")


def model_path(app_dir):
    return os.path.join(app_dir, "share", "models", MODEL_NAME)


def runtime_wheel_names():
    """本机 Python 需要的两个 wheel 文件名（诊断卡展示 + ensure_runtime 共用）。

    随包带了 cp311/312/313 三套共 ~99MB；确认 NAS 实际只跑其中一个版本后，
    可以只留那一套把包体砍到 ~1/3。诊断卡显示的就是这里的结论。
    """
    v = sys_version_info()
    np_name = ("numpy-2.1.3-cp%d%d.whl" if v >= (3, 13)
               else "numpy-1.26.4-cp%d%d.whl") % v
    return {"ort": "onnxruntime-1.20.1-cp%d%d.whl" % v, "numpy": np_name,
            "python": "%d.%d" % v}


def ensure_runtime(app_dir, var_dir, log=None):
    """按本机 Python 版本解包随车 wheel 到 var_dir/ort/py3XX/ 并加进 sys.path。

    返回 (ok, reason)。任何一步失败都不抛——调用方如实展示。
    """
    import sys
    tag = _py_tag()
    dest = os.path.join(var_dir, "ort", tag)
    marker = os.path.join(dest, ".ok")
    wheels = []
    try:
        if not os.path.isfile(marker):
            os.makedirs(dest, exist_ok=True)
            # 选 wheel：onnxruntime 固定 1.20.1；numpy 3.13 用 2.x，其余 1.26.4
            need = runtime_wheel_names()
            for name in (need["ort"], need["numpy"]):
                src = os.path.join(vendor_root(app_dir), name)
                if not os.path.isfile(src):
                    return False, "随包运行时缺少 %s（Python %d.%d）" % (
                        name, sys_version_info()[0], sys_version_info()[1])
                wheels.append(src)
            for src in wheels:
                with zipfile.ZipFile(src) as z:
                    z.extractall(dest)
            with open(marker, "w") as f:
                f.write("ok")
        if dest not in sys.path:
            sys.path.insert(0, dest)
        import numpy          # noqa: F401
        import onnxruntime    # noqa: F401
        return True, ""
    except Exception as e:                       # zip 损坏/glibc 不符/权限
        try:
            if os.path.isdir(dest):
                for root, dirs, files in os.walk(dest, topdown=False):
                    for fn in files:
                        os.remove(os.path.join(root, fn))
                    for d in dirs:
                        os.rmdir(os.path.join(root, d))
                os.rmdir(dest)
        except OSError:
            pass
        return False, "运行时装载失败：%s" % str(e)[:160]


class Engine:
    """单例推理引擎。懒加载：第一次 ensure() 才解 wheel、load 模型。"""

    def __init__(self, app_dir, var_dir, log=None):
        self.app_dir = app_dir
        self.var_dir = var_dir
        self.log = log or (lambda m: None)
        self._sess = None
        self._in_name = None
        self._out_name = None
        self._lock = threading.Lock()
        self.error = ""
        self.avg_ms = 0.0
        self._infer_n = 0

    @property
    def loaded(self):
        return self._sess is not None

    def ensure(self):
        """(ok, reason)。重复调用安全。"""
        if self._sess is not None:
            return True, ""
        with self._lock:
            if self._sess is not None:
                return True, ""
            mp = model_path(self.app_dir)
            if not os.path.isfile(mp):
                self.error = "模型文件缺失：%s" % mp
                return False, self.error
            ok, why = ensure_runtime(self.app_dir, self.var_dir, self.log)
            if not ok:
                self.error = why
                return False, why
            try:
                import onnxruntime as ort
                opt = ort.SessionOptions()
                opt.intra_op_num_threads = 2
                opt.log_severity_level = 3
                so = ort.InferenceSession(mp, opt,
                                          providers=["CPUExecutionProvider"])
                self._sess = so
                self._in_name = so.get_inputs()[0].name
                self._out_name = so.get_outputs()[0].name
                self.error = ""
                return True, ""
            except Exception as e:
                self.error = "模型加载失败：%s" % str(e)[:160]
                return False, self.error

    def infer(self, rgb, w, h, conf_map):
        """一帧 → 检测列表。引擎不可用返回 None（调用方记状态，不重试轰炸）。"""
        ok, _ = self.ensure()
        if not ok:
            return None
        try:
            t0 = time.time()
            x = letterbox(rgb, w, h)
            out = self._sess.run([self._out_name],
                                 {self._in_name: x})[0]
            dets = postprocess(out, w, h, conf_map)
            dt = (time.time() - t0) * 1000.0
            self._infer_n += 1
            self.avg_ms = round(self.avg_ms * 0.9 + dt * 0.1, 1)
            return dets
        except Exception as e:
            self.error = "推理失败：%s" % str(e)[:160]
            self.log("AI 推理失败：%s" % self.error)
            return None

    def close(self):
        with self._lock:
            self._sess = None


# ---------------------------------------------------------------- 抽帧命令

def grab_ppm_cmd(ffmpeg, url, use_tcp=True):
    c = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if use_tcp:
        c += ["-rtsp_transport", "tcp"]
    c += ["-i", url, "-frames:v", "1", "-f", "image2pipe",
          "-vcodec", "ppm", "-"]
    return c


def grab_ppm(ffmpeg, url, timeout=10, use_tcp=True):
    """抽一帧 PPM。返回 (ok, ppm_bytes or "", err)。子码流优先，与快照同源。"""
    if not ffmpeg:
        return False, "", "ffmpeg 未找到"
    try:
        proc = subprocess.run(grab_ppm_cmd(ffmpeg, url, use_tcp),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "", "抽帧超时（%d 秒）" % timeout
    except OSError as e:
        return False, "", "ffmpeg 无法启动：%s" % str(e)[:120]
    if proc.returncode != 0 or not proc.stdout:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return False, "", (tail[-1].strip()[:200] if tail else "rc=%d" % proc.returncode)
    return True, proc.stdout, ""


# ---------------------------------------------------------------- 调度 Worker

class DetectWorker:
    """快照节奏的检测调度：每 interval 秒对每路抽 1 帧 → 推理 → 台账窗口。

    与 SnapshotWorker 平行而独立：这一路全挂也不影响预览出图。总闸关闭时
    整个 worker 不启动（schedule() 才被 nvr_slim 调用）。
    """

    def __init__(self, engine, ffmpeg, var_dir, record_root_fn, cams_fn,
                 cfg_fn, interval=5, timeout=10, log=None, notifier=None):
        self.engine = engine
        self.ffmpeg = ffmpeg
        self.var_dir = var_dir
        self.record_root_fn = record_root_fn
        self.cams_fn = cams_fn
        self.cfg_fn = cfg_fn            # 返回 (detect_cfg, cameras) 快照
        self.interval = max(1, min(60, int(interval or 5)))
        self.timeout = int(timeout)
        self.log = log or (lambda m: None)
        self.notifier = notifier        # Notifier.offer(cam_name, cat)——None=不通知
        self.stop_event = threading.Event()
        self.thread = None
        # 统计：cam_id -> {"total":n, "today":{date:n}, "cls":{date:{person:n,...}}}
        self.stats = {}
        self._stats_lock = threading.Lock()
        self._day = time.strftime("%Y-%m-%d")

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name="detect")
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    # ---------------------------------------------------------- 主循环
    def _loop(self):
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                self._round()
            except Exception as e:
                self.log("检测轮异常：%r" % e)
            self.stop_event.wait(max(0.5, self.interval - (time.time() - t0)))

    def _round(self):
        cfg, cams = self.cfg_fn() or ({}, [])
        if not cfg.get("enable"):
            return
        ok, why = self.engine.ensure()
        if not ok:
            # 诚实但安静：状态行已经显示原因，这里每分钟最多一条
            key = "engine|%s" % why[:40]
            if self.stats.get("_engine_err") != key:
                self.stats["_engine_err"] = key
                self.log("AI 检测不可用：%s" % why)
            return
        conf_map = cfg.get("conf") or conf_from_sensitivity("std")
        now = time.time()
        for c in cams:
            if self.stop_event.is_set():
                return
            if not isinstance(c, dict) or c.get("enabled") is False:
                continue
            if not cam_detect_on(c, cfg.get("enable")):
                continue
            classes = cam_classes(c, cfg.get("classes"))
            if not any(classes.values()):
                continue                      # 全关=不检测（设置页已提示）
            url = str(c.get("sub_url") or c.get("url") or "").strip()
            if not url:
                continue
            self._one(c, url, conf_map, classes, now)

    def _one(self, cam, url, conf_map, classes, now):
        ok, data, err = grab_ppm(self.ffmpeg, url, timeout=self.timeout)
        if not ok:
            self._fail(cam["id"], err)
            return
        ppm = parse_ppm(data)
        if not ppm:
            self._fail(cam["id"], "PPM 解析失败")
            return
        w, h, rgb = ppm
        dets = self.engine.infer(rgb, w, h, conf_map)
        if dets is None:
            self._fail(cam["id"], self.engine.error or "推理失败")
            return
        # 类别过滤（算力口径）+ 忽略区过滤（框中心落忽略格即丢）。
        # 忽略格是 16×9 归一化矩形，与归一化框同一坐标系，直接比。
        rects = self._ignore_rects(cam)
        dets = [d for d in dets if classes.get(d["c"])
                and not in_ignore(d["b"], rects)]
        if dets:
            self._write_ledger(cam, dets, now)
            self._notify(cam, dets)
        self._ok_stat(cam["id"], dets)

    def _notify(self, cam, dets):
        """检出 → 通知入队（person/vehicle 口径；two_wheeler 归 vehicle）。
        offer 内部查 cooldown 与开关，绝不在推理线程里发请求。"""
        if not self.notifier:
            return
        cats = {d["c"] for d in dets}
        name = cam.get("name") or cam["id"]
        for cat in ("person", "vehicle"):
            hit = cat in cats or (cat == "vehicle" and "two_wheeler" in cats)
            if hit:
                try:
                    self.notifier(name, cat)
                except Exception:
                    pass

    # ---------------------------------------------------------- 台账
    def _write_ledger(self, cam, dets, now):
        """检出 → 更新该路当前 5 分钟桶（src=ai:<分片文件名>），原子整块替换。"""
        import ledger
        try:
            root = self.record_root_fn()
        except Exception:
            return
        dname = cam.get("dir_name") or cam["id"]
        lt = time.localtime(now)
        date = time.strftime("%Y-%m-%d", lt)
        start_sec = (lt.tm_hour * 60 + lt.tm_min) // 5 * 300
        # 桶名与 start_sec 严格对齐（MM 是 5 的倍数）。以前用 "%H-%M-00" 直接
        # 取当前分钟，命名与 start 错位、每分钟还多出一个源。
        bucket = "%02d-%02d-00" % (start_sec // 3600, (start_sec % 3600) // 60)
        src = "ai:%s.mp4" % bucket
        tod = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        wins = windows_from_hits([(float(tod), d["c"]) for d in dets])
        # 与桶内已有窗口合并（同 src 读-改-写，保持幂等）。
        # 坐标系：文件里存的 windows 与 wins 都是「当天钟点秒」，_merge_windows
        # 在这个坐标系里合并；而 ledger.merge 的入参契约是「分片内相对秒」
        # （它内部会再加一次 start_sec 换算回钟点秒）。
        # 0.9.66 以前直接把绝对秒传进 merge，被双重偏移后全部 >86400 遭
        # min(DAY_SEC) 夹掉、e<=s 丢弃——AI 每 5 秒勤勤恳恳写进去的全是
        # windows:[] 的空源，回放页一个标记都没有（真机实测：stats 照常涨、
        # 台账零 AI 事件）。这里统一减掉 start_sec 再交给 merge。
        cur = ledger.load(root, dname, date) or {}
        old = []
        srcs = cur.get("sources") if isinstance(cur, dict) else None
        if isinstance(srcs, dict) and isinstance(srcs.get(src), dict):
            for wv in (srcs[src].get("windows") or []):
                try:
                    old.append([float(wv[0]), float(wv[1]),
                                dict(wv[2]) if len(wv) > 2 and
                                isinstance(wv[2], dict) else {}])
                except (TypeError, ValueError, IndexError):
                    continue
        merged = self._merge_windows(old + [[s, e, c] for s, e, c in wins])
        ledger.merge(root, dname, date, src, start_sec,
                     [[s - start_sec, e - start_sec, c] for s, e, c in merged],
                     density=1.0, engine="ai")   # AI 检出=高置信活动

    @staticmethod
    def _merge_windows(wins):
        """窗口并集（gap ≤ AI_GAP_SEC 合并），cls 计数取 max。

        0.9.69：5 秒 → 30 秒。抽帧每 5 秒一次、窗口 ±1 秒，正常间隙 3 秒看着够用，
        但只要一次抓帧超时（10 秒）就漏出 10~20 秒的空隙，一辆车停在那里就被记成
        几十条 2 秒事件。30 秒与 ledger.AI_GAP_SEC 同一口径，写读两侧不再打架。
        """
        gap = getattr(_ledger, "AI_GAP_SEC", 30.0)
        ws = sorted([[float(s), float(e), c or {}] for s, e, c in wins
                     if float(e) > float(s)], key=lambda x: x[0])
        out = []
        for s, e, c in ws:
            if out and s - out[-1][1] <= gap:
                out[-1][1] = max(out[-1][1], e)
                out[-1][2] = merge_cls(out[-1][2], c)
            else:
                out.append([s, e, dict(c)])
        return out

    # ---------------------------------------------------------- 忽略区
    @staticmethod
    def _ignore_rects(cam):
        """cam.masks.ignore（16×9 格号）→ 像素矩形。格号编码 row*16+col。"""
        rects = []
        m = (cam or {}).get("masks")
        if not isinstance(m, dict):
            return rects
        cells = [int(x) for x in (m.get("ignore") or [])
                 if isinstance(x, int) or str(x).isdigit()]
        # 没有画面尺寸时用 16:9 相对坐标：rects 以「比例」传入由调用方乘宽高。
        # 这里直接返回归一化矩形（0~1），调用方按比例缩放。
        for cell in cells:
            if cell < 0 or cell >= 144:
                continue
            row, col = divmod(cell, 16)
            rects.append([col / 16.0, row / 9.0, 1.0 / 16.0, 1.0 / 9.0])
        return rects

    # ---------------------------------------------------------- 统计
    def _ok_stat(self, cam_id, dets):
        with self._stats_lock:
            st = self.stats.setdefault(cam_id, {"total": 0, "today": {},
                                                "cls": {}, "last": None})
            st["total"] += 1
            d = self._day
            st["today"][d] = st["today"].get(d, 0) + 1
            if dets:
                cd = st["cls"].setdefault(d, {})
                for cat in {x["c"] for x in dets}:
                    cd[cat] = cd.get(cat, 0) + 1
                st["last"] = {"t": time.time(),
                              "cats": sorted({x["c"] for x in dets})}
            self.stats.pop("_engine_err", None)

    def _fail(self, cam_id, err):
        with self._stats_lock:
            st = self.stats.setdefault(cam_id, {"total": 0, "today": {},
                                                "cls": {}, "last": None})
            st["last_err"] = str(err)[:120]
            st["last_err_at"] = time.time()

    def status(self):
        """给 /api/status：加载态 + 错误 + 平均耗时 + 每路统计。"""
        with self._stats_lock:
            per_cam = {}
            for cid, st in self.stats.items():
                if cid.startswith("_"):
                    continue
                per_cam[cid] = {
                    "total": st.get("total", 0),
                    "today": st.get("today", {}).get(self._day, 0),
                    "cls": st.get("cls", {}).get(self._day, {}),
                    "last": st.get("last"),
                    "last_err": st.get("last_err"),
                }
            return {
                "loaded": self.engine.loaded,
                "error": self.engine.error,
                "avg_ms": self.engine.avg_ms,
                "alive": self.alive(),
                "per_cam": per_cam,
            }


if __name__ == "__main__":
    # 命令行自检：纯逻辑部分（不加载模型）
    print(json.dumps({
        "trigger_ok": [trigger_ok(None, {"motion": True, "person": False, "vehicle": False}),
                       trigger_ok({"person": 1}, {"motion": False, "person": False, "vehicle": False}),
                       trigger_ok({"two_wheeler": 1}, {"motion": False, "person": False, "vehicle": True})],
        "defaults": DETECT_DEFAULTS,
    }, ensure_ascii=False, indent=1))
