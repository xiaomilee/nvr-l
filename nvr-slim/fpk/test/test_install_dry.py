#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install_callback / upgrade_callback 真实干跑。

背景：0.9.20 的 install_callback 在最后写安装日志时引用了一个只在
"无向导无配置"分支才定义的变量（PORT_WANT），set -u 下触发 unbound
variable，脚本以非零退出——此时配置其实已写好，但 fnOS 只会显示
「执行脚本出错且原因未知」。bash -n 抓不到这种运行期错误，只有真跑
一遍才知道。所以这个测试**真的执行脚本**，三种安装场景各跑一遍。

跑：python test/test_install_dry.py   （无 bash 环境时整体 skip）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BASH = shutil.which("bash")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(BASH, "需要 bash 环境（fnOS 自带；开发机用 Git Bash）")
class TestInstallCallbackDryRun(unittest.TestCase):
    def _stage(self):
        work = tempfile.mkdtemp(prefix="nvrdry")
        app = os.path.join(work, "app")
        shutil.copytree(os.path.join(ROOT, "app"), app,
                        ignore=shutil.ignore_patterns("__pycache__"))
        return work, app

    def _run_install(self, wizard_env, app=None, var=None):
        """跑一次完整 install_callback，断言退出码 0 且 port.txt 与配置一致。

        app/var 可由调用方预先布置（升级场景要先放 config.json）。
        """
        if app is None:
            work, app = self._stage()
            var = os.path.join(work, "var")
        env = dict(os.environ)
        env.update({
            "TRIM_APPDEST": app,
            "TRIM_PKGVAR": var,
            "TRIM_APPNAME": "nvr",
            "TRIM_APPVER": "0.9.21",
            # 干跑不起服务，只看脚本退出码与落盘结果
            "TRIM_TEMP_LOGFILE": os.path.join(var, "install-msg.log"),
            "NVR_PYTHON": sys.executable,
        })
        env.update(wizard_env)

        p = subprocess.run([BASH, os.path.join(ROOT, "cmd", "install_callback")],
                           env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0,
                         "install_callback 非零退出（fnOS 会显示「执行脚本出错且原因未知」）\n"
                         "stderr:\n%s" % p.stderr[-2000:])

        cfg = json.loads(_read(os.path.join(var, "config.json")))
        # port.txt 必须与配置一致：cmd/main 的健康检查等它
        self.assertEqual(_read(os.path.join(var, "port.txt")).strip(),
                         str(cfg["service_port"]))
        return cfg, p

    def test_fresh_install_with_wizard_defaults(self):
        work, _ = self._stage()
        rec = os.path.join(work, "rec-from-wizard")
        cfg, _ = self._run_install({"wizard_record_dir": rec, "wizard_port": "18080"},
                                   var=os.path.join(work, "var"))
        self.assertEqual(cfg["record_dir"], rec)
        self.assertEqual(cfg["service_port"], 18080)

    def test_wizard_dir_unwritable_falls_back(self):
        """向导目录建不出 → 不许 fail 安装，回退共享目录并留痕。"""
        work, _ = self._stage()
        # blocker 直接建成文件，其下的 mkdir -p 必败
        with open(os.path.join(work, "blocker"), "w") as f:
            f.write("x")
        bad = os.path.join(work, "blocker", "deep", "noperm")
        cfg, p = self._run_install({"wizard_record_dir": bad, "wizard_port": "18080"},
                                   var=os.path.join(work, "var"))
        # Git Bash 会把 /vol1/... 映射成宿主机物理路径，只断言结尾段
        self.assertTrue(cfg["record_dir"].endswith("vol1/@appshare/nvr-output"),
                        "应回退到共享目录，实际 %s" % cfg["record_dir"])
        self.assertIn("已回退", p.stderr, "降级必须在安装输出里留痕")

    def test_upgrade_keeps_existing_port_and_dir(self):
        """升级（无向导变量）：端口与目录必须原样保留。"""
        work, app = self._stage()
        var = os.path.join(work, "var")
        os.makedirs(var, exist_ok=True)
        # config.json 里的路径用正斜杠（fnOS 上是 POSIX 路径；Git Bash 也认 C:/）
        old_rec = os.path.join(work, "old-rec").replace("\\", "/")
        os.makedirs(old_rec, exist_ok=True)
        with open(os.path.join(var, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"service_port": 18099, "record_dir": old_rec}, f)
        cfg, _ = self._run_install({}, app=app, var=var)
        self.assertEqual(cfg["service_port"], 18099, "升级不得改动用户端口")
        self.assertEqual(cfg["record_dir"], old_rec, "升级不得改动用户目录")


if __name__ == "__main__":
    unittest.main(verbosity=1)
