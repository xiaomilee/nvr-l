/* 设置页回归测试（无浏览器）
   做法：从 app/ui/index.html 里抽出主脚本，在极简 DOM 桩上执行，
   验证「不抛异常 + 产出结构正确 + 编辑态草稿/保存/重置语义正确」。
   用法：node test/ui/settings.test.js */
require("./harness.js");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = path.join(__dirname, "..", "..", "app", "ui", "index.html");
const src = fs.readFileSync(HTML, "utf8");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g) || [];
if(!blocks.length){ console.error("找不到 <script> 块"); process.exit(1); }
const code = blocks[blocks.length-1].replace(/^<script>/, "").replace(/<\/script>$/, "");

let pass = 0, fail = 0;
function ck(name, cond){
  if(cond){ pass++; }
  else { fail++; console.log("  FAIL " + name); }
}
const ctx = vm.createContext(global);
try{
  vm.runInContext(code, ctx, { filename:"index.html" });
}catch(e){
  console.error("顶层执行失败：" + e.message);
  process.exit(1);
}
const body = () => document.getElementById("camBody").innerHTML;
const run = s => vm.runInContext(s, ctx);
const get = s => vm.runInContext(s, ctx);
const pickHtml = () => document.getElementById("camPick").innerHTML;

/* ---- 0. 初始状态 ---- */
ck("通用·默认收起", get('SET.genOpen') === false);
ck("后端·配置推送与快照拉取就绪", typeof get('pushCfg') === "function" && typeof get('pullSnap') === "function");

/* ---- 1. 非编辑态：通用设置在上，摄像头在下，两区明显分开 ---- */
run('state.camId="cam-01"; SET.open=null; SET.editing=false; SET.draft=null; SET.genOpen=true; renderCamTabs();');
let h = body();
ck("通用区有标题", h.includes("通用设置"));
ck("通用区在前", h.indexOf("保存目录") < h.indexOf("data-addcam"));
ck("三区标记齐全", h.includes("zone gen") && h.includes("zone cam") && h.includes("zone diag"));
ck("有摄像头列表", h.includes("data-camrow"));
ck("有添加入口", h.includes("data-addcam"));
ck("只读态没有删除入口（收进编辑页底部两段确认）", !h.includes("data-delcam"));
ck("已去掉品牌下拉", !h.includes("brand"));
ck("已去掉录像策略", !h.includes("录像策略"));
ck("已去掉分组", !h.includes("分组（可空）"));
ck("已去掉扫描局域网", !h.includes("扫描局域网摄像头"));
ck("已去掉账号密码输入", !h.includes("密码</label>"));
ck("公共区无滑杆", !h.includes('type="range"'));

/* ---- 2. 通用设置：保存后折叠 ---- */
ck("通用·展开态有保存键", h.includes('data-gensave="1"'));
run('SET.genOpen=false; renderCamTabs();');
h = body();
ck("通用·折叠后显示摘要", h.includes("录制已启用") && h.includes("即刻压制"));
ck("通用·折叠后无保存键", !h.includes('data-gensave="1"'));
ck("通用·折叠后有展开入口", h.includes("data-gentoggle"));
ck("通用·折叠后摄像头区不受影响", h.includes("data-camrow") && h.includes("data-addcam"));
ck("通用·折叠后无保存目录输入框", !h.includes('id="recDir"'));
run('SET.genOpen=true; renderCamTabs();');

/* ---- 3. 展开只读 ---- */
run('SET.open="cam-01"; renderCamTabs();');
h = body();
ck("只读·专属卡片标记", h.includes("仅此摄像头的参数"));
ck("只读·专属卡片染底", h.includes("ccard"));
ck("只读·有修改键", h.includes('data-editcam="1"'));
ck("只读·画布锁定", h.includes("maskstage locked"));
ck("只读·展示主码流", h.includes("rtsp://192.168.2.108:8554/ch1"));
ck("只读·无滑杆", !h.includes('type="range"'));

/* ---- 4. 编辑态独占一屏 ----
   0.9.18 起「缩时压制」默认折叠，判定/画质/网格都在折叠区里，
   先展开再断言这些区块；折叠行为本身在第 4b 段验证。 */
run('openEditor("cam-01");');
h = body();
ck("编辑·不再显示公共卡", !h.includes("保存目录"));
ck("编辑·有保存键", h.includes('data-save="1"'));
ck("编辑·有恢复默认", h.includes('data-reset="1"'));
ck("编辑·有取消", h.includes('data-cancel="1"'));
ck("编辑·主码流输入框", h.includes('data-k="main"'));
ck("编辑·子码流输入框", h.includes('data-k="sub"'));
ck("编辑·未保存提示", h.includes("编辑中 · 未保存"));

/* ---- 4b. 0.9.24：四段手风琴（一次只展开一段） + 缩时总闸 + 倍速 ----
   折叠不等于失明：收起时标题右侧仍然显示该段当前值。 */
ck("手风琴·默认展第一段（名称与码流）", get('SET.acc') === "base"
   && h.includes('data-k="main"') && !h.includes('data-cond="1"'));
ck("手风琴·四段标题齐全", (h.match(/data-acc="(base|keep|condense|mask)"/g) || []).length === 4);
ck("手风琴·收起段标题带当前值", h.includes("全时 3 天") && h.includes("整段拼接 · 1× · 10 天"));
ck("手风琴·删除收在编辑页底部", h.includes('data-delcam="') && h.includes("删除这个摄像头")
   && h.includes("已录的文件不会被删除"));
run('SET.acc="keep"; renderCamTabs();');
h = body();
ck("保留·全时天数 stepper", h.includes('data-ret="fullDays"'));
ck("保留·分片长度 chips 六档", (h.match(/data-camseg="/g) || []).length === 6);
ck("保留·缩时保留不在这段", !h.includes('data-ret="clipDays"'));
ck("保留·指明缩时天数在缩时压制里", h.includes("缩时片段的保留天数在「缩时压制」里单独设"));
run('SET.acc="condense"; renderCamTabs();');
h = body();
ck("缩时·展开后有总开关", h.includes('data-cond="1"'));
ck("缩时·总闸是独立一条（不与普通开关并列）", h.includes('class="master"'));
ck("缩时·影响预估四格", (h.match(/class="c"><div class="kv"/g) || []).length === 4);
ck("缩时·缩时保留在菜单里", h.includes('data-ret="clipDays"'));
ck("缩时·倍速四档", (h.match(/data-speed="\d"/g) || []).length === 4);
ck("缩时·常用段只露灵敏度", (h.match(/data-sens="(low|mid|high)"/g) || []).length === 3
   && !h.includes("data-engine="));
ck("缩时·进阶默认收起", h.includes('data-adv="1"') && !h.includes('data-k="judge.pixelDelta"'));
run('SET.adv = true; renderCamTabs();');
h = body();
ck("缩时·进阶展开后有像素差门槛滑杆", h.includes('data-k="judge.pixelDelta"'));
ck("缩时·进阶展开后有最短事件/模糊/抽样/采样/合并滑杆",
   h.includes('data-k="judge.minEvent"') && h.includes('data-k="judge.blur"')
   && h.includes('data-k="judge.fps"') && h.includes('data-k="judge.probeSec"')
   && h.includes('data-k="judge.mergeGap"'));
ck("缩时·进阶底部标默认值", h.includes("默认值：门槛 24"));
ck("缩时·画质默认收起", h.includes('data-outopen="1"') && !h.includes('data-k="day.crf"'));
run('SET.outOpen = true; renderCamTabs();');
h = body();
ck("缩时·展开后有画质滑杆", h.includes('data-k="day.crf"'));
ck("缩时·画质折叠行收起时显示当前档", src.includes('(SET.outOpen ? "" : esc(qualLine(p)))'));
run('SET.acc="mask"; SET.adv=false; SET.outOpen=false; renderCamTabs();');
h = body();
ck("缩时·收起后明细消失只剩标题条", !h.includes('data-cond="1"') && h.includes("整段拼接 · 1× · 10 天"));
ck("缩时·展开后有忽略区画布", h.includes("maskCv-ig"));
ck("编辑·画布可画", !h.includes("maskstage locked"));

/* ---- 4c. 总开关关闭 → 参数区置灰 ---- */
run('SET.acc="condense"; draftSet("condense", false); renderCamTabs();');
h = body();
ck("缩时·关闭后参数区 dim", h.includes('class="dim"'));
ck("缩时·关闭后有提示", h.includes("总开关已关闭"));
ck("缩时·关闭后标题条改口", h.includes("已关闭 · 只录全时"));
run('draftSet("condense", true); renderCamTabs();');
h = body();

/* ---- 5. 草稿语义：改草稿不动原值 ---- */
const orig = get('camParams["cam-01"].judge.pixelDelta');
run('draftSet("judge.pixelDelta", 60);');
ck("草稿·写入生效", get('draftGet("judge.pixelDelta")') === 60);
ck("草稿·原值未变", get('camParams["cam-01"].judge.pixelDelta') === orig);
run('draftSet("day.crf", 30);');
ck("草稿·支持嵌套路径", get('draftGet("day.crf")') === 30);

/* ---- 6. 保存落盘 ---- */
run('saveDraft();');
ck("保存·参数落盘", get('camParams["cam-01"].judge.pixelDelta') === 60);
ck("保存·嵌套落盘", get('camParams["cam-01"].day.crf') === 30);
ck("保存·退出编辑", get('SET.editing') === false);
ck("保存·回到只读", body().includes('data-editcam="1"'));

/* ---- 7. 恢复默认：只改草稿，不自动落盘 ---- */
run('openEditor("cam-01"); resetDraft();');
ck("默认·像素差门槛归位", get('draftGet("judge.pixelDelta")') === 24);
ck("默认·码流保留", get('draftGet("main")') === "rtsp://192.168.2.108:8554/ch1");
ck("默认·不自动落盘", get('camParams["cam-01"].judge.pixelDelta') === 60);

/* ---- 8. 画面区域：16×9 方块预选 ---- */
/* 上一段 resetDraft() 把 masks 重置成空的了；openEditor 在"已在编辑这一路"时
   会直接 return，不会重建 draft，所以先退出再进，拿到干净的草稿。 */
run('closeEditor();');
run('openEditor("cam-01"); SET.acc="mask"; renderCamTabs();');     // 编辑态才有画布
h = body();
ck("网格·16 列 9 行", get('GRID_COLS === 16 && GRID_ROWS === 9'));
ck("网格·只剩忽略区画布", h.includes("maskCv-ig") && !h.includes("maskCv-rd"));
ck("网格·标题只剩检测忽略区", h.includes("检测忽略区") && !h.includes("输出遮蔽区"));
ck("网格·旧的类型切换已去掉", !h.includes("data-mtool"));
const ig0 = get('maskArr("ignore").length');
run('maskArr("ignore").push(143);');
ck("网格·可追加", get('maskArr("ignore").length') === ig0 + 1);
run('maskArr("ignore").pop();');
ck("网格·可删除", get('maskArr("ignore").length') === ig0);
/* 输出遮蔽区整块下线：没有它的画布、也没有它的数据通道。
   这里的 DOM 是极简桩，innerHTML 不会真的重建节点，所以用源码断言。 */
ck("网格·遮蔽区画布已移除", !src.includes("maskCv-rd") && !src.includes("rdCnt"));
ck("网格·normMasks 只留 ignore",
   get('JSON.stringify(Object.keys(normMasks({ignore:[1,2],redact:[3,4]})))') === '["ignore"]');
run('renderMasks();');
ck("网格·列表可清空", document.getElementById("mlist").innerHTML.includes("data-clrmask"));
ck("网格·格号换算成矩形", JSON.stringify(get('rectCells(1,1,2,2)')) === "[17,18,33,34]");
// 单路分片长度：0 表示跟随通用设置。注意 saveDraft 会清空 draft，
// 所以先断言落盘结果，再单独验证一次"默认跟随"。
ck("分片·默认跟随通用设置", get('defaultCamParams().segSec') === 0);
run('SET.draft.segSec = 60;');
run('saveDraft();');
ck("分片·单路长度随保存落盘", get('camParams["cam-01"].segSec') === 60);
ck("网格·随保存落盘", get('normMasks(camParams["cam-01"].masks).ignore.length') > 0);
ck("网格·旧自由框可迁移", get('normMasks([{x:0,y:0,w:0.1,h:0.12,type:"ignore"}]).ignore.length') === 2);
run('closeEditor();');

/* ---- 9. 回放页摄像头下拉 ---- */
run('renderCamPick();');
const nCams = get('CAMERAS.length');
ck("下拉·条目数与摄像头一致", (pickHtml().match(/data-pickcam/g) || []).length === nCams);
ck("下拉·带通道号", pickHtml().includes("CH01"));
run('state.camId = CAMERAS[0].id; pickCam(CAMERAS[1].id);');
ck("下拉·切换生效", get('state.camId') === get('CAMERAS[1].id'));
ck("下拉·标题跟着变", document.getElementById("camSelName").textContent === get('CAMERAS[1].name'));
run('state.camId = CAMERAS[0].id;');

/* ---- 10. 默认保留天数 + 保留周期位置 ---- */
ck("默认·全时 3 天", get('defaultCamParams(CAMERAS[0],0).fullDays') === 3);
ck("默认·缩时 10 天", get('defaultCamParams(CAMERAS[0],0).clipDays') === 10);
run('state.camId="cam-01"; SET.open="cam-01"; SET.editing=false; SET.draft=null; renderCamTabs(); openEditor("cam-01");');
/* 0.9.24 起四段手风琴一次只渲染一段，DOM 里不可能同时出现"保留"和"缩时"，
   顺序改为对源码里的装配顺序断言 —— 这才是设计约束本身（名称→保留→缩时→忽略区）。 */
ck("布局·四段装配顺序固定", (()=>{
  const a = src.indexOf('camAcc("base", 1'), b = src.indexOf('camAcc("keep", 2'),
        c = src.indexOf('camAcc("condense", 3'), d = src.indexOf('camAcc("mask", 4');
  return a > 0 && a < b && b < c && c < d;
})());
ck("布局·保留段内含全时天数与分片", /保留与分片[\s\S]{0,1600}全时录像保留[\s\S]{0,1600}录制分片长度/.test(src));
ck("布局·缩时段在保留段之后（含总闸与倍速）", /缩时压制[\s\S]{0,900}data-cond[\s\S]{0,900}data-speed/.test(src));
ck("布局·忽略区独立成段", src.includes('camAcc("mask", 4')
   && src.indexOf('camAcc("mask", 4') > src.indexOf('data-speed'));
run('closeEditor(); SET.open="cam-01"; renderCamTabs();');
h = body();
ck("布局·只读态同样顺序", h.indexOf("保留周期") > h.indexOf("子码流 · 预览") && h.indexOf("保留周期") < h.indexOf("判读条件"));

/* ---- 11. 拖动排序 ---- */
/* 直接复用 harness 的内存版 localStorage：这里换一份新实现会把前一段
   （含 masks / segSec）的存档丢掉，后面的"持久化读回"就必然失败。 */
run('saveCfg();');                     // 先存一份原始顺序，排序后再读回来验证
ck("排序·有拖动手柄", h.includes("data-drag"));
const orderBefore = get('CAMERAS.map(c=>c.id)');
run('dragCam = { id:CAMERAS[0].id, row:{style:{}, classList:{remove(){}}}, rows:[], from:0, to:2, lastTo:2, hh:70, y0:0 }; finishCamDrag();');
const orderAfter = get('CAMERAS.map(c=>c.id)');
ck("排序·第一台移到第三位", orderAfter[2] === orderBefore[0] && orderAfter[0] === orderBefore[1]);
ck("排序·回放下拉跟着变", (pickHtml().match(/data-pickcam/g) || []).length === orderAfter.length
   && pickHtml().indexOf(orderAfter[0]) < pickHtml().indexOf(orderAfter[2]));
run('CAMERAS.length = 0; loadCfg();');   // 刷新（清空后从存档读回）
ck("排序·刷新后顺序不变", JSON.stringify(get('CAMERAS.map(c=>c.id)')) === JSON.stringify(orderAfter));

/* ---- 12. 配置持久化：删掉的摄像头不能再自己长回来 ---- */
run('saveCfg();');
const snapshot = get('CAMERAS.map(c=>c.id)');
run('CAMERAS.length = 0; Object.keys(camParams).forEach(k=>delete camParams[k]);');
run('loadCfg();');
ck("持久化·数量读回", get('CAMERAS.length') === snapshot.length);
ck("持久化·顺序读回", JSON.stringify(get('CAMERAS.map(c=>c.id)')) === JSON.stringify(snapshot));
ck("持久化·参数读回", get('camParams[CAMERAS[0].id].fullDays') === 3);
/* 注意：第 11 段把 cam-01 拖到了第三位，所以这里不能假设 CAMERAS[0] 还是 cam-01 */
ck("持久化·区域读回", get('normMasks(camParams["cam-01"].masks).ignore.length') > 0);
ck("持久化·单路分片长度读回", get('camParams["cam-01"].segSec') === 60);
run('delCamera(CAMERAS[0].id); saveCfg(); const n = CAMERAS.length; CAMERAS.length = 0; loadCfg();');
ck("持久化·删除后刷新不再复活", get('CAMERAS.length') === snapshot.length - 1);
ck("持久化·删掉那台确实没了", !get('CAMERAS.map(c=>c.id)').includes(snapshot[0]));

/* ---- 13. 增删摄像头：直播/回放同步 ---- */
const before = get('CAMERAS.length');
run('addCamera(); closeEditor();');
ck("添加·数量 +1", get('CAMERAS.length') === before + 1);
ck("添加·退出编辑", get('SET.editing') === false);
ck("添加·新行可见", body().includes("摄像头 " + (before + 1)));
run('renderCamPick();');
ck("添加·下拉同步", (pickHtml().match(/data-pickcam/g) || []).length === before + 1);
const goneId = get('CAMERAS[CAMERAS.length-1].id');
run('delCamera(CAMERAS[CAMERAS.length-1].id);');
ck("删除·数量复原", get('CAMERAS.length') === before);
ck("删除·下拉里不再出现", !pickHtml().includes(goneId));
ck("删除·设置列表里不再出现", !body().includes(goneId));
ck("删除·直播快照墙仍可渲染", (()=>{ try{ run('renderLive();'); return true; }catch(e){ return false; } })());

/* ---- 14. 删空后不崩 ---- */
run('CAMERAS.slice().forEach(c=>delCamera(c.id));');
ck("删空·列表为空", get('CAMERAS.length') === 0);
ck("删空·渲染不抛异常", (()=>{ try{ run('renderCamTabs();'); return body().includes("还没有摄像头"); }catch(e){ return false; } })());
ck("删空·回放页不崩", (()=>{ try{ run('renderDates(); renderOverview(); renderHeat(); renderClips(); renderCamPick(); renderLive();'); return true; }catch(e){ return false; } })());
ck("删空·下拉提示引导", pickHtml().includes("还没有摄像头"));

/* ---- 15. 诊断与日志（0.9.24 起独立成区，默认折叠，摘要在标题下） ---- */
run('SET.diagOpen=false; SET.genOpen=false; SET.editing=false; SET.draft=null; SET.open=null; renderCamTabs();');
h = body();
ck("诊断·折叠态只露一行摘要", h.includes('data-diagtoggle="1"') && h.includes("剩余")
   && !h.includes("diagLogText"));
ck("诊断·摘要含在录路数/剩余空间/异常数", src.includes('(on + "/" + ch.length + " 路在录")')
   && src.includes('" · 剩余 "') && src.includes('" 路异常"'));
ck("诊断·三个分区同时存在且顺序固定（通用 / 摄像头 / 诊断）",
   h.indexOf('class="zone gen"') < h.indexOf('class="zone cam"')
   && h.indexOf('class="zone cam"') < h.indexOf('class="zone diag"'));
run('SET.diagOpen=true; renderCamTabs();');
h = body();
ck("诊断·有日志查看框", h.includes("diagLogText"));
ck("诊断·刷新/复制/清除齐全", h.includes("btnLogRefresh") && h.includes("btnLogCopy") && h.includes("btnLogClear"));
ck("诊断·读/复制/清函数就绪",
   typeof get('loadDiagLog') === "function" && typeof get('copyDiagLog') === "function" && typeof get('clearDiagLog') === "function");
ck("诊断·旧版警告条 + 重启按钮", h.includes("diagWarn") && h.includes("btnRestart"));
ck("诊断·重启/环境自检函数就绪",
   typeof get('restartDiag') === "function" && typeof get('loadDiagEnv') === "function");
ck("全屏·切换函数就绪", typeof get('toggleFull') === "function");
ck("在线状态·pollLive 同步函数就绪", typeof get('pollLive') === "function");
ck("历史·导航函数就绪",
   typeof get('goPage') === "function" && typeof get('activatePage') === "function" && typeof get('activePage') === "function");

/* ---- 16. 四角状态层 + 实时流播放 + 缩时清单（0.9.10 / 0.9.24 / 0.9.25 改版）
   旧的「右上角倒计时圆点徽标」已下线：右上角让给时间戳。
   0.9.25：左上录制态徽标（lv-rec）也下线——摄像头画面自带的时间戳多在左上，
   徽标正好盖住它；状态收敛到右下「HomeKit 风格状态点 + 新鲜度」。 ---- */
ck("角标·时间戳在图右上", /\.livecard \.lv-osd\{position:absolute;right:8px;top:7px/.test(src));
ck("角标·左上不再放徽标（不盖画面自带时间戳）", !src.includes(".lv-rec"));
ck("角标·名称通道在图左下", /\.livecard \.lv-name\{position:absolute;left:8px;bottom:8px/.test(src));
ck("角标·状态位在图右下", /\.snap-ago\{position:absolute;right:10px;bottom:9px/.test(src));
ck("角标·状态点绿=正常/红=异常，带光晕", /\.snap-ago \.dot\{[^}]*#30d158/.test(src)
   && /\.snap-ago\.bad \.dot\{[^}]*#ff453a/.test(src)
   && /box-shadow:0 0 7px rgba\(48,209,88/.test(src)
   && /box-shadow:0 0 7px rgba\(255,69,58/.test(src));
ck("角标·重试态仍可点（红底胶囊）", src.includes(".snap-ago.retry"));
ck("角标·未成功过显示 -- 不拿现在顶上去", src.includes('(st.shownAt ? snapTime(st.shownAt) : "--:--:--")'));
ck("角标·实时流起不来时如实回落", src.includes('ago.textContent = "快照 · " + SNAP.interval + "s"')
   && src.includes('ago.textContent = "无画面 · 重试"'));
ck("角标·旧的倒计时徽标已下线", !src.includes("snap-cd") && !src.includes("Math.ceil(left)"));
ck("实时流·播放按钮在画面外的底栏", src.includes('id="lrPlay"') && src.includes('class="lr-play"')
   && typeof get('toggleLiveStream') === "function");
ck("实时流·旧的画面悬浮胶囊已移除", !src.includes("lr-float") && !src.includes("data-lrplay"));
ck("实时流·宫格也走流（不再是只拉快照）",
   src.includes("mountLiveStream(tile, c, grid)") && src.includes("STREAM_Q.grid"));
ck("实时流·流失败要回落快照",
   src.includes("img.onerror") && src.includes("实时流不可用 · 已回落快照")
   && src.includes("pullSnap(c.id, img"));
ck("实时流·服务端端点接线", src.includes("/api/live/stream/"));
ck("实时流·进入实时层默认播放", src.includes("state.liveStream = true"));
ck("回放·缩时清单通道", src.includes('realSegs(state.camId, curDay(), "clip")') && src.includes("rec.clips"));
ck("回放·录像清单防缓存", src.includes('"/api/recordings?_t=" + Date.now()'));
ck("设置·缩时存放位置提示", src.includes("_clip/&lt;摄像头&gt;/&lt;日期&gt;/"));

/* ---- 17. 资源栏随页面滚动 + 齿轮调刷新率 + 配置真的推上去（0.9.11） ---- */
ck("预览·资源栏与卡片同一滚动容器", src.includes('<div class="livescroll">')
   && /\.livescroll\{[^}]*overflow-y:auto/.test(src)
   && /\.livescroll \.livewrap\{flex:0 0 auto;overflow:visible/.test(src));
ck("预览·播放层不被滚动带走", src.includes('<div class="liveroom" id="liveRoom">')
   && !/#page-live\{[^}]*overflow-y:auto/.test(src));
ck("预览·齿轮弹出刷新率面板", src.includes('class="ratepop" id="ratePop"')
   && typeof get('toggleRatePop') === "function" && typeof get('renderRatePop') === "function");
ck("预览·刷新率 1~10 秒可选", src.includes("for(let n = 1; n <= 10; n++)")
   && src.includes("Math.max(1, Math.min(10, Math.round(+n) || 10))"));
ck("预览·刷新率落盘并推服务端", src.includes('localStorage.setItem("nvr.snapRate"')
   && src.includes("SNAP.interval = n;"));
ck("配置·分片长度随 pushCfg 上送", src.includes("record_segment_sec: LK.segSec")
   && src.includes("link_mode: LK.mode") && src.includes("link_interval_h: LK.intervalH"));
ck("配置·改分片立即保存并推送", /data-linkseg[\s\S]{0,320}saveCfgSoon\(\)/.test(src));
ck("配置·启动时以服务端为准", typeof get('syncFromServer') === "function"
   && /syncFromServer\(\)/.test(src) && src.includes('apiGet("/api/config")'));

/* ---- 18. 录制自愈与诊断（0.9.15）
   真机上五路全报 "Output file does not contain any stream"，页面只显示这句英文
   原文，用户完全无从下手。这一组守住三件事：说人话、能自证、别刷屏。 ---- */
ck("录制·每路有诊断按钮", src.includes('class="recprobe" data-probe="')
   && src.includes("forEach(el=>{\n    el.onclick = ()=> runProbe(el.dataset.probe, el);"));
ck("录制·诊断函数就绪", typeof get('runProbe') === "function"
   && typeof get('probeHtml') === "function");
ck("录制·诊断走独立端点", src.includes('"/api/record/probe?id="'));
ck("录制·失败时先说建议再说原文", src.includes('else if(hint) tail = hint;'));
ck("录制·ffmpeg 原文收进折叠区", src.includes('recmore"><summary>ffmpeg 原文</summary>'));
ck("录制·自动跳过的参数要如实显示", src.includes("rec.bad_opts && rec.bad_opts.length")
   && src.includes("本机 ffmpeg 不支持的参数已自动跳过"));
ck("录制·诊断结果展示编码与分辨率", src.includes("s.type, s.codec || \"?\", dim, s.fps"));
ck("录制·诊断结果展示 Stream mapping", src.includes('h += \'<div class="h">Stream mapping：\'')
   || src.includes("Stream mapping："));
ck("录制·诊断弹的是人话结论不是英文", src.includes("j.verdict")
   && !src.includes('box.innerHTML = j.trial_err'));

/* ---- 19. 0.9.18：目录锁定 / 端口默认 / 缩时时间轴 ---- */
ck("目录·默认共享目录", src.includes('dir:"/vol1/@appshare/nvr-output"') && !src.includes('/vol2/1000/NVRL'));
ck("目录·默认锁定 + 修改按钮", src.includes('id="btnDirEdit"') && src.includes('id="btnDirSave"')
   && src.includes("api/dir/check"));
ck("目录·折叠后不显示输入框", !src.includes('id="recDir"') || src.includes('id="dirEditBox"'));
ck("端口·UI 默认 18080", src.includes("port:18080"));
ck("缩时·每路开关与倍速推服务端", src.includes("condense: p.condense !== false")
   && src.includes("clip_speed: p.clipSpeed || 1"));
ck("缩时·时间轴用源时刻", src.includes("function segA(") && src.includes("function segB(")
   && src.includes("src: st, srcEnd: en"));
ck("缩时·播放器显示源时刻", src.includes("覆盖 ") && src.includes("hms(srcNow)"));

/* ---- 20. 0.9.22 判读引擎 / 0.9.24 收进「缩时压制 → 进阶判读参数」 ----
   前面的用例可能已经把摄像头删光了：新建一路再验证菜单（addCamera 直接进编辑态） */
run('addCamera(); SET.acc="condense"; SET.adv=true; renderCamTabs();');
h = body();
ck("判读·菜单标题改为判读条件", h.includes("进阶判读参数") && !h.includes("事件判定"));
ck("判读·常用段只露灵敏度、进阶才露引擎",
   h.indexOf('data-sens="') > 0 && h.indexOf('data-engine="') > h.indexOf('data-sens="'));
ck("判读·引擎三档", (h.match(/data-engine="(auto|pixel|bytes)"/g) || []).length === 3);
ck("判读·画面类型三档", (h.match(/data-scene="(auto|color|mono)"/g) || []).length === 3);
ck("判读·灵敏度三档", (h.match(/data-sens="(low|mid|high)"/g) || []).length === 3);
ck("判读·像素差门槛滑杆", h.includes('data-k="judge.pixelDelta"'));
ck("判读·最短事件滑杆", h.includes('data-k="judge.minEvent"'));
ck("判读·模糊半径滑杆", h.includes('data-k="judge.blur"'));
ck("判读·分析抽样帧滑杆", h.includes('data-k="judge.fps"'));
ck("判读·日夜采样间隔滑杆", h.includes('data-k="judge.probeSec"'));
ck("判读·静默合并滑杆", h.includes('data-k="judge.mergeGap"'));
ck("判读·默认标准灵敏度", get('JUDGE_DEFAULTS.sens') === "mid");
ck("判读·默认引擎自动", get('JUDGE_DEFAULTS.engine') === "auto");
ck("判读·推服务端字段 snake_case", (()=>{ const w = get('judgeWire({})');
   return w.pixel_delta === 24 && w.min_event === 2 && w.scene_probe_sec === 5
          && w.sens === "mid" && "engine" in w && "fps" in w; })());
ck("判读·服务端档案回读", (()=>{ const j = get('judgeFromProfile({sensitivity:"low", pixel_delta:40, min_event:4, blur_mono:2, fps:1.5, scene_probe_sec:8, merge_gap:9, scene:"mono", engine:"bytes"})');
   return j.sens === "low" && j.pixelDelta === 40 && j.minEvent === 4 && j.blur === 2
          && j.fps === 1.5 && j.probeSec === 8 && j.mergeGap === 9 && j.scene === "mono"
          && j.engine === "bytes"; })());
ck("判读·旧参数迁移（百分比→档位）", (()=>{ const j = get('normJudge({sensitivity:80}, {sensitivity:80, minKeep:12, mergeGap:30})');
   return j.sens === "high" && j.minEvent === 12 && j.mergeGap === 30; })());
/* 点引擎 chip 要真的改草稿（不是只改显示） */
run('draftSet("judge.engine","bytes"); renderCamTabs();');
ck("判读·切引擎进草稿", get('draftGet("judge.engine")') === "bytes");

ck("诊断·像素自检入口", src.includes("/api/diag/pixel") && src.includes('id="btnPix"'));

/* ---- 21. 0.9.23：回放页改版 + 导出当前文件 + 多日筛选修复 ---- */
ck("回放·双层时间轴元素齐全", src.includes('id="tStart"') && src.includes('id="fPos"')
   && src.includes('id="fDur"') && src.includes('class="pt-c"'));
ck("回放·按钮五键（去掉±10s）", src.includes('id="bSpeed"') && src.includes('id="bNext"')
   && src.includes('id="bLand"') && !src.includes('id="bBack"') && !src.includes('id="bFwd"'));
ck("回放·倍速档位 1/2/4/8/16/32", get('SPEEDS.join(",")') === "1,2,4,8,16,32");
ck("回放·全屏播放中隐藏控制", src.includes("fshide") && src.includes("state.land && state.playing"));
ck("回放·进度条触控优化（命中区+drag 反馈）", /touch-action:none/.test(src) && src.includes(".p-bar.drag .p-knob"));
ck("回放·上一段/下一段图标方向已修正", src.includes("M7 6v12M18 6l-8 6 8 6z")
   && src.includes("M17 6v12M6 6l8 6-8 6z"));
ck("导出·菜单与下载参数", src.includes('id="pmExport"') && src.includes('id="pmCopy"')
   && src.includes("&dl=1"));

/* 多日筛选：日期顺序只有一种（旧→新全量），选中跟日期字符串走。
   0.9.22 及之前 recalcDayList 排新→旧、renderDates 排旧→新截 7 天，
   dayIdx 在两套顺序里指向不同的日子 —— 多日数据下点哪天加载的都是别的天。 */
run('var tc = CAMERAS[0].id; state.camId = tc;'
  + 'state.rec = {segment_sec:300, cameras:{}, clips:{}};'
  + 'state.rec.cameras[tc] = {"2026-10-20":[{n:"08-00-00.mp4",b:1024},{n:"08-05-00.mp4",b:1024}],'
  + '"2026-10-21":[{n:"09-00-00.mp4",b:1024}],"2026-10-22":[{n:"10-00-00.mp4",b:1024}]};'
  + 'state.daySel = "2026-10-21"; recalcDayList();');
ck("多日·dayList 旧→新全量", get('state.dayList.join(",")') === "2026-10-20,2026-10-21,2026-10-22");
ck("多日·daySel 命中原选中日（不串天）", get('state.dayIdx') === 1 && get('curDay()') === "2026-10-21");
run('state.daySel = null; recalcDayList();');
ck("多日·未选时默认最新一天", get('state.dayIdx') === 2 && get('curDay()') === "2026-10-22");
ck("多日·dayList 赋值不再 reverse/slice(-7)",
   !/dayList\s*=[^;]*reverse\(\)/.test(src) && !/dayList\s*=[^;]*slice\(-7\)/.test(src));

/* 筛选时段计数：第 x/y 段按 visibleSegs，上一段/下一段只在时段内切换 */
run('state.segs = ['
  + '{s:0,e:10,clip:true,src:36000,srcEnd:36300,reason:"缩时片段"},'
  + '{s:10,e:20,clip:true,src:39600,srcEnd:39900,reason:"缩时片段"},'
  + '{s:20,e:30,clip:true,src:43200,srcEnd:43500,reason:"缩时片段"}];');
run('state.hourBlock = 5; state.segIdx = 1; setSegTitle(1);');
ck("筛选·标题只计时段内段数（10:00–12:00 共 2 段）",
   document.getElementById("pTitle").textContent.includes("第 2/2 段"));
ck("筛选·播放顺序只在时段内", get('playOrder().join(",")') === "0,1");
run('state.hourBlock = -1; setSegTitle(2);');
ck("全天·标题计全量", document.getElementById("pTitle").textContent.includes("第 3/3 段"));
run('state.t = 12; state.playing = false; syncPlayer();');
ck("副标题·不再重复段号", (()=>{ const t = document.getElementById("pSub").textContent;
   return t.includes("覆盖 11:00:00") && !t.includes("段"); })());
ck("下行·文件进度 mm:ss", document.getElementById("fPos").textContent === "00:02"
   && document.getElementById("fDur").textContent === "00:10");
ck("上行·起始为源钟点", document.getElementById("tStart").textContent === "11:00:00");

/* ---- 22. 0.9.24：诊断区的存储占用（非实时 + 无灰色剩余段） ---- */
ck("字节格式化·TB/GB/MB 自适应", get('fmtBytes(1536*1024*1024)') === "1.5 GB"
   && get('fmtBytes(0)') === "0 B" && get('fmtBytes(2048)') === "2 KB");
ck("存储·统计只按目录汇总，不读文件内容", src.includes("只扫目录元数据，不读文件内容"));
ck("存储·前端只信服务端给的统计时刻（TTL 在后端）", src.includes("STORAGE.at = +d.at"));
ck("存储·失败后不自动重试（防渲染环）", src.includes("if(!force && STORAGE.err) return Promise.resolve(null)"));
ck("存储·只有诊断区展开才去统计", src.includes("if(SET.diagOpen) loadStorageUsage(false)"));
ck("存储·手动重算走独立端点", src.includes('"/api/storage/usage/refresh"') && src.includes('id="btnStRefresh"'));
run('STORAGE.data = { ok:true, at:' + Math.floor(Date.now()/1000) + ','
  + ' root:"/vol1/@appshare/nvr-output", full:6.4e9, clip:1.1e9,'
  + ' disk_free_gb:820, disk_total_gb:1860,'
  + ' cameras:[{id:"a",name:"车库入口",full:3.2e9,clip:0.5e9,total:3.7e9},'
  + '          {id:"b",name:"后院",full:3.2e9,clip:0.6e9,total:3.8e9}] };');
const stc = get('cardStorage()');
ck("存储·总量条无灰色「其余可用」段（容量共享，无意义）", !stc.includes("其余可用")
   && stc.includes('class="stbar-big"') && !/stbar-big[\s\S]{0,220}class="r"/.test(stc));
ck("存储·总条只由全时+缩时两段填满", (()=>{ const m = stc.match(/stbar-big">([\s\S]*?)<\/div>/);
   return m && (m[1].match(/<i /g) || []).length === 2; })());
ck("存储·图例给百分比", stc.includes("全时 <b>") && stc.includes("缩时 <b>") && stc.includes("%</b>"));
ck("存储·每路按自身总量归满格（行按总量降序）",
   stc.indexOf("后院") < stc.indexOf("车库入口")
   && /class="stbar"><i class="f" style="width:\d+%"><\/i><i class="c" style="width:\d+%">/.test(stc));
ck("存储·每路显示合计 + 全时/缩时明细", stc.includes("合计 · 全时 / 缩时")
   && stc.includes("3.5 GB") && stc.includes("3.0 GB / 572 MB"));
ck("存储·标注非实时与统计时刻", stc.includes("非实时") && stc.includes("每 30 分钟最多重算一次")
   && stc.includes("统计于 "));
ck("存储·无可用容量段时该路显示无录像", (()=>{ run('STORAGE.data.cameras.push({id:"c",name:"空机位",full:0,clip:0,total:0});');
   return get('cardStorage()').includes("无录像"); })());
run('STORAGE.data = null; STORAGE.err = "连接超时";');
const stErr = get('cardStorage()');
ck("存储·失败给人话 + 重算入口", stErr.includes("统计失败") && stErr.includes("立即重算"));
run('STORAGE.data = null; STORAGE.err = "";');

/* ---- 23. 0.9.24：直播首页（单双列 / 连续播放 / 四角状态 / 单路全屏） ---- */
ck("列数·按钮默认可用且落盘", src.includes('const LIVE_COLS_KEY = "nvr.live.cols"')
   && src.includes("localStorage.setItem(LIVE_COLS_KEY"));
run('setLiveCols(2);');
ck("列数·切双列写入存档", get('LIVE_COLS') === 2 && get('localStorage.getItem("nvr.live.cols")') === "2");
run('setLiveCols(1);');
ck("列数·切回单列", get('LIVE_COLS') === 1 && get('localStorage.getItem("nvr.live.cols")') === "1");
ck("列数·只按存档读回（刷新保持）", src.includes('localStorage.getItem(LIVE_COLS_KEY) === "2" ? 2 : 1'));
ck("列数·只有一路时藏起按钮", src.includes("CAMERAS.length > 1"));
ck("列数·与刷新率弹层互斥", src.includes('const rp = document.getElementById("ratePop");\n  if(rp) rp.classList.remove("on");'));

ck("连续播放·按钮不是跳转（原地换流）", src.includes("state.wallStream = true;")
   && src.includes("mountLiveStream(d, c, true)"));
ck("连续播放·轮询期间不重建卡片", /if\(state\.wallStream\) updateSnapLabels\(\);\s*\n\s*else renderLive\(\);/.test(src));
run('state.wallStream = true; state.wallAt = Date.now(); syncWallBar();');
ck("连续播放·按钮变「回到快照」", document.getElementById("wallTxt").textContent === "回到快照"
   && /实时 · 已播 /.test(document.getElementById("wallStat").innerHTML));
run('state.wallStream = false; syncWallBar();');
ck("回到快照·按钮文案还原", /^连续播放/.test(document.getElementById("wallTxt").textContent));
ck("连续播放·快照续图对正在播的卡片让路",
   src.includes('if(state.wallStream && card.classList.contains("streaming") && !card.classList.contains("fallback")) return;'));

/* 右下角状态点（0.9.25）：绿=正常 / 红=异常；连续播放只留一颗裸点 */
ck("状态点·卡片右下角带 dot 元素", src.includes('<i class="dot"></i><span data-ago-txt>'));
ck("状态点·异常判定换成 bad 类", src.includes('box.classList.toggle("bad", bad || card.classList.contains("fallback"))')
   && !src.includes('classList.toggle("stale"'));
ck("状态点·连续播放中的卡片不按快照口径误判异常",
   src.includes("const live = card.classList.contains(\"streaming\") && !card.classList.contains(\"fallback\");")
   && src.includes("const bad = live ? false :"));
ck("状态点·连续播放时胶囊缩成裸点（倒数文字隐藏）",
   /\.livecard\.streaming \.snap-ago\{background:transparent/.test(src)
   && src.includes(".livecard.streaming .snap-ago span{display:none}"));
ck("状态点·停用/未配置仍由覆盖层如实说明",
   src.includes('已停用 · 去设置里启用') && src.includes('未配置 RTSP 地址'));

/* 单路全屏横屏：复用全屏 API，退出全屏回到快照墙 */
ck("单路·点快照进全屏", src.includes("function openLiveFull(idx)")
   && src.includes("state.lrFs = true") && src.includes('room.classList.add("fs")'));
ck("单路·不触发 Fullscreen 二次退出", src.includes("enterFull(null, { noExit: true })"));
ck("单路·全屏画面吃满视口不再超界（瓦片不锁 16:9）",
   /\.liveroom\.fs \.lr-tile\{border-radius:0;aspect-ratio:auto;height:100%\}/.test(src)
   && src.includes(".liveroom.fs .lr-body{position:absolute;inset:0;padding:0;align-content:stretch;gap:0;\n  grid-template-rows:100%}"));
ck("单路·全屏画面 contain 完整呈现（与回放页同口径，超出留黑边）",
   src.includes(".liveroom.fs .lr-tile .snap-img{object-fit:contain}"));
ck("单路·控制条浮现后自动收起", /syncLiveChrome\._h = setTimeout\(\(\)=>\{ state\.lrChrome = false; syncLiveChrome\(\); \}, 2600\)/.test(src));
ck("单路·暂停时画面中央给播放键", /\.liveroom\.paused \.lr-center/.test(src) && src.includes('id="lrBigPlay"'));
run('state.lrFs = true; state.liveStream = true; state.liveView = "single"; closeLiveRoom();');
ck("单路·关播放层时状态全归位", get('state.lrFs') === false && get('state.lrChrome') === true
   && get('state.liveStream') === false && get('state.liveView') === "snap");
ck("单路·退出时主动断 MJPEG", src.includes('img.removeAttribute("src")')
   && src.includes('document.getElementById("lrBody").innerHTML = ""'));

/* ---- 24. 0.9.24：防误操作（底部常驻改动数 / 已调 N 项 / 删除两段确认） ---- */
run('closeEditor(); openEditor(CAMERAS[0].id); SET.acc="condense"; renderCamTabs();');
ck("防误·未改动时写「无改动」", body().includes("无改动"));
run('draftSet("judge.pixelDelta", 50); renderCamTabs();');
ck("防误·底部常驻「已改 N 项未保存」", get('dirtyCount()') === 1
   && body().includes("已改 1 项未保存"));
ck("防误·进阶判读标题标「已调 1 项」", body().includes("已调 1 项"));
run('draftSet("judge.pixelDelta", 24); renderCamTabs();');
ck("防误·改回默认后计数归零", get('dirtyCount()') === 0);
run('SET.armed = CAMERAS[0].id; SET.acc="base"; renderCamTabs();');
ck("防误·删除要再点一次确认", body().includes("再点一次确认删除") && body().includes("delbtn armed"));
run('SET.armed = null; renderCamTabs();');
ck("防误·未确认时按钮是普通文案", body().includes("删除这个摄像头") && !body().includes("delbtn armed"));
run('closeEditor();');

/* ---- 25. 0.9.25：跨设备 RTSP 地址保护 ----
   明文只存"配置它的那台浏览器"的 localStorage；换设备/清缓存后本地没有明文。
   以前任何一次设置推送都带着 url:"" 去覆盖 NAS 真地址 —— 名字/掩码还在，
   但提示「未配置 RTSP 地址」。守三点：显示不误报、推送不覆盖、显式清空才真清。 */
ck("跨设备·服务端回传带 hasUrl/hasSub 标记",
   src.includes("hasUrl: !!c.url, hasSub: !!c.sub_url"));
run('applyServerCams([{id:"srv-01",name:"车库",url:"rtsp://***@192.168.2.108:554/ch1",sub_url:""}]);');
ck("跨设备·脱敏回传也能识别出「NAS 已存地址」",
   get('CAMERAS.some(c=>c.id==="srv-01" && c.hasUrl===true && c.hasSub===false)'));
ck("跨设备·本机无明文不冒充未配置（就绪判定认 NAS 地址）",
   src.includes("const ready = !!((p.main || c.hasUrl) && p.enabled !== false);"));
ck("跨设备·NAS 与本机都没有才提示未配置",
   src.includes('(ready ? "等待第一帧…" : (p.enabled === false ? "已停用 · 去设置里启用" : "未配置 RTSP 地址"))'));
ck("跨设备·连续播放计数认 NAS 地址",
   src.includes("(p.main || c.hasUrl) && p.enabled !== false"));
ck("跨设备·推送带显式清空标记，空值不再覆盖",
   src.includes("url_clear: !!p.urlClear, sub_clear: !!p.subClear"));
ck("跨设备·清空意图只在本地曾有明文且被清空时打标",
   src.includes("dst.urlClear = !!(dst.main && !SET.draft.main)")
   && src.includes("dst.subClear = !!(dst.sub && !SET.draft.sub)"));
ck("跨设备·编辑页如实提示地址存于 NAS",
   src.includes("已保存在 NAS（本机不显示明文）· 留空 = 不修改"));
ck("跨设备·只读页提示明文存于 NAS", src.includes("已保存在 NAS · 本机不显示明文"));

/* ---- 26. 0.9.27：实时时刻跟钟走、断流冻结 + 全屏信息去重 ---- */
ck("时刻·流状态有 live/frozen 字段",
   src.includes("live:false, frozen:0"));
ck("时刻·流成功标记存活并清冻结", /img\.onload = \(\)=>\{[^}]*st\.live = true; st\.frozen = 0;/.test(src));
ck("时刻·断流记下断流时刻", src.includes("if(!st.frozen) st.frozen = Date.now();"));
ck("时刻·秒级同步函数就绪", typeof get("syncLiveStamps") === "function"
   && src.includes("s.textContent = st.live ? nowStr : (st.frozen ? snapTime(st.frozen) : \"--:--:--\")"));
ck("时刻·断流且快照未续上停在断流时刻", src.includes("s.textContent = snapTime(st.frozen);"));
ck("时刻·快照墙 tick 与播放层 tick 都接了同步",
   src.includes("updateSnapLabels();\n  syncLiveStamps();")
   && src.includes("syncLiveStamps();   /* 流活着跟钟走；断流/暂停冻结，不再无脑写当前时刻 */"));
ck("全屏·四角布局：名字+通道在左下（与快照卡一致）",
   src.includes(".liveroom.fs .lr-tile .tn{left:16px;bottom:16px;font-size:12.5px")
   && src.includes("'<div class=\"tn\">' + esc(c.name) + '<em>' + esc(c.ch) + '</em></div>'"));
ck("全屏·顶栏名字/状态行撤掉，只留退出", src.includes(".liveroom.fs .lr-title{display:none}")
   && src.includes(".liveroom.fs .lr-fsbtn span{display:none}"));
ck("全屏·退出只留顶栏（底栏按钮去重）", src.includes(".liveroom.fs #lrExitBot{display:none}"));
ck("全屏·时刻只留画面右上且与退出按钮同行",
   src.includes(".liveroom.fs #lrClock{display:none}")
   && /\.liveroom\.fs \.lr-tile \.tf\{top:18px;right:70px;font-size:12\.5px/.test(src));
ck("全屏·右下状态点与快照卡同一套视觉且加大",
   src.includes(".liveroom.fs .lr-tile .snap-ago{right:16px;bottom:16px;height:26px")
   && src.includes("'<div class=\"snap-ago\" data-lago><i class=\"dot\"></i><span data-lago-txt></span></div>'"));
ck("全屏·状态点出图前隐藏（nophoto 负责说明）",
   src.includes(".lr-tile .snap-ago{visibility:hidden}")
   && src.includes(".lr-tile.hasphoto .snap-ago{visibility:visible}"));
ck("全屏·状态点口径与卡片一致且在瓦片 tick 里驱动",
   src.includes("const bad = live ? false : (!c.online || st.fails > 0 || age > SNAP.interval * 2.2);\n    box.classList.toggle(\"bad\", bad || tile.classList.contains(\"fallback\"));"));
ck("全屏·异常胶囊可点且不触发画面点击",
   src.includes("lago.onclick = (e)=>{") && src.includes("e.stopPropagation();")
   && src.includes('mountLiveStream(tile, c, state.liveView === "grid")'));
ck("全屏·状态行不再重复名字（syncLiveSub 按 fs 裁剪）",
   src.includes("function syncLiveSub()") && src.includes("+ (state.lrFs ? \"\" : '<span class=\"sep\">|</span><span class=\"mono\">'"));
ck("全屏·进出全屏都刷新状态行", src.includes("syncLiveSub();                      /* 全屏形态下状态行少一段，重刷 */"));

/* ---- 27. 0.9.34：播放页分布时间轴（跟随选中时段 · 全宽色条 · 大触区跳播） ---- */
ck("时间轴·结构与函数就绪", src.includes('id="tlane"')
   && typeof get("renderTl") === "function" && typeof get("unionLen") === "function"
   && typeof get("mergeBlocks") === "function" && typeof get("tlPlay") === "function");
/* 回归（0.9.30 真机 bug）：容器必须是空架子——早退分支 innerHTML="" 会清掉
   静态子元素，之后 renderTl 再引用它们就是 null 崩溃，loadDay 中途夭折。
   标题行必须每次随 innerHTML 重建。 */
ck("时间轴·容器无静态子元素（防 innerHTML 自毁）",
   src.includes('<div class="tlane p-tl" id="tlane" style="display:none"></div>'));
ck("时间轴·标题行随渲染重建，不引用静态节点",
   src.includes('<div class="tl-head"><b>')
   && !src.includes('getElementById("tlTitle")')
   && !src.includes('getElementById("tlBack")'));
ck("时间轴·并集去重：重叠区间只记一次", get("unionLen([[0,100],[50,150],[200,260]], 0, 300)") === 210);
ck("时间轴·并集裁剪到窗口外不算", get("unionLen([[10,20],[250,400]], 0, 300)") === 60);
ck("时间轴·空清单返回 0", get("unionLen([], 0, 100)") === 0);
ck("时间轴·相邻分片合并为一块（缝 ≤1s）",
   JSON.stringify(get("mergeBlocks([[0,100],[100.5,200],[300,400]], 0, 86400)")) === "[[0,200],[300,400]]");
ck("时间轴·真空档不合并不跨记",
   JSON.stringify(get("mergeBlocks([[0,100],[200,300]], 0, 86400)")) === "[[0,100],[200,300]]");
ck("时间轴·密度分档：密=1/中=0.62/疏=0.34",
   get("tlDens(0.10)") === 1 && get("tlDens(0.03)") === 0.62 && get("tlDens(0.01)") === 0.34);

/* 数据：10:00–12:00 两段全时（连续无缝）+ 两个缩时片（源覆盖各 1 小时，
   播放时长 20s / 30s）→ 全天视角下全时 7200s 无断录，缩时看完 50s、压缩 144× */
run('state.camId="cam-01"; state.dayIdx=0; state.dayList=["2026-10-22"];'
  + 'state.rec={segment_sec:3600,'
  + 'cameras:{"cam-01":{"2026-10-22":[{n:"10-00-00.mp4",b:1048576},{n:"11-00-00.mp4",b:1048576}]}},'
  + 'clips:{"cam-01":{"2026-10-22":[{n:"10-00-00.mp4",b:4096,d:20},{n:"11-00-00.mp4",b:4096,d:30}]}}};'
  + 'state.hourBlock=-1; renderTl();');
let rc = get('document.getElementById("tlane").innerHTML');
ck("时间轴·显示", get('document.getElementById("tlane").style.display') === "block");
ck("时间轴·默认「全天分布」", rc.includes("全天分布"));
ck("时间轴·全天态提示「点色块选中时段」", rc.includes("点色块选中时段") && !rc.includes("点色块跳播"));

/* 0.9.36 顶部热力图 ↔ 底部 12 格双向联动 */
ck("联动·全天态点色块选中所在 2 小时格（源码含选格逻辑）",
   src.includes("const blk = Math.min(11, Math.max(0, Math.floor(t/7200)));")
   && src.includes("state.hourBlock = blk;")
   && src.includes("renderPlayerHeat();    /* 底部 12 格选中描边 */"));
ck("联动·放大态头部有「‹ 全天」返回钮",
   src.includes('<span class="tl-all" data-tlall')
   && src.includes("if(all) all.onclick = ()=>{"));
ck("联动·放大态提示切回「点色块跳播」", (()=>{
  run('state.hourBlock=5; renderTl();');
  const z = get('document.getElementById("tlane").innerHTML');
  run('state.hourBlock=-1; renderTl();');
  return z.includes("点色块跳播") && z.includes(">‹ 全天</span>");
})());
ck("时间轴·说明行在色条上方（tl-cap 结构，色条独占全宽）",
   rc.includes('<div class="tl-cap"><span class="tl-lbl">')
   && rc.includes('</span></div><span class="tl-track" data-tlkind='));
ck("时间轴·只剩全时/缩时两行（真实时段行下线）",
   rc.includes("全时录像") && rc.includes("缩时录像") && !rc.includes("真实时段"));
ck("时间轴·无「回到全天」", !rc.includes("回到全天"));
ck("时间轴·全时 2 时 0 分 · 无断录", rc.includes("2 时 0 分 · 无断录"));
ck("时间轴·缩时看完仅需 50 秒", rc.includes("50 秒"));
ck("时间轴·压缩倍率 144×", rc.includes("压缩 ≈144.0×"));
ck("时间轴·24 小时刻度（00…24）", rc.includes("<span>00</span>") && rc.includes("<span>24</span>"));
ck("时间轴·全时块绿色 / 缩时块琥珀带密度透明度",
   rc.includes("background:var(--keep)")
   && rc.includes("background:var(--warn);opacity:0.34"));
/* 0.9.34：选中热力格（hourBlock=5 → 10:00–12:00）→ 时间轴放大到该窗口，
   块位置按窗口折算，摘要改覆盖率口径 */
run('state.hourBlock=5; renderTl();');
let rz = get('document.getElementById("tlane").innerHTML');
ck("时间轴·选中时段后标题带窗口钟点", rz.includes("10:00 – 12:00 分布"));
ck("时间轴·放大后 30 分钟刻度（10:00…12:00）",
   rz.includes("<span>10:00</span>") && rz.includes("<span>12:00</span>"));
ck("时间轴·放大后全时按覆盖率", rz.includes("覆盖 100%"));
ck("时间轴·块位置按窗口折算（10:00 → left:0.00%）", rz.includes("left:0.00%"));
ck("时间轴·取消选中回到全天", (()=>{
  run('state.hourBlock=-1; renderTl();');
  return get('document.getElementById("tlane").innerHTML') === rc;
})());
ck("时间轴·色条贯穿全宽、加高方便点按",
   src.includes(".p-tl{flex:0 0 auto;margin:2px 0 4px;padding:6px 12px 2px")
   && src.includes(".p-tl .tl-track{height:20px}")
   && src.includes(".tl-track{flex:1;height:18px"));
/* 密度分档映射到块：疏 0.34 / 中 0.62 / 密 1（d = 播放秒/源秒） */
run('state.rec={segment_sec:3600,'
  + 'cameras:{"cam-01":{"2026-10-22":[{n:"10-00-00.mp4",b:1048576}]}},'
  + 'clips:{"cam-01":{"2026-10-22":[{n:"10-00-00.mp4",b:4096,d:100},{n:"11-00-00.mp4",b:4096,d:300}]}}};'
  + 'renderTl();');
rc = get('document.getElementById("tlane").innerHTML');
ck("时间轴·密度深浅上轴（0.62 与 1 档）",
   rc.includes("opacity:0.62") && rc.includes("opacity:1"));
ck("时间轴·色块按 track 委托跳播（full/clip 各一条）",
   (rc.match(/data-tlkind="full"/g) || []).length === 1
   && (rc.match(/data-tlkind="clip"/g) || []).length === 1
   && src.includes("el.querySelectorAll(\"[data-tlkind]\").forEach(tk=>{"));

/* 断录统计：两段全时（10–11、12–13），中间 11–12 是真空档 */
run('state.rec={segment_sec:3600,'
  + 'cameras:{"cam-01":{"2026-10-22":[{n:"10-00-00.mp4",b:1048576},{n:"12-00-00.mp4",b:1048576}]}},'
  + 'clips:{"cam-01":{}}}; renderTl();');
rc = get('document.getElementById("tlane").innerHTML');
ck("时间轴·断录段数进摘要", rc.includes("断录 1 段"));
ck("时间轴·缺缩时显示「无缩时片段」", rc.includes("无缩时片段"));

/* 全天视角下的缩时折算：09:30 起源跨 1 小时、播放 60s → 覆盖 3600s、
   观看 60s、压缩 60×，块从 39.58% 处起画 */
run('state.rec={segment_sec:3600,'
  + 'cameras:{"cam-01":{"2026-10-22":[{n:"10-00-00.mp4",b:1048576}]}},'
  + 'clips:{"cam-01":{"2026-10-22":[{n:"09-30-00.mp4",b:4096,d:60}]}}};'
  + 'renderTl();');
rc = get('document.getElementById("tlane").innerHTML');
ck("时间轴·缩时折算观看 1 分 0 秒 · 压缩 60×", rc.includes("1 分 0 秒") && rc.includes("压缩 ≈60.0×"));
ck("时间轴·块按源时刻落位（09:30 → left:39.58%）", rc.includes("left:39.58%"));

/* 缺数据如实显示，不冒充 */
run('state.rec={segment_sec:3600,'
  + 'cameras:{"cam-01":{}},'
  + 'clips:{"cam-01":{"2026-10-22":[{n:"10-00-00.mp4",b:4096,d:20}]}}};'
  + 'renderTl();');
rc = get('document.getElementById("tlane").innerHTML');
ck("时间轴·缺全时显示「无录像」（有缩时仍显示）", rc.includes("全时录像") && rc.includes("无录像"));

/* 全天无任何录像 → 整块隐藏 */
run('state.rec={segment_sec:3600, cameras:{"cam-01":{}}, clips:{"cam-01":{}}}; renderTl();');
ck("时间轴·无任何录像整块隐藏", get('document.getElementById("tlane").style.display') === "none");

/* 0.9.33 位置与形态：常驻播放器画面上方（p-top 之后、stage 之前），黑底适配 */
ck("时间轴·组件在播放器内、画面上方",
   src.indexOf('class="tlane p-tl" id="tlane"') >= 0
   && src.indexOf('class="tlane p-tl" id="tlane"') < src.indexOf('<div class="stage" id="stage">'));
ck("时间轴·播放器黑底适配",
   src.includes(".p-tl .tl-track{background:rgba(255,255,255,.10)}")
   && src.includes(".p-tl .tl-lbl{color:rgba(255,255,255,.45)}"));
ck("时间轴·横屏/全屏整块隐藏",
   src.includes(".phone.land .p-tl{display:none}")
   && src.includes(".player.fshide .p-tl{opacity:0;pointer-events:none}"));
ck("时间轴·openPlayer 接线（打开/切段刷新）",
   src.includes("renderPlayerHeat();") && /renderPlayerHeat\(\);[\s\S]{0,80}?renderTl\(\);/.test(src));
ck("时间轴·renderHeat 末尾接线联动", /renderTl\(\);\n\}/.test(src));

/* ---- 27b. 0.9.33 回放页排版精简：CH 行并入 + 概览四格下线 ---- */
ck("排版·CH/文件数并入摄像头名行（HTML）",
   src.includes('<span class="hdr-sub" id="hdrSub">CH01 · 13 个录像文件</span>')
   && !src.includes('<div class="hdr-sub" id="hdrSub">'));
ck("排版·并入行小字弱化样式", src.includes(".camsel .hdr-sub{font-weight:400"));
ck("排版·renderOverview 只剩名字 + CH/文件数",
   src.includes('cam.ch + " · " + state.segs.length + " 个录像文件"')
   && !src.includes("ovKeep") && !src.includes("ovPct")
   && !src.includes("ovCnt") && !src.includes("ovSize"));
ck("排版·概览四格 HTML/CSS 下线",
   !src.includes('class="overview"') && !src.includes(".overview{")
   && !src.includes("保留时长</div>"));

/* ---- 28. 0.9.31：播放页内嵌热力格（画面下方 · 点格跳播 · 横屏隐藏） ---- */
ck("播放热力·结构与函数就绪", src.includes('id="pHeat"')
   && typeof get("renderPlayerHeat") === "function" && typeof get("pHeatJump") === "function");
ck("播放热力·横屏全屏态整块隐藏", src.includes(".phone.land .p-heat{display:none}")
   && src.includes(".player.fshide .p-heat{opacity:0;pointer-events:none}"));
ck("播放热力·openPlayer 内接线（切段自动跟随）",
   src.includes("setSegTitle(i);\n  renderPlayerHeat();"));

/* 全天活动分布：当前播 12:00 起的片段（segIdx=2，落在第 6 格 12:00–14:00） */
run('state.camId="cam-01"; state.dayIdx=0; state.dayList=["2026-10-22"];'
  + 'state.pmode="full"; state.segIdx=2;'
  + 'state.rec={segment_sec:3600,'
  + 'cameras:{"cam-01":{"2026-10-22":[{n:"10-00-00.mp4",b:1},{n:"11-00-00.mp4",b:1},{n:"12-00-00.mp4",b:1},{n:"20-00-00.mp4",b:1}]}},'
  + 'clips:{"cam-01":{}}};'
  + 'state.activity = realSegs("cam-01","2026-10-22","full") || [];'
  + 'state.segs = state.activity; renderPlayerHeat();');
let ph = get('document.getElementById("pHeat").innerHTML');
ck("播放热力·有活动才显示", get('document.getElementById("pHeat").style.display') === "block");
ck("播放热力·标题提示选时段看分布", ph.includes("点格子选中时段 · 上方显示该时段分布"));
ck("播放热力·选中格琥珀描边样式", src.includes(".ph-heat .phcell.sel{box-shadow:0 0 0 2px var(--warn)")
   && src.includes(".ph-heat .phcell.sel.on{box-shadow:0 0 0 2px var(--warn),0 0 0 4px"));
ck("播放热力·12 格齐全", (ph.match(/data-ph="/g) || []).length === 12);
ck("播放热力·当前块高亮（第 6 格 12:00–14:00）", /class="phcell on" data-ph="6"/.test(ph));
ck("播放热力·深色配色（活动绿 / 空格灰）",
   ph.includes("rgba(53,201,140,") && ph.includes("rgba(255,255,255,.12)"));
ck("播放热力·24 小时刻度", ph.includes("<span>00</span>") && ph.includes("<span>24</span>"));

/* 无活动 → 整块隐藏，不摆空架子 */
run('state.activity = []; renderPlayerHeat();');
ck("播放热力·无活动整块隐藏", get('document.getElementById("pHeat").style.display') === "none"
   && get('document.getElementById("pHeat").innerHTML') === "");

/* 跳播：当前模式有片段 → 原模式跳；没有 → 自动切另一种模式 */
run('state.activity = realSegs("cam-01","2026-10-22","full") || [];'
  + 'state.segs = state.activity; state.segIdx=0; renderPlayerHeat();');
let hit = get('(function(){ const a=6*7200; const o=state.segs.filter(s=>segA(s)<a+7200&&segB(s)>a).sort((p,q)=>segA(p)-segA(q))[0]; return o?Math.max(segA(o),a):null; })()');
ck("播放热力·跳播定位到时段内首个片段起点", hit === 43200);
ck("播放热力·跳播复用 tlPlay（切模式+定位一体）",
   src.includes("if(has(state.segs)) return tlPlay(state.pmode, firstT(state.segs));")
   && src.includes("if(has(alt)) return tlPlay(other, firstT(alt));"));
ck("播放热力·两模式都没有如实提示",
   src.includes('toast("toast", "该时段没有录像");'));

/* 0.9.35：底部热力格 = 时段选择器——点格选中 → 时间轴放大到该窗口；
   再点同一格取消回全天。空时段也照常选中（分布如实显示为空）。 */
run('state.activity = realSegs("cam-01","2026-10-22","full") || [];'
  + 'state.segs = state.activity; state.segIdx=0; state.hourBlock=-1;'
  + 'renderPlayerHeat();');
run('pHeatJump(5);');   /* 点第 5 格（10:00–12:00 有录像）→ 选中 + 跳播 */
ck("选时段·点格后 hourBlock 选中", get("state.hourBlock") === 5);
ph = get('document.getElementById("pHeat").innerHTML');
ck("选时段·选中格带琥珀描边类", /phcell[^"]*sel[^"]*" data-ph="5"/.test(ph));
let tzh = get('document.getElementById("tlane").innerHTML');
ck("选时段·时间轴放大到 10:00–12:00", tzh.includes("10:00 – 12:00 分布"));
ck("选时段·时间轴 30 分钟刻度", tzh.includes("<span>10:30</span>"));
ck("选时段·点同一格取消选中回全天", (()=>{
  run('pHeatJump(5);');
  return get("state.hourBlock") === -1
    && get('document.getElementById("tlane").innerHTML').includes("全天分布");
})());
ck("选时段·点空格也选中（分布如实为空）", (()=>{
  run('pHeatJump(0);');   /* 第 0 格 00:00–02:00 无录像 */
  const hb = get("state.hourBlock");
  const th = get('document.getElementById("tlane").innerHTML');
  run('state.hourBlock=-1; renderHeat(); renderPlayerHeat();');
  return hb === 0 && th.includes("全天分布") === false && th.includes("无录像");
})());

/* ---- 29. 0.9.37：16×/32× 倍速档 + 热力图播放定位竖线 ---- */
ck("倍速·档位含 16/32", src.includes("const SPEEDS = [1,2,4,8,16,32];"));
ck("倍速·playbackRate 钳到浏览器上限 16", src.includes("const rate = Math.min(x, 16);"));
ck("倍速·超 16 用跳秒补足（每秒 +x-16）",
   src.includes("if(x > 16){") && src.includes("v.currentTime + (x - 16)"));
ck("倍速·高速自动静音、回落恢复",
   src.includes("if(x > 8 && !v.muted){ v.muted = true; speedAutoMuted = true; }")
   && src.includes("else if(x <= 8 && speedAutoMuted){ v.muted = false; speedAutoMuted = false; }"));
ck("倍速·loadedmetadata 同步钳制与静音",
   src.includes("v.playbackRate = Math.min(state.speed || 1, 16);")
   && src.includes("if((state.speed || 1) > 8) v.muted = true;"));
ck("倍速·选档弹层共用 renderSpeedPop（6 档全渲染）", src.includes("SPEEDS.map(x =>"));

ck("竖线·样式与结构就绪",
   src.includes(".tl-cursor{position:absolute;top:-1px;bottom:-1px;width:2px;background:#ff5a5a;")
   && src.includes('<i class="tl-cursor"></i>')
   && typeof get("syncTlCursor") === "function");

/* 纯函数断言（桩的 querySelector 是万能桩，style 断言只能查源码/算结果）：
   全时 10:00–11:00 的文件，state.t=10:30 → 全天 frac=43.75% */
run('state.hourBlock=-1;'
  + 'state.segs=[{n:"10-00-00.mp4",s:36000,e:39600,b:1,file:"f1"}];'
  + 'state.segIdx=0; state.t=37800;');
ck("竖线·全天+全时：10:30 → frac 0.4375", get("tlCursorFrac()") === 0.4375);
run('state.hourBlock=5;');
ck("竖线·时段+全时：窗口内 frac 0.25", get("tlCursorFrac()") === 0.25);
run('state.hourBlock=-1;'
  + 'state.segs=[{n:"10-00-00.mp4",s:0,e:20,b:1,clip:true,src:36000,srcEnd:39600,file:"c1"}];'
  + 'state.segIdx=0; state.t=10;');
ck("竖线·缩时播放按源时刻（10s/20s → 源 10:30 → 0.4375）", get("tlCursorFrac()") === 0.4375);
run('state.hourBlock=5;'
  + 'state.segs=[{n:"20-00-00.mp4",s:72000,e:75600,b:1,file:"f2"}];'
  + 'state.segIdx=0; state.t=72000;');
ck("竖线·文件在窗口外返回 null（隐藏）", get("tlCursorFrac()") === null);
ck("竖线·两轨同源同位（renderTl 每条 track 都带 .tl-cursor）",
   src.includes('+ \'<i class="tl-cursor"></i></span></div>\';'));
ck("竖线·接线：syncPlayer 轻量跟随 + renderTl 重建即摆位",
   src.includes("syncTlCursor();          /* 热力图定位竖线跟随播放")
   && src.includes("syncTlCursor();          /* 重建后立即摆到当前播放位置 */"));

/* ---- 30. 0.9.38：切分方式（通用 + 每路） ---- */
ck("通用·卡片有切分方式 chips（join/split）",
   src.includes('data-clipstyle="join">整段拼接</button>')
   && src.includes('data-clipstyle="split">活动切片</button>'));
ck("通用·LK 默认整段拼接", get("LK.clipStyle") === "join");
ck("通用·处理器写回 LK.clipStyle 并推服务端",
   src.includes('LK.clipStyle = el.dataset.clipstyle === "split" ? "split" : "join";')
   && src.includes("clip_style: LK.clipStyle"));
ck("通用·服务端回读对齐（/api/link 与 /api/config 两条路）",
   src.includes("if(ls.clip_style) LK.clipStyle = ls.clip_style;")
   && src.includes("if(c.clip_style && c.clip_style !== LK.clipStyle) LK.clipStyle = c.clip_style;")
   && src.includes('if(g.clipStyle) LK.clipStyle = g.clipStyle === "split" ? "split" : "join";'));

ck("每路·默认 follow（跟随通用）",
   get("defaultCamParams(null,0).clipMode") === "follow");
ck("每路·编辑态三选 chips",
   src.includes('[["follow","跟随通用"],["join","整段拼接"],["split","活动切片"]].map')
   && src.includes("data-clipmode"));
ck("每路·处理器写草稿", src.includes('draftSet("clipMode", el.dataset.clipmode);'));
ck("每路·推送与服务端回读",
   src.includes("clip_mode: p.clipMode")
   && src.includes('["follow","join","split"].indexOf(src.clip_mode) >= 0 ? src.clip_mode : "follow"'));
ck("每路·只读摘要显示生效值",
   src.includes('roRow("切分方式", clipModeLabel(p.clipMode, true))'));

run('LK.clipStyle = "join";');
ck("标签·split/join 文案", get('clipModeLabel("split")') === "活动切片"
   && get('clipModeLabel("join")') === "整段拼接");
ck("标签·follow 展开通用值", get('clipModeLabel("follow")') === "跟随通用"
   && get('clipModeLabel("follow", true)') === "跟随通用 · 整段拼接");
run('LK.clipStyle = "split";');
ck("标签·follow 跟随切换后的通用值",
   get('clipModeLabel("follow", true)') === "跟随通用 · 活动切片");
run('LK.clipStyle = "join";');

/* ===== 31. 0.9.39：切片真实覆盖窗 + 红色定位样式 ===== */
ck("覆盖窗·活动切片优先用 idx 侧车",
   src.includes("if(Array.isArray(f.w) && f.w.length === 2)")
   && src.includes("st = +f.w[0]; en = Math.max(st + 1, +f.w[1]);"));
ck("覆盖窗·整段拼接保留旧算法兜底",
   src.includes("const next = i+1 < files.length ? toS(files[i+1].n) : Math.min(86400, st + segLen);"));
ck("竖线·红色光晕",
   src.includes(".tl-cursor{position:absolute;top:-1px;bottom:-1px;width:2px;background:#ff5a5a;")
   && src.includes("box-shadow:0 0 6px rgba(255,80,80,.9)"));
ck("播放当前时刻·红色",
   src.includes(".p-times .pt-c{flex:1;text-align:center;font-size:12.5px;font-weight:600;color:#ff6b6b}"));

console.log("设置页 " + (pass + fail) + " 项：通过 " + pass + "，失败 " + fail);
process.exit(fail ? 1 : 0);
