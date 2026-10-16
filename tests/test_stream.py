"""Потоковое распознавание с поддельной моделью: ни одно слово не теряется, не повторяется и не режется
посередине — какими бы кусками ни приходил звук.

Поддельная модель «слышит» слово, только если его звук целиком попал в буфер. Чтобы она знала, какой
отрезок записи ей дали, звук — это номер отсчёта (пила): по первому значению видно начало буфера."""
import numpy as np
import pytest

from dsp import RATE, VAD_HOP
from recognition import StreamRecognizer

SCALE = 1e7
OFFSET = 0.01            # «пила» сдвинута вверх, чтобы звук не был тишиной


def script(sentences: list[str], word=0.4, gap=0.12, pause=0.7, start=0.5) -> list[tuple[str, float, float]]:
    """Слова с временем начала и конца (секунды от начала записи)."""
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
        self.cut_mid_word = []

    def recognize(self, lang, audio):
        if not len(audio):
            return [], -9.0
        start = round((float(audio[0]) - OFFSET) * SCALE) / RATE
        end = start + len(audio) / RATE
        heard = []
        for w, t0, t1 in self.words:
            # Буфер может начинаться на самом хвосте уже засчитанного слова (запас 0,25 с перед следующим
            # словом подобран на живых записях), но никогда — в начале или середине слова
            if t0 < start < t1 and (start - t0) / (t1 - t0) < 0.6:
                self.cut_mid_word.append((w, start))
            if t0 >= start - 0.01 and t1 <= end:
                heard.append((w, t0 - start + 0.08))    # модель ставит время чуть позже начала слова
        return heard, -0.1


class FakeVAD:
    """Как Silero VAD в потоке: вероятность голоса на каждые 512 отсчётов — по разметке слов."""

    def __init__(self, words):
        self.words = words
        self.fed = 0
        self.pending = 0

    def feed(self, audio):
        self.pending += len(audio)
        out = []
        while self.pending >= VAD_HOP:
            t = (self.fed + VAD_HOP / 2) / RATE
            out.append(0.9 if any(t0 - 0.05 <= t <= t1 + 0.05 for _, t0, t1 in self.words) else 0.02)
            self.fed += VAD_HOP
            self.pending -= VAD_HOP
        return out


def run(sentences, chunk=0.05, step=0.5):
    words = script(sentences)
    total = int((words[-1][2] + 1.5) * RATE)
    audio = (np.arange(total) / SCALE + OFFSET).astype(np.float32)
    asr = FakeASR(words)
    committed, lives = [], []
    sr = StreamRecognizer(asr, "ru", committed.append, lives.append, floor=0.0)
    sr.vad = FakeVAD(words)
    sr.STEP = step
    n = int(chunk * RATE)
    for i in range(0, total, n):
        sr.feed(audio[i:i + n])
        sr.step()
    sr.finish()
    return " ".join(committed), lives, asr


SENTENCES = ["Сегодня я хочу рассказать о своих планах.", "В понедельник нужно закончить отчёт.",
             "Во вторник встреча с командой, обсудим новые задачи и сроки по проекту.", "Готово."]


@pytest.mark.parametrize("chunk,step", [(0.05, 0.5), (0.03, 0.8), (0.2, 0.5)])
def test_every_word_exactly_once(chunk, step):
    text, _, asr = run(SENTENCES, chunk, step)
    assert text == " ".join(SENTENCES)
    assert not asr.cut_mid_word


def test_long_phrase_without_sentence_end_is_committed():
    """Длинная фраза без точек — всё равно засчитывается частями (по запятым или принудительно)."""
    long = " ".join(["слово,"] * 40) + " конец."
    text, _, asr = run([long])
    assert text == long
    assert not asr.cut_mid_word


def test_live_tail_has_no_trailing_period():
    """Точка на конце живого хвоста чаще всего «прыгает» — её не показываем."""
    _, lives, _ = run(SENTENCES)
    assert all(not tail.endswith(".") for tail in lives)
