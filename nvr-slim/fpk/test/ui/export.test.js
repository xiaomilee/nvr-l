/* 文件页「导出时间段」（0.9.127）
   ------------------------------------------------------------------
   守四件事：

   1. **两套口径只留一处**：预览走 `/api/export/plan`，下载走服务端在计划里
      给的 `url`。前端不再拼一遍下载地址 —— 拼第二遍就会出现"预览说 2 个
      分片、下载却给了 3 个"这种对不上的事。
   2. **多给了多少要说出来**：分片原样导出必然在两头多出几十秒，提示里必须
      有"边界多出 前 x / 后 y"，不能只说"3 个分片"。
   3. **导不了就是导不了**：服务端回 ok:false 时按钮必须禁用，只显示原因，
      不显示"请稍后重试"那种等于没说的话。
   4. **日期跟着文件页走**：不在面板里另开日期选择器（那会让人怀疑到底导的
      是哪一天）。

   跑法： node test/ui/export.test.js */
const { els } = require("./harness.js");
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

/* ---- 结构守卫 ---- */
ck("文件页有导出入口", src.indexOf('id="btnExport"') > 0);
ck("面板默认收起", /id="expBox"[^>]*hidden/.test(src));
ck("面板有机位与起止时间",
   src.indexOf('id="expCamBar"') > 0 && src.indexOf('id="expFrom"') > 0
   && src.indexOf('id="expTo"') > 0);
ck("面板里没有第二个日期输入（日期跟文件页走）",
   src.indexOf('type="date"') < 0);

/* ---- 计划地址 ---- */
const u = run('EXP.cams=["c1","c2"]; EXP.from="09:00"; EXP.to="10:30";'
            + ' state.daySel="2026-11-06"; expPlanUrl()');
ck("计划地址指向 /api/export/plan", u.indexOf("/api/export/plan?") === 0);
ck("机位是逗号多值且被编码", u.indexOf("cam=c1%2Cc2") > 0);
ck("起止拼成「日期 钟点」", u.indexOf("from=2026-11-06%2009%3A00") > 0
   && u.indexOf("to=2026-11-06%2010%3A30") > 0);
ck("没选日期时退回今天",
   run('state.daySel=""; expPlanUrl()').indexOf("from=" + run("jsToday()")) > 0);

/* ---- 提示文案 ---- */
const hint = run('expHintText({ok:true,total:{count:3,bytes:3145728,'
               + 'head_extra_sec:180,tail_extra_sec:45},'
               + 'cams:[{first_at:"2026-11-06 08:55:00",last_at:"2026-11-06 09:15:00"}],'
               + 'warnings:[]})');
ck("先说几个分片、多大", hint.indexOf("3 个分片 · 3.0 MB") === 0);
ck("再说实际覆盖到哪一秒",
   hint.indexOf("实际覆盖 08:55–09:15") > 0);
ck("边界多出多少必须写", hint.indexOf("边界多出 前 3 分 / 后 45 秒") > 0);

ck("警告换行附在后面",
   run('expHintText({ok:true,total:{count:1,bytes:1,cams:[],warnings:[]},'
     + 'warnings:["客厅 2026-11-01 只剩归档片段"]})')
     .indexOf("\n客厅 2026-11-01 只剩归档片段") > 0);
ck("警告最多三条，剩下的折成一句",
   (()=>{
     const t = run('expHintText({ok:true,total:{count:1,bytes:1},cams:[],'
                 + 'warnings:["a","b","c","d"]})');
     return t.indexOf("a") > 0 && t.indexOf("c") > 0
         && t.indexOf("d") < 0 && t.indexOf("另有 1 条") > 0;
   })());
ck("导不了时只说原因",
   run('expHintText({ok:false,error:"这次要导出 2.4 GB，超过单次 1.9 GB 上限"})')
     .indexOf("导不了：这次要导出 2.4 GB") === 0);
ck("大小换算三档都对",
   run('[expSize(900), expSize(2048), expSize(1572864), expSize(3221225472)].join("|")')
   === "900 B|2 KB|1.5 MB|3.0 GB");

/* ---- 按钮状态 ---- */
run('EXP.err=""; EXP.busy=false; EXP.cams=["c1"];'
  + 'EXP.plan={ok:true,total:{count:2,bytes:1},cams:[],warnings:[]}; expRender();');
ck("有可导内容时按钮可点", els.btnExpGo.disabled === false);
ck("按钮上写清几个分片", els.btnExpGo.textContent.indexOf("2 个分片") > 0);
run('EXP.plan={ok:false,error:"不认识这个机位：c9"}; expRender();');
ck("导不了时按钮禁用", els.btnExpGo.disabled === true);
run('EXP.cams=[]; EXP.plan={ok:true,total:{count:2,bytes:1},cams:[]}; expRender();');
ck("一个机位都没选时按钮也是禁用的", els.btnExpGo.disabled === true);

/* ---- 打开面板的默认值 ---- */
run('EXP.cams=[]; EXP.from=""; EXP.to=""; state.camId="cam-02";'
  + 'API.req=function(){ return Promise.resolve({ok:false,error:"stub"}); };'
  + 'expOpen(true)');
ck("面板打开", els.expBox.hidden === false);
ck("默认选中文件页当前的机位", run('EXP.cams.join(",")') === "cam-02");
ck("时间默认整天", run('EXP.from') === "00:00" && run('EXP.to') === "23:59");
run('expOpen(false)');
ck("收起后隐藏", els.expBox.hidden === true);

/* ---- 下载地址来自服务端 ---- */
ck("下载只读服务端给的 url", src.indexOf("a.href = d.url") > 0);
ck("前端不自己拼 /api/export?", src.indexOf('"/api/export?') < 0);
const gotUrl = (()=>{
  const saved = document.createElement;
  let got = null;
  document.createElement = () => ({ href:"", download:"", click(){ got = this.href; },
                                    remove(){} });
  try{
    run('EXP.plan={ok:true,url:"/api/export?cam=c1&from=x",'
      + 'total:{count:2,bytes:1},cams:[],warnings:[]}; expGo()');
  } finally { document.createElement = saved; }
  return got;
})();
ck("点导出直接下服务端那一份", gotUrl === "/api/export?cam=c1&from=x");

console.log((fail ? "FAIL" : "OK") + "  export  " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
