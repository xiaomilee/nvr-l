#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件检索口径（批次 8 / 0.9.126）：多类别 OR、多机位 OR、移动侦测、最短时长。

守的四件事：

  1. **同维 OR、跨维 AND**：选了「人员 + 车辆」要两条都出来；再叠机位，就得
     既在这一路、又属于这两类。这条口径写进 SQL 一次，前端不再各写一遍。
  2. **移动侦测不是第四种类别**：`motion` 等价于「一条 AI 类别都没有」，
     不能把有 person 的事件也筛进来，也不能漏掉 categories 为空的行。
  3. **不认识的类别要报错**：`parse_cats("persn")` 必须回 bad，调用方拿它回
     一句人话。静默忽略的话用户会以为筛过了，看到一屏比预期多的事件。
  4. **单值参数不许失效**：`cat=person` / `cam=cam-01` 这些老书签还在用，
     多值能力不能把单值语义改掉。

用内存 SQLite 直接打 `event_index.query()`，不需要台账文件、不需要起服务 ——
这一层是纯查询，构造真实的分片目录只会把测试变成慢动作。

跑：python test/test_event_filter.py
"""
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import event          # noqa: E402
import event_index    # noqa: E402


def ev(eid, cam, start, end, cats, mv=1):
    """造一条事件 dict（字段只要够 to_row 用；口径与 event.from_ledger 一致）。"""
    date = "2026-11-08"
    hh = int(start // 3600)
    mm = int((start % 3600) // 60)
    ss = int(start % 60)
    e_hh = int(end // 3600)
    e_mm = int((end % 3600) // 60)
    e_ss = int(end % 60)
    return {
        "id": eid, "cam": cam, "date": date,
        "start": float(start), "end": float(end),
        "start_at": "%s %02d:%02d:%02d" % (date, hh, mm, ss),
        "end_at": "%s %02d:%02d:%02d" % (date, e_hh, e_mm, e_ss),
        "source_segment": None,
        "categories": list(cats),
        "cls": {k: 1 for k in cats},
        "density": 1.0, "confidence_max": None,
        "motion_confirmed": (None if mv < 0 else bool(mv)),
        "state": "ended",
    }


SAMPLES = [
    # 09:00 人，10 秒
    ev("cam-01_20261108_090000", "cam-01", 9 * 3600, 9 * 3600 + 10, ["person"]),
    # 09:05 车，20 秒
    ev("cam-01_20261108_090500", "cam-01", 9 * 3600 + 300, 9 * 3600 + 320,
       ["vehicle"]),
    # 09:10 人和两轮车，120 秒
    ev("cam-02_20261108_091000", "cam-02", 9 * 3600 + 600, 9 * 3600 + 720,
       ["person", "two_wheeler"]),
    # 09:15 只有移动（帧差确认），30 秒
    ev("cam-02_20261108_091500", "cam-02", 9 * 3600 + 900, 9 * 3600 + 930, []),
    # 09:20 只有移动、且这一路没有帧差源（mv=-1），5 秒
    ev("cam-03_20261108_092000", "cam-03", 9 * 3600 + 1200, 9 * 3600 + 1205,
       [], mv=-1),
]


class FilterCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row   # 与 event_index.connect() 一致
        event_index.ensure(self.conn)
        event_index.upsert(self.conn, SAMPLES)

    def tearDown(self):
        self.conn.close()

    def q(self, **kw):
        return event_index.query(self.conn, "2026-11-08 00:00:00",
                                 "2026-11-08 23:59:59", **kw)

    def ids(self, evs):
        return [e["id"] for e in evs]


class TestSameDimOr(FilterCase):
    def test_multi_cat_is_or(self):
        got = self.ids(self.q(cats=["person", "vehicle"]))
        self.assertEqual(got, ["cam-01_20261108_090000",
                               "cam-01_20261108_090500",
                               "cam-02_20261108_091000"])

    def test_single_cat_backward_compatible(self):
        got = self.ids(self.q(cat="person"))
        self.assertEqual(got, ["cam-01_20261108_090000",
                               "cam-02_20261108_091000"])

    def test_multi_cam_is_or(self):
        got = self.ids(self.q(cams=["cam-01", "cam-03"]))
        self.assertEqual(len(got), 3)
        self.assertNotIn("cam-02_20261108_091000", got)

    def test_single_cam_backward_compatible(self):
        got = self.ids(self.q(cam="cam-02"))
        self.assertEqual(got, ["cam-02_20261108_091000",
                               "cam-02_20261108_091500"])

    def test_cams_win_over_single_cam(self):
        """两个参数同时给时以多值为准：否则前端传了 cams 却被 cam 悄悄改窄。"""
        got = self.ids(self.q(cam="cam-03", cams=["cam-01"]))
        self.assertEqual(len(got), 2)
        self.assertNotIn("cam-03_20261108_092000", got)


class TestMotion(FilterCase):
    def test_motion_only_means_no_ai_category(self):
        got = self.ids(self.q(motion_only=True))
        self.assertEqual(got, ["cam-02_20261108_091500",
                               "cam-03_20261108_092000"])

    def test_motion_plus_person_is_or(self):
        got = self.ids(self.q(cats=["person"], motion_only=True))
        self.assertEqual(got, ["cam-01_20261108_090000",
                               "cam-02_20261108_091000",
                               "cam-02_20261108_091500",
                               "cam-03_20261108_092000"])

    def test_motion_events_carry_null_motion_confirmed(self):
        """mv=-1 的事件仍是移动侦测，但"有没有运动"是未知 —— 不能写成 False。"""
        got = [e for e in self.q(motion_only=True) if e["cam"] == "cam-03"]
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["motion_confirmed"])
        self.assertEqual(got[0]["kind"], event.KIND_MOTION)


class TestDuration(FilterCase):
    def test_min_dur_filters_short_events(self):
        # 09:05 那条车只有 20 秒，25 秒门槛下应当消失
        got = self.ids(self.q(min_dur=25))
        self.assertEqual(got, ["cam-02_20261108_091000",
                               "cam-02_20261108_091500"])

    def test_min_dur_zero_keeps_everything(self):
        self.assertEqual(len(self.q(min_dur=0)), len(SAMPLES))

    def test_duration_is_inclusive(self):
        """恰好等于 30 秒的要留下：用户填 30 想的是"不少于 30 秒"。"""
        got = [e for e in self.q(min_dur=30)]
        self.assertIn("cam-02_20261108_091500", self.ids(got))


class TestCrossDimAnd(FilterCase):
    def test_all_dims_combine_with_and(self):
        got = self.ids(self.q(cams=["cam-02"], motion_only=True, min_dur=10))
        self.assertEqual(got, ["cam-02_20261108_091500"])

    def test_empty_result_is_empty_not_everything(self):
        got = self.q(cams=["cam-03"], cats=["vehicle"])
        self.assertEqual(got, [])

    def test_limit_still_applies_in_sql(self):
        got = self.q(limit=2)
        self.assertEqual(len(got), 2)
        # 倒序取最近 2 条再翻正序：留下的是最后两条
        self.assertEqual(self.ids(got), ["cam-02_20261108_091500",
                                         "cam-03_20261108_092000"])


class TestParseCats(unittest.TestCase):
    def test_split_trim_dedup(self):
        cats, motion, bad = event_index.parse_cats(
            " person , vehicle , person ,")
        self.assertEqual(cats, ["person", "vehicle"])
        self.assertFalse(motion)
        self.assertEqual(bad, [])

    def test_motion_is_flagged_not_listed(self):
        cats, motion, bad = event_index.parse_cats("motion")
        self.assertEqual(cats, [])
        self.assertTrue(motion)
        self.assertEqual(bad, [])

    def test_unknown_collected_for_error(self):
        cats, motion, bad = event_index.parse_cats("person,dog,dog")
        self.assertEqual(cats, ["person"])
        self.assertEqual(bad, ["dog"])          # 去重，只报一次

    def test_empty_is_no_filter(self):
        self.assertEqual(event_index.parse_cats(""), ([], False, []))
        self.assertEqual(event_index.parse_cats(None), ([], False, []))


class TestKindAndSummary(FilterCase):
    def test_rows_carry_kind(self):
        got = {e["id"]: e["kind"] for e in self.q()}
        self.assertEqual(got["cam-01_20261108_090000"], event.KIND_AI)
        self.assertEqual(got["cam-02_20261108_091500"], event.KIND_MOTION)

    def test_summary_counts_motion_separately(self):
        s = event.summary_of(self.q())
        self.assertEqual(s["count"], len(SAMPLES))
        self.assertEqual(s["motion"], 2)
        self.assertEqual(s["by_category"]["person"], 2)
        # 移动侦测不进 by_category：它不是类别
        self.assertNotIn(event.MOTION, s["by_category"])

    def test_kind_of_shared_by_both_paths(self):
        """台账路径（event.from_ledger）与索引路径（from_row）必须同口径。"""
        self.assertEqual(event.kind_of([]), event.KIND_MOTION)
        self.assertEqual(event.kind_of(["person"]), event.KIND_AI)
        self.assertEqual(event.kind_of(None), event.KIND_MOTION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
