/* 样式源拆分与合成（0.9.123）
   ------------------------------------------------------------------
   这一层存在的前提是：**拆分不能改变部署形态**。设备上仍然只有一个 index.html，
   样式是构建时拼进去的；一旦有人改成 <link> 外链，手机端首屏会先闪一下白，
   离线安装的内网环境还多一个 404 的可能。

   要钉住的是这些"看起来正常"的坏法：

     A 漏拼   —— 少一个源文件，页面不报错，只是某一页版式整段消失
     B 顺序错 —— theme 若排到最后，令牌被后写的同特异性规则盖掉，主题色全变
     C 搬错   —— 复制粘贴时把 pages 的内容写进 base（文件头名字对不上内容）
     D 断样式 —— 源文件里出现 </style 或 <script，样式区被提前截断
     E 手改产物 —— 直接改 index.html 的样式区，下一次 build 静默覆盖掉

   跑法： node test/ui/css_split.test.js */

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const UI = path.join(ROOT, "app", "ui");
const CSS_DIR = path.join(UI, "css");
const ORDER = ["theme.css", "base.css", "components.css", "pages.css", "player.css"];

let pass = 0, fail = 0;
function ck(name, cond, detail){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name + (detail ? "  ← " + detail : "")); }
}

const rd = p => fs.readFileSync(p, "utf8").replace(/\r\n/g, "\n");
const html = rd(path.join(UI, "index.html"));

/* ---------- A 源文件齐全、非空 ---------- */
const got = fs.existsSync(CSS_DIR)
  ? fs.readdirSync(CSS_DIR).filter(n => n.endsWith(".css")).sort()
  : [];
ck("css/ 下正好五个源文件", got.length === ORDER.length && ORDER.every(n => got.includes(n)),
   JSON.stringify(got));
const extra = got.filter(n => !ORDER.includes(n));
ck("没有未纳入合成顺序的源文件（多一个就是改了不生效）", extra.length === 0, JSON.stringify(extra));

const bodies = {};
for(const n of ORDER){
  const p = path.join(CSS_DIR, n);
  bodies[n] = fs.existsSync(p) ? rd(p) : "";
  ck("源文件非空 " + n, bodies[n].trim().length > 200, String(bodies[n].length));
}

/* ---------- C 文件头与内容对得上 ---------- */
/* 每条源文件的头部第一行就是自己的文件名；搬错文件时这里是第一个响的地方 */
for(const n of ORDER){
  ck("文件头写了文件名 " + n, bodies[n].split("\n")[0].includes(n),
     bodies[n].split("\n")[0].slice(0, 60));
}

/* ---------- D 不会把样式区截断 ---------- */
for(const n of ORDER){
  const low = bodies[n].toLowerCase();
  ck("不含结束标签 " + n, !low.includes("</style") && !low.includes("<script"));
}

/* ---------- B/E 产物 = 按 ORDER 拼接 ---------- */
const BEGIN = "/* @@css:begin", END = "/* @@css:end */";
ck("样式区有且只有一个起始标记", html.split(BEGIN).length - 1 === 1,
   String(html.split(BEGIN).length - 1));
ck("样式区有且只有一个结束标记", html.split(END).length - 1 === 1,
   String(html.split(END).length - 1));
const i = html.indexOf(BEGIN), j = html.indexOf(END);
ck("标记顺序正确", i >= 0 && j > i);
ck("合成标记包在 <style> 里", html.indexOf("<style>") < i && html.lastIndexOf("</style>") > j);

const region = html.slice(i, j);
const concat = ORDER.map(n => bodies[n].replace(/^\n+|\n+$/g, "")).join("\n\n");
/* 标记行本身不算内容：去掉第一行后再比 */
const regionBody = region.slice(region.indexOf("\n") + 1);
ck("样式区内容 = 五个源文件按序拼接", regionBody.trim() === concat.trim(),
   `产物 ${regionBody.trim().length} 字符 / 拼接 ${concat.trim().length} 字符`);
ck("样式区不是空的（>1000 行）", regionBody.split("\n").length > 1000,
   String(regionBody.split("\n").length));

/* 顺序：五个文件头在产物里必须按 ORDER 依次出现（theme 排最后就会盖掉令牌） */
let cursor = 0, ordered = true;
for(const n of ORDER){
  const at = regionBody.indexOf("============ " + n);
  if(at < cursor){ ordered = false; break; }
  cursor = at;
}
ck("五个文件按 theme→base→components→pages→player 顺序出现", ordered);
ck("令牌在产物最前（:root 出现在第一个组件规则之前）",
   regionBody.indexOf(":root{") >= 0 && regionBody.indexOf(":root{") < regionBody.indexOf(".phone{"),
   regionBody.indexOf(":root{") + " / " + regionBody.indexOf(".phone{"));

/* ---------- 部署形态仍然是单文件 ---------- */
ck("页面没有外链样式表", !/<link[^>]+rel=["']?stylesheet/i.test(html));
ck("页面没有 @import", !/@import/.test(regionBody));

/* ---------- 旧产物不留残迹 ---------- */
/* 拆分期间标记行里曾写过一句"占位（下一步由 compose_ui.py 填入）"，合成没跑成它就会留着 */
ck("没有合成占位残留", !regionBody.includes("下一步由 compose_ui.py 填入"));

console.log("样式源拆分 " + (pass + fail) + " 项：通过 " + pass + "，失败 " + fail);
process.exit(fail ? 1 : 0);
