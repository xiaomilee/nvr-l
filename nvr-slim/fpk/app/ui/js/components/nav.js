/* ================= components/nav.js =================
   tab 切换与安卓返回栈：自己压的层自己弹，只有回到根才把返回交给系统。
   判断标准：管的是"当前在哪一页、怎么回去"，不管那一页里画什么 ——
   各页的渲染函数（renderDates/renderTl/...）属于 pages/，留到下一轮。
   为什么单独成文件：history.length 曾被当成层数、左滑直接退出应用，
   这类"看起来对"的推断必须和它影响的 UI 摆在一起才看得见。
   ====================================================== */

/* ================= tab / 历史返回 =================
   安卓左滑返回手势的本质是 `history.back()`。以前只有"非直播页 → 回直播页"
   一档，于是：
     · 实时播放层里左滑  → 整个应用直接退到桌面（用户以为闪退）
     · 设置页展开了摄像头 → 直接跳回直播页，丢掉了"上一层"
   现在做成一个真正的返回栈，每一层都压一条历史：
     播放器 → 实时层 → 直播快照墙(根)   /   设置展开 → 设置页 → 直播(根)
   只有**已经在根（直播快照墙）**时，返回才交给系统退出应用。 */
function activePage(){
  const on = document.querySelector(".page.on");
  return on ? on.id.replace("page-", "") : "live";
}

/* 当前"在根历史"吗？只有根才允许系统接管返回键。 */
function atRoot(){
  return activePage() === "live"
      && state.liveView === "snap"
      && !document.getElementById("liveRoom").classList.contains("on")
      && !document.getElementById("player").classList.contains("on")
      && !(SET.editing || SET.open);
}

/* 自己数压了几层。
   绝不能拿 history.length 当层数 —— 它是整个标签页会话的累计长度（用户之前
   逛过别的页面也算在内），用它算 history.go(-(n-1)) 会一次性跳过太多，
   表现就是"点了一下回直播，结果直接退出应用"。 */
let LAYERS = 0;

/* 压一层历史。tag 只用于调试。 */
function pushLayer(tag){
  try{ history.pushState({_nvr:1, layer:LAYERS+1, tag:tag || "layer"}, ""); LAYERS++; }catch(e){}
}

/* 一次弹掉所有自己压的层，回到根。 */
function popToRoot(){
  if(LAYERS > 0){
    const n = LAYERS;
    LAYERS = 0;
    try{ history.go(-n); }catch(e){}
  }
}

function activatePage(page){
  if(page !== "live" && state.liveView !== "snap"){
    closeLiveRoom();   // 离开直播页时若还在单路实时层，主动断 MJPEG 流（摘 src + 清空容器），
                       // 否则隐藏的 liveRoom 会继续拉流，手机上白白耗流量与 ffmpeg 转发
  }
  document.querySelectorAll(".tb").forEach(x=>x.classList.toggle("on", x.dataset.page===page));
  document.querySelectorAll(".page").forEach(p=>p.classList.toggle("on", p.id==="page-"+page));
  if(page === "cam"){
    /* 整页重渲染：画布的尺寸要等页面可见后才知道，顺带把事件重新绑上 */
    syncLink();        /* 处理队列的真实状态每次进设置页都刷新 */
    setTimeout(()=>{ renderCamTabs(); }, 30);
  }
  if(page === "live"){
    updateSnapLabels();
    setTimeout(renderLive, 20);   // 回到快照墙时重画，避免尺寸为 0
  }
  if(page === "events"){
    /* 事件页：每次进页重画筛选条 + 读一次列表（30 秒内吃缓存）。
       放在这里而不是 goPage 里，深链 ?page=events 与底部 tab 才会走同一条路。 */
    renderEvChips();
    loadEvents();
  }
  if(page === "list"){
    camPickShow(false);
    playCamShow(false);
    fetchRecordings();   /* 每次进文件页都按磁盘现状刷新 */
    renderCamPick(); renderDates(); renderOverview(); renderHeat(); renderClips();
  }
  if(page !== "play") pvStop();   // 离开回放页就停播，不让画面区在后台空转
  if(page === "play"){
    camPickShow(false);
    /* 进回放页 = 直接定位到默认落点开播（0.9.72）。以前这里不定位，
       必须手点播放钮才出画面；而首次点击常撞上「清单还没拉回来」或
       「落点贴在段尾只剩 1 秒」，表现出来就是要点几下才能开始播。
       清单没拉过时 pvAutoEnter 自己会拉一次再定位。 */
    renderPlay(true);
    /* 页签刚切过来时布局可能还没落定（clientHeight 量不到）：下一帧补测一次，
       上下留白才能按真实视口高度算出来（0.9.68） */
    setTimeout(syncVnowline, 30);
  }
}

/* 用户主动点 tab：改页 + 压历史。回直播页时把叠在上面的层全部弹掉，
   保证"直播快照墙"永远是唯一根。 */
function goPage(page){
  const cur = activePage();
  if(page === cur) return;
  activatePage(page);
  if(page === "live"){
    /* 回直播 = 回根：把压过的层弹干净（不触发二次界面切换，见 popstate 里的守卫） */
    popToRoot();
    return;
  }
  pushLayer(page);
}
document.querySelectorAll(".tb").forEach(b=>{
  b.onclick = ()=>{ goPage(b.dataset.page); };
});
/* 回放页 ↔ 文件页 互跳；回放页机位下拉；时间轴缩放 */
(function(){
  const blp = document.getElementById("btnListPlay");
  if(blp) blp.onclick = ()=>{ goPage("play"); };
  const pcs = document.getElementById("playCamSel");
  if(pcs) pcs.onclick = e=>{ e.stopPropagation(); playCamShow(); };
  const pds = document.getElementById("playDaySel");
  if(pds) pds.onclick = e=>{ e.stopPropagation(); playDayShow(); };
  document.addEventListener("click", ()=>{ playCamShow(false); playDayShow(false); vtlBubbleHide();
    const vs = document.getElementById("vspop"); if(vs) vs.classList.remove("on"); });
  /* 时间轴缩放：桌面 = 按钮 / Ctrl+滚轮；触屏 = 双指捏合。
     滚轮不加修饰键时保持原生滚动，不劫持。 */
  const bin = document.getElementById("btnVtlIn");
  const bout = document.getElementById("btnVtlOut");
  if(bin) bin.onclick = ()=>{ if(!vtlZoom(1.3)) toast("toast2", "已到最大缩放"); };
  if(bout) bout.onclick = ()=>{ if(!vtlZoom(1/1.3)) toast("toast2", "已到最小缩放"); };
  const sc = document.getElementById("vtlScroll");
  if(!sc) return;
  /* 启动时恢复上次筛选态（芯片高亮 + 时间轴活动模式 + 倍速标签）。
     本 IIFE 在 PPLAY 声明之前执行，不能直接调 pvSyncCtl —— 延到脚本跑完后。 */
  const card0 = document.getElementById("vtlCard");
  setTimeout(()=>{
    if(card0) card0.classList.toggle("actmode", PPLAY.filter === "act");
    pvSyncCtl();
    syncVnowline();
  }, 0);
  /* 点轨道任意位置 = 跳到该时刻并收起标注气囊（横线有自己的 click，已 stopPropagation） */
  const vtlBox = document.getElementById("vtl");
  if(vtlBox) vtlBox.addEventListener("click", e=>{
    vtlBubbleHide();
    const r = vtlBox.getBoundingClientRect();
    const t = (e.clientY - r.top - vtlTop()) / vtlPxh * 3600;
    if(!(t >= 0) || t > 86400) return;
    if(!pvSegAt(t)){ toast("toast2", "该时段无录像"); return; }
    pvPlayAt(t);
  });
  /* 画面区：轻点 = 暂停/继续；大播放钮在 .pbig 上（点按 = pvResume） */
  const st = document.getElementById("playStage");
  if(st){
    st.addEventListener("click", e=>{
      if(!PPLAY.on){ pvResume(); return; }
      const v = pvCur;
      if(v.paused){ safePlay(v); } else { try{ v.pause(); }catch(err){} }
    });
    const big = document.getElementById("playBig");
    if(big) big.addEventListener("click", e=>{ e.stopPropagation(); pvResume(); });
    /* 暂停浮层：点它继续播。stopPropagation 防止再触发 stage 的"轻点=切换" */
    const pf = document.getElementById("playPauseFloat");
    if(pf) pf.addEventListener("click", e=>{ e.stopPropagation(); pvResume(); });
  }
  /* 全屏：整个回放页进全屏（画面铺满，时间轴/操作条悬浮），横屏锁 + 旋转兑底复用既有基建 */
  const bf = document.getElementById("btnPlayFull");
  if(bf) bf.onclick = e=>{ e.stopPropagation(); toggleFull(document.getElementById("page-play")); };
  const tog = document.getElementById("btnPlayTog");
  if(tog) tog.onclick = e=>{
    e.stopPropagation();
    if(!PPLAY.on){ pvResume(); return; }
    const v = pvCur;
    if(v.paused){ safePlay(v); } else { try{ v.pause(); }catch(err){} }
  };
  /* 筛选芯片：全时=连播全部；活动=时间轴只亮活动段 + 按预设跳过静止部分。
     记住选择；切换时退出快进态并重绘时间轴（保持滚动位置） */
  document.querySelectorAll("[data-pvfilter]").forEach(b=>{
    b.onclick = e=>{
      e.stopPropagation();
      PPLAY.filter = b.dataset.pvfilter === "act" ? "act" : "full";
      try{ localStorage.setItem("nvr_pvfilter", PPLAY.filter); }catch(err){}
      PPLAY.ff = null; pvRate(); pvSyncCtl();
      const card = document.getElementById("vtlCard");
      if(card) card.classList.toggle("actmode", PPLAY.filter === "act");
      renderVtl(vtlData, vtlDay);
    };
  });
  /* 活动类别筛选（0.9.60）：勾/取消某类，活动导航与时间轴淡出同步变。
     至少留一类——全关时活动模式没有可跳的目标，等于把回放卡死在原地。
     改完立刻重绘时间轴，正在播的如果已经掉出筛选结果，交给 pvSkipStatic 处理 */
  document.querySelectorAll("[data-pvcls]").forEach(b=>{
    b.onclick = e=>{
      e.stopPropagation();
      /* 图例即筛选（0.9.88）：只有「活动」模式才可点。全时模式它就是配色对照表。 */
      if(PPLAY.filter !== "act"){ toast("toast2", "切到「活动」筛选后，点图例可按类别过滤"); return; }
      const k = b.dataset.pvcls;
      if(!(k in PPLAY.cls)) return;
      const next = Object.assign({}, PPLAY.cls);
      next[k] = !next[k];
      if(!next.motion && !next.person && !next.vehicle){
        toast("toast2", "至少保留一个类别 · 全关等于没有活动可看");
        return;
      }
      PPLAY.cls = next;
      try{ localStorage.setItem("nvr_pvcls", JSON.stringify(PPLAY.cls)); }catch(err){}
      pvSyncCtl();
      renderVtl(vtlData, vtlDay);
      if(PPLAY.on) pvOnTime();
    };
  });
  /* 倍速：从操作条向上弹的小网格；1× 是全时连播默认，快进态下不生效 */
  const spd = document.getElementById("btnPvSpeed");
  const spop = document.getElementById("vspop");
  if(spd && spop){
    spd.onclick = e=>{
      e.stopPropagation();
      if(!spop.classList.toggle("on")) return;
      spop.innerHTML = [1,2,4,8,16].map(n=>
        '<button data-sp="' + n + '"' + (n === PPLAY.speed ? ' class="on"' : "") + ">"
        + n + "×</button>").join("");
      spop.querySelectorAll("[data-sp]").forEach(x=> x.onclick = ev=>{
        ev.stopPropagation();
        PPLAY.speed = +x.dataset.sp;
        try{ localStorage.setItem("nvr_pvspeed", String(PPLAY.speed)); }catch(err){}
        PPLAY.ff = null; pvRate(); pvSyncCtl(); spop.classList.remove("on");
      });
    };
    spop.onclick = e=> e.stopPropagation();
  }
  /* 固定时刻线的跟随让位：用户拖着/挑着时间轴时不抢滚动，松手 2 秒后恢复跟随 */
  const hold = ()=>{ vtlHold = Date.now(); };
  sc.addEventListener("touchstart", hold, {passive:true});
  sc.addEventListener("touchmove", hold, {passive:true});
  sc.addEventListener("touchend", hold, {passive:true});
  sc.addEventListener("wheel", hold, {passive:true});
  /* 空闲态：滚到哪里，时刻线就显示哪里的时刻。
     播放中用户拖动时间轴 = 松手后把播放定位到固定线压着的时刻继续播
     （而不是被跟随逻辑拽回原进度，表现为"时间轴跳动"，v0.9.48）。
     只接管用户的拖动：vtlHold 900ms 内才算用户操作，程序滚动的对齐不抢。 */
  let vtlSeekTmr = 0;
  sc.addEventListener("scroll", ()=>{
    if(!PPLAY.on){ syncVnowline(); return; }
    clearTimeout(vtlSeekTmr);
    vtlSeekTmr = setTimeout(()=>{
      if(!PPLAY.on || Date.now() - vtlHold > 900) return;
      const t = Math.max(0, Math.min(86400,
        (sc.scrollTop + sc.clientHeight / 3 - vtlTop()) / vtlPxh * 3600));
      if(Math.abs(t - PPLAY.t) < 1.2) return;   // 停在原进度附近就不折腾
      if(pvSegAt(t)) pvPlayAt(t, { quiet:true });
    }, 400);
  });
  window.addEventListener("resize", syncVnowline);
  sc.addEventListener("wheel", e=>{
    if(!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    vtlZoom(e.deltaY < 0 ? 1.15 : 1/1.15);
  }, {passive:false});
  const tdist = ts => Math.hypot(ts[0].clientX - ts[1].clientX,
                                 ts[0].clientY - ts[1].clientY);
  let pinch = null;
  sc.addEventListener("touchstart", e=>{
    if(e.touches.length !== 2) return;
    const r = sc.getBoundingClientRect();
    const my = (e.touches[0].clientY + e.touches[1].clientY) / 2 - r.top;
    pinch = { d: tdist(e.touches) || 1, pxh0: vtlPxh,
              t: Math.max(0, Math.min(86400,
                (sc.scrollTop + my - vtlTop()) / vtlPxh * 3600)), y: my };
  }, {passive:true});
  sc.addEventListener("touchmove", e=>{
    if(!pinch || e.touches.length !== 2) return;
    e.preventDefault();          // 双指期间接管，避免浏览器同时滚动/缩放页面
    vtlPxh = vtlClampPxh(pinch.pxh0 * (tdist(e.touches) / pinch.d));
    renderVtl(vtlData, vtlDay, pinch.t, pinch.y);
  }, {passive:false});
  const pinchEnd = ()=>{
    if(!pinch) return;
    pinch = null;
    try{ localStorage.setItem("nvr_vtl_pxh", String(Math.round(vtlPxh))); }catch(e){}
  };
  sc.addEventListener("touchend", pinchEnd);
  sc.addEventListener("touchcancel", pinchEnd);
})();

/* 旋转态下的时间轴拖动（0.9.55）：
   画面是被 CSS rotate(90deg) 转过来的（系统不让锁横屏时只能这么干），于是时间轴的
   "竖向"在屏幕坐标里落在横向。原生滚动按**屏幕方向**判 touch-action，pan-y 直接
   把横向拖动拦掉 —— 表现就是"横屏暂停时弹出的时间轴怎么拖都不动"。
   这里自己换算：容器 y 轴指向屏幕 -x，所以手指往屏幕 +x 走 = 内容往上滚。 */
(function(){
  const sc = document.getElementById("vtlScroll");
  if(!sc) return;
  let drag = null;
  sc.addEventListener("touchstart", e=>{
    if(e.touches.length !== 1 || !document.body.classList.contains("rot")){ drag = null; return; }
    drag = { x: e.touches[0].clientX, top: sc.scrollTop };
  }, {passive:true});
  sc.addEventListener("touchmove", e=>{
    if(!drag || e.touches.length !== 1) return;
    e.preventDefault();                       /* 别让浏览器再按错误方向滚一遍 */
    sc.scrollTop = drag.top + (e.touches[0].clientX - drag.x);
  }, {passive:false});
  const stop = ()=>{ drag = null; };
  sc.addEventListener("touchend", stop);
  sc.addEventListener("touchcancel", stop);
})();

/* 返回键/左滑：按"最内层优先"逐层退出。
   顺序必须是 播放器 → 实时层 → 设置展开 → 页面 → 根。
   每次 popstate 都是"用户从自己压的那一层退出去"，所以先扣减层深；
   如果退出这一层后界面已经回到根，就不该再补压回去。 */
window.addEventListener("popstate", (e)=>{
  /* 弹掉一层：计入自己的账本（浏览器只告诉我们会话在退，不告诉退了几层） */
  if(LAYERS > 0) LAYERS--;
  const st = e && e.state;
  if(st && st.layer != null && st.layer >= 0 && st.layer < LAYERS) LAYERS = st.layer;

  /* 1) 播放器 */
  const pl = document.getElementById("player");
  if(pl && pl.classList.contains("on")){
    if(isFullscreen() || state.land) exitFull();
    closePlayer();
    if(!atRoot()) pushLayer("player");
    return;
  }
  /* 2) 实时播放层 */
  const room = document.getElementById("liveRoom");
  if(room && room.classList.contains("on")){
    leaveLive();
    if(!atRoot()) pushLayer("live");
    return;
  }
  /* 3) 设置页里展开的摄像头 / 未保存的编辑 */
  if(SET.editing && SET.draft){
    toast("toast2", "还有未保存的改动 · 已保留草稿");
    SET.editing = false; SET.draft = null;
    renderCamTabs();
    if(!atRoot()) pushLayer("cam");
    return;
  }
  if(SET.open){
    SET.open = null;
    renderCamTabs();
    if(!atRoot()) pushLayer("cam");
    return;
  }
  /* 4) 非直播页 → 回直播页 */
  if(activePage() !== "live"){
    activatePage("live");
    /* 回到直播页时把剩余层弹掉；根上的返回仍然交给系统 */
    popToRoot();
    return;
  }
  /* 5) 已经在根：什么都不做，让系统处理（退出应用） */
});
