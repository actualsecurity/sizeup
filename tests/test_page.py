import numpy as np
import pytest

from sizeup.radio.fm import FRAME, RATE, NoiseSquelch
from sizeup.radio.page import assemble
from sizeup.radio.tones import ToneCode, TonePlan

rng = np.random.default_rng(11)
PLAN = TonePlan([ToneCode("Port Chester FD", 349.1, 311.1, "PCFD")])  # RadioReference wiki, unverified


def hiss(s, amp=0.4):
    """No carrier: the FM discriminator outputs wideband noise."""
    return amp * rng.standard_normal(int(RATE * s))


def keyed(audio, ctcss=156.7):
    """Carrier up: quiet, with the sub-audible CTCSS tone under the audio."""
    t = np.arange(len(audio)) / RATE
    return audio + 0.1 * np.sin(2 * np.pi * ctcss * t) + 0.02 * rng.standard_normal(len(audio))


def tone(hz, s, amp=0.5):
    t = np.arange(int(RATE * s)) / RATE
    return amp * np.sin(2 * np.pi * hz * t)


def voice_like(s):
    t = np.arange(int(RATE * s)) / RATE
    f0 = 130 + 25 * np.sin(2 * np.pi * 1.1 * t)
    ph = 2 * np.pi * np.cumsum(f0) / RATE
    return 0.3 * sum(np.sin(k * ph) / k for k in range(1, 14)) * (0.4 + 0.6 * np.abs(np.sin(2 * np.pi * 2.5 * t)))


def run_squelch(audio):
    clock = {"t": 1000.0}
    sq = NoiseSquelch(clock=lambda: clock["t"])
    segs = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        s = sq.feed(audio[i:i + FRAME])
        if s is not None:
            segs.append(s)
    return segs


def test_page_transmission_is_cut_out_and_split_into_tones_and_voice():
    tx = keyed(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice_like(6.0)]))
    segs = run_squelch(np.concatenate([hiss(4.0), tx, hiss(2.0)]))
    assert len(segs) == 1
    seg = segs[0]
    assert seg.end - seg.start == pytest.approx(10.2, abs=0.6) and seg.quieting_db > 15
    assert seg.start - 1000.0 == pytest.approx(4.0, abs=0.3)  # wall clock of key-up
    page = assemble("South 2", 470.2, seg, PLAN)
    assert page.kind == "page" and page.departments == ["Port Chester FD"] and page.status == "pending"
    assert len(page.voice) / RATE == pytest.approx(6.2, abs=0.5)  # the dispatcher's voice only


def test_voice_only_and_unknown_tones_are_kept():
    segs = run_squelch(np.concatenate([hiss(3.0), keyed(voice_like(4.0)), hiss(1.5),
                                       keyed(np.concatenate([tone(669.9, 1.0), tone(1092.4, 3.0)])), hiss(1.5)]))
    assert len(segs) == 2
    voice, tones_only = (assemble("South 2", 470.2, s, PLAN) for s in segs)
    assert voice.kind == "voice" and voice.tones == []
    assert tones_only.kind == "page" and tones_only.departments == [] and tones_only.status == "no_voice"
    d = tones_only.as_dict()["tones"][0]
    assert d["a_hz"] == pytest.approx(669.9, abs=2) and d["names"] == []  # logged for learning the codes


def test_idle_hiss_never_opens():
    assert run_squelch(hiss(60.0)) == []
