/* 脚本源拆分与合成（0.9.123）
   ------------------------------------------------------------------
   这一层的前提和样式那边一样：**拆分不能改变部署形态**。设备上仍然只有一个
   index.html、一个页面脚本标签；脚本是构建时按标记位置拼进去的。

   但脚本比样式更难对口：CSS 的层叠顺序可以"按名字定死"，脚本不行 ——
   const/let 不提升，声明出现的次序就是执行次序。所以合成顺序不是一张名字表，
   而是**标记在 index.html 里的位置**。这带来几个只有静默坏法的坑：

     A 漏拼   —— 少一个源文件，页面不报错，某个功能就是不响应
     B 顺序错 —— 挪一个标记，两段代码的执行次序就换了，报错信息会指向完全无关的行
     C 搬错   —— 复制粘贴把 api 的内容写进 theme（文件头名字对不上内容）
     D 断脚本 —— 源文件里出现 </script，整段脚本被提前截断，页面全白
     E 手改产物 —— 直接改 index.html 的脚本区，下一次 build 静默覆盖
     F 拆成多标签 —— test/ui/*.test.js 全都取"最后一个 <script>"当脚本源，
                     一旦变成两个标签，十几个测试会一声不响地跑错段（全绿，但没测到）

   跑法： node test/ui/js_split.test.js */

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const UI = path.join(ROOT, "app", "ui");
const JS_DIR = path.join(UI, "js");

/* 顺序即执行顺序。改这张表 = 改页面的执行次序，必须是"想清楚了才改"的动作，
   而不是顺手排个序。注意这三份 pages/ 并不挨在一起：clips 在 core/api 之前，
   设置页两段在它之后 —— 位置就是代码原本所在的位置，没有为了"看着整齐"挪动过。 */
const ORDER = ["core/auth.js", "core/format.js",
               "components/shell.js", "components/fullscreen.js", "components/nav.js",
               "pages/clips.js",
               "core/api.js",
               "pages/settings_cards.js", "pages/settings_editor.js",
               "core/theme.js"];

let pass = 0, fail = 0;
function ck(name, cond, detail){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name + (detail ? "  ← " + detail : "")); }
}

const rd = p => fs.readFileSync(p, "utf8").replace(/\r\n/g, "\n");
const html = rd(path.join(UI, "index.html"));

/* ---------- A 源文件齐全、非空 ---------- */
function walk(dir, rel){
  if(!fs.existsSync(dir)) return [];
  let out = [];
  for(const n of fs.readdirSync(dir)){
    const full = path.join(dir, n);
    const r = rel ? rel + "/" + n : n;
    if(fs.statSync(full).isDirectory()) out = out.concat(walk(full, r));
    else if(n.endsWith(".js")) out.push(r);
  }
  return out;
}
const got = walk(JS_DIR, "").sort();
ck("js/ 下正好 " + ORDER.length + " 个源文件", got.length === ORDER.length && ORDER.every(n => got.includes(n)),
   JSON.stringify(got));
ck("没有未纳入合成的源文件（多一个就是改了不生效）",
   got.filter(n => !ORDER.includes(n)).length === 0, JSON.stringify(got));

const bodies = {};
for(const n of ORDER){
  const p = path.join(JS_DIR, n.split("/").join(path.sep));
  bodies[n] = fs.existsSync(p) ? rd(p) : "";
  ck("源文件非空 " + n, bodies[n].trim().length > 200, String(bodies[n].length));
}

/* ---------- C 文件头写着自己的路径（防复制粘贴搬错） ---------- */
for(const n of ORDER){
  const head = bodies[n].split("\n").slice(0, 2).join("\n");
  ck("文件头写着它的路径 " + n, head.indexOf(n) >= 0, JSON.stringify(head.slice(0, 60)));
  ck("文件头写了取舍标准 " + n,
     /判断标准|为什么|放这里|不进这个文件|只从这里|唯一/.test(bodies[n].split("*/")[0]),
     "文件头没写取舍标准");
}

/* ---------- D 不会把宿主标签截断 ---------- */
for(const n of ORDER){
  ck("源文件里没有 </script " + n, bodies[n].toLowerCase().indexOf("</script") < 0);
  ck("源文件里没有合成标记 " + n, bodies[n].indexOf("@@js:") < 0);
}

/* ---------- B 标记成对、顺序固定、不嵌套 ---------- */
const lines = html.split("\n");
const begins = [], ends = [];
lines.forEach((ln, i) => {
  const m = ln.match(/^\/\* @@js:begin (\S+)/);
  if(m) begins.push({ i: i, name: m[1] });
  if(ln.trim() === "/* @@js:end */") ends.push(i);
});
ck("标记数量与源文件一致（begin/end 各 " + ORDER.length + " 处）",
   begins.length === ORDER.length && ends.length === ORDER.length,
   "begin=" + begins.length + " end=" + ends.length);
ck("每个源文件恰好一处 begin 标记",
   ORDER.every(n => begins.filter(b => b.name === n).length === 1),
   JSON.stringify(begins.map(b => b.name)));
ck("标记的出现顺序就是 ORDER（位置即执行顺序）",
   begins.map(b => b.name).join(",") === ORDER.join(","),
   begins.map(b => b.name).join(","));
ck("没有嵌套：每个 begin 之后、下一个 begin 之前必须有 end",
   begins.every((b, k) => {
     const nextBegin = k + 1 < begins.length ? begins[k + 1].i : lines.length;
     const e = ends.find(x => x > b.i);
     return e !== undefined && e < nextBegin;
   }));

/* ---------- E 产物 == 按序拼接（独立实现，不经 compose_ui.py） ---------- */
const regions = begins.map((b, k) => {
  const e = ends.find(x => x > b.i);
  return { name: b.name, body: lines.slice(b.i + 1, e).join("\n"), endLine: lines[e] };
});
for(const r of regions){
  const want = (bodies[r.name] || "").replace(/\n+$/, "");
  ck("标记区内容 = 源文件 " + r.name, r.body === want,
     "区 " + r.body.length + " 字符 / 源 " + want.length + " 字符");
  ck("标记行写着该文件的路径 " + r.name, lines[begins.find(b => b.name === r.name).i].indexOf(r.name) >= 0);
}
ck("end 标记只有一个写法", regions.every(r => r.endLine === "/* @@js:end */"));

/* ---------- F 部署仍是单文件、单脚本标签 ---------- */
ck("页面里没有 <script src>（外链会让首屏白一下，也让测试跑错段）",
   html.toLowerCase().indexOf("<script src") < 0);
const lastOpen = html.lastIndexOf("<script");
const lastClose = html.indexOf("</script>", lastOpen);
ck("全部 js 标记都落在最后一个 <script> 里", begins.every(b => {
  const at = lines.slice(0, b.i).join("\n").length;
  return at > lastOpen && at < lastClose;
}));
ck("最后一个 <script> 是真正干活的那段（含 authBoot 调用）",
   lines.slice(0, -1).join("\n").indexOf("authBoot();") > 0);

/* ---------- G 没被"顺手搬空" ---------- */
let mainScriptAt = -1;
lines.forEach((ln, i) => { if(ln.trim() === "<script>") mainScriptAt = i; });
ck("脚本区仍有未拆分的主体（> 8000 行，不是只剩几个壳）",
   mainScriptAt >= 0 && lines.length - mainScriptAt > 8000,
   mainScriptAt + " → " + lines.length);

console.log("脚本源拆分：" + pass + " 项通过" + (fail ? "，" + fail + " 项失败" : ""));
process.exit(fail ? 1 : 0);
