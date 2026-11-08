/* ================= core/api.js =================
   统一请求层：超时、401、JSON、CSRF 四条语义只写一遍，全页只从这里出去。
   为什么值得单独成文件：这个 bug 的复发形态是"某人顺手又写了一个 fetch"，
   藏在一万行里根本看不见，摆在独立文件里才会在 diff 里刺眼。
   依赖 core/auth.js 的 authGate —— 它在本文件之后，靠函数声明提升没关系，
   但别把这个方向反过来（请求层不该被凭证层依赖）。
   ================================================== */

/* ================= 统一请求层（0.9.120） =================
   以前 17 处裸 fetch 各自处理超时、错误、CSRF 头和缓存，结果各不相同：
   有的漏了 X-NVR-Request（写接口一律 403，界面只显示"发送失败"），绝大多数
   没有超时（NAS 假死时页面就永远转圈——不是报错，是更糟的沉默），还有几处
   把旧版进程吐出的 HTML 错误页当 JSON 解析。现在所有请求都从这里出去，
   四条语义只写一遍：

     超时   AbortController，默认 15 秒；试录/像素自检/归档这类慢接口自己放宽。
     401    整台设备都不认这份凭证，不是"这个页面加载失败" → 统一回粘贴框。
            登录接口自身除外（allow401）：那里的 401 恰好是"你粘错了 token"，
            调用点要给出的是这句话，不是"凭证已失效"。
     JSON  解析不出来就抛 ApiError（带 HTTP 状态），绝不返回 null 冒充成功。
     CSRF  写操作一律带 X-NVR-Request，服务端 _write_ok 的硬门槛。

   凭证头不在这里拼：上面那处全局 fetch 包装已经统一加 Authorization，
   两件事分开才不会出现第三个地方去拼请求头。 */
const API = { timeout: 15000 };

class ApiError extends Error {
  constructor(msg, status, payload){
    super(msg);
    this.name = "ApiError";
    this.status = status | 0;      // 0 = 没拿到响应（超时/网络层），其余是真实 HTTP 码
    this.payload = payload || null;
  }
}

/* 底层传输留一个可替换的口子：单测要在不联网的前提下验证上面四条语义
   （否则每条断言都得先起一个真服务）。默认就是全局 fetch，浏览器里因此
   仍然会经过凭证包装。 */
API.transport = function(url, init){ return fetch(url, init); };

API.req = function(path, opt){
  const o = opt || {};
  const method = String(o.method || "GET").toUpperCase();
  let url = String(path || "");
  if(method === "GET" && url.indexOf("_t=") < 0){
    /* 破坏缓存的 _t：WebView 曾把空录像清单缓存住，回放页永远"无视频文件"。
       放在这一层，调用点不必每次自己拼（拼漏一处就是一个查不出来的旧数据）。 */
    url += (url.indexOf("?") < 0 ? "?" : "&") + "_t=" + Date.now();
  }
  const init = { method: method, mode: "same-origin", cache: "no-store" };
  if(method !== "GET"){
    /* 写操作必须带自定义头：服务端靠"跨站发不了自定义头"挡 CSRF，
       漏一个的表现是"这个按钮点了没反应"，所以统一放这里。 */
    init.headers = { "Content-Type": "application/json", "X-NVR-Request": "1" };
    init.body = JSON.stringify(o.body == null ? {} : o.body);
  }
  const ms = o.timeout || API.timeout;
  let ctl = null, timer = 0;
  if(typeof AbortController === "function"){
    ctl = new AbortController();
    init.signal = ctl.signal;
    /* fetch 不会因为"对方不回"而失败，NAS 卡住时能挂到浏览器默认的几分钟。 */
    timer = setTimeout(()=>{ try{ ctl.abort(); }catch(e){} }, ms);
  }
  const done = ()=>{ if(timer) clearTimeout(timer); };
  /* 包一层 Promise.resolve()：传输层若同步抛出（相对 URL 在 node 桩里就是这样），
     也要走 reject 通道，否则调用点拿到的是一句同步异常。 */
  return Promise.resolve()
    .then(()=> API.transport(url, init))
    .then(r=>{
      if(r && r.status === 401 && !o.allow401){
        authGate(true, "凭证已失效，请重新粘贴 api_token");
        const e = new ApiError("凭证已失效（401）", 401);
        e.unauthorized = true;
        throw e;
      }
      return r.text().then(txt=>{
        let d = null;
        try{ d = txt ? JSON.parse(txt) : null; }catch(e){ d = null; }
        if(d === null){
          /* 服务端保证错误也是 JSON；解析不出来说明中间层出了事（旧版进程把
             接口当静态文件交回 HTML、反代 502 页面）。必须让调用方知道这次
             真的失败了，而不是拿 null 当"成功但没有数据"。 */
          throw new ApiError("HTTP " + r.status + " " + String(txt || "").slice(0, 80), r.status, txt);
        }
        if(!r.ok && d && d.error === undefined) d.error = "HTTP " + r.status;
        return d;
      });
    })
    .catch(e=>{
      if(e && (e.unauthorized || e instanceof ApiError)) throw e;
      const aborted = e && (e.name === "AbortError" || e.code === 20);
      throw new ApiError(aborted ? ("请求超时（" + Math.round(ms / 1000) + " 秒没响应）")
                                 : ((e && e.message) || "网络错误"), 0);
    })
    .then(d=>{ done(); return d; }, e=>{ done(); throw e; });
};
API.get  = (path, opt)=> API.req(path, Object.assign({ method: "GET" }, opt || {}));
API.post = (path, body, opt)=> API.req(path, Object.assign({ method: "POST", body: body }, opt || {}));
