"""stt.notch_lines cuts each steady tone on the bins round it only, not by a test over the whole spectrum per tone: the
same result to the bit (the old code is kept here as the reference) on synthetic audio of several lengths and sample
rates, with tones at the band's edges, at DC and Nyquist, and next to each other."""

from __future__ import annotations

import numpy as np
import pytest

from sizeup import stt


def notch_lines_reference(X: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, list[float]]:
    """stt.notch_lines as it was: the notch test over every bin, once per line."""
    df = f[1] - f[0] if len(f) > 1 else 1.0
    mag_db = 20 * np.log10(np.abs(X) + 1e-12)
    w = max(3, int(40.0 / df))
    c = np.concatenate([[0.0], np.cumsum(mag_db)])
    lo = np.clip(np.arange(len(f)) - w, 0, len(f))
    hi = np.clip(np.arange(len(f)) + w + 1, 0, len(f))
    baseline = (c[hi] - c[lo]) / (hi - lo)
    cand = (f >= stt.LINE_BAND[0]) & (f <= stt.LINE_BAND[1]) & (mag_db - baseline > stt.LINE_OVER_DB)
    lines: list[float] = []
    gain = np.ones(len(f))
    for i in np.where(cand)[0]:
        if mag_db[i] < mag_db[max(0, i - 2):i + 3].max():
            continue
        lines.append(float(f[i]))
        gain[np.abs(f - f[i]) <= stt.LINE_HALF_HZ] = 0.0
    return X * gain, lines


def whine(seconds: float, rate: int, tones: list[float], seed: int = 1) -> np.ndarray:
    """A made-up transmission: noise, a voice-like buzz whose pitch wanders, and steady tones (the whine)."""
    rng = np.random.default_rng(seed)
    n = max(1, int(seconds * rate))
    t = np.arange(n) / rate
    pitch = 120 + 30 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / rate
    voice = sum(np.sin(k * phase) / k for k in range(1, 25)) * 0.2
    x = 0.05 * rng.standard_normal(n) + voice
    for k, hz in enumerate(tones):
        x += 0.03 * np.sin(2 * np.pi * hz * t + k)
    return x.astype(np.float32)


COMB = [2500.0 + 100 * k for k in range(30)]                      # 2.5-5.4 kHz, 100 Hz apart (the FG1 whine)
EDGES = [1500.0, 1503.0, 1509.0, 5491.0, 5497.0, 5500.0]           # the band's edges, and lines a few Hz apart
NEAR = [3000.0, 3002.5, 3006.0, 3011.0, 4200.0, 4204.9, 4210.1]    # notches that touch or overlap


def same(x, rate):
    X = np.fft.rfft(x.astype(np.float64) - float(np.mean(x)))
    f = np.fft.rfftfreq(len(x), 1 / rate)
    new, new_lines = stt.notch_lines(X, f)
    old, old_lines = notch_lines_reference(X, f)
    assert new_lines == old_lines
    assert np.array_equal(new, old)
    assert np.array_equal(new == 0, old == 0)        # (the same bins cut)
    return new_lines


@pytest.mark.parametrize("rate", [8000, 11025, 16000, 22050])
@pytest.mark.parametrize("seconds", [0.05, 0.9, 7.3, 31.0])
def test_the_notch_is_bit_identical_to_the_whole_spectrum_one(rate, seconds):
    tones = [hz for hz in COMB + EDGES + NEAR if hz < rate / 2]
    lines = same(whine(seconds, rate, tones, seed=int(seconds * 10) + rate), rate)
    if seconds >= 7:
        assert len(lines) > 10                        # (the whine is there to cut)


def test_a_two_minute_clip_is_bit_identical():
    same(whine(119.0, 16000, COMB + EDGES + NEAR), 16000)


@pytest.mark.parametrize("rate", [8000, 16000])
def test_lines_at_dc_and_nyquist(rate, monkeypatch):
    """With the band opened to the whole spectrum, lines in the first and last bins: the window clipped at both ends."""
    monkeypatch.setattr(stt, "LINE_BAND", (0.0, rate / 2))
    n = rate * 3
    f = np.fft.rfftfreq(n, 1 / rate)
    rng = np.random.default_rng(7)
    X = (rng.standard_normal(len(f)) + 1j * rng.standard_normal(len(f))) * 0.01
    for i in (0, 1, 2, 4, 7, 15, len(f) // 2, len(f) // 2 + 3, len(f) - 16, len(f) - 5, len(f) - 2, len(f) - 1):
        X[i] = 1000.0
    new, new_lines = stt.notch_lines(X, f)
    old, old_lines = notch_lines_reference(X, f)
    assert new_lines == old_lines and f[0] in new_lines and f[-1] in new_lines
    assert np.array_equal(new, old)


@pytest.mark.parametrize("n", [1, 2, 3, 10, 64, 65])
def test_tiny_spectra(n, monkeypatch):
    monkeypatch.setattr(stt, "LINE_BAND", (0.0, 1e9))
    f = np.fft.rfftfreq(n, 1 / 16000)
    X = np.zeros(len(f), complex) + 1e-3
    X[len(f) // 2] = 50.0
    new, new_lines = stt.notch_lines(X, f)
    old, old_lines = notch_lines_reference(X, f)
    assert new_lines == old_lines and np.array_equal(new, old)


@pytest.mark.parametrize("rate, seconds, deemph", [(8000, 4.0, True), (16000, 12.5, True), (16000, 60.0, False),
                                                    (11025, 2.2, True)])
def test_radio_voice_is_bit_identical(rate, seconds, deemph, monkeypatch):
    x = whine(seconds, rate, [hz for hz in COMB + EDGES + NEAR if hz < rate / 2], seed=3)
    new = stt.radio_voice(x, rate, deemph=deemph, nr=True)
    monkeypatch.setattr(stt, "notch_lines", notch_lines_reference)
    old = stt.radio_voice(x, rate, deemph=deemph, nr=True)
    assert new.dtype == old.dtype and np.array_equal(new, old)
