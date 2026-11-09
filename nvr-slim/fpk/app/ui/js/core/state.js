/* ================= core/state.js =================
   全页共用的数据形状与可变状态：CAMERAS / camParams 的默认骨架、GS 通用设置、
   回放现场 state，以及把 /api/status 与 /api/link 灌进真实资源状态的那两个入口。
   它们连起来只回答一个问题：**这一刻页面上的「当前值」是什么**。
   判断标准：被多个页面读写、且本身不画界面的状态放这里；纯时间计算进
   core/format.js，画面怎么摆、按钮怎么点都不进这个文件。
   为什么值得单独成文件：设置页写 GS、回放页写 state、直播页读 CAMERAS，
   以前「这个值到底谁改的」只能全文搜；集中之后改一次默认值就能看清牵动哪几页。
   ================================================= */

/* ================= 数据 =================
   摄像头只留「身份 + 码流」两类字段：不区分品牌、不存账号密码（要认证就直接写进
   RTSP 地址里，形如 rtsp://user:pass@ip:554/...），也不做分组/录像策略。
   每路的录像与画质参数统一放 camParams，与该路一一对应。 */
/* 所谓「默认配置」只是首次打开时的演示骨架，不是用户数据。
   它一旦被 saveCfg 写进 localStorage 并 pushCfg 推给服务端，就会把 NAS 上
   已经配好的真实摄像头全部顶掉 —— 换台设备打开页面、或清了浏览器缓存，
   都会触发这一次覆盖。所以推送前必须判断"这份配置是不是来自服务端"。 */
let demoSeeded = false;
const CAMERAS = [
  { id:"cam-01", name:"车库入口", ch:"CH01", res:"4MP",   online:true,
    main:"rtsp://192.0.2.10:8554/ch1", sub:"rtsp://192.0.2.10:8554/ch3" },
  { id:"cam-02", name:"一楼大堂", ch:"CH02", res:"4MP",   online:true,
    main:"rtsp://192.0.2.16:554/Streaming/Channels/101", sub:"rtsp://192.0.2.16:554/Streaming/Channels/102" },
  { id:"cam-03", name:"货梯走廊", ch:"CH03", res:"1080p", online:true,
    main:"rtsp://192.0.2.21:554/cam/realmonitor?channel=1&subtype=0", sub:"rtsp://192.0.2.21:554/cam/realmonitor?channel=1&subtype=1" },
  { id:"cam-04", name:"后门通道", ch:"CH04", res:"1080p", online:true,
    main:"rtsp://192.0.2.30:554/video1", sub:"rtsp://192.0.2.30:554/video2" },
];

/* 录像引擎（全时存储）设置 */
const REC = {
  enabled:true,
  dir:"/vol1/@appshare/nvr-output",   // 按摄像头/日期/时段分目录；默认锁定，点「修改」才可编辑
  segSec:300,             // 段落时长（秒）
  port:18080,             // Web 服务端口（安装时选，应用设置里改）
  cleanupAt:"03:00",      // 每天定点清理过期录像
  /* 0.9.111：原来是演示值（41.2GB / 6 目录 / 1247 文件），以前靠清理按钮
     跑完覆盖掉；清理按钮删除后它就是永久可见的假数据，改成空值 + 真实回填。 */
  lastClean:{ at:"", freed:0, dirs:0, files:0 },
  /* 容量不在前端写死：等 /api/status 的 disk_free_gb / disk_total_gb 回来再显示，
     没拿到就显示 "—"（以前这里写死 473.9 / 336.0，换台机器照样报旧数字） */
  diskTotalGB:null,
  diskFreeGB:null,
};
const REASONS = ["人形移动","车辆进出","画面变化","光照突变","物体遗留"];
const DAYS = ["2026-10-14","2026-10-15","2026-10-16","2026-10-17","2026-10-18","2026-10-19","2026-10-20"];
const WD = ["周日","周一","周二","周三","周四","周五","周六"];

function hashStr(s){ let h=2166136261; for(let i=0;i<s.length;i++){ h^=s.charCodeAt(i); h=Math.imul(h,16777619);} return h>>>0; }
function mulberry(seed){
  return function(){
    seed |= 0; seed = seed + 0x6D2B79F5 | 0;
    let t = Math.imul(seed ^ seed>>>15, 1|seed);
    t = t + Math.imul(t ^ t>>>7, 61|t) ^ t;
    return ((t ^ t>>>14)>>>0) / 4294967296;
  };
}
const segCache = new Map();
function getSegs(camId, day){
  const key = camId+"|"+day;
  if(segCache.has(key)) return segCache.get(key);
  const rnd = mulberry(hashStr(key));
  const segs = [];
  const busy = [[7,9.4],[11.5,13.6],[17,20.4]];
  for(let h=0; h<24; h++){
    let p = 0.16;
    for(const [a,b] of busy) if(h>=a && h<b) p = 0.88;
    if(h<6) p = 0.10;
    let n = 0;
    if(rnd() < p) n = 1 + Math.floor(rnd()*2);
    else if(rnd() < 0.22) n = 1;
    for(let k=0;k<n;k++){
      const start = h*3600 + Math.floor(rnd()*3300);
      const dur = 22 + Math.floor(Math.pow(rnd(),2.1)*820);
      const end = Math.min(start+dur, 86395);
      if(end-start < 18) continue;
      const last = segs[segs.length-1];
      if(last && start < last.e + 45) continue;
      segs.push({ s:start, e:end, reason:REASONS[Math.floor(rnd()*REASONS.length)], level:0.32+rnd()*0.62 });
    }
  }
  if(segs.length < 6){
    segs.length = 0;
    for(let i=0;i<7;i++){
      const start = 7*3600 + i*5400 + Math.floor(rnd()*600);
      segs.push({ s:start, e:start+180+Math.floor(rnd()*600), reason:REASONS[i%REASONS.length], level:0.4+rnd()*0.5 });
    }
  }
  segCache.set(key,segs);
  return segs;
}

const camParams = {};
/* 全时段落（录像引擎按 REC.segSec 切段落盘） */
function fullDayMbps(cam){ return cam.res === "4MP" ? 3.4 : 2.3; }   // 主码流约值
function getFullSegs(camId, day){
  const key = "full|"+camId+"|"+day;
  if(segCache.has(key)) return segCache.get(key);
  const cam = CAMERAS.find(c=>c.id===camId);
  const p = camParams[camId];
  const segs = [];
  const step = REC.segSec;
  const act = getSegs(camId, day);
  for(let s=0; s<86400; s+=step){
    const e = Math.min(s+step, 86400);
    const hit = act.filter(a=>a.s < e && a.e > s);
    segs.push({ s, e, reason:"全时", level: hit.length ? 0.75 : 0.15, hits:hit.length });
  }
  segCache.set(key,segs);
  return segs;
}

/* 每路的完整默认值：新增摄像头与「恢复默认」共用同一份，
   保证"默认"只有一个定义，不会两处漂移。 */
/* 画面区域：16×9 方块预选（对齐摄像头 App 的隐私遮蔽交互）。
   masks = { ignore:[格号…], include:[格号…] }，格号 = 行*16+列，行在上。
   ignore  = 忽略区：框中心落进去就丢（治"这块老误报"）。
   include = 只统计区：框中心落不进去就丢（治"只关心大门/车道"），留空=整幅画面。
   两块重合时按忽略处理（服务端 apply_regions 的顺序），界面上的提示也这么写。
   「输出遮蔽区」已移除：录制阶段打黑块必须放弃 -c copy 改重编码，CPU 翻倍，
   而它想解决的问题（这块别算成事件）本来就该由忽略区在判定阶段处理。
   旧配置里的 redact 字段读到后会被静默丢弃，不会报错。 */
const GRID_COLS = 16, GRID_ROWS = 9;
function rectToCells(x, y, w, h){
  const out = [];
  for(let r=0; r<GRID_ROWS; r++) for(let c=0; c<GRID_COLS; c++){
    const cx=(c+0.5)/GRID_COLS, cy=(r+0.5)/GRID_ROWS;
    if(cx>=x && cx<=x+w && cy>=y && cy<=y+h) out.push(r*GRID_COLS+c);
  }
  return out;
}
function normMasks(m){
  /* 归一化 = 去重 + 丢掉越界/非整数 + 升序。升序不是为了好看：格号是集合，
     同一块区域用不同顺序画出来必须算「没改动」（dirtyCount 直接比 JSON），
     否则用户按一下"取消"会看到"已改 1 项"。服务端 norm_masks 同口径。 */
  const clean = arr => [...new Set((arr || [])
      /* 只认数字与纯数字字符串。null 会被 Number() 变成 0 —— 少这一层判断，
         一个空值就会在画面上点亮左上角那一格，用户怎么点都取消不掉。 */
      .map(x=> typeof x === "number" ? x
             : (typeof x === "string" && /^[0-9]+$/.test(x.trim()) ? +x : NaN))
      .filter(x=>Number.isInteger(x) && x >= 0 && x < GRID_COLS*GRID_ROWS))]
      .sort((a,b)=>a-b);
  if(Array.isArray(m)){   /* 旧自由框格式 → 换算成格号，中心点落在框里即选中 */
    const ig=[];
    m.forEach(r=>{ if(r.type!=="redact") rectToCells(r.x,r.y,r.w,r.h).forEach(i=>ig.push(i)); });
    return { ignore:clean(ig), include:[] };
  }
  const o = m || {};
  return { ignore:clean(o.ignore), include:clean(o.include) };
}

/* 判读条件默认值：与 app/server/params.py 的 ANALYSIS_DEFAULTS / SCENE_PRESETS
   同口径。界面上的每一格都真的会推到服务端 profiles，不再是"只改显示"。 */
const JUDGE_DEFAULTS = {
  engine:"auto",     // auto | pixel | bytes
  scene:"auto",      // auto（自动识别彩色/红外）| color | mono
  sens:"mid",        // high | mid | low
  pixelDelta:24,     // 逐像素亮度差门槛（0~255）
  minEvent:2,        // 最短事件（秒）
  blur:1,            // 模糊半径（0~3）
  fps:2,             // 分析抽样帧（帧/秒）
  probeSec:5,        // 日夜采样间隔（秒）
  mergeGap:5,        // 静默合并（秒）
};
const SENS_MULT  = { high:1.6, mid:2.0, low:3.0 };   // 与服务端 nvr_slim.SENS_MULT 一致
const SENS_NAME  = { high:"高（更易报）", mid:"标准（推荐）", low:"低（更保守）" };
const SCENE_NAME = { auto:"自动识别彩色/红外", color:"彩色", mono:"黑白红外" };
const ENGINE_NAME= { auto:"自动", pixel:"像素差", bytes:"码流曲线" };

function judgeOf(p){ return (p && p.judge) || JUDGE_DEFAULTS; }

/* 前端 judge → 服务端字段（snake_case，与 params.SCHEMA 一一对应） */
function judgeWire(p){
  const j = judgeOf(p);
  return { engine:j.engine, scene:j.scene, sens:j.sens,
           pixel_delta:j.pixelDelta, min_event:j.minEvent, blur:j.blur,
           fps:j.fps, scene_probe_sec:j.probeSec, merge_gap:j.mergeGap };
}
/* 服务端 profiles[cid].analysis → 前端 judge（回读用：换浏览器也能看到服务端真实参数） */
function judgeFromProfile(an){
  if(!an) return null;
  return normJudge({
    engine: an.engine, scene: an.scene, sens: an.sensitivity,
    pixelDelta: an.pixel_delta, minEvent: an.min_event,
    blur: an.blur_mono != null ? an.blur_mono : an.blur_color,
    fps: an.fps, probeSec: an.scene_probe_sec, mergeGap: an.merge_gap
  }, null);
}

/* 旧版参数迁移：早期用 0~100 的"灵敏度百分比"+ 最短保留/静默合并，
   现在改成档位 + 具体秒数。老配置不能丢，按区间映射一次。 */
function normJudge(j, legacy){
  const L = legacy || {};
  const src = j || {};
  /* 只覆盖显式给了值的键：Object.assign 会把 undefined 也盖上去，
     回读服务端档案时字段名对不齐就会把默认值抹成 undefined。 */
  const out = Object.assign({}, JUDGE_DEFAULTS);
  Object.keys(JUDGE_DEFAULTS).forEach(k=>{ if(src[k] != null) out[k] = src[k]; });
  const legacySens = (typeof src.sensitivity === "number") ? src.sensitivity
                    : (typeof L.sensitivity === "number") ? L.sensitivity : null;
  if(src.sens == null && legacySens != null){
    out.sens = legacySens >= 60 ? "high" : (legacySens <= 25 ? "low" : "mid");
  }
  if(src.minEvent == null && typeof L.minKeep === "number"){
    out.minEvent = Math.max(0.5, Math.min(60, L.minKeep));
  }
  if(src.mergeGap == null && typeof L.mergeGap === "number"){
    out.mergeGap = Math.max(0, Math.min(120, L.mergeGap));
  }
  if(["auto","pixel","bytes"].indexOf(out.engine) < 0) out.engine = "auto";
  if(["auto","mono","color"].indexOf(out.scene) < 0) out.scene = "auto";
  if(["high","mid","low"].indexOf(out.sens) < 0) out.sens = "mid";
  out.pixelDelta = clampNum(out.pixelDelta, 1, 80, 24);
  out.minEvent   = clampNum(out.minEvent, 0.5, 60, 2);
  out.blur       = clampNum(out.blur, 0, 3, 1);
  out.fps        = clampNum(out.fps, 0.2, 10, 2);
  out.probeSec   = clampNum(out.probeSec, 1, 30, 5);
  out.mergeGap   = clampNum(out.mergeGap, 0, 120, 5);
  return out;
}
function clampNum(v, lo, hi, dft){
  const n = +v;
  if(!isFinite(n)) return dft;
  return Math.max(lo, Math.min(hi, n));
}

/* 全时保留默认天数（与后端 retention.DEFAULTS["full_days"]=3 同源）。
   设为单一常量：defaultCamParams 与全局「事件侦测与归档」模型说明共用，
   改一处即全站生效，避免数字在多处写死漂移。 */
const FULL_DAYS_DEFAULT = 3;

function defaultCamParams(cam, i){
  i = i || 0;
  return {
    enabled: true,
    main: cam ? (cam.main || "") : "",
    sub:  cam ? (cam.sub  || "") : "",
    mode: i===1 ? "fine" : "fast",
    autoDN: true,
    profile: "day",
    day:   { crf:24, fps:12, scale:100, dn:1 },
    night: { crf:29, fps:8,  scale:75,  dn:3 },
    /* 判读条件（引擎参数，全部推到服务端 profiles） */
    judge: Object.assign({}, JUDGE_DEFAULTS),
    speed:1, speedMode:"pts", prescan:true,
    /* 事件侦测（每路总开关 + 输出倍速）：
       condense=false 时这一路只录全时、不判定也不裁归档；
       clipSpeed>1 时导出走重编码压时间轴（2× = 播放时间缩短一半） */
    condense: true,
    clipSpeed: 1,
    /* 切分方式：follow=跟随通用设置；join=整段拼接；split=活动切片
       （对这一路强制「录完即切」，即使通用选了定时批量） */
    clipMode: "follow",
    /* 保留周期：全时录像按天、归档浓缩片段按天（总回溯天数见通用设置 archive_days）。
       fullDays 默认 FULL_DAYS_DEFAULT，服务端每路可单独覆盖。 */
    fullDays: FULL_DAYS_DEFAULT,
    clipDays: 10,
    /* 单路分片长度（秒）；0 = 跟随「通用设置」。这是真正写进 ffmpeg 命令行的值 */
    segSec: 0,
    /* AI 检测（0.9.60）：follow=跟随全局总闸与类别；on/off 显式覆盖本路。
       detectCls 三键完整状态，缺键一律 False（服务端口径一致，不会偷偷补全局） */
    detect: "follow",
    detectCls: { person:true, two_wheeler:true, vehicle:true },
    /* 两块画面区域都默认空：忽略区空=不过滤，只统计区空=整幅画面都算。
       空 include 是关键兼容点——老配置读进来也是空，判定与 0.9.127 一致 */
    masks: { ignore:[], include:[] }
  };
}

CAMERAS.forEach((c,i)=>{
  const d = defaultCamParams(c, i);
  d.enabled = i < 3;
  d.masks = { ignore: c.id==="cam-01" ? rectToCells(.62,.55,.22,.30) : [], include: [] };
  camParams[c.id] = d;
});

/* ================= 状态 ================= */
const state = {
  camId:"cam-01",
  dayIdx:0,
  daySel:null,      // 选中的日期字符串（跨重排/跨天保持选中，见 recalcDayList）
  hourBlock:-1,     // 0..11 两小时一格
  segs:[],          // 当前回放模式的段列表
  activity:[],      // 活动片段（用于热力条与全时模式标注）
  segIdx:-1,
  t:0,
  playing:false,
  speed:1,          // 回放倍速（1/2/4/8/16/32×），playbackRate 上限 16、超出跳秒补足
  land:false,
  rot:false,        /* 方向锁不住时，画面转了 90° 兜底 */
  maskTool:"ignore",
  pmode:"full",     // full = 全时录像 | archive = 归档浓缩 | clip = 精简片段
  kindPick:null,    // 用户手动选的维度 {day, kind}：当天同时有全时和归档时才给选
  rec:null,         // /api/recordings 的结果：真实落盘的录像清单
  dayList:[],       // 当前摄像头真正有文件的日子（旧→新升序，全量；顺序只有 recalcDayList 一处）
  liveView:"snap",  // snap | single | grid
  liveCam:0,
  livePaused:false,
  liveStream:false,   // 单路视图是否在播 MJPEG 实时流（false = 静态快照轮换）
  wallStream:false,   // 首页快照墙是否在原地播实时流（连续播放）
  wallAt:0,           // 连续播放开始时刻，用来算"已播 N 分钟"
  lrFs:false,         // 实时层是否处于"单路全屏"形态
  lrChrome:true,      // 全屏时底部控制条是否浮现（播放中会自动收起）
};

/* ================= 真实资源状态 =================
   这里只剩"有没有拿到过服务端数据"和错误信息。
   CPU / 内存 / 核显那套读过 /proc 与 ffmpeg -encoders，在 NAS 的精简运行
   环境里经常拿不到（进程 CPU 为空、容器里看不到 /dev/dri），界面上只能显示
   "—"；与其留一排读不出来的数字，不如把这块整体去掉，只保留存储与目录。
   服务端的 res 字段仍然保留（/api/status），需要时可直接查。 */
const RES = {
  ok:false,          // 有没有成功拉到过一次 /api/status
  at:0,              // 上次拉取时间
  err:""
};
/* 资源条只剩「存储 + 目录」。这个请求现在只做一件事：
   把服务端 os.statvfs / disk_usage 读到的真实容量拿回来。
   CPU / 内存 / 核显不再展示，也不再往 RES 里塞没人读的字段。 */
function refreshRes(){
  return API.get("/api/status")
    .then(d=>{
      if(!d || d.ok === false) throw new Error((d && d.error) || "status 失败");
      /* AI 检测 / 通知状态快照：设置页状态行与诊断卡吃这份 */
      if(d.detect){ DET.st = d.detect; }
      if(d.notify){ NT.st = d.notify; }
      ING.st = d.ingest || null;
      ING.fr = d.frames || null;
      const r = d.res || {};
      RES.ok = true;
      RES.at = Date.now();
      RES.err = "";
      if(r.disk_free_gb != null) REC.diskFreeGB = +r.disk_free_gb;
      if(r.disk_total_gb != null) REC.diskTotalGB = +r.disk_total_gb;
      syncDiagSum();
      updateLiveHeader();
      return RES;
    })
    .catch(e=>{
      /* 读不到就是读不到：容量显示 "—"，不保留上次的数字假装还在 */
      RES.ok = false;
      RES.err = (e && e.message) || "无法读取系统状态";
      syncDiagSum();
      return RES;
    });
}

/* 全局执行设置（对所有摄像头生效） */
const GS = {
  mode:"h1",            // now | m15 | h1 | d1
  lastScan:"16:30",
  done:128,
  pending:6,
  resume:2,
  dedupeOnce:true,
  reprocessOnParam:true,
  skipIfExists:true,
  retentionDays:3,
  /* 硬件加速与资源 */
  gpu:true,          // QSV 解码 + 编码
  conc:2,            // 同时处理路数
  nightOnly:false,   // 只在夜间重编码
  lowPrio:true,      // nice / ionice / 磁盘空闲才写
};
/* 录制与判定衔接。
   0.9.111 起两项都固定，界面上不再暴露：
     mode —— 只有 live（分片封口即判定）。scheduled（攒 N 小时批量）那条路已下线，
             字段仍随 /api/config 往返，只为兼容旧配置与后端解析；
     frag —— 恒为分片 MP4。整片 moov 被强杀就整段打不开，没有需要回退的场景。 */
const LK = {
  mode:"live",        // 固定 live，见上
  intervalH:6,        // 旧字段，服务于兼容，不再有界面
  segSec:300,         // 分片长度
  /* 分片 MP4：moov 写在文件头、每个关键帧一个 moof，进程被硬杀/断电后
     已写的部分仍能播，损失上限是一个 GOP 而不是整片。 */
  frag:true,          // 固定 true，见上
  clipStyle:"join",   // 通用切分方式：join=整段拼接 | split=活动切片（每路可用 clipMode 覆盖）
  condenseMode:"virtual", // 事件侦测方案：ledger 开→virtual(仅台账) / 关→off；clipOut(物化切片)为配置项 UI 不暴露
  ledger:true,       // 维度① 活动判读/台账开关：开=写台账、回放有标记、可归档
  clipOut:false,     // 浓缩片段产出(物化切片)，仅配置可开；UI 不暴露，默认仅台账
  archEnable:true,     // 维度③ 归档裁剪开关（仅台账/virtual 下才裁 _archive）
  archDays:7,         // 归档片段保留天数（= 总回溯天数）
  archBatch:120,       // 单晚最多裁多少个分片（跨夜分批）
  /* 提前归档：false（默认）= 只裁「今天就删源」的那天，同一天不会同时存在
     全时与归档；true = 全时最后一天当晚就裁，产物与原画并存约一天，换来
     一整天的补救缓冲。 */
  archPrefetch:false,
  /* ---- AI 检测（0.9.60）：全局默认，每路可覆盖。全部进 pushCfg body ——
     只改内存不推送就是「假设置」，这个坑本项目踩过不止一次。 ---- */
  detEnable:false,     // 总闸：关时每路开了也不检
  detMode:"snap",      // snap=快照节奏 | live=实时（P1）
  detDevice:"auto",    // auto | cpu | gpu（P1）
  detCls:{ person:true, two_wheeler:true, vehicle:true },  // 全局默认检测类别
  detSens:"std",       // 灵敏度：high | std | low → 分品类阈值
  detTrigger:{ motion:true, person:true, vehicle:true },   // 活动触发：未勾选=静止画面
  detFps:5,            // 实时帧率（P1）
  detNight:"throttle", // 夜间策略（P1）
  /* ---- 通知（0.9.60）：三开关 + 自定义 Webhook + 冷却 ----
     平台与模板（0.9.124）：platform=auto 时按 Webhook 的 host 自动识别；
     公司网关把机器人地址代理成自家域名时才需要手选。空模板 = 默认文案。
     这两个键必须一起进 pushCfg —— 只放 localStorage 的话，保存设置时
     服务端那份会被别的设备推回默认值。 */
  ntWebhook:"",
  ntMotion:false,
  ntPerson:false,
  ntVehicle:false,
  ntCooldown:60,
  ntPlatform:"auto",
  ntTemplate:"",
  pending:0,          // 待处理数 —— 以 /api/link 返回为准，不再本地编数
  done:0,
  silent:0,
  failed:0,
  nextBatch:"—",
  lastBatch:"—",
  lastItem:"",
  rec:null,           // /api/link 里的 record.channels：每路录制进程的真实状态
};
/* 服务端 /api/link 的真实状态 → 本地 LK。只有这里写 pending/done/failed，
   别的地方不许手写这些数字，否则又会回到「界面说在处理、服务端其实闲着」。 */
function applyLink(ls){
  if(!ls) return;
  LK.mode = ls.mode || LK.mode;
  LK.intervalH = ls.interval_h || LK.intervalH;
  LK.segSec = ls.segment_sec || LK.segSec;
  if(ls.clip_style) LK.clipStyle = ls.clip_style;
  LK.pending = ls.pending || 0;
  if(ls.counters){
    LK.done = ls.counters.done || 0;
    LK.silent = ls.counters.silent || 0;
    LK.failed = ls.counters.failed || 0;
  }
  LK.nextBatch = ls.next_batch_at || "—";
  LK.lastBatch = ls.last_batch_at || "—";
  LK.lastItem = ls.last_item && ls.last_item.file
    ? (ls.last_item.ok ? "完成 " : "失败 ") + ls.last_item.file : "";
  /* 每路录制进程的真实状态（在跑没跑、上次 ffmpeg 报了什么），
     以前这些只躺在日志里，用户只能看到"一条录像都没有" */
  if(ls.record) LK.rec = ls.record;
  try{ renderCamTabs(); }catch(e){}
}
function syncLink(){
  if(typeof fetch !== "function") return;
  API.get("/api/link")
    .then(d=>{ if(d && d.ok) applyLink(d); }).catch(()=>{});
}
/* 0.9.111：判定时机固定为「即刻判定」（定时压制那条路已随选项一起下线），
   所以这里不再有分支。原「攒够 N 小时批量处理」的文案一并删掉，
   免得界面上留着一段解释一个不存在的选项。 */
function linkHint(){
  return "分片一写完就判定并记台账。数据还在内存里，几乎不额外读盘、也不跟录像抢磁头；"
    + "镜头刚发生的事，一分钟内就能在回放页时间轴上看到活动标记。";
}
const SCAN_LABEL = { now:"立即", m15:"每 15 分钟", h1:"每小时", d1:"每天 03:00" };
/* 分片长度的统一描述：60 秒以下按秒说，整分钟按分钟说 —— 免得出现 "0.5 分钟" */
function segLabel(sec){
  const s = Math.max(0, Math.round(+sec || 0));
  if(!s) return "—";
  if(s < 60) return s + " 秒";
  return Math.round(s/60) + " 分钟";
}
/* 下次扫描时刻 = 从"现在"往上取整到下一个档位。
   以前这里是写死的 "16:45" / "17:00" / "明天 03:00"，无论几点打开都报同样的字。 */
function nextScan(nowMs){
  const now = new Date(nowMs || Date.now());
  if(GS.mode === "now") return "—";
  const t = new Date(now.getTime());
  t.setSeconds(0, 0);
  if(GS.mode === "m15"){
    t.setMinutes(Math.floor(t.getMinutes()/15)*15 + 15);
  }else if(GS.mode === "h1"){
    t.setMinutes(0);
    t.setHours(t.getHours() + 1);
  }else{ /* d1：每天 03:00 */
    t.setMinutes(0, 0, 0);
    t.setHours(3);
    if(t.getTime() <= now.getTime()) t.setDate(t.getDate() + 1);
  }
  const dayDiff = Math.round((new Date(t.getFullYear(),t.getMonth(),t.getDate())
    - new Date(now.getFullYear(),now.getMonth(),now.getDate())) / 86400000);
  return (dayDiff === 0 ? "" : dayDiff === 1 ? "明天 " : dayDiff + " 天后 ")
       + pad(t.getHours()) + ":" + pad(t.getMinutes());
}
function scanHint(){
  const d = GS.retentionDays;
  if(GS.mode === "now") return "点一次扫一次，不驻留定时任务，适合装完机先验证。";
  if(GS.mode === "m15") return "NVR 只留 " + d + " 天，15 分钟一档偏密，但能最大限度避免被覆盖。";
  if(GS.mode === "h1")  return "NVR 留 " + d + " 天，每小时扫一次来得及 —— 文件被覆盖前还有 " + (d*24) + " 次机会。";
  return d <= 1
    ? "警告：NVR 只留 " + d + " 天，每天一档可能在下次扫描前就被覆盖，建议改每小时。"
    : "NVR 留 " + d + " 天，每天一次来得及，但断电一天就会丢一批。";
}
