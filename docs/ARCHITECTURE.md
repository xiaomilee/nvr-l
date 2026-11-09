# 架构说明（0.9.127）

这份文档回答「代码在哪、数据怎么流、某个数字是谁算出来的」。
它是给要改代码的人看的：先在这里定位模块，再进源码，别在
`nvr_slim.py` 的 4000 行里从头翻。

**两张表是机器可读的**：`table:modules` 与 `table:api` 两对注释锚点包住的
表由 `test/test_doc_suite.py` 解析，逐项比对 `app/server/*.py` 的文件名集合与
`nvr_slim.py` 里的 `/api/` 字面量。加一个模块、加一条路由却没改这份文档，
`bash build.sh --check` 会当场红。真相在代码里，文档只是索引。

## 1. 分层与红线

```
飞牛应用中心 / 浏览器
        │  HTTP（监听 0.0.0.0，Bearer token 或 HttpOnly cookie）
        ▼
app/server/nvr_slim.py   唯一 HTTP 入口：路由、鉴权、线程编排、状态汇总
        │  （不写业务算法，只把各模块串成一条时间线）
        ▼
领域模块  recorder / ingest / frames / detect / ledger / archive / notify …
        │  （各自持有运行中的对象与状态，暴露 status() 给上层读）
        ▼
盘上数据  <var>/config.json · <record_dir>/日期/*.mp4 · var/event_index.db · _ledger
```

三条红线来自 [`REVIEW_BASELINE.md`](REVIEW_BASELINE.md) 与 ROADMAP 总原则：

1. **录制链路只加不改**。`recorder.py` 的 RTSP 断流退避、MP4 封口、
   进程收尾是真机换来的经验；新能力一律以新模块挂接，不在主循环里加分支。
2. **检测是二等公民**。「检测漏报不能影响全时录像」——录像是事实数据，
   检测只是对事实数据的解释。检测有有界并发、队列、过期帧丢弃，
   取帧与录像的 CPU 不被抢。
3. **数字必须有出处**。界面上任何状态 / 计数都要能追到真实数据源，
   不允许「看起来对」的估值；失败记「未知」，绝不写成「无活动」。

## 2. 模块清单

<!-- table:modules -->
| 模块 | 职责 | 关键出口 |
|---|---|---|
| `nvr_slim.py` | HTTP 入口：路由 / 鉴权 / 线程编排 / payload 汇总 | `Handler.do_GET` `do_POST`、`status_payload()` `link_payload()` |
| `runtime.py` | 配置的读写与强校、默认值、工具探测、端口落盘、槽位池 | `load_config` `save_config` `coerce` `DEFAULTS` `apply_port` |
| `auth.py` | 安装级凭证：生成 / 常量时间比对 / 会话 cookie / 轮换 | `is_public` `verify` `cookie_ok` `rotate` |
| `redact.py` | 日志 / 诊断导出 / ffmpeg stderr 里的 URL 凭据、Bearer、cookie 脱敏 | `mask_text` `redact_cfg`（配置脱敏在 `runtime.py`） |
| `params.py` | 判读与编码参数体系 | 预设与参数快照 |
| `ffmpeg_cmd.py` | ffmpeg / ffprobe 命令行构建 | `precise_cmd` 等 |
| `recorder.py` | 拉流录制：分片落盘、断流退避重连、跨天换目录、**只消费已封口分片** | `Recorder.status()` `reconfigure()` |
| `ingest.py` | 统一取流：一条连接供录制 / 取帧 / AI 复用，记录上游字节时间 | `IngestManager.status()` `idle_sec` |
| `frames.py` | 取帧节拍与快照 tap | `Frames.detail()` |
| `snapshots.py` | 快照落盘与取图路径 | 快照文件读写 |
| `pixdiff.py` | 像素差（移动侦测）与自检 | `self_test` |
| `detect.py` | AI 检测：每路独立节拍 + 有界工作者池、三态 + skip、十个错误码 | `DetectWorker` `status_of` |
| `confirm.py` | 时间窗确认：N 帧中 M 帧命中、类阈值、最短持续、相邻合并 | `Confirmer`（纯状态机，无 IO） |
| `ledger.py` | 活动台账：按分片落盘、合并窗口、可重建投影 | `merge` `avail_spans` |
| `event.py` | 统一事件模型：id 只由「哪一路 + 何时开始」推出 | `summary_of` |
| `event_index.py` | 可从台账整份重建的 SQLite 索引（WAL） | `ensure` `sync_day` `query` `stats` |
| `condense.py` | 判定（只读包大小曲线，不解码）与导出（流复制切段 + concat） | `analyze` 导出函数 |
| `export.py` | 按时间范围批量导出：挑分片、算边界多给、写 manifest / README、流式打 zip | `select` `totals` `manifest` `readme` `write_zip` `Chunked` |
| `link.py` | 衔接调度：两种入队节奏共用同一套消费端 | `Link.status()` `reconfigure()` |
| `archive.py` | 归档裁剪：按台账活动窗口裁出浓缩片段、夜间连跑到待办清空 | `run_archive` `archive_sweep` |
| `retention.py` | 每日定点清理：按日期整目录删、归档未完成硬闸门 | `run_cleanup` `parse_hhmm` |
| `health.py` | 录像可信度：七态状态机、缺口四种洞位置 / 五条原因文案、覆盖率 | `state_of` `gaps_of` `gap_reason` `summarize` |
| `notify.py` | Webhook 通知分发：入队 / cooldown / 三次指数退避 / 内存 recent | `Notifier.status()` `offer()` |
| `notify_tpl.py` | 通知模板：按 webhook host 出各平台最小 body、变量插值、坏模板回退 | `render()` `platform_of()` |
| `notify_history.py` | 通知发送历史落盘：jsonl 追加、脱敏、裁剪、过滤查询 | `append()` `query()` `stats()` |
| `sysinfo.py` | 系统信息（CPU / 内存 / 磁盘） | 状态快照 |
<!-- /table:modules -->

守卫按第一列的 `.py` 文件名与 `app/server/` 实际文件集合比对，
多一个、少一个、拼错一个都会红；表外的说明性文字不参与解析。

## 3. HTTP 路由

分发在 `nvr_slim.py` 的 `_do_get()`（2908）与 `_do_post()`（3415），
**第一行都先过 `_auth_gate(path)`**。`auth.is_public()`（`auth.py:222`）
放行三类：所有非 `/api/` 路径（首页 / 静态）、`/api/auth/status`、
`/api/auth/login`。`/api/auth/rotate` 故意不在白名单——轮换必须凭旧凭证。
POST 另有两道门槛：`X-NVR-Request: 1` 头与 `_host_ok()`（只放行 loopback /
LAN IP / `.local` / `.lan` / `.fnos.net`）。服务**不处理 CORS 预检**，
也不回 `Access-Control-*`，写入类接口天然免疫跨站。

**没有 `do_HEAD` / `do_OPTIONS`**；协议是 HTTP/1.1（keep-alive），
这是外网回放黑屏修复的一部分。

<!-- table:api -->
| 方法 | 路径 | 作用 | 产出 |
|---|---|---|---|
| GET | `/api/auth/status` | 页面加载先问要不要弹粘贴 token | 内联，`need_token` / `version` |
| GET | `/api/health` | 轻量存活探针与版本徽标 | 内联，`version` / `uptime` / `cams` |
| GET | `/api/health/gaps` | 某日录像缺口与覆盖率 | `health_gaps_payload()` → `health.py` |
| GET | `/api/config` | 拉运行时配置（脱敏） | `runtime.load_config` + `redact_cfg` |
| GET | `/api/status` | 总状态看板（含 `notify.history` 落盘计数，缓存不扫盘） | `status_payload()`（2612） |
| GET | `/api/notify/history` | 通知发送历史（`from/to/cam/cat/ok/limit`），只读落盘事实 | `Notifier.history()` → `notify_history.query()` |
| GET | `/api/gpu` | 核显探测 | `gpu(force=True)` |
| GET | `/api/slots` | 并发槽位 | `SlotPool.status()` |
| GET | `/api/live` | 直播快照态 | `live_payload()` |
| GET | `/api/recordings` | 磁盘真实录像清单（3 秒缓存，`force=1`） | `recordings_payload()`（1747） |
| GET | `/api/ledger` | 活动台账 | `ledger_payload()` |
| GET | `/api/events` | 事件列表（`from/to/cam/cat/limit/min_dur/motion`；`cam`/`cat` 支持逗号多值，同维 OR、跨维 AND） | `events_payload()` → `event_index` |
| GET | `/api/events/` | 单条事件 / 事件定位（`<id>` / `<id>/clip`） | `event_payload` / `event_clip_payload` |
| GET | `/api/export/plan` | 批量导出预演（`cam/from/to`；`detail=1` 带逐片明细），只算不清盘；失败也回 200 + `ok:false`（与 `/api/events` 同口径） | `export_plan_payload()`（1957） |
| GET | `/api/export` | 按时间范围导出：已封口分片原样打包成 zip（chunked，边界多给的秒数写进包内 manifest） | `_export_download()`（3600） |
| GET | `/api/archive` | 归档概览 | `archive_payload()` |
| GET | `/api/archive/progress` | 归档进度（只回内存态，不扫盘） | `archive_progress()` |
| GET | `/api/storage/usage` | 存储占用（30 分钟缓存） | `storage_usage()` |
| GET | `/api/record/probe` | 单路真实试录诊断（约 8 秒） | `_probe_one()` |
| GET | `/api/record/parts` | 录像残片 | `parts_payload()` |
| GET | `/api/record/file` | 分片视频，支持 `Range` 206 / `dl=1` / `remux=1` | `_record_file()`（3297） |
| GET | `/api/retention` | 保留策略 | `retention_payload()` |
| GET | `/api/link` | 衔接与录制状态 | `link_payload()`（2671） |
| GET | `/api/probe` | 单文件 ffprobe（packet + frame） | `_probe_file()` |
| GET | `/api/diag/pixel` | 像素判读自检 | `pixdiff.self_test()` |
| GET | `/api/log` | 内存业务日志环（800 行） | 内联，`warn`/`err` 分级计数 |
| GET | `/api/log/file` | 历史日志按字节倒读翻页 | `log_file_payload()` |
| GET | `/api/log/export` | 诊断包下载（全文脱敏） | `log_export_text()` |
| GET | `/api/live/snapshot/` | 快照 JPEG，带 ETag → 304 | `_snapshot()` |
| GET | `/api/live/stream/` | MJPEG 实时流，最多 8 路 | `_live_stream()` |
| POST | `/api/auth/login` | 粘贴 token 换 HttpOnly cookie | `_auth_login()` |
| POST | `/api/auth/rotate` | 轮换 token，旧凭证与全部 cookie 立即失效 | `auth.rotate()` |
| POST | `/api/config` | 保存配置（逐键 `coerce`，通知运行对象） | `changed` / `restart_hint` |
| POST | `/api/notify/test` | 测试 Webhook | `ok` / `ms` / `error` |
| POST | `/api/scan` | 扫描每通道磁盘真实数据 | `channels` / `total` |
| POST | `/api/slot/acquire` | 占并发槽 | `slot` / `status` |
| POST | `/api/slot/release` | 放并发槽 | `status` |
| POST | `/api/retention/policy` | 改保留周期 / 清理时刻 / 录像根目录 | `retention_payload()` |
| POST | `/api/dir/check` | 探测目录存在与写权限（不改配置） | `runtime.probe_record_dir()` |
| POST | `/api/link/policy` | 改衔接策略（**第二条配置写入口**） | `link_payload()` |
| POST | `/api/link/run` | 手动触发一轮衔接 | `link_payload()` |
| POST | `/api/storage/usage/refresh` | 立即重算占用 | `storage_usage(force=True)` |
| POST | `/api/log/clear` | 清空日志 | `ok` |
| POST | `/api/restart` | 自愈重启（`os.execv`，不换 PID） | `restarting` |
| POST | `/api/retention/cleanup` | 清理（默认 `dry_run=true`，二次确认才删） | `run_cleanup()` |
| POST | `/api/archive/run` | 归档裁剪（真跑异步，返回 202） | `start_archive()` |
| POST | `/api/shutdown` | 停服 | `ok` |
<!-- /table:api -->

两条写入口是历史上最容易漏的地方：`/api/link/policy` 也写
`record_enable` / `record_segment_sec` 并落盘。第 18、20 轮分别抓到
「policy 入口没通知 `Recorder`」与「`bool("false")` 把字符串判成开录像」——
两处都已修，且由 `test/test_btn_effect.py` 的 `test_93` / `test_94` 钉住。

## 4. 数据流：从 RTSP 到回放

```
摄像头主码流 RTSP
   │  -c copy 转封装（不解码，一路 4MP 约 3% 单核）
   ▼
record_dir/<cam>/<日期>/HH-MM-SS.mp4    ← 分片默认 300 秒（60~900 可配）
   │  只消费**已封口**的分片：看到下一片出现即说明上一片写完
   ├──→ ledger.py     活动台账（按片落盘，可整份重建投影）
   │       ├──→ event.py + event_index.py   事件页与点击定位
   │       └──→ archive.py                过期全时 → 归档浓缩片段
   ├──→ link.py       衔接调度 → condense 判定 / 导出
   ├──→ health.py     按分片真实时长核对当天覆盖 → 缺口报告
   └──→ HTTP Range    /api/record/file 直接给回放页（必要时 remux faststart）
```

**取流三层拆分**（`health.py:9`）——这是「进程在跑不等于录上了」的代码化：

| 层 | 字段 | 出处 |
|---|---|---|
| 上游有没有字节 | `ingest.idle_sec` | `ingest.py:564` → `_ingest_idle_map()`（`nvr_slim.py:2464`） |
| 分片还在不在长 | `record.channels[].last_age_sec` | `recorder.py:1567`（把正在写的 `.part` 算进去） |
| 今天覆盖了多久 | `avail_spans` 的实际空洞 | `ledger.py:338` → `health.gaps_of()` |

统一判定 `health.state_of()`（`health.py:100`）输出 7 态；
`cams_counts()`（`nvr_slim.py:2486`）按此计 `connected / recording / stalled / error`，
`connecting / recovering` 单独计、不进 `error`（它们自己会回来）。

`hstate` / `hlabel` / `hreason` 是 `link_payload()` 在**出口**补的
（`nvr_slim.py:2686-2695`），不是 `recorder.status()` 原生字段；
`recorder.status()` 只给 `state / pid / retry / stall / segments / segment_sec /
last_seg / last_age_sec / error / kind / hint`。

## 5. 状态汇总：谁算的

| 接口 / 块 | 产出位置 | 说明 |
|---|---|---|
| `/api/status` | `status_payload()` `nvr_slim.py:2612` | `cams` ← `cams_counts()`；`detect` ← `detect.status_of()`；`frames` ← `Frames.detail()`；`ingest` ← `IngestManager.status()`；`notify` ← `Notifier.status()`；`limits` 内联 |
| `/api/link` | `link_payload()` `:2671` | 主体 ← `Link.status()`（`link.py:715`）；`record` ← `Recorder.status()`（`recorder.py:1524`）；出口补 `hstate` |
| `/api/health` | 内联 `:2922` | 版本、uptime、cams 计数、ffmpeg/ffprobe 路径 |
| `/api/health/gaps` | `health_gaps_payload()` `:2528` | 纯计算全在 `health.py` |
| `/api/events` | `events_payload()` `:1549` | 筛选交给 `event_index.query()`（多类别 / 多机位 / 最短时长 / `motion`），不拉全量 |
| `/api/recordings` | `recordings_payload()` `:1747` | 实扫 `full` 与 `_archive` 两套目录 + 3 秒 TTL |

`detect.status_of()` 给的是七项契约（`enabled / available / running / state /
error_code / error_message / last_success_at`）+ 四档 state + 十个错误码，
前端只翻译不判定——以前「没加载完」和「坏了」都写成「worker 未运行」，
用户只能猜要不要重启。

## 6. 前端与构建

页面在设备上仍是**单个 `index.html`**。仓库里的 `app/ui/css/*.css`（5 份）与
`app/ui/js/**/*.js`（22 份）只是源，构建时由 `tools/compose_ui.py` 合成进
`index.html` 的 `<style>` 与唯一一个 `<script>`：

- CSS 顺序固定 `theme → base → components → pages → player`（令牌在前、页面覆盖在后），
  **不按文件名排序**。
- JS 不能定固定顺序：`const`/`let` 不提升，声明出现的次序就是执行次序，
  所以顺序 = `/* @@js:begin xxx.js */` 标记在 `index.html` 里的位置，
  要调顺序就得挪标记，`test/ui/js_split.test.js` 把顺序钉成一张表。
- 不许改成外链或多个 `<script>`：`test/ui/*.test.js` 全都取
  「最后一个 `<script>`」当脚本源，拆成多标签会让十几个测试静默跑错段。
- `css/` 与 `js/` 不进包（`pack_fpk.py` 的 `PKG_SKIP_DIRS`），
  否则会让人以为在设备上改它们能生效。

**合成必须排在 `validate.py` 之前**（`build.sh` 的 `0/5`），
否则 validate 核对的是上一轮的产物。包布局与目录树见 [`../nvr-slim/fpk/BUILD.md`](../nvr-slim/fpk/BUILD.md)。

## 7. 改动之前先看这里

- 加配置键 → 同步 `docs/CONFIGURATION.md`（`test_config_doc.py` 钉住），
  并确认**两条写入口**和所有 `reconfigure()` 消费点；第 17~20 轮连抓三次
  「接口回 ok、配置也落盘、运行中对象仍按旧值跑」。
- 加路由 / 模块 → 同步本文第 2、3 节的两张表（`test_doc_suite.py` 钉住）。
- 碰录制链路 → 先读 `recorder.py` 的断流 / 封口 / 收尾注释，
  新能力走新模块挂接；检测不得阻塞取帧与录像。
- 改前端 → 只改 `app/ui/css/*.css` 或 `app/ui/js/**/*.js`，
  `index.html` 是合成产物。
- 出包后必须 `deploy_test.py` 部署测试机并查 `/api/link` 两路
  `hstate=recording`；**生产机 `192.168.2.6` 一律不碰**。
