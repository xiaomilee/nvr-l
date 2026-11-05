/* 文件页日期条：今天判定 + 按源分色（0.9.113）
   ------------------------------------------------------------------
   真机反馈（2026-11-06 01:18）：文件页把 11/5 标成了「今天」。根因是
   renderDates 里用 new Date().toISOString().slice(0,10) 取今天 —— toISOString
   走 UTC，北京时间 00:00~08:00 之间 UTC 还停在前一天。全文件其余地方都用
   jsToday()（本地日历），就这一处不一致。

   同时按「这天磁盘上有什么源」给日期格分色：full 灰 / archive 淡紫 /
   both 蓝→紫渐变，与时间轴、存储条的图例同一套色相（--primary / --arch）。

   跑法： node test/ui/dates.test.js */
const { els, mkEl } = require("./harness.js");
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");
const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);
const J = v => JSON.stringify(v);

let pass = 0, fail = 0;
function ck(name, cond){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name); }
}

/* ---- 收集 renderDates 造出来的日期格 ---- */
const made = [];
const origCreate = global.document.createElement;
global.document.createElement = function(tag){
  const el = origCreate.call(this, tag);
  made.push(el);
  return el;
};
function renderChips(){
  made.length = 0;
  run("renderDates();");
  return made.filter(e => /(^|\s)dchip(\s|$)/.test(String(e.className || "")));
}
const txt = el => String(el._html || "");

const CAM = "cam-d";
const F_DAY = "2020-03-01";    // 只有全时
const A_DAY = "2020-03-02";    // 只有归档
const B_DAY = "2020-03-03";    // 两份都在（归档交接期）

const FULL = {};
FULL[F_DAY] = [{ n: "09-00-00.mp4", b: 1024 }];
FULL[B_DAY] = [{ n: "08-00-00.mp4", b: 1024 }];
const ARCH = {};
ARCH[A_DAY] = [{ n: "10-00-00.mp4.c01.mp4", b: 2048, d: 28, w: [36000, 36030] }];
ARCH[B_DAY] = [{ n: "12-00-00.mp4.c01.mp4", b: 2048, d: 29, w: [43200, 43230] }];

function setRec(){
  run('state.camId = ' + J(CAM) + '; state.daySel = null; state.kindPick = null;');
  run('state.rec = ' + J({
    ok: true, segment_sec: 300,
    cameras: (function(){ const o = {}; o[CAM] = FULL; return o; })(),
    archive: (function(){ const o = {}; o[CAM] = ARCH; return o; })()
  }) + ';');
}

/* ---- 1. 分色：三种源的 data-k ---- */
setRec();
let chips = renderChips();
const byDay = {};
chips.forEach(c => { const d = txt(c).match(/(\d\d\/\d\d)/); if(d) byDay["2020-" + d[1].replace("/", "-")] = c; });
ck("全时日 data-k=full",     byDay[F_DAY] && byDay[F_DAY].dataset.k === "full");
ck("归档日 data-k=archive",  byDay[A_DAY] && byDay[A_DAY].dataset.k === "archive");
ck("两源并存 data-k=both",   byDay[B_DAY] && byDay[B_DAY].dataset.k === "both");
ck("归档格带 title 说明",    !!(byDay[A_DAY] && /归档浓缩/.test(byDay[A_DAY].title || "")));
ck("并存格带 title 说明",    !!(byDay[B_DAY] && /归档交接期/.test(byDay[B_DAY].title || "")));

/* ---- 2. 「今天」用本地日历，不用 UTC ---- */
ck("没有 toISOString 取今天的写法",
   !/new Date\(\)\.toISOString\s*\(\s*\)/.test(src.replace(/\/\*[\s\S]*?\*\//g, "")));
ck("renderDates 里 today 来自 jsToday()",
   /const today = jsToday\(\);/.test(src));

/* 把 Date 钉在本地 2026-11-06 01:18（北京时间的凌晨 —— 旧写法会算出 11-05） */
run('var __RD = Date; Date = class extends __RD {'
  + ' constructor(...a){ if(!a.length) super(2026, 10, 6, 1, 18, 0); else super(...a); } };');
const D0 = "2026-11-05", D1 = "2026-11-06";
const FULL2 = {}, ARCH2 = {};
FULL2[D0] = [{ n: "23-00-00.mp4", b: 1024 }];
FULL2[D1] = [{ n: "00-30-00.mp4", b: 1024 }];
run('state.camId = ' + J(CAM) + '; state.daySel = null;');
run('state.rec = ' + J({
  ok: true, segment_sec: 300,
  cameras: (function(){ const o = {}; o[CAM] = FULL2; return o; })(),
  archive: (function(){ const o = {}; o[CAM] = ARCH2; return o; })()
}) + ';');
chips = renderChips();
const chip1 = chips.find(c => txt(c).includes("11/06"));
const chip0 = chips.find(c => txt(c).includes("11/05"));
ck("11/06（本地今天）标「今天」", !!(chip1 && txt(chip1).includes("今天")));
ck("11/05 不再标「今天」",        !!(chip0 && !txt(chip0).includes("今天")));
ck("jsToday() 与本地日历一致",    run('jsToday()') === "2026-11-06");

/* ---- 3. 默认落点：今天没片时退到最近有片的一天 ---- */
run('state.daySel = null; recalcDayList();');
ck("今天有片 → 选中今天", run('curDay()') === "2026-11-06");
run('delete state.rec.cameras[' + J(CAM) + '][' + J(D1) + ']; state.daySel = null; recalcDayList();');
ck("今天没片 → 退到最近有片的一天", run('curDay()') === "2026-11-05");
run('state.rec.cameras[' + J(CAM) + '] = {}; state.daySel = null; recalcDayList();');
/* 一格数据都没有时 all 为空，补今天的逻辑整体不走（日期条由
   applyPlaybackVisibility 直接藏起来），这里只保证不抛错、dayList 不长出假日期。 */
ck("一天都没片 → dayList 为空（日期条隐藏）", run('(state.dayList || []).length') === 0);

/* ---- 4. 选中态只有一个 ---- */
run('Date = __RD;');
setRec();
chips = renderChips();
ck("只有一格 on", chips.filter(c => /(^|\s)on(\s|$)/.test(String(c.className))).length === 1);

console.log("dates.test.js  " + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
