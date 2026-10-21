# -*- coding: utf-8 -*-
"""独立校验 fpk 包内容：解开 app.tgz，逐项断言本轮 5 个改动都进包了。

不依赖 build.sh 的输出，只看真实字节。
    python tools/verify_pkg.py dist/nvr-0.9.21_x86.fpk
"""
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile

MUST_HAVE_UI = [
    # 第 2 项：资源条只留存储+目录
    "updateResBar",
        'id="resDir"',
    # 第 4 项：直播真的动
    "mountLiveStream",
    "liveStreamUrl",
    "STREAM_Q",
    "fps=8&w=960",
    "fps=4&w=480",
    "hasphoto",
    # 第 5 项：播放按钮移出画面到底栏
    'id="lrPlay"',
    "lr-pausebtn",
    # 第 1 项：录制状态可见
    "recCamsBlock",
    "ffmpeg_missing",
    # 第 3 项：雪碧图已删
]
MUST_NOT_HAVE_UI = [
    # 第 2 项删掉的实时占用
    'id="resCpu"',
    'id="resMem"',
    'id="resGpu"',
    "function cardGpu",
    "gpuLine",
    "function fmtPct",
    "function fmtMB",
    # 第 5 项删掉的画面上悬浮胶囊
    "lr-float",
    "data-lrplay",
    # 第 3 项删掉的雪碧图
    "sprite",
    "GS.sprite",
]
MUST_HAVE_SRV = {
    "server/ffmpeg_cmd.py": [
        "def live_stream_cmd",
        '"-an"',
        "nobuffer",
        "mpjpeg",
        # 0.9.18：倍速导出必须重编码压时间轴
        "setpts=PTS/",
    ],
    "server/nvr_slim.py": [
        "MAX_LIVE_STREAMS = 8",
        "ffmpeg_cmd",
        "os.read",
        # 0.9.18：目录探测接口 + 每路缩时开关/倍速透传
        "/api/dir/check",
        "probe_record_dir",
        "clip_speed_of",
        "condense",
        'or runtime.DEFAULTS["record_dir"]',
        # 录制诊断：读流信息 + 试录 8 秒，把"谁的问题"定死（0.9.15）
        "/api/record/probe",
        "def _probe_one(cam, ff, fp, root=None, seg_sec=None)",
        # 试录必须走真实录制路径（0.9.16）："试录成功"要能为整条链路作证
        "__probe__-%H-%M-%S.mp4.part",
        "recorder.cam_dir(root, dname)",
        "def _safe_res",
        "_tb_where",
    ],
    "server/recorder.py": [
        "STALL_SEC",
        "从未写入",
        "ffmpeg_missing",
        "目录不可写",
        # `.part` 后缀根因的修复：显式 -map 绕开按扩展名猜封装（0.9.17）
        '"-map", "0:v:0"',
        # 探测耐心 + 参数自愈 + 错误分类（0.9.15）
        "INPUT_OPTS",
        "-analyzeduration",
        "parse_bad_option",
        "classify_error",
        '"matches no streams"',
        "def hint_of",
        "_absorb_bad_opts",
    ],
    "server/sysinfo.py": [
        "_TICK = (0.0, 0.0, 0.0)",
    ],
    "server/runtime.py": ['VERSION = "0.9.21"',
                          'record_dir": "/vol1/@appshare/nvr-output"',
                          '"service_port": 18080',
                          "def probe_record_dir"],
    # 0.9.18：缩时链路要把每路开关与倍速真正用起来
    "server/link.py": ["condense", "clip_speed_of"],
}
MUST_NOT_HAVE_SRV = {
    # 只查「作为 ffmpeg 命令数组元素」出现的形式（带引号）；注释里提到这些
    # 名字是刻意的说明文字，不能算残留。p.wait(timeout=5) 是 Python 参数，不匹配。
    "server/recorder.py": ['"-timeout"', '"-rw_timeout"', '"-stimeout"'],
    "server/runtime.py": ['"sprite_thumbs"'],
    # 录像目录曾经有两个互不相同的写死回退值，配置写坏时会让设置页显示
    # /vol1 而录制器实际写 /vol2。现在只认 runtime.DEFAULTS 一处。
    "server/nvr_slim.py": ['"/vol2/1000/NVR"'],
}

ok = 0
bad = []


def chk(cond, label):
    global ok
    if cond:
        ok += 1
    else:
        bad.append(label)


def main(path):
    raw = open(path, "rb").read()
    print("包：%s  %.1f KB  md5=%s"
          % (os.path.basename(path), len(raw) / 1024.0,
             hashlib.md5(raw).hexdigest()))

    # fpk = tar.gz，成员名可能带 ./ 前缀
    outer = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    blobs = {}
    for m in outer.getmembers():
        if m.isfile():
            blobs[m.name.lstrip("./")] = outer.extractfile(m).read()
    outer.close()
    print("成员 %d 个：%s" % (len(blobs), ", ".join(sorted(blobs))))
    chk("app.tgz" in blobs, "包内缺 app.tgz")
    chk("manifest" in blobs, "包内缺 manifest")
    if "manifest" not in blobs:
        return

    mf = blobs["manifest"].decode("utf-8", "replace")
    import re as _re
    vline = _re.search(r"(?m)^\s*version\s*=\s*(\S+)", mf)
    chk(bool(vline) and vline.group(1) == "0.9.21",
        "manifest 版本号不是 0.9.21：%s" % (vline.group(1) if vline else "无"))
    print("manifest 版本行：%s"
          % [l for l in mf.splitlines() if l.strip().startswith("version")])

    # changelog 在 fnOS 里是单行、用 " | " 分段。少一段就说明打包时丢了一条说明。
    cl = _re.search(r"(?m)^\s*changelog\s*=\s*(.+)$", mf)
    n_cl = len(cl.group(1).split(" | ")) if cl else 0
    print("changelog 分段：%d" % n_cl)
    chk(n_cl >= 6, "changelog 只有 %d 段，本轮应有 6 段" % n_cl)

    # manifest 做 CRC 校验（fnOS 会校验，坏包直接装不上）
    crc_line = [l for l in mf.splitlines() if l.strip().startswith("crc")]

    # ---- 外层成员：0.9.19 的核心教训是 wizard 从没进过包 ----
    # 向导（端口/目录选择）依赖包根 wizard/install；桌面入口端口依赖外层 ui/config；
    # 升级端口保护依赖 cmd/upgrade_callback。三者任何一个缺席都是静默故障：
    # 向导不出现、图标指错端口，安装器不报任何错。
    for n in ("wizard/install", "wizard/config", "cmd/install_callback",
              "cmd/upgrade_callback", "ui/config"):
        chk(n in blobs, "包外层缺成员 %s" % n)
    if "wizard/install" in blobs:
        wi = blobs["wizard/install"].decode("utf-8", "replace")
        try:
            steps = json.loads(wi)
            chk(isinstance(steps, list) and len(steps) >= 2, "wizard/install 不是步骤数组")
            fields = [it.get("field") for st in steps for it in st.get("items", [])
                      if isinstance(it, dict)]
            chk("wizard_port" in fields, "wizard/install 缺 wizard_port 字段")
            chk("wizard_record_dir" in fields, "wizard/install 缺 wizard_record_dir 字段")
            chk('"18080"' in wi, "wizard/install 默认端口不是 18080")
            chk("/vol1/@appshare/nvr-output" in wi, "wizard/install 默认录制目录不是共享目录")
        except ValueError as e:
            chk(False, "wizard/install JSON 解析失败：%s" % e)
    if "ui/config" in blobs:
        uic = blobs["ui/config"].decode("utf-8", "replace")
        chk('"port": "18080"' in uic, "包外层 ui/config 端口不是 18080（桌面图标会指错）")
    icb = blobs.get("cmd/install_callback", b"").decode("utf-8", "replace")
    # 0.9.20：目录回退链——安装不再因默认目录建不出而失败。
    # 注释里的旧默认值是教训记录，忽略 # 开头的行再断言。
    icb_code = "\n".join(l for l in icb.splitlines() if not l.lstrip().startswith("#"))
    chk("can_write()" in icb_code, "install_callback 缺目录真实写探针（can_write）")
    chk("/vol1/@appshare/nvr-output" in icb_code, "install_callback 回退链缺共享目录")
    chk("/vol2/1000/NVRL" not in icb_code, "install_callback 仍引用不可行的默认目录 /vol2/1000/NVRL")
    chk("DIR_WARN" in icb_code, "install_callback 目录降级时缺少日志留痕（DIR_WARN）")
    chk("PORT_HAVE" in icb, "install_callback 缺升级端口保护（PORT_HAVE）")
    chk("pinport --port" in icb, "install_callback 缺 pinport 对齐调用")
    chk('PORT_WANT="${wizard_port:-${port:-${wizard_app_port:-${TRIM_SERVICE_PORT' not in icb,
        "install_callback 仍保留旧的端口覆盖链（升级会改用户端口）")
    ucb = blobs.get("cmd/upgrade_callback", b"").decode("utf-8", "replace")
    chk("pinport --port" in ucb, "upgrade_callback 缺升级后端口对齐")

    tf = tarfile.open(fileobj=io.BytesIO(blobs["app.tgz"]), mode="r:gz")
    files = {}
    for m in tf.getmembers():
        if m.isfile():
            files[m.name.lstrip("./")] = tf.extractfile(m).read()
    tf.close()
    print("app.tgz 内 %d 个文件" % len(files))

    ui = files.get("ui/index.html", b"").decode("utf-8", "replace")
    chk(bool(ui), "包内缺 ui/index.html")
    if ui:
        print("ui/index.html  %d 行 %.1f KB"
              % (ui.count("\n") + 1, len(ui.encode("utf-8")) / 1024.0))
        for s in MUST_HAVE_UI:
            chk(s in ui, "UI 缺少：%s" % s)
        for s in MUST_NOT_HAVE_UI:
            chk(s not in ui, "UI 仍残留：%s" % s)

    for name, needles in MUST_HAVE_SRV.items():
        body = files.get(name, b"").decode("utf-8", "replace")
        chk(bool(body), "包内缺 %s" % name)
        for s in needles:
            chk(s in body, "%s 缺少：%s" % (name, s))
    for name, needles in MUST_NOT_HAVE_SRV.items():
        body = files.get(name, b"").decode("utf-8", "replace")
        for s in needles:
            chk(s not in body, "%s 仍残留：%s" % (name, s))

    # 静态 ffmpeg 是否存在，决定真机能不能录制
    bins = [n for n in files if n.startswith("bin/") and "ffmpeg" in n]
    print("app/bin 内 ffmpeg：%s" % (bins or "无（依赖系统 ffmpeg）"))

    print("\n%d 项断言，%d 通过，%d 失败" % (ok + len(bad), ok, len(bad)))
    if bad:
        for b in bad:
            print("  ✗ " + b)
        return 1
    print("✓ 包内容与源码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1
                  else "dist/nvr-0.9.21_x86.fpk"))
