#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知链路挂接单测：模板真的进了 POST、发送结果真的落了盘（0.9.124）。

为什么单独一份而不是塞进 test_notify.py：那份测的是冷却 / 重试 / 胖 JSON，
构造 Notifier 时不传 var_dir（不落盘），保持它「不往工作区写文件」的性质。
这里专测新挂上的三件事，用真临时目录 + 真本地 HTTP 服务器，不 mock 落盘。

跑：python test/test_notify_wire.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "server"))
import notify  # noqa: E402
import notify_history as hist  # noqa: E402


class Sink:
    def __init__(self, code=200, fail_times=0):
        self.code = code
        self.fail_times = fail_times
        self.hits = []
        self.lock = threading.Lock()

    def handle(self, body):
        with self.lock:
            self.hits.append(body)
            if self.fail_times > 0:
                self.fail_times -= 1
                return 500
            return self.code


def make_server(sink):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {"_raw": raw.decode("utf-8", "replace")}
            code = sink.handle(body)
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/hook" % srv.server_address[1]


class WireTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="nvr-nwire-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.sink = Sink()
        self.srv, self.url = make_server(self.sink)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.cfg = {"notify": {"webhook": self.url, "motion": True,
                               "person": True, "vehicle": True,
                               "cooldown": 0}}
        self.logs = []
        self.n = notify.Notifier(lambda: {"notify": self.cfg["notify"]},
                                 log=self.logs.append, var_dir=self.d)
        self.n.start()
        self.addCleanup(self.n.stop)

    def drain(self, want=1, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.sink.lock:
                if len(self.sink.hits) >= want:
                    return True
            time.sleep(0.03)
        return False

    def history_rows(self):
        return hist.query(self.d, limit=hist.QUERY_MAX)

    # ------------------------------------------------------------ 模板挂接
    def test_dispatch_uses_render_result(self):
        """POST 出去的 body 必须是 render() 的返回值，不是旧的固定胖 JSON。"""
        fake = {"msg_type": "text", "content": {"text": "来自模板"}}
        with mock.patch.object(notify.notify_tpl, "render",
                               return_value=(fake, "feishu")) as r:
            self.assertTrue(self.n.offer("院子", "person", t=1000.0))
            self.assertTrue(self.drain(1))
        self.assertTrue(r.called)
        self.assertEqual(self.sink.hits[0], fake)

    def test_platform_name_is_recorded(self):
        """落盘记平台名，不记 URL —— 排障够用，又不把 key 写进文件。"""
        with mock.patch.object(notify.notify_tpl, "render",
                               return_value=({"x": 1}, "dingtalk")):
            self.assertTrue(self.n.offer("院子", "person", t=1000.0))
            self.assertTrue(self.drain(1))
            for _ in range(50):
                if self.history_rows():
                    break
                time.sleep(0.02)
        rows = self.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform"], "dingtalk")
        self.assertNotIn("127.0.0.1", json.dumps(rows[0]))

    def test_default_render_when_no_template(self):
        """模板配置入口还没接时，nt.get('template') 是 None，走默认文案。"""
        self.assertTrue(self.n.offer("院子", "person", t=1000.0))
        self.assertTrue(self.drain(1))
        self.assertIn("检测到人员", self.sink.hits[0]["content"]["text"])

    # ------------------------------------------------------------ 落盘
    def test_success_is_persisted(self):
        self.assertTrue(self.n.offer("院子", "person", t=1000.0))
        self.assertTrue(self.drain(1))
        rows = []
        for _ in range(50):
            rows = self.history_rows()
            if rows:
                break
            time.sleep(0.02)
        self.assertEqual(len(rows), 1, "发送成功必须落一条")
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["tries"], 1)
        self.assertGreaterEqual(rows[0]["ms"], 0)

    def test_failure_is_persisted_with_reason(self):
        self.sink.fail_times = 3
        self.n.timeout = 2
        self.assertTrue(self.n.offer("院子", "vehicle", t=1000.0))
        self.assertTrue(self.drain(3, timeout=20))
        rows = []
        for _ in range(50):
            rows = self.history_rows()
            if rows:
                break
            time.sleep(0.05)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["tries"], 3)
        self.assertIn("HTTP", rows[0]["err"])

    def test_restart_keeps_history(self):
        """这条是本批的根：新建一个 Notifier（模拟重启）仍看得到旧记录。"""
        self.assertTrue(self.n.offer("院子", "person", t=1000.0))
        self.assertTrue(self.drain(1))
        for _ in range(50):
            if self.history_rows():
                break
            time.sleep(0.02)
        n2 = notify.Notifier(lambda: {"notify": self.cfg["notify"]},
                             log=self.logs.append, var_dir=self.d)
        self.assertEqual(n2.status()["history"]["total"], 1)
        self.assertEqual(len(n2.history()), 1)

    def test_status_history_is_cached_not_rescanned(self):
        """status() 是轮询路径，不许当场读盘。"""
        st = self.n.status()
        self.assertIn("history", st)
        self.assertEqual(st["history"]["file"], hist.FILE_NAME)
        self.assertEqual(st["history"]["total"], 0)
        self.assertTrue(st["history"]["persisted"])
        with mock.patch.object(notify.notify_history, "stats",
                               side_effect=AssertionError("status 不该读盘")), \
             mock.patch.object(notify.notify_history, "query",
                               side_effect=AssertionError("status 不该读盘")):
            self.n.status()

    def test_no_var_dir_means_no_disk_write(self):
        """单测/嵌入用法不传 var_dir：persisted=False，append 一次都不许调。"""
        n = notify.Notifier(lambda: {"notify": {}}, log=self.logs.append)
        self.assertFalse(n.status()["history"]["persisted"])
        with mock.patch.object(notify.notify_history, "append",
                               side_effect=AssertionError("不该落盘")):
            n._persist({"cam": "c", "cat": "person", "t": 1.0}, True, 1, "", 1,
                       "generic")
        self.assertEqual(n.status()["history"]["total"], 0)

    def test_test_send_also_persists(self):
        """设置页「测试发送」也是一次发送，历史里该能对上账。"""
        ok, ms, err = self.n.test(self.url)
        self.assertTrue(ok, err)
        rows = self.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cam"], "测试")

    def test_history_filters_are_wired(self):
        self.assertTrue(self.n.offer("院子", "person", t=1000.0))
        self.assertTrue(self.drain(1))
        for _ in range(50):
            if self.history_rows():
                break
            time.sleep(0.02)
        self.assertEqual(len(self.n.history(ok=True)), 1)
        self.assertEqual(len(self.n.history(ok=False)), 0)
        self.assertEqual(len(self.n.history(cam="车库")), 0)
        self.assertEqual(len(self.n.history(since=2000.0)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
