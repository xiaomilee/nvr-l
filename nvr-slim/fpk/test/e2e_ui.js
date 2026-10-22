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
    return {
      fsmode: document.body.classList.contains("fsmode"),
      exitVisible: !!b && b.classList.contains("on")
                   && getComputedStyle(b).display !== "none",
      fsEl: !!(document.fullscreenElement || document.webkitFullscreenElement),
    };
  });
  check("全屏后进入 fsmode（容器铺满视口）", fsState.fsmode, fsState);
  check("全屏后右上角出现退出按钮", fsState.exitVisible, fsState);

  const exitBox = await page.$("#fsExit");
  if (exitBox) {
    const bb = await exitBox.boundingBox();
    check("退出按钮在右上角且可点",
          !!bb && bb.width > 0 && bb.height > 0 && bb.x + bb.width > 300 && bb.y < 120, bb);
    await exitBox.click();
    await page.waitForTimeout(600);
    const after = await page.evaluate(() => ({
      fsmode: document.body.classList.contains("fsmode"),
      land: !!state.land, rot: !!state.rot,
      exitOn: (document.getElementById("fsExit") || {}).className || "",
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
    // 按钮自身（内部文字）反向转 90°，否则跟着画面一起躺倒看不懂
    const inner = await page.evaluate(() => {
      const sp = document.querySelector("#fsExit span");
      return { tf: sp ? getComputedStyle(sp).transform : "none",
               txt: sp ? sp.textContent : "" };
    });
    check("旋转态下按钮文字反向旋转（不躺倒）",
          /matrix\(0,\s*1,\s*-1,\s*0/.test(inner.tf) && /退出/.test(inner.txt), inner);
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
