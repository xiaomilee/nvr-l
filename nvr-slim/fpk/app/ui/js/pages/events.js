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
            loading: false, open: "", focus: "", clip: null, clipErr: "",
            box: {}, zoom: "", campop: false};

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

/* 缩略图 onerror：拿不到图（404 / 没登录 / 断网）就把整个缩略图容器藏掉。
   0.9.138 起图外面套了一层定比例的容器（.evthumbbox），只藏 <img> 会留下
   一个灰块，比"没有图"更难看，也更容易被当成页面坏了。
   用内联属性而不是 addEventListener：这些卡片是 innerHTML 一次性生成的，
   逐个绑定会在 200 条列表上多 200 次查询。 */
const EV_IMG_ONERR = "this.parentNode.style.display='none'";

/* 展开区那张大图失败时的处理与缩略图**故意不同**：
   列表里 200 张缩略图挨个显示"破图"会把页面变成一片灰块，所以缩略图失败就藏掉；
   但展开区是用户点开"要看这一眼"的地方，图没了必须说出来 —— 弱网下最常见的
   就是图还没下完（`loading=lazy` + 慢网），静默藏掉会让人以为这条事件根本没快照。
   这里只给容器加个 class，文案交给 CSS 的 ::after，不在内联属性里拼中文。 */
const EV_SHOT_ONERR = "this.parentNode.classList.add('bad')";

/* 快照那一眼（0.9.129）。三件事：
   - 图从**服务端给的 url** 来，前端不拼（与 clip 同一套契约）；
   - 说明写"快照 HH:MM:SS · 事件开始后 N 秒"，N 用服务端算好的 offset，
     前端不重新减（两处算早晚会不一致）；
   - 没有图就如实说原因，并且明确"这不是播放失败" —— 老事件本来就没存，
     不该让用户以为坏了。 */
function evShotWhen(sh){
  const off = (sh && typeof sh.offset === "number") ? sh.offset : null;
  let when = "快照 " + esc((sh && sh.at) || "");
  if(off !== null){
    const n = Math.round(Math.abs(off));
    when += off >= 0 ? " · 事件开始后 " + n + " 秒" : " · 事件开始前 " + n + " 秒";
  }
  return when;
}

/* 检测框叠层（0.9.130）：服务端给的是归一化 cx,cy,w,h，这里换成百分比定位。

   为什么要叠框：缩略图只看得见"有人"，看不出"检测器框住的是不是人"。框来自
   那次推理本身（与这张图同一拍），不是事后补画的 —— 所以位置天然是对的。

   为什么在前端画：服务端画框要解码 + 重编码一张 720p 图，只为几条线；浏览器
   用几个绝对定位的 <i> 就能画，还能随图缩放（缩略图浮层与展开区共用一份数据）。 */
function evBoxPct(v){ return Math.max(0, Math.min(100, (+v) * 100)).toFixed(2) + "%"; }
function evBoxHtml(boxes){
  return (boxes || []).map(b=>{
    const p = (b && b.b) || [];
    const w = +p[2] || 0, h = +p[3] || 0;
    const label = evCatText(b && b.c)
      + (b && typeof b.s === "number" ? " " + b.s.toFixed(2) : "");
    return '<i class="evbox ' + evCatCls(b && b.c) + '" style="left:' + evBoxPct((+p[0] || 0) - w / 2)
      + ';top:' + evBoxPct((+p[1] || 0) - h / 2)
      + ';width:' + evBoxPct(w) + ';height:' + evBoxPct(h) + '">'
      + '<b>' + esc(label) + '</b></i>';
  }).join("");
}
/* 框层：只在这一条真的读到框时渲染（读过但为空 = 没框，什么都不画）。 */
function evBoxLayer(id){
  const boxes = EV.box[id];
  return (boxes && boxes.length) ? evBoxHtml(boxes) : "";
}

function evShotHtml(ev){
  const sh = (ev && ev.shot) || null;
  if(!sh || !sh.url)
    return '<div class="evshotcap">这条没有留快照（0.9.129 之前的事件不存图，'
      + '或检测当时还没确认）。</div>';
  return '<div class="evshotwrap">'
    + '<img class="evshot" src="' + esc(sh.url) + '" alt="" loading="lazy"'
    + ' decoding="async" data-evzoom="' + esc(ev.id) + '" title="点开看大图"'
    + ' onerror="' + EV_SHOT_ONERR + '">'
    + evBoxLayer(ev.id)
    + '</div>'
    + '<div class="evshotcap">' + evShotWhen(sh) + '</div>';
}

/* 卡片右侧那张缩略图（0.9.130 起可点）：点它先看大的，再决定要不要点开回放。
   以前它 pointer-events:none、点了就跳回放 —— 用户只是想看清"那是人还是影子"
   的时候，代价是一部录像的 seek 与解码。

   0.9.138 起不再写死 76×43：外层 .evthumbbox 用 aspect-ratio 定比例、
   align-self:stretch 把高度拉满卡片内容高，宽度按 16:9 自动跟随 —— 比例
   永远不变，高窄屏都成立。图用绝对定位铺满容器：这样容器不参与内容高计算，
   不会把 720p 的原始高度（几百 px）带进卡片布局。
   缩略图只在真有图时出现：没有就不占位 —— 一屏 200 条里大多数是
   0.9.129 之前的老事件，塞满灰块等于把列表变成一片占位符。 */
function evThumbHtml(ev){
  if(!ev || !ev.shot || !ev.shot.url) return "";
  return '<div class="evthumbbox">'
    + '<img class="evthumb" src="' + esc(ev.shot.url) + '" alt="" loading="lazy"'
    + ' decoding="async" data-evzoom="' + esc(ev.id) + '" title="点开看大图"'
    + ' onerror="' + EV_IMG_ONERR + '">'
    + '</div>';
}

/* 打开一条事件后展开的内容（0.9.132 起分两段）。
   播不了时把服务端的中文原因摆出来 —— 需求里"点击直接播放"的前提是这一段
   真的还在，不在就得说清是哪一种不在。

   为什么要拆成两段（用户原话：「把『事件快照』和『录像回放』的操作分开，让
   用户先快速确认，再决定是否打开视频」）：以前图上并排三个按钮、主按钮就是
   「打开回放」，等于把"确认那一眼"和"看全程"压成同一次点击。手机上打开一段
   录像的代价是 seek + 解码 + 流量，而十次里有九次用户只想确认"那是人还是影子"。

   所以：第一段只有图（可点放大），第二段只有一个主按钮，并且把"要花多久"
   写在按钮上 —— 用户得知道自己点下去要付什么代价。前/后 30 秒降为次要，
   它们是"已经在看录像之后"的动作，不该和"要不要看"并列。 */
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
  const sec = Math.round(+((ev && ev.duration) || 0));
  return note + '<div class="evconfirm">'
      + evShotHtml(ev)
      + ((ev && ev.shot && ev.shot.url)
          ? '<div class="evcfm">点图可放大 · 先看清，再决定要不要打开录像</div>' : "")
    + '</div>'
    + '<div class="evacts">'
      + '<button class="evbtn main wide" data-evgo="0">▶ 打开录像'
        + (sec > 0 ? '（约 ' + sec + ' 秒）' : '') + '</button>'
      + '<button class="evbtn sub" data-evgo="-30">◀ 前 30 秒</button>'
      + '<button class="evbtn sub" data-evgo="30">后 30 秒 ▶</button>'
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
  /* 0.9.138 结构调整：卡片改成"上下两行"——
     第一行：色条 + 时间/机位/标签 + 缩略图；第二行（展开时）：详情整卡宽。
     以前详情塞在左列里，宽度被右侧缩略图列压掉 90px，大图右边永远空一条；
     标签挂在缩略图下面又把右列撑高。现在标签归左列（文字块的一部分），
     缩略图直接做卡片子项把高度拉满，详情独占一行铺满整卡宽。 */
  return '<div class="evcard" data-ev="' + esc(ev.id) + '">'
    + '<i class="evstrip ' + evMainCat(ev) + '"></i>'
    + '<div class="evmain">'
      + '<div class="evtime mono">' + gfmtClock(ev.start) + '</div>'
      + '<div class="evmeta">' + esc(cam) + " · " + gfmtDur(ev.duration) + conf + '</div>'
      + '<div class="evtags">'
        + cats.map(k =>
          '<span class="evtag ' + evCatCls(k) + '">' + esc(evCatText(k)) + '</span>').join("")
      + '</div>'
    + '</div>'
    + evThumbHtml(ev)
    + (open ? '<div class="evopen">' + evOpenHtml(ev) + '</div>' : "")
  + '</div>';
}

/* ---------------- 放大看（0.9.130） ----------------
   浮层只做三件事：把图放到尽可能大、把检测框叠回去、给一个"打开回放"的出口。
   刻意不做手势缩放 / 拖动 / 分享：这一版的目的是"看清那一眼"，不是做看图器；
   页面上已经有回放页承接"要看完整过程"的需求。 */
function evCamName(ev){
  return ((CAMERAS.find(c => c.id === ev.cam) || {}).name) || ev.cam || "—";
}
function evZoomHtml(ev){
  const sh = ev.shot || {};
  const boxes = EV.box[ev.id];
  const boxNote = (boxes === undefined || boxes === null)
    ? " · 正在读检测框…"
    : (boxes.length ? " · 框出 " + boxes.length + " 个目标" : " · 这张没有存检测框");
  return '<div class="evzoombox">'
    + '<div class="evzoomwrap">'
      + '<img class="evzoomimg" src="' + esc(sh.url) + '" alt=""'
      + ' onerror="' + EV_SHOT_ONERR + '">'
      + evBoxLayer(ev.id)
    + '</div>'
    + '<div class="evzoomcap"><b>' + esc(evCamName(ev)) + ' · '
      + gfmtClock(ev.start) + '</b> · ' + evShotWhen(sh) + boxNote + '</div>'
    + '<div class="evzoombar">'
      + '<button class="evbtn main" data-evzoomplay="' + esc(ev.id) + '">▶ 打开录像</button>'
      /* 竖屏那两个按钮（0.9.132）：16:9 的横图按屏宽铺满只有 96vw 宽，
         高度自然只有屏高的 1/3，人脸和车牌都看不清。旋转 90° 后图的长边
         变成屏幕的高，能占到屏高 65% 以上 —— 这是手机看横图最省事的办法，
         比让用户自己横过手机再关掉方向锁定可靠。 */
      + '<button class="evbtn" data-evzoomrot="1" title="竖屏时横过来看">⟳ 横过来看</button>'
      + '<button class="evbtn" data-evzoomzoom="1" title="放大一档">＋ 放大</button>'
      + '<button class="evbtn" data-evzoomclose="1">关闭</button>'
    + '</div></div>';
}
/* 浮层根节点建一次就复用：每次点图都新建 DOM 会让 document 上堆一串死节点。
   「横过来看」与「放大」互斥：一个是把图转 90°，一个是让图超出容器可滚动，
   同时开会让 transform 与滚动叠在一起，用户拖半天不知道该往哪拖。 */
function evZoomRoot(){
  let el = document.getElementById("evZoomRoot");
  if(el) return el;
  el = document.createElement("div");
  el.id = "evZoomRoot";
  el.onclick = e=>{
    const t = e.target;
    if(!t || !t.closest) return;
    const play = t.closest("[data-evzoomplay]");
    if(play){
      const id = play.getAttribute("data-evzoomplay");
      evZoomClose();
      /* 关浮层后展开并取定位，再走「打开录像」逻辑跳回放（evGo→evSeek）。
         不直接 evSeek 是因为 clip 可能还没拉（比如从缩略图进浮层时没开过卡片），
         让 evOpen 把定位拿到、evGo 自己判就绪再跳。 */
      evOpen(id).then(()=>evGo(0));
      return;
    }
    const wrap = () => el.querySelector(".evzoomwrap");
    const rot = t.closest("[data-evzoomrot]");
    if(rot){
      const w = wrap();
      if(w){
        const on = w.classList.toggle("rot");
        w.classList.remove("z2");    /* 互斥：转过来就不再放大 */
        rot.setAttribute("aria-pressed", on ? "true" : "false");
      }
      return;
    }
    const zm = t.closest("[data-evzoomzoom]");
    if(zm){
      const w = wrap();
      if(w){
        const on = w.classList.toggle("z2");
        w.classList.remove("rot");
        zm.setAttribute("aria-pressed", on ? "true" : "false");
      }
      return;
    }
    /* 点图本身不关（用户可能正对着框看）；点背景、点"关闭"才关。 */
    if(t.closest("[data-evzoomclose]") || t === el) evZoomClose();
  };
  document.body.appendChild(el);
  return el;
}
function renderEvZoom(){
  const root = evZoomRoot();
  const ev = EV.zoom ? evFind(EV.zoom) : null;
  root.className = ev ? "evzoom" : "";
  root.innerHTML = ev ? evZoomHtml(ev) : "";
}
/* 框按需读一次（?meta=1），结果按事件 id 缓存：
   同一张图来回看几遍不该反复问服务端；`undefined` = 还没问过，
   `null` = 正在问，`[]` = 真没有框（三种状态不能混成一个）。 */
function evLoadBoxes(id){
  if(EV.box[id] !== undefined) return Promise.resolve(EV.box[id]);
  const ev = evFind(id);
  if(!ev || !ev.shot) return Promise.resolve(null);
  EV.box[id] = null;
  return API.get(ev.shot.url + "?meta=1").then(d=>{
    EV.box[id] = (d && d.ok && Array.isArray(d.boxes)) ? d.boxes : [];
  }).catch(()=>{ EV.box[id] = []; }).then(()=>{
    if(EV.zoom === id) renderEvZoom();
    if(EV.open === id) renderEvList();
    return EV.box[id];
  });
}
function evZoomOpen(id){
  const ev = evFind(id);
  if(!ev || !ev.shot) return false;
  EV.zoom = id;
  renderEvZoom();                    /* 先出图：图有浏览器缓存，不等框 */
  evLoadBoxes(id);
  return true;
}
function evZoomClose(){
  if(!EV.zoom) return false;
  EV.zoom = "";
  renderEvZoom();
  return true;
}

/* 加载骨架（0.9.132）：弱网下"正在读事件台账…"一行字要等十几秒，页面上再没有
   别的东西在动，用户分不清是在读还是卡死了。骨架条只是灰块，不做扫光动画 ——
   慢网里飘来飘去的动画比静止更容易被当成"还在加载所以要等"。
   文案保留原句（老用户认得这句），骨架垫在它下面。 */
function evSkeleton(n){
  let out = "";
  for(let i = 0; i < n; i++)
    out += '<div class="evsk"><i></i><span></span><b></b></div>';
  return out;
}

function renderEvList(){
  const el = document.getElementById("evList");
  if(!el) return;
  let body;
  if(EV.err){
    /* 失败要有可点的出口：以前写"点右上角重新读一次"，用户得先找到那个图标。
       弱网下最常见的失败是翻页/切筛选时超时，此时他就在这一屏，重试钮该在这。 */
    body = '<div class="evfail"><b>读取失败</b>'
      + '<span>' + esc(EV.err) + '</span>'
      + '<button class="evbtn main" data-evretry="1">重试</button>'
      + '<i class="evfailhint">检查一下手机和 NAS 是不是同一个网络</i></div>';
  }else if(!EV.data){
    body = '<div class="eveof">正在读事件台账（首次会按台账同步这一天的索引）…</div>'
      + evSkeleton(EV.loading ? 5 : 2);
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
  evZoomClose();                      /* 换了条件，浮层里那张图也过期了 */
  renderEvChips(); renderEvHead(); loadEvents(true);
}

/* ---------------- 机位弹层（0.9.132） ----------------
   弹层内容每次打开时重算：机位列表可能刚在设置页里改过名、增删过路数。
   选完不关：多选要在同一个弹层里连着点，点一下就关等于让人反复开合。 */
function evCamPopHtml(){
  const opts = [{id: "", name: "全部机位"}].concat(
    (CAMERAS || []).map(c => ({id: c.id, name: c.name || c.id})));
  const all = EV.cams.length === 0;
  return '<div class="evcamt"><b>按机位筛选</b><span>可多选，同一次只看这些</span></div>'
    + '<div class="evcamg">' + opts.map(o=>{
        const on = o.id ? EV.cams.indexOf(o.id) >= 0 : all;
        return '<button class="evcamb' + (on ? " on" : "") + '" data-evname="'
          + esc(o.id) + '">' + esc(o.name) + '</button>';
      }).join("") + '</div>'
    + '<div class="evcamf"><button class="evbtn main" data-evcamclose="1">完成</button></div>';
}

function evCamPopOpen(on){
  const pop = document.getElementById("evCamPop");
  const mask = document.getElementById("evCamMask");
  if(!pop) return false;
  if(on){
    pop.innerHTML = evCamPopHtml();
    pop.hidden = false;
    if(mask) mask.hidden = false;
    EV.campop = true;
  }else{
    pop.hidden = true;
    if(mask) mask.hidden = true;
    EV.campop = false;
  }
  return EV.campop;
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
    /* 机位筛选从"横向滚动的 chips"改成"一个按钮 + 底部弹层"（0.9.132）。
       为什么：8 路机位在 375 宽的屏上是 900 多像素的横条，要横滑三四屏才能
       找到最后一路，而横向滚动条在手机上是看不见的 —— 用户根本不知道右边还有。
       弹层是 2 列网格，一眼看全；多选就在弹层里连续点，不用反复开合。
       按钮上写"全部机位"或"3 个机位"：不打开弹层也知道现在筛了什么。 */
    const n = EV.cams.length;
    camBar.innerHTML = '<button class="evchip on" data-evcampop="1">'
      + (n ? n + " 个机位" : "全部机位") + ' ▾</button>'
      + (n ? '<button class="evchip" data-evcamclear="1">清除机位</button>' : "");
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

/* 点卡片 = 展开详情（两段式）：先取定位（决定能不能播、播不了说清原因），
   但**不跳转** —— 跳回放是详情里「打开录像」按钮的事。 */
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
  /* 展开区的宽图也叠框：与浮层共用同一份缓存，先问过的就不再问。 */
  evLoadBoxes(id);
  return API.get("/api/events/" + encodeURIComponent(id) + "/clip").then(d=>{
    if(EV.open !== id) return null;         /* 用户已经点了别的 */
    if(!d || !d.ok){
      EV.clipErr = (d && d.error) || "服务端没有返回定位信息";
      renderEvList();
      return null;
    }
    EV.clip = d;
    renderEvList();
    /* 0.9.132 修正：这里**只取定位、不跳转**。
       打开事件 = 展开详情（图 + 「打开录像」按钮）；真正跳回放的是详情里的
       主按钮（data-evgo）或快照浮层的「打开录像」（data-evzoomplay），由 evGo
       走 evSeek。以前这里顺手 evSeek，等于点开事件就直接进回放，把"先看清
       那一眼"的两段式设计架空了。播不了（missing_reason）也只是留在卡片上
       说清原因，不跳。 */
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
    /* 缩略图先接管：点图 = 看大图（不跳页）。放在 data-evgo 与卡片之前判断，
       否则同一个点击会先被卡片接走，用户还是会被弹去回放。 */
    const z = (t && t.closest) ? t.closest("[data-evzoom]") : null;
    if(z){ e.stopPropagation(); evZoomOpen(z.getAttribute("data-evzoom")); return; }
    /* 失败态里的重试：必须在卡片判断之前（失败时列表里没有卡片，但顺序
       写反了将来加了别的东西就会莫名被卡片吃掉）。 */
    const rt = (t && t.closest) ? t.closest("[data-evretry]") : null;
    if(rt){ e.stopPropagation(); loadEvents(true); return; }
    const g = (t && t.closest) ? t.closest("[data-evgo]") : null;
    if(g){ e.stopPropagation(); evGo(+g.getAttribute("data-evgo")); return; }
    const c = (t && t.closest) ? t.closest("[data-ev]") : null;
    if(c) evOpen(c.getAttribute("data-ev"));
  };
  /* Esc 关浮层：手机上没键盘，但桌面端按 Esc 是最省事的"退出去"。 */
  document.addEventListener("keydown", e=>{
    if((e.key === "Escape" || e.keyCode === 27) && EV.zoom) evZoomClose();
  });
  const rf = document.getElementById("btnEvRefresh");
  if(rf) rf.onclick = ()=>{ loadEvents(true); };
  /* 机位弹层的开合与选择：全走 document 代理。
     弹层内容是每次打开时重算的，逐个绑定会在每次重算后留下一批死监听。 */
  document.addEventListener("click", e=>{
    const t = e.target;
    if(!t || !t.closest) return;
    if(t.closest("[data-evcampop]")){ evCamPopOpen(!EV.campop); return; }
    if(t.closest("[data-evcamclose]") || t.id === "evCamMask"){ evCamPopOpen(false); return; }
    if(t.closest("[data-evcamclear]")){ EV.cams = []; evCamPopOpen(false); evReload(); return; }
    const nm = t.closest("#evCamPop [data-evname]");
    if(nm){
      const id = nm.getAttribute("data-evname");
      if(id) evToggle(EV.cams, id); else EV.cams = [];
      evReload();                       /* 列表立刻跟着变，弹层保持打开 */
      evCamPopOpen(true);               /* 重画弹层的选中态（evReload 不动弹层） */
    }
  });
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
