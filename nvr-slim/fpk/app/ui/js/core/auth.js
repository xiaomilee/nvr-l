/* ================= core/auth.js =================
   凭证：token 的存取、引导层开合，以及唯一那处全局 fetch 包装。
   这里的共同点是**页面还没渲染就得工作** —— 首屏每个 /api 请求都经过它，
   所以它必须排在最前面执行（合成顺序 = 标记在 index.html 里的位置）。
   反过来，凡是"某一页才用得到"的东西，不进这个文件。
   ================================================= */

/* ================= API 凭证（0.9.120） =================
   服务监听 0.0.0.0（手机直连才通），"同网段就能调接口"原本是默认状态。现在所有
   /api/* 都要凭证，这里负责三件事：
     1) 把 token 存在本机浏览器（换设备/清缓存最多再粘一次）；
     2) 一处包住 fetch，让所有请求自动带 Authorization —— 页面里的请求现在都
        走统一请求层（API.get/post），凭证只在这一处加，调用点一个都不用管；
     3) 401 时统一回到粘贴框，而不是让某个页面自己显示"加载失败"。
   注意 <img>/<video> 带不了请求头，撤给它们的是登录时服务端签出的 HttpOnly
   cookie（前端拿不到也删不掉，正合适）。 */
const AUTH = { token: "", on: false };
function authLoad(){ try{ return localStorage.getItem("nvr_api_token") || ""; }catch(e){ return ""; } }
function authSave(t){ try{ t ? localStorage.setItem("nvr_api_token", t)
                              : localStorage.removeItem("nvr_api_token"); }catch(e){} }
AUTH.token = authLoad();

function authGate(show, msg){
  AUTH.on = !!show;
  const g = document.getElementById("authGate");
  if(!g) return;
  g.classList.toggle("on", !!show);
  const e = document.getElementById("agErr");
  if(e) e.textContent = msg || "";
  if(show){ const i = document.getElementById("agTok"); if(i && i.focus) i.focus(); }
}

/* 全局拦截：只在真浏览器里装（node 测试桩没有 window.fetch，跳过即可）。 */
(function(){
  if(typeof window === "undefined" || typeof window.fetch !== "function") return;
  if(window.__nvrAuth) return;
  window.__nvrAuth = true;
  const raw = window.fetch.bind(window);
  window.fetch = function(input, init){
    const opt = Object.assign({}, init || {});
    if(AUTH.token && typeof Headers === "function"){
      const h = new Headers(opt.headers || {});
      if(!h.has("Authorization")) h.set("Authorization", "Bearer " + AUTH.token);
      opt.headers = h;
    }
    return raw(input, opt).then(r=>{
      if(r && r.status === 401) authGate(true, "凭证已失效，请重新粘贴 api_token");
      return r;
    });
  };
})();
