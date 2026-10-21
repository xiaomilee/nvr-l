#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录像目录的默认值只能有一个来源。

背景：nvr_slim.record_dir() 曾经写死回退 "/vol2/1000/NVR"，而
runtime.DEFAULTS["record_dir"] 是 "/vol1/@appshare/nvr-output"。正常路径下
load_config() 从 DEFAULTS 起步，回退走不到；但配置文件里 record_dir 被写成
空串 / null（手改、旧版迁移、写盘中断）时两处就分叉 —— 设置页显示 A 目录，
录制器实际写 B 目录，录像"凭空消失"而日志一切正常。

跑：python test/test_record_dir.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "app", "server")
sys.path.insert(0, SERVER)

import runtime    # noqa: E402
import nvr_slim   # noqa: E402

SERVE_FILE = os.path.join(SERVER, "nvr_slim.py")


def _read():
    with open(SERVE_FILE, "r", encoding="utf-8") as f:
        return f.read()


class TestSingleSource(unittest.TestCase):
    def test_no_hardcoded_fallback_in_source(self):
        """源码里不该再出现那两套写死的目录。

        注意：注释里提到旧值是刻意的说明文字，所以这里匹配带引号的字面量
        （即真被当成 Python 字符串用的形式），而不是裸的路径片段。
        """
        src = _read()
        self.assertNotIn('"/vol2/1000/NVR"', src,
                         "record_dir 又在写死回退值了，必须用 runtime.DEFAULTS")
        self.assertNotIn('os.path.join(a.var, "rec")', src,
                         "启动时的 rec_root 又用了第三套回退，必须用 runtime.DEFAULTS")

    def test_both_fallbacks_use_defaults(self):
        """两条取值路径都要落在 runtime.DEFAULTS 上。"""
        src = _read()
        self.assertIn('or runtime.DEFAULTS["record_dir"]', src,
                      "record_dir() 丢了默认值来源")
        self.assertIn('rec_root = cfg.get("record_dir") or runtime.DEFAULTS',
                      src, "main() 里的 rec_root 没跟 record_dir() 用同一个来源")

    def test_default_is_absolute(self):
        d = runtime.DEFAULTS["record_dir"]
        self.assertTrue(d.startswith("/"), "默认录制目录必须是绝对路径：%r" % d)
        self.assertNotIn("..", d)


class TestBrokenConfigFallsBack(unittest.TestCase):
    """record_dir 被写成空串/null 时，两条路径必须指向同一个目录。"""

    def _run(self, broken):
        var = tempfile.mkdtemp(prefix="nvr-rd-")
        cfg = dict(runtime.DEFAULTS)
        cfg["record_dir"] = broken
        with open(runtime.config_path(var), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        old = nvr_slim.STATE["var_dir"]
        nvr_slim.STATE["var_dir"] = var
        try:
            # 这里的值就是录制器 main() 里 rec_root 的算法
            from_cfg = (runtime.load_config(var).get("record_dir")
                        or runtime.DEFAULTS["record_dir"])
            return from_cfg, nvr_slim.record_dir()
        finally:
            nvr_slim.STATE["var_dir"] = old

    def test_empty_string(self):
        a, b = self._run("")
        self.assertEqual(a, b)
        self.assertEqual(b, runtime.DEFAULTS["record_dir"])

    def test_null(self):
        a, b = self._run(None)
        self.assertEqual(a, b)
        self.assertEqual(b, runtime.DEFAULTS["record_dir"])

    def test_custom_value_wins(self):
        a, b = self._run("/vol9/custom/NVR")
        self.assertEqual(a, b)
        self.assertEqual(b, "/vol9/custom/NVR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
