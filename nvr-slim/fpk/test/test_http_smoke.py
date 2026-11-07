#!/usr/bin/env python3
"""端到端冒烟：真起一个服务进程，用 HTTP 打所有关键接口。

覆盖的是「以前静默失效、用户看不到报错」的那些点：
  - GET /api/config 有没有真的返回 JSON（不是 index.html）
  - POST 缺 X-NVR-Request 会不会被 403
  - 跨站 Host 会不会被 403
  - gpu 传 true 会不会被收敛回 "auto"
  - 超大 body 会不会 413
  - 清理 dry_run / 扫描 / 诊断导出 能不能出数据
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "app", "server", "nvr_slim.py")
PORT = 18787
HDR = {"X-NVR-Request": "1", "Content-Type": "application/json"}

fails = []
TOTAL = [0]


def req(path, method="GET", body=None, headers=None, port=None):
    url = "http://127.0.0.1:%d%s" % (port or PORT, path)
    data = None
    h = dict(HDR)
    if body is not None:
        data = json.dumps(body).encode()
    if headers is not None:
        h.update(headers)
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  ← " + str(detail)))
    if not cond:
        fails.append(name)


def main():
    var = tempfile.mkdtemp(prefix="nvrvar-")
    app = os.path.join(var, "app")
    ui = os.path.join(app, "ui")
    os.makedirs(ui, exist_ok=True)
    real_ui = os.path.join(HERE, "..", "app", "ui")
    shutil.copy(os.path.join(real_ui, "index.html"), os.path.join(ui, "index.html"))
    rec = os.path.join(var, "rec")
    day = time.strftime("%Y-%m-%d")
    os.makedirs(os.path.join(rec, "车库", day), exist_ok=True)
    # 一个"已封口"的假分片（只有大小，不参与解码）。
    # 文件名即分片起点，必须落在「今天已经过去」的时刻：写死 10-00-00 时，
    # 凌晨 00:xx 跑这套测试，分片起点在未来 → avail.full 判成无可用画面而误报。
    _t = time.localtime()
    _mark = max(0, _t.tm_hour * 3600 + _t.tm_min * 60 + _t.tm_sec - 60)
    _seg = "%02d-%02d-%02d.mp4" % (_mark // 3600, _mark % 3600 // 60, _mark % 60)
    with open(os.path.join(rec, "车库", day, _seg), "wb") as f:
        f.write(b"\0" * 4096)
    cfg = {"record_dir": rec, "record_enable": False, "cameras": [
        {"id": "cam-01", "name": "车库", "url": "rtsp://u:p@127.0.0.1:1/s1",
         "sub_url": "", "enabled": True, "full_days": 4, "clip_days": 90}]}
    with open(os.path.join(var, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    env = dict(os.environ, NVR_SIMULATE_GPU="qsv", TRIM_PKGVAR=var)
    p = subprocess.Popen([PY, SERVER, "--port", str(PORT), "--var", var,
                          "--app", app, "--ui", ui, "--host", "127.0.0.1"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        ok = False
        tok = ""
        for _ in range(60):
            time.sleep(0.25)
            # 0.9.120 起所有 /api/* 要凭证，凭证是服务启动时自己写下来的。
            # 先读它再探活：不读的话每一步都是 401，"服务没起来"是假象。
            try:
                with open(os.path.join(var, "secret.json"), encoding="utf-8") as f:
                    tok = json.load(f).get("api_token") or ""
            except (OSError, ValueError):
                tok = ""
            if tok:
                HDR["Authorization"] = "Bearer " + tok
            try:
                s, _b = req("/api/health")
                if s == 200:
                    ok = True
                    break
            except Exception:
                pass
        check("服务起来了 /api/health", ok)
        if not ok:
            return 1

        # 凭证本身：错的一律 401，对的放行，登录能换到 cookie
        s, b = req("/api/status", headers={"Authorization": "Bearer wrong-token"})
        check("错误凭证访问 /api/status 得 401", s == 401, (s, b[:80]))
        s, b = req("/api/auth/status")
        d = json.loads(b)
        check("凭证文件存在时 auth.status 不再要求粘贴",
              s == 200 and d.get("need_token") is False and tok, (s, b[:80]))
        s, b = req("/api/auth/rotate", "POST", {}, headers={
            "Content-Type": "application/json", "X-NVR-Request": "1",
            "Authorization": "Bearer wrong-token"})
        # 这里只验证"轮换不是谁都能按"；真按下去的副作用是让手上的 token 立刻失效，
        # 会把后面所有断言变成一串 401，所以这一条用错凭证打，不触发真轮换。
        check("轮换接口要求正确凭证（错凭证 401）", s == 401, (s, b[:80]))

        s, b = req("/api/config")
        d = json.loads(b)
        check("GET /api/config 返回 JSON", s == 200 and d.get("ok") is True, (s, b[:80]))
        u = (d.get("config", {}).get("cameras") or [{}])[0].get("url")
        check("GET /api/config 里 RTSP 凭据已脱敏", u and "u:p@" not in u, u)
        check("GET /api/config 带回分片长度", "record_segment_sec" in d.get("config", {}))

        s, b = req("/api/config", "POST", {"gpu": True, "concurrency": "2"})
        check("POST 配置（带 X-NVR-Request）成功", s == 200, (s, b[:120]))
        s, b = req("/static-count")
        # 确认服务端真的把 gpu 收敛回 "auto"
        s2, b2 = req("/api/status")
        st = json.loads(b2)
        check("gpu 布尔值被收敛为 auto", st["config"].get("gpu") == "auto", st["config"].get("gpu"))
        check("status 里的 config 已脱敏",
              "u:p@" not in json.dumps(st["config"], ensure_ascii=False))

        # 页面上的 CPU / 内存 / 核显必须来自这里，而不是 HTML 里写死
        res = st.get("res") or {}
        check("status 带 res 资源块", isinstance(res, dict) and res, list(st)[:8])
        check("res 含进程 CPU / 内存 / 整机 CPU 字段",
              all(k in res for k in ("proc_cpu", "mem_mb", "sys_cpu", "mem_total_mb")),
              sorted(res))
        check("res.hw 有可判定的 best / error",
              isinstance(res.get("hw"), dict)
              and ("best" in res["hw"]) and ("error" in res["hw"]),
              res.get("hw"))
        # 读不到必须是 None，绝不能是编出来的数字
        check("读不到的字段为 None 而非占位数字",
              res.get("proc_cpu") is None or isinstance(res.get("proc_cpu"), (int, float)),
              res.get("proc_cpu"))
        cams_res = st.get("cams") or {}
        check("status 带真实摄像头计数",
              isinstance(cams_res.get("total"), int) and isinstance(cams_res.get("recording"), int),
              cams_res)

        # 每路录制状态要能直接看到：进程在不在跑、上次 ffmpeg 报了什么。
        # 用户报「一条录像都没有」时，这是唯一能定位的入口。
        s, b = req("/api/link")
        lk = json.loads(b)
        rec = lk.get("record") or {}
        ch = rec.get("channels") or []
        has_fields = bool(ch) and all(
            k in ch[0] for k in ("name", "state", "error", "segments"))
        check("/api/link 带回每路录制状态（含 ffmpeg 错误）",
              has_fields, ch[:1] or rec)
        # 没 ffmpeg 是"所有通道都未运行"的最常见原因，必须能单独看出来
        check("缺 ffmpeg 时有独立标记", "ffmpeg_missing" in rec, sorted(rec))
        # 每路要带错误分类和给用户的建议，不能只甩英文原文
        has_kind = all(("kind" in c and "hint" in c) for c in ch) if ch else False
        check("每路带错误分类 kind / 建议 hint", has_kind,
              sorted(ch[0]) if ch else rec)
        # 自动剔除过的参数要如实上报，别让用户以为版本有 bug
        check("上报被自动跳过的 ffmpeg 参数", "bad_opts" in rec, sorted(rec))
        # 遮蔽区已下线：接口里不应再出现 redact
        check("配置里不再有输出遮蔽区 redact",
              "redact" not in json.dumps(st["config"]), "redact")

        # 录制诊断：没有摄像头地址时应给出明确的 400，而不是 500 或空响应
        s, b = req("/api/record/probe?id=__nope__")
        check("诊断接口对未知通道返回 400", s == 400, (s, b[:120]))

        s, b = req("/api/config", "POST", {"gpu": "off"},
                   headers={"X-NVR-Request": ""})
        check("缺 X-NVR-Request 被 403", s == 403, s)

        s, b = req("/api/restart", "POST", {}, headers={"Host": "evil.example.com"})
        check("非局域网 Host 被 403", s == 403, s)

        big = {"x": "a" * (5 * 1024 * 1024)}
        s, b = req("/api/config", "POST", big)
        check("超大 body 被 413", s == 413, s)

        s, b = req("/api/scan", "POST", {})
        d = json.loads(b)
        ch = (d.get("channels") or [{}])[0]
        check("扫描接口返回真实分片数", s == 200 and d.get("ok") and ch.get("files") == 1, d)

        s, b = req("/api/retention/cleanup", "POST", {"dry_run": True})
        d = json.loads(b)
        check("清理 dry_run 可预览", s == 200 and "freed_bytes" in d, list(d)[:6])

        s, b = req("/api/log/export")
        check("诊断导出可下载且脱敏",
              s == 200 and b"==== NVR " in b and b"u:p@" not in b, b[:60])

        # 0.9.116 日志分级：前端靠 lines 里的 [WARN]/[ERR] 上色，靠 warn/err 显示计数
        s, b = req("/api/log")
        d = json.loads(b)
        check("日志接口带分级字段",
              s == 200 and isinstance(d.get("lines"), list)
              and isinstance(d.get("warn"), int) and isinstance(d.get("err"), int),
              (s, list(d)[:8]))

        # 0.9.117 历史日志：从落盘文件尾部读，start/end 供前端往前翻
        s, b = req("/api/log/file?lines=50")
        d = json.loads(b)
        check("历史日志可读且带翻页坐标",
              s == 200 and d.get("ok") is True and isinstance(d.get("lines"), list)
              and isinstance(d.get("start"), int) and isinstance(d.get("end"), int)
              and isinstance(d.get("more"), bool),
              (s, [k for k in d][:8]))
        check("历史日志默认只给业务行（访问行会把它冲淡）",
              all(x.startswith("[nvr] ") for x in (d.get("lines") or [])),
              (d.get("lines") or [])[:2])
        s, b = req("/api/log/file?lines=50&access=1")
        d2 = json.loads(b)
        check("access=1 时连访问行一起给", s == 200 and d2.get("ok") is True,
              (s, (d2.get("lines") or [])[:1]))

        # 0.9.118 录像可信度：计数有真实出处（不是配置数），缺口可查
        s, b = req("/api/health")
        d = json.loads(b)
        check("健康接口带摄像头计数",
              s == 200 and isinstance(d.get("cams"), dict)
              and isinstance((d["cams"] or {}).get("recording"), int)
              and isinstance((d["cams"] or {}).get("stalled"), int),
              (s, d.get("cams")))
        s, b = req("/api/health/gaps")
        d = json.loads(b)
        check("缺口接口可用（没有录像也该返回 ok + 空清单）",
              s == 200 and d.get("ok") is True
              and isinstance(d.get("cams"), list) and isinstance(d.get("total"), dict),
              (s, list(d)[:8]))
        s, b = req("/api/health/gaps?date=2026/11/06")
        check("缺口接口拒绝坏日期", s == 200 and json.loads(b).get("ok") is False,
              (s, b[:60]))

        s, b = req("/api/link/run", "POST", {})
        d = json.loads(b)
        check("立即处理一批可用", s == 200 and d.get("ok") is True, (s, b[:100]))

        s, b = req("/api/live")
        d = json.loads(b)
        check("/api/live 正常", s == 200 and d.get("ok"), (s, b[:80]))

        # ---- 台账 / 缩时方案（Aqara 式） ----
        s, b = req("/api/config")
        d = json.loads(b)
        check("GET /api/config 带回 condense_mode 默认 virtual",
              d.get("config", {}).get("condense_mode") == "virtual",
              d.get("config", {}).get("condense_mode"))
        s, b = req("/api/ledger?cam=cam-01&date=" + day)
        d = json.loads(b)
        check("/api/ledger 返回窗口与可用性", s == 200 and d.get("ok") is True
              and isinstance(d.get("events"), list)
              and d.get("avail", {}).get("full"), (s, b[:120]))
        check("/api/ledger 模式解析为 virtual", d.get("mode") == "virtual", d.get("mode"))
        s, b = req("/api/archive")
        d = json.loads(b)
        check("/api/archive 返回归档面板载荷",
              s == 200 and d.get("ok") is True and "todo_count" in d
              and "next_run" in d and "policy" in d, (s, b[:120]))
        check("/api/archive 默认开启且保留 7 天",
              d.get("enable") is True and d.get("archive_days") == 7,
              (d.get("enable"), d.get("archive_days")))
        # 0.9.109：提前归档默认关（同一天不同时存在全时与归档）
        check("/api/archive 提前归档默认关闭", d.get("prefetch") is False,
              d.get("prefetch"))
        s, b = req("/api/config", "POST", {"archive_prefetch": True})
        s2, b2 = req("/api/config")
        check("archive_prefetch 可写入并回读（不是假设置）",
              s == 200 and json.loads(b2).get("config", {}).get("archive_prefetch") is True,
              (s, b[:100], b2[:160]))
        s, b = req("/api/archive")
        check("/api/archive 同步反映 prefetch 开关",
              json.loads(b).get("prefetch") is True, b[:120])
        req("/api/config", "POST", {"archive_prefetch": False})
        # 真跑必须异步：同步等结果在真机上跑 84 分钟才返回，链路早断了
        s, b = req("/api/archive/run", "POST", {"dry_run": False})
        ar = json.loads(b)
        check("POST /api/archive/run 真跑立刻回 202（异步启动）",
              s == 202 and ar.get("async") is True and ar.get("started") is True,
              (s, b[:120]))
        pr = {}
        for _ in range(40):
            s, b = req("/api/archive/progress")
            pr = json.loads(b)
            if (pr.get("result") or {}).get("done"):
                break
            time.sleep(0.25)
        check("/api/archive/progress 给出 done 摘要",
              (pr.get("result") or {}).get("done") is True, pr)
        check("progress 只回内存态（不重算 plan）",
              "todo" not in pr and "todo_count" not in pr, sorted(pr))
        s, b = req("/api/storage/usage")
        d = json.loads(b)
        check("/api/storage/usage 三类占用 + 台账",
              s == 200 and "archive" in d and "ledger" in d, (s, b[:120]))
        s, b = req("/api/ledger?cam=cam-01&date=bad-date")
        d = json.loads(b)
        check("/api/ledger 拒绝坏日期", s == 200 and d.get("ok") is False, (s, b[:80]))
        s, b = req("/api/ledger?cam=no-such&date=" + day)
        d = json.loads(b)
        check("/api/ledger 拒绝未知通道", s == 200 and d.get("ok") is False, (s, b[:80]))

        s, b = req("/")
        check("首页可打开", s == 200 and b"<html" in b.lower(), b[:40])

        s, b = req("/../config.json")
        check("静态目录穿越被拦（回落 index）", b"<html" in b.lower(), b[:60])

    finally:
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
        out = (p.stdout.read() or b"").decode("utf-8", "replace")
        if fails:
            print("\n---- 服务日志 ----\n" + out[-3000:])
        shutil.rmtree(var, ignore_errors=True)
    print("\n结果：%d 项通过，%d 项失败" % (TOTAL[0] - len(fails), len(fails)))
    print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
