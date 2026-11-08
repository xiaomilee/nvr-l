/* ================= core/auth_boot.js =================
   凭证引导层：粘贴 token、校验、写入，以及页面启动时先问 /api/auth/status
   再决定要不要弹引导（authBoot）。
   判断标准：只回答「现在有没有凭证、没有时怎么让用户补上」；凭证怎么存、
   请求头怎么带是 core/auth.js 的事，不进这一份。
   为什么必须排在最后：先确认凭证、页面再去拉数据 —— 没有凭证时每个 /api
   都是 401，越早盖住引导层，首屏越不会先闪一片「加载失败」再弹框。
   ======================================================== */

/* ================= 凭证启动顺序（0.9.120） =================
   先确认凭证，页面再去拉数据：没有凭证时每个 /api 都是 401，越早盖住引导层，
   首屏越不会先闪一片"加载失败"再弹框。 */
function authSubmit(){
  const i = document.getElementById("agTok");
  const t = (i && i.value ? i.value : "").trim();
  if(!t){ authGate(true, "先粘贴 api_token"); return; }
  const b = document.getElementById("agGo");
  if(b) b.disabled = true;
  /* allow401：这里是"你粘错了 token"，不是"会话过期"——别让请求层抢先把
     引导层改成"凭证已失效"，那会让用户以为凭证坏了，反复去轮换。 */
  API.post("/api/auth/login", { token: t }, { allow401: true }).then(d=>{
    if(b) b.disabled = false;
    if(d && d.ok){
      authSave(t); AUTH.token = t;
      authGate(false);
      /* 重新走一遍首屏：此刻登录响应已经把 cookie 种上了，实时流/快照/回放
         才能跟着通。只关掉遮罩不重载的话，已经失败的图片不会自己重来。 */
      location.reload();
      return;
    }
    authGate(true, (d && d.error) || "token 不正确");
  }).catch(err=>{
    if(b) b.disabled = false;
    authGate(true, "无法连接服务：" + ((err && err.message) || err));
  });
}

function authBoot(){
  /* node 测试桩里没有 window.fetch：直接跳过，别让测试去发真请求 */
  if(typeof window === "undefined" || typeof window.fetch !== "function") return;
  const go = document.getElementById("agGo");
  if(go) go.onclick = authSubmit;
  const inp = document.getElementById("agTok");
  if(inp) inp.onkeydown = e=>{ if(e.key === "Enter") authSubmit(); };

  if(AUTH.token){
    /* 存着 token：先用它换一枚新 cookie。cookie 只有 30 天而 token 一直留在本地，
       只带 Bearer 能读接口，但 <img>/<video> 会 401 —— 表现是"页面正常、画面全黑"。 */
    API.post("/api/auth/login", { token: AUTH.token }, { allow401: true }).then(d=>{
      if(d && d.ok){ authGate(false); return; }
      authSave(""); AUTH.token = "";
      authGate(true, "保存的凭证已失效，请重新粘贴");
    }).catch(()=> authGate(true, "无法连接服务"));
    return;
  }
  API.get("/api/auth/status")
    .then(d=>{
      if(d && d.authed){ authGate(false); return; }   // 同一浏览器已登录过
      /* 把凭证文件路径写到引导层上：用户得知道去哪找那串 token */
      if(d && d.token_file){
        const p = document.getElementById("agPath");
        if(p) p.textContent = d.token_file;
      }
      authGate(true, "");
    })
    .catch(()=>{});
}
authBoot();
