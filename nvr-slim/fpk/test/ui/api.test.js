/* 统一请求层 API.get / API.post（0.9.120）
   ------------------------------------------------------------------
   这一层存在的理由，是这些语义以前在 17 处裸 fetch 里各写各的、还都写漏了一点：
   漏 CSRF 头的那个按钮点了永远 403；没有超时的那几处遇到 NAS 假死就永远转圈；
   把旧版进程吐的 HTML 当 JSON 的那处，界面显示「没有日志」而不是「进程是旧的」。
   所以这里逐条锁住，改坏了得先听见响：

     A 超时：AbortController 真的被 abort，文案说得出「超时」而不是「网络错误」
     B 401 ：整机凭证失效 → 引导层弹出来；登录接口 allow401 → 不弹（那是「粘错了」）
     C JSON：解析不出来要抛 ApiError（带 status），绝不返回 null 冒充成功
     D CSRF：写操作一定带 X-NVR-Request；读操作一定不带 body

   再加一条结构守卫：除请求层（与凭证包装）之外，页面里不允许再出现裸 fetch ——
   这个 bug 的复发形态就是「某人顺手又写了一个 fetch」。

   跑法： node test/ui/api.test.js */
const { els, mkEl } = require("./harness.js");
const fs = require("fs"), path = require("path"), vm = require("vm");
const timers = require("timers");
const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");

let pass = 0, fail = 0;
function ck(name, cond, detail){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name + (detail ? "  ← " + detail : "")); }
}

/* 引导层要能记录 class 的增删（否则「401 时弹出引导」根本断言不了） */
const gate = els["authGate"] || (els["authGate"] = mkEl("authGate"));
gate._on = false;
gate.classList = {
  toggle(cls, on){ gate._on = !!on; },
  add(){}, remove(){}, contains(){ return gate._on; }
};

/* 给一句"什么都不做"的 fetch：页面启动链（syncFromServer / loadTopVersion /
   录像清单…）会各自发一次请求，用真 fetch 打相对 URL 只会造出一串没人接的
   拒绝，把后面的断言搅乱。这里让它拿到空 JSON 就收工 —— 既不联网，
   也不会在微任务里炸。 */
global.fetch = ()=> Promise.resolve({
  status: 200, ok: true,
  text: ()=> Promise.resolve("{}"),
  json: ()=> Promise.resolve({})
});

const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");
const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);

/* 跑完脚本先装一个"和气"的传输口：启动链（syncLink / loadTopVersion / 录像清单…）
   会在微任务里发请求，不接住就会漏进来搅乱后面的调用记账。 */
run(`API.transport = function(){ return Promise.resolve(
  { status: 200, ok: true, text: function(){ return Promise.resolve("{}"); } }); }`);

/* ---- 记账桩：把发请求换成记账，响应语义全部自己控制 ---- */
global.__calls = [];
global.__next = null;
const setNext = fn => { global.__next = fn; };
const last = () => global.__calls[global.__calls.length - 1];
const reset = () => { global.__calls.length = 0; };

/* 装成 Response 的样子：请求层读 status / ok / text() */
function resp(status, body){
  const txt = (typeof body === "string") ? body : JSON.stringify(body);
  return {
    status: status, ok: status >= 200 && status < 300,
    text: ()=> Promise.resolve(txt),
    json: ()=> Promise.resolve(JSON.parse(txt))
  };
}

(async ()=>{

/* 让启动链把该发的请求发完，再切到记账桩，免得它的请求混进断言 */
await new Promise(r=> setImmediate(r));
run(`API.transport = function(url, init){
  __calls.push({ url: url, init: init });
  return __next(url, init);
}`);
reset();

/* ================= D · 写操作必须带 CSRF 头，读操作不带 body ================= */
setNext(()=> Promise.resolve(resp(200, { ok: true })));
await run("API.get('/api/status')");
ck("GET 走 same-origin + no-store",
   last().init.method === "GET" && last().init.mode === "same-origin"
   && last().init.cache === "no-store", JSON.stringify(last().init));
ck("GET 自动加 _t 破缓存", /[?&]_t=\d+/.test(last().url), last().url);
ck("GET 不带 body", last().init.body === undefined);
ck("GET 不带 CSRF 头", last().init.headers === undefined);

reset();
setNext(()=> Promise.resolve(resp(200, { ok: true })));
await run("API.get('/api/ledger?cam=a&date=2026-11-08')");
ck("已有查询串时用 & 接 _t", /date=2026-11-08&_t=\d+/.test(last().url), last().url);

reset();
setNext(()=> Promise.resolve(resp(200, { ok: true })));
await run("API.post('/api/config', { a: 1 })");
ck("POST 带 X-NVR-Request（服务端 _write_ok 的硬门槛）",
   !!last().init.headers && last().init.headers["X-NVR-Request"] === "1",
   JSON.stringify(last().init.headers));
ck("POST 是 JSON 内容类型",
   last().init.headers["Content-Type"] === "application/json");
ck("POST 带 JSON body", last().init.body === '{"a":1}', last().init.body);
ck("POST 不拼 _t（写请求本来不进缓存，拼了只是让日志难读）",
   !/[?&]_t=/.test(last().url), last().url);

reset();
setNext(()=> Promise.resolve(resp(200, { ok: true })));
await run("API.post('/api/restart')");
ck("POST 空 body 也送 {}（服务端按 JSON 解析）", last().init.body === "{}", last().init.body);

/* ================= C · 非 JSON 响应绝不能当成功 ================= */
reset();
setNext(()=> Promise.resolve(resp(200, "<!doctype html><title>502</title>")));
let e1 = null;
try{ await run("API.get('/api/log')"); }catch(e){ e1 = e; }
ck("HTML 响应 → 抛 ApiError", !!e1 && e1.name === "ApiError", e1 && e1.name);
ck("HTML 响应保留真实 HTTP 状态（200 也要说清楚）", e1 && e1.status === 200, e1 && e1.status);
ck("错误文案带原文片段，能判断是中间层出错", !!e1 && /doctype/.test(e1.message), e1 && e1.message);

reset();
setNext(()=> Promise.resolve(resp(500, { ok: false })));
const d500 = await run("API.get('/api/boom')");
ck("500 且服务端没给 error → 补 HTTP 500 文案",
   !!d500 && d500.error === "HTTP 500", d500 && d500.error);

reset();
setNext(()=> Promise.resolve(resp(403, { ok: false, error: "缺少 X-NVR-Request 头" })));
const d403 = await run("API.get('/api/write')");
ck("服务端自己的 error 不被覆盖", d403.error === "缺少 X-NVR-Request 头", d403.error);

/* ================= B · 401 分两种：整机失效 vs 粘错 token ================= */
gate._on = false;
reset();
setNext(()=> Promise.resolve(resp(401, { ok: false, error: "token 不正确" })));
let e401 = null;
try{ await run("API.get('/api/status')"); }catch(e){ e401 = e; }
ck("401 → 抛错（调用点不会再拿 null 当成功）", !!e401, e401 && e401.name);
ck("401 → 标记 unauthorized 供调用点区分", !!(e401 && e401.unauthorized));
ck("401 → 引导层自动弹出", gate._on === true);

gate._on = false;
reset();
setNext(()=> Promise.resolve(resp(401, { ok: false, error: "token 不正确" })));
const dLogin = await run("API.post('/api/auth/login', { token: 'bad' }, { allow401: true })");
ck("登录接口 allow401：401 不当成凭证失效",
   !!dLogin && dLogin.ok === false && dLogin.error === "token 不正确", JSON.stringify(dLogin));
ck("登录接口 allow401：不弹引导（这句话该由登录框自己说）", gate._on === false);

/* ================= A · 超时 ================= */
/* harness 把 setTimeout 换成了空转桩（防止页面定时器在测试里乱跑），
   这一条要真的计时，临时换回 node 的实现。 */
const stubST = global.setTimeout;
global.setTimeout = (fn, ms)=> timers.setTimeout(fn, ms);
global.__aborted = false;
reset();
setNext((url, init)=> new Promise((res, rej)=>{
  ck("请求带上了 AbortSignal", !!init.signal);
  init.signal.addEventListener("abort", ()=>{
    global.__aborted = true;
    const err = new Error("aborted"); err.name = "AbortError"; rej(err);
  });
}));
let eT = null;
const t0 = Date.now();
try{ await run("API.get('/api/slow', { timeout: 60 })"); }catch(e){ eT = e; }
const waited = Date.now() - t0;
global.setTimeout = stubST;
ck("超时真的会 abort 掉请求", global.__aborted === true);
ck("超时 → ApiError", !!eT && eT.name === "ApiError", eT && eT.name);
ck("超时文案说得清是超时（不是网络错误）", !!eT && /超时/.test(eT.message), eT && eT.message);
ck("按传入的 timeout 收手（不等默认 15 秒）", waited < 1000, waited + "ms");
ck("超时错误 status=0（没拿到响应）", eT && eT.status === 0, eT && eT.status);

/* 传输层同步抛出（相对 URL 在无 base 的环境里就是这样）也要变成 reject，
   否则调用点拿到的是一句同步异常 —— 那句异常没人 catch。 */
reset();
setNext(()=> { throw new TypeError("Failed to parse URL"); });
let eN = null;
try{ await run("API.get('/api/x')"); }catch(e){ eN = e; }
ck("传输层同步抛出 → 也走 reject 通道", !!eN && eN.name === "ApiError", eN && eN.name);
ck("网络错误保留原因", !!eN && /Failed to parse URL/.test(eN.message), eN && eN.message);

/* ================= 结构守卫：页面里不允许再出现裸 fetch ================= */
/* 剥掉注释后再找 fetch( —— 注释里解释「这里包住 fetch」是正常的，
   正常代码里再写一个裸 fetch 就不正常了。 */
function bareFetchLines(source){
  const script = source.match(/<script>([\s\S]*?)<\/script>/g).pop()
                       .replace(/^<script>/, "").replace(/<\/script>$/, "");
  const out = [];
  let inBlock = false;
  script.split("\n").forEach((raw, i)=>{
    let text = raw;
    if(inBlock){
      const end = text.indexOf("*/");
      if(end < 0) return;
      text = text.slice(end + 2);
      inBlock = false;
    }
    const open = text.indexOf("/*");
    if(open >= 0){
      const close = text.indexOf("*/", open);
      if(close < 0){ inBlock = true; text = text.slice(0, open); }
      else text = text.slice(0, open) + text.slice(close + 2);
    }
    const m = /(^|[^.\w$])fetch\s*\(/.exec(text);
    if(!m) return;
    const at = m.index + m[1].length;
    const lc = text.indexOf("//");
    if(lc >= 0 && lc < at) return;                 // 被行注释掉的
    out.push({ line: i + 1, text: raw.trim() });
  });
  return out;
}
const bare = bareFetchLines(src);
ck("全页只剩传输层一处裸 fetch", bare.length === 1,
   JSON.stringify(bare.map(x=> x.line + ": " + x.text)));
ck("那一处就是请求层的传输口（换掉它测试才能不联网）",
   bare.length === 1 && /API\.transport/.test(bare[0].text), bare[0] && bare[0].text);
ck("页面里已经没有 apiPost / apiGet 的旧入口", !/\bapi(Post|Get)\s*\(/.test(src));

console.log("api: " + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);

})();
