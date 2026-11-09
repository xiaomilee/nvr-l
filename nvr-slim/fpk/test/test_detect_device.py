#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推理设备：自动探测与回落（0.9.125 批次 7）。

要治的是什么：`detect.device` 这个键从很早就躺在 `DETECT_DEFAULTS` /
`DETECT_ENUMS` 里，界面也把它当设置项，但 `Engine` 一直无条件
`providers=["CPUExecutionProvider"]`，诊断卡标题还硬写着「YOLO11n · CPU」。
用户把它改成 gpu —— 接口回 ok、配置也落盘，实际仍在 CPU 上跑。**这是界面
在说一件没验证过的事**，比"没有 GPU 支持"更伤信任。

四条口径，每条都有对应的错误做法：
  1. 先问后选：只认 onnxruntime 自己报的可用后端。不按机器名猜、不看
     `nvidia-smi` —— 猜错的形态是"配了 GPU 却一直静默回落"，用户永远发现不了。
  2. 回落不算故障：GPU 建会话失败（驱动 / CUDA 版本 / 显存）就用 CPU 再建一次，
     `state` 仍是 ready；原因写进 `device.fallback_reason`，界面能说出为什么。
  3. 显式选 cpu 不亮黄：那是用户要的结果，不是降级。
  4. 未知就是未知：模型还没加载时 `actual` 留空，**不拿配置值冒充实际后端**。

GPU 真机难复现（要有核显 + 驱动 + 匹配的 CUDA wheel），所以把"建会话失败"
做成可注入的数据：`ort_loader` 参数就是为这条路径留的，回落逻辑才有可能被闸门
守住，而不是等某台机器上才发现。

跑：python test/test_detect_device.py
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "server"))
import detect  # noqa: E402

CPU = "CPUExecutionProvider"
CUDA = "CUDAExecutionProvider"
TRT = "TensorrtExecutionProvider"
OV = "OpenVINOExecutionProvider"    # 故意不在 GPU 名单里：它自己也能跑 CPU


class FakeSession:
    def get_inputs(self):
        return [types.SimpleNamespace(name="images")]

    def get_outputs(self):
        return [types.SimpleNamespace(name="output0")]


class FakeORT:
    """假 onnxruntime：只带 Engine 用到的那几个入口。"""

    def __init__(self, available, fail_on=(), raises=False):
        self._avail = list(available)
        self._fail_on = set(fail_on)
        self.raises = raises
        self.calls = []                 # 每次建会话用的 providers，供断言

    def get_available_providers(self):
        if self.raises:
            raise RuntimeError("读不到后端列表")
        return list(self._avail)

    def SessionOptions(self):
        return types.SimpleNamespace()

    def InferenceSession(self, mp, opt, providers=None):
        providers = list(providers or [])
        self.calls.append(providers)
        if providers and providers[0] in self._fail_on:
            raise RuntimeError("%s 初始化失败：libcudnn.so.9 not found" % providers[0])
        return FakeSession()


class TestPickProviders(unittest.TestCase):
    """纯函数：意图 + 可用后端 → 用哪几个、实际落在什么设备、为什么回落。"""

    def test_auto_without_gpu_is_cpu_and_not_a_fallback(self):
        """auto 挑不到 GPU 是正常结果，不该报"回落"（否则每台机器开局就亮黄）。"""
        ps, act, why = detect.pick_providers("auto", [CPU])
        self.assertEqual(ps, [CPU])
        self.assertEqual(act, "cpu")
        self.assertEqual(why, "")

    def test_auto_picks_cuda_when_available(self):
        ps, act, why = detect.pick_providers("auto", [CPU, CUDA])
        self.assertEqual(ps[0], CUDA)
        self.assertEqual(ps[-1], CPU)      # CPU 永远垫底：单算子不支持也不整帧失败
        self.assertEqual(act, "cuda")
        self.assertEqual(why, "")

    def test_gpu_request_without_backend_reports_reason(self):
        ps, act, why = detect.pick_providers("gpu", [CPU, OV])
        self.assertEqual(ps, [CPU])
        self.assertEqual(act, "cpu")
        self.assertIn("请求 GPU", why)
        self.assertIn("CUDA", why)         # 原因里要能看出"缺的是哪个后端"
        self.assertIn("CPUExecutionProvider", why)   # 也要说清现在有什么

    def test_explicit_cpu_is_not_a_downgrade(self):
        """用户显式选 cpu 时，机器上有 CUDA 也不许挑，更不许报回落。"""
        ps, act, why = detect.pick_providers("cpu", [CPU, CUDA, OV])
        self.assertEqual(ps, [CPU])
        self.assertEqual(act, "cpu")
        self.assertEqual(why, "")

    def test_cuda_wins_over_tensorrt(self):
        """两个都在时优先 CUDA：TensorRT 要现编 engine，首次加载更脆。"""
        ps, act, _ = detect.pick_providers("gpu", [CPU, TRT, CUDA])
        self.assertEqual(ps[0], CUDA)
        self.assertEqual(act, "cuda")

    def test_illegal_device_falls_back_to_auto(self):
        """与 norm_detect 同口径：写错一个词不该让检测起不来。"""
        ps, act, why = detect.pick_providers("CUDA", [CPU])
        self.assertEqual(ps, [CPU])
        self.assertEqual(act, "cpu")
        self.assertEqual(why, "")
        # 大小写/空格容错
        self.assertEqual(detect.pick_providers(" GPU ", [CPU, CUDA])[1], "cuda")

    def test_cpu_provider_always_appended(self):
        """onnxruntime 理论上一定有 CPU 后端；真缺了也补上，不做"整帧失败"。"""
        ps, act, _ = detect.pick_providers("cpu", [])
        self.assertEqual(ps, [CPU])
        self.assertEqual(act, "cpu")

    def test_providers_list_ends_with_cpu(self):
        ps, _, _ = detect.pick_providers("auto", [CPU, CUDA, OV])
        self.assertEqual(ps[-1], CPU)
        self.assertEqual(ps.count(CPU), 1)


class TestProbeProviders(unittest.TestCase):
    def test_normal(self):
        avail, why = detect.probe_providers(FakeORT([CUDA, CPU]))
        self.assertEqual(avail, [CUDA, CPU])
        self.assertEqual(why, "")

    def test_missing_cpu_is_added(self):
        avail, _ = detect.probe_providers(FakeORT([CUDA]))
        self.assertIn(CPU, avail)

    def test_read_failure_does_not_raise(self):
        """读不到后端列表时退回 CPU 并给一句话，绝不抛 —— 抛了就是整机检测不可用。"""
        avail, why = detect.probe_providers(FakeORT([], raises=True))
        self.assertEqual(avail, [CPU])
        self.assertIn("失败", why)

    def test_old_module_without_property(self):
        avail, why = detect.probe_providers(object())
        self.assertEqual(avail, [CPU])
        self.assertIn("失败", why)


class TestEngineFallback(unittest.TestCase):
    """Engine 的回落路径。真机难造，用假 ort 注入。"""

    def _engine(self, device, ort):
        return detect.Engine("/nonexistent", tempfile.gettempdir(),
                             device=device, ort_loader=lambda: ort)

    def test_gpu_session_failure_falls_back_to_cpu(self):
        ort = FakeORT([CPU, CUDA], fail_on=(CUDA,))
        eng = self._engine("gpu", ort)
        sess = eng._open("fake.onnx")
        self.assertIsNotNone(sess)
        self.assertEqual(eng.device_actual, "cpu")
        self.assertEqual(eng.providers, [CPU])
        self.assertIn(CUDA, eng.device_fallback)     # 原因要能追到具体后端
        self.assertIn("libcudnn", eng.device_fallback)
        self.assertEqual(len(ort.calls), 2)          # 先 GPU 再 CPU，各一次
        self.assertEqual(ort.calls[0][0], CUDA)
        self.assertEqual(ort.calls[1], [CPU])

    def test_both_fail_raises_to_model_load_fail(self):
        """两次都建不起来才是真失败：此时不许假装 loaded。"""
        ort = FakeORT([CPU, CUDA], fail_on=(CUDA, CPU))
        eng = self._engine("gpu", ort)
        with self.assertRaises(Exception):
            eng._open("fake.onnx")
        self.assertFalse(eng.loaded)
        self.assertEqual(eng.device_actual, "")       # 没成就留空，不写"cpu"

    def test_cpu_failure_does_not_retry(self):
        """本来就是 CPU，没有可回落的东西 —— 只该试一次，别把同一句错重放两遍。"""
        ort = FakeORT([CPU], fail_on=(CPU,))
        eng = self._engine("cpu", ort)
        with self.assertRaises(Exception):
            eng._open("fake.onnx")
        self.assertEqual(len(ort.calls), 1)

    def test_device_status_before_load_is_blank(self):
        eng = self._engine("gpu", FakeORT([CPU]))
        st = eng.device_status()
        self.assertEqual(st["requested"], "gpu")
        self.assertEqual(st["actual"], "")           # 未知就是未知
        self.assertEqual(st["providers"], [])
        self.assertEqual(st["available"], [])
        self.assertEqual(set(st), set(detect.device_blank()))

    def test_device_status_after_load(self):
        eng = self._engine("auto", FakeORT([CPU, CUDA]))
        eng._open("fake.onnx")
        st = eng.device_status()
        self.assertEqual(st["requested"], "auto")
        self.assertEqual(st["actual"], "cuda")
        self.assertEqual(st["providers"][0], CUDA)
        self.assertIn(CUDA, st["available"])
        self.assertEqual(st["fallback_reason"], "")

    def test_ensure_uses_open_and_reports_load_fail(self):
        """ensure() 的失败码要还是 MODEL_LOAD_FAIL —— 回落逻辑不能改坏既有口径。"""
        old_mp, old_rt = detect.model_path, detect.ensure_runtime
        detect.model_path = lambda app: __file__
        detect.ensure_runtime = lambda app, var, log: (True, "")
        try:
            eng = self._engine("gpu", FakeORT([CPU, CUDA], fail_on=(CUDA, CPU)))
            ok, why = eng.ensure()
            self.assertFalse(ok)
            self.assertEqual(eng.error_code, detect.E_MODEL_LOAD)
            self.assertIn("模型加载失败", why)
        finally:
            detect.model_path, detect.ensure_runtime = old_mp, old_rt

    def test_ensure_success_sets_session(self):
        old_mp, old_rt = detect.model_path, detect.ensure_runtime
        detect.model_path = lambda app: __file__
        detect.ensure_runtime = lambda app, var, log: (True, "")
        try:
            eng = self._engine("auto", FakeORT([CPU, CUDA]))
            ok, why = eng.ensure()
            self.assertTrue(ok, why)
            self.assertTrue(eng.loaded)
            self.assertEqual(eng.device_actual, "cuda")
            self.assertEqual(eng.error_code, "")
        finally:
            detect.model_path, detect.ensure_runtime = old_mp, old_rt


class StubWorker:
    def __init__(self, detail):
        self.detail = detail

    def status(self):
        return dict(self.detail)


class TestStatusSurfacesDevice(unittest.TestCase):
    """状态接口要带得出去：界面没有这个字段就只能继续硬写 CPU。"""

    def test_status_of_without_worker_still_says_what_was_requested(self):
        st = detect.status_of(None, {"enable": True, "device": "gpu"},
                              ffmpeg="/bin/ffmpeg")
        self.assertEqual(st["device"]["requested"], "gpu")
        self.assertEqual(st["device"]["actual"], "")
        self.assertEqual(st["device"]["providers"], [])

    def test_status_of_passes_engine_block_through(self):
        dev = {"requested": "gpu", "actual": "cpu", "providers": [CPU],
               "available": [CPU], "fallback_reason": "CUDAExecutionProvider 建会话失败"}
        w = StubWorker({"alive": True, "loaded": True, "device": dev,
                        "per_cam": {}, "sched": {}, "confirm": {}})
        st = detect.status_of(w, {"enable": True}, ffmpeg="/bin/ffmpeg")
        self.assertEqual(st["state"], "ready")        # 回落不是不可用
        self.assertEqual(st["device"], dev)

    def test_worker_status_carries_device(self):
        eng = detect.Engine("/nonexistent", tempfile.gettempdir(), device="gpu")
        w = detect.DetectWorker(eng, "/bin/ffmpeg", tempfile.gettempdir(),
                                lambda: "/tmp", lambda: [],
                                lambda: ({"enable": True}, []))
        self.assertEqual(w.status()["device"]["requested"], "gpu")
        self.assertEqual(w.status()["device"]["actual"], "")

    def test_stub_engine_without_device_status_is_tolerated(self):
        """只实现 loaded/infer 的替身引擎（或升级中的旧对象）不能把状态接口打死。

        这条不是为测试而测试：`/api/status` 一 500，整个诊断页与每路状态都没了，
        代价远大于少一个字段。"""
        class FakeEngine:
            loaded = False
            error = ""
            error_code = ""
            avg_ms = 0

        w = detect.DetectWorker(FakeEngine(), "/bin/ffmpeg", tempfile.gettempdir(),
                                lambda: "/tmp", lambda: [],
                                lambda: ({"enable": True}, []))
        self.assertEqual(w.status()["device"], detect.device_blank())


class TestNormKeepsDevice(unittest.TestCase):
    """这个键能被强校，才谈得上"用户改得动"；写错也不该炸。"""

    def test_valid_values_kept(self):
        for v in ("auto", "cpu", "gpu"):
            out, _ = detect.norm_detect({"device": v})
            self.assertEqual(out["device"], v)

    def test_illegal_value_falls_back_with_note(self):
        out, notes = detect.norm_detect({"device": "cuda"})
        self.assertEqual(out["device"], "auto")
        self.assertTrue(any("device" in n for n in notes))

    def test_case_and_space_tolerated(self):
        out, _ = detect.norm_detect({"device": " GPU "})
        self.assertEqual(out["device"], "gpu")


if __name__ == "__main__":
    unittest.main(verbosity=2)
