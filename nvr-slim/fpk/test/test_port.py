#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端口可配置：夹取、占用顺延、三处落盘一致。

跑：python test/test_port.py
"""
import json
import os
import socket
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "server"))

import runtime  # noqa: E402


class TestClamp(unittest.TestCase):
    def test_range(self):
        self.assertEqual(runtime.clamp_port(18081), 18081)
        self.assertEqual(runtime.clamp_port("18090"), 18090)

    def test_low_and_high(self):
        self.assertEqual(runtime.clamp_port(80), runtime.PORT_LIMITS[0])
        self.assertEqual(runtime.clamp_port(70000), runtime.PORT_LIMITS[1])

    def test_garbage_falls_back(self):
        self.assertEqual(runtime.clamp_port("abc"), 18081)
        self.assertEqual(runtime.clamp_port(None), 18081)

    def test_default_config_has_port(self):
        self.assertEqual(runtime.DEFAULTS["service_port"], 18081)


class TestPick(unittest.TestCase):
    def setUp(self):
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            s.close()

    def _occupy(self):
        """占一个临时端口，返回端口号。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("0.0.0.0", 0))
        s.listen(1)
        self.socks.append(s)
        return s.getsockname()[1]

    def test_free_port_kept(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("0.0.0.0", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(runtime.pick_port(port), port)

    def test_occupied_port_shifts_up(self):
        busy = self._occupy()
        got = runtime.pick_port(busy)
        self.assertNotEqual(got, busy)
        self.assertGreater(got, busy)
        self.assertLessEqual(got, busy + 200)

    def test_port_free_helper(self):
        busy = self._occupy()
        self.assertFalse(runtime.port_free(busy))


class TestApply(unittest.TestCase):
    def setUp(self):
        self.var = tempfile.mkdtemp()
        self.app = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app, "ui"))
        with open(os.path.join(self.app, "ui", "config"), "w", encoding="utf-8") as f:
            json.dump({".url": {"nvr.main": {"port": "${wizard_port}"}}}, f)

    def test_writes_three_places(self):
        res = runtime.apply_port(self.var, self.app, 18081)
        self.assertEqual(res["port"], 18081)
        self.assertTrue(res["ui_config"])

        with open(os.path.join(self.var, "config.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["service_port"], 18081)
        with open(os.path.join(self.var, "port.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "18081")
        with open(os.path.join(self.app, "ui", "config"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)[".url"]["nvr.main"]["port"], "18081")

    def test_read_port_prefers_actual(self):
        runtime.apply_port(self.var, self.app, 18082)
        # 服务若顺延过，port.txt 是实际值，应盖过 config
        cfg = runtime.load_config(self.var)
        cfg["service_port"] = 18081
        runtime.save_config(self.var, cfg)
        self.assertEqual(runtime.read_port(self.var), 18082)

    def test_read_port_falls_back_to_config(self):
        cfg = runtime.load_config(self.var)
        cfg["service_port"] = 18095
        runtime.save_config(self.var, cfg)
        self.assertEqual(runtime.read_port(self.var), 18095)

    def test_missing_ui_config_is_not_fatal(self):
        app2 = tempfile.mkdtemp()
        res = runtime.apply_port(self.var, app2, 18083)
        self.assertFalse(res["ui_config"])
        self.assertEqual(res["port"], 18083)

    def test_multiple_entries_all_rewritten(self):
        path = os.path.join(self.app, "ui", "config")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({".url": {"a.main": {"port": "1"}, "b.main": {"port": "2"}}}, f)
        runtime.set_ui_port(self.app, 18084)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual([v["port"] for v in d[".url"].values()], ["18084", "18084"])

    def test_set_ui_port_noop_when_same(self):
        runtime.apply_port(self.var, self.app, 18085)
        self.assertTrue(runtime.set_ui_port(self.app, 18085))


if __name__ == "__main__":
    unittest.main(verbosity=1)
