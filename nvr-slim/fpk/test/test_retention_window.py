#!/usr/bin/env python3
"""锁死「archive_days=总回溯、full_days=前N天全时、缩时不重复全时」语义。

设计约定（2026-10-28 讨论结论）：
  - archive_days 是回看得到的总天数（缩时窗口包住全时，不是并列相加）
  - full_days 是其中前多少天保留全时原画；其余天数只有浓缩 _archive 片段
  - 归档选日（archive.select_days）只处理全时已释放的日期，绝不把全时窗口内
    的当天/昨天再裁一份 _archive（避免重复录像空间）
"""
import os
import sys
import shutil
import tempfile
import unittest
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import retention as rt   # noqa: E402
import archive as arch   # noqa: E402

TODAY = date(2026, 10, 20)
FULL = 3                 # 目标：3 天全时
ARC = 7                  # 目标：7 天缩时（= 总回溯）
CAMS = [{"id": "cam-01", "name": "车库"}]


def mk(base, day, names=("a.mp4",)):
    d = os.path.join(base, day)
    os.makedirs(d, exist_ok=True)
    for n in names:
        with open(os.path.join(d, n), "wb") as f:
            f.write(b"x" * 10)
    return d


def day(k):
    return (TODAY - timedelta(days=k)).isoformat()


class TestWindowSemantics(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvrwin")
        self.cam_cfg = {"cam-01": {"full_days": FULL, "archive_days": ARC}}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def seed(self, full_back, arc_back):
        # 全时：今天往前 full_back 天（含今天，共 full_back+1 个目录）
        for k in range(full_back + 1):
            mk(os.path.join(self.root, "cam-01"), day(k))
        # 归档：今天往前 arc_back 天（含今天）
        for k in range(arc_back + 1):
            mk(os.path.join(self.root, "_archive", "cam-01"), day(k))

    def test_total_lookback_equals_archive_days(self):
        # 全时留 3 天（到 day-2），归档留 7 天（到 day-6）。
        # 多播几天越界目录：day-3 全时该删、day-7 归档该删、day-6 归档该留。
        self.seed(3, 7)
        p = rt.plan(self.root, CAMS, self.cam_cfg, today=TODAY)
        deleted = {(i["kind"], i["date"]) for i in p["delete"]}
        self.assertIn(("full", day(3)), deleted)       # 全时超 3 天 → 删
        self.assertIn(("archive", day(7)), deleted)    # 归档超 7 天 → 删
        self.assertNotIn(("archive", day(6)), deleted)  # 总回溯边界 day-6 仍留

    def test_full_nested_in_first_n_days(self):
        # 全时窗口=前3天，归档窗口=前7天；day-2 全时留、day-3 起只归档
        self.seed(5, 9)
        p = rt.plan(self.root, CAMS, self.cam_cfg, today=TODAY)
        deleted = {(i["kind"], i["date"]) for i in p["delete"]}
        self.assertNotIn(("full", day(2)), deleted)    # day-2 在 3 天全时内
        self.assertIn(("full", day(3)), deleted)       # day-3 超出全时
        self.assertNotIn(("archive", day(6)), deleted)  # day-6 在 7 天归档内
        self.assertIn(("archive", day(7)), deleted)    # day-7 超出归档


class TestArchiveNoDuplication(unittest.TestCase):
    def test_select_days_skips_full_window(self):
        # full=3 → cut=day-2。今天/昨天(d0,d-1) 必须跳过；d-2(边界,明天过期)
        # 作为 expiring 进 todo；d-3/d-4 回填进 todo。即全时窗口内当天与昨天
        # 绝不重复裁 _archive。
        days = [day(k) for k in (0, 1, 2, 3, 4)]
        r = arch.select_days(days, FULL, TODAY, set(), set(days))
        self.assertEqual(set(r["todo"]), {day(2), day(3), day(4)})
        self.assertEqual(r["no_ledger"], [])
        self.assertNotIn(day(0), r["todo"])
        self.assertNotIn(day(1), r["todo"])

    def test_select_days_cut_moves_with_full_days(self):
        days = [day(k) for k in (0, 1, 2)]
        led = set(days)
        r2 = arch.select_days(days, 2, TODAY, set(), led)
        self.assertNotIn(day(0), r2["todo"])   # full=2 → 今天跳过
        self.assertIn(day(1), r2["todo"])      # 昨天(边界)进
        r3 = arch.select_days(days, 3, TODAY, set(), led)
        self.assertNotIn(day(0), r3["todo"])
        self.assertNotIn(day(1), r3["todo"])   # full=3 → 今天/昨天都跳过
        self.assertIn(day(2), r3["todo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
