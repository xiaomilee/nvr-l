#!/usr/bin/env python3
"""日志 / 诊断的统一脱敏口径。

**为什么要单独一个模块**
0.9.120 之前，脱敏只有一条正则、写在全项目的两个地方（`runtime.redact_url`
和 `nvr_slim._CRED`），而且只认 `rtsp://user:pass@` 这一种形态。加了 API 凭证
之后泄漏面一下变宽了：
  · 访问日志会原样记下 `GET /api/status?token=...`（用户拿脚本试接口时很常见）；
  · webhook 地址可能是 `https://user:pass@hooks.example.com/...`；
  · 诊断导出里的录制命令行带 `-headers Authorization: Bearer ...`；
  · 还有 token 本身 —— 轮换时我们主动记一行「新凭证 xxxx」，手滑一次就是整枚
    凭证进日志文件，而这个文件是给用户贴给别人的。

一条正则覆盖不了这些，也不该让每个调用点自己想着脱敏：漏一个就是泄漏，而漏掉
的那个在日志里看起来只是「某一行有点长」。所以收敛到这里，所有落盘/出接口的
文本都过一遍。

**为什么只抹不拒**
日志要能看出「这里发生过一次带凭证的请求」。整行吞掉会让人以为功能没被调用过，
排障时反而多一个假象。抹成 `***` 之后协议、主机、路径、参数名都留着，够定位。

**不做什么**
不识别业务含义（不知道哪个参数是密码就只能按名字猜）。名字猜错了最坏结果是
多抹一个值，不影响功能 —— 反过来（少抹）才是事故，所以参数名表宁可宽一点。
"""
import re

# 协议头后面的 user:pass@ —— rtsp / http(s) / ftp 一起管，webhook 也走这条
_CRED_URL = re.compile(r'((?:rtsps?|https?|ftps?)://)[^@/\s]+@')

# URL 里按名字认定的敏感 query 参数。`?t=` 这类时间戳不在名单里：
# 它一变一个值，抹掉只会让人看不懂「这次请求是新是旧」。
_QUERY_SECRET = re.compile(
    r'([?&](?:api[_-]?token|access[_-]?token|token|auth|password|passwd|pwd'
    r'|secret|key|api[_-]?key|apikey|sig|signature)=)[^&\s#"\']+',
    re.I)

# 请求头形态：`Authorization: Bearer xxx`、`-headers ... Basic xxx`
_AUTH_HEADER = re.compile(r'((?:Bearer|Basic)\s+)[A-Za-z0-9\-._~+/=]{8,}', re.I)

# 登录后签出的会话 cookie（密钥是 token 本身，泄漏等价于泄漏 token）
_COOKIE = re.compile(r'(nvr_sess=)[^;\s"\']+')

# 运行期登记的字面量密钥（当前就是 api_token）。之所以要单独一遍字面替换：
# 它会以任意形态出现（日志正文、URL、命令行），正则认不出「这串就是凭证」。
_SECRETS = []


def add_secret(value):
    """登记一枚「原样出现也算泄漏」的凭证。重复登记无副作用。"""
    v = str(value or "")
    if len(v) >= 8 and v not in _SECRETS:
        _SECRETS.append(v)
    return v


def forget_secrets():
    """清空登记表（测试用；轮换时不必清，新旧都要抹）。"""
    del _SECRETS[:]


def mask_secret(value):
    """展示一枚凭证：头尾各留 4 位。和 auth.mask 同一口径，日志里好对照。"""
    v = str(value or "")
    if len(v) <= 12:
        return "***"
    return "%s…%s" % (v[:4], v[-4:])


def mask_text(text):
    """把一段文本里所有已知形态的凭证抹成 ***。幂等，可重复调用。"""
    s = str(text if text is not None else "")
    if not s:
        return s
    # 字面量先替换：token 可能正好出现在 `?token=<token>` 里，
    # 先抹掉它，后面的参数名规则就只需要处理「名字对但值不是当前 token」的情况。
    for sec in _SECRETS:
        if sec in s:
            s = s.replace(sec, mask_secret(sec))
    s = _CRED_URL.sub(r'\1***@', s)
    s = _QUERY_SECRET.sub(r'\1***', s)
    s = _AUTH_HEADER.sub(r'\1***', s)
    s = _COOKIE.sub(r'\1***', s)
    return s


def leaked(text):
    """这段文本里还有没有明文凭证（测试与自检用，生产路径不依赖它）。

    判据是「再抹一遍还会不会变」：已抹干净的文本过 mask_text 是恒等的，
    这样不必为「已脱敏的形态」再维护一套反向正则（`rtsp://***@` 本身也匹配
    user:pass 的形态，正向判断会永远返回 True）。
    """
    return mask_text(text) != str(text if text is not None else "")
