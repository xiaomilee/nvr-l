#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""残片收尾与抢救：三段式关进程 + 无 moov 的 .part 重建索引。

这两条都来自 2026-10-26 车库真机事故：01:02~01:34 之间 12 个分片永久停在
`.mp4.part`（合计约 75 MB 有效画面），用户在文件管理器里能看到、能选中、
能删除 —— 但任何播放器都打不开，回放页也永远不显示。

根因不是"网络问题导致转换中断"（系统里根本没有"转换"这一步），而是：

  · 看门狗判定卡住后直接 `p.kill()`（SIGKILL，不可捕获）→ ffmpeg 来不及
    写 moov → 文件永久缺索引；
  · finalize_parts 看到没 moov 就 `continue`，不记录、不重试、不清理，
    20 个文件里有 12 个是死账，而系统里没有任何地方能看见它们。

所以这里守六条：
  1. 关进程必须 SIGINT 优先，两次无响应才 SIGKILL（SIGKILL 是残片的制造者）
  2. 正在写的 .part（mtime 太新）绝不动
  3. 静置够久且没 moov 的，交给救援钩子重建索引
  4. 救援成功才用正式文件名，失败改名 .bad（不能留在原名上被反复重试）
  5. 救援有每轮预算，不能把磁盘 IO 从正在录的进程手里抢走
  6. rescue_cmd 必须 -c copy：重编码既慢又降画质，且弱 NAS 上可能跑不动

跑：python -m unittest test.test_parts_rescue
"""
import datetime
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "app", "server")
sys.path.insert(0, SERVER)

import ffmpeg_cmd   # noqa: E402
import recorder     # noqa: E402

# Windows 上不存在 signal.SIGKILL（不存在这个信号，也就没有这个属性），
# 但 recorder 在 POSIX 上用 p.kill()。测试只关心"到底发没发"，
# 所以给一个平台无关的占位值。
SIGKILL = getattr(signal, "SIGKILL", 9)


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


NO_MOOV = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 4096 + b"mdat" + b"\x11" * 8192
WITH_MOOV = (b"\x00\x00\x00\x18ftypisom" + b"\x00" * 4096 + b"mdat" + b"\x11" * 8192
             + b"\x00\x00\x00\x08moov" + b"\x00" * 64)


# ---------------------------------------------------------------- mp4 box 构造
# 判定函数按 box 结构走，所以测试也必须造真结构，不能拿"随便一段含 moov 的字节"
# 糊过去 —— 那样测的正是它要拒绝的东西。

def box(typ, payload=b""):
    return (8 + len(payload)).to_bytes(4, "big") + typ + payload


FTYP = box(b"ftyp", b"isom" + b"\x00" * 12)


def fmp4(payload=b"x" * 4096):
    """分片 MP4：moov 在最前面且为空，索引靠 moof（0.9.71 起的录制形态）。"""
    return FTYP + box(b"moov", b"\x00" * 16) + box(b"moof", b"\x00" * 8) + box(b"mdat", payload)


def plain_sealed(payload=b"x" * 4096):
    """整片 MP4 已封口：ftyp + mdat + moov（moov 在尾部）。"""
    return FTYP + box(b"mdat", payload) + box(b"moov", b"\x00" * 16)


def plain_writing(payload=b"x" * 4096):
    """整片 MP4 正在写：mdat 声明的长度远超实际文件（ffmpeg 还没回填）。"""
    return FTYP + (999999).to_bytes(4, "big") + b"mdat" + payload


class TestHasMoov(unittest.TestCase):
    """`_has_moov` 现在是"这个分片能不能播"，两种封装都要认。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _f(self, data):
        p = os.path.join(self.root, "a.bin")
        _write(p, data)
        return p

    def test_fragmented_recognized(self):
        """分片 MP4 的 moov 在文件头 —— 老实现只扫尾部，会把它当成没封口。"""
        self.assertTrue(recorder._has_moov(self._f(fmp4())))

    def test_plain_sealed_recognized(self):
        """老的整片 MP4：moov 在尾部。存量数据必须继续认。"""
        self.assertTrue(recorder._has_moov(self._f(plain_sealed())))

    def test_plain_writing_rejected(self):
        """正在写的整片 MP4 没有 moov，且最后一个 box 越界 → 未封口。"""
        self.assertFalse(recorder._has_moov(self._f(plain_writing())))

    def test_moov_bytes_inside_mdat_rejected(self):
        """核心防线：H.264 码流里塞了 b"moov" 四个字节，绝不能被判成已封口 ——
        判错的代价是把正在写的分片改名走，ffmpeg 还握着 fd 继续写，无声丢一段录像。"""
        trap = FTYP + box(b"mdat", b"\x00moov\x00moof\x00" + b"x" * 8192)
        self.assertFalse(recorder._has_moov(self._f(trap)))

    def test_truncated_box_rejected(self):
        """box 长度字段指向文件之外（写到一半被杀）→ 未封口。"""
        self.assertFalse(recorder._has_moov(self._f(FTYP + b"\x00\x10\x00\x00mdat")))

    def test_garbage_rejected(self):
        """随机短文件不能抛异常，只能返回 False。"""
        self.assertFalse(recorder._has_moov(self._f(b"\x00" * 8)))
        self.assertFalse(recorder._has_moov(os.path.join(self.root, "nope.bin")))


class TestRecordCmdFrag(unittest.TestCase):
    """-movflags 三个标志缺一不可，且必须能关掉回退。"""

    def test_default_is_fragmented(self):
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/d/%H.mp4.part", 300)
        joined = " ".join(cmd)
        for flag in ("frag_keyframe", "empty_moov", "default_base_moof"):
            self.assertIn(flag, joined, "分片 MP4 缺 %s" % flag)

    def test_frag_goes_through_segment_format_options(self):
        """必须用 `-segment_format_options movflags=…`，不能写 `-movflags`。

        `-movflags` 是 mp4 muxer 的私有选项，而最外层 muxer 是 segment；
        ffmpeg 对找不到归属的输出选项**不报错**（保留后丢弃），segment muxer 又只
        转发 `-segment_format_options` 给子 muxer。写成 `-movflags` 会静默失效：
        录出来的还是整片 mp4，代码里有、日志里没有，最难查的一种。
        """
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/d/%H.mp4.part", 300)
        self.assertIn("-segment_format_options", cmd)
        i = cmd.index("-segment_format_options")
        self.assertTrue(cmd[i + 1].startswith("movflags="),
                        "格式选项必须以 movflags= 开头：%s" % cmd[i + 1])
        self.assertNotIn("-movflags", cmd, "顶层 -movflags 在 -f segment 下无效")

    def test_format_options_before_output(self):
        """输出选项必须落在输出文件之前，且 segment 不能被写成 mp4。"""
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/d/%H.mp4.part", 300)
        self.assertLess(cmd.index("-segment_format_options"), cmd.index("-y"))
        self.assertEqual(cmd[cmd.index("-f") + 1], "segment", "不能把 segment 写成 mp4")

    def test_can_fall_back(self):
        """播放器播不动时能一键退回整片 MP4 —— 这是现场唯一的急救手段。"""
        cmd = recorder.record_cmd("ffmpeg", "rtsp://x", "/d/%H.mp4.part", 300,
                                  frag=False)
        self.assertNotIn("-movflags", cmd)
        self.assertNotIn("-segment_format_options", cmd)
        self.assertNotIn("frag_keyframe", " ".join(cmd))

    def test_sig_changes_with_frag(self):
        """frag 变了必须重启进程：它决定 -movflags，不重启等于设置没生效。"""
        cam = {"id": "c1", "url": "rtsp://x", "dir_name": "d"}
        a = recorder.Recorder("ffmpeg", "/tmp/x", [], frag=True)
        b = recorder.Recorder("ffmpeg", "/tmp/x", [], frag=False)
        self.assertNotEqual(a._sig(cam), b._sig(cam))

    def test_frag_degrades_when_option_rejected(self):
        """老版 ffmpeg 不认 -segment_format_options 时必须**自动退回**整片 MP4。

        这一项不被认时 ffmpeg 直接退出（不是警告），硬顶着等于五路录制全挂。
        退回后照常录，只是硬杀会产生残片 —— 由 0.9.70 的抢救兜底。
        """
        msgs = []
        rec = recorder.Recorder("ffmpeg", "/tmp/x", [], log=msgs.append, frag=True)
        self.assertEqual(rec._absorb_bad_opts(
            "Unrecognized option 'segment_format_options'."), "segment_format_options")
        self.assertFalse(rec.frag, "必须退回整片 MP4")
        self.assertTrue(any("整片 MP4" in m for m in msgs), msgs)
        # 第二次同样的报错不该再刷日志
        self.assertIsNone(rec._absorb_bad_opts(
            "Unrecognized option 'segment_format_options'."))

    def test_frag_degrade_is_idempotent(self):
        """已经是整片 MP4 时不该再"退化"一次（避免日志和 bad_opts 反复写）。"""
        rec = recorder.Recorder("ffmpeg", "/tmp/x", [], frag=False)
        self.assertIsNone(rec._absorb_bad_opts(
            "Unrecognized option 'segment_format_options'."))


class TestFragNotRenamedWhileWriting(unittest.TestCase):
    """分片 MP4 从第一秒起就有 moov，「有 moov」不再等于「写完了」。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.full = os.path.join(self.root, "车库", "2026-10-26")
        os.makedirs(self.full)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, name, data, age):
        p = os.path.join(self.full, name)
        _write(p, data)
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_fresh_frag_part_kept(self):
        """正在写的分片 MP4：有 moov 但 mtime 还新鲜 → 绝不能改名。
        改名后 ffmpeg 仍往旧 inode 写，那个文件从此不被任何扫描看见。"""
        p = self._mk("01-00-00.mp4.part", fmp4(), age=2)
        recorder.finalize_parts(self.root, "车库")
        self.assertTrue(os.path.exists(p), "正在写的分片被误改名了")
        self.assertFalse(os.path.exists(os.path.join(self.full, "01-00-00.mp4")))

    def test_settled_frag_part_renamed(self):
        """写完后静置超过 SEAL_GRACE_SEC 才允许改名。"""
        p = self._mk("01-05-00.mp4.part", fmp4(), age=recorder.SEAL_GRACE_SEC + 5)
        recorder.finalize_parts(self.root, "车库")
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.isfile(os.path.join(self.full, "01-05-00.mp4")))

    def test_sealed_plain_renamed(self):
        """整片 MP4 封口后同样要过静置闸（统一口径，不分封装）。"""
        self._mk("01-10-00.mp4.part", plain_sealed(), age=recorder.SEAL_GRACE_SEC + 5)
        recorder.finalize_parts(self.root, "车库")
        self.assertTrue(os.path.isfile(os.path.join(self.full, "01-10-00.mp4")))


class TestRescueCmd(unittest.TestCase):
    """抢救命令本身：必须只重封装，且三个容错选项一个都不能少。"""

    def setUp(self):
        self.cmd = ffmpeg_cmd.rescue_cmd("ffmpeg", "in.mp4.part", "out.mp4")

    def test_copy_not_reencode(self):
        """-c copy 是救援的全部意义：弱 NAS 上重编码 5 分钟分片要跑几分钟。"""
        self.assertIn("-c", self.cmd)
        self.assertEqual(self.cmd[self.cmd.index("-c") + 1], "copy")
        for enc in ("libx264", "libx265", "h264_qsv", "hevc_qsv"):
            self.assertNotIn(enc, self.cmd, "救援不能引入编码器 %s" % enc)

    def test_err_tolerance_flags(self):
        """断连处的半截 NAL 必须容忍，否则第一条坏帧就中止，等于没救。"""
        self.assertIn("-err_detect", self.cmd)
        self.assertIn("ignore_err", self.cmd)
        joined = " ".join(self.cmd)
        self.assertIn("genpts", joined, "开场时间戳可能从上一个进程残留值起跳")
        self.assertIn("discardcorrupt", joined)

    def test_input_before_output(self):
        """-i 必须在源之前、输出在最后 —— 参数顺序错了 ffmpeg 会把它当输出选项。"""
        i = self.cmd.index("-i")
        self.assertEqual(self.cmd[i + 1], "in.mp4.part")
        self.assertEqual(self.cmd[-1], "out.mp4")

    def test_faststart(self):
        """抢救产物要能边下边播：回放页走 range 请求，moov 必须在头部。"""
        self.assertIn("+faststart", " ".join(self.cmd))

    def test_explicit_container(self):
        """必须显式 `-f mp4`：临时名不以 .mp4 结尾（否则会被录像扫描当成正常
        分片列进回放页），ffmpeg 靠扩展名就推断不出容器，直接报
        "Unable to find a suitable output format"。"""
        i = self.cmd.index("-f")
        self.assertEqual(self.cmd[i + 1], "mp4")
        self.assertLess(i, self.cmd.index("-y"), "-f 是输出选项，必须在输出文件前")


class TestGraceClose(unittest.TestCase):
    """关进程的三段式：SIGINT → 等 → SIGINT → 等 → SIGKILL。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rec = recorder.Recorder("ffmpeg", self.tmp, [])
        # 把 15+10 秒的宽限压到几十毫秒，否则这两条测试要跑 25 秒
        self._soft, self._hard = recorder.KILL_SOFT_SEC, recorder.KILL_HARD_SEC
        recorder.KILL_SOFT_SEC = 0.05
        recorder.KILL_HARD_SEC = 0.05

    def tearDown(self):
        recorder.KILL_SOFT_SEC, recorder.KILL_HARD_SEC = self._soft, self._hard
        shutil.rmtree(self.tmp, ignore_errors=True)

    class _FakeP(object):
        """假 ffmpeg：exit_after=0 表示永不响应（真僵死）。"""

        def __init__(self, exit_after=1):
            self.sigs = []
            self.alive = True
            self.exit_after = exit_after
            self.ints = 0

        def poll(self):
            return None if self.alive else -2

        def send_signal(self, sig):
            self.sigs.append(sig)
            if sig == signal.SIGINT:
                self.ints += 1
                if self.exit_after and self.ints >= self.exit_after:
                    self.alive = False

        def wait(self, timeout=None):
            if self.alive:
                raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)
            return 0

        def kill(self):
            self.sigs.append(SIGKILL)
            self.alive = False

    def test_sigint_alone_suffices(self):
        """正常情况：一次 SIGINT 就退出，绝不能跟着发 SIGKILL。

        这是事故的核心 —— 老代码 wait(5) 超时后直接 SIGKILL，ffmpeg
        没机会写 moov，分片永久废掉。
        """
        p = self._FakeP(exit_after=1)
        self.assertTrue(self.rec._grace_close(p))
        self.assertEqual(p.sigs, [signal.SIGINT])
        self.assertNotIn(SIGKILL, p.sigs)

    def test_second_sigint_before_kill(self):
        """第一次 SIGINT 没反应时，先补一次再考虑 SIGKILL：卡在 read 上的
        ffmpeg 有时要第二个信号才打断。"""
        p = self._FakeP(exit_after=2)
        self.assertTrue(self.rec._grace_close(p))
        self.assertEqual(p.sigs, [signal.SIGINT, signal.SIGINT])
        self.assertNotIn(SIGKILL, p.sigs)

    def test_hard_kill_only_when_truly_dead(self):
        """两次都不响应才允许 SIGKILL，并且返回 False 让调用方记日志。"""
        msgs = []
        rec = recorder.Recorder("ffmpeg", self.tmp, [], log=msgs.append)
        p = self._FakeP(exit_after=0)
        self.assertFalse(rec._grace_close(p, "收尾超时"))
        self.assertEqual(p.sigs, [signal.SIGINT, signal.SIGINT, SIGKILL])
        self.assertTrue(any("收尾超时" in m for m in msgs))

    def test_already_exited_is_noop(self):
        """已经退出的进程不该再收信号（send_signal 会抛 ProcessLookupError）。"""
        p = self._FakeP()
        p.alive = False
        self.assertTrue(self.rec._grace_close(p))
        self.assertEqual(p.sigs, [])


class TestFinalizeRescue(unittest.TestCase):
    """finalize_parts 的四种分支 + 预算。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.day = "2026-10-26"
        self.full = os.path.join(self.root, "车库", self.day)
        os.makedirs(self.full)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, name, data, age=3600):
        p = os.path.join(self.full, name)
        _write(p, data)
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_sealed_renamed_without_rescue(self):
        """有 moov = 只是没来得及改名，不该走救援（救援要读一整遍文件）。"""
        self._mk("01-00-00.mp4.part", plain_sealed())
        called = []
        recorder.finalize_parts(self.root, "车库",
                                rescue=lambda s, d: called.append(s) or True)
        self.assertEqual(called, [])
        self.assertTrue(os.path.isfile(os.path.join(self.full, "01-00-00.mp4")))
        self.assertFalse(os.path.exists(os.path.join(self.full, "01-00-00.mp4.part")))

    def test_fresh_part_untouched(self):
        """正在写的那个分片 mtime 是滚动的 —— 碰它就是制造残片。"""
        self._mk("01-05-00.mp4.part", NO_MOOV, age=5)
        called = []
        recorder.finalize_parts(self.root, "车库",
                                rescue=lambda s, d: called.append(s) or True)
        self.assertEqual(called, [], "静置时间不够的分片不能被抢救")
        self.assertTrue(os.path.exists(os.path.join(self.full, "01-05-00.mp4.part")))

    def test_stale_part_rescued(self):
        """静置够久 + 无 moov → 调救援，成功则落到正式文件名。"""
        part = self._mk("01-10-00.mp4.part", NO_MOOV)

        def fake(src, dst):
            # 模拟 ffmpeg 重建索引后的产物：真文件 + 变大的体积
            _write(dst, NO_MOOV + b"moov" + b"\x00" * 5000)
            return True

        recorder.finalize_parts(self.root, "车库", rescue=fake)
        self.assertTrue(os.path.isfile(os.path.join(self.full, "01-10-00.mp4")))
        self.assertFalse(os.path.exists(part))

    def test_failed_rescue_renamed_bad(self):
        """失败必须改名 .bad：留在原名上会被每个 tick 重新试一次，把 CPU 钉住。"""
        part = self._mk("01-15-00.mp4.part", NO_MOOV)
        recorder.finalize_parts(self.root, "车库", rescue=lambda s, d: False)
        self.assertFalse(os.path.exists(part))
        self.assertTrue(os.path.isfile(part + ".bad"))

    def test_budget_limits_per_run(self):
        """一轮最多救 budget 个：抢救是顺序读盘，不能跟正在录的进程抢 IO。"""
        for i in range(3):
            self._mk("01-%02d-00.mp4.part" % (20 + i), NO_MOOV)
        called = []

        def fake(src, dst):
            called.append(src)
            _write(dst, NO_MOOV)
            return True

        budget = [1]
        recorder.finalize_parts(self.root, "车库", rescue=fake, budget=budget)
        self.assertEqual(len(called), 1)
        self.assertEqual(budget[0], 0)
        parts = [n for n in os.listdir(self.full) if n.endswith(".part")]
        self.assertEqual(len(parts), 2, "预算只给 1 个，剩下 2 个应原样保留")

    def test_existing_mp4_wins(self):
        """同名 .mp4 已存在（上次改名失败残留）→ 删残片，不能覆盖真录像。"""
        self._mk("01-25-00.mp4.part", NO_MOOV)
        dst = os.path.join(self.full, "01-25-00.mp4")
        _write(dst, WITH_MOOV)
        recorder.finalize_parts(self.root, "车库", rescue=lambda s, d: True)
        self.assertFalse(os.path.exists(os.path.join(self.full, "01-25-00.mp4.part")))
        with open(dst, "rb") as f:
            self.assertIn(b"moov", f.read(), "已存在的 .mp4 不能被覆盖")

    def test_no_rescue_hook_keeps_old_behavior(self):
        """不传 rescue（如 stop() 里的收尾）时行为要和以前一致：只封口不改名。"""
        part = self._mk("01-30-00.mp4.part", NO_MOOV)
        recorder.finalize_parts(self.root, "车库")
        self.assertTrue(os.path.exists(part), "没有救援钩子时不能动它")
        self.assertFalse(os.path.exists(part + ".bad"))

    def test_stale_rescuing_cleaned(self):
        """抢救中途被杀留下的 `.rescuing` 静置够久要清掉，否则白占一倍空间。"""
        left = os.path.join(self.full, "01-35-00.mp4.rescuing")
        _write(left, NO_MOOV)
        t = time.time() - 7200
        os.utime(left, (t, t))
        recorder.finalize_parts(self.root, "车库")
        self.assertFalse(os.path.exists(left))

    def test_fresh_rescuing_kept(self):
        """正在抢救的那个不能删 —— 巡检和抢救是同一个线程，理论上撞不上，
        但服务刚重启时可能正好有一轮在跑，删了等于白干。"""
        left = os.path.join(self.full, "01-40-00.mp4.rescuing")
        _write(left, NO_MOOV)
        recorder.finalize_parts(self.root, "车库")
        self.assertTrue(os.path.exists(left))

    def test_rescuing_not_seen_as_segment(self):
        """临时名不以 .mp4 结尾：录像扫描按 endswith(".mp4") 认文件，
        叫 xxx.mp4.rescuing 就不会被塞进回放页和 /api/recordings。"""
        left = os.path.join(self.full, "01-45-00.mp4.rescuing")
        _write(left, NO_MOOV)
        segs = recorder.list_segments(self.root, "车库")
        names = [os.path.basename(s) for s in segs]
        self.assertNotIn("01-45-00.mp4.rescuing", names)


class TestBackoffCeiling(unittest.TestCase):
    """退避必须真能爬到 MAX_BACKOFF，否则"少重启"就只是注释里的愿望。

    这里**直接调用 recorder 的函数**，不再在测试里照抄公式 —— 照抄等于把
    被测代码复制一份，公式写错时测试照样全绿（0.9.70 的 `min(tries-1,4)`
    就是这么活到真机的）。
    """

    def test_connect_backoff_reaches_max(self):
        """连不上：5→10→20→40→80→160→300 封顶。"""
        seq = [recorder.backoff_wait(t) for t in range(1, 10)]
        self.assertEqual(seq[:5], [5, 10, 20, 40, 80])
        self.assertEqual(seq[5], 160)
        self.assertEqual(seq[6], recorder.MAX_BACKOFF)
        self.assertEqual(seq[-1], recorder.MAX_BACKOFF, "封顶后必须稳住 300 秒")

    def test_connect_backoff_no_overflow(self):
        """tries 一直涨，2 的指数必须被夹住，不能算出天文数字。"""
        self.assertEqual(recorder.backoff_wait(999), recorder.MAX_BACKOFF)

    def test_stall_backoff_is_short(self):
        """卡住：5→10→20→30 封顶。

        卡住 ≠ 连不上 —— 进程活着、对端在但不吐数据，等待期间是一分不录的。
        0.9.72 及之前卡住也吃 300 秒退避，真机四路每 75–90 秒卡一次，
        车库因此出现 254 秒、422 秒的空洞。
        """
        seq = [recorder.stall_wait(t) for t in range(1, 8)]
        self.assertEqual(seq, [5, 10, 20, 30, 30, 30, 30])
        self.assertLess(recorder.stall_wait(99), recorder.backoff_wait(99),
                        "卡住必须比连不上等得短")

    def test_stall_cap_survives_big_reconnect(self):
        """reconnect 被设成 60 秒时，卡住也不能等 60 秒以上。"""
        self.assertEqual(recorder.stall_wait(3, reconnect=60),
                         recorder.STALL_BACKOFF)


class _AliveProc:
    """「卡住」的 ffmpeg 替身：poll() 一直是 None（进程活着），收到 SIGINT 才退。"""

    def __init__(self):
        self.rc = None
        self.signals = []

    def poll(self):
        return self.rc

    def send_signal(self, sig):
        self.signals.append(sig)
        self.rc = 0                    # 真机上绝大多数情况一次 SIGINT 就收尾

    def wait(self, timeout=None):
        return self.rc

    def kill(self):
        self.rc = -9


class TestStallRestartBackoff(unittest.TestCase):
    """看门狗「卡住重启」的等待必须短，且**不把连不上的计数顶上去**。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nvr-stall-")
        self.msgs = []
        cam = {"id": "c1", "name": "车库", "url": "rtsp://192.0.2.9/ch1",
               "dir_name": "车库"}
        self.cam = cam
        self.rec = recorder.Recorder("ffmpeg", self.root, [cam],
                                     log=self.msgs.append, var_dir=self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _stuck(self, tries=0, stall=0, started_ago=None):
        """搭一个「进程活着但一个字节都没落盘」的现场。"""
        age_started = started_ago if started_ago is not None else \
            recorder.STALL_SEC + 60
        self.rec.procs["c1"] = {
            "p": _AliveProc(), "day": datetime.date.today().isoformat(),
            "tries": tries, "stall": stall, "next": 0,
            "started_at": 1000.0 - age_started, "dead": None,
            "sig": self.rec._sig(self.cam)}
        now = 1000.0
        self.rec.tick(now=now)
        return self.rec.procs["c1"], now

    def test_stall_waits_short_and_keeps_tries(self):
        st, now = self._stuck(tries=0)
        self.assertIsNone(st["p"], "卡住必须被杀掉等重连")
        self.assertEqual(st["stall"], 1)
        self.assertEqual(st["tries"], 0,
                         "卡住不该顶高「连不上」的退避计数（会让下次等 5 分钟）")
        self.assertEqual(st["next"] - now, recorder.stall_wait(1))

    def test_stall_backoff_grows_then_caps(self):
        st, now = self._stuck(stall=5)
        self.assertEqual(st["stall"], 6)
        self.assertEqual(st["next"] - now, recorder.STALL_BACKOFF,
                         "连续卡住也要封顶在 30 秒，不能爬到 300")

    def test_stall_log_says_short_retry(self):
        self._stuck()
        joined = "\n".join(self.msgs)
        self.assertIn("卡住", joined)
        self.assertIn("第 1 次", joined, "日志要说清第几次，便于判断是否在循环")


class TestRescuePartIntegration(unittest.TestCase):
    """_rescue_part 的真实行为：用本机 Python 冒充 ffmpeg 控制产物。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.msgs = []
        self.rec = recorder.Recorder("ffmpeg", self.root, [], log=self.msgs.append)
        self.dst = os.path.join(self.root, "out.mp4")
        self._real = ffmpeg_cmd.rescue_cmd

    def tearDown(self):
        ffmpeg_cmd.rescue_cmd = self._real
        shutil.rmtree(self.root, ignore_errors=True)

    def _fake_ffmpeg(self, code):
        """让 rescue_cmd 返回一段 python -c：按 code 写产物后退出。"""
        self.rec.ffmpeg = sys.executable
        ffmpeg_cmd.rescue_cmd = lambda ff, src, out: [sys.executable, "-c", code(out)]

    def test_no_ffmpeg_returns_false(self):
        """开发机上没有 ffmpeg：直接返回 False，不能抛，也不能留下半个文件。"""
        self.rec.ffmpeg = ""
        self.assertFalse(self.rec._rescue_part(os.path.join(self.root, "x.part"),
                                              self.dst))
        self.assertFalse(os.path.exists(self.dst))

    def test_empty_output_rejected(self):
        """ffmpeg 退出 0 但只写了 10 字节（空 moov 的典型体积）→ 视为没救回东西。"""
        self._fake_ffmpeg(lambda out: "open(%r,'wb').write(b'x'*10)" % out)
        self.assertFalse(self.rec._rescue_part(os.path.join(self.root, "a.part"),
                                              self.dst))
        self.assertFalse(os.path.exists(self.dst), "没救回内容的产物不能留下")
        self.assertTrue(any("抢救失败" in m for m in self.msgs))

    def test_good_output_accepted(self):
        """退出 0 且产物够大 → 落到 dst，并记一条成功日志。"""
        self._fake_ffmpeg(lambda out: "open(%r,'wb').write(b'moov'+b'\\0'*9000)" % out)
        self.assertTrue(self.rec._rescue_part(os.path.join(self.root, "a.part"),
                                             self.dst))
        self.assertTrue(os.path.isfile(self.dst))
        self.assertTrue(any("抢救成功" in m for m in self.msgs))

    def test_nonzero_exit_rejected(self):
        """ffmpeg 报错退出（源完全没视频流）→ 不留文件、不抛异常。"""
        self._fake_ffmpeg(lambda out: "raise SystemExit(1)")
        self.assertFalse(self.rec._rescue_part(os.path.join(self.root, "a.part"),
                                              self.dst))
        self.assertFalse(os.path.exists(self.dst))


if __name__ == "__main__":
    unittest.main(verbosity=2)
