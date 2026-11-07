/* 凭证引导（0.9.120）
   ------------------------------------------------------------------
   守两件事：

   1. **引导层是真能开合的**：所有接口都要凭证之后，如果这层打不开，
      用户看到的就是一屏"加载失败"，而且没有任何输入 token 的地方 ——
      等于把设备锁死。所以这里断言 class 真的被加/去掉了。
   2. **token 存得住也清得掉**：轮换凭证或服务端拒绝后必须能就地抹掉，
      否则每次打开页面都拿一枚废 token 去撞 401。

   注意：authBoot 在 node 桩里会直接返回（桩没有 window.fetch），真浏览器里
   才去请求 /api/auth/status。这里只测不依赖网络的纯逻辑。

   跑法： node test/ui/auth.test.js */
const { els, mkEl } = require("./harness.js");
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");
const ctx = vm.createContext(global);
/* 引导层要能被 getElementById 找到，且要记录 class 的增删 */
const gate = els["authGate"] || (els["authGate"] = mkEl("authGate"));
gate._on = false;
gate.classList = {
  toggle(cls, on){ gate._on = !!on; },
  add(){}, remove(){}, contains(){ return gate._on; }
};
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);

let pass = 0, fail = 0;
function ck(name, cond, detail){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name + (detail ? "  ← " + detail : "")); }
}

/* ---- 引导层开合 ---- */
ck("初始不显示", gate._on === false);
run('authGate(true, "需要凭证")');
ck("authGate(true) 打开引导", gate._on === true);
ck("错误文案写在引导层里", els["agErr"].textContent === "需要凭证",
   els["agErr"].textContent);
run('authGate(false)');
ck("authGate(false) 收起引导", gate._on === false);
ck("收起时清空旧错误", !els["agErr"].textContent);

/* ---- token 往返 ---- */
ck("默认没有 token", run('AUTH.token') === "");
run('authSave("tok-abc"); AUTH.token = "tok-abc";');
ck("存进 localStorage", run('localStorage.getItem("nvr_api_token")') === "tok-abc");
ck("读回来带得出 token", run('(authLoad())') === "tok-abc");
run('authSave(""); AUTH.token = "";');
ck("清得掉", run('localStorage.getItem("nvr_api_token")') === null);

/* ---- 全局 fetch 包装不该在桩里装 ---- */
ck("桩环境不劫持 fetch（避免测试发真请求）", run('typeof window.__nvrAuth') === "undefined");

/* ---- authBoot 在桩里必须安静返回，不能抛 ---- */
let threw = null;
try{ run('authBoot()'); }catch(e){ threw = e; }
ck("authBoot 在无 fetch 环境直接返回", threw === null, threw && threw.message);

console.log(pass + " 项通过" + (fail ? "，" + fail + " 项失败" : ""));
process.exit(fail ? 1 : 0);
