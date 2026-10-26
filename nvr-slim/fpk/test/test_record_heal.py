#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制自愈：探测耐心、参数黑名单、错误分类、脱敏、CPU 采样初始化。

这几条都是 0.9.14 真机故障（`/vol1/@appshare/nvr-output` 上五路全报
"Output file does not contain any stream"）直接暴露出来的：

  · 摄像头建连后要 1~3 秒才吐第一个 IDR，默认 probe 太急 → 输出侧 0 条流
  · 精简 ffmpeg 认不认某个参数无法预知，只能听它自己说
  · 光有 ffmpeg 的英文原文，用户不知道该改什么
  · sysinfo._TICK 初始化少一个字段，让 /api/status 每 3 秒 500 一次

跑：python test/test_record_heal.py
"""
import os
import json
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "app", "server")
sys.path.insert(0, SERVER)

import recorder   # noqa: E402
import sysinfo    # noqa: E402

URL = "rtsp://admin:s3cr3t@192.0.2.37:554/MPEG-4/ch1/main/av_stream"
PAT = "/data/%Y-%m-%d/%H-%M-%S.mp4.part"


class TestRecordCmd(unittest.TestCase):
    def test_no_timeout_flags_by_default(self):
        """0.9.11 的 -rw_timeout、0.9.13 的 -timeout 都是这么挂的。"""
        cmd = recorder.record_cmd("ffmpeg", URL, PAT)
        for bad in ("-timeout", "-rw_timeout", "-stimeout"):
            self.assertNotIn(bad, cmd, "录制命令里不能出现 %s" % bad)

    def test_opts_go_before_input(self):
        """-analyzeduration / -probesize 是**输入**选项，放到 -i 之后无效。"""
        cmd = recorder.record_cmd("ffmpeg", URL, PAT, opts=recorder.INPUT_OPTS)
        i = cmd.index("-i")
        for name, _v in recorder.INPUT_OPTS:
            self.assertIn(name, cmd)
            self.assertLess(cmd.index(name), i,
                            "%s 必须出现在 -i 之前才会生效" % name)

    def test_probe_patience_values(self):
        """5 秒 / 10MB：家用摄像头吐第一个关键帧通常 1~3 秒，留足余量。

        0.9.84 把 analyzeduration 从 10 秒收到 5 秒：探测期一个字节都不产出，
        静默频繁的机位每轮重连都白扔这 10 秒（95 秒周期里占 8%）。5 秒仍是
        吐帧实测 1~3 秒的近两倍余量；probesize 不动 —— 它只在码率异常时才
        触发提前结束。
        """
        d = dict(recorder.INPUT_OPTS)
        self.assertEqual(int(d["-analyzeduration"]), 5 * 1000 * 1000)
        self.assertEqual(int(d["-probesize"]), 10 * 1000 * 1000)

    def test_copy_never_reencodes(self):
        """录像层不许出现编码器：一插 libx264 每路就常驻一个编码进程。"""
        cmd = recorder.record_cmd("ffmpeg", URL, PAT, opts=recorder.INPUT_OPTS)
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("libx264", cmd)
        self.assertNotIn("drawbox", cmd)


class TestBadOptAbsorb(unittest.TestCase):
    def test_parse_unrecognized(self):
        self.assertEqual(
            recorder.parse_bad_option(
                "[AVFormatContext @ 0x1] Unrecognized option 'analyzeduration'."),
            "analyzeduration")
        self.assertEqual(
            recorder.parse_bad_option("Option 'probesize' not found."),
            "probesize")

    def test_parse_garbage(self):
        self.assertIsNone(recorder.parse_bad_option(""))
        self.assertIsNone(recorder.parse_bad_option("some unrelated error"))

    def test_eff_opts_drops_bad(self):
        r = recorder.Recorder("ffmpeg", "/tmp/x", [], log=lambda m: None)
        self.assertEqual(len(r.eff_opts()), len(recorder.INPUT_OPTS))
        r.bad_opts.add("-analyzeduration")
        names = [o[0] for o in r.eff_opts()]
        self.assertNotIn("-analyzeduration", names)
        self.assertIn("-probesize", names)     # 只删不认的那一个

    def test_absorb_only_known(self):
        """用户地址写错时报的其他选项，不能被我们悄悄吞掉。"""
        r = recorder.Recorder("ffmpeg", "/tmp/x", [], log=lambda m: None)
        self.assertIsNone(r._absorb_bad_opts("Unrecognized option 'frobnicate'."))
        self.assertEqual(r.bad_opts, set())
        self.assertEqual(
            r._absorb_bad_opts("[x] Unrecognized option 'probesize'."), "probesize")
        self.assertIn("probesize", r.bad_opts)

    def test_absorb_is_idempotent(self):
        r = recorder.Recorder("ffmpeg", "/tmp/x", [], log=lambda m: None)
        r._absorb_bad_opts("Unrecognized option 'probesize'.")
        self.assertIsNone(r._absorb_bad_opts("Unrecognized option 'probesize'."))


class TestClassify(unittest.TestCase):
    CASES = [
        ("[out#0/segment] Output file does not contain any stream", "no_stream"),
        ("[AVFormatContext] Unrecognized option 'analyzeduration'.", "badopt"),
        ("Could not find tag for codec hevc in stream #0", "codec"),
        ("[in#0] method SETUP failed: 500 (Internal Server Error)", "rtsp_reject"),
        ("[rtsp] method DESCRIBE failed: 401 Unauthorized", "rtsp_reject"),
        ("[segment] Error opening output file /x/y.mp4.part", "output"),
        ("av_interleaved_write_frame(): No space left on device", "disk_full"),
        ("Connection refused", "net"),
    ]

    def test_kinds(self):
        for err, want in self.CASES:
            self.assertEqual(recorder.classify_error(err)[0], want, err)

    def test_every_kind_has_hint(self):
        """kind 是给程序判断的，hint 是给人看的；两者必须都能拿到。"""
        for err, want in self.CASES:
            kind, hint = recorder.classify_error(err)
            self.assertEqual(recorder.hint_of(kind), hint)
            self.assertTrue(hint, "%s 没有给用户任何建议" % kind)

    def test_unknown(self):
        self.assertEqual(recorder.classify_error(""), ("other", ""))
        self.assertEqual(recorder.hint_of("other"), "")
        self.assertEqual(recorder.hint_of(None), "")

    def test_no_stream_wins_over_output(self):
        """真机是两条一起出现 —— 必须先判 no_stream，否则会被归成"目录问题"。

        原始报错三行：
            Output file does not contain any stream
            Error opening output file /vol1/.../%Y-%m-%d/%H-%M-%S.mp4.part
            Error opening output files: Invalid argument
        如果按 output 归类，用户会被引导去查目录权限，而真正的原因在码流。
        """
        three = ("[out#0/segment] Output file does not contain any stream\n"
                 "Error opening output file /vol1/x/%Y-%m-%d/%H-%M-%S.mp4.part.\n"
                 "Error opening output files: Invalid argument")
        self.assertEqual(recorder.classify_error(three)[0], "no_stream")


class TestMask(unittest.TestCase):
    def test_password_masked(self):
        out = recorder._mask_url(URL)
        self.assertNotIn("s3cr3t", out)
        self.assertIn("192.0.2.37", out)        # 地址要留着，诊断要看是哪台
        self.assertIn("/MPEG-4/ch1/main/av_stream", out)
        self.assertEqual(out, "rtsp://admin:***@192.0.2.37:554/MPEG-4/ch1/main/av_stream")

    def test_plain_path_untouched(self):
        self.assertEqual(recorder._mask_url("/bin/ffmpeg"), "/bin/ffmpeg")
        self.assertEqual(recorder._mask_url("-i"), "-i")


class TestRetryPolicy(unittest.TestCase):
    def test_backoff_cap_is_generous(self):
        """封顶 30 秒时，5 路全挂每 6 秒就有一个进程起来再死，日志没法看。"""
        self.assertGreaterEqual(recorder.MAX_BACKOFF, 300)

    def test_stall_watchdog_threshold(self):
        self.assertGreaterEqual(recorder.STALL_SEC, 60)


class _FakeProc:
    """替身进程：poll() 立刻返回退出码，stderr.read() 给出准备好的报错。"""

    def __init__(self, err, rc=1, pid=4321):
        self._err = err.encode("utf-8")
        self._rc = rc
        self.pid = pid

    def poll(self):
        return self._rc

    @property
    def stderr(self):
        import io
        return io.BytesIO(self._err)


class TestTickHealsBadOption(unittest.TestCase):
    """真机回归：ffmpeg 报 Unrecognized option 时，要立刻换命令重试而不是等退避。

    0.9.14 的现场是五路每 2 秒死一次、一路退避到 30 秒，日志刷了几百行。
    这里把「一条带不认参数的进程刚死」这一瞬间搭出来，验证 tick 的处理：
      · 该选项进黑名单      → 下次命令行自动不带它
      · next 归零           → 下一轮巡检立刻重起，不干等
      · tries 不涨          → 这不是摄像头的错，不能算失败次数
    """

    def _mk(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="nvr-heal-")
        msgs = []
        r = recorder.Recorder("ffmpeg", tmp, [], log=msgs.append, var_dir=tmp)
        cam = {"id": "c1", "name": "客厅", "url": URL}
        r.cams = [cam]
        return r, cam, msgs

    def test_absorb_then_retry_immediately(self):
        r, cam, msgs = self._mk()
        err = "[AVFormatContext @ 0x1] Unrecognized option 'analyzeduration'."
        r.procs["c1"] = {"p": _FakeProc(err), "day": "2026-10-21", "tries": 3,
                         "next": 0, "started_at": 0, "dead": None,
                         "sig": r._sig(cam)}
        r.tick()
        self.assertIn("analyzeduration", r.bad_opts)
        self.assertEqual(r.procs["c1"]["next"], 0, "必须立刻重试，不能等退避")
        self.assertLessEqual(r.procs["c1"]["tries"], 3, "参数问题不该累积成失败次数")
        names = [o[0] for o in r.eff_opts()]
        self.assertNotIn("-analyzeduration", names)
        self.assertIn("-probesize", names)

    def test_real_failure_still_backs_off(self):
        """码流本身有问题时照常退避，别把自愈当成万能。"""
        r, cam, msgs = self._mk()
        err = ("[out#0/segment] Output file does not contain any stream\n"
               "Error opening output files: Invalid argument")
        r.procs["c1"] = {"p": _FakeProc(err), "day": "2026-10-21", "tries": 0,
                         "next": 0, "started_at": 0, "dead": None,
                         "sig": r._sig(cam)}
        r.tick()
        st = r.procs["c1"]
        self.assertEqual(st["tries"], 1)
        self.assertGreater(st["next"], 0, "真失败要退避")
        self.assertEqual(r.last_kind["c1"], "no_stream")
        self.assertEqual(r.bad_opts, set())

    def test_diag_file_written(self):
        """诊断要落盘：日志只有摘要，排查需要完整的 Stream mapping。"""
        import os
        r, cam, msgs = self._mk()
        err = "[out#0/segment] Output file does not contain any stream"
        r.procs["c1"] = {"p": _FakeProc(err), "day": "2026-10-21", "tries": 0,
                         "next": 0, "started_at": 0, "dead": None,
                         "sig": r._sig(cam)}
        r.tick()
        f = os.path.join(r._diag_dir, "c1.log")
        self.assertTrue(os.path.isfile(f), "没写出诊断文件")
        with open(f, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("does not contain any stream", body)
        self.assertIn("# cmd:", body)
        self.assertNotIn("s3cr3t", body, "诊断文件里不能有 RTSP 密码")


class TestLogDedup(unittest.TestCase):
    def test_same_error_not_flooded(self):
        msgs = []
        r = recorder.Recorder("ffmpeg", "/tmp/x", [], log=msgs.append)
        for _ in range(6):
            r._log_dedup("c1", "录制 客厅 中断：同一件事")
        self.assertEqual(len(msgs), 3, "同一错误只打前三次")

    def test_every_twentieth_reported(self):
        msgs = []
        r = recorder.Recorder("ffmpeg", "/tmp/x", [], log=msgs.append)
        for _ in range(20):
            r._log_dedup("c1", "同一件事")
        self.assertEqual(len(msgs), 4)          # 3 次 + 第 20 次的一条汇总
        self.assertIn("已重复 20 次", msgs[-1])

    def test_different_error_resets(self):
        msgs = []
        r = recorder.Recorder("ffmpeg", "/tmp/x", [], log=msgs.append)
        for _ in range(4):
            r._log_dedup("c1", "错误 A")
        r._log_dedup("c1", "错误 B")
        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[-1], "错误 B")


class TestCpuTickInit(unittest.TestCase):
    """sysinfo._TICK 少一个字段 → /api/status 每 3 秒 IndexError，永不恢复。

    回归点有两个：初始值长度要对，且**首次调用**不能抛。
    第二个尤其重要 —— 原来的写法把赋值放在下标访问之后，异常一抛就永远
    执行不到，于是每次都抛同一个错。
    """

    def test_initial_length_is_three(self):
        # 新开进程读模块初始值，避免被同进程里的其他测试改过
        code = "import sys; sys.path.insert(0, %r); import sysinfo; print(len(sysinfo._TICK))" % SERVER
        out = subprocess.check_output([sys.executable, "-c", code],
                                      stderr=subprocess.STDOUT).decode().strip()
        self.assertEqual(out, "3", "_TICK 必须是 (时刻, busy, total) 三元组")

    def test_first_call_does_not_raise(self):
        # 非 Linux 上 /proc/stat 不存在，_cpu_total() 直接返回 None，
        # sys_cpu_pct() 会在碰 _TICK 之前就 return —— 那样这条断言等于空转，
        # 正是"测试验证了一个假实现"。这里把 _cpu_total 打成固定值，
        # 强制走到真正出过错的那几行。
        #
        # 另外必须 reload：同进程里别的用例可能已经把 _TICK 改过，
        # 只有 reload 才能拿到源码里的**真实初值** —— 否则初值写成 2 元组
        # 这个 bug 就测不出来（试过，reload 之前确实漏掉了）。
        import importlib
        importlib.reload(sysinfo)
        orig = sysinfo._cpu_total
        self.assertEqual(len(sysinfo._TICK), 3, "reload 后应是源码里的初始值")
        sysinfo._cpu_total = lambda: (250, 1000)
        try:
            v = sysinfo.sys_cpu_pct()
        finally:
            sysinfo._cpu_total = orig
        self.assertIsNotNone(v, "首次采样就该有值，不该抛 IndexError")
        self.assertAlmostEqual(v, 25.0, places=1)

    def test_repeated_calls(self):
        orig = sysinfo._cpu_total
        seq = [(250, 1000), (300, 1200), (330, 1400)]
        it = iter(seq)
        sysinfo._TICK = (0.0, 0.0, 0.0)
        sysinfo._cpu_total = lambda: next(it)
        try:
            vals = [sysinfo.sys_cpu_pct() for _ in seq]
        finally:
            sysinfo._cpu_total = orig
        self.assertTrue(all(v is not None for v in vals), vals)
        # 第二次：增量 50/200 = 25%；第三次：30/200 = 15%
        self.assertAlmostEqual(vals[1], 25.0, places=1)
        self.assertAlmostEqual(vals[2], 15.0, places=1)

    def test_snapshot_survives(self):
        """/api/status 真正调的是 snapshot()，它不能抛。"""
        orig = sysinfo._cpu_total
        sysinfo._TICK = (0.0, 0.0, 0.0)
        sysinfo._cpu_total = lambda: (100, 400)
        try:
            info = sysinfo.snapshot(None, record_dir=None)
        finally:
            sysinfo._cpu_total = orig
        for k in ("proc_cpu", "sys_cpu", "mem_mb", "disk_free_gb", "hw"):
            self.assertIn(k, info)
        self.assertIsNotNone(info["sys_cpu"])


class TestExplicitMap(unittest.TestCase):
    """0.9.17 真根因：`.part` 后缀让 ffmpeg 的自动映射猜出 0 条流。

    segment muxer 不声明任何编码，av_guess_codec 对 segment/ssegment 有特判：
    按输出**文件名扩展名**重新猜封装（av_guess_format）。`.mp4` 猜得到 →
    正常映射；`.part` 猜不出 → video_codec=NONE → 视频不被映射、-an 挡掉
    音频 → "Output file does not contain any stream" + EINVAL。
    从 0.9.8 到 0.9.16 的"一条录像都没有"全是它 —— 快照/预览走别的命令，
    所以看着正常；0.9.15 的探测耐心修复只是碰巧在试录里用了 .mp4 后缀。
    显式 -map 完全绕开这条猜测路径。
    """

    def test_explicit_map_present_and_positioned(self):
        cmd = recorder.record_cmd("ffmpeg", URL, PAT)
        self.assertIn("-map", cmd)
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:v:0")
        i, ci = cmd.index("-map"), cmd.index("-i")
        self.assertGreater(i, ci, "-map 是输出选项，必须放在 -i 之后")
        self.assertLess(i, cmd.index("-c"), "-map 要在 -c copy 之前")

    def test_opts_stay_before_input(self):
        """INPUT_OPTS（探测耐心）仍在 -i 之前，与 -map 互不干扰。"""
        cmd = recorder.record_cmd("ffmpeg", URL, PAT, opts=recorder.INPUT_OPTS)
        self.assertIn("-analyzeduration", cmd[:cmd.index("-i")])
        self.assertIn("-map", cmd[cmd.index("-i"):])

    def test_matches_no_streams_classified(self):
        """显式 -map 后，摄像头真没给视频流时的报错也要说人话。"""
        kind, hint = recorder.classify_error(
            "Stream map '0:v:0' matches no streams.")
        self.assertEqual(kind, "no_video")
        self.assertIn("通道号", hint)


class TestProbeTrialRealPath(unittest.TestCase):
    """0.9.16：试录必须走正式录制的真实路径。

    0.9.15 真机的教训：试录写到 var/probe/ 成功了，5 路正式录制却全灭 ——
    两者唯一差异是输出路径，而旧试录**证明不了它**。现在试录直接用
    真实根目录 + 日期子目录 + .part 后缀 + 真实分片长度，
    「试录成功」才等于整条链路可用。
    """

    def _cam(self):
        return {"id": "c1", "name": "前院", "dir_name": "前院", "url": URL}

    def _fake_run(self, created, rc=0, err=b"", write=True):
        import datetime
        from unittest import mock
        from types import SimpleNamespace

        def run(cmd, **kw):
            created["cmd"] = cmd
            if "-show_streams" in cmd:      # ffprobe 桩：报一条 h264 视频流
                js = json.dumps({"streams": [
                    {"codec_type": "video", "codec_name": "h264",
                     "width": 1920, "height": 1080, "avg_frame_rate": "25/1"}],
                    "format": {"format_name": "rtsp"}}).encode()
                return SimpleNamespace(returncode=0, stderr=b"", stdout=js)
            if write:
                target = datetime.datetime.now().strftime(cmd[-1])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(b"x" * 4096)      # 超过 trial_ok 的 2048 门槛
            return SimpleNamespace(returncode=rc, stderr=err, stdout=b"")

        return mock.patch.object(subprocess, "run", side_effect=run)

    def test_trial_targets_real_dir_and_cleans_up(self):
        import tempfile
        import nvr_slim
        root = tempfile.mkdtemp(prefix="nvr-probe-")
        created = {}
        with self._fake_run(created):
            rep = nvr_slim._probe_one(self._cam(), "ffmpeg", "",
                                      root=root, seg_sec=120)
        cmd = created["cmd"]
        out = cmd[-1]
        # 输出落在真实根目录下，带日期子目录和 .part 后缀
        self.assertTrue(out.startswith(root), out)
        self.assertIn("%Y-%m-%d", out)
        self.assertIn("__probe__-%H-%M-%S.mp4.part", out)
        # 真实分片长度，不是诊断写死的 60
        self.assertEqual(cmd[cmd.index("-segment_time") + 1], "120")
        # 输入选项与正式录制一致，且 -v info / -t 8 只为诊断服务
        self.assertIn("-analyzeduration", cmd[:cmd.index("-i")])
        self.assertIn("-t", cmd)
        self.assertEqual(cmd[cmd.index("-v") + 1], "info")
        self.assertTrue(rep["trial_ok"], rep)
        self.assertGreater(rep["trial_files"], 0)
        # 试录产物当场清掉，不留"诊断碎片"在录像时间线里
        day_dir = recorder.cam_dir(root, "前院")
        leftovers = [n for n in os.listdir(day_dir)
                     if n.startswith("__probe__-")]
        self.assertEqual(leftovers, [])

    def test_unwritable_dir_reports_dir_kind(self):
        import tempfile
        import nvr_slim
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".lock")
        f.close()
        try:
            rep = nvr_slim._probe_one(self._cam(), "ffmpeg", "",
                                      root=os.path.join(f.name, "x"))
            self.assertEqual(rep["kind"], "dir")
            self.assertIn("不可写", rep["trial_err"])
            self.assertFalse(rep["trial_ok"])
        finally:
            os.unlink(f.name)

    def test_failure_verdict_uses_classified_hint(self):
        import tempfile
        import nvr_slim
        root = tempfile.mkdtemp(prefix="nvr-probe-")
        err = b"[out#0/segment] Output file does not contain any stream"
        # fp 传个名字，让 ffprobe 桩跑起来 —— 真机上就是"ffprobe 看得到视频流、
        # 录制却映射不出"这个组合，提示应指向换子码流而不是核对通道号
        with self._fake_run({}, rc=1, err=err, write=False):
            rep = nvr_slim._probe_one(self._cam(), "ffmpeg", "ffprobe",
                                      root=root, seg_sec=60)
        self.assertFalse(rep["trial_ok"])
        self.assertEqual(rep["kind"], "no_stream")
        self.assertTrue(any(s["type"] == "video" for s in rep["streams"]))
        self.assertIn("子码流", rep["hint"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
