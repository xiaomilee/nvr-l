#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集器的离线守卫：取值策略本身对不对，不需要起服务就能验。

为什么要单独钉一层
------------------
采集器的判定全压在「换成另一个合法值」这一步上。这一步错了，报告会一片绿
或者一片红，而且两种都不好查：值写死成 120 而配置本来就是 120 时，POST 同值
不进 changed，四层证据一起消失 —— 看起来像产品没生效，其实是采集器没按下去。

所以这里不进 HTTP、不起服务，只验：
  1. 每个验收项都有取值策略，且没有多余的策略（表和策略不许各说一套）；
  2. 策略给出的值一定和当前值不同（否则「没按」会被当成「按了」）；
  3. 这个值能原样通过产品自己的 runtime.coerce —— 采集器无权自己发明范围；
  4. 二次取值还会变（枚举/布尔这类有限集合最容易退化成同值）；
  5. 报告口径：partial 算过、fail/unknown 不算，且 partial 必须被列出来。

跑法： python test/test_btn_harvest.py
"""
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))
import button_matrix as bm          # noqa: E402
import btn_harvest as bh            # noqa: E402
import runtime                      # noqa: E402

CTX = {"var": os.path.join(HERE, "_tmp_harvest")}   # 只在取目录时用到，不真建


def start_cfg():
    """采集器的起手配置：产品默认值 + 两路摄像头（与 Srv.start 的种子同形）。"""
    cfg = json.loads(json.dumps(runtime.DEFAULTS))
    cfg["record_dir"] = "/tmp/nvr-harvest-rec"
    cfg["cameras"] = [
        {"id": "cam-01", "name": "甲", "url": "rtsp://u:p@127.0.0.1:1/s1", "enabled": True},
        {"id": "cam-02", "name": "乙", "url": "rtsp://u:p@127.0.0.1:2/s2", "enabled": True},
    ]
    return cfg


def pick(key, cfg=None):
    cfg = cfg or start_cfg()
    return bh.PICKERS[key](cfg.get(key), cfg, CTX)


class TestPickerCoverage(unittest.TestCase):
    def test_01_every_row_has_picker(self):
        """验收表加一行就必须有对应的取值策略，否则报告里那项永远是 unknown。"""
        missing = [r["key"] for r in bm.ROWS if r["key"] not in bh.PICKERS]
        self.assertEqual(missing, [], "这些验收项没有取值策略：%s" % missing)

    def test_02_no_orphan_picker(self):
        """策略里也不许多出验收表没有的键：多出来的等于偷偷改一个没人验收的配置项。"""
        rows = {r["key"] for r in bm.ROWS}
        self.assertEqual(sorted(set(bh.PICKERS) - rows), [],
                         "这些策略没有对应的验收行")

    def test_03_picked_value_differs_from_current(self):
        """「换一个值」必须真的换：同值 POST 不进 changed，四层证据会一起消失。"""
        cfg = start_cfg()
        for row in bm.ROWS:
            with self.subTest(项=row["key"]):
                post, _expect = pick(row["key"], cfg)
                self.assertNotEqual(post, cfg.get(row["key"]),
                                    "%s 取到了与当前值相同的值，等于没按" % row["key"])

    def test_04_picked_value_survives_coerce(self):
        """采集器不许自己发明范围：给的值必须原样通过产品自己的收敛函数。"""
        cfg = start_cfg()
        for row in bm.ROWS:
            key = row["key"]
            with self.subTest(项=key):
                post, expect = pick(key, cfg)
                if key == "cameras":
                    # cameras 不走 coerce（handler 用 norm_cameras 单独处理），
                    # 名字是这一项的锚点，按名字核。
                    self.assertEqual([c["name"] for c in post], expect)
                    self.assertEqual(len(post), len(cfg["cameras"]))
                elif key == "detect":
                    self.assertEqual(runtime.coerce("detect", post)["enable"], expect)
                elif key == "notify":
                    self.assertEqual(runtime.coerce("notify", post)["cooldown"], expect)
                else:
                    self.assertEqual(runtime.coerce(key, post), expect,
                                     "%s 的取值被 coerce 改成了 %r"
                                     % (key, runtime.coerce(key, post)))

    def test_05_numbers_inside_documented_ranges(self):
        """数值项的取值必须落在界面与实现共同约定的区间里。

        这些区间在 /api/config 的夹取与 Link.reconfigure 的 clamp 里各写了一份，
        采集器写歪了不会报错，只会悄悄把某一项写成边界值。
        """
        ranges = {"record_segment_sec": (60, 900), "snapshot_interval": (1, 10),
                  "concurrency": (1, 3), "link_interval_h": (1, 24),
                  "link_max_queue": (20, 2000), "archive_days": (1, 90),
                  "archive_batch": (10, 500)}
        for key, (lo, hi) in ranges.items():
            with self.subTest(项=key):
                post, _ = pick(key)
                self.assertIsInstance(post, int)
                self.assertTrue(lo <= post <= hi, "%s=%r 越界 [%d, %d]" % (key, post, lo, hi))

    def test_06_enums_inside_whitelist(self):
        """枚举项从产品白名单里取，别在采集器里另抄一份取值表。"""
        for key in ("link_mode", "clip_style", "condense_mode", "gpu"):
            with self.subTest(项=key):
                post, _ = pick(key)
                self.assertIn(post, runtime._ENUM_KEYS[key])

    def test_07_cleanup_at_shape(self):
        """改时刻不改格式：格式坏掉之后 retention.parse_hhmm 会天天抛异常。"""
        post, _ = pick("cleanup_at")
        hh, mm = post.split(":")
        self.assertEqual(len(hh), 2)
        self.assertEqual(len(mm), 2)
        self.assertTrue(0 <= int(hh) <= 23 and 0 <= int(mm) <= 59)

    def test_08_detect_block_keeps_other_fields(self):
        """整块推回去只能动总闸：顺手把类别/阈值清成默认，等于采集器改了别人的调参。"""
        cfg = start_cfg()
        cfg["detect"]["conf"]["person"] = 0.61
        post, expect = bh.PICKERS["detect"](cfg["detect"], cfg, CTX)
        self.assertEqual(post["conf"]["person"], 0.61)
        self.assertEqual(expect, not cfg["detect"]["enable"])

    def test_09_second_pick_still_changes(self):
        """拿刚写进去的值再取一次，必须还能变 —— 否则第二次采集就是同值假绿。"""
        for key in ("record_enable", "record_frag", "link_mode", "clip_style",
                    "condense_mode", "record_segment_sec", "concurrency"):
            with self.subTest(项=key):
                cfg = start_cfg()
                first, _ = pick(key, cfg)
                cfg[key] = first
                second, _ = pick(key, cfg)
                self.assertNotEqual(second, first)


class TestReportRules(unittest.TestCase):
    def _items(self, states):
        return [{"key": "k%d" % i, "label": "项%d" % i, "state": s, "reason": "r", "checks": []}
                for i, s in enumerate(states)]

    def test_10_partial_counts_as_pass(self):
        """partial 是「如实标注还差什么」，不是没过；fail/unknown 才是没过。"""
        r = bm.roll_up(self._items(["pass", "partial"]))
        self.assertTrue(r["ok"])
        self.assertEqual(r["tally"], {"pass": 1, "partial": 1, "fail": 0, "unknown": 0})

    def test_11_fail_and_unknown_block(self):
        for bad in ("fail", "unknown"):
            with self.subTest(状态=bad):
                r = bm.roll_up(self._items(["pass", bad]))
                self.assertFalse(r["ok"])

    def test_12_summary_lists_partials(self):
        """「通过 9 / 部分 11」不能只写一行：那 11 项只验到落盘，必须逐条列出来。"""
        r = bm.roll_up(self._items(["pass", "partial"]))
        r["restore"] = {"ok": True, "diff": []}
        r["env"] = {"frames": True}
        text = bh.summary(r)
        self.assertIn("部分通过 1 项", text)
        self.assertIn("项1", text)

    def test_13_restore_diff_ignores_updated_at(self):
        """updated_at 是每次保存自动写的时间戳，不该被算成「配置没还原」。"""
        a = {"cleanup_at": "03:00", "updated_at": "t1"}
        b = {"cleanup_at": "03:00", "updated_at": "t2"}
        self.assertEqual(bh.diff_keys(a, b), [])
        self.assertEqual(bh.diff_keys({"x": 1}, {"x": 2}), ["x"])


class TestStaysLocal(unittest.TestCase):
    def test_14_no_lan_or_ssh(self):
        """采集器只允许打本机回环。

        改配置是有副作用的动作。这个脚本一旦能指向测试机/生产机，
        「跑一遍看看」就会变成一次真实的配置抖动 —— 生产机 192.168.2.6
        的录制断 5 秒会被缺口检测记成真缺口。所以这里把口子钉死：
        源码里不许出现 LAN 地址，也不许引入 SSH 通道。
        """
        src = ""
        with io.open(os.path.join(HERE, "btn_harvest.py"), encoding="utf-8") as f:
            src = f.read()
        for bad in ("192.168.", "paramiko", "ssh ", "scp "):
            self.assertNotIn(bad, src, "采集器里不该出现 %r" % bad)
        self.assertIn("127.0.0.1", src)

    def test_15_record_enable_evidence_excludes_process_count(self):
        """录制总开关的生效证据只认 Recorder 的开关本身。

        status.cams.recording 数的是「此刻真在跑的 ffmpeg」：没装 ffmpeg、
        上游没数据、或者进程正在重启，它都是 0。把它当开关的回执，
        会把「环境没有 ffmpeg」记成「关录像没生效」——第 23 轮首跑就这么红过一次。
        """
        row = [r for r in bm.ROWS if r["key"] == "record_enable"][0]
        self.assertEqual(row["runtime"], ["link.record.enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
