#!/usr/bin/env python3
"""日志分级测试（0.9.116）。

真机 2 小时的日志里 283 行「处理失败」、192 行「取流断开/重连」，和启动横幅
是同一个级别、同一个颜色 —— 出问题时要靠人在几百行里用眼睛捞。这里锁住
三件事：判级准不准、标记插在哪、计数对不对得上。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import nvr_slim  # noqa: E402


class LevelJudgeTest(unittest.TestCase):
    def test_err_words(self):
        for msg in ("录像目录不可写 /vol2/nvr：Permission denied",
                    "录制 客厅 启动失败：ffmpeg not found",
                    "未找到 ffmpeg：预览/录制/压制都将不可用",
                    "archive worker error: boom"):
            self.assertEqual(nvr_slim.log_level(msg), "err", msg)

    def test_warn_words(self):
        for msg in ("取流 院子R 断开（rc=0），3 秒后第 1262 次重连",
                    "处理失败 cam-x/2026-11-05/18-45-00.mp4：ffmpeg exited 1",
                    "快照失败：cam-01 地址错",
                    "录制 客厅 卡住（29 秒无新分片）",
                    "入队暂停：队列已满（64）",
                    "残片抢救失败 18-45-00.mp4",
                    "录制 客厅 按新地址重启（直连兜底）"):
            self.assertEqual(nvr_slim.log_level(msg), "warn", msg)

    def test_info_words(self):
        for msg in ("v0.9.116 pid=12 listen=0.0.0.0:18080",
                    "扫描完成：/vol2 下 7949 个分片",
                    "AI 检测已开启（快照模式，每 5 秒/路）",
                    "配置已更新：archive_prefetch"):
            self.assertEqual(nvr_slim.log_level(msg), "info", msg)

    def test_restart_alone_is_not_warn(self):
        """「重启」刻意不算警告：改目录/改分片长度后的重启是正常动作，
        染黄只会让真正的断流重连淹没在一片黄里。"""
        for msg in ("录像目录改为 /vol2/nvr，录制按新目录重启",
                    "分片长度改为 60 秒，录制按新参数重启",
                    "录制 车库 跨天重启"):
            self.assertEqual(nvr_slim.log_level(msg), "info", msg)

    def test_err_beats_warn(self):
        """同时命中两级时取更严重的那一级。"""
        self.assertEqual(
            nvr_slim.log_level("录像目录不可写，写入失败"), "err")


class TagFormatTest(unittest.TestCase):
    def setUp(self):
        nvr_slim._LOG.clear()
        nvr_slim.log_clear()
        nvr_slim.STATE["var_dir"] = tempfile.mkdtemp(prefix="nvr-lvl-")

    def test_info_untouched(self):
        nvr_slim.log_line("配置已更新：archive_prefetch")
        line = nvr_slim._LOG[-1]
        self.assertNotIn("[WARN]", line)
        self.assertNotIn("[ERR]", line)
        self.assertRegex(line, r"^\[nvr\] \d\d:\d\d:\d\d 配置已更新")

    def test_warn_token_after_timestamp(self):
        nvr_slim.log_line("快照失败：cam-01 掉线")
        line = nvr_slim._LOG[-1]
        # 前缀必须原样保留 —— 前端按行切分、复制粘贴、老用户 grep 都靠它
        self.assertRegex(line, r"^\[nvr\] \d\d:\d\d:\d\d \[WARN\] 快照失败")
        self.assertIn("cam-01 掉线", line)

    def test_err_token_after_timestamp(self):
        nvr_slim.log_line("录像目录不可写 /vol2/nvr")
        line = nvr_slim._LOG[-1]
        self.assertRegex(line, r"^\[nvr\] \d\d:\d\d:\d\d \[ERR\] 录像目录不可写")

    def test_tag_on_line_without_prefix(self):
        """非 [nvr] 前缀的行（模块直连 _record 的情况）标记放行首，
        不能让标记凭空消失、否则那一行就永远不着色。"""
        nvr_slim._record("处理失败：ffmpeg exited 1")
        self.assertTrue(nvr_slim._LOG[-1].startswith("[WARN] "))

    def test_explicit_level_overrides_judge(self):
        nvr_slim.log_line("一切正常", "warn")
        self.assertIn("[WARN] 一切正常", nvr_slim._LOG[-1])
        nvr_slim.log_line("快照失败", "info")   # 显式压平
        self.assertNotIn("[WARN]", nvr_slim._LOG[-1])

    def test_bad_level_falls_back_to_judge(self):
        nvr_slim.log_line("快照失败", "critical")
        self.assertIn("[WARN] 快照失败", nvr_slim._LOG[-1])


class LevelCountTest(unittest.TestCase):
    def setUp(self):
        nvr_slim._LOG.clear()
        nvr_slim.log_clear()
        nvr_slim.STATE["var_dir"] = tempfile.mkdtemp(prefix="nvr-cnt-")

    def test_counts_accumulate(self):
        base = nvr_slim.level_counts()
        nvr_slim.log_line("快照失败 a")
        nvr_slim.log_line("快照失败 b")
        nvr_slim.log_line("录像目录不可写")
        nvr_slim.log_line("配置已更新")
        got = nvr_slim.level_counts()
        self.assertEqual(got["warn"] - base["warn"], 2)
        self.assertEqual(got["err"] - base["err"], 1)

    def test_clear_resets_counts(self):
        nvr_slim.log_line("快照失败")
        nvr_slim.log_line("录像目录不可写")
        nvr_slim.log_clear()
        self.assertEqual(nvr_slim.level_counts(), {"warn": 0, "err": 0})

    def test_counts_independent_of_ring_overflow(self):
        """环是 800 条会滚动的，计数不能跟着滚 —— 一晚上几百条失败时，
        用户要看的是「这轮到底出了多少」，不是「环里还剩几条」。"""
        for i in range(900):
            nvr_slim.log_line("快照失败 %d" % i)
        self.assertEqual(len(nvr_slim._LOG), 800)
        self.assertGreaterEqual(nvr_slim.level_counts()["warn"], 900)


if __name__ == "__main__":
    unittest.main(verbosity=2)
