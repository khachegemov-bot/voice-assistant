"""Стенд качества английского: одни и те же фразы — чистая речь (Zira, David), речь с записанным шумом
ноутбука и речь с сильным русским акцентом (английский текст читает русский голос Irina).
Сравнивает модели распознавания по доле ошибок в словах (WER) и скорости.

    python tools/en_bench.py [папка_модели ...]      (по умолчанию — текущая английская модель)
"""
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dsp import RATE, highpass  # noqa: E402
from paths import MODELS_DIR  # noqa: E402

TMP = Path(os.environ["TEMP"]) / "en_bench"
SENTENCES = [
    "Hello, I want to create a website for my new project.",
    "Can you help me write an email to my manager about the deadline?",
    "Let's schedule a meeting for next Tuesday afternoon.",
    "I'm working on a YouTube video about artificial intelligence.",
    "Please send me the report by Friday morning.",
    "The new version of the app is much faster and more stable.",
    "What do you think about this idea for a mobile game?",
    "I need to buy groceries and pay the bills tonight.",
]
VOICES = {"Zira": "Microsoft Zira Desktop", "David": "Microsoft David Desktop", "акцент": "Microsoft Irina Desktop"}


def tts(voice: str, text: str, path: Path):
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.SelectVoice($env:EB_VOICE); $s.Rate = 0; "
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo 16000, "
          "([System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen), "
          "([System.Speech.AudioFormat.AudioChannel]::Mono); "
          "$s.SetOutputToWaveFile($env:EB_PATH, $f); $s.Speak($env:EB_TEXT); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   env={**os.environ, "EB_VOICE": voice, "EB_PATH": str(path), "EB_TEXT": text})


def load(path: Path) -> np.ndarray:
    w = wave.open(str(path))
    return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def words(s: str) -> list[str]:
    s = s.lower().replace("i'm", "i am").replace("let's", "let us")
    return re.findall(r"[a-z0-9]+", s)


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def clips() -> dict[str, list[np.ndarray]]:
    TMP.mkdir(exist_ok=True)
    out = {}
    for tag, voice in VOICES.items():
        out[tag] = []
        for i, text in enumerate(SENTENCES):
            p = TMP / f"{tag}_{i}.wav"
            if not p.exists():
                tts(voice, text, p)
            out[tag].append(load(p))
    noise_path = Path(os.environ["TEMP"]) / "mic_noise_16k_mme.npy"
    if noise_path.exists():                          # тот же шум ноутбука, что и в asr_bench
        noise = np.load(noise_path)
        out["Zira + шум"] = []
        for a in out["Zira"]:
            s = a / np.sqrt(np.mean(a[np.abs(a) > 0.01] ** 2)) * 0.01
            n = np.tile(noise, int(np.ceil(len(s) / len(noise))))[:len(s)]
            out["Zira + шум"].append((s + n).astype(np.float32))
    return out


def load_model(folder: Path):
    import onnx_asr
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 3
    opts.inter_op_num_threads = 1
    name = folder.name
    return onnx_asr.load_model(name, str(folder), quantization="int8", sess_options=opts)


def prepare(a: np.ndarray) -> np.ndarray:
    a = highpass(np.concatenate([np.zeros(RATE // 4, np.float32), a, np.zeros(RATE // 4, np.float32)]))
    peak = np.percentile(np.abs(a), 99.9)
    return (np.tanh(a * min(0.5 / max(peak, 1e-6), 50) * 1.2) / 1.2).astype(np.float32)


def main():
    folders = [Path(p) for p in sys.argv[1:]] or [MODELS_DIR / "nemo-parakeet-tdt-0.6b-v2"]
    data = clips()
    for folder in folders:
        model = load_model(folder)
        model.recognize(prepare(data["Zira"][0]), sample_rate=RATE)          # прогрев
        print(f"== {folder.name}", flush=True)
        for tag, items in data.items():
            errs, t, dur = [], 0.0, 0.0
            worst = ("", 0.0)
            for ref, audio in zip(SENTENCES, items, strict=True):
                t0 = time.perf_counter()
                kw = {"language": "en"} if "canary" in folder.name else {}
                hyp = model.recognize(prepare(audio), sample_rate=RATE, **kw)
                t += time.perf_counter() - t0
                dur += len(audio) / RATE
                e = wer(ref, hyp)
                errs.append(e)
                if e > worst[1]:
                    worst = (hyp, e)
            print(f"   {tag:11} ошибок {np.mean(errs):5.1%}   скорость {t / dur:.2f} с на 1 с речи"
                  + (f"   худшее: «{worst[0][:70]}»" if worst[1] else ""), flush=True)


if __name__ == "__main__":
    main()
