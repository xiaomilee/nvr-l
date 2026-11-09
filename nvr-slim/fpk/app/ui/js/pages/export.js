/* ================= pages/export.js =================
   文件页的「导出时间段」：把某一天某一段时间的分片打成一个 zip。

   放在文件页而不是回放页：导出的是**磁盘上的分片**，与这一页列的是同一批
   文件；放在回放页会让人以为导的是"正在播的那一段"。

   判断标准：只回答「把这些分片打成一个包」这一件事。哪天有哪些分片是
   clips.js 的事，怎么播是 player/ 的事，筛哪些事件是 events.js 的事 ——
   这里既不重算清单，也不自己拼下载地址。

   三条不能挪走的约定：
     1. 日期跟着 `state.daySel` 走 —— 这一页上下看到的就是那天的分片，
        另开一个日期选择器只会让人怀疑"到底导的是哪天"。
     2. 下载地址用**服务端给的** `d.url`，前端不再拼一遍。拼第二遍就等于
        第二套口径，改了一处忘了另一处时，预览说 2 个分片、下载却给了 3 个。
     3. 边界的多给必须写在提示里（`边界多出 前 x / 后 y`）。分片原样导出
        必然多给几十秒，事前说清楚比事后解释便宜。
   ================================================== */

const EXP = {open:false, cams:[], from:"", to:"", plan:null, err:"",
             busy:false, seq:0};

/* 导出的那一天：文件页当前选中的日期 */
function expDay(){
  return state.daySel || (state.dayList && state.dayList[state.dayIdx]) || jsToday();
}

function expPlanUrl(){
  const day = expDay();
  return "/api/export/plan?cam=" + encodeURIComponent(EXP.cams.join(","))
       + "&from=" + encodeURIComponent(day + " " + (EXP.from || "00:00"))
       + "&to=" + encodeURIComponent(day + " " + (EXP.to || "23:59"));
}

function expSize(n){
  n = Number(n) || 0;
  if(n >= 1073741824) return (n / 1073741824).toFixed(1) + " GB";
  if(n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if(n >= 1024) return Math.max(1, Math.round(n / 1024)) + " KB";
  return n + " B";
}

function expDur(sec){
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if(sec < 60) return sec + " 秒";
  const m = Math.floor(sec / 60), s = sec % 60;
  return s ? (m + " 分 " + s + " 秒") : (m + " 分");
}

/* 计划 → 一句话。导不了时只说原因，不说"请稍后重试"——那种话等于没说。 */
function expHintText(d){
  if(!d) return "正在算…";
  if(!d.ok) return "导不了：" + (d.error || "未知原因");
  const t = d.total || {};
  const out = [t.count + " 个分片 · " + expSize(t.bytes)];
  const firsts = (d.cams || []).map(c => c.first_at).filter(Boolean).sort();
  const lasts = (d.cams || []).map(c => c.last_at).filter(Boolean).sort();
  if(firsts.length && lasts.length){
    out.push("实际覆盖 " + firsts[0].slice(11, 16) + "–"
             + lasts[lasts.length - 1].slice(11, 16));
  }
  if(t.head_extra_sec || t.tail_extra_sec){
    out.push("边界多出 前 " + expDur(t.head_extra_sec)
             + " / 后 " + expDur(t.tail_extra_sec));
  }
  let text = out.join(" · ");
  const warns = d.warnings || [];
  if(warns.length) text += "\n" + warns.slice(0, 3).join("；");
  if(warns.length > 3) text += "；另有 " + (warns.length - 3) + " 条";
  return text;
}

function expRenderCams(){
  const bar = document.getElementById("expCamBar");
  if(!bar) return;
  const list = CAMERAS || [];
  bar.innerHTML = list.map(c => {
    const on = EXP.cams.indexOf(c.id) >= 0;
    return '<button class="evchip' + (on ? " on" : "") + '" data-expcam="'
      + esc(c.id) + '">' + esc(c.name || c.id) + '</button>';
  }).join("");
  bar.querySelectorAll("[data-expcam]").forEach(b => {
    b.onclick = () => {
      const id = b.getAttribute("data-expcam");
      const i = EXP.cams.indexOf(id);
      if(i >= 0) EXP.cams.splice(i, 1); else EXP.cams.push(id);
      expRenderCams(); expRender(); expRefresh();
    };
  });
}

function expRender(){
  const hint = document.getElementById("expHint");
  if(hint){
    const t = EXP.err ? ("导不了：" + EXP.err) : expHintText(EXP.plan);
    if(hint.textContent !== t) hint.textContent = t;
  }
  const go = document.getElementById("btnExpGo");
  if(go){
    const ok = !!(EXP.plan && EXP.plan.ok && EXP.cams.length && !EXP.busy);
    go.disabled = !ok;
    const label = EXP.busy ? "正在算…"
      : (EXP.plan && EXP.plan.ok ? "开始导出（" + EXP.plan.total.count + " 个分片）" : "开始导出");
    if(go.textContent !== label) go.textContent = label;
  }
  const f = document.getElementById("expFrom");
  if(f && f.value !== (EXP.from || "00:00")) f.value = EXP.from || "00:00";
  const t2 = document.getElementById("expTo");
  if(t2 && t2.value !== (EXP.to || "23:59")) t2.value = EXP.to || "23:59";
}

/* 计划请求。用序号而不是"取消上一个请求"：`API.req` 的 fetch 不带 signal，
   后排队的请求仍会回来；序号让晚到的旧结果直接作废，不会覆盖新条件的结果。 */
function expRefresh(){
  if(!EXP.cams.length){
    EXP.plan = null; EXP.err = ""; expRender(); return;
  }
  EXP.busy = true; expRender();
  const url = expPlanUrl(), seq = ++EXP.seq;
  API.req(url).then(d => {
    if(seq !== EXP.seq) return;
    EXP.busy = false;
    if(d && d.ok === false && d.error) { EXP.plan = null; EXP.err = d.error; }
    else { EXP.plan = d; EXP.err = ""; }
    expRender();
  }).catch(e => {
    if(seq !== EXP.seq) return;
    EXP.busy = false; EXP.plan = null;
    EXP.err = String((e && e.message) || e);
    expRender();
  });
}

function expOpen(on){
  EXP.open = (on === undefined) ? !EXP.open : !!on;
  const box = document.getElementById("expBox");
  if(box) box.hidden = !EXP.open;
  if(!EXP.open) return;
  if(!EXP.cams.length) EXP.cams = state.camId ? [state.camId] : [];
  if(!EXP.from) EXP.from = "00:00";
  if(!EXP.to) EXP.to = "23:59";
  expRenderCams(); expRender(); expRefresh();
}

/* 走服务端给的地址直接下载：<a download> 会带上 cookie，不用在前端拼 token。 */
function expGo(){
  const d = EXP.plan;
  if(!d || !d.ok) return;
  const a = document.createElement("a");
  a.href = d.url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  if(a.remove) a.remove();
  toast("toast", "已开始导出 · " + d.total.count + " 个分片，浏览器里看进度");
}

(function(){
  const btn = document.getElementById("btnExport");
  if(btn) btn.onclick = () => expOpen();
  const cl = document.getElementById("btnExpClose");
  if(cl) cl.onclick = () => expOpen(false);
  const go = document.getElementById("btnExpGo");
  if(go) go.onclick = () => expGo();
  /* change 而不是 input：时间框每变一位就发一次计划请求，服务端要去 stat
     几千个分片，输入会卡住。 */
  const f = document.getElementById("expFrom");
  if(f) f.onchange = () => { EXP.from = f.value || "00:00"; expRefresh(); };
  const t = document.getElementById("expTo");
  if(t) t.onchange = () => { EXP.to = t.value || "23:59"; expRefresh(); };
})();
