# -*- coding: utf-8 -*-
"""归档「越压越肿」的回归守卫（0.9.115）

真机账（11-05，5 路全量）：
  客厅   源 1373 kbps → 归档 1759（1.28 倍），一天 9.6GB，比当天全时只省 36%
  院子 L 源  578 kbps → 归档 1628（2.82 倍）
  院子 R 源  960 kbps → 归档 1840（1.92 倍）

根因：归档重编码只有 CRF 26、没有码率上限。CRF 只管画质不管体积，低码率 +
高噪声的监控源二次编码后比特率反而更高。客厅活动覆盖 15.8 小时/天（真实有
人，不是误检），覆盖时长砍不掉，能砍的只有码率。

修法：按「这一路源分片自身的码率」打 75 折算上限（floor 500 / ceil 3000），
等于从结构上保证归档比特率低于源 —— 归档不可能比全时更占地方。

跑法：python test/test_arch_rate.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))
import archive        # noqa: E402
import condense       # noqa: E402
import ffmpeg_cmd     # noqa: E402


def make_src(path, size_bytes):
    with open(path, "wb") as f:
        f.write(b"\x00" * int(size_bytes))
    return path


class TestArchMaxrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-rate-")

    def _rate(self, size_mb, seg_sec):
        p = os.path.join(self.tmp, "s-%s-%s.mp4" % (size_mb, seg_sec))
        if not os.path.isfile(p):
            make_src(p, size_mb * 1024 * 1024)
        return archive.src_maxrate_kbps(p, seg_sec)

    def test_living_room_case(self):
        # 客厅真机：300 秒一段 51.5MB ≈ 1441 kbps → 75 折 ≈ 1080 kbps。
        # 修前归档实测 1759 kbps，所以这条断言就是「别再比源还大」的底线。
        r = self._rate(51.5, 300)
        self.assertTrue(1000 <= r <= 1120, r)
        self.assertLess(r, 1441)             # 必然低于源码率

    def test_floor_applies_to_low_bitrate_source(self):
        # 院子 L：300 秒 20.7MB ≈ 578 kbps，75 折只有 434 → 抬到 500 地板
        self.assertEqual(self._rate(20.7, 300), archive.ARCH_MAXRATE_FLOOR)

    def test_ceil_applies_to_high_bitrate_source(self):
        # 4MP 高码率源（300 秒 150MB ≈ 4194 kbps）别一刀切得太狠
        self.assertEqual(self._rate(150, 300), archive.ARCH_MAXRATE_CEIL)

    def test_missing_file_returns_none(self):
        self.assertIsNone(archive.src_maxrate_kbps(
            os.path.join(self.tmp, "nope.mp4"), 300))

    def test_zero_segment_returns_none(self):
        p = make_src(os.path.join(self.tmp, "z.mp4"), 1024 * 1024)
        self.assertIsNone(archive.src_maxrate_kbps(p, 0))
        self.assertIsNone(archive.src_maxrate_kbps(p, None))
        self.assertIsNone(archive.src_maxrate_kbps(p, "abc"))

    def test_empty_file_returns_none(self):
        p = make_src(os.path.join(self.tmp, "e.mp4"), 0)
        self.assertIsNone(archive.src_maxrate_kbps(p, 300))


class TestPreciseCmdVbv(unittest.TestCase):
    def cmd(self, **kw):
        return " ".join(ffmpeg_cmd.precise_cmd(
            "ffmpeg", "in.mp4", "o.mp4", 10, 40, hw="none", **kw))

    def test_vbv_absent_by_default(self):
        # 不传就不加：缩时/活动切片走的是同一函数，别给它们凭空加约束
        self.assertNotIn("-maxrate", self.cmd())
        self.assertNotIn("-bufsize", self.cmd())

    def test_vbv_values(self):
        s = self.cmd(maxrate_kbps=1080)
        self.assertIn("-maxrate 1080k", s)
        self.assertIn("-bufsize 2160k", s)   # 2 秒缓冲

    def test_vbv_after_encoder_before_vsync(self):
        # VBV 是编码选项：位置错了 ffmpeg 会报 Option not found。
        # 必须排在 -crf 之后、-vsync 之前。
        s = self.cmd(maxrate_kbps=900)
        self.assertLess(s.index("-crf"), s.index("-maxrate"))
        self.assertLess(s.index("-maxrate"), s.index("-vsync"))

    def test_bad_value_ignored(self):
        # 上层传了怪值不该让整片裁不出来
        self.assertNotIn("-maxrate", self.cmd(maxrate_kbps="x"))
        self.assertNotIn("-maxrate", self.cmd(maxrate_kbps=0))
        self.assertNotIn("-maxrate", self.cmd(maxrate_kbps=None))


class TestWriteSplitRecordsRate(unittest.TestCase):
    def test_maxrate_written_into_idx(self):
        # 事后排查「归档怎么又变大了」时，idx 里能直接看到这批按哪个上限裁的
        tmp = tempfile.mkdtemp(prefix="nvr-split-")
        src = make_src(os.path.join(tmp, "00-00-00.mp4"), 1024 * 1024)
        real = condense._export_precise

        def fake(ffmpeg, src_, out, ss, to, hw, speed=1.0, timeout=900,
                 maxrate_kbps=None):
            with open(out, "wb") as f:
                f.write(b"\x00" * 4096)
            return True, "", "x264"

        condense._export_precise = fake
        try:
            r = condense.write_split("ffmpeg", src, os.path.join(tmp, "00-00-00"),
                                     [(10, 40)], tmp, maxrate_kbps=1200)
        finally:
            condense._export_precise = real
        self.assertTrue(r["ok"], r)
        with open(os.path.join(tmp, "00-00-00.idx.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta.get("maxrate"), 1200)

    def test_no_maxrate_no_field(self):
        tmp = tempfile.mkdtemp(prefix="nvr-split-")
        src = make_src(os.path.join(tmp, "01-00-00.mp4"), 1024 * 1024)
        real = condense._export_precise

        def fake(ffmpeg, src_, out, ss, to, hw, speed=1.0, timeout=900,
                 maxrate_kbps=None):
            with open(out, "wb") as f:
                f.write(b"\x00" * 4096)
            return True, "", "x264"

        condense._export_precise = fake
        try:
            condense.write_split("ffmpeg", src, os.path.join(tmp, "01-00-00"),
                                 [(10, 40)], tmp)
        finally:
            condense._export_precise = real
        with open(os.path.join(tmp, "01-00-00.idx.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertNotIn("maxrate", meta)

    def test_bad_maxrate_does_not_break_split(self):
        # 上游传了非法值（None 之外的怪东西）：宁可这一片照常裁出来、只是
        # 不带上限，也不能让整天的 .done 卡在这里。
        tmp = tempfile.mkdtemp(prefix="nvr-split-")
        src = make_src(os.path.join(tmp, "02-00-00.mp4"), 1024 * 1024)
        real = condense._export_precise

        def fake(ffmpeg, src_, out, ss, to, hw, speed=1.0, timeout=900,
                 maxrate_kbps=None):
            with open(out, "wb") as f:
                f.write(b"\x00" * 4096)
            return True, "", "x264"

        condense._export_precise = fake
        try:
            r = condense.write_split("ffmpeg", src, os.path.join(tmp, "02-00-00"),
                                     [(10, 40)], tmp, maxrate_kbps="abc")
        finally:
            condense._export_precise = real
        self.assertTrue(r["ok"], r)
        with open(os.path.join(tmp, "02-00-00.idx.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertNotIn("maxrate", meta)


if __name__ == "__main__":
    unittest.main(verbosity=1)
