#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detect 三态与统一状态单测（0.9.122 批次 4 第 3、4 项）。

两条底线，各自都有过真实事故：
  1. **失败不等于没有**：取帧失败 / 模型没加载 / 推理异常 / 队列丢弃，都只能记
     unknown，不能记 miss。miss 是"看过画面，没人"；unknown 是"没看成"。
     把后者写成前者，等于替用户签一份他没看过的空白报告。
  2. **一个口径**：`enabled/available/running/state/error_code/error_message/
     last_success_at` 只由 status_of 判，前端只做翻译。以前状态行、诊断卡、
     接口各拼一遍，"worker 没起"在一处是"未运行"、在另一处是"加载中"。

跑：python test/test_detect_status.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "server"))
import detect  # noqa: E402

CONTRACT = ("enabled", "available", "running", "state",
            "error_code", "error_message", "last_success_at")


class StubWorker:
    """只带 status() 的替身：status_of 的判定逻辑与线程无关，不该起线程测它。"""

    def __init__(self, detail=None, boom=False):
        self.detail = detail or {}
        self.boom = boom

    def status(self):
        if self.boom:
            raise RuntimeError("读状态炸了")
        return dict(self.detail)


class TestStatusOf(unittest.TestCase):
    def test_contract_keys_always_present(self):
        """无论什么状态，七项契约字段都必须有值（前端不做存在性判断）。"""
        cases = [
            (None, {}, ""),
            (None, {"enable": True}, ""),
            (StubWorker({"alive": True, "loaded": True}), {"enable": True}, "/bin/ffmpeg"),
            (StubWorker({"alive": True, "loaded": False}), {"enable": True}, "/bin/ffmpeg"),
            (StubWorker({"error": "x", "error_code": detect.E_INFER}), {"enable": True}, "/bin/ffmpeg"),
        ]
        for w, cfg, ff in cases:
            st = detect.status_of(w, cfg, ffmpeg=ff)
            for k in CONTRACT:
                self.assertIn(k, st, "缺字段 %s" % k)

    def test_disabled_is_not_error(self):
        st = detect.status_of(StubWorker({"alive": True, "loaded": True}), {"enable": False})
        self.assertFalse(st["enabled"])
        self.assertEqual(st["state"], "disabled")
        self.assertFalse(st["available"])
        self.assertEqual(st["error_code"], "", "总闸关着不是故障，不许给错误码刷红诊断页")

    def test_no_ffmpeg(self):
        st = detect.status_of(None, {"enable": True}, ffmpeg="")
        self.assertEqual(st["state"], "unavailable")
        self.assertEqual(st["error_code"], detect.E_FFMPEG)
        self.assertFalse(st["available"])

    def test_worker_missing(self):
        """开了总闸但 worker 没起（没有 ffmpeg 以外的原因）：WORKER_DOWN。"""
        st = detect.status_of(None, {"enable": True}, ffmpeg="/bin/ffmpeg")
        self.assertEqual(st["error_code"], detect.E_WORKER)
        self.assertFalse(st["running"])
        self.assertIn("未启动", st["error_message"])

    def test_starting(self):
        """刚开启、正在解 wheel / load 模型：说"加载中"，不要报错。"""
        st = detect.status_of(StubWorker({"alive": True, "loaded": False}),
                             {"enable": True}, ffmpeg="/bin/ffmpeg")
        self.assertEqual(st["state"], "starting")
        self.assertEqual(st["error_code"], "")
        self.assertFalse(st["available"], "没加载完就不许说「可用」")

    def test_ready(self):
        st = detect.status_of(
            StubWorker({"alive": True, "loaded": True, "avg_ms": 76.0,
                        "last_success_at": 1234.5,
                        "per_cam": {"a": {"today": 3}},
                        "sched": {"workers": 2}}),
            {"enable": True}, ffmpeg="/bin/ffmpeg", runtime={"python": "3.12"})
        self.assertEqual(st["state"], "ready")
        self.assertTrue(st["available"])
        self.assertTrue(st["running"])
        self.assertEqual(st["last_success_at"], 1234.5)
        self.assertEqual(st["per_cam"]["a"]["today"], 3)
        self.assertEqual(st["sched"]["workers"], 2)
        self.assertEqual(st["runtime"]["python"], "3.12")

    def test_engine_error_wins(self):
        """引擎报错时优先报它，而不是先抱怨线程状态。"""
        st = detect.status_of(
            StubWorker({"alive": True, "loaded": False,
                        "error": "模型文件缺失：/x", "error_code": detect.E_MODEL}),
            {"enable": True}, ffmpeg="/bin/ffmpeg")
        self.assertEqual(st["error_code"], detect.E_MODEL)
        self.assertEqual(st["state"], "unavailable")

    def test_thread_dead(self):
        st = detect.status_of(StubWorker({"alive": False, "loaded": True}),
                              {"enable": True}, ffmpeg="/bin/ffmpeg")
        self.assertEqual(st["error_code"], detect.E_WORKER)
        self.assertFalse(st["running"])
        self.assertFalse(st["available"])

    def test_status_raises(self):
        st = detect.status_of(StubWorker(boom=True), {"enable": True}, ffmpeg="/bin/ffmpeg")
        self.assertEqual(st["error_code"], detect.E_STATUS)
        self.assertFalse(st["available"])

    def test_dict_cfg_missing(self):
        st = detect.status_of(None, None)
        self.assertEqual(st["state"], "disabled", "配置块缺失按「关着」处理，不猜")


class TestEngineCodes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-detect-code-")
        self.app = os.path.join(self.tmp, "app")
        self.var = os.path.join(self.tmp, "var")
        os.makedirs(self.app)
        os.makedirs(self.var)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_model_missing(self):
        eng = detect.Engine(self.app, self.var)
        ok, why = eng.ensure()
        self.assertFalse(ok)
        self.assertEqual(eng.error_code, detect.E_MODEL)
        self.assertIn("模型文件缺失", why)
        self.assertFalse(eng.loaded)

    def test_runtime_missing(self):
        """模型在、随包 wheel 不在（或与 Python 版本不匹配）：RUNTIME_FAIL，不是 MODEL_MISSING。"""
        mp = os.path.join(self.app, "share", "models")
        os.makedirs(mp)
        open(os.path.join(mp, detect.MODEL_NAME), "wb").close()
        eng = detect.Engine(self.app, self.var)
        ok, _why = eng.ensure()
        self.assertFalse(ok)
        self.assertEqual(eng.error_code, detect.E_RUNTIME,
                         "缺运行时被报成缺模型，用户会去装错东西")


class TestFrameCode(unittest.TestCase):
    def test_ffmpeg(self):
        self.assertEqual(detect.frame_error_code("ffmpeg 未找到"), detect.E_FFMPEG)
        self.assertEqual(detect.frame_error_code("ffmpeg 无法启动：权限"), detect.E_FFMPEG)

    def test_other(self):
        self.assertEqual(detect.frame_error_code("解图超时（8 秒）"), detect.E_FRAME)
        self.assertEqual(detect.frame_error_code(""), detect.E_FRAME)
        self.assertEqual(detect.frame_error_code(None), detect.E_FRAME)


class TestThreeState(unittest.TestCase):
    """三态的记账口径：hit/miss 才刷新"看见画面"，unknown/skip 都不算。"""

    def setUp(self):
        self.w = detect.DetectWorker(None, None, "", lambda: "", lambda: [],
                                     lambda: ({}, []))
        self.today = time.strftime("%Y-%m-%d")

    def _cam(self):
        return self.w.status()["per_cam"]["c1"]

    def test_hit_and_miss(self):
        self.w._ok_stat("c1", [{"c": "person", "b": [0.5, 0.5, 0.1, 0.1]}])
        self.w._ok_stat("c1", [])
        st = self._cam()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["today"], 2)
        self.assertEqual(st["cls"], {"person": 1})
        self.assertEqual(st["last_state"], detect.MISS)
        self.assertEqual(st["unknown"], 0)
        self.assertIsNotNone(st["last"])

    def test_unknown_not_counted_as_miss(self):
        self.w._unknown("c1", detect.E_FRAME, "解图超时")
        st = self._cam()
        self.assertEqual(st["unknown"], 1)
        self.assertEqual(st["today"], 0, "unknown 不许进「有结论的帧数」")
        self.assertEqual(st["last_state"], detect.UNKNOWN)
        self.assertEqual(st["last_code"], detect.E_FRAME)
        self.assertIn("超时", st["last_err"])

    def test_unknown_does_not_touch_last_success(self):
        self.w._ok_stat("c1", [])
        ok_at = self.w.last_ok_at
        self.assertIsNotNone(ok_at)
        time.sleep(0.01)
        self.w._unknown("c1", detect.E_INFER, "推理失败")
        self.assertEqual(self.w.last_ok_at, ok_at,
                         "last_success_at 回答的是「最后一次真看见画面」，报错不许刷新它")

    def test_codes_accumulate(self):
        self.w._unknown("c1", detect.E_FRAME, "a")
        self.w._unknown("c1", detect.E_FRAME, "b")
        self.w._unknown("c1", detect.E_STALE, "c")
        st = self._cam()
        self.assertEqual(st["codes"][detect.E_FRAME], 2)
        self.assertEqual(st["codes"][detect.E_STALE], 1)
        self.assertEqual(st["unknown"], 3)

    def test_skip_is_third_kind(self):
        """取帧层没有新画面：既不是 hit/miss，也不是故障，单独一档，不刷红。"""
        self.w._skip("c1")
        st = self._cam()
        self.assertEqual(st["skip"], 1)
        self.assertEqual(st["unknown"], 0)
        self.assertEqual(st["today"], 0)
        self.assertIsNone(st["last_err"], "skip 不是错误，不该留下错误串")

    def test_day_rollover(self):
        """worker 是长期存在的：_day 定死在构造时，"今日"在次日会一直读旧键。"""
        self.w._day = "2020-01-01"
        self.w._ok_stat("c1", [])
        self.assertEqual(self.w._day, self.today)
        self.assertEqual(self._cam()["today"], 1)

    def test_engine_error_cleared_on_success(self):
        self.w.stats["_engine_err"] = "engine|模型文件缺失"
        self.w._ok_stat("c1", [])
        self.assertNotIn("_engine_err", self.w.stats,
                         "成功一拍后要允许下次故障重新记日志")

    def test_status_without_engine(self):
        """engine 为 None（构造替身 / 早期启动）时 status() 不能抛。"""
        st = self.w.status()
        self.assertFalse(st["loaded"])
        self.assertEqual(st["error"], "")
        self.assertEqual(st["error_code"], "")
        self.assertIn("sched", st)
        for k in ("workers", "interval", "queued", "inflight",
                  "dropped_full", "dropped_stale", "round_ms"):
            self.assertIn(k, st["sched"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
