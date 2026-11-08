#!/usr/bin/env python3
"""把 app/ui/css/*.css 合成进 app/ui/index.html 的 <style> 区（纯标准库）。

为什么要有这一步：0.9.123 之前 1400 行样式和 8600 行脚本挤在同一个 index.html 里，
改一条进度条的命中区得一屏一屏翻。拆成源文件之后，**部署形态不变**——设备上仍然
只有一个 index.html，样式是构建时拼进去的，不引入 <link> 外链（外链会让首屏先白
一下，且多一个 404 的可能）。

合成顺序是固定的：theme → base → components → pages → player。
顺序有意义：theme 出令牌，base/components 用令牌，pages 与 player 最后覆盖。
**不要按文件名排序**（base 会跑到 components 前面去，看着对、实则把主题变量甩到末位）。

用法：
    python tools/compose_ui.py           合成（幂等：内容没变就不动文件、不改 mtime）
    python tools/compose_ui.py --check   只校验是否同步（构建闸门用，不同步则退出码 1）
"""
import argparse
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "app", "ui")
INDEX = os.path.join(UI, "index.html")
CSS_DIR = os.path.join(UI, "css")

# 顺序即层叠顺序，见文件头
ORDER = ["theme.css", "base.css", "components.css", "pages.css", "player.css"]

MARK_BEGIN = "/* @@css:begin"
MARK_END = "/* @@css:end */"
BANNER = (MARK_BEGIN + " —— 本段由 build.sh 合成，请改 app/ui/css/*.css；"
          "直接改这里会被下一次合成覆盖 */")


def read(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        # 统一成 LF 再比：git 在 Windows 上会按 autocrlf 把检出换成 CRLF，
        # 不归一化的话"内容一样但行尾不同"每轮都会被判成有变化，白写一次文件。
        return f.read().replace("\r\n", "\n")


def compose_css():
    """按固定顺序拼接五个源文件，文件之间留一个空行。"""
    got = sorted(n for n in os.listdir(CSS_DIR) if n.endswith(".css"))
    missing = [n for n in ORDER if n not in got]
    extra = [n for n in got if n not in ORDER]
    if missing:
        sys.exit("缺少样式源文件：%s" % missing)
    if extra:
        # 多出来的文件不会被合成进去 —— 这种"改了却没生效"的情况必须当场报出来
        sys.exit("存在未纳入合成顺序的样式文件：%s（要新增请同时改 compose_ui.py 的 ORDER）"
                 % extra)
    chunks = []
    for name in ORDER:
        body = read(os.path.join(CSS_DIR, name)).replace("\r\n", "\n").strip("\n")
        if not body:
            sys.exit("样式源文件是空的：%s" % name)
        # 样式区是"原始文本"，只由 "</style" 终止 —— 注释里写"黑边 <44px"没事，
        # 但真出现结束标签或 <script 就会把样式区提前截断，整页回到无样式状态。
        low = body.lower()
        for bad in ("</style", "<script"):
            if bad in low:
                sys.exit("%s 里出现 %r（会把样式区提前截断）" % (name, bad))
        if MARK_BEGIN in body or MARK_END in body:
            sys.exit("%s 里出现合成标记，会把下一次合成搞坏" % name)
        chunks.append(body)
    return "\n\n".join(chunks)


def splice(html, css):
    i = html.find(MARK_BEGIN)
    j = html.find(MARK_END)
    if i < 0 or j < 0:
        sys.exit("index.html 里找不到合成标记（%s / %s）" % (MARK_BEGIN, MARK_END))
    if j < i:
        sys.exit("index.html 的合成标记顺序反了")
    # 标记正文换掉，标记本身与缩进保持原样
    line_start = html.rfind("\n", 0, i) + 1
    return html[:line_start] + BANNER + "\n" + css + "\n" + html[j:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验不同步则退出 1")
    a = ap.parse_args()

    old = read(INDEX)
    new = splice(old, compose_css())
    same = new == old
    if a.check:
        if same:
            print("index.html 与 app/ui/css/ 同步")
            return 0
        print("index.html 与 app/ui/css/ 不同步：跑 bash build.sh（或 python tools/compose_ui.py）后重试",
              file=sys.stderr)
        return 1
    if same:
        print("样式合成：无变化（index.html 未改动）")
        return 0
    with io.open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    print("样式合成：已更新 index.html（%s）" % " → ".join(ORDER))
    return 0


if __name__ == "__main__":
    sys.exit(main())
