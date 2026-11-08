#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置项验收表自身的单测（0.9.123 批次 5 清单第 2 项第一刀）。

这里验的不是「设置项能不能改」——那要连测试机，留到采集器那一轮。
这里验的是**这张表值不值得信**：

  1. 判定器会不会把「没采到」当成「通过」；
  2. 会不会拿配置回显冒充运行态，把「只查 UI 值」换个地方又做一遍；
  3. 表里有没有漏项：DEFAULTS 的每个键都得有验收行或写明不验的理由，
     前端 pushCfg 推的每个键都得有验收行。

第 3 条是这张表的命门：它纯离线、不连机器，唯一的失效方式就是**表和代码
各自漂移**，半年后表还绿着，实际早已不对。所以用源码和 DEFAULTS 双向钉住。

跑：python test/test_button_matrix.py
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import button_matrix as bm      # noqa: E402
import runtime                  # noqa: E402

LIVE_JS = os.path.join(ROOT, "app", "ui", "js", "pages", "live.js")


def seg_row(**kw):
    """一行最小可用的验收行：分片长度，两个运行态观测点，无进程参数。"""
    base = bm._row("record_segment_sec", "录制分片长度", "[data-linkseg]",
                   "config.record_segment_sec",
                   ("link.segment_sec", "link.record.segment_sec"))
    base.update(kw)
    return base


def before_snap(seg=300):
    return {"config": {"record_segment_sec": seg},
            "status": {},
            "link": {"segment_sec": seg, "record": {"segment_sec": seg}}}


def after_snap(seg=600):
    return {"config": {"record_segment_sec": seg},
            "status": {},
            "link": {"segment_sec": seg, "record": {"segment_sec": seg}}}


class TestProbe(unittest.TestCase):
    """取值器：缺键必须能和合法的 None / 空列表区分开。"""

    def test_plain_path(self):
        self.assertEqual(bm.probe({"config": {"gpu": "auto"}}, "config.gpu"), "auto")

    def test_missing_is_sentinel(self):
        self.assertIs(bm.probe({"config": {}}, "config.gpu"), bm.MISSING)

    def test_none_is_a_real_value(self):
        # updated_at 默认就是 None；拿 None 当缺失会把合法空值判成采集失败
        v = bm.probe({"config": {"updated_at": None}}, "config.updated_at")
        self.assertIsNone(v)
        self.assertIsNot(v, bm.MISSING)

    def test_star_over_list(self):
        snap = {"link": {"record": {"channels": [{"segment_sec": 300},
                                                 {"segment_sec": 600}]}}}
        self.assertEqual(bm.probe(snap, "link.record.channels.*.segment_sec"),
                         [300, 600])

    def test_star_over_dict(self):
        # frames 是 {cam_id: {...}}，不是数组
        snap = {"status": {"frames": {"a": {"interval": 10},
                                      "b": {"interval": 10}}}}
        self.assertEqual(bm.probe(snap, "status.frames.*.interval"), [10, 10])

    def test_star_on_scalar_is_missing(self):
        self.assertIs(bm.probe({"a": 1}, "a.*.b"), bm.MISSING)

    def test_star_on_empty_container_is_missing(self):
        self.assertIs(bm.probe({"a": []}, "a.*.b"), bm.MISSING)

    def test_has_missing_nested(self):
        self.assertTrue(bm.has_missing([1, {"x": [bm.MISSING]}]))

    def test_has_missing_clean(self):
        self.assertFalse(bm.has_missing([1, {"x": [None, 2]}]))


class TestVerifyRow(unittest.TestCase):
    """四层判定：任何一层没采到，都不许算通过。"""

    def test_all_layers_pass(self):
        r = bm.verify_row(seg_row(), before_snap(), after_snap(),
                          ["record_segment_sec"])
        self.assertEqual(r["state"], "pass", r["reason"])

    def test_changed_missing_key_fails(self):
        r = bm.verify_row(seg_row(), before_snap(), after_snap(), [])
        self.assertEqual(r["state"], "fail")
        self.assertIn("changed", r["reason"])

    def test_changed_none_is_unknown(self):
        r = bm.verify_row(seg_row(), before_snap(), after_snap(), None)
        self.assertEqual(r["state"], "unknown")

    def test_before_none_is_unknown(self):
        r = bm.verify_row(seg_row(), None, after_snap(), ["record_segment_sec"])
        self.assertEqual(r["state"], "unknown")

    def test_after_none_is_unknown(self):
        r = bm.verify_row(seg_row(), before_snap(), None, ["record_segment_sec"])
        self.assertEqual(r["state"], "unknown")

    def test_landed_unchanged_fails(self):
        r = bm.verify_row(seg_row(), before_snap(), before_snap(),
                          ["record_segment_sec"])
        self.assertEqual(r["state"], "fail")
        self.assertIn("落盘", r["reason"])

    def test_expected_mismatch_fails(self):
        # 服务端 coerce 把写入值修成了别的值：落盘了也不算过
        r = bm.verify_row(seg_row(), before_snap(), after_snap(900),
                          ["record_segment_sec"], expected=600)
        self.assertEqual(r["state"], "fail")

    def test_expected_match_passes(self):
        r = bm.verify_row(seg_row(), before_snap(), after_snap(600),
                          ["record_segment_sec"], expected=600)
        self.assertEqual(r["state"], "pass")

    def test_no_expected_only_checks_change(self):
        r = bm.verify_row(seg_row(), before_snap(), after_snap(900),
                          ["record_segment_sec"])
        self.assertEqual(r["state"], "pass")

    def test_landed_probe_missing_is_unknown(self):
        b = {"config": {}, "status": {}, "link": {}}
        r = bm.verify_row(seg_row(), b, after_snap(), ["record_segment_sec"])
        self.assertEqual(r["state"], "unknown")
        self.assertIn("落盘观测点", r["reason"])

    def test_runtime_unchanged_fails(self):
        a = after_snap(600)
        a["link"]["segment_sec"] = 300     # 配置落了盘，运行态没跟上
        r = bm.verify_row(seg_row(), before_snap(), a, ["record_segment_sec"])
        self.assertEqual(r["state"], "fail")
        self.assertIn("运行态没跟着变", r["reason"])

    def test_runtime_probe_missing_is_unknown(self):
        a = after_snap(600)
        del a["link"]["record"]
        r = bm.verify_row(seg_row(), before_snap(), a, ["record_segment_sec"])
        self.assertEqual(r["state"], "unknown")
        self.assertIn("运行态观测点", r["reason"])

    def test_star_probe_with_one_missing_channel_is_unknown(self):
        row = bm._row("cameras", "摄像头列表", "x", "config.cameras.*.name",
                      ("link.record.channels.*.segment_sec",))
        b = {"config": {"cameras": [{"name": "A"}]},
             "link": {"record": {"channels": [{"segment_sec": 300}]}}}
        a = {"config": {"cameras": [{"name": "B"}]},
             "link": {"record": {"channels": [{"segment_sec": 600}, {}]}}}
        r = bm.verify_row(row, b, a, ["cameras"])
        self.assertEqual(r["state"], "unknown")

    def test_no_runtime_probe_is_partial_not_pass(self):
        row = bm._row("cleanup_at", "夜间任务时刻", "x", "config.cleanup_at",
                      (), note="没有下次运行时刻的接口")
        b = {"config": {"cleanup_at": "03:00"}}
        a = {"config": {"cleanup_at": "04:00"}}
        r = bm.verify_row(row, b, a, ["cleanup_at"])
        self.assertEqual(r["state"], "partial")
        self.assertIn("没有下次运行时刻的接口", r["reason"])

    def test_restart_required_is_partial(self):
        row = seg_row(restart_required=True)
        r = bm.verify_row(row, before_snap(), after_snap(), ["record_segment_sec"])
        self.assertEqual(r["state"], "partial")
        self.assertIn("重启", r["reason"])

    def test_proc_not_collected_is_partial(self):
        row = seg_row(proc="-segment_time 600")
        r = bm.verify_row(row, before_snap(), after_snap(), ["record_segment_sec"])
        self.assertEqual(r["state"], "partial")
        self.assertIn("/proc", r["reason"])

    def test_proc_missing_fragment_fails(self):
        row = seg_row(proc="-segment_time 600")
        r = bm.verify_row(row, before_snap(), after_snap(),
                          ["record_segment_sec"], proc="-segment_time 300")
        self.assertEqual(r["state"], "fail")
        self.assertIn("进程命令行", r["reason"])

    def test_proc_hit_passes(self):
        row = seg_row(proc="-segment_time 600")
        r = bm.verify_row(row, before_snap(), after_snap(),
                          ["record_segment_sec"], proc="ffmpeg -i x -segment_time 600 -y")
        self.assertEqual(r["state"], "pass")

    def test_unknown_wins_over_fail(self):
        # 既有 changed 缺失（fail），又有运行态缺键（unknown）：没采全就没资格下结论
        a = after_snap(600)
        del a["link"]["record"]
        r = bm.verify_row(seg_row(), before_snap(), a, [])
        self.assertEqual(r["state"], "unknown")

    def test_checks_are_recorded(self):
        r = bm.verify_row(seg_row(), before_snap(), after_snap(),
                          ["record_segment_sec"])
        names = [c["name"] for c in r["checks"]]
        self.assertIn("POST 承认变更", names)
        self.assertIn("落盘回读", names)
        self.assertTrue(all("ok" in c for c in r["checks"]))


class TestVerifyAll(unittest.TestCase):
    def test_tally_and_total(self):
        rows = [seg_row(), bm._row("cleanup_at", "夜间", "x", "config.cleanup_at")]
        b = {"config": {"record_segment_sec": 300, "cleanup_at": "03:00"},
             "link": {"segment_sec": 300, "record": {"segment_sec": 300}}}
        a = {"config": {"record_segment_sec": 600, "cleanup_at": "04:00"},
             "link": {"segment_sec": 600, "record": {"segment_sec": 600}}}
        rep = bm.verify_all(rows, b, a, ["record_segment_sec", "cleanup_at"])
        self.assertEqual(rep["total"], 2)
        self.assertEqual(rep["tally"]["pass"], 1)
        self.assertEqual(rep["tally"]["partial"], 1)
        self.assertTrue(rep["ok"])

    def test_fail_makes_report_not_ok(self):
        rep = bm.verify_all([seg_row()], before_snap(), after_snap(), [])
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["tally"]["fail"], 1)

    def test_unknown_makes_report_not_ok(self):
        rep = bm.verify_all([seg_row()], before_snap(), after_snap(), None)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["tally"]["unknown"], 1)

    def test_expected_map_is_per_key(self):
        rep = bm.verify_all([seg_row()], before_snap(), after_snap(900),
                            ["record_segment_sec"], expected={"record_segment_sec": 600})
        self.assertEqual(rep["items"][0]["state"], "fail")

    def test_summary_lists_only_problem_rows(self):
        rep = bm.verify_all([seg_row()], before_snap(), after_snap(), [])
        s = bm.summary(rep)
        self.assertIn("未通过", s)
        self.assertIn("[FAIL]", s)
        self.assertIn("record_segment_sec", s)

    def test_summary_of_clean_report(self):
        rep = bm.verify_all([seg_row()], before_snap(), after_snap(),
                            ["record_segment_sec"])
        self.assertIn("通过", bm.summary(rep))


class TestTableIntegrity(unittest.TestCase):
    """表自身的可信度：漏项、重复、拿配置回显冒充运行态，都在这里红。"""

    def test_keys_unique(self):
        keys = [r["key"] for r in bm.ROWS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_rows_have_required_fields(self):
        for r in bm.ROWS:
            for f in ("key", "label", "ui", "landed", "runtime", "runtime_note",
                      "proc", "restart_required"):
                self.assertIn(f, r, "%s 缺 %s" % (r["key"], f))
            self.assertTrue(r["label"] and r["ui"] and r["landed"])

    def test_landed_paths_are_config_readback(self):
        for r in bm.ROWS:
            self.assertTrue(r["landed"].startswith("config."),
                            "%s 的落盘观测点 %s" % (r["key"], r["landed"]))

    def test_runtime_paths_never_point_at_config(self):
        # 这条是「不只查 UI 值」的机器保证：config.* 是同一份落盘值的回显，
        # 拿它当运行态等于把第 2 层查两遍，第 3 层根本没验
        for r in bm.ROWS:
            for p in r["runtime"]:
                self.assertFalse(p.startswith("config."),
                                 "%s 的 %s 是配置回显，不是运行态" % (r["key"], p))

    def test_runtime_paths_are_from_status_or_link(self):
        for r in bm.ROWS:
            for p in r["runtime"]:
                self.assertTrue(p.startswith(("status.", "link.")),
                                "%s 的 %s 不是已知接口" % (r["key"], p))

    def test_empty_runtime_has_a_reason(self):
        for r in bm.ROWS:
            if not r["runtime"]:
                self.assertTrue(r["runtime_note"],
                                "%s 没有运行态出口却没写原因" % r["key"])

    def test_row_keys_are_real_config_keys(self):
        known = set(runtime.DEFAULTS) | {"cameras"}
        for r in bm.ROWS:
            self.assertIn(r["key"], known, "%s 不是配置键" % r["key"])

    def test_every_default_has_a_verdict(self):
        # 服务端新增一个配置键却没人补验收行或免验理由时，这条当场红
        self.assertEqual(bm.coverage_gaps(runtime.DEFAULTS), [])

    def test_coverage_gaps_detects_unknown_key(self):
        self.assertEqual(bm.coverage_gaps(list(runtime.DEFAULTS) + ["brand_new_key"]),
                         ["brand_new_key"])

    def test_rows_and_not_testable_do_not_overlap(self):
        self.assertFalse({r["key"] for r in bm.ROWS} & set(bm.NOT_TESTABLE))

    def test_known_gaps_and_rows_do_not_overlap(self):
        self.assertFalse({r["key"] for r in bm.ROWS} &
                         {g["key"] for g in bm.KNOWN_GAPS if g.get("key")})

    def test_not_testable_all_have_reason(self):
        for k, why in bm.NOT_TESTABLE.items():
            self.assertTrue(why and len(why) > 6, "%s 的免验理由太敷衍" % k)

    def test_known_gaps_all_documented(self):
        for g in bm.KNOWN_GAPS:
            self.assertTrue(g["label"] and g["ui"] and g["why"])
            self.assertIn("key", g)

    def test_proc_fragments_are_ffmpeg_flags(self):
        for r in bm.ROWS:
            if r["proc"]:
                self.assertTrue(r["proc"].startswith("-segment"),
                                "%s 的进程断言 %s" % (r["key"], r["proc"]))

    def test_restart_rows_are_marked(self):
        marked = {r["key"] for r in bm.ROWS if r["restart_required"]}
        self.assertIn("record_dir", marked)


class TestSourceGuard(unittest.TestCase):
    """表必须跟着代码走：前端改了 pushCfg，这里不更新就红。"""

    def test_live_js_exists(self):
        self.assertTrue(os.path.isfile(LIVE_JS))

    def test_body_keys_all_have_rows(self):
        with open(LIVE_JS, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"const body = \{(.*?)\};", text, re.S)
        self.assertIsNotNone(m, "pushCfg 的 body 写法变了，守卫要跟着改")
        keys = set(re.findall(r"\b([a-z][a-z0-9_]*)\s*:", m.group(1)))
        pushed = keys & set(runtime.DEFAULTS)
        self.assertTrue(pushed, "一个配置键都没抓到，守卫已经失效")
        rows = {r["key"] for r in bm.ROWS}
        self.assertEqual(pushed - rows, set(),
                         "前端推了但表里没有验收行：%s" % sorted(pushed - rows))

    def test_source_keys_all_explained(self):
        mentioned = bm.source_keys(LIVE_JS) & set(runtime.DEFAULTS)
        self.assertTrue(mentioned)
        self.assertEqual(mentioned - bm.covered_keys(), set())

    def test_source_keys_find_core_items(self):
        found = bm.source_keys(LIVE_JS)
        for k in ("record_enable", "record_segment_sec", "detect", "notify"):
            self.assertIn(k, found)


if __name__ == "__main__":
    unittest.main(verbosity=1)
