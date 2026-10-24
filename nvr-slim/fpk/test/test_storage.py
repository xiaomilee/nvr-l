#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存储占用统计（0.9.24）的行为守卫。

这个接口的设计目标是「别把盘扫爆」，所以除了数字对不对，更要守住成本：
  S1 口径：只按「摄像头/日期/」两级目录聚合文件名与大小，不读文件内容
  S2 去重：旧 id 目录与新名称目录可能同时存在，按 (日期, 文件名) 去重，
     否则升级后同一批文件会被算两遍、占用凭空翻倍
  S3 缓存：30 分钟 TTL，命中时不再碰磁盘；force=True 才重算
  S4 结构：前端拿到的字段（used/full/clip/disk_free_gb/cameras[].total）
     一个不能少，前端按 total 归满格画每路占比
  S5 失败可诊断：缓存写不进去只记日志，不能让统计接口整个失败

跑：python test/test_storage.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import nvr_slim  # noqa: E402


def mkfile(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


class StorageCase(unittest.TestCase):
    """一套临时根目录 + 两路摄像头，改 nvr_slim 的全局钩子而不是真的读配置。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvrst-")
        self.root = os.path.join(self.tmp, "out")
        self.var = os.path.join(self.tmp, "var")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.var, exist_ok=True)

        self._saved = {
            "STATE": dict(nvr_slim.STATE),
            "cameras": nvr_slim.cameras,
            "record_dir": nvr_slim.record_dir,
            "cams": getattr(nvr_slim, "cameras", None),
        }
        nvr_slim.STATE["var_dir"] = self.var
        nvr_slim.cameras = lambda: [
            {"id": "cam-01", "name": "车库入口", "dir_name": "车库入口"},
            {"id": "cam-02", "name": "后院", "dir_name": "后院"},
        ]
        nvr_slim.record_dir = lambda: self.root

    def tearDown(self):
        nvr_slim.STATE.clear()
        nvr_slim.STATE.update(self._saved["STATE"])
        nvr_slim.cameras = self._saved["cameras"]
        nvr_slim.record_dir = self._saved["record_dir"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 建数据的两个小工具 ----
    def full(self, dname, day, name, size):
        mkfile(os.path.join(self.root, dname, day, name), size)

    def clip(self, dname, day, name, size):
        mkfile(os.path.join(self.root, "_clip", dname, day, name), size)


class TestS1S2Aggregate(StorageCase):
    def test_s1_only_day_dirs(self):
        """只认「YYYY-MM-DD」目录下的 .mp4；别的东西一概不计。"""
        self.full("车库入口", "2026-10-21", "08-00-00.mp4", 100)
        self.full("车库入口", "不是日期", "x.mp4", 999)
        self.full("车库入口", "2026-10-21", "note.txt", 999)
        self.full("车库入口", "2026-10-21", "段.part", 999)
        d = nvr_slim.storage_usage(force=True)
        cam = [c for c in d["cameras"] if c["id"] == "cam-01"][0]
        self.assertEqual(cam["full"], 100)
        self.assertEqual(cam["clip"], 0)

    def test_s1_full_and_clip_separate(self):
        self.full("车库入口", "2026-10-21", "08-00-00.mp4", 100)
        self.full("车库入口", "2026-10-22", "09-00-00.mp4", 50)
        self.clip("车库入口", "2026-10-21", "08-05-00.mp4", 7)
        d = nvr_slim.storage_usage(force=True)
        cam = [c for c in d["cameras"] if c["id"] == "cam-01"][0]
        self.assertEqual((cam["full"], cam["clip"]), (150, 7))
        self.assertEqual(cam["total"], 157)
        # 总量 = 各路之和
        self.assertEqual(d["full"], 150)
        self.assertEqual(d["clip"], 7)
        self.assertEqual(d["used"], 157)

    def test_s2_dedup_old_and_new_dir(self):
        """旧 id 目录与新名称目录同时存在：同一批文件只算一次。

        升级前数据在 cam-01/，升级后写到「车库入口/」。如果按目录简单相加，
        用户会看到占用翻倍，以为盘要满了。
        """
        self.full("cam-01", "2026-10-21", "08-00-00.mp4", 100)
        self.full("车库入口", "2026-10-21", "08-00-00.mp4", 100)
        self.full("车库入口", "2026-10-22", "09-00-00.mp4", 40)
        cam = [c for c in nvr_slim.storage_usage(force=True)["cameras"]
               if c["id"] == "cam-01"][0]
        self.assertEqual(cam["full"], 140)

    def test_s2_dedup_clip(self):
        self.clip("cam-01", "2026-10-21", "a.mp4", 10)
        self.clip("车库入口", "2026-10-21", "a.mp4", 10)
        cam = [c for c in nvr_slim.storage_usage(force=True)["cameras"]
               if c["id"] == "cam-01"][0]
        self.assertEqual(cam["clip"], 10)

    def test_每路_各自统计(self):
        self.full("车库入口", "2026-10-21", "a.mp4", 10)
        self.full("后院", "2026-10-21", "b.mp4", 90)
        cams = {c["id"]: c for c in nvr_slim.storage_usage(force=True)["cameras"]}
        self.assertEqual(cams["cam-01"]["total"], 10)
        self.assertEqual(cams["cam-02"]["total"], 90)


class TestS3Cache(StorageCase):
    def test_命中缓存不再扫盘(self):
        self.full("车库入口", "2026-10-21", "a.mp4", 100)
        first = nvr_slim.storage_usage()
        self.assertFalse(first["cached"])
        # 再放一个文件：TTL 内不该被算进来
        self.full("车库入口", "2026-10-21", "b.mp4", 500)
        second = nvr_slim.storage_usage()
        self.assertTrue(second["cached"])
        self.assertEqual(second["used"], first["used"])

    def test_force_重算(self):
        self.full("车库入口", "2026-10-21", "a.mp4", 100)
        nvr_slim.storage_usage()
        self.full("车库入口", "2026-10-21", "b.mp4", 500)
        third = nvr_slim.storage_usage(force=True)
        self.assertFalse(third["cached"])
        self.assertEqual(third["used"], 600)

    def test_过期后自动重算(self):
        self.full("车库入口", "2026-10-21", "a.mp4", 100)
        nvr_slim.storage_usage()
        p = nvr_slim.storage_cache_path()
        with open(p, "r", encoding="utf-8") as f:
            cache = json.load(f)
        cache["at"] = time.time() - nvr_slim.STORAGE_TTL - 5
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        self.full("车库入口", "2026-10-21", "b.mp4", 500)
        d = nvr_slim.storage_usage()
        self.assertFalse(d["cached"])
        self.assertEqual(d["used"], 600)

    def test_缓存文件写坏不影响统计(self):
        self.full("车库入口", "2026-10-21", "a.mp4", 100)
        with open(nvr_slim.storage_cache_path(), "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        d = nvr_slim.storage_usage()
        self.assertTrue(d["ok"])
        self.assertEqual(d["used"], 100)

    def test_缓存目录不可写也不抛(self):
        self.full("车库入口", "2026-10-21", "a.mp4", 100)
        nvr_slim.STATE["var_dir"] = os.path.join(self.tmp, "不存在的盘", "var")
        d = nvr_slim.storage_usage()
        self.assertTrue(d["ok"])
        self.assertEqual(d["used"], 100)


class TestS4Payload(StorageCase):
    def test_字段齐全(self):
        self.full("车库入口", "2026-10-21", "a.mp4", 100)
        d = nvr_slim.storage_usage(force=True)
        for k in ("ok", "cached", "at", "ttl", "root", "used", "full", "clip",
                  "disk_free_gb", "disk_total_gb", "cameras"):
            self.assertIn(k, d)
        self.assertEqual(d["ok"], True)
        self.assertEqual(d["ttl"], int(nvr_slim.STORAGE_TTL))
        self.assertEqual(d["root"], self.root)
        self.assertIsInstance(d["at"], int)
        cam = d["cameras"][0]
        self.assertEqual(sorted(cam),
                         ["archive", "clip", "full", "id", "name", "total"])
        self.assertEqual(cam["total"], cam["full"] + cam["archive"] + cam["clip"])
        self.assertIn("ledger", d)   # 台账单列（KB 级，只为可见性）

    def test_空目录也给零而不是缺字段(self):
        d = nvr_slim.storage_usage(force=True)
        self.assertEqual(d["used"], 0)
        self.assertEqual(len(d["cameras"]), 2)
        self.assertTrue(all(c["total"] == 0 for c in d["cameras"]))

    def test_磁盘容量透传(self):
        d = nvr_slim.storage_usage(force=True)
        # 临时目录一定在一张真实存在的分区上；读不到时是 None，不能是字符串
        self.assertTrue(d["disk_free_gb"] is None or isinstance(d["disk_free_gb"], float))
        self.assertTrue(d["disk_total_gb"] is None or isinstance(d["disk_total_gb"], float))


class TestS5Endpoint(unittest.TestCase):
    """路由接线：GET 走缓存、POST 强制重算（前端「立即重算」按这个调）。"""

    def test_路由已接线(self):
        path = os.path.join(ROOT, "app", "server", "nvr_slim.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"/api/storage/usage"', src)
        self.assertIn('"/api/storage/usage/refresh"', src)
        self.assertIn("storage_usage(force=True)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
