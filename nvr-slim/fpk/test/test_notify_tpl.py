#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify_tpl.py 单测：平台判定 / 最小 body / 模板插值与非法回退（0.9.124）。

为什么不测「发送成功」：本模块纯计算，不碰网络；发得出去发不出去是
test_notify.py 的职责。这里只钉住「给这个 URL 该出这个 body」与
「坏模板不许把整条通知搞丢」两件事。

跑：python test/test_notify_tpl.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "server"))
import notify_tpl as tpl  # noqa: E402

T0 = 1793500000.0
ITEM = {"cam": "院子", "cat": "person", "t": T0}


class TestPlatformOf(unittest.TestCase):
    def test_known_hosts(self):
        cases = {
            "https://open.feishu.cn/open-apis/bot/v2/hook/xxx": "feishu",
            "https://open.larksuite.com/open-apis/bot/v2/hook/x": "feishu",
            "https://oapi.dingtalk.com/robot/send?access_token=x": "dingtalk",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x": "wecom",
            "https://sctapi.ftqq.com/ABC.send": "serverchan",
            "https://sc.ftqq.com/ABC.send": "serverchan",
            "https://api.day.app/ABC": "bark",
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(tpl.platform_of(url), want)

    def test_unknown_is_generic(self):
        """本地测试服务器 / 自建钩子 / 空 URL 一律回退，不能因认不出就不发。"""
        for url in ("http://127.0.0.1:8080/hook", "https://hooks.example.com/x",
                    "http://192.168.2.152:9999/x", "", None, "not a url"):
            with self.subTest(url=url):
                self.assertEqual(tpl.platform_of(url), tpl.GENERIC)

    def test_case_insensitive(self):
        self.assertEqual(tpl.platform_of("HTTPS://OPEN.FEISHU.CN/x"), "feishu")

    def test_host_suffix_not_substring(self):
        """open.feishu.cn.evil.example 不能冒充飞书，否则模板会被诱导。"""
        self.assertEqual(tpl.platform_of("https://open.feishu.cn.evil.example/x"),
                         tpl.GENERIC)
        self.assertEqual(tpl.platform_of("https://evilopen.feishu.cn/x"),
                         tpl.GENERIC)

    def test_subdomain_of_known_host_is_ok(self):
        self.assertEqual(tpl.platform_of("https://proxy.open.feishu.cn/x"),
                         "feishu")


class TestRender(unittest.TestCase):
    def test_every_platform_emits_minimal_body(self):
        """每个平台只出自己那套键，不许多塞别人的（有的机器人会校验形状）。"""
        expect = {
            "https://open.feishu.cn/x": {"msg_type", "content"},
            "https://oapi.dingtalk.com/x": {"msgtype", "text"},
            "https://qyapi.weixin.qq.com/x": {"msgtype", "text"},
            "https://sctapi.ftqq.com/x": {"title", "desp"},
            "https://api.day.app/x": {"title", "body", "group"},
        }
        for url, keys in expect.items():
            with self.subTest(url=url):
                body, p = tpl.render(url, ITEM)
                self.assertEqual(set(body), keys)
                self.assertNotEqual(p, tpl.GENERIC)

    def test_generic_keeps_all_four_key_sets(self):
        """兼容底线：认不出来的目标仍收到旧的胖 JSON，四套键一个不少。"""
        body, p = tpl.render("http://127.0.0.1:8080/hook", ITEM)
        self.assertEqual(p, tpl.GENERIC)
        for k in ("msg_type", "content", "msgtype", "text", "title", "desp"):
            self.assertIn(k, body)

    def test_generic_text_matches_legacy_wording(self):
        """旧版文案不能变：前端与用户的截图/告警规则都认这句话。"""
        body, _ = tpl.render("", ITEM)
        self.assertIn("院子", body["content"]["text"])
        self.assertIn("检测到人员", body["content"]["text"])
        self.assertEqual(body["content"]["text"], body["text"]["content"])
        self.assertEqual(body["content"]["text"], body["desp"])

    def test_payload_is_json_serializable(self):
        for url in ("https://open.feishu.cn/x", "https://api.day.app/x", ""):
            with self.subTest(url=url):
                body, _ = tpl.render(url, ITEM)
                json.dumps(body, ensure_ascii=False)

    def test_category_labels(self):
        for cat, zh in (("vehicle", "车辆"), ("two_wheeler", "两轮车"),
                        ("motion", "移动侦测")):
            with self.subTest(cat=cat):
                body, _ = tpl.render("", dict(ITEM, cat=cat))
                self.assertIn(zh, body["content"]["text"])

    def test_unknown_category_falls_back_to_raw(self):
        body, _ = tpl.render("", dict(ITEM, cat="dog"))
        self.assertIn("dog", body["content"]["text"])

    def test_no_url_in_text(self):
        """URL 里的 key / token 不许被拼进消息体。"""
        url = "https://open.feishu.cn/x?key=SUPERSECRET123"
        body, _ = tpl.render(url, ITEM)
        self.assertNotIn("SUPERSECRET123", json.dumps(body, ensure_ascii=False))

    def test_missing_time_does_not_raise(self):
        body, _ = tpl.render("", {"cam": "院子", "cat": "person"})
        self.assertIn("院子", body["content"]["text"])


class TestTextOf(unittest.TestCase):
    def test_all_variables(self):
        s = tpl.text_of(ITEM, "{cam}|{cat}|{label}|{time}|{date}")
        self.assertEqual(s.split("|")[:3], ["院子", "person", "人员"])
        self.assertEqual(len(s.split("|")), 5)

    def test_default_template_when_none(self):
        self.assertEqual(tpl.text_of(ITEM), tpl.text_of(ITEM, tpl.DEFAULT_TPL))

    def test_repeated_variables(self):
        self.assertEqual(tpl.text_of(ITEM, "{cam}{cam}"), "院子院子")

    def test_special_chars_survive_json(self):
        body, _ = tpl.render("", dict(ITEM, cam='院子"引号\n换行'))
        json.dumps(body, ensure_ascii=False)
        self.assertIn("引号", body["content"]["text"])

    def test_field_length_capped(self):
        body, _ = tpl.render("", dict(ITEM, cam="A" * 500))
        self.assertLessEqual(len(body["content"]["text"]), tpl.MAX_TEXT_LEN)


class TestNormalizeTpl(unittest.TestCase):
    def test_empty_uses_default(self):
        self.assertEqual(tpl.normalize_tpl("")[0], tpl.DEFAULT_TPL)
        self.assertEqual(tpl.normalize_tpl(None)[0], tpl.DEFAULT_TPL)

    def test_valid_custom(self):
        got, why = tpl.normalize_tpl("{cam} 有人：{label} @{time}")
        self.assertEqual(why, "")
        self.assertIn("有人", got)

    def test_unknown_placeholder_reverts(self):
        got, why = tpl.normalize_tpl("{cam} {url} {token}")
        self.assertEqual(got, tpl.DEFAULT_TPL)
        self.assertIn("未知占位符", why)

    def test_too_long_reverts(self):
        got, why = tpl.normalize_tpl("x" * (tpl.MAX_TPL_LEN + 1))
        self.assertEqual(got, tpl.DEFAULT_TPL)
        self.assertIn("超过", why)

    def test_non_string_reverts(self):
        for v in (123, [], {"a": 1}, True):
            with self.subTest(v=v):
                got, why = tpl.normalize_tpl(v)
                self.assertEqual(got, tpl.DEFAULT_TPL)
                self.assertTrue(why)

    def test_blank_reverts(self):
        self.assertEqual(tpl.normalize_tpl("   ")[0], tpl.DEFAULT_TPL)

    def test_bad_template_still_renders(self):
        """不变式：坏模板不许把整条通知搞丢。"""
        body, p = tpl.render("https://open.feishu.cn/x", ITEM, "{unknown_var}")
        self.assertEqual(p, "feishu")
        self.assertIn("院子", body["content"]["text"])

    def test_whitelist_excludes_url_and_token(self):
        self.assertNotIn("url", tpl.VARS)
        self.assertNotIn("token", tpl.VARS)
        self.assertEqual(len(tpl.VARS), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
