/* ================= pages/settings_cards.js =================
   设置页的卡片渲染：录制 / 衔接 / 回放 / 检测 / 外观 / 日志 / 诊断导出 /
   磁盘扫描 / 摄像头列表 / 每路只读摘要。全是"把服务端数据画成卡片"的纯渲染，
   不写配置、不改 DOM 之外的状态。
   判断标准：**看得见但点不动的那半张设置页**放这里；一旦涉及草稿、输入框、
   遮罩画布、保存动作，就是下一份 settings_editor.js 的事。
   为什么按这个界线切：设置页历史上最凶的一类 bug 是"保存时读错了卡片上的值"
   —— 渲染与草稿混在一起时，谁也说不清当前显示的是服务端的值还是用户没提交的
   编辑值。两半分开后，"显示"这一半连 draft 都不该出现。
   ============================================================== */

/* ================= 摄像头页 ================= */
/* ================= 设置页 =================
   排布原则：
     1) 公共参数（所有摄像头共用）放最上面 —— 日常调整九成在这几项；
     2) 摄像头是「列表 + 折叠」，添加 / 删除都在列表里完成；
     3) 每路专属参数折叠在它自己那一行下面，染底 + 左侧色条 + 「仅此摄像头」
        标签，一眼就能和公共参数分开；
     4) 专属参数默认只读，点「修改」才进编辑态；改动先进草稿，必须点「保存」
        才落盘 —— 参数密，避免滑杆被手滑碰一下就改了。
*/
/* 设置页展开态。
   genOpen / diagOpen 是两个大分区；acc 是摄像头编辑里的手风琴（一次只开一段）；
   adv / outOpen 是事件侦测里的两个小节（进阶判读 / 输出画质）。 */
const SET = { open:null, editing:false, draft:null, adv:false, armed:null,
              genOpen:false, diagOpen:false, addArmed:false,
              partsOpen:false,   /* 录像残片：默认收起，展开才去扫盘检查 */
              acc:"base", outOpen:false };

function esc(s){
  return String(s==null?"":s).replace(/[&<>"']/g, m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m]));
}
function copyObj(o){ try{ return JSON.parse(JSON.stringify(o)); }catch(e){ return o; } }
function curCam(){ return CAMERAS.find(c=>c.id===state.camId) || CAMERAS[0] || null; }

function estMbps(pr, res, isNight){
  const base = res === "4MP" ? 3.4 : 2.3;
  const f = Math.pow(pr.fps/12, 0.62);
  const s = Math.pow(pr.scale/100, 1.85);
  const c = Math.pow(2, (pr.crf-24)/6);
  const g = isNight ? 0.60 : 1;
  const d = 1 - Math.min(0.38, pr.dn*0.095);
  return base*f*s*c*g*d;
}
const DN_NAMES = ["关闭","轻","中","强"];

/* 判读摘要：与 fpk/app/server/params.py 同一套口径，避免界面和实现两套说法 */
function judgeSummary(p){
  const j = judgeOf(p);
  const mult = SENS_MULT[j.sens] || 2;
  const eng = j.engine === "auto" ? "自动（优先像素差）" : ENGINE_NAME[j.engine];
  return eng + " · " + (j.scene === "auto" ? "自动识别" : SCENE_NAME[j.scene])
    + " · " + SENS_NAME[j.sens].replace(/（.*）/, "")
    + " · 门槛 " + j.pixelDelta + " · ×" + mult.toFixed(1);
}
/* 判据一句话：显示在只读页与折叠头上，用户不用展开也能核对 */
function judgeLine(p){
  const j = judgeOf(p);
  return ENGINE_NAME[j.engine === "auto" ? "pixel" : j.engine] + " · "
    + SENS_NAME[j.sens] + " · 差门槛 " + j.pixelDelta + " · 最短 " + j.minEvent + " 秒";
}

/* 全路合计占用（保留周期跟着每路走，所以放在摄像头区下面汇总）
   只算全时 + 归档浓缩：缩时片段（_clip 物化）已下线，不再计入预算。 */
function recBudgetText(){
  let full = 0, arch = 0;
  CAMERAS.forEach(c=>{
    const p = camParams[c.id];
    if(!p || !p.enabled) return;
    const dayGB = fullDayMbps(c) * 86400 / 8 / 1024;   /* Mbps×86400s÷8=MB，再÷1024=GB */
    full += dayGB * p.fullDays;
    if(cmOf(p) === "virtual")
      arch += dayGB * 0.07 * Math.max(0, (LK.archDays || 7) - p.fullDays);  /* 归档只算全时过期后的浓缩尾巴，不与全时重复 */
  });
  const need = full + arch;
  const free = (REC.diskFreeGB == null || isNaN(REC.diskFreeGB)) ? null : +REC.diskFreeGB;
  const ok = free == null ? true : need <= free;   // 还没读到磁盘就不下"空间不足"的结论
  const parts = ["全时 " + full.toFixed(0)];
  if(arch > 0.5) parts.push("归档 " + arch.toFixed(0));
  return "合计占用 ≈ <b style=\"" + (ok ? "" : "color:var(--danger)") + "\">" + need.toFixed(0)
    + " GB</b>（" + parts.join(" + ")
    + "）· 剩余 " + (free == null ? "—" : free.toFixed(0) + " GB")
    + (ok ? "" : "，<b style='color:var(--danger)'>空间不足：减全时天数或调低画质</b>");
}

function probeHtml(j){
  if(!j || !j.ok)
    return '<div class="h" style="color:var(--warn)">'
      + esc((j && j.error) || "诊断失败") + '</div>';
  const ok = !!j.trial_ok;
  let h = '<div class="pv" style="color:' + (ok ? "var(--live)" : "var(--warn)") + '">'
    + esc(j.verdict || "没有得出结论") + '</div>';
  if(j.hint) h += '<div class="h">' + esc(j.hint) + '</div>';
  const ss = Array.isArray(j.streams) ? j.streams : [];
  if(ss.length){
    h += '<div class="pstreams">' + ss.map(s=>{
      const dim = (s.w && s.h) ? (s.w + "×" + s.h) : "";
      const bits = [s.type, s.codec || "?", dim, s.fps].filter(Boolean);
      return '<span class="ps">' + esc(bits.join(" · ")) + '</span>';
    }).join("") + '</div>';
  }else if(j.probe_ok){
    h += '<div class="h">ffprobe 没读到任何流 —— 这条地址上没有可用的码流。</div>';
  }
  if(j.probe_err) h += '<div class="h">ffprobe：' + esc(j.probe_err) + '</div>';
  if(Array.isArray(j.mapping) && j.mapping.length)
    h += '<div class="h">Stream mapping：' + esc(j.mapping.join(" ／ ")) + '</div>';
  h += '<div class="h">试录产出：' + (j.trial_files || 0) + ' 个分片 · '
    + ((j.trial_bytes || 0) / 1024).toFixed(0) + ' KB</div>';
  if(j.trial_err)
    h += '<details class="recmore"><summary>试录日志</summary>'
      + '<code>' + esc(j.trial_err) + '</code></details>';
  return h;
}

async function runProbe(cid, btn){
  const box = document.getElementById("probeout-" + cid);
  if(!box) return;
  box.style.display = "block";
  box.innerHTML = '<div class="h">正在诊断：读流信息 + 试录 8 秒，约 10~20 秒…</div>';
  if(btn) btn.disabled = true;
  try{
    /* 试录 8 秒 + 读流信息，服务端自己就要十几秒：默认超时会把正常请求掐断，
       于是"诊断失败"看起来像摄像头有问题，其实是前端等不及。 */
    const j = await API.get("/api/record/probe?id=" + encodeURIComponent(cid), { timeout: 90000 });
    box.innerHTML = probeHtml(j);
  }catch(e){
    box.innerHTML = '<div class="h" style="color:var(--warn)">诊断请求失败：'
      + esc(String(e)) + '</div>';
  }finally{
    if(btn) btn.disabled = false;
  }
}

function cardRec(){
  return '<div class="gcard">'
    + '<h3>录制 <span class="sub">所有摄像头共用</span></h3>'
    + '<div class="srow" style="display:flex;align-items:center;padding-top:0;border-top:none">'
      + '<span class="n">总开关</span>'
      + '<div class="sw ' + (REC.enabled?"on":"") + '" data-rec="enabled" style="margin-left:auto"></div>'
    + '</div>'
    + '<div class="hint" style="margin:2px 0 0">每路录制进程是否在跑、最后一次 ffmpeg 报了什么错，'
      + '已经统一收进「诊断与日志 · 每路录制状态」。</div>'
    + '<div class="fld2" style="margin-top:12px">'
      + '<label>保存目录（按 摄像头名称 / 日期 / 时段 分目录 · 不常修改，默认锁定）</label>'
      + '<div style="display:flex;align-items:center;gap:8px;margin-top:2px">'
        + '<code class="mono" id="recDirShow" style="flex:1;font-size:12px;word-break:break-all">' + esc(REC.dir) + '</code>'
        + '<button class="minibtn" id="btnDirEdit">修改</button>'
      + '</div>'
      + '<div id="dirEditBox" style="display:none;margin-top:8px">'
        + '<input type="text" class="mono" value="' + esc(REC.dir) + '" id="recDir" placeholder="/vol1/@appshare/nvr-output">'
        + '<button class="btnline" id="btnDirSave" style="margin-top:8px">检测并保存</button>'
        + '<div class="hint" id="dirCheckOut" style="margin-top:6px">保存前会真实探测：目录是否存在（不存在会尝试创建）、应用是否有写权限。</div>'
      + '</div>'
      + '<div class="hint" style="margin-top:6px">全时录像存 <code style="font-size:10.5px">' + esc(REC.dir) + '/&lt;摄像头&gt;/&lt;日期&gt;/</code>，归档浓缩存 <code style="font-size:10.5px">' + esc(REC.dir) + '/_archive/&lt;摄像头&gt;/&lt;日期&gt;/</code></div>'
    + '</div>'
    + '<div class="fld2">'
      + '<label>夜间任务时刻（先归档裁剪，后清理过期）</label>'
      + '<div class="chips" style="margin-top:2px">'
        + ["01:00","02:00","03:00","04:00","05:00"].map(t=>
            '<button class="chip ' + (REC.cleanupAt===t?"on":"") + '" data-cleanat="' + t + '">' + t + '</button>').join("")
      + '</div>'
    + '</div>'
    + '<div class="hint" style="margin-top:8px">按<b>日期整目录删</b>，不在录像时倒数计时。含 <code style="font-size:10.5px">.part</code> 或待压缩文件的目录会跳过并告警。</div>'
    + '<div class="cleanline" style="margin-top:9px"><i class="dot"></i><span>'
      + (REC.lastClean.at
          ? ('上次 ' + REC.lastClean.at.slice(5)
             + ' 删除 <b>' + REC.lastClean.dirs + ' 个日期目录 / ' + REC.lastClean.files + ' 个文件</b>，释放 ' + REC.lastClean.freed + ' GB')
          : '还没有清理记录（每天 ' + REC.cleanupAt + ' 自动执行）')
      + '</span></div>'
  + '</div>';
}

function cardLink(){
  return '<div class="gcard">'
    + '<h3>事件侦测与归档 <span class="sub">所有摄像头共用 · 每路可单独覆盖</span></h3>'
    + '<div class="srow" style="display:flex;align-items:center">'
      + '<span class="n">① 事件侦测</span>'
      + '<div class="sw ' + (LK.ledger?"on":"") + '" data-ledger="1" style="margin-left:auto"></div>'
    + '</div>'
    + '<div class="h" style="margin-top:4px;line-height:1.55">事件侦测开：每路每天写几 KB 的「活动台账」(_ledger)，回放页时间轴出现活动标记、活动触发与通知可用、全时过期后能裁归档浓缩。关：只录全时、不判定，回放页无活动标记、不归档。</div>'
    + '<div class="srow">'
      + '<div class="top"><span class="n">② 归档回溯</span>'
        + '<span class="v">' + (LK.condenseMode==="off" ? "需先开启事件侦测"
            : (LK.archEnable ? "全时 + 归档浓缩" : "仅全时")) + ' · 归档保留 ' + (LK.archDays || 7) + ' 天</span></div>'
      + (LK.condenseMode==="off"
          ? '<div class="h" style="margin-top:6px;line-height:1.55">归档回溯依赖「事件侦测」写出的活动台账：关闭事件侦测后不会裁出归档浓缩片段，此处自动停用。开启事件侦测后恢复可配置。</div>'
          : ('<div class="swrow" style="border:none;padding:6px 0 0"><span>开启归档裁剪（全时 + 归档浓缩）</span>'
             + '<div class="sw ' + (LK.archEnable?"on":"") + '" data-archen="1"></div></div>'
             + '<div class="chips" style="margin-top:8px">'
             + '<button class="chip" data-archdec="1" aria-label="减少一天">−</button>'
             + '<input id="archDaysInput" type="number" inputmode="numeric" min="1" max="3650" step="1" value="'
               + (LK.archDays || 7) + '" style="width:76px;height:34px;text-align:center;'
               + 'border:1px solid var(--line);border-radius:8px;background:transparent;'
               + 'color:inherit;font-size:13.5px;padding:0 6px">'
             + '<button class="chip" data-archinc="1" aria-label="增加一天">＋</button>'
             + '<span style="align-self:center;color:var(--muted);font-size:12px">天 · 自定义 1–3650</span>'
             + '</div>'
             + '<div class="swrow" style="border:none;padding:10px 0 0">'
               + '<span>提前一天归档（当天会同时存在两份）</span>'
               + '<div class="sw ' + (LK.archPrefetch?"on":"") + '" data-archpre="1"></div>'
             + '</div>'
             + '<div class="h" style="margin-top:6px;line-height:1.55">'
               + (LK.archPrefetch
                   ? '开：全时到期<b>前一晚</b>就把活动窗口裁进 _archive，次日才删全时——那段时间同一天既有全时又有归档（两份占空间），换来一整天的补救缓冲，裁失败还能重来。'
                   : '关（默认）：等到全时<b>到期那一刻</b>才裁，裁完紧接着删源——正常只会在裁的那几十分钟里短暂重叠，不再整天并存。代价是归档片段要晚一天才出现。')
               + '</div>'
             + '<div class="h" style="margin-top:8px;line-height:1.55">'
               + '<b>总回溯 ' + (LK.archDays || 7) + ' 天 = 归档保留天数</b>：'
               + '最前面 ' + FULL_DAYS_DEFAULT + ' 天是<b>全时原画</b>（在「摄像头 → 保留与分片」可逐路改），'
               + '之后 ' + Math.max(0, (LK.archDays || 7) - FULL_DAYS_DEFAULT) + ' 天是<b>归档浓缩片段</b>（_archive）；台账永久。'
               + (LK.archPrefetch
                   ? '全时到期前夜裁、次日删。'
                   : '裁完即删源，<b>不重复占用空间</b>。')
               + '单晚最多裁 ' + (LK.archBatch || 120) + ' 个分片，裁不完的跨夜继续。</div>'
             + '<div class="h" id="archStatus" style="color:var(--muted)">' + (ARCH.sum || "候选情况加载中…") + '</div>'
             + '<button class="btnline" id="btnArchNow" style="margin-top:9px">' + (ARCH.arm ? "确认执行归档" : "立即归档（先预览）") + '</button>'
             + '<div class="hint" id="archOut" style="margin-top:6px">' + (ARCH.out || "") + '</div>'))
    + '</div>'
    /* 0.9.111：去掉「定时判定」与其「每隔多久压一次」步进器。
       判定时机现在只有一种（分片封口即判定），做成可选项只会让人以为
       换成定时能省资源 —— 台账本来就只写几 KB，等几小时再判反而让回放页
       的活动标记迟到。 */
    + (LK.condenseMode!=="off" ? '<div class="srow">'
      + '<div class="top"><span class="n">判定时机</span><span class="v">即刻判定</span></div>'
      + '<div class="h" style="margin-top:4px;line-height:1.55">' + linkHint() + '</div>'
    + '</div>' : '')
    + '<div class="srow">'
      + '<div class="top"><span class="n">录制分片长度</span><span class="v">' + Math.round(LK.segSec/60) + ' 分钟</span></div>'
      + '<div class="chips">'
        + [[60,"1 分钟"],[180,"3 分钟"],[300,"5 分钟"],[600,"10 分钟"],[900,"15 分钟"]].map(([v,l])=>
            '<button class="chip ' + (LK.segSec===v?"on":"") + '" data-linkseg="' + v + '">' + l + '</button>').join("")
      + '</div>'
      + '<div class="h">分片越短，"录完"到"开压"的间隔越短；太碎又会让静止判定的上下文不够。5 分钟是平衡点。</div>'
    + '</div>'
    /* 0.9.111：去掉「整片 MP4」选项，固定分片 MP4。
       整片 moov 必须正常收尾，被强杀就整段打不开（就是用户看到的「残片」）；
       分片 MP4 断电最多丢 1~2 秒，没有需要回退的场景，留着开关只会误选。 */
    + '<div class="srow">'
      + '<div class="top"><span class="n">录像封装</span>'
        + '<span class="v" style="color:var(--primary)">分片 MP4 · 断电也能播</span></div>'
      + '<div class="h">每个关键帧自带索引，进程被强杀或断电时已经写下的部分仍能播放，最多丢 1~2 秒。'
        + '整片封装的 moov 必须正常收尾，一旦被强杀就整段打不开（就是「录像残片」里那些待重建的文件），所以这里不再提供切换。</div>'
    + '</div>'
    + (LK.clipOut ? ('<div class="srow">'
      + '<div class="top"><span class="n">同时处理</span><span class="v">' + GS.conc + ' 路</span></div>'
      + '<div class="chips">'
        + '<button class="chip ' + (GS.conc===1?"on":"") + '" data-conc="1">串行 · 1 路</button>'
        + '<button class="chip ' + (GS.conc===2?"on":"") + '" data-conc="2">并行 · 2 路</button>'
      + '</div>'
      + '<div class="h">压制是 CPU 活，串行最稳；并行 2 路适合白天闲时。</div>'
    + '</div>'
    + '<div class="swrow"><span>只在夜间跑重编码</span><div class="sw ' + (GS.nightOnly?"on":"") + '" data-gs="nightOnly"></div></div>'
    + '<div class="swrow"><span>低优先级 · 磁盘空闲才写</span><div class="sw ' + (GS.lowPrio?"on":"") + '" data-gs="lowPrio"></div></div>'
    + '<div class="srow">'
      + '<div class="top"><span class="n">处理队列</span><span class="v" style="color:' + (LK.pending?"var(--primary)":"var(--keep)") + '">'
        + (LK.pending ? "处理中 · " + LK.pending + " 项" : "空闲") + '</span></div>'
      + '<div class="h">已压制 ' + LK.done + ' · 静默 ' + LK.silent + (LK.failed ? ' · <span style="color:var(--warn)">失败 ' + LK.failed + '</span>' : '') + (LK.lastItem ? ' · 最近 ' + LK.lastItem : '') + '</div>'
      + '<div class="h">下次入队 ' + LK.nextBatch + ' · 上次 ' + LK.lastBatch + '</div>'
      + '<button class="btnline" id="btnLinkRun" style="margin-top:9px">立即处理一批</button>'
    + '</div>') : '')
  + '</div>';
}

/* ---------------- 回放设置（0.9.97：从「事件侦测与归档」卡迁出，回到放本身） ----------------
   原「回放静默段处理」夹在 ① 事件侦测 与 归档回溯 之间，既非存储概念、又只在 virtual 下显示，
   造成编号与概念混乱。它是回放偏好（活动连播时两段活动间静止画面怎么过），与事件侦测/归档正交，
   独立成卡更清晰。data-silent 处理器仍在全局 body 上绑定，移卡不影响交互。 */
function cardPlay(){
  return '<div class="gcard">'
    + '<h3>回放设置 <span class="sub">活动连播与呈现 · 全局</span></h3>'
    + '<div class="srow">'
      + '<div class="top"><span class="n">活动连播 · 静止段处理</span><span class="v">' + silentLabel() + '</span></div>'
      + '<div class="chips">'
        + '<button class="chip ' + (PPLAY.silent==="skip"?"on":"") + '" data-silent="skip">直接跳过</button>'
        + '<button class="chip ' + (PPLAY.silent==="16"?"on":"") + '" data-silent="16">16× 快进</button>'
        + '<button class="chip ' + (PPLAY.silent==="32"?"on":"") + '" data-silent="32">32× 快进</button>'
      + '</div>'
      + '<div class="h">回放页「活动连播」播放时，两个活动之间的静止画面怎么过：跳过最省时间、观感最接近 Aqara；快进保留时间连续感，但对无核显机器 seek 压力大。该偏好仅在开启事件侦测（有活动台账可连播）时影响活动连播。</div>'
    + '</div>'
  + '</div>';
}


/* ---------------- 检测：移动侦测 + AI 识别（0.9.61 合并为一个菜单） ----------------
   用户不需要区分「帧差」和「AI」是两套实现——面对的都是「检测」：
   组 1 移动侦测（像素差，不需要 AI、随事件侦测运行、无总闸，只做说明与指路）；
   组 2 AI 识别（总闸/类别/灵敏度，缩在闸后）；
   组 3 触发与通知（活动触发三勾选 + 通知三开关，刻意放在总闸之外——
   移动侦测的通知不需要 AI，以前藏在 detEnable 里导致关了 AI 就配不了）。
   每路开关与判读参数在摄像头手风琴「检测」段；台账 cls 消费在回放页筛选 chips。
   所有选项都进 pushCfg body（LK.det 系列 / LK.nt 系列），
   服务端 norm_detect/norm_notify 强校。 */
const DET = { st:null };   /* /api/status 的 detect 快照：loaded/error/avg_ms/per_cam */
/* 0.9.80 统一取流：每路一条 RTSP 连接 → 本机端口 → 录制/取帧/AI。
   ING.st = {enabled, cams:{id:{port,clients,idle_sec,tries,stall,error,state}}}
   ING.fr = {id:{frames,ok,interval,age,error}}（老流程时为 null） */
const ING = { st:null, fr:null };
const NT  = { st:null, hist:null };
/* st   = /api/status 的 notify 快照（recent / pending / render / history 计数）
   hist = 显式点「查看发送历史」后 GET /api/notify/history 的结果：
          {loading} | {items:[...]} | {err:"..."}。
          存在这里而不是直接写 DOM，是因为卡片每次交互都会整块重渲染，
          只写 DOM 的话一点别的开关就把列表清掉了。 */

/* 平台列表的兜底。正常情况下吃服务端 /api/status 的 notify.render.platforms
   （唯一一份），这里只为「状态还没回来时按钮先画出来」。
   两边不一致由 test/test_notify_ui.py 的守卫抓——前端不该悄悄多出一个
   后端不认的平台值，那会让保存直接被 norm_notify 打回 auto。 */
const NT_PLATFORMS = [
  ["auto", "自动识别"], ["feishu", "飞书"], ["dingtalk", "钉钉"],
  ["wecom", "企业微信"], ["serverchan", "Server酱"], ["bark", "Bark"],
  ["generic", "通用 JSON"]
];
const NT_CAT = {person: "人员", two_wheeler: "两轮车", vehicle: "车辆",
                motion: "移动侦测"};

function ntPlatformList(){
  const r = (NT.st && NT.st.render) || null;
  const list = (r && r.platforms) || [];
  return list.length ? list.map(o=>[o.v, o.label]) : NT_PLATFORMS;
}

function ntPlatformLabel(v){
  const hit = ntPlatformList().find(p=>p[0] === v);
  return hit ? hit[1] : (v || "—");
}

/* 「这条通知实际会怎么发」——平台是识别来的还是手选的、文案有没有被回退。
   全部照抄 /api/status 的 notify.render，前端不再自己猜一遍平台。 */
function ntRenderLine(){
  const r = (NT.st && NT.st.render) || null;
  if(!r) return '实际发送方式：状态未就绪（稍后自动刷新）';
  let s = '实际平台：<b>' + esc(r.label || ntPlatformLabel(r.platform)) + '</b>'
    + (r.auto ? '（按地址自动识别）' : '（手动指定）');
  s += ' · 文案：' + (r.custom_template ? "自定义" : "默认");
  if(r.template_why){
    s += ' · <span style="color:var(--warn)">模板已回退：' + esc(r.template_why) + '</span>';
  }
  const h = (NT.st && NT.st.history) || null;
  if(h && h.persisted) s += ' · 已落盘 ' + (h.total || 0) + ' 条';
  return s;
}

function ntWhen(ts){
  const n = Number(ts);
  if(!n) return "—";
  const d = new Date(n * 1000);
  return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " "
    + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

/* 发送历史：只列事实（时间 / 路 / 类别 / 成败 / 平台 / 重试次数）。
   失败行把错误原文压到 60 字——排障要看的是「HTTP 404」这种码，
   而不是一整段被截断的堆栈。 */
function ntHistHtml(){
  const h = NT.hist;
  if(!h) return "";
  if(h.loading) return '<div class="hint" style="margin-top:10px">正在读取发送历史…</div>';
  if(h.err) return '<div class="hint" style="margin-top:10px;color:var(--warn)">读取失败：' + esc(h.err) + '</div>';
  const items = h.items || [];
  if(!items.length){
    return '<div class="hint" style="margin-top:10px">还没有落盘记录。发出一条通知（或点「测试发送」）后这里会出现。</div>';
  }
  const rows = items.map(x=>{
    const ok = !!x.ok;
    const who = (x.cam || "?") + " · " + (NT_CAT[x.cat] || x.cat || "?");
    const tail = ok
      ? ("成功" + (x.ms ? " " + x.ms + "ms" : ""))
      : ("失败 " + esc(String(x.err || "").slice(0, 60))
         + (x.tries ? "（试 " + x.tries + " 次）" : ""));
    return '<div class="hint" style="margin-top:4px">'
      + '<span style="color:var(--' + (ok ? "keep" : "warn") + ')">' + (ok ? "✓" : "✗") + '</span> '
      + esc(ntWhen(x.at)) + ' · ' + esc(who) + ' · ' + esc(ntPlatformLabel(x.platform))
      + ' · ' + tail + '</div>';
  }).join("");
  const total = ((NT.st && NT.st.history && NT.st.history.total) || items.length);
  return '<div class="hint" style="margin-top:10px">最近 ' + items.length
    + ' 条（最新在前）· 落盘共 ' + total + ' 条</div>' + rows;
}

/* 0.9.122：检测"能不能用"由后端给 state 判（disabled/starting/unavailable/ready），
   这里只做翻译。以前前端自己拼 running/loaded/error 三段判断，同一个现场在
   设置页说"未运行"、在诊断卡说"加载中"——用户不知道该信哪个，也不知道要不要重启。
   st.state 缺失（旧服务端）时仍走老分支，页面不会因为后端没升级就白屏。 */
function detStateZh(st){
  const s = st || {};
  if(s.state === "disabled") return {t:"已关闭", warn:false};
  if(s.state === "starting") return {t:"加载中…", warn:false};
  if(s.state === "unavailable")
    return {t:"不可用 · " + String(s.error_message || s.error || "原因未知").slice(0, 60), warn:true};
  if(s.state === "ready") return {t:"正常", warn:false};
  /* 旧服务端兜底 */
  if(!s.running) return {t:"未运行", warn:true};
  if(s.error) return {t:"不可用 · " + String(s.error).slice(0, 60), warn:true};
  if(!s.loaded) return {t:"加载中…", warn:false};
  return {t:"正常", warn:false};
}

/* 失败码 → 下一步该干什么。只报错不指路，用户只能来问；这几个码的处置方式
   差别很大（装 ffmpeg / 重装应用 / 换 wheel），不能笼统写"检测不可用"。 */
function detHintOf(st){
  const c = (st && st.error_code) || "";
  if(c === "FFMPEG_NOT_FOUND") return "这台机器上没找到 ffmpeg：装好 ffmpeg，或在设置里指定路径后重启服务。";
  if(c === "MODEL_MISSING")    return "随包模型 yolo11n.onnx 不见了：重装应用，或从官方包补回 share/models/。";
  if(c === "RUNTIME_FAIL")     return "随包 onnxruntime / numpy 与本机 Python 版本对不上：核对下面 Python / wheel 一行。";
  if(c === "MODEL_LOAD_FAIL")  return "模型加载报错：常见于 CPU 指令集不支持，完整原因在服务运行日志里。";
  if(c === "WORKER_DOWN")      return "检测进程没起来：服务可能是旧版本，或刚改完配置还没重启。重开一次服务再刷新。";
  if(c === "INFER_ERR" || c === "FRAME_FAIL")
    return "推理／取帧失败：看下面每路的「最近错误」，多数是摄像头侧问题，与录像无关。";
  return "服务可能是旧版本（没有检测 worker），或刚改完配置还没重启。重开一次服务再刷新。";
}

function detUnknownOf(st){
  const cams = (st && st.per_cam) || {};
  let n = 0;
  Object.keys(cams).forEach(id=>{ n += cams[id].unknown || 0; });
  return n;
}

/* 推理设备（0.9.125）：device 这个键以前只躺在配置里，引擎恒用 CPU，诊断卡
   标题还硬写着「YOLO11n · CPU」。现在把**实际建在哪个后端**摆出来，与配置里
   写的是什么分开说 —— 配置是意图，引擎才是事实。
   warn 只在"点名要 GPU 却回落 CPU"时亮：用户自己选 cpu 不是降级，不该亮黄。 */
const DET_DEV_ZH = {cuda:"CUDA", tensorrt:"TensorRT", rocm:"ROCm",
                    openvino:"OpenVINO", dml:"DirectML", cpu:"CPU"};
const DET_PROV_KIND = {
  CUDAExecutionProvider:"cuda", TensorrtExecutionProvider:"tensorrt",
  ROCMExecutionProvider:"rocm", OpenVINOExecutionProvider:"openvino",
  DmlExecutionProvider:"dml", CPUExecutionProvider:"cpu"
};
function detDevName(k){
  const s = String(k || "");
  return DET_DEV_ZH[s] || (s ? s.toUpperCase() : "");
}
function detDevProv(p){
  const s = String(p || "");
  return DET_DEV_ZH[DET_PROV_KIND[s]] || s.replace(/ExecutionProvider$/, "");
}
function detDevOf(st){
  const d = (st && st.device) || {};
  const req = String(d.requested || "auto");
  const act = String(d.actual || "");
  const why = String(d.fallback_reason || "");
  if(!act) return {tag:"", text:(st && st.loaded)
    ? "服务端未上报（旧版本，无法确认实际后端）"
    : "尚未加载（第一帧推理完才知道实际后端）", warn:false};
  const want = {auto:"自动", cpu:"CPU", gpu:"GPU"}[req] || detDevName(req);
  let text = "实际 " + detDevName(act) + " · 请求 " + want;
  const ps = (d.providers || []).map(detDevProv).filter(s=>!!s);
  if(ps.length) text += " · " + ps.join(" → ");
  if(why) text += " · " + why;
  return {tag:detDevName(act) + (why ? "（已回落）" : ""), text:text, warn:!!why};
}

function detStatusLine(){
  const st = DET.st;
  if(!LK.detEnable) return '<span class="v">已关闭</span>';
  const z = detStateZh(st);
  if(z.warn) return '<span class="v" style="color:var(--warn)">' + esc(z.t) + '</span>';
  if(z.t !== "正常") return '<span class="v">' + esc(z.t) + '</span>';
  const cams = st.per_cam || {};
  let today = 0, hits = 0;
  Object.keys(cams).forEach(id=>{
    today += cams[id].today || 0;
    const cl = cams[id].cls || {};
    Object.keys(cl).forEach(k=>{ hits += cl[k]; });
  });
  /* 未知单独列：它既不是"在正常工作"，也不是"没人来过"，混进命中数会骗人 */
  const unk = detUnknownOf(st);
  return '<span class="v">正常 · 今日 ' + today + ' 帧 / 命中 ' + hits + ' 次'
    + (unk ? ' · <span style="color:var(--warn)">未知 ' + unk + '</span>' : '')
    + (st.avg_ms ? ' · ' + st.avg_ms + 'ms/帧' : '') + '</span>';
}

function cardDetect(){
  /* 0.9.112：三块「同样的三个类别」合并成一张矩阵。
     以前是「检测类别」3 个开关、「活动触发」3 个开关、「通知」3 个开关，
     九次重复、只有细节不同（触发与通知多了移动侦测、少了单独的两轮车），
     用户很难看出三者是什么关系。现在一行一个类别，三列分别是
     记录 / 算活动 / 通知，语义直接摆在一行里。
     data-detcls / data-dettrig / data-ntsw 三个属性名刻意保持不变，
     所以下面的事件绑定代码一行都不用改。 */
  const aiOff = !LK.detEnable;
  const cell = (attr, k, on) =>
    '<span class="cmx-c"><div class="sw ' + (on ? 'on' : '') + '" '
    + attr + '="' + k + '"></div></span>';
  const dash = '<span class="cmx-dash">—</span>';
  const join = '<span class="cmx-dash">并入车辆</span>';
  const row = (label, rec, trig, nt, off) =>
    '<div class="cmx-r' + (off ? ' off' : '') + '">'
    + '<span class="cmx-lab">' + label + '</span>' + rec + trig + nt + '</div>';
  return '<div class="gcard">'
    + '<h3>检测 <span class="sub">移动侦测 + AI 识别 · 所有摄像头共用 · 每路可单独覆盖</span></h3>'
    /* 组 1：移动侦测（像素差）。不需要 AI、没有自己的总闸——它随「事件侦测与归档
       方案」运行，方案关闭就不判定。参数在每路「检测」段，这里只做说明与指路。 */
    + '<div class="srow" style="padding-top:0;border-top:none">'
      + '<div class="top"><span class="n">移动侦测（像素差）</span>'
        + '<span class="v">' + (LK.condenseMode === "off"
            ? "未启用 · 事件侦测已关闭" : "运行中 · 随事件侦测") + '</span></div>'
      + '<div class="h" style="margin-top:8px;line-height:1.55">不需要 AI：ffmpeg 把画面缩到 320×180 后逐像素比对，'
        + '变化超过门槛的时段记为活动，写进活动台账。它随「事件侦测与归档」运行——'
        + '方案选「关闭」就<b>不判定</b>，回放页时间轴也不会有活动标记。'
        + '灵敏度、判读引擎、像素差门槛等参数在<b>摄像头设置 → 检测段</b>，每路可单独调；'
        + '判读滤镜是否可用见「诊断与日志 · 环境自检」。</div>'
    + '</div>'
    /* 组 2：AI 总闸 */
    + '<div class="srow">'
      + '<div class="top"><span class="n">AI 识别（人 / 两轮车 / 车）</span>' + detStatusLine() + '</div>'
      + '<div class="swrow" style="border:none;padding:6px 0 0"><span>开启（每 5 秒/路抽帧 · CPU 推理）</span>'
        + '<div class="sw ' + (LK.detEnable?"on":"") + '" data-deten="1"></div></div>'
      + '<div class="h" style="margin-top:8px;line-height:1.55">默认关闭。开启后每 5 秒对每路抽 1 帧推理（YOLO11n · CPU），'
        + '检出的人 / 两轮车 / 车辆按下表与<b>每路勾选的类别</b>记进活动台账，与移动侦测的台账合并后'
        + '一起供给回放页筛选、活动触发和通知。不换硬件也能跑：i3-12100 扛 5 路只占约半个核。</div>'
    + '</div>'
    + (LK.detEnable ? (
      /* 灵敏度三档 → 分品类阈值 */
      '<div class="srow">'
      + '<div class="top"><span class="n">灵敏度</span><span class="v">'
        + (LK.detSens==="high" ? "高" : LK.detSens==="low" ? "低" : "标准") + '</span></div>'
      + '<div class="chips">'
        + '<button class="chip ' + (LK.detSens==="high"?"on":"") + '" data-detsens="high">高</button>'
        + '<button class="chip ' + (LK.detSens==="std"?"on":"") + '" data-detsens="std">标准</button>'
        + '<button class="chip ' + (LK.detSens==="low"?"on":"") + '" data-detsens="low">低</button>'
      + '</div>'
      + '<div class="h">高=多报（阈值 人.45/两轮.28/车.35），低=少报（.55/.38/.45）。漏检比误检好——误检会污染台账和事件判定。</div>'
    + '</div>'
      /* 0.9.111：删掉「实时检测（下个版本）」这个永远点不动的按钮。 */
    + '<div class="srow">'
      + '<div class="top"><span class="n">检测模式</span><span class="v">快照检测 · 每 5 秒 / 路</span></div>'
      + '<div class="h">按固定间隔抓一帧做判读，对「有人 / 有车经过」的台账事件已经够用，且几乎不占 CPU。</div>'
    + '</div>'
    ) : '')
    /* 组 3：类别矩阵。三件事写在一行里，取代原来三块重复的开关。 */
    + '<div class="srow">'
      + '<div class="n">按类别控制</div>'
      + '<div class="h" style="margin-top:2px;line-height:1.55">一行一个类别，三列分别管三件事：'
        + '<b>记录</b>＝写不写进活动台账，<b>算活动</b>＝进不进事件列表与缩时，<b>通知</b>＝发不发 Webhook。</div>'
      + '<div class="cmx">'
        + '<div class="cmx-h"><span class="cmx-lab">类别</span>'
          + '<span class="cmx-col">记录</span>'
          + '<span class="cmx-col">算活动</span>'
          + '<span class="cmx-col">通知</span></div>'
        + row("移动侦测", dash,
              cell("data-dettrig", "motion", LK.detTrigger.motion),
              cell("data-ntsw", "motion", LK.ntMotion), false)
        + row("人员",
              cell("data-detcls", "person", LK.detCls.person),
              cell("data-dettrig", "person", LK.detTrigger.person),
              cell("data-ntsw", "person", LK.ntPerson), aiOff)
        + row("两轮车",
              cell("data-detcls", "two_wheeler", LK.detCls.two_wheeler),
              join, join, aiOff)
        + row("车辆",
              cell("data-detcls", "vehicle", LK.detCls.vehicle),
              cell("data-dettrig", "vehicle", LK.detTrigger.vehicle),
              cell("data-ntsw", "vehicle", LK.ntVehicle), aiOff)
      + '</div>'
      + '<div class="h" style="margin-top:8px;line-height:1.55">'
        + '移动侦测是像素差，不分类别，所以「记录」列没有开关；'
        + '两轮车在<b>触发与通知</b>上并入车辆（后端同一口径，无法单独控制），'
        + '要它不参与就关车辆那两列，想连录像也别留就关它「记录」列的开关——'
        + '但那样这段画面不会再进归档。记录与算活动各列至少留一项，通知可以全关。'
        + (aiOff ? '<br><b>AI 未开启</b>：人员 / 两轮车 / 车辆三行暂不生效，先打开上方总闸。' : '')
      + '</div>'
    + '</div>'
    /* 组 4：通知通道。类别开关已经上移到矩阵，这里只剩投递参数。 */
    + '<div class="srow">'
      + '<div class="n">通知通道</div>'
      + '<div class="swrow" style="border:none;padding:8px 0 0"><span>冷却（同路同类最小间隔）</span>'
        + '<div class="stepper"><button data-ntcd="-30">−</button><span class="val">' + LK.ntCooldown + ' 秒</span><button data-ntcd="30">+</button></div></div>'
      + '<input type="url" class="mono" id="ntWebhook" placeholder="https://example.com/webhook" value="' + esc(LK.ntWebhook) + '"'
        + ' style="margin-top:8px;width:100%;box-sizing:border-box;height:36px;padding:0 10px;'
        + 'border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font-size:12.5px">'
      + '<div class="h" style="margin-top:6px;line-height:1.55">上表里勾了「通知」的类别，经这个 Webhook 发出'
        + '（飞书 / 钉钉 / 企业微信 / Server酱 / Bark 等标准格式）。'
        + 'fnOS 未开放应用级系统通知接口，故不走系统通知中心。没有冷却，移动侦测通知能把手机炸掉。</div>'
      /* 平台：默认按地址识别。公司网关把机器人地址代理成自家域名时 host 认不出来，
         只能手选；手选后服务端按该平台的 body 发，不再按 host 猜。 */
      + '<div class="h" style="margin-top:10px;line-height:1.55"><b>平台</b>：默认按地址自动识别；'
        + '机器人地址被网关改写过就手选一项。</div>'
      + '<div class="chips" style="margin-top:6px">'
        + ntPlatformList().map(p=>
            '<button class="chip ' + ((LK.ntPlatform || "auto") === p[0] ? "on" : "") + '"'
            + ' data-ntplat="' + esc(p[0]) + '">' + esc(p[1]) + '</button>').join("")
      + '</div>'
      /* 文案模板：留空用默认；非法占位符不会丢通知，只回退并在这里说明原因。 */
      + '<div class="h" style="margin-top:10px;line-height:1.55"><b>文案模板</b>（留空＝默认文案）。可用占位符：'
        + esc(((NT.st && NT.st.render && (NT.st.render.vars || []).join("  ")) || "{cam}  {cat}  {label}  {time}  {date}"))
      + '</div>'
      + '<input type="text" class="mono" id="ntTemplate" maxlength="500"'
        + ' placeholder="【NVR · {cam}】检测到{label} · {time}" value="' + esc(LK.ntTemplate) + '"'
        + ' style="margin-top:6px;width:100%;box-sizing:border-box;height:36px;padding:0 10px;'
        + 'border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font-size:12.5px">'
      + '<div class="h" id="ntRender" style="margin-top:6px;line-height:1.55">' + ntRenderLine() + '</div>'
      + '<div style="display:flex;gap:8px;margin-top:8px">'
        + '<button class="btnline" id="btnNtTest" style="flex:1">测试发送</button>'
        + '<button class="btnline" id="btnNtSave" style="flex:1">保存 Webhook</button>'
        + '<button class="btnline" id="btnNtHist" style="flex:1">查看发送历史</button>'
      + '</div>'
      + '<div class="hint" id="ntOut" style="margin-top:6px"></div>'
      + '<div id="ntHist">' + ntHistHtml() + '</div>'
    + '</div>'
  + '</div>';
}

function cardLook(){
  return '<div class="gcard">'
    + '<h3>外观 <span class="sub">与飞牛系统明暗一致</span></h3>'
    + '<div class="chips" style="margin-top:0">'
      + '<button class="chip" data-theme-set="auto">跟随系统</button>'
      + '<button class="chip" data-theme-set="light">浅色</button>'
      + '<button class="chip" data-theme-set="dark">深色</button>'
    + '</div>'
  + '</div>';
}

/* ---------------- 诊断日志：直接看 / 复制 / 清除 ---------------- */
/* 顶部版本徽标（0.9.111）：进设置页就能看到版本，不用展开「诊断与日志 → 环境自检」。
   拉不到就保持 v—（服务刚重启/离线属于正常瞬间，别用 toast 打扰用户）；
   服务端没回 version 说明后台还是旧进程，用告警色标出来。 */
async function loadTopVersion(){
  const el = document.getElementById("verTop");
  if(!el) return;
  try{
    const d = await API.get("/api/health");
    if(!d || typeof d.version !== "string" || !d.version){
      el.textContent = "v未知";
      el.classList.add("bad");
      el.title = "旧版服务进程仍在运行 · 展开「诊断与日志」看排查步骤";
      return;
    }
    el.textContent = "v" + d.version;
    el.classList.remove("bad");
    el.title = "服务端版本 v" + d.version;
  }catch(e){ /* 保持 v— */ }
}

async function loadDiagEnv(){
  const el = document.getElementById("diagEnv");
  const warn = document.getElementById("diagWarn");
  if(!el) return;
  try{
    const d = await API.get("/api/health");
    const stale = !d || typeof d.version !== "string" || !d.version;
    if(stale){
      // 服务端没返回 version → 磁盘上是新代码，但后台进程仍是旧版
      // （升级时旧进程没被杀掉）。此时 ffmpeg/ffprobe 状态其实是「未知」而非「未找到」。
      el.innerHTML = "服务端版本 <b class='bad'>未知（旧版服务进程仍在运行）</b>";
      if(warn){
        warn.style.display = "block";
        warn.innerHTML = "⚠️ 检测到<b>旧版服务进程仍在运行</b>：新代码只替换到了磁盘，后台跑的仍是老版本。"
          + "请在飞牛「应用中心」里对本应用<b>停止 → 启动</b>（关闭窗口不算重启），"
          + "或点下方「重启服务」按钮；若按钮提示不可用，则必须手动在应用中心重启。";
      }
    }else{
      const ff = d.ffmpeg ? ("<b class='ok'>✓</b> " + esc(d.ffmpeg))
                          : "<b class='bad'>✗ 未找到 ffmpeg</b>（预览/录制会失效）";
      const fp = d.ffprobe ? ("<b class='ok'>✓</b> " + esc(d.ffprobe))
                           : "<b class='bad'>✗ 未找到 ffprobe</b>（事件侦测分析会失效）";
      el.innerHTML = "服务端版本 <b>v" + esc(d.version) + "</b>"
        + "　ffmpeg " + ff
        + "　ffprobe " + fp
        + '<div style="margin-top:6px">像素判读 <span id="pixState" class="empty">未检测</span> '
        + '<button class="logbtn" id="btnPix" style="margin-left:6px">自检</button></div>';
      /* 像素判读要额外四个滤镜（tblend/signalstats/lut/boxblur）。系统 ffmpeg
         缺了就会自动回落码流曲线，但用户有权知道到底用的是哪套 —— 一个按钮
         换一句人话，比事后翻日志强。 */
      const pb = document.getElementById("btnPix");
      if(pb) pb.onclick = async ()=>{
        const st = document.getElementById("pixState");
        if(st) st.textContent = "检测中…";
        try{
          /* 像素自检要在服务端跑一遍 ffmpeg 解帧并与参考帧比差，几十秒是常态 */
          const dd = await API.get("/api/diag/pixel", { timeout: 60000 });
          if(!st) return;
          st.innerHTML = dd.ok
            ? "<b class='ok'>✓ 可用</b>（" + dd.frames + " 帧 · 峰值 "
              + ((dd.peak || 0) * 100).toFixed(1) + "%）"
              + (dd.reason ? " · " + esc(dd.reason) : "")
            : "<b class='bad'>✗ 不可用</b> · " + esc(dd.reason || "未知原因");
        }catch(e){ if(st) st.textContent = "检测失败：" + e; }
      };
      if(warn) warn.style.display = "none";
    }
  }catch(e){
    el.innerHTML = "环境自检失败：" + esc(String(e));
  }
}
async function restartDiag(){
  const btn = document.getElementById("btnRestart");
  try{
    const d = await API.post("/api/restart", {});
    if(d && d.ok){
      if(btn){ btn.disabled = true; btn.textContent = "正在重启…"; }
      toast("toast2", "服务正在重启，3 秒后自动刷新");
      setTimeout(()=>{ location.reload(); }, 3000);
    }else{
      toast("toast2", "重启接口不可用——当前跑的是旧版服务，请在应用中心手动停止→启动");
    }
  }catch(e){
    toast("toast2", "重启接口不可用——请在应用中心手动停止→启动");
  }
}
/* 运行日志：分级 + 只看警告（0.9.116）
   以前整块是一个 <pre>，一次 textContent 铺完，所有行一个颜色。真机 2 小时
   日志里 283 行「处理失败」和启动横幅长得一模一样，出问题时人眼筛不出来。
   现在逐行建 <span>、逐行 textContent —— 日志里的 < > & 一律当字面量，不存在
   拼 HTML 的转义风险；复制的还是 pre.textContent，行间用文本节点换行保留。 */
const LOGP = { lines: [], warn: 0, err: 0, only: false,
               /* 0.9.117：内存环只有 800 条（真机两三小时），翻昨天的事得读落盘文件。
                  file=true 表示当前看的是文件，fStart 是已载入部分在文件里的起始字节，
                  「加载更早」就把它当下一页的 end 传回去。 */
               file: false, fStart: 0, fSize: 0, fMore: false };
const LOG_FILE_PAGE = 800;      // 每次从文件取多少行
const LOG_FILE_CAP  = 6000;     // 面板里最多堆多少行，再多手机端会卡

function logLineClass(t){
  if(t.indexOf("[ERR] ") >= 0) return "le";
  if(t.indexOf("[WARN] ") >= 0) return "lw";
  return "";
}

function logLevelCount(rows){
  let w = 0, e = 0;
  rows.forEach(t=>{
    if(t.indexOf("[ERR] ") >= 0) e++;
    else if(t.indexOf("[WARN] ") >= 0) w++;
  });
  return { w: w, e: e };
}

function logSizeText(n){
  if(!n) return "0 KB";
  if(n >= 1048576) return (n/1048576).toFixed(1) + " MB";
  return Math.max(1, Math.round(n/1024)) + " KB";
}

/* anchor=true：往前翻页时把视口钉在原处。不钉的话每翻一页屏幕都跳到底部，
   人眼刚扫到的那一行直接跑掉，翻历史就没法用了。 */
function renderLog(anchor){
  const pre = document.getElementById("diagLogText");
  if(!pre) return;
  const box = pre.parentElement;
  const stick = (anchor && box) ? (box.scrollHeight - box.scrollTop) : 0;
  const rows = LOGP.only
    ? LOGP.lines.filter(t=>t.indexOf("[WARN] ") >= 0 || t.indexOf("[ERR] ") >= 0)
    : LOGP.lines;
  pre.textContent = "";
  if(!rows.length){
    const sp = document.createElement("span");
    sp.className = "empty";
    if(LOGP.only){
      sp.textContent = LOGP.file
        ? "已载入的这段里没有警告或错误（可以继续「加载更早」）"
        : "没有警告或错误 —— 本次启动以来没出现异常";
    }else{
      sp.textContent = LOGP.file ? "日志文件是空的（「清除日志」会清空它）" : "（暂无日志）";
    }
    pre.appendChild(sp);
  }else{
    const frag = document.createDocumentFragment();
    rows.forEach(t=>{
      const sp = document.createElement("span");
      sp.className = logLineClass(t);
      sp.textContent = t;
      frag.appendChild(sp);
      frag.appendChild(document.createTextNode("\n"));
    });
    pre.appendChild(frag);
  }
  /* 内存环的计数是服务端从启动起累计的；文件模式只能数「已载入的这段」，
     两者口径不同，所以标题上必须写清是「警告 X」还是「本页警告 X」。 */
  const cnt = LOGP.file ? logLevelCount(LOGP.lines) : { w: LOGP.warn, e: LOGP.err };
  const bad = cnt.w + cnt.e;
  const meta = document.getElementById("diagLogMeta");
  if(meta){
    meta.textContent = LOGP.file
      ? ("历史日志 · 已载入 " + LOGP.lines.length + " 行 · 文件 " + logSizeText(LOGP.fSize)
         + (bad ? " · 本页警告 " + cnt.w + " / 错误 " + cnt.e : ""))
      : ((LOGP.lines.length ? "最近 " + LOGP.lines.length + " 行" : "无日志")
         + (bad ? " · 警告 " + LOGP.warn + " / 错误 " + LOGP.err : ""));
  }
  const btn = document.getElementById("btnLogWarn");
  if(btn){
    btn.textContent = LOGP.only ? "显示全部" : (bad ? "只看警告 (" + bad + ")" : "只看警告");
    btn.classList.toggle("on", LOGP.only);
  }
  const hist = document.getElementById("btnLogHist");
  if(hist){
    hist.textContent = LOGP.file ? "回到实时" : "历史日志";
    hist.classList.toggle("on", LOGP.file);
  }
  const more = document.getElementById("btnLogMore");
  if(more){
    /* 到上限也禁用：否则按钮写着「已到上限」却还点得动，点下去什么也不发生 */
    const cap = LOGP.lines.length >= LOG_FILE_CAP;
    more.style.display = LOGP.file ? "" : "none";
    more.disabled = !(LOGP.file && LOGP.fMore) || cap;
    more.textContent = cap ? "已到上限" : "加载更早";
  }
  if(!box) return;
  if(anchor) box.scrollTop = Math.max(0, box.scrollHeight - stick);
  else box.scrollTop = box.scrollHeight;    // 滚动到底部，最新的在最下面
}

/* 读落盘日志的一段。more=true 表示「接着往上翻」：把当前已载入部分的起始
   字节当作下一页的 end，后端从那里往前再取一批。 */
async function loadLogFile(more){
  const pre = document.getElementById("diagLogText");
  if(!pre) return;
  if(!more){
    LOGP.fStart = 0; LOGP.fMore = false; LOGP.fSize = 0;
  }
  const endQ = (more && LOGP.fStart > 0) ? ("&end=" + LOGP.fStart) : "";
  try{
    const d = await API.get("/api/log/file?lines=" + LOG_FILE_PAGE + endQ);
    if(!d || !d.ok){
      toast("toast2", (d && d.reason) || "读取日志文件失败");
      return;
    }
    const rows = Array.isArray(d.lines) ? d.lines : [];
    /* 成功之后才切换模式：失败时保持原样（还能再点一次），不至于把面板清空成
       「暂无日志」让人以为日志没了 */
    LOGP.file = true;
    LOGP.fSize = d.size | 0;
    LOGP.fStart = d.start | 0;
    LOGP.lines = more ? rows.concat(LOGP.lines) : rows;
    /* 到上限就收手：DOM 里堆上万行会把手机卡死，那时候该用「导出诊断」而不是翻页 */
    LOGP.fMore = !!d.more && LOGP.lines.length < LOG_FILE_CAP;
    renderLog(!!more);
    if(more && !rows.length) toast("toast2", "没有更早的记录了");
  }catch(e){
    toast("toast2", "读取日志文件失败：" + e);
  }
}

function toggleLogSource(){
  if(LOGP.file){                        // 回到实时：重新拉内存环（这期间可能又写了新日志）
    LOGP.file = false; LOGP.fStart = 0; LOGP.fMore = false;
    loadDiagLog();
  }else{
    loadLogFile(false);
  }
}

async function loadDiagLog(){
  const pre = document.getElementById("diagLogText");
  if(!pre) return;
  try{
    const d = await API.get("/api/log");
    LOGP.lines = (d && Array.isArray(d.lines)) ? d.lines : [];
    LOGP.warn = (d && d.warn) | 0;
    LOGP.err = (d && d.err) | 0;
    LOGP.file = false;
    renderLog();
  }catch(e){
    /* 旧版服务进程会把 /api/log 当静态文件交回 HTML：解析不出 JSON，请求层抛的
       就是"HTTP 200 <!doctype…>"。这种情形必须给出"去重启"的具体指引，
       而不是一句"读取失败"——那会让人去查网络和端口。 */
    if(e && e.status === 200){
      LOGP.lines = []; LOGP.warn = 0; LOGP.err = 0;
      pre.textContent = "无法读取日志：服务端返回的不是日志接口（HTTP 200 但内容不是 JSON）。\n"
        + "后台跑的还是旧版服务进程——请点上方「重启服务」按钮，或在飞牛应用中心里「停止 → 启动」本应用。";
      return;
    }
    pre.textContent = "无法读取日志：" + e;
  }
}
async function copyDiagLog(){
  const pre = document.getElementById("diagLogText");
  const txt = pre ? pre.textContent : "";
  try{
    await navigator.clipboard.writeText(txt);
    toast("toast2", "日志已复制到剪贴板");
  }catch(e){
    try{                                     // 兼容旧 WebView / 非安全上下文
      const ta = document.createElement("textarea");
      ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); document.body.removeChild(ta);
      toast("toast2", "日志已复制到剪贴板");
    }catch(e2){ toast("toast2", "复制失败，请长按日志框手动选择复制"); }
  }
}
async function clearDiagLog(){
  try{ await API.post("/api/log/clear", {}); }catch(e){}
  LOGP.file = false; LOGP.fStart = 0; LOGP.fMore = false;   // 文件被清空，历史模式没意义了
  await loadDiagLog();
  toast("toast2", "日志已清除");
}

async function loadRetention(){
  try{
    const d = await API.get("/api/retention");
    if(!d || d.ok === false) return;
    if(d.last && d.last.at){
      REC.lastClean = {
        at: d.last.at, dirs: d.last.deleted_dirs || 0,
        files: d.last.files || 0,
        freed: Math.round((d.last.freed_mb || 0) / 1024 * 10) / 10
      };
    }
  }catch(e){}
}

/* ---------------- 诊断导出：真的下载一份 ---------------- */
function exportDiag(btn){
  if(btn){ btn.disabled = true; btn.textContent = "正在导出…"; }
  try{
    const a = document.createElement("a");
    a.href = "/api/log/export?_t=" + Date.now();
    a.download = "";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(()=>{ a.remove(); }, 0);
    toast("toast2", "诊断包已开始下载（凭据已脱敏）");
  }catch(e){
    toast("toast2", "导出失败：" + e);
  }finally{
    setTimeout(()=>{ if(btn){ btn.disabled = false; btn.textContent = "导出诊断包"; } }, 800);
  }
}

/* ---------------- 扫描：列出真实的磁盘占用 ---------------- */
async function runScan(btn){
  if(btn) btn.disabled = true;
  toast("toast2", "正在扫描录像目录…");
  try{
    const d = await API.post("/api/scan", {});
    if(!d || !d.ok){ toast("toast2", "扫描失败：服务端无响应"); return; }
    const rows = d.channels || [];
    const empty = rows.filter(r => !r.files).map(r => r.name);
    const msg = "扫描完成：" + rows.reduce((a,r)=>a+(r.files||0),0) + " 个分片 / "
      + (Math.round((d.total && d.total.bytes || 0)/1048576/102.4)/10) + " GB"
      + (empty.length ? "；无数据：" + empty.join("、") : "");
    toast("toast2", msg);
    loadDiagLog();
    fetchRecordings();
    renderCamTabs();
  }catch(e){
    toast("toast2", "扫描失败：" + e);
  }finally{
    if(btn) btn.disabled = false;
  }
}

/* ---------------- 摄像头列表 ---------------- */
function camIcon(){
  return '<svg viewBox="0 0 24 24"><path d="M3 8.5A2.5 2.5 0 015.5 6h9A2.5 2.5 0 0117 8.5v7a2.5 2.5 0 01-2.5 2.5h-9A2.5 2.5 0 013 15.5z"/><path d="M17 10.5l4-2.2v7.4l-4-2.2"/></svg>';
}

function renderCamList(){
  let h = '<div class="zone cam">'
    + '<div class="zonehead"><span class="zi">◉</span>'
    + '<div><b>摄像头</b><div class="zs">每路独立参数 · 点一行展开</div></div>'
    + '<span class="cnt">' + CAMERAS.length + ' 台</span></div>';
  if(!CAMERAS.length){
    h += '<div class="empty">还没有摄像头<br>点下面的「添加摄像头」，填入主码流 RTSP 地址即可开始录制</div>';
  }
  CAMERAS.forEach(c=>{
    const p = camParams[c.id] || defaultCamParams(c, 0);
    const open = SET.open === c.id;
    let state0;
    if(!p.main && !c.hasUrl) state0 = '<span style="color:var(--warn)">未填主码流</span>';
    else if(p.main && p.enabled === false) state0 = '<span style="color:var(--faint)">已停用</span>';
    else if(!p.main && c.hasUrl) state0 = '<span style="color:var(--faint)">地址存于 NAS · 本机未显码</span>';
    else if(p.enabled === false) state0 = '<span style="color:var(--faint)">已停用</span>';
    else state0 = c.online ? '<span style="color:var(--keep)">● 在线</span>' : '<span style="color:var(--faint)">● 未连接</span>';
    /* 压缩方案摘要胶囊：四路的差异一眼可比，不用逐个点开 */
    const chip = p.enabled === false
      ? '<span class="camchip off">已停用</span>'
      : (cmOf(p) === "off"
          ? '<span class="camchip off">仅全时</span>'
          : '<span class="camchip">' + cmLabel(cmOf(p)) + '</span>');
    h += '<div class="camrow' + (open?" open":"") + '" data-camrow="' + c.id + '">'
      + '<button class="dragh" data-drag="' + c.id + '" title="按住拖动排序">'
      + '<svg viewBox="0 0 16 16"><circle cx="5" cy="4" r="1.4"/><circle cx="11" cy="4" r="1.4"/>'
      + '<circle cx="5" cy="8" r="1.4"/><circle cx="11" cy="8" r="1.4"/>'
      + '<circle cx="5" cy="12" r="1.4"/><circle cx="11" cy="12" r="1.4"/></svg></button>'
      + '<div class="avatar">' + camIcon() + '</div>'
      + '<div class="meta"><div class="t">' + esc(c.name) + '</div>'
        + '<div class="s"><span>' + esc(c.ch) + '</span><span style="color:var(--line)">|</span><span>' + c.res + '</span>'
        + '<span style="color:var(--line)">|</span>' + state0 + chip
        + '</div></div>'
      /* 删除收进编辑页底部：行内只留一个箭头，从源头消掉"点错删除" */
      + '<span class="chev">›</span>'
    + '</div>';
    if(open){
      const p2 = camParams[c.id];
      h += SET.editing ? renderCamEdit(c, SET.draft) : renderCamRead(c, p2);
    }
  });
  /* 添加：两段确认（0.9.64）。这个按钮是全宽的，滚动列表时很容易蹭到，
     而以前误触一下就直接弹出新摄像头编辑页（还顺手往 CAMERAS 插了一条）。
     现在第一点只进入待确认态（按钮变色 + toast，3.2 秒自动取消），
     与「删除这个摄像头」的两段确认同一套语言。 */
  h += '<button class="btnline' + (SET.addArmed ? " armed" : "") + '" data-addcam="1" style="margin-top:3px">'
    + (SET.addArmed ? "再点一次确认添加" : "＋ 添加摄像头") + '</button>';
  h += '</div>';
  return h;
}

/* ---------------- 每路：只读摘要 ---------------- */
function renderCamRead(c, p){
  const pr = p.profile === "night" ? p.night : p.day;
  const mb = estMbps(pr, c.res, p.profile === "night");
  const modeTx = p.mode === "fast" ? "快速 · 流复制" : "精细 · 重编码";
  const keep = state.segs.reduce((a,s)=>a+(s.e-s.s),0)/86400;
  const gb = mb * 0.44 * 24 * Math.max(keep, 0.01);
  return '<div class="ccard">'
    + '<div class="tag"><i></i>仅此摄像头的参数<span class="ro-flag">只读 · 点「修改」可调整</span></div>'
    + '<div class="sub">'
      + '<h4>码流地址</h4>'
      + roRow("主码流 · 录像", p.main ? esc(p.main)
          : (c.hasUrl ? '<span class="dim">已保存在 NAS · 本机不显示明文</span>'
                      : '<span style="color:var(--warn)">未填写</span>'),
          p.main ? "url" : "")
      + roRow("子码流 · 预览", p.sub ? esc(p.sub)
          : (c.hasSub ? '<span class="dim">已保存在 NAS · 本机不显示明文</span>'
                      : '<span class="dim">未填 · 预览用主码流</span>'), p.sub ? "url" : "")
    + '</div>'
    + '<div class="sub">'
      + '<h4>保留周期</h4>'
      + roRow("全时录像", p.fullDays + " 天")
      + roRow("该路状态", p.enabled ? "启用" : "已停用", p.enabled ? "" : "dim")
    + '</div>'
    + '<div class="sub">'
      + '<h4>事件侦测 <em>' + cmLabel(cmOf(p)) + '</em></h4>'
      + roRow("事件侦测", cmOf(p) === "follow" ? "跟随全局" : cmLabel(cmOf(p))
              + (cmOf(p) !== "off" ? " · 判定照常" : " · 只录全时"))
      + (cmOf(p) === "materialize"
        ? roRow("物化切片", '<span style="color:var(--warn)">旧配置 · 建议改「开启」</span>')
        : (cmOf(p) === "virtual"
            ? roRow("归档浓缩片段", (LK.archDays || 7) + " 天（通用设置）")
            : ""))
    + '</div>'
    + '<div class="sub">'
      + '<h4>判读条件 <em>' + judgeLine(p) + '</em></h4>'
      + roRow("判读引擎", ENGINE_NAME[judgeOf(p).engine] + (judgeOf(p).engine === "auto" ? " · 优先像素差" : ""))
      + roRow("画面类型", SCENE_NAME[judgeOf(p).scene])
      + roRow("灵敏度", SENS_NAME[judgeOf(p).sens])
      + roRow("像素差门槛", judgeOf(p).pixelDelta + " / 255")
      + roRow("最短事件 / 静默合并", judgeOf(p).minEvent + " 秒 / " + judgeOf(p).mergeGap + " 秒")
      + roRow("抽样帧率", judgeOf(p).fps + " 帧/秒 · 模糊 " + judgeOf(p).blur)
    + '</div>'
    + '<div class="sub">'
      + '<h4>输出压缩 <em>约 ' + mb.toFixed(1) + ' Mbps · ' + gb.toFixed(1) + ' GB/天</em></h4>'
      + roRow("方式", modeTx)
      + roRow("画质档", (p.profile === "night" ? "夜间" : "白天") + " · CRF " + pr.crf + " · " + pr.fps + "fps · " + pr.scale + "%")
      + roRow("去噪", DN_NAMES[pr.dn])
    + '</div>'
    + '<div class="sub">'
      + '<h4>画面区域 <em>' + maskCount(p.masks) + ' 格忽略 · '
        + normMasks(p.masks).include.length + ' 格只统计</em></h4>'
      + '<div class="maskstage locked"><img class="mimg" id="maskImg" alt=""><canvas id="maskCv"></canvas></div>'
      + maskCountLine(p.masks)
    + '</div>'
    + '<div class="editbar"><button class="no" data-closecam="1">收起</button>'
      + '<button class="ok" data-editcam="1">修改</button></div>'
  + '</div>';
}

function roRow(k, v, cls){
  return '<div class="ro"><span class="k">' + k + '</span><span class="v ' + (cls||"") + '">' + v + '</span></div>';
}
