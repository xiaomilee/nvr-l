/* 全屏视觉复核：截图存到 test/.shots，人工看一眼"是不是真的横过来了"。
   不是断言测试，跑完不参与 CI。 */
const { chromium } = require("playwright-core");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PY = "C:/Users/admin/.workbuddy/binaries/python/versions/3.13.12/python.exe";
const ROOT = path.resolve(__dirname, "..");
const PORT = 18793;
const OUT = path.join(__dirname, ".shots");

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const varDir = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-shot-"));
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
  const ctx = await browser.newContext({ viewport: { width: 420, height: 900 } });
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: "load" });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(OUT, "01-normal.png") });

  await page.evaluate(() => { toggleFull(); });
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(OUT, "02-fullscreen-rot.png") });

  await page.keyboard.press("Escape");
  await page.waitForTimeout(700);
  await page.screenshot({ path: path.join(OUT, "03-after-exit.png") });

  await browser.close();
  srv.kill();
  fs.rmSync(varDir, { recursive: true, force: true });
  console.log("shots ->", OUT);
})();
