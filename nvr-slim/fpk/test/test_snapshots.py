#!/usr/bin/env python3
"""快照 worker 行为测试。

守四条：
  - 抓到图：.part 写好后原子改名成 <id>.jpg，大小够才收
  - 废图拒收：<1KB 的东西（多半是半帧或错误页）不能上线
  - 失败清理：超时/失败后不残留 .part
  - 选择：只抓填了地址且启用的路；有子码流用子码流
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import snapshots  # noqa: E402


def fake_run_factory(written, size=4096, fail=False):
    """伪 subprocess.run：模拟 ffmpeg 在 .part 落一张图（或什么都不留）。"""
    CompletedProcess = snapshots.subprocess.CompletedProcess
    def fake_run(cmd, timeout=None, **kw):
        if fail:
            raise snapshots.subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        out = cmd[-1]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"\xff\xd8" + b"x" * (size - 2))
        written.append(out)
        return CompletedProcess(cmd, 0, b"", b"")
    return fake_run


class SnapshotWorkerTest(unittest.TestCase):
    def setUp(self):
        self.var = tempfile.mkdtemp(prefix="nvr-snap-")
        self.w = snapshots.SnapshotWorker(
            "ffmpeg", self.var,
            lambda: [{"id": "cam-01", "url": "rtsp://a", "sub_url": ""},
                     {"id": "cam-02", "url": "", "sub_url": ""},
                     {"id": "cam-03", "url": "rtsp://b", "enabled": False}])

    def test_grab_success_renames_atomically(self):
        written = []
        with mock.patch.object(snapshots.subprocess, "run",
                               fake_run_factory(written)):
            ok = self.w._grab("cam-01", "rtsp://a")
        self.assertTrue(ok)
        final = os.path.join(self.var, "snapshots", "cam-01.jpg")
        self.assertTrue(os.path.isfile(final))
        self.assertGreater(os.path.getsize(final), 1024)
        self.assertFalse(os.path.isfile(final + ".part"))   # 没有残留半成品
        self.assertTrue(self.w._ok["cam-01"])               # 状态记录为成功

    def test_grab_rejects_tiny_frame(self):
        with mock.patch.object(snapshots.subprocess, "run",
                               fake_run_factory([], size=256)):
            ok = self.w._grab("cam-01", "rtsp://a")
        self.assertFalse(ok)
        final = os.path.join(self.var, "snapshots", "cam-01.jpg")
        self.assertFalse(os.path.isfile(final))
        self.assertFalse(os.path.isfile(final + ".part"))   # 废图连同 .part 一起清掉

    def test_grab_timeout_cleans_part(self):
        with mock.patch.object(snapshots.subprocess, "run",
                               fake_run_factory([], fail=True)):
            ok = self.w._grab("cam-01", "rtsp://a")
        self.assertFalse(ok)
        self.assertFalse(os.path.isfile(
            os.path.join(self.var, "snapshots", "cam-01.jpg.part")))

    def test_round_targets(self):
        """没地址 / 停用的路不进抓取队列；有子码流优先子码流。"""
        seen = []
        def fake_grab(cid, url, redact=None):
            seen.append((cid, url))
            return True
        with mock.patch.object(self.w, "_grab", fake_grab):
            self.w._round()
        ids = dict(seen)
        self.assertNotIn("cam-02", ids)              # 没地址
        self.assertNotIn("cam-03", ids)              # 已停用
        self.assertEqual(ids["cam-01"], "rtsp://a")  # 子码流为空用主码流

    def test_substream_preferred(self):
        w = snapshots.SnapshotWorker(
            "ffmpeg", self.var,
            lambda: [{"id": "cam-01", "url": "rtsp://main", "sub_url": "rtsp://sub"}])
        seen = []
        with mock.patch.object(w, "_grab", lambda cid, url, redact=None: seen.append(url) or True):
            w._round()
        self.assertEqual(seen, ["rtsp://sub"])

    def test_grab_keeps_ffmpeg_error(self):
        """抓帧失败时，把 ffmpeg 的最后一根错误行记进日志，方便真机排错。"""
        CompletedProcess = snapshots.subprocess.CompletedProcess
        def fake_run(cmd, timeout=None, **kw):
            return CompletedProcess(cmd, 1, b"", b"some noise\n401 Unauthorized\n")
        logs = []
        self.w.log = logs.append
        with mock.patch.object(snapshots.subprocess, "run", fake_run):
            ok = self.w._grab("cam-01", "rtsp://a")
        self.assertFalse(ok)
        self.assertTrue(any("401 Unauthorized" in m for m in logs), logs)

    def test_substream_falls_back_to_main(self):
        """子码流抓不到时自动回退主码流——子码流填错/不通时预览仍能出来。"""
        w = snapshots.SnapshotWorker(
            "ffmpeg", self.var,
            lambda: [{"id": "cam-01", "url": "rtsp://main", "sub_url": "rtsp://sub"}])
        seen = []
        def fake_grab(cid, url, redact=None):
            seen.append(url)
            return url == "rtsp://main"          # 子码流失败，主码流成功
        with mock.patch.object(w, "_grab", fake_grab):
            w._round()
        self.assertEqual(seen, ["rtsp://sub", "rtsp://main"])

    def test_udp_probed_once_then_sticks_to_tcp(self):
        """UDP 只探测一次：失败一次后固定回 TCP，不对 broadcast 流周期性误伤。"""
        CompletedProcess = snapshots.subprocess.CompletedProcess
        calls = []
        def fake_run(cmd, timeout=None, **kw):
            calls.append("tcp" if "-rtsp_transport" in cmd else "udp")
            return CompletedProcess(cmd, 1, b"", b"fail\n")   # 全部失败
        with mock.patch.object(snapshots.subprocess, "run", fake_run):
            for _ in range(8):
                self.w._grab("cam-01", "rtsp://a")
        self.assertEqual(calls.count("udp"), 1, calls)   # UDP 只探测了一次
        self.assertEqual(calls[-1], "tcp", calls)        # 之后固定回 TCP

    def test_grab_forces_image_format(self):
        """输出到 .part 时显式指定 image2/mjpeg：不靠扩展名推断，
        否则 ffmpeg 报「Error opening output files: Invalid argument」。"""
        CompletedProcess = snapshots.subprocess.CompletedProcess
        cmds = []
        def fake_run(cmd, timeout=None, **kw):
            cmds.append(cmd)
            out = cmd[-1]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(b"\xff\xd8" + b"x" * 4096)
            return CompletedProcess(cmd, 0, b"", b"")
        with mock.patch.object(snapshots.subprocess, "run", fake_run):
            self.w._grab("cam-01", "rtsp://a")
        cmd = cmds[0]
        self.assertIn("image2", cmd)                 # 显式 image2 muxer
        self.assertIn("mjpeg", cmd)                  # 显式 mjpeg 编码
        self.assertTrue(cmd[-1].endswith(".part"))   # 仍写 .part（原子改名）

    def test_grab_applies_redact_drawbox(self):
        """抓帧不再带任何滤镜。

        遮蔽区（redact）连同 drawbox 一起下线了：每秒过一遍滤镜链去涂黑块，
        代价远大于收益，而且录像那边为了它还得重编码。忽略区只影响缩时判定，
        不碰画面内容。
        """
        CompletedProcess = snapshots.subprocess.CompletedProcess
        cmds = []
        def fake_run(cmd, timeout=None, **kw):
            cmds.append(cmd)
            out = cmd[-1]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(b"\xff\xd8" + b"x" * 4096)
            return CompletedProcess(cmd, 0, b"", b"")
        with mock.patch.object(snapshots.subprocess, "run", fake_run):
            self.w._grab("cam-01", "rtsp://a")
        seen = " ".join(cmds[0])
        self.assertNotIn("-vf", cmds[0])
        self.assertNotIn("drawbox", seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
