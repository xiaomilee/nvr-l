#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知设置页的接线守卫：控件、配置键、状态出口三者必须对得上（0.9.124）。

为什么需要这份测试
------------------
第 23 轮的设置项验收表照出四处「假设置」：界面上有控件、按下去有反馈，
后端永远收不到。这一批又新增两个配置键（`notify.platform` / `notify.template`），
正是同一个坑的高危区：

  * 加进 `DEFAULTS` 却没进 `norm_notify()` → 每次读盘被静默丢掉，改了不生效；
  * 加进前端 `pushCfg()` 却没进 `DEFAULTS` → 服务端 coerce 直接丢弃；
  * 控件写了 `data-ntplat` 但没绑事件 → 点了没反应；
  * 平台白名单在前后端各抄一份 → 一边多一个值，保存被打回 auto。

所以这里不测「渲染出来好不好看」，只测**接线**：值从控件到磁盘到状态接口
这一条链上，每一段都有断言。真正的 DOM 行为在 `test/ui/settings.test.js`。

跑：python test/test_notify_ui.py
"""
import io
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import detect          # noqa: E402
import notify          # noqa: E402
import notify_tpl      # noqa: E402
import runtime         # noqa: E402

UI = os.path.join(ROOT, "app", "ui", "js")
CARDS = os.path.join(UI, "pages", "settings_cards.js")
WIRING = os.path.join(UI, "pages", "diagnostics.js")
LIVE = os.path.join(UI, "pages", "live.js")
STATE = os.path.join(UI, "core", "state.js")


def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


class PlatformParity(unittest.TestCase):
    """平台白名单只许有一份真相（服务端），前端那份是兜底且必须逐项相同。"""

    def setUp(self):
        self.js = read(CARDS)

    def test_10_frontend_fallback_matches_server(self):
        m = re.search(r"const NT_PLATFORMS = \[(.*?)\];", self.js, re.S)
        self.assertIsNotNone(m, "settings_cards.js 里找不到 NT_PLATFORMS 兜底表")
        pairs = re.findall(r'\["([a-z]+)",\s*"([^"]+)"\]', m.group(1))
        self.assertTrue(pairs, "兜底表解析不出 (值, 标签) 对")
        self.assertEqual([v for v, _ in pairs], list(notify_tpl.PLATFORMS),
                         "前端兜底的平台顺序/取值与服务端不一致")
        for v, label in pairs:
            self.assertEqual(label, notify_tpl.PLATFORM_LABEL[v],
                             "平台 %s 的中文名两边不一致" % v)

    def test_20_detect_whitelist_is_the_same_tuple(self):
        """norm_notify 的枚举必须就是 notify_tpl.PLATFORMS，别另抄一份。"""
        self.assertEqual(tuple(detect.NOTIFY_ENUMS["platform"]),
                         tuple(notify_tpl.PLATFORMS))


class ConfigKeys(unittest.TestCase):
    """两个新键必须同时出现在 DEFAULTS 与 norm_notify 的输出里。"""

    def test_30_defaults_and_normalizer_agree(self):
        d = runtime.DEFAULTS["notify"]
        got = detect.norm_notify({})
        self.assertEqual(set(got), set(d),
                         "DEFAULTS 与 norm_notify 的键集合不同：多了 %s / 少了 %s"
                         % (sorted(set(got) - set(d)), sorted(set(d) - set(got))))

    def test_40_platform_enum_falls_back_to_auto(self):
        self.assertEqual(detect.norm_notify({"platform": "bogus"})["platform"],
                         "auto", "写错平台值应当回落 auto，而不是原样落盘")
        self.assertEqual(detect.norm_notify({"platform": "DINGTALK"})["platform"],
                         "dingtalk", "大小写不该让手选失效")

    def test_50_template_kept_and_clamped(self):
        """非法占位符原样留着：渲染时回退，界面才能解释「你写的这句没生效」。"""
        tpl = "{cam} 有人 {bogus}"
        self.assertEqual(detect.norm_notify({"template": tpl})["template"], tpl)
        self.assertEqual(len(detect.norm_notify({"template": "x" * 900})["template"]),
                         500)


class StatusOut(unittest.TestCase):
    """status().render 是设置页那句「实际怎么发」的唯一出处。"""

    def test_60_forced_platform_reported(self):
        n = notify.Notifier(lambda: {"notify": {"webhook": "https://gw.example.com/h",
                                                "platform": "dingtalk",
                                                "template": "{cam}-{label}"}})
        n.reconfigure({"notify": {"webhook": "https://gw.example.com/h",
                                  "platform": "dingtalk",
                                  "template": "{cam}-{label}"}})
        r = n.status()["render"]
        self.assertEqual(r["platform"], "dingtalk")
        self.assertFalse(r["auto"], "手选的平台不该标成自动识别")
        self.assertTrue(r["custom_template"])
        self.assertEqual(r["template_why"], "")

    def test_70_auto_platform_uses_host(self):
        n = notify.Notifier(lambda: {"notify": {}})
        n.reconfigure({"notify": {"webhook": "https://open.feishu.cn/x"}})
        r = n.status()["render"]
        self.assertEqual((r["platform"], r["auto"]), ("feishu", True))
        self.assertFalse(r["custom_template"])

    def test_80_bad_template_reports_reason(self):
        n = notify.Notifier(lambda: {"notify": {}})
        n.reconfigure({"notify": {"webhook": "https://api.day.app/k",
                                  "template": "{nope}"}})
        r = n.status()["render"]
        self.assertIn("占位符", r["template_why"])
        self.assertFalse(r["custom_template"])

    def test_85_status_never_reads_disk(self):
        """状态接口被几秒轮询一次，render 段只能算，不能读配置或扫盘。"""
        calls = []

        def boom():
            calls.append(1)
            return {}

        n = notify.Notifier(boom)
        n.reconfigure({"notify": {"webhook": "https://api.day.app/k"}})
        n.status()
        self.assertEqual(calls, [], "status() 不该再去读一次配置")


class Wiring(unittest.TestCase):
    """控件 → 事件 → pushCfg → 接口，四段缺一不可。"""

    def setUp(self):
        self.cards = read(CARDS)
        self.wiring = read(WIRING)
        self.live = read(LIVE)
        self.state = read(STATE)

    def test_90_controls_exist(self):
        for needle in ('data-ntplat="', 'id="ntTemplate"', 'id="btnNtHist"',
                       'id="ntHist"', 'id="ntRender"'):
            self.assertIn(needle, self.cards, "通知卡缺少控件：%s" % needle)

    def test_92_controls_are_bound(self):
        for needle in ("[data-ntplat]", "#ntTemplate", "#btnNtHist"):
            self.assertIn(needle, self.wiring,
                          "控件没有绑事件，等于点了没反应：%s" % needle)

    def test_94_push_includes_both_keys(self):
        """少推一个键，保存设置时就会被整块覆盖打回默认（第 21 轮的老坑）。"""
        self.assertIn('platform: LK.ntPlatform || "auto"', self.live)
        self.assertIn('template: LK.ntTemplate || ""', self.live)

    def test_96_state_has_fields(self):
        self.assertIn("ntPlatform", self.state)
        self.assertIn("ntTemplate", self.state)

    def test_98_readback(self):
        self.assertIn("LK.ntPlatform = n.platform", self.live)
        self.assertIn("LK.ntTemplate = n.template", self.live)

    def test_99_history_uses_readonly_api(self):
        self.assertIn("/api/notify/history?limit=20", self.wiring)
        self.assertIn("/api/notify/test", self.wiring)

    def test_100_server_test_endpoint_forwards_override(self):
        src = read(os.path.join(ROOT, "app", "server", "nvr_slim.py"))
        self.assertIn("nt.test(url, tpl=tpl, platform=plat)", src,
                      "测试发送必须把界面上刚填的模板/平台带上")


if __name__ == "__main__":
    unittest.main(verbosity=1)
