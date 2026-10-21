#!/bin/bash
# 在飞牛真机上用官方 fnpack 出包 —— 这是"标准答案"。
# tools/pack_fpk.py 只是复刻 fnpack 的产物形态；一旦两者有偏差，以 fnpack 为准。
# 怀疑包结构有问题时，优先用这个脚本重出一份对比。
#
# 用法（root 或 sudo）：
#   sudo bash tools/build_on_fnos.sh
# 产物：dist/<appname>-<version>_x86.fpk
set -e

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FNPACK_URL="https://static2.fnnas.com/fnpack/fnpack-1.0.4-linux-amd64"
APPNAME="$(grep -m1 '^appname' "$SRC/manifest" | cut -d= -f2 | tr -d ' \r')"

echo "== 1/4 准备 fnpack =="
if command -v fnpack >/dev/null 2>&1; then
  FNPACK="$(command -v fnpack)"
else
  echo "   未安装，从官方静态资源下载"
  if command -v curl >/dev/null 2>&1; then
    curl -#kL "$FNPACK_URL" -o "$WORK/fnpack"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$FNPACK_URL" -O "$WORK/fnpack"
  else
    echo "   既没有 curl 也没有 wget，请先装一个" >&2
    exit 1
  fi
  chmod +x "$WORK/fnpack"
  FNPACK="$WORK/fnpack"
fi

echo "== 2/4 组装 fnpack 输入目录 =="
# fnpack 的输入是开发目录：app/ 会被它自己打成 app.tgz，
# 所以这里拷的是源码树里的 app/，不能再预先打成 tar。
PKG="$WORK/pkg"
mkdir -p "$PKG"
cp "$SRC/manifest" "$PKG/"
cp "$SRC"/ICON.PNG "$PKG/" 2>/dev/null || true
cp "$SRC"/ICON_256.PNG "$PKG/" 2>/dev/null || true
for d in app cmd config wizard; do
  if [ -d "$SRC/$d" ]; then cp -a "$SRC/$d" "$PKG/"; fi
done
find "$PKG" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -name "*.pyc" -delete 2>/dev/null || true
# checksum 由 fnpack 注入；源 manifest 里本来就是空的，这里再兜一次底
sed -i 's/^checksum.*/checksum =/' "$PKG/manifest" 2>/dev/null || true

echo "   输入目录结构："
ls -1 "$PKG" | sed 's/^/     /'

echo "== 3/4 fnpack build =="
"$FNPACK" build --directory "$PKG" 2>&1 | tail -20
FPK="$(find "$PKG" "$WORK" "$SRC" -maxdepth 2 -name '*.fpk' -newermt '-5 minutes' 2>/dev/null | head -1)"
if [ -z "$FPK" ]; then
  FPK="$(ls -t "$SRC"/*.fpk 2>/dev/null | head -1)"
fi
if [ -z "$FPK" ] || [ ! -f "$FPK" ]; then
  echo "fnpack 没产出 .fpk，看上面的报错" >&2
  exit 1
fi

echo "== 4/4 校验并交付 =="
mkdir -p "$SRC/dist"
NAME="$(basename "$FPK")"
cp -f "$FPK" "$SRC/dist/$NAME"
echo "   产物：$SRC/dist/$NAME"

PY="$(command -v python3 || true)"
if [ -n "$PY" ]; then
  "$PY" - "$SRC/dist/$NAME" <<'PYEOF'
import hashlib, sys, tarfile
path = sys.argv[1]
tar = tarfile.open(path)
names = tar.getnames()
man = tar.extractfile("manifest").read().decode("utf-8", "replace")
cs = [l for l in man.splitlines() if l.strip().startswith("checksum")]
print("   成员 %d 个；顶层：%s" % (len(names), sorted({n.split("/")[0] for n in names})))
print("   带 ./ 前缀：%s" % any(n.startswith("./") for n in names))
if cs:
    want = cs[0].split("=", 1)[1].strip()
    got = hashlib.md5(tar.extractfile("app.tgz").read()).hexdigest()
    ok = "一致" if want == got else "不一致（安装器会判数据包不完整）"
    print("   checksum：声明 %s / 实际 %s -> %s" % (want or "<空>", got, ok))
else:
    print("   !! manifest 里没有 checksum 行")
PYEOF
fi

echo
echo "装它：应用中心 → 右上角「手动安装」→ 选 $SRC/dist/$NAME"
echo "装完打不开：sudo bash /var/apps/$APPNAME/target/tools/doctor.sh"
