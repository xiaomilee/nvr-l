/* ================= pages/timeline.js =================
   回放中按秒定位的那一整套：Aqara 式垂直时间轴（缩放 / 上下留白 / 刻度 /
   事件气泡与三车道 / 连线 / 跟随）、常驻画面区（双 video 预加载、类别筛选、
   活动模式"我在哪 / 下一个是谁"、OSD 与控制条同步、默认落点与续播）。
   判断标准：所有绑着「当天 0 点起的浮点秒」与 video.currentTime 的逻辑放这里；
   按天看"这一天有哪些片"在 clips.js，取数与跨页同步在 playback_data.js。
   为什么这 1200 行不再切碎：时间轴坐标、气泡分组、常驻画面区的预加载与
   pvOnTime 是同一条时钟链上的环节，改一处缩放就得连着看气泡落点与跟随；
   按控件名切成七八份，只会让"点了事件却跳错秒"变成跨文件追查。
   ==================================================== */

/* ================= 回放页（Aqara 式垂直时间轴） =================
   数据源：/api/ledger（events + avail），与文件页共享 state.camId /
   dayList / dayIdx，两页切机位、切日期始终同步。
   P0 范围：全天时间轴渲染 + 定位 + 事件列表；点标记的 seek 连播随 P1
   常驻画面区一起落地。坐标约定与后端一致：当天 0 点起的浮点秒。 */
const VTL_TOP = 30;                        // 顶部留 30px（0:00 刻度标签的上方余量）
const VTL_PXH_DEF = 120, VTL_PXH_MIN = 30, VTL_PXH_MAX = 600;
let vtlPxh = VTL_PXH_DEF;                  // 1 小时 = 120px（旧版 60 太挤，热点事件互相叠）
let vtlData = null, vtlDay = null;         // 最近一次台账缓存，缩放重绘时复用
/* 事件线 → 气泡的连接曲线（0.9.90）：svg 层与 #vtl 内容同层，path 里直接写内容坐标
   （不设 viewBox，避免被缩放），滚动由外层 vtlscroll 承担 —— 所以曲线天然跟着内容走。
   vtlMarks 记下每个可点的横线标注（含它代表的事件组），供「播放扫过自动跟随」查表。 */
const SVG_NS = "http://www.w3.org/2000/svg";
let vtlWireSvg = null, vtlMarks = [];
/* ---------- 上下留白（0.9.68）----------
   固定时刻线钉在视口自下而上 2/3 高度（距顶 1/3 处）。以前内容顶端只留 30px、
   底端 24px，于是滚动到两端时 00:00:00 / 24:00:00 还离时刻线很远 —— 想定位
   当天最前面/最后面只能凭感觉，全时轴缩到最小时（30px/h）甚至差出小半个屏。
   现在内容上下各补一段空白：顶上补到「线到视口顶的距离」，底下补到「线到视口底
   的距离」，滚到两端时 00:00 与 24:00 正好压在线上。留白随视口高度变化（切页、
   全屏、旋转、窗口缩放都会重算），与缩放倍率无关 —— 倍率只影响 1 小时占多少像素。 */
let vtlPadTop = 0, vtlPadBot = 0;
let vtlScH = 0;                            // 上次量到的视口高度，用于发现「高度变了」
function vtlTop(){ return VTL_TOP + vtlPadTop; }
function vtlY(sec){ return vtlTop() + sec / 3600 * vtlPxh; }
/* 按当前视口高度重算上下留白。页面刚显示量不到高度（clientHeight=0）时沿用上次的值 */
function vtlSyncPad(){
  const sc = document.getElementById("vtlScroll");
  let h = (sc && typeof sc.clientHeight === "number") ? sc.clientHeight : 0;
  if(h < 40) h = vtlScH || 0;
  vtlScH = h;
  const line = Math.round(h / 3);            // 固定时刻线距视口顶
  vtlPadTop = Math.max(0, line - VTL_TOP);
  vtlPadBot = Math.max(0, h - line - 24);    // 线下那段也要留够（底部 24px 是原留白）
  return h;
}
function vtlClampPxh(v){
  return Math.max(VTL_PXH_MIN, Math.min(VTL_PXH_MAX, v));
}
try{
  const v = +localStorage.getItem("nvr_vtl_pxh");
  if(v >= VTL_PXH_MIN && v <= VTL_PXH_MAX) vtlPxh = v;
}catch(e){}

/* 缩放：factor>1 放大。锚点 = anchorSec（该时刻在缩放前后留在屏幕
   anchorY 位置不动），不传则锚定视口中心——缩放时视线不跳。 */
function vtlZoom(factor, anchorSec, anchorY){
  const sc = document.getElementById("vtlScroll");
  if(!sc) return false;
  if(anchorSec == null){
    /* 无显式锚点：优先锚可见的播放点（缩放围绕当前画面，播放点不漂出视口）；
       否则锚固定时刻线压着的时刻（自下而上 2/3 高度处）——缩放后线下的时刻还在线下 */
    if(PPLAY.on && PPLAY.day === vtlDay){
      const py = vtlY(PPLAY.t) - sc.scrollTop;
      if(py >= 0 && py <= sc.clientHeight){ anchorY = py; anchorSec = PPLAY.t; }
    }
    if(anchorSec == null){
      anchorY = Math.round(sc.clientHeight / 3);
      /* 落在上下留白里时算出的是负值/超界，夹回当天范围（否则缩放会锚到不存在的时刻） */
      anchorSec = Math.max(0, Math.min(86400,
        (sc.scrollTop + anchorY - vtlTop()) / vtlPxh * 3600));
    }
  }
  const np = vtlClampPxh(vtlPxh * factor);
  if(np === vtlPxh) return false;
  vtlPxh = np;
  try{ localStorage.setItem("nvr_vtl_pxh", String(Math.round(vtlPxh))); }catch(e){}
  renderVtl(vtlData, vtlDay, anchorSec, anchorY);
  return true;
}
function jsToday(){
  const t = new Date();
  return t.getFullYear() + "-" + pad(t.getMonth() + 1) + "-" + pad(t.getDate());
}

function renderPlay(autoStart){
  pvLoadDay();   // 换机位/换日期 = 停播并重装分片清单（放最前面，避免旧引用）
  const cam = CAMERAS.find(c=>c.id === state.camId);
  const nameEl = document.getElementById("playCamName");
  if(nameEl) nameEl.textContent = cam ? cam.name : "—";
  renderPlayDates();
  renderPlayCamPick();
  renderVtl(null, curDay());
  /* 进页 / 换机位 / 换日期：定位到默认落点开播（0.9.72 起与点播放同一套规则） */
  if(autoStart) pvAutoEnter(true);
  const day = curDay();
  if(!state.camId || !day) return;   // 空态由时间轴摘要兜住
  API.get("/api/ledger?cam=" + encodeURIComponent(state.camId) + "&date=" + day)
    .then(d=>{
      if(!d || !d.ok) throw new Error((d && d.error) || "台账读取失败");
      renderVtl(d, day);
      const sum = d.summary || {count:0, active_sec:0};
      /* 全天统计写进 #vtlSum：非全屏这行是 display:none（不占高度），全屏才露出。
         有活动才写全（带各类别计数与色片）；一条都没有时留空，交给下面的
         vtlNote 用一层浅提示说明原因——两处都写会重复。 */
      const sumEl = document.getElementById("vtlSum");
      if(sumEl){
        sumEl.innerHTML = sum.count
          ? (sum.count + " 个活动 · 累计 " + durText(sum.active_sec) + clsDayText(d.events))
          : "";
      }
      vtlNote(sum.count ? "" : "暂无活动记录（判定进行中，或这一路的「事件侦测」是关闭）");
      /* 图例：这天一条 AI 类别都没有（AI 没开 / 旧台账）→ 两个 AI 类别降透明度 */
      const lg = document.getElementById("evLegend");
      if(lg) lg.classList.toggle("noai", !clsDayText(d.events));
    }).catch(e=>{
      const sumEl = document.getElementById("vtlSum");
      if(sumEl) sumEl.textContent = "";      // 读失败不能留着上一次的统计冒充这一天的
      vtlNote("台账读取失败");
      const lg = document.getElementById("evLegend");
      if(lg) lg.classList.add("noai");
      toast("toast2", String(e.message || e));
    });
}

/* 时间轴上的「没有内容」浅提示（0.9.89）：传空串 = 隐藏。
   替代原来常驻在时间轴顶上那行摘要里的「暂无活动 / 读取失败」。 */
function vtlNote(txt){
  const el = document.getElementById("vtlNote");
  if(!el) return;
  el.textContent = txt || "";
  el.hidden = !txt;
}

function renderPlayDates(){
  const pick = document.getElementById("playDayPick");
  const name = document.getElementById("playDayName");
  const sel = curDay();
  if(name) name.textContent = sel ? sel.slice(5) : "—";
  if(!pick) return;
  pick.innerHTML = "";
  (state.dayList || []).forEach((d,i)=>{
    const b = document.createElement("div");
    b.className = "dp" + (d === sel ? " on" : "");
    const dt = new Date(d + "T00:00:00");
    b.innerHTML = '<span>' + d.slice(5) + '</span><span class="w">周'
      + "日一二三四五六"[dt.getDay()] + '</span>';
    b.onclick = e=>{ e.stopPropagation();
      if(state.dayIdx !== i){ state.dayIdx = i; state.daySel = d; state.hourBlock = -1; renderPlay(true); }
      playDayShow(false);
    };
    pick.appendChild(b);
  });
}

function playDayShow(force){
  const pick = document.getElementById("playDayPick");
  if(!pick) return;
  const open = (force == null) ? !pick.classList.contains("on") : !!force;
  pick.classList.toggle("on", open);
}

function renderPlayCamPick(){
  const box = document.getElementById("playCamPick");
  if(!box) return;
  if(!CAMERAS.length){
    box.innerHTML = '<div class="cempty">还没有摄像头 · 去设置页添加</div>';
    return;
  }
  box.innerHTML = CAMERAS.map(c=>{
    const pp = camParams[c.id] || {};
    const off = !c.online || !pp.main || pp.enabled === false;
    return '<div class="cp' + (c.id === state.camId ? " on" : "") + '" data-playcam="' + esc(c.id) + '">'
      + '<span class="dt' + (off ? " off" : "") + '"></span>'
      + '<span class="nm">' + esc(c.name) + '</span>'
      + '<span class="ch">' + esc(c.ch || c.id) + '</span></div>';
  }).join("");
  box.querySelectorAll("[data-playcam]").forEach(el=>{
    el.onclick = e=>{ e.stopPropagation(); pickCam(el.dataset.playcam, true); playCamShow(false); };
  });
}

function playCamShow(force){
  const box = document.getElementById("playCamPick");
  const sel = document.getElementById("playCamSel");
  if(!box) return;
  const open = (force == null) ? !box.classList.contains("on") : !!force;
  box.classList.toggle("on", open);
  if(sel) sel.classList.toggle("open", open);
  if(open) renderPlayCamPick();
}

function renderVtl(d, day, anchorSec, anchorY){
  const box = document.getElementById("vtl");
  if(!box) return;
  if(d){ vtlData = d; vtlDay = day; }
  vtlBubbleHide();
  box.innerHTML = "";
  vtlMarks = [];
  vtlWireSvg = document.createElementNS(SVG_NS, "svg");
  vtlWireSvg.setAttribute("class", "vtlwire");
  vtlWireSvg.setAttribute("width", "100%");
  vtlWireSvg.setAttribute("height", "100%");
  box.appendChild(vtlWireSvg);
  vtlSyncPad();   // 上下留白先算：下面的坐标、高度、滚动定位全依赖它
  box.style.height = (vtlTop() + 24 * vtlPxh + vtlPadBot + 24) + "px";
  /* 刻度步进随缩放变精细：默认 120px/h 仍是 1 小时刻度，
     放大到 150px/h 起 30 分 → 15 分 → 5 分；缩太小则 2/3/6 小时一格 */
  const TICKS = [[480,5],[240,10],[150,30],[90,60],[45,120],[22,180],[0,360]];
  let hstep = 180;
  for(const t of TICKS){ if(vtlPxh >= t[0]){ hstep = t[1]; break; } }
  for(let m = 0; m <= 24 * 60; m += hstep){
    const el = document.createElement("div");
    el.className = "vhour";
    el.style.top = vtlY(m * 60) + "px";
    el.textContent = pad(Math.floor(m / 60)) + ":" + pad(m % 60);
    box.appendChild(el);
  }
  const track = document.createElement("div");
  track.className = "vtrack";
  track.style.top = vtlY(0) + "px";
  track.style.height = (24 * vtlPxh) + "px";
  box.appendChild(track);
  const av = (d && d.avail) || {};
  /* 服务端时基（0.9.72）：录像分片按 NAS 时钟命名，所以「现在」也用 NAS 给的，
     别用浏览器 Date —— 手机与 NAS 差几分钟时，蓝块会画到红线之外，
     看着就是「真实时间还没到，已经显示录了视频」。d.now 只在今天有值。 */
  const srvNow = (d && typeof d.now === "number") ? d.now : null;
  /* 活动清单先算好：横线标注与活动时段块共用。
     pvClsMatch 过一遍类别筛选：不命中的时段块/横线整体变淡（0.9.60），
     但历史事件（无 cls）永远不淡——升级不该把旧活动藏掉 */
  const evs = ((d && d.events) || []).slice().sort((a,b)=>a.s-b.s);
  const evHit = evs.map(pvClsMatch);
  (av.full || []).forEach(iv=>{
    /* 兜底夹紧：服务端已经按 now 夹过（ledger.avail_spans），这里再夹一次，
       防止老版本服务端 / 缓存响应把蓝块画到未来 */
    let e2 = iv[1];
    if(srvNow != null) e2 = Math.min(e2, srvNow);
    if(e2 <= iv[0]) return;
    const el = document.createElement("div");
    el.className = "vavail";
    el.style.top = vtlY(iv[0]) + "px";
    el.style.height = Math.max(4, vtlY(e2) - vtlY(iv[0])) + "px";
    box.appendChild(el);
  });
  (av.archive || []).forEach(iv=>{
    const el = document.createElement("div");
    el.className = "vavail arch";
    el.style.top = vtlY(iv[0]) + "px";
    el.style.height = Math.max(4, vtlY(iv[1]) - vtlY(iv[0])) + "px";
    box.appendChild(el);
  });
  /* 活动时段块（0.9.69）：高度 = 这段持续多久。
     最小 4px 地板——默认 120px/h 下 2 秒只有 0.07px，不给地板等于没画。
     尾巴从事件起点向下长，头顶正好压着同一时刻的那根 3px 亮线：
     线回答「什么时候开始的」，尾巴回答「待了多久」。

     0.9.75：时段块也按「哪里有画面」切开。以前一律画满整段，于是断录期
     （人还在院子里、但录制卡住/重连）也照样一条橙块垂下来 —— 与蓝块不一致，
     用户点进去发现没画面，「有活动 = 有画面」的暗示是错的。现在与蓝块共用
     同一份 av.full：中间断开、整段都在断录里就只留虚线框。

     0.9.76：宽度改回整条轨道（左 46 / 宽 60），与蓝块严格同列同宽。
     0.9.69 把块收进最左 19px 后，有活动的地方蓝块被盖掉左边一截、只剩右边
     41px，没活动的地方蓝块又从最左边起 —— 同一根竖线在时间轴上有两种左边缘，
     读起来像两条并排的轨道。用户真机截图指出「蓝色和黄色应该对齐」。
     颜色换成一格主色（evDom），类别细分交给起点三格线。 */
  evs.forEach((ev, k)=>{
    const parts = vtlClipByAvail(ev.s, ev.e, av.full);
    if(!parts.length){
      /* 整个活动都落在断录里：不画类别色条，只留一个虚线框。
         不直接不画 —— 台账里是真有过活动，凭空消失比画错更让人怀疑数据丢了。 */
      const el = document.createElement("div");
      el.className = "vevspan gap" + (evHit[k] ? "" : " dim");
      el.style.top = vtlY(ev.s) + "px";
      el.style.height = Math.max(4, vtlY(ev.e) - vtlY(ev.s)) + "px";
      box.appendChild(el);
      return;
    }
    parts.forEach(pt=>{
      const el = document.createElement("div");
      el.className = "vevspan" + (evHit[k] ? "" : " dim");
      el.style.top = vtlY(pt[0]) + "px";
      const h = Math.max(4, vtlY(pt[1]) - vtlY(pt[0]));
      /* 同一条 60px 轨道（容器对齐蓝块），颜色按类别落在三格窄道上（0.9.77）。
         不再用单一主色铺满整宽：整段只有一类时会把蓝块完全盖掉。 */
      el.innerHTML = vtlLanes(evKind(ev), h);
      box.appendChild(el);
    });
  });
  // 事件层：类别色短横线标注（0.9.62 分色，0.9.67 起三车道）。
  // 挤在一起（间距 < 20px）的合并为一根线（组），点线弹标注气囊，点空白处直接跳播。
  // 组的线段取组内各类别并集 —— 一簇里既有人又有车，左中右三格都点亮。
  const GAP = 20;   // 标记命中区 18px，留 2px 呼吸
  let i = 0;
  while(i < evs.length){
    let j = i;
    while(j + 1 < evs.length && vtlY(evs[j + 1].s) - vtlY(evs[i].s) < GAP) j++;
    const ev0 = evs[i], cnt = j - i + 1, lo = i, hi = j;   // 闭包捕获：i/j 循环里会变，必须先存局部
    /* 组内只要有一个命中就不淡（整组共用一个点击目标，分开算会让用户点不到） */
    let grpHit = false;
    const uk = { motion:false, person:false, vehicle:false };
    for(let k = lo; k <= hi; k++){
      if(evHit[k]) grpHit = true;
      const kd = evKind(evs[k]);
      for(const key in uk) if(kd[key]) uk[key] = true;
    }
    const el = document.createElement("div");
    el.className = "vmark" + (cnt > 1 ? " grp" : "") + (grpHit ? "" : " dim");
    el.style.top = vtlY(ev0.s) + "px";
    el.innerHTML = '<div class="lanes">' + vtlLanes(uk) + "</div>";
    /* 0.9.91：簇内再按类别拆「分组」，气泡按分组分行、连线按分组各画一条。
       单事件不建分组，保持旧气泡（详细时长 / 密度 / AI 计数 + 一条线到气泡腰）。 */
    const list = cnt > 1 ? evs.slice(lo, hi + 1) : null;
    const groups = list ? vtlClusterGroups(list) : null;
    el.onclick = e=>{ e.stopPropagation();
      vtlBubbleShow(e.currentTarget, ev0, list, groups); };
    box.appendChild(el);
    /* 登记：跟随逻辑要按「红线落在哪个事件」找回对应的可点标注 */
    vtlMarks.push({ el, ev0, list, groups });
    i = j + 1;
  }
  /* 时刻线不再画进内容层：改为固定在滚动视口垂直居中的 .vnowline（Aqara 式），
     播放时内容向上滚动对准它。这里只决定非播放态的默认落点。 */
  let nowSec;
  if(PPLAY.on && PPLAY.day === day && PPLAY.camId === state.camId){
    nowSec = PPLAY.t;
  }else if(day === jsToday()){
    if(srvNow != null){
      /* 红线与蓝块必须共用同一个「现在」：一个用 NAS 时钟、一个用手机时钟，
         差几分钟就会出现线落在蓝块外面，看起来像"未来也有录像" */
      nowSec = srvNow;
    }else{
      const t = new Date();
      nowSec = t.getHours() * 3600 + t.getMinutes() * 60 + t.getSeconds();
    }
  }else{
    nowSec = evs.length ? evs[evs.length - 1].e : 12 * 3600;
  }
  nowSec = Math.max(0, Math.min(86400, nowSec));
  const pct = document.getElementById("vtlPct");
  if(pct) pct.textContent = "×" + (vtlPxh / VTL_PXH_DEF).toFixed(1);
  const sc = document.getElementById("vtlScroll");
  if(sc){
    if(anchorSec != null){
      /* 缩放/捏合锚点：锚定时刻停在屏幕原位置（默认 = 时刻线所在的 2/3 高度处） */
      const ay = (anchorY != null) ? anchorY : Math.round(sc.clientHeight / 3);
      sc.scrollTop = Math.max(0, vtlY(anchorSec) - ay);
    }else if(PPLAY.on && PPLAY.day === day && PPLAY.camId === state.camId){
      /* 播放中：让固定时刻线对准播放位置 */
      sc.scrollTop = Math.max(0, vtlY(PPLAY.t) - Math.round(sc.clientHeight / 3));
    }else if(vtlDay !== day){
      /* 换天/首次：把默认落点（今天=现在钟点，历史=最后活动/正午）摆到 2/3 高度处 */
      sc.scrollTop = Math.max(0, vtlY(nowSec) - Math.round(sc.clientHeight / 3));
    }
    /* 其余情况（同天重绘，如切筛选/弹层）：保持滚动位置不动 */
  }
  syncVnowline();
}

/* 固定时刻线：钉在滚动视口自下而上 2/3 高度处，线短只盖轨道区。
   播放中 = 显示播放时刻（内容自动滚动对准）；空闲 = 显示线下压着的时刻（随滚动变） */
function syncVnowline(){
  const sc = document.getElementById("vtlScroll");
  const ln = document.getElementById("vnowline");
  if(!sc || !ln) return;
  const lineY = Math.round(sc.clientHeight / 3);
  ln.style.top = (sc.offsetTop + lineY) + "px";
  /* 视口高度变了（进全屏 / 转屏 / 页面刚显示 / 窗口缩放）：上下留白必须跟着重算，
     否则两端还是滚不到线。锚住线下的时刻重绘，视线不跳。 */
  if(vtlData && sc.clientHeight > 60 && Math.abs(sc.clientHeight - vtlScH) > 2){
    const t = Math.max(0, Math.min(86400,
      (sc.scrollTop + lineY - vtlTop()) / vtlPxh * 3600));
    renderVtl(vtlData, vtlDay, t, lineY);
    return;   // renderVtl 末尾会再调一次本函数（此时高度已同步，不会再进来）
  }
  const em = document.getElementById("vnowTime");
  if(!em) return;
  if(PPLAY.on){ em.textContent = hms(PPLAY.t); return; }
  const t = Math.max(0, Math.min(86400,
    (sc.scrollTop + sc.clientHeight / 3 - vtlTop()) / vtlPxh * 3600));
  em.textContent = hms(t);
}

/* 运动佐证（0.9.69）：事件起止是不是帧差确认的。
   0.9.89 起不再为 mv=1（帧差确认的起止）追加任何文案——这是绝大多数事件的常态，
   多一句只是噪声，还把气囊顶高一行。只保留真正需要解释的反常情况：
   mv=0 → 有帧差管线，但这段时间画面没动过（AI 可能误检，如实提示）；
   mv=1 / -1 / 缺 → 不解释。 */
function mvText(ev){
  const mv = ev && ev.mv;
  if(mv === 0) return '<div class="bi">这段时间画面没有变化 · AI 单独判定</div>';
  return "";
}
/* 标注气囊：点横线弹出（再点同一根 = 关）；列表行/播放钮直接跳播 */
let vtlBubbleEl = null, vtlBubbleRef = null;
/* 当前气泡如果是密集簇：groups = 按类别的分组表，vtlRowIdx = 正在播的那一行（-1 无）。
   跟随只改这两样，不重建气泡（0.9.91）。 */
let vtlBubbleGroups = null, vtlRowIdx = -1;
/* 跟随换气泡时的「先关旧再开新」窗口（0.9.90）：置位期间不摘/不加 bubbleopen，
   否则缩放胶囊会跟着闪一下。声明放这么前是因为 vtlBubbleHide 也要读它。 */
let vtlSwapKeep = false, vtlFollowAt = 0;
function vtlBubbleHide(){
  if(vtlBubbleEl){ vtlBubbleEl.remove(); vtlBubbleEl = null; }
  vtlBubbleRef = null;
  vtlBubbleGroups = null; vtlRowIdx = -1;
  if(vtlWireSvg) vtlWireSvg.innerHTML = "";   // 连线跟着气泡一起消失（0.9.90）
  const card = document.getElementById("vtlCard");
  /* 跟随切换时是「旧的先关、新的立刻开」：类不摘掉，缩放胶囊就不会跟着闪一下 */
  if(card && !vtlSwapKeep) card.classList.remove("bubbleopen");
}
function densText(d){ return d >= 0.3 ? " · 高密度" : d >= 0.1 ? " · 中密度" : " · 低密度"; }
/* 事件类别摘要（0.9.60）：台账 events_of 聚合出的各类别计数。
   纯帧差时代的事件没有 cls，返回空串——不显示"0 人 0 车"这种误导信息 */
/* 气泡里的类别摘要：加粗 + 类别色 + 小色片，与时间轴横线段同一套配色。
   0.9.63 起两轮车并入车辆：车 = 车辆 + 两轮车计数。
   旧台账没有 cls → 返回空串，不出现「0 人 0 车」这种误导信息 */
function clsText(ev){
  const c = ev && ev.cls;
  if(!c) return "";
  const parts = [];
  if((c.person|0) > 0)
    parts.push('<b class="clsw t-person"><i class="cd c-person"></i>人 ×' + (c.person|0) + "</b>");
  const v = (c.vehicle|0) + (c.two_wheeler|0);
  if(v > 0)
    parts.push('<b class="clsw t-veh"><i class="cd c-veh"></i>车 ×' + v + "</b>");
  return parts.length ? ' <span class="csep">·</span> ' + parts.join(' <span class="csep">·</span> ') : "";
}
/* 顶部摘要里的全天各类别计数（0.9.62，0.9.63 起两轮车并入车辆）：
   与时间轴同色，扫一眼就知道这天人/车各多少。
   只有真检出过才拼这段 —— AI 没开或旧台账时摘要保持原样，不摆零 */
function clsDayText(evs){
  let p = 0, v = 0;
  (evs || []).forEach(ev=>{
    const c = ev && ev.cls;
    if(!c) return;
    p += (c.person|0); v += (c.vehicle|0) + (c.two_wheeler|0);
  });
  if(!p && !v) return "";
  const seg = (n, lab, cls)=> n
    ? '<b class="clsw ' + cls + '"><i class="cd ' + cls.replace("t-", "c-") + '"></i>' + lab + " " + n + "</b>"
    : "";
  return ' <span class="csep">·</span> ' + [seg(p, "人", "t-person"), seg(v, "车", "t-veh")]
    .filter(Boolean).join(' <span class="csep">·</span> ');
}

function vtlBubbleShow(anchor, ev0, list, groups){
  const boxv = document.getElementById("vtl");
  if(!boxv || !anchor) return;
  if(vtlBubbleRef === anchor){ vtlBubbleHide(); return; }
  vtlBubbleHide();
  const el = document.createElement("div");
  /* 描边 = 事件类型色（0.9.90）：与连到它的曲线同色，一眼对上「这根线说的是这件事」。
     密集簇是多类型混在一起，不套单色描边（否则等于宣称「这簇只有这一类」）。 */
  const clu = !!(groups && groups.length);
  el.className = "vbubble" + (clu ? "" : " k-" + evLaneKey(ev0));
  if(clu){
    /* 0.9.91 密集簇：按类别分行（行序 = 车道序），每行一条连线。
       同一类别的多起合并进一行数字 —— 气泡高度与事件数解耦，30 个事件也还是三行。
       不做逐条展开：3 秒内挤 8 个事件时逐条本来也选不准，展开还会改行高、
       把已经画好的连线落点带歪。 */
    let html = '<div class="bt">' + hms(list[0].s) + " 附近 · " + list.length + " 个事件</div>";
    groups.forEach(g=>{
      let sum = 0;
      g.evs.forEach(e=> { sum += Math.max(0, (e.e || e.s) - e.s); });
      html += '<div class="row" data-t="' + Math.max(0, g.ev0.s - PV_ROLL) + '">'
        + '<i class="bul ' + g.key + '"></i>'
        + '<b class="lab">' + laneLabel(g.key) + '</b>'
        + '<span class="cnt">' + g.evs.length + " 个 · 共 " + durText(sum) + '</span>'
        + '<span class="d">播放 ▸</span></div>';
    });
    el.innerHTML = html;
  }else if(list){
    let html = '<div class="bt">' + hms(list[0].s) + " 附近 · " + list.length + " 个事件</div>";
    list.forEach(ev=>{
      html += '<div class="row" data-t="' + Math.max(0, ev.s - PV_ROLL) + '"><b>' + hms(ev.s) + '</b>'
        + '<span>' + durText(ev.e - ev.s) + densText(ev.d) + clsText(ev) + '</span>'
        + '<span class="d">播放 ▸</span></div>';
    });
    el.innerHTML = html;
  }else{
    el.innerHTML = '<div class="bt">' + hms(ev0.s) + '</div>'
      + '<div class="bi">' + durText(ev0.e - ev0.s) + densText(ev0.d) + clsText(ev0) + '</div>'
      + mvText(ev0)
      + '<button class="go" data-t="' + Math.max(0, ev0.s - PV_ROLL) + '">▶ 从此处播放</button>';
  }
  el.style.top = (parseFloat(anchor.style.top) + 10) + "px";
  boxv.appendChild(el);
  vtlBubbleEl = el; vtlBubbleRef = anchor;
  /* 跟随高亮的分组表：红线在簇内移动时只换「亮哪一行」，不重建气泡 */
  vtlBubbleGroups = clu ? groups : null;
  vtlRowIdx = -1;
  /* 气囊会横穿到右侧缩放胶囊上（0.9.89）：这期间把胶囊淡出 */
  const card = document.getElementById("vtlCard");
  if(card && !vtlSwapKeep) card.classList.add("bubbleopen");
  el.querySelectorAll("[data-t]").forEach(x=> x.onclick = e=>{
    e.stopPropagation();
    if(pvPlayAt(+x.dataset.t)) vtlBubbleHide();
  });
  /* 气囊尾部超出可视区则上提（元素挂在 #vtl 内，坐标含 scrollTop 换算到可视带） */
  const sc = document.getElementById("vtlScroll");
  if(sc){
    const top = parseFloat(el.style.top), bh = el.offsetHeight;
    if(top + bh > sc.scrollTop + sc.clientHeight)
      el.style.top = Math.max(sc.scrollTop, sc.scrollTop + sc.clientHeight - bh - 8) + "px";
  }
  /* 位置定稿后再画连线：上提会改变气泡位置，线必须照最终位置连。
     簇内多条线把线宽降一档，三条并排时不至于糊成一片。 */
  const bubTop = parseFloat(el.style.top) || 0;
  if(vtlWireSvg && vtlWireSvg.classList) vtlWireSvg.classList.toggle("multi", clu);
  vtlWireDraw(anchor, ev0, groups, bubTop);
  /* 暂停态点开也要立即亮出「现在红线在哪一行」 */
  if(clu) vtlRowHi(PPLAY.on ? PPLAY.t : ev0.s);
}

/* 事件该算在哪一道：取最右命中的那一格（车辆 > 人员 > 移动侦测），
   线越短越不碍眼；三格 x 与 CSS `.vtl .vmark .lanes i` 的 left 严格对应 */
function evLaneKey(ev){
  const k = evKind(ev);
  if(k.vehicle) return "vehicle";
  if(k.person) return "person";
  return "motion";
}
const VTL_LANE_X = { motion: 65, person: 85, vehicle: 105 };   // 46 + 格位(0/20/40) + 格宽 19
const VTL_BUBBLE_X = 132;    // 与 CSS .vbubble 的 left 必须一致（有测试对账）
/* 行序固定按车道序（移动 → 人员 → 车辆），不按时间排。
   这是「曲线永不交叉」的唯一保证：左边车道配上面行，右边车道配下面行，
   同一簇里的几条线各自单调下降，互相不会穿插。 */
const VTL_LANE_ORDER = ["motion", "person", "vehicle"];
function laneLabel(k){
  return k === "vehicle" ? "车辆" : k === "person" ? "人员" : "移动侦测";
}
/* 把一簇事件按类别拆成分组（0.9.91）。evLaneKey 每个事件只归一道，
   所以这里是不重不漏的划分；空类别不占行，最多三行。 */
function vtlClusterGroups(list){
  const out = [];
  VTL_LANE_ORDER.forEach(k=>{
    const evs = (list || []).filter(ev=> evLaneKey(ev) === k);
    if(evs.length) out.push({ key: k, evs, ev0: evs[0] });
  });
  return out;
}
/* 气泡分行的坐标：只靠注释里的固定值算，不量 DOM。
   padding-top 10 + 标题行高 18 + 行 margin 2 + 行高 28 / 2 = 44，行距 = 28 + 2 = 30。
   CSS 里 .bt 的 line-height 与 .row 的 height 必须与这两个数一致（有测试对账）。 */
const VTL_ROW_Y0 = 44, VTL_ROW_PITCH = 30;

/* 画「事件线尾 → 气泡」的柔和曲线。
   单事件：一条，起点 = 该事件车道格右端，终点 = 气泡左腰（0.9.90 的行为，不变）。
   密集簇：每种类别一条（最多 3 条），终点 = 气泡里该类别的行中线（0.9.91）。
   控制点各取水平差的 45%，保证 c1 < c2，两端都是水平切线，S 形不会反折。
   气泡被上提夹紧时垂直差变大，曲线自然拉长 —— 这正是选曲线而非直线的理由。 */
function vtlWireDraw(anchor, ev, groups, bubTop){
  if(!vtlWireSvg) return;
  vtlWireSvg.innerHTML = "";
  if(!anchor || !ev || !vtlBubbleEl) return;
  const r = v => Math.round(v * 10) / 10;
  const top = parseFloat(anchor.style.top) || 0;
  const y1base = bubTop == null ? (parseFloat(vtlBubbleEl.style.top) || top) : bubTop;
  const draw = (kind, y1)=>{
    const x0 = VTL_LANE_X[kind] || VTL_LANE_X.motion;
    const x1 = VTL_BUBBLE_X;
    const dx = x1 - x0;
    const c1 = x0 + dx * 0.45, c2 = x1 - dx * 0.45;
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M" + r(x0) + " " + r(top)
      + " C" + r(c1) + " " + r(top) + " " + r(c2) + " " + r(y1) + " " + r(x1) + " " + r(y1));
    path.setAttribute("class", "w-" + kind);
    /* 起点小圆点：气泡被上提、垂直差很大时，一眼看出线是从哪一刻出发的 */
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", r(x0));
    dot.setAttribute("cy", r(top));
    dot.setAttribute("r", "2.6");
    dot.setAttribute("class", "d-" + kind);
    vtlWireSvg.appendChild(path);
    vtlWireSvg.appendChild(dot);
  };
  if(groups && groups.length){
    /* 起点与终点都随分组下标单调排列，所以多条线只会并流分开、不会交叉 */
    groups.forEach((g, i)=> draw(g.key, y1base + VTL_ROW_Y0 + i * VTL_ROW_PITCH));
    return;
  }
  const bh = vtlBubbleEl.offsetHeight || 90;
  draw(evLaneKey(ev), y1base + bh / 2);
}

/* 播放扫过自动跟随（0.9.90）：红线进入某个事件的起止区间，就把气泡与连线一起切过去。
   三条自我约束，缺一条都会变得很烦人：
   ① 只在用户已经点开过气泡时跟随 —— 不主动弹（需求：只有点击才开始连线）；
   ② 只在真正播放中跟随 —— 暂停后手动拖时间轴是「我要看这条」，不许被抢；
   ③ 300ms 迟滞 + 红线落在事件之外时保持原样 —— 相邻事件边界不会来回抖。 */
function vtlFollow(tSec){
  if(!vtlBubbleEl || !vtlMarks.length) return;
  if(!PPLAY.on || !pvCur || pvCur.paused || pvCur.ended) return;
  if(Date.now() - vtlHold < 2000) return;      // 用户刚拖过，2 秒内不抢
  let hit = null;
  for(const m of vtlMarks){
    const list = m.list || [m.ev0];
    if(list.some(ev=> ev.s <= tSec && tSec <= ev.e)){ hit = m; break; }
  }
  if(!hit || vtlBubbleRef === hit.el) return;
  const now = Date.now();
  if(now - vtlFollowAt < 300) return;
  vtlFollowAt = now;
  vtlSwapKeep = true;                          // 换气泡期间别让缩放胶囊闪
  try{ vtlBubbleShow(hit.el, hit.ev0, hit.list, hit.groups); }
  finally{ vtlSwapKeep = false; }
}

/* 簇内行高亮（0.9.91）：红线在簇内移动时，不换气泡，只把「现在播到哪一类」那一行点亮。
   密集簇里按事件换气泡会 1~2 秒闪一次；按簇保持 + 行高亮，切换次数从事件数降到簇数。
   行命中口径与连线/筛选一致：该行的任一事件起止区间含当前时刻。 */
/* 红线此刻落在哪一行（-1 = 不在一簇内）。抽成纯函数是为了能直接测：
   命中口径 = 该行任一事件的起止区间含当前时刻，与连线、筛选同一口径。 */
function vtlRowIdxAt(groups, tSec){
  if(!groups || !groups.length) return -1;
  for(let i = 0; i < groups.length; i++){
    if(groups[i].evs.some(ev=> ev.s <= tSec && tSec <= ev.e)) return i;
  }
  return -1;
}
function vtlRowHi(tSec){
  if(!vtlBubbleEl || !vtlBubbleGroups) return;
  const idx = vtlRowIdxAt(vtlBubbleGroups, tSec);
  if(idx === vtlRowIdx) return;
  vtlRowIdx = idx;
  const rows = vtlBubbleEl.querySelectorAll ? vtlBubbleEl.querySelectorAll(".row") : [];
  if(!rows || !rows.forEach) return;
  rows.forEach((r, i)=> { if(r && r.classList) r.classList.toggle("on", i === idx); });
}

/* 0.9.67 起取消「播放落点胶囊」：以前手指点到时间轴某处（正好落在活动上）
   会弹出一颗软底小胶囊把整根标记圈起来，看着像凭空冒出来的浮层，
   还把类别色压淡。位置信息交给固定的红色时刻线就够了，这里不再高亮 */

/* ================= 回放页常驻画面区（P1）=================
   与文件页的弹出播放器相互独立：这里只服务 Aqara 回放页的
   「按时刻跳播 + 连续播放」。分片地址复用 realSegs("full") 的
   /api/record/file；跳播落点向前垫 PV_ROLL 秒（与服务端 analysis.pre_buf
   默认 3 秒一致），把关键帧对齐误差整个吸收掉。 */
const pvA = document.getElementById("playVidA");
const pvB = document.getElementById("playVidB");
const PV_ROLL = 3.0;
/* filter：full=全时连播；act=活动连播（按预设跳过静止段）。
   speed：全时/平时的倍速（浏览器上限 16×）；silent 仍是活动模式下静止段的预设方案。 */
const PPLAY = { on:false, filter:"full", silent:"skip",
                day:null, camId:null, segs:[], segIdx:-1, t:0, ff:null, speed:1,
                endHit:false, inAct:false, seekTo:null, seekAt:0,
                /* 活动类别筛选（0.9.60，0.9.63 车辆含两轮车，0.9.67 三类并列）：
                   未勾的类别不参与活动导航、时间轴上同步变淡。
                   移动侦测 = 没有任何 AI 类别的纯帧差事件（与 evKind 同一口径）。
                   全勾 = 不过滤，与旧行为完全一致 */
                cls:{ motion:true, person:true, vehicle:true } };
/* 归档面板状态：sum=候选摘要文案；arm=预览已出待确认；out=最近一次结果 */
const ARCH = { sum:"", out:"", arm:null, at:0, loading:false };
try{
  const f = localStorage.getItem("nvr_pvfilter"); if(f === "act") PPLAY.filter = "act";
  const sp = +localStorage.getItem("nvr_pvspeed");
  if([1,2,4,8,16].indexOf(sp) >= 0) PPLAY.speed = sp;
  const s = localStorage.getItem("nvr_silent");
  if(s === "16" || s === "32") PPLAY.silent = s;
  /* 活动类别筛选回读（0.9.60，0.9.63 迁移车辆，0.9.67 加移动侦测）：
     缺键按「勾选」算，保证旧偏好/脏数据不缩小结果集 */
  const cs = localStorage.getItem("nvr_pvcls");
  if(cs){
    try{
      const o = JSON.parse(cs);
      if(o && typeof o === "object"){
        /* 旧偏好里没有 motion 键（0.9.67 之前只有人员/车辆）→ 勾上，不藏事件 */
        PPLAY.cls.motion  = o.motion !== false;
        PPLAY.cls.person  = o.person !== false;
        /* 0.9.63 起两轮车并入车辆：旧偏好里 two_wheeler 的勾选取并集，
           避免升级后原本能看的两轮车事件被筛选藏掉 */
        PPLAY.cls.vehicle = (o.vehicle !== false) || (o.two_wheeler !== false);
      }
    }catch(e){}
  }
}catch(e){}
let pvCur = pvA, pvNxt = pvB, pvPreloadSeg = -1;
/* 事件页点过来的待播落点（0.9.121）：{t, at}。
   为什么不在事件页直接操作播放器：定位要等清单/分片列表装好（首次进页可能还在
   拉 /api/recordings），而"装好之后再定位"的唯一入口就是 pvAutoEnter。 */
let pvSeekTo = null;

function pvSegAt(t){
  const a = PPLAY.segs;
  let lo = 0, hi = a.length - 1;
  while(lo <= hi){
    const m = (lo + hi) >> 1, sg = a[m];
    if(t < sg.s) hi = m - 1;
    else if(t >= sg.e) lo = m + 1;
    else return { seg: sg, idx: m };
  }
  return null;
}

function pvStop(){
  [pvA, pvB].forEach(v=>{ try{ v.pause(); }catch(e){} });
  PPLAY.on = false; PPLAY.ff = null;
  pvRate();
  pvSyncCtl();
  pvSyncVtl();
}

/* 回放页专用播放：进页面自动播可能被浏览器策略拦（从返回键/历史恢复进来不算用户手势）。
   被拦时把 PPLAY.on 退回 false —— 大播放钮必须留在画面上，
   否则状态显示「已定位在播」、按钮却已隐藏，用户找不到重新播放的入口。
   不直接用 safePlay：那个是直播/文件页共用的静默版本，不能改它的语义。 */
function pvSafePlay(){
  const back = ()=>{ PPLAY.on = false; pvSyncCtl(); };
  try{
    const p = pvCur.play();
    if(p && p.catch) p.catch(back);
  }catch(e){ back(); }
}

/* 跳播：t = 当天钟点秒。目标无画面时提示并拒绝（宁可不播也不黑屏） */
function pvPlayAt(t, opts){
  opts = opts || {};
  const hit = pvSegAt(Math.max(0, Math.min(86399, t)));
  if(!hit){
    if(!opts.quiet) toast("toast2", "该时刻无录像");
    return false;
  }
  const seg = hit.seg;
  PPLAY.on = true; PPLAY.segIdx = hit.idx;
  PPLAY.t = Math.max(seg.s, t);
  PPLAY.day = curDay(); PPLAY.camId = state.camId; PPLAY.ff = null;
  PPLAY.endHit = false;   // 重新定位 = 离开尽头状态，重试资格恢复（收播后点播放续看也走这里）
  /* 落点是否在活动内：决定「过了最后一个活动」时该收播还是照常播（手动跳播不算播完） */
  PPLAY.inAct = PPLAY.filter === "act" && pvInEvent(PPLAY.t);
  PPLAY.seekTo = PPLAY.t;   // 落点守卫：timeupdate 里等 currentTime 真的到位再认位置
  PPLAY.seekAt = Date.now();
  /* 下一段已预加载好 = 热切换；否则冷启动整个 src（避免清空再加载的黑帧） */
  if(pvPreloadSeg === hit.idx && pvNxt.getAttribute("src") === seg.file){
    const old = pvCur;
    pvCur = pvNxt; pvNxt = old;
    pvPreloadSeg = -1;
    pvNxt.classList.remove("on");
  }else if(pvCur.getAttribute("src") !== seg.file){
    try{ pvCur.pause(); }catch(e){}
    pvCur.src = seg.file;
    try{ pvCur.load(); }catch(e){}
  }
  pvCur.classList.add("on");
  try{ pvCur.currentTime = Math.max(0, PPLAY.t - seg.s); }catch(e){}
  pvRate();
  pvSafePlay();
  pvSyncOsd(); pvSyncCtl(); pvPreloadNext(); pvSyncVtl();
  return true;
}

function pvPreloadNext(){
  const nx = PPLAY.segs[PPLAY.segIdx + 1];
  if(!nx){ pvPreloadSeg = -1; return; }
  const v = pvNxt;
  if(pvPreloadSeg === PPLAY.segIdx + 1 && v.getAttribute("src") === nx.file) return;
  pvPreloadSeg = PPLAY.segIdx + 1;
  try{ if(v.getAttribute("src") !== nx.file){ v.src = nx.file; v.load(); } }catch(e){}
}

/* ---------- 事件类别与配色（0.9.62，0.9.63 起车辆含两轮车）----------
   时间轴「一眼分型」的公共口径：
     橙 = 移动侦测（帧差，台账无 cls） · 蓝 = 人员 · 青绿 = 车辆（含两轮车）
   - 时段块（.vevspan）与横线同一套三格：左 1/3 移动侦测、中 1/3 人员、
     右 1/3 车辆，容器宽度与蓝块一致（0.9.77：0.9.76 的单主色铺满整宽会把
     蓝块整条盖掉，车库整条全绿就是这个原因）；
   - 组（多个事件挤在一起合并成一根线）取组内各类别的并集。 */
const CLS_COLOR = { motion:"motion", person:"person", vehicle:"veh" };
/* 某事件出现了哪些类别。
   - 没有 cls（旧台账 / 纯帧差）→ 只有移动侦测；
   - cls 存在但全 0（极端情况）→ 回落移动侦测，别渲染成一条没颜色的线；
   - mv===1（0.9.69）→ 移动侦测格也点亮：帧差确认了这次进出的边界，
     移动侦测从此不只属于「AI 没看懂」的时段，而是人员/车辆的辅助判断条件。
     三格是宽度方向互不重叠，同时点亮不同格不冲突。
   0.9.63 起两轮车并入车辆：台账里的 two_wheeler 计数算进 vehicle */
function evKind(ev){
  const c = ev && ev.cls;
  if(!c) return { motion:true, person:false, vehicle:false };
  const k = { motion:false,
              person:(c.person|0) > 0,
              vehicle:((c.vehicle|0) + (c.two_wheeler|0)) > 0 };
  /* 移动侦测 = **没有任何 AI 类别**的纯帧差段，与 pvClsMatch 的 hasM 同一口径。
     0.9.77 之前这里写的是 `motion:(ev.mv === 1)` —— mv 是「这段吸收了移动
     簇」的标记，几乎每个 AI 段都带它，于是橙格几乎常亮、三类互不重叠的语义
     就废了；叠在时段块上还会让块白占掉 2/3 宽（车库车辆段实测亮橙+绿两格）。 */
  if(!k.person && !k.vehicle) k.motion = true;
  return k;
}
/* 三格车道 HTML：位置即类别（左移动 / 中人员 / 右车辆）。
   h 有值时给每格上高度（活动时段块用，4px 地板在调用方算）；
   不给 h 就是起点那根 3px 线（高度由 .vmark .lanes i 的 CSS 定）。 */
function vtlLanes(kind, h){
  const st = (h != null)
    ? ' style="height:' + (Math.round(h * 10) / 10) + 'px"' : "";
  let out = "";
  if(kind.motion)  out += '<i class="c-motion mot"' + st + "></i>";
  if(kind.person)  out += '<i class="c-person per"' + st + "></i>";
  if(kind.vehicle) out += '<i class="c-veh veh"' + st + "></i>";
  return out;
}
/* 把 [s,e] 按「有画面的区间」切成若干段（0.9.75，与服务端 ledger.split_by_spans 同口径）。
   活动块用它、将来别的叠加层也能用 —— 时间轴上「哪里有画面」只有一个事实来源：
   服务端 avail.full（现在由分片 mtime 算真实结束时刻，断录真的留洞）。
   传空/缺数据时原样返回：老服务端不带 avail 时不能把所有活动都裁没。 */
function vtlClipByAvail(s, e, spans){
  if(!spans || !spans.length) return [[s, e]];
  const out = [];
  for(let k = 0; k < spans.length; k++){
    const lo = Math.max(s, spans[k][0]), hi = Math.min(e, spans[k][1]);
    if(hi - lo <= 0.01) continue;
    const last = out[out.length - 1];
    if(last && lo <= last[1] + 0.01) last[1] = Math.max(last[1], hi);
    else out.push([lo, hi]);
  }
  return out;
}
/* 起点三格线段的 HTML：位置固定 左 0 / 中 20 / 右 40，各 19px，位置本身就是类别语义。
   横线（.vmark）与活动时段块（.vevspan）共用同一个（见上），区别只在有没有高度。 */

/* 活动类别筛选（0.9.60，0.9.63 车辆含两轮车，0.9.67 加移动侦测）：
   某事件是否落在当前勾选的类别里。cls 是台账聚合的各类别计数（events_of 输出）。
   「移动侦测」= 没有任何 AI 类别的纯帧差事件（与 evKind 完全同一口径）——
   被 AI 认出人/车的那一段属于人员/车辆，勾「移动侦测」不再重复收到它，
   这样三类筛选互不重叠，用户才能真正筛到「AI 没看懂、但画面上有动静」的部分。
   旧台账事件没有 cls（纯帧差时代）→ 同样按移动侦测算，默认勾选，升级不藏活动。
   vehicle 口径含 two_wheeler：勾「车辆」时两轮车的计数也算命中。 */
function pvClsMatch(ev){
  const c = ev && ev.cls;
  const hasP = !!(c && (c.person|0) > 0);
  const hasV = !!(c && ((c.vehicle|0) + (c.two_wheeler|0)) > 0);
  const hasM = !hasP && !hasV;      /* 没有 AI 类别 = 纯移动侦测 */
  const cl = PPLAY.cls || {};
  if(cl.motion  && hasM) return true;
  if(cl.person  && hasP) return true;
  if(cl.vehicle && hasV) return true;
  return false;
}
/* 当前筛选下的活动清单（已按开始时间排序） */
function pvEvents(){
  const evs = (vtlData && vtlData.events) || [];
  return evs.filter(pvClsMatch);
}

/* ---------- 活动模式「我在哪 / 下一个是谁」的唯一口径（0.9.74）----------
   0.9.73 及以前，这两件事由三处各写一个 for 循环，判定条件还不一样，真机踩出两类误判：

   ① **只按「起点」找下一目标**：pvNextTarget 找「起点晚于当前时刻的活动」，
      而人正处在某个活动的**中间**时，这个活动起点已过、后面又没有更晚的活动，
      于是返回 null 被当成「到头」。分片一到边界（车库 120 秒、客厅 300 秒）就收播。
      真机证据：车库 03:43:24 播放在 `03:42:33→04:00:26` 这条车辆活动内部，
      客厅 03:40:01 在 `03:38:30→03:44:54` 内部，两条都在块中间弹「播完最后一个活动」。

   ② **把活动当互不重叠的区间序列**：0.9.69 读端分档后，AI 段会吸收 60 秒内的移动簇
      （mv 标记），AI 段与移动簇可以**同起点、不同终点**甚至相交。所以
      「拿第一条覆盖到 t 的就完事」会拿到较短的那条，提前判成离开活动。

   统一成两个纯函数，推进、跳播、收播文案全走它们，不再各算各的：
     pvActSpan(t) → 覆盖 t 的活动里**最晚的结束时刻** e（没有则 null）
     pvActNext(t) → 起点晚于 t 的活动里**最早的那个**（没有则 null）
   之所以返回 e 而不是事件对象：调用方只关心「这段活动还剩多少可播」。 */
function pvActSpan(t){
  const evs = pvEvents();
  let e = null;
  for(let k = 0; k < evs.length; k++){
    const ev = evs[k];
    if(t >= ev.s - PV_ROLL - 0.01 && t < ev.e && (e === null || ev.e > e)) e = ev.e;
  }
  return e;
}
function pvActNext(t){
  const evs = pvEvents();
  let nx = null;
  for(let k = 0; k < evs.length; k++){
    const ev = evs[k];
    if(ev.s > t + 0.25 && (nx === null || ev.s < nx.s)) nx = ev;
  }
  return nx;
}
/* 服务端时基下的「现在」（当天钟点秒）。红线、蓝块、收播文案共用一个时基：
   手机与 NAS 差几分钟时，用浏览器 Date 判「还在录」会判错（0.9.72 的教训） */
function pvNowSec(){
  const d = vtlData;
  return (d && typeof d.now === "number") ? d.now : null;
}
/* 最后一片之后是否还在录：录制仍在写新分片 → 不是「没录像了」，是「还没录到」 */
function pvStillRec(){
  const segs = PPLAY.segs, last = segs && segs[segs.length - 1];
  if(!last) return false;
  const now = pvNowSec();
  if(now === null) return false;                       // 不是今天，没有「还在录」这回事
  const rec = state.rec || {};
  const segLen = Math.max(10, Math.min(3600, +(rec.segment_sec || LK.segSec || 300)));
  return now < last.e + segLen;
}

/* 下一个播放目标：
   活动模式 = ① 还在这段活动里 → 接下一分片继续播（cont）；
              ② 活动已播完 → 下一个活动起点（垫 PV_ROLL）；
              ③ 都没有 → null（真到头，交给 pvAdvance 重试/收播）
   全时模式 = 下一分片起点 */
function pvNextTarget(){
  if(PPLAY.filter === "act"){
    /* ① 人在活动里：先把这段播完，别急着找「下一个活动」。
       分片边界落在活动中间是常态（2~5 分钟一片），这里必须接上下一片，
       否则每跨一次片就被判一次「播完」（0.9.73 真机每次到分片尾就停的根因） */
    const e = pvActSpan(PPLAY.t);
    if(e !== null){
      const nx = PPLAY.segs[PPLAY.segIdx + 1];
      if(nx && nx.s < e) return { t: nx.s, cont:true };
    }
    /* ② 活动已播完（或活动内已无片可接）：去下一个活动起点 */
    const nxEv = pvActNext(PPLAY.t);
    if(nxEv) return { t: Math.max(0, nxEv.s - PV_ROLL) };
    return null;
  }
  const nx = PPLAY.segs[PPLAY.segIdx + 1];
  return nx ? { t: nx.s } : null;
}

function pvAdvance(){
  if(!PPLAY.on) return;
  const tgt = pvNextTarget();
  if(!tgt){
    /* 列表尽头 ≠ 磁盘尽头：进页时的段列表是快照，正在录的 .part 不在其中，
       播放期间新分片还会陆续封口。先重拉一次清单再判，确实没有才收播
       （0.9.56：修「已播到今天最后一段录像」误报——时间轴蓝轨比段列表新） */
    if(!PPLAY.endHit){
      PPLAY.endHit = true;
      fetchRecordings(function(){
        if(!PPLAY.on) return;              // 等待刷新时用户已停播
        if(PPLAY.filter === "act"){
          pvRefreshLedger(function(){      // 活动模式的目标来自台账，也刷新一次
            if(!PPLAY.on) return;
            if(pvNextTarget()){ pvAdvance(); return; }
            pvFinish();
          });
          return;
        }
        if(pvNextTarget()){ pvAdvance(); return; }
        pvFinish();
      });
      return;
    }
    /* endHit 已置位 = 有一次刷新在途，收播/续播由它的回调裁决；
       这里再收一次会和回调抢（分片尾与刷新同时到时提前弹提示） */
    return;
  }
  PPLAY.endHit = false;   // 正常推进，恢复「尽头重试」资格
  /* 活动内跨分片 = 正常连播：不快进、不跳、不改倍速，直接接下一片。
     必须排在下面两个 act 分支之前——它们都会给目标加 PV_ROLL 或设 ff，
     而 cont 的目标是分片起点本身，加了余量就会跳过 3 秒画面 */
  if(tgt.cont){ pvPlayAt(tgt.t, { quiet:true }); return; }
  /* 静止段快进：不 seek，继续播但加速静音，到下一活动起点恢复原速。
     注意先 pvPlayAt 再设 ff（pvPlayAt 内部会清 ff，顺序反了会被吃掉） */
  if(PPLAY.filter === "act" && PPLAY.silent !== "skip"){
    const evT = tgt.t + PV_ROLL;      // 活动本体起点（不需要落点余量）
    const seg = PPLAY.segs[PPLAY.segIdx];
    /* 已到分片尾/当前片已不在：光设 ff 不会换片，画面会停在结束帧——
       必须真换片（0.9.59 修：16× 模式在分片尾卡死） */
    if(!seg || PPLAY.t >= seg.e - 0.05 || PPLAY.t >= evT){
      pvPlayAt(Math.max(evT, PPLAY.t + 0.2), { quiet:true });
      return;
    }
    if(!pvSegAt(PPLAY.t)){
      const nx = PPLAY.segs.find(sg => sg.e > PPLAY.t);
      if(nx && !pvPlayAt(nx.s, { quiet:true })) return;
    }
    PPLAY.ff = evT;
    pvRate();
    return;
  }
  /* 活动模式 + 直接跳过：落点走 pvActTarget（不跨回上一分片、只准前进），
     否则「活动起点-3 秒」贴在分片边界时会 29/30 秒死循环；全时模式维持原样 */
  if(PPLAY.filter === "act"){
    pvPlayAt(pvActTarget(tgt.t + PV_ROLL, PPLAY.t + 0.2), { quiet:true });
    return;
  }
  pvPlayAt(tgt.t, { quiet:true });
}

/* 播完收尾的文案要说准，别把三种完全不同的处境混成一句话：
   活动模式以前一律说「已播完今天最后一个活动」，可 0.9.73 真机两次都发生在
   **人还在活动里**的时候（车库 03:43:24、客厅 03:40:01），用户看到的就是
   红线明明压在活动块上却被告知播完了 —— 文案和行为自相矛盾。
   现在按「还在不在活动里 × 还在不在录」分四句：
     在活动里 + 在录   → 已播到最新，这个活动还在录制（等分片封口）
     在活动里 + 没在录 → 这个活动后面没有录像（真有未录的空洞）
     已出活动 + 在录   → 已播完今天最后一个活动，正在录新的
     已出活动 + 没在录 → 已播完今天最后一个活动 */
function pvEndMsg(){
  if(PPLAY.filter === "act"){
    if(pvActSpan(PPLAY.t) !== null)
      return pvStillRec() ? "已播到最新，这个活动还在录制" : "这个活动后面没有录像";
    return pvStillRec() ? "已播完今天最后一个活动，正在录新的" : "已播完今天最后一个活动";
  }
  if(pvStillRec()) return "已播到最新一段，下一段录制中";
  return "已播到今天最后一段录像";
}
function pvFinish(){
  pvStop();
  toast("toast2", pvEndMsg());
}

/* 只刷新台账数据不动画面（播放中重绘会晃动滚动位置），给活动模式尽头重试用 */
function pvRefreshLedger(done){
  const day = curDay();
  if(!state.camId || !day){ if(done) done(); return; }
  API.get("/api/ledger?cam=" + encodeURIComponent(state.camId) + "&date=" + day)
    .then(d=>{
      if(d && d.ok){ vtlData = d; vtlDay = day; }
      if(done) done();
    }).catch(()=>{ if(done) done(); });
}

/* 倍速设定：活动模式快进时按预设 16/32×（浏览器上限 16）+ 静音；平时按用户倍速 */
function pvRate(){
  const ff = PPLAY.ff != null;
  const r = ff ? Math.min(16, +PPLAY.silent || 16) : Math.min(16, +PPLAY.speed || 1);
  [pvA, pvB].forEach(v=>{ try{ v.playbackRate = r; v.muted = ff; }catch(e){} });
}

/* 活动筛选：掉出活动窗口 = 进入静止段，按预设方案处理——
   skip = 直接 seek 到下一活动起点；16/32 = 静音快进到活动本体。
   判定窗口用 s-PV_ROLL：跳播落点本身就垫在活动前 3 秒，不能把它当成静止段，否则死循环 */
function pvSkipStatic(){
  if(PPLAY.filter !== "act") return false;
  if(!pvEvents().length) return false;
  /* 还在某个活动里（活动会重叠，取最晚的 e）→ 继续播，什么都不做 */
  if(pvActSpan(PPLAY.t) !== null){ PPLAY.inAct = true; return false; }
  const nxt = pvActNext(PPLAY.t);
  if(!nxt){
    /* 已经过了最后一个活动。只有「从活动里自然播出来」才收播——
       手动跳播/进回放落在静止区（inAct=false）不该立刻弹「已播完最后一个活动」，
       那是 0.9.57 的回归：点任意时刻都被判成播完。自然播完仍走 pvAdvance 的
       尽头重试（先刷清单/台账，可能刚判出新活动），endHit 防重复触发 */
    if(PPLAY.inAct){
      PPLAY.inAct = false;
      if(!PPLAY.endHit) pvAdvance();
    }
    return false;
  }
  PPLAY.inAct = false;   /* 已离开活动，正走向下一个活动 */
  if(PPLAY.silent === "skip") return pvPlayAt(pvActTarget(nxt.s, PPLAY.t + 0.2), { quiet:true });
  if(PPLAY.ff == null){ PPLAY.ff = nxt.s; pvRate(); }
  return false;
}

/* 某时刻是否落在活动窗口内（含落点余量 PV_ROLL）——跳播后用它判定「是不是从活动里开始」 */
function pvInEvent(t){
  return pvActSpan(t) !== null;
}

/* 活动跳播落点：常规垫 PV_ROLL 吸收关键帧误差；两个硬约束——
   ① 不许跨回上一分片：活动紧贴分片边界时（如 23:30:02 的活动，s-3 = 23:29:59）
      落点会掉进上一分片的最后一秒，播完那一秒分片就结束、pvAdvance 又把同一落点
      算回来，于是在 29/30 秒之间死循环（0.9.59 修）；
   ② 不许后退：floorT 保证每次跳播都往前，杜绝原地打转 */
function pvActTarget(evS, floorT){
  let t = Math.max(0, evS - PV_ROLL);
  const hit = pvSegAt(evS);           // 活动起点所在分片
  if(hit && t < hit.seg.s) t = hit.seg.s;
  if(floorT != null && t < floorT) t = floorT;
  return t;
}

function pvOnTime(){
  if(!PPLAY.on) return;
  const v = pvCur, seg = PPLAY.segs[PPLAY.segIdx];
  if(!seg) return;
  /* seek 未落定时 currentTime 还是 0/旧值，拿它算位置会被当成分片起点：
     活动模式据此「跳到下一个活动」、全时据此误判分片到头。落定前一律不处理 */
  if(v.seeking) return;
  const nt = seg.s + (v.currentTime || 0);
  if(PPLAY.seekTo != null){
    /* 落点没生效就等下一个 timeupdate；超过 3 秒还没对上（文件比预期短/seek 被钳制）
       按实际位置继续，不能卡死在守卫里 */
    if(Math.abs(nt - PPLAY.seekTo) > 5 && Date.now() - (PPLAY.seekAt || 0) < 3000) return;
    PPLAY.seekTo = null;
  }
  PPLAY.t = nt;
  if(PPLAY.ff != null && PPLAY.t >= PPLAY.ff){ PPLAY.ff = null; pvRate(); }
  if(pvSkipStatic()) return;
  if(PPLAY.t >= seg.e - 0.05 || v.ended){ pvAdvance(); return; }
  pvSyncOsd(); pvSyncVtl();
}

function pvSyncOsd(){
  const s = hms(PPLAY.t);
  const a = document.getElementById("playOsdTime"), b = document.getElementById("playTNow");
  if(a) a.textContent = s;
  if(b) b.textContent = s;
}
function pvSyncCtl(){
  const big = document.getElementById("playBig");
  if(big) big.classList.toggle("off", PPLAY.on);
  /* 暂停浮层：已开始播 + 当前暂停时浮现（未开播交给 .pbig，不重复长按钮） */
  const pf = document.getElementById("playPauseFloat");
  if(pf) pf.classList.toggle("on", !!(PPLAY.on && pvCur && pvCur.paused && !pvCur.ended));
  /* 全屏时：播放中隐藏时间轴悬浮层，暂停恢复（CSS 由 pvplaying 类驱动）。
   注意用「正在播」而不是 PPLAY.on：暂停后要能看到时间轴拖动跳转 */
  const pg = document.getElementById("page-play");
  const nowPlaying = PPLAY.on && pvCur && !pvCur.paused && !pvCur.ended;
  if(pg) pg.classList.toggle("pvplaying", nowPlaying);
  document.querySelectorAll("[data-pvfilter]").forEach(x=>
    x.classList.toggle("on", x.dataset.pvfilter === PPLAY.filter));
  /* 活动类别筛选：图例即筛选（0.9.88）——只有活动模式可点，选中项亮蓝色胶囊；
     全时模式保持只读（actmode 不加，.on 也不加） */
  const evl = document.getElementById("evLegend");
  const actMode = PPLAY.filter === "act";
  if(evl) evl.classList.toggle("actmode", actMode);
  document.querySelectorAll("#evLegend [data-pvcls]").forEach(x=>
    x.classList.toggle("on", actMode && !!PPLAY.cls[x.dataset.pvcls]));
  /* 播放/暂停图标随真实状态变（暂停/切分片/快进都会触发 pause 事件） */
  const ico = document.getElementById("playTogIco");
  if(ico){
    const playing = PPLAY.on && pvCur && !pvCur.paused;
    ico.innerHTML = playing ? '<path fill="#fff" d="M8 5h3v14H8zM13 5h3v14h-3z"/>'
                            : '<path fill="#fff" d="M8 5l11 7-11 7z"/>';
  }
  const sp = document.getElementById("btnPvSpeed");
  if(sp) sp.textContent = (PPLAY.speed || 1) + "×";
}

/* 播放跟随：固定时刻线不动，滚动内容让它对准自下而上 2/3 高度处（用户拖着时暂不打扰） */
let vtlHold = 0;   /* 用户最近一次拖动/捏合的时间戳，2 秒内不抢滚动 */
function pvSyncVtl(){
  const sc = document.getElementById("vtlScroll");
  if(sc && PPLAY.on && Date.now() - vtlHold > 2000)
    sc.scrollTop = Math.max(0, vtlY(PPLAY.t) - Math.round(sc.clientHeight / 3));
  syncVnowline();
  /* 红线扫过事件 → 气泡与连线自动跟随（0.9.90）。放在滚动之后：
     换气泡要用最新的 scrollTop 判断是否上提 */
  vtlFollow(PPLAY.t);
  /* 簇内不换气泡，只点亮「现在播到哪一类」那一行（0.9.91） */
  vtlRowHi(PPLAY.t);
}

/* 回放页这一天该播哪种源：与 loadDay 同一口径（dayKind + 用户手动选的维度）。
   以前 pvLoadDay 写死 "full"，于是第 4~7 天只有归档的日子永远播不出东西。 */
function pvKind(day){
  let kind = dayKind(day);
  if(state.pmode === "full" || state.pmode === "archive"){
    const key = state.pmode === "archive" ? "archive" : "cameras";
    if((((state.rec || {})[key] || {})[state.camId] || {})[day]) kind = state.pmode;
  }
  return kind;
}

/* 回放页的分片清单，统一成「钟点坐标」。
   全时：realSegs 的 s/e 本来就是钟点，原样用。
   归档：realSegs 给的 s/e 是「按真实播放时长累加」的播放坐标（0,1,2…），
         那是文件页顺序播的口径；回放页的 PPLAY.t 要拿去和时间轴红线、OSD、
         台账活动窗口比对，必须是钟点，混用会整天错位。所以这里改写：
           s = 源窗口起点 src，e = src + 真实时长
         · 文件内 0 秒永远对应 s，seek 公式 PPLAY.t - seg.s 两种源通用；
         · e 取真实时长而不是窗口终点 —— 文件本身只有这么长，照窗口画
           会播到一半就被判「到头」（真机 148 个片段比标记窗短 ≥1 秒）。 */
function pvSegs(camId, day){
  if(!camId || !day) return [];
  const kind = pvKind(day);
  const segs = realSegs(camId, day, kind) || [];
  if(kind !== "archive") return segs;
  return segs.map(function(x){
    const len = x.dur > 0.3 ? x.dur : Math.max(1, (x.srcEnd || x.src + 1) - x.src);
    return Object.assign({}, x, { s: x.src, e: x.src + len, clock: true });
  });
}

/* 换机位/换日期：停播并重装分片清单 */
function pvLoadDay(){
  const day = curDay();
  if(PPLAY.camId !== state.camId || PPLAY.day !== day){
    pvStop();
    PPLAY.segIdx = -1; PPLAY.t = 0;
  }
  PPLAY.segs = pvSegs(state.camId, day);
  PPLAY.endHit = false;   // 新清单装好，「尽头重试」资格恢复
  PPLAY.inAct = false;    // 换日/换机位：不继承上一个清单的「在活动内」状态
  const cam = CAMERAS.find(c=>c.id === state.camId);
  const oc = document.getElementById("playOsdCam");
  if(oc) oc.textContent = cam ? cam.ch : "—";
}

/* ===== 回放默认落点（0.9.72）=====
   今天      → 最新一段的段首
   历史日期  → 当天第一段的段首
   活动筛选  → 当天第一个活动前 PV_ROLL 秒（0.9.60 起的语义，这里不动）

   为什么不再用「段尾往前 30 秒」：0.9.48 用 last.e-30 想让画面贴近"现在"，
   但录制一停 last.e 就是很久以前，而且落点只剩一两秒可播 —— ended 之后
   pvAdvance 找不到下一段就 pvFinish 收播，用户看到的是「点一下、闪一下、没了」，
   再点还是闪一下。段首落点一定能连播满一整段，关键帧也天然对齐。 */
function pvEnterStart(){
  const segs = PPLAY.segs;
  if(!segs.length) return null;
  return curDay() === jsToday() ? segs[segs.length - 1].s : segs[0].s;
}

/* 选中的日期一个分片都没有（今天还没落盘 / 这是条空日期）→ 退到最近有录像的一天。
   全时与归档都算「有录像」：以前只认 full，于是点了只有归档的那天（第 4~7 天）
   会被悄悄弹回最近的全时日 —— 表现就是「归档的日期切不过去」。
   返回是否发生了切换（切完 PPLAY.segs 已是新日期的清单）。 */
function pvFallbackDay(){
  if(PPLAY.segs.length) return false;
  const list = (state.dayList || []).slice().reverse();   // 旧→新，从最近的往前找
  for(const d of list){
    if(!pvSegs(state.camId, d).length) continue;
    state.daySel = d;
    state.dayIdx = (state.dayList || []).indexOf(d);
    loadDay();                  // 清单/时间轴/维度 chip 一起跟上，别只换播放器
    pvLoadDay();
    renderPlayDates();          // 实际回退到了别的日期，菜单栏显示跟上
    renderVtl(null, curDay());
    return true;
  }
  return false;
}

/* 自动入场：进回放页 / 换机位 / 换日期 / 点播放都走这里。
   force=true 时即使正在播也重新定位（换机位、换日期必须跳）；
   清单还没拉回来就先拉一次，回来后自己再进来 —— 不再误报「该时刻无录像」。
   拉清单用 3 秒节流而不是布尔锁：锁一旦因异常路径没复位，
   之后进页就再也不会自动定位了（这类哑弹只有真机才摸得到）。 */
let pvEnterAt = 0;
function pvAutoEnter(force){
  if(!state.camId) return;
  if(!state.rec){                        // 清单没拉到：拉一次再来
    const now = Date.now();
    if(now - pvEnterAt < 3000) return;   // 3 秒内只拉一次，避免重复请求
    pvEnterAt = now;
    fetchRecordings(function(){ if(state.rec) pvAutoEnter(force); });
    return;
  }
  if(!force && PPLAY.on) return;         // 已经在播：重绘不打断
  pvLoadDay();                           // 清单可能刚回来，段列表按最新数据重装
  const fellBack = pvFallbackDay();
  if(!PPLAY.segs.length) return;
  /* 事件页点进来的落点优先（0.9.121）：它带的是用户刚点的那条事件，
     不是"默认落点"。两种情况作废 —— 回退过日期（目标那天一片都没有，
     落点已经没有意义），或者已经过期（清单拉了三次才回来，超过 15 秒）；
     不作废的话，用户下次进回放页会被一条很久以前的事件莫名其妙地带走。 */
  if(pvSeekTo){
    const p = pvSeekTo;
    pvSeekTo = null;
    if(!fellBack && Date.now() - p.at < 15000 && pvPlayAt(p.t)) return;
  }
  const t = pvEnterStart();
  if(t == null) return;
  pvPlayAt(t);
}

/* 大播放钮 / 轻点画面：有断点就续播，没有就从默认落点开始 */
function pvDefaultStart(){
  const evs = pvEvents();
  if(PPLAY.filter === "act" && evs.length) return Math.max(0, evs[0].s - PV_ROLL);
  return pvEnterStart();
}
function pvResume(){
  if(PPLAY.segIdx >= 0 && pvCur.getAttribute("src")){
    PPLAY.on = true;
    pvSafePlay();
    pvSyncCtl();
    return;
  }
  const t = pvDefaultStart();
  if(t == null){
    /* 清单没到 / 这天确实没片子：交给 pvAutoEnter 去拉清单或回退日期，
       不再直接弹「该时刻无录像」—— 那对用户等于「点了没用」 */
    if(!state.rec) toast("toast2", "正在读取录像清单…");
    pvAutoEnter();
    return;
  }
  pvPlayAt(t);
}

[pvA, pvB].forEach(v=>{
  v.addEventListener("timeupdate", e=>{
    if(e.target !== pvCur) return;   // 预加载路的 timeupdate 不参与
    pvOnTime();
  });
  /* 播放/暂停状态实时反映到操作条图标 */
  v.addEventListener("play", e=>{ if(e.target === pvCur) pvSyncCtl(); });
  v.addEventListener("pause", e=>{ if(e.target === pvCur) pvSyncCtl(); });
  v.addEventListener("ended", e=>{
    if(e.target !== pvCur || !PPLAY.on) return;
    pvAdvance();
  });
});
