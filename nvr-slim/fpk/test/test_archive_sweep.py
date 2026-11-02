#!/usr/bin/env python3
"""夜间归档连跑（nvr_slim.archive_sweep）的判停逻辑。

只跑一轮 = 每晚只消化 archive_batch 片，剩下的第二天叠上新的，待办永远
归不了零。这里锁住四条：跑到 remaining==0、零进展就停、轮数/时长上限、
抢不到锁时立刻收手（手点那轮正在跑，不能两轮一起裁同一天）。
"""
import os
import sys
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
