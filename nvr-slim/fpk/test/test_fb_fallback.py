#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.9.85 静默期直连兜底：切入/切出/冷却/指纹联动。

设计依据（真机 2026-10-26 哨兵实验 18/18）：上游取流停顿的窗口里，对同一
相机新开 RTSP 会话 90 秒全程零静默 —— 停流是「会话级」。录制平时读本机
分发端口，上游一停就没数据；兜底 = 上游停顿且磁盘断流超 8 秒时，录制进程
临时直连相机，上游恢复后切回。

这几条最容易写错的地方（所以每条都有测试钉着）：
  · 状态放 self._fb（实例级）而不是 procs[cid] —— 放进程表里会被
    收尸/重建推倒，切换后第一次重启模式就翻回去，sig 跟着变，
    无限重启循环；
  · 切换绝不直接碰进程，只改标志，让 sig 指纹变化驱动 reconcile；
  · 冷却期（FB_COOLDOWN）必须存在 —— 否则相机级故障（直连也没数据）
    时「切直连→没数据→切回→没数据」打摆，每 2 秒重启一次 ffmpeg。

跑：python test/test_fb_fallback.py
"""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "app", "server")
sys.path.insert(0, SERVER)

import recorder   # noqa: E402

CAM_URL = "rtsp://192.0.2.10:8554/ch3"
LOCAL = "tcp://127.0.0.1:12345?listen=0"


class _LiveProc:
    """活着的替身进程：poll() 返回 None，记录收到的信号。"""

    def __init__(self, pid=999):
        self.pid = pid
        self.signals = []
        self.killed = False
        self._rc = None

    def poll(self):
        return self._rc

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True


class TestUrlOf(unittest.TestCase):
    """_url_of 的三种取值：兜底直连 / 本机分发 / 取流未就绪回落直连。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-fb-")
        self.addCleanup(_rm, self.tmp)
        self.r = recorder.Recorder("ffmpeg", self.tmp, [], log=lambda m: None,
                                   var_dir=self.tmp)
        self.cam = {"id": "c1", "name": "车库", "url": CAM_URL}
        self.r.cams = [self.cam]
        self.r.url_fn = lambda cid: LOCAL

    def test_normal_uses_local_port(self):
        self.assertEqual(self.r._url_of(self.cam), LOCAL)

    def test_fallback_uses_camera_url(self):
        self.r._fb["c1"] = {"on": True, "at": time.time(), "back_at": 0}
        self.assertEqual(self.r._url_of(self.cam), CAM_URL,
                         "兜底中必须直连相机，不能还读本机端口")

    def test_url_fn_none_falls_back_to_camera(self):
        """取流没就绪（url_fn 返回 None）时回落直连 —— 0.9.80 的老约定不能破。"""
        self.r.url_fn = lambda cid: None
        self.assertEqual(self.r._url_of(self.cam), CAM_URL)

    def test_sig_changes_when_fb_flips(self):
        """标志一翻 sig 必须变 —— reconcile 靠它发现要按新地址重启。"""
        sig1 = self.r._sig(self.cam)
        self.r._fb["c1"] = {"on": True, "at": time.time(), "back_at": 0}
        self.assertNotEqual(self.r._sig(self.cam), sig1,
                            "兜底切换不改 sig = reconcile 永远不重启 = 白切")


class TestFbTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-fb-")
        self.addCleanup(_rm, self.tmp)
        self.msgs = []
        self.r = recorder.Recorder("ffmpeg", self.tmp, [], log=self.msgs.append,
                                   var_dir=self.tmp)
        self.cam = {"id": "c1", "name": "车库", "dir_name": "车库", "url": CAM_URL}
        self.r.cams = [self.cam]
        self.starved = [False]
        self.r.pause_fn = lambda cid: self.starved[0]
        self.now = time.time()

    def _old_segment(self, age_sec=30):
        """在当天目录放一个 age_sec 秒没动过的分片，喂给 _newest_age。"""
        import datetime
        day = os.path.join(self.tmp, "车库", datetime.date.today().isoformat())
        os.makedirs(day, exist_ok=True)
        f = os.path.join(day, "10-00-00.mp4")
        open(f, "wb").close()
        os.utime(f, (time.time() - age_sec,) * 2)

    def test_enter_when_starved_and_disk_stale(self):
        self._old_segment(30)
        self.starved[0] = True
        self.r._fb_tick(self.cam, self.now)
        fb = self.r._fb.get("c1") or {}
        self.assertTrue(fb.get("on"), "上游停顿+磁盘断流 8 秒+冷却已过：必须切入")
        self.assertTrue(any("直连相机" in m for m in self.msgs),
                        "切入必须留下日志（诚实 UI）")

    def test_no_enter_when_disk_fresh(self):
        """磁盘还有新数据（比如上游停顿刚发生）：不许切。"""
        self._old_segment(1)
        self.starved[0] = True
        self.r._fb_tick(self.cam, self.now)
        self.assertFalse((self.r._fb.get("c1") or {}).get("on"))

    def test_no_enter_when_upstream_ok(self):
        self._old_segment(30)
        self.starved[0] = False
        self.r._fb_tick(self.cam, self.now)
        self.assertFalse((self.r._fb.get("c1") or {}).get("on"))

    def test_cooldown_blocks_reenter(self):
        """切回后 90 秒内不许再切 —— 相机级故障时否则 2 秒一次打摆重启。"""
        self._old_segment(30)
        self.starved[0] = True
        self.r._fb["c1"] = {"on": False, "at": self.now - 60,
                            "back_at": self.now - 10}   # 10 秒前刚切回
        self.r._fb_tick(self.cam, self.now)
        self.assertFalse((self.r._fb.get("c1") or {}).get("on"),
                         "冷却期内不许再切入")
        # 冷却过了就允许
        self.r._fb["c1"]["back_at"] = self.now - recorder.FB_COOLDOWN - 1
        self.r._fb_tick(self.cam, self.now)
        self.assertTrue((self.r._fb.get("c1") or {}).get("on"))

    def test_exit_when_upstream_recovered_and_dwell_met(self):
        self.starved[0] = False
        self.r._fb["c1"] = {"on": True, "at": self.now - recorder.FB_EXIT_DWELL - 1,
                            "back_at": 0}
        self.r._fb_tick(self.cam, self.now)
        self.assertFalse((self.r._fb.get("c1") or {}).get("on"),
                         "上游恢复且直连跑满最短时长：必须切回")
        self.assertTrue(any("切回本机分发" in m for m in self.msgs))

    def test_stays_while_camera_level_outage(self):
        """相机级故障（直连也没数据 → starved 仍为 True）：不许切回。

        切回去只会落进退避循环；直连进程挂着等相机回来立刻续写更快。
        """
        self.starved[0] = True
        self.r._fb["c1"] = {"on": True, "at": self.now - 600, "back_at": 0}
        self.r._fb_tick(self.cam, self.now)
        self.assertTrue((self.r._fb.get("c1") or {}).get("on"))

    def test_no_exit_before_dwell(self):
        """上游刚恢复 1 秒就切回 = 抖动切换；必须等 FB_EXIT_DWELL。"""
        self.starved[0] = False
        self.r._fb["c1"] = {"on": True, "at": self.now - 3, "back_at": 0}
        self.r._fb_tick(self.cam, self.now)
        self.assertTrue((self.r._fb.get("c1") or {}).get("on"))

    def test_pause_fn_exception_is_safe(self):
        """starved_of 抛异常 = 当上游正常，绝不能把整轮 tick 炸掉。"""
        def boom(cid):
            raise RuntimeError("x")
        self.r.pause_fn = boom
        self._old_segment(30)
        self.r._fb_tick(self.cam, self.now)   # 不应抛
        self.assertFalse((self.r._fb.get("c1") or {}).get("on"))


class TestEnterThreshold(unittest.TestCase):
    """0.9.87：切入门槛 8 秒 → 25 秒。

    真机 18:06:45 那一例：分发层 4 秒停顿后自己重连就能恢复，录制端 8 秒
    就抢跑，把「上游自愈」放大成 23~26 秒的洞。门槛必须比上游的 15 秒耐心
    更长，否则每次抖动都要付一次来回切换的成本。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-fb-")
        self.addCleanup(_rm, self.tmp)
        self.msgs = []
        self.r = recorder.Recorder("ffmpeg", self.tmp, [], log=self.msgs.append,
                                   var_dir=self.tmp)
        self.cam = {"id": "c1", "name": "车库", "dir_name": "车库", "url": CAM_URL}
        self.r.cams = [self.cam]
        self.starved = [True]
        self.r.pause_fn = lambda cid: self.starved[0]
        self.now = time.time()

    def _old_segment(self, age_sec):
        import datetime
        day = os.path.join(self.tmp, "车库", datetime.date.today().isoformat())
        os.makedirs(day, exist_ok=True)
        f = os.path.join(day, "10-00-00.mp4")
        open(f, "wb").close()
        os.utime(f, (time.time() - age_sec,) * 2)

    def test_threshold_is_25(self):
        self.assertEqual(recorder.FB_ENTER_SEC, 25,
                         "25 秒是「上游 15 秒耐心 + 重启重连」的经验值，改动要有真机依据")

    def test_no_enter_at_20s(self):
        """20 秒：旧口径（8 秒）会切，新口径必须继续等 —— 上游还能自愈。"""
        self._old_segment(20)
        self.r._fb_tick(self.cam, self.now)
        self.assertFalse((self.r._fb.get("c1") or {}).get("on"))

    def test_enter_at_26s(self):
        self._old_segment(26)
        self.r._fb_tick(self.cam, self.now)
        self.assertTrue((self.r._fb.get("c1") or {}).get("on"))


class TestGlobalStall(unittest.TestCase):
    """0.9.87：多路同时停顿 = NAS 侧事件，静默期内不切直连。

    真机 18:06:50 四路同一秒停顿、18:06:54 四路一起切直连 —— 在 CPU/IO 最紧的
    时刻再开四个 ffmpeg，把抖动放大成 23~26 秒洞；18:24 三路同形，车库连环
    三个洞。判据必须用「转 starved 的时刻」，否则院子 R 那种长期 starved 的
    相机会把任何单路抖动凑成「两路」，拦掉真该兜底的那一路。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-fb-")
        self.addCleanup(_rm, self.tmp)
        self.msgs = []
        self.r = recorder.Recorder("ffmpeg", self.tmp, [], log=self.msgs.append,
                                   var_dir=self.tmp)
        self.cams = [{"id": "c1", "name": "车库", "dir_name": "车库", "url": CAM_URL},
                     {"id": "c2", "name": "餐厅", "dir_name": "餐厅", "url": CAM_URL}]
        self.r.cams = self.cams
        self.starved = set()
        self.r.pause_fn = lambda cid: cid in self.starved
        self.now = time.time()

    def _old_segment(self, cid, dname, age_sec):
        import datetime
        day = os.path.join(self.tmp, dname, datetime.date.today().isoformat())
        os.makedirs(day, exist_ok=True)
        f = os.path.join(day, "10-00-00.mp4")
        open(f, "wb").close()
        os.utime(f, (time.time() - age_sec,) * 2)

    def test_two_cams_same_moment_blocks_entry(self):
        self._old_segment("c1", "车库", 60)
        self._old_segment("c2", "餐厅", 60)
        self.starved = {"c1", "c2"}
        self.r._fb_scan_global(self.now)          # 两路同一刻转 starved
        self.assertGreater(self.r._fb_quiet_until, self.now,
                           "多路同停必须立起静默期")
        self.r._fb_tick(self.cams[0], self.now)
        self.assertFalse((self.r._fb.get("c1") or {}).get("on"),
                         "静默期内磁盘断流再久也不许切直连")
        self.assertTrue(any("NAS 侧事件" in m for m in self.msgs),
                        "判定为全局事件必须留下日志（诚实 UI）")

    def test_quiet_expires_then_enters(self):
        self._old_segment("c1", "车库", 60)
        self._old_segment("c2", "餐厅", 60)
        self.starved = {"c1", "c2"}
        self.r._fb_scan_global(self.now)
        later = self.now + recorder.FB_GLOBAL_QUIET + 1
        self.r._fb_scan_global(later)             # 两路仍停，但已过窗口 → 不再续期
        self.r._fb_tick(self.cams[0], later)
        self.assertTrue((self.r._fb.get("c1") or {}).get("on"),
                        "静默期过后仍无数据：必须兜底，不能永久不救")

    def test_single_cam_stall_still_enters(self):
        """只有一路停（单相机掉线）：不许被全局规则误伤。"""
        self._old_segment("c1", "车库", 60)
        self.starved = {"c1"}
        self.r._fb_scan_global(self.now)
        self.assertLessEqual(self.r._fb_quiet_until, self.now, "单路停顿不是全局事件")
        self.r._fb_tick(self.cams[0], self.now)
        self.assertTrue((self.r._fb.get("c1") or {}).get("on"))

    def test_long_starved_cam_does_not_count_as_fresh(self):
        """院子 R 这类长期 starved 的相机：不能把另一路的正常抖动凑成两路。"""
        self._old_segment("c1", "车库", 60)
        self.starved = {"c1", "c2"}
        self.r._fb_scan_global(self.now - 600)    # c2 早在 10 分钟前就停了
        self.starved = {"c1", "c2"}
        self.r._fb_scan_global(self.now)          # 此刻 c1 才转停 → 只有 1 路是"新鲜"的
        self.assertLessEqual(self.r._fb_quiet_until, self.now)
        self.r._fb_tick(self.cams[0], self.now)
        self.assertTrue((self.r._fb.get("c1") or {}).get("on"),
                        "长期 starved 的相机不该把别的路的兜底拦掉")

    def test_scan_runs_before_tick_in_loop(self):
        """扫描必须排在逐路 _fb_tick 之前，否则同轮的切入判断吃不到静默期。"""
        order = []
        self.r.ffmpeg = "ffmpeg"
        self.r.enabled = True
        self.r._spawn = lambda *a, **k: None
        self.r._fb_scan_global = lambda now: order.append("scan")
        self.r._fb_tick = lambda cam, now: order.append("tick")
        self.r.tick(self.now)
        self.assertEqual(order[:2], ["scan", "tick"])

    def test_stop_clears_global_state(self):
        self.starved = {"c1", "c2"}
        self.r._fb_scan_global(self.now)
        self.r.stop()
        self.assertEqual(self.r._fb_starve_since, {})
        self.assertEqual(self.r._fb_quiet_until, 0.0)


class TestStateSurvivesRespawn(unittest.TestCase):
    """兜底状态是实例级的：收尸/重建不得把它弄丢。"""

    def test_state_not_in_procs(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-fb-")
        self.addCleanup(_rm, self.tmp)
        r = recorder.Recorder("ffmpeg", self.tmp, [], log=lambda m: None,
                              var_dir=self.tmp)
        r._fb["c1"] = {"on": True, "at": time.time(), "back_at": 0}
        r.procs["c1"] = {"p": None}          # 模拟进程表被清掉重建
        cam = {"id": "c1", "name": "车库", "url": CAM_URL}
        r.url_fn = lambda cid: LOCAL
        self.assertEqual(r._url_of(cam), CAM_URL,
                         "procs 重建后兜底状态必须仍然生效（否则 sig 翻转成循环）")

    def test_stop_clears_state(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-fb-")
        self.addCleanup(_rm, self.tmp)
        r = recorder.Recorder("ffmpeg", self.tmp, [], log=lambda m: None,
                              var_dir=self.tmp)
        r._fb["c1"] = {"on": True, "at": time.time(), "back_at": 0}
        r.stop()
        self.assertEqual(r._fb, {}, "停服后兜底状态必须清空")


class TestFastSwitchClose(unittest.TestCase):
    """0.9.86 热修：切换走非阻塞收尾 + 快速升级。

    0.9.85 首跑真机实测：reconcile 的同步 _kill 在星量进程上吃满 15+10 秒
    还阻塞整个调度循环，两路同时切换串行 47 秒，吃掉兜底大部分收益。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-fb-")
        self.addCleanup(_rm, self.tmp)
        self.msgs = []
        self.r = recorder.Recorder("ffmpeg", self.tmp, [], log=self.msgs.append,
                                   var_dir=self.tmp)
        self.cam = {"id": "c1", "name": "车库", "url": CAM_URL}
        self.r.cams = [self.cam]
        self.r.url_fn = lambda cid: LOCAL

    def _live(self, now):
        p = _LiveProc()
        self.r.procs["c1"] = {"p": p, "day": "2026-10-26", "tries": 0,
                              "stall": 0, "next": 0, "started_at": now,
                              "dead": None, "sig": self.r._sig(self.cam)}
        return p

    def _flip(self, now):
        self.r._fb["c1"] = {"on": True, "at": now, "back_at": 0,
                            "flipped_at": now}

    def test_fb_switch_is_nonblocking_and_fast(self):
        now = time.time()
        p = self._live(now)
        self._flip(now)
        self.r.reconcile([self.cam])
        st = self.r.procs["c1"]
        self.assertIsNone(st["p"], "切换后立刻置空 p，绝不同步等收尾")
        self.assertIsNotNone(st.get("closing"), "挂上非阻塞收尾状态")
        self.assertLessEqual(st["close_at"] - now, recorder.FB_CLOSE_SOFT + 0.5,
                             "兜底切换必须用 3 秒快速升级，不能 15 秒")
        self.assertEqual(st["close_hard"], recorder.FB_CLOSE_HARD)
        self.assertFalse(p.killed,
                         "SIGKILL 由 _close_step 按节奏推进，这里不发")
        self.assertTrue(any("直连兜底切换" in m for m in self.msgs))

    def test_config_change_keeps_slow_close(self):
        """普通配置变更（非兜底）仍走 15/10 秒优雅收尾。"""
        now = time.time()
        p = self._live(now)
        self.r.reconcile([self.cam])     # sig 相同？
        # 改分片长度制造真实的配置变更
        self.cam["segment_sec"] = 60
        self.r.reconcile([self.cam])
        st = self.r.procs["c1"]
        self.assertGreater(st["close_at"] - now, recorder.KILL_SOFT_SEC - 1,
                           "配置变更保留 15 秒优雅收尾（写完 moov）")
        self.assertEqual(st["close_hard"], recorder.KILL_HARD_SEC)

    def test_stale_flip_is_treated_as_config_change(self):
        """兜底翻转超过 180 秒后的 sig 变化按普通配置变更处理。"""
        now = time.time()
        p = self._live(now)
        self._flip(now - 200)
        self.cam["segment_sec"] = 60
        self.r.reconcile([self.cam])
        st = self.r.procs["c1"]
        self.assertGreater(st["close_at"] - now, recorder.KILL_SOFT_SEC - 1)

    def test_plain_mp4_mode_keeps_slow_close(self):
        """frag=False（纯 MP4）不吃快速升级：SIGKILL 会整段丢。"""
        now = time.time()
        p = self._live(now)
        self.r.frag = False
        self._flip(now)
        self.r.reconcile([self.cam])
        st = self.r.procs["c1"]
        self.assertGreater(st["close_at"] - now, recorder.KILL_SOFT_SEC - 1)

    def test_close_step_uses_per_cam_hard(self):
        """_close_step 的硬超时按 st.close_hard 走（4 秒），不再固定 10 秒。"""
        now = time.time()
        p = _LiveProc()
        st = {"closing": p, "close_at": now + recorder.FB_CLOSE_SOFT,
              "close_round": 1, "close_hard": recorder.FB_CLOSE_HARD}
        recorder.Recorder._close_step(None, st, now + recorder.FB_CLOSE_SOFT)
        self.assertEqual(st["close_round"], 2)
        # 第二轮：4 秒后应 SIGKILL（固定 10 秒的话还没到点）
        recorder.Recorder._close_step(None, st,
                                      now + recorder.FB_CLOSE_SOFT
                                      + recorder.FB_CLOSE_HARD)
        self.assertTrue(p.killed, "close_hard=4 秒到点必须 SIGKILL")


def _rm(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
