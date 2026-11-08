/* ================= pages/live_snap.js =================
   直播页的取图入口：刷新间隔、单张快照的超时与 token 去重加载。
   它排在统一请求层**之前**，不是漏搬 —— 快照是 img.src 直接取一张 JPEG，
   走 fetch/API 反而要多一次解码与凭证处理；请求层之后那半张直播页在
   pages/live.js，两段被 core/api.js 隔开是执行顺序决定的。
   判断标准：只负责「哪张图、多久换、加载失败怎么算」的放这里；
   快照墙怎么摆、字幕怎么画、宫格怎么切都不进这个文件。
   为什么单独成文件：断网时「快照墙一直转圈还是显示几秒前」由这一小段决定，
   混在渲染里最难复现，也最容易被顺手改坏。
   ====================================================== */

/* ================= 直播页 =================
   三种状态：
     snap   快照墙（默认）—— 只有静态截图，显示"几秒前"与倒计时，几乎不耗流量
     single 单路实时 —— 点某张快照进入，只解一路子码流
     grid   宫格实时 —— 默认铺满全部摄像头，点任意窗口回到单路
   ============================================ */
const SNAP = { interval: 10 };   // 快照刷新间隔（秒），1~10 可调，齿轮里改
try{
  const r = +(localStorage.getItem("nvr.snapRate") || 0);
  if(r >= 1 && r <= 10) SNAP.interval = r;
}catch(e){}
/* 每路快照的真实状态。字段含义：
     at      : 最近一次**成功拿到图**的时间戳（倒计时从它算，失败不前推）
     since   : 本轮"连续失败"从什么时候开始（成功一次就清空）
     pend    : 已发出请求还没回（防止同一次刷新挂多个请求）
     fails   : 连续失败次数
     shownAt : 当前画面上这张图的实际拍摄时刻（服务端给的时间，没有则用 at）
   以前 tickSnaps 是"到点就把 at 改成 now，然后发一个请求"——请求成不成功都
   算刷新成功，于是断网时倒计时照常 10→0→10 循环，看起来一切正常。 */
const snapState = {};            // camId -> { at, since, pend, fails, shownAt }
function snapSt(camId){
  let st = snapState[camId];
  /* live/frozen：实时流的时刻追踪 —— 流活着右上角跟当前钟走；
     流断了冻结在断流那一刻（frozen 记下断流时间），不继续假装在走 */
  if(!st) st = snapState[camId] = { at:0, since:0, pend:false, fails:0, shownAt:0, live:false, frozen:0, lastSrc:"" };
  return st;
}
const SNAP_FAIL_MS = 3000;       // 一次请求超过这个时间没结果就算超时（RTSP 拉不起来时不能干等）
let snapAcc = 0;

/* 真实快照：服务端 ffmpeg 每隔 SNAP.interval 秒抓一帧（设置页填了 RTSP 就有）。
   图还没就绪（404）就留在示意画面上，来了自动切过去。
   注：老的 hookSnapImg / refreshSnapImg 已由 pullSnap 统一接管 ——
   它们不关心加载结果，正是"断网也倒数"的根源。 */

/* 真正"刷新一张快照"：把 img 换 src，然后**等它 load 成功**才算刷新成功。
   失败/超时就把这一路标成失败态 —— 倒计时停在原地并显示"未更新"，
   而不是继续 10→0 地假装在刷新。 */
function pullSnap(camId, img, onDone){
  const st = snapSt(camId);
  if(st.pend) return;                        // 上一次还没回，别叠请求
  if(!img){ onDone && onDone(false); return; }
  st.pend = true;
  const token = Date.now();
  let settled = false;
  const finish = ok => {
    if(settled) return;
    settled = true;
    st.pend = false;
    clearTimeout(timer);
    if(ok){
      st.at = token;
      st.shownAt = token;
      st.fails = 0;
      st.since = 0;
      st.lastSrc = img.src;                  // 记住这张图，重渲直播墙时先贴回去，不再闪"等待第一帧"
      img.parentNode && img.parentNode.classList.add("hasphoto");
    }else{
      st.fails += 1;
      if(!st.since) st.since = token;        // 失败起点，用来区分"偶发一次"和"一直不通"
      /* 曾经成功出过图就别把画面清空：冻结上一帧 + 角标显示"未更新"，
         比整块翻回"等待第一帧"更像正常 NVR（0.9.88）。
         恢复 lastSrc 会触发一次 onload，但 settled 已置真，不会误判成刷新成功。 */
      if(st.lastSrc && img){
        img.src = st.lastSrc;
        img.parentNode && img.parentNode.classList.add("hasphoto");
      }else{
        img.parentNode && img.parentNode.classList.remove("hasphoto");
      }
    }
    onDone && onDone(ok);
  };
  const timer = setTimeout(()=> finish(false), SNAP_FAIL_MS);
  img.onload = ()=>{ if(img.getAttribute("data-tok") === String(token)) finish(true); };
  img.onerror = ()=>{ if(img.getAttribute("data-tok") === String(token)) finish(false); };
  img.setAttribute("data-tok", String(token));
  img.src = "/api/live/snapshot/" + encodeURIComponent(camId) + ".jpg?t=" + token;
}
