"""
Builds the launcher's image assets. Run once after changing anything here:

    python tools/make_assets.py

Needs numpy and Pillow (dev only; the launcher itself only needs Pillow).

Outputs into assets/:
    logo.png        Project Reclaimer wordmark, cut out of the game's loading
                    screen with a real alpha channel
    emblem.png      the loading-screen ring emblem, cut out the same way
    background.jpg  original backdrop (deep space, planet rim, hex grid)
    icon.ico        window/exe icon made from the emblem
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "source-screenshot.png"
OUT = ROOT / "assets"

# Regions of the source screenshot (1066 x 401)
LOGO_BOX = (40, 28, 1000, 178)
EMBLEM_BOX = (482, 222, 566, 304)
TEXT_BOX = (290, 300, 760, 350)


# --------------------------------------------------------------------------
# Cut-outs
# --------------------------------------------------------------------------

def fit_background(img: np.ndarray, masked_boxes) -> np.ndarray:
    """Fit a smooth 2D polynomial to the plain gradient behind the logo."""
    h, w, _ = img.shape
    yy, xx = np.mgrid[0:h, 0:w]
    keep = np.ones((h, w), bool)
    for x0, y0, x1, y1 in masked_boxes:
        keep[y0:y1, x0:x1] = False
    xs = (xx / w * 2 - 1)
    ys = (yy / h * 2 - 1)
    terms = [np.ones_like(xs)]
    for i in range(1, 5):
        for j in range(0, i + 1):
            terms.append(xs ** (i - j) * ys ** j)
    A = np.stack([t[keep] for t in terms], 1)
    full = np.stack([t.ravel() for t in terms], 1)
    bg = np.zeros_like(img)
    for c in range(3):
        coef, *_ = np.linalg.lstsq(A, img[..., c][keep], rcond=None)
        bg[..., c] = (full @ coef).reshape(h, w)
    return bg


def cut_out(img: np.ndarray, bg: np.ndarray, box, t_lo=4.0, t_hi=46.0) -> Image.Image:
    """Separate foreground from the fitted background (colour unmixing)."""
    x0, y0, x1, y1 = box
    c = img[y0:y1, x0:x1]
    b = bg[y0:y1, x0:x1]
    d = c - b
    dist = np.abs(d).max(axis=2)
    a = np.clip((dist - t_lo) / (t_hi - t_lo), 0, 1)
    safe = np.maximum(a, 1e-3)[..., None]
    f = np.clip(b + d / safe, 0, 255)
    rgba = np.dstack([f, a * 255]).astype(np.uint8)
    im = Image.fromarray(rgba, "RGBA")
    return im.crop(im.getbbox())


def make_cutouts():
    src = np.asarray(Image.open(SRC).convert("RGB")).astype(np.float64)
    bg = fit_background(src, [LOGO_BOX, EMBLEM_BOX, TEXT_BOX])
    logo = cut_out(src, bg, LOGO_BOX)
    emblem = cut_out(src, bg, EMBLEM_BOX, t_lo=6, t_hi=40)
    logo.save(OUT / "logo.png", optimize=True)
    emblem.save(OUT / "emblem.png", optimize=True)
    print("logo", logo.size, "emblem", emblem.size)
    return emblem


# --------------------------------------------------------------------------
# Original backdrop
# --------------------------------------------------------------------------

def lerp(a, b, t):
    return a + (b - a) * t


def make_background(w=1920, h=1240):
    rng = random.Random(117)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    nx, ny = xx / w, yy / h

    # Base: radial navy gradient matching the game's loading screen
    cx, cy = 0.5, 0.22
    r = np.sqrt(((nx - cx) * 1.25) ** 2 + (ny - cy) ** 2)
    t = np.clip(r / 0.95, 0, 1) ** 1.15
    top = np.array([28, 42, 66], float)
    edge = np.array([5, 9, 16], float)
    img = top[None, None, :] * (1 - t[..., None]) + edge[None, None, :] * t[..., None]

    # Soft cool glow behind the logo
    g = np.exp(-(((nx - 0.5) / 0.32) ** 2 + ((ny - 0.13) / 0.09) ** 2))
    img += g[..., None] * np.array([18, 34, 56])

    # Planet: dark body with an atmospheric rim, filling the lower part
    pcx, pcy, pr = 0.5 * w, h * 1.0 + 1.55 * w * 0.5, 1.55 * w * 0.5 + h * 0.27
    d = np.sqrt((xx - pcx) ** 2 + (yy - pcy) ** 2) - pr  # <0 inside planet
    inside = d < 0
    body = np.array([4, 8, 14], float)
    # Lit side towards the upper left
    lit = np.clip(1 - (xx - pcx + 0.6 * w) / (1.6 * w), 0, 1) ** 2
    body_col = body[None, None, :] + lit[..., None] * np.array([10, 20, 34])
    depth = np.clip(-d / (h * 0.06), 0, 1)[..., None]
    img = np.where(inside[..., None], img * (1 - depth) + body_col * depth, img)
    rim = np.exp(-(d / (h * 0.006)) ** 2)            # thin bright edge
    halo = np.exp(-np.clip(d, 0, None) / (h * 0.05)) * (d > 0)  # outer haze
    inner = np.exp(np.clip(d, None, 0) / (h * 0.03)) * inside   # inner scatter
    side = 0.35 + 0.65 * np.exp(-((xx - 0.5 * w) / (0.42 * w)) ** 2)
    atm = np.array([90, 170, 235], float)
    img += (rim * 1.0 + halo * 0.35 + inner * 0.25)[..., None] * side[..., None] * atm[None, None, :]

    # Stars (sky only), a few brighter ones with a soft bloom
    star = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(star)
    for _ in range(900):
        x, y = rng.uniform(0, w), rng.uniform(0, h * 0.9)
        b = rng.random() ** 3
        rad = 0.5 + b * 1.4
        sd.ellipse([x - rad, y - rad, x + rad, y + rad], fill=int(60 + 195 * b))
    bloom = star.filter(ImageFilter.GaussianBlur(3))
    s = (np.asarray(star, float) + np.asarray(bloom, float) * 1.5) / 255.0
    s *= (~inside) * np.clip(d / (h * 0.03), 0, 1)
    s *= 0.25 + 0.75 * np.clip(r / 0.6, 0, 1)  # fewer stars right behind the logo
    img += s[..., None] * np.array([190, 215, 255])

    # Faint hexagon grid, strongest at the lower left and right
    hexl = Image.new("L", (w, h), 0)
    hd = ImageDraw.Draw(hexl)
    size = 46
    hw = math.sqrt(3) * size
    for row in range(int(h / (size * 1.5)) + 2):
        for col in range(int(w / hw) + 2):
            x = col * hw + (hw / 2 if row % 2 else 0)
            y = row * size * 1.5
            pts = [(x + size * math.cos(math.radians(60 * k + 30)),
                    y + size * math.sin(math.radians(60 * k + 30))) for k in range(6)]
            hd.polygon(pts, outline=255)
    hx = np.asarray(hexl, float) / 255.0
    fade = np.clip(np.abs(nx - 0.5) * 2 - 0.25, 0, 1) ** 1.5 * np.clip(ny * 1.4 - 0.2, 0, 1)
    img += (hx * fade * 0.10)[..., None] * np.array([110, 170, 230])

    # Fine scanlines and vignette
    img *= (1 - 0.035 * (np.arange(h) % 3 == 0))[:, None, None]
    vig = np.clip(1 - (((nx - 0.5) * 1.1) ** 2 + ((ny - 0.45) * 1.0) ** 2) * 0.9, 0.35, 1)
    img *= vig[..., None]

    # Dither to stop gradient banding
    img += np.random.default_rng(7).uniform(-1.2, 1.2, img.shape)
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    out.save(OUT / "background.jpg", quality=93, subsampling=0, optimize=True)
    print("background", out.size)


def make_icon(emblem: Image.Image):
    side = max(emblem.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(emblem, ((side - emblem.width) // 2, (side - emblem.height) // 2))
    # Dark disc behind the emblem so it reads on light taskbars too
    base = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(base).ellipse([6, 6, 250, 250], fill=(12, 22, 36, 255))
    em = sq.resize((210, 210), Image.LANCZOS)
    base.alpha_composite(em, (23, 23))
    base.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    base.save(OUT / "icon.png")
    print("icon done")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    em = make_cutouts()
    make_background()
    make_icon(em)
