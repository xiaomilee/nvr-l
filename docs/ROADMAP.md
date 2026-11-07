# NVR-L 整改路线图（5 批）

来源：外部技术评审（产品/架构/代码三视角）逐条拆解，按「风险 × 用户价值」排序，
拆成 5 个可独立验收、可独立回滚的版本批次。每批一个版本号，一批一提交。

## 总原则

1. **不推倒重写。** 录制链路（RTSP 断流、MP4 封口、进程收尾、统一取流）是真机经验，
   只加不改；新能力以新模块挂接。
2. **数字必须有出处。** 界面上任何状态/计数都要能追到真实数据源，
   不允许「看起来对」的估值（评审原文："监控系统不能给用户展示看起来正确、实际却没有验证过的数据"）。
3. **失败不等于没有。** 取帧失败、模型加载失败、断流都记「未知」，绝不写成「无活动」。
4. **升级不丢数据。** 配置、台账、录像、日志的旧格式必须能被新版本读。

## 进度表（自动化任务读这段决定下一步）

| 批次 | 版本 | 主题 | 状态 | 提交 |
|----|----|----|----|----|
| 1 | 0.9.118 | 录像可信度：真实状态 + 缺口报告 | done | 0.9.118 |
| 2 | 0.9.119 | 安全：API Token / 凭据收敛 / 前端统一请求层 | todo | - |
| 3 | 0.9.120 | 事件中心：统一事件模型 + 跨摄像头检索 + 点击定位 | todo | - |
| 4 | 0.9.121 | 检测治理：有界并发 + 时间窗确认 + 诊断完整 | todo | - |
| 5 | 0.9.122 | 前端拆分 + 文档重构 + 按钮验收自动化 | todo | - |

状态取值：`todo` / `doing` / `done` / `blocked`。`blocked` 必须在批次小节里写原因。

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

## 批次 2 · 0.9.119 安全

**目标**：把「能连上局域网就能操作」变成「有凭证才能操作」，同时不破坏手机直连与 fnOS 桌面入口。

清单：

- [ ] `var/secret.json`：首次启动生成 `api_token`（`secrets.token_urlsafe(32)`，0600 权限），
      另存 `created_at`；支持 `POST /api/auth/rotate` 轮换。
- [ ] 统一鉴权：除 `/`、`/index.html`、静态资源、`/api/auth/*` 外，所有 `/api/*` 与录像/导出下载
      要求 `Authorization: Bearer <token>`，用 `hmac.compare_digest` 比对；保留 `X-NVR-Request`（CSRF 是另一层）。
- [ ] 首次访问 `/api/auth/status` 返回 `{need_token: true}`，页面弹出「粘贴 token」引导，存 localStorage；
      401 时自动回到引导。
- [ ] RTSP 凭据收敛：GET 配置一律脱敏；编辑时密码框留空 = 保留原密码（服务端合并）；前端不再把明文写 localStorage。
- [ ] 日志/诊断统一脱敏（token、RTSP 密码、URL query）。
- [ ] 前端统一请求层 `API.get/post`（超时 + 401 + JSON + CSRF 头），替掉散落的 `fetch`。
- [ ] 测试：`test_auth.py`（401/放行/常量时间/白名单/轮换 12+ 条）、`test/ui/auth.test.js`。
- [ ] 版本 0.9.119。

**验收**：`curl http://nas:port/api/status` 无 token 得 401；带正确 token 得 200；
手机浏览器首次打开出现引导，填 token 后一切功能正常；旧配置升级后摄像头密码不丢。

---

## 批次 3 · 0.9.120 事件中心

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
- [ ] 版本 0.9.120。

**验收**：任选一条真实人员事件，页面点击后 2 秒内开始播放，且播放起点在事件发生前 10 秒内。

---

## 批次 4 · 0.9.121 检测治理

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
- [ ] 版本 0.9.121。

**验收**：8 路配置下，单轮检测总耗时不再随路数线性增长（≤ 1.8 × 单路耗时）；
人为让某路取帧失败，统计里出现 `unknown` 而不掉「无活动」。

---

## 批次 5 · 0.9.122 前端拆分与文档

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
- [ ] 版本 0.9.122。

**验收**：拆分前后 `index.html` 渲染结果逐像素一致（沿用 `test/ui/render_check.js`）；
一个新人照 README 能在 fnOS 上装起来。

---

## 不在本轮范围

评审 P3 明确不建议投入：人脸识别、车牌识别、自定义模型训练、复杂多用户权限、实时滤镜。
理由：模型依赖、硬件兼容、隐私与维护成本远超收益，现阶段先把「事件发生 → 找到录像」做透。
