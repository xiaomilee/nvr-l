/* ================= pages/settings_persist.js =================
   设置页改完之后的落地动作：配置读写（localStorage + 服务端合并、防抖保存、
   旧格式兼容）与摄像头列表拖动排序。两者看着不搭，其实是同一件事的两半 ——
   **用户改过的值怎么活过刷新、怎么变成后端真在用的参数**。
   判断标准：写配置、合并旧数据、保存顺序的放这里；编辑中的草稿与表单在
   settings_editor.js，卡片长什么样在 settings_cards.js。
   为什么单独成文件：0.7.0 之前删掉的机器一刷新又回来了，真因就是这段
   当年散在渲染函数里；持久化集中一处，"保存了却没生效"只需查这一份。
   ============================================================= */

/* ================= 配置持久化 =================
   摄像头列表、每路参数、通用设置都要活过刷新。
   0.7.0 之前全在内存里，删掉的机器一刷新又回来了 —— 这是"删了还在"的真因。
   存在 localStorage（按 http://NAS:端口 这个源隔离），换浏览器/清缓存会回到演示数据。 */
const CFG_KEY = "nvr.cfg.v1";
let cfgTimer = null;

function saveCfg(){
  try{
    cfgTimer = null;
    pushCfg();
    const params = {};
    CAMERAS.forEach(c=>{
      const p = camParams[c.id];
      if(!p) return;
      /* 地址与其中的账号密码不落盘（0.9.120）。
         以前整份 camParams 直接序列化，于是 rtsp://user:pass@ip 就永久留在了
         这台设备的 localStorage 里 —— 而它很可能是前台电脑或临时借来的手机，
         清缓存之前谁都读得到。现在明文只在内存里活到页面关闭；要看/改地址
         就重新填一遍，只改别的设置则留空即可（服务端回落 NAS 值）。 */
      const q = Object.assign({}, p);
      delete q.main; delete q.sub; delete q.url; delete q.subUrl;
      params[c.id] = q;
    });
    localStorage.setItem(CFG_KEY, JSON.stringify({
      v: 1,
      cams: CAMERAS.map(c=>({ id:c.id, name:c.name, ch:c.ch, res:c.res, online:!!c.online })),
      params: params,
      gen: {
        enabled: REC.enabled, dir: REC.dir, cleanupAt: REC.cleanupAt,
        linkMode: LK.mode, intervalH: LK.intervalH, segSec: LK.segSec,
        clipStyle: LK.clipStyle,
        gpu: GS.gpu, conc: GS.conc, nightOnly: GS.nightOnly, lowPrio: GS.lowPrio
      }
    }));
  }catch(e){}
}
/* 渲染很频繁，攒一下再写，避免连续改动时反复序列化 */
function saveCfgSoon(){
  if(cfgTimer) return;
  try{ cfgTimer = setTimeout(saveCfg, 250); }catch(e){ saveCfg(); }
}

function cfgMerge(base, saved){
  if(!saved || typeof saved !== "object") return base;
  Object.keys(saved).forEach(k=>{
    const v = saved[k];
    if(v && typeof v === "object" && !Array.isArray(v)
       && base[k] && typeof base[k] === "object" && !Array.isArray(base[k])){
      cfgMerge(base[k], v);
    }else if(v !== undefined){
      base[k] = v;
    }
  });
  return base;
}

function loadCfg(){
  let raw = null;
  try{ raw = localStorage.getItem(CFG_KEY); }catch(e){ return false; }
  if(!raw) return false;
  let d = null;
  try{ d = JSON.parse(raw); }catch(e){ return false; }
  if(!d || !Array.isArray(d.cams)) return false;

  CAMERAS.length = 0;
  d.cams.forEach(c=>{
    if(!c || !c.id) return;
    CAMERAS.push({ id:c.id, name:c.name || "摄像头", ch:c.ch || "", res:c.res || "1080p",
                   online:!!c.online, main:"", sub:"" });
  });
  Object.keys(camParams).forEach(k=>{ delete camParams[k]; });
  CAMERAS.forEach((c,i)=>{
    const saved = d.params && d.params[c.id];
    const p = cfgMerge(defaultCamParams(c, i), saved || null);
    p.masks = normMasks(p.masks);
    /* 旧版本把明文地址存在这里（0.9.120 之前）。读到就清空：留着等于帮它续命，
       而且一旦被 pushCfg 带上服务端，服务端会当成"新地址"覆盖 —— 明明没改。
       键保留成空串（不是删掉），下面渲染与推送都按"本机没有明文"处理。 */
    p.main = ""; p.sub = "";
    delete p.url; delete p.subUrl;
    /* 老配置迁移：0~100 的灵敏度百分比 / 最短保留 / 静默合并 → 新判读模型 */
    p.judge = normJudge(p.judge, saved || null);
    delete p.sensitivity; delete p.minKeep; delete p.mergeGap;
    camParams[c.id] = p;
  });

  const g = d.gen || {};
  ["enabled","dir","cleanupAt"].forEach(k=>{ if(g[k] !== undefined) REC[k] = g[k]; });
  if(g.linkMode) LK.mode = g.linkMode;
  if(g.intervalH) LK.intervalH = g.intervalH;
  if(g.segSec) LK.segSec = g.segSec;
  if(g.clipStyle) LK.clipStyle = g.clipStyle === "split" ? "split" : "join";
  ["gpu","conc","nightOnly","lowPrio"].forEach(k=>{ if(g[k] !== undefined) GS[k] = g[k]; });

  if(!CAMERAS.length){ state.camId = null; }
  else if(!CAMERAS.some(c=>c.id === state.camId)){ state.camId = CAMERAS[0].id; }
  return true;
}

/* ================= 拖动排序 ================= */
let dragCam = null;
function bindCamDrag(body){
  body.querySelectorAll("[data-drag]").forEach(h=>{
    h.onpointerdown = e=>{
      if(SET.editing) return;                     /* 编辑态先别动顺序，免得误触 */
      const row = h.closest ? h.closest("[data-camrow]") : null;
      if(!row) return;
      const rows = Array.prototype.slice.call(body.querySelectorAll("[data-camrow]"));
      const from = rows.indexOf(row);
      if(from < 0) return;
      e.preventDefault(); e.stopPropagation();
      const hh = (row.getBoundingClientRect().height || 64) + 8;
      dragCam = { id:h.dataset.drag, row, rows, from, to:from, lastTo:from, y0:e.clientY, hh };
      row.classList.add("dragging");
      try{ h.setPointerCapture(e.pointerId); }catch(err){}
    };
    h.onpointermove = e=>{
      if(!dragCam) return;
      e.preventDefault();
      const dy = e.clientY - dragCam.y0;
      dragCam.row.style.transform = "translateY(" + dy + "px)";
      const to = Math.max(0, Math.min(dragCam.rows.length-1, dragCam.from + Math.round(dy / dragCam.hh)));
      if(to !== dragCam.lastTo){
        dragCam.lastTo = to; dragCam.to = to;
        dragCam.rows.forEach((r,i)=>{
          if(r === dragCam.row) return;
          let off = 0;
          if(dragCam.from < to && i > dragCam.from && i <= to) off = -dragCam.hh;
          else if(dragCam.from > to && i >= to && i < dragCam.from) off = dragCam.hh;
          r.style.transition = "transform .12s ease";
          r.style.transform = off ? "translateY(" + off + "px)" : "";
        });
      }
    };
    h.onpointerup = ()=>{ if(dragCam) finishCamDrag(); };
    h.onpointercancel = ()=>{ if(dragCam) cancelCamDrag(); };
  });
}
function finishCamDrag(){
  const d = dragCam; dragCam = null;
  if(!d) return;
  d.rows.forEach(r=>{ r.style.transform = ""; r.style.transition = ""; });
  d.row.classList.remove("dragging");
  if(d.to !== d.from){
    const moved = CAMERAS.splice(d.from, 1)[0];
    if(moved) CAMERAS.splice(d.to, 0, moved);
    saveCfg();
    renderCamTabs();
    syncCams();                       /* 预览墙与回放下拉的顺序立刻跟着变 */
    toast("toast2", "顺序已更新 · 预览与回放同步");
  }else{
    renderCamTabs();
  }
}
function cancelCamDrag(){
  const d = dragCam; dragCam = null;
  if(!d) return;
  d.rows.forEach(r=>{ r.style.transform = ""; r.style.transition = ""; });
  d.row.classList.remove("dragging");
}
