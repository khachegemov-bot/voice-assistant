"""Всплывающие окна в «дорогом» стиле: глубокий чёрный блок с большими скруглениями и мягкой тенью,
крупный белый текст, синий значок-«таблетка» сверху, пульсирующая точка-курсор.

Окно рисуется как картинка (Pillow, со сглаживанием) и показывается через «многослойные» окна
Windows (UpdateLayeredWindow) — с настоящей прозрачностью по каждому пикселю. Окна не забирают фокус:
текст продолжает печататься туда, где стоит курсор.
"""
import ctypes
import os
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
        self.pos = None                         # своё положение (если окно двигали) — пока не используется
        s = scale
        self.f_badge = _font("seguisb.ttf", int(12.5 * s))
        self.f_main = _font("seguisb.ttf", int(17 * s))
        self.f_small = _font("segoeui.ttf", int(12 * s))
        self.f_small_b = _font("seguisb.ttf", int(11.5 * s))
        self.f_orig = _font("segoeui.ttf", int(14 * s))
        self.f_close = _font("segoeui.ttf", int(19 * s))
        self.badge = ("", "badge_blue", True)   # текст, цвета, пульсирующая точка
        self.pulse = 1.0
        self._pulse_dir = -1
        self.bottom_offset = int(24 * s)
        self.last_size = (0, 0)
        self._shadow_cache: dict = {}

    @property
    def visible(self) -> bool:
        return self.win.visible

    # — отрисовка —
    def content(self, draw: ImageDraw.ImageDraw, x0: int, y0: int, width: int, measure_only: bool) -> int:
        """Нарисовать содержимое внутри блока; вернуть его высоту."""
        raise NotImplementedError

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
        radius = int(26 * s)
        # тень (размытие — самое дорогое; для каждого размера считаем один раз и запоминаем)
        key = (total_w, total_h)
        if key not in self._shadow_cache:
            shadow = Image.new("RGBA", key, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle((bx0, by0 + int(6 * s), bx1, by1 + int(6 * s)), radius,
                                                     fill=(0, 0, 0, 150))
            if len(self._shadow_cache) > 12:
                self._shadow_cache.clear()
            self._shadow_cache[key] = shadow.filter(ImageFilter.GaussianBlur(int(13 * s)))
        img = self._shadow_cache[key].copy()
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((bx0, by0, bx1, by1), radius, fill=PALETTE["bubble"], outline=PALETTE["edge"],
                            width=max(1, int(1 * s)))
        self.content(d, bx0 + pad_x, by0 + pad_y + int(6 * s), W - 2 * pad_x, measure_only=False)
        self._draw_badge(img, (bx0 + bx1) // 2, margin)
        self.origin = (bx0, by0)
        return img

    def _draw_badge(self, img: Image.Image, cx: int, top: int):
        text, colors, pulsing = self.badge
        if not text:
            return
        s = self.s
        h = int(30 * s)
        dot_r = int(4 * s)
        tw = int(self.f_badge.getlength(text))
        w = tw + int(28 * s) + 2 * dot_r + int(8 * s)
        x0 = cx - w // 2
        grad = _gradient(w, h, *PALETTE[colors]).convert("RGBA")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=255)
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle((x0, top + int(3 * s), x0 + w, top + h + int(3 * s)), h // 2,
                                               fill=(*PALETTE[colors][1], 90))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(int(8 * s))))
        img.paste(grad, (x0, top), mask)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((x0, top, x0 + w - 1, top + h - 1), h // 2, outline=(255, 255, 255, 60),
                            width=max(1, int(1 * s)))
        dx = x0 + int(14 * s) + dot_r
        cy = top + h // 2
        a = int(120 + 135 * self.pulse) if pulsing else 255
        d.ellipse((dx - dot_r, cy - dot_r, dx + dot_r, cy + dot_r), fill=(255, 255, 255, a))
        d.text((dx + dot_r + int(8 * s), cy), text, font=self.f_badge, fill=(255, 255, 255, 255), anchor="lm")

    # — показ —
    def refresh(self):
        img = self.render()
        l, t, r, b = work_area()
        w, h = img.size
        x = (l + r - w) // 2
        y = b - h - self.bottom_offset + int(46 * self.s)        # тень может заходить за край
        self.last_size = (w, h)
        self.win.update(img, x, y, self.alpha)

    def show(self):
        self.cancel_hide()
        if not self.win.visible:
            self.alpha = 0
            self.refresh()
            self.win.show()
            self._fade(255)
            self._tick()
        else:
            self.refresh()

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

        def step():
            delta = 255 // 7
            self.alpha = min(target, self.alpha + delta) if target > self.alpha else max(target, self.alpha - delta)
            if self.win.img is not None:
                self.win.update(self.win.img, *self.win.pos, self.alpha)
            if self.alpha != target:
                self._anim = self.root.after(16, step)
            else:
                self._anim = None
                if then:
                    then()
        step()

    def _tick(self):
        """Пульсация точек ~12 раз в секунду, пока окно видно."""
        if not self.win.visible:
            return
        if self.animated():
            self.pulse += 0.12 * self._pulse_dir
            if self.pulse <= 0.15 or self.pulse >= 1:
                self._pulse_dir *= -1
                self.pulse = max(0.15, min(1.0, self.pulse))
            if self.alpha == 255 and not self._anim:
                self.refresh()
        self.root.after(85, self._tick)

    def animated(self) -> bool:
        return bool(self.badge[2])


class DictationBubble(Bubble):
    """Диктовка: ваши слова крупным белым, «уточняющийся» хвост серым, синяя точка-курсор в конце."""
    WIDTH = 520             # компактнее
    MAX_LINES = 3           # видно три последние строки
    PAD = (24, 18)

    def __init__(self, root, scale):
        super().__init__(root, scale, clickable=False)
        self.committed, self.live, self.placeholder = "", "", "Говорите…"
        self.cursor = True
        self.lang = "RU"
        self.problem = ""
        self.f_main = _font("seguisb.ttf", int(16 * scale))     # компактнее, чем у окна лекции
        self.shown = 0                       # сколько знаков уже «допечатано» на экране
        self._typing = None

    def set_state(self, state: str, lang: str = ""):
        if lang:
            self.lang = lang.upper()
        self.cursor = state == "rec"
        self.badge = {
            "rec": (f"Диктовка · {self.lang}", "badge_blue", True),
            "busy": ("Уточняю текст…", "badge_violet", True),
            "done": ("Вставлено", "badge_green", False),
            "problem": ("Микрофон", "badge_red", False),
        }.get(state, ("", "badge_blue", False))
        if state != "problem":
            self.problem = ""

    def _full(self) -> str:
        return (self.committed + (" " if self.committed and self.live else "") + self.live).strip()

    def set_text(self, committed: str, live: str = "", placeholder: str = ""):
        old = self._full()
        self.committed, self.live = committed.strip(), live.strip()
        if placeholder:
            self.placeholder = placeholder
        new = self._full()
        # плавная «допечатка»: общее начало оставляем, новое дописываем по нескольку знаков за кадр
        common = 0
        for a, b in zip(old, new):
            if a != b:
                break
            common += 1
        self.shown = min(self.shown, common)
        self._start_typing()

    def _start_typing(self):
        if self._typing is None and self.shown < len(self._full()):
            self._typing = self.root.after(0, self._type_step)

    def _type_step(self):
        self._typing = None
        target = len(self._full())
        if self.shown < target:
            # быстрее, если отстаём сильно: весь хвост появляется за ~0,2–0,3 с
            self.shown = min(target, self.shown + max(2, (target - self.shown + 5) // 6))
            if self.visible and self.alpha == 255:
                self.refresh()
            self._typing = self.root.after(28, self._type_step)

    def show_all(self):
        """Показать весь текст сразу (например, итог после «стоп»)."""
        self.shown = len(self._full())

    def set_problem(self, text: str):
        self.problem = text
        self.set_state("problem")

    def content(self, d, x0, y0, width, measure_only):
        s = self.s
        styles = {"main": {"font": self.f_main, "fill": PALETTE["white"]},
                  "live": {"font": self.f_main, "fill": PALETTE["grey"]},
                  "ph": {"font": self.f_main, "fill": PALETTE["dim"]},
                  "warn": {"font": self.f_main, "fill": (252, 165, 165, 255)}}
        if self.problem:
            runs = [(self.problem, "warn")]
        elif self.committed or self.live:
            full, n = self._full(), self.shown              # на экране — только «допечатанная» часть
            border = len(self.committed)
            runs = [(full[:min(n, border)], "main"), (full[border:n] if n > border else "", "live")]
            if not any(t.strip() for t, _ in runs):
                runs = [(self.placeholder, "ph")]
        else:
            runs = [(self.placeholder, "ph")]
        lines = wrap_runs(runs, styles, width - int(18 * s))
        lines = lines[-self.MAX_LINES:]
        line_h = int(self.f_main.size * 1.42)
        if measure_only:
            return line_h * len(lines)
        y = y0
        x_end = x0
        for i, line in enumerate(lines):
            x = x0
            if i == 0 and line and line[0][0].startswith(" "):
                line = [(line[0][0].lstrip(), line[0][1])] + line[1:]
            for piece, style in line:
                d.text((x, y), piece, font=styles[style]["font"], fill=styles[style]["fill"])
                x += styles[style]["font"].getlength(piece)
            x_end = x
            y += line_h
        if self.cursor:                                           # синяя точка-курсор, как на образце
            r = int(6.5 * s)
            cx, cy = x_end + int(10 * s) + r, y - line_h + int(self.f_main.size * 0.72)
            a = int(110 + 145 * self.pulse)
            d.ellipse((cx - r - int(3 * s), cy - r - int(3 * s), cx + r + int(3 * s), cy + r + int(3 * s)),
                      fill=(59, 130, 246, a // 4))
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(59, 130, 246, a))
        return y - y0

    def animated(self) -> bool:
        return self.cursor or super().animated()

    def relayout(self, bottom_offset: int):
        self.bottom_offset = bottom_offset
        if self.visible:
            self.refresh()


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
            d.text((x, y0 - (int(5 * s) if action == "close" else 0)), label, font=fnt, fill=PALETTE["link"] if action != "close" else PALETTE["grey"])
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
