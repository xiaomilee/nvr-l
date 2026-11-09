/* ================= pages/live.js =================
   直播页的渲染与实时播放层：把配置推给后端（决定显示哪几路、多久刷一次）、
   快照墙与宫格、时间戳与倒计时、进出直播间、实时流挂载、兜底画面绘制。
   它们共用 CAMERAS 与 LIVE_COLS 两份现场状态，改一路数量就得连着看渲染、
   列数弹层与流地址，所以摆在一起而不按控件切碎。
   判断标准：出现在直播页、要读全局状态或碰 DOM 的逻辑放这里；
   取图超时与 token 去重在 pages/live_snap.js，请求统一走 core/api.js，
   时间格式化走 core/format.js。
   为什么放在请求层之后：这段要 POST 配置、要按服务端通道表重排摄像头，
   依赖 API.get/post 已经就位 —— 位置即执行顺序，别为了「同页同目录」挪动它。
   ================================================== */

/* 把摄像头与通用设置推给后端：快照 worker、录制、衔接吃的都是这份配置。
   内容没变就不发（签名去重），变了静默 POST，失败也不打扰用户。 */
let lastPushSig = "";
let serverCamsLoaded = false;     /* 当前 CAMERAS 是否已经来自服务端（而不是演示骨架） */
let pushPending = false;          /* 在拿到服务端通道表之前被拦下的推送请求 */
function pushCfg(){
  if(typeof fetch !== "function") return;
  /* 还没从服务端拿到过通道列表 → 此刻页面上的 CAMERAS 是演示骨架。
     这时候推上去就是把 NAS 上的真实配置清空。等 syncFromServer 回来再说。 */
  if(!serverCamsLoaded){ pushPending = true; return; }
  pushPending = false;
  const cams = CAMERAS.map(c=>{
    const p = camParams[c.id] || {};
    return { id:c.id, name:c.name, url:p.main || "", sub_url:p.sub || "",
             /* 空 url 只表示"本机没存明文"；只有这两个显式标记才会让服务端真清地址。
                服务端对空/脱敏 url 一律回落 NAS 旧值，换设备推送不会再抹掉配置。 */
             url_clear: !!p.urlClear, sub_clear: !!p.subClear,
             enabled:p.enabled !== false,
             full_days:p.fullDays, clip_days:p.clipDays,
             /* 每路事件侦测总开关与倍速：必须真的推上去，
                link 调度靠 condense 决定这一路入不入队 */
             condense: p.condense !== false,
             clip_speed: p.clipSpeed || 1,
             /* 每路切分方式：follow=跟随通用；join/split 显式覆盖。
                link 消费端按它选导出路径（拼接 vs 活动切片） */
             clip_mode: p.clipMode || "follow",
             /* 每路压缩方案：follow=跟随全局；virtual/materialize/off 显式覆盖。
                服务端 link 按 link.condense_mode_of() 消费它决定只写台账还是双写导出 */
             condense_mode: p.condenseMode || "follow",
             /* 每路 AI 检测（0.9.60）：follow/on/off；显式配了类别才推
                detect_classes（三键完整状态，服务端缺键=False） */
             detect: p.detect || "follow",
             detect_classes: p.detectCls || null,
             /* 单路分片长度：0 表示跟随通用设置。必须真的推上去 ——
                它是 ffmpeg 命令行的物理参数，只存在 localStorage 等于没设。 */
             segment_sec: p.segSec || 0,
             /* 判读条件：同样必须推。判定引擎在服务端，留在前端只是摆设 ——
                以前"灵敏度滑杆"就是这么变成假设置的：拖了三年，后端从没收到过。 */
             judge: judgeWire(p),
             masks: normMasks(p.masks) };
  });
  const body = { cameras: cams,
                 record_enable: !!REC.enabled, record_dir: REC.dir,
                 cleanup_at: REC.cleanupAt, snapshot_interval: SNAP.interval || 10,
                 /* 这三个以前只存在 localStorage 里，服务端根本没收到过 ——
                    recorder 的分片长度、link 的节奏都是读服务端配置的，
                    所以「改分片长度没反应」其实是推都没推。 */
                 record_segment_sec: LK.segSec,
                 /* 录像封装：分片 MP4 让硬杀/断电后已写片段仍可播。
                   必须推 —— 它是 ffmpeg 的 -movflags，留在前端等于没设。 */
                 record_frag: !!LK.frag,
                 link_mode: LK.mode,
                 clip_style: LK.clipStyle,
                 condense_mode: LK.condenseMode,
                 archive_enable: LK.archEnable,
                 archive_days: LK.archDays,
                 archive_batch: LK.archBatch,
                 archive_prefetch: !!LK.archPrefetch,
                 link_interval_h: LK.intervalH,
                 concurrency: GS.conc,
                 /* AI 检测 / 通知两块（0.9.60）：嵌套结构，服务端 norm_detect/
                    norm_notify 强校（enum 白名单 + 夹取 + trigger 至少一项） */
                 detect: { enable: !!LK.detEnable, mode: LK.detMode,
                           device: LK.detDevice, classes: LK.detCls,
                           trigger: LK.detTrigger, live_fps: LK.detFps,
                           night: LK.detNight },
                 notify: { webhook: LK.ntWebhook, motion: !!LK.ntMotion,
                           person: !!LK.ntPerson, vehicle: !!LK.ntVehicle,
                           cooldown: LK.ntCooldown,
                           /* 平台与文案模板（0.9.124）必须一起推：
                              服务端 notify 块是整块覆盖的，少推一个键就等于
                              把用户在另一台设备上的选择抹回默认。 */
                           platform: LK.ntPlatform || "auto",
                           template: LK.ntTemplate || "" },
                 /* 必须是枚举值：以前推的是 !!GS.gpu 布尔，后端把 "auto" 顶成了
                    true，于是"核显不可用 → 强制单路"的降级保护静默失效 */
                 gpu: (GS.gpu === false ? "off" : "auto") };
  const sig = JSON.stringify(body);
  if(sig === lastPushSig) return;
  lastPushSig = sig;
  API.post("/api/config", body).then(d=>{
    if(d && d.ok){
      /* 服务端把配置收敛过（比如端口/并发被夹取），以它回来的为准重新对齐一次 */
      lastPushedAt = Date.now();
      applyServerCfg(d.config);
      /* 清空标记是一次性的：服务端在 merge_cam_urls 里消费掉它并落盘。
         本地若一直留着，下次任何推送都会再删一遍地址 —— 用户在两台设备之间
         来回切换时，这足以把刚配好的地址莫名其妙清掉。 */
      CAMERAS.forEach(c=>{
        const p = camParams[c.id];
        if(p){ p.urlClear = false; p.subClear = false; }
      });
    }else if(d && d.ok === false){
      lastPushSig = "";            /* 失败允许下次重发，别把签名钉死 */
    }
  }).catch(()=>{ lastPushSig = ""; });
}
let lastPushedAt = 0;
/* 服务端配置 → 前端状态。只认白名单字段，不整份覆盖（避免把 localStorage 的
   本地预览状态冲掉），同时给「分片长度」这类数字一个统一的夹取口径。 */
function applyServerCfg(c){
  if(!c) return;
  if(c.record_segment_sec){
    const s = +c.record_segment_sec;
    if(s && s !== LK.segSec) LK.segSec = s;
  }
  /* 0.9.111：旧配置里可能存着 scheduled / record_frag=false，
     现在界面已无对应开关，若照搬会让「界面说即刻判定、实际按定时跑」。
     一律按固定值生效，下次保存即把服务端也纠正过来。 */
  LK.mode = "live";
  if(c.clip_style && c.clip_style !== LK.clipStyle) LK.clipStyle = c.clip_style;
  if(c.condense_mode && ["virtual","materialize","off"].indexOf(c.condense_mode) >= 0
     && c.condense_mode !== LK.condenseMode) LK.condenseMode = c.condense_mode;
  /* 事件侦测由 condense_mode 推导（ledger→virtual/off）；clipOut 为配置项，UI 不暴露 */
  LK.ledger = LK.condenseMode !== "off";
  LK.clipOut = LK.condenseMode === "materialize";
  if(typeof c.archive_enable === "boolean") LK.archEnable = c.archive_enable;
  if(c.archive_days){ const v = +c.archive_days; if(v >= 1) LK.archDays = v; }
  if(c.archive_batch){ const v = +c.archive_batch; if(v >= 10) LK.archBatch = v; }
  if(typeof c.archive_prefetch === "boolean") LK.archPrefetch = c.archive_prefetch;
  /* AI 检测 / 通知回读（0.9.60）：服务端是强校后的真相，本地以它为准 */
  if(c.detect && typeof c.detect === "object"){
    const d = c.detect;
    if(typeof d.enable === "boolean") LK.detEnable = d.enable;
    if(d.mode) LK.detMode = d.mode;
    if(d.device) LK.detDevice = d.device;
    if(d.classes && typeof d.classes === "object") LK.detCls = Object.assign({}, LK.detCls, d.classes);
    if(d.trigger && typeof d.trigger === "object") LK.detTrigger = Object.assign({}, LK.detTrigger, d.trigger);
    if(d.live_fps) LK.detFps = +d.live_fps || 5;
    if(d.night) LK.detNight = d.night;
  }
  if(c.notify && typeof c.notify === "object"){
    const n = c.notify;
    if(typeof n.webhook === "string") LK.ntWebhook = n.webhook;
    if(typeof n.motion === "boolean") LK.ntMotion = n.motion;
    if(typeof n.person === "boolean") LK.ntPerson = n.person;
    if(typeof n.vehicle === "boolean") LK.ntVehicle = n.vehicle;
    if(n.cooldown) LK.ntCooldown = +n.cooldown || 60;
    /* 平台 / 模板（0.9.124）：服务端强校过的值才是真相。缺键时保持当前值，
       不能因为旧服务端没这两个键就把它清成默认。 */
    if(typeof n.platform === "string" && n.platform) LK.ntPlatform = n.platform;
    if(typeof n.template === "string") LK.ntTemplate = n.template;
  }
  if(c.link_interval_h){
    const h = +c.link_interval_h;
    if(h && h !== LK.intervalH) LK.intervalH = h;
  }
  if(c.record_dir) REC.dir = c.record_dir;
  if(typeof c.record_enable === "boolean") REC.enabled = c.record_enable;
  /* 录像封装：服务端是权威（recorder 真正读的这份）。老配置里没有这个键，
     缺键时保持默认开 —— 不能因为读不到就当作用户关掉了。 */
  LK.frag = true;   /* 同上：封装固定分片 MP4，不采纳旧值 */
  if(c.cleanup_at) REC.cleanupAt = c.cleanup_at;
  /* 判读条件回读：服务端 profiles 才是判定真正用的那份参数。
     正在编辑这一路时跳过，否则会把用户的草稿来源改掉。 */
  if(c.profiles && typeof c.profiles === "object"){
    Object.keys(c.profiles).forEach(cid=>{
      const p = camParams[cid];
      const an = c.profiles[cid] && c.profiles[cid].analysis;
      if(!p || !an) return;
      if(SET.editing && state.camId === cid) return;
      p.judge = judgeFromProfile(an);
    });
  }
}

/* 服务端通道列表 → 前端。这是"谁说了算"的关键：NAS 上的配置才是权威，
   浏览器只是显示器。以前只认 localStorage，于是清缓存/换设备就回到演示数据。 */
function applyServerCams(list){
  if(!Array.isArray(list)) return false;
  /* url 回来时是脱敏的（rtsp://***@ip），不能拿去覆盖本地明文地址；
     明文只存在于本机 localStorage。所以按 id 匹配，保留本地 url，其余以服务端为准。 */
  const prev = {};
  CAMERAS.forEach((c,i)=>{
    const p = camParams[c.id] || {};
    prev[c.id] = { main: p.main || c.main || "", sub: p.sub || c.sub || "" };
  });
  CAMERAS.length = 0;
  const keepParams = camParams;
  list.forEach((c,i)=>{
    if(!c || !c.id) return;
    const name = c.name || ("摄像头 " + (i+1));
    CAMERAS.push({ id:c.id, name,
                   ch:c.ch || ("CH" + String(i+1).padStart(2,"0")),
                   res:c.res || "1080p",
                   online:!!c.online,
                   main: prev[c.id] ? prev[c.id].main : "",
                   sub: prev[c.id] ? prev[c.id].sub : "",
                   /* NAS 上有没有地址：回传的 url 是脱敏串（rtsp://***@），
                      但"非空"这个事实是可信的 —— 本机 localStorage 没有明文
                      （换设备/清缓存）时，状态显示与编辑页都要靠它，
                      不能把"本机不知道"误报成"未配置"。 */
                   hasUrl: !!c.url, hasSub: !!c.sub_url });
  });
  /* 参数：本地已有的保留，新出现的给默认；服务端带的保留天数/掩码以服务端为准 */
  const usedIds = {};
  CAMERAS.forEach((c,i)=>{
    const p = keepParams[c.id] || defaultCamParams(c, i);
    const src = list.find(x=>x && x.id === c.id) || {};
    if(src.full_days != null) p.fullDays = src.full_days;
    if(src.clip_days != null) p.clipDays = src.clip_days;
    if(src.condense != null) p.condense = !(src.condense === false || src.condense === "false");
    if(src.clip_speed != null) p.clipSpeed = [1,2,4,8].indexOf(+src.clip_speed) >= 0 ? +src.clip_speed : 1;
    if(src.clip_mode != null) p.clipMode = ["follow","join","split"].indexOf(src.clip_mode) >= 0 ? src.clip_mode : "follow";
    if(src.condense_mode != null) p.condenseMode = ["follow","virtual","materialize","off"].indexOf(src.condense_mode) >= 0 ? src.condense_mode : "follow";
    if(src.segment_sec != null) p.segSec = +src.segment_sec || 0;
    /* 每路 AI 检测（0.9.60）：服务端 cameras 数组带 detect + detect_classes，
       以服务端为准回读（与 clip_mode 同一口径：白名单 + 三键完整状态） */
    if(src.detect != null) p.detect = ["follow","on","off"].indexOf(src.detect) >= 0 ? src.detect : "follow";
    if(src.detect_classes && typeof src.detect_classes === "object"){
      const dc = src.detect_classes;
      p.detectCls = { person: !!dc.person, two_wheeler: !!dc.two_wheeler, vehicle: !!dc.vehicle };
    }
    if(src.masks) p.masks = normMasks(src.masks);
    p.judge = normJudge(p.judge, null);
    if(src.enabled === false) p.enabled = false;
    else if(src.enabled === true) p.enabled = true;
    camParams[c.id] = p;
    usedIds[c.id] = 1;
  });
  Object.keys(camParams).forEach(k=>{ if(!usedIds[k]) delete camParams[k]; });
  if(!CAMERAS.length){ state.camId = null; }
  else if(!CAMERAS.some(c=>c.id === state.camId)){ state.camId = CAMERAS[0].id; }
  return true;
}

function clockStr(){
  const t = new Date();
  return pad(t.getHours()) + ":" + pad(t.getMinutes()) + ":" + pad(t.getSeconds());
}
function snapStamp(at){
  const t = new Date(at);
  if(isNaN(t.getTime())) return "—";
  /* 以前这里硬编码 "2026-10-20 "，导致快照角标永远显示同一个日期 */
  return t.getFullYear() + "-" + pad(t.getMonth()+1) + "-" + pad(t.getDate())
    + " " + pad(t.getHours()) + ":" + pad(t.getMinutes()) + ":" + pad(t.getSeconds());
}
function agoText(sec){
  if(sec < 1) return "刚刚";
  if(sec < 60) return Math.round(sec) + " 秒前";
  return Math.floor(sec/60) + " 分前";
}

/* 首页列数：单列 / 双列，记在本地配置里（换设备不共享，跟排列偏好一致）。
   只有 1 路摄像头时单双列没区别，按钮直接隐藏。 */
const LIVE_COLS_KEY = "nvr.live.cols";
let LIVE_COLS = 1;
try{ LIVE_COLS = localStorage.getItem(LIVE_COLS_KEY) === "2" ? 2 : 1; }catch(e){}

function setLiveCols(n){
  LIVE_COLS = n === 2 ? 2 : 1;
  try{ localStorage.setItem(LIVE_COLS_KEY, String(LIVE_COLS)); }catch(e){}
  applyLiveCols();
  renderLive();
  toast("toast2", LIVE_COLS === 2 ? "已切换为双列 · 一屏四路" : "已切换为单列 · 大画面");
}
function applyLiveCols(){
  const wrap = document.getElementById("liveWrap");
  if(wrap) wrap.classList.toggle("two", LIVE_COLS === 2);
  const ic = document.getElementById("colsIcon");
  if(ic) ic.innerHTML = LIVE_COLS === 2
    ? '<rect x="3" y="4" width="7.5" height="16" rx="1.6"/><rect x="13.5" y="4" width="7.5" height="16" rx="1.6"/>'
    : '<rect x="4" y="3" width="16" height="7" rx="1.6"/><rect x="4" y="14" width="16" height="7" rx="1.6"/>';
  const b = document.getElementById("btnLiveCols");
  if(b) b.style.display = CAMERAS.length > 1 ? "" : "none";
  renderColsPop();
}
function renderColsPop(){
  const box = document.getElementById("colsGrid");
  if(!box) return;
  const now = document.getElementById("colNow");
  if(now) now.textContent = LIVE_COLS === 2 ? "双列" : "单列";
  box.innerHTML = [[1,"单列"], [2,"双列"]].map(([n, t])=>{
    const svg = n === 1
      ? '<svg viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="7" rx="1.6"/><rect x="4" y="14" width="16" height="7" rx="1.6"/></svg>'
      : '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="7.5" height="16" rx="1.6"/><rect x="13.5" y="4" width="7.5" height="16" rx="1.6"/></svg>';
    return '<button data-cols="' + n + '" class="' + (LIVE_COLS === n ? "on" : "") + '">' + svg + t + '</button>';
  }).join("");
  box.querySelectorAll("[data-cols]").forEach(el=>{
    el.onclick = e=>{ e.stopPropagation(); setLiveCols(+el.dataset.cols); toggleColsPop(null, false); };
  });
}
function toggleColsPop(e, force){
  const pop = document.getElementById("colPop");
  if(!pop) return;
  if(e) e.stopPropagation();
  const want = (typeof force === "boolean") ? force : !pop.classList.contains("on");
  const rp = document.getElementById("ratePop");
  if(rp) rp.classList.remove("on");
  pop.classList.toggle("on", want);
  if(want) renderColsPop();
}

/* 旧左上角录制态徽标已下线：摄像头画面自带的时间戳
   多在左上，徽标正好盖住它。录制状态改由右下角状态点 + 设置页诊断区承担；
   已停用/未配置仍由 nophoto 覆盖层如实说明。 */
function snapTime(at){
  const t = new Date(at);
  if(isNaN(t.getTime())) return "--:--:--";
  return pad(t.getHours()) + ":" + pad(t.getMinutes()) + ":" + pad(t.getSeconds());
}

function renderLive(){
  const wrap = document.getElementById("liveWrap");
  if(!wrap) return;
  wrap.innerHTML = "";
  applyLiveCols();
  if(state.liveCam >= CAMERAS.length) state.liveCam = 0;
  if(!CAMERAS.length){
    wrap.innerHTML = '<div class="empty">还没有摄像头<br>去「设置 · 摄像头」里添加一台，填入主码流 RTSP 地址</div>';
    updateLiveHeader();
    return;
  }
  CAMERAS.forEach((c,i)=>{
    const st = snapSt(c.id);
    const p = camParams[c.id] || {};
    const ready = !!((p.main || c.hasUrl) && p.enabled !== false);
    const d = document.createElement("div");
    d.className = "livecard" + (ready ? "" : " off");
    d.innerHTML =
      '<img class="snap-img" alt="">' +
      '<div class="nophoto"><span class="np-ico">◐</span><span data-np>' +
        (ready ? "等待第一帧…" : (p.enabled === false ? "已停用 · 去设置里启用" : "未配置 RTSP 地址")) +
      '</span></div>' +
      /* 左上不再放徽标：摄像头画面自带的时间戳/厂标多在左上，叠上去正好盖住。
         状态收敛到右下一颗点（绿=正常/红=异常）+ 新鲜度文字。 */
      '<div class="lv-osd mono" data-stamp>' + (st.shownAt ? snapTime(st.shownAt) : "--:--:--") + '</div>' +
      '<div class="lv-name">' + esc(c.name) + '<em>' + esc(c.ch) + '</em></div>' +
      '<div class="snap-ago" data-ago><i class="dot"></i><span data-ago-txt>-- 秒前</span></div>';
    d.onclick = e=>{
      if(e.target.closest(".snap-ago.retry")) return;
      if(!ready && p.enabled === false){ toast("toast2", "这一路已停用 · 先在设置里启用"); return; }
      openLiveFull(i);
    };
    wrap.appendChild(d);
    d._camIdx = i;
    const snapImg = d.querySelector(".snap-img");
    /* 重渲直播墙时（每次切回直播页都会）先把上一次成功的那张立刻贴回去：
       同 URL，浏览器缓存里就有，不会先闪一下"等待第一帧"再出图（0.9.88） */
    if(st.lastSrc && snapImg){ snapImg.src = st.lastSrc; d.classList.add("hasphoto"); }
    if(state.wallStream && ready){
      mountLiveStream(d, c, true);
    }else{
      pullSnap(c.id, snapImg, ok=>{
        if(ok){
          const s = d.querySelector("[data-stamp]");
          if(s) s.textContent = snapTime(snapSt(c.id).shownAt);
        }
        updateSnapLabels();
      });
    }
  });
  const hint = document.createElement("div");
  hint.className = "livehint";
  hint.textContent = state.wallStream
    ? "连续播放中 · 点任意画面进入单路全屏（可暂停、可退出）"
    : "快照每 " + SNAP.interval + " 秒自动刷新 · 点任意画面进入单路全屏播放";
  wrap.appendChild(hint);

  updateSnapLabels();
  updateLiveHeader();
  syncWallBar();
}

/* 直播页头部计数（原来写死在 HTML 里，删了摄像头也不会变） */
function updateLiveHeader(){
  const on = CAMERAS.filter(c=>{
    const p = camParams[c.id];
    return c.online && p && (p.main || c.hasUrl) && p.enabled !== false;
  }).length;
  const el = document.getElementById("liveOnline");
  if(el){
    if(state.wallStream){
      el.textContent = "● 连续播放";
      el.style.color = "var(--live)";
    }else{
      el.textContent = "● " + on + " 台在线";
      el.style.color = on ? "var(--keep)" : "var(--faint)";
    }
  }
  const m = document.getElementById("liveMode");
  if(m) m.textContent = state.wallStream
    ? (CAMERAS.length + " 路 · 子码流实时")
    : "快照 · " + SNAP.interval + " 秒刷新";
}

/* 底部那条按钮：连续播放 ↔ 回到快照 */
function syncWallBar(){
  const btn = document.getElementById("wallPlay");
  if(!btn) return;
  const txt = document.getElementById("wallTxt");
  const ico = document.getElementById("wallIcon");
  const st = document.getElementById("wallStat");
  const ready = CAMERAS.filter(c=>{
    const p = camParams[c.id] || {};
    return (p.main || c.hasUrl) && p.enabled !== false;
  }).length;
  btn.disabled = !ready && !state.wallStream;
  btn.classList.toggle("stop", state.wallStream);
  if(txt) txt.textContent = state.wallStream ? "回到快照" : ("连续播放" + (ready ? " · " + ready + " 路" : ""));
  if(ico) ico.innerHTML = state.wallStream
    ? '<rect x="6" y="6" width="12" height="12" rx="2"/>'
    : '<path d="M8 5v14l11-7z"/>';
  if(st){
    if(state.wallStream){
      const mins = Math.max(0, Math.floor((Date.now() - state.wallAt) / 60000));
      st.innerHTML = '<i></i>实时 · 已播 ' + (mins < 1 ? "不到 1 分钟" : mins + " 分钟");
    }else{
      st.innerHTML = ready ? ("快照 · " + SNAP.interval + " 秒刷新") : "没有可播放的通道";
    }
  }
}
function toggleWall(){
  if(state.wallStream){
    state.wallStream = false;
    renderLive();
    toast("toast2", "已回到快照 · 实时流已断开");
    return;
  }
  const ready = CAMERAS.filter(c=>{
    const p = camParams[c.id] || {};
    return (p.main || c.hasUrl) && p.enabled !== false;
  }).length;
  if(!ready){ toast("toast2", "没有可播放的通道 · 先去设置里填 RTSP 地址"); return; }
  state.wallStream = true;
  state.wallAt = Date.now();
  renderLive();
  toast("toast2", "已开始连续播放 · 当前画面原地换成实时流");
}

function updateSnapLabels(){
  const now = Date.now();
  document.querySelectorAll("#liveWrap .livecard").forEach(card=>{
    const i = card._camIdx;
    const c = CAMERAS[i];
    if(!c) return;
    const st = snapSt(c.id);
    const hasPhoto = card.classList.contains("hasphoto");
    /* 正在播实时流（且没回落快照）的卡片：快照已停更，age 必然越涨越大，
       不能按快照口径判异常 —— 实时流本身活着就是正常，绿点。 */
    const live = card.classList.contains("streaming") && !card.classList.contains("fallback");
    /* age 是"距上次成功的时间"；一次都没成功过时 at=0，不能拿它算倒计时 */
    const age = st.at ? (now - st.at)/1000 : Infinity;
    const bad = live ? false : (!c.online || (st.fails > 0) || age > SNAP.interval * 2.2);
    const np = card.querySelector("[data-np]");
    if(np && !hasPhoto){
      /* 三种真实状态，别再一律 "等待第一帧…" */
      if(st.pend) np.textContent = "正在拉取…";
      else if(st.since && now - st.since > 25000) np.textContent = "拉流失败 · 检查 RTSP 地址";
      else if(st.fails > 0) np.textContent = "拉流失败 · 正在重试";
      else np.textContent = "等待第一帧…";
    }
    const ago = card.querySelector("[data-ago-txt]");
    const box = card.querySelector("[data-ago]");
    if(box) box.classList.remove("retry");
    /* 右下角状态点：绿=正常，红=异常（离线/拉图失败/快照过期/实时流回落） */
    if(box) box.classList.toggle("bad", bad || card.classList.contains("fallback"));
    if(ago && !live){
      if(card.classList.contains("fallback")){
        /* 实时流起不来：如实说明已回落快照，不冒充实时；
           连快照都拿不到才给「重试」，点了只重连这一格。 */
        if(!hasPhoto && st.fails > 0){
          ago.textContent = "无画面 · 重试";
          if(box) box.classList.add("retry");
        }else{
          ago.textContent = "快照 · " + SNAP.interval + "s";
        }
      }else if(st.fails > 0 && !hasPhoto) ago.textContent = "未拿到画面";
      else if(!c.online) ago.textContent = "离线";
      else if(!st.at) ago.textContent = "-- 秒前";
      else ago.textContent = agoText(age);
    }
  });
  if(state.wallStream) syncWallBar();
}

/* 实时画面的右上角时刻（连续播放卡片 data-stamp / 播放层瓦片 data-tf 共用）：
   流活着 → 跟当前钟走；流断了 → 冻结在断流那一刻；快照续上的 → 交给快照回调。
   暂停态不碰（画面冻结在最后一帧，时刻也跟着停）。 */
function syncLiveStamps(){
  const nowStr = clockStr();
  document.querySelectorAll("#liveWrap .livecard").forEach(card=>{
    if(!state.wallStream) return;
    const c = CAMERAS[card._camIdx]; if(!c) return;
    const st = snapSt(c.id);
    const s = card.querySelector("[data-stamp]"); if(!s) return;
    if(card.classList.contains("streaming") && !card.classList.contains("fallback")){
      s.textContent = st.live ? nowStr : (st.frozen ? snapTime(st.frozen) : "--:--:--");
    }else if(card.classList.contains("fallback") && st.frozen && !st.at){
      s.textContent = snapTime(st.frozen);   /* 断了且快照还没续上：停在断流时刻 */
    }
  });
  document.querySelectorAll("#lrBody [data-camid]").forEach(tile=>{
    const st = snapSt(tile.dataset.camid);
    const tf = tile.querySelector("[data-tf]"); if(!tf) return;
    if(tile.classList.contains("streaming") && !tile.classList.contains("fallback")){
      tf.textContent = st.live ? nowStr : (st.frozen ? snapTime(st.frozen) : "--:--:--");
    }else if(!tile.classList.contains("streaming") && st.frozen && !st.at){
      tf.textContent = snapTime(st.frozen);
    }
  });
  /* 播放层瓦片的右下状态点：口径与快照卡完全一致（绿=正常/红=异常）。
     room 打开时 tickSnaps 不跑，靠这里的两个 tick 驱动。 */
  document.querySelectorAll("#lrBody [data-camid]").forEach(tile=>{
    const cid = tile.dataset.camid;
    const c = CAMERAS.find(x=>x.id === cid); if(!c) return;
    const st = snapSt(cid);
    const box = tile.querySelector("[data-lago]"); if(!box) return;
    const hasPhoto = tile.classList.contains("hasphoto");
    const live = tile.classList.contains("streaming") && !tile.classList.contains("fallback");
    const age = st.at ? (Date.now() - st.at)/1000 : Infinity;
    const bad = live ? false : (!c.online || st.fails > 0 || age > SNAP.interval * 2.2);
    box.classList.toggle("bad", bad || tile.classList.contains("fallback"));
    const txt = tile.querySelector("[data-lago-txt]");
    box.classList.remove("retry");
    if(txt){
      if(live){
        txt.textContent = "";                  /* 流活着：裸绿点，不加文字 */
      }else if(tile.classList.contains("fallback")){
        if(!hasPhoto && st.fails > 0){
          txt.textContent = "无画面 · 重试";
          box.classList.add("retry");
        }else{
          txt.textContent = "快照 · " + SNAP.interval + "s";
        }
      }else if(st.fails > 0 && !hasPhoto) txt.textContent = "未拿到画面";
      else if(!c.online) txt.textContent = "离线";
      else if(!st.at) txt.textContent = "";
      else txt.textContent = agoText(age);
    }
  });
}

function tickSnaps(dt){
  snapAcc += dt;
  if(snapAcc < 0.4) return;
  snapAcc = 0;
  livePollAcc += 0.4;
  if(livePollAcc >= 5){ livePollAcc = 0; pollLive(); }   // 每 ~5 秒同步一次服务端真实状态
  const now = Date.now();
  document.querySelectorAll("#liveWrap .livecard").forEach(card=>{
    const i = card._camIdx;
    const c = CAMERAS[i];
    if(!c) return;
    const st = snapSt(c.id);
    /* 连续播放中：已经在播流的卡片不许再用快照盖上去；
       只有"流起不来回落到快照"的那一格才在这里续图。 */
    if(state.wallStream && card.classList.contains("streaming") && !card.classList.contains("fallback")) return;
    /* 只看"距上次成功拿到图"过了多久。拉流失败时 at 不会前推，
       于是这里每 tick 都会重试（每 0.4s 一次太密，pend 会挡住），
       倒计时则老老实实停在 0 并显示未更新。 */
    if(!st.pend && now - st.at >= SNAP.interval * 1000){
      pullSnap(c.id, card.querySelector(".snap-img"), ok=>{
        const s = card.querySelector("[data-stamp]");
        if(ok && s) s.textContent = snapTime(st.shownAt);
      });
    }
  });
  /* 实时层：只有"流没起来回落到快照"的瓦片才需要在这里续图。
     暂停态整层冻结（不再拉图），这正是"暂停"该有的样子。 */
  const roomOn = document.getElementById("liveRoom").classList.contains("on");
  if(roomOn && state.liveStream){
    document.querySelectorAll("#lrBody [data-camid]").forEach(tile=>{
      if(tile.classList.contains("streaming")) return;   // 流播放中，别用快照覆盖
      const cid = tile.dataset.camid;
      const st = snapSt(cid);
      if(st.pend || now - st.at < SNAP.interval * 1000) return;
      pullSnap(cid, tile.querySelector(".snap-img"), ok=>{
        if(ok) tile.classList.add("hasphoto");
      });
    });
  }
  updateSnapLabels();
  syncLiveStamps();
}

/* 回到前台立刻补一次快照，不等下一次定时轮询（0.9.88）：
   手机锁屏再打开、切到别的 App 再切回来，上一张往往已经旧了。
   pullSnap 由 pend 去重，和 tickSnaps 撞上也不会叠请求。 */
function refreshLiveSnapsNow(){
  if(typeof activePage === "function" && activePage() !== "live") return;
  document.querySelectorAll("#liveWrap .livecard").forEach(card=>{
    const c = CAMERAS[card._camIdx];
    if(!c) return;
    if(state.wallStream && card.classList.contains("streaming") && !card.classList.contains("fallback")) return;
    pullSnap(c.id, card.querySelector(".snap-img"), ok=>{
      const s = ok ? card.querySelector("[data-stamp]") : null;
      if(s) s.textContent = snapTime(snapSt(c.id).shownAt);
      updateSnapLabels();
    });
  });
}
document.addEventListener("visibilitychange", ()=>{
  if(document.visibilityState === "visible") refreshLiveSnapsNow();
});
window.addEventListener("pageshow", ()=> refreshLiveSnapsNow());

/* online 必须以服务端真实快照三态（/api/live 的 snap）为准：
   localStorage 里存的 online 是保存那一刻的静态值，服务端快照恢复后如果不回写，
   前端会永远认为离线——不刷新画面、顶栏一直「0 台在线」。 */
let livePollAcc = 0;
function pollLive(){
  if(typeof fetch !== "function") return;
  API.get("/api/live")
    .then(d=>{
      if(!d || !Array.isArray(d.cameras)) return;
      let changed = false;
      d.cameras.forEach(sc=>{
        const c = CAMERAS.find(x=>x.id === sc.id);
        if(!c) return;
        const on = sc.snap === true;             // 只有快照真正成功才算在线
        if(c.online !== on){ c.online = on; changed = true; }
      });
      if(changed){
        updateLiveHeader();
        /* 连续播放中不重建卡片 —— 重建等于把正在播的流全掐掉重连。
           在线状态变了只更新角标即可。 */
        if(state.wallStream) updateSnapLabels();
        else renderLive();
      }
    }).catch(()=>{});
}

/* ---------- 实时播放层 ---------- */
function enterLive(idx){
  state.liveView = "single";
  state.liveCam = idx;
  state.livePaused = false;
  state.liveStream = true;     // 进入实时观看默认播真实流
  document.getElementById("liveRoom").classList.add("on");
  buildLiveRoom();
  pushLayer("live");           // 压一层：左滑先退出实时层，而不是退出应用
}

/* 首页点卡片 = 直接进单路全屏横屏（设计稿 ⑧）。
   以前点一下只是把实时层铺在页面里，还得再点一次全屏 —— 多一步，而且
   在手机上"铺在页面里"和"真全屏"完全是两种观感。 */
function openLiveFull(idx){
  enterLive(idx);
  state.lrFs = true;
  state.lrChrome = false;             // 进来就在播，底栏先收起
  const room = document.getElementById("liveRoom");
  room.classList.add("fs");
  syncLiveSub();                      /* 全屏形态下状态行少一段，重刷 */
  syncLiveChrome();
  if(!isFullscreen()) enterFull(null, { noExit: true });
  requestAnimationFrame(()=>{ setLand(true); layout(); });
}

function closeLiveRoom(){
  state.liveView = "snap";
  state.livePaused = false;
  state.liveStream = false;
  state.lrFs = false;
  state.lrChrome = true;
  /* 主动断开 MJPEG：把 img 的 src 摘掉并清空容器，浏览器才会真的关连接，
     服务端随即杀掉那几路转发 ffmpeg。只加 class 不管 url 的话，连接会挂着。 */
  document.querySelectorAll("#lrBody .snap-img").forEach(img=>{
    img.onload = img.onerror = null;
    img.removeAttribute("src");
  });
  document.getElementById("lrBody").innerHTML = "";
  const room = document.getElementById("liveRoom");
  room.classList.remove("on", "fs", "chrome", "paused");
  renderLive();          // 退出时刷新一遍快照，年龄从头算
}

function leaveLive(){
  /* 全屏里退出播放层：先把层收掉再退全屏，顺序反了会绕两圈
     （exitFull 会回头调 closeLiveRoom）。 */
  if(isFullscreen() || state.land){
    if(state.lrFs) state.lrFs = false;
    closeLiveRoom();
    exitFull();
    return;
  }
  closeLiveRoom();
}

/* MJPEG 实时流：<img> 直接吃 multipart 流。
   分辨率与帧率由前端按场景给：宫格 4 帧 480 宽（4~6 路同时播也扛得住），
   单画面 8 帧 960 宽。以前服务端把参数写死，宫格根本没走流、只拉快照，
   于是"看起来是几秒前的画面，也不动"。 */
const STREAM_Q = { single:"fps=8&w=960", grid:"fps=4&w=480" };
function liveStreamUrl(cid, grid){
  return "/api/live/stream/" + encodeURIComponent(cid)
    + "?" + (grid ? STREAM_Q.grid : STREAM_Q.single) + "&t=" + Date.now();
}

/* 给一块瓦片挂流。失败不装死：立刻说明原因并回落快照刷新。 */
function mountLiveStream(tile, c, grid){
  const img = tile.querySelector(".snap-img");
  const np  = tile.querySelector("[data-np]");
  if(!img) return;
  tile.classList.add("streaming");
  tile.classList.remove("hasphoto", "fallback");
  img.onload = ()=>{ tile.classList.add("hasphoto"); tile.classList.remove("fallback");
    const st = snapSt(c.id); st.live = true; st.frozen = 0; };
  img.onerror = ()=>{
    /* 子码流拉不起来：摄像头没配子码流、并发到顶、或 ffmpeg 拒收。
       以前这里什么都不做 —— 覆盖层一直挂着，用户看到的就是"永远等待第一帧"。 */
    const st = snapSt(c.id);
    st.live = false;
    if(!st.frozen) st.frozen = Date.now();   /* 记下断流时刻，右上角停在这里 */
    tile.classList.remove("streaming", "hasphoto");
    tile.classList.add("fallback");
    if(np) np.textContent = "实时流不可用 · 已回落快照";
    pullSnap(c.id, img, ok=>{ if(ok) tile.classList.add("hasphoto"); });
  };
  img.src = liveStreamUrl(c.id, grid);
}

/* 暂停/播放：单画面与宫格一起管。暂停 = 把 img 换成一张静态快照并停止轮询。 */
function toggleLiveStream(){
  state.liveStream = !state.liveStream;
  state.lrChrome = true;
  buildLiveRoom();
  syncLiveChrome();
  toast("toast2", state.liveStream ? "已开始实时播放" : "已暂停 · 画面冻结");
}

/* 全屏时：底栏播放中自动收起，暂停或轻点画面浮现；顶部状态行常驻（它是唯一的退出出口） */
function syncLiveChrome(){
  const room = document.getElementById("liveRoom");
  if(!room) return;
  room.classList.toggle("chrome", !!state.lrChrome);
  room.classList.toggle("paused", !state.liveStream);
  clearTimeout(syncLiveChrome._h);
  if(state.lrFs && state.liveStream && state.lrChrome){
    syncLiveChrome._h = setTimeout(()=>{ state.lrChrome = false; syncLiveChrome(); }, 2600);
  }
}
function nudgeLiveChrome(){
  state.lrChrome = true;
  syncLiveChrome();
}

function buildLiveRoom(){
  const body = document.getElementById("lrBody");
  const room = document.getElementById("liveRoom");
  if(!CAMERAS.length){                       /* 全删光了就退回快照墙 */
    closeLiveRoom();
    return;
  }
  if(!CAMERAS[state.liveCam]) state.liveCam = 0;
  const grid = state.liveView === "grid";
  const total = CAMERAS.length;

  document.getElementById("lrGridTxt").textContent = grid ? "单路" : "宫格";
  document.getElementById("lrHint").textContent = grid
    ? "点任意窗口放大 · 全部 " + total + " 路同时在播"
    : (state.liveStream ? "轻点画面显示控制条" : "点画面继续播放");

  /* 布局随摄像头数量自适应：1→1列，2~4→2列，5~9→3列 */
  const cols = grid ? Math.min(3, Math.ceil(Math.sqrt(total))) : 1;
  body.style.gridTemplateColumns = "repeat(" + cols + ",minmax(0,1fr))";

  const list = grid ? CAMERAS.map((_,i)=>i) : [state.liveCam];
  body.innerHTML = "";
  list.forEach(i=>{
    const c = CAMERAS[i];
    const tile = document.createElement("div");
    tile.className = "lr-tile" + (grid && i === state.liveCam ? " cur" : "");
    tile.dataset.camid = c.id;
    tile.innerHTML = '<img class="snap-img" alt="">' +
      '<div class="nophoto"><span class="np-ico">◐</span><span data-np>等待第一帧…</span></div>' +
      '<div class="tf mono" data-tf>' + clockStr() + '</div>' +
      '<div class="tn">' + esc(c.name) + '<em>' + esc(c.ch) + '</em></div>' +
      /* 右下状态点：与快照卡同一套 .snap-ago 视觉（绿=正常/红=异常），
         全屏下它是常驻的（不随控制条收起）；异常胶囊可点，只重连这一格。 */
      '<div class="snap-ago" data-lago><i class="dot"></i><span data-lago-txt></span></div>' +
      ((c.online || !!(camParams[c.id] && (camParams[c.id].main || c.hasUrl))) ? "" : '<div class="off">离线</div>');
    const lago = tile.querySelector("[data-lago]");
    lago.onclick = (e)=>{
      e.stopPropagation();                     /* 别触发画面点击（暂停/控制条） */
      if(!lago.classList.contains("retry")) return;
      tile.classList.remove("fallback");
      mountLiveStream(tile, c, state.liveView === "grid");
      toast("toast2", "正在重连 " + c.name);
    };
    tile.onclick = ()=>{
      /* 全屏/单路：轻点画面 = 显示控制条（暂停时点画面 = 继续播放）
         宫格里点某一格 = 放大到这一路 */
      if(grid && state.liveStream && state.liveView === "grid" && !state.lrFs){
        state.liveCam = i; state.liveView = "single"; buildLiveRoom();
        toast("toast2", "已放大 " + CAMERAS[i].name);
        return;
      }
      if(!state.liveStream){ toggleLiveStream(); return; }
      if(state.lrFs){ state.lrChrome = !state.lrChrome; syncLiveChrome(); }
      else toggleLiveStream();
    };
    body.appendChild(tile);
    /* 单画面和宫格都走实时流 —— 「宫格不播」正是上一版的老毛病 */
    if(state.liveStream){
      mountLiveStream(tile, c, grid);
    }else{
      pullSnap(c.id, tile.querySelector(".snap-img"), ok=>{
        if(ok) tile.classList.add("hasphoto");
      });
    }
  });

  const c = CAMERAS[state.liveCam];
  document.getElementById("lrName").textContent = grid ? "全部 " + total + " 路" : c.name;
  syncLiveSub();
  document.getElementById("lrClock").textContent = clockStr();
  room.classList.toggle("fs", !!state.lrFs);
  syncLivePlayBtn();
  syncLiveChrome();
}

/* 顶部状态行：在播 / 已暂停、哪一路、在不在录 —— 用户随时能确认。
   全屏时不重复名字（标题上已经有了），进出全屏都要刷新一次。 */
function syncLiveSub(){
  const c = CAMERAS[state.liveCam];
  if(!c) return;
  const st = document.getElementById("lrSub");
  if(!st) return;
  const grid = state.liveView === "grid";
  const total = CAMERAS.length;
  if(state.liveStream){
    const p = camParams[c.id] || {};
    const rec = (REC.enabled && p.enabled !== false && (p.main || c.hasUrl))
      ? '<span class="sep">|</span><span class="rec">● REC 全时</span>'
      : '<span class="sep">|</span><span class="mono">未录制</span>';
    st.innerHTML = '<i></i><b>直播中</b>'
      + (state.lrFs ? "" : '<span class="sep">|</span><span class="mono">'
         + (grid ? ("全部 " + total + " 路") : (esc(c.name) + " · " + esc(c.ch))) + '</span>')
      + '<span class="sep">|</span><span class="mono">' + (grid ? "子码流" : "子码流实时") + '</span>'
      + rec;
  }else{
    st.innerHTML = '<i style="background:#8b93a1"></i><b>已暂停</b>'
      + '<span class="sep">|</span><span class="mono">画面冻结在最后一帧</span>';
  }
}

/* 底部控制条上的播放/暂停按钮：与画面分离，不遮挡任何内容 */
function syncLivePlayBtn(){
  const b = document.getElementById("lrPlay");
  if(!b) return;
  b.classList.toggle("paused", !state.liveStream);
  const t = b.querySelector("span");
  if(t) t.textContent = state.liveStream ? "暂停" : "播放";
  const i = b.querySelector("i");
  if(i) i.className = state.liveStream ? "on" : "";
}

function syncDiagSum(){
  /* 直播页那条存储栏已经整体搬进「设置 · 诊断与日志」。折叠态的摘要里
     有「剩余 xx」，系统状态轮询回来后就地改这一行，不必重渲染整个设置页
     （重渲染会打断正在编辑的草稿）。 */
  const el = document.getElementById("diagSum");
  if(!el) return;
  el.textContent = diagSummary();
}
const LIVE_PAL = [
  { top:"#262c34", bot:"#3a4149", acc:"#4d5560", kind:"yard" },
  { top:"#23272d", bot:"#454c55", acc:"#5b636e", kind:"garage" },
  { top:"#2b3038", bot:"#363c44", acc:"#4a525d", kind:"room" },
  { top:"#20242a", bot:"#3f454e", acc:"#545c68", kind:"hall" },
];
function drawLiveScene(cv, idx){
  if(!cv) return;
  const cam = CAMERAS[idx];
  if(!cam) return;
  const rr = cv.getBoundingClientRect();
  if(!rr.width) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = rr.width, h = w*9/16;
  if(cv.width !== Math.round(w*dpr)){
    cv.width = Math.round(w*dpr); cv.height = Math.round(h*dpr);
    cv.style.height = h + "px";
  }
  const c = cv.getContext("2d");
  c.setTransform(dpr,0,0,dpr,0,0);
  const t = performance.now()/1000;
  const P = LIVE_PAL[idx % 4];

  c.fillStyle = P.top; c.fillRect(0,0,w,h*0.38);
  c.fillStyle = P.bot; c.fillRect(0,h*0.38,w,h*0.62);

  if(P.kind === "yard"){
    c.fillStyle = "#333a44"; c.fillRect(w*0.06,h*0.30,w*0.26,h*0.26);
    c.fillStyle = "#3f4a3a";
    for(let i=0;i<5;i++){ c.beginPath(); c.arc(w*(0.08+i*0.055), h*0.30, w*0.032, 0, Math.PI*2); c.fill(); }
    c.fillStyle = P.acc; c.fillRect(w*0.62,h*0.55,w*0.3,h*0.05);
  }else if(P.kind === "garage"){
    c.fillStyle = "#2b323b"; roundRect(c, w*0.10,h*0.48,w*0.46,h*0.30,6); c.fill();
    roundRect(c, w*0.14,h*0.52,w*0.38,h*0.14,5); c.fillStyle="rgba(150,180,205,.20)"; c.fill();
    c.fillStyle = "#171a1f";
    c.beginPath(); c.arc(w*0.20,h*0.80,w*0.045,0,Math.PI*2); c.fill();
    c.beginPath(); c.arc(w*0.46,h*0.80,w*0.045,0,Math.PI*2); c.fill();
  }else if(P.kind === "room"){
    c.fillStyle = P.acc; c.fillRect(w*0.08,h*0.56,w*0.34,h*0.13);
    c.fillRect(w*0.50,h*0.52,w*0.4,h*0.17);
    c.fillStyle = "rgba(190,205,220,.14)"; c.fillRect(w*0.12,h*0.20,w*0.2,h*0.16);
  }else{
    c.fillStyle = P.acc;
    c.fillRect(w*0.2,h*0.42,w*0.05,h*0.5);
    c.fillRect(w*0.75,h*0.42,w*0.05,h*0.5);
    c.fillStyle = "rgba(200,215,230,.10)"; c.fillRect(w*0.30,h*0.24,w*0.4,h*0.2);
  }

  /* 移动的人影 */
  const px = w*((t*0.045 + idx*0.37) % 1.15 - 0.07);
  const ph2 = h*0.24;
  c.fillStyle = "#c3ccd8";
  c.fillRect(px-ph2*0.13, h*0.78-ph2*0.6, ph2*0.26, ph2*0.52);
  c.beginPath(); c.arc(px, h*0.78-ph2*0.7, ph2*0.115, 0, Math.PI*2); c.fill();

  /* 噪点 + 暗角（模拟子码流压缩感） */
  const rnd = mulberry(hashStr(CAMERAS[idx].id) + Math.floor(t*3));
  c.fillStyle = "rgba(255,255,255,.035)";
  for(let i=0;i<70;i++) c.fillRect(rnd()*w, rnd()*h, 1.2, 1.2);
  const g = c.createRadialGradient(w/2,h/2,Math.min(w,h)*0.34,w/2,h/2,Math.max(w,h)*0.74);
  g.addColorStop(0,"rgba(0,0,0,0)"); g.addColorStop(1,"rgba(0,0,0,.42)");
  c.fillStyle = g; c.fillRect(0,0,w,h);
}
