/* 临时复现：用户实测回放页异常截图。起服务 + 造台账 + 播放 + 放大 ×5 截图。 */
const { chromium } = require("playwright-core");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PY = "C:/Users/admin/.workbuddy/binaries/python/versions/3.13.12/python.exe";
const ROOT = path.resolve(__dirname, "..");
const SERVER = path.join(ROOT, "app", "server", "nvr_slim.py");
const UI = path.join(ROOT, "app", "ui");
const PORT = 18793;

function d2(n) { return String(n).padStart(2, "0"); }

(async () => {
  const varDir = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-repro-"));
  const rec = path.join(varDir, "rec");
  const now = new Date();
  const today = `${now.getFullYear()}-${d2(now.getMonth() + 1)}-${d2(now.getDate())}`;
  fs.mkdirSync(path.join(rec, "院子", today), { recursive: true });
  // 8:00-11:06 全时录像分片（5 分钟一片）
  for (let s = 8 * 3600; s < 11 * 3600 + 6 * 60; s += 300) {
    const h = d2(Math.floor(s / 3600)), m = d2(Math.floor(s % 3600 / 60));
    fs.writeFileSync(path.join(rec, "院子", today, `${h}-${m}-00.mp4`), Buffer.alloc(2048));
  }
  // 台账：模仿用户截图——若干短事件 + 两个高密度长事件；sources 条目 = 分片判定结论
  const wins = [];
  const add = (h, m, sec, dur, dens) => wins.push([h * 3600 + m * 60 + sec, h * 3600 + m * 60 + sec + dur, dens]);
  add(10, 42, 15, 12, 0.05); add(10, 42, 40, 8, 0.04);
  add(10, 47, 19, 277, 0.35);
  add(10, 52, 8, 10, 0.05); add(10, 52, 30, 6, 0.04);
  add(10, 54, 38, 9, 0.06); add(10, 54, 50, 7, 0.05); add(10, 55, 2, 8, 0.04); add(10, 55, 20, 6, 0.04);
  add(11, 0, 56, 218, 0.4);
  const sources = { "10-45-00.mp4": { start: 10 * 3600 + 45 * 60, windows: wins.map(w => [w[0], w[1]]), d: 0.35 } };
  const ledger = { v: 1, cam: "院子", date: today, sources,
    updated_at: today + " 11:06:00" };
  fs.mkdirSync(path.join(rec, "_ledger", "院子"), { recursive: true });
  fs.writeFileSync(path.join(rec, "_ledger", "院子", today + ".json"), JSON.stringify(ledger));

  fs.writeFileSync(path.join(varDir, "config.json"), JSON.stringify({
    record_dir: rec, record_enable: false, record_segment_sec: 300,
    cameras: [{ id: "cam-01", name: "院子", url: "rtsp://u:p@127.0.0.1:1/s1",
                sub_url: "", enabled: true, full_days: 4, clip_days: 90,
                segment_sec: 0, masks: { ignore: [] } }]
  }));

  const srv = spawn(PY, [SERVER, "--port", String(PORT), "--var", varDir,
    "--app", varDir, "--ui", UI, "--host", "127.0.0.1"],
    { env: { ...process.env, NVR_SIMULATE_GPU: "qsv" }, stdio: "pipe" });
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 250));
    try { const r = await fetch(`http://127.0.0.1:${PORT}/api/health`); if (r.ok) break; } catch (e) {}
  }
  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe", headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(String(e)));
  await page.goto(`http://127.0.0.1:${PORT}/`);
  await page.waitForTimeout(800);
  if (errs.length) console.log("加载期 pageerrors:", errs);
  await page.click('.tb[data-page="play"]');
  await page.waitForTimeout(800);
  // 模拟用户：点事件跳播开始回放，然后放大到 ×5
  // 模拟用户：点事件跳播开始回放（fixture 假视频会触发 ended→pvStop，先暂停定格）
  await page.evaluate(() => { pvPlayAt(10 * 3600 + 45 * 60); try{ pvCur.pause(); }catch(e){} });
  await page.waitForTimeout(800);
  await page.evaluate(() => {
    window.__trace = [];
    const _rv = renderVtl;
    renderVtl = function(d, day, a, y){
      _rv(d, day, a, y);
      const sc = document.getElementById("vtlScroll");
      window.__trace.push({ on: PPLAY.on, pxh: Math.round(vtlPxh), st: Math.round(sc.scrollTop) });
    };
  });
  for (let k = 0; k < 8; k++) { await page.click("#btnVtlIn").catch(() => {}); await page.waitForTimeout(80); }
  console.log("renderVtl trace:", JSON.stringify(await page.evaluate(() => window.__trace)));
  console.log("daybtn:", JSON.stringify(await page.evaluate(() => ({
    name: document.getElementById("playDayName").textContent,
    daySel: state.daySel, dayList: (state.dayList || []).slice(-3),
    btnW: document.getElementById("playDaySel").getBoundingClientRect().width,
    html: document.getElementById("playDaySel").innerHTML.slice(0, 120),
  }))));
  await page.waitForTimeout(500);
  fs.mkdirSync(__dirname + "/_repro", { recursive: true });
  await page.screenshot({ path: __dirname + "/_repro/play_full.png", fullPage: false });
  // 点一根可视区内的横线（vmark）验证标注气囊
  const mark = await page.evaluate(() => {
    const sc = document.getElementById("vtlScroll").getBoundingClientRect();
    const ok = [...document.querySelectorAll("#vtl .vmark")].filter(m => {
      const r = m.getBoundingClientRect();
      return r.y > sc.y + 10 && r.y < sc.y + sc.height - 10;
    });
    if (!ok.length) {
      return { dbg: true,
        n: document.querySelectorAll("#vtl .vmark").length,
        scrollTop: document.getElementById("vtlScroll").scrollTop,
        pplay: { on: PPLAY.on, t: PPLAY.t, day: PPLAY.day, vtlDay: vtlDay, camId: PPLAY.camId, camId2: state.camId,
                 dayEq: PPLAY.day === vtlDay, nowText: (document.querySelector("#vtl .vtlnow em")||{}).textContent },
        nowSecHint: (d => d)(""),
        tops: [...document.querySelectorAll("#vtl .vmark")].map(m => ({
          y: Math.round(m.getBoundingClientRect().y), top: m.style.top })),
        scY: Math.round(sc.y), scH: Math.round(sc.height) };
    }
    const r = ok[0].getBoundingClientRect();
    return { x: r.x + 20, y: r.y + 8, n: document.querySelectorAll("#vtl .vmark").length };
  });
  if (mark && !mark.dbg) {
    await page.mouse.click(mark.x, mark.y);
    await page.waitForTimeout(300);
    const bub = await page.evaluate(() => {
      const b = document.querySelector("#vtl .vbubble");
      return b ? { text: b.textContent.slice(0, 80), top: b.style.top } : null;
    });
    console.log("vmark 数量:", mark.n, "气囊:", JSON.stringify(bub));
    await page.screenshot({ path: __dirname + "/_repro/play_bubble.png", fullPage: false });
  } else {
    console.log("无可视 vmark！", JSON.stringify(mark));
  }
  // 滚动容器状态
  const lay = await page.evaluate(() => {
    const r = id => (b => ({ w: Math.round(b.width), h: Math.round(b.height) }))(document.getElementById(id).getBoundingClientRect());
    return { stage: r("playStage"), vtlScroll: r("vtlScroll"),
      hdr: (b => ({ w: Math.round(b.width), h: Math.round(b.height) }))(document.querySelector(".hdr")),
      listheadGone: !document.getElementById("playEvCount") };
  });
  console.log("布局:", JSON.stringify(lay));
  const flow = await page.evaluate(() => {
    const g = id => { const b = document.getElementById(id).getBoundingClientRect();
      return { top: Math.round(b.top), bottom: Math.round(b.bottom), h: Math.round(b.height) }; };
    const pb = document.querySelector("#page-play .pops").getBoundingClientRect();
    const cs = getComputedStyle(document.getElementById("vtlCard"));
    const pg = document.getElementById("page-play");
    return { hdr: g("page-play") && Math.round(document.querySelector("#page-play .hdr").getBoundingClientRect().bottom),
      stage: g("playStage"), pbar: { top: Math.round(pb.top), bottom: Math.round(pb.bottom), h: Math.round(pb.height) },
      card: g("vtlCard"), pos: cs.position, mt: cs.marginTop,
      pageH: Math.round(pg.getBoundingClientRect().height),
      cls: pg.className };
  });
  console.log("flow:", JSON.stringify(flow));
  const st = await page.evaluate(() => {
    const s = document.getElementById("playStage");
    const r = s.getBoundingClientRect();
    const vids = [...s.querySelectorAll("video")].map(v => ({
      cls: v.className, w: v.videoWidth, h: v.videoHeight,
      ready: v.readyState, cur: (v.currentSrc || v.src || "").split("/").pop(),
      rect: (b => ({ x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }))(v.getBoundingClientRect()),
    }));
    const big = document.getElementById("playBig");
    return { stage: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
             bigOff: big.classList.contains("off"), vids, errs: window.__e || null };
  });
  console.log(JSON.stringify(st, null, 1));
  // 活动筛选模式：时间轴变淡 + 活动时段块点亮 + 播放跳静止
  await page.evaluate(() => {
    document.querySelector('[data-pvfilter="act"]').click();
    pvPlayAt(10 * 3600 + 4 * 60, { quiet: true });   // 落在静止段 → 应自动跳 10:07:16
    pvSkipStatic();
    try{ pvCur.pause(); }catch(e){}
  });
  await page.waitForTimeout(500);
  const act = await page.evaluate(() => ({ t: PPLAY.t, filter: PPLAY.filter,
    dim: document.getElementById("vtlCard").classList.contains("actmode") }));
  console.log("活动筛选:", JSON.stringify(act), "（期望 t≈38436 = 10:07:16）");
  await page.screenshot({ path: __dirname + "/_repro/play_act.png", fullPage: false });
  await page.evaluate(() => {
    document.querySelector('[data-pvfilter="full"]').click();
    pvStop();
  });
  // 全屏悬浮态：headless 进不了真全屏，直接驱动横屏态验证 CSS（暂停 = 悬浮层可见）
  await page.evaluate(() => { pvStop(); setLand(true); applyRotate(true); pvSyncCtl(); syncVnowline(); });
  await page.waitForTimeout(400);
  await page.screenshot({ path: __dirname + "/_repro/play_fs.png", fullPage: false });
  await page.evaluate(() => { setLand(false); });
  console.log("pageerrors:", errs);
  await browser.close();
  srv.kill();
})().catch(e => { console.error(e); process.exit(1); });
