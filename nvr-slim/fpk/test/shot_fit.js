/* 全屏「画幅」复核：用 16:9 的探针图替身视频（本机没 ffmpeg，造不出真 mp4），
   量几何 + 截图，确认竖屏/横屏两种全屏都不裁剪、且画面真的延伸到底部操作条下面。
   不是断言测试，跑完不参与 CI。 */
const { chromium } = require("playwright-core");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PY = "C:/Users/admin/.workbuddy/binaries/python/versions/3.13.12/python.exe";
const ROOT = path.resolve(__dirname, "..");
const PORT = 18794;
const OUT = path.join(__dirname, ".shots");

/* 16:9 探针图：四角标记 + 4x3 网格，任何裁切都能一眼看出 */
const PROBE = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900">
<rect width="1600" height="900" fill="#1b3a2a"/>
${[0,1,2,3].map(i=>`<rect x="${i*400}" y="0" width="1" height="900" fill="#7ce0a0"/>`).join("")}
${[0,1,2].map(i=>`<rect x="0" y="${i*300}" width="1600" height="1" fill="#7ce0a0"/>`).join("")}
<rect x="0" y="0" width="1600" height="900" fill="none" stroke="#ffd54a" stroke-width="14"/>
<circle cx="60" cy="60" r="34" fill="#ff5a5a"/><circle cx="1540" cy="60" r="34" fill="#ff5a5a"/>
<circle cx="60" cy="840" r="34" fill="#ff5a5a"/><circle cx="1540" cy="840" r="34" fill="#ff5a5a"/>
<text x="800" y="480" font-size="96" fill="#ffffff" text-anchor="middle" font-family="sans-serif">16:9 FULL FRAME</text>
<text x="800" y="600" font-size="52" fill="#ffd54a" text-anchor="middle" font-family="sans-serif">四角标记必须都在 = 没裁切</text>
</svg>`);

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const varDir = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-fit-"));
  fs.mkdirSync(path.join(varDir, "rec"), { recursive: true });
  fs.writeFileSync(path.join(varDir, "config.json"), JSON.stringify({
    record_dir: path.join(varDir, "rec"), record_enable: false, record_segment_sec: 300,
    cameras: [{ id: "cam-01", name: "车库", url: "rtsp://127.0.0.1:1/s1",
                sub_url: "", enabled: true, full_days: 4, clip_days: 90 }],
  }));

  const srv = spawn(PY, [path.join(ROOT, "app", "server", "nvr_slim.py"),
    "--port", String(PORT), "--var", varDir, "--app", varDir,
    "--ui", path.join(ROOT, "app", "ui"), "--host", "127.0.0.1"],
    { env: { ...process.env, NVR_SIMULATE_GPU: "qsv" }, stdio: "ignore" });

  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 250));
    try { const r = await fetch(`http://127.0.0.1:${PORT}/api/health`); if (r.ok) break; } catch (e) {}
  }

  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });
  /* 竖屏手机视口：和真机截图同尺寸 */
  const ctx = await browser.newContext({ viewport: { width: 420, height: 900 }, hasTouch: true, isMobile: true });
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  page.on("pageerror", e => console.log("  [pageerror]", e.message));
  await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: "load" });
  await page.waitForTimeout(1200);

  /* 进回放页，塞探针图（几何规则与 video 完全一致：inset:0 + object-fit:contain） */
  await page.evaluate((src) => {
    goPage("play");
    const st = document.getElementById("playStage");
    let img = document.getElementById("probeImg");
    if (!img) {
      img = document.createElement("img");
      img.id = "probeImg";
      img.src = src;
      img.style.cssText = "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;z-index:0";
      st.appendChild(img);
    }
    document.querySelectorAll("#playStage video").forEach(v => v.style.opacity = "0");
  }, PROBE);
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, "fit-00-normal.png") });

  const geo = async (tag) => await page.evaluate((tag) => {
    const st = document.getElementById("playStage");
    const img = document.getElementById("probeImg");
    const pops = document.querySelector("#page-play .pops");
    const pg = document.getElementById("page-play");
    const cs = getComputedStyle(img);
    /* 探针图实际绘制尺寸（contain 后） */
    const boxW = st.clientWidth, boxH = st.clientHeight;
    const ar = 16 / 9;
    let dw = boxW, dh = boxW / ar;
    if (dh > boxH) { dh = boxH; dw = boxH * ar; }
    const tools = document.getElementById("fsTools");
    return { tag, fit: cs.objectFit,
      stage: [st.offsetWidth, st.offsetHeight],
      page: [pg.offsetWidth, pg.offsetHeight],
      drawn: [Math.round(dw), Math.round(dh)],
      bars: [Math.round((boxW - dw) / 2), Math.round((boxH - dh) / 2)],
      rail: document.body.classList.contains("rail"),
      railw: getComputedStyle(document.documentElement).getPropertyValue("--railw").trim(),
      popsBox: [pops.offsetLeft, pops.offsetTop, pops.offsetWidth, pops.offsetHeight],
      toolsBox: tools ? [tools.offsetLeft, tools.offsetTop, tools.offsetWidth, tools.offsetHeight] : null,
      popsPos: getComputedStyle(pops).position,
      popsBottomGap: pg.offsetHeight - (pops.offsetTop + pops.offsetHeight),
      rot: document.body.classList.contains("rot"),
      vp: [innerWidth, innerHeight] };
  }, tag);

  const portrait = await geo("portrait");
  console.log("竖屏全屏:", JSON.stringify(portrait));

  /* 进全屏（竖屏：不旋转，先看「未旋转」的铺满情况） */
  await page.evaluate(() => { enterFull(null, { noExit: false }); });
  await page.waitForTimeout(900);
  await page.evaluate(() => { applyRotate(false); setRotPref("off"); });
  await page.waitForTimeout(500);
  const fsP = await geo("fs-portrait");
  console.log("全屏竖屏:", JSON.stringify(fsP));
  await page.screenshot({ path: path.join(OUT, "fit-01-fs-portrait.png") });

  /* 切旋转（真机截图里那一屏） */
  await page.evaluate(() => { applyRotate(true); setRotPref("on"); });
  await page.waitForTimeout(600);
  const fsR = await geo("fs-rot");
  console.log("全屏旋转:", JSON.stringify(fsR));
  await page.screenshot({ path: path.join(OUT, "fit-02-fs-rot.png") });

  /* 旋转态时间轴能否拖动：用 CDP 的真触摸事件（不是 JS 合成事件），
     走完整手势链路，这才是真机上"拖不动"的复现路径 */
  const client = await page.context().newCDPSession(page);
  const dragRes = await (async () => {
    const box = await page.evaluate(() => {
      const sc = document.getElementById("vtlScroll");
      sc.scrollTop = 0;
      const r = sc.getBoundingClientRect();      /* 旋转态 bbox 已换轴，正好是可点坐标 */
      return { cx: Math.round(r.left + r.width / 2), cy: Math.round(r.top + r.height / 2),
               max: sc.scrollHeight - sc.clientHeight };
    });
    const send = (type, x) => client.send("Input.dispatchTouchEvent", {
      type, touchPoints: type === "touchEnd" ? [] : [{ x, y: box.cy }] });
    await send("touchStart", box.cx - 40);
    for (let i = 1; i <= 8; i++) { await send("touchMove", box.cx - 40 + i * 10); await page.waitForTimeout(20); }
    await send("touchEnd", box.cx + 40);
    await page.waitForTimeout(120);
    const after = await page.evaluate(() => document.getElementById("vtlScroll").scrollTop);
    return { maxScroll: box.max, after, touchAction:
      await page.evaluate(() => getComputedStyle(document.getElementById("vtlScroll")).touchAction) };
  })();
  console.log("旋转态拖动时间轴:", JSON.stringify(dragRes));


  /* 旋转成横向视口再看一眼（真机系统级横屏的样子） */
  await page.setViewportSize({ width: 900, height: 420 });
  await page.waitForTimeout(300);
  await page.evaluate(() => { applyRotate(false); });
  await page.waitForTimeout(500);
  const fsL = await geo("fs-land-real");
  console.log("真实横屏视口:", JSON.stringify(fsL));
  await page.screenshot({ path: path.join(OUT, "fit-03-land-real.png") });

  await browser.close();
  srv.kill();
  fs.rmSync(varDir, { recursive: true, force: true });
  console.log("shots ->", OUT);
})();
