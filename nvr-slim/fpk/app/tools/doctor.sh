#!/bin/bash
# NVR-L 现场诊断：装不上 / 打不开时先跑这个，输出直接贴给开发者。
#   bash /var/apps/nvr/target/tools/doctor.sh
# 只读操作，不修改任何文件。

APPNAME="${TRIM_APPNAME:-nvr}"
ROOT="/var/apps/$APPNAME"
TARGET="$(readlink -f "$ROOT/target" 2>/dev/null)"
VAR="$(readlink -f "$ROOT/var" 2>/dev/null)"

say() { printf '\n== %s ==\n' "$1"; }

say "系统"
echo "appname   : $APPNAME"
echo "os        : $(cat /etc/trinity/version 2>/dev/null || cat /etc/os-release 2>/dev/null | grep PRETTY | cut -d= -f2)"
echo "kernel    : $(uname -r)  arch: $(uname -m)"
echo "date      : $(date '+%F %T %Z')"

say "应用目录"
if [ ! -d "$ROOT" ]; then
  echo "!! $ROOT 不存在 —— 应用根本没装上，或已被系统清理"
else
  ls -la "$ROOT" 2>/dev/null
fi
echo "target -> ${TARGET:-<无效软链>}"
echo "var    -> ${VAR:-<无效软链>}"

say "程序文件完整性（这是「数据包不完整」的直接依据）"
miss=0
for f in server/nvr_slim.py server/runtime.py server/params.py server/ffmpeg_cmd.py \
         server/retention.py server/link.py server/recorder.py server/condense.py \
         ui/index.html ui/config ui/images/icon-64.png ui/images/icon-256.png; do
  if [ -n "$TARGET" ] && [ -f "$TARGET/$f" ]; then
    printf '  OK   %-26s %s bytes\n' "$f" "$(wc -c <"$TARGET/$f")"
  else
    printf '  MISS %s\n' "$f"
    miss=$((miss + 1))
  fi
done
[ "$miss" -eq 0 ] && echo "  -> 文件齐全" || echo "  -> 缺 $miss 个文件"

say "manifest"
if [ -f "$ROOT/manifest" ]; then
  for k in appname version display_name platform service_port checkport \
           desktop_uidir desktop_applaunchname checksum source; do
    printf '  %-22s %s\n' "$k" "$(grep "^$k" "$ROOT/manifest" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^ *//')"
  done
else
  echo "  !! 缺 manifest"
fi

say "checksum 校验（app.tgz 还在时才有意义）"
if [ -f "$ROOT/app.tgz" ]; then
  got="$(md5sum "$ROOT/app.tgz" 2>/dev/null | cut -d' ' -f1)"
  want="$(grep '^checksum' "$ROOT/manifest" 2>/dev/null | tr -d ' ' | cut -d= -f2)"
  echo "  实际 $got"
  echo "  声明 $want"
  [ -n "$want" ] && [ "$got" = "$want" ] && echo "  -> 一致" || echo "  !! 不一致或缺失"
else
  echo "  app.tgz 已被系统解包后清理（正常），跳过"
fi

say "桌面入口 ui/config（飞牛用它拼打开地址）"
for p in "$TARGET/ui/config" "$ROOT/ui/config"; do
  [ -f "$p" ] || continue
  echo "--- $p"
  cat "$p"
  port="$(grep -o '"port"[^,}]*' "$p" | head -1 | sed 's/.*: *"*//; s/"*$//')"
  case "$port" in
    ''|*[!0-9]*)
      echo "  !! 端口不是纯数字（$port）：飞牛会拼出无效地址，应用必然打不开" ;;
    *)
      echo "  -> 端口 $port（合法）" ;;
  esac
done

say "运行配置"
if [ -n "$VAR" ] && [ -f "$VAR/config.json" ]; then
  echo "  service_port : $([ -f "$VAR/port.txt" ] && cat "$VAR/port.txt" || grep -o '"service_port"[^,}]*' "$VAR/config.json" | head -1)"
  echo "  record_dir   : $(grep -o '"record_dir": *"[^"]*"' "$VAR/config.json" | head -1 | cut -d'"' -f4)"
  echo "  link_mode    : $(grep -o '"link_mode": *"[^"]*"' "$VAR/config.json" | head -1 | cut -d'"' -f4)"
  echo "  config.json  : $(wc -c <"$VAR/config.json") bytes"
else
  echo "  !! 缺 config.json（安装回调没跑成功？）"
fi
[ -n "$VAR" ] && [ -f "$VAR/port.txt" ] && echo "  port.txt     : $(cat "$VAR/port.txt")"

say "进程与监听"
pid="$(cat "$VAR/app.pid" 2>/dev/null | tr -dc '0-9')"
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  echo "  pid $pid 存活"
  ps -o pid,etime,rss,cmd -p "$pid" 2>/dev/null
else
  echo "  未运行（app.pid=${pid:-无}）"
fi
if command -v ss >/dev/null 2>&1; then
  ss -lntp 2>/dev/null | grep -E "python|$APPNAME|:18" | sed 's/^/  /'
fi

say "健康检查"
port="$(cat "$VAR/port.txt" 2>/dev/null | tr -dc '0-9')"
port="${port:-18081}"
if command -v curl >/dev/null 2>&1; then
  echo "  http://127.0.0.1:$port/api/health"
  curl -s -m 3 -o /dev/null -w "  -> HTTP %{http_code}\n" "http://127.0.0.1:$port/api/health" 2>&1
else
  echo "  无 curl，跳过"
fi

say "日志尾部"
for f in "$VAR/log/app.log" "$VAR/log/install.log"; do
  [ -f "$f" ] || continue
  echo "--- $f"
  tail -n 15 "$f" 2>/dev/null | sed 's/^/  /'
done

say "GNU tar 能否正确解开这个包（装不上时的关键证据）"
if [ -f "$ROOT/app.tgz" ]; then
  tar -tzf "$ROOT/app.tgz" 2>&1 | head -5 | sed 's/^/  /'
  echo "  ... 成员数 $(tar -tzf "$ROOT/app.tgz" 2>/dev/null | wc -l)"
fi

printf '\n== 完成 ==\n把上面全部内容贴给开发者即可。\n'
