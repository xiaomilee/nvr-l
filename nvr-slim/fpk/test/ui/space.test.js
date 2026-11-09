/* 空间预警的前端回归（0.9.131，无浏览器）。

   守的是三件事：
     1. 等级与文案由服务端给，前端不重算 —— 所以这里只验"渲染对不对"，
        不在这里复算阈值（复算就等于又造了第二份规则）。
     2. 该出现的地方都要出现：诊断存储卡（展开）、折叠摘要、直播页头部。
     3. 不该出现的地方一处都不许出现：ok / off / 还没算过。

   用法：node test/ui/space.test.js */
require("./harness.js");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = path.join(__dirname, "..", "..", "app", "ui", "index.html");
const src = fs.readFileSync(HTML, "utf8").replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g) || [];
if(!blocks.length){ console.error("找不到 <script> 块"); process.exit(1); }
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");

let pass = 0, fail = 0;
function ck(name, cond){
  if(cond){ pass++; }
  else { fail++; console.log("  FAIL " + name); }
}
const ctx = vm.createContext(global);
try{
  vm.runInContext(code, ctx, { filename: "index.html" });
}catch(e){
  console.error("顶层执行失败：" + e.message);
  process.exit(1);
}
const run = s => vm.runInContext(s, ctx);
const get = s => vm.runInContext(s, ctx);

/* ---- 1. 接线：这一版新增的四处都得真的接上 ---- */
ck("设置卡有阈值输入", src.includes('id="spaceWarnGB"'));
ck("阈值随 pushCfg 上送", src.includes("space_warn_gb:"));
ck("阈值从服务端回读", src.includes("REC.spaceWarnGB = +c.space_warn_gb || 0"));
ck("状态轮询收下 space 结论", src.includes("if(d.space) REC.space = d.space;"));
ck("阈值变更立刻重算一次", src.includes("loadStorageUsage(true)"));
ck("默认阈值 20（前端兜底展示值）", get("REC.spaceWarnGB") === 20);

/* ---- 2. spaceStrip：只在需要的时候出声 ---- */
ck("ok 不出现", get('spaceStrip({level:"ok"})') === "");
ck("off 不出现", get('spaceStrip({level:"off"})') === "");
ck("没有结论不出现", get("spaceStrip(null)") === "" && get("spaceStrip({})") === "");

const crit = get(`spaceStrip({level:"critical",free_gb:7.6,total_gb:55,rate_gb:12,
  rate_label:"最近 3 个完整日",need_gb:36,keep_text:"全时 3 天",headroom_gb:6.9,
  reasons:[{code:"free_critical",level:"critical",text:"只剩 7.6 GB"}],
  advice:"在「保留策略」里预览并清理过期目录"})`);
ck("告急有条", crit.includes("空间告急"));
ck("告急用 crit 配色", crit.includes("spw crit"));
ck("告急写清四个数", crit.includes("剩余 7.6 / 55 GB") && crit.includes("每天约 12 GB")
   && crit.includes("保留 全时 3 天 约需 36 GB") && crit.includes("稳态余量 6.9 GB"));
ck("告急给出理由", crit.includes("只剩 7.6 GB"));
ck("告急给出建议", crit.includes("建议：") && crit.includes("预览并清理"));

const warn = get(`spaceStrip({level:"warn",free_gb:21,total_gb:500,rate_gb:40,keep_text:"全时 3 天",
  need_gb:120,reasons:[{text:"稳态余量只剩 4.2 GB"}]})`);
ck("偏低用 warn 配色", warn.includes("spw warn"));
ck("偏低也有理由", warn.includes("稳态余量只剩 4.2 GB"));

const unk = get('spaceStrip({level:"unknown",reasons:[{text:"读不到磁盘容量"}]})');
ck("未知照实说", unk.includes("空间状态未知") && unk.includes("读不到磁盘容量"));

/* 文案里出现尖括号必须转义：这些文字来自服务端拼的字符串，不是 HTML */
const esc1 = get('spaceStrip({level:"warn",reasons:[{text:"<b>x</b>"}]})');
ck("理由做了转义", esc1.includes("&lt;b&gt;") && !esc1.includes("<b>x</b>"));

/* ---- 3. 三处显示位置 ---- */
run('REC.space={level:"critical",free_gb:7.6,total_gb:55};');
ck("折叠摘要标红", get("diagSummary()").includes("空间告急"));
ck("折叠摘要用告急色", get("diagSummary()").includes("var(--danger)"));

run('REC.space={level:"warn"};');
ck("折叠摘要标黄", get("diagSummary()").includes("空间偏低")
   && get("diagSummary()").includes("var(--warn)"));

run('REC.space={level:"ok"};');
ck("正常时不加字", !get("diagSummary()").includes("空间"));

run('REC.space={};');
ck("还没算过时不加字", !get("diagSummary()").includes("空间"));

run('REC.space={level:"critical"};');
ck("直播头有告急角标", get("spaceChip()").includes("空间告急")
   && get("spaceChip()").includes("spchip critical"));
run('REC.space={level:"warn"};');
ck("直播头有偏低角标", get("spaceChip()").includes("空间偏低"));
run('REC.space={level:"ok"};');
ck("正常时直播头不加角标", get("spaceChip()") === "");
run('REC.space={}; delete REC.space; REC.space={};');
ck("未算过时直播头不加角标", get("spaceChip()") === "");

/* ---- 4. 设置卡里的输入框跟着当前值走 ---- */
run('REC.spaceWarnGB=35;');
ck("输入框显示当前阈值", get("cardRec()").includes('value="35"'));
run('REC.spaceWarnGB=0;');
ck("0（关闭）也能显示", get("cardRec()").includes('value="0"'));
ck("输入框写明 0 = 关闭", get("cardRec()").includes("0 = 关闭"));

console.log((fail ? "FAIL" : "OK") + "  space  " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
