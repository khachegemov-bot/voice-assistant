"""Снимки экранов программы (для проверки дизайна и для инструкции).
Запуск: venv\\Scripts\\python tools\\screenshots.py <папка>
Снимает окна напрямую (PrintWindow), поэтому работает даже при заблокированном экране."""
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageGrab

ctypes.windll.shcore.SetProcessDpiAwareness(1)      # как в самой программе — иначе окно рисуется белым
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as A  # noqa: E402

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
u, g = ctypes.windll.user32, ctypes.windll.gdi32


def locked() -> bool:
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq LogonUI.exe"], capture_output=True, text=True).stdout
    return "LogonUI.exe" in out


class BIH(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("w", ctypes.c_int32), ("h", ctypes.c_int32), ("p", ctypes.c_uint16),
                ("b", ctypes.c_uint16), ("c", ctypes.c_uint32), ("s", ctypes.c_uint32), ("x", ctypes.c_int32),
                ("y", ctypes.c_int32), ("u", ctypes.c_uint32), ("i", ctypes.c_uint32)]


def shot(hwnd, name):
    a.root.update()                       # дорисовать всё перед снимком
    time.sleep(0.3)
    a.root.update()
    if not locked():                      # экран не заблокирован — обычный снимок экрана точнее
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save(OUT / f"{name}.png")
        print("снимок:", name, r.right - r.left, r.bottom - r.top, flush=True)
        return
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = u.GetWindowDC(hwnd)
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mdc, bmp)
    u.PrintWindow(hwnd, mdc, 2)
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(BIH(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)), 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(OUT / f"{name}.png")
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(hwnd, hdc)
    print("снимок:", name, w, h, flush=True)


EN = ("So the thing nobody tells you about building a company is that the first year is mostly about learning "
      "what your customers actually want. We spent almost 8 months building features that nobody used,")
RU = ("Итак, о создании компании никто не говорит вам, что первый год в основном посвящён изучению того, чего на "
      "самом деле хотят ваши клиенты. Мы потратили почти 8 месяцев на создание функций, которыми никто не "
      "пользовался, и, честно говоря, это было болезненно.")

class MemorySettings(dict):
    """Настройки только в памяти — снимки не должны менять настройки пользователя."""
    def set(self, key, value):
        self[key] = value


a = A.App(start_hidden=False)
a.settings = MemorySettings(a.settings)
# пример истории — настоящие диктовки пользователя в снимки не попадают (и не меняются)
a.history = type("DemoHistory", (), {"items": [
    {"time": "01.10 14:05", "lang": "ru", "text": "Добрый день! Высылаю отчёт по проекту, все цифры сверены. "
                                                   "Если будут вопросы — пишите, обсудим на созвоне в четверг."},
    {"time": "01.10 12:40", "lang": "en", "text": "Thanks for the update! Let's meet on Friday to discuss "
                                                   "the next steps."},
    {"time": "30.09 19:12", "lang": "ru", "text": "Купить хлеб, молоко и кофе. Записаться к врачу на понедельник."},
], "remove": lambda self, i: None, "clear": lambda self: None, "add": lambda self, *x: None})()
a.stats = type("DemoStats", (), {"today": lambda self: 1240, "words": 18650, "sessions": 214,
                                "saved_minutes": lambda self: 18650 / 40 - 18650 / 130})()
a.render_history()
a.settings.update({"overlay": True, "src": "link", "link": "https://www.youtube.com/watch?v=aircAruvnKk"})
a.link_entry.delete(0, "end")
a.link_entry.insert(0, a.settings["link"])
main = lambda: u.GetParent(a.root.winfo_id())  # noqa: E731
steps = [
    (900, lambda: (a.show_page("home"), a.refresh_controls(), a.root.lift(), a.root.attributes("-topmost", True))),
    (1500, lambda: shot(main(), "1_dictation_tab")),
    (1700, lambda: a.show_page("lecture")),
    (2300, lambda: shot(main(), "2_lectures_tab")),
    (2500, lambda: a.show_page("history")),
    (3100, lambda: shot(main(), "3_history_tab")),
    (3300, lambda: a.show_page("settings")),
    (3900, lambda: shot(main(), "4_settings_tab")),
    (4100, lambda: a.quit()),
]
for n, (ms, fn) in enumerate(steps):
    a.root.after(4000 + n * 1500, fn)          # первые секунды окно дорисовывается; между шагами — пауза
a.run()
