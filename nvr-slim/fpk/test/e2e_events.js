/* 事件页端到端验收（0.9.121）：真开 Chrome 点一遍"从事件到画面"。
   ------------------------------------------------------------------
   为什么不进 build.sh --check：它要一台**真在录的**机器（测试机 152），
   而构建闸门必须能在没有摄像头、没有网络的机器上跑完。所以它是手动跑的一档，
   与 test/e2e_ui.js 同一层。

   它验的是验收条件本身，不是"渲染对不对"：
     - 点一条真实事件后多久开始播（验收口径 ≤ 2000ms）
     - 播放起点是否落在事件发生前 10 秒内
     - 展开卡片的三个动作（前 30 秒 / 打开回放 / 后 30 秒）在不在
     - 全程无 JS 报错

   跑法（依赖 playwright-core + 本机 Chrome，用系统装的那个，不下载浏览器）：
     npm i playwright-core
     NVR_URL=http://192.168.2.152:18085 NVR_TOKEN=<secret.json 里的 api_token>        NVR_SHOT=/tmp node test/e2e_events.js
*/
const { chromium } = require("playwright-core");

const URL = process.env.NVR_URL;
const TOKEN = process.env.NVR_TOKEN;
const OUT = process.env.NVR_SHOT || ".";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";

let pass = 0;
const fails = [];
function ck(name, cond, detail) {
  if (cond) { pass++; console.log("  OK   " + name); }
  else { fails.push(name); console.log("  FAIL " + name + "  ← " + JSON.stringify(detail)); }
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 },
                                         deviceScaleFactor: 2 });
  /* 先把凭证塞进 localStorage：省掉引导层那一步，测的是事件页不是登录页 */
  await ctx.addInitScript(t => {
    try { localStorage.setItem("nvr_api_token", t); } catch (e) {}
  }, TOKEN);
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", e => errs.push(String(e)));
  page.on("console", m => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await page.goto(URL + "/", { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1200);

  /* 1) 底部导航有「事件」且点得进去 */
  const tab = await page.$('.tb[data-page="events"]');
  ck("底部导航有事件入口", !!tab);
  await tab.click();
  await page.waitForTimeout(1500);
  ck("事件页已激活", await page.$eval("#page-events", e => e.classList.contains("on")));

  const head = await page.$eval("#evSum", e => e.textContent.trim());
  ck("页头给出条数（不是 —）", /\d+\s*条/.test(head), head);
  console.log("     头部：" + head);

  /* 2) 三段筛选条都画出来了 */
  const chips = await page.evaluate(() => ({
    day: document.querySelectorAll("#evDayBar [data-evday]").length,
    cam: document.querySelectorAll("#evCamBar [data-evname]").length,
    cat: document.querySelectorAll("#evCatBar [data-evcat]").length,
  }));
  ck("日期条有近 7 天", chips.day === 7, chips);
  ck("机位条有「全部 + 每路」", chips.cam >= 3, chips);
  ck("类别条有 4 项", chips.cat === 4, chips);

  const cards = await page.$$eval(".evcard", els => els.map(e => ({
    id: e.getAttribute("data-ev"),
    text: e.innerText.replace(/\s+/g, " ").slice(0, 80),
  })));
  ck("列表里有真实事件卡片", cards.length > 0, cards.length);
  if (!cards.length) { await browser.close(); process.exit(1); }
  console.log("     首条：" + cards[0].text);

  /* 带 AI 类别的优先（人员/车辆/两轮车才是这一批要验的那条路） */
  const ai = cards.find(c => /人员|车辆|两轮车/.test(c.text));
  const target = ai || cards[cards.length - 1];
  ck("有带类别的真实事件（AI 路径）", !!ai, cards.map(c => c.text));

  await page.screenshot({ path: OUT + "/ev_list.png" });

  /* 3) 点一条 → 量到画面真的开始动为止 */
  const t0 = Date.now();
  await page.click(`.evcard[data-ev="${target.id}"]`);
  let played = false, cost = 0, info = null;
  try {
    await page.waitForFunction(() => {
      const vs = [document.getElementById("playVidA"), document.getElementById("playVidB")];
      const v = vs.find(x => x && x.classList.contains("on")) || vs[0];
      return v && !v.paused && v.readyState >= 2 && v.currentTime > 0.15;
    }, null, { timeout: 15000 });
    played = true;
    cost = Date.now() - t0;
    info = await page.evaluate(() => {
      const v = [document.getElementById("playVidA"), document.getElementById("playVidB")]
        .find(x => x && x.classList.contains("on"));
      return { t: PPLAY.t, day: PPLAY.day, cam: PPLAY.camId,
               on: PPLAY.on, url: v ? v.getAttribute("src") : null,
               cur: v ? v.currentTime : null };
    });
  } catch (e) {
    cost = Date.now() - t0;
  }
  ck("点事件后画面真的在播", played, { cost, info });
  ck("2 秒内开始播（验收口径）", played && cost <= 2000, cost);
  console.log("     起播耗时：" + cost + " ms");

  if (played) {
    const ev = await page.evaluate(async id => {
      const r = await fetch("/api/events/" + id, { headers: { Authorization: "Bearer " + localStorage.getItem("nvr_api_token") } });
      return (await r.json()).event;
    }, target.id);
    const delta = info.t - ev.start;
    ck("落点在这一天", info.day === ev.date, { info, ev: ev.date });
    ck("落点在这一路", info.cam === ev.cam, { info, ev: ev.cam });
    ck("起点在事件发生前 10 秒内（验收口径）", delta <= 0 && delta >= -10.5, delta);
    console.log("     落点 " + info.t + " / 事件 " + ev.start + " → " + delta.toFixed(1) + " 秒");
    await page.screenshot({ path: OUT + "/ev_play.png" });
  }

  /* 4) 回到事件页，展开卡片的 ±30 秒按钮在 */
  await page.click('.tb[data-page="events"]');
  await page.waitForTimeout(800);
  const acts = await page.$$eval(".evcard .evacts .evbtn", els => els.map(e => e.innerText.trim()));
  ck("展开的卡片有计划中的三个动作", acts.length >= 3, acts);
  console.log("     动作：" + acts.join(" | "));
  await page.screenshot({ path: OUT + "/ev_actions.png" });

  ck("页面无 JS 报错", errs.length === 0, errs.slice(0, 4));

  await browser.close();
  console.log((fails.length ? "FAIL" : "OK") + "  e2e  " + pass + " 通过 / " + fails.length + " 失败");
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error("E2E 崩了：" + e.message); process.exit(2); });
