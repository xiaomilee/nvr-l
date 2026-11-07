#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一脱敏口径测试（0.9.120 批次 2 · 清单第 5 项）。

守四件事：
  R1 形态覆盖：RTSP/HTTP 的 user:pass、URL 里的敏感 query、Authorization 头、
     会话 cookie —— 少一种就是一条泄漏路径。
  R2 字面量：api_token 会以任意形态出现（`?token=`、轮换提示、命令行），
     正则认不出"这串就是凭证"，只能按登记表抹。
  R3 幂等 + 可判定：抹过一遍的文本再抹不变；leaked() 因此能当自检用。
  R4 不误伤：主机、端口、路径、参数名、时间戳要留着 —— 抹掉就没法排障了，
     而"看不出这次请求是新是旧"的日志等于没有日志。

跑：python test/test_redact.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import redact  # noqa: E402


class RedactUrlTest(unittest.TestCase):
    def test_rtsp_credentials(self):
        s = redact.mask_text("拉流失败 rtsp://admin:ADNVGC@192.168.2.137:554/Streaming/Channels/101 超时")
        self.assertNotIn("ADNVGC", s)
        self.assertNotIn("admin:", s)
        self.assertIn("rtsp://***@192.168.2.137:554/Streaming/Channels/101", s)

    def test_https_credentials(self):
        """webhook 地址里带账号密码：这类地址会被贴进"通知失败"的日志行"""
        s = redact.mask_text("通知失败 https://hook:s3cret@push.example.com/api?x=1")
        self.assertNotIn("s3cret", s)
        self.assertIn("https://***@push.example.com/api", s)

    def test_rtsps_scheme(self):
        self.assertEqual(redact.mask_text("rtsps://u:p@h/ch"),
                         "rtsps://***@h/ch")

    def test_no_credentials_untouched(self):
        """没凭据的地址一个字都不改：脱敏顺手改了主机名比不脱敏更难查"""
        for s in ("rtsp://192.168.2.137:554/ch1",
                  "http://nas.local:18083/api/status",
                  "录像写入 /vol1/@appshare/nvr-output/cam-01/2026-11-08/00-00-00.mp4"):
            self.assertEqual(redact.mask_text(s), s)


class RedactQueryTest(unittest.TestCase):
    def test_token_query(self):
        s = redact.mask_text("GET /api/status?token=abcDEF123456 HTTP/1.1")
        self.assertNotIn("abcDEF123456", s)
        self.assertIn("/api/status?token=***", s)

    def test_token_param_names(self):
        for name in ("token", "api_token", "api-token", "access_token",
                     "key", "apikey", "api_key", "secret", "password",
                     "passwd", "pwd", "sig", "signature", "auth"):
            raw = "/api/x?%s=leakme123" % name
            self.assertNotIn("leakme123", redact.mask_text(raw), name)

    def test_keeps_other_params(self):
        """不带秘密的参数必须留着：`?date=` / `?cam=` 是排障的主要线索"""
        s = redact.mask_text("/api/health?date=2026-11-08&cam=cam-01&force=1")
        self.assertEqual(s, "/api/health?date=2026-11-08&cam=cam-01&force=1")

    def test_timestamp_param_not_masked(self):
        """`?_t=` 是前端防缓存的毫秒时间戳，抹了就看不出请求新旧"""
        s = "/api/status?_t=1731096000123"
        self.assertEqual(redact.mask_text(s), s)

    def test_token_query_among_others(self):
        s = redact.mask_text("/api/log?lines=200&token=tok123456&end=99")
        self.assertNotIn("tok123456", s)
        self.assertIn("lines=200", s)
        self.assertIn("end=99", s)


class RedactHeaderTest(unittest.TestCase):
    def test_bearer_header(self):
        s = redact.mask_text("Authorization: Bearer abcdefgh12345678")
        self.assertNotIn("abcdefgh12345678", s)
        self.assertIn("Bearer ***", s)

    def test_ffmpeg_headers_arg(self):
        """录制命令行里的 -headers 形态（诊断导出会原样带上）"""
        s = redact.mask_text('-headers "Authorization: Basic YWRtaW46cGFzcw=="')
        self.assertNotIn("YWRtaW46cGFzcw", s)

    def test_session_cookie(self):
        s = redact.mask_text("Cookie: nvr_sess=1731096000.deadbeef")
        self.assertNotIn("deadbeef", s)
        self.assertIn("nvr_sess=***", s)


class RedactSecretTest(unittest.TestCase):
    def setUp(self):
        redact.forget_secrets()

    def tearDown(self):
        redact.forget_secrets()

    def test_literal_secret(self):
        tok = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abc"
        redact.add_secret(tok)
        s = redact.mask_text("轮换完成，新凭证 %s" % tok)
        self.assertNotIn(tok, s)
        self.assertIn(redact.mask_secret(tok), s)

    def test_literal_secret_in_url(self):
        """`?token=<真 token>`：参数名规则先抹，字面量规则兜底，两遍都不该留全串"""
        tok = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abc"
        redact.add_secret(tok)
        self.assertNotIn(tok, redact.mask_text("/api/status?token=" + tok))

    def test_short_value_not_registered(self):
        """太短的值不当凭证：'abc' 会在任何文本里命中，把日志抹成筛子"""
        redact.add_secret("abc")
        self.assertEqual(redact.mask_text("abc 三个字母"), "abc 三个字母")

    def test_add_secret_idempotent(self):
        """重复登记不能让同一处被抹两遍（会得到 `0123…cdef…cdef` 这种鬼东西）"""
        redact.add_secret("0123456789abcdef")
        redact.add_secret("0123456789abcdef")
        self.assertEqual(redact.mask_text("0123456789abcdef"),
                         redact.mask_secret("0123456789abcdef"))

    def test_forget(self):
        redact.add_secret("0123456789abcdef")
        redact.forget_secrets()
        self.assertEqual(redact.mask_text("0123456789abcdef"), "0123456789abcdef")


class RedactContractTest(unittest.TestCase):
    def test_empty_safe(self):
        self.assertEqual(redact.mask_text(""), "")
        self.assertEqual(redact.mask_text(None), "")

    def test_idempotent(self):
        raw = ("rtsp://admin:pw@10.0.0.1/ch?token=tok123456 "
               "Authorization: Bearer abcdefgh12345678")
        once = redact.mask_text(raw)
        self.assertEqual(redact.mask_text(once), once)

    def test_leaked_detects_and_clears(self):
        raw = "rtsp://admin:pw@10.0.0.1/ch"
        self.assertTrue(redact.leaked(raw))
        self.assertFalse(redact.leaked(redact.mask_text(raw)))

    def test_leaked_literal(self):
        redact.add_secret("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abc")
        self.assertTrue(redact.leaked("token=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abc"))
        redact.forget_secrets()

    def test_multiple_secrets_one_line(self):
        s = redact.mask_text("src rtsp://u1:p1@a/ch → dst rtsp://u2:p2@b/ch")
        self.assertNotIn("p1", s)
        self.assertNotIn("p2", s)
        self.assertEqual(s.count("***@"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
