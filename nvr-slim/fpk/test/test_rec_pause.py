#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制看门狗给上游取流让路的回归（0.9.82）。

真机 2026-10-26 09:32:25–09:35:14 的现场（0.9.81，清盘后 140 秒空洞）：

    09:32:25  五路目录被删 → 重建重启
    09:33:01  院子 R / 餐厅 / 车库 **同时**停 4~5 秒（相机端不再吐数据）
    09:34:13  录制层 75 秒看门狗先判死 → 三路录制重启（此时上游压根没数据）
    09:34:31  三路录制重连上本机端口，仍然没数据
    09:34:57  取流层终于打穿 120 秒耐心 → 重启 ffmpeg
    09:35:14  录制真正开始写盘

两个重启回合叠在一起，把「相机停了 120 秒」放大成「断了 140 秒录像」。
责任该单向划清：**等不等由取流决定（它看得见相机），录制只管把拿到的
字节写下去** —— 上游明确在等相机时，磁盘上没新字节不是录制的错。

这里钉住四件事：
1. 上游暂停时，录制进程一个字节都不许动（不重启、不计 stall）；
2. 上游恢复后如果还是没数据，照常重启（不许因为「曾经暂停过」就永久瘫痪）；
3. 没注入 pause_fn / 注入的函数抛异常，退回旧行为 —— 宁可多重启一次，
   也不能因为取流模块的小毛病把录制看门狗整体废掉；
4. 目录被删这条快路不受影响，它是无歧义的。
"""
import datetime
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "server"))

import recorder   # noqa: E402

URL = "rtsp://192.0.2.10/live"
SEG = recorder.STALL_SEC
CID = "c1"
_TODAY = datetime.date.today().isoformat()


class _LiveProc:
    """活着的替身进程：poll() 返回 None，记录收到的信号。"""

    def __init__(self, pid=1001):
        self.pid = pid
        self.signals = []

    def poll(self):
        return None

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvr-rec-pause-")
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.msgs = []
        self.r = recorder.Recorder("ffmpeg", self.root, [],
                                   log=self.msgs.append, var_dir=self.root)
        self.cam = {"id": CID, "name": "客厅", "url": URL}
        self.r.cams = [self.cam]

    def _armed(self):
        """放一个「进程活着、但磁盘上最新分片早已停止增长」的状态。"""
        day = os.path.join(self.root, CID, datetime.date.today().isoformat())
        os.makedirs(day, exist_ok=True)
        p = os.path.join(day, "00-00-00.mp4.part")
        with open(p, "wb") as f:
            f.write(b"\0" * 16)
        old = time.time() - SEG * 5
        os.utime(p, (old, old))          # 最后一片早就不长了
        proc = _LiveProc()
        self.r.procs[CID] = {
            "p": proc, "day": _TODAY, "tries": 0, "stall": 0,
            "next": 0, "started_at": old, "dead": None, "sig": None,
        }
        return proc

    def tick(self, now=None):
        self.r.tick(now=now if now is not None else time.time())


class TestPauseHandsOff(_Base):
    def test_upstream_paused_skips_restart(self):
        """上游在等相机时，录制看门狗必须让路。

        断言到「进程还是原来那个、一个信号都没发、stall 没涨」—— 只看
        「没重启」会漏掉后台已经在收尾的情形。
        """
        self.r.pause_fn = lambda cid: True
        proc = self._armed()
        self.tick()
        self.assertIs(self.r.procs[CID].get("p"), proc, "不许换进程")
        self.assertEqual(proc.signals, [], "不许给 ffmpeg 发任何信号")
        self.assertEqual(self.r.procs[CID].get("stall", 0), 0,
                         "相机没数据不该记到录制头上")
        self.assertTrue([m for m in self.msgs if "上游取流正在等相机" in m],
                        "日志要说清为什么不重启，否则看起来像看门狗失效")

    def test_pause_notice_is_throttled(self):
        """暂停期的提醒要节流：每 2 秒巡检都刷一条，日志就没法看了。"""
        self.r.pause_fn = lambda cid: True
        self._armed()
        for i in range(6):
            self.tick(now=time.time() + i * 2)
        hits = [m for m in self.msgs if "上游取流正在等相机" in m]
        self.assertEqual(len(hits), 1)

    def test_upstream_resumed_restarts_normally(self):
        """上游恢复后仍然没数据 —— 照常重启，不许被「曾经暂停」永久豁免。"""
        self.r.pause_fn = lambda cid: False
        proc = self._armed()
        self.tick()
        self.assertEqual(self.r.procs[CID].get("stall", 0), 1,
                         "恢复后还卡着就是真卡住，要记账")
        self.assertIn(recorder.signal.SIGINT, proc.signals)

    def test_missing_pause_fn_keeps_old_behaviour(self):
        """没注入（老调用方 / 取流没启用）时行为和以前完全一致。"""
        self._armed()
        self.tick()
        self.assertEqual(self.r.procs[CID].get("stall", 0), 1)

    def test_pause_fn_exception_is_contained(self):
        """pause_fn 抛异常时当 False 处理，绝不能让整轮巡检炸掉。"""
        def boom(cid):
            raise RuntimeError("取流模块挂了")
        self.r.pause_fn = boom
        self._armed()
        self.tick()
        self.assertEqual(self.r.procs[CID].get("stall", 0), 1,
                         "问不出来就按没有暂停办，宁可多重启一次")

    def test_dir_gone_still_fast_path(self):
        """目录被删这条路不受影响：它无歧义，即使上游暂停也要立刻重建。"""
        self.r.pause_fn = lambda cid: True
        proc = _LiveProc()
        self.r.procs[CID] = {
            "p": proc, "day": _TODAY, "tries": 0, "stall": 0,
            "next": 0, "started_at": time.time(), "dead": None, "sig": None,
        }
        day = os.path.join(self.root, CID, datetime.date.today().isoformat())
        self.assertFalse(os.path.isdir(day))
        self.tick()
        self.assertTrue(os.path.isdir(day), "目录消失要立刻重建")
        self.assertIn(recorder.signal.SIGINT, proc.signals,
                      "目录没了就得重启，不能因为上游暂停而跳过")


if __name__ == "__main__":
    unittest.main(verbosity=2)
