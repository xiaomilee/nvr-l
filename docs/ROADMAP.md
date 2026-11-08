# NVR-L 整改路线图

来源：外部技术评审（产品/架构/代码三视角）逐条拆解，按「风险 × 用户价值」排序，
拆成可独立验收、可独立回滚的版本批次。每批一个版本号。
**方向锚点见 `REVIEW_BASELINE.md`——每轮开工前先读它，再取活。**

## 总原则

1. **不推倒重写。** 录制链路（RTSP 断流、MP4 封口、进程收尾、统一取流）是真机经验，
   只加不改；新能力以新模块挂接。
2. **数字必须有出处。** 界面上任何状态/计数都要能追到真实数据源，
   不允许「看起来对」的估值（评审原文："监控系统不能给用户展示看起来正确、实际却没有验证过的数据"）。
3. **失败不等于没有。** 取帧失败、模型加载失败、断流都记「未知」，绝不写成「无活动」。
4. **升级不丢数据。** 配置、台账、录像、日志的旧格式必须能被新版本读。
5. **跑起来才算完成。** 单测全绿只是入场券；能部署到测试平台、接口自检通过、
   真实两路信号在录，才叫这一批做完。

## 进度表（自动化任务读这段决定下一步）

| 批次 | 版本 | 主题 | 状态 | 提交 |
|----|----|----|----|----|
| 1 | 0.9.118 | 录像可信度：真实状态 + 缺口报告 | done | 64aa8ee |
| 1.5 | 0.9.119 | 实测修复：状态原因不自相矛盾 + 测试机部署工具 | done | 8fbe09f |
| 2 | 0.9.120 | 安全：API Token / 凭据收敛 / 前端统一请求层 | done | c113ff3 |
| 3 | 0.9.121 | 事件中心：统一事件模型 + 跨摄像头检索 + 点击定位 | todo | - |
| 4 | 0.9.122 | 检测治理：有界并发 + 时间窗确认 + 诊断完整 | todo | - |
| 5 | 0.9.123 | 前端拆分 + 文档重构 + 按钮验收自动化 | todo | - |

状态取值：`todo` / `doing` / `done` / `blocked`。`blocked` 必须在批次小节里写原因。

## 轮次台账（自动任务每轮结束追加一行）

节奏：每小时一轮（调度器最小间隔 1 小时，30 分钟做不到，已实测确认）。
**不设轮次上限**——只要还有 `todo` / `doing` 的批次，就一轮接一轮往下推，
直到全部 `done` 或 `blocked` 才停。用户在 11-08 明确要求：「在定时任务允许的范围内尽快迭代更新测试」。

每轮的动作固定为六步：
`读 REVIEW_BASELINE 对照方向` → `取 1~4 个清单项` → `写代码 + 测试` → `build.sh --check 全绿`
→ `bump 版本 + 打包` → `部署到测试平台自检` → `提交 + 回填台账`。

一轮做不完一批很正常，留 `doing` 下一轮接着做；但**每轮必须有一个可提交的完整切片**，
不许留"改到一半构建是红的"状态过夜。批次全部清单项打勾、且「验收」条件在测试平台上真实跑过，
才标 `done`。

| 轮 | 时间 | 做了哪个批次 / 切片 | 提交 | 结果 |
|----|----|----|----|----|
| 1 | 11-08 02:30 | 批次 1 全量（health.py / status 语义 / 缺口接口 / 诊断卡） | 64aa8ee | done，0.9.118 已打包 |
| 2 | 11-08 03:42 | 打通测试平台部署链路（`tools/deploy_test.py`）、两路 RTSP 接入、修「上游 0 秒没数据」文案 | 8fbe09f | 0.9.119 部署 152 自检六项全绿，两路 recording |
| 3 | 11-08 04:57 | 批次 2 切片一：安装级 token（`auth.py`）、统一鉴权闸、登录/cookie/轮换接口、页面粘贴引导；顺手修「界面谎报 stalled」（`status()` 与看门狗同源） | bb1034e / 010c162 | 未出包（批次未收尾，按规则不 bump）；152 只读出「两路实际在写、界面报 stalled」并已修 |
| 4 | 11-08 06:35 | 批次 2 切片二：明文凭据不再落 localStorage（实测 `saveCfg` 原来整份序列化 camParams）、「清除地址」改显式按钮；新增 `redact.py` 统一脱敏（日志/诊断/试录 stderr），token 在 `auth.token()` 入口登记 | ea066ee | 未出包（批次只剩第 6/8 项）；后端 828 条 + 前端 cred 12 项全绿 |
| 5 | 11-08 08:06 | 批次 2 收尾：前端 18 处裸 fetch 收进 `API.get/post`（超时/401/JSON/CSRF 四处只写一遍），修「测试通知」永远 403；部署工具补同步 `/var/apps/nvr/cmd`（旧 main 把 `/api/health` 的 401 当失败，导致"装成功了却判超时还回滚"）+ `wait_health` 重读 port.txt | c113ff3 | 0.9.120 出包；152 部署七项全绿（含无凭证必须 401），两路 `hstate=recording` |

## 测试平台（可以随便折腾，与生产隔离）

| 项 | 值 |
|---|---|
| 主机 | `192.168.2.152`（飞牛 FN-V，Linux 6.18） |
| SSH | `admin` / `admin`，`sudo` 密码同为 `admin` |
| 应用端口 | 声明 **18083**，实际会顺延（真实端口只认 `/vol1/@appdata/nvr/port.txt`；11-08 第 5 轮在 **18085**） |
| 程序目录 | `/vol1/@appcenter/nvr` |
| 数据目录 | `/vol1/@appdata/nvr`（配置 / 台账 / 日志） |
| 录像目录 | `/vol1/@appshare/nvr-output` |
| 启停 | `sudo bash /tmp/nvr-ctl-deploy.sh start|stop`（部署脚本会自动写入） |

测试信号（两路，均已验证 1080p H.264 可拉流）：

- A：`rtsp://admin:ADNVGC@192.168.2.137:554/Streaming/Channels/101`
- B：`rtsp://833:376@192.168.2.116:8554/ch1`

部署一条命令：

```bash
cd nvr-slim/fpk && python tools/deploy_test.py --report ../../test-reports/deploy-<ver>.json
```

它做六件事：传包解包 → 备份程序目录 → 停服务（确认进程真的没了）→ 覆盖（**含生命周期脚本
`/var/apps/<app>/cmd`，真升级也会替换它**）→ 以 nvr 身份启动 → 等版本号上线 → 跑六项接口自检
＋ 一项「无凭证必须 401」（这项要求的是"被拦住"，判定口径与其余相反）。任一步失败自动回滚到备份并重启。

**踩过的坑（第 5 轮）**：只覆盖程序目录不够。fnOS 把 `cmd/main` 装在 `/var/apps/nvr/cmd`，
"起没起来"由那份文件拿 `/api/health` 判；0.9.120 起这个接口回 401，2023 年装的那份 main 把
401 当失败 —— 进程明明在录，工具却判超时、收进程、回滚，回滚走同一段判定又失败。
**只校验程序行为，不校验 fpk 打包正确性**（manifest/privilege/wizard 仍要靠手动安装暴露）。

## 部署规则

**生产机 `192.168.2.6` 一律手动**：自动任务不 ssh、不 scp、不调升级接口、不重启它的服务。
原因是它的 SSH 没有免密凭证，且每次重启会让录制断 5~15 秒，而缺口检测会把这段记成真缺口。
出包后由人在飞牛应用中心升级，汇报里给出包名与「装完该看什么」。

**测试机 `192.168.2.152` 允许自动部署**：每轮出包后部署 + 自检，失败自动回滚。
这正是测试机存在的意义——把"装上去会不会炸"从人工环节挪到每轮自动完成。


---

## 批次 1 · 0.9.118 录像可信度

**目标**：回答「今天到底录上了没有」。进程活着 ≠ 录像正常，摄像头在线 ≠ 录像在写。

清单：

- [x] `recorder.status()` 每路状态细化成状态机：`stopped / connecting / recording / stalled / recovering / error`，
      并补 `last_packet_age_sec`（最近收包至今秒数）、`recovery_sec`（上次恢复耗时）、`reconnect_count`。
- [x] 新模块 `app/server/health.py`：
      - `gaps_of(day, cam)`：以 `ledger.avail_spans` 为基础，取区间之间 ≥ `GAP_MIN_SEC`（默认 60s）的空洞；
      - 每段缺口给 `start / end / duration / reason / during_recording`（断的那一刻录制进程是否在跑）；
      - `day_summary(day)`：每路「有录像时长 / 缺口时长 / 缺口段数 / 最大缺口」，全路合计。
- [x] `/api/health?date=YYYY-MM-DD`（GET）：返回上面两份数据，走 3 秒 TTL 缓存。
- [x] `status_payload()["cams"]` 改为真实语义：`total / enabled / connected / recording / stalled / error`
      —— `recording` 必须是「进程存在 且 最近分片在增长」，不再是配置数。
- [x] 前端：摄像头卡片显示细分状态徽标；诊断页新增「录像缺口」列表（近 7 天，可展开到段）。
- [x] 测试：`test_health.py`（缺口算法 8+ 条）、`test_status_semantics.py`（计数口径 5+ 条）、
      `test/ui/health.test.js`（徽标与缺口渲染）。
- [x] 版本 0.9.118（五处：runtime.VERSION / manifest / 两个测试字面量与正则 / 断言）。

**验收**：制造一次 90 秒断流（停 ingest 或拔线），缺口页在 5 分钟内出现该段，
且标注「录制进程在跑」；`/api/status` 的 `cams.recording` 在断流期间减 1。

---

## 批次 1.5 · 0.9.119 实测修复与测试平台

**目标**：让「改完到底能不能跑」这件事在每轮自动回答，而不是攒到人工安装时才知道。

清单：

- [x] `tools/deploy_test.py`：传包 → 备份 → 确认停进程 → 覆盖 → 以 nvr 身份启动
      → 等版本上线 → 六项接口自检；任一步失败自动回滚并重启。
      踩过的五个坑都写在注释里：CRLF 的 `set -u`、pgrep 自匹配、端口顺延、
      sudo 起成 root、**固定 mtime 让 rsync 判定"文件没变"从而根本不覆盖**（最后这个最凶：
      部署报成功、跑的还是旧版本，因为两次 `runtime.py` 只差版本字符串、字节数完全一样）。
      覆盖后增加一次「grep 版本号」硬校验，不再信任 rsync 的判等。
- [x] 测试平台 `192.168.2.152` 接两路指定 RTSP，确认 `hstate=recording` 且分片落盘。
- [x] `health.reason_of` 修自相矛盾：上游畅通时不再输出「上游 0 秒没数据」，
      只有真的超过 `STALL_SEC` 才提上游；补 3 条测试锁住这三种情形。
- [x] 版本 0.9.119。

**验收**：`python tools/deploy_test.py` 在测试机上一条命令跑完，六项自检全绿；
人为把包改坏（或改错端口）能被校验拦下并自动回滚到原版本。

---

## 批次 2 · 0.9.120 安全

**目标**：把「能连上局域网就能操作」变成「有凭证才能操作」，同时不破坏手机直连与 fnOS 桌面入口。

清单：

- [x] `var/secret.json`：首次启动生成 `api_token`（`secrets.token_urlsafe(32)`，0600 权限），
      另存 `created_at`；支持 `POST /api/auth/rotate` 轮换。
- [x] 统一鉴权：除 `/`、`/index.html`、静态资源、`/api/auth/*` 外，所有 `/api/*` 与录像/导出下载
      要求 `Authorization: Bearer <token>`，用 `hmac.compare_digest` 比对；保留 `X-NVR-Request`（CSRF 是另一层）。
- [x] 首次访问 `/api/auth/status` 返回 `{need_token: true}`，页面弹出「粘贴 token」引导，存 localStorage；
      401 时自动回到引导。
      *（实现补充：`<img>/<video>` 带不了请求头，登录时另签一枚 HttpOnly 会话 cookie —— 签名密钥就是
      token 本身、服务端无状态，主 token 因此永远不进 URL。轮换 token 即让所有旧 cookie 失效。）*
- [x] RTSP 凭据收敛：GET 配置一律脱敏；编辑时密码框留空 = 保留原密码（服务端合并）；前端不再把明文写 localStorage。
      *（第 4 轮收尾：脱敏与"留空=保留"早就在，缺的是"明文真的没落盘" —— 实测 `saveCfg`
      把整份 camParams 序列化，`rtsp://user:pass@ip` 就永久留在了那台设备的 localStorage 里。
      已改为地址字段不落盘 + 读到旧值时清空；"清掉地址"改由编辑页的「清除 NAS 上已保存的地址」
      按钮显式打标，不再从"输入框是空的"推断——后者在明文不落盘之后会把每次保存都变成删地址。）*
- [x] 日志/诊断统一脱敏（token、RTSP 密码、URL query）。
      *（新模块 `app/server/redact.py`：rtsp/http(s)/ftps 的 user:pass、敏感 query 参数、
      `Authorization: Bearer`、`nvr_sess` cookie、以及 api_token 字面量；`_record` /
      `_record_access` / 诊断导出 / ffprobe 与试录 stderr 全部走这一处。
      token 在 `auth.token()` 这唯一取值口登记，任何调用方都不会漏。）*
- [x] 前端统一请求层 `API.get/post`（超时 + 401 + JSON + CSRF 头），替掉散落的 `fetch`。
      *（18 处裸 fetch 全收进 `API.req`：AbortController 超时（默认 15 秒，试录 90 / 像素自检 60 /
      归档预览 120 / 存储重算 300 秒单独放宽）、401 归引导层、非 JSON 必须抛 `ApiError`、
      写操作统一带 `X-NVR-Request`、`_t` 统一加。登录接口用 `allow401` 例外：那里的 401 是
      "你粘错了 token"，不该被改写成"凭证已失效"。顺带修掉「测试通知」永远 403 —— 它自己拼头时
      漏了 X-NVR-Request，好地址也只回"发送失败"（152 上实测：带头 200、不带头 403）。
      新增 `test/ui/api.test.js` 31 条，其中一条是结构守卫：剥掉注释后全页只允许剩传输层那一处
      `fetch(` —— 这个 bug 的复发形态就是"某人顺手又写了一个 fetch"。）*

> **本轮顺带修的真问题（部署工具，比前端更凶）**：152 上第一次装 0.9.120 直接判"启动失败"并回滚，
> 回滚也失败。查下来不是产品问题：fnOS 把生命周期脚本装在 `/var/apps/nvr/cmd`，而部署工具只覆盖
> `/vol1/@appcenter/nvr`（程序目录）；"起没起来"由那份 **2023 年装的 main** 拿 `/api/health` 判，
> 而 0.9.120 起这个接口要凭证、回 401 —— 旧 main 把 401 当失败。于是进程明明在录，工具判超时、
> 收掉它、再回滚、回滚走同一段判定、又失败。真升级会整体替换 `cmd/`，所以工具现在也一起同步
> （含备份与回滚，并用 md5 核一遍换进去的确实是包里那份）。`cmd/main` 的 `wait_health` 另有两处
> 老毛病一并修：`port.txt` 只读一次会拿上一轮的旧端口死探 30 秒、以及只探一个端口。
> `test/test_wait_health.py` 5 条把「端口顺延 / 401 算起来了 / 文件被改写要重读」钉住。
- [x] 测试：`test_auth.py`（401/放行/常量时间/白名单/轮换 12+ 条）、`test/ui/auth.test.js`。
      *实际 38 条 + 11 条；另 `test_http_smoke.py` 补 4 条 HTTP 级断言（本地 52 项全绿）。*
- [x] 版本 0.9.120（五处 + 打包 + 部署 152 自检）。

> 批次 2 收尾（11-08 第 5 轮）：清单全部打勾，`0.9.120` 已出包并部署到 152，
> 六项接口自检 + 「凭证设防（无凭证必须 401）」全绿，两路 `hstate=recording`。
> 包：`dist/nvr-0.9.120_x86.fpk`（101120063 字节，md5 `9752d28be86bb91b593568c3e0948910`）。
> 152 上端口顺延到 **18085**（18084 被上一轮的残留进程占着，部署工具按 port.txt 走）。

> **本轮顺带修的真问题（批次 1 的回归，记在这里以免漏账）**：152 上只读检查发现两路
> `hstate=stalled`，而磁盘上 `.part` 一直在写。原因是 `recorder.status()` 的
> `last_age_sec` 只数**已封口**的 `.mp4`，分片 300 秒封口一次、判 stalled 阈值 60 秒 ——
> 每 5 分钟里有 4 分钟在喊断流，真断流反而分不出来；而看门狗用的是 `_newest_age`
> （含 `.part`），于是「界面说断了、进程说没事」。现在两边同源，`test_status_age.py`
> 7 条钉住它，并保证「今天真的一片都没录」仍然报 stalled。生产机若已装 0.9.118/0.9.119，
> 大概率也在谎报 —— 随 0.9.120 一起修掉。

**验收**：`curl http://nas:port/api/status` 无 token 得 401；带正确 token 得 200；
手机浏览器首次打开出现引导，填 token 后一切功能正常；旧配置升级后摄像头密码不丢。

---

## 批次 3 · 0.9.121 事件中心

**目标**：从「翻录像」变成「点事件直接看」。

清单：

- [ ] 新模块 `app/server/event.py`：统一事件生命周期
      `DETECTED → CANDIDATE → CONFIRMED → ACTIVE → ENDED → INDEXED`，
      `event_id` 稳定幂等（`cam_YYYYMMDD_HHMMSS`，延长结束时间不产生新 id）。
- [ ] 新模块 `app/server/event_index.py`：SQLite（标准库）索引
      `event_id / camera_id / start_at / end_at / categories / confidence_max / motion_confirmed / source_date / source_segment`。
      台账 JSON 保持不动，索引可从台账重建（`rebuild_index()`）。
- [ ] API：`GET /api/events?from=&to=&cam=&cat=&limit=`、`GET /api/events/<id>`、
      `GET /api/events/<id>/clip` → 返回 `{segment, offset_sec, url, missing_reason}`，
      定位口径：事件起点前 10 秒；跨分片、未封口、已删除、损坏都要给出明确 `missing_reason`。
- [ ] 前端「事件」页：日期 + 摄像头 + 类别筛选、列表卡片、点击直接播放（前 10 秒入点）、前后 30 秒跳转。
- [ ] 测试：`test_event.py`（合并/幂等/跨分片定位/缺失原因 15+ 条）、`test/ui/events.test.js`。
- [ ] 版本 0.9.121。

**验收**：任选一条真实人员事件，页面点击后 2 秒内开始播放，且播放起点在事件发生前 10 秒内。

---

## 批次 4 · 0.9.122 检测治理

**目标**：多路不互相拖死，误报不碎片化，失败可见。

清单：

- [ ] `detect.py` 有界并发：默认 2 路、上限 4 路，工作者池 + 任务队列；
      同一路不重入；每路独立 `next_due`；超时任务丢弃过期帧；检测绝不阻塞取帧与录像。
- [ ] 时间窗确认：N 帧中 M 帧命中、类置信度阈值、最短持续、结束等待、相邻事件合并
      （轻量实现，不引入 DeepSORT/ByteTrack）。
- [ ] 三态语义：`hit / miss / unknown`，`unknown`（取帧失败、模型未加载、推理异常、断流）不得计入「无活动」。
- [ ] `detect_status()` 统一 `{enabled, available, running, error_code, error_message, last_success_at}`，
      所有失败路径都给 code（`FFMPEG_NOT_FOUND` / `MODEL_MISSING` / `FRAME_FAIL` / `INFER_ERR`）。
- [ ] 前端：检测卡片显示可用性与原因；统计里 `unknown` 单独一类。
- [ ] 测试：`test_detect_sched.py`、`test_event_confirm.py`、`test_detect_status.py`。
- [ ] 版本 0.9.122。

**验收**：8 路配置下，单轮检测总耗时不再随路数线性增长（≤ 1.8 × 单路耗时）；
人为让某路取帧失败，统计里出现 `unknown` 而不掉「无活动」。

---

## 批次 5 · 0.9.123 前端拆分与文档

**目标**：让下一个人（或下一次的我）能在 10 分钟内看懂并改动。

清单：

- [ ] `app/ui/` 源码拆分：`css/{base,theme,components,pages,player}.css`、
      `js/{core,components,pages,player}/*.js`，构建时由 `build.sh` 合成单文件 `index.html`
      （部署形态不变，仍是单 HTML）。
- [ ] 按钮功能验收表落成自动化：每个设置项改完要验证后端状态/进程参数真的变了（不只查 UI 值）。
- [ ] 文档重构：`README.md`（介绍/截图/安装/快速上手）、`docs/USER_GUIDE.md`、
      `docs/ARCHITECTURE.md`、`docs/CONFIGURATION.md`（每项的 key/类型/默认/范围/生效方式）、
      `docs/TROUBLESHOOTING.md`、`BUILD.md` 只留构建；历史排障记录进 `CHANGELOG.md`。
- [ ] `nvr_slim.py` 头部说明重写（当前描述的"后端骨架"与实际 3000+ 行不符）。
- [ ] 版本 0.9.123。

**验收**：拆分前后 `index.html` 渲染结果逐像素一致（沿用 `test/ui/render_check.js`）；
一个新人照 README 能在 fnOS 上装起来。

---

## 不在本轮范围

评审 P3 明确不建议投入：人脸识别、车牌识别、自定义模型训练、复杂多用户权限、实时滤镜。
理由：模型依赖、硬件兼容、隐私与维护成本远超收益，现阶段先把「事件发生 → 找到录像」做透。

批次 2 期间另有三件**故意不做**的：
- 设置页「轮换凭证」按钮：`POST /api/auth/rotate` 已可用（返回新 token），但页面上按一下就把
  自己锁在门外，要连带处理"轮换后自动用新 token 重新登录"，属于新功能而非本批收尾，故仍未做。
  请求层（第 6 项）已经落地，接这个按钮时直接用 `API.post` + `allow401:false` 即可。
- 用户名/密码登录：单机单人，多用户权限属 P3；再叠一层密码只会多一个可忘记的东西。
- 向导里播报 token：安装时用户还看不到页面，等真的打开页面引导层会说清楚去哪找。
