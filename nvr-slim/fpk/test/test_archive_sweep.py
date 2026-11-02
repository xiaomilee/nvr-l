#!/usr/bin/env python3
"""夜间归档连跑（nvr_slim.archive_sweep）的判停逻辑。

只跑一轮 = 每晚只消化 archive_batch 片，剩下的第二天叠上新的，待办永远
归不了零。这里锁住四条：跑到 remaining==0、零进展就停、轮数/时长上限、
抢不到锁时立刻收手（手点那轮正在跑，不能两轮一起裁同一天）。
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "server"))
import nvr_slim  # noqa: E402


def _round(files=0, days=1, used=0, remaining=0, failed=0):
    return {"ok": not failed, "dry_run": False,
            "days": [{"cam": "cam1", "date": "2026-11-02"}] * days,
            "failed": [{"cam": "cam1", "error": "boom"}] * failed,
            "files": files, "bytes_out": files * 100,
            "segments_used": used, "remaining": remaining,
            "no_ledger": []}


class TestArchiveSweep(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.rounds_seen = []
        self._real_run = nvr_slim.run_archive
        self._real_load = nvr_slim.load_state_file
        self._real_save = nvr_slim.save_state_file
        # 不碰真实 var 目录
        nvr_slim.load_state_file = lambda p: {}
        nvr_slim.save_state_file = lambda p, o: None

    def tearDown(self):
        nvr_slim.run_archive = self._real_run
        nvr_slim.load_state_file = self._real_load
        nvr_slim.save_state_file = self._real_save

    def _seq(self, *rounds):
        def fake(dry_run=False, trigger="schedule", round_no=0):
            self.rounds_seen.append(round_no)
            r = rounds[min(len(self.calls), len(rounds) - 1)]
            self.calls.append(r)
            return dict(r)
        nvr_slim.run_archive = fake

    def test_runs_until_remaining_zero(self):
        self._seq(_round(files=3, used=5, remaining=2),
                  _round(files=2, used=4, remaining=1),
                  _round(files=1, used=2, remaining=0))
        res = nvr_slim.archive_sweep(trigger="schedule")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(res["rounds"], 3)
        self.assertEqual(res["files"], 6)          # 各轮累加
        self.assertEqual(res["segments_used"], 11)
        self.assertEqual(res["remaining"], 0)
        self.assertEqual(len(res["days"]), 3)
        self.assertEqual(self.rounds_seen, [1, 2, 3])   # 界面上要能显示「第 N 轮」

    def test_stops_when_no_progress(self):
        """全失败（花了预算但没产出）→ 停，别整夜空转重试同一批。"""
        self._seq(_round(files=0, days=0, used=12, remaining=3, failed=12),
                  _round(files=99, used=1, remaining=0))
        res = nvr_slim.archive_sweep()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(res["rounds"], 1)
        self.assertEqual(len(res["failed"]), 12)

    def test_stops_at_max_rounds(self):
        self._seq(_round(files=1, used=1, remaining=9))
        res = nvr_slim.archive_sweep(max_rounds=3)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(res["rounds"], 3)
        self.assertEqual(res["remaining"], 9)

    def test_lock_error_aborts_immediately(self):
        self._seq({"ok": False, "error": "已有归档任务在跑，等这一轮结束再点"})
        res = nvr_slim.archive_sweep()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("error", res)
        self.assertEqual(res["rounds"], 0)

    def test_should_stop_checked_between_rounds(self):
        """服务在停：每轮前问一次，不应再多跑一轮。"""
        flags = [False, True]
        self._seq(_round(files=1, used=1, remaining=5))
        res = nvr_slim.archive_sweep(
            should_stop=lambda: flags[min(len(self.calls), 1)])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(res["rounds"], 1)

    def test_budget_exhausted(self):
        """时长预算用完就收手（第一轮总要跑完，不能半途而废）。"""
        self._seq(_round(files=1, used=1, remaining=5))
        res = nvr_slim.archive_sweep(budget_sec=0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(res["rounds"], 1)


class TestStartArchive(unittest.TestCase):
    """异步启动：真机上同步等结果跑了 84 分钟才断在链路上（curl http=000，
    界面报「归档失败」，文件其实全裁好了）。现在立刻回 202，结果落 _ARCH_RESULT。"""

    def setUp(self):
        self._real_run = nvr_slim.run_archive
        self._real_load = nvr_slim.load_state_file
        self._real_save = nvr_slim.save_state_file
        nvr_slim.load_state_file = lambda p: {}
        nvr_slim.save_state_file = lambda p, o: None
        if nvr_slim._ARCH_LOCK.locked():
            nvr_slim._ARCH_LOCK.release()

    def tearDown(self):
        nvr_slim.run_archive = self._real_run
        nvr_slim.load_state_file = self._real_load
        nvr_slim.save_state_file = self._real_save
        nvr_slim._ARCH_PROG["running"] = False
        nvr_slim._ARCH_RESULT.clear()
        if nvr_slim._ARCH_LOCK.locked():
            nvr_slim._ARCH_LOCK.release()

    def _wait_idle(self, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if not nvr_slim._ARCH_LOCK.locked():
                return True
            time.sleep(0.01)
        return False

    def test_returns_immediately_then_stores_summary(self):
        started = threading.Event()
        release = threading.Event()

        def slow(dry_run=False, trigger="manual", round_no=0, lock_held=False):
            self.assertTrue(lock_held)          # 锁由 start_archive 持有
            started.set()
            release.wait(3)
            return {"ok": True, "days": [{}, {}], "files": 5, "bytes_out": 100,
                    "failed": [], "remaining": 1, "no_ledger": [], "at": "01:00"}

        nvr_slim.run_archive = slow
        r = nvr_slim.start_archive()
        self.assertTrue(r["ok"])
        self.assertTrue(r["async"])
        self.assertTrue(r["started"])           # 立刻返回，没等任务跑完
        self.assertTrue(started.wait(3))
        p = nvr_slim.archive_progress()
        self.assertIsNotNone(p["running"])      # 跑的时候能看到进度
        self.assertIsNone(p["result"])
        release.set()
        self.assertTrue(self._wait_idle())
        p = nvr_slim.archive_progress()
        self.assertIsNone(p["running"])
        self.assertTrue(p["result"]["done"])
        self.assertEqual(p["result"]["days_done"], 2)
        self.assertEqual(p["result"]["files"], 5)

    def test_second_start_rejected_while_running(self):
        gate = threading.Event()

        def hang(dry_run=False, trigger="manual", round_no=0, lock_held=False):
            gate.wait(3)
            return {"ok": True, "days": [], "failed": [], "files": 0,
                    "bytes_out": 0, "remaining": 0, "no_ledger": [], "at": "x"}

        nvr_slim.run_archive = hang
        self.assertTrue(nvr_slim.start_archive()["ok"])
        try:
            again = nvr_slim.start_archive()
            self.assertFalse(again["ok"])
            self.assertIn("已有归档任务在跑", again["error"])
        finally:
            gate.set()
            self.assertTrue(self._wait_idle())

    def test_worker_error_is_reported(self):
        def boom(dry_run=False, trigger="manual", round_no=0, lock_held=False):
            raise RuntimeError("ffmpeg 挂了")

        nvr_slim.run_archive = boom
        self.assertTrue(nvr_slim.start_archive()["ok"])
        self.assertTrue(self._wait_idle())
        res = nvr_slim.archive_progress()["result"]
        self.assertTrue(res["done"])
        self.assertFalse(res["ok"])
        self.assertIn("ffmpeg", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=1)