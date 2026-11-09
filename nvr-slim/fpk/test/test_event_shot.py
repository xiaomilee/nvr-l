#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件快照（0.9.129 批次 11）：存的是"当时那一帧"，不是"现在那一帧"。

要治的是什么：事件页只有一行字，用户想判断"这条值不值得点开"只能先点进去
播一遍。快照就是那一眼。但"快照"最容易做错的地方不是存图，而是**存哪一帧**：

  1. 拍「当时」。拿 `snapshots/<cam>.jpg` 充数是最省事的写法，也是错得最
     彻底的写法 —— 那是几秒后（甚至几小时后）的画面，属于"看起来对、其实
     没验证过"的假数据。所以时间取源图自己的 mtime，不取当前时钟。
  2. 只加不改。检测线程里只读一次内存、入队一次；写盘在独立线程。
     这条约束在这里的体现是：`offer()` 永不阻塞、永不抛，队列满就丢。
  3. 图是可清理的附属物。7 天过期、每路每天 300 张上限、不进事件索引。

跑：python test/test_event_shot.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "server"))
import detect  # noqa: E402
import event_shot as es  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64 + b"\xff\xd9"


def wait_for(pred, timeout=3.0):
    """等一个"写线程先落盘"的条件成立。

    写盘是异步的：固定 sleep 一个拍脑袋的秒数，猜短了随机红、猜长了一轮测试
    白等。这里轮询到条件成立就返回，超时才判假。
    """
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
        self.var = tempfile.mkdtemp(prefix="nvr-shot-")
        es.cache_clear()

    def tearDown(self):
        shutil.rmtree(self.var, ignore_errors=True)
        es.cache_clear()

    def put(self, cam, day, sec, data=JPEG):
        p = es.path_of(self.var, cam, day, es.name_of(sec))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p


class TestNaming(Tmp):
    def test_01_name_roundtrip(self):
        self.assertEqual(es.name_of(3723.456), "010203_456.jpg")
        self.assertAlmostEqual(es.parse_name("010203_456.jpg"), 3723.456, places=3)

    def test_02_name_bad_shapes(self):
        for bad in ("", "abc.jpg", "010203.jpg", "010203_45.jpg", "010203_456.png"):
            with self.subTest(名字=bad):
                self.assertIsNone(es.parse_name(bad))

    def test_03_name_rejects_impossible_clock(self):
        """24:00:00 / 00:60:00 这种名字只可能是手写的，宁可当它不存在。"""
        for bad in ("240000_000.jpg", "006000_000.jpg", "000060_000.jpg"):
            with self.subTest(名字=bad):
                self.assertIsNone(es.parse_name(bad))

    def test_04_name_at_keeps_millis(self):
        """毫秒不能全是 000：`localtime` 只有整秒，用它拼名会让同一秒的图互相覆盖。"""
        at = 1770000000.789
        n = es.name_at(at)
        self.assertTrue(n.endswith(".jpg"))
        self.assertNotEqual(es.parse_name(n), None)
        self.assertAlmostEqual(es.parse_name(n) % 1.0, 0.789, places=3)

    def test_05_name_of_clamps(self):
        """越界的钟点秒夹到当天内，绝不产出 25 点这种名字。"""
        self.assertEqual(es.name_of(-5), "000000_000.jpg")
        self.assertEqual(es.name_of(99999), "235959_999.jpg")


class TestPaths(Tmp):
    def test_10_layout(self):
        p = es.path_of(self.var, "cam-01", "2026-11-09", "102341_123.jpg")
        self.assertEqual(p, os.path.join(self.var, "event_shots", "cam-01",
                                         "2026-11-09", "102341_123.jpg"))

    def test_11_traversal_is_refused(self):
        """`..`、绝对段、子目录一律在这里挡掉 —— 这是唯一一处拼路径的地方。"""
        cases = [("../etc", "2026-11-09", "102341_123.jpg"),
                 ("cam-01", "../../etc", "102341_123.jpg"),
                 ("cam-01", "2026-11-09", "../102341_123.jpg"),
                 ("cam-01", "2026-11-09", "sub/102341_123.jpg"),
                 ("cam-01", "2026-13-09", "102341_123.jpg"),
                 ("", "2026-11-09", "102341_123.jpg")]
        for cam, day, name in cases:
            with self.subTest(cam=cam, day=day, name=name):
                self.assertIsNone(es.path_of(self.var, cam, day, name))

    def test_12_dir_of_refuses_bad_date(self):
        self.assertIsNone(es.dir_of(self.var, "cam-01", "2026-11"))
        self.assertIsNone(es.dir_of(self.var, "cam 01", "2026-11-09"))


class TestPick(Tmp):
    def test_20_pick_earliest_in_window(self):
        names = ["102341_000.jpg", "102350_000.jpg", "102410_000.jpg"]
        got = es.pick(names, 10 * 3600 + 23 * 60 + 30, 10 * 3600 + 23 * 60 + 55)
        self.assertEqual(got[0], "102341_000.jpg")

    def test_21_pick_window_is_closed(self):
        sec = 10 * 3600 + 23 * 60 + 41
        self.assertIsNotNone(es.pick(["102341_000.jpg"], sec, sec))

    def test_22_pick_ignores_garbage_names(self):
        names = ["README.txt", "102341_000.jpg", "999999_000.jpg"]
        self.assertEqual(es.pick(names, 0, 86400)[0], "102341_000.jpg")

    def test_23_pick_nothing(self):
        self.assertIsNone(es.pick([], 0, 86400))
        self.assertIsNone(es.pick(["102341_000.jpg"], 100, 200))


class TestFind(Tmp):
    def test_30_find_prefers_shot_after_start(self):
        """一条 8 秒事件，图落在开始后 6 秒：要被找到，且 offset 为正。"""
        d = day_str()
        self.put("cam-01", d, 3729.0)          # 01:02:09
        hit = es.find(self.var, "cam-01", d, 3723.0, 3731.0)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit["sec"], 3729.0, places=3)

    def test_31_find_window_slack(self):
        """图比事件起点早 10 秒（确认滞后）也要落进窗口 —— 不能因为"早"就丢。"""
        d = day_str()
        self.put("cam-01", d, 3690.0)
        self.assertIsNotNone(es.find(self.var, "cam-01", d, 3700.0, 3708.0))
        # 早 30 秒就超出 15 秒外扩了：那不是这条事件的画面
        self.assertIsNone(es.find(self.var, "cam-01", d, 3720.0, 3728.0))

    def test_32_find_missing_dir_is_none(self):
        self.assertIsNone(es.find(self.var, "cam-99", "2026-11-09", 0, 100))

    def test_33_find_cross_midnight(self):
        """事件尾巴跨过零点：图在第二天的目录里，sec 要算成 > 86400。"""
        self.put("cam-01", "2026-11-10", 5.0)
        hit = es.find(self.var, "cam-01", "2026-11-09", 86390.0, 86400.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["date"], "2026-11-10")
        self.assertAlmostEqual(hit["sec"], 86405.0, places=3)

    def test_34_find_bad_numbers(self):
        self.assertIsNone(es.find(self.var, "cam-01", day_str(), "x", "y"))


class TestShotOf(Tmp):
    def test_40_contract_shape(self):
        d = day_str()
        self.put("cam-01", d, 3729.0)
        ev = {"id": "cam-01_20261109_010203", "cam": "cam-01", "date": d,
              "start": 3723.0, "end": 3731.0}
        shot = es.shot_of(self.var, ev)
        self.assertEqual(set(shot), {"url", "at", "offset"})
        self.assertEqual(shot["url"], "/api/events/cam-01_20261109_010203/shot")
        self.assertEqual(shot["at"], "01:02:09")
        self.assertAlmostEqual(shot["offset"], 6.0, delta=0.01)

    def test_41_no_shot_is_none_not_empty_object(self):
        """前端只判 `if(ev.shot)`：空对象会被判真，渲染出一个没有 src 的 <img>。"""
        ev = {"id": "cam-01_20261109_010203", "cam": "cam-01", "date": day_str(),
              "start": 100.0, "end": 108.0}
        self.assertIsNone(es.shot_of(self.var, ev))

    def test_42_bad_event_is_none(self):
        self.assertIsNone(es.shot_of(self.var, None))
        self.assertIsNone(es.shot_of(self.var, {"cam": "cam-01"}))

    def test_43_offset_can_be_negative(self):
        """图比事件起点还早（确认窗把起点外推了）时，offset 如实为负。"""
        d = day_str()
        self.put("cam-01", d, 3710.0)
        ev = {"id": "cam-01_20261109_010203", "cam": "cam-01", "date": d,
              "start": 3723.0, "end": 3731.0}
        shot = es.shot_of(self.var, ev)
        self.assertLess(shot["offset"], 0)


class TestCache(Tmp):
    def test_50_list_cache_hides_new_file_until_cleared(self):
        """缓存是 5 秒：这是"列表里刚存的图下一轮就看得见"与"200 条别扫 200 次"
        之间的取舍。测试钉住它确实缓存了，避免以后有人顺手删掉还说要性能。"""
        d = day_str()
        self.assertEqual(es.list_for(self.var, "cam-01", d), [])
        self.put("cam-01", d, 100.0)
        self.assertEqual(es.list_for(self.var, "cam-01", d), [])
        es.cache_clear()
        self.assertEqual(len(es.list_for(self.var, "cam-01", d)), 1)


class TestStore(Tmp):
    def setUp(self):
        super().setUp()
        self.store = es.ShotStore(self.var, min_gap=30.0, max_per_day=3, queue_max=8)

    def tearDown(self):
        self.store.stop()
        super().tearDown()

    def test_60_offer_writes_file(self):
        self.store.start()
        at = time.time()
        self.assertTrue(self.store.offer("cam-01", JPEG, at))
        for _ in range(60):
            if self.store.written >= 1:
                break
            time.sleep(0.02)
        files = es.list_for(self.var, "cam-01", day_str(at))
        self.assertEqual(len(files), 1)
        self.assertIsNotNone(es.parse_name(files[0]))

    def test_61_rate_limit_same_cam(self):
        at = time.time()
        self.assertTrue(self.store.offer("cam-01", JPEG, at))
        self.assertFalse(self.store.offer("cam-01", JPEG, at + 5),
                         "30 秒内同一路只收一张，否则一条事件存十份")
        self.assertEqual(self.store.rate_limited, 1)
        self.assertTrue(self.store.offer("cam-01", JPEG, at + 31))
        self.assertTrue(self.store.offer("cam-02", JPEG, at + 1),
                        "限流是按路算的，不能一路挡全站")

    def test_61b_backfill_skips_rate_limit(self):
        """补图档位（gap=False）：不同事件各留一张，不被 30 秒闸门整片丢掉。

        0.9.134 生产现象：补旧事件时 at 是**历史时刻**、翻页还会倒退，
        `at - last` 变成负数 → 被当成"30 秒内又来一张"，于是只有隔 30 秒以上
        的事件才有图，用户看到的就是"移动侦测没有快照"。
        """
        at = time.time()
        self.assertTrue(self.store.offer("cam-01", JPEG, at))
        self.assertFalse(self.store.offer("cam-01", JPEG, at + 5))
        limited = self.store.rate_limited
        self.assertTrue(self.store.offer("cam-01", JPEG, at + 4, gap=False),
                        "补图不该被实时限流挡住")
        self.assertTrue(self.store.offer("cam-01", JPEG, at + 3, gap=False),
                        "倒退的历史时刻也不该被当成重复确认")
        self.assertEqual(self.store.rate_limited, limited,
                         "补图不增加限流计数")

    def test_62_daily_cap(self):
        at = time.time()
        n = 0
        for i in range(6):
            if self.store.offer("cam-01", JPEG, at + i * 60):
                n += 1
        self.assertEqual(n, 3, "每路每天上限 3 张（测试参数）")
        self.assertEqual(self.store.capped, 3)

    def test_62b_backfill_has_its_own_daily_budget(self):
        """补图有独立的日预算：检测池顶满后，补图照样能存。

        0.9.135 真机现象：AI 检测开着时每路 300 张上午就用完，共用一份预算的话
        之后一整天的运动事件补图全被 `capped` 挡掉 —— 生产机「升级后一张运动
        快照都没有」就是这么来的。
        """
        at = time.time()
        for i in range(3):
            self.assertTrue(self.store.offer("cam-01", JPEG, at + i * 60))
        self.assertEqual(self.store.capped, 0)
        self.assertFalse(self.store.offer("cam-01", JPEG, at + 300),
                         "检测池已顶满（3 张）")
        # 补图池另算：还能存 3 张，第 4 张才被拦
        for i in range(3):
            self.assertTrue(self.store.offer("cam-01", JPEG, at + 3 + i,
                                             gap=False),
                            "补图不该被检测池的用量挡住")
        self.assertFalse(self.store.offer("cam-01", JPEG, at + 9, gap=False))

    def test_63_queue_full_drops_without_blocking(self):
        """队列满就丢：宁可少一张图，也不能让检测线程等在磁盘上。"""
        st = es.ShotStore(self.var, queue_max=1)      # 不 start：没人消费
        st.offer("cam-01", JPEG, time.time())
        st.offer("cam-02", JPEG, time.time())
        self.assertGreaterEqual(st.dropped, 1)

    def test_64_oversized_and_empty_are_refused(self):
        self.assertFalse(self.store.offer("cam-01", b"", time.time()))
        self.assertFalse(self.store.offer("cam-01", b"x" * (es.MAX_SHOT_BYTES + 1),
                                          time.time()))

    def test_65_bad_cam_refused(self):
        self.assertFalse(self.store.offer("../etc", JPEG, time.time()))
        self.assertFalse(self.store.offer("", JPEG, time.time()))

    def test_66_status_shape(self):
        st = self.store.status()
        for k in ("written", "dropped", "failed", "bytes", "keep_days",
                  "max_per_day", "running", "dir"):
            with self.subTest(键=k):
                self.assertIn(k, st)
        self.assertEqual(st["keep_days"], 7)
        self.assertEqual(st["max_per_day"], 3)

    def test_67_sweep_keeps_window(self):
        """保留 7 天 = 今天 + 前 6 天；第 8 天之前的整个目录删掉。"""
        now = time.time()
        old = time.strftime("%Y-%m-%d", time.localtime(now - 8 * 86400))
        edge = time.strftime("%Y-%m-%d", time.localtime(now - 6 * 86400))
        for d in (old, edge, day_str(now)):
            self.put("cam-01", d, 100.0)
        removed = es.ShotStore(self.var).sweep(now)
        self.assertEqual(removed, 1)
        left = sorted(os.listdir(os.path.join(self.var, "event_shots", "cam-01")))
        self.assertEqual(left, [edge, day_str(now)])

    def test_68_sweep_ignores_foreign_dirs(self):
        """形状不对的目录一律不碰：宁可留点垃圾，也不能删错东西。"""
        root = es.root_of(self.var)
        os.makedirs(os.path.join(root, "not-a-date"), exist_ok=True)
        os.makedirs(os.path.join(root, "cam-01", "2020-01-01"), exist_ok=True)
        os.makedirs(os.path.join(root, "cam-01", "keepme"), exist_ok=True)
        es.ShotStore(self.var).sweep(time.time())
        self.assertTrue(os.path.isdir(os.path.join(root, "not-a-date")))
        self.assertTrue(os.path.isdir(os.path.join(root, "cam-01", "keepme")))
        self.assertFalse(os.path.isdir(os.path.join(root, "cam-01", "2020-01-01")))

    def test_69_part_file_never_readable(self):
        """写盘走 `.part` + replace：读的人不该看到 `.part` 被当成一张图。"""
        self.assertIsNone(es.parse_name("102341_000.jpg.part"))


class FakeStore(object):
    """只记账、不落盘：用来验检测侧"交出去的是什么"。"""

    def __init__(self):
        self.got = []
        self.boxes = []

    def offer(self, cam, data, at=None, dets=None):
        self.got.append((cam, data, at))
        self.boxes.append(es.boxes_of(dets))       # 与真实 offer 同一套归一化
        return True


class TestDetectHook(Tmp):
    """检测侧的挂钩（detect.DetectWorker._shot）：三条自我约束各测一条。"""

    def worker(self, store):
        return detect.DetectWorker(None, "ffmpeg", self.var, lambda: self.var,
                                   lambda: [], lambda: ({}, []),
                                   shot_store=store)

    def test_70_hands_over_source_bytes_and_mtime(self):
        src = os.path.join(self.var, "cam-01.jpg")
        with open(src, "wb") as f:
            f.write(JPEG)
        os.utime(src, (1770000000.0, 1770000000.0))
        store = FakeStore()
        self.worker(store)._shot({"id": "cam-01"}, src, time.time())
        self.assertEqual(len(store.got), 1)
        cam, data, at = store.got[0]
        self.assertEqual(cam, "cam-01")
        self.assertEqual(data, JPEG)
        self.assertAlmostEqual(at, 1770000000.0, delta=0.01)

    def test_71_no_store_is_noop(self):
        """shot_store=None 必须与 0.9.128 逐字一致：不读盘、不入队、不报错。"""
        w = self.worker(None)
        w._shot({"id": "cam-01"}, "/nonexistent/x.jpg", time.time())   # 不抛即可

    def test_72_missing_source_does_not_raise(self):
        store = FakeStore()
        self.worker(store)._shot({"id": "cam-01"},
                                 os.path.join(self.var, "nope.jpg"), time.time())
        self.assertEqual(store.got, [])

    def test_73_direct_mode_has_no_source(self):
        """直连抽帧没有源文件（src=""）：不存图，事件与通知照常。"""
        store = FakeStore()
        self.worker(store)._shot({"id": "cam-01"}, "", time.time())
        self.assertEqual(store.got, [])

    def test_74_oversized_file_is_skipped(self):
        src = os.path.join(self.var, "big.jpg")
        with open(src, "wb") as f:
            f.write(b"x" * (detect.SHOT_MAX_BYTES + 1))
        store = FakeStore()
        self.worker(store)._shot({"id": "cam-01"}, src, time.time())
        self.assertEqual(store.got, [])


class TestBoxes(Tmp):
    """检测框旁挂（0.9.130）：框跟着图走，但两件事互不牵连。

    守四件事：
      1. 坐标归一化并夹回 0~1（贴边的框会略出界，前端是按百分比定位的）；
      2. 认不出来的条目跳过、绝不抛 —— 这个函数在检测线程里被调用；
      3. 框写盘失败只丢框，图还在（计数分开，不能都算"写失败"）；
      4. 没有框就不写 json：老图与新图要能被同一条读取路径区分开。
    """

    DETS = [{"c": "person", "s": 0.91, "b": [0.5, 0.5, 0.2, 0.4]},
            {"c": "vehicle", "s": 0.42, "b": [0.2, 0.7, 0.3, 0.2]}]

    def test_90_boxes_are_normalized_and_kept(self):
        got = es.boxes_of(self.DETS)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["c"], "person")          # 置信度高的在前
        self.assertEqual(got[0]["b"], [0.5, 0.5, 0.2, 0.4])
        self.assertEqual(got[0]["s"], 0.91)

    def test_91_boxes_are_clamped(self):
        got = es.boxes_of([{"c": "person", "s": 1.4, "b": [-0.2, 1.8, 0.5, 0.5]}])
        self.assertEqual(got[0]["b"][0], 0.0)
        self.assertEqual(got[0]["b"][1], 1.0)
        self.assertEqual(got[0]["s"], 1.0)

    def test_92_bad_entries_are_skipped(self):
        got = es.boxes_of([None, {}, {"c": "person", "b": [0.5, 0.5]},
                           {"c": "person", "b": ["a", "b", "c", "d"]},
                           {"c": "person", "s": "x", "b": [0.5, 0.5, 0.1, 0.1]}])
        self.assertEqual(len(got), 1)                    # 只有最后一条能认
        self.assertEqual(got[0]["s"], 0.0)               # 置信度认不出按 0，不编一个

    def test_93_degenerate_box_is_skipped(self):
        """宽或高为 0 的框画出来就是一根线，存它没有意义。"""
        self.assertEqual(es.boxes_of([{"c": "person", "b": [0.5, 0.5, 0, 0.2]}]), [])

    def test_94_cap_and_order(self):
        dets = [{"c": "person", "s": i / 100.0, "b": [0.5, 0.5, 0.1, 0.1]}
                for i in range(30)]
        got = es.boxes_of(dets)
        self.assertEqual(len(got), es.MAX_BOXES)
        self.assertEqual(got[0]["s"], 0.29)              # 置信度最高的留下

    def test_95_meta_path_follows_jpg(self):
        p = es.meta_path_of(self.var, "cam-01", "2026-11-09", "102341_123.jpg")
        self.assertTrue(p.endswith("102341_123.json"))
        self.assertEqual(os.path.dirname(p),
                         os.path.dirname(es.path_of(self.var, "cam-01",
                                                    "2026-11-09", "102341_123.jpg")))
        self.assertIsNone(es.meta_path_of(self.var, "../x", "2026-11-09",
                                          "102341_123.jpg"))

    def test_96_store_writes_sidecar(self):
        """offer(dets=...) → 图与框都落盘，read_meta 读得回来。"""
        store = es.ShotStore(self.var)
        store.start()
        try:
            at = time.time()
            self.assertTrue(store.offer("cam-01", JPEG, at, self.DETS))
            self.assertEqual(wait_for(lambda: store.status()["written"] >= 1), True)
            self.assertEqual(wait_for(lambda: store.status()["meta"] >= 1), True)
        finally:
            store.stop()
        date = day_str(at)
        got = es.read_meta(self.var, "cam-01", date, es.name_at(at))
        self.assertEqual(len(got["boxes"]), 2)
        self.assertAlmostEqual(got["at"], at, delta=0.01)

    def test_97_store_without_boxes_writes_no_sidecar(self):
        store = es.ShotStore(self.var)
        store.start()
        try:
            at = time.time()
            store.offer("cam-01", JPEG, at)                # 没框（或没传）
            self.assertTrue(wait_for(lambda: store.status()["written"] >= 1))
        finally:
            store.stop()
        date = day_str(at)
        self.assertIsNone(es.read_meta(self.var, "cam-01", date, es.name_at(at)))
        self.assertEqual(store.status()["meta"], 0)

    def test_98_bad_json_reads_as_missing(self):
        """坏 json 当作"没存框"：不修、不重建、不抛。"""
        name = es.name_of(3723.0)
        p = es.meta_path_of(self.var, "cam-01", "2026-11-09", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(es.read_meta(self.var, "cam-01", "2026-11-09", name))

    def test_99_meta_of_contract(self):
        """meta_of：图在、框没有也要能给出一份能解释的答复。"""
        hit = {"name": es.name_of(37421.0), "cam": "cam-01",
               "date": "2026-11-09", "sec": 37421.0}
        got = es.meta_of(self.var, hit, 37421.0)
        self.assertEqual(got["boxes"], [])
        self.assertFalse(got["has_boxes"])
        self.assertEqual(got["at"], "10:23:41")
        self.assertEqual(got["offset"], 0.0)

        p = es.meta_path_of(self.var, "cam-01", "2026-11-09", hit["name"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"at": 37421.0, "boxes": self.DETS}, f)
        got = es.meta_of(self.var, hit, 37415.0)
        self.assertTrue(got["has_boxes"])
        self.assertEqual(got["count"], 2)
        self.assertAlmostEqual(got["offset"], 6.0, delta=0.1)


class TestBoxesHook(Tmp):
    """检测侧的框交付：交出去的就是这一拍过完区域过滤的框。"""

    def test_a0_dets_reach_store(self):
        src = os.path.join(self.var, "cam-01.jpg")
        with open(src, "wb") as f:
            f.write(JPEG)
        store = FakeStore()
        w = detect.DetectWorker(None, "ffmpeg", self.var, lambda: self.var,
                                lambda: [], lambda: ({}, []), shot_store=store)
        dets = [{"c": "person", "s": 0.8, "b": [0.5, 0.5, 0.2, 0.4]}]
        w._shot({"id": "cam-01"}, src, time.time(), dets)
        self.assertEqual(store.boxes, [[{"c": "person", "s": 0.8,
                                         "b": [0.5, 0.5, 0.2, 0.4]}]])


class TestGrabReturnsSource(Tmp):
    """_grab 必须把"这次真正读的那张图"带回来，而不是让调用方再问一次。

    再问一次会拿到**下一张**图的路径：那正是"拍现在、不拍当时"的假数据。
    """

    def test_80_grab_carries_source_path(self):
        src = os.path.join(self.var, "cam-01.jpg")
        with open(src, "wb") as f:
            f.write(JPEG)
        w = detect.DetectWorker(None, "ffmpeg", self.var, lambda: self.var,
                                lambda: [], lambda: ({}, []),
                                frame_path_fn=lambda cid: src)
        # 没有真 ffmpeg 时解图会失败，但 src 只在成功时才算数 —— 这里只验形状
        ok, data, err, got = w._grab({"id": "cam-01"}, "rtsp://x")
        self.assertEqual(len((ok, data, err, got)), 4)
        self.assertFalse(ok)

    def test_81_grab_no_new_frame_has_empty_source(self):
        w = detect.DetectWorker(None, "ffmpeg", self.var, lambda: self.var,
                                lambda: [], lambda: ({}, []),
                                frame_path_fn=lambda cid: "")
        ok, data, err, got = w._grab({"id": "cam-01"}, "rtsp://x")
        self.assertFalse(ok)
        self.assertEqual(err, detect.NO_NEW_FRAME)
        self.assertEqual(got, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
