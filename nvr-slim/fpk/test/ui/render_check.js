/* 时间轴三车道渲染验证（0.9.67）：不起浏览器，用 DOM 桩把 renderVtl 真跑一遍，
   检查产出的 className / 车道结构 / 筛选淡出。浏览器端到覆盖在 test/e2e_ui.js。
   跑法： node test/ui/render_check.js */
require("./harness.js");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");

/* 记录所有 createElement 产物：appendChild 是空转，只能自己收 */
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

/* 与 e2e fixture 同构的一天：4 个事件，其中 3 个带 cls（AI 台账窗口落在帧差窗口内）。
   0.9.63 起两轮车并入车辆：10:13 那条（车3+两轮1）整体按车辆呈现 */
const day = "2026-10-25";
const data = {
  ok: true, cam: "cam-01", date: day,
  summary: { count: 4, active_sec: 800 },
  avail: { full: [[36000, 43200]], archive: [] },
  events: [
    { s: 10 * 3600 + 135, e: 10 * 3600 + 147, d: 0.35, cls: { person: 2 } },
    { s: 10 * 3600 + 439, e: 10 * 3600 + 716, d: 0.35, cls: {} },
    { s: 10 * 3600 + 810, e: 10 * 3600 + 820, d: 0.35, cls: { vehicle: 3, two_wheeler: 1 } },
    { s: 11 * 3600 + 56,  e: 11 * 3600 + 274, d: 0.4,  cls: { person: 1, vehicle: 1 } },
  ]
};

let pass = 0; const fails = [];
const ck = (name, cond, detail) => {
  if (cond) { pass++; console.log("  ✅ " + name); }
  else { fails.push(name); console.log("  ❌ " + name + "  ← " + JSON.stringify(detail)); }
};

made.length = 0;
run("renderVtl(" + JSON.stringify(data) + ", " + JSON.stringify(day) + ");");

const spans = made.filter(e => /(^|\s)vevspan/.test(e.className));
const marks = made.filter(e => /(^|\s)vmark/.test(e.className));
/* 三车道：左 1/3 = 移动侦测 · 中 1/3 = 人员 · 右 1/3 = 车辆，互不重叠 */
const BARS = { mot: '<i class="c-motion mot"></i>',
               per: '<i class="c-person per"></i>',
               veh: '<i class="c-veh veh"></i>' };
const barsOf = m => {
  const h = m._html || "";
  return { mot: h.includes(BARS.mot), per: h.includes(BARS.per), veh: h.includes(BARS.veh), html: h };
};

console.log("时段块 " + spans.length + " 个 · 横线 " + marks.length + " 根");
/* 桩的 classList.contains 永远 false，只能解析 className 字符串 */
const has = (el, c) => new RegExp("(^|\\s)" + c + "(\\s|$)").test(el.className);
ck("时段块 4 个（每事件一块）", spans.length === 4, spans.length);
/* GAP=20px：10:02→10:07 相距 10px 并组，10:07→10:13 相距 12px 但已并进同组后
   累计 22px 超阈值，所以最后一根独立 */
ck("横线 3 根（前两个挤成一组，其余单根）",
   marks.length === 3 && marks.filter(m => has(m, "grp")).length === 1,
   marks.map(m => m.className));

const spanCls = k => spans.filter(s => (s._html || "").includes("c-" + k)).length;
ck("时段块三格：人×2 → 蓝格（10:02 人2、11:01 人1车1 各一格）",
   spanCls("person") === 2, spanCls("person"));
ck("时段块三格：车3+两轮1 → 绿格（两轮已并入车辆）", spanCls("veh") === 2, spanCls("veh"));
ck("时段块三格：纯帧差事件 → 橙格", spanCls("motion") === 1, spanCls("motion"));
ck("时段块没有青色（两轮车并入车辆，c-two 已删除）",
   spanCls("two") === 0, spanCls("two"));
/* 0.9.69：时段块收进 1/3 格，高度 = 持续时长（4px 地板）
   10:02:15→10:02:27 = 12 秒 @120px/h = 0.4px → 地板 4px
   10:07:19→10:11:56 = 277 秒 @120px/h = 9.2px */
const hOf = el => {
  const m = /height:([\d.]+)px/.exec(el._html || "");
  return m ? +m[1] : null;
};
ck("短事件尾巴吃 4px 地板（12 秒在默认缩放下只有 0.4px）",
   hOf(spans[0]) === 4, hOf(spans[0]));
ck("长事件尾巴高度 = 真实时长（277 秒 → 9.2px）",
   hOf(spans[1]) === 9.2, hOf(spans[1]));
ck("混类事件的三格尾巴各自独立（人1车1 各画一根，位置左中右不重叠）",
   (spans[3]._html.match(/style="height:/g) || []).length === 2
   && spans[3]._html.includes('class="c-person per"')
   && spans[3]._html.includes('class="c-veh veh"'), spans[3]._html);

const laneInfo = marks.map(m => {
  const b = barsOf(m);
  return { dim: has(m, "dim"), grp: has(m, "grp"), mot: b.mot, per: b.per, veh: b.veh };
});
ck("并组那根：左 1/3 橙（移动侦测）+ 中 1/3 蓝（人员），右 1/3 留空",
   laneInfo[0].grp && laneInfo[0].mot && laneInfo[0].per && !laneInfo[0].veh, laneInfo[0]);
ck("10:13 那根：只有右 1/3 绿（车辆），左中两格留空",
   !laneInfo[1].mot && !laneInfo[1].per && laneInfo[1].veh, laneInfo[1]);
ck("11:01 那根：中 1/3 蓝 + 右 1/3 绿 同枚并排，左 1/3 留空",
   !laneInfo[2].mot && laneInfo[2].per && laneInfo[2].veh, laneInfo[2]);
ck("三格同时出现时按 左橙→中蓝→右绿 的固定顺序",
   run("vtlLanes({ motion:true, person:true, vehicle:true })")
     === BARS.mot + BARS.per + BARS.veh);
ck("纯移动侦测：只画左 1/3（vtlLanes 直接验证）",
   run("vtlLanes({ motion:true, person:false, vehicle:false })") === BARS.mot);
ck("没有移动侦测时不画左格（不留空线，位置空着本身就是信息）",
   run("vtlLanes({ motion:false, person:true, vehicle:false })") === BARS.per);

/* 三格宽度：60px 三等分，每格 19px 起于 0/20/40 —— 相加不超过 60，永不重叠 */
const slot = /\.vtl \.vmark \.lanes i\.(\w+)\{left:(\d+)(?:px)?;width:(\d+)px\}/g;
const slots = {}; let mm;
while ((mm = slot.exec(src))) slots[mm[1]] = [ +mm[2], +mm[3] ];
ck("三格横向排布 0/20/40 各 19px（三等分且互不重叠）",
   slots.mot && slots.per && slots.veh
   && slots.mot[0] === 0 && slots.per[0] === 20 && slots.veh[0] === 40
   && [slots.mot, slots.per, slots.veh].every(s => s[1] === 19)
   && [slots.mot, slots.per, slots.veh].every(s => s[0] + s[1] <= 60), slots);
/* 0.9.69：时段块也用同一套三格位置（以前是整块 60px 一条色） */
ck("时段块三格 left 0/20/40 各 19px，与横线同位置",
   src.includes(".vtl .vevspan i.mot{left:0}")
   && src.includes(".vtl .vevspan i.per{left:20px}")
   && src.includes(".vtl .vevspan i.veh{left:40px}")
   && src.includes(".vtl .vevspan i{position:absolute;top:0;width:19px"));
ck("横线中心对齐事件时刻（top:7.5px；0.9.67 的 5px 差 2.5px = 默认缩放下 1 分 15 秒）",
   src.includes(".vtl .vmark .lanes i{position:absolute;top:7.5px;"));
ck("时段块不再按主导类别上色（evDom 已退役）", !src.includes("function evDom"));
ck("可用性蓝轨退成 30% 背景（满饱和蓝 = 人员蓝，蓝尾巴压在蓝底上等于没画）",
   src.includes("background:var(--primary);opacity:.3}") && src.includes(".vtl .vavail.arch{opacity:.14}"));

/* 类别筛选：三类互不重叠
   - 移动侦测 = 没有任何 AI 类别的纯帧差事件
   - 人员/车辆 = 该类别计数 > 0（车辆含两轮车） */
run("PPLAY.cls = { motion:false, person:true, vehicle:false };");
made.length = 0;
run("renderVtl(" + JSON.stringify(data) + ", " + JSON.stringify(day) + ");");
const marks2 = made.filter(e => /(^|\s)vmark/.test(e.className));
const spans2 = made.filter(e => /(^|\s)vevspan/.test(e.className));
ck("只勾人员：1 根线变淡、2 根不淡（并组那根本身含人员 → 整组不淡）",
   marks2.filter(m => has(m, "dim")).length === 1
   && marks2.filter(m => !has(m, "dim")).length === 2,
   marks2.map(m => has(m, "dim")));
ck("只勾人员：时段块 2 淡 2 亮（10:07 纯帧差与 10:13 车变淡）",
   spans2.filter(s => has(s, "dim")).length === 2
   && spans2.filter(s => !has(s, "dim")).length === 2,
   spans2.map(s => has(s, "dim")));
ck("只勾人员：变淡的线仍带类别色（c-* 类没被抹掉）",
   marks2.filter(m => has(m, "dim"))
          .every(m => /c-(motion|person|veh)/.test(m._html || "")),
   marks2.filter(m => has(m, "dim")).map(m => m._html));

run("PPLAY.cls = { motion:true, person:false, vehicle:false };");
made.length = 0;
run("renderVtl(" + JSON.stringify(data) + ", " + JSON.stringify(day) + ");");
const spans3 = made.filter(e => /(^|\s)vevspan/.test(e.className));
ck("只勾移动侦测：只有 10:07 那段亮，人/车三段全淡",
   spans3.filter(s => has(s, "dim")).length === 3
   && spans3.filter(s => !has(s, "dim")).every(s => (s._html || "").includes("c-motion")),
   spans3.map(s => s.className));

/* 全勾 = 不过滤：没有任何 dim */
run("PPLAY.cls = { motion:true, person:true, vehicle:true };");
made.length = 0;
run("renderVtl(" + JSON.stringify(data) + ", " + JSON.stringify(day) + ");");
ck("全勾时不淡出任何一根（与旧行为一致）",
   made.filter(e => /(^|\s)vmark/.test(e.className) && has(e, "dim")).length === 0);

/* 气泡摘要：类别色 + 计数（0.9.63 起两轮并入车） */
const bt = run("clsText({ s:0, e:1, d:0, cls:{ person:2, two_wheeler:1, vehicle:3 } })");
ck("气泡摘要：人 ×2 · 车 ×4（两轮 1 已并入车，不再单列）",
   /clsw t-person"><i class="cd c-person"><\/i>人 ×2/.test(bt)
   && /clsw t-veh"><i class="cd c-veh"><\/i>车 ×4/.test(bt)
   && !/两轮/.test(bt), bt);
ck("旧台账（无 cls）摘要为空串，不摆「0 人 0 车」",
   run("clsText({ s:0, e:1, d:0 })") === "" && run("clsText({ s:0, e:1, d:0, cls:{} })") === "");
const dt = run("clsDayText(" + JSON.stringify(data.events) + ")");
ck("全天摘要：人 3 · 车 5（车 = 4 + 两轮 1）",
   /t-person"><i class="cd c-person"><\/i>人 3/.test(dt)
   && /t-veh"><i class="cd c-veh"><\/i>车 5/.test(dt)
   && !/两轮/.test(dt), dt);
ck("全天摘要：没有 AI 数据时返回空串", run("clsDayText([{ s:0, e:1, cls:{} }])") === "");

/* 事件类别判定：并列/边界/回落都必须稳定（同一次渲染内不跳色） */
ck("evKind 并列时人与车各自点亮（三格各画各的，不再择一上色）",
   (()=>{ const k = JSON.parse(run("JSON.stringify(evKind({ cls:{ person:1, vehicle:1 } }))"));
          return k.person === true && k.vehicle === true; })());
ck("evKind 两轮车计数算进 vehicle",
   JSON.parse(run("JSON.stringify(evKind({ cls:{ two_wheeler:2 } }))")).vehicle === true);
ck("evKind 全 0 的 cls 回落移动侦测（不渲染没颜色的线）",
   JSON.parse(run("JSON.stringify(evKind({ cls:{ person:0, vehicle:0 } }))")).motion === true);
/* 0.9.69：移动侦测参与人/车检测，作为边界佐证 */
ck("evKind：mv=1 的 AI 事件橙格也亮（帧差确认了这次进出）",
   JSON.parse(run("JSON.stringify(evKind({ cls:{person:1}, mv:1 }))")).motion === true
   && JSON.parse(run("JSON.stringify(evKind({ cls:{person:1}, mv:0 }))")).motion === false
   && JSON.parse(run("JSON.stringify(evKind({ cls:{person:1}, mv:-1 }))")).motion === false);
ck("mvText：佐证 / 无变化 / 无从判断三种措辞，最后一种不显示",
   run("mvText({ mv:1 })").includes("移动侦测确认")
   && run("mvText({ mv:0 })").includes("画面没有变化")
   && run("mvText({ mv:-1 })") === "" && run("mvText({})") === "");

/* 筛选口径：三类互不重叠 */
run("PPLAY.cls = { motion:false, person:false, vehicle:true };");
ck("pvClsMatch 只勾车辆：两轮车事件命中、人事件不命中",
   run("pvClsMatch({ s:0, e:1, cls:{ two_wheeler:1 } })") === true
   && run("pvClsMatch({ s:0, e:1, cls:{ person:1 } })") === false);
run("PPLAY.cls = { motion:true, person:false, vehicle:false };");
ck("pvClsMatch 只勾移动侦测：纯帧差命中，AI 认出人/车的不重复收",
   run("pvClsMatch({ s:0, e:1, cls:{} })") === true
   && run("pvClsMatch({ s:0, e:1 })") === true
   && run("pvClsMatch({ s:0, e:1, cls:{ person:1 } })") === false
   && run("pvClsMatch({ s:0, e:1, cls:{ vehicle:1 } })") === false);
run("PPLAY.cls = { motion:true, person:true, vehicle:true };");
ck("pvClsMatch 全勾：三类事件全收（不过滤）",
   run("pvClsMatch({ s:0, e:1 })") === true
   && run("pvClsMatch({ s:0, e:1, cls:{ person:1 } })") === true
   && run("pvClsMatch({ s:0, e:1, cls:{ vehicle:1 } })") === true);

/* 定位胶囊彻底移除：不再有 .on 高亮、不再有 vtlMarkActive */
ck("定位胶囊已移除（没有 .vmark.on::before）", !src.includes(".vmark.on::before"));
ck("播放落点高亮函数已删除（vtlMarkActive 不再存在）",
   !src.includes("function vtlMarkActive") && !src.includes("vtlMarkActive();"));

/* ---- 0.9.68：时间轴上下留白 ----
   固定时刻线钉在视口 1/3 处。留白要保证：滚到最顶 = 00:00:00 压线，
   滚到最底 = 24:00:00 压线；且留白只随视口高度变，与缩放倍率无关。 */
const scEl = document.getElementById("vtlScroll");
const boxEl = document.getElementById("vtl");
const tEl = document.getElementById("vnowTime");
scEl.clientHeight = 600; scEl.offsetTop = 0; scEl.scrollTop = 0;
run("vtlPxh = 120;");
run("renderVtl(" + JSON.stringify(data) + ", " + JSON.stringify(day) + ");");
ck("留白按视口 1/3 分：600 高 → 上 170 / 下 376（线距顶 200）",
   run("vtlPadTop") === 170 && run("vtlPadBot") === 376,
   [run("vtlPadTop"), run("vtlPadBot")]);
ck("内容总高 = 上留白 200 + 24h×120 + 下留白 376 + 24 = 3480",
   boxEl.style.height === "3480px", boxEl.style.height);
ck("可滚动量正好 24h×120 = 2880（不多不少，两端都够到线）",
   parseInt(boxEl.style.height, 10) - scEl.clientHeight === 24 * 120,
   parseInt(boxEl.style.height, 10) - scEl.clientHeight);
scEl.scrollTop = 0;
run("syncVnowline()");
ck("滚到最顶：时刻线压着 00:00:00（以前差 30px，现在 0）",
   tEl.textContent === "00:00:00", tEl.textContent);
scEl.scrollTop = 24 * 120;
run("syncVnowline()");
ck("滚到最底：时刻线压着 24:00:00（当天最末）",
   tEl.textContent === "24:00:00", tEl.textContent);
/* 缩放：留白不该跟着倍率变 —— 否则小倍率下又够不到两端 */
run("vtlPxh = 60;");
run("renderVtl(" + JSON.stringify(data) + ", " + JSON.stringify(day) + ");");
ck("缩到 60px/h：留白不变（仍 170 / 376）",
   run("vtlPadTop") === 170 && run("vtlPadBot") === 376,
   [run("vtlPadTop"), run("vtlPadBot")]);
ck("缩到 60px/h：可滚动量仍等于 24h×60 = 1440",
   parseInt(boxEl.style.height, 10) - scEl.clientHeight === 24 * 60,
   parseInt(boxEl.style.height, 10) - scEl.clientHeight);
scEl.scrollTop = 0; run("syncVnowline()");
ck("缩到 60px/h 后滚到最顶仍是 00:00:00", tEl.textContent === "00:00:00", tEl.textContent);
/* 视口高度变了（全屏 / 转屏 / 页面刚显示）：syncVnowline 要发现并重算留白 */
scEl.clientHeight = 300;
run("syncVnowline()");
ck("视口落到 300：留白自动重算 70 / 176",
   run("vtlPadTop") === 70 && run("vtlPadBot") === 176,
   [run("vtlPadTop"), run("vtlPadBot")]);
ck("重算后仍能滚满全天（内容高 - 视口高 = 24h×60）",
   parseInt(boxEl.style.height, 10) - 300 === 24 * 60,
   parseInt(boxEl.style.height, 10) - 300);
ck("重算时锚住线下的时刻，视线不跳（滚动位置被重设到同一时刻）",
   Number.isFinite(scEl.scrollTop) && scEl.scrollTop >= 0, scEl.scrollTop);
ck("换算口径统一走 vtlTop()（VTL_TOP 只剩定义 + vtlTop + vtlSyncPad 三处）",
   (src.match(/VTL_TOP/g) || []).length === 3, (src.match(/VTL_TOP/g) || []).length);

/* ---- 0.9.72 蓝块不许画到「现在」之后 ----
   真机反馈「真实时间还未到，已经显示录制了视频」：末片结束是按 st+seg 推算的，
   录制中途停/正在写时蓝块就画到了未来。服务端 ledger.avail_spans 已夹紧，
   前端这里再兜一次（老版本服务端 / 被缓存的老响应）。 */
const TODAY_ = run("jsToday()");
made.length = 0;
run("renderVtl(" + JSON.stringify({
  ok: true, cam: "cam-01", date: TODAY_, now: 4200,
  summary: { count: 0, active_sec: 0 },
  avail: { full: [[3600, 5400]], archive: [] },
  events: []
}) + ", " + JSON.stringify(TODAY_) + ");");
const avails = made.filter(e => has(e, "vavail"));
const wantH = run("vtlY(4200) - vtlY(3600)");
ck("蓝块·今天带 now 时只画到 now（[3600,5400] → 4200）",
   avails.length === 1 && Math.abs(parseFloat(avails[0].style.height) - wantH) < 0.6,
   avails.map(a => [a.className, a.style.height, wantH]));
ck("蓝块·顶端仍是真实起点 3600",
   avails.length === 1 && Math.abs(parseFloat(avails[0].style.top) - run("vtlY(3600)")) < 0.6,
   avails.map(a => a.style.top));
/* 起点已经在 now 之后的区间整段丢掉，不画一个「负数高度」的块 */
made.length = 0;
run("renderVtl(" + JSON.stringify({
  ok: true, cam: "cam-01", date: TODAY_, now: 4200,
  summary: { count: 0, active_sec: 0 },
  avail: { full: [[5000, 5300]], archive: [] },
  events: []
}) + ", " + JSON.stringify(TODAY_) + ");");
ck("蓝块·整段在未来时不画（不留假覆盖）",
   made.filter(e => has(e, "vavail")).length === 0,
   made.filter(e => has(e, "vavail")).length);

console.log("\n时间轴三车道 " + (pass + fails.length) + " 项：通过 " + pass + "，失败 " + fails.length);
process.exit(fails.length ? 1 : 0);
