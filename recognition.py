"""Распознавание речи: модели (GigaAM v3 — русский, NVIDIA Parakeet — английский), определение языка
по голосу, потоковое распознавание без потерь слов и фоновая итоговая сверка.

Тяжёлые библиотеки (onnx-asr, ONNX Runtime) подключаются только при загрузке модели — поэтому логику
потокового распознавания можно проверять тестами с поддельной моделью (tests/test_stream.py)."""
import datetime as dt
import queue
import re
import threading
import time
from collections import deque

import numpy as np

from dsp import RATE, VAD_HOP, highpass, rms, speech_mask, split_at_pauses, squeeze_pauses
from paths import MODELS_DIR
from textproc import append_piece, is_filler, tidy, tokens_to_words


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


ASR_MODELS = {   # язык: (модель, квантизация)
    "ru": ("gigaam-v3-e2e-rnnt", "int8"),        # int8: в 4 раза меньше, на 20% быстрее, точность та же
    "en": ("nemo-parakeet-tdt-0.6b-v2", "int8"),
}
LANG_NAMES = {"ru": "Русский", "en": "English"}


class Recognizers:
    """Модели распознавания, загружаются при первом использовании."""

    THREADS = 3

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
                # 3 потока: у процессоров ноутбуков (2 быстрых ядра + 8 экономных) это в 2–3 раза быстрее,
                # чем «все ядра» по умолчанию — экономные ядра тормозят общую работу
                import onnx_asr
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self.THREADS
                opts.inter_op_num_threads = 1
                self.models[lang] = onnx_asr.load_model(name, str(local) if local.is_dir() else None,
                                                        quantization=quant, sess_options=opts)
                self.ts_models[lang] = self.models[lang].with_timestamps()
                # первое распознавание после загрузки в несколько раз медленнее (ONNX готовит вычисления) —
                # делаем его сразу, чтобы первая диктовка не «подвисала»
                warm = np.random.default_rng(0).normal(0, 0.01, RATE * 2).astype(np.float32)
                self.ts_models[lang].recognize(warm, sample_rate=RATE)
                log(f"модель {name} загружена и прогрета за {time.time() - t:.1f} с")
                self.on_status(None)
            return self.models[lang]

    vad = None              # Silero VAD (задаёт App): громкость подстраиваем по голосу, а не по щелчкам
    VOICE_GATE = False      # приглушать отрезки без голоса — на проверке ухудшило точность (срезает края слов)

    def prepare(self, audio: np.ndarray) -> np.ndarray:
        """Громкость для распознавания. Раньше подстраивалась по самому громкому месту — щелчок клавиши
        оказывался громче тихого голоса, и голос оставался тихим. Теперь — по голосу (Silero VAD)."""
        if not len(audio):
            return audio
        mask = None
        if self.vad is not None and len(audio) >= RATE // 4:
            mask = speech_mask(self.vad.probs(audio), len(audio), pad=0.3)
            if not mask.any():
                mask = None
        if mask is not None:
            voice_rms = rms(audio[mask])
            gain = min(0.08 / max(voice_rms, 1e-6), 40)          # голос → комфортный уровень
            if self.VOICE_GATE:
                ramp = np.convolve(mask.astype(np.float32), np.ones(800) / 800, "same")   # плавно, без щелчков
                audio = audio * (0.15 + 0.85 * ramp)                # без голоса — тише в ~7 раз
        else:
            peak = np.percentile(np.abs(audio), 99.9)
            gain = min(0.5 / peak, 50) if peak > 0 else 1.0
        out = audio * gain
        return (np.tanh(out * 1.2) / 1.2).astype(np.float32)          # мягкий ограничитель вместо обрезания

    def recognize(self, lang: str, audio: np.ndarray) -> tuple[list[tuple[str, float]], float]:
        """Слова с временем начала каждого (в секундах) и уверенность модели (средний log-prob, ≤ 0)."""
        self.get(lang)
        audio = self.prepare(highpass(audio))
        r = self.ts_models[lang].recognize(audio, sample_rate=RATE)
        lp = getattr(r, "logprobs", None) or []
        return tokens_to_words(r.tokens or [], r.timestamps or []), (float(np.mean(lp)) if lp else -9.0)

    def recognize_words(self, lang: str, audio: np.ndarray) -> list[tuple[str, float]]:
        return self.recognize(lang, audio)[0]

    LANG_MARGIN = 0.05       # насколько одна модель должна быть увереннее другой, чтобы сменить язык

    def detect_lang(self, audio: np.ndarray, guess: str, known: dict | None = None) -> tuple[str | None, dict]:
        """Язык речи по самому голосу: обе модели распознают один и тот же звук, побеждает более уверенная.
        Русская модель, слыша английскую речь, пишет латиницей — это тоже верный признак.
        Возвращает (язык или None — «пока непонятно», результаты обеих моделей)."""
        res = dict(known or {})
        for lang in ("ru", "en"):
            if lang not in res:
                res[lang] = self.recognize(lang, audio)
        (ru_words, ru_lp), (en_words, en_lp) = res["ru"], res["en"]
        if not ru_words and not en_words:
            return None, res
        if not en_words:
            return "ru", res
        if not ru_words:
            return "en", res
        letters = re.findall(r"[^\W\d_]", " ".join(w for w, _ in ru_words))
        if letters and sum(ch.isascii() for ch in letters) / len(letters) > 0.5:
            return "en", res
        if en_lp - ru_lp > self.LANG_MARGIN:
            return "en", res
        if ru_lp - en_lp > self.LANG_MARGIN:
            return "ru", res
        return None, res


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
        self.vad_probs: list = []                     # вероятности голоса для всего звука (по 512 отсчётов)
        self.vad = None                               # Silero VAD (поток): отличает голос от шума
        self.voice_since_step = False                 # был ли голос с прошлого шага
        self.voice_samples = 0                        # сколько всего звучал голос
        self.detect = False                           # «Авто»: язык ещё уточняется по голосу
        self.fast_step = None                         # чаще обновлять, пока буфер короткий (диктовка)
        self.on_lang = lambda lang: None

    def feed(self, audio: np.ndarray):
        if len(audio):
            self.buf = np.concatenate([self.buf, audio])
            self.levels.append(rms(audio))
            if self.keep_all:
                self.all_audio.append(audio)
            if self.vad is not None:
                probs = self.vad.feed(audio)
                if self.keep_all:
                    self.vad_probs.extend(probs)
                voiced = sum(p >= 0.5 for p in probs)
                if voiced:
                    self.voice_since_step = True
                    self.voice_samples += voiced * 512

    def _quiet(self, audio: np.ndarray) -> bool:
        if self.vad is not None:                      # нейросеть: голос — по звучанию, а не по громкости
            return not self.voice_since_step
        noise = float(np.percentile(self.levels, 10)) * 2.5 if len(self.levels) > 20 else 0.0
        return rms(audio) < max(self.floor, noise)

    def _recognize(self, audio):
        return self._recognize_lp(audio)[0]

    def _recognize_lp(self, audio):
        try:
            return self.asr.recognize(self.lang, audio)
        except Exception as e:
            log("!! распознавание:", e)
            return [], -9.0

    def _check_lang(self, words, lp, quiet: bool) -> tuple[list, bool]:
        """«Авто»: сверяем язык по голосу, пока он не ясен. Возвращает (слова, можно ли засчитывать)."""
        heard = self.voice_samples / RATE
        if not words or (heard < 0.8 and not quiet):
            return words, False                       # мало голоса — ждём, ничего не засчитываем
        try:
            lang, res = self.asr.detect_lang(self.buf, self.lang, {self.lang: (words, lp)})
        except Exception as e:
            log("!! определение языка:", e)
            self.detect = False
            return words, True
        final = heard >= 3 or quiet
        if lang is None and not final:
            return words, False                       # обе модели одинаково уверены — слушаем дальше
        self.detect = False
        if lang and lang != self.lang:
            log(f"язык по голосу: {LANG_NAMES[lang]} (а не {LANG_NAMES[self.lang]})")
            self.lang = lang
            self.prev = []                            # устойчивость слов считаем заново — другая модель
            self.on_lang(lang)
            return res[lang][0], False
        return words, True

    def _emit(self, words):
        text = tidy(" ".join(w for w, _ in words).strip())
        if text and not is_filler(text):
            self.on_commit(text)

    @staticmethod
    def _norm(word: str) -> str:
        return re.sub(r"[^\w']", "", word.lower())

    def step(self):
        step = self.fast_step if self.fast_step and len(self.buf) < 6 * RATE else self.STEP
        if len(self.buf) - self.decoded < step * RATE:
            return
        quiet = self._quiet(self.buf[self.decoded:])
        self.voice_since_step = False
        if quiet and not self.prev:                   # тишина и дописывать нечего — не тратим процессор
            self.buf = self.buf[-RATE // 2:]
            self.decoded = len(self.buf)
            return
        words, lp = self._recognize_lp(self.buf)
        may_commit = True
        if self.detect:
            words, may_commit = self._check_lang(words, lp, quiet)
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
        if not may_commit:
            upto = 0

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
        self.on_live(re.sub(r"[.…]+$", "", " ".join(w for w, _ in self.prev)))

    def will_final_pass(self) -> bool:
        return self.keep_all          # диктовка: итоговая сверка любой длины (идёт в фоне — см. Finalizer)

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


class Finalizer:
    """Итоговая сверка, которая идёт в фоне прямо во время диктовки.

    Потоковое распознавание показывает слова сразу, но режет речь на куски — на стыках рвутся фразы
    и появляются лишние точки. Поэтому готовые куски записи (12–20 с, разрезанные в паузах) распознаются
    заново целиком, с укороченными паузами (на длинной тишине модель теряет слова). После «стоп»
    остаётся дораспознать только хвост — ждать почти не приходится, какой бы длинной ни была диктовка."""
    SEG_MIN, SEG_MAX = 12, 20

    def __init__(self, asr: "Recognizers", sr: StreamRecognizer):
        self.asr, self.sr = asr, sr
        self.pos = 0                                  # столько отсчётов уже сверено
        self.text = ""
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="finalizer")

    def start(self):
        self.thread.start()

    def _audio(self) -> np.ndarray:
        parts = list(self.sr.all_audio)
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def _piece(self, audio: np.ndarray, start: int) -> str:
        hop = VAD_HOP
        probs = self.sr.vad_probs[start // hop:(start + len(audio) + hop - 1) // hop]
        words = self.asr.recognize_words(self.sr.lang, squeeze_pauses(audio, probs))
        piece = tidy(" ".join(w for w, _ in words).strip())
        return "" if not piece or is_filler(piece) else piece

    def _add(self, audio: np.ndarray, start: int):
        piece = self._piece(audio, start)
        if piece:
            self.text = append_piece(self.text, piece, self.sr.lang)

    def _loop(self):
        while not self.done.wait(0.5):
            if self.sr.detect:                        # язык ещё не определён — ждём
                continue
            with self.lock:
                audio = self._audio()
                if len(audio) - self.pos < (self.SEG_MAX + 1) * RATE:
                    continue
                window = audio[self.pos:self.pos + (self.SEG_MAX + 1) * RATE]
                cut = self.pos + len(split_at_pauses(window, self.SEG_MIN, self.SEG_MAX,
                                                     self.sr.vad_probs[self.pos // VAD_HOP:])[0])
                cut -= cut % 512                      # по сетке Silero VAD
                try:
                    self._add(audio[self.pos:cut], self.pos)
                except Exception as e:
                    log("!! фоновая сверка:", e)
                self.pos = cut

    MAX_TAIL = 45            # с — если сверка отстала сильнее (процессор занят другой программой), не ждём

    def finish(self) -> str | None:
        """Стоп: дождаться фоновой работы и дораспознать хвост. None — сверка слишком отстала:
        лучше сразу вставить живой текст, чем заставлять ждать."""
        self.done.set()
        with self.lock:
            audio = self._audio()
            pos = self.pos
            if len(audio) - pos > self.MAX_TAIL * RATE:
                log(f"сверка отстала на {(len(audio) - pos) / RATE:.0f} с — вставляю живой текст")
                return None
            for part in split_at_pauses(audio[pos:], self.SEG_MIN, self.SEG_MAX, self.sr.vad_probs[pos // VAD_HOP:]):
                self._add(part, pos)
                pos += len(part)
            self.pos = pos
            return self.text


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
