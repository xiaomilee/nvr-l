/* ================= core/theme.js =================
   明暗主题引擎：跟随系统 / 固定浅色 / 固定深色，桌面壳可用 postMessage 强制。
   为什么单独成文件：它是**唯一**改 documentElement.dataset.theme 的地方，
   连同 app/ui/css/theme.css 的 :root 令牌，构成全部主题逻辑 ——
   排查"颜色不对"只需看这两处，不必再在八千行里搜 dataset.theme。
   ================================================== */

/* ================= 明暗主题 =================
   auto    : 跟随系统 prefers-color-scheme（飞牛桌面明暗）
   light   : 固定浅色
   dark    : 固定深色
   桌面壳如果通过 postMessage 通知主题（{fnosTheme:"dark"}），优先级最高。 */
const THEME = {
  mode: localStorage.getItem("nvr.theme") || "auto",
  forced: null,
  mq: window.matchMedia("(prefers-color-scheme: dark)")
};
function applyTheme(){
  const t = THEME.forced || (THEME.mode === "dark"
    || (THEME.mode === "auto" && THEME.mq.matches) ? "dark" : "light");
  document.documentElement.dataset.theme = t;
  const meta = document.querySelector('meta[name="theme-color"]');
  if(meta) meta.setAttribute("content", t === "dark" ? "#14161b" : "#ffffff");
  document.querySelectorAll("[data-theme-set]").forEach(el=>
    el.classList.toggle("on", el.dataset.themeSet === THEME.mode));
}
if(THEME.mq.addEventListener) THEME.mq.addEventListener("change", applyTheme);
else if(THEME.mq.addListener) THEME.mq.addListener(applyTheme);
window.addEventListener("message", e=>{
  const t = e && e.data && (e.data.fnosTheme || e.data.theme);
  if(t === "dark" || t === "light"){ THEME.forced = t; applyTheme(); }
});
document.addEventListener("click", e=>{
  const b = e.target.closest("[data-theme-set]");
  if(!b) return;
  THEME.mode = b.dataset.themeSet;
  THEME.forced = null;
  localStorage.setItem("nvr.theme", THEME.mode);
  applyTheme();
  toast("toast2", THEME.mode === "auto" ? "外观跟随系统"
    : THEME.mode === "dark" ? "已切换深色" : "已切换浅色");
});
(()=>{ const q = new URLSearchParams(location.search).get("theme");
  if(q === "dark" || q === "light") THEME.mode = q;
  applyTheme(); })();
