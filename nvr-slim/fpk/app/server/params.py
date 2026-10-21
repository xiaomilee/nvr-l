#!/usr/bin/env python3
"""判读与编码参数体系。

移植自 Windows 版 VideoTrim（video-trim-方案.md / video-trim-GUI设计.md §3.3.4），
按 fnOS + Intel UHD 730 重新定标。只依赖标准库。

设计要点（与 Windows 版保持一致，避免两套行为）：
1. 阈值 = 本机位噪声底线 × noise_mult，不是写死的常数 —— 树多、雨多的机位自动放宽。
2. 黑白/彩色不是"同一套参数换个数值"，而是两套判据组合：
   黑白只有亮度路（必须空域模糊压颗粒噪声）；彩色多一条色度路（低门槛、不模糊）。
3. 画面类型靠**色度标准差**判定，不是饱和度：夜间彩色画面饱和度只有 0.15，
   按饱和度会误判成黑白；真黑白画面的 U/V 恒为 128，std < 1.5。
4. 调光靠**同向率**判定：变化像素里 80% 以上同向才算整体调光。
   实测调光帧 100% 同向，车通过帧只有 42%，分离很干净。
5. 播放加速分两种，不能混：只改时间戳不减体积；重编码丢帧才减体积。
"""

# ---------------------------------------------------------------- 默认值

ANALYSIS_DEFAULTS = {
    # 画面类型
    "scene": "auto",            # auto | mono | color
    "scene_probe_sec": 5,       # 判彩色的抽点间隔，0=逐帧
    "icr_guard": 2.0,           # 滤片切换前后排除窗口（秒）
    # 采样
    "fps": 2.0,                 # 0.2~10，D1/720p 枪机建议 4
    "width": 320,               # 80~640
    # 降噪
    "blur_mono": 1,             # 黑白空域模糊半径，0~3
    "blur_color": 0,
    "temporal_median": 0,       # 0=关。开了会抹掉只出现 1~2 帧的目标
    # 判据
    "pixel_delta": 16,          # 黑白降到 14
    "chroma_delta": 8,
    "noise_mult": 2.0,          # 1.1~20
    "floor": 5e-4,              # 噪声极小时的绝对下限
    "smooth": 3,                # 分数曲线中值平滑帧数
    "min_score": {              # 无噪声基线时的保守兜底（320x180 画面）
        "high": 0.002, "mid": 0.008, "low": 0.02,
    },
    "sensitivity": "mid",       # high | mid | low
    # 事件成型
    "min_event": 2.0,           # 短于此算瞬态（飞虫、闪光）
    "strong_area": 0.05,        # 强变化豁免：像素占比
    "strong_chroma": 0.02,
    "pre_buf": 3.0,
    "post_buf": 3.0,
    "merge_gap": 5.0,
    "min_clip": 3.0,
    "max_clip": 0,              # 0=不限
    # 光照 / 曝光
    "light_jump": 6.0,          # 帧间平均亮度跳变
    "light_coherence": 0.80,    # 同向率
    # 性能
    "prescan": True,            # 先用 pkt_size 曲线粗筛，不解码
    "prescan_margin": 10,       # 候选区间前后外扩秒数
    # 播放加速（只作用于保留段，检测永远在原片时间轴）
    "speed": 1.0,               # 1 | 1.5 | 2 | 3 | 4
    "speed_mode": "pts",        # pts=只改时间戳（无损、不减体积） | reencode=丢帧
}

# 编码档：白天彩色 / 夜间黑白。Windows 版是 libx264 CRF，飞牛改用 QSV ICQ
# （-global_quality），且同画质比 x265 多耗 20~30% 码率，装机需 A/B 校准。
ENCODE_DEFAULTS = {
    "mode": "fine",             # fast=流复制（画质参数全部失效） | fine=重编码
    "codec": "hevc",            # hevc | h264
    "day":   {"quality": 26, "fps": 12, "scale": 100, "denoise": 1},
    "night": {"quality": 29, "fps": 8,  "scale": 75,  "denoise": 3},
    "audio": "drop",            # drop | aac32
    "faststart": True,
    "night_fps_auto": True,     # 首夜自动校准 8→10/6
}

# 画面类型预设：黑白/彩色是两套判据组合，不是换个数值
SCENE_PRESETS = {
    "mono":  {"blur": 1, "pixel_delta": 14, "use_chroma": False},
    "color": {"blur": 0, "pixel_delta": 16, "use_chroma": True},
}

PROFILE_DEFAULTS = {
    "name": "未命名机位",
    "enabled": True,
    "analysis": dict(ANALYSIS_DEFAULTS),
    "encode": {
        "mode": ENCODE_DEFAULTS["mode"],
        "codec": ENCODE_DEFAULTS["codec"],
        "day": dict(ENCODE_DEFAULTS["day"]),
        "night": dict(ENCODE_DEFAULTS["night"]),
        "audio": ENCODE_DEFAULTS["audio"],
        "faststart": ENCODE_DEFAULTS["faststart"],
        "night_fps_auto": ENCODE_DEFAULTS["night_fps_auto"],
    },
    # 归一化坐标 0~1，带权重而不是二值开关（见规格 §6.3）
    "masks": [],
}

# ---------------------------------------------------------------- 校验表

SCHEMA = {
    "analysis.fps":        (0.2, 10.0, float),
    "analysis.width":      (80, 640, int),
    "analysis.blur_mono":  (0, 3, int),
    "analysis.blur_color": (0, 3, int),
    "analysis.temporal_median": (0, 7, int),
    "analysis.pixel_delta": (1, 80, int),
    "analysis.chroma_delta": (1, 80, int),
    "analysis.noise_mult": (1.1, 20.0, float),
    "analysis.smooth":     (1, 9, int),
    "analysis.min_event":  (0.5, 60.0, float),
    "analysis.pre_buf":    (0, 30.0, float),
    "analysis.post_buf":   (0, 30.0, float),
    "analysis.merge_gap":  (0, 120.0, float),
    "analysis.min_clip":   (0, 120.0, float),
    "analysis.max_clip":   (0, 3600.0, float),
    "analysis.speed":      (1.0, 4.0, float),
    "encode.day.quality":  (10, 45, int),
    "encode.night.quality": (10, 45, int),
    "encode.day.fps":      (5, 30, int),
    "encode.night.fps":    (5, 30, int),
    "encode.day.scale":    (40, 100, int),
    "encode.night.scale":  (40, 100, int),
    "encode.day.denoise":  (0, 3, int),
    "encode.night.denoise": (0, 3, int),
}

ENUMS = {
    "analysis.scene": {"auto", "mono", "color"},
    "analysis.sensitivity": {"high", "mid", "low"},
    "analysis.speed_mode": {"pts", "reencode"},
    "encode.mode": {"fast", "fine"},
    "encode.codec": {"hevc", "h264"},
    "encode.audio": {"drop", "aac32"},
}


def analysis(**overrides):
    """独立的一份分析参数。

    造完整 profile 只为了取阈值时，用这个更直接（测试、一次性判定都靠它）。
    """
    a = json_copy(ANALYSIS_DEFAULTS)
    a.update(overrides)
    return a


def new_profile(name="未命名机位"):
    p = {
        "name": name,
        "enabled": True,
        "analysis": json_copy(ANALYSIS_DEFAULTS),
        "encode": {
            "mode": ENCODE_DEFAULTS["mode"],
            "codec": ENCODE_DEFAULTS["codec"],
            "day": dict(ENCODE_DEFAULTS["day"]),
            "night": dict(ENCODE_DEFAULTS["night"]),
            "audio": ENCODE_DEFAULTS["audio"],
            "faststart": ENCODE_DEFAULTS["faststart"],
            "night_fps_auto": ENCODE_DEFAULTS["night_fps_auto"],
        },
        "masks": [],
    }
    return p


def json_copy(d):
    # 只处理 dict/list/标量，避免引入 copy 之外的东西
    if isinstance(d, dict):
        return {k: json_copy(v) for k, v in d.items()}
    if isinstance(d, list):
        return [json_copy(v) for v in d]
    return d


def _dig(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(d, path, value):
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def validate(profile):
    """返回 (ok, errors)。打包与保存配置前都要过一遍。"""
    errors = []
    for path, (lo, hi, typ) in SCHEMA.items():
        v = _dig(profile, path)
        if v is None:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errors.append("%s 必须是数字" % path)
            continue
        if v < lo or v > hi:
            errors.append("%s=%s 超出范围 %s~%s" % (path, v, lo, hi))
    for path, allowed in ENUMS.items():
        v = _dig(profile, path)
        if v is not None and v not in allowed:
            errors.append("%s=%s 不在 %s 中" % (path, v, sorted(allowed)))
    for i, m in enumerate(profile.get("masks") or []):
        area = float(m.get("w", 0)) * float(m.get("h", 0))
        if area > 0.5:
            errors.append("mask[%d] 单块面积 %.0f%% 超过 50%%，真实事件会被一起忽略" % (i, area * 100))
    total = sum(float(m.get("w", 0)) * float(m.get("h", 0)) for m in (profile.get("masks") or []))
    if total > 0.7:
        errors.append("mask 合计面积 %.0f%% 超过 70%%，拒绝保存" % (total * 100))
    return (not errors), errors


# ---------------------------------------------------------------- 生效参数

def resolve(profile, scene="color", sensitivity=None):
    """把档位、画面类型预设合并成一份"真正拿去算"的参数。

    scene: mono / color
    """
    a = json_copy((profile or {}).get("analysis") or ANALYSIS_DEFAULTS)
    s = SCENE_PRESETS.get(scene, SCENE_PRESETS["color"])
    a["blur"] = a.get("blur_mono", 1) if scene == "mono" else a.get("blur_color", 0)
    a["use_chroma"] = s["use_chroma"]
    # 用户显式改过 pixel_delta 就以用户为准；否则用画面类型预设
    if a.get("pixel_delta") in (None, ANALYSIS_DEFAULTS["pixel_delta"]):
        a["pixel_delta"] = s["pixel_delta"]
    sens = sensitivity or a.get("sensitivity", "mid")
    a["score_floor"] = ANALYSIS_DEFAULTS["min_score"].get(sens, 0.008)
    a["scene"] = scene
    return a


def threshold(noise_floor, a):
    """实际阈值 = 本机位噪声底线 × 倍数，但不低于绝对下限。

    这是整套判据里最该调的旋钮：环境越吵（树多、雨多）倍数越大。

    注意 score_floor 只在**噪声基线未知**（首帧还没测出来）时兜底。
    如果拿它一起取 max，会把 0.0008 的自适应阈值抬到 0.008，
    自适应就废了 —— 这也是"换了机位却怎么调都迟钝"的常见原因。
    """
    base = float(noise_floor or 0.0)
    mult = float(a.get("noise_mult", 2.0))
    floor = float(a.get("floor", 5e-4))
    if base <= 0:
        return float(a.get("score_floor", floor) or floor)
    return max(base * mult, floor)


def light_is_event(prev_mean, cur_mean, coherence, a):
    """整帧亮度跳变 + 高同向率 = 调光，不是事件。只更新背景，不计分。"""
    jump = abs(float(cur_mean) - float(prev_mean))
    limit = float(a.get("light_jump", 6.0))
    if jump < limit:
        return False
    return float(coherence) >= float(a.get("light_coherence", 0.80))


def classify_scene(chroma_std):
    """用色度标准差判黑白/彩色。

    真黑白（红外）U/V 恒为 128，std < 1.5；彩色画面即使发灰也有 6 以上。
    不要用饱和度：夜间彩色画面饱和度只有 0.15，会误判成黑白。
    """
    return "mono" if float(chroma_std) < 1.5 else "color"


# ---------------------------------------------------------------- 布局

def grid_cols(n, max_cols=4):
    """宫格列数 = ceil(sqrt(n))：1 路 1 列、2~4 路 2 列、5~9 路 3 列。

    手机竖屏上 4 路 2×2 每格约 195×110，够判断"有没有人"，
    再小就只能看个色块，不如直接点开单路。
    """
    import math
    n = max(1, int(n))
    return max(1, min(int(max_cols), int(math.ceil(math.sqrt(n)))))


# ---------------------------------------------------------------- 指纹
def fingerprint(profile, scene=None):
    """参数指纹进幂等键：参数一改，指纹变，自动重新处理。

    只取影响产物的字段（判据、编码、遮蔽区），不含 name/enabled 这类元数据，
    否则改个机位名就会把一天的活全部重算。
    """
    import hashlib
    p = profile or {}
    payload = {
        "analysis": p.get("analysis") or {},
        "encode": p.get("encode") or {},
        "masks": p.get("masks") or [],
    }
    if scene:
        payload["analysis"] = dict(payload["analysis"], resolved_scene=scene)
    raw = repr(_canonical(payload)).encode("utf-8")   # dict 已排序，repr 稳定可复现
    return hashlib.sha256(raw).hexdigest()[:16]


def _canonical(obj):
    if isinstance(obj, dict):
        return {k: _canonical(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canonical(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


# ---------------------------------------------------------------- 命令行自检

if __name__ == "__main__":
    import json
    import sys

    prof = new_profile("自检机位")
    ok, errs = validate(prof)
    print("默认配置合法:", ok, errs)
    for scene in ("mono", "color"):
        a = resolve(prof, scene)
        print(scene, "blur=%s pixel_delta=%s chroma=%s threshold(0.0004)=%.5f"
              % (a["blur"], a["pixel_delta"], a["use_chroma"],
                 threshold(0.0004, a)))
    print("场景判定 1.2 ->", classify_scene(1.2), "| 23.2 ->", classify_scene(23.2))
    print("调光?", light_is_event(40, 62, 0.98, resolve(prof)), "| 车通过?",
          light_is_event(40, 43, 0.42, resolve(prof)))
    print("指纹:", fingerprint(prof), "| 排序无关:",
          fingerprint(prof) == fingerprint(json_copy(prof)))
    sys.exit(0 if ok else 1)
