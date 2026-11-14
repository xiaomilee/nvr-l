#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可写缓存的落点（0.9.141）。

为什么要有这一层
----------------
0.9.140 之前，凡是"程序自己攒出来、能再生"的文件全落在 `@appdata/nvr`：
事件截图 453MB、remux 缓存、抽帧临时文件、快照。用户设了存储路径（`record_dir`），
指望"大文件都在盘上"，结果 `@appdata` 一路涨到 15GB —— 而这块通常是系统盘
（`/vol1` 根分区），涨满之后录制、索引、网页一起崩。生产机 remux 单项 14GB 就是
移动端每播一次就多一份副本、且**从来没有删除逻辑**攒出来的。

所以这里立一条规矩：

    <record_dir>/_cache/{shots,snapshots,remux,tmp}

  - `shots`     事件截图与检测框旁挂 json
  - `snapshots` 首页快照（每 N 秒覆盖写，天生有界）
  - `remux`     移动端播放用的 faststart 重封装副本
  - `tmp`       抽帧 / 浓缩 / 导出的临时文件

配置目录只留"必需且极小"的东西（config.json / secret / port.txt / 模型 / 日志 /
备份）。判断标准不是大小，而是**丢了会不会让程序起不来**：配置丢了起不来，缓存丢了
下一分钟就重新生成。

三条边界
--------
1. **绝不创建 `record_dir` 本身**。外接盘拔掉时它的挂载点还在，`os.makedirs`
   会在系统盘上把目录建出来 —— 于是"盘掉线"变成"悄悄写进系统盘"，比直接失败更糟。
   `record_dir` 必须是已存在的目录，否则一律走回退。
2. **写探测，不靠 `isfile`**。权限被 ACL 改掉、只读重挂、配额打满，`os.path.isdir`
   全都返回 True。每 20 秒真写一个字节才算数。
3. **回退不等于正常**。盘不可用时回退到 `var/_cache`，但状态得能被 `/api/status`
   看到，日志里也要留一句 —— 否则用户只会觉得"事件图怎么时有时无"。

盘恢复后，回退期攒在 `var/_cache` 的东西会被搬回去（`_merge_back`），
不会长期分裂成两份。
"""
import os
import shutil
import threading
import time

# 顶层目录名。以 `_` 开头：与摄像头目录（`cam1/`、`_archive/`、`_ledger/`）
# 排在一起时一眼能看出是程序自己管的，用户清理时也知道这个能删。
ROOT_NAME = "_cache"

# 子目录 → 用途说明（给 UI / 诊断页直接展示，不用再维护第二份文案）
KINDS = {
    "shots": "事件截图",
    "snapshots": "首页快照",
    "remux": "播放重封装",
    "tmp": "临时文件",
}

# 0.9.140 及以前直接摊在 var 下的目录名 → 新子目录
LEGACY = (
    ("event_shots", "shots"),
    ("snapshots", "snapshots"),
    ("remux", "remux"),
    ("tmp", "tmp"),
)

# 一次性迁移完成的标记。放在 var 而不是 _cache 里：跟着配置目录走，
# 换盘/清盘之后不会因为标记没了而把已经在盘上的东西又"迁移"一遍。
MIGRATE_MARK = ".cache-migrated"

PROBE_TTL = 20.0            # 可写探测的缓存时长（秒）
PROBE_FILE = ".w"
STATS_TTL = 60.0            # 占用统计的缓存时长（秒）

# remux 上限。5GB 是"8 路 × 一周"量级的经验值：单份 5 分钟 720p 约 60MB，
# 正常使用下真正会被反复点开的片源集中在最近几天，剩下的全是垃圾。
REMUX_MAX_BYTES = 5 * 1024 ** 3
REMUX_MAX_AGE = 7 * 86400
# 最近 10 分钟内动过的文件不动它：可能正被播放中的请求持有。
# Linux 上删掉打开的文件不会出错（句柄仍有效），但会让这次播放变成"白播一遍"，
# 下次还得再转一次 —— 用户感知是"越清越卡"。
REMUX_HOLD = 600.0

_LOCK = threading.Lock()
_BIND = {"store": None, "log": None}
_STATE = {
    "root": None,       # 上次解析出的根
    "at": 0.0,          # 解析时刻
    "var": None,        # 上次解析用的 var_dir（换了目录必须重新解析）
    "on_store": True,   # 当前是不是真的在存储路径上
    "why": "",          # 回退原因（给状态接口用）
    "said": 0.0,        # 上次写"回退/恢复"日志的时刻（节流）
}
_STATS = {"at": 0.0, "data": None}
_MERGING = [False]


# ------------------------------------------------------------------ 绑定

def bind(store_fn=None, log=None):
    """注入"当前存储路径"的提供者与日志函数。

    `cache` 不能 import `nvr_slim`（会成环），也不该自己读配置 —— 配置由
    `nvr_slim.record_dir()` 统一解释（含空串/null 的回退口径）。这里只持一个
    回调，单测直接 `bind(lambda: "/tmp/xx")` 就行。
    """
    if store_fn is not None:
        _BIND["store"] = store_fn
    if log is not None:
        _BIND["log"] = log


def _log(msg):
    fn = _BIND["log"]
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


def _store_root():
    fn = _BIND["store"]
    if not fn:
        return None
    try:
        return fn() or None
    except Exception:
        return None


# ------------------------------------------------------------------ 落点

def _probe(d):
    """真写一个字节再删掉。目录不存在/只读/配额满都会在这里失败。

    返回 (ok, 原因)。**原因要带出来**：0.9.142 在 152 上遇到过一次部署瞬间的
    回退，日志里只有「存储路径不可写」，没人知道是 EACCES 还是 ENOSPC ——
    前者是该查属主，后者是该清盘，处置完全相反。
    """
    p = os.path.join(d, PROBE_FILE)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("1")
        os.unlink(p)
        return True, ""
    except OSError as e:
        return False, "%s %s" % (type(e).__name__, getattr(e, "strerror", "") or "")


def _var_root(var_dir):
    return os.path.join(var_dir, ROOT_NAME)


def root(var_dir):
    """缓存的根目录（绝对路径）。解析失败也一定返回 var 下的路径。

    结果缓存 20 秒：`sub()` 在录制、抽帧、播放请求里都会被调到，每次都 statvfs
    或写探测文件，在机械盘上就是白送的 I/O。20 秒足够让"拔盘"在一个刷新周期内
    被感知到。
    """
    if not var_dir:
        return None
    now = time.time()
    key = os.path.abspath(var_dir)
    with _LOCK:
        r, at, v = _STATE["root"], _STATE["at"], _STATE["var"]
    # 缓存必须带上 var_dir：`STATE` 被改（测试、多实例、换数据目录）时同一个
    # 进程里会连续问不同的 var，只按时间判断就会把上一份根返回给下一份配置。
    if r and now - at < PROBE_TTL and v == key:
        return r

    cand, why = None, ""
    st = _store_root()
    if not st:
        why = "未设置存储路径"
    elif not os.path.isdir(st):
        # 关键：不创建 record_dir 本体。盘掉线时挂载点还在，makedirs 会把
        # "_cache" 建到系统盘上，问题就从"报错"变成"悄悄写错地方"。
        why = "存储路径不可用（%s）" % st
    else:
        d = os.path.join(st, ROOT_NAME)
        perr = ""
        try:
            os.makedirs(d, exist_ok=True)
            ok, perr = _probe(d)
        except OSError as e:
            ok = False
            perr = "%s %s" % (type(e).__name__, getattr(e, "strerror", "") or "")
        if ok:
            cand = d
        else:
            why = ("存储路径不可写（%s）：%s" % (d, perr.strip() or "未知原因"))

    fallback = _var_root(var_dir)
    if cand is None:
        cand = fallback
        try:
            os.makedirs(cand, exist_ok=True)
        except OSError:
            pass

    on_store = cand != fallback
    with _LOCK:
        was = _STATE["on_store"]
        prev = _STATE["root"]
        _STATE.update(root=cand, at=now, var=key,
                      on_store=on_store, why=why)
        if prev != cand:
            _STATS.update(at=0.0, data=None)

    # 只在状态**翻转**时吭声（外加 5 分钟一次的复述），不刷屏
    if prev is not None and was != on_store:
        if on_store:
            _log("缓存落点恢复：转回存储路径 %s（回退期的文件会陆续搬回去）" % cand)
            _merge_back_async(var_dir)
        else:
            _log("警告：缓存落点回退到配置目录 %s —— %s。"
                 "事件图/快照/播放缓存会暂时写在这里，盘恢复后自动搬回"
                 % (cand, why))
            _STATE["said"] = now
    return cand


def sub(var_dir, kind):
    """`<root>/<kind>`，保证目录存在（best-effort）。"""
    base = root(var_dir)
    if not base:
        return None
    d = os.path.join(base, kind)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def mode(var_dir=None):
    """给状态接口用：`{on_store, root, why, kinds}`。"""
    with _LOCK:
        on_store = _STATE["on_store"]
        r = _STATE["root"]
        why = _STATE["why"]
    return {"on_store": bool(on_store), "root": r, "why": why or ""}


def reset():
    """单测 / 切目录时清掉缓存（STATE 变了，落点必须重新解析）。"""
    with _LOCK:
        _STATE.update(root=None, at=0.0, var=None, on_store=True, why="",
                      said=0.0)
    _STATS.update(at=0.0, data=None)


# ------------------------------------------------------------------ 占用

def _tree_bytes(d):
    total = 0
    try:
        for dirpath, _dirs, files in os.walk(d):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def stats(var_dir, fresh=False):
    """各子目录占用（字节）。默认缓存 60 秒 —— 空间看板 30 秒轮询一次。"""
    now = time.time()
    if not fresh:
        with _LOCK:
            at, data = _STATS["at"], _STATS["data"]
        if data is not None and now - at < STATS_TTL:
            return data
    base = root(var_dir)
    out = {"root": base, "total": 0, "kinds": {}}
    if base:
        for k in KINDS:
            b = _tree_bytes(os.path.join(base, k))
            out["kinds"][k] = b
            out["total"] += b
    with _LOCK:
        _STATS.update(at=now, data=out)
    return out


def invalidate_stats():
    with _LOCK:
        _STATS.update(at=0.0, data=None)


# ------------------------------------------------------------------ remux 清理

def prune_remux(var_dir, log=None, max_bytes=REMUX_MAX_BYTES,
                max_age=REMUX_MAX_AGE, hold=REMUX_HOLD, now=None):
    """把 remux 缓存压回上限：先删超期的，再按最久未用删到总量达标。

    返回 `{"removed": n, "freed": bytes}`。

    为什么要"先时间后容量"：容量优先会让常用片源被反复删了重建（每次播放都
    重转一遍），而超期的是纯粹没人再看的。时间线先兜住"很久没碰过"的，再用
    容量兜住"最近看得太多"的。
    """
    d = os.path.join(root(var_dir) or "", "remux")
    if not var_dir or not os.path.isabs(d) or not os.path.isdir(d):
        return {"removed": 0, "freed": 0}
    now = float(now if now is not None else time.time())
    items = []
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        items.append((p, st.st_size, st.st_mtime))
    total = sum(i[1] for i in items)
    removed = freed = 0

    def _drop(p, size):
        try:
            os.unlink(p)
            return True, size
        except OSError:
            return False, 0

    # 1) 超期（且不在播放保护窗口里）
    keep = []
    for p, size, mt in items:
        if now - mt > max_age and now - mt > hold:
            ok, n = _drop(p, size)
            if ok:
                removed += 1
                freed += n
                total -= n
                continue
        keep.append((p, size, mt))

    # 2) 容量：最久没碰的先走
    if total > max_bytes:
        keep.sort(key=lambda x: x[2])
        for p, size, mt in keep:
            if total <= max_bytes:
                break
            if now - mt < hold:
                continue                # 可能正在播，跳过（后面的更旧，不受影响）
            ok, n = _drop(p, size)
            if ok:
                removed += 1
                freed += n
                total -= n

    if removed:
        _log("清理播放缓存 %d 份，释放 %.1f MB（上限 %.1f GB / %d 天）"
             % (removed, freed / 1048576.0,
                max_bytes / float(1024 ** 3), max_age // 86400))
        invalidate_stats()
        if log:
            try:
                log("remux 清理：%d 份 / %.1f MB" % (removed, freed / 1048576.0))
            except Exception:
                pass
    return {"removed": removed, "freed": freed}


# ------------------------------------------------------------------ 备份清理

BACKUP_KEEP = 2


def prune_backup(var_dir, keep=BACKUP_KEEP, log=None):
    """配置目录里的升级备份最多留 2 份（上一版 + 上上版就够回滚）。

    `var/backup` 是配置目录里最大的一块：每次升级/部署都把整份程序目录拷进去
    （`app-*`，一份几十上百 MB），152 上攒到 504MB、共十几份。配置目录的定位是
    "必需且极小"，备份不清理就违背了这个前提 —— 而真出问题时用得上的只有最近两份。

    只删**目录**，不碰 backup 下散落的文件（那些是配置迁移留下的单文件备份，
    体积极小，且名字带时间戳不好判断归属）。
    """
    d = os.path.join(var_dir, "backup")
    if not var_dir or not os.path.isdir(d):
        return {"removed": 0, "freed": 0}
    items = []
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        if not os.path.isdir(p):
            continue
        try:
            items.append((p, os.path.getmtime(p)))
        except OSError:
            continue
    items.sort(key=lambda x: x[1], reverse=True)
    removed = freed = failed = 0
    for p, _mt in items[max(0, int(keep)):]:
        size = _tree_bytes(p)
        shutil.rmtree(p, ignore_errors=True)
        if os.path.exists(p):
            failed += 1        # 属主不是本应用（root 做的升级备份）就删不掉
        else:
            removed += 1
            freed += size
    if removed:
        _log("清理旧升级备份 %d 份，释放 %.1f MB（保留最近 %d 份）"
             % (removed, freed / 1048576.0, keep))
        if log:
            try:
                log("备份清理：%d 份 / %.1f MB" % (removed, freed / 1048576.0))
            except Exception:
                pass
    if failed:
        # 诚实报告：`rmtree(ignore_errors=True)` 静默失败过一次 —— 152 上残留
        # 11 个 root 属主的 `cmd-*` 目录，日志里却像"清理完了"。删不掉就说删不掉，
        # 并给出需要 root 的处理方式，别让人以为空间已经回来了。
        _log("有 %d 份旧备份删不掉（属主不是本应用，需以 root 处理）："
             "%s 升级后由安装脚本清，或手工 rm -rf" % (failed, d))
    return {"removed": removed, "freed": freed, "failed": failed}


# ------------------------------------------------------------------ 迁移

def _move_file(src, dst):
    """跨卷也能用的单文件搬运；目标已存在且大小相同则只删源。"""
    try:
        if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
            os.unlink(src)
            return os.path.getsize(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return os.path.getsize(dst)
    except OSError:
        return 0


def _merge_tree(src, dst):
    """把 src 下的文件逐个并入 dst（目录结构保留）。返回 (文件数, 字节)。"""
    n = b = 0
    for dirpath, _dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        for fn in files:
            s = os.path.join(dirpath, fn)
            t = os.path.join(dst, rel, fn) if rel != "." else os.path.join(dst, fn)
            got = _move_file(s, t)
            if got:
                n += 1
                b += got
    # 源目录空了就收掉（没空说明还有搬失败的文件，留着下次再试）。
    # 用 `os.listdir` 判空而不是 walk 给的 dirs/files：那是遍历开始时的快照，
    # 子目录刚被删掉，父目录的 dirs 里仍然列着它 —— 结果就是"文件全搬走了，
    # 目录树留在原地"（152 升级后 event_shots/cam-test01 空壳残留，就是这么来的）。
    for dirpath, _dirs, _files in os.walk(src, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass
    return n, b


def _drop_tree(d):
    try:
        shutil.rmtree(d, ignore_errors=True)
        return True
    except OSError:
        return False


def _drop_empty_tree(d):
    """自底向上删掉空目录，留着还有文件的（迁移失败的残留要等下次再搬）。"""
    for dirpath, _dirs, _files in os.walk(d, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def migrate_once(var_dir, log=None):
    """把 0.9.140 及以前摊在 var 下的缓存搬进存储路径。

    - `shots` / `snapshots`：搬 —— 事件图是用户会回头看的，不该因为升级丢；
    - `remux` / `tmp`：直接删 —— 纯再生物，跨卷搬 14GB 换不来任何东西，
      下次播放自己会重新生成（生产机 remux 14GB，搬一次要几十分钟）。

    返回一份结果摘要（诊断页/日志用）。失败不写标记，下次启动重试。
    """
    base = root(var_dir)
    marker = os.path.join(var_dir, MIGRATE_MARK)
    if not var_dir or not base:
        return {"skipped": "no_root"}
    if os.path.abspath(base) == os.path.abspath(_var_root(var_dir)):
        return {"skipped": "fallback"}      # 盘还没好，等下次
    if os.path.exists(marker):
        # 标记在也顺手收一下空壳目录：0.9.141 首版迁移判空用的是 walk 给的
        # dirs 快照，会在 var 下留下 `event_shots/cam-*` 这种空树（152 真机实录）。
        # 升级到修好这版的机器不该再看见它们 —— 这里是幂等的，没有空目录就是空转。
        for legacy, _kind in LEGACY:
            d = os.path.join(var_dir, legacy)
            if os.path.isdir(d):
                _drop_empty_tree(d)
        return {"skipped": "done"}

    out = {}
    for legacy, kind in LEGACY:
        src = os.path.join(var_dir, legacy)
        if not os.path.isdir(src):
            continue
        if kind in ("remux", "tmp"):
            _drop_tree(src)
            out[kind] = {"action": "dropped"}
            continue
        n, b = _merge_tree(src, os.path.join(base, kind))
        out[kind] = {"action": "moved", "files": n, "bytes": b}
    if out:
        _log("缓存迁移到存储路径：%s" % ", ".join(
            "%s=%s" % (k, v.get("action") if v.get("action") == "dropped"
                       else "%d 个/%.1f MB" % (v.get("files", 0),
                                               (v.get("bytes") or 0) / 1048576.0))
            for k, v in sorted(out.items())))
        invalidate_stats()
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write("%d\n" % int(time.time()))
    except OSError:
        pass
    if log:
        for k, v in sorted(out.items()):
            try:
                log("缓存迁移 %s：%s" % (k, v))
            except Exception:
                pass
    return out


def _merge_back(var_dir):
    """盘恢复后，把回退期写在 var/_cache 的东西搬回存储路径。

    只搬 shots / snapshots（有保留价值的），remux / tmp 直接删。
    """
    base = root(var_dir)
    src = _var_root(var_dir)
    if not base or os.path.abspath(base) == os.path.abspath(src):
        return
    try:
        if not os.path.isdir(src):
            return
        for kind in ("shots", "snapshots"):
            d = os.path.join(src, kind)
            if os.path.isdir(d) and os.listdir(d):
                _merge_tree(d, os.path.join(base, kind))
        for kind in ("remux", "tmp"):
            d = os.path.join(src, kind)
            if os.path.isdir(d):
                _drop_tree(d)
        # 搬完把空的 shots/ snapshots/ 一并收掉：只 rmdir 顶层是不够的，
        # 子目录还在，`os.listdir(src)` 就非空 —— var 下会永久留着一个空壳。
        _drop_empty_tree(src)
        try:
            if not os.listdir(src):
                os.rmdir(src)
        except OSError:
            pass
        invalidate_stats()
    except OSError:
        pass


def _merge_back_async(var_dir):
    """后台搬，别让一个 HTTP 请求（或录制线程）等跨卷拷贝。"""
    if _MERGING[0]:
        return
    _MERGING[0] = True

    def _run():
        try:
            _merge_back(var_dir)
        finally:
            _MERGING[0] = False

    try:
        t = threading.Thread(target=_run, daemon=True, name="cache-mergeback")
        t.start()
    except Exception:
        _MERGING[0] = False


def start_maintenance(var_dir, log=None):
    """后台维护：启动时做一次迁移，之后每小时压一次 remux 上限。

    不用定时器线程做"整点任务"那套：这里的清理是幂等的、随时可跑的，
    每小时看一眼就够，漏一次也不影响正确性。
    """
    def _run():
        try:
            migrate_once(var_dir, log=log)
        except Exception:
            pass
        try:
            prune_backup(var_dir, log=log)
        except Exception:
            pass
        prune_remux(var_dir, log=log)
        while True:
            time.sleep(3600)
            try:
                prune_remux(var_dir, log=log)
            except Exception:
                pass

    try:
        t = threading.Thread(target=_run, daemon=True, name="cache-maint")
        t.start()
        return t
    except Exception:
        return None
