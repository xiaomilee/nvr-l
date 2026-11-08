/* AI 检测状态与三态展示（0.9.122 批次 4 第 5 项）
   ------------------------------------------------------------------
   守两件事：

   1. **一个口径**：能不能用由后端 state 判（disabled/starting/unavailable/ready），
      前端只翻译。旧版前端自己拼 running/loaded/error，同一现场在设置页说
      "未运行"、在诊断卡说"加载中"；用户不知道该信哪个，也不知道要不要重启。
   2. **未知就是未知**：抽帧失败 / 推理异常 / 过期丢弃记 unknown，必须与
      命中、"无命中"分开显示。把 unknown 并进命中数，用户看到的数就既不是
      "看见了什么"，也不是"没看成多久"。

   顺手守住一个老 bug：每路「最近」以前直接拼 c.last（是个对象），
   渲染成 [object Object]。

   跑法： node test/ui/detect.test.js */
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

/* ---- detStateZh：四种 state 的中文与告警色 ---- */
ck("disabled → 已关闭且不告警",
   run('(()=>{const z=detStateZh({state:"disabled"});return z.t==="已关闭"&&!z.warn})()'));
ck("starting → 加载中且不告警(别让人以为坏了)",
   run('(()=>{const z=detStateZh({state:"starting"});return z.t==="加载中…"&&!z.warn})()'));
ck("unavailable 带上原因",
   run('detStateZh({state:"unavailable",error_message:"未找到 ffmpeg"}).t')
     .indexOf("未找到 ffmpeg") > 0);
ck("unavailable 标告警",
   run('detStateZh({state:"unavailable",error_message:"x"}).warn') === true);
ck("ready → 正常",
   run('detStateZh({state:"ready"}).t') === "正常");
ck("旧服务端(无 state)未运行 → 未运行 + 告警",
   run('(()=>{const z=detStateZh({running:false});return z.t==="未运行"&&z.warn})()'));
ck("旧服务端有 error → 不可用",
   run('detStateZh({running:true,error:"模型文件缺失"}).t').indexOf("模型文件缺失") > 0);
ck("旧服务端加载中",
   run('detStateZh({running:true,loaded:false}).t') === "加载中…");
ck("空对象不抛",
   run('detStateZh(null).t') === "未运行");

/* ---- detHintOf：每种失败码给不同的下一步 ---- */
ck("FFMPEG_NOT_FOUND 指路装 ffmpeg",
   run('detHintOf({error_code:"FFMPEG_NOT_FOUND"})').indexOf("ffmpeg") > 0);
ck("MODEL_MISSING 指路重装",
   run('detHintOf({error_code:"MODEL_MISSING"})').indexOf("重装") > 0);
ck("RUNTIME_FAIL 指向 Python/wheel",
   run('detHintOf({error_code:"RUNTIME_FAIL"})').indexOf("Python") > 0);
ck("WORKER_DOWN 提示重启服务",
   run('detHintOf({error_code:"WORKER_DOWN"})').indexOf("重启") > 0);
ck("FRAME_FAIL 指向摄像头侧",
   run('detHintOf({error_code:"FRAME_FAIL"})').indexOf("摄像头") > 0);
ck("无码给通用解释",
   run('detHintOf({})').indexOf("重开一次服务") > 0);
ck("四种提示各不相同",
   new Set(["FFMPEG_NOT_FOUND","MODEL_MISSING","RUNTIME_FAIL","WORKER_DOWN"]
     .map(c => run('detHintOf({error_code:"' + c + '"})'))).size === 4);

/* ---- detUnknownOf ---- */
ck("未知数按路汇总",
   run('detUnknownOf({per_cam:{a:{unknown:2},b:{unknown:3},c:{}}})') === 5);
ck("没有 per_cam 时为 0",
   run('detUnknownOf({})') === 0);
ck("未知字段缺失不报 NaN",
   run('String(detUnknownOf({per_cam:{a:{unknown:null}}}))') === "0");

/* ---- detStatusLine ---- */
ck("关闭时只说已关闭",
   run('LK.detEnable=false; detStatusLine()').indexOf("已关闭") > 0);
ck("不可用时显示原因",
   run('LK.detEnable=true; DET.st={state:"unavailable",error_message:"未找到 ffmpeg"};'
     + 'detStatusLine()').indexOf("未找到 ffmpeg") > 0);
ck("加载中显示加载中",
   run('DET.st={state:"starting"}; detStatusLine()').indexOf("加载中") > 0);
ck("正常时给出帧数/命中/未知",
   run('DET.st={state:"ready",avg_ms:76,per_cam:{a:{today:10,cls:{person:2},unknown:3}}};'
     + 'detStatusLine()').indexOf("今日 10 帧") > 0);
ck("未知单独标红显示",
   run('DET.st={state:"ready",per_cam:{a:{today:10,cls:{},unknown:3}}};'
     + 'detStatusLine()').indexOf("未知 3") > 0);
ck("没有未知时不出现未知字样",
   run('DET.st={state:"ready",per_cam:{a:{today:10,cls:{},unknown:0}}};'
     + 'detStatusLine()').indexOf("未知") < 0);

/* ---- cardDetectDiag ---- */
ck("未开启时给开启路径",
   run('LK.detEnable=false; DET.st=null; cardDetectDiag()').indexOf("通用设置") > 0);
ck("不可用时有失败码",
   run('LK.detEnable=true; DET.st={state:"unavailable",error_code:"MODEL_MISSING",'
     + 'error_message:"模型文件缺失"}; cardDetectDiag()').indexOf("MODEL_MISSING") > 0);
ck("不可用时有下一步",
   run('DET.st={state:"unavailable",error_code:"MODEL_MISSING",error_message:"x"};'
     + 'cardDetectDiag()').indexOf("重装") > 0);
ck("不可用时不写「worker 未运行」",
   run('DET.st={state:"unavailable",error_code:"WORKER_DOWN",error_message:"x"};'
     + 'cardDetectDiag()').indexOf("worker 未运行") < 0);
ck("加载中提示等几秒而不是重启",
   run('DET.st={state:"starting"}; cardDetectDiag()').indexOf("等几秒") > 0);

const READY = {state:"ready", loaded:true, avg_ms:76,
  runtime:{python:"3.12", ort:"onnxruntime-1.20.1-cp312.whl"},
  sched:{workers:2, queued:0, queue_max:4, dropped_full:1, dropped_stale:2,
         round_avg_ms:180, round_cams:2},
  per_cam:{a:{today:10, cls:{person:2, vehicle:1}, unknown:3, skip:4,
              last:{t:1700000000, cats:["person"]},
              last_err:"解图超时（8 秒）", last_code:"FRAME_FAIL"}}};
run('LK.detEnable=true; DET.st=' + J(READY) + ';');
ck("调度行：并发数",
   run('cardDetectDiag()').indexOf("并发 2 路") > 0);
ck("调度行：队列上限",
   run('cardDetectDiag()').indexOf("队列 0/4") > 0);
ck("调度行：丢弃分两类",
   run('cardDetectDiag()').indexOf("队列满 1 / 过期 2") > 0);
ck("调度行：一轮耗时与路数",
   run('cardDetectDiag()').indexOf("一轮 180ms / 2 路") > 0);
ck("每路：未知单独列",
   run('cardDetectDiag()').indexOf("未知 3") > 0);
ck("每路：同帧跳过不被当成错误",
   run('cardDetectDiag()').indexOf("同帧跳过 4") > 0);
ck("每路：最近时刻是时间不是对象",
   run('cardDetectDiag()').indexOf("[object") < 0);
ck("最近错误带失败码",
   run('cardDetectDiag()').indexOf("[FRAME_FAIL]") > 0);
ck("底部说明改口径为「未知」",
   run('cardDetectDiag()').indexOf("不会写成") > 0);

/* 老服务端（无 state / sched / unknown）不能白屏 */
const OLD = {running:true, loaded:true, avg_ms:60,
             per_cam:{a:{today:5, cls:{person:1}, last:{t:1700000000, cats:["person"]}}}};
run('DET.st=' + J(OLD) + ';');
ck("老 payload 仍渲染每路统计",
   run('cardDetectDiag()').indexOf("抽帧 5") > 0);
ck("老 payload 不显示调度行（没有数据就不编）",
   run('cardDetectDiag()').indexOf("并发 ") < 0);
ck("老 payload 不显示未知（没有这一项就不写 0）",
   run('cardDetectDiag()').indexOf("未知 ") < 0);

/* ---- diagSummary ---- */
ck("摘要：不可用点出来",
   run('LK.detEnable=true; DET.st={state:"unavailable",error_code:"FRAME_FAIL",'
     + 'error_message:"取帧失败"}; diagSummary()').indexOf("AI 不可用") > 0);
ck("摘要：未知数带上",
   run('DET.st={state:"ready",loaded:true,per_cam:{a:{cls:{person:1},unknown:2}}};'
     + 'diagSummary()').indexOf("未知 2") > 0);

console.log("\ndetect: " + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
