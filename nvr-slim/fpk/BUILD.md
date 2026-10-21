# NVR-L fpk

三合一：实时预览 / 全时录像与回放 / 缩时压缩回放。**录像由本应用自己拉主码流落盘**，
不再依赖其它 NVR 的目录；录制与压制之间支持两种衔接节奏（即刻 / 定时）。

## 0.5.0 回到 0.3.1 的标识与形态（真机对照结论）

真机对照给出了决定性线索：**0.3.1 可以安装（只是端口被占），0.4.x 一律报「应用数据包不完整」**。
把 0.3.1 → 0.4.x 的差异逐项列出来，最可疑的一处是**应用标识从 `nvr` 改成了 `nvr-l`**。

### 根因假设：appname 不允许连字符

飞牛用 `manifest.appname` 同时派生出四样东西：`/var/apps/<appname>`、`/vol1/@appcenter/<appname>`、
`/vol1/@appdata/<appname>`，以及 `config/privilege` 里声明的**应用专用 Linux 用户与组**。
0.4.x 把 appname 改成 `nvr-l` 后，用户名也是 `nvr-l`。带连字符的标识在飞牛的
标识/用户名校验里一旦不通过，安装中途就会判包无效 —— 这与「包本身能完整解开」并不矛盾，
也解释了为什么同样结构、只差一个连字符的两个包会一个能装一个不能装。

这一版把标识改回纯字母：

| 项 | 0.4.x | 0.5.0 |
|---|---|---|
| `manifest.appname` | `nvr-l` | `nvr` |
| `config/privilege` 用户/组 | `nvr-l` | `nvr` |
| `desktop_applaunchname` / `ui/config` key | `nvr-l.main` | `nvr.main` |
| 共享目录 | `nvr-l-output` | `nvr-output` |

界面显示名仍是 **NVR-L**（`display_name`），用户看到的名字不变，只有内部标识回归 0.3.1。

### 同时去掉的第二个变量：wizard/

0.3.1 的包里**没有 `wizard/`**（当时打包器漏了），0.4.x 才加进来。两个变量同时存在就无法定位，
所以这一版**向导暂不进包**：端口、录制目录、衔接方式全部走内置默认值（18081、
`/vol1/@appshare/nvr-output`、即刻压制、5 分钟分片），装完在应用设置里照常能改。

打包器加了开关 `NVR_PKG_WIZARD=1`，需要做对照实验时再打开。

### 向导自身的两处规范违反（等验证完再放回去）

查官方 wizard 文档时发现两处不合规，即使不是本次根因也必须修：

1. **`rules` 必须是数组**。`wizard/install` 里 `record_dir` 写成了对象
   `{"required":true,"message":"..."}`，其余字段都是数组。解析器遇到类型不一致的表单项，
   很容易整份向导判为无效。
2. **自定义字段统一 `wizard_` 前缀**。官方明确「字段名直接成为环境变量」，
   而我们写的是 `port`/`record_dir`/`link_mode`，`cmd/` 里却按 `wizard_port`/`wizard_record_dir` 取值 ——
   **取到的永远是空**，于是向导里改端口、改衔接方式全部无效，端口一直回落到 `manifest.service_port`。
   这就是「装了选 18081 却还是 18080」的原因。现已改为 `wizard_*`，`cmd/` 侧同时兼容不带前缀的旧写法。

`validate.py` 加了两条断言（`rules` 必须是 list、有 `field` 就必须 `wizard_` 前缀），防止再犯。

### 端口链路的三处修正

- 默认端口 **18081**（18080 在真机上被占）；
- `install_init` 里端口被占**不再阻断安装**，只写警告：启动时 `nvr_slim.py` 会向上顺延到空闲端口，
  启动成功后 `pinport` 把桌面入口对齐到真实端口。装不上比端口顺延难查得多；
- `runtime.py` 的 `set_ui_port` 现在**同时改两个位置**：
  `/var/apps/nvr/ui/config`（飞牛桌面真正读的）和 `/var/apps/nvr/target/ui/config`（app.tgz 里的副本）。
  0.4.x 只改后者，所以图标永远停在旧端口。

### 真机验证顺序

```bash
# 0. 先看 0.4.x 的残留（只查不删，确认后自行清理）
ls -d /vol1/@appcenter/nvr-l /vol1/@appdata/nvr-l /var/apps/nvr-l 2>/dev/null

# 1. 装完确认目录按新标识生成
ls -d /vol1/@appcenter/nvr /vol1/@appdata/nvr /var/apps/nvr

# 2. 端口与监听
bash /var/apps/nvr/cmd/main status; echo $?
ss -lntp | grep -E '1808[0-9]'

# 3. 一键诊断
bash /var/apps/nvr/target/tools/doctor.sh
```

## 0.4.1 修复「应用数据包不完整」

装上后打开应用提示 **「应用数据包不完整，无法继续使用。请卸载后重新安装。」**，卸载重装同样报错。
排查方式是把它和官方 `fnpack` 的产物逐项对齐，找出所有"我们和官方不一样"的地方。

### 最重要的怀疑：桌面入口里的 `${wizard_port}` 没被替换

0.4.0 把 `app/ui/config` 的 `port` 写成了 `${wizard_port}` 占位符。飞牛桌面**直接用这个值拼打开地址**，
一旦占位符没被替换，URL 就是 `http://127.0.0.1:${wizard_port}/` —— 应用必然打不开。
时间线也对得上：0.3.1 是硬编码 18080，报的是 `ERR_CONNECTION_REFUSED`（能打开、连不上）；
0.4.0 换成占位符之后才出现"数据包不完整"。而改写 `target/ui/config` 是在安装回调做的，
此时 `@appcenter` 下的应用目录可能还不归应用用户所有，写失败会被 `except OSError: return False` 静默吞掉。

改法不再是"换个地方写"，而是**从根上不依赖占位符**：

- `app/ui/config` 的 `port` 写**合法数字 18080**，任何情况下都能拼出有效地址；
- 安装回调仍按向导值改写，但改写失败会往 `install.log` 和 `$TRIM_TEMP_LOGFILE` 写明确警告；
- `cmd/main` 启动成功后补一次对齐（`runtime.py pinport`）——启动阶段目录通常已归属应用用户；
- `validate.py` 断言端口必须是数字，占位符残留直接判失败。

### 包结构：三处与官方 `fnpack` 不一致

官方 `build-fpk.sh` 的最后一步是 `tar -czf "$FPK_NAME" *`，从这条命令能反推出产物形态：

| 项 | 官方 | 我们（0.4.0 及更早） | 影响 |
|---|---|---|---|
| 成员名 | `manifest` | `./manifest` | 严格路径匹配会一个都命中不了 |
| 目录条目 | 有 `cmd/`、`config/`、`ui/` | 只加了文件，无目录条目 | 按目录遍历的校验会认为缺目录 |
| 包根 `ui/` | 只有 `config` + `images/` | 多塞了一份 `ui/index.html`（110 KB） | 与 `app.tgz` 内的同名文件重复 |

三处已全部对齐。`manifest` 也改成官方的 `key            = value`（等号第 16 列），
解析方用 `awk -F'=' + tr -d ' '` 取值，两种写法都能读，但对齐后和官方产物逐字一致。

### 其它

- 桌面入口 `type` 从 `iframe` 改为 `url`。端口服务类应用必须用 `url`：飞牛桌面是 https，
  内嵌 http 的 iframe 会被浏览器按混合内容拦截。
- 删除 `nvr-l.sc`（端口转发声明）。端口是安装时用户自选的，写死一个端口自相矛盾；
  且 `.sc` 的段格式一旦不被识别，就是白白多一个安装失败点。服务监听 `0.0.0.0`，不需要转发。
- 新增 `app/tools/doctor.sh`（进包，安装后位于 `target/tools/doctor.sh`）：一次性打印应用目录软链、
  **program 文件完整性**、manifest 关键字段、checksum 比对、`ui/config` 端口是否合法、
  进程/监听/健康检查、日志尾部。装不上时先跑它，输出直接贴回来即可。

### 打包器自身的 bug

`tools/pack_fpk.py` 的排除逻辑拿**绝对路径**判断，而暂存目录叫 `.packtmp` 恰在排除名单里，
导致 `cmd/*` 全部被当成"要排除的文件"扔掉、自检报缺成员。改为只对相对路径判断，
暂存目录同时改名 `build_stage`。

校验：结构 71/71、参数 20/20、保留 11/11、衔接 29/29、端口 13/13、干跑 43/43。

### 如果 0.4.1 还报同样的错

在真机上用官方工具出包对照（这是唯一 100% 正确的路径）：

```bash
sudo bash tools/build_on_fnos.sh
```

它会下载官方 `fnpack`、按 `fnpack` 要求的输入目录组装、出包，并打印产物里的成员数、
是否带 `./` 前缀、checksum 是否与 `app.tgz` 一致。然后跑：

```bash
sudo bash /var/apps/nvr-l/target/tools/doctor.sh
```

## 0.4.0 端口可配置 + 两个真 bug

### 端口可配置（用户诉求：装了启不来，提示端口被占用）

- 安装向导第 1 步就是「Web 服务端口」，默认 18080；填的数字写入 `config.json` 的 `service_port`。
- `app/ui/config` 的 `port` 改为 `${wizard_port}`（官方文档指定的向导占位符），安装回调再把它固化成字面量 ——
  这样「进程实际监听端口」和「桌面图标指向端口」永远同一个数字。
- `manifest` 设 `checkport=false`：端口检查改由我们自己做，报错文案才能说清「改成 18081 重装」。
- `install_init` 在装前真实 bind 一次：被占就直接失败并提示换端口；若占用者是本应用旧实例则放行（装完接管）。
- 运行期兜底：`cmd/main` 读 `config.json` 的端口；若 bind 仍失败，服务自动向上找空闲端口（最多 +40），
  把实际值写回 `config.json`、`app/ui/config` 和 `var/port.txt`，健康检查读 `port.txt` 所以不会误判。
- `应用设置` 里改端口：`config_callback` 同时改配置与入口；端口变了会顺手停掉旧进程并提示重启。

### health.json 移除

它按 manifest 的**固定端口**探活，用户一旦改端口就会被判成"未运行"。启动等待本来就由 `cmd/main`
的 `wait_health` 负责，改为读 `var/port.txt` 的真实端口，两件事合成一件。

### 两个真 bug（都影响真机安装）

1. **`wizard/` 从来没打进包**。`tools/pack_fpk.py` 只复制了 `cmd/ config/ ui/`，漏了 `wizard/`，
   所以前几个版本安装时**根本没有向导**——端口、录制目录、衔接方式全都没让用户选。现已纳入并在自检里断言。
2. **干跑测试会改源码树**。`TRIM_APPDEST` 指向真实 `app/`，安装回调把 `app/ui/config` 的
   `${wizard_port}` 改成了测试端口 18099。改为先 `cp -r` 到临时目录再跑。

校验：结构 73/73、参数 20/20、保留 11/11、衔接 29/29、端口 13/13、干跑 43/43（新增 2 条端口断言）。

## 0.3.1 修复：局域网直连打不开

`ERR_CONNECTION_REFUSED` on `http://<NAS-IP>:18080` 的根因是服务只 bind 了 `127.0.0.1`——
飞牛桌面入口经网关转发到 loopback 是通的，但从手机/电脑直连 NAS 的 IP 必然被拒。

- `nvr_slim.py` 新增 `--host`（默认取 `TRIM_LISTEN_HOST`，回落 `0.0.0.0`）；
- `cmd/main` 启动时显式传 `--host "${TRIM_LISTEN_HOST:-0.0.0.0}"`；
- 端口被占时不再抛 traceback，直接给出 `ss -lntp` 排查提示并以 1 退出；
- `/api/status` 增加 `listen` 字段，启动首行日志改成 `listen=0.0.0.0:18080`；
- 设置页「诊断与日志」补一行直连地址说明。

排查顺序：`ss -lntp | grep 18080` 看监听地址 → `tail -30 /vol1/@appdata/nvr-l/log/app.log` 看是否起不来 → `bash /var/apps/nvr-l/cmd/main status; echo $?`（0=运行，3=停止）。

```
fpk/
├── manifest               appname=nvr-l / version=0.4.1 / platform=x86 / service_port=18080(默认) / checkport=false
├── ICON.PNG / ICON_256.PNG   摄像头简图（飞牛官方配色：#4778FF→#3D5DFF 渐变 + 白色枪机）
├── (无 health.json)       探活改由 cmd/main 读 var/port.txt 的实际端口，不绑固定端口
├── nvr-l.sc               防火墙端口声明：18080/tcp
├── cmd/                   9 个生命周期脚本（fnOS 约定：status 0=运行 3=停止）
│   ├── main               start/stop/status，启动后等健康检查通过才算成功
│   ├── install_init       装前检查 python3；**只探测**已存在的数据目录是否可写
│   ├── install_callback   建目录 + 修属主/ACL、写初始配置、核显自检、落向导里的衔接方式
│   ├── upgrade_init       停服务 + 备份 config/queue/db（保留最近 3 份）
│   ├── upgrade_callback   合并新版默认值，保留用户配置，属主不对才修
│   ├── uninstall_init     停服务
│   ├── uninstall_callback 清 tmp；录制与片段不动
│   ├── config_init        配置损坏时先修复
│   └── config_callback    把向导字段写回 config.json
├── config/
│   ├── privilege          run-as: package，用户/组 = nvr-l
│   └── resource           data-share nvr-l-output（rw 给 nvr-l）
├── wizard/                install 4 步（端口/录制目录/衔接方式）/ config 3 步；**必须进包**
├── app/
│   ├── ui/                index.html（手机原型，明暗跟随飞牛）+ config（iframe 入口 nvr-l.main）
│   ├── server/
│   │   ├── nvr_slim.py    HTTP 服务、路由、线程编排
│   │   ├── runtime.py     配置（带 mtime 缓存）、核显自检、槽位池、工具探测
│   │   ├── params.py      判读与编码参数体系
│   │   ├── ffmpeg_cmd.py  ffmpeg/ffprobe 命令构建
│   │   ├── recorder.py    拉流录制：分片落盘、断流退避重连、跨天换目录
│   │   ├── condense.py    判定（只读包大小）+ 导出（流复制切段 + concat）
│   │   ├── link.py        衔接调度：两种入队节奏共用同一套消费端
│   │   └── retention.py   每日定点清理
│   └── bin/               放静态 ffmpeg（可选，见 README.md）
├── tools/
│   ├── validate.py        75 项结构/语法校验（打包前必跑）
│   ├── gen_icons.py       纯标准库生成四件图标
│   └── pack_fpk.py        无 fnpack 的 fpk 打包器（checksum=md5(app.tgz) 自动回填）
├── dist/                  打包产物 nvr-l-0.3.0_x86.fpk
└── test/
    ├── dryrun.sh          生命周期干跑（41 项断言，含 /api/link）
    ├── test_params.py     参数体系/命令构建/快照接口（20 项）
    ├── test_retention.py  保留周期与清理边界（11 项）
    └── test_link.py       录制封口判定/包大小判定/幂等键/两种衔接节奏（29 项）
```

## 打包

```bash
bash build.sh            # 结构 → 参数 → 保留 → 衔接 → 打包（无 fnpack 时用 pack_fpk.py）
bash build.sh --check    # 只校验不打包
```

fpk 布局：包根 tar.gz，成员 `manifest`（含 `checksum=md5(app.tgz)`）、
`app.tgz`（app/ 内容在根）、`cmd/`、`config/`、`ui/`（app/ui 副本）、
`ICON.PNG`、`ICON_256.PNG`、`nvr-l.sc`，以及 `wizard/`。

## 0.3.0 的三件事

### 1. 修了「数据目录不可写：/vol1/@appdata/nvr」

**根因不是软件名**，是 `install_init` 越权建目录。这个钩子在安装流程最前面执行，
此时 `/vol1/@appdata/` 属 root、应用用户还没就绪，`mkdir -p` 必然失败，
正好落在 `fail "数据目录不可写：$VAR_DIR"` 这行。

改法：`install_init` 只做探针（目录存在才测写，不存在交给系统建）；
`install_callback` 在建完目录后按需 `chown -R` + `chmod 700` + `setfacl -b`
（ACL 的 mask 被压成 `---` 时，属主权限再对也写不进去）；
`cmd/main` 启动前再探一次写，把报错点前移到最容易排查的位置。

改名 NVR-L（`appname=nvr-l`）是顺带的：换一个干净的目录名，
顺带绕开旧版本可能留下的坏属主目录。

### 2. 录制由本应用自己做

`recorder.py` 用 `-c copy` 转封装落盘，一路 4MP 只占约 3% 单核；分片默认 5 分钟
（可配 60~900 秒），按 `日期/HH-MM-SS.mp4` 命名。

只消费**已封口**的分片：segment muxer 顺序写，看到下一个分片出现就说明上一个写完了。
这条规则比 mtime 稳，也比 inotify 简单；正在写的那个分片 moov 还没落地，读它就是坏文件。

### 3. 两种衔接节奏，共用一套判定与导出

| | 即刻压制（live） | 定时压制（scheduled） |
|---|---|---|
| 入队时机 | 分片一封口（每 2 秒扫） | 每 N 小时批量（1~24） |
| 数据温度 | 热，还在 page cache 里 | 冷，要真读一遍盘 |
| 额外读盘 | 几乎为零 | 每 N 小时一次 |
| 与录像争磁头 | 几乎没有（读的正是刚写的区域） | 有，需低优先级让路 |
| 适合 | 想尽快在回放里看到缩时片段 | 白天不想让磁盘和 CPU 有任何额外动作 |

判定 `condense.analyze()` 只读 `ffprobe` 的包大小曲线（`pkt_size`），**不解码**；
阈值取「20 分位 × 倍率」，不用中位数 —— 一段里人越多包越大，中位数会被活动段拉高，
阈值跟着漂，结果是越热闹越判不出来。
导出用 `-c copy` 切活跃区间，画质零损失；代价是切点只能落关键帧，
所以 `pre_buf`/`post_buf` 默认各给 3 秒。

两种模式共用同一个消费端，所以不会出现「切模式后画质不一样」这种坑。

## 本机试过什么（2026-10-20，Windows Git Bash）

- `tools/validate.py`：75/75
- `test/test_params.py`：20/20；`test_retention.py`：11/11；`test_link.py`：29/29
- `test/dryrun.sh`：41/41。含 3.7 节：`/api/link` 字段、切定时模式、间隔 0 夹到 1
  （不是悄悄回落默认值）、分片 30 秒夹到 60、手动入队返回 200、切回即刻压制
- 打包自检：包内成员齐全、`checksum` 与 `app.tgz` 实测 md5 一致

## 装上真机后按顺序验证

1. 应用中心手动安装 `dist/nvr-l-0.3.0_x86.fpk`，向导里填录制目录、选衔接方式
2. 装完先在文件管理里确认录制根目录存在（默认 `/vol1/@appshare/nvr-l-output`）
3. 打开应用 → 设置 → 摄像机连接，填一路主码流 RTSP 地址
4. `curl http://127.0.0.1:18080/api/link`，看 `record.channels[0].state` 是不是 `recording`
5. 等一个分片周期（默认 5 分钟 + 1 分钟余量），`curl /api/link` 看 `counters.done`
   或 `counters.silent` 是否增长，`last_item` 里有区间数与耗时
6. `ls <录制目录>/_clip/<cam>/<日期>/` 看有没有切出来的片段
7. `cat /var/apps/nvr-l/var/log/install.log` 看 `gpu_state=`；`var/tmp/gpu.json` 有逐项明细

## 尚未覆盖 / 需要在真机确认

| 事项 | 现状 | 计划 |
|---|---|---|
| pack_fpk.py 出的包未经 fnpack 校验 | 布局按社区验证样例复刻，含 checksum | 真机试装；报结构错误就用官方 `fnpack build` 对比 |
| 改录制目录需重启 | `link`/`recorder` 启动时固定 root | 后续做运行时重建（先停录制→迁移→重启） |
| 预览快照与实时流 | 接口与前端已就位，抓帧 worker 未接 | 接一路子码流取帧，复用 `snapshot_path` |
| 判定的绝对地板 `8192 B/s` | 按 4 Mbps 主码流估的 | 真机拿实际片段校准，写进预设 |
| ES 索引 / 缩略图雪碧图 | 未做 | 与回放页对齐后统一做 |
| iframe vs url 入口 | 用的 iframe | 飞牛 App 内嵌有问题就改 `url` 一行 |
| ffmpeg 静态二进制 | 未打包 | 确认带 qsv 的构建后放入，包体 +80MB |
