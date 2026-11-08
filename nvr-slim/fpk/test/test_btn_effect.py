#!/usr/bin/env python3
"""按钮功能验收表：设置项改完，要看到后端**真的**变了（0.9.123）。

为什么要有这套测试：设置页以前只验证「界面上的数字变了」，接口返回 ok、
config.json 也写进去了，但运行中的对象未必重新读过配置。把「待处理上限」
从 200 改到 333，界面显示 333、/api/config 回读也是 333，而 link 的背压
闸门仍按 200 限流 —— 直到重启服务才生效，界面却没有任何提示。这类
「改了不生效」在 UI 层和回读层都看不见，所以每个设置项必须钉住一条
**生效证据**：从 /api/link 读运行中的 Link / Recorder 对象状态，
而不是再读一遍配置（那只是把 UI 值换了个地方读）。

四层证据，缺一层不算过：
  1. POST 回执 ok=True 且 changed 含该键
  2. GET /api/config 回读 == 期望值
  3. 磁盘 config.json == 期望值
  4. /api/link 对应字段 == 期望值（运行中对象，即热生效）

真起一个本地服务而不是 mock handler：这条链的价值就在「按钮按下去之后
真的走了一遍 HTTP → 落盘 → reconfigure」，mock 掉任何一环都会把 bug mock 没。
服务绑 127.0.0.1、端口由系统分配、临时目录跑完即删，不碰测试机与生产机；
record_enable=False 且本机没有 ffmpeg，不会真的拉流或写录像。
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

# 验收表：加一行就多一条自动化用例，不用复制粘贴一个 test 方法。
# effects 是 /api/link 里的字段路径 —— 只允许写「运行中对象的状态」，
# 不许写 config 回读路径，否则这一项就退化成「只查 UI 值」的另一种说法。
CASES = [
    dict(label="衔接模式", key="link_mode", value="scheduled",
         effects=[("mode",)], expect="scheduled",
         why="切换即刻/定时压制，决定分片是立刻入队还是等批量时刻"),
    dict(label="定时压制间隔", key="link_interval_h", value=3,
         effects=[("interval_h",)], expect=3,
         why="定时模式下 next_batch 的步长，改错会整天不出片段"),
    dict(label="分片长度", key="record_segment_sec", value=120,
         effects=[("segment_sec",), ("record", "segment_sec")], expect=120,
         why="同时决定 ffmpeg 封口周期与回放按秒定位的基准，两处不一致会跳错秒"),
    dict(label="切分方式", key="clip_style", value="split",
         effects=[("clip_style",)], expect="split",
         why="拼接 vs 活动切片走两条完全不同的导出路径"),
    dict(label="待处理上限", key="link_max_queue", value=333,
         effects=[("max_queue",)], expect=333,
         why="背压硬闸门：队列到这个数就暂缓入队，界面改了不生效会静默堆 backlog"),
]


def dig(d, path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


class BtnEffect(unittest.TestCase):
    """整套用例共用一个服务进程：起一次约几秒，逐项改、逐项验。"""

    @classmethod
    def setUpClass(cls):
        cls.var = tempfile.mkdtemp(prefix="nvrbtn-")
        app = os.path.join(cls.var, "app")
        ui = os.path.join(app, "ui")
        os.makedirs(ui, exist_ok=True)
        shutil.copy(os.path.join(ROOT, "app", "ui", "index.html"),
                    os.path.join(ui, "index.html"))
        rec = os.path.join(cls.var, "rec")
        os.makedirs(rec, exist_ok=True)
        cfg = {"record_dir": rec, "record_enable": False, "cameras": [
            {"id": "cam-01", "name": "验收用", "url": "rtsp://u:p@127.0.0.1:1/s1",
             "sub_url": "", "enabled": True, "full_days": 4, "clip_days": 90}]}
        with io.open(os.path.join(cls.var, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

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
        cls.log_tail = ""
        # 凭证是服务启动时自己写下来的，不先读它每一步都是 401，
        # 「服务没起来」会是假象（0.9.120 起所有 /api/* 都要凭证）。
        for _ in range(80):
            time.sleep(0.25)
            if cls.proc.poll() is not None:
                break
            try:
                with io.open(os.path.join(cls.var, "secret.json"), encoding="utf-8") as f:
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
        raise RuntimeError("本地服务没起来（端口 %s，日志 %s）"
                           % (cls.port, cls.log_tail[-1000:]))

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log_tail = (cls.proc.stdout.read() or b"").decode("utf-8", "replace")
        shutil.rmtree(cls.var, ignore_errors=True)

    @classmethod
    def req(cls, path, method="GET", body=None):
        h = {"X-NVR-Request": "1", "Content-Type": "application/json"}
        if cls.token:
            h["Authorization"] = "Bearer " + cls.token
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (cls.port, path),
                                   data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    @classmethod
    def get(cls, path):
        code, body = cls.req(path)
        if code != 200:
            raise AssertionError("GET %s 返回 %s：%s" % (path, code, body[:200]))
        return json.loads(body)

    @classmethod
    def disk(cls):
        with io.open(os.path.join(cls.var, "config.json"), encoding="utf-8") as f:
            return json.load(f)

    def assert_effect(self, case):
        """一个设置项走完四层证据。顺序不能换：先改，再看落盘，最后看运行对象。"""
        key, value, expect = case["key"], case["value"], case["expect"]
        with self.subTest(层="1/4 POST 回执"):
            code, body = self.req("/api/config", "POST", {key: value})
            d = json.loads(body)
            self.assertEqual(code, 200, body[:200])
            self.assertIs(d.get("ok"), True, d)
            self.assertIn(key, d.get("changed") or [],
                          "接口没把它算作改动：%s" % d.get("changed"))
        with self.subTest(层="2/4 接口回读"):
            self.assertEqual(self.get("/api/config")["config"].get(key), expect)
        with self.subTest(层="3/4 磁盘落盘"):
            self.assertEqual(self.disk().get(key), expect)
        with self.subTest(层="4/4 运行中对象"):
            link = self.get("/api/link")
            self.assertIs(link.get("ok"), True, link)
            for path in case["effects"]:
                self.assertEqual(dig(link, path), expect,
                                 "%s 改了但 %s 没跟着变（%s）"
                                 % (key, ".".join(path), case["why"]))

    def test_00_service_ready(self):
        d = self.get("/api/link")
        self.assertIs(d.get("ok"), True)
        self.assertIsInstance(d.get("max_queue"), int)
        self.assertIsInstance(d.get("record"), dict)

    def test_90_table_shape(self):
        """验收表本身也要被验：每项必须声明「运行中对象」的证据路径。"""
        self.assertEqual(len({c["key"] for c in CASES}), len(CASES), "重复的设置项")
        for c in CASES:
            with self.subTest(项=c["key"]):
                self.assertTrue(c["effects"], "没写生效证据，等于只查 UI 值")
                self.assertTrue(c["why"], "没写为什么，下一个人会把这项删掉")
                for path in c["effects"]:
                    self.assertTrue(path)
                    self.assertNotIn("config", path,
                                     "配置回读不是生效证据：%s" % (path,))

    def test_91_invalid_value_does_not_leak(self):
        """非法枚举值要被收敛，且不能把运行中的对象带歪。"""
        code, body = self.req("/api/config", "POST", {"link_mode": "no-such-mode"})
        self.assertEqual(code, 200, body[:200])
        self.assertEqual(self.get("/api/config")["config"].get("link_mode"), "live")
        self.assertEqual(self.get("/api/link").get("mode"), "live")

    def test_92_policy_route_also_hot_applies(self):
        """设置页还有一条 /api/link/policy 入口，热生效不能只修 /api/config 那条。"""
        code, body = self.req("/api/link/policy", "POST", {"link_max_queue": 444})
        self.assertEqual(code, 200, body[:200])
        self.assertIs(json.loads(body).get("ok"), True)
        self.assertEqual(self.get("/api/link").get("max_queue"), 444)
        self.assertEqual(self.disk().get("link_max_queue"), 444)


def _bind(case, idx):
    def test(self):
        self.assert_effect(case)
    test.__name__ = "test_%02d_%s" % (idx, case["key"])
    test.__doc__ = "%s：%s" % (case["label"], case["why"])
    return test


for _i, _c in enumerate(CASES, 1):
    setattr(BtnEffect, "test_%02d_%s" % (_i, _c["key"]), _bind(_c, _i))


if __name__ == "__main__":
    unittest.main(verbosity=2)
