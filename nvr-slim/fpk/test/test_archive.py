#!/usr/bin/env python3
"""archive.py 单测：选日规则 / 窗口切片 / 计划与执行幂等。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "server"))
import archive  # noqa: E402

TODAY = date(2026, 10, 24)


def touch(p, size=100):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"0" * size)


class TestSelectDays(unittest.TestCase):
    def test_expiring_first(self):
        # full_days=7 → cut = 10-18。10-18 明天过期最优先；10-10 回填；10-20 保留期内
        r = archive.select_days(["2026-10-10", "2026-10-18", "2026-10-20"],
                                7, TODAY, set(), {"2026-10-10", "2026-10-18"})
        self.assertEqual(r["todo"], ["2026-10-18", "2026-10-10"])
        self.assertEqual(r["no_ledger"], [])

    def test_no_ledger_skipped(self):
        r = archive.select_days(["2026-10-18", "2026-10-12"], 7, TODAY,
                                set(), set())
        self.assertEqual(r["todo"], [])
        self.assertEqual(r["no_ledger"], ["2026-10-12", "2026-10-18"])

    def test_done_skipped(self):
        r = archive.select_days(["2026-10-18"], 7, TODAY,
                                {"2026-10-18"}, {"2026-10-18"})
        self.assertEqual(r["todo"], [])

    def test_future_and_in_window_ignored(self):
        # full_days=4 → cut=10-21：10-22 保留期内不进；10-24/25 未来不进；
        # 非日期名不进（10-21 恰好明天过期会进 expiring，这里不放在列表里）
        r = archive.select_days(["2026-10-22", "2026-10-24", "2026-10-25",
                                 "not-a-date"], 4, TODAY, set(),
                                {"2026-10-22"})
        self.assertEqual(r["todo"], [])
        self.assertEqual(r["no_ledger"], [])


class TestSliceWindows(unittest.TestCase):
    def test_overlap_and_offsets(self):
        segs = {"10-00-00.mp4": (36000, 36300), "10-05-00.mp4": (36300, 36600)}
        r = archive.slice_windows([(36100, 36150), (36280, 36320)], segs)
        self.assertEqual(r["10-00-00.mp4"], [(100.0, 150.0), (280.0, 300.0)])
        self.assertEqual(r["10-05-00.mp4"], [(0.0, 20.0)])

    def test_no_overlap_excluded(self):
        r = archive.slice_windows([(100, 200)], {"09-00-00.mp4": (0, 90)})
        self.assertEqual(r, {})

    def test_day_end_clamped(self):
        r = archive.slice_windows([(86300, 86400)], {"23-55-00.mp4": (85800, 86400)})
        self.assertEqual(r["23-55-00.mp4"], [(500.0, 600.0)])


class _Env(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvr-arch-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # cam1：10-18（明天过期，有台账）+ 10-12（回填，有台账）+ 10-16（无台账）
        for d, files in (("2026-10-18", ["10-00-00.mp4", "10-05-00.mp4"]),
                         ("2026-10-12", ["00-01-00.mp4"]),
                         ("2026-10-16", ["10-00-00.mp4"])):
            for f in files:
                touch(os.path.join(self.root, "cam1", d, f))
        # 台账：10-18 有活动窗口；10-12 也写一份（内容无所谓，存在即可）
        for d, wins in (("2026-10-18", [{"s": 36060.0, "e": 36120.0}]),
                        ("2026-10-12", [{"s": 100.0, "e": 200.0}])):
            touch(os.path.join(self.root, "_ledger", "cam1", d + ".json"), 0)
            p = os.path.join(self.root, "_ledger", "cam1", d + ".json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"windows": wins}, f)

    def cams(self):
        return [{"id": "cam1", "name": "前门", "dir_name": "cam1"}]

    def modes(self):
        return {"cam1": "virtual"}


class TestPlan(_Env):
    def test_virtual_only_and_estimates(self):
        p = archive.plan(self.root, self.cams(), {"cam1": {"full_days": 7}},
                         self.modes(), today=TODAY, seg_sec=300.0)
        dates = [x["date"] for x in p["todo"]]
        self.assertEqual(dates, ["2026-10-18", "2026-10-12"])
        self.assertEqual(p["segments"], 3)
        it18 = p["todo"][0]
        self.assertEqual(it18["segments"], 2)
        self.assertGreater(it18["bytes"], 0)
        self.assertGreater(it18["est_bytes"], 0)
        self.assertEqual([x["date"] for x in p["no_ledger"]], ["2026-10-16"])

    def test_materialize_not_planned(self):
        p = archive.plan(self.root, self.cams(), {"cam1": {"full_days": 7}},
                         {"cam1": "materialize"}, today=TODAY)
        self.assertEqual(p["todo"], [])

    def test_done_excluded(self):
        arc = archive.archive_dir(self.root, "cam1", "2026-10-18")
        archive.write_done(arc, 2)
        p = archive.plan(self.root, self.cams(), {"cam1": {"full_days": 7}},
                         self.modes(), today=TODAY)
        self.assertEqual([x["date"] for x in p["todo"]], ["2026-10-12"])


class TestRun(_Env):
    def _run(self, batch=10, fail_names=()):
        real_split = archive._split

        def fake_split(ffmpeg, src, out_base, wins, arc_dir):
            name = os.path.basename(out_base) + ".mp4"
            if name in fail_names:
                return {"ok": False, "error": "boom"}
            out = out_base + ".c01.mp4"
            touch(out, 50)
            return {"ok": True, "reason": "split", "files": [out]}
        archive._split = fake_split
        try:
            return archive.run(self.root, self.cams(),
                               {"cam1": {"full_days": 7}}, self.modes(),
                               None, today=TODAY, batch=batch)
        finally:
            archive._split = real_split

    def test_success_and_done_marks(self):
        # 窗口 36060~36120 只命中 10-00-00（36000~36300）；10-05-00 无交集不裁
        r = self._run()
        self.assertTrue(r["ok"])
        self.assertEqual(r["segments_used"], 2)
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-18")))
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-12")))
        self.assertEqual(r["remaining"], 0)

    def test_idempotent_rerun(self):
        self._run()
        r = self._run()
        self.assertEqual(r["segments_used"], 0)   # done 日期不再进 todo
        self.assertEqual(r["plan"]["segments"], 0)

    def test_failure_no_done_and_retry(self):
        # 10-18 的 10-00-00 命中窗口且失败 → 该日无 done；10-12 正常收尾
        r = self._run(fail_names={"10-00-00.mp4"})
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["failed"]), 1)
        self.assertFalse(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-18")))
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-12")))
        # 修复后重跑：失败那片这次成功 → 收尾
        r2 = self._run()
        self.assertEqual(r2["segments_used"], 1)
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-18")))

    def test_batch_stops_midway_without_done(self):
        r = self._run(batch=1)
        self.assertEqual(r["segments_used"], 1)
        # 10-18 唯一命中分片已成功 → 该日收尾；10-12 没轮到 → 无 done
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-18")))
        self.assertFalse(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-12")))
        self.assertEqual(r["remaining"], 1)

    def test_silent_day_empty_done(self):
        # 台账窗口清空 → 该日无可裁画面，直接空收尾
        p = os.path.join(self.root, "_ledger", "cam1", "2026-10-18.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"windows": []}, f)
        r = self._run()
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-18")))
        self.assertTrue(any(x.get("empty") for x in r["days"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
