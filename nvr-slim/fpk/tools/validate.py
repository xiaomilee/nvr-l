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
          "app/server/recorder.py", "app/server/condense.py", "app/server/link.py",
          # 事件中心（0.9.121）：nvr_slim 直接 import 这两个模块，
          # 语法错就是启动即崩，必须在打包前编译一遍。
          "app/server/event.py", "app/server/event_index.py",
          # 时间窗确认（0.9.122）：detect 在模块级 import 它，同一条理由。
          "app/server/confirm.py",
          # 前端合成器（0.9.123）：打包前的第一步就是它，语法错等于整个构建闸门失效。
          "tools/compose_ui.py"]:
    try:
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
    except py_compile.PyCompileError as e:
        pybad.append("%s: %s" % (f, str(e).splitlines()[0]))
check("py_compile 服务脚本", not pybad, str(pybad))

# 7.5 前端样式源（0.9.123 起 index.html 的样式区由 app/ui/css/*.css 合成）
# 缺一个源文件，页面不会报错，只会少一页的版式 —— 这种"看起来正常"的缺失
# 只能靠构建闸门拦。顺序表与 compose_ui.py 的 ORDER 必须一致，否则改了不生效。
CSS_DIR = os.path.join(ROOT, "app", "ui", "css")
CSS_ORDER = ["theme.css", "base.css", "components.css", "pages.css", "player.css"]
missing_css = [n for n in CSS_ORDER if not os.path.isfile(os.path.join(CSS_DIR, n))]
check("样式源文件齐全（theme/base/components/pages/player）", not missing_css,
      "缺: %s" % missing_css)
extra_css = ([n for n in sorted(os.listdir(CSS_DIR))
              if n.endswith(".css") and n not in CSS_ORDER]
             if os.path.isdir(CSS_DIR) else [])
check("css/ 下没有未纳入合成顺序的文件", not extra_css, "多出: %s" % extra_css)
ui_html = read("app/ui/index.html")
check("index.html 有且只有一个样式合成标记",
      ui_html.count("/* @@css:begin") == 1 and ui_html.count("/* @@css:end */") == 1,
      "begin=%d end=%d" % (ui_html.count("/* @@css:begin"),
                           ui_html.count("/* @@css:end */")))
if not missing_css:
    _sync = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "compose_ui.py"),
                            "--check"], capture_output=True, text=True)
    check("index.html 与 css/、js/ 已同步", _sync.returncode == 0,
          (_sync.stderr or _sync.stdout).strip())

# 7.6 前端脚本源（0.9.123 起 index.html 的脚本区按标记合成自 app/ui/js/**/*.js）
# 脚本没法像样式那样"按文件名定顺序"：const/let 不提升，标记在 index.html 里的
# 位置就是执行次序。所以这里只查"源文件在、标记成对、没有外链"，顺序由
# test/ui/js_split.test.js 钉成一张表 —— 那里才是"挪一个标记就红"的地方。
JS_DIR = os.path.join(ROOT, "app", "ui", "js")
JS_REQUIRED = ["core/auth.js", "core/format.js", "core/api.js", "core/theme.js"]
missing_js = [n for n in JS_REQUIRED
              if not os.path.isfile(os.path.join(JS_DIR, n.replace("/", os.sep)))]
check("脚本源文件齐全（core/auth·format·api·theme）", not missing_js, "缺: %s" % missing_js)
_js_begin = len(re.findall(r"/\* @@js:begin[ ]", ui_html))
check("index.html 的 js 合成标记成对",
      _js_begin >= 1 and _js_begin == ui_html.count("/* @@js:end */"),
      "begin=%d end=%d" % (_js_begin, ui_html.count("/* @@js:end */")))
# 外链是最容易"看着更好"的改法：多一个文件、多一次 404、首屏多白一下，而
# test/ui/*.test.js 全都取"最后一个 <script>"当脚本源，一旦多出标签就静默跑错段。
check("页面脚本没有外链（无 <script src>）",
      "<script src" not in ui_html.lower(), "出现了 <script src>")
# 同一个理由的另一半：所有合成标记必须都落在最后一个 <script> 里
_js_last = ui_html.rfind("<script")
_js_close = ui_html.find("</script>", _js_last)
outside = [m.start() for m in re.finditer(r"/\* @@js:begin[ ]", ui_html)
           if not (_js_last < m.start() < _js_close)]
check("全部 js 标记都在最后一个 <script> 里", not outside, "越界 %d 处" % len(outside))

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
