# NVR-L

> 装在飞牛 NAS 上的轻量 NVR：实时预览 / 全时录像 / 事件中心 / 归档浓缩回放。
> **录像由本应用自己拉摄像头主码流分段落盘**（`-c copy` 转封装，不解码），
> 不依赖其它 NVR 的目录。当前版本 **0.9.140**。

这份 README 是给「第一次拿到这个仓库 / 第一次装这个应用」的人看的：
照着第 2、3 节走一遍，能在 10 分钟内装起来并确认**真的在录**。
方向锚点在 [`docs/REVIEW_BASELINE.md`](docs/REVIEW_BASELINE.md)——评审一句话结论是
「最缺的不是功能，是把已有功能整合成可信、能无人值守运行的产品」，
所以这份文档里每个状态数字都标了出处，不写「看起来对」的估值。

## 1. 它长什么样

页面是**单个 `index.html`**（构建时由 `css/` + `js/` 合成，设备上是单文件，
无外链、首屏不闪白）。底部 5 个 tab，没有独立的「诊断」页——诊断是设置页里的
一个分区：

| tab | `data-page` | 用来做什么 |
|---|---|---|
| 直播 | `live` | 看各路实时画面与快照墙 |
| 回放 | `play` | 按时间找录像，两轨时间轴对比全时与归档 |
| 事件 | `events` | 按事情找录像：日期 / 摄像头 / 类别筛选，卡片带确认那一刻的快照（缩略图可点开看大图、图上叠当时检测到的框），点一条直接跳到那一刻 |
| 文件 | `list` | 按日期看盘上真实录像文件，切换「全时 / 归档浓缩」；右上角可按时间段**批量导出** |
| 设置 | `cam` | 摄像头、录制、检测、外观；**诊断与日志**也在这里（存储卡带**空间预警**：会不会写满、这块盘按现在的写入速度最多留几天） |

> 出处：`app/ui/index.html` 的 `#tabbar`（约 1780 行）；页面切换在
> `app/ui/js/components/nav.js` 的 `activatePage()`。

界面截图暂未随仓库提供（包里只有 `ICON.PNG` / `ICON_256.PNG` 两枚图标）。
各页控件与状态文案逐项写在 [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)。

## 2. 装到飞牛上

产物是 `nvr-slim/fpk/dist/nvr-0.9.140_x86.fpk`（由 `bash nvr-slim/fpk/build.sh` 生成，
不入库；约 100 MB）。**在飞牛应用中心选择这个 fpk 手动安装**。

| 项 | 值 | 出处 |
|---|---|---|
| 应用标识 | `appname=nvr`（纯字母，不带连字符） | `manifest:1` |
| 界面名 | NVR-L | `manifest:3` |
| 默认端口 | **18080**，被占时自动向上顺延，最多 +40 | `manifest:9`、`nvr_slim.py:3803` |
| 真实端口 | `/vol1/@appdata/nvr/port.txt`（**只认这个文件**，别按默认端口猜） | `runtime.py:498` |
| 程序目录 | `/vol1/@appcenter/nvr` | `config/privilege` |
| 数据目录 | `/vol1/@appdata/nvr`（配置 / 台账 / 日志 / 凭证） | `cmd/main:9` |
| 录像目录 | `/vol1/@appshare/nvr-output` | `config/resource` |
| 运行身份 | Linux 用户 `nvr:nvr`（`run-as: package`） | `config/privilege` |

启动命令由生命周期脚本拼出（`cmd/main:156`）：

```bash
cd /vol1/@appcenter/nvr/server && nohup python3 nvr_slim.py \
  --port "$PORT" --host "${TRIM_LISTEN_HOST:-0.0.0.0}" \
  --var "$VAR_DIR" --app "$APP_DIR" --ui "$APP_DIR/ui" >>"$LOG_FILE" 2>&1 &
```

**服务监听 `0.0.0.0`**，因为手机直连 NAS 的 IP 是主要用法；只绑 `127.0.0.1`
会让局域网直连报 `ERR_CONNECTION_REFUSED`（0.3.1 修过，见 CHANGELOG）。

### 第一次打开：粘贴 token

服务第一次启动在 `<var>/secret.json` 生成 `api_token`（0600 权限，
`auth.py:60`）。首页 / 静态资源 / `/api/auth/status` / `/api/auth/login` 免凭证，
**其余所有 `/api/*` 与录像下载都要凭证**：`Authorization: Bearer <token>`，
或首次粘贴后签发的 HttpOnly 会话 cookie（`nvr_sess`）。比对用 `hmac.compare_digest`。
页面首次打开会弹引导让你粘贴 token；轮换 token 会让所有旧 cookie 一起失效。

```bash
# 在 NAS 上取值（也可以从应用中心的诊断导出里看）
cat /vol1/@appdata/nvr/secret.json
```

## 3. 快速上手：确认「真的在录」

「进程在跑」不等于「录上了」——这是整个项目最重要的一条口径。
真机断流时 ffmpeg 进程还活着（`poll()` 是 `None`），只是分片不再增长，
只看进程存在就会一直显示「录像中」。所以确认分三层，每层各有出处
（`app/server/health.py:9`）：

1. **上游有没有字节** → `ingest.idle_sec`（`ingest.py:564`）
2. **分片还在不在长** → `record.channels[].last_age_sec`（`recorder.py:1567`，
   把正在写的 `.part` 也算进去，否则每 5 分钟有 4 分钟谎报断流）
3. **今天盘上覆盖了多久** → 台账 `avail_spans` 的实际空洞
   （`ledger.py:338` → `health.gaps_of()`）

照这个顺序走：

```bash
TOK=$(python3 -c 'import json;print(json.load(open("/vol1/@appdata/nvr/secret.json"))["api_token"])')
PORT=$(cat /vol1/@appdata/nvr/port.txt)

# ① 装上了、版本对、进程活着
curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:$PORT/api/health"

# ② 加摄像头：设置 → 摄像头列表，填主码流 RTSP 地址
#    编辑时密码留空 = 保留原密码；地址不会落 localStorage（0.9.120）

# ③ 看每路的真实状态：两路都该是 hstate=recording
curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:$PORT/api/link" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print([(c["id"],c["hstate"],c.get("last_age_sec")) for c in d["record"]["channels"]])'

# ④ 等一个分片周期（默认 300 秒）后，看今天有没有缺口
curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:$PORT/api/health/gaps?force=1"
```

`hstate` 有 7 种取值（`health.py:30` 的 `STATE_LABEL`）：
`off / stopped / connecting / recording / stalled / recovering / error`。
出现 `stalled` 要**点名**看是上游断流还是分片不增长，两者处置不同。
所有洞的原因文案在 `health.gap_reason()`（`health.py:161`），共 5 条。

之后：回放页按时间翻、事件页按类别筛，点事件直接跳到录像时间点（前后 30 秒
也在卡片上）；要交给别人就把文件页右上角「导出」打成 zip（分片原样、不重编码，
边界多给多少写在包里）；想省空间就到「事件侦测与归档」卡配置归档，
总回溯天数与全时/归档的划分见 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。

**装不上 / 打开是空白 / 某一路卡住**：先跑包内的
`bash /var/apps/nvr/target/tools/doctor.sh`（只读，不改文件），
再看 [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)。

## 4. 构建与测试

```bash
cd nvr-slim/fpk
bash build.sh --check    # 前端合成 → 81 项结构校验 → 全部后端单测 → 前端断言（不打包）
bash build.sh            # 上面全过之后才出 dist/nvr-<版本>_x86.fpk
```

`--check` 是唯一闸门：改了配置键没同步 `docs/CONFIGURATION.md` 会当场红，
改了前端源没合成也会红（合成刻意排在结构校验之前）。
完整目录树、测试表与打包布局在 [`nvr-slim/fpk/BUILD.md`](nvr-slim/fpk/BUILD.md)。

测试平台 `192.168.2.152` 允许自动部署（`tools/deploy_test.py`，九项接口自检，
失败自动回滚）；**生产机 `192.168.2.6` 一律手动升级**，自动任务不碰它——
每次重启会让录制断 5~15 秒，而缺口检测会把这段记成真缺口。

## 5. 文档索引

| 文档 | 回答什么问题 |
|---|---|
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | 每个页面、每张卡、每种状态该怎么读、怎么操作 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 模块分层、全部 HTTP 路由、数据流与状态汇总口径 |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | 每个配置键的类型 / 默认值 / 范围 / **改完谁重新读** |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | 六类常见故障的真实排查命令与接口 |
| [`nvr-slim/fpk/CHANGELOG.md`](nvr-slim/fpk/CHANGELOG.md) | 版本沿革与早期真机排障记录（从 BUILD.md 搬出） |
| [`nvr-slim/fpk/BUILD.md`](nvr-slim/fpk/BUILD.md) | 只讲构建、目录树与测试 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 整改路线图的进度表、轮次台账与部署规则 |
| [`docs/REVIEW_BASELINE.md`](docs/REVIEW_BASELINE.md) | 外部评审的方向锚点（P0 / P1 / P2 / P3） |

文档与代码的「不漂移」由 `test/test_config_doc.py`（配置键）与
`test/test_doc_suite.py`（页面 / 路由 / 模块 / 版本）在闸门里钉住：
**真相始终在代码里，测试 import `runtime` 或解析源码，不把数字再抄一份进测试。**
