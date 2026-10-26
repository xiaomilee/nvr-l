#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一取流（0.9.80）：取流广播 / 取帧切图 / 录制与检测的输入地址。

这次改动的动机是一个可数的量：一路摄像头被三处各自拉流，5 路每 5 秒就是
**120 次新建 RTSP 会话**。Frigate 的排查文档把「同时拉流的路数」列在摄像头
掉线的第一梯队原因里，并建议所有角色共用一路 restream。

测试重点不是"能不能跑通"，而是几条**安全边界**：
  · 慢消费者不许阻塞取流（队列有界、丢最旧）
  · 取流没起来时录制必须退回直连（宁可多一条连接，不能一条录像都没有）
  · 非 RTSP 输入不许带 -rtsp_transport（精简 ffmpeg 会直接退出）
  · MJPEG 切帧不许把半帧写出去（预览会显示半张图）
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "server"))

import detect    # noqa: E402
import frames    # noqa: E402
import ingest    # noqa: E402
import recorder  # noqa: E402

RTSPS = "rtsp://192.0.2.10/live"
TCPURL = "tcp://127.0.0.1:39001?listen=0"


class _Proc:
    """替身 ffmpeg 进程。alive=True 时 poll() 返回 None。"""

    def __init__(self, pid=1234, alive=True, rc=None):
        self.pid = pid
        self._alive = alive
        self._rc = rc if rc is not None else 0
        self.signals = []
        self.killed = False

    def poll(self):
        return None if self._alive else self._rc

    def send_signal(self, s):
        self.signals.append(s)
        self._alive = False          # SIGINT 就够了：真 ffmpeg 会干净退出

    def wait(self, timeout=None):
        return self._rc

    def kill(self):
        self.killed = True
        self._alive = False


def _cam(cid="c1", name="客厅", url=RTSPS):
    return {"id": cid, "name": name, "url": url, "dir_name": name}


# ---------------------------------------------------------------- 切帧

class TestJpegSplit(unittest.TestCase):
    def test_two_frames(self):
        got = []
        a, b = b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BBB\xff\xd9"
        tail = frames.jpeg_split(a + b, got.append)
        self.assertEqual(got, [a, b])
        self.assertEqual(tail, b"")

    def test_partial_tail_kept(self):
        got = []
        data = b"\xff\xd8AAA\xff\xd9" + b"\xff\xd8BB"
        tail = frames.jpeg_split(data, got.append)
        self.assertEqual(len(got), 1)
        self.assertEqual(tail, b"\xff\xd8BB", "半帧必须留着等下一次 read")

    def test_skips_garbage_before_soi(self):
        got = []
        frames.jpeg_split(b"junk\n\xff\xd8X\xff\xd9", got.append)
        self.assertEqual(got, [b"\xff\xd8X\xff\xd9"])

    def test_no_soi_returns_empty(self):
        self.assertEqual(frames.jpeg_split(b"nope", lambda d: None), b"")


class TestFrameTapWrite(unittest.TestCase):
    def test_atomic_write_and_age(self):
        tmp = tempfile.mkdtemp(prefix="tap-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        out = os.path.join(tmp, "snaps", "c1.jpg")
        tap = frames.FrameTap("ffmpeg", "c1", lambda: TCPURL, out, interval=5)
        self.assertIsNone(tap.path_age())
        tap._write(b"\xff\xd8DATA\xff\xd9")
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8DATA\xff\xd9")
        self.assertFalse(os.path.exists(out + ".part"), "临时文件必须被 replace 掉")
        self.assertIsNotNone(tap.path_age())

    def test_cmd_has_skip_frame_until_degraded(self):
        tap = frames.FrameTap("ffmpeg", "c1", lambda: TCPURL, "/tmp/x.jpg", interval=5)
        self.assertIn("-skip_frame", tap._cmd(TCPURL))
        tap._no_skip = True
        self.assertNotIn("-skip_frame", tap._cmd(TCPURL),
                         "GOP 太长的摄像头要靠全帧解码兜底")

    def test_cmd_rate_comes_from_interval(self):
        tap = frames.FrameTap("ffmpeg", "c1", lambda: TCPURL, "/tmp/x.jpg", interval=7)
        cmd = tap._cmd(TCPURL)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("fps=1/7", vf)
        self.assertIn("scale=-2:720", vf, "预览/AI 共用一张图，要缩到 720p")


# ---------------------------------------------------------------- 广播

class TestBroadcast(unittest.TestCase):
    def _ing(self):
        ing = ingest.Ingest("ffmpeg", _cam(), log=lambda m: None)
        ing.port = 39001                    # 不真开端口，测试只关心广播语义
        return ing

    def test_backlog_drops_oldest(self):
        ing = self._ing()
        q = __import__("collections").deque()
        ing._clients = [{"sock": None, "q": q, "gone": False}]
        for i in range(ingest.BACKLOG + 10):
            ing._broadcast(bytes([i % 256]))
        self.assertEqual(len(q), ingest.BACKLOG, "队列必须封顶，否则内存无上限")
        self.assertEqual(q[-1], bytes([(ingest.BACKLOG + 9) % 256]), "保留最新的")

    def test_dead_client_is_skipped(self):
        ing = self._ing()
        q = __import__("collections").deque()
        ing._clients = [{"sock": None, "q": q, "gone": True}]
        ing._broadcast(b"x")
        self.assertEqual(len(q), 0)

    def test_url_shape(self):
        ing = self._ing()
        self.assertEqual(ing.url, TCPURL)
        ing.port = 0
        self.assertIsNone(ing.url, "端口还没开就必须返回 None，让调用方退回直连")


class TestIngestWatchdog(unittest.TestCase):
    def _ing(self, proc):
        ing = ingest.Ingest("ffmpeg", _cam(), log=lambda m: None)
        ing.port = 39001
        ing.proc = proc
        ing.started_at = 1000.0
        return ing

    def test_handshake_failure_restarts_fast(self):
        """从 spawn 到现在一个字节都没有 —— 这种自己好不了，必须快杀。"""
        ing = self._ing(_Proc())
        ing.tick(now=1000.0 + ing.stall_sec + 1)
        self.assertIsNone(ing.proc, "握手失败必须先收掉进程")
        self.assertEqual(ing.stall, 1)
        self.assertLessEqual(ing.next_at - (1000.0 + ing.stall_sec + 1),
                             ingest.MAX_BACKOFF)
        self.assertIn("握手", ing.last_error)

    def test_never_wrote_is_reported(self):
        ing = self._ing(_Proc())
        ing.tick(now=1000.0 + ing.stall_sec + 1)
        self.assertIn("没吐过数据", ing.last_error)

    def test_dead_proc_backs_off(self):
        ing = self._ing(_Proc(alive=False, rc=254))
        ing.tick(now=1100.0)
        self.assertEqual(ing.tries, 1)
        self.assertGreater(ing.next_at, 1100.0, "崩溃要退避，不能立刻重起")
        self.assertEqual(ing.state, "reconnecting")

    def test_healthy_does_nothing(self):
        ing = self._ing(_Proc())
        ing.last_byte = 1099.0
        ing.tick(now=1100.0)
        self.assertIsNotNone(ing.proc, "数据一直在流就不能动它")
        self.assertEqual(ing.stall, 0)


class TestIngestManager(unittest.TestCase):
    def test_url_for_unknown_is_none(self):
        m = ingest.IngestManager("ffmpeg", lambda: [], enabled=False)
        self.assertIsNone(m.url_for("nope"))

    def test_disabled_starts_nothing(self):
        m = ingest.IngestManager("ffmpeg", lambda: [_cam()], enabled=False)
        m.start()
        self.assertEqual(m.items, {}, "关掉取流就不许起任何进程")

    def test_key_change_trigger(self):
        ing = ingest.Ingest("ffmpeg", _cam(), log=lambda m: None)
        self.assertFalse(ing._key_changed(_cam()))
        self.assertTrue(ing._key_changed(_cam(url="rtsp://192.0.2.11/x")))

    # ---- starved_of：录制层问「上游现在给不出数据吗」（0.9.82）----
    #
    # 这个返回值决定录制看门狗让不让路。判错方向的代价很实在：说「上游有数据」
    # 而其实没有 → 录制层把相机缺失记成自己卡住，白重启一轮（09:33 现场，
    # 140 秒空洞）；说「上游没数据」而其实有 → 录制真的卡住却没人管，整段丢。
    # 所以四种情形都要钉住，尤其**不能只判 `_paused` 一个标志位**。

    def _mg_with(self, **kw):
        m = ingest.IngestManager("ffmpeg", lambda: [_cam()], enabled=False)
        ing = ingest.Ingest("ffmpeg", _cam(), log=lambda x: None)
        for k, v in kw.items():
            setattr(ing, k, v)
        m.items[_cam()["id"]] = ing
        return m, ing

    def test_starved_unknown_cam(self):
        m = ingest.IngestManager("ffmpeg", lambda: [], enabled=False)
        self.assertFalse(m.starved_of("nope"), "没有这一路就别让路")

    def test_starved_while_paused(self):
        import time
        m, ing = self._mg_with(proc=_Proc(), last_byte=time.time(),
                               _paused=True)
        self.assertTrue(m.starved_of(_cam()["id"]))

    def test_not_starved_when_data_flowing(self):
        """上游一直在吐字节 → 不让路，录制真卡住必须被管。"""
        import time
        m, ing = self._mg_with(proc=_Proc(), last_byte=time.time(), _paused=False)
        self.assertFalse(m.starved_of(_cam()["id"]))

    def test_starved_when_long_idle_even_if_flag_clear(self):
        """标志位已清但很久没有字节（认命重连的退避/探测期）→ 也要让路。

        这正是只判 `_paused` 会漏的窗口：`_begin_close` 一调用，`_paused`
        就被清掉，可接下来 5 秒退避 + 10 秒探测里一个字节都不会来。
        """
        import time
        m, ing = self._mg_with(proc=_Proc(),
                               last_byte=time.time() - ingest.STALL_SEC - 5,
                               _paused=False)
        self.assertTrue(m.starved_of(_cam()["id"]))

    def test_starved_when_process_gone_or_closing(self):
        """进程还没起来 / 正在收尾 —— 两种都等于「没有上游」。"""
        m, ing = self._mg_with(proc=None, last_byte=0.0)
        self.assertTrue(m.starved_of(_cam()["id"]))
        m, ing = self._mg_with(proc=_Proc(), last_byte=0.0, _closing=object())
        self.assertTrue(m.starved_of(_cam()["id"]))


# ---------------------------------------------------------------- 输入地址

class TestRecordUrl(unittest.TestCase):
    def test_rtsp_keeps_transport(self):
        cmd = recorder.record_cmd("ffmpeg", RTSPS, "/x/%Y.mp4.part", 300)
        self.assertIn("-rtsp_transport", cmd)

    def test_tcp_url_has_no_rtsp_transport(self):
        """精简 ffmpeg 见到不认的参数会直接退出 = 一条录像都没有。"""
        cmd = recorder.record_cmd("ffmpeg", TCPURL, "/x/%Y.mp4.part", 300)
        self.assertNotIn("-rtsp_transport", cmd)
        self.assertIn(TCPURL, cmd)

    def test_recorder_falls_back_to_direct(self):
        rec = recorder.Recorder("ffmpeg", "/tmp/x", [], log=lambda m: None)
        cam = _cam()
        self.assertEqual(rec._url_of(cam), RTSPS, "没有取流必须退回直连")
        rec.url_fn = lambda cid: None if cid == "c1" else ""
        self.assertEqual(rec._url_of(cam), RTSPS)
        rec.url_fn = lambda cid: TCPURL
        self.assertEqual(rec._url_of(cam), TCPURL)

    def test_sig_tracks_effective_url(self):
        """取流晚一拍就绪时，指纹必须变化，否则录制永远停在直连。"""
        rec = recorder.Recorder("ffmpeg", "/tmp/x", [], log=lambda m: None)
        cam = _cam()
        s1 = rec._sig(cam)                  # 直连
        rec.url_fn = lambda cid: TCPURL
        s2 = rec._sig(cam)                  # 走本机端口
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(rec._sig(_cam(url="rtsp://192.0.2.99/x")), s2,
                            "改了摄像头地址也必须重建")


class TestDetectUrl(unittest.TestCase):
    def test_grab_cmd_rtsp(self):
        cmd = detect.grab_ppm_cmd("ffmpeg", RTSPS)
        self.assertIn("-rtsp_transport", cmd)

    def test_grab_cmd_tcp(self):
        cmd = detect.grab_ppm_cmd("ffmpeg", TCPURL)
        self.assertNotIn("-rtsp_transport", cmd)

    def test_frame_path_mode_never_reconnects(self):
        """取帧层在跑时，拿不到新画面也不许退回直连（会重建连接风暴）。"""
        w = detect.DetectWorker(None, "ffmpeg", "/tmp", lambda: "/tmp",
                                lambda: [], lambda: ({}, []),
                                frame_path_fn=lambda cid: None)
        ok, data, err = w._grab({"id": "c1"}, RTSPS)
        self.assertFalse(ok)
        self.assertIn("取帧层", err)


class TestLoopbackFanout(unittest.TestCase):
    """真开一个本机端口、真连两个客户端 —— 这是整套方案唯一的新增部件。"""

    def test_two_clients_get_same_bytes(self):
        import socket
        import time as _t
        ing = ingest.Ingest("ffmpeg", _cam(), log=lambda m: None)
        self.assertTrue(ing._open_server(), "本机端口必须能开")
        self.addCleanup(ing.stop)
        socks = [socket.create_connection(("127.0.0.1", ing.port), timeout=3)
                 for _ in range(2)]
        for s in socks:
            self.addCleanup(s.close)
        # accept 是异步的：等它把两个客户端都登记进来
        for _ in range(200):
            if len(ing._clients) == 2:
                break
            _t.sleep(0.01)
        self.assertEqual(len(ing._clients), 2, "两个消费者都要挂上")
        ing._broadcast(b"PAYLOAD")
        for s in socks:
            self.assertEqual(s.recv(7), b"PAYLOAD", "每一路消费者都要收到同一份字节")


class TestFrameSeqDedup(unittest.TestCase):
    """同一张图只能喂 AI 一次。

    统一取流之前，快照和检测各自 5 秒拉一次流、各自出图，碰巧不重复；
    合并成一张图之后，检测的 5 秒节拍会读到 10 秒才换一次的那张图 ——
    不按序号去重，同一条事件会在台账里记两遍、通知也推两次。
    """

    def setUp(self):
        import shutil
        import types
        import nvr_slim
        self.nvr = nvr_slim
        self.types = types
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self._old_frames = nvr_slim.STATE.get("frames")
        self._old_seen = nvr_slim.STATE.get("frame_seen")
        # 去重与节拍是两件事：这里先把节拍放开，只测去重；
        # 节拍闸单独一条用例测（下面把它设回真值）。
        self._old_gap = nvr_slim.AI_MIN_GAP
        nvr_slim.AI_MIN_GAP = 0
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_frames is None:
            self.nvr.STATE.pop("frames", None)
        else:
            self.nvr.STATE["frames"] = self._old_frames
        if self._old_seen is None:
            self.nvr.STATE.pop("frame_seen", None)
        else:
            self.nvr.STATE["frame_seen"] = self._old_seen
        self.nvr.AI_MIN_GAP = self._old_gap

    def _tap(self, seq=1, ok=True):
        tap = frames.FrameTap("ffmpeg", "c1", lambda: None,
                              os.path.join(self.td, "c1.jpg"), interval=10)
        tap.seq = seq
        tap.ok = ok
        with open(tap.out_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")
        return tap

    def _install(self, tap):
        self.nvr.STATE["frames"] = self.types.SimpleNamespace(items={"c1": tap})
        self.nvr.STATE["frame_seen"] = {}

    def test_same_seq_only_once(self):
        tap = self._tap(seq=3)
        self._install(tap)
        self.assertEqual(self.nvr._frame_path_for_detect("c1"), tap.out_path)
        self.assertEqual(self.nvr._frame_path_for_detect("c1"), "",
                         "同一张图第二次要返回空串（静默跳过），不是路径")

    def test_new_seq_goes_through(self):
        tap = self._tap(seq=3)
        self._install(tap)
        self.nvr._frame_path_for_detect("c1")
        tap.seq = 4
        self.assertEqual(self.nvr._frame_path_for_detect("c1"), tap.out_path,
                         "换了新图就该再推理一次")

    def test_dead_tap_reports_error_not_silence(self):
        """取帧层自己判死时要返回 None（记真实错误），不能伪装成"没新图"。

        否则这一路永远静默，诊断页上看着一切正常，实际一张图都没在出。
        """
        tap = self._tap(seq=1, ok=False)
        self._install(tap)
        self.assertIsNone(self.nvr._frame_path_for_detect("c1"))

    def test_fast_snapshot_does_not_speed_up_ai(self):
        """把「快照刷新」调到 1 秒，AI 也不能跟着跑到 1 秒一轮。

        取帧间隔是设置页里给人看图的旋钮（1~10 秒），AI 的节拍是算力问题。
        两张新图连续到达时，第二张必须被 AI_MIN_GAP 挡住 —— 否则五路就是
        每秒五次推理，NAS 直接被自己的预览设置打满。
        """
        self.nvr.AI_MIN_GAP = 5.0
        tap = self._tap(seq=1)
        self._install(tap)
        self.assertEqual(self.nvr._frame_path_for_detect("c1"), tap.out_path)
        tap.seq = 2                        # 1 秒后又出了一张新图
        self.assertEqual(self.nvr._frame_path_for_detect("c1"), "",
                         "新图也要等 AI 自己的节拍")
        # 把上次消费时间往前拨，节拍到了就放行
        self.nvr.STATE["frame_seen"]["c1"] = (2, __import__("time").time() - 10)
        tap.seq = 3
        self.assertEqual(self.nvr._frame_path_for_detect("c1"), tap.out_path)

    def test_seq_counts_written_frames(self):
        """seq 只在真的换成新图后 +1；frames 计数早于落盘，不能当判据。"""
        tap = frames.FrameTap("ffmpeg", "c1", lambda: None,
                              os.path.join(self.td, "c1.jpg"), interval=10)
        self.assertEqual(tap.seq, 0)
        tap._on_frame(b"\xff\xd8\xff\xd9")
        self.assertEqual(tap.seq, 1)
        self.assertEqual(tap.frames, 1)


class TestNoNewFrameNotAFailure(unittest.TestCase):
    def test_grab_returns_sentinel(self):
        w = detect.DetectWorker(None, "ffmpeg", "/tmp", lambda: "/tmp",
                                lambda: [], lambda: ({}, []),
                                frame_path_fn=lambda cid: "")
        ok, data, err = w._grab({"id": "c1"}, RTSPS)
        self.assertFalse(ok)
        self.assertEqual(err, detect.NO_NEW_FRAME,
                         "空串路径 = 没有新图，要能和真正的故障区分开")

    def test_one_is_silent_on_sentinel(self):
        """_one 遇到无新帧要直接返回：不记账、不算失败、不碰引擎。"""
        w = detect.DetectWorker(None, "ffmpeg", "/tmp", lambda: "/tmp",
                                lambda: [], lambda: ({}, []),
                                frame_path_fn=lambda cid: "")
        fails_before = dict(getattr(w, "fails", {}) or {})
        w._one({"id": "c1", "name": "c1"}, RTSPS, {}, {}, 0.0)
        self.assertEqual(dict(getattr(w, "fails", {}) or {}), fails_before,
                         "无新帧不许记失败")


class TestStallCloseAndStderr(unittest.TestCase):
    """0.9.80：卡住要能看见 ffmpeg 原话，收尾不许阻塞巡检。"""

    def setUp(self):
        self.msgs = []

    def _ing(self, proc):
        ing = ingest.Ingest("ffmpeg", _cam(), log=self.msgs.append)
        ing.port = 39001
        ing.proc = proc
        ing.started_at = 1000.0
        return ing

    def test_two_thresholds_exist_and_are_sane(self):
        """握手（从没吐过数据）要快，停顿（吐过后停了）要慢。

        这条不是形式主义：真机 2026-10-26 就是拿一个 20 秒的阈值同时管这两件事，
        结果相机停 20 秒本来能自己回来，被我们杀掉重启，多花 15 秒握手探测，
        20 秒的停顿变成 35 秒以上，还反复消耗摄像头的会话名额。
        """
        self.assertLessEqual(ingest.HANDSHAKE_SEC, 30,
                             "握手死了要快杀，它自己好不了")
        # 上下都要卡住。上界来自真机账：118 次停顿只有 1 次自愈（4 秒），
        # 其余全打穿耐心，等 120 秒等于每轮白丢 2 分钟。下界 15 秒：0.9.84
        # 从 25 收到 15 —— 118 次停顿里唯一那次自愈只停了 4 秒，15 秒
        # 仍远超任何抖动量级，再等就是纯丢（误判代价只是 5 秒短退避重连）。
        self.assertGreaterEqual(ingest.PATIENCE_SEC, 15,
                                "停顿耐心不能低于 15 秒，正常抖动不该被杀")
        self.assertLessEqual(ingest.PATIENCE_SEC, 35,
                             "打穿耐心才重连的机型上，等太久就是纯丢录像")
        self.assertEqual(ingest.STALL_SEC, ingest.HANDSHAKE_SEC)

    def test_stall_log_carries_ffmpeg_words(self):
        """只报「41 秒没有新数据」等于什么都没说 —— 病因全在 stderr 里。

        真机 2026-10-26 院子 R 连卡 4 次，日志里没有一句 ffmpeg 的话，
        只能靠猜。这一条守住「卡住必须带原话」。
        """
        ing = self._ing(_Proc())
        ing.last_byte = 0.0                 # 从 spawn 到现在没吐过 = 握手这条路径
        ing._errbuf.append("[rtsp @ 0x1] Connection reset by peer")
        ing._errbuf.append("[mpegts @ 0x2] Non-monotonous DTS in output stream")
        ing.tick(now=1000.0 + ing.stall_sec + 1)
        joined = "\n".join(self.msgs)
        self.assertIn("Connection reset", joined)
        self.assertIn("Non-monotonous", joined)
        self.assertIn("Connection reset", ing.last_error,
                      "状态接口也要带上，诊断页不该只有一句「没有新数据」")

    def test_pause_is_not_restarted_below_patience(self):
        """出过数据后停 12 秒（< 耐心 15 秒）—— 一个字节都不许动。

        历史教训是 0.9.80 用 20 秒阈值把相机能自己恢复的停顿杀成重启；
        0.9.84 耐心收到 15 秒（118 次停顿仅 1 次自愈），所以「不动」的
        保证只覆盖耐心以内 —— 超过耐心由 test_pause_restarts_only_after_
        patience 那条管。
        """
        p = _Proc()
        ing = self._ing(p)
        ing.last_byte = 1000.0
        ing.tick(now=1000.0 + 12)
        self.assertIs(ing.proc, p, "耐心以内的停顿不许重启")
        self.assertIsNone(p.signals or None, "更不许给它发信号")
        self.assertTrue(ing._paused, "要标记为停顿中，好让诊断页说实话")
        self.assertEqual(ing.stall, 0, "停顿不计入握手失败次数，否则退避会爬到 5 分钟")

    def test_pause_restarts_only_after_patience(self):
        p = _Proc()
        ing = self._ing(p)
        ing.last_byte = 1000.0
        ing.tick(now=1000.0 + ing.patience_sec - 1)
        self.assertIs(ing.proc, p, "还没到耐心上限，别动")
        ing.tick(now=1000.0 + ing.patience_sec + 1)
        self.assertIsNone(ing.proc, "超过耐心上限才认命重连")
        self.assertIn("超过", ing.last_error)
        self.assertEqual(ing.stall, 1)

    def test_pause_duration_is_measured(self):
        """恢复时必须记下真实停顿长度 —— 这是判断要不要换 UDP 的唯一依据。"""
        ing = self._ing(_Proc())
        ing.last_byte = 1000.0
        ing.tick(now=1005.0)                 # 停 5 秒，进中间态
        self.assertTrue(ing._paused)
        # 在耐心以内回来（耐心 25 秒时用 20 秒；不能写死 35，那会越过耐心）
        back = 1000.0 + ing.patience_sec - 5
        ing.tick(now=back)
        ing.last_byte = back
        ing.tick(now=back + 1)
        self.assertFalse(ing._paused)
        self.assertEqual(ing.pause_count, 1)
        self.assertAlmostEqual(ing.pause_max, ing.patience_sec - 5, delta=1.0)
        self.assertTrue([m for m in self.msgs if "恢复" in m],
                        "恢复要留一条日志，带上停了多久")

    def test_pause_duration_measured_even_when_patience_wins(self):
        """停到打穿耐心、相机根本没回来的那一次，也必须记时长。

        真机 2026-10-26 09:33 三路（院子 R / 餐厅 / 车库）就是这样：诊断页
        显示「停顿 1 次（最长 0 秒）」，等于什么都没测到 —— 因为旧写法只在
        「相机自己回来」的恢复分支里记 pause_max，而这三路是走重连分支出去的。
        没有这个数，就无法判断该等 30 秒还是 120 秒，也无法判断要不要换传输。
        """
        p = _Proc()
        ing = self._ing(p)
        ing.last_byte = 1000.0
        ing.tick(now=1000.0 + ing.patience_sec + 1)   # 一次都没恢复
        self.assertIsNone(ing.proc, "超过耐心要重连")
        self.assertEqual(ing.pause_count, 1, "只算一次停顿，不许一会儿记一会儿不记")
        self.assertAlmostEqual(ing.pause_max, ing.patience_sec + 1, delta=1.0)
        self.assertAlmostEqual(ing.last_pause, ing.patience_sec + 1, delta=1.0)
        self.assertFalse(ing._paused, "已经交给重连了，别再挂着停顿态误导诊断页")
        st = ing.status()
        self.assertGreater(st["pause_max_sec"], 0,
                           "状态接口必须把这个数报出去，否则界面还是显示 0")
        self.assertGreater(st["last_pause_sec"], 0)

    def test_stale_probe_noise_not_reported_as_cause(self):
        """静默开始**之前**的老报错不许出现在这次的说明里。

        真机 2026-10-26 每一路停顿都打同样的三行 `non-existing PPS 0 referenced
        + no frame!`，看着像病因；其实是进程刚起来探测阶段留下的，一直躺在
        errbuf 里（只在 spawn 时清空）。据此改代码会改错方向。
        """
        ing = self._ing(_Proc())
        ing.last_byte = 1000.0
        ing._errbuf.append((900.0, "[h264 @ 0x1] non-existing PPS 0 referenced"))
        ing._errbuf.append((901.0, "[h264 @ 0x1] no frame!"))
        ing.tick(now=1005.0)                 # 静默从 1000 开始，老噪声都在之前
        joined = "\n".join(self.msgs)
        self.assertNotIn("non-existing PPS", joined,
                         "探测阶段的老噪声不许冒充这次停顿的原因")
        self.assertIn("没有任何新报错", joined,
                      "没有新报错就要直说 —— 那才等于『相机没在发数据』")

    def test_new_error_after_silence_is_reported(self):
        """静默开始之后 ffmpeg 真说了什么，必须原样带出来。"""
        ing = self._ing(_Proc())
        ing.last_byte = 1000.0
        ing._errbuf.append((900.0, "[h264 @ 0x1] no frame!"))
        ing._errbuf.append((1004.0, "[rtsp @ 0x2] Connection reset by peer"))
        ing.tick(now=1005.0)
        joined = "\n".join(self.msgs)
        self.assertIn("Connection reset", joined)
        self.assertNotIn("no frame!", joined, "只要静默之后的")

    def test_err_tail_keeps_only_tail(self):
        ing = self._ing(_Proc())
        for i in range(50):
            ing._errbuf.append("line-%d" % i)
        tail = ing._err_tail()
        self.assertIn("line-49", tail)
        self.assertNotIn("line-0 ", tail, "只留尾巴，别把 128 行全塞进日志")

    def test_close_does_not_block_tick(self):
        """5 路同时卡不许串行等 —— 旧实现每路 wait(5)，一串就是 25 秒。"""
        import time as _t
        t0 = _t.time()
        for i in range(5):
            ing = ingest.Ingest("ffmpeg", _cam(), log=lambda m: None)
            ing.port = 39010 + i
            p = _Proc()
            slow = lambda timeout=None: (_t.sleep(0.3), 0)[1]
            p.wait = slow                       # 模拟收尾很慢
            ing.proc = p
            ing.started_at = 1000.0
            ing.last_byte = 1000.0
            ing.tick(now=1000.0 + ing.patience_sec + 1)
            self.assertIsNone(ing.proc)
        self.assertLess(_t.time() - t0, 0.5,
                        "巡检线程不许等收尾完成（0.9.78 在 recorder 犯过同样的错）")

    def test_no_spawn_while_closing(self):
        """收尾没完就起新进程 = 两个 ffmpeg 同时连摄像头，廉价机会挂起来。"""
        ing = self._ing(_Proc())
        ing.proc = None
        ing.next_at = 0.0
        ing._closing = object()                 # 假装还有一路在收尾
        ing.tick(now=2000.0)
        self.assertIsNone(ing.proc, "收尾期间不许 spawn")

    def test_client_socket_has_timeout(self):
        """消费者僵死（进程在、不读）时 sendall 会永久阻塞，槽位永不释放。

        真机上见过 clients=3（只有录制+取帧两个消费者），就是留在这里的。
        """
        ing = ingest.Ingest("ffmpeg", _cam(), log=lambda m: None)
        self.assertTrue(ing._open_server())
        self.addCleanup(ing.stop)
        import socket as _s
        c = _s.create_connection(("127.0.0.1", ing.port), timeout=3)
        self.addCleanup(c.close)
        import time as _t
        for _ in range(200):
            if ing._clients:
                break
            _t.sleep(0.01)
        self.assertEqual(len(ing._clients), 1)
        self.assertIsNotNone(ing._clients[0]["sock"].gettimeout(),
                             "接入的消费者 socket 必须带发送超时")


class TestFramesUpstreamGuard(unittest.TestCase):
    """取帧层：上游断流时不许把它当成「摄像头 GOP 太长」。"""

    def setUp(self):
        import shutil
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.msgs = []

    class _Alive:
        """活着的替身进程：poll 返回 None，收尾时能接住 SIGINT。"""

        def __init__(self):
            self.signals = []

        def poll(self):
            return None

        def send_signal(self, s):
            self.signals.append(s)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def _tap(self, upstream_idle):
        tap = frames.FrameTap("ffmpeg", "c1", lambda: None,
                              os.path.join(self.td, "c1.jpg"), interval=5,
                              log=self.msgs.append,
                              upstream_idle_fn=lambda cid: upstream_idle)
        tap.proc = self._Alive()
        tap._spawned_at = 1000.0
        tap.last_frame = 1000.0
        return tap

    def test_quiet_upstream_is_not_gop(self):
        """上游 9 秒没字节时，30 秒不出图是断流，不是 GOP 长。

        旧行为会摘掉 skip_frame 全帧解码（CPU 约 20 倍），然后判死重启 ——
        断流期间每路都在白烧 CPU，还把消费者一起打断。
        """
        tap = self._tap(upstream_idle=9.0)
        tap.tick(now=1040.0)
        self.assertFalse(tap._no_skip, "上游断流时不许摘 skip_frame")
        self.assertIsNotNone(tap.proc, "也不该判死重启")
        self.assertFalse([m for m in self.msgs if "全帧解码" in m])

    def test_live_upstream_still_tries_skip(self):
        """上游有数据却不出图 —— 这才是真的 GOP 太长，该重试。"""
        tap = self._tap(upstream_idle=0.2)
        tap.tick(now=1040.0)
        self.assertTrue(tap._no_skip, "源头有数据还不出图，才需要全帧重试")

    def test_unknown_upstream_keeps_old_behaviour(self):
        """走直连（没有取流层）时 upstream_idle_fn 返回 None，行为不变。"""
        tap = self._tap(upstream_idle=None)
        tap.tick(now=1040.0)
        self.assertTrue(tap._no_skip)

    def test_probe_api_helper_reports_none_without_ingest(self):
        import nvr_slim
        old = nvr_slim.STATE.get("ingest")
        nvr_slim.STATE.pop("ingest", None)
        self.addCleanup(lambda: nvr_slim.STATE.__setitem__("ingest", old)
                        if old is not None else None)
        self.assertIsNone(nvr_slim._upstream_idle_for("c1"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
