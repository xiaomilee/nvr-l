"""启动就绪探测（cmd/main 里的 wait_health）
------------------------------------------------------------------
0.9.120 装到测试机上第一次就撞了个跟产品无关、但足以让整轮白跑的问题：
port.txt 是**上一轮**留下的，wait_health 只读一次，于是拿旧端口（顺延后的
18084）死探 30 秒，新进程其实老老实实占了 18083 —— 结果判"服务启动超时"，
把刚起好的进程收掉，部署工具再去回滚，回滚用的是同一段代码，也起不来。

所以这里把那段探针抠出来单独跑，钉住三件事：
  1) 请求的端口能起来 → 成功（不能因为 port.txt 写着别的端口就失败）；
  2) 服务顺延到了 port.txt 里的端口 → 也要成功（两个端口都得探）；
  3) 谁也连不上 → 必须超时失败（不然"起来了没有"这个问题就没有答案了）；
另外 401 必须算"已启动"：0.9.120 起 /api/health 要凭证，把 401 当失败会让
每一次升级都判定失败。

跑法： python test/test_wait_health.py
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def probe_source():
    """从 cmd/main 里抠出 wait_health 的 heredoc。抠不出来就说明函数被重写了，
    这条测试应当跟着改，所以直接判失败而不是跳过。"""
    with open(os.path.join(ROOT, "cmd", "main"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"wait_health\(\)\s*\{.*?<<'PYEOF'\n(.*?)\nPYEOF", text, re.S)
    return m.group(1) if m else None


class _Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(_Handler.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def serve(status):
    _Handler.status = status
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def stop(srv):
    srv.shutdown()
    srv.server_close()


def dead_port():
    """拿一个刚被释放的端口：连不上，但不会被别的进程突然占走。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestWaitHealth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = probe_source()

    def setUp(self):
        self.assertIsNotNone(self.code, "cmd/main 里找不到 wait_health 的 PYEOF 段")
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                               encoding="utf-8", newline="\n")
        self.tmp.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _run(self, want, timeout, pfile_port):
        with open(self.tmp.name, "w", encoding="utf-8", newline="\n") as f:
            f.write(str(pfile_port))
        import time
        t0 = time.time()
        rc = subprocess.call([sys.executable, "-c", self.code,
                              str(want), str(timeout), self.tmp.name],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return rc, time.time() - t0

    def test_requested_port_ready_ignores_stale_file(self):
        """旧 port.txt 写着 18084、服务其实占了 18083 —— 这正是那次失败。"""
        srv, port = serve(200)
        try:
            rc, cost = self._run(port, 5, dead_port())
        finally:
            stop(srv)
        self.assertEqual(rc, 0, "请求的端口已经通了，不该判超时")
        self.assertLess(cost, 3, "应当立刻返回，而不是死等满 5 秒")

    def test_401_counts_as_started(self):
        """0.9.120 起 /api/health 要凭证：401 是"起来了"，不是"没起来"。"""
        srv, port = serve(401)
        try:
            rc, _ = self._run(port, 5, port)
        finally:
            stop(srv)
        self.assertEqual(rc, 0)

    def test_fallback_port_from_file_also_probed(self):
        """服务顺延到 port.txt 写的端口：请求端口没人，也必须认。"""
        srv, real = serve(200)
        try:
            rc, _ = self._run(dead_port(), 5, real)
        finally:
            stop(srv)
        self.assertEqual(rc, 0, "port.txt 里的端口通了也算启动成功")

    def test_nothing_listening_fails(self):
        """两个端口都没人：必须失败。否则"服务起来了没有"就等于没答案。"""
        a, b = dead_port(), dead_port()
        rc, cost = self._run(a, 1, b)
        self.assertEqual(rc, 1)
        self.assertGreater(cost, 0.5)

    def test_stale_file_is_reread(self):
        """文件是在探测过程中才写对的（服务启动后才落 port.txt）——每轮重读才能看到。"""
        srv, real = serve(200)
        want = dead_port()
        with open(self.tmp.name, "w", encoding="utf-8", newline="\n") as f:
            f.write(str(dead_port()))          # 一开始写的是错端口
        try:
            def later():
                with open(self.tmp.name, "w", encoding="utf-8", newline="\n") as f:
                    f.write(str(real))
            threading.Timer(0.8, later).start()
            rc = subprocess.call([sys.executable, "-c", self.code,
                                  str(want), "6", self.tmp.name],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            stop(srv)
        self.assertEqual(rc, 0, "port.txt 被改写后应当能读到新端口")


if __name__ == "__main__":
    unittest.main(verbosity=2)
