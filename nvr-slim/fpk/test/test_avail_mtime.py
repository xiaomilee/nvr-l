#!/usr/bin/env python3
"""断录要真的画出来（0.9.75）：分片结束时刻用 mtime，不再靠「下一片起点」猜。

真机 2026-10-26 现场：5 路当天的 avail 全是 **1 个区间**（蓝块一整条），
实际断录合计 —— 车库 19.2 分 / 院子 R 10.4 分 / 客厅 7.7 分 / 院子 L 5.1 分。
车库 50 分钟里 38% 没有画面，时间轴上却是满的。

根因不是前端画错，是数据从来不读真实时长：
  - `avail_spans` 判「间隔 ≤ 3 个片长就是连续」，车库 3×120=360 秒，
    而真实断录 95~130 秒（间隔 220~250）→ 全被涂成有画面；
  - 更根本的是分片**名义时长 ≠ 真实时长**。ffprobe 实测：
    车库 `03-42-00.mp4` 配置 120 秒、实际 **84 秒**，结束于 03:43:24 ——
    正是用户截图里播放停住的那一刻，之后到 03:45:01 是 97 秒空洞。

mtime 就是这一片的结束时刻：ffmpeg 的 segment muxer 只在切下一片时才关闭上一片。
守这些：
  - mtime 可信 → 结束 = mtime，中间真有洞就真的画成两段
  - 缝 < AVAIL_MERGE_SEC（10 秒）→ 并成连续（秒级精度 + GOP 抖动，不是断录）
  - mtime 越界（跨天 / 被 rsync 改过 / 小于起点）→ 不采信，退回「下一片起点」
  - now 夹紧优先于 mtime（还没录到的不许画）
  - 活动块按可用区间切段（split_by_spans），整段落在断录里返回空
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import ledger as lg  # noqa: E402


def hms(s):
    s = int(round(s))
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


class TestClockSecOf(unittest.TestCase):
    def _ts(self, y, mo, d, h, mi, s=0):
        return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))

    def test_same_day_returns_clock_seconds(self):
        ts = self._ts(2026, 10, 26, 3, 43, 24)
        self.assertEqual(lg.clock_sec_of(ts, "2026-10-26"), 3 * 3600 + 43 * 60 + 24)

    def test_other_day_returns_none(self):
        """跨天分片：文件名 23-59-xx、写完已是次日 00:00 —— 不能塞进当天的 0 点。"""
        ts = self._ts(2026, 10, 27, 0, 0, 2)
        self.assertIsNone(lg.clock_sec_of(ts, "2026-10-26"))

    def test_midnight_belongs_to_its_own_day(self):
        ts = self._ts(2026, 10, 26, 0, 0, 0)
        self.assertEqual(lg.clock_sec_of(ts, "2026-10-26"), 0)


class TestRealEndFromMtime(unittest.TestCase):
    SEG = 120          # 车库：配置 120 秒

    def test_gap_appears_when_mtime_shorter(self):
        """真机车库原样：03:42:00 那片只有 84 秒，下一片 03:45:01 → 必须留 97 秒洞。

        旧口径下这里是 [[13320,13501]] 一整段（洞被吞），正是用户看到的「蓝块满的
        但播到 03:43:24 就跳」。
        """
        starts = [13320.0, 13501.0, 13639.0, 13765.0]   # 03:42:00 03:45:01 03:47:19 03:49:25
        ends = {13320.0: 13404.0,                          # 03:43:24
                13501.0: 13503.0,                          # 只录 2 秒
                13639.0: 13693.0}
        got = lg.avail_spans(starts, self.SEG, ends=ends)
        self.assertEqual(got, [[13320.0, 13404.0],
                               [13501.0, 13503.0],
                               [13639.0, 13693.0],
                               [13765.0, 13765.0 + lg.seg_typ_of(starts, self.SEG)]])

    def test_old_behaviour_without_ends(self):
        """不给 ends（老调用方 / 老数据）时行为不变 —— 洞被吞成连续。"""
        starts = [13320.0, 13501.0, 13639.0, 13765.0]
        # seg_typ_of 取间隔中位数 138（126/138/181），末片按它兜底
        self.assertEqual(lg.avail_spans(starts, self.SEG), [[13320.0, 13903.0]])

    def test_small_gap_merged(self):
        """正常连续录制：上片结束比下片起点早 3 秒（封口延迟），不许画成断录。"""
        starts = [100.0, 220.0, 340.0, 460.0]
        ends = {100.0: 217.0, 220.0: 337.0, 340.0: 457.0}
        got = lg.avail_spans(starts, self.SEG, ends=ends)
        self.assertEqual(got, [[100.0, 460.0 + self.SEG]])

    def test_gap_equal_to_threshold_is_kept(self):
        """缝正好等于 10 秒：不合并（阈值是「小于」），断录从 10 秒起算。"""
        starts = [100.0, 220.0, 340.0, 460.0]
        ends = {100.0: 210.0, 220.0: 337.0, 340.0: 457.0}
        got = lg.avail_spans(starts, self.SEG, ends=ends)
        self.assertEqual(got[0], [100.0, 210.0])
        self.assertEqual(got[1][0], 220.0)

    def test_mtime_too_far_ignored(self):
        """mtime 落在 (起点, 起点+3片长] 之外 → 不是「写完封口」的时刻，退回旧口径。"""
        starts = [100.0, 220.0, 340.0, 460.0]
        ends = {100.0: 100.0 + 3600.0}          # 被 rsync 改成了几小时后
        # 不可信 → 每片都按「下一片起点」接上，全天连续，末片按 120 兜底
        self.assertEqual(lg.avail_spans(starts, self.SEG, ends=ends), [[100.0, 580.0]])

    def test_mtime_before_start_ignored(self):
        """mtime 早于起点（时钟错乱 / 复制保留旧时间）→ 不采信。"""
        starts = [100.0, 220.0, 340.0, 460.0]
        ends = {100.0: 50.0}
        got = lg.avail_spans(starts, self.SEG, ends=ends)
        self.assertEqual(got, [[100.0, 460.0 + self.SEG]])

    def test_partial_ends_mixes_both_rules(self):
        """只有部分片有 mtime：有就用、没有就退回「下一片起点」。"""
        self.assertEqual(lg.avail_spans([100.0, 220.0, 340.0, 460.0], self.SEG,
                                        ends={340.0: 350.0}),
                         [[100.0, 350.0], [460.0, 460.0 + self.SEG]])

    def test_now_clamps_even_with_mtime(self):
        """今天：mtime 说是录到 300，但 now 才 250 → 仍然夹到 250。"""
        starts = [100.0, 220.0, 340.0, 460.0]
        ends = {460.0: 590.0}
        got = lg.avail_spans(starts, self.SEG, now_sec=250.0, ends=ends)
        self.assertEqual(got, [[100.0, 250.0]])

    def test_whole_day_no_gap_stays_one_span(self):
        """完美连续的一天（每片正好首尾相接）不该被切碎。"""
        starts = [float(i * 120) for i in range(10)]
        ends = {s: s + 120.0 for s in starts}
        self.assertEqual(lg.avail_spans(starts, self.SEG, ends=ends), [[0.0, 1200.0]])


class TestSplitBySpans(unittest.TestCase):
    """活动块与蓝块共用同一份「哪里有画面」的事实。"""

    def test_clip_into_pieces(self):
        self.assertEqual(lg.split_by_spans(100.0, 300.0, [[0.0, 150.0], [200.0, 400.0]]),
                         [[100.0, 150.0], [200.0, 300.0]])

    def test_fully_inside_gap_returns_empty(self):
        """整个活动都落在断录里 → 空：调用方画虚线框，不画类别色条。"""
        self.assertEqual(lg.split_by_spans(160.0, 190.0, [[0.0, 150.0], [200.0, 400.0]]), [])

    def test_fully_covered_returns_whole(self):
        self.assertEqual(lg.split_by_spans(110.0, 190.0, [[0.0, 400.0]]), [[110.0, 190.0]])

    def test_adjacent_spans_not_split(self):
        """两个区间在活动里首尾相接 → 不该切出零长度碎片。"""
        self.assertEqual(lg.split_by_spans(100.0, 300.0, [[0.0, 200.0], [200.0, 400.0]]),
                         [[100.0, 300.0]])

    def test_empty_spans_returns_empty(self):
        """没有可用区间 = 这段真的没有画面（注意前端 vtlClipByAvail 相反：
        空表示「服务端没给数据」，原样返回。两者语义不同，别混）。"""
        self.assertEqual(lg.split_by_spans(100.0, 300.0, []), [])


class TestRealDeviceGarageDay(unittest.TestCase):
    """真机车库 2026-10-26 片段原样固化：断录必须切成多段。

    ENDS 只放实测/可确证的两片，其余不给 —— 未被采信的片会退回「下一片起点」，
    这是真实运行时的常态（旧数据没有 mtime）。断言只压「确定该出现的东西」。
    """

    STARTS = [11991.0, 12000.0, 12120.0, 12239.0, 12360.0, 12480.0, 12600.0,
              12722.0, 12840.0, 12895.0, 12989.0, 13080.0, 13200.0, 13320.0,
              13501.0, 13503.0, 13639.0, 13765.0, 13802.0, 13920.0, 14085.0,
              14241.0, 14393.0, 14400.0, 14529.0, 14667.0, 14831.0, 14880.0]
    # 03-42-00.mp4：ffprobe 实测 84 秒（配 120），结束正是截图停住的 03:43:24
    # 03-45-01.mp4：262 KB，只录 2 秒
    ENDS = {13320.0: 13404.0, 13501.0: 13503.0}

    def test_gap_is_drawn(self):
        got = lg.avail_spans(self.STARTS, 120, ends=self.ENDS)
        # ENDS 只覆盖 2 片，其余仍按旧口径接上 → 恰好切出 2 段；
        # 真实运行每片都有 mtime，切出的段数远多于此（当天实测断 10 段）
        self.assertGreaterEqual(len(got), 2, "03:43:24 之后断到 03:45:01，蓝块不该是 1 段")
        self.assertTrue(any(abs(e - 13404.0) < 0.01 for _s, e in got),
                        "03:43:24 必须是某一段的结尾（那片只录了 84 秒）")
        holes = [(got[i][1], got[i + 1][0]) for i in range(len(got) - 1)]
        self.assertTrue(any(hms(a) == "03:43:24" and hms(b) == "03:45:01" for a, b in holes),
                        "03:43:24→03:45:01 的 97 秒空洞没画出来：%s" % holes)

    def test_span_never_covers_a_known_hole(self):
        """已知断录（03:43:24→03:45:01）不许被任何区间覆盖。"""
        got = lg.avail_spans(self.STARTS, 120, ends=self.ENDS)
        for s, e in got:
            self.assertFalse(s < 13450 < e, "区间 %s~%s 跨过了 03:44 的断录" % (hms(s), hms(e)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
