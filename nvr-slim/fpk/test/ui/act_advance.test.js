/* 活动模式推进的端到端回归（0.9.74）
   ------------------------------------------------------------------
   真机事故（0.9.73，2026-10-26）：
     车库 03:43:24 —— 播放在车辆活动「03:42:33 → 04:00:26」内部停住并报
                    「已播完今天最后一个活动」，红线明明压在活动块上。
   根因：pvNextTarget 只找「起点晚于当前时刻的活动」。人处在活动中间时，
   这个活动起点已过、后面又没有更晚的活动 → 返回 null → pvAdvance 刷一轮仍 null
   → pvFinish 收播。车库分片 120 秒、客厅 300 秒，于是每跨一次片边界就停一次。

   play_enter.test.js 钉住的是「单个函数怎么算」，这里钉的是**整条推进链路**：
   用真机那天的台账与分片，让 pvAdvance 一路走，必须逐片连播到最后一分片才判到头。
   跑法： node test/ui/act_advance.test.js */
require("./harness.js");
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8").replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");
const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);
const get = s => vm.runInContext(s, ctx);

/* 车库 2026-10-26 真机数据：车辆活动 03:42:33 → 04:00:26，另一条 03:19:52 → 03:41:59
   分片 120 秒，从 03:41:20 起连续（真机 03:43:24 就是在这里停住的） */
const segs = [];
for (let t = 3 * 3600 + 41 * 60 + 20; t < 4 * 3600; t += 120)
  segs.push({ s: t, e: t + 120, file: "seg-" + t + ".mp4" });

run('PPLAY.filter = "act"; PPLAY.on = true;'
  + ' PPLAY.cls = {motion:true, person:true, vehicle:true};'
  + ' state.rec = {segment_sec:120};');
run('vtlData = { events: [{s:13192, e:13319, cls:{vehicle:1}, mv:1},'
  + '                     {s:13353, e:14426, cls:{vehicle:1}}], now: 14430 };');
run('PPLAY.segs = ' + JSON.stringify(segs) + ';');

/* 桩 pvPlayAt：只记录落点并推进状态，不碰 DOM */
run('globalThis.__hop = 0; globalThis.__trace = [];');
run('pvPlayAt = function(t, o){'
  + ' globalThis.__hop++; globalThis.__trace.push(Math.round(t));'
  + ' PPLAY.t = t; PPLAY.inAct = PPLAY.filter==="act" && pvInEvent(t); PPLAY.endHit=false;'
  + ' PPLAY.segIdx = PPLAY.segs.findIndex(function(sg){ return t>=sg.s && t<sg.e; });'
  + ' if(PPLAY.segIdx < 0) PPLAY.segIdx = PPLAY.segs.length-1;'
  + ' return true; };');

/* 从 03:43:24 开始：每"播完一片"就调一次 pvAdvance，最多 40 跳 */
run('PPLAY.t = 13404; PPLAY.segIdx = 1; PPLAY.inAct = true; PPLAY.endHit = false;');
let hops = 0, endHitAt = -1;
for (let i = 0; i < 40; i++) {
  const before = run("PPLAY.t");
  run("pvAdvance();");
  const after = run("PPLAY.t");
  if (run("PPLAY.endHit") === true) { endHitAt = i; break; }   // 这一步判到头了
  hops++;
  if (after === before) break;
}
const trace = JSON.parse(run("JSON.stringify(globalThis.__trace)"));
const hms = s => [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60]
  .map(x => String(x).padStart(2, "0")).join(":");
console.log("跳转轨迹：" + trace.map(hms).join(" → "));
console.log("走到尽头的时刻：" + hms(run("PPLAY.t")));

const lastSeg = JSON.parse(run("JSON.stringify(PPLAY.segs[PPLAY.segs.length-1])"));
/* 事故点 03:43:24 → 第一跳必须是下一分片段首 03:45:20（旧代码这里直接收播）。
   之后每跳 +120 秒，直到最后一片（03:59:20）才允许判到头 —— 
   活动 04:00:26 收尾落在最后一片内部，那是"分片还没封口"，不是逻辑错误。 */
const okFirst = trace[0] === 13520;
const okWalk = trace.every((t, i) => i === 0 || t === trace[i-1] + 120);
const okLast = trace[trace.length-1] === lastSeg.s;
const ok = okFirst && okWalk && okLast && hops === 8 && endHitAt === 8;
console.log(ok
  ? "PASS：03:43:24 不再收播，逐片连播到最后一分片 " + hms(lastSeg.s) + " 才判到头"
  : "FAIL：first=" + okFirst + " walk=" + okWalk + " last=" + okLast + " hops=" + hops);
process.exit(ok ? 0 : 1);
