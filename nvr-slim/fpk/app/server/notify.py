#!/usr/bin/env python3
"""通知分发器：AI 检测 / 移动侦测事件 → 自定义 Webhook（0.9.60）。

三条设计底线：
1. **绝不在推理/录制路径上同步发通知**：offer() 只入队，独立线程 dispatch；
2. **cooldown 双键**（cam + 类别）：移动侦测没有冷却能把手机炸掉，默认 60 秒；
3. **失败指数退避**（2s→4s→8s，最多 3 次）后丢弃并记日志，不阻塞主链路。

消息体是「胖 JSON」——一个 body 同时带 msg_type/content.text（飞书）、
msgtype/text.content（钉钉 / 企业微信）、title/desp（Server酱）四套键，
主流群机器人开箱即显示，不用为每个平台写一套发送逻辑。
"""
import json
import queue
import threading
import time
import urllib.request

CAT_LABEL = {"person": "人员", "two_wheeler": "两轮车", "vehicle": "车辆",
             "motion": "移动侦测"}


class Notifier:
    def __init__(self, cfg_fn, log=None, timeout=8):
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
        return nt if isinstance(nt, dict) else {}

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
        msg = self._message(item)
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
                return
            if attempt < 2:
                time.sleep(delay)      # 最后一次失败不再空等，直接落日志
                delay *= 2
        with self._lock:
            self._fail_streak[key] = self._fail_streak.get(key, 0) + 1
            self._record(item, False, 0, err)
        self.log("通知发送失败（已重试 3 次）：%s %s · %s"
                 % (item["cam"], CAT_LABEL.get(item["cat"], item["cat"]), err))

    @staticmethod
    def _message(item):
        label = CAT_LABEL.get(item["cat"], item["cat"])
        when = time.strftime("%H:%M:%S", time.localtime(item.get("t") or time.time()))
        text = "【NVR · %s】检测到%s · %s" % (item["cam"], label, when)
        # 胖 JSON：飞书 / 钉钉 / 企业微信 / Server酱 四套键一次带齐
        return {"msg_type": "text", "content": {"text": text},
                "msgtype": "text", "text": {"content": text},
                "title": "NVR 检测通知", "desp": text}

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
    def test(self, url):
        """设置页「测试发送」：同步发一条，返回 (ok, ms, err)。"""
        url = str(url or "").strip()
        if not url:
            return False, 0, "Webhook 地址为空"
        ok, ms, err = self._post(url, self._message(
            {"cam": "测试", "cat": "person", "t": time.time()}))
        return ok, ms, err

    # ------------------------------------------------------------ 状态
    def _record(self, item, ok, ms, err=""):
        self.recent.insert(0, {"cam": item["cam"], "cat": item["cat"],
                               "ok": ok, "ms": ms, "err": err,
                               "at": time.strftime("%H:%M:%S")})
        del self.recent[20:]

    def status(self):
        with self._lock:
            return {"recent": list(self.recent),
                    "pending": self.q.qsize()}


if __name__ == "__main__":
    n = Notifier(lambda: {"notify": {"webhook": "", "cooldown": 60}})
    print("offer 无 webhook:", n.offer("院子", "person"))
    print("status:", n.status())
