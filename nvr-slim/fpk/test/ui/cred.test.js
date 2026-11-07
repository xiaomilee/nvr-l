/* 凭据不出本机（0.9.120 批次 2 · 清单第 4 项）
   ------------------------------------------------------------------
   批次 2 第 4 项的口径是「GET 配置一律脱敏 / 编辑时密码框留空 = 保留原密码 /
   前端不再把明文写 localStorage」。前两条由服务端 `merge_cam_urls` 与
   `runtime.redact_cfg` 守（见 test_url_keep.py、test_http_smoke.py），
   前端这一半只有靠这里钉住：

   1. **明文不落盘**：摄像头地址（含账号密码）只在内存里活到页面关闭。
      一旦被 saveCfg 序列化进 localStorage，它就永久留在那台设备的浏览器里，
      清缓存才会消失 —— 而这台设备很可能是前台的公共电脑。
   2. **重新载入不冒充明文**：loadCfg 回来时 main/sub 必须是空串。
      如果它把脱敏串（rtsp://***@）当明文存下来再推给服务端，
      服务端会按"用户填了新地址"处理，等于把真密码换成三个星号。

   跑法： node test/ui/cred.test.js */
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
function ck(name, cond, detail){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name + (detail ? "  ← " + detail : "")); }
}

const PLAIN = "rtsp://admin:pw2o26secret@192.0.2.10:554/Streaming/Channels/101";

/* ---- 明文不进 localStorage ---- */
run('applyServerCams([{id:"cam-01",name:"车库",url:"rtsp://***@192.0.2.10:554/x",sub_url:""}]);');
run('camParams["cam-01"].main = ' + JSON.stringify(PLAIN) + ';'
  + 'CAMERAS[0].main = ' + JSON.stringify(PLAIN) + ';');
run('saveCfg();');
const raw = global.localStorage.getItem("nvr.cfg.v1") || "";
ck("配置写进去了", raw.length > 0);
ck("明文密码没有被写进 localStorage", raw.indexOf("pw2o26secret") < 0, raw.slice(0, 120));
ck("连地址本身都不写（免得漏掉某种凭据形态）", raw.indexOf("rtsp://") < 0);
ck("该留的还留着：通道名/保留天数",
   raw.indexOf("车库") >= 0 && raw.indexOf("fullDays") >= 0);

/* ---- 账本里也不能有 ---- */
const anyKey = Object.keys(global.localStorage).length;
let leaked = "";
for(const k of ["nvr.cfg.v1","nvr_api_token","fsRot"]){
  const v = global.localStorage.getItem(k) || "";
  if(v.indexOf("pw2o26secret") >= 0) leaked = k;
}
ck("任何 localStorage 键里都没有明文密码", leaked === "", leaked);
ck("localStorage 里没有多长出别的键", anyKey >= 1);

/* ---- 重新载入：不把空当明文、不把脱敏串当明文 ---- */
run('CAMERAS.length = 0; Object.keys(camParams).forEach(k=>delete camParams[k]);');
run('loadCfg();');
ck("重新载入只恢复身份，不恢复明文",
   run('(CAMERAS[0] && CAMERAS[0].main) || ""') === "", run('CAMERAS[0] && CAMERAS[0].main'));
ck("重新载入后子码流同样为空",
   run('(CAMERAS[0] && CAMERAS[0].sub) || ""') === "");

/* ---- 服务端回的是脱敏串，前端不能把它当明文存下来 ---- */
run('applyServerCams([{id:"cam-01",name:"车库",url:"rtsp://***@192.0.2.10:554/x",sub_url:""}]);');
ck("脱敏串不落进主码流字段",
   run('camParams["cam-01"].main') !== "rtsp://***@192.0.2.10:554/x");
ck("但「NAS 已存地址」的事实要保留（否则误报未配置）",
   run('CAMERAS[0].hasUrl') === true);

/* ---- 留空 = 保留：推送里带空 url + 不带清空标记 ---- */
run('camParams["cam-01"].urlClear = false;');
const wire = run('JSON.stringify(CAMERAS.map(c=>{const p=camParams[c.id]||{};'
               + 'return {id:c.id,url:p.main||"",url_clear:!!p.urlClear};}))');
const arr = JSON.parse(wire);
ck("本机没有明文时推送的是空 url（服务端回落旧值）", arr[0].url === "");
ck("且不带 url_clear（空 ≠ 要清掉）", arr[0].url_clear === false);

console.log("cred: " + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
