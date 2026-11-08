# 排障手册（0.9.123）

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
> 当作「进程已经起来了」，部署自检第 8 项反过来要求「无凭证必须 401」。
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
（`settings_cards.js:304`）翻译成中文，`detHintOf()`（`:320`）按错误码给下一步。

十个错误码（`app/server/detect.py:127-136`）：

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

## 9. 部署自检与红线

`tools/deploy_test.py` 的八项自检 = 7 项「要通」+ 1 项「要被拦住」：

| # | 接口 | 判据 |
|---|---|---|
| 1 | `/api/status` | 有 `version` |
| 2 | `/api/health` | `ok` |
| 3 | `/api/health/gaps` | `ok` |
| 4 | `/api/link` | `ok` + `channels` 数 |
| 5 | `/api/log?lines=200` | `ok` + warn/err |
| 6 | `/api/recordings` | `ok` + `cams` |
| 7 | `/api/events` | `ok` + 条数与索引覆盖天数 |
| 8 | `/api/status` **不带凭证** | 必须 `401`（口径与其余相反） |

任一步失败自动回滚到备份并重启；报告写进 `test-reports/deploy-<版本>.json`。

**测试机 `192.168.2.152` 允许自动部署**（两路测试源，端口看 `port.txt`）。
**生产机 `192.168.2.6` 一律手动升级**：自动任务不 ssh、不 scp、不调升级接口、
不重启它的服务——SSH 没有免密凭证，且每次重启会让录制断 5~15 秒，
缺口检测会把这段记成真缺口。

出包后的验收动作：查 `/api/link` 两路 `hstate` 应当是 `recording`；
出现 `stalled` 要先判断是测试环境信号源问题还是产品问题，别直接判部署成功。
