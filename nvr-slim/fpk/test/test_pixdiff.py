#!/usr/bin/env python3
"""0.9.22 像素差判读引擎的守卫测试。

为什么要专门测这条链：像素判读整条差分都在 ffmpeg 滤镜里，Python 只看到
"每帧一个数"。一旦滤镜串拼错、解析错位或者降级没生效，界面照样显示"判读正常"，
实际却整天判不出事件 —— 这种错误在真机上表现为"录了但没缩时"，最难查。
所以这里把三件事钉死：
  1. 滤镜串的形状（含忽略区 / 模糊 / 门槛的落点）
  2. metadata 输出的解析（含时间偏移）
  3. 任何一步不可用都必须**回落**到码流曲线并留下原因
"""

import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import condense      # noqa: E402
import nvr_slim      # noqa: E402
import params        # noqa: E402
import pixdiff       # noqa: E402


def _read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 滤镜串

class TestChain(unittest.TestCase):
    def test_chain_shape(self):
        c = pixdiff.build_chain({"fps": 2, "pixel_delta": 24, "blur": 2},
                                [(20, 40, 60, 20)])
        self.assertIn("fps=2", c)
        self.assertIn("scale=320:180", c)
        self.assertIn("format=gray", c)
        self.assertIn("drawbox=x=20:y=40:w=60:h=20", c)
        self.assertIn("boxblur=2:1", c)
        self.assertIn("tblend=all_mode=difference", c)
        # 门槛落在 lut 上 —— 这是"像素级"判定的关键，不能被整帧平均替代
        self.assertIn("lut=y='if(gt(val,24),255,0)'", c)
        self.assertIn("signalstats", c)
        self.assertIn("metadata=print:key=lavfi.signalstats.YAVG", c)
        # 差必须在缩放之后：1080p 全分辨率差分被压缩噪声压死
        self.assertLess(c.index("scale="), c.index("tblend="))

    def test_chain_clamps(self):
        c = pixdiff.build_chain({"fps": 999, "pixel_delta": -5, "blur": 9}, [])
        self.assertIn("fps=10", c)            # 10 封顶
        self.assertIn("gt(val,1)", c)         # 门槛至少 1
        self.assertIn("boxblur=3:1", c)       # 模糊半径至少 3
        self.assertNotIn("drawbox", c)        # 没有忽略区就不该出现

    def test_chain_no_blur_no_drawbox(self):
        c = pixdiff.build_chain({"fps": 1, "pixel_delta": 20, "blur": 0}, [])
        self.assertNotIn("boxblur", c)
        self.assertNotIn("drawbox", c)

    def test_scene_rects(self):
        # 2×2 格（每格 20×20）应合并成一个矩形，而不是四个
        rects = condense.cell_rects([0, 1, 16, 17])
        self.assertEqual(rects, [(0, 0, 40, 40)])
        self.assertEqual(condense.cell_rects([]), [])
        self.assertEqual(condense.cell_rects([9999]), [])


# ---------------------------------------------------------------- 解析

class TestParse(unittest.TestCase):
    SAMPLE = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.signalstats.YAVG=0.000000\n"
        "frame:2    pts:40      pts_time:0.400000\n"
        "lavfi.signalstats.YAVG=25.500000\n"
        "frame:4    pts:80      pts_time:0.800000\n"
        "lavfi.signalstats.YAVG=255.000000\n"
    )

    def test_parse_ratio(self):
        pts = pixdiff.parse(self.SAMPLE)
        self.assertEqual(len(pts), 3)
        self.assertAlmostEqual(pts[0][1], 0.0, places=6)
        self.assertAlmostEqual(pts[1][1], 0.1, places=3)   # 25.5/255
        self.assertAlmostEqual(pts[2][1], 1.0, places=6)

    def test_parse_offset(self):
        """分段读的时候时间戳是相对的，必须加上区间起点才是源时间。"""
        pts = pixdiff.parse(self.SAMPLE, offset=100.0)
        self.assertAlmostEqual(pts[1][0], 100.4, places=3)

    def test_parse_ignores_noise(self):
        junk = "some ffmpeg warning\nYAVG=9.0\nframe:9 pts_time:3.0\n"
        # 没有 pts 的 YAVG 不能凭空造点；有 pts 没 YAVG 也不能算一帧
        self.assertEqual(pixdiff.parse(junk), [])


# ---------------------------------------------------------------- 可用性 / 降级

class TestAvailability(unittest.TestCase):
    def test_no_ffmpeg(self):
        ok, why = pixdiff.available("")
        self.assertFalse(ok)
        self.assertIn("未找到 ffmpeg", why)

    def test_self_test_without_binary(self):
        r = pixdiff.self_test("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["frames"], 0)


class TestAnalyzeFallback(unittest.TestCase):
    def setUp(self):
        self._orig_curve = pixdiff.curve
        self._orig_avail = pixdiff.available
        self._orig_packets = condense.read_packets

    def tearDown(self):
        pixdiff.curve = self._orig_curve
        pixdiff.available = self._orig_avail
        condense.read_packets = self._orig_packets

    def _bytes_case(self):
        condense.read_packets = lambda *a, **k: ([(0.0, 500), (1.0, 500)], "")
        a = params.analysis(engine="bytes")
        return condense.analyze("ffprobe", "x.mp4", a, ffmpeg="/bin/ffmpeg")

    def test_engine_bytes_untouched(self):
        r = self._bytes_case()
        self.assertEqual(r["engine"], "bytes")
        self.assertEqual(r["engine_note"], "")

    def test_no_ffmpeg_falls_back(self):
        condense.read_packets = lambda *a, **k: ([(0.0, 500), (1.0, 500)], "")
        a = params.analysis(engine="pixel")
        r = condense.analyze("ffprobe", "x.mp4", a, ffmpeg=None)
        self.assertEqual(r["engine"], "bytes")
        self.assertIn("未找到 ffmpeg", r["engine_note"])

    def test_missing_filters_falls_back(self):
        pixdiff.available = lambda ff: (False, "ffmpeg 缺少滤镜：tblend")
        condense.read_packets = lambda *a, **k: ([(0.0, 500), (1.0, 500)], "")
        a = params.analysis(engine="auto")
        r = condense.analyze("ffprobe", "x.mp4", a, ffmpeg="/bin/ffmpeg")
        self.assertEqual(r["engine"], "bytes")
        self.assertIn("tblend", r["engine_note"])

    def test_pixel_failure_falls_back(self):
        pixdiff.available = lambda ff: (True, "")
        pixdiff.curve = lambda *a, **k: {"ok": False, "curve": [], "error": "像素判读无输出"}
        condense.read_packets = lambda *a, **k: ([(0.0, 500), (1.0, 500)], "")
        a = params.analysis(engine="pixel")
        r = condense.analyze("ffprobe", "x.mp4", a, ffmpeg="/bin/ffmpeg")
        self.assertEqual(r["engine"], "bytes")
        self.assertIn("像素判读失败", r["engine_note"])

    def test_pixel_path_windows_and_prescan(self):
        """像素路径：预筛出候选区间 → 只解这些区间 → 区间时间要能拼回源时间。"""
        captured = {}

        def fake_curve(ffmpeg, src, a, rects=None, ranges=None, timeout=300):
            captured["ranges"] = ranges
            captured["rects"] = rects
            captured["fps"] = a.get("fps")
            # 20~25 秒有人经过，其余全静（占比 0.0002 是噪声水平）
            pts = []
            for t in range(0, 60):
                pts.append((float(t), 0.02 if 20 <= t < 26 else 0.0002))
            return {"ok": True, "curve": pts, "error": ""}

        pixdiff.available = lambda ff: (True, "")
        pixdiff.curve = fake_curve
        # 预筛：第 20~26 秒字节数暴涨
        pk = [(float(t), 9000 if 20 <= t < 26 else 500) for t in range(0, 60)]
        condense.read_packets = lambda *a, **k: (pk, "")

        a = params.analysis(engine="pixel", min_event=2.0, merge_gap=2.0,
                            pre_buf=1.0, post_buf=1.0, prescan=True)
        r = condense.analyze("ffprobe", "x.mp4", a, ffmpeg="/bin/ffmpeg",
                             ignore=[0, 1])
        self.assertEqual(r["engine"], "pixel")
        self.assertTrue(r["windows"], "像素曲线明显有活动却没切出区间")
        s, e = r["windows"][0]
        self.assertLessEqual(s, 21.0)
        self.assertGreaterEqual(e, 25.0)
        # 预筛把解码限制在候选区间，不是整段
        self.assertTrue(captured["ranges"])
        self.assertLess(captured["ranges"][0][1], 60)
        # 忽略区以像素矩形下发给 drawbox
        self.assertTrue(captured["rects"])

    def test_pixel_empty_prescan_scans_full_at_1fps(self):
        """预筛无候选：仍要全段扫像素，但把抽样降到 1 帧/秒。"""
        captured = {}

        def fake_curve(ffmpeg, src, a, rects=None, ranges=None, timeout=300):
            captured["ranges"] = ranges
            captured["fps"] = a.get("fps")
            return {"ok": True, "curve": [(float(t), 0.0002) for t in range(0, 30)],
                    "error": ""}

        pixdiff.available = lambda ff: (True, "")
        pixdiff.curve = fake_curve
        condense.read_packets = lambda *a, **k: ([(float(t), 500) for t in range(30)], "")

        a = params.analysis(engine="auto", fps=2.0)
        r = condense.analyze("ffprobe", "x.mp4", a, ffmpeg="/bin/ffmpeg")
        self.assertEqual(r["engine"], "pixel")
        self.assertEqual(captured["ranges"], [(None, None)])
        self.assertEqual(captured["fps"], 1.0)
        self.assertTrue(r["silent"])


class TestPixelThreshold(unittest.TestCase):
    def test_threshold_uses_ratio_floor(self):
        """纯噪声（占比 1e-5）不该被当活动；0.5% 的变化一定要被抓住。"""
        a = params.resolve(params.new_profile("t"), "color")
        th = condense.pixel_threshold([(0.0, 0.00001), (1.0, 0.00002)], a)
        self.assertGreaterEqual(th, 0.0005)
        th2 = condense.pixel_threshold([(0.0, 0.05), (1.0, 0.06)], a)
        self.assertGreater(th2, th)          # 噪声大的机位门槛自动抬高
        wins = condense.pick_windows(
            [(0.0, 0.0001), (1.0, 0.01), (2.0, 0.01), (3.0, 0.0001), (4.0, 0.0001)],
            a, 5.0, th=0.002)
        self.assertEqual(len(wins), 1)

    def test_sparse_activity_not_swallowed(self):
        """稀疏活动的回归：非零值算分位 = 把活动当噪声，整段会被判成静止。

        这个 bug 真发生过：5 分钟里只有 10 秒有人走过时，非零值的 20 分位
        就是活动强度本身，乘倍率后门槛比活动还高，于是"人有走、片段没有"。
        """
        a = params.analysis(engine="pixel", noise_mult=2.0)
        curve = [(float(t), 0.0) for t in range(60)]
        for t in range(20, 26):
            curve[t] = (float(t), 0.0077)      # 3 秒活动 / 60 秒
        th = condense.pixel_threshold(curve, a)
        self.assertLess(th, 0.0077, "门槛高过活动强度：这 3 秒会被判成静止")
        wins = condense.pick_windows(curve, a, 60.0, th=th)
        self.assertEqual(len(wins), 1)
        self.assertLessEqual(wins[0][0], 20.5)
        self.assertGreaterEqual(wins[0][1], 25.0)

    def test_zero_baseline_uses_floor(self):
        """全零曲线（完全静止）→ 门槛就是绝对地板，不能变成 0 把噪声当事件。"""
        a = params.analysis(engine="pixel")
        th = condense.pixel_threshold([(float(t), 0.0) for t in range(30)], a)
        self.assertAlmostEqual(th, condense.PIXEL_FLOOR, places=6)

    def test_byte_threshold_mono_tolerance(self):
        """黑白/红外机位多给 25% 容噪：同一段字节曲线门槛更高。"""
        # 基线要给足（30KB/s）：低于 8KB 的地板时两边都被地板夹住，看不出差异
        curve = [(0.0, 30000), (1.0, 30000), (2.0, 90000), (3.0, 30000)]
        color = condense.threshold(curve, params.analysis(scene="color"))
        mono = condense.threshold(curve, params.analysis(scene="mono"))
        self.assertGreater(mono, color)
        self.assertAlmostEqual(mono / color, 1.25, places=3)


# ---------------------------------------------------------------- 参数落盘

class TestProfileFromJudge(unittest.TestCase):
    def test_mapping(self):
        p = nvr_slim.profile_from_judge("cam-01", "车库", {
            "engine": "pixel", "scene": "mono", "sens": "high",
            "pixel_delta": 24, "min_event": 2.0, "blur": 2,
            "fps": 2, "scene_probe_sec": 5, "merge_gap": 8,
        })
        a = p["analysis"]
        self.assertEqual(a["engine"], "pixel")
        self.assertEqual(a["scene"], "mono")
        self.assertEqual(a["sensitivity"], "high")
        self.assertAlmostEqual(a["noise_mult"], 1.6)   # 从档位推出来的倍率
        self.assertEqual(a["pixel_delta"], 24)
        self.assertAlmostEqual(a["min_event"], 2.0)
        self.assertAlmostEqual(a["min_clip"], 2.0)     # 最短事件同时管成品长度
        self.assertEqual(a["blur_mono"], 2)
        self.assertEqual(a["blur_color"], 2)
        self.assertAlmostEqual(a["merge_gap"], 8.0)

    def test_clamped_not_crashed(self):
        p = nvr_slim.profile_from_judge("cam-02", "x", {
            "engine": "telepathy", "scene": "sepia", "sens": "ultra",
            "pixel_delta": 999, "min_event": -3, "blur": 77, "fps": 1e9,
            "merge_gap": 1e9,
        })
        a = p["analysis"]
        self.assertEqual(a["engine"], "auto")
        self.assertEqual(a["scene"], "auto")
        self.assertEqual(a["sensitivity"], "mid")
        self.assertEqual(a["pixel_delta"], 80)
        self.assertAlmostEqual(a["min_event"], 0.5)
        self.assertEqual(a["blur_mono"], 3)
        self.assertAlmostEqual(a["fps"], 10.0)
        self.assertAlmostEqual(a["merge_gap"], 120.0)

    def test_profile_validates(self):
        p = nvr_slim.profile_from_judge("c", "n", {"sens": "low"})
        ok, errs = params.validate(p)
        self.assertTrue(ok, errs)

    def test_judge_survives_cameras_norm(self):
        """cameras 里临时挂的 judge 要能被 norm_cameras 带出来（再摘掉）。"""
        cams = nvr_slim.norm_cameras([{
            "id": "cam-01", "name": "车库", "url": "rtsp://x",
            "judge": {"sens": "low", "min_event": 3},
        }])
        self.assertIn("judge", cams[0])
        cams[0].pop("judge")
        self.assertNotIn("judge", cams[0])


# ---------------------------------------------------------------- 版本

class TestVersion(unittest.TestCase):
    def test_version(self):
        self.assertEqual(nvr_slim.runtime.VERSION, "0.9.73")
        m = _read(os.path.join(ROOT, "manifest"))
        self.assertRegex(m, r"(?m)^version\s*=\s*0\.9\.73\s*$")


# ---------------------------------------------------------------- 真机滤镜链

def _find_ffmpeg():
    """找一颗能用的 ffmpeg。找不到就跳过 —— 开发机/构建机未必装了。"""
    import shutil
    for cand in (os.environ.get("NVR_FFMPEG"), "ffmpeg"):
        if not cand:
            continue
        p = cand if os.path.isfile(cand) else shutil.which(cand)
        if p:
            return p
    return None


@unittest.skipUnless(_find_ffmpeg(), "本机没有 ffmpeg，跳过真实滤镜链测试")
class TestRealChain(unittest.TestCase):
    """在真 ffmpeg 上验证语义，而不是只验字符串。

    字符串对了不等于行为对：`lut` 的表达式、`tblend` 的首帧、`metadata` 的输出
    格式任何一处偏了，解析都会拿到空列表 —— 而空列表在业务里等于"整段静止"，
    是最危险的失败方式（静默丢内容）。所以只要机器上有 ffmpeg 就真的跑一遍。
    """

    @classmethod
    def setUpClass(cls):
        cls.ff = _find_ffmpeg()
        cls.tmp = os.path.join(ROOT, "test", ".pixdiff_tmp")
        os.makedirs(cls.tmp, exist_ok=True)
        cls.static = os.path.join(cls.tmp, "static.mp4")
        cls.moving = os.path.join(cls.tmp, "moving.mp4")
        cls._gen()

    @classmethod
    def _gen(cls):
        import subprocess
        common = ["-hide_banner", "-loglevel", "error", "-y"]
        base = "color=c=0x202020:s=640x360:r=15:d=20"
        # 静止：纯色画面
        subprocess.run([cls.ff] + common + ["-f", "lavfi", "-i", base,
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", cls.static],
                       capture_output=True, timeout=180)
        # 运动：5~10 秒有一个白块横向穿过
        subprocess.run([cls.ff] + common + [
            "-f", "lavfi", "-i", base,
            "-f", "lavfi", "-i", "color=c=white:s=60x60:r=15:d=20",
            "-filter_complex",
            "[0][1]overlay=x='if(between(t,5,10),140+(t-5)*20,-300)':y=150",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", cls.moving],
            capture_output=True, timeout=180)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _a(self):
        return params.analysis(engine="pixel", prescan=False, min_event=1.0,
                               merge_gap=1.0, pre_buf=0.5, post_buf=0.5)

    def test_self_test_passes(self):
        r = pixdiff.self_test(self.ff)
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["frames"], 0)

    def test_static_is_silent(self):
        r = condense.analyze("", self.static, self._a(), ffmpeg=self.ff)
        self.assertEqual(r["engine"], "pixel")
        self.assertTrue(r["silent"])

    def test_motion_window(self):
        r = condense.analyze("", self.moving, self._a(), ffmpeg=self.ff)
        self.assertEqual(r["engine"], "pixel")
        self.assertTrue(r["windows"], "5~10 秒有白块穿过却没切出区间")
        s, e = r["windows"][0]
        self.assertLess(s, 5.5)
        self.assertGreater(e, 9.5)

    def test_ignore_region_masks_motion(self):
        """白块全程在忽略区里 → 必须判成静止（涂黑后差分恒为 0）。"""
        cells = [r * 16 + c for r in (3, 4, 5) for c in range(2, 8)]
        r = condense.analyze("", self.moving, self._a(), ffmpeg=self.ff,
                             ignore=cells)
        self.assertTrue(r["silent"], "忽略区没起作用：%s" % (r["windows"],))
        self.assertEqual(r["ignored_regions"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
