#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detect.py 单测：配置强校 / 每路覆盖 / 触发闸 / 几何与后处理数学。

不加载模型、不起 ffmpeg——纯函数部分全在这里覆盖；
Engine/DetectWorker 依赖 onnxruntime 与真机 RTSP，留给真机验证。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "server"))
import detect  # noqa: E402


class TestNorm(unittest.TestCase):
    def test_defaults(self):
        v, notes = detect.norm_detect({})
        self.assertFalse(v["enable"], "默认必须关闭（opt-in）")
        self.assertEqual(v["mode"], "snap")
        self.assertEqual(v["device"], "auto")
        self.assertTrue(all(v["classes"].values()))
        self.assertEqual(v["trigger"],
                         {"motion": True, "person": True, "vehicle": True})
        self.assertEqual(notes, [])

    def test_detect_not_dict(self):
        v, notes = detect.norm_detect(None)
        self.assertFalse(v["enable"])
        self.assertEqual(notes, [])

    def test_enum_whitelist(self):
        v, notes = detect.norm_detect({"mode": "LIVE!!!", "device": "npu", "night": "day"})
        self.assertEqual(v["mode"], "snap")
        self.assertEqual(v["device"], "auto")
        self.assertEqual(v["night"], "throttle")
        self.assertEqual(len(notes), 3, "三个非法 enum 都要记修正说明")

    def test_enum_blank_is_not_note(self):
        # 空串 / None 是"没填"，不算非法值，不该刷修正日志
        _, notes = detect.norm_detect({"mode": "", "device": None})
        self.assertEqual(notes, [])

    def test_trigger_all_off_illegal(self):
        v, notes = detect.norm_detect({"trigger": {"motion": False, "person": False, "vehicle": False}})
        self.assertEqual(v["trigger"], {"motion": True, "person": True, "vehicle": True})
        self.assertTrue(any("trigger" in n for n in notes))

    def test_trigger_partial_kept(self):
        v, notes = detect.norm_detect({"trigger": {"motion": False, "person": True, "vehicle": False}})
        self.assertEqual(v["trigger"], {"motion": False, "person": True, "vehicle": False})
        self.assertEqual(notes, [])

    def test_live_fps_clamped(self):
        v, _ = detect.norm_detect({"live_fps": 999})
        self.assertEqual(v["live_fps"], 10)
        v, _ = detect.norm_detect({"live_fps": -3})
        self.assertEqual(v["live_fps"], 1)
        v, _ = detect.norm_detect({"live_fps": "abc"})
        self.assertEqual(v["live_fps"], 5)

    def test_conf_clamped(self):
        v, _ = detect.norm_detect({"conf": {"person": 5.0, "two_wheeler": -1, "vehicle": "0.4"}})
        self.assertLessEqual(v["conf"]["person"], detect.CONF_HI)
        self.assertGreaterEqual(v["conf"]["two_wheeler"], detect.CONF_LO)
        self.assertAlmostEqual(v["conf"]["vehicle"], 0.4, places=3)

    def test_bool_coercion(self):
        v, _ = detect.norm_detect({"enable": "true", "classes": {"person": 1, "vehicle": "off"}})
        self.assertTrue(v["enable"])
        self.assertTrue(v["classes"]["person"])
        self.assertFalse(v["classes"]["vehicle"])

    def test_notify_norm(self):
        v = detect.norm_notify({"webhook": "  https://x/y  ", "cooldown": 5, "person": "yes"})
        self.assertEqual(v["webhook"], "https://x/y")
        self.assertEqual(v["cooldown"], 30, "冷却下限 30 秒")
        self.assertTrue(v["person"])
        self.assertFalse(v["motion"])
        v = detect.norm_notify({"cooldown": 99999})
        self.assertEqual(v["cooldown"], 600)
        v = detect.norm_notify(None)
        self.assertEqual(v["webhook"], "")
        self.assertEqual(v["cooldown"], 60)

    def test_notify_webhook_truncated(self):
        v = detect.norm_notify({"webhook": "x" * 900})
        self.assertEqual(len(v["webhook"]), 500)

    def test_sensitivity_presets(self):
        for s in ("high", "std", "low"):
            self.assertIn(s, detect.CONF_PRESETS)
        self.assertEqual(detect.conf_from_sensitivity("bogus"),
                         detect.conf_from_sensitivity("std"))
        # 两轮车阈值必须系统性低于车辆（小目标换召回）
        for s, m in detect.CONF_PRESETS.items():
            self.assertLess(m["two_wheeler"], m["vehicle"], "%s 档两轮车应更宽松" % s)


class TestPerCam(unittest.TestCase):
    GLOBAL_CLASSES = {"person": True, "two_wheeler": True, "vehicle": True}

    def test_global_off_forces_off(self):
        self.assertEqual(detect.cam_detect_mode({"detect": "on"}, False), "off")
        self.assertFalse(detect.cam_detect_on({"detect": "on"}, False))

    def test_mode_whitelist(self):
        self.assertEqual(detect.cam_detect_mode({"detect": "ON"}, True), "on")
        self.assertEqual(detect.cam_detect_mode({"detect": "garbage"}, True), "follow")
        self.assertEqual(detect.cam_detect_mode({}, True), "follow")
        self.assertTrue(detect.cam_detect_on({"detect": "follow"}, True))
        self.assertFalse(detect.cam_detect_on({"detect": "off"}, True))

    def test_cam_classes_follow(self):
        self.assertEqual(detect.cam_classes({}, self.GLOBAL_CLASSES), self.GLOBAL_CLASSES)

    def test_cam_classes_missing_key_is_false(self):
        """院子只勾「人员+车辆」→ 没勾的两轮车必须是 False，不许补全局值。"""
        got = detect.cam_classes({"detect_classes": {"person": True, "vehicle": True}},
                                 self.GLOBAL_CLASSES)
        self.assertEqual(got, {"person": True, "two_wheeler": False, "vehicle": True})

    def test_cam_classes_all_off_is_legal(self):
        got = detect.cam_classes({"detect_classes": {"person": False, "two_wheeler": False,
                                                     "vehicle": False}},
                                 self.GLOBAL_CLASSES)
        self.assertEqual(got, {"person": False, "two_wheeler": False, "vehicle": False})

    def test_cam_classes_empty_dict_falls_back(self):
        self.assertEqual(detect.cam_classes({"detect_classes": {}}, self.GLOBAL_CLASSES),
                         self.GLOBAL_CLASSES)

    def test_norm_cam_classes_partial(self):
        got = detect.norm_cam_classes({"person": True}, self.GLOBAL_CLASSES)
        self.assertEqual(got, {"person": True, "two_wheeler": False, "vehicle": False})
        got = detect.norm_cam_classes(None, {"person": True, "two_wheeler": False,
                                             "vehicle": False})
        self.assertEqual(got, {"person": True, "two_wheeler": False, "vehicle": False})


class TestTriggerGate(unittest.TestCase):
    ALL = {"motion": True, "person": True, "vehicle": True}

    def test_frame_diff_window_needs_motion(self):
        self.assertTrue(detect.trigger_ok(None, {"motion": True, "person": False, "vehicle": False}))
        self.assertFalse(detect.trigger_ok(None, {"motion": False, "person": True, "vehicle": True}))

    def test_person(self):
        self.assertTrue(detect.trigger_ok({"person": 1}, self.ALL))
        self.assertFalse(detect.trigger_ok({"person": 1}, {"motion": True, "person": False,
                                                           "vehicle": False}))

    def test_two_wheeler_counts_as_vehicle(self):
        """回归：两轮车计数不在 vehicle 键下，早期版本漏判成静止画面。"""
        self.assertTrue(detect.trigger_ok({"two_wheeler": 1},
                                          {"motion": False, "person": False, "vehicle": True}))

    def test_vehicle(self):
        self.assertTrue(detect.trigger_ok({"vehicle": 2}, self.ALL))
        self.assertFalse(detect.trigger_ok({"vehicle": 2},
                                           {"motion": False, "person": False, "vehicle": False}))

    def test_unchecked_category_is_silent(self):
        # 勾了人+车但没勾移动侦测：AI 窗口仍算活动，帧差窗口全静止
        t = {"motion": False, "person": True, "vehicle": True}
        self.assertTrue(detect.trigger_ok({"person": 1}, t))
        self.assertFalse(detect.trigger_ok(None, t))

    def test_empty_counts(self):
        self.assertFalse(detect.trigger_ok({}, self.ALL))
        self.assertFalse(detect.trigger_ok({"person": 0}, self.ALL))

    def test_hit_categories(self):
        self.assertEqual(detect.cls_hit_categories({"person": 1, "vehicle": 2}),
                         {"person", "vehicle"})
        self.assertEqual(detect.cls_hit_categories(None), set())


class TestIgnoreArea(unittest.TestCase):
    def test_center_inside(self):
        box = (0.4, 0.4, 0.2, 0.2)          # 中心 (0.5, 0.5)
        self.assertTrue(detect.in_ignore(box, [(0.3, 0.3, 0.4, 0.4)]))

    def test_center_outside(self):
        box = (0.0, 0.0, 0.1, 0.1)          # 中心 (0.05, 0.05)
        self.assertFalse(detect.in_ignore(box, [(0.3, 0.3, 0.4, 0.4)]))

    def test_edge_counts_as_inside(self):
        box = (0.0, 0.0, 0.6, 0.6)          # 中心正好压在矩形边上 (0.3,0.3)
        self.assertTrue(detect.in_ignore(box, [(0.3, 0.3, 0.4, 0.4)]))

    def test_degenerate_rect_skipped(self):
        box = (0.4, 0.4, 0.2, 0.2)
        self.assertFalse(detect.in_ignore(box, [(0.5, 0.5, 0.0, 0.0)]))
        self.assertFalse(detect.in_ignore(box, [("bad", 0, 1, 1)]))

    def test_filter_boxes(self):
        boxes = [(0.4, 0.4, 0.2, 0.2), (0.0, 0.0, 0.1, 0.1)]
        got = detect.filter_boxes(boxes, [(0.3, 0.3, 0.4, 0.4)])
        self.assertEqual(got, [(0.0, 0.0, 0.1, 0.1)])


class TestPpm(unittest.TestCase):
    def _ppm(self, w, h, maxv=255):
        return b"P6\n%d %d\n%d\n" % (w, h, maxv) + b"\x01\x02\x03" * (w * h)

    def test_basic(self):
        data = self._ppm(2, 2)
        got = detect.parse_ppm(data)
        self.assertIsNotNone(got)
        w, h, body = got
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(len(body), 2 * 2 * 3)

    def test_only_first_frame(self):
        data = self._ppm(2, 2) + self._ppm(2, 2)
        w, h, body = detect.parse_ppm(data)
        self.assertEqual(len(body), 12, "不许把第二帧的字节吃进来")

    def test_truncated(self):
        self.assertIsNone(detect.parse_ppm(b"P6\n8 8\n255\n" + b"\x00" * 10))

    def test_bad_magic(self):
        self.assertIsNone(detect.parse_ppm(b"P5\n2 2\n255\n" + b"\x00" * 12))
        self.assertIsNone(detect.parse_ppm(b""))
        self.assertIsNone(detect.parse_ppm(b"P6"))

    def test_bad_maxv(self):
        self.assertIsNone(detect.parse_ppm(b"P6\n2 2\n65535\n" + b"\x00" * 24))

    def test_absurd_dims(self):
        self.assertIsNone(detect.parse_ppm(b"P6\n99999 2\n255\n" + b"\x00" * 24))


class TestMath(unittest.TestCase):
    def setUp(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("本机没有 numpy（NAS 运行时随包提供）")

    def test_letterbox_shape_and_range(self):
        import numpy as np
        w, h = 640, 360
        rgb = bytes(range(256)) * (w * h * 3 // 256 + 1)
        rgb = rgb[:w * h * 3]
        out = detect.letterbox(rgb, w, h)
        self.assertEqual(out.shape, (1, 3, 640, 640))
        self.assertTrue(out.dtype == np.float32)
        self.assertLessEqual(float(out.max()), 1.0)
        self.assertGreaterEqual(float(out.min()), 0.0)

    def test_letterbox_pads_gray(self):
        import numpy as np
        w, h = 640, 360                    # 竖条：上下出灰边，oy=(640-360)//2=140
        out = detect.letterbox(b"\x80" * (w * h * 3), w, h)
        top = float(out[0, 0, 10, 320])    # 第 10 行是灰边区
        mid = float(out[0, 0, 320, 320])   # 第 320 行是画面区
        self.assertAlmostEqual(top, 114 / 255.0, places=5)
        self.assertAlmostEqual(mid, 128 / 255.0, places=5)

    def test_nms_suppresses_overlap(self):
        boxes = [[0.5, 0.5, 0.2, 0.2], [0.51, 0.5, 0.2, 0.2], [0.1, 0.1, 0.1, 0.1]]
        scores = [0.9, 0.8, 0.7]
        keep = detect.nms(boxes, scores, iou_thr=0.45)
        self.assertIn(0, keep)
        self.assertNotIn(1, keep, "IoU 高的重复框应被抑制")
        self.assertIn(2, keep)

    def test_nms_keeps_disjoint(self):
        keep = detect.nms([[0.1, 0.1, 0.1, 0.1], [0.9, 0.9, 0.1, 0.1]], [0.9, 0.8])
        self.assertEqual(sorted(keep), [0, 1])

    def test_nms_empty(self):
        self.assertEqual(detect.nms([], []), [])

    def test_postprocess_single_person(self):
        import numpy as np
        out = np.zeros((1, 84, 3), dtype=np.float32)
        # 候选 0：person 高分；候选 1：car 高分；候选 2：全 0
        out[0, 4 + 0, 0] = 0.90           # COCO 0 = person
        out[0, 4 + 2, 1] = 0.70           # COCO 2 = car
        out[0, 0, 0], out[0, 1, 0], out[0, 2, 0], out[0, 3, 0] = 320, 320, 64, 64
        out[0, 0, 1], out[0, 1, 1], out[0, 2, 1], out[0, 3, 1] = 160, 160, 32, 32
        conf_map = {"person": 0.45, "two_wheeler": 0.28, "vehicle": 0.35}
        dets = detect.postprocess(out, 640, 360, conf_map)
        cats = [d["c"] for d in dets]
        self.assertEqual(cats, ["person", "vehicle"])
        p = [d for d in dets if d["c"] == "person"][0]
        # 640×360 进 640 画布：scale=1.0，纵向灰边 oy=(640-360)//2=140。
        # 画布中心行 320 → 原图行 320-140=180 → 归一化 0.5（不是 0.888）
        self.assertAlmostEqual(p["b"][0], 0.5, places=3)
        self.assertAlmostEqual(p["b"][1], 0.5, places=3)
        self.assertAlmostEqual(p["b"][2], 0.1, places=3)
        self.assertAlmostEqual(p["b"][3], 64 / 360.0, places=3)

    def test_postprocess_letterbox_roundtrip(self):
        """回归：非 16:9 画面的框必须按 letterbox 逆运算回原图坐标。
        旧实现直接除以 640，4:3 画面会把所有框压到画布中段，
        忽略区（按归一化坐标比对方框中心）整体失配。"""
        import numpy as np
        out = np.zeros((1, 84, 1), dtype=np.float32)
        out[0, 4 + 0, 0] = 0.9
        # 原图 640×480 的人站在画面正中心 (320,240)；
        # letterbox 后落在画布 (320, 240+80)=(320,320)
        out[0, 0, 0], out[0, 1, 0], out[0, 2, 0], out[0, 3, 0] = 320, 320, 64, 48
        dets = detect.postprocess(out, 640, 480, {"person": 0.45, "two_wheeler": 0.28,
                                                  "vehicle": 0.35})
        self.assertEqual(len(dets), 1)
        self.assertAlmostEqual(dets[0]["b"][0], 0.5, places=3)
        self.assertAlmostEqual(dets[0]["b"][1], 0.5, places=3)
        self.assertAlmostEqual(dets[0]["b"][2], 0.1, places=3)
        self.assertAlmostEqual(dets[0]["b"][3], 48 / 480.0, places=3)

    def test_postprocess_top_corner_box(self):
        """画面顶部 10% 处的框，归一化 y 必须≈0.1（旧实现会算成 ~0.33）。"""
        import numpy as np
        out = np.zeros((1, 84, 1), dtype=np.float32)
        out[0, 4 + 0, 0] = 0.9
        # 原图 640×360，y=36（顶部 10%）→ 画布 y=36+140=176
        out[0, 0, 0], out[0, 1, 0], out[0, 2, 0], out[0, 3, 0] = 320, 176, 40, 40
        dets = detect.postprocess(out, 640, 360, {"person": 0.45, "two_wheeler": 0.28,
                                                  "vehicle": 0.35})
        self.assertAlmostEqual(dets[0]["b"][1], 0.1, places=2)

    def test_postprocess_transposed_layout(self):
        """防御性分支：导出成 (8400,84) 也要能解。"""
        import numpy as np
        out = np.zeros((3, 84), dtype=np.float32)
        out[0, 4 + 0] = 0.9
        out[0, 0], out[0, 1], out[0, 2], out[0, 3] = 100, 100, 50, 50
        dets = detect.postprocess(out, 320, 320, {"person": 0.45, "two_wheeler": 0.28,
                                                  "vehicle": 0.35})
        self.assertEqual([d["c"] for d in dets], ["person"])

    def test_postprocess_below_threshold(self):
        import numpy as np
        out = np.zeros((1, 84, 1), dtype=np.float32)
        out[0, 4 + 0, 0] = 0.10
        dets = detect.postprocess(out, 640, 360, {"person": 0.45, "two_wheeler": 0.28,
                                                  "vehicle": 0.35})
        self.assertEqual(dets, [])

    def test_postprocess_ignores_unmapped_coco(self):
        """COCO 里没有映射的类（如 9=traffic light）不该出现在结果里。"""
        import numpy as np
        out = np.zeros((1, 84, 1), dtype=np.float32)
        out[0, 4 + 9, 0] = 0.99
        dets = detect.postprocess(out, 640, 360, {"person": 0.45, "two_wheeler": 0.28,
                                                  "vehicle": 0.35})
        self.assertEqual(dets, [])


class TestWindows(unittest.TestCase):
    def test_windows_from_hits(self):
        wins = detect.windows_from_hits([(10.0, "person"), (10.0, "person"), (12.0, "vehicle")],
                                        roll=1.0)
        self.assertEqual(len(wins), 2)
        s, e, cls = wins[0]
        self.assertEqual((s, e), (9.0, 11.0))
        self.assertEqual(cls, {"person": 1}, "同一时刻同类多次只计一次")
        self.assertEqual(wins[1][2], {"vehicle": 1})

    def test_windows_unknown_category_dropped(self):
        wins = detect.windows_from_hits([(5.0, "dog")])
        self.assertEqual(wins, [])

    def test_windows_clamped_at_zero(self):
        wins = detect.windows_from_hits([(0.2, "person")], roll=1.0)
        self.assertEqual(wins[0][0], 0.0)

    def test_merge_cls_takes_max(self):
        a = {"person": 2, "vehicle": 1}
        b = {"person": 1, "vehicle": 5}
        self.assertEqual(detect.merge_cls(a, b), {"person": 2, "vehicle": 5})
        self.assertEqual(detect.merge_cls(None, b), b)


class TestRuntimeWheels(unittest.TestCase):
    """ensure_runtime 的解包路径。回归：wheels 列表漏初始化 → 首次装载必失败。"""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="nvrtest_ort_")
        self.app = os.path.join(self.tmp, "app")
        self.var = os.path.join(self.tmp, "var")
        os.makedirs(os.path.join(self.app, "vendor"))
        os.makedirs(self.var)
        self._shutil = shutil
        self._saved_path = list(sys.path)

    def tearDown(self):
        sys.path[:] = self._saved_path
        self._shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_wheel(self, mod_name):
        """造一个只含一个纯 Python 桩模块的假 wheel（zip）。"""
        import zipfile
        v = detect.sys_version_info()
        np_name = ("numpy-2.1.3-cp%d%d.whl" if v >= (3, 13)
                   else "numpy-1.26.4-cp%d%d.whl") % v
        name = ("onnxruntime-1.20.1-cp%d%d.whl" % v) if mod_name == "onnxruntime" else np_name
        p = os.path.join(self.app, "vendor", name)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(mod_name + "/__init__.py", "STUB = %r\n" % mod_name)
        return name

    def test_missing_wheel_reports_name(self):
        ok, why = detect.ensure_runtime(self.app, self.var)
        self.assertFalse(ok)
        self.assertIn("缺少", why)
        self.assertIn("whl", why)

    def test_extract_and_marker(self):
        self._fake_wheel("numpy")
        self._fake_wheel("onnxruntime")
        ok, why = detect.ensure_runtime(self.app, self.var)
        self.assertTrue(ok, why)
        dest = os.path.join(self.var, "ort", detect._py_tag())
        self.assertTrue(os.path.isfile(os.path.join(dest, ".ok")))
        self.assertIn(dest, sys.path)
        self.assertTrue(os.path.isfile(os.path.join(dest, "numpy", "__init__.py")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "onnxruntime", "__init__.py")))

    def test_marker_skips_reextract(self):
        self._fake_wheel("numpy")
        self._fake_wheel("onnxruntime")
        self.assertTrue(detect.ensure_runtime(self.app, self.var)[0])
        dest = os.path.join(self.var, "ort", detect._py_tag())
        with open(os.path.join(dest, "numpy", "__init__.py"), "w") as f:
            f.write("STUB = 'tampered'\n")
        # 有 .ok 标记：不再解包，保留现状（幂等，不重复做功）
        self.assertTrue(detect.ensure_runtime(self.app, self.var)[0])
        with open(os.path.join(dest, "numpy", "__init__.py")) as f:
            self.assertEqual(f.read(), "STUB = 'tampered'\n")

    def test_runtime_wheel_names(self):
        need = detect.runtime_wheel_names()
        v = detect.sys_version_info()
        self.assertEqual(need["python"], "%d.%d" % v)
        self.assertTrue(need["ort"].startswith("onnxruntime-1.20.1-cp"))
        self.assertTrue(need["numpy"].endswith(".whl"))
        if v >= (3, 13):
            self.assertTrue(need["numpy"].startswith("numpy-2.1.3"))
        else:
            self.assertTrue(need["numpy"].startswith("numpy-1.26.4"))


class TestModelFiles(unittest.TestCase):
    def test_model_present(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
        p = detect.model_path(root)
        self.assertTrue(os.path.isfile(p), "模型必须随包：%s" % p)
        self.assertGreater(os.path.getsize(p), 5 * 1024 * 1024, "10MB 级的 YOLO11n")

    def test_vendor_wheels_present(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "vendor")
        if not os.path.isdir(root):
            self.skipTest("没有 vendor 目录（本机调试）")
        names = os.listdir(root)
        for tag in ("311", "312", "313"):
            self.assertTrue(any(("cp3" + tag[1:]) in n and n.endswith(".whl") for n in names
                                if "onnxruntime" in n),
                            "缺 onnxruntime cp3%s wheel" % tag)
        self.assertTrue(any("numpy" in n for n in names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
