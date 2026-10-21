# app/bin 说明

这里放**静态链接的 ffmpeg / ffprobe（x86_64）**，应用启动时会优先使用：

```
app/bin/ffmpeg    可执行
app/bin/ffprobe   可执行
```

## 为什么建议自带

fnOS 的应用服务常以精简 PATH 启动，`shutil.which("ffmpeg")` 会漏掉 apt 装到
`/usr/bin` 的 ffmpeg；个别机型/容器环境里应用根本看不到宿主机的 ffmpeg。
自带静态二进制后，预览、录制、压制不再依赖系统环境，装上就能用。

## 查找顺序（runtime.find_ffmpeg / find_ffprobe）

1. `app/bin/ffmpeg`（自带，优先，版本自洽）
2. `$PATH` 里的 ffmpeg
3. 常见绝对路径：`/usr/bin`、`/usr/local/bin`、`/opt/ffmpeg/bin`

## 怎么获取二进制

在**网络通畅的机器**（或飞牛本机 SSH）执行：

```bash
bash tools/fetch_ffmpeg.sh        # 下载 BtbN GPL 静态构建，解出 ffmpeg+ffprobe 到 app/bin
```

BtbN GPL 静态构建带 qsv/vaapi 硬件加速和 libx264/libx265，覆盖压制/缩时的软件重编码。
若只做预览与录制（`-c copy`），用 LGPL 构建体积略小；johnvansickle 静态构建也兼容
（约 40MB、含 libx264/libx265、无 qsv）。

放入后重新 `bash build.sh` 打包即可。体积提示：GPL 构建约 150MB，会让 fpk 明显变大。

## 没有自带二进制时

应用会回落系统 ffmpeg。装机后在「设置 → 诊断日志」顶部能看到
「ffmpeg ✓ /path」或「✗ 未找到 ffmpeg」，一眼判断当前用的是哪一个。
