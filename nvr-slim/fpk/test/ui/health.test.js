/* 录像可信度：细分状态 + 缺口卡（0.9.118）
   ------------------------------------------------------------------
   守两件事：

   1. **状态别撒谎**：界面以前用 c.state === "recording" 判「在录」，
      而那是「ffmpeg 进程还在」——真机断流时进程一直活着，界面就一直写「在录」。
      现在读后端算好的 hstate（recording / stalled / connecting / recovering /
      error / stopped / off），没有 hstate 的老进程才退回粗状态。
   2. **缺口要说人话**：一段 19 分钟的洞，得写成「10:05:00 → 10:24:00（19 分）」
      并给原因，不能只给一个数字。

   跑法： node test/ui/health.test.js */
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

/* ---- recHState：有 hstate 就用，没有才兜底 ---- */
ck("hstate=stalled 优先于 state=recording",
   run('recHState({hstate:"stalled", state:"recording"})') === "stalled");
ck("hstate=recording",
   run('recHState({hstate:"recording", state:"recording"})') === "recording");
ck("老进程无 hstate 兜底 recording",
   run('recHState({state:"recording"})') === "recording");
ck("老进程 idle 兜底 stopped",
   run('recHState({state:"idle"})') === "stopped");
ck("空对象不抛",
   run('recHState({})') === "stopped");

/* ---- 时间格式化 ---- */
ck("gfmtDur 45 秒", run('gfmtDur(45)') === "45 秒");
ck("gfmtDur 300 秒 = 5 分", run('gfmtDur(300)') === "5 分");
ck("gfmtDur 900 秒 = 15 分", run('gfmtDur(900)') === "15 分");
ck("gfmtDur 7380 秒 = 2 时 3 分", run('gfmtDur(7380)') === "2 时 3 分");
ck("gfmtDur 负数归零", run('gfmtDur(-5)') === "0 秒");
ck("gfmtClock 36300 = 10:05:00", run('gfmtClock(36300)') === "10:05:00");
ck("gfmtClock 0 = 00:00:00", run('gfmtClock(0)') === "00:00:00");

/* ---- cardGaps 三种形态 ---- */
ck("无数据时提示正在核对",
   run('GAPS.data=null; GAPS.err=""; cardGaps()').indexOf("核对今天") > 0);
ck("失败时给出重试口径",
   run('GAPS.data=null; GAPS.err="boom"; cardGaps()').indexOf("重新核对") > 0);

const EMPTY = {ok: true, cams: [], total: {gap_count: 0, gap_sec: 0}};
ck("没有缺口时明说",
   run('GAPS.data=' + J(EMPTY) + '; GAPS.err=""; cardGaps()')
     .indexOf("没有超过 1 分钟") > 0);

const ONE = {ok: true, total: {gap_count: 1, gap_sec: 900, max_gap_sec: 900}, cams: [{
  id: "c1", name: "客厅", state: "recording", gap_count: 1, gap_sec: 900,
  max_gap_sec: 900, coverage: 0.98,
  gaps: [{start: 36300, end: 37200, duration: 900,
          reason: "该时段录像进程未运行（断流、重启或应用未启动），已恢复"}]
}]};
let h = run('GAPS.data=' + J(ONE) + '; cardGaps()');
ck("缺口卡显示合计", h.indexOf("合计缺口 15 分") > 0);
ck("缺口卡显示路段时刻", h.indexOf("10:05:00 → 10:20:00") > 0);
ck("缺口卡显示时长", h.indexOf("（15 分）") > 0);
ck("缺口卡显示原因", h.indexOf("已恢复") > 0);
ck("缺口卡显示覆盖率", h.indexOf("98%") > 0);
ck("缺口卡显示路名", h.indexOf("客厅") > 0);

const MANY = {ok: true, total: {gap_count: 9, gap_sec: 3600, max_gap_sec: 1200}, cams: [{
  id: "c1", name: "院子", gap_count: 9, gap_sec: 3600, coverage: 0.9,
  gaps: Array.from({length: 9}, (_, i) => ({
    start: 3600 + i * 1500, end: 3600 + i * 1500 + 600, duration: 600, reason: "断流"
  }))
}]};
h = run('GAPS.data=' + J(MANY) + '; cardGaps()');
ck("最多列 6 段", (h.match(/断流/g) || []).length === 6);
ck("超出部分提示还有 3 段", h.indexOf("还有 3 段未列出") > 0);

/* ---- diagSummary 用 hstate 计数 ---- */
const REC = {channels: [
  {id: "c1", name: "客厅", state: "recording", hstate: "recording"},
  {id: "c2", name: "院子", state: "recording", hstate: "stalled",
   hreason: "分片 300 秒没增长"},
  {id: "c3", name: "车库", state: "recording", hstate: "recording"},
  {id: "c4", name: "后门", state: "idle", hstate: "connecting",
   hreason: "正在重连上游（已试 2 次）"},
  {id: "c5", name: "楼道", state: "idle", hstate: "error", error: "退出码 1"}
]};
run('LK.rec=' + J(REC) + '; REC.diskFreeGB=100; LK.detEnable=false;');
let sum = run('diagSummary()');
ck("摘要按 hstate 数在录（卡住不算）", sum.indexOf("2/5 路在录") !== -1);
ck("摘要把卡住与异常一起计为异常", sum.indexOf("2 路异常") > 0);

/* ---- cardRecCams：卡住要说原因 ---- */
run('LK.rec=' + J(REC) + ';');
let card = run('cardRecCams()');
ck("卡片标题给出卡住路数", card.indexOf("1 路卡住") > 0);
ck("卡住的路显示原因而不是「在录」", card.indexOf("分片 300 秒没增长") > 0);
ck("连接中的路显示重试提示", card.indexOf("正在重连上游") !== -1);

/* ---- 没 hstate 的旧数据不能崩 ---- */
run('LK.rec={channels:[{id:"c1",name:"客厅",state:"recording",last_seg:"10-00-00.mp4"}]};');
card = run('cardRecCams()');
ck("旧数据仍显示在录", card.indexOf("在录 · 最新 10-00-00.mp4") > 0);
ck("旧数据不报卡住", card.indexOf("路卡住") === -1);

console.log((fail ? "FAILED " : "OK ") + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
