#!/usr/bin/env python3
"""保留策略与定时清理的行为测试。

重点守四条：
  - 边界：保留 4 天 = 今天 + 前 3 天，第 4 天前的才删（差一天就少留一批）
  - 保护：今天/未来目录、非日期目录、含 .part/.pending 的目录永不删
  - 两类分开：全时 4 天、缩时 90 天各自算，互不影响
  - 幂等：dry_run 不落盘；真删完再算一次计划为空
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import retention as rt  # noqa: E402

TODAY = date(2026, 10, 20)
CAMS = [{"id": "cam-01", "name": "车库入口"}, {"id": "cam-02", "name": "一楼大堂"}]


def mk(base, day, names=("a.mp4",)):
    d = os.path.join(base, day)
    os.makedirs(d, exist_ok=True)
    for n in names:
        with open(os.path.join(d, n), "wb") as f:
            f.write(b"x" * 10)
    return d


class TestPolicy(unittest.TestCase):
    def test_cutoff_boundary(self):
        # 保留 4 天：今天 20 号 → 保留 20/19/18/17，删 16 号及更早
        self.assertEqual(rt.cutoff(TODAY, 4), date(2026, 10, 17))
        self.assertEqual(rt.cutoff(TODAY, 1), TODAY)

    def test_clamp(self):
        self.assertEqual(rt.clamp_days("full_days", 0), 1)
        self.assertEqual(rt.clamp_days("full_days", 99), 30)
        self.assertEqual(rt.clamp_days("clip_days", 99999), 3650)
        self.assertEqual(rt.clamp_days("clip_days", "abc"), 90)

    def test_hhmm(self):
        self.assertEqual(rt.parse_hhmm("03:00"), "03:00")
        self.assertEqual(rt.parse_hhmm("3:5"), "03:05")
        self.assertEqual(rt.parse_hhmm("25:99"), "03:00")
        self.assertEqual(rt.parse_hhmm(None), "03:00")

    def test_due_and_next_run(self):
        self.assertFalse(rt.due(None, "03:00", datetime(2026, 10, 20, 2, 59)))
        self.assertTrue(rt.due("2026-10-19", "03:00", datetime(2026, 10, 20, 3, 1)))
        self.assertFalse(rt.due("2026-10-20", "03:00", datetime(2026, 10, 20, 3, 1)))
        self.assertEqual(rt.next_run("03:00", datetime(2026, 10, 20, 18, 0)),
                         datetime(2026, 10, 21, 3, 0))
        self.assertEqual(rt.next_run("03:00", datetime(2026, 10, 20, 1, 0)),
                         datetime(2026, 10, 20, 3, 0))


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvrret")
        self.cam_cfg = {}
        # 全时：20 今天 / 19 / 18 / 17 保留，16 删除
        for day in ("2026-10-20", "2026-10-19", "2026-10-18", "2026-10-17", "2026-10-16"):
            mk(os.path.join(self.root, "cam-01"), day)
        # 缩时：8 月 1 日保留（90 天内），7 月 1 日删除
        mk(os.path.join(self.root, "_clip", "cam-01"), "2026-08-01")
        mk(os.path.join(self.root, "_clip", "cam-01"), "2026-07-01")
        # 保护：含 .part；以及一个非日期目录
        mk(os.path.join(self.root, "cam-02"), "2026-10-10", ("b.mp4", "b.mp4.part"))
        os.makedirs(os.path.join(self.root, "cam-02", "lost+found"))
        # 未来日期
        mk(os.path.join(self.root, "cam-02"), "2026-10-25")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_plan_two_kinds(self):
        p = rt.plan(self.root, CAMS, self.cam_cfg, today=TODAY)
        paths = [i["path"] for i in p["delete"]]
        self.assertIn(os.path.join(self.root, "cam-01", "2026-10-16"), paths)
        self.assertIn(os.path.join(self.root, "_clip", "cam-01", "2026-07-01"), paths)
        # 边界：17 号恰好还在保留期内，不能删
        self.assertNotIn(os.path.join(self.root, "cam-01", "2026-10-17"), paths)
        # 今天不能进删除列表
        self.assertFalse(any(i["date"] == "2026-10-20" for i in p["delete"]))
        # 未来日期不碰
        self.assertFalse(any(i["date"] == "2026-10-25" for i in p["delete"]))
        # 版本：全时 4 天、缩时 90 天
        pol = {x["cam"]: x for x in p["policy"]}
        self.assertEqual(pol["cam-01"]["full_days"], 4)
        self.assertEqual(pol["cam-01"]["clip_days"], 90)
        self.assertGreater(p["freed_bytes"], 0)

    def test_part_files_protected(self):
        p = rt.plan(self.root, CAMS, self.cam_cfg, today=TODAY)
        prot = [i["path"] for i in p["protected"]]
        self.assertIn(os.path.join(self.root, "cam-02", "2026-10-10"), prot)
        self.assertNotIn(os.path.join(self.root, "cam-02", "2026-10-10"),
                         [i["path"] for i in p["delete"]])

    def test_unknown_dirs_untouched(self):
        p = rt.plan(self.root, CAMS, self.cam_cfg, today=TODAY)
        self.assertTrue(any(x.endswith("lost+found") for x in p["unknown"]))
        self.assertFalse(any("lost+found" in i["path"] for i in p["delete"]))

    def test_per_camera_override(self):
        mk(os.path.join(self.root, "_clip", "cam-02"), "2026-10-12")
        # 默认 90 天时 10-12 保留
        p1 = rt.plan(self.root, CAMS, {}, today=TODAY)
        self.assertFalse(any(i["path"].endswith("_clip/cam-02/2026-10-12")
                             or i["path"].endswith(r"_clip\cam-02\2026-10-12")
                             for i in p1["delete"]))
        # 该路缩时只留 7 天 → 要被删
        p2 = rt.plan(self.root, CAMS, {"cam-02": {"clip_days": 7}}, today=TODAY)
        self.assertTrue(any(i["date"] == "2026-10-12" and i["kind"] == "clip"
                            for i in p2["delete"]))

    def test_dir_name_and_legacy_merge(self):
        # 新目录用摄像头名称，旧 id 目录里的存量数据也要纳入清理
        cams = [{"id": "cam-01", "name": "客厅", "dir_name": "客厅"}, {"id": "cam-02", "name": "一楼大堂"}]
        mk(os.path.join(self.root, "客厅"), "2026-10-15")
        mk(os.path.join(self.root, "cam-01"), "2026-10-14")   # 升级前的旧目录
        p = rt.plan(self.root, cams, self.cam_cfg, today=TODAY)
        paths = [i["path"] for i in p["delete"]]
        self.assertIn(os.path.join(self.root, "客厅", "2026-10-15"), paths)
        self.assertIn(os.path.join(self.root, "cam-01", "2026-10-14"), paths)

    def test_dry_run_then_real(self):
        p = rt.plan(self.root, CAMS, self.cam_cfg, today=TODAY)
        r1 = rt.execute(p, dry_run=True)
        self.assertTrue(r1["dry_run"])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "cam-01", "2026-10-16")))
        r2 = rt.execute(p, dry_run=False)
        self.assertTrue(r2["ok"], r2["failed"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "cam-01", "2026-10-16")))
        # 保留期内的还在
        self.assertTrue(os.path.isdir(os.path.join(self.root, "cam-01", "2026-10-17")))
        # 幂等：再算一次没有可删的
        p2 = rt.plan(self.root, CAMS, self.cam_cfg, today=TODAY)
        self.assertEqual(p2["delete"], [])
        self.assertGreater(r2["freed_bytes"], 0)
        self.assertGreater(r2["files"], 0)

    def test_plan_is_sorted_and_jsonable(self):
        import json
        p = rt.plan(self.root, CAMS, self.cam_cfg, today=TODAY)
        keys = [(i["cam"], i["kind"], i["date"]) for i in p["delete"]]
        self.assertEqual(keys, sorted(keys))
        json.dumps(p)   # 能直接进 HTTP 响应

    def test_empty_root(self):
        empty = tempfile.mkdtemp(prefix="nvrempty")
        p = rt.plan(empty, CAMS, {}, today=TODAY)
        self.assertEqual(p["delete"], [])
        self.assertEqual(rt.execute(p)["ok"], True)
        os.rmdir(empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
