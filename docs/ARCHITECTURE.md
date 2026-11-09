# 架构说明（0.9.129）

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
| `detect.py` | AI 检测：每路独立节拍 + 有界工作者池、三态 + skip、十个错误码、画面两块区域（ignore/include） | `DetectWorker` `status_of` `apply_regions` |
| `confirm.py` | 时间窗确认：N 帧中 M 帧命中、类阈值、最短持续、相邻合并 | `Confirmer`（纯状态机，无 IO） |
| `ledger.py` | 活动台账：按分片落盘、合并窗口、可重建投影 | `merge` `avail_spans` |
| `event.py` | 统一事件模型：id 只由「哪一路 + 何时开始」推出 | `summary_of` |
| `event_index.py` | 可从台账整份重建的 SQLite 索引（WAL） | `ensure` `sync_day` `query` `stats` |
| `event_shot.py` | 事件快照：确认那一帧落盘（`var/event_shots/<cam>/<日期>/`）、检测框旁挂同名 `.json`、7 天过期、每路每天上限、窗口内取最早一张 | `ShotStore` `find` `shot_of` `boxes_of` `meta_of` `read_meta` `pick` |
| `motion_shot.py` | 运动事件快照：纯帧差运动事件（AI 没框住）在查询路径发现无图时，后台从 recorder 已录分片抽一帧补进 `var/event_shots/`，时间锚定事件起点、幂等（窗口内已有图就跳过）、队列满丢、分片过期 / 没 ffmpeg 就退化 None，复用 `ShotStore` 落盘与查询，前端零改动 | `MotionShotService` `request` `status` `epoch_of` `locate_segment` |
| `condense.py` | 判定（只读包大小曲线，不解码）与导出（流复制切段 + concat） | `analyze` 导出函数 |
| `export.py` | 按时间范围批量导出：挑分片、算边界多给、写 manifest / README、流式打 zip | `select` `totals` `manifest` `readme` `write_zip` `Chunked` |
| `link.py` | 衔接调度：两种入队节奏共用同一套消费端 | `Link.status()` `reconfigure()` |
| `archive.py` | 归档裁剪：按台账活动窗口裁出浓缩片段、夜间连跑到待办清空 | `run_archive` `archive_sweep` |
| `retention.py` | 每日定点清理：按日期整目录删、归档未完成硬闸门 | `run_cleanup` `parse_hhmm` |
| `space.py` | 存储空间预警（纯函数）：按分层日速率与保留天数算稳态需求、定等级、给一条可执行建议 | `daily_rate` `rates_of` `evaluate` `text_of` |
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
| GET | `/api/events/` | 单条事件 / 事件定位 / 事件快照（`<id>` / `<id>/clip` / `<id>/shot`；快照 200 走 `image/jpeg`+ETag，没有则 404 JSON；`/shot?meta=1` 回那张图的检测框 JSON） | `event_payload` / `event_clip_payload` / `_event_shot` / `_event_shot_meta` |
| GET | `/api/export/plan` | 批量导出预演（`cam/from/to`；`detail=1` 带逐片明细），只算不清盘；失败也回 200 + `ok:false`（与 `/api/events` 同口径） | `export_plan_payload()`（1957） |
| GET | `/api/export` | 按时间范围导出：已封口分片原样打包成 zip（chunked，边界多给的秒数写进包内 manifest） | `_export_download()`（3600） |
| GET | `/api/archive` | 归档概览 | `archive_payload()` |
| GET | `/api/archive/progress` | 归档进度（只回内存态，不扫盘） | `archive_progress()` |
| GET | `/api/storage/usage` | 存储占用与空间预警（30 分钟缓存；`days` 近 30 天每日字节，`space` 预警块）；`?force=1` 强制重算 | `storage_usage()` `space_eval()` |
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

**事件快照（0.9.129）挂在检测链上，不挂在录像链上**：

```
取帧层 snapshots/<cam>.jpg（720p / q:v4，每 N 秒一张）
   │  detect 这一拍读的就是它 → 推理 → 区域 → 确认
   ▼  确认出窗口之后（台账已写）
detect._shot()  读这张 jpg 的字节 + 取它自己的 mtime（"当时"）+ 这一拍的 dets
   │  ShotStore.offer()  仅 30 秒/路限流 + 每日上限 + 非阻塞入队
   ▼
var/event_shots/<cam>/<日期>/<HHMMSS>_<mmm>.jpg   （独立单线程写，7 天过期）
                             <HHMMSS>_<mmm>.json  （0.9.130 检测框，可缺）
   │
   ├──→ /api/events 每条补 shot{url,at,offset} → /api/events/<id>/shot（JPEG）
   └──→ 放大时一次 /api/events/<id>/shot?meta=1（框清单，几百字节）
```

**运动事件快照（0.9.134）补齐纯帧差运动事件的「那一眼」**：0.9.129 的快照只在 AI 确认那一拍落盘（`ShotStore.offer()` 仅 `detect.py` 调用），纯帧差运动事件（`KIND_MOTION`，AI 没框住）在 0.9.133 及之前一直是 `ev.shot is None`。0.9.134 新增 `motion_shot.MotionShotService`：在事件查询路径发现某条 `kind=="motion"` 且还没有图的事件时，后台用 `ffmpeg -ss <offset> -i <分片> -frames:v 1` 从 `recorder` 已录好的分片里抽一帧补进 `event_shots/`，时间锚定事件起点（用图片 mtime 锚到事件当天，不是「现在」），**复用 `ShotStore` 落盘与查询、前端零改动**。抽帧在独立线程、幂等（窗口内已有图就跳过）、队列满丢、分片过期 / 没 ffmpeg 就退化 None，不抛到查询路径、不碰 `recorder.py`、不写 `.json` 旁挂（运动事件没检测框）。

**补图在真实事件量下的三处修正（0.9.135）**：0.9.134 在测试机上能出图，生产机却几乎补不出来，原因是三条只在真实事件量下暴露的假设。

1. **限流档位**：`ShotStore.offer()` 的 `min_gap`（30 秒/路）是为「同一条事件被检测节拍反复确认」设的，前提是 `at` 单调跟现在走。补旧事件时 `at` 是历史时刻、翻页还会倒退，`at - last` 变成负数 → 被判定为「30 秒内又来一张」整片丢弃，于是只有相隔 30 秒以上的事件才有图。0.9.135 给 `offer()` 加 `gap=False` 档位：补图绕过实时限流，日上限（`MAX_PER_DAY`）与队列满丢照旧；关掉时不更新 `_last`，实时检测的节流口径不被历史时刻污染。
2. **投递顺序**：`event_index.query()` 末尾 `reversed(rows)`，返回的是**时间升序**。顺着投，最老的事件先占满队列（`QUEUE_MAX` 32），而它们的录像早已过期、`locate_segment` 恒返回 None，既不出图也不推进；每次刷新都在重投同一批，最新事件永远排不上。0.9.135 改为 `for e in reversed(evs)` 从最新往前投，并给「定位不到分片」加 `MISS_TTL`（600 秒）负缓存，冷却期内 `request()` 直接跳过。
3. **计数口径**：`_handle` 原先忽略 `offer()` 的返回值，抽到帧就 `extracted += 1`，而落盘层可能以日上限/限流拒收 —— 计数器全绿、列表一张图没有。0.9.135 拆成 `extracted`（抽到帧）/ `stored`（真写进 `event_shots`）/ `rejected`（被拒收），`/api/status` 的 `motion_shot` 一并暴露。

另外单条事件详情（`event_payload`）也会对无图的运动事件投一次补图：从通知或分享链接直接点进来时，列表可能从未被打开过。

**补图可自证（0.9.136）**：前两版的问题不是「没实现」，而是「看不到卡在哪」——`stored=0` 可能是录像目录找错、分片已被清、`ffmpeg` 跑不动、目录写不进，四种处置完全不同，而聚合计数把它们混成一个 0。现在每条补图都留一条轨迹（`motion_shot._trace`，环形 6 条）：解析出的目录、`dirs_seen`（真实命中的目录）、`files`/`segs`（当天扫到几个 `.mp4`、其中几个能当全时分片）、定位到的分片与片内偏移、`ffmpeg` 的 stderr 摘要、落盘收没收回。轨迹随 `/api/status` 的 `motion_shot.trace` 暴露。

`GET /api/motion_shot/probe?cam=&date=&start=` 是同一套判断的「当场自检」：只读、不抽帧、不落盘，一次答完四件事——目录尝试了哪些/命中哪些、当天分片数、定位结果、`event_shots` 目录能不能写（向上找第一个存在的祖先判 `W_OK`）。诊断页的「事件快照」卡直接展示两池计数 + 轨迹 + 自检按钮。

`ffmpeg` 也从「拿到路径就用」改成先验：路径不存在时按 `PATH` 找（`shutil.which`）、查执行权限、`-version` 确认真能跑，结论缓存；抽帧失败会去掉 `-c:v mjpeg` 再试一次（精简版 ffmpeg 可能没有独立 mjpeg 编码器）。失败原因写进 `_last_err` 随轨迹带走 —— 以前一律静默返回 `None`，线上只能看到 `failed` 涨。

**空间预警（0.9.131）复用存储统计，不另扫一遍盘**：
```
storage_usage()   扫 <cam>/<日期>/ 目录元数据（30 分钟缓存）
   │  per-cam {full, archive, clip, days{}} + 分层每日字节 _kind{}
   ▼
space_eval(free_b, total_b, used_b, kind_days)
   │  space.daily_rate()  最近 7 个完整日的中位数（丢掉 0 字节的日子）
   │  need   = Σ rate[kind] × 保留天数[kind]
   │  other  = 总容量 − 剩余 − 录像实测占用
   │  headroom = 总容量 − other − need
   ▼
level ∈ {ok, warn, critical, unknown, off} + reasons[] + advice（服务端出中文）
   ├──→ /api/storage/usage.space   诊断页存储卡顶部那条
   ├──→ /api/status.space          监控线程 10 分钟写一次，**只读内存**
   └──→ /api/health.space{level,free_gb}   桌面壳高频轮询，只给结论
```

三条口径：**只读元数据**（不解码、不往录像目录写）、**算事实**
（没有完整日就回 `unknown`，不编天数）、**只报告不处置**（自动删录像会
让"空间紧张"改写事实数据，红线）。

三条不能松的口径：**拍「当时」**（用源图 mtime，绝不拿当前快照充数）、
**只加不改**（台账之后、独立线程、队列满就丢，`recorder.py` 一行不碰）、
**可清理**（7 天过期、每路每天上限、**不进事件索引** —— 索引是台账投影，
把图路径写进去，清理之后就出现指向空气的行）。

框是**旁挂**的（0.9.130）：坐标用 `postprocess` 的归一化 `cx,cy,w,h`，绘制在
浏览器里（几个绝对定位的 `<i>`），服务端不解码、不重编码。框写盘失败只丢框，
单独计 `meta_failed` —— 与"图写失败"是两件事。

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

**画面区域的过滤只有一处**：`detect.apply_regions()` 依次做「类别 → 忽略区 →
只统计区」，`_one()` 只调它，没有第二份判定。`cam.masks` 两块都是 16×9 格号
（`row*16+col`），解码成归一化矩形后与归一化框直接比，不需要画面尺寸。

顺序有意义：**忽略区优先**（重叠按忽略），且只统计区留空时恒真——升级不改
任何既有判定。被只统计区挡掉的框数进 `per_cam.outside`（按天），诊断页读它
说「区外忽略 N」；`skip` / `unknown` / `outside` 三档互不混算。

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

移动端适配（0.9.133）仍在同一套单 `index.html` 里做，不新增页面与路由：事件详情把快照与
回放拆成两段（先确认再打开视频）、放大浮层加「横过来看 / 放大」适配竖屏、机位筛选从横滑
chips 改为底部弹层多选、事件列表加加载骨架与可重试失败态；回放 `<video>` 在 `visibilitychange`
回前台时自动 resume，离开直播单路实时层或切 tab 时主动断 MJPEG 流。新增窄屏断点只在事件页
相关控件上动，全站此前零 `@media`。

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
