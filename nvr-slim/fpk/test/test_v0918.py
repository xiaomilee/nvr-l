#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0.9.18 产品化打磨的行为守卫。

五条需求各守一批断言：
  R1  端口默认 18080：manifest / 向导 / runtime / cmd 四处一致
  R2  录像目录默认 /vol1/@appshare/nvr-output（0.9.20 起改回 fnOS 自动授权
      的共享目录；个人存储空间在应用沙箱下建不出也写不进）：默认值、
      目录探测（存在性 + 真实写探针）
  R3  每路缩时压制总开关：norm_cameras 透传、link 入队过滤
  R4  缩时倍速：白名单 1/2/4/8，倍速命令必须重编码（setpts）
  R5  缩时时间轴按源时刻：realSegs 的 src/srcEnd 与 UI 取值函数
      （UI 侧只做静态断言，交互逻辑由 e2e_ui.js 覆盖）

跑：python test/test_v0918.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "app", "server")
sys.path.insert(0, SERVER)

import ffmpeg_cmd  # noqa: E402
import link as link_mod  # noqa: E402
import nvr_slim  # noqa: E402
import runtime  # noqa: E402

MANIFEST = os.path.join(ROOT, "manifest")
WIZARD = os.path.join(ROOT, "wizard", "install")
CMD_MAIN = os.path.join(ROOT, "cmd", "main")
INDEX = os.path.join(ROOT, "app", "ui", "index.html")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestR1Port(unittest.TestCase):
    def test_runtime_default(self):
        self.assertEqual(runtime.DEFAULTS["service_port"], 18080)
        self.assertEqual(runtime.clamp_port("abc"), 18080)

    def test_manifest_and_wizard_agree(self):
        m = _read(MANIFEST)
        self.assertRegex(m, r"(?m)^service_port\s*=\s*18080\s*$")
        w = _read(WIZARD)
        self.assertIn('"initValue": "18080"', w)
        self.assertNotIn('"initValue": "18081"', w)

    def test_cmd_main_fallback(self):
        self.assertIn('TRIM_SERVICE_PORT:-18080', _read(CMD_MAIN))

    def test_server_arg_default(self):
        src = _read(os.path.join(SERVER, "nvr_slim.py"))
        self.assertIn('TRIM_SERVICE_PORT", "18080"', src)


class TestR2RecordDir(unittest.TestCase):
    def test_default_value(self):
        self.assertEqual(runtime.DEFAULTS["record_dir"], "/vol1/@appshare/nvr-output")

    def test_wizard_and_installer_agree(self):
        w = _read(WIZARD)
        self.assertIn('/vol1/@appshare/nvr-output', w)
        inst = _read(os.path.join(ROOT, "cmd", "install_callback"))
        # 回退链里必须还有共享目录；不可行的写死默认值必须消失。
        # 注释里提到旧默认值是刻意的教训记录，忽略 # 开头的行再查。
        code = "\n".join(l for l in inst.splitlines() if not l.lstrip().startswith("#"))
        self.assertIn('/vol1/@appshare/nvr-output', code)
        self.assertNotIn('/vol2/1000/NVRL', code)
        # 目录建不出不再 fail 安装：真实写探针 + 降级留痕
        self.assertIn('can_write()', code)
        self.assertIn('DIR_WARN', code)

    def test_valid_record_dir(self):
        ok, _ = runtime.valid_record_dir("/vol1/@appshare/nvr-output")
        self.assertTrue(ok)
        for bad in ("", "nvr", "../x", "/etc", "/"):
            ok, why = runtime.valid_record_dir(bad)
            self.assertFalse(ok, "不应接受 %r" % bad)

    def test_coerce_rejects_relative(self):
        self.assertEqual(runtime.coerce("record_dir", "nvr"),
                         runtime.DEFAULTS["record_dir"])
        self.assertEqual(runtime.coerce("record_dir", "  /vol3/abc  "), "/vol3/abc")

    def test_probe_creates_and_writes(self):
        base = tempfile.mkdtemp(prefix="nvrdir")
        try:
            target = os.path.join(base, "a", "b")
            res = runtime.probe_record_dir(target, create=False)
            self.assertFalse(res["ok"])
            self.assertFalse(res["exists"])
            res = runtime.probe_record_dir(target, create=True)
            self.assertTrue(res["ok"], res["error"])
            self.assertTrue(res["created"])
            self.assertTrue(os.path.isdir(target))
            self.assertFalse(os.listdir(target))   # 探针文件必须删掉
            # 已存在的目录：不再报 created
            res = runtime.probe_record_dir(target, create=True)
            self.assertTrue(res["ok"] and not res["created"])
        finally:
            shutil.rmtree(base, ignore_errors=True)

    @unittest.skipIf(os.name == "nt",
                     "路径合法性按 POSIX 校验（/ 开头），Windows 上无法构造该形态的真实文件")
    def test_probe_rejects_file_path(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        try:
            res = runtime.probe_record_dir(f.name, create=True)
            self.assertFalse(res["ok"])
            self.assertIn("文件", res["error"])
        finally:
            os.unlink(f.name)


class TestR3CondenseSwitch(unittest.TestCase):
    def test_norm_cameras_passes_switch_and_speed(self):
        out = nvr_slim.norm_cameras([
            {"id": "a", "name": "A"},                                  # 缺省 → 开
            {"id": "b", "name": "B", "condense": False},               # 显式关
            {"id": "c", "name": "C", "condense": "false"},             # 字符串关
            {"id": "d", "name": "D", "condense": "off"},
            {"id": "e", "name": "E", "clip_speed": 4},
            {"id": "f", "name": "F", "clip_speed": "2"},
            {"id": "g", "name": "G", "clip_speed": 3},                 # 非白名单 → 1
            {"id": "h", "name": "H", "clip_speed": "abc"},
        ])
        by = {c["id"]: c for c in out}
        self.assertTrue(by["a"]["condense"])
        self.assertFalse(by["b"]["condense"])
        self.assertFalse(by["c"]["condense"])
        self.assertFalse(by["d"]["condense"])
        self.assertEqual(by["e"]["clip_speed"], 4)
        self.assertEqual(by["f"]["clip_speed"], 2)
        self.assertEqual(by["g"]["clip_speed"], 1)
        self.assertEqual(by["h"]["clip_speed"], 1)

    def test_cameras_expose_fields(self):
        old = nvr_slim.STATE["var_dir"]
        tmp = tempfile.mkdtemp(prefix="nvrcfg")
        try:
            nvr_slim.STATE["var_dir"] = tmp
            cams = nvr_slim.cameras()
            for c in cams:
                self.assertIn("condense", c)
                self.assertIn("clip_speed", c)
        finally:
            nvr_slim.STATE["var_dir"] = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_link_skips_condense_off(self):
        tmp = tempfile.mkdtemp(prefix="nvrlink")
        try:
            root = os.path.join(tmp, "rec")
            cams = [{"id": "cam-01", "name": "A", "enabled": True, "condense": False},
                    {"id": "cam-02", "name": "B", "enabled": True, "condense": True}]
            lk = link_mod.Link("ffmpeg", "ffprobe", root, tmp, cams, {}, log=lambda m: None)
            base = os.path.join(root, "cam-02", "2026-10-20")
            os.makedirs(base, exist_ok=True)
            # 至少两个分片：最后一个被视为"正在写"，只有前面的会入队
            for name in ("10-00-00.mp4", "10-05-00.mp4"):
                with open(os.path.join(base, name), "wb") as f:
                    f.write(b"x" * 64)
            items = lk._new_items()
            self.assertEqual([it["cam"] for it in items], ["cam-02"])
            self.assertEqual(lk.clip_speed_of("cam-02"), 1)
            lk.cams[1]["clip_speed"] = 8
            self.assertEqual(lk.clip_speed_of("cam-02"), 8)
            self.assertEqual(lk.clip_speed_of("cam-01"), 1)
            self.assertEqual(lk.clip_speed_of("missing"), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestR4Speed(unittest.TestCase):
    def test_fast_copy_untouched_at_1x(self):
        cmd = ffmpeg_cmd.encode_cmd("ffmpeg", "in.mp4", "o.mp4",
                                    {"mode": "fast", "speed": 1}, ss=1, to=2)
        line = " ".join(cmd)
        self.assertIn("-c copy", line)
        self.assertNotIn("setpts", line)
        self.assertNotIn("libx264", line)

    def test_speed_forces_reencode(self):
        cmd = ffmpeg_cmd.encode_cmd("ffmpeg", "in.mp4", "o.mp4",
                                    {"mode": "fast", "speed": 2}, ss=1, to=2)
        line = " ".join(cmd)
        self.assertIn("setpts=PTS/2", line)
        self.assertIn("libx264", line)
        self.assertNotIn("copy", line)

    def test_speed_window_still_applies(self):
        cmd = ffmpeg_cmd.encode_cmd("ffmpeg", "in.mp4", "o.mp4",
                                    {"mode": "fast", "speed": 4}, ss=5, to=25)
        line = " ".join(cmd)
        self.assertIn("-ss", cmd)
        self.assertIn("-to", cmd)
        self.assertIn("setpts=PTS/4", line)
        i_ss, i_i = cmd.index("-ss"), cmd.index("-i")
        self.assertLess(i_ss, i_i, "输入寻址必须放在 -i 之前")

    def test_fine_mode_speed_prepends_setpts(self):
        enc = {"mode": "fine", "codec": "hevc",
               "day": {"quality": 26, "fps": 12, "scale": 100, "denoise": 0},
               "speed": 2}
        cmd = ffmpeg_cmd.encode_cmd("ffmpeg", "in.mp4", "o.mp4", enc)
        i = cmd.index("-vf")
        filt = cmd[i + 1]
        self.assertTrue(filt.startswith("setpts=PTS/2,"), filt)
        self.assertIn("fps=12", filt)

    def test_speed_garbage_falls_back(self):
        cmd = ffmpeg_cmd.encode_cmd("ffmpeg", "in.mp4", "o.mp4",
                                    {"mode": "fast", "speed": "x"})
        self.assertNotIn("setpts", " ".join(cmd))


class TestR5ClipTimeline(unittest.TestCase):
    def test_ui_has_source_time_helpers(self):
        ui = _read(INDEX)
        # 0.9.24 起缩时压制收进四段手风琴：折叠开关由 data-acc 统一管，
        # 原来的 data-tltoggle 已下线，倍速 chip 仍在。
        for needle in ("function segA(", "function segB(",
                       "src: st, srcEnd: en", "seg.srcEnd", "api/dir/check",
                       "btnDirSave", 'data-acc="', 'data-speed="'):
            self.assertIn(needle, ui, "UI 缺少 %s" % needle)

    def test_ui_default_dir_and_port(self):
        ui = _read(INDEX)
        self.assertIn('dir:"/vol1/@appshare/nvr-output"', ui)
        self.assertIn("port:18080", ui)
        self.assertNotIn('dir:"/vol2/1000/NVRL"', ui)


class TestVersion(unittest.TestCase):
    def test_version_bumped(self):
        self.assertEqual(runtime.VERSION, "0.9.91")
        m = _read(MANIFEST)
        self.assertRegex(m, r"(?m)^version\s*=\s*0\.9\.91\s*$")


class TestWizardPackaging(unittest.TestCase):
    """0.9.19：wizard 从未进包（打包器开关默认关），安装向导从没出现过。

    这批守卫盯三件事：打包器默认带 wizard；包内桌面入口端口与新默认一致；
    升级路径绝不改动用户已在用的端口。
    """

    def test_packer_includes_wizard_by_default(self):
        src = _read(os.path.join(ROOT, "tools", "pack_fpk.py"))
        self.assertIn('WITH_WIZARD = os.environ.get("NVR_PKG_WIZARD", "1") != "0"', src)

    def test_ui_config_port_matches_default(self):
        uic = json.loads(_read(os.path.join(ROOT, "app", "ui", "config")))
        self.assertEqual(uic[".url"]["nvr.main"]["port"], "18080")

    def test_config_wizard_port_synced(self):
        wc = _read(os.path.join(ROOT, "wizard", "config"))
        self.assertIn('"initValue": "18080"', wc)
        self.assertNotIn('"initValue": "18081"', wc)

    def test_install_callback_preserves_existing_port(self):
        src = _read(os.path.join(ROOT, "cmd", "install_callback"))
        # 升级（无向导端口）时读现有配置并 pinport 对齐，不走 setport 顺延
        self.assertIn('PORT_HAVE="$(sed -n', src)
        self.assertIn('pinport --port "$PORT_HAVE"', src)
        # 旧的「向导缺省直接落到 manifest 端口」覆盖链必须消失
        self.assertNotIn(
            'PORT_WANT="${wizard_port:-${port:-${wizard_app_port:-${TRIM_SERVICE_PORT',
            src)

    def test_upgrade_callback_repins_ui_port(self):
        src = _read(os.path.join(ROOT, "cmd", "upgrade_callback"))
        self.assertIn("pinport --port", src)
        self.assertIn('PORT_HAVE="$(sed -n', src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
