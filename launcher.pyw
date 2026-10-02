"""Запускает «Голосовой помощник» и перезапускает его, если он аварийно упал.

Обычный выход (через «Выход» в меню) — код 0, тогда лаунчер тоже завершается.
Если программа падает чаще 5 раз за 10 минут — перестаём перезапускать, чтобы не зациклиться.
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"
PYTHONW = HERE / "venv" / "Scripts" / "pythonw.exe"
LOG = HERE / "data" / "launcher_log.txt"


def log(msg: str):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(time.strftime("%d.%m %H:%M:%S ") + msg + "\n")


crashes: list[float] = []
args = sys.argv[1:]
while True:
    code = subprocess.call([str(PYTHONW), str(APP), *args], cwd=str(HERE))
    if code == 0:
        break
    now = time.time()
    crashes = [t for t in crashes if now - t < 600] + [now]
    log(f"помощник упал (код {code:#x}), перезапуск #{len(crashes)}")
    if len(crashes) >= 5:
        log("слишком много падений подряд — перезапуск остановлен")
        break
    time.sleep(2)
    args = [a for a in args if a != "--hidden"] + ["--hidden"]    # после падения — сразу в фоне
