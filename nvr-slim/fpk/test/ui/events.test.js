/* 事件页：按"事情"找录像（0.9.121）
   ------------------------------------------------------------------
   守四件事：

   1. **定位不许自己编**：入点 = 服务端给的分片名 + 偏移。分片名解析不出来就
      返回 null，让上层说"拿不到定位"，绝不回落成 0 —— 从 0 秒开始播会让用户
      以为"这个事件在半夜"。
   2. **播不了要说清**：missing_reason 有五种（未封口 / 分片不在 / 空文件 /
      结构坏 / 那时没录），文案由服务端给，这里只负责不被吞掉。
   3. **±30 秒的基准是画面，不是条目**：用户按"后 30 秒"时看的是播放器停下的
      那一秒；只有还没在这条事件的片上播过，才退回事件前 10 秒的入点。
   4. **跨页交代清楚**：哪一路、哪一天、从哪一秒。少交代一天就会"点了 A 事件、
      播的却是 B 那天的同一秒"。

   跑法： node test/ui/events.test.js */
const { els, mkEl } = require("./harness.js");
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(ROOT, "app", "ui", "index.html"), "utf8")
             .replace(/\r\n/g, "\n");
const blocks = src.match(/<script>([\s\S]*?)<\/script>/g);
const code = blocks[blocks.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");
const ctx = vm.createContext(global);
vm.runInContext(code, ctx, { filename: "index.html" });
const run = s => vm.runInContext(s, ctx);
const J = v => JSON.stringify(v);

let pass = 0, fail = 0;
function ck(name, cond){
  if(cond) pass++;
  else { fail++; console.log("  FAIL " + name); }
}

/* ---- 结构守卫：页面与入口必须同时存在 ----
   这条盯着一种真实的复发形态：页面上线了、底部导航忘了加（或反过来），
   结果是"事件页写好了但用户点不到"。 */
ck("底部导航有事件入口", src.indexOf('data-page="events"') > 0);
ck("有事件页容器", src.indexOf('id="page-events"') > 0);
ck("切页时会去读事件", src.indexOf('if(page === "events")') > 0);

/* ---- 分片名 → 钟点秒 ---- */
ck("10-23-41.mp4 = 37421", run('evSegSec("10-23-41.mp4")') === 37421);
ck("00-00-00.mp4 = 0", run('evSegSec("00-00-00.mp4")') === 0);
ck("23-59-59.mp4 = 86399", run('evSegSec("23-59-59.mp4")') === 86399);
ck("冒号形式不认（分片名用短横线）", run('evSegSec("10:23:41.mp4")') === null);
ck("归档名 .cNN.mp4 不认", run('evSegSec("10-23-41.c01.mp4")') === null);
ck("空串返回 null", run('evSegSec("")') === null);
ck("null 返回 null", run('evSegSec(null)') === null);

/* ---- clip → 入点 ---- */
ck("偏移 0 = 片首", run('evClipStart({segment:"10-23-41.mp4", offset_sec:0})') === 37421);
ck("偏移 10 秒", run('evClipStart({segment:"10-23-41.mp4", offset_sec:10})') === 37431);
ck("偏移是字符串也认", run('evClipStart({segment:"10-23-41.mp4", offset_sec:"10"})') === 37431);
ck("负偏移归零", run('evClipStart({segment:"10-23-41.mp4", offset_sec:-5})') === 37421);
ck("没有 segment 返回 null（不猜 0）", run('evClipStart({segment:null, offset_sec:10})') === null);
ck("clip 为 null 返回 null", run('evClipStart(null)') === null);

/* ---- 类别文案与配色 ---- */
ck("person → 人员", run('evCatText("person")') === "人员");
ck("vehicle → 车辆", run('evCatText("vehicle")') === "车辆");
ck("two_wheeler → 两轮车", run('evCatText("two_wheeler")') === "两轮车");
ck("不认识的类别原样返回", run('evCatText("dog")') === "dog");
ck("person 用主色", run('evCatCls("person")') === "person");
ck("two_wheeler 并入车辆色", run('evCatCls("two_wheeler")') === "vehicle");
ck("未知类别落移动侦测", run('evCatCls("dog")') === "motion");

/* ---- 主类别：人 > 车 > 移动 ---- */
ck("人车同在取人", run('evMainCat({categories:["vehicle","person"]})') === "person");
ck("只有两轮车取车辆", run('evMainCat({categories:["two_wheeler"]})') === "vehicle");
ck("只有车辆取车辆", run('evMainCat({categories:["vehicle"]})') === "vehicle");
ck("没有类别取移动侦测", run('evMainCat({categories:[]})') === "motion");
ck("脏数据不抛", run('evMainCat({})') === "motion");

/* ---- 卡片 ---- */
const EV1 = {id:"c1_20261108_102341", cam:"c1", date:"2026-11-08",
             start:37421, end:37443, duration:22, categories:["person"],
             confidence_max:0.82};
ck("卡片显示钟点", run('evCardHtml(' + J(EV1) + ')').indexOf("10:23:41") > 0);
ck("卡片显示时长", run('evCardHtml(' + J(EV1) + ')').indexOf("22 秒") > 0);
ck("卡片显示类别徽标", run('evCardHtml(' + J(EV1) + ')').indexOf("人员") > 0);
ck("卡片带事件 id 供点击", run('evCardHtml(' + J(EV1) + ')').indexOf(EV1.id) > 0);
ck("置信度按两位显示", run('evCardHtml(' + J(EV1) + ')').indexOf("置信度 0.82") > 0);

const EV2 = Object.assign({}, EV1, {confidence_max:null, categories:[]});
ck("没有置信度就不写（不编一个默认值）",
   run('evCardHtml(' + J(EV2) + ')').indexOf("置信度") < 0);
ck("没有类别时标移动侦测",
   run('evCardHtml(' + J(EV2) + ')').indexOf("移动侦测") > 0);
ck("收起状态没有操作按钮",
   run('EV.open=""; evCardHtml(' + J(EV1) + ')').indexOf("前 30 秒") < 0);

/* ---- 展开后的操作区 ---- */
ck("展开后有三个动作",
   run('EV.clipErr=""; EV.clip={ok:true,segment:"10-23-41.mp4",offset_sec:0}; evOpenHtml(' + J(EV1) + ')')
     .indexOf("data-evgo=\"30\"") > 0);
ck("未封口：文案与代码都摆出来",
   run('EV.clipErr=""; EV.clip={missing_reason:"not_sealed",missing_text:"事件所在分片还在写入（尚未封口），稍后再看"};'
     + ' evOpenHtml(' + J(EV1) + ')')
     .indexOf("尚未封口") > 0);
ck("缺失原因带机器可读 code",
   run('EV.clipErr=""; EV.clip={missing_reason:"segment_missing",missing_text:"x"};'
     + ' evOpenHtml(' + J(EV1) + ')').indexOf("segment_missing") > 0);
ck("前摇被截断要说明",
   run('EV.clipErr=""; EV.clip={ok:true,clamped:true,segment:"10-23-41.mp4",offset_sec:0};'
     + ' evOpenHtml(' + J(EV1) + ')').indexOf("片头截断") > 0);
ck("定位读失败要说读不到定位",
   run('EV.clip=null; EV.clipErr="boom"; evOpenHtml(' + J(EV1) + ')').indexOf("读不到定位") > 0);

/* 0.9.132 事件详情：快照与回放拆成两段（用户原话：先快速确认，再决定打开视频） */
ck("主按钮是「打开录像」且写明大约时长",
   run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EV1) + ')')
     .indexOf('▶ 打开录像（约 22 秒）') > 0);

/* 0.9.132 弱网：明确的失败态 + 加载骨架（慢网里一行字干等像卡死） */
run('EV.err="网络超时"; EV.data=null; EV.loading=false; renderEvList();');
ck("读取失败有「重试」钮与提示",
   els["evList"]._html.indexOf('data-evretry="1"') > 0
   && els["evList"]._html.indexOf("读取失败") > 0);
run('EV.err=""; EV.data=null; EV.loading=true; renderEvList();');
ck("加载中给骨架而不是一行字干等",
   els["evList"]._html.indexOf('class="evsk"') > 0);

/* 0.9.132 机位筛选弹层（从横滑 chips 改为底部弹层 + 2 列网格） */
ck("机位弹层含「全部机位」与每路按钮",
   run('EV.cams=[]; evCamPopHtml()').indexOf("全部机位") > 0
   && run('EV.cams=[]; evCamPopHtml()').indexOf('data-evname="cam-01"') > 0);
ck("机位弹层按 EV.cams 标出已选",
   run('EV.cams=["cam-01"]; evCamPopHtml()').indexOf('camb on') > 0);

/* ---- ±30 秒的基准 ---- */
ck("没播过这条事件：基准是入点",
   run('EV.focus="x"; PPLAY.segIdx=-1; evPlayPoint(' + J(EV1)
     + ',{segment:"10-23-41.mp4",offset_sec:10},0)') === 37431);
ck("前 30 秒",
   run('EV.focus="x"; PPLAY.segIdx=-1; evPlayPoint(' + J(EV1)
     + ',{segment:"10-23-41.mp4",offset_sec:10},-30)') === 37401);
ck("后 30 秒",
   run('EV.focus="x"; PPLAY.segIdx=-1; evPlayPoint(' + J(EV1)
     + ',{segment:"10-23-41.mp4",offset_sec:10},30)') === 37461);
ck("正在播这条事件：基准是画面停下的位置",
   run('EV.focus="' + EV1.id + '"; PPLAY.segIdx=3; PPLAY.day="2026-11-08";'
     + ' PPLAY.camId="c1"; PPLAY.t=37500; evPlayPoint(' + J(EV1)
     + ',{segment:"10-23-41.mp4",offset_sec:10},0)') === 37500);
ck("换过片（day 不同）就退回入点",
   run('EV.focus="' + EV1.id + '"; PPLAY.segIdx=3; PPLAY.day="2026-11-07";'
     + ' PPLAY.camId="c1"; PPLAY.t=37500; evPlayPoint(' + J(EV1)
     + ',{segment:"10-23-41.mp4",offset_sec:10},0)') === 37431);
ck("前 30 秒不会落到负数",
   run('EV.focus="' + EV1.id + '"; PPLAY.segIdx=3; PPLAY.day="2026-11-08";'
     + ' PPLAY.camId="c1"; PPLAY.t=5; evPlayPoint(' + J(EV1)
     + ',{segment:"10-23-41.mp4",offset_sec:0},-30)') === 0);
ck("后 30 秒不会超过一天",
   run('EV.focus="' + EV1.id + '"; PPLAY.segIdx=3; PPLAY.day="2026-11-08";'
     + ' PPLAY.camId="c1"; PPLAY.t=86390; evPlayPoint(' + J(EV1)
     + ',{segment:"23-59-59.mp4",offset_sec:0},30)') === 86399);
ck("没有定位就没有落点", run('evPlayPoint({id:"a",date:"x"},null,0)') === null);

/* ---- 跨页交代：哪一路、哪一天 ---- */
ck("落到事件所在机位与日期",
   run('state.dayList=["2026-11-06","2026-11-07"]; state.daySel="2026-11-06";'
     + ' state.dayIdx=0; evTargetState(' + J(EV1) + ')'
     + ' && state.camId === "c1" && state.daySel === "2026-11-08"'
     + ' && state.dayList[state.dayIdx] === "2026-11-08"') === true);
ck("日期不在清单里就补进去（按顺序）",
   run('state.dayList=["2026-11-06","2026-11-07"]; state.daySel="2026-11-06";'
     + ' state.dayIdx=0; evTargetState(' + J(EV1) + ');'
     + ' JSON.stringify(state.dayList)') === J(["2026-11-06", "2026-11-07", "2026-11-08"]));
ck("缺字段返回 false", run('evTargetState({id:"x"})') === false);
ck("空事件返回 false", run('evTargetState(null)') === false);

/* ---- 列表三态 ---- */
ck("没数据时说明在同步索引",
   run('EV.data=null; EV.err=""; renderEvList();' 
     + ' document.getElementById("evList").innerHTML').indexOf("正在读事件台账") > 0);
ck("失败时给出重读口径（重试钮 + 网络提示）",
   run('EV.data=null; EV.err="boom"; renderEvList();'
     + ' document.getElementById("evList").innerHTML').indexOf('data-evretry="1"') > 0
   && run('EV.data=null; EV.err="boom"; renderEvList();'
     + ' document.getElementById("evList").innerHTML').indexOf("同一个网络") > 0);

const EMPTY = {ok:true, summary:{count:0, by_category:{}}, events:[]};
ck("空列表要和'加载失败'区分开",
   run('EV.err=""; EV.data=' + J(EMPTY) + '; renderEvList();'
     + ' document.getElementById("evList").innerHTML').indexOf("没有符合条件") > 0);

const TWO = {ok:true, summary:{count:2, by_category:{person:1, two_wheeler:1}},
             events:[EV1, Object.assign({}, EV1,
               {id:"c2_20261108_102500", start:37500, categories:["two_wheeler"]})]};
let h = run('EV.err=""; EV.data=' + J(TWO) + '; renderEvList();'
     + ' document.getElementById("evList").innerHTML');
ck("列表两条都在", h.indexOf("10:23:41") > 0 && h.indexOf("10:25:00") > 0);
ck("列表尾部报总数", h.indexOf("共 2 条") > 0);

/* 接口给时间正序（时间轴读法），事件页要的是"最近发生了什么" → 展示倒过来。
   只影响展示：定位仍用服务端坐标，所以这条只查顺序。 */
const ORD = {ok:true, summary:{count:2, by_category:{}}, events:[
  Object.assign({}, EV1, {id:"a", start:36000, end:36100, duration:100}),
  Object.assign({}, EV1, {id:"b", start:36300, end:36400, duration:100})]};
const oh = run('EV.err=""; EV.data=' + J(ORD) + '; renderEvList();'
             + ' document.getElementById("evList").innerHTML');
ck("列表最新在前（事件页读法）", oh.indexOf("10:05:00") < oh.indexOf("10:00:00"));
ck("头部摘要带分类计数",
   run('EV.err=""; EV.data=' + J(TWO) + '; renderEvHead();'
     + ' document.getElementById("evSum").textContent')
     .indexOf("2 条 · 人员 1 / 两轮车 1") >= 0);
ck("头部失败时写读取失败",
   run('EV.err="boom"; renderEvHead();'
     + ' document.getElementById("evSum").textContent') === "读取失败");

/* ---- 查询串：筛选交给服务端 ---- */
ck("默认只查当天（from=to）",
   run('EV.day="2026-11-08"; EV.cams=[]; EV.cats=[]; EV.from=""; EV.to="";'
     + ' EV.minDur=""; evQuery()')
     .indexOf("from=2026-11-08&to=2026-11-08") > 0);
ck("带了机位就有 cam",
   run('EV.cams=["c1"]; evQuery()').indexOf("cam=c1") > 0);
ck("多机位用逗号（同一维 OR）",
   run('EV.cams=["c1","c2"]; evQuery()').indexOf("cam=c1%2Cc2") > 0);
ck("带了类别就有 cat",
   run('EV.cats=["person"]; evQuery()').indexOf("cat=person") > 0);
ck("多类别用逗号",
   run('EV.cats=["person","two_wheeler"]; evQuery()').indexOf("cat=person%2Ctwo_wheeler") > 0);
ck("移动侦测进 cat 参数（服务端 0.9.126 起认）",
   run('EV.cats=["motion"]; evQuery()').indexOf("cat=motion") > 0);
ck("没选机位就不带 cam",
   run('EV.cams=[]; evQuery()').indexOf("cam=") < 0);
ck("时间范围带钟点（不是整天）",
   run('EV.cats=[]; EV.from="09:00"; EV.to="10:30"; evQuery()')
     .indexOf("from=2026-11-08%2009%3A00&to=2026-11-08%2010%3A30") > 0);
ck("最短时长进 min_dur",
   run('EV.from=""; EV.to=""; EV.minDur="30"; evQuery()').indexOf("min_dur=30") > 0);
ck("没填时长就不带 min_dur",
   run('EV.minDur=""; evQuery()').indexOf("min_dur") < 0);

/* ---- 多选状态（0.9.126）：同维 OR，不同维 AND ---- */
ck("再点一次取消选中", run('EV.cats=[]; evToggle(EV.cats,"person");'
   + ' evToggle(EV.cats,"vehicle"); evToggle(EV.cats,"person"); JSON.stringify(EV.cats)')
   === '["vehicle"]');
ck("没有条件时 evAnyFilter 为假",
   run('EV.cams=[]; EV.cats=[]; EV.from=""; EV.to=""; EV.minDur=""; evAnyFilter()') === false);
ck("选了类别就算有筛选",
   run('EV.cats=["motion"]; evAnyFilter()') === true);
ck("只填时长也算有筛选",
   run('EV.cats=[]; EV.minDur="10"; evAnyFilter()') === true);
ck("条件写成人话", run('EV.cams=[]; EV.cats=["person","motion"];'
   + ' EV.from="09:00"; EV.to="10:00"; EV.minDur="30"; evFilterText()')
   === "人员 / 移动侦测 · 09:00–10:00 · ≥30 秒");

/* ---- 筛选条结构：移动侦测必须是可点的筛选项 ---- */
ck("类别条有「移动侦测」按钮",
   run('renderEvChips(); document.getElementById("evCatBar").innerHTML')
     .indexOf('data-evcat="motion"') > 0);
ck("类别条有「全部类别」复位键",
   run('document.getElementById("evCatBar").innerHTML').indexOf('data-evcat=""') > 0);
ck("机位条是「全部机位」+ 每一路",
   run('document.getElementById("evCamBar").innerHTML').indexOf("全部机位") > 0);
ck("多选时全部机位不再高亮",
   run('EV.cams=["c1"]; renderEvChips();'
     + ' document.getElementById("evCamBar").innerHTML')
     .indexOf('class="evchip on" data-evname=""') < 0);
ck("更多筛选面板默认收起",
   run('EV.more=false; renderEvChips(); document.getElementById("evMoreBox").hidden') === true);
ck("点开后不再 hidden",
   run('EV.more=true; renderEvChips(); document.getElementById("evMoreBox").hidden') === false);
ck("面板有起始/结束/最短时长三个输入",
   src.indexOf('id="evFrom"') > 0 && src.indexOf('id="evTo"') > 0
   && src.indexOf('id="evMinDur"') > 0);
ck("列表头写清当前条件",
   run('EV.err=""; EV.cams=[]; EV.cats=["person"]; EV.from=""; EV.to=""; EV.minDur="";'
     + ' EV.data={summary:{count:1,by_category:{person:1},motion:0},events:[]};'
     + ' renderEvHead(); document.getElementById("evSum").textContent')
     .indexOf("条件：人员") > 0);
ck("摘要里移动侦测单独一档",
   run('EV.cats=[]; EV.data={summary:{count:3,by_category:{person:1},motion:2},events:[]};'
     + ' renderEvHead(); document.getElementById("evSum").textContent')
     .indexOf("移动侦测 2") > 0);
ck("卡片色条读服务端给的 kind（不再自己数 categories）",
   src.indexOf('ev.kind === "motion"') > 0);

/* ---- 查找 ---- */
ck("按 id 找得到", run('EV.data=' + J(TWO) + '; evFind("c1_20261108_102341").start') === 37421);
ck("找不到返回 null", run('EV.data=' + J(TWO) + '; evFind("nope")') === null);
ck("没有数据也不抛", run('EV.data=null; evFind("x")') === null);

/* ---- 播不了就不跳页 ---- */
ck("没有定位时 ±30 不动作", run('EV.clip=null; EV.open="c1_20261108_102341";'
   + ' EV.focus="c1_20261108_102341"; evGo(30)') === false);
ck("定位缺失时 ±30 也不动作",
   run('EV.clip={missing_reason:"not_sealed",missing_text:"x"}; evGo(-30)') === false);

/* ---- 事件快照（0.9.129）：那一眼 ----
   五条口径各一条：有图才占位 / 图地址用服务端给的 / 说明写真实时刻与偏移 /
   没有图如实说原因 / 拿不到图不许留破图。 */
const EVS = Object.assign({}, EV1, {shot:{url:"/api/events/c1_20261108_102341/shot",
                                          at:"10:23:47", offset:6}});
/* 0.9.132 事件详情：快照与回放拆成两段（EVS 在上方才声明，断言放这） */
ck("展开区分「确认区」与「动作区」",
   run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EVS) + ')')
     .indexOf('class="evconfirm"') > 0
   && run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EVS) + ')')
     .indexOf('class="evacts"') > 0);
ck("确认区提示先看清再决定",
   run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EVS) + ')')
     .indexOf("先看清，再决定要不要打开录像") > 0);
ck("前/后 30 秒降级为同一展开里的次要动作",
   run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EVS) + ')')
     .indexOf('data-evgo="-30"') > 0
   && run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EVS) + ')')
     .indexOf('data-evgo="30"') > 0);
ck("有快照：卡片出现缩略图",
   run('EV.open=""; evCardHtml(' + J(EVS) + ')').indexOf("evthumb") > 0);
ck("缩略图地址用服务端给的（不自己拼）",
   run('EV.open=""; evCardHtml(' + J(EVS) + ')')
     .indexOf("/api/events/c1_20261108_102341/shot") > 0);
ck("缩略图懒加载 + 异步解码（一次 200 条）",
   run('EV.open=""; evCardHtml(' + J(EVS) + ')').indexOf('loading="lazy"') > 0);
ck("拿不到图就隐藏，不留破图",
   run('EV.open=""; evCardHtml(' + J(EVS) + ')').indexOf("this.style.display=\'none\'") > 0);
ck("没有快照：卡片不塞占位图",
   run('EV.open=""; evCardHtml(' + J(EV1) + ')').indexOf("evthumb") < 0);
ck("展开时有宽图",
   run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EVS) + ')')
     .indexOf('class="evshot"') > 0);
ck("展开时写清快照时刻与偏移",
   run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EVS) + ')')
     .indexOf("快照 10:23:47 · 事件开始后 6 秒") > 0);
ck("图早于事件起点：写\"开始前\"而不是负号",
   run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(Object.assign({}, EVS,
     {shot:{url:"/api/events/x/shot", at:"10:23:35", offset:-6}})) + ')')
     .indexOf("事件开始前 6 秒") > 0);
ck("没有快照：展开区说清原因（不是播放失败）",
   run('EV.clipErr=""; EV.clip=null; evOpenHtml(' + J(EV1) + ')')
     .indexOf("这条没有留快照") > 0);
ck("没有快照时也照常给三个动作按钮",
   run('EV.clipErr=""; EV.clip={ok:true,segment:"10-23-41.mp4",offset_sec:0};'
     + ' evOpenHtml(' + J(EV1) + ')').indexOf("data-evgo=\"0\"") > 0);

/* ---- 放大与检测框（0.9.130） ----
   缩略图太小，看不清"那是人还是影子"；叠框是为了回答"检测器当时框住了什么"。
   框的位置**必须**由服务端给的归一化坐标换算，前端不猜、不重新跑推理。 */
ck("缩略图可点（带 data-evzoom 与事件 id）",
   run('EV.open=""; evCardHtml(' + J(EVS) + ')')
     .indexOf('data-evzoom="' + EVS.id + '"') > 0);
ck("缩略图提示「点开看大图」",
   run('EV.open=""; evCardHtml(' + J(EVS) + ')').indexOf("点开看大图") > 0);
ck("归一化框 → 百分比定位（左上角 = 中心 - 半个尺寸）",
   run('evBoxHtml([{c:"person",s:0.9,b:[0.5,0.5,0.2,0.4]}])')
     .indexOf('left:40.00%;top:30.00%;width:20.00%;height:40.00%') > 0);
ck("框标签带类别与置信度",
   run('evBoxHtml([{c:"person",s:0.9,b:[0.5,0.5,0.2,0.4]}])').indexOf("<b>人员 0.90</b>") > 0);
ck("框的颜色跟类别走（车用 vehicle 类）",
   run('evBoxHtml([{c:"vehicle",s:0.5,b:[0.5,0.5,0.1,0.1]}])').indexOf('class="evbox vehicle"') > 0);
ck("越界的框夹回 0~100%（贴边的框很常见）",
   run('evBoxHtml([{c:"person",s:0.5,b:[0.0,0.0,0.3,0.3]}])').indexOf('left:0.00%;top:0.00%') > 0);
ck("没有框就不画框层", run('EV.box["x"]=[]; evBoxLayer("x")') === "");
ck("读过但为空：浮层写「没有存检测框」",
   run('EV.box["' + EVS.id + '"]=[]; EV.zoom="' + EVS.id + '";'
     + ' EV.data={events:[' + J(EVS) + ']}; renderEvZoom();'
     + ' document.getElementById("evZoomRoot").innerHTML')
     .indexOf("这张没有存检测框") > 0);
ck("还没读到时写「正在读检测框」",
   run('delete EV.box["' + EVS.id + '"]; EV.data={events:[' + J(EVS) + ']};'
     + ' evZoomHtml(' + J(EVS) + ')').indexOf("正在读检测框") > 0);
ck("浮层有大图 + 打开回放 + 关闭三个出口",
   run('EV.box["' + EVS.id + '"]=[]; EV.data={events:[' + J(EVS) + ']};'
     + ' evZoomHtml(' + J(EVS) + ')').indexOf("evzoomimg") > 0
   && run('evZoomHtml(' + J(EVS) + ')').indexOf('data-evzoomplay="' + EVS.id + '"') > 0
   && run('evZoomHtml(' + J(EVS) + ')').indexOf("data-evzoomclose") > 0);
ck("没有快照的事件点不开浮层",
   run('EV.data={events:[' + J(EV1) + ']}; evZoomOpen("' + EV1.id + '")') === false);
ck("点缩略图开浮层、Esc/关闭能退回",
   run('EV.data={events:[' + J(EVS) + ']}; EV.box["' + EVS.id + '"]=[];'
     + ' evZoomOpen("' + EVS.id + '"); EV.zoom') === EVS.id
   && run('evZoomClose(); EV.zoom') === "");

/* 0.9.132 放大浮层竖屏适配：横图在竖屏只占屏高 1/3，给「横过来看」与「放大」 */
ck("浮层有「横过来看」与「放大」两个竖屏按钮",
   run('EV.box["' + EVS.id + '"]=[]; EV.data={events:[' + J(EVS) + ']};'
     + ' evZoomHtml(' + J(EVS) + ')').indexOf('data-evzoomrot="1"') > 0
   && run('evZoomHtml(' + J(EVS) + ')').indexOf('data-evzoomzoom="1"') > 0);
ck("浮层主按钮文案是「打开录像」",
   run('evZoomHtml(' + J(EVS) + ')').indexOf("▶ 打开录像") > 0);

console.log((fail ? "FAIL" : "OK") + "  events  " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
