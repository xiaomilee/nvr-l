#!/usr/bin/env python3
"""录制 → 衔接 → 压制这条链的行为测试。

守的几条：
  - 只消费已封口分片（碰正在写的那个就是坏文件）
  - 判定不解码：静止段不出片段，活动段带前后缓冲
  - 两种衔接模式只在「入队节奏」上不同，判定与导出完全共用
  - 幂等键含参数指纹：换参数要重跑，不换绝不重复
"""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import condense   # noqa: E402
import link as link_mod  # noqa: E402
import params     # noqa: E402
import recorder   # noqa: E402
import runtime    # noqa: E402

CAMS = [{"id": "cam-01", "name": "车库入口", "url": "rtsp://x/1"},
        {"id": "cam-02", "name": "一楼大堂", "url": ""}]


def touch(path, size=16, age=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    if age:
        st = os.stat(path)
        os.utime(path, (st.st_atime - age, st.st_mtime - age))


class TestSegments(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvrseg")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_pattern_carries_date_dirs(self):
        pat = recorder.out_pattern(self.root, "cam-01")
        self.assertIn("%Y-%m-%d", pat)
        self.assertIn("%H-%M-%S.mp4", pat)

    def test_ensure_day(self):
        d = recorder.ensure_day(self.root, "cam-01")
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(os.path.basename(d).count("-"), 2)

    def test_finished_skips_last(self):
        base = os.path.join(self.root, "cam-01", "2026-10-20")
        for i in range(3):
            touch(os.path.join(base, "10-0%d-00.mp4" % i))
        got = recorder.finished(self.root, "cam-01", seg_sec=300)
        self.assertEqual([os.path.basename(p) for p in got],
                         ["10-00-00.mp4", "10-01-00.mp4"])

    def test_finished_takes_last_when_idle(self):
        base = os.path.join(self.root, "cam-01", "2026-10-20")
        for i in range(3):
            touch(os.path.join(base, "10-0%d-00.mp4" % i), age=900)
        got = recorder.finished(self.root, "cam-01", seg_sec=300)
        self.assertEqual(len(got), 3)

    def test_record_cmd_never_decodes(self):
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x/1", "/tmp/%H.mp4", 300)
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertIn("-segment_format", cmd)
        self.assertIn("-rtsp_transport", cmd)
        self.assertNotIn("libx265", cmd)
        # 分片必须对齐墙钟，文件名才等于片段起点
        self.assertIn("-segment_atclocktime", cmd)

    def test_recorder_skips_cameras_without_url(self):
        r = recorder.Recorder("ffmpeg", self.root, CAMS, enabled=True)
        self.assertEqual([c["id"] for c in r.cams], ["cam-01"])


class TestCondense(unittest.TestCase):
    def curve(self, active=(), total=200, quiet=1000.0, loud=100000.0):
        return [(float(i), loud if i in active else quiet) for i in range(total)]

    def test_static_has_no_window(self):
        a = params.analysis()
        self.assertEqual(condense.pick_windows(self.curve(), a), [])

    def test_active_block_gets_buffers(self):
        a = dict(params.analysis(), pre_buf=3.0, post_buf=3.0, min_clip=3.0)
        w = condense.pick_windows(self.curve(range(110, 120)), a, duration=200)
        self.assertEqual(len(w), 1)
        self.assertAlmostEqual(w[0][0], 107.0, places=1)
        self.assertAlmostEqual(w[0][1], 123.0, places=1)

    def test_nearby_blocks_merge(self):
        a = dict(params.analysis(), merge_gap=5.0, pre_buf=0, post_buf=0)
        active = list(range(110, 119)) + list(range(122, 131))
        w = condense.pick_windows(self.curve(active), a, duration=200)
        self.assertEqual(len(w), 1)

    def test_far_blocks_stay_apart(self):
        a = dict(params.analysis(), merge_gap=2.0, pre_buf=0, post_buf=0)
        active = list(range(110, 119)) + list(range(140, 149))
        w = condense.pick_windows(self.curve(active), a, duration=200)
        self.assertEqual(len(w), 2)

    def test_short_blip_dropped(self):
        a = dict(params.analysis(), pre_buf=0, post_buf=0, min_clip=3.0)
        w = condense.pick_windows(self.curve([110]), a, duration=200)
        self.assertEqual(w, [])

    def test_threshold_ignores_active_majority(self):
        # 活动占多数时中位数会被拉高；20 分位不会
        curve = [(float(i), 100000.0 if i < 150 else 1000.0) for i in range(200)]
        a = params.analysis()
        th = condense.threshold(curve, a)
        self.assertLess(th, 100000.0)

    def test_clip_path_mirrors_source(self):
        root = "/vol2/NVR"
        src = os.path.join(root, "cam-01", "2026-10-20", "14-05-00.mp4")
        out = condense.clip_path(root, "cam-01", src)
        self.assertEqual(out, os.path.join(root, "_clip", "cam-01",
                                           "2026-10-20", "14-05-00.mp4"))

    def test_activity_curve_buckets(self):
        curve = condense.activity_curve([(0.1, 100), (0.9, 50), (1.4, 70)], 1.0)
        self.assertEqual(curve, [(0.0, 150), (1.0, 70)])


class TestKey(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvrkey")
        self.src = os.path.join(self.root, "cam-01", "2026-10-20", "10-00-00.mp4")
        touch(self.src, size=64)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_same_file_same_key(self):
        p = params.new_profile("车库入口")
        k1 = link_mod.item_key(self.src, self.root, "cam-01", p)
        k2 = link_mod.item_key(self.src, self.root, "cam-01", p)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 16)

    def test_rewrite_changes_key(self):
        p = params.new_profile("车库入口")
        k1 = link_mod.item_key(self.src, self.root, "cam-01", p)
        # 显式挪 mtime：文件系统时间精度可能让"立刻重写"看不出差别
        st = os.stat(self.src)
        os.utime(self.src, (st.st_atime, st.st_mtime + 10))
        k2 = link_mod.item_key(self.src, self.root, "cam-01", p)
        self.assertNotEqual(k1, k2)

    def test_param_change_changes_key(self):
        p1 = params.new_profile("车库入口")
        p2 = params.new_profile("车库入口")
        p2["analysis"]["noise_mult"] = 3.5
        k1 = link_mod.item_key(self.src, self.root, "cam-01", p1)
        k2 = link_mod.item_key(self.src, self.root, "cam-01", p2)
        self.assertNotEqual(k1, k2)

    def test_missing_file(self):
        self.assertIsNone(link_mod.item_key(os.path.join(self.root, "nope.mp4"),
                                            self.root, "cam-01", params.new_profile()))


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvrlink")
        self.var = tempfile.mkdtemp(prefix="nvrvar")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.var, ignore_errors=True)

    def make(self, **kw):
        cfg = {"link_mode": "live", "link_interval_h": 6, "record_segment_sec": 300}
        cfg.update(kw)
        return link_mod.Link("ffmpeg", "ffprobe", self.root, self.var, CAMS, cfg)

    def test_live_has_no_batch_time(self):
        lk = self.make(link_mode="live")
        lk.tick(now=1000.0)
        self.assertEqual(lk.next_batch, 0)

    def test_scheduled_first_run_is_immediate(self):
        lk = self.make(link_mode="scheduled", link_interval_h=6)
        lk.tick(now=1000.0)
        # 装完不能干等 6 小时，首次要先把积压处理掉
        self.assertGreater(lk.next_batch, 1000.0)
        self.assertAlmostEqual(lk.next_batch, 1000.0 + 6 * 3600, delta=2)

    def test_scheduled_interval_bounds(self):
        lk = self.make(link_mode="scheduled", link_interval_h=99)
        self.assertEqual(lk.interval_h, 24)
        lk2 = self.make(link_mode="scheduled", link_interval_h=0)
        self.assertEqual(lk2.interval_h, 1)

    def test_mode_switch_resets_batch(self):
        lk = self.make(link_mode="scheduled", link_interval_h=12)
        lk.tick(now=1000.0)
        lk.reconfigure({"link_mode": "live", "link_interval_h": 12,
                        "record_segment_sec": 300})
        self.assertEqual(lk.mode, "live")
        self.assertEqual(lk.next_batch, 0)

    def test_status_shape(self):
        lk = self.make()
        s = lk.status()
        for k in ("mode", "mode_label", "interval_h", "pending", "counters",
                  "saved_ratio", "next_batch_at", "last_item"):
            self.assertIn(k, s)
        self.assertEqual(s["mode_label"], "即刻压制")

    def test_enqueue_ignores_unfinished(self):
        # 只建一个分片：它是"正在写的那个"，不该被入队
        base = os.path.join(self.root, "cam-01", "2026-10-20")
        touch(os.path.join(base, "10-00-00.mp4"))
        lk = self.make()
        self.assertEqual(lk._new_items(), [])

    def test_enqueue_takes_closed_segment(self):
        base = os.path.join(self.root, "cam-01", "2026-10-20")
        touch(os.path.join(base, "10-00-00.mp4"), age=900)
        touch(os.path.join(base, "10-05-00.mp4"))
        lk = self.make()
        got = lk._new_items()
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["path"].endswith("10-00-00.mp4"))


class TestConfig(unittest.TestCase):
    def test_segment_sec_clamp(self):
        self.assertEqual(runtime.segment_sec(10), 60)
        self.assertEqual(runtime.segment_sec(5000), 900)
        self.assertEqual(runtime.segment_sec("junk"), 300)

    def test_interval_clamp(self):
        self.assertEqual(runtime.link_interval_h(-5), 1)
        self.assertEqual(runtime.link_interval_h(48), 24)
        self.assertEqual(runtime.link_interval_h(6), 6)

    def test_mode_fallback(self):
        self.assertEqual(runtime.link_mode("scheduled"), "scheduled")
        self.assertEqual(runtime.link_mode("whatever"), "live")

    def test_defaults_present(self):
        for k in ("record_enable", "record_segment_sec", "link_mode",
                  "link_interval_h", "link_max_queue"):
            self.assertIn(k, runtime.DEFAULTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
