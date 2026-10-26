#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡死看门狗的重启路径回归（0.9.78）。

真机 2026-10-26 05:06–05:09 的现场把两个放大器暴露得很清楚：

1. **收尾是串行的**。4 路几乎同时被判"卡住"，而 `_grace_close` 在 tick 主循环里
   同步等（15 秒 + 10 秒 / 路）。日志写着"5 秒后重连"，实际 47 秒后才全部起来
   （05:08:25 判卡 → 05:09:12 才"已启动"）。这一段是纯丢的录像，而且一路的
   收尾会挡住其它路的重启。现在只发 SIGINT，等待拆到后续巡检推进。

2. **目录被删后要白等 105 秒**。清盘把当天目录删掉时，ffmpeg 手里那个 .part
   已经是 unlink 掉的 inode，它继续写得好好的，但路径上永远长不出新文件 ——
   看门狗只能看到"从未写入"，必须等到 STALL_SEC+30 才动手。目录消失是无歧义
   的信号，现在立刻重建 + 立刻重连，且不算一次"卡住"。

顺带钉住一个纯文案 bug：`"（%s 秒没有新数据）"` 喂进 `"从未写入"` 之后
真机日志是「卡住（从未写入 秒没有新数据）」—— 单位丢了，读起来像乱码。
"""
import os
import signal
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "server"))

import recorder   # noqa: E402

URL = "rtsp://192.0.2.10/live"


class _LiveProc:
    """活着的替身进程：poll() 返回 None，记录收到的信号。"""

    def __init__(self, pid=999):
        self.pid = pid
        self.signals = []
        self.killed = False
        self._rc = None          # None = 还在跑

    def poll(self):
        return self._rc

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True

    def exit(self):
        self._rc = 0


def _mk(root=None):
    tmp = root or tempfile.mkdtemp(prefix="nvr-stall-")
    msgs = []
    r = recorder.Recorder("ffmpeg", tmp, [], log=msgs.append, var_dir=tmp)
    cam = {"id": "c1", "name": "客厅", "url": URL}
    r.cams = [cam]
    return r, cam, msgs, tmp


class TestCloseStepNonBlocking(unittest.TestCase):
    """_close_step 必须只推进一小步就返回，绝不阻塞。"""

    def test_escalation_and_no_blocking(self):
        st = {"closing": _LiveProc(), "close_at": 1015, "close_round": 1}
        p = st["closing"]

        # 还没到宽限期：什么都不做，但仍然报"在收尾"
        self.assertTrue(recorder.Recorder._close_step(None, st, 1000))
        self.assertEqual(p.signals, [])

        # 宽限到点：补一次 SIGINT（第 2 轮），不 kill
        self.assertTrue(recorder.Recorder._close_step(None, st, 1016))
        self.assertEqual(p.signals, [signal.SIGINT])
        self.assertFalse(p.killed, "第二段还得给 ffmpeg 时间写 moov")
        self.assertEqual(st["close_round"], 2)

        # 硬宽限也到点：才 SIGKILL
        self.assertTrue(recorder.Recorder._close_step(None, st, 1030))
        self.assertTrue(p.killed)
        self.assertEqual(st["close_round"], 3)

        # 进程真的退了：收尾结束，交回正常分支
        p.exit()
        self.assertFalse(recorder.Recorder._close_step(None, st, 1032))
        self.assertIsNone(st["closing"])

    def test_no_closing_is_a_noop(self):
        self.assertFalse(recorder.Recorder._close_step(None, {"p": None}, 1))

    def test_begin_close_skips_dead_proc(self):
        p = _LiveProc()
        p.exit()
        self.assertFalse(recorder.Recorder._begin_close(None, p))
        self.assertEqual(p.signals, [])


class TestTickStall(unittest.TestCase):
    """tick 判卡后：立刻返回、挂 closing、不再同步等。"""

    def _stuck(self, r, cam, now, started):
        p = _LiveProc()
        r.procs["c1"] = {"p": p, "day": "2026-10-26", "tries": 0, "stall": 0,
                         "next": 0, "started_at": started, "dead": None,
                         "sig": r._sig(cam)}
        return p

    def test_stall_marks_closing_without_waiting(self):
        r, cam, msgs, tmp = _mk()
        recorder.ensure_day(tmp, "c1")          # 目录在 → 走"卡住"而不是"目录被删"
        now = 1_000_000.0
        p = self._stuck(r, cam, now, now - 400)
        r._newest_age = lambda c: None          # 一个字节都没落盘
        r.tick(now)
        st = r.procs["c1"]
        self.assertIsNone(st["p"], "旧进程不能让 tick 继续当它是活的")
        self.assertIs(st["closing"], p, "旧进程必须挂到 closing 上由后续巡检收尾")
        self.assertEqual(p.signals, [signal.SIGINT], "第一刀必须是 SIGINT，不是 SIGKILL")
        self.assertFalse(p.killed)
        self.assertEqual(st["stall"], 1)
        self.assertGreater(st["close_at"], now, "宽限期从此刻开始算")

    def test_stall_log_has_no_orphan_unit(self):
        """「从未写入 秒没有新数据」是 0.9.77 及以前的文案 bug。"""
        r, cam, msgs, tmp = _mk()
        recorder.ensure_day(tmp, "c1")
        now = 1_000_000.0
        self._stuck(r, cam, now, now - 400)
        r._newest_age = lambda c: None
        r.tick(now)
        joined = "\n".join(msgs)
        self.assertIn("从未写入", joined)
        self.assertNotIn("从未写入 秒", joined, "单位被套在占位符上，读起来像乱码")

    def test_stall_log_keeps_real_age(self):
        r, cam, msgs, tmp = _mk()
        recorder.ensure_day(tmp, "c1")
        now = 1_000_000.0
        self._stuck(r, cam, now, now - 400)
        r._newest_age = lambda c: 82.0
        r.tick(now)
        self.assertTrue(any("82 秒没有新数据" in m for m in msgs), msgs)

    def test_no_spawn_while_closing(self):
        """收尾没完就 spawn = 同一路两个 ffmpeg 抢同一个分片名。"""
        r, cam, msgs, tmp = _mk()
        recorder.ensure_day(tmp, "c1")
        now = 1_000_000.0
        p = _LiveProc()
        r.procs["c1"] = {"p": None, "day": "2026-10-26", "tries": 0, "stall": 1,
                         "next": 0, "started_at": now, "dead": None,
                         "sig": r._sig(cam), "closing": p,
                         "close_at": now + recorder.KILL_SOFT_SEC, "close_round": 1}
        spawned = []
        r._spawn = lambda *a, **k: spawned.append(a)
        r.tick(now)
        self.assertEqual(spawned, [], "旧进程还活着，绝不能再起一个")

        # 进程退出后才允许重起
        p.exit()
        r.tick(now + 1)
        self.assertEqual(len(spawned), 1, "收尾完成后必须马上重连")
        self.assertIsNone(r.procs["c1"]["closing"])


class TestDirDeletedSelfHeal(unittest.TestCase):
    """清盘删掉当天目录：不该等 105 秒，应当立刻重建并重连。"""

    def test_missing_day_dir_restarts_immediately(self):
        r, cam, msgs, tmp = _mk()
        now = 1_000_000.0
        p = _LiveProc()
        # 刚启动 3 秒（远没到 STALL_SEC+30），但目录已经不在了
        r.procs["c1"] = {"p": p, "day": "2026-10-26", "tries": 0, "stall": 0,
                         "next": 0, "started_at": now - 3, "dead": None,
                         "sig": r._sig(cam)}
        r._newest_age = lambda c: None
        r.tick(now)
        st = r.procs["c1"]
        self.assertIs(st["closing"], p, "必须立刻收尾重启，而不是继续等")
        self.assertEqual(st["next"], now, "目录被删不该退避，下一轮就重起")
        self.assertEqual(st["stall"], 0, "不是摄像头的错，不许记成一次卡住")
        self.assertTrue(os.path.isdir(os.path.join(tmp, "c1",
                                                   recorder.datetime.date.today().isoformat())),
                        "重启前就要把当天目录建回来")
        self.assertTrue(any("目录被删除" in m for m in msgs), msgs)

    def test_existing_dir_still_waits(self):
        """目录好好的，刚启动 3 秒 → 不能因为"还没写入"就杀进程。"""
        r, cam, msgs, tmp = _mk()
        recorder.ensure_day(tmp, "c1")
        now = 1_000_000.0
        p = _LiveProc()
        r.procs["c1"] = {"p": p, "day": "2026-10-26", "tries": 0, "stall": 0,
                         "next": 0, "started_at": now - 3, "dead": None,
                         "sig": r._sig(cam)}
        r._newest_age = lambda c: None
        r.tick(now)
        self.assertIs(r.procs["c1"]["p"], p, "启动初期要给它建连和探测的时间")
        self.assertEqual(p.signals, [])


class TestKillClosesInFlight(unittest.TestCase):
    """停服务 / 删通道时，正在收尾的那个进程也不能漏成孤儿。"""

    def test_kill_waits_for_closing_proc(self):
        r, cam, msgs, tmp = _mk()
        p = _LiveProc()
        r.procs["c1"] = {"p": None, "closing": p, "close_at": 0, "close_round": 1}
        closed = []
        r._grace_close = lambda proc, why="": closed.append(proc) or True
        r._kill("c1")
        self.assertIn(p, closed, "closing 里的进程必须被一起收掉")
        self.assertNotIn("c1", r.procs)


if __name__ == "__main__":
    unittest.main(verbosity=1)
