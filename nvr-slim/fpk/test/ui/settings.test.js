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
ck("后端·配置推送与快照钩子就绪", typeof get('pushCfg') === "function" && typeof get('hookSnapImg') === "function");

/* ---- 1. 非编辑态：通用设置在上，摄像头在下，两区明显分开 ---- */
run('state.camId="cam-01"; SET.open=null; SET.editing=false; SET.draft=null; SET.genOpen=true; renderCamTabs();');
let h = body();
ck("通用区有标题", h.includes("通用设置"));
ck("通用区在前", h.indexOf("保存目录") < h.indexOf("data-addcam"));
ck("两分区标记齐全", h.includes("zone gen") && h.includes("zone cam"));
ck("有摄像头列表", h.includes("data-camrow"));
ck("有添加入口", h.includes("data-addcam"));
ck("有删除入口", h.includes("data-delcam"));
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

/* ---- 4. 编辑态独占一屏 ---- */
run('openEditor("cam-01");');
h = body();
ck("编辑·不再显示公共卡", !h.includes("保存目录"));
ck("编辑·有保存键", h.includes('data-save="1"'));
ck("编辑·有恢复默认", h.includes('data-reset="1"'));
ck("编辑·有取消", h.includes('data-cancel="1"'));
ck("编辑·主码流输入框", h.includes('data-k="main"'));
ck("编辑·子码流输入框", h.includes('data-k="sub"'));
ck("编辑·灵敏度滑杆", h.includes('data-k="sensitivity"'));
ck("编辑·画质滑杆", h.includes('data-k="day.crf"'));
ck("编辑·画布可画", !h.includes("maskstage locked"));
ck("编辑·未保存提示", h.includes("编辑中 · 未保存"));

/* ---- 5. 草稿语义：改草稿不动原值 ---- */
const orig = get('camParams["cam-01"].sensitivity');
run('draftSet("sensitivity", 88);');
ck("草稿·写入生效", get('draftGet("sensitivity")') === 88);
ck("草稿·原值未变", get('camParams["cam-01"].sensitivity') === orig);
run('draftSet("day.crf", 30);');
ck("草稿·支持嵌套路径", get('draftGet("day.crf")') === 30);

/* ---- 6. 保存落盘 ---- */
run('saveDraft();');
ck("保存·参数落盘", get('camParams["cam-01"].sensitivity') === 88);
ck("保存·嵌套落盘", get('camParams["cam-01"].day.crf') === 30);
ck("保存·退出编辑", get('SET.editing') === false);
ck("保存·回到只读", body().includes('data-editcam="1"'));

/* ---- 7. 恢复默认：只改草稿，不自动落盘 ---- */
run('openEditor("cam-01"); resetDraft();');
ck("默认·灵敏度归位", get('draftGet("sensitivity")') === 35);
ck("默认·码流保留", get('draftGet("main")') === "rtsp://192.168.2.108:8554/ch1");
ck("默认·不自动落盘", get('camParams["cam-01"].sensitivity') === 88);

/* ---- 8. 画面区域：16×9 方块预选 ---- */
ck("网格·16 列 9 行", get('GRID_COLS === 16 && GRID_ROWS === 9'));
ck("网格·两个画布并存（上下两组）", h.includes("maskCv-ig") && h.includes("maskCv-rd"));
ck("网格·上下两组标题", h.includes("检测忽略区") && h.includes("输出遮蔽区"));
ck("网格·旧的类型切换已去掉", !h.includes("data-mtool"));
ck("网格·示例数据带格子", get('camParams["cam-01"].masks.ignore.length') > 0);
const ig0 = get('maskArr("ignore").length');
run('maskArr("ignore").push(143);');
ck("网格·可追加", get('maskArr("ignore").length') === ig0 + 1);
run('maskArr("ignore").pop();');
ck("网格·可删除", get('maskArr("ignore").length') === ig0);
run('maskArr("redact").length = 0; maskArr("redact").push(5,6,21,22);');
ck("网格·遮蔽区独立存储", get('maskArr("redact").length') === 4);
run('renderMasks();');
ck("网格·列表可清空", document.getElementById("mlist").innerHTML.includes("data-clrmask"));
ck("网格·格号换算成矩形", JSON.stringify(get('rectCells(1,1,2,2)')) === "[17,18,33,34]");
run('saveDraft();');
ck("网格·随保存落盘", get('camParams["cam-01"].masks.redact.length') === 4);
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
h = body();
ck("布局·保留周期在码流地址之后", h.indexOf("保留周期") > h.indexOf('data-k="sub"'));
ck("布局·保留周期在事件判定之前", h.indexOf("保留周期") < h.indexOf("事件判定"));
run('closeEditor(); SET.open="cam-01"; renderCamTabs();');
h = body();
ck("布局·只读态同样顺序", h.indexOf("保留周期") > h.indexOf("子码流 · 预览") && h.indexOf("保留周期") < h.indexOf("事件判定"));

/* ---- 11. 拖动排序 ---- */
const store = {};
global.localStorage = {
  getItem:k=>Object.prototype.hasOwnProperty.call(store,k) ? store[k] : null,
  setItem:(k,v)=>{ store[k] = String(v); },
  removeItem:k=>{ delete store[k]; }
};
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
ck("持久化·区域读回", get('normMasks(camParams[CAMERAS[0].id].masks).ignore.length + normMasks(camParams[CAMERAS[0].id].masks).redact.length') > 0);
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

/* ---- 15. 诊断日志 / 全屏 / 历史返回 ---- */
run('SET.genOpen=true; SET.editing=false; SET.draft=null; SET.open=null; renderCamTabs();');
h = body();
ck("诊断·有日志查看框", h.includes("diagLogText"));
ck("诊断·刷新/复制/清除齐全", h.includes("btnLogRefresh") && h.includes("btnLogCopy") && h.includes("btnLogClear"));
ck("诊断·读/复制/清函数就绪",
   typeof get('loadDiagLog') === "function" && typeof get('copyDiagLog') === "function" && typeof get('clearDiagLog') === "function");
ck("诊断·旧版警告条 + 重启按钮", h.includes("diagWarn") && h.includes("btnRestart"));
ck("诊断·重启/环境自检函数就绪",
   typeof get('restartDiag') === "function" && typeof get('loadDiagEnv') === "function");
ck("全屏·切换函数就绪", typeof get('toggleLiveFull') === "function");
ck("在线状态·pollLive 同步函数就绪", typeof get('pollLive') === "function");
ck("历史·导航函数就绪",
   typeof get('goPage') === "function" && typeof get('activatePage') === "function" && typeof get('activePage') === "function");

/* ---- 16. HomeKit 风格倒计时 + 实时流播放 + 缩时清单（0.9.10） ---- */
ck("倒计时·徽标在右上角", /\.snap-cd\{position:absolute;right:10px;top:8px/.test(src));
ck("倒计时·徽标压在快照图上", /\.snap-cd\{[^}]*z-index:4/.test(src));
ck("倒计时·失败时红点", src.includes(".snap-cd.bad .dot") && src.includes('"-" + Math.ceil(left) + "s"'));
ck("实时流·悬浮播放/暂停按钮", src.includes("lr-float") && src.includes("data-lrplay") && typeof get('toggleLiveStream') === "function");
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
   && src.includes("syncFromServer();"));

console.log("设置页 " + (pass + fail) + " 项：通过 " + pass + "，失败 " + fail);
process.exit(fail ? 1 : 0);
