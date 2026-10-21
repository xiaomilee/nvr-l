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
from unittest import mock

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

    def test_cmd_has_no_timeout_flags(self):
        """录制命令里不许出现任何 ffmpeg 超时参数。

        0.9.11 加了 -rw_timeout：这台 NAS 的精简 ffmpeg 直接报
        "Option not found"，五路录制全挂。0.9.12 去掉后 0.9.13 又留下一个
        -timeout，同样会让 ffmpeg 一起手就退出 —— 表现正是「快照能看、
        录像一条都没有」。读超时一律交给 recorder.tick() 的软件看门狗。
        """
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/tmp/%Y.mp4", 300)
        joined = " ".join(cmd)
        self.assertNotIn("-timeout", joined)
        self.assertNotIn("-rw_timeout", joined)
        self.assertNotIn("-stimeout", joined)

    def test_watchdog_covers_never_wrote(self):
        """连接建起来但一个字节都没落盘，也必须被判为卡住并重启。

        以前 _newest_age() 返回 None 时直接放行，等于把"假活进程"永远留在
        进程表里；去掉 -timeout 之后这就是唯一的兜底，必须断言它存在。
        """
        self.assertEqual(recorder.STALL_SEC, 75)
        with open(recorder.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("age is None or age > STALL_SEC", src)

    def test_cmd_no_redact_stays_copy(self):
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/tmp/%Y.mp4", 300)
        self.assertIn("-c", cmd)
        self.assertNotIn("-vf", cmd)          # 无遮蔽区不加滤镜，保持流复制零成本

    def test_cmd_never_redacts(self):
        """「输出遮蔽区」已整体移除。

        原设计要给画面打黑块，就必须放弃 `-c copy` 改 libx264 重编码 ——
        等于为了一个遮蔽效果把所有录像的 CPU 翻倍，属于设计错误。
        现在 record_cmd 连 redact 参数都不收，保证永远流复制。
        """
        with self.assertRaises(TypeError):
            recorder.record_cmd("ffmpeg", "rtsp://x", "/tmp/%Y.mp4", 300,
                                redact=[3, 20])
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/tmp/%Y.mp4", 300)
        joined = " ".join(cmd)
        self.assertNotIn("drawbox", joined)
        self.assertNotIn("libx264", joined)
        self.assertIn("copy", joined)

    def test_seg_of_camera_override(self):
        """单路分片长度必须真的进 ffmpeg 命令行 —— 它是物理参数。"""
        root = tempfile.mkdtemp(prefix="nvrrec")
        rec = recorder.Recorder("ffmpeg", root, [], segment_sec=300)
        self.assertEqual(rec.seg_of({"id": "a"}), 300)                 # 没填 → 跟随全局
        self.assertEqual(rec.seg_of({"id": "a", "segment_sec": 60}), 60)
        self.assertEqual(rec.seg_of({"id": "a", "segment_sec": 0}), 300)  # 0 = 跟随

    def test_seg_of_in_command(self):
        """100 秒分片要出现在 -segment_time 里，不是仍按全局 300。"""
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/tmp/%Y.mp4", 100)
        i = cmd.index("-segment_time")
        self.assertEqual(cmd[i + 1], "100")

    def test_sig_includes_segment(self):
        """分片长度变了，签名必须跟着变，否则 reconcile 不会重启进程。"""
        rec = recorder.Recorder("ffmpeg", tempfile.mkdtemp(), [])
        a = rec._sig({"id": "c", "url": "rtsp://x", "dir_name": "d", "segment_sec": 300})
        b = rec._sig({"id": "c", "url": "rtsp://x", "dir_name": "d", "segment_sec": 60})
        self.assertNotEqual(a, b)

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


class IgnoreRegionTest(unittest.TestCase):
    """检测忽略区：必须真的让判定"看不见"那块画面。

    原来的实现只是给预览画面涂黑块，判定逻辑压根没读这个配置 ——
    设置页里画的框，纯粹是动画。
    """

    def test_region_filter_empty(self):
        self.assertEqual(condense.region_filter([]), "")
        self.assertEqual(condense.region_filter(None), "")

    def test_region_filter_out_of_range_ignored(self):
        # 越界格号直接丢弃，不能生成 crop 参数为负的滤镜
        self.assertEqual(condense.region_filter([999, -3]), "")

    def test_region_filter_single_cell(self):
        vf = condense.region_filter([17])          # row=1, col=1
        self.assertIn("crop=", vf)
        self.assertIn("x=iw*1/16", vf)
        self.assertIn("y=ih*1/9", vf)

    def test_region_filter_merges_contiguous(self):
        # 一整行 16 格应合并成 1 个矩形，而不是 16 路 ffprobe
        row0 = list(range(16))
        vf = condense.region_filter(row0)
        self.assertEqual(vf.count("crop="), 1)

    def test_region_filter_keeps_split_when_gap(self):
        vf = condense.region_filter([0, 5])
        self.assertEqual(vf.count("crop="), 2)

    def test_subtract_removes_region_activity(self):
        """区域里的活动被减掉后，整段应判为静默。"""
        full = [(0.0, 1000.0), (1.0, 90000.0), (2.0, 1000.0)]
        sub  = [(0.0, 0.0), (1.0, 88000.0), (2.0, 0.0)]
        calls = {"n": 0}

        def fake_read(ffprobe, src, timeout=180, vf=None):
            if vf:
                return sub, ""
            return full, ""

        with mock.patch.object(condense, "read_packets", fake_read):
            curve, regions = condense._subtract_regions("fp", "s", full, "crop=a", 10)
        self.assertEqual(regions, 1)
        self.assertEqual([b for _, b in curve], [1000.0, 2000.0, 1000.0])

    def test_subtract_falls_back_on_read_error(self):
        """区域读不出来 → 保持原曲线（宁可多报事件，不能漏真事件）。"""
        def fake_read(ffprobe, src, timeout=180, vf=None):
            if vf:
                return [], "boom"
            return [(0.0, 5000.0)], ""

        with mock.patch.object(condense, "read_packets", fake_read):
            curve, regions = condense._subtract_regions("fp", "s", [(0.0, 5000.0)], "crop=a", 10)
        self.assertEqual(regions, 0)
        self.assertEqual(curve, [(0.0, 5000.0)])

    def test_subtract_clamps_at_zero(self):
        """重叠区域会让减数偏大，逐点夹 0，不能出现负字节。"""
        full = [(0.0, 100.0)]
        sub = [(0.0, 500.0)]
        with mock.patch.object(condense, "read_packets",
                               lambda *a, **k: (sub, "") if k.get("vf") else (full, "")):
            curve, _ = condense._subtract_regions("fp", "s", full, "crop=a", 10)
        self.assertEqual(curve, [(0.0, 0.0)])


class LiveStreamCmdTest(unittest.TestCase):
    """实时预览流命令：错一个参数就会表现成「画面永远停在等待第一帧」。"""

    def test_has_an_and_mpjpeg(self):
        cmd = ffmpeg_cmd.live_stream_cmd("ffmpeg", "rtsp://x", fps=8, width=960)
        joined = " ".join(cmd)
        # 不带 -an 时，子码流的音轨会让 mpjpeg 写头失败 → 浏览器拿到空响应
        self.assertIn("-an", cmd)
        self.assertIn("mpjpeg", cmd)
        self.assertIn("nvrframe", cmd)
        self.assertIn("-fflags nobuffer", joined)

    def test_grid_params_lower_than_single(self):
        grid = " ".join(ffmpeg_cmd.live_stream_cmd("ffmpeg", "rtsp://x", fps=4, width=480))
        single = " ".join(ffmpeg_cmd.live_stream_cmd("ffmpeg", "rtsp://x", fps=8, width=960))
        self.assertIn("fps=4,scale=480:-2", grid)
        self.assertIn("fps=8,scale=960:-2", single)

    def test_width_zero_keeps_native(self):
        cmd = ffmpeg_cmd.live_stream_cmd("ffmpeg", "rtsp://x", fps=6, width=0)
        joined = " ".join(cmd)
        self.assertIn("fps=6", joined)
        self.assertNotIn("scale=", joined)

    def test_odd_width_rounded_even(self):
        # 奇数宽会让编码器报 "width not divisible by 2"
        self.assertIn("scale=480:-2",
                      " ".join(ffmpeg_cmd.live_stream_cmd("ffmpeg", "rtsp://x", 8, 479)))

    def test_params_clamped(self):
        low = " ".join(ffmpeg_cmd.live_stream_cmd("ffmpeg", "rtsp://x", fps=0, width=0))
        high = " ".join(ffmpeg_cmd.live_stream_cmd("ffmpeg", "rtsp://x", fps=99, width=9999))
        self.assertIn("fps=1", low)
        self.assertIn("fps=15", high)
        self.assertIn("scale=1920:-2", high)


if __name__ == "__main__":
    unittest.main()
