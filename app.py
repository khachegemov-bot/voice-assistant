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
from ctypes import wintypes
from pathlib import Path

from paths import APP_DIR, DATA_DIR, FROZEN, MODELS_DIR  # noqa: E402

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


import customtkinter as ctk

import ctranslate2
import numpy as np
import pyperclip
import pystray
import sentencepiece as spm
import sounddevice as sd
from PIL import Image, ImageDraw

from dsp import RATE, rms, spectral_filter, squeeze_pauses
from recognition import LANG_NAMES, Finalizer, Recognizers, StreamRecognizer, log, run_stream
from textproc import append_piece, is_filler, split_sentences, tidy

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

APP_TITLE = "Голосовой помощник"
APP_VERSION = "1.4.0"
HISTORY_MAX = 10


MIC_DEFAULT = "По умолчанию (как в Windows)"

C = {   # тёмная тема в духе Apple: чистый чёрный, графитовые карточки, системный синий и зелёный
    "bg": "#0b0b0d", "surface": "#1c1c1e", "surface2": "#2c2c2e", "border": "#2c2c2e",
    "card": "#1c1c1e", "card2": "#2c2c2e",
    "text": "#f5f5f7", "muted": "#8e8e93", "accent": "#0a84ff", "accent_hover": "#0071e3",
    "accent_text": "#64a8ff", "rec": "#ff453a", "rec_hover": "#e0372d", "chip": "#2c2c2e",
    "chip_hover": "#3a3a3c", "ok": "#30d158", "blue": "#64d2ff", "blue_btn": "#5e5ce6",
    "blue_btn_hover": "#4f4dd6", "seg_sel": "#636366", "seg_sel_hover": "#6e6e73", "switch_on": "#30d158",
    "side": "#141416", "line": "#232326", "nav_sel": "#2c2c2e", "nav_hover": "#1f1f22", "muted2": "#636366",
}
UI_FONT = "Segoe UI Variable Display"            # шрифт Windows 11; если его нет — обычный Segoe UI


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
                          "mic": "", "auto_unmute": True, "keep_recordings": True})
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


class Stats:
    """Сколько надиктовано: слов сегодня, всего, сколько это сэкономило времени (только на этом компьютере)."""
    FILE = DATA_DIR / "stats.json"
    TYPE_WPM, SPEAK_WPM = 40, 130            # обычная скорость печати и речи, слов в минуту

    def __init__(self):
        d = load_json(self.FILE, {})
        self.days: dict = d.get("days", {})
        self.words: int = d.get("words", 0)
        self.sessions: int = d.get("sessions", 0)

    def add(self, text: str):
        n = len(re.findall(r"\w+", text))
        if not n:
            return
        today = dt.date.today().isoformat()
        self.days[today] = self.days.get(today, 0) + n
        for old in sorted(self.days)[:-60]:      # храним два месяца
            del self.days[old]
        self.words += n
        self.sessions += 1
        try:
            save_json(self.FILE, {"days": self.days, "words": self.words, "sessions": self.sessions})
        except Exception as e:
            log("!! статистика:", e)

    def today(self) -> int:
        return self.days.get(dt.date.today().isoformat(), 0)

    def saved_minutes(self) -> float:
        return self.words / self.TYPE_WPM - self.words / self.SPEAK_WPM


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

def paste(text: str, restore: bool = True):
    """Вставляет текст в активное окно через буфер обмена и (restore) возвращает старый буфер."""
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    # Alt ещё зажат после Alt+Z — тогда Ctrl+V превратится в Ctrl+Alt+V (у части раскладок это AltGr:
    # напечатается лишний символ). Ждём, пока отпустят Alt/Shift/Win (не дольше 1,5 с)
    end = time.time() + 1.5
    while time.time() < end and any(user32.GetAsyncKeyState(k) & 0x8000 for k in (0x12, 0x10, 0x5B, 0x5C)):
        time.sleep(0.03)
    pyperclip.copy(text)
    time.sleep(0.05)
    kb = user32.keybd_event                         # Ctrl+V по кодам клавиш — не зависит от раскладки
    kb(0x11, 0, 0, 0); kb(0x56, 0, 0, 0); kb(0x56, 0, 2, 0); kb(0x11, 0, 2, 0)
    # браузеры, Telegram, Discord читают буфер не мгновенно — вернём старый буфер чуть позже,
    # иначе вместо диктовки вставится то, что было в буфере раньше
    time.sleep(0.6)
    if restore and old:                             # пустой буфер (или картинка) — не затираем
        pyperclip.copy(old)


def foreground_elevated() -> bool:
    """Активное окно запущено от имени администратора, а мы — нет. Тогда Windows не пропустит
    наши нажатия клавиш (Ctrl+V) в это окно — так устроена защита UIPI."""
    advapi32 = ctypes.windll.advapi32
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return False
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
        proc = kernel32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not proc:
            return True                                                # даже не открыть — защищённое окно
        try:
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(proc, 0x0008, ctypes.byref(token)):   # TOKEN_QUERY
                return True                                            # нет доступа — окно администратора
            try:
                elevated, size = wintypes.DWORD(), wintypes.DWORD()
                ok = advapi32.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size))
                return bool(ok and elevated.value)                     # 20 = TokenElevation
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(proc)
    except Exception:
        return False


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
    """Названия микрофонов (как их видит Windows через MME). Список каждый раз свежий — видна
    только что подключённая гарнитура."""
    from audio_input import _pa_lock, _reinit
    try:
        with _pa_lock:
            _reinit()
            mme = next(i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME")
            names = [d["name"] for d in sd.query_devices()
                     if d["max_input_channels"] > 0 and d["hostapi"] == mme and "Переназначение" not in d["name"]
                     and "Microsoft Sound Mapper" not in d["name"]]
        return list(dict.fromkeys(names))
    except Exception as e:
        log("!! список микрофонов:", e)
        return []


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

    CHIME = 0.45            # столько звучит сигнал «начали» — его не записываем

    def __init__(self, app: "App"):
        self.app = app
        self.active = False
        self.session = 0
        self.level = 0.0                    # громкость микрофона сейчас — для индикатора в окошке
        self.listen_from = 0.0

    def start(self, lang: str):
        self.active = True
        self.session += 1
        self.level = 0.0
        # Сигнал начинает звучать в момент нажатия, а микрофон открывается ~0,5 с — отсекаем звук
        # по часам (раньше отсекали 0,45 с от открытия микрофона и теряли начало речи)
        self.listen_from = time.time() + self.CHIME
        q: queue.Queue = queue.Queue()
        threading.Thread(target=self._capture, args=(q, self.session), daemon=True).start()
        threading.Thread(target=self._process, args=(q, lang), daemon=True).start()

    def stop(self):
        self.active = False

    def _capture(self, q: queue.Queue, session: int):
        # (выключен ли микрофон, проверяется в главном потоке при нажатии Alt+Z — здесь только звук)
        heard = {"samples": 0, "peak": 0.0, "warned": False}

        def callback(data, *_):
            if time.time() < self.listen_from:
                return                                # ещё звучит сигнал «начали»
            block = data[:, 0].copy()
            if not heard["samples"]:
                self.app.ui("dict_ready")             # микрофон слушает — можно говорить
            self.level = rms(block)
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
            from audio_input import MicStream
            name = self.app.settings.get("mic", "")
            with MicStream(name, lambda block: callback(block[:, None])) as mic:
                log("микрофон:", mic.mode)
                while self.active and self.session == session:
                    time.sleep(0.03)
        except Exception as e:
            log("!! микрофон:", e)
            self.app.ui("error", f"Не удалось открыть микрофон: {e}")
        q.put(None)

    def _process(self, q: queue.Queue, lang: str):
        text, live = "", ""
        sr = fin = None
        final = ""
        try:
            def on_commit(piece: str):
                # на экран — вместе с живым хвостом в on_live (раньше хвост на миг пропадал и печатался заново)
                nonlocal text
                text = append_piece(text, piece, sr.lang)
                log("»", piece)
                save_draft(text, sr.lang)             # автосохранение: при сбое текст не пропадёт

            def on_live(tail: str):
                nonlocal live
                live = tail
                self.app.ui("dict_update", text, tail)

            sr = StreamRecognizer(self.app.asr, lang, on_commit, on_live, floor=1e-4)
            sr.keep_all = True
            if self.app.vad is not None:
                sr.vad = self.app.vad.new_stream()
                sr.detect = self.app.settings.get("lang") == "auto"     # язык — по голосу
                sr.on_lang = lambda new: self.app.ui("dict_lang", new)
            sr.STEP = 0.5          # диктовке — обновляем чаще (куски не укорачиваем: иначе рвутся предложения)
            fin = Finalizer(self.app.asr, sr)
            fin.start()
            run_stream(q, sr, alive=lambda: True)
            final = self._final_pass(sr, fin, text.strip(), live)
        except Exception as e:                          # что бы ни случилось — текст не теряем
            log("!! диктовка:", e)
            import traceback
            traceback.print_exc()
            final = (text + (" " + live if live else "")).strip()
        finally:
            if fin is not None:
                fin.done.set()                          # фоновая сверка не должна работать вечно
        lang = sr.lang if sr is not None else lang
        clear_draft()
        if self.app.settings.get("keep_recordings", True) and sr is not None and sr.all_audio:
            try:                                        # последние 5 записей — для проверки качества (только локально)
                from audio_input import save_recording
                save_recording(DATA_DIR / "recordings", np.concatenate(sr.all_audio), final)
            except Exception as e:
                log("!! запись для проверки:", e)
        try:
            if final:
                self.app.history.add(final, lang)
                self.app.stats.add(final)
                if self.app.settings["paste"] and not foreground_is_ours():
                    if foreground_elevated():
                        # в окна администратора Windows может не пропустить Ctrl+V — пробуем, но текст
                        # оставляем в буфере обмена и подсказываем вставить вручную
                        paste(final + " ", restore=False)
                        log("окно от администратора или защищённое — текст оставлен в буфере обмена")
                        self.app.ui("paste_blocked")
                    else:
                        paste(final + " ")
        except Exception as e:
            log("!! вставка:", e)
        self.app.ui("dict_done", final)

    def _final_pass(self, sr: "StreamRecognizer", fin: Finalizer, streamed: str, live: str = "") -> str:
        """Итоговая сверка: запись распознаётся заново кусками по 12–20 с, разрезанными в паузах, с укороченными
        паузами (большая часть — ещё в фоне во время диктовки, см. Finalizer). Модель видит фразы целиком —
        точнее знаки препинания, заглавные буквы и слова на стыках, где потоковое распознавание резало звук.
        Если сверка вдруг дала заметно меньше слов, чем потоковое распознавание, — оставляем потоковое."""
        whole = (streamed + (" " + live if live else "")).strip()
        parts = sr.all_audio
        if not parts:
            fin.done.set()
            return whole
        audio = np.concatenate(parts)
        if rms(audio) < 1e-4 or (self.app.vad is not None and not whole and max(sr.vad_probs or [0]) < 0.5):
            fin.done.set()
            log("итоговая сверка: голоса не слышно — пропускаю")
            return whole
        self.app.ui("dict_update", streamed, live)     # живой хвост остаётся на экране, пока идёт сверка
        try:
            t = time.time()
            if sr.detect:                             # короткая фраза — язык ещё не определён
                head = audio[:Finalizer.SEG_MAX * RATE]
                lang, _ = self.app.asr.detect_lang(squeeze_pauses(head, sr.vad_probs), sr.lang)
                sr.detect = False
                if lang and lang != sr.lang:
                    log(f"язык по голосу: {LANG_NAMES[lang]}")
                    sr.lang = lang
                    self.app.ui("dict_lang", lang)
                    whole = ""                        # потоковый текст был на другом языке
            text = fin.finish()
        except Exception as e:
            log("!! итоговая сверка:", e)
            return whole
        streamed = whole
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
            text = append_piece(text, piece, commands=False)

        q: queue.Queue = queue.Queue()
        threading.Thread(target=feed_audio, args=(q, session), daemon=True, name="lecture-audio").start()
        sr = StreamRecognizer(self.app.asr, self.lang, on_commit,
                              lambda live: self.app.ui("pod_update", text, live), floor=3e-4)
        if getattr(self.app, "vad", None) is not None:
            sr.vad = self.app.vad.new_stream()          # голос, а не музыка и шумы — по звучанию
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
                piece = tidy(" ".join(w for w, _ in words).strip())
                if piece and not is_filler(piece):
                    text = append_piece(text, piece, commands=False)
            pos = end
            spent = time.time() - started
            left = spent / max(pos, 1) * (len(audio) - pos)
            self.app.ui("tr_source", f"{self.title} — обработано {fmt_time(pos / RATE)} из {fmt_time(total)}"
                                     f"  ·  осталось ≈ {fmt_time(left)}")
            self.app.ui("pod_update", text, "")
        return text


# ─────────────────────────── главное окно ───────────────────────────

class App:
    def __init__(self, start_hidden: bool):
        self.settings = Settings()
        self.history = History()
        self.stats = Stats()
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
        root.geometry("980x680")                 # CustomTkinter сам учитывает масштаб экрана
        root.minsize(860, 580)
        ico = DATA_DIR / "icon.ico"
        make_icon().save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        root.iconbitmap(default=str(ico))
        root.after(300, lambda: root.iconbitmap(str(ico)))   # CustomTkinter ставит свою иконку — меняем обратно
        root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._build()
        root.update_idletasks()
        hwnd = user32.GetParent(root.winfo_id())
        dwm(hwnd, 20, 1)                                      # тёмный заголовок окна
        r_, g_, b_ = (int(C["bg"][i:i + 2], 16) for i in (1, 3, 5))
        dwm(hwnd, 35, r_ | g_ << 8 | b_ << 16)                # заголовок — цвета окна (Windows 11), без «шва»

        from overlay_ui import DictationBubble, LectureBubble
        self.pill = DictationBubble(root, self.scale)
        self.pill.level_source = lambda: self.dictation.level
        self.pod_closed = False
        self.panel = LectureBubble(root, self.scale, on_close=lambda: self.ui("close_panel"),
                                   on_copy=lambda: self.ui("copy_translation"), on_open=os.startfile)

        self.asr = Recognizers(lambda msg: self.ui("loading", msg))
        self.vad = None
        try:
            from audio_input import SileroVAD
            vad_path = MODELS_DIR / "silero-vad" / "silero_vad.onnx"
            if vad_path.exists():
                self.vad = SileroVAD(vad_path)
        except Exception as e:
            log("!! Silero VAD не загрузился — определяю речь по громкости:", e)
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
        self.preload_models(self.settings["lang"])
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
        box.pack(fill="x", pady=(0, 12))
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=16)
        self._label(inner, title, 15, bold=True).pack(fill="x")
        if hint:
            self._label(inner, hint, 12, C["muted"], wrap=620).pack(fill="x", pady=(3, 0))
        body = ctk.CTkFrame(inner, fg_color="transparent")
        body.pack(fill="x", pady=(12, 0))
        return body

    def _switch(self, parent, text: str, key: str, hint: str = "") -> ctk.CTkSwitch:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        sw = ctk.CTkSwitch(row, text="", progress_color=C["switch_on"], button_color="#ffffff",
                           button_hover_color="#f2f2f7", fg_color=C["chip_hover"], command=lambda: self._flip(key),
                           switch_width=42, switch_height=24, width=48)
        sw.pack(side="right")
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", fill="x", expand=True)
        self._label(col, text, 13).pack(fill="x")
        if hint:
            self._label(col, hint, 11, C["muted"], wrap=520).pack(fill="x")
        sw.key = key
        return sw

    def _glyph(self, key: str, color: str, size: int = 18) -> ctk.CTkImage:
        from ui_main import ICONS, icon_font
        big = size * 4
        img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((big / 2, big / 2), ICONS[key], font=icon_font(int(big * 0.78)), fill=color,
                                 anchor="mm")
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))

    def _page(self, key: str, title: str, subtitle: str = ""):
        pg = ctk.CTkScrollableFrame(self.main, fg_color="transparent", scrollbar_button_color=C["surface2"],
                                    scrollbar_button_hover_color=C["chip_hover"])
        self.pages[key] = pg
        head = ctk.CTkFrame(pg, fg_color="transparent")
        head.pack(fill="x", pady=(0, 18))
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        title_lbl = self._label(left, title, 28, bold=True)
        title_lbl.pack(anchor="w")
        if subtitle:
            self._label(left, subtitle, 13, C["muted"]).pack(anchor="w", pady=(2, 0))
        pg.title_lbl = title_lbl
        return pg, head

    def show_page(self, key: str):
        self.page = key
        for k, pg in self.pages.items():
            pg.pack_forget()
            b = self.nav[k]
            sel = k == key
            b.configure(fg_color=C["nav_sel"] if sel else "transparent", text_color=C["text"] if sel else C["muted"],
                        image=b.icons[sel])
        self.pages[key].pack(fill="both", expand=True, padx=(26, 14), pady=(20, 12))
        if key == "home":
            self.render_home()

    def _build(self):
        from ui_main import Hero, ICONS
        r = self.root
        self.pages: dict = {}
        self.nav: dict = {}

        # ── боковое меню ──
        side = ctk.CTkFrame(r, width=236, fg_color=C["side"], corner_radius=0)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        ctk.CTkFrame(r, width=1, fg_color=C["line"], corner_radius=0).pack(side="left", fill="y")
        self.main = ctk.CTkFrame(r, fg_color=C["bg"], corner_radius=0)
        self.main.pack(side="left", fill="both", expand=True)

        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(20, 22))
        self._logo = ctk.CTkImage(make_icon(size=96), size=(36, 36))
        ctk.CTkLabel(brand, image=self._logo, text="").pack(side="left", padx=(0, 11))
        names = ctk.CTkFrame(brand, fg_color="transparent")
        names.pack(side="left")
        self._label(names, "Голосовой", 15, bold=True).pack(anchor="w")
        self._label(names, "помощник", 15, bold=True).pack(anchor="w", pady=(0, 0))

        for key, label in (("home", "Главная"), ("lecture", "Лекции"), ("history", "История"),
                           ("settings", "Настройки")):
            icons = (self._glyph(key, C["muted"]), self._glyph(key, "#ffffff"))
            b = ctk.CTkButton(side, text="  " + label, image=icons[0], compound="left", anchor="w", height=40,
                              corner_radius=10, font=self._font(14, True), fg_color="transparent",
                              hover_color=C["nav_hover"], text_color=C["muted"],
                              command=lambda k=key: self.show_page(k))
            b.icons = icons
            b.pack(fill="x", padx=12, pady=2)
            self.nav[key] = b

        bottom = ctk.CTkFrame(side, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=18, pady=18)
        keys = ctk.CTkFrame(bottom, fg_color=C["surface"], corner_radius=14)
        keys.pack(fill="x", pady=(0, 12))
        for combo, what in (("Alt + Z", "диктовка"), ("Alt + X", "лекция")):
            row = ctk.CTkFrame(keys, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(10 if combo == "Alt + Z" else 0, 10 if combo == "Alt + X" else 4))
            ctk.CTkLabel(row, text=combo, font=self._font(12, True), fg_color=C["surface2"], corner_radius=7,
                         text_color=C["text"], height=24, width=64).pack(side="left")
            self._label(row, what, 12, C["muted"]).pack(side="left", padx=(10, 0))
        self.status = ctk.CTkLabel(bottom, text="", font=self._font(12, True), anchor="w", text_color=C["ok"])
        self.status.pack(fill="x", padx=4)

        # ── Главная ──
        pg, head = self._page("home", self._greeting(), "Всё распознаётся на этом ноутбуке — без интернета")
        self.home_title = pg.title_lbl
        self.hero = Hero(pg, self.scale, bg=C["bg"], colors=("#1d4ed8", "#7c3aed"), glyph=ICONS["mic"],
                         accent="#2563eb", eyebrow="Диктовка", title="Говорите — я напишу",
                         subtitle="Нажмите Alt+Z в любом окне и говорите. Ещё раз Alt+Z — текст встанет туда, "
                                  "где курсор.", hotkey="Alt + Z", command=self.toggle_record,
                         level_source=lambda: self.dictation.level, ui_font=UI_FONT)
        self.hero.pack(fill="x")
        stats = ctk.CTkFrame(pg, fg_color="transparent")
        stats.pack(fill="x", pady=(14, 12))
        self.stat_labels = {}
        for i, (key, title) in enumerate((("today", "Слов сегодня"), ("total", "Слов всего"),
                                          ("saved", "Сэкономлено времени"))):
            stats.grid_columnconfigure(i, weight=1, uniform="stats")
            card = ctk.CTkFrame(stats, fg_color=C["surface"], corner_radius=18)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            self._label(card, title, 12, C["muted"]).pack(anchor="w", padx=18, pady=(16, 0))
            value = self._label(card, "0", 30, bold=True)
            value.pack(anchor="w", padx=18)
            sub = self._label(card, "", 12, C["muted"])
            sub.pack(anchor="w", padx=18, pady=(0, 16))
            self.stat_labels[key] = (value, sub)
        s = self._section(pg, "Язык диктовки", "«Авто» — язык определяется по голосу: говорите по-русски "
                                               "или по-английски, раскладку переключать не нужно.")
        self.lang_seg = self._seg(s, [("auto", "Авто · по голосу"), ("ru", "Русский"), ("en", "English")],
                                  self.set_lang)
        self.lang_seg.pack(fill="x")
        last = ctk.CTkFrame(pg, fg_color=C["surface"], corner_radius=18)
        last.pack(fill="x", pady=(0, 12))
        top = ctk.CTkFrame(last, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 0))
        self._label(top, "Последняя диктовка", 15, bold=True).pack(side="left")
        self.last_meta = self._label(top, "", 12, C["muted"])
        self.last_meta.pack(side="left", padx=(10, 0))
        self.last_copy = ctk.CTkButton(top, text="Копировать", width=10, height=28, corner_radius=8,
                                       font=self._font(12, True), fg_color=C["surface2"],
                                       hover_color=C["chip_hover"], text_color=C["text"],
                                       command=lambda: self.copy(self.history.items[0]["text"])
                                       if self.history.items else None)
        self.last_copy.pack(side="right")
        self.last_text = self._label(last, "", 13, wrap=640)
        self.last_text.pack(fill="x", padx=20, pady=(8, 18))

        # ── Лекции ──
        pg, head = self._page("lecture", "Лекции и подкасты",
                              "Запишу лекцию, видео или прямой эфир и переведу на русский")
        self.lec_hero = Hero(pg, self.scale, bg=C["bg"], colors=("#6d28d9", "#db2777"), glyph=ICONS["play"],
                             accent="#7c3aed", eyebrow="Лекция", title="Слушаю и перевожу", subtitle="",
                             hotkey="Alt + X", command=self.toggle_translate, level_source=lambda: 0.0,
                             ui_font=UI_FONT, bars=False)
        self.lec_hero.pack(fill="x", pady=(0, 14))
        s = self._section(pg, "Откуда слушать")
        self.src_seg = self._seg(s, [("system", "Весь звук"), ("app", "Программа"), ("link", "Ссылка")],
                                 self.set_source)
        self.src_seg.pack(fill="x")
        self.src_box = ctk.CTkFrame(s, fg_color="transparent")
        self.src_box.pack(fill="x", pady=(12, 0))
        # весь звук
        self.sys_box = ctk.CTkFrame(self.src_box, fg_color="transparent")
        self._label(self.sys_box, "Слушает всё, что играет на ноутбуке. Чтобы параллельно смотреть своё "
                    "видео — выберите «Программа» или «Ссылка».", 12, C["muted"], wrap=620).pack(fill="x")
        # одна программа
        self.app_box = ctk.CTkFrame(self.src_box, fg_color="transparent")
        row = ctk.CTkFrame(self.app_box, fg_color="transparent")
        row.pack(fill="x")
        self.app_menu = ctk.CTkOptionMenu(row, values=["нажмите ↻"], command=self._pick_app, height=36,
                                          corner_radius=10, font=self._font(13), fg_color=C["surface2"],
                                          button_color=C["chip_hover"], button_hover_color=C["accent"],
                                          dropdown_fg_color=C["surface2"], dropdown_hover_color=C["accent"],
                                          dropdown_font=self._font(13), text_color=C["text"])
        self.app_menu.set(self.settings["src_app"] or "выберите программу")
        self.app_menu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="", image=self._glyph("refresh", C["text"], 16), width=40, height=36,
                      corner_radius=10, fg_color=C["surface2"], hover_color=C["chip_hover"],
                      command=self.refresh_programs).pack(side="left", padx=(8, 0))
        self.sw_duck = self._switch(self.app_box, "Приглушить её для меня", "duck",
                                    "Вы её почти не слышите, а запись идёт в полной громкости.")
        self._label(self.app_box, "В списке — программы, которые сейчас играют звук. Своё видео смотрите в "
                    "другой программе: например, лекция в Edge, видео в Firefox.", 12, C["muted"],
                    wrap=620).pack(fill="x", pady=(4, 0))
        # ссылка
        self.link_box = ctk.CTkFrame(self.src_box, fg_color="transparent")
        row = ctk.CTkFrame(self.link_box, fg_color="transparent")
        row.pack(fill="x")
        self.link_entry = ctk.CTkEntry(row, placeholder_text="Ссылка на YouTube, VK Видео, Twitch…", height=38,
                                       corner_radius=10, font=self._font(13), fg_color=C["surface2"],
                                       border_color=C["border"], text_color=C["text"])
        if self.settings["link"]:
            self.link_entry.insert(0, self.settings["link"])
        self.link_entry.pack(side="left", fill="x", expand=True)
        # Ctrl+V при русской раскладке tkinter не понимает — ловим по коду клавиши
        self.link_entry._entry.bind("<Control-KeyPress>", lambda e: self.link_entry._entry.event_generate(
            "<<Paste>>") if e.keycode == 86 and e.keysym.lower() != "v" else None)
        ctk.CTkButton(self.link_box.winfo_children()[0], text="Вставить", width=96, height=38, corner_radius=10,
                      font=self._font(13, True), fg_color=C["surface2"], hover_color=C["chip_hover"],
                      command=self.paste_link).pack(side="left", padx=(8, 0))
        self._label(self.link_box, "Запись обрабатывается быстрее реального времени, прямой эфир — по ходу "
                    "трансляции. Звук у вас при этом не играет.", 12, C["muted"], wrap=620).pack(fill="x",
                                                                                               pady=(8, 0))
        s = self._section(pg, "Язык лекции")
        self.lec_seg = self._seg(s, [("en", "English → перевод"), ("ru", "Русский")], self.set_lec_lang)
        self.lec_seg.pack(fill="x")
        s = self._section(pg, "Перевод")
        self.mode_seg = self._seg(s, [(True, "Онлайн · Google"), (False, "Офлайн")], self.set_tr_mode)
        self.mode_seg.pack(fill="x")
        self.mode_info = self._label(s, "", 12, C["muted"], wrap=620)
        self.mode_info.pack(fill="x", pady=(10, 0))

        # ── История ──
        pg, head = self._page("history", "История", f"Последние {HISTORY_MAX} диктовок — более старые удаляются сами")
        tools = ctk.CTkFrame(head, fg_color="transparent")
        tools.pack(side="right", anchor="s")
        self.hist_search = ctk.CTkEntry(tools, placeholder_text="Поиск", width=200, height=34, corner_radius=10,
                                        font=self._font(13), fg_color=C["surface"], border_color=C["line"],
                                        text_color=C["text"])
        self.hist_search.pack(side="left")
        self.hist_search.bind("<KeyRelease>", lambda e: self.render_history())
        ctk.CTkButton(tools, text="Очистить", width=10, height=34, corner_radius=10, font=self._font(13),
                      fg_color="transparent", hover_color=C["surface"], text_color=C["muted"],
                      command=self.clear_history).pack(side="left", padx=(8, 0))
        self.list = ctk.CTkFrame(pg, fg_color="transparent")
        self.list.pack(fill="both", expand=True)

        # ── Настройки ──
        pg, head = self._page("settings", "Настройки")
        s = self._section(pg, "Микрофон", "Если есть гарнитура — её микрофон обычно слышит чище встроенного.")
        row = ctk.CTkFrame(s, fg_color="transparent")
        row.pack(fill="x")
        self.mic_menu = ctk.CTkOptionMenu(row, values=[MIC_DEFAULT], command=self._pick_mic, height=36,
                                          corner_radius=10, font=self._font(13), fg_color=C["surface2"],
                                          button_color=C["chip_hover"], button_hover_color=C["accent"],
                                          dropdown_fg_color=C["surface2"], dropdown_hover_color=C["accent"],
                                          dropdown_font=self._font(13), text_color=C["text"])
        self.mic_menu.set(self.settings["mic"] or MIC_DEFAULT)
        self.mic_menu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="", image=self._glyph("refresh", C["text"], 16), width=40, height=36,
                      corner_radius=10, fg_color=C["surface2"], hover_color=C["chip_hover"],
                      command=self.refresh_mics).pack(side="left", padx=(8, 0))
        self.refresh_mics()
        self.sw_unmute = self._switch(s, "Включать микрофон, если он выключен", "auto_unmute",
                                      "Например, если его случайно выключили клавишей на ноутбуке.")
        s = self._section(pg, "Поведение")
        self.sw_paste = self._switch(s, "Вставлять текст туда, где курсор", "paste",
                                     "Выключено — текст только сохраняется в «Истории».")
        self.sw_overlay = self._switch(s, "Показывать окошко с текстом", "overlay")
        self.settings["autostart"] = autostart_enabled()
        self.sw_autostart = self._switch(s, "Запускать вместе с Windows", "autostart",
                                         "Программа тихо ждёт у часов — Alt+Z работает сразу.")
        s = self._section(pg, "Качество распознавания")
        self.sw_keep = self._switch(s, "Хранить 5 последних записей", "keep_recordings",
                                    "Только на этом компьютере — чтобы при необходимости проверить, как "
                                    "программа слышит ваш голос.")
        ctk.CTkButton(s, text="  Открыть папку с записями", image=self._glyph("folder", C["text"], 16),
                      compound="left", height=34, corner_radius=10, font=self._font(13), fg_color=C["surface2"],
                      hover_color=C["chip_hover"], text_color=C["text"],
                      command=self.open_recordings).pack(anchor="w", pady=(8, 0))
        s = self._section(pg, "Как пользоваться")
        for line in ("Alt+Z — начать, ещё раз Alt+Z — вставить текст туда, где курсор.",
                     "Голосом: «новая строка», «новый абзац» · «new line», «new paragraph».",
                     "Alt+X — записать лекцию или подкаст, ещё раз Alt+X — перевод.",
                     "Закрытие окна не выключает программу — она ждёт у часов. Выход — через значок там же."):
            self._label(s, "•  " + line, 13, C["muted"], wrap=620).pack(fill="x", pady=2)
        s = self._section(pg, "О программе")
        self._label(s, f"Голосовой помощник {APP_VERSION}", 13).pack(fill="x")
        self._label(s, "Распознавание: GigaAM v3 (русский) и NVIDIA Parakeet (английский) — работают без "
                    "интернета. Определение речи: Silero VAD.", 12, C["muted"], wrap=620).pack(fill="x", pady=(4, 0))

        self.render_history()
        self.show_page("home")

    @staticmethod
    def _greeting() -> str:
        h = dt.datetime.now().hour
        return ("Доброй ночи" if h < 5 else "Доброе утро" if h < 12 else "Добрый день" if h < 18
                else "Добрый вечер")

    def render_home(self):
        st = self.stats
        today, total, saved = st.today(), st.words, st.saved_minutes()
        self.stat_labels["today"][0].configure(text=f"{today:,}".replace(",", " "))
        self.stat_labels["today"][1].configure(text=dt.date.today().strftime("%d.%m"))
        self.stat_labels["total"][0].configure(text=f"{total:,}".replace(",", " "))
        self.stat_labels["total"][1].configure(text=f"{st.sessions} диктовок")
        self.stat_labels["saved"][0].configure(text=f"{saved / 60:.1f} ч" if saved >= 60 else f"{saved:.0f} мин")
        self.stat_labels["saved"][1].configure(text="по сравнению с печатью")
        self.home_title.configure(text=self._greeting())
        if self.history.items:
            item = self.history.items[0]
            text = item["text"]
            self.last_meta.configure(text=f"{item['time']}  ·  {item['lang'].upper()}")
            self.last_text.configure(text=text if len(text) < 420 else text[:420].rsplit(" ", 1)[0] + "…",
                                     text_color=C["text"])
            self.last_copy.pack(side="right")
        else:
            self.last_meta.configure(text="")
            self.last_text.configure(text="Пока пусто. Нажмите Alt+Z в любом окне и скажите что-нибудь.",
                                     text_color=C["muted"])
            self.last_copy.pack_forget()

    def clear_history(self):
        self.history.clear()
        self.render_history()

    def open_recordings(self):
        folder = DATA_DIR / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

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
        query = self.hist_search.get().strip().lower() if hasattr(self, "hist_search") else ""
        items = [(i, it) for i, it in enumerate(self.history.items) if query in it["text"].lower()]
        if not items:
            msg = ("Ничего не нашлось." if query else "Пока пусто. Нажмите Alt+Z в любом окне и скажите что-нибудь.")
            self._label(self.list, msg, 13, C["muted"]).pack(anchor="w", pady=8)
        for i, item in items:
            card = ctk.CTkFrame(self.list, fg_color=C["surface"], corner_radius=16)
            card.pack(fill="x", pady=(0, 10), padx=(0, 4))
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=18, pady=(14, 0))
            ctk.CTkLabel(top, text=f" {item['lang'].upper()} ", font=self._font(11, True), corner_radius=6,
                         fg_color=C["surface2"], text_color=C["muted"], height=22).pack(side="left")
            self._label(top, f"   {item['time']}", 12, C["muted"]).pack(side="left")
            for label, fn, color in (("Удалить", lambda i=i: (self.history.remove(i), self.render_history()),
                                      C["muted"]),
                                     ("Копировать", lambda t=item["text"]: self.copy(t), C["accent_text"])):
                ctk.CTkButton(top, text=label, width=10, height=26, corner_radius=8, font=self._font(12, True),
                              fg_color="transparent", hover_color=C["surface2"], text_color=color,
                              command=fn).pack(side="right", padx=(4, 0))
            self._label(card, item["text"], 14, wrap=640).pack(fill="x", padx=18, pady=(6, 16))
        if hasattr(self, "stat_labels"):
            self.render_home()

    def refresh_controls(self):
        s = self.settings
        self.hero.set_active(self.recording)
        self.hero.set_text(status="Слушаю… Ещё раз Alt+Z — остановить и вставить текст." if self.recording else "")
        for seg, value in ((self.lang_seg, s["lang"]), (self.mode_seg, s["tr_online"]),
                           (self.src_seg, s["src"]), (self.lec_seg, s["lec_lang"])):
            seg.set(seg.labels[value])
        for sw in (self.sw_paste, self.sw_overlay, self.sw_duck, self.sw_autostart, self.sw_unmute, self.sw_keep):
            sw.select() if s[sw.key] else sw.deselect()
        for box in (self.sys_box, self.app_box, self.link_box):
            box.pack_forget()
        {"system": self.sys_box, "app": self.app_box, "link": self.link_box}[s["src"]].pack(fill="x")
        action = "перевести" if s["lec_lang"] == "en" else "сохранить"
        where = {"system": "весь звук ноутбука", "app": "одну программу", "link": "видео по ссылке"}[s["src"]]
        self.lec_hero.set_active(self.translating)
        self.lec_hero.set_text(
            title="Слушаю и перевожу" if s["lec_lang"] == "en" else "Слушаю и записываю",
            subtitle=f"Источник — {where}. Нажмите Alt+X, чтобы начать; ещё раз — {action}.",
            status=f"Идёт запись… Alt+X — остановить и {action}." if self.translating else "")
        self.mode_info.configure(text=self.tr_note or (
            "Живой перевод Google, нужен интернет — английский текст уходит в Google. "
            "Без интернета программа сама переводит офлайн."
            if s["tr_online"] else
            "Всё на ноутбуке, без интернета. Перевод верный по смыслу, но местами буквальный."))
        self.panel.set_mode("· Google" if s["tr_online"] else "· офлайн")
        if self.loading_msg:
            self.set_status("●  " + self.loading_msg, C["muted"])
        elif self.recording:
            self.set_status("●  Идёт запись", C["rec"])
        elif self.translating:
            self.set_status("●  Слушаю лекцию", C["blue"])
        else:
            self.set_status("●  Готово к работе", C["ok"])
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

    # ---------- события ----------
    def ui(self, *msg):
        self.uiq.put(msg)

    def _poll(self):
        try:
            while True:
                msg = self.uiq.get_nowait()
                try:
                    self._handle(*msg)
                except Exception as e:                # ошибка в одном событии не должна «заморозить» окно
                    log("!! интерфейс:", msg[0], e)
                    import traceback
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

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
        elif kind == "dict_ready":                  # микрофон открылся — индикатор голоса оживает
            self.pill.set_ready()
        elif kind == "dict_lang":                   # «Авто»: язык определён по голосу
            self.pill.set_lang(args[0])
        elif kind == "paste_blocked":
            self.paste_blocked = True
            self.tray.notify("Окно запущено от имени администратора — Windows может не дать вставить текст. "
                             "Если он не появился, нажмите Ctrl+V: текст в буфере обмена.", APP_TITLE)
        elif kind == "dict_done":
            final = args[0]
            blocked, self.paste_blocked = getattr(self, "paste_blocked", False), False
            if not self.recording:
                if not self.settings["paste"]:
                    label = "Сохранено в истории"
                else:
                    label = "В буфере · Ctrl+V" if blocked else "Вставлено"
                self.pill.set_state("done" if final else "idle", done_label=label)
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
            loaded = lang in self.asr.models
            self.pill.set_text("", "", "Говорите…" if loaded else
                               "Говорите — распознавание ещё загружается, текст появится через пару секунд")
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
        self.preload_models(code)

    def preload_models(self, code: str):
        """Заранее загрузить модели — по очереди, нужная первой: при включении ноутбука диск и процессор
        заняты, и одна модель грузится заметно быстрее, чем две наперегонки."""
        order = {"auto": ("ru", "en"), "ru": ("ru",), "en": ("en",)}.get(code, ("ru",))

        def load():
            for lang in order:
                try:
                    self.asr.get(lang)
                except Exception as e:
                    log(f"!! модель {lang} не загрузилась:", e)
                    self.ui("error", f"Не удалось загрузить распознавание ({LANG_NAMES[lang]}): {e}")

        threading.Thread(target=load, daemon=True, name="preload").start()

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
