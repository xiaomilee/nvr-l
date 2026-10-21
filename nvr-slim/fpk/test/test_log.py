#!/usr/bin/env python3
"""nvr_slim 运行日志测试：环形缓冲、凭据脱敏、清空。"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import nvr_slim  # noqa: E402


class LogTest(unittest.TestCase):
    def setUp(self):
        nvr_slim._LOG.clear()
        self.var = tempfile.mkdtemp(prefix="nvr-log-")
        nvr_slim.STATE["var_dir"] = self.var

    def test_log_line_appends_and_redacts(self):
        nvr_slim.log_line("快照失败：cam-01 rtsp://admin:secret123@192.168.2.116:8554/ch1 401")
        text = "\n".join(nvr_slim._LOG)
        self.assertIn("快照失败", text)
        self.assertNotIn("secret123", text)          # 密码已脱敏
        self.assertNotIn("admin:secret123", text)
        self.assertIn("rtsp://***@192.168.2.116", text)

    def test_log_clear(self):
        nvr_slim.log_line("a")
        nvr_slim.log_line("b")
        nvr_slim.log_clear()
        self.assertEqual(len(nvr_slim._LOG), 0)

    def test_log_persists_to_file(self):
        nvr_slim.log_line("写入文件")
        p = nvr_slim._log_path()
        self.assertTrue(os.path.isfile(p))
        with open(p, encoding="utf-8") as f:
            self.assertIn("写入文件", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
