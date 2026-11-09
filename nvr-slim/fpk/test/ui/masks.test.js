/* 画面区域：忽略区 + 只统计区（0.9.128 批次 10）
   ------------------------------------------------------------------
   守四件事：

   1. **两块区域不许互相吞掉**：`normMasks` 必须同时保留 ignore 与 include。
      以前这个函数只认 ignore，多出来的键一律丢 —— 前端画得再漂亮，推给
      服务端时也会被悄悄抹掉，那正是「设置页画了、判定不理」的老毛病。
   2. **空 = 不限制**：include 为空就是整幅画面，与 0.9.127 行为一致；
      老配置（只有 ignore）读进来必须是 include:[]，不能是 undefined。
   3. **格号是集合**：去重 + 丢越界 + 升序。顺序不同但内容相同时必须算
      「没改动」，否则保存后 dirtyCount 会永远显示"已改 1 项"。
   4. **界面说得出两块钱**：清单、计数、只读摘要都要分得清琥珀（忽略）
      与绿（只统计），并且各自能清空。

   跑法： node test/ui/masks.test.js */
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
const J = v => JSON.stringify(v);

let pass = 0, fail = 0;
function ck(name, cond){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name); }
}

/* ---- normMasks：两块都要留住 ---- */
ck("两块都保留",
   J(run('normMasks({ignore:[1,2],include:[10,11]})')) === '{"ignore":[1,2],"include":[10,11]}');
ck("老配置只有 ignore → include 补成空数组（不是 undefined）",
   J(run('normMasks({ignore:[3]})')) === '{"ignore":[3],"include":[]}');
ck("空/脏入参 → 两块皆空",
   J(run('normMasks(null)')) === '{"ignore":[],"include":[]}'
   && J(run('normMasks(undefined)')) === '{"ignore":[],"include":[]}');

/* ---- 清洗：去重、丢越界、升序 ---- */
ck("重复格号去重",
   J(run('normMasks({include:[5,5,5]}).include')) === "[5]");
ck("越界格号丢掉（-1 与 144 都不存在）",
   J(run('normMasks({include:[-1,0,143,144,999]}).include')) === "[0,143]");
ck("非整数丢掉",
   J(run('normMasks({include:[1.5,"x",null,7]}).include')) === "[7]");
ck("数字字符串认（手改配置常见）",
   J(run('normMasks({include:["9"]}).include')) === "[9]");
ck("升序：同一块区域换个画法要算没改动",
   J(run('normMasks({include:[30,4,17]}).include')) === "[4,17,30]");
ck("两块各自独立清洗（不会串到对方身上）",
   J(run('normMasks({ignore:[2,2],include:[1]})')) === '{"ignore":[2],"include":[1]}');

/* ---- 旧自由框数组格式：只迁到 ignore，不能凭空造出 include ---- */
ck("旧数组格式 → 换算成 ignore，include 保持空",
   J(run('normMasks([{x:0,y:0,w:0.0625,h:0.1112}])')) === '{"ignore":[0],"include":[]}');
ck("旧格式里 type=redact 的框仍然跳过",
   run('normMasks([{type:"redact",x:0,y:0,w:1,h:1}]).ignore').length === 0);

/* ---- 三张画布 → 各自负责哪一块 ---- */
ck("maskKindOf：读图摘要两块都画", run('maskKindOf("maskCv")') === "both");
ck("maskKindOf：忽略区画布", run('maskKindOf("maskCv-ig")') === "ignore");
ck("maskKindOf：只统计区画布", run('maskKindOf("maskCv-inc")') === "include");

/* ---- 编辑动作：两块互不覆盖 ---- */
run('SET.editing = true; SET.draft = { masks: normMasks({ignore:[0], include:[]}) };'
    + 'state.camId = "cam-01";');
run('gdrag = { kind:"include", cv:{}, c0:2, r0:0, c1:4, r1:1, moved:true }; finishGridDrag();');
ck("拖进只统计区：写进 include",
   J(run('SET.draft.masks.include')) === "[2,3,4,18,19,20]");
ck("拖进只统计区：ignore 一格没动",
   J(run('SET.draft.masks.ignore')) === "[0]");

run('gdrag = { kind:"ignore", cv:{}, c0:0, r0:0, c1:0, r1:0, moved:false }; finishGridDrag();');
ck("单击已选的忽略格 → 取消选中",
   J(run('SET.draft.masks.ignore')) === "[]");
ck("取消忽略格不影响只统计区",
   J(run('SET.draft.masks.include')) === "[2,3,4,18,19,20]");

/* ---- 清单与计数：两块分开显示、各自能清空 ---- */
run('SET.draft.masks = normMasks({ignore:[0,1], include:[10,11,12]}); renderMasks();');
ck("忽略区清单显示 2 格",
   run('document.getElementById("mlist").innerHTML').indexOf("忽略区 2 格") >= 0);
ck("只统计区清单显示 3 格",
   run('document.getElementById("ilist").innerHTML').indexOf("只统计区 3 格") >= 0);
ck("两块清单各有清空入口",
   run('document.getElementById("mlist").innerHTML').indexOf('data-clrmask="ignore"') >= 0
   && run('document.getElementById("ilist").innerHTML').indexOf('data-clrmask="include"') >= 0);
ck("只统计区用绿点、忽略区用琥珀，颜色不能搞混",
   run('document.getElementById("ilist").innerHTML').indexOf("#3ecf8e") >= 0
   && run('document.getElementById("mlist").innerHTML').indexOf("#e0a33a") >= 0);

/* ---- 只读摘要：两块都要说出来 ---- */
ck("摘要里有忽略区与只统计区两行",
   run('maskCountLine({ignore:[1],include:[2,3]})').indexOf("忽略区 1 格") >= 0
   && run('maskCountLine({ignore:[1],include:[2,3]})').indexOf("只统计区 2 格") >= 0);
ck("两块都空时说清楚是「整幅画面都算」，不能说「没划定区域」了事",
   run('maskCountLine({ignore:[],include:[]})').indexOf("整幅画面都算") >= 0);

/* ---- 诊断页：区外被挡掉的框数必须出现在每路一行里 ---- */
ck("诊断页保留「区外忽略」这一项",
   code.indexOf("区外忽略") > 0);
ck("诊断页读的是 per_cam.outside（不是本地估的）",
   code.indexOf("c.outside") > 0);

/* ---- 推送：masks 真的进 pushCfg body（推了才算设了） ---- */
ck("pushCfg 带 masks 且经过 normMasks",
   code.indexOf("masks: normMasks(p.masks)") > 0);

console.log((fail ? "FAIL " : "OK ") + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
