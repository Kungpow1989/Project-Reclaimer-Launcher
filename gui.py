"""
Project Reclaimer Launcher: the window.

Everything visible is rendered with Pillow (so the fonts, glows and
letter-spacing look the same on every PC) and shown on a single Tk canvas.
Update logic lives in launcher.py; this file only drives it from a button
that reads UPDATE (or INSTALL) and turns into PLAY once the client is current.
"""

from __future__ import annotations

import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageTk

import launcher as L

W, H = 960, 660          # logical window size (scaled for high-DPI screens)

TEXT = (224, 238, 252)
DIM = (126, 152, 180)
ACCENT = (122, 204, 255)
WARN = (255, 160, 128)
OK = (150, 230, 190)

BUTTON_STYLES = {
    # fill top, fill bottom, border, glow, text
    "update":   ((246, 184, 82), (176, 102, 22), (255, 226, 168), (255, 168, 60), (255, 255, 255)),
    "play":     ((104, 196, 255), (22, 102, 186), (206, 238, 255), (84, 182, 255), (255, 255, 255)),
    "retry":    ((92, 122, 156), (40, 60, 86), (176, 204, 230), (120, 170, 220), (255, 255, 255)),
    "disabled": ((26, 42, 62), (14, 24, 38), (72, 102, 134), (0, 0, 0), (120, 146, 172)),
}


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------

class Painter:
    """Draws text and widgets as RGBA images at the screen's scale."""

    def __init__(self, scale: float):
        self.s = scale
        self.fdir = L.resource_dir() / "assets" / "fonts"
        self._fonts = {}

    def p(self, v: float) -> int:
        return int(round(v * self.s))

    def font(self, name: str, size: float) -> ImageFont.FreeTypeFont:
        key = (name, size)
        if key not in self._fonts:
            try:
                self._fonts[key] = ImageFont.truetype(str(self.fdir / f"{name}.ttf"), self.p(size))
            except OSError:
                self._fonts[key] = ImageFont.load_default(self.p(size))
        return self._fonts[key]

    def measure(self, text: str, font, spacing: int) -> float:
        return sum(font.getlength(c) for c in text) + spacing * max(len(text) - 1, 0)

    def text(self, text: str, face: str, size: float, color, spacing: float = 0,
             shadow: bool = True, glow=None, glow_strength: float = 0.6) -> Image.Image:
        f = self.font(face, size)
        sp = self.p(spacing)
        asc, desc = f.getmetrics()
        pad = self.p(8)
        w = int(math.ceil(self.measure(text, f, sp))) + pad * 2
        h = asc + desc + pad * 2
        mask = Image.new("L", (max(w, 1), h), 0)
        d = ImageDraw.Draw(mask)
        x = float(pad)
        for c in text:
            d.text((x, pad), c, font=f, fill=255)
            x += f.getlength(c) + sp
        out = Image.new("RGBA", mask.size, (0, 0, 0, 0))
        if glow:
            g = mask.filter(ImageFilter.GaussianBlur(self.p(5)))
            g = g.point(lambda v: int(v * glow_strength))
            out.alpha_composite(Image.merge("RGBA", (*[Image.new("L", mask.size, c) for c in glow], g)))
        if shadow:
            sh = mask.filter(ImageFilter.GaussianBlur(max(1, self.p(1.5)))).point(lambda v: int(v * 0.75))
            layer = Image.new("RGBA", mask.size, (0, 0, 0, 0))
            layer.putalpha(sh)
            out.alpha_composite(layer, (0, max(1, self.p(1.5))))
        fill = Image.new("RGBA", mask.size, (*color[:3], 255))
        fill.putalpha(mask)
        out.alpha_composite(fill)
        return out

    def wrap(self, text: str, face: str, size: float, spacing: float, max_w: float) -> list:
        f = self.font(face, size)
        sp = self.p(spacing)
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if cur and self.measure(trial, f, sp) > self.p(max_w):
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def text_block(self, lines, face, size, color, spacing=0, line_gap=4) -> Image.Image:
        imgs = [self.text(l, face, size, color, spacing) for l in lines] or [self.text(" ", face, size, color)]
        step = imgs[0].height - self.p(16) + self.p(line_gap)
        w = max(i.width for i in imgs)
        h = step * (len(imgs) - 1) + imgs[-1].height
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for n, im in enumerate(imgs):
            out.alpha_composite(im, ((w - im.width) // 2, n * step))
        return out

    def chamfer_mask(self, w: int, h: int, cut: int, ss: int = 4, corners="tl,br") -> Image.Image:
        """Rectangle with cut corners, antialiased by supersampling."""
        W2, H2, c = w * ss, h * ss, cut * ss
        pts = []
        pts += [(c, 0)] if "tl" in corners else [(0, 0)]
        pts += [(W2 - c, 0), (W2, c)] if "tr" in corners else [(W2, 0)]
        pts += [(W2, H2 - c), (W2 - c, H2)] if "br" in corners else [(W2, H2)]
        pts += [(c, H2), (0, H2 - c)] if "bl" in corners else [(0, H2)]
        if "tl" in corners:
            pts += [(0, c)]
        m = Image.new("L", (W2, H2), 0)
        ImageDraw.Draw(m).polygon(pts, fill=255)
        return m.resize((w, h), Image.LANCZOS)

    def outline(self, mask: Image.Image, width: int) -> Image.Image:
        inner = mask.filter(ImageFilter.MinFilter(width * 2 + 1))
        return ImageChops.subtract(mask, inner)

    def gradient(self, w: int, h: int, top, bottom) -> Image.Image:
        col = Image.new("RGB", (1, h))
        for y in range(h):
            t = y / max(h - 1, 1)
            col.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
        return col.resize((w, h))

    def button(self, label: str, style: str, hover=False, pressed=False, pulse=0.0) -> Image.Image:
        top, bottom, border, glow, tcol = BUTTON_STYLES[style]
        bw, bh, pad, cut = self.p(300), self.p(64), self.p(30), self.p(16)
        out = Image.new("RGBA", (bw + pad * 2, bh + pad * 2), (0, 0, 0, 0))
        mask = self.chamfer_mask(bw, bh, cut)
        dy = self.p(2) if pressed else 0
        if style != "disabled":
            strength = 0.42 + 0.30 * pulse + (0.22 if hover else 0)
            gm = Image.new("L", out.size, 0)
            gm.paste(mask, (pad, pad + dy))
            gm = gm.filter(ImageFilter.GaussianBlur(self.p(13))).point(lambda v: int(min(255, v * strength * 1.6)))
            out.alpha_composite(Image.merge("RGBA", (*[Image.new("L", out.size, c) for c in glow], gm)))
        k = 1.12 if hover else 1.0
        k *= 0.85 if pressed else 1.0
        t2 = tuple(min(255, int(c * k)) for c in top)
        b2 = tuple(min(255, int(c * k)) for c in bottom)
        body = self.gradient(bw, bh, t2, b2).convert("RGBA")
        # Sheen on the upper half and faint diagonal stripes
        sheen = Image.new("L", (bw, bh), 0)
        sd = ImageDraw.Draw(sheen)
        sd.rectangle([0, 0, bw, bh * 0.48], fill=34 if style != "disabled" else 10)
        step = self.p(9)
        for x in range(-bh, bw + bh, step):
            sd.line([(x, bh), (x + bh, 0)], fill=14 if style != "disabled" else 6, width=max(1, self.p(2)))
        white = Image.new("RGBA", (bw, bh), (255, 255, 255, 0))
        white.putalpha(sheen)
        body.alpha_composite(white)
        if style == "disabled":
            body.putalpha(mask.point(lambda v: int(v * 0.86)))
        else:
            body.putalpha(mask)
        out.alpha_composite(body, (pad, pad + dy))
        edge = self.outline(mask, max(1, self.p(1.4)))
        ea = 255 if style != "disabled" else 150
        edge_img = Image.new("RGBA", (bw, bh), (*border, 0))
        edge_img.putalpha(edge.point(lambda v: int(v * ea / 255)))
        out.alpha_composite(edge_img, (pad, pad + dy))
        txt = self.text(label, "BarlowSemiCondensed-Bold", 27, tcol, spacing=8,
                        shadow=style != "disabled")
        out.alpha_composite(txt, ((out.width - txt.width) // 2 + self.p(4),
                                  (out.height - txt.height) // 2 + dy))
        return out

    def progress(self, frac: float, width=420) -> Image.Image:
        bw, bh, pad = self.p(width), max(2, self.p(4)), self.p(10)
        out = Image.new("RGBA", (bw + pad * 2, bh + pad * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(out)
        d.rectangle([pad, pad, pad + bw - 1, pad + bh - 1], fill=(255, 255, 255, 34))
        fw = int(bw * max(0.0, min(1.0, frac)))
        if fw > 0:
            g = Image.new("L", out.size, 0)
            ImageDraw.Draw(g).rectangle([pad, pad, pad + fw - 1, pad + bh - 1], fill=255)
            g = g.filter(ImageFilter.GaussianBlur(self.p(5)))
            out.alpha_composite(Image.merge("RGBA", (*[Image.new("L", out.size, c) for c in ACCENT], g)))
            bar = self.gradient(fw, bh, (90, 170, 240), (90, 170, 240)).convert("RGBA")
            # brighten towards the leading edge
            hl = Image.linear_gradient("L").rotate(90).resize((fw, bh)).point(lambda v: int(v * 0.6))
            whiteish = Image.new("RGBA", (fw, bh), (235, 248, 255, 0))
            whiteish.putalpha(hl.transpose(Image.FLIP_LEFT_RIGHT))
            bar.alpha_composite(whiteish)
            out.alpha_composite(bar, (pad, pad))
        return out

    def panel(self, w, h, title, lines, hover=False) -> Image.Image:
        pw, ph, cut = self.p(w), self.p(h), self.p(14)
        out = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        mask = self.chamfer_mask(pw, ph, cut, corners="tr,bl")
        fill = Image.new("RGBA", (pw, ph), (6, 13, 24, 0))
        fill.putalpha(mask.point(lambda v: int(v * (0.72 if not hover else 0.8))))
        out.alpha_composite(fill)
        edge = self.outline(mask, max(1, self.p(1)))
        e = Image.new("RGBA", (pw, ph), (*ACCENT, 0))
        e.putalpha(edge.point(lambda v: int(v * (0.30 if not hover else 0.6))))
        out.alpha_composite(e)
        d = ImageDraw.Draw(out)
        # accent tab on the top edge
        d.rectangle([self.p(18), 0, self.p(90), max(1, self.p(2.5))], fill=(*ACCENT, 230))
        head = self.text(title, "BarlowSemiCondensed-SemiBold", 13.5, ACCENT, spacing=4, shadow=False)
        out.alpha_composite(head, (self.p(10), self.p(6)))
        y = self.p(40)
        f = self.font("Barlow-Medium", 15.5)
        maxw = pw - self.p(64)
        for line in lines:
            t = line
            while f.getlength(t) > maxw and len(t) > 4:
                t = t[:-2].rstrip() + "…"
                t = t[:-1]
            if t != line:
                t = t.rstrip(" ,.;:") + "…"
            # diamond marker
            cx, cy = self.p(28), y + self.p(11)
            r = self.p(3.5)
            d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=(*ACCENT, 220))
            ti = self.text(t, "Barlow-Medium", 15.5, TEXT, spacing=0.3)
            out.alpha_composite(ti, (self.p(38) - self.p(8), y - self.p(8)))
            y += self.p(24)
        return out

    def window_button(self, kind: str, hover: bool) -> Image.Image:
        w, h = self.p(46), self.p(32)
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(out)
        if hover:
            d.rectangle([0, 0, w, h], fill=(196, 54, 44, 230) if kind == "close" else (255, 255, 255, 30))
        col = (240, 246, 252, 255) if hover else (170, 192, 214, 255)
        lw = max(1, self.p(1.3))
        cx, cy, r = w // 2, h // 2, self.p(5)
        if kind == "close":
            d.line([(cx - r, cy - r), (cx + r, cy + r)], fill=col, width=lw)
            d.line([(cx - r, cy + r), (cx + r, cy - r)], fill=col, width=lw)
        else:
            d.line([(cx - r, cy), (cx + r, cy)], fill=col, width=lw)
        return out


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

class LauncherWindow:
    def __init__(self, root: tk.Tk, base: Path, cfg: dict, opts: dict, log):
        self.root, self.base, self.cfg, self.opts, self.log = root, base, cfg, opts, log
        self.q: queue.Queue = queue.Queue()
        self.info: L.UpdateInfo | None = None
        self.busy = False
        self.state = ""
        self.hover = None
        self.pressed = None
        self.drag = None
        self.spin_angle = 0
        self.pulse_t = 0.0
        self.closing_at = None
        self.dl_started = 0.0

        scale = root.winfo_fpixels("1i") / 96.0
        self.s = max(1.0, min(3.0, scale))
        self.P = Painter(self.s)
        p = self.P.p
        self.cw, self.ch = p(W), p(H)

        b = str(cfg.get("borderless", "auto")).strip().lower()
        # Borderless windows behave well on Windows; on macOS and Linux the
        # window manager handles focus and the dock/taskbar better with a
        # normal title bar.
        self.borderless = (os.name == "nt") if b == "auto" else L.as_bool(b)
        root.title("Project Reclaimer Launcher")
        root.configure(bg="#08101b")
        root.resizable(False, False)
        sx = (root.winfo_screenwidth() - self.cw) // 2
        sy = (root.winfo_screenheight() - self.ch) // 2
        root.geometry(f"{self.cw}x{self.ch}+{sx}+{max(0, sy)}")
        self._set_icon()

        self.c = tk.Canvas(root, width=self.cw, height=self.ch, highlightthickness=0, bd=0, bg="#08101b")
        self.c.pack()
        self.photos = {}   # key -> PhotoImage (kept alive)
        self.items = {}    # key -> canvas item id
        self.cache = {}    # rendered PhotoImages by key
        self.regions = {}  # name -> (x0, y0, x1, y1) clickable areas

        self._draw_static()
        self._bind()
        if self.borderless:
            self._make_borderless()

        self.set_state("checking")
        if opts.get("update", True) and L.as_bool(cfg["check_for_updates"]):
            self.start_check()
        else:
            self._after_check(None, None)
        self.root.after(30, self.tick)

    # ---- setup -----------------------------------------------------------

    def _set_icon(self):
        try:
            ico = L.resource_dir() / "assets" / "icon.png"
            self._icon = ImageTk.PhotoImage(Image.open(ico))
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    def _make_borderless(self):
        self.root.overrideredirect(True)
        if os.name == "nt":
            self.root.after(10, self._fix_taskbar)

    def _fix_taskbar(self):
        """Borderless Tk windows drop off the taskbar on Windows; put them back."""
        try:
            import ctypes
            GWL_EXSTYLE, WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = -20, 0x00040000, 0x00000080
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            self.root.withdraw()
            self.root.after(10, self.root.deiconify)
        except Exception as e:
            self.log.warning("Taskbar fix failed: %s", e)

    def _bind(self):
        self.c.bind("<Motion>", self.on_motion)
        self.c.bind("<Leave>", lambda e: self.set_hover(None))
        self.c.bind("<ButtonPress-1>", self.on_press)
        self.c.bind("<B1-Motion>", self.on_drag)
        self.c.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Return>", lambda e: self.activate("button"))
        self.root.bind("<space>", lambda e: self.activate("button"))
        self.root.bind("<Escape>", lambda e: self.close())
        self.root.bind("<Map>", self.on_map)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def show(self, key, img: Image.Image | None, x=0, y=0, anchor="center", cache_key=None):
        """Create or update a canvas image item. img=None (and nothing cached
        under cache_key) hides it."""
        cached = cache_key is not None and cache_key in self.cache
        if img is None and not cached:
            if key in self.items:
                self.c.itemconfigure(self.items[key], state="hidden")
            return
        if cached:
            photo = self.cache[cache_key]
        else:
            photo = ImageTk.PhotoImage(img)
            if cache_key is not None:
                self.cache[cache_key] = photo
        self.photos[key] = photo
        if key in self.items:
            self.c.itemconfigure(self.items[key], image=photo, state="normal")
            self.c.coords(self.items[key], x, y)
            self.c.itemconfigure(self.items[key], anchor=anchor)
        else:
            self.items[key] = self.c.create_image(x, y, image=photo, anchor=anchor)

    def _draw_static(self):
        P, p = self.P, self.P.p
        assets = L.resource_dir() / "assets"
        bg = Image.open(assets / "background.jpg").convert("RGBA")
        # cover-fit
        r = max(self.cw / bg.width, self.ch / bg.height)
        bg = bg.resize((math.ceil(bg.width * r), math.ceil(bg.height * r)), Image.LANCZOS)
        left, top = (bg.width - self.cw) // 2, (bg.height - self.ch) // 2
        bg = bg.crop((left, top, left + self.cw, top + self.ch))
        d = ImageDraw.Draw(bg)
        if self.borderless:
            d.rectangle([0, 0, self.cw - 1, self.ch - 1], outline=(52, 84, 118, 255), width=1)
            d.line([(p(24), 0), (p(140), 0)], fill=(*ACCENT, 200), width=max(1, p(2)))
        self.show("bg", bg, 0, 0, "nw")

        logo = Image.open(assets / "logo.png").convert("RGBA")
        lw = p(620)
        logo = logo.resize((lw, round(logo.height * lw / logo.width)), Image.LANCZOS)
        self.show("logo", logo, self.cw // 2, p(42), "n")

        em = Image.open(assets / "emblem.png").convert("RGBA")
        side = max(em.size)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(em, ((side - em.width) // 2, (side - em.height) // 2))
        big = sq.resize((p(56) * 3, p(56) * 3), Image.BICUBIC)
        self.spinner = []
        for i in range(30):
            fr = big.rotate(-i * 12, resample=Image.BICUBIC).resize((p(56), p(56)), Image.LANCZOS)
            self.spinner.append(ImageTk.PhotoImage(fr))
        dimmed = big.resize((p(56), p(56)), Image.LANCZOS)
        dimmed.putalpha(dimmed.getchannel("A").point(lambda v: int(v * 0.55)))
        self.spinner_idle = ImageTk.PhotoImage(dimmed)
        self.items["spinner"] = self.c.create_image(self.cw // 2, p(184), image=self.spinner_idle)

        # window controls
        if self.borderless:
            bw, bh = p(46), p(32)
            self.regions["close"] = (self.cw - bw, 0, self.cw, bh)
            self.regions["minimize"] = (self.cw - 2 * bw, 0, self.cw - bw, bh)
            self._draw_window_buttons()

        # footer links
        x = p(26)
        y = self.ch - p(26)
        for name, label in (("notes", "PATCH NOTES"), ("recheck", "CHECK AGAIN"), ("folder", "GAME FOLDER")):
            img = P.text(label, "Barlow-SemiBold", 11.5, DIM, spacing=3, shadow=False)
            self.show(f"link_{name}", img, x - p(8), y, "w")
            self.regions[name] = (x - p(4), y - p(12), x - p(16) + img.width, y + p(12))
            x += img.width - p(16) + p(26)
        ver = P.text(f"LAUNCHER V{L.LAUNCHER_VERSION}", "Barlow-SemiBold", 11.5, (84, 108, 134), spacing=3, shadow=False)
        self.show("version", ver, self.cw - p(18), y, "e")

    def _draw_window_buttons(self):
        for kind in ("close", "minimize"):
            x0, y0, _, _ = self.regions[kind]
            hov = self.hover == kind
            self.show(f"win_{kind}", self.P.window_button(kind, hov), x0, y0, "nw",
                      cache_key=("win", kind, hov))

    # ---- state -----------------------------------------------------------

    def set_state(self, state, status=None, sub=None, sub_color=DIM, button=None, style=None):
        self.state = state
        defaults = {
            "checking": ("CHECKING FOR UPDATES", "", "CHECKING", "disabled"),
            "update": ("UPDATE AVAILABLE", "", "UPDATE", "update"),
            "updating": ("DOWNLOADING", "", "UPDATING", "disabled"),
            "ready": ("READY", "", "PLAY", "play"),
            "launching": ("LAUNCHING", "SEE YOU ON THE BATTLEFIELD", "STARTING", "disabled"),
            "error": ("SOMETHING WENT WRONG", "", "RETRY", "retry"),
        }[state]
        self.status_text = status if status is not None else defaults[0]
        self.sub_text = sub if sub is not None else defaults[1]
        self.sub_color = sub_color
        self.button_label = button or defaults[2]
        self.button_style = style or defaults[3]
        self.busy = state in ("checking", "updating", "launching")
        self._draw_status()
        self._draw_button()
        if state != "updating":
            self.show("progress", None)

    def _draw_status(self):
        P, p = self.P, self.P.p
        st = P.text(self.status_text.upper(), "BarlowSemiCondensed-SemiBold", 20, TEXT, spacing=7,
                    glow=ACCENT, glow_strength=0.35)
        self.show("status", st, self.cw // 2 + p(3), p(236))
        if self.sub_text:
            # Short status lines are spaced capitals; warnings stay readable
            # in normal case (they can contain settings to type).
            warn = self.sub_color == WARN
            txt = self.sub_text if warn else self.sub_text.upper()
            sp = 0.6 if warn else 2.2
            lines = P.wrap(txt, "Barlow-Medium", 13.5 if warn else 13, sp, 600)[:2]
            if len(P.wrap(txt, "Barlow-Medium", 13.5 if warn else 13, sp, 600)) > 2:
                lines[-1] = lines[-1].rstrip(" .,") + "…"
            sub = P.text_block(lines, "Barlow-Medium", 13.5 if warn else 13, self.sub_color,
                               spacing=sp, line_gap=1)
            self.show("sub", sub, self.cw // 2, p(262), "n")
        else:
            self.show("sub", None)

    def _draw_button(self):
        p = self.P.p
        pulse = 0.5 + 0.5 * math.sin(self.pulse_t) if self.button_style in ("play", "update") else 0.0
        q = round(pulse * 12) / 12
        hov = self.hover == "button" and self.button_style != "disabled"
        prs = self.pressed == "button" and hov
        key = ("btn", self.button_label, self.button_style, hov, prs, q)
        img = None if key in self.cache else self.P.button(self.button_label, self.button_style, hov, prs, q)
        cx, cy = self.cw // 2, p(352)
        self.show("button", img, cx, cy, cache_key=key)
        self.regions["button"] = (cx - p(150), cy - p(32), cx + p(150), cy + p(32))

    def _draw_notes(self):
        p = self.P.p
        if not self.info or not self.info.notes:
            self.show("notes", None)
            self.regions.pop("panel", None)
            return
        hov = self.hover == "panel"
        key = ("panel", self.info.latest_tag, hov)
        img = None
        if key not in self.cache:
            img = self.P.panel(540, 44 + 24 * min(5, len(self.info.notes)) + 8,
                               f"WHAT'S NEW IN {self.info.latest_tag.upper()}",
                               self.info.notes[:5], hover=hov)
        top = p(424)
        self.show("notes", img, self.cw // 2, top, "n", cache_key=key)
        ph = p(44 + 24 * min(5, len(self.info.notes)) + 8)
        self.regions["panel"] = (self.cw // 2 - p(270), top, self.cw // 2 + p(270), top + ph)

    def _draw_link(self, name):
        if f"link_{name}" not in self.items:
            return
        labels = {"notes": "PATCH NOTES", "recheck": "CHECK AGAIN", "folder": "GAME FOLDER"}
        hov = self.hover == name
        img = self.P.text(labels[name], "Barlow-SemiBold", 11.5, ACCENT if hov else DIM, spacing=3, shadow=False)
        x0, y0, x1, y1 = self.regions[name]
        self.show(f"link_{name}", img, x0 - self.P.p(4), (y0 + y1) // 2, "w", cache_key=("link", name, hov))

    # ---- workers ---------------------------------------------------------

    def start_check(self):
        self.set_state("checking")

        def work():
            try:
                info = L.check_for_update(self.base, self.cfg, self.log,
                                          lambda m: self.q.put(("status", m)))
                self.q.put(("checked", info))
            except Exception as e:
                self.log.exception("Update check failed")
                self.q.put(("check_failed", e))

        threading.Thread(target=work, daemon=True).start()

    def _after_check(self, info, err):
        installed = (self.base / self.cfg["game_exe"]).exists()
        self.info = info
        self._draw_notes()
        if err is not None:
            msg = str(err).splitlines()[0] if str(err) else type(err).__name__
            if not isinstance(err, L.UpdateError):
                code = getattr(err, "code", None)
                if code in (403, 429):
                    msg = "GitHub is rate limiting update checks right now. Try again in a few minutes."
                elif isinstance(err, OSError):
                    msg = "No connection to GitHub. Check your internet connection and try again."
            if installed:
                self.set_state("ready", sub=f"Couldn't check for updates. {self._short(msg)}", sub_color=WARN)
            else:
                net = not isinstance(err, L.UpdateError)
                self.set_state("error", status="COULDN'T REACH GITHUB" if net else "CAN'T INSTALL",
                               sub=self._short(msg), sub_color=WARN)
            return
        if info is None:  # updates switched off
            if installed:
                self.set_state("ready", sub="Update check is turned off")
            else:
                self.set_state("error", status="NOT INSTALLED",
                               sub="Turn on check_for_updates in launcher.ini to download the game",
                               sub_color=WARN)
            return
        if info.needs_update:
            if not info.installed:
                self.set_state("update", status="READY TO INSTALL", button="INSTALL",
                               sub=f"Project Reclaimer {info.latest_tag}")
            else:
                frm = info.installed_tag or "Installed version"
                self.set_state("update", sub=f"{frm}  ›  {info.latest_tag}")
            if L.as_bool(self.cfg.get("auto_update", "false")):
                self.start_install()
        else:
            self.set_state("ready", sub=f"{info.installed_tag or info.latest_tag}  ·  up to date", sub_color=OK)

    def start_install(self):
        if not self.info:
            return
        info = self.info
        self.set_state("updating", status=f"DOWNLOADING {info.latest_tag}", sub="Starting download")
        self.dl_started = time.monotonic()
        self.show("progress", self.P.progress(0), self.cw // 2, self.P.p(300))

        def work():
            try:
                msg = L.install_update(self.base, self.cfg, info, self.log,
                                       lambda m: self.q.put(("status", m)),
                                       lambda d, t: self.q.put(("progress", (d, t))))
                self.q.put(("installed", msg))
            except Exception as e:
                self.log.exception("Install failed")
                self.q.put(("install_failed", e))

        threading.Thread(target=work, daemon=True).start()

    def play(self):
        try:
            L.launch_game(self.base, self.cfg, self.opts.get("extra", []), self.log)
        except Exception as e:
            self.log.exception("Launch failed")
            self.set_state("ready", sub=f"Couldn't start the game: {self._short(str(e))}", sub_color=WARN)
            return
        self.set_state("launching")
        if L.as_bool(self.cfg["close_after_launch"]):
            self.closing_at = time.monotonic() + 2.2
        else:
            self.root.after(3000, lambda: self.set_state("ready", sub="Game started"))

    @staticmethod
    def _short(msg: str, n: int = 240) -> str:
        msg = " ".join(msg.split())
        return msg if len(msg) <= n else msg[: n - 1].rstrip() + "…"

    # ---- events ----------------------------------------------------------

    def hit(self, x, y):
        for name, (x0, y0, x1, y1) in self.regions.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def set_hover(self, name):
        if name == self.hover:
            return
        old, self.hover = self.hover, name
        for n in (old, name):
            if n == "button":
                self._draw_button()
            elif n in ("close", "minimize"):
                self._draw_window_buttons()
            elif n == "panel":
                self._draw_notes()
            elif n in ("notes", "recheck", "folder"):
                self._draw_link(n)
        clickable = name and not (name == "button" and self.button_style == "disabled")
        self.c.configure(cursor="hand2" if clickable else "")

    def on_motion(self, e):
        self.set_hover(self.hit(e.x, e.y))

    def on_press(self, e):
        name = self.hit(e.x, e.y)
        self.pressed = name
        if name == "button":
            self._draw_button()
        if name is None and self.borderless:
            self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def on_drag(self, e):
        if self.drag:
            self.root.geometry(f"+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}")

    def on_release(self, e):
        name = self.hit(e.x, e.y)
        pressed, self.pressed, self.drag = self.pressed, None, None
        if pressed == "button":
            self._draw_button()
        if name and name == pressed:
            self.activate(name)

    def activate(self, name):
        if name == "button":
            if self.button_style == "disabled":
                return
            if self.state == "update":
                self.start_install()
            elif self.state == "ready":
                self.play()
            elif self.state == "error":
                self.start_check()
        elif name == "close":
            self.close()
        elif name == "minimize":
            self.minimize()
        elif name in ("notes", "panel"):
            url = (self.info.html_url if self.info else
                   f"{self.cfg['web_base'].rstrip('/')}/{self.cfg['repo']}/releases/latest")
            webbrowser.open(url)
        elif name == "recheck":
            if not self.busy:
                self.start_check()
        elif name == "folder":
            self.open_folder()

    def open_folder(self):
        try:
            if os.name == "nt":
                os.startfile(self.base)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.base)])
            else:
                subprocess.Popen(["xdg-open", str(self.base)])
        except Exception as e:
            self.log.warning("Couldn't open folder: %s", e)

    def minimize(self):
        if self.borderless:
            self.root.overrideredirect(False)
        self.root.iconify()

    def on_map(self, e):
        if self.borderless and self.root.state() == "normal" and not self.root.overrideredirect():
            self.root.overrideredirect(True)
            if os.name == "nt":
                self.root.after(10, self._fix_taskbar)

    def close(self):
        self.root.destroy()

    # ---- main loop -------------------------------------------------------

    def tick(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                self.handle(kind, val)
        except queue.Empty:
            pass
        # spinner
        if self.busy:
            self.spin_angle = (self.spin_angle + 1) % len(self.spinner)
            self.c.itemconfigure(self.items["spinner"], image=self.spinner[self.spin_angle])
        else:
            self.c.itemconfigure(self.items["spinner"], image=self.spinner_idle)
        # button glow pulse
        if self.button_style in ("play", "update"):
            before = round((0.5 + 0.5 * math.sin(self.pulse_t)) * 12)
            self.pulse_t += 0.09
            if round((0.5 + 0.5 * math.sin(self.pulse_t)) * 12) != before:
                self._draw_button()
        if self.closing_at and time.monotonic() >= self.closing_at:
            self.close()
            return
        self.root.after(33, self.tick)

    def handle(self, kind, val):
        p = self.P.p
        if kind == "status":
            low = val.lower()
            if self.state == "checking":
                self.set_state("checking", status=val.rstrip(".").upper())
            elif self.state == "updating" and ("verifying" in low or "installing" in low):
                self.set_state("updating", status=val.rstrip(".").upper(), sub="")
                self.show("progress", self.P.progress(1.0), self.cw // 2, p(300))
        elif kind == "checked":
            self._after_check(val, None)
        elif kind == "check_failed":
            self._after_check(None, val)
        elif kind == "progress":
            done, total = val
            if self.state != "updating":
                return
            frac = done / total if total else 0
            el = max(time.monotonic() - self.dl_started, 0.001)
            rate = done / el / 1048576
            sub = f"{done / 1048576:.1f} / {total / 1048576:.1f} MB  ·  {rate:.1f} MB/s" if total \
                else f"{done / 1048576:.1f} MB"
            self.sub_text, self.sub_color = sub, DIM
            self._draw_status()
            self.show("progress", self.P.progress(frac), self.cw // 2, p(300))
        elif kind == "installed":
            tag = self.info.latest_tag if self.info else ""
            if self.info:
                self.info.needs_update = False
                self.info.installed = True
                self.info.installed_tag = tag
            self.set_state("ready", sub=f"Updated to {tag}", sub_color=OK)
        elif kind == "install_failed":
            installed = (self.base / self.cfg["game_exe"]).exists()
            if installed:
                self.set_state("update", status="UPDATE FAILED", button="TRY AGAIN",
                               sub=self._short(str(val)), sub_color=WARN)
            else:
                self.set_state("update", status="INSTALL FAILED", button="TRY AGAIN",
                               sub=self._short(str(val)), sub_color=WARN)


def _dpi_aware():
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ProjectReclaimer.Launcher")
    except Exception:
        pass


def run_gui(base: Path, cfg: dict, opts: dict, log) -> int:
    _dpi_aware()
    root = tk.Tk()
    LauncherWindow(root, base, cfg, opts, log)
    root.mainloop()
    return 0
