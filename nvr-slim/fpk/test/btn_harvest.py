#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置项采集器：把声明表（button_matrix.ROWS）真跑一遍，出一份报告。

为什么要有它
------------
声明表说的是「该验什么」，执行器（test_btn_effect.py）只覆盖 6 个衔接/录制项。
剩下十几个设置项没人真的按过：配置键改了之后服务端认不认、落不落盘、
运行中的对象跟不跟得上，全靠人肉点设置页 —— 而「改了不生效」恰恰是
第 17~20 轮连抓三次的那类 bug（接口回 ok、配置也落盘，消费端仍按旧值跑）。

采集器把这条链跑成一条流水线：

    读盘取原值 → 换成另一个合法值 POST /api/config → 等 500ms
    → 取 /api/config + /api/status + /api/link 三份快照 → 还原原值
    → 喂 button_matrix.verify_row 判四层 → 出报告

三个口径，别改成别的
--------------------
1. **「另一个合法值」由类型和范围推出，不写死常量**：分片长度在 60~900 秒里
   挑、并发在 1~3 里挑、枚举在白名单里挑下一个。写死 `120` 的话，配置里
   本来就是 120 时 POST 同值不进 changed，用例会变成「什么都没按」的假绿。
2. **还原必须验**：改完不还原，跑一遍就等于把本地配置搅乱；跑完还会整份比对
   磁盘 config.json（除 updated_at），不一致就判这次采集不算数。测试机上
   不小心把录像总闸留在关闭状态，比没跑过更糟。
3. **还原值从磁盘取，不从 /api/config 取**：接口返回的 cameras.url 是脱敏后的
   `rtsp://***@...`，拿它回推会把真地址写坏 —— 这正是批次 2 那条「明文凭据
   不出接口」的配套要求。本机采集器读得到 var/config.json，就用磁盘那份。

只打本机回环
------------
服务绑 127.0.0.1、端口由系统分配、var 用临时目录，跑完即删。本机没有 ffmpeg
时服务照样起、Link/Recorder 对象照样建，链路与 152 同构（第 17 轮已验证）。
**不要给这个脚本加 --base / SSH 之类的能力**：改配置是有副作用的动作，
一旦能指向测试机甚至生产机，「跑一遍看看」就会变成一次真实的配置抖动。
真机采集（/proc 里的 ffmpeg argv）要单独设计，见 ROADMAP。

跑法
----
    python test/btn_harvest.py                      # 全表，报告写进 test-reports/
    python test/btn_harvest.py --only record_enable,cameras
    python test/btn_harvest.py --skip record_dir --keep
"""
import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "server"))
import button_matrix as BM          # noqa: E402
import runtime                      # noqa: E402

SERVER = os.path.join(ROOT, "app", "server", "nvr_slim.py")
PY = sys.executable

# 前端 pushCfg 有 250ms 防抖，服务端落盘与 reconfigure 之后还有一拍 killa/spawn。
# 500ms 是「采得到新值」与「不把 20 项跑成三分钟」的折中；采不到会判 fail/unknown，
# 不会因为等太短而悄悄判通过。
SETTLE_SEC = 0.5
RESTORE_SETTLE = 0.4

# 个别项的消费端不是「请求里就生效」，而是下一拍巡检才轮到它：
# snapshot_interval 由 _pump 每 2 秒重读配置、重建取帧 tap，tap 的 interval 才会变。
# 等不够就判成 fail，等于把「设计如此」记成 bug —— 给这一项单独放宽，等两拍。
SETTLE_BY_KEY = {"snapshot_interval": 3.5}


class Skip(Exception):
    """这一项在当前环境里没法安全地改（例如找不到合法目录）。如实记 unknown。"""


# ---------------------------------------------------------------- 取值策略
#
# 每个 picker 收 (当前值, 磁盘配置, 上下文)，返回 (要 POST 的值, 期望落盘值)。
# 「期望落盘值」是交给 verify_row 的第二层断言用的：只判「变了」会漏掉
# 「变成别的合法值」这种偏差（比如 coerce 夹到了范围边界）。

def _pair(v):
    return v, v


def _alt_num(cur, cands, lo, hi):
    """在 [lo, hi] 里挑一个与当前值不同的数。候选都不合适就兜底 ±1。"""
    try:
        cur = int(cur)
    except (TypeError, ValueError):
        cur = lo
    for c in cands:
        if lo <= c <= hi and c != cur:
            return c
    return cur + 1 if cur + 1 <= hi else cur - 1


def _flip_bool(cur, _cfg, _ctx):
    v = not bool(cur)
    return _pair(v)


def _enum_alt(values):
    def pick(cur, _cfg, _ctx):
        cur = str(cur or values[0])
        for v in values:
            if v != cur:
                return v, v
        return values[0], values[0]
    return pick


def _pick_cleanup_at(cur, _cfg, _ctx):
    # 只换时刻不换格式：落盘后由 retention.parse_hhmm 解析，格式坏了清理链会天天抛异常
    for c in ("04:30", "02:15", "05:45"):
        if c != str(cur or ""):
            return c, c
    return "03:00", "03:00"


def _pick_record_dir(_cur, _cfg, ctx):
    d = os.path.join(ctx["var"], "alt-rec")
    os.makedirs(d, exist_ok=True)
    return d, d


def _pick_detect(cur, _cfg, _ctx):
    """整个 detect 块推回去，只翻总闸。

    只推 {"enable": ...} 会让 norm_detect 把类别/阈值/trigger 全部打回默认 ——
    采集器不该顺手清掉别人的调参，所以原块整份带上再改一个字段。
    """
    blk = json.loads(json.dumps(cur or {}))
    blk["enable"] = not bool(blk.get("enable"))
    return blk, blk["enable"]


def _pick_notify(cur, _cfg, _ctx):
    blk = json.loads(json.dumps(cur or {}))
    blk["cooldown"] = _alt_num(blk.get("cooldown"), (45, 90, 120), 30, 600)
    return blk, blk["cooldown"]


def _pick_cameras(cur, _cfg, _ctx):
    cams = json.loads(json.dumps(cur or []))
    if not cams:
        raise Skip("配置里没有摄像头，改不了这一项")
    expect = []
    for i, c in enumerate(cams):
        base = str(c.get("name") or ("通道 %d" % (i + 1)))
        # 名字是这一项的锚点（见声明表该行的说明）。加后缀而不是换整串，
        # 免得「改个名」把用户按名字认路的那点信息也一起抹了。
        c["name"] = (base + "·采")[:40]
        expect.append(c["name"])
    return cams, expect


PICKERS = {
    "record_enable": _flip_bool,
    "record_dir": _pick_record_dir,
    "record_segment_sec": lambda cur, _c, _x: _pair(
        _alt_num(cur, (120, 180, 240), 60, 900)),
    "snapshot_interval": lambda cur, _c, _x: _pair(
        _alt_num(cur, (5, 6, 7), 1, 10)),      # 服务端夹取 1~10
    "cleanup_at": _pick_cleanup_at,
    "record_frag": _flip_bool,
    "link_mode": _enum_alt(("live", "scheduled")),
    "clip_style": _enum_alt(("join", "split")),
    "condense_mode": _enum_alt(("virtual", "materialize", "off")),
    "archive_enable": _flip_bool,
    "archive_days": lambda cur, _c, _x: _pair(
        _alt_num(cur, (9, 11, 5), 1, 90)),
    "archive_batch": lambda cur, _c, _x: _pair(
        _alt_num(cur, (80, 150, 60), 10, 500)),
    "archive_prefetch": _flip_bool,
    "link_interval_h": lambda cur, _c, _x: _pair(
        _alt_num(cur, (3, 4, 8), 1, 24)),
    "link_max_queue": lambda cur, _c, _x: _pair(
        _alt_num(cur, (333, 444), 20, 2000)),
    "concurrency": lambda cur, _c, _x: _pair(
        _alt_num(cur, (3, 2, 1), 1, 3)),       # 服务端夹取 1~3
    "gpu": _enum_alt(("auto", "qsv", "off")),
    "detect": _pick_detect,
    "notify": _pick_notify,
    "cameras": _pick_cameras,
}


# ---------------------------------------------------------------- 本地服务

class Srv:
    """一次性本地服务：临时 var、系统分配端口、跑完删目录。"""

    def __init__(self):
        self.var = tempfile.mkdtemp(prefix="nvrharvest-")
        self.proc = None
        self.token = ""
        self.port = 0
        # 服务日志写文件而不是 PIPE：20 项采集期间服务端会一直打日志，
        # 管道缓冲区一满，服务端就会卡在 write() 上 —— 表现是「改值请求永远不返回」，
        # 看起来像产品死锁，其实是采集器自己没读管道。
        self._log = None
        self.log_path = os.path.join(self.var, "server.log")

    # ---- 起停 ----------------------------------------------------
    def start(self):
        app = os.path.join(self.var, "app")
        ui = os.path.join(app, "ui")
        os.makedirs(ui, exist_ok=True)
        shutil.copy(os.path.join(ROOT, "app", "ui", "index.html"),
                    os.path.join(ui, "index.html"))
        rec = os.path.join(self.var, "rec")
        os.makedirs(rec, exist_ok=True)
        # 两路：单路写法会让「只更新了第一路」这类 bug 混过去
        cfg = {
            "record_dir": rec,
            "record_enable": True,
            "record_segment_sec": 300,
            "snapshot_interval": 10,
            "cameras": [
                {"id": "cam-01", "name": "采集甲", "url": "rtsp://u:p@127.0.0.1:1/s1",
                 "sub_url": "", "enabled": True, "full_days": 4, "clip_days": 90},
                {"id": "cam-02", "name": "采集乙", "url": "rtsp://u:p@127.0.0.1:2/s2",
                 "sub_url": "", "enabled": True, "full_days": 4, "clip_days": 90},
            ],
        }
        with io.open(os.path.join(self.var, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        # 用产品自己的 merge_defaults 补齐其余键，而不是只写上面这几项：
        # 只有「磁盘原值」里每个键都真实存在，还原才能拿它当目标值。
        # 否则 cleanup_at / record_frag 这些没显式写的键，原值是 None，
        # 还原时 POST null 会被 coerce 打回默认值 —— 看起来像「还原失败」，
        # 其实是采集器自己没把起始状态铺全。
        runtime.merge_defaults(self.var)

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        # NVR_SIMULATE_GPU：没有核显时 auto 会强制单路，concurrency 改了 effective
        # 也不动，那一项会被判成「运行态没跟上」——那是环境限制，不是 bug。
        env = dict(os.environ, NVR_SIMULATE_GPU="qsv", TRIM_PKGVAR=self.var)
        self._log = io.open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [PY, SERVER, "--port", str(self.port), "--var", self.var,
             "--app", app, "--ui", ui, "--host", "127.0.0.1"],
            stdout=self._log, stderr=subprocess.STDOUT, env=env)
        for _ in range(80):
            time.sleep(0.25)
            if self.proc.poll() is not None:
                break
            try:
                with io.open(os.path.join(self.var, "secret.json"), encoding="utf-8") as f:
                    self.token = json.load(f).get("api_token") or ""
            except (OSError, ValueError):
                pass
            if not self.token:
                continue
            try:
                if self.get("/api/health")[0] == 200:
                    self.canonicalize()
                    return self
            except Exception:
                pass
        out = ""
        try:
            with io.open(self.log_path, encoding="utf-8", errors="replace") as f:
                out = f.read()
        except OSError:
            pass
        raise RuntimeError("本地服务没起来（端口 %s）：%s" % (self.port, out[-800:]))

    def canonicalize(self):
        """把起始状态过一遍服务端强校，让磁盘上就是「产品自己的形态」。

        嵌套块推上去会被 norm_cameras / norm_detect 补一批默认字段（transport、
        clip_mode、detect.confirm…）。这些字段本来不存在，采集器若拿「还没规范过」
        的配置当标准答案，每一项还原回来都会被判成「没对齐」——
        那不是值没回去，是标准答案抄错了版本。
        """
        cfg = self.disk()
        body = {k: cfg[k] for k in ("cameras", "detect", "notify") if k in cfg}
        if body:
            self.post("/api/config", body)
            time.sleep(0.2)

    def stop(self, keep=False):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log is not None:
            self._log.close()
            self._log = None
        if not keep:
            shutil.rmtree(self.var, ignore_errors=True)

    # ---- HTTP ----------------------------------------------------
    def _req(self, path, method="GET", body=None):
        h = {"X-NVR-Request": "1", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                   data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path):
        return self._req(path)

    def getj(self, path):
        code, raw = self._req(path)
        if code != 200:
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None

    def post(self, path, body):
        return self._req(path, "POST", body)

    def disk(self):
        with io.open(os.path.join(self.var, "config.json"), encoding="utf-8") as f:
            return json.load(f)

    def snapshots(self):
        """三份快照合成一个 dict；任一取不到就整份算「没采全」。

        半份快照比没有更危险：缺的那一层会被 verify_row 判成 unknown，
        但如果只缺一个键，人容易以为是产品问题。宁可整份作废。
        """
        cfg = self.getj("/api/config")
        status = self.getj("/api/status")
        link = self.getj("/api/link")
        if not isinstance(cfg, dict) or not isinstance(status, dict) \
                or not isinstance(link, dict):
            return None
        return {"config": cfg.get("config") or {}, "status": status, "link": link}

    def env(self):
        """当前环境能提供哪些观测点 —— 报告里要写清，否则 unknown 会被当成产品问题。"""
        st = self.getj("/api/status") or {}
        link = self.getj("/api/link") or {}
        rec = link.get("record") or {}
        return {"frames": isinstance(st.get("frames"), dict),
                "recorder": isinstance(rec, dict) and bool(rec.get("channels")),
                "ffmpeg": not rec.get("ffmpeg_missing", True)}


# ---------------------------------------------------------------- 采集

def harvest_one(srv, row):
    """改一项、采三份快照、还原、判四层。返回 (结果, 还原是否成功)。

    原值每项现读磁盘，不吃开工时那份快照：相邻两项的配置会互相影响
    （比如 record_dir 那一项改完再还原，磁盘已经被整份重写过一次），
    拿旧快照当原值会把「其实已经对了」判成还原失败。
    """
    key = row["key"]
    pick = PICKERS.get(key)
    disk_cfg = srv.disk()
    orig = disk_cfg.get(key)
    if pick is None:
        return BM._result(row, "unknown", "没有取值策略，这一项还没接采集", []), True
    try:
        post, expect = pick(orig, disk_cfg, {"var": srv.var})
    except Skip as e:
        return BM._result(row, "unknown", str(e), []), True

    before = srv.snapshots()
    code, raw = srv.post("/api/config", {key: post})
    changed, err = None, ""
    if code == 200:
        try:
            changed = json.loads(raw.decode("utf-8", "replace")).get("changed")
        except ValueError:
            err = "POST 回的不是 JSON"
    else:
        err = "POST /api/config 返回 %s：%s" % (
            code, raw[:120].decode("utf-8", "replace"))
    time.sleep(SETTLE_BY_KEY.get(key, SETTLE_SEC))
    after = srv.snapshots()

    # 先还原再判定：万一 verify_row 自己抛异常，配置也已经回去了
    rcode, rraw = srv.post("/api/config", {key: orig})
    time.sleep(RESTORE_SETTLE)
    restored = (rcode == 200 and srv.disk().get(key) == orig)

    result = BM.verify_row(row, before, after, changed,
                           expect if changed is not None else BM.MISSING)
    if err:
        result = dict(result, state="unknown", reason=err,
                      checks=result.get("checks") or [])
    return result, restored


def diff_keys(before, after):
    """两份磁盘配置的差异键。updated_at 是每次保存自动写的时间戳，不算差异。"""
    keys = set(before) | set(after)
    return sorted(k for k in keys
                  if k != "updated_at" and before.get(k) != after.get(k))


def run(srv, rows, only=None, skip=None):
    disk_before = srv.disk()
    env = srv.env()
    items, unrestored = [], []
    for row in rows:
        if only and row["key"] not in only:
            continue
        if skip and row["key"] in skip:
            items.append(BM._result(row, "unknown", "按 --skip 跳过，本轮不采集", []))
            continue
        result, restored = harvest_one(srv, row)
        # 逐项打一行：20 项跑十几秒，卡住时没有这一行就只能看到「脚本没反应」
        print("  %-18s %-8s %s" % (row["key"], result["state"],
                                   "（还原失败）" if not restored else ""))
        sys.stdout.flush()
        if not restored:
            unrestored.append(row["key"])
            result = dict(result, state="unknown",
                          reason=(result["reason"] + "；" if result["reason"] else "")
                          + "还原失败，磁盘配置可能已偏离采集前状态")
        items.append(result)
    diff = diff_keys(disk_before, srv.disk())
    report = BM.roll_up(items)
    report["env"] = env
    report["restore"] = {"ok": not diff and not unrestored,
                         "diff": diff, "unrestored": unrestored}
    report["meta"] = {"version": (srv.getj("/api/status") or {}).get("version"),
                      "port": srv.port, "settle_ms": int(SETTLE_SEC * 1000),
                      "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    return report


def summary(report):
    """给终端看的一页纸：结论 + 未通过项 + 部分通过的原因 + 环境限制 + 还原结果。"""
    lines = [BM.summary(report)]
    # 部分通过必须逐条列出来。只写一行「通过 9 / 部分 11」会被读成「全过了」，
    # 而这 11 项恰恰是「只验到落盘、运行态没有出口或没采」的那些 ——
    # 它们和「验到了」是两种结论，混在一起就等于把验收表变回一张回执。
    partials = [x for x in report["items"] if x["state"] == "partial"]
    if partials:
        lines.append("  部分通过 %d 项（只验到落盘，运行态没采到）：" % len(partials))
        for x in partials:
            lines.append("    - %s %s：%s" % (x["key"], x["label"], x["reason"][:90]))
    env = report.get("env") or {}
    if not env.get("frames"):
        lines.append("  环境：没有取流（无 ffmpeg 或未起 ingest），"
                     "status.frames 为空 —— 依赖取帧观测点的项只能判未知")
    r = report.get("restore") or {}
    if r.get("ok"):
        lines.append("  还原：全部恢复到采集前的磁盘配置")
    else:
        lines.append("  还原：未完全恢复！差异键 %s" % (r.get("diff") or r.get("unrestored")))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="设置项采集器（只打本机回环）")
    ap.add_argument("--only", default="", help="只采这些键（逗号分隔）")
    ap.add_argument("--skip", default="", help="跳过这些键（逗号分隔）")
    ap.add_argument("--report", default="", help="报告路径，默认 test-reports/btn-matrix-<时间>.json")
    ap.add_argument("--keep", action="store_true", help="保留临时 var 目录，便于看日志")
    a = ap.parse_args(argv)

    only = {x.strip() for x in a.only.split(",") if x.strip()} or None
    skip = {x.strip() for x in a.skip.split(",") if x.strip()}
    srv = Srv()
    try:
        srv.start()
        report = run(srv, BM.ROWS, only=only, skip=skip)
    finally:
        srv.stop(keep=a.keep)

    print(summary(report))
    path = a.report or os.path.join(
        os.path.normpath(os.path.join(ROOT, "..", "..")), "test-reports",
        "btn-matrix-%s.json" % time.strftime("%Y%m%d-%H%M"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print("  报告：%s" % path)
    except OSError as e:
        print("  报告写入失败（不影响判定）：%r" % e)
    return 0 if (report["ok"] and report["restore"]["ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
