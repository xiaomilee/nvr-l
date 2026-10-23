#!/usr/bin/env python3
"""0.9.38 缩时压制切分方式（join/split）的行为测试。

守的几条：
  - 通用 clip_style 只认 join/split；每路 clip_mode 只认 follow/join/split
  - 精确导出命令：QSV 与软编两条路都帧级精确（-ss 前置 + 重编码）
  - 三级回落：QSV 失败 → libx264 → itsscale 转封装（免编码器），硬编失败要进负缓存
  - write_split 产物：cNN.mp4 按时间升序 + idx.json 侧车；幂等；半途失败清场
  - link 调度：split 路在定时批量模式下仍然即刻入队；切分方式进幂等键
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import condense    # noqa: E402
import ffmpeg_cmd  # noqa: E402
import link as link_mod  # noqa: E402
import nvr_slim    # noqa: E402
import params      # noqa: E402
import runtime     # noqa: E402

CAMS = [{"id": "cam-01", "name": "车库入口", "url": "rtsp://x/1",
         "condense": True, "clip_mode": "split"},
        {"id": "cam-02", "name": "一楼大堂", "url": "rtsp://x/2",
         "condense": True, "clip_mode": "follow"},
        {"id": "cam-03", "name": "后门", "url": "rtsp://x/3",
         "condense": True, "clip_mode": "join"}]


def touch(path, size=16, age=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    if age:
        st = os.stat(path)
        os.utime(path, (st.st_atime - age, st.st_mtime - age))


class TestClamp(unittest.TestCase):
    def test_clip_style_whitelist(self):
        self.assertEqual(runtime.clip_style("split"), "split")
        self.assertEqual(runtime.clip_style("join"), "join")
        self.assertEqual(runtime.clip_style(""), "join")
        self.assertEqual(runtime.clip_style(None), "join")
        self.assertEqual(runtime.clip_style("nope"), "join")
        # coerce 走枚举键：布尔/数字都进不来
        self.assertEqual(runtime.coerce("clip_style", True), "join")

    def test_clip_mode_whitelist(self):
        self.assertEqual(runtime.clip_mode("follow"), "follow")
        self.assertEqual(runtime.clip_mode("split"), "split")
        self.assertEqual(runtime.clip_mode("bogus"), "follow")
        self.assertEqual(runtime.clip_mode(None), "follow")


class TestPreciseCmd(unittest.TestCase):
    def test_qsv_variant_frame_accurate(self):
        c = ffmpeg_cmd.precise_cmd("ffmpeg", "in.mp4", "o.mp4", 3.5, 9.2, hw="qsv")
        s = " ".join(c)
        self.assertIn("-ss 3.5", s)          # 输入侧 seek，起点不受关键帧约束
        self.assertIn("-to 9.2", s)
        self.assertIn("h264_qsv", s)         # 输出固定 H.264（浏览器可播）
        self.assertIn("-global_quality 26", s)
        self.assertNotIn("copy", c)

    def test_sw_variant(self):
        c = ffmpeg_cmd.precise_cmd("ffmpeg", "in.mp4", "o.mp4", 0, 5, hw="none")
        s = " ".join(c)
        self.assertIn("libx264", s)
        self.assertIn("veryfast", s)
        self.assertNotIn("qsv", s)

    def test_speed_reuses_setpts(self):
        c = ffmpeg_cmd.precise_cmd("ffmpeg", "in.mp4", "o.mp4", 0, 5, hw="none",
                                   speed=2.0)
        s = " ".join(c)
        self.assertIn("setpts=PTS/2", s)
        self.assertIn("fps=", s)

    def test_hwaccel_precedes_input(self):
        """0.9.39 回归：-hwaccel 是输入选项，放 -i 之后 ffmpeg 直接报错。

        0.9.38 把它跟在 -i 后面，QSV 那一级在真机上 100% 必挂
        （"Error opening output files: Invalid argument"），三处导出命令同查。
        """
        for cmd in (ffmpeg_cmd.precise_cmd("ffmpeg", "in.mp4", "o.mp4", 0, 5, hw="qsv"),
                    ffmpeg_cmd.encode_cmd("ffmpeg", "in.mp4", "o.mp4",
                                          {"mode": "fine"}, hw="qsv"),
                    ffmpeg_cmd.speed_cmd("ffmpeg", "in.mp4", "o.mp4", 2, "reencode")):
            self.assertLess(cmd.index("-hwaccel"), cmd.index("-i"), cmd)

    def test_copy_speed_cmd_encoder_free(self):
        c = ffmpeg_cmd.copy_speed_cmd("ffmpeg", "in.mp4", "o.mp4", 10, 30, speed=2)
        s = " ".join(c)
        self.assertIn("-itsscale 0.5", s)    # 2 倍速 = 时间戳减半
        self.assertIn("-c copy", s)
        self.assertNotIn("264", s)           # 不依赖任何编码器
        self.assertNotIn("265", s)


class TestExportPrecise(unittest.TestCase):
    def setUp(self):
        condense._HW_FAIL.clear()
        self.tmp = tempfile.mkdtemp(prefix="nvrclip")
        self.out = os.path.join(self.tmp, "o.mp4")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_qsv_ok(self):
        def fake_run(cmd, capture_output=True, timeout=None):
            class R:
                returncode = 0
                stderr = b""
            with open(cmd[-1], "wb") as f:
                f.write(b"data")
            return R()
        with mock.patch.object(condense.subprocess, "run", fake_run):
            ok, err, codec = condense._export_precise("ff", "s.mp4", self.out,
                                                      0, 5, "qsv")
        self.assertTrue(ok)
        self.assertEqual(codec, "qsv")

    def test_qsv_fail_falls_to_x264_and_cached(self):
        calls = []

        def fake_run(cmd, capture_output=True, timeout=None):
            calls.append(" ".join(cmd))
            if "h264_qsv" in calls[-1]:
                class R:
                    returncode = 1
                    stderr = b"qsv boom"
                return R()
            class R:
                returncode = 0
                stderr = b""
            with open(cmd[-1], "wb") as f:
                f.write(b"data")
            return R()

        with mock.patch.object(condense.subprocess, "run", fake_run):
            ok, err, codec = condense._export_precise("ff", "s.mp4", self.out,
                                                      0, 5, "qsv")
        self.assertTrue(ok)
        self.assertEqual(codec, "x264")
        # 负缓存生效：下一次连 qsv 都不再试，只有一次调用
        calls.clear()
        with mock.patch.object(condense.subprocess, "run", fake_run):
            ok, err, codec = condense._export_precise("ff", "s.mp4",
                                                      self.out + "2", 0, 5, "qsv")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("h264_qsv", calls[0])
        self.assertFalse(condense._hw_available("ff", "qsv"))

    def test_all_fail_falls_to_copy(self):
        def fake_run(cmd, capture_output=True, timeout=None):
            class R:
                returncode = 1
                stderr = b"boom"
            return R()

        with mock.patch.object(condense.subprocess, "run", fake_run), \
             mock.patch.object(condense, "_export_copy",
                               return_value=(True, "")) as mo:
            ok, err, codec = condense._export_precise("ff", "s.mp4", self.out,
                                                      0, 5, "qsv")
        self.assertTrue(ok)
        self.assertEqual(codec, "copy")
        mo.assert_called_once()
        # force=True：speed==1 时也要有人接住（x264 缺失的场景）
        self.assertTrue(mo.call_args.kwargs.get("force"))

    def test_copy_fallback_skipped_without_force(self):
        """speed==1 且非强制时不再空跑 copy_speed_cmd（主路径本来就是 copy）。"""
        with mock.patch.object(condense.ffmpeg_cmd, "copy_speed_cmd",
                               side_effect=AssertionError("不该调用")):
            ok, err = condense._export_copy("ff", "s.mp4", "o.mp4", 0, 5,
                                            1.0, 900, "boom")
        self.assertFalse(ok)

    def test_export_one_speed_falls_to_copy(self):
        """整段拼接的倍速重编码失败（缺 libx264）时退 to itsscale 转封装。

        0.9.38 之前这一步直接失败返回，join 路在 LGPL ffmpeg 上也一样没输出。
        """
        def fake_run(cmd, capture_output=True, timeout=None):
            s = " ".join(cmd)
            if "libx264" in s:
                class R1:
                    returncode = 1
                    stderr = b"Unknown encoder 'libx264'"
                return R1()
            class R2:
                returncode = 0
                stderr = b""
            with open(cmd[-1], "wb") as f:
                f.write(b"data")
            return R2()

        with mock.patch.object(condense.subprocess, "run", fake_run):
            ok, err = condense._export_one("ff", "s.mp4", self.out, 0, 5,
                                           900, speed=2.0)
        self.assertTrue(ok)


class TestWriteSplit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nvrsplit")
        self.src = os.path.join(self.tmp, "seg.mp4")
        touch(self.src, 64)
        self.base = os.path.join(self.tmp, "_clip", "cam", "10-00-00")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_export(self, ok=True, codec="qsv"):
        def fake(ffmpeg, src, out, ss, to, hw, speed=1.0, timeout=900):
            if ok:
                with open(out, "wb") as f:
                    f.write(b"clip")
            return ok, "" if ok else "boom", codec
        return fake

    def test_products_and_index(self):
        with mock.patch.object(condense, "_export_precise", self._fake_export()):
            r = condense.write_split("ff", self.src, self.base,
                                     [(3.0, 9.0), (20.0, 26.5)],
                                     os.path.join(self.tmp, "t"), hw="qsv")
        self.assertTrue(r["ok"])
        self.assertEqual(r["parts"], 2)
        f1 = self.base + ".c01.mp4"
        f2 = self.base + ".c02.mp4"
        idx = self.base + ".idx.json"
        for p in (f1, f2, idx):
            self.assertTrue(os.path.isfile(p), p)
        # 片段名排序后与时间顺序一致（回放页目录扫描零改动）
        self.assertLess(os.path.basename(f1), os.path.basename(f2))
        meta = json.load(open(idx, encoding="utf-8"))
        self.assertEqual(meta["src"], "seg.mp4")
        self.assertEqual(len(meta["windows"]), 2)
        self.assertEqual(meta["windows"][0]["ss"], 3.0)
        self.assertEqual(meta["windows"][1]["to"], 26.5)

    def test_idempotent_by_index(self):
        with mock.patch.object(condense, "_export_precise", self._fake_export()):
            condense.write_split("ff", self.src, self.base, [(0, 5)],
                                 os.path.join(self.tmp, "t"))
        with mock.patch.object(condense, "_export_precise",
                               side_effect=AssertionError("不该再切")):
            r = condense.write_split("ff", self.src, self.base, [(0, 5)],
                                     os.path.join(self.tmp, "t"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["reason"], "exists")

    def test_partial_failure_cleans_up(self):
        state = {"n": 0}

        def fake(ffmpeg, src, out, ss, to, hw, speed=1.0, timeout=900):
            state["n"] += 1
            if state["n"] == 1:
                with open(out, "wb") as f:
                    f.write(b"clip")
                return True, "", "qsv"
            return False, "boom", "copy"

        with mock.patch.object(condense, "_export_precise", fake):
            r = condense.write_split("ff", self.src, self.base,
                                     [(0, 5), (6, 9)], os.path.join(self.tmp, "t"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["index"], 1)
        # 半套产物不能留：成功过的 c01 也要清掉，别让回放页猜
        self.assertFalse(os.path.exists(self.base + ".c01.mp4"))
        self.assertFalse(os.path.exists(self.base + ".idx.json"))

    def test_no_window(self):
        r = condense.write_split("ff", self.src, self.base, [],
                                 os.path.join(self.tmp, "t"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no-window")


class TestClipWindow(unittest.TestCase):
    """0.9.39：idx 侧车 → 片段真实源时间窗（修「同一 5 分钟多个片段全标 5 分钟」）。"""

    def setUp(self):
        import nvr_slim
        self.ns = nvr_slim
        self.day = tempfile.mkdtemp(prefix="nvrday")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.day, ignore_errors=True)
        self.ns._IDX_CACHE.clear()

    def test_window_from_idx(self):
        touch(os.path.join(self.day, "10-30-00.c01.mp4"))
        touch(os.path.join(self.day, "10-30-00.c02.mp4"))
        with open(os.path.join(self.day, "10-30-00.idx.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"windows": [
                {"file": "10-30-00.c01.mp4", "ss": 10.0, "to": 20.0},
                {"file": "10-30-00.c02.mp4", "ss": 100.0, "to": 140.0}]}, f)
        # 分片起点 10:30:00 = 37800 秒
        self.assertEqual(self.ns._clip_window(self.day, "10-30-00.c01.mp4"),
                         [37810.0, 37820.0])
        self.assertEqual(self.ns._clip_window(self.day, "10-30-00.c02.mp4"),
                         [37900.0, 37940.0])

    def test_non_slice_and_missing_idx(self):
        self.assertIsNone(self.ns._clip_window(self.day, "10-30-00.mp4"))
        touch(os.path.join(self.day, "10-30-00.c01.mp4"))
        self.assertIsNone(self.ns._clip_window(self.day, "10-30-00.c01.mp4"))


class TestLinkSplit(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvrroot")
        self.var = tempfile.mkdtemp(prefix="nvrvar")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.var, ignore_errors=True)

    def make(self, clip_style="join", link_mode="live"):
        cfg = {"link_mode": link_mode, "link_interval_h": 6,
               "record_segment_sec": 300, "clip_style": clip_style}
        return link_mod.Link("ffmpeg", "ffprobe", self.root, self.var, CAMS, cfg)

    def _seed(self, cam_dir, n=2, age=9999):
        base = os.path.join(self.root, cam_dir, "2026-10-20")
        for i in range(n):
            # 每个文件 age 递减：10-02 的 mtime 最新（排序测试要区分先后）
            touch(os.path.join(base, "10-0%d-00.mp4" % i), age=age - i)

    def test_effective_mode(self):
        lk = self.make(clip_style="join")
        self.assertEqual(lk.eff_clip_mode("cam-01"), "split")   # 路上显式 split
        self.assertEqual(lk.eff_clip_mode("cam-02"), "join")    # follow → 通用
        self.assertEqual(lk.eff_clip_mode("cam-03"), "join")
        lk2 = self.make(clip_style="split")
        self.assertEqual(lk2.eff_clip_mode("cam-02"), "split")  # follow → 通用 split

    def test_key_changes_with_style(self):
        lk = self.make()
        p = params.new_profile("x")
        base = os.path.join(self.root, "cam-01", "2026-10-20")
        touch(os.path.join(base, "10-00-00.mp4"))
        src = os.path.join(base, "10-00-00.mp4")
        k1 = link_mod.item_key(src, self.root, "cam-01", p, [], style="join")
        k2 = link_mod.item_key(src, self.root, "cam-01", p, [], style="split")
        self.assertNotEqual(k1, k2)     # 改切分方式必须重跑

    def test_items_carry_style(self):
        self._seed("cam-01")
        lk = self.make(clip_style="join")
        items = lk._new_items()
        self.assertEqual(len(items), 2)
        self.assertTrue(all(it["style"] == "split" for it in items))

    def test_items_newest_first(self):
        """0.9.39：改切分方式会重排全部历史，正序要消化完积压才轮到今天。"""
        self._seed("cam-01", n=3)       # 10-00 / 10-01 / 10-02，mtime 递增
        lk = self.make(clip_style="join")
        items = lk._new_items()
        mts = [it["mtime"] for it in items]
        self.assertEqual(mts, sorted(mts, reverse=True))
        self.assertTrue(items[0]["path"].endswith("10-02-00.mp4"))

    def test_items_newest_across_cams(self):
        """0.9.40：排序按 mtime 不按路径。

        0.9.39 按路径倒序被中文目录名的 Unicode 码点劫持——「餐>院>车>客」，
        餐厅的历史消化完之前别的路一片都出不来（真机表现为"只有一路在出片"）。
        cam-01（车库入口）放旧分片、cam-02（一楼大堂）放新分片，
        新的必须排在前面，跟目录名毫无关系。
        """
        self._seed("cam-01", n=1, age=9000)     # 车库入口：旧
        self._seed("cam-02", n=1, age=500)      # 一楼大堂：新（500>封口阈值，可处理）
        lk = self.make(clip_style="join")
        items = lk._new_items()
        self.assertEqual(len(items), 2)
        self.assertEqual(os.path.basename(items[0]["path"]),
                         os.path.basename(items[1]["path"]))
        self.assertEqual(items[0]["dir"], "cam-02")   # 新的分片在前
        self.assertEqual(items[1]["dir"], "cam-01")

    def test_scheduled_tick_enqueues_split_only(self):
        self._seed("cam-01")            # split 路
        self._seed("cam-03")            # join 路
        lk = self.make(clip_style="join", link_mode="scheduled")
        # 首拍会立即做一次批量（既有行为），把批量窗口推到未来再测普通一拍
        lk.next_batch = 1000.0 + 6 * 3600
        lk.tick(now=1000.0)
        got = [it["cam"] for it in lk.pending]
        self.assertIn("cam-01", got)
        self.assertNotIn("cam-03", got)  # join 路要等批量窗口

    def test_scheduled_batch_enqueues_all(self):
        self._seed("cam-01")
        self._seed("cam-03")
        lk = self.make(clip_style="join", link_mode="scheduled")
        lk.next_batch = 1000.0 + 6 * 3600
        lk.tick(now=1000.0)             # 普通一拍：只有 split 路
        got = [it["cam"] for it in lk.pending]
        self.assertNotIn("cam-03", got)
        lk.trigger()                    # 批量窗口：join 路也进来
        got = [it["cam"] for it in lk.pending]
        self.assertIn("cam-01", got)
        self.assertIn("cam-03", got)

    def test_reconfigure_updates_style(self):
        lk = self.make(clip_style="join")
        self.assertEqual(lk.clip_style, "join")
        lk.reconfigure({"link_mode": "live", "record_segment_sec": 300,
                        "clip_style": "split"})
        self.assertEqual(lk.clip_style, "split")
        self.assertEqual(lk.eff_clip_mode("cam-02"), "split")

    def test_status_carries_style(self):
        lk = self.make(clip_style="split")
        s = lk.status()
        self.assertEqual(s["clip_style"], "split")
        self.assertEqual(s["clip_style_label"], "活动切片")


class TestNameWhitelist(unittest.TestCase):
    """0.9.40：/api/record/file 的文件名白名单要认活动切片。

    真机翻过车：切片文件产得好好的，白名单只认 01-15-00.mp4，
    页面里切片全部 400 —— 「出片了但播不了」。
    """

    def test_clip_accepts_slice_names(self):
        for n in ("01-15-00.mp4", "01-15-00.c01.mp4", "01-15-00.c02.mp4",
                  "23-59-59.c99.mp4", "00-00-00.c100.mp4"):
            self.assertTrue(nvr_slim.NAME_CLIP_RE.match(n), n)

    def test_clip_rejects_traversal_and_garbage(self):
        for n in ("../../etc/passwd", "01-15-00.mp4.exe", "x.mp4",
                  "01-15-00.c1.mp4",            # 切片序号至少两位
                  "01-15-00..mp4", "01-15-00.c.mp4", ""):
            self.assertFalse(nvr_slim.NAME_CLIP_RE.match(n), n)

    def test_full_rejects_slice_names(self):
        # full 目录里永远不会出现 .cNN；full 请求拿切片名来撞目录必须拒绝
        self.assertTrue(nvr_slim.NAME_FULL_RE.match("01-15-00.mp4"))
        self.assertFalse(nvr_slim.NAME_FULL_RE.match("01-15-00.c02.mp4"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
