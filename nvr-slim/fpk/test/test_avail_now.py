#!/usr/bin/env python3
"""画面可用性区间（0.9.72）：末片不许画到「现在」之后。

真机反馈原话：「真实时间还未到，已经显示录制了视频」。截图证据：手机 02:28，
时间轴上代表「有录像」的蓝块却延伸到 02:30+（像素量到蓝块 1780–2207，
红色时刻线在 2154，蓝块明显越过它）。

根因不在画布、在数据：ledger_payload 给末片算结束时刻用的是
`min(86400, st + seg)` —— 分片长度是**名义值**，不管这段是否真的录到那儿。
录制中途停了、或正在写的时候，蓝块就会往后多画一整个分片长度。

守六条：
  - 中间片：结束 = 下一片起点（下一片存在 = 这段确实录到了这里）
  - 末片：无下一片参照时按分片长度兜底，但给了 now 就必须夹紧
  - now 落在末片中间 → 区间末端 = now（不是 st+seg）
  - 起点已晚于 now（时间戳超前 / 时钟错乱）→ 丢弃，不画假覆盖
  - 相邻片首尾相接 → 并成一个区间（不是画成一堆相邻小块）
  - 历史日期（now=None）保持旧行为，不受影响
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import ledger as lg  # noqa: E402


class TestAvailSpans(unittest.TestCase):
    SEG = 300

    def test_single_seg_no_now_uses_nominal_length(self):
        """历史日期（无 now）：末片按分片长度兜底，与旧行为一致。"""
        self.assertEqual(lg.avail_spans([3600.0], self.SEG), [[3600.0, 3900.0]])

    def test_last_seg_clamped_to_now(self):
        """末片只录到 now：区间末端必须是 now，不能是 st+seg。"""
        self.assertEqual(lg.avail_spans([3600.0], self.SEG, now_sec=3660.0),
                         [[3600.0, 3660.0]])

    def test_last_seg_not_extended_when_now_is_later(self):
        """录制停了一会儿（now 已过名义末端）：仍然只画到 st+seg，不往后延伸。"""
        self.assertEqual(lg.avail_spans([3600.0], self.SEG, now_sec=4200.0),
                         [[3600.0, 3900.0]])

    def test_middle_seg_ends_at_next_start(self):
        """中间片：结束 = 下一片起点；末片才按名义长度补足。"""
        # 3600/3900/4200 首尾相接 → 并成一段，末端 = 末片 4200+300 = 4500
        self.assertEqual(lg.avail_spans([3600.0, 3900.0, 4200.0], self.SEG, now_sec=8000.0),
                         [[3600.0, 4500.0]])
        # 只有两片时：3600 收在下一片起点 3900，3900 是末片 → 3900+300 = 4200
        self.assertEqual(lg.avail_spans([3600.0, 3900.0], self.SEG, now_sec=8000.0),
                         [[3600.0, 4200.0]])
        # 历史日期（无 now）同理，行为与旧版一致
        self.assertEqual(lg.avail_spans([3600.0, 3900.0], self.SEG, now_sec=None),
                         [[3600.0, 4200.0]])

    def test_now_inside_middle_seg(self):
        """now 落在中间片区间里（后面还有片但都在未来）→ 夹到现在，未来的片丢掉。"""
        got = lg.avail_spans([3600.0, 3900.0, 4200.0], self.SEG, now_sec=3950.0)
        self.assertEqual(got, [[3600.0, 3950.0]])

    def test_future_only_starts_dropped(self):
        """起点全在 now 之后（时钟错乱 / 文件名超前）：一条都不画。"""
        self.assertEqual(lg.avail_spans([4200.0, 4500.0], self.SEG, now_sec=3900.0), [])

    def test_adjacent_segments_merge(self):
        """首尾相接并成一个区间，长度 = 首起点到末尾端点。"""
        got = lg.avail_spans([0.0, 300.0, 600.0], self.SEG, now_sec=900.0)
        self.assertEqual(got, [[0.0, 900.0]])

    def test_gap_split_keeps_two_spans(self):
        """中间断录（相隔超过分片长度）必须留两个区间，不能连成一片。"""
        got = lg.avail_spans([0.0, 3600.0], self.SEG, now_sec=3900.0)
        self.assertEqual(got, [[0.0, 300.0], [3600.0, 3900.0]])

    def test_no_negative_or_empty_span(self):
        """任何情况下都不产出 e <= s 的区间（画出来是负高度）。"""
        for now in (0.0, 1.0, 3599.0, 3600.0, 3601.0):
            for span in lg.avail_spans([3600.0], self.SEG, now_sec=now):
                self.assertGreater(span[1], span[0], "now=%r → %r" % (now, span))

    def test_day_end_clamped(self):
        """末片贴着 24:00：不越过 86400。"""
        got = lg.avail_spans([86300.0], self.SEG, now_sec=86400.0)
        self.assertEqual(got, [[86300.0, 86400.0]])

    def test_duplicate_and_unsorted_starts(self):
        """起点重复/乱序：先去重排序，结果的连续性不依赖输入顺序。"""
        got = lg.avail_spans([3900.0, 3600.0, 3900.0, 4200.0], self.SEG, now_sec=4300.0)
        self.assertEqual(got, [[3600.0, 4300.0]])

    def test_empty(self):
        self.assertEqual(lg.avail_spans([], self.SEG, now_sec=1000.0), [])


class TestSegTypOf(unittest.TestCase):
    """实际分片长度必须从起点间隔推，不能用配置的名义值。

    真机 2026-10-26 实测：同一次请求里三路分片长度各不相同 ——
    院子 R 60 秒、车库 120 秒、其余跟随全局 300 秒。而 ledger_payload 只拿得到
    一个全局 record_segment_sec=300，拿它给 60 秒那一路兜底末片，就会多画 4 分钟。
    """

    def test_infers_from_median_gap(self):
        """30 片、每 60 秒一片 → 实际片长 60（不是配置的 300）。"""
        starts = [3600.0 + 60 * i for i in range(30)]
        self.assertEqual(lg.seg_typ_of(starts, 300), 60.0)

    def test_tolerates_gop_overshoot(self):
        """摄像头 GOP 不整除：配 120 实际 132，推断值要跟着真实间隔走。"""
        starts = [0.0]
        for _ in range(10):
            starts.append(starts[-1] + 132.0)
        self.assertEqual(lg.seg_typ_of(starts, 120), 132.0)

    def test_few_segments_falls_back_to_config(self):
        """片数太少（两片之间可能正好断过录）→ 不猜，退回配置值。"""
        self.assertEqual(lg.seg_typ_of([0.0, 3600.0], 300), 300.0)
        self.assertEqual(lg.seg_typ_of([0.0, 60.0, 120.0], 300), 300.0)

    def test_single_segment_falls_back(self):
        self.assertEqual(lg.seg_typ_of([3600.0], 300), 300.0)
        self.assertEqual(lg.seg_typ_of([], 300), 300.0)

    def test_all_gaps_absurd_falls_back(self):
        """全是几小时的断录间隔（中位数远超配置）→ 退回配置，别把断录当片长。"""
        starts = [0.0, 7200.0, 14400.0, 21600.0, 28800.0]
        self.assertEqual(lg.seg_typ_of(starts, 60), 60.0)


class TestPerCamSegmentLength(unittest.TestCase):
    """每路分片长度不同时，末片与断录判定都要跟着对。"""

    def test_short_segments_last_not_overdrawn(self):
        """院子 R 场景：配置 300、实际 60 秒。末片只能画 60 秒。"""
        starts = [7200.0 + 60 * i for i in range(20)]
        got = lg.avail_spans(starts, 300, now_sec=None)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], starts[-1] + 60.0)

    def test_short_segments_gap_detected(self):
        """实际 60 秒一片，中间空 10 分钟 → 必须留空洞。"""
        starts = [7200.0 + 60 * i for i in range(6)] + [8000.0 + 60 * i for i in range(6)]
        got = lg.avail_spans(starts, 300, now_sec=None)
        self.assertEqual(len(got), 2, got)
        self.assertLessEqual(got[0][1], 8000.0)   # 第一段不许覆盖到断录之后

    def test_ordinary_300s_unaffected(self):
        """普通 300 秒一路：推断值 = 配置值，行为与之前完全一致。"""
        starts = [0.0, 300.0, 600.0, 900.0, 1200.0]
        self.assertEqual(lg.avail_spans(starts, 300),
                         lg.avail_spans(starts, 300, now_sec=None))
        self.assertEqual(lg.avail_spans(starts, 300, now_sec=1000.0), [[0.0, 1000.0]])


class TestDaySecNow(unittest.TestCase):
    def test_returns_second_of_day(self):
        """day_sec_now 返回当天钟点秒（0..86399），与 time.localtime 同源。"""
        v = lg.day_sec_now()
        lt = time.localtime()
        self.assertEqual(v, lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        self.assertGreaterEqual(v, 0)
        self.assertLess(v, 86400)

    def test_accepts_explicit_timestamp(self):
        """可传时间戳（便于测试与复用），不依赖"此刻"。"""
        self.assertEqual(lg.day_sec_now(0), lg.day_sec_now(0))
        self.assertTrue(0 <= lg.day_sec_now(1700000000) < 86400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
