/* 回放页「切不到归档日期」回归（0.9.110）
   ------------------------------------------------------------------
   真机反馈（2026-11-05）：回放页切日期时，只有全时那几天能切过去，
   点第 4~7 天（只剩 _archive 归档浓缩的日子）会被悄悄弹回最近的全时日。

   根因两处，都在回放页：
     ① pvLoadDay 写死 realSegs(camId, day, "full")，归档日必然得到空清单；
     ② pvFallbackDay 找「最近有录像的一天」时也只认 full，于是空清单 →
        回退到全时日 → renderPlayDates 把菜单日期改掉，用户看到「切不过去」。

   顺带修一个坐标口径问题：realSegs 给归档片段的 s/e 是「按真实播放时长
   累加」的播放坐标（0,1,2…），那是文件页顺序播用的；回放页的 PPLAY.t 要跟
   时间轴红线、OSD 钟点、台账活动窗口比对，必须是钟点。pvSegs 现在把归档
   改写成 s = 源窗口起点 src、e = src + 真实时长。

   跑法： node test/ui/pv_archive.test.js */
require("./harness.js");
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");
const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);
const get = s => vm.runInContext(s, ctx);
const J = v => JSON.stringify(v);

let pass = 0, fail = 0;
function ck(name, cond){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name); }
}

const CAM = "cam-arc";
const ARCH_DAY = "2020-03-01";     // 只有归档（全时已过期）
const FULL_DAY = "2020-03-03";     // 只有全时
const BOTH_DAY = "2020-03-02";     // 两份并存
const EMPTY_DAY = "2020-03-04";    // 什么都没有

/* 归档片段：n 是源分片名 + .cNN，w 是 .idx.json 解出的源时间窗，d 是 ffprobe 真实时长。
   故意让 d 比窗口短 2 秒（真机 148 个片段都比标记窗短），验证 e 取真实时长。 */
function arcFile(name, w0, w1, dur){
  return { n: name, b: 800000, d: dur, w: [w0, w1] };
}
const ARCH = {
  [ARCH_DAY]: [
    arcFile("10-00-00.mp4.c01.mp4", 36000, 36030, 28),
    arcFile("11-00-00.mp4.c01.mp4", 39600, 39620, 18)
  ],
  [BOTH_DAY]: [ arcFile("12-00-00.mp4.c01.mp4", 43200, 43230, 29) ]
};
const FULL = {
  [FULL_DAY]: [{ n: "09-00-00.mp4", b: 1024 }, { n: "09-05-00.mp4", b: 1024 }],
  [BOTH_DAY]: [{ n: "08-00-00.mp4", b: 1024 }]
};

function setup(day){
  run('state.camId = ' + J(CAM) + '; state.pmode = "full";'
    + ' PPLAY.on = false; PPLAY.segIdx = -1; PPLAY.filter = "full";'
    + ' state.kindPick = null;');
  run('state.rec = ' + J({ ok: true, segment_sec: 300,
                           cameras: (function(){ const o = {}; o[CAM] = FULL; return o; })(),
                           archive: (function(){ const o = {}; o[CAM] = ARCH; return o; })() }) + ';');
  run('state.daySel = ' + J(day) + '; recalcDayList();');
  run('pvLoadDay();');
}

/* ---- 1. 归档日的来源判定 ---- */
setup(ARCH_DAY);
ck("归档日·pvKind 判定为 archive", get('pvKind("' + ARCH_DAY + '")') === "archive");
ck("全时日·pvKind 仍为 full", get('pvKind("' + FULL_DAY + '")') === "full");
ck("并存日·默认走全时（原行为不变）", get('pvKind("' + BOTH_DAY + '")') === "full");

/* ---- 2. pvLoadDay 必须装出归档清单（以前恒为空 → 被回退弹走）---- */
ck("归档日·分片清单不再为空", get("PPLAY.segs.length") === 2);
ck("归档日·分片地址走 kind=archive",
   String(get("PPLAY.segs[0].file")).indexOf("kind=archive") > 0);
ck("归档日·保留 arch 标记（时间轴按归档块样式画）",
   get("PPLAY.segs[0].arch") === true);

/* ---- 3. 坐标口径：归档片段的 s/e 必须是钟点，不是播放累加值 ---- */
ck("归档日·第 1 段 s = 源窗口起点 10:00:00", get("PPLAY.segs[0].s") === 36000);
ck("归档日·第 1 段 e = s + 真实时长（不是窗口终点 36030）",
   get("PPLAY.segs[0].e") === 36028);
ck("归档日·第 2 段 s = 11:00:00（不再从 0 累加）", get("PPLAY.segs[1].s") === 39600);
ck("归档日·段序严格递增（pvSegAt 二分的前提）",
   get("PPLAY.segs[1].s") > get("PPLAY.segs[0].e"));

/* ---- 4. 同一天再切回全时日，坐标不受污染 ---- */
setup(FULL_DAY);
ck("全时日·s 就是文件名钟点 09:00:00", get("PPLAY.segs[0].s") === 32400);
ck("全时日·没有 clock 改写标记", get("PPLAY.segs[0].clock") === undefined);

/* ---- 5. 核心回归：点归档日不被弹回全时日 ---- */
setup(ARCH_DAY);
let fell = null, err = null;
try{ fell = get("pvFallbackDay()"); }catch(e){ err = e; }
ck("归档日·pvFallbackDay 不抛异常", err === null);
ck("归档日·不发生回退（清单非空即不触发）", fell === false);
ck("归档日·当前日期仍是归档日", get("curDay()") === ARCH_DAY);
ck("归档日·清单仍是那 2 段", get("PPLAY.segs.length") === 2);

/* ---- 6. 空日期回退时，归档日也算「有录像」----
   以前只找 full，于是只有归档的那几天永远不会成为回退目标。 */
run('state.camId = ' + J(CAM) + '; state.pmode = "full"; PPLAY.filter = "full"; PPLAY.segs = [];');
run('state.daySel = ' + J(EMPTY_DAY) + '; recalcDayList();');
let fell2 = false, err2 = null;
try{ fell2 = get("pvFallbackDay()"); }catch(e){ err2 = e; }
ck("空日期·回退不抛异常", err2 === null);
ck("空日期·确实发生了回退", fell2 === true);
ck("空日期·回退目标是全时日（最近的优先，全时优先）", get("curDay()") === FULL_DAY);
ck("空日期·回退后清单已就位", get("PPLAY.segs.length") === 2);

/* 只剩归档的日子：全时全没了，回退目标必须能落在归档日。
   这里把归档也只留 03-01，确保「最近有录像的一天」就是纯归档日。 */
run('state.rec = ' + J({ ok: true, segment_sec: 300,
                         cameras: (function(){ const o = {}; o[CAM] = {}; return o; })(),
                         archive: (function(){ const o = {}; o[CAM] = {}; o[CAM][ARCH_DAY] = ARCH[ARCH_DAY]; return o; })() }) + ';');
run('state.daySel = ' + J(EMPTY_DAY) + '; recalcDayList(); PPLAY.segs = [];');
let fell3 = false, err3 = null;
try{ fell3 = get("pvFallbackDay()"); }catch(e){ err3 = e; }
ck("仅归档·回退不抛异常", err3 === null);
ck("仅归档·能回退到归档日（旧逻辑这里返回 false，页面空白）", fell3 === true);
ck("仅归档·落在归档日", get("curDay()") === ARCH_DAY);
ck("仅归档·清单是归档那 2 段", get("PPLAY.segs.length") === 2);

/* ---- 7. 用户手动选过维度时尊重选择（并存日）---- */
setup(BOTH_DAY);
run('state.pmode = "archive"; state.kindPick = { day: ' + J(BOTH_DAY) + ', kind: "archive" };');
run("pvLoadDay();");
ck("并存日·手动选归档 → 播归档", get('pvKind("' + BOTH_DAY + '")') === "archive");
ck("并存日·手动选归档 → 清单是归档段", get("PPLAY.segs[0].arch") === true);
run('state.pmode = "full"; state.kindPick = null;');

/* ---- 8. 归档日 OSD 钟点：PPLAY.t 落在钟点轴上 ----
   pvOnTime 的公式是 seg.s + currentTime，s 换成钟点后 OSD 自然显示真实时刻。 */
setup(ARCH_DAY);
run("PPLAY.on = true; PPLAY.segIdx = 0;");
const v = get("pvCur");
v.currentTime = 5;
v.seeking = false;
let onErr = null;
try{ run("pvOnTime()"); }catch(e){ onErr = e; }
ck("归档日·pvOnTime 不抛异常", onErr === null);
ck("归档日·OSD 时刻 = 10:00:05（不是 00:00:05）", get("PPLAY.t") === 36005);
ck("归档日·hms 显示 10:00:05", get("hms(PPLAY.t)") === "10:00:05");

/* ---- 9. 落点选段：按钟点跳播能命中归档段 ---- */
const hit = get("pvSegAt(39605)");
ck("归档日·按钟点 11:00:05 能命中第 2 段", !!hit && hit.idx === 1);
ck("归档日·seek 偏移 = 目标钟点 - 段起点",
   get("39605 - PPLAY.segs[1].s") === 5);
ck("归档日·不属于任何段的钟点返回 null", get("pvSegAt(37000)") === null);

console.log("\n" + (fail ? "FAILED" : "OK") + "  pass=" + pass + " fail=" + fail);
process.exit(fail ? 1 : 0);
