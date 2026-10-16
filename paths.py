"""Где лежат модели и данные программы.

Из исходников: модели — в models/, настройки, журнал и история — в data/ рядом с программой.
Собранный .exe: модели — внутри сборки, данные — в %LOCALAPPDATA%\\Голосовой помощник."""
import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)            # запущено как собранный .exe
if FROZEN:
    # всё нужное лежит внутри сборки, а настройки/журнал/история — в папке пользователя
    APP_DIR = Path(sys.executable).resolve().parent
    MODELS_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "models"
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Голосовой помощник"
else:
    APP_DIR = Path(__file__).resolve().parent
    MODELS_DIR = APP_DIR / "models"
    DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

os.environ["HF_HUB_OFFLINE"] = "1"                 # модели уже скачаны — в интернет за ними не ходим
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
