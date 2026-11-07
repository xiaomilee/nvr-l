#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「多久没写进字节」的口径一致性（0.9.120）。

真机现场：测试机 192.168.2.152 上两路都在稳定写 `.part`（分片每 300 秒封口一次），
`/api/link` 却报 `hstate=stalled` —— 因为 `status()` 当时只数**已封口**的 `.mp4`，
而判 stalled 的阈值是 60 秒。于是每 5 分钟里有 4 分钟界面在喊断流：

  - 真断流时反而看不出来（一直红着 = 没有告警）；
  - 看门狗用的是 `_newest_age`（含 `.part`），它认为一切正常、不重启进程，
    于是「界面说断了、进程说没事」，两个口径互相打脸。

现在 `status()` 与看门狗同源。这个文件就钉住这件事，同时钉住它不能反过来
把「今天真的一片都没录」也算成正常。

跑：python test/test_status_age.py
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "server"))

import health     # noqa: E402
import recorder   # noqa: E402

DAY = "2026-11-08"


def _touch(path, age_sec=0):
    """造一个文件并把 mtime 拨到 age_sec 秒之前。"""
    with open(path, "wb") as f:
        f.write(b"\0" * 1024)
    if age_sec:
        import time
        t = time.time() - age_sec
        os.utime(path, (t, t))


def _mk(day_secs, part_sec=None, other_day=None):
    """建一个只看得到「今天」的录制目录。

    day_secs：已封口分片的 mtime 距今秒数（None = 一个都没有）
    part_sec：正在写的 .part 距今年秒数（None = 没有 .part）
    """
    root = tempfile.mkdtemp(prefix="nvr-age-")
    daydir = os.path.join(root, "c1", DAY)
    os.makedirs(daydir, exist_ok=True)
    if day_secs is not None:
        _touch(os.path.join(daydir, "10-00-00.mp4"), day_secs)
    if part_sec is not None:
        _touch(os.path.join(daydir, "10-05-00.mp4.part"), part_sec)
    if other_day:
        od = os.path.join(root, "c1", other_day)
        os.makedirs(od, exist_ok=True)
        _touch(os.path.join(od, "23-55-00.mp4"), 90000)
    r = recorder.Recorder("ffmpeg", root, [], log=lambda *_: None, var_dir=root)
    cam = {"id": "c1", "name": "测试", "url": "rtsp://192.0.2.10/live"}
    r.cams = [cam]
    r.procs["c1"] = {"p": _Alive(), "tries": 0, "stall": 0, "started_at": 0,
                     "day": DAY}
    return r, cam


class _Alive:
    """活着的 ffmpeg 替身：poll() 返回 None。"""
    pid = 4321

    def poll(self):
        return None


class TestAgeSource(unittest.TestCase):
    """今天固定用 date.today()，所以临时目录要按真实日期建。"""

    def setUp(self):
        # _newest_age 只看当天目录，测试得跟着真实日期走
        import datetime
        global DAY
        DAY = datetime.date.today().isoformat()

    def test_part_file_counts_as_writing(self):
        # 分片刚封口 4 分钟，但 .part 一秒前还在写 → 这一路是好的
        r, cam = _mk(240, part_sec=1)
        ch = r.status()["channels"][0]
        self.assertLessEqual(ch["last_age_sec"], 5)
        self.assertEqual(health.state_of(ch), "recording")

    def test_no_part_and_stale_means_stalled(self):
        # 只有 300 秒前封口的分片、没有 .part → 真断流，必须报 stalled
        r, cam = _mk(300, part_sec=None)
        ch = r.status()["channels"][0]
        self.assertGreater(ch["last_age_sec"], health.STALL_SEC)
        self.assertEqual(health.state_of(ch), "stalled")

    def test_nothing_today_is_not_normal(self):
        # 今天一个字节都没有、昨天有 → 退回已封口分片，age 必须很大
        import datetime
        yday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        root = tempfile.mkdtemp(prefix="nvr-age-")
        od = os.path.join(root, "c1", yday)
        os.makedirs(od, exist_ok=True)
        _touch(os.path.join(od, "23-55-00.mp4"), 7200)
        r = recorder.Recorder("ffmpeg", root, [], log=lambda *_: None, var_dir=root)
        r.cams = [{"id": "c1", "name": "测试", "url": "rtsp://192.0.2.10/live"}]
        r.procs["c1"] = {"p": _Alive(), "tries": 0, "stall": 0, "started_at": 0,
                         "day": DAY}
        ch = r.status()["channels"][0]
        self.assertIsNotNone(ch["last_age_sec"])
        self.assertGreater(ch["last_age_sec"], 3600)
        self.assertEqual(health.state_of(ch), "stalled")

    def test_no_files_at_all_no_number(self):
        # 什么都没有：绝不编一个数字出来，None 表示"不知道"
        root = tempfile.mkdtemp(prefix="nvr-age-")
        os.makedirs(os.path.join(root, "c1"), exist_ok=True)
        r = recorder.Recorder("ffmpeg", root, [], log=lambda *_: None, var_dir=root)
        r.cams = [{"id": "c1", "name": "测试", "url": "rtsp://192.0.2.10/live"}]
        r.procs["c1"] = {"p": _Alive(), "tries": 0, "stall": 0, "started_at": 0,
                         "day": DAY}
        ch = r.status()["channels"][0]
        self.assertIsNone(ch["last_age_sec"])

    def test_last_seg_still_closed_file(self):
        # 「最近分片」是给人看的文件名，仍是已封口那个，不能变成 .part
        r, cam = _mk(240, part_sec=1)
        ch = r.status()["channels"][0]
        self.assertEqual(ch["last_seg"], "10-00-00.mp4")

    def test_status_matches_watchdog(self):
        # 两个口径必须一致，否则一边在杀进程一边界面说正常
        r, cam = _mk(240, part_sec=2)
        ch = r.status()["channels"][0]
        self.assertLessEqual(abs(ch["last_age_sec"] - int(r._newest_age(cam))), 1)

    def test_segments_count_excludes_part(self):
        # 计数仍只数已封口的：.part 还没写完，算进去会让"分片数"跳来跳去
        r, cam = _mk(240, part_sec=1)
        self.assertEqual(r.status()["channels"][0]["segments"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
