#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知历史接口的真机冒烟：真起一次本地服务，验 HTTP 那一层（0.9.123）。

为什么要真起服务：单测已经证明 `notify_history.query()` 能查文件，但
「路由挂没挂上」「鉴权有没有继承」「status_payload 的计数是不是缓存」
这三件事只有走过 HTTP 才算数。风格照 `test_btn_effect.py`：绑 127.0.0.1、
端口由系统分配、临时目录跑完即删，不碰测试机与生产机。

跑：python test/test_notify_http.py
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "app", "server", "nvr_slim.py")
PY = sys.executable


class NotifyHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.var = tempfile.mkdtemp(prefix="nvr-nhttp-")
        app = os.path.join(cls.var, "app")
        ui = os.path.join(app, "ui")
        os.makedirs(ui, exist_ok=True)
        shutil.copy(os.path.join(ROOT, "app", "ui", "index.html"),
                    os.path.join(ui, "index.html"))
        rec = os.path.join(cls.var, "rec")
        os.makedirs(rec, exist_ok=True)
        cfg = {"record_dir": rec, "record_enable": False, "cameras": []}
        with io.open(os.path.join(cls.var, "config.json"), "w",
                     encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

        # 预置两条历史：一条成功一条失败，验证服务端真的读了这个文件，
        # 而不是把 status() 里的内存 recent 换个名字回出来。
        with io.open(os.path.join(cls.var, "notify-history.jsonl"), "w",
                     encoding="utf-8") as f:
            f.write(json.dumps({"at": 1000.0, "cam": "院子", "cat": "person",
                                "ok": True, "ms": 12, "err": "", "tries": 1,
                                "platform": "feishu"}) + "\n")
            f.write(json.dumps({"at": 2000.0, "cam": "车库", "cat": "vehicle",
                                "ok": False, "ms": 0, "err": "HTTP 500",
                                "tries": 3, "platform": "generic"}) + "\n")

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        env = dict(os.environ, NVR_SIMULATE_GPU="qsv", TRIM_PKGVAR=cls.var)
        cls.proc = subprocess.Popen(
            [PY, SERVER, "--port", str(cls.port), "--var", cls.var,
             "--app", app, "--ui", ui, "--host", "127.0.0.1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        cls.token = ""
        for _ in range(80):
            time.sleep(0.25)
            if cls.proc.poll() is not None:
                break
            try:
                with io.open(os.path.join(cls.var, "secret.json"),
                             encoding="utf-8") as f:
                    cls.token = json.load(f).get("api_token") or ""
            except (OSError, ValueError):
                pass
            if not cls.token:
                continue
            try:
                code, _ = cls.req("/api/health")
                if code == 200:
                    return
            except Exception:
                pass
        cls.proc.terminate()
        raise RuntimeError("本地服务没起来（端口 %s）" % cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.var, ignore_errors=True)

    @classmethod
    def req(cls, path, method="GET", body=None, authed=True):
        h = {"X-NVR-Request": "1", "Content-Type": "application/json"}
        if authed and cls.token:
            h["Authorization"] = "Bearer " + cls.token
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (cls.port, path),
                                   data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path, authed=True):
        return self.req(path, authed=authed)

    # ------------------------------------------------------------ 鉴权
    def test_10_requires_token(self):
        """新路由继承统一鉴权闸，没凭证不许读历史（里面有摄像头名）。"""
        code, _ = self.get("/api/notify/history", authed=False)
        self.assertEqual(code, 401)

    # ------------------------------------------------------------ 形状与落盘
    def test_20_reads_persisted_rows(self):
        code, raw = self.get("/api/notify/history")
        self.assertEqual(code, 200)
        d = json.loads(raw)
        self.assertTrue(d["ok"])
        self.assertEqual(d["file"], "notify-history.jsonl")
        self.assertEqual(d["count"], 2)
        self.assertEqual(len(d["items"]), 2)
        self.assertEqual(d["items"][0]["at"], 2000.0, "最新在前")
        self.assertEqual(d["items"][0]["platform"], "generic")

    def test_22_no_webhook_url_in_payload(self):
        _code, raw = self.get("/api/notify/history")
        d = json.loads(raw)
        for row in d["items"]:
            self.assertNotIn("webhook", row)
            self.assertNotIn("url", row)

    def test_30_status_history_is_cached_count(self):
        """status 里的 history 是启动读一次的计数，不随查询变，也不当场扫盘。"""
        _code, raw = self.get("/api/status")
        h = json.loads(raw)["notify"]["history"]
        self.assertEqual(h["file"], "notify-history.jsonl")
        self.assertEqual(h["total"], 2)
        self.assertEqual(h["last_at"], 2000.0)
        self.assertTrue(h["persisted"])

    # ------------------------------------------------------------ 过滤参数
    def test_40_filter_ok(self):
        for flag, want in (("1", 1), ("0", 1)):
            with self.subTest(ok=flag):
                _c, raw = self.get("/api/notify/history?ok=" + flag)
                d = json.loads(raw)
                self.assertEqual(d["count"], want)
                self.assertEqual(bool(d["items"][0]["ok"]), flag == "1")

    def test_42_filter_cam_and_cat(self):
        _c, raw = self.get("/api/notify/history?cam=%E9%99%A2%E5%AD%90")
        self.assertEqual(json.loads(raw)["count"], 1, "按摄像头名过滤")
        _c, raw = self.get("/api/notify/history?cat=vehicle")
        self.assertEqual(json.loads(raw)["count"], 1, "按类别过滤")

    def test_44_filter_time_range(self):
        _c, raw = self.get("/api/notify/history?from=1500&to=2500")
        self.assertEqual(json.loads(raw)["count"], 1)

    def test_46_limit(self):
        _c, raw = self.get("/api/notify/history?limit=1")
        self.assertEqual(len(json.loads(raw)["items"]), 1)

    def test_48_bad_params_do_not_500(self):
        for q in ("?from=abc", "?limit=abc", "?ok=maybe", "?to="):
            with self.subTest(q=q):
                code, _ = self.get("/api/notify/history" + q)
                self.assertEqual(code, 200, "坏参数应被吞掉而不是 500")

    def test_50_other_routes_still_work(self):
        """新分支插在 /api/status 后面，不能把后面的 if 链截断。"""
        for p in ("/api/health", "/api/status", "/api/link", "/api/config"):
            with self.subTest(path=p):
                code, _ = self.get(p)
                self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
