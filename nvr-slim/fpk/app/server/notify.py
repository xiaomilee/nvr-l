#!/usr/bin/env python3
"""通知分发器：AI 检测 / 移动侦测事件 → 自定义 Webhook（0.9.60）。

三条设计底线：
1. **绝不在推理/录制路径上同步发通知**：offer() 只入队，独立线程 dispatch；
2. **cooldown 双键**（cam + 类别）：移动侦测没有冷却能把手机炸掉，默认 60 秒；
3. **失败指数退避**（2s→4s→8s，最多 3 次）后丢弃并记日志，不阻塞主链路。

0.9.124 起消息体交给 `notify_tpl.render()` 按目标平台出最小 body，认不出来的
目标仍回退原来的「胖 JSON」（四套键齐全），保证升级不会让已有机器人收不到。
发送结果同时落到 `notify_history.jsonl`：内存 recent 重启即丢，排障时
「昨晚到底发没发出去」不能只靠界面上那 5 行绿字。
"""
import json
import queue
import threading
import time
import urllib.request

import notify_history
import notify_tpl
import wechat_push

CAT_LABEL = {"person": "人员", "two_wheeler": "两轮车", "vehicle": "车辆",
             "motion": "移动侦测"}

# 连续失败多少条要把话说到明面上。取 3：一条可能是网络抖动，两条可能是
# 对端重启，三条就是「你配的东西不对了」——再沉默就等于让人以为家里没事。
FAIL_ALARM = 3


def channel_of(nt):
    """这条通知走哪条通道：'wechat'（公众号）/ 'url'（Webhook）/ ''（没配）。

    公众号只在**显式选了平台**时才走：它有 appid/secret 这些「填了就想留着」
    的字段，若按「填全了就发」来判断，用户临时清空 Webhook 换通道时会被
    意外地改成发微信，而且他自己不知道。
    """
    if not isinstance(nt, dict):
        return ""
    if notify_tpl.norm_platform(nt.get("platform")) == "wechat":
        return "wechat"
    if str(nt.get("webhook") or "").strip():
        return "url"
    return ""


class Notifier:
    def __init__(self, cfg_fn, log=None, timeout=8, var_dir=None):
        self.cfg_fn = cfg_fn            # 返回完整 cfg（读 notify 块）
        self.log = log or (lambda m: None)
        self.timeout = int(timeout)
        self.q = queue.Queue(maxsize=500)
        self.stop_event = threading.Event()
        self.thread = None
        self._last_sent = {}            # (cam, cat) -> ts
        self._fail_streak = {}          # (cam, cat) -> 连续失败次数
        # 全局连续失败计数：单路失败可能是这路没配好，全局连续失败意味着
        # 通道本身断了（token 失效 / 额度用尽 / 对端挂了）。这个才是要喊出来的。
        self._fails = {"n": 0, "err": "", "at": 0.0, "platform": ""}
        self.recent = []                # 最近 20 条 [{cam,cat,ok,at,ms}]
        self._lock = threading.Lock()
        # 最近一次已知的 notify 块。status() 要回答「这条通知会按哪个平台、
        # 哪句文案发」，但不能为了一次轮询去 load_config 读盘：/api/status
        # 几秒一次，扫盘会把状态接口拖成 IO 等待。
        self._cfg = {}
        self._nt = {}
        # var_dir 为空时不落盘：单测直接 new 一个 Notifier 不该往工作区写文件。
        # 计数在启动时读一次文件，之后靠 append 的返回值累加，/api/status
        # 每几秒被轮询一次，绝不能在那个路径上扫盘。
        self.var_dir = var_dir
        self._hist = (notify_history.stats(var_dir) if var_dir else
                      {"total": 0, "last_at": None,
                       "file": notify_history.FILE_NAME})

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name="notify")
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def reconfigure(self, cfg):
        """设置改了直接读新配置（notify 块由 runtime 强校过）。"""
        self._cfg = cfg
        # 通知块单独留一份：status() 只读这一份，不再回头读整份配置。
        nt = (cfg or {}).get("notify") if isinstance(cfg, dict) else None
        self._nt = nt if isinstance(nt, dict) else {}

    # ------------------------------------------------------------ 入队
    def offer(self, cam_name, category, t=None):
        """事件入队。推理线程调，绝不阻塞。cooldown 外的重复直接丢。"""
        nt = self._notify_cfg()
        if not self._enabled(nt, category):
            return False
        key = (str(cam_name), str(category))
        cd = int(nt.get("cooldown") or 60)
        now = t or time.time()
        with self._lock:
            last = self._last_sent.get(key, 0)
            if now - last < cd:
                return False                       # 冷却中：静默丢弃
            self._last_sent[key] = now
        try:
            self.q.put_nowait({"cam": cam_name, "cat": category, "t": now})
            return True
        except queue.Full:
            return False

    def _enabled(self, nt, category):
        """这一类要不要发。判据是「有没有一条能走通的通道」，不是「有没有 webhook」：
        公众号没有地址，按老写法会被判成没配，静默不发。"""
        if category not in ("motion", "person", "vehicle"):
            return False
        if not nt or not nt.get(category):
            return False
        ch = channel_of(nt)
        if ch == "wechat":
            return wechat_push.ready(nt.get("wechat"))[0]
        return ch == "url"

    def _notify_cfg(self):
        cfg = self.cfg_fn() if self.cfg_fn else {}
        if not isinstance(cfg, dict):
            return {}
        nt = cfg.get("notify")
        if isinstance(nt, dict):
            # 顺手刷新 status() 用的那份副本：派发时既然已经读到了新值，
            # 就没必要让状态接口还停在 reconfigure 那一刻。
            self._nt = nt
            return nt
        return {}

    # ------------------------------------------------------------ 派发循环
    def _loop(self):
        while not self.stop_event.is_set():
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._dispatch(item)
            except Exception as e:
                self.log("通知派发异常：%r" % e)

    def _dispatch(self, item):
        nt = self._notify_cfg()
        ch = channel_of(nt)
        if not ch:
            return
        key = (item["cam"], item["cat"])
        delay = 2.0
        err, platform, tries = "", ch, 0
        for attempt in range(3):
            if self.stop_event.is_set():
                return
            tries = attempt + 1
            ok, ms, err, platform, can_retry = self._send_once(nt, item)
            if ok:
                with self._lock:
                    self._fail_streak[key] = 0
                    self._record(item, True, ms)
                self._note_ok()
                self._persist(item, True, ms, "", tries, platform)
                return
            if not can_retry:
                # 未关注 / 额度用尽 / 模板写错：这几种再等 6 秒也不会变好，
                # 而且公众号是按调用次数扣额度的，重试只会把额度烧得更快。
                break
            if attempt < 2:
                time.sleep(delay)      # 最后一次失败不再空等，直接落日志
                delay *= 2
        with self._lock:
            self._fail_streak[key] = self._fail_streak.get(key, 0) + 1
            self._record(item, False, 0, err)
        self._note_fail(err, platform)
        self._persist(item, False, 0, err, tries, platform)
        self.log("通知发送失败（试 %d 次）：%s %s · %s"
                 % (tries, item["cam"], CAT_LABEL.get(item["cat"], item["cat"]), err))

    def _send_once(self, nt, item):
        """发一次。返回 (ok, ms, err, platform, 可重试)。

        公众号走两步接口（换 token → 发模板），其余通道走一次 POST。合流在
        这里，派发循环就不必知道通道差别 —— 否则重试/冷却/落盘三处都要
        写一遍 "if 公众号"。
        """
        tpl = nt.get("template")
        if channel_of(nt) == "wechat":
            text = notify_tpl.text_of(item, tpl)
            ok, ms, err, can = wechat_push.send(nt.get("wechat"), text,
                                                timeout=max(self.timeout, 10))
            return ok, ms, err, "wechat", can
        url = str(nt.get("webhook") or "").strip()
        msg, platform = notify_tpl.render(url, item, tpl, nt.get("platform"))
        ok, ms, err = self._post(url, msg)
        return ok, ms, err, platform, True

    def _note_ok(self):
        """成功一次就把连续失败计数清零，并在「刚恢复」时说一声。

        只在跨过阈值那一刻说：一个填错的 openid 会按分钟失败，逐条记日志
        等于用同一句话把磁盘刷满，反而盖掉真正有用的上下文。
        """
        with self._lock:
            n = int(self._fails.get("n") or 0)
            self._fails = {"n": 0, "err": "", "at": 0.0, "platform": ""}
        if n >= FAIL_ALARM:
            self.log("通知发送已恢复（此前连续失败 %d 条）" % n)

    def _note_fail(self, err, platform):
        with self._lock:
            self._fails["n"] = int(self._fails.get("n") or 0) + 1
            self._fails["err"] = str(err or "")[:160]
            self._fails["at"] = time.time()
            self._fails["platform"] = platform
            n = self._fails["n"]
        if n == FAIL_ALARM:
            self.log("[ERR] 通知连续失败 %d 条，最近一次：%s —— 去设置页看「实际平台」"
                     "与发送历史，通知现在是断的" % (n, err))

    @staticmethod
    def _message(item):
        """兼容入口：不认平台时就是原来那份四套键齐全的胖 JSON。

        保留它是因为设置页「测试发送」与既有单测都按这个形状断言；
        真正的平台适配走 notify_tpl.render(url, item)。
        """
        body, _p = notify_tpl.render("", item)
        return body

    def _persist(self, item, ok, ms, err, tries, platform):
        """落盘只做一次 append，失败也吞掉 —— 通知发没发出去不该被磁盘状况改写。"""
        if not self.var_dir:
            return
        written, total = notify_history.append(self.var_dir, {
            "at": item.get("t") or time.time(), "cam": item["cam"],
            "cat": item["cat"], "ok": ok, "ms": ms, "err": err,
            "tries": tries, "platform": platform})
        if not written:
            self.log("通知历史落盘失败（不影响发送结果）：var 目录不可写？")
            return
        with self._lock:
            self._hist["total"] = total
            self._hist["last_at"] = item.get("t") or time.time()

    def _post(self, url, payload):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                rc = r.getcode()
            ms = int((time.time() - t0) * 1000)
            if 200 <= rc < 300:
                return True, ms, ""
            return False, ms, "HTTP %d" % rc
        except Exception as e:
            return False, int((time.time() - t0) * 1000), str(e)[:120]

    # ------------------------------------------------------------ 测试发送
    def test(self, url, tpl=None, platform=None, wx=None):
        """设置页「测试发送」：同步发一条，返回 (ok, ms, err, platform)。

        测试发送也按目标平台出 body / 走同一条通道，否则「测试成功、真实通知
        却被拒收」会变成一个查不出来的假象。测试同样记一笔历史，方便事后对账。
        模板 / 平台 / 公众号凭据可由调用方临时指定：界面上刚改还没保存时，
        测的就该是屏幕上那套值，而不是磁盘上的旧值。
        """
        url = str(url or "").strip()
        item = {"cam": "测试", "cat": "person", "t": time.time()}
        nt = self._notify_cfg()
        if tpl is None:
            tpl = nt.get("template")
        if platform is None:
            platform = nt.get("platform")
        if wx is None:
            wx = nt.get("wechat")

        if notify_tpl.norm_platform(platform) == "wechat":
            # 页面拿不到 secret 原文（redact_cfg 抹空了），所以「测试发送」传上来的
            # 永远是空串。不在这里补一次，就会变成「保存后真事件发得出去、点测试
            # 却说缺 appsecret」——这个假象比报错更难查（0.9.142 真机抓到）。
            wx = dict(wx) if isinstance(wx, dict) else {}
            if not str(wx.get("secret") or "").strip():
                _old = (nt.get("wechat") or {}).get("secret")
                if _old:
                    wx["secret"] = _old
            okw, missing = wechat_push.ready(wx)
            if not okw:
                return (False, 0, "公众号未配置完整：缺 " + "、".join(missing),
                        "wechat")
            # 测试走真发：只有真拿到 token 再真发一条，「配好了」才算数。
            ok, ms, err, _can = wechat_push.send(
                wx, notify_tpl.text_of(item, tpl),
                timeout=max(self.timeout, 10))
            self._persist(item, ok, ms, err, 1, "wechat")
            return ok, ms, err, "wechat"

        if not url:
            return False, 0, "Webhook 地址为空", ""
        msg, plat = notify_tpl.render(url, item, tpl, platform)
        ok, ms, err = self._post(url, msg)
        self._persist(item, ok, ms, err, 1, plat)
        return ok, ms, err, plat

    # ------------------------------------------------------------ 状态
    def _record(self, item, ok, ms, err=""):
        self.recent.insert(0, {"cam": item["cam"], "cat": item["cat"],
                               "ok": ok, "ms": ms, "err": err,
                               "at": time.strftime("%H:%M:%S")})
        del self.recent[20:]

    def status(self):
        # render 段是「这条通知实际会怎么发」的唯一出处：平台是识别来的还是
        # 人选的、文案有没有因为占位符写错被回退。设置页照抄这三行，不自己猜。
        nt = self._nt if isinstance(self._nt, dict) else {}
        # 公众号的状态单独取：它要看「四个字段填全了没、token 还在不在」。
        # 只给摘要（appid/openid 都脱敏），secret 与 token 原文一律不出这个函数。
        wx = wechat_push.state(nt.get("wechat"))
        with self._lock:
            # history 是启动读一次 + 每次 append 累加的缓存值，不是当场扫盘：
            # /api/status 被前端几秒轮询一次，扫盘会把通知线程拖进 IO 等待。
            fails = dict(self._fails)
            return {"recent": list(self.recent),
                    "pending": self.q.qsize(),
                    "channel": channel_of(nt) or "none",
                    "render": notify_tpl.describe(nt.get("webhook"),
                                                  nt.get("template"),
                                                  nt.get("platform"), wx),
                    # 连续失败到阈值才喊：偶发一条失败不该让诊断页常年挂红。
                    "alarm": ({"n": int(fails.get("n") or 0),
                               "err": fails.get("err") or "",
                               "at": fails.get("at") or 0,
                               "platform": fails.get("platform") or ""}
                              if int(fails.get("n") or 0) >= FAIL_ALARM else None),
                    "history": {"file": self._hist.get("file"),
                                "total": self._hist.get("total", 0),
                                "last_at": self._hist.get("last_at"),
                                "persisted": bool(self.var_dir)}}

    def history(self, **kw):
        """只读查询入口；真正的读盘只在有人显式查历史时发生。"""
        return notify_history.query(self.var_dir, **kw)


if __name__ == "__main__":
    n = Notifier(lambda: {"notify": {"webhook": "", "cooldown": 60}})
    print("offer 无 webhook:", n.offer("院子", "person"))
    print("status:", n.status())
