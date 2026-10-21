#!/usr/bin/env python3
"""无 fnpack 环境下的 fpk 打包器（纯标准库）。

目标是**逐项复刻 `fnpack build` 的产物形态**，而不是"能解开就行"。
官方 build-fpk.sh 最后一步是 `tar -czf "$FPK_NAME" *`，由此可推出三条硬约束：

  1. 成员名**不带 `./` 前缀**（`manifest` 而非 `./manifest`）；
  2. **包含目录条目**（`cmd/`、`config/`、`ui/images/` 本身也是成员）；
  3. 属主统一 root(0:0)，脚本 0755、其余 0644。

第 1 条曾是我们的实际错误：旧版把所有成员写成 `./xxx`。fnOS 安装器按
`^manifest$` / `^app[.]tgz$` 这类严格路径匹配取文件，`./` 前缀会让它一个都找不到，
进而报「应用数据包不完整」——这与"包能解开"并不矛盾。

包根布局（与官方一致）：
    manifest                     元数据，checksum = app.tgz 的 md5
    app.tgz                      app/ 的内容 → 安装后即 TRIM_APPDEST(target)
    cmd/                         生命周期脚本
    config/                      privilege + resource
    wizard/                      安装/配置向导（缺了它安装时一个选项都问不到）
    ui/                          桌面入口：只放 config + images/
    ICON.PNG / ICON_256.PNG

用法：python tools/pack_fpk.py [--out dist]
"""
import argparse
import gzip
import hashlib
import os
import shutil
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_DIRS = {"__pycache__", ".DS_Store", "build_stage"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".DS_Store", "Thumbs.db")
# wizard/ 是否进包。默认不进：0.3.1（无 wizard）可以安装，0.4.x（有 wizard）在
# 真机上报「应用数据包不完整」。对照实验时置 NVR_PKG_WIZARD=1 打开。
WITH_WIZARD = os.environ.get("NVR_PKG_WIZARD") == "1"

# 固定时间戳：同样的输入永远得到同样的包，checksum 才能跨机器复现
FIXED_MTIME = 1700000000
# 包根 ui/ 只放桌面入口需要的东西。index.html 属于服务端静态资源，
# 它在 app.tgz 里（→ target/ui/index.html），不该在包根重复一份 110KB。
UI_ROOT_KEEP = ("config", "images")


def read_manifest():
    m = {}
    with open(os.path.join(ROOT, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                m[k.strip()] = v.strip()
    return m


def excluded(path):
    parts = path.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    return any(path.endswith(s) for s in EXCLUDE_SUFFIX)


def add_dir(tar, name):
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE
    ti.mode = 0o755
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mtime = FIXED_MTIME
    tar.addfile(ti)


def add_file(tar, full, arcname, mode):
    ti = tar.gettarinfo(full, arcname)
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mtime = FIXED_MTIME
    ti.mode = mode
    with open(full, "rb") as f:
        tar.addfile(ti, f)


def add_tree(tar, src_dir, prefix, mode_dir=0o755, mode_file=0o644):
    """添加目录自身 + 其下全部文件，成员名不带 './'。"""
    add_dir(tar, prefix)
    for base, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        rel_base = os.path.relpath(base, src_dir).replace("\\", "/")
        for d in dirs:
            rel = d if rel_base == "." else "%s/%s" % (rel_base, d)
            add_dir(tar, "%s/%s" % (prefix, rel))
        for fn in sorted(files):
            rel = fn if rel_base == "." else "%s/%s" % (rel_base, fn)
            if excluded(rel):          # 必须用相对路径：绝对路径里带暂存目录名会误伤全部文件
                continue
            full = os.path.join(base, fn)
            # cmd/ 与 *.sh 给可执行位；app/bin 里的静态 ffmpeg/ffprobe 也必须可执行
            # （Windows 的 NTFS 存不住 chmod，必须显式写）
            executable = (prefix == "cmd" or fn.endswith(".sh")
                          or fn in ("ffmpeg", "ffprobe"))
            mode = mode_dir if executable else mode_file
            add_file(tar, full, "%s/%s" % (prefix, rel), mode)


def open_gz(out_path):
    """mtime=0 + 去掉原始文件名，gzip 头才稳定。"""
    raw = open(out_path, "wb")
    gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9)
    return raw, gz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args()
    m = read_manifest()
    appname, version, platform = m["appname"], m["version"], m.get("platform", "all")

    os.makedirs(a.out, exist_ok=True)
    out = os.path.join(a.out, "%s-%s_%s.fpk" % (appname, version, platform))

    stage = os.path.join(ROOT, "build_stage")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)

    # ---------- 1. app.tgz：app/ 的内容（安装后 = TRIM_APPDEST） ----------
    app_tgz = os.path.join(stage, "app.tgz")
    raw, gz = open_gz(app_tgz)
    try:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            add_dir(tar, ".")
            for sub in sorted(os.listdir(os.path.join(ROOT, "app"))):
                full = os.path.join(ROOT, "app", sub)
                if excluded(sub):
                    continue
                if os.path.isdir(full):
                    add_tree(tar, full, sub)
                else:
                    add_file(tar, full, sub, 0o644)
    finally:
        gz.close()
        raw.close()

    # ---------- 2. checksum = md5(app.tgz)，按官方对齐格式回填 ----------
    md5 = hashlib.md5()
    with open(app_tgz, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
    checksum = md5.hexdigest()

    # 官方 manifest 是 `key        = value`（等号前补空格到第 16 列），
    # 解析方用 awk -F'=' + tr -d ' ' 取值。改成这个形态最稳。
    lines = []
    with open(os.path.join(ROOT, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            s = line.rstrip("\n")
            if "=" not in s:
                lines.append(s)
                continue
            k, _, v = s.partition("=")
            k = k.strip()
            if k == "checksum":
                v = checksum
            lines.append("%-14s = %s" % (k, v.strip()))
    with open(os.path.join(stage, "manifest"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")

    # ---------- 3. 组装包根 ----------
    shutil.copytree(os.path.join(ROOT, "cmd"), os.path.join(stage, "cmd"))
    # wizard/ 默认不进包：0.3.1（不带 wizard）能装，0.4.x（带 wizard）报「应用数据包不完整」。
    # 需要时可以 NVR_PKG_WIZARD=1 打开做对照实验。
    if WITH_WIZARD:
        shutil.copytree(os.path.join(ROOT, "wizard"), os.path.join(stage, "wizard"))
    shutil.copytree(os.path.join(ROOT, "config"), os.path.join(stage, "config"))
    ui_stage = os.path.join(stage, "ui")
    os.makedirs(ui_stage)
    for name in UI_ROOT_KEEP:
        src = os.path.join(ROOT, "app", "ui", name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(ui_stage, name),
                            ignore=shutil.ignore_patterns(*EXCLUDE_DIRS))
        elif os.path.isfile(src):
            shutil.copy2(src, os.path.join(ui_stage, name))
    for name in ("ICON.PNG", "ICON_256.PNG"):
        shutil.copy2(os.path.join(ROOT, name), os.path.join(stage, name))
    # *.sc 端口转发声明已移除：端口是安装时用户自选的，写死一个端口自相矛盾，
    # 且 .sc 的段格式一旦不被识别就是白白多一个失败点。服务监听 0.0.0.0，无需转发。

    # ---------- 4. 出包 ----------
    order = ["ICON.PNG", "ICON_256.PNG", "app.tgz", "cmd", "config",
             "manifest", "ui"]
    if WITH_WIZARD:
        order.append("wizard")
    raw, gz = open_gz(out)
    try:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for sub in order:
                full = os.path.join(stage, sub)
                if not os.path.exists(full):
                    sys.exit("组装失败，缺 %s" % sub)
                if os.path.isdir(full):
                    add_tree(tar, full, sub)
                else:
                    add_file(tar, full, sub, 0o644)
    finally:
        gz.close()
        raw.close()
    shutil.rmtree(stage, ignore_errors=True)

    # ---------- 5. 自检 ----------
    problems = []
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        nameset = set(names)
        need = ["manifest", "app.tgz", "cmd/main", "config/privilege", "config/resource",
                "ui/config", "ICON.PNG", "ICON_256.PNG"]
        if WITH_WIZARD:
            need.append("wizard/install")
        for n in need:
            if n not in nameset:
                problems.append("缺成员 %s" % n)
        for n in names:
            if n.startswith("./"):
                problems.append("成员名带 ./ 前缀：%s" % n)
                break
        # 目录条目必须存在（官方 tar -czf * 会带上）
        dir_need = ["cmd", "config", "ui"] + (["wizard"] if WITH_WIZARD else [])
        for d in dir_need:
            if d not in nameset:
                problems.append("缺目录条目 %s/" % d)
        if "ui/index.html" in nameset:
            problems.append("包根 ui/ 不应含 index.html（它属于 app.tgz）")
        data = tar.extractfile("app.tgz").read()
        got = hashlib.md5(data).hexdigest()
        if got != checksum:
            problems.append("checksum 不一致 %s != %s" % (got, checksum))
        # app.tgz 自己能解开，且含服务端入口
        inner = tarfile.open(fileobj=tar.extractfile("app.tgz"))
        inames = set(inner.getnames())
        for n in ("server/nvr_slim.py", "ui/index.html", "ui/config"):
            if n not in inames:
                problems.append("app.tgz 缺 %s" % n)
        # cmd/* 必须是可执行
        for mem in tar.getmembers():
            if mem.name.startswith("cmd/") and mem.isfile() and not (mem.mode & 0o111):
                problems.append("%s 没有可执行位" % mem.name)
                break
    if problems:
        sys.exit("自检失败：\n  " + "\n  ".join(problems))

    print("打包完成：%s" % out)
    print("  app.tgz md5 = %s（已写入 manifest.checksum）" % checksum)
    print("  成员 %d 个，大小 %.1f KB" % (len(names), os.path.getsize(out) / 1024.0))


if __name__ == "__main__":
    main()
