#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨设备 RTSP 地址保护（0.9.24 补丁）的行为守卫。

背景事故：明文 RTSP 地址只存在「配置它的那台浏览器」的 localStorage，
服务端回给浏览器的永远是脱敏串（rtsp://***@）。换设备/清缓存后前端
本地没有明文，任何一次设置推送都带着 url:"" —— 以前服务端直接整表
替换，NAS 上的真地址就这样被抹掉了（用户现象：名字和忽略区还在，
提示「未配置 RTSP 地址」）。

守住三条：
  U1 空/脱敏回落：推回的 url 为空或含 ***@ → 按 id 回落 NAS 旧值
  U2 显式清空：只有 url_clear/sub_clear（本地曾有明文且被用户清空）
     或推来新明文，才真的改动地址；新明文优先于 clear 标记
  U3 通道：norm_cameras 要把 url_clear/sub_clear 带进干净结构

跑：python test/test_url_keep.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))

import nvr_slim  # noqa: E402

OLD = nvr_slim.norm_cameras([
    {"id": "cam-01", "name": "车库", "url": "rtsp://admin:pw@192.168.2.108:554/ch1",
     "sub_url": "rtsp://admin:pw@192.168.2.108:554/ch3", "enabled": True},
    {"id": "cam-02", "name": "后院", "url": "rtsp://admin:pw@192.168.2.116:554/101",
     "sub_url": "", "enabled": True},
])


def push(cams):
    """模拟一次浏览器推送后的最终 cameras（服务端落盘前的样子）。"""
    out = nvr_slim.norm_cameras(cams)
    return nvr_slim.merge_cam_urls(out, OLD)


class TestUrlKeep(unittest.TestCase):
    def test_u1_empty_url_falls_back(self):
        """换设备推送：本机没有明文 → url 是空 → 必须回落 NAS 旧值"""
        out = push([{"id": "cam-01", "name": "车库"},
                    {"id": "cam-02", "name": "后院"}])
        self.assertEqual(out[0]["url"], OLD[0]["url"])
        self.assertEqual(out[0]["sub_url"], OLD[0]["sub_url"])
        self.assertEqual(out[1]["url"], OLD[1]["url"])

    def test_u1_masked_url_falls_back(self):
        """万一脱敏串被原样推回（旧版本前端路径），同样不许覆盖"""
        out = push([{"id": "cam-01", "name": "车库",
                     "url": "rtsp://***@192.168.2.108:554/ch1"}])
        self.assertEqual(out[0]["url"], OLD[0]["url"])

    def test_u2_new_plaintext_wins(self):
        """用户在任意设备填了新地址 → 直接采用"""
        new = "rtsp://root:toor@10.0.0.9:554/live"
        out = push([{"id": "cam-01", "name": "车库", "url": new}])
        self.assertEqual(out[0]["url"], new)

    def test_u2_explicit_clear(self):
        """本地曾有明文、保存时清空 → url_clear 才真清"""
        out = push([{"id": "cam-01", "name": "车库",
                     "url": "", "url_clear": True}])
        self.assertEqual(out[0]["url"], "")
        self.assertEqual(out[0]["sub_url"], OLD[0]["sub_url"])  # 子码流未标清，保留

    def test_u2_new_plaintext_beats_clear_flag(self):
        """新明文优先于残留的 clear 标记（防止旧标记误清新地址）"""
        new = "rtsp://root:toor@10.0.0.9:554/live"
        out = push([{"id": "cam-01", "name": "车库",
                     "url": new, "url_clear": True}])
        self.assertEqual(out[0]["url"], new)
        self.assertNotIn("url_clear", out[0])   # 标记用完即摘，不进配置

    def test_u2_sub_clear(self):
        out = push([{"id": "cam-01", "name": "车库", "sub_clear": True}])
        self.assertEqual(out[0]["url"], OLD[0]["url"])
        self.assertEqual(out[0]["sub_url"], "")

    def test_new_cam_no_old_value(self):
        """新加的摄像头没有旧值可落 → 空就是空，等用户填"""
        out = push([{"id": "cam-01", "name": "车库"},
                    {"id": "cam-99", "name": "新机位"}])
        self.assertEqual(out[1]["url"], "")

    def test_empty_old_config(self):
        """全新安装（NAS 上还没有 cameras）→ 不炸，空回落为空"""
        out = nvr_slim.merge_cam_urls(
            nvr_slim.norm_cameras([{"id": "cam-01", "name": "车库"}]), None)
        self.assertEqual(out[0]["url"], "")

    def test_u3_norm_carries_clear_flags(self):
        """norm_cameras 必须把显式清空标记带进干净结构，否则上面全白搭"""
        out = nvr_slim.norm_cameras(
            [{"id": "cam-01", "name": "车库", "url_clear": True, "sub_clear": True}])
        self.assertTrue(out[0]["url_clear"])
        self.assertTrue(out[0]["sub_clear"])

    def test_roundtrip_stability(self):
        """空推送往返两次，地址不丢也不产生多余 diff（否则每次推送都写盘）"""
        once = push([{"id": "cam-01", "name": "车库"},
                     {"id": "cam-02", "name": "后院"}])
        again = nvr_slim.merge_cam_urls(
            nvr_slim.norm_cameras([{"id": "cam-01", "name": "车库"},
                                   {"id": "cam-02", "name": "后院"}]), once)
        self.assertEqual(once, again)
        self.assertEqual(again, OLD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
