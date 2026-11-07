#!/usr/bin/env python3
"""录像可信度（health）测试（0.9.118）。

守的是四件事：

  1. **缺口算得对**：区间之间、开头、结尾三种洞都要报，且「今天」只算到现在 ——
     否则每到下午就报「今天缺了 8 小时」，那是还没发生。
  2. **覆盖按实测累加**：不能用「末片起点 − 首片起点」，那样断流那段会被算成有画面。
  3. **状态分层**：进程活着但分片不增长（真机断流的样子）必须判 stalled，
     不能因为 poll() 还是 None 就报「录像中」。
  4. **计数不漏**：/api/status 的 cams 不再是配置数，connecting/recovering 不进 error
     —— 否则「异常」天天红着，真出事反而没人看。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import health  # noqa: E402
import ledger  # noqa: E402


class TestGaps(unittest.TestCase):
    def test_middle_gap(self):
        spans = [[0.0, 600.0], [900.0, 1200.0]]
        g = health.gaps_of(spans, now=1200)
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["start"], 600.0)
        self.assertEqual(g[0]["end"], 900.0)
        self.assertEqual(g[0]["duration"], 300.0)

    def test_head_gap(self):
        g = health.gaps_of([[3600.0, 7200.0]])
        self.assertEqual(g[0]["start"], 0.0)
        self.assertEqual(g[0]["duration"], 3600.0)

    def test_tail_gap_clamped_by_now(self):
        g = health.gaps_of([[0.0, 3600.0]], now=3900)
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["start"], 3600.0)
        self.assertEqual(g[0]["end"], 3900.0)
        self.assertEqual(g[0]["duration"], 300.0)

    def test_future_not_reported_as_gap(self):
        # 现在是 4000 秒，晚于它的时段还没发生，不该算缺
        g = health.gaps_of([[0.0, 4000.0]], now=4000)
        self.assertEqual(g, [])

    def test_small_hole_ignored(self):
        # 59.9 秒的缝是 mtime 秒级抖动，不报警
        g = health.gaps_of([[0.0, 600.0], [659.9, 900.0]], now=900)
        self.assertEqual(g, [])

    def test_empty_spans_is_one_gap(self):
        g = health.gaps_of([], now=1800)
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["duration"], 1800.0)

    def test_bad_span_skipped(self):
        g = health.gaps_of([["x", "y"], None, [0.0, 600.0]], now=600)
        self.assertEqual(g, [])

    def test_overlapping_spans_not_double_counted(self):
        g = health.gaps_of([[0.0, 900.0], [300.0, 1200.0]], now=1200)
        self.assertEqual(g, [])


class TestSummarize(unittest.TestCase):
    def test_covered_is_sum_not_span(self):
        # 两段各 300 秒、中间断 600 秒：覆盖只有 600，不是 1200
        s = health.summarize([[0.0, 300.0], [900.0, 1200.0]],
                             [{"duration": 600.0}], now=1200)
        self.assertEqual(s["covered_sec"], 600.0)
        self.assertEqual(s["gap_sec"], 600.0)
        self.assertEqual(s["gap_count"], 1)
        self.assertEqual(s["max_gap_sec"], 600.0)

    def test_coverage_denominator_is_now(self):
        # 上午看页面时用满一天做分母，永远显示 30%，看着像坏了
        s = health.summarize([[0.0, 3600.0]], [], now=3600)
        self.assertEqual(s["coverage"], 1.0)

    def test_coverage_capped(self):
        s = health.summarize([[0.0, 1000.0]], [], now=100)
        self.assertEqual(s["coverage"], 1.0)

    def test_day_report_sums_all_cams(self):
        d = health.day_report([{"covered_sec": 100, "gap_sec": 10, "gap_count": 1,
                                "max_gap_sec": 10},
                               {"covered_sec": 200, "gap_sec": 20, "gap_count": 2,
                                "max_gap_sec": 12}])
        self.assertEqual(d["cams"], 2)
        self.assertEqual(d["covered_sec"], 300)
        self.assertEqual(d["gap_sec"], 30)
        self.assertEqual(d["gap_count"], 3)
        self.assertEqual(d["max_gap_sec"], 12)


class TestState(unittest.TestCase):
    def ch(self, **kw):
        base = {"enabled": True, "state": "recording", "retry": 0, "stall": 0,
                "last_age_sec": 1, "error": None}
        base.update(kw)
        return base

    def test_off_when_disabled(self):
        self.assertEqual(health.state_of(self.ch(enabled=False)), "off")

    def test_recording_normal(self):
        self.assertEqual(health.state_of(self.ch(), idle_sec=1), "recording")

    def test_stalled_by_segment_age(self):
        # 进程活着（poll() 还是 None），但分片 200 秒没增长 —— 真机断流就是这样
        self.assertEqual(health.state_of(self.ch(last_age_sec=200)), "stalled")

    def test_stalled_by_upstream_idle(self):
        self.assertEqual(health.state_of(self.ch(last_age_sec=1), idle_sec=200),
                         "stalled")

    def test_idle_unknown_does_not_stall(self):
        self.assertEqual(health.state_of(self.ch(), idle_sec=None), "recording")

    def test_not_running_with_stall_is_recovering(self):
        self.assertEqual(health.state_of(self.ch(state="idle", stall=2)),
                         "recovering")

    def test_not_running_with_retry_is_connecting(self):
        self.assertEqual(health.state_of(self.ch(state="idle", retry=3)),
                         "connecting")

    def test_not_running_with_error(self):
        self.assertEqual(health.state_of(self.ch(state="idle", error="退出码 1")),
                         "error")

    def test_not_running_plain(self):
        self.assertEqual(health.state_of(self.ch(state="idle")), "stopped")

    def test_reason_texts(self):
        for st, ch in (("off", self.ch(enabled=False)),
                       ("error", self.ch(state="idle", error="退出码 1")),
                       ("connecting", self.ch(state="idle", retry=2)),
                       ("recovering", self.ch(state="idle", stall=1)),
                       ("stalled", self.ch(last_age_sec=120)),
                       ("stopped", self.ch(state="idle"))):
            self.assertTrue(health.reason_of(st, ch), st)


class TestGapReason(unittest.TestCase):
    def test_midnight_start(self):
        r = health.gap_reason({"start": 0.0}, "recording")
        self.assertIn("00:00", r)

    def test_stalled_today(self):
        r = health.gap_reason({"start": 3600.0}, "stalled", day_is_today=True)
        self.assertIn("停止输出", r)

    def test_recovered_past(self):
        r = health.gap_reason({"start": 3600.0}, "recording")
        self.assertIn("已恢复", r)


DATE = "2026-11-06"


def _mtime(hh, mm, ss=0):
    return time.mktime(time.strptime("%s %02d:%02d:%02d" % (DATE, hh, mm, ss),
                                     "%Y-%m-%d %H:%M:%S"))


class TestGapsPayload(unittest.TestCase):
    """接口层：真的扫一遍目录，算出一段 15 分钟的断流。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cam = {"id": "c1", "name": "客厅", "dir_name": "客厅",
                    "enabled": True, "url": "rtsp://x"}
        self.files = {DATE: [
            {"n": "00-00-00.mp4", "m": _mtime(0, 5)},
            {"n": "00-20-00.mp4", "m": _mtime(0, 25)},
        ]}
        self.patches = [
            mock.patch.object(__import__("nvr_slim"), "cameras", lambda: [self.cam]),
            mock.patch.object(__import__("nvr_slim"), "cam_storage_dirs",
                              lambda c: ["客厅"]),
            mock.patch.object(__import__("nvr_slim"), "record_dir", lambda: self.tmp),
            mock.patch.object(__import__("nvr_slim"), "_scan_day_dirs",
                              lambda p: self.files),
        ]
        for p in self.patches:
            p.start()
        self.ns = __import__("nvr_slim")
        self.ns.STATE["var_dir"] = self.tmp
        self.ns.STATE["recorder"] = None
        self.ns.STATE["ingest"] = None

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_finds_middle_gap(self):
        d = self.ns.health_gaps_payload(DATE, force=True)
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["cams"]), 1)
        gs = d["cams"][0]["gaps"]
        self.assertTrue(any(abs(g["duration"] - 900.0) < 1.0 for g in gs),
                        [g["duration"] for g in gs])
        self.assertTrue(all(g["reason"] for g in gs))

    def test_bad_date_rejected(self):
        d = self.ns.health_gaps_payload("2026/11/06", force=True)
        self.assertFalse(d["ok"])
        self.assertIn("YYYY-MM-DD", d["error"])

    def test_empty_day_not_reported_for_history(self):
        # 历史日期一整天没分片 = 那天本来没开录像，不该报一条 20 小时的缺口
        self.files[DATE] = []
        d = self.ns.health_gaps_payload(DATE, force=True)
        self.assertTrue(d["ok"])
        self.assertEqual(d["cams"], [])


class TestCamsCounts(unittest.TestCase):
    def setUp(self):
        self.ns = __import__("nvr_slim")
        self.cams = [{"id": "c%d" % i, "name": "c%d" % i, "enabled": True,
                      "url": "rtsp://x"} for i in range(5)]
        self.ns.STATE["ingest"] = None

    def _run(self, channels, idle=None):
        fake_rc = mock.Mock()
        fake_rc.status.return_value = {"channels": channels}
        with mock.patch.object(self.ns, "cameras", lambda: self.cams), \
                mock.patch.object(self.ns, "_ingest_idle_map", lambda: (idle or {})):
            self.ns.STATE["recorder"] = fake_rc
            try:
                return self.ns.cams_counts()
            finally:
                self.ns.STATE["recorder"] = None

    def ch(self, cid, **kw):
        base = {"id": cid, "name": cid, "enabled": True, "state": "recording",
                "retry": 0, "stall": 0, "last_age_sec": 1, "error": None}
        base.update(kw)
        return base

    def test_counts_by_real_state(self):
        c = self._run([self.ch("c0"), self.ch("c1"), self.ch("c2"),
                       self.ch("c3", last_age_sec=300),
                       self.ch("c4", state="idle", error="退出码 1")])
        self.assertEqual(c["total"], 5)
        self.assertEqual(c["enabled"], 5)
        self.assertEqual(c["recording"], 3)
        self.assertEqual(c["connected"], 3)
        self.assertEqual(c["stalled"], 1)
        self.assertEqual(c["error"], 1)

    def test_retrying_not_counted_as_error(self):
        # 还在重连循环里：会自己回来，混进 error 只会让数字天天红着
        c = self._run([self.ch("c0", state="idle", retry=2),
                       self.ch("c1", state="idle", stall=1),
                       self.ch("c2"), self.ch("c3"), self.ch("c4")])
        self.assertEqual(c["retrying"], 2)
        self.assertEqual(c["error"], 0)

    def test_disabled_cam_not_recording(self):
        self.cams[0]["enabled"] = False
        c = self._run([self.ch("c0", enabled=False)] +
                      [self.ch("c%d" % i) for i in range(1, 5)])
        self.assertEqual(c["enabled"], 4)
        self.assertEqual(c["off"], 1)
        self.assertEqual(c["recording"], 4)

    def test_no_recorder_returns_zero(self):
        with mock.patch.object(self.ns, "cameras", lambda: self.cams):
            self.ns.STATE["recorder"] = None
            c = self.ns.cams_counts()
        self.assertEqual(c["recording"], 0)
        self.assertEqual(c["total"], 5)


if __name__ == "__main__":
    unittest.main()
