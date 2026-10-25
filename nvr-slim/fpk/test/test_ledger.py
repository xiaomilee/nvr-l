#!/usr/bin/env python3
"""活动台账的行为测试。

重点守七条：
  - 窗口换算：分片内秒 + 分片起点 = 当天钟点秒，且夹进 [0, 86400]
  - 同 src 替换：重判不堆积旧条目（幂等的另一半）
  - 跨分片合并：gap 内的相邻窗口连成一段，重叠取并集
  - 密度加权：跨片窗口的 d 是重叠时长加权的均值，不是简单平均
  - 原子写：.tmp 残留不留坑；损坏/缺失文件读出 None 而不是抛异常
  - 防御：非法日期/目录/窗口一律不写不崩
  - 读端三档时间尺度（0.9.69）：AI 在场 30 秒合并 / 帧差运动簇 60 秒 /
    帧差给 AI 段定进出边界（mv 标记）
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import ledger as lg  # noqa: E402


class TestBasics(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_src_start(self):
        self.assertEqual(lg.src_start("08-12-00.mp4"), 8 * 3600 + 12 * 60)
        self.assertEqual(lg.src_start("08-12-00.c02.mp4"), 8 * 3600 + 12 * 60)
        self.assertIsNone(lg.src_start("random.mp4"))
        self.assertIsNone(lg.src_start("25-00-00.mp4"))
        self.assertIsNone(lg.src_start(""))

    def test_merge_roundtrip(self):
        r = lg.merge(self.root, "车库入口", "2026-10-23", "08-12-00.mp4",
                     8 * 3600 + 12 * 60, [[3.0, 120.0]], density=0.31,
                     engine="pixel")
        self.assertIsNotNone(r)
        data = lg.load(self.root, "车库入口", "2026-10-23")
        self.assertIsNotNone(data)
        w = data["sources"]["08-12-00.mp4"]["windows"]
        self.assertEqual(w, [[8 * 3600 + 12 * 60 + 3.0, 8 * 3600 + 12 * 60 + 120.0]])
        ev = lg.events_of(data)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["d"], 0.31)

    def test_merge_replaces_same_src(self):
        lg.merge(self.root, "cam", "2026-10-23", "08-00-00.mp4", 0, [[10, 20]])
        # 同 src 重判：窗口变了，旧窗口必须整体消失而不是追加
        lg.merge(self.root, "cam", "2026-10-23", "08-00-00.mp4", 0, [[100, 150]])
        data = lg.load(self.root, "cam", "2026-10-23")
        self.assertEqual(data["sources"]["08-00-00.mp4"]["windows"], [[100.0, 150.0]])
        ev = lg.events_of(data)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["s"], 100.0)

    def test_cross_segment_union(self):
        # 5:59:58 走进画面、6:00:02 离开：两片的窗口必须连成一个事件
        lg.merge(self.root, "cam", "2026-10-23", "05-55-00.mp4", 5 * 3600 + 55 * 60,
                 [[298.0, 300.0]])
        lg.merge(self.root, "cam", "2026-10-23", "06-00-00.mp4", 6 * 3600,
                 [[0.0, 2.0]])
        ev = lg.events_of(lg.load(self.root, "cam", "2026-10-23"))
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["s"], 5 * 3600 + 55 * 60 + 298.0)
        self.assertEqual(ev[0]["e"], 6 * 3600 + 2.0)

    def test_gap_merges_nearby(self):
        # 帧差是"运动簇"（0.9.69 起 gap=60 秒）：一个人走到一半站住几秒，
        # 这一段动静不该被切成两条事件
        lg.merge(self.root, "cam", "2026-10-23", "08-00-00.mp4", 0,
                 [[10, 20], [24, 40]])
        ev = lg.events_of(lg.load(self.root, "cam", "2026-10-23"))
        self.assertEqual(len(ev), 1)          # 4 秒间隙 → 合并
        lg.merge(self.root, "cam", "2026-10-23", "08-00-00.mp4", 0,
                 [[10, 20], [50, 60]])
        ev = lg.events_of(lg.load(self.root, "cam", "2026-10-23"))
        self.assertEqual(len(ev), 1)          # 30 秒间隙 ≤ 60 → 仍是同一段动静
        lg.merge(self.root, "cam", "2026-10-23", "08-00-00.mp4", 0,
                 [[10, 20], [200, 210]])
        ev = lg.events_of(lg.load(self.root, "cam", "2026-10-23"))
        self.assertEqual(len(ev), 2)          # 180 秒间隙 → 保持两段

    def test_density_weighted(self):
        # 窗口 [0,100] 跨两片：前 90 秒来自 d=0.2 的片，后 10 秒来自 d=0.6 的片
        lg.merge(self.root, "cam", "2026-10-23", "00-00-00.mp4", 0,
                 [[0, 90]], density=0.2)
        lg.merge(self.root, "cam", "2026-10-23", "01-00-00.mp4", 3600,
                 [[-3510, -3500]], density=0.6)   # 负偏移换算后 = 90~100 秒段
        ev = lg.events_of(lg.load(self.root, "cam", "2026-10-23"))
        self.assertEqual(len(ev), 1)
        # (0.2*90 + 0.6*10) / 100 = 0.24
        self.assertAlmostEqual(ev[0]["d"], 0.24, places=2)

    def test_clamp_day_bounds(self):
        # 分片起 23:59:50，窗口溢出当天末尾 → 夹到 86400
        lg.merge(self.root, "cam", "2026-10-23", "23-59-50.mp4", 86390,
                 [[0, 100]])
        ev = lg.events_of(lg.load(self.root, "cam", "2026-10-23"))
        self.assertEqual(ev[0]["e"], 86400.0)

    def test_defense(self):
        self.assertIsNone(lg.merge(self.root, "cam", "bad-date", "a.mp4", 0, []))
        self.assertIsNone(lg.merge(self.root, "", "2026-10-23", "a.mp4", 0, []))
        self.assertIsNone(lg.merge(self.root, "..", "2026-10-23", "a.mp4", 0, []))
        self.assertIsNone(lg.merge(self.root, "cam", "2026-10-23", "a.mp4", None, []))
        self.assertIsNone(lg.load(self.root, "cam", "nope"))   # 不存在 → None
        # 损坏文件 → None，不抛
        d = os.path.join(self.root, "_ledger", "cam")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "2026-10-23.json"), "w") as f:
            f.write("{broken")
        self.assertIsNone(lg.load(self.root, "cam", "2026-10-23"))
        self.assertEqual(lg.events_of(None), [])
        self.assertEqual(lg.events_of({"sources": "junk"}), [])

    def test_tmp_not_left_behind(self):
        lg.merge(self.root, "cam", "2026-10-23", "08-00-00.mp4", 0, [[1, 2]])
        leftovers = [n for n in os.listdir(os.path.join(self.root, "_ledger", "cam"))
                     if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_silent_src_recorded(self):
        # 判为静默的分片也要记账（空窗口）：否则它会被当成"还没判过"反复重扫
        lg.merge(self.root, "cam", "2026-10-23", "03-00-00.mp4", 3 * 3600, [])
        data = lg.load(self.root, "cam", "2026-10-23")
        self.assertIn("03-00-00.mp4", data["sources"])
        self.assertEqual(lg.events_of(data), [])

    def test_summary(self):
        lg.merge(self.root, "cam", "2026-10-23", "08-00-00.mp4", 0,
                 [[10, 70], [200, 260]])
        s = lg.summary_of(lg.load(self.root, "cam", "2026-10-23"))
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["active_sec"], 120.0)


class TestSessions(unittest.TestCase):
    """0.9.69 读端三档时间尺度：AI 在场合并 / 运动簇 / 帧差给 AI 段定边界。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _data(self):
        return lg.load(self.root, "cam", "2026-10-23")

    def test_ai_gap_holds_through_frame_timeout(self):
        """AI 每 5 秒抽一帧，抓帧超时会漏出 10~20 秒空隙——30 秒内算同一段在场。"""
        lg.merge(self.root, "cam", "2026-10-23", "ai:10-00-00.mp4", 0,
                 [[100, 102, {"vehicle": 1}], [117, 119, {"vehicle": 1}],
                  [141, 143, {"vehicle": 1}]], density=1.0, engine="ai")
        ev = lg.events_of(self._data())
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["s"], 100.0)
        self.assertEqual(ev[0]["e"], 143.0)
        self.assertEqual(ev[0]["cls"], {"vehicle": 1})

    def test_ai_gap_two_events_when_far_apart(self):
        lg.merge(self.root, "cam", "2026-10-23", "ai:10-00-00.mp4", 0,
                 [[100, 102, {"person": 1}], [200, 202, {"person": 1}]],
                 density=1.0, engine="ai")
        self.assertEqual(len(lg.events_of(self._data())), 2)

    def test_parked_car_single_event_no_cap(self):
        """车停 40 分钟：不裂成几百条，也不设 10 分钟上限。"""
        wins = [[t, t + 2, {"vehicle": 1}] for t in range(100, 2500, 15)]
        lg.merge(self.root, "cam", "2026-10-23", "ai:10-00-00.mp4", 0, wins,
                 density=1.0, engine="ai")
        ev = lg.events_of(self._data())
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["s"], 100.0)
        self.assertEqual(ev[0]["e"], 2487.0)

    def test_motion_extends_ai_bounds(self):
        """帧差先看见车进来/离开，AI 认出车型时车已在画面中间 → 边界取帧差。"""
        lg.merge(self.root, "cam", "2026-10-23", "10-00-00.mp4", 0,
                 [[96, 150]], density=0.3)
        lg.merge(self.root, "cam", "2026-10-23", "ai:10-00-00.mp4", 0,
                 [[110, 140, {"vehicle": 1}]], density=1.0, engine="ai")
        ev = lg.events_of(self._data())
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["s"], 96.0)      # arrive：车从画面边缘进来
        self.assertEqual(ev[0]["e"], 150.0)     # leave：车开走
        self.assertEqual(ev[0]["mv"], 1)
        self.assertEqual(ev[0]["cls"], {"vehicle": 1})

    def test_ai_without_motion_corroboration(self):
        """有帧差管线、但这段时间画面没动 → mv=0，边界不动（可疑的误检）。"""
        lg.merge(self.root, "cam", "2026-10-23", "10-00-00.mp4", 0,
                 [[10, 20]], density=0.3)
        lg.merge(self.root, "cam", "2026-10-23", "ai:10-00-00.mp4", 0,
                 [[600, 620, {"person": 1}]], density=1.0, engine="ai")
        ev = lg.events_of(self._data())
        ai = [e for e in ev if e["cls"]]
        self.assertEqual(len(ai), 1)
        self.assertEqual(ai[0]["s"], 600.0)
        self.assertEqual(ai[0]["e"], 620.0)
        self.assertEqual(ai[0]["mv"], 0)

    def test_no_motion_pipeline_at_all(self):
        """只有 AI 台账（没跑过帧差）→ mv=-1，如实说"无从判断"而不是"没动"。"""
        lg.merge(self.root, "cam", "2026-10-23", "ai:10-00-00.mp4", 0,
                 [[600, 620, {"person": 1}]], density=1.0, engine="ai")
        ev = lg.events_of(self._data())
        self.assertEqual(ev[0]["mv"], -1)

    def test_motion_only_event_keeps_mv1(self):
        lg.merge(self.root, "cam", "2026-10-23", "10-00-00.mp4", 0,
                 [[10, 20]], density=0.3)
        ev = lg.events_of(self._data())
        self.assertEqual(ev[0]["mv"], 1)
        self.assertEqual(ev[0]["cls"], {})

    def test_motion_far_away_not_pulled_in(self):
        """运动簇离 AI 段超过 60 秒 → 不并入（另一个人的事）。"""
        lg.merge(self.root, "cam", "2026-10-23", "10-00-00.mp4", 0,
                 [[100, 110], [900, 910]], density=0.3)
        lg.merge(self.root, "cam", "2026-10-23", "ai:10-00-00.mp4", 0,
                 [[500, 520, {"person": 1}]], density=1.0, engine="ai")
        ev = lg.events_of(self._data())
        self.assertEqual(len(ev), 3)
        ai = [e for e in ev if e["cls"]]
        self.assertEqual((ai[0]["s"], ai[0]["e"], ai[0]["mv"]), (500.0, 520.0, 0))

    def test_explicit_gap_overrides_all(self):
        """显式传 gap 时两档都用它（单一口径，供特殊调用/测试）。"""
        lg.merge(self.root, "cam", "2026-10-23", "ai:10-00-00.mp4", 0,
                 [[100, 102, {"person": 1}], [117, 119, {"person": 1}]],
                 density=1.0, engine="ai")
        self.assertEqual(len(lg.events_of(self._data(), gap=5.0)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
