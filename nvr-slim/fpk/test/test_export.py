#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按时间范围批量导出（/api/export、/api/export/plan）测试。

守的是六件事：

  1. **挑片口径是半开区间**：正好在 09:00:00 结束的那一片不算进 09:00 起的
     窗口 —— 与事件检索的时间窗同一套语义，否则同一段录像在两个页面里
     会是两个答案。
  2. **边界多出来的秒数要算出来**：分片原样导出必然多给，多给多少必须写在
     计划与 manifest 里，不能靠用户自己看文件名猜。
  3. **只认已封口的分片**：`.mp4.part` 与活动切片（`.cNN.mp4`）都不进包 ——
     前者没 moov 播不了，后者是压缩产物，不是录像本身。
  4. **上限在计划阶段就拦**：字节 / 文件数 / 天数三条，超了明确说超多少，
     不让人下一个半截的包。
  5. **zip 是流式写出来的**：写进不可 seek 的流（socket）也要能解压，中文
     路径、README/manifest 齐全，且收尾空块真的写出来了（否则客户端会拿到
     一个"看起来正常"的半截包）。
  6. **少给要说**：机位写错整单报错；某天只剩归档片段时如实提示"本次不含归档"。

跑：python test/test_export.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import export  # noqa: E402
import nvr_slim  # noqa: E402

DATE = "2026-11-06"
CAMS = [
    {"id": "c1", "name": "客厅", "dir_name": "客厅", "enabled": True,
     "url": "rtsp://x"},
    {"id": "c2", "name": "院子", "dir_name": "院子", "enabled": True,
     "url": "rtsp://y"},
]
# 结构上能过 recorder._has_moov 的最小 MP4（ftyp + moov）
MP4_OK = (b"\x00\x00\x00\x10ftypisom\x00\x00\x02\x00"
          b"\x00\x00\x00\x08moov")


def hms(h, m, s=0):
    return float(h * 3600 + m * 60 + s)


def epoch(day, sec):
    return time.mktime(time.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S")) + float(sec)


class PureTest(unittest.TestCase):
    """export.py 的纯函数：不碰磁盘，只验口径。"""

    def seg(self, st, en, cam="c1", name="x", size=1000):
        return {"cam": cam, "day": DATE, "name": name,
                "start": float(st), "end": float(en), "bytes": size}

    def test_half_open_left_edge(self):
        """[0,300) 与 [300,600)：左边那片不算 —— 它一秒都没落进窗口。"""
        got = export.select([self.seg(0, 300), self.seg(300, 600)], 300, 600)
        self.assertEqual([g["start_sec"] for g in got], [300])

    def test_half_open_right_edge(self):
        """[300,600) 与 [0,300)：右边也一样，边界归后一片。"""
        got = export.select([self.seg(0, 300), self.seg(300, 600)], 0, 300)
        self.assertEqual([g["start_sec"] for g in got], [0])

    def test_overlap_and_extras(self):
        """窗口 09:03–09:08 落在两片中间：两头都要标出多给了多少。"""
        a, b = 300 + 180, 300 + 480          # 相对 09:00 的秒：09:03 ~ 09:08
        got = export.select([self.seg(300, 600), self.seg(600, 800)], a, b)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["overlap_sec"], 120)      # 09:03–09:05
        self.assertEqual(got[0]["head_extra_sec"], 180)   # 片头比窗口早 3 分钟
        self.assertEqual(got[1]["overlap_sec"], 180)      # 09:05–09:08
        self.assertEqual(got[1]["tail_extra_sec"], 20)    # 尾巴多 20 秒

    def test_totals_takes_first_and_last_only(self):
        """边界秒数只算首尾：中间那几片"多出来"的部分是窗口内部的正常覆盖。"""
        e = export.select([self.seg(0, 300), self.seg(300, 600), self.seg(600, 900)],
                          100, 800)
        t = export.totals(e)
        self.assertEqual(t["count"], 3)
        self.assertEqual(t["bytes"], 3000)
        self.assertEqual(t["head_extra_sec"], 100)
        self.assertEqual(t["tail_extra_sec"], 100)

    def test_cap_problem_three_limits(self):
        self.assertIn("天", export.cap_problem(
            {"count": 1, "bytes": 1}, export.MAX_SPAN_SEC + 1))
        self.assertIn("分片", export.cap_problem(
            {"count": export.MAX_FILES + 1, "bytes": 1}, 60))
        msg = export.cap_problem({"count": 1, "bytes": export.MAX_BYTES + 1}, 60)
        self.assertIn("GB", msg)
        self.assertIsNone(export.cap_problem(
            {"count": 10, "bytes": 1024}, 60))

    def test_slug_sanitizes(self):
        self.assertEqual(export.slug("车库/入口"), "车库_入口")
        self.assertEqual(export.slug('a:b*c?"d'), "a_b_c__d")
        self.assertEqual(export.slug("  ..  "), "cam")      # 全是不能用的字符
        self.assertEqual(export.slug(""), "cam")
        self.assertLessEqual(len(export.slug("长" * 200)), export._NAME_MAX)

    def test_folders_dedupe_only_on_collision(self):
        """同名机位必须分开；不同名的那些不该被无端加后缀。"""
        cams = [{"id": "c1", "name": "客厅"}, {"id": "c2", "name": "客厅"},
                {"id": "c3", "name": "院子"}]
        f = export.folders(cams)
        self.assertNotEqual(f["c1"], f["c2"])
        self.assertEqual(f["c3"], "院子")
        self.assertIn("c1", f["c1"])

    def test_write_zip_seekable(self):
        buf = io.BytesIO()
        n = export.write_zip(buf, [("a/1.mp4", b"hello"),
                                   ("说明.txt", "中文".encode("utf-8"))])
        self.assertEqual(n, 2)
        z = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
        self.assertEqual(z.namelist(), ["a/1.mp4", "说明.txt"])
        self.assertEqual(z.read("说明.txt"), "中文".encode("utf-8"))
        self.assertIsNone(z.testzip())

    def test_chunked_framing_and_unseekable_zip(self):
        """socket 不可 seek：zipfile 会走数据描述符那条路，这里把整条链路跑一遍。"""
        raw = io.BytesIO()
        w = export.Chunked(raw)
        export.write_zip(w, [("a.mp4", b"x" * 1000)])
        w.finish()
        blob = raw.getvalue()
        self.assertTrue(blob.endswith(b"0\r\n\r\n"))
        body = self.unframe(blob)
        z = zipfile.ZipFile(io.BytesIO(body))
        self.assertEqual(z.namelist(), ["a.mp4"])
        self.assertEqual(z.read("a.mp4"), b"x" * 1000)
        self.assertIsNone(z.testzip())

    @staticmethod
    def unframe(blob):
        """把 chunked 编码拆回原始字节（顺带验证每一块的声明长度都对得上）。"""
        out, i = b"", 0
        while True:
            j = blob.index(b"\r\n", i)
            n = int(blob[i:j], 16)
            if n == 0:
                return out
            out += blob[j + 2:j + 2 + n]
            i = j + 2 + n + 2


class RealDiskTest(unittest.TestCase):
    """真磁盘：分片、目录、mtime 全是真的，走的也是 _export_build 那条路。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvrexport")
        self.root = os.path.join(self.tmp, "rec")
        self.var = os.path.join(self.tmp, "var")
        os.makedirs(self.var, exist_ok=True)
        for c in CAMS:
            d = os.path.join(self.root, c["dir_name"], DATE)
            os.makedirs(d, exist_ok=True)
            for st in (hms(8, 55), hms(9, 0), hms(9, 5), hms(9, 10)):
                self.seg(c, st)
            # 未封口的残片 + 活动切片：两种都不该进包
            with open(os.path.join(d, "09-15-00.mp4.part"), "wb") as f:
                f.write(MP4_OK)
            with open(os.path.join(d, "08-55-00.c01.mp4"), "wb") as f:
                f.write(MP4_OK)
        self.patches = [
            mock.patch.object(nvr_slim, "cameras", lambda: [dict(c) for c in CAMS]),
            mock.patch.object(nvr_slim, "cam_storage_dirs",
                              lambda c: [c.get("dir_name") or c["id"]]),
            mock.patch.object(nvr_slim, "record_dir", lambda: self.root),
            mock.patch.object(nvr_slim, "_seg_nominal", lambda c: 300.0),
        ]
        for p in self.patches:
            p.start()
        nvr_slim.STATE["var_dir"] = self.var
        nvr_slim.STATE["recorder"] = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seg(self, cam, st):
        name = "%02d-%02d-%02d.mp4" % (int(st // 3600), int(st % 3600 // 60),
                                       int(st % 60))
        p = os.path.join(self.root, cam["dir_name"], DATE, name)
        with open(p, "wb") as f:
            f.write(MP4_OK)
        # mtime = 这一片写完的时刻。事件定位与导出都靠它算"真实结束"。
        t = epoch(DATE, st + 300)
        os.utime(p, (t, t))
        return p

    # ---- 计划 ------------------------------------------------------
    def test_plan_counts_and_skips_unsealed(self):
        got = nvr_slim.export_plan_payload("c1", DATE + " 09:00", DATE + " 09:10")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["total"]["count"], 2)      # 09:00 与 09:05 两片
        self.assertEqual(got["cams"][0]["count"], 2)
        self.assertEqual(got["total"]["head_extra_sec"], 0)
        self.assertEqual(got["total"]["tail_extra_sec"], 0)
        self.assertNotIn("files", got)                  # 默认不吐几百条明细
        detail = nvr_slim.export_plan_payload("c1", DATE + " 09:00",
                                              DATE + " 09:10", detail=True)
        names = [f["name"] for f in detail["files"]]
        self.assertEqual(names, ["09-00-00.mp4", "09-05-00.mp4"])
        # 未封口与活动切片都不在包里
        joined = json.dumps(detail["files"], ensure_ascii=False)
        self.assertNotIn(".part", joined)
        self.assertNotIn(".c01", joined)

    def test_plan_extra_at_both_ends(self):
        got = nvr_slim.export_plan_payload("c1", DATE + " 09:03", DATE + " 09:08",
                                           detail=True)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["total"]["count"], 2)
        self.assertEqual(got["files"][0]["head_extra_sec"], 180)
        self.assertEqual(got["files"][-1]["tail_extra_sec"], 120)
        self.assertEqual(got["total"]["head_extra_sec"], 180)
        self.assertEqual(got["total"]["tail_extra_sec"], 120)

    def test_plan_multi_cam_and_url(self):
        got = nvr_slim.export_plan_payload("c1,c2", DATE + " 09:00",
                                           DATE + " 09:10")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["total"]["count"], 4)
        self.assertEqual([c["folder"] for c in got["cams"]], ["客厅", "院子"])
        self.assertIn("/api/export?", got["url"])
        self.assertIn("cam=c1%2Cc2", got["url"])

    def test_plan_bad_inputs(self):
        bad_cam = nvr_slim.export_plan_payload("c9", DATE + " 09:00",
                                               DATE + " 09:10")
        self.assertFalse(bad_cam["ok"])
        self.assertIn("c9", bad_cam["error"])
        bad_time = nvr_slim.export_plan_payload("c1", "昨天", "今天")
        self.assertFalse(bad_time["ok"])
        same = nvr_slim.export_plan_payload("c1", DATE + " 09:00",
                                            DATE + " 09:00")
        self.assertFalse(same["ok"])
        empty = nvr_slim.export_plan_payload("c1", DATE + " 12:00",
                                             DATE + " 13:00")
        self.assertFalse(empty["ok"])
        self.assertIn("封口", empty["error"])

    def test_plan_reports_cap(self):
        with mock.patch.object(export, "MAX_BYTES", 10):
            got = nvr_slim.export_plan_payload("c1", DATE + " 09:00",
                                               DATE + " 09:10")
        self.assertFalse(got["ok"])
        self.assertIn("GB", got["error"])
        self.assertEqual(got["total"]["count"], 2)      # 仍然告诉你有多少

    def test_plan_warns_archive_only_day(self):
        """全时已过期、只剩归档片段的那天：如实说"本次不含归档"。"""
        old = "2026-11-01"
        for c in CAMS:
            d = os.path.join(self.root, "_archive", c["dir_name"], old)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "10-00-00.c01.mp4"), "wb") as f:
                f.write(MP4_OK)
        got = nvr_slim.export_plan_payload("c1", old, old)
        self.assertFalse(got["ok"])                     # 没有全时分片 → 导不出
        self.assertEqual([g["day"] for g in got["archive_only"]], [old])

    def test_plan_warns_archive_only_mixed(self):
        """同一次请求里有一天能导、有一天只剩归档：能导的照导，另一天写进警告。"""
        old = "2026-11-01"
        os.makedirs(os.path.join(self.root, "_archive", "客厅", old), exist_ok=True)
        with open(os.path.join(self.root, "_archive", "客厅", old,
                               "10-00-00.c01.mp4"), "wb") as f:
            f.write(MP4_OK)
        got = nvr_slim.export_plan_payload("c1", old, DATE + " 09:10")
        self.assertTrue(got["ok"], got)
        self.assertTrue(any("不含归档" in w for w in got["warnings"]))
        self.assertEqual(got["total"]["count"], 3)      # 08:55 / 09:00 / 09:05

    # ---- 下载 ------------------------------------------------------
    def test_download_streams_zip(self):
        h = _FakeHandler(io.BytesIO())
        h._export_download("cam=c1&from=%s%%2009%%3A00&to=%s%%2009%%3A10"
                           % (DATE, DATE))
        head = dict((k, v) for k, v in h.head if k != "status")
        self.assertEqual(head.get("content-type"), "application/zip")
        self.assertIn("attachment", head.get("content-disposition", ""))
        self.assertEqual(head.get("x-export-files"), "2")
        body = PureTest.unframe(h.wfile.getvalue())
        z = zipfile.ZipFile(io.BytesIO(body))
        names = z.namelist()
        self.assertEqual(names[0], "README.txt")
        self.assertEqual(names[1], "manifest.json")
        self.assertEqual(names[2:], ["客厅/%s/09-00-00.mp4" % DATE,
                                     "客厅/%s/09-05-00.mp4" % DATE])
        man = json.loads(z.read("manifest.json").decode("utf-8"))
        self.assertEqual(man["counts"]["count"], 2)
        self.assertEqual(len(man["files"]), 2)
        self.assertIn("start_at", man["files"][0])
        readme = z.read("README.txt").decode("utf-8")
        self.assertIn("没有重新编码", readme)
        self.assertIn("没有录像", readme)

    def test_download_rejects_bad_cam(self):
        h = _FakeHandler(io.BytesIO())
        h._export_download("cam=nope&from=%s&to=%s" % (DATE, DATE))
        self.assertTrue(h.body.get("ok") is False)
        self.assertIn("nope", h.body.get("error"))
        self.assertEqual(h.wfile.getvalue(), b"")      # 一个字节都没往外写


class _FakeHandler(nvr_slim.Handler):
    """只借 Handler 的响应方法，不要真 socket。"""

    def __init__(self, wfile):
        self.wfile = wfile
        self._responded = False
        self.headers = {}
        self.head = []
        self.body = None
        self.close_connection = False

    def send_response(self, code, message=None):
        self.head.append(("status", code))

    def send_header(self, k, v):
        self.head.append((str(k).lower(), v))

    def end_headers(self):
        pass

    def _send(self, code, body, ctype=None, headers=None):
        self._responded = True
        self.body = body
        self.head.append(("status", code))


if __name__ == "__main__":
    unittest.main(verbosity=2)
