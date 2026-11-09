#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检测 include 区（只统计区内目标）：0.9.128 批次 10。

要治的是什么：院子里装了摄像头，画面里有半条马路和邻居的走廊。忽略区只能
"把某块涂掉"，治不了反过来的诉求——**只想知道车道/大门口有没有人**。
用户只能在事件页自己翻，或者干脆把灵敏度调低，连该报的也一起漏掉。

四条口径，每条都对应一种错误做法：

  1. **空 include = 不限制**。默认必须与 0.9.127 逐字相同，升级不改任何既有
     判定；否则老配置一升级就"AI 突然变笨"。
  2. **框中心口径**，与忽略区同一套坐标。用重叠面积会出现"半个身子在区外就
     不算"，画窄带覆盖走廊时很别扭；中心点口径用户学一次就懂。
  3. **忽略区优先**。两块重叠按忽略处理——用户画了"别管这块"，不该因为
     另一块"只看这块"又被放回来。
  4. **挡掉的框要记账**。把只统计区画得太窄，事件页会一片安静；这个数进
     诊断页（"区外忽略 N"），用户才知道是区域画错了，而不是 AI 不干活。

跑：python test/test_detect_include.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "server"))
import detect  # noqa: E402


def box_at(x, y, w=0.05, h=0.10):
    """按框左上角造一个框（16×9 网格下 1 格 ≈ 0.0625 × 0.1111）。"""
    return [x, y, w, h]


def det(cat, box, score=0.9):
    return {"c": cat, "b": box, "s": score}


CLASSES = {"person": True, "two_wheeler": True, "vehicle": True}


class TestCells(unittest.TestCase):
    """格号 → 归一化矩形。格号 = row*16+col，行在上。"""

    def test_first_cell(self):
        self.assertEqual(detect.cells_to_rects([0]),
                         [[0.0, 0.0, 1 / 16.0, 1 / 9.0]])

    def test_last_cell(self):
        r = detect.cells_to_rects([143])[0]
        self.assertAlmostEqual(r[0], 15 / 16.0)
        self.assertAlmostEqual(r[1], 8 / 9.0)

    def test_row_major(self):
        """格 16 = 第二行第一列（行在上，不是列在上）。"""
        r = detect.cells_to_rects([16])[0]
        self.assertAlmostEqual(r[0], 0.0)
        self.assertAlmostEqual(r[1], 1 / 9.0)

    def test_skip_out_of_range(self):
        self.assertEqual(detect.cells_to_rects([-1, 144, 999]), [])

    def test_skip_garbage(self):
        """配置是手改过的、或者前端传了脏值：不抛异常，只是丢掉。"""
        self.assertEqual(detect.cells_to_rects(["", "x", None]), [])
        self.assertEqual(len(detect.cells_to_rects(["5"])), 1)   # 数字字符串认

    def test_empty(self):
        self.assertEqual(detect.cells_to_rects([]), [])
        self.assertEqual(detect.cells_to_rects(None), [])


class TestInInclude(unittest.TestCase):
    def setUp(self):
        self.rects = detect.cells_to_rects([0])      # 左上角第一格

    def test_inside(self):
        self.assertTrue(detect.in_include(box_at(0.01, 0.01), self.rects))

    def test_outside(self):
        self.assertFalse(detect.in_include(box_at(0.80, 0.80), self.rects))

    def test_empty_rects_means_no_limit(self):
        """默认（没配只统计区）必须整幅画面都算 —— 这是升级不改行为的关键。"""
        self.assertTrue(detect.in_include(box_at(0.99, 0.99), []))
        self.assertTrue(detect.in_include(box_at(0.99, 0.99), None))

    def test_center_not_corner(self):
        """框的左上角在区内、中心在区外 → 不算。口径是中心，不是交叠。"""
        # 第一格 x:0~0.0625；框从 0.06 起、宽 0.2 → 中心 0.16，在区外
        self.assertFalse(detect.in_include(box_at(0.06, 0.01, 0.2, 0.2), self.rects))

    def test_multi_cell_union(self):
        """两格不连通也算"任一命中"：用户画的是两块，不是必须连成一块。"""
        rects = detect.cells_to_rects([0, 143])
        self.assertTrue(detect.in_include(box_at(0.95, 0.90), rects))


class TestApplyRegions(unittest.TestCase):
    def test_no_regions_passthrough(self):
        """两块都没配 → 与旧版本一模一样（没有 include 这回事）。"""
        ds = [det("person", box_at(0.10, 0.10)),
              det("vehicle", box_at(0.80, 0.80))]
        kept, outside = detect.apply_regions(ds, CLASSES)
        self.assertEqual(len(kept), 2)
        self.assertEqual(outside, 0)

    def test_include_drops_outside_and_counts(self):
        inc = detect.cells_to_rects([0, 1, 2])
        ds = [det("person", box_at(0.02, 0.02)),     # 区内
              det("person", box_at(0.70, 0.70)),     # 区外
              det("vehicle", box_at(0.75, 0.75))]    # 区外
        kept, outside = detect.apply_regions(ds, CLASSES, [], inc)
        self.assertEqual([d["c"] for d in kept], ["person"])
        self.assertEqual(outside, 2)

    def test_ignore_wins_over_include(self):
        """同一格既在忽略区又在只统计区 → 按忽略处理，且不算"区外"。"""
        inc = detect.cells_to_rects([0])
        ign = detect.cells_to_rects([0])
        ds = [det("person", box_at(0.01, 0.01))]
        kept, outside = detect.apply_regions(ds, CLASSES, ign, inc)
        self.assertEqual(kept, [])
        self.assertEqual(outside, 0)   # 被忽略区挡掉的，不该记成"区外忽略"

    def test_class_filter_first(self):
        """类别没勾的不进"区外"计数：那不是区域挡的，是用户没要这一类。"""
        inc = detect.cells_to_rects([0])
        ds = [det("vehicle", box_at(0.70, 0.70))]
        kept, outside = detect.apply_regions(
            ds, {"person": True, "vehicle": False, "two_wheeler": False}, [], inc)
        self.assertEqual(kept, [])
        self.assertEqual(outside, 0)

    def test_none_dets(self):
        self.assertEqual(detect.apply_regions(None, CLASSES), ([], 0))

    def test_bad_box_shape_does_not_raise(self):
        """手改配置/上游脏数据：坏框按"不在区内"处理，不能让一路带崩整轮。"""
        inc = detect.cells_to_rects([0])
        ds = [{"c": "person", "b": None, "s": 0.5}]
        kept, outside = detect.apply_regions(ds, CLASSES, [], inc)
        self.assertEqual(kept, [])
        self.assertEqual(outside, 1)


class TestWorkerRects(unittest.TestCase):
    """DetectWorker 的两个解码入口：cam.masks 形状不对时返回空（=不限制）。"""

    def test_ignore_and_include(self):
        cam = {"masks": {"ignore": [0, 1], "include": [143]}}
        self.assertEqual(len(detect.DetectWorker._ignore_rects(cam)), 2)
        self.assertEqual(len(detect.DetectWorker._include_rects(cam)), 1)

    def test_missing_include_is_empty(self):
        """老配置只有 ignore（0.9.127 及以前）→ include 为空 = 不限制。"""
        cam = {"masks": {"ignore": [0]}}
        self.assertEqual(detect.DetectWorker._include_rects(cam), [])

    def test_no_masks(self):
        self.assertEqual(detect.DetectWorker._include_rects({"id": "cam-01"}), [])
        self.assertEqual(detect.DetectWorker._include_rects(None), [])
        self.assertEqual(detect.DetectWorker._include_rects({"masks": "oops"}), [])


class TestOutsideStat(unittest.TestCase):
    """挡掉的框数要真的记进每路统计（诊断页的"区外忽略 N"读这里）。"""

    def _worker(self):
        return detect.DetectWorker(None, "ffmpeg", "/tmp", lambda: "/tmp",
                                   lambda: [], lambda: ({}, []))

    def test_counts_per_day(self):
        w = self._worker()
        try:
            w._outside("cam-01", 3)
            w._outside("cam-01", 2)
            w._outside("cam-02", 1)
            st = w.status()["per_cam"]
            self.assertEqual(st["cam-01"]["outside"], 5)
            self.assertEqual(st["cam-02"]["outside"], 1)
        finally:
            # worker 没 start()，没有线程可停；这里只保证统计口径正确
            pass

    def test_zero_not_recorded(self):
        w = self._worker()
        w._outside("cam-01", 0)
        self.assertNotIn("cam-01", w.status()["per_cam"])

    def test_blank_shape_has_outside(self):
        """空壳也要有 outside 键：调用方按固定键读，缺键就要多写一个判断。"""
        self.assertEqual(detect.DetectWorker._blank()["outside"], {})


class TestNormMasks(unittest.TestCase):
    """服务端的强校：与前端 normMasks 同口径（去重 / 丢越界 / 升序）。

    两处必须一致，否则会出现「页面显示 12 格、服务端只认 11 格」这种
    没法排查的漂移。写盘与对外回显也共用这一个函数。
    """

    def setUp(self):
        import nvr_slim
        self.nm = nvr_slim.norm_masks

    def test_two_keys(self):
        self.assertEqual(self.nm({"ignore": [2, 1], "include": [10]}),
                         {"ignore": [1, 2], "include": [10]})

    def test_old_config_only_ignore(self):
        """0.9.127 及以前的配置只有 ignore，读上来 include 必须是空列表。"""
        self.assertEqual(self.nm({"ignore": [3]})["include"], [])

    def test_drop_out_of_range_and_garbage(self):
        out = self.nm({"include": [-1, 0, 143, 144, "x", None, 1.5, "7"]})
        self.assertEqual(out["include"], [0, 7, 143])

    def test_not_a_dict(self):
        self.assertEqual(self.nm(None), {"ignore": [], "include": []})
        self.assertEqual(self.nm("oops"), {"ignore": [], "include": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
