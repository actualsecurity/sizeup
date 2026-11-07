"""Airband steady tones (GitHub #263): a heterodyne's tone over an AM transmission is found and notched out
(sizeup/radio/amtones.py), the voice kept, before the clip and speech-to-text on every airband channel
(sizeup/radio/am.py CarrierSquelch). Synthetic audio only: a speech-like voice (a moving pitch, its harmonics shaped
by formants, syllables and gaps), tones, hum and noise."""

from __future__ import annotations

import time

import numpy as np
import pytest

from sizeup.radio import amtones
from sizeup.radio.am import CarrierSquelch
from sizeup.radio.amtones import detone, find_tones, notch_gain
from sizeup.radio.fm import FRAME, RATE
from sizeup.stt import radio_voice

T0 = 1_800_000_000.0


def voice(seconds: float, seed: int = 0, f0: float = 120.0) -> np.ndarray:
    """Speech-like: a glottal pitch moving with intonation (+/-25%) and jitter, harmonics to 3.5 kHz weighted by three
    formants that move per syllable, syllables ~4 a second with gaps between words. RMS ~0.1 while talking."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    pitch = f0 * (1 + 0.25 * np.sin(2 * np.pi * 0.7 * t + rng.uniform(0, 6)) + 0.08 * np.sin(2 * np.pi * 3.1 * t))
    pitch *= 1 + 0.01 * np.convolve(rng.standard_normal(n), np.ones(400) / 20, "same")
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    syl = int(np.ceil(seconds * 4)) + 1
    formants = [np.repeat(rng.uniform(lo, hi, syl), RATE // 4)[:n] for lo, hi in ((300, 800), (900, 2200), (2300, 3000))]
    x = np.zeros(n)
    for h in range(1, 40):
        fh = h * pitch
        w = sum(1 / (1 + ((fh - F) / 120) ** 2) for F in formants) / (h ** 0.5)
        x += w * np.sin(h * phase) * (fh < 3500)
    env = np.repeat((rng.uniform(size=syl) > 0.2).astype(float), RATE // 4)[:n]           # some syllables silent
    env = np.convolve(env * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t) ** 2), np.ones(160) / 160, "same")
    x = x * env
    return 0.1 * x / np.sqrt(np.mean(x[env > 0.3] ** 2))


def tone(seconds: float, f: float, amp: float, drift_hz_s: float = 0.0, start: float = 0.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    y = amp * np.sin(2 * np.pi * (f * t + 0.5 * drift_hz_s * t * t))
    y[t < start] = 0
    return y


def power_at(x: np.ndarray, f: float, half: float = 3.0) -> float:
    X = np.abs(np.fft.rfft(x)) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / RATE)
    return float(X[np.abs(fr - f) <= half].sum())


def band_power(x: np.ndarray, lo: float = 300, hi: float = 3000, skip: tuple = (), half: float = 25.0) -> float:
    X = np.abs(np.fft.rfft(x)) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / RATE)
    m = (fr >= lo) & (fr <= hi)
    for f in skip:
        m &= np.abs(fr - f) > half
    return float(X[m].sum())


def db(a: float, b: float) -> float:
    return 10 * np.log10(a / b)


# -- found and notched, the voice kept ---------------------------------------------------------------------------------


@pytest.mark.parametrize("f", [180.0, 255.0, 300.5, 342.0, 420.0, 604.0, 987.0, 1500.0, 2400.0])
@pytest.mark.parametrize("rel_db", [10.0, 0.0, -12.0])
def test_a_steady_tone_is_notched_and_the_voice_kept(f, rel_db):
    """The real clips' tones were 245-605 Hz at 23 dB under to 16 dB over the voice; anywhere 150-3500 Hz is looked at."""
    v = voice(4.0, seed=int(f))
    amp = 0.1 * np.sqrt(2) * 10 ** (rel_db / 20)                      # the tone's RMS rel_db over the voice's
    x = v + tone(4.0, f, amp) + 0.002 * np.random.default_rng(1).standard_normal(len(v))
    got = find_tones(x)
    assert got and abs(got[0][0] - f) < 1.5, got
    y = detone(x, tones=got)
    assert db(power_at(x, f), power_at(y, f)) > 20                     # the tone: over 20 dB down
    assert abs(db(band_power(y, skip=[f]), band_power(x, skip=[f]))) < 0.3   # the voice band around it: kept
    assert abs(db(band_power(y, skip=[f], half=6), band_power(v, skip=[f], half=6))) < 0.5   # ... and what is left is the voice


def test_a_tone_and_its_harmonics_from_the_am_detector():
    """A strong beat's envelope isn't a sine: its 2nd and 3rd harmonics are notched too (the real clips: 329/658/987 Hz)."""
    v = voice(3.0, seed=3)
    x = v + tone(3.0, 329.0, 0.2) + tone(3.0, 658.0, 0.05) + tone(3.0, 987.0, 0.02)
    got = sorted(f for f, _s in find_tones(x))
    assert len(got) == 3 and np.allclose(got, [329, 658, 987], atol=1.5)
    y = detone(x)
    for f in (329.0, 658.0, 987.0):
        assert db(power_at(x, f), power_at(y, f)) > 20


def test_a_tone_drifting_through_the_transmission():
    """The oscillator warming: the real clips drifted 1-3 Hz across one. The notch widens to the drift."""
    v = voice(4.0, seed=4)
    x = v + tone(4.0, 300.0, 0.15, drift_hz_s=1.0)                    # 300 -> 304 Hz
    (f, spread), = find_tones(x)
    assert 300 < f < 304 and 1 < spread < 6
    y = detone(x)
    assert db(power_at(x, 302, 4), power_at(y, 302, 4)) > 20
    assert abs(db(band_power(y, skip=[302], half=30), band_power(x, skip=[302], half=30))) < 0.3


def test_a_tone_on_part_of_the_transmission():
    """A tone that comes up halfway through a long transmission (a second carrier keyed late) is still found."""
    v = voice(8.0, seed=5)
    x = v + tone(8.0, 450.0, 0.2, start=5.0)                           # the last 3 s of 8
    got = find_tones(x)
    assert got and abs(got[0][0] - 450) < 1.5
    assert db(power_at(x, 450), power_at(detone(x), 450)) > 20


def test_hum_is_cut_by_the_high_pass_and_the_notches():
    """60 Hz hum and its family: radio_voice's high-pass (gone under 200 Hz, full from 300) takes the low ones, the
    notches the ones in the voice band. (No hum family was seen on the real clips.)"""
    v = voice(4.0, seed=6)
    hum = sum(tone(4.0, 60.0 * k, 0.05) for k in range(1, 8))          # 60 .. 420 Hz, each 3 dB under the voice
    x = v + hum
    got = {round(f / 60) for f, _s in find_tones(x)}
    assert {3, 4, 5, 6, 7} <= got                                      # 180 .. 420 Hz notched (150+ is looked at)
    before, after = radio_voice(x, deemph=False, nr=False), radio_voice(detone(x), deemph=False, nr=False)
    ref = radio_voice(v, deemph=False, nr=False)
    for k in range(1, 8):
        f = 60.0 * k
        own = db(power_at(ref, f), band_power(ref))                          # the voice's own energy there
        assert db(power_at(after, f), band_power(after)) < max(-40, own + 5)  # each line: gone, down to the voice
        if k >= 4:                                                         # (over 200 Hz: the high-pass passes some)
            assert db(power_at(before, f), band_power(before)) > -30         # ... and they were there without the notch
    assert abs(db(band_power(after, 500, 3000, skip=[420]), band_power(ref, 500, 3000, skip=[420]))) < 1.0


def test_the_high_pass_response():
    """radio_voice's low edge (sizeup/stt.py, before the clip and speech-to-text): nothing under 200 Hz, a raised cosine
    to full at 300 Hz; the voice band flat."""
    x = sum(tone(2.0, f, 0.05) for f in (100.0, 250.0, 500.0, 1000.0, 2000.0))
    y = radio_voice(x, deemph=False, nr=False).astype(float)
    ref = power_at(y, 1000)
    assert db(power_at(y, 100), ref) < -40
    assert -9 < db(power_at(y, 250), ref) < -3                         # half-way: 0.5 in amplitude (-6 dB)
    assert abs(db(power_at(y, 500), ref)) < 0.5 and abs(db(power_at(y, 2000), ref)) < 0.5


def test_the_notch_response():
    """Q 30: -6 dB (zero-phase, |H|^2) at f0 +/- f0/60, deep at f0, flat 50 Hz away."""
    f0 = 300.0
    f = np.array([f0, f0 - f0 / 60, f0 + f0 / 60, f0 - 50, f0 + 50, 1000.0])
    g = notch_gain(f, [(f0, 0.0)])
    assert g[0] < 1e-6
    assert g[1] == pytest.approx(0.5, abs=0.05) and g[2] == pytest.approx(0.5, abs=0.05)
    assert g[3] > 0.97 and g[4] > 0.97 and g[5] > 0.999
    assert notch_gain(np.array([f0 + 3.0]), [(f0, 8.0)])[0] < 0.2     # widened to a tone's drift
    assert amtones.TONE_Q == 30


# -- left alone ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("f0", [95.0, 120.0, 180.0, 230.0])
def test_no_notch_without_a_steady_tone(seed, f0):
    """Voice alone (low and high voices, wall to wall or with gaps), with hiss: nothing found, the audio untouched."""
    v = voice(5.0, seed=seed, f0=f0) + 0.003 * np.random.default_rng(seed).standard_normal(5 * RATE)
    assert find_tones(v) == []
    assert detone(v) is v


def test_no_notch_on_noise_silence_or_a_short_clip():
    rng = np.random.default_rng(9)
    assert find_tones(0.01 * rng.standard_normal(3 * RATE)) == []
    assert find_tones(np.zeros(3 * RATE)) == []
    assert find_tones(tone(0.3, 400.0, 0.2)) == []                     # under TONE_MIN_S
    assert find_tones(np.array([])) == []


def test_at_most_tone_max_notches():
    v = voice(3.0, seed=10)
    x = v + sum(tone(3.0, f, 0.1) for f in (260, 410, 730, 1130, 1570, 1990, 2430, 2890))
    assert len(find_tones(x)) == amtones.TONE_MAX


# -- in the AM chain ----------------------------------------------------------------------------------------------------


def test_the_carrier_squelch_notches_a_heterodyne_before_the_clip():
    """An aircraft's carrier beating 300 Hz with a second one: the transmission the squelch hands on (the clip, the
    source kept for re-runs, speech-to-text) has the tone notched out and says which (Segment.tones)."""
    rng = np.random.default_rng(11)
    talk = voice(3.0, seed=11)
    beat = 0.3 * np.sin(2 * np.pi * 300.0 * np.arange(len(talk)) / RATE)
    carrier = 0.05
    env = np.concatenate([0.01 + 0.0005 * rng.standard_normal(5 * RATE),
                          carrier * (1 + 0.5 * talk / 0.1 * 0.5 + beat) + 0.0005 * rng.standard_normal(len(talk)),
                          0.01 + 0.0005 * rng.standard_normal(2 * RATE)])
    sq = CarrierSquelch(3.5, clock=lambda: T0)
    segs = [s for k in range(len(env) // FRAME) if (s := sq.feed(env[k * FRAME:(k + 1) * FRAME])) is not None]
    assert len(segs) == 1
    s = segs[0]
    assert len(s.tones) == 1 and abs(s.tones[0] - 300.0) < 1.5
    assert power_at(s.audio, 300.0) < 0.01 * band_power(s.audio)        # the tone: gone from the clip
    plain = CarrierSquelch(3.5, clock=lambda: T0)
    env2 = env.copy()
    env2[5 * RATE:5 * RATE + len(talk)] = carrier * (1 + 0.5 * talk / 0.1 * 0.5)
    s2 = [s for k in range(len(env2) // FRAME) if (s := plain.feed(env2[k * FRAME:(k + 1) * FRAME])) is not None][0]
    assert s2.tones == []                                              # no beat: nothing notched


def _short_transmission(seconds: float, beat: float, seed: int) -> np.ndarray:
    """The real clips' shape (HPN Tower, 2026-11-07, e.g. 15:08:51: 1.9 s, a 361 Hz beat left in it): 5 s of no
    carrier, `seconds` of carrier with voice and a 361 Hz beat at `beat` x the carrier, then no carrier."""
    rng = np.random.default_rng(seed)
    talk = voice(seconds, seed=seed)
    b = beat * np.sin(2 * np.pi * 361.0 * np.arange(len(talk)) / RATE)
    return np.concatenate([0.01 + 0.0005 * rng.standard_normal(5 * RATE),
                           0.05 * (1 + 0.25 * talk / 0.1 + b) + 0.0005 * rng.standard_normal(len(talk)),
                           0.01 + 0.0005 * rng.standard_normal(2 * RATE)])


def _segment(env: np.ndarray):
    sq = CarrierSquelch(3.5, clock=lambda: T0)
    segs = [s for k in range(len(env) // FRAME) if (s := sq.feed(env[k * FRAME:(k + 1) * FRAME])) is not None]
    assert len(segs) == 1
    return segs[0]


def test_a_short_transmissions_tone_is_looked_for_on_its_carrier(monkeypatch):
    """The tone left in short transmissions (2026-11-07: 22 of 342 HPN Tower clips, median 1.95 s, after #267): the
    0.4 s pre-roll and 0.2 s tail have no carrier and so no beat, and on a 1.9 s clip they held the tone's share of the
    frames under TONE_PRESENT. It is looked for on the carrier now; the notch still covers the whole clip."""
    from sizeup.radio import am
    found = []
    for seed in range(6):
        s = _segment(_short_transmission(1.3, 0.1, seed))
        found.append(bool(s.tones) and abs(s.tones[0] - 361.0) < 1.5)
        if s.tones:
            assert power_at(s.audio, 361.0) < 0.01 * band_power(s.audio)
        assert s.preroll_s == pytest.approx(0.4)
    assert all(found)
    # as it was (the whole clip, pre-roll and tail in it): missed on half of them
    monkeypatch.setattr(am, "find_tones", lambda a, rate, span=None: find_tones(a, rate))
    old = [bool(_segment(_short_transmission(1.3, 0.1, seed)).tones) for seed in range(6)]
    assert sum(old) <= 3


@pytest.mark.parametrize("seconds", [0.8, 1.3, 2.0])
def test_a_short_transmission_without_a_tone_is_left_alone(seconds):
    for seed in range(6):
        assert _segment(_short_transmission(seconds, 0.0, seed)).tones == []


def test_the_span_is_where_tones_are_looked_for():
    """find_tones(span=): only that part is looked at (a tone outside it isn't found; one inside is)."""
    x = np.concatenate([tone(2.0, 400.0, 0.1), voice(2.0, seed=13)])
    assert find_tones(x, span=(2 * RATE, 4 * RATE)) == []
    got = find_tones(x, span=(0, 2 * RATE))
    assert got and abs(got[0][0] - 400.0) < 1.5
    assert find_tones(x, span=(0, RATE // 4)) == []                       # under TONE_MIN_S


# -- cheap ---------------------------------------------------------------------------------------------------------------


def test_it_is_cheap():
    """At segment close, on the receiver's thread: a 30 s transmission (the longest) in well under a second; about
    6 ms per second of audio on the bench Mac."""
    x = voice(30.0, seed=12) + tone(30.0, 310.0, 0.1)
    detone(x)                                                          # warm up
    # CPU time, not the wall clock: a loaded machine (pytest-xdist's workers) stretches the wall clock, not the work (#282)
    t0 = time.process_time()
    for _ in range(3):
        y = detone(x)
    per_s = (time.process_time() - t0) / 3 / 30.0
    assert per_s < 0.03, f"{per_s * 1000:.1f} ms per second of audio"
    assert len(y) == len(x)


def test_the_tones_notched_go_with_the_transmission(tmp_path):
    """The row says which tones were notched (sent to the hub in its traffic doc, and in the station's log line), so a
    tone heard in a clip can be checked against what the station took out."""
    from dataclasses import replace

    from sizeup.app import App
    from sizeup.radio.fm import Channel
    from tests.test_app import FakeWhisper, cfg, wait
    from tests.test_atc import FIVE

    app = App(replace(cfg(tmp_path), channels=FIVE))
    app.stt.client = FakeWhisper({k: "Westchester tower, JetBlue 1234, runway three four" for k in range(1, 6)})
    app.stt.start()
    s = _segment(_short_transmission(2.0, 0.2, 1))
    assert s.tones
    app._segment(Channel(118.575, "HPN Tower"), s)
    assert wait(lambda: any(x.source == "HPN Tower" for x in app.store.loose), 10)
    t = next(x for x in app.store.loose if x.source == "HPN Tower")
    assert t.tones == s.tones and "tones" in t.as_dict()
    plain = _segment(_short_transmission(2.0, 0.0, 2))
    plain.start += 60
    plain.end += 60
    app._segment(Channel(118.575, "HPN Tower"), plain)
    assert wait(lambda: sum(x.source == "HPN Tower" for x in app.store.loose) == 2, 10)
    t2 = max((x for x in app.store.loose if x.source == "HPN Tower"), key=lambda x: x.start)
    assert t2 is not t and "tones" not in t2.as_dict()
