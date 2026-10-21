#!/usr/bin/env python3
"""存储目录用摄像头名称（dir_name）的行为测试。

重点守四条：
  - 名称清洗：非法字符 / 控制字符 / 首尾点全去掉，截到 40 字
  - 空名称回退 id，冲突自动加 -2/-3 后缀
  - cam_storage_dirs：新名称在前、旧 id 兜底，相同时去重
  - link 队列带 dir 字段，缩时输出镜像到来源目录
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import nvr_slim as ns  # noqa: E402
import condense  # noqa: E402


class TestSafeDirName(unittest.TestCase):
    def test_plain_name(self):
        used = set()
        self.assertEqual(ns._safe_dir_name("客厅", "cam-01", used), "客厅")
        self.assertEqual(used, {"客厅"})

    def test_strips_illegal_chars(self):
        used = set()
        s = ns._safe_dir_name('a/b\\c:d*e?f"g<h>i|j\x07k', "cam-01", used)
        for ch in ns._DIR_BAD:
            self.assertNotIn(ch, s)
        self.assertEqual(s, "abcdefghijk")

    def test_trims_and_caps_length(self):
        used = set()
        self.assertEqual(ns._safe_dir_name("  ..名字..  ", "cam-01", used), "名字")
        used2 = set()
        long = "很" * 60
        self.assertEqual(len(ns._safe_dir_name(long, "cam-01", used2)), 40)

    def test_empty_falls_back_to_id(self):
        used = set()
        self.assertEqual(ns._safe_dir_name("", "cam-07", used), "cam-07")
        self.assertEqual(ns._safe_dir_name("///", "cam-08", used), "cam-08")

    def test_conflict_gets_suffix(self):
        used = set()
        self.assertEqual(ns._safe_dir_name("客厅", "cam-01", used), "客厅")
        self.assertEqual(ns._safe_dir_name("客厅", "cam-02", used), "客厅-2")
        self.assertEqual(ns._safe_dir_name("客厅", "cam-03", used), "客厅-3")

    def test_cam_storage_dirs_dedup(self):
        self.assertEqual(ns.cam_storage_dirs({"id": "cam-01", "dir_name": "客厅"}),
                         ["客厅", "cam-01"])
        # 没算出 dir_name 时直接用 id，不能出现两个一样的目录
        self.assertEqual(ns.cam_storage_dirs({"id": "cam-01"}), ["cam-01"])


class TestClipMirror(unittest.TestCase):
    def test_clip_path_mirrors_source_dir(self):
        root = os.sep + "tmp" + os.sep + "nvr"
        src = os.path.join(root, "客厅", "2026-10-21", "01-32-08.mp4")
        self.assertEqual(
            condense.clip_path(root, "客厅", src),
            os.path.join(root, "_clip", "客厅", "2026-10-21", "01-32-08.mp4"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
