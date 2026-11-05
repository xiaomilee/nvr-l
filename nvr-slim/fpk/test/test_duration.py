# -*- coding: utf-8 -*-
"""文件页加载慢的回归守卫（0.9.114）

真机反馈：进文件页要等很久。根因是 recordings_payload 给每个归档片段补时长时
一律起 ffprobe 子进程 —— 冷启动要 probe 几千片，单次几十毫秒，累计几分钟。

现在三道闸：
  ① 先读 mp4 的 mvhd（几十字节 seek+read），读不出才回落 ffprobe；
  ② 读到的时长按 (size, mtime_ns) 落盘到 var/durations.json，服务重启直接命中；
  ③ /api/recordings 结果带 3 秒短缓存，切页/切机位不再重复全量扫盘。

跑法：python test/test_duration.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))
import nvr_slim  # noqa: E402
import condense  # noqa: E402


def fake_mp4(path, dur, ts=1000, pad=2048, moov_at_tail=False):
    """只带 mvhd 的最小 mp4，与 test_clipsplit.fake_mp4 同形。"""
    payload = (b"\x00\x00\x00\x00" + b"\x00" * 8
               + int(ts).to_bytes(4, "big")
               + int(round(dur * ts)).to_bytes(4, "big"))
    mvhd = (len(payload) + 8).to_bytes(4, "big") + b"mvhd" + payload
    moov = (len(mvhd) + 8).to_bytes(4, "big") + b"moov" + mvhd
    ftyp = (16).to_bytes(4, "big") + b"ftyp" + b"isom" + b"\x00\x00\x02\x00"
    mdat = pad.to_bytes(4, "big") + b"mdat" + b"\x00" * (pad - 8)
    with open(path, "wb") as f:
        f.write(ftyp + (mdat + moov if moov_at_tail else moov + mdat))


class TestFastDuration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvr-dur-")
        nvr_slim.STATE["var_dir"] = self.tmp
        nvr_slim.STATE["ffprobe_bin"] = None      # 没 ffprobe 也必须能出结果
        nvr_slim._DUR_CACHE.clear()
        nvr_slim._DUR_LOADED = False
        nvr_slim._DUR_DIRTY = 0
        try:
            os.remove(nvr_slim._dur_disk_path())
        except OSError:
            pass

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mvhd_no_ffprobe(self):
        """ffprobe 不可用时也能拿到真实时长 —— 证明走的是 mvhd 而不是进程。"""
        p = os.path.join(self.tmp, "10-00-00.mp4.c01.mp4")
        fake_mp4(p, 28.5)
        self.assertAlmostEqual(nvr_slim.file_duration(p), 28.5, places=2)

    def test_moov_at_tail(self):
        """moov 落尾的封装也认（转封装兜底产物就是这个形态）。"""
        p = os.path.join(self.tmp, "tail.mp4")
        fake_mp4(p, 11.0, moov_at_tail=True)
        self.assertAlmostEqual(nvr_slim.file_duration(p), 11.0, places=2)

    def test_unknown_structure_falls_back_silently(self):
        """结构不认识就返回 None，不抛异常（调用方按「未知」处理）。"""
        p = os.path.join(self.tmp, "junk.mp4")
        with open(p, "wb") as f:
            f.write(b"\x00" * 64)
        self.assertIsNone(nvr_slim.file_duration(p))

    def test_disk_cache_roundtrip(self):
        """落盘后「重启」（清内存、保留 json）能直接读回，不再开文件。"""
        p = os.path.join(self.tmp, "a.mp4")
        fake_mp4(p, 30.0)
        self.assertAlmostEqual(nvr_slim.file_duration(p), 30.0, places=2)
        nvr_slim._dur_save(force=True)
        self.assertTrue(os.path.exists(nvr_slim._dur_disk_path()))

        nvr_slim._DUR_CACHE.clear()
        nvr_slim._DUR_LOADED = False
        os.remove(p)                              # 文件没了，但缓存键还在
        with open(nvr_slim._dur_disk_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(len(raw), 1)
        self.assertAlmostEqual(raw[os.path.abspath(p)][2], 30.0, places=2)

        # 文件已被删：stat 失败 → 返回 None，不会拿缓存硬撑
        self.assertIsNone(nvr_slim.file_duration(p))

    def test_cache_invalidates_on_rewrite(self):
        """同名文件被重裁（size/mtime 变了）必须重新读，不能返回旧时长。"""
        p = os.path.join(self.tmp, "b.mp4")
        fake_mp4(p, 30.0)
        self.assertAlmostEqual(nvr_slim.file_duration(p), 30.0, places=2)
        fake_mp4(p, 45.0, pad=4096)               # 体积也变了
        st = os.stat(p)
        os.utime(p, (st.st_atime + 5, st.st_mtime + 5))
        self.assertAlmostEqual(nvr_slim.file_duration(p), 45.0, places=2)

    def test_mp4_duration_public_alias(self):
        """公开名与旧私有名都能用（test_clipsplit 仍按旧名调用）。"""
        self.assertIs(condense.mp4_duration, condense._mp4_duration)


class TestRecordingsCache(unittest.TestCase):
    def test_cache_key_changes_with_record_dir(self):
        """缓存键含录像根：换盘/改建目录天然失效，不会端出上一块盘的数据。"""
        k1 = nvr_slim._rec_key()
        old = nvr_slim.record_dir
        try:
            nvr_slim.record_dir = lambda: "/tmp/other-root"
            k2 = nvr_slim._rec_key()
        finally:
            nvr_slim.record_dir = old
        self.assertNotEqual(k1[0], k2[0])

    def test_ttl_is_short(self):
        """录像一直在写，缓存不能长：超过 5 秒就会让新分片「看不见」。"""
        self.assertLessEqual(nvr_slim.REC_TTL, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
