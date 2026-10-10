"""Диагностика микрофона: формат устройства, уровни по каналам, шум.  python tools/mic_diag.py"""
import numpy as np
import sounddevice as sd

apis = sd.query_hostapis()
mme = next(i for i, h in enumerate(apis) if h["name"] == "MME")
wasapi = next(i for i, h in enumerate(apis) if "WASAPI" in h["name"])
dflt = sd.query_devices(kind="input")
print("микрофон по умолчанию:", dflt["name"], "| каналов:", dflt["max_input_channels"],
      "| частота:", dflt["default_samplerate"])

wdev = next((i for i, d in enumerate(sd.query_devices())
             if d["hostapi"] == wasapi and d["max_input_channels"] > 0 and dflt["name"][:20] in d["name"]), None)
if wdev is not None:
    d = sd.query_devices(wdev)
    sr, ch = int(d["default_samplerate"]), d["max_input_channels"]
    print(f"WASAPI: {d['name']} — {ch} кан., {sr} Гц")
    a = sd.rec(int(sr * 3), samplerate=sr, channels=ch, dtype="float32", device=wdev)
    sd.wait()
    for c in range(ch):
        x = a[:, c]
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        f = np.fft.rfftfreq(len(x), 1 / sr)
        hi = spec[f > 4000].sum() / max(spec.sum(), 1e-12)
        print(f"  канал {c}: RMS {np.sqrt(np.mean(x ** 2)):.6f}  пик {np.abs(x).max():.5f}  доля >4кГц {hi:.2f}")
    if ch > 1:
        cc = np.corrcoef(a.T)
        print("  похожесть каналов (корреляция):", np.round(cc[0], 2).tolist())

b = sd.rec(16000 * 3, samplerate=16000, channels=1, dtype="float32", device=None)
sd.wait()
print(f"MME 16 кГц моно (как сейчас): RMS {np.sqrt(np.mean(b ** 2)):.6f}  пик {np.abs(b).max():.5f}")
