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
本机没有 ffmpeg，不会真的拉流或写录像（启动时 record_enable=True 也没关系）。

第二组补的是「换个入口按，也要生效」：设置页还有 /api/link/policy，它同样
写 record_enable / record_segment_sec 并落盘，但以前只通知了 Link。另外
/api/link 顶层的 record_enable 是 link_payload() 现读 config.json 得来的，
跟 Recorder 真在不在录没有必然关系 —— 所以生效证据只认 record.enabled，
不认那个同名的回读字段（test_90 会把写错的证据路径挡下来）。
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
    dict(label="录像总开关", key="record_enable", value=False,
         effects=[("record", "enabled")], expect=False,
         why="关掉录像后 Recorder 必须真的停；只让配置回读变成 false 等于没关"),
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
        # 录像总闸要从「开」改到「关」才测得出热生效；以 False 起手的话
        # POST 同值不会进 changed，用例会变成什么都没按的假绿。
        # 本机没有 ffmpeg，开着也只会建对象、不会真拉流写盘。
        cfg = {"record_dir": rec, "record_enable": True, "cameras": [
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
                    # /api/link 顶层的 record_enable 是 link_payload() 现读
                    # config.json 补的，Recorder 未必跟着变过（policy 入口那个
                    # bug 就是这样瞒过第一组验收表的）。
                    self.assertNotEqual(
                        tuple(path), ("record_enable",),
                        "顶层 record_enable 是配置回读，不是生效证据")

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

    def test_93_policy_route_applies_to_recorder(self):
        """第二条入口改录像总闸 / 分片长度，Recorder 也必须跟着变。

        /api/link/policy 会写这两个键并落盘，以前只通知 Link：接口回 ok、
        /api/config 回读也对，录制却仍按旧参数跑。分片长度尤其危险 ——
        Link 按新秒数入队、ffmpeg 按旧秒数封口，回放按秒定位会跳错片段；
        「关掉录像」也可能根本没关。用当前值的反面起手，不依赖前面的用例顺序。
        """
        before = self.get("/api/link")
        new_on = not bool((before.get("record") or {}).get("enabled"))
        new_seg = 180 if before.get("segment_sec") != 180 else 240
        code, body = self.req("/api/link/policy", "POST",
                              {"record_enable": new_on,
                               "record_segment_sec": new_seg})
        self.assertEqual(code, 200, body[:200])
        self.assertIs(json.loads(body).get("ok"), True)
        with self.subTest(层="磁盘落盘"):
            self.assertEqual(self.disk().get("record_enable"), new_on)
            self.assertEqual(self.disk().get("record_segment_sec"), new_seg)
        after = self.get("/api/link")
        with self.subTest(层="运行中 Recorder"):
            self.assertEqual((after.get("record") or {}).get("enabled"), new_on)
            self.assertEqual((after.get("record") or {}).get("segment_sec"), new_seg)
        with self.subTest(层="Link 与 Recorder 同一个秒数"):
            self.assertEqual(after.get("segment_sec"), new_seg)

    def test_94_policy_bool_string_not_truthy(self):
        """录制总闸的字符串 "false" 不能被当成"开"（两条入口必须同口径）。

        /api/config 走 runtime.coerce，把 "false" / "0" 收敛成 False；
        policy 入口以前是裸 bool("false")，结果是 True —— 用户从设置页能
        关掉录像，同样一个值从衔接策略这条入口进来却把录像打开了，
        接口还照样回 ok。录制总闸最不能有歧义，所以这里不验正常布尔值，
        专拿字符串来钉住两条入口给出同一个结论。

        用例自己先把总闸设成"开"再"关"，不依赖前面用例留下的状态
        （CASES 里有一项会把总闸改成 False，test_93 又会取反）。
        证据取磁盘与运行中的 Recorder，不读 /api/link 顶层那个同名回读字段。
        """
        code, body = self.req("/api/link/policy", "POST", {"record_enable": True})
        self.assertEqual(code, 200, body[:200])
        self.assertIs(json.loads(body).get("ok"), True)
        self.assertIs(self.disk().get("record_enable"), True)
        self.assertIs((self.get("/api/link").get("record") or {}).get("enabled"), True,
                      "起手没把 Recorder 打开，后面这次'关'验不出变化")

        code, body = self.req("/api/link/policy", "POST",
                              {"record_enable": "false"})
        self.assertEqual(code, 200, body[:200])
        self.assertIs(json.loads(body).get("ok"), True)
        with self.subTest(层="磁盘落盘"):
            self.assertIs(self.disk().get("record_enable"), False,
                          '字符串 "false" 落盘成了 True：关录像被判成了开录像')
        with self.subTest(层="运行中 Recorder"):
            self.assertIs((self.get("/api/link").get("record") or {}).get("enabled"),
                          False, "Recorder 仍开着，关录像没有真的生效")
        with self.subTest(层="两条入口同口径"):
            # 同一个字符串走设置页那条入口，coerce 一直给 False；
            # policy 入口此前给 True，正是这个用例要挡住的相反结论。
            self.req("/api/config", "POST", {"record_enable": True})
            code, body = self.req("/api/config", "POST",
                                  {"record_enable": "false"})
            self.assertEqual(code, 200, body[:200])
            self.assertIs(self.get("/api/config")["config"].get("record_enable"),
                          False)
            self.assertIs(self.disk().get("record_enable"), False,
                          "配置入口把总闸关了，不该被 policy 的旧结论带开")


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
