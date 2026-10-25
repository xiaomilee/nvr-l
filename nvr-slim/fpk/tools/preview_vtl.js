/* 时间轴视觉校验：把 index.html 的真实 CSS + 真实 renderVtl 跑出静态快照。
   目的不是替代浏览器，而是把「三格尾巴」的实际几何摊开看一眼——
   单测只能验结构，重叠/错位这种问题得靠眼睛。
   跑法： node tools/preview_vtl.js  → 生成 vtl_preview.html */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
              .replace(/\r\n/g, "\n");
const styles = (src.match(/<style>([\s\S]*?)<\/style>/g) || [])
  .map(b => b.replace(/^<style>/, "").replace(/<\/style>$/, "")).join("\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1]
  .replace(/^<script>/, "").replace(/<\/script>$/, "");

/* ---- 能序列化的 DOM 桩 ---- */
const ESC = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                          .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
function mkEl(tag) {
  const el = {
    tag: tag || "div", className: "", style: {}, dataset: {}, children: [],
    _html: "", textContent: "", id: "",
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, v) { if (v === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (v) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); }
    },
    appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, insertBefore() {}, remove() {},
    setAttribute() {}, getAttribute() { return null; },
    addEventListener() {}, removeEventListener() {},
    querySelectorAll() { return []; },
    querySelector() { return mkEl("div"); },
    getBoundingClientRect() { return { width: 390, height: 600, left: 0, top: 0, right: 390, bottom: 600 }; },
    getContext() { const g = { addColorStop() {} }; return new Proxy({}, { get: () => () => g }); },
    setPointerCapture() {}, releasePointerCapture() {}, scrollIntoView() {}, focus() {}, blur() {},
    closest() { return null; }, cloneNode() { return mkEl(this.tag); },
    children0: [], childNodes: [{ nodeValue: "", textContent: "" }],
    firstChild: { nodeValue: "", textContent: "" }, parentNode: null,
    onclick: null, oninput: null, onchange: null, value: "", width: 0, height: 0,
    clientHeight: 600, clientWidth: 390, offsetTop: 0, offsetHeight: 600, scrollTop: 0
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html; },
    set(v) { el._html = String(v); el.children.length = 0; }
  });
  return new Proxy(el, {
    get(t, k) {
      if (k in t) return t[k];
      if (typeof k === "string" && /^(on|add|remove|set)/.test(k)) return () => {};
      return undefined;
    },
    set(t, k, v) { t[k] = v; return true; }
  });
}
const els = {};
global.document = {
  getElementById(id) { if (!els[id]) { els[id] = mkEl("div"); els[id].id = id; } return els[id]; },
  createElement(tag) { return mkEl(tag); },
  querySelectorAll() { return []; },
  querySelector() { return mkEl("div"); },
  addEventListener() {},
  documentElement: { dataset: {} },
  body: { appendChild() {},
          classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } } }
};
global.window = { matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
                  addEventListener() {}, devicePixelRatio: 2, innerWidth: 390, innerHeight: 844 };
global.matchMedia = global.window.matchMedia;
const _ls = Object.create(null);
global.localStorage = { getItem: k => (k in _ls ? _ls[k] : null),
                        setItem: (k, v) => { _ls[k] = String(v); },
                        removeItem: k => { delete _ls[k]; } };
global.location = { search: "" };
global.URLSearchParams = class { get() { return null; } };
global.requestAnimationFrame = () => 0;
global.setTimeout = () => 0;
global.setInterval = () => 0;
global.performance = { now: () => 0 };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });

const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);

/* ---- 一天的真实形状（取自真机台账的统计特征：中位 2 秒、少量长驻留）---- */
const T = (h, m, s) => h * 3600 + m * 60 + (s || 0);
const day = "2026-10-25";
const data = {
  ok: true, cam: "cam-01", date: day,
  avail: { full: [[T(6, 30), T(13, 0)]], archive: [] },
  events: [
    { s: T(6, 31, 12), e: T(6, 31, 40), d: 0.3, cls: { vehicle: 2 }, mv: 1 },          // 车进出 28s
    { s: T(7, 12, 3),  e: T(7, 12, 41), d: 0.28, cls: { person: 1 }, mv: 1 },          // 人走过 38s
    { s: T(8, 5, 0),   e: T(8, 5, 2),   d: 0.2, cls: {}, mv: 1 },                      // 纯帧差 2s
    { s: T(8, 40, 10), e: T(9, 20, 30), d: 0.2, cls: { vehicle: 3 }, mv: 1 },          // 车停 40 分钟
    { s: T(10, 2, 15), e: T(10, 2, 27), d: 0.35, cls: { person: 2 }, mv: 1 },          // 短事件 12s
    { s: T(10, 45, 0), e: T(10, 45, 5), d: 0.3, cls: { person: 1, vehicle: 1 }, mv: 1 }, // 混类 5s
    { s: T(11, 30, 0), e: T(11, 30, 3), d: 0.3, cls: { person: 1 }, mv: 0 },           // 无帧差佐证
    { s: T(12, 10, 0), e: T(12, 10, 55), d: 0.4, cls: { person: 1, vehicle: 2 }, mv: 1 },
    { s: T(12, 40, 0), e: T(12, 40, 8), d: 0.2, cls: {}, mv: 1 }
  ]
};
els.vtlScroll.clientHeight = 600;
els.vtlScroll.offsetTop = 0;
run("renderVtl(" + JSON.stringify(data) + "," + JSON.stringify(day) + ")");

/* 只截取某一段：SCROLL_H（环境变量）指定视口顶部压着的钟点，默认 9:55 */
const pxh = run("vtlPxh"), top = run("vtlTop()");
const sh = Number(process.env.SCROLL_H || 9 + 55 / 60);
els.vtlScroll.scrollTop = Math.max(0, top + sh * 3600 / 3600 * pxh - 120);

function ser(el) {
  const cls = el.className || (el.classList._s.size ? [...el.classList._s].join(" ") : "");
  const st = Object.keys(el.style || {}).filter(k => el.style[k] != null && el.style[k] !== "")
    .map(k => k.replace(/[A-Z]/g, m => "-" + m.toLowerCase()) + ":" + el.style[k] + ";").join("");
  return "<" + el.tag + (cls ? ' class="' + cls + '"' : "") + (st ? ' style="' + st + '"' : "") + ">"
    + (el._html || el.textContent || "") + el.children.map(ser).join("") + "</" + el.tag + ">";
}

const out = `<!doctype html><html><head><meta charset="utf-8">
<title>时间轴视觉校验 · 0.9.69</title>
<style>${styles}
html,body{margin:0;background:var(--bg,#0f1115);}
.wrap{max-width:430px;margin:0 auto;padding:14px;}
.vtlscroll{height:600px;overflow:hidden;}
.note{font:12px/1.7 -apple-system,sans-serif;color:#8a93a6;padding:10px 2px 0}
.note b{color:#cfd6e4}
</style></head><body><div class="wrap">
<div class="vtlcard" id="vtlCard">
  <div class="heatcap"><b id="vtlTitle">全天</b><span id="vtlSum">9 个活动 · 累计 46 分</span>
    <div class="vtlzoom"><button>−</button><b id="vtlPct">×1.0</b><button>＋</button></div>
  </div>
  <div class="vtlscroll" id="vtlScroll" style="position:relative">
    <div class="vtl" id="vtl" style="position:relative;top:-${els.vtlScroll.scrollTop}px">${els.vtl.children.map(ser).join("")}</div>
  </div>
  <div class="vnowline" id="vnowline" style="position:absolute"><em id="vnowTime">10:03:00</em><i></i></div>
</div>
<div class="note">
  <b>看什么：</b>橙/蓝/绿三格各自独立；短事件的尾巴是 4px 地板（像一根线）；
  8:40 那辆停了 40 分钟的车在 10:00 附近之外，滚上去能看到一条很长的绿尾巴；
  横线的中心应当正好压在尾巴顶端。
</div></div></body></html>`;

fs.writeFileSync(path.join(ROOT, "..", "vtl_preview.html"), out, "utf8");
console.log("vtl_preview.html 已生成，元素 " + els.vtl.children.length + " 个");
