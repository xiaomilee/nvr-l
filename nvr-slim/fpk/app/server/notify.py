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

CAT_LABEL = {"person": "人员", "two_wheeler": "两轮车", "vehicle": "车辆",
             "motion": "移动侦测"}


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
        if not nt or not str(nt.get("webhook") or "").strip():
            return False
        return bool(nt.get(category)) if category in ("motion", "person", "vehicle") else False

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
        url = str(nt.get("webhook") or "").strip()
        if not url:
            return
        # 模板按目标平台选；template / platform 缺省时走默认文案与自动识别。
        msg, platform = notify_tpl.render(url, item, nt.get("template"),
                                          nt.get("platform"))
        key = (item["cam"], item["cat"])
        delay = 2.0
        for attempt in range(3):
            if self.stop_event.is_set():
                return
            ok, ms, err = self._post(url, msg)
            if ok:
                with self._lock:
                    self._fail_streak[key] = 0
                    self._record(item, True, ms)
                self._persist(item, True, ms, "", attempt + 1, platform)
                return
            if attempt < 2:
                time.sleep(delay)      # 最后一次失败不再空等，直接落日志
                delay *= 2
        with self._lock:
            self._fail_streak[key] = self._fail_streak.get(key, 0) + 1
            self._record(item, False, 0, err)
        self._persist(item, False, 0, err, 3, platform)
        self.log("通知发送失败（已重试 3 次）：%s %s · %s"
                 % (item["cam"], CAT_LABEL.get(item["cat"], item["cat"]), err))

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
    def test(self, url, tpl=None, platform=None):
        """设置页「测试发送」：同步发一条，返回 (ok, ms, err, platform)。

        测试发送也按目标平台出 body，否则「测试成功、真实通知却被拒收」
        会变成一个查不出来的假象。测试同样记一笔历史，方便事后对账。
        模板 / 平台可由调用方临时指定：界面上刚改还没保存时，测的就该是
        屏幕上那句文案，而不是磁盘上的旧值。
        """
        url = str(url or "").strip()
        if not url:
            return False, 0, "Webhook 地址为空", ""
        item = {"cam": "测试", "cat": "person", "t": time.time()}
        nt = self._notify_cfg()
        if tpl is None:
            tpl = nt.get("template")
        if platform is None:
            platform = nt.get("platform")
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
        with self._lock:
            # history 是启动读一次 + 每次 append 累加的缓存值，不是当场扫盘：
            # /api/status 被前端几秒轮询一次，扫盘会把通知线程拖进 IO 等待。
            return {"recent": list(self.recent),
                    "pending": self.q.qsize(),
                    "render": notify_tpl.describe(nt.get("webhook"),
                                                  nt.get("template"),
                                                  nt.get("platform")),
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
