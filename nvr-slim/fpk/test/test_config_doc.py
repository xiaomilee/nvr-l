#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CONFIGURATION.md 的守卫：文档里的每个数字都得能在代码里找到（0.9.123）。

为什么需要这套测试
------------------
配置文档最容易变成「写完就烂」：加一个键、改一次默认值、夹取范围调一格，
代码变了文档没变，下一个人照着文档排查「改了不生效」反而被误导。批次 5
已经连续三轮抓到这类缺口（reconfigure 漏抄字段、第二条入口没通知第二个
对象、裸 bool 把 "false" 判成开），根因都是**没人说得清这个键该由谁重新读**。

所以这份测试不解析 prose，只解析两张机器可读的表：

  * 主表一行一个 `runtime.DEFAULTS` 顶层键，逐项比对类型、默认值、枚举白名单、
    `_ENUM_KEYS` 与三个范围常量；多一个键、少一个键、抄错默认值都会红。
  * §2 的「生效方式」受控词表也必须和这里一致 —— 不许悄悄多写一档把
    「需重启」写成「热生效」，那正是文档最危险的失效方式。

真相始终在代码里：测试 import `runtime` 读常量，不把默认值再抄一份进测试。
出处列逐个检查文件是否真实存在，防止写完一句「见 xxx.py」就没人核实过。

跑：python test/test_config_doc.py
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import runtime  # noqa: E402

DOC = os.path.join(ROOT, "..", "..", "docs", "CONFIGURATION.md")

# 词表是契约：文档 §2 与本表必须逐字一致，且主表每一行只能用其中一个值。
EFFECTS = ("热生效", "每轮读盘", "夜间任务", "部分热生效",
           "需重启", "前端常量", "遗留字段")

# 只有这三个范围常量在 runtime 里是具名的；其余范围（archive_days、
# link_max_queue 等）写在各自模块的 LIMITS 里，测试只验文档写了范围且出处有效，
# 不在测试里再抄一份数字当第二份真相。
RANGE_FROM_RUNTIME = {
    "record_segment_sec": "SEGMENT_LIMITS",
    "link_interval_h": "LINK_INTERVAL_LIMITS",
    "service_port": "PORT_LIMITS",
}

HEADERS = ("key", "类型", "默认值", "范围 / 枚举", "生效方式", "出处", "说明")

# dict 键的默认值写在 §4 的嵌套小节里，主表只放一个指针。用独立哨兵区分
# 「跳过比对」和「文档里真的写了 None」——updated_at 的默认值恰好就是 None。
NESTED = object()


def read_doc():
    with open(DOC, "r", encoding="utf-8") as f:
        return f.read()


def between(text, open_tag, close_tag):
    """取一对注释锚点之间的内容；锚点必须唯一，否则说明文档结构被改乱了。"""
    a = "<!-- %s -->" % open_tag
    b = "<!-- /%s -->" % close_tag
    if text.count(a) != 1 or text.count(b) != 1:
        raise AssertionError("锚点必须各出现一次：%s / %s" % (a, b))
    i, j = text.index(a) + len(a), text.index(b)
    if j <= i:
        raise AssertionError("锚点顺序反了：%s" % open_tag)
    return text[i:j]


def cells(line):
    return [c.strip() for c in line.strip().split("|")[1:-1]]


def parse_table(text):
    """解析 <!-- table:config --> 里的数据行，返回 {key: 各列}。"""
    block = between(text, "table:config", "table:config")
    lines = [ln for ln in block.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3:
        raise AssertionError("主表至少要表头、分隔行和一行数据")
    rows = {}
    for ln in lines[2:]:
        cs = cells(ln)
        if len(cs) != len(HEADERS):
            raise AssertionError("主表某行列数不是 %d：%r" % (len(HEADERS), ln))
        key = cs[0].strip("`").strip()
        if key in rows:
            raise AssertionError("主表里 key 重复：%s" % key)
        rows[key] = dict(zip(HEADERS[1:], cs[1:]))
    return rows, cells(lines[0])


def unquote(cell):
    return cell.strip().strip("`").strip()


def parse_default(cell):
    s = unquote(cell)
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "null":
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if s.startswith("见 §"):
        return NESTED
    return s


def type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, dict):
        return "dict"
    return "str"


def source_files(cell):
    """出处列里所有 `xxx.py:行号` 的文件名。"""
    return re.findall(r"`([A-Za-z0-9_]+\.py):\d+`", cell)


class ConfigDoc(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = read_doc()
        cls.rows, cls.header = parse_table(cls.text)

    # ---------------------------------------------------------------- 表形状

    def test_10_table_header_and_anchors(self):
        """列名固定，七列一列不少：改列名会让解析静默错位。"""
        self.assertEqual(tuple(self.header), HEADERS)
        self.assertEqual(len(self.rows), len(runtime.DEFAULTS))
        # 锚点成对且唯一，parse_table / between 已经验过一次；这里再钉住
        # 文档没把主表挪到别的地方（挪走就抓不到行）。
        self.assertEqual(self.text.count("<!-- table:config -->"), 1)
        self.assertEqual(self.text.count("<!-- effects:vocab -->"), 1)

    def test_20_keys_cover_defaults(self):
        """加一个 DEFAULTS 键却没写文档，闸门当场红。"""
        self.assertEqual(set(self.rows), set(runtime.DEFAULTS),
                         "多了 %s / 少了 %s" % (
                             sorted(set(self.rows) - set(runtime.DEFAULTS)),
                             sorted(set(runtime.DEFAULTS) - set(self.rows))))

    def test_30_types_match_defaults(self):
        for key, value in runtime.DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(unquote(self.rows[key]["类型"]),
                                 type_name(value))

    def test_40_defaults_match_code(self):
        """默认值逐键比对；dict 键只验主表指向了 §4 的嵌套小节。"""
        for key, value in runtime.DEFAULTS.items():
            with self.subTest(key=key):
                got = parse_default(self.rows[key]["默认值"])
                if isinstance(value, dict):
                    self.assertIs(got, NESTED,
                                  "嵌套块应写「见 §4.x」，不要在主表里抄一份")
                    self.assertIn("§4", self.rows[key]["默认值"])
                else:
                    self.assertEqual(got, value)

    # ---------------------------------------------------------------- 取值约束

    def test_50_enums_match_whitelist(self):
        """枚举列的取值集合必须等于 _ENUM_KEYS，多写少写都算漂移。

        第 17 轮就是 _ENUM_KEYS 漏了 link_mode，非法值原样落盘回读把界面
        显示成一个不存在的模式；文档若跟着抄错，排查的人会被再骗一次。
        """
        for key, allowed in runtime._ENUM_KEYS.items():
            with self.subTest(key=key):
                raw = unquote(self.rows[key]["范围 / 枚举"])
                got = {p.strip().strip("`").strip()
                       for p in re.split(r"[,，]", raw) if p.strip()}
                self.assertEqual(got, set(allowed))

    def test_60_ranges_match_constants(self):
        """具名范围常量与文档一致；其余范围键至少要写出区间和出处。"""
        for key, const in RANGE_FROM_RUNTIME.items():
            with self.subTest(key=key):
                lo, hi = getattr(runtime, const)
                self.assertIn("%d~%d" % (lo, hi),
                              unquote(self.rows[key]["范围 / 枚举"]))
        for key in ("concurrency", "snapshot_interval", "archive_days",
                    "archive_batch", "link_max_queue"):
            with self.subTest(key=key, 检查="区间非空"):
                self.assertRegex(unquote(self.rows[key]["范围 / 枚举"]),
                                 r"\d+~\d+")

    # ---------------------------------------------------------------- 生效方式

    def test_70_effects_vocab_agrees(self):
        """§2 词表与本文件 EFFECTS 逐字一致，且每行只用其中一档。"""
        block = between(self.text, "effects:vocab", "effects:vocab")
        written = set(re.findall(r"^- `([^`]+)`", block, re.M))
        self.assertEqual(written, set(EFFECTS),
                         "文档词表与测试词表不一致：%s" % (written ^ set(EFFECTS)))
        for key, row in self.rows.items():
            with self.subTest(key=key):
                self.assertIn(unquote(row["生效方式"]), EFFECTS)

    def test_72_effect_is_not_a_guess(self):
        """几处最容易写错的口径单独钉住，防止「顺手改成热生效」。"""
        fixed = {
            "ingest_enable": "需重启",       # IngestManager 启动读一次
            "record_reconnect_sec": "需重启",  # Recorder 构造期参数
            "service_port": "需重启",       # cmd/main 解析，还会顺延
            "concurrency": "部分热生效",     # SlotPool 启动期建
            "night_only": "前端常量",        # 只进 localStorage
            "record_enable": "热生效",       # 两条入口都要通知 Recorder
            "cleanup_at": "夜间任务",        # 没有实时出口
        }
        for key, want in fixed.items():
            with self.subTest(key=key):
                self.assertEqual(unquote(self.rows[key]["生效方式"]), want)

    # ---------------------------------------------------------------- 出处与坑位

    def test_80_sources_exist(self):
        """出处列指向的文件必须真实存在，不许编一个文件名糊过去。"""
        known_dirs = (os.path.join(ROOT, "app", "server"),
                      os.path.join(ROOT, "tools"), HERE)
        for key, row in self.rows.items():
            with self.subTest(key=key):
                names = source_files(row["出处"])
                self.assertTrue(names, "出处列没有 `xxx.py:行号` 形式的引用")
                for name in names:
                    self.assertTrue(
                        any(os.path.isfile(os.path.join(d, name))
                            for d in known_dirs),
                        "出处文件不存在：%s（%s）" % (name, key))

    def test_90_pitfalls_are_documented(self):
        """双入口 / 强校 / 嵌套块那几个坑，文档被简化成占位就要红。"""
        for needle in ("coerce", "merge_defaults", "redact_cfg",
                       "/api/link/policy", "reconfigure",
                       "detect.confirm", "norm_detect", "norm_notify",
                       "CONFIRM_DEFAULTS", "KNOWN_GAPS",
                       "partial", "config.json"):
            with self.subTest(缺少=needle):
                self.assertIn(needle, self.text)
        tail = self.text[self.text.index("## 4. 嵌套块"):]
        for needle in ("live_fps", "cooldown", "confirm", "NOTIFY_DEFAULTS"):
            with self.subTest(嵌套块缺少=needle):
                self.assertIn(needle, tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
