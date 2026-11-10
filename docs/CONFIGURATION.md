# 配置项参考（0.9.140）

这份文档回答一个设置项被改完之后**到底谁会重新读它**。它属于批次 5 的文档重构，
方向上是 P0「可信、能无人值守」：前几轮在按钮验收表上连续抓到三个「接口回 ok、
配置也落盘、运行中对象却仍按旧值跑」的缺口（`Link.reconfigure` 漏 `max_queue`、
`_ENUM_KEYS` 漏 `link_mode`、`set_link()` 没通知 `Recorder`），根因都是**没人说得清
这个键该由谁热更新**。把口径写下来，比再补一个测试更能防下一次。

**数字必须有出处**：下面主表里的默认值、类型、范围、生效方式都不是抄界面文案，
而是逐个对回 `app/server/` 的消费点。主表由 `test/test_config_doc.py` 解析并与
`runtime.DEFAULTS` / `_ENUM_KEYS` / 三个范围常量逐项比对——改了代码没改这份文档，
`bash build.sh --check` 会红。文档是给人看的，闸门里那份真相始终是代码。

## 1. 配置存在哪、怎么读写

| 环节 | 事实 | 出处 |
|---|---|---|
| 文件 | `<var_dir>/config.json`，即 `/vol1/@appdata/nvr/config.json` | `runtime.py:360` |
| 读取 | `load_config()` 以 `dict(DEFAULTS)` 打底、再用文件覆盖；按 `(mtime_ns, size)` 判缓存 | `runtime.py:368` |
| 写入 | `save_config()` 先写 `.tmp` + `fsync`，再 `os.replace` 原子替换；失败重试 6 次并清理残骸 | `runtime.py:401` |
| 归一 | 读盘时 `detect` / `notify` 两块一律过 `norm_detect()` / `norm_notify()`，读写两侧同一口径 | `runtime.py:388` |
| 保存 | `POST /api/config` 逐键过 `coerce()` 后再落盘；`updated_at` 是服务端自动写的时间戳 | `nvr_slim.py:3500` |
| 第二条入口 | `POST /api/link/policy` 也写 `record_enable` / `record_segment_sec` / `link_*` 并落盘 | `nvr_slim.py:2704` |
| 出接口 | `redact_cfg()` 抹掉摄像头 URL 里的 `user:pass@`，明文地址不回前端 | `runtime.py:200` |
| 升级兼容 | `merge_defaults()` 保留旧配置里已有的值；改默认值不会把用户已存的端口打回默认 | `runtime.py:520` |

`coerce()` 是写入侧唯一的强校入口，按 `DEFAULTS[key]` 的类型收敛：枚举只认
`_ENUM_KEYS` 白名单、布尔键把 `"false"` / `"0"` 正确判成假、数值键转 `int`、
`record_dir` 额外过 `valid_record_dir()`。**字符串 `"false"` 曾经在
`/api/link/policy` 里被裸 `bool()` 判成 `True`，点「关录像」反而把录像打开**
（第 20 轮已修）——这就是为什么两条入口必须走同一套收敛，不能再各写一次。

## 2. 生效方式：说「改完谁会重新读」

这一列是整份文档的重点，也是最容易写错的一列。必须和「接口上能不能看见」分开：

- **生效方式**说的是改完之后哪个消费方会重新拿到新值；
- **可观测出口**说的是某个 HTTP 接口有没有把运行态字段摆出来。

例如 `condense_mode` 是热生效的（`Link.reconfigure()` 换掉 `self.cfg`，
`condense_mode_of()` 每次现读），但没有任何接口输出「当前是什么台账模式」，
所以验收表 `test/button_matrix.py` 把它判 `partial` 并写明「只能验落盘」。
**`partial` 不等于「改了不生效」**，两张表说的是两件事，别互相改写结论。

<!-- effects:vocab -->
只有下面七种取值允许写进主表，测试会逐行核对：

- `热生效`：保存后由 `/api/config` 或 `/api/link/policy` 通知运行中的对象
  （`Recorder.reconfigure` / `Link.reconfigure` / `_sync_detect_worker`），不必重启服务。
- `每轮读盘`：消费方每轮 `load_config()` 现读，下一次循环才用上新值；
  没有常驻对象持有旧值，所以也不算「改了不生效」。
- `夜间任务`：只在每天 `cleanup_at` 触发的归档/清理任务里读，服务端没有实时出口。
- `部分热生效`：配置与状态显示会跟着变，但某个运行对象是启动期建的，
  要重启才全量生效——`concurrency` 的 `SlotPool` 就是这一类。
- `需重启`：只在 `main()` 启动期读一次，改完必须重启进程（有的还会换端口）。
- `前端常量`：设置页没有真正接入口，值由前端写死或只留在 localStorage，
  改 `config.json` 也不会产生预期效果。
- `遗留字段`：旧版本留下、当前没有有效消费方（或只剩显示），保留是为了读旧配置。
<!-- /effects:vocab -->

## 3. 主表：`runtime.DEFAULTS` 的 29 个顶层键

「范围」列里 `a~b` 是代码里的夹取区间，逗号分隔的是 `_ENUM_KEYS` 白名单；
写 `—` 表示 `coerce()` 只做类型转换、没有额外约束（不代表可以随便填）。
「出处」列是消费点，不是声明处——`DEFAULTS` 本身在 `runtime.py:18`。

<!-- table:config -->
| key | 类型 | 默认值 | 范围 / 枚举 | 生效方式 | 出处 | 说明 |
|---|---|---|---|---|---|---|
| `source_dir` | str | `/vol1/nvr` | — | `遗留字段` | `runtime.py:19` | 录制根目录已统一走 `record_dir`；界面无入口，留着只为读旧配置 |
| `output_dir` | str | `/vol1/nvr` | — | `遗留字段` | `runtime.py:20` | 同上，缩时输出也归 `record_dir` 管 |
| `scan_mode` | str | `h1` | `now, m15, h1, d1` | `遗留字段` | `runtime.py:111` | 枚举仍有效但无消费方；`SCAN_LABEL` 只是文案表 |
| `process_mode` | str | `fast` | `fast, fine` | `遗留字段` | `runtime.py:112` | 同上 |
| `gpu` | str | `auto` | `auto, qsv, off` | `每轮读盘` | `runtime.py:722` | `gpu_report()` 与状态里的降级判断每次请求现算；界面无独立入口，前端按 `GS.gpu` 推 `auto`/`off` |
| `concurrency` | int | `2` | `1~3` | `部分热生效` | `nvr_slim.py:3566` | `/api/status` 的 `limits` 会跟着变，但 `STATE["pool"]` 是 `main()` 里建的、不跟着重建；要真正改槽位池须重启 |
| `night_only` | bool | `false` | — | `前端常量` | `button_matrix.py:198` | 控件被 `LK.clipOut` 包住且默认不渲染，`pushCfg` 也不带这个键，值只进 localStorage |
| `low_priority` | bool | `true` | — | `前端常量` | `runtime.py:564` | 同上只进 localStorage；`limits.prefix` 只是按配置拼出的字符串，ffmpeg 的 `Popen` 没加这个前缀 |
| `snapshot_interval` | int | `10` | `1~10` | `每轮读盘` | `nvr_slim.py:3570` | 取帧 worker 每轮用 `interval_fn` 现读配置；没有「运行中对象」字段可看，验收表按 `partial` |
| `record_dir` | str | `/vol1/@appshare/nvr-output` | 绝对路径，非系统目录 | `需重启` | `runtime.py:233` | `valid_record_dir()` 挡相对路径与系统目录；保存会触发录像重启，且 `restart_hint` 明确提示需重启才全量生效 |
| `cleanup_at` | str | `03:00` | `HH:MM` | `夜间任务` | `nvr_slim.py:2336` | 归档与清理的固定顺序是先归档后清理；`parse_hhmm()` 解析，服务端没有「下次几点跑」的接口 |
| `space_warn_gb` | int | `20` | `0~2000`（0 = 关闭） | `每轮读盘` | `space.py:66` | 空间预警阈值：剩余低于它（或总量 10%）就提醒。预警线程每 10 分钟用 `runtime.load_config` 现读，改完无需重启；`0` 关闭全部空间预警 |
| `archive_enable` | bool | `true` | — | `夜间任务` | `nvr_slim.py:2000` | 夜间任务才读；关掉后只录全时、不做归档裁剪 |
| `archive_days` | int | `7` | `1~3650` | `夜间任务` | `retention.py:37` | 归档片段保留天数，也是总回溯天数；摄像头级可单独覆盖 |
| `archive_batch` | int | `120` | `10~2000` | `夜间任务` | `archive.py:38` | 单次最多裁多少分片，跨夜分批消化；真机可调大避免追不平 |
| `archive_prefetch` | bool | `false` | — | `夜间任务` | `runtime.py:43` | 提前一天归档换一整天补救缓冲，代价是原画与归档同日共存 |
| `record_enable` | bool | `true` | — | `热生效` | `nvr_slim.py:2741` | 录制总闸；`/api/config` 与 `/api/link/policy` 两条入口都要通知 `Recorder`，少一条就会出现「关不掉」 |
| `record_segment_sec` | int | `300` | `60~900` | `热生效` | `runtime.py:105` | ffmpeg 命令行参数，改了要优雅收尾再拉起；`Link` 与 `Recorder` 必须是同一个秒数，否则回放按秒定位跳错片段 |
| `record_reconnect_sec` | int | `5` | — | `需重启` | `nvr_slim.py:3956` | `Recorder` 构造期参数，`reconfigure()` 不覆盖它；界面无入口 |
| `ingest_enable` | bool | `true` | — | `需重启` | `nvr_slim.py:3978` | 统一取流的现场回退开关，`IngestManager` 启动期读一次且没有 `reconfigure`；改它要动录制链路，明确不做热生效 |
| `record_frag` | bool | `true` | — | `前端常量` | `recorder.py:1048` | 0.9.111 起设置页无开关，`applyServerCfg()` 写死 `LK.frag=true` 并纠正服务端；`Recorder.reconfigure` 虽读它，但界面推不进别的值 |
| `link_mode` | str | `live` | `live, scheduled` | `热生效` | `link.py:686` | `live` 录完即压 / `scheduled` 定时批量；`_ENUM_KEYS` 第 17 轮补进白名单，非法值不再落盘回读污染界面 |
| `link_interval_h` | int | `6` | `1~24` | `热生效` | `runtime.py:107` | 定时模式间隔，`Link.reconfigure()` 会重设 |
| `link_max_queue` | int | `200` | `20~2000` | `热生效` | `link.py:693` | 背压硬闸门；第 17 轮抓到 `reconfigure` 漏抄这一行，界面显示 333、实际仍按 200 限流 |
| `clip_style` | str | `join` | `join, split` | `热生效` | `link.py:695` | `join` 整段拼接（流复制）/ `split` 活动切片（重编码）；界面已下线，仍可由接口改 |
| `condense_mode` | str | `virtual` | `virtual, materialize, off` | `热生效` | `link.py:172` | `Link.reconfigure` 换掉 `self.cfg`，`condense_mode_of()` 每次现读；但无独立运行态接口，验收表判 `partial` |
| `service_port` | int | `18080` | `1024~65000` | `需重启` | `runtime.py:330` | 由 `cmd/main` 解析（配置 > `TRIM_SERVICE_PORT` > 18081），占用时往上顺延；真实端口只认 `port.txt` |
| `detect` | dict | 见 §4.1 | — | `热生效` | `nvr_slim.py:3584` | 总闸由 `_sync_detect_worker` 起停 worker；整块保存时过 `detect.norm_detect()` |
| `notify` | dict | 见 §4.2 | — | `每轮读盘` | `nvr_slim.py:3968` | `Notifier` 的 `cfg_fn` 是 `lambda: load_config(a.var)`，每轮现读；`cooldown` 改动不会立刻反映在 `status.notify` |
| `updated_at` | null | `null` | — | `遗留字段` | `nvr_slim.py:3580` | 服务端保存配置时自动写的时间戳，不是设置项，前端推它无效 |
<!-- /table:config -->

**表外的两个键**：`cameras` 与 `profiles` 不在 `DEFAULTS` 里，但由设置页
`pushCfg()` 推送并落盘。摄像头 URL 出接口前脱敏，所以验收表用 `name` 而不是
`url` 判「变没变」（脱敏值恒为 `***`）。每路还可以单独覆盖
`record_segment_sec` / `condense_mode` / `archive_days` / `detect` 等字段，
口径是「显式覆盖优先，`follow` 或缺省回落全局」，见 `nvr_slim.py:687`。

## 4. 嵌套块：两份默认值不一样，别混

嵌套块最容易写错的地方是：**`runtime.DEFAULTS` 不是最终形态**。读盘时
`load_config()` 会用 `detect.norm_detect()` / `detect.norm_notify()` 重新归一，
保存时 `/api/config` 又走一次 `coerce()` 的 dict 分支。三处口径一致，但字段集合
并不完全相同——`detect.confirm` 只在 `norm_detect()` 的输出里出现，
`runtime.DEFAULTS["detect"]` 里没有它。

### 4.1 `detect`

默认来自 `detect.DETECT_DEFAULTS`（`detect.py:88`），`_ENUM_KEYS` 管的是顶层键，
这里另外由 `DETECT_ENUMS` 管：

- `enable=false` 总闸，默认关（opt-in）；`mode=snap | live`、
  `night=full | throttle | off`。
- `device=auto | cpu | gpu`：**0.9.125 起才真的接管推理后端**，此前这个键只
  存在配置里（引擎恒用 `CPUExecutionProvider`，界面还硬写「YOLO11n · CPU」）。
  三档口径：
  - `auto`：有 GPU 后端就用（CUDA > TensorRT > ROCm），没有就 CPU；
    **这不算降级**，不亮黄，`fallback_reason` 为空。
  - `gpu`：点名要 GPU。缺后端、或建会话失败（驱动 / CUDA 版本 / 显存）时
    立刻用 CPU 再建一次；检测照跑、`state` 仍是 `ready`，原因写进
    `fallback_reason`（形如「… 建会话失败：libcudnn.so.9 not found」）。
  - `cpu`：一律 CPU，机器上有 GPU 也不挑，且不报回落（那是用户要的结果）。

  实际后端与原因在 `/api/status` 的 `detect.device`：`requested / actual /
  providers / available / fallback_reason`（`actual` 为空 = 模型还没加载完，
  不拿配置值冒充）。诊断卡标题与「推理设备」那一行都读它。
  **和顶层 `gpu`（`auto|qsv|off`）是两件事**：那个管 ffmpeg 硬解 / 硬编
  （`runtime.gpu_report()`），这个管 onnxruntime 推理，互不影响。
  生效方式：保存设置后服务端重建检测 worker（`_sync_detect_worker` 比对
  `STATE["detect_device"]`），只换检测线程，不碰取帧与录像。
- `classes` / `trigger` 各是三键 dict；`trigger` **至少留一项**，全假会被
  `norm_trigger()` 恢复默认并写一条「配置强校」日志。
- `conf` 是分品类阈值字典，默认 `CONF_PRESETS["std"]`；`live_fps` 夹在 `1~10`。
- `confirm` 时间窗确认块由 `confirm.norm()` 强校（`confirm.CONFIRM_DEFAULTS`），
  **设置页没有它的控件**；保存设置时靠 `nvr_slim.py` 那 3 行沿用磁盘旧值，
  删了就会每次保存都把窗口参数打回默认。这是本块最脆的一处。

### 4.2 `notify`

默认来自 `detect.NOTIFY_DEFAULTS`（`detect.py:98`）：
`webhook=""`、`motion/person/vehicle=false`、`cooldown=60`、
`platform="auto"`、`template=""`。
`norm_notify()` 把 `webhook` / `template` 各截到 500 字符、`cooldown` 夹到
`30~600`、`platform` 按 `notify_tpl.PLATFORMS` 白名单强校（写错一个词回落
`auto`，不会让整条通知发不出去）。
分发器 `Notifier` 用 `cfg_fn` 每轮读盘，所以改冷却时间不需要重启。

- `platform`：`auto` 表示按 webhook 的 host 自动识别（飞书 / 钉钉 / 企业微信 /
  Server酱 / Bark，认不出来回退通用胖 JSON）；其余值强制按该平台出 body。
  存在的理由是公司网关常把机器人地址代理成自家域名，host 已经不是官方域名，
  **自动识别永远认不出来，只能由人指定**。
- `template`：自定义文案，占位符只认 `{cam} {cat} {label} {time} {date}`。
  写了未知占位符、或超过 500 字，不会丢通知，只在渲染时回退默认文案，
  并把原因报到 `/api/status` 的 `notify.render.template_why`——界面上那句
  「模板已回退：未知占位符 xxx」就是从这儿来的。

**这两个键必须由 `pushCfg()` 一起推**：`/api/config` 对 `notify` 块是整块
覆盖的，少推一个键就等于把用户在另一台设备上的选择抹回默认值（§4.1 的
`detect.confirm` 栽过同一个坑）。`test/test_notify_ui.py` 双向钉住：`DEFAULTS`
与 `norm_notify` 的键集合必须相同，前端 `pushCfg` 必须带上这两个键、
`applyServerCfg` 必须能回读，平台白名单前后端逐项一致。

发送历史不占配置键：结果落在 `<var>/notify-history.jsonl`（保留最近 2000 条），
用 `GET /api/notify/history` 查，`/api/status` 的 `notify.history` 只报缓存计数
（条数 / 最后一条时间），**绝不在轮询路径上扫盘**。

### 4.3 已知「假设置」

`test/button_matrix.py` 的 `KNOWN_GAPS` 照出四处界面上看得见、后端却永远收不到的
控件：AI 灵敏度（`pushCfg` 的 detect 块不带 `sensitivity`）、`night_only` /
`low_priority`（只进 localStorage），以及已注释下线却仍被推常量的输出画质等四项。
它们是产品行为欠账，**不是文档能解释过去的配置项**，已记进 ROADMAP
「不在本轮范围」，等设置页整理时一起处理，不在这里给它们编默认值。

## 5. 怎么改这份文档

1. 加一个配置键：先改 `runtime.DEFAULTS` 和 `coerce()`，再往主表加一行——
   `test_config_doc.py` 会发现 `DEFAULTS` 多了键而表里没有，当场红。
2. 改默认值 / 枚举 / 范围：改代码后同步主表对应列；测试逐项比对
   `DEFAULTS`、`_ENUM_KEYS`、`SEGMENT_LIMITS` / `LINK_INTERVAL_LIMITS` / `PORT_LIMITS`。
3. 改「生效方式」：先确认消费点真的变了，再改主表与 §2 词表；词表本身也由
   测试钉住，不许多写或少写一档。
4. 「出处」列必须指向 `app/server/`、`tools/` 或 `test/` 下真实存在的文件，
   测试会逐个查文件是否在。写「出处」不是为了好看，是为了下次有人怀疑某个
   结论时能直接跳到代码核实。

相关文档：`BUILD.md`（怎么跑闸门）、`docs/ROADMAP.md`（为什么做这些）、
`test/test_btn_effect.py`（真起服务验热生效）、`test/button_matrix.py`
（该验什么、怎么算过）。
