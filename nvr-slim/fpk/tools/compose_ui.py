#!/usr/bin/env python3
"""把 app/ui/css/*.css 与 app/ui/js/**/*.js 合成进 app/ui/index.html（纯标准库）。

为什么要有这一步：0.9.123 之前 1400 行样式和 8600 行脚本挤在同一个 index.html 里，
改一条进度条的命中区得一屏一屏翻。拆成源文件之后，**部署形态不变**——设备上仍然
只有一个 index.html，样式与脚本都是构建时拼进去的，不引入 <link> / <script src>
外链。除了"首屏先白一下"和"离线内网多一个 404"，还有一个更要紧的理由：
`test/ui/*.js` 那十几个测试全都取**最后一个 `<script>`** 当脚本源，
一旦有人拆成多个标签，它们会一声不响地跑错段 —— 全绿，但什么都没测到。

样式：合成顺序固定 theme → base → components → pages → player（令牌在前、
页面覆盖在后），**不要按文件名排序**（base 会跑到 components 前面去，看着对、
实则把主题变量甩到末位）。标记是 `/* @@css:begin … */`，全页只有一处。

脚本：一个源文件一段，标记是 `/* @@js:begin core/api.js */` … `/* @@js:end */`。
脚本没法像样式那样"按名字定顺序"：const/let 不提升，声明出现的次序就是执行次序。
所以**合成顺序由标记在 index.html 里的位置决定**——代码从哪儿搬走就还回哪儿去，
拼接结果与拆分前等价（除文件头注释）。要调顺序就得挪 index.html 里的标记，
这件事必须被看见，所以 `test/ui/js_split.test.js` 把顺序钉成一张固定表。

用法：
    python tools/compose_ui.py           合成（幂等：内容没变就不动文件、不改 mtime）
    python tools/compose_ui.py --check   只校验是否同步（构建闸门用，不同步则退出码 1）
"""
import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "app", "ui")
INDEX = os.path.join(UI, "index.html")
CSS_DIR = os.path.join(UI, "css")
JS_DIR = os.path.join(UI, "js")

# 顺序即层叠顺序，见文件头
CSS_ORDER = ["theme.css", "base.css", "components.css", "pages.css", "player.css"]

CSS_MARK_BEGIN = "/* @@css:begin"
CSS_MARK_END = "/* @@css:end */"
CSS_BANNER = (CSS_MARK_BEGIN + " —— 本段由 build.sh 合成，请改 app/ui/css/*.css；"
              "直接改这里会被下一次合成覆盖 */")

JS_MARK_END = "/* @@js:end */"
# 名字里不带空白和 *，后面紧跟的是"——"那串中文说明，所以只取到第一个空白为止
JS_MARK_BEGIN_RE = re.compile(r"/\* @@js:begin[ ]+([^\s*]+)")
JS_BANNER = ("/* @@js:begin %s —— 本段由 build.sh 合成，请改 app/ui/js/%s；"
             "直接改这里会被下一次合成覆盖 */")


def read(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        # 统一成 LF 再比：git 在 Windows 上会按 autocrlf 把检出换成 CRLF，
        # 不归一化的话"内容一样但行尾不同"每轮都会被判成有变化，白写一次文件。
        return f.read().replace("\r\n", "\n")


def warn_bad_ending(name, body, closer):
    """源文件里绝不能出现会提前截断宿主标签的东西，返回错误描述或空串。"""
    low = body.lower()
    for bad in (closer, "<script", "<style"):
        if bad in low:
            return "%s 里出现 %r（会把宿主区提前截断）" % (name, bad)
    return ""


# ---------------------------------------------------------------- 样式

def compose_css():
    """按固定顺序拼接五个源文件，文件之间留一个空行。"""
    got = sorted(n for n in os.listdir(CSS_DIR) if n.endswith(".css"))
    missing = [n for n in CSS_ORDER if n not in got]
    extra = [n for n in got if n not in CSS_ORDER]
    if missing:
        sys.exit("缺少样式源文件：%s" % missing)
    if extra:
        # 多出来的文件不会被合成进去 —— 这种"改了却没生效"的情况必须当场报出来
        sys.exit("存在未纳入合成顺序的样式文件：%s（要新增请同时改 compose_ui.py 的 ORDER）"
                 % extra)
    chunks = []
    for name in CSS_ORDER:
        body = read(os.path.join(CSS_DIR, name)).strip("\n")
        if not body:
            sys.exit("样式源文件是空的：%s" % name)
        bad = warn_bad_ending(name, body, "</style")
        if bad:
            sys.exit(bad)
        if CSS_MARK_BEGIN in body or CSS_MARK_END in body:
            sys.exit("%s 里出现合成标记，会把下一次合成搞坏" % name)
        chunks.append(body)
    return "\n\n".join(chunks)


def splice_css(html, css):
    i = html.find(CSS_MARK_BEGIN)
    j = html.find(CSS_MARK_END)
    if i < 0 or j < 0:
        sys.exit("index.html 里找不到合成标记（%s / %s）" % (CSS_MARK_BEGIN, CSS_MARK_END))
    if j < i:
        sys.exit("index.html 的合成标记顺序反了")
    # 标记正文换掉，标记本身与缩进保持原样
    line_start = html.rfind("\n", 0, i) + 1
    return html[:line_start] + CSS_BANNER + "\n" + css + "\n" + html[j:]


# ---------------------------------------------------------------- 脚本

def list_js():
    """js/ 下全部源文件的相对路径（正斜杠），只认 .js。"""
    out = []
    for base, _dirs, files in os.walk(JS_DIR):
        for fn in files:
            if fn.endswith(".js"):
                rel = os.path.relpath(os.path.join(base, fn), JS_DIR)
                out.append(rel.replace("\\", "/"))
    return sorted(out)


def js_body(name):
    body = read(os.path.join(JS_DIR, name.replace("/", os.sep))).strip("\n")
    if not body:
        sys.exit("脚本源文件是空的：%s" % name)
    bad = warn_bad_ending(name, body, "</script")
    if bad:
        sys.exit(bad)
    if "@@js:" in body:
        sys.exit("%s 里出现合成标记，会把下一次合成搞坏" % name)
    return body


def find_js_regions(html):
    """按出现顺序列出 (begin 标记, 结束标记位置, 名字)，并检查是否嵌套/缺尾。"""
    regions = []
    pos = 0
    while True:
        m = JS_MARK_BEGIN_RE.search(html, pos)
        if not m:
            break
        name = m.group(1)
        end = html.find(JS_MARK_END, m.end())
        if end < 0:
            sys.exit("@@js:begin %s 没有配对的 %s" % (name, JS_MARK_END))
        nxt = JS_MARK_BEGIN_RE.search(html, m.end())
        if nxt and nxt.start() < end:
            sys.exit("@@js:begin %s 与下一段交叠：少了结束标记，或顺序反了" % name)
        regions.append((m, end, name))
        pos = end + len(JS_MARK_END)
    return regions


def compose_js(html):
    """按标记在 index.html 里的位置依次换回源文件内容（位置即执行顺序）。"""
    regions = find_js_regions(html)
    names = [n for _m, _e, n in regions]
    files = list_js()

    dup = sorted(n for n in set(names) if names.count(n) > 1)
    if dup:
        # 同一份源码出现两次 = 整段逻辑执行两遍（事件监听重复、定时器翻倍）
        sys.exit("同一个脚本源被合成了两次：%s" % dup)
    missing = [n for n in names if n not in files]
    if missing:
        sys.exit("标记引用了不存在的源文件：%s（找不到 app/ui/js/%s）"
                 % (missing, " 或 app/ui/js/".join(missing)))
    orphan = [n for n in files if n not in names]
    if orphan:
        # 这种"写了却不生效"的坑，改的人只会以为自己改错了地方
        sys.exit("app/ui/js 下有文件没有合成标记，改了不会生效：%s" % orphan)

    out = []
    prev = 0
    for m, end, name in regions:
        line_start = html.rfind("\n", 0, m.start()) + 1
        end_line = html.rfind("\n", 0, end) + 1
        out.append(html[prev:line_start])
        out.append(JS_BANNER % (name, name))
        out.append("\n" + js_body(name) + "\n")
        prev = end_line
    out.append(html[prev:])
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验不同步则退出 1")
    a = ap.parse_args()

    old = read(INDEX)
    new = compose_js(splice_css(old, compose_css()))
    same = new == old
    if a.check:
        if same:
            print("index.html 与 app/ui/css/、app/ui/js/ 同步")
            return 0
        print("index.html 与源文件不同步：跑 bash build.sh（或 python tools/compose_ui.py）后重试",
              file=sys.stderr)
        return 1
    if same:
        print("前端合成：无变化（index.html 未改动）")
        return 0
    with io.open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    print("前端合成：已更新 index.html（样式 %s；脚本 %s）"
          % (" → ".join(CSS_ORDER),
             " → ".join(n for _m, _e, n in find_js_regions(old)) or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
