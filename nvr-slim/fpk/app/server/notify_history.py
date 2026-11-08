#!/usr/bin/env python3
"""通知发送历史落盘：把「今晚到底发没发出去」变成能查的事实（0.9.124）。

为什么必须落盘
--------------
`notify.py` 的 `recent` 只是进程内 20 条内存环，重启即清空。真机上排查
「半夜有人经过但手机没响」时，界面上那 5 行绿字既不能证明发过，也不能
证明没发过 —— 正是评审说的「不能给用户展示看起来正确、却没验证过的数据」。
通知属于事实链的一环，和录像台账一样，该有独立于进程的落盘记录。

为什么是 jsonl 而不是 SQLite
---------------------------
事件索引用 SQLite 是因为要按天重建投影；通知历史只追加、不更新、不跨行
合并，jsonl 一行一条最省事，也最容易兼容旧格式：损坏行直接跳过，不会像
一个坏掉的 db 文件那样让整份历史都读不出来。裁剪照 `link.py` 的
`link-done.jsonl` 范式：tmp + os.replace 原子重写，写到一半断电也不会留半行。

三条不变式
----------
1. **绝不记 webhook URL**：地址里常带 `?key=` / `?access_token=`，
   落一次盘就等于把凭据写进诊断包。只记平台名（feishu / dingtalk / …）。
2. **所有字符串统一脱敏**：异常信息里 urllib 会把完整 URL 带出来，
   所以 cam / cat / err 全部过 `redact.mask_text` 再写。
3. **落盘失败不拖垮派发**：磁盘满、目录不可写都只返回 False，由调用方
   记一行日志。通知线程绝不能因为写历史失败而停止发后面的通知。

跑：python app/server/notify_history.py
"""
import json
import os
import threading
import time

FILE_NAME = "notify-history.jsonl"
MAX_KEEP = 2000          # 超过就裁到 KEEP_AFTER_TRIM，别让文件无限长
KEEP_AFTER_TRIM = 1500
QUERY_MAX = 500          # 单次查询硬上限，防前端一次拉爆
_lock = threading.Lock()


def path_of(var_dir):
    return os.path.join(str(var_dir or "."), FILE_NAME)


def mask(s):
    """统一脱敏入口；redact 不在时（单测直接 import 本模块）也不许抛。"""
    try:
        import redact
        return redact.mask_text(str(s or ""))
    except Exception:
        return str(s or "")


def clean(rec):
    """把调用方给的记录收敛成固定形状，多出来的键一律不收。"""
    ts = rec.get("at")
    try:
        ts = float(ts) if ts is not None else time.time()
    except (TypeError, ValueError):
        ts = time.time()
    return {
        "at": round(ts, 3),
        "cam": mask(rec.get("cam"))[:200],
        "cat": mask(rec.get("cat"))[:64],
        "ok": bool(rec.get("ok")),
        "ms": max(0, int(rec.get("ms") or 0)),
        "err": mask(rec.get("err"))[:300],
        "tries": max(1, int(rec.get("tries") or 1)),
        "platform": str(rec.get("platform") or "generic")[:32],
    }


def append(var_dir, rec):
    """追加一条；返回 (ok, total)。total 是本次写后的估计条数，供调用方缓存。"""
    p = path_of(var_dir)
    line = json.dumps(clean(rec), ensure_ascii=False, separators=(",", ":"))
    with _lock:
        try:
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            n = _count(p)
            if n > MAX_KEEP:
                _trim(p)
                n = _count(p)
            return True, n
        except (OSError, ValueError, TypeError):
            return False, -1


def _read_lines(p):
    """读全部有效行；坏行跳过而不是整份作废（旧格式兼容）。

    errors="replace" 是这里的关键：以前用严格解码，文件里只要有一行坏
    UTF-8（断电写半行、老版本 GBK 残留）就整份 return []，等于把最后一条
    能证明「通知发出去过」的证据也一起丢了。现在坏字节变成替换符，
    json.loads 把它单独跳过，其余行照常读出来。
    """
    out = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and "at" in d:
                    out.append(d)
    except OSError:
        return []
    return out


def _count(p):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


def _trim(p):
    """只保留最后 KEEP_AFTER_TRIM 条；原子替换，写坏了不碰原文件。"""
    rows = _read_lines(p)[-KEEP_AFTER_TRIM:]
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for d in rows:
            f.write(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, p)


def query(var_dir, since=None, until=None, cam=None, cat=None,
          ok=None, limit=100):
    """按时间 / 摄像头 / 类别 / 成败过滤，最新在前。"""
    try:
        limit = max(1, min(QUERY_MAX, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    rows = _read_lines(path_of(var_dir))
    out = []
    for d in rows:
        try:
            at = float(d.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if since is not None and at < float(since):
            continue
        if until is not None and at > float(until):
            continue
        if cam and str(d.get("cam") or "") != str(cam):
            continue
        if cat and str(d.get("cat") or "") != str(cat):
            continue
        if ok is not None and bool(d.get("ok")) != bool(ok):
            continue
        out.append(d)
    out.sort(key=lambda d: float(d.get("at") or 0), reverse=True)
    return out[:limit]


def stats(var_dir):
    """启动时读一次就够：条数 / 最后一条时间 / 文件字节。轮询里不许调它。"""
    p = path_of(var_dir)
    rows = _read_lines(p)
    try:
        size = os.path.getsize(p)
    except OSError:
        size = 0
    ats = []
    for d in rows:
        try:
            ats.append(float(d.get("at") or 0))
        except (TypeError, ValueError):
            pass
    return {"total": len(rows), "last_at": max(ats) if ats else None,
            "bytes": size, "file": FILE_NAME}


if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp(prefix="nvr-nhist-")
    append(d, {"at": 1000, "cam": "院子", "cat": "person", "ok": True,
               "ms": 32, "platform": "feishu"})
    append(d, {"at": 1001, "cam": "院子", "cat": "vehicle", "ok": False,
               "err": "HTTP 500 on https://hook.example/x?key=SECRET",
               "platform": "generic"})
    print("stats:", stats(d))
    print("query(ok=False):", query(d, ok=False))
