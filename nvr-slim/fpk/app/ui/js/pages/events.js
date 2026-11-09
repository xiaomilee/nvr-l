/* ================= pages/events.js =================
   事件页：最近 7 天的事件列表、四段筛选（日期 / 机位 / 类别 / 时间与时长）、
   事件卡片，以及「点事件直接看」的定位交接（哪一路 / 哪一天 / 从哪一秒）。
   只读 /api/events 与 clip 接口。
   判断标准：围绕一条事件的取数、渲染、筛选与跳转放这里；真正把画面播出来
   是 player/player.js 与 pages/timeline.js 的事，播放逻辑只该有一处实现。
   为什么单独成文件：评审点名的差异化之一就是「从翻录像变成点事件直接看」。
   段首那三条边界（不自己拼播放地址 / missing_reason 由服务端翻译 /
   筛选交给服务端）是这一页的契约，改动时不该悄悄把服务端口径搬回前端。
   ===================================================== */

/* ================= 事件页（0.9.121） =================
   评审点名的两件差异化之一：从「翻录像」变成「点事件直接看」。
   这一页只读三段接口（/api/events、/<id>、/<id>/clip），不碰录制链路；
   播放仍走回放页那一套（定位 / 预加载 / OSD 同步只该有一处实现），
   这里只负责把「哪一路、哪一天、从哪一秒」交代过去。

   三个边界写在这里，免得下一个人扩大它：
   1. 不自己拼播放地址：url 一律用服务端 clip 接口给的那一条（评审点名的
      第二个工程问题），前端只从分片名里读回"这一片从几点开始"。
   2. 播不了就说清原因：missing_reason 有多种（未封口 / 分片不在 / 结构坏），
      翻译文案由服务端给（event.MISSING_TEXT），前端不再维护第二份。
   3. 筛选全部交给服务端：拉全量再在前端过滤，会把"前 200 条里的全部"当成全部。 */
const EV = {day: jsToday(), cams: [], cats: [], from: "", to: "", minDur: "",
            more: false, data: null, key: "", at: 0, err: "",
            loading: false, open: "", focus: "", clip: null, clipErr: ""};

/* 筛选状态一律是数组 + 字符串，且**只在发请求时拼成参数**：
   界面上"选中"只是数组里有这一项，服务端只认拼好的查询串。中间不留第二份
   布尔状态（`catPerson: true` 之类），否则迟早出现"按钮亮着但查询没带"。 */
function evToggle(arr, v){
  const i = arr.indexOf(v);
  if(i >= 0) arr.splice(i, 1); else arr.push(v);
  return arr;
}
function evAnyFilter(){
  return !!(EV.cams.length || EV.cats.length || EV.from || EV.to || EV.minDur);
}
/* 筛选条件的人话版（列表头用）。只描述**当前真的在生效**的条件，
   不做"上次用过"的记忆 —— 界面上的高亮已经是唯一事实来源。 */
function evFilterText(){
  const parts = [];
  if(EV.cams.length){
    const names = EV.cams.map(id => (CAMERAS.find(c => c.id === id) || {}).name || id);
    parts.push(names.length > 2 ? names.length + " 路" : names.join(" / "));
  }
  if(EV.cats.length){
    parts.push(EV.cats.length > 2 ? EV.cats.length + " 类"
             : EV.cats.map(evCatText).join(" / "));
  }
  if(EV.from || EV.to) parts.push((EV.from || "00:00") + "–" + (EV.to || "24:00"));
  if(EV.minDur) parts.push("≥" + EV.minDur + " 秒");
  return parts.join(" · ");
}

/* 日期条 = 最近 7 天（今天在最前）。
   不做任意日期选择器：台账按天同步，而录像保留期默认就是 7 天；
   多翻一天就多一次整天重建索引，用户还多半看不到东西。 */
const EV_DAYS = 7;

function evPad(n){ return (n < 10 ? "0" : "") + n; }
function evYmd(d){
  return d.getFullYear() + "-" + evPad(d.getMonth() + 1) + "-" + evPad(d.getDate());
}
function evDayList(){
  const out = [];
  const now = new Date();
  for(let i = 0; i < EV_DAYS; i++)
    out.push(evYmd(new Date(now.getFullYear(), now.getMonth(), now.getDate() - i)));
  return out;
}

/* 分片名 `HH-MM-SS.mp4` → 当天钟点秒。
   这是前端唯一一处"从文件名读时间"，而且读的是服务端刚给的那一片：
   事件身份、偏移、可用性都由后端裁定，这里只把片头换算成钟点。
   解析不出来就返回 null —— 宁可让上层说"拿不到定位"，也不猜一个 0。 */
function evSegSec(name){
  const m = /^(\d{2})-(\d{2})-(\d{2})\.mp4$/.exec(String(name || ""));
  if(!m) return null;
  return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
}

/* clip 接口 → 播放入点（当天钟点秒）：分片起点 + 服务端给的偏移。
   偏移就是「事件起点前 10 秒」落在这一片里的位置，clamped 时它已从片首算过，
   所以这里不需要再判一次截断。 */
function evClipStart(clip){
  if(!clip) return null;
  const s = evSegSec(clip.segment);
  if(s == null) return null;
  return s + Math.max(0, +clip.offset_sec || 0);
}

function evCatText(k){
  return k === "person" ? "人员" : k === "vehicle" ? "车辆"
       : k === "two_wheeler" ? "两轮车" : k === "motion" ? "移动侦测" : String(k || "");
}
/* 两轮车并入车辆：与时间轴的三车道同一口径（evKind），否则同一条事件
   在时间轴上是绿的、在事件页是灰的。 */
function evCatCls(k){
  return k === "person" ? "person"
       : (k === "vehicle" || k === "two_wheeler") ? "vehicle" : "motion";
}
/* 卡片左侧色条取"最值得说"的类别：人 > 车 > 移动。
   一条事件常常同时有人员和车辆，色条只能有一个颜色，挑最能代表它的那个。
   0.9.126 起优先读服务端给的 `kind`：那是筛选与摘要共用的同一处判断，
   前端不再自己数 categories 的长度（两处判断迟早会分叉）。 */
function evMainCat(ev){
  if(ev && ev.kind === "motion") return "motion";
  const c = (ev && ev.categories) || [];
  if(c.indexOf("person") >= 0) return "person";
  if(c.indexOf("vehicle") >= 0 || c.indexOf("two_wheeler") >= 0) return "vehicle";
  return "motion";
}

function evFind(id){
  const evs = (EV.data && EV.data.events) || [];
  for(let i = 0; i < evs.length; i++) if(evs[i].id === id) return evs[i];
  return null;
}

/* 打开一条事件后展开的操作区。播不了时把服务端的中文原因摆出来 ——
   需求里"点击直接播放"的前提是这一段真的还在，不在就得说清是哪一种不在。 */
function evOpenHtml(ev){
  let note = "";
  if(EV.clipErr){
    note = '<div class="evnote">读不到定位：' + esc(EV.clipErr) + '</div>';
  }else if(EV.clip && EV.clip.missing_reason){
    note = '<div class="evnote">' + esc(EV.clip.missing_text || "这段录像现在播不了")
         + '<span class="code mono">' + esc(EV.clip.missing_reason) + '</span></div>';
  }else if(EV.clip && EV.clip.clamped){
    note = '<div class="evnote info">事件前面那段被片头截断，入点从片首开始。</div>';
  }
  return note + '<div class="evacts">'
    + '<button class="evbtn" data-evgo="-30">◀ 前 30 秒</button>'
    + '<button class="evbtn main" data-evgo="0">▶ 打开回放</button>'
    + '<button class="evbtn" data-evgo="30">后 30 秒 ▶</button>'
    + '</div>';
}

function evCardHtml(ev){
  const open = EV.open === ev.id;
  const cam = (CAMERAS.find(c => c.id === ev.cam) || {}).name || ev.cam || "—";
  /* 置信度只在这条事件真有评估值时才显示：台账里没有就留空，
     编一个 0.5 出来是最容易被当成"数字有出处"的那种假数据。 */
  const conf = (typeof ev.confidence_max === "number")
    ? " · 置信度 " + ev.confidence_max.toFixed(2) : "";
  const cats = (ev.categories && ev.categories.length) ? ev.categories : ["motion"];
  return '<div class="evcard" data-ev="' + esc(ev.id) + '">'
    + '<i class="evstrip ' + evMainCat(ev) + '"></i>'
    + '<div class="evmain">'
      + '<div class="evtime mono">' + gfmtClock(ev.start) + '</div>'
      + '<div class="evmeta">' + esc(cam) + " · " + gfmtDur(ev.duration) + conf + '</div>'
      + (open ? evOpenHtml(ev) : "")
    + '</div>'
    + '<div class="evtags">' + cats.map(k =>
        '<span class="evtag ' + evCatCls(k) + '">' + esc(evCatText(k)) + '</span>').join("")
    + '</div></div>';
}

function renderEvList(){
  const el = document.getElementById("evList");
  if(!el) return;
  let body;
  if(EV.err){
    body = '<div class="eveof">读取失败：' + esc(EV.err) + '<br>点右上角重新读一次</div>';
  }else if(!EV.data){
    body = '<div class="eveof">正在读事件台账（首次会按台账同步这一天的索引）…</div>';
  }else{
    const evs = EV.data.events || [];
    /* 接口给的是时间正序（那是时间轴的读法：从早到晚读一天）。事件页要的是
       "最近发生了什么"，所以列表倒过来展示 —— 只影响展示顺序，不参与任何
       定位计算，切回回放页的落点仍然是服务端给的坐标。 */
    const list = evs.slice().reverse();
    body = evs.length
      ? list.map(evCardHtml).join("") + '<div class="eveof">共 ' + evs.length
        + ' 条 · 点一条直接看</div>'
      : '<div class="eveof">' + (EV.day === jsToday() ? "今天" : EV.day)
        + ' 没有符合条件的事件。<br>判定关闭或这一路没开事件侦测时会是空的。</div>';
  }
  el.innerHTML = body;
}

function renderEvHead(){
  const el = document.getElementById("evSum");
  if(!el) return;
  if(EV.err){ el.textContent = "读取失败"; return; }
  if(!EV.data){ el.textContent = "—"; return; }
  const s = EV.data.summary || {};
  const by = s.by_category || {};
  const parts = [];
  ["person", "vehicle", "two_wheeler"].forEach(k=>{
    if(by[k]) parts.push(evCatText(k) + " " + by[k]);
  });
  /* 移动侦测单独一档，不进 by_category：它不是类别，是"一条类别都没有"的
     剩余集（服务端 event.summary_of 同口径）。 */
  if(s.motion) parts.push("移动侦测 " + s.motion);
  const cond = evFilterText();
  el.textContent = (s.count || 0) + " 条"
    + (parts.length ? " · " + parts.join(" / ") : "")
    + (cond ? " · 条件：" + cond : "");
}

/* 四段筛选条（0.9.126）。类别里含「移动侦测」—— 服务端从这一版起认
   `cat=motion`（= 一条 AI 类别都没有的事件），它不再是只能在界面上兜底的标签。
   多选口径：同一维内 OR，不同维之间 AND（与 `event_index.query` 一致）。 */
function evReload(){
  EV.open = ""; EV.focus = ""; EV.clip = null; EV.clipErr = "";
  EV.data = null;                     /* 换了条件：旧列表不许冒充新结果 */
  renderEvChips(); renderEvHead(); loadEvents(true);
}

function renderEvChips(){
  const dayBar = document.getElementById("evDayBar");
  if(dayBar){
    /* 日期仍是单选：一次看一天。台账按天分文件，跨天就得逐天同步，
       不该藏在一次点击后面。 */
    dayBar.innerHTML = evDayList().map(d =>
      '<button class="evchip' + (d === EV.day ? " on" : "") + '" data-evday="' + d + '">'
      + (d === jsToday() ? "今天" : d.slice(5)) + '</button>').join("");
    dayBar.querySelectorAll("[data-evday]").forEach(b=>{
      b.onclick = ()=>{ EV.day = b.getAttribute("data-evday"); evReload(); };
    });
  }
  const camBar = document.getElementById("evCamBar");
  if(camBar){
    const opts = [{id: "", name: "全部机位"}].concat(
      (CAMERAS || []).map(c => ({id: c.id, name: c.name || c.id})));
    camBar.innerHTML = opts.map(o => {
      const on = o.id ? EV.cams.indexOf(o.id) >= 0 : EV.cams.length === 0;
      return '<button class="evchip' + (on ? " on" : "") + '" data-evname="'
        + esc(o.id) + '">' + esc(o.name) + '</button>';
    }).join("");
    camBar.querySelectorAll("[data-evname]").forEach(b=>{
      b.onclick = ()=>{
        const id = b.getAttribute("data-evname");
        if(id) evToggle(EV.cams, id); else EV.cams = [];
        evReload();
      };
    });
  }
  const catBar = document.getElementById("evCatBar");
  if(catBar){
    /* motion 放最后：前三个是真实类别，它是"一条类别都没有"的剩余集。 */
    const opts = [{id: "", name: "全部类别"}, {id: "person", name: "人员"},
                  {id: "vehicle", name: "车辆"}, {id: "two_wheeler", name: "两轮车"},
                  {id: "motion", name: "移动侦测"}];
    catBar.innerHTML = opts.map(o => {
      const on = o.id ? EV.cats.indexOf(o.id) >= 0 : EV.cats.length === 0;
      return '<button class="evchip' + (on ? " on" : "") + '" data-evcat="'
        + esc(o.id) + '">' + esc(o.name) + '</button>';
    }).join("");
    catBar.querySelectorAll("[data-evcat]").forEach(b=>{
      b.onclick = ()=>{
        const id = b.getAttribute("data-evcat");
        if(id) evToggle(EV.cats, id); else EV.cats = [];
        evReload();
      };
    });
  }
  const moreBar = document.getElementById("evMoreBar");
  if(moreBar){
    const extra = [];
    if(EV.from || EV.to) extra.push((EV.from || "00:00") + "–" + (EV.to || "24:00"));
    if(EV.minDur) extra.push("≥" + EV.minDur + " 秒");
    let html = '<button class="evchip' + (EV.more || extra.length ? " on" : "")
      + '" data-evmore="1">' + (EV.more ? "收起" : "更多筛选") + '</button>';
    if(extra.length)
      html += '<span class="evchip on">' + esc(extra.join(" ")) + '</span>';
    if(evAnyFilter())
      html += '<button class="evchip" data-evclear="1">清空筛选</button>';
    moreBar.innerHTML = html;
    moreBar.querySelectorAll("[data-evmore]").forEach(b=>{
      b.onclick = ()=>{ EV.more = !EV.more; renderEvChips(); };
    });
    moreBar.querySelectorAll("[data-evclear]").forEach(b=>{
      b.onclick = ()=>{
        EV.cams = []; EV.cats = []; EV.from = ""; EV.to = ""; EV.minDur = "";
        evReload();
      };
    });
  }
  const box = document.getElementById("evMoreBox");
  if(box){
    box.hidden = !EV.more;
    /* 只在值不同才写回：每次渲染都重新赋值会让正在输入的秒数跳光标。 */
    const setv = (id, v)=>{
      const el = document.getElementById(id);
      if(el && el.value !== v) el.value = v;
    };
    setv("evFrom", EV.from);
    setv("evTo", EV.to);
    setv("evMinDur", EV.minDur);
  }
}

function evQuery(){
  const day = EV.day || jsToday();
  /* 带钟点的时间窗：`from=YYYY-MM-DD HH:MM` 服务端认（`_norm_when`），
     不带钟点时仍是整天。from/to 谁先谁后由服务端交换，前端不猜。 */
  const from = EV.from ? day + " " + EV.from : day;
  const to = EV.to ? day + " " + EV.to : day;
  let u = "/api/events?from=" + encodeURIComponent(from)
        + "&to=" + encodeURIComponent(to) + "&limit=200";
  if(EV.cams.length) u += "&cam=" + encodeURIComponent(EV.cams.join(","));
  if(EV.cats.length) u += "&cat=" + encodeURIComponent(EV.cats.join(","));
  if(EV.minDur) u += "&min_dur=" + encodeURIComponent(EV.minDur);
  return u;
}

/* 读列表。两条硬规矩：
   - 失败过的不自动重试（否则「渲染 → 请求 → 失败 → 再渲染」会打成环，页面一直闪）；
   - 同一组筛选 30 秒内吃缓存（服务端同步索引的代价是按天赋指纹，别一秒一次）。 */
function loadEvents(force){
  const url = evQuery();
  if(!force && EV.err) return Promise.resolve(EV.data);
  if(!force && EV.data && EV.key === url && (Date.now() / 1000 - EV.at) < 30)
    return Promise.resolve(EV.data);
  if(EV.loading) return Promise.resolve(EV.data);
  EV.loading = true;
  EV.key = url; EV.err = "";
  if(force) EV.data = null;
  renderEvHead(); renderEvList();
  return API.get(url).then(d=>{
    EV.loading = false;
    if(d && d.ok){ EV.data = d; EV.at = Date.now() / 1000; }
    else EV.err = (d && d.error) || "服务端没有返回事件数据";
  }).catch(e=>{
    EV.loading = false;
    EV.err = String((e && e.message) || e);
  }).then(()=>{ renderEvHead(); renderEvList(); return EV.data; });
}

/* 事件 → 回放要交代三件事：哪一路、哪一天、从哪一秒。
   前两件是 state，第三件塞给回放页的入场逻辑（pvSeekTo）。
   切页与起播都留在回放页那一侧 —— 定位/预加载/OSD 只该有一处实现。 */
function evTargetState(ev){
  if(!ev || !ev.cam || !ev.date) return false;
  state.camId = ev.cam;
  if(state.daySel !== ev.date) state.hourBlock = -1;
  state.daySel = ev.date;
  /* 日期条是"按 state.rec 算出来的最近有录像的那些天"，事件日期一定在里面；
     万一不在（清单还没拉回来），补进去再算下标 —— 少了这一步会拿旧下标
     播另一天的同一秒。 */
  const l = (state.dayList || []).slice();
  let i = l.indexOf(ev.date);
  if(i < 0){ l.push(ev.date); l.sort(); state.dayList = l; i = l.indexOf(ev.date); }
  state.dayIdx = i;
  return true;
}

/* ±30 秒的基准：优先用「播放器最后停在哪」——用户按"后 30 秒"时看的是画面，
   不是事件条目。切到事件页会停播（pvStop），但 PPLAY.t 就停在停下的那一秒，
   正是要的基准；只有还没在这条事件的片上播过（换过片 / 没点开）才退回定位入点。 */
function evBasePoint(ev, clip){
  if(EV.focus === ev.id && PPLAY.segIdx >= 0
     && PPLAY.day === ev.date && PPLAY.camId === ev.cam) return PPLAY.t;
  return evClipStart(clip);
}
function evPlayPoint(ev, clip, delta){
  const base = evBasePoint(ev, clip);
  if(base == null) return null;
  return Math.max(0, Math.min(86399, base + (+delta || 0)));
}

function evSeek(ev, t){
  if(!(t >= 0)) return false;
  evTargetState(ev);
  pvSeekTo = {t: t, at: Date.now()};
  if(activePage() === "play") pvAutoEnter(true);
  else goPage("play");
  return true;
}

/* 点卡片：先问定位，能播就跳；播不了留在卡片上把原因说清（不跳页、不给黑框）。 */
function evOpen(id){
  const ev = evFind(id);
  if(!ev) return Promise.resolve(null);
  if(EV.open === id){                       /* 再点一次收起 */
    EV.open = ""; EV.clip = null; EV.clipErr = "";
    renderEvList();
    return Promise.resolve(null);
  }
  EV.open = id; EV.focus = id; EV.clip = null; EV.clipErr = "";
  renderEvList();
  return API.get("/api/events/" + encodeURIComponent(id) + "/clip").then(d=>{
    if(EV.open !== id) return null;         /* 用户已经点了别的 */
    if(!d || !d.ok){
      EV.clipErr = (d && d.error) || "服务端没有返回定位信息";
      renderEvList();
      return null;
    }
    EV.clip = d;
    renderEvList();
    if(d.missing_reason) return d;
    evSeek(ev, evClipStart(d));             /* 入点 = 分片起点 + 偏移（事件前 10 秒） */
    return d;
  }).catch(e=>{
    if(EV.open === id){
      EV.clipErr = String((e && e.message) || e);
      renderEvList();
    }
    return null;
  });
}

function evGo(delta){
  const ev = evFind(EV.focus || EV.open);
  if(!ev) return false;
  if(!EV.clip || EV.clip.missing_reason){
    toast("toast2", (EV.clip && EV.clip.missing_text) || "还没拿到这段的定位");
    return false;
  }
  const t = evPlayPoint(ev, EV.clip, delta);
  if(t == null) return false;
  return evSeek(ev, t);
}

(function(){
  const list = document.getElementById("evList");
  if(list) list.onclick = e=>{
    const t = e.target;
    const g = (t && t.closest) ? t.closest("[data-evgo]") : null;
    if(g){ e.stopPropagation(); evGo(+g.getAttribute("data-evgo")); return; }
    const c = (t && t.closest) ? t.closest("[data-ev]") : null;
    if(c) evOpen(c.getAttribute("data-ev"));
  };
  const rf = document.getElementById("btnEvRefresh");
  if(rf) rf.onclick = ()=>{ loadEvents(true); };
  /* 「更多筛选」的两个时间框与最短时长：用 change（失焦/回车）而不是 input，
     否则每敲一个数字就发一次请求 —— 服务端一次要按天跑指纹同步，不是免费的。
     最短时长只收非负整数秒：小数在这里没有意义（事件精度就是秒）。 */
  const bind = (id, key, num)=>{
    const el = document.getElementById(id);
    if(!el) return;
    el.onchange = ()=>{
      let v = String(el.value || "").trim();
      if(num) v = v.replace(/[^\d]/g, "");
      EV[key] = v;
      evReload();
    };
  };
  bind("evFrom", "from", false);
  bind("evTo", "to", false);
  bind("evMinDur", "minDur", true);
})();
