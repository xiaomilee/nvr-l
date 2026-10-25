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

console.log("\nplay_enter: " + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
