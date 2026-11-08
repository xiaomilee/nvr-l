#!/usr/bin/env python3
"""把 fpk 部署到测试机并自检，失败自动回滚。

为什么不用 fnOS 应用中心：`fnpack` 只能打包不能安装，`trim_app_center` 不在 PATH，
web 安装接口要登录态。测试机只需要"代码换掉 + 进程重启"，而这一步的语义很明确：
解 app.tgz → 覆盖 TRIM_APPDEST → 重启。所以这里直接做这件事，把包安装器的行为
（保留 var、保留 ui、按 manifest 版本对齐）复刻出来，换来的是**几秒一次**的迭代速度。

代价要说清：这条路径**不校验 fpk 本身的打包正确性**（manifest 字段、privilege、
wizard）。打包问题仍要靠真机手动安装暴露，所以每出一次正式包，仍建议在真机上装一遍。

设计要点：
- 部署前先备份 TRIM_APPDEST，失败按备份还原，不让测试机停在半坏状态；
- 校验不只查 version：新版本号对上但接口全 500 的情况真实发生过；
- 只读接口，不在校验里改配置——改配置会触发录像重启，掩盖真正要测的东西。
"""
import argparse
import hashlib
import json
import os
import posixpath
import sys
import tempfile
import time
import urllib.error
import urllib.request

try:
    import paramiko
except ImportError:
    sys.exit("需要 paramiko：python -m pip install paramiko")

HERE = os.path.dirname(os.path.abspath(__file__))
FPK_ROOT = os.path.dirname(HERE)

# 校验清单：路径 → (说明, 判定函数)
# 判定函数拿到解析后的 JSON，返回 (ok, 摘要)。任何异常都算失败——
# "接口 500 但进程活着"是最容易漏判的一种失败。
CHECKS = [
    # /api/status 没有 ok 字段（它本身就是总览），别拿 ok 去判，否则永远假红
    ("/api/status", "状态总览", lambda d: (bool(d.get("version")), "v%s cams=%s" % (
        d.get("version"), (d.get("cams") or {}).get("total")))),
    ("/api/health", "健康自检", lambda d: (bool(d.get("ok")), "version=%s" % d.get("version"))),
    ("/api/health/gaps", "录像缺口", lambda d: (bool(d.get("ok")), "%d 路有缺口" % len(
        [c for c in (d.get("cams") or []) if c.get("gaps")]))),
    ("/api/link", "取流链路", lambda d: (bool(d.get("ok")), "channels=%d" % len(
        d.get("channels") or (d.get("record") or {}).get("channels") or []))),
    ("/api/log?lines=200", "运行日志", lambda d: (bool(d.get("ok")), "warn=%s err=%s" % (
        d.get("warn"), d.get("err")))),
    ("/api/recordings", "录像清单", lambda d: (bool(d.get("ok")), "cams=%d" % len(
        d.get("cameras") or {}))),
    # 事件中心（0.9.121）。这一项最容易"接口通但没数据"：索引建了、同步也跑了，
    # 却因为口径不对一条都读不出来。所以把条数与索引覆盖天数一起写进报告 ——
    # count=0 是真没事件还是索引没同步，看 days 就能分开（0.9.121 起纳入自检）。
    # 路径不加查询串：走默认「今天 00:00:00 ~ 23:59:59」，免得 curl 把 & 当后台符号。
    ("/api/events", "事件索引", lambda d: (bool(d.get("ok")), "count=%s 索引天数=%s" % (
        d.get("count"), (d.get("index") or {}).get("days")))),
]


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_version(fpk):
    """从本地 manifest 读版本，别依赖远端返回——远端返回什么正是要验证的东西。"""
    with open(os.path.join(FPK_ROOT, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("version"):
                return line.split("=", 1)[1].strip()
    return ""


class Box:
    def __init__(self, host, user, pwd, port=22):
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.c.connect(host, port=port, username=user, password=pwd,
                       timeout=15, allow_agent=False, look_for_keys=False)
        self.sudo_pwd = pwd
        # 0.9.120 起 /api/* 要凭证。这里存一份，ssh_http 自动带上；
        # 空串表示目标还是老版本（那时接口不设防，多带一个头也无害）。
        self.token = ""

    def run(self, cmd, timeout=60, sudo=False, check=False):
        if sudo:
            # fnOS 的 admin 在 Administrators 组，sudo 需要密码；-S 从 stdin 读，
            # 把密码塞进命令字符串会在 ps 里露出来，所以走 stdin。
            cmd = "sudo -S -p '' bash -c %s" % shquote(cmd)
        stdin, out, err = self.c.exec_command(cmd, timeout=timeout)
        if sudo:
            stdin.write(self.sudo_pwd + "\n")
            stdin.flush()
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError("命令失败 rc=%d: %s\n%s%s" % (rc, cmd, o, e))
        return rc, o, e

    def put(self, local, remote):
        s = self.c.open_sftp()
        s.put(local, remote)
        s.close()

    def http(self, port, path, timeout=15):
        url = "http://127.0.0.1:%d%s" % (port, path)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read().decode("utf-8", "replace")
        except Exception as ex:
            return 0, str(ex)


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


# 生命周期脚本必须以 nvr 身份跑。直接 sudo 会把 nvr_slim.py 起成 root，
# var 目录里的文件属主跟着变，下一次正常启动反而因权限失败。
# 另外 main start 里的 nohup 没有重定向，SSH 通道会一直等子进程退出，
# 所以这层包装顺手把输出接走。
CTL_WRAP = """#!/bin/bash
set -u
export TRIM_APPDEST=%s
export TRIM_PKGVAR=%s
export TRIM_SERVICE_PORT=%d
su -s /bin/bash nvr -c "bash %s $1" >/tmp/nvr-main.log 2>&1 </dev/null
exit $?
"""


def install_ctl(box, app_dir, var_dir, port, ctl):
    """把启停包装写到测试机。每次部署重写一遍，避免沿用上一版写死的路径。"""
    s = CTL_WRAP % (app_dir, var_dir, port, ctl)
    # newline="\n" 不能省：Windows 上按文本写会把行尾变成 CRLF，
    # 传到 Linux 后 `set -u\r` 被 bash 当成非法选项，整个启停脚本直接失效——
    # 表现是"停不掉也起不来"，而错误信息只提 set，看不出是换行符的问题。
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                     suffix=".sh", delete=False) as f:
        f.write(s)
        local = f.name
    try:
        box.put(local, "/tmp/nvr-ctl-deploy.sh")
        box.run("chmod +x /tmp/nvr-ctl-deploy.sh")
    finally:
        os.unlink(local)


def alive(box):
    """数还活着的 nvr_slim 进程。停服务不能只看脚本返回码——pid 文件过期时
    stop 会「成功」退出，进程还在跑，然后新进程因为端口被占偷偷换个端口起，
    测了半天测的是旧代码。"""
    rc, o, _ = box.run("pgrep -c -f 'nvr_slim[.]py' || true")
    try:
        return int((o or "0").strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def real_port(box, var_dir, fallback):
    """应用在端口被占时会顺延，真实端口写在 port.txt。用它，别用命令行传的。"""
    rc, o, _ = box.run("cat %s/port.txt 2>/dev/null | tr -dc '0-9'" % var_dir)
    try:
        p = int((o or "").strip())
        return p if 0 < p < 65536 else fallback
    except ValueError:
        return fallback


def stop_app(box, port, var_dir):
    box.run("bash /tmp/nvr-ctl-deploy.sh stop", sudo=True, timeout=90)
    # 脚本说停了不算数，进程还在才算数
    for _ in range(10):
        if alive(box) == 0:
            return True
        time.sleep(1)
    box.run("pkill -f 'nvr_slim[.]py'", sudo=True)
    for _ in range(10):
        if alive(box) == 0:
            return True
        time.sleep(1)
    box.run("pkill -9 -f 'nvr_slim[.]py'", sudo=True)
    time.sleep(2)
    return alive(box) == 0


def start_app(box, port, var_dir):
    rc, o, e = box.run("bash /tmp/nvr-ctl-deploy.sh start", sudo=True, timeout=120)
    if rc != 0:
        raise RuntimeError("启动失败 rc=%d：%s%s" % (rc, o[-400:], e[-400:]))
    return real_port(box, var_dir, port)


def read_token(box, var_dir):
    """读测试机上的 API 凭证（var/secret.json）。读不到返回空串。

    为什么由部署工具读：0.9.120 起所有 /api/* 都要凭证，而凭证是服务第一次启动
    时自己生成的。工具要自检，就必须自己拿到这枚 token —— 让用户手工粘一次
    会变成"每轮都要人守着"，等于把自动化废掉。
    """
    rc, o, _ = box.run("cat %s/secret.json 2>/dev/null" % var_dir)
    if rc != 0 or not o.strip():
        return ""
    try:
        return str(json.loads(o).get("api_token") or "").strip()
    except (ValueError, AttributeError):
        return ""


def wait_token(box, var_dir, limit=25):
    """新版本起来后才会有 secret.json，等它出现再取。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        tok = read_token(box, var_dir)
        if tok:
            return tok
        time.sleep(1)
    return ""


def ssh_http(box, port, path, timeout=15):
    # %% 是给 Python 的 % 格式化转义：curl 的 -w 里 %{http_code} 必须原样传过去
    hdr = ""
    if box.token:
        hdr = " -H " + shquote("Authorization: Bearer " + box.token)
    rc, o, e = box.run("curl -s -m %d%s -w '\\n%%{http_code}' http://127.0.0.1:%d%s"
                       % (timeout, hdr, port, path), timeout=timeout + 10)
    if "\n" not in o:
        return 0, o
    body, _, code = o.rpartition("\n")
    try:
        return int(code.strip()), body
    except ValueError:
        return 0, o


def wait_version(box, port, want, limit=75):
    """等到新版本号出现。不能只看进程在——旧进程还在时 /api/health 也是 200。"""
    t0 = time.time()
    last = ""
    while time.time() - t0 < limit:
        code, body = ssh_http(box, port, "/api/health", timeout=5)
        if code == 200:
            try:
                last = json.loads(body).get("version") or ""
            except Exception:
                last = body[:80]
            if last == want:
                return True, last, time.time() - t0
        time.sleep(2)
    return False, last, time.time() - t0


def deploy(args):
    fpk = os.path.abspath(args.fpk)
    if not os.path.isfile(fpk):
        sys.exit("找不到包：%s" % fpk)
    version = read_version(fpk)
    digest = md5_file(fpk)
    print("包 %s  version=%s  md5=%s  %d 字节"
          % (os.path.basename(fpk), version, digest, os.path.getsize(fpk)))

    box = Box(args.host, args.user, args.password, args.ssh_port)
    app_dir = args.app_dir
    var_dir = args.var_dir
    ctl = args.ctl
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stage = "/tmp/nvr-deploy-%s" % stamp
    bak = posixpath.join(var_dir, "backup", "app-%s" % stamp)
    cmd_bak = ""

    try:
        # 用 port.txt 里的真实端口探活：应用在端口被占时会顺延，命令行那个不一定准
        cur_port = real_port(box, var_dir, args.port)
        # 先拿旧凭证：目标机器如果已经是 0.9.120+，没这枚 token 连"部署前版本"都读不到
        box.token = read_token(box, var_dir)
        print("凭证：%s" % ("已读到（%s…）" % box.token[:4] if box.token
                            else "无（目标为 0.9.120 之前，接口不设防）"))
        code, before = ssh_http(box, cur_port, "/api/health", timeout=5)
        cur = ""
        if code == 200:
            try:
                cur = json.loads(before).get("version") or ""
            except Exception:
                pass
        print("部署前版本：%s" % (cur or "未知/不可达"))

        print("[1/6] 上传并解包 ...")
        box.run("mkdir -p %s/root %s/app" % (stage, stage), check=True)
        box.put(fpk, "%s/nvr.fpk" % stage)
        box.run("cd %s && tar xzf nvr.fpk -C root && tar xzf root/app.tgz -C app"
                % stage, timeout=180, check=True)
        rc, o, _ = box.run("ls %s/app/server/nvr_slim.py" % stage)
        if rc != 0:
            raise RuntimeError("解包后找不到 server/nvr_slim.py，包结构不对")

        print("[2/6] 备份现有程序目录 → %s" % bak)
        rc, _, _ = box.run("test -d %s" % app_dir)
        has_old = (rc == 0)
        if has_old:
            box.run("mkdir -p %s && cp -a %s %s"
                    % (posixpath.dirname(bak), app_dir, bak), timeout=180, sudo=True, check=True)
        else:
            # 目录不存在（首次部署，或上一次回滚把它清掉了）：没有可备份的东西。
            # 这里**绝对不能**顺手 rm -rf —— 曾经就是这么把测试机的程序目录删空的。
            print("    程序目录不存在，按首次部署处理")
            box.run("mkdir -p %s && chown nvr:nvr %s" % (app_dir, app_dir),
                    sudo=True, check=True)

        print("[3/6] 停服务 ...")
        install_ctl(box, app_dir, var_dir, args.port, ctl)
        if not stop_app(box, args.port, var_dir):
            raise RuntimeError("停不掉旧进程，中止部署（不覆盖正在运行的程序目录）")

        print("[4/6] 覆盖程序目录 ...")
        # --delete 是必要的：新版删掉的模块（比如改名后的旧文件）不清掉的话，
        # 老 .py 会继续被 import，测出来是"新版本跑着旧代码"这种最难查的假象。
        # --checksum 更是必需的：打包器把所有成员 mtime 固定成 1700000000，
        # 而 0.9.118→0.9.119 的 runtime.py 只有版本字符串不同、**字节数完全一样**，
        # 默认的「大小 + mtime」判等会认定没变过而跳过覆盖，于是部署"成功"了、
        # 跑的还是旧版本。这个坑真实发生过，用校验和比才可靠。
        box.run("rsync -a --checksum --delete --chown=nvr:nvr %s/app/ %s/"
                % (stage, app_dir), timeout=300, sudo=True, check=True)

        # 覆盖之后再确认一次文件真的换了：上面那条 rsync 的坑不能再靠信任
        rc, o, _ = box.run("grep -m1 'VERSION = ' %s/server/runtime.py" % app_dir)
        if version not in (o or ""):
            raise RuntimeError("覆盖后版本号没变（%s），rsync 没有真正替换文件"
                               % (o or "").strip()[:60])

        # 生命周期脚本（cmd/）不在程序目录里：fnOS 把它装在 /var/apps/<app>/cmd，
        # "启动成功没有"正是由那里的 main 拿 /api/health 判的。只覆盖 app/ 的话，
        # 0.9.120 第一次部署就撞了：新服务的 /api/health 要凭证、回 401，
        # 而机器上那个 2023 年装的 main 把 401 当失败 —— 进程明明起来了、明明在录，
        # 工具却判"启动超时"，接着回滚，回滚走同一段判定，于是"回滚也失败"。
        # 真升级会整体替换 cmd/，所以这里也替换，测试机才算"装过一次新版本"。
        cmd_dir = args.cmd_dir
        if cmd_dir and box.run("test -d %s" % cmd_dir)[0] == 0:
            cmd_bak = posixpath.join(var_dir, "backup", "cmd-%s" % stamp)
            box.run("cp -a %s %s" % (cmd_dir, cmd_bak), sudo=True, check=True)
            box.run("rsync -a --checksum --chown=root:root %s/root/cmd/ %s/"
                    % (stage, cmd_dir), timeout=120, sudo=True, check=True)
            # 这个目录曾经压根没被覆盖过，所以必须自己核一遍换进去的是什么
            _, want_md5, _ = box.run("md5sum %s/root/cmd/main | cut -d' ' -f1" % stage)
            _, got_md5, _ = box.run("md5sum %s/main | cut -d' ' -f1" % cmd_dir)
            if (want_md5 or "").strip() != (got_md5 or "").strip():
                raise RuntimeError("生命周期脚本没换成新的（%s → %s）"
                                   % ((want_md5 or "?").strip(), (got_md5 or "?").strip()))
            print("    生命周期脚本已同步 → %s（备份 %s）" % (cmd_dir, cmd_bak))
        else:
            print("    跳过生命周期脚本同步（%s 不存在）" % cmd_dir)

        print("[5/6] 启动服务 ...")
        live_port = start_app(box, args.port, var_dir)
        if live_port != args.port:
            print("    端口顺延：%d → %d（port.txt 为准）" % (args.port, live_port))
        # 新版本第一次启动才会生成 secret.json。不先取到它，下面每一步自检都会 401，
        # 而 401 看起来像"部署失败"，实际上只是工具还空着手。
        box.token = wait_token(box, var_dir) or box.token
        if not box.token:
            print("    未读到凭证（老版本或生成失败），按不设防接口继续")

        print("[6/6] 等新版本上线（最多 75 秒）...")
        ok, got, cost = wait_version(box, live_port, version)
        if not ok:
            raise RuntimeError("等不到 %s，实际 %s（%.1fs）" % (version, got, cost))
        print("    上线成功：%s，耗时 %.1fs" % (got, cost))

        report = {"version": version, "md5": digest, "before": cur, "port": live_port,
                  "boot_sec": round(cost, 1), "checks": []}
        bad = []
        for path, label, judge in CHECKS:
            code, body = ssh_http(box, live_port, path, timeout=30)
            detail = "http=%s" % code
            ok2 = False
            if code == 200:
                try:
                    ok2, detail = judge(json.loads(body))
                except Exception as ex:
                    ok2, detail = False, "%s / %s" % (detail, ex)
            report["checks"].append({"path": path, "label": label,
                                     "ok": bool(ok2), "detail": detail})
            print("    %-22s %s  %s" % (label, "OK  " if ok2 else "FAIL", detail))
            if not ok2:
                bad.append("%s %s" % (label, detail))

        # 凭证是否真的生效：故意不带 token 打一次 /api/status，必须 401。
        # 不放进 CHECKS 是因为它的判定口径与其余六项相反 —— 那六项要求"通"，
        # 这一项要求"被拦住"。少了它，"所有人都能读状态"这件事永远不会被发现。
        if box.token:
            keep = box.token
            box.token = ""
            code, _b = ssh_http(box, live_port, "/api/status", timeout=10)
            box.token = keep
            good = (code == 401)
            report["checks"].append({"path": "/api/status(无凭证)", "label": "凭证设防",
                                     "ok": good, "detail": "http=%s（应为 401）" % code})
            print("    %-22s %s  http=%s（应为 401）"
                  % ("凭证设防", "OK  " if good else "FAIL", code))
            if not good:
                bad.append("凭证设防 http=%s（应当 401，说明接口仍在裸奔）" % code)

        if bad:
            raise RuntimeError("校验不通过：" + "；".join(bad))

        # 顺手把日志里的 ERR 捞出来：接口全绿但后台在报错，是另一种失败
        code, body = ssh_http(box, live_port, "/api/log/file?lines=200&access=0", timeout=20)
        errs = []
        if code == 200:
            try:
                for ln in (json.loads(body).get("lines") or []):
                    if "[ERR]" in ln:
                        errs.append(ln)
            except Exception:
                pass
        report["err_lines"] = errs[:10]
        if errs:
            print("    日志有 %d 条 [ERR]：" % len(errs))
            for ln in errs[:5]:
                print("      " + ln[:160])

        box.run("rm -rf %s" % stage)
        # 只留最近 5 个备份，不然测试机磁盘会被自己撑爆
        box.run("ls -1dt %s/backup/app-* 2>/dev/null | tail -n +6 | xargs -r rm -rf"
                % var_dir, sudo=True)

        if args.report:
            os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print("    报告写入 %s" % args.report)
        print("\n部署成功：%s → %s" % (cur or "?", version))
        return 0

    except Exception as ex:
        print("\n部署失败：%s" % ex)
        rc, _, _ = box.run("test -d %s" % bak)
        if rc != 0:
            # 备份还没做就挂了（比如连不上、包不对），此时程序目录没被动过，
            # 再做一次 rm -rf 反而是最危险的动作
            print("备份尚未创建，程序目录未改动，无需回滚")
            box.run("rm -rf %s" % stage)
            return 1
        print("回滚到 %s ..." % bak)
        try:
            box.run("rm -rf %s && cp -a %s %s" % (app_dir, bak, app_dir),
                    timeout=300, sudo=True, check=True)
            if cmd_bak and box.run("test -d %s" % cmd_bak)[0] == 0:
                box.run("rsync -a --checksum --delete --chown=root:root %s/ %s/"
                        % (cmd_bak, args.cmd_dir), timeout=120, sudo=True, check=True)
            back_port = start_app(box, args.port, var_dir)
            if cur:
                ok, got, _ = wait_version(box, back_port, cur, limit=60)
                print("回滚后版本：%s（%s）" % (got, "恢复成功" if ok else "未恢复，需人工看"))
            else:
                print("回滚已执行，原版本未知，需人工确认")
        except Exception as ex2:
            print("回滚也失败：%s" % ex2)
        finally:
            box.run("rm -rf %s" % stage)
        return 1
    finally:
        box.c.close()


def main():
    p = argparse.ArgumentParser(description="部署 fpk 到测试机并自检")
    p.add_argument("--fpk", default=os.path.join(FPK_ROOT, "dist", "nvr-0.9.118_x86.fpk"))
    p.add_argument("--host", default="192.168.2.152")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default="admin")
    p.add_argument("--ssh-port", type=int, default=22)
    p.add_argument("--port", type=int, default=18081, help="应用 HTTP 端口")
    p.add_argument("--app-dir", default="/vol1/@appcenter/nvr")
    p.add_argument("--var-dir", default="/vol1/@appdata/nvr")
    p.add_argument("--ctl", default="/var/apps/nvr/cmd/main")
    p.add_argument("--cmd-dir", default="/var/apps/nvr/cmd",
                   help="fnOS 安装的生命周期脚本目录；真升级会替换它，这里也替换")
    p.add_argument("--report", default="")
    args = p.parse_args()
    sys.exit(deploy(args))


if __name__ == "__main__":
    main()
