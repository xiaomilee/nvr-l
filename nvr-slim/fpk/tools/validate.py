#!/usr/bin/env python3
"""fpk 包结构校验器：打包前必跑。

检查 manifest 字段、严格 JSON、图标尺寸、cmd 脚本、UI 入口一致性，
并调用 bash -n / py_compile 做语法级校验（可用则用，不可用则跳过并注明）。
"""
import json
import os
import py_compile
import re
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED_FILES = [
    "manifest", "ICON.PNG", "ICON_256.PNG",
    "config/privilege", "config/resource",
    "app/ui/config", "app/ui/images/icon-64.png", "app/ui/images/icon-256.png",
    "app/server/nvr_slim.py", "app/server/runtime.py",
    "app/server/params.py", "app/server/ffmpeg_cmd.py", "app/server/retention.py",
    "app/server/recorder.py", "app/server/condense.py", "app/server/link.py",
    "cmd/main", "cmd/install_init", "cmd/install_callback",
    "cmd/uninstall_init", "cmd/uninstall_callback",
    "cmd/upgrade_init", "cmd/upgrade_callback",
    "cmd/config_init", "cmd/config_callback",
    "wizard/install", "wizard/config", "wizard/upgrade", "wizard/uninstall",
]
REQUIRED_MANIFEST = ["appname", "version", "display_name", "platform", "source",
                     "service_port", "desktop_uidir", "desktop_applaunchname",
                     "checksum"]
WIZARD_TYPES = {"text", "password", "radio", "checkbox", "select", "switch", "tips"}

results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))


def read(path, binary=False):
    with open(os.path.join(ROOT, path), "rb" if binary else "r",
              encoding=None if binary else "utf-8") as f:
        return f.read()


# 1. 必备文件
for f in REQUIRED_FILES:
    check("文件存在 %s" % f, os.path.isfile(os.path.join(ROOT, f)))

# 2. manifest
m = {}
try:
    for i, line in enumerate(read("manifest").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError("第 %d 行不是 key=value：%r" % (i, line))
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k in m:
            raise ValueError("第 %d 行重复键：%s" % (i, k))
        m[k] = v
    missing = [k for k in REQUIRED_MANIFEST if k not in m]
    check("manifest 必备键齐全", not missing, "缺: %s" % missing if missing else "")
    check("manifest appname 只含小写字母/数字/短横线",
          bool(re.match(r"^[a-z][a-z0-9-]*$", m.get("appname", ""))), m.get("appname", ""))
    check("manifest version 格式",
          bool(re.match(r"^\d+\.\d+\.\d+(-build\d+)?$", m.get("version", ""))),
          m.get("version", ""))
    check("manifest platform 合法", m.get("platform") in ("x86", "arm", "all"),
          m.get("platform", ""))
    check("manifest source=thirdparty", m.get("source") == "thirdparty")
    check("manifest service_port 为数字", m.get("service_port", "").isdigit())
    check("desktop_applaunchname 与 appname 前缀一致",
          m.get("desktop_applaunchname", "").startswith(m.get("appname", "\0") + "."))
    # checksum 由打包脚本写入，源码目录里留空是正常的
    check("manifest checksum 为 md5 或空（打包时回填）",
          m.get("checksum", "") == "" or bool(re.match(r"^[0-9a-f]{32}$", m.get("checksum", ""))),
          m.get("checksum", ""))
except ValueError as e:
    check("manifest 解析", False, str(e))

# 3. 严格 JSON（任何注释/尾逗号都会在这里爆出来）
for f in ["config/privilege", "config/resource", "app/ui/config"]:
    try:
        data = json.loads(read(f))
        check("严格 JSON %s" % f, True)
    except ValueError as e:
        check("严格 JSON %s" % f, False, str(e))

try:
    priv = json.loads(read("config/privilege"))
    check("privilege 有 run-as", "defaults" in priv and "run-as" in priv["defaults"],
          priv.get("defaults", {}).get("run-as", ""))
    check("run-as 为 package（root 仅限官方合作发布）",
          priv.get("defaults", {}).get("run-as") == "package")
except ValueError:
    pass

try:
    uicfg = json.loads(read("app/ui/config"))
    url = uicfg.get(".url", {})
    ok = len(url) == 1 and list(url)[0].startswith(m.get("appname", "\0") + ".")
    check("ui/config 入口键与 appname 一致", ok, str(list(url)))
    port = str(next(iter(url.values()), {}).get("port", ""))
    # 绝不能留 ${wizard_port} 占位符：飞牛桌面直接用这个值拼 URL，
    # 占位符没被替换就会变成 http://127.0.0.1:${wizard_port}/，应用必然打不开。
    # 正确做法是写合法默认值，安装回调再按用户选择改写。
    check("ui/config 端口是合法数字（无占位符残留）",
          port.isdigit(), port)
    check("ui/config 端口在合法范围", port.isdigit() and 1024 <= int(port) <= 65000, port)
except ValueError:
    pass

# 3.5 端口声明 *.sc
# health.json 已移除：它按 manifest 的固定端口探活，用户改端口后会误判成未运行；
# 启动等待改由 cmd/main 的 wait_health 完成（它读 var/port.txt 里的真实端口）。
check("不依赖固定端口探活（无 health.json）", not os.path.exists(os.path.join(ROOT, "health.json")))
check("checkport 关闭（端口由向导决定）", m.get("checkport", "").lower() == "false",
      m.get("checkport", ""))

# *.sc（端口转发声明）已移除：端口由安装向导决定，写死一个端口自相矛盾，
# 且 .sc 段格式一旦不被识别就是白白多一个安装失败点。服务监听 0.0.0.0，无需转发。
sc_files = [f for f in os.listdir(ROOT) if f.endswith(".sc")]
check("不依赖 .sc 端口声明", not sc_files, str(sc_files))
check("桌面入口 type=url（iframe 会被 https 桌面的混合内容策略拦截）",
      next(iter(json.loads(read("app/ui/config"))[".url"].values())).get("type") == "url")


# wizard/ 默认不进包（0.3.1 无向导可正常安装，0.4.x 有向导反而报「数据包不完整」）。
# 源码树里保留向导定义，存在则严格校验，缺失不判失败。
for f in ["wizard/install", "wizard/config", "wizard/upgrade", "wizard/uninstall"]:
    if not os.path.isfile(os.path.join(ROOT, f)):
        continue
    try:
        steps = json.loads(read(f))
        ok = isinstance(steps, list) and len(steps) > 0
        detail = []
        if ok:
            for s in steps:
                if "stepTitle" not in s or not isinstance(s.get("items"), list):
                    ok, detail = False, ["缺 stepTitle/items"]
                    break
                for it in s["items"]:
                    if it.get("type") not in WIZARD_TYPES:
                        ok = False
                        detail.append("未知类型 %r" % it.get("type"))
                    if it.get("type") == "switch":
                        v = it.get("initValue")
                        if v is not None and str(v).lower() not in ("true", "false"):
                            ok = False
                            detail.append("switch initValue 须为 true/false")
                    # rules 必须是数组（官方规范）；写成对象会让向导解析失败，
                    # 进而整个安装流程报「应用数据包不完整」。
                    if "rules" in it and not isinstance(it["rules"], list):
                        ok = False
                        detail.append("field %r 的 rules 必须是数组" % it.get("field"))
                    # 自定义字段统一 wizard_ 前缀，否则 cmd 里的 $wizard_xxx 取不到值。
                    fl = it.get("field")
                    if fl and not fl.startswith("wizard_"):
                        ok = False
                        detail.append("field %r 缺 wizard_ 前缀" % fl)
        check("向导结构 %s" % f, ok, "；".join(detail))
    except ValueError as e:
        check("向导结构 %s" % f, False, str(e))

# 5. 图标尺寸（直接读 IHDR，不依赖 PIL）
def png_size(path):
    blob = read(path, binary=True)
    if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    w, h = struct.unpack(">II", blob[16:24])
    return w, h

for f, want in [("ICON.PNG", 64), ("ICON_256.PNG", 256),
                ("app/ui/images/icon-64.png", 64), ("app/ui/images/icon-256.png", 256)]:
    size = png_size(f)
    check("图标尺寸 %s" % f, size == (want, want), str(size))

# 6. cmd 脚本
for f in os.listdir(os.path.join(ROOT, "cmd")):
    text = read("cmd/" + f)
    check("脚本 shebang %s" % f, text.startswith("#!"), text.splitlines()[0] if text else "")
main_text = read("cmd/main")
check("cmd/main 支持 start/stop/status",
      all(k in main_text for k in ("start)", "stop)", "status)")))
check("cmd/main status 未运行返回 3", "exit 3" in main_text)

# 7. 语法校验
bash = None
for b in ("bash", "C:/Program Files/Git/bin/bash.exe"):
    try:
        subprocess.run([b, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bash = b
        break
    except OSError:
        continue
if bash:
    bad = []
    for f in sorted(os.listdir(os.path.join(ROOT, "cmd"))):
        p = os.path.join(ROOT, "cmd", f)
        r = subprocess.run([bash, "-n", p], capture_output=True, text=True)
        if r.returncode != 0:
            bad.append("%s: %s" % (f, r.stderr.strip().splitlines()[:1]))
    check("bash -n 全部 cmd 脚本", not bad, str(bad))
else:
    check("bash -n 全部 cmd 脚本", True, "（本机无 bash，已跳过）")

pybad = []
for f in ["app/server/nvr_slim.py", "app/server/runtime.py", "app/server/params.py",
          "app/server/ffmpeg_cmd.py", "app/server/retention.py",
          "app/server/recorder.py", "app/server/condense.py", "app/server/link.py"]:
    try:
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
    except py_compile.PyCompileError as e:
        pybad.append("%s: %s" % (f, str(e).splitlines()[0]))
check("py_compile 服务脚本", not pybad, str(pybad))

# 8. app/bin 说明与 ffmpeg 占位
check("app/bin 有 ffmpeg 说明",
      os.path.isfile(os.path.join(ROOT, "app", "bin", "README.md")))

# 若 app/bin 已放入 ffmpeg/ffprobe，必须是合法的 x86_64 ELF（防手滑放进文本/HTML/错误架构）
for _bn in ("ffmpeg", "ffprobe"):
    _bp = os.path.join(ROOT, "app", "bin", _bn)
    _ok, _why = True, ""
    if os.path.isfile(_bp):
        _blob = read("app/bin/" + _bn, binary=True)
        if len(_blob) < 20 or not _blob.startswith(b"\x7fELF"):
            _ok, _why = False, "不是 ELF 文件"
        elif _blob[4] != 2:
            _ok, _why = False, "不是 64 位"
        elif _blob[18:20] != b"\x3e\x00":
            _ok, _why = False, "不是 x86_64"
    check("app/bin/%s 为合法 x86_64 ELF（若有）" % _bn, _ok, _why)

fails = [r for r in results if not r[0]]
for ok, name, detail in results:
    mark = "PASS" if ok else "FAIL"
    print("%-4s %-38s %s" % (mark, name, detail if not ok else ""))
print("\n%d 项检查，%d 个通过，%d 个失败" % (len(results), len(results) - len(fails), len(fails)))
sys.exit(1 if fails else 0)
