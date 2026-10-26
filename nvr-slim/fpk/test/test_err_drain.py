#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制进程 stderr 排空线程的回归（0.9.84）。

真机 2026-10-26 的实测把病根钉死了：录制 ffmpeg 的 stderr=PIPE 只在进程
**死后**才被读一次（旧代码 `p.stderr.read(65536)` 在 tick 的 dead 分支里），
运行期间无人排空 —— 管道缓冲 64KB，攒满后 ffmpeg 卡死在 write 上假活
（进程在、盘不动），看门狗再杀掉重启 = 录像空洞。

ingest 层早有 `_drain_stderr` 且注释原话就是"必须有人读 stderr"，
录制层漏了。本文件钉住三件事：

1. 排空线程必须能让 ≥300KB 的 stderr 写入端**不被 64KB 管道缓冲塞死**；
2. 尾部缓冲 maxlen=64，`_stderr_text` 只回尾部且内容完整；
3. `_reset_errbuf` 换进程时清空旧残留（否则新进程一死，classify_error
   拿到的是上一轮的旧噪声）。
"""
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "server"))

import recorder  # noqa: E402


class TestErrDrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr_errdrain_")
        self.rec = recorder.Recorder("ffmpeg", self.tmp, [],
                                     log=lambda m: None)

    def tearDown(self):
        self.rec = None

    def _fake_proc(self, fd):
        return types.SimpleNamespace(stderr=os.fdopen(fd, "rb"))

    def test_300kb_writer_not_blocked_by_pipe(self):
        """写端灌 300KB（>64KB 管道缓冲），排空线程在跑就不能卡死写端。"""
        r, w = os.pipe()
        done = []

        def writer():
            for i in range(3000):
                os.write(w, (b"line-%05d-" % i) + b"x" * 88 + b"\n")
            os.write(w, b"FINAL-MARKER\n")
            os.close(w)
            done.append(True)

        tw = threading.Thread(target=writer, daemon=True)
        tw.start()
        # 同步跑排空：读到 EOF 才返回。若排空不存在，writer 会在 64KB 处
        # 永久阻塞，tw.join 超时、done 为空 —— 断言即失败。
        self.rec._drain_stderr("c1", self._fake_proc(r))
        tw.join(timeout=10)
        self.assertTrue(done, "写端 300KB 未写完 = 管道被塞满（排空未生效）")
        text = self.rec._stderr_text("c1")
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 64, "尾部缓冲必须封顶 64 行")
        self.assertTrue(lines[-1].startswith("FINAL-MARKER"),
                        "尾部必须保住最后几行（诊断只看尾部）")

    def test_stderr_text_empty_before_any_drain(self):
        self.assertEqual(self.rec._stderr_text("nope"), "")

    def test_reset_errbuf_clears_stale(self):
        """换进程必须清旧残留：预塞 100 条旧噪声，reset 后为空且带 maxlen。"""
        old = deque(("old-%d" % i for i in range(100)), maxlen=None)
        self.rec._errbuf["c1"] = old
        self.rec._reset_errbuf("c1")
        self.assertEqual(len(self.rec._errbuf["c1"]), 0)
        self.assertEqual(self.rec._errbuf["c1"].maxlen, 64)

    def test_drain_stops_on_closed_pipe(self):
        """写端立即关闭（进程秒死）：readline 返回空，线程路径正常收尾不抛。"""
        r, w = os.pipe()
        os.close(w)
        self.rec._drain_stderr("c1", self._fake_proc(r))
        self.assertEqual(self.rec._stderr_text("c1"), "")

    def test_drain_respects_stopped_flag(self):
        """stop() 之后排空循环应尽快退出，不给关机添堵。"""
        r, w = os.pipe()
        self.rec.stopped = True
        t0 = time.time()
        self.rec._drain_stderr("c1", self._fake_proc(r))
        os.close(w)
        self.assertLess(time.time() - t0, 2.0)


if __name__ == "__main__":
    unittest.main()
