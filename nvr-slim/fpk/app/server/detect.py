#!/usr/bin/env python3
"""AI 检测引擎：人员 / 两轮车 / 车辆（YOLO11n ONNX + onnxruntime；推理后端见「推理设备」一节）。

在整套系统里的位置（见《人员车辆检测-功能选项设计.md》§3）：
  快照节奏的抽帧 → 本模块推理 → 类别+框 → 忽略区过滤 → 台账窗口带 cls 计数
  → 消费侧（回放页 chips / 缩时触发闸 / 通知）按 detect.trigger 裁决。

六条设计底线：
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
5. **有界并发**（0.9.122）：每路独立节拍 + 有界工作者池（默认 2、封顶 4），
   队列满了丢、过期帧丢，绝不 wait、绝不一路一线程。检测是二等公民，
   不许和取帧、录像抢 CPU——评审红线是「漏报不能影响全时录像」，
   而"影响"最现实的形态就是检测把 CPU 吃满、编码器掉帧。
6. **三态**（0.9.122）：hit / miss / unknown。unknown（取帧失败、模型未加载、
   推理异常、断流、排队过期）不得计入「无活动」；失败一律带码，
   调用方不靠匹配中文串判断。

模型与运行时分发：
  app/share/models/yolo11n.onnx          ~10.7MB（COCO 80 类，opset 19）
  app/vendor/onnxruntime-1.20.1-cp31x.whl 随包（manylinux_2_28，glibc≥2.28）
  app/vendor/numpy-*.whl                  随包
  首次使用时按 sys.version_info 选对应 wheel，解压到 var_dir/ort/py3XX/ 再 import
  ——绕开「fnOS 只带标准库」与 wheel ABI 错配两个坑（解压有 .ok 标记，原子）。
"""
import json
import os
import queue
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

try:
    # 时间窗确认（0.9.122）。同目录模块，跟着 app/server 一起打包；
    # 单独跑本文件时可能不在 sys.path 上，缺了只是退回旧口径（每帧一条），不崩。
    import confirm as _confirm
except ImportError:
    _confirm = None

try:
    # 通知平台白名单（通知模块系列）。理由同上：缺了只影响 notify 的枚举强校，
    # 检测本体不该因为一个模板模块不可用就起不来。
    import notify_tpl as _notify_tpl
except ImportError:
    _notify_tpl = None

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
    # 模板与平台（0.9.124 批次 6）：auto 表示按 webhook 的 host 自动识别，
    # 其余值强制按该平台出 body。空模板 = 默认文案。
    "platform": "auto",
    "template": "",
}

DETECT_ENUMS = {
    "mode": ("snap", "live"),
    "device": ("auto", "cpu", "gpu"),
    "night": ("full", "throttle", "off"),
}
# 通知块的枚举单独一份：它属于 notify，不属于 detect，混进 DETECT_ENUMS
# 会让 norm_detect 顺手把 notify 的键也按 detect 的口径校验。
NOTIFY_ENUMS = {
    "platform": (_notify_tpl.PLATFORMS if _notify_tpl else
                 ("auto", "feishu", "dingtalk", "wecom", "serverchan",
                  "bark", "generic")),
}
CAM_DETECT_MODES = ("follow", "on", "off")

MODEL_NAME = "yolo11n.onnx"
INPUT_SIZE = 640
NMS_IOU = 0.45

# ---------------------------------------------------------------- 调度与状态口径
# 0.9.122 之前是「一个循环顺序跑完所有路」：8 路一轮 = 8 × 单路耗时，
# 一旦超过 5 秒节拍就永远在补旧帧，检测频率随路数稀释——加摄像头越多越"迟钝"。
# 现在改成「每路独立节拍 + 有界工作者池」，三个常量就是这条改动的全部旋钮。

SCHED_DEFAULT_WORKERS = 2        # 默认并发：单路推理已用 2 个 intra_op 线程，2 并发正好
SCHED_MAX_WORKERS = 4            # 封顶：再多只会让单帧耗时翻倍、总吞吐反降
SCHED_TICK_SEC = 0.5             # 到点判定精度（节拍是 5 秒，0.5 足够且几乎不耗 CPU）

# 事件快照单张上限（0.9.129）：取帧层出的是 q:v4 / 720p（真机 45~180KB）。
# 超过 2MB 只可能是读到了别的文件或写坏了，存进去只是白占盘。
SHOT_MAX_BYTES = 2 * 1024 * 1024

# 三态。hit=有检出；miss=推理成功但没检出（**只有**这个才叫"无活动"）；
# unknown=这一拍没有可信结论。三态必须分开：把 unknown 记成 miss，
# 等于在摄像头掉线时告诉用户"没人来过"——监控里这种"看起来对"的数据
# 比一条报错危险得多。
HIT, MISS, UNKNOWN = "hit", "miss", "unknown"

# 失败码（/api/status 的 detect.error_code）。所有失败路径都给其中之一，
# 前端按码取文案；中文错误串只用于给人看，不参与判断。
E_FFMPEG = "FFMPEG_NOT_FOUND"
E_MODEL = "MODEL_MISSING"
E_RUNTIME = "RUNTIME_FAIL"
E_MODEL_LOAD = "MODEL_LOAD_FAIL"
E_FRAME = "FRAME_FAIL"
E_INFER = "INFER_ERR"
E_WORKER = "WORKER_DOWN"
E_QUEUE = "QUEUE_FULL"
E_STALE = "STALE_FRAME"
E_STATUS = "STATUS_ERR"


def frame_error_code(err):
    """取帧失败 → 失败码。ffmpeg 缺失/起不来是环境问题，与"这一帧坏了"要分开：
    前者整机都检测不了，后者只影响这一路这一拍。"""
    s = str(err or "")
    if "ffmpeg 未找到" in s or "ffmpeg 无法启动" in s:
        return E_FFMPEG
    return E_FRAME


# ---------------------------------------------------------------- 推理设备（0.9.125）
# device 这个键从很早就躺在 DETECT_DEFAULTS / DETECT_ENUMS 里，但 Engine 一直
# 无条件 providers=["CPUExecutionProvider"]，诊断卡标题还硬写着「YOLO11n · CPU」。
# 用户把它改成 gpu —— 接口回 ok、配置也落盘，实际仍在 CPU 上跑，界面上更没有
# 一个字能看出差别。这不是"少做一个功能"，是界面在说一件没验证过的事。
#
# 这一层的三条口径：
# 1. **先问后选**：只认 onnxruntime 自己报的可用后端，不按机器名猜、不看
#    nvidia-smi。猜错的代价是"配了 GPU 却一直静默回落"，比明说 CPU 更糟。
# 2. **回落不算故障**：GPU 建会话失败（驱动 / CUDA 版本 / 显存）时立刻用 CPU
#    再建一次。检测能跑比跑得快重要，所以不把 state 打成 unavailable；但原因
#    必须写进状态接口，界面要能说出"你要 GPU，实际在 CPU，因为……"。
# 3. **显式选 cpu 不亮黄**：那是用户要的结果，不是降级。
# 只认这几个**明确是 GPU/NPU 算力**的后端。故意不含 OpenVINO：它自己也能跑在
# CPU 上，选它等于没法回答"到底用没用上显卡"，而这一层存在的意义就是回答这句话。
# DirectML 是 Windows 专用，本机是 Linux NAS，留着只会让"支持的设备"看着更宽。
GPU_PROVIDERS = ("CUDAExecutionProvider", "TensorrtExecutionProvider",
                 "ROCMExecutionProvider")
DEVICE_KINDS = {
    "CUDAExecutionProvider": "cuda",
    "TensorrtExecutionProvider": "tensorrt",
    "ROCMExecutionProvider": "rocm",
    "CPUExecutionProvider": "cpu",
}


def device_blank():
    """设备块的空壳（没 worker / 还没加载模型时）。actual 留空 = 未知。"""
    return {"requested": "", "actual": "", "providers": [],
            "available": [], "fallback_reason": ""}


def engine_device(eng):
    """从引擎取设备块。取不到就回空壳，绝不因为"读不到设备"让 /api/status 500。

    引擎可能是只实现 loaded/infer 的替身（单测里的 FakeEngine），也可能是
    升级过程中的旧对象——与 `getattr(eng, "error_code", "")` 同一个理由：
    状态汇总这条路上，少一个字段好过整块报错。
    """
    fn = getattr(eng, "device_status", None)
    if not callable(fn):
        return device_blank()
    try:
        got = fn()
    except Exception:
        return device_blank()
    return got if isinstance(got, dict) else device_blank()


def probe_providers(ort):
    """(可用后端列表, 说明)。读不出来就只认 CPU —— 不猜、不假装。"""
    try:
        got = [str(p) for p in (getattr(ort, "get_available_providers")() or [])]
    except Exception as e:                 # 旧版 ort / 假模块 / 属性被裁掉
        return (["CPUExecutionProvider"],
                "读取 onnxruntime 可用后端失败：%s" % str(e)[:80])
    if "CPUExecutionProvider" not in got:
        got.append("CPUExecutionProvider")  # CPU 后端永远在，缺了也补上
    return got, ""


def pick_providers(device, available):
    """按用户意图挑后端。返回 (providers, actual, fallback_reason)。

    providers 里 CPU 永远排在最后：同一会话内某个算子 GPU 不支持时，ORT 会
    自己落到 CPU 上算，而不是整帧失败。
    """
    dev = str(device or "auto").strip().lower()
    if dev not in ("auto", "cpu", "gpu"):
        dev = "auto"                       # 与 norm_detect 同口径：写错按自动走
    avail = [p for p in (available or []) if p]
    if "CPUExecutionProvider" not in avail:
        avail.append("CPUExecutionProvider")
    if dev == "cpu":
        return ["CPUExecutionProvider"], "cpu", ""
    gpu = [p for p in GPU_PROVIDERS if p in avail]
    if gpu:
        return gpu + ["CPUExecutionProvider"], DEVICE_KINDS.get(gpu[0], "gpu"), ""
    if dev == "gpu":
        # 用户点名要 GPU 却挑不出来：这是**能看见的降级**，不是错误。
        # 文案同时说清"缺什么"和"现在有什么"——只说后者，用户还得自己去查
        # CUDA 该装哪个包。
        return (["CPUExecutionProvider"], "cpu",
                "请求 GPU，但 onnxruntime 没有 %s 后端（当前可用：%s）"
                % (" / ".join(p.replace("ExecutionProvider", "")
                              for p in GPU_PROVIDERS),
                   ", ".join(sorted(avail))))
    return ["CPUExecutionProvider"], "cpu", ""


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
    # 时间窗确认（0.9.122）：整块交给 confirm.norm 强校。它必须进 norm_detect
    # 的输出，否则每次保存设置都会把它丢掉，参数永远停在默认值上。
    if _confirm is not None:
        cv, cnotes = _confirm.norm(src.get("confirm"))
        out["confirm"] = cv
        notes.extend(cnotes)
    return out, notes


def norm_notify(cfg):
    src = cfg if isinstance(cfg, dict) else {}
    plat = str(src.get("platform") or NOTIFY_DEFAULTS["platform"]).strip().lower()
    if plat not in NOTIFY_ENUMS["platform"]:
        plat = NOTIFY_DEFAULTS["platform"]      # 写错一个词不该让整条通知发不出去
    # 模板原样留着：非法占位符由 notify_tpl 在渲染时回退，并把原因报到状态接口，
    # 界面才能告诉用户「你写的这句没生效，因为 {xxx} 不认识」。在这里清空
    # 反而会让用户看不到自己写的东西，也查不出为什么没生效。
    return {
        "webhook": str(src.get("webhook") or "").strip()[:500],
        "motion": _as_bool(src.get("motion"), NOTIFY_DEFAULTS["motion"]),
        "person": _as_bool(src.get("person"), NOTIFY_DEFAULTS["person"]),
        "vehicle": _as_bool(src.get("vehicle"), NOTIFY_DEFAULTS["vehicle"]),
        "cooldown": int(_clamp(src.get("cooldown"), 30, 600, NOTIFY_DEFAULTS["cooldown"])),
        "platform": plat,
        "template": str(src.get("template") or "").strip()[:500],
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


# ---------------------------------------------------------------- 画面区域过滤
#
# 两块区域共用一张 16×9 格子（格号 = row*16 + col，行在上），语义却相反：
#   ignore  忽略区   —— 框中心落**进去**就丢。解决"这块老是误报"。
#   include 只统计区 —— 框中心落**不进去**就丢。解决"只关心大门/车道"。
# include 留空 = 不限制，与 0.9.127 的行为逐字相同（升级不改任何既有判定）。
#
# 为什么放在**框中心**而不是重叠面积：面积口径下一个人半个身子在区外就会被
# 判成"区外"，画一条窄带能覆盖走廊的时候会很别扭；中心点口径与忽略区一致，
# 用户学一次就懂，台账里也不会出现"同一目标有时算有时不算"。

GRID_COLS = 16
GRID_CELLS = 144          # 16×9


def cells_to_rects(cells):
    """格号列表 → 归一化矩形 [x, y, w, h]（0~1）。越界/非数字格号跳过。"""
    rects = []
    for cell in cells or []:
        try:
            c = int(cell)
        except (TypeError, ValueError):
            continue
        if c < 0 or c >= GRID_CELLS:
            continue
        row, col = divmod(c, GRID_COLS)
        rects.append([col / float(GRID_COLS), row / 9.0,
                      1.0 / GRID_COLS, 1.0 / 9.0])
    return rects


def _cx_cy(box):
    x, y, w, h = box
    return x + w / 2.0, y + h / 2.0


def _center_in(box, rects):
    try:
        cx, cy = _cx_cy(box)
    except (TypeError, ValueError, IndexError):
        # 形状不对的框（手改配置 / 上游脏数据）：判"不在区内"。
        # 不抛异常——一路的坏数据不该带崩整轮检测，那会连累其余摄像头。
        return False
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


def in_ignore(box, rects):
    """框中心落在任一忽略矩形内 → True（后端过滤，不改画面）。"""
    return _center_in(box, rects)


def in_include(box, rects):
    """只统计区：rects 为空 = 不限制；非空时框中心必须落进至少一格。"""
    if not rects:
        return True
    return _center_in(box, rects)


def filter_boxes(boxes, rects):
    return [b for b in (boxes or []) if not in_ignore(b, rects)]


def apply_regions(dets, classes, ignore_rects=(), include_rects=()):
    """类别过滤 → 忽略区 → 只统计区，返回 (留下的框, 被只统计区挡掉的个数)。

    顺序是刻意的：**忽略区优先**。两块重叠时按忽略处理 —— 用户既然画了
    "这块别管"，就不该因为另一块"只看这块"又把它放回来。否则用户改一次
    只统计区，就得回头检查忽略区有没有被悄悄推翻。

    返回被挡掉的个数，是为了让"区域画得太窄、什么都进不来"看得见：这个数
    进诊断页，用户能自己发现是区域画错了，而不是以为 AI 不干活了。这是
    评审那条"失败不等于没有"的同一个道理——挡掉的目标数也是事实，得记账。
    """
    kept, outside = [], 0
    for d in dets or []:
        if not isinstance(d, dict):
            continue
        c = d.get("c")
        if not classes.get(c):
            continue
        b = d.get("b")
        if in_ignore(b, ignore_rects):
            continue
        if not in_include(b, include_rects):
            outside += 1
            continue
        kept.append(d)
    return kept, outside


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
    """单例推理引擎。懒加载：第一次 ensure() 才解 wheel、load 模型。

    device（0.9.125）在开会话时读一次，之后不再变：改配置由 nvr_slim 重建
    worker 生效（重建只换检测这一条线程，不碰取帧与录像）。
    """

    def __init__(self, app_dir, var_dir, log=None, device="auto", ort_loader=None):
        self.app_dir = app_dir
        self.var_dir = var_dir
        self.log = log or (lambda m: None)
        self._sess = None
        self._in_name = None
        self._out_name = None
        self._lock = threading.Lock()
        self.error = ""
        # 失败码（0.9.122）：error 是给人看的话，error_code 是给程序用的键。
        # 以前只有中文串，状态行要判断"到底缺模型还是缺运行时"只能做子串匹配，
        # 改一句文案就静默失效。
        self.error_code = ""
        self.avg_ms = 0.0
        self._infer_n = 0
        # 推理设备（0.9.125）。四个字段都是"事实"，不是配置的复印：
        # requested 是要什么，actual 是这次会话真的建在什么后端上。
        self.device_requested = str(device or "auto").strip().lower()
        self.device_actual = ""
        self.providers = []
        self.available = []
        self.device_fallback = ""
        # 单测用的注入点：没装 wheel 的机器上也要能验「GPU 建不起来会回落」。
        self._ort_loader = ort_loader

    @property
    def loaded(self):
        return self._sess is not None

    def device_status(self):
        """给状态接口的设备块。还没加载时 actual/providers 留空 —— 未知就是未知，
        不拿配置值冒充实际后端。"""
        return {
            "requested": self.device_requested or "auto",
            "actual": self.device_actual,
            "providers": list(self.providers),
            "available": list(self.available),
            "fallback_reason": self.device_fallback,
        }

    def _ort(self):
        if self._ort_loader is not None:
            return self._ort_loader()
        import onnxruntime as ort
        return ort

    def _session(self, ort, mp, providers):
        opt = ort.SessionOptions()
        opt.intra_op_num_threads = 2
        opt.log_severity_level = 3
        return ort.InferenceSession(mp, opt, providers=list(providers))

    def _open(self, mp):
        """建会话：先按 device 挑后端，GPU 建不起来就用 CPU 再建一次。"""
        ort = self._ort()
        avail, why = probe_providers(ort)
        self.available = list(avail)
        providers, actual, reason = pick_providers(self.device_requested, avail)
        self.device_fallback = reason or why
        wanted = providers[0]
        try:
            sess = self._session(ort, mp, providers)
        except Exception as e:
            if actual == "cpu":
                raise                       # 本来就是 CPU，没有可回落的东西
            first = str(e)[:120]
            # 驱动缺失、CUDA 版本与 wheel 不匹配、显存不足都会走到这里。
            # 回落一次 CPU，成功就带着原因继续跑 —— 检测有结果比跑得快重要。
            self.log("推理后端 %s 建会话失败，回落 CPU：%s" % (wanted, first))
            sess = self._session(ort, mp, ["CPUExecutionProvider"])
            providers = ["CPUExecutionProvider"]
            actual = "cpu"
            self.device_fallback = "%s 建会话失败：%s" % (wanted, first)
        self.providers = list(providers)
        self.device_actual = actual
        if self.device_fallback:
            self.log("推理设备：请求 %s，实际 %s（%s）"
                     % (self.device_requested, actual, self.device_fallback))
        return sess

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
                self.error_code = E_MODEL
                return False, self.error
            ok, why = ensure_runtime(self.app_dir, self.var_dir, self.log)
            if not ok:
                self.error = why
                self.error_code = E_RUNTIME
                return False, why
            try:
                so = self._open(mp)
                self._sess = so
                self._in_name = so.get_inputs()[0].name
                self._out_name = so.get_outputs()[0].name
                self.error = ""
                self.error_code = ""
                return True, ""
            except Exception as e:
                self.error = "模型加载失败：%s" % str(e)[:160]
                self.error_code = E_MODEL_LOAD
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
            self.error_code = ""
            return dets
        except Exception as e:
            self.error = "推理失败：%s" % str(e)[:160]
            self.error_code = E_INFER
            self.log("AI 推理失败：%s" % self.error)
            return None

    def close(self):
        with self._lock:
            self._sess = None


# ---------------------------------------------------------------- 抽帧命令

def grab_ppm_cmd(ffmpeg, url, use_tcp=True):
    c = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    # -rtsp_transport 只对 RTSP 输入有意义；喂 tcp:// 或本地文件时会变成
    # "Option not found" 直接退出（0.9.80 起检测也可能吃本机取流端口）。
    if use_tcp and str(url).lower().startswith("rtsp"):
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


# 取帧层健康、只是没有新图。**不是错误**：检测节拍（5 秒）比取帧节拍
# （snapshot_interval，默认 10 秒）密，每两轮必有一轮拿到同一张图。
# 这种情况既不能记账（会重复计数），也不能记失败（会把诊断页刷红）。
NO_NEW_FRAME = "[no-new-frame]"


def grab_ppm_image(ffmpeg, path, timeout=8):
    """把取帧层写好的 JPEG 解成 PPM。**不碰网络**，所以没有建连成本。

    0.9.80 统一取流之后，预览 / 移动侦测 / AI 共用同一张图：摄像头只看到
    取流层那一条连接。本地解一张 200~600KB 的 jpg 只要几十毫秒。
    """
    if not ffmpeg:
        return False, "", "ffmpeg 未找到"
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
             "-i", path, "-frames:v", "1", "-f", "image2pipe",
             "-vcodec", "ppm", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "", "解图超时（%d 秒）" % timeout
    except OSError as e:
        return False, "", "ffmpeg 无法启动：%s" % str(e)[:120]
    if proc.returncode != 0 or not proc.stdout:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return False, "", (tail[-1].strip()[:200] if tail else "rc=%d" % proc.returncode)
    return True, proc.stdout, ""


# ---------------------------------------------------------------- 调度内核

def pool_size(n_cams):
    """并发度：路少就少开线程，路多封顶 4。

    上限 4 不是拍脑袋：单次推理已经用 2 个 intra_op 线程（见 Engine.ensure），
    4 并发 ≈ 8 个计算线程，再往上只会让单帧耗时翻倍、总吞吐反而下降。
    下限 2 是因为 76ms 的推理不值当为单路退化成串行。
    """
    try:
        n = int(n_cams or 0)
    except (TypeError, ValueError):
        n = 0
    return max(SCHED_DEFAULT_WORKERS, min(SCHED_MAX_WORKERS, n))


def is_stale(queued_at, now, interval):
    """排队超过一个节拍 = 过期帧，直接丢。

    为什么丢而不是"接着推"：检测要回答的是「**此刻**有没有人」。
    一帧在队列里躺了 6 秒才轮到推理，结论对应的是 6 秒前的画面，却会被写进
    当前 5 分钟桶——时间轴上就出现"人走了才标记"。丢掉的代价只是这一拍
    没有结论（记 unknown），不会漏掉下一拍。
    """
    try:
        return (float(now) - float(queued_at)) > max(0.5, float(interval))
    except (TypeError, ValueError):
        return True


class Scheduler:
    """每路独立节拍 + 在跑集合。无 IO、无线程，单测直接喂时间戳驱动。

    ≤0.9.121 的写法是「一个循环里 for 顺序跑完所有路」，一轮耗时 = 路数 ×
    单路耗时；8 路必然超过 5 秒节拍，于是每一拍都迟到，实际检测频率掉到 1/8。
    现在节拍跟着**每一路**走：谁到点谁能跑，跑不下的排队，过期就丢。
    """

    def __init__(self, interval=5.0):
        self.interval = max(1.0, float(interval or 5.0))
        self.next_due = {}
        self.inflight = set()
        self.lock = threading.Lock()

    def due(self, cam_ids, now):
        """到点且没在跑的路（按传入顺序，结果稳定，便于单测断言）。

        到点判定成立就**立刻**推后 next_due，而不是等任务跑完再推：
        队列满被丢掉的那一拍不该在下一拍补跑，否则队列一堵就是双倍负载。
        """
        out = []
        with self.lock:
            for cid in cam_ids:
                if cid in self.inflight:
                    continue
                nd = self.next_due.get(cid)
                if nd is not None and now < nd:
                    continue
                self.next_due[cid] = now + self.interval
                out.append(cid)
        return out

    def begin(self, cid):
        with self.lock:
            self.inflight.add(cid)

    def end(self, cid):
        with self.lock:
            self.inflight.discard(cid)

    def trim(self, alive_ids):
        """清掉已不在配置里的路：删摄像头/关掉某路检测后，不该留下永远不跑的节拍。"""
        keep = set(alive_ids)
        with self.lock:
            for cid in list(self.next_due):
                if cid not in keep:
                    self.next_due.pop(cid, None)
            self.inflight &= keep

    def inflight_count(self):
        with self.lock:
            return len(self.inflight)


# ---------------------------------------------------------------- 调度 Worker

class DetectWorker:
    """有界并发的检测调度（0.9.122）。

    一条 ticker 线程"到点就派活"，N 个工作者线程"抽帧 → 推理 → 落账"。
    三条不变式就是这一批要守的东西：

    1. **有界**：并发 = pool_size(路数)，默认 2、封顶 4；队列长度 2×并发。
       任何时候都不会因为"路多"冒出几十个推理线程把 CPU 吃干——录像和取帧
       在同一台机器上，检测是二等公民。
    2. **不重入**：同一路在跑（或已在队列里）就不再派第二份，防止慢路自我叠加。
    3. **不阻塞**：ticker 只做判定 + `put_nowait`，队列满了就丢并记 unknown，
       绝不等在队列上；过期任务被工作者拿到时直接丢，不推两拍前的画面。

    与 SnapshotWorker 平行而独立：这一路全挂也不影响预览出图。总闸关闭时
    整个 worker 不启动（schedule() 才被 nvr_slim 调用）。
    """

    def __init__(self, engine, ffmpeg, var_dir, record_root_fn, cams_fn,
                 cfg_fn, interval=5, timeout=10, log=None, notifier=None,
                 frame_path_fn=None, workers=None, queue_size=None,
                 shot_store=None):
        self.engine = engine
        self.ffmpeg = ffmpeg
        self.var_dir = var_dir
        self.record_root_fn = record_root_fn
        self.cams_fn = cams_fn
        self.cfg_fn = cfg_fn            # 返回 (detect_cfg, cameras) 快照
        # 0.9.80：优先读取帧层写好的那张 jpg（cam_id -> path），不再自己拉 RTSP。
        # 返回 None/空 → 退回直连抽帧（取流没起来时的降级路径）。
        self.frame_path_fn = frame_path_fn
        self.interval = max(1, min(60, int(interval or 5)))
        self.timeout = int(timeout)
        # workers=None 表示"按路数自动定"，在 start() 里按当时的摄像头数算一次：
        # 测试机 2 路 → 2 并发，院子 8 路 → 4 并发。构造时算不了，因为配置
        # 还没读；固定成 2 又会让 8 路跑不满。
        self._auto_workers = workers is None
        self.workers = max(1, min(SCHED_MAX_WORKERS,
                                  int(workers or SCHED_DEFAULT_WORKERS)))
        self.queue_size = max(1, int(queue_size or self.workers * 2))
        self.log = log or (lambda m: None)
        self.notifier = notifier        # Notifier.offer(cam_name, cat)——None=不通知
        # 事件快照（0.9.129）：None = 不存图，行为与 0.9.128 逐字一致。
        # 只在这里持有一个引用，调用点是 _shot()，它绝不阻塞、绝不抛。
        self.shot_store = shot_store
        self.stop_event = threading.Event()
        self.thread = None              # ticker
        self._threads = []              # 工作者
        self.q = None
        self.sched = Scheduler(self.interval)
        # 时间窗确认（0.9.122）：只决定"记不记台账/发不发通知"。
        # 参数每一拍由 ticker 从配置快照同步（update 内部比对，没变就返回）。
        self.confirmer = _confirm.Confirmer() if _confirm is not None else None
        # 统计：cam_id -> {total, today, cls, unknown, skip, codes, last, ...}
        self.stats = {}
        self._stats_lock = threading.Lock()
        self._day = time.strftime("%Y-%m-%d")
        # 调度实况（验收要看的数）：队列丢了多少、过期丢了多少、"一轮"多长
        self.dropped = 0
        self.stale = 0
        self.last_ok_at = None
        self._pending = 0               # 已派发未完成的任务数
        self._busy_since = 0.0
        self._busy_cams = 0
        self._round_ms = 0.0
        self._round_avg = 0.0
        self._round_cams = 0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        if self._auto_workers:
            try:
                _cfg, cams = self.cfg_fn() or ({}, [])
            except Exception:
                cams = []
            self.workers = pool_size(len(cams or []))
            self.queue_size = max(1, self.workers * 2)
        # 重启时换一只新队列：上一轮积压的旧任务已经过期，留着只会被逐个丢掉
        self.q = queue.Queue(maxsize=self.queue_size)
        self.sched = Scheduler(self.interval)
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name="detect")
        self.thread.start()
        self._threads = []
        for i in range(self.workers):
            t = threading.Thread(target=self._work, daemon=True,
                                 name="detect-%d" % i)
            t.start()
            self._threads.append(t)

    def stop(self):
        self.stop_event.set()

    def alive(self):
        """ticker 与**所有**工作者都活着才算在跑：少一个工作者就是少一份并发，
        而"线程悄悄死了"正是旧实现最难发现的一种故障（接口照旧说 running）。"""
        if not (self.thread and self.thread.is_alive()):
            return False
        return all(t.is_alive() for t in self._threads)

    # ---------------------------------------------------------- 主循环
    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                self.log("检测轮异常：%r" % e)
            self.stop_event.wait(SCHED_TICK_SEC)

    def _tick(self):
        """一拍：判定谁到点 → 派活。只做判定与入队，绝不在这里抽帧/推理。"""
        cfg, cams = self.cfg_fn() or ({}, [])
        if not cfg.get("enable"):
            return
        ok, why = self.engine.ensure()
        if not ok:
            # 诚实但安静：状态行已经显示原因，这里同一原因只记一条
            key = "engine|%s" % why[:40]
            if self.stats.get("_engine_err") != key:
                self.stats["_engine_err"] = key
                self.log("AI 检测不可用：%s" % why)
            return
        conf_map = cfg.get("conf") or conf_from_sensitivity("std")
        now = time.time()
        ready = []
        for c in cams:
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
            ready.append((c, url, classes))
        ids = [c["id"] for c, _u, _k in ready]
        self.sched.trim(ids)
        if self.confirmer is not None:
            # 参数跟配置走（改窗口/阈值不重启服务），并清掉已删除的路。
            self.confirmer.update(cfg.get("confirm"))
            self.confirmer.trim(ids)
        due = self.sched.due(ids, now)
        if not due:
            return
        by_id = {c["id"]: (c, u, k) for c, u, k in ready}
        got = 0
        for cid in due:
            cam, url, classes = by_id[cid]
            self.sched.begin(cid)
            try:
                self.q.put_nowait((cid, cam, url, conf_map, classes, now))
                got += 1
            except queue.Full:
                # 队列满 = 上一拍的活还没干完。丢这一拍并如实记 unknown，
                # 不做阻塞等待：ticker 一旦堵住，所有路一起停摆，那才是真事故。
                self.sched.end(cid)
                with self._stats_lock:
                    self.dropped += 1
                self._unknown(cid, E_QUEUE, "检测队列已满（%d 项），本轮丢弃" % self.queue_size)
        if got:
            self._busy_add(got, now)

    def _work(self):
        """工作者线程：取任务 → 判过期 → 抽帧推理落账 → 交还 inflight。"""
        while not self.stop_event.is_set():
            try:
                task = self.q.get(timeout=SCHED_TICK_SEC)
            except queue.Empty:
                continue
            cid = task[0] if task else None
            try:
                cid, cam, url, conf_map, classes, queued_at = task
                if is_stale(queued_at, time.time(), self.interval):
                    with self._stats_lock:
                        self.stale += 1
                    self._unknown(cid, E_STALE,
                                  "排队超过 %d 秒，丢弃过期帧" % self.interval)
                else:
                    self._one(cam, url, conf_map, classes, time.time())
            except Exception as e:
                self.log("检测任务异常：%r" % e)
                if cid:
                    self._unknown(cid, E_INFER, "检测任务异常：%r" % e)
            finally:
                if cid:
                    self.sched.end(cid)
                self._busy_done()

    # ---------------------------------------------------------- 一轮耗时
    def _busy_add(self, n, now):
        """记录"这一拍到点的那批路什么时候全部干完"——验收口径
        「8 路一轮 ≤ 1.8 × 单路」直接读 round_ms / round_cams。"""
        with self._stats_lock:
            if self._pending == 0:
                self._busy_since = now
                self._busy_cams = 0
            self._pending += n
            self._busy_cams += n

    def _busy_done(self):
        with self._stats_lock:
            if self._pending <= 0:
                return
            self._pending -= 1
            if self._pending > 0:
                return
            ms = round((time.time() - self._busy_since) * 1000.0, 1)
            self._round_ms = ms
            self._round_cams = self._busy_cams
            self._round_avg = (ms if not self._round_avg
                               else round(self._round_avg * 0.8 + ms * 0.2, 1))

    # ---------------------------------------------------------- 单路一拍
    def _one(self, cam, url, conf_map, classes, now):
        ok, data, err, src = self._grab(cam, url)
        cid = cam["id"]
        if not ok:
            if err == NO_NEW_FRAME:
                # 正常节拍错位：取帧 10 秒一张、检测 5 秒一拍，隔拍必撞同一张图。
                # 既不是 hit 也不是 miss，更不是 unknown —— 记 skip，不刷诊断页。
                self._skip(cid)
                return
            self._unknown(cid, frame_error_code(err), err)
            return
        ppm = parse_ppm(data)
        if not ppm:
            self._unknown(cid, E_FRAME, "PPM 解析失败")
            return
        w, h, rgb = ppm
        dets = self.engine.infer(rgb, w, h, conf_map)
        if dets is None:
            self._unknown(cid, E_INFER, self.engine.error or "推理失败")
            return
        # 类别过滤（算力口径）+ 两块区域过滤（框中心口径，见 apply_regions）。
        # 格号解码成 16×9 归一化矩形，与归一化框同一坐标系，直接比。
        # 只统计区留空时 in_include 恒真 —— 没配就与旧版本一模一样。
        dets, outside = apply_regions(dets, classes,
                                      self._ignore_rects(cam),
                                      self._include_rects(cam))
        if outside:
            self._outside(cid, outside)
        if dets:
            wins = self._confirm(cam, dets, now)
            if wins:
                self._write_ledger(cam, wins, now)
                # 顺序是刻意的（0.9.129）：先落台账（事实），再存图（附属物），
                # 最后通知。图存不下来不许影响前两步与通知 —— _shot 内部吞掉
                # 一切异常，返回值也没人看。
                # 框跟着图走（0.9.130）：这一拍保留的 dets 就是这张图上"检测器
                # 看到了什么"，交给快照层旁挂一份 —— 检测线程里不多做一次推理。
                self._shot(cam, src, now, dets)
                self._notify(cam, {k for _s, _e, c in wins for k in c})
        self._ok_stat(cid, dets)

    def _confirm(self, cam, dets, now):
        """检出框 → 台账窗口（当天钟点秒）。**只决定记不记台账**。

        没有这一层时每一帧都自成一条事件：5 秒采样下，一片树影就能在事件页
        留下一串 2 秒碎片。时间窗确认要求「N 帧里至少 M 帧命中」，单帧误报
        直接消失；确认之后的每一帧都会带上**整条事件的起点**，所以台账里
        长出来的仍然是一个连续窗口，而不是逐帧的小段。

        确认层关掉（confirm.enable=false）或模块缺失时退回旧口径（每帧一条），
        这样出问题能一键回到 0.9.121 的行为，不用回滚整个包。
        """
        lt = time.localtime(now)
        tod = float(lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        if self.confirmer is None or not self.confirmer.enabled:
            return windows_from_hits([(tod, d["c"]) for d in dets])
        # 每类只留最大置信度：确认看的是"这一帧这一类有多确定"，
        # 同帧十个框不会因此更可信（重复框是同一个人的手和脚）。
        confs = {}
        for d in dets:
            c = d.get("c")
            if c not in CATEGORIES:
                continue
            try:
                confs[c] = max(confs.get(c, 0.0), float(d.get("s") or 0.0))
            except (TypeError, ValueError):
                continue
        out = []
        for s_ts, e_ts, cls in self.confirmer.feed(cam["id"], now, confs):
            # now（epoch）→ tod（当天钟点秒）的换算只做一次，窗口两端各自平移。
            s = max(0.0, tod - (now - s_ts))
            e = tod + (e_ts - now)
            if e > s:
                out.append((s, e, cls))
        return out

    def _grab(self, cam, url):
        """取一帧 PPM：优先用取帧层的本地 jpg，没有才退回自己拉流。

        统一取流之后，「每路每 5 秒新建两次 RTSP 会话」变成零次 —— 摄像头只
        看到 ingest 那一条连接。

        **取帧层在跑时不再退回直连**：退回等于把刚拆掉的连接风暴重新装回来，
        而且是在摄像头本来就异常的时候。宁可这一轮不出检测结果（诊断页会
        显示「取帧层还没有新画面」），也不要偷偷多开一路 RTSP。

        返回 `(ok, data, err, src)`：`src` 是这次推理读的那张 jpg 的路径
        （直连抽帧时为空串）。事件快照（0.9.129）靠它拿到"当时那一帧" ——
        让调用方自己去问 `frame_path_fn` 会拿到**下一张**图的路径，那正好是
        「拍现在、不拍当时」的假数据。
        """
        if self.frame_path_fn is not None:
            try:
                path = self.frame_path_fn(cam["id"])
            except Exception as e:
                return False, "", "取帧层异常：%r" % e, ""
            if path == "":
                return False, "", NO_NEW_FRAME, ""
            if not path:
                return False, "", "取帧层还没有新画面", ""
            ok, data, err = grab_ppm_image(self.ffmpeg, path, timeout=self.timeout)
            return ok, data, err, (path if ok else "")
        ok, data, err = grab_ppm(self.ffmpeg, url, timeout=self.timeout)
        return ok, data, err, ""

    def _shot(self, cam, src, now, dets=None):
        """把"确认那一刻"的那一帧交给快照层（0.9.129）。

        三条自我约束，改的时候一条都别松：

        - **不阻塞**。只做一次 `open().read()`（≤2MB）与一次非阻塞入队。
          写盘、建目录、清过期都在 ShotStore 自己的线程里。
        - **不抛**。快照是附属物，任何异常都不许冒到 `_work()` 上去 ——
          那会被记成「检测任务异常」，一条通知与一条台账都可能跟着丢。
        - **拍当时**。时间取源图自己的 mtime（取帧层落盘的时刻），不是 now。
          检测节拍比取帧慢时，两者能差一个抽帧间隔；用 now 会把图说成比实际晚。

        `dets`（0.9.130）是这一拍**过完区域过滤**的框。它和这张图是同一次推理的
        产物，所以叠回去的位置天然是对的；换成"下次再跑一遍推理"就会因为画面
        变了而框错人。
        """
        st = self.shot_store
        if st is None or not src:
            return
        try:
            at = os.path.getmtime(src)
        except OSError:
            at = now
        try:
            with open(src, "rb") as f:
                data = f.read(SHOT_MAX_BYTES + 1)
        except OSError:
            return
        if not data or len(data) > SHOT_MAX_BYTES:
            return
        try:
            st.offer(cam.get("id"), data, at, dets)
        except Exception as e:
            self.log("事件快照入队异常（%s）：%r" % (cam.get("id"), e))

    def _notify(self, cam, cats):
        """确认过的类别 → 通知入队（person/vehicle 口径；two_wheeler 归 vehicle）。

        0.9.122 起入参是**类别集合**而不是原始框：通知跟着确认走。给用户推一条
        "有人出现"而事件页里根本没有这条记录，是最容易让人不再看通知的做法。
        offer 内部另查 cooldown 与开关，绝不在推理线程里发请求。"""
        if not self.notifier:
            return
        cats = set(cats or ())
        name = cam.get("name") or cam["id"]
        for cat in ("person", "vehicle"):
            hit = cat in cats or (cat == "vehicle" and "two_wheeler" in cats)
            if hit:
                try:
                    self.notifier(name, cat)
                except Exception:
                    pass

    # ---------------------------------------------------------- 台账
    def _write_ledger(self, cam, wins, now):
        """窗口 → 更新该路当前 5 分钟桶（src=ai:<分片文件名>），原子整块替换。

        wins 由 `_confirm()` 给出：`[(当天钟点秒起, 止, {类别: 1})]`。
        0.9.121 以前这里自己从 dets 逐帧算窗口，于是每一帧都落成一条事件；
        现在时间窗确认已经把"够不够格"判完，这一层只负责落盘。
        """
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
        rel = []
        for s, e, c in merged:
            # 跨桶事件：窗口起点可能早于本桶起点。源名就是「这个 5 分钟」，
            # 把更早的时间写进这个源等于篡改别人的账；裁到桶首，跨桶的连续性
            # 交给读侧按 AI_GAP_SEC 合并（逐帧写窗口那会儿天然也是这样）。
            if e <= start_sec:
                continue
            rel.append([max(start_sec, float(s)) - start_sec, e - start_sec, c])
        if not rel:
            return
        ledger.merge(root, dname, date, src, start_sec,
                     rel,
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

    # ---------------------------------------------------------- 画面区域
    @staticmethod
    def _ignore_rects(cam):
        """cam.masks.ignore（16×9 格号）→ 归一化矩形。格号编码 row*16+col。"""
        m = (cam or {}).get("masks")
        if not isinstance(m, dict):
            return []
        return cells_to_rects(m.get("ignore") or [])

    @staticmethod
    def _include_rects(cam):
        """cam.masks.include（只统计区）→ 归一化矩形。空列表 = 不限制。

        与 ignore 分开两个函数而不是合成一个字典返回：调用点各自只关心自己
        那块，合成结构会让"忘了传第二块"这种 bug 更难看出来。
        """
        m = (cam or {}).get("masks")
        if not isinstance(m, dict):
            return []
        return cells_to_rects(m.get("include") or [])

    # ---------------------------------------------------------- 三态统计
    @staticmethod
    def _blank():
        return {"total": 0, "today": {}, "cls": {}, "last": None,
                "unknown": {}, "skip": {}, "codes": {},
                # 只统计区挡掉的框数（按天）。它不是三态里的任何一档：
                # 这一拍照常出了结论（hit 或 miss），只是"区外的目标不算"。
                "outside": {}}

    def _roll_day(self):
        """跨零点自动换天（调用方已持锁）。

        worker 是长期存在的，_day 在构造时定死会让「今日」在次日一直读旧键
        —— 诊断页从此显示 0 帧，用户以为 AI 不干活了。
        """
        d = time.strftime("%Y-%m-%d")
        if d != self._day:
            self._day = d

    def _ok_stat(self, cam_id, dets):
        """有结论的一拍：hit 或 miss。

        **只有这里**刷新 last_success_at —— 它回答的是"检测最后一次真的看见
        画面是何时"，不是"线程还在转"。状态行的"正常"必须由它背书。
        """
        with self._stats_lock:
            self._roll_day()
            st = self.stats.setdefault(cam_id, self._blank())
            st["total"] += 1
            d = self._day
            st["today"][d] = st["today"].get(d, 0) + 1
            st["last_state"] = HIT if dets else MISS
            if dets:
                cd = st["cls"].setdefault(d, {})
                for cat in {x["c"] for x in dets}:
                    cd[cat] = cd.get(cat, 0) + 1
                st["last"] = {"t": time.time(),
                              "cats": sorted({x["c"] for x in dets})}
            self.last_ok_at = time.time()
            self.stats.pop("_engine_err", None)

    def _unknown(self, cam_id, code, err):
        """没有结论的一拍。绝不写成"无活动"。

        unknown 与 miss 的差别就是「不知道」和「没人」的差别；把摄像头掉线
        记成"没人来过"，等于替用户签了一份他没看过的空白报告。
        """
        with self._stats_lock:
            self._roll_day()
            st = self.stats.setdefault(cam_id, self._blank())
            d = self._day
            st["unknown"][d] = st["unknown"].get(d, 0) + 1
            cd = st["codes"].setdefault(d, {})
            cd[code] = cd.get(code, 0) + 1
            st["last_err"] = str(err)[:120]
            st["last_code"] = code
            st["last_err_at"] = time.time()
            st["last_state"] = UNKNOWN

    def _skip(self, cam_id):
        """这一拍没有新画面（正常节拍错位）。三类都不算：不是 hit、不是 miss、
        也不是故障，所以单独一档，免得每两拍就刷一次"未知"。"""
        with self._stats_lock:
            self._roll_day()
            st = self.stats.setdefault(cam_id, self._blank())
            d = self._day
            st["skip"][d] = st["skip"].get(d, 0) + 1

    def _outside(self, cam_id, n):
        """只统计区挡掉的框数（按天累计）。

        为什么要单独记账：用户把只统计区画小了，界面上一切正常、事件页一片
        安静 —— 那种"看起来对"的安静最危险。有这个数，诊断页能说"今天挡掉
        了 N 个框"，用户才知道该去把区域放大，而不是以为院子真没人来。
        """
        if n <= 0:
            return
        with self._stats_lock:
            self._roll_day()
            st = self.stats.setdefault(cam_id, self._blank())
            d = self._day
            st["outside"][d] = st["outside"].get(d, 0) + int(n)

    # ---------------------------------------------------------- 状态
    def status(self):
        """检测详情：加载态 + 错误码 + 每路三态计数 + 调度实况。

        判定口径（能不能用、为什么不能用）在 status_of 里，这里只交事实。
        """
        with self._stats_lock:
            per_cam = {}
            d = self._day
            for cid, st in self.stats.items():
                if cid.startswith("_"):
                    continue
                per_cam[cid] = {
                    "total": st.get("total", 0),
                    "today": st.get("today", {}).get(d, 0),
                    "cls": st.get("cls", {}).get(d, {}),
                    "unknown": st.get("unknown", {}).get(d, 0),
                    "skip": st.get("skip", {}).get(d, 0),
                    "outside": st.get("outside", {}).get(d, 0),
                    "codes": st.get("codes", {}).get(d, {}),
                    "last": st.get("last"),
                    "last_state": st.get("last_state"),
                    "last_err": st.get("last_err"),
                    "last_code": st.get("last_code"),
                }
            sched = {
                "workers": self.workers,
                "interval": self.interval,
                "queued": (self.q.qsize() if self.q is not None else 0),
                "queue_max": self.queue_size,
                "inflight": self.sched.inflight_count(),
                "dropped_full": self.dropped,
                "dropped_stale": self.stale,
                "pending": self._pending,
                "round_ms": self._round_ms,
                "round_avg_ms": self._round_avg,
                "round_cams": self._round_cams,
            }
        eng = self.engine
        return {
            "loaded": bool(eng is not None and eng.loaded),
            "error": (eng.error if eng is not None else ""),
            "error_code": (getattr(eng, "error_code", "") if eng is not None else ""),
            "avg_ms": (eng.avg_ms if eng is not None else 0),
            "alive": self.alive(),
            "last_success_at": self.last_ok_at,
            # 推理设备（0.9.125）：请求什么 / 实际建在什么后端 / 为什么回落。
            # 从引擎身上取，不从配置抄 —— 配置是意图，引擎才是事实。
            "device": engine_device(eng),
            "per_cam": per_cam,
            "sched": sched,
            # 时间窗确认实况：确认过几条、单帧误报挡掉几手。这两个数就是
            # 「误报碎片有没有变少」的出处，界面上不必再估。
            "confirm": (self.confirmer.snapshot() if self.confirmer is not None
                        else confirm_blank()),
        }


# ---------------------------------------------------------------- 统一状态

def sched_blank():
    """调度实况的空壳（没 worker 时的形状）。

    为什么给一张"全 0 的形状"而不是 `{}`：调用方按固定键读，缺键就得判断
    "是没有还是没起"，判断多了迟早写错。0 在这里是诚实的——真的没跑。
    """
    return {"workers": 0, "interval": 0, "queued": 0, "queue_max": 0,
            "inflight": 0, "dropped_full": 0, "dropped_stale": 0,
            "pending": 0, "round_ms": 0.0, "round_avg_ms": 0.0, "round_cams": 0}


def confirm_blank():
    """确认实况的空壳（没 worker 或模块缺失时），理由同 sched_blank。"""
    if _confirm is not None:
        return _confirm.snapshot_blank()
    return {"enabled": False, "window": 0, "min_hits": 0,
            "confirmed": 0, "rejected": 0, "pending": 0}


def status_of(worker=None, det_cfg=None, ffmpeg="", runtime=None):
    """`/api/status` 的 detect 块：**一个**判定口径，三处共用。

    为什么要有这个函数：状态行、诊断卡、接口以前各自拼一遍"能不能用"，
    同一个现场在不同地方说法不一样（worker 没起时一处写"未运行"、
    一处写"加载中"）。现在只有这里判，前端只负责把 state 翻译成中文。

    返回（前七项是批次 4 定的契约，其余为既有字段的兼容保留）：
      enabled        总闸是否打开
      available      现在能不能出检测结果（true 才敢在界面上说"正常"）
      running        调度线程是否活着
      state          disabled / starting / unavailable / ready
      error_code     失败码（见 E_*）；空串=没有失败
      error_message  给人看的中文原因
      last_success_at 最后一次真的看见画面的时刻（None=还没有过）
    """
    enabled = _as_bool((det_cfg or {}).get("enable"), False)
    detail = {}
    if worker is not None:
        try:
            detail = dict(worker.status() or {})
        except Exception as e:
            detail = {"error": "检测状态读取失败：%r" % e, "error_code": E_STATUS}
    running = bool(detail.get("alive") or detail.get("running"))
    code = str(detail.get("error_code") or "")
    msg = str(detail.get("error") or "")
    if not enabled:
        # 关闭不是故障：给空码，界面按"已关闭"说，不要红字
        state, code, msg = "disabled", "", ""
    elif not ffmpeg and not code:
        state, code, msg = "unavailable", E_FFMPEG, "未找到 ffmpeg，无法抽帧"
    elif worker is None:
        state = "unavailable"
        if not code:
            code, msg = E_WORKER, "检测进程未启动（服务刚启动或已退出）"
    elif code:
        state = "unavailable"
    elif not running:
        state, code, msg = "unavailable", E_WORKER, "检测线程已停止"
    elif not detail.get("loaded"):
        # 首次要解 wheel + 加载 10.7MB 模型，几秒钟的事，别报错
        state = "starting"
    else:
        state = "ready"
    out = {
        "enabled": enabled,
        "available": state == "ready",
        "running": running,
        "state": state,
        "error_code": code,
        "error_message": msg,
        "last_success_at": detail.get("last_success_at"),
    }
    for k in ("loaded", "error", "avg_ms", "per_cam", "sched", "alive",
              "confirm", "device"):
        if k in detail:
            out[k] = detail[k]
    out.setdefault("loaded", False)
    out.setdefault("error", "")
    out.setdefault("avg_ms", 0)
    out.setdefault("per_cam", {})
    out.setdefault("sched", sched_blank())
    out.setdefault("confirm", confirm_blank())
    # 没 worker 时设备块不能空着：requested 至少要说清"配置里要的是什么"，
    # 否则前端只能写"未知"，用户看不出自己那台到底配没配 GPU。
    out.setdefault("device", device_blank())
    if not out["device"].get("requested"):
        out["device"]["requested"] = str((det_cfg or {}).get("device") or "auto")
    if runtime is not None:
        out["runtime"] = runtime
    return out


if __name__ == "__main__":
    # 命令行自检：纯逻辑部分（不加载模型）
    print(json.dumps({
        "trigger_ok": [trigger_ok(None, {"motion": True, "person": False, "vehicle": False}),
                       trigger_ok({"person": 1}, {"motion": False, "person": False, "vehicle": False}),
                       trigger_ok({"two_wheeler": 1}, {"motion": False, "person": False, "vehicle": True})],
        "defaults": DETECT_DEFAULTS,
    }, ensure_ascii=False, indent=1))
