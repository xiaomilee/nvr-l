#!/usr/bin/env python3
"""find_ffmpeg / find_ffprobe 查找逻辑：自带优先、PATH 回落、绝对路径兜底、可执行校验。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import runtime


class FindFfmpegTest(unittest.TestCase):
    def test_prefers_bundled_bin(self):
        app = tempfile.mkdtemp()
        with mock.patch.object(runtime, "_usable", return_value=True):
            self.assertEqual(runtime.find_ffmpeg(app),
                             os.path.join(app, "bin", "ffmpeg"))
            self.assertEqual(runtime.find_ffprobe(app),
                             os.path.join(app, "bin", "ffprobe"))

    def test_absolute_fallback_when_path_empty(self):
        """服务以精简 PATH 运行时 which() 会漏掉 apt 装到 /usr/bin 的 ffmpeg，靠绝对路径兜底。"""
        app = tempfile.mkdtemp()
        fake = "/usr/bin/ffmpeg"

        def fake_usable(p):
            return p == fake

        with mock.patch.object(runtime.shutil, "which", return_value=None), \
             mock.patch.object(runtime, "_usable", side_effect=fake_usable), \
             mock.patch.object(runtime, "_FFMPEG_ABS", (fake,)), \
             mock.patch.object(runtime, "_FFPROBE_ABS", ()):
            self.assertEqual(runtime.find_ffmpeg(app), fake)

    def test_returns_none_when_missing(self):
        app = tempfile.mkdtemp()
        with mock.patch.object(runtime.shutil, "which", return_value=None), \
             mock.patch.object(runtime, "_usable", return_value=False), \
             mock.patch.object(runtime, "_FFMPEG_ABS", ()), \
             mock.patch.object(runtime, "_FFPROBE_ABS", ()):
            self.assertIsNone(runtime.find_ffmpeg(app))
            self.assertIsNone(runtime.find_ffprobe(app))

    def test_usable_requires_exec(self):
        p = "/some/ffmpeg"
        with mock.patch.object(runtime.os.path, "isfile", return_value=True), \
             mock.patch.object(runtime.os, "access", return_value=False):
            self.assertFalse(runtime._usable(p))
        with mock.patch.object(runtime.os.path, "isfile", return_value=True), \
             mock.patch.object(runtime.os, "access", return_value=True):
            self.assertTrue(runtime._usable(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
