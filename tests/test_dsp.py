"""Обработка звука: громкость, фильтр, разметка речи, сжатие пауз, разрезание длинной записи."""
import numpy as np

from dsp import RATE, VAD_HOP, highpass, rms, speech_mask, split_at_pauses, squeeze_pauses


def tone(freq: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_rms():
    assert abs(rms(tone(440, 1, amp=1.0)) - 1 / np.sqrt(2)) < 1e-3
    assert rms(np.zeros(0, np.float32)) == 0.0


def test_highpass_removes_hum_and_keeps_voice():
    hum, voice = tone(30, 2), tone(1000, 2)
    assert rms(highpass(hum)) < 0.05 * rms(hum)
    assert rms(highpass(voice)) > 0.95 * rms(voice)


def test_speech_mask_pads_both_sides():
    probs = np.array([0, 0, 0, 0, 1, 1, 0, 0, 0, 0], np.float32)
    mask = speech_mask(probs, len(probs) * VAD_HOP, pad=0.05)
    pad = int(0.05 * RATE)
    assert mask[4 * VAD_HOP - pad] and mask[6 * VAD_HOP + pad - 1]
    assert not mask[0] and not mask[-1]


def frames_for(segments: list[tuple[float, bool]]) -> tuple[np.ndarray, np.ndarray]:
    """Звук из кусков (секунды, голос ли) и вероятности голоса к нему — как от Silero VAD."""
    audio, probs = [], []
    for seconds, voiced in segments:
        n = int(seconds * RATE) // VAD_HOP * VAD_HOP
        audio.append(tone(300, n / RATE) if voiced else np.zeros(n, np.float32))
        probs += [0.9 if voiced else 0.02] * (n // VAD_HOP)
    return np.concatenate(audio), np.array(probs, np.float32)


def test_squeeze_shortens_long_pauses_only():
    audio, probs = frames_for([(1, True), (3, False), (1, True), (0.2, False), (1, True)])
    out = squeeze_pauses(audio, probs, keep=0.35, pad=0.2)
    removed = (len(audio) - len(out)) / RATE
    assert 2.0 < removed < 3.0                  # из трёх секунд тишины осталось около 0,35 с + запас
    assert abs(rms(out) ** 2 * len(out) - rms(audio) ** 2 * len(audio)) / (rms(audio) ** 2 * len(audio)) < 0.01


def test_squeeze_without_voice_returns_audio_as_is():
    audio = np.zeros(RATE, np.float32)
    assert squeeze_pauses(audio, np.zeros(RATE // VAD_HOP)) is audio


def test_split_cuts_in_pauses_and_keeps_everything():
    audio, _ = frames_for([(14, True), (0.6, False), (16, True), (0.6, False), (14, True)])
    parts = split_at_pauses(audio, min_s=12, max_s=20)
    assert np.array_equal(np.concatenate(parts), audio)
    assert all(len(p) <= 20 * RATE for p in parts)
    for p in parts[:-1]:                        # разрез — в паузе, а не посреди звука
        assert rms(p[-200:]) < 0.01


def test_short_audio_is_not_split():
    audio = tone(300, 5)
    assert len(split_at_pauses(audio)) == 1


def test_split_cuts_in_the_middle_of_the_longest_pause():
    """С разметкой голоса режем посередине самой длинной паузы, даже если громче всего шумит именно там."""
    audio, probs = frames_for([(13, True), (0.3, False), (2, True), (1.0, False), (6, True)])
    audio = audio + np.random.default_rng(0).normal(0, 0.05, len(audio)).astype(np.float32)   # громкий шум
    cut = len(split_at_pauses(audio, 12, 20, probs)[0]) / RATE
    assert abs(cut - (13 + 0.3 + 2 + 0.5)) < 0.1
