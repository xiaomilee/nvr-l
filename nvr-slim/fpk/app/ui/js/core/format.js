/* ================= core/format.js =================
   纯函数：把秒数变成人看的时间与时长。没有 DOM、没有全局状态，
   所以谁都能用，也能被 node 测试直接喂数字。
   判断标准：函数体里出现 document / localStorage / fetch 的，不放这里。
   =================================================== */

/* ================= 工具 ================= */
const pad = n => String(n).padStart(2,"0");
function hms(sec){
  sec = Math.max(0, Math.floor(sec));
  return pad(Math.floor(sec/3600)) + ":" + pad(Math.floor(sec%3600/60)) + ":" + pad(sec%60);
}
function hm(sec){ return pad(Math.floor(sec/3600)) + ":" + pad(Math.floor(sec%3600/60)); }
function durText(sec){
  sec = Math.floor(sec);
  const h = Math.floor(sec/3600), m = Math.floor(sec%3600/60), s = sec%60;
  if(h) return h + " 时 " + m + " 分";
  if(m) return m + " 分 " + s + " 秒";
  return s + " 秒";
}
