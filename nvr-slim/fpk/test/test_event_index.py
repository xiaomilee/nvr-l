#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件索引（event_index）测试。

守的是四件事：

  1. **幂等**：同一份台账反复灌进索引，行数不变、内容不变。索引天天被同步，
     不幂等就等于每秒都在制造"新事件"。
  2. **投影可重建**：rebuild_index 清空重灌的结果必须和增量同步一致，
     而且台账里消失的事件必须真的从索引里消失（只增不删的索引会永远留着
     ​点进去播不了的事件）。
  3. **过滤器不误伤**：类别过滤按整词（,person,）匹配，不能把 two_wheeler
     当成 person，也不能让半截词命中。
  4. **状态只许前进**：正在进行的窗口不许被标成"已入库"。

跑：python test/test_event_index.py
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import event  # noqa: E402
import event_index  # noqa: E402

DATE = "2026-11-08"


def hms(h, m, s=0):
    return float(h * 3600 + m * 60 + s)


def ev(cam="c1", start=None, end=None, cls=None, state="ended", date=DATE):
    """造一条事件（字段与 event.from_ledger 一致）。"""
    s = hms(7, 30) if start is None else float(start)
    e = s + 10.0 if end is None else float(end)
    cls = {"person": 1} if cls is None else cls
    return {
        "id": event.event_id(cam, date, s), "cam": cam, "date": date,
        "start": s, "end": e,
        "start_at": event.iso_at(date, s), "end_at": event.iso_at(date, e),
        "duration": e - s,
        "categories": event.categories_of(cls),
        "cls": dict(cls), "density": 0.4,
        "confidence_max": None, "motion_confirmed": None,
        "state": state, "source_segment": "07-30-00.mp4",
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvrevent")
        self.path = event_index.db_path(self.tmp)
        self.conn = event_index.connect(self.path)
        event_index.ensure(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def count(self):
        return self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]


class TestSchema(Base):
    def test_db_path_in_var_dir(self):
        self.assertEqual(self.path, os.path.join(self.tmp, "event_index.db"))

    def test_ensure_is_idempotent(self):
        event_index.ensure(self.conn)
        event_index.ensure(self.conn)
        row = self.conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        self.assertEqual(row["v"], event_index.SCHEMA_VERSION)

    def test_connect_creates_parent_dir(self):
        p = os.path.join(self.tmp, "sub", "deep", "idx.db")
        conn = event_index.connect(p)
        try:
            event_index.ensure(conn)
            self.assertTrue(os.path.isfile(p))
        finally:
            conn.close()


class TestUpsert(Base):
    def test_insert_new(self):
        r = event_index.upsert(self.conn, [ev()])
        self.assertEqual(r, {"new": 1, "updated": 0, "same": 0})
        self.assertEqual(self.count(), 1)

    def test_same_input_twice_is_noop(self):
        """幂等的硬指标：第二次一条都不该写。"""
        event_index.upsert(self.conn, [ev()])
        r = event_index.upsert(self.conn, [ev()])
        self.assertEqual(r, {"new": 0, "updated": 0, "same": 1})
        self.assertEqual(self.count(), 1)

    def test_extends_end(self):
        event_index.upsert(self.conn, [ev(end=hms(7, 30, 10))])
        eid = ev()["id"]
        r = event_index.upsert(self.conn, [ev(end=hms(7, 31))])
        self.assertEqual(r["updated"], 1)
        got = event_index.get(self.conn, eid)
        self.assertEqual(got["end"], hms(7, 31))
        self.assertEqual(got["end_at"], "2026-11-08 07:31:00")

    def test_active_is_not_marked_indexed(self):
        """正在进行的窗口不许标"已入库"（结束时间还会变）。"""
        event_index.upsert(self.conn, [ev(state="active")])
        got = event_index.get(self.conn, ev()["id"])
        self.assertEqual(got["state"], "active")

    def test_ended_becomes_indexed(self):
        event_index.upsert(self.conn, [ev(state="ended")])
        got = event_index.get(self.conn, ev()["id"])
        self.assertEqual(got["state"], "indexed")

    def test_state_never_goes_back(self):
        event_index.upsert(self.conn, [ev(state="ended")])
        event_index.upsert(self.conn, [ev(state="active", end=hms(7, 31))])
        self.assertEqual(event_index.get(self.conn, ev()["id"])["state"], "indexed")

    def test_bad_rows_skipped(self):
        r = event_index.upsert(self.conn, [None, {}, {"id": ""}, "x"])
        self.assertEqual(r, {"new": 0, "updated": 0, "same": 0})

    def test_row_round_trip(self):
        """写进去再读出来要一模一样：前端不该为"列表"和"详情"认两套结构。"""
        src = ev(cls={"person": 2, "vehicle": 1})
        event_index.upsert(self.conn, [src])
        got = event_index.get(self.conn, src["id"])
        for k in ("id", "cam", "date", "start", "end", "start_at", "end_at",
                  "duration", "categories", "cls", "motion_confirmed",
                  "confidence_max", "source_segment"):
            self.assertEqual(got[k], src[k], k)

    def test_unknown_motion_stays_null(self):
        """mv 无从判断 → NULL，不是 0。0 会被读成"确认没有运动"。"""
        event_index.upsert(self.conn, [ev()])
        row = self.conn.execute("SELECT motion_confirmed m FROM events").fetchone()
        self.assertIsNone(row["m"])
        self.assertIsNone(event_index.get(self.conn, ev()["id"])["motion_confirmed"])


class TestQuery(Base):
    def setUp(self):
        super().setUp()
        event_index.upsert(self.conn, [
            ev("c1", hms(7, 30), hms(7, 40), {"person": 2}),
            ev("c1", hms(9, 0), hms(9, 10), {"vehicle": 1}),
            ev("c2", hms(8, 0), hms(8, 10), {"two_wheeler": 3}),
        ])

    def at(self, h, m, s=0):
        return event.iso_at(DATE, hms(h, m, s))

    def test_all(self):
        self.assertEqual(len(event_index.query(self.conn)), 3)

    def test_time_window_is_intersection(self):
        """跨窗口边界的事件两边都要出现，否则"整点那一刻没有事件"。"""
        got = event_index.query(self.conn, end_at=self.at(7, 35))
        self.assertEqual(len(got), 1)
        got = event_index.query(self.conn, start_at=self.at(7, 35))
        self.assertEqual(len(got), 3)
        # 07:30-07:40 这条跨过了 07:35：起点在窗口外也得出现
        self.assertEqual(got[0]["start"], hms(7, 30))

    def test_cam_filter(self):
        got = event_index.query(self.conn, cam="c2")
        self.assertEqual([e["cam"] for e in got], ["c2"])
        self.assertEqual(event_index.query(self.conn, cam="nope"), [])

    def test_cat_filter(self):
        got = event_index.query(self.conn, cat="person")
        self.assertEqual([e["cam"] for e in got], ["c1"])
        self.assertEqual(len(got[0]["categories"]), 1)

    def test_cat_filter_no_partial_match(self):
        """two_wheeler 不能命中 person，也不能被 'wheel' 半截词命中。"""
        self.assertEqual([e["cam"] for e in event_index.query(self.conn, cat="two_wheeler")],
                         ["c2"])
        self.assertEqual(event_index.query(self.conn, cat="wheel"), [])
        self.assertEqual(event_index.query(self.conn, cat="two"), [])

    def test_limit_keeps_newest_in_ascending_order(self):
        """limit 取的是"最近的 N 条"，但返回仍按时间正序（跟时间轴一致）。"""
        got = event_index.query(self.conn, limit=2)
        self.assertEqual([e["start"] for e in got], [hms(8, 0), hms(9, 0)])

    def test_get_missing(self):
        self.assertIsNone(event_index.get(self.conn, "nope_20261108_073000"))


class TestSync(Base):
    def loader(self, evs):
        return lambda d: list(evs)

    def test_sync_writes_and_records_sig(self):
        r = event_index.sync_day(self.conn, DATE, self.loader([ev()]), sig="abc")
        self.assertFalse(r["skipped"])
        self.assertEqual(r["events"], 1)
        self.assertEqual(self.count(), 1)
        self.assertEqual(event_index.days_synced(self.conn), [DATE])

    def test_same_sig_skips(self):
        event_index.sync_day(self.conn, DATE, self.loader([ev()]), sig="abc")
        r = event_index.sync_day(self.conn, DATE, self.loader([ev()]), sig="abc")
        self.assertTrue(r["skipped"])

    def test_changed_sig_resyncs(self):
        event_index.sync_day(self.conn, DATE, self.loader([ev()]), sig="abc")
        r = event_index.sync_day(self.conn, DATE, self.loader([ev()]), sig="def")
        self.assertFalse(r["skipped"])
        self.assertEqual(self.count(), 1)

    def test_resync_drops_removed_events(self):
        """台账重判后事件变少（口径改了/忽略区改了），索引必须跟着少。"""
        event_index.sync_day(self.conn, DATE, self.loader([
            ev("c1", hms(7, 30)), ev("c1", hms(8, 30))]), sig="a")
        self.assertEqual(self.count(), 2)
        event_index.sync_day(self.conn, DATE, self.loader([ev("c1", hms(7, 30))]),
                             sig="b")
        self.assertEqual(self.count(), 1)

    def test_loader_failure_keeps_index(self):
        """loader 抛错时索引必须原样保留，不能留下"删了一半"的空窗。"""
        event_index.sync_day(self.conn, DATE, self.loader([ev()]), sig="a")

        def boom(d):
            raise RuntimeError("台账读坏了")

        with self.assertRaises(RuntimeError):
            event_index.sync_day(self.conn, DATE, boom, sig="b")
        self.assertEqual(self.count(), 1)

    def test_bad_date(self):
        r = event_index.sync_day(self.conn, "2026/11/08", self.loader([ev()]))
        self.assertTrue(r["skipped"])
        self.assertIn("bad date", r["error"])

    def test_rows_without_id_skipped(self):
        r = event_index.sync_day(self.conn, DATE, self.loader([{}, {"id": ""}]))
        self.assertEqual(r["events"], 0)
        self.assertEqual(self.count(), 0)


class TestRebuild(Base):
    def test_rebuild_from_scratch(self):
        """索引只是投影：清空重灌必须得到同一份结果。"""
        evs = [ev("c1", hms(7, 30)), ev("c2", hms(8, 0))]
        event_index.upsert(self.conn, evs)          # 先有脏数据（含别的日子）
        stale = ev("c9", hms(6, 0))
        event_index.upsert(self.conn, [stale])
        seen = []

        def loader(d):
            return list(evs)

        r = event_index.rebuild_index(self.conn, [DATE], loader, progress=seen.append)
        self.assertEqual(r, {"days": 1, "events": 2})
        self.assertEqual(self.count(), 2)
        self.assertIsNone(event_index.get(self.conn, stale["id"]))
        self.assertEqual(len(seen), 1)

    def test_rebuild_clears_day_sync(self):
        event_index.sync_day(self.conn, DATE, lambda d: [ev()], sig="a")
        event_index.rebuild_index(self.conn, [], lambda d: [])
        self.assertEqual(event_index.days_synced(self.conn), [])

    def test_stats(self):
        event_index.upsert(self.conn, [ev("c1"), ev("c2", hms(9, 0))])
        st = event_index.stats(self.conn)
        self.assertEqual(st["events"], 2)
        self.assertEqual(st["days"], 1)
        self.assertEqual(st["cams"], 2)
        self.assertEqual(st["from"], "2026-11-08 07:30:00")

    def test_stats_on_empty(self):
        st = event_index.stats(self.conn)
        self.assertEqual(st["events"], 0)
        self.assertIsNone(st["from"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
