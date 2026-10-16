"""Обработка звука без внешних зависимостей (только numpy): фильтры, громкость, разметка речи,
разрезание длинной записи в паузах и укорачивание пауз. Всё — 16 кГц моно float32."""
import numpy as np

RATE = 16000
VAD_HOP = 512            # Silero VAD: одна вероятность голоса на 512 отсчётов (32 мс)


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0


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


def speech_mask(probs: np.ndarray, n_samples: int, pad: float = 0.25, threshold: float = 0.5) -> np.ndarray:
    """Маска «здесь речь» по отсчётам, с запасом pad секунд до и после (чтобы не резать начала слов)."""
    frame = np.repeat(probs >= threshold, VAD_HOP)[:n_samples]
    frame = np.pad(frame, (0, max(0, n_samples - len(frame))))
    if frame.any() and pad:
        k = int(pad * RATE)
        idx = np.flatnonzero(np.diff(np.concatenate([[0], frame.astype(np.int8), [0]])))
        for start, end in zip(idx[::2], idx[1::2], strict=True):
            frame[max(0, start - k):min(n_samples, end + k)] = True
    return frame


def squeeze_pauses(audio: np.ndarray, probs, keep: float = 0.35, pad: float = 0.2) -> np.ndarray:
    """Длинные паузы → короткие (keep с). На длинной тишине внутри куска модель распознавания иногда
    «теряет» слова после неё (на живой записи пропадала целая фраза), а без пауз ещё и быстрее.
    probs — вероятности голоса Silero VAD с начала этого куска (по одной на 512 отсчётов)."""
    probs = np.asarray(probs, np.float32)
    if not len(probs) or not len(audio):
        return audio
    m = speech_mask(probs, len(audio), pad=pad)
    if not m.any():
        return audio
    keep_n = int(keep * RATE)
    out, last = [], 0
    edges = np.flatnonzero(np.diff(np.concatenate([[1], m.astype(np.int8), [1]])))
    for s0, s1 in zip(edges[::2], edges[1::2], strict=True):          # s0…s1 — тишина
        if s1 - s0 > keep_n:
            out.append(audio[last:s0 + keep_n // 2])
            last = s1 - keep_n // 2
    out.append(audio[last:])
    return np.concatenate(out)


def split_at_pauses(audio: np.ndarray, min_s: float = 12, max_s: float = 20) -> list[np.ndarray]:
    """Длинный звук → куски по min_s…max_s секунд, разрезанные в самом тихом месте (между словами)."""
    parts, pos = [], 0
    while len(audio) - pos > max_s * RATE:
        lo, hi = pos + int(min_s * RATE), pos + int(max_s * RATE)
        win = audio[lo:hi]
        n = len(win) // 480
        levels = np.sqrt(np.mean(win[:n * 480].reshape(n, 480) ** 2, axis=1))
        levels = np.convolve(levels, np.ones(10) / 10, "same")
        end = lo + int(np.argmin(levels)) * 480 + 240
        parts.append(audio[pos:end])
        pos = end
    parts.append(audio[pos:])
    return parts
