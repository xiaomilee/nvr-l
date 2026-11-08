/* ================= core/boot.js =================
   页面加载后的启动与对表：读回本地配置 → 渲染各页 → 扫盘 → 接扫描按钮；
   齿轮里的快照刷新频率（1~10 秒）；与服务端配置的定时同步 syncFromServer。
   判断标准：决定「什么时候把哪些页叫起来」「多久跟服务端对一次表」的放这里；
   某一页怎么画在 pages/，请求怎么发在 core/api.js，按钮怎么接在 core/wiring.js。
   为什么单独成文件：启动次序与配置对表是全局的，不属于任何一页；塞进某个
   页面文件，改保留期或刷新率就得跨四五个文件找入口。pushPending 与
   serverCfgStamp 是一对（用户刚改完的配置别被服务端旧值盖回去），别拆散。
   ================================================= */

/* ================= 启动 ================= */
function loadDay(){
  if(!state.camId || !camParams[state.camId]){
    state.curKind = "full"; state.activity = []; state.segs = [];
    renderDates(); renderOverview(); renderHeat(); renderClips();
    renderKindChips(null);
    applyPlaybackVisibility();
    return;
  }
  recalcDayList();
  const day = curDay();
  let kind = dayKind(day);
  /* 回放页用户主动在 full/clip 间切换时，若该天确有该源则尊重其选择；
     文件页没有模式切换按钮，统一按当天磁盘上实际有什么文件走 dayKind ——
     否则第 4~7 天的归档浓缩因没有 full 会被跳过，在文件页彻底消失。 */
  const onPlay = document.getElementById("page-play").classList.contains("on");
  if(onPlay && (state.pmode === "full" || state.pmode === "archive")){
    /* 回放页用户主动切维度时，若该天确有该源则尊重其选择。归档也要走这一支：
       以前只有 full/clip，点时间轴上的归档块会把 pmode 设成 archive，但这里
       不认，又回落 dayKind → 当天有全时就是 full，于是「点归档块播的却是全时」。 */
    const srcKey = state.pmode === "archive" ? "archive" : "cameras";
    if((((state.rec || {})[srcKey] || {})[state.camId] || {})[day])
      kind = state.pmode;
  }
  state.curKind = kind;
  const segs = realSegs(state.camId, day, kind) || [];
  state.activity = segs;   // 磁盘上没有就是没有，绝不拿演示数据充数
  state.segs = segs;
  renderDates(); renderOverview(); renderHeat(); renderClips();
  renderKindChips(day);
  applyPlaybackVisibility();
}

loadCfg();          /* 先把上次保存的摄像头与参数读回来，再渲染 */
/* 清理记录：原来只在「立即预览清理」跑完后刷新，入口删掉后没人调了，
   启动就拉一次，否则卡片上永远是空的。失败不影响渲染。 */
Promise.resolve(loadRetention()).catch(()=>{});
/* 启动顺序：localStorage 只是缓存，服务端的通道列表才是权威。
   必须先 syncFromServer 再渲染，否则会先闪一帧演示数据、并且可能把它推上去。 */
Promise.resolve(syncFromServer()).catch(()=>{}).then(()=>{
  renderDates();
  renderCamTabs();
  renderLive();
  loadDay();
  layout();
  syncLink();         /* 处理队列的真实 pending/done/failed 以服务端为准 */
  loadTopVersion();   /* 设置页顶部的版本徽标（0.9.111） */
  fetchRecordings();  /* 回放页只要磁盘上真实存在的录像，有就点亮 */
  pollLive();         /* 立刻同步一次服务端快照状态（否则 localStorage 里的旧 online 会压住真实状态） */
  refreshRes();       /* CPU / 内存 / 核显的真实值，来自 /api/status */
  /* 资源占用是会变的：开着页面就每 15 秒刷一次（只在这一页可见时） */
  setInterval(()=>{
    const onLive = document.querySelector(".page.on");
    if(onLive && onLive.id === "page-live") refreshRes();
  }, 15000);
  setTimeout(()=>{ renderCamTabs(); renderLive(); }, 60);
  requestAnimationFrame(loop);
});

document.getElementById("btnScan").onclick = ()=> runScan(document.getElementById("btnScan"));

/* ================= 齿轮：快照刷新频率（1~10 秒） =================
   原来这个齿轮是"跳到设置页"的快捷方式，但预览页最常调的其实就是
   刷新快慢 —— 就地弹出比来回翻页顺手。设置页仍可从底部导航进。 */
function renderRatePop(){
  const grid = document.getElementById("rateGrid");
  const now = document.getElementById("rateNow");
  if(!grid) return;
  let html = "";
  for(let n = 1; n <= 10; n++){
    html += '<button data-rate="' + n + '"' + (n === SNAP.interval ? ' class="on"' : "") + '>'
          + n + 's</button>';
  }
  grid.innerHTML = html;
  if(now) now.textContent = SNAP.interval + " 秒";
  grid.querySelectorAll("[data-rate]").forEach(b=>{
    b.onclick = (e)=>{ e.stopPropagation(); setSnapRate(+b.dataset.rate); renderRatePop(); };
  });
}
function setSnapRate(n){
  n = Math.max(1, Math.min(10, Math.round(+n) || 10));
  const same = n === SNAP.interval;
  SNAP.interval = n;
  try{ localStorage.setItem("nvr.snapRate", String(n)); }catch(e){}
  updateLiveHeader();
  renderLive();
  saveCfgSoon();          /* 服务端抓帧线程每轮读一次配置，推上去就生效 */
  toast("toast2", same ? ("快照间隔已是 " + n + " 秒") : ("快照改为每 " + n + " 秒刷新"));
}
function toggleRatePop(e, force){
  const pop = document.getElementById("ratePop");
  if(!pop) return;
  const on = (typeof force === "boolean") ? force : !pop.classList.contains("on");
  if(on){
    const cp = document.getElementById("colPop");
    if(cp) cp.classList.remove("on");       // 两个弹层互斥，别叠在一起
  }
  pop.classList.toggle("on", on);
  if(on) renderRatePop();
  if(e && e.stopPropagation) e.stopPropagation();
}
document.getElementById("btnGoSet").onclick = (e)=> toggleRatePop(e);
document.getElementById("ratePop").onclick = (e)=> e.stopPropagation();
const _colPopEl = document.getElementById("colPop");
if(_colPopEl) _colPopEl.onclick = (e)=> e.stopPropagation();
document.addEventListener("click", ()=>{
  const p = document.getElementById("ratePop");
  if(p) p.classList.remove("on");
  const c = document.getElementById("colPop");
  if(c) c.classList.remove("on");
});
/* 从服务端把「运行时配置」拉回来：分片长度 / 衔接模式 / 快照间隔
   以服务端为准，localStorage 只是缓存 —— 否则 UI 显示 10 分钟、
   recorder 实际按 5 分钟切，两边各说各话。 */
function syncFromServer(){
  if(typeof fetch !== "function") return;
  return API.get("/api/config").then(d=>{
    const c = (d && (d.config || d)) || null;
    if(!c || d.ok === false) return;
    let dirty = false;
    const before = JSON.stringify([LK.segSec, LK.mode, LK.intervalH, REC.dir, REC.enabled]);
    applyServerCfg(c);
    /* 通道列表以服务端为准：NAS 上已经配好的机位，不能因为清了浏览器缓存就消失 */
    if(Array.isArray(c.cameras) && applyServerCams(c.cameras)){
      serverCamsLoaded = true;
      dirty = true;
    }else{
      /* 服务端也没有通道（全新安装）→ 用演示骨架，并允许推送一次
         （这样用户直接在页面上填地址就能开始录，不用先跑安装向导） */
      serverCamsLoaded = true;
    }
    if(c.snapshot_interval){
      const n = Math.max(1, Math.min(10, +c.snapshot_interval || 10));
      if(n !== SNAP.interval){
        SNAP.interval = n;
        try{ localStorage.setItem("nvr.snapRate", String(n)); }catch(e){}
        updateLiveHeader(); renderLive();
      }
    }
    if(c.updated_at && c.updated_at !== serverCfgStamp){
      serverCfgStamp = c.updated_at;
      dirty = true;
    }
    if(JSON.stringify([LK.segSec, LK.mode, LK.intervalH, REC.dir, REC.enabled]) !== before) dirty = true;
    if(dirty){
      saveCfg();
      try{ renderCamTabs(); renderLive(); renderCamPick(); }catch(e){}
      try{ fetchRecordings(); }catch(e){}
    }
    /* 进页面时如果在 syncFromServer 回来之前就点过「保存」，那次 pushCfg 会被
       serverCamsLoaded 守卫吞掉（否则会拿演示骨架覆盖真配置）。这里补推一次，
       用户的修改才不会白改 —— applyServerCams 保留了本地已存在的 camParams，
       所以补推的就是用户刚才改的那份。 */
    if(pushPending){
      pushPending = false;
      pushCfg();
    }
  }).catch(()=>{});
}
let serverCfgStamp = null;
