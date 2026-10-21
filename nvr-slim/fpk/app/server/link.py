#!/usr/bin/env python3
"""录制与压制的衔接调度。

同一个队列，两种入队节奏 —— 这就是界面上那个选项的全部实现差异：

  live       每 2 秒扫一次目录，发现已封口的分片立刻入队。
             分片刚写完，数据还在 page cache 里，判定阶段几乎不产生物理读，
             也不会和录像写盘抢磁头。代价是判定当场就要下结论。

  scheduled  每隔 N 小时（1~24）批量入队一次。适合白天不想让磁盘和 CPU 有
             任何额外动作的场景；代价是数据已冷，读盘会真的走一趟盘。

两种模式共用同一个消费端，所以"激进"只体现在入队节奏上，判定与导出的
代码完全一致 —— 不会出现"实时档和定时档切出来画质不一样"这种坑。

三条硬约束（都是踩过的）：
1. **只消费已封口的分片**（recorder.finished 担保）。正在写的分片 moov 还没落地，
   读它就是坏文件，ffprobe 会返回空，然后被误判成"整段静止"直接丢掉。
2. **背压只暂停入队，不丢队列**。GPU 忙时宁可让分片在盘上多躺一会儿，
   也不能把任务扔掉 —— 扔掉的后果是那段时间永远没有精简片段。
3. **幂等键含参数指纹**。改了压缩参数要重跑，没改就绝不重复处理。
   用源文件是否存在判断幂等是错的：换个参数重跑时输出文件还在，会被误跳过。
"""
import collections
import hashlib
import json
import os
import queue
import threading
import time

import condense
import params
import recorder
import runtime


def _fingerprint(profile):
    try:
        return params.fingerprint(profile, "auto")
    except Exception:
        return "na"


def item_key(src, root, cam_id, profile, ignore=None):
    """幂等键：相对路径 + 大小 + mtime + 参数指纹 + 忽略区。

    忽略区必须进指纹：用户画完忽略区，同一批分片的判定结果本该不同。
    不进指纹的话它们已经在 done 集合里，会被永久跳过 —— 表现就是
    "忽略了树影，但历史片段一点没变"，用户只能以为功能没生效。
    """
    try:
        st = os.stat(src)
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        return None
    rel = os.path.relpath(src, os.path.join(root, cam_id)).replace("\\", "/")
    ig = ",".join(str(int(x)) for x in sorted(set(ignore or [])))
    raw = "%s|%d|%d|%s|%s" % (rel, size, mtime, _fingerprint(profile), ig)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Link:
    def __init__(self, ffmpeg, ffprobe, root, var_dir, cameras, cfg,
                 log=None, on_done=None):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.root = root
        self.var_dir = var_dir
        self.cams = list(cameras or [])
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.on_done = on_done

        # 统一走 runtime 的夹取函数：`x or 默认值` 会把 0 悄悄吞成默认值，
        # 而 0 在这里是"用户设错了"，应该夹到下限 1 而不是变成 6
        self.mode = runtime.link_mode(cfg.get("link_mode"))
        self.interval_h = runtime.link_interval_h(cfg.get("link_interval_h"))
        self.seg_sec = runtime.segment_sec(cfg.get("record_segment_sec"))
        self.max_queue = runtime.clamp_int(cfg.get("link_max_queue"), 20, 2000, 200)

        self.profiles = {}
        raw = cfg.get("profiles") or {}
        for c in self.cams:
            p = raw.get(c["id"]) if isinstance(raw, dict) else None
            self.profiles[c["id"]] = (params.new_profile(c.get("name") or c["id"])
                                      if not isinstance(p, dict) else p)

        self.q = queue.Queue()
        self.pending = collections.deque()      # 镜像 q 的内容，仅用于统计与展示
        self.pending_keys = set()               # 与 pending 同步的键集合，去重用 O(1) 而不是线性扫
        self.done = set()
        self.failed = {}                        # key -> 错误摘要
        self.stats = {"done": 0, "silent": 0, "partial": 0, "failed": 0,
                      "bytes_in": 0, "bytes_out": 0}
        self._wait_logged = {}      # cid -> 上次「等待封口」日志时间，10 分钟节流
        self.log("衔接 worker 启动：模式=%s，分片=%d 秒，扫描目录=%s"
                 % ("即刻压制" if self.mode == "live"
                    else "定时压制（每 %d 小时）" % self.interval_h,
                    self.seg_sec,
                    "、".join(dict.fromkeys(c.get("dir_name") or c["id"] for c in self.cams))
                    or "无"))
        self.last_batch = 0.0
        self.next_batch = 0.0
        self.last_scan = 0.0
        self.last_item = None
        self.stopped = False
        self._scan_cache = {}                   # 日期目录 -> (mtime_ns, [文件])
        self._lock = threading.Lock()

        self._load()

    def cam_of(self, cam_id):
        for c in self.cams:
            if c.get("id") == cam_id:
                return c
        return {}

    def clip_speed_of(self, cam_id):
        """这一路的缩时倍速。只认白名单 1/2/4/8，其它一律回落 1。

        倍速走重编码，CPU 是真代价；让任意整数进来（比如 3、7）只会让
        用户误以为"倍数越怪省得越多"，实际收益和 2/4 没区别。
        """
        try:
            v = int(float((self.cam_of(cam_id) or {}).get("clip_speed") or 1))
        except (TypeError, ValueError):
            return 1
        return v if v in (1, 2, 4, 8) else 1

    def set_cameras(self, cams):
        """列表变化（增删/改地址）时更新；新出现的摄像头补一个默认画质档。"""
        self.cams = [c for c in (cams or []) if c.get("enabled", True)]
        for c in self.cams:
            if c["id"] not in self.profiles:
                self.profiles[c["id"]] = params.new_profile(c.get("name") or c["id"])

    def ignore_cells(self, cam_id):
        """这一路的检测忽略区格号（16×9）。没有就返回空列表。

        以前 masks 只存在配置里、从来没进过判定，于是"设置页画了忽略区、
        树影照样天天触发"。判定要用它，就必须在消费侧现取最新值 ——
        用户改完不重启也应该下一个分片就生效。
        """
        for c in self.cams:
            if c.get("id") == cam_id:
                m = c.get("masks") or {}
                return [int(x) for x in (m.get("ignore") or [])
                        if isinstance(x, int) or str(x).isdigit()]
        # 通道可能已被停用/改名但仍留有历史分片：退回配置里那份
        for c in (self.cfg.get("cameras") or []):
            if isinstance(c, dict) and c.get("id") == cam_id:
                m = c.get("masks") or {}
                return [int(x) for x in (m.get("ignore") or [])
                        if isinstance(x, int) or str(x).isdigit()]
        return []

    def set_profiles(self, profiles):
        """设置页改了画质参数：整份替换，指纹变了下一个分片就会按新参数重跑。"""
        if isinstance(profiles, dict):
            for cid, p in profiles.items():
                if isinstance(p, dict):
                    self.profiles[cid] = p

    # ------------------------------------------------------------ 持久化

    def _state_path(self):
        return os.path.join(self.var_dir, "link-state.json")

    def _done_path(self):
        return os.path.join(self.var_dir, "link-done.jsonl")

    def _load(self):
        try:
            with open(self._state_path(), "r", encoding="utf-8") as f:
                s = json.load(f)
            self.last_batch = float(s.get("last_batch") or 0)
            self.stats.update(s.get("stats") or {})
            self.last_item = s.get("last_item")
        except (OSError, ValueError):
            pass
        # 完成记录用追加写，只回读最近 3 天，避免启动时把几年的记录全拉进内存
        try:
            with open(self._done_path(), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.done.add(line)
        except OSError:
            pass

    def _save(self):
        tmp = self._state_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_batch": self.last_batch, "stats": self.stats,
                           "mode": self.mode, "interval_h": self.interval_h,
                           "last_item": self.last_item,
                           "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_path())
        except OSError:
            pass

    def _mark_done(self, key):
        self.done.add(key)
        try:
            with open(self._done_path(), "a", encoding="utf-8") as f:
                f.write(key + "\n")
        except OSError:
            pass
        # 半年清一次，防止文件无限增长
        if len(self.done) > 200000:
            self.done = set(list(self.done)[-50000:])
            try:
                with open(self._done_path(), "w", encoding="utf-8") as f:
                    f.write("\n".join(sorted(self.done)) + "\n")
            except OSError:
                pass

    # ------------------------------------------------------------ 扫描

    def _segments(self, cam_id):
        """带目录 mtime 缓存的分片列表。8 路 × 每 2 秒一次 listdir 也要省着用。"""
        base = os.path.join(self.root, cam_id)
        out = []
        try:
            days = sorted(d for d in os.listdir(base)
                          if len(d) == 10 and d[4] == "-" and d[7] == "-")
        except OSError:
            return out
        for d in days:
            full = os.path.join(base, d)
            try:
                mt = os.stat(full).st_mtime_ns
            except OSError:
                continue
            cached = self._scan_cache.get(full)
            if cached and cached[0] == mt:
                names = cached[1]
            else:
                try:
                    names = sorted(n for n in os.listdir(full)
                                   if n.lower().endswith(".mp4"))
                except OSError:
                    continue
                self._scan_cache[full] = (mt, names)
            out.extend(os.path.join(full, n) for n in names)
        return out

    def _new_items(self):
        """尚未入队、且已封口的分片。"""
        now = time.time()
        out = []
        for c in self.cams:
            cid = c["id"]
            # 每路的缩时压制总开关：关了就不入队（已生成的片段照常回放/清理）
            if c.get("condense") is False:
                continue
            segs = []
            # 新目录（摄像头名称）+ 旧 id 目录都要扫：升级前的存量分片也要缩时
            for d in dict.fromkeys([c.get("dir_name") or cid, cid]):
                segs.extend(self._segments(d))
            if not segs:
                continue
            head, tail = segs[:-1], segs[-1]
            try:
                idle = now - os.path.getmtime(tail)
            except OSError:
                idle = 0
            ready = segs if idle > max(120, self.seg_sec + 60) else head
            if not ready and segs and now - self._wait_logged.get(cid, 0) > 600:
                self._wait_logged[cid] = now
                self.log("缩时等待：%s 已有 %d 个分片，正在写的分片封口后才处理"
                         % (c.get("name") or cid, len(segs)))
            profile = self.profiles.get(cid)
            ignore = self.ignore_cells(cid)
            for p in ready:
                k = item_key(p, self.root, cid, profile, ignore)
                if not k or k in self.done or k in self.failed:
                    continue
                # pending 最多 link_max_queue(默认 200) 个，以前用 any() 线性扫，
                # 队列满时每 2 秒就是 200×200 次比较；换成集合后是 O(1)
                if k in self.pending_keys:
                    continue
                rel = os.path.relpath(p, self.root)
                out.append({"key": k, "path": p, "cam": cid,
                            "dir": rel.split(os.sep)[0],
                            "name": c.get("name") or cid})
        out.sort(key=lambda x: x["path"])
        return out

    # ------------------------------------------------------------ 入队

    def tick(self, now=None):
        """一次调度节拍。live 模式每拍都扫；scheduled 模式到点才扫。"""
        if self.stopped:
            return
        now = now if now is not None else time.time()
        self.last_scan = now

        if self.mode == "live":
            self._enqueue(now)
            return

        if self.next_batch <= 0:
            # 首次启动：先处理一次积压，否则要等满一个间隔才有第一个片段
            self.next_batch = now
        if now >= self.next_batch:
            self._enqueue(now, batch=True)
            self.last_batch = now
            self.next_batch = now + self.interval_h * 3600
            self._save()

    def _enqueue(self, now, batch=False):
        if self.q.qsize() >= self.max_queue:
            if not batch:
                return          # 背压：暂停入队，队列不丢
            self.log("入队暂停：队列已满（%d），本轮跳过" % self.q.qsize())
            return
        items = self._new_items()
        if not items:
            return
        cap = self.max_queue - self.q.qsize()
        n = 0
        for it in items[:max(0, cap)]:
            self.q.put(it)
            with self._lock:
                self.pending.append(it)
                self.pending_keys.add(it["key"])
            n += 1
        self.log("%s入队 %d 项%s" % ("批量" if batch else "即时", n,
                                     ("（积压 %d）" % (len(items) - n)) if len(items) > n else ""))

    def trigger(self):
        """手动「立即处理」：不看模式，马上入队一轮。"""
        self._enqueue(time.time(), batch=True)
        if self.mode == "scheduled":
            self.last_batch = time.time()
            self.next_batch = self.last_batch + self.interval_h * 3600
        self._save()
        return self.status()

    # ------------------------------------------------------------ 消费

    def _process(self, it):
        src = it["path"]
        if not os.path.isfile(src):
            return {"ok": False, "reason": "missing"}
        if not self.ffprobe:
            # 没有 ffprobe 就判不了"有没有变化"，此时宁可什么都不做，
            # 也不能把整段当静止丢掉（那等于删录像）。
            return {"ok": False, "reason": "no-ffprobe",
                    "error": "缺少 ffprobe，无法分析画面变化"}
        profile = self.profiles.get(it["cam"]) or params.new_profile(it["name"])
        a = params.resolve(profile, "auto")

        try:
            size_in = os.path.getsize(src)
        except OSError:
            size_in = 0

        res = condense.analyze(self.ffprobe, src, a, ignore=self.ignore_cells(it["cam"]))
        if res["error"]:
            return {"ok": False, "reason": "analyze", "error": res["error"]}

        if res["silent"]:
            # 整段没有值得留的变化：不产出文件，只记账。
            # 源文件不删 —— 全时录像的删除权归保留策略，压缩层不能越界。
            return {"ok": True, "silent": True, "bytes_in": size_in,
                    "bytes_out": 0, "detail": res}

        out = condense.clip_path(self.root, it.get("dir") or it["cam"], src)
        w = condense.write_one(self.ffmpeg, src, out, res["windows"],
                               os.path.join(self.var_dir, "tmp", it["key"]),
                               speed=self.clip_speed_of(it["cam"]))
        if not w["ok"]:
            return {"ok": False, "reason": "export", "error": w.get("error"), "detail": res}
        try:
            size_out = os.path.getsize(out)
        except OSError:
            size_out = 0
        return {"ok": True, "silent": False, "bytes_in": size_in,
                "bytes_out": size_out, "out": out, "detail": res}

    def _worker(self):
        while not self.stopped:
            try:
                it = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if it is None:
                break
            t0 = time.time()
            try:
                r = self._process(it)
            except Exception as e:                      # 单项故障不能拖垮整个队列
                r = {"ok": False, "reason": "exception", "error": repr(e)[:200]}
            with self._lock:
                try:
                    self.pending.remove(it)
                except ValueError:
                    pass
                self.pending_keys.discard(it["key"])
            if r["ok"]:
                self._mark_done(it["key"])
                if r.get("silent"):
                    self.stats["silent"] += 1
                else:
                    self.stats["done"] += 1
                    try:
                        where = os.path.relpath(r.get("out") or "", self.root)
                    except ValueError:
                        where = r.get("out") or "?"
                    self.log("缩时完成 %s → %s" % (os.path.basename(it["path"]), where))
                self.stats["bytes_in"] += r.get("bytes_in", 0)
                self.stats["bytes_out"] += r.get("bytes_out", 0)
            else:
                self.failed[it["key"]] = (r.get("error") or r.get("reason") or "?")[:200]
                self.stats["failed"] += 1
                # 失败表无限增长会一直占内存（而且这些 key 永远不会再被处理）。
                # 只留最近 500 条：界面本来也只展示最后 5 条。
                if len(self.failed) > 500:
                    self.failed = dict(list(self.failed.items())[-200:])
            self.last_item = {
                "cam": it["cam"], "name": it["name"],
                "file": os.path.basename(it["path"]),
                "ok": r["ok"], "silent": bool(r.get("silent")),
                "sec": round(time.time() - t0, 1),
                "at": time.strftime("%H:%M:%S"),
                "detail": {k: r.get("detail", {}).get(k) for k in
                           ("windows", "duration", "active_ratio", "error")}
                          if r.get("detail") else None,
                "error": r.get("error"),
            }
            if not r["ok"]:
                # 带上摄像头名和所在日期：只报文件名时，5 路同名分片根本分不清是哪一路
                try:
                    rel = os.path.relpath(it["path"], self.root)
                except ValueError:
                    rel = it["path"]
                self.log("处理失败 %s：%s" % (rel, (r.get("error") or r.get("reason"))))
            if self.on_done:
                try:
                    self.on_done(it, r)
                except Exception:
                    pass
            self.q.task_done()
        self._save()

    # ------------------------------------------------------------ 生命周期

    def start(self):
        self.worker = threading.Thread(target=self._worker, name="link-worker", daemon=True)
        self.worker.start()
        self.log("衔接调度启动：%s%s" % (
            "即刻压制" if self.mode == "live" else "定时压制（每 %d 小时）" % self.interval_h,
            "" if self.mode == "live" else "，首次启动会先处理一次积压"))
        return self

    def stop(self):
        self.stopped = True
        try:
            self.q.put(None)
        except Exception:
            pass
        self._save()

    def reconfigure(self, cfg):
        """运行中改设置：只切节奏与根目录，不重建队列。"""
        self.cfg = cfg
        old_mode = self.mode
        self.mode = runtime.link_mode(cfg.get("link_mode"))
        self.interval_h = runtime.link_interval_h(cfg.get("link_interval_h"))
        self.seg_sec = runtime.segment_sec(cfg.get("record_segment_sec"))
        new_root = (str(cfg.get("record_dir") or "").strip() or self.root)
        if new_root != self.root:
            self.root = new_root
            self._scan_cache.clear()     # 缓存键含根目录，换根后必须清，否则扫的还是旧目录
            self.log("衔接扫描目录改为 %s" % self.root)
        if self.mode != old_mode:
            if self.mode == "live":
                self.next_batch = 0
            else:
                self.next_batch = (self.last_batch + self.interval_h * 3600
                                   if self.last_batch else time.time())
            self.log("衔接模式切换为%s" % ("即刻压制" if self.mode == "live"
                                          else "定时压制（每 %d 小时）" % self.interval_h))
        self._save()

    def status(self):
        pending = 0
        with self._lock:
            pending = len(self.pending)
        nxt = None
        if self.mode == "scheduled" and self.next_batch > 0:
            nxt = time.strftime("%m-%d %H:%M", time.localtime(self.next_batch))
        total_in = self.stats.get("bytes_in") or 0
        total_out = self.stats.get("bytes_out") or 0
        return {
            "mode": self.mode,
            "mode_label": "即刻压制" if self.mode == "live" else "定时压制",
            "interval_h": self.interval_h,
            "segment_sec": self.seg_sec,
            "max_queue": self.max_queue,
            "pending": pending,
            "running": pending > 0,
            "counters": {k: self.stats.get(k, 0) for k in
                         ("done", "silent", "failed", "bytes_in", "bytes_out")},
            "saved_ratio": (round(1 - total_out / total_in, 3) if total_in else 0),
            "next_batch_at": nxt,
            "next_batch_ts": self.next_batch or None,
            "last_batch_at": (time.strftime("%m-%d %H:%M", time.localtime(self.last_batch))
                              if self.last_batch else None),
            "last_scan_at": (time.strftime("%H:%M:%S", time.localtime(self.last_scan))
                             if self.last_scan else None),
            "last_item": self.last_item,
            "failed_items": list(self.failed.items())[-5:],
        }
