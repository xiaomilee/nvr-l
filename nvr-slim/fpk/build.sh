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

echo "== 1/5 结构校验 =="
"$PY" "$ROOT/tools/validate.py" || exit 1

echo "== 2/5 后端单测（遍历 test/test_*.py）=="
# 逐个列文件名的写法漏过 test_condense.py / test_dirname.py / test_playback.py，
# 它们其实一直在跑却从没进过构建闸门。改成遍历，新加的测试自动纳入。
# test_http_smoke.py 需要真起服务、且依赖 18081 空闲，单独手动跑（见 BUILD.md）。
FAILED=""
for f in "$ROOT"/test/test_*.py; do
  case "$(basename "$f")" in
    test_http_smoke.py) continue ;;
  esac
  printf '  - %-24s ' "$(basename "$f")"
  if out="$("$PY" "$f" 2>&1)"; then
    echo "OK  $(echo "$out" | grep -oE 'Ran [0-9]+ test' | head -1)"
  else
    echo "FAIL"
    echo "$out" | tail -20
    FAILED="$FAILED $(basename "$f")"
  fi
done
if [ -n "$FAILED" ]; then
  echo "后端单测失败：$FAILED" >&2
  exit 1
fi

echo "== 3/5 设置页交互断言 =="
if [ -n "$NODE" ]; then
  "$NODE" "$ROOT/test/ui/settings.test.js" || exit 1
  # 时间轴分色渲染：DOM 桩跑 renderVtl，验证类别色/分段/变淡（浏览器 e2e 见 test/e2e_ui.js）
  echo "== 4/5 时间轴分色渲染 =="
  "$NODE" "$ROOT/test/ui/render_check.js" || exit 1
  # 回放默认落点：今天=最后一段段首 / 历史=当天第一段 / 进页自动定位（0.9.72）
  echo "== 回放默认落点 =="
  "$NODE" "$ROOT/test/ui/play_enter.test.js" || exit 1
else
  echo "SKIP 未找到 node，跳过设置页/时间轴测试（只影响测试，不影响出包）"
fi

# fnOS 上脚本需要可执行位；NTFS 下 chmod 可能无效，失败不阻断
chmod +x "$ROOT/cmd/"* "$ROOT/test/dryrun.sh" "$ROOT/build.sh" 2>/dev/null || true

if [ "${1:-}" = "--check" ]; then
  echo "校验通过（未打包）"
  exit 0
fi

echo "== 5/5 打包 =="
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
