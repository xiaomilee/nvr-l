#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存储空间预警（0.9.131）的行为守卫。

这一层最容易出的两类错：
  S1 **算错含义**：把"剩余 ÷ 每天写入"当结论。按天整目录删的策略下，
     这个数会得出"只剩 0.6 天"，而机器其实已经稳跑几周 —— 于是天天喊满，
     喊几次就没人信。所以这里钉住的核心口径是"稳态装不装得下"：
     `need = Σ rate[kind] × 保留天数[kind]`，`headroom = 总容量 − 非录像占用 − need`。
  S2 **没数据时编数据**：一个完整日都没跑过就报"还能录 30 天"，或者把
     0 字节的日子算进速率（那天断电没录）。前者是假数据，后者会让预警永不触发。

跑：python test/test_space.py
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import space  # noqa: E402

GB = 1024.0 ** 3
TODAY = date(2026, 11, 9)
# 2026-11-09 16:00 本地时间 —— 用它算"今天已过多久"，避免测试跟真实时钟走
NOW = datetime(2026, 11, 9, 16, 0, 0).timestamp()


def d(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


def days_ago(*sizes):
    """返回 {(今天-1): s0, (今天-2): s1, …}，顺序与传入一致（最近的在最前）。"""
    out = {}
    for i, b in enumerate(sizes, 1):
        out[d(-i)] = int(b)
    return out


class TestClamp(unittest.TestCase):
    def test_默认与边界(self):
        self.assertEqual(space.clamp_warn_gb(20), 20)
        self.assertEqual(space.clamp_warn_gb(0), 0)          # 0 = 关闭，必须保留
        self.assertEqual(space.clamp_warn_gb(2000), 2000)
        self.assertEqual(space.clamp_warn_gb(5000), 2000)    # 上限夹住
        self.assertEqual(space.clamp_warn_gb(-5), 0)         # 负数夹到 0
        self.assertEqual(space.clamp_warn_gb("12"), 12)      # 前端推字符串
        self.assertEqual(space.clamp_warn_gb(12.6), 13)      # 四舍五入，不截断
        self.assertEqual(space.clamp_warn_gb("abc"), 20)     # 脏值回默认，不是 0
        self.assertEqual(space.clamp_warn_gb(None), 20)

    def test_gb(self):
        self.assertEqual(space.gb(GB), 1.0)
        self.assertEqual(space.gb(int(1.5 * GB)), 1.5)
        self.assertIsNone(space.gb(None))                    # 前端显示 "—"
        self.assertIsNone(space.gb("x"))


class TestDailyRate(unittest.TestCase):
    def test_中位数而不是平均(self):
        # 一天异常大（补录/换盘），不该把长期速率带偏
        rate, n, label = space.daily_rate(days_ago(12 * GB, 12 * GB, 120 * GB), TODAY, NOW)
        self.assertEqual(n, 3)
        self.assertEqual(rate, 12 * GB)
        self.assertIn("3 个完整日", label)

    def test_丢掉零字节的日子(self):
        # 断电那天目录在但没内容：算进去会把 12 GB/天 拉成 6 GB/天
        rate, n, _ = space.daily_rate(days_ago(12 * GB, 0, 12 * GB), TODAY, NOW)
        self.assertEqual(n, 2)
        self.assertEqual(rate, 12 * GB)

    def test_今天不进样本(self):
        rate, n, _ = space.daily_rate(days_ago(10 * GB, 10 * GB), TODAY, NOW)
        self.assertEqual(n, 2)
        mixed = days_ago(10 * GB, 10 * GB)
        mixed[d(0)] = int(999 * GB)          # 今天的数据再离谱也不进中位数
        rate2, n2, _ = space.daily_rate(mixed, TODAY, NOW)
        self.assertEqual((rate2, n2), (10 * GB, 2))

    def test_只看最近七天(self):
        many = dict((d(-i), int(1 * GB)) for i in range(1, 20))
        rate, n, _ = space.daily_rate(many, TODAY, NOW)
        self.assertEqual(n, space.RATE_DAYS)
        self.assertEqual(rate, 1 * GB)

    def test_今天外推要够一小时(self):
        # 15:10 起才开始写 → 只有 50 分钟，不足 TODAY_MIN_SEC，不许外推
        early = datetime(2026, 11, 9, 0, 50, 0).timestamp()
        rate, n, label = space.daily_rate({d(0): int(1 * GB)}, TODAY, early)
        self.assertEqual((rate, n, label), (0.0, 0, ""))
        # 16:00 时 1 GB / 2/3 天 → 约 1.5 GB/天，并如实写"今天已跑"
        rate2, n2, label2 = space.daily_rate({d(0): int(1 * GB)}, TODAY, NOW)
        self.assertEqual(n2, 0)
        self.assertAlmostEqual(rate2 / GB, 1.5, places=1)
        self.assertIn("今天已跑", label2)

    def test_没有任何数据(self):
        self.assertEqual(space.daily_rate({}, TODAY, NOW), (0.0, 0, ""))
        self.assertEqual(space.daily_rate(None, TODAY, NOW), (0.0, 0, ""))

    def test_坏值跳过不抛(self):
        bad = {"2026-11-08": "x", "not-a-date": 100, d(-2): -5, d(-3): int(2 * GB)}
        rate, n, _ = space.daily_rate(bad, TODAY, NOW)
        self.assertEqual((rate, n), (2 * GB, 1))


class TestRatesOf(unittest.TestCase):
    def test_分层速率(self):
        rates, n, label = space.rates_of({
            "full": days_ago(10 * GB, 10 * GB),
            "archive": days_ago(2 * GB, 2 * GB),
            "clip": {},
        }, TODAY, NOW)
        self.assertEqual(rates["full"], 10 * GB)
        self.assertEqual(rates["archive"], 2 * GB)
        self.assertEqual(rates["clip"], 0.0)
        self.assertEqual(n, 2)
        self.assertIn("2 个完整日", label)

    def test_缺层不抛(self):
        rates, n, _ = space.rates_of({"full": days_ago(1 * GB)}, TODAY, NOW)
        self.assertEqual(rates, {"full": 1 * GB, "archive": 0.0, "clip": 0.0})


class TestKeepMap(unittest.TestCase):
    def test_字典与单值(self):
        self.assertEqual(space.keep_map({"full": 3, "archive": 7, "clip": 90}),
                         {"full": 3, "archive": 7, "clip": 90})
        self.assertEqual(space.keep_map(4), {"full": 4, "archive": 0, "clip": 0})
        self.assertEqual(space.keep_map({"full": "3"}), {"full": 3, "archive": 0, "clip": 0})
        self.assertEqual(space.keep_map(None), {"full": 0, "archive": 0, "clip": 0})
        self.assertEqual(space.keep_map({"full": -2}), {"full": 0, "archive": 0, "clip": 0})


class TestEvaluate(unittest.TestCase):
    """152 的现状当作基准样本：55 GB 盘、剩 7.6、录像 35.3、每天 12 GB、留 3 天。"""

    def base(self, **kw):
        args = dict(
            free_b=7.6 * GB, total_b=55 * GB, used_b=35.3 * GB,
            days_by_kind={"full": days_ago(12 * GB, 12.4 * GB, 11.8 * GB)},
            keep_days={"full": 3, "archive": 7, "clip": 90},
            warn_gb=20, today=TODAY, now=NOW)
        args.update(kw)
        return space.evaluate(**args)

    def test_这台盘就是告急(self):
        p = self.base()
        self.assertEqual(p["level"], "critical")
        self.assertEqual([r["code"] for r in p["reasons"]], ["free_critical"])
        self.assertAlmostEqual(p["other_gb"], 12.1, places=1)   # 55 − 7.6 − 35.3
        self.assertAlmostEqual(p["headroom_gb"], 6.9, places=1)  # 55 − 12.1 − 36
        self.assertEqual(p["keep_text"], "全时 3 天")            # 归档没产出，不写它
        self.assertEqual(p["suggest_days"], 3)                   # (55−12.1)/12

    def test_建议里要有最多能留几天(self):
        p = self.base()
        self.assertIn("最多留 3 天", p["advice"])
        self.assertIn("03:00", p["advice"])

    def test_容量装不下是告急且给出可执行天数(self):
        p = self.base(days_by_kind={"full": days_ago(30 * GB, 30 * GB)})
        codes = [r["code"] for r in p["reasons"]]
        self.assertEqual(p["level"], "critical")
        self.assertIn("capacity", codes)
        self.assertEqual(p["suggest_days"], 1)                   # (55−12.1)/30 ≈ 1.4
        self.assertIn("把保留天数从 3 天降到 1 天以内", p["advice"])

    def test_稳态余量偏小是偏低(self):
        # 每天 14.5 GB × 3 天 = 43.5；other = 55 − 12 − 35.3 = 7.7；
        # 余量 3.8 GB < 兜底线 5 GB → 偏低；剩余 12 GB 未到告急线 10 GB
        p = self.base(days_by_kind={"full": days_ago(14.5 * GB, 14.5 * GB)},
                      free_b=12 * GB)
        codes = [r["code"] for r in p["reasons"]]
        self.assertIn("headroom", codes)
        self.assertIn("free_low", codes)
        self.assertEqual(p["level"], "warn")

    def test_大冰箱盘不再拿绝对阈值喊(self):
        # 1000 GB 盘剩 90 GB（9%）→ 高于 20 GB 阈值，但低于 10% 兜底线
        p = space.evaluate(90 * GB, 1000 * GB, 800 * GB,
                           {"full": days_ago(10 * GB)}, {"full": 3}, 20,
                           today=TODAY, now=NOW)
        self.assertEqual(p["level"], "warn")
        self.assertEqual([r["code"] for r in p["reasons"]], ["free_low"])
        # 剩 150 GB（15%）→ 什么都不报
        p2 = space.evaluate(150 * GB, 1000 * GB, 800 * GB,
                            {"full": days_ago(10 * GB)}, {"full": 3}, 20,
                            today=TODAY, now=NOW)
        self.assertEqual(p2["level"], "ok")

    def test_正常(self):
        p = space.evaluate(400 * GB, 1000 * GB, 200 * GB,
                           {"full": days_ago(10 * GB)}, {"full": 3}, 20,
                           today=TODAY, now=NOW)
        self.assertEqual(p["level"], "ok")
        self.assertEqual(p["reasons"], [])
        self.assertEqual(p["advice"], "")

    def test_关闭(self):
        p = self.base(warn_gb=0)
        self.assertEqual(p["level"], "off")
        self.assertEqual(p["reasons"], [])
        self.assertEqual(p["warn_gb"], 0)

    def test_读不到容量时说未知而不是正常(self):
        p = space.evaluate(0, 0, 0, {}, {"full": 3}, 20, today=TODAY, now=NOW)
        self.assertEqual(p["level"], "unknown")
        self.assertEqual([r["code"] for r in p["reasons"]], ["no_disk"])

    def test_没速率时不说还能录几天(self):
        p = space.evaluate(400 * GB, 1000 * GB, 200 * GB, {}, {"full": 3}, 20,
                           today=TODAY, now=NOW)
        self.assertEqual(p["level"], "unknown")
        self.assertEqual([r["code"] for r in p["reasons"]], ["no_rate"])
        self.assertIsNone(p["suggest_days"])
        self.assertIsNone(p["free_days"])

    def test_没速率但盘已经很低时照样报(self):
        # 没有速率只是"算不出容量结论"，不妨碍 statvfs 说"现在只剩 7 GB"
        p = space.evaluate(7 * GB, 55 * GB, 30 * GB, {}, {"full": 3}, 20,
                           today=TODAY, now=NOW)
        self.assertEqual(p["level"], "critical")
        self.assertEqual([r["code"] for r in p["reasons"]], ["free_critical"])
        self.assertIsNone(p["suggest_days"])

    def test_清理过期(self):
        p = self.base(cleanup_overdue_h=5.0)
        codes = [r["code"] for r in p["reasons"]]
        self.assertIn("cleanup_stale", codes)
        self.assertIn("晚了 5.0 小时", p["reasons"][-1]["text"])
        self.assertIn("手动执行一次清理", p["advice"])
        # 超过一天没跑 → 告急
        p2 = self.base(cleanup_overdue_h=30.0, free_b=40 * GB)
        self.assertEqual(p2["level"], "critical")
        # 刚到点 / 晚一点点：不报（清理线程每 30 秒问一次，容差要有）
        p3 = self.base(cleanup_overdue_h=1.0, free_b=40 * GB)
        self.assertNotIn("cleanup_stale", [r["code"] for r in p3["reasons"]])

    def test_归档按自己的速率算(self):
        # 归档只装活动片段：1 GB/天 × 7 天，不该按全时的 12 GB/天 去乘
        p = space.evaluate(400 * GB, 1000 * GB, 200 * GB,
                           {"full": days_ago(12 * GB), "archive": days_ago(1 * GB)},
                           {"full": 3, "archive": 7}, 20, today=TODAY, now=NOW)
        self.assertAlmostEqual(p["need_gb"], 43.0, places=1)     # 36 + 7
        self.assertEqual(p["keep_text"], "全时 3 天、归档 7 天")

    def test_多个原因都保留按最重的定级(self):
        p = space.evaluate(7 * GB, 55 * GB, 35 * GB,
                           {"full": days_ago(30 * GB)}, {"full": 3}, 20,
                           cleanup_overdue_h=5.0, today=TODAY, now=NOW)
        codes = [r["code"] for r in p["reasons"]]
        self.assertEqual(p["level"], "critical")
        self.assertEqual(set(codes), {"capacity", "free_critical", "cleanup_stale"})

    def test_键始终在(self):
        p = self.base()
        for k in ("level", "free_gb", "total_gb", "used_gb", "other_gb", "rate_gb",
                  "rate_basis", "rate_label", "rates_gb", "keep_days", "keep",
                  "keep_text", "need_gb", "headroom_gb", "warn_gb", "free_days",
                  "suggest_days", "reasons", "advice", "at"):
            self.assertIn(k, p)
        self.assertIsInstance(p["at"], int)


class TestTextOf(unittest.TestCase):
    def test_一行话带上关键数字(self):
        p = space.evaluate(7.6 * GB, 55 * GB, 35.3 * GB,
                           {"full": days_ago(12 * GB)}, {"full": 3}, 20,
                           today=TODAY, now=NOW)
        s = space.text_of(p)
        self.assertIn("空间告急", s)
        self.assertIn("剩余 7.6 GB / 55.0 GB", s)
        self.assertIn("每天约 12.0 GB", s)
        self.assertIn("建议：", s)

    def test_各等级都有词(self):
        for p, word in (({"level": "ok", "free_gb": 1, "total_gb": 2}, "空间正常"),
                        ({"level": "warn"}, "空间偏低"),
                        ({"level": "off"}, "已关闭")):
            self.assertIn(word, space.text_of(p))

    def test_空载荷不抛(self):
        self.assertIsInstance(space.text_of({}), str)
        self.assertIsInstance(space.text_of(None), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
