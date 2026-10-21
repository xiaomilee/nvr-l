#!/usr/bin/env python3
"""生成 fpk 图标（纯标准库，不依赖 PIL）。

图形：飞牛原生风格的 squircle 底板 + 白色摄像头简图（枪机侧视：机身、镜头环、支架）。
配色按飞牛官方图标规范采样：竖向渐变 #4778FF → #3D5DFF，图形纯白，占比约 68% 高度。

    python tools/gen_icons.py
"""
import math
import os
import struct
import zlib

GRAD_TOP = (0x47, 0x78, 0xFF)      # 飞牛官方图标底色上端
GRAD_BOTTOM = (0x3D, 0x5D, 0xFF)   # 下端
WHITE = (255, 255, 255, 255)
CLEAR = (0, 0, 0, 0)

# 枪机侧视的几何：先在「正放」坐标系里描述，最后整体轻微旋转，避免呆板
TILT = -8.0 * math.pi / 180.0
PIVOT = (0.5, 0.52)


def rotate(u, v):
    du, dv = u - PIVOT[0], v - PIVOT[1]
    c, s = math.cos(TILT), math.sin(TILT)
    return PIVOT[0] + du * c - dv * s, PIVOT[1] + du * s + dv * c


def squircle(u, v, n=3.6):
    x, y = (u - 0.5) * 2.0, (v - 0.5) * 2.0
    return abs(x) ** n + abs(y) ** n <= 1.0


def round_rect(u, v, x0, y0, x1, y1, r):
    if u < x0 or u > x1 or v < y0 or v > y1:
        return False
    cx = min(max(u, x0 + r), x1 - r)
    cy = min(max(v, y0 + r), y1 - r)
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def circle(u, v, cx, cy, r):
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def glyph(u, v):
    """返回 True = 该点属于白色图形。挖空判断必须先于包含它的白色形状。"""
    # 镜头高光（在玻璃内部，最先判）
    if circle(u, v, 0.700, 0.400, 0.030):
        return True
    # 镜头玻璃（挖空露出底色）
    if circle(u, v, 0.735, 0.435, 0.093):
        return False
    # 镜头外环
    if circle(u, v, 0.735, 0.435, 0.148):
        return True
    # 机身顶部指示灯（挖空）
    if circle(u, v, 0.300, 0.352, 0.024):
        return False
    # 机身（左端圆角收口）
    if round_rect(u, v, 0.225, 0.305, 0.720, 0.565, 0.128):
        return True
    # 支架立臂
    if round_rect(u, v, 0.335, 0.545, 0.430, 0.815, 0.032):
        return True
    # 支架底座
    if round_rect(u, v, 0.235, 0.795, 0.545, 0.865, 0.032):
        return True
    return False


def sample(u, v):
    if not squircle(u, v):
        return CLEAR
    ru, rv = rotate(u, v)
    if glyph(ru, rv):
        return WHITE
    t = min(1.0, max(0.0, (v - 0.06) / 0.88))          # 竖向渐变，顶端亮
    return (round(GRAD_TOP[0] + (GRAD_BOTTOM[0] - GRAD_TOP[0]) * t),
            round(GRAD_TOP[1] + (GRAD_BOTTOM[1] - GRAD_TOP[1]) * t),
            round(GRAD_TOP[2] + (GRAD_BOTTOM[2] - GRAD_TOP[2]) * t),
            255)


def render(size, ss=4):
    rows = []
    step = 1.0 / (size * ss)
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    cr, cg, cb, ca = sample((x * ss + sx + 0.5) * step,
                                            (y * ss + sy + 0.5) * step)
                    r += cr * ca
                    g += cg * ca
                    b += cb * ca
                    a += ca
            n = ss * ss
            if a == 0:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes((r // a, g // a, b // a, a // n))
        rows.append(row)
    return rows


def write_png(path, size, rows):
    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    targets = [
        (os.path.join(root, "ICON.PNG"), 64, 4),
        (os.path.join(root, "ICON_256.PNG"), 256, 3),
        (os.path.join(root, "app", "ui", "images", "icon-64.png"), 64, 4),
        (os.path.join(root, "app", "ui", "images", "icon-256.png"), 256, 3),
    ]
    for path, size, ss in targets:
        n = write_png(path, size, render(size, ss))
        print("%-46s %dx%d  %d bytes" % (os.path.relpath(path, root), size, size, n))
