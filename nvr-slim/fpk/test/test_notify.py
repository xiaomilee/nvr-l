#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify.py 单测：入队闸门 / cooldown 双键 / 胖 JSON / 退避与状态。

用本地 HTTP 服务器收真请求（不起外网），
覆盖「发没发、发了几次、发的是什么」三件事。
"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "server"))
import notify  # noqa: E402


class _Sink:
    """记录收到的请求，按脚本返回状态码。"""

    def __init__(self, code=200, fail_times=0):
        self.code = code
        self.fail_times = fail_times
        self.hits = []            # [(path, body_dict)]
        self.lock = threading.Lock()

    def handle(self, path, body):
        with self.lock:
            self.hits.append((path, body))
            if self.fail_times > 0:
                self.fail_times -= 1
                return 500
            return self.code


def _make_server(sink):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {"_raw": raw.decode("utf-8", "replace")}
            code = sink.handle(self.path, body)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d/hook" % srv.server_address[1]


class NotifierTestBase(unittest.TestCase):
    def setUp(self):
        self.sink = _Sink()
        self.srv, self.url = _make_server(self.sink)
        self.logs = []
        self.nt = {"webhook": self.url, "motion": True, "person": True,
                   "vehicle": True, "cooldown": 60}
        self.n = notify.Notifier(lambda: {"notify": self.nt}, log=self.logs.append)
        self.n.start()

    def tearDown(self):
        self.n.stop()
        self.srv.shutdown()
        self.srv.server_close()

    def wait_drain(self, want=1, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.sink.lock:
                if len(self.sink.hits) >= want:
                    return True
            time.sleep(0.05)
        return False


class TestOfferGate(NotifierTestBase):
    def test_no_webhook_drops(self):
        self.nt["webhook"] = ""
        self.assertFalse(self.n.offer("院子", "person"))
        self.assertEqual(len(self.sink.hits), 0)

    def test_blank_webhook_drops(self):
        self.nt["webhook"] = "   "
        self.assertFalse(self.n.offer("院子", "person"))

    def test_category_switch(self):
        self.nt["person"] = False
        self.assertFalse(self.n.offer("院子", "person"), "人员通知关着就不该发")
        self.assertTrue(self.n.offer("院子", "vehicle"))

    def test_unknown_category_drops(self):
        self.assertFalse(self.n.offer("院子", "dog"))

    def test_two_wheeler_needs_vehicle_switch(self):
        """两轮车并入车辆口径：开关看 vehicle。"""
        self.nt["vehicle"] = False
        self.assertFalse(self.n.offer("院子", "two_wheeler"))

    def test_cooldown_double_key(self):
        t0 = 1000.0
        self.assertTrue(self.n.offer("院子", "person", t=t0))
        self.assertFalse(self.n.offer("院子", "person", t=t0 + 30), "冷却中静默丢弃")
        self.assertTrue(self.n.offer("院子", "vehicle", t=t0 + 30), "不同类别不受同类冷却影响")
        self.assertTrue(self.n.offer("客厅", "person", t=t0 + 30), "不同路不受影响")
        self.assertTrue(self.n.offer("院子", "person", t=t0 + 60), "到点恢复")

    def test_cooldown_value_respected(self):
        self.nt["cooldown"] = 10
        t0 = 500.0
        self.assertTrue(self.n.offer("院子", "person", t=t0))
        self.assertFalse(self.n.offer("院子", "person", t=t0 + 9))
        self.assertTrue(self.n.offer("院子", "person", t=t0 + 10))

    def test_queue_full_drops_not_raise(self):
        self.n.q = _FullQueue()
        self.assertFalse(self.n.offer("院子", "person"))


class _FullQueue:
    def put_nowait(self, item):
        raise __import__("queue").Full


class TestDispatch(NotifierTestBase):
    def test_send_ok(self):
        self.assertTrue(self.n.offer("院子", "person", t=time.time()))
        self.assertTrue(self.wait_drain(1), "线程应在 5 秒内发出")
        body = self.sink.hits[0][1]
        # 胖 JSON：四套键都要在
        self.assertEqual(body["msg_type"], "text")
        self.assertEqual(body["msgtype"], "text")
        self.assertIn("text", body["content"])
        self.assertIn("content", body["text"])
        self.assertIn("title", body)
        self.assertIn("desp", body)
        self.assertIn("检测到人员", body["content"]["text"])
        self.assertIn("院子", body["content"]["text"])
        st = self.n.status()
        self.assertTrue(st["recent"][0]["ok"])
        self.assertEqual(st["pending"], 0)

    def test_retry_then_give_up(self):
        self.sink.fail_times = 3
        self.n.timeout = 2
        self.assertTrue(self.n.offer("院子", "person", t=time.time()))
        # 3 次尝试全失败：2s+4s 退避；最后一次失败不再空等，记录立刻落
        self.assertTrue(self.wait_drain(3, timeout=20), "应重试满 3 次")
        time.sleep(0.5)
        st = self.n.status()
        self.assertFalse(st["recent"][0]["ok"])
        self.assertTrue(any("通知发送失败" in m for m in self.logs))

    def test_stop_aborts_retry(self):
        self.sink.fail_times = 99
        self.n.timeout = 2
        self.n.offer("院子", "person", t=time.time())
        time.sleep(0.4)
        self.n.stop()
        n0 = len(self.sink.hits)
        time.sleep(3.0)
        self.assertEqual(len(self.sink.hits), n0, "stop 后不该继续重试")

    def test_recent_capped(self):
        for i in range(30):
            self.n._record({"cam": "c%d" % i, "cat": "person"}, True, 1)
        self.assertEqual(len(self.n.status()["recent"]), 20)

    def test_message_label(self):
        m = notify.Notifier._message({"cam": "院子", "cat": "vehicle",
                                      "t": time.time()})
        self.assertIn("检测到车辆", m["content"]["text"])
        m = notify.Notifier._message({"cam": "院子", "cat": "two_wheeler",
                                      "t": time.time()})
        self.assertIn("检测到两轮车", m["content"]["text"])
        m = notify.Notifier._message({"cam": "院子", "cat": "motion",
                                      "t": time.time()})
        self.assertIn("检测到移动侦测", m["content"]["text"])


class TestSyncTest(NotifierTestBase):
    def test_empty_url(self):
        ok, ms, err = self.n.test("")
        self.assertFalse(ok)
        self.assertEqual(err, "Webhook 地址为空")

    def test_ok(self):
        ok, ms, err = self.n.test(self.url)
        self.assertTrue(ok, err)
        self.assertGreaterEqual(ms, 0)
        body = self.sink.hits[0][1]
        self.assertIn("测试", body["content"]["text"])

    def test_failure_reported(self):
        self.srv.shutdown()
        self.srv.server_close()
        ok, ms, err = self.n.test(self.url)
        self.assertFalse(ok)
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
