/* 浏览器端到端：真起服务 + 真开 Chrome，点一遍用户实际会点的路径。
   验证的是「页面功能能不能落地」，不只是"能不能渲染"：
     1. 页面无 JS 报错
     2. 设置页 → 编辑摄像头 → 画遮蔽区 → 保存 → 配置真的落到服务端
     3. 扫描按钮出真实数字
     4. 清理按钮：预览 → 二次确认 → 真删
     5. 诊断导出真的触发下载
     6. 回放页读到磁盘上的真实分片
*/
const { chromium } = require("playwright-core");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PY = "C:/Users/admin/.workbuddy/binaries/python/versions/3.13.12/python.exe";
const ROOT = path.resolve(__dirname, "..");
const SERVER = path.join(ROOT, "app", "server", "nvr_slim.py");
const UI = path.join(ROOT, "app", "ui");
const PORT = 18791;

let pass = 0;
const fails = [];
function check(name, cond, detail) {
  if (cond) { pass++; console.log("  ✅ " + name); }
  else { fails.push(name); console.log("  ❌ " + name + "  ← " + JSON.stringify(detail)); }
}

function d2(n) { return String(n).padStart(2, "0"); }

(async () => {
  const varDir = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-e2e-"));
  const rec = path.join(varDir, "rec");
  const now = new Date();
  const today = `${now.getFullYear()}-${d2(now.getMonth() + 1)}-${d2(now.getDate())}`;
  const oldDay = `${now.getFullYear()}-${d2(now.getMonth() + 1)}-${d2(now.getDate() - 8)}`;
  // 今天 3 个分片 + 8 天前 1 个（远超保留 4 天，必须被清理）
  for (const d of [today, oldDay]) {
    fs.mkdirSync(path.join(rec, "车库", d), { recursive: true });
  }
  for (const h of ["10-00-00", "10-05-00", "10-10-00"]) {
    fs.writeFileSync(path.join(rec, "车库", today, h + ".mp4"), Buffer.alloc(2048));
  }
  // 台账 fixture：判定引擎需要 ffprobe（本机没有），按服务端真实格式种一天窗口，
  // 让横线标注/活动时段块/活动筛选跳静止都有数据可断言。窗口 = 当天钟点秒。
  // 0.9.62 起另种两份 AI 台账（src = "ai:<分片名>"，与帧差 src 平行），窗口全部
  // 落进对应帧差窗口内部：并集事件数不变（仍是 4 个），但这些事件带上了 cls，
  // 时间轴才需要按类别上色 —— 这也正是真机上的样子（AI 与帧差对同一段录像各有结论）。
  // 0.9.63 起回放页车辆不区分车型：two_wheeler 计数并入车辆，图例/摘要/筛选同步。
  const W = (h, m, s, dur, cls) =>
      [h * 3600 + m * 60 + s, h * 3600 + m * 60 + s + dur].concat(cls ? [cls] : []);
  fs.mkdirSync(path.join(rec, "_ledger", "cam-01"), { recursive: true });
  fs.writeFileSync(path.join(rec, "_ledger", "cam-01", today + ".json"), JSON.stringify({
    v: 1, cam: "cam-01", date: today, updated_at: today + " 11:05:00",
    sources: { "10-00-00.mp4": { start: 10 * 3600, d: 0.35, windows: [
      W(10, 2, 15, 12), W(10, 7, 19, 277), W(10, 13, 30, 10),
    ] }, "11-00-00.mp4": { start: 11 * 3600, d: 0.4, windows: [
      W(11, 0, 56, 218),
    ] },
      "ai:10-00-00.mp4": { start: 10 * 3600, d: 0.4, windows: [
        W(10, 2, 18, 8, { person: 2 }),                        // 人 ×2 → 单人事件
        W(10, 13, 32, 6, { vehicle: 3, two_wheeler: 1 }),      // 车3+两轮1 → 并入车辆（单色紫）
      ] },
      "ai:11-00-00.mp4": { start: 11 * 3600, d: 0.4, windows: [
        W(11, 1, 0, 60, { person: 1, vehicle: 1 }),            // 人+车 → 双色事件
      ] } },
  }));
  fs.writeFileSync(path.join(rec, "车库", oldDay, "09-00-00.mp4"), Buffer.alloc(4096));
  // 缩时片段：真实时长要靠 ffprobe 读文件，这里造不出真 mp4，就验证「读不到
  // 时长时不会拿 segment_sec 冒充」（否则界面显示 2 小时 30 分，3 秒播完）。
  for (const h of ["10-00-00", "10-05-00", "10-10-00"]) {
    fs.mkdirSync(path.join(rec, "_clip", "车库", today), { recursive: true });
    fs.writeFileSync(path.join(rec, "_clip", "车库", today, h + ".mp4"), Buffer.alloc(1024));
  }
  fs.writeFileSync(path.join(varDir, "config.json"), JSON.stringify({
    record_dir: rec, record_enable: false, record_segment_sec: 300,
    /* masks 必须显式给空：前面用例保存过忽略区，不写死初始值的话
       断言"拖选后正好 4 格"会在残留数据上失败 */
    cameras: [{ id: "cam-01", name: "车库", url: "rtsp://u:p@127.0.0.1:1/s1",
                sub_url: "", enabled: true, full_days: 4, clip_days: 90,
                segment_sec: 0, masks: { ignore: [] } }]
  }));

  const srv = spawn(PY, [SERVER, "--port", String(PORT), "--var", varDir,
    "--app", varDir, "--ui", UI, "--host", "127.0.0.1"],
    { env: { ...process.env, NVR_SIMULATE_GPU: "qsv" }, stdio: "pipe" });
  let srvLog = "";
  srv.stdout.on("data", b => { srvLog += b.toString(); });
  srv.stderr.on("data", b => { srvLog += b.toString(); });

  // 等服务就绪
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 250));
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/api/health`);
      if (r.ok) break;
    } catch (e) {}
  }

  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  const ctx = await browser.newContext({ viewport: { width: 420, height: 900 } });
  // 干净环境：前端配置缓存在 localStorage（按 origin 隔离，上次跑的会留下来）。
  // CI / 复跑时它会盖掉服务端夹具，必须先清。
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(String(e)));
  page.on("console", m => { if (m.type() === "error") errs.push("console: " + m.text()); });
  // 不存在的 RTSP 会让快照接口一直 404，这是预期内的，不算错误
  const ignore = t => /snapshot|No resource|404|ERR_/.test(t);

  await page.goto(`http://127.0.0.1:${PORT}/index.html?page=cam`, { waitUntil: "load" });
  await page.waitForTimeout(1200);

  check("页面加载无 JS 报错", errs.filter(e => !ignore(e)).length === 0,
        errs.filter(e => !ignore(e)));

  // --- 服务端配置是否被前端读取（syncFromServer 以前是死路）---
  const cfgText = await page.evaluate(async () => {
    const r = await fetch("/api/config"); const d = await r.json(); return d;
  });
  check("GET /api/config 有数据", cfgText && cfgText.ok === true, cfgText && cfgText.ok);
  check("前端读到服务端录像目录",
        await page.evaluate(() => REC.dir) === rec, await page.evaluate(() => REC.dir));

  // --- 设置页：展开摄像头 → 编辑 → 画检测忽略区 → 保存（并验证分片长度）---
  await page.click(".camrow");
  await page.waitForTimeout(300);
  const hasEdit = await page.$("[data-editcam]");
  check("摄像头行可展开并有「修改」入口", !!hasEdit);
  if (hasEdit) {
    await hasEdit.click();
    await page.waitForTimeout(900);
    // 注意：页面里的 SET 是顶层 `const`，不会挂到 window 上，只能在
    // evaluate 的表达式里直接引用；写 window.SET 永远是 undefined。
    const editing = await page.evaluate(() => !!(SET && SET.editing && SET.draft));
    check("点「修改」后真的进入编辑态", editing, editing);

    // 遮蔽区整块已下线：编辑页里不应再有它的画布
    check("输出遮蔽区已从编辑页移除", !(await page.$("#maskCv-rd")));

    // 0.9.24：参数分四段手风琴，一次只展开一段，画布在「检测忽略区」段里
    const accMask = await page.$('[data-acc="mask"]');
    check("编辑页有四段手风琴", !!accMask && !!(await page.$('[data-acc="condense"]')));
    if (accMask) { await accMask.click(); await page.waitForTimeout(400); }
    const cv = await page.$("#maskCv-ig");
    check("检测忽略区画布已挂载", !!cv);
    if (cv) {
      // 画布在页面很下面，鼠标坐标若落在视口外，pointerdown 根本不会打到它。
      await cv.scrollIntoViewIfNeeded();
      await page.waitForTimeout(200);
      const box = await cv.boundingBox();
      // 16×9 网格；在左上拖 2×2 格（起点格 → 右下一格）
      const cw = box.width / 16, ch = box.height / 9;
      const x0 = box.x + cw * 0.5, y0 = box.y + ch * 0.5;
      const x1 = box.x + cw * 1.5, y1 = box.y + ch * 1.5;
      await page.mouse.move(x0, y0);
      await page.mouse.down();
      await page.mouse.move(x1, y1, { steps: 8 });
      await page.mouse.up();
      await page.waitForTimeout(400);
      const ig = await page.evaluate(() =>
        (SET.draft && SET.draft.masks && SET.draft.masks.ignore) || []);
      // 初始配置里 masks.ignore 是空的，拖 2×2 后应正好 4 格
      check("拖选忽略区后草稿里真有格子", ig.length === 4, ig);

      // 单路分片长度：切到「保留与分片」段，点「1 分钟」芯片，草稿应立刻变 60
      const accKeep = await page.$('[data-acc="keep"]');
      if (accKeep) { await accKeep.click(); await page.waitForTimeout(400); }
      const segBtn = await page.$('[data-camseg="60"]');
      check("编辑页有单路分片长度选项", !!segBtn);
      if (segBtn) {
        await segBtn.scrollIntoViewIfNeeded();
        await segBtn.click();
        await page.waitForTimeout(200);
        const seg = await page.evaluate(() => SET.draft && SET.draft.segSec);
        check("点分片芯片后草稿记下 60 秒", seg === 60, seg);
      }

      const saveBtn = await page.$("[data-save]");
      check("有保存按钮", !!saveBtn);
      if (saveBtn) {
        await saveBtn.click();
        await page.waitForTimeout(1800);
        const srvCfg = await (await fetch(`http://127.0.0.1:${PORT}/api/config`)).json();
        const cam0 = (srvCfg.config.cameras || [])[0] || {};
        const igSrv = ((cam0.masks || {}).ignore) || [];
        check("保存后忽略区真的落到服务端", igSrv.length === 4, igSrv);
        // 这是本项的核心：分片长度是 ffmpeg 命令行物理参数，必须真推上去
        check("保存后单路分片长度落到服务端", cam0.segment_sec === 60, cam0.segment_sec);
        check("服务端配置里不再有 redact",
              !((cam0.masks || {}).redact), cam0.masks);
      }
    }
  }

  // --- 扫描按钮 ---
  await page.evaluate(() => { const b = document.getElementById("btnScan"); b && b.click(); });
  await page.waitForTimeout(1200);
  const toast1 = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".toast")).map(e => e.textContent).join(" | "));
  check("扫描按钮给出真实分片数", /4\s*个分片/.test(toast1), toast1);

  // --- 清理：预览 → 确认 ---
  await page.evaluate(() => { SET.genOpen = true; renderCamTabs(); });
  await page.waitForTimeout(400);
  const cleanBtn = await page.$("#btnCleanNow");
  check("清理按钮存在", !!cleanBtn);
  if (cleanBtn) {
    await cleanBtn.click();
    await page.waitForTimeout(1500);
    const prev = await page.textContent("#cleanPreview");
    check("清理预览显示待删目录数", /将删除\s*1\s*个日期目录/.test(prev || ""), prev);
    const disabled = await page.$eval("#btnCleanNow", b => b.disabled);
    check("预览后按钮可再次点击（二次确认）", disabled === false, disabled);
    await page.evaluate(() => { const b = document.getElementById("btnCleanNow"); if (b) b.click(); });
    await page.waitForTimeout(2500);
    const gone = !fs.existsSync(path.join(rec, "车库", oldDay));
    check("确认后过期日期目录真被删除", gone, fs.readdirSync(path.join(rec, "车库")));
    const keep = fs.existsSync(path.join(rec, "车库", today));
    check("今天的目录没有被误删", keep);
    const raw = await page.evaluate(() =>
      Array.from(document.querySelectorAll(".toast")).map(e => e.textContent).join(" | "));
    check("删除结果有回执", /已删除/.test(raw), raw);
  }

  // --- 诊断导出 ---
  const dl = page.waitForEvent("download", { timeout: 8000 }).catch(() => null);
  await page.evaluate(() => { const b = document.getElementById("btnDiag"); if (b) b.click(); });
  const got = await dl;
  check("诊断导出触发下载", !!got, got ? "ok" : "no download event");
  if (got) {
    const p2 = await got.path();
    const txt = fs.readFileSync(p2, "utf8");
    check("诊断包含状态且凭据已脱敏",
          txt.includes("==== NVR") && !txt.includes("u:p@"), txt.slice(0, 40));
  }

  // --- 回放页：读到真实分片 ---
  await page.evaluate(() => { const b = document.querySelector('.tb[data-page="cam"]'); });
  await page.evaluate(async () => { await fetchRecordings(); });
  await page.waitForTimeout(800);
  const segs = await page.evaluate(() => (state.segs || []).length);
  check("回放页读到 3 个真实分片", segs === 3, segs);
  const lens = await page.evaluate(() => (state.segs || []).map(s => s.e - s.s));
  check("分片时长取服务端 300 秒而非写死",
        lens.length === 3 && lens[1] === 300 && lens[2] === 300, lens);
  const dayCount = await page.evaluate(() => (state.dayList || []).length);
  // 清理已经把 8 天前那天的目录删掉了，只剩今天。以前这里写死 slice(-7)，
  // 天数一多就把老日期藏起来，用户找不到录像。断言的是"列表来自真实扫描"。
  check("日期列表包含真实存在的每一天", dayCount === 1, dayCount);

  // --- 0.9.42 Aqara 回放页骨架：4 tab / 文件页瘦身 / 时间轴渲染 ---
  const tabs = await page.evaluate(() =>
    document.querySelectorAll(".tb").length);
  check("底部导航 4 个页签（直播/回放/文件/设置）", tabs === 4, tabs);
  const seg2 = await page.evaluate(() =>
    document.querySelector("[data-pmode]"));
  check("文件页 seg2（全时/精简切换）已退役", seg2 === null, !!seg2);
  const heatGone = await page.evaluate(() =>
    document.getElementById("heat") === null);
  check("文件页热力图已迁出", heatGone, heatGone);
  await page.click('.tb[data-page="play"]');
  await page.waitForTimeout(600);
  const onPlay = await page.evaluate(() =>
    document.getElementById("page-play").classList.contains("on"));
  check("回放页能打开", onPlay, onPlay);
  const hours = await page.evaluate(() =>
    document.querySelectorAll("#vtl .vhour").length);
  check("垂直时间轴默认密度 120px/h（1 小时刻度 × 25）", hours === 25, hours);
  // --- 时间轴缩放：默认 ×1.0，放大一档轨道变高，再缩小还原 ---
  const vh0 = await page.evaluate(() =>
    parseFloat(document.getElementById("vtl").style.height));
  check("默认轨道高度 = 30 + 24h×120px + 24", Math.abs(vh0 - 2934) < 1, vh0);
  await page.click('#btnVtlIn');
  await page.waitForTimeout(150);
  const vh1 = await page.evaluate(() => ({
    h: parseFloat(document.getElementById("vtl").style.height),
    pct: document.getElementById("vtlPct").textContent,
  }));
  check("放大按钮：轨道变高、倍率显示 ×1.3",
        vh1.h > vh0 && vh1.pct === "×1.3", vh1);
  const sc1 = await page.evaluate(() => {
    const sc = document.getElementById("vtlScroll");
    return { top: sc.scrollTop, };
  });
  await page.click('#btnVtlOut');
  await page.waitForTimeout(150);
  const vh2 = await page.evaluate(() => ({
    h: parseFloat(document.getElementById("vtl").style.height),
    pct: document.getElementById("vtlPct").textContent,
  }));
  check("缩小按钮还原到 ×1.0", Math.abs(vh2.h - vh0) < 1 && vh2.pct === "×1.0", vh2);
  check("缩放后视口仍停在有效区间（锚点不越界）",
        sc1.top >= 0, sc1);
  const avail = await page.evaluate(() =>
    document.querySelectorAll("#vtl .vavail").length);
  check("时间轴蓝轨来自真实分片覆盖", avail >= 1, avail);
  await page.waitForTimeout(400);   // 台账 fetch 回调写摘要
  const dayName = await page.evaluate(() =>
    document.getElementById("playDayName").textContent);
  check("菜单栏右侧日期下拉显示当前日期", /\d{2}-\d{2}/.test(dayName), dayName);
  const playSum = await page.evaluate(() =>
    document.getElementById("vtlSum").textContent);
  check("时间轴标题旁摘要带活动数与累计（台账 fixture：4 个活动）",
        /^4 个活动 · 累计/.test(playSum), playSum);
  check("摘要带全天各类别计数（AI fixture：人 3 · 车 5，两轮已并入车）",
        /人 3/.test(playSum) && /车 5/.test(playSum) && !/两轮/.test(playSum), playSum);
  const marks = await page.evaluate(() =>
    document.querySelectorAll("#vtl .vmark").length);
  check("台账事件渲染为 Aqara 式横线标注", marks >= 3, marks);
  /* --- 0.9.62 事件按类别分色，0.9.67 三色定版 + 三车道 ---
     色值不写死：拿图例里同类别的色片做基准，深浅主题切换都不受影响。 */
  const clsr = await page.evaluate(() => {
    const col = sel => getComputedStyle(document.querySelector(sel)).backgroundColor;
    const lg = k => col("#evLegend .lg .d." + k + " i");
    const spans = [...document.querySelectorAll("#vtl .vevspan")];
    const marks = [...document.querySelectorAll("#vtl .vmark")];
    const geo = m => [...m.querySelectorAll(".lanes i")].map(i => {
      const r = i.getBoundingClientRect(), mr = m.getBoundingClientRect();
      return { cls: i.className, left: Math.round(r.left - mr.left),
               right: Math.round(r.right - mr.left), col: getComputedStyle(i).backgroundColor };
    });
    return {
      spanCls: { person: spans.filter(s => s.querySelector(".c-person")).length,
                 veh:   spans.filter(s => s.querySelector(".c-veh")).length,
                 motion:spans.filter(s => s.querySelector(".c-motion")).length },
      /* 0.9.69：时段块的三格尾巴高度（px）——同一事件几格必须等高 */
      spanBars: spans.map(s => [...s.querySelectorAll("i")].map(i =>
        Math.round(i.getBoundingClientRect().height))),
      markCls: { person: marks.filter(s => s.querySelector(".c-person")).length,
                 veh:   marks.filter(s => s.querySelector(".c-veh")).length,
                 motion:marks.filter(s => s.querySelector(".c-motion")).length },
      bars: marks.map(geo),
      markW: marks.length ? Math.round(marks[0].getBoundingClientRect().width) : 0,
      legendNoai: document.getElementById("evLegend").classList.contains("noai"),
      legendItems: document.querySelectorAll("#evLegend .lg").length,
      lg: { person: lg("per"), veh: lg("veh"), motion: lg("mot") },
    };
  });
  /* 0.9.69：时段块也进三格（同一事件几格等高，高度=时长，4px 地板）——
     10:02 人×2 → 蓝格；10:13 车3+两轮1 → 绿格；11:01 人1+车1 → 蓝+绿两格；
     10:07 纯帧差 → 橙格。主导类别上色（evDom）已退役 */
  check("时段块收进三格（人×2 蓝 · 车1+两轮1 绿 · 混类蓝+绿 · 纯帧差橙）",
        clsr.spanCls.person === 2 && clsr.spanCls.veh === 2
        && clsr.spanCls.motion === 1, clsr.spanCls);
  check("同一事件的多格尾巴等高且不低于 4px 地板",
        clsr.spanBars.every(bs => bs.length > 0
          && bs.every(h => h >= 4 && h === bs[0])), clsr.spanBars);
  check("横线同样按类别上色（蓝 2 · 绿 2 · 橙 1）",
        clsr.markCls.person === 2 && clsr.markCls.veh === 2
        && clsr.markCls.motion === 1, clsr.markCls);
  /* 0.9.67 三车道：60px 轨道三等分，每格 19px，互不重叠。
     10:02+10:07 并组 → 左橙 + 中蓝；10:13 只有右绿；11:01 中蓝 + 右绿 */
  check("标记三格宽度三等分（每格约 19px，轨道 60px）",
        clsr.markW === 60
        && clsr.bars.flat().every(b => b.right - b.left === 19), [clsr.markW, clsr.bars]);
  check("三格互不重叠（同枚标记内任意两格不交叠）",
        clsr.bars.every(bs => bs.every((a, i) =>
          bs.every((b, j) => i === j || a.right <= b.left || b.right <= a.left))),
        clsr.bars);
  check("并组那根：左格橙（移动侦测）+ 中格蓝（人员），右格留空",
        clsr.bars[0].length === 2
        && clsr.bars[0].some(b => b.cls.includes("mot"))
        && clsr.bars[0].some(b => b.cls.includes("per")), clsr.bars[0]);
  check("10:13 那根：只有右格绿（车辆）", (() => {
    const one = clsr.bars.find(bs => bs.length === 1 && bs[0].cls.includes("veh"));
    return !!one && one[0].left >= 40;
  })(), clsr.bars);
  check("格子颜色与图例色片一致（同源取色，主题切换不失真）",
        clsr.bars.flat().every(b =>
          b.col === clsr.lg.person || b.col === clsr.lg.veh
          || b.col === clsr.lg.motion),
        [clsr.lg, clsr.bars]);
  check("类别图例常驻 3 项，有 AI 数据时点亮（不收进 noai）",
        clsr.legendItems === 3 && !clsr.legendNoai, clsr);
  check("定位胶囊已移除（播放落点不再弹软底高亮）",
        await page.evaluate(() =>
          [...document.querySelectorAll("#vtl .vmark")].every(m => !m.classList.contains("on"))));
  /* 0.9.68 上下留白：滚到两端时 00:00 / 24:00 正好压在固定时刻线上 */
  const ended = await page.evaluate(() => {
    const sc = document.getElementById("vtlScroll");
    const t = document.getElementById("vnowTime");
    const h = sc.clientHeight;
    const before = sc.scrollTop;
    sc.scrollTop = 0;
    const topTxt = (syncVnowline(), t.textContent);
    sc.scrollTop = sc.scrollHeight;
    const botTxt = (syncVnowline(), t.textContent);
    const room = sc.scrollHeight - h;
    sc.scrollTop = before;
    syncVnowline();
    return { topTxt, botTxt, room, want: Math.round(24 * vtlPxh) };
  });
  check("滚到最顶：时刻线显示 00:00:00", ended.topTxt === "00:00:00", ended);
  check("滚到最底：时刻线显示 24:00:00", ended.botTxt === "24:00:00", ended);
  check("可滚动量 = 24 小时行程（上下留白已补齐，不多不少）",
        Math.abs(ended.room - ended.want) <= 1, ended);
  const gone = await page.evaluate(() => ({
    dates: document.getElementById("playDates") === null,
    evList: document.getElementById("playEvents") === null,
    playscroll: document.querySelector(".playscroll") === null,
    legend: document.querySelector(".vtllegend") === null,
    files: document.getElementById("btnPlayFiles") === null,
    osd: document.querySelector("#playStage .posd") === null,
  }));
  check("日期 chip/活动列表/图例/文件按钮/画面 OSD 均已移除",
        gone.dates && gone.evList && gone.playscroll && gone.legend && gone.files && gone.osd, gone);

  // --- 0.9.43 P1：常驻画面区 + 点按跳播 + 模拟缩时/原速 ---
  const vids = await page.evaluate(() =>
    document.querySelectorAll("#playStage video").length);
  check("回放页常驻画面区含双 video（A/B 预加载）", vids === 2, vids);
  const seek1 = await page.evaluate(() => {
    const ok = pvPlayAt(36010);       // 测试分片在 10:00 起
    return { ok, idx: PPLAY.segIdx, src: pvCur.getAttribute("src") || "" };
  });
  check("点按时刻 10:00:10 跳播命中第一个分片",
        seek1.ok && seek1.idx === 0 && seek1.src.indexOf("/api/record/file") >= 0, seek1);
  const seekGap = await page.evaluate(() => pvPlayAt(12 * 3600));   // 正午无分片
  check("无画面时刻拒绝跳播（宁可不播不黑屏）", seekGap === false, seekGap);
  // --- 0.9.47：操作条移到画面区外下方（不遮画面）---
  const bar = await page.evaluate(() => {
    const b = document.querySelector("#page-play .pops");
    const stage = document.getElementById("playStage").getBoundingClientRect();
    const br = b.getBoundingClientRect();
    return { bar: !!b, outside: br.top >= stage.bottom - 1,
      tog: !!document.getElementById("btnPlayTog"),
      spd: (document.getElementById("btnPvSpeed") || {}).textContent,
      seg: document.querySelectorAll("#page-play .pseg [data-pvfilter]").length,
      full: !!document.getElementById("btnPlayFull"),
      line: !!document.getElementById("vnowline") };
  });
  check("操作条在画面区外下方：播放/倍速/全时活动筛选/全屏齐全",
        bar.bar && bar.outside && bar.tog && bar.seg === 2 && bar.full && /1×/.test(bar.spd || ""), bar);
  check("固定时刻线挂在时间轴卡上（JS 定位）", bar.line, bar);
  const linePos = await page.evaluate(() => {
    const sc = document.getElementById("vtlScroll");
    const ln = document.getElementById("vnowline");
    const i = ln.querySelector("i"), em = ln.querySelector("em");
    const ir = i.getBoundingClientRect(), er = em.getBoundingClientRect();
    return { top: parseFloat(ln.style.top),
      twoThird: sc.offsetTop + Math.round(sc.clientHeight / 3),
      lineColor: getComputedStyle(i).backgroundColor,
      emColor: getComputedStyle(em).color,
      midGap: Math.abs((ir.top + ir.bottom) / 2 - (er.top + er.bottom) / 2) };
  });
  check("固定时刻线在自下而上 2/3 高度（视口上 1/3 处）",
        Math.abs(linePos.top - linePos.twoThird) < 2, linePos);
  check("时刻文字与横线红色且互相居中（线对准数字中腰）",
        /rgb\(21[12], 59, 59\)/.test(linePos.lineColor) &&
        linePos.lineColor === linePos.emColor &&
        linePos.midGap < 2, linePos);
  await page.click('[data-pvfilter="full"]');
  const filtFull = await page.evaluate(() => ({
    filter: PPLAY.filter,
    chip: document.querySelector('[data-pvfilter="full"]').classList.contains("on") }));
  await page.click('[data-pvfilter="act"]');
  const filtAct = await page.evaluate(() => ({
    filter: PPLAY.filter,
    chip: document.querySelector('[data-pvfilter="act"]').classList.contains("on"),
    dim: document.getElementById("vtlCard").classList.contains("actmode"),
    spans: document.querySelectorAll("#vtl .vevspan").length }));
  /* 活动筛选核心行为：掉出活动窗口 = 跳到下一活动起点（垫 3 秒）。
     fixture：10:02:15-27 / 10:07:19-11:11:56... 落在 10:04（静止段）应跳 10:07:16 */
  const skip = await page.evaluate(() => {
    const ok = pvPlayAt(10 * 3600 + 4 * 60, { quiet: true });
    const jumped = pvSkipStatic();
    return { ok, jumped, t: PPLAY.t, expect: 10 * 3600 + 7 * 60 + 19 - 3 };
  });
  check("活动筛选：自动跳过全时录像的静止部分，连播下一个活动",
        skip.ok && skip.jumped && Math.abs(skip.t - skip.expect) < 1.5, skip);
  await page.click('[data-pvfilter="full"]');
  await page.evaluate(() => { pvStop(); pvSyncCtl(); });
  check("筛选切换 全时/活动：芯片同步 + 活动模式时间轴变淡且点亮活动时段块",
        filtFull.filter === "full" && filtFull.chip &&
        filtAct.filter === "act" && filtAct.chip &&
        filtAct.dim && filtAct.spans >= 3, [filtFull, filtAct]);
  // --- 0.9.48：全时模式活动标记常亮 / 日期下拉含今天 / 换日期自动定位开播 / 拖动续播 ---
  const evVis = await page.evaluate(() => {
    const sp = document.querySelector("#vtl .vevspan");
    const mk = document.querySelector("#vtl .vmark");
    return { spOp: sp ? getComputedStyle(sp).opacity : null,
      mkZ: mk ? getComputedStyle(mk).zIndex : null };
  });
  check("全时模式活动标记常亮（橙色时段块+横线在上层不被遮盖）",
        parseFloat(evVis.spOp) > 0.5 && +evVis.mkZ >= 2, evVis);
  await page.click('#playDaySel');
  const dayHasToday = await page.evaluate(() => {
    const t = new Date();
    const md = ("0" + (t.getMonth() + 1)).slice(-2) + "-" + ("0" + t.getDate()).slice(-2);
    return { md, rows: Array.from(document.querySelectorAll("#playDayPick .dp"))
      .map(x => x.textContent) };
  });
  check("日期下拉包含当前日期",
        dayHasToday.rows.some(x => x.indexOf(dayHasToday.md) >= 0), dayHasToday);
  await page.evaluate(() => playDayShow(false));
  const jump = await page.evaluate(() => {
    pvStop();
    const ok = pvJumpLatest();
    const segs = PPLAY.segs, last = segs[segs.length - 1];
    return { ok, on: PPLAY.on, t: PPLAY.t, lastS: last ? last.s : null,
      lastE: last ? last.e : null };
  });
  check("换日期/机位自动定位最新全时录像并回溯 30 秒开播",
        jump.ok && jump.on && jump.lastE != null &&
        Math.abs(jump.t - Math.max(jump.lastS, jump.lastE - 30)) < 1, jump);
  const scrub = await page.evaluate(async () => {
    pvStop();
    pvPlayAt(10 * 3600 + 10 * 60 + 10, { quiet: true });   // 播放中（10:10:10）
    const sc = document.getElementById("vtlScroll");
    vtlHold = Date.now();                                  // 模拟用户手指拖动
    const target = 10 * 3600 + 12 * 60;                    // 拖到 10:12（分片覆盖内）
    sc.scrollTop = Math.max(0, vtlY(target) - sc.clientHeight / 3);
    await new Promise(r => setTimeout(r, 700));
    /* scrollTop 是整数（浏览器截断），期望值按实际 scrollTop 反推，容 1px 量化差 */
    const expectT = Math.max(0, (sc.scrollTop + sc.clientHeight / 3 - VTL_TOP) / vtlPxh * 3600);
    return { t: PPLAY.t, expectT, target };
  });
  check("播放中拖动时间轴：松手后从固定线时刻继续播（不被拽回原进度）",
        Math.abs(scrub.t - scrub.expectT) < 1, scrub);
  await page.evaluate(() => pvStop());
  // --- 0.9.47：全屏回放 = 横屏全屏 + 时间轴悬浮（播放隐藏/暂停恢复） ---
  // 0.9.51 回归：部分 webview 的 orientation.lock 永远挂起（真机全屏仍竖屏根因），
  // 覆盖成「永不 resolve 的 lock」，进全屏后 800ms 竞速超时也必须转横屏
  await page.evaluate(() => {
    const so = screen.orientation;
    if(!window.__origLock) window.__origLock = so && so.lock ? so.lock.bind(so) : null;
    if(so) Object.defineProperty(so, "lock",
      { value: () => new Promise(()=>{}), configurable: true });
  });
  await page.click('#btnPlayFull');
  await page.waitForTimeout(600);
  const fsOn = await page.evaluate(() => {
    const cs = getComputedStyle(document.getElementById("vtlCard"));
    const pp = document.getElementById("page-play");
    return {
      fs: document.body.classList.contains("fsmode"),
      land: !!document.querySelector(".phone.land"),
      cardAbs: cs.position, cardBg: cs.backgroundColor, cardShadow: cs.boxShadow,
      fsTag: (document.fullscreenElement || document.webkitFullscreenElement || {}).tagName || null,
      pageDims: [pp.offsetWidth, pp.offsetHeight] };
  });
  /* 0.9.54：全屏画面不裁剪（contain）+ 舞台吃掉整屏（含操作条原来占的那条高度）
     + 操作条悬浮在画面之上、按钮放大到可点尺寸且互不重叠 */
  const fsLayout = await page.evaluate(() => {
    const vid = document.querySelector("#playStage video.on") || document.querySelector("#playStage video");
    const st = document.getElementById("playStage");
    const pops = document.querySelector("#page-play .pops");
    const pg = document.getElementById("page-play");
    /* 用 offset* 而不是 getBoundingClientRect：旋转态下 bbox 已按 90° 换轴，
       量不出真实横排关系。底栏是横排比左右，黑边轨是竖排比上下 */
    const col = getComputedStyle(pops).flexDirection === "column";
    const btns = [...pops.querySelectorAll(".pbtn, .pseg")].map(e => col
      ? { l: e.offsetTop, r: e.offsetTop + e.offsetHeight }
      : { l: e.offsetLeft, r: e.offsetLeft + e.offsetWidth }
    ).sort((a, b) => a.l - b.l);
    let overlap = false;
    for (let i = 1; i < btns.length; i++) if (btns[i].l < btns[i-1].r - 1) overlap = true;
    const segBtn = pops.querySelector(".pseg button");
    return { fit: getComputedStyle(vid).objectFit,
             rail: document.body.classList.contains("rail"),
             stagePos: getComputedStyle(st).position,
             popsPos: getComputedStyle(pops).position,
             stageH: st.offsetHeight, stageW: st.offsetWidth, popsH: pops.offsetHeight,
             pageH: pg.offsetHeight, pageW: pg.offsetWidth,
             /* 操作条贴底（悬浮）：底边与页面底边对齐，不再挤占画面高度 */
             popsGap: pops.offsetParent === pg
               ? pg.offsetHeight - (pops.offsetTop + pops.offsetHeight) : -1,
             minBtnH: Math.min(...[...pops.querySelectorAll(".pbtn")].map(e => e.offsetHeight)),
             segH: segBtn ? segBtn.offsetHeight : 0,
             segW: segBtn ? segBtn.offsetWidth : 0, overlap };
  });
  /* 0.9.55：横屏时按钮要排进左右黑边（rail 布局），竖屏没黑边就用底栏悬浮 */
  check("全屏画面不裁剪（contain）+ 舞台整屏 + 按钮 ≥40px 不重叠（底栏/黑边轨两种布局都算）",
        fsLayout.fit === "contain" && fsLayout.stagePos === "absolute" &&
        fsLayout.popsPos === "absolute" &&
        (fsLayout.rail
          ? fsLayout.popsH >= fsLayout.pageH - 2
          : fsLayout.popsGap === 0) &&
        fsLayout.stageH >= fsLayout.pageH - 2 && fsLayout.stageW >= fsLayout.pageW - 2 &&
        fsLayout.minBtnH >= 40 && !fsLayout.overlap &&
        fsLayout.segH >= (fsLayout.rail ? 30 : 36) && fsLayout.segW >= 44, fsLayout);
  /* 0.9.55：黑边轨必须真的落在黑边里——左轨右边缘 ≤ 画面左边缘，
     右轨（退出/旋转）左边缘 ≥ 画面右边缘；两个轨道都要竖排（flex-direction:column） */
  const fsRail = await page.evaluate(() => {
    const pg = document.getElementById("page-play");
    const pops = document.querySelector("#page-play .pops");
    const tools = document.getElementById("fsTools");
    const st = document.getElementById("playStage");
    const w = pg.offsetWidth, h = pg.offsetHeight;
    /* 假 mp4 没有元数据 → vidAsp 走默认 16:9；contain 后的画面宽度 = 高度×画幅 */
    const drawnW = Math.round(h * (16 / 9));
    const picL = Math.round((w - drawnW) / 2), picR = Math.round((w + drawnW) / 2);
    return { rail: document.body.classList.contains("rail"),
      pageW: w, pageH: h, stageH: st.offsetHeight, picL: picL, picR: picR,
      railw: getComputedStyle(document.documentElement).getPropertyValue("--railw").trim(),
      popsRight: pops.offsetLeft + pops.offsetWidth, popsH: pops.offsetHeight,
      popsDir: getComputedStyle(pops).flexDirection,
      segDir: getComputedStyle(pops.querySelector(".pseg")).flexDirection,
      toolsLeft: tools ? tools.offsetLeft : -1,
      toolsTop: tools ? tools.offsetTop : -1,
      toolsH: tools ? tools.offsetHeight : -1,
      toolsDir: tools ? getComputedStyle(tools).flexDirection : "",
      toolsLabelHidden: tools ? [...tools.querySelectorAll("span")].every(
        s => getComputedStyle(s).display === "none") : false };
  });
  check("横屏按钮排进左右黑边：左轨放播放控制、右轨放退出/旋转，都不压画面",
        fsRail.rail && fsRail.popsRight <= fsRail.picL + 1 &&
        fsRail.toolsLeft >= fsRail.picR - 1 &&
        fsRail.popsDir === "column" && fsRail.segDir === "column" &&
        fsRail.toolsDir === "column" && fsRail.toolsLabelHidden &&
        fsRail.popsH >= fsRail.pageH - 2 && fsRail.toolsH >= fsRail.pageH - 2,
        fsRail);
  /* 0.9.55：旋转态（CSS rotate 90°）下时间轴要能拖——屏幕横向位移换算成 scrollTop。
     真机是原生滚动被 touch-action 按屏幕方向拦掉，这里直接验我们自己的换算逻辑 */
  const rotDrag = await page.evaluate(() => {
    const sc = document.getElementById("vtlScroll");
    sc.scrollTop = 0;
    const before = sc.scrollTop;
    const mk = (type, x) => {
      const t = new Touch({ identifier: 1, target: sc, clientX: x, clientY: 200 });
      sc.dispatchEvent(new TouchEvent(type, {
        touches: type === "touchend" ? [] : [t],
        targetTouches: type === "touchend" ? [] : [t],
        changedTouches: [t], bubbles: true, cancelable: true }));
    };
    mk("touchstart", 120);
    for (let i = 1; i <= 6; i++) mk("touchmove", 120 + i * 10);
    mk("touchend", 180);
    const after = sc.scrollTop;
    return { rot: document.body.classList.contains("rot"), before, after,
             touchAction: getComputedStyle(sc).touchAction };
  });
  check("旋转态时间轴可拖动（横屏暂停后时间轴拖不动 → 屏幕横向位移换算 scrollTop）",
        rotDrag.rot && rotDrag.touchAction === "none" && rotDrag.after >= 50, rotDrag);
  const fsPlay = await page.evaluate(() => {
    /* 假 mp4 无法真实 play（paused 恒为 true），用实例属性遮蔽 paused 模拟播放态 */
    Object.defineProperty(pvCur, "paused", { get: () => false, configurable: true });
    PPLAY.on = true; pvSyncCtl();
    const r = { pv: document.getElementById("page-play").classList.contains("pvplaying"),
      pe: getComputedStyle(document.getElementById("vtlCard")).pointerEvents };
    delete pvCur.paused;
    return r;
  });
  const fsPause = await page.evaluate(() => {
    /* 暂停态：paused 恢复真实值 true，PPLAY.on 仍为 true（暂停 ≠ 停止） */
    try{ pvCur.pause(); }catch(e){}
    PPLAY.on = true; pvSyncCtl();
    return { pv: document.getElementById("page-play").classList.contains("pvplaying"),
      pe: getComputedStyle(document.getElementById("vtlCard")).pointerEvents };
  });
  /* 0.9.53：全屏工具组（退出/旋转）挂进容器内 → 跟着旋转，永远在用户视角右上角；
     诊断角标改成默认隐藏（?fsdiag=1 才出现） */
  const rotBtn = await page.evaluate(() => {
    const box = document.getElementById("fsTools");
    const b = document.getElementById("fsRot");
    const d = document.getElementById("fsDiag");
    localStorage.setItem("fsdiag", "1");
    fsDiag();
    const diagOn = !!d && d.classList.contains("on") && !!d.textContent;
    localStorage.removeItem("fsdiag");
    fsDiag();
    const diagOff = !d || !d.classList.contains("on");
    const cs = box ? getComputedStyle(box) : null;
    return {
      exists: !!b, visible: !!b && !!cs && cs.display !== "none",
      groupOn: !!box && box.classList.contains("on"),
      inPhone: !!box && document.getElementById("phone").contains(box),
      diagOn, diagOff,
      rot: state.rot,
      btnCount: box ? box.querySelectorAll("button").length : 0,
    };
  });
  check("全屏工具组：退出+旋转同组、挂在容器内随旋转、诊断角标默认隐藏",
        rotBtn.exists && rotBtn.visible && rotBtn.groupOn && rotBtn.inPhone &&
        rotBtn.btnCount === 2 && rotBtn.diagOn && rotBtn.diagOff, rotBtn);
  const rotToggle = await page.evaluate(async () => {
    const before = state.rot, b = document.getElementById("fsRot");
    b.click(); await new Promise(r => setTimeout(r, 150));
    const mid = state.rot;
    b.click(); await new Promise(r => setTimeout(r, 150));
    const after = state.rot;
    try { localStorage.removeItem("fsRot"); } catch(e) {}
    return { before, mid, after };
  });
  check("旋转按钮可切换横/竖屏并切回",
        rotToggle.mid !== rotToggle.before && rotToggle.after === rotToggle.before, rotToggle);

  /* lock 竞速超时是 800ms，fsPause 已消耗约 600+，再等一拍后必已转横屏 */  const rotHang = await page.evaluate(async () => {
    await new Promise(r => setTimeout(r, 700));
    return { rot: document.body.classList.contains("rot"),
      portrait: window.innerHeight > window.innerWidth,
      cardBg: getComputedStyle(document.getElementById("vtlCard")).backgroundColor,
      scrollBg: getComputedStyle(document.getElementById("vtlScroll")).backgroundColor };
  });
  check("lock 永久挂起时仍转横屏（800ms 超时兜底）+ 时间轴双层底色透明",
        (!rotHang.portrait || rotHang.rot) &&
        /rgba\(0, 0, 0, 0\)|transparent/.test(rotHang.cardBg) &&
        /rgba\(0, 0, 0, 0\)|transparent/.test(rotHang.scrollBg), rotHang);
  await page.evaluate(() => {
    const so = screen.orientation;
    if(so){ if(window.__origLock) Object.defineProperty(so, "lock", { value: window.__origLock, configurable: true });
            else delete so.lock; }
  });
  await page.evaluate(() => exitFull());
  await page.waitForTimeout(400);
  const fsOff = await page.evaluate(() => ({
    fs: document.body.classList.contains("fsmode"),
    pv: document.getElementById("page-play").classList.contains("pvplaying") }));
  await page.evaluate(() => { pvStop(); });
  check("全屏回放：进入 fsmode/横屏，时间轴变悬浮层且底色透明（不遮画面）",
        fsOn.fs && fsOn.land && fsOn.cardAbs === "absolute" &&
        /rgba\(0, 0, 0, 0\)|transparent/.test(fsOn.cardBg) && fsOn.cardShadow === "none", fsOn);
  /* 真机三轮"全屏仍竖屏"的根因：对页面元素 requestFullscreen 会被提到顶层，
     UA 的 :fullscreen{position:fixed} 让它脱离 .phone 的旋转 —— 必须全屏根节点 */
  check("全屏只提根节点（页面元素进顶层会绕过旋转）",
        !fsOn.fsTag || fsOn.fsTag === "HTML", fsOn);
  check("旋转态下页面按横向空间布局（宽>高）",
        fsOn.pageDims[0] > fsOn.pageDims[1], fsOn.pageDims);
  check("全屏播放中隐藏时间轴悬浮层，暂停恢复（仍可筛选拖动跳转）",
        fsPlay.pv && fsPlay.pe === "none" && !fsPause.pv && fsPause.pe !== "none", [fsPlay, fsPause]);
  check("退出全屏恢复正常布局", !fsOff.fs, fsOff);
  // --- 0.9.43 P1：设置页缩时与存储方案区块 ---
  await page.click('.tb[data-page="cam"]');
  await page.waitForTimeout(500);
  const setChk = await page.evaluate(() => ({
    cmode: document.querySelectorAll("[data-cmode]").length,
    clipstyle: document.querySelectorAll("[data-clipstyle]").length,
    silent: document.querySelectorAll("[data-silent]").length,
    lk: LK.condenseMode,
  }));
  check("设置页有方案模式三选 + 静默段处理（虚拟模式）",
        setChk.cmode === 3 && setChk.silent === 3 && setChk.lk === "virtual", setChk);
  check("虚拟缩时下切分方式选项隐藏（避免假选项）", setChk.clipstyle === 0, setChk);

  // --- 缩时片段：时长必须来自文件本身，不能拿 segment_sec 冒充 ---
  const clipSegs = await page.evaluate(() =>
    realSegs(state.camId, curDay(), "clip"));
  check("缩时清单能读到 3 个片段", !!clipSegs && clipSegs.length === 3,
        clipSegs && clipSegs.length);
  if (clipSegs && clipSegs.length) {
    // 造的是全 0 字节假文件，ffprobe 读不到时长 → 兜底 0.2 秒。
    // 关键断言：绝不能是 300（segment_sec），那就是"显示两小时"的根因。
    const durs = clipSegs.map(s => s.e - s.s);
    check("缩时片段时长不冒充 segment_sec", durs.every(d => d < 10), durs);
    check("缩时片段 time 轴从 0 累加（不是钟点轴）",
          clipSegs[0].s === 0 && clipSegs[2].s > clipSegs[1].s,
          clipSegs.map(s => [s.s, s.e]));
    check("缩时片段带 clip 标记", clipSegs.every(s => s.clip === true));
    const total = clipSegs.reduce((a, s) => a + (s.e - s.s), 0);
    check("缩时总时长按片段累加（远小于 2 小时）", total < 30, total);
  }

  // ================= 直播层：单画面与宫格都必须真的去拉流 =================
  /* 关键：src 必须在同一个 tick 里读。
     本机没有 ffmpeg，/api/live/stream 会回 400，img.onerror 一触发就会回落快照；
     等 250ms 再读只能看到回落后的快照地址，测的就不是"有没有发起拉流"了。 */
  const liveRoom = await page.evaluate(() => {
    const read = () => {
      const t = document.querySelector("#lrBody .lr-tile");
      const i = t && t.querySelector(".snap-img");
      return {
        n: document.querySelectorAll("#lrBody .lr-tile").length,
        src: (i && i.getAttribute("src")) || "",
        streaming: !!(t && t.classList.contains("streaming")),
      };
    };
    enterLive(0);
    const single = read();
    single.hasFloat = !!document.querySelector(".lr-float");
    single.btnText = (document.getElementById("lrPlay") || {}).textContent || "";
    state.liveView = "grid";
    buildLiveRoom();
    const g = Array.from(document.querySelectorAll("#lrBody .lr-tile"));
    return {
      single,
      grid: {
        n: g.length,
        streaming: g.length > 0 && g.every(t => t.classList.contains("streaming")),
        srcs: g.map(t => {
          const i = t.querySelector(".snap-img");
          return i ? (i.getAttribute("src") || "") : "";
        }),
      },
    };
  });
  check("单画面进入后真的请求实时流",
        /\/api\/live\/stream\//.test(liveRoom.single.src)
        && /fps=8&w=960/.test(liveRoom.single.src), liveRoom.single.src);
  check("单画面瓦片标记为 streaming", liveRoom.single.streaming, liveRoom.single);
  check("画面里不再叠悬浮控件（旧的 lr-float 已删）",
        liveRoom.single.hasFloat === false);
  check("播放/暂停按钮在画面外的底栏",
        /暂停/.test(liveRoom.single.btnText), liveRoom.single.btnText);
  check("宫格每块都在拉流，且用低帧率参数",
        liveRoom.grid.n === liveRoom.single.n && liveRoom.grid.streaming
        && liveRoom.grid.srcs.every(s => /\/api\/live\/stream\//.test(s)
             && /fps=4&w=480/.test(s)),
        { n: liveRoom.grid.n, srcs: liveRoom.grid.srcs });

  // 流拉不起来（本机没有 ffmpeg / 子码流没有）时必须说清楚并回落，不能装死
  const liveFallback = await page.evaluate(async () => {
    enterLive(0);
    await new Promise(r => setTimeout(r, 1600));
    const tile = document.querySelector("#lrBody .lr-tile");
    return {
      streaming: tile.classList.contains("streaming"),
      overlay: (tile.querySelector("[data-np]") || {}).textContent || "",
    };
  });
  check("流不可用时不卡在「等待第一帧」",
        liveFallback.streaming || /回落快照/.test(liveFallback.overlay),
        liveFallback);

  // 暂停 = 冻结：换回静态快照，画面上出现唯一一个播放按钮
  const livePaused = await page.evaluate(async () => {
    enterLive(0);
    await new Promise(r => setTimeout(r, 200));
    document.getElementById("lrPlay").click();
    await new Promise(r => setTimeout(r, 250));
    const tile = document.querySelector("#lrBody .lr-tile");
    return {
      btn: document.getElementById("lrPlay").textContent,
      pauseBtn: !!tile.querySelector(".lr-pausebtn"),
      src: tile.querySelector(".snap-img").getAttribute("src") || "",
      sub: document.getElementById("lrSub").textContent,
    };
  });
  check("点底栏按钮进入暂停态", /播放/.test(livePaused.btn) && livePaused.pauseBtn,
        livePaused);
  check("暂停后不再占流（换回快照）",
        !/\/api\/live\/stream\//.test(livePaused.src), livePaused.src);

  const liveLeft = await page.evaluate(async () => {
    leaveLive();
    await new Promise(r => setTimeout(r, 150));
    LAYERS = 0;                       // 这几次 enterLive 压的层要还回去
    return {
      tiles: document.querySelectorAll("#lrBody .lr-tile").length,
      on: document.getElementById("liveRoom").classList.contains("on"),
    };
  });
  check("退出实时层会清掉瓦片（连接随之关闭）",
        liveLeft.tiles === 0 && liveLeft.on === false, liveLeft);


  // --- 全屏：真进全屏 + 右上角出现退出按钮 + 点它能退 ---
  await page.evaluate(() => { toggleFull(); });
  await page.waitForTimeout(700);
  const fsState = await page.evaluate(() => {
    const b = document.getElementById("fsExit");
    const box = document.getElementById("fsTools");
    return {
      fsmode: document.body.classList.contains("fsmode"),
      exitVisible: !!b && !!box && box.classList.contains("on")
                   && getComputedStyle(box).display !== "none",
      fsEl: !!(document.fullscreenElement || document.webkitFullscreenElement),
    };
  });
  check("全屏后进入 fsmode（容器铺满视口）", fsState.fsmode, fsState);
  check("全屏后右上角出现退出按钮", fsState.exitVisible, fsState);

  const exitBox = await page.$("#fsExit");
  if (exitBox) {
    const bb = await exitBox.boundingBox();
    /* 旋转态下"用户视角右上角"落在屏幕物理边缘中部，所以这里只验尺寸与不出界；
       是否锚在画面右上角由容器局部坐标断言（见下） */
    check("退出按钮触控区 ≥40px 且在视口内可点",
          !!bb && bb.width >= 40 && bb.height >= 40 &&
          bb.x >= -2 && bb.y >= -2 &&
          bb.x + bb.width <= 422 && bb.y + bb.height <= 902, bb);
    await exitBox.click();
    await page.waitForTimeout(600);
    const after = await page.evaluate(() => ({
      fsmode: document.body.classList.contains("fsmode"),
      land: !!state.land, rot: !!state.rot,
      exitOn: (document.getElementById("fsTools") || {}).className || "",
    }));
    check("点退出按钮真的退出全屏",
          !after.fsmode && !after.land && !after.rot
          && !/on/.test(after.exitOn), after);
  }

  // 竖屏视口下锁不了方向时必须转画面（用户要的是"横过来看"，不是"调用成功"）
  await page.evaluate(() => { toggleFull(); });
  await page.waitForTimeout(700);
  const rotState = await page.evaluate(() => ({
    fsmode: document.body.classList.contains("fsmode"),
    rot: document.body.classList.contains("rot"),
    w: window.innerWidth, h: window.innerHeight,
  }));
  check("锁不了横屏时自动旋转画面兜底",
        rotState.fsmode && (rotState.w <= rotState.h ? rotState.rot : true), rotState);
  if (rotState.rot) {
    // 画面转了 90°，退出按钮必须还在「用户眼里」的右上角，而且自己不能躺着
    const rb = await page.evaluate(() => {
      const b = document.getElementById("fsExit");
      const r = b.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height,
               tf: getComputedStyle(b).transform,
               vw: window.innerWidth, vh: window.innerHeight };
    });
    check("旋转态下退出按钮仍在视口内且可见",
          rb.w > 0 && rb.h > 0 && rb.x >= -2 && rb.y >= -2
          && rb.x + rb.w <= rb.vw + 2 && rb.y + rb.h <= rb.vh + 2, rb);
    /* 0.9.53：工具组进容器后不再需要手写反向旋转——用「容器局部坐标」验证
       它始终锚在画面右上角（旋转与否都是用户视角的右上角），文字不补偿 */
    const anchor = await page.evaluate(() => {
      const ph = document.getElementById("phone");
      const box = document.getElementById("fsTools");
      const sp = document.querySelector("#fsExit span");
      return {
        gapRight: ph.offsetWidth - (box.offsetLeft + box.offsetWidth),
        gapTop: box.offsetTop,
        spanTf: sp ? getComputedStyle(sp).transform : "none",
        txt: sp ? sp.textContent : "",
        pageW: document.getElementById("page-play").offsetWidth,
        pageH: document.getElementById("page-play").offsetHeight,
      };
    });
    check("旋转态下工具组锚在画面右上角（随容器旋转，文字无需补偿）",
          anchor.gapRight >= 0 && anchor.gapRight <= 20 && anchor.gapTop <= 20 &&
          anchor.spanTf === "none" && /退出/.test(anchor.txt), anchor);
  }
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);
  const escState = await page.evaluate(() => ({
    fsmode: document.body.classList.contains("fsmode"),
    rot: document.body.classList.contains("rot"),
  }));
  check("Esc 能退出全屏横屏", !escState.fsmode && !escState.rot, escState);

  // --- 无缝续播：播完一段自动接下一段，且不清空 src ---
  const seamless = await page.evaluate(() => {
    const segs = state.segs || [];
    if (segs.length < 2) return { skip: true };
    state.segIdx = 0;
    const before = activeVideo();
    // 预加载是异步的（openPlayer 里拉下一段的 src），这里显式跑一次等它就绪
    preloadNext();
    const pre = { preloadFor, src: nxtVid && nxtVid.getAttribute("src") };
    finishSegAndNext();
    return {
      idx: state.segIdx,
      playing: state.playing,
      title: document.getElementById("pTitle").textContent,
      switchedEl: activeVideo() !== before,
      hasSrc: !!activeVideo().getAttribute("src"),
      pre,
    };
  });
  check("下一段已预加载（preloadFor 指向下一段）",
        seamless.skip || (seamless.pre && seamless.pre.preloadFor === 1
                          && !!seamless.pre.src), seamless.pre);
  check("播完一段自动接下一段", seamless.skip || seamless.idx === 1, seamless);
  check("续播后仍是播放状态（不用再点播放）", seamless.skip || seamless.playing === true,
        seamless);
  check("续播走 A/B 热切换（换了 video 元素）", seamless.skip || seamless.switchedEl === true,
        seamless);
  check("续播后新元素有 src（画面不会空白）", seamless.skip || seamless.hasSrc === true,
        seamless);

  // ================= 本轮修复的四项 =================

  // --- 5) 存储信息搬进「设置 · 诊断与日志」；顶栏 CPU / 内存 / 核显 早已下线 ---
  const resInfo = await page.evaluate(async () => {
    const r = await refreshRes();
    SET.genOpen = false; SET.diagOpen = true; SET.editing = false; SET.draft = null;
    renderCamTabs();
    const diag = document.querySelector(".zone.diag");
    const out = {
      ok: r.ok, err: r.err,
      diagText: diag ? diag.textContent.replace(/\s+/g, " ").trim() : "",
      hasLiveBar: !!document.querySelector(".resbar"),
      hasCpuEl: !!document.getElementById("resCpu"),
      hasStorageCard: !!document.querySelector(".zone.diag .dcard"),
      bogus: /178%|312\.6|UHD 730|96\.4 MB/.test(diag ? diag.textContent : ""),
    };
    SET.diagOpen = false;
    renderCamTabs();
    return out;
  });
  check("/api/status 仍然返回资源数据（接口保留，只是界面不展示）", resInfo.ok, resInfo.err);
  check("直播页不再有第二排状态条", resInfo.hasLiveBar === false);
  check("存储占用落在诊断与日志里", resInfo.hasStorageCard === true);
  check("诊断区不出现 CPU / 内存 / 核显 这类实时占用",
        !/CPU|内存|核显/.test(resInfo.diagText), resInfo.diagText);
  check("顶栏 CPU / 内存 / 核显 元素已移除", resInfo.hasCpuEl === false);
  check("页面不再出现编造的占用数字（178% / 312.6 / UHD 730）",
        resInfo.bogus === false);

  // --- 6) 快照墙右下角的「多久之前」必须真反映状态，不能假装新鲜 ---
  const cdFail = await page.evaluate(async () => {
    const cam = CAMERAS[0];
    const st = snapSt(cam.id);
    // 模拟"上一次成功在很久以前"，且接下来请求一定失败
    st.at = Date.now() - 60000; st.fails = 0; st.pend = false; st.since = 0;
    // 造一个必然 404 的地址：直接把 img 指到不存在的路径
    const img = document.querySelector("#liveWrap .livecard .snap-img");
    if (img) img.src = "/api/live/snapshot/__nope__.jpg?t=" + Date.now();
    // 真实跑一次 pullSnap，但让它去打一个不存在的摄像头
    await new Promise(res => pullSnap("__nope__", img, () => res()));
    return { fails: snapSt("__nope__").fails };
  });
  check("拉图失败时该路进入失败态", cdFail.fails > 0, cdFail);

  const cdText = await page.evaluate(async () => {
    const cam = CAMERAS[0];
    const st = snapSt(cam.id);
    /* 失败态优先于离线态：网断了 c.online 也是 false，但用户看到的应是
       "未拿到画面"（这张图没刷新），而不是一句笼统的"离线"。 */
    st.fails = 3; st.at = Date.now() - 60000; st.pend = false;
    updateSnapLabels();
    const card = document.querySelector("#liveWrap .livecard");
    const ago = card.querySelector("[data-ago]");
    return {
      ago: (card.querySelector("[data-ago-txt]") || {}).textContent,
      bad: ago ? /bad/.test(ago.className) : false,
      dot: !!ago && !!ago.querySelector(".dot"),
      stamp: (card.querySelector("[data-stamp]") || {}).textContent,
    };
  });
  check("失败时不再显示「刚刚」这类假的新鲜度",
        cdText.ago !== "刚刚" && cdText.ago !== "0 秒前"
        && /未拿到画面|离线/.test(cdText.ago || ""), cdText);
  check("失败时状态点转红（bad 类）且点元素存在",
        cdText.bad === true && cdText.dot === true, cdText);
  check("时间戳是抓拍时刻（HH:MM:SS），不是当前钟点",
        /^\d\d:\d\d:\d\d$/.test(cdText.stamp || ""), cdText.stamp);

  const cdOk = await page.evaluate(() => {
    const cam = CAMERAS[0];
    cam.online = true;                   // 这一项测的是"在线且正常"的显示
    const st = snapSt(cam.id);
    st.fails = 0; st.at = Date.now() - 3000; st.pend = false;
    updateSnapLabels();
    const card = document.querySelector("#liveWrap .livecard");
    const ago = card.querySelector("[data-ago]");
    return {
      txt: (card.querySelector("[data-ago-txt]") || {}).textContent,
      bad: ago ? /bad/.test(ago.className) : null,
    };
  });
  check("正常时年龄条回到「N 秒前」且状态点是绿色（无 bad 类）",
        /秒前/.test(cdOk.txt || "") && cdOk.bad === false, cdOk);

  // --- 4) 手势返回逐层退，不能一退就退出应用 ---
  const hist = await page.evaluate(async () => {
    const wait = ms => new Promise(r => setTimeout(r, ms));
    const out = {};
    // 回到直播页根，并清掉之前压的层（前面的用例开过播放器/实时层）
    activatePage("live"); state.liveView = "snap";
    document.getElementById("liveRoom").classList.remove("on");
    document.getElementById("player").classList.remove("on");
    SET.editing = false; SET.draft = null; SET.open = null;
    popToRoot();
    await wait(300);
    out.layerAtRoot = LAYERS;
    out.atRootAtStart = atRoot();

    // 进回放页 → 应压一层
    goPage("list");
    await wait(150);
    out.layersAfterGoList = LAYERS;
    out.pageAfterGoList = activePage();

    // 返回一次 → 回直播页，且不应退出（atRoot 应为 true）
    history.back();
    await wait(600);
    out.pageAfterBack = activePage();
    out.layersAfterBack = LAYERS;
    out.atRootAfterBack = atRoot();

    // 再返回一次：已经到根，应交给系统（这里不再压层，浏览器会尝试关页）
    out.layersStable = LAYERS === 0;
    return out;
  });
  check("起始状态停在一个干净的根", hist.atRootAtStart === true && hist.layerAtRoot === 0, hist);
  check("进二级页会压一层历史", hist.layersAfterGoList === 1, hist);
  check("从二级页返回回到直播页", hist.pageAfterBack === "live", hist);
  check("返回后层深归零（不再残留叠层）", hist.layersAfterBack === 0, hist);
  check("回到直播页后才算根（下一次返回才交给系统）", hist.atRootAfterBack === true, hist);

  // 实时层与播放器也必须各占一层，否则左滑会直接退应用
  const hist2 = await page.evaluate(async () => {
    const wait = ms => new Promise(r => setTimeout(r, ms));
    const out = {};
    goPage("list");
    await wait(150);
    // 进播放器
    const segs = state.segs || [];
    if (segs.length) { openPlayer(0); }
    await wait(200);
    out.layerWithPlayer = LAYERS;
    out.playerOn = document.getElementById("player").classList.contains("on");
    // 左滑一次 → 只关播放器，还在回放页
    history.back();
    await wait(500);
    out.playerAfterBack = document.getElementById("player").classList.contains("on");
    out.pageAfterPlayerBack = activePage();
    out.appStillAlive = !!document.querySelector(".page.on");
    // 再左滑一次 → 回直播页
    history.back();
    await wait(500);
    out.pageFinal = activePage();
    return out;
  });
  check("播放器打开时再压一层（回放页+播放器=2 层）",
        hist2.layerWithPlayer === 2 && hist2.playerOn === true, hist2);
  check("返回先关播放器，不退出应用",
        hist2.playerAfterBack === false && hist2.appStillAlive === true, hist2);
  check("关播放器后仍在回放页（不是直接回根）",
        hist2.pageAfterPlayerBack === "list", hist2);
  check("再返回一次才回到直播页", hist2.pageFinal === "live", hist2);

  await page.evaluate(async () => {
    await new Promise(r => setTimeout(r, 80));
  });

  await browser.close();
  srv.kill();
  fs.rmSync(varDir, { recursive: true, force: true });

  console.log(`\n结果：${pass} 项通过，${fails.length} 项失败`);
  if (fails.length) {
    console.log("FAILED: " + fails.join("；"));
    console.log("---- 服务日志 ----\n" + srvLog.slice(-2500));
  } else {
    console.log("ALL PASS");
  }
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error("E2E 崩溃：", e); process.exit(1); });
