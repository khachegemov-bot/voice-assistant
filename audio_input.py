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

from dsp import RATE, VAD_HOP


# ─────────────────────────── определение речи ───────────────────────────

class SileroVAD:
    """Потоковая Silero VAD: на каждые 32 мс звука — вероятность, что это голос (0…1)."""
    HOP, CTX = VAD_HOP, 64

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


# ─────────────────────────── захват микрофона ───────────────────────────

_pa_lock = threading.Lock()


def _reinit():
    try:
        sd._terminate()
        sd._initialize()
    except Exception as e:                      # noqa: BLE001
        print("!! обновление списка устройств:", e, flush=True)


def refresh_devices():
    """Свежий список звуковых устройств (~0,02 с). Без этого после подключения или отключения гарнитуры
    номера устройств устаревают, и микрофон не открывается с первого раза."""
    with _pa_lock:
        _reinit()


def find_mme_input(name: str = ""):
    """Микрофон через MME по названию; None — микрофон Windows по умолчанию."""
    if not name:
        return None
    mme = _hostapi("MME")
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d["max_input_channels"] > 0 and d["hostapi"] == mme:
            return i
    return None

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
        self.name, self.on_audio = name, on_audio
        self.stream = None
        self.mode = ""
        self.device_name = ""

    def _open_wasapi(self, dev: int):
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
        try:
            self.stream.start()
        except Exception:
            self.stream.close()
            self.stream = None
            raise
        self.mode = f"WASAPI {sr // 1000} кГц × {ch} кан."
        self.device_name = info["name"]

    def __enter__(self):
        with _pa_lock:
            _reinit()
            for attempt in range(2):                # не открылся — обновляем список устройств и ещё раз
                dev = find_wasapi_input(self.name)
                if dev is None:
                    break
                try:
                    self._open_wasapi(dev)
                    return self
                except Exception as e:          # noqa: BLE001
                    print(f"WASAPI не открылся (попытка {attempt + 1}):", e, flush=True)
                    if attempt == 0:
                        _reinit()
            self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                                         device=find_mme_input(self.name), blocksize=int(RATE * 0.05),
                                         callback=lambda d, *_: self.on_audio(d[:, 0].copy()))
            self.stream.start()
            self.mode = "MME 16 кГц"
            try:
                self.device_name = sd.query_devices(self.stream.device, "input")["name"]
            except Exception:                   # noqa: BLE001
                self.device_name = ""
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
