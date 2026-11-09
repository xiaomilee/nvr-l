#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detect 调度内核单测（0.9.122 批次 4 第 1 项）。

要守的三件事，正好对应三条不变式：
  S 有界 —— 并发度 = pool_size(路数)，默认 2、封顶 4，绝不"一路一线程"；
  R 不重入 —— 同一路在跑就不再派第二份；
  N 不阻塞 —— ticker 只判定 + 入队，队列满了丢、排队过期丢，都记 unknown。

外加一条"数值证据"：8 路一轮的墙钟时间必须明显短于串行的 8 × 单路，
否则「有界并发」只是把 for 循环换了个写法（验收口径 1.8 × 单路的技术底座）。

跑：python test/test_detect_sched.py
"""
import os
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "server"))
import detect  # noqa: E402


def ppm2x2():
    """最小合法 PPM（parse_ppm 只认头部声明的宽高，不看内容）。"""
    return b"P6 2 2 255\n" + b"\x10" * 12


class FakeEngine:
    """不起 onnxruntime 的引擎替身：只记并发峰值和调用次数。"""

    def __init__(self, delay=0.0, dets=None):
        self.delay = delay
        self.dets = dets if dets is not None else []
        self.loaded = True
        self.error = ""
        self.error_code = ""
        self.avg_ms = 12.0
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def ensure(self):
        return True, ""

    def infer(self, rgb, w, h, conf_map):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return list(self.dets)
        finally:
            with self.lock:
                self.active -= 1


def cam(i, on=True):
    return {"id": "cam%d" % i, "name": "路%d" % i, "enabled": on,
            "url": "rtsp://u:p@10.0.0.%d/1" % i, "detect": "follow"}


def cams(n, on=True):
    return [cam(i, on) for i in range(n)]


def make_worker(n=8, workers=4, queue_size=None, interval=5, engine=None,
                dets=None, delay=0.0):
    """worker 起来但 _grab 被换成"立刻给一张 2×2 PPM"（不碰 ffmpeg/网络）。"""
    eng = engine or FakeEngine(delay=delay, dets=dets)
    w = detect.DetectWorker(
        eng, "/bin/true", os.path.dirname(os.path.abspath(__file__)),
        lambda: "", lambda: cams(n),
        cfg_fn=lambda: ({"enable": True,
                         "conf": {"person": 0.5, "two_wheeler": 0.33, "vehicle": 0.4},
                         "classes": {"person": True, "two_wheeler": True, "vehicle": True}},
                        cams(n)),
        workers=workers, queue_size=queue_size)
    w._grab = lambda c, url: (True, ppm2x2(), "", "")
    return w


class TestPoolSize(unittest.TestCase):
    def test_default_two(self):
        self.assertEqual(detect.pool_size(1), 2, "单路也保底 2 并发（推理 76ms 不值得串行）")
        self.assertEqual(detect.pool_size(2), 2)

    def test_cap_four(self):
        self.assertEqual(detect.pool_size(4), 4)
        self.assertEqual(detect.pool_size(8), 4, "8 路封顶 4：再多只会互相抢 CPU")
        self.assertEqual(detect.pool_size(64), 4)

    def test_bad_input(self):
        self.assertEqual(detect.pool_size(None), 2)
        self.assertEqual(detect.pool_size("abc"), 2)
        self.assertEqual(detect.pool_size(-3), 2)


class TestStale(unittest.TestCase):
    def test_fresh(self):
        self.assertFalse(detect.is_stale(100.0, 100.0, 5))
        self.assertFalse(detect.is_stale(100.0, 104.9, 5))

    def test_expired(self):
        self.assertTrue(detect.is_stale(100.0, 105.1, 5))
        self.assertTrue(detect.is_stale(100.0, 200.0, 5))

    def test_bad_input_is_stale(self):
        # 时间戳坏了就当成过期：宁可这一拍没结论，也不要把不知道年龄的帧记成"现在"
        self.assertTrue(detect.is_stale(None, 100.0, 5))
        self.assertTrue(detect.is_stale("x", 100.0, 5))


class TestScheduler(unittest.TestCase):
    def test_first_round_due_immediately(self):
        s = detect.Scheduler(5)
        self.assertEqual(s.due(["a", "b"], 100.0), ["a", "b"],
                         "新路（新增摄像头 / 刚开启）第一拍就要跑，别让人等一个节拍")

    def test_interval_gates(self):
        s = detect.Scheduler(5)
        s.due(["a"], 100.0)
        self.assertEqual(s.due(["a"], 101.0), [], "没到点不派")
        self.assertEqual(s.due(["a"], 105.0), ["a"], "到点就派")

    def test_no_reentry(self):
        s = detect.Scheduler(5)
        s.due(["a"], 100.0)
        s.begin("a")
        self.assertEqual(s.due(["a"], 200.0), [], "在跑的路不许再派一份（慢路会自我叠加）")
        s.end("a")
        self.assertEqual(s.due(["a"], 201.0), ["a"], "跑完后立刻可再派")

    def test_dropped_tick_not_compensated(self):
        """被队列挡下的那一拍在下一拍不补跑 —— 否则队列一堵就是双倍负载。"""
        s = detect.Scheduler(5)
        s.due(["a"], 100.0)          # 假设这一拍被丢了（没 begin/end）
        self.assertEqual(s.due(["a"], 103.0), [], "next_due 已经推到 105")
        self.assertEqual(s.due(["a"], 105.0), ["a"])

    def test_trim_forgets_removed_cams(self):
        s = detect.Scheduler(5)
        s.due(["a", "b"], 100.0)
        s.begin("b")
        s.trim(["a"])
        self.assertNotIn("b", s.next_due, "删掉的摄像头不该留一个永远不跑的节拍")
        self.assertEqual(s.inflight_count(), 0)

    def test_order_is_stable(self):
        s = detect.Scheduler(5)
        self.assertEqual(s.due(["c", "a", "b"], 1.0), ["c", "a", "b"])


class TestWorkerBounded(unittest.TestCase):
    """真起线程：验证并发上限、不重入、不阻塞、过期丢弃。"""

    def setUp(self):
        self.ws = []

    def tearDown(self):
        for w in self.ws:
            w.stop()

    def _start(self, w):
        w.start()
        self.ws.append(w)
        self.addCleanup(w.stop)     # 断言失败也要收线程，别把进程吊着

    def _wait_idle(self, w, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            with w._stats_lock:
                if w._pending == 0:
                    return True
            time.sleep(0.01)
        return False

    def test_concurrency_capped(self):
        eng = FakeEngine(delay=0.05)
        w = make_worker(n=8, workers=4, queue_size=8, engine=eng)
        self._start(w)
        w._tick()
        self.assertTrue(self._wait_idle(w, 3.0))
        self.assertLessEqual(eng.max_active, 4, "4 并发就是 4，不许冒第 5 个推理线程")
        self.assertGreaterEqual(eng.max_active, 2, "该并的没并起来，等于没改")
        self.assertEqual(eng.calls, 8, "8 路都要跑到")

    def test_round_not_linear(self):
        """8 路一轮的墙钟 ≈ 2 × 单路（4 并发），而不是 8 × 单路。"""
        single = FakeEngine(delay=0.05)
        w1 = make_worker(n=1, workers=1, queue_size=2, engine=single)
        self._start(w1)
        t0 = time.time()
        w1._tick()
        self.assertTrue(self._wait_idle(w1, 3.0))
        one = time.time() - t0

        eng = FakeEngine(delay=0.05)
        w8 = make_worker(n=8, workers=4, queue_size=8, engine=eng)
        self._start(w8)
        t0 = time.time()
        w8._tick()
        self.assertTrue(self._wait_idle(w8, 3.0))
        eight = time.time() - t0
        self.assertLess(eight, one * 8 * 0.6,
                        "8 路耗时 %.3fs 与串行的 %.3fs 差不多，说明并发没生效"
                        % (eight, one * 8))

    def test_tick_never_blocks(self):
        """队列满时 ticker 立刻返回：一旦堵在这里，所有路一起停摆。"""
        w = make_worker(n=3, workers=1, queue_size=1, delay=0.2)
        self._start(w)
        w._tick()                    # 第一拍：1 个进队列，其余被丢
        t0 = time.time()
        w._tick()                    # 第二拍：队列还满着，必须马上返回
        dt = time.time() - t0
        self.assertLess(dt, 0.15, "ticker 被队列堵住了 %.3fs" % dt)

    def test_queue_full_counts_unknown(self):
        """队列满不是"没人"，是"不知道"：按 E_QUEUE 记 unknown。"""
        w = make_worker(n=1, workers=1, queue_size=1)
        w.q = queue.Queue(maxsize=1)
        w.q.put_nowait(("x", cam(9), "rtsp://x", {}, {}, time.time()))
        w._tick()
        st = w.status()["per_cam"]["cam0"]
        self.assertEqual(st["unknown"], 1, "队列满必须有账")
        self.assertEqual(st["codes"].get(detect.E_QUEUE), 1)
        self.assertEqual(st["today"], 0, "unknown 不许算进「有结论的帧数」")
        self.assertEqual(w.status()["sched"]["dropped_full"], 1)

    def test_stale_task_discarded(self):
        """排队超过一个节拍的任务直接丢，绝不把两拍前的画面记成现在。"""
        # 摄像头列表给空：让 ticker 自己不去派活，只测"排队过期"这一条路径
        w = make_worker(n=0, workers=1, queue_size=2, interval=1)
        self._start(w)
        eng = w.engine
        w.q.put_nowait(("cam0", cam(0), "rtsp://x", {}, {"person": True},
                        time.time() - 30))
        time.sleep(0.4)
        self.assertEqual(eng.calls, 0, "过期帧不许进推理")
        st = w.status()["per_cam"]["cam0"]
        self.assertEqual(st["codes"].get(detect.E_STALE), 1)
        self.assertEqual(w.status()["sched"]["dropped_stale"], 1)

    def test_same_cam_not_reentered(self):
        """慢路自我叠加：同一路在跑时，下一拍不许再派它。"""
        w = make_worker(n=1, workers=1, queue_size=4)
        w.q = queue.Queue(maxsize=4)
        w.sched.begin("cam0")        # 假装这一路还在跑
        w._tick()
        self.assertEqual(w.q.qsize(), 0, "cam0 在跑，不该再进队列")
        w.sched.end("cam0")
        w._tick()
        self.assertEqual(w.q.qsize(), 1, "跑完后立刻可再派")

    def test_alive_requires_all_threads(self):
        w = make_worker(n=2, workers=2)
        self.assertFalse(w.alive(), "没 start 不算活着")
        self._start(w)
        self.assertTrue(w.alive())

    def test_auto_workers_from_cam_count(self):
        """workers 不指定时按当时的路数定：2 路 → 2 并发，8 路 → 4 并发。

        构造时还读不到配置，所以不能在 __init__ 里算死；固定成 2 又会让
        8 路的院子跑不满 4 个工作者。口径落在 start()。
        """
        two = make_worker(n=2, workers=None)
        self._start(two)
        self.assertEqual(two.workers, 2)
        self.assertEqual(two.queue_size, 4, "队列 = 2 × 并发")

        eight = make_worker(n=8, workers=None)
        self._start(eight)
        self.assertEqual(eight.workers, 4)
        self.assertEqual(eight.queue_size, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
