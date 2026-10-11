"""Стенд качества диктовки: речь + настоящий шум микрофона → программа → доля ошибок в словах (WER).
python tools/asr_bench.py [метка]   (шум берётся из %TEMP%\\mic_noise_16k_mme.npy)"""
import os
import queue
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as A  # noqa: E402

TMP = Path(os.environ["TEMP"])
SENTENCES = [
    "Сегодня я хочу рассказать о своих планах на неделю.",
    "В понедельник нужно закончить отчёт по проекту и отправить его руководителю.",
    "Во вторник встреча с командой, обсудим новые задачи.",
    "Купи, пожалуйста, хлеб, молоко и немного сыра.",
    "Напомни мне позвонить маме вечером в семь часов.",
    "Курсовая работа по программированию почти готова, осталось оформить отчёт.",
    "Преподаватель попросил сдать лабораторную до пятницы.",
    "Давай встретимся у метро через двадцать минут.",
]


def tts(text: str, path: Path):
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.SelectVoice('Microsoft Irina Desktop'); "
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo 16000, "
          "([System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen), ([System.Speech.AudioFormat.AudioChannel]::Mono); "
          f"$s.SetOutputToWaveFile('{path}', $f); $s.Speak($env:BENCH_TEXT); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, env={**os.environ, "BENCH_TEXT": text})


def speech_track() -> tuple[np.ndarray, str]:
    path = TMP / "bench_speech.wav"
    if not path.exists():
        tts(" ".join(SENTENCES), path)
    w = wave.open(str(path))
    return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768, " ".join(SENTENCES)


def words(s: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", s.lower().replace("ё", "е"))


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


class FakeApp:
    def __init__(self):
        self.asr = A.Recognizers(lambda m: None)
        self.settings = {"paste": False, "mic": "", "keep_recordings": False, "lang": "ru"}
        self.stats = type("S", (), {"add": lambda *a: None})()
        self.vad = None
        self.history = type("H", (), {"add": lambda *a: None})()
        self.done = threading.Event()
        self.final = ""

    def ui(self, kind, *args):
        if kind == "dict_done":
            self.final = args[0]
            self.done.set()


def run(audio: np.ndarray, fa: FakeApp) -> str:
    d = A.Dictation(fa)
    q: queue.Queue = queue.Queue()
    fa.done.clear()
    threading.Thread(target=d._process, args=(q, "ru"), daemon=True).start()
    step = 800
    for i in range(0, len(audio), step):                 # в реальном темпе, как с микрофона
        q.put(audio[i:i + step])
        time.sleep(step / 16000)
    q.put(None)
    fa.done.wait(60)
    return fa.final


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "текущая версия"
    speech, ref = speech_track()
    noise = np.load(TMP / "mic_noise_16k_mme.npy")
    rng = np.random.default_rng(0)
    fa = FakeApp()
    fa.asr.get("ru")
    if "--vad" in sys.argv:
        from audio_input import SileroVAD
        fa.vad = SileroVAD(Path(A.MODELS_DIR) / "silero-vad" / "silero_vad.onnx")
    if "--gain" in sys.argv:
        fa.asr.vad = fa.vad
        A.Recognizers.VOICE_GATE = "--gate" in sys.argv
    print(f"== {label} ==")
    total = []
    for name, level in (("обычная речь", 0.03), ("тихая речь", 0.008), ("очень тихая речь", 0.004)):
        s = speech / np.sqrt(np.mean(speech[np.abs(speech) > 0.01] ** 2)) * level   # громкость речи
        lead = np.zeros(8000, np.float32)
        x = np.concatenate([lead, s, np.zeros(12000, np.float32)])
        reps = int(np.ceil(len(x) / len(noise)))
        nz = np.tile(noise, reps)[:len(x)] * 1.0
        x = (x + nz + rng.normal(0, 1e-5, len(x))).astype(np.float32)
        hyp = run(x, fa)
        e = wer(ref, hyp)
        total.append(e)
        print(f"  {name} (уровень {level}): ошибок в словах {e:.0%}\n    → {hyp[:160]}")
    print(f"  СРЕДНЯЯ ОШИБКА: {np.mean(total):.0%}")


if __name__ == "__main__":
    main()
