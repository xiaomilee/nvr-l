#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件中心接口（/api/events、/api/events/<id>、/api/events/<id>/clip）测试。

守的是五件事：

  1. **列表来自台账，不来自猜**：事件条数、类别、可播片名都要能追到台账与
     磁盘上的分片；同一份台账同步两次不能变出更多事件。
  2. **同步是增量的**：台账指纹没变就不重算 —— 页面刷新一次全量重扫，
     在 5 路 × 30 天上是几十秒的卡顿。
  3. **定位口径**：起点前 10 秒；落在片头之前就夹到片头（标 clamped），
     不做跨片前摇。
  4. **播不了必须说清楚**：未封口 / 已清理 / 空文件 / 损坏各有各的 code 与
     中文原因，绝不给一个播不动的黑框。
  5. **过滤器不误伤**：按类别筛 person 不能把 two_wheeler 算进来。

跑：python test/test_event_api.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import event  # noqa: E402
import ledger  # noqa: E402
import nvr_slim  # noqa: E402

DATE = "2026-11-06"
CAM = {"id": "c1", "name": "客厅", "dir_name": "客厅", "enabled": True,
       "url": "rtsp://x"}

# 一个结构上可播的最小 MP4：ftyp + moov 两个顶层 box。
# recorder._has_moov 按 box 结构走，认的就是这个。
MP4_OK = (b"\x00\x00\x00\x10ftypisom\x00\x00\x02\x00"
          b"\x00\x00\x00\x08moov")


def hms(h, m, s=0):
    return float(h * 3600 + m * 60 + s)


def epoch(date, sec):
    base = time.mktime(time.strptime(date + " 00:00:00", "%Y-%m-%d %H:%M:%S"))
    return base + float(sec)


class Base(unittest.TestCase):
    """真磁盘：录像分片、台账 JSON、事件索引全在临时目录里。"""

    segs = (hms(7, 25), hms(7, 30), hms(7, 35))

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvreventapi")
        self.root = os.path.join(self.tmp, "rec")
        self.var = os.path.join(self.tmp, "var")
        os.makedirs(self.var, exist_ok=True)
        self.day = os.path.join(self.root, "客厅", DATE)
        os.makedirs(self.day, exist_ok=True)
        for st in self.segs:
            self.write_seg(event.seg_name(st), MP4_OK)
        led = os.path.join(self.root, "_ledger", "客厅")
        os.makedirs(led, exist_ok=True)
        self.led_path = os.path.join(led, DATE + ".json")
        self.write_ledger([
            (hms(7, 30, 5), hms(7, 30, 15), {"person": 2}),
            (hms(7, 31, 30), hms(7, 31, 40), {"vehicle": 1}),
        ])
        self.patches = [
            mock.patch.object(nvr_slim, "cameras", lambda: [dict(CAM)]),
            mock.patch.object(nvr_slim, "cam_storage_dirs", lambda c: ["客厅"]),
            mock.patch.object(nvr_slim, "record_dir", lambda: self.root),
        ]
        for p in self.patches:
            p.start()
        nvr_slim.STATE["var_dir"] = self.var
        nvr_slim.STATE["recorder"] = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 造数据 ----------------------------------------------------
    def write_seg(self, name, blob=MP4_OK, end_sec=None):
        """造一个分片。mtime 一律设成「这一片写完的时刻」——

        ffmpeg 是切到下一片时才关上一片，所以 mtime 就是这一片最后一帧。
        事件定位靠它判断"这个起点还在不在这一片里"，随便取 now 会让那一天
        的 ends 整个变成空（mtime 跨天不采信）。
        """
        p = os.path.join(self.day, name)
        with open(p, "wb") as f:
            f.write(blob)
        st = ledger.src_start(name)
        if st is not None:
            t = epoch(DATE, min(86390.0, st + 300.0 if end_sec is None
                                else end_sec))
            os.utime(p, (t, t))
        return p

    def write_ledger(self, wins):
        data = {"v": 1, "cam": "客厅", "date": DATE, "sources": {
            "ai:07-30-00.mp4": {"start": hms(7, 30), "d": 0.5,
                                "windows": [[s, e, c] for s, e, c in wins]}}}
        with open(self.led_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def listing(self, **kw):
        kw.setdefault("frm", DATE)
        kw.setdefault("to", DATE)
        d = nvr_slim.events_payload(**kw)
        self.assertTrue(d["ok"], d)
        return d

    def first_id(self, **kw):
        return self.listing(**kw)["events"][0]["id"]


class TestList(Base):
    def test_events_from_ledger(self):
        d = self.listing()
        self.assertEqual(d["count"], 2)
        evs = d["events"]
        self.assertEqual(evs[0]["id"], "c1_20261106_073005")
        self.assertEqual(evs[0]["categories"], ["person"])
        # 已结束且已入库：历史日期没有"进行中"（现在时态只属于今天）
        self.assertEqual(evs[0]["state"], "indexed")
        self.assertEqual(evs[0]["source_segment"], "07-30-00.mp4")
        self.assertEqual(evs[1]["categories"], ["vehicle"])
        self.assertEqual(d["summary"]["by_category"]["person"], 1)

    def test_second_call_is_incremental(self):
        """指纹没变就不重算：页面每次刷新都全量重扫，5 路 × 30 天就是几十秒。"""
        self.listing()
        d = self.listing()
        self.assertEqual(d["synced"], [])
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["index"]["events"], 2)

    def test_ledger_change_resyncs(self):
        self.listing()
        self.write_ledger([(hms(7, 30, 5), hms(7, 30, 15), {"person": 2})])
        d = self.listing()
        self.assertEqual(d["synced"], [DATE])
        self.assertEqual(d["count"], 1)

    def test_cat_filter(self):
        self.assertEqual(len(self.listing(cat="person")["events"]), 1)
        self.assertEqual(len(self.listing(cat="vehicle")["events"]), 1)
        # two_wheeler 不能命中 person（按整词匹配）
        self.assertEqual(len(self.listing(cat="two_wheeler")["events"]), 0)

    def test_cam_filter(self):
        self.assertEqual(len(self.listing(cam="c1")["events"]), 2)
        self.assertEqual(len(self.listing(cam="c9")["events"]), 0)

    def test_limit_keeps_newest(self):
        d = self.listing(limit="1")
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["events"][0]["categories"], ["vehicle"])

    def test_bad_range_rejected(self):
        """给了但认不出的时间必须报错：悄悄换成"今天"会让人以为昨天没有事件。"""
        d = nvr_slim.events_payload(frm="yyyy", to="zzz")
        self.assertFalse(d["ok"])
        self.assertIn("无法识别", d["error"])

    def test_empty_range_defaults_to_today(self):
        d = nvr_slim.events_payload()
        self.assertTrue(d["ok"])
        self.assertEqual(d["from"][:10], __import__("time").strftime("%Y-%m-%d"))

    def test_range_too_wide_rejected(self):
        d = nvr_slim.events_payload(frm="2026-01-01", to="2026-12-31")
        self.assertFalse(d["ok"])
        self.assertIn("%d" % nvr_slim.EVENT_MAX_DAYS, d["error"])

    def test_no_cameras_still_ok(self):
        with mock.patch.object(nvr_slim, "cameras", lambda: []):
            d = self.listing()
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["events"], [])


class TestSingle(Base):
    def test_hit(self):
        eid = self.first_id()
        d = nvr_slim.event_payload(eid)
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["event"]["id"], eid)

    def test_garbage_id(self):
        d = nvr_slim.event_payload("not-an-id")
        self.assertFalse(d["ok"])
        self.assertIn("无法识别", d["error"])

    def test_missing_event_syncs_first(self):
        """先同步那一天再查：新事件刚进台账时点进来不该得到"没有这条事件"。"""
        eid = "c1_20261106_073005"
        d = nvr_slim.event_payload(eid)
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["event"]["start"], hms(7, 30, 5))

    def test_missing_event_unknown(self):
        d = nvr_slim.event_payload("c1_20261106_000000")
        self.assertFalse(d["ok"])
        self.assertIn("没有这条事件", d["error"])


class TestClip(Base):
    def clip(self, eid):
        return nvr_slim.event_clip_payload(eid)

    def test_pre_ten_seconds(self):
        """起点 07:30:05 → 从 07:30:00 播（前摇被夹到片头，标 clamped）。"""
        d = self.clip("c1_20261106_073005")
        self.assertTrue(d["ok"])
        self.assertEqual(d["segment"], "07-30-00.mp4")
        self.assertEqual(d["offset_sec"], 0.0)
        self.assertTrue(d["clamped"])
        self.assertIn("name=07-30-00.mp4", d["url"])
        self.assertIsNone(d["missing_reason"])

    def test_offset_carries_pre(self):
        """起点 07:30:40 → 从 07:30:30 播，偏移 30 秒。"""
        self.write_ledger([(hms(7, 30, 40), hms(7, 30, 50), {"person": 1})])
        d = self.clip(self.first_id())
        self.assertEqual(d["offset_sec"], 30.0)
        self.assertFalse(d["clamped"])
        self.assertEqual(d["pre_sec"], 10.0)

    def test_url_goes_through_record_file(self):
        """不另开一条读文件的路径：白名单 / Range / 鉴权都在既有接口里。"""
        d = self.clip("c1_20261106_073005")
        self.assertTrue(d["url"].startswith("/api/record/file?"))
        self.assertIn("cam=c1", d["url"])
        self.assertIn("day=" + DATE, d["url"])

    def test_camera_missing(self):
        d = self.clip("c9_20261106_073005")
        self.assertEqual(d["missing_reason"], "camera_missing")
        self.assertTrue(d["missing_text"])

    def test_no_recording(self):
        shutil.rmtree(self.day)
        d = self.clip("c1_20261106_073005")
        self.assertEqual(d["missing_reason"], "no_recording")

    def test_not_sealed(self):
        """事件落在正在写的分片里 → 说清楚"还没封口"，不指向上一片末尾。"""
        with open(os.path.join(self.day, "07-50-00.mp4.part"), "wb") as f:
            f.write(MP4_OK)
        self.write_ledger([(hms(7, 50, 30), hms(7, 50, 40), {"person": 1})])
        d = self.clip(self.first_id())
        self.assertEqual(d["missing_reason"], "not_sealed")
        self.assertEqual(d["pending_segment"], "07-50-00.mp4")
        self.assertIn("封口", d["missing_text"])

    def test_segment_missing(self):
        """事发那一片被删了：只能找到更早的一片时，必须说"不在了"。"""
        os.unlink(os.path.join(self.day, "07-30-00.mp4"))
        d = self.clip("c1_20261106_073005")
        self.assertEqual(d["missing_reason"], "segment_missing")

    def test_empty_segment(self):
        self.write_seg("07-30-00.mp4", b"")
        d = self.clip("c1_20261106_073005")
        self.assertEqual(d["missing_reason"], "empty_segment")

    def test_corrupt_segment(self):
        """没有 moov 的 .mp4：浏览器打开就是黑屏，直接说"这片坏了"。"""
        self.write_seg("07-30-00.mp4", b"x" * 4096)
        d = self.clip("c1_20261106_073005")
        self.assertEqual(d["missing_reason"], "corrupt_segment")

    def test_garbage_id(self):
        d = self.clip("nope")
        self.assertFalse(d["ok"])

    def test_before_first_segment(self):
        self.write_ledger([(hms(7, 0, 30), hms(7, 0, 40), {"person": 1})])
        d = self.clip(self.first_id())
        self.assertEqual(d["missing_reason"], "no_segment_before")


class TestHelpers(Base):
    def test_day_files_skips_clip_slices(self):
        """活动切片（.cNN.mp4）不是全时录像分片，定位不能把它算进来。"""
        self.write_seg("07-30-00.c01.mp4")
        f = nvr_slim._day_files(CAM, DATE)
        self.assertEqual(len(f["sealed"]), 3)
        self.assertEqual(sorted(f["sealed"].values()),
                         ["07-25-00.mp4", "07-30-00.mp4", "07-35-00.mp4"])

    def test_day_files_collects_parts(self):
        with open(os.path.join(self.day, "07-40-00.mp4.part"), "wb") as f:
            f.write(b"x")
        f = nvr_slim._day_files(CAM, DATE)
        self.assertEqual(list(f["parts"].values()), ["07-40-00.mp4.part"])

    def test_seg_nominal(self):
        self.assertAlmostEqual(nvr_slim._seg_nominal(CAM), 300.0, places=0)

    def test_day_files_records_real_end(self):
        """ends 是「这一片真实录到哪儿」，判断事件是否还在这一片里全靠它。"""
        f = nvr_slim._day_files(CAM, DATE)
        self.assertEqual(sorted(f["ends"].keys()), sorted(self.segs))
        self.assertTrue(all(v > 0 for v in f["ends"].values()))

    def test_norm_when(self):
        self.assertEqual(nvr_slim._norm_when("2026-11-06", "d"), "2026-11-06 00:00:00")
        self.assertEqual(nvr_slim._norm_when("2026-11-06", "d", end_of_day=True),
                         "2026-11-06 23:59:59")
        self.assertEqual(nvr_slim._norm_when("2026-11-06 07:30", "d"),
                         "2026-11-06 07:30:00")
        self.assertEqual(nvr_slim._norm_when("", "d"), "d")
        self.assertIsNone(nvr_slim._norm_when("garbage", "d"))

    def test_dates_between(self):
        self.assertEqual(nvr_slim._dates_between("2026-11-06", "2026-11-06"), [DATE])
        self.assertEqual(nvr_slim._dates_between("2026-11-06", "2026-11-05"), [])
        self.assertEqual(nvr_slim._dates_between("bad", "2026-11-06"), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
