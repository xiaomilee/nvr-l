#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件模型（event）测试。

守的是四件事：

  1. **身份稳定**：event_id 只由「哪一路 + 何时开始」决定。事件变长、重判、
     重建索引都不得换 id —— 换了，用户昨天点过的链接今天就打不开。
     反过来，不同起点的两条事件绝不能撞成一条。
  2. **生命周期只许前进**：台账每 300 秒重判一次，同一段会被反复重算；
     允许回退的话，已结束的事件会在下一次重判时变回"进行中"。
     唯一的例外是"正在进行的窗口不许标已入库"。
  3. **未知不等于没有**：mv=-1（没有帧差源）必须是 None，不能写成 False；
     置信度台账里没有就留 None，不许编一个 0.5。
  4. **定位口径**：事件起点前 10 秒 → 该打开哪个分片、偏移多少；
     定位不到必须有明确的 missing_reason 与中文原因，不许静默失败。

跑：python test/test_event.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import detect  # noqa: E402
import event  # noqa: E402
import ledger  # noqa: E402

DATE = "2026-11-08"


def hms(h, m, s=0):
    return float(h * 3600 + m * 60 + s)


def ledger_data(ai=(), mv=(), d=0.5):
    """造一份台账：ai/mv 都是当天钟点秒的窗口列表。

    ai 每项 (s, e, cls)；mv 每项 (s, e)。来源名沿用真实格式
    （分片名 + "ai:" 前缀），因为 events_of 靠前缀区分两档 gap。
    """
    srcs = {}
    if mv:
        srcs["07-30-00.mp4"] = {"start": hms(7, 30), "d": d,
                                "windows": [[s, e] for s, e in mv]}
    if ai:
        srcs["ai:07-30-00.mp4"] = {"start": hms(7, 30), "d": d,
                                  "windows": [[s, e, c] for s, e, c in ai]}
    return {"v": 1, "cam": "c1", "date": DATE, "sources": srcs}


class TestId(unittest.TestCase):
    def test_format(self):
        self.assertEqual(event.event_id("cam-01", DATE, hms(7, 30, 5)),
                         "cam-01_20261108_073005")

    def test_bad_date_rejected(self):
        for bad in ("", None, "2026/11/08", "20261108", "2026-11-8"):
            self.assertIsNone(event.event_id("cam-01", bad, 100), bad)

    def test_parse_round_trip(self):
        eid = event.event_id("cam-01", DATE, hms(7, 30, 5))
        got = event.parse_id(eid)
        self.assertEqual(got["cam"], "cam-01")
        self.assertEqual(got["date"], DATE)
        self.assertEqual(got["start"], hms(7, 30, 5))

    def test_parse_cam_with_underscore(self):
        """用户自建的 id 允许下划线：必须从右往左切，否则 cam 会被切坏。"""
        eid = event.event_id("cam_front", DATE, hms(23, 59, 59))
        self.assertEqual(event.parse_id(eid)["cam"], "cam_front")

    def test_parse_rejects_garbage(self):
        for bad in ("", None, "abc", "cam-01_20261108", "cam-01_20261108_256000",
                    "cam-01_2026-11-08_073005", "cam-01_20261108_0730"):
            self.assertIsNone(event.parse_id(bad), bad)

    def test_id_does_not_contain_end(self):
        """延长结束时间不产生新 id —— 这是「事件变长」的核心保证。"""
        a = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(7, 30), hms(7, 30, 10), {"person": 1})]))
        b = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(7, 30), hms(7, 30, 40), {"person": 1})]))
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(a[0]["id"], b[0]["id"])
        self.assertGreater(b[0]["end"], a[0]["end"])


class TestCategories(unittest.TestCase):
    def test_matches_detect_categories(self):
        """结构守卫：类别体系只有一个来源（detect.CATEGORIES），
        event 里那份只是拷贝，顺序不能漂。"""
        self.assertEqual(event.CATEGORIES, detect.CATEGORIES)

    def test_fixed_order(self):
        got = event.categories_of({"vehicle": 2, "person": 1,
                                   "two_wheeler": 3})
        self.assertEqual(got, ["person", "two_wheeler", "vehicle"])

    def test_zero_and_unknown_dropped(self):
        self.assertEqual(event.categories_of({"person": 0, "dog": 5}), [])
        self.assertEqual(event.categories_of({"dog": 5}), [])
        self.assertEqual(event.categories_of(None), [])
        self.assertEqual(event.categories_of({}), [])

    def test_summary_counts_events_not_hits(self):
        """一个人出现 5 帧是一条事件，不是 5 个人。"""
        evs = [{"categories": ["person"]}, {"categories": ["person", "vehicle"]},
               {"categories": []}]
        s = event.summary_of(evs)
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["by_category"]["person"], 2)
        self.assertEqual(s["by_category"]["vehicle"], 1)


class TestLifecycle(unittest.TestCase):
    def test_rank_order(self):
        ranks = [event.state_rank(s) for s in event.STATES]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(event.state_rank("nonsense"), -1)

    def test_forward(self):
        self.assertEqual(event.next_state("detected", "confirmed"), "confirmed")
        self.assertEqual(event.next_state("ended", "indexed"), "indexed")

    def test_no_backward(self):
        """重判把同一段再算一遍时，已结束的事件不能变回进行中。"""
        self.assertEqual(event.next_state("ended", "active"), "ended")
        self.assertEqual(event.next_state("indexed", "ended"), "indexed")

    def test_empty_sides(self):
        self.assertEqual(event.next_state("", "ended"), "ended")
        self.assertEqual(event.next_state("ended", ""), "ended")
        self.assertEqual(event.next_state(None, None), None)

    def test_unknown_state_does_not_win(self):
        self.assertEqual(event.next_state("ended", "garbage"), "ended")
        self.assertEqual(event.next_state("garbage", "ended"), "ended")

    def test_indexed_never_covers_active(self):
        """正在进行的窗口不许标"已入库"：它的结束时间随时会变。"""
        self.assertEqual(event.next_state("active", "indexed"), "active")


class TestFromLedger(unittest.TestCase):
    def test_fields(self):
        data = ledger_data(ai=[(hms(7, 30), hms(7, 30, 10), {"person": 2})],
                           mv=[(hms(7, 29, 50), hms(7, 30, 20))])
        evs = event.from_ledger("c1", DATE, data, now_sec=hms(9, 0))
        self.assertEqual(len(evs), 1)
        e = evs[0]
        # id 跟着**最终**起点走：运动簇把起点前推到 07:29:50，id 就必须是
        # 07:29:50 —— 否则同一段活动换一次判定口径就换一个 id。
        self.assertEqual(e["id"], "c1_20261108_072950")
        self.assertEqual(e["cam"], "c1")
        self.assertEqual(e["date"], DATE)
        self.assertEqual(e["start"], hms(7, 29, 50))   # 被运动簇向外定边界
        self.assertEqual(e["end"], hms(7, 30, 20))
        self.assertEqual(e["start_at"], "2026-11-08 07:29:50")
        self.assertEqual(e["duration"], 30.0)
        self.assertEqual(e["categories"], ["person"])
        self.assertEqual(e["cls"], {"person": 2})
        self.assertTrue(e["motion_confirmed"])
        self.assertEqual(e["state"], "ended")

    def test_active_when_tail_near_now(self):
        data = ledger_data(ai=[(hms(7, 30), hms(7, 30, 10), {"person": 1})])
        evs = event.from_ledger("c1", DATE, data, now_sec=hms(7, 30, 30))
        self.assertEqual(evs[0]["state"], "active")

    def test_active_tail_window_is_bounded(self):
        """超过 LIVE_TAIL_SEC 就不算进行中：否则列表里永远挂着"进行中"。"""
        data = ledger_data(ai=[(hms(7, 30), hms(7, 30, 10), {"person": 1})])
        now = hms(7, 30, 10) + event.LIVE_TAIL_SEC + 1
        self.assertEqual(event.from_ledger("c1", DATE, data, now_sec=now)[0]["state"],
                         "ended")

    def test_history_has_no_active(self):
        """历史日期不传 now_sec，任何窗口都只能是已结束。"""
        data = ledger_data(ai=[(hms(23, 59), hms(23, 59, 30), {"person": 1})])
        self.assertEqual(event.from_ledger("c1", DATE, data)[0]["state"], "ended")

    def test_motion_unknown_is_none(self):
        """mv=-1（这一路没有帧差源）必须是 None，不能写成 False。"""
        evs = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(7, 30), hms(7, 30, 10), {"person": 1})]))
        self.assertIsNone(evs[0]["motion_confirmed"])

    def test_confidence_is_never_invented(self):
        evs = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(7, 30), hms(7, 30, 10), {"person": 1})]))
        self.assertIsNone(evs[0]["confidence_max"])

    def test_source_segment_matches_play_entry(self):
        """source_segment 必须等于 locate() 打开的那一片：列表标一片、点进去
        打开另一片，是最容易被当成 bug 的那种不一致。"""
        start = hms(7, 30, 5)
        data = ledger_data(ai=[(start, hms(7, 30, 15), {"person": 1})])
        starts = [hms(7, 25), hms(7, 30)]
        evs = event.from_ledger("c1", DATE, data, starts=starts)
        self.assertEqual(evs[0]["source_segment"], "07-30-00.mp4")
        self.assertEqual(evs[0]["source_segment"],
                         event.locate(evs[0]["start"], starts)["segment"])

    def test_source_segment_after_first(self):
        data = ledger_data(ai=[(hms(7, 32, 5), hms(7, 32, 15), {"person": 1})])
        evs = event.from_ledger("c1", DATE, data,
                                starts=[hms(7, 25), hms(7, 30)])
        self.assertEqual(evs[0]["source_segment"], "07-30-00.mp4")

    def test_source_segment_none_before_first(self):
        data = ledger_data(ai=[(hms(7, 0, 5), hms(7, 0, 15), {"person": 1})])
        evs = event.from_ledger("c1", DATE, data, starts=[hms(7, 25)])
        self.assertIsNone(evs[0]["source_segment"])

    def test_bad_input_is_empty(self):
        for bad in (None, {}, {"sources": "x"}, {"sources": {"a": "x"}}):
            self.assertEqual(event.from_ledger("c1", DATE, bad), [], bad)


class TestMerge(unittest.TestCase):
    def _one(self, end=hms(7, 30, 10), cls=None, now=None, mv=(("x", "y"), ())):
        ai = [(hms(7, 30), end, cls or {"person": 1})]
        return event.from_ledger("c1", DATE, ledger_data(ai=ai), now_sec=now)

    def test_idempotent(self):
        """同一份输入连跑两次，第二次必须 changed=False。

        这是整个索引能不能稳定下来的前提：/api/events 每刷新一次就同步一次，
        不幂等的话索引永远"有新事件"，前端一直重绘、磁盘一直写。
        """
        fresh = self._one()
        once, ch1 = event.merge([], fresh)
        twice, ch2 = event.merge(once, fresh)
        self.assertTrue(ch1)
        self.assertFalse(ch2)
        self.assertEqual(once, twice)

    def test_extends_end_without_new_id(self):
        a = self._one()
        b = self._one(end=hms(7, 31, 0))
        out, changed = event.merge(a, b)
        self.assertTrue(changed)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], a[0]["id"])
        self.assertEqual(out[0]["end"], hms(7, 31, 0))
        self.assertEqual(out[0]["end_at"], "2026-11-08 07:31:00")

    def test_categories_max_not_doubled(self):
        a = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(7, 30), hms(7, 30, 10), {"person": 2})]))
        b = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(7, 30), hms(7, 30, 10), {"person": 2})]))
        out, _ = event.merge(a, b)
        self.assertEqual(out[0]["cls"]["person"], 2)

    def test_state_never_regresses(self):
        ended = self._one()
        self.assertEqual(ended[0]["state"], "ended")
        active = self._one(now=hms(7, 30, 20))          # fresh 说"进行中"
        self.assertEqual(active[0]["state"], "active")
        out, _ = event.merge(ended, active)
        self.assertEqual(out[0]["state"], "ended")

    def test_motion_unknown_survives_merge(self):
        a = self._one()
        b = self._one(end=hms(7, 31, 0))
        out, _ = event.merge(a, b)
        self.assertIsNone(out[0]["motion_confirmed"])

    def test_distinct_ids_kept(self):
        a = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(7, 30), hms(7, 30, 10), {"person": 1})]))
        b = event.from_ledger("c2", DATE, ledger_data(
            ai=[(hms(7, 30), hms(7, 30, 10), {"person": 1})]))
        out, _ = event.merge(a, b)
        self.assertEqual(len(out), 2)
        self.assertEqual([e["cam"] for e in out], ["c1", "c2"])

    def test_sorted_by_start(self):
        early = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(7, 0), hms(7, 0, 5), {"person": 1})]))
        late = event.from_ledger("c1", DATE, ledger_data(
            ai=[(hms(9, 0), hms(9, 0, 5), {"person": 1})]))
        out, _ = event.merge(late, early)
        self.assertLess(out[0]["start"], out[1]["start"])

    def test_bad_entries_skipped(self):
        out, changed = event.merge([None, {"no_id": 1}, "x"],
                                   [None, {"id": ""}, {"no_id": 2}])
        self.assertEqual(out, [])
        self.assertFalse(changed)


class TestLocate(unittest.TestCase):
    starts = [hms(7, 30), hms(7, 35), hms(7, 40)]

    def test_offset_is_pre_ten(self):
        """起点 07:30:30 → 从 07:30:20 播，偏移 20 秒。"""
        loc = event.locate(hms(7, 30, 30), self.starts)
        self.assertEqual(loc["segment"], "07-30-00.mp4")
        self.assertEqual(loc["offset_sec"], 20.0)
        self.assertFalse(loc["clamped"])
        self.assertEqual(loc["pre_sec"], 10.0)
        self.assertIsNone(loc["missing_reason"])

    def test_picks_covering_segment(self):
        loc = event.locate(hms(7, 36), self.starts)
        self.assertEqual(loc["segment"], "07-35-00.mp4")
        self.assertEqual(loc["offset_sec"], 50.0)

    def test_cross_segment_head_is_clamped(self):
        """事件在 07:35 这一片开头的第 3 秒：前摇退不到上一片里去。

        不做"跨片前摇"（打开上一片从末尾 5 秒处开播）：单个 <video> 播到片尾
        就停，用户得先看 5 秒再手动续下一片，而事件本身反而没播到。
        宁可夹到片头、标 clamped，也要保证点开就连续看到事件发生。
        """
        loc = event.locate(hms(7, 35, 3), self.starts)
        self.assertEqual(loc["segment"], "07-35-00.mp4")
        self.assertEqual(loc["offset_sec"], 0.0)
        self.assertTrue(loc["clamped"])
        self.assertEqual(loc["pre_sec"], 3.0)

    def test_no_segment_before(self):
        loc = event.locate(hms(7, 0, 5), self.starts)
        self.assertIsNone(loc["segment"])
        self.assertEqual(loc["missing_reason"], "no_segment_before")
        self.assertTrue(loc["missing_text"])

    def test_pre_zero_still_works(self):
        loc = event.locate(hms(7, 35, 3), self.starts, pre=0)
        self.assertEqual(loc["segment"], "07-35-00.mp4")
        self.assertEqual(loc["offset_sec"], 3.0)

    def test_seg_name(self):
        self.assertEqual(event.seg_name(hms(7, 30)), "07-30-00.mp4")

    def test_every_missing_code_has_text(self):
        """每个 code 都要有中文原因：只给 code 等于把翻译甩给前端（两份真相）。"""
        for code in event.MISSING_TEXT:
            m = event.missing(code)
            self.assertEqual(m["missing_reason"], code)
            self.assertTrue(m["missing_text"])
            self.assertIsNone(m["segment"])

    def test_unknown_code_still_readable(self):
        self.assertTrue(event.missing("who_knows")["missing_text"])


class TestResolve(unittest.TestCase):
    """未封口分片的处理：它决定"点进去播一秒就停"会不会再发生。"""

    def test_unsealed_takes_precedence(self):
        """事件落在正在写的分片里 → 必须说"还没封口"，不能算到上一片末尾。

        算到上一片的末尾是以前的形态：播放器打开后立刻到片尾，
        用户看到的就是"点进去播一秒就停"，跟"录像坏了"一模一样。
        """
        loc = event.resolve(hms(7, 35, 5), [hms(7, 30)], [hms(7, 35)])
        self.assertEqual(loc["missing_reason"], "not_sealed")
        self.assertEqual(loc["pending_segment"], "07-35-00.mp4")
        self.assertTrue(loc["missing_text"])

    def test_unsealed_only(self):
        loc = event.resolve(hms(7, 35, 5), [], [hms(7, 35)])
        self.assertEqual(loc["missing_reason"], "not_sealed")

    def test_older_part_ignored(self):
        """上一片已经封口、当前片也在写但事件在已封口那一片里：正常播。"""
        loc = event.resolve(hms(7, 35, 5), [hms(7, 30), hms(7, 35)], [hms(7, 30)])
        self.assertIsNone(loc["missing_reason"])
        self.assertEqual(loc["segment"], "07-35-00.mp4")
        # 起点在片内第 5 秒，前摇只能退到片头
        self.assertEqual(loc["offset_sec"], 0.0)
        self.assertTrue(loc["clamped"])

    def test_first_segment_fallback(self):
        """前摇落在当天第一片之前 → 退回用事件起点定位，别直接判"播不了"。"""
        loc = event.resolve(hms(7, 30, 3), [hms(7, 30)])
        self.assertIsNone(loc["missing_reason"])
        self.assertEqual(loc["segment"], "07-30-00.mp4")
        self.assertEqual(loc["offset_sec"], 0.0)

    def test_nothing_anywhere(self):
        loc = event.resolve(hms(7, 0), [hms(7, 30)], [])
        self.assertEqual(loc["missing_reason"], "no_segment_before")


class TestIsoAt(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(event.iso_at(DATE, hms(7, 30, 5)),
                         "2026-11-08 07:30:05")

    def test_clamped(self):
        self.assertEqual(event.iso_at(DATE, -5), "2026-11-08 00:00:00")
        self.assertEqual(event.iso_at(DATE, 90000), "2026-11-08 23:59:59")

    def test_sorts_lexicographically(self):
        a = event.iso_at(DATE, hms(9, 0))
        b = event.iso_at(DATE, hms(10, 0))
        self.assertLess(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=1)
