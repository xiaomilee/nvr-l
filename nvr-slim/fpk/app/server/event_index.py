# -*- coding: utf-8 -*-
"""事件索引：一表 SQLite，按「时间 × 摄像头 × 类别」检索。

为什么要有它：台账是「路 × 天」各一个 JSON，回答"昨天下午院子和车库有哪些人
经过"要遍历 N 路 × D 天再逐条合并 —— 跨路检索在页面里根本没有入口，只能一路
一路翻。索引把这件事变成一条 SQL。

**索引是台账的投影，不是第二份真相。** 它可以从台账整份重建
（`rebuild_index`），所以写入失败、库文件损坏、升级换了格式，都不算丢数据：
删掉重来即可。这条界线决定了下面所有的设计取舍 —— 宁可少存、宁可重算，
也不允许索引里出现台账里没有的事件。

时间列存 'YYYY-MM-DD HH:MM:SS' 本地时间字符串而不是 epoch：
  字符串比较就是时间序，能直接 range 查询；不用把时区/夏令时算进来
  （中国无夏令时，本地时间单调）；索引文件用 sqlite3 命令打开就能看。
  回放定位要的是「当天钟点秒」，另存 start_sec/end_sec 两个浮点列。
"""
import json
import os
import sqlite3
import threading
import time

import event

SCHEMA_VERSION = "1"

# 同步与查询会同时发生（页面在查、另一个请求在同步那一天）。用 RLock 而不是
# Lock：sync_day 内部要调 upsert，普通 Lock 会把自己锁死。
_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    camera_id        TEXT NOT NULL,
    start_at         TEXT NOT NULL,
    end_at           TEXT NOT NULL,
    start_sec        REAL NOT NULL,
    end_sec          REAL NOT NULL,
    source_date      TEXT NOT NULL,
    source_segment   TEXT,
    categories       TEXT NOT NULL DEFAULT '',
    cls              TEXT NOT NULL DEFAULT '{}',
    density          REAL,
    confidence_max   REAL,
    motion_confirmed INTEGER,
    state            TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
CREATE INDEX IF NOT EXISTS idx_events_cam ON events(camera_id, start_at);
CREATE TABLE IF NOT EXISTS day_sync (
    date       TEXT PRIMARY KEY,
    sig        TEXT,
    synced_at  TEXT,
    events     INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

INSERT_SQL = """
INSERT INTO events (event_id, camera_id, start_at, end_at, start_sec, end_sec,
                    source_date, source_segment, categories, cls, density,
                    confidence_max, motion_confirmed, state, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(event_id) DO UPDATE SET
    camera_id=excluded.camera_id,
    start_at=excluded.start_at,
    end_at=excluded.end_at,
    start_sec=excluded.start_sec,
    end_sec=excluded.end_sec,
    source_date=excluded.source_date,
    source_segment=excluded.source_segment,
    categories=excluded.categories,
    cls=excluded.cls,
    density=excluded.density,
    confidence_max=excluded.confidence_max,
    motion_confirmed=excluded.motion_confirmed,
    state=excluded.state,
    updated_at=excluded.updated_at
"""


def db_path(var_dir):
    """索引位置：跟配置/台账/日志放一起（var_dir），随应用数据一起备份。"""
    return os.path.join(str(var_dir), "event_index.db")


def connect(path):
    """打开索引。每次调用新建一条连接 —— HTTP 是每请求一个线程，共享连接要
    处理 check_same_thread 与串行化，代价比重新 connect（几十微秒）大得多。"""
    p = str(path)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        # WAL：页面在查、同步在写，两边不互相等锁。默认的 rollback journal
        # 会让一次整天同步把列表请求卡住，用户看到的是"事件页转圈"。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def ensure(conn):
    """建表（幂等）。老的库只补缺的表/索引，不动已有数据 —— 索引随时可重建，
    没必要为它写迁移。"""
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO meta(k, v) VALUES('schema', ?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (SCHEMA_VERSION,))
    conn.commit()


# ---------------------------------------------------------------- 行 ↔ 事件

def cat_field(cats):
    """类别列表 → 可 LIKE 的字段：',person,vehicle,'（英文逗号）。

    两端也带分隔符，是为了让 `LIKE '%,person,%'` 精确匹配整词。只写
    'person,vehicle' 时 'person' 会命中任何同前缀类别 —— 现在没有这种类别，
    但过滤器不能靠"现在没有"成立。
    """
    items = [str(c).strip() for c in (cats or ()) if str(c).strip()]
    return "," + ",".join(items) + "," if items else ""


def cat_list(field):
    return [x for x in str(field or "").split(",") if x]


def to_row(ev):
    """事件 dict → 一行元组（列顺序与 INSERT_SQL 一致）。"""
    mc = ev.get("motion_confirmed")
    return (
        str(ev.get("id")),
        str(ev.get("cam") or ""),
        str(ev.get("start_at") or ""),
        str(ev.get("end_at") or ""),
        float(ev.get("start") or 0.0),
        float(ev.get("end") or 0.0),
        str(ev.get("date") or ""),
        ev.get("source_segment"),
        cat_field(ev.get("categories")),
        json.dumps(ev.get("cls") or {}, ensure_ascii=False, sort_keys=True),
        float(ev.get("density") or 0.0),
        ev.get("confidence_max"),
        None if mc is None else (1 if mc else 0),
        str(ev.get("state") or ""),
        time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def from_row(row):
    """一行 → 事件 dict，键名与 event.from_ledger 完全一致：
    前端不该为了"列表"和"详情"认两套结构。"""
    try:
        cls = json.loads(row["cls"] or "{}")
    except ValueError:
        cls = {}
    mc = row["motion_confirmed"]
    start = float(row["start_sec"] or 0.0)
    end = float(row["end_sec"] or 0.0)
    return {
        "id": row["event_id"],
        "cam": row["camera_id"],
        "date": row["source_date"],
        "start": round(start, 2),
        "end": round(end, 2),
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "duration": round(end - start, 2),
        "categories": cat_list(row["categories"]),
        "cls": cls if isinstance(cls, dict) else {},
        "density": row["density"],
        "confidence_max": row["confidence_max"],
        "motion_confirmed": None if mc is None else bool(mc),
        "state": row["state"],
        "source_segment": row["source_segment"],
    }


# ---------------------------------------------------------------- 写入

def upsert(conn, events):
    """写入一批事件 → {"new", "updated", "same"}。

    先读旧行再合并，而不是让 SQL 做 max/min：合并语义只有 `event.merge_one`
    一处实现（类别取 max、状态只前进、mv=-1 保持 None）。SQL 里再写一遍就是
    两份真相，早晚不一致 —— 而这类不一致只会表现为"列表里的事件跳来跳去"
    这种极难复现的 bug。一天的窗口量级是几百条，多几百次 SELECT 不值一提。
    """
    new = upd = same = 0
    with _LOCK:
        cur = conn.cursor()
        try:
            for ev in events or ():
                if not isinstance(ev, dict) or not ev.get("id"):
                    continue
                row = cur.execute("SELECT * FROM events WHERE event_id=?",
                                  (str(ev["id"]),)).fetchone()
                if row is None:
                    out, new = dict(ev), new + 1
                else:
                    out, changed = event.merge_one(from_row(row), ev)
                    if not changed:
                        same += 1
                        continue
                    upd += 1
                # 落库即"已入库"；但正在进行的窗口不标（规矩定在 event.next_state：
                # 它的结束时间随时会变，标成"已定型"会骗人）。
                out["state"] = event.next_state(out.get("state"), "indexed")
                cur.execute(INSERT_SQL, to_row(out))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
    return {"new": new, "updated": upd, "same": same}


def sync_day(conn, date, loader, sig=None):
    """把某一天的台账重算一遍灌进索引 → {"date","events","skipped","sig"}。

    sig 是调用方算的「这一天台账的指纹」（路径/大小/mtime 的摘要），一样就跳过：
    台账一个分片（默认 300 秒）才动一次，页面每次刷新都全量重算纯属白烧 CPU。

    整天先删后插（而不是只增不删）：台账重判后事件可能变少（合并口径改了、
    忽略区改了），只增的索引会永远留着已经不存在的事件，用户点进去只能得到
    "没有这条事件"。
    """
    d = str(date or "")
    if not event.valid_date(d):
        return {"date": d, "events": 0, "skipped": True, "error": "bad date"}
    with _LOCK:
        if sig is not None:
            row = conn.execute("SELECT sig FROM day_sync WHERE date=?",
                               (d,)).fetchone()
            if row is not None and row["sig"] == str(sig):
                return {"date": d, "events": 0, "skipped": True, "sig": str(sig)}
    # 先算再删：loader 抛错时索引必须保持原样，不能留下"删了一半"的空窗。
    evs = [e for e in (loader(d) or ())
           if isinstance(e, dict) and e.get("id")]
    with _LOCK:
        conn.execute("DELETE FROM events WHERE source_date=?", (d,))
        conn.commit()
        upsert(conn, evs)
        conn.execute(
            "INSERT INTO day_sync(date, sig, synced_at, events) VALUES(?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET sig=excluded.sig, "
            "synced_at=excluded.synced_at, events=excluded.events",
            (d, str(sig) if sig is not None else "",
             time.strftime("%Y-%m-%d %H:%M:%S"), len(evs)))
        conn.commit()
    return {"date": d, "events": len(evs), "skipped": False,
            "sig": str(sig) if sig is not None else ""}


def rebuild_index(conn, dates, loader, progress=None):
    """从台账整份重建（清空索引后逐日重算）→ {"days","events"}。

    这是"索引只是投影"的证据：任何时候把 event_index.db 删掉重来，结果必须
    一致。所以它不做增量优化、也不看指纹 —— 它的用途就是"我怀疑索引不对"。
    """
    days = n = 0
    with _LOCK:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM day_sync")
        conn.commit()
    for d in dates or ():
        r = sync_day(conn, d, loader, sig=None)
        days += 1
        n += int(r.get("events") or 0)
        if progress:
            progress(r)
    return {"days": days, "events": n}


# ---------------------------------------------------------------- 读取

def query(conn, start_at=None, end_at=None, cam=None, cat=None, limit=200):
    """按「时间窗 × 摄像头 × 类别」检索 → 事件列表（时间正序）。

    时间窗是**交集**语义（`end_at >= 起点 且 start_at <= 终点`）：跨窗口边界的
    那条事件照样出现在两边，否则用户会以为"整点那一刻没有事件"。

    先按时间倒序取最近 limit 条再翻回正序：用户要的是"最近的事"，而列表本身
    按时间轴读（跟回放页一致）。limit 必须在 SQL 里生效，不能先取全量再截。
    """
    where, args = [], []
    if start_at:
        where.append("end_at >= ?")
        args.append(str(start_at))
    if end_at:
        where.append("start_at <= ?")
        args.append(str(end_at))
    if cam:
        where.append("camera_id = ?")
        args.append(str(cam))
    if cat:
        where.append("categories LIKE ?")
        args.append("%," + str(cat) + ",%")
    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_at DESC, event_id DESC LIMIT ?"
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 200
    args.append(max(1, min(lim, 5000)))
    rows = conn.execute(sql, args).fetchall()
    return [from_row(r) for r in reversed(rows)]


def get(conn, eid):
    """单条事件；没有返回 None（调用方决定回 404 还是先同步再查）。"""
    row = conn.execute("SELECT * FROM events WHERE event_id=?",
                       (str(eid),)).fetchone()
    return from_row(row) if row is not None else None


def days_synced(conn):
    """已经同步过的日期清单（重建索引用：知道台账覆盖了哪些天）。"""
    return [r["date"] for r in conn.execute(
        "SELECT date FROM day_sync ORDER BY date").fetchall()]


def stats(conn):
    """诊断用：索引里到底有多少条、覆盖几天几路。空索引一眼看出没同步过 ——
    这比"事件页是空的"有用得多，后者分不清是没事件还是索引坏了。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT source_date) d, "
            "COUNT(DISTINCT camera_id) c, MIN(start_at) a, MAX(end_at) b "
            "FROM events").fetchone()
        day_row = conn.execute(
            "SELECT COUNT(*) n, MAX(synced_at) t FROM day_sync").fetchone()
    except sqlite3.DatabaseError:
        return {"events": 0, "days": 0, "cams": 0, "from": None, "to": None,
                "synced_days": 0, "synced_at": None}
    return {"events": row["n"], "days": row["d"], "cams": row["c"],
            "from": row["a"], "to": row["b"],
            "synced_days": day_row["n"], "synced_at": day_row["t"]}
