# nvr-l 代码审查报告

审查对象：github.com/xiaomilee/nvr-l（nvr-slim/fpk，v0.9.12）  
审查范围：app/server/*.py（7 个模块）、app/ui/index.html（3533 行）、cmd/、打包脚本

## 🔴 安全隐患

### 1. 全部 API 无认证 + 绑定 0.0.0.0（最高风险）

- 所有接口（POST /api/config、/api/retention/cleanup、/api/restart、/api/shutdown、/api/probe）无鉴权。
- 危害链：改 `record_dir` 指向任意路径 → 定时清理对 `YYYY-MM-DD` 目录 `shutil.rmtree`（retention.py:196）→ 可诱导删除任意目录。
- 可直接看快照/实时流、下载全部录像、打挂服务。
- `/api/probe` 未认证可触发 2×120s 的 ffprobe（DoS 放大）。
- `do_POST` 不校验 Content-Type → 任何网页可跨站 CSRF（text/plain 发 JSON）。
- 修复建议：安装时生成 token（Header/参数校验）；所有 POST 要求自定义 Header（如 `X-NVR-Request`）。

### 2. RTSP 凭据经 API 泄露

- status_payload / config 响应原样返回 cfg，摄像头 url 内嵌 user:pass@。
- 日志有 \_CRED 脱敏，API 侧没有。出接口前统一过脱敏。

### 3. POST body 无大小限制

- do_POST 按 Content-Length 全量读入内存。加 1~10MB 上限。

### 4. \_static 前缀判断边角漏洞

- `full.startswith(root)` 应改为 `full == root or full.startswith(root + os.sep)`。

## 🟠 性能问题

1. **link.py::\_new_items O(pending×items)**：`any(it["key"]==k for it in self.pending)`，队列 2000 满载时每 2 秒 400 万次比较。加 `pending_keys` set。
2. **recorder.status()/list_segments 全量 walk**：每次 /api/status、/api/link 轮询都列出所有日期所有分片（90 天 × 288 片 × N 路）。复用 link.py::\_segments 的 mtime 缓存。
3. **_record 每条日志 open/append/close**：改常驻句柄或缓冲。
4. **快照 worker 只首轮并行**：之后串行，每路超时 8s×(子码流+主码流)，离线路多时一轮 32s+，interval 失效。每轮进 ThreadPoolExecutor。
5. **link.failed 字典无限增长**：按天/条数截断。

## 🟡 功能落地 / 交互缺陷

1. **GET /api/config 路由缺失**：UI syncFromServer 静默失败，「以服务端为准」从未生效。补 `if path == "/api/config"` 分支（响应脱敏 url）。
2. **UI 推送 `gpu: !!GS.gpu` 布尔污染配置**：cfg["gpu"] 变 true/false，`gpu_report` 只认 "off"、status_payload 降级判断只认 "auto" → 硬件不可用时"强制单路"保护失效。UI 改推 "auto"/"off" 或后端枚举校验。
3. **改 RTSP 地址/遮蔽区后运行中进程不重启**：recorder.reconfigure 只比对 seg/on/root；url/redact 变了旧进程继续按老参数录。逐路比对，变了 SIGINT 重拉。
4. **删除摄像头后录制进程泄漏**：set_cameras 只换列表，procs 里已删通道的 ffmpeg 永远写盘。tick 对不在 cams 的 cid 收尾。
5. **三个假按钮**：#btnCleanNow（写死"41.2 GB"toast，没调 /api/retention/cleanup）、#btnDiag、#btnScan。接真接口或隐藏。
6. **回放时间轴写死 300 秒**（realSegs）：分片改 60/900s 后显示错。从 /api/status 带segment_sec。
7. **多浏览器配置互踩**：localStorage 整份覆盖，后写赢。可用 updated_at 提示冲突。

## ✅ 亮点

- 快照 .part 原子改名；录像 moov 封口判定而非 mtime 猜测
- 保留策略按日期整目录删 + 今天/未来永不删 + .part/.pending 保护
- 幂等键含参数指纹；日志凭据脱敏；端口顺延回写三处；退避重连 dead 去重

## 建议修复顺序

安全1（token + POST 校验）→ 功能1（GET /api/config）→ 功能2（gpu 布尔）→ 功能3/4（热更新与进程收尸）→ 性能1/2。

---

# 第二轮：修复执行与验证结果

上面 16 项问题**已全部改完并跑通验证**。下面是逐项对照，以及修复过程中额外发现的 6 个真问题。

## 一、原报告问题的修复情况

### 🔴 安全隐患（4/4 已修）

| # | 问题                | 修法                                                                                          | 位置                                |
| - | ----------------- | ------------------------------------------------------------------------------------------- | --------------------------------- |
| 1 | 全部 API 无认证、可 CSRF | 所有写请求强制自定义头 `X-NVR-Request: 1` → 跨站必触发预检，而服务端不应答 CORS，浏览器直接拦下；再加 `Host` 头白名单挡 DNS rebinding | `nvr_slim.py::_write_ok/_host_ok` |
| 2 | RTSP 凭据经 API 泄露   | `runtime.redact_cfg/redact_cams/redact_url` 统一脱敏，GET/POST /api/config、/api/status、诊断导出全部走它  | `runtime.py`                      |
| 3 | POST body 无上限     | `MAX_BODY=4MB`，超限先 `_drain()` 读完再回 413（直接断连客户端只看到"连接被重置"）                                   | `nvr_slim.py`                     |
| 4 | `_static` 前缀判断漏洞  | `runtime.under(root, path)`：`path == root or path.startswith(root + os.sep)`                | `runtime.py`                      |

### 🟠 性能问题（5/5 已修）

| # | 问题                          | 修法                                     |
| - | --------------------------- | -------------------------------------- |
| 1 | link O(n²)                  | `pending_keys` 集合，O(1) 判重              |
| 2 | `recorder.status()` 全量 walk | mtime 缓存（`self._status_cache`），目录不变不重扫 |
| 3 | 日志每行 open/close             | 常驻句柄 `_LOG_FH`，路径变了才重开                 |
| 4 | 快照只首轮并行                     | `_round` 每轮都进线程池；子码流回退日志只记一次           |
| 5 | `link.failed` 无限增长          | 超 500 条截断到最近 200                       |

### 🟡 功能落地（7/7 已修）

| # | 问题                   | 修法                                                                               |
| - | -------------------- | -------------------------------------------------------------------------------- |
| 1 | `GET /api/config` 缺失 | 补路由，返回脱敏配置 + `changed` 列表                                                        |
| 2 | `gpu` 布尔污染           | 后端 `coerce()` 按 `DEFAULTS` 类型逐键收敛，枚举键走白名单；前端改推 `"auto"/"off"`                    |
| 3 | 改配置后进程不重启            | `recorder._sig(cam)` 参数指纹 + `reconcile()` 每拍对齐                                   |
| 4 | 删摄像头后 ffmpeg 泄漏      | `reconcile()` 对不在 cams 里的 cid 收尸                                                 |
| 5 | 三个假按钮                | `#btnCleanNow` 走 dry_run → 二次确认 → 真删；`#btnDiag` 真下载；`#btnScan` 真扫盘               |
| 6 | 回放写死 300 秒           | `/api/recordings` 返回 `segment_sec`，前端用 `rec.segment_sec \|\| LK.segSec \|\| 300` |
| 7 | 多浏览器互踩               | 服务端为权威，`applyServerCams` 按 id 合并，本地只保留明文 url                                     |

## 二、修复过程中新发现的问题（6 个，均已修）

1. **页面启动会用演示骨架覆盖 NAS 真实配置**（最严重）  
   启动时 `CAMERAS` 还是写死的演示数据（车库入口/一楼大堂/…），`saveCfg` 一触发就整份 POST 上去。  
   → 加 `serverCamsLoaded` 守卫 + `applyServerCams` + 启动顺序改为先 `syncFromServer` 再渲染。  
   *这个是 E2E 前 7 项集体失败暴露出来的，属于"用户换个浏览器就丢配置"的真实缺陷。*
2. **`save_config` 在 Windows 上会因文件占用抛 `PermissionError`**  
   `os.replace` 撞上杀毒/索引服务持有句柄 → 整个 `POST /api/config` 抛异常，浏览器只看到 `ERR_EMPTY_RESPONSE`。  
   → 6 次退避重试 + `fsync` + 失败时清理 `.tmp`。
3. **`do_GET` / `do_POST` 没有异常兜底**  
   任何一个未捕获异常都变成空响应，前端只能显示"失败"，日志里也没有因果链。  
   → 拆 `_do_get` / `_do_post` + 统一 500 JSON 兜底 + `_responded` 标志防止"头已发出又发 500"的畸形双响应。
4. **缺 ffprobe 时报 `TypeError: ... not NoneType`**  
   `prescan_cmd(None, src)` 把 None 拼进 argv，报错信息完全看不出是"缺 ffprobe"。  
   → `read_packets` 提前判空给出人话；`Link._process` 无 ffprobe 时直接跳过（**绝不能把整段当静止丢掉，那等于删录像**）。
5. **`coerce()` 会把 `None` 变成字符串 `"None"` 落盘**  
   `cleanup_at: null` → `"None"` → `parse_hhmm("None")` 天天抛异常 → 清理链全废。  
   → 字符串键遇 `None`/空串/非 str 一律回落默认值；枚举键只收 str。
6. **`recorder._spawn` 的退避判断方向反了**  
   参数刚变（用户刚改完 url）时反而沿用旧退避窗口，改完要干等几十秒才生效，与修复目标相反。  
   → `sig_changed` 就清零等待。

另修一处测试自身缺陷：`test_port` 用系统临时端口（Windows 49152–65535）超出业务端口上限 65000，`clamp_port` 夹回去导致随机失败。改为在端口范围内取空闲端口。

## 三、验证结果

### 后端单测（11 个文件，全绿）

```
test_condense / test_dirname / test_find_ffmpeg / test_link / test_log
test_params / test_playback / test_port / test_retention / test_snapshots   → OK
test_http_smoke.py                                                         → 17 项通过，0 失败
```

`test_http_smoke.py` 为本轮新增，覆盖：配置脱敏、`gpu` 收敛、缺 `X-NVR-Request` 403、坏 Host 403、超大 body 413、扫描、清理 dry_run、诊断导出、link/run、静态目录穿越。

### 浏览器 E2E（21 项，全绿）

`test/e2e_ui.js`：真起服务 + 真开 Chrome，只走用户实际会点的路径。

```
✅ 页面加载无 JS 报错              ✅ 清理按钮存在
✅ GET /api/config 有数据          ✅ 清理预览显示待删目录数
✅ 前端读到服务端录像目录           ✅ 预览后按钮可再次点击（二次确认）
✅ 摄像头行可展开并有「修改」入口    ✅ 确认后过期日期目录真被删除
✅ 点「修改」后真的进入编辑态       ✅ 今天的目录没有被误删
✅ 遮蔽区画布已挂载                ✅ 删除结果有回执
✅ 拖选遮蔽区后计数变化             ✅ 诊断导出触发下载
✅ 有保存按钮                      ✅ 诊断包含状态且凭据已脱敏
✅ 保存后遮蔽区真的落到服务端       ✅ 回放页读到 3 个真实分片
✅ 扫描按钮给出真实分片数           ✅ 分片时长取服务端 300 秒而非写死
                                  ✅ 日期列表包含真实存在的每一天
```

E2E 抓到的三个测试侧陷阱（已修，留作后续踩坑参考）：

- 画布在页面很下方，鼠标坐标落在视口外时 `pointerdown` 打不到元素 → 先 `scrollIntoViewIfNeeded()`
- 页面里 `SET` 是顶层 `const`，**不会挂到 window** → `page.evaluate` 里直接写 `SET`，写 `window.SET` 永远是 undefined
- `localStorage` 跨次运行残留 → `ctx.addInitScript(() => localStorage.clear())`

## 四、仍建议后续处理（非阻塞）

1. ~~二次确认没有绑定"用户看到的那份清单"~~ → **已修**（见第五轮）
2. **`link._process` 的临时目录名用 `it["key"]`**：key 是 hex 指纹，安全；但如果以后换成路径派生，需重新确认。
3. **`/api/log/export` 在超大日志时会整份读进内存**：目前日志有行数上限，暂无风险；若放开上限应改流式。

## 五、第三轮：全屏横屏 + 缩时续播（按用户反馈执行）

用户反馈三个问题：①全屏按钮要真全屏真横屏且带退出按钮；②缩时视频时长显示两小时；③短文件频繁切换影响观感。

### 5.1 全屏模块整体重写（`app/ui/index.html`）

三级降级，每级失败都不影响下一级：

```
requestFullscreen()          → 真全屏（地址栏都没了）
screen.orientation.lock()    → 手机浏览器锁横屏；桌面/HTTP 页 reject → 继续
CSS 旋转 90° 兜底            → 锁不了就把画面转 90° 铺满视口
```

- `body.fsmode`：`.phone` 铺满视口（100dvh）、隐藏 hdr/tabbar/resbar、复位 transform:scale（桌面端否则缩成一小块）
- `body.rot`：宽高互换 + `rotate(90deg)`；`layout()` 在全屏/旋转时直接 return，尺寸全交 CSS
- 退出按钮 `#fsExit` 常驻右上角（`env(safe-area-inset-top)` 适配刘海），Esc 兜底，`fullscreenchange` 监听浏览器自行退出时同步 UI
- **旋转态按钮定位教训**：把 `rotate(90deg)` 直接加在 `position:fixed` 元素上，`right/bottom` 锚定的是变换前的盒子，旋转后宽高对调会被推出视口（实测 y=791）。改为**定位与旋转分离**：外层按钮只摆位置，内层 span/svg 各自 `rotate(90deg)`，文字方向与画面内容一致（用户横过手机后读得顺）
- 三个入口（直播页 `btnLiveFull`、回放页 `lrFull`、回放横屏键 `bLand`）统一走 `toggleFull()`；`bLand` 原来只是给 `.phone` 加类（手机上只是把内容挤进小框，不是全屏），已废弃

### 5.2 缩时时长修正（后端 + 前端）

根因：缩时片段是**压缩成品**（源 5 分钟压成十几秒），前端却拿 `segment_sec`（300 秒）画时间轴 → 显示"2 小时 30 分"，实际 3 秒播完然后疯狂切文件。

- `condense.probe_duration()`：ffprobe 读 `format.duration`，缺则退到 stream duration，读不到返回 None
- `nvr_slim.file_duration()`：`(size, mtime_ns)` 缓存 + 2 万条上限淘汰；文件覆盖/正在写自然失效
- `recordings_payload()` 对 `_clip/` 清单 `f["d"] = 真实时长`，并剥离内部绝对路径
- 前端 `realSegs(kind="clip")`：`dur = f.d`（兜底 0.2s），时间轴从 0 累加 —— 是"片段轴"不是"钟点轴"；`syncPlayer` 显示 `第 N/M 段 · 本段 mm:ss`，总时长按片段累加

### 5.3 跨文件无缝续播（双 video A/B）

- `vidA`/`vidB` 交替：播 A 时预加载 B 成下一段；A `ended` → `finishSegAndNext()` → `swapVideos()` 热切换，`opacity .18s` 淡入淡出，中间不出现黑帧
- `preloadNext()` 短路条件是"目标段 + src 一致"，**外部不要置 `preloadFor = -1`** —— 置了会被 loop 每帧重新触发、与预加载互相打断，永远赶不到就绪（E2E 抓到的真缺陷：`openPlayer` 冷启动分支曾无条件清它）
- `loop` 兜底：距段尾 < 2 秒就提前 `preloadNext()`，缩时 3 秒片段也来得及
- `seekBy`/进度条拖动/`jumpSeg` 跳转后立即重排预加载；`swapVideos` 里 `curVid/nxtVid` 交换提前到过渡开始时，`switching` 防重入
- 冷启动兜底：预加载没赶上（磁盘慢）就直接前台换 src，保持播放状态不要求用户再点

### 5.4 验证

- 后端 11 个单测 + `test_http_smoke`（17 项）全绿
- E2E 扩到 **39 项全绿**，新增：缩时清单 4 项（时长不冒充 / 片段轴累加 / clip 标记 / 总时长远小于 2 小时）、全屏 6 项（fsmode / 退出按钮出现 / 右上角可点 / 点击真退 / 旋转兜底 / 旋转态按钮在视口内且文字方向一致）、Esc 退出、续播 5 项（预加载就绪 / 自动接段 / 保持播放 / A/B 热切换 / 新元素有 src）
- 截图复核（`test/shot_fs.js` → `test/.shots/`）：全屏旋转态与退出后布局正常
- `test/.shots/` 与 `shot_fs.js` 仅人工复核用，不参与断言

---

# 六、第四轮：六项功能核验与修复

用户逐条点名了六个「看起来有、实际没落地」的功能。这一轮的原则只有一条：  
**界面上每个数字、每个开关，都要能追到一行真实的数据或一次真实的副作用。**

## 6.1 录制分片长度选项是否生效 —— 原来只存在于 localStorage

**问题**：设置页能选 1/2/5/10 分钟，但这是 ffmpeg 命令行的物理参数  
（`-segment_time`）。原来它只在浏览器里存着，从没推给服务端，录制器一直按  
默认 300 秒切。

**修复**（全链路打通）：

| 层     | 改动                                                                                |
| ----- | --------------------------------------------------------------------------------- |
| 前端 UI | 编辑页新增「录制分片」芯片组（跟随 / 1 / 2 / 5 / 10 / 15 分钟），`data-camseg` 真实写入 `SET.draft.segSec` |
| 推送    | `pushCfg()` 每路带 `segment_sec: p.segSec \|\| 0`                                    |
| 服务端归一 | `norm_cameras()` / `cameras()` 用 `runtime.segment_sec()` 夹到允许范围，`0` 表示跟随全局        |
| 录制器   | 新增 `Recorder.seg_of(cam)`：单路覆盖优先，否则全局；`_spawn` 用它                                 |
| 生效机制  | `_sig(cam)` 指纹加入 `segment_sec` —— 不加入的话，改了单路长度 reconcile 认为"没变"，永远不会重启进程          |

**为什么必须进指纹**：分片长度是进程启动时定死的，改它只能"优雅 SIGINT →  
写完 moov → 重启"。指纹漏了这一项，界面改了、进程不动，用户看到的就是  
"这个选项是假的"。

## 6.2 核显调用是否生效 —— 原来页面自己编了个 QSV

**问题**：`cardGpu()` 里写死 `Intel UHD 730 / QSV`，`GS.gpu` 只是个前端布尔。  
换台没有核显的机器，页面照样宣布"核显可用"。

**修复**：新建 `app/server/sysinfo.py`，作为界面所有资源数字的唯一真实来源。

- `hw_caps(ffmpeg)`：三级判断，任何一级不过就判不可用
  1. 内核设备在不在（`/dev/dri/renderD128`、`/dev/nvidia0`）
  2. `ffmpeg -encoders` 里有没有对应编码器（`h264_qsv` / `hevc_qsv` …）
  3. 前两步都过才算（真跑一次留给后续）
- 结果缓存 5 分钟（探测要跑 ffmpeg，不能每次请求都跑）；ffmpeg 路径变了立刻重测
- **读不到返回 `None` 而不是编一个数**
- `snapshot()` 额外并入 `runtime.gpu_report()` 的 `state`/`reason`/`effective`，  
  让"页面说的"和"录制真正用的"是同一个结论

`/api/status` 新增：

```json
"res": { "proc_cpu":…, "sys_cpu":…, "mem_mb":…, "mem_total_mb":…,
         "disk_free_gb":…, "disk_total_gb":…, "hw": {…} },
"cams": { "total": N, "recording": M }
```

前端 `RES` 对象承接，`gpuLine()` / `gpuOk()` 只认后端结论。

## 6.3 输出遮蔽区下线 + 检测忽略区真正生效

**输出遮蔽区为什么必须删**：要在录像里打黑块，就必须放弃 `-c copy` 改 libx264  
重编码 —— 为一个视觉效果把所有录像的 CPU 成本翻倍。这是设计错误，不是实现  
问题。已经整体移除（UI、绘制、`record_cmd` 的 redact 分支、快照 drawbox）。  
`record_cmd()` 现在连 `redact` 参数都不收，从签名上杜绝复发。


**检测忽略区原来是纯动画**：设置页画了框，`condense.analyze()` 根本没读它。  
现在：

- `region_filter(cells)`：16×9 格号 → `crop` 滤镜链；相邻格贪心合并成矩形  
  （一片树影十几个格，不合并会生成十几路 ffprobe，磁盘读放大得离谱）
- `analyze(..., ignore=…)`：先读整帧包大小曲线，再单独读忽略区曲线，逐点相减  
  `max(0, 整帧 − 区域)`
- **逐点夹 0 而不是整段夹 0**：重叠区域和封装开销会让减数偏大
- **区域读不出来就保持原曲线**：宁可多报事件，也不能漏掉真事件
- 忽略区格号进幂等键 `item_key()` —— 改了忽略区，同一份录像必须重新判定，  
  否则旧结论会一直被当成"已处理"

为什么用 `crop` 而不是 `drawbox`：判定要的是"**丢掉**这块区域的变化"。  
drawbox 只是把像素涂黑，静物在遍历里仍是一块零；而树的晃动被涂黑后反而变成  
"黑块从无到有"，照样触发变化。

## 6.4 手势返回不能直接退出应用

**问题**：以前只有"非直播页 → 回直播页"一档。实时播放层里左滑 → 整个应用直接  
退到桌面（用户以为闪退）；设置页展开了摄像头 → 直接跳回直播页，丢掉上一层。

**修复**：一个真正的返回栈，每层压一条历史：

```
播放器 → 实时层 → 设置展开/编辑草稿 → 二级页 → 直播快照墙(根)
```

`popstate` 按"最内层优先"逐层退；`atRoot()` 判定是否已经在根——  
只有到根才把返回交还系统去退应用。

**踩到的坑**：一度用 `history.go(-(history.length - 1))` 来"弹干净"。  
但 `history.length` 是**整个标签页会话**的累计长度（用户之前逛过的页面也算），  
用它算会一次性跳过头，表现还是"一点就退出应用"。改为自己维护 `LAYERS` 计数。

## 6.5 页面注释里的数字必须是真实动态的

| 位置     | 原来                                 | 现在                              |
| ------ | ---------------------------------- | ------------------------------- |
| 顶栏资源条  | `CPU 6%` `内存 96.4 MB` `核显 QSV`     | `/api/status` 实测值，读不到显示 `—`     |
| 硬件卡片   | `178%` `312.6 MB` `Intel UHD 730`  | 进程/整机 CPU、RSS/总内存、`hw_caps` 结论  |
| 摄像头数   | HTML 写死                            | `cams.total` / `cams.recording` |
| 快照角标日期 | 硬编码 `2026-10-20`                   | 真实时间戳                           |
| 下次扫描时刻 | 硬编码 `16:45` / `17:00` / `明天 03:00` | 从当前时刻向上取整到下一档                   |
| OSD 时间 | 硬编码                                | 播放位置                            |

`nextScan()` 会算出真实的"下一个 15 分整 / 下一个整点 / 明天 03:00"，  
跨天自动加"明天"前缀。

**顺带修掉一个隐藏矛盾**：`NVR_SIMULATE_GPU` 是演示开关，`runtime.gpu_report()`  
会返回模拟值，而 `sysinfo.hw_caps()` 不读它 —— 两边会互相打架（页面说"可用"，  
探测说"没有 /dev/dri"）。现在 `hw_caps` 如实带出 `simulated` 标记，界面显示  
"（模拟演示）"。

## 6.6 快照倒计时：断网时必须真反映

**问题**：`tickSnaps` 是"到点就把 `st.at = now`，然后发一个请求"。  
请求成不成功都算刷新成功 —— 断网时倒计时照常 10→0→10 循环，看起来一切正常。

**修复**：新增 `pullSnap(camId, img, cb)`，**等 `load` 事件才算成功**：

- 成功 → 前推 `at`、清 `fails`
- 失败/超时（3 秒）→ `fails++`、记 `since`、**不动 `at`**
- `pend` 防重入，不会叠请求
- 倒计时显示：`未更新`（失败）/ `…`（请求在飞）/ `-Ns`（正常）/ `—`（从未成功）
- 失败态优先于离线态：网断了 `c.online` 也是 false，但用户更该看到"未更新"
- 从未成功过时角标显示 `--`，不拿"现在"顶上去假装有图

原来的 `hookSnapImg` / `refreshSnapImg` 已删除 —— 它们不关心加载结果，  
正是"断网也倒数"的根源。

## 6.7 验证

| 测试                               | 结果           |
| -------------------------------- | ------------ |
| 后端单测 `test/test_*.py`            | **126 项全通过** |
| HTTP smoke `test_http_smoke.py`  | **23 项全通过**  |
| UI 单测 `test/ui/settings.test.js` | **117 项全通过** |
| 浏览器 E2E `test/e2e_ui.js`         | **64 项全通过**  |

本轮新增的针对性断言：

- 分片长度：`seg_of` 覆盖/跟随/`-segment_time` 实际值/指纹含它
- 忽略区：`region_filter` 合并与越界丢弃 / 逐点夹 0 / 读失败回退 / E2E 真拖选 + 落服务端
- 遮蔽区：`record_cmd` 拒收 `redact` 参数 / 命令无 `drawbox`、无 `libx264` / 配置里无 `redact`
- 资源：`res` 字段齐全 / `hw` 有 `best`+`error` / 页面不出现 `6%`、`96.4 MB`、`UHD 730`
- 历史栈：进二级页压 1 层 / 返回后归零 / 播放器再压一层 / 返回先关播放器后仍在回放页
- 倒计时：失败进失败态 / 显示"未更新" / 标红 / 不显示"刚刚" / 正常时恢复 `-Ns`

## 6.8 测试基础设施的两个真缺陷（顺带修掉）

1. `test/ui/harness.js` 的 `localStorage` 是**空转桩**（`getItem` 永远 null、  
   `setItem` 什么都不做）。所有"存进去再读回来"的断言在这套 harness 下  
   从来就不可能通过 —— 测试等于在验证一个假实现。改为内存实现。
2. `settings.test.js` 里断言 `toggleLiveFull` 存在，但页面已改名 `toggleFull`；  
   另外第 11 段用一份新 localStorage 覆盖了前一段的存档，导致"持久化读回"  
   必然失败。均已修正。

## 6.9 仍建议后续处理（非阻塞）

1. **`hw_caps` 的第三级"真跑一次"没做**：目前是设备 + 编码器两步。驱动坏了  
   但设备节点还在的极端情况会误判为可用。可以在首次使用硬件编码失败时回写缓存。
2. **分片长度的"优雅重启"**：目前 `reconcile()` 检测到指纹变化会重启进程。  
   SIGINT 后要等 ffmpeg 写完 moov，如果进程卡住需要兜底 SIGKILL —— 现有  
   `STALL_SEC` 看门狗能覆盖，但重启窗口内那一路会有几秒空档。
3. **忽略区的计算成本**：每多一路 `crop` ffprobe 就多读一次文件。目前靠矩形  
   合并把数量压住，但用户如果画得非常零碎（几十个独立格子），压制会明显变慢。  
   可以考虑在前端限制独立矩形数量上限并提示。
4. **`sysinfo.sys_cpu_pct()` 需要两次采样**，首次调用拿到的是 uptime 均值  
   （冷启动瞬间数字会偏"平滑"），第二次起才准确。

---

# 七、第五轮：五项用户反馈（v0.9.14）

> 用户原话：`等迭代稳定后再更新到github上，现在发现以下问题：1、无法录制任何全时视频，2、硬件与资源无法调取系统实时占用，建议去掉，保留存储与目录信息，3、缩略图合成雪碧图，去掉这个功能，4、快照页面可以更新，但是点击画面进入实时直播页面时，无论单画面或宫格画面，均是静止的，单画面卡在等待第一帧，宫格画面看起来是几秒前的快照，没有更新，也不会同时播放，5、实时预览单摄像头的播放和暂停按钮悬浮在画面上，设计一下交互页面，减少画面遮住`

## 7.1 无法录制任何全时视频 —— 又是"精简 ffmpeg 不认参数"

**根因（和第 5 轮的 `-rw_timeout` 是同一类错误）**：`record_cmd()` 里多带了  
一个 `-timeout 10000000`。fnOS 自带的精简版 ffmpeg 不认这个选项，进程立刻  
以 `Unrecognized option 'timeout'` 退出。表现极具迷惑性：

- 快照能看（快照只用 `ffmpeg -ss/-frames:v 1`，没这个参数）
- 实时预览能看（预览走另一条命令）
- **唯独一条录像都没有**，且进程起来就死，日志被 stderr 的 `-v error` 刷满

0.9.12 的 docstring 里我自己写着"不依赖 ffmpeg 的 `-rw_timeout`"，  
说明当时知道这个坑，却换成了 `-timeout` 以为更保险 —— 结果只是把一个不认的  
选项换成另一个不认的。同一个坑踩两次，这次把结论写死在注释里。

**修复**：

1. **录制命令里一个超时参数都不留**（`-timeout` / `-rw_timeout` / `-stimeout`  
   全不出现）。读超时统一交给 `recorder.tick()` 的软件看门狗。这个决定写进了  
   `record_cmd()` 的 docstring，避免下次又有人"顺手加个超时"。
2. **看门狗补上"从未写盘"这一格**。原来写的是  
   `if age is not None and age > STALL_SEC:` —— `_newest_age()` 在目录里  
   一个分片都没有时返回 `None`，于是这个 `and` 恰好把"进程活着但一个字节没写"  
   这种**最像本次故障的情形显式排除**掉了。进程起不来就算了，最怕的是它  
   活着、不报错、不写盘，看门狗也当没看见。  
   改为 `if age is None or age > STALL_SEC`，日志明确区分  
   `从未写入` / `N 秒没有新数据`（`test_watchdog_covers_never_wrote` 守着）。
3. **每路录制状态在页面上可见**。新增 `recCamsBlock()`，把  
   `state / error / last_seg / retry` 逐路列出来。以前用户只能看到"录制开关是  
   开的"，不知道底下四路全死了。
4. **`ffmpeg_missing` 显式标记**。`/api/link` 里带上这个布尔值，UI 直接给  
   可执行指引（把静态 ffmpeg 放到 `app/bin/`，或 `apt install ffmpeg`），  
   而不是让用户对着一排灰点亮瞎猜。


5. **目录不可写提前拦**。`_spawn()` 里先 `ensure_day()`，失败就写  
   `last_error` 并打印"录像目录不可写：<路径>（请在设置里换成可写目录）"，  
   不再让 ffmpeg 去撞墙。
6. `last_error` 保留**整段**而非最后一行（`" ".join(err.split())[:300]`）——  
   ffmpeg 的真正原因常常在第一行，只留最后一行会得到无用的空行。

## 7.2 硬件与资源实时占用 —— 下线

`sysinfo.sys_cpu_pct()` 只有在 Linux 读 `/proc/stat` 才准，而 fpk 跑在  
fnOS 的沙箱里，"系统实时占用"拿到的其实是整机数字，和这个应用关系不大，  
展示出来反而是误导（用户第 4 轮已经指出过"6% / 96.4 MB"是编的）。

**改动**：删掉资源条里的 `资源` 那一行（CPU / 内存 / 核显），只保留  
**存储**（剩余 / 共 / 保留）和**目录**（实际落盘路径）。同时：

- `REC.diskTotalGB / diskFreeGB` 从写死的 `473.9 / 336.0` 改成 `null`，  
  由 `/api/status` 的 `disk_free_gb` / `disk_total_gb` 真实填充；拿不到就显示 `—`
- `recBudgetText()` 用 `free == null ? true : need <= free`，避免把 `null`  
  当 0 用而误报"空间不足"
- 删除 `cardGpu()`（51 行）、`fmtPct` / `fmtMB` / `gpuLine` / `gpuOk`  
  四个死函数，`RES` 对象瘦身为 `{ok, at, err}`
- 硬件卡里的三个压制开关（同时处理 / 只在夜间跑重编码 / 低优先级）没有删，  
  而是并进 `cardLink()`——它们是**真实会影响调度**的选项，不属于"实时占用"

## 7.3 缩略图合成雪碧图 —— 下线

`GS.sprite` 及其在 `pushCfg` / `loadCfg` 里的读写全部删除，  
`runtime.DEFAULTS` 里的 `sprite_thumbs` 也一并去掉。理由：雪碧图需要额外跑  
一遍解码 + 拼接，收益只在"数千张缩略图列表"场景，而这个应用一屏最多几十路。

## 7.4 实时直播静止不动 —— 三个独立 bug 叠在一起

这条是最影响观感的，实际是**三个 bug 同时存在**：

**bug ①：单画面卡在"等待第一帧"（前端）**  
`buildLiveRoom()` 的流分支只做了 `img.src = liveStreamUrl(...)`，**从不给瓦片  
加 `hasphoto` class**。而 `.nophoto` 覆盖层是 `z-index: 3`，永远压在 `<img>`  
上面 —— 视频其实在播，只是被一层"等待第一帧"的遮罩盖住了。  
→ 抽出 `mountLiveStream()`，在 `img.onload` 里才加 `hasphoto`，加载失败回退快照。

**bug ②：宫格永远只拉快照（前端）**  
分支写的是 `if(!grid && state.liveStream)` —— 宫格被显式排除在流之外，  
所以"宫格画面看起来是几秒前的快照，没有更新"是必然的。  
→ 单画面和宫格都走 `mountLiveStream()`，宫格用更低参数  
`STREAM_Q.grid = "fps=4&w=480"`，单画面用 `"fps=8&w=960"`。

**bug ③：服务端硬卡 2 路并发流（后端）**  
原来直接写死 `if STATE.get("live_streams", 0) >= 2: 503`，宫格第 3 路开始  
必然 503。四路宫格只能动两个，看起来就是"有的在动有的不动"。  
→ 提成具名常量 `MAX_LIVE_STREAMS = 8`（4 路宫格 + 若干单画面放大都够）。

顺带修的两个真问题：

- **`-an` 必须加**：子码流带 G.711/G.726 音轨时，`-f mpjpeg` 写头会失败退出。  
  表现为"流开了但一帧没有"。
- **`p.stdout.read(16384)` 会攒满 16KB 才返回**：低码率子码流几秒才推够一次，  
  于是画面"一顿一顿"。改 `os.read(fd, 65536)`。
- `-fflags nobuffer -flags low_delay`：不加会晚 2~3 秒，观感就是"卡在几秒前"。

**暂停语义也重做了**：暂停 = 真断流（摘掉 `img.src`），瓦片显示居中的  
`▶` 图标；恢复 = 重新建流。`leaveLive()` 会清空 `#lrBody.innerHTML`，  
不留后台解码的僵尸 `<img>`。

## 7.5 播放/暂停按钮遮画面 —— 移到控制条

原来是一个悬浮在画面右下角的胶囊（`.lr-float` + `data-lrplay`），  
全屏时正好压住时间戳和画面主体。改为：

- 控制条新增 `#lrPlay`（`class="lr-play"`），带状态点 + 文案（暂停 / 继续）
- **播放中，画面上不出现任何控件**（这是关键诉求）
- 暂停时瓦片中央才显示 `▶`，点一下恢复
- 宫格在播放中点击瓦片 = 放大该路（原行为）；暂停中点击 = 恢复播放

`.lr-float` 的全部 CSS 规则已删除，`data-lrplay` 属性不再存在  
（`test/ui/settings.test.js` 里有断言守着，防止又长回来）。

## 7.6 验证

| 测试                               | 结果                   |
| -------------------------------- | -------------------- |
| 后端单测 `test/test_*.py`            | **138 项全通过**（11 个文件） |
| HTTP smoke `test_http_smoke.py`  | **25 项全通过**          |
| UI 单测 `test/ui/settings.test.js` | **120 项全通过**         |
| 浏览器 E2E `test/e2e_ui.js`         | **72 项全通过**          |
| 包内容独立校验 `tools/verify_pkg.py`    | **50 项断言全通过**        |

本轮新增的针对性断言：

- 录制：`record_cmd` 里**不存在** `-timeout` / `-rw_timeout` / `-stimeout`  
  （按带引号的命令数组元素匹配，注释里的说明文字不算残留）
- 看门狗：`test_watchdog_covers_never_wrote` —— 目录空（`age is None`）也判卡住
- 直播命令：`-an` + `mpjpeg` 必在 / 宫格参数比单画面低 / `width=0` 不缩放 /  
  奇数宽取偶 / fps 越界夹取
- 直播前端：宫格也走流 / 流失败回落快照 / `id="lrPlay"` + `class="lr-play"` /  
  旧 `.lr-float` 胶囊已移除
- 资源条：只剩存储 + 目录 / `resCpu`、`resMem`、`resGpu` 元素不存在 /  
  `cardGpu` 函数不存在
- 配置：`/api/link` 带回每路录制状态 + `ffmpeg_missing` 标记

**伪 ffmpeg 探针实测录制器**（本机没有真 ffmpeg，用会快速退出并写文件的  
假二进制驱动）：命令里 `含 -timeout: False`、9 秒内 0 次额外重启、  
产出 4 个 `.mp4` + 1 个 `.part`、stop 后 `.part` 全部封口成 `.mp4`。

**解包实跑**（把 `app.tgz` 解开直接跑，`TRIM_SERVICE_PORT=18197`）：

- `/api/health` → `{"ok": true, "version": "0.9.14", "ffmpeg": null}`
- `/api/link` 的 `record.root` = `/vol1/@appshare/nvr-output`（与 `runtime.DEFAULTS`  
  同一个来源）、`ffmpeg_missing=true`、4 路 channels（`error/id/last_age_sec/
  last_seg/name/pid/retry/segment_sec/segments/state` 齐全）
- 启动日志明确打出 `录制未启动：系统里没有 ffmpeg`

**产物**：`dist/nvr-0.9.14_x86.fpk`，163.2 KB，包 md5 `bcb57dce52b092ebd27e87cae3e3205f`。

## 7.7 本轮顺带修掉：录像目录默认值分叉

`nvr_slim.record_dir()` 的回退写死 `/vol2/1000/NVR`，`runtime.DEFAULTS["record_dir"]`  
是 `/vol1/@appshare/nvr-output`，`main()` 启动时算 `rec_root` 又用了第三套  
`os.path.join(a.var, "rec")` —— 同一个概念三处取值。

正常路径下 `load_config()` 从 `DEFAULTS` 起步，回退走不到；但只要配置文件里  
`record_dir` 被写成空串或 null（手改、旧版迁移、写盘中断），三处就会分叉：  
**设置页显示 A 目录，录制器实际写 B 目录，录像"凭空消失"而日志一切正常**。  
这类问题排查成本极高（用户会先怀疑摄像头、再怀疑权限，最后才发现目录不对）。

改为两处都只认 `runtime.DEFAULTS["record_dir"]`，并新增  
`test/test_record_dir.py`（6 项）守着：源码里不许再出现写死字面量、  
两条取值路径必须落在同一来源、配置被写坏时空串/null/自定义值三种情形下  
`record_dir()` 与 `main()` 的算法结果必须相等。

顺带修 `build.sh` 的测试收集方式：原来是逐个列文件名，漏掉了  
`test_condense.py`（31 项）、`test_dirname.py`（7 项）、`test_playback.py`（3 项）——  
它们一直在跑、却从没进过构建闸门。改成遍历 `test/test_*.py`，新加的测试自动纳入。

## 7.8 本轮仍未处理（非阻塞，已知）

1. **包里没有 ffmpeg 二进制**（`app/bin` 只有 README）。真机依赖系统 ffmpeg；  
   没有时页面会明确告知，但录制/预览都用不了。要彻底解决得把静态构建  
   （约 40~70 MB）打进 fpk，包体积会从 163 KB 涨到几十 MB，需要你确认是否接受。
2. **`PORT` / `DATA_DIR` 环境变量名是 `TRIM_SERVICE_PORT` / `TRIM_PKGVAR`**，  
   手工起服务时容易踩（本轮测试就踩了一次），可考虑在 `--help` 里写清楚。

---

# 八、第六轮：真机日志推翻了我的判断（v0.9.15）

> 用户装了 0.9.14 之后贴来一份真机日志。**上一轮我对"录不上"的归因是错的** ——  
> 日志直接把它推翻了，这里如实记下来。

## 8.1 日志说了什么

```
17:19:52 录制 5 路已启动
17:19:54 录制 cam-mvh3ropd 中断（code=234）：
         [out#0/segment] Output file does not contain any stream
         Error opening output file /vol1/@appshare/nvr-output/院子 L/%Y-%m-%d/%H-%M-%S.mp4.part
         Error opening output files: Invalid argument
         → 2 秒后同一句话 ×5 路，然后每 5/10/20/30 秒重连一次，共 10 次
17:24:06 用户把目录改成 /vol2/1000/NVR/NVRL 测试 → 仍然是同一个错
17:20:02 起 GET /api/status 异常：IndexError('tuple index out of range')，每几秒一次
```

**证据链**：

1. `ffmpeg=/bin/ffmpeg` —— 系统 ffmpeg 存在，不是"缺 ffmpeg"
2. 换目录后报错完全一样 —— **不是目录权限问题**（我上一轮补充的"目录不可写"判断没命中）
3. 失败发生在启动后 **2 秒** —— 说明 ffmpeg 没等到流就放弃了
4. `Output file does not contain any stream` 是 **mux 初始化阶段**的错误，  
   含义是"输出侧 map 不出任何流"，跟写盘路径无关（日志里那行 `%Y-%m-%d`  
   未展开只是 ffmpeg 打印模板，strftime 是在真正 avio_open 时才做的，正常现象）


## 8.2 真正的根因：探测耐心不够

`-v error` 会吞掉大部分提示，但残留的 `[h264] non-existing PPS 0 referenced` /  
`no frame!` 说明：ffmpeg 在做 stream probe 时**没拿到 SPS/PPS，也没解出任何一帧**。  
摄像头建连后通常要 1~3 秒才吐出第一个 IDR，而 ffmpeg 默认只给约 2 秒 / 5MB  
就结束探测。探测结束 → h264 parser 拿不出参数集 → 该流被判为无效 →  
默认 map 选不出输出流 → mux init 报 "does not contain any stream" 并退出。

这解释了全部现象：为什么快照能看（快照命令用 `-frames:v 1` 会一直等）、  
预览能看（另一条命令）、**唯独录制一条都没有**，以及为什么"2 秒就死"。

**修复**：`INPUT_OPTS` 加上 `-analyzeduration 10000000` / `-probesize 10000000`，  
放在 `-i` 之前（输入选项放错位置等于没加）。

## 8.3 顺带治好的三个真问题

**① `/api/status` 每 3 秒 500，永不恢复**

`sysinfo.py`:

```python
_TICK = (0.0, 0.0)          # ← 初始值只有两个字段
...
with _LOCK:
    pb, pt = _TICK[1], _TICK[2]     # ← 首次调用 IndexError
    _TICK = (time.time(), busy, total)   # ← 异常一抛，永远执行不到
```

初始值是 2 元组、使用处按 3 元组下标访问。**更致命的是赋值写在访问之后** ——  
异常一抛就再也走不到赋值，于是 `_TICK` 永远停在 2 元组，**每次**请求都抛同一个错。  
这就是日志里那条错误从头到尾不断出现的原因。

修：初值改 3 元组 + 用解包 `_, pb, pt = _TICK`（以后谁再改结构会立刻 ValueError）。

**② 附加信息失败拖垮整个接口**

`res` 只是"存储剩余/目录"这类附加信息，它出错却让整个 `/api/status` 500，  
摄像头列表、录制状态、看板一起空白。新增 `_safe_res()`：采集失败只降级该项，  
并在日志里限流记录（60 秒一条，带累计次数）。

**③ 重连风暴把日志刷成噪音**

退避封顶 30 秒 → 5 路全断时每 6 秒起一个进程再死，7 分钟刷出 250 行同一句话。  
改：封顶 300 秒 + 同一路同一错误只打前 3 次、之后每 20 次汇总一条。

## 8.4 参数兼容：不再靠记性

0.9.11 的 `-rw_timeout`、0.9.13 的 `-timeout`，加上这轮的 `-analyzeduration`  
风险 —— 精简版 ffmpeg 支持哪些参数**无法预知**。所以这轮加了**自愈**：

```
ffmpeg 报 "Unrecognized option 'x'"
  → 若 x 在 INPUT_OPTS 里：加入黑名单、命令行立即重建、下一轮巡检立刻重试
  → 若 x 不在（比如用户把地址写错引发的其他选项）：照旧上报，不吞掉
```

写完才发现一个会让它**静默失效**的细节：ffmpeg 说的是 `'probesize'`（不带横线），  
而 INPUT_OPTS 里写的是 `-probesize`，字符串直接比永远不相等 —— 自愈逻辑  
看起来在跑、实际一次都不会命中。测试 `test_absorb_only_known` 当场抓到了它。  
现在两边都 `lstrip("-")` 归一化后再比。

## 8.5 每路「诊断」：让应用自己证明原因

日志只能给"现象"，而"录不上"有三种完全不同的原因（没码流 / 编码不支持 / 连接被拒），  
从一行 stderr 分不清。所以设置页每路加了一个「诊断」按钮，后端 `_probe_one()`：

1. **ffprobe 读流**：`-show_streams` 拿到有没有视频流、编码、分辨率、帧率
2. **试录 8 秒**：用 `-v info`（不是 error）跑一遍，把 `Stream mapping` 捞出来 ——  
   这是"到底有没有输出流"的铁证
3. **给结论**：按错误分类直接说人话
   - 「摄像头没有给出视频流」→ 核对 RTSP 通道号
   - 「有视频流但拿不到关键帧/参数集」→ 换子码流试试
   - 「这台 ffmpeg 缺少该编码格式的封装支持（检测到 hevc）」→ 换子码流
   - 「摄像头拒绝了这个连接」→ 核对密码 / 并发上限

诊断是**真跑**，不是查缓存，所以结果可复现。加了进程内锁，避免并发点把  
摄像头仅有的几路连接抢光反而得出"连不上"的假结论。

诊断文件同时落盘到 `var/record-diag/<cam>.log`（命令 + 完整 stderr 尾 8KB，  
密码遮掉、IP 保留），并且并入「导出诊断」—— 用户点一次就够，不用去翻目录。

## 8.6 顺手修的两处

- **跨天目录不预建**：ffmpeg 的 `-strftime` 只替换 `%`，**不会 mkdir**。  
  零点一过它就去写 `2026-10-22/`，目录不存在当场退出 —— 表现是"每天零点整  
  所有通道同时断一次"，很容易被误判成摄像头问题。现在 `_spawn` 时把明天的  
  目录也建好。
- **异常日志没有位置信息**：原来只记 `%r`，用户发来日志，能定位的范围是"整个后端"。  
  现在记最内层帧：`GET /api/status 异常：sysinfo.py:67 in sys_cpu_pct() → IndexError: ...`。

## 8.7 验证

| 测试           | 结果            |
| ------------ | ------------- |
| 后端单测（12 个文件） | **165 项全通过**  |
| HTTP smoke   | **28 项全通过**   |
| UI 单测        | **129 项全通过**  |
| 浏览器 E2E      | **72 项全通过**   |
| 包内容独立校验      | **62 项断言全通过** |

新增 `test/test_record_heal.py`（27 项），其中两条是**反向验证过的**：

- 把 `_TICK` 改回 2 元组 → `test_first_call_does_not_raise` 与  
  `test_initial_length_is_three` **都失败**（说明它们真能抓住原 bug）
- 把 `_absorb_bad_opts` 的归一化去掉 → `test_absorb_only_known` 失败

第一条一开始没做到：测试里手动把 `_TICK` 设成 3 元组，等于把被测的初始值覆盖掉了，  
注入原 bug 也照样通过 —— 和之前 `harness.js` 的 localStorage 空转桩是同一类错误  
（测试验证了一个假实现）。改成 `importlib.reload(sysinfo)` 拿源码真实初值后才算数。

**解包实跑**（`TRIM_SERVICE_PORT=18199`）：

- `/api/health` → `{"ok": true, "version": "0.9.15", "ffmpeg": null}`
- `/api/status` → **200**，`res` 字段齐全
- `/api/link` → `record.bad_opts=[]`，每路 channels 带 `kind` / `hint`
- `/api/record/probe?id=__nope__` → 400（未知通道）
- `/api/log/export` → 含「录制诊断」段和完整 stderr，`rtsp://` 凭据已脱敏

**产物**：`dist/nvr-0.9.15_x86.fpk`，174.3 KB，app.tgz md5 `62f3b4aabc21963a9013739157a30b41`。

## 8.8 这轮没做的（等你决定）

1. **H.265 自动降级**：如果诊断确认是 HEVC 而这台 ffmpeg 封不了 mp4，  
   可以把分片改成 `.ts`（TS 不需要 extradata，容错好得多）。但回放链路  
   现在按 `HH-MM-SS.mp4` 匹配，改格式会牵动回放、清理、缩时三处，值得单独一轮。
2. **静态 ffmpeg 打进包**：+40~70 MB。现在依赖系统 ffmpeg，缺了会明确提示。

---

# 九、第七轮：诊断结果推翻了"摄像头问题"（v0.9.16）

> 用户在真机上点了 0.9.15 新增的「诊断」，贴回结果：**试录成功**。

## 9.1 这份诊断证明了什么

试录日志给出了完整的证据链：

```
Input #0, rtsp: Stream #0:0 Video: h264 (Main), 1920x1080, 25 tbr
             Stream #0:1 Audio: aac (LC), 16000 Hz, mono
Stream mapping: Stream #0:0 -> #0:0 (copy)
试录产出：1 个分片 · 1301 KB
```

三条结论直接成立：

1. **不是 H.265**——主码流是 H.264 Main，这台 ffmpeg（Lavf62.12，相当新）  
   封装它毫无问题；
2. **不是"关键帧来得太慢"**——加了 `-analyzeduration/-probesize` 之后  
   探测拿到了参数集（日志里只剩一句无害的 `Missing PPS in sprop-parameter-sets,
   ignoring`，SDP 里没带 PPS，ffmpeg 从流里自己解出来了）；
3. **不是摄像头拒绝连接**——建连、拉流、映射、落盘全程走通。

那 5 路全灭还剩什么解释？**试录和正式录制唯一没被同一命令覆盖过的差异：  
输出路径**。旧试录写 `var/probe/%H-%M-%S.mp4`（扁平、无日期子目录、无  
.part 后缀），正式录制写 `<录制目录>/<摄像头名>/<日期>/%H-%M-%S.mp4.part`。  
「试录成功」证明不了正式路径可用——诊断结果和录制状态互相矛盾，却无法定位。

## 9.2 修复：试录改走正式录制的真实路径

`_probe_one()` 的试录段重写：

- **真实根目录**（取常驻录制器的 `rec.root`，不是临时目录）；
- **真实目录结构**：`<root>/<摄像头名>/%Y-%m-%d/…mp4.part`，含 `.part` 后缀；
- **真实分片长度**（`rec.seg_of(cam)`，不再写死 60 秒）；
- 输入选项与正式录制完全同源（`_eff_opts()`），这点 0.9.15 就对了；
- 只差三项不影响结论的：`-t 8`、`-v info`、文件名加 `__probe__-` 前缀  
  （前缀是为了和常驻录制正在写的分片区分开，跑完**只清自己的产物**，  
  不碰真实录像，也不在回放时间线里留下 8 秒的诊断碎片）；
- 目录建不出来时直接给 `dir` 类结论，不再白跑 ffmpeg。

试录成功的 verdict 同步升级：如果常驻录制此刻没在录，明确告诉用户  
"这是上次失败的自动重连等待（最长 5 分钟），重试后仍未恢复再导出诊断"——  
0.9.15 把退避封顶从 30 秒提到 300 秒之后，这种"看起来矛盾"的状态会更多见，  
必须由页面自己解释。

## 9.3 验证

`test_record_heal.py` 新增 `TestProbeTrialRealPath`（3 项），用可编程的假  
subprocess 桩验证：

- 试录命令输出落在传入的真实根目录、带日期子目录与 `__probe__-…mp4.part`  
  形态、用真实分片长度、`-analyzeduration` 在 `-i` 之前、`-v info`、`-t 8`；
- 试录产物全部清理干净（`__probe__-` 前缀零残留）；
- 目录不可写 → `kind=dir`，不启动 ffmpeg；
- ffprobe 有视频流 + 试录报 no_stream → 提示指向"换子码流"而非"核对通道号"  
  （这条一开始测出来的是后者——桩没模拟 ffprobe，走了"无视频流"分支，  
  恰好证明 verdict 分支依赖 ffprobe 结果，补上桩后覆盖真实组合）。

后端 168 / smoke 28 / UI 129 / E2E 72，全绿。  
产物 `dist/nvr-0.9.16_x86.fpk`。

## 9.4 下一轮的判定路径（写给下一次排障）

用户升级 0.9.16 后再点一次「诊断」：

- **试录失败** → 弹窗里的报错就是正式录制的报错（同一条命令），  
  连同「导出诊断」发回来即可定位；
- **试录成功且状态恢复** → 结案，之前是输入侧探测问题 + 退避窗口的叠加观感；
- **试录成功但状态长时间不恢复** → 导出诊断，看常驻录制器的  
  `record-diag/<cam>.log` 里的实时 stderr——那是唯一剩下的未知量。

---

# 十、结案：`.part` 后缀（v0.9.17）

> 0.9.16 的真实路径试录当场复现了正式录制的报错 —— 这一次不用再猜了。

## 10.1 决定性对照

同一台机器、同一条 URL、同一个 ffmpeg、同样的输入选项，只差输出模式：

| 试录模式      | 输出模式                                                 | 结果                                       |
| --------- | ---------------------------------------------------- | ---------------------------------------- |
| 0.9.15 试录 | `var/probe/%H-%M-%S.mp4`                             | ✅ 映射成功，产出分片                              |
| 0.9.16 试录 | `<root>/<摄像头名>/%Y-%m-%d/__probe__-%H-%M-%S.mp4.part` | ❌ `does not contain any stream` + EINVAL |

两次的**输入侧 dump 几乎一字不差**（h264 Main 1920×1080 都解析出来了），  
差异只剩文件名后缀。ffmpeg 的源码解释了一切（`av_guess_codec`，format.c）：

```c
if (av_match_name("segment", fmt->name) || av_match_name("ssegment", fmt->name)) {
    const AVOutputFormat *fmt2 = av_guess_format(NULL, filename, NULL);
    if (fmt2) fmt = fmt2;          /* 按输出文件名扩展名重新猜封装 */
}
```

segment muxer **不声明任何编码**（`video_codec = AV_CODEC_ID_NONE`，源码里  
结构体确实没有 `.video_codec` 字段）。于是自动流映射走到 `av_guess_codec`：

- 模式以 `.mp4` 结尾 → 按扩展名猜到 mp4 族封装 → video_codec 非空 → 正常映射
- 模式以 `.part` 结尾 → **没有任何 muxer 的 extensions 匹配 `.part`** →  
  `av_guess_format` 返回 NULL → 保持 segment → video_codec = NONE →  
  **视频不被自动映射**，`-an` 又挡掉音频 → 输出侧 0 条流 →  
  `Output file does not contain any stream` + `AVERROR(EINVAL)` 退出

`Error opening output file … Invalid argument` 就是这个 EINVAL 的报壳，  
和目录、权限都无关。

## 10.2 为什么错了这么多轮

| 轮次            | 当时的归因                         | 实际                                                       |
| ------------- | ----------------------------- | -------------------------------------------------------- |
| 0.9.8–0.9.10  | —                             | `.part` 后缀从第一天就在                                         |
| 0.9.11/0.9.13 | `-rw_timeout`/`-timeout` 不被识别 | 那是真问题，但不是这个                                              |
| 0.9.14        | 目录不可写 / 文件名抓不到                | 被真机日志否定                                                  |
| 0.9.15        | 探测耐心（关键帧太慢）                   | analyzeduration 确实该加，但**不是根因**——它"见效"是因为试录碰巧用了 `.mp4` 后缀 |
| 0.9.16        | 输出路径（方向对了）                    | 试录走真实路径后当场复现，锁定后缀                                        |

教训：**0.9.15 的"验证通过"是假阳性** —— 试录命令与正式命令不是同一条，  
"试录成功"证明不了正式路径。任何自证手段，只要和被验证的对象有一处差异，  
那一处就是盲区。

## 10.3 修复：显式 `-map 0:v:0`

`record_cmd()` 在 `-i` 之后加 `-map 0:v:0`（在 `-c copy` 之前）。  
显式映射完全绕开 av_guess_codec 的扩展名猜测，输出模式叫什么都能映射。

副作用与守卫：

- 摄像头真没给视频流时，报错从"0 条流"变成明确的  
  `Stream map '0:v:0' matches no streams.`——`classify_error` 新增  
  `no_video` 规则，提示"核对 RTSP URL 里的通道号/码流类型"
- `-map` 是 ffmpeg CLI 自带选项，任何构建都认，不进 bad_opts 自愈名单
- `test_record_heal.py` 新增 `TestExplicitMap`（3 项）：`-map` 存在且位置  
  正确（`-i` 之后、`-c` 之前）、INPUT_OPTS 仍在 `-i` 之前、  
  `matches no streams` 分类为人话提示

回归：后端 171 / smoke 28 / UI 129 / E2E 72，全绿。  
产物 `dist/nvr-0.9.17_x86.fpk`。

## 10.4 预期

装上 0.9.17 后：

- **正式录制应当直接开始**（不用换子码流、不用改任何设置）——  
  之前所有"换个角度再试"的建议都可以无视，根因不在摄像头侧
- 再点一次「诊断」应显示"用正式录制目录试录成功"，且状态栏几秒内  
  从 0/5 变成 5/5 在录
- 若仍有异常，诊断弹窗里的报错现在就是正式录制的报错，发我即可

## 十一、0.9.18：结案后的产品化打磨（第六轮反馈）

0.9.17 真机确认"可以录制全时视频了"。本轮五项需求全是产品形态，不再有排障，  
但每一条背后仍有一个"为什么必须这么做"：

### 11.1 端口默认 18080（安装时可选）

- 向导 `wizard/install` 本来就有 `wizard_port` 字段，只是默认值 18081。  
  改默认值要动**五处**：manifest `service_port`（fnOS 桌面入口读它）、  
  `runtime.DEFAULTS`、`runtime.clamp_port/apply_port/read_port` 的缺省参数、  
  `cmd/main` 的 `TRIM_SERVICE_PORT:-18080` 兜底、`cmd/install_callback` 的  
  向导缺省。漏任何一处就会出现"装完图标指向的端口和实际监听不一致"。
- 已装用户不受影响：`cmd/main` 优先读 `config.json` 里的 `service_port`，  
  `merge_defaults` 保留旧值。

### 11.2 录像目录：默认锁定 + 保存前真实探测

- 默认值 `/vol2/1000/NVRL`（用户环境的大容量数据卷）。
- 界面从"永远可编辑的输入框"改为"只读展示 + 修改按钮"，编辑框默认隐藏。  
  理由：这是全应用唯一一个"改错即丢录像"的配置，不值得快捷修改。
- 新增 `POST /api/dir/check`：`runtime.probe_record_dir()` 三步各自给结论——  
  存在性（可自动创建）、是目录、**真实写探针**（落临时文件再删）。  
  只用 `os.access` 判断不了 ACL、只读挂载、磁盘满，这些都是 access 通过  
  而写入失败的实例；必须真写一次。
- `runtime.coerce` 对 `record_dir` 增加形态校验（绝对路径、非系统目录），  
  相对路径一律回落默认值——防止手改配置把录制器带进每 5 秒报错的死循环。

### 11.3 每路缩时总开关 + 折叠菜单 + 保留周期拆分

- 每路新增 `condense`（布尔）与 `clip_speed`，`norm_cameras` 透传、  
  `link._new_items` 对 `condense=False` 的路直接不入队（全时照录）。
- UI 把事件判定、输出压缩、检测忽略区、缩时保留天数全部收进可折叠的  
  「缩时压制」菜单；总开关关闭时参数区置灰。全时保留天数留在录制区。
- 字符串 `"false"`/`"off"` 也要认：前端推字符串是常态，白名单判断写在  
  服务端而不是信前端。

### 11.4 倍速输出

- `condense` 链路的导出在**切段时**按倍速重编码（`setpts=PTS/N` + fps 封顶  
  15），而不是切完再整体变速——后者要对成品再读再写一遍，机械盘 I/O 翻倍。
- 倍速 >1 时从流复制退到 `libx264 veryfast`：`-c copy` 连时间戳都不碰，  
  倍速与无损复制在原理上不可兼得，这是物理约束不是实现取舍。
- 倍数白名单 1/2/4/8：放任意整数进来（3、7）只会让人误以为"倍数越怪省得越多"。
- 已存在的 `ffmpeg_cmd.speed_cmd` 是 Windows 版遗留，本次没有复用它——  
  它不处理 `-ss/-to` 窗口，而切段永远带窗口。

### 11.5 缩时时间轴按源时刻定位

根因：缩时片段的时间轴位置用的是"播放坐标"（从 0 累加的播放秒数），  
热力条/列表把它当钟点画，于是所有片段都挤在 00:00 附近；点 14:00 的格子  
按同一坐标过滤，自然"提示无视频"。  
修复：`realSegs` 给每个缩时段补 `src/srcEnd`（取自分片文件名 HH-MM-SS，  
与全时模式同一约定），`segA()/segB()` 统一取"展示坐标"：

- 热力条、时段过滤、片段列表、缩略图、概览的覆盖统计全部改走 segA/segB
- 播放器进度条仍按播放坐标推进（视频只有十几秒），但时间显示与 OSD  
  换算成源时刻，标题同时给出"覆盖 14:00–14:05 · 播放 00:12"

### 11.6 测试与产物

- 新增 `test/test_v0918.py`（21 项）：端口五处一致、目录默认值与探测  
  （创建/写探针/文件占用拒绝）、norm_cameras 开关与倍速白名单、  
  link 跳过 condense=False、倍速命令形态（setpts + 无 copy + 窗口仍在）。
- `settings.test.js` 更新：折叠菜单默认收起成为被测行为；新增目录锁定、  
  推送字段、时间轴源时刻断言。`verify_pkg.py` 版本与包内断言同步。
- 注意一个跨平台坑：`os.path.normpath` 在 Windows 上把 `/vol2/x` 变成  
  `\vol2\x`，"以 / 开头"的校验必须做纯字符串判断，否则开发机测试全挂。
- 回归：结构 73 / 后端 192 / 设置页 145 全绿；`verify_pkg` 77 项通过。  
  产物 `dist/nvr-0.9.18_x86.fpk`。


### 11.7 真机验证清单

1. 安装向导第一屏端口默认 18080，可改；装完桌面图标指向的端口即实际端口
2. 设置页保存目录显示 `/vol2/1000/NVRL` 且无输入框；点「修改」展开，  
   填一个不存在的路径保存 → 自动创建并提示"可写"；填一个无权限路径 → 保存被拒并给出原因
3. 任一路关闭「缩时压制」总开关 → 该路全时照录、缩时片段不再新增
4. 任一路设 2× 倍速 → 下一个缩时片段时长约为源片段的一半
5. 回放页切到缩时模式：热力条按真实钟点亮、点 14:00 格子能列出该时段片段、  
   播放器显示"覆盖 14:00 – 14:05 · 播放 00:12"

## 十二、0.9.19：安装向导从未存在 + 桌面图标端口错位（第七轮反馈）

用户反馈两条：装 0.9.18 时没有端口输入页；装完点开桌面图标报  
`http://192.0.2.6:18081/ ERR_CONNECTION_REFUSED`。卸载重装也没用——  
这不是升级跳向导的问题，是向导从来没出现过。

### 12.1 根因一：wizard 从未进包

`pack_fpk.py` 里有这样一行：

```
WITH_WIZARD = os.environ.get("NVR_PKG_WIZARD") == "1"
```

注释写「0.4.x（有 wizard）在真机上报应用数据包不完整」——于是 wizard 被关掉，  
**然后再也没人回头复核**。对照官方文档（developer.fnnas.com/docs/core-concepts/  
framework/）：包根 `wizard/{install,upgrade,uninstall,config}` 就是标准结构，  
社区大量 fpk 都带 wizard 正常安装。当年的「应用数据包不完整」真实原因是旧打包器  
自己的成员名缺陷（`./` 前缀那一版），wizard 只是恰好在那次实验里被一起测了。  
从 0.4.x 到 0.9.18，安装向导（端口、录制目录选择）从未真正出现过——  
**被否定掉的方案不会自己复活，除非有人专门复查它被否定的理由。**

### 12.2 根因二：包内桌面入口端口没跟着改

0.9.18 把默认端口 18081→18080 时改了五处，但漏了第六处：`app/ui/config`  
（打包到包根 `ui/config`，桌面图标读它）。结果：

- install_callback 无向导参数 → 兜底 `TRIM_SERVICE_PORT`（18080）→ setport  
  把 config.json 改成 18080，服务监听 18080
- 包内 ui/config 仍是 18081 → 图标指向 18081 → ERR_CONNECTION_REFUSED

两个值各自「合理」，合起来就是打不开。教训：**改默认值必须全量搜一遍 18081，  
而不是靠记忆列清单**——verify_pkg 现在断言包外层 ui/config 的端口。

### 12.3 修复内容

1. `pack_fpk.py`：`WITH_WIZARD` 默认开（`NVR_PKG_WIZARD=0` 才关），注释写清考证
2. `app/ui/config`：port 18080；`wizard/config`（应用内设置向导）同步
3. `cmd/install_callback` 端口决策改三路：
   - 向导给了端口 → setport（落配置 + 对齐图标 + 占用顺延）
   - 无向导但配置里已有端口（升级/覆盖重装）→ **pinport 只对齐图标，绝不动端口**  
     （升级瞬间旧进程可能还占着端口，setport 会顺延挪走 → 图标又错位）
   - 全新且无配置 → manifest 默认
4. `cmd/upgrade_callback`：升级整目录替换 app 后，把包内 ui/config 的端口  
   pinport 回用户现有端口；启动阶段 cmd/main 的 pinport 再兜一次
5. 守卫：verify_pkg 92 项（含 wizard/install JSON 解析 + 字段 + 默认值、  
   外层 ui/config 端口、install/upgrade_callback 的端口保护反断言）；  
   test_v0918.py 增 TestWizardPackaging 5 项。回归：结构 73 / 后端 197 /  
   设置页 145 全绿。

### 12.4 端口三处一致性的不变式

包内 ui/config 端口、向导默认端口、manifest service_port 三者必须相等，  
且任何脚本只在「向导端口」这一个入口改端口；升级路径只做对齐不做修改。  
verify_pkg 从包的真实字节上断言这条不变式。

## 十三、0.9.20：默认录制目录在权限模型下不可行（第八轮反馈）

0.9.19 的向导出现了（端口页正常），但用户接受默认目录 `/vol2/1000/NVRL` 后  
安装失败：`输出目录无法创建`。

### 13.1 为什么 /vol2/1000/NVRL 作为包默认值行不通

fnOS 的应用权限模型（config/privilege `run-as=package`，用户 nvr）：

- 只对 **config/resource 里 `data-share` 声明的共享目录**（`/vol1/@appshare/nvr-output`）  
  自动创建目录并授权应用用户读写，且文件管理 / SMB 可见
- `/vol2/1000` 是用户的个人存储空间：应用用户既无权 mkdir、运行时也无写权限。  
  即使安装期（可能 root）硬建出来，录制进程照样写不进

即：这个默认值在"能装上"和"能录进"两头都不成立，而且是每台机器布局相关的。  
用户 R2 提出"默认 /vol2/1000/NVRL"时暴露的是**需求**（大容量卷、SMB 可见、  
不想常改），不是**路径**——路径应该由满足该需求的机制去实现。

### 13.2 0.9.20 的方案

1. 包默认目录改回 `/vol1/@appshare/nvr-output`（自动授权、跨机器可用、SMB 可见）。  
   用户想用 NVRL：装完后在应用内「修改」里填 `/vol2/1000/NVRL`，保存时探测——  
   若用户在文件管理里建好了目录并授权，探测通过即生效；不通过会给出具体原因。
2. install_callback 目录决策改回退链：向导目录 → 配置已有目录（升级不覆盖）→  
   共享目录 → 数据目录内；每级用**真实写探针**（落文件再删）验证，mkdir 成功  
   不代表可写。回退时 DIR_WARN 写安装界面（TRIM_TEMP_LOGFILE）与 install.log——  
   静默降级比失败更危险（用户以为录到 A 实际录到 B）。
3. 向导帮助文本诚实说明：为什么默认是共享目录、想要别的目录怎么做、  
   回退会发生什么。
4. 守卫：test_v0918 R2 改断言 + 新增回退链守卫（忽略注释行——注释里的教训  
   记录是刻意的）；verify_pkg 96 项含 install_callback 的回退链反断言。

### 13.3 教训

默认值必须选"权限模型保证可行"的那一个；用户指定的路径做成**可配置目标**  
（探测+引导），不做包默认。回归：结构 73 / 后端 26 文件全绿 / 设置页 145 /  
包断言 96。产物 dist/nvr-0.9.20_x86.fpk。

## 十四、0.9.21：「执行脚本出错且原因未知」（第九轮反馈）

0.9.20 全新安装（全默认、无个性化修改）报「执行脚本出错且原因未知」。

### 14.1 根因：set -u 下的 unbound variable

0.9.19 重构端口决策时引入 `PORT_WIZARD`，但脚本末尾的安装日志行还在引用  
旧变量 `PORT_WANT`——它现在只在"无向导无配置"的兜底分支才定义。  
`set -u` 下 echo 未定义变量 → 脚本在**最后一行**崩溃 → 非零退出。  
讽刺的是：配置、目录、端口此刻都已正确落盘，脚本只差一条日志没写完，  
fnOS 却把整个安装判为失败。bash -n（语法检查）抓不到这种运行期错误——  
它属于"重构时旧引用没清干净"一类，只有真跑一遍才会现形。

### 14.2 定位手段：本地干跑

模拟 fnOS 环境（TRIM_APPDEST/TRIM_PKGVAR 指向临时目录 + 向导变量）在  
Git Bash 里 `bash -x cmd/install_callback`，十几步 trace 直接看到  
`line 181: PORT_WANT: unbound variable`。事后看，0.9.19/0.9.20 都缺这个  
干跑——它现在已是测试闸门的一部分。

### 14.3 修复与守卫

1. 日志行改 `wizard=${PORT_WIZARD:-}`（未绑定也给默认空串）
2. **新增 test/test_install_dry.py**：真的以子进程执行 install_callback，  
   三种场景各一遍——全新安装（向导默认值）、向导目录建不出（断言回退  
   共享目录且 stderr 留痕）、升级（断言端口 18099 与旧目录原样保留）。  
   反向验证：把 bug 注回去，3 项全 FAIL；还原后全过。
3. upgrade_callback 的 pinport 日志重定向提前 mkdir log 目录
4. 测试自身的坑：Git Bash 把 `/vol1/...` 映射成宿主物理路径，断言回退  
   目标只能比结尾段；config.json 里的路径要用正斜杠（fnOS 是 POSIX）
5. 回归：结构 73 / 后端（含 install_dry 3 项）全绿 / 设置页 145 /  
   包断言 96。产物 dist/nvr-0.9.21_x86.fpk。

### 14.4 教训

**shell 脚本的"能装上"必须靠执行验证，不能靠语法检查。** 0.9.19 重构  
端口决策时改了赋值点却没搜全部引用点（和 0.9.18 漏改 ui/config 是同一  
类失误：改了"源头"没全量搜"使用处"）。全量搜索应该用工具（grep）而不是  
靠记忆——这条已经在 12.2 写过一次，这次换了个形态又犯。干跑测试闸门  
是结构性防御：以后重构脚本，跑不过 install_dry 就出不了包。

## 十五、0.9.22：判读从「码流曲线」升级为「像素差」（第十轮反馈）

用户拿另一个 NVR 的判读条件菜单（画面类型 / 灵敏度 / 像素差门槛 / 最短事件 /  
模糊半径 / 分析抽样帧 / 日夜采样间隔 / 加速播放）来问：能不能改成像素差，  
把静止画面真正剔掉。答案是能，而且本来就该这么做 —— 参数体系早就按像素判据  
写好了，只是当年为了省 CPU 退化成了码流曲线。

### 15.1 为什么码流曲线不够

码流曲线看的是"这段有多少字节"。它便宜（不过容器、不解码），但有两个硬伤：

1. **被码率策略骗**：静止画面遇到 I 帧照样一大包，于是"没有事"的时段也会  
   被切出来；反过来，码率恒定的机位（CBR）真有事时字节几乎不动，直接漏掉。
2. **区间太粗**：一个 30 秒的事件，前后各扩 3 秒再加上阈值抖动，常常切出  
   2 分钟；一天下来"缩时"跟全时差不多长，缩时就没意义了。

像素差看的是"这块画面有多少像素真的变了"。静止就是静止，一切以画面为准。

### 15.2 实现：差分全部交给 ffmpeg，Python 只看一个数

fnOS 应用包只带标准库，没有 numpy。320×180 灰度 × 2 fps × 5 分钟  
= 1700 万次像素比较，纯 Python 要十几秒且吃满一个核 —— 这条路不通。  
所以整条判据链都用 ffmpeg 滤镜完成：

```
fps 抽样 → scale=320:180 → format=gray → drawbox 涂黑忽略区
→ boxblur 压噪声 → tblend 差分 → lut 按像素差门槛二值化
→ signalstats 求 YAVG → metadata=print
```

YAVG 就是「255 × 超过门槛的像素占比」，Python 每帧只解析一个浮点数。  
门槛判定发生在**像素级**，不会被"一大片静止把一小块变化平均掉"稀释 ——  
这正是帧平均方案最大的毛病。

三个反直觉的点，都在代码注释里写清了：

- **先缩放再差分**：1080p 全分辨率上压缩噪声让每个像素都"在动"，  
  门槛怎么调都压不住；320×180 上一个人仍有 6×12 像素，判定够用。
- **忽略区用 drawbox 涂黑是对的**（与码流曲线必须用 crop 相反）：  
  两帧都涂黑后差分恒为 0，真正不参与判定；而字节曲线里涂黑反而制造事件。
- **零值必须参与分位计算**：见 15.4。

### 15.3 成本控制：预筛 + 只解候选区间

像素判读要解码，这是唯一的新成本。做法是先跑原来的码流曲线做**粗筛**  
（门槛下调、min_clip 降到 1 秒，宁可多报不漏），把候选区间前后各扩 10 秒，  
只解码这些区间。实测（ffmpeg 7.1，1080p 25fps）：

```
全段 120 秒像素判读        2.3 秒
单区间 40 秒（候选窗口）    0.8 秒
```

5 分钟分片、两三个候选窗口的话，NAS 上十几秒量级，和导出耗的是一个数量级。  
预筛一个候选都没有时，仍然全段扫一遍像素（避免"码率恒定机位"被整段判死），  
但抽样降到 1 帧/秒，把静态段的代价压一半。

### 15.4 实测抓到的两个真 bug

拿到 ffmpeg 7.1 后本地真跑，发现的问题都不是读代码能看出来的：

1. **阈值基线算错**：最初用"非零值的 20 分位"当基线，于是 5 分钟里只有  
   10 秒有人走过时，非零值全是活动帧，分位数就是活动强度本身，乘 2 倍后  
   门槛比活动还高 —— "明明有人在走，判出来整段静止"。含零后基线自然压到 0，  
   门槛退回绝对地板 5e-4（0.05% 像素）。这条已固化成回归测试。
2. **`drawbox` 的 eval**：合成测试视频时 drawbox 默认只在 init 阶段求值一次，  
   要逐帧动得用 overlay —— 这个是测试脚手架的问题，不是产品代码的。

实测结果（同一颗 ffmpeg）：

```
静止画面              → 静默，不产出片段
5~10 秒白块穿过        → 窗口 4.5~11.0（2fps 粒度 + 前后缓冲 0.5 秒）
同一段 + 忽略区覆盖   → 静默（涂黑后差分恒 0，忽略区真生效）
模糊 0 / 1 / 3        → 都能抓住 30×30 的目标
```

### 15.5 判读菜单照截图的预设重排

每路「判读条件」：

| 项      | 取值               | 落到哪                                          |
| ------ | ---------------- | -------------------------------------------- |
| 判读引擎   | 自动 / 像素差 / 码流曲线  | `analysis.engine`                            |
| 画面类型   | 自动识别 / 彩色 / 黑白红外 | `analysis.scene`（黑白额外放宽 25% 容噪）              |
| 灵敏度    | 低 / 标准 / 高       | `sensitivity` → `noise_mult` 1.6 / 2.0 / 3.0 |
| 像素差门槛  | 1~80（默认 24）      | `pixel_delta`（lut 表达式）                       |
| 最短事件   | 0.5~60 秒（默认 2）   | `min_event` + `min_clip`                     |
| 模糊半径   | 0~3（默认 1）        | `blur_mono/blur_color`（boxblur）              |
| 分析抽样帧  | 0.2~10 帧/秒（默认 2） | `fps`（fps 滤镜）                                |
| 日夜采样间隔 | 1~30 秒（默认 5）     | `scene_probe_sec`                            |
| 静默合并   | 0~120 秒（默认 5）    | `merge_gap`                                  |
| 加速播放   | 1/2/4/8×         | `clip_speed`（导出，在缩时压制区）                      |

**这些参数以前是纯摆设**：滑杆只改 localStorage，服务端从没收到过，  
判定一直用 `params.ANALYSIS_DEFAULTS`。这次每路参数随配置推送到  
`config.profiles[cid]`，由 `profile_from_judge()` 夹取 + `params.validate()`  
校验后写入，`link` 每处理一个分片现取最新档案 —— 改完下一个分片就生效，  
换浏览器/清缓存后回读的也是服务端那份。旧配置的"0~100 灵敏度百分比"  
按区间映射成档位，不会丢。

### 15.6 降级与自检

像素判读多了四个滤镜依赖（tblend/signalstats/lut/boxblur）。系统 ffmpeg 缺  
任何一个都不能让功能"看起来有、实际不工作"：

- `pixdiff.available()` 一次探测（`ffmpeg -filters` 列全，缓存），缺就返回人话
- `condense.analyze()` 在滤镜缺失、或这次跑不出曲线时**整段**回落码流曲线，  
  并在 `engine_note` 里写明原因；`link` 每次降级只记一次日志（不刷屏）
- 诊断页新增「像素判读 自检」：用 lavfi `testsrc` 合成片源跑一遍判据链，  
  不需要用户的录像、不需要编码器，直接给"可用（N 帧 · 峰值 x%）"或  
  "不可用 · 缺 tblend"

### 15.7 测试与产物

- 新增 `test/test_pixdiff.py` 28 项：滤镜串形状（含忽略区/模糊/门槛落点）、  
  metadata 解析（含时间偏移）、降级三条路径、预筛区间与 1fps 全段扫描、  
  阈值语义（稀疏活动回归、零基线取地板、黑白容噪）、参数夹取与校验。
- 其中 4 项是**真实 ffmpeg 集成测试**：本机有 ffmpeg（`NVR_FFMPEG` 或 PATH）  
  就现生成静止/移动两段视频真跑，验证静止静默、移动切出、忽略区屏蔽；  
  没有就 skip —— 构建机不必装 ffmpeg，但装了就能拦住"字符串对、行为错"。
- 回归：结构 73 / 后端 14 个测试文件全绿（含 pixdiff 28）/ 设置页 161 /  
  包断言 114。产物 `dist/nvr-0.9.22_x86.fpk`（197 KB）。

### 15.8 真机验证清单

1. 诊断页点「像素判读 自检」→ 应显示「✓ 可用（5 帧 · 峰值 x%）」
2. 设置 → 任一路 → 判读条件：引擎默认「自动」，展开可切「码流曲线」
3. 把 像素差门槛 调到 40、灵敏度「低」→ 保存 → 日志里看该路下一个分片的  
   缩时片段明显变短（静止时段被剔除）
4. 画一块忽略区盖住树影/马路 → 该区域的晃动不再触发片段
5. 对比：切「码流曲线」后同样素材的片段会变长、变碎（说明两套引擎真的不同）

## 十六、0.9.23：回放页改版 + 导出当前文件 + 多日筛选修复（第十一轮反馈）

设计先行：`nvr-slim/原型/回放页改版设计.html`（v1.1，4 帧 + 标注），用户确认后开发。

### 16.1 多日筛选错乱（用户实报，根因确定性）

现象：多日数据下回放页选 A 天、加载成 B 天。根因不在数据、在**排序分裂**：

- `recalcDayList()`：`Object.keys(days).sort().reverse()` —— 新→旧、全量
- `renderDates()`：`Object.keys(camDays).sort().slice(-7)` —— 旧→新、末 7 天
- 点日期 chip 写入 `dayIdx=i`（renderDates 的升序下标），`loadDay()` 先调 `recalcDayList()`  
  把列表翻成新→旧，再 `curDay() = dayList[dayIdx]` —— 取到另一天。  
  **N 天里只有正中间那天碰巧正确**；只有 1 天数据时两套顺序相同，永远看不出。
- 修完后 `renderDates()` 又把列表翻回去，于是"高亮的日子 ≠ 加载的日子"。

修复：排序只保留一处（`recalcDayList`，旧→新全量，`renderDates` 只消费不再排）；  
新增 `state.daySel`（日期字符串），选中跟字符串走，跨天新增文件/切模式不漂移。  
回归测试：3 天数据 + 选中中间天，断言 dayList 升序、dayIdx 命中、默认选最新一天。

### 16.2 播放器改版

- **双层时间轴**：上行钟点（起始·当前·结束，缩时按源时刻，当前值固定居中主色高亮），  
  下行本文件播放进度（00:00·当前·总长，`mmss`）。用户反馈否掉了"当前值跟随进度点"。
- **五键**：倍速（1/2/4/8×，`SPEEDS` 数组 + `playbackRate` + 画布段 `dt*8*speed`，  
  `loadedmetadata` 时重套倍速防换 src 重置）/ 上一段 / 暂停播放 / 下一段 / 全屏。  
  ±10s 按钮移除（`seekBy` 一并删除）。上一段/下一段图标此前画反（生产代码也反），已修正。
- **真全屏**：复用 `toggleFull()`（Fullscreen API + `orientation.lock("landscape")` +  
  CSS 旋转三级降级，直播页早已在用）；新增 `fshide`：全屏且播放中时 `.p-top/.p-ctrl`  
  整体淡出（`pointer-events:none`），暂停/轻点画面即浮现 —— 由 `syncPlayer()` 统一驱动。
- **段号去重 + 筛选计数**：pSub 去掉"第 x/y 段"（用户指名去第二处）；  
  新增 `playOrder()`：选了时段筛选（`hourBlock>=0`）时返回 `visibleSegs()` 的索引表，  
  标题计数、上一段/下一段（`jumpSeg`）、播完自动接续（`finishSegAndNext`、ended、loop）  
  全部走这一份顺序 —— 筛选 10:00–12:00 后播放不再跳出时段。
- **进度条触控**：`.p-bar` 高 26→34px + `touch-action:none`，knob 13→16px，  
  拖动时 `drag` 类加光圈反馈；`pointercancel` 也清状态。

### 16.3 导出当前文件

- 服务端：`/api/record/file` 加 `dl=1` 参数 → `Content-Disposition: attachment;
  filename*=UTF-8''<cam>_<day>_<HHMMSS>.mp4`（仅 200 响应加头，Range/目录试探全复用）。
- 前端：右上「⋯」（原占位）→ 弹出菜单：文件名预览 +「导出当前文件」（`<a download>` 触发）  
  +「复制下载链接」（clipboard API，失败退化为 toast 链接本体）。关播放器时收起菜单。

### 16.4 测试与产物

- UI 178 项（新增 17 项：双层时间轴/五键/倍速档位/fshide/触控/图标方向/导出/多日筛选/playOrder/段号计数）；  
  Python 228 项 OK（5 项真实 ffmpeg 集成按环境跳过）；validate.py 73 项；verify_pkg 129 项（版本 0.9.23、  
  changelog ≥10 段、UI/服务端新断言、MUST_NOT_HAVE 里 ±10s 按钮）。
- 产物：`dist/nvr-0.9.23_x86.fpk`（201.9 KB，md5 见 manifest.checksum）。
- 真机清单：装包 → 多日数据逐天点日期条核对列表与日期一致 → 播放页五键逐个点 →  
  倍速 2×/8× 听语速 → 全屏横屏播放中确认无控制、轻点浮现 → 拖进度条（ slow drag）→  
  「⋯」导出文件并核对文件名与可播性 → 筛选 10:00–12:00 后看「第 x/y 段」。


## 十七、0.9.24：设置页三区整理 + 诊断存储账本 + 直播状态与连播（第十二轮反馈）

用户四批需求一次交付：设置页信息架构重排、诊断区补存储占用（非实时）、  
直播首页单双列与连续播放、点快照进单路全屏横屏。本轮先出 9 帧设计稿  
（`原型/设置页整理设计.html` v1.2）多轮确认后才动代码，"设计先行"跑通了一遍。

### 17.1 设置页三区

- **分区**：`renderCamTabs()` 固定输出 `zone gen`（折叠，一行 `genSummary()` 摘要  
  +「修改」）→ `zone cam`（永远展开，列表带 `缩时 1× · 10 天` 胶囊 / `已停用`）  
  → `zone diag`（折叠，`diagSummary()` 显示 `x/y 路在录 · 剩余 · 异常数`）。  
  摄像头固定在中间，不被另外两块的展开状态推着上下跳。
- **四段手风琴**（`camAcc()`，一次只展开一段，`SET.acc` 单值驱动）：  
  ① 名称与码流 ② 保留与分片 ③ 缩时压制 ④ 检测忽略区。  
  收起不等于失明：折叠段标题右侧始终显示该段当前值  
  （`全时 3 天 · 5 分钟` / `已开启 · 1× · 10 天` / `已选 N 格`）。
- **缩时压制三层**：总闸（`master` 独立条，非普通开关行）→ 影响预估四格  
  （`impact`：倍速 / 段每天 / GB每天 / 保留天数）→ 常用 3 项（倍速、缩时保留、灵敏度）
  - `进阶判读参数`（8 项折叠，偏离默认标 `已调 N 项`）+ `输出画质`（折叠）。
- **防误操作**：底部动作栏常驻 `dirtyCount()`（递归 diff 草稿 vs 已存）→  
  `● 已改 N 项未保存`；列表行的行内删除按钮移除，删除收进编辑页底部  
  两段确认（`SET.armed`，`再点一次确认删除`）；旧 `SET.tlOpen` 折叠菜单下线。

### 17.2 诊断区的存储账本（非实时）

- **后端**（nvr_slim.py）：`storage_usage()` 只按「摄像头/日期」两级目录聚合  
  文件大小元数据（4 路 × 30 天 ≈ 120 目录，不读文件内容）；  
  `_sum_cam_bytes()` 按 `(日期, 文件名)` **去重求和** —— 旧 id 目录与新名称目录  
  升级后会同时存在，不去重占用会凭空翻倍。结果写 `storage-usage.json`，  
  30 分钟 TTL（`STORAGE_TTL`），缓存命中不碰盘；`?force=1` / POST refresh 强制重算。
- **前端**：只有 `SET.diagOpen` 且缓存过期才发请求（`loadStorageUsage(false)`），  
  直播/回放/其它页一律不触发；失败后**不再自动重试**（否则渲染→请求→失败→  
  再渲染打成一个环，页面会一直闪），重试交给「立即重算」按钮（错误态也真的给按钮，  
  不是只有文字）。
- **展示口径**：总条 = 全时(深蓝)+缩时(浅蓝) 按已用体积归一，**没有灰色"其余可用"段**  
  （容量是共享的，每路的"其余可用"没有意义）；每路条按该路自身总量归满格，  
  行按总量降序；标注 `非实时 · 统计于 HH:MM`。

### 17.3 直播：单双列 / 连续播放 / 单路全屏 / 四角状态

- **单双列**：`btnLiveCols` 齿轮旁按钮 + `colPop` 弹层（与刷新率弹层互斥）；  
  `setLiveCols()` 写 `localStorage["nvr.live.cols"]`，刷新保持；一路时自动藏按钮。
- **连续播放是按钮不是跳转**：`toggleWall()` 把当前快照墙**原地**换成 MJPEG 流  
  （`mountLiveStream(d, c, true)`），不切页、不重建列表、滚动位置不丢；  
  `pollLive()` 与 `tickSnaps()` 在 `state.wallStream` 时跳过重建与快照续图  
  （对 `streaming && !fallback` 的卡），单路失败只影响那一格、如实回落快照。  
  按钮变「回到快照」，`wallStat` 显示 `实时 · 已播 N 分钟`。
- **单路全屏横屏**：点快照 → `openLiveFull(idx)`：进入实时层 + `lrFs` 形态 +  
  `enterFull(null,{noExit:true})`（全屏 API + `orientation.lock("landscape")`）。  
  可暂停（居中播放键）、可退出；`syncLiveChrome()` 单点驱动控制条，播放中 2.6s  
  自动收起，轻点/暂停浮现；`closeLiveRoom()` 主动摘 img.src 断 MJPEG（只隐藏  
  class 连接会挂着），退出全屏（`exitFull`/`fullscreenchange`）都会兜底回快照墙。
- **四角状态层**（首页卡片 / 连续播放 / 单路全屏共用一套 CSS）：  
  左上 `lv-rec`（`recTagOf()`：REC 红 / 仅预览 蓝 / 已停用 灰 / 直播中 / 未配置）、  
  右上 `lv-osd` 时间戳（没成功过显示 `--:--:--`，不拿当前钟点冒充）、  
  左下 `lv-name` 名称+通道、右下 `snap-ago` **只在异常时**出现  
  （`快照 · 10s` 琥珀 / `无画面 · 重试` 红，点了只重连这一格）。  
  旧右上角倒计时圆点徽标（`.snap-cd`）整体下线。

### 17.4 测试与产物

- UI **246 项**全绿（重写缩时/诊断/布局三段断言适配手风琴；新增 22~24 段：  
  存储卡口径与缓存策略、列数落盘、连续播放不重建、四角状态矩阵、  
  单路全屏状态归位、防误操作三项）。Python **242 项** OK（新增 test_storage.py  
  14 项：目录口径 / 新旧目录去重 / TTL 缓存与 force / 缓存写坏与目录不可写 /  
  payload 字段）。validate 73 项；verify_pkg **197 项**（版本 0.9.24、  
  changelog ≥11 段、UI/服务端 0.9.24 断言、MUST_NOT_HAVE 里 resbar/tlOpen/snap-cd）。
- 顺手清理：`updateResBar()` → `syncDiagSum()`（资源栏元素已随信息栏搬走，  
  现在只就地刷新诊断折叠摘要 `#diagSum`，不再整页重渲染打断编辑）。
- 排障备忘：headless Chrome（Windows）有最小窗口宽度钳制（~500px），  
  `--window-size=430` 会被钳成 500 而截图仍按请求尺寸裁切，造成"右侧按钮被裁"  
  的假象；用外层 1400px 窗口 + 430px iframe 模拟视口即可核对真实布局。
- 产物：`dist/nvr-0.9.24_x86.fpk`（214.6 KB，md5 见 manifest.checksum）。
- 真机清单：装包 → 设置页三区折叠/展开 → 诊断区核对存储数字与磁盘实际占用  
  （du 对账）→ 点「立即重算」→ 直播页切单/双列并刷新核对保持 →  
  点「连续播放」确认不切页、滚动位置不丢 → 点快照进单路全屏横屏、  
  暂停/轻点/退出 → 停一路摄像头核对右下角回落提示 → 改参数不保存看底部计数 →  
  编辑页底部删除点两次确认。

## 十八、0.9.25：跨设备 RTSP 地址被抹掉的根因与修复

用户真机反馈：换一个地址（另一台设备/浏览器）访问后再操作，两路摄像头  
名字和忽略区都在，但提示「未配置 RTSP 地址」。追出来这不是显示问题，  
是一次**真实的数据覆盖事故**：

### 18.1 根因链

1. 明文 RTSP 地址（含 `user:pass@`）只存在「配置它的那台浏览器」的  
   localStorage；服务端回给浏览器的永远是脱敏串 `rtsp://***@ip`  
   （`redact_cams`，防止密码出服务端——这个决定本身是对的）。
2. `applyServerCams()` 按 id 合并：本机有明文就保留、其余以服务端为准。  
   换设备/清缓存后本机 `prev` 是空的 → `CAMERAS[].main = ""`。
3. 任何一次设置推送（`pushCfg`，改快照间隔/开关/参数都会触发）把  
   `url: ""` 发给 `POST /api/config`；服务端 `norm_cameras` 后**整表替换**  
   `cfg["cameras"]` → NAS 上的真地址被空值覆盖，录制随之断源。  
   名字/掩码/保留天数活下来，是因为它们来自服务端回传，不是本机。

### 18.2 修复（服务端为准 + 显式意图）

- **服务端 `merge_cam_urls(cams, old_cams)`**（nvr_slim.py，POST /api/config  
  在整表替换前调用）：推回的 url 为空**或含 `***@`** → 按 id 回落 NAS 旧值；  
  只有两种情况真改地址——推来新明文（直接采用），或带 `url_clear/sub_clear`  
  显式清空标记。新明文优先于 clear 标记（防残留标记误清新地址）。
- **前端**：
  - `applyServerCams` 给每路记 `hasUrl/hasSub`（脱敏串非空这个事实可信）；  
    |  |  |  |  
    | --- | --- | --- |
  - 所有「已配置」判定改用 `p.main || c.hasUrl`（四角状态 recTagOf、  
    直播卡片 ready、在线计数、连续播放计数、宫格离线角标、REC 指示）；
  - 编辑页/只读页如实显示「已保存在 NAS · 本机不显示明文」，  
    输入框 placeholder 提示「留空 = 不修改」；
  - `saveDraft` 只在「本地曾有明文且被清空」时打 `urlClear/subClear`，  
    `pushCfg` 把标记带给服务端——本机不知道 ≠ 想清掉。
- **注意**：已经被抹掉的地址无法找回（日志里也全是脱敏串），  
  需要重新填一次；填完之后这个 bug 不会再吃掉它。

### 18.3 验证

- 单测 `test/test_url_keep.py` 10 项：空/脱敏回落、新明文优先、显式清空、  
  标记用完即摘不进配置、新装无旧值、往返两次零 diff（不多写盘）。
- **端到端 `test/url_keep_e2e.sh` 6 项**（真实起服务 + HTTP）：  
  设备 A 配地址 → GET 回传脱敏非空 → **设备 B 推空地址 NAS 原样保留** →  
  填新明文直接采用 → url_clear 真清。顺带固定了两个环境坑：  
  Git Bash 的 /tmp 路径传给 Windows python 要 `cygpath -m` 转混合形式  
  （环境变量不做 MSYS 转换）；POST 必须带 `X-NVR-Request: 1` 护栏头。
- UI 255 项全绿（新增 25 段 9 项）；Python 252 项 OK；validate 73 项；  
  verify_pkg 208 项。产物 `dist/nvr-0.9.25_x86.fpk`（215.9 KB）。

## 十九、0.9.26：直播页状态点与单路全屏适配（第十三轮反馈）

四条界面细节一次交付，全部围绕一个原则：**悬浮层不能比画面还抢眼**。

### 19.1 快照卡：左上徽标下线，右下改 HomeKit 状态点

- **左上的录制态徽标（REC/仅预览/直播中）整体删除**——摄像头画面自带的  
  时间戳/厂标多在左上角，徽标正好压在它上面。已停用/未配置仍由 nophoto  
  覆盖层如实说明（「已停用 · 去设置里启用」「未配置 RTSP 地址」），  
  诊断区每路录制状态也还在。`recTagOf()` 与 `.lv-rec` 一并移除。
- **右下角改「状态点 + 新鲜度」**：胶囊内一颗 7px 圆点，  
  绿 `#30d158`（正常）/ 红 `#ff453a`（异常：离线、拉图失败、快照过期、  
  实时流回落），都带 `box-shadow` 光晕 + 2.4s 呼吸动画（参考 HomeKit）。  
  文字仍显示「N 秒前 / 离线 / 无画面 · 重试」。琥珀色 stale 底色下线，  
  异常一律走红点，语义只有两态。
- 单列/双列共用同一套 livecard 结构，自动一致。

### 19.2 连续播放：倒数没有意义，只留一颗裸点

`.livecard.streaming .snap-ago` 缩成透明胶囊、文字隐藏，只显示状态点。  
关键修正：连续播放中快照已停更，`age` 必然越涨越大——不能按快照口径判异常，  
改为 `live = streaming && !fallback`，实时流活着（没回落）就是绿点；  
只有回落后才按快照口径判红。

### 19.3 单路全屏：画面超出屏幕的根因与修复

- **根因**：`.lr-tile` 锁死 `aspect-ratio:16/9`。全屏横屏时手机屏约 2.16:1，  
  瓦片高度 = 宽度 ÷ 1.78 > 视口高度 → 底部超出屏幕；`object-fit:cover`  
  又把超出的部分裁掉，用户看到的就是"画面放大溢出"。
- **修复**（与回放页同口径）：fs 形态下 `.lr-tile{aspect-ratio:auto;height:100%}`
  - `.lr-body{grid-template-rows:100%}`，瓦片永远精确吃满视口；  
    `.snap-img{object-fit:contain}` 完整呈现，超出部分留黑边。  
    控制保持只有「继续直播/暂停」，无上下段按钮（本就是直播）。
- 验证：headless 注入脚本量几何——瓦片/容器/视口三者一致（906×272），  
  `fit=contain`、`aspect=auto` 均生效，无溢出。

### 19.4 测试与产物

- UI 259 项全绿（四角段重写为状态点断言 9 项；连续播放态 4 项；  
  单路全屏 contain/不超界 2 项）；Python 252 项 OK；validate 73 项；  
  verify_pkg 217 项（MUST_NOT_HAVE 加 recTagOf/lv-rec/lv-badge/.snap-ago.stale）。
- 产物 `dist/nvr-0.9.26_x86.fpk`（216.4 KB）。
- 探针备忘：给 iframe 里的直播页强制注类做视觉验证时，注意 `pollLive`  
  每 10 秒会 `renderLive()` 重建卡片、清掉注入的类——注入后要么立刻截图，  
  要么把 `state.wallStream` 一并置真让它走 `updateSnapLabels()` 分支。

## 二十、0.9.27：实时时刻跟钟走/断流冻结 + 全屏信息去重（第十四轮反馈）

两条需求：连续播放要显示当前时刻、断流停在中断时刻（全屏同理）；  
全屏页信息有重复、排版不合理。

### 20.1 时刻状态机（syncLiveStamps）

- `snapSt` 每路增加 `live / frozen` 两个字段：`mountLiveStream` 的  
  `img.onload` 置 `live=true, frozen=0`；`img.onerror`（MJPEG 连接断开）  
  置 `live=false` 并记 `frozen=Date.now()`。
- 新增 `syncLiveStamps()`，由两处 tick 驱动（快照墙 0.4s 一拍、  
  播放层秒级一拍）：
  - 流活着（`streaming && !fallback`）→ 右上角时刻 = 当前钟，逐秒走；
  - 断流且快照还没续上 → 冻结在 `snapTime(st.frozen)`，即"停在中断时刻"；
  - 回落快照成功后 → 交给快照回调（显示快照自身时间）；
  - 暂停态不碰 —— 画面冻结在最后一帧，时刻跟着停。
- 修复了顺带的旧毛病：播放层 `[data-tf]` 以前每帧无条件写 `clockStr()`，  
  断流/暂停也继续走，属于"假实时"。
- 全屏（单路横屏）共用同一机制，瓦片右上角 `.tf` 就是时刻位——  
  "切到全屏时也带上这个显示"。

### 20.2 全屏页信息去重

改之前一类信息出现两次：名字（顶部条 + 画面左下 `.tn`）、退出全屏  
（顶栏 + 底栏）、时刻（画面右上 `.tf` + 底栏 `#lrClock`）、  
状态行里名字再来一遍。fs 形态下统一裁掉重复侧：

- 名字留顶部条：`.liveroom.fs .lr-tile .tn{display:none}`；
- 退出留顶栏（常驻、chrome 收起也可见）：`#lrExitBot` 隐藏；
- 时刻留画面右上（`.tf`，顶栏下方 top:64px，加大到 12.5px 带底色胶囊），  
  底栏 `#lrClock` 隐藏——chrome 收起后时刻仍然可见；
- 顶部状态行抽出 `syncLiveSub()`：全屏时去掉"名字 · CH"段（标题已有），  
  |  |  |  |  
  | --- | --- | --- |  
  只留 `直播中 | 子码流实时 | ● REC 全时`；进出全屏都刷新  
  （`openLiveFull` 里补了一次调用，避免按非全屏形态渲染完再改类不重刷）。

去重后底栏只剩「暂停/播放」按钮 + 轻点提示；顶栏 = 名字 + 状态行 + 退出。

### 20.3 验证

- UI 271 项全绿（新增第 26 段 12 项：live/frozen 字段、断流记时、  
  同步函数、两处 tick 接线、三处去重 CSS、syncLiveSub 裁剪与刷新）。
- dump-dom 实测：3 路活卡时刻 `20:25:39`（当前钟）、断流卡冻结  
  `20:24:34`（恰为 65s 前）；fs 下 `.tn`/`#lrExitBot`/`#lrClock`  
  display 均 none、`.tf` 显示当前钟位于 top=64/right≈890、  
  |  |  |  |  
  | --- | --- | --- |  
  状态行 `直播中|子码流实时|● REC 全时`。
- Python 252 项 OK、validate 73 项、verify_pkg 224 项。  
  产物 `dist/nvr-0.9.27_x86.fpk`（217.4 KB）。
- 真机重点：连续播放时右上角秒针应逐秒走；拔网线/断摄像头后应  
  停在断流时刻并转红点；进全屏后同一时刻显示仍在（右上角、更大）。

## 二十一、0.9.28：单路全屏横屏信息显示对齐快照卡（第十五轮反馈）

上一轮把全屏信息"去重"后，布局仍是"顶栏 + 底栏"式（名字/状态行在顶栏，  
退出在顶栏、时刻在顶栏下方）。用户要求全屏横屏时直接参照快照卡的  
四角布局 —— 两套界面本来就该长一个样。

### 21.1 fs 形态改为快照卡同款四角

- **左上**：留空（摄像头画面自带的 OSD 不被遮挡）——与卡片一致；
- **右上**：时刻胶囊（跟钟走/断流冻结，0.9.27 的机制直接复用），  
  与退出按钮**同一横排**：退出是 38px 圆钮贴最右，时刻胶囊在其左侧 16px；
- **左下**：名称 + 通道（`.tn` 恢复显示并带上 `CH`，样式对齐卡片的  
  `lv-name`：全屏下 16px 边距、12.5px 字号）；
- **右下**：状态点直接复用快照卡的 `.snap-ago` 全套视觉  
  （绿 `#30d158` / 红 `#ff453a` + 光晕 + 2.4s 呼吸），全屏下加大一档；  
  出图前隐藏（nophoto 覆盖层负责说明原因）；异常时显示文字  
  （离线 / 未拿到画面 / 快照 · Ns / 无画面 · 重试），  
  「重试」胶囊可点且 `stopPropagation`，只重连这一格、不触发暂停/控制条。
- **顶栏**撤掉名字与状态行（`.lr-title{display:none}`），只剩那颗圆退出钮；  
  状态点口径与卡片一致，由 `syncLiveStamps()` 统一驱动  
  （room 打开时 tickSnaps 不跑，两个 tick 都经过它，不会漏驱动）。

### 21.2 排障记录

- `lr-title` 原本靠 `flex:1` 把退出按钮推到右侧，`display:none` 后  
  退出滑到左上角 —— 补 `#lrExitTop{margin-left:auto}`。
- 探针注入脚本把量测语句插到 `}catch` 之后，`out` 不在作用域静默报错、  
  title 停在页面默认值 —— 注入探针后若 title 没变成 P28*，  
  先怀疑脚本没跑而不是样式没生效。
- headless 下 room 高 272 是应用外框高度（vh 330），全屏 API 下铺满屏幕，  
  不是溢出回归；关键校验是 room=body=tile 三者矩形一致。

### 21.3 验证

- UI 275 项全绿（26 段改写为四角布局断言：左下名字+通道、顶栏只留退出、  
  时刻与退出同行、右下状态点复用与加大、出图前隐藏、口径一致、  
  异常胶囊 stopPropagation 重连）；Python 252 项 OK；validate 73 项；  
  verify_pkg 219 项。产物 `dist/nvr-0.9.28_x86.fpk`（218.3 KB）。
- dump-dom 实测：exitTop 右上 [852,10→890,48]、时刻 20:48:04 同排左邻 16px、  
  左下「车库入口 CH01」、右下绿点；断流态红点 + 快照 · 10s + 时刻冻结。

## 二十二、0.9.29：回放页时段对比——真实/全时/缩时放在同一把尺子上（第十六轮反馈）

### 22.1 需求

选中某个时段后，呈现三类时长的比较：真实时段长度、全时录像时段长度、  
缩时录像时段长度。

### 22.2 设计

- **位置与出现时机**：热力条下方新增 `.rangecmp` 对比条；点热力条选中  
  2 小时格后出现，取消选中（再点一次或「显示全部」）即隐藏。
- **同一把尺子**：三条时长全部以「真实时段长度」（2 小时 = 7200s）为  
  满刻度，条宽即时长占比：
  1. **真实时段**——中性色满条，基准线；
  2. **全时录像**——绿条 = 全时片段与窗口交集的并集时长，右侧文字  
     「1 时 58 分 · 覆盖 99%」，断录一眼可见；
  3. **缩时录像**——琥珀实条 = 看完该时段缩时片所需播放时长，浅琥珀  
     底衬 = 缩时的源覆盖范围（两根条叠放），文字「6 分 24 秒 ·  
     压缩 ≈18.6×」——时长条直接表达压缩效果。
- **口径**（`unionLen` + `renderRangeCmp`）：
  - 全时片段 s/e 本身是钟点坐标，裁剪到窗口后求**并集**（重叠只记一次）；
  - 缩时片段 src/srcEnd 是源时刻、e−s 是播放时长：覆盖按交集累加；  
    观看时长按「交集 ÷ 该片源跨度」折算——跨界片段不会整段算进去，  
    两个窗口也不会算重；
  - 数据各自取自当天的真实清单（realSegs 两遍，与当前回放模式无关），  
    缺全时显示「无录像」、缺缩时显示「无缩时片段」，绝不互凑。
- **接线**：`renderHeat()` 末尾调用 `renderRangeCmp()`，时段切换、  
  换天、换摄像头、换模式全部自动刷新。


### 22.3 排障记录

- 首轮功能测试 7 项失败，全部是**测试数据用错时段索引**：hourBlock 是  
  2 小时格号，`hourBlock=10` 是 20:00–22:00（a=72000），不是 10:00–12:00  
  （应为 block 5）。应用逻辑无错，探针（a,b=72000,79200）一眼定位。
- 缩时行两根条起初会上下堆叠——`.rctrack` 改 `position:relative`、  
  `.rcbar` 改 `position:absolute` 叠层后底衬/前景才各就各位。
- 覆盖率文字只在全时行出现；缩时行覆盖改用底衬条宽度断言（width:25.0%）。

### 22.4 验证

- UI 292 项全绿（新增第 27 段 17 项：unionLen 去重/裁剪/空表、三行齐全、  
  满刻度基准、覆盖 100%、看完 50 秒、压缩 144×、底衬/前景各一、  
  跨界折算 30 秒与 25% 底衬、缺数据显示「无」、取消选中隐藏、renderHeat 接线）；
- Python 252 项 OK（含 install 干跑）；validate 73 项；verify_pkg 223 项。
- 产物 `dist/nvr-0.9.29_x86.fpk`（219.6 KB，app.tgz md5 2bdeb2ef…）。

## 二十三、0.9.30：时段对比升级为三轴对齐时间轴（第十六轮反馈·续）

### 23.1 设计（先确认后开发，四项均按推荐方案）

- **A+B 合并**：默认全天 24h 三轴；点热力格后整条放大到该 2 小时，  
  「回到全天」一键还原（hourBlock 是唯一状态源，与热力条清除同口径）；
- **替代数字条**：0.9.29 的 `.rangecmp` 整体下线，时长/覆盖率/压缩倍率  
  并入每行右侧摘要；
- **缩时密度用颜色深浅**：d = 播放秒/源秒，三档透明度 0.34/0.62/1  
  （`tlDens`），同样覆盖 1 小时压得越短颜色越深；
- **色块可点跳播**：事件委托到 track，点击横坐标换算成时刻  
  （`tlPlay`），当前模式不同先切模式（按钮态同步）再在清单里定位播放。

### 23.2 关键实现：覆盖不能吃 realSegs 的桥接

`realSegs` 会把相邻文件首尾桥接（上一片终点=下一片起点），清单里缺  
文件造成的真断录会被抹平——直接用它画分布，断录永远是 0。时间轴改用  
「**文件起点序列 + 分片长度**」还原覆盖：某段没有文件，块在起点+segLen  
处停住，空档如实留在轴上（全时/缩时同法）。断录段数 = 合并块之间的  
真空档数（相邻缝 ≤1s 视为连续，`mergeBlocks`）。

### 23.3 排障记录

- 首轮测试 4 项失败：①桥接问题（见 23.2，探针显示两文件被并成一块  
  [36000,46800]）；②测试数据密度失真（20s/3600s 永远落在最疏档，  
  补一组 d=100/300 验证 0.62/1 档）；③桩的 querySelector 返回万能桩，  
  onclick 断言改查源码委托代码；④「无录像」行只在有缩时数据时才可见  
  （两者都缺整块隐藏，是设计行为）。

### 23.4 验证

- UI 307 项全绿（第 27 段重写为 32 项：mergeBlocks 合并/空档、  
  tlDens 三档、全天/放大双态、刻度、断录段数、跨界折算、密度上轴、  
  委托跳播、回全天同口径）；Python 252 项 OK；validate 73 项；  
  verify_pkg 231 项（旧 rangecmp 全套进 MUST_NOT_HAVE）。
- 产物 `dist/nvr-0.9.30_x86.fpk`（221.2 KB，app.tgz md5 88e0df84…）。

## 二十四、0.9.31：热力格搬进播放页（第十七轮反馈）

### 24.1 需求与布局

非全屏播放时，把回放页的热力格搬到画面下方；画面位置相应下移，  
合理利用竖屏下控制区上方的大片死黑区。

实现：`.p-heat` 插在 `.stage` 与 `.p-ctrl` 之间。视频本来就是  
`inset:0 + object-fit:contain` 铺满舞台居中，舞台被热力格压矮后  
画面自动下移——不需要碰视频样式，横屏全屏态（stage absolute 铺满）  
`.phone.land .p-heat{display:none}` 整块隐藏，fshide 淡出同步覆盖。

### 24.2 行为

- 数据吃 `state.activity`（当前模式真实清单），与回放页热力条同源；  
  深色配色（活动绿/空格灰），无活动整块隐藏；
- **正在播放的片段落在哪个 2 小时格就高亮哪格**，上/下一段自动跟随  
  （openPlayer 内 renderPlayerHeat()）；
- 点格跳播 `pHeatJump`：复用 tlPlay——当前模式该格有片段直接跳，  
  没有则自动切另一种模式再跳（full↔clip），两种都没有 toast  
  「该时段没有录像」；跳到「时段内首个片段起点」，不吃跨界片段。

### 24.3 排障记录

- 测试抓到真 bug：`firstT` 里 `segA(o.s)` 多取了一层 `.s`（o 本身就是  
  片段），返回恒 NaN——修正为 `segA(o)`。NaN 断言（hit===43200 失败）  
  比肉眼 review 更早拦住了它。
- 高亮断言首版用错 segIdx（0 是 10:00 片段，落第 5 格而非第 6 格），  
  与 0.9.29 的「hourBlock 格号」教训同源：先算清楚格号再写期望值。

### 24.4 验证

- UI 320 项全绿（新增第 28 段 13 项：结构/隐藏/接线/12 格/高亮格号/  
  深色配色/刻度/无活动隐藏/跳播定位/切模式复用/无录像提示）；  
  Python 252 项 OK；validate 73 项；verify_pkg 236 项。
- 产物 `dist/nvr-0.9.31_x86.fpk`（222.4 KB，app.tgz md5 e42bee43…）。

## 二十五、0.9.32：修复回放页「没有任何录像文件」误报（第十八轮反馈）

### 25.1 现象与定位

0.9.30 起回放页选择摄像头提示「还没有任何录像文件」，0.9.31 依旧。  
pickCam/loadDay/applyPlaybackVisibility/realSegs 与 0.9.29 逐函数 diff  
完全一致——问题出在执行路径上唯一的变化：renderHeat 尾部改调 renderTl()。

真机复现三步走（.probe/repro_empty.sh：真实起服务 + 磁盘造假录像 +  
headless 探针）：

1. `pickCam=ok` 但页面空状态——pickCam 内部 `try{loadDay()}catch` 吞了异常；
2. 裸调 `loadDay()` → `Cannot set properties of null (setting 'textContent')`；
3. 逐元素排查 → `tlTitle=NULL`、`tlBack=NULL`。

### 25.2 根因：早退分支清空容器时把自己的静态骨架也清了

- `#tlane` 的 HTML 里有静态 `<b id="tlTitle">`/`<span id="tlBack">`；
- `renderTl()` 无数据早退分支执行 `el.innerHTML=""`——**把骨架一起清掉**；
- 页面启动时录像清单必然未到 → 必然早退一次 → tlTitle 从 DOM 永久消失；
- 数据到达后再 renderTl → `getElementById("tlTitle")` 为 null → 崩溃；
- 崩溃点在 loadDay 的 renderHeat（renderClips/applyPlaybackVisibility 之前）→  
  日期条/热力条/片段列表全不渲染，空状态提示保持 HTML 默认可见。

0.9.29 无此问题：`#rangeCmp` 是空容器、每次整体重建、不引用静态子节点。  
单元测试没抓到：harness 的 getElementById 自动造桩元素，永远不会是 null。

### 25.3 修复（参照 0.9.29 模式）

- `#tlane` 改为空容器（无任何静态子元素）；
- 标题行（分布标题 + 回到全天）并入每次 renderTl 重建的 innerHTML，  
  「回到全天」用 `el.querySelector("#tlBack")` 在刚建的内容里取；
- 测试新增回归断言：容器必须是空架子 + 源码不得再按 id 找标题/回全天节点。

### 25.4 真机验证（headless 探针，服务端 3 段全时 + 1 段缩时）

- 修复后：空状态消失、heatwrap/dates/clips 恢复显示、loadDay 不再抛异常、  
  时间轴「全天分布」标题正常、片段列表 3 段；
- 播放页热力格回归：打开 08:00 片段 → 第 4 格（08:00–10:00）高亮正确；  
  点第 5 格 → 跳播成功且高亮跟随；点空格 → 不误跳；
- UI 322 项全绿 / Python 全量 OK / verify_pkg 236 项。
- 产物 `dist/nvr-0.9.32_x86.fpk`（222.7 KB，app.tgz md5 0783e925…）。

### 25.5 教训

- **innerHTML="" 清容器前，先问容器里有没有静态骨架**——0.9.29 的  
  rangeCmp 模式（空容器+全量重建）才是可复用的正确范式；
- pickCam 这类 `try{...}catch(e){}` 吞异常的入口，回归时异常完全不可见，  
  探针必须绕过包装直接裸调内层函数；
- window.onerror 抓不到 .then() 里的异常（unhandledrejection），两个都要挂；
- harness 桩的 getElementById 自动建元素 → 「null 崩溃」类 bug 在单测里  
  天然隐身，必须靠真浏览器探针补位。

## 二十六、0.9.33：回放页排版精简 + 时间轴进驻播放器（第十九轮反馈）

### 26.1 三项需求

1. **CH 行并入**：「CH01 · N 个录像文件」（hdrSub）从独立一行并入摄像头  
   名行——`<span class="hdr-sub">` 移进 `.camsel`，小字弱化  
   （`.camsel .hdr-sub{font-weight:400}`），旧 div 行删除；
2. **概览四格下线**：保留时长/占全天/片段数/体积整行删除（HTML+CSS），  
   `renderOverview` 瘦身为只剩「摄像头名 + CH·文件数」两件事，多余调用  
   点全部保留（函数还在，语义没变）；
3. **全天分布时间轴进驻播放器**：`#tlane` 从回放页 heatwrap 移到播放器  
   `p-top` 与 `stage` 之间（画面上方）；「真实时段」基准行与缩放态  
   （hourBlock/回到全天）整体下线，固定全天 24h 视角，只留全时/缩时  
   两行分布；黑底适配（track/lbl/val/ticks 全套 rgba 覆盖），横屏/全屏  
   与 p-heat 同款隐藏；`openPlayer` 接线刷新，色块点跳播保留。

### 26.2 设计取舍

- 缩放态下线的理由：播放器里看的是「这一天录了什么、哪里断了」，  
  2 小时放大与热力条选段职责重叠；hourBlock 只保留给回放页热力条  
  高亮与片段过滤，时间轴对它不再响应（有专门断言验证）。
- 全天视角下缩时折算口径不变：观看时长按「相交 ÷ 源跨度」折算，  
  块按源时刻落位（09:30 → left:39.58%）。

### 26.3 验证

- UI 324 项全绿（第 27 段按新行为重写 30 项 + 27b 排版 4 项）；  
  Python 全量 OK；verify_pkg 242 项（概览四格/回全天入 MUST_NOT_HAVE）。
- 真机探针（headless + 真实服务）：CH 行并入生效且旧行消失、  
  .overview 不存在、时间轴在 stage 之前且黑底计算样式  
  rgba(255,255,255,0.1)、无「真实时段」行、播放页热力格高亮/跳播回归正常、  
  全程无 JS 错误。
- 产物 `dist/nvr-0.9.33_x86.fpk`（222.8 KB，app.tgz md5 4eff03a5…）。

## 二十七、0.9.34：播放页分布时间轴跟随选中时段 + 全宽色条（第二十轮反馈）

### 27.1 三项需求与实现

1. **跟随选中的时段**：0.9.33 曾把时间轴固定为全天视角，本轮恢复  
   `state.hourBlock` 响应——回放页热力条选中某个 2 小时格后，播放页  
   时间轴放大到该窗口显示片段级分布：标题带窗口钟点（「08:00 – 10:00  
   分布」）、刻度变 30 分钟一档、全时摘要从「断录 N 段」改为「覆盖 N%」  
   （窗口太小断录统计无意义）；取消选中（再点同格或「显示全部」）自动  
   回到全天视角。renderHeat 尾部既有接线直接生效，回放页选段即刷新。
2. **色条贯穿屏幕**：两行分布条从「左标签 + 色条 + 右摘要」三段式改为  
   **说明行在上（名称左 · 摘要右）、色条独占一整行**；`.p-tl` 左右  
   margin 归零（内容 padding 12px、分隔线贴边），色条宽度 = 行宽  
   （真机探针 rect 同口径量测 651/651）。
3. **方便手指操作**：色条高度 14px → 播放页 20px（回放页 18px）、  
   色块最小宽度 2px → 3px；标题行右侧加「点色块跳播」小提示；  
   跳播仍是点色条任意位置按横坐标换算时刻，整条都是热区。

### 27.2 排障记录

- 真机探针首轮 TL_fullwidth=BAD:346/395 —— 346/395 ≈ 0.876，是页面  
  外框 transform 缩放导致的量测口径差（getBoundingClientRect 是变换后  
  尺寸、clientWidth 是布局尺寸），改用 rect-vs-rect 同口径对比后  
  651/651 全宽确认；TL_touchH 同理（rect 18px = 布局 20px × 0.876），  
  改看 computed style 确认 20px。教训：缩放容器里量布局，必须统一口径。

### 27.3 验证

- UI 331 项全绿（第 27 段更新：默认全天 + 选中放大两组断言、tl-cap  
  结构、提示文案、全宽/触区 CSS）；Python 全量 OK；verify_pkg 245 项。
- 真机探针：TL_hint/capLayout/fullwidth(651/651)/touchH(20px)/  
  zoomHead(08:00 – 10:00 分布)/zoomTicks/zoomCov/zoomBlocks(3)/  
  backAll 全部 ok，播放页热力格高亮/跳播回归正常，无 JS 错误。
- 产物 `dist/nvr-0.9.34_x86.fpk`（223.3 KB，app.tgz md5 d2140b5e…）。

## 二十八、0.9.35：底部热力格 = 时段选择器（第二十轮反馈·修正）

### 28.1 需求澄清

0.9.34 的「跟随选中的时段」接的是回放页热力条（hourBlock）——用户  
真正的意图是：**播放页底部那排热力格就是时段选择器**，点某格后上方  
时间轴显示该时段内的全时/缩时分布。前一轮理解错了交互源头，真机上  
点底部格子只跳播、时间轴不动。

### 28.2 实现

- **pHeatJump → 先选择后跳播**：点格 i 先 `state.hourBlock = i`  
  （再点同一格 toggle 回 -1），`renderHeat()` 同步回放页热力条并经  
  尾部既有接线触发 `renderTl()` —— 时间轴放大到该 2 小时窗口；随后  
  保留原跳播逻辑（该时段有录像 → 跳首段，可跨模式；空时段 → toast，  
  但选中与空分布照常生效）；
- **视觉**：选中格琥珀描边（`--warn`），正在播的格保持蓝描边；两态  
  重叠时双层描边（内琥珀外蓝）；标题文案改为「点格子选中时段 · 上方  
  显示该时段分布」；
- 顺手加固 `safePlay`：`v.play()` 套 try——桩环境/异常元素不再炸选段  
  链路（本轮测试执行 pHeatJump 真实链路时暴露）。

### 28.3 验证

- UI 338 项全绿（新增 7 项：选中态类/时间轴联动/再点取消/空格选中  
  如实为空/双层描边）；Python 全量 OK；verify_pkg 249 项。
- 真机探针：点第 5 格 → sel 描边 + hourBlock=5 + 时间轴「10:00 –  
  12:00 分布」；再点同格 → 取消 + 回「全天分布」；点第 4 格 → 选中  
  且跳播到 08:00 首段；点空格（第 0 格）→ 照常选中、分布如实显示  
  「无录像/无缩时片段」；全程无 JS 错误。
- 产物 `dist/nvr-0.9.35_x86.fpk`（223.9 KB，app.tgz md5 6fb8e2b6…）。

## 二十九、0.9.36：顶部热力图 ↔ 底部 12 格双向联动（第二十一轮反馈）

### 29.1 需求与实现

0.9.35 只做了「底部格子 → 顶部缩放」单向；本轮补上反向，两边成为  
一个完整的互动系统：

- **全天视图点顶部热力图**：按点击横坐标换算时刻 → 选中所在 2 小时格  
  （底部格子琥珀描边、顶部自己放大到该窗口）+ 跳播到点击时刻；  
  提示文案分态：全天「点色块选中时段」/ 放大「点色块跳播」；
- **放大态头部新增「‹ 全天」返回钮**：一键取消选中（底部描边同步  
  消失、回全天分布），不再只能靠再点底部同一格取消；
- **底部格子选时段**（0.9.35）保持不变。

### 29.2 排障记录

- 首轮探针全挂（title 回落默认值）——探针 JS 用了单引号，把 bash  
  单引号包裹的 PROBE 字符串截断了；改用无引号属性选择器  
  `.tl-track[data-tlkind=full]`；
- `data-tlall` 后多写了一个游离引号，HTML 解析成带引号的属性名，  
  querySelector 匹配不到，「‹ 全天」点击为 null——探针 BI_allBtn=  
  MISSING + OUTER null.click 一眼定位。

### 29.3 验证

- UI 341 项全绿（新增 3 项联动断言）；Python 全量 OK；verify_pkg 252 项。
- 真机探针：全天视图点全时轨 35% 处 → hourBlock=4 + 底部第 4 格  
  琥珀描边 + 顶部放大「08:00 – 10:00 分布」+ 返回钮可见；点「‹ 全天」  
  → 取消选中回全天、描边消失；点底部第 4 格 → 顶部放大（反向回归）；  
  全程无 JS 错误。
- 产物 `dist/nvr-0.9.36_x86.fpk`（224.4 KB，app.tgz md5 e4ef3ef8…）。

## 三十、0.9.37：16×/32× 倍速 + 热力图播放定位竖线（第二十二轮反馈）

### 30.1 倍速 16×/32×

- `SPEEDS = [1,2,4,8,16,32]`，选档弹层与标签自动跟上；
- **浏览器 playbackRate 硬上限 16**：设置时钳到 16，>16 的差额由补速  
  定时器每秒跳秒补足（32× = 16× 倍速 + 每秒再跳 16 秒），切档/暂停  
  时清理；跳秒超过片长由 ended 自然接续下一文件；
-

### 30.2 热力图播放定位竖线

- 顶部热力图两条分布轨（全时/缩时）各注入 `.tl-cursor`（2px 白线 +  
  光晕），**全天/选中时段 × 全时/缩时四种形态都显示**；
- 位置口径与 syncPlayer 的 srcNow 一致：全时按钟点，缩时按播放坐标  
  占比映射回源时刻——两条轨竖线永远同位（同一钟点）；
- **轻量跟随**：timeupdate → syncPlayer → syncTlCursor 只改样式不重建  
  DOM；连播切段走 openPlayer → renderTl 重建后 `syncTlCursor()` 立即  
  摆到新文件位置（探针实测 09:00 文件起点 37.5%）；
- 当前文件与所选时段无交集时隐藏（不误导）。

### 30.3 排障记录

- 首轮竖线测试 5 项失败：①喂错数据源——renderTl 读 state.rec 真实  
  清单，光标读 state.segs/state.t，两者都要喂；②桩的 querySelector  
  返回万能桩（每次新建），style 变更无法持久断言——把位置计算抽成  
  纯函数 `tlCursorFrac()`，单测断纯函数值 + 源码接线断言，真机探针  
  断真实 DOM。

### 30.4 验证

- UI 354 项全绿（第 29 段 13 项）；Python 全量 OK；verify_pkg 256 项。
- 真机探针：SPEEDS 6 档；竖线 2 条（两轨各一）；全天 08:00 → 33.33%、  
  推进到 08:30 → 35.42%、选中时段窗口内 25%、缩时按源时刻、切段后  
  竖线跳到新文件起点 37.5%；全程无 JS 错误。
- 产物 `dist/nvr-0.9.37_x86.fpk`（225.7 KB，app.tgz md5 af6bb621…）。

## 三十一、0.9.38：缩时切分方式（整段拼接 / 活动切片）通用 + 每路（第二十三轮反馈）

### 31.1 需求与方案讨论（三轮收敛）

- 用户连续三轮讨论检测/切分架构：运动检测与人车检测算力评估 → 流缓存内存 +
  按压制结果拆片的可行性 → 双路分流（主拷贝及时落盘 + 实时精编分支）评估；
- 结论：主拷贝 `-c copy` 落盘不动（安全底线），「活动切片」作为**可选项**
  落地——每个活动区间单独成片、重编码帧级精确，消除片段内静止段，
  热力图/跳播定位从 ±1 个 GOP 收紧到帧级；不做自建内存缓冲（page cache
  已覆盖收益）、不做全实时压制（鲁棒性反转、负载恒定化）。

### 31.2 服务端实现

- `runtime.py`：新增 `clip_style`（join/split，枚举键 coerce 防布尔顶掉）与
  `clip_mode` 白名单（follow/join/split）+ `CLIP_STYLE_LABEL`；
- `ffmpeg_cmd.precise_cmd()`：`-ss/-to` 输入侧 seek + 重编码，输出固定 H.264
  （浏览器 `<video>` 兼容）；hw=qsv 走 `h264_qsv -global_quality`，软编
  libx264 veryfast；speed>1 复用 setpts+fps 封顶规则；
- `condense.write_split()`：与 write_one（整段拼接）共用同一套判定结果，
  导出方式不同——每个区间产出 `<stem>.cNN.mp4`（排序即时间序，回放页目录
  扫描零改动）+ `<stem>.idx.json` 侧车（片段↔真实时刻映射）；
- `_export_precise()` 三级回落：QSV → libx264 → 流复制；**入口时**查硬编
  可用性（首轮实现把负缓存判据放循环里，QSV 失败后连软编都被跳过——测试
  抓住后改为入口判定），失败进 `_HW_FAIL` 负缓存 10 分钟不再重试；
- `link.py`：`eff_clip_mode()`（follow 展开通用值）；**切分方式进幂等键**
  （join↔split 是两种产物，不进指纹切过去的路永远不会再处理）；定时批量
  模式下 split 路每拍即刻入队（`_enqueue(only_split=True)` 只扫这几路），
  join 路仍等批量窗口——「每路即刻精编分支」的服务端形态；`_process` 按
  `it["style"]` 分流，日志带切片数；
- `nvr_slim.py`：norm_cameras/cameras() 透传 clip_mode；/api/config 走
  DEFAULTS 枚举 coerce 自动收 clip_style；set_link 同步支持。

### 31.3 前端实现

- 通用设置「录制与压制衔接」卡片新增「缩时片段切分方式」chips + 分态说明
  （整段拼接=流复制零损失；活动切片=帧级精确、编码开销更高、建议核显机）；
- 每路编辑态「缩时压制」段新增切分方式三选（跟随通用/整段拼接/活动切片），
  只读摘要显示生效值（follow 展开「跟随通用 · X」），手风琴收起态标题带
  当前生效方式；LK.clipStyle 进 localStorage 与服务端推送/回读三条链路。

### 31.4 验证

- 新增 `test/test_clipsplit.py` 19 项：枚举钳制、precise_cmd 两路形态、
  三级回落与负缓存、write_split 产物/幂等/半途清场、eff_clip_mode、
  幂等键随方式变化、定时模式 split 路即刻入队而 join 路等窗口；
- UI 366 项全绿（第 30 段 13 项；两处旧断言随摘要文案更新为
  「整段拼接 · 1× · 10 天」）；Python 全量 OK；verify_pkg 281 项；
- 产物 `dist/nvr-0.9.38_x86.fpk`（231.1 KB）。

## 三十二、0.9.39：修复活动切片真机零输出 + 切片真实覆盖窗（第二十四轮反馈）

### 32.1 问题定位（真实 ffmpeg e2e 探针）

本机装 imageio-ffmpeg（内置 ffmpeg 7.1 二进制）搭 `.probe/e2e_split.py`，
端到端跑 `precise_cmd` / `write_split`，两个硬结论：

1. **`-hwaccel qsv` 被放在 `-i` 之后** —— 输入选项落到输出侧，
   ffmpeg 报 "Error opening output files: Invalid argument"（rc=-22），
   QSV 那一级在**所有机器**上 100% 必挂。`encode_cmd`（fine 档）与
   `speed_cmd`（reencode 档）同样带病，只是此前从未走到过。
2. 管线本身健全：x264 回落产出正确（10s 窗口精确 10.0s，speed=2 减半），
   write_split 产物/幂等/清场全对。零输出在导出层之外的解释只剩
   「每项都失败进 failed 表」或「历史积压把今天的片段压在队尾」——
   切分方式进幂等键后，join→split 会让**全部历史**重新入队（旧正序排序），
   用户看到的"所有摄像头无输出"正是今天的分片排在队尾消化不到。

### 32.2 修复（0.9.39）

- **hwaccel 位置**：三处导出命令统一移到 `-i` 之前（真机有核显时 QSV
  一级即可命中，不再白白挂一次进负缓存）；
- **免编码器终极兜底 `copy_speed_cmd`**：`-itsscale 1/speed`（时间戳缩放
  实现倍速）+ `-c copy`，不依赖任何编码器。此前 speed>1 的"流复制兜底"
  会悄悄换成 libx264 重编码——LGPL 构建的 ffmpeg（无 libx264）上三级
  全灭。回落链改为 QSV → libx264 → itsscale 转封装；`_export_copy(force)`
  区分两处调用（_export_one 只在 speed>1 时重试，_export_precise 无条件）；
- **最新优先**：`_new_items` 倒序出队，改切分方式后今天的片段几分钟内
  出片，历史积压留给空闲时段追；
- **真实覆盖窗**：服务端 `recordings_payload` 读 `.idx.json` 侧车，给
  `.cNN.mp4` 附 `w=[分片起点+ss, 分片起点+to]`（带 (size,mtime) 缓存）；
  前端 realSegs 优先消费 `f.w`——同一 5 分钟里多个片段不再互相挤成
  1 秒、末片不再虚标覆盖 5 分钟，热力图/列表/播放 OSD 全部变准；
- **红色样式**：热力图定位竖线 `#ff5a5a` + 红色光晕；播放控件当前时刻
  （tNow，居中主值）改 `#ff6b6b`。

### 32.3 验证

- `.probe/e2e_split.py` 复跑：qsv 级报错从致命解析错误变为编码器初始化
  失败（本机无核显的正常结果，真机可命中），x264/write_split 产物不变；
- test_clipsplit 26 项（+7：hwaccel 位置回归 ×3 命令、copy_speed_cmd
  形态、force 语义、_export_one 倍速回落、_clip_window 2 项、最新优先）；
- UI 370 项全绿（+4，旧「竖线·样式」断言同步改红）；Python 全量 OK；
  verify_pkg 291 项；产物 `dist/nvr-0.9.39_x86.fpk`（233.3 KB）。

## 三十三、0.9.40：修复活动切片无法播放 + 断开刷屏（第二十五轮反馈，真机日志定位）

### 33.1 真机日志读出的三件事

用户贴来 0.9.39 真机日志，关键信息：

1. **零输出已解决**：「缩时完成 …（N 个切片）」大量出现，三级回落按预期
   走到软编（真机 gpu=none：i965 老驱动无 HEVC profile）；
2. **新缺陷：切片全部 400**：`GET /api/record/file?kind=clip&name=01-15-00.c02.mp4`
   成串 400 —— 文件产出了却播不了。根因是 `_record_file` 的文件名白名单
   `^\d{2}-\d{2}-\d{2}\.mp4$` 不认 `.cNN.mp4` 后缀（0.9.38 引入切片命名时
   只改了产物侧，忘了服务侧的白名单）；
3. **日志刷屏**：一次页面刷新浏览器掐掉十几个挂起的 `/api/recordings` 响应，
   `_send` 的 `end_headers()` 在 try 之外，头缓冲 flush 时的
   ConnectionResetError 逃到 do_GET 兜底被当异常记日志（一次 16 条），
   会把真问题从 800 条环形缓冲里挤出去。

另：录制看门狗反复重启三路（75~99 秒无新数据）——看门狗逻辑本身正确，
结合「快照失败：拉流超时」，更像摄像头侧断流（凌晨/无线），非本轮代码问题。

### 33.2 修复（0.9.40）

- **切片白名单**：正则抽成模块级 `NAME_FULL_RE` / `NAME_CLIP_RE`（可单测），
  `_record_file` 按 kind 区分——clip 接受 `(\.c\d{2,3})?`、full 维持原名
  （full 目录里永远不会出现 .cNN，不让 full 请求拿切片名撞目录）；
  `/api/record/probe` 同步放行切片名；
- **断开静默**：`end_headers()` 挪进 try（头缓冲 flush 也走 socket）；
  do_GET/do_POST 兜底层对 (BrokenPipeError, ConnectionResetError) 静默——
  客户端主动断开是正常现象，不再是异常。

### 33.3 验证

- test_clipsplit 29 项（+3：白名单接受切片名、拒绝穿越/垃圾、full 拒绝切片名）；
- 后端全量 OK；UI 370 项全绿；verify_pkg 296 项；
- 产物 `dist/nvr-0.9.40_x86.fpk`（234.1 KB）。

## 三十四、0.9.41：修复缩时调度一路独占（第二十六轮反馈，真机日志定位）

### 34.1 用户观察与日志对账

用户：「只有一路摄像头实时切片成功，存了 7 个视频 + 5 个 idx.json」。日志对账：

- **7 视频 + 5 idx 完全正常**：5 个源分片（00-30/00-35/00-35-07/00-40/00-45）
  各产一个 idx.json，按活动区间拆出 2+1+1+2+1=7 个切片——数字严丝合缝；
- **切片播放已通**：`kind=clip&name=00-30-00.c01.mp4 → 206`，0.9.40 修复生效；
- **只有餐厅在出片**：整份日志上百条「缩时完成」全部是 `_clip/餐厅/…`，
  其它四路一条都没有。

### 34.2 根因：排序被中文目录名的码点劫持

0.9.39 的「最新优先」实现为按**文件路径倒序**。目录名是中文摄像头名，
Unicode 码点排序：餐(U+9910) > 院(U+9662) > 车(U+8F66) > 客(U+5BA2)——
餐厅排最前，把它的全部历史积压（上千分片）消化完之前，其它四路
一片都出不来。「最新优先」名不副实：路径排序只有**同一路内**才是时间序。

### 34.3 修复（0.9.41）

- `_new_items` 排序键改为 `(mtime, path)` 倒序：mtime 是"哪个分片最新
  录完"，跨路按时间公平；mtime 相同用路径兜底保持稳定；
- 测试更新：`_seed` 支持按文件递减 age（mtime 可区分）；新增
  `test_items_newest_across_cams`——车库入口放旧分片、一楼大堂放新分片，
  新的必须在前（旧实现会按路径排序挂掉）；测试断言别用绝对路径
  `split(os.sep)[0]`（Windows 上是盘符），item 自带 `dir` 字段。

### 34.4 验证

- test_clipsplit 30 项、test_link 29 项、后端全量 OK；UI 370 全绿；
  verify_pkg 296 项；产物 `dist/nvr-0.9.41_x86.fpk`（234.4 KB）。

### 34.5 升级预期

0.9.41 装上后，调度立即按 mtime 公平轮转：今天各路的分片先出片，
四路的历史积压按"最新优先"在空闲时段交错消化，不再有一路独占。

## 三十五、设计讨论：标记与视频解耦（Aqara 时间轴模式，第二十七轮）

### 35.1 参照系

用户给出 Aqara App 回放页截图：**一条视频轨**（蓝=有录像）+ 叠加的**事件标记层**
（橙=移动侦测"识别到有人"）。标记不是第二份视频，只是「时刻+事件类型」元数据，
点标记即 seek 到连续视频对应位置。模型：一份视频 + 几 KB 标记 + 按标记 seek。

### 35.2 对照现状：标记层已经存在

pixdiff 引擎 0.5s 粒度活动窗口（3~6% 单核/路）+ 缩时切片写出的 `.idx.json`
侧车（片段↔真实时刻映射）——热力图/时段选择/跳播消费的就是这套数据。
因此问题实质是：**能否不物化缩时视频文件，只靠标记 + 全时视频按需生成？**

### 35.3 评估结论

- **虚拟缩时（不物化切片）可行，且对无核显机器（本机 gpu=none 软编）收益最大**：
  常驻重编码归零（省掉 10~20s/分片与 1200+ 积压），热力图精度不变（0.5s），
  存储不再增 5~15%；代价是跳播先 seek（0.3~1s/窗）、「下载缩时成片」变为
  导出时临时裁剪；
- **定时按时间戳裁剪老旧视频可行，但要区分两层**：
  ① 标记寿命 > 视频寿命——活动台账（每天每路几 KB）可永久保留，视频到期裁掉
  后热力图与活动统计仍完整（只是无画面）；
  ② 长期画面保留仍需物化——"30 天前只留有活动的画面"靠归档裁剪：全时临近
  保留期时把活动区间裁出（深夜批量而非常驻），之后全时正常过期；
- **与 Aqara 的本质差异**：其标记来自摄像头边缘 AI；我们自算（pixdiff），
  口径自主可控（忽略区/灵敏度/未来接 ONVIF 事件双源）。

### 35.4 建议落地形态（暂未开工，待确认）

1. 活动台账常驻化：索引从"跟分片文件走"改为"按路按天独立台账"（零风险地基）；
2. 播放页「虚拟缩时」模式：点热力格直接在全时视频上按窗口跳播，与物化模式
   并存、按路可选；
3. 「归档裁剪」定时任务：全时临近保留期时裁出归档片段 → 终局形态
   **全时短留 + 归档片段长留 + 台账永久**。
