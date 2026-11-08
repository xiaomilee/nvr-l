# -*- coding: utf-8 -*-
"""时间窗确认：把「某一帧里出现过」变成「确实发生过」。

为什么要有这一层（批次 4 清单第 2 项）：5 秒抽一帧、每帧独立判定，等于每一帧
都单独成立一条事件。5 秒采样下，树影、反光、远处一晃而过的人影都能让某一帧
出框，台账于是被切成一串 2 秒碎片 —— 事件页"最近发生了什么"全是噪声，
缩时也被这些碎片带着跳。确认层不改推理、不改取帧节奏，只在「这一帧要不要
记一笔」这一步加一道时间窗。

五条口径（全是纯计算：不读盘、不看时钟，now 与置信度都由调用方传）：

  窗口    最近 N 个**有结论的帧**里至少 M 帧命中。用帧数而不是秒数：检测节拍
          会随路数与负载变化，"3 帧"在任何节拍下都表示同一件事，而"15 秒"
          在 8 路时会变成 1 帧、在 2 路时会变成 3 帧。
  置信度  只有 ≥ min_conf[类] 的框才算命中。它高于 detect.conf 的检出阈值：
          检出阈值决定"要不要往下算"，确认阈值决定"算不算一件事"。
  持续    命中跨度 ≥ min_span_sec 才确认。M=2 时相邻两帧天然满足，这条是给
          「隔很久才凑够第二帧」和自定义 N/M 留的闸。
  结束    连续 end_wait_sec 没有命中 → 这一条结束，之后再来的命中算新事件。
  合并    上一条刚结束、新的命中来得比 merge_gap_sec 还近 → 接着算同一条，
          起点沿用上一条（"人出去又回来"不该在列表里变两条）。

三条不许越界的红线：

1. **unknown 不参与判定**。取帧失败、模型没加载、推理异常、排队过期这些帧
   既不喂 hit 也不喂 miss —— 把故障当"没活动"正是评审点名的那种假数据
   （`detect` 的三态在本模块之外，这里只收"这一帧有结论"的结果）。
2. **只决定记不记台账**。本模块在 `DetectWorker._one()` 的 dets 之后、
   `_write_ledger()` 之前：确认失败最多丢一条事件，绝不碰取帧、推理与录像。
3. **不是重新检测**。输入是同一批框，输出只是"这些框里哪些够格记一笔"；
   不做跟踪、不引 DeepSORT/ByteTrack —— 评审没要求，多路 CPU 也扛不住。
"""
import threading

# 与 detect.CATEGORIES 同序（test_event_confirm 里有结构守卫钉住）。
# 本模块不 import detect：确认层不该被推理引擎（numpy/onnxruntime）那条链拖下水。
CATEGORIES = ("person", "two_wheeler", "vehicle")

# 默认值怎么来的：
#   window=3 / min_hits=2 —— 5 秒节拍下"连续两帧里有一帧漏检"仍能确认，
#     而单帧误报（鸟、树影）直接被挡掉；再宽就会把真正的"一晃而过"也放进来。
#   min_span_sec=4       —— 两帧天然相隔 5 秒，这条只挡"隔很久才凑够"的情况。
#   end_wait_sec=20      —— 人走出画面到彻底判结束留 4 个节拍，避免走走停停被切碎。
#   merge_gap_sec=25     —— 比 end_wait 略大：20~25 秒之间回来的算同一件事；
#     同时小于 ledger.AI_GAP_SEC(30)，台账侧的合并不会把两条独立事件粘起来。
#   min_conf             —— 在 std 灵敏度检出阈值（0.50/0.33/0.40）上各抬一档：
#     宁可少记一条事件，也不要记一条看不清的事件。
CONFIRM_DEFAULTS = {
    "enable": True,
    "window": 3,
    "min_hits": 2,
    "min_span_sec": 4.0,
    "end_wait_sec": 20.0,
    "merge_gap_sec": 25.0,
    "min_conf": {"person": 0.55, "two_wheeler": 0.42, "vehicle": 0.48},
}

# 窗口末尾向前留 1 秒，与 detect.windows_from_hits 的 roll 同口径：
# YOLO 在低频采样下会漏一帧，留一点余量事件才不会在时间轴上出现细缝。
ROLL_SEC = 1.0

_WINDOW_LIMITS = (2, 10)
_SPAN_LIMITS = (0.0, 120.0)
_END_LIMITS = (5.0, 300.0)
_MERGE_LIMITS = (0.0, 600.0)
_CONF_LO, _CONF_HI = 0.05, 0.99


def _clamp(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                       # NaN
        return default
    return max(lo, min(hi, f))


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


def norm(cfg):
    """整块强校：夹取范围 + min_hits 不许超过 window。返回 (规范值, 修正说明)。"""
    notes = []
    src = cfg if isinstance(cfg, dict) else {}
    win = int(_clamp(src.get("window"), _WINDOW_LIMITS[0], _WINDOW_LIMITS[1],
                     CONFIRM_DEFAULTS["window"]))
    if _keep_int(src.get("window")) not in (None, win):
        notes.append("confirm.window 非法值已夹到 %d" % win)
    # min_hits 的上界是窗口：写着 5 却只有 3 帧可看，等于永远不确认（静默失效）
    hits = int(_clamp(src.get("min_hits"), 1, win, CONFIRM_DEFAULTS["min_hits"]))
    mc = src.get("min_conf") if isinstance(src.get("min_conf"), dict) else {}
    out = {
        "enable": _as_bool(src.get("enable"), CONFIRM_DEFAULTS["enable"]),
        "window": win,
        "min_hits": hits,
        "min_span_sec": round(_clamp(src.get("min_span_sec"), _SPAN_LIMITS[0],
                                     _SPAN_LIMITS[1],
                                     CONFIRM_DEFAULTS["min_span_sec"]), 1),
        "end_wait_sec": round(_clamp(src.get("end_wait_sec"), _END_LIMITS[0],
                                     _END_LIMITS[1],
                                     CONFIRM_DEFAULTS["end_wait_sec"]), 1),
        "merge_gap_sec": round(_clamp(src.get("merge_gap_sec"), _MERGE_LIMITS[0],
                                      _MERGE_LIMITS[1],
                                      CONFIRM_DEFAULTS["merge_gap_sec"]), 1),
        "min_conf": {},
    }
    for k in CATEGORIES:
        out["min_conf"][k] = round(_clamp(mc.get(k), _CONF_LO, _CONF_HI,
                                          CONFIRM_DEFAULTS["min_conf"][k]), 3)
    return out, notes


def _keep_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class _Run(object):
    """一路一个类别当前这条事件的现场。"""

    __slots__ = ("start", "last_hit", "hits", "confirmed")

    def __init__(self, now):
        self.start = now          # 这条事件的第一帧命中时刻（合并时沿用上一条的）
        self.last_hit = now
        self.hits = []            # 最近 N 帧的命中与否（True/False）
        self.confirmed = False


class Confirmer(object):
    """多路多类别的确认状态机。线程安全（工作者池里多路并行喂）。

    对外只有三件事：`update(cfg)` 换参数、`feed(cam, now, confs)` 吃一帧、
    `trim(alive)` 清掉已经不在配置里的路。`feed` 返回的窗口是
    `[(start_ts, end_ts, {类别: 1})]`，时间戳用调用方的 now 坐标系
    （epoch 秒），换算成"当天钟点秒"是调用方的事 —— 本模块不认识日期。
    """

    def __init__(self, cfg=None):
        self.lock = threading.Lock()
        self._raw = None
        self.cfg = norm(cfg)[0]
        self._raw = cfg if isinstance(cfg, dict) else None
        self.state = {}           # (cam, cat) -> _Run（在途或已确认未结束）
        self.closed = {}          # (cam, cat) -> (start, end)，留着判"相邻合并"
        self.confirmed = 0        # 累计确认过的事件数（=台账里该多出来的条数）
        self.rejected = 0         # 单帧命中但没凑够窗，被丢掉的手数

    # ---------------------------------------------------------- 配置
    @property
    def enabled(self):
        return bool(self.cfg.get("enable"))

    def update(self, cfg):
        """每拍由 ticker 喂一次配置快照。

        参数真的变了才重置在途状态：改窗口大小之后，用旧窗口攒出来的
        "已 N 帧命中 M 帧"是另一套口径的成绩，继续用会得出没人验证过的结论。
        配置变更只在用户保存设置时发生，重置代价可以忽略。
        """
        raw = cfg if isinstance(cfg, dict) else None
        with self.lock:
            if raw == self._raw and self._raw is not None:
                return
            new = norm(raw)[0]
            if new != self.cfg:
                self.state.clear()
                self.closed.clear()
            self.cfg = new
            self._raw = dict(raw) if isinstance(raw, dict) else None

    def trim(self, alive_cams):
        """清掉已不在配置里的路：删摄像头/关某路检测后，不该留下永远不结束的事件。"""
        keep = set(alive_cams or ())
        with self.lock:
            for key in [k for k in self.state if k[0] not in keep]:
                self.state.pop(key, None)
            for key in [k for k in self.closed if k[0] not in keep]:
                self.closed.pop(key, None)

    # ---------------------------------------------------------- 吃一帧
    def feed(self, cam, now, confs):
        """一帧的结论 → 该记的窗口。

        confs: {类别: 该帧该类最大置信度}；缺键=这一帧没检出该类（=miss）。
        调用方只该在**有结论**的帧上调用；取帧失败/推理异常那几帧不要调 ——
        那会把"没看成"变成"没有"，正是本模块要防的事。
        """
        try:
            now = float(now)
        except (TypeError, ValueError):
            return []
        cfg = self.cfg
        if not cfg.get("enable"):
            # 关掉确认 = 调用方退回"每帧一条"的旧口径，这里什么都不判。
            # 不是"什么都不记"——那会把整条事件链变成空的，性质完全不同。
            return []
        out = []
        with self.lock:
            for cat in CATEGORIES:
                try:
                    conf = float((confs or {}).get(cat) or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
                hit = conf >= cfg["min_conf"][cat]
                key = (cam, cat)
                run = self.state.get(key)
                if run is None:
                    if not hit:
                        continue           # 既没在途事件、这一帧也没人：什么都不做
                    run = _Run(now)
                    prev = self.closed.get(key)
                    if prev and now - prev[1] <= cfg["merge_gap_sec"]:
                        # 紧挨着上一条：接着算（起点沿用上一条，列表里仍是一条）
                        run.start = prev[0]
                        run.confirmed = True
                        self.closed.pop(key, None)
                    self.state[key] = run
                run.hits.append(hit)
                if len(run.hits) > cfg["window"]:
                    del run.hits[0]
                if hit:
                    run.last_hit = now
                if run.confirmed:
                    if hit:
                        out.append((run.start, run.last_hit + ROLL_SEC, {cat: 1}))
                    if now - run.last_hit > cfg["end_wait_sec"]:
                        self._close(key, run)
                    continue
                if now - run.last_hit > cfg["end_wait_sec"]:
                    # 在途太久没再命中：不确认，整条丢掉。单帧误报就消失在这里。
                    self.rejected += 1
                    self.state.pop(key, None)
                    continue
                if (sum(1 for h in run.hits if h) >= cfg["min_hits"]
                        and now - run.start >= cfg["min_span_sec"] - 1e-9):
                    run.confirmed = True
                    self.confirmed += 1
                    out.append((run.start, run.last_hit + ROLL_SEC, {cat: 1}))
        return out

    def _close(self, key, run):
        """结束一条：记下首尾供"相邻合并"判断（终点用最后一次命中，不是判定时刻）。"""
        self.closed[key] = (run.start, run.last_hit)
        self.state.pop(key, None)

    # ---------------------------------------------------------- 诊断
    def snapshot(self):
        """给 `/api/status` 用的事实：确认过几条、丢掉几手、现在还有几条在途。"""
        with self.lock:
            pending = sum(1 for r in self.state.values() if not r.confirmed)
            return {
                "enabled": bool(self.cfg.get("enable")),
                "window": self.cfg["window"],
                "min_hits": self.cfg["min_hits"],
                "confirmed": self.confirmed,
                "rejected": self.rejected,
                "pending": pending,
            }


def snapshot_blank():
    """没 worker 时的全 0 形状（与 detect.sched_blank 同一个理由：调用方按固定键读）。"""
    return {"enabled": False, "window": 0, "min_hits": 0,
            "confirmed": 0, "rejected": 0, "pending": 0}
