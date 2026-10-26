/* 回放默认落点（0.9.72）回归测试
   ------------------------------------------------------------------
   背景（实测出来的老毛病）：
   0.9.48 用 PV_BACK=30 做「段尾往前 30 秒开播」，但它只在换机位/换日期时触发，
   从底部导航进回放页根本不定位；而手点播放走的是另一套 pvDefaultStart，
   落点 = min(now, 最后段.e-1)。录制一停，这个落点只剩 1 秒可播 → ended →
   pvAdvance 找不到下一段 → pvFinish 收播，用户看到「点一下闪一下」，
   于是要点几下才能真的开始播。

   0.9.72 规则：今天 = 最后一段段首；历史日期 = 当天第一段段首；
   进页面自动定位并播。本测试钉住这三条 + 删掉的旧符号。
   跑法： node test/ui/play_enter.test.js */
require("./harness.js");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
if(!blocks || !blocks.length){ console.error("找不到 <script> 块"); process.exit(1); }
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");

let pass = 0, fail = 0;
function ck(name, cond){ if(cond){ pass++; } else { fail++; console.log("  FAIL " + name); } }

const ctx = vm.createContext(global);
try{
  vm.runInContext(code, ctx, { filename:"index.html" });
}catch(e){
  console.error("顶层执行失败：" + e.message);
  process.exit(1);
}
const run = s => vm.runInContext(s, ctx);
const get = s => vm.runInContext(s, ctx);
const J = v => JSON.stringify(v);

const TODAY = get("jsToday()");
const YDAY  = "2020-01-02";       // 固定过去日期，不会跟"今天"撞

/* 造一份 /api/recordings 结构。分片文件名 HH-MM-SS.mp4（服务端就按钟点命名），
   realSegs 从名字前 8 字符解出起始秒。 */
function recJson(cam, days){
  const cameras = {}; cameras[cam] = {};
  Object.keys(days).forEach(d => {
    cameras[cam][d] = days[d].map(t => ({ n: t + ".mp4", b: 1024 }));
  });
  return J({ ok:true, segment_sec:300, cameras, clips:{} });
}
function loadCam(cam, days, daySel){
  run('state.camId = ' + J(cam) + '; state.pmode = "full";'
    + ' PPLAY.on = false; PPLAY.segIdx = -1; PPLAY.filter = "full";');
  run('state.rec = ' + recJson(cam, days) + ';');
  run('state.daySel = ' + J(daySel) + '; recalcDayList();');
  run('pvLoadDay();');
}

/* ---- 1. 今天：落点 = 最后一段的段首 ---- */
loadCam("cam-x", { [TODAY]: ["01-00-00", "01-05-00", "01-10-00"] }, TODAY);
ck("今天·段列表装好 3 段", get("PPLAY.segs.length") === 3);
const tToday = get("pvEnterStart()");
ck("今天·落点 = 最后一段段首 01:10:00", tToday === 1 * 3600 + 10 * 60);
ck("今天·落点不在段尾（不是 min(now, e-1) 那种贴尾公式）",
   tToday !== get("PPLAY.segs[2].e - 1"));

/* ---- 2. 段首落点必须留足可播时长（旧逻辑只剩 1 秒 → 秒停收播）---- */
const lastE = get("PPLAY.segs[2].e");
ck("今天·落点剩余可播 ≥ 分片长度的一半", lastE - tToday >= 150);

/* ---- 3. 历史日期：落点 = 当天第一段段首（从头看，能一路往后连播）---- */
loadCam("cam-x", { [YDAY]: ["08-00-00", "08-05-00"] }, YDAY);
ck("历史日期·选中日生效", get("curDay()") === YDAY);
ck("历史日期·落点 = 第一段段首 08:00:00", get("pvEnterStart()") === 8 * 3600);

/* ---- 4. 没有分片时返回 null，绝不返回"现在时刻" ---- */
loadCam("cam-x", { [TODAY]: ["01-00-00"] }, TODAY);
run("PPLAY.segs = [];");
ck("无分片·pvEnterStart 返回 null", get("pvEnterStart()") === null);
ck("无分片·pvDefaultStart 返回 null（旧版返回 now，导致误报「该时刻无录像」）",
   get("pvDefaultStart()") === null);

/* ---- 5. 活动筛选的语义没被改动：仍是当天第一个活动前 3 秒 ---- */
loadCam("cam-x", { [TODAY]: ["01-00-00"] }, TODAY);
run('PPLAY.filter = "act"; vtlData = { events: [{ s:3600, e:3700 }], summary:{} };');
ck("活动筛选·第一个活动前 PV_ROLL(3s)", get("pvDefaultStart()") === 3597);
run('PPLAY.filter = "full"; vtlData = null;');

/* ---- 6. 当前日期没片子 → 回退到最近有录像的一天 ---- */
run('state.camId = "cam-x"; state.pmode = "full"; PPLAY.filter = "full";');
run('state.rec = ' + recJson("cam-x", { [YDAY]: ["08-00-00"] }) + ';');
run('state.daySel = ' + J(TODAY) + '; recalcDayList(); pvLoadDay();');
ck("回退前·今天没有全时分片", get("PPLAY.segs.length") === 0);
let fell = false, fellErr = null;
try{ fell = get("pvFallbackDay()"); }catch(e){ fellErr = e; }
ck("pvFallbackDay·执行不抛异常", fellErr === null);
ck("pvFallbackDay·返回 true 表示发生了切换", fell === true);
ck("pvFallbackDay·日期已切到最近有录像的那天", get("curDay()") === YDAY);
ck("pvFallbackDay·切完段列表已就位", get("PPLAY.segs.length") === 1);

/* ---- 7. 清单还没拉回来：不抛异常、不自激递归、不误开播 ----
   拉清单走 3 秒节流（不是布尔锁）：布尔锁一旦因异常路径没复位，
   之后每次进页都会静默跳过自动定位，属于只有真机才摸得到的哑弹。 */
run('state.camId = "cam-x"; state.rec = null; PPLAY.segs = []; PPLAY.on = false;');
let autoErr = null;
try{ run("pvAutoEnter()"); }catch(e){ autoErr = e; }
ck("无清单·pvAutoEnter 不抛异常（含递归爆栈）", autoErr === null);
ck("无清单·没有误开播", get("PPLAY.on") === false);
ck("无清单·记下了这次拉清单的时间（节流锚点）", get("pvEnterAt") > 0);
ck("无清单·连续调用被节流挡住（不重复发请求）", (()=>{
  const before = get("pvEnterAt");
  run("pvAutoEnter()");
  return get("pvEnterAt") === before;
})());

/* ---- 8. 自动播被浏览器拦：状态必须退回「未开播」，别让大播放钮消失 ----
   桩里的 pvCur 没有 play()，正好等价于「播放能力不可用/被拦」。 */
run("PPLAY.on = true;");
let playErr = null;
try{ run("pvSafePlay()"); }catch(e){ playErr = e; }
ck("pvSafePlay·内部异常不外抛", playErr === null);
ck("pvSafePlay·播放没起来时 PPLAY.on 退回 false（大播放钮留在画面上）",
   get("PPLAY.on") === false);

/* ---- 9. 接线：进页面自动定位 + 删掉旧的 30 秒回溯 ---- */
ck("PV_BACK 常量已删除", !/const\s+PV_BACK/.test(src));
ck("pvJumpLatest 已删除（逻辑并入 pvAutoEnter）", !/function\s+pvJumpLatest/.test(src));
ck("进回放页调 renderPlay(true)", /if\(page === "play"\)\{[\s\S]{0,320}renderPlay\(true\);/.test(src));
ck("renderPlay 的 autoStart 走 pvAutoEnter(true)",
   src.includes("if(autoStart) pvAutoEnter(true);"));
ck("手点播放不再直落 pvPlayAt(pvDefaultStart())",
   !src.includes("pvPlayAt(pvDefaultStart())"));
ck("旧贴尾公式 min(now, best.e - 1) 已不存在",
   !src.includes("Math.min(now, best.e - 1)"));
ck("非强制入场不打断正在播（force 语义）",
   src.includes("if(!force && PPLAY.on) return;"));
ck("清单没到只提示读取中，不报「该时刻无录像」",
   src.includes('"正在读取录像清单…"'));
ck("回放页播放走 pvSafePlay（被拦时退回未开播），不再用通用的 safePlay",
   /function pvSafePlay\(\)\{/.test(src) && !src.includes("  safePlay(pvCur);"));

/* ---- 10. 活动模式推进（0.9.74）：人在活动里，不许判「播完最后一个活动」----
   真机两次事故（2026-10-26，都在 0.9.73）：
     车库 03:43:24 —— 播放在车辆活动「03:42:33 → 04:00:26」内部停住；
     客厅 03:40:01 —— 播放在移动活动「03:38:30 → 03:44:54」内部停住。
   共同点：当前活动的**起点早于播放时刻**，而旧 pvNextTarget 只找「起点更晚的活动」，
   于是返回 null → pvAdvance 刷一轮清单/台账仍是 null → pvFinish 收播。
   分片跨边界（车库 120 秒、客厅 300 秒）就停一次，用户看到红线压在活动块上却说播完了。
   下面用这两次的真实时刻做回归。 */
const GK_T = 3 * 3600 + 43 * 60 + 24;          // 车库事故的播放时刻 03:43:24
run('PPLAY.filter = "act"; PPLAY.on = true;'
  + ' PPLAY.cls = {motion:true, person:true, vehicle:true}; state.rec = {segment_sec:120};');
/* 分片：03:39:00 起每片 60 秒（取整方便算），播放头在第二片末尾 */
run('PPLAY.segs = [{s:13140, e:13200, file:"a.mp4"}, {s:13200, e:13260, file:"b.mp4"}]'
  + '; PPLAY.segIdx = 0; PPLAY.t = ' + GK_T + ';');
/* 台账：一条 03:42:33 → 04:00:26 的车辆活动，覆盖播放头，且没有更晚的活动（事故原样） */
run('vtlData = { events: [{s:13353, e:14426, cls:{vehicle:1}}], now: 14430 };');

let nxt = null, nxtErr = null;
try{ run("__n = pvNextTarget();"); nxt = get("__n && {t:__n.t, cont:!!__n.cont}"); }
catch(e){ nxtErr = e; }
ck("活动内·pvNextTarget 不抛异常", nxtErr === null);
ck("活动内·目标不是 null（旧逻辑在这里返回 null 直接收播）", nxt !== null);
ck("活动内·目标 = 下一分片起点（继续播这段活动）", nxt && nxt.t === 13200);
ck("活动内·标记 cont：跨片连播，不快进也不跳", nxt && nxt.cont === true);

ck("活动内·pvActSpan 返回这条活动的结束时刻（不是 null，也不是起点）",
   get("pvActSpan(" + GK_T + ")") === 14426);
ck("活动内·pvInEvent 与 pvActSpan 同一口径", get("pvInEvent(" + GK_T + ")") === true);

/* 活动重叠（0.9.69 读端分档：AI 段吸收移动簇，可同起点不同终点）→ 必须取最晚的 e，
   否则拿到短的那条会提前判「已离开活动」 */
run('vtlData.events = [{s:13353, e:13400, cls:{}}, {s:13353, e:14426, cls:{vehicle:1}}];');
ck("活动重叠·取最晚结束的那条（14426，不是 13400）",
   get("pvActSpan(" + GK_T + ")") === 14426);

/* 活动内但后面已经没有分片了 → 才算真到头（返回 null 交给 endHit 重试） */
run('PPLAY.segIdx = 1;');
ck("活动内·最后一个分片之后没有片可接 → null",
   get("(function(){var r=pvNextTarget(); return r===null;})()") === true);

/* 分片起点越过了活动末尾 → 这段活动已播完，不该再「继续播」 */
run('PPLAY.segs = [{s:13140, e:13200, file:"a.mp4"}, {s:14900, e:14960, file:"b.mp4"}]'
  + '; PPLAY.segIdx = 0;'
  + ' vtlData.events = [{s:13353, e:14426, cls:{vehicle:1}}, {s:15000, e:15200, cls:{}}];');
nxt = get("(function(){var r=pvNextTarget(); return r && {t:r.t, cont:!!r.cont};})()");
ck("活动已过·目标 = 下一个活动起点前 PV_ROLL(3s)", nxt && nxt.t === 14997);
ck("活动已过·不标记 cont（这一步要跳/快进静止段）", nxt && nxt.cont !== true);

/* ---- 11. 收播文案：四种处境不能混成一句 ----
   旧版活动模式一律「已播完今天最后一个活动」，才会出现「红线在活动块上却被告知播完」 */
run('PPLAY.segs = [{s:13140, e:13200, file:"a.mp4"},{s:13200, e:13500, file:"b.mp4"}]'
  + '; PPLAY.segIdx = 1; PPLAY.t = ' + GK_T + '; state.rec = {segment_sec:300};');
run('vtlData = { events: [{s:13353, e:14426, cls:{vehicle:1}}], now: 13520 };');   // 还在录
ck("文案·在活动内 + 还在录 → 已播到最新，这个活动还在录制",
   get("pvEndMsg()") === "已播到最新，这个活动还在录制");
run('vtlData.now = 14430;');                                                      // 录制已停
ck("文案·在活动内 + 已停录 → 这个活动后面没有录像",
   get("pvEndMsg()") === "这个活动后面没有录像");
run('PPLAY.t = 15000;');                     // 播放头已出活动
ck("文案·已出活动 + 已停录 → 已播完今天最后一个活动",
   get("pvEndMsg()") === "已播完今天最后一个活动");
run('PPLAY.segs = [{s:13140, e:13200, file:"a.mp4"}, {s:14900, e:15300, file:"b.mp4"}]'
  + '; PPLAY.segIdx = 1; vtlData.now = 15360;');   // 最后一片刚封口，还在往后录
ck("文案·已出活动 + 还在录 → 已播完今天最后一个活动，正在录新的",
   get("pvEndMsg()") === "已播完今天最后一个活动，正在录新的");
ck("文案·活动模式不再无条件宣称播完",
   !/filter === "act"\)\s*return "已播完今天最后一个活动";/.test(src));

/* ---- 12. 全时模式不受影响 ---- */
run('PPLAY.filter = "full"; PPLAY.segs = [{s:100, e:400, file:"a.mp4"},{s:400, e:700, file:"b.mp4"}]'
  + '; PPLAY.segIdx = 0; PPLAY.t = 399;');
nxt = get("(function(){var r=pvNextTarget(); return r && {t:r.t, cont:!!r.cont};})()");
ck("全时·目标仍是下一分片起点", nxt && nxt.t === 400);
ck("全时·不带 cont 标记", nxt && nxt.cont !== true);

/* ---- 13. 接线：cont 必须在两个 act 分支之前被消费 ----
   pvAdvance 的两个 act 分支都会给目标加 PV_ROLL 或设 ff，
   cont 的目标是分片起点本身，排到后面就会被多垫 3 秒、跳过画面 */
ck("pvAdvance·cont 分支存在", /if\(tgt\.cont\)\{\s*pvPlayAt\(tgt\.t, \{ quiet:true \}\); return; \}/.test(src));
ck("pvAdvance·cont 分支排在活动快进分支之前",
   src.indexOf("if(tgt.cont)") > 0
   && src.indexOf("if(tgt.cont)") < src.indexOf('PPLAY.filter === "act" && PPLAY.silent !== "skip"'));

console.log("\nplay_enter: " + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
