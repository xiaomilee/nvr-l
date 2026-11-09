# 变更与排障沿革

这份文档收的是**历史**：为什么某个版本长成现在这样、真机上踩过哪些坑、
哪些结论当时还没验证。它从 `BUILD.md` 搬出（第 24 轮），
因为 `BUILD.md` 只该讲「怎么构建」——排障史混在里面，
新人找构建命令得先翻过 180 行版本考古。

**版本发布的逐条说明在 `manifest` 第 15 行的 `changelog`**（安装后在
应用中心「版本说明」里能看到），那里是面向用户的一句话说清「这版改了什么」。
本文件不复制它，只保留**工程判断与排障过程**，以及 manifest 里放不下的
验证清单与未覆盖项。整改路线图的轮次台账在 `docs/ROADMAP.md`。

## 1. 版本沿革（一句话索引）

| 版本 | 主题 |
|---|---|
| 0.3.0 | 录制由本应用自己做；两种衔接节奏；数据目录权限修复 |
| 0.3.1 | 局域网直连（`--host 0.0.0.0`） |
| 0.4.0 | 端口可配置 + 修两个真 bug（向导漏进包、干跑改源码树） |
| 0.4.1 | 「应用数据包不完整」：包结构与官方 fnpack 逐项对齐 |
| 0.5.0 | 标识改回纯字母 `nvr`、向导暂不进包、端口链路三处修正 |
| 0.9.90 ~ 0.9.117 | 界面收敛 / 归档压缩 / 回放兼容 / 日志分级 / 文件页性能（逐版见 manifest） |
| 0.9.118 | 录像可信度：真实状态机 + 缺口报告（进程在跑不等于录上了） |
| 0.9.119 | 实测修复「状态原因自相矛盾」+ 测试机部署工具 `deploy_test.py` |
| 0.9.120 | 安全：API Token / 凭据脱敏 / 前端统一请求层 |
| 0.9.121 | 事件中心：统一事件模型 + 跨摄像头检索 + 点击定位 |
| 0.9.122 | 检测治理：有界并发 + 时间窗确认 + 诊断完整 |
| 0.9.123 | 前端源码拆分 + 文档重构 + 按钮验收自动化（批次 5） |
| 0.9.124 | Webhook 通知补齐：多模板 / 平台手选 / 文案模板 / 发送历史落盘（批次 6） |
| 0.9.125 | 推理设备：GPU 自动探测与自动回落（批次 7） |

> **0.9.125 的工程判断（写在这里，免得下一个人又把它当成"加个开关"）**：
> `detect.device` 从 0.9.x 起就在 `DETECT_DEFAULTS` 与 `DETECT_ENUMS` 里，
> 设置页也把它当设置项，但 `Engine` 一直无条件
> `providers=["CPUExecutionProvider"]`，诊断卡标题硬写着「YOLO11n · CPU」。
> 于是"配了 GPU"是一个**从未被验证过的说法**——接口回 ok、配置也落盘，
> 实际跑在哪只有读代码才知道。这一版把它变成事实：只认 onnxruntime 自己报的
> 可用后端（不按机器名猜、不看 `nvidia-smi`，猜错的形态是"配了 GPU 却静默回落"），
> 挑不到或建会话失败就用 CPU 再建一次，**回落不算故障**（`state` 仍是 `ready`），
> 但原因必须写进 `detect.device.fallback_reason` 并显示在诊断卡上。
> 两个故意的取舍：① 只认 CUDA / TensorRT / ROCm，不含 OpenVINO ——
> 它自己也能跑 CPU，选它等于没法回答"到底用没用上显卡"；② 不引入新错误码，
> 降级不是失败，混进 `error_code` 会让"能用"被报成"坏了"。
> `gpu` 这个顶层键管的是 ffmpeg 硬解 / 硬编（`runtime.gpu_report()`），
> 与推理后端是两件事，文档里已分开写。

> 0.9.123 是进度表给批次 5 定的目标版本。用户在 11-08 要求「先出个当前进度的包
> 看一下」，因此第 22 轮提前 bump 五处并出了**进度预览包**，manifest changelog
> 明确写「本版未收尾」。后续切片继续在这个版本号上累积，不再 bump 第二次；
> 批次真正收尾时改 changelog 措辞、重新出同名包（md5 会变）并重新部署测试机。

## 2. 历史排障记录（原 `BUILD.md` 第 0.3.0 ~ 0.5.0 节）

以下各节是当时的原文判断，保留「为什么这么改」，不做现代化改写。
其中部分路径与端口是当时的形态（例如 `nvr-l` 标识、`/vol1/@appdata/nvr-l`），
**不要照着现在的包去执行**；现状以 `README.md` 与 `docs/ARCHITECTURE.md` 为准。

### 0.5.0 回到 0.3.1 的标识与形态（真机对照结论）

真机对照给出了决定性线索：**0.3.1 可以安装（只是端口被占），0.4.x 一律报
「应用数据包不完整」**。把 0.3.1 → 0.4.x 的差异逐项列出来，最可疑的一处是
**应用标识从 `nvr` 改成了 `nvr-l`**。

飞牛用 `manifest.appname` 同时派生出四样东西：`/var/apps/<appname>`、
`/vol1/@appcenter/<appname>`、`/vol1/@appdata/<appname>`，以及
`config/privilege` 里声明的**应用专用 Linux 用户与组**。改成 `nvr-l` 后
用户名也是 `nvr-l`，带连字符的标识在飞牛的标识/用户名校验里一旦不通过，
安装中途就会判包无效——这与「包本身能完整解开」并不矛盾。

这一版把标识改回纯字母：

| 项 | 0.4.x | 0.5.0 |
|---|---|---|
| `manifest.appname` | `nvr-l` | `nvr` |
| `config/privilege` 用户/组 | `nvr-l` | `nvr` |
| `desktop_applaunchname` / `ui/config` key | `nvr-l.main` | `nvr.main` |
| 共享目录 | `nvr-l-output` | `nvr-output` |

界面显示名仍是 **NVR-L**（`display_name`），用户看到的名字不变。

**同时去掉的第二个变量：wizard/**。0.3.1 的包里没有 `wizard/`（当时打包器漏了），
0.4.x 才加进来。两个变量同时存在就无法定位，所以这一版向导暂不进包：
端口、录制目录、衔接方式全部走内置默认值，装完在应用设置里照常能改。
打包器加了开关 `NVR_PKG_WIZARD=1`，需要做对照实验时再打开。

**向导自身的两处规范违反**（等验证完再放回去）：

1. **`rules` 必须是数组**。`wizard/install` 里 `record_dir` 写成了对象
   `{"required":true,"message":"..."}`，其余字段都是数组；解析器遇到类型
   不一致的表单项，很容易整份向导判为无效。
2. **自定义字段统一 `wizard_` 前缀**。官方明确「字段名直接成为环境变量」，
   而我们写的是 `port` / `record_dir` / `link_mode`，`cmd/` 里却按
   `wizard_port` / `wizard_record_dir` 取值——**取到的永远是空**，
   于是向导里改端口、改衔接方式全部无效，端口一直回落到
   `manifest.service_port`。现已改为 `wizard_*`，`cmd/` 侧兼容不带前缀的旧写法。

`validate.py` 加了两条断言（`rules` 必须是 list、有 `field` 就必须
`wizard_` 前缀），防止再犯。

**端口链路三处修正**：默认端口 18081（当时 18080 在真机上被占）；
`install_init` 里端口被占不再阻断安装，只写警告，启动时向上顺延到空闲端口，
启动成功后 `pinport` 把桌面入口对齐到真实端口；
`runtime.set_ui_port` 同时改 `/var/apps/nvr/ui/config`（飞牛桌面真正读的）
和 `/var/apps/nvr/target/ui/config`（app.tgz 里的副本）——
0.4.x 只改后者，所以图标永远停在旧端口。

### 0.4.1 修复「应用数据包不完整」

装上后打开应用提示 **「应用数据包不完整，无法继续使用。请卸载后重新安装。」**，
卸载重装同样报错。排查方式是把它和官方 `fnpack` 的产物逐项对齐，
找出所有「我们和官方不一样」的地方。

**最重要的怀疑：桌面入口里的 `${wizard_port}` 没被替换**。0.4.0 把
`app/ui/config` 的 `port` 写成了 `${wizard_port}` 占位符，飞牛桌面直接用这个值
拼打开地址，一旦占位符没被替换，URL 就是 `http://127.0.0.1:${wizard_port}/`。
改写 `target/ui/config` 是在安装回调做的，此时 `@appcenter` 下的应用目录
可能还不归应用用户所有，写失败会被 `except OSError: return False` 静默吞掉。

改法不再是「换个地方写」，而是**从根上不依赖占位符**：
`app/ui/config` 的 `port` 写合法数字、安装回调改写失败写明确警告、
`cmd/main` 启动成功后补一次 `runtime.py pinport`、`validate.py` 断言端口必须是数字。

**包结构三处与官方 `fnpack` 不一致**（从官方 `tar -czf "$FPK_NAME" *` 反推）：
成员名 `./manifest` → `manifest`；补目录条目 `cmd/ config/ ui/`；
包根 `ui/` 去掉多塞的 `ui/index.html`。`manifest` 也改成官方的
`key            = value`（等号第 16 列）。

**其它**：桌面入口 `type` 从 `iframe` 改为 `url`（飞牛桌面是 https，
内嵌 http 的 iframe 会被按混合内容拦截）；删除 `nvr-l.sc` 端口转发声明
（端口是安装时用户自选的，写死一个端口自相矛盾；服务监听 `0.0.0.0` 不需要转发）；
新增 `app/tools/doctor.sh` 一次性只读体检。
`tools/pack_fpk.py` 的排除逻辑拿绝对路径判断，暂存目录 `.packtmp`
恰在排除名单里导致 `cmd/*` 被全扔掉，改为只对相对路径判断、暂存目录改名 `build_stage`。

### 0.4.0 端口可配置 + 两个真 bug

- 安装向导第 1 步「Web 服务端口」，默认 18080；`app/ui/config` 的 `port`
  改为 `${wizard_port}`，安装回调固化成字面量；`manifest` 设 `checkport=false`。
- `install_init` 装前真实 bind 一次，被占直接失败并提示换端口；
  运行期兜底：bind 失败时自动向上找空闲端口（最多 +40），把实际值写回
  `config.json`、`app/ui/config` 和 `var/port.txt`。
- **移除 `health.json`**：它按 manifest 的固定端口探活，用户一改端口就判
  「未运行」；改为由 `cmd/main` 的 `wait_health` 读 `var/port.txt` 的真实端口。
- 两个真 bug：① `wizard/` 从来没打进包（`pack_fpk.py` 只复制了
  `cmd/ config/ ui/`）；② 干跑测试会改源码树——`TRIM_APPDEST` 指向真实
  `app/`，安装回调把 `app/ui/config` 的 `${wizard_port}` 改成了测试端口 18099，
  改为先 `cp -r` 到临时目录再跑。

### 0.3.1 修复：局域网直连打不开

`ERR_CONNECTION_REFUSED` on `http://<NAS-IP>:18080` 的根因是服务只 bind 了
`127.0.0.1`——飞牛桌面入口经网关转发到 loopback 是通的，
但从手机/电脑直连 NAS 的 IP 必然被拒。

- `nvr_slim.py` 新增 `--host`（默认取 `TRIM_LISTEN_HOST`，回落 `0.0.0.0`）；
- `cmd/main` 启动时显式传 `--host "${TRIM_LISTEN_HOST:-0.0.0.0}"`；
- 端口被占时不再抛 traceback，直接给 `ss -lntp` 排查提示并以 1 退出；
- `/api/status` 增加 `listen` 字段，启动首行日志改成 `listen=0.0.0.0:18080`。

### 0.3.0 的三件事

**① 数据目录不可写不是软件名问题**，是 `install_init` 越权建目录：
这个钩子在安装流程最前面执行，此时 `/vol1/@appdata/` 属 root、
应用用户还没就绪，`mkdir -p` 必然失败。改法：`install_init` 只做探针，
`install_callback` 建完目录后按需 `chown -R` + `chmod 700` + `setfacl -b`
（ACL 的 mask 被压成 `---` 时，属主权限再对也写不进去）；
`cmd/main` 启动前再探一次写，把报错点前移。

**② 录制由本应用自己做**：`recorder.py` 用 `-c copy` 转封装落盘，
一路 4MP 只占约 3% 单核；分片默认 5 分钟（可配 60~900 秒），
按 `日期/HH-MM-SS.mp4` 命名。只消费**已封口**的分片：segment muxer 顺序写，
看到下一个分片出现就说明上一个写完了——这条规则比 mtime 稳，也比 inotify 简单；
正在写的那个分片 moov 还没落地，读它就是坏文件。

**③ 两种衔接节奏，共用一套判定与导出**：

| | 即刻压制（live） | 定时压制（scheduled） |
|---|---|---|
| 入队时机 | 分片一封口（每 2 秒扫） | 每 N 小时批量（1~24） |
| 数据温度 | 热，还在 page cache 里 | 冷，要真读一遍盘 |
| 与录像争磁头 | 几乎没有（读的正是刚写的区域） | 有，需低优先级让路 |

判定 `condense.analyze()` 只读 `ffprobe` 的包大小曲线（`pkt_size`），
**不解码**；阈值取「20 分位 × 倍率」，不用中位数——一段里人越多包越大，
中位数会被活动段拉高，阈值跟着漂，结果是越热闹越判不出来。
导出用 `-c copy` 切活跃区间，画质零损失；代价是切点只能落关键帧，
所以 `pre_buf` / `post_buf` 默认各给 3 秒。两种模式共用同一个消费端，
所以不会出现「切模式后画质不一样」这种坑。

> 注：0.9.104 起「缩时片段 / 物化切片」整体下线，界面收敛为
> 「全时 + 归档浓缩」两维。上表描述的是当时的设计取舍，
> 现行存储口径见 `docs/USER_GUIDE.md` §3 与 `docs/CONFIGURATION.md`。

## 3. 早期验证记录

**本机试过什么（2026-10-20，Windows Git Bash）**：
`tools/validate.py` 75/75；`test_params.py` 20/20；`test_retention.py` 11/11；
`test_link.py` 29/29；`test/dryrun.sh` 41/41（含 `/api/link` 字段、切定时模式、
间隔 0 夹到 1、分片 30 秒夹到 60、手动入队返回 200、切回即刻压制）；
打包自检：包内成员齐全、`checksum` 与 `app.tgz` 实测 md5 一致。
这些数字是当时的，现在的闸门规模见 `BUILD.md` 与 `docs/ROADMAP.md`。

**装上真机后按顺序验证**（当时的 0.3.0 形态，命令里的路径/端口已过时）：

1. 应用中心手动安装 `dist/nvr-l-0.3.0_x86.fpk`，向导里填录制目录、选衔接方式
2. 装完先在文件管理里确认录制根目录存在
3. 打开应用 → 设置 → 摄像机连接，填一路主码流 RTSP 地址
4. `curl http://127.0.0.1:18080/api/link`，看 `record.channels[0].state` 是不是 `recording`
5. 等一个分片周期，看 `counters.done` / `counters.silent` 是否增长
6. `ls <录制目录>/_clip/<cam>/<日期>/` 看有没有切出来的片段
7. `cat /var/apps/nvr-l/var/log/install.log` 看 `gpu_state=`；`var/tmp/gpu.json` 有逐项明细

现行做法见 `README.md` §3（**用 `hstate` 而不是 `state`**，
且要先带 token）——`state` 只知道进程在不在，`hstate` 才判得出「进程在但没输出」。

## 4. 当时尚未覆盖 / 后来怎么处理的

| 事项 | 当时现状 | 后续 |
|---|---|---|
| `pack_fpk.py` 出的包未经 fnpack 校验 | 布局按社区样例复刻，含 checksum | 真机试装过；`build.sh` 优先用官方 fnpack，缺失时回退 `pack_fpk.py` |
| 改录制目录需重启 | `link` / `recorder` 启动时固定 root | **仍如此**，`/api/config` 的 `restart_hint` 明说；见 `CONFIGURATION.md` 的「需重启」档 |
| 预览快照与实时流 | 接口与前端已就位，抓帧 worker 未接 | 已接，`ingest` / `frames` / `snapshots` 模块 |
| 判定的绝对地板 `8192 B/s` | 按 4 Mbps 主码流估的 | 归档改按源码率设 VBV 上限（0.9.115） |
| ES 索引 / 缩略图雪碧图 | 未做 | 事件索引改用 SQLite 标准库（`event_index.py`），雪碧图未做 |
| iframe vs url 入口 | 用的 iframe | 已改 `url`（0.4.1） |
| ffmpeg 静态二进制 | 未打包 | 仍靠系统 ffmpeg；`app/bin/README.md` 说明可选放入 |

**仍属「不在本轮范围」的**（见 `docs/ROADMAP.md`）：
人脸 / 车牌识别、自定义模型训练平台、复杂多用户权限、过多实时滤镜（P3 明确不做）；
给 ffmpeg 命令行加 HTTP 观测出口、给夜间任务加「下次几点跑」接口
（清单外的接口面扩展，验收表里如实标 `partial` 并写明免验理由）。
