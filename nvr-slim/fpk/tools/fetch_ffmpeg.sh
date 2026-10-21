#!/bin/bash
# 下载静态 ffmpeg + ffprobe（x86_64）到 app/bin，供打包进 fpk。
# 在「网络通畅」的机器上跑（本机或飞牛 SSH 均可），完成后重新 bash build.sh 打包。
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/app/bin"
URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "下载 $URL"
curl -fL --retry 3 -o "$WORK/ffmpeg.tar.xz" "$URL"

echo "解包…"
tar -xJf "$WORK/ffmpeg.tar.xz" -C "$WORK"
DIR="$(find "$WORK" -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)"
[ -n "$DIR" ] || DIR="$WORK"

mkdir -p "$DEST"
for b in ffmpeg ffprobe; do
  src="$(find "$DIR" -type f -name "$b" | head -1)"
  [ -n "$src" ] || { echo "未找到 $b"; exit 1; }
  cp "$src" "$DEST/$b"
  chmod +x "$DEST/$b"
  echo "已写入 $DEST/$b ($(du -h "$DEST/$b" | cut -f1))"
done

echo "完成。现在可以：bash build.sh 重新打包。"
