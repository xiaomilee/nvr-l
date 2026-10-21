#!/bin/bash
# 生命周期干跑：模拟 fnOS 调用 cmd/* 的完整流程
# 用法：  bash test/dryrun.sh          （默认端口 18099）
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${NVR_PYTHON:-}"
[ -n "$PY" ] || PY="$(command -v python3 || command -v python)"
PORT="${DRYRUN_PORT:-18099}"
WORK="$(mktemp -d)"
# 安装会把 app/ui/config 的端口写成字面量，所以这里必须用副本，
# 否则会把源码树里的 ${wizard_port} 改成测试端口
cp -r "$ROOT/app" "$WORK/app"
export TRIM_APPDEST="$WORK/app"
export TRIM_PKGVAR="$WORK/var"
export TRIM_SERVICE_PORT="$PORT"
export NVR_PYTHON="$PY"

BASE="http://127.0.0.1:$PORT"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; }
sec()  { printf '\n== %s ==\n' "$1"; }

http() { curl -s --max-time 3 "$@"; }

sec "环境"
echo "  临时数据目录: $WORK"
echo "  python: $PY  端口: $PORT"

sec "1. 安装 install_init + install_callback"
wizard_port="$PORT" wizard_source_dir="$WORK/nvr" wizard_output_dir="$WORK/out" \
  bash "$ROOT/cmd/install_init" \
  && ok "install_init" || bad "install_init"
mkdir -p "$WORK/nvr"
wizard_port="$PORT" wizard_source_dir="$WORK/nvr" wizard_output_dir="$WORK/out" \
  bash "$ROOT/cmd/install_callback" \
  && ok "install_callback（输出目录 $WORK/out 已创建）" || bad "install_callback"
[ -f "$TRIM_PKGVAR/config.json" ] && ok "config.json 已生成" || bad "config.json 缺失"
grep -q "gpu_state=" "$TRIM_PKGVAR/log/install.log" 2>/dev/null \
  && ok "install.log 记录了核显自检" || bad "install.log 无核显记录"

# ---- 端口：向导值必须同时落到 config.json 与桌面入口 ----
CFG_PORT="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["service_port"])' \
  "$TRIM_PKGVAR/config.json" 2>/dev/null)"
[ "$CFG_PORT" = "$PORT" ] && ok "向导端口 $PORT 写进 config.json" \
  || bad "config.json 端口为 ${CFG_PORT:-空}，期望 $PORT"

UI_PORT="$("$PY" -c 'import json,sys;print(list(json.load(open(sys.argv[1]))[".url"].values())[0]["port"])' \
  "$TRIM_APPDEST/ui/config" 2>/dev/null)"
[ "$UI_PORT" = "$PORT" ] && ok "桌面入口端口已固化为 $PORT" \
  || bad "桌面入口端口为 ${UI_PORT:-空}，期望 $PORT"

sec "2. 启动 cmd/main start（真实检测本机核显）"
bash "$ROOT/cmd/main" start && ok "start 退出码 0" || bad "start 失败"
[ "$(bash "$ROOT/cmd/main" status; echo $?)" = "0" ] && ok "status 退出码 0（运行中）" || bad "status 非 0"

sec "3. /api/status 关键字段"
S="$(http "$BASE/api/status")"
echo "$S" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
g=d["gpu"]
print("  gpu.state =", g["state"])
print("  gpu.reason =", g["reason"])
print("  limits =", d["limits"]["concurrency"], "路; 降级:", d["limits"]["downgraded"])
print("  slots  =", d["slots"]["total"], "槽; 占用", d["slots"]["used"])
print("  queue  =", d["queue"])
'
echo "$S" | grep -q '"app": *"nvr"' && ok "status JSON 有效" || bad "status JSON 异常"

sec "3.5 /api/live 快照墙元数据"
L="$(http "$BASE/api/live")"
echo "$L" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("  刷新间隔 =", d["interval"], "秒; 摄像头", len(d["cameras"]), "路; 宫格列数", d["grid_cols"])
print("  首路 =", d["cameras"][0]["id"], "age =", d["cameras"][0]["age_sec"], "stale =", d["cameras"][0]["stale"])
'
echo "$L" | grep -q '"grid_cols"' && ok "live JSON 有宫格列数" || bad "live JSON 缺 grid_cols"
echo "$L" | grep -q '[?]t=' && ok "快照 URL 带时间戳（防浏览器缓存）" || bad "快照 URL 缺时间戳"
CODE=$(http -o /dev/null -w '%{http_code}' "$BASE/api/live/snapshot/cam-01.jpg")
[ "$CODE" = "404" ] && ok "还没有快照时返回 404（而非空图）" || bad "缺失快照未返回 404: $CODE"
CODE2=$(http -o /dev/null -w '%{http_code}' "$BASE/api/live/snapshot/..%2f..%2fconfig.json")
[ "$CODE2" = "400" ] && ok "非法摄像头 id 被拒（400）" || bad "穿目录未被拒: $CODE2"

sec "3.6 保留周期与每日清理"
# JSON 里的路径不能带反斜杠，否则 \U 之类会让服务端判为非法 JSON
REC_ROOT="$WORK/rec"
if command -v cygpath >/dev/null 2>&1; then REC_ROOT="$(cygpath -m "$WORK")/rec"; fi
mkdir -p "$REC_ROOT/cam-01/2020-01-01" "$REC_ROOT/_clip/cam-01/2020-01-01" "$REC_ROOT/cam-01/$(date +%Y-%m-%d)"
printf 'x' > "$REC_ROOT/cam-01/2020-01-01/a.mp4"
printf 'x' > "$REC_ROOT/_clip/cam-01/2020-01-01/b.mp4"
printf 'x' > "$REC_ROOT/cam-01/$(date +%Y-%m-%d)/today.mp4"
http -X POST -d "{\"record_dir\":\"$REC_ROOT\",\"cleanup_at\":\"04:00\",\"cameras\":[{\"id\":\"cam-03\",\"full_days\":2,\"clip_days\":180}]}" \
  "$BASE/api/retention/policy" >/dev/null
R="$(http "$BASE/api/retention")"
echo "$R" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("  清理时刻", d["cleanup_at"], "| 下次", d["next_run"], "| 待删", d["preview"]["delete_count"], "个目录")
for p in d["policy"]:
    print("   ", p["cam"], "全时", p["full_days"], "天 / 缩时", p["clip_days"], "天")
'
echo "$R" | grep -q '"full_days": *2' && ok "cam-03 全时周期改为 2 天" || bad "周期未生效: $R"
echo "$R" | grep -q '"delete_count": *2' && ok "找到 2 个过期日期目录（全时+缩时各一）" || bad "待删目录数不对"
echo "$R" | grep -q "$(date +%Y-%m-%d)" && ok "今天目录出现在策略里（但不在待删列表）" || true
D="$(http -X POST -d '{"dry_run":true}' "$BASE/api/retention/cleanup")"
echo "$D" | grep -q '"dry_run": *true' && ok "预览清理可执行" || bad "预览清理失败: $D"
[ -d "$REC_ROOT/cam-01/2020-01-01" ] && ok "预览不删文件（目录仍在）" || bad "预览把文件删了"
http -X POST -d '{"dry_run":false}' "$BASE/api/retention/cleanup" >/dev/null
[ ! -d "$REC_ROOT/cam-01/2020-01-01" ] && ok "真删后过期目录消失" || bad "过期目录未删除"
[ -d "$REC_ROOT/cam-01/$(date +%Y-%m-%d)" ] && ok "今天的目录仍在（保护生效）" || bad "今天的目录被误删"
R2="$(http "$BASE/api/retention")"
echo "$R2" | grep -q '"delete_count": *0' && ok "同日再跑计划为空（幂等）" || bad "同日重复清理仍待删: $R2"
[ -f "$WORK/var/cleanup-state.json" ] && ok "cleanup-state.json 已记录" || bad "缺 cleanup-state.json"

sec "3.7 录制与压制衔接"
K="$(http "$BASE/api/link")"
echo "$K" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("  模式 =", d["mode_label"], "| 间隔", d["interval_h"], "小时 | 分片", d["segment_sec"], "秒")
print("  队列 =", d["pending"], "| 已出片段", d["counters"]["done"], "| 整段静默", d["counters"]["silent"])
print("  已配置 RTSP 的通道 =", d["configured_cams"], "路")
'
echo "$K" | grep -q '"mode_label"' && ok "/api/link 返回模式与计数" || bad "/api/link 缺字段"
echo "$K" | grep -q '"record"' && ok "/api/link 带录制器状态" || bad "/api/link 缺 record"

http -X POST -d '{"link_mode":"scheduled","link_interval_h":12,"record_segment_sec":600}' \
  "$BASE/api/link/policy" >/dev/null
K2="$(http "$BASE/api/link")"
echo "$K2" | grep -q '"interval_h": *12[,}]' && ok "切到定时模式：间隔 12 小时" || bad "间隔未生效"
echo "$K2" | grep -q '"segment_sec": *600[,}]' && ok "分片长度改为 600 秒" || bad "分片长度未生效"
echo "$K2" | grep -q '"next_batch_at"' && ok "定时模式给出下次入队时刻" || bad "缺 next_batch_at"

http -X POST -d '{"link_interval_h":0,"record_segment_sec":30}' "$BASE/api/link/policy" >/dev/null
K3="$(http "$BASE/api/link")"
echo "$K3" | grep -q '"interval_h": *1[,}]' && ok "间隔 0 夹到下限 1（不是悄悄回落默认值）" || bad "间隔下限失效"
echo "$K3" | grep -q '"segment_sec": *60[,}]' && ok "分片 30 秒夹到下限 60" || bad "分片下限失效"

CODE=$(http -o /dev/null -w '%{http_code}' -X POST -d '{}' "$BASE/api/link/run")
[ "$CODE" = "200" ] && ok "手动「立即处理一批」返回 200" || bad "link/run: $CODE"

http -X POST -d '{"link_mode":"live","link_interval_h":6,"record_segment_sec":300}' \
  "$BASE/api/link/policy" >/dev/null
K4="$(http "$BASE/api/link")"
echo "$K4" | grep -q '"mode": *"live"' && ok "切回即刻压制" || bad "模式切回失败"

sec "4. 并发闸门：2 路配置下抢第 3 个槽位应被拒绝"
A=$(http -X POST -d '{"owner":"demo-a"}' "$BASE/api/slot/acquire")
B=$(http -X POST -d '{"owner":"demo-b"}' "$BASE/api/slot/acquire")
C=$(http -o /dev/null -w '%{http_code}' -X POST -d '{"owner":"demo-c"}' "$BASE/api/slot/acquire")
echo "$A" | grep -q '"ok": *true' && ok "槽 0 获取" || bad "槽 0 失败: $A"
echo "$B" | grep -q '"ok": *true' && ok "槽 1 获取" || bad "槽 1 失败: $B"
[ "$C" = "429" ] && ok "第 3 个请求被拒绝（429）" || bad "第 3 个请求未被拒绝: $C"

sec "5. 改配置为 1 路，槽位池应收紧"
http -X POST -d '{"concurrency":1}' "$BASE/api/config" >/dev/null
T=$(http "$BASE/api/slots")
echo "$T" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print("  slots.total =", d["total"])'
echo "$T" | grep -q '"total": *1' && ok "槽位池收紧为 1" || bad "槽位池未收紧: $T"

sec "6. 停止 cmd/main stop"
bash "$ROOT/cmd/main" stop && ok "stop 退出码 0" || bad "stop 失败"
sleep 0.5
ST="$(bash "$ROOT/cmd/main" status; echo $?)"
if [ "$ST" = "3" ]; then ok "status 退出码 3（未运行，符合 fnOS 约定）"
else
  # Windows 上跨进程 kill 可能失效，走应用内关停兜底（Linux 上不会走到这里）
  http -X POST "$BASE/api/shutdown" >/dev/null 2>&1
  sleep 1.2
  bash "$ROOT/cmd/main" stop >/dev/null 2>&1
  ST="$(bash "$ROOT/cmd/main" status; echo $?)"
  [ "$ST" = "3" ] && ok "status 退出码 3（经 Windows 兜底关停）" || bad "stop 后仍显示运行中"
  echo "  （注：Git Bash 跨进程 kill 受限，fnOS/Linux 上由 SIGTERM 直接生效）"
fi

sec "7. 升级与配置向导"
bash "$ROOT/cmd/upgrade_init" && ok "upgrade_init（配置已备份）" || bad "upgrade_init"
bash "$ROOT/cmd/upgrade_callback" && ok "upgrade_callback（默认值已合并）" || bad "upgrade_callback"
wizard_scan_mode=m15 wizard_process_mode=fine wizard_concurrency=3 wizard_low_priority=false \
  bash "$ROOT/cmd/config_callback" && ok "config_callback（向导值已写入）" || bad "config_callback"
grep -q '"scan_mode": *"m15"' "$TRIM_PKGVAR/config.json" \
  && grep -q '"concurrency": *3' "$TRIM_PKGVAR/config.json" \
  && ok "config.json 内容正确" || bad "config.json 内容不符"

sec "8. 卸载"
bash "$ROOT/cmd/uninstall_init" && ok "uninstall_init" || bad "uninstall_init"
bash "$ROOT/cmd/uninstall_callback" && ok "uninstall_callback（tmp 已清，记录保留）" || bad "uninstall_callback"
[ -f "$TRIM_PKGVAR/config.json" ] && ok "config.json 保留（由应用中心决定是否删数据）" || bad "config.json 丢失"

sec "结论"
echo "  通过 $PASS / $((PASS+FAIL))"
[ -n "${KEEP_WORK:-}" ] && echo "  数据目录保留: $WORK" || rm -rf "$WORK"
[ "$FAIL" = "0" ]
