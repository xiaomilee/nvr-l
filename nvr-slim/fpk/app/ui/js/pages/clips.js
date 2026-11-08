/* ================= pages/clips.js =================
   日期条 → 热力条 → 时段对比时间轴 → 播放页内嵌热力格 → 概览 → 缩略图 →
   片段列表 → 场景绘制。它们连起来只回答一个问题：**这一天磁盘上有哪些录像、
   分别占哪段时间**。所以放在一起，而不是按函数名切成八片 —— 改日期条的配色
   口径时要连着看热力条与片段列表，分家之后这种"同一份事实的三种画法"最容易
   只改一处。
   判断标准：只在"进播放器之前那一屏"出现的渲染函数放这里；从下面
   `const player = ...` 起已经是播放器的地盘，归 player/，不进这个文件。
   为什么不含垂直时间轴：8547 起那段是按秒定位的播放中时间轴，坐标、缩放、
   事件气泡全绑着 <video> 的 currentTime，跟这里的"按天看有什么片"不是一件事。
   ====================================================== */

/* ================= 日期条 ================= */
function renderDates(){
  const box = document.getElementById("dates");
  if(!box) return;
  recalcDayList();                       // 顺序只有这一种：旧→新全量，别处不得再排
  const list = state.dayList;
  box.innerHTML = "";
  /* 0.9.113 修「昨天被标成今天」：这里原来用 new Date().toISOString().slice(0,10)，
     toISOString 走 UTC —— 北京时间 00:00~08:00 之间 UTC 还停在前一天，于是日期条
     把昨天写成「今天」，而今天那格反倒显示成周几。全文件就地用本地日历 jsToday。 */
  const today = jsToday();
  list.forEach((d,i)=>{
    const dt = new Date(d+"T00:00:00");
    const rec = state.rec || {};
    const camDays = (rec.cameras && rec.cameras[state.camId]) || {};
    const archDays = (rec.archive && rec.archive[state.camId]) || {};
    /* 日期条的体积按「那天磁盘上真实存在的所有源」合计：全时 + 归档 */
    const fullArr = camDays[d] || [];
    const archArr = archDays[d] || [];
    const files = fullArr.concat(archArr);
    const bytes = files.reduce((a,f)=>a+(f.b||0),0);
    const ktext = files.length
      ? (bytes >= 1073741824 ? (bytes/1073741824).toFixed(1)+"G" : Math.max(1,Math.round(bytes/1048576))+"M")
      : "—";
    /* 这天是哪种源（配色依据）：两个都在 → both（归档交接期，只持续一天左右） */
    const kind = (fullArr.length && archArr.length) ? "both"
      : (archArr.length ? "archive" : "full");
    const b = document.createElement("button");
    b.className = "dchip" + (i===state.dayIdx ? " on" : "");
    b.dataset.k = kind;
    b.title = kind === "both" ? "全时 + 归档都有（归档交接期，约一天）"
      : kind === "archive" ? "只有归档浓缩（全时已过期）" : "全时录像（原画）";
    b.innerHTML = '<span class="w">' + (d===today ? "今天" : WD[dt.getDay()].replace("周","")) + '</span>' +
      '<span class="d">' + d.slice(5).replace("-","/") + '</span>' +
      '<span class="k">' + ktext + '</span>';
    b.onclick = ()=>{ state.dayIdx = i; state.daySel = d; state.hourBlock = -1; loadDay(); };
    box.appendChild(b);
  });
  const on = box.querySelector(".dchip.on");
  if(on) on.scrollIntoView({inline:"center", block:"nearest"});
}

/* ================= 热力条 ================= */
/* 按「展示坐标」（归档=源时刻，全时=分片起点）落进 2 小时格 */
function renderHeat(){
  /* 0.9.42：文件页热力图退役，职责移交回放页垂直时间轴。
     但 renderHeat 还被播放器同步链（syncPlayer / 时段跳转）调用——它的
     renderTl() 尾巴是播放器上方时间 lanes 的重建入口。热力元素不存在时
     只跳过热力绘制，同步职责照常履行。 */
  const box = document.getElementById("heat");
  if(!box){ renderTl(); return; }
  const segs = state.activity;
  const per = new Array(12).fill(0);   // 每格 2 小时，单位秒
  segs.forEach(s=>{
    const a = Math.max(0, segA(s));
    const b = Math.min(86400, Math.max(a + 1, segB(s)));
    const i0 = Math.min(11, Math.floor(a / 7200));
    const i1 = Math.min(11, Math.floor(Math.max(a, b - 0.01) / 7200));
    for(let i = i0; i <= i1; i++){
      const blkEnd = (i+1)*7200;
      per[i] += Math.max(0, Math.min(b, blkEnd) - Math.max(a, i*7200));
    }
  });
  const max = Math.max(...per, 1);
  box.innerHTML = "";
  per.forEach((v,i)=>{
    const d = document.createElement("button");
    d.className = "hcell" + (state.hourBlock===i ? " on" : "");
    const ratio = v / max;
    d.style.background = v <= 0 ? "var(--fill)"
      : "rgba(31,157,99," + (0.16 + ratio*0.78).toFixed(2) + ")";
    d.onclick = ()=>{
      state.hourBlock = (state.hourBlock===i) ? -1 : i;
      renderHeat(); renderClips();
    };
    box.appendChild(d);
  });
  const clear = document.getElementById("heatClear");
  clear.style.display = state.hourBlock >= 0 ? "block" : "none";
  clear.onclick = ()=>{ state.hourBlock = -1; renderHeat(); renderClips(); };
  document.getElementById("heatTitle").textContent =
    state.hourBlock >= 0 ? pad(state.hourBlock*2)+":00 – "+pad(state.hourBlock*2+2)+":00" : "全天活动";
  renderTl();
}

/* ================= 时段对比时间轴 =================
   两根左端对齐、同一 24h 刻度的时间轴（0.9.30 引入；0.9.33 起常驻
   播放器画面上方，固定全天视角，真实时段基准行与缩放态下线）：
   全时录像（绿块分布，空档=断录）/ 归档浓缩（蓝块，颜色深浅=压缩密度：
   同样覆盖 1 小时压得越短颜色越深）。
   - 色块可点直接跳播（当前模式没有就自动切另一种模式）；
   - 全时块按相邻合并（gap≤1s 视为连续），归档逐片成块；
   - 数据各自取自当天真实清单，缺哪种就如实显示「无」。
   缩时片段（_clip）已下线，不再占一行。 */
function unionLen(spans, a, b){
  const xs = spans.map(x=>[Math.max(a, x[0]), Math.min(b, x[1])])
    .filter(x=>x[1] > x[0]).sort((p,q)=>p[0]-q[0]);
  let tot = 0, curE = -1;
  xs.forEach(x=>{
    const s = Math.max(x[0], curE);
    if(x[1] > s){ tot += x[1] - s; curE = x[1]; }   // 重叠部分只记一次
  });
  return tot;
}

/* 合并相邻/重叠区间为色块（裁剪到窗口）：全时逐文件区间合并后，
   空档才是真实的断录；相邻分片之间 ≤1s 的缝视为连续 */
function mergeBlocks(spans, a, b){
  const xs = spans.map(x=>[Math.max(a, x[0]), Math.min(b, x[1])])
    .filter(x=>x[1] > x[0]).sort((p,q)=>p[0]-q[0]);
  const out = [];
  xs.forEach(x=>{
    const last = out[out.length-1];
    if(last && x[0] - last[1] <= 1) last[1] = Math.max(last[1], x[1]);
    else out.push([x[0], x[1]]);
  });
  return out;
}

/* 压缩密度 → 归档块透明度：d = 播放秒 / 源覆盖秒，越大说明该段画面变化越多 */
function tlDens(d){
  return d > 0.06 ? 1 : d > 0.02 ? 0.62 : 0.34;
}

/* 时间轴点块跳播：跨界也按点击时刻找到覆盖它的那个片段。
   当前模式不同就先切过去（按钮态同步），再在清单里定位播放 */
function tlPlay(kind, t){
  if(state.pmode !== kind){
    state.pmode = kind;
    document.querySelectorAll("[data-pmode]").forEach(x=>x.classList.toggle("on", x.dataset.pmode===kind));
    loadDay();
  }
  const idx = state.segs.findIndex(s=> t >= segA(s) - 0.5 && t < segB(s));
  if(idx >= 0) openPlayer(idx);
}

function renderTl(){
  const el = document.getElementById("tlane");
  if(!el) return;
  const fulls = realSegs(state.camId, curDay(), "full") || [];
  const archives = realSegs(state.camId, curDay(), "archive") || [];
  if(!fulls.length && !archives.length){ el.style.display = "none"; el.innerHTML = ""; return; }
  /* 0.9.34 恢复跟随选中的时段：回放页热力条选中某个 2 小时格（hourBlock）
     后，时间轴放大到该窗口看片段级分布；未选中则显示全天。 */
  const zoom = state.hourBlock >= 0;
  const a = zoom ? state.hourBlock*7200 : 0;
  const b = zoom ? a + 7200 : 86400;
  const real = b - a;

  /* 全时的覆盖不能直接用 segA/segB —— realSegs 会把相邻文件首尾桥接
     （上一片的终点=下一片的起点），清单里缺文件造成的真断录会被抹平。
     这里用「文件起点序列 + 分片长度」还原：某段没有文件，块就在
     起点+segLen 处停住，空档如实留在轴上。 */
  const segLen = Math.max(10, Math.min(3600, +((state.rec || {}).segment_sec || LK.segSec || 300)));
  const fstarts = [...new Set(fulls.map(s=>s.s))].sort((p,q)=>p-q);
  const fblocks = fstarts.map((st,i)=>
    [st, i+1 < fstarts.length ? Math.min(st+segLen, fstarts[i+1]) : st+segLen]);
  const fb = mergeBlocks(fblocks, a, b);
  const fullCov = unionLen(fblocks, a, b);
  let gaps = 0;
  for(let i=1;i<fb.length;i++) if(fb[i][0] - fb[i-1][1] > 1) gaps++;

  /* 归档浓缩：逐片成块（裁剪到窗口），密度随片携带。
     块本身用 [起,止] 数组（与全时的 fb 同构，blocksHtml 统一按 x[0]/x[1] 取值），
     密度挂在数组的 d 属性上 —— 不要再推 {s,e,d} 对象，否则坐标取出来是 undefined、
     色块直接画成 NaN%。 */
  let archCov = 0, aWatch = 0;
  const ab = [];
  const astarts = [...new Set(archives.map(c=>segA(c)))].sort((p,q)=>p-q);
  const abyStart = {};
  archives.forEach(c=>{ abyStart[segA(c)] = c; });
  astarts.forEach((st,i)=>{
    const en = i+1 < astarts.length ? Math.min(st+segLen, astarts[i+1]) : st+segLen;
    const ca = Math.max(st, a), cz = Math.min(en, b);
    if(cz <= ca) return;
    const span = Math.max(0.001, en - st);
    const ov = cz - ca;
    archCov += ov;
    const c = abyStart[st];
    aWatch += Math.max(0, c.e - c.s) * ov / span;
    ab.push(Object.assign([ca, cz], { d: Math.max(0, c.e - c.s)/span }));
  });

  const blocksHtml = (blocks, kind)=>blocks.map(x=>
    '<span class="tl-block" data-t0="' + x[0] + '" data-t1="' + x[1] + '"'
    + ' style="left:' + ((x[0]-a)/real*100).toFixed(2) + '%;width:'
    + Math.max(0.5, (x[1]-x[0])/real*100).toFixed(2) + '%'
    + (kind === "full" ? ';background:var(--keep)'
       : ';background:var(--arch);opacity:' + tlDens(x.d))
    + '"></span>').join("");
  /* 0.9.34 行结构：说明行（名称左 · 摘要右）在上，色条独占一整行贯穿全宽，
     高度 20px 方便手指点按跳播 */
  const row = (lbl, kind, inner, val)=>'<div class="tlrow">'
    + '<div class="tl-cap"><span class="tl-lbl">' + lbl + '</span>'
    + '<span class="tl-val">' + val + '</span></div>'
    + '<span class="tl-track" data-tlkind="' + kind + '">' + inner
    + '<i class="tl-cursor"></i></span></div>';

  const fullVal = fullCov > 0
    ? (zoom ? durText(fullCov) + " · 覆盖 " + Math.round(fullCov/real*100) + "%"
            : durText(fullCov) + (gaps ? " · 断录 " + gaps + " 段" : " · 无断录"))
    : "无录像";
  const archVal = archCov <= 0 ? "无归档片段"
    : aWatch > 0 ? durText(aWatch) + " · 压缩 ≈" + (archCov/aWatch).toFixed(1) + "×"
    : durText(archCov) + " · 无成片";
  const ticks = zoom
    ? [0,1,2,3,4].map(i=>hm(a + i*1800))
    : [0,4,8,12,16,20,24].map(h=>String(h).padStart(2,"0"));

  /* 标题行也一并重建：容器必须是「空架子」，早退分支的 innerHTML="" 才不会
     把自己的骨架清掉（0.9.30 的教训，见 0.9.32 评审）。 */
  el.innerHTML =
    '<div class="tl-head"><b>' + (zoom ? hm(a) + " – " + hm(b) + " 分布" : "全天分布") + '</b>'
      + '<span class="tl-all" data-tlall' + (zoom ? "" : ' style="display:none"') + '>‹ 全天</span>'
      + '<span class="tl-hint">' + (zoom ? "点色块跳播" : "点色块选中时段") + '</span></div>'
    + row("全时录像", "full", blocksHtml(fb, "full"), fullVal)
    + row("归档浓缩", "archive", blocksHtml(ab, "archive"), archVal)
    + '<div class="tl-ticks">' + ticks.map(t=>"<span>" + t + "</span>").join("") + '</div>';

  /* 0.9.36 顶部热力图 ↔ 底部 12 格双向联动：
     全天视图点色块 → 选中点击处所在 2 小时格（底部格子琥珀描边、
     自己放大到该窗口）+ 跳播到点击时刻；放大视图点色块 → 窗口内
     精确跳播。 */
  el.querySelectorAll("[data-tlkind]").forEach(tk=>{
    const kind = tk.dataset.tlkind;
    tk.onclick = e=>{
      const r = tk.getBoundingClientRect();
      const t = a + Math.max(0, Math.min(1, (e.clientX - r.left)/Math.max(1, r.width))) * real;
      if(!zoom){
        const blk = Math.min(11, Math.max(0, Math.floor(t/7200)));
        if(state.hourBlock !== blk){
          state.hourBlock = blk;
          renderPlayerHeat();    /* 底部 12 格选中描边 */
          renderHeat();          /* 回放页热力条同步 + 尾部 renderTl（自己重建成放大态） */
        }
      }
      tlPlay(kind, t);
    };
  });
  const all = el.querySelector("[data-tlall]");
  if(all) all.onclick = ()=>{
    state.hourBlock = -1;
    renderPlayerHeat();
    renderHeat();          /* 尾部 renderTl 回全天 */
  };
  el.style.display = "block";
  syncTlCursor();          /* 重建后立即摆到当前播放位置 */
}

/* 当前播放文件的定位竖线：全时/归档两条轨同源同位。
   位置口径与 syncPlayer 的 srcNow 一致——全时按钟点、归档按源时刻
   （播放坐标按片内占比映射回源时间），全天/选中时段两种窗口通用；
   当前文件与窗口无交集时隐藏。timeupdate 频繁触发，这里只改样式
   不重建 DOM。 */
function tlCursorFrac(){
  const seg = state.segs[state.segIdx];
  if(!seg) return null;
  const p = Math.max(0, Math.min(1, (state.t - seg.s) / Math.max(0.2, seg.e - seg.s)));
  const ct = seg.clip && seg.src != null
    ? seg.src + p * Math.max(1, seg.srcEnd - seg.src) : state.t;
  const zoom = state.hourBlock >= 0;
  const a = zoom ? state.hourBlock*7200 : 0;
  const b = zoom ? a + 7200 : 86400;
  const frac = (ct - a) / (b - a);
  return (frac < 0 || frac > 1) ? null : frac;
}
function syncTlCursor(){
  const el = document.getElementById("tlane");
  if(!el) return;
  const cursors = el.querySelectorAll(".tl-cursor");
  if(!cursors.length) return;
  const frac = tlCursorFrac();
  if(frac === null){ cursors.forEach(c=>c.style.display = "none"); return; }
  cursors.forEach(c=>{
    c.style.display = "block";
    c.style.left = (frac*100).toFixed(2) + "%";
  });
}

/* ================= 播放页内嵌热力格 =================
   0.9.31：把回放页的热力格搬进播放器非全屏画面下方——视频是
   inset:0 + contain 铺满 stage 居中，热力格插在 stage 和控制区之间，
   stage 变矮、画面随之下移，竖屏下大片死黑区被利用起来。
   - 数据吃 state.activity（当前模式的真实清单），与回放页热力条同源同口径；
   - 正在播放的片段落在哪个 2 小时格，哪格高亮（随上/下一段自动跟随）；
   - 点格跳播：当前模式该格有片段就直接跳，没有则尝试另一种模式，
     两种都没有如实提示「该时段没有录像」；横屏/全屏态整块隐藏。 */
function renderPlayerHeat(){
  const box = document.getElementById("pHeat");
  if(!box) return;
  const segs = state.activity || [];
  const per = new Array(12).fill(0);
  segs.forEach(s=>{
    const sa = Math.max(0, segA(s));
    const sb = Math.min(86400, Math.max(sa + 1, segB(s)));
    const i0 = Math.min(11, Math.floor(sa / 7200));
    const i1 = Math.min(11, Math.floor(Math.max(sa, sb - 0.01) / 7200));
    for(let i = i0; i <= i1; i++){
      const blkEnd = (i+1)*7200;
      per[i] += Math.max(0, Math.min(sb, blkEnd) - Math.max(sa, i*7200));
    }
  });
  if(!per.some(v=>v > 0)){ box.style.display = "none"; box.innerHTML = ""; return; }
  const cur = state.segs[state.segIdx];
  const curBlock = cur ? Math.min(11, Math.max(0, Math.floor(segA(cur)/7200))) : -1;
  const max = Math.max(...per, 1);
  box.innerHTML =
    '<div class="ph-cap">点格子选中时段 · 上方显示该时段分布</div>'
    + '<div class="heat ph-heat">' + per.map((v,i)=>
        '<button class="phcell' + (i===curBlock ? " on" : "")
        + (i===state.hourBlock ? " sel" : "") + '" data-ph="' + i + '"'
        + ' style="background:' + (v <= 0 ? "rgba(255,255,255,.12)"
            : "rgba(53,201,140," + (0.22 + v/max*0.72).toFixed(2) + ")") + '"></button>').join("")
    + '</div>'
    + '<div class="ph-lbl"><span>00</span><span>04</span><span>08</span><span>12</span><span>16</span><span>20</span><span>24</span></div>';
  box.style.display = "block";
  box.querySelectorAll("[data-ph]").forEach(btn=>{
    btn.onclick = ()=> pHeatJump(+btn.dataset.ph);
  });
}

function pHeatJump(i){
  /* 0.9.35：底部热力格 = 时段选择器——点某格先选中（琥珀描边），上方
     时间轴随之放大到该 2 小时窗口显示全时/归档分布；再点同一格取消
     选中回全天。选中与跳播并存：该时段有录像就顺带跳到首段，没有则
     只提示（空时段的分布照常显示）。 */
  const toggle = state.hourBlock === i;
  state.hourBlock = toggle ? -1 : i;
  renderHeat();          /* 回放页热力条同步 + 尾部 renderTl 联动（时间轴缩放） */
  renderPlayerHeat();    /* 选中态描边（正在播的蓝描边不受影响） */
  if(toggle) return;
  const a = i*7200;
  const has = (list)=> list.some(s=>segA(s) < a+7200 && segB(s) > a);
  const firstT = (list)=>{
    const o = list.filter(s=>segA(s) < a+7200 && segB(s) > a).sort((p,q)=>segA(p)-segA(q))[0];
    return o ? Math.max(segA(o), a) : null;   /* o 本身就是片段，别再取 .s */
  };
  if(has(state.segs)) return tlPlay(state.pmode, firstT(state.segs));
  /* 当前维度这段是空的：换另一种源再试（全时 ↔ 归档），都没有才提示 */
  const other = state.pmode === "full" ? "archive" : "full";
  const alt = realSegs(state.camId, curDay(), other) || [];
  if(has(alt)) return tlPlay(other, firstT(alt));
  toast("toast", "该时段没有录像");
}

/* ================= 概览 ================= */
/* 0.9.33 只剩两件事：摄像头名 + 「CH · N 个录像文件」（并入名行） */
function renderOverview(){
  const cam = CAMERAS.find(c=>c.id===state.camId);
  const nameEl = document.getElementById("camSelName");
  if(nameEl) nameEl.textContent = cam ? cam.name : "还没有摄像头";
  const sub = document.getElementById("hdrSub");
  if(sub) sub.textContent = !cam
    ? "还没有摄像头 · 去设置页添加一台"
    : cam.ch + " · " + state.segs.length + (state.curKind === "archive" ? " 个归档片段" : " 个录像文件");
}

/* ================= 缩略图 ================= */
function drawThumb(cv, level){
  const w = 192, h = 108;
  cv.width = w; cv.height = h;
  const c = cv.getContext("2d");
  c.fillStyle = "#232830"; c.fillRect(0,0,w,h);
  const hz = h*0.34;
  c.fillStyle = "#2c313a"; c.fillRect(0,0,w,hz);
  c.fillStyle = "#363c45"; c.fillRect(0,hz,w,h-hz);
  c.strokeStyle = "rgba(255,255,255,.05)"; c.lineWidth = 1;
  for(let i=-3;i<=3;i++){
    c.beginPath(); c.moveTo(w*0.5+i*w*0.07, hz); c.lineTo(w*0.5+i*w*0.33, h); c.stroke();
  }
  c.fillStyle = "#1b1e24"; c.fillRect(w*0.40, h*0.06, w*0.14, hz-h*0.06);
  c.fillStyle = "#262b33"; c.fillRect(w*0.80, h*0.10, w*0.06, h*0.60);
  c.fillStyle = "#3f4650"; c.fillRect(w*0.05, h*0.66, w*0.26, h*0.14);
  const px = w*(0.16 + 0.6*level);
  const ph2 = h*0.20;
  c.fillStyle = "#c2cad6";
  c.fillRect(px-ph2*0.13, h*0.80-ph2*0.62, ph2*0.26, ph2*0.5);
  c.beginPath(); c.arc(px, h*0.80-ph2*0.72, ph2*0.115, 0, Math.PI*2); c.fill();
  const g = c.createLinearGradient(0,h*0.6,0,h);
  g.addColorStop(0,"rgba(0,0,0,0)"); g.addColorStop(1,"rgba(0,0,0,.45)");
  c.fillStyle = g; c.fillRect(0,h*0.6,w,h*0.4);
}

/* ================= 片段列表 =================
   过滤/展示都用「展示坐标」：归档片段按源时刻（segA/segB），全时按分片起点。
   以前压缩模式直接把播放坐标（从 0 累加的秒数）当钟点过滤，点 14:00 的
   格子永远提示"这个时段没有任何画面变化"。 */
function visibleSegs(){
  if(state.hourBlock < 0) return state.segs.map((s,i)=>({s,i}));
  const a = state.hourBlock*7200, b = a+7200;
  return state.segs.map((s,i)=>({s,i})).filter(o=>segA(o.s) < b && segB(o.s) > a);
}

/* 列表分批渲染的批次号：每次重绘自增，旧批次发现号变了就停手 */
let clipSeq = 0;

function renderClips(){
  const box = document.getElementById("clips");
  const list = visibleSegs();
  const p = camParams[state.camId];
  const full = state.curKind === "full";
  const cam = CAMERAS.find(c=>c.id===state.camId);
  box.innerHTML = "";
  document.getElementById("listTitle").textContent =
    state.hourBlock >= 0 ? pad(state.hourBlock*2)+":00 – "+pad(state.hourBlock*2+2)+":00"
      : (full ? "全时录像" : "归档浓缩");
  document.getElementById("listCount").textContent = list.length + " 段";

  if(!list.length){
    box.innerHTML = '<div style="text-align:center;color:var(--faint);font-size:13px;padding:44px 0">'
      + (full ? "该时段没有录像"
              : "这个时段没有归档浓缩片段（全时录像过期后才由归档任务裁出）") + '</div>';
    return;
  }

  /* 一天 288 个分片、归档日更多：一次建完 288 个卡片 + 288 张 canvas 缩略图
     会把手机主线程占住近一秒（用户感受就是「点了没反应」）。按每帧 30 条铺，
     首屏立刻有内容，滚动与点按也还能响应。
     seq 用来作废上一批：期间切了日期/机位，旧批不能再往新列表里塞。 */
  const seq = ++clipSeq;
  let i = 0;
  (function step(){
    if(seq !== clipSeq) return;
    const end = Math.min(i + 30, list.length);
    for(; i < end; i++){
      const s = list[i].s;
      const idx = list[i].i;
      const size = s.bytes ? s.bytes/1048576
        : fullDayMbps(cam)*(s.e-s.s)/8/1000;
      const cov = Math.max(1, segB(s) - segA(s));
      const d = document.createElement("div");
      d.className = "card";
      d.innerHTML =
        '<canvas class="thumb"></canvas>' +
        '<div class="cbody">' +
          '<div class="ct mono">' + hms(segA(s)) + ' – ' + hms(segB(s)) + '</div>' +
          '<div class="cm"><span>' + (full ? durText(s.e-s.s)
              : "播放 " + durText(s.e-s.s) + " · 覆盖源 " + durText(cov)) + '</span><span>' + size.toFixed(1) + ' MB</span></div>' +
          '<div style="margin-top:5px;display:flex;gap:5px">' +
            (full
              ? '<span class="tag">' + (s.hits ? "含活动 " + s.hits + " 段" : (s.file ? "连续录像" : "无活动")) + '</span>'
              : '<span class="tag g">' + (s.src != null ? "归档 · " + hms(s.src) : s.reason) + '</span>') +
          '</div>' +
        '</div>' +
        '<svg class="chev" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>';
      box.appendChild(d);
      if(s.file){
        drawThumbReal(d.querySelector(".thumb"), s);   // 真分片：时间+体积，不画假画面
      }else{
        drawThumb(d.querySelector(".thumb"), s.level);
      }
      d.onclick = ()=> openPlayer(idx);
    }
    if(i < list.length) requestAnimationFrame(step);
  })();
}

/* 真实分片的缩略图：只标时间和体积，不伪造画面 */
function drawThumbReal(cv, seg){
  const w = 192, h = 108;
  cv.width = w; cv.height = h;
  const c = cv.getContext("2d");
  c.fillStyle = "#14171c"; c.fillRect(0,0,w,h);
  c.fillStyle = "#1e232a"; c.fillRect(0,h*0.66,w,h*0.34);
  c.fillStyle = "#9aa2ad"; c.font = "11px Consolas,monospace"; c.textAlign = "center";
  c.fillText(hms(segA(seg)) + " – " + hms(segB(seg)), w/2, h/2);
  c.fillStyle = "#5d646f"; c.font = "10px sans-serif";
  c.fillText((seg.bytes/1048576).toFixed(1) + " MB", w/2, h/2 + 17);
}

/* ================= 场景绘制 ================= */
const scene = document.getElementById("scene");
const sctx = scene.getContext("2d");
let noiseSeed = 1;

function roundRect(ctx,x,y,w,h,r){
  r = Math.min(r, w/2, h/2);
  ctx.beginPath();
  ctx.moveTo(x+r,y);
  ctx.arcTo(x+w,y,x+w,y+h,r);
  ctx.arcTo(x+w,y+h,x,y+h,r);
  ctx.arcTo(x,y+h,x,y,r);
  ctx.arcTo(x,y,x+w,y,r);
  ctx.closePath();
}

function drawScene(){
  const rect = scene.getBoundingClientRect();
  if(!rect.width) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = rect.width, h = w * 9/16;
  if(scene.width !== Math.round(w*dpr) || scene.height !== Math.round(h*dpr)){
    scene.width = Math.round(w*dpr);
    scene.height = Math.round(h*dpr);
    scene.style.height = h + "px";
  }
  sctx.setTransform(dpr,0,0,dpr,0,0);
  sctx.clearRect(0,0,w,h);

  const seg = state.segIdx >= 0 ? state.segs[state.segIdx] : null;
  const prog = seg ? Math.max(0, Math.min(1, (state.t-seg.s)/Math.max(1,seg.e-seg.s))) : 0;

  sctx.fillStyle = "#20242a"; sctx.fillRect(0,0,w,h);
  const hz = h*0.34;
  sctx.fillStyle = "#292e36"; sctx.fillRect(0,0,w,hz);
  sctx.fillStyle = "#333941"; sctx.fillRect(0,hz,w,h-hz);

  sctx.strokeStyle = "rgba(255,255,255,.055)"; sctx.lineWidth = 1;
  for(let i=-4;i<=4;i++){
    sctx.beginPath();
    sctx.moveTo(w*0.5+i*w*0.06, hz);
    sctx.lineTo(w*0.5+i*w*0.30, h);
    sctx.stroke();
  }
  sctx.fillStyle = "#181b20"; sctx.fillRect(w*0.40, h*0.06, w*0.15, hz-h*0.06);
  sctx.fillStyle = "rgba(255,238,200,.10)"; sctx.fillRect(w*0.405, h*0.075, w*0.14, h*0.03);
  sctx.fillStyle = "#242930"; sctx.fillRect(w*0.80, h*0.10, w*0.055, h*0.62);

  sctx.fillStyle = "#3b414a"; roundRect(sctx, w*0.05, h*0.64, w*0.26, h*0.15, 4); sctx.fill();
  sctx.fillStyle = "rgba(140,175,205,.16)"; roundRect(sctx, w*0.09, h*0.59, w*0.18, h*0.06, 3); sctx.fill();
  sctx.fillStyle = "#434a54"; roundRect(sctx, w*0.47, h*0.71, w*0.28, h*0.16, 4); sctx.fill();

  const px = w*(0.14 + 0.68*prog);
  const py = h*(0.78 + 0.07*Math.sin(prog*Math.PI));
  const ph2 = h*0.22;
  sctx.fillStyle = "#bcc5d1";
  roundRect(sctx, px-ph2*0.13, py-ph2*0.62, ph2*0.26, ph2*0.52, 3); sctx.fill();
  sctx.beginPath(); sctx.arc(px, py-ph2*0.72, ph2*0.115, 0, Math.PI*2); sctx.fill();
  sctx.fillStyle = "rgba(0,0,0,.35)";
  sctx.beginPath(); sctx.ellipse(px, py+2, ph2*0.16, ph2*0.045, 0, 0, Math.PI*2); sctx.fill();

  /* 检测框 */
  if(seg){
    const bx = px-ph2*0.24, by = py-ph2*0.88, bw = ph2*0.48, bh = ph2*0.92;
    sctx.strokeStyle = "rgba(80,220,180,.9)"; sctx.lineWidth = 1.3;
    sctx.setLineDash([5,4]); sctx.strokeRect(bx,by,bw,bh); sctx.setLineDash([]);
    sctx.font = '9.5px "Cascadia Mono",Consolas,monospace';
    const label = seg.reason + " " + Math.round(70+seg.level*29) + "%";
    const tw2 = sctx.measureText(label).width + 9;
    sctx.fillStyle = "rgba(80,220,180,.9)";
    sctx.fillRect(bx, by-13, tw2, 12);
    sctx.fillStyle = "#08231a"; sctx.fillText(label, bx+4, by-3.5);
  }

  /* 检测忽略区：在画布占位场景上标出来，让用户确认"这块确实不算事件" */
  const igCells = normMasks(camParams[state.camId] && camParams[state.camId].masks).ignore;
  if(igCells.length){
    const cellW = w/GRID_COLS, cellH = h/GRID_ROWS;
    sctx.fillStyle = "rgba(240,180,40,.14)";
    sctx.strokeStyle = "rgba(240,180,40,.55)";
    sctx.lineWidth = 1;
    igCells.forEach(idx=>{
      const col = idx % GRID_COLS, row = (idx - col) / GRID_COLS;
      sctx.fillRect(col*cellW, row*cellH, cellW, cellH);
      sctx.strokeRect(col*cellW+.5, row*cellH+.5, cellW-1, cellH-1);
    });
  }

  /* 噪点 + 暗角 */
  const rnd = mulberry(noiseSeed);
  sctx.fillStyle = "rgba(255,255,255,.03)";
  for(let i=0;i<90;i++) sctx.fillRect(rnd()*w, rnd()*h, 1, 1);
  const g = sctx.createRadialGradient(w/2,h/2,Math.min(w,h)*0.3,w/2,h/2,Math.max(w,h)*0.72);
  g.addColorStop(0,"rgba(0,0,0,0)"); g.addColorStop(1,"rgba(0,0,0,.45)");
  sctx.fillStyle = g; sctx.fillRect(0,0,w,h);
}
