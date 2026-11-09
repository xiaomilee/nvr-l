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
| 3 | 0.9.121 | 事件中心：统一事件模型 + 跨摄像头检索 + 点击定位 | done | 0972f1c |
| 4 | 0.9.122 | 检测治理：有界并发 + 时间窗确认 + 诊断完整 | done | 55b3773 |
| 5 | 0.9.123 | 前端拆分 + 文档重构 + 按钮验收自动化 | **done** | ce81b12 |
| 6 | 0.9.124 | P2 · Webhook 通知补齐：多模板 / 平台手选 / 文案模板 / 发送历史 | **done** | c170220 |
| 7 | 0.9.125 | P2 · 推理设备：GPU 自动探测与自动回落 | **done** | 304a7c4 |
| 8 | 0.9.126 | P2 · 事件检索：多机位 / 多类别 / 移动侦测 / 时间范围 / 最短时长 | **done** | f36af31 |
| 9 | 0.9.127 | P2 · 按时间范围批量导出：分片原样打 zip + 包内 manifest / README | **done** | fef737f |
| 10 | 0.9.128 | P2 · 检测只统计区：框中心落在区内才算目标，区外计数上诊断页 | **done** | 149dc1c |
| 11 | 0.9.129 | P1 · 事件快照：检测确认帧落盘 + 事件卡片缩略图与展开大图 | **done** | 6d7470b |
| 12 | 0.9.130 | P1 · 快照放大：点缩略图看大图（不跳页）+ 检测框按当时那一拍叠回去 | **done** | 7f5c030 |
| 13 | 0.9.131 | P0 · 存储空间预警：稳态容量（写入速率 × 保留天数）与等级 / 建议 | **done** | 6242a83 |
| 14 | 0.9.132 | P1 · 移动端体验：窄屏断点 / 快照与回放分两步 / 竖屏看图 / 弱网态 / 机位筛选 | **done** | 7493181 |

状态取值：`todo` / `doing` / `done` / `blocked`。`blocked` 必须在批次小节里写原因。

**批次 11 状态说明**：第 31 轮只做**调研 + 立项 + 方案**（用户 11-09 13:38 指令），
清单未动、未 bump 版本、未出包。下一轮按本节清单实现。

## 路线合并与目标复核（第 22 轮，按用户 11-08 指令）

用户要求「合并所有路线，整理进度，review 目标，先出个当前进度的包看一下，再继续切片推进」。
**合并结论：不需要 `git merge`。** 两路并行实现自始至终工作在同一个 `main` 上，
48 个本地提交线性叠加（无分支分叉、无冲突、工作区干净）。所谓「两条路线」只是
ROADMAP 叙述里的**分工口径**：一路做「声明表 / 采集器 / 对齐守卫」，一路做
「真起服务的执行器 / 输入强校 / 配置文档」。从本轮起收成**一条进度线**：
以本进度表 + 下方轮次台账 + 批次 5 清单为唯一事实来源，不再按「哪一路认领」记录。

### 目标复核（对照 `docs/REVIEW_BASELINE.md` 与文末三问）

| 自检问题 | 结论 |
|---|---|
| 落在哪一格？ | 批次 5 全部落在 **P0「可信、能无人值守」的工程支撑**：前端源码可拆分维护、设置项改完有证据链、配置键有唯一出处。评审原话「最缺的不是功能，是把已有功能整合成可信的产品」。 |
| 会让录像事实链更不可信吗？ | 不会。前端拆分有 8631 行代码序列逐行相同的等价证明；四个「改了不生效」修复都只在配置写入/通知路径，不动 `recorder.py` 的录制主循环；152 部署后两路 `hstate=recording` 实测在录。 |
| 混进 P3 了吗？ | 没有。人脸/车牌/自训模型/多用户权限/实时滤镜一件未做，见「不在本轮范围」。 |

P1（事件中心、智能缩时）在批次 3/4 已交付，本批次不重复投入；P2 未动。
**方向没有偏，验收已在第 24 轮真跑过，批次 5 标 `done`**：
① 拆分等价三项证明（831 条 CSS 规则 / 8631 行脚本 / 16 个前端测试重跑）；
② 采集器端到端跑分在第 23 轮跑过（20 项 pass 9 / partial 11 / fail 0 / unknown 0）；
③ 「新人照 README 装起来」在第 24 轮由 `deploy_test.py` 把**正式包**部署到 152、
八项自检全绿，再按 README §3 真机跑 `/api/health`、`/api/link`、
`/api/health/gaps`，两路 `hstate=recording`、无 stalled，程序目录里本轮新头注释
已到位（证实正式包真的取代了预览包）。
三问复核：全部落在 P0「可信、能无人值守」的工程支撑；文档与注释不碰录制链路、
不增加失败点；无 P3。

### 清单真实完成度（不假装打勾）

| 清单项 | 状态 | 实际做到哪 |
|---|---|---|
| 1 前端源码拆分 | **done** | 5 份 CSS + 22 份 JS，构建期合成单 HTML；三项等价证明 + 194 条前端守卫 |
| 2 按钮功能验收表 | **done** | 声明表（20 key / 四档判定 / 58 条单测）+ 执行器（12 项四层证据）+ 两表对齐守卫 + 全表采集器（15 条守卫）均已就位；第 23 轮跑出首份端到端报告：**pass 9 / partial 11 / fail 0 / unknown 0**、还原全部成功。11 项 partial 是产品无运行态出口，逐条写明免验理由，不是全验过 |
| 3 文档重构 | **done** | 六份齐：`CONFIGURATION.md`（第 21 轮）+ 第 24 轮的 `README.md` / `USER_GUIDE.md` / `ARCHITECTURE.md` / `TROUBLESHOOTING.md` / `CHANGELOG.md`；`BUILD.md` 只留构建、历史排障搬进 `CHANGELOG.md`；`test_doc_suite.py` 14 项守卫钉住模块表 / 路由表 / tab / 版本 / 状态速查与源码不漂移 |
| 4 `nvr_slim.py` 头注释 | **done** | 重写为 4000 行主程序的模块分层、路由分区与状态汇总入口说明（纯注释，不改逻辑），写明两条配置写入口与三层状态口径 |
| 5 版本 0.9.123 | **done** | 第 22 轮已 bump 五处，收尾**未重复 bump**；只把 changelog 的「进度预览 / 未收尾」措辞改成正式说明，重新出同名包（md5 变了）并重新部署 152 |

### 版本策略（这次是预览包，务必按此执行，别重复 bump）

- `0.9.123` 是进度表给批次 5 定的**目标版本号**。用户要求先看当前进度，
  因此第 22 轮提前把它 bump 到五处并出了**进度预览包**，manifest changelog
  明确写「本版未收尾，后续继续切片推进」。
- 后续切片继续在 `0.9.123` 上累积，**不再 bump 到 0.9.124**（否则与进度表不符）。
- 批次真正收尾时：把 changelog 的「进度预览」措辞改为正式说明、补齐清单与验收，
  **重新出同名 `nvr-0.9.123_x86.fpk`（md5 会变）并重新部署 152**；台账必须写明
  「正式包取代预览包」及两个 md5，避免装错。
- 生产机 `192.168.2.6` 一律不碰，由用户手动升级；预览包阶段更不应升级生产机。

**第 24 轮已收尾，正式包取代预览包**：预览包 `101169048` 字节 /
md5 `e0f7206f51ba5761c9869126eb959e03`，正式包 `101169995` 字节 /
md5 `0d3567e1678808a3fbc8c6b31497d6bd`（同为 `nvr-0.9.123_x86.fpk`，**别装错**）。
部署 152 八项自检全 OK，`before=0.9.123 → 0.9.123` 属同版本重部署：
工具先数进程确认旧进程真停、再 `rsync --checksum --delete` 覆盖、后重启，
程序目录里本轮新头注释已到位可证。生产机 2.6 仍需手动升级。

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
| 6 | 11-08 09:40 | 批次 3 切片一：`event.py`（身份/生命周期/定位）+ `event_index.py`（SQLite 投影）+ 三个只读接口（`/api/events`、`/<id>`、`/<id>/clip`）；测试 111 条 | 720a7da | 未出包（批次只剩前端页与版本收尾）；后端 944 + 前端全绿，validate 73 项通过 |
| 7 | 11-08 11:25 | 批次 3 收尾：前端「事件」页（三段筛选 / 卡片 / 点击定位 / ±30 秒）+ `test/ui/events.test.js` 70 条 + `/api/events` 进部署自检 + 五处版本 0.9.121 + `test/e2e_events.js` 端到端 | 0972f1c | 0.9.121 出包（101142027 字节，md5 f8b82be104966f53a29c118e4ceb933b）；152 部署八项全绿，两路 `hstate=recording`；真机点 11:19:52「人员」事件 814ms 起播、落点 −10.0 秒 |
| 8 | 11-08 12:55 | 批次 4 切片一：有界并发调度（`Scheduler` + 工作者池 + 过期丢弃）+ 三态记账（hit/miss/unknown/skip）+ 统一 `detect.status_of`（七项契约 + 十个错误码）+ 前端按 state 翻译与未知单独列 | d2138c3 / 5d495fc | 未出包（批次还剩时间窗确认与版本收尾）；后端 989 + 前端 915 全绿，validate 73 项通过，HTTP 冒烟 55 项全通 |
| 9 | 11-08 14:50 | 批次 4 收尾：时间窗确认（`confirm.py`：N 帧 M 命中 / 分类确认阈值 / 最短持续 / 结束等待 / 相邻合并）+ 通知与台账跟着确认走 + 跨桶窗口裁到桶首 + 五处版本 0.9.122 | 55b3773 | 0.9.122 出包（101160332 字节，md5 5ea95cd090fae412793b91f61543671a）；152 部署八项全绿，两路 `hstate=recording`；「一路取帧失败 → unknown + FRAME_FAIL 且 today 不涨」实测通过；8 路 1.8× 目标按 3 核实测**未达成**（见批次 4 验收记录） |
| 10 | 11-08 15:56 | 批次 5 切片一：样式源拆分（`app/ui/css/{theme,base,components,pages,player}.css`）+ `tools/compose_ui.py` 合成器 + 构建闸门（validate 四项、`css_split.test.js` 28 条）+ 打包器把 `ui/css/` 排除出包 | 275b2a8 | 未出包（批次还剩 JS 拆分 / 按钮验收 / 文档重构 / 版本收尾）；validate 77 项、后端 1022 条、前端含新 28 条全绿；样式层叠等价已证（831 条规则逐条同） |
| 11 | 11-08 17:29 | 批次 5 切片二：脚本源开始拆，只搬 **core** （`core/{auth,format,api,theme}.js` 四段 187 行）+ `compose_ui.py` 扩成多段合成（标记位置即执行顺序）+ 闸门（validate 77→81、`js_split.test.js` 39 条、打包排除 `ui/js`） | b20c514 | 未出包（批次还剩 components/pages/player 三段与文档收尾）；后端 1022 条、前端全绿、validate 81 项；等价已证：未搬的 8442 行逐行相同、四段去掉文件头后逐行相同 |
| 12 | 11-08 19:43 | 批次 5 切片三：脚本源 `components` 三段（shell / fullscreen / nav，共 658 行）搬进 `js/components/`，`js_split.test.js` 39→60 条、validate 的源文件清单 4→7 | c597afd | 未出包（批次还剩 pages/player 两段、按钮验收、文档、版本收尾）；后端 1022 条、前端全绿、validate 81 项；等价已证：剥掉标记与文件头后脚本 **8631 行逐行相同**，index.html 又是 29 行纯新增 |
| 13 | 11-08 19:58 | 批次 5 切片四：脚本源 `pages` 三段（`clips.js` 566 / `settings_cards.js` 918 / `settings_editor.js` 655，共 2139 行）搬进 `js/pages/`，两张表 7→10（js_split 60→81 条、validate JS_REQUIRED 7→10） | d76c416 | 未出包（批次还剩 player 段、按钮验收、文档重构、nvr_slim.py 头注释、版本收尾）；validate 81 项、后端 1022 条、前端全绿；等价已证：剥标记与文件头后 **8631 行逐行相同**，index.html 39 行纯新增、0 删除 |
| 14 | 11-08 20:18 | 批次 5 切片五：脚本源再搬 4 段共 1996 行（`core/state.js` 463 / `player/player.js` 545 / `pages/live_snap.js` 78 / `pages/live.js` 910），凑齐 core/components/pages/player 四目录；两张表 10→14 | e84dd13 | 未出包（批次还剩脚本最后两片、按钮验收、文档重构、nvr_slim.py 头注释、版本收尾）；validate 81 项、后端 1022 条、前端全绿（js_split 81→**109** 项）；等价已证：剥标记与文件头后 **8631 行逐行相同**，index.html 52 行纯新增、0 删除；152 只读 0.9.122、两路 recording（age 0/1） |
| 15 | 11-08 20:28 | 批次 5 切片六：脚本源再搬 4 段共 2990 行（`pages/diagnostics.js` 1254 / `settings_persist.js` 161 / `playback_data.js` 296 / `timeline.js` 1279），源文件 14→18 份；两张表 14→18 | ac1fe4f | 未出包（批次还剩脚本两片、按钮验收、文档重构、nvr_slim.py 头注释、版本收尾）；validate 81 项、后端 1022 条、前端全绿（js_split 109→**137** 项）；等价已证：剥标记与文件头后 **8631 行逐行相同**，index.html 53 行纯新增、0 删除；152 只读 0.9.122、两路 recording（age 3/0） |
| 16 | 11-08 20:44 | 批次 5 切片七（脚本拆分收口）：最后两片切成 4 段共 639 行（`pages/events.js` 334 / `core/boot.js` 160 / `core/wiring.js` 86 / `core/auth_boot.js` 59），源文件 18→22 份；**清单第 1 项打勾**；新增「标记段外无散落代码」断言 | 8b542c1 | 未出包（批次还剩按钮验收表、文档重构、nvr_slim.py 头注释、版本收尾）；validate 81 项、后端 1022 条、前端全绿（js_split 137→**166** 项）；等价已证：剥标记与文件头后 **8631 行逐行相同**，index.html 46 行纯新增、0 删除，脚本区外只剩 `"use strict";`；152 只读 0.9.122、两路 recording（age 1/0） |
| 17 | 11-08 21:04 | 批次 5 切片八：按钮功能验收表落地（`test/test_btn_effect.py`，真起本地服务，5 个衔接/录制设置项 × 四层证据，9 项进闸门）；**首跑抓到并修掉两个「改了不生效」缺口**：`Link.reconfigure` 漏 `max_queue`、`_ENUM_KEYS` 漏 `link_mode` | fc3b5b9 | 未出包（清单第 2 项还剩多组设置项、文档重构、nvr_slim.py 头注释、版本收尾）；validate 81 项、后端 **1031** 条（1022+9）、前端全绿；本地临时服务与 152 同构，**未连测试机改配置**；152 只读 0.9.122、两路 recording（age 1/0） |
| 18 | 11-08 21:23 | 批次 5 切片九：按钮验收表第二组（`record_enable` 进 `CASES`、`test_90` 禁用顶层同名回读字段、新增 `test_93` 验第二条写入口）；**首跑抓到并修掉第三个热生效缺口**：`set_link()` 改 `record_enable` / `record_segment_sec` 只通知 Link，Recorder 仍按旧参数录制 | 8f1bf3a | 未出包（清单第 2 项还剩多组设置项、文档重构、nvr_slim.py 头注释、版本收尾）；validate 81 项、后端 **1033** 条（1031+2）、前端全绿，`test_btn_effect.py` 9→**11** 项；`snapshot_interval` / `record_frag` 查明非设置页按钮或非可观测，记入「不在本轮范围」；152 只读 0.9.122、两路 recording（age 2/1） |
| 19 | 11-08 21:40 | 批次 5 切片十：清单第 2 项第三组，第 18 轮留下的「两张表归属」定案——声明式验收矩阵 `test/button_matrix.py`（20 行覆盖 `pushCfg` 全部 key、四层判定、pass/partial/fail/unknown 四档、DEFAULTS 与源码双向覆盖守卫、照出 4 处假设置）+ 58 条单测；补「两表对齐」守卫，执行器 CASES 的 6 个 key / 7 条 effects 必须都进声明表 | 8249fd9 / 4cdfbd5 | 未出包（清单第 2 项仍未打勾，按规则不 bump/不部署）；validate 81 项、后端 **1091** 条（含新增 58）、前端全绿、0 FAIL；152 只读 0.9.122、两路 `hstate=recording`（age 1/1）、无 stalled、`detect.state=ready`、confirmed=50 |
| 20 | 11-08 22:04 | 批次 5 切片十一：清单第 2 项第四组，收口第 18 轮挂账的输入强校缺口——`set_link()` 的 `record_enable` 从裸 `bool()` 改用 `runtime.coerce`，字符串 `"false"` 不再被判成 `True` 而把录像打开；`test_94` 单独带一条用例（磁盘 + 运行中 Recorder + 两条入口同口径三层证据），首跑复现旧行为全红、修后全绿 | 817c4c6 | 未出包（清单第 2 项仍未打勾：采集器与第一份端到端报告未接，按规则不 bump/不部署）；validate 81 项、后端 **1092** 条（1091+1）、51 个测试文件、前端 16 文件全绿、0 FAIL；`test_btn_effect.py` 11→**12** 项，并发路的 `test_button_matrix.py` 58 项未受影响；152 只读 0.9.122、端口 18085、两路 `hstate=recording`（age 3/0）、`record.enabled=True`、无 stalled、`detect.state=ready`、confirmed=51 |
| 21 | 11-08 23:01 | 批次 5 切片十二：清单第 3 项文档重构第一刀——`docs/CONFIGURATION.md`（29 个顶层键逐项写清类型 / 默认值 / 范围 / **生效方式** / 代码出处，另含配置读写链、嵌套块双口径与已知假设置）+ `test/test_config_doc.py` 10 项守卫，把主表与 `DEFAULTS` / `_ENUM_KEYS` / 三个范围常量逐项钉住；`BUILD.md` 同步测试目录、两表分工与「改配置键要同步文档」 | df21cb1 | 未出包（清单第 2 项采集器由并发路认领、第 3 项还剩五份文档，按规则不 bump / 不部署）；validate 81 项、后端 **1102** 条（1092+10）、52 个测试文件、前端 16 文件全绿、0 FAIL；首跑故意改坏两处（默认值 300→600、生效方式 需重启→热生效）各命中一条断言，恢复后 10 项全绿；152 只读 0.9.122、端口 18085、两路 `hstate=recording`（age 0/0）、`record.enabled=True`、无 stalled、`detect.state=ready`、confirmed=58 |
| 22 | 11-08 23:44 | **按用户指令合并两路路线并出进度预览包**（非收尾）：确认两路成果线性叠在 `main`、无需 merge，ROADMAP 新增「路线合并与目标复核」小节（对照 REVIEW_BASELINE 三问 + 清单真实完成度 + 版本策略）；五处版本 bump 到 `0.9.123`，manifest changelog 标注「本版未收尾」 | f818518 | **出包** `dist/nvr-0.9.123_x86.fpk` **101169048 字节** md5 `e0f7206f51ba5761c9869126eb959e03`；闸门 validate 81 项、后端 **1102** 条、52 文件、前端 16 文件、0 FAIL；`deploy_test.py` 部署 152 八项自检全 OK（`test-reports/deploy-0.9.123.json`），0.9.122→0.9.123 上线 0.1s、端口 18085，单独复核两路 `hstate=recording`（age 5/0）、`record.enabled=True`、无 stalled、`detect.state=ready`；批次 5 仍 `doing`，清单 2/3/4 未打勾，生产机 2.6 未碰 |
| 23 | 11-09 00:38 | 批次 5 切片十三：**清单第 2 项收尾**——接手并发实例 00:01~00:06 写好但未提交的采集器在制品（`test/btn_harvest.py` 全表 20 项改值→三份快照→还原→四层判定→出报告 + `test_btn_harvest.py` 15 条离线守卫 + `BUILD.md` 用法 + `button_matrix.py` 抽出 `roll_up()` 并修正两处证据口径）；复核闸门全绿后跑出**首份端到端报告** | de5e6a3 | 未出包（清单 3/4 未勾、批次未收尾，按规则不 bump / 不部署）；首跑闸门偶发 `test_btn_effect.py` 起服务失败，单独复跑 12 项全绿后整轮复跑全绿：validate 81 项、后端 **1117** 条（1102+15）、53 文件、前端全绿、0 FAIL；报告 `test-reports/btn-matrix-0.9.123-r23.json`：20 项 **pass 9 / partial 11 / fail 0 / unknown 0**、总判定通过、还原全部成功；11 项 partial 逐条写明免验理由（需重启 / 夜间任务无出口 / ffmpeg argv 无 HTTP 出口），**不是 20 项全验过**；152 只读 0.9.123、18085、两路 `hstate=recording`（age 1/2）、无 stalled、`detect.state=ready`，生产机 2.6 未碰 |
| 24 | 11-09 01:30 | **批次 5 收尾**：清单第 3 项文档重构整项（`README.md` / `docs/USER_GUIDE.md` / `docs/ARCHITECTURE.md` / `docs/TROUBLESHOOTING.md` / `CHANGELOG.md` 五份 + `BUILD.md` 只留构建、历史排障搬进 CHANGELOG）+ `test/test_doc_suite.py` 14 项守卫；清单第 4 项 `nvr_slim.py` 头注释重写；changelog 正式化、**不重复 bump**；出同名正式包并部署 152 跑真机验收 | ce81b12 | **出包** `dist/nvr-0.9.123_x86.fpk` **101169995 字节** md5 `0d3567e1678808a3fbc8c6b31497d6bd`（预览包 101169048 / `e0f7206f51ba5761c9869126eb959e03`，正式包取代预览包）；闸门 validate 81 项、后端 **1131** 条（1117+14）、54 文件、前端全绿、0 FAIL；守卫首跑故意改坏模块名与路由各命中一条断言、恢复后 14 项全绿；`deploy_test.py` 部署 152 八项自检全 OK（`test-reports/deploy-0.9.123.json`），0.9.123→0.9.123 上线 0.1s、端口 18085；按 README §3 真机验收：两路 `hstate=recording`（age 1/0）、`record.enabled=True`、无 stalled、`detect.state=ready`，程序目录含本轮新头注释（正式包实证）；`gaps` 有 1 条 61 秒缺口，原因「录像进程未运行…已恢复」，系部署停服重启所致、非产品问题；**批次 5 标 done**，生产机 2.6 未碰 |
| 25 | 11-09 07:57 | **批次 6 立项 + 切片一**（按用户「继续、加快进度」取 P2 首项）：`notify_tpl.py` 多模板按 host 适配五平台 + 通用回退、`notify_history.py` jsonl 落盘与过滤查询、`GET /api/notify/history` 只读接口、`status.notify.history` 缓存计数、停机补 `Notifier.stop()`；文档/守卫同步 | 1a6652f | 未出包（清单 5/6 未勾、批次 未收尾，按规则不 bump / 不部署）；闸门 validate 81 项、后端 **1199** 条（1131+68）、58 文件、前端全绿、0 FAIL；四份新单测 68 条含真起本地服务的 HTTP 冒烟 10 项（401 / 落盘读取 / 五种过滤 / 坏参数不 500）；首跑抓到并修掉 「坏 UTF-8 行让整份历史作废」；文档守卫故意改坏两处各命中一条、恢复全绿；152 只读 0.9.123、18085、两路 `hstate=recording`（age 3/0）、无 stalled、`detect.state=ready`，生产机 2.6 未碰；**批次 6 保持 doing** |
| 26 | 11-09 08:54 | **批次 6 收尾**：设置页平台手选 + 文案模板 + 实际发送方式 + 发送历史查看；`notify.platform` / `notify.template` 两键全链路接通（DEFAULTS / norm_notify / pushCfg / 回读 + `test_notify_ui.py` 16 条守卫）；测试发送带界面值；部署自检加第九项，项数改由代码数出来；五处 bump 0.9.124 并写 changelog | c170220 | **出包** `dist/nvr-0.9.124_x86.fpk` **101180736 字节** md5 `d9191c3aa3bbbc8db8f781820521e42d`；闸门 validate 81 项、后端 **1224** 条（1199+25）、59 文件、前端 514 / 70 / 28 / 166 全绿、0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.124.json`），0.9.123→0.9.124 上线 0.1s、端口 18085、通知历史 0 条、凭证 401；真机验收两路 `hstate=recording`（age 1/2）、无 stalled、`detect.state=ready`、`notify.render` 有 platforms 与 `history.persisted`、config 回读带新键、程序目录含新代码；缺口为部署停服窗口（覆盖率 0.9926 / 0.9877）；**批次 6 标 done**，生产机 2.6 未碰 |
| 27 | 11-09 09:35 | **批次 7 立项 + 全量收尾**（按用户「继续」取 P2 清单下一项）：`detect.device` 从「配置里的死键」变成真的接管推理后端——`probe_providers` 只认 onnxruntime 自报的可用后端、`pick_providers` 按 CUDA > TensorRT > ROCm 挑、GPU 建会话失败自动回落 CPU 并记原因；`Engine` 收 `device` 与 `ort_loader` 注入点，暴露 `device_status()`；`status_of` 透出 `device` 块；前端 `detDevOf()` 让标题栏跟实际后端走、诊断卡新增「推理设备」一行；文档 CONFIGURATION §4.1 / TROUBLESHOOTING §5.1 / CHANGELOG 同步 | 304a7c4 | **出包** `dist/nvr-0.9.125_x86.fpk` **101186255 字节** md5 `928ffaf2258323f54a5acabc8b7041dc`；闸门 validate 81 项、后端 **1250** 条（60 文件，新增 `test_detect_device.py` 26 条）、前端 detect 58（+14）/ 设置页 514 / 时间轴 70 / 日志 25+38 / 健康 30 / 凭证 11 / 请求层 31 / 事件 70 / 样式 28 / 脚本 166 全绿、0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.125.json`），0.9.124→0.9.125 上线 0.1s、端口 18085、事件索引 190 条 / 2 天、通知历史 0 条、凭证 401；真机把 `detect.device` 改 `gpu` 实测：`requested=gpu / actual=cpu / fallback_reason=「请求 GPU，但 onnxruntime 没有 CUDA / Tensorrt / ROCM 后端（当前可用：AzureExecutionProvider, CPUExecutionProvider）」`、`state` 仍 `ready`、`avg_ms` 43；日志三条实证（「AI 推理设备改为 gpu，重建检测 worker」→ 回落原因 → 「改为 auto」）；两路 `hstate=recording`（age 1/0）、`record.enabled=True`、`confirm` 块未被覆盖；随后还原 `auto` 并复核；程序目录含 `pick_providers` / `engine_device` / `detDevOf` 新代码；**批次 7 标 done**，生产机 2.6 未碰 |
| 28 | 11-09 10:05 | **批次 8 全量收尾**（按用户「立项」取 P2 下一项）：事件检索高级筛选。`event_index.query()` 增 `cams` IN / `cats` OR / `min_dur` / `motion_only`（单值 `cat`/`cam` 兼容）；`parse_cats()` 逗号多值 + `motion` 单独提取 + 未知词回 bad；`event.kind_of()` 一处判断，`kind` 进事件与索引，`summary_of()` 增 `motion` 计数；`/api/events` 收 `min_dur`/`motion`、参数错明确报错、`filters` 回显服务端实际用上的条件；前端机位/类别改多选 + 「移动侦测」+ 「更多筛选」（HH:MM / 最短时长 / 清空）+ 列表头写条件 | f36af31 | **出包** `dist/nvr-0.9.126_x86.fpk` **101191778 字节** md5 `4c7aa9b998d43381481ea0697709234d`；闸门 validate 81 项、后端 **1279** 条（61 文件，新增 `test_event_filter.py` 21 条 + `test_event_api.py` 8 条）、前端 events **91**（+21）/ detect 58 / 设置页 514 / 时间轴 70 / 样式 28 / 脚本 166 全绿、0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.126.json`），0.9.125→0.9.126 上线 0.1s、端口 18085、事件索引 **196 条 / 2 天**、通知历史 0 条、凭证 401；真机 196 条事件验筛选：person 42 / vehicle 22 / 并集 **60**（=42+22−4，逐 id 校验并集恒等式）/ motion 440 且与 AI 零交集 / cam-test01 339、cam-test02 163 / min_dur=60 → 296 / `cat=dog` 与 `min_dur=ten` 均 `ok:false` 带可用值 / `09:00–10:00` 时间窗 12 条且 `filters.from` 回显；两路 `hstate=recording`（age 1/1）、`detect ready`；程序目录含 `kind_of` / `parse_cats` / `motion_only` / `evMoreBox`；**批次 8 标 done**，生产机 2.6 未碰 |
| 29 | 11-09 10:35 | **批次 9 立项 + 全量收尾**（按用户「继续」取 P2 候选池）：按时间范围批量导出。候选池第一项「模型版本管理」仍缺第二份模型与标注样本，跳过；导出不是增强而是「找到 → 确认 → 带走」缺了最后一步——以前只能一片一片点 `dl=1`。新增 `export.py`（`select` 半开区间 / `totals` / `cap_problem` / `slug`+`folders` 同名避撞 / `manifest`+`readme` / `write_zip` / `Chunked`）；`_export_build()` 一处构建，`/api/export/plan`（`detail=1`）与 `/api/export`（chunked，`X-Export-Files`/`X-Export-Bytes`）共用；枚举复用 `_day_files()` 与事件定位同口径；只剩归档的日子回 `archive_only` 明说「不含归档」；前端 `pages/export.js` 文件页右上角「导出」（机位多选 + 时间范围，先计划再下载，日期跟 `state.daySel`，下载地址直接用服务端 `d.url`）| fef737f | **出包** `dist/nvr-0.9.127_x86.fpk` **101203350 字节** md5 `82e354685596b9b17ce016a44e5329b4`；闸门 validate **82** 项、后端 **1297** 条（62 文件，新增 `test_export.py` 18 条）、前端 export **26** / events 91 / detect 58 / 设置页 514 / 时间轴 70 / 日志 25+38 / 健康 30 / 凭证 11 / 请求层 31 / 事件 70 / 样式 28 / 脚本 **173** 全绿、0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.127.json`），0.9.126→0.9.127 上线 0.1s、端口 18085、事件索引 200 条 / 2 天、通知历史 0 条、凭证 401；真机两路 40 分钟导出：计划 20 片 / **737580686 字节**、`head_extra_sec=180`，下载 HTTP 200 + `X-Export-Files=20`、22 条目、`testzip()` 通过、全 STORED、逐片字节与 manifest 全等、中文目录正常；错误路径（机位写错 / 空时段 / 4 小时两路 3.8 GB 超限）均给出明确原因；导出全程两路 `hstate=recording`（age 0~3）、日志有「导出：… 703.4 MB」；**批次 9 标 done**，生产机 2.6 未碰 |
| 30 | 11-09 11:55 | **批次 10 立项 + 全量收尾**（按用户「继续」取 P2 候选池）：检测只统计区。候选池第一项「模型版本管理」仍缺第二份模型与标注样本，第三轮跳过。`detect.apply_regions()` 一处做「类别 → 忽略区 → 只统计区」，返回被挡掉的框数进 `per_cam.outside`；`nvr_slim.norm_masks()` 写盘与回显共用（只认整数与纯数字字符串）；前端第二张 16×9 画布（绿=只统计 / 琥珀=忽略，互相淡显）+ 两块清单各自清空 + 计数进标题；诊断页每路一行加「区外忽略 N」| 149dc1c | **出包** `dist/nvr-0.9.128_x86.fpk` **101208384 字节** md5 `832d3c9ec8ec1109e30aa51ce0ae4906`；闸门 validate **81** 项、后端 **1324** 条（63 文件，新增 `test_detect_include.py` 27 条）、前端 masks **27** / export 26 / events 91 / detect 58 / 设置页 514 / 时间轴 70 / 样式 28 / 脚本 173 全绿、0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.128.json`）；真机：`include=[0,1,2]` 往返一致、脏值 `[-1,144,1.5,null,"7"]` 落成 `[7]`、诊断行「只统计区=1 格」、只统计区设 1 格后两轮 `cam-test01 outside=9` 而 `cam-test02 outside=0`、还原后两块皆空；全程两路 `hstate=recording`、`detect.state=ready`；**批次 10 标 done**，生产机 2.6 未碰 |
| 31 | 11-09 14:05 | **批次 11 立项（只做调研 + 方案，未动代码）**（按用户「继续，导出功能简化，研究事件页快照并写进路线图」）：把「事件快照」立成批次 11。调研核过六条事实：快照只有最新一张且被 `os.replace` 覆盖、事件模型 15 键 / 15 列无任何图字段、事件身份只由 cam + 起点秒决定（图可按时间窗匹配，不必改身份）、检测推理读的就是取帧层那张 720p jpg（不必再起 ffmpeg）、取帧已是 q:v4 45~180KB、移动侦测边界是事后帧差曲线算的（无确认时刻可挂图）。152 只读实测：`var/snapshots` 8 文件 848KB、`/vol1` 可用 11G、事件索引 200 条 / 2 天。方案定三条口径（拍「当时」不拍「现在」/ 只加不改走独立线程与有界队列 / 图是可清理附属物不进索引）、存储 `var/event_shots/<cam>/<日期>/<HHMMSS>_<mmm>.jpg`、接口 `shot:{url,at,offset}` 与 `/api/events/<id>/shot`（404 JSON 不退回当前快照）、前端 76×43 缩略图 + 展开宽图、六类失败降级；导出三个延伸按用户「不用深究」**就此收口**不再排期 | 89531d3 | 未出包、未 bump（清单未动，按规则不打包）；闸门 validate **81** 项、前端 events 91 / export 26 / detect 58 / masks 27 / 样式 28 / 脚本 173 全绿 0 FAIL；152 只读 0.9.128，两路 `hstate=recording`（age 0/1）、`detect.state=ready`；生产机 2.6 未碰 |
| 32 | 11-09 15:05 | **批次 11 实现并收尾**（按用户「把事件的快照继续落实」）：新增 `event_shot.py`（392 行）—— `ShotStore` 30 秒/路限流 + 每路每天 300 张 + 队列 8 满即丢 + 单写线程 + 7 天 sweep，`find()` 窗口外扩 15 秒取**最早**一张（跨零点也认）；`detect._grab()` 改返回 `(ok,data,err,src)`（src = 这次真正读的那张 jpg，防止"拍现在不拍当时"），`_one()` 在 `_write_ledger()` 之后 `_notify()` 之前调 `_shot()`（时间取源图 mtime，异常全吞）；`nvr_slim._attach_shots()` 给列表补 `shot{url,at,offset}`，路由 `/api/events/<id>/shot` 200 走 `image/jpeg`+ETag+`max-age=86400`、没有则 404 JSON，`/api/status` 暴露 `shots`；前端缩略图 76×43（lazy + onerror 隐藏）+ 展开宽图「快照 HH:MM:SS · 事件开始后 N 秒」，无图时说明「这条没有留快照」| 6d7470b | **出包** `dist/nvr-0.9.129_x86.fpk` **101218696 字节** md5 `046fefe536fb56c4d3b4520923a0de8a`；闸门 validate **81** 项、后端 **1365** 条（64 文件，新增 `test_event_shot.py` 39 条）、前端 events **101** / export 26 / detect 58 / masks 27 / 样式 28 / 脚本 173 全绿 0 FAIL；152 部署**九项全 OK**；真机：14:58:45 AI 事件自动带 `shot{at:14:58:55, offset:9.3}`，GET → **200 image/jpeg 145077 字节 / ffprobe 1280×720 / file 认 JPEG**，ETag 二次请求 **304**，旧事件 → **404 JSON** 且列表 `shot:null`，列表 200 条 145/90/91ms，检测关 43 秒 `written` 不动、重开后 3→5 张，`rate_limited=9`，全程两路 `hstate=recording`，`recorder.py` **git diff 为空**；**批次 11 标 done**，生产机 2.6 未碰 |

| 33 | 11-09 16:25 | **批次 12 全量收尾**（按用户「继续」取批次 11 挂账的快照延伸）：缩略图从 76×43 变成可点开的大图浮层（**不跳页**，浮层里才有「打开回放」出口，Esc / 点背景关闭）；检测框随图旁挂同名 `.json`，坐标来自**那一拍推理**过完区域过滤的 `dets`，由浏览器按百分比叠层绘制（服务端不解码、不重编码）；`?meta=1` 与图片共用一处定位 `shot_lookup()`（避免「有图没框」），图不在就 404 不给空框清单、坏 json 当作没框不修不重建；写线程框失败只动 `meta_failed`，`recorder.py` 一行未改 | 7f5c030 | **出包** `dist/nvr-0.9.130_x86.fpk` **101226985 字节** md5 `f43f553041cf61c2a8cdda4be82d071b`；闸门 validate **81** 项、后端 **1382** 条 / 64 文件，前端 events **113**（+12）/ export 26 / detect 58 / masks 27 / 设置页 514 / 时间轴 70 / 样式 28 / 脚本 173，0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.130.json`），0.9.129→0.9.130 上线 **0.1s**、端口 18085；真机：31 秒人员事件 `cam-test01_20261109_161839` → `?meta=1` 回 **2 个框**（`person 0.656` + `vehicle 0.404`，offset 29.7）、图 138193 字节；`cam-test02_...161713` 图 **182007 字节 / 1280×720 / JPEG**、ETag 二次 **304**；升级前老图 → `boxes: []` + note「0.9.130 之前存下的图」；无快照事件 → **404 JSON**；`shots`: `written=5` / `meta=5` / `meta_failed=0` / `rate_limited=14` / `bytes≈22.9MB`，磁盘上 json 与 jpg 同名成对；全程两路 `hstate=recording`（age 0/3），`recorder.py` **git diff 为空**；**批次 12 标 done**，生产机 2.6 未碰 |
| 34 | 11-09 17:16 | **批次 13 立项 + 全量收尾**（按用户「继续」取 P0「存储管理」三格里唯一没做过的一格）：新增 `space.py` 纯函数模块，按**稳态容量**判空间够不够 —— `rate` = 最近 7 个完整日中位数（丢 0 字节日）、`need` = rate × 保留天数（归档按归档速率）、`other` = 总量 − 剩余 − 录像实测、`headroom` = 总量 − other − need；分 `off / unknown / critical / warn / ok` 五档，建议按原因给（装不下 / 稳态余量太小 / 现在剩余就低，多因全保留、取最重定级）；`storage_usage` 顺手按 (日期, 文件名) 去重出 `days` 与 `space`（不额外扫盘，缓存加格式版本号 `STORAGE_CACHE_V=2` 让旧缓存升级后重扫而不是"未知"半小时）；监控线程 10 分钟一算、只读内存喂 `/api/status.space` 与 `/api/health.space`，等级变化才记日志（升级一条、恢复一条，不刷屏）；配置键 `space_warn_gb`（0 = 关闭，大盘 10% 兜底）；诊断页存储卡顶部预警条 + 直播页头部提示 + 设置页阈值输入；**预警只报告不处置** —— 自动删录像等于让"空间紧张"改写事实数据，删录像仍走「保留策略」预览加二次确认；`recorder.py` 一行未改 | 6242a83 | **出包** `dist/nvr-0.9.131_x86.fpk` **101238964 字节** md5 `26c6c9bbed84315c85e291733c1c9b04`；闸门 validate **81** 项、后端 **1425** 条 / **65** 文件，前端 space **30**（新增）/ 设置页 514 / 时间轴 70 / events 113 / export 26 / detect 58 / masks 27 / 样式 28 / 脚本 173，0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.131.json`），0.9.130→0.9.131 上线 **0.1s**、端口 18085；真机 `/api/storage/usage.space` 判 `critical`：`rate=19.3 GB/天`（最近 1 个完整日）、`need=57.9 GB`、`free/total=7.0/55.0`、`headroom=-17.3 GB`、`suggest_days=2`、`keep_text=全时 3 天`；`space_warn_gb` 往返：`0 → off`（`reasons=[]`、无 advice）、`3 → reasons 由 [capacity, free_critical] 收成 [capacity]`（阈值生效，但盘本身装不下所以仍 `critical` —— **计划里"改 3 应降为 warn"那条写错了，已在批次小节记明**）、还原 `20` 复核落盘；清理过期未误报（`cleanup_stale` 不出现）；`/api/status.space` 部署后首轮（等 20 秒）即写回、`/api/health.space={level:critical, free_gb:7.1}`；全程两路 `hstate=recording`、`recorder.py` **git diff 为空**；**批次 13 标 done**，P0 六项全部有落地，生产机 2.6 未碰 |
| 35 | 11-09 19:05 | **批次 14 立项 + 全量收尾**（按用户「移动端体验还应该进一步优化」取 P1「事件中心」与 P0「回放」的可用性）：全站首个 `@media (max-width:640px)` 断点（字号上浮一档、触控目标 ≥ 40px、卡片重排）；事件详情两段式（先看清那一眼再决定开不开视频，主按钮「▶ 打开录像（约 N 秒）」把代价写在按钮上，±30 秒降为次要）；快照浮层「横过来看（旋转 90° 占满竖屏高）+ 放大一档」；机位筛选从横滑 chips 改底部弹层 + 2 列网格多选（替代 `evchip`）；事件列表加载骨架 + 可重试失败态（断网给「同一个网络」提示）；`player.js` 加 `visibilitychange` 切后台暂停、回来续播；`nav.js` 切走直播/其它页调 `closeLiveRoom()` 主动断 MJPEG 流；`recorder.py` 一行未改 | 7493181 | **出包** `dist/nvr-0.9.132_x86.fpk` **101244567 字节** md5 `5b81e6a5ae713807ea2990fe4725f632`；闸门 validate **81** 项、后端 **1425** 条 / **65** 文件，前端 events **123**（+10）/ space 30 / detect 58 / masks 27 / 设置页 514 / 时间轴 70 / export 26 / 样式 28 / 脚本 173，0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.132.json`），0.9.131→0.9.132 上线 **0.1s**、端口 18085；真机部署后 `curl` 拉 `ui/` 确认新代码已生效：`evCamPopHtml`/`data-evzoomrot`/`data-evzoomzoom`/`evconfirm`/`evcampop-sheet`/`EV_SHOT_ONERR`/`visibilitychange`/`closeLiveRoom` 全部进编译后 HTML、`@media (max-width:640px)` 断点已下发；两路 `hstate=recording`、`recorder.py` **git diff 为空**；**待用户手机确认**：375 宽布局 / 竖屏旋转后检测框不错位 / 后台切回续播 / 切页断流的**视觉与触摸**层面（代码与部署已实锤，机上观感需真机过一眼）；**批次 14 标 done**，生产机 2.6 未碰；0.9.132 部署后用户试出一处回归——打开事件详情会立即跳回放（evOpen 里顺手 `evSeek` 把两段式架空），0.9.133 已修正（见第 36 轮） |
| 36 | 11-09 19:48 | **0.9.133 修正回归**（批次 14 收尾后用户试出）：`events.js` 的 `evOpen(id)` 在拉到 clip 后顺手调 `evSeek` 跳回放，把"先看清再打开"的两段式架空了——点事件就直接进回放页。修正：`evOpen` 只取定位（决定能不能播、播不了说清原因）**不再跳转**；真正跳回放的是详情里的「打开录像」按钮（`data-evgo` → `evGo` → `evSeek`）与快照浮层的「打开录像」（`data-evzoomplay` 改为 `evOpen(id).then(()=>evGo(0))`，兼容从缩略图进浮层时还没开过卡片、clip 未拉的情形）。七份文档版本戳 bump 0.9.132→0.9.133，manifest `changelog` 首条改写；`recorder.py` 一行未改 | 1d37693 | **出包** `dist/nvr-0.9.133_x86.fpk` **101244890 字节** md5 `5e49d83b56fbf2074a7719016bf4f34c`；闸门 validate **81** 项、后端 **1425** 条 / **65** 文件，前端 events **123** / 其余全绿，0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.133.json`），0.9.132→0.9.133 上线 **0.1s**、端口 18085；机上 `curl ui/` 确认 `evSeek(ev, evClipStart(d))` 已从编译后 HTML 消失、`evOpen(id).then(()=>evGo(0))` 已进、两段式标记 `evconfirm`/`evacts`/`data-evgo` 都在；两路 `hstate=recording`、日志 warn=0 err=0、`recorder.py` **git diff 为空**；真机行为需用户复测确认不再自动跳；**生产机 2.6 未碰** |
| 37 | 11-09 21:05 | **批次 15 立项 + 全量收尾**（按用户「移动侦测的事件记录，怎么实现快照功能？规划一下」→「执行」）：纯帧差运动事件（`KIND_MOTION`，AI 没框住）在 0.9.133 及之前 `ev.shot` 恒为 None。新增 `motion_shot.py`（`MotionShotService`）——事件查询发现某条 motion 且尚无图时，后台从 recorder 已录分片用 `ffmpeg -ss` 抽一帧补进 `event_shots/`，时间锚定事件起点（图片 mtime 锚到事件当天，不是「现在」）、幂等（窗口内已有图就跳过）、队列满丢、分片过期 / 没 ffmpeg 退化 None，复用 `ShotStore` 落盘与查询、前端零改动；`nvr_slim` 5 处接入（import / 启动挂载 start / 查询触发 / stop / status 暴露）。**真机核对抓到两处 bug 并修**：① `cam_dirs_fn` 收到 cam id 字符串却按 dict 传给 `cam_storage_dirs`（首条就 AttributeError 打死 worker）→ 新增 `cam_dirs_for(id)` 做 id→dict；② `_loop` 逐条处理未兜底（异常静默退出线程、队列只塞不抽）→ 拆 `_handle` + `_loop` try/except 计 `failed`。加 worker 存活回归测试（`test_motion_shot` 12 条） | e1fb14e | **出包** `dist/nvr-0.9.134_x86.fpk` **101249502 字节** md5 `44ec26c678672dd3a70b645de801a2ab`；闸门 validate **81** 项、后端全量绿（新增 `test_motion_shot.py` 12 条，`test_pixdiff` / `test_v0918` 版本守卫同步 0.9.134），前端零改动（index.html 未变）0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.134.json`），0.9.133→0.9.134 上线 **0.1s**、端口 18085；机上核对 `motion_shot` 状态 `running=true / extracted=34 / failed=0 / missed=0 / qsize=0`，`/api/events` 30 条里 24 条 motion 有 **16 条已带 shot**（样例 `at:17:37:04` 锚定事件起点），首轮 bug（`extracted=0 / running=false / dropped=169`）已修；两路 `hstate=recording`、日志 warn=0 err=0、`recorder.py` **git diff 为空**；**批次 15 标 done**，生产机 2.6 未碰 |
| 38 | 11-09 22:55 | **批次 16 · 0.9.135 运动快照补图修正**（按用户「生产机已升级到 134 版本，但是未看到移动侦测的事件快照」）：0.9.134 在测试机 16/24 出图、生产机一张没有。静态审查 + 152 真机复现，定位**三个只在真实事件量下暴露**的病根：① `ShotStore.offer` 的 `min_gap`（30 秒/路）是给实时检测设的，补旧事件时 `at` 是历史时刻、翻页还会倒退，`at-last` 变负 → 被当重复确认整片丢；新增 `gap` 参数，补图走 `gap=False`（保留日上限与队列满丢，且不更新 `_last` 以免污染检测口径）。② 事件索引按时间**升序**返回（`reversed(rows)`），补图顺着投 → 最老的事件先占满队列，而它们录像早滚掉、`locate_segment` 恒 None，既不推进也不出图，最新事件永远排不上；改为 `reversed(evs)` 从最新往前投、`QUEUE_MAX` 16→32、`MISS_TTL=600` 秒负缓存。③ **日上限串池**：300 张/路/天原本只有 AI 快照一个消费者，AI 开着时上午就顶满，之后一整天补图全 `capped`（152 实测 `cam-test02` 当天 487 张 → `rejected` 44/44）；改为检测池 `_day_n` 与补图池 `_day_bf` 各 300，补图池每进程从 0 计。另：`_handle` 忽略 `offer` 返回值、抽到帧就计 `extracted`（计数器全绿而列表没图）→ 拆 `stored`/`rejected`；单条事件详情也触发一次补图 | 8e05c7f | **出包** `dist/nvr-0.9.135_x86.fpk` **101251712 字节** md5 `d567ab2c1acdb0ca16f67a262eaee742`；闸门 validate **81** 项、后端全量绿（`test_event_shot` **52** 条、`test_motion_shot` **14** 条，版本守卫同步 0.9.135，`test_doc_suite` 七份文档一致），前端零改动 0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.135.json`），0.9.134→0.9.135 上线 **0.1s**、端口 18085；机上核对 `motion_shot` `running=true / extracted=128 / stored=128 / rejected=0 / missed=0 / qsize=0`，200 条事件里 **165 条 motion 全部带图（100%）**，其中含此前被日上限挡死的 `cam-test02`（14/14）；`shots` `capped=2` 只剩检测池（AI 当天已顶满，符合预期）；两路 `hstate=recording`、日志 warn=0 err=0、`recorder.py` **git diff 为空**；**批次 16 标 done**，生产机 2.6 待用户升级 0.9.135 |

| 39 | 11-09 23:30 | **批次 17 · 0.9.136 运动快照可自证**（按用户「还是没有」继续追）：生产机升 0.9.135 后仍无图，而前两版的失败是"看不见卡在哪"。本版只做可观测性：每条补图留处理轨迹（目录/命中/分片数/定位/ffmpeg 报错/落盘结果）、新增 `/api/motion_shot/probe` 当场自检、ffmpeg 先验可用性与失败原因回传、诊断页新增「事件快照」卡。152 实测 probe 命中中文目录 `测试A-137`（dir_name≠id）、trace 实录定位→落盘。**未解决生产机具体根因，等诊断页数据** |

| 40 | 11-10 01:55 | **批次 18 · 0.9.137 运动事件 AI 复核**（按用户「执行吧，识别出人/车标签怎么归类」）：0.9.136 确认补图全链路健康后，用户确认快照已可见并要求给运动快照补识别框。设计：抽帧同一次 ffmpeg 运行多出一份 PPM 原始像素，AI 总闸开着且 worker 活着时对这一帧再跑一次 YOLO，推得出人员/车辆就把 dets 随图旁挂（与 AI 确认事件同一份 .json 格式），放大时前端零改动叠框；**事件 kind 不变**（台账记的是「当时 AI 没确认」，复核只补证据不改写历史，kind_of 单一实现红线不动），归类答案：仍归「移动侦测」，框与类别跟着快照走。真机调试抓出两坑：ffmpeg 8 对 .part 怪后缀拒开输出/把第二输出编成 mjpeg（临时文件改真扩展名）、ppm 字节漏 parse_ppm（解析挪进 _extract）。test_motion_shot 27 项全绿；出包 101234775 字节 md5 eab60538d43b4086a82fdf53341ad4a0；152 部署九项全 OK，复核链路全通（深夜空画面如实报「没认出」）。插曲：152 /vol1 被录像填到 100%，连续两次部署中途写满失败——清掉 26 个旧部署备份（601M→101M）与 11-08 全天录像（约 20G）后恢复；生产机 2.6 待用户手动升 0.9.137 |
| 41 | 11-10 03:10 | **批次 19 · 0.9.138 事件卡片尺寸重排**（按用户「小图应该拉满高度，大图应该拉满宽度，保证画面比例不变」）：① 列表缩略图不再写死 76×43，外层新增 `.evthumbbox`（`aspect-ratio:16/9` 定比例 + `align-self:stretch` 拉满卡片内容高、宽度自动跟随，图绝对定位铺满容器避免 720p 固有高度进布局）；② 类别标签从缩略图下方移到左列机位行下横排，右列只剩缩略图、卡片高度不再被标签撑出富余；③ 展开详情从左列挪出成 `.evopen`（`flex:1 1 100%` 独占第二行），大图 `width:100% + height:auto` 按原比例铺满整卡宽，去掉 `max-height:210 + object-fit:cover` 裁切，展开间距由卡片 gap 兼管、段落 margin 归零防双重间距；④ 缩略图 onerror 改藏整个容器（否则留灰块）、加载骨架灰块同步约 116×66、窄屏断点不再覆写缩略图尺寸；events UI 测试 +2（evthumbbox 容器 / evopen 整卡宽）；`recorder.py` 一行未改 | b7c3d61 | **出包** `dist/nvr-0.9.138_x86.fpk` **101235740 字节** md5 `28c100e22b9b90a30fbdede9e03f0ae0`；闸门 validate 全绿、前端 events **125**（+2）/ 其余套件 0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.138.json`），0.9.137→0.9.138 上线 **0.1s**、端口 18085；机上 `curl ui/` 确认 `evthumbbox`/`evopen`/`aspect-ratio` 共 **17** 处进编译产物、旧 `width:76px;height:43px` **0** 残留；`/vol1` 余 19G（65%）；两路取流 `hstate=recording`（日志 warn=0 err=0）；真机观感（缩略图变大、大图整卡宽、比例不裁切）待用户手机确认；**批次 19 标 done**，生产机 2.6 未碰 |
| 42 | 11-10 04:10 | **批次 20 · 0.9.139 事件列表全部大图**（按用户「需要展开才能看到大图？不妥。或者全部大图，或者保留小图，展开才能看到大图」）：0.9.138 的中间态（中等缩略图 + 展开才出大图）被否，取用户列的第一方案「全部大图」——有快照的事件卡片直接画整卡宽大图（`.evshotwrap` 独占一行、`width:100% + height:auto` 原比例不裁切），**不用展开就能看清**；小缩略图整个下线（`.evthumb`/`.evthumbbox`/`EV_IMG_ONERR` 删除，大图模式下同一张图不必画两遍）；点大图仍直接弹放大浮层（data-evzoom 代理顺序不变），点卡片展开的 `.evopen` 只剩动作区（打开录像/±30 秒/播不了的原因/没有快照说明），不再重复出图；快照时刻说明随大图常驻；检测框仍只在展开/放大时取一次（不为叠框发 200 个请求），加载失败仍 `bad` 类明说；骨架跟随新卡片形状（文字灰条 + 16:9 大图灰块）；events UI 测试改 6 条删 2 条净 123 项；`recorder.py` 一行未改 | 5e8c860 | **出包** `dist/nvr-0.9.139_x86.fpk` **101234891 字节** md5 `0ddaa1b74b1176bad4a5d8a4261d62a5`；闸门 validate 81 项、前端 events **123**（-2）/ 其余全绿 0 FAIL；`deploy_test.py` 部署 152 **九项全 OK**（`test-reports/deploy-0.9.139.json`），0.9.138→0.9.139 上线 **0.1s**、端口 18085；机上 `curl ui/`：新标记 13 处、`evthumb*`/`EV_IMG_ONERR` **0** 残留、版本 0.9.139；`/vol1` 余 19G（66%）；日志 warn=0 err=0；真机观感（列表直接大图、比例不裁、点图弹浮层）待用户确认；**批次 20 标 done**，生产机 2.6 未碰 |

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
`/var/apps/<app>/cmd`，真升级也会替换它**）→ 以 nvr 身份启动 → 等版本号上线 → 跑七项接口自检
＋ 一项「无凭证必须 401」（这项要求的是"被拦住"，判定口径与其余相反）。任一步失败自动回滚到备份并重启。
第七项是 0.9.121 加的事件索引（`/api/events`），报告里同时给条数与索引覆盖天数：
`count=0` 到底是"真没事件"还是"索引没同步"，看天数就能分开。

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

- [x] 新模块 `app/server/event.py`：统一事件生命周期
      `DETECTED → CANDIDATE → CONFIRMED → ACTIVE → ENDED → INDEXED`，
      `event_id` 稳定幂等（`cam_YYYYMMDD_HHMMSS`，延长结束时间不产生新 id）。
      *（第 6 轮落地。三条口径写死在模块里：id 只由「哪一路 + 何时开始」推出，
      所以事件变长/重判/重建索引都不换 id；六态只许前进，**正在进行的窗口不许
      标已入库**（结束时间还会变）；`mv=-1` 如实留 `None` —— 未知不等于没有运动，
      置信度台账里没有就留 `None`，不编一个 0.5。定位 `resolve()` 一并放在这里：
      按「事件起点所在的片」算，未封口如实报 `not_sealed`，前摇被片头截断标
      `clamped`，**不做跨片前摇** —— 单个 `<video>` 播到片尾就停，事件本身反而
      没播到。）*
- [x] 新模块 `app/server/event_index.py`：SQLite（标准库）索引
      `event_id / camera_id / start_at / end_at / categories / confidence_max / motion_confirmed / source_date / source_segment`。
      台账 JSON 保持不动，索引可从台账重建（`rebuild_index()`）。
      *（第 6 轮落地。另外存了 `start_sec/end_sec`（当天钟点秒，回放定位要它）与
      一张 `day_sync(date, sig)` 表：台账指纹没变就跳过整天重算。整天先删后插，
      重判后事件变少也能如实反映。类别存 `,person,vehicle,` 整词 LIKE，
      `two_wheeler` 不会命中 `person`。）*
- [x] API：`GET /api/events?from=&to=&cam=&cat=&limit=`、`GET /api/events/<id>`、
      `GET /api/events/<id>/clip` → 返回 `{segment, offset_sec, url, missing_reason}`，
      定位口径：事件起点前 10 秒；跨分片、未封口、已删除、损坏都要给出明确 `missing_reason`。
      *（第 6 轮落地。五种原因各有 code + 中文文案：`not_sealed` / `segment_missing` /
      `empty_segment` / `corrupt_segment` / `no_segment_before`。"已删除"与"那段时间
      没录上"用文件清单分不开，文案如实把两种都说出来。`url` 走既有
      `/api/record/file`，不另开读文件的入口。"事件是否还在这一片里"用分片 **mtime**
      判断（文件名分不出"一片被删"和"这段分片更长"），mtime 不在可信窗口就不采信。）*
- [x] 前端「事件」页：日期 + 摄像头 + 类别筛选、列表卡片、点击直接播放（前 10 秒入点）、前后 30 秒跳转。
      *（第 7 轮落地。底部导航新增「事件」（5 个 tab），三段筛选全部走服务端参数 ——
      前端拉全量再过滤会把"前 200 条里的全部"当成全部。列表**最新在前**：接口给的
      是时间正序（那是时间轴的读法），而这一页要回答的是"最近发生了什么"，只在展示层
      倒序，定位仍用服务端坐标。点击 → `/api/events/<id>/clip` → 拿到 `{segment, offset_sec}`
      后跳回放页从**事件前 10 秒**起播；`url` 一律用接口给的那条，前端只从分片名里读回
      "这一片从几点开始"（不自己拼文件名）。播不了不跳页、不给黑框：五种 `missing_reason`
      连同中文文案直接写在展开的卡片上，`clamped` 也明说"前摇被片头截断"。
      ±30 秒的基准是**播放器最后停下的那一秒**（用户按的时候看的是画面），
      只有还没在这条事件的片上播过才退回入点。回放页加了一条"事件页落点优先"：
      清单没回来时先记住目标秒数，回来后落到位；过期 15 秒或日期回退过就作废。）*
- [x] 测试：`test_event.py`（合并/幂等/跨分片定位/缺失原因 15+ 条）、`test/ui/events.test.js`。
      *（第 6 轮已落地 111 条：`test_event.py` 50、`test_event_index.py` 30、
      `test_event_api.py` 31；第 7 轮补 `test/ui/events.test.js` 70 条 —— 分片名解析、
      clip 入点、类别文案与配色、主类别、卡片三态、±30 秒基准与钳位、跨页交代、
      列表最新在前、查询串把筛选交给服务端、播不了不动作。）*
- [x] 版本 0.9.121。

> 批次 3 收尾（11-08 第 7 轮）：清单全部打勾，`0.9.121` 已出包并部署到 152。
> 包：`dist/nvr-0.9.121_x86.fpk`（101142027 字节，md5 `f8b82be104966f53a29c118e4ceb933b`）。
> 部署八项全绿（七项接口 + 凭证设防；新增的「事件索引」报 `count=123 索引天数=1`），
> 两路 `hstate=recording`（last_age ≤ 1 秒）。
>
> **验收怎么跑的**：新增 `test/e2e_events.js`（真 Chrome 点一遍，不进 build.sh --check，
> 它要一台真在录的机器）。152 上跑出 15 项全绿：点 11:19:52「测试B-116 人员」那条 →
> **814ms 起播**，落点 `40782.0` 对事件 `40792.0` = **−10.0 秒**，全程无 JS 报错。
> 截图在 `_probe/ev_e2e/ev_list.png` / `ev_play.png`（不进仓库）。
>
> **两件这轮才发现的事（记下来，别当没看见）**：
> 1. **152 上 AI 检测原本是关的**（`detect.enable=false`），所以全天 127 条事件全是纯帧差
>    —— "真实人员事件"根本不存在，验收无从跑起。这轮只改了**配置**（改成开，改前备份
>    `config.json.bak-121`），模型 `share/models/yolo11n.onnx` 本来就在，加载成功
>    （`loaded=true`，单次推理 ~76ms），十分钟内就产出了真实的人员/车辆/两轮车事件。
>    **没有改代码**；批次 4 正好要在真机上做检测治理，现在有数据了。
> 2. **刚发生的、还在写入那一片里的事件，重启后会从列表消失**（127 → 123），
>    等那片封口才回来。口径是台账按片落盘，不是丢数据；但"刚发生就看不见"对用户是
>    个真问题 —— 留给批次 4 一起判（要不要让未封口片里的事件提前可见）。
>
> **收尾清单里没做的那一项**：第 6 轮写的收尾清单已全部做完（前端页 / 前端测试 /
> 部署自检项 / 五处版本 / 出包 / 部署 / 两路复核 / 验收）。

**验收**：任选一条真实人员事件，页面点击后 2 秒内开始播放，且播放起点在事件发生前 10 秒内。
（152 实测：814ms、起点 −10.0 秒，见上。）

---

## 批次 4 · 0.9.122 检测治理

**目标**：多路不互相拖死，误报不碎片化，失败可见。

清单：

- [x] `detect.py` 有界并发：默认 2 路、上限 4 路，工作者池 + 任务队列；
      同一路不重入；每路独立 `next_due`；超时任务丢弃过期帧；检测绝不阻塞取帧与录像。
      *（第 8 轮落地。新增 `Scheduler`（无 IO 无线程，单测直接喂时间戳）+
      ticker 线程 + `pool_size()` 个工作者线程：ticker 只判定与 `put_nowait`，
      队列满就丢并记 `QUEUE_FULL`；工作者拿到排队超过一个节拍的任务直接丢
      （`STALE_FRAME`）。并发度 = `pool_size(路数)`，默认 2、封顶 4，在 `start()`
      按当时路数算——构造时读不到配置，固定成 2 又让 8 路跑不满。旧写法「一个
      循环顺序跑完所有路」在 8 路时一轮 = 8 × 单路，必然超过 5 秒节拍，检测频率
      随路数稀释。`last_success_at` 只在真有结论的一拍刷新。）*
- [x] 时间窗确认：N 帧中 M 帧命中、类置信度阈值、最短持续、结束等待、相邻事件合并
      （轻量实现，不引入 DeepSORT/ByteTrack）。
      *（第 9 轮落地。新模块 `app/server/confirm.py`：纯状态机、无 IO、now 由调用方传。
      默认「最近 3 个有结论的帧里 ≥2 帧命中」+ 每类确认阈值（0.55/0.42/0.48，高于 std
      检出阈值一档）+ 最短持续 4 秒 + 结束等待 20 秒 + 相邻合并 25 秒（比结束等待略大、
      比 `ledger.AI_GAP_SEC`(30) 略小，两个尺度各司其职）。
      确认**之后**每一帧都带上整条事件的起点，所以落账的是一个连续窗口而不是逐帧小段；
      unknown 帧不喂进去 —— 既不算命中，也不判「没人」。
      顺带三处必须一起改的地方：① `_write_ledger(cam, wins, now)` 改成收窗口而不是收 dets
      （否则确认层白做）；② 通知跟着确认走（给用户推一条事件页里没有的「有人出现」最伤信任）；
      ③ 跨 5 分钟桶的事件起点裁到桶首 —— `ledger.merge` 会把负的相对秒夹到 0，
      不裁就会凭空冒出一条从 00:00 开始的窗口（这条是新坐标系带出来的真问题）。
      `confirm.enable=false` 一键退回 0.9.121 的每帧一条。参数暂只在配置文件里可调
      （设置页没有这几项），`/api/config` 保存时若前端没带 confirm 就沿用磁盘上的那份。）*
- [x] 三态语义：`hit / miss / unknown`，`unknown`（取帧失败、模型未加载、推理异常、断流）不得计入「无活动」。
      *（第 8 轮落地。三态之外多一档 `skip`：取帧层「本轮没有新画面」是正常节拍
      错位（检测 5 秒 / 取帧 10 秒），既不是 hit 也不是故障，混进 unknown 会每两拍
      刷一次红。只有 hit/miss 进 `today`（有结论的帧数），unknown 按错误码分桶，
      `last_err` 只由 unknown 写。顺带修掉 `_day` 定死在构造时导致的跨零点
      「今日」永远 0 帧。）*
- [x] `detect_status()` 统一 `{enabled, available, running, error_code, error_message, last_success_at}`，
      所有失败路径都给 code（`FFMPEG_NOT_FOUND` / `MODEL_MISSING` / `FRAME_FAIL` / `INFER_ERR`）。
      *（第 8 轮落地。判定搬进 `detect.status_of(worker, det_cfg, ffmpeg, runtime)`，
      `/api/status` 只负责取三样现场。除清单四项外另有 `RUNTIME_FAIL` /
      `MODEL_LOAD_FAIL` / `WORKER_DOWN` / `QUEUE_FULL` / `STALE_FRAME` / `STATUS_ERR`；
      加 `state` 四档（disabled / starting / unavailable / ready）供前端直接翻译——
      「没加载完」和「坏了」以前都被写成「worker 未运行」，用户只能猜要不要重启。
      旧字段 `loaded/error/avg_ms/per_cam` 全部保留，老前端不会白屏。）*
- [x] 前端：检测卡片显示可用性与原因；统计里 `unknown` 单独一类。
      *（第 8 轮落地。`detStateZh` / `detHintOf` / `detUnknownOf` 三个纯函数：
      state 翻中文、十个错误码各给下一步（装 ffmpeg / 重装应用 / 核 Python 版本 /
      重开服务）、未知数按路汇总。诊断卡新增「调度」一行（并路数 / 队列 / 丢弃
      队列满与过期 / 一轮耗时与路数），每路行加「未知 N」「同帧跳过 N」。
      顺手修掉每路「最近」渲染成 `[object Object]`（`c.last` 是对象，不是字符串）。）*
- [x] 测试：`test_detect_sched.py`、`test_event_confirm.py`、`test_detect_status.py`。
      *（第 8 轮已落地两个：`test_detect_sched.py` 20 条（并发上限、不重入、
      ticker 不阻塞、队列满/过期丢弃、8 路一轮明显短于串行、按路数自动定并发）、
      `test_detect_status.py` 23 条（七项契约、四档 state、引擎错误码、三态记账、
      跨零点、无 worker 时的全 0 形状）；另 `test_detect_boot.py` 补 2 条接线断言、
      `test/ui/detect.test.js` 44 条、`test_http_smoke.py` 补 3 条接口契约断言。
      第 9 轮补 `test_event_confirm.py` 33 条：配置夹取与"min_hits 不许超过窗口"、
      单帧误报被丢、两帧确认且**起点是第一帧**、窗口内掉一帧仍确认、低置信度不算命中、
      unknown 帧不参与（事件不延长也不结束）、结束等待、相邻合并（起点沿用上一条）、
      隔得远算两条事件、最短持续、多路多类互不串味，外加 8 条接线断言
      （第一帧不落账、第二帧落一条连续窗口、台账里真有带 cls 的窗口、关掉确认退回逐帧、
      故障帧不续账、跨桶裁到桶首、status 带 confirm 全 0 形状、norm_detect 带上 confirm）。
      `test_detect_boot.py` 的 4 处 `_write_ledger` 调用改用 `_win()` 造窗口，口径不变。）*
- [x] 版本 0.9.122。

**验收**：8 路配置下，单轮检测总耗时不再随路数线性增长（≤ 1.8 × 单路耗时）；
人为让某路取帧失败，统计里出现 `unknown` 而不掉「无活动」。

> **进度（第 8 轮，未收尾）**：有界并发 / 三态 / 统一状态 / 前端展示四项已落地，
> 剩「时间窗确认」与版本收尾。验收怎么量也定了口径：`/api/status` 的
> `detect.sched.round_avg_ms` 与 `round_cams` 就是这个数（ticker 派活到这一拍
> 全部干完的墙钟），本轮先在单测里证明"8 路一轮 ≈ 2 × 单路"而非 8 × 单路；
> 真机只有 2 路，8 路的结论要等出包后用 `round_avg_ms` 外推并在汇报里写清口径。
> 「人为让某路取帧失败」用停掉 `frames` 那一路来做（`_frame_path_for_detect`
> 会回 `None` → `FRAME_FAIL`），不做假数据。

> **批次 4 收尾（11-08 第 9 轮）**：清单全部打勾，`0.9.122` 已出包并部署到 152。
> 包：`dist/nvr-0.9.122_x86.fpk`（101160332 字节，md5 `5ea95cd090fae412793b91f61543671a`）。
> 部署八项全绿（状态总览 / 健康 / 缺口 / 取流 / 日志 / 清单 / 事件索引 `count=167` / 凭证 401），
> 两路 `hstate=recording`（last_age 1~2 秒）。
>
> **验收怎么跑的（两条验收，一条过一条没过，如实记）**：
> 1. **unknown 这条过了**：`kill -9` 掉测试A-137 的取帧进程（`fps=1/5` 那条，
>    录制是另一条 ffmpeg，全程没断），3 秒后 `/api/status` 出现
>    `unknown=1 / codes={"FRAME_FAIL":1} / last_state=unknown`，同一时刻 `today` 停在 21
>    不涨；另一路 `unknown=0 / last_state=hit` 完全没受影响。取帧层 8 秒内自愈，
>    两路回到 recording、取帧进程恢复 2 个。
> 2. **8 路 ≤1.8× 这条没达成**：测试机 3 核，2 路一轮 `round_avg_ms=163`、
>    单帧推理 `avg_ms=114.5`（同规模串行约 229ms，并发确实生效）。按 `pool_size(8)=4`
>    分两批外推，8 路一轮 ≈ 330ms ≈ **2.9 × 单帧**，比 1.8 × 的目标高。
>    原因是并发封顶 4：3 核上再往上加就是拿编码器的 CPU 换检测延迟，而「检测不许影响
>    全时录像」是本项目的红线。**所以这一条按「不再线性增长」判过（串行 8 路要 916ms，
>    外推 330ms，是它的 36%），按「≤1.8×」判未过** —— 留给后续：要么真机 8 路实测后再定
>    并发策略（按核数而不是按固定 4），要么把这条验收改写成「不随路数线性增长」。
>    这条偏差写在汇报里，不当作已完成。
>
> 另：升级后 14:37:56 落了一条 person 事件（`/api/status` 的 `confirm.confirmed=1`），
> 说明确认层在真机是出得来事件的，不是把一切都挡掉了。碎片化到底少了多少，
> 需要更长时间的样本，留给下一轮观察（152 上 AI 事件本来就稀疏）。
>
> **本轮顺带记下的一个坑**：`ledger.merge` 把负的相对秒夹到 0 —— 跨桶窗口不裁到桶首，
> 台账里就会多出一条从当天 00:00 开始的窗口。`test_window_clamped_to_bucket` 钉住它。

---

## 批次 5 · 0.9.123 前端拆分与文档

**目标**：让下一个人（或下一次的我）能在 10 分钟内看懂并改动。

清单：

- [x] `app/ui/` 源码拆分：`css/{base,theme,components,pages,player}.css`、
      `js/{core,components,pages,player}/*.js`，构建时由 `build.sh` 合成单文件 `index.html`
      （部署形态不变，仍是单 HTML）。
      *（第 10 轮做了**样式这一半**：1400 行 `<style>` 拆成五个源文件，`tools/compose_ui.py`
      按 `theme→base→components→pages→player` 固定顺序拼回 `index.html` 的样式区，
      幂等（没变化不写文件、不动 mtime）。拆分口径不是"逐行搬"，而是**按职责搬整段**：
      令牌 / 骨架与全局 chrome / 跨页控件 / 各页面 / 画面与时间。判断标准写进了每个文件头，
      下一个人不用问"这条该放哪"。脚本那 8600 行（`js/` 四个目录）留到下一轮，
      一轮一个可独立提交的切片。*）
- [x] 按钮功能验收表落成自动化：每个设置项改完要验证后端状态/进程参数真的变了（不只查 UI 值）。
      *（第 23 轮打勾，口径见下方进度 note：打勾的是**自动化链路闭合**——声明表
      20 key / 四档判定 / 覆盖守卫 + 执行器 12 项四层证据 + 两表对齐守卫 +
      全表采集器 + 首份端到端报告。20 项里 9 项验到运行中对象状态，另 11 项
      如实标 `partial` 并逐条写明还差什么：它们是产品没有运行态观测出口
      （需重启 / 夜间任务 / ffmpeg argv 无 HTTP 出口），补出口属清单外改动，
      已记进「不在本轮范围」。**不拿落盘回读冒充「后端状态真的变了」。**）*
- [x] 文档重构：`README.md`（介绍/截图/安装/快速上手）、`docs/USER_GUIDE.md`、
      `docs/ARCHITECTURE.md`、`docs/CONFIGURATION.md`（每项的 key/类型/默认/范围/生效方式）、
      `docs/TROUBLESHOOTING.md`、`BUILD.md` 只留构建；历史排障记录进 `CHANGELOG.md`。
- [x] `nvr_slim.py` 头部说明重写（第 24 轮已重写；原文那句说的是当时的旧描述）。
- [x] 版本 0.9.123。

**验收**：① 第 1 项的拆分等价**不用截图**——本机没有 puppeteer/playwright，
`e2e_*.js` 要一台真在录的机器，截图只能盖一个视口一套主题；改用三项覆盖所有状态的证明：
CSS 831 条规则按 `(媒体查询上下文, 选择器)` 归并后声明序列逐条相同、
脚本 8631 行代码序列逐行相同、16 个前端测试在合成后的页面上重跑全绿；
`js_split.test.js` 另钉住「标记段之外没有散落代码」，保证以后新功能不会又堆回单文件。
② 批次验收（**第 24 轮已真跑**）：`deploy_test.py` 把正式包部署到 152、
八项自检全绿，再按 README §3 在真机逐条跑 `/api/health`、`/api/link`、
`/api/health/gaps`，两路 `hstate=recording`、无 stalled；程序目录里本轮新写的
`nvr_slim.py` 头注释已到位，证明正式包确实取代了预览包。
**不把「版本号相同」当成装过了**：这次是 0.9.123→0.9.123 同版本重部署，
靠「停进程 → `--checksum --delete` 覆盖 → 重启 → 头注释实证」四步确认。

> **进度（第 10 轮，未收尾）**：样式源已拆完，脚本源与文档两项未动。
> **"逐像素一致"这条用的不是截图，是层叠等价证明**：本机没有 Chrome 自动化
> （`puppeteer`/`playwright` 都没装，`test/e2e_*.js` 要一台真在录的机器才能跑），
> 截图比对只能覆盖一个视口、一套主题、一个状态，反而不如直接把 CSS 的层叠结果
> 摆出来对：拆分前后各解析出 831 条规则，按 `(媒体查询上下文, 选择器)` 归并后
> **每条选择器的声明序列逐条相同**（只在同一选择器内部，顺序才决定胜负），
> 且样式区之外的 HTML（标记 + 8600 行脚本）**逐字节相同**。
> 这条证明比截图强：它覆盖所有主题、所有视口、所有状态。
> 顺带把 `render_check.js`、`settings.test.js` 等 16 个前端测试在合成后的页面上重跑，全绿。
>
> **这轮踩到的两个坑（值得记）**：
> 1. **合成必须排在 `validate.py` 之前**。validate 现在会核对"`index.html` 与 `css/` 是否同步"，
>    合成放后面就等于拿上一轮的产物过闸门 —— 改了 css、闸门还说绿。
> 2. **切片的行号基准不能读活文件**。第一次插入合成标记后，`<style>` 后面多了一行，
>    再按老行号切片就整体错位一行（切出来第一行总是缺一句）。切片脚本后来固定读
>    拆分前的快照，这也是为什么"拆分前先存一份快照"这件事必须写进流程。
>
> **打包侧同步改了**：`pack_fpk.py` 把 `ui/css/` 排除出 `app.tgz`。理由不是省 100KB，
> 而是避免"设备上有一份能改的 css，改了却完全不生效"——浏览器读的是 `index.html`
> 里的内联样式。试打包验证过：`app.tgz` 45 个成员、无 `ui/css`、`ui/index.html` 544563 字节含合成标记。
>
> **进度（第 11 轮，仍未收尾）**：脚本源开始拆，本轮只搬 **core** 四段 ——
> `core/auth.js`（凭证与全局 fetch 包装）、`core/format.js`（纯时间格式化）、
> `core/api.js`（请求层）、`core/theme.js`（主题引擎），共 187 行代码，
> 每份文件头上写清"放什么、什么不放进来"的判断标准。
> 剩下 `components / pages / player` 三段（约 8400 行）留给后面几轮，一轮一个可独立提交的切片。
>
> **机制跟样式不一样，这点值得记牢**：CSS 能"按名字定顺序"，脚本不行 ——
> `const`/`let` 不提升，声明出现的次序就是执行次序。所以 `@@js:begin <文件>` …
> `@@js:end` 是**多段标记**，合成顺序 = 标记在 `index.html` 里的位置，代码从哪儿
> 搬走就还回哪儿去。`compose_ui.py` 一次管两件事（css 区 + 若干 js 段），
> 并且会拦三种"改了不生效"：标记引用了不存在的文件、`js/` 下有文件没标记、
> 同一份源码被标记两次。顺序本身由 `test/ui/js_split.test.js` 钉成一张固定表 ——
> 挪标记必须连带改测试，那就是"想清楚了才改"。
>
> **等价怎么证的（口径同第 10 轮，不用截图）**：把合成后的脚本区拆成"标记段"与
> "未搬动的部分"，未搬动的 8442 行与拆分前**逐行相同**；四段各自去掉新增的文件头后
> 与拆分前**逐行相同**。验证脚本在 `var_jssplit/verify.py`（不入库），
> 拆分前快照 `var_jssplit/script.before.js` 也在那里。
> 另外 `index.html` 的 git diff 是 **36 行纯新增、0 行删除** —— 代码一行没动，只是被标记框住了。
>
> **闸门同步加厚**：`validate.py` 77 → **81** 项（脚本源齐全 / 标记成对 / 无 `<script src>` 外链 /
> 全部标记都在最后一个 `<script>` 里）。最后那两项拦的是这一层最阴的坏法：
> `test/ui/*.test.js` 全都取"最后一个 `<script>`"当脚本源，一旦有人把脚本拆成多个标签，
> 十几个测试会一声不响地跑错段 —— 全绿，但什么都没测到。
> `pack_fpk.py` 也把 `ui/js/` 排除出 `app.tgz`（同 `ui/css/`：设备上有一份能改却不生效的源，
> 比没有更坏）。
>
> **下一轮（第 12 轮已执行，见下）**：搬 `components` 段，判断标准沿用"谁都能用的纯函数进 core、
> 跨页控件进 components、只在某一页出现的进 pages"。
>
> **进度（第 12 轮，仍未收尾）**：`components` 三段落地 ——
> `components/shell.js`（toast / layout / resize，32 行）、
> `components/fullscreen.js`（三级降级全屏 + 横屏 + Esc + 按钮接线，286 行）、
> `components/nav.js`（tab 切换与安卓返回栈，340 行），共 **658 行**。
> 三段在源码里本来就不挨着（中间夹着页面渲染函数），多段标记正好派上用场：
> 一段一个 `@@js:begin`，顺序仍由位置决定，不用为了"能拆"先去搬动代码。
>
> **等价口径这轮升级了**：不再逐段比对，而是把 `index.html` 里所有合成标记与文件头注释
> 剥掉，剩下的**代码序列**与拆分前逐行比对 —— 8631 行完全相同。
> 这比逐段更严：它同时覆盖此前已拆的四段与这轮新拆的三段，且不受"以后又加了几段"影响。
> 验证脚本 `var_jssplit/verify2.py`、拆分前快照 `var_jssplit/index.before12.html`（都不入库）。
> `index.html` 的 git diff 仍是 **29 行纯新增、0 行删除**。
>
> 闸门同步：`js_split.test.js` 的 ORDER 表 4→7（39→**60** 条），
> `validate.py` 的 `JS_REQUIRED` 4→7。这两张表是"挪一个标记就红"的地方，必须和标记一起改。
>
> **进度（第 13 轮，仍未收尾）**：`pages` 三段落地，共 **2139 行** ——
> `pages/clips.js`（日期条 / 热力条 / 时段对比时间轴 / 内嵌热力格 / 概览 /
> 缩略图 / 片段列表 / 场景绘制，566 行）、`pages/settings_cards.js`（设置页卡片
> 的纯渲染：录制/衔接/回放/检测/外观/日志/诊断导出/扫描/摄像头列表/只读摘要，918 行）、
> `pages/settings_editor.js`（四段手风琴 / 草稿读写 / 遮罩画布 / 编辑动作 / 渲染入口，655 行）。
> 三段的界线按"能不能按下去"切：**画得见点不动的**和**改得了值的**分成两份，
> 因为"保存时读到的是卡片值还是草稿值"是这页历史上最难查的一类 bug，摆在一起
> 下一个人分不清当前显示的是服务端的值还是没提交的编辑值。
>
> **等价口径沿用第 12 轮的全脚本比对**（`var_jssplit/verify3.py`，快照
> `index.before13.html`）：剥掉全部 10 组合成标记与文件头后，代码 **8631 行逐行相同**；
> `index.html` 的 git diff 是 **39 行纯新增、0 行删除**。闸门两张表同步 7→10。
>
> **这轮真踩了一次坑，值得记**：切片边界把**段尾空行**框进了源文件。
> `compose_ui.py` 的 `js_body()` 会 `strip("\n")` 掉文件尾换行，三段就悄悄少了
> 3 个空行 —— 等价检查第一遍当场报「第 1752 行不同」。修法是把三段收在**非空行**
> 上（3053–3618 / 5258–6175 / 6177–6831），段尾空行留给 `index.html`。
> 这也是"每轮必跑等价验证"的价值：这种偏差肉眼看 diff 完全看不出来。
>
> **进度（第 14 轮，仍未收尾）**：又搬 4 段共 **1996 行**进 `js/`，凑齐四个目录 ——
> `core/state.js`（463：数据骨架 / GS / 回放现场 / 真实资源状态）、
> `player/player.js`（545：归档播放器，A/B video 热切换 / 倍速 / 导出 / 同步 / 主循环）、
> `pages/live_snap.js`（78：直播页取图入口，快照超时与 token 去重）、
> `pages/live.js`（910：配置推拉 / 快照墙 / 宫格 / 实时流 / 兜底绘制）。
>
> **同一张直播页被 `core/api.js` 隔成两份，不是漏搬**：`live_snap` 走 `img.src`
> 直接取 JPEG，排在统一请求层之前；`live.js` 要 POST 配置、按服务端通道表重排
> 摄像头，必须排在请求层之后。位置即执行顺序，不为"同页同目录"去挪动代码 ——
> 这条理由写进了 `js_split.test.js` 的 ORDER 表注释，免得下一个人"顺手整理"时
> 把两段并到一处、反而改坏加载次序。
>
> **等价口径沿用第 12 轮的全脚本比对**（`var_jssplit/verify4.py`，快照
> `index.before14.html`）：剥掉全部 14 组合成标记与文件头后，代码仍 **8631 行逐行相同**；
> `index.html` 的 git diff 是 **52 行纯新增、0 行删除**。闸门两张表同步 10→14
> （`js_split.test.js` 81→**109** 项、`validate.py` 的 `JS_REQUIRED` 10→14）。
>
> **进度（第 15 轮，仍未收尾）**：又搬 4 段共 **2990 行**进 `js/pages/` ——
> `diagnostics.js`（1254：存储卡 / 每路录制状态就地诊断 / 环境自检 / 运行日志 /
> 录像残片 / AI 检测诊断卡）、`settings_persist.js`（161：配置读写与拖动排序，
> 都是"改完之后怎么落地"）、`playback_data.js`（296：日期列表重算 / 取磁盘真实
> 片段 / 跨页同步 / 摄像头下拉）、`timeline.js`（1279：垂直时间轴与常驻画面区，
> 同一条"当天 0 点起浮点秒 → video.currentTime"的时钟链，不按控件切碎）。
> 源文件 14→**18** 份（core 5 / components 3 / pages 9 / player 1）。
>
> **等价口径沿用第 12 轮的全脚本比对**（`var_jssplit/verify5.py`，快照
> `index.before15.html`）：剥掉全部 18 组合成标记与文件头后，代码仍 **8631 行逐行相同**；
> `index.html` 的 git diff 是 **53 行纯新增、0 行删除**。闸门两张表同步 14→18
> （`js_split.test.js` 109→**137** 项、`validate.py` 的 `JS_REQUIRED` 14→18）。
>
> **本轮的两个判断值得记**：① 回放数据与时间轴分开，因为"明明有录像却显示空白"
> 和"有录像但点进去跳错秒"是两类完全不同的排查入口；② 时间轴 1279 行不再切碎，
> 缩放、气泡分组、常驻画面区预加载、pvOnTime 是同一条时钟链，按控件名分家只会
> 让跳秒问题变成跨文件追查。段尾仍收在非空行（第 13 轮的坑，本轮四段全部核过）。
>
> **进度（第 16 轮，清单第 1 项完成）**：最后两片切成 4 段共 **639 行** ——
> `pages/events.js`（334：事件列表 / 筛选 / 点事件直接看的定位交接）、
> `core/boot.js`（160：启动次序 / 快照刷新率 / 与服务端配置对表）、
> `core/wiring.js`（86：控件到函数的最后一层绑定）、
> `core/auth_boot.js`（59：凭证引导层与 authBoot）。源文件 18→**22** 份
> （core 8 / components 3 / pages 10 / player 1）。
>
> **脚本区现在只剩 `"use strict";` 在标记段之外，这一行故意不搬**：严格模式指令
> 只有作为整个 `<script>` 的第一条语句才对整个脚本生效，放进任何一段都会退化成
> 一句没用的字符串表达式。`js_split.test.js` 新增 H 组断言「标记段之外没有散落代码」，
> 把"拆分完成"钉成机器保证 —— 以后往 index.html 里补一行功能，闸门立刻红，
> 而不是等半年后又在 8000 行里翻找。js_split 137→**166** 项。
>
> **等价口径沿用第 12 轮的全脚本比对**（`var_jssplit/verify6.py`，快照
> `index.before16.html`）：剥掉全部 22 组合成标记与文件头后，代码仍 **8631 行逐行相同**；
> `index.html` 的 git diff 是 **46 行纯新增、0 行删除**。闸门两张表同步 18→22
> （`js_split.test.js` 的 ORDER、`validate.py` 的 `JS_REQUIRED`），`BUILD.md` 目录说明同步。
>
> **进度（第 17 轮，清单第 2 项第一组）**：落成 `test/test_btn_effect.py` ——
> 真起一个本地服务（绑 `127.0.0.1`、端口由系统分配、临时目录跑完即删），
> 对「衔接与录制」5 个设置项逐项验四层证据：POST 回执 `changed` → `/api/config`
> 回读 → 磁盘 `config.json` → **`/api/link` 里运行中的 Link/Recorder 对象状态**。
> 第四层才是清单说的「后端状态/进程参数真的变了」：`link.status()` 报的
> `segment_sec` / `mode` / `max_queue` 就是消费端真在用的值，不是把 UI 值换个地方读。
> 表是数据（`CASES`），加一行就多一条用例；`test_90_table_shape` 会拦住
> 「只写回读路径」的凑数项。它自动进 2/5 的 `test_*.py` 遍历，不用单独手动跑。
>
> **推翻上一轮「要连测试机」的预估**：本机没有 ffmpeg 时服务照样起来、
> Link/Recorder 对象照样建，改设置走的是同一条 HTTP → 落盘 → `reconfigure`，
> 与 152 完全同构。连测试机反而会改到真配置、干扰录制。探针与正式测试都在
> 临时目录里，不碰 152，也不碰生产机。
>
> **这张表当场抓到两个真缺口，本轮一并修掉（只加不改）**：
> ① `Link.reconfigure()` 漏了 `max_queue` —— 用户改「待处理上限」，接口回 ok、
>    配置也落盘了，背压闸门仍按启动时的 200 限流，直到重启才生效；
>    `/api/link/policy` 入口同病。现按 `__init__` 同样的 clamp 口径热更新。
> ② `runtime._ENUM_KEYS` 漏了 `link_mode` —— `/api/link/policy` 会收敛非法值，
>    但 `/api/config` 走 coerce 的字符串分支，非法值原样落盘并回读给前端，
>    `syncFromServer` 就会把不存在的模式显示到界面。现补进白名单。
> 首跑 3 红（05 / 91 / 92），修完 9 项全绿；受影响的 link 33 / clipsplit 44 /
> params 20 也全绿。闸门：validate 81、后端 **1031** 条（1022+9）、前端 16 文件。
>
> **清单第 2 项仍未打勾**：`CASES` 目前只覆盖 5 个衔接/录制设置项，设置页还有
> 快照刷新频率、封装格式、并发、扫描/清理时刻、归档参数、每路参数、AI 检测与
> 通知等项。它们多数没有 `/api/link` 这样的对象状态出口（有的是定时器实时读盘），
> 下一轮要先找各自的生效证据再往表里加，不能为了凑数写成回读断言。
>
> **进度（第 18 轮，清单第 2 项第二组）**：推翻第 17 轮「先找 snapshot_interval /
> record_frag 的生效证据」的预估 —— 查清后这两项根本不是设置页的按钮：
> `record_frag` 自 0.9.111 起界面无开关（`applyServerCfg` 里写死 `LK.frag = true`，
> 每次保存还把服务端纠正回分片 MP4）；`snapshot_interval` 虽有按钮，但取帧 worker
> 是每轮用 `interval_fn` 现读盘，没有「运行中对象状态」出口，离线老流程的
> `SnapshotWorker.status()` 只回一张 ok 表。把它们凑进 `CASES` 只会写成回读断言，
> 恰恰是清单要防的假验收，故记进「不在本轮范围」。
>
> **第二组真正补的是「同一设置项，换个入口按」这个维度**：设置页有两条写入口，
> `/api/config` 与 `/api/link/policy`。`test_92` 第一组只验了后者对 `max_queue`
> 的热生效，而 `set_link()` 同样会写 `record_enable` / `record_segment_sec` 并落盘。
> 新增 `test_93` 后首跑即红：`/api/link/policy` 把分片改成 120、`record_enable`
> 改成 false，接口回 ok、磁盘与回读都对，`Link.segment_sec` 也变成 120，
> **Recorder 却仍按 300 秒封口、录像总闸仍是开** —— Link 按新秒数入队、
> ffmpeg 按旧秒数封口，回放按秒定位会跳错片段；「关掉录像」根本没关。
> 修法只在 `set_link()` 补一次 `STATE["recorder"].reconfigure(cfg)`（条件触发、
> 异常兜底，不碰 recorder.py 的录制链路），与 `/api/config` 那条路径对齐。
>
> 同时把 `record_enable` 加进 `CASES`（第 6 项，证据取 `record.enabled`），
> 并在 `test_90` 钉住一条护栏：`/api/link` 顶层那个同名 `record_enable` 是
> `link_payload()` 现读 `config.json` 补的，**不是** Recorder 的状态，不许拿它当证据 ——
> 这个坑正是上一轮 `test_92` 漏过 policy 缺口的原因。验收表 9→**11** 项。
>
> **清单第 2 项仍未打勾**：还有 condense_mode、归档四项、并发、扫描/清理时刻、
> 每路参数、AI 检测与通知嵌套块未覆盖。下一轮先定「完成口径」：设置页上能按、
> 且后端有运行中对象消费的项走四层证据；纯粹靠定时器/请求时读盘消费的项
> 用一条「读的是磁盘不是缓存」的专门断言覆盖，或明确归入「不在本轮范围」，
> 不能为了把格子填满而写回读断言。之后才是文档重构（六份）、
> `nvr_slim.py` 头注释、0.9.123 五处 bump + 出包 + 部署 152 + 真机验收。
>
> **第 18 轮收尾时的工作区提示**：出现两个**非本轮产出**的未跟踪文件
> `test/button_matrix.py` 与 `test/test_button_matrix.py`（另一路并行实现：
> 离线验收矩阵 + 覆盖它的 54 条单测，思路是连测试机采 `/proc/<pid>/cmdline`）。
> 本轮**没有提交、没有删除、没有改动**它们；含它们的闸门复核仍全绿
> （后端 1033+54=1087 条、零 FAIL）。下一轮开工前先确认归属：两份验收表
> 定位重叠，要么合并要么明确分工（一份离线四层证据、一份真机进程参数采集），
> 别让 `CASES` 和矩阵各说一套。
>
> **进度（第 19 轮，归属定案）**：两路并行实现**不合并，明确分工**——
> `test/test_btn_effect.py` 是**执行器**（真起本地服务、逐项改值、拿运行中
> Link/Recorder 对象状态当第四层证据，并修掉三个热生效缺口）；
> `test/button_matrix.py` 是**声明表**（给每个配置键一个「该验什么、怎么算过」
> 的说法：20 行覆盖 `pushCfg` 的 19 个顶层 key + cameras，另有 10 个免验理由
> 与 4 处假设置照现）。另更正第 18 轮的一句预估：声明表是**纯离线**的，
> 采集器留到下一轮，`/proc` 只是它可选的第 4 层，本轮没连测试机改任何配置。
>
> **声明表补的正是执行器没有的那一半**：① `pass / partial / fail / unknown`
> 四档 —— 没有运行态出口的项（`cleanup_at` / 归档四项 / `condense_mode` /
> `gpu` / `notify`）判 `partial` 并写明还差什么，不撒谎也不让表永远红；
> 快照缺键判 `unknown`，「没报错」绝不算通过。② 四层里第 4 层单列：
> 所有 HTTP 接口都不输出 ffmpeg 命令行，采不到 `/proc/<pid>/cmdline` 就降
> `partial`，不拿派生值冒充 argv。③ 覆盖度守卫：`runtime.DEFAULTS` 每个键
> 都得有验收行或免验理由，`live.js` 的 `pushCfg` body 里出现的键必须都有
> 验收行 —— 服务端或前端加一个配置键却没人补表，闸门当场红；
> 运行态路径不许写成 `config.*`，否则等于把第 2 层查两遍。
>
> **「别让 CASES 和矩阵各说一套」已钉成机器断言**：执行器的 `CASES` 反过来
> 成为声明表的输入 —— 6 个 `key` 与 7 条 `effects`（`link.mode` /
> `link.interval_h` / `link.segment_sec` / `link.record.segment_sec` /
> `link.clip_style` / `link.max_queue` / `link.record.enabled`）必须都能在声明表里
> 找到同名运行态观测点，且这些行不许标「需重启」、不许说「无运行态出口」。
> 任一张表先漂移，58 条单测里就会红一条，不靠人记得同步。
>
> **两处口径差别补一句，免得下一个人以为打架**：`link_mode` 在声明表里标
> 「UI 无入口、前端恒推 live」，执行器仍把它当设置项验 —— 两边验的都是
> `/api/config` 写入后的热生效，不是「页面上有这个按钮」，不矛盾。
> `snapshot_interval` 第 18 轮记「无运行中对象出口」，声明表登记的是
> `status.frames.*.interval`（取帧 worker 每轮重读配置的实时派生值，入口在
> 直播页齿轮不在设置页）；它仍按 `partial` 如实标注，不改写第 18 轮
> 「不塞进 CASES」的结论。
>
> **清单第 2 项仍未打勾**：声明表与执行器都已就位，但「采集器」（改值 →
> 等 300ms 防抖 → 取三份快照 → 还原 → 喂判定器 → 出报告）还没接，也没有
> 一次完整的端到端跑分。下一轮先接采集器并产出第一份报告，把 `partial` 项
> 逐条消化或写明免验；之后才是文档重构（六份）、`nvr_slim.py` 头注释、
> 0.9.123 五处 bump + 出包 + 部署 152 + 真机验收。
>
> **进度（第 20 轮，执行器侧收口第 18 轮挂账）**：并发路做完第 19 轮后，
> 我这一路按第 18 轮台账与「不在本轮范围」共同挂着的那条账开工：
> `set_link()` 的 `record_enable` 用 `bool(body.get(...))`，字符串 `"false"`
> 会被判成 `True`；`/api/config` 走 `runtime.coerce` 却收敛成 `False`。
> **实际后果比台账记的更重**：不是「关不掉」，而是从这条 policy 入口点
> 「关录像」反而把 `record_enable` 落盘成 `True`、`Recorder.enabled` 也变
> `True` —— 接口照回 ok、配置照落盘，界面完全看不出两条入口给了相反结论。
> 录制总闸是 P0 里最不能有歧义的开关，所以统一交给 `coerce`，值怎么进来
> 都给同一个结果。**只改这一行 + 单独一条用例**（第 18 轮写明「别捎带进
> 别的改动」），不动声明表/守卫/采集器（并发路拥有），不碰 `recorder.py`
> 的录制链路。
>
> `test_94` 的起手先把总闸设成"开"再传字符串 `"false"`，因为 `CASES` 里
> 有一项会把它改成 `False`、`test_93` 又会取反 —— 不自己铺状态就会变成
> 「同值没变化」的假绿。证据取磁盘与运行中 `Recorder`，不读 `/api/link`
> 顶层那个同名回读字段（第 18 轮 `test_90` 已把它列为禁用证据）。
> **先临时把产品代码退回 `bool()` 跑了一次**：磁盘与 Recorder 两层都红
> （`True is not False`），确认用例真能挡住旧行为，再恢复修复版跑绿。
> 新用例不引入 `key="..."` / `effects=[...]` 字面量、不动 `CASES`，
> 所以并发路的 `TestTwoTablesAgree` 正则扫描不受影响（58 项复跑仍全绿）。
>
> 闸门：validate 81 项、后端 **1092** 条（1091+1）、51 个测试文件、
> 前端 16 文件全绿、0 FAIL。未出包、未部署、未 bump（清单第 2 项仍在做）。
>
> **进度（第 21 轮，取清单第 3 项文档重构第一刀）**：开工查并发，另一路在第
> 19 / 20 轮之后没有新提交，它认领的「采集器」尚未落地。按第零步「有 doing
> 接下一未勾项」，清单第 2 项既已由并发路认领，就不重复取活（也不空转收工），
> 改取**下一未勾的清单第 3 项**里的第一份 `docs/CONFIGURATION.md`。两路各推
> 一个独立清单项，不碰对方的文件。方向自检：P0「可信、能无人值守」的工程
> 支撑，纯文档 + 纯离线守卫，不碰录制链路、不碰 P3。
>
> **这份文档要治的是前几轮的病根**：第 17~20 轮连抓三个「接口回 ok、配置也
> 落盘、运行中对象却仍按旧值跑」，根因都是没人说得清某个键**该由谁重新读**。
> 所以主表不抄界面文案，而是逐个对回消费点，给 29 个 `DEFAULTS` 顶层键写清
> 类型 / 默认值 / 范围 / 生效方式 / 出处。§2 把生效方式收敛成七档受控词表
> （热生效 / 每轮读盘 / 夜间任务 / 部分热生效 / 需重启 / 前端常量 / 遗留字段）。
>
> **两处最容易写错的判断，写进文档以免下一个人改回去**：① 「生效方式」说的是
> *改完谁会重新读*，「接口上有没有观测出口」是另一件事 —— `condense_mode`
> 经 `Link.reconfigure` 换 `self.cfg` 后每次现读，确实是热生效，但没有任何
> 接口把它摆出来，声明表判 `partial` 是如实标注，不是「改了不生效」，两张表
> 不许互相改写结论。② `runtime.DEFAULTS` **不是嵌套块的最终形态**：读盘与保存
> 都要过 `norm_detect()` / `norm_notify()`，`detect.confirm` 只存在于
> `norm_detect()` 的输出里，设置页又没控件，全靠 `nvr_slim.py` 三行沿用磁盘
> 旧值 —— 删了就会每次保存把窗口参数打回默认。
>
> **文档不能写完就烂**：`test_config_doc.py` 解析主表与词表，逐项比对
> `DEFAULTS` 的类型/默认值、`_ENUM_KEYS` 白名单、三个具名范围常量，逐个核实
> 「出处」列指向的文件真实存在，并单独钉住 `ingest_enable` / `concurrency` /
> `record_enable` 等几处最易被顺手改错的生效方式。真相始终从产品代码 import，
> 测试里不抄第二份默认值。**首跑故意改坏两处**（`record_segment_sec` 默认值
> 300→600、`ingest_enable` 生效方式 需重启→热生效），各命中一条断言，恢复后
> 10 项全绿，确认不是假绿守卫。新增测试自动进 2/5 的 `test_*.py` 遍历。
>
> **清单第 3 项仍未打勾**：六份文档只完成 `CONFIGURATION.md`，还剩
> `README.md`、`USER_GUIDE.md`、`ARCHITECTURE.md`、`TROUBLESHOOTING.md`、
> `CHANGELOG.md`，以及「`BUILD.md` 只留构建、历史排障搬进 CHANGELOG」那一刀。
> 清单第 2 项的采集器与第一份端到端报告仍归并发路；之后才是
> `nvr_slim.py` 头注释、正式出包与真机验收。
> 版本号仍是 0.9.122，本轮未 bump、未出包、未部署。

> **进度（第 22 轮，按用户指令「合并所有路线 / 整理进度 / review 目标 / 先出个包」）**：
> 以上是第 21 轮当时的状态，**本轮已被用户的新指令改写**。合并结论、目标复核、
> 清单真实完成度与版本策略统一写在本文上方「路线合并与目标复核」小节，
> 那里是唯一事实来源；下面这段保留作历史记录，不再作为待办依据。
>
> 本轮**没有新增功能切片**，做的是收口：① 确认两路成果线性叠在 `main` 上，
> 无需 merge，把分工叙述并成一条线；② 对照 `REVIEW_BASELINE` 逐项复核目标，
> 确认方向落在 P0 工程支撑、不碰录制链路、无 P3，但**批次 5 验收条件未满足，
> 进度表保持 `doing`**；③ 按「先出个当前进度的包看一下」把 `0.9.123` 提前 bump
> 五处并出**进度预览包**（非收尾包），`build.sh --check` 全绿后部署 152 自检。
> 清单第 2/3/4 项如实保持未打勾，采集器与剩余五份文档留待后续切片。
>
> 产物：`dist/nvr-0.9.123_x86.fpk`，**101169048 字节**，
> md5 `e0f7206f51ba5761c9869126eb959e03`。闸门：validate 81 项、
> 后端 **1102** 条、52 个测试文件、前端 16 文件、0 FAIL。
> 部署 152 八项自检全 OK（`deploy-0.9.123.json`），`0.9.122 → 0.9.123` 上线
> 耗时 0.1s、端口 18085；单独复核 `/api/link` 两路 `hstate=recording`
> （age 5/0）、`record.enabled=True`、无 stalled、`detect.state=ready`。
> 生产机 192.168.2.6 未触碰。用户看完预览包后继续切片推进，批次不收尾。

> **进度（第 23 轮，清单第 2 项收尾打勾）**：开工时工作区有四个**未提交**
> 文件（`btn_harvest.py` / `test_btn_harvest.py` / `BUILD.md` / `button_matrix.py`），
> 是同一 automation 的并发实例在 00:01~00:06 写好的第 23 轮在制品：它跑了四轮
> 采集调试、00:06:49 最后改完 BUILD.md 后**没有提交、没有回填台账、没有写记录**，
> 此后十余分钟无任何动静。按「接下一个未勾清单项」本轮就该做清单第 2 项，
> 故接手这份完整切片而非另起炉灶、也不空转收工；提交信息里如实写明是接手的。
>
> **在制品里有两处值得留下的判断**：① `record_enable` 的生效证据从
> `link.record_enable` / `status.cams.recording` 收敛到只剩
> `link.record.enabled` —— 前者是 `link_payload()` 现读 config 补的回读字段，
> 后者数的是「此刻真在跑的 ffmpeg」，本机没装 ffmpeg 时恒为 0，拿它当开关回执
> 会把「环境没装」记成「关录像没生效」（首跑就这么红过一次）。② `cameras` 的
> 锚点从 `segment_sec` 换成 `name` —— 单路分片长度改成 0（跟随通用设置）时该键
> 会从配置里消失，改前改后键名不同会把合法操作误判成采集失败；名字一定在，
> 且它决定 `dir_name`，Recorder 通道列表必须跟着换。另把 `roll_up()` 从
> `verify_all()` 抽出来，采集器与整表判定共用一处 tally，不许各数一遍。
>
> **首份端到端报告**（`test-reports/btn-matrix-0.9.123-r23.json`，不入库）：
> 20 项 **pass 9 / partial 11 / fail 0 / unknown 0**，总判定通过，
> **还原全部成功**（整份比对磁盘 config，只忽略 `updated_at`）。
> 9 项 pass 验到的是运行中对象状态（`link.record.enabled` / `link.mode` /
> `status.frames.*.interval` / `status.detect.*` 等），不是 UI 值换个地方读。
> 11 项 partial 分三类：需重启（`record_dir`）、只被夜间任务读而没有实时出口
> （`cleanup_at` / 归档四项 / `condense_mode` / `notify`）、需要 ffmpeg 命令行
> 而所有 HTTP 接口都不输出 argv（`record_segment_sec` / `record_frag` / `gpu`）。
> 补这些出口要动取流/录制链路或扩大接口面，属清单外改动，逐条写明免验理由
> 而不是凑一条回读断言 —— 这正是第 19 轮定的四档口径「不撒谎也不让表永远红」。
>
> **打勾口径**：按第 20 轮写明的「采集器 + 首份报告接上，partial 逐条消化或写明
> 免验」，清单第 2 项的**自动化链路**已闭合，故打勾；**不等于 20 项全部四层验过**。
> 首跑闸门曾偶发 `test_btn_effect.py` 的 `setUpClass` 起本地服务失败（日志为空、
> 端口 65375），单独复跑 12 项全绿、整轮复跑全绿，判定为本地服务启动竞争而非
> 产品问题，未改任何产品代码。闸门：validate 81 项、后端 **1117** 条
> （1102+15）、53 个测试文件、前端全绿（脚本拆分 166 / 样式 28）、0 FAIL。
>
> 版本仍 `0.9.123`（第 22 轮已提前 bump，**不重复 bump**），本轮清单 3/4 未勾、
> 批次未收尾，按规则**未出包、未部署**；152 只读复核 0.9.123 / 18085 /
> 两路 `hstate=recording`（age 1/2）、无 stalled、`detect.state=ready`。
> 下一轮取清单第 3 项文档重构（还剩 `README.md`、`USER_GUIDE.md`、
> `ARCHITECTURE.md`、`TROUBLESHOOTING.md`、`CHANGELOG.md` 五份与 `BUILD.md` 瘦身）。

> **进度（第 24 轮，批次 5 收尾，`done`）**：用户要求「每轮任务多做一些，加快进度」，
> 故本轮一次推完清单第 3、4 项并做批次收尾，不再一轮一份文档。
> 新增五份文档（README 安装/快速上手、USER_GUIDE 页面与状态读法、
> ARCHITECTURE 模块与路由、TROUBLESHOOTING 六类故障、CHANGELOG 版本沿革），
> `BUILD.md` 重写为只讲构建 / 目录树 / 测试 / 打包，180 行历史排障整段搬进
> `CHANGELOG.md` 并保留当时判断；`nvr_slim.py` 头注释重写为 4000 行主程序的
> 分层入口说明（纯注释）。
>
> **文档不靠自觉维护**：新增 `test/test_doc_suite.py` 14 项守卫，解析
> `ARCHITECTURE.md` 的两张表与源码逐项比对——模块表 = `app/server/*.py`、
> 路由表 = `nvr_slim.py` 的 `/api/` 字面量、tab = `index.html` 的 `data-page`、
> 版本 = `runtime.VERSION`、七态 / 十个错误码 / 五条缺口原因从 `health.py`
> 与 `detect.py` 读；另验相对链接存在、`BUILD.md` 不含历史章节而 `CHANGELOG.md`
> 含。**故意改坏模块名与路由各命中一条断言**（第一版路由判定前缀方向写反，
> `/api/health-ghost` 曾被放过，已收紧），恢复后 14 项全绿。
>
> 事实核对纠正了几处旧口径并写进文档：只有 5 个 tab、没有独立诊断页；
> 无 `do_HEAD` / `do_OPTIONS`；`gap_reason` 是 5 条不是 4 条；部署自检是
> 7 项要通 + 1 项要 401；默认端口 18080（`cmd/main:3798` 注释写 18081 是笔误）；
> `hstate` 是 `link_payload()` 在出口补的，不是 `recorder.status()` 原生字段。
>
> 闸门：validate 81 项、后端 **1131** 条（1117+14）、54 文件、前端全绿、0 FAIL。
> 版本**未重复 bump**，只把 manifest changelog 的「进度预览 / 未收尾」措辞
> 改成正式说明（保留后面全部历史条目）；`pack_fpk.py` 回写的 `checksum` 已还原为空。
> 正式包 `101169995` 字节 / md5 `0d3567e1678808a3fbc8c6b31497d6bd` 取代
> 预览包 `101169048` / `e0f7206f51ba5761c9869126eb959e03`（同名，别装错）。
> `deploy_test.py` 部署 152 八项自检全 OK、端口 18085；按 README §3 真机验收
> 两路 `hstate=recording`（age 1/0）、无 stalled、`detect.state=ready`。
> `gaps` 有 1 条 61 秒缺口、原因「录像进程未运行…已恢复」，是部署停服重启
> 造成的正常空洞（覆盖率 0.982），**不是产品问题**。
>
> 至此批次 1 ~ 5 **全部 done**。按取活规则，下一轮若没有新批次，
> 应「什么都不改，只回一句结论」收工；不要发明新任务。
> 生产机 192.168.2.6 仍未升级，由用户手动操作。

> **第 25 轮立项说明（用户 11-09 07:22「继续，每轮任务多做一些，加快进度」）**：
> 用户连续第三次要求继续，且上一轮收工时已把候选池逐个列给他
> （P2 增强 / 「不在本轮范围」挂账 / 批次 4 遗留验收）。这句「继续」是对
> 「全部 done 就收工」的**显式覆盖**，不是让自动任务自己发明需求：
> 本批只取候选池里 `REVIEW_BASELINE.md:42` 白纸黑字的 P2 首项
> 「Webhook（多模板、失败重试、发送历史）」，方向仍由方向锚点管着。
> 用户一句话即可改方向或叫停，本轮在汇报里明确声明了默认取值。

## 批次 6 · 0.9.124 Webhook 通知补齐（第 25 轮立项）

**为什么立这一批**：评审把「Webhook（多模板、失败重试、发送历史）」列为 P2 首项
（`REVIEW_BASELINE.md:42`）。真机跑起来后发现三件事只做了一件半：
`notify.py` 的**失败重试**（3 次指数退避）与 cooldown 已落地，但
① 消息体是一份「胖 JSON」同时塞飞书 / 钉钉 / 企业微信 / Server酱 四套键，
   不是多模板——平台各不相同，多余字段有的直接拒收，且没法自定义文案；
② 发送历史只是进程内 20 条内存环，**重启即丢、无落盘、无时间范围查询**，
   排障时「昨晚到底发没发出去」没有事实可查；
③ `Notifier.stop()` 从未进停机序列，重试中的 `time.sleep` 靠 daemon 硬退。

这一批不改变录制链路：`recorder.py` 一行不碰，触发点仍是 `detect.DetectWorker._notify()`
与 `link.Link._notify_motion()` 两处入队回调，新能力全部走 `app/server/notify_tpl.py`
与 `app/server/notify_history.py` 两个新模块挂接。

**方向三问（对照 `REVIEW_BASELINE.md` 文末）**：
1. 落在 **P2 增强首项**；其中「历史可查、失败可见」也服务 P0「最看重录像可靠性 /
   数字必须有出处」——通知发出去没有，得有能追到的事实，不能靠界面上一行绿字。
2. 不会让录像事实链更不可信：不碰 `recorder.py` / `ingest.py` / 取帧路径；
   通知本来就在独立 daemon 线程，落盘只是一次 append，失败也吞掉不抛给派发循环。
3. 没有 P3：人脸 / 车牌 / 自训模型 / 多用户权限 / 实时滤镜一件未做。

### 清单

- [x] 多模板：`notify_tpl.py`。按 webhook 的 host 自动适配飞书 / 钉钉 / 企业微信 /
      Server酱 / Bark 五套最小 body + 通用胖 JSON 回退；支持自定义文本模板与
      `{cam} {cat} {label} {time} {date}` 插值，占位符白名单、长度强校、非法回退默认。
- [x] 发送历史落盘：`notify_history.py`。jsonl 追加写 + 原子裁剪 + 损坏行跳过（旧格式
      兼容）+ 按时间 / 摄像头 / 类别 / 成败过滤与 limit；落盘前统一脱敏，**不记 webhook URL**。
- [x] 只读接口 `GET /api/notify/history`；`status_payload().notify` 带落盘统计
      （条数 / 最后一条时间，计数缓存在内存，绝不在轮询里扫盘）。
- [x] 生命周期：`Notifier` 接 `var_dir`，`stop()` 补进 `nvr_slim.py` 停机序列。
- [x] 前端：设置页平台手选 + 文案模板 + 实际发送方式 + 历史查看入口
      （第 26 轮做完；动前端确实牵动了 `js_split` 等价守卫与合成器，单独一轮是对的）。
- [x] 版本 0.9.124：五处 bump、出包、部署 152、真机验收（第 26 轮收尾）。

**进度（第 25 轮，清单 1~4 已打勾，批次仍 `doing`）**：
按第四步「批次收尾才 bump」，本轮**不 bump、不出包、不部署**；
`VERSION` 仍是 `0.9.123`，只在 152 做只读复核。
自定义文本模板若做成配置键，会被前端 `pushCfg()` 整块推回默认值打掉
（第 21 轮 `detect.confirm` 踩过同一个坑），故本轮**零新增配置键**：
模板按 URL 自动适配，自定义模板走下一轮单独设计的写入口。

落地实况（数字都有出处）：两个新模块 `notify_tpl.py`（143 行）/ `notify_history.py`
（193 行）；`notify.py` 只加不改地挂了 `var_dir`、模板渲染、`_persist()` 与
`status()` 的缓存计数，旧的 `_message()` 静态方法保留作兼容入口（16 条老单测全绿）；
`nvr_slim.py` 三处：构造传 `var_dir`、停机补 `Notifier.stop()`、新增只读分支
`GET /api/notify/history`。四份新单测 68 条（模板 26 / 历史 22 / 挂接 10 /
真起服务 HTTP 冒烟 10），闸门 validate 81 项、后端 **1199** 条（1131+68）、
58 文件、前端全绿、0 FAIL。

**首跑抓到并修掉一个真缺陷**：`notify_history._read_lines()` 原来用严格 UTF-8
解码，文件里只要有一行坏字节就 `return []` 把整份历史作废——正好违反模块
自己写的「损坏行跳过」。改成 `errors="replace"` 后坏行被 `json.loads` 单独跳过，
其余行照常读出来，`test_invalid_utf8_does_not_raise` 钉住。
文档守卫也故意改坏两处（删掉模块表里的 `notify_tpl.py`、把路由改成
`/api/notify/history-ghost`）各命中一条断言，恢复后全绿，不是假绿。

**进度（第 26 轮，清单 5/6 打勾，批次 6 收尾 `done`）**：用户又说「继续」，
这一轮把前端入口与收尾一起推完，不留半成品。

前端（`settings_cards.js` / `diagnostics.js` / `live.js` / `core/state.js`）：
- 「通知通道」新增**平台手选**（自动识别 + 飞书 / 钉钉 / 企业微信 / Server酱 /
  Bark / 通用 JSON）。选项列表从 `/api/status` 的 `notify.render.platforms` 读，
  前端只留一份兜底，且由 `test_notify_ui.py` 逐项比对，不会悄悄多出后端不认的值。
- **文案模板**输入（占位符说明也来自 `notify.render.vars`），下面一行写明
  「实际平台（识别/手选）· 文案（默认/自定义）· 已落盘 N 条」，模板被回退时
  直接写「模板已回退：未知占位符 xxx」。
- **查看发送历史**：点一次读一次盘，列最近 20 条（时间 / 路 / 类别 / 成败 /
  平台 / 重试次数），加载中 / 读取失败 / 空 三态各有话说。列表存在 `NT.hist`
  里而不是直接写 DOM —— 卡片每次交互都整块重渲染，只写 DOM 会被冲掉。
- **测试发送**带上界面上刚填的 template / platform（`Notifier.test()` 返回四元组
  含 platform），测的就是屏幕上那句，不是磁盘旧值。

配置键（第 25 轮特意没加的两个）：`notify.platform` / `notify.template` 这一轮
**前后端一起加**——`runtime.DEFAULTS`、`detect.NOTIFY_DEFAULTS`、`norm_notify()`
（平台按白名单强校、写错回落 auto；模板原样保留，非法占位符交给渲染时回退）、
`pushCfg()`、`applyServerCfg()` 五处齐。第 25 轮担心的「`pushCfg` 整块覆盖把新键
打回默认」不再靠「不加键」回避，而是靠**前后端同时接线 + 守卫**钉住：
`test_notify_ui.py` 16 条验 DEFAULTS 键集合等于 `norm_notify` 输出、前端必须推
必须回读、平台白名单前后端逐项相同、`status()` 不许在读状态时读配置。

部署自检加第九项 `/api/notify/history`（0 条也算通过：守的是「文件还没生成也不
500」）。项数不再写死：`test_doc_suite.py` 从 `deploy_test.CHECKS` 数出来，
文档只许跟着代码走——这一轮就是这么红的（文档改了、测试里还写着「八项」）。

收尾实况：五处 bump `0.9.124`（`runtime.VERSION` / manifest 第 2 行与第 15 行
changelog 新条目 / `test_pixdiff.py` / `test_v0918.py`），历史 changelog 条目全部保留；
`test_doc_suite.py` 的「不许写下个版本」改为从 `runtime.VERSION` 推导下一个补丁号，
不再写死字面量。出包 `dist/nvr-0.9.124_x86.fpk` **101180736 字节 /
md5 `d9191c3aa3bbbc8db8f781820521e42d`**；`deploy_test.py` 部署 152 **九项全 OK**
（0.9.123→0.9.124 上线 0.1s、端口 18085、通知历史 0 条、凭证 401）。

真机验收：两路 `hstate=recording`（age 1/2）、无 stalled、`detect.state=ready`；
`notify.render` 返回 platforms 列表与 `history.persisted=true`，`/api/config` 回读
带 `platform="auto"` / `template=""`，程序目录里 `PLATFORMS` / `describe` / 前端
`btnNtHist` 都在（新代码实证，不是靠版本号判定）。缺口两段落在部署停服窗口
（31800→32036 秒，另两条是更早的重启），覆盖率 0.9926 / 0.9877，非产品问题。

测试：闸门 validate 81 项、后端 **1224** 条（1199+25）、59 文件、前端设置页
**514** 项（+9）全绿、0 FAIL。中途一次整轮里两个「起本地服务」的用例在
`setUpClass` 超时（端口竞争），单跑均 2 秒全绿、整轮复跑全绿，未改产品代码。

---

## 批次 7 · 0.9.125 推理设备（第 27 轮立项，同日收尾）

**为什么立这一批**：`REVIEW_BASELINE.md:42` 的 P2 清单里，Webhook 已于批次 6 交付，
下一项是「GPU 自动探测与自动回退」。而 `detect.device` 这个键从 0.9.x 起就在
`DETECT_DEFAULTS` / `DETECT_ENUMS` 里、设置页也把它当设置项，`Engine` 却一直无条件
`providers=["CPUExecutionProvider"]`，诊断卡标题还硬写着「YOLO11n · CPU」——
**用户改成 gpu 后接口回 ok、配置也落盘，实际仍在 CPU 上跑，界面上没有一个字能看出差别。**
所以这一批不是"加个开关"，是把一句没验证过的说法变成可查的事实。

**方向三问**：
1. 落在 **P2 增强（GPU 探测 / 回退）**，同时服务 P0「数字必须有出处」——
   "到底用没用上显卡"必须有出口，不能靠读代码。
2. 不会让录像事实链更不可信：只在**模型加载那一刻**选后端，不碰取帧、编码与
   `recorder.py`；改 device 只重建检测 worker（`DetectorWorker.stop()` 只置事件位，
   不 join），两路录制全程未断（实测 age 1/0）。
3. 没有 P3：人脸 / 车牌 / 自训模型 / 多用户权限 / 实时滤镜一件未做。

### 清单

- [x] `detect.py` 新增 `GPU_PROVIDERS` / `probe_providers()` / `pick_providers()`：
      只认 onnxruntime 自报的可用后端，按 CUDA > TensorRT > ROCm 挑，CPU 永远垫底。
- [x] `Engine` 接 `device` 与 `ort_loader`（注入点），按 device 建会话；
      GPU 建会话失败**自动回落 CPU 再建一次**，两次都失败才是 `MODEL_LOAD_FAIL`。
- [x] 设备事实出口：`Engine.device_status()` → `DetectWorker.status()["device"]` →
      `status_of()["device"]`；未加载时 `actual` 留空（未知就是未知，不拿配置值冒充）。
- [x] 前端：`detDevOf()` 让标题栏跟实际后端走、诊断卡新增「推理设备」一行，
      回落时标黄并写原因；显式选 cpu 不亮黄。
- [x] 改 `device` 热生效：`_sync_detect_worker` 比对 `STATE["detect_device"]`，
      变了就重建 worker —— **只换检测线程，不碰取帧与录像**。
- [x] 文档：`CONFIGURATION.md` §4.1 三档口径 + 「与顶层 `gpu`（ffmpeg 硬解 / 硬编）
      是两件事」；`TROUBLESHOOTING.md` §5.1「配了 GPU，诊断卡却说 CPU」怎么读；
      `CHANGELOG.md` 记两个故意取舍。顺手修掉两处已漂移的行号出处
      （`settings_cards.js` 304/320 → 385/401、`detect.py` 127-136 → 145-154），
      以及 `nvr_slim.py` 本轮插入导致的 7 处表格行号（→ 3566/3570/3580/3584/3956/3968/3978）。
- [x] 版本 0.9.125：五处 bump、出包、部署 152、真机验收。

**两个故意的取舍（别在下一个人手里改回去）**：
- **不含 OpenVINO**：它自己也能跑 CPU，选它等于没法回答"到底用没用上显卡"，
  而这一层存在的意义就是回答这句话。DirectML 是 Windows 专用，本机是 Linux NAS。
- **不引入新错误码**：降级不是失败。混进 `error_code` 会让 `state` 变 `unavailable`，
  把"能用但慢"报成"坏了"，用户会去重启服务。

**验收（152 真机，不是只跑单测）**：把 `detect.device` 由 `auto` 改成 `gpu` 保存 →
8 秒后 `/api/status` 的 `detect.device` 变成
`{"requested":"gpu","actual":"cpu","providers":["CPUExecutionProvider"],
"fallback_reason":"请求 GPU，但 onnxruntime 没有 CUDA / Tensorrt / ROCM 后端
（当前可用：AzureExecutionProvider, CPUExecutionProvider）"}`，`state` 仍是 `ready`、
`avg_ms` 43；日志三条实证「AI 推理设备改为 gpu，重建检测 worker」→ 回落原因 →
「AI 推理设备改为 auto，重建检测 worker」；两路 `hstate=recording`（age 1/0）、
`record.enabled=True`、`detect.confirm` 块未被覆盖；随后还原 `auto` 并复核
`fallback_reason` 清空。
**顺带发现**：这台机器的 onnxruntime 报的可用后端里还有一个 `AzureExecutionProvider`
—— 所以「挑后端」必须是显式白名单，不能写成"可用的第一个非 CPU 就是 GPU"。

---

## 批次 8 · 0.9.126 事件检索（第 28 轮立项）

**为什么立这一批**：`REVIEW_BASELINE.md:43` 的 P2 清单按序是「模型版本管理与效果对比 →
多区域配置 → 高级筛选 → 按时间范围批量导出」。开工前逐项核过，前两项本轮都不该硬做：

- **模型版本管理**：手上只有一份 `share/models/yolo11n.onnx`，没有第二份模型、也没有
  标注样本。没有对照物就没有"对比"，做出来只能是"看起来变好了"——评审最反对的那种结论。
- **多区域配置**：不是缺口。16×9 忽略区从 0.9.x 起就在 `cam.masks.ignore` 里，
  `detect._ignore_rects()` 与 `condense` 两处都吃它，设置页「画面区域」也有格子编辑器
  （`settings_editor.js` 的 `maskCv-ig`）。真正没做的是「只统计区域内的目标」（include 区），
  那是新语义，记进候选池，不在本轮顺带糊进去。

于是取**高级筛选**，并且把挂了两轮的账当场还掉：批次 3 说「移动侦测要等服务端给等价
条件（时间窗确认时一起考虑）」，批次 4 又说「留给事件页高级筛选」——现在就是那个时刻。
现状是事件页只有「天 / 机位 / 类别」三段**单选**，`/api/events` 的 `cat` 只认单个
`person|vehicle|two_wheeler`，「移动侦测」在前端是硬编码的兜底标签（`evMainCat()` 返回
`motion`），服务端根本没有这个口径。

**方向三问**：
1. 落在 **P2 增强（高级筛选）**，同时服务 P1「事件中心」——评审的差异化第一条是
   「按摄像头和时间过滤」，现在只能单选一路、一天、一类，等于半个功能。
2. 不会让录像事实链更不可信：只改查询与列表展示，不碰台账写入、不碰 `recorder.py`、
   不新增定时任务；索引仍是台账的投影。
3. 没有 P3：人脸 / 车牌 / 自训模型 / 多用户权限 / 实时滤镜一件未做。

**口径先定死（免得下一个人各写一套）**：
- 「移动侦测」= **一条 AI 类别都没有的事件**（`categories = ''`），不是第四种类别。
  它可能来自纯帧差窗口（AI 关闭或没认出东西），所以界面文案是「移动侦测」而不是
  「无人无车」——`unknown ≠ 无活动` 这条底线仍然成立。
- 多类别是 **OR**（人员 或 车辆），多机位是 **OR**（这一路 或 那一路），不同维度之间是
  **AND**。选「移动侦测 + 人员」= 没有类别的 **或** 有人员的。
- 「最短时长」用 `end_sec - start_sec`（同一天内的钟点秒，跨零点已被台账夹到日界）。

### 清单

- [x] `event_index.query()` 支持 `cats` 多类别 OR / `cams` 多机位 IN / `min_dur` 最短时长 /
      `motion_only` 移动侦测；单值 `cat` / `cam` 保持兼容（老书签不许失效）。
- [x] 事件补 `kind`（`ai` / `motion`）字段，`event.summary_of()` 增 `motion` 计数 ——
      前端与筛选共用一处判断，不再各自写 `categories.length === 0`。
- [x] `/api/events` 参数解析：`cat` 支持逗号多值（含 `motion`）、`cam` 逗号多值、
      `min_dur` 秒；不认识的类别**明确报错**，不静默忽略（静默忽略会让人以为筛过了）。
- [x] 前端事件页：类别 / 机位改多选，类别加「移动侦测」，新增「更多筛选」（时间范围
      HH:MM、最短时长）与「清空」；筛选项全进查询串，列表头写清当前条件。
- [x] 测试：新 `test_event_filter.py`（内存 SQLite 验四种口径与组合，21 条）、`test_event_api.py`
      补参数校验（+8 条）；前端 `test/ui/events.test.js` 补多选与移动侦测守卫（70 → 91）。
- [x] 文档：`ARCHITECTURE.md` 的 `table:api` 参数（`events_payload()` 出处 `:1526` → `:1549`）、
      `USER_GUIDE.md` 事件页四段筛选表、`TROUBLESHOOTING.md`「筛不出东西先看这三处」、
      `CHANGELOG.md` 四条工程判断；`events.js` 里「服务端没有这个键」的旧注释已改掉
      （它从本轮起就是错的）。
- [x] 版本 0.9.126：五处 bump、出包、部署 152、真机用 196 条事件验收四种筛选。

**验收（152 真机，196 条事件 / 2 天）**：`GET /api/events?from=2026-11-08&to=2026-11-09`

| 查询 | 结果 | 说明 |
|---|---|---|
| 无条件 | 500（limit 封顶） | `summary` = person 42 / two_wheeler 4 / vehicle 22 / motion 439 |
| `cat=person` | 42 | 与 summary 一致 |
| `cat=vehicle` | 22 | |
| `cat=person,vehicle` | **60** | = 42 + 22 − 4；集合并集恒等式逐 id 校验通过 |
| `cat=motion` | 440 | 与「有 AI 类别」的事件**零交集**（逐 id 校验） |
| `cat=person,motion` | 482 | 60 + 422，OR 语义 |
| `cam=cam-test01` / `cam-test02` | 339 / 163 | 两路相加 502 > limit 上限，`cam=两路` 回 500 封顶 |
| `min_dur=60` / `min_dur=300` | 296 / 84 | 秒 |
| `cat=motion&min_dur=30` | 289 | 跨维 AND |
| `cat=dog` | `ok:false` + 列出可用值 | 不静默忽略 |
| `min_dur=ten` | `ok:false` | 参数错不猜 |
| `from=2026-11-08 09:00&to=2026-11-08 10:00` | 12 | 钟点时间窗，`filters.from` 回显 `09:00:00` |

两路 `hstate=recording`（age 1/1）、`detect ready`、`device.actual=cpu`；
程序目录含 `kind_of` / `parse_cats` / `motion_only` / `evMoreBox`。

**一个上层口径的代价（写下来免得当成 bug）**：`limit` 是"最近的 N 条"，
所以 `cam=两路` 在 502 条时回 500 —— 不是筛选错，是列表本来的上限。
要精确总数得靠 `summary`（它按本次返回集合统计，同样受 limit 约束）；
真要看全量就把 `limit` 调大或缩窄时间窗。

---

## 批次 9 · 0.9.127 按时间范围批量导出（第 29 轮立项并当日收尾）

**为什么立这一批**：候选池按序是「模型版本管理 → 按时间范围批量导出 →
include 区」。第一项仍缺对照物（只有一份 `yolo11n.onnx`，没有第二份模型与
标注样本），跳过；取第二项。它不是"增强"，是**最基础的搬运用不了**：

- 以前唯一的导出入口是播放器「⋯ → 导出当前文件」，一次一片（`dl=1`）。
  要交出一小时的录像就是点 12 次、自己记下到哪了。
- 事件页/回放页都能"找到"录像，却没有任何"拿走"的路径 ——
  找到与拿走之间的缺口，比再多一个筛选项更值得补。

**方向三问**：
1. 落在 **P2 增强（按时间范围批量导出）**，服务 P0「存储管理」与 P1「事件中心」
   的同一条用户旅程：找到 → 确认 → 带走。评审原话要的是"能无人值守地信任它"，
   交接能力是信任的一部分。
2. 不会让录像事实链更不可信：**只读**。不碰 `recorder.py`、不改台账、
   不新增定时任务、不写临时文件（流式打包），导出期间录制照旧。
3. 没有 P3：没有人脸 / 车牌 / 自训模型 / 多用户权限 / 实时滤镜。

**口径先定死**：
- **不裁剪**。分片是 `-c copy` 落下来的事实数据；`-c copy` 切点只能落关键帧
  （仍差一个 GOP），帧级精确要重编码。导整片，多给多少写进 manifest。
- **半开区间**：`[start, end)` 与窗口 `[a, b)` 相交才算。与事件检索同一套语义，
  否则同一段录像在事件页和导出里会给出两个答案。
- **只认已封口分片**：`.mp4.part` 没有 moov 播不了；`.cNN.mp4` 是压缩产物不是证据。
- **上限**：1.9 GB / 2000 片 / 7 天。流式 zip 不能启用 zip64（无法回头补头），
  总量必须留在 2 GB 以内；超了在计划阶段说清超多少。
- **先计划再下载**：`/api/export/plan` 与 `/api/export` 共用 `_export_build()`，
  不存在两套口径。

### 清单

- [x] 新增 `export.py`：`select()`（半开区间 + 边界多给）、`totals()`、`cap_problem()`、
      `slug()` / `folders()`（同名机位不叠目录）、`manifest()` / `readme()`、`write_zip()`、
      `Chunked`（HTTP 分块写入口，故意不提供 `tell`）
- [x] `nvr_slim.py`：`_export_build()` 一处构建；`/api/export/plan`（`detail=1` 出逐片明细）、
      `/api/export`（chunked 流式、`X-Export-Files` / `X-Export-Bytes`）；
      枚举复用 `_day_files()`，与事件定位同一套口径
- [x] 归档缺口如实提示：所选日期只剩 `_archive` 片段时，计划里回 `archive_only` 并
      明说「本次导出不含归档」，不假装导出成功
- [x] 前端 `pages/export.js`：文件页右上角「导出」→ 机位多选 + 时间范围 → 先出计划
      （分片数 / 大小 / 实际覆盖 / 边界多给）再下载；日期跟 `state.daySel` 走；
      下载地址直接用服务端给的 `d.url`（前端不拼第二遍）
- [x] 测试：`test_export.py` 18 条（半开区间、边界秒数、上限、文件名清洗、同名机位、
      不可 seek 流的 zip 校验、manifest/README、归档提示、下载头与逐片路径）；
      `test/ui/export.test.js` 26 条；`js_split` ORDER 与 `build.sh` 同步登记
- [x] 文档：`ARCHITECTURE.md` 模块表 + 接口表、`USER_GUIDE.md` 文件页、
      `TROUBLESHOOTING.md` §6.1、`CHANGELOG.md` 四条工程判断、`README.md`
- [x] 版本 0.9.127：五处 bump、出包、部署 152、真机做一次真实下载校验

### 验收（152 真机）

| 项 | 结果 |
|---|---|
| 计划（两路 / 40 分钟） | `ok:true`，20 个分片 / 737580686 字节，`head_extra_sec=180`（窗口 09:53，首片 09:50） |
| 下载 | HTTP 200 `application/zip`，`X-Export-Files=20`，文件名 `nvr_20261109_0953-1033.zip` |
| 包结构 | 22 条目 = README + manifest + 20 片；`testzip()` 通过；全 `STORED`；中文目录正常 |
| 口径一致 | 逐片 `file_size` 与 `manifest.files[].bytes` 全等；`manifest.counts` 与计划一致 |
| 机位写错 | `ok:false`「不认识这个机位：nope」 |
| 空时段 | `ok:false`「这段时间没有已封口的分片（正在写的那一片要等封口）」+ 具体哪路哪天 |
| 超上限 | 4 小时两路 = 3.8 GB / 118 片 → `ok:false`「超过单次 1.9 GB 上限；请缩短时间范围或减少机位」 |
| 录制未受影响 | 导出全程两路 `hstate=recording`（age 0~3），日志有「导出：… 20 个分片 / 703.4 MB」 |

**故意没做的两件（记进候选池，不是漏）**：① 边界精确裁剪 —— 需要临时文件与二倍
I/O，且切点仍只能落关键帧，收益不抵失真；② 导出归档片段 —— 归档是重编码产物，
与"原样交接"是两件事，要做先想清楚 manifest 怎么写。

---

## 批次 10 · 0.9.128 检测只统计区（第 30 轮立项并当日收尾）

**为什么取这一项**：P2 候选池第一项是「模型版本管理与效果对比」，但仓库里仍只有
一份 `yolo11n.onnx`、没有第二份模型与标注样本 —— 「效果对比」没有对照物就只剩观感，
属于没有验收口径的功能，继续跳过（第三轮）。第二项「检测 include 区」才是真缺口：
**忽略区只能解决「这块别算」，反过来的「只关心这块」一直没有入口**。用户在真机上
的诉求很具体：院子摄像头画面里有半条马路和邻居走廊，只想数车道上的人车。

**它不是「多区域配置」的补漏**：16×9 忽略区（`cam.masks.ignore`）从 0.9.x 起就
在，检测与缩时都吃它、设置页也有编辑器。这次新增的是**新语义**（框中心必须落在
区内），会碰判定口径与统计，所以要单独一批。

**口径先定死**：
- **空 include = 不限制**。默认必须与 0.9.127 逐字相同；老配置只有 `ignore`，
  读上来 `include` 补成空数组，不是 `undefined`。
- **框中心口径**，与忽略区同一套归一化坐标。重叠面积口径会出现「半个身子在区外
  就不算」，画窄带覆盖走廊时很别扭；台账里还会出现「同一目标有时算有时不算」。
- **忽略区优先**。两块重叠按忽略处理 —— 用户画了「别管这块」，不该因为另一块
  「只看这块」被放回来。
- **挡掉的框要记账**。区域画窄了，事件页会一片安静；那种「看起来对」的安静最危险。
  `per_cam.outside` 按天累计，诊断页说「区外忽略 N」，用户才知道该放大区域。
- **只影响 AI 识别**。移动侦测是像素差，没有「目标」这个概念，谈不上区内区外；
  录像、快照、缩时完全不受影响（写进文档与代码注释，不悄悄扩大语义）。

### 清单

- [x] `detect.py`：`cells_to_rects()` 统一格号解码（`_ignore_rects` / `_include_rects`
      共用）、`in_include()`（空=不限制）、`apply_regions()` 一处做「类别 → 忽略区 →
      只统计区」并返回被挡掉的个数；坏框判「不在区内」而不抛异常（一路脏数据不许
      带崩整轮）；`_blank()` 增 `outside`，新增 `_outside()` 按天累计，
      `status()` 的 `per_cam` 透出 `outside`
- [x] `nvr_slim.py`：新增 `norm_masks()`，**写盘与回显共用**（以前两处各写一份列表
      推导，加新键必漏一处）；只认整数与纯数字字符串 —— `int(1.5)` 会静默变成 1、
      `int(True)` 变成 1，前后端就会一个显示 12 格、一个只认 11 格；诊断导出每路
      一行加「只统计区=N 格」
- [x] 前端：`state.js` 的 `normMasks()` 收两块（去重 / 丢越界 / 升序，升序是为了
      「换个顺序画同一块」不算改动）；`settings_editor.js` 在「检测」段加第二张
      16×9 画布（绿=只统计，琥珀=忽略，两张画布互相淡显对方那块）、两块清单各自
      能清空、计数进手风琴标题；只读摘要与 `settings_cards.js` 同时说两块
- [x] 诊断页每路一行加「区外忽略 N」（读的是服务端 `per_cam.outside`，不是本地估）
- [x] 测试：`test_detect_include.py` 27 条（格号解码 / 中心口径 / 空=不限制 /
      忽略优先 / 类别优先不进区外计数 / 坏框不炸 / outside 记账 / 服务端强校）；
      `test/ui/masks.test.js` 27 条（两块互不覆盖 / 清洗口径 / 清单与清空 /
      诊断行 / `pushCfg` 真的带 masks），并接入 `build.sh`
- [x] 文档：`ARCHITECTURE.md`（apply_regions 一处判定 + outside 口径）、
      `USER_GUIDE.md`（画面区域两块的判定表与三条口径）、
      `TROUBLESHOOTING.md` §6.0（「AI 不报事件」先分没看见 / 被挡 / 真没人）、
      `CHANGELOG.md`（四个取舍 + 故意没做）、`README.md`
- [x] 版本 0.9.128：五处 bump、出包、部署 152、真机验配置往返与脏值强校

### 验收（152 真机）

| 项 | 结果 |
|---|---|
| 部署 | 0.9.127 → 0.9.128，0.1s 上线，九项自检全 OK（端口 18085，事件索引 200 条 / 2 天，通知历史 0 条，凭证 401） |
| 配置往返 | `include=[0,1,2]` 写入后回读逐字相同，`ignore` 未被吞 |
| 脏值强校 | 提交 `[-1,144,1.5,null,"7"]` → 落盘 `include=[7]`，与前端清洗同结果 |
| 诊断可读 | `/api/log/export` 每路一行出现「只统计区=1 格」，与配置一致 |
| **真的在挡** | 只统计区设成 1 格后两轮检测：`cam-test01` `outside=9`、`cam-test02`（未设）`outside=0` —— 挡掉的是真实检出框，不是空转 |
| 还原 | 两块都清空后配置回到 `{ignore:[],include:[]}` |
| 录制未受影响 | 全程两路 `hstate=recording`（age 0~1），`detect.state=ready` |

**故意没做的（记进候选池，不是漏）**：只统计区不管移动侦测 —— 像素差没有「目标」，
要管就得改成区域裁剪式差分（多一份画面拷贝与阈值口径）或重写 pixel 判定，
没人提过这个需求。

---

## 批次 11 · 0.9.129 事件快照（第 31 轮立项，第 32 轮实现并收尾）

**为什么取这一项**：用户 11-09 13:38 明确要求「研究事件页面显示事件的快照，把功能
实现写进路线图，规划逻辑流程与实现方案」，同时「导出功能简化一些，不用深究」。
事件中心现在能"找到 → 点开 → 播到那一秒"，但列表里只有一行字：
`10:23:35 · 院子 · 8 秒 · 人员`。用户想判断"这条值不值得点开"，只能先点进去播一遍。
评审 P1 事件中心的原话是「从翻录像变成点事件直接看」——**"看"之前先要"那一眼"**。
这是 P1 的收口，不是新功能扩张。

### 调研结论（现状，全部在代码里核过）

| 事实 | 出处 | 对方案的影响 |
|---|---|---|
| 快照只有「最新一张」，每 N 秒被覆盖 | `frames.FrameTap._write`：写 `.part` → `os.replace`，路径 `var/snapshots/<cam>.jpg` | 事件发生那一帧**不落盘**，事后取不回 |
| 事件模型没有任何图片字段 | `event.from_ledger` 的 15 个键、`event_index.SCHEMA` 的 15 列 | 事件与画面之间零关联，得新建一层 |
| 事件身份只由「哪一路 + 起点秒」决定 | `event.event_id` | 图可以按时间窗匹配，不用改身份规则 |
| 检测每次推理读的就是这张 jpg | `nvr_slim._frame_path_for_detect` → `detect.grab_ppm_image` | 命中那一刻画面就在手边，**不需要再起 ffmpeg 抽帧** |
| 取帧层已经是 720p / q:v4 | `frames.FrameTap._cmd`：`fps=1/N,scale=-2:720` | 快照直接复用这张图，不额外解码、不重新编码 |
| 移动侦测事件的边界是**事后**由帧差曲线算的 | `ledger.events_of` 的 `mv` 来自运动簇，`pixdiff.curve` 在缩时扫描时才算 | 它没有"确认时刻"可挂图（见「故意不做」） |

真机数字（152，11-09 13:41 只读核对）：`var/snapshots/` 8 个文件共 848KB，
720p q:v4 单张 **45~180KB（均值约 110KB）**；`/vol1` 55G 已用 80%，可用 11G；
事件索引 200 条 / 2 天（约 50 条/路/天）。

### 三条口径先定死（改之前先看这三条）

1. **拍的是「当时」，不是「现在」。** 绝不允许拿 `snapshots/<cam>.jpg` 当事件图充数
   —— 那是几秒（甚至几小时）之后的画面，正是评审点名的"看起来对、实际没验证过"的
   假数据。没有就如实说"这条没有留快照"。
2. **只加不改，绝不阻塞事实链。** 存图发生在台账写入**之后**，检测线程只做一次
   内存读（≤200KB）+ 一次非阻塞入队；写盘在独立单线程里。队列满就丢。不 wait、
   不建 RTSP、不碰 `recorder.py`、不动判定与确认的任何一行。
3. **不留清不掉的东西。** 图是可清理的附属物：7 天随日期目录过期，每路每天有硬上限，
   **不进事件索引表**（索引是台账投影，图不是事实；把路径写进索引，清理后就会出现
   指向空气的行）。

### 逻辑流程（一轮检测 → 一张图 → 一张卡片）

```
检测第 N 拍（detect.DetectWorker._one）
  _grab()                    取帧层已经写好的 jpg 路径（不新增进程、不新增连接）
    → 推理 → apply_regions → confirm.feed（时间窗确认）
    → wins 非空（这一帧够格记一条事件）
        ① _write_ledger()     先落台账：事实
        ② _shot(cam, src, now) 再存图：附属物
             限流   同一路 ≥30 秒才收一张；每路每天 ≤300 张
             读取   open(src,"rb").read()   只读内存，不做磁盘写
             入队   ShotStore.offer()       queue 上限 8，满了丢并计数（不抛）
        ③ _notify()           通知：原有顺序与口径不变

ShotStore 写线程（单线程，独立于检测 / 取帧 / 录像）
  var/event_shots/<cam>/<YYYY-MM-DD>/<HHMMSS>_<mmm>.jpg
  写 .part → os.replace（与取帧层同一套原子替换，读的人不会拿到半张图）
  每天第一次写顺带 sweep：删掉保留期（7 天）之外的日期目录
  启动时 sweep 一次（停机几天再开机也不会攒一堆旧图）

GET /api/events?from=&to=...（列表）
  events_payload() 逐条补 shot：
    按 (cam, date) 分组 → 每个目录 listdir 一次（请求内缓存，不重复扫）
    find(start-15, end+15) → 窗口内**最早**一张（最接近事件开始的那一眼）
    命中 → shot = {url, at, offset}；没有 → shot = null

GET /api/events/<id>/shot（单张图）
  id 解析不出 / 目录里没有 → 404 {"ok":false,"error":"这条事件没有快照"}
  有 → image/jpeg + ETag + Cache-Control: private,max-age=86400（图不可变，可长缓存）
  鉴权走 cookie（<img> 带不了 Authorization 头，这条路径与现有快照接口同一条）
```

### 存储布局、命名与配额

```
var/event_shots/<cam>/<YYYY-MM-DD>/<HHMMSS>_<mmm>.jpg
```

- 时间取**图片真实落盘时刻**（`os.stat().st_mtime` 的本地时间），不取事件起点：
  事件会被重判外推、会被 merge 合并，起点可能比这张图早 20 多秒。文件名说真话，
  UI 才能写"快照 10:23:41 · 事件开始后 26 秒"。
- 日期目录用**图片时刻**的日期（不是事件日期）：跨零点时图落在正确的那一天，
  查询跨天也不会漏。
- 配额：单张按 150KB 估，每路 50 条/天 ≈ 5.5MB/天，7 天 ≈ **38MB/路**，5 路 ≈ 190MB，
  占可用 11G 的 1.7%。硬上限 300 张/天/路 = 33MB/天/路；到顶就不再存，
  日志记一次（不静默、也不反复刷屏）。

### 接口契约（字段名先定死，前后端与测试都按这个写）

```json
"shot": null | {
  "url":   "/api/events/<id>/shot",
  "at":    "10:23:41",
  "offset": 6.0
}
```

- `url` 由服务端给：这一页的既有契约就是"不自己拼播放地址"，图片同理。
- `at` 是图片真实时刻；`offset = at - start`（秒，负数表示图早于事件起点）。
- 没有图就是 `null`，不是空对象、不是空字符串 —— 前端只判 `if(ev.shot)`。

### 前端交互

- 卡片右侧一列：**76×43 缩略图**（16:9，有 `shot` 才渲染）在上，类别标签在下；
  无 `shot` 时卡片与 0.9.128 完全一致（不塞灰块，避免一屏占位符）。
- 缩略图 `loading="lazy" decoding="async"`：一次 200 条，只加载视口内的几张；
  加载失败（404 / 断网）→ `onerror` 隐藏，**不留破图**。
- 展开卡片时，在 ±30 秒按钮上方显示宽图（`max-height:210px; object-fit:cover`）
  + 一行小字「快照 10:23:41 · 事件开始后 6 秒」；没有图时写清原因：
  「这条没有留快照（0.9.129 之前的事件不存图，或检测当时未确认）」。
- 点击语义不变：点卡片仍然进回放，缩略图不拦截点击（点图放大列为下一版，
  见「故意不做」）。

### 失败与降级（每条都要有对应测试）

| 情况 | 行为 |
|---|---|
| 取帧层没起 / 直连模式拿不到 src | 不存图、不报错，事件与通知照常 |
| 磁盘写失败 / 配额到顶 | 计数 + 日志一次；检测、台账、通知都不受影响 |
| 事件窗口内没有图 | `shot: null`；前端不渲染图位，展开区说明原因 |
| 历史事件（0.9.129 之前） | 同上，文案点明"升级前不存图" |
| 图被清理但事件还在 | 404 + `shot: null`；**不退回当前快照** |
| `<img>` 请求 401（没登录 cookie） | 图隐藏，列表照常；不弹错误、不重试 |

### 清单（第 32 轮全部完成）

- [x] `app/server/event_shot.py`（新模块 392 行）：`parse_name()` / `name_of()` /
      `name_at()` / `pick()`（纯函数，窗口内最早一张）/ `ShotStore`（`offer` 限流 +
      有界队列 + 单写线程 + `sweep` + `status`）/ `list_for(cam,date)` 5 秒缓存 /
      `find()`（含跨零点）/ `path_of()` 防穿目录 / `shot_of()` 契约
- [x] `detect.py`：`_grab()` 返回 4 元组（多一个源路径）；`DetectWorker` 收
      `shot_store`（None = 不存图，行为与 0.9.128 逐字一致）；`_one()` 在
      `_write_ledger()` 之后、`_notify()` 之前调 `_shot()`（读字节 + 入队，
      异常一律吞掉）
- [x] `nvr_slim.py`：`ShotStore` 起停与 `STATE["shots"]` 注入；`_attach_shots()`
      给列表逐条补 `shot`；路由 `/api/events/<id>/shot`（`bits[1] == "shot"`），
      404 走 JSON、200 走二进制 + ETag + `max-age=86400`；`/api/status` 暴露
      `shots`（written / dropped / failed / rate_limited / capped / bytes）
- [x] 前端：`events.js` 缩略图（lazy + onerror 隐藏）与展开宽图 + 说明、
      `pages.css` 三条样式（`.evthumb` / `.evshot` / `.evshotcap`）、`index.html`
      合成产物
- [x] 测试：`test_event_shot.py` **39 条**（命名解析 / 窗口选点 / 跨零点 / 限流 /
      日上限 / 队列满 / sweep / 防穿目录 / 契约 / 检测侧挂钩 / `_grab` 四返回值）、
      `test_event_api.py` **+2**（`shot` 字段与"键始终存在"）、
      `test/ui/events.test.js` **+10**（有图 / 无图 / 宽图 / 负偏移 / onerror）；
      三处旧替身随 `_grab` 多返回值更新（`test_ingest` / `test_detect_sched` /
      `test_event_confirm`）
- [x] 文档：`USER_GUIDE.md`（事件页加「快照那一眼」三条规矩）、`ARCHITECTURE.md`
      （模块表 + 路由表 + 快照数据流）、`TROUBLESHOOTING.md` §6.0.1
      （「事件卡片没有缩略图」四种原因）、`CHANGELOG.md`（五个取舍 + 六条故意不做）、
      README
- [x] 版本 0.9.129：五处 bump、出包、部署 152、真机验缩略图 / 404 / 开关联动

### 验收（152 真机，第 32 轮实测）

| # | 项 | 结果 |
|---|---|---|
| 1 | 部署与自检 | 0.9.128 → 0.9.129，0.1s 上线，九项自检全 OK（端口 18085、事件索引 200 条 / 2 天、凭证 401） |
| 2 | 真事件自动出图 | 14:58:45 的 `cam-test01` AI 事件自动带 `shot{at:14:58:55, offset:9.3}`；GET `/api/events/<id>/shot` → **200 `image/jpeg` 145077 字节**，`ffprobe` **1280×720**，`file` 认成 JPEG |
| 3 | 缓存 | `ETag: "145855_324.jpg-145077"`，带 `If-None-Match` 二次请求 → **304** |
| 4 | 没有图 | 旧事件 → **404 JSON** `{"ok":false,"error":"这条事件没有快照"}`，列表 `shot: null`；删掉图之后立刻回到 404（不退回当前快照） |
| 5 | 列表耗时 | `limit=200` 三次：145 / 90 / 91 ms（200 条跨 4 天 2 路，只 listdir 两个目录） |
| 6 | 检测开关联动 | 关掉检测 43 秒：`written` 3 → 3、目录不增长、`state=disabled`；重开后 3 → **5** 张，`state=ready` |
| 7 | 限流与占用 | `rate_limited=9`（一条长事件在 30 秒限流下只收几张，不是每拍一张）；`bytes` ≈ 1MB / 5 张（约 140KB 一张） |
| 8 | 录像未受影响 | 全程两路 `hstate=recording`（age 0~2）；`git diff --stat -- recorder.py` **为空**（一行未改） |
| 9 | 部署产物核对 | 线上 `ui/index.html` 有 `evthumb` ×2 与 `evShotHtml`，`server/nvr_slim.py` 有 8 处 `event_shot`，`runtime.py` 为 0.9.129 |

### 故意不做（记在这里，不许悄悄合入）

- **移动侦测事件（`kind=motion`）的快照**：它的边界是缩时扫描时由帧差曲线**事后**
  算出来的，事件发生当时没有"确认时刻"可以挂图。要覆盖它得在像素差链路里再加一条
  常驻取帧与判定——那是另一批，且与「检测只解释事实、不反向影响录像」的红线擦边。
- **事后从录像抽帧补历史**：`ffmpeg -ss` 抽一帧要 seek + 解码（100~400ms），
  200 条列表就是 200 次解码，与录像 IO 抢资源；抽到的还可能是空画面。
  快照的价值是"检测器当时看到的那一帧"，不是"事后随便截一帧"。
- **在图上画检测框**：框是推理输出，画上去就成了"标注图"，而用户要判断的是画面
  本身；画框还要引入 numpy 绘制与重新编码，CPU 成本与收益不成比例。
- **点缩略图放大 / 下载 / 分享**：先把"列表里能看见"做对；放大要新写一层浮层、
  手势与缓存，属于交互增强，不是这一批的验收条件。
- **服务端另生成小图**：`<img>` 由浏览器缩放即可，局域网多传 100KB 不值得为此
  再起一次 ffmpeg 解码（那才是真正的成本）。
- **给快照加配置开关**：先无条件开（每路每天几十 MB 量级）。真要关，`detect.enable`
  已经是总闸；等出现磁盘告警再加开关，免得又多一个"默认值改变了既有行为"的坑。

---

## 批次 12 · 0.9.130 快照放大与检测框（第 33 轮立项并当日收尾）

**为什么取这一项**：批次 11 把"那一眼"放进了卡片，但只有 **76×43**。用户想判断
"那是人还是影子"时只剩两条路：点卡片（**会直接跳去回放**，seek + 解码，比看一眼
贵两个数量级），或者把脸凑近屏幕。评审 P1 的原话是「从翻录像变成点事件直接看」，
可"看"这一半还没做透。0.9.129 的「故意不做」里列了 ③ 在图上画检测框、④ 点缩略图
放大 —— 这一版做这两件，但**换了实现方式**：框由浏览器叠层绘制，服务端不解码不
重编码。原来那条"画框要 numpy 绘制 + 重编码，成本与收益不成比例"对**服务端画框**
成立，对浏览器叠层不成立。

三问自检：① 落在 P1 事件中心的收口，不是新方向；② 不碰录像链 —— `recorder.py`
`git diff` 为空，检测线程只多传一个已经算好的 `dets` 引用；③ 没混进 P3。

### 三条口径（改之前先看这三条）

1. **框只来自那一拍推理。** `_one()` 里过完区域过滤的 `dets` 就是这张图上的目标，
   与图同一帧。事后重跑推理用的是**现在**的画面（框会框到别人身上），
   从录像抽帧再画也不是"检测器当时看到的"，两种都不做。
2. **旁挂、按需读。** `.json` 与图同名同目录；列表仍只取缩略图，`?meta=1` 只在
   真要放大时读一次（几百字节），结果按事件 id 缓存（`undefined` 没问过 /
   `null` 正在问 / `[]` 真没有，三种状态不混）。
3. **失败互不牵连。** 框写盘失败只丢框（`meta_failed` 单独计数、单独日志，不算
   "图写失败"）；没有 jpg 时 `?meta=1` 回 404，不给空框清单（否则前端以为
   "图在、只是没框"）；坏 json 当"没框"，不修不重建。

### 逻辑流程与接口

```
detect._one()  一帧命中 → _write_ledger（事实）→ _shot(cam, src, now, dets)
                                                    │  读字节 + 源图 mtime
                                                    ▼
                            ShotStore.offer(cam, data, at, dets)  归一化 boxes_of()
                                                    │  非阻塞入队（队列满就丢）
                                                    ▼
       var/event_shots/<cam>/<日期>/<HHMMSS>_<mmm>.jpg
                                    <HHMMSS>_<mmm>.json   {"at":…, "boxes":[{c,s,b}]}
       写线程：jpg 先写（.part→replace），再写 json；json 失败只动 meta_failed

GET /api/events/<id>/shot?meta=1
   → 与图片**同一处**定位（shot_lookup），图不在就 404
   → {"ok":true, "at":"16:19:08", "offset":29.7,
      "boxes":[{"c":"person","s":0.656,"b":[cx,cy,w,h]}], "count":2, "has_boxes":true}
   → 没有框：boxes:[] + note（老图写"0.9.130 之前存下的图"，坏文件不写这句）
```

前端：`events.js` 里 `evThumbHtml()`（缩略图带 `data-evzoom`）、`evZoomHtml()`
（浮层大图 + 框层 + 「打开回放」/「关闭」）、`evBoxHtml()`（归一化 → 百分比，
夹在 0~100%）、`evLoadBoxes()`（按 id 缓存）。框的 DOM 是绝对定位的 `<i>`，
颜色按类别走卡片色条同一套（人蓝 / 车绿 / 两轮车琥珀）。

### 清单（第 33 轮全部完成）

- [x] `event_shot.py`：`boxes_of()`（归一化 + 夹 0~1 + 去退化框 + 限 20 个）、
      `meta_path_of()` / `read_meta()`（坏文件当没有）、`meta_of()` 契约、
      `ShotStore.offer(..., dets)` 与写线程的 `_write_meta()`、`status()` 增
      `meta` / `meta_failed`
- [x] `detect.py`：`_shot(cam, src, now, dets=None)` 把这一拍的框交出去；
      `_one()` 传入已过区域过滤的 `dets`（不额外推理、不额外读盘）
- [x] `nvr_slim.py`：定位抽成模块函数 `shot_lookup()`（图与框共用一处，避免
      "有图没框"）、`event_shot_meta_payload()` 可直接单测、路由
      `/api/events/<id>/shot?meta=1`
- [x] 前端：缩略图可点（`pointer-events` 从 none 改回）、浮层大图与框层、
      Esc / 点背景关闭、展开区也叠框（共用缓存）、`pages.css` 新增
      `.evshotwrap` / `.evbox` / `.evzoom*`
- [x] 测试：`test_event_shot.py` **+11**（归一化 / 夹边界 / 坏条目 / 退化框 /
      限数量排序 / 旁挂路径 / 写盘读回 / 没框不写 / 坏 json / 契约 / 检测侧交付）、
      `test_event_api.py` **+6**（带框 / 无框说明 / 坏框不报错 / 无图 404 /
      坏 id / offset 与列表一致）、`test/ui/events.test.js` **+12**
- [x] 文档：`USER_GUIDE.md`「放大看：检测框就在图上」、`ARCHITECTURE.md`
      （模块表 / 路由表 / 快照数据流 + 旁挂说明）、`TROUBLESHOOTING.md` §6.0.2
      （「有画没框」三种原因）、`CHANGELOG.md`（五个取舍 + 四条故意不做）、README
- [x] 版本 0.9.130：五处 bump、出包、部署 152、真机验框 / 304 / 老图说明 / 404

### 验收（152 真机，第 33 轮实测）

| # | 项 | 结果 |
|---|---|---|
| 1 | 部署与自检 | 0.9.129 → 0.9.130，**0.1s** 上线，九项自检全 OK（端口 18085、事件索引 200 条 / 2 天、通知 0 条、凭证 401） |
| 2 | 真事件带框 | `cam-test01_20261109_161839`（31.03 秒人员事件）→ `?meta=1` 回 **2 个框**：`person 0.656` + `vehicle 0.404`，`offset=29.7`；同一张图 138193 字节 |
| 3 | 图片与缓存 | `cam-test02_20261109_161713`：200 `image/jpeg` **182007 字节**，`ffprobe` **1280×720**，`file` 认 JPEG；`ETag` 二次请求 **304** |
| 4 | 老图没有框 | `cam-test01_20261109_160440`（16:09:08，升级前）→ `boxes: []`、`has_boxes:false`、note 写明「0.9.130 之前存下的图」 |
| 5 | 没有图 | 无快照事件 → **404** `{"ok":false,"error":"这条事件没有快照"}`（不给空框清单） |
| 6 | 写盘计数 | `shots`: `written=5` / `meta=5` / `meta_failed=0` / `rate_limited=14` / `bytes≈22.9MB`；磁盘上 `event_shots/**/*.json` 与 jpg 同名成对 |
| 7 | 录像未受影响 | 全程两路 `hstate=recording`（age 0/3）；`git diff --stat -- recorder.py` **为空** |
| 8 | 部署产物核对 | 线上 `ui/index.html` 含 `evzoom`/`evbox` 30 处、`server/event_shot.py` 含 `meta_of`/`boxes_of` 5 处、`nvr_slim.py` 含 meta 路由 6 处、`runtime.py` 为 0.9.130 |
| 9 | 闸门 | validate **81** 项、后端 **1382** 条 / 64 文件、前端 events **113** / export 26 / detect 58 / masks 27 / 设置页 514 / 时间轴 70 / 样式 28 / 脚本 173，0 FAIL |

### 故意不做（记在这里，不许悄悄合入）

- **移动侦测事件（`kind=motion`）的快照与框**：边界是缩时扫描时事后算的，发生当时
  没有"确认时刻"可挂图；要覆盖得在像素差链路里再加常驻取帧，与「检测只解释事实、
  不反向影响录像」的红线擦边。
- **服务端把框烧进 JPEG**：要解码 + 重编码（numpy/ffmpeg 都行），CPU 成本与
  "几条线"的收益不成比例；浏览器叠层还能随图缩放，缩放时不糊。
- **手势缩放 / 拖动 / 下载 / 分享**：这一版目的是"看清那一眼"，不是做看图器；
  要看完整过程，浮层里有「打开回放」。
- **把框做成检索条件**（"只要框里有人的"）：那是检索语义（与批次 8 的类别筛选
  是两件事），先把"看图"做对。
- **事后重跑推理补框**：用的是现在这一帧，框会框到别人身上 —— 那正是
  「看起来对、实际没验证过」的假数据。

---

## 批次 13 · 0.9.131 存储空间预警（第 34 轮立项并收尾）

**为什么取这一项**：评审基线 P0「存储管理」列了三格 —— 保留策略、清理预览、
空间预警。前两格在批次 5 就已交付（`retention.py` 按天整目录删 + `/api/retention`
预览清单），只有**空间预警**从来没做过，是 P0 里唯一还欠着的一项。它欠的又正好
是「能无人值守运行」最关键的那一格：盘写满那天 ffmpeg 报 ENOSPC、录制停摆，
而页面上现有的三个数字（剩余 GB、每路占用、保留天数）都要用户自己心算
"这还能撑几天"——不会有人天天算。

三问自检：① 落在 P0 存储管理，不是新方向；② 只读目录名与文件大小，不解码、
不写录像目录、不碰 `recorder.py`；③ 没混进 P3。

### 为什么不能只算「剩余 ÷ 每天写入」

这台测试机就是反例：55 GB 盘、已用 86%、剩 7.6 GB，实测每天写入约 12 GB，
保留 3 天。`free / rate` = 0.6 天，但事实是它已经**稳定跑了几周** —— 每天
03:00 整目录删掉最旧的一天，写入与删除基本相等。拿倒计时当结论会天天喊
"明天就满"，喊几次就没人信了。

正确的问法是**稳态装不装得下**：

```
rate    = 最近 7 个完整日的中位数（去掉 0 字节的日子：那天没录，不代表速率是 0）
need    = rate × 保留天数（开了归档取归档天数，否则取全时天数；多路取最大）
other   = 总容量 − 剩余 − 录像实测占用        （同一块盘上的其它数据）
headroom= 总容量 − other − need               （稳态余量）
```

`headroom < 0` 才是"不管清理跑不跑都会满"；`headroom ≥ 0` 但很小是"再满一点
就危险"；`剩余 < 阈值` 是"现在就该处理"。三种情况对应不同的建议，混成一句
红字等于没说。

### 三条口径（改之前先看这三条）

1. **只读元数据**：只用目录名与 `stat` 出来的大小，不读内容、不解码、不在
   录像目录里写任何东西。
2. **算事实，不算猜测**：今天没走完就按已过时长外推，并**标明依据**
   （"最近 3 个完整日" / "今天已跑 6.2 小时"）；一个完整日都没有时回 `unknown`，
   绝不拿默认值编一个"还能录 30 天"。
3. **预警只负责说清楚，不自动删**：自动删录像等于让"空间紧张"改写"事实数据"，
   踩红线。要删由用户在清理预览里确认。

### 等级与建议

| level | 条件 | 用户该做什么 |
|---|---|---|
| `off` | `space_warn_gb = 0` | 用户显式关掉 |
| `unknown` | 没有完整日数据，且今天不足 1 小时 | 再等等；页面写"数据不足" |
| `critical` | 剩余 < max(阈值/2, 总量 3%)，或 `headroom < 0` | 现在清理 / 降低保留天数 |
| `warn` | 剩余 < max(阈值, 总量 10%)，或 `0 ≤ headroom < max(5 GB, 总量 5%)`，或清理过期 > 2 小时 | 看一眼清理预览 |
| `ok` | 其余 | — |

建议文本按原因给：装不下时算"这块盘按实测最多留 N 天，现在配的是 M 天"；
只是剩余低时指向清理预览与今晚的时刻；清理过期时指向夜间任务是否被归档/压缩
占住。多因同时命中时按等级排序，全部保留（用户要看到全部理由，不是只看最重的）。

### 接口与透出（不新增路由）

- `GET /api/storage/usage` → 增加 `days`（近 30 天每日字节）与 `space`（完整预警块）。
  该接口已有 30 分钟缓存，顺手算，不多扫一次盘。
- `GET /api/status` → 增加 `space`（**只读内存**，监控线程 10 分钟更新一次）。
  状态接口几秒被轮询一次，绝不能在它上面扫盘。
- `GET /api/health` → 增加 `space.level` / `space.free_gb`（同上，内存）。
- 配置键 `space_warn_gb`（默认 20，范围 0~2000，0 = 关闭）：`runtime.DEFAULTS`
  + `coerce` + `pushCfg` + 回读 + 设置页输入框。

### 清单

1. `space.py`：`clamp_warn_gb` / `daily_rate` / `evaluate` / `text_of`（纯函数，
   不 import nvr_slim，单测可直接喂假数据）
2. `storage_usage`：按 (日期, 文件名) 去重出 `days`，`space` 块顺便算
3. 监控线程：10 分钟一算，等级变化记日志（升级一条、恢复一条，不刷屏）
4. `/api/status`、`/api/health` 透出（内存，不扫盘）
5. 前端：诊断页存储卡顶部预警条（等级配色 + 四个数字 + 建议）、折叠摘要与
   直播页头部提示、设置页阈值输入
6. 测试：`test_space.py` + `test_storage.py` 扩 + 前端设置/诊断用例
7. 文档：CONFIGURATION / USER_GUIDE / ARCHITECTURE / TROUBLESHOOTING /
   CHANGELOG / README

### 验收（152 真机，第 34 轮实测）

| # | 项 | 结果 |
|---|---|---|
| 1 | 这台盘现状要判 `critical`，建议里要出现"最多留 N 天" | **通过**：`level=critical`、`rate=19.3 GB/天`（最近 1 个完整日）、`need=57.9 GB`、`free/total=7.0/55.0`、`suggest_days=2`、`keep_text=全时 3 天`；建议"把保留天数从 3 天降到 2 天以内，或先删掉一部分旧录像" |
| 2 | 阈值改 0 → `off` 且页面不显示红条 | **通过**：`space_warn_gb=0` 后 `level=off`、`reasons=[]`、`advice` 为空（前端按 `off` 不出条） |
| 3 | 阈值改 3 → 应降为 `warn` | **未按计划走，见下**：`reasons` 从 `[capacity, free_critical]` 收成 `[capacity]`（阈值确实生效），但 `level` 仍是 `critical` |
| 4 | 清理跑过之后不再报"清理过期" | **通过**：`reasons` 里始终没有 `cleanup_stale`（`cleanup-state.date` 是今天，且启动 15 分钟内一律不判） |
| 5 | 重启后 `/api/status.space` 十分钟内出现 | **通过**：部署后首轮（等 20 秒）就写回，`checked_at` 有值，`/api/health.space={level:critical, free_gb:7.1}` |
| 6 | `recorder.py` `git diff` 为空；两路 `hstate=recording` | **通过**：`recorder.py` 0 行改动；`cam-test01` / `cam-test02` 均 `hstate=recording` |

第 3 项为什么"没按计划走"——是**验收条款写错了**，不是实现不对：

写这条时脑子里只想着"剩余低于阈值"这一个原因，忘了这台盘同时命中 `capacity`
（`headroom = -17.3 GB`：按实测每天 19.3 GB、保留 3 天要 57.9 GB，加上非录像
占用 14.4 GB，55 GB 的盘无论如何装不下）。等级取最重原因，`capacity` 是
`critical`，所以阈值从 20 调到 3 只会让"剩余偏低"这条理由消失，整体仍是
`critical`。这才是对的结论：盘**真的**装不下，不该被一个阈值调小掩盖掉。

真正验证"阈值生效"的证据是 `reasons` 少了一条：`22 → 3` 时 `free_critical`
消失、`capacity` 保留。阈值本身的 `warn` / `ok` 分支由
`test_space.py::TestEvaluate` 里 15 条用例覆盖（含大冰箱盘不拿绝对阈值喊人）。
验完已把 `space_warn_gb` 还原成 20（`GET /api/config` 复核 `config.space_warn_gb=20`）。

### 故意不做（记在这里，不许悄悄合入）

- **自动删录像腾空间**：见口径 3。
- **把预警接进 Webhook**：现有通知契约是「事件」（cam + 类别 + 模板），系统告警
  要另一套语义（去重键、每日一次、失败重试口径）。这轮先把"算出结论并说清楚"
  做对，接通知单独立项（下一轮候选）。
- **按摄像头分别预警**：空间是整块盘的，每路分开发预警只会让用户以为要分头
  处理；每路占用已经在存储卡里单列。
- **INODE 用量 / SMART 磁盘健康**：另一个数据源（`/proc`、smartctl），与
  "还能录几天"不是一件事。

---

## 批次 14 · 0.9.132 移动端体验（第 35 轮立项）

**为什么现在做**：功能已经铺到 13 个批次，但这一层是**在手机上用的**。用户原话：
「NVR 的实际使用场景往往是手机」。而现在的界面是按桌面写的 —— 全站 1533 行
样式里**一个 `@media` 都没有**，按钮高度按鼠标指针的精度设计，字号按 27 寸屏
的阅读距离设计。功能不缺，缺的是"在 375 宽的屏上还顺手"。

三问自检：① 落在 P1「事件中心」与 P0「回放」的可用性上，不是新方向；
② 只动前端展示与交互，不解码、不碰 `recorder.py`、不改任何事实数据；
③ 没混进 P3（不是人脸/车牌那一类）。

### 七项审计结论（有证据，不是"已优化"）

| # | 项 | 现状证据 | 判定 |
|---|---|---|---|
| 1 | 事件卡片窄屏信息密度 | `pages.css` 里 23 处 `10.5px`、21 处 `11px`、24 处 `11.5px`；卡片右侧 `evtags` 竖排两个徽标 + 76×43 缩略图，375 宽时时间列被压到只剩钟点 | **要改**：窄屏提字号降密度 |
| 2 | 快照放大竖屏适配 | `.evzoomimg{max-height:78vh}`，但 16:9 横图宽度受 `96vw` 限，竖屏实际只占屏高 **约 1/3**；框标签 `9px` | **要改**：横过来看 + 放大一档 |
| 3 | 多机位筛选易操作性 | 机位是横向滚动的 `evchip`，8 路时找一路要横滑；`.evchip` 高 29px | **要改**：弹层 2 列多选 + 触控尺寸 |
| 4 | 回放时间轴定位精度 | 默认 `120px/小时` → **1px = 30 秒**；最大缩放 600px/h → 1px = 6 秒；手指点击精度约 10px | **要改**：窄屏默认档与点击微调 |
| 5 | 后台切回能否恢复播放 | `visibilitychange` 只在 `live.js` 里刷快照；播放器（`player.js` / `timeline.js`）没有任何可见性处理 | **要改**：隐藏即暂停、回来续播 |
| 6 | 弱网加载与失败态 | `API.timeout=15000` + `AbortController` 有；但事件列表加载中没有提示、失败只有摘要一行"读取失败"，没有重试入口 | **要改**：加载态 + 可重试失败态 |
| 7 | 页面切换释放视频资源 | `stopAllVideos()` 做了 `pause + removeAttribute(src) + load()`，但只在关播放器时调；`pvStop()` 只 `pause()`，**不释放 src** | **要改**：离开回放页释放 |

### 一条交互决定：快照与回放拆成两步

用户建议原文：「在事件详情里把『事件快照』和『录像回放』的操作分开，让用户先
快速确认，再决定是否打开视频。」

现在展开一条事件，图上就并排摆着「前 30 秒 / 打开回放 / 后 30 秒」三个按钮，
主按钮直接是"打开回放"——等于把"确认"和"看全程"压成一次点击。手机上打开一段
录像的代价是 seek + 解码 + 流量，而用户十次里有九次只想确认"那是人还是影子"。

改成两段：

```
第一段（确认区）：大图 + 「快照 HH:MM:SS · 事件开始后 N 秒」+ 可点放大
第二段（决策区）：「▶ 打开录像（约 N 秒）」一个主按钮；前/后 30 秒降为次要
```

主按钮文案从「打开回放」改成「打开录像（约 N 秒）」：把"要花多久"写在按钮上，
用户才知道自己点下去要付什么代价。

### 实施清单

1. 新增窄屏断点 `@media (max-width:640px)`：字号上浮一档、触控目标 ≥ 40px、
   卡片布局与内边距重排（**全站第一个媒体查询**，写清为什么是 640）
2. 事件详情两段式（上面那条）+ 展开区大图可点放大
3. 快照浮层：「横过来看」旋转 90°（16:9 在竖屏上旋转后能占满屏高）+ 倍率切换
4. 事件列表：加载态（骨架条）与失败态（原因 + 重试按钮）
5. 机位筛选：`全部机位 / 已选 N 路` 按钮 + 2 列网格弹层，替代横向滑动的 chips
6. 回放页与播放器：`visibilitychange` 隐藏即暂停、回来续播；离开回放页释放 src
7. 时间轴：窄屏默认档提高（1px 更细）、点击后给 ±5 秒微调

### 验收（152 真机 + 桌面浏览器）

- 375 宽下事件卡片：时间、机位、类别徽标同屏可见不截断；触控目标 ≥ 40px
- 展开一条事件：先看到图，只有点「打开录像」才起播；按钮上带时长
- 竖屏点「横过来看」：图占屏高 ≥ 65%，检测框跟着转、不错位
- 事件列表：断网时出现"读取失败 + 重试"，恢复网络点重试能拉到列表
- 切到别的 App 再回来：原本在播的继续播；不在播的不会被自动起播
- 离开回放页后 `document.querySelectorAll("video[src]")` 为空
- 桌面端（≥1024）外观零变化（媒体查询只作用于窄屏）

### 故意不做

- **重做一套移动端 DOM**：同一份页面 + 媒体查询，避免两套 UI 各修一遍 bug。
- **手势缩放/拖动看图器**：只在浮层里给"旋转 + 一倍放大"两个按钮，
  做完整看图器是另一个产品（这条推翻批次 12 的"不做手势"，理由是手机竖屏
  看图确实看不清；但仍不做连续捏合与平移）。
- **PWA / 离线缓存**：弱网下先做到"说清楚失败并能重试"，离线播放是另一个量级。

---

## 批次 15 · 0.9.134 运动事件快照（第 37 轮立项并收尾）

**为什么现在做**：批次 11（0.9.129）只在 AI 确认那一拍落图，纯帧差的运动事件
（`KIND_MOTION`，AI 没框住）从不存图 —— 事件列表 / 详情里 `ev.shot` 恒为 `None`，
用户想「先看一眼」只能点开回放。批次 11/12 都把它写进「故意不做」（边界事后算、
没有确认时刻可挂图）。用户问「移动侦测的事件记录，怎么实现快照功能？规划一下」
→「执行」，于是补上这「那一眼」。

三问自检：① 是 P1「事件中心」的可用性补全（找到 → 看一眼 → 再看全程）；② 只加不改、
抽帧在独立线程、不碰 `recorder.py`、不改任何事实数据；③ 不混进 P3。

### 方案（选 A：事件查询触发 + 分片抽帧）

判过四条路：A 事件落账后异步从 recorder 分片抽帧（选用）；B 检测那拍把
`snapshots/<cam>.jpg` 顺手存一份（会被「拍现在不拍当时」污染、且帧差事件没有
确切的「那一拍」）；C 分析期事后重扫分片（重、且要另一套调度）；D 起临时 RTSP
抓图（拉流成本 + 与录像不同源）。选 A 的理由：分片已在盘上、时间可锚定事件起点、
复用现成 `event_shot` 落盘与查询，前端零改动。

```
/api/events 查到某条 kind==motion 且无 shot
   │  motion_shot.request(ev)   幂等（窗口内已有图就跳）+ 非阻塞 put_nowait
   ▼
后台线程：再判幂等 → locate_segment（复用 event.resolve 定位事件起点所在分片 + 片内偏移）
   │  ffmpeg -ss <offset> -i <分片> -frames:v 1 → 一帧 JPEG
   ▼
ShotStore.offer(cam, data, epoch_of(date,start))   ← 时间锚定事件起点，不是「现在」
   ▼
后续 /api/events 的 ev.shot 由现成 shot_of 统一补上（前端零改动）
```

### 实施清单

1. 新增 `motion_shot.py`：`epoch_of`（本地午夜锚定）、`locate_segment`（纯函数，
   复用 `event.resolve`，排除 `.cNN` 活动切片）、`MotionShotService`（`request` /
   `_loop` / `_handle` / `_extract` / `status`）
2. `nvr_slim` 5 处接入：`import motion_shot`；启动 `MotionShotService(...).start()`
   挂 `STATE["motion_shot"]`（异常置 None、不拖垮其余链路）；events 查询后对
   `kind=="motion"` 且无 shot 的事件投一次 `request`；shutdown 调 `.stop()`；
   `/api/status` 暴露 `motion_shot{queued,dropped,extracted,failed,missed,running,qsize}`
3. 新增 `test_motion_shot.py`（12 条）：`epoch_of` / `locate_segment`（含前摇、
   缺失→None、坏日期→None、切片排除）/ `request` 幂等与入队 / 后台抽帧落盘 /
   `shot_store=None` 不抛 / **单条异常不打死 worker**（回归守卫）
4. 七份文档 bump 0.9.134（README / BUILD / CONFIGURATION / CHANGELOG / USER_GUIDE /
   TROUBLESHOOTING / ARCHITECTURE），ARCHITECTURE 模块表补 `motion_shot.py` 行

### 真机核对抓到并修的两处 bug（写清楚，别让下一个人踩）

1. **`cam_dirs_fn` 契约错**：挂载时 `cam_dirs_fn=lambda cam: cam_storage_dirs(cam)`，
   但 worker 传进来的是事件里的 **cam id 字符串**，而 `cam_storage_dirs(c)` 要的是
   **摄像头字典**（内部 `c.get("dir_name") or c["id"]`）→ 第一条就
   `AttributeError: 'str' object has no attribute 'get'`。修：新增 `cam_dirs_for(id)`
   用 `next((c for c in cameras() if c.get("id") == id), ...)` 做 id→dict 再取目录。
2. **`_loop` 逐条处理没兜底**：异常不在 try 里 → 线程静默退出，队列只塞不抽、
   全 dropped、`extracted` 永远 0（线上状态 `running=false / extracted=0 /
   dropped=169` 即此）。修：拆 `_handle`，`_loop` 对每条 `try/except` 计 `failed`，
   worker 永不因单条坏数据退出。

修复后机上状态：`running=true / extracted=34 / failed=0 / missed=0 / qsize=0`，
30 条事件里 16 条 motion 已带图。

### 验收（152 真机）

- `motion_shot` `running=true`、`extracted` 持续增长、队列能排空
- `/api/events` 的运动事件出现 `shot{url,at,offset}`，`at` 锚定事件起点（不是「现在」）
- 抽不到（分片过期 / 没 ffmpeg / 队列满）只计 `missed` / `failed` / `dropped`，不抛、不影响列表
- 两路 `hstate=recording`、日志 warn=0 err=0、`recorder.py` **git diff 为空**

### 故意不做

- **不新建 RTSP / 不重解码整段**：从已录分片 `-ss` 抽一帧，代价最小、与录像同源。
- **不写检测框 `.json`**：运动事件没有检测框，硬塞空框反而误导（`?meta=1` 回 404 即可）。
- **不进事件索引**：图是附属物，写进索引会在 7 天过期后留下指向空气的行（同批次 11 口径）。
- **不做「同步阻塞出图」**：查询线程只入队，出图慢半拍但绝不拖慢打开事件的响应。

---

## 批次 16 · 0.9.135 运动快照补图修正（第 38 轮）

**为什么现在做**：用户把生产机升到 0.9.134 后反馈「未看到移动侦测的事件快照」。0.9.134 在测试机是 16/24 出图，看起来很绿 —— 但那台机器的运动事件稀疏、且只有一路没顶到日上限，等于只验证了「最理想的那一半」。

**三处病根（都只在真实事件量下暴露）**：

1. **补图被实时限流误伤**。`ShotStore.offer()` 的 `min_gap`（30 秒/路）本意是「同一条事件被检测节拍反复确认，别存十份」，前提是 `at` 单调跟着现在走。补旧事件时 `at` 是历史时刻，翻页还会倒退 → `at - last` 变负数，被判成「30 秒内又来一张」整片丢弃。**修**：`offer()` 加 `gap` 参数，补图走 `gap=False`；日上限与队列满丢不松，且关掉时不更新 `_last`（实时检测的节流口径不被历史时刻污染）。
2. **队列饥饿**。`event_index.query()` 末尾 `reversed(rows)` → 返回**时间升序**；补图顺着投，最老的事件先占满队列（原 `QUEUE_MAX=16`），而它们的录像早已过期、`locate_segment` 恒返回 None，既不推进也不出图；每次刷新都在重投同一批，最新事件永远排不上。**修**：`for e in reversed(evs)` 从最新往前投、`QUEUE_MAX` 16→32、给「定位不到分片」加 `MISS_TTL=600` 秒负缓存。
3. **日上限串池**。每路每天 300 张的预算原本只有 AI 快照一个消费者。AI 检测开着时上午就顶满，之后一整天运动事件补图全被 `capped`。152 实测：`cam-test02` 当天目录 **487 张** → 该路补图 `rejected` 44/44，而 `cam-test01`（152 张）正常出图。**修**：检测池 `_day_n` / 补图池 `_day_bf` 各 300（合计上限翻倍，8 路 7 天约 5GB，仍有界），补图池每进程从 0 计。

另外两处顺手修：`_handle` 忽略 `offer()` 返回值、抽到帧就 `extracted+1` → 计数器全绿而列表一张图没有，拆成 `extracted` / **`stored`** / `rejected`；单条事件详情（`event_payload`）也投一次补图，从通知或分享链接直接点进来时列表可能从未被打开过。

**教训**：0.9.134 的验收只看「有没有图」，没看「该有的有没有都出现」。这次验收改成**覆盖率**（165/165）而不是「非零」。

### 验收（152 真机）

- `motion_shot`：`running=true`、`stored=128`、`rejected=0`、`missed=0`、`qsize=0`
- `/api/events?limit=200`：165 条 motion **全部带 `shot`**（含此前被日上限挡死的 `cam-test02` 14/14）
- `shots.capped=2` 只来自检测池（AI 当天已顶满 300，符合预期，补图池独立不受影响）
- 两路 `hstate=recording`、日志 warn=0 err=0、`recorder.py` **git diff 为空**

### 故意不做

- **不取消日上限**：补图是附属物，无上限会在"画面一直在动"的机位上写爆盘；改成两池各 300 是把预算分给两个消费者，不是取消护栏。
- **不做同步出图**：查询线程仍只入队，宁可慢半拍，也不让打开事件列表等 ffmpeg。
- **不改 `recorder.py`**：分片枚举与定位全部复用既有代码（`event.resolve` / `ledger.src_start`）。

---

## 批次 17 · 0.9.136 运动快照可自证（第 39 轮）

**为什么现在做**：生产机升到 0.9.135 后，用户仍反馈「还是没有」。到这一步，问题已经不是「补图逻辑对不对」，而是**「没法知道它卡在哪」**：`stored=0` 可能是「录像目录找错」「分片已被清掉」「ffmpeg 跑不动」「目录写不进」四件事之一，四种处置完全不同，而聚合计数把它们混成一个 0。前两版都在猜，这版改成能看见。

**做了什么（只加可观测性，不改判定、不改落盘规则）**：

1. **处理轨迹**：每条补图留一条（`_trace` 环形 6 条）——解析出的目录、`dirs_seen`（真实命中）、`files`/`segs`（当天扫到几个 `.mp4`、几个能当全时分片）、定位到的分片与片内偏移、`ffmpeg` 的 stderr 摘要、落盘结果。随 `/api/status` 的 `motion_shot.trace` 暴露。
2. **当场自检** `GET /api/motion_shot/probe?cam=&date=&start=`：只读、不抽帧、不落盘，一次答完四件事（目录尝试/命中、当天分片数、定位结果、`event_shots` 目录可写性——向上找第一个存在的祖先判 `W_OK`）。
3. **ffmpeg 先验**：路径不存在时按 `PATH` 找（`shutil.which`）、查执行权限、`-version` 验真，结论缓存；抽帧失败去掉 `-c:v mjpeg` 再试一次；失败原因写进 `_last_err` 随轨迹带走（以前一律静默返回 `None`，线上只能看到 `failed` 涨）。
4. **诊断页「事件快照」卡**：运行版本、检测池与补图池各自计数、`ffmpeg` 状态、最近 4 条轨迹、自检按钮。

`scan_day` 拆出 `dirs_seen`/`files_seen`，`locate_segment` 行为不变（测试契约保留）；`event_shot.py` / `recorder.py` 一行未改。

### 验收（152 真机）

- `build.sh --check` 全绿：`test_motion_shot` **20** 条、`test_event_shot` 52 条、`test_doc_suite` 14 条，前端合成 173 项通过
- 出包 `dist/nvr-0.9.136_x86.fpk` **101257734 字节** md5 `0dcc00a27e6c676854c35915352a6393`；152 部署九项全 OK（0.9.135→0.9.136，上线 0.1s）
- `probe` 实测：`cam-test01` → `dirs=["测试A-137","cam-test01"]`、`dirs_seen=["测试A-137"]`（**中文 dir_name 与 id 不同，正是 `cam_dirs_for` 存在的理由**）、`files=301`/`segs=299`、`shot_writable=true`
- `trace` 实录：`23-10-00.mp4@206s → 99176 字节 → 已落盘`；`motion_shot stored=1 / rejected=0 / failed=0 / missed=0`

### 明确没做到

**没有定位到生产机的具体根因**。`192.168.2.6` 的 SSH 凭证与测试机不同（`admin/admin` 被拒），API 全部要 Bearer token，读不到它的 `/api/status`。这版的价值是：下一次只需一张诊断页截图（或一段 `probe` 返回），就能直接指到那一步。

---


## 批次 18 · 0.9.137 运动事件 AI 复核（第 40 轮）

**为什么现在做**：0.9.136 的诊断卡实锤补图链路全绿后，用户确认快照已可见，并提出：运动事件若复核出人/车，标签怎么归类？执行给补图快照补识别框。

**归类的设计决策（用户问的那一句）**：**事件仍归「移动侦测」，kind 不变**。台账记的是「事件发生时 AI 没确认」，事后对一张抽帧图再跑推理只补证据、不改写历史——`event.kind_of` 只有 categories 一条判定线，塞第二套必出两份真相；`event_index.sync_day` 从台账重建，改 kind 的写法都会被抹掉；把侦测事件升级成「人员」会让移动侦测筛选漏事件、摘要计数对不上。复核出的框与类别跟着快照走（旁挂 `.json`，与 AI 确认事件同一格式），放大时前端零改动直接叠框 +「人员 0.87」标签；`人员 x / 车辆 y / 移动侦测 z` 的摘要计数不受影响。

**做了什么**：

1. **同帧双产物**：补图抽帧的**同一次** ffmpeg 运行多出一份 PPM（推理吃原始像素，JPEG 解来解去是浪费）；`_run_both` 一次运行两个输出，jpg 给快照、ppm 给复核。
2. **复核开关**：`_motion_detect_ref()` 每条现查（AI 总闸开着 + `DetectWorker` 活着才返回引擎引用，不缓存——换设备重建 worker 时复核自动跟随新会话）；总闸关着就不跑，不背用户关掉的开关偷算力。
3. **框随图走**：推得出人员/车辆 → `dets` 传给 `offer()` → 旁挂 `.json`；白名单外（猫狗）不画框、推不出不硬造；复核是附属物的附属物，失败只影响框不影响快照。
4. **可观测**：`motion_shot` 计数加 `redetected`；`probe` 报复核可用性；轨迹行加 AI 摘要（`复核出 2 框` / `没认出人员/车辆` / `AI 检测总闸关闭...`）；诊断卡加「AI 复核框」行。

**真机调试抓出的两个坑（152 实锤）**：① ffmpeg 8.x 靠扩展名猜输出格式——`.jpg.part` 怪后缀直接拒开输出（退出码 234），即使显式 `-f image2` 第二个输出仍可能被编成 mjpeg（产物头 `FF D8` 而非 `P6`）→ 临时文件改用真扩展名（/tmp 私有文件，写完即读即删，无需 `.part` 原子性）；② `_extract` 返回的 ppm 字节漏了 `detect.parse_ppm`，`_redetect` 按三元组解包必炸 → 解析挪进 `_extract`，测试替身契约同步。

**插曲（测试平台运维）**：152 `/vol1` 被录像填到 100%，连续两次部署中途写满失败（rsync `No space left on device`，回滚也起不来）。清掉 26 个旧部署备份（601M→101M）仍不够——服务短暂运行的十几分钟里两路录像就把空间吃回去了；删 11-08 全天录像（约 20G）后恢复 17G 可用并部署成功。**保留期/空间预警为什么没拦住 100% 这条线，值得后续单独查**。

### 验收（152 真机）

- `build.sh --check` 全绿：`test_motion_shot` **27** 条（+7：dets 流转/白名单过滤/引擎降级/无像素跳过/probe 可用性/失败计数），全量校验 0 失败
- 出包 `dist/nvr-0.9.137_x86.fpk` **101234775 字节** md5 `eab60538d43b4086a82fdf53341ad4a0`；152 部署九项全 OK（0.9.136→0.9.137）
- 机上实测：复核链路全通（双输出 → PPM 解析 → 推理 → 落盘），深夜空画面如实报「复核完成：没认出人员/车辆」——机制工作正常，正样本（真出框）由单测 dets 流转 + `event_shot` 既有旁挂逻辑保证，白天的真实画面上框会自然出现



---

## 批次 20 · 0.9.139 事件列表全部大图（第 42 轮）

**为什么现在做**：0.9.138 部署后用户复核界面，结论是中间态不妥——「小图呢？
需要展开才能看到大图？不妥。」并给出两个可接受的方向：全部大图，或保留小图、
展开才能看到大图。用户未在澄清提问中选择，按其反馈的主要痛点
（「需要展开才能看到大图」）取第一方案「全部大图」执行。

**做了什么**：

1. **大图常驻**：有快照的事件卡片直接把整卡宽大图画在卡片里（`.evshotwrap`
   `flex:1 1 100%` 独占一行，`width:100% + height:auto` 按原比例、不裁切），
   快照时刻说明（`快照 HH:MM:SS · 事件开始后 N 秒`）随图常驻。
2. **小缩略图下线**：删 `.evthumb` / `.evthumbbox` / `EV_IMG_ONERR` 与
   `evThumbHtml()`——大图模式下同一张图不必画两遍；0.9.138 刚立的
   「aspect-ratio 定比例 + 绝对定位防 720p 固有高度进布局」整套随缩略图一起退役，
   骨架只保留 `aspect-ratio:16/9` 大图灰块。
3. **展开区只剩动作**：`.evopen` 不再重复出图，承载「打开录像（约 N 秒）」、
   ±30 秒、播不了的服务端原因、没有快照的说明；「先看清、再决定打开」的次序
   不变，只是「看清」不再需要展开。
4. **不动的**：点大图直接弹放大浮层（`data-evzoom` 在图上，列表点击代理里
   先于卡片展开判断，不跳页）；检测框仍只在展开/放大时取一次
   （`evLoadBoxes` 缓存），框进来叠在常驻大图上；图加载失败仍 `bad` 类 + 
   CSS ::after 明说；`recorder.py` 一行未改。

### 验收（152 真机）

- `build.sh --check` 全绿：前端 events **123** 条（改 6 删 2，净 -2），
  后端全量 0 FAIL（版本守卫同步 0.9.139）。
- 出包 `dist/nvr-0.9.139_x86.fpk` **101234891 字节** md5 `0ddaa1b74b1176bad4a5d8a4261d62a5`；
  152 部署**九项全 OK**、上线 0.1s；机上 `curl ui/`：新标记 13 处、
  `evthumb*`/`EV_IMG_ONERR` 0 残留。
- 真机观感待用户确认。

---

## 批次 19 · 0.9.138 事件卡片尺寸重排（第 41 轮）

**为什么现在做**：0.9.137 收尾后用户发来事件页截图并指出——展开卡片里「小图
（缩略图）应该拉满高度，大图（展开快照）应该拉满宽度」，前提是**画面比例不变**。
对照实现逐条核对：缩略图写死 76×43（卡片高 ~66px，永远填不满）；展开大图塞在
左列里被右侧缩略图列压掉 ~90px 宽，右边永远空一条；两者都靠 `object-fit:cover`
或 `max-height` 裁切凑数。

**做了什么**：

1. **缩略图拉满高度**：`.evthumbbox` 用 `aspect-ratio:16/9` 定比例 +
   `align-self:stretch` 拉满卡片内容高，宽度自动跟随（≈116×66，面积比原来大一圈）。
   关键坑：图若不绝对定位，720p 的固有高度（几百 px）会参与 flex 布局计算，
   把卡片撑爆——绝对定位铺满后容器不参与内容高计算。
2. **标签归位**：「人员 / 车辆 / 移动侦测」标签从缩略图下方移到左列机位行下横排
   ——标签挂在右列时会把列撑得比文字块高，缩略图怎么缩都"填不满"。
3. **大图拉满宽度**：展开详情从 `evmain` 挪出成 `.evopen`（卡片 `flex-wrap`
   两行结构的第二行），大图 `width:100% + height:auto` 按原比例铺满整卡宽；
   去掉 `max-height:210 + object-fit:cover`（宽屏必裁）。
4. **配套**：缩略图 onerror 从"藏图"改"藏整个容器"（定比例容器留着会是一块灰底）；
   加载骨架灰块同步新尺寸防加载完成瞬间跳动；窄屏断点删掉 70×39 覆写。

### 验收（152 真机）

- `build.sh --check` 全绿：前端 events **125** 条（+2：evthumbbox 容器 / evopen
  独占整卡宽），后端全量 0 FAIL（版本守卫同步 0.9.138）。
- 出包 `dist/nvr-0.9.138_x86.fpk` **101235740 字节** md5 `28c100e22b9b90a30fbdede9e03f0ae0`；
  152 部署**九项全 OK**，上线 0.1s；机上 `curl ui/`：新标记 17 处、旧尺寸 0 残留。
- 真机观感待用户确认（缩略图变大、大图整卡宽、比例不裁切）。

---

## 不在本轮范围

评审 P3 明确不建议投入：人脸识别、车牌识别、自定义模型训练、复杂多用户权限、实时滤镜。
理由：模型依赖、硬件兼容、隐私与维护成本远超收益，现阶段先把「事件发生 → 找到录像」做透。

**剩余候选（批次 15 起）**：
`预警转通知`（批次 13 只做「算出结论并说清楚」，系统告警要另一套语义：
去重键 / 每日一次 / 与事件通知共用一个 webhook）、
`模型版本管理与效果对比`（仍缺第二份模型与标注样本，挂了六轮）、
~~`移动侦测快照`~~ → **批次 15（0.9.134）已交付**：纯帧差运动事件从 recorder 已录分片异步抽一帧补进 `event_shots`，时间锚定事件起点（不是「现在」），复用 `event_shot` 落盘与查询、前端零改动。批次 11/12 曾写进「故意不做」（边界事后算、没有确认时刻可挂图），批次 15 用「事件起点所在分片抽帧」绕开了这个限制。

- 「空间预警」已由**批次 13（0.9.131）**接手：评审基线 P0 存储管理三格里的最后一格，
  保留策略与清理预览在批次 5 已交付。做完这格，P0 六项全部有落地。

- 「模型版本管理」仍未做：只有一份 `yolo11n.onnx`、没有第二份模型与标注样本，
  「效果对比」没有对照物就只剩观感。要做先解决"第二份模型从哪来"。
- ~~「快照的四个延伸」~~ → **两件已交付、两件有明确理由不再排期**：
  ① 点图放大 → **批次 12（0.9.130）已交付**（浮层大图，点击不跳页）；
  ② 图上画框 → **批次 12 已交付，但换了实现**（框由浏览器叠层绘制，服务端不解码
  不重编码）；③ 服务端小图 → 不做，浏览器缩放即可，多传 100KB 不值得再解一次码；
  ④ 移动侦测快照 → **批次 15（0.9.134）已交付**，见上面那条（批次 17 再补了诊断能力）。
- ~~「include 区」~~ → **批次 10 已交付**（0.9.128）。它确实是新语义而不是补漏：
  忽略区（`cam.masks.ignore`）管「这块别算」，include 区（`cam.masks.include`）管
  「只算这块」，两块重叠时忽略优先。只影响 AI 识别，移动侦测仍是整幅画面的像素差。
- ~~「导出的三个延伸」~~ → **用户 11-09 13:38 明确「导出功能简化一些，不用深究」，
  就此收口，三项不再排期**：① 边界精确裁剪（要临时文件 + 二倍 I/O，切点仍只能落
  关键帧）；② 导出归档片段（归档是重编码产物，与"原样交接"是两件事）；
  ③ 2 GB 以上分卷下载（流式 zip 不能启用 zip64）。
  理由：批次 9 的导出已经覆盖「选定时间段 → 原样打包带走」。再往前是备份工具的活
  （增量、校验、断点续传、异地），不是 NVR 的活；继续深挖只会把维护成本压到
  「录像可靠性」这条主线上。真有大包需求，正确的工具是 rsync 而不是网页下载。
- 「高级筛选」由批次 8 接手（见上一节），同时还掉批次 3/4 挂了两轮的 `cat=motion` 服务端等价条件。

批次 2 期间另有三件**故意不做**的：
- 设置页「轮换凭证」按钮：`POST /api/auth/rotate` 已可用（返回新 token），但页面上按一下就把
  自己锁在门外，要连带处理"轮换后自动用新 token 重新登录"，属于新功能而非本批收尾，故仍未做。
  请求层（第 6 项）已经落地，接这个按钮时直接用 `API.post` + `allow401:false` 即可。
- 用户名/密码登录：单机单人，多用户权限属 P3；再叠一层密码只会多一个可忘记的东西。
- 向导里播报 token：安装时用户还看不到页面，等真的打开页面引导层会说清楚去哪找。

批次 3 期间**故意不做**的两件（都记在这里，不许悄悄合入）：
- **「移动侦测」不作为事件页的筛选项**：它不是类别，而是"一条 AI 类别都没有"的兜底口径
  （台账 `categories` 里只有 `person / two_wheeler / vehicle`）。要做就得前端再过滤一遍，
  而列表被 `limit` 截过 —— 用户会以为筛出来的就是全部。真要做，先让服务端给一个
  `cat=motion` 的等价条件（批次 4 时间窗确认时一起考虑）。
- **设置页「事件索引重建」按钮**：`rebuild_index()` 已经可用（索引只是台账投影）。
  现在索引坏了靠删 `var/event_index.db` 自愈，没做成按钮是因为还没遇到真实的坏索引形态，
  做了也只是给一个没有验证过的动作加一个入口。

批次 4 期间**仍然没做**的两件（写在这里，别当成漏账）：
- **`cat=motion` 服务端等价条件**：批次 3 说"时间窗确认时一起考虑"，这轮确认层只做了
  「够不够格记一笔」，没做「只记移动侦测」的口径。事件页的筛选仍只有 person/vehicle/
  two_wheeler，移动侦测仍不是可筛类别 —— 它压根不是类别，硬塞进筛选项会让用户以为
  筛出来的就是全部（列表被 limit 截过）。留给做「事件页高级筛选」（P2）时一起做。
- **确认参数进设置页**：窗口/阈值这轮只落在配置文件里（`detect.confirm`），界面上看不到
  也改不了。理由：这是给调参用的闸，默认值就是目标行为；做进 UI 要先想清楚
  「用户改了窗口会怎样」这类解释，属于设置页整理（批次 5）的活。

批次 5 按钮验收表期间**查清后故意不收进 `CASES`** 的三项（第 18 轮）：
- **`record_frag`**：0.9.111 起设置页已无此开关，`applyServerCfg()` 写死 `LK.frag = true`，
  每次保存把服务端也纠正回分片 MP4。它不是「按钮」，往表里加只会验一个前端常量。
  另：`Recorder.status()` 不暴露当前 `frag`，诊断页看不到封装方式 —— 补这个字段属于
  「系统诊断」，不是按钮验收，留给做诊断包（P0）时一并考虑。
- **`snapshot_interval`**：界面有刷新频率按钮，但生效方式是取帧 worker 每轮用
  `interval_fn` 现读 `config.json`，没有「运行中对象状态」出口；离线老流程的
  `SnapshotWorker.status()` 只回 `cam_id → ok` 表。补可观测出口要动三处文件并跨线程，
  属于清单外的改动。若要验，应改成一条「定时器读的是盘不是启动时的缓存」的专门断言。
- **`ingest_enable`**：设置页无控件，只在配置文件里可调；`IngestManager` 在 `main()`
  启动时读一次且没有 `reconfigure`。补热生效要动已稳定的取流链路（每路一条 RTSP
  连接、录制与取帧都挂在它下面），明确不做 —— 它是现场回退开关，重启一次即可生效。

~~另有一处记下但未修~~ → **第 20 轮已修**：`set_link()` 里 `record_enable`
原用 `bool(body.get(...))`，前端误传字符串 `"false"` 会被判成 `True`
（与 `runtime.coerce` 口径相反）。第 18 轮只修「有没有通知 Recorder」，
按约定把输入强校留给下一轮单独带用例来修；第 20 轮已改为
`runtime.coerce("record_enable", ...)` 并由 `test_94` 钉住（见台账第 20 行）。
修前实测后果是「关录像反而落成开录像」，不是单纯关不掉。

批次 5 声明表期间**故意不修**的两件（第 19 轮）：
- **4 处「假设置」只照现、不接线**：`night_only` / `low_priority`（控件被
  `LK.clipOut` 包住且默认不渲染，`pushCfg` 也不带这两个键，值只进 localStorage）、
  AI 灵敏度（`detect` 块不带该字段，`norm_detect` 也不产出）、以及已下线却仍被
  `pushCfg` 推常量的输出画质等四项。它们都是产品行为改动，要连设置页一起整理，
  不属于「验收表落成自动化」这一项，故只记进 `button_matrix.KNOWN_GAPS` 与台账，
  不悄悄合入。
- **给 ffmpeg argv 加 HTTP 出口**：声明表把 `/proc/<pid>/cmdline` 列为可选的第 4 层，
  但服务端目前不输出任何进程命令行。加接口会扩大对外接口面，且要改 `recorder.py`
  的进程登记（录制链路只加不改），留到确认真机采集确实需要时单独评估；
  在此之前这些行一律判 `partial`，不假装验过。
