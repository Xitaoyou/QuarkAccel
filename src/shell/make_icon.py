# -*- coding: utf-8 -*-
"""生成 QuarkAccel 的窗口/文件图标（多尺寸 .ico）。纯 Pillow，无外部资源。"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def gradient(size, top, bottom):
    g = Image.new("RGB", (1, size))
    px = g.load()
    for y in range(size):
        t = y / float(size - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return g.resize((size, size), Image.BILINEAR)


def bolt(draw, cx, cy, w, h):
    """向下的闪电（加速感），坐标按 1024 基准。"""
    pts = [
        (cx + w * 0.06, cy - h / 2),
        (cx - w * 0.34, cy + h * 0.10),
        (cx - w * 0.02, cy + h * 0.10),
        (cx - w * 0.16, cy + h / 2),
        (cx + w * 0.36, cy - h * 0.08),
        (cx + w * 0.02, cy - h * 0.08),
    ]
    draw.polygon(pts, fill=(255, 255, 255, 255))


def build():
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = gradient(S, (46, 122, 255), (13, 83, 255)).convert("RGBA")
    mask = rounded_mask(S, int(S * 0.235))
    base.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(base)
    # 顶部高光
    hi = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hi).ellipse([-S * 0.35, -S * 0.72, S * 1.35, S * 0.42],
                               fill=(255, 255, 255, 26))
    base = Image.alpha_composite(base, Image.composite(
        hi, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))

    d = ImageDraw.Draw(base)
    bolt(d, S * 0.50, S * 0.505, S * 0.62, S * 0.60)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [base.resize((s, s), Image.LANCZOS) for s in sizes]
    out = os.path.join(HERE, "app.ico")
    imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes])
    base.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "app.png"))
    print("ICON_OK", out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    build()
