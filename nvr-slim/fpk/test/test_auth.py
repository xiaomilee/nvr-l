#!/usr/bin/env python3
"""API 凭证（auth）测试（0.9.120）。

守四件事：

  1. **该拦的拦住**：`/api/*` 默认要凭证，只有登录入口的几条放行 —— 判断写成
     白名单，漏一条就是整片接口裸奔，所以每条要逐个断言（含前缀相近的路径）。
  2. **比对照旧常量时间**：`hmac.compare_digest` 跑通、错 token 一律 False、
     空值一律 False。空 token 放行过一次，等于谁都能进。
  3. **cookie 是有状态的那一层**：签出来能验、过期不能验、改一位签名不能验、
     换 token 后旧 cookie 必须失效（不然"轮换"只是换了个装饰）。
  4. **落盘 0600**：凭证文件与录像目录不同，它不该被同机其他账号读到。

跑：python test/test_auth.py
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app", "server"))

import auth  # noqa: E402


def tmp_var():
    d = tempfile.mkdtemp(prefix="nvrsec-")
    return d


class TestPublicPaths(unittest.TestCase):
    def test_root_is_public(self):
        self.assertTrue(auth.is_public("/"))

    def test_index_is_public(self):
        self.assertTrue(auth.is_public("/index.html"))

    def test_static_asset_is_public(self):
        self.assertTrue(auth.is_public("/images/icon-64.png"))

    def test_auth_status_is_public(self):
        self.assertTrue(auth.is_public("/api/auth/status"))

    def test_auth_login_is_public(self):
        self.assertTrue(auth.is_public("/api/auth/login"))

    def test_rotate_requires_token(self):
        # 轮换要是也放行，谁都能把主人锁在门外
        self.assertFalse(auth.is_public("/api/auth/rotate"))

    def test_status_requires_token(self):
        self.assertFalse(auth.is_public("/api/status"))

    def test_health_requires_token(self):
        self.assertFalse(auth.is_public("/api/health"))

    def test_media_requires_token(self):
        # 实时流/回放是最容易被直接拿到的两条，必须设防
        self.assertFalse(auth.is_public("/api/live/stream/cam-01"))
        self.assertFalse(auth.is_public("/api/record/file"))

    def test_prefix_is_not_enough(self):
        # "/api/auth/statusx" 不能被当成公开路径 —— 白名单必须是精确匹配
        self.assertFalse(auth.is_public("/api/auth/statusx"))
        self.assertFalse(auth.is_public("/api/auth/login/../config"))


class TestTokenFile(unittest.TestCase):
    def test_first_load_creates_token(self):
        d = tmp_var()
        tok = auth.load(d)["api_token"]
        self.assertTrue(len(tok) >= 32)
        self.assertTrue(os.path.isfile(auth.secret_path(d)))

    def test_second_load_is_stable(self):
        d = tmp_var()
        a = auth.load(d)["api_token"]
        b = auth.load(d)["api_token"]
        self.assertEqual(a, b)

    def test_file_mode_0600(self):
        if os.name == "nt":
            self.skipTest("Windows 上 chmod 不生效，权限断言只在 Linux 有意义")
        d = tmp_var()
        auth.load(d)
        mode = stat.S_IMODE(os.stat(auth.secret_path(d)).st_mode)
        self.assertEqual(mode, 0o600)

    def test_created_at_present(self):
        d = tmp_var()
        self.assertTrue(auth.load(d)["created_at"])

    def test_broken_file_regenerates(self):
        # secret.json 被写坏不能让服务起不来，也不能因此放行
        d = tmp_var()
        with open(auth.secret_path(d), "w", encoding="utf-8") as f:
            f.write("{不是 json")
        tok = auth.load(d)["api_token"]
        self.assertTrue(tok)
        self.assertTrue(auth.verify(d, tok))

    def test_env_overrides_file(self):
        d = tmp_var()
        auth.load(d)
        with mock.patch.dict(os.environ, {auth.ENV_TOKEN: "env-token-for-test"}):
            self.assertEqual(auth.token(d), "env-token-for-test")
            self.assertTrue(auth.verify(d, "env-token-for-test"))


class TestVerify(unittest.TestCase):
    def test_right_token(self):
        d = tmp_var()
        tok = auth.load(d)["api_token"]
        self.assertTrue(auth.verify(d, tok))

    def test_wrong_token(self):
        d = tmp_var()
        auth.load(d)
        self.assertFalse(auth.verify(d, "nope"))

    def test_empty_given(self):
        d = tmp_var()
        auth.load(d)
        self.assertFalse(auth.verify(d, ""))
        self.assertFalse(auth.verify(d, None))

    def test_no_secret_file_no_pass(self):
        # 凭证读不到时必须拒绝，不能"读不到就当成不需要"
        d = tmp_var()
        with mock.patch.dict(os.environ, {auth.ENV_TOKEN: ""}):
            with mock.patch.object(auth, "load", return_value={"api_token": ""}):
                self.assertFalse(auth.verify(d, "anything"))


class TestBearer(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(auth.bearer("Bearer abc"), "abc")

    def test_case_insensitive_scheme(self):
        self.assertEqual(auth.bearer("bearer abc"), "abc")

    def test_extra_spaces(self):
        self.assertEqual(auth.bearer("  Bearer   abc  "), "abc")

    def test_other_scheme(self):
        self.assertEqual(auth.bearer("Basic dXNlcjpwYXNz"), "")

    def test_missing(self):
        self.assertEqual(auth.bearer(None), "")
        self.assertEqual(auth.bearer(""), "")


class TestCookie(unittest.TestCase):
    def test_signed_cookie_passes(self):
        d = tmp_var()
        auth.load(d)
        self.assertTrue(auth.cookie_ok(d, "%s=%s" % (auth.COOKIE_NAME,
                                                     auth.cookie_value(d))))

    def test_missing_cookie(self):
        d = tmp_var()
        auth.load(d)
        self.assertFalse(auth.cookie_ok(d, ""))
        self.assertFalse(auth.cookie_ok(d, None))
        self.assertFalse(auth.cookie_ok(d, "other=1"))

    def test_expired_cookie(self):
        d = tmp_var()
        auth.load(d)
        self.assertFalse(auth.cookie_ok(d, "%s=%s" % (auth.COOKIE_NAME,
                                                      auth.cookie_value(d, ttl=-10))))

    def test_tampered_signature(self):
        d = tmp_var()
        auth.load(d)
        exp, _, sig = auth.cookie_value(d).partition(".")
        bad = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        self.assertFalse(auth.cookie_ok(d, "%s=%s.%s" % (auth.COOKIE_NAME, exp, bad)))

    def test_extension_of_expiry_is_not_enough(self):
        # 只改过期时间、签名不变 —— 签名里带了 exp，所以必然验不过
        d = tmp_var()
        auth.load(d)
        exp, _, sig = auth.cookie_value(d).partition(".")
        self.assertFalse(auth.cookie_ok(d, "%s=%d.%s"
                                        % (auth.COOKIE_NAME, int(exp) + 9999, sig)))

    def test_rotate_kills_old_cookie(self):
        d = tmp_var()
        auth.load(d)
        ck = auth.cookie_value(d)
        auth.rotate(d)
        self.assertFalse(auth.cookie_ok(d, "%s=%s" % (auth.COOKIE_NAME, ck)))

    def test_cookie_from_other_install(self):
        d1, d2 = tmp_var(), tmp_var()
        auth.load(d1)
        auth.load(d2)
        self.assertFalse(auth.cookie_ok(d2, "%s=%s" % (auth.COOKIE_NAME,
                                                       auth.cookie_value(d1))))


class TestRotate(unittest.TestCase):
    def test_rotate_changes_token(self):
        d = tmp_var()
        old = auth.load(d)["api_token"]
        new = auth.rotate(d)["api_token"]
        self.assertNotEqual(old, new)

    def test_old_token_dead_after_rotate(self):
        d = tmp_var()
        old = auth.load(d)["api_token"]
        auth.rotate(d)
        self.assertFalse(auth.verify(d, old))
        self.assertTrue(auth.verify(d, auth.load(d)["api_token"]))

    def test_rotate_persisted(self):
        # 只改内存不落盘 = 重启后旧 token 复活，比不轮换更危险
        d = tmp_var()
        auth.load(d)
        new = auth.rotate(d)["api_token"]
        with open(auth.secret_path(d), "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["api_token"], new)

    def test_rotate_refuses_env_token(self):
        d = tmp_var()
        with mock.patch.dict(os.environ, {auth.ENV_TOKEN: "fixed-by-env"}):
            with self.assertRaises(RuntimeError):
                auth.rotate(d)


class TestMask(unittest.TestCase):
    def test_mask_hides_middle(self):
        d = tmp_var()
        tok = auth.load(d)["api_token"]
        m = auth.mask(tok)
        self.assertNotIn(tok[6:-6], m)
        self.assertIn(tok[:4], m)

    def test_mask_short(self):
        self.assertEqual(auth.mask("short"), "***")


if __name__ == "__main__":
    unittest.main(verbosity=2)
