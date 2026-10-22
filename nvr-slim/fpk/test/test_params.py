#!/usr/bin/env python3
"""参数体系与命令构建的断言测试（标准库 unittest，无需 pytest）。

重点守住几条"看起来对、实际会让性能或产物出错"的边界：
  - 分析支路必须零拷贝（漏 -hwaccel_output_format 会让 CPU 翻倍）
  - QSV 用 -global_quality 而不是 -crf
  - 流复制模式下不能出现任何画质参数
  - 参数指纹与字典顺序无关，但会随判据变化
  - 黑白/彩色是两套判据，不是同一套换个数值
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import params            # noqa: E402
import ffmpeg_cmd as fc  # noqa: E402


class TestParams(unittest.TestCase):
    def setUp(self):
        self.prof = params.new_profile("测试机位")

    def test_defaults_valid(self):
        ok, errs = params.validate(self.prof)
        self.assertTrue(ok, errs)

    def test_scene_presets_differ(self):
        mono = params.resolve(self.prof, "mono")
        color = params.resolve(self.prof, "color")
        self.assertEqual(mono["blur"], 1)          # 黑白必须空域模糊压颗粒噪声
        self.assertEqual(mono["pixel_delta"], 20)  # 门槛随 0.9.22 的像素差引擎上调
        self.assertFalse(mono["use_chroma"])
        self.assertEqual(color["blur"], 0)         # 彩色色度本身干净，模糊会丢小目标
        self.assertTrue(color["use_chroma"])

    def test_scene_classify_uses_chroma_std(self):
        # 真黑白 U/V 恒定 → std < 1.5；夜间彩色虽然饱和度低，色度仍有起伏
        self.assertEqual(params.classify_scene(1.2), "mono")
        self.assertEqual(params.classify_scene(23.2), "color")
        self.assertEqual(params.classify_scene(6.4), "color")

    def test_threshold_adaptive(self):
        a = params.resolve(self.prof, "color")
        t_noisy = params.threshold(0.0020, a)      # 树多雨多
        t_clean = params.threshold(0.0001, a)
        self.assertGreater(t_noisy, t_clean)
        self.assertAlmostEqual(t_noisy, 0.0040, places=6)   # 噪声 × 2.0
        # 噪声极小时不能乘出一个荒谬的小阈值
        self.assertGreaterEqual(t_clean, a["floor"])

    def test_light_change_vs_motion(self):
        a = params.resolve(self.prof, "color")
        self.assertTrue(params.light_is_event(40, 62, 0.98, a))   # 整点开灯
        self.assertFalse(params.light_is_event(40, 43, 0.42, a))  # 车通过
        self.assertFalse(params.light_is_event(40, 62, 0.42, a))  # 跳变但不同向

    def test_fingerprint_stable_and_sensitive(self):
        a = params.fingerprint(self.prof)
        self.assertEqual(a, params.fingerprint(self.prof))
        # 键顺序不同不影响
        reordered = params.json_copy(self.prof)
        prof2 = {"masks": reordered["masks"], "encode": reordered["encode"],
                 "enabled": reordered["enabled"], "name": reordered["name"],
                 "analysis": reordered["analysis"]}
        self.assertEqual(a, params.fingerprint(prof2))
        # 改判据必须变
        prof3 = params.new_profile("测试机位")
        prof3["analysis"]["noise_mult"] = 3.0
        self.assertNotEqual(a, params.fingerprint(prof3))
        # 改名字不能变（否则改个名就要重算一天）
        prof4 = params.new_profile("改了名字")
        self.assertEqual(a, params.fingerprint(prof4))

    def test_mask_area_guard(self):
        p = params.new_profile()
        p["masks"] = [{"x": 0, "y": 0, "w": 0.9, "h": 0.9}]
        ok, errs = params.validate(p)
        self.assertFalse(ok)
        self.assertTrue(any("50%" in e for e in errs))

    def test_range_guard(self):
        p = params.new_profile()
        p["analysis"]["fps"] = 40
        p["encode"]["day"]["quality"] = 99
        ok, errs = params.validate(p)
        self.assertFalse(ok)
        self.assertEqual(len(errs), 2)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.prof = params.new_profile()
        self.a = params.resolve(self.prof, "mono")

    def test_analysis_zero_copy(self):
        cmd = fc.analysis_cmd("ffmpeg", "in.mp4", self.a, hw="qsv")
        self.assertIn("-hwaccel_output_format", cmd)
        self.assertIn("qsv", cmd)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("vpp_qsv", vf)      # 缩放必须在显存里做
        self.assertIn("hwdownload", vf)   # 只回传小图
        self.assertIn("format=gray", vf)
        self.assertLess(vf.index("vpp_qsv"), vf.index("hwdownload"))
        self.assertIn("pipe:1", cmd)

    def test_analysis_software_fallback(self):
        cmd = fc.analysis_cmd("ffmpeg", "in.mp4", self.a, hw="none")
        self.assertNotIn("-hwaccel", cmd)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("scale=320:-2", vf)
        self.assertIn("fps=2", vf)

    def test_analysis_chroma_channel(self):
        cmd = fc.analysis_cmd("ffmpeg", "in.mp4", self.a, hw="qsv", channel="chroma")
        self.assertIn("extractplanes=u", cmd[cmd.index("-vf") + 1])

    def test_fast_mode_has_no_quality_params(self):
        enc = dict(self.prof["encode"], mode="fast")
        cmd = fc.encode_cmd("ffmpeg", "in.mp4", "out.mp4", enc, ss=10, to=40)
        self.assertIn("-c", cmd)
        self.assertIn("copy", cmd)
        for bad in ("-crf", "-global_quality", "-vf", "-c:v"):
            self.assertNotIn(bad, cmd)

    def test_fine_qsv_uses_global_quality(self):
        cmd = fc.encode_cmd("ffmpeg", "in.mp4", "out.mp4", self.prof["encode"], night=True)
        self.assertIn("hevc_qsv", cmd)
        self.assertIn("-global_quality", cmd)
        self.assertNotIn("-crf", cmd)
        self.assertEqual(cmd[cmd.index("-global_quality") + 1],
                         str(self.prof["encode"]["night"]["quality"]))
        self.assertIn("-an", cmd)   # 监控基本无音轨

    def test_fine_software_uses_crf(self):
        cmd = fc.encode_cmd("ffmpeg", "in.mp4", "out.mp4", self.prof["encode"], hw="none")
        self.assertIn("libx265", cmd)
        self.assertIn("-crf", cmd)
        self.assertIn("hvc1", cmd)   # 飞牛 App 播放器兼容

    def test_denoise_before_scale(self):
        # 夜间档才有缩放（75%），正好用来验证顺序：去噪必须在降帧与缩放之前
        cmd = fc.encode_cmd("ffmpeg", "in.mp4", "o.mp4", self.prof["encode"], night=True)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertLess(vf.index("hqdn3d"), vf.index("scale"))
        self.assertLess(vf.index("hqdn3d"), vf.index("fps"))

    def test_speed_pts_is_lossless(self):
        cmd = fc.speed_cmd("ffmpeg", "in.mp4", "o.mp4", 2, "pts")
        self.assertIn("-itsscale", cmd)
        self.assertEqual(cmd[cmd.index("-itsscale") + 1], "0.5")   # 2 倍速 = 时间戳乘 0.5
        self.assertIn("copy", cmd)

    def test_speed_reencode_drops_frames(self):
        cmd = fc.speed_cmd("ffmpeg", "in.mp4", "o.mp4", 2, "reencode")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("setpts=PTS/2", vf)
        self.assertIn("fps=", vf)
        self.assertIn("hevc_qsv", cmd)

    def test_prescan_does_not_decode(self):
        cmd = fc.prescan_cmd("ffprobe", "in.mp4")
        joined = " ".join(cmd)
        self.assertIn("packet=size,pts_time", joined)   # 容器层信息，不碰解码器
        self.assertNotIn("frame=", joined)
        self.assertIn("compact=p=0:nk=0", joined)       # 带字段名，解析不看字段顺序
        self.assertNotIn("-hwaccel", cmd)
        # 兜底命令才用 frame=，且同样带字段名
        self.assertIn("frame=pkt_size,pts_time", " ".join(fc.prescan_frame_cmd("ffprobe", "in.mp4")))


class TestLiveSnapshots(unittest.TestCase):
    """直播页默认只发光快照：年龄、卡住判定、宫格列数。"""

    def test_grid_cols(self):
        self.assertEqual(params.grid_cols(1), 1)
        self.assertEqual(params.grid_cols(2), 2)
        self.assertEqual(params.grid_cols(4), 2)    # 手机竖屏 2×2 每格约 195px
        self.assertEqual(params.grid_cols(6), 3)
        self.assertEqual(params.grid_cols(9), 3)
        self.assertEqual(params.grid_cols(16), 4)   # 封顶 4 列，再多看不清

    def test_live_payload_age_and_stale(self):
        import os
        import tempfile
        import time
        import nvr_slim

        var = tempfile.mkdtemp(prefix="nvrlive")
        snaps = os.path.join(var, "snapshots")
        os.makedirs(snaps)
        p = os.path.join(snaps, "cam-01.jpg")
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")
        old = time.time() - 26
        os.utime(p, (old, old))

        nvr_slim.STATE["var_dir"] = var
        data = nvr_slim.live_payload()
        cam1 = [c for c in data["cameras"] if c["id"] == "cam-01"][0]
        self.assertIn(cam1["age_sec"], (25, 26, 27))
        # 间隔 10 秒，超过 2.2 个周期没更新 → 标记卡住
        self.assertTrue(cam1["stale"])
        self.assertIn("?t=", cam1["snapshot"])      # 带时间戳，避免吃浏览器缓存
        # 没有快照的那几路 age=None 且算 stale
        no_snap = [c for c in data["cameras"] if c["id"] != "cam-01"][0]
        self.assertIsNone(no_snap["age_sec"])
        self.assertTrue(no_snap["stale"])
        self.assertEqual(data["grid_cols"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
