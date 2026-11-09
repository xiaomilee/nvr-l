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
    """落盘层替身：记下调用参数，尤其是 gap（补图必须为 False）与 dets（复核框）。"""

    def __init__(self, ok=True):
        self.got = []
        self.ok = ok

    def offer(self, cam, data, at=None, dets=None, gap=True):
        self.got.append({"cam": cam, "data": data, "at": at,
                         "gap": gap, "dets": dets})
        return self.ok


class FakeEngine(object):
    """推理引擎替身：infer 返回预置 dets；None = 引擎不可用（记 error）。"""

    def __init__(self, dets=None, err=None):
        self._dets = dets
        self.error = err or ""
        self.seen = []

    def infer(self, rgb, w, h, conf_map):
        self.seen.append((w, h, conf_map))
        return self._dets


PPM = (64, 48, b"\x00" * (64 * 48 * 3))   # 假 PPM 三元组：宽 高 RGB 字节


class FakeExtractService(ms.MotionShotService):
    """抽帧用假字节，免去真 ffmpeg 依赖。返回 (jpg, ppm) 二元组（0.9.137）。"""

    def __init__(self, *a, ppm=None, **kw):
        self._ppm = ppm
        super(FakeExtractService, self).__init__(*a, **kw)

    def _extract(self, path, offset):
        return JPEG, self._ppm


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
        g = store.got[0]
        cam, data, at, gap, dets = (g["cam"], g["data"], g["at"],
                                    g["gap"], g["dets"])
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


class TestRedetect(Tmp):
    """AI 复核（0.9.137）：抽到的帧再跑一次推理，推得出框就随图旁挂。

    守三条：
    1. kind 不变 —— 复核只写旁挂框，绝不改事件分类（台账是事实）。
    2. 复核失败/未启用只影响框，不影响快照本身（框是附属物的附属物）。
    3. 框必须真的传给落盘层（offer 的 dets 参数），不能"推了个寂寞"。
    """

    DETS = [{"c": "person", "s": 0.82, "b": [0.5, 0.5, 0.2, 0.4]},
            {"c": "vehicle", "s": 0.61, "b": [0.7, 0.6, 0.3, 0.25]},
            {"c": "cat", "s": 0.99, "b": [0.1, 0.1, 0.1, 0.1]}]   # cat 不在白名单

    def _seed(self):
        date = day_str()
        os.makedirs(os.path.join(self.rec, "cam-01", date), exist_ok=True)
        with open(os.path.join(self.rec, "cam-01", date, "00-00-00.mp4"), "wb") as f:
            f.write(b"fake-mp4")
        return date

    def _run(self, svc, date):
        ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
              "date": date, "start": 100.0, "end": 108.0}
        svc.start()
        try:
            self.assertTrue(svc.request(ev))
            self.assertTrue(wait_for(lambda: svc.extracted >= 1))
        finally:
            svc.stop()
        return ev

    def test_60_dets_flow_to_store(self):
        """复核出框 → dets 传给 offer（白名单外的类别被滤掉），redetected 计数。"""
        date = self._seed()
        eng = FakeEngine(dets=self.DETS)
        store = FakeStore()
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], store,
                                 ppm=PPM, detect_ref=lambda: {
                                     "engine": eng, "conf_map": {"person": 0.5}})
        self._run(svc, date)
        self.assertEqual(svc.redetected, 1)
        self.assertEqual(len(eng.seen), 1, "每张图正好跑一次推理")
        self.assertEqual(eng.seen[0], (64, 48, {"person": 0.5}))
        g = store.got[0]
        self.assertEqual([d["c"] for d in g["dets"]], ["person", "vehicle"],
                         "白名单外的类别（cat）必须滤掉")
        self.assertFalse(g["gap"])

    def test_61_no_detect_ref_stores_plain(self):
        """没配复核（AI 总闸关/worker 没起）：快照照补，offer 不带 dets。"""
        date = self._seed()
        store = FakeStore()
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], store, ppm=PPM)
        self._run(svc, date)
        self.assertEqual(svc.redetected, 0)
        self.assertIsNone(store.got[0]["dets"])
        self.assertEqual(store.got[0]["data"], JPEG)
        engine, why = svc.detect_ready()
        self.assertIsNone(engine)
        self.assertTrue(why, "不能跑必须给原因")

    def test_62_engine_failure_degrades_gracefully(self):
        """推理引擎不可用：图照落盘、redetected=0，原因进 detect.err。"""
        date = self._seed()
        eng = FakeEngine(dets=None, err="模型加载失败")
        store = FakeStore()
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], store,
                                 ppm=PPM, detect_ref=lambda: {
                                     "engine": eng, "conf_map": {}})
        self._run(svc, date)
        self.assertEqual(svc.redetected, 0)
        self.assertEqual(svc.stored, 1, "复核失败不许拖累快照本体")
        self.assertIn("推理不可用", svc.status()["detect"]["err"])
        self.assertIn("模型加载失败", svc.status()["detect"]["err"])

    def test_63_unknown_target_still_stores(self):
        """推完了但没认出人员/车辆：照存图，轨迹如实说「没认出」。"""
        date = self._seed()
        eng = FakeEngine(dets=[{"c": "cat", "s": 0.9, "b": [0.5, 0.5, 0.2, 0.2]}])
        store = FakeStore()
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], store,
                                 ppm=PPM, detect_ref=lambda: {
                                     "engine": eng, "conf_map": {}})
        self._run(svc, date)
        self.assertEqual(svc.redetected, 0)
        self.assertEqual(svc.stored, 1)
        self.assertIsNone(store.got[0]["dets"])

    def test_64_no_ppm_skips_inference(self):
        """ffmpeg 双输出只出了 jpg 没出 ppm：复核跳过，图照存。"""
        date = self._seed()
        eng = FakeEngine(dets=self.DETS)
        store = FakeStore()
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], store,
                                 ppm=None, detect_ref=lambda: {
                                     "engine": eng, "conf_map": {}})
        self._run(svc, date)
        self.assertEqual(len(eng.seen), 0, "没像素就不该跑推理")
        self.assertEqual(svc.stored, 1)
        self.assertIn("无原始像素", svc.status()["detect"]["err"])

    def test_65_probe_reports_redetect_availability(self):
        svc = ms.MotionShotService("", self.var, self.rec,
                                   lambda cam: ["cam-01"], None)
        p = svc.probe("cam-01", "2026-11-09")
        self.assertIn("redetect", p)
        self.assertFalse(p["redetect"]["ok"])
        self.assertTrue(p["redetect"]["why"], "不可用要给原因")
        # 配上复核引用后 probe 应报 ok
        svc2 = ms.MotionShotService("", self.var, self.rec,
                                    lambda cam: ["cam-01"], None,
                                    detect_ref=lambda: {
                                        "engine": FakeEngine(dets=[]),
                                        "conf_map": {}})
        p2 = svc2.probe("cam-01", "2026-11-09")
        self.assertTrue(p2["redetect"]["ok"])

    def test_66_extract_failure_counts_failed(self):
        """抽帧彻底失败（jpg 都没出）：failed 计数、不进落盘层。"""
        date = self._seed()
        store = FakeStore()

        class Boom(ms.MotionShotService):
            def _extract(self, path, offset):
                return None, None

        svc = Boom("", self.var, self.rec, lambda cam: ["cam-01"], store)
        svc.start()
        try:
            ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
                  "date": date, "start": 100.0, "end": 108.0}
            self.assertTrue(svc.request(ev))
            self.assertTrue(wait_for(lambda: svc.failed >= 1))
        finally:
            svc.stop()
        self.assertEqual(svc.failed, 1)
        self.assertEqual(svc.extracted, 0)
        self.assertEqual(store.got, [])


class TestDiag(Tmp):
    """诊断能力（0.9.136）：连续两版"测试机绿、生产机没图"，靠计数看不出卡在哪。

    这里守的是：probe 必须能分清「目录不存在 / 目录在但没分片 / 目录写不进」，
    trace 必须留下失败原因 —— 否则下一轮排障还是只能猜。
    """

    def _seed(self, cam, date, names):
        d = os.path.join(self.rec, cam, date)
        os.makedirs(d, exist_ok=True)
        for n in names:
            with open(os.path.join(d, n), "wb") as f:
                f.write(b"fake-mp4")
        return d

    def test_50_scan_day_separates_dirs_and_files(self):
        date = day_str()
        self._seed("cam-01", date,
                   ["00-00-00.mp4", "00-05-05.mp4", "00-10-10.c01.mp4",
                    "00-15-15.mp4.part", "note.txt"])
        sealed, parts, dirs_seen, files = ms.scan_day(
            self.rec, ["cam-01", "cam-gone"], date)
        self.assertEqual(dirs_seen, ["cam-01"], "只报真实存在的目录")
        self.assertEqual(files, 4, "只数 .mp4 / .mp4.part")
        self.assertEqual(len(sealed), 2, "活动切片不是全时分片")
        self.assertEqual(len(parts), 1)

    def test_51_probe_missing_dir(self):
        svc = ms.MotionShotService("", self.var, self.rec,
                                   lambda cam: ["cam-01"], None)
        p = svc.probe("cam-01", "2026-11-09")
        self.assertEqual(p["dirs"], ["cam-01"])
        self.assertEqual(p["dirs_seen"], [])
        self.assertEqual(p["files"], 0)
        self.assertEqual(p["segs"], 0)
        self.assertIn("shot_writable", p, "目录可写性必须报出来")
        self.assertIn("ffmpeg", p)

    def test_52_probe_locate(self):
        date = day_str()
        self._seed("cam-01", date, ["00-00-00.mp4"])
        svc = ms.MotionShotService("", self.var, self.rec,
                                   lambda cam: ["cam-01"], None)
        p = svc.probe("cam-01", date, 100.0)
        self.assertEqual(p["segs"], 1)
        self.assertEqual(p["dirs_seen"], ["cam-01"])
        self.assertIsNotNone(p["locate"]["seg"], "给了 start 就该报出定位到的分片")
        self.assertIsNotNone(p["locate"]["offset"])

    def test_53_status_exposes_trace_and_ffmpeg(self):
        svc = ms.MotionShotService("", self.var, self.rec,
                                   lambda cam: ["cam-01"], None)
        st = svc.status()
        for k in ("trace", "ffmpeg", "record_root", "stored", "rejected",
                  "missed", "redetected", "detect"):
            self.assertIn(k, st)
        self.assertEqual(st["trace"], [])
        self.assertIsNone(st["ffmpeg"]["ok"], "status 不该主动跑 ffmpeg 探测")

    def test_54_trace_keeps_failure_reason(self):
        date = day_str()
        os.makedirs(os.path.join(self.rec, "cam-01", date), exist_ok=True)  # 空目录
        svc = FakeExtractService("", self.var, self.rec,
                                 lambda cam: ["cam-01"], None)
        svc.start()
        try:
            ev = {"id": "cam-01_%s_000100" % date.replace("-", ""), "cam": "cam-01",
                  "date": date, "start": 100.0, "end": 108.0}
            svc.request(ev)
            self.assertTrue(wait_for(lambda: svc.missed >= 1))
        finally:
            svc.stop()
        tr = svc.status()["trace"]
        self.assertTrue(tr, "失败也必须有轨迹，否则排障无从下手")
        self.assertEqual(tr[-1]["step"], "locate")
        self.assertEqual(tr[-1]["segs"], 0)
        self.assertIn("分片", tr[-1]["reason"])

    def test_55_ffmpeg_missing_is_reported(self):
        svc = ms.MotionShotService("/no/such/ffmpeg-binary", self.var, self.rec,
                                   lambda cam: ["cam-01"], None)
        ok, why = svc.ffmpeg_ready()
        self.assertFalse(ok)
        self.assertTrue(why, "不可用时必须给原因，不能只说 False")
        self.assertFalse(svc.ffmpeg_ready()[0], "结论要缓存，不重复探测")


if __name__ == "__main__":
    unittest.main(verbosity=2)
