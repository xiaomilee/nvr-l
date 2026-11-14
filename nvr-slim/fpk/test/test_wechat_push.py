#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_push.py 单测：token 缓存 / 40001 重取 / 错误映射 / 不漏密钥（0.9.142）。

为什么不连真微信：这里要钉的是**有状态的部分**（token 缓存在什么时刻失效、
被顶掉之后重取几次、哪些错误不该重试），真接口没法把 40001 稳定复现出来。
真机验证另做一条：152 上用真 appID 发一条，见 ROADMAP 第 45 轮。

唯一出口 `_http_json()` 在这里被整体替换 —— 这也是当初把它单独拎出来的原因：
不替换它就得给 urllib 造一套 mock，测出来的东西和线上跑的不是一条路。

跑：python test/test_wechat_push.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "server"))
import wechat_push as wx  # noqa: E402

FULL = {"appid": "wx08a5ba20eac223dd", "secret": "s" * 32,
        "openid": "oR_cp3WVts0RzDucPkwm64zHRSwk",
        "template_id": "AzxFiUL7U1QXQmqXeL0_RKAUBaPu5fYv1IM6D-vhR38"}


class Base(unittest.TestCase):
    def setUp(self):
        wx.forget_token()
        self.reqs = []          # [(url, payload)]
        self.replies = []       # 依次弹出的返回值
        self._orig = wx._http_json
        wx._http_json = self._fake

    def tearDown(self):
        wx._http_json = self._orig
        wx.forget_token()

    def _fake(self, url, payload=None, timeout=8):
        self.reqs.append((url, payload))
        if self.replies:
            return self.replies.pop(0)
        # 没排队的请求按 URL 给合理默认：换 token 的回 access_token，发消息的回 errcode 0
        if "cgi-bin/token" in url:
            return {"access_token": "T", "expires_in": 7200}, ""
        return {"errcode": 0, "errmsg": "ok"}, ""

    def queue(self, *pairs):
        self.replies.extend(pairs)

    def token_hits(self):
        return [u for u, _p in self.reqs if "cgi-bin/token" in u]

    def send_hits(self):
        return [u for u, _p in self.reqs if "message/template/send" in u]


class TestConfig(Base):
    def test_norm_fills_key(self):
        self.assertEqual(wx.norm({})["key"], "content")
        self.assertEqual(wx.norm({"key": "  "})["key"], "content")
        self.assertEqual(wx.norm({"key": "first"})["key"], "first")

    def test_norm_strips_and_caps(self):
        w = wx.norm({"appid": "  x  ", "secret": "y" * 999})
        self.assertEqual(w["appid"], "x")
        self.assertEqual(len(w["secret"]), wx.MAX_FIELD)

    def test_ready_lists_missing(self):
        ok, miss = wx.ready({})
        self.assertFalse(ok)
        self.assertEqual(miss, ["appID", "appsecret", "openid", "模板 ID"])
        self.assertTrue(wx.ready(FULL)[0])
        # 少了 openid 也算没配好：能换到 token 但发不出去，这种最容易误判「配好了」
        partial = dict(FULL)
        partial["openid"] = ""
        self.assertEqual(wx.ready(partial), (False, ["openid"]))

    def test_mask(self):
        self.assertEqual(wx.mask("wx08a5ba20eac223dd"), "wx08…dd")
        self.assertEqual(wx.mask("abc"), "***")
        self.assertEqual(wx.mask(""), "")

    def test_state_never_leaks_secret(self):
        st = wx.state(FULL)
        blob = json.dumps(st, ensure_ascii=False)
        self.assertNotIn("s" * 32, blob)
        self.assertNotIn(FULL["openid"], blob)
        self.assertTrue(st["ready"])


class TestBody(Base):
    def test_default_key_is_content(self):
        body = wx.build_body(FULL, "hello")
        self.assertEqual(body["touser"], FULL["openid"])
        self.assertEqual(body["template_id"], FULL["template_id"])
        self.assertEqual(body["data"], {"content": {"value": "hello"}})
        self.assertNotIn("url", body)

    def test_custom_key(self):
        """模板写 {{first.DATA}} 的用户要能把键名改成 first，否则只能 40037。"""
        body = wx.build_body(dict(FULL, key="first"), "x")
        self.assertEqual(set(body["data"]), {"first"})

    def test_url_only_when_set(self):
        body = wx.build_body(dict(FULL, url="https://nas.local"), "x")
        self.assertEqual(body["url"], "https://nas.local")

    def test_long_text_truncated(self):
        body = wx.build_body(FULL, "字" * 999)
        self.assertEqual(len(body["data"]["content"]["value"]), wx.MAX_VALUE)

    def test_body_is_json_serializable(self):
        json.dumps(wx.build_body(FULL, "x"), ensure_ascii=False)


class TestToken(Base):
    def test_cached_across_sends(self):
        ok, _ms, err, _can = wx.send(FULL, "a", now=1000)
        self.assertTrue(ok, err)
        ok, _ms, err, _can = wx.send(FULL, "b", now=1001)
        self.assertTrue(ok, err)
        self.assertEqual(len(self.token_hits()), 1, "第二次发送不该再取 token")
        self.assertEqual(len(self.send_hits()), 2)

    def test_refresh_after_ttl(self):
        wx.send(FULL, "a", now=1000)
        wx.send(FULL, "b", now=1000 + wx.TOKEN_TTL + 10)
        self.assertEqual(len(self.token_hits()), 2)

    def test_ttl_respects_expires_in(self):
        self.queue(({"access_token": "T1", "expires_in": 300}, ""),
                   ({"access_token": "T2", "expires_in": 7200}, ""))
        tok1, _e, _c = wx.get_token(FULL["appid"], FULL["secret"], now=1000)
        # 300 秒 - 200 秒余量 = 100 秒；过了这个点必须重取
        tok2, _e, _c = wx.get_token(FULL["appid"], FULL["secret"], now=1001 + 200)
        self.assertEqual(tok1, "T1")
        self.assertEqual(tok2, "T2")

    def test_short_expires_in_floor(self):
        """expires_in 小到没法减余量时，至少留 60 秒，不能算出负数。"""
        self.queue(({"access_token": "T1", "expires_in": 100}, ""),
                   ({"access_token": "T2", "expires_in": 7200}, ""))
        wx.get_token(FULL["appid"], FULL["secret"], now=1000)
        tok, _e, _c = wx.get_token(FULL["appid"], FULL["secret"], now=1030)
        self.assertEqual(tok, "T1", "60 秒内不该重取")
        tok, _e, _c = wx.get_token(FULL["appid"], FULL["secret"], now=1061)
        self.assertEqual(tok, "T2")

    def test_force_ignores_cache(self):
        wx.send(FULL, "a", now=1000)
        wx.get_token(FULL["appid"], FULL["secret"], force=True, now=1001)
        self.assertEqual(len(self.token_hits()), 2)

    def test_token_error_is_not_retryable_for_bad_secret(self):
        self.queue(({"errcode": 40125, "errmsg": "invalid appsecret"}, ""))
        tok, err, can = wx.get_token(FULL["appid"], FULL["secret"], now=1000)
        self.assertEqual(tok, "")
        self.assertIn("appsecret", err)
        self.assertFalse(can)
        self.assertNotIn(FULL["secret"], err)


class TestSend(Base):
    def test_success(self):
        ok, ms, err, can = wx.send(FULL, "【NVR】检测到人员", now=1000)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(self.reqs[0][1], None, "取 token 走 GET，不该带 body")
        self.assertEqual(self.reqs[1][1]["data"],
                         {"content": {"value": "【NVR】检测到人员"}})

    def test_not_configured_short_circuits(self):
        ok, _ms, err, can = wx.send({}, "x")
        self.assertFalse(ok)
        self.assertFalse(can)
        self.assertIn("未配置完整", err)
        self.assertEqual(self.reqs, [], "没配好就不该去打扰微信接口")

    def test_empty_text(self):
        ok, _ms, err, _can = wx.send(FULL, "   ")
        self.assertFalse(ok)
        self.assertIn("内容为空", err)

    def test_40001_refetches_once_and_succeeds(self):
        """token 被顶掉（同 appid 另一个进程取过）是真实会发生的：重取一次就好。"""
        self.queue(({"access_token": "OLD", "expires_in": 7200}, ""),
                   ({"errcode": 40001, "errmsg": "invalid credential"}, ""),
                   ({"access_token": "NEW", "expires_in": 7200}, ""),
                   ({"errcode": 0}, ""))
        ok, _ms, err, _can = wx.send(FULL, "x", now=1000)
        self.assertTrue(ok, err)
        self.assertEqual(len(self.token_hits()), 2, "应该重取一次 token")
        self.assertEqual(len(self.send_hits()), 2)

    def test_unsubscribed_not_retryable(self):
        self.replies = [({"access_token": "T", "expires_in": 7200}, ""),
                        ({"errcode": 43004, "errmsg": "require subscribe"}, "")]
        ok, _ms, err, can = wx.send(FULL, "x", now=1000)
        self.assertFalse(ok)
        self.assertIn("关注", err)
        self.assertFalse(can, "未关注重试没用，别白等 6 秒")
        self.assertEqual(len(self.send_hits()), 1)

    def test_quota_not_retryable(self):
        self.replies = [({"access_token": "T", "expires_in": 7200}, ""),
                        ({"errcode": 45009, "errmsg": "reach max api daily quota"}, "")]
        ok, _ms, err, can = wx.send(FULL, "x", now=1000)
        self.assertFalse(ok)
        self.assertIn("额度", err)
        self.assertFalse(can)

    def test_template_mismatch_hint(self):
        self.replies = [({"access_token": "T", "expires_in": 7200}, ""),
                        ({"errcode": 40037, "errmsg": "invalid template_id"}, "")]
        ok, _ms, err, _can = wx.send(FULL, "x", now=1000)
        self.assertFalse(ok)
        self.assertIn("数据键名", err)

    def test_unknown_errcode_keeps_errmsg(self):
        self.replies = [({"access_token": "T", "expires_in": 7200}, ""),
                        ({"errcode": 12345, "errmsg": "something odd"}, "")]
        ok, _ms, err, _can = wx.send(FULL, "x", now=1000)
        self.assertFalse(ok)
        self.assertIn("12345", err)
        self.assertIn("something odd", err)

    def test_network_error_is_retryable(self):
        self.replies = [({"access_token": "T", "expires_in": 7200}, ""),
                        (None, "URLError: 连接微信接口失败")]
        ok, _ms, err, can = wx.send(FULL, "x", now=1000)
        self.assertFalse(ok)
        self.assertTrue(can, "网络失败值得让上层重试")

    def test_token_fetch_failure_is_retryable(self):
        self.replies = [(None, "URLError: timed out")]
        ok, _ms, err, can = wx.send(FULL, "x", now=1000)
        self.assertFalse(ok)
        self.assertTrue(can)
        self.assertEqual(self.send_hits(), [])


class TestNoLeak(unittest.TestCase):
    """错误信息会进日志、进 notify_history.jsonl、进界面。这三个地方都有人截图。"""

    def test_safe_reason_masks_query(self):
        class E(Exception):
            pass
        s = wx._safe_reason(E("bad url https://api.weixin.qq.com/x?secret=ABCDEF&access_token=XYZ"))
        self.assertNotIn("ABCDEF", s)
        self.assertNotIn("XYZ", s)

    def test_hint_never_contains_secret(self):
        for code in wx.ERRORS:
            msg, _can = wx.hint(code)
            self.assertNotIn("secret=", msg)


if __name__ == "__main__":
    unittest.main(verbosity=1)
