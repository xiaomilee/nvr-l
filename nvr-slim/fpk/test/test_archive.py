#!/usr/bin/env python3
"""archive.py 单测：选日规则 / 窗口切片 / 计划与执行幂等。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

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

    def test_prefetch_off_skips_last_day(self):
        # full_days=2 → cut = 昨天。不提前：昨天今天不裁（明天删源前才裁，
        # 这样同一天不会同时存在全时与归档），前天（今天就要删）照常回填。
        y = (TODAY - timedelta(days=1)).isoformat()
        b = (TODAY - timedelta(days=2)).isoformat()
        r = archive.select_days([y, b], 2, TODAY, set(), {y, b}, prefetch=False)
        self.assertEqual(r["todo"], [b])

    def test_prefetch_on_includes_last_day(self):
        # 默认开启提前量时保持旧行为：昨天最优先、前天回填。
        y = (TODAY - timedelta(days=1)).isoformat()
        b = (TODAY - timedelta(days=2)).isoformat()
        r = archive.select_days([y, b], 2, TODAY, set(), {y, b}, prefetch=True)
        self.assertEqual(r["todo"], [y, b])

    def test_prefetch_off_still_backfills_overdue(self):
        # 不提前也不能丢掉回填：更早的过期日期仍要裁（上次失败/停机错过）。
        names = [(TODAY - timedelta(days=n)).isoformat() for n in (1, 2, 3)]
        r = archive.select_days(names, 2, TODAY, set(), set(names),
                                prefetch=False)
        # 回填组按日期升序（既有的「最早的先补」口径）
        self.assertEqual(r["todo"], sorted(names[1:]))


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

    def test_expiring_before_backfill_across_cams(self):
        """跨路排序：今晚要过期的日期必须排在别的路的回填之前。

        老实现按「摄像头顺序 + 每路日期」出队，run() 又按 todo 顺序吃 batch：
        排在前面的那路只要有积压（真机客厅一天 300 多片），后面那路今晚就会
        过期、明天源被删，永久丢失 —— 明明它更需要先做。
        """
        touch(os.path.join(self.root, "cam2", "2026-10-18", "10-00-00.mp4"))
        ledger.merge(self.root, "cam2", "2026-10-18", "10-00-00.mp4", 36000,
                     [[60.0, 120.0]], density=0.05, engine="pixel")
        cams = self.cams() + [{"id": "cam2", "name": "后门", "dir_name": "cam2"}]
        p = archive.plan(self.root, cams,
                         {"cam1": {"full_days": 7}, "cam2": {"full_days": 7}},
                         {"cam1": "virtual", "cam2": "virtual"}, today=TODAY)
        got = [(x["cam"], x["date"]) for x in p["todo"]]
        # cam1 的 10-18 与 cam2 的 10-18 都是最后机会，10-12 是回填 → 垫底
        self.assertIn(("cam2", "2026-10-18"), got)
        self.assertEqual(got[0], ("cam1", "2026-10-18"))   # 紧急组内不重排
        self.assertEqual(got[-1], ("cam1", "2026-10-12"))


class TestRun(_Env):
    def _run(self, batch=10, fail_names=(), part_bytes=4096):
        real_split = archive._split

        def fake_split(ffmpeg, src, out_base, wins, arc_dir, seg_sec=None):
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

    def test_exists_does_not_eat_batch(self):
        """幂等跳过的分片不能占 batch 预算。

        0.9.102 及以前 `used += 1` 在 _split 之前，于是「已归档过的那片」也
        算一片：batch=120 时重跑，前 120 个全是 exists → 本轮一片新的都没裁，
        界面转完却什么都没多出来。真机表现就是「点了归档，还是没产生文件」。
        """
        real_split = archive._split

        def fake_split(ffmpeg, src, out_base, wins, arc_dir, seg_sec=None):
            if os.path.isfile(out_base + ".idx.json"):
                return {"ok": True, "reason": "exists", "parts": 0, "files": []}
            out = out_base + ".c01.mp4"
            touch(out, 4096)
            return {"ok": True, "reason": "split", "files": [out]}
        # 10-18 已经有 idx（上一轮裁好了），但没有 .done
        touch(os.path.join(archive.archive_dir(self.root, "cam1", "2026-10-18"),
                           "10-00-00.idx.json"), 64)
        archive._split = fake_split
        try:
            r = archive.run(self.root, self.cams(), {"cam1": {"full_days": 7}},
                            self.modes(), None, today=TODAY, batch=1)
        finally:
            archive._split = real_split
        # 只花掉 1 个预算（10-12 那片），不是 2
        self.assertEqual(r["segments_used"], 1)
        # 10-12 真的被裁到了 —— 修复前这里仍是 False
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-12")))
        # 10-18 全部 exists → 也算整日裁完，收尾
        self.assertTrue(archive.is_done(
            archive.archive_dir(self.root, "cam1", "2026-10-18")))

    def test_step_progress_callback(self):
        steps = []
        real_split = archive._split

        def fake_split(ffmpeg, src, out_base, wins, arc_dir, seg_sec=None):
            out = out_base + ".c01.mp4"
            touch(out, 4096)
            return {"ok": True, "reason": "split", "files": [out]}
        archive._split = fake_split
        try:
            archive.run(self.root, self.cams(), {"cam1": {"full_days": 7}},
                        self.modes(), None, today=TODAY, batch=10,
                        on_step=steps.append)
        finally:
            archive._split = real_split
        self.assertTrue(steps)
        last = steps[-1]
        # 进度字段齐全，且最后一片时 done == total（界面靠这个显示 X/Y 片）
        for k in ("cam", "date", "done", "total", "used", "batch",
                  "files", "bytes", "days_done", "days_total"):
            self.assertIn(k, last)
        self.assertEqual(last["done"], last["total"])
        self.assertGreaterEqual(last["days_done"], 1)
        self.assertLessEqual(last["used"], last["batch"])


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
        self.segs_seen = []                  # 0.9.115：_split 收到的这一路分片长度
        real = archive._split

        def fake(ffmpeg, src, out_base, wins, arc_dir, seg_sec=None):
            used.append(os.path.basename(src))
            self.segs_seen.append(seg_sec)
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

    def test_cam_seg_passed_to_split(self):
        # 0.9.115：归档码率上限按「这一路」的分片长度算 —— 传错就会拿全局
        # 300 秒去套 60 秒的一路，源码率被低估 5 倍，上限形同虚设。
        self._run({"segment_sec": 60})
        self.assertEqual(self.segs_seen, [60.0])

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

    def test_purge_only_drops_own_idx(self):
        """只清空壳那一片的 idx，别把整日其它分片的成果一并作废。

        老实现清完空壳后把目录下**所有** `.idx.json` 删掉，于是这一天的产物
        全部失去幂等标记，下次整日重裁、同名覆盖：体积不涨，白烧几小时 IO。
        """
        touch(os.path.join(self.arc, "10-05-00.c01.mp4"), 4096)   # 好片
        touch(os.path.join(self.arc, "10-05-00.idx.json"), 90)
        touch(os.path.join(self.arc, "10-00-00.c01.mp4"), 262)    # 空壳
        touch(os.path.join(self.arc, "10-00-00.idx.json"), 90)
        self.assertTrue(archive._purge_empty_parts(self.arc))
        self.assertFalse(os.path.exists(os.path.join(self.arc, "10-00-00.idx.json")))
        self.assertTrue(os.path.exists(os.path.join(self.arc, "10-05-00.idx.json")))
        self.assertTrue(os.path.exists(os.path.join(self.arc, "10-05-00.c01.mp4")))

    def test_part_base(self):
        self.assertEqual(archive._part_base("18-40-22.c01.mp4"), "18-40-22")
        self.assertEqual(archive._part_base("18-40-22.c12.MP4"), "18-40-22")
        self.assertEqual(archive._part_base("weird.mp4"), "weird")


class TestSegEnd(unittest.TestCase):
    """分片结束时刻的三种口径（0.9.108）。

    真机废片的根因就在这里：mtime 只到秒，内容不足 1 秒的退化分片 mtime
    会落回起点那一秒，老实现要求 `st < mt` 就直接放弃 mtime、退回
    `st + seg_sec`，把后面整整 5 分钟的窗口全派给一片只有 2~4 秒的残片。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvrse")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, stamp):
        p = os.path.join(self.tmp, "10-00-00.mp4")
        with open(p, "wb") as f:
            f.write(b"0" * 64)
        os.utime(p, (stamp, stamp))
        return p

    def test_mtime_wins_when_between_start_and_nominal(self):
        """mtime 落在 (st, st+seg) → 用它：这是唯一能看出断录的口径。"""
        p = self._mk(_ts("2026-11-02", 10, 1, 24))
        self.assertEqual(
            archive._seg_end(p, "2026-11-02", 36000.0, 300.0, next_st=36300.0),
            36084.0)

    def test_mtime_at_start_falls_back_to_next_start(self):
        """退化分片：mtime 与文件名同秒 → 改看下一片起点，别退回 300 秒。

        真机 `15-57-19.mp4` 只有 4.6 秒内容、下一片是 15-57-24，老实现派给它
        300 秒窗口，三个相隔 150 秒的窗口裁出三个字节数完全一样的残片。
        """
        p = self._mk(_ts("2026-11-02", 10, 0, 0))
        self.assertEqual(archive._seg_end(p, "2026-11-02", 36000.0, 300.0,
                                          next_st=36005.0), 36005.0)

    def test_no_mtime_no_next_uses_nominal(self):
        """两者都不可用 → 名义片长兜底（旧行为）。"""
        p = self._mk(_ts("2026-10-01", 10, 0, 0))     # 跨天 mtime，不可信
        self.assertEqual(archive._seg_end(p, "2026-11-02", 36000.0, 300.0), 36300.0)

    def test_next_start_only_inside_window(self):
        """下一片起点超出名义窗口时不采信（断了 19 分钟那种，别吞成连续）。"""
        p = self._mk(_ts("2026-10-01", 10, 0, 0))
        self.assertEqual(archive._seg_end(p, "2026-11-02", 36000.0, 300.0,
                                          next_st=37200.0), 36300.0)

    def test_seg_seq_carries_next_start(self):
        base = os.path.join(self.tmp, "cam", "2026-11-02")
        os.makedirs(base)
        for n in ("10-00-00.mp4", "10-05-00.mp4", "10-05-20.mp4"):
            with open(os.path.join(base, n), "wb") as f:
                f.write(b"0" * 8)
        seq = archive._seg_seq(base)
        self.assertEqual(seq, [("10-00-00.mp4", 36000.0, 36300.0),
                               ("10-05-00.mp4", 36300.0, 36320.0),
                               ("10-05-20.mp4", 36320.0, None)])


class TestDoneDead(unittest.TestCase):
    """被跳过的死区间要落进 .done，否则整日 files=0 会被当成误标反复重做。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvrdead")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_and_read(self):
        archive.write_done(self.tmp, 0, 7)
        self.assertEqual(archive._done_files(self.tmp), 0)
        self.assertEqual(archive._done_dead(self.tmp), 7)

    def test_missing_field_defaults_zero(self):
        archive.write_done(self.tmp, 3)
        self.assertEqual(archive._done_dead(self.tmp), 0)

    def test_done_days_does_not_revive_dead_only_day(self):
        """files=0 + dead>0 = 这天确实裁不出东西，不是「读错台账」的误标。"""
        arc = os.path.join(self.tmp, "_archive", "cam1", "2026-10-18")
        archive.write_done(arc, 0, 9)
        led = os.path.join(self.tmp, "_ledger", "cam1")
        os.makedirs(led, exist_ok=True)
        with open(os.path.join(led, "2026-10-18.json"), "w", encoding="utf-8") as f:
            json.dump({"sources": {"10-00-00.mp4": {"windows": [[10, 20]]}}}, f)
        self.assertEqual(archive._done_days(self.tmp, "cam1", led), {"2026-10-18"})
        # 对照：files=0 且没有 dead（典型的误标）必须放回去重做
        archive.write_done(arc, 0, 0)
        self.assertEqual(archive._done_days(self.tmp, "cam1", led), set())


def _ts(day, h, m, s):
    import time as _t
    return _t.mktime(_t.strptime("%s %02d:%02d:%02d" % (day, h, m, s),
                                 "%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    unittest.main(verbosity=1)