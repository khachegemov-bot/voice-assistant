"""Звук на входе: чистый захват микрофона и умное определение речи.

• Микрофон берём через WASAPI в его родном качестве (обычно 48 кГц, у ноутбуков — 2 микрофона),
  сводим каналы в один (речь у них совпадает, а шум — нет, поэтому шум ослабевает) и качественно
  переводим в 16 кГц (soxr). Если WASAPI не открылся — старый путь через MME.
• Silero VAD — нейросеть, которая отличает голос от шума (стук клавиш, вентилятор, шипение)
  по тому, как он звучит, а не по громкости. Тихая речь больше не принимается за тишину.
"""
import threading
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort
import sounddevice as sd

RATE = 16000


# ─────────────────────────── определение речи ───────────────────────────

class SileroVAD:
    """Потоковая Silero VAD: на каждые 32 мс звука — вероятность, что это голос (0…1)."""
    HOP, CTX = 512, 64

    def __init__(self, model_path: Path):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self.lock = threading.Lock()

    def new_stream(self) -> "VADStream":
        return VADStream(self)

    def probs(self, audio: np.ndarray) -> np.ndarray:
        """Вероятности голоса для всего куска звука (новая независимая проверка)."""
        s = self.new_stream()
        return np.array(s.feed(audio) + s.flush(), np.float32)


class VADStream:
    def __init__(self, vad: SileroVAD):
        self.vad = vad
        self.state = np.zeros((2, 1, 128), np.float32)
        self.ctx = np.zeros(SileroVAD.CTX, np.float32)
        self.pending = np.zeros(0, np.float32)

    def feed(self, audio: np.ndarray) -> list[float]:
        self.pending = np.concatenate([self.pending, audio.astype(np.float32)])
        out = []
        hop = SileroVAD.HOP
        while len(self.pending) >= hop:
            frame = np.concatenate([self.ctx, self.pending[:hop]])[None, :]
            with self.vad.lock:
                prob, self.state = self.vad.sess.run(["output", "stateN"], {
                    "input": frame, "state": self.state, "sr": np.array([RATE], np.int64)})
            out.append(float(prob[0, 0]))
            self.ctx = self.pending[hop - SileroVAD.CTX:hop]
            self.pending = self.pending[hop:]
        return out

    def flush(self) -> list[float]:
        if not len(self.pending):
            return []
        return self.feed(np.zeros(SileroVAD.HOP - len(self.pending), np.float32))


def speech_mask(probs: np.ndarray, n_samples: int, pad: float = 0.25, threshold: float = 0.5) -> np.ndarray:
    """Маска «здесь речь» по отсчётам, с запасом pad секунд до и после (чтобы не резать начала слов)."""
    frame = np.repeat(probs >= threshold, SileroVAD.HOP)[:n_samples]
    frame = np.pad(frame, (0, max(0, n_samples - len(frame))))
    if frame.any() and pad:
        k = int(pad * RATE)
        idx = np.flatnonzero(np.diff(np.concatenate([[0], frame.astype(np.int8), [0]])))
        for start, end in zip(idx[::2], idx[1::2]):
            frame[max(0, start - k):min(n_samples, end + k)] = True
    return frame


# ─────────────────────────── захват микрофона ───────────────────────────

def _hostapi(name_part: str):
    for i, h in enumerate(sd.query_hostapis()):
        if name_part in h["name"]:
            return i
    return None


def find_wasapi_input(name: str = ""):
    """WASAPI-устройство для микрофона: по названию (начало совпадает) или микрофон по умолчанию."""
    wasapi = _hostapi("WASAPI")
    if wasapi is None:
        return None
    want = name or sd.query_devices(kind="input")["name"]
    want = want[:24]
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == wasapi and d["max_input_channels"] > 0 and d["name"].startswith(want):
            return i
    default = sd.query_hostapis(wasapi).get("default_input_device", -1)
    return default if default is not None and default >= 0 and not name else None


class MicStream:
    """with MicStream(name, on_audio): on_audio(np.float32 моно 16 кГц) вызывается ~каждые 50 мс."""

    def __init__(self, name: str, on_audio, mme_device=None):
        self.name, self.on_audio, self.mme_device = name, on_audio, mme_device
        self.stream = None
        self.mode = ""

    def __enter__(self):
        dev = find_wasapi_input(self.name)
        if dev is not None:
            try:
                import soxr
                info = sd.query_devices(dev)
                sr, ch = int(info["default_samplerate"]), max(1, min(2, info["max_input_channels"]))
                rs = soxr.ResampleStream(sr, RATE, 1, dtype="float32", quality="HQ")

                def cb(data, *_):
                    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
                    out = rs.resample_chunk(np.ascontiguousarray(mono, np.float32))
                    if len(out):
                        self.on_audio(out)

                self.stream = sd.InputStream(samplerate=sr, channels=ch, dtype="float32", device=dev,
                                             blocksize=int(sr * 0.05), callback=cb)
                self.stream.start()
                self.mode = f"WASAPI {sr // 1000} кГц × {ch} кан."
                return self
            except Exception as e:          # noqa: BLE001 — падаем на MME
                print("WASAPI не открылся, беру MME:", e, flush=True)
                self.stream = None
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", device=self.mme_device,
                                     blocksize=int(RATE * 0.05), callback=lambda d, *_: self.on_audio(d[:, 0].copy()))
        self.stream.start()
        self.mode = "MME 16 кГц"
        return self

    def __exit__(self, *exc):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()


# ─────────────────────────── записи для проверки качества ───────────────────────────

def save_recording(folder: Path, audio: np.ndarray, text: str, keep: int = 5):
    """Последние записи диктовки (только у вас на компьютере) — чтобы проверять качество на живом голосе."""
    import datetime as dt
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    with wave.open(str(folder / f"{stamp}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    (folder / f"{stamp}.txt").write_text(text, "utf-8")
    files = sorted(folder.glob("*.wav"))
    for old in files[:-keep]:
        old.unlink(missing_ok=True)
        old.with_suffix(".txt").unlink(missing_ok=True)
