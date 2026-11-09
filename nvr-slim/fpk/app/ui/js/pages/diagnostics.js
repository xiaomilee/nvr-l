/* ================= pages/diagnostics.js =================
   诊断页与日志：存储卡（按需统计，不实时）、每路录制状态与就地诊断、
   环境自检、运行日志、维护动作、录像残片、AI 检测诊断卡。
   它们连起来只回答一个问题：**现在到底哪里不对、证据在哪**。
   判断标准：用来查故障、看日志、读真实资源状态的渲染与动作放这里；
   把配置项画成卡片是 settings_cards.js，改配置项是 settings_editor.js，
   这两件事都不进这个文件（诊断只读，不该有"保存"按钮）。
   为什么单独成文件：诊断数据来自 /api/status、/api/link、/api/health 等
   多个口径，一旦混进设置页，最容易把"估值"当成"实测值"显示出来 ——
   评审点名的正是这类问题，摆在一起才看得清每个数字出自哪个接口。
   ======================================================== */

/* ================= 诊断与日志（独立的分区） =================
   三块内容整体从别处搬进来：
     · 直播页底部那条「存储 / 目录」→ 存储卡（按需统计，不实时）
     · 通用设置里的「每路录制状态」 → 每路一行 + 就地诊断
     · 环境自检 / 运行日志 / 维护动作 */
const STORAGE = { at:0, data:null, loading:false, err:"" };

function fmtBytes(b){
  b = +b || 0;
  if(b >= 1099511627776) return (b/1099511627776).toFixed(b >= 10995116277760 ? 0 : 1) + " TB";
  if(b >= 1073741824) return (b/1073741824).toFixed(b >= 10737418240 ? 0 : 1) + " GB";
  if(b >= 1048576) return Math.round(b/1048576) + " MB";
  if(b >= 1024) return Math.round(b/1024) + " KB";
  return b + " B";
}

/* 0.9.118：每路的细分状态由后端算（hstate），这里只做兜底。
   以前前端拿 c.state 判「在不在录」，而那个值是 fs 的「进程还在」——
   真机断流时 ffmpeg 进程照样活着（poll() 还是 None），界面就一直写「在录」，
   用户在时间轴上看到的是满的、点进去播到一半才断。 */
function recHState(c){
  if(c && c.hstate) return String(c.hstate);
  return (c && c.state === "recording") ? "recording" : "stopped";
}

function diagSummary(){
  const rec = LK.rec;
  const ch = (rec && Array.isArray(rec.channels)) ? rec.channels : [];
  const on = ch.filter(c=>recHState(c) === "recording").length;
  const bad = ch.filter(c=>{
    const s = recHState(c);
    return s === "stalled" || s === "error";
  }).length;
  const free = REC.diskFreeGB;
  /* AI 检测摘要（0.9.60）：没开就不提；开了但不可用要给个醒（0.9.122：
     统一走 detStateZh，不再自己判 running/loaded，也不再只写"未运行"） */
  let det = "";
  if(LK.detEnable){
    const st = DET.st;
    const z = detStateZh(st);
    if(z.warn) det = " · <span style='color:var(--warn)'>AI " + esc(z.t) + "</span>";
    else if(st && st.state !== "starting" && st.loaded){
      const cams = st.per_cam || {};
      let hits = 0;
      Object.keys(cams).forEach(id=>{
        const cl = cams[id].cls || {};
        Object.keys(cl).forEach(k=>{ hits += cl[k]; });
      });
      const unk = detUnknownOf(st);
      det = " · AI 命中 " + hits + " 次"
        + (unk ? " · <span style='color:var(--warn)'>未知 " + unk + "</span>" : "");
    }
  }
  return (ch.length ? (on + "/" + ch.length + " 路在录") : "录制状态未上报")
    + " · 剩余 " + (free == null ? "—" : fmtBytes(free * 1073741824))
    + (bad ? " · " + bad + " 路异常" : " · 无异常") + det;
}

function renderDiagZone(){
  let h = '<div class="zone diag">'
    + '<div class="zonehead" data-diagtoggle="1">'
    + '<span class="zi">◎</span>'
    + '<div><b>诊断与日志</b><div class="zs">存储占用 · 每路录制 · 环境 · AI 检测 · 日志</div></div>'
    + '<span class="chev">' + (SET.diagOpen ? "收起 ⌃" : "展开 ⌄") + '</span></div>';
  if(SET.diagOpen){
    /* 缺口要扫当天分片目录，进诊断页才拉，且 60 秒内不重复拉 */
    if(!GAPS.data && !GAPS.loading && !GAPS.err) loadGaps(false);
    h += cardStorage() + cardRecCams() + cardGaps() + cardIngest() + cardEnv() + cardParts()
       + cardDetectDiag() + cardLog() + cardMaint();
  }else{
    h += '<div class="gcard genfold"><div class="genline" id="diagSum">' + diagSummary() + '</div>'
      + '<button class="minibtn" data-diagtoggle="1">查看</button></div>';
  }
  return h + '</div>';
}

function cardStorage(){
  const d = STORAGE.data;
  let body;
  if(!d){
    body = '<div class="stload">' + (STORAGE.err
      ? ('统计失败：' + esc(STORAGE.err)
        + '<button class="btnmini" id="btnStRefresh" style="margin:6px 0 0">立即重算</button>')
      : '正在统计各摄像头占用…（只扫目录元数据，不读文件内容）') + '</div>';
  }else{
    const full = +d.full || 0, arch = +d.archive || 0, clip = +d.clip || 0;
    const ledger = +d.ledger || 0;
    const used = full + arch + clip;
    const denom = Math.max(used, 1);
    const fp = used ? Math.round(full / denom * 100) : 0;
    const ap = used ? Math.round(arch / denom * 100) : 0;
    const totalB = (d.disk_total_gb || 0) * 1073741824;
    const freeB = (d.disk_free_gb != null ? d.disk_free_gb : REC.diskFreeGB || 0) * 1073741824;
    const rows = (d.cameras || []).slice().sort((a,b)=>b.total - a.total).map(c=>{
      const t = +c.total || 0;
      const f = t ? Math.round((c.full || 0) / t * 100) : 0;
      const a2 = t ? Math.round((c.archive || 0) / t * 100) : 0;
      return '<div class="strow">'
        + '<span class="nm">' + esc(c.name) + '</span>'
        + '<span class="stbar">' + (t
            ? '<i class="f" style="width:' + f + '%"></i><i class="c" style="width:' + a2 + '%"></i>'
            : '') + '</span>'
        + '<span class="vv">' + (t ? fmtBytes(t) : "—")
          + '<em>' + (t ? (fmtBytes(c.full || 0) + " / " + (c.archive ? fmtBytes(c.archive) : "—")) : "无录像") + '</em></span>'
        + '</div>';
    }).join("");
    const at = d.at ? new Date(+d.at * 1000) : null;
    body = '<div class="sthead">'
        + '<span class="mono big">' + (used ? fmtBytes(used).replace(/ ?(TB|GB|MB)$/, "") : "0") + '</span>'
        + '<span class="unit">' + (used ? (fmtBytes(used).replace(/^[\d.]+ ?/, "") + " 已用") : "已用")
          + (totalB ? (" / 共 " + fmtBytes(totalB)) : "") + '</span>'
        + '<span class="rt">剩余 ' + (freeB ? fmtBytes(freeB) : "—") + '</span>'
      + '</div>'
      + '<div class="stbar-big"><i class="f" style="width:' + fp + '%"></i><i class="c" style="width:' + ap + '%"></i></div>'
      + '<div class="legend">'
        + '<span><i style="background:var(--primary)"></i>全时 <b>' + fmtBytes(full) + ' · ' + fp + '%</b></span>'
        + '<span><i style="background:var(--arch)"></i>归档 <b>' + fmtBytes(arch) + ' · ' + ap + '%</b></span>'
      + '</div>'
      + '<div class="dline" style="margin-top:9px">活动台账 <b>' + fmtBytes(ledger)
        + '</b>（永久保留 · 几乎不占空间）</div>'
      + (clip > 1048576 ? '<div class="dline">缩时残留 <b>' + fmtBytes(clip)
        + '</b>（功能已下线，可手动删除 <code class="mono" style="font-size:10.5px">_clip</code> 目录回收）</div>' : '')
      + '<div style="margin-top:11px;padding-top:10px;border-top:1px solid var(--line2)">'
        + '<div style="display:flex;align-items:baseline;font-size:10.5px;color:var(--faint)">'
          + '<span>每路占用</span><span style="margin-left:auto">合计 · 全时 / 归档</span></div>'
        + (rows || '<div class="stload">还没有摄像头</div>')
      + '</div>'
      + '<div class="dline" style="margin-top:9px">' + recBudgetText() + '</div>'
      + '<div class="dline">保留策略 <b>' + keepPolicyText() + '</b></div>'
      + '<div class="dline">循环覆盖 <b>超出后按日期整目录删最旧的</b></div>'
      + '<div class="pathchip"><code class="mono">' + esc(d.root || REC.dir) + '</code><span>录像根目录</span></div>'
      + '<div class="stnote"><span>按「摄像头 / 日期」目录汇总，每 30 分钟最多重算一次；'
        + '只有打开本页且缓存过期才扫盘，直播与回放不触发。'
        + (at ? ('统计于 ' + pad(at.getHours()) + ":" + pad(at.getMinutes()) + ' · 非实时') : '')
        + '</span><button class="btnmini" id="btnStRefresh">立即重算</button></div>';
  }
  return '<div class="dcard"><div class="dh">存储与占用'
    + '<span class="tg">非实时 · 按需统计</span>'
    + '<span class="rt">' + (STORAGE.loading ? "统计中…" : "全时 + 归档") + '</span></div>'
    + body + '</div>';
}

function keepPolicyText(){
  const rng = a => !a.length ? "—" : Math.min(...a) === Math.max(...a)
    ? Math.min(...a) + " 天"
    : Math.min(...a) + "~" + Math.max(...a) + " 天";
  const ps = CAMERAS.map(c=>camParams[c.id]).filter(Boolean);
  if(!ps.length) return "无摄像头";
  /* 三层寿命：台账永久 > 归档 N 天 > 全时各路自定。归档期用全局值展示
     （每路覆盖在后端生效，界面只报全局口径，避免一处显示两套数） */
  return "全时 " + rng(ps.map(p=>p.fullDays)) + " / 归档 " + (LK.archDays || 7)
    + " 天 / 台账永久";
}

function loadStorageUsage(force){
  if(STORAGE.loading) return Promise.resolve(STORAGE.data);
  /* 失败过就不再自动重试：否则「渲染 → 请求 → 失败 → 再渲染」会打成一个环，
     用户看到页面一直在闪。要重试由用户点「立即重算」。 */
  if(!force && STORAGE.err) return Promise.resolve(null);
  if(!force && STORAGE.data && (Date.now()/1000 - STORAGE.at) < 1800) return Promise.resolve(STORAGE.data);
  STORAGE.loading = true;
  STORAGE.err = "";
  const url = force ? "/api/storage/usage/refresh" : "/api/storage/usage";
  /* 强制重算是真要扫盘（du 走一遍录像目录），15 秒默认超时对几万个分片不够 */
  const req = force
    ? API.post(url, {}, { timeout: 300000 })
    : API.get(url, { timeout: 60000 });
  return req.then(d=>{
    if(d && d.ok){
      STORAGE.data = d;
      STORAGE.at = +d.at || (Date.now()/1000);
      if(d.disk_free_gb != null) REC.diskFreeGB = +d.disk_free_gb;
      if(d.disk_total_gb != null) REC.diskTotalGB = +d.disk_total_gb;
    }else{
      STORAGE.err = (d && d.error) || "服务端没有返回统计结果";
    }
    return STORAGE.data;
  }).catch(e=>{
    STORAGE.err = String(e && e.message || e);
    return STORAGE.data;
  }).finally(()=>{
    STORAGE.loading = false;
    const body = document.getElementById("camBody");
    if(body && SET.diagOpen && !SET.editing) renderCamTabs();
  });
}

function cardRecCams(){
  const rec = LK.rec;
  let inner;
  if(!rec || !Array.isArray(rec.channels) || !rec.channels.length){
    inner = '<div class="dline">服务端还没上报录制进程状态（/api/link 的 record.channels 为空）。'
      + '重启服务后如果仍是空的，先看下面的运行日志。</div>';
  }else if(rec.ffmpeg_missing){
    inner = '<div class="dline" style="color:var(--warn)">找不到 ffmpeg —— 录像和预览都依赖它。'
      + '把静态 ffmpeg 放到安装目录的 app/bin/ 下，或本机装好 ffmpeg 后重启应用。</div>';
  }else{
    // 失败时先说「下一步怎么办」（后端按错误分类给的中文建议），
    // ffmpeg 的英文原文收进 details —— 原文对排查有用，但对用户是天书。
    inner = rec.channels.map(c=>{
      const hs = recHState(c);
      const running = hs === "recording";
      const err = c.error ? String(c.error) : "";
      const hint = c.hint ? String(c.hint) : "";
      const hwhy = c.hreason ? String(c.hreason) : "";
      /* 卡住与异常是两种事：卡住 = 进程在跑但没输出（断流 / 相机无响应），
         异常 = 进程已经退出。都该告警，但文案必须分开 —— 否则用户会拿
         「重启一下」去处理一个其实还活着的进程。 */
      const dot = running ? "var(--live)"
        : ((hs === "stalled" || hs === "error") ? "var(--warn)" : "var(--faint2)");
      let tail;
      if(running) tail = c.last_seg ? ("在录 · 最新 " + c.last_seg) : "已连上，等第一个分片";
      else if(hwhy) tail = hwhy;
      else if(hint) tail = hint;
      else if(err) tail = err;
      else tail = "进程未运行";
      const more = (err && hint)
        ? '<details class="recmore"><summary>ffmpeg 原文</summary><code>' + esc(err) + '</code></details>'
        : "";
      return '<div class="recrow"><i style="background:' + dot + '"></i>'
        + '<b>' + esc(c.name) + '</b><span>' + esc(tail) + '</span>'
        + '<button class="recprobe" data-probe="' + esc(c.id) + '">诊断</button></div>'
        + more
        + '<div class="probeout" id="probeout-' + esc(c.id) + '" style="display:none"></div>';
    }).join("");
    if(rec.bad_opts && rec.bad_opts.length){
      inner += '<div class="dline">本机 ffmpeg 不支持的参数已自动跳过：'
        + esc(rec.bad_opts.map(x=>"-" + String(x).replace(/^-/, "")).join("、")) + '</div>';
    }
  }
  const on = (rec && Array.isArray(rec.channels))
    ? rec.channels.filter(c=>recHState(c) === "recording").length : 0;
  const stalled = (rec && Array.isArray(rec.channels))
    ? rec.channels.filter(c=>recHState(c) === "stalled").length : 0;
  const n = (rec && Array.isArray(rec.channels)) ? rec.channels.length : CAMERAS.length;
  return '<div class="dcard">'
    + '<div class="dh">每路录制状态<span class="tg">进程 / 最新分片 / 诊断</span>'
    + '<span class="rt">' + on + " / " + n + ' 在录'
    + (stalled ? (' · <span style="color:var(--warn)">' + stalled + ' 路卡住</span>') : '')
    + '</span></div>'
    + inner
    + '<div class="dline" style="margin-top:9px;font-size:10px;color:var(--faint)">'
    + '点「诊断」就地展开：ffprobe 读流 + 试录 8 秒，直接给出是摄像头还是 ffmpeg 的问题。</div>'
    + '</div>';
}

/* 录像缺口（0.9.118）：回答「今天到底录上了没有」。

   这张卡与上面「每路录制状态」不是一回事：上面说的是「此刻进程在不在写」，
   这里说的是「盘上这一天真的有连续画面吗」。真机最常见的误解就发生在这两者
   之间 —— 进程一直在跑，中间断了 19 分钟，时间轴看着还是满的。 */
const GAPS = {data:null, err:"", loading:false, at:0};

function gfmtDur(sec){
  const s = Math.max(0, Math.round(+sec || 0));
  if(s < 60) return s + " 秒";
  const m = Math.floor(s / 60);
  if(m < 60) return m + " 分" + (m < 10 && s % 60 ? (s % 60) + " 秒" : "");
  return Math.floor(m / 60) + " 时 " + (m % 60) + " 分";
}
function gfmtClock(sec){
  const s = Math.max(0, Math.round(+sec || 0));
  const p = n => (n < 10 ? "0" : "") + n;
  return p(Math.floor(s / 3600)) + ":" + p(Math.floor((s % 3600) / 60)) + ":" + p(s % 60);
}

function loadGaps(force){
  if(GAPS.loading) return Promise.resolve(GAPS.data);
  /* 失败过就不再自动重试：否则「渲染 → 请求 → 失败 → 再渲染」会打成一个环，
     页面一直闪。要重试由用户点「重新核对」。 */
  if(!force && GAPS.err) return Promise.resolve(null);
  if(!force && GAPS.data && (Date.now() / 1000 - GAPS.at) < 60) return Promise.resolve(GAPS.data);
  GAPS.loading = true;
  GAPS.err = "";
  const day = (typeof jsToday === "function") ? jsToday() : "";
  const url = "/api/health/gaps" + (day ? ("?date=" + encodeURIComponent(day) + (force ? "&force=1" : "")) : "");
  return API.get(url).then(d=>{
    if(d && d.ok){
      GAPS.data = d;
      GAPS.at = Date.now() / 1000;
    }else{
      GAPS.err = (d && d.error) || "服务端没有返回缺口数据";
    }
    return GAPS.data;
  }).catch(e=>{
    GAPS.err = String((e && e.message) || e);
    return GAPS.data;
  }).finally(()=>{
    GAPS.loading = false;
    const body = document.getElementById("camBody");
    if(body && SET.diagOpen && !SET.editing) renderCamTabs();
  });
}

function cardGaps(){
  const d = GAPS.data;
  let body;
  if(!d){
    body = '<div class="dline">' + (GAPS.err
      ? ('读取失败：' + esc(GAPS.err) + '（点「重新核对」再试一次）')
      : '正在按分片的真实时长核对今天的录像（只读目录元数据，不读文件内容）…') + '</div>';
  }else if(!d.cams || !d.cams.length){
    body = '<div class="dline">今天各路都没有超过 1 分钟的缺口。</div>';
  }else{
    const t = d.total || {};
    body = '<div class="dline">合计缺口 ' + gfmtDur(t.gap_sec) + ' · ' + (t.gap_count || 0)
      + ' 段' + (t.max_gap_sec ? (' · 最长 ' + gfmtDur(t.max_gap_sec)) : '') + '</div>'
      + d.cams.map(c=>{
          const gaps = (c.gaps || []).slice(0, 6);
          return '<div class="recrow"><i style="background:var(--warn)"></i>'
            + '<b>' + esc(c.name) + '</b><span>' + (c.gap_count || 0) + ' 段 · 共 '
            + gfmtDur(c.gap_sec) + ' · 覆盖 ' + Math.round((c.coverage || 0) * 100) + '%</span></div>'
            + gaps.map(g=>'<div class="dline" style="margin:3px 0 0 22px">'
              + gfmtClock(g.start) + ' → ' + gfmtClock(g.end)
              + '（' + gfmtDur(g.duration) + '）'
              + (g.reason ? ('<span style="color:var(--faint)"> · ' + esc(g.reason) + '</span>') : '')
              + '</div>').join('')
            + (c.gap_count > gaps.length
              ? ('<div class="dline" style="margin-left:22px;color:var(--faint)">还有 '
                 + (c.gap_count - gaps.length) + ' 段未列出（按时间排序，最长的在前）</div>') : '');
        }).join("");
  }
  return '<div class="dcard"><div class="dh">录像缺口<span class="tg">今天的实际覆盖</span>'
    + '<span class="rt"><button class="btnmini" id="btnGapsRefresh">重新核对</button></span></div>'
    + body + '</div>';
}

/* 统一取流（0.9.80）：每路一条 RTSP 连接 + 本机端口 + 消费者数。
   这张卡的用途很具体：以前出问题要分辨「是摄像头、还是某一路拉流」，
   现在连接只有一条，看 idle_sec 就知道是不是取流本身没数据。 */
function cardIngest(){
  const st = ING.st;
  if(!st || st.enabled !== true){
    return '<div class="dcard"><div class="dh">统一取流<span class="tg">每路一条连接</span>'
      + '<span class="rt">未启用</span></div>'
      + '<div class="dline">当前走旧流程：录制 / 预览 / AI 各自直连摄像头。'
      + '每 5 秒一轮、5 路约每分钟 120 次新建 RTSP 会话 —— 廉价摄像头会因此踢人。'
      + '把配置里的 ingest_enable 打开（默认开）即可切到单连接。</div></div>';
  }
  const cams = st.cams || {};
  const ids = Object.keys(cams);
  let inner;
  if(!ids.length){
    inner = '<div class="dline">取流未上报（可能还没有带 RTSP 地址的通道）。</div>';
  }else{
    inner = ids.map(id=>{
      const c = cams[id] || {};
      const fr = ING.fr && ING.fr[id] ? ING.fr[id] : null;
      const live = c.state === "live";
      const idle = c.idle_sec;
      /* c.paused = 相机没发数据但我们**故意不重启**（0.9.81）。这个状态必须
         说出来，否则用户看到「0 秒前有数据」变成「40 秒前」只会以为坏了 —— 
         实际是进程活着、会话还在，相机回来就立刻续上，比杀进程重连快 15 秒。 */
      const paused = !!c.paused;
      const dot = !live ? "var(--warn)"
        : (paused ? "var(--warn)" : (idle != null && idle > 30 ? "var(--warn)" : "var(--live)"));
      let tail = live ? ("端口 " + (c.port || "—") + " · 消费者 " + (c.clients|0)) : "重连中";
      if(live && idle != null){
        tail += paused ? (" · 停顿 " + idle + " 秒，等相机自己回来（不重启）")
                       : (" · " + idle + " 秒前有数据");
      }
      if(c.tries) tail += " · 握手重试 " + c.tries + " 次";
      /* 0.9.82：停顿长度以前一直显示 0 —— 只有"相机自己回来"那条路会记时长，
         "停到打穿耐心才重连"那条路漏记。现在两条都记，这个数才有诊断价值：
         0~10 秒=抖动噪声；接近 25 秒（耐心上限）=相机被掐且不自愈，
         重连是唯一有效手段，该考虑换传输或子码流了。 */
      if(c.pause_count) tail += " · 停顿 " + c.pause_count + " 次"
        + (c.pause_max_sec ? "（最长 " + c.pause_max_sec + " 秒）" : "")
        + (c.last_pause_sec && c.last_pause_sec !== c.pause_max_sec
            ? "，最近 " + c.last_pause_sec + " 秒" : "");
      if(fr) tail += " · 取帧 " + (fr.frames|0) + " 张/" + (fr.interval||"–") + "s";
      return '<div class="recrow"><i style="background:' + dot + '"></i>'
        + '<b>' + esc(c.name || id) + '</b><span>' + esc(tail) + '</span></div>'
        + ((c.error || (fr && fr.error))
            ? '<details class="recmore"><summary>错误原文</summary><code>'
              + esc(c.error || fr.error) + '</code></details>'
            : "");
    }).join("");
  }
  const liveN = ids.filter(id=> (cams[id]||{}).state === "live").length;
  return '<div class="dcard">'
    + '<div class="dh">统一取流<span class="tg">一条连接 → 录制 / 取帧 / AI</span>'
    + '<span class="rt">' + liveN + " / " + ids.length + ' 在流</span></div>'
    + inner
    + '<div class="dline" style="margin-top:9px;font-size:10px;color:var(--faint)">'
    + '「消费者」是接在这条流上的本机进程数（录制 + 取帧 = 2）。'
    + '它掉到 0 说明消费者都退出了；idle 一直涨说明取流没拿到数据。</div>'
    + '</div>';
}

function cardEnv(){
  return '<div class="dcard">'
    + '<div class="dh">环境自检<span class="tg">版本 / 依赖 / 像素判读</span></div>'
    + '<div class="diagwarn" id="diagWarn" style="display:none;margin-top:0"></div>'
    + '<div class="diagenv" id="diagEnv" style="margin-top:0"><span class="empty">环境自检中…</span></div>'
    + '</div>';
}

/* ---------------- 录像残片（0.9.71） ----------------
   回答一个以前只能靠手机文件管理器一屏屏数的问题：盘上还有几个没封口的
   .part、各自多大、放了多久、能不能直接封口。残片在此之前是"看不见的死账"——
   /api/record/file 的白名单只放行 HH-MM-SS.mp4，所以既不能回放、也下载不到，
   用户唯一的办法是挨个数，而且数完也不知道该删还是能救。 */
/* 0.9.111：改成折叠。残片在正常情况下本来就是“没有”，
   却占着诊断区一整块高度、每次进设置都要扫一遍目录。
   收起后默认只剩一行标题，展开才发请求（见 bindSettings 里的触发）。 */
function cardParts(){
  const open = !!SET.partsOpen;
  return '<div class="dcard">'
    + '<div class="dh partshead" data-partstoggle="1">录像残片'
      + '<span class="tg">未封口的 .part</span>'
      + '<span class="rt">' + (open ? "收起 ⌃" : "展开 ⌄") + '</span></div>'
    + (open
        ? '<div class="diagenv" id="diagParts" style="margin-top:0"><span class="empty">检查中…</span></div>'
        : '<div class="dline" style="margin-top:0;font-size:10.5px;color:var(--faint)">'
          + '分片没封口就会在这里出现（静置超过一段时间会自动重建索引）。点开才检查。</div>')
    + '</div>';
}

/* 状态口径（与 /api/record/parts 的 moov/kind 一一对应）：
     待重建 = 无 moov，需要 ffmpeg 扫 mdat 重建索引（静置够久后自动做）
     可封口 = 有 moov 只是没来得及改名（分片 MP4 下这是常态）
     已放弃 = 抢救失败改名的 .bad，内容确实没法恢复，可以删 */
async function loadDiagParts(){
  const el = document.getElementById("diagParts");
  if(!el) return;
  try{
    const d = await API.get("/api/record/parts");
    if(!d || !d.ok) throw new Error((d && d.error) || "接口无响应（服务端可能还是旧版本）");
    if(!d.count){
      el.innerHTML = "<b class='ok'>✓</b> 没有残片，所有分片都正常封口了";
      return;
    }
    el.innerHTML = '<div class="dline">共 <b>' + d.count + '</b> 个'
      + (d.bad ? '（其中已放弃 <b>' + d.bad + '</b> 个）' : '')
      + ' · 占用 <b>' + fmtBytes(d.bytes) + '</b>'
      + (d.rescuable ? ' · 待抢救 <b style="color:var(--warn)">' + d.rescuable + '</b>' : '')
      + '</div>'
      + d.items.slice(0, 30).map(x=>{
          const mb = (x.size / 1048576).toFixed(1) + " MB";
          const a = x.age_sec;
          const age = a < 120 ? (a + " 秒前")
                    : a < 7200 ? (Math.round(a / 60) + " 分钟前")
                    : (Math.round(a / 3600) + " 小时前");
          const tag = x.kind === "bad" ? "<b class='bad'>已放弃</b>"
                    : x.moov ? "<b class='ok'>可封口</b>"
                    : '<b style="color:var(--warn)">待重建</b>';
          return '<div class="dline kv"><span class="k">' + esc(x.cam) + '</span>'
            + '<span class="v">' + esc(x.name) + ' · ' + mb + ' · ' + age + " " + tag + '</span></div>';
        }).join("")
      + (d.count > 30 ? '<div class="dline">另有 ' + (d.count - 30) + ' 个未列出</div>' : '')
      + '<div class="dline">静置超过 ' + Math.round(d.grace_sec / 60)
      + ' 分钟的残片会自动重建索引并改名为 .mp4；抢救失败的改名为 .bad，可以放心删。</div>'
      + '<button class="btnline" id="btnPartsRefresh" style="margin-top:9px">重新检查</button>';
    const b = document.getElementById("btnPartsRefresh");
    if(b) b.onclick = ()=>{ el.innerHTML = '<span class="empty">检查中…</span>'; loadDiagParts(); };
  }catch(e){
    el.innerHTML = "残片检查失败：" + esc(String((e && e.message) || e));
  }
}

/* ---------------- AI 检测诊断卡（0.9.60） ----------------
   回答三个问题：① 推理到底跑起来没有（runtime/模型/平均耗时）；
   ② 每路今天抽了多少帧、命中哪些类别；③ 通知最近发了什么、队列堵没堵。
   没开检测时这张卡只留一行说明，不摆一堆 0 假装在工作。 */
function cardDetectDiag(){
  const st = DET.st;
  const nt = NT.st;
  const line = (k, v, warn) =>
    '<div class="dline kv"><span class="k">' + k + '</span><span class="v"'
    + (warn ? ' style="color:var(--warn)"' : '') + '>' + v + '</span></div>';
  /* 标题栏不再硬写「· CPU」（0.9.125）：实际建在哪个后端由引擎报回来，
     还没加载就先不写 —— 写死一个没验证过的值，正是这一版要治的病。 */
  const dv = detDevOf(st);
  const devLine = line("推理设备", esc(dv.text), dv.warn);
  let head = '<div class="dcard">'
    + '<div class="dh">AI 检测<span class="tg">YOLO11n'
    + (dv.tag ? ' · ' + esc(dv.tag) : '') + '</span></div>';
  if(!LK.detEnable){
    return head + '<div class="dline" style="font-size:10.5px;color:var(--faint)">'
      + '未开启。开启路径：通用设置 · AI 检测。开启后这里会显示推理耗时、每路命中与通知记录。</div>'
      + '</div>';
  }
  /* 0.9.122：不可用与"正在加载"分开说。旧版把两者都写成「worker 未运行」，
     用户看到就以为要重启服务；实际上首次启动要解 wheel + 载 10.7MB 模型，
     几秒钟的事。现在按后端给的 state 分三档，并且不可用时给出失败码与下一步。 */
  const z = detStateZh(st);
  if(z.warn){
    const rw0 = (st && st.runtime) || {};
    return head
      + '<div class="dline" style="color:var(--warn)">' + esc(z.t) + '</div>'
      + ((st && st.error_code) ? line("失败码", esc(String(st.error_code)), true) : "")
      + (dv.text ? devLine : "")
      + (rw0.python ? line("Python / wheel", "Python " + esc(rw0.python) + " · "
                           + esc(rw0.ort || "") + (rw0.numpy ? " · " + esc(rw0.numpy) : "")) : "")
      + '<div class="dline" style="font-size:10px;color:var(--faint)">'
      + esc(detHintOf(st)) + '</div>'
      + '</div>';
  }
  if(z.t !== "正常"){
    return head + '<div class="dline">' + esc(z.t) + '</div>'
      + '<div class="dline" style="font-size:10px;color:var(--faint)">'
      + '首次启动要解压运行时并加载模型，等几秒刷新即可；一直停在这里说明运行时没装成。</div>'
      + '</div>';
  }
  const rt = st.error
    ? line("推理运行时", esc(String(st.error).slice(0, 80)), true)
    : (st.loaded ? line("推理运行时", "onnxruntime 已加载 · 模型 yolo11n.onnx")
                 : line("推理运行时", "正在解压 / 加载运行时…"));
  /* 本机 Python 与随包 wheel 的对应关系：随包带了三套（cp311/312/313，约 99MB），
     确认 NAS 只跑其中一个版本后可以只留那一套，把包体砍到约 1/3 */
  const rw = st.runtime || {};
  const rtLine = rw.python
    ? line("Python / wheel", "Python " + esc(rw.python) + " · " + esc(rw.ort || "")
           + (rw.numpy ? " · " + esc(rw.numpy) : ""))
    : "";
  const perf = st.avg_ms ? line("平均耗时", st.avg_ms + " ms/帧 · 每路每 5 秒 1 帧")
                         : line("平均耗时", "还没有样本（等第一帧抽完）");
  /* 调度实况（0.9.122）：并发几路、队列积压多少、丢了多少、一轮多久。
     这几个数就是"检测有没有被路数拖死"的直接证据，验收时看的也是它。 */
  const sc = st.sched || {};
  const schLine = sc.workers
    ? line("调度", "并发 " + sc.workers + " 路 · 队列 " + (sc.queued|0) + "/" + (sc.queue_max|0)
           + ((sc.dropped_full|0) + (sc.dropped_stale|0)
              ? ' · <span style="color:var(--warn)">丢弃 ' + ((sc.dropped_full|0) + (sc.dropped_stale|0))
                + "（队列满 " + (sc.dropped_full|0) + " / 过期 " + (sc.dropped_stale|0) + "）</span>"
              : " · 无丢弃")
           + (sc.round_avg_ms ? " · 一轮 " + Math.round(sc.round_avg_ms) + "ms / "
              + (sc.round_cams || 1) + " 路" : ""))
    : "";
  const cams = st.per_cam || {};
  const ids = Object.keys(cams);
  let rows = "";
  if(ids.length){
    rows = '<div class="dline" style="margin-top:4px"><span class="k">每路今日</span></div>';
    ids.forEach(id=>{
      const c = cams[id];
      const cl = c.cls || {};
      const parts = [];
      if((cl.person|0) > 0)      parts.push("人 ×" + (cl.person|0));
      if((cl.two_wheeler|0) > 0) parts.push("两轮 ×" + (cl.two_wheeler|0));
      if((cl.vehicle|0) > 0)     parts.push("车 ×" + (cl.vehicle|0));
      const cam = (typeof CAMERAS !== "undefined" ? CAMERAS : []).find(x=>x && x.id === id);
      /* 未知单独一类（0.9.122）：抽帧失败/推理异常/过期丢弃都归这里。
         它不是"没人来过"，也不能并进命中数——两者并在一起，
         用户看到的数就既不是"看见了什么"，也不是"没看见多久"。 */
      const unk = c.unknown || 0;
      rows += '<div class="dline kv"><span class="k">' + esc(cam ? cam.name : id) + '</span><span class="v">'
        + "抽帧 " + (c.today || 0) + (parts.length ? " · " + parts.join(" · ") : " · 无命中")
        + (unk ? ' · <b style="color:var(--warn)">未知 ' + unk + '</b>' : "")
        + (c.skip ? " · 同帧跳过 " + c.skip : "")
        /* 只统计区挡掉的框数（0.9.128）：这不是"没人来过"，而是"有人在但不在区里"。
           不说出来，用户只会看到事件页一片安静，以为 AI 坏了 */
        + (c.outside ? " · 区外忽略 " + c.outside : "")
        + (c.last && c.last.t ? " · 最近 " + snapTime(c.last.t * 1000) : "")
        + '</span></div>';
      if(c.last_err){
        rows += '<div class="dline kv" style="color:var(--warn)"><span class="k">最近错误</span>'
          + '<span class="v">' + (c.last_code ? "[" + esc(String(c.last_code)) + "] " : "")
          + esc(String(c.last_err).slice(0, 70)) + '</span></div>';
      }
    });
  }else{
    rows = line("每路统计", "暂无（开启后第一轮抽帧完成才有数）");
  }
  let ntBlock = "";
  if(nt){
    const rec = (nt.recent || []);
    const pend = nt.pending || 0;
    ntBlock = line("通知队列", pend ? (pend + " 条待发" + (pend >= 400 ? " · 可能堵了，检查 Webhook" : ""))
                                    : "空 · 已清空")
      + (rec.length ? "" : line("通知记录", "还没有发过"));
    if(rec.length){
      ntBlock += '<div class="dline" style="margin-top:2px"><span class="k">最近发送</span></div>';
      rec.slice(0, 5).forEach(r=>{
        ntBlock += '<div class="dline kv"><span class="k">' + esc(String(r.at || "")) + '</span><span class="v">'
          + esc(String(r.cam || "")) + " · " + esc(String(r.cat || ""))
          + (r.ok ? " · 成功 " + (r.ms != null ? r.ms + "ms" : "")
                  : ' · <span style="color:var(--warn)">失败 ' + esc(String(r.err || "").slice(0, 40)) + "</span>")
          + '</span></div>';
      });
    }
  }
  return head + rt + devLine + rtLine + perf + schLine + rows + ntBlock
    + '<div class="dline" style="margin-top:8px;font-size:10px;color:var(--faint)">'
    + '取帧失败只记「未知」，不会写成"无活动"；命中类别同时写进活动台账，'
    + '回放页与活动触发都从台账读，与这里的计数同源。</div>'
    + '</div>';
}

function cardLog(){
  return '<div class="dcard">'
    + '<div class="dh">运行日志<span class="tg">密码已脱敏</span><span class="rt" id="diagLogMeta">正在读取…</span></div>'
    + '<div class="hint" style="margin:0 0 8px">画面出不来 / 录像中断 / 反复重连时先看这里 —— '
      + '快照失败、ffmpeg 报错、调度异常都记在这儿。带 <b style="color:#c98a12">[WARN]</b> 的是'
      + '「能跑但不对」，带 <b style="color:#c9432b">[ERR]</b> 的是「这块功能已经没了」。'
      + '面板默认只看最近两三小时，查更早的点「历史日志」。</div>'
    + '<div class="logbox" id="diagLog" style="margin-top:0"><pre id="diagLogText"><span class="empty">正在读取…</span></pre></div>'
    + '<div class="logbtns">'
      + '<button class="logbtn" id="btnLogWarn">只看警告</button>'
      + '<button class="logbtn" id="btnLogHist">历史日志</button>'
      + '<button class="logbtn" id="btnLogRefresh">刷新</button>'
    + '</div>'
    + '<div class="logbtns" style="margin-top:8px">'
      + '<button class="logbtn" id="btnLogMore" style="display:none" disabled>加载更早</button>'
      + '<button class="logbtn" id="btnLogCopy">复制日志</button>'
    + '</div>'
    + '<div class="logbtns" style="margin-top:8px">'
      + '<button class="logbtn danger" id="btnLogClear">清除日志</button>'
    + '</div>'
    + '</div>';
}

function cardMaint(){
  return '<div class="dcard">'
    + '<div class="dh">维护<span class="tg">会影响正在进行的录制</span></div>'
    + '<button class="btnline" id="btnDiag">导出诊断包（凭据已脱敏）</button>'
    + '<button class="btnline" id="btnRestart">重启服务（加载最新代码）</button>'
    + '<div class="dline" style="font-size:10px;color:var(--faint)">局域网直连 <b>http://&lt;NAS 的 IP&gt;:'
      + REC.port + '</b>。服务监听 0.0.0.0，桌面入口与本机端口互不影响。</div>'
    + '</div>';
}

function renderCamTabs(){
  const body = document.getElementById("camBody");
  const tabs = document.getElementById("camTabs");
  if(tabs) tabs.style.display = "none";
  if(!body) return;
  /* 归档候选摘要：设置页开着且方案是「仅台账 + 归档」时才拉，10 分钟节流 */
  if(SET.open === "generic" && LK.condenseMode === "virtual"
     && Date.now() - ARCH.at > 10*60*1000 && !ARCH.loading){
    ARCH.at = Date.now(); ARCH.loading = true;
    API.get("/api/archive").then(d=>{
      ARCH.loading = false;
      if(!d || !d.ok) return;
      ARCH.sum = "待归档 <b>" + d.todo_count + " 天</b> / " + d.segments
        + " 片 · 预估 " + fmtBytes(d.est_bytes || 0)
        + (d.no_ledger_count ? " · 无台账跳过 " + d.no_ledger_count + " 天" : "")
        + (d.prefetch ? "" : " · 只算今天要删的（未提前）")
        + " · 下次 " + (d.next_run || "—").slice(5);
      const el = document.getElementById("archStatus");
      if(el) el.innerHTML = ARCH.sum;
    }).catch(()=>{ ARCH.loading = false; });
  }

  if(!CAMERAS.length){
    state.camId = null;
    SET.open = null; SET.editing = false; SET.draft = null;
  }else if(!CAMERAS.some(c=>c.id === state.camId)){
    state.camId = CAMERAS[0].id;
  }
  /* 编辑态下必须始终有草稿；否则退出编辑 */
  if(SET.editing && !SET.draft){ SET.editing = false; }

  /* 编辑态独占一屏：公共参数先收起来，避免手滑改到全局，也省得往下滚半天 */
  if(SET.editing){
    body.innerHTML = renderEditMode();
    const wrap = body.parentNode;
    if(wrap) wrap.scrollTop = 0;
    renderMasks(); bindMaskCanvas(); drawMaskScene();
    bindSettings(body);
    saveCfgSoon();
    return;
  }

  /* 三个分区，顺序固定：通用（折叠）→ 摄像头 → 诊断与日志（折叠）。
     摄像头永远在中间且默认展开 —— 它是日常改动对象，不该被另外两块的
     展开状态推着上下跳。 */
  body.innerHTML = renderGenZone()
    + renderCamList()
    + renderDiagZone();

  bindSettings(body);
  renderMasks();
  bindMaskCanvas();
  drawMaskScene();
  /* 诊断区展开且缓存过期才去统计；直播/回放/其它页一律不触发 */
  if(SET.diagOpen) loadStorageUsage(false);
  saveCfgSoon();
}

/* 兼容旧调用点 */
function renderCamBody(){ renderCamTabs(); }

function bindSettings(body){
  /* 公共：录制总开关 / 清理时刻 / 目录 */
  body.querySelectorAll("[data-rec]").forEach(el=>{
    el.onclick = ()=>{
      REC.enabled = !REC.enabled;
      renderCamTabs(); syncDiagSum();
      toast("toast2", REC.enabled ? "录像已启用" : "录像已停止，直播不受影响");
    };
  });
  body.querySelectorAll("[data-cleanat]").forEach(el=>{
    el.onclick = ()=>{ REC.cleanupAt = el.dataset.cleanat; renderCamTabs(); };
  });
  /* 保存目录：默认锁定，点「修改」展开编辑框。
     「检测并保存」先让后端真实探测（存在性 + 写权限），通过才落配置 ——
     以前直接改文本框就把值推上去了，目录写不进要等录像失败才发现。 */
  const de = body.querySelector("#btnDirEdit");
  if(de) de.onclick = ()=>{
    const box = body.querySelector("#dirEditBox");
    if(!box) return;
    const show = box.style.display === "none";
    box.style.display = show ? "" : "none";
    de.textContent = show ? "收起" : "修改";
  };
  const rd = body.querySelector("#recDir");
  const ds = body.querySelector("#btnDirSave");
  if(ds) ds.onclick = async ()=>{
    const out = body.querySelector("#dirCheckOut");
    const p = ((rd && rd.value) || "").trim();
    if(!p){ if(out) out.textContent = "请先填写路径"; return; }
    ds.disabled = true; ds.textContent = "正在检测…";
    if(out) out.textContent = "正在检测目录是否存在、是否可写…";
    try{
      const d = await API.post("/api/dir/check", { path: p, create: true });
      if(d && d.ok){
        REC.dir = d.path || p;
        saveCfgSoon();
        if(out) out.innerHTML = '<span style="color:var(--keep)">✓ ' + esc(REC.dir)
          + (d.created ? "（目录已自动创建）" : "") + ' · 可写，已保存</span>';
        toast("toast2", "保存目录已更新 · 录制立即写入新目录");
        setTimeout(()=>{ try{ renderCamTabs(); syncDiagSum(); }catch(e){} }, 600);
      }else{
        if(out) out.innerHTML = '<span style="color:var(--danger)">✗ '
          + esc((d && d.error) || "检测失败") + '</span>';
      }
    }catch(e){
      if(out) out.innerHTML = '<span style="color:var(--danger)">✗ 检测请求失败：'
        + esc(String(e)) + '</span>';
    }finally{
      ds.disabled = false; ds.textContent = "检测并保存";
    }
  };
  // 每路「诊断」：真跑一次 ffprobe + 8 秒试录，把"是摄像头的问题还是
  // ffmpeg 的问题"当场定死。之前只显示一行 ffmpeg 英文原文，用户没法判断。
  body.querySelectorAll("[data-probe]").forEach(el=>{
    el.onclick = ()=> runProbe(el.dataset.probe, el);
  });
  const dg = body.querySelector("#btnDiag");
  if(dg) dg.onclick = ()=> exportDiag(dg);

  /* 诊断日志：直接看 / 复制 / 清除 */
  const lr = body.querySelector("#btnLogRefresh");
  if(lr) lr.onclick = ()=>{
    /* 看历史时刷新＝重新读文件最新一页；看实时时才是拉内存环 */
    if(LOGP.file) loadLogFile(false); else loadDiagLog();
    toast("toast2", "日志已刷新");
  };
  /* 历史日志 ⇄ 回到实时：内存环只有最近两三小时，查「昨天凌晨为什么断」得读文件 */
  const lh = body.querySelector("#btnLogHist");
  if(lh) lh.onclick = toggleLogSource;
  const lm = body.querySelector("#btnLogMore");
  if(lm) lm.onclick = ()=>{ if(LOGP.file && LOGP.fMore) loadLogFile(true); };
  const lc = body.querySelector("#btnLogCopy");
  if(lc) lc.onclick = copyDiagLog;
  const lx = body.querySelector("#btnLogClear");
  if(lx) lx.onclick = clearDiagLog;
  /* 只看警告：纯前端过滤，不重新请求（日志量大的时候点一下就要有反应） */
  const lw = body.querySelector("#btnLogWarn");
  if(lw) lw.onclick = ()=>{ LOGP.only = !LOGP.only; renderLog(); };
  const rs = body.querySelector("#btnRestart");
  if(rs) rs.onclick = restartDiag;
  if(body.querySelector("#diagEnv")) loadDiagEnv();
  /* 残片：点开才去扫目录（本地快，但没必要每次进设置都扫） */
  body.querySelectorAll("[data-partstoggle]").forEach(el=>{
    el.onclick = ()=>{ SET.partsOpen = !SET.partsOpen; renderCamTabs(); };
  });
  if(body.querySelector("#diagParts")) loadDiagParts();
  if(body.querySelector("#diagLogText")) loadDiagLog();

  /* ① 事件侦测总闸：开/关 */
  body.querySelectorAll("[data-ledger]").forEach(el=>{
    el.onclick = ()=>{
      LK.ledger = !LK.ledger;
      syncCondenseMode();
      renderCamTabs(); saveCfgSoon();
      toast("toast2", LK.ledger ? "事件侦测已开启 · 回放页出现活动标记、可归档" : "事件侦测已关闭 · 只录全时不判定");
    };
  });
  /* 回放静默段处理：客户端播放偏好，存本机（不影响服务端判定） */
  body.querySelectorAll("[data-silent]").forEach(el=>{
    el.onclick = ()=>{
      PPLAY.silent = el.dataset.silent;
      try{ localStorage.setItem("nvr_silent", PPLAY.silent); }catch(e){}
      renderCamTabs();
    };
  });
  /* 夜间归档裁剪：开关 / 保留天数（推服务端，夜间任务循环消费） */
  body.querySelectorAll("[data-archen]").forEach(el=>{
    el.onclick = ()=>{
      LK.archEnable = !LK.archEnable;
      renderCamTabs(); saveCfgSoon();
      toast("toast2", LK.archEnable
        ? "夜间归档已开启 · 夜间任务时刻先归档后清理"
        : "夜间归档已关闭 · 临期全时将直接被清理删除");
    };
  });
  /* 提前归档：决定「全时最后一天」是当晚就裁（并存一天）还是删前才裁（不并存） */
  body.querySelectorAll("[data-archpre]").forEach(el=>{
    el.onclick = ()=>{
      LK.archPrefetch = !LK.archPrefetch;
      ARCH.at = 0;                     /* 候选日期集合会变，摘要重拉 */
      renderCamTabs(); saveCfgSoon();
      toast("toast2", LK.archPrefetch
        ? "已开启提前归档 · 全时到期前一晚就裁，当天会有两份"
        : "已关闭提前归档 · 到删源那一刻才裁，只重叠几十分钟");
    };
  });
  /* 归档保留天数：−/＋ 步进 + 数字直填，范围与服务端 LIMITS 一致（1–3650） */
  body.querySelectorAll("[data-archdec],[data-archinc]").forEach(el=>{
    el.onclick = ()=>{
      const dir = el.dataset.archinc ? 1 : -1;
      LK.archDays = Math.max(1, Math.min(3650, (parseInt(LK.archDays,10)||7) + dir));
      ARCH.at = 0;                     /* 保留期变了，候选摘要重拉 */
      renderCamTabs(); saveCfgSoon();
    };
  });
  const archInp = body.querySelector("#archDaysInput");
  if(archInp) archInp.onchange = ()=>{
    const v = Math.max(1, Math.min(3650, parseInt(archInp.value,10)||7));
    archInp.value = v;                 /* 非法输入回落：钳到边界并回显 */
    if(v !== LK.archDays){
      LK.archDays = v;
      ARCH.at = 0;                     /* 保留期变了，候选摘要重拉 */
      renderCamTabs(); saveCfgSoon();
    }
  };
  /* ---- AI 检测（0.9.60）----
     所有交互最终都落 LK.det 系列 / LK.nt 系列并 saveCfgSoon() 推服务端——
     这是本项目「假设置」血泪教训的防线：只改内存 = 后端永远收不到。 */
  body.querySelectorAll("[data-deten]").forEach(el=>{
    el.onclick = ()=>{
      LK.detEnable = !LK.detEnable;
      renderCamTabs(); saveCfgSoon();
      toast("toast2", LK.detEnable
        ? "AI 检测已开启 · 每 5 秒/路抽帧推理，稍候在状态行查看加载结果"
        : "AI 检测已关闭 · 台账与回放行为恢复纯帧差判定");
    };
  });
  body.querySelectorAll("[data-detcls]").forEach(el=>{
    el.onclick = ()=>{
      const k = el.dataset.detcls;
      LK.detCls[k] = !LK.detCls[k];
      if(!LK.detCls.person && !LK.detCls.two_wheeler && !LK.detCls.vehicle){
        LK.detCls[k] = true;            /* 全关=不检测，设置页拦下并说明 */
        toast("toast2", "全关等于不检测 · 至少保留一个类别，或直接关总闸");
        return;
      }
      renderCamTabs(); saveCfgSoon();
    };
  });
  body.querySelectorAll("[data-detsens]").forEach(el=>{
    el.onclick = ()=>{
      LK.detSens = el.dataset.detsens;
      renderCamTabs(); saveCfgSoon();
    };
  });
  body.querySelectorAll("[data-dettrig]").forEach(el=>{
    el.onclick = ()=>{
      const k = el.dataset.dettrig;
      LK.detTrigger[k] = !LK.detTrigger[k];
      if(!LK.detTrigger.motion && !LK.detTrigger.person && !LK.detTrigger.vehicle){
        LK.detTrigger[k] = true;        /* 至少保留一项（与服务端强校同口径） */
        toast("toast2", "活动触发至少保留一项 · 全不勾所有画面都会按静止剔除");
        return;
      }
      renderCamTabs(); saveCfgSoon();
    };
  });
  body.querySelectorAll("[data-ntcd]").forEach(el=>{
    el.onclick = ()=>{
      LK.ntCooldown = Math.max(30, Math.min(600, (parseInt(LK.ntCooldown,10)||60) + parseInt(el.dataset.ntcd,10)));
      renderCamTabs(); saveCfgSoon();
    };
  });
  /* 通知三开关：开着但没填 Webhook → 拦下并说明（没有发送目标，开了也发不出去） */
  body.querySelectorAll("[data-ntsw]").forEach(el=>{
    el.onclick = ()=>{
      const k = el.dataset.ntsw;
      const nk = k === "motion" ? "ntMotion" : k === "person" ? "ntPerson" : "ntVehicle";
      if(!LK[nk] && !(LK.ntWebhook || "").trim()){
        toast("toast2", "先填写 Webhook 地址，再开通知开关");
        return;
      }
      LK[nk] = !LK[nk];
      renderCamTabs(); saveCfgSoon();
    };
  });
  /* 通知三开关：开着但没填 Webhook → 拦下并说明（发送目标都没有，开了也发不出去） */
  body.querySelectorAll("#ntWebhook").forEach(inp=>{
    inp.onchange = ()=>{
      LK.ntWebhook = (inp.value || "").trim();
      saveCfgSoon();
    };
  });
  /* 平台手选（0.9.124）：公司网关会改写机器人地址的 host，自动识别不出来。
     保存后由 pushCfg 一起推，服务端按该平台出 body。 */
  body.querySelectorAll("[data-ntplat]").forEach(el=>{
    el.onclick = ()=>{
      LK.ntPlatform = el.dataset.ntplat || "auto";
      renderCamTabs(); saveCfgSoon();
    };
  });
  body.querySelectorAll("#ntTemplate").forEach(inp=>{
    inp.onchange = ()=>{
      LK.ntTemplate = (inp.value || "").trim();
      saveCfgSoon();
    };
  });
  const bNtSave = body.querySelector("#btnNtSave");
  if(bNtSave) bNtSave.onclick = ()=>{
    const inp = body.querySelector("#ntWebhook");
    const tp = body.querySelector("#ntTemplate");
    LK.ntWebhook = ((inp && inp.value) || "").trim();
    if(tp) LK.ntTemplate = (tp.value || "").trim();
    saveCfgSoon();
    const out = body.querySelector("#ntOut");
    if(out) out.textContent = LK.ntWebhook ? "已保存 · 记得点右下角「保存通用设置」" : "已清空（保存后不再发送）";
  };
  /* 发送历史（0.9.124）：只在点按钮时读盘。列表存在 NT.hist 里，
     因为卡片每次交互都会整块重渲染，直接写 DOM 会被下一次 render 冲掉。 */
  const bNtHist = body.querySelector("#btnNtHist");
  if(bNtHist) bNtHist.onclick = async ()=>{
    NT.hist = { loading:true };
    renderCamTabs();
    try{
      const d = await API.get("/api/notify/history?limit=20");
      NT.hist = (d && d.items) ? { items:d.items }
               : { err:(d && d.error) || "服务端没返回历史" };
    }catch(e){
      NT.hist = { err:"网络错误，读不到发送历史" };
    }
    renderCamTabs();
  };
  const bNtTest = body.querySelector("#btnNtTest");
  if(bNtTest) bNtTest.onclick = async ()=>{
    const inp = body.querySelector("#ntWebhook");
    const tp = body.querySelector("#ntTemplate");
    const out = body.querySelector("#ntOut");
    const url = ((inp && inp.value) || "").trim();
    if(!url){ if(out) out.textContent = "先填 Webhook 地址再测试"; return; }
    /* 测试发的是「你现在填的这句」而不是磁盘上旧的：否则测试成功、
       真实通知却被改坏的模板回退，问题查不出来。 */
    const tpl = tp ? (tp.value || "").trim() : LK.ntTemplate;
    LK.ntWebhook = url; LK.ntTemplate = tpl || "";
    if(out) out.textContent = "发送中…";
    try{
      /* 这处以前自己拼请求头，漏了 X-NVR-Request —— 服务端 _write_ok 直接 403，
         于是一个能用的 Webhook 地址永远只回"发送失败"。走请求层后由它统一带。
         要等对端 webhook 回应，20 秒是给慢端点的（默认 15 秒偏紧）。 */
      const d = await API.post("/api/notify/test",
        { webhook: url, template: tpl || "", platform: LK.ntPlatform || "auto" },
        { timeout: 20000 });
      if(out) out.textContent = d && d.ok
        ? ("测试发送成功" + (d.ms ? "（" + d.ms + "ms）" : "")
           + (d.platform ? "· 按 " + ntPlatformLabel(d.platform) + " 格式发出" : ""))
        : ("发送失败：" + ((d && d.error) || "未知原因"));
    }catch(e){
      if(out) out.textContent = "发送失败：网络错误";
    }
  };
  /* 立即归档：第一次点 = dry_run 预览；第二次点 = 真跑（受 batch 限速） */
  const bArch = body.querySelector("#btnArchNow");
  if(bArch) bArch.onclick = async ()=>{
    const ao = document.getElementById("archOut");
    if(!ARCH.arm){
      bArch.disabled = true;
      if(ao) ao.textContent = "正在扫描候选日期…";
      try{
        /* 预览要扫台账、算出候选日期与预计产出，慢盘上十几秒很常见 */
        const r = await API.post("/api/archive/run", {dry_run:true}, { timeout: 120000 });
        if(!r || !r.ok){
          if(ao) ao.textContent = "预览失败：" + ((r && r.error) || "未知错误");
          return;
        }
        const est = r.est_bytes ? fmtBytes(r.est_bytes) : "≈0";
        const lines = (r.todo || []).slice(0, 8).map(x=>
          x.name + " " + x.date + "（" + x.segments + " 片）").join("、");
        ARCH.arm = { at: Date.now() };
        ARCH.out = "将归档 <b>" + (r.todo || []).length + " 天</b> / "
          + r.segments + " 个分片，预计产出 <b>" + est + "</b>"
          + (r.no_ledger && r.no_ledger.length
             ? "；跳过无台账 " + r.no_ledger.length + " 天" : "")
          + (lines ? "<br>" + esc(lines) + ((r.todo||[]).length > 8 ? " …" : "") : "")
          + "<br>再点一次开始执行（单晚最多 " + r.batch + " 片）。";
        renderCamTabs();
      }catch(e){
        if(ao) ao.textContent = "预览失败：" + esc(String(e));
      }finally{ bArch.disabled = false; }
      return;
    }
    /* 二次确认 → 执行（预览 5 分钟过期，防误触旧清单） */
    if(Date.now() - ARCH.arm.at > 5*60*1000){
      ARCH.arm = null; ARCH.out = "预览已过期，请重新预览。";
      renderCamTabs();
      return;
    }
    bArch.disabled = true;
    if(ao) ao.textContent = "归档中，正在准备…";
    /* 归档是「点了就走」的长任务：真机上要把一天的量裁完得 80 分钟以上，
       而 HTTP 请求在这期间一个字节都不回，中间链路（NAT/代理）早把空闲
       连接掐了 —— 同步等结果的表现就是「跑了 84 分钟，最后弹归档失败」，
       可文件其实全裁好了。现在服务端立刻回 202，进度与结果都靠轮询拿；
       轮询打的是轻量 /api/archive/progress（只读内存，不扫盘重算计划）。 */
    let poll = null;
    const finished = new Promise((resolve)=>{
      poll = setInterval(()=>{
        API.get("/api/archive/progress").then(a=>{
          const run = a && a.running;
          if(run){
            if(!ao) return;
            ao.innerHTML = "归档中：" + esc(run.name || run.cam || "…") + " "
              + esc(run.date || "") + " 第 <b>" + (run.done || 0) + "/" + (run.total || 0)
              + "</b> 片"
              + (run.round ? "（第 " + run.round + "/" + (run.rounds_max || 6) + " 轮）" : "")
              + (run.used ? "，本轮已裁 " + run.used + "/" + run.batch + " 片" : "")
              + (run.files ? "，产出 " + run.files + " 段 / " + fmtBytes(run.bytes || 0) : "");
            return;
          }
          if(a && a.result && a.result.done){
            clearInterval(poll); poll = null;
            resolve(a.result);
          }
        }).catch(()=>{});
      }, 1500);
    });
    try{
      const s = await API.post("/api/archive/run", {dry_run:false});
      if(!s || !s.ok) throw new Error((s && s.error) || "归档任务未能启动");
      const r = await finished;
      ARCH.arm = null;
      ARCH.out = r.ok
        ? ("归档完成：<b>" + (r.days_done||0) + " 天</b> / " + (r.files||0)
           + " 段 / " + fmtBytes(r.bytes_out||0)
           + ((r.rounds||1) > 1 ? "，连跑 " + r.rounds + " 轮" : "")
           + (r.remaining ? "，剩余 " + r.remaining + " 天跨夜继续" : "")
           + (r.no_ledger ? "；无台账跳过 " + r.no_ledger + " 天" : ""))
        : ("部分失败：完成 " + (r.days_done||0) + " 天，失败 "
           + (r.failed||0) + " 片（下次自动重试）"
           + (r.error ? "：" + esc(String(r.error)) : ""));
      STORAGE.data = null; STORAGE.at = 0;   /* 占用变了，存储卡强制重拉 */
      fetchRecordings(null, true);           /* 归档产物立刻出现在文件页的日期条里（绕过 3 秒缓存） */
      renderCamTabs();
    }catch(e){
      if(poll) clearInterval(poll);
      ARCH.arm = null;
      if(ao) ao.textContent = "归档未完成：" + esc(String(e));
      renderCamTabs();
    }
  };
  body.querySelectorAll("[data-linkseg]").forEach(el=>{
    el.onclick = ()=>{
      LK.segSec = +el.dataset.linkseg; renderCamTabs();
      saveCfgSoon();            /* 落盘 + 推服务端：分片长度立即热生效 */
      toast("toast2", "分片改为 " + Math.round(LK.segSec/60) + " 分钟，正在按新参数重启录制");
    };
  });
  const lkRun = body.querySelector("#btnLinkRun");
  if(lkRun) lkRun.onclick = ()=>{
    lkRun.disabled = true; lkRun.textContent = "正在触发…";
    API.post("/api/link/run", {}).then(d=>{
        if(d && d.ok && d.link) applyLink(d.link);
        const n = (d && d.link && d.link.pending) || 0;
        toast("toast2", n ? "已入队，待处理 " + n + " 项" : "本轮没有新的已封口分片");
      }).catch(()=>{ toast("toast2", "触发失败：请查看服务端日志"); })
      .finally(()=>{ lkRun.disabled = false; lkRun.textContent = "立即处理一批"; });
  };

  /* 公共：硬件 */
  body.querySelectorAll("[data-gs]").forEach(el=>{
    el.onclick = ()=>{ GS[el.dataset.gs] = !GS[el.dataset.gs]; renderCamTabs(); };
  });
  body.querySelectorAll("[data-conc]").forEach(el=>{
    el.onclick = ()=>{
      GS.conc = +el.dataset.conc; renderCamTabs();
      toast("toast2", GS.conc===1 ? "改为串行：内存峰值减半" : "并行 2 路：约 1.5～1.8 倍速");
    };
  });

  bindCamDrag(body);

  /* 摄像头行：展开 / 收起 */
  body.querySelectorAll("[data-camrow]").forEach(row=>{
    row.onclick = e=>{
      if(e.target.closest("[data-delcam]") || e.target.closest("[data-drag]")) return;
      SET.addArmed = false;   // 点到别处就取消「待确认添加」
      const id = row.dataset.camrow;
      if(SET.editing && state.camId !== id){
        toast("toast2", "还有未保存的改动 · 先保存或取消");
        return;
      }
      if(SET.open === id && !SET.editing){ SET.open = null; }
      else { SET.open = id; state.camId = id; }
      renderCamTabs();
      if(SET.open === id) loadDay();
    };
  });

  /* 删除：两段确认 */
  body.querySelectorAll("[data-delcam]").forEach(el=>{
    el.onclick = e=>{
      e.stopPropagation();
      SET.addArmed = false;
      const id = el.dataset.delcam;
      if(SET.armed === id){ delCamera(id); return; }
      SET.armed = id;
      renderCamTabs();
      toast("toast2", "再点一次「确认删除」");
      setTimeout(()=>{ if(SET.armed === id){ SET.armed = null; renderCamTabs(); } }, 3200);
    };
  });

  /* 添加：两段确认（0.9.64）。第一点只待确认，第二点才真添加；
     超时/点到别处自动取消待确认态 */
  body.querySelectorAll("[data-addcam]").forEach(el=>{
    el.onclick = e=>{
      e.stopPropagation();
      if(SET.addArmed){ SET.addArmed = false; addCamera(); return; }
      SET.addArmed = true;
      renderCamTabs();
      toast("toast2", "再点一次「确认添加」· 3 秒内有效");
      setTimeout(()=>{ if(SET.addArmed){ SET.addArmed = false; renderCamTabs(); } }, 3200);
    };
  });

  /* 只读 → 编辑 / 收起 */
  body.querySelectorAll("[data-editcam]").forEach(el=>{
    el.onclick = e=>{ e.stopPropagation(); openEditor(state.camId); };
  });
  body.querySelectorAll("[data-closecam]").forEach(el=>{
    el.onclick = e=>{ e.stopPropagation(); closeEditor(); };
  });

  /* 编辑态：输入框 / 滑杆 */
  body.querySelectorAll("[data-k]").forEach(inp=>{
    inp.oninput = ()=>{
      const k = inp.dataset.k;
      const v = inp.type === "range" ? +inp.value : inp.value;
      draftSet(k, v);
      /* 又填了新地址 → 之前点的「清除」作废：用户的最后意图是"改成这个"，
         不是"先删再建"。服务端也按"新明文优先于 clear 标记"处理，两边一致。 */
      if((k === "main" || k === "sub") && v) draftSet(k === "main" ? "urlClear" : "subClear", false);
      syncOuts();
    };
    inp.onchange = inp.oninput;
  });
  const nm = body.querySelector("[data-camname]");
  if(nm) nm.oninput = ()=>{ const c = curCam(); if(c) c.name = nm.value; };

  /* 编辑态：快照型按钮（点击后重绘） */
  body.querySelectorAll("[data-nuke]").forEach(el=>{
    el.onclick = e=>{
      e.stopPropagation();
      /* 只打标记、不直接发请求：和别的改动一样走「点保存才生效」，
         用户误点了还能取消（取消 = 丢掉草稿，标记也就没了）。 */
      const k = el.dataset.nuke;
      if(k === "main"){ draftSet("main", ""); draftSet("urlClear", true); }
      else { draftSet("sub", ""); draftSet("subClear", true); }
      renderCamTabs();
      toast("toast2", "已标记清除 · 点「保存」才真正删掉 NAS 上的地址");
    };
  });
  body.querySelectorAll("[data-camseg]").forEach(el=>{
    el.onclick = ()=>{
      draftSet("segSec", +el.dataset.camseg);
      renderCamTabs();
      const v = +el.dataset.camseg;
      toast("toast2", v ? "分片改为 " + segLabel(v) + " · 保存后重启这一路录制"
                        : "改为跟随通用设置 " + segLabel(LK.segSec));
    };
  });
  body.querySelectorAll("[data-en]").forEach(el=>{
    el.onclick = ()=>{ draftSet("enabled", !draftGet("enabled")); renderCamTabs(); };
  });
  /* 事件侦测：每路总开关（草稿态，点保存才落盘）。
     折叠由手风琴的 data-acc 统一管，这里不再有自己的开关。
     注：输出画质 / 切分方式 / 输出倍速 / 浓缩保留 已随缩时片段下线，
     对应控件不再渲染，绑定也一并移除。 */
  body.querySelectorAll("[data-cond]").forEach(el=>{
    el.onclick = ()=>{
      draftSet("condense", !draftGet("condense"));
      renderCamTabs();
    };
  });
  /* 事件侦测：follow=跟随 / virtual=开启 / off=关闭（materialize 仅配置可设，草稿态，点保存才落盘） */
  body.querySelectorAll("[data-cmcam]").forEach(el=>{
    el.onclick = ()=>{
      draftSet("condenseMode", el.dataset.cmcam);
      renderCamTabs();
    };
  });
  /* 本路 AI 检测：follow/on/off（草稿态）。off 时不抽帧不推理，
     全局总闸关闭时本段整体不生效（由 bodyDetect 的提示文案说明） */
  body.querySelectorAll("[data-camdet]").forEach(el=>{
    el.onclick = ()=>{
      draftSet("detect", el.dataset.camdet);
      renderCamTabs();
    };
  });
  /* 本路检测类别：person / two_wheeler / vehicle 三开关（草稿态）。
     只推显式配了的三键，服务端缺键=False，不会把全局类别偷偷补回来。 */
  body.querySelectorAll("[data-camdetcls]").forEach(el=>{
    el.onclick = ()=>{
      const k = el.dataset.camdetcls;
      const cur = draftGet("detectCls") || { person:true, two_wheeler:true, vehicle:true };
      const next = Object.assign({}, cur);
      next[k] = !cur[k];
      if(!next.person && !next.two_wheeler && !next.vehicle){
        toast("toast2", "至少保留一个类别 · 全关等于不检测，直接用「关」更省算力");
        return;
      }
      draftSet("detectCls", next);
      renderCamTabs();
    };
  });
  /* 判读条件：引擎 / 画面类型 / 灵敏度 三组 chips */
  body.querySelectorAll("[data-engine]").forEach(el=>{
    el.onclick = ()=>{ draftSet("judge.engine", el.dataset.engine); renderCamTabs(); };
  });
  body.querySelectorAll("[data-scene]").forEach(el=>{
    el.onclick = ()=>{ draftSet("judge.scene", el.dataset.scene); renderCamTabs(); };
  });
  body.querySelectorAll("[data-sens]").forEach(el=>{
    el.onclick = ()=>{ draftSet("judge.sens", el.dataset.sens); renderCamTabs(); };
  });
  /* 通用设置：展开 / 收起 / 保存并折叠 */
  body.querySelectorAll("[data-gentoggle]").forEach(el=>{
    el.onclick = ()=>{
      if(SET.editing) return;
      SET.genOpen = !SET.genOpen;
      renderCamTabs();
    };
  });
  /* 诊断与日志：展开时才去统计（缓存命中就秒回），平时一次请求都不发 */
  body.querySelectorAll("[data-diagtoggle]").forEach(el=>{
    el.onclick = ()=>{
      if(SET.editing) return;
      SET.diagOpen = !SET.diagOpen;
      renderCamTabs();
      const st = document.getElementById("camBody").parentNode;
      if(st && SET.diagOpen) st.scrollTop = st.scrollHeight;
    };
  });
  const gR = body.querySelector("#btnGapsRefresh");
  if(gR) gR.onclick = ()=>{
    gR.disabled = true;
    gR.textContent = "核对中…";
    loadGaps(true).then(()=>{
      const b = document.getElementById("btnGapsRefresh");
      if(b){ b.disabled = false; b.textContent = "重新核对"; }
      toast("toast2", GAPS.err ? "读取缺口失败 · 看日志" : "已按分片重新核对今天的录像");
    });
  };

  const stR = body.querySelector("#btnStRefresh");
  if(stR) stR.onclick = ()=>{
    stR.disabled = true;
    stR.textContent = "统计中…";
    loadStorageUsage(true).then(()=>{
      const b = document.getElementById("btnStRefresh");
      if(b){ b.disabled = false; b.textContent = "立即重算"; }
      toast("toast2", STORAGE.err ? "统计失败 · 看日志" : "已重新统计各摄像头占用");
    });
  };

  /* 摄像头编辑：四段手风琴 + 两个小节折叠 */
  body.querySelectorAll("[data-acc]").forEach(el=>{
    el.onclick = ()=>{
      const k = el.dataset.acc;
      SET.acc = (SET.acc === k) ? "" : k;
      renderCamTabs();
    };
  });
  body.querySelectorAll("[data-adv]").forEach(el=>{
    el.onclick = ()=>{ SET.adv = !SET.adv; renderCamTabs(); };
  });
  const gsv = body.querySelector("[data-gensave]");
  if(gsv) gsv.onclick = ()=>{
    SET.genOpen = false;
    genApply();          /* 通用参数在这里落盘 + 对齐到录像/回放页 */
    renderCamTabs();
    toast("toast2", "通用设置已保存 · 已折叠");
  };

  /* 步进器：保留天数（草稿） */
  body.querySelectorAll("[data-ret]").forEach(el=>{
    el.onclick = ()=>{
      const key = el.dataset.ret, d = +el.dataset.d;
      const lim = key === "fullDays" ? [1,30] : [7,3650];
      const cur = draftGet(key);
      draftSet(key, Math.max(lim[0], Math.min(lim[1], cur + d)));
      renderCamTabs();
    };
  });

  /* 保存 / 恢复默认 / 取消 */
  body.querySelectorAll("[data-save]").forEach(el=>{ el.onclick = e=>{ e.stopPropagation(); saveDraft(); }; });
  body.querySelectorAll("[data-reset]").forEach(el=>{ el.onclick = e=>{ e.stopPropagation(); resetDraft(); }; });
  body.querySelectorAll("[data-cancel]").forEach(el=>{ el.onclick = e=>{ e.stopPropagation(); closeEditor(); }; });
}
