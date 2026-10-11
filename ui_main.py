"""Элементы главного окна, которые рисуются Pillow (со сглаживанием): большая карточка с градиентом,
круглая кнопка с иконкой и «дышащим» кольцом, живой индикатор голоса.

Обычные виджеты (кнопки, переключатели, списки) — CustomTkinter, а здесь то, чего он не умеет:
градиенты, мягкие тени, сглаженные круги и анимация."""
import math
import os
import time
import tkinter as tk

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

FONTS_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
SS = 3                                   # рисуем крупнее и уменьшаем — так края гладкие


def font(names: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    for name in names:
        try:
            return ImageFont.truetype(os.path.join(FONTS_DIR, name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def icon_font(size: int) -> ImageFont.FreeTypeFont:
    """Значки Windows 11 (Segoe Fluent Icons), на Windows 10 — Segoe MDL2 Assets."""
    return font(("SegoeIcons.ttf", "segmdl2.ttf"), size)


def icon_family() -> str:
    return "Segoe Fluent Icons" if os.path.exists(os.path.join(FONTS_DIR, "SegoeIcons.ttf")) else "Segoe MDL2 Assets"


ICONS = {"home": "\uE80F", "lecture": "\uE7BE", "history": "\uE81C", "settings": "\uE713",
         "mic": "\uE720", "play": "\uE768", "copy": "\uE8C8", "delete": "\uE74D", "search": "\uE721",
         "folder": "\uE838", "refresh": "\uE72C"}


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def gradient_card(w: int, h: int, c1: str, c2: str, radius: int, glow: bool = True) -> Image.Image:
    """Карточка с диагональным градиентом и мягкими «бликами» света, края скруглены."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    t = np.clip((xx / max(w, 1)) * 0.75 + (yy / max(h, 1)) * 0.25, 0, 1)[..., None]
    rgb = np.array(_hex(c1), np.float32) * (1 - t) + np.array(_hex(c2), np.float32) * t
    if glow:                              # два световых пятна — объём, как у карточек Apple
        for cx, cy, r, k in ((0.82, -0.1, 0.55, 0.22), (0.05, 1.15, 0.5, 0.12)):
            d = np.sqrt(((xx / w - cx) * w / h) ** 2 + (yy / h - cy) ** 2) / r
            rgb += (255 - rgb) * (np.clip(1 - d, 0, 1) ** 2)[..., None] * k
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    mask = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), radius * SS, fill=255)
    img.putalpha(mask.resize((w, h), Image.LANCZOS))
    # тонкая светлая кромка сверху — «стекло»
    edge = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), radius * SS,
                                           outline=(255, 255, 255, 46), width=SS)
    img.alpha_composite(edge.resize((w, h), Image.LANCZOS))
    return img


def round_button(d: int, fill: str, glyph: str, glyph_color: str, shadow: bool = True,
                 square: bool = False) -> Image.Image:
    """Круглая кнопка диаметром d (с тенью вокруг): значок — или белый квадрат «стоп»."""
    pad = d // 4
    size = d + 2 * pad
    big = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    if shadow:
        sh = Image.new("RGBA", big.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).ellipse((pad * SS, (pad + d // 14) * SS, (pad + d) * SS, (pad + d + d // 14) * SS),
                                   fill=(0, 0, 0, 110))
        big.alpha_composite(sh.filter(ImageFilter.GaussianBlur(d // 9 * SS)))
    dr = ImageDraw.Draw(big)
    dr.ellipse((pad * SS, pad * SS, (pad + d) * SS, (pad + d) * SS), fill=(*_hex(fill), 255))
    cx = cy = (pad + d / 2) * SS
    if square:
        q = d * 0.17 * SS
        dr.rounded_rectangle((cx - q, cy - q, cx + q, cy + q), radius=q * 0.35, fill=(*_hex(glyph_color), 255))
    else:
        f = icon_font(int(d * 0.36 * SS))
        dr.text((cx, cy), glyph, font=f, fill=(*_hex(glyph_color), 255), anchor="mm")
    return big.resize((size, size), Image.LANCZOS)


def ring_frames(d: int, color: str, n: int = 24) -> list[Image.Image]:
    """Кадры «дышащего» кольца вокруг кнопки во время записи."""
    pad = d // 4
    size = d + 2 * pad
    frames = []
    for i in range(n):
        k = i / n
        r = d / 2 + pad * 0.9 * k
        a = int(150 * (1 - k) ** 1.6)
        big = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
        c = size / 2 * SS
        ImageDraw.Draw(big).ellipse((c - r * SS, c - r * SS, c + r * SS, c + r * SS),
                                    outline=(*_hex(color), a), width=max(1, int(d * 0.035 * SS)))
        frames.append(big.resize((size, size), Image.LANCZOS))
    return frames


def bars_image(level: float, w: int, h: int, color=(255, 255, 255), n: int = 24, t: float = 0.0) -> Image.Image:
    """Индикатор голоса: ряд столбиков, высота — громкость, по краям ниже (как волна)."""
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    gap = w * SS / n
    bw = gap * 0.45
    for i in range(n):
        env = math.sin(math.pi * (i + 0.5) / n) ** 0.8                     # выше в середине
        wob = 0.65 + 0.35 * math.sin(t * 9 + i * 0.9) * math.sin(t * 5.3 + i * 0.37)
        v = max(0.07, min(1.0, level * env * wob * 1.15))
        bh = h * SS * v
        x = i * gap + (gap - bw) / 2
        d.rounded_rectangle((x, (h * SS - bh) / 2, x + bw, (h * SS + bh) / 2), radius=bw / 2,
                            fill=(*color, int(120 + 135 * v)))
    return big.resize((w, h), Image.LANCZOS)


class Hero(tk.Canvas):
    """Большая карточка: градиент, крупный заголовок, круглая кнопка справа, клавиша-подсказка.
    set_active(True) — кнопка становится «стоп», вокруг неё дышит кольцо, под текстом пляшет голос."""

    def __init__(self, parent, scale: float, bg: str, colors: tuple[str, str], glyph: str, accent: str,
                 eyebrow: str, title: str, subtitle: str, hotkey: str, command, level_source=None,
                 height: int = 200, ui_font: str = "Segoe UI Variable Display", bars: bool = True):
        super().__init__(parent, height=int(height * scale), bg=bg, highlightthickness=0, bd=0)
        self.s, self.colors, self.glyph, self.accent = scale, colors, glyph, accent
        self.command = command
        self.level_source = level_source or (lambda: 0.0)
        self.texts = {"eyebrow": eyebrow, "title": title, "subtitle": subtitle, "hotkey": hotkey, "status": ""}
        self.ui_font = ui_font
        self.bars = bars
        self.active = False
        self.hover = False
        self.level = 0.0
        self.floor_db = -60.0
        self._imgs: dict = {}
        self._job = None
        self._frame = 0
        self.w = 0
        D = int(84 * scale)
        self.D = D
        self.btn = {
            "idle": round_button(D, "#ffffff", glyph, accent),
            "hover": round_button(int(D * 1.05), "#ffffff", glyph, accent),
            "active": round_button(D, "#ff453a", "", "#ffffff", square=True),
            "active_hover": round_button(int(D * 1.05), "#ff453a", "", "#ffffff", square=True),
        }
        self.btn = {k: ImageTk.PhotoImage(v) for k, v in self.btn.items()}
        self.rings = [ImageTk.PhotoImage(f) for f in ring_frames(int(D * 1.0), "#ffffff")]
        self.bind("<Configure>", self._resize)
        self.tag_bind("btn", "<Button-1>", lambda e: self.command())
        self.tag_bind("btn", "<Enter>", lambda e: self._hover(True))
        self.tag_bind("btn", "<Leave>", lambda e: self._hover(False))

    # — размеры и фон —
    def _resize(self, e):
        if e.width == self.w or e.width < 50:
            return
        self.w, self.h = e.width, e.height
        self._bg = ImageTk.PhotoImage(gradient_card(self.w, self.h, *self.colors, radius=int(22 * self.s)))
        self._draw()

    def _draw(self):
        if not self.w:
            return
        s, w, h = self.s, self.w, self.h
        self.delete("all")
        self.create_image(0, 0, image=self._bg, anchor="nw")
        x = int(30 * s)
        f = self.ui_font
        self.create_text(x, int(30 * s), text=self.texts["eyebrow"].upper(), anchor="nw", fill="#dfe6ff",
                         font=(f + " Semib", 9))
        self.create_text(x, int(52 * s), text=self.texts["title"], anchor="nw", fill="#ffffff",
                         font=(f + " Semib", 22))
        sub = self.texts["status"] or self.texts["subtitle"]
        self.create_text(x, int(98 * s), text=sub, anchor="nw", fill="#e4e9ff", font=(f, 11),
                         width=max(100, w - x - self.D - int(110 * s)), tags="sub")
        # клавиша-подсказка
        hk = self.texts["hotkey"]
        self.bars_x = x
        if hk:
            self.create_text(x, h - int(30 * s), text=hk, anchor="w", fill="#ffffff", font=(f + " Semib", 10),
                             tags="hk")
            bx0, by0, bx1, by1 = self.bbox("hk")
            pad = int(9 * s)
            chip = Image.new("RGBA", ((bx1 - bx0 + 2 * pad) * SS, (by1 - by0 + int(8 * s)) * SS), (0, 0, 0, 0))
            ImageDraw.Draw(chip).rounded_rectangle((0, 0, chip.width - 1, chip.height - 1), chip.height // 2,
                                                   fill=(255, 255, 255, 40), outline=(255, 255, 255, 70),
                                                   width=SS)
            self._imgs["chip"] = ImageTk.PhotoImage(chip.resize((chip.width // SS, chip.height // SS),
                                                                Image.LANCZOS))
            self.create_image(bx0 - pad, (by0 + by1) // 2, image=self._imgs["chip"], anchor="w")
            self.tag_raise("hk")
            self.bars_x = bx1 + pad + int(18 * s)
        self.bx, self.by = w - int(36 * s) - self.D // 2, h // 2
        self.create_image(self.bx, self.by, image=self.rings[0], tags="ring", state="hidden")
        self.create_image(self.bx, self.by, image=self._btn_img(), tags="btn")
        self.create_image(self.bars_x, h - int(30 * s), anchor="w", tags="bars")
        self._update_bars()

    def _btn_img(self):
        key = ("active" if self.active else "idle") + ("_hover" if self.hover and self.active else "")
        if self.hover and not self.active:
            key = "hover"
        return self.btn[key]

    def _hover(self, on: bool):
        self.hover = on
        self.configure(cursor="hand2" if on else "")
        self.itemconfigure("btn", image=self._btn_img())

    # — состояние —
    def set_text(self, **kw):
        changed = any(self.texts.get(k) != v for k, v in kw.items())
        self.texts.update(kw)
        if changed:
            if set(kw) == {"status"} and self.find_withtag("sub"):
                self.itemconfigure("sub", text=self.texts["status"] or self.texts["subtitle"])
            else:
                self._draw()

    def set_active(self, on: bool):
        if on == self.active:
            return
        self.active = on
        self.level = 0.0
        self.itemconfigure("btn", image=self._btn_img())
        if on:
            self.itemconfigure("ring", state="normal")
            self._started = time.time()
            if not self._job:
                self._animate()
        else:
            self.itemconfigure("ring", state="hidden")
            self._update_bars()

    def _animate(self):
        self._job = None
        if not self.active:
            return
        try:
            self._frame = (self._frame + 1) % len(self.rings)
            self.itemconfigure("ring", image=self.rings[self._frame])
            raw = max(float(self.level_source() or 0.0), 1e-6)
            db = 20 * math.log10(raw)
            self.floor_db = db if db < self.floor_db else self.floor_db + 0.07
            v = max(0.0, min(1.0, (db - self.floor_db - 4) / 24))
            self.level += (v - self.level) * (0.5 if v > self.level else 0.15)
            self._update_bars()
        except tk.TclError:
            return
        except Exception as e:                                  # noqa: BLE001 — анимация не должна ронять окно
            print("hero:", e, flush=True)
        self._job = self.after(40, self._animate)

    def _update_bars(self):
        if not self.w or not self.bars:
            return
        s = self.s
        bw, bh = int(min(260 * s, self.bx - self.D // 2 - 40 * s - self.bars_x)), int(26 * s)
        if bw < 40:
            return
        if self.active:
            img = bars_image(self.level, bw, bh, t=time.perf_counter())
        else:
            img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        self._imgs["bars"] = ImageTk.PhotoImage(img)
        self.itemconfigure("bars", image=self._imgs["bars"])
