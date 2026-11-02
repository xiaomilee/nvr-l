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
import ledger  # noqa: E402

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
        # 台账：必须用真实写入端 ledger.merge 落盘。以前这里手搓
        # {"windows": [...]}，恰好和「读错键」的实现互相印证 —— 单测全绿，
        # 线上却一个归档文件都出不来（0.9.101 修的就是这个）。
        for d, src, start, rel in (("2026-10-18", "10-00-00.mp4", 36000,
                                    [[60.0, 120.0]]),
                                   ("2026-10-12", "00-01-00.mp4", 60,
                                    [[40.0, 140.0]])):
            ledger.merge(self.root, "cam1", d, src, start, rel,
                         density=0.05, engine="pixel")

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
    def _run(self, batch=10, fail_names=(), part_bytes=4096):
        real_split = archive._split

        def fake_split(ffmpeg, src, out_base, wins, arc_dir):
            name = os.path.basename(out_base) + ".mp4"
            if name in fail_names:
                return {"ok": False, "error": "boom"}
            out = out_base + ".c01.mp4"
            # 真实片段至少几 KB；ARCHIVE.EMPTY_PART_BYTES 会把几十字节的
            # 「只有容器头」当成 0 帧空壳，夹具必须模拟真实体积
            touch(out, part_bytes)
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
        # 台账确实没有活动窗口 → 该日无可裁画面，直接空收尾
        ledger.merge(self.root, "cam1", "2026-10-18", "10-00-00.mp4", 36000,
                     [], density=0.0, engine="pixel")
        r = self._run()
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-18")))
        self.assertTrue(any(x.get("empty") for x in r["days"]))


class TestLedgerRead(unittest.TestCase):
    """0.9.101 回归守卫：归档必须能读懂台账 v1（窗口嵌在 sources 下）。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvr-led-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.led = os.path.join(self.root, "_ledger", "cam1")

    def test_reads_sources_schema(self):
        ledger.merge(self.root, "cam1", "2026-10-18", "10-00-00.mp4", 36000,
                     [[60.0, 120.0]], density=0.05, engine="pixel")
        with open(os.path.join(self.led, "2026-10-18.json"),
                  encoding="utf-8") as f:
            raw = json.load(f)
        # 写入端不产生顶层 windows —— 旧读取端正是读它才永远读空
        self.assertNotIn("windows", raw)
        self.assertIn("sources", raw)
        self.assertEqual(archive._ledger_events(self.led, "2026-10-18"),
                         [(36060.0, 36120.0)])

    def test_missing_ledger_and_empty_windows(self):
        self.assertEqual(archive._ledger_events(self.led, "2026-10-18"), [])
        ledger.merge(self.root, "cam1", "2026-10-18", "10-00-00.mp4", 36000,
                     [], density=0.0, engine="pixel")
        self.assertEqual(archive._ledger_events(self.led, "2026-10-18"), [])

    def test_bogus_empty_done_self_heals(self):
        """误标为「空收尾」的日期要能自愈，真静默日不被反复扫。"""
        ledger.merge(self.root, "cam1", "2026-10-18", "10-00-00.mp4", 36000,
                     [[60.0, 120.0]], density=0.05, engine="pixel")
        archive.write_done(archive.archive_dir(self.root, "cam1", "2026-10-18"), 0)
        archive.write_done(archive.archive_dir(self.root, "cam1", "2026-10-17"), 0)
        self.assertEqual(archive._done_days(self.root, "cam1", self.led),
                         {"2026-10-17"})


class TestPerCamSegment(unittest.TestCase):
    """回归：分片长度必须按路取，不能拿全局值套所有通道。

    0.9.101 及以前 plan/run 只用全局 seg_sec（默认 300）算每个分片的覆盖区间。
    配了 60 秒分片的通道（真机院子 R）于是被当成 5 分钟长：18:40:22 那片被
    估成覆盖到 18:45:22，18:45:00 的活动窗口就被派给它去裁 —— 那片其实
    18:41:22 就结束了，ffmpeg 裁出 262 字节的 0 帧空壳还返回成功。
    真机现场：院子 R 一天 214 个产物里 174 个是这种空壳。
    """

    DAY = "2026-10-18"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvr-perseg-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in ("18-40-22.mp4", "18-41-22.mp4", "18-45-00.mp4"):
            touch(os.path.join(self.root, "cam1", self.DAY, f))
        # 台账：18:45:00~18:45:17 一段活动
        ledger.merge(self.root, "cam1", self.DAY, "18-45-00.mp4", 67500,
                     [[0.0, 17.0]], density=0.05, engine="pixel")

    def cams(self):
        return [{"id": "cam1", "name": "院子R", "dir_name": "cam1"}]

    def _run(self, cam_extra):
        used = []
        real = archive._split

        def fake(ffmpeg, src, out_base, wins, arc_dir):
            used.append(os.path.basename(src))
            out = out_base + ".c01.mp4"
            touch(out, 4096)                 # 真实片段体积
            return {"ok": True, "files": [out]}

        archive._split = fake
        try:
            archive.run(self.root, self.cams(),
                        {"cam1": dict({"full_days": 7}, **cam_extra)},
                        {"cam1": "virtual"}, None,
                        today=TODAY, batch=10)
        finally:
            archive._split = real
        return used

    def test_only_true_overlap_is_sliced(self):
        # 该路配 60 秒分片：只有 18-45-00 真正含这段窗口
        used = self._run({"segment_sec": 60})
        self.assertEqual(used, ["18-45-00.mp4"])

    def test_old_global_seg_would_misfire(self):
        # 反证：同一批分片按全局 300 秒算，18:40:22 也会被拉进来（旧 bug 现场）
        segs = {}
        for f in ("18-40-22.mp4", "18-41-22.mp4", "18-45-00.mp4"):
            st = archive._src_start(f)
            segs[f] = (st, min(86400.0, st + 300))
        cuts = archive.slice_windows([(67500.0, 67517.0)], segs)
        self.assertIn("18-40-22.mp4", cuts)
        self.assertEqual(len(cuts), 3)

    def test_cam_seg_sec_fallback(self):
        self.assertEqual(archive._cam_seg_sec({"segment_sec": 60}, 300), 60.0)
        self.assertEqual(archive._cam_seg_sec({}, 300), 300.0)
        self.assertEqual(archive._cam_seg_sec({"segment_sec": 0}, 300), 300.0)
        self.assertEqual(archive._cam_seg_sec({"segment_sec": "bad"}, 120), 120.0)


class TestEmptyPartSelfHeal(unittest.TestCase):
    """0 帧空壳必须让该日作废重做，而不是永远算「已完成」。"""

    DAY = "2026-10-18"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvr-empty-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        touch(os.path.join(self.root, "cam1", self.DAY, "10-00-00.mp4"))
        ledger.merge(self.root, "cam1", self.DAY, "10-00-00.mp4", 36000,
                     [[60.0, 120.0]], density=0.05, engine="pixel")
        self.arc = archive.archive_dir(self.root, "cam1", self.DAY)

    def test_empty_part_invalidates_done(self):
        touch(os.path.join(self.arc, "10-00-00.c01.mp4"), 262)   # 空壳
        archive.write_done(self.arc, 3)
        self.assertNotIn(self.DAY, archive._done_days(self.root, "cam1"))

    def test_healthy_parts_keep_done(self):
        touch(os.path.join(self.arc, "10-00-00.c01.mp4"), 4096)  # 正常片段
        archive.write_done(self.arc, 1)
        self.assertIn(self.DAY, archive._done_days(self.root, "cam1"))

    def test_purge_removes_empty_and_idx(self):
        touch(os.path.join(self.arc, "10-00-00.c01.mp4"), 262)
        touch(os.path.join(self.arc, "10-00-00.idx.json"), 90)
        touch(os.path.join(self.arc, "10-00-00.c02.mp4"), 4096)  # 正常，保留
        self.assertTrue(archive._purge_empty_parts(self.arc))
        self.assertFalse(os.path.exists(os.path.join(self.arc, "10-00-00.c01.mp4")))
        self.assertFalse(os.path.exists(os.path.join(self.arc, "10-00-00.idx.json")))
        self.assertTrue(os.path.exists(os.path.join(self.arc, "10-00-00.c02.mp4")))


if __name__ == "__main__":
    unittest.main(verbosity=1)