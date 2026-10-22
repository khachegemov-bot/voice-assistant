"""Всплывающие окна в «дорогом» стиле: глубокий чёрный блок с большими скруглениями и мягкой тенью,
крупный белый текст, синий значок-«таблетка» сверху, пульсирующая точка-курсор.

Окно рисуется как картинка (Pillow, со сглаживанием) и показывается через «многослойные» окна
Windows (UpdateLayeredWindow) — с настоящей прозрачностью по каждому пикселю. Окна не забирают фокус:
текст продолжает печататься туда, где стоит курсор.
"""
import ctypes
import math
import os
import re
import time
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32

# ─────────────────────────── многослойное окно Windows ───────────────────────────

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
# Точные типы: на 64-битной Windows дескрипторы не помещаются в int по умолчанию
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.HDC,
                                       ctypes.c_void_p, wintypes.COLORREF, ctypes.c_void_p, wintypes.DWORD]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.SetCursor.argtypes = [wintypes.HANDLE]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_CLASS = "VoiceAssistantOverlay"
_handlers: dict[int, "LayeredWindow"] = {}


def _wndproc(hwnd, msg, wparam, lparam):
    win = _handlers.get(hwnd)
    try:
        if win is not None:
            if msg == 0x0021:                                   # WM_MOUSEACTIVATE — не забирать фокус
                return 3                                        # MA_NOACTIVATE
            if msg == 0x0202 and win.on_click:                  # WM_LBUTTONUP
                x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
                win.on_click(x, y)
                return 0
            if msg == 0x020A and win.on_wheel:                  # WM_MOUSEWHEEL
                win.on_wheel(ctypes.c_short((wparam >> 16) & 0xFFFF).value)
                return 0
            if msg == 0x0020 and win.on_click:                  # WM_SETCURSOR — «рука» над кнопками
                user32.SetCursor(user32.LoadCursorW(None, 32649 if win.hover_hand else 32512))
                return 1
            if msg == 0x0200 and win.on_move:                   # WM_MOUSEMOVE
                x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
                win.on_move(x, y)
    except Exception as e:                                      # noqa: BLE001
        print("overlay wndproc:", e, flush=True)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_proc = WNDPROC(_wndproc)                                       # держим ссылку, иначе её соберёт GC
_registered = False


def _register():
    global _registered
    if _registered:
        return
    wc = WNDCLASSW()
    wc.lpfnWndProc = _proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = _CLASS
    user32.RegisterClassW(ctypes.byref(wc))
    _registered = True


class LayeredWindow:
    """Окно без рамки поверх всех: картинка с прозрачностью, не забирает фокус."""

    def __init__(self, clickable: bool):
        _register()
        ex = 0x00080000 | 0x00000008 | 0x00000080 | 0x08000000     # LAYERED | TOPMOST | TOOLWINDOW | NOACTIVATE
        if not clickable:
            ex |= 0x00000020                                        # TRANSPARENT — клики проходят насквозь
        self.hwnd = user32.CreateWindowExW(ex, _CLASS, "overlay", 0x80000000, 0, 0, 1, 1,     # WS_POPUP
                                           None, None, kernel32.GetModuleHandleW(None), None)
        _handlers[self.hwnd] = self
        self.on_click = self.on_wheel = self.on_move = None
        self.hover_hand = False
        self.visible = False
        self.img = None
        self.pos = (0, 0)

    def update(self, img: Image.Image, x: int, y: int, alpha: int = 255):
        """Показать картинку RGBA в точке (x, y) экрана с общей прозрачностью alpha."""
        self.img, self.pos = img, (x, y)
        w, h = img.size
        a = np.asarray(img, dtype=np.uint16)
        rgb = a[..., :3] * a[..., 3:4] // 255                      # «премультипликация» альфы
        bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], a[..., 3]]).astype(np.uint8)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, bgra.tobytes(), w * h * 4)
        old = gdi32.SelectObject(mem, hbmp)
        size = wintypes.SIZE(w, h)
        src = wintypes.POINT(0, 0)
        dst = wintypes.POINT(x, y)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, alpha)), 1)    # AC_SRC_ALPHA
        user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(dst), ctypes.byref(size), mem,
                                   ctypes.byref(src), 0, ctypes.byref(blend), 2)                  # ULW_ALPHA
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)

    def show(self):
        if not self.visible:
            user32.ShowWindow(self.hwnd, 4)                        # SW_SHOWNOACTIVATE
            self.visible = True
        user32.SetWindowPos(self.hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # HWND_TOPMOST

    def hide(self):
        if self.visible:
            user32.ShowWindow(self.hwnd, 0)
            self.visible = False


def work_area() -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


# ─────────────────────────── рисование ───────────────────────────

FONTS_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def _font(name: str, size: int):
    for f in (name, "segoeui.ttf"):
        try:
            return ImageFont.truetype(os.path.join(FONTS_DIR, f), size)
        except OSError:
            continue
    return ImageFont.load_default()


PALETTE = {
    "bubble": (12, 12, 14, 255), "edge": (255, 255, 255, 26), "white": (255, 255, 255, 255),
    "grey": (142, 142, 150, 255), "dim": (110, 112, 122, 255), "link": (147, 197, 253, 255),
    "dot": (59, 130, 246, 255),
    "badge_blue": ((29, 78, 216), (37, 99, 235)), "badge_violet": ((91, 63, 210), (124, 108, 242)),
    "badge_green": ((4, 120, 87), (16, 185, 129)), "badge_red": ((185, 28, 28), (239, 68, 68)),
}


def _gradient(w: int, h: int, c1, c2) -> Image.Image:
    t = np.linspace(0, 1, w)[None, :, None]
    row = (np.array(c1)[None, None, :] * (1 - t) + np.array(c2)[None, None, :] * t).astype(np.uint8)
    return Image.fromarray(np.repeat(row, h, axis=0), "RGB")


def wrap_runs(runs: list[tuple[str, str]], styles: dict, width: int) -> list[list[tuple[str, str]]]:
    """Перенос по словам для текста из кусков разного стиля. '\\n' — новая строка."""
    lines: list[list[tuple[str, str]]] = [[]]
    line_w = 0.0
    for text, style in runs:
        font = styles[style]["font"]
        for para_i, para in enumerate(text.split("\n")):
            if para_i > 0:
                lines.append([])
                line_w = 0.0
            for word in para.split(" "):
                if not word:
                    continue
                piece = (" " if lines[-1] else "") + word
                w = font.getlength(piece)
                if line_w + w > width and lines[-1]:
                    lines.append([])
                    line_w = 0.0
                    piece, w = word, font.getlength(word)
                lines[-1].append((piece, style))
                line_w += w
    return [ln for ln in lines] or [[]]


def _ease(t: float) -> float:
    """Плавное замедление к концу (ease-out cubic)."""
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


_erf = np.frompyfunc(math.erf, 1, 1)


def _blur_profile(n: int, start: float, end: float, sigma: float) -> np.ndarray:
    """Размытый «прямоугольник» по одной оси: так выглядит тень вдоль ширины или высоты."""
    x = np.arange(n) + 0.5
    k = 1 / (sigma * math.sqrt(2))
    return (0.5 * (_erf((x - start) * k) - _erf((x - end) * k))).astype(np.float32)


class Shadow:
    """Мягкая тень под блоком любого размера. Гауссово размытие прямоугольника раскладывается на
    произведение двух одномерных — считается за миллисекунду, без тяжёлого размытия картинки."""

    def __init__(self, sigma: float, alpha: int = 150):
        self.sigma, self.alpha = sigma, alpha
        self.cache: dict = {}

    def make(self, size: tuple[int, int], box: tuple[int, int, int, int]) -> Image.Image:
        key = (size, box)
        img = self.cache.get(key)
        if img is None:
            w, h = size
            x0, y0, x1, y1 = box
            a = np.outer(_blur_profile(h, y0, y1, self.sigma), _blur_profile(w, x0, x1, self.sigma)) * self.alpha
            img = Image.new("RGBA", size, (0, 0, 0, 0))
            img.putalpha(Image.fromarray(a.astype(np.uint8), "L"))
            if len(self.cache) > 48:
                self.cache.clear()
            self.cache[key] = img
        return img.copy()


class Bubble:
    """Общее: чёрный блок с тенью и значок-«таблетка» сверху. Наследники задают содержимое."""
    WIDTH = 600
    PAD = (28, 24)

    def __init__(self, root, scale: float, clickable: bool):
        self.root, self.s = root, scale
        self.win = LayeredWindow(clickable)
        self.alpha = 0
        self._anim = None
        self._hide_job = None
        self._ticking = False
        s = scale
        self.f_badge = _font("seguisb.ttf", int(12.5 * s))
        self.f_main = _font("seguisb.ttf", int(17 * s))
        self.f_small = _font("segoeui.ttf", int(12 * s))
        self.f_small_b = _font("seguisb.ttf", int(11.5 * s))
        self.f_orig = _font("segoeui.ttf", int(14 * s))
        self.f_close = _font("segoeui.ttf", int(19 * s))
        self.badge = ("", "badge_blue", True)   # текст, цвета, пульсирующая точка
        self.pulse = 1.0
        self.bottom_offset = int(24 * s)
        self.last_size = (0, 0)
        self.shadow = Shadow(13 * s)
        self._badge_cache: dict = {}
        self._body_cache: dict = {}

    @property
    def visible(self) -> bool:
        return self.win.visible

    # — отрисовка —
    def content(self, draw: ImageDraw.ImageDraw, x0: int, y0: int, width: int, measure_only: bool) -> int:
        """Нарисовать содержимое внутри блока; вернуть его высоту."""
        raise NotImplementedError

    def _body(self, img: Image.Image, box: tuple[int, int, int, int], radius: int | None = None):
        """Сам блок: еле заметный градиент сверху вниз, тонкая светлая кромка — как стекло."""
        s = self.s
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        radius = min(radius or int(26 * s), h // 2)
        key = (w, h, radius)
        body = self._body_cache.get(key)
        if body is None:
            t = np.linspace(0, 1, h)[:, None, None]
            top, bottom = np.array([24, 24, 28]), np.array(PALETTE["bubble"][:3])
            grad = np.clip(top * (1 - np.minimum(1, t * 1.6)) + bottom * np.minimum(1, t * 1.6), 0, 255)
            body = Image.fromarray(np.repeat(grad, w, axis=1).astype(np.uint8), "RGB").convert("RGBA")
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=255)
            body.putalpha(mask)
            d = ImageDraw.Draw(body)
            d.rounded_rectangle((0, 0, w - 1, h - 1), radius, outline=PALETTE["edge"], width=max(1, int(1 * s)))
            # блик по верхней кромке
            d.arc((0, 0, 2 * radius, 2 * radius), 200, 270, fill=(255, 255, 255, 40), width=max(1, int(1 * s)))
            d.line((radius, 0, w - radius, 0), fill=(255, 255, 255, 40), width=max(1, int(1 * s)))
            d.arc((w - 1 - 2 * radius, 0, w - 1, 2 * radius), 270, 340, fill=(255, 255, 255, 40),
                  width=max(1, int(1 * s)))
            if len(self._body_cache) > 48:
                self._body_cache.clear()
            self._body_cache[key] = body
        img.alpha_composite(body, (x0, y0))

    def render(self) -> Image.Image:
        s = self.s
        W = int(self.WIDTH * s)
        pad_x, pad_y = int(self.PAD[0] * s), int(self.PAD[1] * s)
        margin = int(46 * s)                     # место для тени — чтобы она растворялась без края
        badge_h = int(30 * s)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        content_h = self.content(probe, 0, 0, W - 2 * pad_x, measure_only=True)
        bubble_h = content_h + 2 * pad_y + int(6 * s)
        total_w = W + 2 * margin
        total_h = badge_h // 2 + bubble_h + 2 * margin
        bx0, by0 = margin, margin + badge_h // 2
        bx1, by1 = bx0 + W, by0 + bubble_h
        img = self.shadow.make((total_w, total_h), (bx0, by0 + int(6 * s), bx1, by1 + int(6 * s)))
        self._body(img, (bx0, by0, bx1, by1))
        d = ImageDraw.Draw(img)
        self.content(d, bx0 + pad_x, by0 + pad_y + int(6 * s), W - 2 * pad_x, measure_only=False)
        self._draw_badge(img, (bx0 + bx1) // 2, margin)
        self.origin = (bx0, by0)
        self.last_size = (total_w, total_h)
        return img

    def _badge_image(self, text: str, colors: str) -> Image.Image:
        """Значок-«таблетка» со свечением (без точки) — рисуется один раз на каждый текст."""
        key = (text, colors)
        img = self._badge_cache.get(key)
        if img is None:
            s = self.s
            h = int(30 * s)
            dot_r = int(4 * s)
            tw = int(self.f_badge.getlength(text))
            w = tw + int(28 * s) + 2 * dot_r + int(8 * s)
            g = int(14 * s)                                      # поле под свечение
            img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
            glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(glow).rounded_rectangle((g, g + int(3 * s), g + w, g + h + int(3 * s)), h // 2,
                                                   fill=(*PALETTE[colors][1], 90))
            img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(int(8 * s))))
            grad = _gradient(w, h, *PALETTE[colors]).convert("RGBA")
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=255)
            img.paste(grad, (g, g), mask)
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((g, g, g + w - 1, g + h - 1), h // 2, outline=(255, 255, 255, 60),
                                width=max(1, int(1 * s)))
            d.text((g + int(14 * s) + 2 * dot_r + int(8 * s), g + h // 2), text, font=self.f_badge,
                   fill=(255, 255, 255, 255), anchor="lm")
            if len(self._badge_cache) > 32:
                self._badge_cache.clear()
            self._badge_cache[key] = img
        return img

    def _draw_badge(self, img: Image.Image, cx: int, top: int):
        text, colors, pulsing = self.badge
        if not text:
            return
        s = self.s
        b = self._badge_image(text, colors)
        g = int(14 * s)
        x0 = cx - (b.width - 2 * g) // 2
        img.alpha_composite(b, (x0 - g, top - g))
        h, dot_r = int(30 * s), int(4 * s)
        dx, cy = x0 + int(14 * s) + dot_r, top + h // 2
        a = int(120 + 135 * self.pulse) if pulsing else 255
        ImageDraw.Draw(img).ellipse((dx - dot_r, cy - dot_r, dx + dot_r, cy + dot_r), fill=(255, 255, 255, a))

    # — показ —
    def refresh(self):
        img = self.render()
        left, _top, right, bottom = work_area()
        w, h = img.size
        x = (left + right - w) // 2
        y = bottom - h - self.bottom_offset + int(46 * self.s)        # тень может заходить за край
        self.win.update(img, x, y, self.alpha)

    def show(self):
        self.cancel_hide()
        if not self.win.visible:
            self.alpha = 0
            self.refresh()
            self.win.show()
            self._fade(255)
        else:
            self.refresh()
        self._start_tick()

    def hide(self):
        self.cancel_hide()
        if self.win.visible:
            self._fade(0, then=self.win.hide)

    def hide_later(self, ms: int):
        self.cancel_hide()
        self._hide_job = self.root.after(ms, self.hide)

    def cancel_hide(self):
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None

    def _fade(self, target: int, then=None):
        if self._anim:
            self.root.after_cancel(self._anim)
        start, t0 = self.alpha, time.perf_counter()
        dur = 0.18 if target > start else 0.26

        def step():
            try:
                k = _ease((time.perf_counter() - t0) / dur)
                self.alpha = int(start + (target - start) * k)
                if self.win.img is not None:
                    self.win.update(self.win.img, *self.win.pos, self.alpha)
            except Exception as e:                              # noqa: BLE001 — окно не должно «зависнуть»
                print("overlay fade:", e, flush=True)
                k = 1
                self.alpha = target
            if k < 1:
                self._anim = self.root.after(16, step)
            else:
                self._anim = None
                if then:
                    then()
        step()

    TICK_MS = 85

    def _start_tick(self):
        if not self._ticking:
            self._ticking = True
            self.root.after(self.TICK_MS, self._tick)

    def _tick(self):
        """Анимация (пульсация точки и т. п.), пока окно видно."""
        if not self.win.visible:
            self._ticking = False
            return
        try:
            if self.animate() and self.alpha == 255 and not self._anim:
                self.refresh()
        except Exception as e:                                  # noqa: BLE001
            print("overlay tick:", e, flush=True)
        self.root.after(self.TICK_MS, self._tick)

    def animate(self) -> bool:
        """Сдвинуть анимацию на кадр; True — надо перерисовать."""
        if not self.badge[2]:
            return False
        self.pulse = 0.575 + 0.425 * math.cos(time.perf_counter() * 4.2)
        return True


def _norm_word(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


class DictationBubble(Bubble):
    """Диктовка: ваши слова крупным белым, «уточняющийся» хвост серым, после текста — живой индикатор голоса.

    Плавность: слово не «печатается» по буквам и не стирается назад — новое слово мягко проявляется,
    исправленное — проявляется на своём месте, засчитанное — светлеет из серого в белый. Если хвост
    на миг стал короче (модель «передумала»), прежние слова держатся ещё 0,7 с. Новая строка
    выезжает снизу, окно растёт плавно."""
    WIDTH = 540
    MAX_LINES = 3
    PAD = (24, 16)
    TICK_MS = 33             # ~30 кадров в секунду — индикатор голоса и проявление слов
    FADE = 0.24              # с — слово проявляется
    BRIGHTEN = 0.35          # с — засчитанное слово светлеет
    HOLD_SHRINK = 0.7        # с — держим прежний хвост, если новый короче

    COLORS = {"main": (245, 245, 247), "live": (150, 152, 162), "ph": (110, 112, 122), "warn": (252, 165, 165)}

    CLICKABLE = False

    def __init__(self, root, scale):
        super().__init__(root, scale, clickable=self.CLICKABLE)
        self.f_main = _font("seguisb.ttf", int(16 * scale))
        self.line_h = int(self.f_main.size * 1.45)
        self.space = self.f_main.getlength(" ")
        self.state = "idle"
        self.lang = "RU"
        self.problem = ""
        self.placeholder = "Говорите…"
        self.done_label = "Вставлено"
        self.committed = self.live = ""
        self.words: list[list] = []            # [слово, стиль, когда появилось, когда сменило стиль]
        self.level_source = lambda: 0.0
        self.level = 0.0
        self.floor_db = -60.0
        self.ready = False
        self.started = time.time()
        self._pending = None
        self.anim_h = float(self.line_h)
        self.shift = 0.0
        self.n_lines = 1
        self._t_last = time.perf_counter()

    # — состояние —
    def set_state(self, state: str, lang: str = "", done_label: str = ""):
        if lang:
            self.lang = lang.upper()
        if done_label:
            self.done_label = done_label
        if state == "rec" and self.state != "rec":       # новая диктовка — с чистого листа
            self.started = time.time()
            self.ready = False
            self.level = 0.0
            self.words, self.committed, self.live, self._pending = [], "", "", None
            self.n_lines, self.anim_h, self.shift = 1, float(self.line_h), 0.0
        self.state = state
        if state != "problem":
            self.problem = ""
        self._update_badge()

    def set_lang(self, lang: str):
        self.lang = lang.upper()
        self._update_badge()

    def set_ready(self):
        self.ready = True

    def _update_badge(self):
        if self.state == "rec":
            sec = int(time.time() - self.started)
            timer = f" · {sec // 60}:{sec % 60:02d}" if sec >= 1 else ""
            self.badge = (f"Диктовка · {self.lang}{timer}", "badge_blue", True)
        else:
            self.badge = {
                "busy": ("Уточняю текст…", "badge_violet", True),
                "done": (self.done_label, "badge_green", False),
                "problem": ("Микрофон", "badge_red", False),
            }.get(self.state, ("", "badge_blue", False))

    def set_problem(self, text: str):
        self.problem = text
        self.set_state("problem")
        self._apply("", "")

    # — текст —
    @staticmethod
    def _tokens(text: str) -> list[str]:
        return re.findall(r"\n|[^\s]+", text)

    def set_text(self, committed: str, live: str = "", placeholder: str = ""):
        if placeholder:
            self.placeholder = placeholder
        committed, live = committed.strip(), live.strip()
        old_live = [_norm_word(w) for w in self.live.split()]
        new_live = [_norm_word(w) for w in live.split()]
        if committed == self.committed and len(new_live) < len(old_live) and old_live[:len(new_live)] == new_live:
            # хвост укоротился, а начало то же — скорее всего, модель на миг «передумала»: не стираем сразу
            if self._pending is None:
                self._pending = [committed, live, time.time() + self.HOLD_SHRINK]
            else:
                self._pending[:2] = committed, live
            return
        self._pending = None
        self._apply(committed, live)

    def _apply(self, committed: str, live: str):
        import difflib
        now = time.time()
        new = [(w, "main") for w in self._tokens(committed)] + [(w, "live") for w in self._tokens(live)]
        old = self.words
        sm = difflib.SequenceMatcher(None, [_norm_word(w[0]) or w[0] for w in old],
                                     [_norm_word(w) or w for w, _ in new], autojunk=False)
        born = [now] * len(new)
        since = [now - 10] * len(new)
        for a, b, size in sm.get_matching_blocks():
            for k in range(size):
                o = old[a + k]
                born[b + k] = o[2]
                since[b + k] = o[3] if o[1] == new[b + k][1] else now
        self.words = [[w, st, born[i], since[i]] for i, (w, st) in enumerate(new)]
        self.committed, self.live = committed, live

    def show_all(self):
        """Показать весь текст сразу (итог после «стоп»)."""
        for w in self.words:
            w[2] = min(w[2], time.time() - self.FADE)

    # — раскладка —
    def _layout(self, width: float):
        """Строки: [(x, индекс слова)]; слова без места переносятся на новую строку."""
        lines: list[list] = [[]]
        x = 0.0
        for i, (w, *_rest) in enumerate(self.words):
            if w == "\n":
                lines.append([])
                x = 0.0
                continue
            ww = self.f_main.getlength(w)
            if lines[-1] and x + self.space + ww > width:
                lines.append([])
                x = 0.0
            if lines[-1]:
                x += self.space
            lines[-1].append((x, i, ww))
            x += ww
        return lines

    def _bars_w(self) -> int:
        return int(5 * 3.5 * self.s + 4 * 3 * self.s)

    # — анимация —
    def animate(self) -> bool:
        now_p = time.perf_counter()
        dt = min(0.1, now_p - self._t_last)
        self._t_last = now_p
        if self._pending and time.time() >= self._pending[2]:
            committed, live, _ = self._pending
            self._pending = None
            self._apply(committed, live)
        if self.state == "rec":
            self._update_badge()
            raw = max(float(self.level_source() or 0.0), 1e-6)
            db = 20 * math.log10(raw)
            # уровень шума: быстро вниз, медленно вверх — индикатор реагирует на голос, а не на гул
            self.floor_db = db if db < self.floor_db else self.floor_db + 2.0 * dt
            v = max(0.0, min(1.0, (db - self.floor_db - 4) / 24))
            self.level += (v - self.level) * (0.55 if v > self.level else 0.18)
        else:
            self.level += (0 - self.level) * 0.2
        self.pulse = 0.575 + 0.425 * math.cos(now_p * 4.2)
        # высота окна и прокрутка строк — плавно
        target_h = max(1, min(self.MAX_LINES, self.n_lines)) * self.line_h
        k = min(1.0, dt * 12)
        self.anim_h += (target_h - self.anim_h) * k
        self.shift += (0 - self.shift) * k
        if abs(self.anim_h - target_h) < 0.5:
            self.anim_h = float(target_h)
        if abs(self.shift) < 0.5:
            self.shift = 0.0
        moving = self.anim_h != target_h or self.shift != 0.0
        fading = any(time.time() - w[2] < self.FADE or time.time() - w[3] < self.BRIGHTEN for w in self.words)
        return moving or fading or self.state in ("rec", "busy") or self.badge[2]

    # — отрисовка —
    def render(self) -> Image.Image:
        s = self.s
        W = int(self.WIDTH * s)
        pad_x, pad_y = int(self.PAD[0] * s), int(self.PAD[1] * s)
        margin = int(46 * s)
        badge_h = int(30 * s)
        text_w = W - 2 * pad_x
        bars_gap = int(12 * s)
        # слова → строки
        if self.problem:
            shown = [[w, "warn", 0.0, 0.0] for w in self._tokens(self.problem)]
        elif self.words:
            shown = self.words
        else:
            shown = [[w, "ph", 0.0, 0.0] for w in self._tokens(self.placeholder)]
        saved, self.words = self.words, shown
        lines = self._layout(text_w - self._bars_w() - bars_gap)
        self.words = saved
        if len(lines) != self.n_lines:                          # новая строка — выезжает снизу
            self.shift += (len(lines) - self.n_lines) * self.line_h
            self.shift = max(-2.0 * self.line_h, min(2.0 * self.line_h, self.shift))
            self.n_lines = len(lines)
        text_h = max(1, int(round(self.anim_h)))
        max_text_h = self.MAX_LINES * self.line_h
        canvas = (W + 2 * margin, badge_h // 2 + max_text_h + 2 * pad_y + int(4 * s) + 2 * margin)
        bubble_h = text_h + 2 * pad_y + int(4 * s)
        bx0, bx1 = margin, margin + W
        by1 = canvas[1] - margin
        by0 = by1 - bubble_h
        img = self.shadow.make(canvas, (bx0, by0 + int(6 * s), bx1, by1 + int(6 * s)))
        self._body(img, (bx0, by0, bx1, by1))
        # текст — на отдельном слое высотой с видимую часть (что выше — плавно растворяется)
        layer = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        now = time.time()
        n = len(lines)
        x_end, y_last = 0.0, text_h - self.line_h + self.shift
        for li, line in enumerate(lines):
            y = text_h - (n - li) * self.line_h + self.shift
            if y <= -self.line_h or y >= text_h:
                continue
            for x, i, _width in line:
                w, style, born, since = shown[i]
                k = _ease((now - born) / self.FADE)
                col = self.COLORS[style]
                if style == "main":
                    kb = _ease((now - since) / self.BRIGHTEN)
                    g = self.COLORS["live"]
                    col = tuple(int(g[c] + (col[c] - g[c]) * kb) for c in range(3))
                d.text((x, y + (1 - k) * 5 * s), w, font=self.f_main, fill=(*col, int(255 * k)))
            if li == n - 1:
                x_end = (line[-1][0] + line[-1][2]) if line else 0.0
                y_last = y
        if self.state in ("rec", "busy"):
            self._draw_bars(d, x_end + bars_gap if x_end else 0, y_last + self.line_h * 0.5)
        if n > self.MAX_LINES or self.shift > 1:                # верх растворяется, если строки уходят вверх
            fade = int(10 * s)
            a = np.asarray(layer).copy()
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
            a[:fade, :, 3] = (a[:fade, :, 3] * ramp).astype(np.uint8)
            layer = Image.fromarray(a, "RGBA")
        img.alpha_composite(layer, (bx0 + pad_x, by0 + pad_y + int(2 * s)))
        self._draw_badge(img, (bx0 + bx1) // 2, by0 - badge_h // 2)
        self.last_size = (canvas[0], badge_h // 2 + bubble_h + 2 * margin)
        return img

    def _draw_bars(self, d: ImageDraw.ImageDraw, x0: float, cy: float):
        """Живой индикатор голоса: пять столбиков пляшут в такт речи; «уточняю» — мягкая волна."""
        s = self.s
        t = time.perf_counter()
        bw, gap = 3.5 * s, 3 * s
        min_h, max_h = 4 * s, 20 * s
        pattern = (0.55, 0.85, 1.0, 0.8, 0.5)
        for i in range(5):
            if self.state == "busy":
                v = 0.25 + 0.3 * (0.5 + 0.5 * math.sin(t * 7 - i * 0.9))
                col = (167, 139, 250)
            else:
                wobble = 0.72 + 0.28 * math.sin(t * 11 + i * 1.7)
                idle = 0.08 * (0.5 + 0.5 * math.sin(t * 2.4 + i * 0.8)) if self.ready else 0.0
                v = max(idle, self.level * pattern[i] * wobble)
                k = i / 4
                col = (int(59 + (99 - 59) * k), int(130 + (102 - 130) * k), int(246 + (241 - 246) * k))
            h = min_h + (max_h - min_h) * v
            x = x0 + i * (bw + gap)
            a = 255 if (self.ready or self.state == "busy") else 110
            d.rounded_rectangle((x, cy - h / 2, x + bw, cy + h / 2), radius=bw / 2, fill=(*col, a))

    def relayout(self, bottom_offset: int):
        self.bottom_offset = bottom_offset
        if self.visible:
            self.refresh()


class AquaBubble(DictationBubble):
    """Окошко диктовки в духе Aqua Voice.

    • Небольшое: начинается с одной строки и плавно растёт, когда текста становится больше (до 5 строк,
      дальше старые строки мягко уходят вверх).
    • Слова, которые ещё уточняются, чуть размыты; когда фраза засчитана — становятся чёткими. Поэтому
      правки модели выглядят как «проявление», а не как прыжки текста.
    • Новые слова появляются не пачкой, а одно за другим — ввод не «замирает» между обновлениями.
    • Синяя точка-курсор дышит в такт голосу. Внизу — куда вставится текст, какой микрофон и кнопка «стоп»."""
    WIDTH = 440
    MAX_LINES = 5
    TICK_MS = 16             # ~60 кадров в секунду — рост окна и проявление слов
    CLICKABLE = True         # кнопка «стоп»
    SHARPEN = 0.35           # с — засчитанное слово из размытого становится чётким
    RADIUS = 26

    def __init__(self, root, scale):
        super().__init__(root, scale)
        s = scale
        self.f_main = _font("seguisb.ttf", int(16.5 * s))
        self.line_h = int(self.f_main.size * 1.42)
        self.space = self.f_main.getlength(" ")
        self.anim_h = float(self.line_h)
        self.f_strip = _font("segoeui.ttf", int(11.5 * s))
        self.f_strip_b = _font("seguisb.ttf", int(11.5 * s))
        self.target = ""                  # куда вставится текст: «Chrome: claude.ai»
        self.mic = ""
        self.on_stop = None
        self.stop_box = None
        self.hover_stop = False
        self._body_h_cache: dict = {}
        self.win.on_click = self._click
        self.win.on_move = self._move

    def set_context(self, target: str = "", mic: str = ""):
        if target:
            self.target = target
        if mic:
            self.mic = mic

    # — слова появляются одно за другим, а не пачкой —
    def _apply(self, committed: str, live: str):
        t = time.time()
        super()._apply(committed, live)
        fresh = [w for w in self.words if w[2] >= t]
        if len(fresh) > 1:
            step = min(0.06, 0.4 / len(fresh))
            for i, w in enumerate(fresh):
                w[2] = t + i * step

    # — мышь: кнопка «стоп» —
    def _hit_stop(self, x, y) -> bool:
        b = self.stop_box
        return bool(b) and b[0] <= x <= b[2] and b[1] <= y <= b[3]

    def _click(self, x, y):
        if self._hit_stop(x, y) and self.on_stop and self.state == "rec":
            self.on_stop()

    def _move(self, x, y):
        over = self._hit_stop(x, y) and self.state == "rec"
        self.win.hover_hand = over
        self.hover_stop = over

    # — отрисовка —
    def _box(self, w: int, h: int, strip_h: int) -> Image.Image:
        """Чёрный блок с большими скруглениями и чуть более светлой полоской снизу."""
        key = (w, h, strip_h)
        img = self._body_h_cache.get(key)
        if img is None:
            s = self.s
            r = int(self.RADIUS * s)
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=255)
            fill = Image.new("RGBA", (w, h), (6, 6, 8, 250))
            ImageDraw.Draw(fill).rectangle((0, h - strip_h, w, h), fill=(22, 22, 25, 252))
            img.paste(fill, (0, 0), mask)
            d = ImageDraw.Draw(img)
            d.line((0, h - strip_h, w, h - strip_h), fill=(255, 255, 255, 16), width=max(1, int(s)))
            d.rounded_rectangle((0, 0, w - 1, h - 1), r, outline=(255, 255, 255, 30), width=max(1, int(s)))
            if len(self._body_h_cache) > 40:
                self._body_h_cache.clear()
            self._body_h_cache[key] = img
        return img

    @staticmethod
    def _fit(font, text: str, width: float) -> str:
        if font.getlength(text) <= width:
            return text
        while text and font.getlength(text + "…") > width:
            text = text[:-1]
        return text.rstrip() + "…"

    def render(self) -> Image.Image:
        s = self.s
        W = int(self.WIDTH * s)
        pad_x, pad_top, pad_bot = int(24 * s), int(22 * s), int(14 * s)
        strip_h = int(34 * s)
        margin, badge_h = int(40 * s), int(30 * s)
        cursor_room = int(24 * s)
        text_w = W - 2 * pad_x
        if self.problem:
            shown = [[w, "warn", 0.0, 0.0] for w in self._tokens(self.problem)]
        elif self.words:
            shown = self.words
        else:
            shown = [[w, "ph", 0.0, 0.0] for w in self._tokens(self.placeholder)]
        saved, self.words = self.words, shown
        lines = self._layout(text_w - cursor_room)
        self.words = saved
        # окно растёт и курсор движется вслед за уже показанными словами, а не за теми, что ещё появятся
        now = time.time()
        revealed = [i for i, wd in enumerate(shown) if wd[2] <= now]
        if revealed:
            last = revealed[-1]
            upto = max((li for li, line in enumerate(lines) for _, i, _ in line if i <= last), default=0)
            lines = lines[:upto + 1]
        else:
            lines = lines[:1]
        if len(lines) != self.n_lines:                          # новая строка — выезжает снизу
            self.shift += (len(lines) - self.n_lines) * self.line_h
            self.shift = max(-2.0 * self.line_h, min(2.0 * self.line_h, self.shift))
            self.n_lines = len(lines)
        text_h = max(1, int(round(self.anim_h)))
        box_h = pad_top + text_h + pad_bot + strip_h
        max_box_h = pad_top + self.MAX_LINES * self.line_h + pad_bot + strip_h
        canvas = (W + 2 * margin, badge_h // 2 + max_box_h + 2 * margin)
        bx0, bx1 = margin, margin + W
        by1 = canvas[1] - margin
        by0 = by1 - box_h
        img = self.shadow.make(canvas, (bx0, by0 + int(8 * s), bx1, by1 + int(8 * s)))
        img.alpha_composite(self._box(W, box_h, strip_h), (bx0, by0))

        # текст: чёткий слой (засчитанное) + мягко размытый (уточняется) + лёгкое свечение
        sharp = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
        soft = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
        ds, dsoft = ImageDraw.Draw(sharp), ImageDraw.Draw(soft)
        n = len(lines)
        x_end, y_last = 0.0, text_h - self.line_h + self.shift
        for li, line in enumerate(lines):
            y = text_h - (n - li) * self.line_h + self.shift
            if y <= -self.line_h or y >= text_h:
                continue
            for x, i, _width in line:
                w, style, born, since = shown[i]
                k = _ease((now - born) / self.FADE) if now >= born else 0.0
                if k <= 0:
                    continue
                if k > 0.25:
                    x_end, y_last = x + _width, y               # курсор — сразу за последним показанным
                yy = y + (1 - k) * 4 * s
                if style == "live":
                    dsoft.text((x, yy), w, font=self.f_main, fill=(236, 238, 245, int(235 * k)))
                    ds.text((x, yy), w, font=self.f_main, fill=(236, 238, 245, int(70 * k)))
                elif style == "main":
                    kc = _ease((now - since) / self.SHARPEN)          # из размытого — в чёткий
                    if kc < 1:
                        dsoft.text((x, yy), w, font=self.f_main, fill=(245, 245, 247, int(235 * k * (1 - kc))))
                    ds.text((x, yy), w, font=self.f_main, fill=(245, 245, 247, int(255 * k * (0.27 + 0.73 * kc))))
                else:
                    col = self.COLORS[style]
                    ds.text((x, yy), w, font=self.f_main, fill=(*col, int(255 * k)))
            if li == n - 1 and not line:                        # пустая строка после «новой строки»
                x_end, y_last = 0.0, y
        layer = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
        small = sharp.resize((max(1, text_w // 3), max(1, text_h // 3)), Image.BILINEAR)
        glow = small.filter(ImageFilter.GaussianBlur(2.2 * s)).resize((text_w, text_h), Image.BILINEAR)
        ga = np.asarray(glow).copy()
        ga[..., 3] = (ga[..., 3] * 0.45).astype(np.uint8)
        layer.alpha_composite(Image.fromarray(ga, "RGBA"))
        layer.alpha_composite(soft.filter(ImageFilter.GaussianBlur(1.7 * s)))
        layer.alpha_composite(sharp)
        if n > self.MAX_LINES or self.shift > 1:                # верх растворяется, если строки уходят вверх
            fade = int(12 * s)
            a = np.asarray(layer).copy()
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
            a[:fade, :, 3] = (a[:fade, :, 3] * ramp).astype(np.uint8)
            layer = Image.fromarray(a, "RGBA")
        tx, ty = bx0 + pad_x, by0 + pad_top
        img.alpha_composite(layer, (tx, ty))
        d = ImageDraw.Draw(img)

        # курсор: синяя точка дышит в такт голосу; «уточняю» — фиолетовая
        if self.state in ("rec", "busy"):
            cx = tx + (x_end + int(13 * s) if x_end else int(7 * s))
            cy = ty + min(max(y_last, 0), text_h - self.line_h) + self.line_h * 0.5   # не выходит за текст
            if self.state == "busy":
                col, r = (167, 139, 250), 6.5 * s * (0.85 + 0.15 * self.pulse)
            else:
                col, r = (59, 130, 246), 6.5 * s * (0.9 + 0.45 * self.level)
            a = int(150 + 105 * self.pulse) if self.ready or self.state == "busy" else 110
            g = Image.new("RGBA", (int(r * 6), int(r * 6)), (0, 0, 0, 0))
            ImageDraw.Draw(g).ellipse((r * 1.5, r * 1.5, r * 4.5, r * 4.5), fill=(*col, a // 2))
            g = g.filter(ImageFilter.GaussianBlur(r * 0.8))
            img.alpha_composite(g, (int(cx - r * 3), int(cy - r * 3)))
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*col, a))
            d.ellipse((cx - r * 0.45, cy - r * 0.75, cx + r * 0.25, cy - r * 0.15), fill=(255, 255, 255, a // 3))

        # нижняя полоска: значок, куда пишем, микрофон, «стоп»
        sy = by1 - strip_h / 2
        icon_r = 8 * s
        ix = bx0 + int(20 * s)
        d.ellipse((ix - icon_r, sy - icon_r, ix + icon_r, sy + icon_r), fill=(58, 58, 62, 255))
        d.ellipse((ix - icon_r * 0.35, sy - icon_r * 0.55, ix + icon_r * 0.35, sy + icon_r * 0.25),
                  fill=(200, 200, 206, 255))
        d.arc((ix - icon_r * 0.6, sy - icon_r * 0.3, ix + icon_r * 0.6, sy + icon_r * 0.55), 20, 160,
              fill=(200, 200, 206, 255), width=max(1, int(1.3 * s)))
        right = bx1 - int(16 * s)
        if self.state == "rec":
            sr_ = 10 * s
            scx = right - sr_
            fill = (255, 69, 58, 255) if not self.hover_stop else (255, 99, 88, 255)
            d.ellipse((scx - sr_, sy - sr_, scx + sr_, sy + sr_), fill=fill)
            q = sr_ * 0.38
            d.rounded_rectangle((scx - q, sy - q, scx + q, sy + q), radius=q * 0.3, fill=(255, 255, 255, 255))
            self.stop_box = (int(scx - sr_ - 4 * s), int(sy - sr_ - 4 * s), int(scx + sr_ + 4 * s),
                             int(sy + sr_ + 4 * s))
            right = scx - sr_ - int(10 * s)
        else:
            self.stop_box = None
        dim = (142, 142, 150, 255)
        mic = self._fit(self.f_strip, self.mic, W * 0.32) if self.mic else ""
        if mic:
            mw = self.f_strip.getlength(mic)
            d.text((right - mw, sy), mic, font=self.f_strip, fill=dim, anchor="lm")
            right -= mw + int(14 * s)
        if self.target:
            left = ix + icon_r + int(14 * s)
            text = self._fit(self.f_strip, "→  " + self.target, max(10, right - left))
            d.text(((left + right) / 2, sy), text, font=self.f_strip, fill=dim, anchor="mm")

        self._draw_badge(img, (bx0 + bx1) // 2, by0 - badge_h // 2)
        self.last_size = (canvas[0], badge_h // 2 + box_h + 2 * margin)
        return img


class CompactBubble(DictationBubble):
    """Компактное окошко диктовки — капсула в одну строку внизу экрана.

    Слева пляшет индикатор голоса, посередине — ваши слова: новые мягко проявляются, строка плавно
    уезжает влево, а старые слова растворяются у левого края. Справа — язык и время записи.
    Анимации те же, что у большого окошка (DictationBubble), меняется только раскладка."""
    WIDTH = 460
    HEIGHT = 46
    PAD_X = 18
    META = {"live": (142, 142, 150), "busy": (196, 181, 253), "done": (110, 231, 183), "warn": (252, 165, 165)}

    def __init__(self, root, scale):
        super().__init__(root, scale)
        s = scale
        self.f_main = _font("seguisb.ttf", int(14.5 * s))
        self.space = self.f_main.getlength(" ")
        self.line_h = int(self.f_main.size * 1.45)
        self.f_meta = _font("seguisb.ttf", int(11.5 * s))
        self.shadow = Shadow(11 * s, alpha=140)
        self.xoff = 0.0                       # насколько строка уехала влево (плавно догоняет target)
        self.target_xoff = 0.0
        self._t_x = time.perf_counter()

    def set_state(self, state: str, lang: str = "", done_label: str = ""):
        if state == "rec" and self.state != "rec":
            self.xoff = self.target_xoff = 0.0
        super().set_state(state, lang, done_label)

    def animate(self) -> bool:
        busy = super().animate()
        now = time.perf_counter()
        dt, self._t_x = min(0.1, now - self._t_x), now
        self.xoff += (self.target_xoff - self.xoff) * min(1.0, dt * 9)
        if abs(self.xoff - self.target_xoff) < 0.5:
            self.xoff = self.target_xoff
        return busy or self.xoff != self.target_xoff

    def _meta(self) -> tuple[str, tuple]:
        if self.state == "rec":
            sec = int(time.time() - self.started)
            return f"{self.lang} · {sec // 60}:{sec % 60:02d}", self.META["live"]
        if self.state == "busy":
            return "Уточняю…", self.META["busy"]
        if self.state == "done":
            return self.done_label, self.META["done"]
        return "", self.META["live"]

    def render(self) -> Image.Image:
        if self.problem:
            return self._render_problem()
        s = self.s
        W, H = int(self.WIDTH * s), int(self.HEIGHT * s)
        margin = int(36 * s)
        pad = int(self.PAD_X * s)
        canvas = (W + 2 * margin, H + 2 * margin)
        bx0, by0 = margin, margin
        bx1, by1 = bx0 + W, by0 + H
        img = self.shadow.make(canvas, (bx0, by0 + int(5 * s), bx1, by1 + int(5 * s)))
        self._body(img, (bx0, by0, bx1, by1), radius=H // 2)
        d = ImageDraw.Draw(img)
        cy = by0 + H / 2
        # слева — индикатор: голос / «уточняю» / галочка «готово»
        ind_w = self._bars_w()
        if self.state in ("rec", "busy"):
            self._draw_bars(d, bx0 + pad, cy)
        elif self.state == "done":
            r = 9 * s
            cx = bx0 + pad + ind_w / 2
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(16, 185, 129, 255))
            d.line((cx - r * 0.45, cy + r * 0.02, cx - r * 0.1, cy + r * 0.38, cx + r * 0.5, cy - r * 0.35),
                   fill=(255, 255, 255, 255), width=max(2, int(2.2 * s)), joint="curve")
        # справа — язык и время, «Уточняю…» или «Вставлено»
        meta, meta_col = self._meta()
        mw = self.f_meta.getlength(meta) if meta else 0
        mx = bx1 - pad - mw
        if meta:
            d.text((mx, cy), meta, font=self.f_meta, fill=(*meta_col, 255), anchor="lm")
        # посередине — слова в одну строку
        tx0 = bx0 + pad + ind_w + int(12 * s)
        tx1 = (mx - int(14 * s)) if meta else (bx1 - pad)
        area = max(10, int(tx1 - tx0))
        layer = Image.new("RGBA", (area, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        shown = self.words or [[w, "ph", 0.0, 0.0] for w in self._tokens(self.placeholder)]
        widths = [self.f_main.getlength(w) if w != "\n" else 0.0 for w, *_ in shown]
        xs, x = [], 0.0
        for w in widths:
            xs.append(x)
            x += w + (self.space if w else 0.0)
        total = max(0.0, x - self.space)
        self.target_xoff = max(0.0, total - area)
        now = time.time()
        for (w, style, born, since), x0, ww in zip(shown, xs, widths, strict=True):
            x = x0 - self.xoff
            if not ww or x + ww < 0 or x > area:
                continue
            k = _ease((now - born) / self.FADE)
            col = self.COLORS[style]
            if style == "main":
                kb = _ease((now - since) / self.BRIGHTEN)
                g = self.COLORS["live"]
                col = tuple(int(g[c] + (col[c] - g[c]) * kb) for c in range(3))
            ld.text((x, H / 2 + (1 - k) * 4 * s), w, font=self.f_main, fill=(*col, int(255 * k)), anchor="lm")
        if self.xoff > 1:                                     # старые слова растворяются у левого края
            fade = min(area, int(42 * s))
            a = np.asarray(layer).copy()
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[None, :] ** 1.5
            a[:, :fade, 3] = (a[:, :fade, 3] * ramp).astype(np.uint8)
            layer = Image.fromarray(a, "RGBA")
        img.alpha_composite(layer, (int(tx0), by0))
        self.last_size = canvas
        return img

    def _render_problem(self) -> Image.Image:
        """Предупреждение (например, «микрофон выключен») — в несколько строк, красным."""
        s = self.s
        W = int(self.WIDTH * s)
        margin, pad = int(36 * s), int(self.PAD_X * s)
        styles = {"warn": {"font": self.f_main, "fill": (*self.META["warn"], 255)}}
        lines = wrap_runs([(self.problem, "warn")], styles, W - 2 * pad - int(20 * s))
        line_h = int(self.f_main.size * 1.45)
        H = max(int(self.HEIGHT * s), len(lines) * line_h + 2 * int(12 * s))
        canvas = (W + 2 * margin, H + 2 * margin)
        bx0, by0 = margin, margin
        img = self.shadow.make(canvas, (bx0, by0 + int(5 * s), bx0 + W, by0 + H + int(5 * s)))
        self._body(img, (bx0, by0, bx0 + W, by0 + H), radius=min(H // 2, int(22 * s)))
        d = ImageDraw.Draw(img)
        r = 4 * s
        d.ellipse((bx0 + pad, by0 + H / 2 - r, bx0 + pad + 2 * r, by0 + H / 2 + r), fill=(239, 68, 68, 255))
        y = by0 + (H - len(lines) * line_h) / 2
        for line in lines:
            text = "".join(piece for piece, _ in line).lstrip()
            d.text((bx0 + pad + int(20 * s), y), text, font=self.f_main, fill=styles["warn"]["fill"])
            y += line_h
        self.last_size = canvas
        return img


class LectureBubble(Bubble):
    """Лекция: текст по ходу записи, затем перевод с оригиналом и кнопками. Окно кликабельно."""
    WIDTH = 620
    MAX_LINES = 7

    def __init__(self, root, scale, on_close, on_copy, on_open):
        super().__init__(root, scale, clickable=True)
        self.on_close, self.on_copy, self.on_open = on_close, on_copy, on_open
        self.win.on_click = self._click
        self.win.on_wheel = self._wheel
        self.win.on_move = self._move
        self.state = "idle"
        self.runs: list[tuple[str, str]] = []
        self.hint = ""
        self.mode = ""
        self.ru = ""
        self.path = ""
        self.src_title = "Слушаю"
        self.lang = "en"
        self.follow = True
        self.scroll = 0                     # сколько строк прокручено вниз от начала
        self.buttons: list[tuple[tuple[int, int, int, int], str]] = []
        self.copied = False
        self.last_text = ""

    # — состояния —
    def listening(self, en: str, live: str):
        self.state = "listening"
        self.badge = (f"{self.src_title} · {self.lang.upper()}", "badge_red", True)
        self.last_text = (en + " " + live).strip()
        self.runs = [(en, "main"), (live, "live")] if (en or live) else [("Жду речь — слова появятся здесь…", "ph")]

    def translating(self, en: str):
        self.state = "translating"
        self.badge = ("Перевожу…" if self.lang == "en" else "Сохраняю…", "badge_violet", True)
        self.hint = ""
        self.runs = [(en, "live")]

    def done(self, en: str, ru: str, path: str = ""):
        self.state = "done"
        self.follow, self.scroll, self.copied = False, 0, False
        self.ru, self.path = (ru or en), path
        if ru:
            self.badge = ("Перевод готов", "badge_green", False)
            self.runs = [(ru, "main"), ("\n\nОРИГИНАЛ", "label"), ("\n" + en, "orig")]
        else:
            self.badge = ("Текст лекции готов", "badge_green", False)
            self.runs = [(en, "main")]
        self.hint = ("Сохранено: Документы → Голосовой помощник → Лекции" if path else "") + "   ·   Alt+X — снова"

    def set_mode(self, text: str):
        self.mode = text

    def set_hint(self, text: str):
        self.hint = text

    def flash_copied(self):
        self.copied = True
        if self.visible:
            self.refresh()
        self.root.after(1600, lambda: (setattr(self, "copied", False), self.visible and self.refresh()))

    # — содержимое —
    def _styles(self):
        return {"main": {"font": self.f_main, "fill": PALETTE["white"]},
                "live": {"font": self.f_main, "fill": PALETTE["grey"]},
                "ph": {"font": self.f_main, "fill": PALETTE["dim"]},
                "label": {"font": self.f_small_b, "fill": PALETTE["dim"]},
                "orig": {"font": self.f_orig, "fill": PALETTE["grey"]}}

    def content(self, d, x0, y0, width, measure_only):
        s = self.s
        styles = self._styles()
        lines = wrap_runs(self.runs, styles, width)
        heights = [int(max(styles[st]["font"].size for _, st in ln) * 1.45) if ln else int(10 * s)
                   for ln in lines]
        total_lines = len(lines)
        if self.follow:
            self.scroll = max(0, total_lines - self.MAX_LINES)
        self.scroll = max(0, min(self.scroll, max(0, total_lines - self.MAX_LINES)))
        view = range(self.scroll, min(total_lines, self.scroll + self.MAX_LINES))
        top_bar = int(26 * s)
        hint_h = int(24 * s) if self.hint else 0
        body_h = sum(heights[i] for i in view)
        if measure_only:
            return top_bar + body_h + hint_h
        # верхняя строка: режим слева, кнопки справа
        self.buttons = []
        d.text((x0, y0), self.mode, font=self.f_small, fill=PALETTE["dim"])
        x = x0 + width
        items = [("×", "close")]
        if self.state == "done":
            if self.path:
                items.append(("Открыть файл", "open"))
            items.append(("Скопировано ✓" if self.copied else "Копировать", "copy"))
        for label, action in items:
            fnt = self.f_close if action == "close" else self.f_small_b
            w = fnt.getlength(label)
            x -= w
            color = PALETTE["grey"] if action == "close" else PALETTE["link"]
            d.text((x, y0 - (int(5 * s) if action == "close" else 0)), label, font=fnt, fill=color)
            self.buttons.append(((int(x - 6 * s), int(y0 - 6 * s), int(x + w + 6 * s), int(y0 + 20 * s)), action))
            x -= int(18 * s)
        # текст
        y = y0 + top_bar
        for i in view:
            xx = x0
            line = lines[i]
            if line and line[0][0].startswith(" "):
                line = [(line[0][0].lstrip(), line[0][1])] + line[1:]
            for piece, st in line:
                d.text((xx, y), piece, font=styles[st]["font"], fill=styles[st]["fill"])
                xx += styles[st]["font"].getlength(piece)
            y += heights[i]
        if total_lines > self.MAX_LINES:                      # тонкая полоса прокрутки справа
            track_h = body_h
            bar_h = max(int(20 * s), track_h * self.MAX_LINES // total_lines)
            bar_y = y0 + top_bar + (track_h - bar_h) * self.scroll // max(1, total_lines - self.MAX_LINES)
            bx = x0 + width + int(14 * s)
            d.rounded_rectangle((bx, bar_y, bx + int(3 * s), bar_y + bar_h), int(2 * s), fill=(255, 255, 255, 50))
        if self.hint:
            d.text((x0, y + int(6 * s)), self.hint, font=self.f_small, fill=PALETTE["dim"])
            y += hint_h
        return y - y0

    # — мышь —
    def _hit(self, x, y):
        for (x0, y0, x1, y1), action in self.buttons:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return action
        return None

    def _click(self, x, y):
        action = self._hit(x, y)
        if action == "close":
            self.on_close()
        elif action == "copy":
            self.on_copy()
        elif action == "open" and self.path:
            self.on_open(self.path)

    def _move(self, x, y):
        self.win.hover_hand = self._hit(x, y) is not None

    def _wheel(self, delta: int):
        self.scroll += -1 if delta > 0 else 1
        self.follow = False
        self.refresh()

    def relayout(self, bottom_offset: int):
        self.bottom_offset = bottom_offset
        if self.visible:
            self.refresh()
