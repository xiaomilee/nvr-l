#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一取流：每路摄像头只建 **一条** RTSP 连接，再把码流分发给本机消费者。

## 为什么要重做

0.9.77 及以前，一路摄像头被三个地方各自拉流：

    recorder   —— 常驻 ffmpeg，-c copy 分段落盘          1 条长连接
    snapshots  —— 每 N 秒新起一个 ffmpeg 抓 1 帧        N 秒一条新连接
    detect     —— 每 N 秒再新起一个 ffmpeg 抽 1 帧 PPM   N 秒一条新连接

默认 5 秒一轮、5 路，就是 **每分钟 120 次新建 RTSP 会话**。这不是理论问题：
Frigate 的排查文档把「同时拉流的路数」列在摄像头掉线的第一梯队原因里
（"Count everything pulling from the camera at once ... each use one"），
并明确建议「所有角色都走同一路 restream，摄像头只看到一条连接，往往自己就好了」。

## 现在的形状

    camera ──RTSP×1──▶ ingest ffmpeg ──MPEG-TS──▶ 127.0.0.1:port ─┬─▶ recorder ffmpeg   (-c copy → 分片)
                                                                  ├─▶ frames ffmpeg     (关键帧 → JPEG)
                                                                  └─▶ （将来任何新消费者）

三个消费者仍然是**独立进程**，这是刻意保留的隔离：Frigate 也是 detect / record
各一个 ffmpeg 进程，只共用 go2rtc 的 restream。检测那一路解码出错、被 OOM、
被我们自己的看门狗杀掉，都不该把正在落盘的录像带下水（0.9.65 的教训）。

## 关键取舍

- **用 MPEG-TS 而不是 RTSP 转发**：写一个 RTSP 服务端要处理 SETUP/PLAY 与
  interleaved 封装，几百行且极易出错；TS 是字节流，广播即可，ffmpeg 直接
  `-i tcp://...` 就能解。代价是每路多一次 TS 重封装（-c copy，不解码，可忽略）。
- **不做共享内存 / ring buffer**：消费者是 ffmpeg，它只认 URL。TS over TCP
  是能让「任意数量的本机 ffmpeg」零改造接入的最短路径。
- **慢消费者不许阻塞取流**：每个客户端一个 96×64KB 的有界队列，满了丢最旧的。
  取流进程的存活与任何消费者无关 —— 反过来，消费者挂掉也绝不影响录像。
- **失效即降级**：取流起不来找不到端口时，`url_for()` 返回 None，录制会退回
  直连摄像头。宁可多一条连接，也不能因为新模块出错导致「一条录像都没有」。
"""
import os
import socket
import subprocess
import threading
import time
from collections import deque

CHUNK = 65536              # 单次 read 的块大小：TS 的 PAT/PMT 约 100ms 一轮，64KB 够跟
BACKLOG = 96               # 每客户端最多积压 96×64KB ≈ 6MB；超了丢最旧，绝不阻塞广播
# 两种「没有输出」必须分开，真机 2026-10-26 的教训：
#
#   A. 从 spawn 到现在**一个字节都没吐过** —— 握手/探测死了，自己永远好不了，
#      必须尽快杀掉重来。这就是 HANDSHAKE_SEC。
#   B. **出过数据之后停了** —— 相机自己的抖动。0.9.80 的现场记录说这种停顿
#      20~40 秒能自己回来，所以 0.9.81 把耐心放到 120 秒（那一次也确实把
#      「20 秒就杀」的方向纠了回来）。
#
#   但 0.9.82 拿到的实测把「能自己回来」这件事推翻了。2026-10-26 09:33–09:44
#   这台机器上五个机位一共 6 次停顿，**没有一次**打出「恢复（相机自己回来的）」
#   那条日志 —— 全是停下去就再也不动，一直等到打穿耐心才重连；而每次重连之后
#   都能立刻恢复、正常工作 20~60 秒，然后又停。也就是说在这台机器上：
#
#       等待 = 纯损失，重连 = 有效手段。
#
#   120 秒的等待于是变成「每轮白丢 2 分钟录像」。0.9.82 先降到 45 秒，
#   同一天 10:03–12:19 又攒了 118 次停顿的账，把这个判断彻底坐实：
#
#       · 118 次停顿里只有 1 次「相机自己回来」，而且只停了 4 秒；
#       · 其余 117 次全部走「打穿耐心 → 重连」，静默时长中位数 121 秒；
#       · 每一轮的周期约 216 秒 = 静默 121 + 退避 30 + 探测 10 + 正常 55，
#         也就是**只有 25% 的时间在录**。
#
#   所以 45 秒仍然偏大，收到 25 秒。25 秒 = 正常码流 25 秒一个包都没有，
#   远超过任何"抖动"的量级；而 0.9.80 试过的 20 秒只差 5 秒，没本质区别。
#   误判的代价只是多发一次重连（连得上，见 B 分支的短退避），比白等划算。
#
#   0.9.84 再收到 15 秒：当天 12:39–12:59 的免凭据双轨实测（每 2 秒对每台相机
#   新建连接发 OPTIONS，同时轮询 ingest 的 idle_sec）给出了更硬的证据 ——
#   12 段静默全部持续 150~156 秒且无一自愈；静默期间 OPTIONS 成功 804/807，
#   静默前 30 秒的延迟中位数 16~31ms 与基线一致。相机活、链路通、只有媒体通道死。
#   既然它不存在"自己回来"的可能，15 秒就已经足够宽容（唯一一次自愈只停了 4 秒）。
#
#   更重要的是：0.9.82 起录制层不再抢着判死（starved_of 会让路），所以缩短
#   这个阈值**不会**再放大成两轮重启 —— 重连期间消费者 socket 保持不断，
#   端口也不重建，录制进程只要等数据续上就行。
HANDSHAKE_SEC = 25
PATIENCE_SEC = 15
# 静默多久才在日志里说一句。低于这个数的抖动不值一提，说了只是刷屏。
PAUSE_LOG_SEC = 3.0
STALL_SEC = HANDSHAKE_SEC          # 兼容旧名字（构造参数仍叫 stall_sec）
MAX_BACKOFF = 300
SOFT_KILL_SEC = 5
KEEP_ERR_BYTES = 8192      # stderr 只留最后 8KB，用来判病因，不占内存
# 消费者 socket 的发送超时。**不能不给**：消费者若僵死（进程在、不读），
# sendall 会永久阻塞，这个客户端就永远留在 _clients 里 —— 真机上见过
# clients=3（只有录制+取帧两个消费者），8 个槽满了新消费者就接不进来。
CLIENT_SEND_SEC = 30


# ---------------------------------------------------------------- 单路取流

class Ingest:
    """一路摄像头 ↔ 一条 RTSP 连接 ↔ 一个本机 TS 端口。"""

    def __init__(self, ffmpeg, cam, opts=(), log=None,
                 stall_sec=STALL_SEC, reconnect=5):
        self.ffmpeg = ffmpeg
        self.cam = cam
        self.cid = cam.get("id") or ""
        self.name = cam.get("name") or self.cid
        self.rtsp = str(cam.get("url") or "").strip()
        # 传输方式：tcp（默认）/ udp。
        # 为什么要能选：0.9.83 的免凭据实测（2026-10-26）证明——静默期间相机
        # 20~30 毫秒就能回一个 RTSP 请求，静默前 30 秒的延迟中位数和基线一样，
        # 也就是说链路是通的、设备是活的，**冻住的是那一路的 RTP 媒体通道**，
        # 只有重新建会话能救活（12 段静默，最长 156 秒，无一自愈）。
        # 若病因是「相机 TCP 发送缓冲一堵就放弃媒体流」，UDP 没有流控，
        # 链路抖一下只丢几个包，不会让整条媒体通道死掉 —— 值得 A/B 验证。
        tp = str(cam.get("transport") or "tcp").strip().lower()
        self.transport = tp if tp in ("tcp", "udp") else "tcp"
        self.opts = list(opts or ())        # 与录制同一套探测耐心（-analyzeduration 等）
        self.log = log or (lambda m: None)
        self.stall_sec = int(stall_sec) if stall_sec else HANDSHAKE_SEC
        self.patience_sec = PATIENCE_SEC
        self.reconnect = max(1, int(reconnect))

        self.proc = None
        self.srv = None
        self.port = 0
        self._clients = []
        self._lock = threading.Lock()       # 保护 _clients 与各队列的 append
        self._stop = threading.Event()
        self._threads = []

        self.started_at = 0.0
        self._closing = None                # 正在后台收尾的线程（见 _begin_close）
        self.last_byte = 0.0                # 最近一次从摄像头读到字节的时刻（看门狗用）
        self.bytes_total = 0
        self.clients_seen = 0
        self.tries = 0
        self.stall = 0
        # 停顿统计：这是判断「相机到底停多久」的唯一数据源。上一版一停 20 秒
        # 就杀，所以永远测不到真实停顿长度，也就无从判断该不该换 UDP。
        self.pause_count = 0
        self.pause_max = 0.0
        self.last_pause = 0.0               # 最近一次停顿有多长（含"等不到、直接重启"那类）
        self._paused = False
        self._pause_at = 0.0
        self._pause_from = 0.0
        self._pause_idle = 0.0
        self.next_at = 0.0
        self.state = "idle"                 # idle / live / reconnecting
        self.last_error = ""
        self._errbuf = deque(maxlen=KEEP_ERR_BYTES // 64)

    # -------------------------------------------------- 生命周期

    def start(self):
        if not self.ffmpeg or not self.rtsp:
            return False
        if not self._open_server():
            return False
        self._spawn()
        return True

    def stop(self):
        self._stop.set()
        # 收尾线程是 daemon：这里不等它，进程退出时会被强杀，可能留下 ffmpeg。
        # 退出路径不怕慢，等它把 SIGINT/kill 走完。
        t = self._closing
        if t is not None:
            try:
                t.join(SOFT_KILL_SEC + 3)
            except Exception:
                pass
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=SOFT_KILL_SEC)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    p.kill()
                except (OSError, ValueError):
                    pass
        self._close_clients()
        if self.srv is not None:
            try:
                self.srv.close()
            except OSError:
                pass
            self.srv = None
        self.state = "idle"

    # -------------------------------------------------- 本机广播服务

    def _open_server(self):
        """只绑 127.0.0.1、端口交给内核挑 —— 不暴露到局域网，也不会端口撞车。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            s.listen(8)
            s.settimeout(0.5)
        except OSError as e:
            self.last_error = "本机转发端口开不出来：%s" % e
            self.log("取流 %s 启动失败：%s" % (self.name, self.last_error))
            return False
        self.srv = s
        self.port = s.getsockname()[1]
        t = threading.Thread(target=self._accept_loop, daemon=True,
                             name="ingest-acc-%s" % self.cid)
        t.start()
        self._threads.append(t)
        return True

    @property
    def url(self):
        """给本机 ffmpeg 消费的输入 URL。listen=0 表示当客户端连过来，而不是等连接。"""
        if not self.port:
            return None
        return "tcp://127.0.0.1:%d?listen=0" % self.port

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                sock.settimeout(CLIENT_SEND_SEC)   # 僵死消费者要能被踢掉
            except OSError:
                pass
            cl = {"sock": sock, "q": deque(), "gone": False}
            with self._lock:
                self._clients.append(cl)
                self.clients_seen += 1
            threading.Thread(target=self._serve_client, args=(cl,), daemon=True,
                             name="ingest-cli-%s" % self.cid).start()

    def _serve_client(self, cl):
        sock = cl["sock"]
        # 消费端（ffmpeg）读得慢的时候，sendall 会阻塞；阻塞只影响这一个消费者，
        # 广播侧靠队列上限自保，所以这里可以放心地慢。
        while not self._stop.is_set() and not cl["gone"]:
            with self._lock:
                chunk = cl["q"].popleft() if cl["q"] else None
            if chunk is None:
                time.sleep(0.02)
                continue
            try:
                sock.sendall(chunk)
            except OSError:
                break
        cl["gone"] = True
        try:
            sock.close()
        except OSError:
            pass
        with self._lock:
            if cl in self._clients:
                self._clients.remove(cl)

    def _broadcast(self, chunk):
        with self._lock:
            for cl in self._clients:
                if cl["gone"]:
                    continue
                q = cl["q"]
                if len(q) >= BACKLOG:
                    q.popleft()             # 慢消费者丢最旧：宁可它花屏，不许拖住取流
                q.append(chunk)

    def _close_clients(self):
        with self._lock:
            clients, self._clients = self._clients, []
        for cl in clients:
            cl["gone"] = True
            try:
                cl["sock"].close()
            except OSError:
                pass

    # -------------------------------------------------- 取流进程

    def _cmd(self):
        c = [self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
        for o in self.opts:
            if isinstance(o, (list, tuple)):
                c.extend([str(x) for x in o])
            else:
                c.append(str(o))
        c += ["-rtsp_transport", self.transport, "-i", self.rtsp,
              # 只要视频：录像本来就是 -an，音频在 TS 里纯属多出来的失败点
              #（G.711 塞不进 MP4 这类问题是 Frigate 文档里的经典坑）。
              "-map", "0:v:0", "-c", "copy",
              "-f", "mpegts", "-flush_packets", "1", "pipe:1"]
        return c

    def _spawn(self):
        # 上一轮还在收尾就别起新的：两个 ffmpeg 同时连同一个摄像头，
        # 廉价机会把新连接挂起来（真机上这就是「越重启越连不上」）。
        if self._stop.is_set() or self._closing is not None:
            return
        try:
            p = subprocess.Popen(self._cmd(), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        except OSError as e:
            self.last_error = "ffmpeg 无法启动：%s" % e
            self.log("取流 %s 启动失败：%s" % (self.name, self.last_error))
            self.next_at = time.time() + self.reconnect
            self.proc = None
            return
        self.proc = p
        self.started_at = time.time()
        self.last_byte = 0.0
        self.state = "live"
        self._errbuf.clear()
        for fn in (self._read_stdout, self._drain_stderr):
            t = threading.Thread(target=fn, args=(p,), daemon=True,
                                 name="ingest-%s-%s" % (fn.__name__[-4:], self.cid))
            t.start()
            self._threads.append(t)

    def _read_stdout(self, p):
        """唯一的读口。读完（进程退出）就收尾，别在这里做任何可能阻塞的事。"""
        out = p.stdout
        while not self._stop.is_set():
            try:
                # read1 有多少给多少；用 read(n) 会攒满 64KB 才吐一次，
                # 2 Mbps 的流就是 0.26 秒的额外延迟，预览和 AI 都吃这个亏。
                chunk = out.read1(CHUNK) if hasattr(out, "read1") else out.read(CHUNK)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            self.last_byte = time.time()
            self.bytes_total += len(chunk)
            self._broadcast(chunk)
        try:
            out.close()
        except (OSError, ValueError):
            pass

    def _drain_stderr(self, p):
        """必须有人读 stderr：管道的 64KB 缓冲满了，ffmpeg 会卡在 write 上假死。

        这与「录制进程只在死的时候才 read(stderr)」不同 —— 取流是长跑进程，
        而且我们靠它的 stderr 判病因，所以要一直读、只留尾巴。
        """
        err = p.stderr
        while not self._stop.is_set():
            try:
                line = err.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            # 带时间戳存：不记时间的 stderr 会被误读。真机 2026-10-26 每一路
            # 停顿都打出同样的三行 non-existing PPS，看着像病因，其实是进程
            # 刚起来探测阶段留下的旧噪声（errbuf 只在 spawn 时清空）。
            self._errbuf.append((time.time(),
                                 line.decode("utf-8", "replace").rstrip()))
        try:
            err.close()
        except (OSError, ValueError):
            pass

    def _err_tail(self, lines=3, width=180, since=None):
        """ffmpeg 最后几行原话，给日志用。

        **这是定位卡顿的唯一入口**：只报「41 秒没有新数据」等于什么都没说，
        到底是连接重置、401、还是不吐关键帧，全在 stderr 里（0.9.11 那次
        就是靠把 stderr 记下来才翻案的）。只取尾巴，128 行全塞进去会
        把业务日志淹掉（日志环形缓冲的教训，见 0.9.72）。
        """
        out = []
        # since 给了就只要这之后的 —— 静默开始前的老噪声与这次停顿无关，
        # 混进来会让人把「探测阶段的老报错」当成停顿的原因。
        for item in list(self._errbuf)[-max(lines * 4, 24):]:
            ts, line = item if isinstance(item, tuple) else (None, item)
            if since is not None and ts is not None and ts < since:
                continue
            line = (line or "").strip()
            if line:
                out.append(line[:width])
        return " | ".join(out[-lines:])

    def _why_quiet(self, since):
        """静默的原因说明：有新报错就给原话，没有就说「没有」。"""
        tail = self._err_tail(since=since)
        if tail:
            return "；ffmpeg 自静默起：%s" % tail
        return "；ffmpeg 静默期间没有任何新报错（= 相机没在发数据，不是我们解码失败）"

    def _begin_close(self, p, wait_after, now=None):
        """发起收尾：SIGINT 立刻发，**等待放到后台线程**。

        以前这里是 p.wait(timeout=5) 直接写在巡检线程里 —— 5 路同时卡就是
        串行 25 秒，这期间没有任何一路被巡检，恢复时间被自己拉长。这正是
        0.9.78 在 recorder 里修过的同一个错误，取流层又犯了一次。
        """
        now = now if now is not None else time.time()
        self.proc = None
        self._paused = False
        self.state = "reconnecting"
        self.next_at = now + max(1, wait_after)
        if p is None:
            return
        t = threading.Thread(target=self._close_worker, args=(p,), daemon=True,
                             name="ingest-close-%s" % self.cid)
        self._closing = t
        t.start()

    def _close_worker(self, p):
        try:
            if p.poll() is not None:
                return                      # 已经自己退了，没得收
            try:
                p.send_signal(2)            # SIGINT：让 ffmpeg 正常关闭 RTSP 会话
                p.wait(timeout=SOFT_KILL_SEC)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    p.kill()
                    p.wait(timeout=2)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        finally:
            self._closing = None

    def _kill(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        if p.poll() is None:
            try:
                p.send_signal(2)            # SIGINT：让 ffmpeg 干净退出
                p.wait(timeout=SOFT_KILL_SEC)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    p.kill()
                except (OSError, ValueError):
                    pass
                try:
                    p.wait(timeout=2)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass

    # -------------------------------------------------- 看门狗

    def tick(self, now=None):
        """一次巡检。调用方按 1~2 秒节奏调，与录制巡检共用线程。"""
        if self._stop.is_set() or not self.port:
            return
        now = now if now is not None else time.time()
        p = self.proc

        if p is None:
            if now >= self.next_at and self.rtsp and self.ffmpeg:
                self._spawn()
            return

        rc = p.poll()
        if rc is not None:
            tail = self._err_tail()
            self.last_error = tail or ("rc=%s" % rc)
            self.tries += 1
            wait = min(MAX_BACKOFF, self.reconnect * (2 ** min(self.tries - 1, 9)))
            self._begin_close(p, wait, now)
            self.log("取流 %s 断开（rc=%s），%d 秒后第 %d 次重连%s"
                     % (self.name, rc, wait, self.tries,
                        ("：" + tail) if tail else ""))
            return

        # 稳定跑满 5 分钟就把失败计数清零：白天的偶发抖动不该累积成 5 分钟退避
        if now - self.started_at > 300 and self.tries:
            self.tries = 0

        idle = now - (self.last_byte or self.started_at)

        # ---- A. 从 spawn 到现在一个字节都没有：握手死了，自愈不了 ----
        if not self.last_byte:
            if idle > self.stall_sec:
                self.stall += 1
                why = self._why_quiet(self.started_at)
                self.last_error = "%.0f 秒没吐过数据（握手/探测失败）%s" % (idle, why)
                wait = min(30, self.reconnect * (2 ** min(self.stall - 1, 9)))
                self._begin_close(p, wait, now)
                self.log("取流 %s 握手失败（%.0f 秒没吐过数据），重启"
                         "（第 %d 次，%d 秒后再连）%s"
                         % (self.name, idle, self.stall, wait, why))
            return

        # ---- B. 出过数据之后停了：等一小会儿，再认命重连 ----
        if idle > self.patience_sec:
            # 0.9.82：这一路是「停到打穿耐心还没回来」，走的是本分支而不是下面
            # 的"恢复"分支 —— 如果不在这里补记，pause_max 永远是 0，
            # 诊断卡上「停顿 N 次（最长 0 秒）」等于什么都没测到。
            # 真机 2026-10-26 09:33 三路就是这样：显示停顿 1 次、最长 0 秒。
            self.pause_max = max(self.pause_max, idle)
            self.last_pause = idle
            if not self._paused:
                # tick 间隔大于耐心（或一路直接打穿）时根本没经过中间的停顿态，
                # 这里补记一次，否则"停顿 N 次"会漏掉最严重的那些。
                self.pause_count += 1
            self._paused = False
            self.stall += 1
            why = self._why_quiet(self.last_byte)
            self.last_error = ("%.0f 秒没有新数据（超过 %d 秒耐心）%s"
                               % (idle, self.patience_sec, why))
            # 0.9.83：**停顿后的重连一律短退避**，不要再指数爬升。
            # 0.9.81 的真机账（10:03–12:19，118 次停顿）说明：这台机器上一轮
            # 周期 = 静默 121 秒 + 退避 30 秒 + 探测 10 秒 + 正常 55 秒，
            # 退避已经爬到封顶的 30 秒，而它换来的「少连一点」毫无意义 ——
            # 每次重连都真的能连上，只是过几十秒又停。指数退避本来是给
            # 「连不上」用的（见 A 分支），套在「连得上但会停」身上就是白丢。
            wait = self.reconnect
            self._begin_close(p, wait, now)
            self.log("取流 %s 静默 %.0f 秒仍无数据（超过 %d 秒耐心），重启"
                     "（第 %d 次，%d 秒后再连）%s"
                     % (self.name, idle, self.patience_sec, self.stall, wait, why))
            return

        # 停顿中：只记一次，进程一个字节都不碰
        if idle > PAUSE_LOG_SEC:
            if not self._paused:
                self._paused = True
                self.pause_count += 1
                self._pause_from = now - idle      # 真实静默开始时刻
                self._pause_idle = idle
                self.log("取流 %s 停顿（%.0f 秒没有数据，保持连接等它自己回来）%s"
                         % (self.name, idle, self._why_quiet(self.last_byte)))
            return

        # 恢复了：把这次停顿的真实长度记下来（判断要不要换传输全靠这个数）
        if self._paused:
            dur = max(0.0, self.last_byte - self._pause_from)
            self.pause_max = max(self.pause_max, dur)
            self.last_pause = dur
            self._paused = False
            self.log("取流 %s 恢复（停顿了 %.0f 秒，相机自己回来的）"
                     % (self.name, dur))

    # -------------------------------------------------- 对外

    def _key_changed(self, cam):
        """地址或传输方式变了就得重建取流进程（ffmpeg 不会自己换源/换传输）。"""
        tp = str(cam.get("transport") or "tcp").strip().lower()
        tp = tp if tp in ("tcp", "udp") else "tcp"
        return ((cam.get("url") or "", cam.get("id") or "", tp)
                != (self.rtsp, self.cid, self.transport))

    def status(self):
        p = self.proc
        live = bool(p is not None and p.poll() is None)
        return {
            "id": self.cid, "name": self.name,
            "state": ("live" if live else self.state),
            "port": self.port, "pid": p.pid if live else None,
            "clients": len(self._clients),
            "bytes": self.bytes_total,
            "idle_sec": (int(time.time() - self.last_byte)
                         if self.last_byte else None),
            "tries": self.tries, "stall": self.stall,
            "paused": self._paused,
            "pause_count": self.pause_count,
            "pause_max_sec": (round(self.pause_max, 1) if self.pause_max else 0),
            "last_pause_sec": (round(self.last_pause, 1) if self.last_pause else 0),
            "error": self.last_error,
        }


# ---------------------------------------------------------------- 管理器

class IngestManager:
    """按摄像头列表维护一组 Ingest。地址/开关变了就重建那一路。"""

    def __init__(self, ffmpeg, cams_fn, opts_fn=None, log=None, enabled=True):
        self.ffmpeg = ffmpeg
        self.cams_fn = cams_fn
        self.opts_fn = opts_fn or (lambda: ())
        self.log = log or (lambda m: None)
        self.enabled = bool(enabled)
        self.items = {}
        self.started = False

    def _key(self, cam):
        tp = str(cam.get("transport") or "tcp").strip().lower()
        if tp not in ("tcp", "udp"):
            tp = "tcp"
        return (cam.get("url") or "", cam.get("id") or "", tp)

    def start(self):
        if not self.enabled:
            return
        self.started = True
        self.reconcile(self.cams_fn(), spawn=True)

    def reconcile(self, cams, spawn=True):
        """让 items 与当前摄像头列表对齐。新增的起、消失的停、地址变的重建。"""
        if not self.enabled:
            return
        want = {}
        for cam in (cams or []):
            if not isinstance(cam, dict) or cam.get("enabled") is False:
                continue
            if not str(cam.get("url") or "").strip():
                continue
            want[cam.get("id") or ""] = cam

        for cid in list(self.items):
            if cid not in want:
                self.items.pop(cid).stop()
                continue
            if self.items[cid]._key_changed(want[cid]):
                self.items.pop(cid).stop()

        for cid, cam in want.items():
            if cid in self.items:
                continue
            ing = Ingest(self.ffmpeg, cam, opts=self.opts_fn(), log=self.log)
            if spawn:
                ing.start()
            self.items[cid] = ing

    def tick(self, now=None):
        for ing in list(self.items.values()):
            try:
                ing.tick(now)
            except Exception as e:              # 单路异常不许拖死巡检
                self.log("取流巡检异常（%s）：%r" % (ing.name, e))

    def url_for(self, cam_id):
        """给录制/取帧用的本机 URL；没有就返回 None（调用方退回直连）。"""
        ing = self.items.get(cam_id)
        if ing is None or not ing.url:
            return None
        return ing.url

    def starved_of(self, cam_id):
        """上游这一路**现在有没有在稳定吐数据**（False 才有）。

        录制层用它给自己松绑：上游压根没有数据的时刻，去重启录制没有意义 ——
        真机 2026-10-26 09:33 就是录制层 75 秒先判死、取流 120 秒才认命，
        两轮重启把 120 秒的相机缺失放大成 140 秒空洞。责任应当单向：
        **等不等由取流决定，录制只管把已经拿到的字节写下去**。

        注意这里判的不是"停顿态"一个标志位，而是「上游现在给不出数据」的
        全部情形 —— 只判 `_paused` 会漏掉两个窗口：认命重连的退避/探测期
        （此时 `_paused` 已清，但一个字节都不会来），以及进程刚起还没握上手。
        这两个窗口里录制层的分片 mtime 一样是旧的，一样会误判成自己卡住。
        """
        ing = self.items.get(cam_id)
        if ing is None:
            return False
        if ing._paused:
            return True
        if ing.proc is None or ing._closing is not None:
            return True
        last = ing.last_byte or 0.0
        if not last:
            return True
        return (time.time() - last) > ing.stall_sec

    def status(self):
        return {cid: ing.status() for cid, ing in self.items.items()}

    def stop(self):
        for ing in list(self.items.values()):
            try:
                ing.stop()
            except Exception:
                pass
        self.items.clear()
