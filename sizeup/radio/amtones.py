"""Steady tones out of an airband (AM) transmission (#263), before the clip and speech-to-text.

Some HPN Tower transmissions carry a low tone over the voice, a different pitch on each one. On 50 real clips
(the hub's, restored read-only): 21 had one, 245-605 Hz (16 of them 270-352 Hz), with its 2nd harmonic on 9 and its
3rd on 1; its level from 23 dB under the rest of the voice band to 16 dB over it (median: as loud as the voice), on
the whole transmission, steady to 1-3 Hz across it and drifting slowly upward from one transmission to the next
from the same aircraft (300.0 -> 300.9, 302.7 -> 304.1, 305.6 -> 306.0 Hz over 16 s), on pilots' transmissions:
most likely a heterodyne, the AM detector's beat between the aircraft's carrier and a second, steady one near the
channel, its pitch set by how far that aircraft's transmitter is off frequency (an oscillator warming up drifts like
that). Not hum (not on multiples of 60 Hz, and it drifts) and not the AGC (the tone's level holds to a few dB). The
strongest tower transmissions had none (an on-frequency carrier would beat under 300 Hz, which radio_voice cuts).

A high-pass can't take it: it sits in the voice band (300-3,000 Hz), and radio_voice (sizeup/stt.py) already rolls
off under 300 Hz. So the tone is found and notched:

    find      a short-time spectrum (128 ms frames, 32 ms apart, 7.8 Hz bins at 16 kHz): in each frame, the peaks
              TONE_OVER_DB over the median of their +/-80 Hz neighbourhood. A tone is such a peak (+/-1 bin) in
              TONE_PRESENT of the frames of the clip's carrier (not its pre-roll and tail: #263), or of any
              TONE_WINDOW_S of it (a tone on part of a transmission),
              and steady: its frequency, frame to frame, within TONE_SPREAD_HZ (p10-p90). Voice doesn't pass: its
              harmonics move with the pitch and stop between words.
    notch     each tone (TONE_MAX at most, the strongest first) with a notch of Q TONE_Q (10 Hz wide at 300 Hz),
              widened to the tone's own drift, applied zero-phase (|H|^2, as a forward-backward IIR notch) on the
              whole clip's spectrum, padded so it doesn't wrap. Nothing found: the audio comes back untouched.

numpy only (the station's venv has it). About 6 ms per second of audio (the bench Mac), at the segment's close.
"""

from __future__ import annotations

import numpy as np

TONE_BAND = (150.0, 3500.0)    # where a tone is looked for (Hz)
TONE_NFFT = 2048               # 128 ms at 16 kHz: 7.8 Hz bins
TONE_HOP = 512                 # 32 ms
TONE_NEIGH_HZ = 80.0           # a peak is measured against the median of this much either side
TONE_OVER_DB = 10.0            # ... and is this far over it
TONE_PRESENT = 0.6             # a tone is a peak in this share of the frames (of the clip or of one window) ...
TONE_WINDOW_S = 2.0            # ... the window
TONE_SPREAD_HZ = 12.0          # ... and steady: its frequency within this (p10-p90) where it is
TONE_MIN_S = 0.5               # and there this long at least
TONE_MAX = 6                   # notches at most
TONE_Q = 30.0                  # notch Q: f / Q wide (-3 dB), 10 Hz at 300 Hz


def _frames(x: np.ndarray, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Power spectrogram (frames x bins) and the bin frequencies."""
    n = TONE_NFFT
    if len(x) < n:
        x = np.concatenate((x, np.zeros(n - len(x))))
    nf = 1 + (len(x) - n) // TONE_HOP
    idx = np.arange(n)[None, :] + TONE_HOP * np.arange(nf)[:, None]
    P = np.abs(np.fft.rfft(x[idx] * np.hanning(n), axis=1)) ** 2 + 1e-20
    return P, np.fft.rfftfreq(n, 1 / rate)


def find_tones(audio: np.ndarray, rate: int = 16000, span: tuple[int, int] | None = None) -> list[tuple[float, float]]:
    """The steady tones in a clip: [(frequency Hz, its drift Hz p10-p90)], the strongest first, TONE_MAX at most.
    span: (first, end) sample of the carrier, where a beat can be (#263): looked for there only, so the pre-roll and
    the tail (0.6 s of no carrier, a third of a 1.9 s transmission) don't hold its share of the frames under
    TONE_PRESENT."""
    x = np.asarray(audio, dtype=np.float64)
    if span is not None:
        x = x[max(0, int(span[0])):max(0, int(span[1]))]
    if len(x) < TONE_MIN_S * rate:
        return []
    P, f = _frames(x - x.mean(), rate)
    keep = (f >= TONE_BAND[0] - 2 * TONE_NEIGH_HZ) & (f <= TONE_BAND[1] + 2 * TONE_NEIGH_HZ)
    P, f = P[:, keep], f[keep]                          # the band and its neighbourhood: the rest isn't looked at
    nfr = P.shape[0]
    D = 10 * np.log10(P)
    df = f[1] - f[0]
    k = max(2, int(round(TONE_NEIGH_HZ / df)))
    pad = np.pad(D, ((0, 0), (k, k)), mode="edge")
    base = np.median(np.lib.stride_tricks.sliding_window_view(pad, 2 * k + 1, axis=1), axis=2)
    pk = np.zeros(D.shape, bool)
    pk[:, 1:-1] = (D[:, 1:-1] >= D[:, :-2]) & (D[:, 1:-1] >= D[:, 2:]) & (D[:, 1:-1] - base[:, 1:-1] > TONE_OVER_DB)
    near = pk.copy()                                    # a peak within +/-1 bin (a tone between two bins, or drifting)
    near[:, 1:] |= pk[:, :-1]
    near[:, :-1] |= pk[:, 1:]
    inband = (f >= TONE_BAND[0]) & (f <= TONE_BAND[1])
    # present in TONE_PRESENT of the frames: of the whole clip, or of any TONE_WINDOW_S of it
    w = min(nfr, max(1, int(round(TONE_WINDOW_S * rate / TONE_HOP))))
    c = np.concatenate((np.zeros((1, near.shape[1])), np.cumsum(near, axis=0)))
    share = ((c[w:] - c[:-w]) / w).max(axis=0)
    share = np.maximum(share, near.mean(axis=0))
    need = max(TONE_MIN_S * rate / TONE_HOP, 1)
    cand = np.where(inband & (share >= TONE_PRESENT) & (near.sum(axis=0) >= need))[0]
    if not len(cand):
        return []
    level = (P * near).sum(axis=0)                      # the tone's power where it is: the strongest first
    found: list[tuple[float, float, float]] = []
    taken: list[int] = []
    for i in sorted(cand, key=lambda i: -level[i]):
        if any(abs(i - j) <= 3 for j in taken):
            continue
        taken.append(i)
        rows = np.where(near[:, i])[0]
        lo, hi = max(1, i - 2), min(len(f) - 2, i + 2)
        j = lo + np.argmax(D[rows, lo:hi + 1], axis=1)        # its bin, frame by frame
        a, b, cc = D[rows, j - 1], D[rows, j], D[rows, j + 1]
        den = a - 2 * b + cc
        frac = np.where(np.abs(den) > 1e-9, 0.5 * (a - cc) / np.where(np.abs(den) > 1e-9, den, 1), 0.0)
        track = f[j] + np.clip(frac, -1, 1) * df
        p10, p50, p90 = np.percentile(track, (10, 50, 90))
        if p90 - p10 > TONE_SPREAD_HZ:
            continue                                     # it moves: voice, not a tone
        found.append((float(level[i]), float(p50), float(p90 - p10)))
        if len(found) >= TONE_MAX:
            break
    return [(fr, sp) for _l, fr, sp in sorted(found, reverse=True)]


def notch_gain(f: np.ndarray, tones: list[tuple[float, float]], q: float = TONE_Q) -> np.ndarray:
    """The zero-phase gain of the notches at frequencies f: each a 2nd-order notch (bandwidth f0/q, or the tone's
    drift and a bin more, whichever is wider), squared (forward and backward)."""
    g = np.ones(len(f))
    w = np.asarray(f, dtype=np.float64)
    for f0, spread in tones:
        bw = max(f0 / q, spread + 4.0)
        num = (f0 * f0 - w * w) ** 2
        g *= num / (num + (w * bw) ** 2 + 1e-30)
    return g


def detone(audio: np.ndarray, rate: int = 16000, tones: list[tuple[float, float]] | None = None) -> np.ndarray:
    """The clip with its steady tones notched out (find_tones); unchanged (the same array) if it has none."""
    if tones is None:
        tones = find_tones(audio, rate)
    if not tones:
        return audio
    x = np.asarray(audio, dtype=np.float64)
    n = len(x)
    m = 1 << int(np.ceil(np.log2(n + rate // 4)))       # padded: the notch's ringing (~Q/f0, 30 ms) doesn't wrap
    X = np.fft.rfft(x, m)
    y = np.fft.irfft(X * notch_gain(np.fft.rfftfreq(m, 1 / rate), tones), m)[:n]
    return y.astype(np.asarray(audio).dtype if np.asarray(audio).dtype.kind == "f" else np.float32)
