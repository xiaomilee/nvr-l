#!/usr/bin/env python3
"""微信公众号（模板消息）推送：一条文本 → 关注者的微信（0.9.142）。

为什么不塞进 notify_tpl
-----------------------
其它通道都是「一个 POST 到 webhook」，公众号是**两步且有状态**：先拿
access_token（7200 秒），再拿它发模板消息。token 不能每次现取 —— 获取接口
有每日额度，而且同一个 appID 频繁取会把上一个顶掉（旧的还能活 5 分钟，
之后报 40001）。缓存 + 提前续 + 40001 重取一次，这三件事必须在一处做，
散在派发循环里迟早会写出「每次发送都取一次 token」的版本。

四条不变式
----------
1. **secret 与 token 绝不进错误信息**：返回界面的 err 只有中文说明 + errcode，
   日志、/api/status、notify_history.jsonl 里都不该出现这两个值。
   `_safe_reason()` 会再兜一次底，把 URL 里 `secret=…` / `access_token=…` 抹掉。
2. **不可重试的错误不重试**：43004（未关注）、45009（额度用尽）、40037（模板
   不匹配）多试两次只会白等 6 秒，还可能把额度刷得更快。只有网络类失败和
   token 失效才值得重试。
3. **token 只活在内存**：落盘就等于多一份密钥副本，而它两小时就过期，
   重启后重新取一次的代价远小于多一个泄露面。
4. **唯一出口 `_http_json()`**：单测替换这一个函数就能覆盖全部路径，
   不必起 HTTP 桩，也不必给每个分支造一套 mock。

跑：python app/server/wechat_push.py   （只打印配置校验结果，不联网）
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/template/send"

# 微信给的是 7200 秒。提前 200 秒换，是为了避开「刚好在过期那一刻发出去」
# 这种只在真机上偶发、单测里永远复现不了的边界。
TOKEN_TTL = 7000
DEFAULT_KEY = "content"
# 模板字段值的长度上限（微信侧约 200 字，超了报 45002）。NVR 的文案实际只有
# 三十来个字，这里截断只是防呆：自定义模板塞了一整段进去时，宁可发出去少半句，
# 也不要整条被拒。
MAX_VALUE = 200
MAX_FIELD = 200

FIELDS = ("appid", "secret", "openid", "template_id", "key", "url")
FIELD_LABEL = {"appid": "appID", "secret": "appsecret", "openid": "openid",
               "template_id": "模板 ID", "key": "数据键名", "url": "跳转链接"}

# errcode → (给人看的中文, 值不值得重试)。文案要能直接指向「去改哪个字段」，
# 不能只说「发送失败」——用户手里没有 errcode 对照表。
ERRORS = {
    -1: ("微信接口没有响应（系统繁忙，稍后自动重试）", True),
    40001: ("access_token 失效（已自动重取一次仍失败）", True),
    40003: ("openid 无效（多半是复制少了几位）", False),
    40013: ("appID 不对", False),
    40037: ("模板 ID 不存在，或模板变量名与「数据键名」不一致", False),
    40125: ("appsecret 不对（是否在公众平台重置过？）", False),
    41001: ("缺少 access_token（内部错误，重试即可）", True),
    42001: ("access_token 过期（已自动重取一次仍失败）", True),
    43004: ("这个 openid 还没关注该公众号", False),
    45002: ("消息内容超长（模板字段值上限约 200 字）", False),
    45009: ("今天的接口调用额度已用尽", False),
    45047: ("客服消息额度用尽", False),
    48001: ("该公众号没有模板消息权限（测试号可用，未认证订阅号不行）", False),
}

_LOCK = threading.Lock()
# appid -> {"t": token, "exp": 到期时刻}。键用 appid 而不是 secret：
# secret 换了（重置）旧 token 立刻失效，靠 40001 重取那条路兜。
_TOKENS = {}


# ------------------------------------------------------------------ 配置
def norm(wx):
    """配置块 → 干净的六个字段。只 strip + 限长，不猜格式。

    故意不做长度/字符集硬校验：微信自己会报 40013/40125，比我们猜得准；
    在本地把用户填的值判死，只会让人对着「格式不对」四个字反复试。
    """
    src = wx if isinstance(wx, dict) else {}
    out = {}
    for k in FIELDS:
        out[k] = str(src.get(k) or "").strip()[:MAX_FIELD]
    if not out["key"]:
        out["key"] = DEFAULT_KEY
    return out


def ready(wx):
    """填全了没有。返回 (bool, 缺失字段的中文名列表)。

    openid / 模板 ID 也要求填：只填 appid+secret 能换到 token，但发不出去，
    这种人最容易以为「配置好了但没收到」，不如在入口就说清楚缺什么。
    """
    w = norm(wx)
    missing = [FIELD_LABEL[k] for k in ("appid", "secret", "openid",
                                        "template_id") if not w[k]]
    return (not missing), missing


def mask(s):
    """给状态接口/日志看的脱敏：留头 4 尾 2。appid 不是秘密，但 openid
    是「谁能收到通知」的凭据，不该整串出现在截图里。"""
    s = str(s or "")
    if len(s) <= 8:
        return ("*" * len(s)) if s else ""
    return s[:4] + "…" + s[-2:]


def state(wx):
    """给 /api/status 的摘要。**不含 secret，也不含完整 openid/token。**"""
    w = norm(wx)
    ok, missing = ready(w)
    now = time.time()
    with _LOCK:
        c = _TOKENS.get(w["appid"]) or {}
    return {"appid": mask(w["appid"]), "openid": mask(w["openid"]),
            "key": w["key"], "ready": ok, "missing": missing,
            "token_cached": bool(c.get("t")) and float(c.get("exp") or 0) > now,
            "token_exp_in": (max(0, int(float(c.get("exp") or 0) - now))
                             if c.get("t") else None)}


# ------------------------------------------------------------------ HTTP
def _safe_reason(e):
    """异常 → 一句能进日志的话，且抹掉可能带着 secret / token 的 query。

    urllib 的异常串里通常只有 "HTTP Error 401" 这种，但 URLError 有可能把
    整条 URL 带出来（含 `secret=`）。这一层是最后一道闸，不指望调用方自觉。
    """
    s = "%s: %s" % (type(e).__name__, e)
    s = re.sub(r"(secret|access_token|appid)=[^&\s'\"]+", r"\1=***", s)
    return s[:120]


def _http_json(url, payload=None, timeout=8):
    """唯一出口。payload=None 走 GET。返回 (dict|None, err 文本)。

    WeChat 的接口无论成败都回 HTTP 200，成败看 body 里的 errcode ——
    所以「拿到 JSON」不等于成功，调用方必须自己判 errcode。
    """
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return None, "微信接口返回 HTTP %s" % getattr(e, "code", "?")
    except Exception as e:
        return None, _safe_reason(e)
    try:
        return json.loads(raw.decode("utf-8", "replace")), ""
    except ValueError:
        return None, "微信接口返回的不是 JSON"


def hint(errcode, errmsg=""):
    """errcode → (中文, 可重试)。认不出来的码保留 errmsg，别把原文吞掉。"""
    try:
        code = int(errcode)
    except (TypeError, ValueError):
        code = -1
    if code in ERRORS:
        return ERRORS[code]
    tail = str(errmsg or "").strip()[:60]
    return ("微信返回错误 %s%s" % (code, ("：" + tail) if tail else ""), False)


# ------------------------------------------------------------------ token
def get_token(appid, secret, timeout=8, force=False, now=None):
    """取 access_token。返回 (token, err 文本, 可重试)。

    force=True 时无视缓存重取（40001 之后走这条路）。缓存失效就删掉这一项，
    免得一个坏 token 在表里躺两小时，每次发送都先失败一次再重试。
    """
    appid = str(appid or "").strip()
    secret = str(secret or "").strip()
    if not appid or not secret:
        return "", "appID 或 appsecret 为空", False
    now = float(now if now is not None else time.time())
    if not force:
        with _LOCK:
            c = _TOKENS.get(appid) or {}
        if c.get("t") and float(c.get("exp") or 0) > now:
            return c["t"], "", True
    q = urllib.parse.urlencode({"grant_type": "client_credential",
                                "appid": appid, "secret": secret})
    res, err = _http_json(TOKEN_URL + "?" + q, timeout=timeout)
    if res is None:
        return "", err or "取 token 失败", True
    tok = str(res.get("access_token") or "")
    if tok:
        ttl = TOKEN_TTL
        try:
            ttl = min(TOKEN_TTL, max(60, int(res.get("expires_in") or 7200) - 200))
        except (TypeError, ValueError):
            pass
        with _LOCK:
            _TOKENS[appid] = {"t": tok, "exp": now + ttl}
        return tok, "", True
    # 换 token 失败：40125/40013 是填错了，重试没有意义；-1 是系统繁忙。
    msg, can = hint(res.get("errcode"), res.get("errmsg"))
    with _LOCK:
        _TOKENS.pop(appid, None)
    return "", msg, can


def forget_token(appid=None):
    """丢掉缓存的 token（重置 appsecret / 单测清理用）。"""
    with _LOCK:
        if appid:
            _TOKENS.pop(str(appid).strip(), None)
        else:
            _TOKENS.clear()


# ------------------------------------------------------------------ 发送
def build_body(wx, text):
    """模板消息体。**键名就是「数据键名」，不是 DATA。**

    `{{content.DATA}}` 里 `.DATA` 是微信的固定语法后缀，前面那截才是键名。
    写成 `{"data": {"DATA": {"value": ...}}}` 会直接 40037 —— 这是接公众号
    最常见的一个坑，所以键名做成可配（默认 content），并在 40037 的提示里
    直说「变量名与数据键名不一致」。
    """
    w = norm(wx)
    body = {"touser": w["openid"], "template_id": w["template_id"],
            "data": {w["key"]: {"value": str(text or "")[:MAX_VALUE]}}}
    if w["url"]:
        body["url"] = w["url"]
    return body


def send(wx, text, timeout=8, now=None):
    """发一条模板消息。返回 (ok, ms, err 文本, 可重试)。

    40001 / 42001（token 被顶掉或过期）内部重取一次再发一遍 —— 这不算
    「对外重试」，所以不占派发循环那 3 次预算；真失败了才把 4 秒的重试
    留给上层。
    """
    w = norm(wx)
    ok, missing = ready(w)
    if not ok:
        return False, 0, "公众号未配置完整：缺 " + "、".join(missing), False
    text = str(text or "").strip()
    if not text:
        return False, 0, "消息内容为空", False

    t0 = time.time()
    body = build_body(w, text)
    err = ""
    for attempt in (0, 1):
        tok, terr, tcan = get_token(w["appid"], w["secret"], timeout,
                                    force=bool(attempt), now=now)
        if not tok:
            return False, int((time.time() - t0) * 1000), terr, tcan
        url = SEND_URL + "?access_token=" + urllib.parse.quote(tok, safe="")
        res, nerr = _http_json(url, body, timeout=timeout)
        if res is None:
            err = nerr or "发送失败"
            return False, int((time.time() - t0) * 1000), err, True
        try:
            code = int(res.get("errcode") or 0)
        except (TypeError, ValueError):
            code = -1
        if code == 0:
            return True, int((time.time() - t0) * 1000), "", True
        if code in (40001, 41001, 42001) and attempt == 0:
            forget_token(w["appid"])       # 换一个 token 再试一次
            err = hint(code, res.get("errmsg"))[0]
            continue
        msg, can = hint(code, res.get("errmsg"))
        return False, int((time.time() - t0) * 1000), msg, can
    return False, int((time.time() - t0) * 1000), err or "发送失败", True


if __name__ == "__main__":
    demo = {"appid": "wx0123456789abcdef", "secret": "s" * 32,
            "openid": "oR_cp3WVts0RzDucPkwm64zHRSwk",
            "template_id": "AzxFiUL7U1QXQmqXeL0_RKAUBaPu5fYv1IM6D-vhR38"}
    print("ready:", ready(demo))
    print("state:", state(demo))
    print("empty:", ready({}))
    print("body:", json.dumps(build_body(demo, "【NVR · 院子】检测到人员 · 12:30:00"),
                              ensure_ascii=False))
    for code in (40037, 43004, 45009, 48001, 99999):
        print(code, hint(code, "demo errmsg"))
