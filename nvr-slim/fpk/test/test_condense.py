#!/usr/bin/env python3
"""缩时压制的两个老大难：探测解析 & 录制卡死判定。

真实事故：所有分片都报「读不到包信息」，因为探测用的是 `frame=` 段 ——
那条路要过解码器，解不出来就一行都不吐，stderr 又被丢掉，于是只剩一句
含糊话。现在换成容器层的 `packet=`，并强制带字段名输出，失败必带证据。
"""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import condense  # noqa: E402
import ffmpeg_cmd  # noqa: E402
import recorder  # noqa: E402


class ParseProbeTest(unittest.TestCase):
    def test_compact_with_keys(self):
        txt = "size=1200|pts_time=0.000000\nsize=340|pts_time=0.500000\n"
        self.assertEqual(condense.parse_probe_output(txt),
                         [(0.0, 1200), (0.5, 340)])

    def test_sorted_by_time(self):
        txt = "size=9|pts_time=2.0\nsize=8|pts_time=0.0\n"
        self.assertEqual([t for t, _ in condense.parse_probe_output(txt)], [0.0, 2.0])

    def test_frame_fallback_keys(self):
        txt = "pkt_size=777|pts_time=1.250000\n"
        self.assertEqual(condense.parse_probe_output(txt), [(1.25, 777)])

    def test_legacy_csv_any_order(self):
        # 老式 csv 没有字段名：位置靠不住，得靠"能不能转成数字"来认
        self.assertEqual(condense.parse_probe_output("1200,I,0.5\n"), [(0.5, 1200)])
        self.assertEqual(condense.parse_probe_output("0.5,1200\n"), [(0.5, 1200)])

    def test_skips_na_and_noise(self):
        txt = "N/A,N/A\n\nsize=N/A|pts_time=1.0\npts_time=2.0\n"
        self.assertEqual(condense.parse_probe_output(txt), [])

    def test_read_packets_reports_stderr(self):
        # 拿 python 自己当"ffprobe"：退出码非 0、stdout 空、stderr 有话说
        packets, err = condense.read_packets(sys.executable, "x.mp4", timeout=30)
        self.assertEqual(packets, [])
        self.assertTrue(err, "失败必须带回 ffprobe 真实报错，不能只回一个空列表")

    def test_analyze_error_carries_reason(self):
        res = condense.analyze(sys.executable, "x.mp4", {"ratio": 1.0}, timeout=30)
        self.assertTrue(res["error"].startswith("读不到包信息"))
        self.assertGreater(len(res["error"]), len("读不到包信息"))

    def test_prescan_is_packet_level(self):
        cmd = ffmpeg_cmd.prescan_cmd("ffprobe", "a.mp4")
        self.assertIn("packet=size,pts_time", " ".join(cmd))


class RecordWatchdogTest(unittest.TestCase):
    """进程活着但磁盘不动 —— 无线摄像头半死连接的典型状态。"""

    def test_cmd_has_read_timeout(self):
        # 0.9.12 起去掉 -rw_timeout：这台 NAS 的精简 ffmpeg 不认该参数，
        # 会让整路录制起不来（"Option not found"）。读超时改由软件看门狗兜底。
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/tmp/%Y.mp4", 300)
        self.assertIn("-timeout", cmd)
        self.assertNotIn("-rw_timeout", cmd)

    def test_cmd_no_redact_stays_copy(self):
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/tmp/%Y.mp4", 300)
        self.assertIn("-c", cmd)
        self.assertNotIn("-vf", cmd)          # 无遮蔽区不加滤镜，保持流复制零成本

    def test_cmd_redact_reencodes_with_drawbox(self):
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/tmp/%Y.mp4", 300,
                                  redact=[3, 20])
        self.assertIn("-vf", cmd)
        joined = " ".join(cmd)
        self.assertIn("drawbox", joined)
        self.assertIn("libx264", joined)      # 打黑块必须重编码

    def test_newest_age_none_when_empty(self):
        root = tempfile.mkdtemp(prefix="nvrrec")
        rec = recorder.Recorder("ffmpeg", root, [{"id": "cam-1", "name": "车库"}])
        self.assertIsNone(rec._newest_age({"id": "cam-1", "dir_name": "车库"}))

    def test_newest_age_reads_name_dir(self):
        root = tempfile.mkdtemp(prefix="nvrrec")
        day = time.strftime("%Y-%m-%d")
        d = os.path.join(root, "车库", day)
        os.makedirs(d)
        p = os.path.join(d, "10-00-00.mp4")
        with open(p, "wb") as f:
            f.write(b"0" * 16)
        rec = recorder.Recorder("ffmpeg", root, [])
        age = rec._newest_age({"id": "cam-1", "dir_name": "车库"})
        self.assertIsNotNone(age)
        self.assertLess(age, 5)

    def test_stall_threshold_sane(self):
        # 阈值必须显著短于最短分片，否则卡死半小时才被发现
        self.assertLess(recorder.STALL_SEC, 120)


if __name__ == "__main__":
    unittest.main()
