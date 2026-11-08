/* ================= pages/settings_editor.js =================
   设置页的编辑态：四段手风琴、每路参数表单、草稿读写（draftGet/draftSet）、
   遮罩画布与渲染入口、添加/删除摄像头、保存与重置。
   判断标准：**用户能按下去、能改值的那半张设置页**放这里。它读的是
   settings_cards.js 画出来的容器，写的是 draft，最后一并推给后端。
   为什么不跟卡片渲染合一份：这半边有"折叠到哪一段、草稿脏不脏、遮罩画到哪
   一格"三类状态，全是某一次编辑会话的临时状态；跟纯渲染摆在一起，下一个人
   会以为刷新页面就该把它们恢复出来。
   ============================================================== */

/* ---------------- 每路：编辑态（四段手风琴） ----------------
   一次只展开一段，其余收起。折叠状态标题右侧仍然显示该段当前值 ——
   收起不等于失明。这样一屏只面对一类参数，滑块不会紧挨着开关。 */
function camAcc(id, idx, title, summary, body, open){
  return '<div class="part' + (open ? " open" : "") + '">'
    + '<div class="ph" data-acc="' + id + '">'
      + '<span class="idx">' + idx + '</span><b>' + title + '</b>'
      + '<span class="st">' + summary + '</span>'
      + '<span class="chev">' + (open ? "收起 ⌃" : "展开 ⌄") + '</span>'
    + '</div>'
    + (open ? '<div class="accbody">' + body + '</div>' : '')
  + '</div>';
}

function judgeDrift(p){
  const j = judgeOf(p), d = JUDGE_DEFAULTS;
  return ["engine","scene","sens","pixelDelta","minEvent","blur","fps","probeSec","mergeGap"]
    .filter(k=>String(j[k]) !== String(d[k])).length;
}
function judgeAdvLine(p){
  const j = judgeOf(p);
  return "引擎 " + ENGINE_NAME[j.engine] + " · 画面 " + SCENE_NAME[j.scene]
    + " · 门槛 " + j.pixelDelta + " · 最短 " + j.minEvent + " 秒"
    + " · 模糊 " + (j.blur ? j.blur : "关") + " · 抽样 " + j.fps + " 帧/秒"
    + " · 采样 " + j.probeSec + " 秒 · 合并 " + j.mergeGap + " 秒";
}
function qualLine(p){
  const isN = p.profile === "night", pr = isN ? p.night : p.day;
  return (p.mode === "fast" ? "流复制" : "重编码") + " · " + (isN ? "夜间" : "白天")
    + " · CRF " + pr.crf + " · " + pr.fps + "fps · " + pr.scale + "% · 降噪 " + DN_NAMES[pr.dn];
}

/* 切分方式的显示名。eff=true 时把 follow 展开成通用值（读态/摘要用） */
function clipModeLabel(m, eff){
  if(eff && (m || "follow") === "follow") return "跟随通用 · " + (LK.clipStyle === "split" ? "活动切片" : "整段拼接");
  return m === "split" ? "活动切片" : m === "join" ? "整段拼接" : "跟随通用";
}

/* 压缩方案：每路覆盖（p.condenseMode）优先，follow/缺省回落全局 LK.condenseMode
   取值沿用后端语义：off=只录全时；virtual=只写活动台账、全时过期后裁归档浓缩；
   materialize=物化切片（缩时产物已下线，仅用于兼容旧配置，UI 不再提供入口）。 */
function cmOf(p){
  const m = (p && p.condenseMode) || "follow";
  return m === "follow" ? (LK.condenseMode || "virtual") : m;
}
function cmLabel(m){
  return m === "off" ? "关闭" : m === "materialize" ? "物化切片（旧配置）"
       : m === "virtual" ? "仅台账 + 归档" : "跟随全局";
}
/* 事件侦测→virtual/off 映射到服务端 condense_mode；浓缩片段(clipOut)为配置项，UI 不暴露，改后会同步 */
function syncCondenseMode(){
  LK.condenseMode = !LK.ledger ? "off" : (LK.clipOut ? "materialize" : "virtual");
}
function condenseHint(){
  if(LK.condenseMode === "off")
    return "关闭：只录全时、不判定。回放页时间轴没有活动标记，也不归档。";
  if(LK.condenseMode === "materialize")
    return "物化切片（旧配置）：历史上会额外产出 _clip 片段，该产物已下线；若仍处于此模式，全时过期后不会自动裁归档，建议改成「仅台账 + 归档」。";
  return "事件侦测开启：判定只记「活动台账」（每路每天几 KB，可永久保留）；回放在回放页时间轴上点标记按需播放，全时过期后自动裁归档浓缩。零常驻编码，存储只花在全时录像与归档上。";
}
function silentLabel(){
  return PPLAY.silent === "skip" ? "跳过" : PPLAY.silent + "× 快进";
}

function renderCamEdit(c, p){
  const isNight = p.profile === "night";
  const pr = isNight ? p.night : p.day;
  const mb = estMbps(pr, c.res, isNight);
  const PFX = isNight ? "night" : "day";
  const j = judgeOf(p);
  const openAcc = SET.acc || "base";
  const speed = p.clipSpeed || 1;
  const cm = cmOf(p);
  const on = cm !== "off";

  /* ---- ① 名称与码流 ---- */
  const bodyBase =
      '<div class="fld2" style="margin-top:8px"><label>名称</label>'
      + '<input type="text" value="' + esc(c.name) + '" data-camname="1"></div>'
    + '<div class="fld2"><label>主码流 RTSP · 录像用（必填）</label>'
      + '<input type="text" class="mono" value="' + esc(p.main) + '" data-k="main" placeholder="'
      + (c.hasUrl && !p.main
          ? "已保存在 NAS（本机不显示明文）· 留空 = 不修改"
          : "rtsp://192.0.2.10:554/stream1") + '"></div>'
    /* 「留空 = 不修改」与「清掉地址」必须是两个动作。
       0.9.120 起明文不再留在本机（localStorage 里没有地址），所以输入框是空的
       这件事本身只代表"本机不知道"，不能推断成"要删掉"；真要删就点这个按钮，
       由它打 url_clear 标记。 */
    + (c.hasUrl && !p.main
        ? '<div class="hint" style="margin:-4px 0 10px;font-size:10.5px">'
          + '<button class="minibtn" data-nuke="main">清除 NAS 上已保存的主码流地址</button>'
          + '<span style="margin-left:8px">换机位才用 · 清空后这一路会停止录像</span></div>'
        : '')
    + '<div class="fld2"><label>子码流 RTSP · 预览用（可空，留空则用主码流）</label>'
      + '<input type="text" class="mono" value="' + esc(p.sub) + '" data-k="sub" placeholder="'
      + (c.hasSub && !p.sub
          ? "已保存在 NAS（本机不显示明文）· 留空 = 不修改"
          : "rtsp://192.0.2.10:554/stream2") + '"></div>'
    + (c.hasSub && !p.sub
        ? '<div class="hint" style="margin:-4px 0 10px;font-size:10.5px">'
          + '<button class="minibtn" data-nuke="sub">清除 NAS 上已保存的子码流地址</button>'
          + '<span style="margin-left:8px">清空后预览改用主码流</span></div>'
        : '')
    + '<div class="hint" style="margin:6px 0 10px;font-size:10.5px">需要认证就把用户名密码写进地址：'
      + '<code style="font-size:10px">rtsp://user:pass@ip:554/...</code></div>'
    + '<div class="swrow"><div><span class="n">该摄像头启用</span>'
      + '<span class="h">关闭后不再录像、也不出现在直播页，配置保留</span></div>'
      + '<div class="sw ' + (p.enabled ? "on" : "") + '" data-en="1"></div></div>';

  /* ---- ② 保留与分片 ---- */
  const bodyKeep =
      '<div class="srow" style="padding-top:6px">'
      + '<div class="top"><span class="n">全时录像保留</span><span class="v" data-out="fullDays">' + p.fullDays + ' 天</span></div>'
      + '<div class="stepper" style="margin-top:8px"><button data-ret="fullDays" data-d="-1">−</button>'
        + '<span class="val">' + p.fullDays + ' 天</span><button data-ret="fullDays" data-d="1">+</button></div>'
      + '<div class="h">最前面 ' + p.fullDays + ' 天是<b>全时原画</b>（一路约 20~30 GB/天），这也是全局「归档回溯」的前段全时天数；更早的录像以<b>归档浓缩片段</b>保留（总天数见通用设置的「归档保留」），两者不重复占用空间。</div>'
    + '</div>'
    + '<div class="srow">'
      + '<div class="top"><span class="n">录制分片长度</span>'
        + '<span class="v">' + (p.segSec ? segLabel(p.segSec) : "跟随通用 " + segLabel(LK.segSec)) + '</span></div>'
      + '<div class="chips" style="margin-top:7px">'
        + [0,60,120,300,600,900].map(v=>
            '<button class="chip ' + ((p.segSec||0)===v?"on":"") + '" data-camseg="' + v + '">'
            + (v ? segLabel(v) : "跟随") + '</button>').join("")
      + '</div>'
      + '<div class="h">分片长度写进 ffmpeg 命令行，改了会<b>优雅重启这一路录制</b>（当前分片先写完再切），不会丢数据。</div>'
    + '</div>';

  /* ---- ③ 事件侦测 ---- */
  const fullDayGB = fullDayMbps(c) * 86400 / 8 / 1024;
  const clipDayGB = fullDayGB * 0.07 / speed;
  const segsToday = (state.activity && state.activity.length) ? state.activity.length : null;
  const drift = judgeDrift(p);
  const bodyCond =
      '<div class="srow" style="padding-top:6px">'
      + '<div class="top"><span class="n">事件侦测</span>'
        + '<span class="v">' + cmLabel(p.condenseMode || "follow") + '</span></div>'
      + '<div class="chips">'
        + [["follow","跟随全局"],["virtual","开启"],["off","关闭"]].map(([v,l])=>
            '<button class="chip ' + ((p.condenseMode||"follow")===v?"on":"") + '" data-cmcam="' + v + '">' + l + '</button>').join("")
      + '</div>'
      + '<div class="h">跟随全局 = 用「通用设置 · 事件侦测与归档」。开启 = 每路单独做事件侦测、写活动台账、全时过期后裁归档浓缩；关闭 = 只录全时不判定。每路可单独覆盖全局。</div>'
    + '</div>'
    + (cm === "materialize" ? (
        '<div class="hint" style="margin:6px 0 10px;color:var(--warn)">'
        + '这一路沿用旧的<b>物化切片</b>配置：历史上会产出 <code style="font-size:10.5px">_clip</code> 片段，'
        + '该产物已下线；此模式下全时过期后<b>不会</b>自动裁归档浓缩。建议把上面的「事件侦测」改成<b>开启</b>（仅台账 + 归档）。</div>'
      ) : (cm === "virtual"
        ? '<div class="hint" style="margin:6px 0 10px">这一路用<b>仅台账 + 归档</b>：判定照常跑、只写活动台账（回放页时间轴可看）；全时录像过期后自动裁出归档浓缩片段。</div>'
        : '<div class="hint" style="margin:6px 0 10px">这一路事件侦测已<b>关闭</b>：只录全时、不判定，回放页时间轴没有活动标记。已有归档片段不会被删除。</div>'))
    /* 输出画质 / 切分方式 / 输出倍速 / 浓缩片段保留 全部属于「物化切片」参数，
       随缩时片段一起下线：全时录像走流复制、归档浓缩由归档任务统一裁剪，
       这些重编码参数对它们都不生效，留着只会让人以为改了有用。 */
    ;

  /* ---- ④ 检测忽略区 ---- */
  const maskCnt = (p.masks && p.masks.ignore) ? p.masks.ignore.length : 0;
  /* ---- ④ 检测（0.9.60）：每路 AI 开关 + 类别，忽略区原样复用 ----
     每路 detect：follow=跟随全局总闸与类别；on/off 显式覆盖。
     detect_classes 三键完整状态，初值拷全局默认，用户改哪个推哪个。 */
  const camDet = p.detect || "follow";
  const camCls = p.detectCls || { person:true, two_wheeler:true, vehicle:true };
  const camDetOn = camDet === "on" || (camDet === "follow" && LK.detEnable);
  const detClsRow = (k, label) =>
      '<div class="swrow" style="border:none;padding:5px 0 0"><span>' + label + '</span>'
      + '<div class="sw ' + (camCls[k]?"on":"") + '" data-camdetcls="' + k + '"></div></div>';
  const bodyDetect =
      '<div class="srow" style="padding-top:6px">'
      + '<div class="top"><span class="n">本路 AI 识别</span>'
        + '<span class="v">' + (camDet === "follow" ? "跟随全局（" + (LK.detEnable ? "开" : "关") + "）"
          : camDet === "on" ? "开" : "关") + '</span></div>'
      + '<div class="chips">'
        + [["follow","跟随全局"],["on","开"],["off","关"]].map(([v,l])=>
            '<button class="chip ' + (camDet===v?"on":"") + '" data-camdet="' + v + '">' + l + '</button>').join("")
      + '</div>'
      + '<div class="h">跟随全局 = 用「通用设置 · 检测」里 AI 识别的总闸与类别。全局关闭时本段整体不生效。</div>'
    + '</div>'
    + (camDetOn ? (
        '<div class="srow">'
      + '<div class="n">本路检测类别</div>'
      + detClsRow("person", "人员")
      + detClsRow("two_wheeler", "两轮车")
      + detClsRow("vehicle", "车辆")
      + '<div class="h" style="margin-top:6px">只为需要的内容花算力：院子可配「人员 + 车辆」、客厅只留「人员」。全关等于不检测。</div>'
    + '</div>'
      ) : (camDet === "off" ? '<div class="hint" style="margin:6px 0 10px">本路已<b>强制关闭</b> AI 识别：不抽帧、不推理、不通知，下面的移动侦测（像素差）不受影响。</div>' : ""))
    /* 移动侦测（像素差）组：0.9.61 从「事件侦测」段搬来，与 AI 识别并列——
       用户面对的是一个「检测」段，不该猜到帧差参数藏在压缩方案段落里 */
    + '<div class="srow">'
      + '<div class="top"><span class="n">移动侦测（像素差）</span>'
        + '<span class="v">' + (on ? SENS_NAME[j.sens] + " · " + ENGINE_NAME[j.engine] : "未启用 · 事件侦测已关闭") + '</span></div>'
      + '<div class="h" style="margin-top:6px;line-height:1.55">不需要 AI：ffmpeg 把画面缩到 320×180 后逐像素比对，'
        + '变化超过门槛的时段记为活动。它随本路事件侦测运行（关闭 = 不判定），与上面的 AI 识别各自独立。</div>'
    + '</div>'
    + '<div class="' + (on ? "" : "dim") + '">'
      + chipRow("灵敏度", "judge.sens", j.sens,
          [["low","低"],["mid","标准（推荐）"],["high","高"]], "sens",
          "阈值倍率 低 " + SENS_MULT.low + "× · 标准 " + SENS_MULT.mid + "× · 高 " + SENS_MULT.high
            + "×。树影、雨雪多选「低」；要抓远处小目标选「高」。")
      + '<div class="subfold" data-adv="1"><b>进阶判读参数</b>'
        + (drift ? '<span class="st" style="color:var(--warn)">已调 ' + drift + ' 项</span>' : '')
        + '<span class="st" style="margin-left:auto;' + (drift ? "color:var(--faint)" : "") + '">'
          + (SET.adv ? "" : esc(judgeAdvLine(p))) + '</span>'
        + '<span class="chev">' + (SET.adv ? "收起 ⌃" : "展开 ⌄") + '</span></div>'
      + (SET.adv ? (
          chipRow("判读引擎", "judge.engine", j.engine,
              [["auto","自动"],["pixel","像素差"],["bytes","码流曲线"]], "engine",
              "像素差逐帧比对画面：静止时段真正被剔除、事件区间更准，代价是要解码（先用码流曲线粗筛、只解候选区间）。码流曲线不解码最省 CPU，但会被码率策略骗。")
          + chipRow("画面类型", "judge.scene", j.scene,
              [["auto","自动识别"],["color","彩色"],["mono","黑白红外"]], "scene",
              "红外机位噪点更多，选黑白/红外会额外放宽阈值，避免满屏颗粒被当成连续活动。")
          + rangeRow("像素差门槛", "judge.pixelDelta", j.pixelDelta, 1, 80, 1,
              "单像素亮度变化超过这个值才算「变了」，24 ≈ 亮度差 9%。噪点大调高，小目标总漏调低。")
          + rangeRow("最短事件", "judge.minEvent", j.minEvent, 0.5, 60, 0.5,
              "短于这个时长的事件视为瞬态（飞虫、闪光）丢弃，不会生成一闪而过的碎片。")
          + rangeRow("模糊半径", "judge.blur", j.blur, 0, 3, 1,
              "判定前先做空域模糊，压掉压缩颗粒。0 = 关闭；红外机位建议 1~2。")
          + rangeRow("分析抽样帧", "judge.fps", j.fps, 0.2, 10, 0.2,
              "每秒比对多少帧。2 帧/秒已能抓住人车；调高更灵敏但更耗 CPU。")
          + rangeRow("日夜采样间隔", "judge.probeSec", j.probeSec, 1, 30, 1,
              "自动识别彩色/红外时的采样间隔（秒），滤片切换瞬间不会被当成事件。")
          + rangeRow("静默合并", "judge.mergeGap", j.mergeGap, 0, 120, 1,
              "两段活动之间的安静时间短于此值就拼成一段，避免一个事件被切成很多碎片。")
          + '<div class="sumline" style="margin-top:6px">默认值：门槛 ' + JUDGE_DEFAULTS.pixelDelta
            + ' · 最短 ' + JUDGE_DEFAULTS.minEvent + ' 秒 · 模糊 ' + JUDGE_DEFAULTS.blur
            + ' · 抽样 ' + JUDGE_DEFAULTS.fps + ' 帧/秒 · 合并 ' + JUDGE_DEFAULTS.mergeGap + ' 秒</div>'
        ) : '')
    + '</div>'
    + (on ? '' : '<div class="hint" style="margin:6px 0 0">本路事件侦测已关闭：以上侦测参数暂不生效，开启后恢复；AI 识别不受影响。</div>')
    + '<div class="mtip" style="margin:10px 0 8px"><b style="color:var(--warn)">检测忽略区</b>：框中心落进忽略格的检出结果'
      + '<b>直接丢弃</b>（后端过滤、不改画面），AI 识别与移动侦测<b>共用</b>这块忽略区。'
      + '<b>按住拖动多选，点一下已选的格子取消。</b></div>'
    + '<div class="maskstage"><img class="mimg" id="maskImg-ig" alt=""><canvas id="maskCv-ig"></canvas></div>'
    + '<div class="mlist" id="mlist"></div>'
    + '<div class="sumline" style="margin-top:8px">已选 <b>' + maskCnt + '</b> / 144 格。'
      + '区域越多、越容易漏掉真实事件，只框真正会误报的地方。</div>';

  return '<div class="ccard">'
    + '<div class="tag"><i></i>仅此摄像头的参数'
      + '<span class="ro-flag" style="color:var(--primary)">编辑中 · 未保存</span></div>'
    + camAcc("base", 1, "名称与码流",
        (p.main || c.hasUrl)
          ? ("已填主码流" + ((p.sub || c.hasSub) ? " · 有子码流" : "")
             + (p.main ? "" : " · 明文存于 NAS"))
          : '<span style="color:var(--warn)">未填主码流</span>',
        bodyBase, openAcc === "base")
    + camAcc("keep", 2, "保留与分片",
        "全时 " + p.fullDays + " 天 · " + segLabel(p.segSec || LK.segSec),
        bodyKeep, openAcc === "keep")
    + camAcc("condense", 3, "事件侦测",
        (on ? ("事件侦测" + (cmOf(p) === "materialize" ? " · 含浓缩片段" : " · 仅台账"))
    : '<span style="color:var(--faint)">已关闭 · 只录全时</span>'),
        bodyCond, openAcc === "condense")
    + camAcc("mask", 4, "检测",
        (!LK.detEnable ? "AI 未开启"
          : camDet === "off" ? "AI 识别已关"
          : (camCls.person||camCls.two_wheeler||camCls.vehicle) ? "AI 开 · " +
            [camCls.person?"人":"", camCls.two_wheeler?"两轮":"", camCls.vehicle?"车":""].filter(Boolean).join("+")
          : "AI 开 · 类别全关")
        + " · 侦测 " + (on ? SENS_NAME[j.sens] : "随事件侦测关闭")
        + " · 忽略区 " + maskCnt + " 格",
        bodyDetect, openAcc === "mask")
    + '<div class="dz"><button class="delbtn' + (SET.armed === c.id ? " armed" : "") + '" data-delcam="' + c.id + '">'
      + (SET.armed === c.id ? "再点一次确认删除" : "删除这个摄像头") + '</button>'
      + '<div class="sumline" style="margin-top:7px;text-align:center">已录的文件不会被删除</div></div>'
    + '<div class="editbar">'
      + '<span class="dirty">' + (dirtyCount() ? ("● 已改 " + dirtyCount() + " 项未保存") : "无改动") + '</span>'
      + '<button class="no" data-cancel="1">取消</button>'
      + '<button class="rst" data-reset="1">恢复默认</button>'
      + '<button class="ok" data-save="1">保存</button>'
    + '</div>'
  + '</div>';
}

/* 草稿与已保存参数的差异项数：底部动作栏常驻显示，避免"改完就忘" */
function dirtyCount(){
  const saved = camParams[state.camId];
  if(!saved || !SET.draft) return 0;
  let n = 0;
  const walk = (a, b)=>{
    Object.keys(b || {}).forEach(k=>{
      const va = a ? a[k] : undefined, vb = b[k];
      if(vb && typeof vb === "object" && !Array.isArray(vb)) walk(va || {}, vb);
      else if(JSON.stringify(va) !== JSON.stringify(vb)) n++;
    });
  };
  walk(saved, SET.draft);
  return n;
}

/* 一组 chips（枚举型参数）。opts = [[值, 显示名], ...]，attr 是 data-* 的名字 */
function chipRow(label, key, cur, opts, attr, hint){
  const hit = opts.filter(o=>o[0] === cur)[0] || opts[0];
  return '<div class="srow">'
    + '<div class="top"><span class="n">' + label + '</span>'
      + '<span class="v" data-out="' + key + '">' + hit[1] + '</span></div>'
    + '<div class="chips">'
      + opts.map(o=>'<button class="chip ' + (o[0] === cur ? "on" : "")
          + '" data-' + attr + '="' + o[0] + '">' + o[1] + '</button>').join("")
    + '</div>'
    + (hint ? '<div class="h">' + hint + '</div>' : '')
  + '</div>';
}

function rangeRow(label, key, val, min, max, step, hint){
  const tail = key.split(".").pop();
  return '<div class="srow">'
    + '<div class="top"><span class="n">' + label + '</span><span class="v" data-out="' + key + '">' + fmtVal(tail, val) + '</span></div>'
    + '<input type="range" min="' + min + '" max="' + max + '" step="' + step + '" value="' + val + '" data-k="' + key + '">'
    + (hint ? '<div class="h">' + hint + '</div>' : '')
  + '</div>';
}

function fmtVal(k, v){
  if(k==="minEvent" || k==="mergeGap" || k==="probeSec" || k==="preBuf" || k==="postBuf")
    return (+v).toFixed(+v % 1 ? 1 : 0) + " 秒";
  if(k==="pixelDelta") return v + " / 255";
  if(k==="blur") return v === 0 ? "关闭" : v + " 像素";
  if(k==="fps") return (+v).toFixed(1) + " 帧/秒";
  if(k==="noiseMult") return (+v).toFixed(1) + "×";
  if(k==="scale") return v + "%";
  if(k==="dn") return DN_NAMES[v] || v;
  if(k==="fullDays" || k==="clipDays") return v + " 天";
  return v;
}

/* ---------------- 草稿读写 ---------------- */
function draftGet(path){
  const ks = path.split(".");
  let o = SET.draft;
  for(let i=0;i<ks.length;i++){ o = o[ks[i]]; if(o == null) return null; }
  return o;
}
function draftSet(path, v){
  const ks = path.split(".");
  let o = SET.draft;
  for(let i=0;i<ks.length-1;i++){
    if(!o[ks[i]]) o[ks[i]] = {};
    o = o[ks[i]];
  }
  o[ks[ks.length-1]] = v;
}
function syncOuts(){
  const body = document.getElementById("camBody");
  if(!body || !SET.draft) return;
  body.querySelectorAll("[data-out]").forEach(el=>{
    const v = draftGet(el.dataset.out);
    if(v == null) return;
    el.textContent = fmtVal(el.dataset.out.split(".").pop(), v);
  });
  /* 码率 / 体积预估跟着画质参数一起动 */
  const c = curCam();
  if(c){
    const p = SET.draft;
    const isNight = p.profile === "night";
    const pr = isNight ? p.night : p.day;
    const mb = estMbps(pr, c.res, isNight);
    const keep = state.segs.reduce((a,s)=>a+(s.e-s.s),0)/86400;
    const gb = mb * 0.44 * 24 * Math.max(keep, 0.01);
    const sub = body.querySelector("#outSub");
    if(sub) sub.textContent = "约 " + mb.toFixed(1) + " Mbps · " + gb.toFixed(1) + " GB/天";
  }
}

/* ---------------- 遮罩画布 ---------------- */
/* 画面区域：16×9 方块预选。两个画布（忽略/遮蔽）各自独立，
   只读摘要是第三张（两者叠加展示、不可编辑）。 */
let gdrag = null;

function maskArr(kind){
  if(SET.editing && SET.draft){
    const m = SET.draft.masks;
    return m[kind] || (m[kind] = []);
  }
  const p = camParams[state.camId];
  if(!p) return [];
  return p.masks[kind] || (p.masks[kind] = []);
}
function maskCount(m){
  return normMasks(m).ignore.length;
}
function maskCountLine(m){
  m = normMasks(m);
  if(!m.ignore.length)
    return '<span style="font-size:11.5px;color:var(--faint2)">还没有忽略区</span>';
  return '<div class="mlist"><span class="mitem"><i style="background:#e0a33a"></i>忽略区 '
    + m.ignore.length + ' 格</span></div>';
}

function drawMaskScene(){
  ["maskCv", "maskCv-ig"].forEach(drawGridCv);
}

function drawGridCv(id){
  const cv = document.getElementById(id);
  if(!cv) return;
  const rr = cv.getBoundingClientRect();
  if(rr.width < 8) return;                  /* 页面还没显示，等切过来再画 */
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = rr.width, h = rr.height || w*9/16;
  const cw = Math.round(w*dpr), ch = Math.round(h*dpr);
  if(cv.width !== cw || cv.height !== ch){ cv.width = cw; cv.height = ch; }
  const c = cv.getContext("2d");
  c.setTransform(dpr,0,0,dpr,0,0);
  const kind = id === "maskCv" ? "both" : "ignore";

  /* 底图：有快照就让画布透明叠在上面；没有就画示意场景占位 */
  const img = document.getElementById(id.replace("Cv", "Img"));
  const hasPhoto = !!(img && img.classList && img.classList.contains("on"));
  if(hasPhoto){
    c.clearRect(0,0,w,h);
  }else{
    c.fillStyle = "#20242a"; c.fillRect(0,0,w,h);
    const hz = h*0.34;
    c.fillStyle = "#292e36"; c.fillRect(0,0,w,hz);
    c.fillStyle = "#333941"; c.fillRect(0,hz,w,h-hz);
    c.fillStyle = "#181b20"; c.fillRect(w*0.40,h*0.06,w*0.15,hz-h*0.06);
    c.fillStyle = "#242930"; c.fillRect(w*0.80,h*0.10,w*0.055,h*0.62);
  }

  /* 选中的格子 */
  const cellW = w/GRID_COLS, cellH = h/GRID_ROWS;
  const fillCells = (kindSel, fill, stroke)=>{
    maskArr(kindSel).forEach(idx=>{
      const col = idx % GRID_COLS, row = (idx - col) / GRID_COLS;
      c.fillStyle = fill;
      c.fillRect(col*cellW, row*cellH, cellW, cellH);
      if(stroke){ c.strokeStyle = stroke; c.lineWidth = 1; c.strokeRect(col*cellW+.5, row*cellH+.5, cellW-1, cellH-1); }
    });
  };
  fillCells("ignore", "rgba(240,180,40,.42)", "rgba(240,180,40,.9)");

  /* 拖动中的预览 */
  if(gdrag && gdrag.kind === kind){
    const cells = rectCells(gdrag.c0, gdrag.r0, gdrag.c1, gdrag.r1);
    c.fillStyle = "rgba(240,180,40,.34)";
    cells.forEach(idx=>{
      const col = idx % GRID_COLS, row = (idx - col) / GRID_COLS;
      c.fillRect(col*cellW, row*cellH, cellW, cellH);
    });
  }

  /* 网格线盖在最上面 */
  c.strokeStyle = "rgba(255,255,255,.20)"; c.lineWidth = 1;
  for(let i=1; i<GRID_COLS; i++){
    c.beginPath(); c.moveTo(i*cellW, 0); c.lineTo(i*cellW, h); c.stroke();
  }
  for(let j=1; j<GRID_ROWS; j++){
    c.beginPath(); c.moveTo(0, j*cellH); c.lineTo(w, j*cellH); c.stroke();
  }
  if(!hasPhoto){   /* 示意底图上加一条地平线，让格子有参照 */
    c.strokeStyle = "rgba(255,255,255,.08)";
    c.beginPath(); c.moveTo(0, h*0.34); c.lineTo(w, h*0.34); c.stroke();
  }
}

function rectCells(c0, r0, c1, r1){
  const cs = [Math.min(c0,c1), Math.max(c0,c1)];
  const rs = [Math.min(r0,r1), Math.max(r0,r1)];
  const out = [];
  for(let r=rs[0]; r<=rs[1]; r++) for(let c=cs[0]; c<=cs[1]; c++) out.push(r*GRID_COLS+c);
  return out;
}
function cellAt(cv, e){
  const r = cv.getBoundingClientRect();
  const x = Math.max(0, Math.min(0.9999, (e.clientX - r.left) / r.width));
  const y = Math.max(0, Math.min(0.9999, (e.clientY - r.top) / r.height));
  return { c: Math.floor(x*GRID_COLS), r: Math.floor(y*GRID_ROWS) };
}

function renderMasks(){
  const box = document.getElementById("mlist");
  const ig = maskArr("ignore");
  if(box){
    box.innerHTML = ig.length
      ? '<span class="mitem"><i style="background:#e0a33a"></i>忽略区 ' + ig.length + ' 格'
        + '<span class="x" data-clrmask="ignore">清空</span></span>'
      : '<span class="mitem"><i style="background:#e0a33a"></i>忽略区 0 格</span>';
    box.querySelectorAll("[data-clrmask]").forEach(el=>{
      el.onclick = ()=>{ maskArr(el.dataset.clrmask).length = 0; renderMasks(); drawMaskScene(); };
    });
  }
  const igN = document.getElementById("igCnt");
  if(igN) igN.textContent = "已选 " + ig.length + " 格";
}

/* 关键：每次重绘设置页都会换掉 canvas 元素，所以绑定必须跟着渲染走，
   不能只在启动时绑一次 —— 这正是之前"画不出任何区域"的原因。 */
function bindMaskCanvas(){
  [["maskCv-ig","ignore"]].forEach(([id, kind])=>{
    const cv = document.getElementById(id);
    if(!cv) return;

    /* 底图快照；接口没接好 404 时静默留在示意底图 */
    const img = document.getElementById(id.replace("Cv", "Img"));
    if(img && !img.dataset.hooked){
      img.dataset.hooked = "1";
      img.onload = ()=>{ img.classList.add("on"); drawMaskScene(); };
      img.onerror = ()=>{ img.classList.remove("on"); };
      img.src = "/api/live/snapshot/" + encodeURIComponent(state.camId) + ".jpg?t=" + Date.now();
    }

    cv.onpointerdown = e=>{
      if(!(SET.editing && SET.draft)) return;
      e.preventDefault();
      const cell = cellAt(cv, e);
      gdrag = { kind, cv, c0:cell.c, r0:cell.r, c1:cell.c, r1:cell.r, moved:false };
      try{ cv.setPointerCapture(e.pointerId); }catch(err){}
      drawMaskScene();
    };
    cv.onpointermove = e=>{
      if(!gdrag || gdrag.cv !== cv) return;
      e.preventDefault();
      const cell = cellAt(cv, e);
      if(cell.c !== gdrag.c1 || cell.r !== gdrag.r1){
        gdrag.c1 = cell.c; gdrag.r1 = cell.r; gdrag.moved = true;
      }
      drawMaskScene();
    };
    cv.onpointerup = ()=>{
      if(!gdrag || gdrag.cv !== cv) return;
      finishGridDrag();
    };
    cv.onpointercancel = ()=>{ gdrag = null; drawMaskScene(); };
  });
}

function finishGridDrag(){
  const { kind, c0, r0, c1, r1, moved } = gdrag;
  gdrag = null;
  const arr = maskArr(kind);
  if(!moved){
    /* 单击：点了已选的格子就取消，没选就选上 */
    const idx = r0*GRID_COLS + c0;
    const pos = arr.indexOf(idx);
    if(pos >= 0) arr.splice(pos, 1); else arr.push(idx);
  }else{
    let added = 0;
    rectCells(c0, r0, c1, r1).forEach(i=>{ if(arr.indexOf(i) < 0){ arr.push(i); added++; } });
    if(added) toast("toast2", "已选 " + arr.length + " 格 · 记得点「保存」");
  }
  renderMasks(); drawMaskScene();
}

/* ---------------- 编辑动作 ---------------- */
function openEditor(id){
  if(SET.editing && state.camId !== id) return;   /* 别把未保存的改动丢掉 */
  if(SET.editing && state.camId === id) return;
  state.camId = id;
  SET.open = id;
  SET.draft = copyObj(camParams[id]);
  SET.draft.masks = normMasks(SET.draft.masks);
  SET.editing = true;
  SET.adv = false;
  SET.outOpen = false;
  SET.acc = "base";        /* 手风琴从第一段开始：先确认这是哪台机器、码流对不对 */
  SET.armed = null;
  renderCamTabs();
  loadDay();
}
function closeEditor(){
  SET.editing = false; SET.draft = null; SET.open = null;
  renderCamTabs();
}
function saveDraft(){
  const id = state.camId;
  if(!id || !SET.draft) return;
  const dst = camParams[id];
  /* 地址清空意图只认显式动作（编辑页「清除地址」按钮打的标记）。
     以前是"本地曾有明文 + 保存时被清空"来推断，那条路在 0.9.120 断了：
     明文不再留在本机，输入框在任何刷新过的页面上都是空的 —— 若还按旧规则
     推断，每次保存任何一项设置都会把 NAS 上的地址删掉。 */
  dst.urlClear = !!SET.draft.urlClear;
  dst.subClear = !!SET.draft.subClear;
  Object.keys(SET.draft).forEach(k=>{ dst[k] = SET.draft[k]; });
  SET.editing = false; SET.draft = null; SET.open = id;
  renderCamTabs();
  syncCams();
  toast("toast2", "已保存 · 下一段录像按新参数执行");
}
function resetDraft(){
  const c = curCam();
  if(!c) return;
  const i = CAMERAS.indexOf(c);
  const d = defaultCamParams(c, i);
  d.main = SET.draft.main || c.main || "";
  d.sub  = SET.draft.sub  || c.sub  || "";
  SET.draft = d;
  renderCamTabs();
  toast("toast2", "已填回默认值 · 点「保存」才生效");
}
function addCamera(){
  let n = CAMERAS.length + 1;
  let idx = n;
  while(CAMERAS.some(c=>c.ch === "CH" + String(idx).padStart(2,"0"))) idx++;
  const id = "cam-" + Date.now().toString(36);
  const cam = { id, name:"摄像头 " + n, ch:"CH" + String(idx).padStart(2,"0"), res:"1080p", online:false,
                main:"", sub:"" };
  CAMERAS.push(cam);
  camParams[id] = defaultCamParams(cam, CAMERAS.length-1);
  state.camId = id;
  SET.open = id; SET.editing = true; SET.draft = copyObj(camParams[id]);
  SET.acc = "base"; SET.adv = false; SET.outOpen = false;
  renderCamTabs();
  syncCams();
  toast("toast2", "已添加 · 填入主码流 RTSP 后点保存");
}
function delCamera(id){
  const i = CAMERAS.findIndex(c=>c.id === id);
  if(i < 0) return;
  CAMERAS.splice(i,1);
  delete camParams[id];
  if(state.camId === id) state.camId = CAMERAS[0] ? CAMERAS[0].id : null;
  if(SET.open === id){ SET.open = null; SET.editing = false; SET.draft = null; }
  SET.armed = null;
  renderCamTabs();
  try{ loadDay(); }catch(e){}
  syncCams();
  toast("toast2", "已删除该摄像头 · 已录文件不会被删除");
}

/* ---------------- 渲染入口 ---------------- */
function renderEditMode(){
  const c = curCam();
  if(!c || !SET.draft){ SET.editing = false; return ""; }
  return '<div class="sechead"><b>' + c.ch + ' · ' + esc(c.name) + '</b><span class="ln"></span>'
    + '<button class="minibtn" data-cancel="1">取消</button></div>'
    + renderCamEdit(c, SET.draft);
}

/* 通用设置摘要（折叠后显示一行，省得每次都展开翻） */
function genSummary(){
  return (REC.enabled ? "录制已启用" : "录制已停用")
    + " · 目录 " + REC.dir
    + " · 即刻判定"
    + " · 分片 " + Math.round(LK.segSec/60) + " 分钟"
    + " · " + GS.conc + " 路"
    + " · 每天 " + REC.cleanupAt + " 清理";
}

function renderGenZone(){
  let h = '<div class="zone gen">'
    + '<div class="zonehead" data-gentoggle="1">'
    + '<span class="zi">⚙</span>'
    + '<div><b>通用设置</b><div class="zs">所有摄像头共用 · 录制 / 压制 / 外观</div></div>'
    + '<span class="chev">' + (SET.genOpen ? "收起 ⌃" : "展开 ⌄") + '</span></div>';
  if(SET.genOpen){
    h += cardRec() + cardLink() + cardDetect() + cardLook() + cardPlay()
      + '<div class="editbar"><button class="ok" data-gensave="1">保存通用设置</button></div>';
  }else{
    h += '<div class="gcard genfold"><div class="genline">' + genSummary() + '</div>'
      + '<button class="minibtn" data-gentoggle="1">修改</button></div>';
  }
  return h + '</div>';
}
