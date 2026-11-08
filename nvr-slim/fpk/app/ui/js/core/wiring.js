/* ================= core/wiring.js =================
   把控件接到已有函数上的最后一层：返回键、回放页摄像头下拉、?page= 直达、
   首页列数与连续播放、全屏退出的两个出口、单格「无画面 · 重试」重连。
   判断标准：这里只有 DOM 事件绑定与一两行转发，不定义渲染、不定义请求；
   一旦要算数据或画卡片，就该写在对应的 pages/ 或 components/ 文件里。
   为什么单独成文件：它排在主题引擎之后、凭证启动之前，是页面「能点」之前
   的最后一道接线。混进各页之后，「按钮没反应」会变成满文件找 addEventListener。
   ==================================================== */

document.getElementById("lrBack").onclick = ()=> leaveLive();

/* 回放页摄像头下拉：标题点一下展开，点别处收起 */
const camSelEl = document.getElementById("camSel");
if(camSelEl) camSelEl.onclick = e=>{ e.stopPropagation(); camPickShow(); };
document.addEventListener("click", e=>{
  const box = document.getElementById("camPick");
  if(!box || !box.classList.contains("on")) return;
  if(box.contains(e.target) || (e.target.closest && e.target.closest("#camSel"))) return;
  camPickShow(false);
});
renderCamPick();

/* 直达某一页（排查 / 截图用）：index.html?page=cam 直接落在设置页 */
(()=>{
  try{
    const q = new URLSearchParams(location.search);
    const p = q.get("page");
    if(p){
      const b = document.querySelector('.tb[data-page="' + p + '"]');
      if(b) b.click();
    }
    /* 深链展开：?page=cam&diag=1 展开诊断；&edit=1&acc=condense&adv=1&out=1
       直接打开第一台摄像头的对应段 —— 供真机排障与截图复核，不影响正常使用 */
    if(q.get("diag") === "1"){ SET.diagOpen = true; renderCamTabs(); }
    if(q.get("edit") === "1" && CAMERAS.length){
      openEditor(CAMERAS[0].id);
      if(q.get("acc")) SET.acc = q.get("acc");
      if(q.get("adv") === "1") SET.adv = true;
      if(q.get("out") === "1") SET.outOpen = true;
      renderCamTabs();
    }
  }catch(e){}
})();
document.getElementById("lrGrid").onclick = e=>{
  e.stopPropagation();
  state.liveView = state.liveView === "grid" ? "single" : "grid";
  state.livePaused = false;
  state.liveStream = true;                 // 切宫格/单路都续播实时流
  state.lrChrome = true;
  buildLiveRoom();
  syncLiveChrome();
};

/* 首页：列数按钮 / 列数弹层 / 连续播放 */
const _btnCols = document.getElementById("btnLiveCols");
if(_btnCols) _btnCols.onclick = e=>{ e.stopPropagation(); toggleRatePop(null, false); toggleColsPop(e); };
const _wallPlay = document.getElementById("wallPlay");
if(_wallPlay) _wallPlay.onclick = e=>{ e.stopPropagation(); toggleWall(); };

/* 全屏退出：顶部条与底部条各一个，走同一条出口。
   全屏只有一种退出方式是不够的 —— 用户可能在任何时候想看顶部那个。 */
["lrExitTop","lrExitBot"].forEach(id=>{
  const b = document.getElementById(id);
  if(b) b.onclick = e=>{ e.stopPropagation(); leaveLive(); };
});
const _lrBigPlay = document.getElementById("lrBigPlay");
if(_lrBigPlay) _lrBigPlay.onclick = e=>{
  e.stopPropagation();
  if(!state.liveStream) toggleLiveStream();
  else nudgeLiveChrome();
};
/* 全屏里点画面：显示/隐藏控制条（播放中）。暂停时点画面直接续播。 */
const _lrBody = document.getElementById("lrBody");
if(_lrBody) _lrBody.addEventListener("click", ()=>{
  if(!state.lrFs) return;
  if(!state.liveStream){ toggleLiveStream(); return; }
  state.lrChrome = !state.lrChrome;
  syncLiveChrome();
});

/* 连续播放时某一路挂了：右下角「无画面 · 重试」只重连这一格 */
const _liveWrap = document.getElementById("liveWrap");
if(_liveWrap) _liveWrap.addEventListener("click", e=>{
  const chip = e.target.closest(".snap-ago.retry");
  if(!chip) return;
  e.stopPropagation();
  const card = chip.closest(".livecard");
  if(!card) return;
  const c = CAMERAS[card._camIdx];
  if(!c) return;
  card.classList.remove("fallback","streaming","hasphoto");
  snapSt(c.id).fails = 0;
  mountLiveStream(card, c, true);
  toast("toast2", "正在重连 " + c.name);
});
