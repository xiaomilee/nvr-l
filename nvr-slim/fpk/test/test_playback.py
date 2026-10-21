#!/usr/bin/env python3
"""回放数据通道的行为测试：全时 + 缩时两套清单、新旧目录合并。

重点守三条：
  - recordings_payload 同时给出 cameras（全时）与 clips（_clip 缩时）
  - 中文名新目录与旧 id 目录都扫，同名文件去重
  - _scan_day_dirs 只认 YYYY-MM-DD 目录与 .mp4 文件
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import runtime  # noqa: E402
import nvr_slim as ns  # noqa: E402


class PayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvrplay")
        self.cfg = {"record_dir": os.path.join(self.tmp, "rec"),
                    "cameras": [{"id": "cam-01", "name": "客厅",
                                 "url": "rtsp://x", "sub_url": "", "enabled": True}]}
        self._orig = runtime.load_config
        runtime.load_config = lambda var: self.cfg
        root = self.cfg["record_dir"]
        for d, f in [(os.path.join(root, "客厅", "2026-10-21"), "03-00-00.mp4"),
                     (os.path.join(root, "cam-01", "2026-10-20"), "01-32-08.mp4"),
                     (os.path.join(root, "_clip", "客厅", "2026-10-21"), "03-05-00.mp4"),
                     (os.path.join(root, "_clip", "cam-01", "2026-10-20"), "01-40-00.mp4")]:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f), "wb") as fh:
                fh.write(b"x" * 100)
        # 干扰项：非日期目录、非 mp4 文件，都不能混进清单
        os.makedirs(os.path.join(root, "客厅", "lost+found"))
        with open(os.path.join(root, "客厅", "2026-10-21", "note.txt"), "wb") as fh:
            fh.write(b"no")

    def tearDown(self):
        runtime.load_config = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_payload_lists_full_and_clips(self):
        pl = ns.recordings_payload()
        self.assertTrue(pl["ok"])
        per = pl["cameras"]["cam-01"]
        self.assertIn("2026-10-21", per)       # 新名称目录
        self.assertIn("2026-10-20", per)       # 旧 id 目录
        clips = pl["clips"]["cam-01"]
        self.assertIn("2026-10-21", clips)     # 缩时也扫两个目录
        self.assertIn("2026-10-20", clips)
        self.assertEqual(pl["total"], 2)       # 全时两个文件

    def test_scan_ignores_non_day_and_non_mp4(self):
        days = ns._scan_day_dirs(os.path.join(self.cfg["record_dir"], "客厅"))
        self.assertEqual(list(days), ["2026-10-21"])
        self.assertEqual([f["n"] for f in days["2026-10-21"]], ["03-00-00.mp4"])

    def test_scan_missing_dir(self):
        self.assertEqual(ns._scan_day_dirs(os.path.join(self.tmp, "nope")), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
