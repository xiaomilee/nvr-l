#!/usr/bin/env python3
"""0.9.100 回归修复后的守卫测试（0.9.99 引入 _remux_to_faststart，0.9.100 修复其误置于类体内导致 Handler 方法被吞的回归）。

为什么要测这条链：录制分片是碎片化 fMP4（empty_moov），iOS Safari / 微信 WebView
的 <video> 渐进式播放器解析不了、必黑屏。修复是在播放请求时把分片按需重封装成
moov 前置的 faststart 进度式 MP4（见 nvr_slim._remux_to_faststart）。这条链一旦
ffmpeg 命令拼错或 faststart 没生效，桌面 Chrome 还能勉强播、只有手机黑屏 —— 最难的
一类回归。所以这里把"输出确实是 moov 在 mdat 之前的可播文件"钉死。

无 ffmpeg 环境（如 CI 沙箱）自动跳过，不卡构建。
"""

import os
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import nvr_slim  # noqa: E402
import runtime  # noqa: E402


def _find_ffmpeg():
    ff = runtime.find_ffmpeg(os.path.join(ROOT, "app"))
    if ff and os.path.exists(ff):
        return ff
    import shutil
    return shutil.which("ffmpeg")


def _make_fmp4(ff, path):
    """生成与录制同款的碎片化 fMP4（empty_moov）。"""
    cmd = [ff, "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
           "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-movflags", "+frag_keyframe+empty_moov+default_base_moof", path]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      timeout=120)
    return r.returncode == 0 and os.path.getsize(path) > 0


def _box_index(data, typ):
    i = 0
    while i + 8 <= len(data):
        sz = struct.unpack(">I", data[i:i + 4])[0]
        t = data[i + 4:i + 8]
        if t == typ:
            return i
        if sz == 0:
            break
        i += sz
    return -1


class RemuxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ff = _find_ffmpeg()
        cls.tmp = tempfile.mkdtemp(prefix="remux_test_")
        cls.src = os.path.join(cls.tmp, "frag.mp4")
        if cls.ff and _make_fmp4(cls.ff, cls.src):
            cls.has_fmp4 = True
        else:
            cls.has_fmp4 = False

    @classmethod
    def tearDownClass(cls):
        import shutil as _sh
        try:
            _sh.rmtree(cls.tmp, ignore_errors=True)
        except Exception:
            pass

    def test_no_ffmpeg_returns_none(self):
        # ffmpeg 不可用（或显式 None）时必须退化返回 None，绝不抛异常
        self.assertIsNone(nvr_slim._remux_to_faststart(self.src, None, self.tmp))
        if self.ff:
            self.assertIsNone(nvr_slim._remux_to_faststart(
                self.src, "/no/such/ffmpeg", self.tmp))

    def test_remux_produces_faststart_mp4(self):
        if not (self.ff and self.has_fmp4):
            self.skipTest("no ffmpeg / fMP4 generation unavailable")
        cache = os.path.join(self.tmp, "cache")
        out = nvr_slim._remux_to_faststart(self.src, self.ff, cache)
        self.assertIsNotNone(out, "remux 应返回输出路径")
        self.assertTrue(os.path.isfile(out) and os.path.getsize(out) > 0)
        with open(out, "rb") as fh:
            data = fh.read()
        moov = _box_index(data, b"moov")
        mdat = _box_index(data, b"mdat")
        self.assertGreater(moov, 0, "输出里应含 moov")
        self.assertGreater(mdat, 0, "输出里应含 mdat")
        self.assertLess(moov, mdat, "moov 必须位于 mdat 之前（faststart）")

    def test_remux_is_cached(self):
        if not (self.ff and self.has_fmp4):
            self.skipTest("no ffmpeg / fMP4 generation unavailable")
        cache = os.path.join(self.tmp, "cache2")
        a = nvr_slim._remux_to_faststart(self.src, self.ff, cache)
        b = nvr_slim._remux_to_faststart(self.src, self.ff, cache)
        self.assertEqual(a, b, "相同源二次调用应命中缓存、路径一致")


if __name__ == "__main__":
    unittest.main()
