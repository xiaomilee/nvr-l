#!/usr/bin/env python3
"""安装级 API 凭证（`var/secret.json`）。

**为什么要有这一层**
本服务必须监听 0.0.0.0 —— 手机直连 `http://<NAS-IP>:<port>` 才通，改回
127.0.0.1 等于砍掉一半使用场景。于是「同一局域网里谁都能操作」成了默认状态：
现有的 `X-NVR-Request` 只能证明「请求由本页面的脚本发出」（防 CSRF），
证明不了「发请求的人有权操作」。评审把这一条列为三个工程问题之首。

**为什么是单一 token，而不是用户体系**
本机单人使用。复杂多用户权限被评审明确列为 P3 不做（模型依赖与维护成本远超
收益）。这里只解决一件事：没有凭证就不能调接口。

**为什么还要签一枚 cookie**
实时流 `<img src>`、快照、回放 `<video src>` 都拿不到自定义请求头，没法带
`Authorization`。而把主 token 塞进 URL query 更糟 —— URL 会进访问日志、浏览器
历史、Referer，等于把凭证摊在最容易被看到的地方。所以：粘贴 token 登录一次，
服务端签一枚 HttpOnly cookie，密钥就是 api_token 本身、签名里带过期时间，
服务端不存任何会话状态（重启不掉登录）。轮换 token 等于让所有旧 cookie 立刻
失效，这正是想要的行为。

**比对口径**
一律 `hmac.compare_digest`。按字节短路比较会在响应耗时上泄露「前缀猜对了几位」，
32 字节的 token 也经不起这种泄露。
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time

SECRET_NAME = "secret.json"
# 允许外部注入（容器、自动化测试、装的时候想固定一枚）。环境变量优先于文件：
# 有它就不落盘、也不轮换，避免"以为换了其实没换"。
ENV_TOKEN = "NVR_API_TOKEN"
COOKIE_NAME = "nvr_sess"
COOKIE_TTL = 30 * 86400
# 不需要凭证的接口。只放「登录入口」两条：
#   status —— 页面要先知道"要不要弹粘贴框"，它自己就带凭证校验结果；
#   login  —— 拿 token 换 cookie，没有它就没法登录。
# rotate **不在**这里：轮换必须凭旧凭证，否则谁都能把主人锁在门外。
PUBLIC_PATHS = ("/api/auth/status", "/api/auth/login")
_BEARER_RE = re.compile(r"^\s*Bearer\s+(\S+)\s*$", re.I)

_CACHE = {"path": None, "key": None, "token": ""}


def secret_path(var_dir):
    return os.path.join(var_dir, SECRET_NAME)


def env_token():
    return (os.environ.get(ENV_TOKEN) or "").strip()


def new_token():
    """32 字节随机 → 43 字符的 URL 安全串。足够抗暴力猜，也方便手抄。"""
    return secrets.token_urlsafe(32)


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())        # 断电时宁可慢一点，也别留半截凭证文件
        except OSError:
            pass
    try:
        # 只给服务自己的运行身份看。Windows 上 chmod 不生效，忽略即可：
        # 测试机与生产机都是 Linux，这一条在真机上才是有效的那条。
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load(var_dir, create=True):
    """读凭证；不存在就生成一份。返回 `{api_token, created_at, path}`。"""
    path = secret_path(var_dir)
    raw = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except (ValueError, OSError):
            # 文件坏了不能让整个服务起不来：当作没有，重新生成一枚。
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    tok = env_token() or str(raw.get("api_token") or "").strip()
    if not tok:
        if not create:
            return {"api_token": "", "created_at": "", "path": path}
        tok = new_token()
        raw = {"api_token": tok,
               "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            os.makedirs(var_dir, exist_ok=True)
            _write(path, raw)
        except OSError:
            # 落盘失败也先用内存里这份跑起来（否则整个服务不可用）；
            # 代价是重启后重新生成，用户要再粘一次 —— 由日志里那条提示暴露。
            pass
    return {"api_token": tok,
            "created_at": str(raw.get("created_at") or ""),
            "path": path}


def token(var_dir):
    """当前 token（带缓存）。每个请求都要用它，不该每次都读盘 + 解析 JSON。"""
    env = env_token()
    if env:
        return env
    path = secret_path(var_dir)
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _CACHE["path"] == path and _CACHE["key"] == key:
        return _CACHE["token"]
    tok = load(var_dir).get("api_token") or ""
    if key is not None:
        _CACHE.update({"path": path, "key": key, "token": tok})
    return tok


def verify(var_dir, given):
    """常量时间比对。空值一律不通过（含 secret.json 还没生成成功的极早期）。"""
    want = token(var_dir)
    if not want or not given:
        return False
    try:
        return hmac.compare_digest(want.encode("utf-8"), str(given).encode("utf-8"))
    except (TypeError, ValueError):
        return False


def bearer(header):
    """从 `Authorization: Bearer xxx` 里取凭证。取不到返回空串，绝不抛异常。"""
    m = _BEARER_RE.match(header or "")
    return m.group(1) if m else ""


def mask(tok):
    """日志/诊断里展示用：只留头尾各 4 位。"""
    tok = str(tok or "")
    if len(tok) <= 12:
        return "***"
    return "%s…%s" % (tok[:4], tok[-4:])


def rotate(var_dir):
    """换一枚新 token：旧 token 与所有已签出的 cookie 立即失效。

    落盘失败必须冒泡 —— 只改内存不落盘的话，重启后旧 token 又活过来了，
    用户会以为"轮换过了"，比不轮换更危险。
    """
    if env_token():
        raise RuntimeError("凭证由环境变量 %s 指定，请改环境变量后重启" % ENV_TOKEN)
    if not token(var_dir):
        raise RuntimeError("当前没有可轮换的凭证")
    tok = new_token()
    data = {"api_token": tok,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(var_dir, exist_ok=True)
    _write(secret_path(var_dir), data)
    _CACHE.update({"path": None, "key": None, "token": ""})
    return {"api_token": tok, "created_at": data["created_at"]}


def _sig(key, exp):
    return hmac.new(key.encode("utf-8"), b"nvr-sess|%d" % int(exp),
                    hashlib.sha256).hexdigest()


def cookie_value(var_dir, ttl=COOKIE_TTL):
    """签一枚会话串 `过期时间.签名`。返回空串表示还没有凭证。"""
    tok = token(var_dir)
    if not tok:
        return ""
    exp = int(time.time()) + int(ttl)
    return "%d.%s" % (exp, _sig(tok, exp))


def cookie_ok(var_dir, header):
    """校验 Cookie 头里的会话签名。解析失败 / 过期 / 换过 token 都算没通过。"""
    tok = token(var_dir)
    if not tok or not header:
        return False
    jar = {}
    for part in str(header).split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            jar[k.strip()] = v.strip()
    exp_s, _, sig = (jar.get(COOKIE_NAME) or "").partition(".")
    if not sig or not exp_s.isdigit():
        return False
    if int(exp_s) < time.time():
        return False
    try:
        return hmac.compare_digest(_sig(tok, int(exp_s)), sig)
    except (TypeError, ValueError):
        return False


def is_public(path):
    """这条路径要不要凭证。

    只放三类：页面本身（`/`、`/index.html`、图标等静态资源 —— 它们不含任何业务
    数据）、以及上面那两条登录入口。其余 `/api/*` 一律要凭证。
    注意这里收到的是 `urlparse(path).path`，不带 query。
    """
    if not path.startswith("/api/"):
        return True
    return path in PUBLIC_PATHS
