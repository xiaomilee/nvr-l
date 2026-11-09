# 排障手册（0.9.128）

这份文档只写**能查到出处的**。每条命令、每个接口、每个状态值都指向真实代码位置，
不写「大概试试重启」这类没有判据的动作。

## 0. 三条总原则

1. **先分层，再动手**。绝大多数「录不上」不是服务挂了，而是三层里某一层断了：
   上游字节 / 分片增长 / 当天覆盖（[`ARCHITECTURE.md`](ARCHITECTURE.md) §4）。
   先定位到具体一层，再决定动作；一上来重启会把现场清掉。
2. **状态数字必须有出处**。看到「5 路在录」这类话先追它读的是哪个对象。
   历史上 `cams.recording` 数的是「配了 RTSP 且启用」的路数，
   院子每 25 秒断一次流，这个数字从头到尾都是 5（0.9.118 修正）。
3. **失败不等于没有**。取帧失败、模型没加载、断流都记「未知」，
   绝不写成「无活动」；日志里 `[WARN]` / `[ERR]` 要单独看。

**先只读，再动手**：`app/tools/doctor.sh`（装后位于
`/var/apps/nvr/target/tools/doctor.sh`）是一次性只读体检，输出可直接贴回来：
应用目录软链、程序文件完整性、manifest 关键字段、checksum 比对、
`ui/config` 端口合法性、进程 / 监听 / 健康检查、日志尾部、
GNU tar 能否正确解开这个包。

```bash
sudo bash /var/apps/nvr/target/tools/doctor.sh
```

## 1. 装不上 / 「应用数据包不完整」

| 判据 | 出处 |
|---|---|
| 包内成员必须齐全（`manifest` / `app.tgz` / `cmd/` / `config/` / `ui/` / 两枚 ICON / `wizard/install`） | `tools/pack_fpk.py:263` |
| `manifest.checksum` 必须等于 `app.tgz` 的实测 md5 | `tools/pack_fpk.py:193` |
| `ui/config` 的 `port` 必须是**纯数字**，残留 `${wizard_port}` 必然打不开 | `doctor.sh:63-75` |
| 应用标识必须是纯字母 `nvr`（不带连字符） | `manifest:1` |

历史根因写在 [`../nvr-slim/fpk/CHANGELOG.md`](../nvr-slim/fpk/CHANGELOG.md)：
0.4.x 把 `appname` 改成 `nvr-l` 后用户名也带连字符，安装中途判包无效；
0.4.0 的桌面入口占位符没被替换，URL 变成
`http://127.0.0.1:${wizard_port}/`。这两处现在都由 `validate.py` 断言拦住
（`rules` 必须是数组、自定义字段必须 `wizard_` 前缀、端口必须是数字）。

```bash
# 只查不删，确认有没有旧标识残留
ls -d /vol1/@appcenter/nvr-l /vol1/@appdata/nvr-l /var/apps/nvr-l 2>/dev/null
# 本机出的包若在真机报结构错误，改用官方工具对照（唯一 100% 正确的路径）
sudo bash tools/build_on_fnos.sh
```

**数据目录不可写**（`cmd/install_init` 的报错）不是软件名问题，是安装钩子越权建目录：
此时 `/vol1/@appdata/` 属 root、应用用户还没就绪。现在 `install_init` 只做探针，
`install_callback` 建完目录后再按需修属主。真遇到残留坏属主目录：

```bash
chown -R nvr:nvr /vol1/@appdata/nvr && chmod 700 /vol1/@appdata/nvr && setfacl -b /vol1/@appdata/nvr
```

## 2. 打不开 / 局域网直连 `ERR_CONNECTION_REFUSED`

服务监听 `0.0.0.0`（`cmd/main:156` 显式传 `--host`）；只绑 `127.0.0.1` 时
飞牛桌面经网关转发到 loopback 是通的，但手机直连 NAS IP 必然被拒（0.3.1 修过）。

```bash
ss -lntp | grep -E '1808[0-9]'                         # 看监听地址与真实端口
tail -30 /vol1/@appdata/nvr/log/app.log                 # 看是不是根本没起来
bash /var/apps/nvr/cmd/main status; echo $?              # 0=运行 3=停止 其它=异常
cat /vol1/@appdata/nvr/port.txt                          # 真实端口只认这个文件
```

**桌面图标指向旧端口**：`runtime.apply_port()` 同时写 `config.json`、
`ui/config`（飞牛桌面真正读的）和 `var/port.txt` 三处；启动脚本成功后还会
补一次 `pinport`。只改其中一处就会「服务在跑、图标点不开」。

首次打开弹「粘贴 token」是正常的：`/api/auth/status` 返回 `need_token=true`。
免凭证的只有首页 / 静态资源 / `/api/auth/status` / `/api/auth/login`，
其余 `/api/*` 与录像下载都要 `Authorization: Bearer` 或登录 cookie
（`app/server/auth.py:222`）。

> **`/api/health` 也要凭证**。`cmd/main` 的 `wait_health()` 明确把 401 / 403
> 当作「进程已经起来了」，部署自检第 9 项反过来要求「无凭证必须 401」。
> 用 curl 探活却忘了带 token，看到 401 别当成服务没起。

## 3. 端口被占

端口优先级：`var/config.json` 的 `service_port` > `TRIM_SERVICE_PORT` > `18080`
（`cmd/main:8-16`、`manifest:9`）。被占**不再阻断安装**，启动时自动向上找空闲端口，
最多 +40（`nvr_slim.py:3803-3833`）；全部占满才在 stderr 给
「用 `ss -lntp | grep <port>` 查占用进程，或在应用设置 → 服务端口换一个后重启」。

```bash
cat /vol1/@appdata/nvr/port.txt      # 顺延后的真实值
curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:$(cat /vol1/@appdata/nvr/port.txt)/api/health"
```

> `nvr_slim.py:3798` 的注释写默认端口 18081 是笔误；
> `manifest:9`、`nvr_slim.py:3767`、`cmd/main:10` 一致为 **18080**。

## 4. 录像卡住 / 某一路不录 / 回放页空白

按三层顺序查，每层一个接口：

```bash
PORT=$(cat /vol1/@appdata/nvr/port.txt)
TOK=$(python3 -c 'import json;print(json.load(open("/vol1/@appdata/nvr/secret.json"))["api_token"])')
H="Authorization: Bearer $TOK"

# ① 每路真实状态（hstate 七态、last_age_sec、error、hint）
curl -s -H "$H" "http://127.0.0.1:$PORT/api/link"
# ② 上游字节 / 取帧 / 检测 / 通知 / 资源
curl -s -H "$H" "http://127.0.0.1:$PORT/api/status"
# ③ 当天真实覆盖与缺口（force=1 绕过 3 秒缓存）
curl -s -H "$H" "http://127.0.0.1:$PORT/api/health/gaps?force=1"
# ④ 单路真实试录（约 8 秒，直接给 verdict：目录写不进 / 无视频流 / 拿不到关键帧 / ...）
curl -s -H "$H" "http://127.0.0.1:$PORT/api/record/probe?id=<cam-id>"
# ⑤ 「回放页为什么是空的」先看盘上到底有什么
curl -s -X POST -H "$H" -H 'X-NVR-Request: 1' -H 'Content-Type: application/json' \
  -d '{}' "http://127.0.0.1:$PORT/api/scan"
curl -s -H "$H" "http://127.0.0.1:$PORT/api/recordings?force=1"
# ⑥ 残片与单文件探针
curl -s -H "$H" "http://127.0.0.1:$PORT/api/record/parts?cam=<cam-id>"
curl -s -H "$H" "http://127.0.0.1:$PORT/api/probe?cam=<cam-id>&day=YYYY-MM-DD&name=HH-MM-SS.mp4"
```

**`hstate` 速查**（`app/server/health.py:30` 的 `STATE_LABEL`）：

| 值 | 中文 | 通常意味着 |
|---|---|---|
| `off` | 未启用 | 这一路配置里关了 |
| `stopped` | 已停止 | 进程没跑且没有重试 |
| `connecting` | 连接中 | 正在重连，自己会回来 |
| `recording` | 录像中 | 三层都正常 |
| `stalled` | 卡住 | **进程还在但停止输出**：上游断流或分片不增长 |
| `recovering` | 恢复中 | 断流后退避重连中 |
| `error` | 异常 | 进程已退出且带 error |

`stalled` 与 `error` 要分开处置：拿「重启一下」去处理一个还活着的进程只会白折腾。
阈值 `STALL_SEC = 60.0`（`health.py:26`）；缺口只报 ≥ `GAP_MIN_SEC = 60.0` 的洞，
1 分钟以下的小缝是 mtime 秒级抖动与 GOP 边界的正常现象。

**缺口原因文案**共 5 条（`health.py:161-178` 的 `gap_reason()`）：
当日 00:00 起没录像 / 进程在跑但已停止输出 / 断流后正在重连 /
录制进程已退出 / 该时段进程未运行（已恢复）。前端逐段列起止、时长与原因。

**回放页空白的另一类根因**是维度：同一天有全时原画时，界面可能只显示
`full`；归档产物要切到「归档浓缩」才看得见。文件页的「全时 N / 归档 N」
双 chip 就是为这个准备的（0.9.103 / 0.9.104）。

## 5. AI 检测不识别 / 一直「未知」

```bash
curl -s -H "$H" "http://127.0.0.1:$PORT/api/status" | python3 -m json.tool | less
curl -s -H "$H" "http://127.0.0.1:$PORT/api/diag/pixel"      # 像素判读自检
curl -s -H "$H" "http://127.0.0.1:$PORT/api/config"           # 看 detect 块
```

`/api/status` 的 `detect` 块由 `detect.status_of()` 一处判定，四档 state：
`disabled / starting / unavailable / ready`；前端 `detStateZh()`
（`settings_cards.js:385`）翻译成中文，`detHintOf()`（`:401`）按错误码给下一步。

十个错误码（`app/server/detect.py:145-154`）：

| 码 | 含义 |
|---|---|
| `FFMPEG_NOT_FOUND` | 这台机器上没找到 ffmpeg |
| `MODEL_MISSING` / `MODEL_LOAD_FAIL` | 随包模型 `yolo11n.onnx` 不在 / 加载报错 |
| `RUNTIME_FAIL` | onnxruntime / numpy 与本机 Python 版本对不上 |
| `FRAME_FAIL` | 取帧失败（含上游断流） |
| `INFER_ERR` | 推理异常 |
| `WORKER_DOWN` | 检测进程没起来 |
| `QUEUE_FULL` / `STALE_FRAME` | 队列满丢这一拍 / 排队超一个节拍的过期帧被丢 |
| `STATUS_ERR` | 状态汇总自身出错 |

诊断页「AI 检测」卡还有**调度行**：并发几路、队列 `queued/queue_max`、
丢弃分类（队列满 / 过期）、一轮 `round_avg_ms` 与路数；每路有「未知 N」
「同帧跳过 N」「最近错误 `[code]`」。

> `unknown` 不等于漏报，也不等于「没人来过」。`skip` 是取帧层
> 「本轮没有新画面」的正常节拍错位，混进 `unknown` 会每两拍刷一次红。
> 检测是二等公民：有界并发、队列满就丢这一拍，**绝不阻塞取帧与录像**。

### 5.1 配了 GPU，诊断卡却说 CPU

先看 `detect.device` 这个块（`/api/status` 里，0.9.125 起）：

```bash
curl -s -H "$H" "http://127.0.0.1:$PORT/api/status" \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["detect"]["device"], ensure_ascii=False, indent=1))'
```

| 字段 | 读法 |
|---|---|
| `requested` | 配置里要的是什么（`auto` / `cpu` / `gpu`） |
| `actual` | 这次会话**真的**建在哪个后端：`cuda` / `tensorrt` / `rocm` / `cpu`；空串 = 还没加载模型 |
| `providers` | 实际传给 onnxruntime 的列表，CPU 永远垫底（单算子不支持时落 CPU 算，不整帧失败） |
| `available` | onnxruntime 自己报的可用后端，`fallback_reason` 的出处 |
| `fallback_reason` | 非空就是**降级**：要么 `gpu` 挑不到后端，要么 GPU 建会话失败后用 CPU 重试成功 |

三条判断：

1. `requested=gpu` 且 `fallback_reason` 里写「没有 … 后端」——这台的
   onnxruntime 是 CPU 版（随包就是 CPU wheel）。要 GPU 得换带 CUDA 的
   onnxruntime，且驱动 / CUDA 版本要对得上；**不是配置写错了**。
2. `fallback_reason` 里写「建会话失败：…」——后端在、但起不来，最常见是
   `libcudnn.so` 缺失或版本不匹配。这时检测仍在跑（CPU），不会因为选 GPU 就停。
3. `actual` 空、`state=ready` 以外的值——模型还没加载完，别急着判断设备。

> 不用 GPU 也能跑：`auto` 在只有 CPU 后端的机器上就是 CPU，且**不算回落**，
> 不会亮黄。显式选 `cpu` 同理 —— 那是你要的结果。
> 改 `device` 后保存设置即可生效：服务端会重建检测 worker（只换检测线程，
> 不碰取帧与录像）。

## 6. 回放黑屏 / 拖动卡顿

| 手段 | 出处 |
|---|---|
| `GET /api/record/file?...&remux=1` 把碎片化 fMP4 重封装为 moov 前置的 faststart | `nvr_slim.py:3331`（0.9.99 加，iOS Safari / 微信 WebView 必需） |
| `?dl=1` 导出原片 | `:3364` |
| `Range` → 206 + `HTTP/1.1` keep-alive | `:3339`、`protocol_version` `:2756`（0.9.98，外网 NAT 会话数） |
| 前端 `error` / `stalled` 自愈：当前段失败重试 ≤3 次后跳下一段 | `app/ui/js/player/player.js:25` |
| 归档 `-g 2×fps` + `-vsync cfr` 让拖动只解 ≈2 秒关键帧 | 0.9.108 归档修复 |

事件点击定位走 `/api/events/<id>/clip`，服务端给 `{分片, 偏移, url}` 与
五种 `missing_reason`（未封口 / 分片不在 / 空文件 / 结构不完整 / 那时没录）。
**播不了不跳页、不给黑框**，原因直接写在卡片上。

**事件页筛不出东西，先按顺序查这三处**（0.9.126）：

1. **类别拼错会被明确拒绝**：`/api/events?cat=persn` 回 `ok:false` 并列出可用值。
   以前是静默忽略——用户以为筛过了，看到的却是一整天的全部事件。
2. **`motion` 不是第四种类别**：它是"一条 AI 类别都没有"的剩余集（`categories=''`）。
   想「人员 + 移动侦测」得两个都传：`cat=person,motion`；只传 `cat=motion` 时，
   有人员的事件**不会**出现，这是设计而不是漏。
3. **`min_dur` 的单位是秒**：`min_dur=30` 是"不少于 30 秒"，不是 30 分钟；
   跨零点的事件在日界被截断，时长按当天那一段算。

三条都不成立还空，就看列表头那行条件：它写的是**服务端实际用上的**条件
（`filters` 回显），与界面高亮不一致时以它为准。

## 6.0 AI 不报事件：先分「没看见」和「看见了但被挡」

画面里明明有人，事件页一片安静，先别急着以为模型坏了（0.9.128）。三种情况
长得一模一样，只有诊断页那行数能分开：

| 诊断页每路一行 | 含义 | 怎么办 |
|---|---|---|
| 未知 N > 0 | 抽帧失败 / 推理异常 / 排队过期，**不知道有没有人** | 看「最近错误」的错误码，按 §5 的表处理 |
| 区外忽略 N > 0 | 看见了，但框中心不在「只统计区」里 | 事件页安静是**配置的预期结果**。真关心那块就把只统计区放大，或清空（=整幅画面） |
| 抽帧在涨、无命中、区外 0 | 那一拍确实没有目标 | 调类别 / 灵敏度，或确认这一路 AI 是否开着 |

判断依据来自 `detect.apply_regions()`：类别 → 忽略区 → 只统计区，忽略区
**优先**。所以两块重叠时，框会被算进「忽略」，**不会**进「区外忽略」——
`区外忽略` 只统计"只统计区挡掉的"，这一点决定了该去改哪块区域。

`cam.masks.include` 留空是**不限制**，空数组不是"什么都不算"。手改配置文件
写错格号（负数、≥144、`1.5`、`null`）会被 `norm_masks()` 丢掉，不会把整路
检测弄坏，但也别指望它生效——设置页保存一次就会写成干净值。

## 6.1 导出时间段：包下不来 / 解不开 / 少了几段

导出是「先计划、再打包」两步，出问题先分清是哪一步（0.9.127）。

| 症状 | 先看这里 | 结论 |
|---|---|---|
| 点「开始导出」灰着 | `GET /api/export/plan?cam=&from=&to=` 的 `ok` 与 `error` | 按钮的可用性完全由这一份决定，不看别的 |
| `ok:false` + 「超过单次 1.9 GB 上限」 | `total.count` / `total.bytes` | 缩小范围或减少机位。上限是硬拦截，不会下一个半截包 |
| `ok:false` + 「没有已封口的分片」 | 「正在写的那一片」 | 刚录完的几分钟还没封口（`.mp4.part`），等下一片切出来 |
| `ok:false` + 「只剩归档片段」 | `/api/archive` | 全时已过保留期，归档片段不在导出范围（见 ROADMAP 候选池） |
| 下载到一半断了 | 服务端日志里 `导出：…` 那行有没有 | 有 = 已发完；没有 = 中途异常，chunked 收尾块没写，**包就是不完整的** |
| 包能下、解压报错 | 同上 | 同上：不要用半截包，重导一次 |

三条口径容易误会：

1. **包里的片子会比选的范围多一点**。分片按 5 分钟落盘，导出整片不裁剪，
   所以两头最多各多出约 5 分钟。多出多少写在计划提示与 `manifest.json` 的
   `head_extra_sec` / `tail_extra_sec` 里。
2. **文件名不等于内容长度**。`09-00-00.mp4` 是**起始时刻**，不是"只有一分钟"；
   跨零点的那一片会归在起始那天。
3. **不同机位同名时目录会带 id**。两路都叫「客厅」时包内目录是
   `客厅_cam-01` / `客厅_cam-02`，否则两路的录像会叠在一起。

## 7. 「改了不生效」专门查这里

症状：接口回 `ok`、`/api/config` 回读也变了、磁盘 `config.json` 也写了，
但运行中对象仍按旧值跑。批次 5 连抓三次，根因都是**消费端没被通知**：

| 缺口 | 修法 |
|---|---|
| `Link.reconfigure()` 漏 `max_queue` | 按 `__init__` 同口径 clamp 热更新 |
| `runtime._ENUM_KEYS` 漏 `link_mode` | 补白名单，非法值不再原样落盘回读 |
| `/api/link/policy` 没通知 `Recorder` | `set_link()` 条件触发补一次 `reconfigure` |
| `record_enable` 用裸 `bool()` | 改走 `runtime.coerce()`，字符串 `"false"` 不再判成开 |

排查顺序（别只测 `/api/config`）：

1. GREP 这个 key 的**全部消费点**，逐个确认热更新路径；
2. 确认两条写入口（`/api/config`、`/api/link/policy`）都通知了所有相关对象；
3. 看 [`CONFIGURATION.md`](CONFIGURATION.md) 的「生效方式」列——
   它说的是**改完谁重新读**，和「接口上有没有观测出口」是两回事；
   `condense_mode` 热生效但没有运行态字段，验收表如实判 `partial`，
   **不是「改了不生效」**；
4. 有些项本来就要重启（`record_dir`、`ingest_enable`、`service_port`），
   `/api/config` 的 `restart_hint` 会明说；
5. 夜间任务才读的项（`cleanup_at`、归档四项）没有实时出口，只能验落盘。

自动化的四层证据在 `test/test_btn_effect.py`（真起本地服务）与
`test/btn_harvest.py`（全表采集，报告写进 `test-reports/`）。
**采集器只打 `127.0.0.1`**：改配置是有副作用的动作，指向测试机会变成一次真实配置抖动。

## 8. 日志与诊断导出

- `/api/log`：内存环最近 800 行，带 `warn` / `err` 分级计数。
  判级在 `nvr_slim.py:131-172`：错误关键词（不可写 / 未找到 ffmpeg / 崩溃…）
  记 `[ERR]`，警告关键词（失败 / 断开 / 重连 / 卡住 / 队列已满 / 残片…）
  记 `[WARN]`；「按新参数重启」刻意不算警告，否则真断流会淹没在黄里。
- `/api/log/file?lines=&end=&access=1`：按字节从落盘文件尾部倒读，
  一页 800 行往前翻，不整份加载；默认只给业务行，访问行要显式带 `access=1`
  （访问行占九成以上，拖一次进度条就是上百条 Range 请求）。
- `/api/log/export`：一键诊断包，含状态摘要、通道清单（URL 脱敏）、
  最近业务日志、HTTP 访问汇总、`var/record-diag/*.log` 里录制失败的
  命令行与完整 stderr，全文再过 `redact.mask_text()`。
- 设置页「只看警告」筛选、诊断卡「运行日志」分级着色
  （`settings_cards.js:581` 的 `logLineClass`）。
- `/api/notify/history`：通知发送历史的落盘事实（`notify-history.jsonl`），
  支持 `from` / `to` / `cam` / `cat` / `ok` / `limit`。诊断卡上的「最近 5 条」
  只是内存环，重启即丢；查「昨晚那条到底发没发出去」必须走这个接口。

## 8.1 通知没响 / Webhook 收不到

按顺序查，别一上来就改配置：

1. **有没有入队**：`/api/status` 的 `notify.recent` 最近 20 条。一条都没有，
   多半是类别开关没勾、Webhook 地址为空，或 cooldown（默认 60 秒）把重复事件
   静默丢了——移动侦测连续触发时这是**设计行为**，不是故障。
2. **发了几次、错在哪**：`/api/notify/history?ok=0` 看失败记录，
   `tries=3` 表示三次指数退避都用完了，`err` 里是 HTTP 状态码或网络异常。
   注意 `err` 与 `cam` 都脱敏过，URL 里的 key 不会出现在文件里。
3. **平台对不对**：同一条记录里的 `platform` 是按 Webhook 域名判的。
   若是 `generic`，发的是兼容用的「四套键齐全」胖 JSON；飞书 / 钉钉 /
   企业微信 / Server酱 / Bark 各自只收自己那套最小 body。用自建转发时
   域名认不出来属正常，回到通用 body 即可，不必改代码。
   **但公司网关把机器人地址代理成自家域名时，`generic` 就是病根**：部分平台
   对多余字段直接拒收。到设置页「通知通道 → 平台」手选一项，保存后
   `/api/status` 的 `notify.render` 会变成 `{"platform":"dingtalk","auto":false}`，
   历史里新记录的 `platform` 也跟着变。
4. **文案模板有没有被回退**：`/api/status` 的 `notify.render.template_why` 非空
   就说明模板没生效（未知占位符 / 超过 500 字），此时发的是默认文案，
   通知不会丢。设置页那一行「模板已回退：…」显示的就是这个字段。
5. **设置页「测试发送」**：`POST /api/notify/test` 同步发一条，直接回
   `ok` / `ms` / `error` / `platform`；它也记一笔历史，方便和真实通知对账。
   请求会带上界面上刚填的 `template` / `platform`，所以「没保存就测」测的是
   屏幕上那句，而不是磁盘上的旧值。
6. **落盘本身失败**：日志里有「通知历史落盘失败（不影响发送结果）」时，
   是 `var` 目录不可写，**通知已经发出去了**，先把磁盘权限修好，别重复触发。

`Notifier` 在独立 daemon 线程里派发，失败最多重试 3 次（2s→4s），
绝不在取帧 / 录制路径上同步发请求；所以「通知坏了」不会拖慢录像，
反过来录像有问题也不该先从通知查起。

## 9. 部署自检与红线

`tools/deploy_test.py` 的九项自检 = 8 项「要通」+ 1 项「要被拦住」：

| # | 接口 | 判据 |
|---|---|---|
| 1 | `/api/status` | 有 `version` |
| 2 | `/api/health` | `ok` |
| 3 | `/api/health/gaps` | `ok` |
| 4 | `/api/link` | `ok` + `channels` 数 |
| 5 | `/api/log?lines=200` | `ok` + warn/err |
| 6 | `/api/recordings` | `ok` + `cams` |
| 7 | `/api/events` | `ok` + 条数与索引覆盖天数 |
| 8 | `/api/notify/history?limit=5` | `ok` + `items` 是数组（**0 条也算通过**：测试机不一定发过通知，这项守的是「文件还没生成也不 500」） |
| 9 | `/api/status` **不带凭证** | 必须 `401`（口径与其余相反） |

任一步失败自动回滚到备份并重启；报告写进 `test-reports/deploy-<版本>.json`。

**测试机 `192.168.2.152` 允许自动部署**（两路测试源，端口看 `port.txt`）。
**生产机 `192.168.2.6` 一律手动升级**：自动任务不 ssh、不 scp、不调升级接口、
不重启它的服务——SSH 没有免密凭证，且每次重启会让录制断 5~15 秒，
缺口检测会把这段记成真缺口。

出包后的验收动作：查 `/api/link` 两路 `hstate` 应当是 `recording`；
出现 `stalled` 要先判断是测试环境信号源问题还是产品问题，别直接判部署成功。
