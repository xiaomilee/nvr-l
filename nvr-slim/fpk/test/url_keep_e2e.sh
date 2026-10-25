#!/bin/bash
# 跨设备 RTSP 地址保护 —— 真实服务端到端回归（0.9.25）
# 场景：设备 A 配好地址 → 设备 B（本机无明文）推回空地址 → NAS 地址必须还在；
#       只有显式 url_clear 或新明文才真改。
# 用法：  bash test/url_keep_e2e.sh     （默认端口 18097）
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${NVR_PYTHON:-}"
[ -n "$PY" ] || PY="$(command -v python3 || command -v python)"
PORT="${URLKEEP_PORT:-18097}"
WORK="$(mktemp -d)"
# Windows 原生 python 不认 Git Bash 的 /tmp 路径（环境变量不做 MSYS 转换，
# 会被解析成 C:\tmp\...），统一转成 C:/... 混合形式，bash 与 python 两边一致
WORKW="$(cygpath -m "$WORK" 2>/dev/null || echo "$WORK")"
cp -r "$ROOT/app" "$WORK/app"
export TRIM_APPDEST="$WORKW/app"
export TRIM_PKGVAR="$WORKW/var"
export TRIM_SERVICE_PORT="$PORT"
export NVR_PYTHON="$PY"

BASE="http://127.0.0.1:$PORT"
CFG="$TRIM_PKGVAR/config.json"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; }
http() { curl -s --max-time 5 "$@"; }
# 服务端护栏：所有 POST 必须带 X-NVR-Request: 1（官方页面才带的头）
post() { curl -s --max-time 5 -H "X-NVR-Request: 1" "$@"; }
url_of() { "$PY" -c 'import json,sys;print((json.load(open(sys.argv[1]))["cameras"][0].get("url") or ""))' "$CFG"; }

cleanup() { bash "$ROOT/cmd/main" stop >/dev/null 2>&1; }   # 临时目录留在系统 temp，由系统清理
trap cleanup EXIT

# ---- 起服务（与 dryrun 相同的三步）----
wizard_port="$PORT" wizard_source_dir="$WORK/nvr" wizard_output_dir="$WORK/out" \
  bash "$ROOT/cmd/install_init" >/dev/null || { bad "install_init"; exit 1; }
mkdir -p "$WORK/nvr"
wizard_port="$PORT" wizard_source_dir="$WORK/nvr" wizard_output_dir="$WORK/out" \
  bash "$ROOT/cmd/install_callback" >/dev/null || { bad "install_callback"; exit 1; }
bash "$ROOT/cmd/main" start >/dev/null || { bad "服务启动"; exit 1; }

# 等 /api/health 就绪
for i in 1 2 3 4 5 6 7 8 9 10; do
  http "$BASE/api/health" | grep -q '"ok": *true' && break
  sleep 1
done

# ---- 场景 1：设备 A 配好两路地址 ----
post -d '{"cameras":[{"id":"cam-01","name":"车库","url":"rtsp://admin:pw@192.0.2.10:554/ch1","sub_url":"rtsp://admin:pw@192.0.2.10:554/ch3"},{"id":"cam-02","name":"后院","url":"rtsp://admin:pw@192.0.2.16:554/101"}],"snapshot_interval":10}' \
  "$BASE/api/config" | grep -q '"ok": *true' \
  && ok "设备A：写入两路地址" || bad "设备A：写入失败"

DISK1="$(url_of)"
[ "$DISK1" = "rtsp://admin:pw@192.0.2.10:554/ch1" ] \
  && ok "NAS 落盘的是明文地址（录制要用）" || bad "落盘地址异常: $DISK1"

# ---- 场景 2：GET 回给浏览器的是脱敏串，但非空（前端 hasUrl 的依据）----
GETURL="$(http "$BASE/api/config" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["config"]["cameras"][0]["url"])')"
case "$GETURL" in
  rtsp://***@*) ok "API 回传脱敏（$GETURL）且非空 → 前端能判「已配置」" ;;
  "") bad "API 回传 url 为空，前端会误报未配置" ;;
  *) bad "API 回传异常: $GETURL" ;;
esac

# ---- 场景 3：设备 B（本机无明文）改个无关设置，推回空地址 ----
post -d '{"cameras":[{"id":"cam-01","name":"车库改名","url":"","sub_url":""},{"id":"cam-02","name":"后院","url":"","sub_url":""}],"snapshot_interval":5}' \
  "$BASE/api/config" | grep -q '"ok": *true' || bad "设备B：推送失败"
DISK2="$(url_of)"
[ "$DISK2" = "rtsp://admin:pw@192.0.2.10:554/ch1" ] \
  && ok "设备B推空地址 → NAS 地址原样保留（本 bug 的核心场景）" \
  || bad "地址被抹掉了: $DISK2"

# ---- 场景 4：设备 B 填入新地址 → 直接采用 ----
post -d '{"cameras":[{"id":"cam-01","name":"车库改名","url":"rtsp://root:toor@198.51.100.9:554/live","sub_url":""}],"snapshot_interval":5}' \
  "$BASE/api/config" >/dev/null
DISK3="$(url_of)"
[ "$DISK3" = "rtsp://root:toor@198.51.100.9:554/live" ] \
  && ok "填入新明文 → 直接采用" || bad "新地址未采用: $DISK3"

# ---- 场景 5：本地曾有明文的浏览器清空保存（url_clear）→ 真清 ----
post -d '{"cameras":[{"id":"cam-01","name":"车库改名","url":"","sub_url":"","url_clear":true}],"snapshot_interval":5}' \
  "$BASE/api/config" >/dev/null
DISK4="$(url_of)"
[ "$DISK4" = "" ] && ok "显式 url_clear → 地址真清" || bad "url_clear 未生效: $DISK4"

echo
echo "url_keep_e2e：通过 $PASS，失败 $FAIL"
[ "$FAIL" = 0 ]
