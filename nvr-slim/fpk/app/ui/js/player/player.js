/* ================= player/player.js =================
   归档播放器：A/B 两个 video 热切换、倍速、导出、片段同步与主循环。
   它们绑在同一件事上 —— **currentTime 与 src 到底归谁管**，
   所以不能按函数名切成「倍速.js」「导出.js」：改一处换源逻辑就得同时看预加载、
   重试与标题更新，分家之后最容易只改一半。
   判断标准：读 state.segs（磁盘上的归档片段）、写播放器里那两个 video 的放这里；
   直播页的快照与实时流读的是快照/RTSP 接口，归 pages/，不进这个文件。
   为什么单独成文件：这是全页唯一会碰 currentTime 的地方，「点了事件却跳错秒」
   只需在这一份里查，不必再在八千行里找 seekVideo 被谁覆盖过。
   ==================================================== */

/* ================= 播放器 ================= */
const player = document.getElementById("player");

/* ---------- 双 video 无缝续播 ----------
   归档片段每个文件往往只有几秒（只包含有活动的那一段）。单 video 播完
   再换 src，会有一次黑屏 + 解码冷启动，一天几百段就是"一直在闪"。
   做法：A/B 两个 video 交替，播 A 时把 B 预加载成下一段；A 一结束就切到已经
   就绪的 B，同时用 A 去预加载再下一段。切换用透明度过渡，旧帧不会先消失。 */
const vidA = document.getElementById("stageVideo");
const vidB = document.getElementById("stageVideoB");
let curVid = vidA, nxtVid = vidB;
let preloadFor = -1;        /* nxtVid 里预加载的是第几段 */
let switching = false;      /* 防止 ended 重入 */
let segRetry = {};          /* 每段加载失败重试计数（key=segIdx），超限跳下一段避免永久黑屏 */

function videoEls(){ return [vidA, vidB]; }

function stopAllVideos(){
  videoEls().forEach(v=>{
    try{ v.pause(); v.removeAttribute("src"); v.load(); }catch(e){}
    v.classList.remove("on");
    v.style.display = "none";
  });
  curVid = vidA; nxtVid = vidB; preloadFor = -1;
}

/* 播放顺序：选了时段筛选就只在筛选范围内播，没选就是全天。
   标题的「第 x/y 段」、上一段/下一段、播完自动接续都走这一份顺序，
   y 从此是筛选后时段里的总段数（如 10:00–12:00 内的段数），不再是全天总数。 */
function playOrder(){
  if(state.hourBlock < 0) return state.segs.map((s,i)=>i);
  return visibleSegs().map(o=>o.i);
}

function setSegTitle(i){
  const seg = state.segs[i];
  const cam = CAMERAS.find(c=>c.id===state.camId);
  const ord = playOrder();
  const pos = ord.indexOf(i);
  const extra = seg.clip && pos >= 0 ? (" · 第 " + (pos+1) + "/" + ord.length + " 段") : "";
  document.getElementById("pTitle").textContent =
    (cam ? cam.name : "") + " · " + seg.reason + extra;
  if(cam) document.getElementById("osdCam").textContent = cam.ch;
}

function openPlayer(i, keepSrc){
  const seg = state.segs[i];
  if(!seg) return;
  const wasOn = player.classList.contains("on");
  state.segIdx = i;
  state.t = seg.s;
  state.playing = true;
  player.classList.add("on");
  /* 压一层历史：从回放列表点进播放器后，左滑/返回键先关播放器，
     而不是把整个应用退出掉。热切换（wasOn=true）不重复压。 */
  if(!wasOn) pushLayer("player");
  setSegTitle(i);
  renderPlayerHeat();          /* 热力格高亮跟随当前片段，切段自动刷新 */
  renderTl();                  /* 0.9.33 时间轴在画面上方，随播放数据刷新 */
  const sceneEl = document.getElementById("scene");

  if(!seg.file){
    /* 没有真实文件 → 画布占位，绝不假装在播 */
    stopAllVideos();
    state.videoSeg = false;
    if(sceneEl) sceneEl.style.display = "";
    syncPlayer();
    requestAnimationFrame(()=>{ drawScene(); });
    return;
  }

  state.videoSeg = true;
  if(sceneEl) sceneEl.style.display = "none";

  /* 如果下一段已经预加载好并且正是它，直接热切换，不重新 load */
  if(keepSrc !== false && preloadFor === i && nxtVid !== curVid
     && nxtVid.getAttribute("src")){
    swapVideos(true);
  }else{
    /* 冷启动：整个 src 一次加载，避免"清空再加载"的两帧黑 */
    curVid.style.display = "block";
    curVid.classList.add("on");
    if(curVid.getAttribute("src") !== seg.file){
      curVid.src = seg.file;
      try{ curVid.load(); }catch(e){}
    }
    try{ curVid.currentTime = 0; }catch(e){}
    safePlay(curVid);
    nxtVid.classList.remove("on");
    /* 注意：这里**不能**把 preloadFor 置 -1。
       置了之后 preloadNext() 会被 loop 每帧重新触发，和预加载相互打断，
       永远赶不到就绪 —— 表现就是"短片段每段都冷启动、卡一下"。
       preloadNext() 自己带着 "目标段 + src 已一致" 的短路判断，不需要外部清。 */
  }
  syncPlayer();
  requestAnimationFrame(()=>{ drawScene(); });
  preloadNext();
}

function safePlay(v){
  try{
    const p = v.play();
    if(p && p.catch) p.catch(()=>{ /* 自动播放被拦时由用户点播放，不算错误 */ });
  }catch(e){ /* 播放能力缺失时静默，不影响选段逻辑 */ }
}

/* 交换 A/B：把已经就绪的 nxtVid 推到前台。
   注意：不要在 200ms 过渡期间把 preloadFor 清掉 —— 下一段可能只有 2、3 秒，
   过渡还没走完它就要接着预加载了。preloadNext() 只在目标段变化时才重新设
   src，所以这里提前调用是安全的。 */
function swapVideos(fromPreload){
  switching = true;
  const old = curVid, nv = nxtVid;
  nv.style.display = "block";
  if(fromPreload){
    try{ nv.currentTime = 0; }catch(e){}
    safePlay(nv);
  }
  old.classList.remove("on");
  /* 先亮新的，再淡出旧的：中间不会出现黑底那一帧 */
  requestAnimationFrame(()=>{ nv.classList.add("on"); });
  curVid = nv;
  nxtVid = old;
  /* 立刻开始为「下一段」预加载；过渡结束只负责收拾旧的 */
  preloadNext();
  setTimeout(()=>{
    old.classList.remove("on");
    try{ old.pause(); }catch(e){}
    switching = false;
  }, 220);
}

/* 预加载下一段：不播、只把数据拉够第一帧。
   归档片段一段往往只有几秒，切完再预加载来不及 —— 所以在切换完成的回调里立刻
   启动，并额外检查"下一段是否已经就该是它"。 */
function preloadNext(){
  const n = state.segIdx + 1;
  const seg = state.segs[n];
  if(!seg || !seg.file){ preloadFor = -1; return; }
  const v = nxtVid;
  if(!v) return;
  if(preloadFor === n && v.getAttribute("src") === seg.file) return;
  preloadFor = n;
  v.style.display = "block";
  v.classList.remove("on");
  try{
    if(v.getAttribute("src") !== seg.file){
      v.preload = "auto";
      v.src = seg.file;
      v.load();
    }
  }catch(e){}
}

/* 当前在播的元素（永远读 curVid，不要在别处缓存） */
function activeVideo(){ return curVid; }

/* 用户在段内跳转：把当前位置喂给前台 video。
   跳完后当前位置离段尾可能不到 1 秒，下一段的预加载必须立刻重排 —— 否则
   「刚拖到末尾就播完」会走冷启动，观感上就是一次卡顿。 */
function seekVideo(sec){
  const v = activeVideo();
  if(!state.videoSeg || !v) return;
  try{ v.currentTime = Math.max(0, sec); }catch(e){}
  preloadNext();
}

function closePlayer(){
  state.playing = false;
  stopAllVideos();
  state.videoSeg = false;
  closeExportMenu();
  document.getElementById("speedPop").classList.remove("on");
  const sceneEl = document.getElementById("scene");
  if(sceneEl) sceneEl.style.display = "";
  player.classList.remove("on");
  if(isFullscreen()) exitFull();
  else if(state.land){ setLand(false); }
}
document.getElementById("pClose").onclick = closePlayer;

document.getElementById("bPlay").onclick = ()=>{
  state.playing = !state.playing;
  const v = activeVideo();
  if(state.videoSeg && v){
    if(state.playing) safePlay(v);
    else { try{ v.pause(); }catch(e){} }
  }
  syncPlayer();
};
/* 倍速：1/2/4/8/16/32×，作用于 <video>.playbackRate；
   画布占位段（无真实文件）由播放循环按 state.speed 推进。
   浏览器 playbackRate 上限 16：>16 的部分由补速定时器每秒跳秒补足
   （32× = 16× 倍速 + 每秒再跳 16 秒），高速档自动静音。 */
const SPEEDS = [1,2,4,8,16,32];
let speedBoostTimer = null, speedAutoMuted = false;
function renderSpeedPop(){
  const pop = document.getElementById("speedPop");
  pop.innerHTML = SPEEDS.map(x =>
    '<button data-speed="' + x + '"' + (x === (state.speed||1) ? ' class="on"' : "") + '>' + x + '×</button>'
  ).join("");
  document.getElementById("spdLabel").textContent = (state.speed||1) + "×";
  pop.querySelectorAll("[data-speed]").forEach(b=>{
    b.onclick = (e)=>{ e.stopPropagation(); setSpeed(+b.dataset.speed); };
  });
}
function setSpeed(x){
  state.speed = x;
  const rate = Math.min(x, 16);   // 浏览器硬上限
  videoEls().forEach(v=>{
    try{ v.playbackRate = rate; }catch(e){}
    if(x > 8 && !v.muted){ v.muted = true; speedAutoMuted = true; }       // 高速静音
    else if(x <= 8 && speedAutoMuted){ v.muted = false; speedAutoMuted = false; }
  });
  if(speedBoostTimer){ clearInterval(speedBoostTimer); speedBoostTimer = null; }
  if(x > 16){
    speedBoostTimer = setInterval(()=>{
      const v = activeVideo();
      if(!v || v.paused || switching) return;
      try{ if(v.duration) v.currentTime = Math.min(v.duration, v.currentTime + (x - 16)); }catch(e){}
    }, 1000);
  }
  renderSpeedPop();
  toast("toast", "倍速 " + x + "×");
}
document.getElementById("bSpeed").onclick = (e)=>{
  e.stopPropagation();
  renderSpeedPop();
  document.getElementById("speedPop").classList.toggle("on");
};
/* 点别处收起倍速菜单（捕获阶段，抢在 stage 的暂停/播放判定之前） */
document.addEventListener("pointerdown", e=>{
  const pop = document.getElementById("speedPop");
  if(!pop.classList.contains("on")) return;
  if(pop.contains(e.target) || document.getElementById("bSpeed").contains(e.target)) return;
  pop.classList.remove("on");
}, true);
document.getElementById("bPrev").onclick = ()=> jumpSeg(-1);
document.getElementById("bNext").onclick = ()=> jumpSeg(1);

/* ⋯ 导出菜单：导出当前播放的源文件（服务端 /api/record/file 加 dl=1 → 附件下载） */
function exportUrl(){
  const seg = state.segs[state.segIdx];
  return seg && seg.file ? (seg.file.replace(/&remux=1/, "") + "&dl=1") : null;
}
function exportName(){
  const seg = state.segs[state.segIdx];
  if(!seg) return "—";
  const cam = CAMERAS.find(c=>c.id===state.camId);
  const st = seg.clip && seg.src != null ? seg.src : seg.s;
  return (cam ? cam.name : state.camId) + "_" + curDay() + "_"
    + pad(Math.floor(st/3600)) + pad(Math.floor(st%3600/60)) + pad(Math.floor(st%60)) + ".mp4";
}
function closeExportMenu(){
  document.getElementById("pMenu").classList.remove("on");
  document.getElementById("pMask").classList.remove("on");
}
document.getElementById("pMore").onclick = (e)=>{
  e.stopPropagation();
  document.getElementById("pmFn").textContent = exportName();
  document.getElementById("pMenu").classList.add("on");
  document.getElementById("pMask").classList.add("on");
};
document.getElementById("pMask").onclick = closeExportMenu;
document.getElementById("pmExport").onclick = ()=>{
  const u = exportUrl();
  closeExportMenu();
  if(!u){ toast("toast", "当前段没有可导出的文件"); return; }
  const a = document.createElement("a");
  a.href = u; a.download = exportName();
  document.body.appendChild(a); a.click(); a.remove();
  toast("toast", "已开始导出 · 保存在系统下载目录");
};
document.getElementById("pmCopy").onclick = ()=>{
  const u = exportUrl();
  closeExportMenu();
  if(!u){ toast("toast", "当前段没有可导出的文件"); return; }
  const abs = new URL(u, location.href).href;
  const ok = ()=> toast("toast", "下载链接已复制");
  try{
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(abs).then(ok).catch(()=> toast("toast", abs));
    }else{ toast("toast", abs); }
  }catch(e){ toast("toast", abs); }
};

/* 绑定 A/B 两个 video 的事件（逻辑完全一致，只是作用对象不同） */
videoEls().forEach(v=>{
  v.addEventListener("timeupdate", ()=>{
    if(v !== curVid || !state.videoSeg || state.segIdx < 0) return;
    const seg = state.segs[state.segIdx];
    if(!seg) return;
    state.t = seg.s + (v.currentTime || 0);
    syncPlayer();
  });
  v.addEventListener("ended", ()=>{
    if(v !== curVid || switching) return;
    /* 一段播完：播放顺序里还有下一段就无缝接上，没有再停。
       预加载若已把当前元素换掉（curVid 变了），这里自然就不再命中。 */
    const ord = playOrder();
    const pos = ord.indexOf(state.segIdx);
    if(state.segIdx >= 0 && pos >= 0 && pos < ord.length-1){
      finishSegAndNext();
    }else{
      state.playing = false;
      state.t = state.segs[state.segIdx] ? state.segs[state.segIdx].e - 0.05 : 0;
      syncPlayer();
    }
  });
  v.addEventListener("play", ()=>{ if(v === curVid && state.videoSeg && !state.playing){ state.playing = true; syncPlayer(); } });
  v.addEventListener("pause", ()=>{
    if(v === curVid && state.videoSeg && state.playing && !v.ended){ state.playing = false; syncPlayer(); }
  });
  /* 新载入的段套用当前倍速（换 src 后浏览器会重置回 1×）；
     高速档（>8×）补静音（setSpeed 只对在场的 video 生效，换元素后要补） */
  v.addEventListener("loadedmetadata", ()=>{
    try{ v.playbackRate = Math.min(state.speed || 1, 16); }catch(e){}
    if((state.speed || 1) > 8) v.muted = true;
    /* 按摄像机真实画幅定舞台比例（同一通道来源一致，后到的写一次也无害）；
       vidAsp 同时给黑边工具轨算宽度用 */
    const st = document.getElementById("playStage");
    if(st && v.videoWidth && v.videoHeight){
      vidAsp = v.videoWidth / v.videoHeight;
      st.style.setProperty("--vasp", v.videoWidth + "/" + v.videoHeight);
      syncRail();
    }
  });
  /* 外网/NAT 下视频流偶发断流：error=当前段加载失败、stalled=缓冲卡住。
     自愈：前台播放中的 video 触发时重拉当前段，超限则跳下一段；预加载的
     video 触发时只清预加载标记让其重新拉，不污染前台切换。 */
  v.addEventListener("error", ()=>{
    if(v !== curVid){                 /* 预加载失败：清标记，下轮重新预加载 */
      if(preloadFor >= 0) preloadFor = -1;
      return;
    }
    if(!state.videoSeg) return;
    const seg = state.segs[state.segIdx];
    if(!seg || !seg.file) return;
    const k = state.segIdx;
    segRetry[k] = (segRetry[k] || 0) + 1;
    if(segRetry[k] <= 3){
      try{
        v.preload = "auto";
        v.src = seg.file;
        v.load();
        safePlay(v);
      }catch(e){}
    }else{                           /* 超限放弃本段，接下一段，不让画面卡死 */
      const ord = playOrder();
      const pos = ord.indexOf(k);
      if(pos >= 0 && pos < ord.length - 1) finishSegAndNext();
    }
  });
  let _stalledAt = 0;
  v.addEventListener("stalled", ()=>{
    if(v !== curVid || !state.videoSeg) return;
    const now = Date.now();
    if(now - _stalledAt < 2000) return;     /* 2s 内只试一次，避免频繁重连 */
    _stalledAt = now;
    if(v.readyState >= 3) return;           /* 缓冲其实够，别瞎重连打断播放 */
    const seg = state.segs[state.segIdx];
    if(!seg || !seg.file) return;
    try{
      v.preload = "auto";
      v.src = seg.file;
      v.load();
      safePlay(v);
    }catch(e){}
  });
});

/* 播完一段 → 接下一段。若下一段已预加载好就走热切换（观感连续），
   否则退回冷启动。两种情况都保持播放状态，用户不用再点播放。 */
function finishSegAndNext(){
  /* 自动接续也走播放顺序（筛选时段内不跳出范围） */
  const ord = playOrder();
  const pos = ord.indexOf(state.segIdx);
  const n = pos >= 0 ? ord[pos + 1] : undefined;
  const seg = n === undefined ? null : state.segs[n];
  if(!seg) return;
  state.segIdx = n;
  state.t = seg.s;
  state.playing = true;
  setSegTitle(n);
  if(preloadFor === n && nxtVid.getAttribute("src")){
    swapVideos(true);     /* 已就绪：热切换，旧帧淡出、新帧接上 */
  }else{
    /* 预加载没赶上（比如磁盘慢）：直接给前台换 src，尽快续上 */
    curVid.src = seg.file;
    try{ curVid.currentTime = 0; }catch(e){}
    safePlay(curVid);
    preloadFor = -1;
    preloadNext();
  }
  syncPlayer();
}
/* 回放页横屏按钮：走统一的 toggleFull（真全屏 + 锁横屏 + 右上角退出按钮）。
   以前这里只是给 .phone 加个类，手机上那只是把内容挤进一个小框，不是全屏。 */
document.getElementById("bLand").onclick = ()=> toggleFull();
document.getElementById("pMore").onclick = ()=> toast("toast", "下载原画质 / 导出该片段");

function jumpSeg(dir){
  const ord = playOrder();
  const pos = ord.indexOf(state.segIdx);
  const n = pos < 0 ? undefined : ord[pos + dir];
  if(n === undefined){ toast("toast", dir>0 ? "已经是最后一段" : "已经是第一段"); return; }
  openPlayer(n);
}

function durStr(sec){
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec/3600), m = Math.floor(sec%3600/60), s = sec%60;
  return h ? (h + ":" + pad(m) + ":" + pad(s)) : (m + ":" + pad(s));
}
/* 本文件播放进度：mm:ss（全时分片 5 分钟、归档片段可能只有几秒，分钟两位够用） */
function mmss(sec){
  sec = Math.max(0, Math.floor(sec || 0));
  return pad(Math.floor(sec/60)) + ":" + pad(sec%60);
}

function syncPlayer(){
  const seg = state.segs[state.segIdx];
  if(!seg) return;
  /* 归档浓缩：时间与标题都按「源时刻」显示（seg.src 来自 .idx.json 的源时间窗），
     播放进度条仍按真实播放时长推进 —— 用户对的是"事情发生的钟点"，
     播的是裁剪成品，两套坐标并存且都标注清楚。
     双层时间轴：上行钟点（起始/当前/结束），下行本文件 00:00 → 总长。 */
  const clipMode = !!seg.clip;
  const p = Math.max(0, Math.min(1, (state.t - seg.s) / Math.max(0.2, seg.e - seg.s)));
  const srcNow = clipMode && seg.src != null
    ? seg.src + p * Math.max(1, seg.srcEnd - seg.src) : state.t;
  document.getElementById("pSub").textContent = clipMode
    ? ("覆盖 " + hms(seg.src) + " – " + hms(seg.srcEnd) + " · 播放 " + durStr(seg.e-seg.s))
    : (hms(seg.s) + " – " + hms(seg.e));
  document.getElementById("tStart").textContent = clipMode
    ? (seg.src != null ? hms(seg.src) : "—") : hms(seg.s);
  document.getElementById("tNow").textContent = clipMode
    ? hms(srcNow) : hms(state.t);
  document.getElementById("tEnd").textContent = clipMode
    ? (seg.src != null ? hms(seg.srcEnd) : durStr(state.segs.reduce((a,x)=>a+(x.e-x.s),0)))
    : hms(seg.e);
  document.getElementById("fPos").textContent = mmss(state.t - seg.s);
  document.getElementById("fDur").textContent = mmss(seg.e - seg.s);
  document.getElementById("pFill").style.width = (p*100).toFixed(1) + "%";
  document.getElementById("pKnob").style.left = (p*100).toFixed(1) + "%";
  document.getElementById("playIcon").innerHTML = state.playing
    ? '<path d="M7 5h3.4v14H7zM13.6 5H17v14h-3.4z"/>'
    : '<path d="M8 5v14l11-7z"/>';
  document.getElementById("ring").classList.toggle("show", !state.playing);
  /* 全屏横屏时播放中收起全部控制；暂停/轻点画面后浮现 */
  player.classList.toggle("fshide", !!(state.land && state.playing));
  document.getElementById("osdTime").textContent = clipMode
    ? (curDay() + " " + hms(srcNow) + " · 归档")
    : (curDay() + " " + hms(state.t));
  syncTlCursor();          /* 热力图定位竖线跟随播放（连播切段由 renderTl 重建接管） */
}

/* 进度条拖动 */
(function bindBar(){
  const bar = document.getElementById("pBar");
  let on = false;
  function set(e){
    const seg = state.segs[state.segIdx];
    if(!seg) return;
    const r = bar.getBoundingClientRect();
    const p = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    state.t = seg.s + p*(seg.e-seg.s);
    seekVideo(Math.max(0, state.t - seg.s));
    syncPlayer();
  }
  bar.addEventListener("pointerdown", e=>{ on=true; bar.classList.add("drag"); state.playing=false; bar.setPointerCapture(e.pointerId); set(e); });
  bar.addEventListener("pointermove", e=>{ if(on) set(e); });
  bar.addEventListener("pointercancel", ()=>{ on=false; bar.classList.remove("drag"); });
  bar.addEventListener("pointerup", ()=>{ on=false; bar.classList.remove("drag"); });
})();

/* 左右滑动切换片段 */
(function bindSwipe(){
  const stage = document.getElementById("stage");
  let x0=null, y0=null;
  stage.addEventListener("pointerdown", e=>{ x0=e.clientX; y0=e.clientY; });
  stage.addEventListener("pointerup", e=>{
    if(x0===null) return;
    const dx = e.clientX - x0, dy = e.clientY - y0;
    if(Math.abs(dx) > 56 && Math.abs(dx) > Math.abs(dy)*1.6){
      jumpSeg(dx < 0 ? 1 : -1);
    }else if(Math.abs(dx) < 8 && Math.abs(dy) < 8){
      state.playing = !state.playing; syncPlayer();
    }
    x0 = null;
  });
})();

/* 播放循环 */
let last = 0, liveAcc = 0;
function loop(ts){
  if(document.hidden){ requestAnimationFrame(loop); return; }   // 后台标签不画，省 CPU/电
  if(!last) last = ts;
  const dt = Math.min(0.1, (ts-last)/1000);
  last = ts;
  if(player.classList.contains("on")){
    const seg = state.segs[state.segIdx];
    if(seg && state.playing){
      if(state.videoSeg){
        /* 真实录像：进度由 <video> 推进。切段由 ended 事件 + 预加载接管，
           这里只兜底同步进度，绝不要在这里再调 openPlayer —— 那会和预加载
           的交叉淡入打架，出现"切两次"。 */
        const v = activeVideo();
        if(v && !v.paused){ state.t = seg.s + (v.currentTime||0); }
        /* 快播完了就提前把下一段拉起来：归档片段往往只有几秒，等到 ended 才开始
           预加载必然赶不上，"接了但黑一帧"就是这么来的。 */
        if(v && seg.e - state.t < 2) preloadNext();
      }else{
        state.t += dt * 8 * (state.speed || 1);   // 画布占位段：没有解码器，按倍速快进模拟
        if(state.t >= seg.e){
          const ord = playOrder();
          const pos = ord.indexOf(state.segIdx);
          if(pos >= 0 && pos < ord.length-1){ openPlayer(ord[pos+1]); }
          else { state.t = seg.e-0.2; state.playing = false; }
        }
      }
      syncPlayer();
    }
    if(!state.videoSeg){ noiseSeed++; drawScene(); }   /* 真视频在播时画布已隐藏，不再画假场景 */
  }
  /* 直播：快照墙按倒计时刷新；实时层按状态重绘 */
  liveAcc += dt;
  const roomEl = document.getElementById("liveRoom");
  const roomOpen = roomEl.classList.contains("on");
  if(roomOpen){
    const step = state.liveView === "grid" ? 0.22 : 0.09;   // 宫格降帧，省解码
    if(liveAcc > step){
      liveAcc = 0;
      if(!state.livePaused){
        document.querySelectorAll("#lrBody .lr-tile").forEach(t=>drawLiveScene(t._cv, t._idx));
        syncLiveStamps();   /* 流活着跟钟走；断流/暂停冻结，不再无脑写当前时刻 */
      }
      document.getElementById("lrClock").textContent = clockStr();
    }
  }else if(document.getElementById("page-live").classList.contains("on")
           && !player.classList.contains("on")){
    tickSnaps(dt);
  }
  requestAnimationFrame(loop);
}
