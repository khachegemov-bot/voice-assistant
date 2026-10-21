"""Фоновая итоговая сверка: длинная запись режется на куски в паузах, паузы укорачиваются, куски
распознаются заново — и ни одно слово не должно потеряться или повториться на стыках.

Поддельная модель «слышит» слово, только если весь его звук дошёл до неё целым и подряд. Звук — номер
отсчёта (пила), поэтому после вырезания пауз видно, какие отсчёты остались."""
import threading
import time
from types import SimpleNamespace

import numpy as np

from dsp import RATE, VAD_HOP
from recognition import Finalizer

SCALE, OFFSET = 1e7, 0.01


def script(sentences, word=0.4, gap=0.12, pause=0.9, start=0.5):
    out, t = [], start
    for sentence in sentences:
        for w in sentence.split():
            out.append((w, t, t + word))
            t += word + gap
        t += pause
    return out


class FakeASR:
    def __init__(self, words):
        self.words = words
        self.calls = 0

    def recognize_words(self, lang, audio):
        self.calls += 1
        idx = np.round((audio.astype(np.float64) - OFFSET) * SCALE).astype(np.int64)
        heard = []
        for w, t0, t1 in self.words:
            a, b = int(t0 * RATE), int(t1 * RATE)
            i = int(np.searchsorted(idx, a))
            j = i + (b - a)
            if i < len(idx) and idx[i] == a and j < len(idx) and idx[j] == b:   # слово целиком и подряд
                heard.append((w, i / RATE))
        return heard


def vad_probs(words, n_samples):
    t = (np.arange(n_samples // VAD_HOP) * VAD_HOP + VAD_HOP / 2) / RATE
    voiced = np.zeros(len(t), bool)
    for _, t0, t1 in words:
        voiced |= (t >= t0 - 0.05) & (t <= t1 + 0.05)
    return list(np.where(voiced, 0.9, 0.02))


def run(sentences, background=True):
    words = script(sentences)
    total = int((words[-1][2] + 1.0) * RATE) // VAD_HOP * VAD_HOP
    audio = (np.arange(total) / SCALE + OFFSET).astype(np.float32)
    sr = SimpleNamespace(all_audio=[audio[i:i + 8000] for i in range(0, total, 8000)],
                         vad_probs=vad_probs(words, total), detect=False, lang="ru")
    asr = FakeASR(words)
    fin = Finalizer(asr, sr)
    if background:                                  # дать фоновой сверке пройти готовые куски
        fin.start()
        deadline = time.time() + 15
        while time.time() < deadline and len(audio) - fin.pos > (Finalizer.SEG_MAX + 1) * RATE:
            time.sleep(0.1)
    return fin.finish(), fin, asr


SENTENCES = [f"Это предложение номер {n}, в нём есть несколько обычных слов." for n in range(1, 13)]


def test_long_dictation_every_word_once():
    text, fin, asr = run(SENTENCES)
    assert text == " ".join(SENTENCES)
    assert asr.calls >= 4                           # запись ~75 с — несколько кусков


def test_without_background_pass():
    """Фоновая сверка не успела ничего — после «стоп» распознаётся весь хвост (до 45 с)."""
    text, _, _ = run(SENTENCES[:6], background=False)
    assert text == " ".join(SENTENCES[:6])


def test_far_behind_gives_up_instead_of_making_you_wait():
    """Процессор занят другой программой, сверка отстала больше чем на 45 с — не ждём, вставляем живой текст."""
    text, _, asr = run(SENTENCES, background=False)
    assert text is None and asr.calls == 0


def test_speech_without_long_pauses():
    """Человек говорит без остановок: резать приходится в коротких промежутках между словами."""
    long = " ".join(f"слово{n}" for n in range(70)) + "."
    text, _, _ = run([long])
    assert text == long


def test_does_not_run_while_language_unknown():
    words = script(SENTENCES)
    total = int((words[-1][2] + 1.0) * RATE) // VAD_HOP * VAD_HOP
    sr = SimpleNamespace(all_audio=[(np.arange(total) / SCALE + OFFSET).astype(np.float32)],
                         vad_probs=vad_probs(words, total), detect=True, lang="ru")
    fin = Finalizer(FakeASR(words), sr)
    fin.start()
    time.sleep(1.2)
    assert fin.pos == 0                             # пока язык не определён — ждём
    fin.done.set()
    threading.Event().wait(0.6)
