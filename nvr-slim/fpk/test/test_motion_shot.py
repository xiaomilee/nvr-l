#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运动事件快照（0.9.134）：从 recorder 分片抽一帧补进 event_shots。

要治的是什么：纯帧差的运动事件（KIND_MOTION）在 0.9.133 及之前没有快照，
事件列表/详情里 ev.shot 是 None，用户只能点开回放才能确认。这一层在事件被
查询到「还没有图」时，从 recorder 已录好的分片里异步抽一帧补上。

守三件事：
  1. 拍「当时」不是「现在」：抽的是事件起点所在分片里的那一帧，锚定事件窗口，
     不是 snapshots/<cam>.jpg 那种「几秒后的画面」（那是 event_shot 明文禁止的假数据）。
  2. 只加不改、不阻塞：request 只做幂等判断 + 非阻塞入队；抽帧/落盘在后台线程。
  3. 图是附属物：复用 event_shot 的过期/上限/限流，抽不到就 None，绝不抛到查询路径。

跑：python test/test_motion_shot.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "server"))
import event
import event_shot as es
import motion_shot as ms

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64 + b"\xff\xd9"


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def day_str(at=None):
    return time.strftime("%Y-%m-%d", time.localtime(at if at is not None else time.time()))


class Tmp(unittest.TestCase):
    def setUp(self):
        self.var = tempfile.mkdtemp(prefix="nvr-mshot-")
        self.rec = tempfile.mkdtemp(prefix="nvr-rec-")
        es.cache_clear()

    def tearDown(self):
        shutil.rmtree(self.var, ignore_errors=True)
        shutil.rmtree(self.rec, ignore_errors=True)
        es.cache_clear()


class TestEpochOf(Tmp):
    def test_01_local_midnight(self):
        """本地 2026-11-09 00:00:00 当 epoch 起点；+100 秒仍落在同一天目录。"""
        at = ms.epoch_of("2026-11-09", 100.0)
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(at)), "2026-11-09")
        base = ms.epoch_of("2026-11-09", 0.0)
        self.assertAlmostEqual(at - base, 100.0, delta=0.01)

    def test_02_bad_input_falls_back(self):
        now = time.time()
        self.assertAlmostEqual(ms.epoch_of("not-a-date", None), now, delta=2.0)


class TestLocate(Tmp):
    def _seed(self, cam, date, name):
        d = os.path.join(self.rec, cam, date)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"fake-mp4")
        return p

    def test_10_resolves_within_segment(self):
        """事件起点落在某片内：返回分片路径与片内偏移（含前摇）。"""
        date = day_str()
        self._seed("cam-01", date, "00-00-00.mp4")   # 起点 0 的分片
        got = ms.locate_segment(self.rec, ["cam-01"], date, 100.0)
        self.assertIsNotNone(got)
        path, offset = got
        self.assertTrue(os.path.isfile(path))
        pre = getattr(event, "PRE_SEC", 10.0)
        self.assertAlmostEqual(offset, max(0.0, 100.0 - pre), delta=1.0)

    def test_11_missing_segment_is_none(self):
        """事件起点之前没有任何分片：定位不到，返回 None（不抛）。"""
        date = day_str()
        self._seed("cam-01", date, "10-00-00.mp4")   # 起点 36000 的分片
        self.assertIsNone(ms.locate_segment(self.rec, ["cam-01"], date, 100.0))

    def test_12_bad_date_dir_is_none(self):
        self.assertIsNone(ms.locate_segment(self.rec, ["cam-01"], "2026-13-09", 100.0))

    def test_13_clip_slice_excluded(self):
        """活动切片 (.c01.mp4) 不算全时分片，定位要找的是事实链上的那一片。"""
        date = day_str()
        self._seed("cam-01", date, "00-00-00.c01.mp4")
        self.assertIsNone(ms.locate_segment(self.rec, ["cam-01"], date, 100.0))


class FakeStore(object):
    """落盘层替身：记下调用参数，尤其是 gap（补图必须为 False）。"""

    def __init__(self, ok=True):
        self.got = []
        self.ok = ok

    def offer(self, cam, data, at=None, dets=None, gap=True):
        self.got.append((cam, data, at, gap))
        return self.ok


class FakeExtractService(ms.MotionShotService):
    """抽帧用假字节，免去真 ffmpeg 依赖。"""

    def _extract(self, path, offset):
        return JPEG


class TestRequest(Tmp):
    def test_20_request_is_idempotent_when_image_exists(self):
        """窗口内已有图：request 直接跳过，不入队。"""
        date = day_str()
        p = es.path_of(self.var, "cam-01", date, es.name_of(100.0))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(JPEG)
        svc = ms.MotionShotService("", self.var, self.rec,
                                   lambda cam: ["cam-01"], None)
        ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
              "date": date, "start": 100.0, "end": 108.0}
        self.assertFalse(svc.request(ev), "已有图就不必再抽")

    def test_21_request_queues_when_missing(self):
        date = day_str()
        svc = ms.MotionShotService("", self.var, self.rec,
                                   lambda cam: ["cam-01"], None)
        ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
              "date": date, "start": 100.0, "end": 108.0}
        self.assertTrue(svc.request(ev))
        self.assertEqual(svc.queued, 1)

    def test_22_request_bad_event_is_noop(self):
        svc = ms.MotionShotService("", self.var, self.rec,
                                   lambda cam: ["cam-01"], None)
        self.assertFalse(svc.request(None))
        self.assertFalse(svc.request({"cam": "cam-01"}))


class TestServiceFlow(Tmp):
    def test_30_background_extract_and_store(self):
        """无图事件入队 → 后台抽帧（假字节）→ FakeStore 收到一张。"""
        date = day_str()
        os.makedirs(os.path.join(self.rec, "cam-01", date), exist_ok=True)
        with open(os.path.join(self.rec, "cam-01", date, "00-00-00.mp4"), "wb") as f:
            f.write(b"fake-mp4")
        store = FakeStore()
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], store)
        svc.start()
        try:
            ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
                  "date": date, "start": 100.0, "end": 108.0}
            self.assertTrue(svc.request(ev))
            self.assertTrue(wait_for(lambda: len(store.got) >= 1))
        finally:
            svc.stop()
        cam, data, at, gap = store.got[0]
        self.assertEqual(cam, "cam-01")
        self.assertEqual(data, JPEG)
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(at)), date)
        self.assertFalse(gap, "补图必须关掉实时限流（gap=False）")

    def test_31_no_shot_store_does_not_raise(self):
        """shot_store=None：抽到数据也只跳过落盘，不抛、extracted 仍计数。"""
        date = day_str()
        os.makedirs(os.path.join(self.rec, "cam-01", date), exist_ok=True)
        with open(os.path.join(self.rec, "cam-01", date, "00-00-00.mp4"), "wb") as f:
            f.write(b"fake-mp4")
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], None)
        svc.start()
        try:
            ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
                  "date": date, "start": 100.0, "end": 108.0}
            svc.request(ev)
            self.assertTrue(wait_for(lambda: svc.extracted >= 1))
        finally:
            svc.stop()

    def test_32_rejected_store_is_not_counted_as_stored(self):
        """落盘层拒收（日上限 / 限流）时 stored 必须为 0。

        0.9.134 的坑：`_handle` 不看 offer 的返回值，抽到帧就 extracted+1，
        计数器全绿而事件列表里一张图都没有。extracted 只该说"抽到了帧"，
        stored 才是"图真的落在了硬盘上"。
        """
        date = day_str()
        os.makedirs(os.path.join(self.rec, "cam-01", date), exist_ok=True)
        with open(os.path.join(self.rec, "cam-01", date, "00-00-00.mp4"), "wb") as f:
            f.write(b"fake-mp4")
        store = FakeStore(ok=False)          # 落盘层一律拒收
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], store)
        svc.start()
        try:
            ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
                  "date": date, "start": 100.0, "end": 108.0}
            self.assertTrue(svc.request(ev))
            self.assertTrue(wait_for(lambda: svc.rejected >= 1))
        finally:
            svc.stop()
        self.assertEqual(svc.extracted, 1, "抽到帧了，extracted 该记一笔")
        self.assertEqual(svc.stored, 0, "被拒收就不能算已落盘")
        self.assertEqual(svc.rejected, 1)

    def test_33_missing_segment_is_not_retried(self):
        """分片定位不到 → 记进负缓存，冷却期内不再入队（防队列饥饿）。

        事件列表是时间升序的，最老的事件录像早已滚掉。没有负缓存的话，每次
        刷新都会重投它们、把 32 个槽位吃满，最新的事件永远排不上。
        """
        date = day_str()
        os.makedirs(os.path.join(self.rec, "cam-01", date), exist_ok=True)  # 空目录
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], None)
        ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
              "date": date, "start": 100.0, "end": 108.0}
        svc.start()
        try:
            self.assertTrue(svc.request(ev))
            self.assertTrue(wait_for(lambda: svc.missed >= 1))
            self.assertFalse(svc.request(ev), "刚确认过分片不在，冷却期内不该重投")
            self.assertEqual(svc.queued, 1)
        finally:
            svc.stop()


class TestRobustness(Tmp):
    def test_40_worker_survives_bad_item(self):
        """单条处理抛异常不能打死 worker（0.9.134 真机 bug 的回归守卫）。

        真机现象：cam_dirs_fn 契约不匹配（传 id 却按 dict 用）在第一条就抛
        AttributeError，逐条处理没兜底 → worker 线程静默退出，队列只塞不抽、
        全部 dropped、extracted 永远 0。这里让第一条必抛、第二条正常，断言
        线程仍活着且第二条能抽到图。
        """
        date = day_str()
        os.makedirs(os.path.join(self.rec, "cam-01", date), exist_ok=True)
        with open(os.path.join(self.rec, "cam-01", date, "00-00-00.mp4"), "wb") as f:
            f.write(b"fake-mp4")
        store = FakeStore()
        calls = {"n": 0}

        def boom_then_ok(cam):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AttributeError("'str' object has no attribute 'get'")
            return ["cam-01"]

        svc = FakeExtractService("", self.var, self.rec, boom_then_ok, store)
        svc.start()
        try:
            ev1 = {"id": "cam-01_%s_000100" % date.replace("-", ""),
                   "cam": "cam-01", "date": date, "start": 100.0, "end": 108.0}
            ev2 = {"id": "cam-01_%s_000200" % date.replace("-", ""),
                   "cam": "cam-01", "date": date, "start": 200.0, "end": 208.0}
            self.assertTrue(svc.request(ev1))
            self.assertTrue(svc.request(ev2))
            # 第一条抛错被吞、第二条仍能抽到图 → 证明 worker 没死
            self.assertTrue(wait_for(lambda: len(store.got) >= 1))
            self.assertGreaterEqual(svc.failed, 1)
            self.assertTrue(svc.alive(), "单条异常不应让 worker 线程退出")
        finally:
            svc.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
