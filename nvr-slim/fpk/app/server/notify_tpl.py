#!/usr/bin/env python3
"""通知消息模板：按目标平台给最小 body，别再一份「胖 JSON」打天下（0.9.124）。

为什么要拆出来
--------------
`notify.py` 原本把飞书 / 钉钉 / 企业微信 / Server酱 四套键塞进同一个 body，
图的是「开箱即显示」。但平台各不相同：钉钉对未知键宽容，飞书的部分机器人
会校验 msg_type 与 content 的形状，Server酱 只认 title / desp，Bark 只认
title / body。多余字段轻则被忽略、重则整条拒收，而且用户没法改文案。

所以按 webhook 的 host 认平台，每个平台只发它自己那套最小 body；认不出来
（自建钩子、内网转发、本地测试服务器）就回退原来的胖 JSON —— 这是兼容底线，
已有的部署不该因为升级就收不到通知。

自动识别有它认不出的情况：钉钉 / 飞书的自定义机器人常被公司网关代理成
`https://mrobot.example.com/...`，host 已经不是官方域名，此时只能由人在设置页
显式指定平台。所以 `platform="auto"` 走识别，其余值直接按指定平台出 body。

三条不变式
----------
1. **纯计算**：不读配置、不读盘、不看时钟。时间从 item["t"] 由调用方传进来，
   单测喂常量就能验格式，不用起服务也不用等真实时间。
2. **非法模板回退，不抛异常**：自定义模板里的未知占位符、超长文本、坏 URL
   都退回默认文案。派发循环不能因为一句模板写错就丢掉整条通知。
3. **URL 里的密钥不进消息体**：平台只按 host 判定，query / path 一概不取，
   `?key=xxx` 不会被拼进文本。

跑：python app/server/notify_tpl.py
"""
import re
import time
from urllib.parse import urlsplit

# host 判定用「结尾匹配」而不是包含：钉钉自定义机器人可能走公司代理域名，
# 但 open.feishu.cn.evil.example 这种 obviously 不该被认成飞书。
PLATFORM_HOSTS = (
    ("feishu", ("open.feishu.cn", "open.larksuite.com")),
    ("dingtalk", ("oapi.dingtalk.com",)),
    ("wecom", ("qyapi.weixin.qq.com",)),
    ("serverchan", ("sctapi.ftqq.com", "sc.ftqq.com")),
    ("bark", ("api.day.app",)),
)
GENERIC = "generic"
# 平台可选值：auto 之外都是「强制按这套 body 发」。顺序即设置页下拉顺序，
# 前端不另抄一份 —— 服务端把这张表原样放进 /api/status，界面照它渲染。
PLATFORMS = ("auto", "feishu", "dingtalk", "wecom", "serverchan", "bark",
             GENERIC)
PLATFORM_LABEL = {"auto": "自动识别", "feishu": "飞书", "dingtalk": "钉钉",
                  "wecom": "企业微信", "serverchan": "Server酱",
                  "bark": "Bark", GENERIC: "通用 JSON"}

# 标签与 notify.CAT_LABEL 保持一致，但这里单独一份：模板模块不该反向依赖
# 分发器（notify.py 要 import 本模块，反过来就成了循环）。
CAT_LABEL = {"person": "人员", "two_wheeler": "两轮车",
             "vehicle": "车辆", "motion": "移动侦测"}

DEFAULT_TPL = "【NVR · {cam}】检测到{label} · {time}"
TITLE = "NVR 检测通知"

# 占位符白名单：只许这几个，多一个就整份模板退回默认。
# 不放 {ts} / {url} 之类，是为了不让模板作者有机会把 webhook 地址带进消息。
VARS = ("cam", "cat", "label", "time", "date")
VAR_RE = re.compile(r"\{([^{}]*)\}")
MAX_TPL_LEN = 500
MAX_TEXT_LEN = 1000
MAX_FIELD_LEN = 200


def platform_of(url):
    """按 host 认平台；认不出来一律 generic（保持旧的胖 JSON 行为）。"""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().strip(".")
    except Exception:
        return GENERIC
    if not host:
        return GENERIC
    for name, hosts in PLATFORM_HOSTS:
        for h in hosts:
            if host == h or host.endswith("." + h):
                return name
    return GENERIC


def norm_platform(v):
    """配置里的平台值：认不出来的按 auto，绝不因为写错一个词就不发通知。"""
    s = str(v or "").strip().lower()
    return s if s in PLATFORMS else "auto"


def effective_platform(url, platform=None):
    """实际发出去的平台：显式指定优先，否则按 host 识别。

    返回 (平台, 是否来自自动识别)。第二个值给状态接口用，界面才能说清
    「这个平台是我认出来的，还是你手选的」——两者的排障方向完全不同。
    """
    p = norm_platform(platform)
    if p != "auto":
        return p, False
    return platform_of(url), True


def describe(url, tpl=None, platform=None):
    """给设置页看的「这一条通知实际会怎么发」：平台来源 + 模板是否被回退。

    不在这里做发送，也不读配置：调用方（notify.Notifier.status）把当前
    notify 块递进来即可。界面上那句「实际平台：钉钉（手选）· 文案：自定义」
    必须来自这里，不许前端自己再猜一遍。
    """
    pl, auto = effective_platform(url, platform)
    _tpl, why = normalize_tpl(tpl)
    return {"platform": pl, "label": PLATFORM_LABEL.get(pl, pl),
            "auto": auto, "forced": norm_platform(platform),
            "custom_template": bool(tpl) and not why,
            "template_why": why, "vars": list(VARS),
            "platforms": [{"v": v, "label": PLATFORM_LABEL.get(v, v)}
                          for v in PLATFORMS]}


def normalize_tpl(tpl):
    """校验自定义模板，返回 (可用模板, 原因)。原因非空表示回退了默认。"""
    if tpl is None or tpl == "":
        return DEFAULT_TPL, ""
    if not isinstance(tpl, str):
        return DEFAULT_TPL, "模板不是字符串"
    if len(tpl) > MAX_TPL_LEN:
        return DEFAULT_TPL, "模板超过 %d 字" % MAX_TPL_LEN
    unknown = [v for v in VAR_RE.findall(tpl) if v not in VARS]
    if unknown:
        return DEFAULT_TPL, "未知占位符 %s" % ",".join(sorted(set(unknown))[:3])
    if not tpl.strip():
        return DEFAULT_TPL, "模板为空"
    return tpl, ""


def text_of(item, tpl=None):
    """把事件渲染成一句文本。item["t"] 是 epoch 秒，由 offer() 入队时定死。"""
    tpl, _why = normalize_tpl(tpl)
    cam = str(item.get("cam") or "")[:MAX_FIELD_LEN]
    cat = str(item.get("cat") or "")[:MAX_FIELD_LEN]
    label = CAT_LABEL.get(cat, cat)[:MAX_FIELD_LEN]
    ts = item.get("t")
    try:
        ts = float(ts) if ts is not None else time.time()
    except (TypeError, ValueError):
        ts = time.time()
    lt = time.localtime(ts)
    values = {
        "cam": cam, "cat": cat, "label": label,
        "time": time.strftime("%H:%M:%S", lt),
        "date": time.strftime("%Y-%m-%d", lt),
    }
    out = VAR_RE.sub(lambda m: values.get(m.group(1), m.group(0)), tpl)
    return out[:MAX_TEXT_LEN]


def render(url, item, tpl=None, platform=None):
    """返回 (payload_dict, platform)。payload 一定是可直接 json.dumps 的 dict。

    platform 传了非 auto 的值就按它出 body：公司网关把机器人地址代理成
    自家域名时，host 识别不出来，只能由人指定。
    """
    text = text_of(item, tpl)
    p, _auto = effective_platform(url, platform)
    if p == "feishu":
        body = {"msg_type": "text", "content": {"text": text}}
    elif p == "dingtalk":
        body = {"msgtype": "text", "text": {"content": text}}
    elif p == "wecom":
        # 企业微信群机器人与钉钉同形，但额外支持 mentioned_list；不主动 @ 人，
        # 避免在没人值守的夜里把通知变成打扰。
        body = {"msgtype": "text", "text": {"content": text}}
    elif p == "serverchan":
        body = {"title": TITLE, "desp": text}
    elif p == "bark":
        body = {"title": TITLE, "body": text, "group": "NVR"}
    else:
        # 兼容底线：认不出来的目标仍收到原来那份四套键齐全的胖 JSON。
        body = {"msg_type": "text", "content": {"text": text},
                "msgtype": "text", "text": {"content": text},
                "title": TITLE, "desp": text}
    return body, p


if __name__ == "__main__":
    it = {"cam": "院子", "cat": "person", "t": 1793500000.0}
    for u in ("https://open.feishu.cn/open-apis/bot/v2/hook/x?key=SECRET",
              "https://oapi.dingtalk.com/robot/send?access_token=SECRET",
              "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET",
              "https://sctapi.ftqq.com/SECRET.send",
              "https://api.day.app/SECRET",
              "http://127.0.0.1:8080/hook"):
        body, p = render(u, it)
        print("%-12s %s" % (p, body))
