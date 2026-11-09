#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间窗确认单测（0.9.122 批次 4 第 2 项）。

这一层只回答一个问题：**这一帧够不够格记一笔台账**。所以测试也只钉三件事：

  1. 单帧误报不许进台账（这是整层的存在理由）；
  2. 确认之后的事件要是一条连续窗口，起点是**第一帧命中**，不是确认那一刻
     —— 否则事件页点进去会比实际晚 5~10 秒，"看到人"和"人在画面里"对不上；
  3. unknown（取帧失败/推理异常）不参与判定：既不算命中，也不算"没人"。

跑：python test/test_event_confirm.py
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "server"))
import confirm  # noqa: E402

T0 = 1793500000.0          # 任意一个 epoch 秒，测试只做相对加减


def _h(conf=0.9):
    return {"person": conf}


class TestNorm(unittest.TestCase):
    def test_defaults(self):
        cfg, notes = confirm.norm({})
        self.assertTrue(cfg["enable"], "默认开启：误报碎片是这个批次要治的病")
        self.assertEqual(cfg["window"], 3)
        self.assertEqual(cfg["min_hits"], 2)
        self.assertEqual(notes, [])
        for k in confirm.CATEGORIES:
            self.assertIn(k, cfg["min_conf"], "三类阈值必须齐全，否则缺键就当 0 命中")

    def test_window_clamped(self):
        self.assertEqual(confirm.norm({"window": 99})[0]["window"], 10)
        self.assertEqual(confirm.norm({"window": 1})[0]["window"], 2)
        self.assertEqual(confirm.norm({"window": "abc"})[0]["window"], 3)

    def test_min_hits_never_exceeds_window(self):
        """写着 5 却只有 3 帧可看 = 永远不确认，而且看不出来（静默失效）。"""
        cfg = confirm.norm({"window": 3, "min_hits": 5})[0]
        self.assertEqual(cfg["min_hits"], 3)

    def test_conf_clamped(self):
        cfg = confirm.norm({"min_conf": {"person": 9, "vehicle": -1}})[0]
        self.assertLessEqual(cfg["min_conf"]["person"], 0.99)
        self.assertGreaterEqual(cfg["min_conf"]["vehicle"], 0.05)

    def test_bad_window_reports_note(self):
        _cfg, notes = confirm.norm({"window": 99})
        self.assertTrue(any("window" in n for n in notes), "夹取要留痕，否则用户以为生效了")

    def test_not_dict(self):
        cfg, notes = confirm.norm(None)
        self.assertEqual(cfg, confirm.CONFIRM_DEFAULTS)
        self.assertEqual(notes, [])


class TestConfirm(unittest.TestCase):
    def setUp(self):
        self.c = confirm.Confirmer({})

    def test_single_frame_is_rejected(self):
        """5 秒采样下一片树影出一帧 —— 不许进台账。"""
        self.assertEqual(self.c.feed("a", T0, _h()), [])
        self.assertEqual(self.c.feed("a", T0 + 5, {}), [])
        # 超过 end_wait 都没再命中：这条在途事件被丢弃并计一次误报
        self.assertEqual(self.c.feed("a", T0 + 30, {}), [])
        self.assertEqual(self.c.snapshot()["rejected"], 1)
        self.assertEqual(self.c.snapshot()["confirmed"], 0)

    def test_two_consecutive_frames_confirm(self):
        self.assertEqual(self.c.feed("a", T0, _h()), [])          # 第 1 帧：还不敢确认
        got = self.c.feed("a", T0 + 5, _h())                     # 第 2 帧：够了
        self.assertEqual(len(got), 1)
        s, e, cls = got[0]
        self.assertAlmostEqual(s, T0, msg="起点必须是第一帧命中，不是确认那一刻")
        self.assertAlmostEqual(e, T0 + 5 + confirm.ROLL_SEC)
        self.assertEqual(cls, {"person": 1})
        self.assertEqual(self.c.snapshot()["confirmed"], 1)

    def test_miss_between_hits_inside_window(self):
        """hit / miss / hit：3 帧里 2 帧命中 → 仍算确认（中间掉一帧是正常漏检）。"""
        self.c.feed("a", T0, _h())
        self.c.feed("a", T0 + 5, {})
        got = self.c.feed("a", T0 + 10, _h())
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0][0], T0)

    def test_confirmed_keeps_emitting_same_start(self):
        """确认之后每一帧都带上整条事件的起点，台账里才是连续窗口而不是碎片。"""
        self.c.feed("a", T0, _h())
        self.c.feed("a", T0 + 5, _h())
        later = self.c.feed("a", T0 + 10, _h())
        self.assertEqual(len(later), 1)
        self.assertAlmostEqual(later[0][0], T0, msg="起点不许随帧前移")
        self.assertAlmostEqual(later[0][1], T0 + 10 + confirm.ROLL_SEC)

    def test_low_conf_is_not_hit(self):
        cfg = {"min_conf": {"person": 0.7}}
        c = confirm.Confirmer(cfg)
        c.feed("a", T0, _h(0.6))
        self.assertEqual(c.feed("a", T0 + 5, _h(0.6)), [],
                         "低于确认阈值的一帧只算没命中")
        self.assertEqual(c.confirmed, 0)

    def test_unknown_frames_do_not_count(self):
        """不喂 = 不算。取帧失败那几帧既不该凑命中，也不该判"没人"。"""
        self.c.feed("a", T0, _h())
        self.c.feed("a", T0 + 5, _h())          # 确认
        before = self.c.snapshot()
        # 之后 40 秒一帧都没喂（全在 unknown）：事件既不延长也不结束
        self.assertEqual(self.c.state[("a", "person")].confirmed, True)
        self.assertEqual(self.c.snapshot()["confirmed"], before["confirmed"])
        # 再喂一帧命中：上面那 40 秒并没有被判结束，事件仍在
        got = self.c.feed("a", T0 + 45, _h())
        self.assertEqual(len(got), 1)

    def test_end_wait_closes_run(self):
        self.c.feed("a", T0, _h())
        self.c.feed("a", T0 + 5, _h())
        self.assertIn(("a", "person"), self.c.state)
        self.c.feed("a", T0 + 40, {})           # 35 秒没命中 > end_wait(20)
        self.assertNotIn(("a", "person"), self.c.state, "结束要落到 closed，供合并判断")
        self.assertIn(("a", "person"), self.c.closed)

    def test_adjacent_run_merges_into_one(self):
        """人出去又回来：间隔小于 merge_gap 就不该在列表里变两条。"""
        c = confirm.Confirmer({"end_wait_sec": 10, "merge_gap_sec": 60})
        c.feed("a", T0, _h())
        c.feed("a", T0 + 5, _h())               # 第一条：T0 ~ T0+6
        c.feed("a", T0 + 20, {})                # 15 秒没命中 > end_wait(10) → 结束
        self.assertIn(("a", "person"), c.closed)
        got = c.feed("a", T0 + 30, _h())        # 距上一条终点 25 秒 < merge_gap(60)
        self.assertEqual(len(got), 1, "合并进来的命中要立刻落账，不能被当成新事件的第 1 帧")
        self.assertAlmostEqual(got[0][0], T0, msg="合并后起点沿用上一条")
        self.assertEqual(c.snapshot()["confirmed"], 1, "合并进来不算新事件")

    def test_far_apart_are_two_events(self):
        c = confirm.Confirmer({"end_wait_sec": 5, "merge_gap_sec": 10})
        c.feed("a", T0, _h())
        c.feed("a", T0 + 5, _h())
        c.feed("a", T0 + 20, {})                # 结束
        self.assertEqual(c.feed("a", T0 + 40, _h()), [],
                         "隔了 35 秒：这是新事件的第一帧，还不敢确认")
        self.assertEqual(len(c.feed("a", T0 + 45, _h())), 1)
        self.assertEqual(c.snapshot()["confirmed"], 2, "隔得远就是两件事")

    def test_min_span_blocks_late_second_hit(self):
        """两次命中隔了 30 秒才凑够（中间靠窗口内其它帧）—— 跨度不足不确认。"""
        c = confirm.Confirmer({"window": 5, "min_hits": 2, "min_span_sec": 40.0,
                               "end_wait_sec": 60.0, "merge_gap_sec": 0})
        self.assertEqual(c.feed("a", T0, _h()), [])
        self.assertEqual(c.feed("a", T0 + 5, _h()), [], "跨度 5 秒 < 40 秒，不许确认")
        got = c.feed("a", T0 + 45, _h())
        self.assertEqual(len(got), 1, "跨度够了才确认")

    def test_per_camera_and_per_class_isolated(self):
        self.c.feed("a", T0, _h())
        self.c.feed("b", T0, {"vehicle": 0.9})
        self.assertEqual(self.c.feed("a", T0 + 5, _h())[0][2], {"person": 1})
        self.assertEqual(self.c.feed("b", T0 + 5, {"vehicle": 0.9})[0][2], {"vehicle": 1})

    def test_two_wheeler_threshold_lower(self):
        """两轮车远距离分低，确认阈值单独放低（与 detect 的分品类阈值同思路）。"""
        c = confirm.Confirmer({})
        c.feed("a", T0, {"two_wheeler": 0.45})
        self.assertEqual(len(c.feed("a", T0 + 5, {"two_wheeler": 0.45})), 1)

    def test_disabled_returns_nothing(self):
        c = confirm.Confirmer({"enable": False})
        for i in range(5):
            self.assertEqual(c.feed("a", T0 + i * 5, _h()), [])
        self.assertFalse(c.enabled)

    def test_trim_drops_removed_cams(self):
        self.c.feed("a", T0, _h())
        self.c.feed("gone", T0, _h())
        self.c.trim(["a"])
        self.assertIn(("a", "person"), self.c.state)
        self.assertNotIn(("gone", "person"), self.c.state)

    def test_update_clears_state_on_change(self):
        """窗口口径变了，用旧窗口攒出来的成绩不能继续用。"""
        self.c.feed("a", T0, _h())
        self.c.update({"window": 5})
        self.assertEqual(self.c.state, {})
        self.assertEqual(self.c.cfg["window"], 5)

    def test_update_same_cfg_keeps_state(self):
        self.c.feed("a", T0, _h())
        self.c.update(dict(confirm.CONFIRM_DEFAULTS))
        self.assertIn(("a", "person"), self.c.state, "参数没变不许把在途事件清掉")

    def test_bad_now_is_ignored(self):
        self.assertEqual(self.c.feed("a", "abc", _h()), [])

    def test_snapshot_shape(self):
        snap = self.c.snapshot()
        for k in ("enabled", "window", "min_hits", "confirmed", "rejected", "pending"):
            self.assertIn(k, snap)
        self.assertEqual(snap, dict(snap))
        self.assertEqual(confirm.snapshot_blank()["confirmed"], 0)


class TestCategoriesMatchDetect(unittest.TestCase):
    """类别集合必须与 detect 一致 —— 两处漂移会让某个类永远确认不了。"""

    def test_same_categories(self):
        import detect
        self.assertEqual(confirm.CATEGORIES, detect.CATEGORIES)


class _Engine:
    """只回固定框的引擎替身：确认层与推理无关，不该为它加载 10.7MB 模型。"""

    loaded = True
    error = ""
    avg_ms = 0

    def __init__(self, dets):
        self.dets = dets
        self.calls = 0

    def infer(self, rgb, w, h, conf_map):
        self.calls += 1
        return [dict(d) for d in self.dets]

    def ensure(self):
        return True, ""


PPM = b"P6\n2 2\n255\n" + b"\x10\x20\x30" * 4


def _tod(now):
    lt = time.localtime(now)
    return float(lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


class TestWorkerWiring(unittest.TestCase):
    """确认层接在 `_one()` 里：第一帧不落账、第二帧落一条、通知跟着确认走。"""

    def setUp(self):
        import shutil
        import tempfile
        import detect
        self.detect = detect
        self.tmp = tempfile.mkdtemp(prefix="confirm")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cam = {"id": "cam1", "name": "测试A", "dir_name": "cam1"}
        self.notified = []
        self.engine = _Engine([{"c": "person", "s": 0.92,
                                "b": [0.5, 0.5, 0.1, 0.2]}])
        self.w = self._worker(self.engine)

    def _worker(self, engine, confirm_cfg=None):
        w = self.detect.DetectWorker(
            engine, "", self.tmp, lambda: self.tmp,
            lambda: ([], []), lambda: ({}, []))
        w._grab = lambda cam, url: (True, PPM, "", "")
        w.notifier = lambda name, cat: self.notified.append((name, cat))
        if confirm_cfg is not None:
            w.confirmer.update(confirm_cfg)
        self.writes = []
        real = w._write_ledger
        w._write_ledger = lambda cam, wins, now: (self.writes.append(list(wins)),
                                                  real(cam, wins, now))[1]
        return w

    def test_first_frame_writes_nothing(self):
        now = time.time()
        self.w._one(self.cam, "rtsp://x", {}, {"person": True}, now)
        self.assertEqual(self.writes, [], "单帧命中不许进台账")
        self.assertEqual(self.notified, [], "也不许发通知：通知了却查到不事件最伤信任")

    def test_second_frame_writes_one_continuous_window(self):
        now = time.time()
        self.w._one(self.cam, "rtsp://x", {}, {"person": True}, now)
        self.w._one(self.cam, "rtsp://x", {}, {"person": True}, now + 5)
        self.assertEqual(len(self.writes), 1)
        s, e, cls = self.writes[0][0]
        self.assertAlmostEqual(s, _tod(now), places=3, msg="起点是第一帧命中")
        self.assertAlmostEqual(e, _tod(now) + 6, places=3)
        self.assertEqual(cls, {"person": 1})
        self.assertEqual(self.notified, [("测试A", "person")])

    def test_confirmed_event_reaches_ledger(self):
        """落盘口径：台账里真的长出一条带类别的窗口（不是只在内存里对）。"""
        import ledger
        now = time.time()
        self.w._one(self.cam, "rtsp://x", {}, {"person": True}, now)
        self.w._one(self.cam, "rtsp://x", {}, {"person": True}, now + 5)
        date = time.strftime("%Y-%m-%d", time.localtime(now))
        cur = ledger.load(self.tmp, "cam1", date) or {}
        srcs = cur.get("sources") or {}
        self.assertTrue(srcs, "台账里必须有一个 AI 源")
        wins = [w for s in srcs.values() for w in (s.get("windows") or [])]
        self.assertTrue(wins)
        self.assertEqual(wins[0][2], {"person": 1})

    def test_confirm_off_falls_back_to_every_frame(self):
        """一键回到 0.9.121 行为：关掉确认，单帧就落账。"""
        w = self._worker(self.engine, {"enable": False})
        w._one(self.cam, "rtsp://x", {}, {"person": True}, time.time())
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.notified, [("测试A", "person")])

    def test_unknown_frame_feeds_nothing(self):
        """取帧失败那一拍既不算命中也不算"没人"：事件不被判结束。"""
        now = time.time()
        self.w._one(self.cam, "rtsp://x", {}, {"person": True}, now)
        self.w._one(self.cam, "rtsp://x", {}, {"person": True}, now + 5)
        self.w._grab = lambda cam, url: (False, "", "抽帧超时（10 秒）", "")
        for k in range(3):
            self.w._one(self.cam, "rtsp://x", {}, {"person": True},
                        now + 10 + k * 5)
        self.assertEqual(len(self.writes), 1, "故障帧不许续账，也不许落账")
        self.assertIn(("cam1", "person"), self.w.confirmer.state)

    def test_window_clamped_to_bucket(self):
        """跨 5 分钟桶：源名就是"这个 5 分钟"，起点必须裁到桶首。

        不裁的话 ledger.merge 会把负的相对秒夹到 0 —— 台账里凭空出现一条
        从 00:00 开始的窗口，比没有更糟。
        """
        now = time.time()
        tod = _tod(now)
        self.w._write_ledger(self.cam, [(tod - 1200, tod + 1, {"person": 1})], now)
        start_sec = (int(tod) // 300) * 300
        self.assertEqual(len(self.writes), 1)
        import ledger
        date = time.strftime("%Y-%m-%d", time.localtime(now))
        cur = ledger.load(self.tmp, "cam1", date) or {}
        wins = [w for s in (cur.get("sources") or {}).values()
                for w in (s.get("windows") or [])]
        self.assertEqual(wins[0][0], start_sec, "窗口起点必须是桶首，不是 0")
        self.assertGreater(wins[0][1], start_sec)

    def test_status_has_confirm_block(self):
        st = self.w.status()
        for k in ("enabled", "window", "min_hits", "confirmed", "rejected", "pending"):
            self.assertIn(k, st["confirm"])
        self.assertTrue(st["confirm"]["enabled"])
        st2 = self.detect.status_of(self.w, {"enable": True}, ffmpeg="/bin/ffmpeg")
        self.assertIn("confirm", st2)
        st3 = self.detect.status_of(None, {"enable": True})
        self.assertEqual(st3["confirm"], self.detect.confirm_blank(),
                         "没 worker 也要给全 0 形状，别让前端判缺键")

    def test_norm_detect_carries_confirm(self):
        """confirm 必须进 norm_detect 的输出，否则每次保存设置都被打回默认。"""
        v, _ = self.detect.norm_detect({})
        self.assertIn("confirm", v)
        self.assertEqual(v["confirm"]["window"], confirm.CONFIRM_DEFAULTS["window"])
        v2, _ = self.detect.norm_detect({"confirm": {"window": 5, "min_hits": 3}})
        self.assertEqual(v2["confirm"]["window"], 5)
        self.assertEqual(v2["confirm"]["min_hits"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
