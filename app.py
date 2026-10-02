"""Голосовой помощник.

• Alt+Z — начать/остановить диктовку. Слова появляются вживую в маленьком окошке
  внизу экрана, а по окончании весь текст вставляется туда, где стоит курсор.
• Язык диктовки — русский или английский.
• История — последние 10 диктовок, более старые удаляются.
• Alt+X — подкаст: слушает звук ноутбука и аккуратно записывает английскую речь вживую;
  Alt+X ещё раз — стоп и перевод всего текста на русский. Перевод онлайн (Google) или офлайн.

Закрытие окна (✕) — сворачивание в трей, выход — через значок у часов.
Распознавание: GigaAM v3 (русский), NVIDIA Parakeet (английский) — всегда на ноутбуке.
"""
import ctypes
import datetime as dt
import json
import os
import queue
import re
import sys
import threading
import time
import wave
import winsound
from collections import deque
from ctypes import wintypes
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

# Без консоли (pythonw) печатать некуда — пишем журнал в файл. Вспомогательный процесс записи
# звука программы тоже импортирует этот файл — он пишет в отдельный журнал, чтобы не мешать.
import multiprocessing
if sys.stdout is None:
    _log_path = DATA_DIR / ("log.txt" if multiprocessing.parent_process() is None else "log_capture.txt")
    _mode = "a" if _log_path.exists() and _log_path.stat().st_size < 1_000_000 else "w"   # дописываем, до 1 МБ
    sys.stdout = sys.stderr = open(_log_path, _mode, encoding="utf-8", buffering=1)
    print("\n=====", dt.datetime.now().strftime("%d.%m.%Y %H:%M:%S"), "=====", flush=True)

import warnings
warnings.filterwarnings("ignore", message="data discontinuity")   # безобидно, но засоряет журнал

# Python 3.12 узнаёт сведения о системе через WMI, и если это впервые происходит не в главном
# потоке (так делает библиотека захвата звука при первом Alt+X), программа аварийно падает.
# Спрашиваем один раз здесь — ответ запоминается, и опасный запрос больше не повторяется.
import platform
platform.uname()
platform.machine()


def _log_thread_crash(args):
    import traceback
    print(dt.datetime.now().strftime("%H:%M:%S"), "!! ошибка в потоке", args.thread.name, flush=True)
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)


threading.excepthook = _log_thread_crash

# Если программа всё-таки упадёт на уровне Windows (ошибка памяти), Python запишет в журнал,
# в каком месте кода это случилось, — во всех потоках.
import faulthandler
if sys.stderr is not None:
    faulthandler.enable(file=sys.stderr, all_threads=True)

import tkinter as tk
from tkinter import font as tkfont

import customtkinter as ctk

import ctranslate2
import numpy as np
import onnx_asr
import pyperclip
import pystray
import sentencepiece as spm
import sounddevice as sd
from PIL import Image, ImageDraw

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

APP_TITLE = "Голосовой помощник"
RATE = 16000
HISTORY_MAX = 10


def spectral_filter(audio: np.ndarray, sr: int, low: float = 0, high: float = 0) -> np.ndarray:
    """Фильтр через спектр (без SciPy — так программа запускается быстрее и весит меньше).
    low — убрать всё ниже (гул, стук), high — убрать всё выше (звонкость). Края — плавные."""
    if not len(audio):
        return audio.astype(np.float32)
    spec = np.fft.rfft(audio)
    f = np.fft.rfftfreq(len(audio), 1 / sr)
    gain = np.ones_like(f)
    if low:
        gain *= np.clip((f - low * 0.5) / (low * 0.5), 0, 1)      # от low/2 до low — плавный подъём
    if high:
        gain *= np.clip((high * 1.5 - f) / (high * 0.5), 0, 1)    # от high до 1,5·high — плавный спад
    return np.fft.irfft(spec * gain, len(audio)).astype(np.float32)


def highpass(audio: np.ndarray) -> np.ndarray:
    return spectral_filter(audio, RATE, low=80)                     # убирает гул и стук

ASR_MODELS = {   # язык: (модель, квантизация)
    "ru": ("gigaam-v3-e2e-rnnt", "int8"),        # int8: в 4 раза меньше, на 20% быстрее, точность та же
    "en": ("nemo-parakeet-tdt-0.6b-v2", "int8"),
}
LANG_NAMES = {"ru": "Русский", "en": "English"}
MIC_DEFAULT = "По умолчанию (как в Windows)"

C = {   # тёмная тема в духе Apple: чистый чёрный, графитовые карточки, системный синий и зелёный
    "bg": "#000000", "surface": "#1c1c1e", "surface2": "#2c2c2e", "border": "#2c2c2e",
    "card": "#1c1c1e", "card2": "#2c2c2e",
    "text": "#f5f5f7", "muted": "#8e8e93", "accent": "#0a84ff", "accent_hover": "#0071e3",
    "accent_text": "#64a8ff", "rec": "#ff453a", "rec_hover": "#e0372d", "chip": "#2c2c2e",
    "chip_hover": "#3a3a3c", "ok": "#30d158", "blue": "#64d2ff", "blue_btn": "#5e5ce6",
    "blue_btn_hover": "#4f4dd6", "seg_sel": "#636366", "seg_sel_hover": "#6e6e73", "switch_on": "#30d158",
    "ov_bg": "#15161b", "ov_text": "#f4f4f6", "ov_old": "#a3a7b3", "ov_muted": "#6f7482",
}
UI_FONT = "Segoe UI Variable Display"            # шрифт Windows 11; если его нет — обычный Segoe UI


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ─────────────────────────── хранилище ───────────────────────────

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)


class Settings(dict):
    FILE = DATA_DIR / "settings.json"

    def __init__(self):
        super().__init__({"lang": "auto", "paste": True, "overlay": True, "tr_online": True,
                          "src": "system", "src_app": "", "duck": True, "lec_lang": "en", "link": "",
                          "mic": "", "auto_unmute": True})
        self.update(load_json(self.FILE, {}))

    def set(self, key, value):
        self[key] = value
        save_json(self.FILE, dict(self))


DRAFT_FILE = DATA_DIR / "draft.json"


def save_draft(text: str, lang: str):
    """Черновик идущей диктовки — если программа вдруг упадёт, текст восстановится при запуске."""
    try:
        save_json(DRAFT_FILE, {"text": text, "lang": lang})
    except Exception as e:
        log("!! черновик:", e)


def clear_draft():
    try:
        DRAFT_FILE.unlink(missing_ok=True)
    except Exception:
        pass


class History:
    """Последние 10 диктовок; всё, что старше, удаляется."""
    FILE = DATA_DIR / "history.json"

    def __init__(self):
        self.items: list[dict] = load_json(self.FILE, [])[:HISTORY_MAX]

    def add(self, text: str, lang: str):
        self.items.insert(0, {"time": dt.datetime.now().strftime("%d.%m %H:%M"), "lang": lang, "text": text})
        del self.items[HISTORY_MAX:]
        save_json(self.FILE, self.items)

    def remove(self, i: int):
        del self.items[i]
        save_json(self.FILE, self.items)

    def clear(self):
        self.items.clear()
        save_json(self.FILE, self.items)


# ─────────────────────────── звук ───────────────────────────

def make_chime(path: Path, notes: list[float], gap: float = 0.12, length: float = 1.1, volume: float = 0.14):
    """Тихий тёплый звук: чистые низкие тона, мягкое нарастание, долгое плавное затухание.
    Без звонких обертонов (срезаны фильтром), лёгкая расстройка даёт «воздушность»."""
    sr = 44100
    t = np.arange(int(sr * length)) / sr
    env = np.minimum(1, t / 0.025) ** 2 * np.exp(-t * 4.2)          # мягкая атака 25 мс
    out = np.zeros(int(sr * (gap * (len(notes) - 1) + length)))
    for i, f in enumerate(notes):
        tone = (np.sin(2 * np.pi * f * t)
                + 0.45 * np.sin(2 * np.pi * f * 1.004 * t + 0.7)      # чуть расстроенный двойник — тепло
                + 0.10 * np.sin(2 * np.pi * f * 2 * t) * np.exp(-t * 12))   # еле слышная «деревянная» атака
        s = int(i * gap * sr)
        out[s:s + len(t)] += tone * env * (0.85 if i else 1.0)
    out = spectral_filter(out, sr, high=1800)                      # убираем звонкость
    tail = int(sr * 0.08)
    out[-tail:] *= np.linspace(1, 0, tail)
    out = (out / np.abs(out).max() * volume * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(out.tobytes())


SOUND_ON, SOUND_OFF = DATA_DIR / "sound_on.wav", DATA_DIR / "sound_off.wav"
SOUND_POD_ON, SOUND_POD_OFF = DATA_DIR / "sound_pod_on.wav", DATA_DIR / "sound_pod_off.wav"


def make_sounds():
    make_chime(SOUND_ON, [329.63, 493.88])          # ми → си, мягко вверх
    make_chime(SOUND_OFF, [493.88, 329.63])         # вниз
    make_chime(SOUND_POD_ON, [293.66, 440.0, 587.33], gap=0.1)    # ре-ля-ре — «слушаю»
    make_chime(SOUND_POD_OFF, [440.0, 293.66])


def play(path: Path):
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0


# ─────────────────────────── текст ───────────────────────────

COMMANDS = {   # голосовые команды
    "ru": [(r",?\s*\bновый абзац\b[,.!]?\s*", "\n\n"), (r",?\s*\b(?:новая строка|с новой строки)\b[,.!]?\s*", "\n")],
    "en": [(r",?\s*\bnew paragraph\b[,.!]?\s*", "\n\n"), (r",?\s*\bnew line\b[,.!]?\s*", "\n")],
}


def apply_commands(text: str, lang: str) -> str:
    for rx, repl in COMMANDS[lang]:
        text = re.sub(rx, repl, text, flags=re.I)
    return re.sub(r"\n(\s*)(\w)", lambda m: "\n" + m.group(1) + m.group(2).upper(), text)


def join_to_previous(text: str, prev: str) -> str:
    """Кусок продолжает незаконченную фразу — не начинаем его с заглавной буквы."""
    if not prev or not text or prev.endswith("\n") or prev.rstrip()[-1:] in ".!?…":
        return text
    first = text.split(" ", 1)[0].strip(",.!?")
    if first in ("Я", "I", "I'm", "I've", "I'll", "I'd") or (len(first) > 1 and first.isupper()):
        return text
    return text[0].lower() + text[1:]


def is_filler(text: str) -> bool:
    """«А-а-а», «Ммм», «Угу», «Uh», «Hmm» без настоящих слов — не печатаем."""
    words = re.findall(r"\w+", text.lower())
    fillers = ("угу", "ага", "хм", "эм", "uh", "um", "hmm", "mm", "mhm", "uhm")
    return bool(words) and all(len(set(w)) == 1 or w in fillers for w in words)


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


# ─────────────────────────── распознавание ───────────────────────────

def tokens_to_words(tokens: list[str], times: list[float]) -> list[tuple[str, float]]:
    """Склеивает кусочки слов от модели в слова. Пробел в начале кусочка — начало нового слова.
    Важно: у моделей есть и отдельный кусочек «просто пробел» — после него тоже начинается новое
    слово (раньше он терялся, и слова слипались: «во времядиктовки», «могувернуть»)."""
    words: list[list] = []
    new_word = True
    for tok, t in zip(tokens, times):
        if tok.startswith(" ") or tok.startswith("▁"):
            new_word = True
            tok = tok.lstrip(" ▁")
        if not tok:
            continue                                  # кусочек «только пробел» — следующий начнёт слово
        if new_word or not words:
            words.append([tok, float(t)])
            new_word = False
        else:
            words[-1][0] += tok
    return [(w, t) for w, t in words]


class Recognizers:
    """Модели распознавания, загружаются при первом использовании."""

    def __init__(self, on_status):
        self.models: dict = {}
        self.ts_models: dict = {}
        self.locks = {lang: threading.Lock() for lang in ASR_MODELS}
        self.on_status = on_status

    def get(self, lang: str):
        with self.locks[lang]:
            if lang not in self.models:
                self.on_status(f"Загружаю модель ({LANG_NAMES[lang]})…")
                name, quant = ASR_MODELS[lang]
                t = time.time()
                local = MODELS_DIR / name                     # в .exe модели лежат внутри сборки
                self.models[lang] = onnx_asr.load_model(name, str(local) if local.is_dir() else None,
                                                        quantization=quant)
                self.ts_models[lang] = self.models[lang].with_timestamps()
                log(f"модель {name} загружена за {time.time() - t:.1f} с")
                self.on_status(None)
            return self.models[lang]

    def recognize_words(self, lang: str, audio: np.ndarray) -> list[tuple[str, float]]:
        """Распознаёт звук и возвращает слова с временем начала каждого (в секундах)."""
        self.get(lang)
        audio = highpass(audio)
        peak = np.percentile(np.abs(audio), 99.9) if len(audio) else 0
        if peak > 0:
            audio = audio * min(0.5 / peak, 50)       # тихий звук — усиливаем
        r = self.ts_models[lang].recognize(audio, sample_rate=RATE)
        return tokens_to_words(r.tokens or [], r.timestamps or [])


class StreamRecognizer:
    """Потоковое распознавание без потерь слов.

    Каждые ~0,8 с заново распознаём накопленный звук и засчитываем только слова,
    совпавшие в двух распознаваниях подряд. Законченные фразы отдаём наружу
    (on_commit), их звук выбрасываем по времени слов — поэтому слова никогда
    не режутся посередине. Незасчитанный «хвост» показываем вживую (on_live).
    """
    STEP = 0.8           # как часто перераспознаём, с
    LONG_PHRASE = 8      # фраза длиннее — отдаём частями по запятым
    MAX_BUFFER = 15      # столько звука без конца фразы — отдаём принудительно

    def __init__(self, asr: Recognizers, lang: str, on_commit, on_live, floor: float):
        self.asr, self.lang = asr, lang
        self.on_commit, self.on_live, self.floor = on_commit, on_live, floor
        self.buf = np.zeros(0, np.float32)
        self.decoded = 0
        self.prev: list[tuple[str, float]] = []
        self.levels: deque = deque(maxlen=300)
        self.keep_all = False                         # хранить весь звук записи (для итоговой сверки)
        self.all_audio: list = []

    def feed(self, audio: np.ndarray):
        if len(audio):
            self.buf = np.concatenate([self.buf, audio])
            self.levels.append(rms(audio))
            if self.keep_all:
                self.all_audio.append(audio)

    def _quiet(self, audio: np.ndarray) -> bool:
        noise = float(np.percentile(self.levels, 10)) * 2.5 if len(self.levels) > 20 else 0.0
        return rms(audio) < max(self.floor, noise)

    def _recognize(self, audio):
        try:
            return self.asr.recognize_words(self.lang, audio)
        except Exception as e:
            log("!! распознавание:", e)
            return []

    def _emit(self, words):
        text = " ".join(w for w, _ in words).strip()
        text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)      # «almost8» → «almost 8»
        text = re.sub(r"(\w)—", r"\1 —", text)                 # «вторник— встреча» → «вторник — встреча»
        if text and not is_filler(text):
            self.on_commit(text)

    @staticmethod
    def _norm(word: str) -> str:
        return re.sub(r"[^\w']", "", word.lower())

    def step(self):
        if len(self.buf) - self.decoded < self.STEP * RATE:
            return
        quiet = self._quiet(self.buf[self.decoded:])
        if quiet and not self.prev:                   # тишина и дописывать нечего — не тратим процессор
            self.buf = self.buf[-RATE // 2:]
            self.decoded = len(self.buf)
            return
        words = self._recognize(self.buf)
        self.decoded = len(self.buf)
        dur = len(self.buf) / RATE

        stable = 0                                    # слова, совпавшие с прошлым распознаванием
        while stable < min(len(words), len(self.prev)) and \
                self._norm(words[stable][0]) == self._norm(self.prev[stable][0]):
            stable += 1

        upto = 0
        for i in range(stable - 1, -1, -1):           # до конца последней устоявшейся фразы
            if words[i][0][-1:] in ".?!":
                upto = i + 1
                break
        if not upto and quiet and words and stable == len(words):
            upto = len(words)                         # пауза в речи — отдаём всё
        if not upto and dur > self.LONG_PHRASE:       # длинная фраза — по запятой
            for i in range(stable - 3, 2, -1):
                if words[i][0].endswith((",", ";", ":")):
                    upto = i + 1
                    break
        if not upto and dur > self.MAX_BUFFER and stable > 3:
            upto = stable - 2

        if upto:
            rest = words[upto:]
            # модель ставит время слова чуть позже его настоящего начала — режем с запасом 0,25 с,
            # но не раньше последнего засчитанного слова (иначе оно попадёт в текст дважды)
            if rest:
                cut = max(words[upto - 1][1] + 0.15, rest[0][1] - 0.25)
            else:
                cut = min(dur, words[upto - 1][1] + 0.6)
            cut = max(0.0, cut)
            self._emit(words[:upto])
            self.buf = self.buf[int(cut * RATE):]
            self.decoded = len(self.buf)
            self.prev = [(w, t - cut) for w, t in rest]
        else:
            self.prev = words
            if not words and dur > 5:                 # долго ничего не распознаётся — шум/музыка
                self.buf = self.buf[-RATE:]
                self.decoded = len(self.buf)
        self.on_live(" ".join(w for w, _ in self.prev))

    FINAL_PASS_MAX = 10      # запись не длиннее — после «стоп» распознаётся заново целиком (итоговая сверка)

    def will_final_pass(self) -> bool:
        return self.keep_all and sum(map(len, self.all_audio)) <= RATE * self.FINAL_PASS_MAX

    def finish(self):
        """Конец записи: дораспознаём всё, что осталось, целиком.
        Если запись пойдёт на итоговую сверку целиком — хвост отдельно не распознаём (лишняя работа)."""
        if self.will_final_pass():
            self.buf = np.zeros(0, np.float32)
            self.prev = []
            return
        if len(self.buf) > RATE * 0.3 and (self.prev or not self._quiet(self.buf)):
            words = self._recognize(self.buf)
            if words:
                self._emit(words)
        self.buf = np.zeros(0, np.float32)
        self.prev = []
        self.on_live("")


def run_stream(audio_q: queue.Queue, sr: StreamRecognizer, alive, skip_samples: int = 0):
    """Кормит распознаватель звуком из очереди, пока не придёт None или alive() не станет False."""
    while True:
        try:
            item = audio_q.get(timeout=0.5)
        except queue.Empty:
            if not alive():
                break
            continue
        pieces, end = [item], item is None
        while not end:
            try:
                nxt = audio_q.get_nowait()
            except queue.Empty:
                break
            pieces.append(nxt)
            end = nxt is None
        audio = np.concatenate([p for p in pieces if p is not None] or [np.zeros(0, np.float32)])
        if skip_samples:
            cut = min(skip_samples, len(audio))
            audio, skip_samples = audio[cut:], skip_samples - cut
        sr.feed(audio)
        if end or not alive():
            break
        sr.step()
    sr.finish()


# ─────────────────────────── перевод ───────────────────────────

class Translator:
    """Перевод английский → русский: онлайн (Google) или офлайн (OPUS-MT tc-big)."""
    ONLINE_URL = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ru&dt=t"
    RETRY_ONLINE_AFTER = 30      # после сбоя сети — столько секунд переводим офлайн

    def __init__(self, on_status=lambda msg: None):
        self.lock = threading.Lock()
        self.tr = None
        self.on_status = on_status
        self.online_failed_at = 0.0

    def _online(self, text: str) -> str:
        """Google Переводчик. Текст уходит в теле запроса (не в адресе)."""
        import urllib.parse
        import urllib.request
        req = urllib.request.Request(
            self.ONLINE_URL, data=urllib.parse.urlencode({"q": text}).encode(),
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=4) as r:
            res = json.loads(r.read().decode("utf-8"))
        return "".join(part[0] for part in res[0] if part[0]).strip()

    def translate(self, sentences: list[str], online: bool) -> str:
        if online and time.time() - self.online_failed_at > self.RETRY_ONLINE_AFTER:
            try:
                out = self._online(" ".join(sentences))
                if self.online_failed_at:
                    self.online_failed_at = 0.0
                    self.on_status("online")
                return out
            except Exception as e:
                log("!! онлайн-перевод не удался, перевожу офлайн:", e)
                self.online_failed_at = time.time()
                self.on_status("offline_fallback")
        return self.offline(sentences)

    def translate_long(self, text: str, online: bool) -> str:
        """Длинный текст: режем по предложениям на куски до ~1500 знаков и переводим подряд."""
        chunks, cur = [], []
        for s in split_sentences(text) or [text]:
            if cur and len(" ".join(cur + [s])) > 1500:
                chunks.append(cur)
                cur = []
            cur.append(s)
        if cur:
            chunks.append(cur)
        return " ".join(self.translate(c, online) for c in chunks).strip()

    def _load(self):
        big = MODELS_DIR / "tcbig"
        try:
            src = spm.SentencePieceProcessor(model_file=str(big / "source.spm"))
            tgt = spm.SentencePieceProcessor(model_file=str(big / "target.spm"))
            self.tr = ctranslate2.Translator(str(big), device="cpu", compute_type="int8", intra_threads=2)
            self.encode = lambda s: [">>rus<<"] + src.encode(s, out_type=str) + ["</s>"]
            self.decode = tgt.decode
            log("офлайн-перевод: OPUS-MT tc-big")
        except Exception as e:
            log("!! tc-big не загрузилась, беру запасную модель:", e)
            d = MODELS_DIR / "en_ru"
            sp = spm.SentencePieceProcessor(model_file=str(d / "sentencepiece.model"))
            self.tr = ctranslate2.Translator(str(d / "model"), device="cpu", compute_type="int8", intra_threads=2)
            self.encode = lambda s: sp.encode(s, out_type=str)
            self.decode = sp.decode

    def offline(self, sentences: list[str]) -> str:
        with self.lock:
            if self.tr is None:
                self._load()
            res = self.tr.translate_batch([self.encode(s) for s in sentences],
                                          beam_size=4, max_decoding_length=256)
        return " ".join(self.decode(r.hypotheses[0]) for r in res)


# ─────────────────────────── Windows ───────────────────────────

def paste(text: str):
    """Вставляет текст в активное окно через буфер обмена и возвращает старый буфер."""
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.05)
    kb = user32.keybd_event                         # Ctrl+V по кодам клавиш — не зависит от раскладки
    kb(0x11, 0, 0, 0); kb(0x56, 0, 0, 0); kb(0x56, 0, 2, 0); kb(0x11, 0, 2, 0)
    time.sleep(0.2)
    if old is not None:
        pyperclip.copy(old)


def foreground_is_ours() -> bool:
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value == os.getpid()


HOTKEYS = {   # имя: (виртуальный код клавиши, подпись) — всегда с Alt
    "dictation": (0x5A, "Alt+Z"),
    "podcast": (0x58, "Alt+X"),
}


def register_hotkeys(callback):
    """Системные горячие клавиши (Alt+Z, Alt+X) — работают при любой раскладке.
    callback(name) при нажатии, callback(name, error=True) — если клавиша занята."""
    names = list(HOTKEYS)

    def loop():
        for i, name in enumerate(names, start=1):
            vk, label = HOTKEYS[name]
            if not user32.RegisterHotKey(None, i, 0x0001 | 0x4000, vk):   # MOD_ALT | MOD_NOREPEAT
                log(f"!! Не удалось занять {label} — его уже использует другая программа.")
                callback(name, error=True)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312 and 1 <= msg.wParam <= len(names):     # WM_HOTKEY
                callback(names[msg.wParam - 1])

    threading.Thread(target=loop, daemon=True).start()


def microphones() -> list[str]:
    """Названия микрофонов (как их видит Windows через MME — он сам подстраивает частоту)."""
    try:
        mme = next(i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME")
        names = [d["name"] for d in sd.query_devices()
                 if d["max_input_channels"] > 0 and d["hostapi"] == mme and "Переназначение" not in d["name"]
                 and "Microsoft Sound Mapper" not in d["name"]]
        return list(dict.fromkeys(names))
    except Exception as e:
        log("!! список микрофонов:", e)
        return []


def mic_device(name: str):
    """Номер устройства по названию; None — микрофон Windows по умолчанию."""
    if not name:
        return None
    try:
        mme = next(i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME")
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == name and d["max_input_channels"] > 0 and d["hostapi"] == mme:
                return i
    except Exception:
        pass
    log(f"микрофон «{name}» не найден — беру микрофон по умолчанию")
    return None


def _mic_endpoint_volume():
    """Громкость/mute микрофона по умолчанию. ВЫЗЫВАТЬ ТОЛЬКО ИЗ ГЛАВНОГО ПОТОКА.
    Важно: интерфейс получаем через QueryInterface, а не ctypes.cast — cast создаёт второго
    «владельца» одного объекта Windows, он освобождается дважды, и программа падает (_ctypes, 0xc0000005)."""
    import comtypes
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import EDataFlow, ERole, IAudioEndpointVolume, IMMDeviceEnumerator
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    en = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    dev = en.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, ERole.eMultimedia.value)
    return dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)


def _check_main_thread(what: str) -> bool:
    if threading.current_thread() is not threading.main_thread():
        log(f"!! {what}: вызов не из главного потока — пропускаю (это могло бы уронить программу)")
        return False
    return True


def unmute_mic() -> bool:
    """Включить микрофон по умолчанию (главный поток)."""
    if not _check_main_thread("включение микрофона"):
        return False
    try:
        _mic_endpoint_volume().SetMute(False, None)
        log("микрофон был выключен — включил")
        return True
    except Exception as e:
        log("!! не удалось включить микрофон:", e)
        return False


def mic_muted() -> bool:
    """Выключен ли микрофон по умолчанию в Windows (главный поток). Если узнать не удалось — False."""
    if not _check_main_thread("проверка микрофона"):
        return False
    try:
        return bool(_mic_endpoint_volume().GetMute())
    except Exception as e:
        log("не удалось проверить mute микрофона:", e)
        return False


STARTUP_LINK = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / \
    "Startup" / "Голосовой помощник.lnk"


def autostart_enabled() -> bool:
    return STARTUP_LINK.exists()


def set_autostart(on: bool):
    """Ярлык в папке «Автозагрузка»: программа сама стартует в фоне вместе с Windows."""
    import subprocess
    if not on:
        STARTUP_LINK.unlink(missing_ok=True)
        return
    if FROZEN:
        target, args = sys.executable, "--hidden"
    else:
        target = str(APP_DIR / "venv" / "Scripts" / "pythonw.exe")
        args = f'"{APP_DIR / "launcher.pyw"}" --hidden'
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:VA_LNK); $s.TargetPath=$env:VA_TARGET; "
          "$s.Arguments=$env:VA_ARGS; $s.WorkingDirectory=$env:VA_DIR; $s.IconLocation=$env:VA_ICON; $s.Save()")
    env = dict(os.environ, VA_LNK=str(STARTUP_LINK), VA_TARGET=target, VA_ARGS=args, VA_DIR=str(APP_DIR),
               VA_ICON=str(DATA_DIR / "icon.ico"))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, creationflags=0x08000000, timeout=20)


def work_area() -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)       # SPI_GETWORKAREA — без панели задач
    return r.left, r.top, r.right, r.bottom


def dwm(hwnd, attr: int, value: int):
    try:
        v = ctypes.c_int(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def make_icon(recording: bool = False, size: int = 256) -> Image.Image:
    s = 256
    # «плитка» с диагональным градиентом, как у иконок macOS: синий → фиолетовый (запись — красный → розовый)
    c1, c2 = ((255, 69, 58), (255, 55, 95)) if recording else ((10, 132, 255), (94, 92, 230))
    yy, xx = np.mgrid[0:s, 0:s]
    t = ((xx + yy) / (2 * s))[..., None]
    grad = (np.array(c1) * (1 - t) + np.array(c2) * t).astype(np.uint8)
    tile = Image.fromarray(grad, "RGB").convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8, 8, s - 8, s - 8), radius=64, fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(tile, (0, 0), mask)
    d = ImageDraw.Draw(img)
    w = "white"
    d.rounded_rectangle((98, 44, 158, 150), radius=30, fill=w)                 # капсула микрофона
    d.arc((70, 80, 186, 182), start=0, end=180, fill=w, width=14)              # дуга-держатель
    d.line((128, 182, 128, 210), fill=w, width=14)
    d.line((92, 212, 164, 212), fill=w, width=14)
    return img.resize((size, size), Image.LANCZOS)


# ─────────────────────────── диктовка ───────────────────────────

class Dictation:
    """Микрофон → потоковое распознавание → живой текст в окошке → вставка по окончании."""

    def __init__(self, app: "App"):
        self.app = app
        self.active = False
        self.session = 0

    def start(self, lang: str):
        self.active = True
        self.session += 1
        q: queue.Queue = queue.Queue()
        threading.Thread(target=self._capture, args=(q, self.session), daemon=True).start()
        threading.Thread(target=self._process, args=(q, lang), daemon=True).start()

    def stop(self):
        self.active = False

    def _capture(self, q: queue.Queue, session: int):
        # (выключен ли микрофон, проверяется в главном потоке при нажатии Alt+Z — здесь только звук)
        heard = {"samples": 0, "peak": 0.0, "warned": False}

        def callback(data, *_):
            block = data[:, 0].copy()
            q.put(block)
            heard["samples"] += len(block)
            heard["peak"] = max(heard["peak"], float(np.abs(block).max()))
            # 2 секунды идеальной тишины — микрофон заглушён или занят другой программой
            if not heard["warned"] and heard["samples"] >= RATE * 2 and heard["peak"] < 1e-6:
                heard["warned"] = True
                log("!! микрофон молчит (идеальная тишина)")
                self.app.ui("mic_problem", "Микрофон ничего не слышит. Проверьте, что он включён "
                                           "(клавиша с микрофоном на ноутбуке) и не занят другой программой.")

        try:
            with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=int(RATE * 0.05),
                                device=mic_device(self.app.settings.get("mic", "")), callback=callback):
                while self.active and self.session == session:
                    time.sleep(0.03)
        except Exception as e:
            log("!! микрофон:", e)
            self.app.ui("error", f"Не удалось открыть микрофон: {e}")
        q.put(None)

    def _process(self, q: queue.Queue, lang: str):
        text = ""

        def on_commit(piece: str):
            nonlocal text
            piece = apply_commands(join_to_previous(piece, text), lang)
            sep = "" if not text or text.endswith("\n") else " "
            text += sep + piece
            log("»", piece)
            save_draft(text, lang)                    # автосохранение: при сбое текст не пропадёт
            self.app.ui("dict_update", text, "")

        sr = StreamRecognizer(self.app.asr, lang, on_commit,
                              lambda live: self.app.ui("dict_update", text, live), floor=1e-4)
        sr.keep_all = True
        sr.STEP = 0.5          # диктовке — обновляем чаще (куски не укорачиваем: иначе рвутся предложения)
        # первые 0,45 с — звук колокольчика из динамиков, пропускаем
        run_stream(q, sr, alive=lambda: True, skip_samples=int(RATE * 0.45))

        final = self._final_pass(lang, sr.all_audio, text.strip()) if sr.will_final_pass() else text.strip()
        clear_draft()
        if final:
            self.app.history.add(final, lang)
            if self.app.settings["paste"] and not foreground_is_ours():
                paste(final + " ")
        self.app.ui("dict_done", final)

    def _final_pass(self, lang: str, parts: list, streamed: str) -> str:
        """Итоговая сверка короткой диктовки: целиком модель видит всю фразу сразу — точнее знаки
        препинания, заглавные буквы и слова на стыках кусков. Хвост записи потоковое распознавание
        в этом случае не дораспознаёт — его распознаёт именно сверка. Если она вдруг дала заметно
        меньше слов, чем потоковое, — оставляем потоковое."""
        if not parts:
            return streamed
        audio = np.concatenate(parts)
        if rms(audio) < 1e-4:                         # тишина — распознавать нечего
            return streamed
        self.app.ui("dict_update", streamed, "")
        try:
            t = time.time()
            words = self.app.asr.recognize_words(lang, audio)
            text = " ".join(w for w, _ in words).strip()
            text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
            text = apply_commands(re.sub(r"(\w)—", r"\1 —", text), lang)
        except Exception as e:
            log("!! итоговая сверка:", e)
            return streamed
        if not text or len(text.split()) < len(streamed.split()) * 0.85:
            log("итоговая сверка короче потоковой — оставляю потоковую")
            return streamed
        if text != streamed:
            log(f"итоговая сверка ({time.time() - t:.1f} с): уточнён текст")
        return text


# ─────────────────────────── перевод подкаста ───────────────────────────

LECTURES_DIR = Path(os.path.expanduser("~")) / "Documents" / "Голосовой помощник" / "Лекции"
DOWNLOADS_DIR = DATA_DIR / "downloads"


def fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def decode_audio(source: str, options: dict | None = None):
    """Звук из файла или ссылки (PyAV) → куски np.float32 моно 16 кГц."""
    import av
    container = av.open(source, options=options or {}, timeout=20)
    resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
    try:
        for frame in container.decode(audio=0):
            for f in resampler.resample(frame):
                yield f.to_ndarray()[0].astype(np.float32)
    finally:
        container.close()


def safe_filename(name: str) -> str:
    return (re.sub(r"\s{2,}", " ", re.sub(r'[\\/:*?"<>|\r\n]+', " ", name)).strip() or "лекция")[:90]


class LectureListener:
    """Alt+X: слушать лекцию и записывать текст вживую; Alt+X ещё раз — стоп и перевод целиком.

    Источники звука:
      system — весь звук ноутбука;
      app    — только одна программа (Zoom, Edge…), остальной звук не попадает в запись;
      link   — ссылка: запись скачивается и обрабатывается быстрее реального времени,
               прямая трансляция — слушается по ходу эфира. Звук у вас при этом не играет.
    """
    DUCK_VOLUME = 0.05      # «приглушить для меня»: громкость программы на время записи

    def __init__(self, app: "App"):
        self.app = app
        self.active = False
        self.session = 0
        self.boost = 1.0        # во сколько раз приглушена программа (запись усиливаем обратно)

    def start(self, source: str, target=None, lang: str = "en"):
        self.active = True
        self.session += 1
        self.source, self.target, self.lang = source, target, lang
        self.title = {"system": "Звук ноутбука", "app": str(target[0]) if target else "",
                      "link": "Ссылка"}.get(source, "")
        threading.Thread(target=self._run, args=(self.session,), daemon=True, name="lecture").start()

    def stop(self):
        self.active = False

    def _running(self, session: int) -> bool:
        return self.active and self.session == session

    # ---------- общий ход ----------
    def _run(self, session: int):
        text = ""
        try:
            if self.source == "link":
                self.app.asr.get(self.lang)
                text = self._from_link(session)
            else:
                # звук начинаем слушать сразу; модель, если ещё не загружена, догрузится,
                # пока звук копится в очереди — начало лекции не теряется
                text = self._from_stream(session)
        except Exception as e:
            log("!! лекция:", e)
            self.app.ui("error", f"Не удалось обработать лекцию: {e}")
        self.app.ui("lecture_ended")                     # кнопки/иконки — в «остановлено»
        self._finish(text.strip())

    def _finish(self, text: str):
        if not text:
            self.app.ui("pod_done", "", "", "")
            return
        ru = ""
        if self.lang == "en":
            self.app.ui("pod_translating", text)
            try:
                ru = self.app.translator.translate_long(text, online=self.app.settings["tr_online"])
            except Exception as e:
                log("!! перевод:", e)
                ru = f"(не удалось перевести: {e})"
        path = self._save(text, ru)
        self.app.ui("pod_done", text, ru, str(path) if path else "")

    def _save(self, text: str, ru: str):
        try:
            LECTURES_DIR.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H-%M")
            path = LECTURES_DIR / f"{stamp} {safe_filename(self.title)}.txt"
            parts = [f"{self.title}", f"Записано: {dt.datetime.now():%d.%m.%Y %H:%M}", ""]
            if ru:
                parts += ["ПЕРЕВОД", "", ru, "", "", "ОРИГИНАЛ (EN)", "", text]
            else:
                parts += [text]
            path.write_text("\n".join(parts) + "\n", "utf-8")
            log("лекция сохранена:", path)
            return path
        except Exception as e:
            log("!! не удалось сохранить лекцию:", e)
            return None

    def _stream_text(self, session: int, feed_audio):
        """Живое распознавание: feed_audio(q) кладёт звук в очередь, пока идёт запись."""
        text = ""

        def on_commit(piece: str):
            nonlocal text
            log("LEC»", piece)
            text += (" " if text else "") + join_to_previous(piece, text)
            self.app.ui("pod_update", text, "")

        q: queue.Queue = queue.Queue()
        threading.Thread(target=feed_audio, args=(q, session), daemon=True, name="lecture-audio").start()
        sr = StreamRecognizer(self.app.asr, self.lang, on_commit,
                              lambda live: self.app.ui("pod_update", text, live), floor=3e-4)
        run_stream(q, sr, alive=lambda: self._running(session))
        return text

    def _from_stream(self, session: int) -> str:
        feed = self._feed_system if self.source == "system" else self._feed_app
        return self._stream_text(session, feed)

    # ---------- весь звук ноутбука ----------
    def _feed_system(self, q: queue.Queue, session: int):
        try:
            import soundcard as sc                    # при первом импорте сама готовит COM в этом потоке
        except Exception as e:
            log("!! soundcard:", e)
            self.app.ui("error", f"Не удалось захватить звук ноутбука: {e}")
            q.put(None)
            return
        ctypes.windll.ole32.CoInitializeEx(None, 0)   # а в каждом следующем потоке — это делаем мы
        failures = 0
        while self._running(session):
            try:
                speaker = sc.default_speaker()
                mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
                with mic.recorder(samplerate=48000, channels=1, blocksize=1440) as rec:
                    self.app.ui("tr_source", f"Слушаю весь звук: {speaker.name}")
                    failures, checked = 0, time.time()
                    while self._running(session):
                        data = rec.record(numframes=4800)[:, 0]            # 0,1 с
                        n = len(data) // 3 * 3
                        q.put(data[:n].reshape(-1, 3).mean(axis=1).astype(np.float32))   # 48 → 16 кГц
                        if time.time() - checked > 2:               # сменились наушники/колонки?
                            checked = time.time()
                            if sc.default_speaker().name != speaker.name:
                                log("звук переключился на", sc.default_speaker().name)
                                break
            except Exception as e:
                failures += 1
                log("!! захват звука:", e)
                if failures >= 5:
                    self.app.ui("error", f"Не удалось захватить звук ноутбука: {e}")
                    break
                self.app.ui("tr_source", "переподключаюсь…")
                time.sleep(1)
        q.put(None)

    # ---------- одна программа ----------
    def _feed_app(self, q: queue.Queue, session: int):
        """Звук одной программы. Громкость («приглушить для меня») меняет главный поток
        (App.duck_start / duck_restore) — здесь только запись, без обращений к COM."""
        from process_audio import IsolatedProcessCapture
        name, pid = self.target
        boost = self.boost                            # во сколько раз приглушили — столько и возвращаем записи
        if boost > 1:
            time.sleep(0.3)                           # Windows убавляет громкость плавно — ждём, пока закончит
        try:
            with IsolatedProcessCapture(pid) as cap:
                self.app.ui("tr_source", f"Слушаю только: {name}" + ("  ·  для вас приглушено" if boost > 1 else ""))
                while self._running(session) and cap.alive():
                    time.sleep(0.1)
                    q.put(np.clip(cap.read() * boost, -1, 1).astype(np.float32))
                if cap.lost:
                    log(f"!! потерь звука программы: {cap.lost}")
        except Exception as e:
            log("!! звук программы:", e)
            self.app.ui("error", f"Не удалось слушать {name}: {e}")
        q.put(None)

    # ---------- ссылка ----------
    def _from_link(self, session: int) -> str:
        import yt_dlp
        url = self.target
        self.app.ui("tr_source", "Получаю сведения о видео…")
        opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
        # эмодзи (🔴 и т.п.) tkinter может не показать, а в имени файла они ни к чему
        self.title = re.sub(r"[^\u0000-￿]", "", info.get("title") or url).strip(" |·-") or url
        if info.get("is_live"):
            return self._link_live(session, info)
        return self._link_recorded(session, url, info)

    def _link_live(self, session: int, info: dict) -> str:
        """Прямой эфир: слушаем поток по ходу трансляции (только звуковая дорожка — мало трафика).
        Если поток оборвался (мигнул интернет, YouTube сменил адрес) — переподключаемся сами."""
        url = info.get("webpage_url") or info.get("original_url")

        def options_for(i: dict) -> dict:
            headers = i.get("http_headers") or {}
            return {"headers": "".join(f"{k}: {v}\r\n" for k, v in headers.items())} if headers else {}

        def feed(q: queue.Queue, s: int):
            import yt_dlp
            current, failures = info, 0
            while self._running(s):
                try:
                    self.app.ui("tr_source", f"Прямой эфир: {self.title}")
                    got = False
                    for chunk in decode_audio(current["url"], options_for(current)):
                        if not self._running(s):
                            break
                        q.put(chunk)
                        if not got:
                            got, failures = True, 0
                    if self._running(s) and got:
                        log("эфир: поток закончился, проверяю, идёт ли трансляция")
                except Exception as e:
                    log("!! эфир:", e)
                if not self._running(s):
                    break
                failures += 1
                if failures > 6:
                    self.app.ui("error", "Эфир недоступен — запись остановлена")
                    break
                self.app.ui("tr_source", f"Переподключаюсь к эфиру… (попытка {failures})")
                time.sleep(min(2 * failures, 10))
                try:
                    with yt_dlp.YoutubeDL({"format": "bestaudio/best", "quiet": True, "no_warnings": True,
                                           "noplaylist": True}) as y:
                        current = y.extract_info(url, download=False)
                    if not current.get("is_live"):
                        log("эфир завершился")
                        break
                except Exception as e:
                    log("!! эфир, новый адрес:", e)
            q.put(None)

        return self._stream_text(session, feed)

    def _link_recorded(self, session: int, url: str, info: dict) -> str:
        """Запись: скачиваем звук и распознаём кусками по ~25 с, быстрее реального времени."""
        import yt_dlp
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

        def progress(d):
            if d.get("status") == "downloading" and d.get("total_bytes"):
                self.app.ui("tr_source", f"Скачиваю звук: {d['downloaded_bytes'] * 100 // d['total_bytes']}%")
            if not self._running(session):
                raise yt_dlp.utils.DownloadCancelled("остановлено")

        opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "noplaylist": True,
                "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"), "progress_hooks": [progress], "noprogress": True,
                "concurrent_fragment_downloads": 4}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
            path = y.prepare_filename(info)
        try:
            self.app.ui("tr_source", "Читаю звук…")
            audio = np.concatenate(list(decode_audio(path)))
        finally:
            try:
                os.remove(path)                       # звук больше не нужен
            except OSError:
                pass
        total = len(audio) / RATE
        log(f"лекция «{self.title}»: {fmt_time(total)}")

        text, pos, started = "", 0, time.time()
        levels = None
        while pos < len(audio) and self._running(session):
            end = min(len(audio), pos + int(RATE * 25))
            if end < len(audio):                      # режем в самом тихом месте между 18 и 30 с
                lo, hi = pos + int(RATE * 18), min(len(audio), pos + int(RATE * 30))
                win = audio[lo:hi]
                n = len(win) // 480
                levels = np.sqrt(np.mean(win[:n * 480].reshape(n, 480) ** 2, axis=1))
                levels = np.convolve(levels, np.ones(10) / 10, "same")
                end = lo + int(np.argmin(levels)) * 480 + 240
            chunk = audio[pos:end]
            if rms(chunk) > 1e-4:
                try:
                    words = self.app.asr.recognize_words(self.lang, chunk)
                except Exception as e:
                    log("!! распознавание:", e)
                    words = []
                piece = " ".join(w for w, _ in words).strip()
                piece = re.sub(r"([A-Za-z])(\d)", r"\1 \2", piece)
                if piece and not is_filler(piece):
                    text += (" " if text else "") + join_to_previous(piece, text)
            pos = end
            spent = time.time() - started
            left = spent / max(pos, 1) * (len(audio) - pos)
            self.app.ui("tr_source", f"{self.title} — обработано {fmt_time(pos / RATE)} из {fmt_time(total)}"
                                     f"  ·  осталось ≈ {fmt_time(left)}")
            self.app.ui("pod_update", text, "")
        return text


# ─────────────────────────── всплывающие окошки ───────────────────────────

class Floating:
    """Окошко поверх всех окон: без рамки, скруглённое, не забирает фокус (текст
    продолжает печататься туда, где был курсор). Можно перетаскивать мышкой."""

    def __init__(self, root: tk.Tk, width: int):
        self.root = root
        self.width = width
        self.win = w = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)                # непрозрачное: полупрозрачность даёт артефакты
        w.configure(bg=C["ov_bg"])
        w.update_idletasks()
        self.hwnd = user32.GetParent(w.winfo_id())
        ex = user32.GetWindowLongW(self.hwnd, -20)
        user32.SetWindowLongW(self.hwnd, -20, ex | 0x08000000 | 0x80 | 0x8)   # NOACTIVATE | TOOLWINDOW | TOPMOST
        dwm(self.hwnd, 33, 2)                       # скруглённые углы Windows 11
        dwm(self.hwnd, 34, 0x00372D2A)              # тонкая рамка в цвет темы (#2a2d37, формат BGR)
        self.pos = None                             # позиция после перетаскивания
        self._drag = None
        self._hide_job = None
        self.visible = True
        self.hide()

    def draggable(self, *widgets):
        for wd in widgets:
            wd.bind("<ButtonPress-1>", self._drag_start, add="+")
            wd.bind("<B1-Motion>", self._drag_move, add="+")

    def _drag_start(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
            self.pos = (x, y)
            self.win.geometry(f"+{x}+{y}")

    def place(self, bottom_offset: int):
        """Ставим окно внизу по центру. Размер не задаём — его подбирает tkinter по содержимому
        (если задавать размер вручную, tkinter не успевает перерисовывать содержимое)."""
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        if self.pos:
            x, y = self.pos
        else:
            l, t, r, b = work_area()
            x, y = (l + r - w) // 2, b - h - bottom_offset
        self.win.geometry(f"+{x}+{y}")

    def show(self):
        # показываем средствами tkinter (иначе он не перерисовывает содержимое);
        # фокус не отбирается благодаря стилю WS_EX_NOACTIVATE
        self.cancel_hide()
        if not self.visible:
            self.win.deiconify()
            self.visible = True
        user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # поверх всех, без активации

    def hide(self):
        self.cancel_hide()
        if self.visible:
            self.win.withdraw()
            self.visible = False

    def hide_later(self, ms: int):
        self.cancel_hide()
        self._hide_job = self.win.after(ms, self.hide)

    def cancel_hide(self):
        if self._hide_job:
            self.win.after_cancel(self._hide_job)
            self._hide_job = None


def text_widget(parent, font: tkfont.Font, width_px: int, **kw) -> tk.Text:
    """Текстовое поле фиксированной ширины (в знаках) — высоту окна tkinter подбирает сам."""
    opts = dict(bg=C["ov_bg"], fg=C["ov_text"], font=font, wrap="word", bd=0, highlightthickness=0,
                height=1, width=max(10, width_px // max(1, font.measure("0"))), padx=0, pady=0,
                cursor="arrow", insertwidth=0, spacing1=1, spacing3=1)
    opts.update(kw)
    t = tk.Text(parent, **opts)
    t.configure(state="disabled")
    return t


def display_lines(t: tk.Text) -> int:
    t.update_idletasks()
    n = t.count("1.0", "end", "update", "displaylines")    # update — досчитать переносы строк сейчас же
    n = n[0] if isinstance(n, tuple) else (n or 1)
    return max(1, n)


class DictationPill(Floating):
    """Маленькая тёмная «таблетка» внизу экрана: точка записи, ваши слова, кнопка стоп."""
    MAX_LINES = 6

    def __init__(self, root, scale, fonts, on_stop, on_fix=None):
        super().__init__(root, int(560 * scale))
        box = tk.Frame(self.win, bg=C["ov_bg"], padx=int(14 * scale), pady=int(10 * scale))
        box.pack(fill="both", expand=True)
        d = int(12 * scale)
        self.dot = tk.Canvas(box, width=d, height=d, bg=C["ov_bg"], highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, d - 1, d - 1, fill=C["rec"], outline="")
        self.dot.pack(side="left", anchor="n", pady=(int(6 * scale), 0), padx=(0, int(10 * scale)))
        right = tk.Frame(box, bg=C["ov_bg"])
        right.pack(side="right", anchor="n", padx=(int(10 * scale), 0))
        self.lang = tk.Label(right, text="RU", bg=C["chip"], fg=C["ov_old"], font=fonts["tiny"],
                             padx=int(6 * scale), pady=1)
        self.lang.pack(side="left", pady=(int(3 * scale), 0))
        self.stop = tk.Label(right, text="■", bg=C["ov_bg"], fg=C["rec"], font=fonts["norm"], cursor="hand2",
                             padx=int(8 * scale))
        self.stop.pack(side="left")
        self.stop.bind("<Button-1>", lambda e: on_stop())
        # кнопка «Включить» — появляется, только когда микрофон выключен в Windows
        self.fix = tk.Label(right, text="", bg=C["accent"], fg="white", font=fonts["tiny_b"], cursor="hand2",
                            padx=int(8 * scale), pady=int(2 * scale))
        self.fix.bind("<Button-1>", lambda e: on_fix and on_fix())
        font, wrap = fonts["ov"], int(440 * scale)
        self.max_chars = int(wrap / font.measure("о") * 0.92) * self.MAX_LINES   # примерно 6 строк
        self.label = tk.Label(box, text="", bg=C["ov_bg"], fg=C["ov_text"], font=font, justify="left",
                              anchor="nw", wraplength=wrap, width=max(10, wrap // font.measure("0")))
        self.label.pack(side="left", anchor="n")
        self.draggable(box, self.dot, self.label)
        self.state = "idle"
        self._pulse_on = True
        self._pulse()

    def _pulse(self):
        if self.state == "rec":
            self._pulse_on = not self._pulse_on
            self.dot.itemconfigure(self.dot_id, fill=C["rec"] if self._pulse_on else "#7f1d1d")
        self.win.after(550, self._pulse)

    def set_state(self, state: str, lang: str = ""):
        self.state = state
        colors = {"rec": C["rec"], "busy": C["blue"], "done": C["ok"]}
        self.dot.itemconfigure(self.dot_id, fill=colors.get(state, C["ov_muted"]))
        self.stop.configure(text="■" if state == "rec" else "")
        if lang:
            self.lang.configure(text=lang.upper())

    def set_text(self, committed: str, live: str = "", placeholder: str = ""):
        text = (committed + (" " if committed and live and not committed.endswith("\n") else "") + live).strip()
        if not text:
            self.label.configure(text=placeholder, fg=C["ov_muted"])
            return
        if len(text) > self.max_chars:                      # длинный текст — показываем последние строки
            text = "…" + text[-self.max_chars:].split(" ", 1)[-1]
        self.label.configure(text=text, fg=C["ov_text"])

    def relayout(self, bottom_offset: int):
        self.place(bottom_offset)


class PodcastPanel(Floating):
    """Окно подкаста. Пока слушаем — английский текст вживую; после остановки —
    перевод всего текста и оригинал под ним. Остаётся на экране, пока не закроете ✕."""

    def __init__(self, root, scale, fonts, on_close, on_copy):
        super().__init__(root, int(660 * scale))
        s = scale
        head = tk.Frame(self.win, bg=C["ov_bg"], padx=int(16 * s), pady=int(9 * s))
        head.pack(fill="x")
        self.title = tk.Label(head, text="", bg=C["ov_bg"], fg=C["blue"], font=fonts["tiny_b"])
        self.title.pack(side="left")
        self.mode = tk.Label(head, text="", bg=C["ov_bg"], fg=C["ov_muted"], font=fonts["tiny"])
        self.mode.pack(side="left", padx=(int(8 * s), 0))
        close = tk.Label(head, text="✕", bg=C["ov_bg"], fg=C["ov_muted"], font=fonts["norm"], cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: on_close())
        self.copy = tk.Label(head, text="", bg=C["ov_bg"], fg=C["accent"], font=fonts["tiny_b"], cursor="hand2")
        self.copy.pack(side="right", padx=(0, int(14 * s)))
        self.copy.bind("<Button-1>", lambda e: self.copy["text"] and on_copy())
        self.open = tk.Label(head, text="", bg=C["ov_bg"], fg=C["accent"], font=fonts["tiny_b"], cursor="hand2")
        self.open.pack(side="right", padx=(0, int(14 * s)))
        self.open.bind("<Button-1>", lambda e: self.path and os.startfile(self.path))
        body = tk.Frame(self.win, bg=C["ov_bg"], padx=int(16 * s))
        body.pack(fill="both", expand=True, pady=(0, int(8 * s)))
        self.text = text_widget(body, fonts["ov_big"], int(620 * s), spacing3=int(4 * s))
        self.text.tag_configure("main", foreground=C["ov_text"])
        self.text.tag_configure("live", foreground=C["ov_old"])
        self.text.tag_configure("label", foreground=C["ov_muted"], font=fonts["tiny_b"], spacing1=int(10 * s))
        self.text.tag_configure("orig", foreground=C["ov_old"], font=fonts["ov_small_n"])
        self.text.tag_configure("ph", foreground=C["ov_muted"])
        self.text.pack(fill="both", expand=True)
        self.hint = tk.Label(self.win, text="", bg=C["ov_bg"], fg=C["ov_muted"], font=fonts["tiny"],
                             anchor="w", padx=int(16 * s), pady=int(0))
        self.hint.pack(fill="x", pady=(0, int(10 * s)))
        self.draggable(head, self.title)
        self.state = "idle"
        self.follow = True
        self.max_lines = 8
        self.ru = ""
        self.path = ""
        self.src_title = "Слушаю"
        self.lang = "en"

    def set_mode(self, text: str):
        self.mode.configure(text=text)

    def _fill(self, parts: list[tuple[str, str]]):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for text, tag in parts:
            t.insert("end", text, tag)
        t.configure(state="disabled")

    def listening(self, en: str, live: str):
        self.state, self.max_lines = "listening", 8
        self.title.configure(text=f"●  {self.src_title}  ·  {self.lang.upper()}", fg=C["rec"])
        self.copy.configure(text="")
        self.open.configure(text="")
        if en or live:
            self._fill([(en, "main"), ((" " if en and live else "") + live, "live")])
        else:
            self._fill([("Жду речь — слова появятся здесь…", "ph")])

    def translating(self, en: str):
        self.state = "translating"
        self.title.configure(text="◌  Перевожу…" if self.lang == "en" else "◌  Сохраняю…", fg=C["blue"])
        self.hint.configure(text="")
        self._fill([(en, "live")])

    def done(self, en: str, ru: str, path: str = ""):
        self.state, self.max_lines = "done", 12
        self.follow = False
        self.ru = ru or en                                # что копировать
        self.path = path
        if ru:
            self.title.configure(text="✓  Перевод  EN → RU", fg=C["ok"])
            self.copy.configure(text="Копировать перевод")
            self._fill([(ru, "main"), ("\nОРИГИНАЛ\n", "label"), (en, "orig")])
        else:
            self.title.configure(text="✓  Текст лекции", fg=C["ok"])
            self.copy.configure(text="Копировать текст")
            self._fill([(en, "main")])
        self.open.configure(text="Открыть файл" if path else "")
        self.hint.configure(text="Сохранено в «Документы → Голосовой помощник → Лекции»   ·   Alt+X — снова"
                            if path else "Alt+X — слушать снова")
        self.text.yview_moveto(0)

    def relayout(self, bottom_offset: int):
        n = display_lines(self.text)
        self.text.configure(height=min(max(n, 2), self.max_lines))
        if self.follow:
            self.text.see("end")
        self.place(bottom_offset)


# ─────────────────────────── главное окно ───────────────────────────

class HoverButton(tk.Label):
    """Плоская кнопка с подсветкой при наведении."""

    def __init__(self, parent, text, command, bg, hover, fg="white", **kw):
        super().__init__(parent, text=text, bg=bg, fg=fg, cursor="hand2", **kw)
        self.colors = [bg, hover]
        self.command = command
        self.bind("<Enter>", lambda e: self.configure(bg=self.colors[1]))
        self.bind("<Leave>", lambda e: self.configure(bg=self.colors[0]))
        self.bind("<Button-1>", lambda e: self.command())

    def set_colors(self, bg, hover, fg=None):
        self.colors = [bg, hover]
        self.configure(bg=bg, **({"fg": fg} if fg else {}))


class App:
    def __init__(self, start_hidden: bool):
        self.settings = Settings()
        self.history = History()
        draft = load_json(DRAFT_FILE, None)           # программа упала посреди диктовки — спасаем текст
        self.recovered = bool(draft and draft.get("text", "").strip())
        if self.recovered:
            self.history.add("(восстановлено после сбоя) " + draft["text"].strip(), draft.get("lang", "ru"))
            log("восстановил прерванную диктовку из черновика")
        clear_draft()
        self.uiq: queue.Queue = queue.Queue()
        self.recording = False
        self.translating = False
        self.mic_warning = False
        self.loading_msg = None
        self.tr_note = ""

        make_sounds()

        ctk.set_appearance_mode("dark")
        self.root = root = ctk.CTk()
        self.scale = root.winfo_fpixels("1i") / 96
        root.title(APP_TITLE)
        root.configure(fg_color=C["bg"])
        root.geometry("540x680")                 # CustomTkinter сам учитывает масштаб экрана
        root.minsize(460, 560)
        ico = DATA_DIR / "icon.ico"
        make_icon().save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        root.iconbitmap(default=str(ico))
        root.after(300, lambda: root.iconbitmap(str(ico)))   # CustomTkinter ставит свою иконку — меняем обратно
        root.protocol("WM_DELETE_WINDOW", self.hide_window)
        f = "Segoe UI"
        self.fonts = {
            "h1": tkfont.Font(family=f + " Semibold", size=16),
            "big": tkfont.Font(family=f + " Semibold", size=12),
            "norm": tkfont.Font(family=f, size=10),
            "bold": tkfont.Font(family=f + " Semibold", size=10),
            "small": tkfont.Font(family=f, size=9),
            "tiny": tkfont.Font(family=f, size=8),
            "tiny_b": tkfont.Font(family=f + " Semibold", size=8),
            "ov": tkfont.Font(family=f, size=12),
            "ov_big": tkfont.Font(family=f, size=13),
            "ov_small": tkfont.Font(family=f, size=10, slant="italic"),
            "ov_small_n": tkfont.Font(family=f, size=10),
        }
        self._build()
        root.update_idletasks()
        dwm(user32.GetParent(root.winfo_id()), 20, 1)          # тёмный заголовок окна

        from overlay_ui import DictationBubble, LectureBubble
        self.pill = DictationBubble(root, self.scale)
        self.pod_closed = False
        self.panel = LectureBubble(root, self.scale, on_close=lambda: self.ui("close_panel"),
                                   on_copy=lambda: self.ui("copy_translation"), on_open=os.startfile)
        root.bind_all("<MouseWheel>", self._wheel, add="+")   # «+» — не мешаем прокрутке истории

        self.asr = Recognizers(lambda msg: self.ui("loading", msg))
        self.translator = Translator(lambda msg: self.ui("tr_note", msg))
        self.dictation = Dictation(self)
        self.podcast = LectureListener(self)

        self.tray = pystray.Icon("voice_assistant", make_icon(size=64), APP_TITLE, menu=pystray.Menu(
            pystray.MenuItem("Открыть", lambda *_: self.ui("show"), default=True),
            pystray.MenuItem("Запись (Alt+Z)", lambda *_: self.ui("toggle")),
            pystray.MenuItem("Подкаст: слушать / перевести (Alt+X)", lambda *_: self.ui("toggle_tr")),
            pystray.MenuItem("Выход", lambda *_: self.ui("quit")),
        ))
        self.tray.run_detached()
        self.refresh_controls()
        register_hotkeys(lambda name, error=False: self.ui(
            "hotkey_error" if error else ("toggle" if name == "dictation" else "toggle_tr"), name))
        for lang in (("ru", "en") if self.settings["lang"] == "auto" else (self.settings["lang"],)):
            threading.Thread(target=lambda lang=lang: self.asr.get(lang), daemon=True).start()
        self.root.after(3000, self._restore_ducked)
        if self.recovered:
            self.root.after(2500, lambda: self.tray.notify(
                "Прошлая диктовка оборвалась из-за сбоя — текст сохранён во вкладке «История».", APP_TITLE))

        if start_hidden:
            root.withdraw()
        self.hidden_hint_shown = start_hidden
        root.after(50, self._poll)

    def px(self, n: float) -> int:
        return int(n * self.scale)

    # ---------- построение окна (CustomTkinter) ----------
    def _font(self, size: int, bold: bool = False):
        return ctk.CTkFont(family=UI_FONT + " Semib" if bold else UI_FONT, size=size)   # полужирный, как у Apple

    def _seg(self, parent, options: list[tuple], command):
        """Сегментная кнопка: [(значение, подпись)…] → command(значение)."""
        labels = {v: t for v, t in options}
        back = {t: v for v, t in options}
        seg = ctk.CTkSegmentedButton(
            parent, values=[t for _, t in options], command=lambda t: command(back[t]),
            font=self._font(13, True), height=34, corner_radius=10,
            fg_color=C["surface2"], unselected_color=C["surface2"], unselected_hover_color=C["chip_hover"],
            selected_color=C["seg_sel"], selected_hover_color=C["seg_sel_hover"], text_color=C["text"])
        seg.labels = labels
        return seg

    def _label(self, parent, text, size=13, color=None, bold=False, wrap=0, **kw):
        return ctk.CTkLabel(parent, text=text, font=self._font(size, bold), text_color=color or C["text"],
                            anchor="w", justify="left", wraplength=wrap, **kw)

    def _section(self, parent, title: str, hint: str = "") -> ctk.CTkFrame:
        box = ctk.CTkFrame(parent, fg_color=C["surface"], corner_radius=18, border_width=0)
        box.pack(fill="x", pady=(0, 10))
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=14)
        self._label(inner, title, 14, bold=True).pack(fill="x")
        if hint:
            self._label(inner, hint, 12, C["muted"], wrap=420).pack(fill="x", pady=(2, 0))
        body = ctk.CTkFrame(inner, fg_color="transparent")
        body.pack(fill="x", pady=(10, 0))
        return body

    def _switch(self, parent, text: str, key: str) -> ctk.CTkSwitch:
        sw = ctk.CTkSwitch(parent, text=text, font=self._font(13), text_color=C["text"],
                           progress_color=C["switch_on"], button_color="#ffffff", button_hover_color="#f2f2f7",
                           fg_color=C["chip_hover"], command=lambda: self._flip(key), switch_width=40,
                           switch_height=22)
        sw.pack(fill="x", pady=4)
        sw.key = key
        return sw

    def _build(self):
        r = self.root
        outer = ctk.CTkFrame(r, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=20, pady=(16, 14))

        # — шапка —
        head = ctk.CTkFrame(outer, fg_color="transparent")
        head.pack(fill="x")
        self._logo = ctk.CTkImage(make_icon(size=96), size=(38, 38))
        ctk.CTkLabel(head, image=self._logo, text="").pack(side="left", padx=(0, 12))
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left")
        self._label(titles, APP_TITLE, 20, bold=True).pack(anchor="w")
        self._label(titles, "Диктовка, лекции и перевод", 12, C["muted"]).pack(anchor="w")
        self.status = ctk.CTkLabel(head, text="", font=self._font(12, True), corner_radius=10, height=28,
                                   fg_color=C["surface2"], text_color=C["ok"], padx=12)
        self.status.pack(side="right")

        # — главная кнопка —
        self.rec_btn = ctk.CTkButton(outer, text="", command=self.toggle_record, height=58, corner_radius=16,
                                     font=self._font(17, True), fg_color=C["accent"],
                                     hover_color=C["accent_hover"])
        self.rec_btn.pack(fill="x", pady=(16, 12))

        # — вкладки —
        tabs = ctk.CTkTabview(outer, fg_color=C["bg"], corner_radius=14,
                              segmented_button_fg_color=C["surface"],
                              segmented_button_selected_color=C["seg_sel"],
                              segmented_button_selected_hover_color=C["seg_sel_hover"],
                              segmented_button_unselected_color=C["surface"],
                              segmented_button_unselected_hover_color=C["surface2"],
                              text_color=C["text"], anchor="w")
        tabs.pack(fill="both", expand=True)
        tabs._segmented_button.configure(font=self._font(13, True), height=34)
        t_dict, t_lec, t_hist = tabs.add("Диктовка"), tabs.add("Лекции"), tabs.add("История")
        self.tabs = tabs
        # содержимое вкладок прокручивается колесом мыши, если не помещается по высоте
        scroll = dict(fg_color="transparent", scrollbar_button_color=C["surface2"],
                      scrollbar_button_hover_color=C["chip_hover"])
        t_dict = ctk.CTkScrollableFrame(t_dict, **scroll)
        t_dict.pack(fill="both", expand=True)
        t_lec = ctk.CTkScrollableFrame(t_lec, **scroll)
        t_lec.pack(fill="both", expand=True)

        # ── Диктовка ──
        s = self._section(t_dict, "Язык диктовки")
        self.lang_seg = self._seg(s, [("auto", "Авто · по раскладке"), ("ru", "Русский"), ("en", "English")],
                                  self.set_lang)
        self.lang_seg.pack(fill="x")
        s = self._section(t_dict, "Микрофон", "Если есть гарнитура — её микрофон обычно слышит чище встроенного.")
        row = ctk.CTkFrame(s, fg_color="transparent")
        row.pack(fill="x")
        self.mic_menu = ctk.CTkOptionMenu(row, values=[MIC_DEFAULT], command=self._pick_mic, height=34,
                                          corner_radius=10, font=self._font(13), fg_color=C["surface2"],
                                          button_color=C["chip_hover"], button_hover_color=C["accent"],
                                          dropdown_fg_color=C["surface2"], dropdown_hover_color=C["accent"],
                                          dropdown_font=self._font(13), text_color=C["text"])
        self.mic_menu.set(self.settings["mic"] or MIC_DEFAULT)
        self.mic_menu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="↻", width=40, height=34, corner_radius=10, font=self._font(16, True),
                      fg_color=C["surface2"], hover_color=C["chip_hover"],
                      command=self.refresh_mics).pack(side="left", padx=(8, 0))
        self.refresh_mics()
        s = self._section(t_dict, "Настройки")
        self.sw_paste = self._switch(s, "Вставлять текст туда, где курсор", "paste")
        self.sw_overlay = self._switch(s, "Показывать окошко с текстом", "overlay")
        self.settings["autostart"] = autostart_enabled()
        self.sw_autostart = self._switch(s, "Запускать вместе с Windows", "autostart")
        self.sw_unmute = self._switch(s, "Включать микрофон, если он выключен", "auto_unmute")
        s = self._section(t_dict, "Как пользоваться")
        for line in ("Alt+Z — начать, ещё раз Alt+Z — вставить текст туда, где курсор.",
                     "Голосом: «новая строка», «новый абзац» · «new line», «new paragraph».",
                     "Закрытие окна не выключает программу — она ждёт у часов."):
            self._label(s, "•  " + line, 12, C["muted"], wrap=420).pack(fill="x", pady=1)

        # ── Лекции ──
        s = self._section(t_lec, "Откуда слушать")
        self.src_seg = self._seg(s, [("system", "Весь звук"), ("app", "Программа"), ("link", "Ссылка")],
                                 self.set_source)
        self.src_seg.pack(fill="x")
        self.src_box = ctk.CTkFrame(s, fg_color="transparent")
        self.src_box.pack(fill="x", pady=(10, 0))
        # весь звук
        self.sys_box = ctk.CTkFrame(self.src_box, fg_color="transparent")
        self._label(self.sys_box, "Слушает всё, что играет на ноутбуке. Чтобы параллельно смотреть своё "
                    "видео — выберите «Программа» или «Ссылка».", 12, C["muted"], wrap=420).pack(fill="x")
        # одна программа
        self.app_box = ctk.CTkFrame(self.src_box, fg_color="transparent")
        row = ctk.CTkFrame(self.app_box, fg_color="transparent")
        row.pack(fill="x")
        self.app_menu = ctk.CTkOptionMenu(row, values=["нажмите ↻"], command=self._pick_app, height=34,
                                          corner_radius=10, font=self._font(13), fg_color=C["surface2"],
                                          button_color=C["chip_hover"], button_hover_color=C["accent"],
                                          dropdown_fg_color=C["surface2"], dropdown_hover_color=C["accent"],
                                          dropdown_font=self._font(13), text_color=C["text"])
        self.app_menu.set(self.settings["src_app"] or "выберите программу")
        self.app_menu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="↻", width=40, height=34, corner_radius=10, font=self._font(16, True),
                      fg_color=C["surface2"], hover_color=C["chip_hover"],
                      command=self.refresh_programs).pack(side="left", padx=(8, 0))
        self.sw_duck = self._switch(self.app_box, "Приглушить её для меня (запись не страдает)", "duck")
        self._label(self.app_box, "В списке — программы, которые сейчас играют звук. Своё видео смотрите в "
                    "другой программе: например, лекция в Edge, видео в Firefox.", 12, C["muted"],
                    wrap=420).pack(fill="x")
        # ссылка
        self.link_box = ctk.CTkFrame(self.src_box, fg_color="transparent")
        row = ctk.CTkFrame(self.link_box, fg_color="transparent")
        row.pack(fill="x")
        self.link_entry = ctk.CTkEntry(row, placeholder_text="Ссылка на YouTube, VK Видео, Twitch…", height=36,
                                       corner_radius=10, font=self._font(13), fg_color=C["surface2"],
                                       border_color=C["border"], text_color=C["text"])
        if self.settings["link"]:
            self.link_entry.insert(0, self.settings["link"])
        self.link_entry.pack(side="left", fill="x", expand=True)
        # Ctrl+V при русской раскладке tkinter не понимает — ловим по коду клавиши
        self.link_entry._entry.bind("<Control-KeyPress>", lambda e: self.link_entry._entry.event_generate(
            "<<Paste>>") if e.keycode == 86 and e.keysym.lower() != "v" else None)
        ctk.CTkButton(row, text="Вставить", width=90, height=36, corner_radius=10, font=self._font(13, True),
                      fg_color=C["surface2"], hover_color=C["chip_hover"],
                      command=self.paste_link).pack(side="left", padx=(8, 0))
        self._label(self.link_box, "Запись обрабатывается быстрее реального времени, прямой эфир — по ходу "
                    "трансляции. Звук у вас при этом не играет.", 12, C["muted"], wrap=420).pack(fill="x", pady=(6, 0))

        s = self._section(t_lec, "Язык лекции")
        self.lec_seg = self._seg(s, [("en", "English → перевод"), ("ru", "Русский")], self.set_lec_lang)
        self.lec_seg.pack(fill="x")
        self.tr_btn = ctk.CTkButton(t_lec, text="", command=self.toggle_translate, height=48, corner_radius=14,
                                    font=self._font(15, True), fg_color=C["surface2"], hover_color=C["chip_hover"],
                                    border_width=1, border_color=C["border"])
        self.tr_btn.pack(fill="x", pady=(0, 10))
        s = self._section(t_lec, "Перевод")
        self.mode_seg = self._seg(s, [(True, "Онлайн · Google"), (False, "Офлайн")], self.set_tr_mode)
        self.mode_seg.pack(fill="x")
        self.mode_info = self._label(s, "", 12, C["muted"], wrap=420)
        self.mode_info.pack(fill="x", pady=(8, 0))

        # ── История ──
        hh = ctk.CTkFrame(t_hist, fg_color="transparent")
        hh.pack(fill="x", pady=(0, 6))
        self._label(hh, f"Последние {HISTORY_MAX} диктовок", 14, bold=True).pack(side="left")
        ctk.CTkButton(hh, text="Очистить", width=80, height=28, corner_radius=8, font=self._font(12),
                      fg_color="transparent", hover_color=C["surface2"], text_color=C["muted"],
                      command=lambda: (self.history.clear(), self.render_history())).pack(side="right")
        self.list = ctk.CTkScrollableFrame(t_hist, fg_color="transparent", scrollbar_button_color=C["surface2"],
                                           scrollbar_button_hover_color=C["chip_hover"])
        self.list.pack(fill="both", expand=True)
        self.render_history()

    def set_status(self, text: str, color: str):
        self.status.configure(text=text, text_color=color)

    def _pick_app(self, name: str):
        self.settings.set("src_app", name)

    def _pick_mic(self, name: str):
        self.settings.set("mic", "" if name == MIC_DEFAULT else name)

    def refresh_mics(self):
        self.mic_menu.configure(values=[MIC_DEFAULT] + microphones())

    def _flip(self, key: str):
        value = not self.settings[key]
        if key == "autostart":                      # не настройка в файле, а ярлык в «Автозагрузке»
            try:
                set_autostart(value)
            except Exception as e:
                log("!! автозапуск:", e)
            self.settings[key] = autostart_enabled()
        else:
            self.settings.set(key, value)
        self.refresh_controls()

    def render_history(self):
        for w in self.list.winfo_children():
            w.destroy()
        if not self.history.items:
            self._label(self.list, "Пока пусто. Нажмите Alt+Z и скажите что-нибудь.", 13, C["muted"]).pack(
                anchor="w", pady=8)
            return
        for i, item in enumerate(self.history.items):
            card = ctk.CTkFrame(self.list, fg_color=C["surface"], corner_radius=12, border_width=1,
                                border_color=C["border"])
            card.pack(fill="x", pady=(0, 8), padx=(0, 4))
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=14, pady=(10, 0))
            ctk.CTkLabel(top, text=f" {item['lang'].upper()} ", font=self._font(11, True), corner_radius=6,
                         fg_color=C["surface2"], text_color=C["muted"], height=20).pack(side="left")
            self._label(top, f"   {item['time']}", 12, C["muted"]).pack(side="left")
            for label, fn in (("Удалить", lambda i=i: (self.history.remove(i), self.render_history())),
                              ("Копировать", lambda t=item["text"]: self.copy(t))):
                ctk.CTkButton(top, text=label, width=10, height=24, corner_radius=6, font=self._font(12, True),
                              fg_color="transparent", hover_color=C["surface2"], text_color=C["accent_text"],
                              command=fn).pack(side="right", padx=(4, 0))
            self._label(card, item["text"], 13, wrap=400).pack(fill="x", padx=14, pady=(4, 12))

    def refresh_controls(self):
        s = self.settings
        if self.recording:
            self.rec_btn.configure(text="■   Остановить и вставить     Alt+Z", fg_color=C["rec"],
                                   hover_color=C["rec_hover"])
        else:
            self.rec_btn.configure(text="●   Начать диктовку     Alt+Z", fg_color=C["accent"],
                                   hover_color=C["accent_hover"])
        for seg, value in ((self.lang_seg, s["lang"]), (self.mode_seg, s["tr_online"]),
                           (self.src_seg, s["src"]), (self.lec_seg, s["lec_lang"])):
            seg.set(seg.labels[value])
        for sw in (self.sw_paste, self.sw_overlay, self.sw_duck, self.sw_autostart, self.sw_unmute):
            sw.select() if s[sw.key] else sw.deselect()
        for box in (self.sys_box, self.app_box, self.link_box):
            box.pack_forget()
        {"system": self.sys_box, "app": self.app_box, "link": self.link_box}[s["src"]].pack(fill="x")
        action = "перевести" if s["lec_lang"] == "en" else "сохранить"
        if self.translating:
            self.tr_btn.configure(text=f"■   Остановить и {action}     Alt+X", fg_color=C["blue_btn"],
                                  hover_color=C["blue_btn_hover"], border_color=C["blue_btn"])
        else:
            start = "Обработать ссылку" if s["src"] == "link" else "Слушать лекцию"
            self.tr_btn.configure(text=f"▶   {start}     Alt+X", fg_color=C["surface2"],
                                  hover_color=C["chip_hover"], border_color=C["border"])
        self.mode_info.configure(text=self.tr_note or (
            "Живой перевод Google, нужен интернет — английский текст уходит в Google. "
            "Без интернета программа сама переводит офлайн."
            if s["tr_online"] else
            "Всё на ноутбуке, без интернета. Перевод верный по смыслу, но местами буквальный."))
        self.panel.set_mode("· Google" if s["tr_online"] else "· офлайн")
        if self.loading_msg:
            self.set_status(self.loading_msg, C["muted"])
        elif self.recording:
            self.set_status("●  идёт запись", C["rec"])
        elif self.translating:
            self.set_status("●  слушаю лекцию", C["blue"])
        else:
            self.set_status("●  готово", C["ok"])
        try:
            self.tray.icon = make_icon(self.recording, 64)
        except Exception:
            pass

    # ---------- всплывающие окошки ----------
    def layout_overlays(self):
        base = self.px(14)
        if self.pill.visible:
            self.pill.relayout(base)
        if self.panel.visible:
            above = self.pill.last_size[1] - self.px(40) if self.pill.visible else 0
            self.panel.relayout(base + max(0, above))

    def _wheel(self, e):
        try:
            top = e.widget.winfo_toplevel()
        except Exception:
            return
        step = int(-e.delta / 120)
        del top, step                                 # окна лекции прокручиваются сами (overlay_ui)

    # ---------- события ----------
    def ui(self, *msg):
        self.uiq.put(msg)

    def _poll(self):
        try:
            while True:
                self._handle(*self.uiq.get_nowait())
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _handle(self, kind, *args):
        show_ov = self.settings["overlay"]
        if kind == "toggle":
            self.toggle_record()
        elif kind == "toggle_tr":
            self.toggle_translate()
        elif kind == "show":
            self.show_window()
        elif kind == "quit":
            self.quit()
        elif kind == "loading":
            self.loading_msg = args[0]
            self.refresh_controls()
        elif kind == "dict_update":
            committed, live = args
            if not committed and not live and self.mic_warning:
                return                              # не затираем предупреждение о микрофоне
            self.pill.set_text(committed, live, "Говорите…")
            if show_ov and self.recording:
                self.pill.show()
                self.layout_overlays()
        elif kind == "dict_done":
            final = args[0]
            if not self.recording:
                self.pill.set_state("done" if final else "idle")
                self.pill.set_text(final, "", "Ничего не услышал")
                self.pill.show_all()                    # итог — сразу целиком
                if show_ov:
                    self.pill.show()
                    self.layout_overlays()
                self.pill.hide_later(2600 if final else 1500)
                self.root.after(2700, self.layout_overlays)
            self.render_history()
        elif kind == "mic_problem":                 # микрофон выключен/молчит — говорим прямо в окошке
            self.mic_warning = True
            self.pill.set_problem(args[0])
            if self.recording:
                self.pill.show()
                self.layout_overlays()
            self.set_status("Микрофон выключен", C["rec"])
        elif kind == "pod_update":                  # английский текст вживую
            if self.translating and not self.pod_closed:
                self.panel.listening(*args)
                self._show_panel()
        elif kind == "pod_translating":
            if not self.pod_closed:
                self.panel.translating(args[0])
                self._show_panel()
        elif kind == "pod_done":
            en, ru, path = args
            if self.pod_closed or self.translating:
                return
            if en:
                self.panel.done(en, ru, path)
                self._show_panel()
            else:
                self.panel.hide()
                self.layout_overlays()
        elif kind == "lecture_ended":               # запись закончилась (сама или по Alt+X)
            self.duck_restore()                     # вернуть громкость приглушённой программы
            if self.translating:
                self.translating = False
                play(SOUND_POD_OFF)
                self.refresh_controls()
        elif kind == "tr_source":
            self.tr_note = ""
            action = "перевести" if self.panel.lang == "en" else "сохранить"
            self.panel.set_hint(f"{args[0]}   ·   Alt+X — остановить и {action}")
            if self.panel.visible:
                self.layout_overlays()
        elif kind == "tr_note":
            self.tr_note = ("Нет связи с Google — временно перевожу офлайн" if args[0] == "offline_fallback" else "")
            self.refresh_controls()
        elif kind == "tr_stopped":
            self.translating = False
            self.panel.hide()
            self.refresh_controls()
        elif kind == "close_panel":
            self.close_panel()
        elif kind == "copy_translation":
            self.copy_translation()
        elif kind == "hotkey_error":
            label = HOTKEYS.get(args[0], (0, "Горячая клавиша"))[1] if args else "Горячая клавиша"
            self.set_status(f"{label} занята другой программой", C["rec"])
        elif kind == "error":
            self.tray.notify(args[0][:200], APP_TITLE)
            self.set_status("Ошибка — см. уведомление", C["rec"])

    def _show_panel(self):
        self.panel.show()
        self.layout_overlays()

    def close_panel(self):
        """✕ в окне подкаста: остановить запись (если идёт) и спрятать окно."""
        self.pod_closed = True
        if self.translating:
            self.translating = False
            self.podcast.stop()
            play(SOUND_POD_OFF)
        self.panel.hide()
        self.layout_overlays()
        self.refresh_controls()

    def copy_translation(self):
        pyperclip.copy(self.panel.ru)
        self.panel.flash_copied()

    # ---------- действия ----------
    def toggle_record(self):
        log("диктовка:", "стоп" if self.recording else f"старт ({self.settings['lang']})")
        muted = False
        if not self.recording and not self.settings.get("mic") and mic_muted():   # главный поток — безопасно
            if self.settings.get("auto_unmute", True) and unmute_mic():
                # микрофон был выключен (часто — случайно, клавишей на ноутбуке) — включаем сами
                self.tray.notify("Микрофон был выключен в Windows — я его включил.", APP_TITLE)
            else:
                muted = True
        if self.recording:
            self.recording = False
            self.dictation.stop()
            self.pill.set_state("busy")
            play(SOUND_OFF)
        else:
            self.recording = True
            self.mic_warning = False
            lang = self.dictation_lang()
            self.pill.set_state("rec", lang)
            self.pill.set_text("", "", "Говорите…")
            if self.settings["overlay"]:
                self.pill.show()
                self.layout_overlays()
            self.dictation.start(lang)
            play(SOUND_ON)
            if muted:
                self.ui("mic_problem", "Микрофон выключен в Windows — включите его клавишей с перечёркнутым "
                                       "микрофоном или в Параметры → Система → Звук → Ввод.", "muted")
        self.refresh_controls()

    def toggle_translate(self):
        """Alt+X: первое нажатие — слушать и записывать английский, второе — стоп и перевод."""
        log("подкаст:", "стоп → перевод" if self.translating else "старт")
        if self.translating:
            self.translating = False
            self.podcast.stop()
            self.panel.translating(self.panel.last_text)
            self._show_panel()
            play(SOUND_POD_OFF)
        else:
            s = self.settings
            src, target = s["src"], None
            if src == "app":
                from process_audio import audio_programs
                progs = {n: pid for n, pid in audio_programs() if pid != os.getpid()}
                name = self.app_menu.get()
                if name not in progs:
                    self.warn("Выберите программу со звуком: Источник → Программа → ↻ обновить")
                    return
                target, title = (name, progs[name]), f"Слушаю только {name}"
            elif src == "link":
                url = self.link_entry.get().strip()
                if not re.match(r"https?://", url):
                    self.warn("Вставьте ссылку на видео: Источник → Ссылка")
                    return
                s.set("link", url)
                target, title = url, "Лекция по ссылке"
            else:
                title = "Слушаю весь звук"
            self.translating = True
            self.pod_closed = False
            self.tr_note = ""
            self.panel.follow = True
            self.panel.src_title, self.panel.lang = title, s["lec_lang"]
            self.panel.set_hint("Подключаюсь…")
            self.panel.listening("", "")
            self._show_panel()
            self.podcast.boost = self.duck_start(*target) if src == "app" and s["duck"] else 1.0
            self.podcast.start(src, target, s["lec_lang"])
            play(SOUND_POD_ON)
        self.refresh_controls()

    def duck_start(self, name: str, pid: int) -> float:
        """«Приглушить для меня» (главный поток): громкость программы → 5%. Возвращает, во сколько раз
        надо усилить запись, чтобы распознаванию громкость казалась прежней."""
        from process_audio import set_app_volume
        try:
            old = set_app_volume(pid, LectureListener.DUCK_VOLUME)
        except Exception as e:
            log("!! приглушение:", e)
            return 1.0
        if not old:
            return 1.0
        self._ducked_now = (name, pid, old)
        self.settings.set("ducked", {"name": name, "volume": old})    # вернём даже после аварийного выхода
        return old / LectureListener.DUCK_VOLUME

    def duck_restore(self):
        """Вернуть громкость приглушённой программы (главный поток)."""
        rec = getattr(self, "_ducked_now", None)
        if not rec:
            return
        from process_audio import set_app_volume
        name, pid, old = rec
        try:
            if set_app_volume(pid, old) is None:              # программа уже закрыла звук — по имени
                from process_audio import restore_app_volume
                restore_app_volume(name, old)
            self.settings.set("ducked", None)
        except Exception as e:
            log("!! возврат громкости:", e)
        self._ducked_now = None

    def _restore_ducked(self):
        """Если в прошлый раз программа приглушила Zoom/браузер и аварийно закрылась — вернуть громкость.
        Это возможно, только пока та программа играет звук, поэтому пробуем раз в 30 с."""
        rec = self.settings.get("ducked")
        if not rec or self.translating:
            return
        try:
            from process_audio import restore_app_volume
            if restore_app_volume(rec["name"], rec["volume"]):
                log(f"вернул громкость {rec['name']} после аварийного выхода")
                self.settings.set("ducked", None)
                return
        except Exception as e:
            log("!! возврат громкости:", e)
        self.root.after(30000, self._restore_ducked)

    def dictation_lang(self) -> str:
        """Язык диктовки. «Авто» — по раскладке клавиатуры в окне, куда вы печатаете."""
        lang = self.settings["lang"]
        if lang != "auto":
            return lang
        try:
            thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            layout = user32.GetKeyboardLayout(thread) & 0xFFFF
            return "en" if layout in (0x0409, 0x0809, 0x1009, 0x0C09, 0x1409) else "ru"
        except Exception:
            return "ru"

    def warn(self, text: str):
        """Сообщение, если окно скрыто — уведомлением у часов."""
        self.set_status(text, C["rec"])
        if not self.root.winfo_viewable():
            self.tray.notify(text, APP_TITLE)
        self.root.after(6000, self.refresh_controls)

    def set_source(self, src: str):
        self.settings.set("src", src)
        if src == "app":
            self.refresh_programs()
        self.refresh_controls()

    def set_lec_lang(self, code: str):
        self.settings.set("lec_lang", code)
        self.refresh_controls()

    def refresh_programs(self):
        from process_audio import audio_programs
        try:
            progs = [n for n, pid in audio_programs() if pid != os.getpid()]
        except Exception as e:
            log("!! список программ:", e)
            progs = []
        self.app_menu.configure(values=progs or ["(сейчас ни одна программа не играет звук)"])
        if self.app_menu.get() not in progs:
            if progs:
                preferred = [p for p in progs if p.lower().startswith(("zoom", "teams", "msedge", "chrome"))]
                self.app_menu.set((preferred or progs)[0])
                self.settings.set("src_app", self.app_menu.get())
            else:
                self.app_menu.set("включите звук в нужной программе и нажмите ↻")

    def paste_link(self):
        try:
            self.link_entry.delete(0, "end")
            self.link_entry.insert(0, pyperclip.paste().strip())
            self.settings.set("link", self.link_entry.get())
        except Exception:
            pass

    def set_lang(self, code: str):
        self.settings.set("lang", code)
        self.refresh_controls()
        for lang in (("ru", "en") if code == "auto" else (code,)):            # заранее загрузить модели
            threading.Thread(target=lambda lang=lang: self.asr.get(lang), daemon=True).start()

    def set_tr_mode(self, online: bool):
        self.settings.set("tr_online", online)
        self.translator.online_failed_at = 0.0
        self.tr_note = ""
        self.refresh_controls()

    def copy(self, text: str):
        pyperclip.copy(text)
        self.set_status("Скопировано", C["ok"])
        self.root.after(1500, self.refresh_controls)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.render_history()

    def hide_window(self):
        self.root.withdraw()
        if not self.hidden_hint_shown:
            self.hidden_hint_shown = True
            self.tray.notify("Я работаю в фоне: Alt+Z — запись. Окно — через значок у часов, "
                             "там же «Выход».", APP_TITLE)

    def quit(self):
        self.dictation.stop()
        self.podcast.stop()
        self.duck_restore()
        try:
            self.tray.stop()
        finally:
            self.root.destroy()
            os._exit(0)

    def run(self):
        self.root.mainloop()


# ─────────────────────────── запуск ───────────────────────────

def single_instance() -> bool:
    """Если помощник уже запущен — просим его показать окно и выходим."""
    kernel32.CreateMutexW(None, False, "VoiceAssistant_Mutex")
    if kernel32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
        ev = kernel32.OpenEventW(0x0002, False, "VoiceAssistant_Show")
        if ev:
            kernel32.SetEvent(ev)
        return False
    return True


def listen_show_requests(app: App):
    ev = kernel32.CreateEventW(None, False, False, "VoiceAssistant_Show")

    def loop():
        while kernel32.WaitForSingleObject(ev, 0xFFFFFFFF) == 0:
            app.ui("show")

    threading.Thread(target=loop, daemon=True).start()


def diag_capture(pid: int, seconds: float = 12):
    """Диагностика: «Голосовой помощник.exe --diag-capture PID» — 12 с слушаем программу PID
    в отдельном процессе и пишем в журнал уровень звука и распознанный английский текст."""
    from process_audio import IsolatedProcessCapture
    log(f"диагностика: слушаю процесс {pid} {seconds:.0f} с")
    parts = []
    with IsolatedProcessCapture(pid) as cap:
        end = time.time() + seconds
        while time.time() < end and cap.alive():
            time.sleep(0.1)
            parts.append(cap.read())
    audio = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    log(f"диагностика: записано {len(audio) / RATE:.1f} с, уровень {rms(audio):.4f}, потерь {cap.lost}")
    asr = Recognizers(lambda m: None)
    log("диагностика: распознано:", " ".join(w for w, _ in asr.recognize_words("en", audio)) or "(ничего)")


def main():
    if "--diag-capture" in sys.argv:
        diag_capture(int(sys.argv[sys.argv.index("--diag-capture") + 1]))
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)            # чёткий текст на экранах с масштабом
    except Exception:
        pass
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VoiceAssistant.App")
    if not single_instance():
        return
    app = App(start_hidden="--hidden" in sys.argv)
    listen_show_requests(app)
    log("запущено")
    app.run()


if __name__ == "__main__":
    multiprocessing.freeze_support()            # для .exe: процесс записи звука запускается из него же
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        user32.MessageBoxW(None, traceback.format_exc()[-900:], APP_TITLE + " — ошибка", 0x10)
