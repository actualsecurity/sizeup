import numpy as np
import pytest

from sizeup.radio.tones import ToneCode, ToneDetector, TonePlan

RATE = 16000
rng = np.random.default_rng(7)


def tone(hz, s, amp=0.5):
    t = np.arange(int(RATE * s)) / RATE
    return amp * np.sin(2 * np.pi * hz * t + rng.uniform(0, 6.28))


def gap(s, amp=0.0):
    return amp * rng.standard_normal(int(RATE * s))


def voice_like(s):
    """Speech stand-in: several drifting harmonics + noise, amplitude-modulated."""
    t = np.arange(int(RATE * s)) / RATE
    f0 = 140 + 30 * np.sin(2 * np.pi * 1.3 * t)
    phase = 2 * np.pi * np.cumsum(f0) / RATE
    x = sum(np.sin(k * phase) / k for k in range(1, 12))
    return 0.3 * x * (0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 3 * t))) + 0.05 * rng.standard_normal(len(t))


def decode(audio, chunk=None, noise=0.02):
    audio = audio + noise * rng.standard_normal(len(audio))
    d = ToneDetector(RATE)
    out = []
    if chunk is None:
        out += d.feed(audio)
    else:
        for i in range(0, len(audio), chunk):
            out += d.feed(audio[i:i + chunk])
    return out + d.flush()


def test_two_tone_page_then_voice():
    ev = decode(np.concatenate([gap(0.5), tone(1084.0, 1.0), tone(1530.0, 3.0), gap(0.3), voice_like(4.0)]))
    assert len(ev) == 1 and ev[0].kind == "two_tone"
    assert ev[0].a_hz == pytest.approx(1084.0, abs=2) and ev[0].b_hz == pytest.approx(1530.0, abs=2)
    assert ev[0].start == pytest.approx(0.5, abs=0.1) and ev[0].end == pytest.approx(4.5, abs=0.1)


def test_stacked_pages_in_odd_chunks():
    audio = np.concatenate([tone(669.9, 1.0), tone(1092.4, 3.0), tone(788.5, 1.0), tone(1153.4, 3.0), gap(1.0)])
    ev = decode(audio, chunk=777)
    assert len(ev) == 2
    for e, (a, b) in zip(ev, [(669.9, 1092.4), (788.5, 1153.4)]):
        assert e.a_hz == pytest.approx(a, abs=2) and e.b_hz == pytest.approx(b, abs=2)


def test_long_tone_and_voice_alone():
    ev = decode(np.concatenate([tone(1200.0, 8.0), gap(0.5)]))
    assert len(ev) == 1 and ev[0].kind == "long_tone" and ev[0].a_hz == pytest.approx(1200, abs=2)
    assert decode(voice_like(10.0)) == []
    assert decode(gap(5.0, amp=0.3)) == []  # static


def test_plan_matches_within_tolerance():
    plan = TonePlan([ToneCode("Port Chester FD", 1084.0, 1530.0, "PCFD"),
                     ToneCode("Rye Brook FD", 669.9, 1092.4, "RBFD"),
                     ToneCode("County all call", 1200.0, None)])
    ev = plan.match_all(decode(np.concatenate([tone(1090.0, 1.0), tone(1522.0, 3.0), tone(1200.0, 6.0)])))
    assert [e.names for e in ev] == [("Port Chester FD",), ("County all call",)]
    ev = plan.match_all(decode(np.concatenate([tone(1150.0, 1.0), tone(1530.0, 3.0)])))
    assert ev[0].names == ()  # A is 6 % off: someone else's page


def test_same_frequency_code_and_county_list():
    # Larchmont FD / New Rochelle: A = B (A 1 s + B 3 s at one frequency merge into one ~4 s tone).
    from sizeup.config import load
    from pathlib import Path
    ev = decode(np.concatenate([tone(435.1, 1.0), tone(435.1, 3.0), gap(0.5)]))
    assert len(ev) == 1 and ev[0].kind == "two_tone" and ev[0].a_hz == pytest.approx(435.1, abs=2)
    assert ev[0].b_hz == pytest.approx(ev[0].a_hz)
    root = Path(__file__).parent.parent
    plan = TonePlan(list(load(root / "config.example.toml").tones))
    assert plan.match(ev[0]).names == ("Larchmont FD", "Peekskill")
    # Real South 2 pages 2026-10-01: PCFD's code, an estimated-list match, codes learned on the air,
    # and a pair nobody has (stays unknown).
    from sizeup.radio.tones import ToneEvent
    assert plan.match(ToneEvent("two_tone", 349.1, 311.1, 0, 1)).names == ("Port Chester FD",)
    assert plan.match(ToneEvent("two_tone", 496.8, 767.4, 0, 1)).names == ("Eastchester VAC (heard)",)  # 16:00 "EVAC"
    assert plan.match(ToneEvent("two_tone", 928.1, 810.2, 0, 1)).names == ("Scarsdale EMS (heard)",)
    assert plan.match(ToneEvent("two_tone", 741.3, 623.7, 0, 1)).names == ()  # heard, owner not known yet
    assert plan.match(ToneEvent("two_tone", 1700.0, 1100.0, 0, 1)).names == ()
