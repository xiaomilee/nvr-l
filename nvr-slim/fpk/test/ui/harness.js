/* 极简 DOM 桩：只为把 index.html 的脚本完整跑一遍，抓运行时错误。
   它不是浏览器替代品，验证的是「渲染函数不抛异常 + 产出结构正确」。 */
const els = {};
function mkEl(id){
  const el = {
    id, _html:"", style:{}, dataset:{},
    classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    width:0, height:0, value:"", textContent:"", onclick:null, oninput:null, onchange:null,
    childNodes:[{ nodeValue:"", textContent:"", style:{} }],
    children:[], firstChild:{ nodeValue:"", textContent:"" }, parentNode:null,
    appendChild(){}, removeChild(){}, insertBefore(){}, remove(){},
    setAttribute(){}, getAttribute(){ return null; },
    addEventListener(){}, removeEventListener(){},
    scrollIntoView(){}, focus(){}, blur(){},
    cloneNode(){ return mkEl("c"); },
    setPointerCapture(){}, releasePointerCapture(){},
    closest(){ return null; },
    getBoundingClientRect(){ return { width:320, height:180, left:0, top:0, right:320, bottom:180 }; },
    getContext(){
      const grad = { addColorStop(){}, width:12 };
      const noop = ()=>grad;
      return new Proxy({}, { get:()=>noop });
    },
    querySelectorAll(){ return []; },
    querySelector(){ return mkEl("q"); }
  };
  Object.defineProperty(el, "innerHTML", {
    get(){ return el._html; },
    set(v){ el._html = String(v); }
  });
  return el;
}
global.document = {
  getElementById(id){ if(!els[id]) els[id] = mkEl(id); return els[id]; },
  querySelectorAll(){ return []; },
  querySelector(){ return mkEl("qs"); },
  createElement(){ return mkEl("tmp"); },
  /* SVG 元素（0.9.90 起 renderVtl 会建一张 <svg> 承载「事件线 → 气泡」的曲线） */
  createElementNS(ns, tag){ return mkEl(tag || "svg"); },
  addEventListener(){},
  documentElement:{ dataset:{} },
  body:{ appendChild(){}, classList:{ add(){}, remove(){}, toggle(){} } }
};
global.window = {
  matchMedia: ()=>({ matches:false, addEventListener(){}, addListener(){} }),
  addEventListener(){}, devicePixelRatio:2, innerWidth:390, innerHeight:844
};
global.matchMedia = global.window.matchMedia;
/* 内存版 localStorage。
   以前是空转桩（getItem 永远 null、setItem 什么都不做），于是所有
   "存进去再读回来"的断言都不可能通过 —— 测试等于在验证一个假实现。 */
const _ls = Object.create(null);
global.localStorage = {
  getItem: k => (k in _ls ? _ls[k] : null),
  setItem: (k, v) => { _ls[k] = String(v); },
  removeItem: k => { delete _ls[k]; },
  clear: () => { Object.keys(_ls).forEach(k => delete _ls[k]); },
};
global.location = { search:"" };
global.URLSearchParams = class { get(){ return null; } };
global.requestAnimationFrame = ()=>0;
global.setTimeout = ()=>0;
global.setInterval = ()=>0;
global.performance = { now: ()=>0 };
global.getComputedStyle = ()=>({ getPropertyValue:()=>"" });
module.exports = { els, mkEl };
