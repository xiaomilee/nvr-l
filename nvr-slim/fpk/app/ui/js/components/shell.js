/* ================= components/shell.js =================
   应用外壳：一条 toast、桌面/手机两种版式的布局，以及窗口尺寸变化监听器。
   放这里的判断标准：**不属于任何一页，但每页都看得见** —— 它管的是 #phone
   这个壳子和全局提示，不关心页面里正在显示什么。
   layout() 会调 isFullscreen()/syncRail()（声明在 components/fullscreen.js），
   靠的是函数声明提升，不是依赖倒置；别把这个方向反过来理解。
   ======================================================== */

function toast(el, text){
  const t = document.getElementById(el);
  t.textContent = text;
  t.classList.add("on");
  clearTimeout(t._h);
  t._h = setTimeout(()=>t.classList.remove("on"), 1600);
}

/* ================= 布局 ================= */
const phone = document.getElementById("phone");
function layout(){
  /* 全屏/旋转状态下的尺寸完全交给 CSS（body.fsmode / body.rot）：
     这里再写 width/transform 会把它顶掉，桌面端会缩成一小块。 */
  if(state.land || state.rot || isFullscreen()){
    document.body.classList.toggle("desktop", false);
    syncRail();          /* 全屏/旋转一变，黑边宽度也变，工具轨跟着重算 */
    return;
  }
  syncRail();
  const desktop = window.innerWidth >= 760 && window.innerHeight >= 700;
  document.body.classList.toggle("desktop", desktop);
  if(!desktop){
    phone.style.width = "";
    phone.style.height = "";
    phone.style.transform = "";
    return;
  }
  phone.style.width = "390px";
  phone.style.height = "844px";
  const s = Math.min(1, (window.innerHeight - 40) / (844 + 24), (window.innerWidth - 40) / (390 + 24));
  phone.style.transform = "scale(" + s.toFixed(3) + ")";
}
