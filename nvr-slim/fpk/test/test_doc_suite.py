#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档套件的守卫：写完不许烂，也不许抄第二份「看起来对」的数字（0.9.123）。

为什么需要它
------------
批次 5 的文档重构一次产出五份说明，最容易的失效方式不是「写错」，
而是**代码变了文档没变**：加一个后端模块、加一条路由、bump 一次版本、
把 BUILD.md 里搬走的历史又贴回来，读文档的人会被安静地误导。
`test_config_doc.py` 已经守住配置键，这份守住其余四个面：

  1. 文档齐不齐、内部相对链接指向的文件是否真实存在；
  2. 版本号与 `runtime.VERSION` 一致，且不许提前写下下一个版本；
  3. `ARCHITECTURE.md` 的模块表 / 路由表与 `app/server/*.py`、
     `nvr_slim.py` 里的 `/api/` 字面量逐项对齐；
  4. 页面 tab 与 `index.html` 的 `data-page` 对齐；
  5. `BUILD.md` 只留构建，历史排障必须在 `CHANGELOG.md` 里；
  6. 状态速查表里的七态 / 十个错误码 / 五条缺口原因与源码一致。

真相始终从代码读：版本 import `runtime`，模块名扫目录，路由与常量解析源码文本，
测试里不把默认值、模块名、错误码再抄一份。这样「改了代码没改文档」会红，
「改了文档没改代码」也会红。

跑：python test/test_doc_suite.py
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # fpk/
REPO = os.path.normpath(os.path.join(ROOT, "..", ".."))   # 仓库根
DOCS = os.path.join(REPO, "docs")
SERVER = os.path.join(ROOT, "app", "server")
sys.path.insert(0, SERVER)

import runtime  # noqa: E402

FILES = {
    "readme": os.path.join(REPO, "README.md"),
    "guide": os.path.join(DOCS, "USER_GUIDE.md"),
    "arch": os.path.join(DOCS, "ARCHITECTURE.md"),
    "config": os.path.join(DOCS, "CONFIGURATION.md"),
    "trouble": os.path.join(DOCS, "TROUBLESHOOTING.md"),
    "baseline": os.path.join(DOCS, "REVIEW_BASELINE.md"),
    "changelog": os.path.join(ROOT, "CHANGELOG.md"),
    "build": os.path.join(ROOT, "BUILD.md"),
}

# 这些章节属于「历史排障」，第 24 轮从 BUILD.md 搬进了 CHANGELOG.md。
# BUILD.md 再出现就是又混回去了，CHANGELOG.md 少了就是搬丢了。
HISTORY_TITLES = (
    "0.5.0 回到 0.3.1 的标识与形态",
    "0.4.1 修复「应用数据包不完整」",
    "0.4.0 端口可配置",
    "0.3.1 修复：局域网直连打不开",
    "0.3.0 的三件事",
    "本机试过什么",
    "装上真机后按顺序验证",
    "当时尚未覆盖",
)


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def between(text, tag):
    """取一对注释锚点之间的内容；锚点必须各出现一次，否则说明文档结构被改乱了。"""
    a = "<!-- %s -->" % tag
    b = "<!-- /%s -->" % tag
    if text.count(a) != 1 or text.count(b) != 1:
        raise AssertionError("锚点必须各出现一次：%s" % tag)
    i, j = text.index(a) + len(a), text.index(b)
    if j <= i:
        raise AssertionError("锚点顺序反了：%s" % tag)
    return text[i:j]


def cells(line):
    return [c.strip() for c in line.strip().split("|")[1:-1]]


def data_rows(block):
    lines = [ln for ln in block.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3:
        raise AssertionError("表格至少要表头、分隔行和一行数据")
    return [cells(ln) for ln in lines[2:]]


class DocSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = {k: read(v) for k, v in FILES.items()}
        cls.server_src = read(os.path.join(SERVER, "nvr_slim.py"))
        cls.health_src = read(os.path.join(SERVER, "health.py"))
        cls.detect_src = read(os.path.join(SERVER, "detect.py"))
        cls.index_html = read(os.path.join(ROOT, "app", "ui", "index.html"))

    # ---------------------------------------------------------------- 齐备与链接

    def test_10_all_docs_exist(self):
        """少一份文档就是「文档重构」没做完，别只查自己刚写的那几份。"""
        for name, path in FILES.items():
            with self.subTest(文档=name):
                self.assertTrue(os.path.isfile(path), "缺文档：%s" % path)
                self.assertGreater(len(self.text[name]), 800,
                                   "%s 内容过短，像是占位" % name)

    def test_12_relative_links_resolve(self):
        """写完一句「见 xxx.md」就没人核实过，是文档最常见的烂法。"""
        for name, path in FILES.items():
            text = self.text[name]
            for target in re.findall(r"\]\(([^)#]+)\)", text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                with self.subTest(文档=name, 链接=target):
                    resolved = os.path.normpath(
                        os.path.join(os.path.dirname(path), target.strip()))
                    self.assertTrue(os.path.isfile(resolved),
                                    "链接指向的文件不存在：%s" % resolved)

    # ---------------------------------------------------------------- 版本

    def test_20_version_agrees_with_runtime(self):
        """文档里的版本号从 runtime.VERSION 来，测试里不抄字面量。"""
        for name in ("readme", "guide", "arch", "config", "trouble",
                     "changelog", "build"):
            with self.subTest(文档=name):
                self.assertIn(runtime.VERSION, self.text[name])
                self.assertNotIn("0.9.124", self.text[name],
                                 "别提前写下下一个版本，批次 5 的目标版本就是 0.9.123")

    # ---------------------------------------------------------------- 模块表

    def test_30_module_table_matches_server_dir(self):
        """加一个后端模块却没写进 ARCHITECTURE.md，这里当场红。"""
        block = between(self.text["arch"], "table:modules")
        got = set()
        for cs in data_rows(block):
            m = re.fullmatch(r"`([\w.]+\.py)`", cs[0])
            self.assertIsNotNone(m, "模块表第一列必须是 `xxx.py`：%r" % cs[0])
            got.add(m.group(1))
        actual = {f for f in os.listdir(SERVER)
                  if f.endswith(".py") and
                  os.path.isfile(os.path.join(SERVER, f))}
        self.assertEqual(got, actual,
                         "文档多了 %s / 少了 %s" % (sorted(got - actual),
                                                sorted(actual - got)))

    # ---------------------------------------------------------------- 路由表

    def test_40_route_table_has_source(self):
        """文档里每条路径都要能在 nvr_slim.py 找到字面量，不许编一个接口。"""
        literals = set(re.findall(r'"(/api/[A-Za-z0-9_/<>?=&.\-]+)"',
                                  self.server_src))
        self.assertGreater(len(literals), 30)
        block = between(self.text["arch"], "table:api")
        paths = []
        for cs in data_rows(block):
            self.assertIn(cs[0], ("GET", "POST"), "方法列只能是 GET/POST")
            paths.append(cs[1].strip("`").strip())
        self.assertGreater(len(paths), 35)
        for p in paths:
            with self.subTest(路径=p):
                base = re.sub(r"<.*", "", p).rstrip("/")
                # 只允许「精确相等」或「源码字面量以文档路径开头」（后者用于
                # /api/events/ 这类按前缀分发的分支）。反过来「文档路径以源码
                # 字面量开头」不能算命中：/api/health-ghost 也会以 /api/health
                # 开头，那样编一个不存在的接口就混过去了。
                self.assertTrue(
                    any(s.rstrip("/") == base or s.startswith(base)
                        for s in literals),
                    "源码里找不到这个路由：%s" % p)

    def test_42_every_source_route_is_documented(self):
        """反过来也要守：源码新增一条路由，文档里至少得提到它。"""
        literals = set(re.findall(r'"(/api/[A-Za-z0-9_/<>?=&.\-]+)"',
                                  self.server_src))
        missing = sorted(s for s in literals if s not in self.text["arch"])
        self.assertEqual(missing, [], "这些接口没写进 ARCHITECTURE.md：%s" % missing)

    # ---------------------------------------------------------------- 页面

    def test_50_tabs_match_index_html(self):
        """导航只有 5 个 tab，没有独立「诊断」页；文档别写成 6 个。"""
        pages = set(re.findall(r'data-page="([a-z]+)"', self.index_html))
        self.assertEqual(pages, {"live", "play", "events", "list", "cam"})
        for name in ("readme", "guide"):
            for p in pages:
                with self.subTest(文档=name, 页面=p):
                    self.assertIn(p, self.text[name])

    # ---------------------------------------------------------------- BUILD 瘦身

    def test_60_build_only_talks_about_building(self):
        """历史排障搬走就别再搬回来，否则新人找构建命令又要翻版本考古。"""
        for title in HISTORY_TITLES:
            with self.subTest(历史章节=title):
                self.assertNotIn(title, self.text["build"])
        self.assertFalse(re.search(r"^## 0\.\d", self.text["build"], re.M),
                         "BUILD.md 里不该再有历史版本的二级标题")
        self.assertIn("CHANGELOG.md", self.text["build"])

    def test_62_changelog_keeps_the_history(self):
        for title in HISTORY_TITLES:
            with self.subTest(历史章节=title):
                self.assertIn(title, self.text["changelog"])

    # ---------------------------------------------------------------- 状态速查

    def test_70_hstate_vocabulary_matches_health(self):
        """七态从 health.STATE_LABEL 读，排障手册不许自己发明第八种。"""
        m = re.search(r"STATE_LABEL\s*=\s*\{(.*?)\}", self.health_src, re.S)
        self.assertIsNotNone(m)
        keys = re.findall(r'"(\w+)"\s*:', m.group(1))
        self.assertEqual(len(keys), 7)
        for k in keys:
            with self.subTest(状态=k):
                self.assertIn(k, self.text["trouble"])

    def test_72_detect_error_codes_match_source(self):
        codes = re.findall(r'^E_[A-Z_]+\s*=\s*"([A-Z_]+)"',
                           self.detect_src, re.M)
        self.assertEqual(len(codes), 10)
        for c in codes:
            with self.subTest(错误码=c):
                self.assertIn(c, self.text["trouble"])

    def test_74_gap_reason_count_is_honest(self):
        """缺口原因文案的条数从源码数出来，文档里那个「共 N 条」不许拍脑袋。"""
        m = re.search(r"def gap_reason\(.*?\):(.*?)(?=\ndef |\nclass |\Z)",
                      self.health_src, re.S)
        self.assertIsNotNone(m)
        n = len(re.findall(r'return\s+"', m.group(1)))
        self.assertEqual(n, 5)
        self.assertIn("共 5 条", self.text["trouble"])

    # ---------------------------------------------------------------- 端口口径

    def test_80_default_port_matches_manifest(self):
        """默认端口 18080 有几处注释写错过，文档的口径只认 manifest。"""
        manifest = read(os.path.join(ROOT, "manifest"))
        m = re.search(r"^service_port\s*=\s*(\d+)", manifest, re.M)
        self.assertIsNotNone(m)
        port = m.group(1)
        self.assertIn(port, self.text["readme"])
        self.assertIn(port, self.text["trouble"])

    def test_82_deploy_selfcheck_count_is_documented(self):
        """八项 = 7 项要通 + 1 项要被拦住，别再写成「六项」。"""
        deploy = read(os.path.join(ROOT, "tools", "deploy_test.py"))
        self.assertIn("CHECKS", deploy)
        self.assertIn("八项", self.text["trouble"])
        self.assertIn("八项", self.text["build"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
