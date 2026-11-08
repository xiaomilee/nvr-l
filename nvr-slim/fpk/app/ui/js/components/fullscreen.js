/* ================= components/fullscreen.js =================
   全屏与横屏：Fullscreen API → orientation.lock → CSS 旋转 三级降级，
   外加 Esc 兜底、resize 自纠、以及页面上那几个全屏/播放按钮的接线。
   判断标准：凡是"退出路径"相关的都放这里 —— 这条链路历史上最凶的 bug 是
   "进得去出不来"，把降级、监听、按钮拆散到三处就再也看不出还缺哪一级。
   ============================================================== */

let fsRotTimer = 0;
window.addEventListener("resize", ()=>{
  layout(); if(state.view==="player") drawScene();
  /* 全屏横屏自纠：转屏/锁定生效都会触发 resize。防抖 250ms 取最终方向，
     避免过渡帧把旋转误撤后不再恢复（v0.9.51 真机仍竖屏的可疑原因之一） */
  if(state.land){
    clearTimeout(fsRotTimer);
    fsRotTimer = setTimeout(fsSyncRot, 250);
  }
});

/* ================= 全屏横屏 =================
   目标：点全屏 → 真的进全屏 + 尽量横过来，并且**永远有路退出**。

   三级降级（任何一步失败都不影响下一步）：
     1. Fullscreen API  元素级全屏（真全屏，地址栏也没了）
     2. orientation.lock('landscape')  仅手机浏览器在真全屏下可用；
        桌面 Chrome / Safari / HTTP 页面一律 reject，所以必须 try/catch 后继续
     3. CSS 旋转          锁不了方向时，把画面转 90° 并让容器铺满视口
                          —— 用户要的是"横过来看"，不是"调用成功"

   为什么以前"点了没反应"：`state.land` 只是给 .phone 加了个类（844×390 的
   模拟尺寸），在手机上那只是把内容挤进一个小框，并没有进全屏；而
   orientation.lock 的 rejection 被空 catch 吞了，什么提示都没有。
*/
function isFullscreen(){
  return !!(document.fullscreenElement || document.webkitFullscreenElement);
}
function fsElement(){
  return document.fullscreenElement || document.webkitFullscreenElement || null;
}

/* 横屏状态：land=容器按横向排版；rot=没有真横屏，只能把画面转 90° */
function setLand(on){
  state.land = !!on;
  phone.classList.toggle("land", state.land);
  document.body.classList.toggle("fsmode", !!on);
  if(!on){
    state.rot = false;
    phone.classList.remove("rot");
    document.body.classList.remove("rot");
  }
  layout();
}

/* 画面旋转兜底：真的转不过来时，把舞台转 90° 铺满。
   这里任何异常都不能往上抛——真机上曾因这步出错导致退出按钮/旋转一起丢失 */
function applyRotate(on){
  state.rot = !!on;
  try{
    phone.classList.toggle("rot", state.rot);
    document.body.classList.toggle("rot", state.rot);
    layout();
  }catch(e){}
  fsDiag();
}

/* 全屏自诊断角标：默认不显示（界面干净），排障时 URL 带 ?fsdiag=1 才出现 */
function fsDiagWanted(){
  try{
    if(/(?:^|[?&])fsdiag=1/.test(location.search)) return true;
    return localStorage.getItem("fsdiag") === "1";
  }catch(e){ return false; }
}
function fsDiag(){
  let d = document.getElementById("fsDiag");
  if(!d){
    d = document.createElement("div");
    d.id = "fsDiag";
    (phone || document.body).appendChild(d);   /* 挂在 .phone 内 → 跟着旋转 */
  }
  const on = (state.land || isFullscreen()) && fsDiagWanted();
  d.classList.toggle("on", !!on);
  if(!on) return;
  d.textContent = window.innerWidth + "×" + window.innerHeight +
    " · 旋转" + (state.rot ? "开" : "关");
}

/* 手动旋转偏好：on/off 优先于自动判断（真机某 webview 自动算不出来时的兜底） */
function rotPref(){
  try{ return localStorage.getItem("fsRot") || ""; }catch(e){ return ""; }
}
function setRotPref(v){
  try{ v ? localStorage.setItem("fsRot", v) : localStorage.removeItem("fsRot"); }catch(e){}
}

/* 横屏状态同步：以「实际视口方向」为准（CSS 旋转不会改变 innerWidth/Height，
   所以视口横向 = 系统真的转过去了）。多个检查点独立求值，过渡帧误判后能自愈 */
function fsSyncRot(){
  if(!state.land) return;
  const pref = rotPref();
  const want = pref ? pref === "on" : (window.innerHeight > window.innerWidth);
  if(want !== state.rot) applyRotate(want);
  else fsDiag();
}

/* ================= 黑边工具轨 =================
   全屏横屏时画面 contain 会左右各留一条黑边（宽度 = (舞台宽 - 舞台高×画幅)/2）。
   黑边是"白送"的地方：把操作条排进左轨、退出/旋转排进右轨，画面就一点不挡了。
   黑边太窄（<44px）时退回 0.9.54 的底栏悬浮布局——宁可压一点也不能把按钮挤成 20px。 */
var vidAsp = 16 / 9;      /* 摄像机真实画幅，元数据到了就更新（4:3 机位也能算对）；var 防 TDZ */
function syncRail(){
  const st = document.getElementById("playStage");
  const pg = document.getElementById("page-play");
  const fs = document.body.classList.contains("fsmode");
  let barw = 0, land = false;
  if(fs && st && st.clientWidth && st.clientHeight){
    const w = st.clientWidth, h = st.clientHeight;
    land = w > h;
    if(land && vidAsp > 0.2) barw = Math.round((w - h * vidAsp) / 2);
  }
  const rail = !!fs && land && barw >= 44;
  try{
    document.body.classList.toggle("rail", rail);
    if(pg) pg.classList.toggle("rail", rail);
    document.documentElement.style.setProperty("--railw",
      (rail ? Math.min(58, Math.max(44, barw - 6)) : 56) + "px");
  }catch(e){}
  return { rail: rail, barw: barw };
}

/* 全屏工具组容器：**挂在 .phone 里**（跟着旋转走，永远在用户视角右上角）。
   两颗按钮横排、间距 8px、触控区 40×40 以上，不会互相挤压 */
function ensureFsTools(){
  let box = document.getElementById("fsTools");
  if(box) return box;
  box = document.createElement("div");
  box.id = "fsTools";
  (phone || document.body).appendChild(box);
  return box;
}

/* 旋转按钮：全屏期间常驻，一键切换横/竖屏 */
function ensureRotBtn(){
  let b = document.getElementById("fsRot");
  if(b) return b;
  b = document.createElement("button");
  b.id = "fsRot";
  b.type = "button";
  b.title = "切换横屏 / 竖屏";
  b.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 8V4h4M20 16v4h-4M4 4l6 6M20 20l-6-6"/></svg><span>旋转</span>';
  b.onclick = (e)=>{
    e.stopPropagation();
    applyRotate(!state.rot);
    setRotPref(state.rot ? "on" : "off");    /* 本次全屏内记住，退出全屏后清掉重新自动判断 */
    layout();
    try{ requestAnimationFrame(()=>{ drawScene(); syncVnowline(); fsSyncRot(); }); }catch(_){}
  };
  ensureFsTools().appendChild(b);
  return b;
}

/* 退出按钮：全屏时浮在右上角。桌面端和手机端都显示，这是最可靠的出口。 */
function ensureExitBtn(){
  let b = document.getElementById("fsExit");
  if(b) return b;
  b = document.createElement("button");
  b.id = "fsExit";
  b.type = "button";
  b.title = "退出全屏（Esc）";
  b.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 3v6H3M15 3v6h6M15 21v-6h6M9 21v-6H3"/></svg><span>退出</span>';
  b.onclick = (e)=>{ e.stopPropagation(); exitFull(); };
  ensureFsTools().appendChild(b);
  return b;
}

async function enterFull(target, opts){
  opts = opts || {};
  /* 关键：永远对整个文档根进全屏，**不要**对页面元素进全屏。
     元素级全屏会把该元素提到浏览器顶层（top layer），UA 样式给它
     position:fixed，彻底脱离 .phone 的旋转变换——画面永远转不过来；
     同时它会盖住挂在 body 上的退出/旋转按钮（真机三轮"全屏仍竖屏 +
     按钮消失"都是这一个原因）。CSS 布局由 body.fsmode 负责，与全屏元素无关。 */
  const el = document.documentElement;

  /* ① 先把横屏做完（同步、不等任何异步）——真机上 requestFullscreen /
     orientation.lock 都可能挂起，旋转不能排在它们后面 */
  const portrait = window.innerHeight > window.innerWidth;
  state.fsAt = Date.now();      /* fullscreenchange 防抖用：吃掉进入瞬间的补发事件 */
  setLand(true);
  /* 实时层自己有「退出全屏」（顶部+底部各一个），不再叠一个全局的浮标 ——
     两个退出按钮压在同一角，反而不知道点哪个。 */
  if(!opts.noExit) ensureExitBtn().removeAttribute("hidden");
  else { const b = document.getElementById("fsExit"); if(b) b.setAttribute("hidden", ""); }
  ensureRotBtn().removeAttribute("hidden");
  ensureFsTools().classList.add("on");

  const pref = rotPref();
  applyRotate(pref === "on" ? true : pref === "off" ? false : portrait);

  /* ② 再请求真全屏（拿不到就当铺满，不影响已经做好的横屏） */
  let ok = false;
  try{
    if(el.requestFullscreen) { await el.requestFullscreen({ navigationUI: "hide" }); ok = true; }
    else if(el.webkitRequestFullscreen) { el.webkitRequestFullscreen(); ok = true; }
  }catch(e){
    /* 用户手势过期 / iframe 没 allowfullscreen / 浏览器策略禁止 —— 都退到容器铺满 */
    ok = false;
  }

  /* ③ 方向锁：竞速 800ms，挂起/假成功都不阻塞（旋转已经在①里做完了） */
  let locked = false;
  try{
    const so = screen.orientation;
    if(so && so.lock){
      await Promise.race([
        so.lock("landscape").then(()=>{ locked = true; }).catch(()=>{}),
        new Promise(r=>setTimeout(r, 800))
      ]);
    }
  }catch(e){ locked = false; }

  /* ④ 多个检查点校准：lock 真生效 → 视口变横向 → 撤掉 CSS 旋转；
     过渡帧误判也能在后面的检查点自愈。手动偏好始终优先 */
  [0, 400, 1000, 2000].forEach(ms=>setTimeout(fsSyncRot, ms));
  /* 全屏/旋转都可能改变视口尺寸，等一帧再量；回放页的时刻线位置也跟着重算 */
  requestAnimationFrame(()=>{ layout(); drawScene(); syncVnowline(); fsDiag(); });

  if(!ok){
    toast("toast2", "浏览器不允许真全屏 · 已切换为铺满横屏（Esc 退出）");
  }else if(state.rot){
    toast("toast2", "已全屏 · 画面已转横屏（右上角「旋转」可切回）");
  }else{
    toast("toast2", "已进入全屏横屏 · 右上角或 Esc 退出");
  }
}

async function exitFull(){
  try{
    if(document.exitFullscreen) await document.exitFullscreen();
    else if(document.webkitExitFullscreen) document.webkitExitFullscreen();
  }catch(e){}
  try{ if(screen.orientation && screen.orientation.unlock) screen.orientation.unlock(); }catch(e){}
  const box = document.getElementById("fsTools");
  if(box) box.classList.remove("on");
  /* 本次的手动旋转选择随退出作废——下次进全屏重新按视口方向自动判断，
     免得上次选了"关"之后再也自动转不过来（真机就是这么卡住的） */
  setRotPref("");
  setLand(false);
  fsDiag();
  /* 实时层的全屏退出 = 回到首页原来的位置与列数（不是留在一个黑底的播放层里） */
  const room = document.getElementById("liveRoom");
  if(room && room.classList.contains("on") && state.lrFs){
    state.lrFs = false;
    closeLiveRoom();
  }
  requestAnimationFrame(()=>{ layout(); drawScene(); syncVnowline(); });
  toast("toast2", "已退出全屏");
}

function toggleFull(target){
  if(isFullscreen() || state.land) return exitFull();
  return enterFull(target);
}

/* 浏览器自己退出全屏（按 Esc / 手势 / 切走）时同步 UI，别让按钮状态和数据打架。
   进入后的 600ms 内的 change 事件一律忽略——部分 webview 在 requestFullscreen
   resolve 之后会补发一次「空」fullscreenchange，照单全收会把刚进的全屏直接退掉 */
["fullscreenchange","webkitfullscreenchange"].forEach(ev=>
  document.addEventListener(ev, ()=>{
    if(Date.now() - (state.fsAt || 0) < 600) return;
    if(!isFullscreen()){
      const box = document.getElementById("fsTools");
      if(box) box.classList.remove("on");
      setRotPref("");            /* 手动旋转只管本次全屏，退出即作废（否则下次自动判断被压住） */
      if(state.land) setLand(false);
      fsDiag();
      const room = document.getElementById("liveRoom");
      if(room && room.classList.contains("on") && state.lrFs){
        state.lrFs = false;
        closeLiveRoom();
      }
    }
  })
);
/* Esc 兜底：部分 WebView 不派发 fullscreenchange */
document.addEventListener("keydown", e=>{
  if(e.key === "Escape" && (isFullscreen() || state.land)){ e.preventDefault(); exitFull(); }
});

const _btnLiveFull = document.getElementById("btnLiveFull");
if(_btnLiveFull) _btnLiveFull.onclick = ()=> toggleFull();
const _lrFull = document.getElementById("lrFull");
if(_lrFull) _lrFull.onclick = ()=> toggleFull();
const _lrPlay = document.getElementById("lrPlay");
if(_lrPlay) _lrPlay.onclick = e=>{ e.stopPropagation(); toggleLiveStream(); };
