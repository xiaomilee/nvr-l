# NVR-L fpk · 构建说明

三合一：实时预览 / 全时录像与回放 / 归档浓缩回放。**录像由本应用自己拉主码流落盘**，
不再依赖其它 NVR 的目录；页面在设备上是单个 `index.html`。

这份文档只讲**怎么构建、怎么测、包里装了什么**。
版本沿革、真机排障史、早期验证记录与未覆盖项都在同目录的
[`CHANGELOG.md`](CHANGELOG.md)；安装与快速上手见仓库根
[`README.md`](../../README.md)，配置键见
[`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md)。

## 目录树

```
fpk/
├── manifest               appname=nvr / version=0.9.126 / platform=x86 / service_port=18080 / checkport=false
├── ICON.PNG / ICON_256.PNG   图标（gen_icons.py 纯标准库生成）
├── (无 health.json)       探活由 cmd/main 的 wait_health 读 var/port.txt 的真实端口
├── (无 *.sc)              端口是安装时自选的，不写死端口转发声明
├── cmd/                   9 个生命周期脚本（fnOS 约定：status 0=运行 3=停止）
│   ├── main               start/stop/status，启动后等健康检查通过才算成功
│   ├── install_init       装前只探测 python3 与已存在数据目录的可写性
│   ├── install_callback   建目录 + 修属主/ACL、写初始配置、核显自检、落向导字段
│   ├── upgrade_init       停服务 + 备份 config/queue/db（保留最近 3 份）
│   ├── upgrade_callback   合并新版默认值，保留用户配置，属主不对才修
│   ├── uninstall_init     停服务
│   ├── uninstall_callback 清 tmp；录制与片段不动
│   ├── config_init        配置损坏时先修复
│   └── config_callback    把向导字段写回 config.json
├── config/
│   ├── privilege          run-as: package，用户/组 = nvr
│   └── resource           data-share nvr-output（rw 给 nvr）
├── wizard/                install / config / upgrade / uninstall（**必须进包**）
├── app/
│   ├── ui/
│   │   ├── index.html     合成产物：单 HTML，页面在设备上就是它
│   │   ├── config         桌面入口端口（必须是纯数字，不留占位符）
│   │   ├── images/
│   │   ├── css/           样式源：theme/base/components/pages/player（构建时合成，**不进包**）
│   │   └── js/            脚本源：core/ 8 + components/ 3 + pages/ 10 + player/ 1 共 22 份（**不进包**）
│   ├── server/            25 个后端模块；nvr_slim.py 是唯一 HTTP 入口（约 4000 行）
│   ├── bin/               可选静态 ffmpeg（见 app/bin/README.md）
│   ├── share/             随包模型 yolo11n.onnx
│   ├── tools/doctor.sh    装完的一次性只读体检
│   └── vendor/            随包 wheel
├── tools/
│   ├── validate.py        81 项结构/语法校验（打包前必跑）
│   ├── compose_ui.py      前端合成：css/*.css 与 js/**/*.js → index.html（幂等，可 --check）
│   ├── gen_icons.py       纯标准库生成四件图标
│   ├── pack_fpk.py        无 fnpack 时的 fpk 打包器（checksum=md5(app.tgz) 自动回填）
│   └── deploy_test.py     测试机部署与九项接口自检（失败自动回滚）
├── dist/                  打包产物 nvr-<版本>_x86.fpk（不入库）
└── test/
    ├── test_*.py          后端单测：build.sh 遍历，**新增文件自动进闸门**
    ├── ui/*.test.js       前端断言：build.sh 逐个显式列出（新增要改 build.sh）
    ├── btn_harvest.py     设置项采集器：改值 → 采三份快照 → 还原 → 出报告（手动跑，不进闸门）
    └── test_btn_harvest.py 采集器离线守卫：取值策略与报告口径
```

**后端模块清单逐项、每条 HTTP 路由的出处在
[`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) 的两张机器可读表里**，
由 `test/test_doc_suite.py` 与源码比对，不在这里再抄一份。

## 打包

```bash
bash build.sh            # 合成前端源 → 结构 → 全部单测 → 打包（有 fnpack 用 fnpack build）
bash build.sh --check    # 只校验不打包
```

五步顺序（`--check` 跑前四步）：

| 步 | 做什么 | 为什么 |
|---|---|---|
| 0/5 | `tools/compose_ui.py` 合成前端 | **必须排在结构校验之前**：validate 会核对 `index.html` 与 `css/`、`js/` 是否同步，放后面就等于拿上一轮的产物过闸门 |
| 1/5 | `tools/validate.py` 81 项 | 结构、语法、py_compile、前端源与产物同步、包内必备文件 |
| 2/5 | 遍历 `test/test_*.py` | 逐个列文件名的写法曾漏跑三份测试；遍历后新增测试自动纳入。`test_http_smoke.py` 需真起服务且依赖固定端口空闲，**单独手动跑** |
| 3/5 + 4/5 | 前端 node 断言 | `test/ui/*.test.js` 逐个显式列出；新增前端测试要改 `build.sh` |
| 5/5 | 打包 | 优先官方 `fnpack build`；没有则回退 `tools/pack_fpk.py` |

`pack_fpk.py` 会**回写 `manifest.checksum`**：只是试打包也要事后
`git checkout -- manifest`，别把一次性的 md5 提交进去。

## 前端合成（0.9.123 起）

```bash
python tools/compose_ui.py           # css + js → index.html（幂等，没变化不写文件）
python tools/compose_ui.py --check   # 只查是否同步，不同步退出码 1
```

样式合成顺序固定为 `theme → base → components → pages → player`
（令牌在前、页面覆盖在后），**不要按文件名排序**。

脚本没法这样定顺序：`const`/`let` 不提升，声明出现的次序就是执行次序。
所以脚本的合成顺序是**标记在 `index.html` 里的位置**——代码从哪儿搬走
就还回哪儿去，`/* @@js:begin core/api.js */` … `/* @@js:end */` 之间由
`app/ui/js/core/api.js` 填。要调顺序就得挪标记，这件事必须被看见：
`test/ui/js_split.test.js` 把顺序钉成一张表（166 项）。

页面在设备上仍是单个 `index.html`（无 `<link>` / `<script src>` 外链，
首屏不闪白；更要紧的是 `test/ui/*.test.js` 全都取「最后一个 `<script>`」
当脚本源，一旦拆成多个标签，十几个测试会静默跑错段）。
`app/ui/css/` 与 `app/ui/js/` 只是仓库里的源，不进包——
否则会让人以为在设备上改它们能生效。

## 测试表怎么分工

**按钮验收链路**（批次 5，见 `docs/ROADMAP.md`）：

- `test/button_matrix.py` 是**声明表**：该验什么、`pass / partial / fail /
  unknown` 怎么算过、`DEFAULTS` 与前端源码双向覆盖守卫。
- `test/test_btn_effect.py` 是**执行器**：真起本地服务（绑 `127.0.0.1`、
  端口由系统分配、临时目录跑完即删），逐个设置项验证「接口回执 → 接口回读
  → 磁盘落盘 → `/api/link` 里运行中的对象状态」四层证据。
  **新增设置项只往 `CASES` 加一行**，`effects` 必须指向运行中对象的字段路径，
  不能写配置回读——否则这一项就退化成「只查 UI 值」的另一种说法。
  `/api/link` 顶层的 `record_enable` 是 `link_payload()` 现读 config 补的，
  不是 `Recorder` 的状态；`test_90` 会把这个写错的证据路径挡下来。
- 设置页有两条写入口：`/api/config` 与 `/api/link/policy`。
  后者同样能改 `record_enable` / `record_segment_sec`，所以 `test_93` 单独验它
  会通知 `Recorder`；`test_94` 钉住两条入口对 `record_enable` 的输入强校口径一致
  （字符串 `"false"` 曾经从 policy 进来被判成开）。
- 两表分工不合并，另有一条对齐守卫钉住 `CASES` 的 key 与生效证据不漂移。

**全表采集用 `test/btn_harvest.py`**（不进闸门，手动跑，20 项约 30 秒）：

```bash
python test/btn_harvest.py                       # 全表，报告写进 test-reports/
python test/btn_harvest.py --only record_enable,cameras
python test/btn_harvest.py --skip record_dir --keep   # 留下临时 var 便于看服务日志
```

它对声明表每一行做同一件事：读磁盘原值 → 换成另一个合法值 `POST /api/config`
→ 等 500ms（`snapshot_interval` 等 3.5s，它要等巡检那一拍）→ 取
`/api/config` + `/api/status` + `/api/link` 三份快照 → **还原原值** →
喂 `button_matrix.verify_row` 判四层。退出码 = 报告通过 **且** 还原成功。

三条口径别改：①「另一个合法值」由 `runtime.coerce` 验过
（`test_btn_harvest.py` 钉住），不许自己发明范围；② 还原值从磁盘
`config.json` 取，不从 `/api/config` 取（接口里的摄像头地址是
`rtsp://***@` 脱敏值，拿它回推会把真地址写坏）；③ 它只打 `127.0.0.1`，
源码里不许出现 LAN 地址或 SSH —— 改配置是有副作用的动作，
能指向测试机就会变成一次真实的配置抖动（`test_14` 守着这条）。

报告里 `partial` 是**只验到落盘**（该项没有运行态出口、要重启、或 ffmpeg argv
采不到），采集器会把它们逐条列出来，别把「通过 9 / 部分 11」读成全过了。

**改配置键要同步 `docs/CONFIGURATION.md`**：那份文档的主表由
`test_config_doc.py` 解析，逐项比对 `runtime.DEFAULTS` 的类型、默认值、
`_ENUM_KEYS` 白名单与三个范围常量，并逐个核实「出处」列指向的文件存在。
加键、改默认值、改夹取区间却没改文档，2/5 会当场红。文档里的
「生效方式」说的是**改完谁会重新读**，和「接口上有没有观测出口」是两回事
—— `condense_mode` 热生效但没有运行态字段，声明表如实判 `partial`。

**加路由 / 模块 / 页面要同步 `docs/ARCHITECTURE.md`**：
`test/test_doc_suite.py` 拿两张表与源码比对，也核对文档版本与
`runtime.VERSION` 一致、文档间相对链接指向的文件真实存在。

## fpk 布局

包根是 tar.gz，成员（顺序固定）：`ICON.PNG`、`ICON_256.PNG`、`app.tgz`、
`cmd/`、`config/`、`manifest`、`ui/`，以及 `wizard/`。

- `app.tgz` 里是 `app/` 的内容（在归档根），自检断言含
  `server/nvr_slim.py`、`ui/index.html`、`ui/config`。
- 包根 `ui/` 只有 `config` + `images/`（`UI_ROOT_KEEP`），
  **不含 `index.html`**，也不含 `css/` / `js/` 源。
- `manifest.checksum` = `app.tgz` 的 md5。
- 仓库根的 `dist/`、`test/`、`tools/`、`docs/`、`BUILD.md` 都不进包。

自检必须存在的成员：`manifest`、`app.tgz`、`cmd/main`、`config/privilege`、
`config/resource`、`ui/config`、两枚 ICON、`wizard/install`。
打包只校验结构与程序行为，**不校验 fpk 在真机安装时的
manifest / privilege / wizard 正确性**——那要靠 `tools/deploy_test.py`
在测试机真装一遍来暴露。

## 部署

```bash
cd nvr-slim/fpk && python tools/deploy_test.py \
  --fpk dist/nvr-<版本>_x86.fpk --report ../../test-reports/deploy-<版本>.json
```

九项接口自检（含「无凭证必须 401」，这项要求的是「被拦住」，口径与其余相反），
任一步失败自动回滚到备份并重启。**测试机 `192.168.2.152` 允许自动部署；
生产机 `192.168.2.6` 一律手动升级**，自动任务不碰它。
部署规则与测试平台参数见 `docs/ROADMAP.md`。
