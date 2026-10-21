#!/bin/bash
# 打包前校验 + 调用官方 fnpack
# 用法： bash build.sh            完整打包
#       bash build.sh --check     只校验不打包
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="${NVR_PYTHON:-}"
[ -n "$PY" ] || PY="$(command -v python3 || command -v python)"
NODE="${NVR_NODE:-}"
[ -n "$NODE" ] || NODE="$(command -v node || true)"

echo "== 1/7 结构校验 =="
"$PY" "$ROOT/tools/validate.py" || exit 1

echo "== 2/7 参数与命令断言 =="
"$PY" "$ROOT/test/test_params.py" || exit 1

echo "== 3/7 保留策略断言 =="
"$PY" "$ROOT/test/test_retention.py" || exit 1

echo "== 4/7 录制与衔接断言 =="
"$PY" "$ROOT/test/test_link.py" || exit 1

echo "== 5/7 端口断言 =="
"$PY" "$ROOT/test/test_port.py" || exit 1
echo "== 3b/7 快照 worker =="
"$PY" "$ROOT/test/test_snapshots.py" || exit 1
echo "== 3c/7 诊断日志 =="
"$PY" "$ROOT/test/test_log.py" || exit 1
echo "== 3d/7 ffmpeg 查找逻辑 =="
"$PY" "$ROOT/test/test_find_ffmpeg.py" || exit 1

echo "== 6/7 设置页交互断言 =="
if [ -n "$NODE" ]; then
  "$NODE" "$ROOT/test/ui/settings.test.js" || exit 1
else
  echo "SKIP 未找到 node，跳过设置页测试（只影响测试，不影响出包）"
fi

# fnOS 上脚本需要可执行位；NTFS 下 chmod 可能无效，失败不阻断
chmod +x "$ROOT/cmd/"* "$ROOT/test/dryrun.sh" "$ROOT/build.sh" 2>/dev/null || true

if [ "${1:-}" = "--check" ]; then
  echo "校验通过（未打包）"
  exit 0
fi

echo "== 7/7 打包 =="
APPNAME="$(grep -m1 '^appname=' "$ROOT/manifest" | cut -d= -f2)"
VERSION="$(grep -m1 '^version=' "$ROOT/manifest" | cut -d= -f2)"

if ! command -v fnpack >/dev/null 2>&1; then
  cat <<'EOF'

本机未安装 fnpack，改用内置打包器 tools/pack_fpk.py（fpk = tar.gz，包根为
manifest + app.tgz(checksum=md5) + cmd/ + config/ + ui/ + 图标）。
若真机安装报结构错误，请改在飞牛机器上 SSH 执行官方 `fnpack build` 出包。

EOF
  "$PY" "$ROOT/tools/pack_fpk.py" || exit 1
  exit 0
fi

cd "$ROOT" && fnpack build && echo "完成：$APPNAME-$VERSION"
