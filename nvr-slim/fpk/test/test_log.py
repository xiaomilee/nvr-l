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
        nvr_slim._ACCESS.clear()
        nvr_slim._ACCESS_TOTAL[0] = 0
        nvr_slim._ACCESS_AGG.update({"path": None, "n": 0, "shown": 0})
        self.var = tempfile.mkdtemp(prefix="nvr-log-")
        nvr_slim.STATE["var_dir"] = self.var

    def test_log_line_appends_and_redacts(self):
        nvr_slim.log_line("快照失败：cam-01 rtsp://admin:secret123@192.0.2.16:8554/ch1 401")
        text = "\n".join(nvr_slim._LOG)
        self.assertIn("快照失败", text)
        self.assertNotIn("secret123", text)          # 密码已脱敏
        self.assertNotIn("admin:secret123", text)
        self.assertIn("rtsp://***@192.0.2.16", text)

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


class AccessLogSplitTest(unittest.TestCase):
    """访问日志不能挤业务日志（0.9.73）。

    真机现场：手机点一次回放，787 条 /api/record/file 刷进去，800 条环形
    缓冲被整段覆盖，诊断页只剩 1 条业务日志 —— 排查等于瞎。
    """

    def setUp(self):
        nvr_slim._LOG.clear()
        nvr_slim._ACCESS.clear()
        nvr_slim._ACCESS_TOTAL[0] = 0
        nvr_slim._ACCESS_AGG.update({"path": None, "n": 0, "shown": 0})
        self.var = tempfile.mkdtemp(prefix="nvr-acc-")
        nvr_slim.STATE["var_dir"] = self.var

    def _access(self, path, q="", code=200):
        nvr_slim._record_access(
            "%s \"GET %s%s HTTP/1.1\" %d -" % ("03:00:00", path, q, code))

    def test_access_does_not_touch_business_log(self):
        nvr_slim.log_line("录制 车库 中断")
        for i in range(500):
            self._access("/api/record/file", "?cam=c1&name=02-48-00.mp4&i=%d" % i)
        self.assertEqual(len(nvr_slim._LOG), 1, "业务日志必须一条不少")
        self.assertIn("录制 车库 中断", nvr_slim._LOG[0])

    def test_business_overflow_does_not_touch_access(self):
        for i in range(900):
            nvr_slim.log_line("快照失败 %d" % i)
        self.assertEqual(len(nvr_slim._ACCESS), 0)
        self.assertEqual(len(nvr_slim._LOG), 800, "业务环上限仍是 800")

    def test_same_path_collapsed(self):
        """100 次同路径请求 → 环里只留首条，次数尾巴附在读取结果里。"""
        for _ in range(100):
            self._access("/api/record/file", "?cam=c1")
        self.assertEqual(len(nvr_slim._ACCESS), 1, "折叠后环里只有首条")
        self.assertEqual(nvr_slim._ACCESS[0].count("GET /api/record/file"), 1)
        total, lines = nvr_slim._access_snapshot()
        self.assertEqual(total, 100, "总量要如实统计")
        self.assertEqual(len(lines), 2, "首条 + 一条次数尾巴")
        self.assertIn("累计 100 次", lines[1])

    def test_reading_log_does_not_grow_ring(self):
        """读一次 /api/log 不能自己长出一条尾巴 —— 否则 120 条小环会被
        「累计 N 次」填满，换成另一种刷屏。"""
        for _ in range(50):
            self._access("/api/record/file", "?cam=c1")
        for _ in range(5):
            nvr_slim._access_snapshot()
        self.assertEqual(len(nvr_slim._ACCESS), 1,
                         "只读不写：环里仍然只有首条")

    def test_burst_end_lands_in_ring(self):
        """一段连续请求结束（路径变了）时，次数尾巴要真落进环里存档。"""
        for _ in range(30):
            self._access("/api/record/file", "?cam=c1")
        self._access("/api/status")
        joined = "\n".join(nvr_slim._ACCESS)
        self.assertIn("累计 30 次", joined)

    def test_different_path_not_collapsed(self):
        self._access("/api/recordings")
        self._access("/api/ledger", "?cam=c1")
        joined = "\n".join(nvr_slim._ACCESS)
        self.assertIn("/api/recordings", joined)
        self.assertIn("/api/ledger", joined)

    def test_redacted_in_access(self):
        self._access("/api/x", "?u=rtsp://admin:secret123@192.0.2.9/ch1")
        joined = "\n".join(nvr_slim._ACCESS)
        self.assertNotIn("secret123", joined)

    def test_export_has_both_sections(self):
        nvr_slim.log_line("业务一行")
        self._access("/api/record/file", "?cam=c1")
        text = nvr_slim.log_export_text()
        self.assertIn("业务一行", text)
        self.assertIn("HTTP 访问", text)
        self.assertIn("累计 1 次", text)

    def test_log_clear_resets_access(self):
        self._access("/api/record/file")
        nvr_slim.log_clear()
        self.assertEqual(len(nvr_slim._ACCESS), 0)
        self.assertEqual(nvr_slim._ACCESS_TOTAL[0], 0)

    def test_concurrent_counts_not_lost(self):
        """每个请求一个线程，折叠计数是读-改-写，必须加锁才不丢数。"""
        import threading

        def work():
            for _ in range(200):
                self._access("/api/record/file", "?cam=c1")

        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(nvr_slim._ACCESS_TOTAL[0], 1600)
        total, lines = nvr_slim._access_snapshot()
        self.assertIn("累计 1600 次", "\n".join(lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
