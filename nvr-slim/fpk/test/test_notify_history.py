#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify_history.py 单测：落盘 / 脱敏 / 裁剪 / 坏行兼容 / 过滤查询（0.9.124）。

为什么用真临时目录而不是 mock open：这份模块的全部价值就是「重启之后还在」，
mock 掉文件等于把要验的那件事验没了。按项目惯例用 tempfile + addCleanup。

跑：python test/test_notify_history.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "server"))
import notify_history as hist  # noqa: E402


def rec(at, cam="院子", cat="person", ok=True, **kw):
    d = {"at": at, "cam": cam, "cat": cat, "ok": ok,
         "ms": 12, "err": "", "tries": 1, "platform": "feishu"}
    d.update(kw)
    return d


class HistTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="nvr-nhist-test-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def append(self, r):
        return hist.append(self.d, r)

    # ------------------------------------------------------------ 基本读写
    def test_no_file_means_empty(self):
        self.assertEqual(hist.query(self.d), [])
        self.assertEqual(hist.stats(self.d)["total"], 0)
        self.assertIsNone(hist.stats(self.d)["last_at"])

    def test_append_then_query(self):
        ok, n = self.append(rec(1000.0))
        self.assertTrue(ok)
        self.assertEqual(n, 1)
        rows = hist.query(self.d)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cam"], "院子")
        self.assertEqual(rows[0]["platform"], "feishu")

    def test_newest_first(self):
        for t in (1000.0, 1003.0, 1001.0):
            self.append(rec(t))
        rows = hist.query(self.d)
        self.assertEqual([r["at"] for r in rows], [1003.0, 1001.0, 1000.0])

    def test_stats(self):
        self.append(rec(1000.0))
        self.append(rec(2000.0, ok=False))
        st = hist.stats(self.d)
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["last_at"], 2000.0)
        self.assertEqual(st["file"], hist.FILE_NAME)
        self.assertGreater(st["bytes"], 0)

    def test_file_name_is_stable(self):
        self.append(rec(1.0))
        self.assertTrue(os.path.isfile(os.path.join(self.d, hist.FILE_NAME)))

    def test_var_dir_created_if_missing(self):
        nested = os.path.join(self.d, "a", "b")
        self.assertTrue(hist.append(nested, rec(1.0))[0])
        self.assertEqual(len(hist.query(nested)), 1)

    # ------------------------------------------------------------ 脱敏与不记 URL
    def test_secret_in_err_is_masked(self):
        self.append(rec(1.0, ok=False,
                        err="HTTP 500 on https://hook.example/x?key=SUPERSECRET"))
        raw = open(os.path.join(self.d, hist.FILE_NAME), encoding="utf-8").read()
        self.assertNotIn("SUPERSECRET", raw)
        self.assertIn("HTTP 500", raw)

    def test_bearer_in_err_is_masked(self):
        self.append(rec(1.0, err="failed: Bearer abcdefghijklmnop123456"))
        raw = open(os.path.join(self.d, hist.FILE_NAME), encoding="utf-8").read()
        self.assertNotIn("abcdefghijklmnop123456", raw)

    def test_credential_in_cam_is_masked(self):
        self.append(rec(1.0, cam="rtsp://admin:PASS123@192.168.2.9/Stream"))
        rows = hist.query(self.d)
        self.assertNotIn("PASS123", rows[0]["cam"])

    def test_no_url_field_in_record(self):
        self.append(rec(1.0, url="https://secret.example/x", tpl="{cam}"))
        rows = hist.query(self.d)
        self.assertNotIn("url", rows[0])
        self.assertNotIn("tpl", rows[0])
        self.assertNotIn("secret.example", json.dumps(rows[0]))

    def test_extra_keys_are_dropped(self):
        """只收白名单字段，免得调用方哪天顺手把 webhook 地址塞进来。"""
        self.append(rec(1.0, webhook="https://x/y?key=SECRET", password="pw"))
        rows = hist.query(self.d)
        for k in ("webhook", "password"):
            self.assertNotIn(k, rows[0])

    def test_clean_coerces_types(self):
        row = hist.clean({"at": "not-a-number", "ms": None, "tries": 0,
                          "ok": 0, "err": None})
        self.assertIsInstance(row["at"], float)
        self.assertEqual(row["ms"], 0)
        self.assertGreaterEqual(row["tries"], 1)
        self.assertFalse(row["ok"])
        self.assertEqual(row["err"], "")

    # ------------------------------------------------------------ 坏行 / 旧格式
    def test_corrupt_lines_are_skipped(self):
        p = hist.path_of(self.d)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(hist.clean(rec(1000.0))) + "\n")
            f.write("{this is not json\n")
            f.write("\n")
            f.write("[1,2,3]\n")
            f.write(json.dumps({"no_at": 1}) + "\n")
            f.write(json.dumps(hist.clean(rec(2000.0))) + "\n")
        self.assertEqual(len(hist.query(self.d)), 2)
        self.assertEqual(hist.stats(self.d)["total"], 2)

    def test_invalid_utf8_does_not_raise(self):
        p = hist.path_of(self.d)
        with open(p, "wb") as f:
            f.write(b"\xff\xfe\x00garbage\n")
            f.write((json.dumps(hist.clean(rec(1.0))) + "\n").encode("utf-8"))
        self.assertEqual(len(hist.query(self.d)), 1)

    def test_old_record_without_new_fields_still_readable(self):
        """旧格式只有四个键，查询不该 KeyError。"""
        old = {"at": 1000.0, "cam": "院子", "cat": "person", "ok": True}
        with open(hist.path_of(self.d), "w", encoding="utf-8") as f:
            f.write(json.dumps(old) + "\n")
        rows = hist.query(self.d)
        self.assertEqual(rows[0]["cam"], "院子")
        self.assertNotIn("platform", rows[0])

    # ------------------------------------------------------------ 裁剪
    def test_trim_keeps_only_recent(self):
        p = hist.path_of(self.d)
        with open(p, "w", encoding="utf-8") as f:
            for i in range(hist.MAX_KEEP + 2):
                f.write(json.dumps(hist.clean(rec(float(i)))) + "\n")
        self.append(rec(float(hist.MAX_KEEP + 10)))
        self.assertLessEqual(hist.stats(self.d)["total"], hist.MAX_KEEP)
        rows = hist.query(self.d, limit=hist.QUERY_MAX)
        self.assertIn(float(hist.MAX_KEEP + 10), [r["at"] for r in rows])

    def test_trim_removes_tmp(self):
        p = hist.path_of(self.d)
        with open(p, "w", encoding="utf-8") as f:
            for i in range(hist.MAX_KEEP + 1):
                f.write(json.dumps(hist.clean(rec(float(i)))) + "\n")
        self.append(rec(999999.0))
        self.assertFalse(os.path.exists(p + ".tmp"))

    # ------------------------------------------------------------ 查询过滤
    def setUpMulti(self):
        self.append(rec(1000.0, cam="院子", cat="person", ok=True))
        self.append(rec(2000.0, cam="车库", cat="vehicle", ok=False))
        self.append(rec(3000.0, cam="院子", cat="motion", ok=True))

    def test_filter_since_until(self):
        self.setUpMulti()
        self.assertEqual([r["at"] for r in hist.query(self.d, since=1500.0)],
                         [3000.0, 2000.0])
        self.assertEqual([r["at"] for r in hist.query(self.d, until=2000.0)],
                         [2000.0, 1000.0])
        self.assertEqual(len(hist.query(self.d, since=1500.0, until=2500.0)), 1)

    def test_filter_cam_cat_ok(self):
        self.setUpMulti()
        self.assertEqual(len(hist.query(self.d, cam="院子")), 2)
        self.assertEqual(len(hist.query(self.d, cat="vehicle")), 1)
        self.assertEqual(len(hist.query(self.d, ok=False)), 1)
        self.assertEqual(len(hist.query(self.d, ok=True)), 2)
        self.assertEqual(len(hist.query(self.d, cam="院子", ok=False)), 0)

    def test_limit_is_capped(self):
        for i in range(10):
            self.append(rec(float(i)))
        self.assertEqual(len(hist.query(self.d, limit=3)), 3)
        self.assertLessEqual(len(hist.query(self.d, limit=99999)),
                             hist.QUERY_MAX)
        self.assertEqual(len(hist.query(self.d, limit="bad")), 10)

    def test_limit_zero_becomes_one(self):
        self.setUpMulti()
        self.assertEqual(len(hist.query(self.d, limit=0)), 1)

    def test_ok_string_not_treated_as_truthy_inside_module(self):
        """ok 由调用方传布尔；这里只保证 clean 把 0/1 收敛成 bool。"""
        self.assertTrue(hist.clean(rec(1.0, ok=1))["ok"])
        self.assertFalse(hist.clean(rec(1.0, ok=0))["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
