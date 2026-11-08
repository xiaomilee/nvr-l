/* ================= pages/playback_data.js =================
   回放页在进时间轴之前怎么拿到**磁盘上真实存在的文件**：日期列表重算、
   按 kind 取片段、跨页同步（设置页改了机位/名字后直播与回放要跟着变）、
   摄像头下拉与切机位。
   判断标准：回答"哪一天、哪一路、有哪些片段"的取数与同步放这里；
   一旦开始按秒算坐标、画刻度、绑定 video 的 currentTime，就是 timeline.js。
   为什么跟时间轴分开：这一段错了的表现是"明明有录像却显示空白"，
   那一段错了的表现是"有录像但点进去跳错秒"，两类问题的排查入口完全不同。
   日期列表早年固定 slice(-7)，保留期设 30 天也只看得到 7 天 —— 这类
   "数据给少了"的 bug 只在这一份里查。
   ========================================================== */

/* ================= 回放页只认磁盘上的真实文件 ================= */
/* 按 state.rec 缓存重算当前摄像头的日期列表（切摄像头时立即可用，不等网络）
   以前固定 slice(-7)：保留期设成 30 天也只看得到 7 天，用户会以为"录像丢了"。
   日期条本来就是横向滚动的，给全量更符合预期。

   0.9.23 修复「多日数据下选 A 天加载成 B 天」：这里曾经 sort().reverse()
  （新→旧全量），renderDates() 又自己 sort().slice(-7)（旧→新末 7 天），
   两套顺序并存 —— 点日期条写入的 dayIdx 在 recalc 的顺序里指向另一天，
   N 天里只有正中间那天碰巧加载对。现在顺序只有一种：旧→新全量；
   选中跟「日期字符串」走（daySel），不跟下标，跨天/切模式不漂移。 */
function recalcDayList(){
  const rec = state.rec || {};
  /* 文件页覆盖「全时 + 归档浓缩」两源：任意一天只要任一源有文件就进日期条，
     这样 7 天回溯里第 4~7 天（只有 _archive 归档片段）也能翻到，
     不再像以前只看 cameras 导致后几天在文件页彻底消失。
     （_clip 缩时片段已下线，即使磁盘上还有历史产物也不再进日期条。） */
  const set = {};
  ["cameras", "archive"].forEach(k=>{
    const d = (rec[k] && rec[k][state.camId]) || {};
    Object.keys(d).forEach(day=>{ if(d[day] && d[day].length) set[day] = 1; });
  });
  const all = Object.keys(set).sort();          // 旧→新，全量（两源并集）
  /* 日期下拉必须含当前日期：今天还没落盘第一个文件时也补进去，
     否则用户打开下拉找不到"今天"（v0.9.48 反馈） */
  if(all.length){
    const today = jsToday();
    if(all[all.length - 1] < today) all.push(today);
  }
  state.dayList = all;
  let idx = state.daySel ? all.indexOf(state.daySel) : -1;
  if(idx < 0){
    /* 没选过 / 选中的天没了 → 落在「最近一个真有录像的日子」。
       以前直接取 all.length-1，而 all 末位永远补着今天，于是凌晨打开文件页
       时今天还没落盘、列表整片空白。今天有片时这里仍然选中今天，行为不变。 */
    idx = -1;
    for(let i = all.length - 1; i >= 0; i--){
      const hv = dayHas(all[i]);
      if(hv.full || hv.archive){ idx = i; break; }
    }
    if(idx < 0) idx = all.length ? all.length - 1 : 0;
  }
  state.dayIdx = idx;
}

/* 某一天「以哪种 kind 展示」：当天有全时原画 → full；否则有归档浓缩 → archive；
   否则有物化切片 → clip。文件页不再按全局 pmode 单选，而是跟着当天磁盘上实际
   有什么文件走 —— 否则第 4~7 天的归档片段会因为没有 full 而被整个跳过。 */
function dayKind(day){
  const hv = dayHas(day);
  /* 用户手动选过的维度优先（同一天既可能看全时原画、也可能看归档浓缩），
     但只在那一源当天真有文件时才生效，避免选了归档、切到没有归档的日子后空白。 */
  const kp = state.kindPick;
  if(kp && kp.day === day && hv[kp.kind]) return kp.kind;
  if(hv.full) return "full";
  if(hv.archive) return "archive";
  return "full";
}

/* 某天两种源各有多少个文件（0 表示没有）。文件页的维度切换、dayKind 的默认顺序
   都用它，口径必须与 /api/recordings 的 cameras / archive 两个键一致。
   返回「数量」而不是布尔：切换 chip 上直接带条数，用户一眼知道哪天有归档、有多少。
   （_clip 缩时片段已下线：后端即使还有历史产物也不再作为可选维度。） */
function dayHas(day){
  const rec = state.rec || {};
  const cam = state.camId;
  const n = k=>{ const d = (rec[k] && rec[k][cam]) || {}; return (d[day] && d[day].length) || 0; };
  return {full:n("cameras"), archive:n("archive")};
}

/* 列表头维度切换：当天有 2 个源才出现。
   以前只有一个 dayKind 自动判定（全时优先），于是「全时还没过期」的那些天里
   归档片段一个都看不到 —— 用户点了立即归档、产物也写盘了，界面却毫无变化。 */
function renderKindChips(day){
  const el = document.getElementById("kindChips");
  if(!el) return;
  const hv = day ? dayHas(day) : {full:0,archive:0};
  const opts = [];
  if(hv.full) opts.push(["full", "全时 " + hv.full]);
  if(hv.archive) opts.push(["archive", "归档 " + hv.archive]);
  if(opts.length < 2){ el.innerHTML = ""; return; }
  el.innerHTML = opts.map(o=>
    '<button class="kchip' + (state.curKind === o[0] ? " on" : "") +
    '" data-kind="' + o[0] + '" title="' + (o[0] === "full" ? "全时录像（原画）" : "归档浓缩（全时过期后裁出）") + '">'
    + o[1] + '</button>').join("");
  el.querySelectorAll("[data-kind]").forEach(b=>{
    b.onclick = ()=>{
      state.kindPick = {day: day, kind: b.dataset.kind};
      state.pmode = b.dataset.kind;
      loadDay();
    };
  });
}
let recRetry = 0;
function fetchRecordings(done, force){
  if(typeof fetch !== "function"){ if(done) done(); return; }
  /* 首屏还没有任何清单时先亮一句「正在读取」：后端要扫磁盘上两万多个分片，
     机器慢/盘冷时真会等几秒。不给反馈的话用户看到的是「还没有任何录像文件」，
     会以为没录上。已有清单时（切页刷新）不打扰，免得闪一下。 */
  if(!state.rec){
    state.recLoading = true;
    try{ applyPlaybackVisibility(); }catch(e){}
  }
  /* force=1 绕过服务端 3 秒短缓存：归档刚跑完这类「必须立刻看见」的场合用。
     缓存破坏参数由请求层统一加，这里不再自己拼 ?_t。 */
  API.get(force ? "/api/recordings?force=1" : "/api/recordings", { timeout: 60000 })
    .then(d=>{
    state.recLoading = false;
    if(!d || !d.ok){ if(done) done(); return; }
    state.rec = d;
    recRetry = 0;
    recalcDayList();
    renderDates(); renderOverview(); renderHeat(); renderClips();
    renderKindChips(curDay());
    applyPlaybackVisibility();
    try{ renderPlay(); }catch(e){}   /* 回放页若开着，吃到新清单后重绘日期条 */
    if(done) done();
  }).catch(()=>{
    if(recRetry++ < 3){ setTimeout(function(){ fetchRecordings(done); }, 2500); return; }   // 服务刚重启时给几次机会
    /* 重试都失败：收掉「正在读取」，退回原来的空态文案，别让转圈文案永远挂着 */
    state.recLoading = false;
    try{ applyPlaybackVisibility(); }catch(e){}
    if(done) done();
  });
}

/* 某天真实分片 → 回放片段。只剩两种源：
     全时录像  文件名 HH-MM-SS 就是起始时刻，时长 = 下一片的起点（末片用 segment_sec）。
               这是「时间轴定位」，用户要能对着钟点找到 14:23 发生了什么。
     归档浓缩  文件是裁剪成品（<分片>.cNN.mp4）：一个文件可能只覆盖源录像里的一小段。
               播放坐标（s/e）按真实播放时长从 0 累加，播放器靠它推进；
               但**展示坐标**（src/srcEnd）取自 .idx.json 里的源时间窗 —— 早期把
               播放坐标直接画在钟点轴上，列表里全是 00:00:00 附近、点某个钟点
               又提示无视频，就是这么来的。热力条 / 列表 / 播放器 OSD 一律用 src。
   （缩时片段 _clip/ 已下线，不再是有可选维度。） */
function realSegs(camId, day, kind){
  const rec = state.rec || {};
  const isComp = kind === "archive";
  const src = isComp ? rec.archive : rec.cameras;
  const days = src && src[camId];
  const files = days && days[day];
  if(!Array.isArray(files) || !files.length) return null;
  const segLen = Math.max(10, Math.min(3600, +(rec.segment_sec || LK.segSec || 300)));
  const toS = n=>{ const q = n.slice(0,8).split("-"); return (+q[0])*3600 + (+q[1])*60 + (+q[2]); };
  const url = n=> "/api/record/file?kind=" + (isComp ? "archive" : "full")
                + "&cam=" + encodeURIComponent(camId)
                + "&day=" + day + "&name=" + encodeURIComponent(n) + "&remux=1";

  if(isComp){
    let acc = 0;
    const out = [];
    files.forEach((f,i)=>{
      const dur = Math.max(0.2, +f.d || 0);
      const realD = +f.d || 0;             /* 服务端 ffprobe 的真实时长，缺省 0 = 未知 */
      /* 归档片段可能「文件在、画面几乎没有」：源分片在那段时间已经断流，
         ffmpeg 只交出尾巴几帧。真机全库体检 3078 片里有 15 片不到 1 秒，
         画在 24 小时轴上是一个几乎不可见的点，点进去闪一下 —— 直接不画，
         免得回放页多出一串假黑块。d 未知（老数据）时一律保留。
         阈值与后端 condense.TINY_PART_SEC 对齐。 */
      if(realD > 0 && realD < 1.0) return;
      /* 覆盖窗：活动切片（.cNN，同一源分片可能出好几个片段）用服务端从
         .idx.json 解出的真实源时间窗；整段拼接才按「下一个文件起点」推。 */
      let st, en;
      if(Array.isArray(f.w) && f.w.length === 2){
        st = +f.w[0];
        en = Math.max(st + 1, +f.w[1]);
        /* 台账窗口是「这段该有多长」，d 是「文件里实际有多长」。取短的那个：
           真机 148 个片段文件比标记窗短 ≥1 秒（最狠一条标记 237 秒、实物
           152 秒），照标称画就是一个 237 秒的宽块，播到 152 秒就断。 */
        if(realD > 0 && st + realD < en) en = Math.max(st + 0.2, st + realD);
      }else{
        st = toS(f.n);                        // 源时刻：分片文件名就是起点钟点
        const next = i+1 < files.length ? toS(files[i+1].n) : Math.min(86400, st + segLen);
        en = Math.max(st + 1, next);          // 覆盖到下一片开始（末片按分片长度兜底）
      }
      /* clip:true 只表示「这是压缩/裁剪产物，播放时长 ≠ 源覆盖时长」，
         时间轴与 OSD 靠它切换坐标口径，与已下线的缩时片段无关。 */
      out.push({ s: acc, e: acc + dur, src: st, srcEnd: en, dur,
                 level:.5, reason:"归档浓缩", hits:0,
                 bytes:f.b || 0, file:url(f.n), clip:true, arch:true, name:f.n });
      acc += dur;
    });
    /* 全都被判成废片 → 这天没有可播的归档内容，按「无数据」处理 */
    return out.length ? out : null;
  }
  return files.map((f,i)=>{
    const s = toS(f.n);
    const e = i+1 < files.length
      ? Math.max(s+1, toS(files[i+1].n))
      : Math.min(86400, s + segLen);
    return { s, e, level:.5, reason:"已录像", hits:0, bytes:f.b || 0, file:url(f.n) };
  });
}

/* 展示坐标：归档浓缩用源时刻（src），全时用自身（s）。热力条/列表/概览统一走这里 */
function segA(seg){ return seg.src != null ? seg.src : seg.s; }
function segB(seg){ return seg.srcEnd != null ? seg.srcEnd : seg.e; }
function curDay(){
  return (state.dayList && state.dayList[state.dayIdx]) || DAYS[state.dayIdx];
}

/* 没有录像文件就不显示日期条、片段列表这些空架子 */
function applyPlaybackVisibility(){
  const rec = state.rec || {};
  const camDays = (rec.cameras && rec.cameras[state.camId]) || {};
  const archDays = (rec.archive && rec.archive[state.camId]) || {};
  const hasCam = Object.keys(camDays).length || Object.keys(archDays).length;
  const show = !!hasCam;
  [".modebar","#dates",".heatwrap",".listhead","#clips"].forEach(sel=>{
    const el = document.querySelector(sel);
    if(el) el.style.display = show ? "" : "none";
  });
  const empty = document.getElementById("playEmpty");
  if(!empty) return;
  empty.style.display = show ? "none" : "block";
  if(!rec || !rec.total){
    /* 首屏在等清单时不要说「还没有任何录像」——那句话是结论，不是过程 */
    empty.innerHTML = state.recLoading
      ? "正在读取录像清单…<br><span style=\"font-size:11.5px;color:var(--faint2)\">正在扫描磁盘上的分片；分片多的通道第一次会慢一点，之后走缓存</span>"
      : "还没有任何录像文件<br><span style=\"font-size:11.5px;color:var(--faint2)\">填好 RTSP 地址并启用后，录像会按「摄像头 / 日期 / 时段」存进保存目录；出现文件后这里会自动点亮</span>";
  }else{
    const cam = curCam();
    const what = state.curKind === "archive"
      ? "归档浓缩片段：全时录像过期后由归档任务裁出"
      : "录像文件";
    empty.innerHTML = "「" + esc(cam ? cam.name : "该摄像头") + "」还没有" + what;
  }
}

/* ================= 跨页同步 ================= */
/* 设置页增删摄像头、改名字之后，直播与回放不能还拿着启动时那份列表 */
function syncCams(){
  try{ renderLive(); }catch(e){}
  try{ renderDates(); }catch(e){}
  try{ renderOverview(); }catch(e){}
  try{ renderHeat(); }catch(e){}
  try{ renderClips(); }catch(e){}
  try{ syncDiagSum(); }catch(e){}
  try{ renderCamPick(); }catch(e){}
  try{ fetchRecordings(); }catch(e){}   /* 摄像头增删后，回放页的录像列表跟着同步 */
}

/* 通用参数是即时生效的，这里只把依赖它的界面重新对齐 */
function genApply(){
  try{ syncDiagSum(); }catch(e){}
  try{ renderDates(); }catch(e){}
  try{ updateLiveHeader(); }catch(e){}
  try{ renderClips(); }catch(e){}
}

/* ================= 回放页：摄像头下拉 ================= */
function renderCamPick(){
  const box = document.getElementById("camPick");
  if(!box) return;
  if(!CAMERAS.length){
    box.innerHTML = '<div class="cempty">还没有摄像头 · 去设置页添加</div>';
    return;
  }
  box.innerHTML = CAMERAS.map(c=>{
    const pp = camParams[c.id] || {};
    const off = !c.online || !pp.main || pp.enabled === false;
    return '<div class="cp' + (c.id === state.camId ? " on" : "") + '" data-pickcam="' + c.id + '">'
      + '<span class="dt' + (off ? " off" : "") + '"></span>'
      + '<span class="nm">' + esc(c.name) + '</span>'
      + '<span class="ch">' + c.ch + '</span></div>';
  }).join("");
  box.querySelectorAll("[data-pickcam]").forEach(el=>{
    el.onclick = e=>{ e.stopPropagation(); pickCam(el.dataset.pickcam); };
  });
}

function camPickShow(force){
  const box = document.getElementById("camPick");
  const sel = document.getElementById("camSel");
  if(!box) return;
  const open = (force == null) ? !box.classList.contains("on") : !!force;
  box.classList.toggle("on", open);
  if(sel) sel.classList.toggle("open", open);
  if(open) renderCamPick();
}

/* 切摄像头：回放页所有面板都按新机器重算 */
function pickCam(id, autoPlay){
  const c = CAMERAS.find(x=>x.id === id);
  if(!c) return;
  state.camId = id;
  state.hourBlock = -1;
  camPickShow(false);
  try{ loadDay(); }catch(e){}
  renderOverview(); syncDiagSum(); renderDates();
  renderCamPick();
  try{ renderPlay(autoPlay); }catch(e){}   /* 回放页若开着，跟随机位切换 */
  /* 设置页若正展开这一路，跟着切过去 */
  if(SET.open) SET.open = id;
  renderCamTabs();
  toast("toast2", "已切到「" + c.name + "」");
}
