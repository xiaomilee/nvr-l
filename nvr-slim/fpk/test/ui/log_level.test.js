/* 运行日志分级 + 只看警告（0.9.116）
   ------------------------------------------------------------------
   真机 2 小时日志里 283 行「处理失败」、192 行「取流断开/重连」，和启动横幅
   是同一个级别、同一个颜色。用户截图发过来时，正好没截到那几行 —— 排障等于瞎。

   这里锁三件事：
   1) 行级着色按 [WARN]/[ERR] 标记走，纯文本行不着色；
   2) 「只看警告」过滤只留带标记的行，计数与标题跟着变；
   3) 渲染用逐行 textContent，不拼 HTML —— 日志里有 < > & 也不能被解析。

   跑法： node test/ui/log_level.test.js */
const { els, mkEl } = require("./harness.js");
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");
const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);

let pass = 0, fail = 0;
function ck(name, cond){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name); }
}

/* ---- 把日志面板的三个节点换成会记账的假节点 ---- */
const pre = mkEl("diagLogText");
let kids = [];
pre.appendChild = n => { kids.push(...(n && n._kids ? n._kids : [n])); };
const meta = mkEl("diagLogMeta");
const btn = mkEl("btnLogWarn");
const origGet = global.document.getElementById;
global.document.getElementById = function(id){
  if(id === "diagLogText") return pre;
  if(id === "diagLogMeta") return meta;
  if(id === "btnLogWarn") return btn;
  return origGet.call(global.document, id);
};

const L = [
  "[nvr] 01:00:00 v0.9.116 pid=12 listen=0.0.0.0:18080",
  "[nvr] 01:00:05 扫描完成：/vol2 下 7949 个分片",
  "[nvr] 01:02:11 [WARN] 取流 院子R 断开（rc=0），3 秒后第 1262 次重连",
  "[nvr] 01:02:12 [ERR] 录像目录不可写 /vol2/nvr：Permission denied",
  "[nvr] 01:03:00 [WARN] 处理失败 cam-x/18-45-00.mp4：ffmpeg exited 1",
];
function render(lines, only){
  kids = [];
  run("LOGP.lines = " + JSON.stringify(lines) + ";");
  run("LOGP.warn = " + lines.filter(t => t.indexOf("[WARN] ") >= 0).length + ";");
  run("LOGP.err = " + lines.filter(t => t.indexOf("[ERR] ") >= 0).length + ";");
  run("LOGP.only = " + (only ? "true" : "false") + ";");
  run("renderLog();");
  return kids.filter(n => n && n.className !== undefined && n.textContent !== undefined
                          && n.nodeValue === undefined);
}

/* ---- 1. 单行判色 ---- */
ck("纯文本行不着色", run('logLineClass("[nvr] 01:00:00 扫描完成")') === "");
ck("WARN 行 → lw", run('logLineClass("[nvr] 01:02:11 [WARN] 取流 院子R 断开")') === "lw");
ck("ERR 行 → le", run('logLineClass("[nvr] 01:02:12 [ERR] 无法写入")') === "le");
ck("ERR 优先于 WARN（同时出现时取更严重的）",
   run('logLineClass("[nvr] 01:02:12 [ERR] [WARN] x")') === "le");

/* ---- 2. 全量渲染 ---- */
let spans = render(L, false);
ck("全量渲染行数不变（5 条）", spans.length === 5);
ck("全量渲染顺序不变", spans[0].textContent.indexOf("v0.9.116") > 0
   && spans[4].textContent.indexOf("ffmpeg exited 1") > 0);
ck("两行 WARN 上色", spans.filter(s => s.className === "lw").length === 2);
ck("一行 ERR 上色", spans.filter(s => s.className === "le").length === 1);
ck("纯文本行 className 为空（走默认色）", spans[1].className === "");
ck("标题显示行数与计数",
   /最近 5 行/.test(meta.textContent) && /警告 2/.test(meta.textContent)
   && /错误 1/.test(meta.textContent));
ck("按钮提示可筛的条数", /只看警告 \(3\)/.test(btn.textContent));

/* ---- 3. 只看警告 ---- */
spans = render(L, true);
ck("过滤后只剩带标记的 3 行", spans.length === 3);
ck("过滤后没有纯文本行", !spans.some(s => s.className === ""));
ck("过滤后 WARN/ERR 都还在",
   spans.filter(s => s.className === "lw").length === 2
   && spans.filter(s => s.className === "le").length === 1);
ck("过滤态按钮改为「显示全部」", /显示全部/.test(btn.textContent));

/* ---- 4. 空态 ---- */
kids = [];
run("LOGP.lines = []; LOGP.warn = 0; LOGP.err = 0; LOGP.only = true; renderLog();");
const empt = kids.filter(n => n && n.className === "empty")[0];
ck("无警告时不留空白，给一句人话",
   !!empt && /没有警告或错误/.test(empt.textContent));

/* ---- 5. 不拼 HTML ---- */
const fnSrc = run("renderLog.toString()");
ck("渲染不用 innerHTML（日志里的 < > & 一律当字面量）",
   !/innerHTML/.test(fnSrc));
ck("渲染逐行用 textContent", /textContent\s*=/.test(fnSrc));
const risky = ['[nvr] 01:05:00 [WARN] 快照失败：<b>x</b> & "y"'];
spans = render(risky, false);
ck("尖括号不被解析成标签", spans[0].textContent === risky[0]);

/* ---- 6. 结构就位 ---- */
ck("面板有计数位", src.includes('id="diagLogMeta"'));
ck("面板有筛选按钮", src.includes('id="btnLogWarn"'));
ck("筛选按钮已绑定", /#btnLogWarn[\s\S]{0,120}LOGP\.only/.test(src));
ck("着色样式存在", src.includes(".logbox pre .lw") && src.includes(".logbox pre .le"));
ck("筛选按钮有按下态", src.includes(".logbtn.on"));
ck("数据仍取 lines（不再是拼好的 text）", /d\.lines/.test(src));

console.log("log_level: " + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
