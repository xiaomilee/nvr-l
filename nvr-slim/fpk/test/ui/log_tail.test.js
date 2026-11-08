/* 历史日志翻页（0.9.117）
   ------------------------------------------------------------------
   内存环只有 800 条（真机两三小时），查「昨天凌晨为什么断」过去只能登 NAS
   去 tail 文件。现在面板里能直接往前翻落盘日志。

   这里锁的是翻页语义，而不是像素：
   1) 「历史日志」切到文件模式，按后端返回的 start 作为下一页的 end；
   2) 「加载更早」把旧行**接在前面**（顺序不能反），并且视口要钉住；
   3) 翻到文件头 / 堆到行数上限时按钮必须点不动，不能让人无限点下去。

   跑法： node test/ui/log_tail.test.js */
const { els, mkEl } = require("./harness.js");
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");

let pass = 0, fail = 0;
function ck(name, cond){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name); }
}

/* ---- 假节点：pre 记账子节点，logbox 记账滚动位置 ---- */
const pre = mkEl("diagLogText");
let kids = [];
pre.appendChild = n => { kids.push(...(n && n._kids ? n._kids : [n])); };
const logbox = { scrollTop: 100, scrollHeight: 900, clientHeight: 190 };
pre.parentElement = logbox;
const nodes = {
  diagLogText: pre,
  diagLogMeta: mkEl("diagLogMeta"),
  btnLogWarn: mkEl("btnLogWarn"),
  btnLogHist: mkEl("btnLogHist"),
  btnLogMore: mkEl("btnLogMore"),
};
const origGet = global.document.getElementById;
global.document.getElementById = function(id){
  return nodes[id] || origGet.call(global.document, id);
};

/* ---- fetch 桩：**按 URL 分流**，不按调用顺序。
   页面启动时会自己拉 /api/retention、/api/config、/api/health、/api/log，
   用队列发响应会把测试数据喂给启动请求，断言就全乱了。 */
let calls = [], fileResp = null, memResp = null;
global.fetch = function(url){
  const u = String(url);
  calls.push(u);
  let next = { ok: true };
  if(u.indexOf("/api/log/file") === 0) next = fileResp || { ok: true, lines: [], start: 0, end: 0, size: 0, more: false };
  else if(u.indexOf("/api/log?") === 0) next = memResp || { ok: true, lines: [], warn: 0, err: 0 };
  /* 桩要装成 Response 的样子：统一请求层读 r.status / r.text()，
     只有 json() 的桩会让「非 JSON 响应」这条分支永远测不到。 */
  return Promise.resolve({
    status: 200, ok: true,
    headers: { get: () => "application/json" },
    text: () => Promise.resolve(JSON.stringify(next)),
    json: () => Promise.resolve(next)
  });
};

const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);

const L = n => Array.from({ length: n }, (_, i) => "[nvr] 01:00:0" + (i % 10) + " 第 " + i + " 行");
function rows(){ return kids.filter(n => n && n.className !== undefined
                                   && n.nodeValue === undefined); }
function lastUrl(p){ 
  for(let i = calls.length - 1; i >= 0; i--) if(calls[i].indexOf(p) === 0) return calls[i];
  return "";
}
/* 让启动链路的那几次 fetch 先落地，免得它的 loadDiagLog 在断言之后才回头改状态 */
const tick = () => new Promise(r => process.nextTick(r));
const more = nodes.btnLogMore, hist = nodes.btnLogHist, meta = nodes.diagLogMeta;

(async () => {
  for(let i = 0; i < 5; i++) await tick();

  /* ---- 1. 切到历史日志 ---- */
  fileResp = { ok: true, exists: true, lines: L(10), start: 5000, end: 6000,
               size: 204800, more: true };
  await run("loadLogFile(false)");
  ck("切到文件模式", run("LOGP.file") === true);
  ck("请求带页大小与时间戳", /\/api\/log\/file\?lines=\d+/.test(lastUrl("/api/log/file")));
  ck("第一页不带 end（要从文件尾读）", lastUrl("/api/log/file").indexOf("&end=") < 0);
  ck("载入返回的行", run("LOGP.lines.length") === 10);
  ck("记下起始字节（翻页用）", run("LOGP.fStart") === 5000);
  ck("文件大小记下来给标题用", run("LOGP.fSize") === 204800);
  ck("有更早的记录 → 按钮可用", more.disabled === false);
  ck("文件模式按钮可见", more.style.display !== "none");
  ck("标题写明是历史日志", /历史日志/.test(meta.textContent));
  ck("标题带文件体积", /200 KB/.test(meta.textContent));
  ck("切换按钮变成「回到实时」", /回到实时/.test(hist.textContent));

  /* ---- 2. 加载更早：接在前面，不能反 ---- */
  kids = [];
  fileResp = { ok: true, exists: true, lines: ["[nvr] 00:00:01 更早A", "[nvr] 00:00:02 更早B"],
               start: 4800, end: 5000, size: 204800, more: true };
  /* 模拟「用户已经往上滚到中间」：视口离底部 900px。翻页后这个距离必须不变 */
  logbox.scrollHeight = 1200; logbox.scrollTop = 300;
  await run("loadLogFile(true)");
  ck("翻页把 end 传成上一页的 start", /&end=5000/.test(lastUrl("/api/log/file")));
  ck("旧行接在前面", run("LOGP.lines.slice(0,2)").join("|") === "[nvr] 00:00:01 更早A|[nvr] 00:00:02 更早B");
  ck("原有行还在后面", run("LOGP.lines.length") === 12);
  ck("起点前移到 4800", run("LOGP.fStart") === 4800);
  ck("翻页保留视口（离底部的距离不变）", logbox.scrollTop === 300);

  /* ---- 3. 翻到文件头 ---- */
  fileResp = { ok: true, exists: true, lines: ["[nvr] 00:00:00 最早一行"],
               start: 0, end: 4800, size: 204800, more: false };
  await run("loadLogFile(true)");
  ck("到文件头 → more=false", run("LOGP.fMore") === false);
  ck("到文件头 → 按钮点不动", more.disabled === true);

  /* ---- 4. 行数上限 ---- */
  run("LOGP.lines = new Array(6000).fill('[nvr] x'); LOGP.fMore = true; renderLog();");
  ck("到 6000 行上限 → 按钮禁用", more.disabled === true);
  ck("到上限时按钮文案说清楚", /已到上限/.test(more.textContent));

  /* ---- 5. 文件模式的计数口径 ---- */
  kids = [];
  run("LOGP.file = true; LOGP.lines = ['[nvr] a', '[nvr] b [WARN] 失败', '[nvr] c [ERR] 挂了'];"
    + " LOGP.only = false; renderLog();");
  ck("文件模式数本页警告", /本页警告 1 \/ 错误 1/.test(meta.textContent));
  ck("文件模式不说「最近 N 行」", !/最近 \d+ 行/.test(meta.textContent));
  ck("本页着色仍然生效", rows().filter(s => s.className === "lw").length === 1
     && rows().filter(s => s.className === "le").length === 1);

  /* ---- 6. 只看警告在文件模式下也管用 ---- */
  kids = [];
  run("LOGP.only = true; renderLog();");
  ck("文件模式过滤只剩带标记的行", rows().length === 2);
  ck("过滤后按钮提示改文案", /显示全部/.test(nodes.btnLogWarn.textContent));

  /* ---- 7. 空文件 ---- */
  kids = [];
  run("LOGP.file = true; LOGP.lines = []; LOGP.only = false; LOGP.fMore = false; renderLog();");
  const empt = kids.filter(n => n && n.className === "empty")[0];
  ck("空文件给一句人话", !!empt && /日志文件是空的/.test(empt.textContent));

  /* ---- 8. 回到实时 ---- */
  memResp = { ok: true, lines: ["[nvr] 12:00:00 实时一行"], warn: 3, err: 1 };
  run("toggleLogSource()");
  for(let i = 0; i < 5; i++) await tick();   // toggleLogSource 内部没 await，等它把请求走完
  ck("回到实时：file 复位", run("LOGP.file") === false);
  ck("回到实时：重新拉内存环", /\/api\/log\?_t=/.test(lastUrl("/api/log?")));
  ck("回到实时：标题恢复「最近 N 行」", /最近 1 行/.test(meta.textContent));
  ck("回到实时：计数用服务端累计值", /警告 3 \/ 错误 1/.test(meta.textContent));

  /* ---- 9. 刷新在两种模式下走对接口 ---- */
  run("LOGP.file = true;");
  fileResp = { ok: true, lines: [], start: 0, end: 0, size: 10, more: false };
  await run("loadLogFile(false)");
  ck("历史模式下刷新读文件", lastUrl("/api/log/file") !== "");

  /* ---- 10. 结构就位 ---- */
  ck("面板有历史日志按钮", src.includes('id="btnLogHist"'));
  ck("面板有加载更早按钮", src.includes('id="btnLogMore"'));
  ck("加载更早默认隐藏（实时模式不该出现）", /id="btnLogMore"[^>]*display:none/.test(src));
  ck("历史按钮已绑定", /#btnLogHist[\s\S]{0,80}toggleLogSource/.test(src));
  ck("加载更早已绑定", /#btnLogMore[\s\S]{0,120}loadLogFile\(true\)/.test(src));
  ck("清除日志会退出历史模式", /async function clearDiagLog[\s\S]{0,400}LOGP\.file = false/.test(src));
  ck("禁用态有样式", src.includes(".logbtn[disabled]"));

  console.log("log_tail: " + pass + " passed, " + fail + " failed");
  process.exit(fail ? 1 : 0);
})();
