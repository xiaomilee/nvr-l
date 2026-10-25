#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 检测启动路径的回归（两个真机事故的守卫）。

事故一（0.9.65）：装 0.9.64 后「无法启用 NVR-L · 服务启动超时」——
    engine = detect.Engine(STATE["app_dir"], STATE["var_dir"], log=_log_line)
    NameError: name '_log_line' is not defined.
成因：_log_line 以前只在 main() 里定义成局部闭包，而 _make_detect_worker /
_sync_detect_worker 是模块级函数、把它当全局名用。detect.enable=true 时
启动必走这条（设置页热切换会把开启态持久化，重启照旧）→ 主线程挂掉、
端口再也不起来。

事故二（0.9.66）：真机「回放页没有任何 AI 标记」——stats 照常涨
（per_cam.cls 有 person/vehicle 计数），但台账里一条 AI 事件都没有。
成因：_write_ledger 把「当天钟点秒」的窗口直接传给 ledger.merge，而
merge 的契约是「分片内相对秒」（内部再加 start_sec 换算回钟点秒）。
双重偏移后窗口全部 >86400 被 min(DAY_SEC) 夹掉、e<=s 丢弃，AI 每 5 秒
勤勤恳恳写进去的全是 windows:[] 的空源。

守卫：
  B1  _log_line 在模块作用域可解析、可调用
  B2  _make_detect_worker 真跑一遍不抛（DetectWorker 用替身，不起线程）
  B3  _sync_detect_worker(enable=false) 不起 worker 也不抛
  B4  _sync_detect_worker(enable=true) 能起、能再关掉
  B5  传给 Engine / DetectWorker 的 log 回调是模块级 _log_line 本体
  B6  _write_ledger 落账后 events_of 返回带 cls 的事件（修复前为空）
  B7  同桶再次命中：与文件里旧窗口正确合并（同一坐标系）
  B8  桶末跨桶窗口不丢；桶名与 5 分钟对齐（MM 是 5 的倍数）
  B9  30 秒在场口径：抓帧超时漏出的 18 秒空隙仍并成同一段（0.9.69）

跑：python test/test_detect_boot.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "app", "server")
sys.path.insert(0, SERVER)

import nvr_slim  # noqa: E402
import detect    # noqa: E402
import ledger    # noqa: E402

CAM = {"id": "cam-boot-test", "name": "回归"}


class FakeWorker:
    """不起线程的 DetectWorker 替身：只记构造参数，start/stop/alive 空转。"""

    last = None

    def __init__(self, *a, **kw):
        self.args, self.kwargs = a, kw
        FakeWorker.last = self
        self._alive = False

    def start(self):
        self._alive = True

    def stop(self):
        self._alive = False

    def alive(self):
        return self._alive

    def status(self):
        return {"running": self._alive}


class _FakeWorkerCtx:
    """把 detect.DetectWorker 临时换成替身（不起线程的热切换用例用）。"""

    def __init__(self):
        self._real = None

    def __enter__(self):
        self._real = detect.DetectWorker
        detect.DetectWorker = FakeWorker
        return FakeWorker

    def __exit__(self, *a):
        detect.DetectWorker = self._real


class DetectBootTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-detect-boot-")
        self._saved = dict(nvr_slim.STATE)
        nvr_slim.STATE.clear()
        nvr_slim.STATE.update({
            "app_dir": SERVER,
            "var_dir": self.tmp,
            "ffmpeg_bin": "/bin/true",   # 只要有值就能过 _make_detect_worker 的门
            "notifier": None,
            "detect_worker": None,
        })
        # 今天 23:00:00 的本地 epoch（tod=82800，5 分钟桶起点）
        lt = time.localtime()
        self.now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                23, 0, 0, 0, 0, -1))
        self.date = time.strftime("%Y-%m-%d", time.localtime(self.now))

    def tearDown(self):
        nvr_slim.STATE.clear()
        nvr_slim.STATE.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _worker(self):
        """真 DetectWorker（不起线程）：_write_ledger 只用 record_root_fn。"""
        return detect.DetectWorker(
            None, None, self.tmp, lambda: self.tmp, lambda: [], lambda: ({}, []))

    def _day_events(self):
        d = ledger.load(self.tmp, CAM["id"], self.date) or {}
        return d, ledger.events_of(d)

    # ---------------------------------------------------------- 事故一

    def test_b1_module_level_log_alias(self):
        """_log_line 必须是模块级可解析名（事故一里它不是）。"""
        self.assertTrue(hasattr(nvr_slim, "_log_line"))
        self.assertTrue(callable(nvr_slim._log_line))
        nvr_slim._log_line("boot-test")     # 真调一次，不能抛

    def test_b2_make_worker_no_nameerror(self):
        """_make_detect_worker 整条跑完不抛——修复前这里就是 NameError。"""
        with _FakeWorkerCtx():
            w = nvr_slim._make_detect_worker({})
        self.assertIsNotNone(w)
        self.assertIsInstance(w, FakeWorker)
        self.assertIs(w.kwargs.get("log"), nvr_slim._log_line)

    def test_b3_sync_off_no_worker(self):
        """AI 关：不起 worker、不抛（旧版本这条本来就没问题，防回归）。"""
        nvr_slim._sync_detect_worker({"detect": {"enable": False}})
        self.assertIsNone(nvr_slim.STATE.get("detect_worker"))

    def test_b4_sync_on_starts_and_stops(self):
        """AI 开：起 worker；再推一份 enable=false：停掉。热切换两条路都走通。"""
        with _FakeWorkerCtx():
            nvr_slim._sync_detect_worker({"detect": {"enable": True}})
            w = nvr_slim.STATE.get("detect_worker")
            self.assertIsNotNone(w)
            self.assertTrue(w.alive())
            nvr_slim._sync_detect_worker({"detect": {"enable": False}})
            self.assertFalse(w.alive())

    def test_b5_engine_gets_log(self):
        """事故那一行：Engine(..., log=_log_line) 必须拿到真回调。"""
        with _FakeWorkerCtx():
            w = nvr_slim._make_detect_worker({"detect": {"enable": True}})
        eng = w.args[0]
        self.assertIsInstance(eng, detect.Engine)
        self.assertIs(eng.log, nvr_slim._log_line)

    # ---------------------------------------------------------- 事故二

    def test_b6_write_ledger_lands_events(self):
        """AI 命中必须落成带 cls 的台账事件（修复前 events_of 为空）。"""
        w = self._worker()
        w._write_ledger(CAM, [{"c": "person", "b": [0.1, 0.1, 0.2, 0.2]}],
                        self.now)
        d, ev = self._day_events()
        srcs = d.get("sources") or {}
        self.assertIn("ai:23-00-00.mp4", srcs, "桶名应与 5 分钟对齐")
        wins = srcs["ai:23-00-00.mp4"].get("windows") or []
        self.assertTrue(wins, "windows 不能为空（事故二：绝对秒双重偏移被夹掉）")
        self.assertEqual(len(ev), 1, "events_of 应返回 1 个 AI 事件")
        e = ev[0]
        self.assertAlmostEqual(e["s"], 82799.0, delta=1.0)
        self.assertAlmostEqual(e["e"], 82801.0, delta=1.0)
        self.assertEqual(e["cls"].get("person"), 1)

    def test_b7_same_bucket_merges_old_windows(self):
        """同桶二次命中：与文件里的旧窗口按同一坐标系合并（gap 内并窗）。"""
        w = self._worker()
        w._write_ledger(CAM, [{"c": "person", "b": [0.1, 0.1, 0.2, 0.2]}],
                        self.now)
        w._write_ledger(CAM, [{"c": "person", "b": [0.3, 0.3, 0.4, 0.4]}],
                        self.now + 2.0)          # 23:00:02，同桶、间隔 2 秒
        d, ev = self._day_events()
        srcs = d.get("sources") or {}
        wins = (srcs.get("ai:23-00-00.mp4") or {}).get("windows") or []
        self.assertEqual(len(wins), 1, "间隔 2s 的两次命中应并成一个窗口")
        self.assertAlmostEqual(wins[0][0], 82799.0, delta=1.0)
        self.assertAlmostEqual(wins[0][1], 82803.0, delta=1.0)
        self.assertEqual(len(ev), 1)

    def test_b8_bucket_edge_and_name(self):
        """桶末（23:04:59）命中不丢；桶名 MM 必须是 5 的倍数。"""
        w = self._worker()
        edge = self.now + 4 * 60 + 59            # 23:04:59，仍在 23:00 桶
        w._write_ledger(CAM, [{"c": "vehicle", "b": [0.5, 0.5, 0.9, 0.9]}],
                        edge)
        d, ev = self._day_events()
        srcs = d.get("sources") or {}
        self.assertIn("ai:23-00-00.mp4", srcs,
                      "23:04:59 属于 23:00 桶，不能按分钟命名")
        wins = (srcs.get("ai:23-00-00.mp4") or {}).get("windows") or []
        self.assertTrue(wins, "桶末命中不能丢窗口")
        self.assertLess(wins[0][0], wins[0][1])
        self.assertLessEqual(wins[0][1], 86400.0)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["cls"].get("vehicle"), 1)
        # 桶名分钟数必须是 5 的倍数（对齐 start_sec）
        for k in srcs:
            mm = int(k.split(":")[1].split("-")[1])
            self.assertEqual(mm % 5, 0, "桶名分钟未对齐 5 分钟：%s" % k)

    def test_b9_gap_30s_holds_vehicle_in_place(self):
        """0.9.69：两次命中间隔 18 秒仍是同一辆车（5 秒口径下会裂成两条事件）。

        真机上抓帧超时 10 秒很常见，一次超时就漏出 10~20 秒空隙；一辆车停在
        画面里本该是「一条 10 分钟的事件」，旧口径下被记成几十条 2 秒事件。
        """
        self.assertEqual(getattr(ledger, "AI_GAP_SEC", None), 30.0)
        self.assertEqual(detect.DetectWorker._merge_windows(
            [[100, 102, {"vehicle": 1}], [120, 122, {"vehicle": 1}]]),
            [[100.0, 122.0, {"vehicle": 1}]])
        w = self._worker()
        w._write_ledger(CAM, [{"c": "vehicle", "b": [0.5, 0.5, 0.9, 0.9]}],
                        self.now)
        w._write_ledger(CAM, [{"c": "vehicle", "b": [0.5, 0.5, 0.9, 0.9]}],
                        self.now + 20.0)         # 23:00:20，同桶、间隔 18 秒
        d, ev = self._day_events()
        wins = ((d.get("sources") or {}).get("ai:23-00-00.mp4") or {}).get("windows") or []
        self.assertEqual(len(wins), 1, "间隔 18s ≤ 30s：应并成一个窗口")
        self.assertEqual(len(ev), 1, "一次在场不能被切成多条事件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
