"""Картинки новых окошек (для проверки дизайна и инструкции): venv\\Scripts\\python tools\\overlay_shots.py <папка>"""
import ctypes
import sys
import tkinter as tk
from pathlib import Path

ctypes.windll.shcore.SetProcessDpiAwareness(1)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from overlay_ui import CompactBubble, DictationBubble, LectureBubble  # noqa: E402

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
root = tk.Tk()
root.withdraw()
scale = root.winfo_fpixels("1i") / 96


def settle(bubble, frames: int = 25):
    """Прокрутить анимацию (проявление слов, высоту окна) до конца, как это происходит на экране."""
    import time
    for _ in range(frames):
        bubble.animate()
        bubble.render()
        time.sleep(0.033)


def save(bubble, name):
    settle(bubble)
    bubble.render().save(OUT / f"{name}.png")
    print("картинка:", name, flush=True)


d = DictationBubble(root, scale)
_levels = iter([0.002, 0.004, 0.03, 0.06, 0.05, 0.08, 0.04] * 40)
d.level_source = lambda: next(_levels, 0.05)
d.set_state("rec", "ru")
d.set_ready()
d.set_text("Слушай, создай мне схему того, как вообще нужно начинать YouTube канал, сделай HTML-версию",
           "очень красивую в стиле Apple")
save(d, "7b_bubble_full")
c = CompactBubble(root, scale)
c.level_source = d.level_source
c.set_state("rec", "ru")
c.set_ready()
c.set_text("Слушай, создай мне схему того, как вообще нужно начинать YouTube канал,", "сделай HTML-версию")
save(c, "7_bubble_dictation")
d.set_state("done")
d.set_text("Добрый день! Высылаю отчёт по проекту, все цифры сверены.", "")
save(d, "7c_bubble_done")

lec = LectureBubble(root, scale, on_close=lambda: None, on_copy=lambda: None, on_open=lambda p: None)
lec.src_title, lec.lang = "Лекция по ссылке", "en"
lec.set_mode("Google")
lec.set_hint("But what is a neural network? — обработано 4:12 из 18:39   ·   Alt+X — остановить и перевести")
lec.listening("So the thing nobody tells you about building a company is that the first year is mostly about "
              "learning what your customers actually want. We spent almost 8 months building features that "
              "nobody used,", "and honestly that was painful")
save(lec, "8_bubble_lecture")
lec.done("So the thing nobody tells you about building a company is that the first year is mostly about learning "
         "what your customers actually want. We spent almost 8 months building features that nobody used.",
         "Итак, о создании компании никто не говорит вам, что первый год в основном посвящён изучению того, "
         "чего на самом деле хотят ваши клиенты. Мы потратили почти 8 месяцев на создание функций, которыми "
         "никто не пользовался.", "C:\\example.txt")
save(lec, "9_bubble_translation")
root.destroy()
