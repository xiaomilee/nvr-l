/* 活动块按「哪里有画面」切段（0.9.75）。
   真机 2026-10-26：蓝块因为 avail_spans 用 mtime 算出真实片尾，开始留洞；
   活动块必须跟蓝块共用同一份事实，否则「有活动 = 有画面」这个暗示继续骗人 ——
   断录期间人还在院子里，橙块照样垂下来，点进去没画面。

   跑法： node test/ui/avail_gap.test.js */
require("./harness.js");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");

const made = [];
const mk = id => {
  const el = {
    id, _html: "", className: "", style: {}, dataset: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    textContent: "", onclick: null,
    appendChild(){}, querySelectorAll(){ return []; }
  };
  Object.defineProperty(el, "innerHTML", {
    get(){ return el._html; },
    set(v){ el._html = String(v); }
  });
  return el;
};
document.createElement = tag => { const el = mk(tag); made.push(el); return el; };

const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);
const clip = (s, e, spans) => run("vtlClipByAvail(" + s + "," + e + "," + JSON.stringify(spans) + ")");

let pass = 0; const fails = [];
const ck = (name, cond, detail) => {
  if (cond) { pass++; console.log("  ✅ " + name); }
  else { fails.push(name); console.log("  ❌ " + name + "  ← " + JSON.stringify(detail)); }
};

console.log("— 纯函数 vtlClipByAvail —");
ck("切成两段：活动 100~300 被断录 150~200 分开",
   JSON.stringify(clip(100, 300, [[0, 150], [200, 400]])) === "[[100,150],[200,300]]",
   clip(100, 300, [[0, 150], [200, 400]]));
ck("整个活动落在断录里 → 空数组（调用方画虚线框）",
   JSON.stringify(clip(160, 190, [[0, 150], [200, 400]])) === "[]");
ck("活动完全被一个区间盖住 → 原样一段",
   JSON.stringify(clip(110, 190, [[0, 400]])) === "[[110,190]]");
ck("首尾相接的两个区间不切出零长度碎片",
   JSON.stringify(clip(100, 300, [[0, 200], [200, 400]])) === "[[100,300]]");
ck("没有 avail 数据（老服务端 / 接口失败）→ 原样返回，不许把活动裁没",
   JSON.stringify(clip(100, 300, [])) === "[[100,300]]"
   && JSON.stringify(run("vtlClipByAvail(100,300,null)")) === "[[100,300]]"
   && JSON.stringify(run("vtlClipByAvail(100,300,undefined)")) === "[[100,300]]");
ck("只有极小重叠（≤0.01 秒）→ 丢弃这段，不留 0px 碎块",
   JSON.stringify(clip(100, 300, [[0, 100.005], [200, 400]])) === "[[200,300]]",
   clip(100, 300, [[0, 100.005], [200, 400]]));
ck("三段（两处断录）",
   JSON.stringify(clip(0, 1000, [[0, 100], [200, 300], [400, 1000]])) === "[[0,100],[200,300],[400,1000]]");

console.log("— renderVtl 集成 —");
const day = "2026-10-26";
const data = {
  ok: true, cam: "cam-01", date: day,
  now: 12 * 3600,
  summary: { count: 2, active_sec: 1100 },
  /* 有录像：10:00~10:25、10:30~10:35；10:25~10:30 是断录 */
  avail: { full: [[36000, 37500], [37800, 38100]], archive: [] },
  events: [
    /* 跨断录：10:10~10:33:20 → 该切成 10:10~10:25 + 10:30~10:33:20 */
    { s: 36600, e: 38000, d: 0.35, cls: { person: 2 }, mv: 1 },
    /* 整段落在断录里：10:26~10:28 → 该变成 gap 虚线框 */
    { s: 37560, e: 37680, d: 0.35, cls: { vehicle: 1 } },
  ]
};

made.length = 0;
run("renderVtl(" + JSON.stringify(data) + ", " + JSON.stringify(day) + ");");
const spans = made.filter(e => /(^|\s)vevspan/.test(e.className));
const has = c => spans.some(s => new RegExp("(^|\\s)" + c + "(\\s|$)").test(s.className));
const tops = spans.map(s => parseFloat(s.style.top));
console.log("  时段块 " + spans.length + " 个 · top=" + JSON.stringify(tops));

ck("跨断录的活动被切成 2 块 + 断录里的活动留 1 个虚线框 = 3 个块",
   spans.length === 3, spans.length);
ck("断录里的活动是 gap 虚线框（不画类别色条）",
   has("gap") && spans.filter(s => /(^|\s)gap/.test(s.className)).length === 1,
   spans.map(s => s.className));
ck("虚线框里没有三车道色条（有活动没画面，不能暗示能点开看）",
   spans.filter(s => /(^|\s)gap/.test(s.className)).every(s => !/c-(motion|person|veh)/.test(s._html || "")),
   spans.filter(s => /(^|\s)gap/.test(s.className)).map(s => s._html));
ck("活动块共 2 块带色条（被切开的两段）",
   spans.filter(s => /c-(motion|person|veh)/.test(s._html || "")).length === 2,
   spans.map(s => s._html));
/* 10:10 = 36600 → vtlY 单调，只需比较相对顺序：第一段起点 < 第二段起点 */
const colored = spans.filter(s => /c-(motion|person|veh)/.test(s._html || ""));
ck("两段分别在断录两侧（top 递增，中间隔着断录的像素）",
   colored.length === 2 && parseFloat(colored[0].style.top) < parseFloat(colored[1].style.top),
   colored.map(s => s.style.top));

/* 老服务端：没有 avail 字段 → 活动块不许消失 */
made.length = 0;
const legacy = { ok: true, cam: "cam-01", date: day, now: 12 * 3600,
                 summary: { count: 2, active_sec: 1100 },
                 events: data.events };
run("renderVtl(" + JSON.stringify(legacy) + ", " + JSON.stringify(day) + ");");
const ls = made.filter(e => /(^|\s)vevspan/.test(e.className));
ck("老服务端（无 avail）→ 两个活动各一整块，不被裁没",
   ls.length === 2 && !ls.some(s => /(^|\s)gap/.test(s.className)), ls.length);

/* ---- 蓝黄对齐（0.9.76）----
   真机 2026-10-26 餐厅原样固化。用户截图指出「蓝色和黄色应该对齐」：
   0.9.69 把活动块收进 60px 轨道的最左 19px，于是有活动的地方蓝块被盖掉左边一截、
   只剩右侧 41px 露出来，没活动的地方蓝块又从最左边起 —— 同一根竖线两种左边缘，
   看着像两条并排轨道。
   这里验的是「黄块必须落在蓝块的矩形里」：左/宽由 CSS 保证（render_check 有断言），
   纵向则由每个子段必须被某一段蓝块包住来保证。 */
console.log("— 真机餐厅 2026-10-26：蓝黄对齐 —");
const H = (h, m, s) => h * 3600 + m * 60 + (s || 0);
const ct = {
  ok: true, cam: "cam-mvi5vhzp", date: day, now: H(4, 40),
  summary: { count: 8, active_sec: 2388 },
  avail: { full: [
    [H(3,20,9), H(3,42,2)], [H(3,43,39), H(3,45,32)], [H(3,47,19), H(3,53,8)],
    [H(3,54,45), H(4,5,20)], [H(4,7,11), H(4,13,58)], [H(4,15,33), H(4,16,10)],
    [H(4,17,45), H(4,17,50)], [H(4,19,53), H(4,20,34)], [H(4,22,23), H(4,25,46)],
    [H(4,25,59), H(4,30,0)]
  ], archive: [] },
  events: [
    { s: H(3,30,58), e: H(3,42,1),  cls: {}, mv: 1 },
    { s: H(3,43,39), e: H(3,45,31), cls: {}, mv: 1 },
    { s: H(3,47,19), e: H(3,53,6),  cls: {}, mv: 1 },
    { s: H(3,54,45), e: H(4,5,18),  cls: {}, mv: 1 },
    { s: H(4,7,11),  e: H(4,13,49), cls: {}, mv: 1 },
    { s: H(4,15,33), e: H(4,16,9),  cls: {}, mv: 1 },
    { s: H(4,19,53), e: H(4,20,32), cls: {}, mv: 1 },
    { s: H(4,22,23), e: H(4,25,45), cls: {}, mv: 1 }
  ]
};
made.length = 0;
run("renderVtl(" + JSON.stringify(ct) + ", " + JSON.stringify(day) + ");");
const rails = made.filter(e => /(^|\s)vavail/.test(e.className))
                  .map(e => ({ t: parseFloat(e.style.top), h: parseFloat(e.style.height) }));
/* 色块的高度挂在里面的 <i> 上（容器只定位），虚线框才把高度放容器 */
const blk = made.filter(e => /(^|\s)vevspan/.test(e.className))
                .map(e => {
                  const m = /height:([\d.]+)px/.exec(e._html || "");
                  return { t: parseFloat(e.style.top),
                           h: m ? parseFloat(m[1]) : parseFloat(e.style.height),
                           gap: /(^|\s)gap/.test(e.className) };
                });
const inside = b => rails.some(r => b.t >= r.t - 0.5 && b.t + b.h <= r.t + r.h + 0.5);
console.log("  蓝块 " + rails.length + " 段 · 活动块 " + blk.length + " 个");
ck("蓝块 10 段（断录留洞，与真机 /api/ledger 一致）", rails.length === 10, rails.length);
ck("8 个活动都有画面 → 全是色块，没有虚线框",
   blk.length === 8 && blk.every(b => !b.gap), blk.map(b => b.gap));
ck("每个活动块都完整落在某一段蓝块里（黄块不越出蓝块）",
   blk.every(inside), blk.filter(b => !inside(b)));
ck("04:22:23 那条：块顶端与蓝块顶端差 < 1px（用户截图的当前位置）",
   (() => {
     const b = blk[blk.length - 1];
     const r = rails.find(x => x.t + x.h >= b.t + b.h - 0.5 && x.t <= b.t + 0.5);
     return r && Math.abs(r.t - b.t) < 1 && Math.abs((r.t + r.h) - (b.t + b.h)) < 4;
   })(), blk[blk.length - 1]);

console.log("\navail_gap: " + pass + " passed, " + fails.length + " failed");
process.exit(fails.length ? 1 : 0);
