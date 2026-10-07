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


def run_squelch(audio, **kw):
    clock = {"t": 1000.0}
    sq = NoiseSquelch(clock=lambda: clock["t"], **kw)
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


def test_radio_voice_removes_ctcss_and_hiss_and_sets_loudness():
    from sizeup.stt import TARGET_DBFS, radio_voice
    # Measured on real FG1 clips: CTCSS + hiss louder than the voice, speech ~ -30 dBFS.
    raw = keyed(0.05 * voice_like(3.0)) + 0.05 * rng.standard_normal(RATE * 3)
    raw[100] = 0.9  # a key-up click must not turn the whole clip down
    y = radio_voice(raw)
    spec, f = np.abs(np.fft.rfft(y)), np.fft.rfftfreq(len(y), 1 / RATE)
    assert spec[np.abs(f - 156.7) < 3].max() < 0.05 * spec.max()          # sub-audible tone gone
    blk = 320
    e = np.sqrt((y[: len(y) // blk * blk].reshape(-1, blk) ** 2).mean(axis=1))
    db = 20 * np.log10(e + 1e-12)
    act = e[db > np.percentile(db, 5) + 8]                    # the voice blocks (all, if no pauses)
    speech = 20 * np.log10(np.sqrt(np.mean((act if len(act) else e) ** 2)))
    assert abs(speech - TARGET_DBFS) < 1.5 and np.abs(y).max() <= 1.0


def test_segments_are_trimmed_to_the_carrier_and_clicks_have_no_speech():
    from sizeup.radio.fm import speech_seconds
    # FG1 bench: the loudest part of every clip was hiss before key-up and the squelch tail.
    def burst(s=0.2):  # MDC-1200 PTT-ID on release: 1200 baud MSK, 1200/1800 Hz (as on the FG1 bench)
        bits = np.random.default_rng(5).integers(0, 2, int(1200 * s))
        f = np.repeat(np.where(bits, 1200.0, 1800.0), RATE // 1200 + 1)[: int(RATE * s)]
        return 0.6 * np.sin(2 * np.pi * np.cumsum(f) / RATE)

    talk = np.concatenate([np.zeros(int(0.3 * RATE)), voice_like(2.0)])   # key up, then talk
    hold = np.zeros(int(0.15 * RATE))   # carrier held a moment after the ID (14:03:50 click on the bench)
    segs = run_squelch(np.concatenate([hiss(3.0), keyed(np.concatenate([np.zeros(int(0.9 * RATE)), burst(), hold])),
                                       hiss(1.5), keyed(np.concatenate([talk, burst(), hold])), hiss(1.5)]))
    assert len(segs) == 2
    click, speech = segs
    for s in segs:
        edge = np.sqrt(np.mean(s.audio[:FRAME] ** 2)), np.sqrt(np.mean(s.audio[-FRAME:] ** 2))
        assert max(edge) < 0.3, "hiss left at the edges"          # hiss rms is 0.4, speech ~0.25
    assert speech_seconds(click.audio) < 0.15                       # a mic click: dead air only
    assert speech_seconds(speech.audio) > 0.5
    assert len(click.bursts) == 1 and len(speech.bursts) == 1        # the PTT-IDs, kept for decoding


def test_noise_reduction_removes_steady_whine_not_speech():
    from sizeup.stt import radio_voice
    # FG1 bench: a comb of steady tones 100 Hz apart around 3 kHz ran through every transmission.
    t = np.arange(RATE * 3) / RATE
    whine = sum(0.01 * np.sin(2 * np.pi * (2545 + 100 * k) * t) for k in range(8))
    talk = np.concatenate([np.zeros(int(0.8 * RATE)), 0.1 * voice_like(2.2)])
    raw = keyed(talk + whine)

    def whine_vs_speech(y):
        n = 2048
        spec = np.abs(np.fft.rfft(y[:int(0.7 * RATE)][:n] * np.hanning(n)))   # dead air before speech
        f = np.fft.rfftfreq(n, 1 / RATE)
        return 20 * np.log10(spec[(f > 2500) & (f < 3400)].max() + 1e-12)

    off, on = radio_voice(raw, nr=False), radio_voice(raw)
    assert whine_vs_speech(off) - whine_vs_speech(on) > 12      # speech held at the same loudness


def test_long_page_with_quieting_dips_is_kept_whole():
    # South 2, 2026-10-01 15:59: a ~60 s page was cut at 17 s because quieting dipped under the opening
    # level (still above the hold level) for the rest of the transmission.
    hold = 10 ** (-8 / 20)  # quieting ~8 dB: between close (6) and open (10)
    weak = (voice_like(30.0) + 0.4 * hold * rng.standard_normal(RATE * 30))
    t = np.arange(len(weak)) / RATE
    weak = weak + 0.1 * np.sin(2 * np.pi * 156.7 * t)
    tx = np.concatenate([keyed(np.concatenate([tone(741.3, 1.0), tone(623.7, 3.0), voice_like(10.0)])), weak])
    segs = run_squelch(np.concatenate([hiss(3.0), tx, hiss(2.0)]))
    assert len(segs) == 1 and segs[0].end - segs[0].start > 40



def mdc_burst(s=0.2):  # MDC-1200 PTT-ID: 1200 baud MSK, 1200/1800 Hz
    bits = np.random.default_rng(5).integers(0, 2, int(1200 * s))
    f = np.repeat(np.where(bits, 1200.0, 1800.0), RATE // 1200 + 1)[: int(RATE * s)]
    return 0.6 * np.sin(2 * np.pi * np.cumsum(f) / RATE)


def strong(audio):      # a PCFD portable nearby: ~45 dB quieting, no CTCSS (FG1 carries none)
    return audio + 0.002 * rng.standard_normal(len(audio))


def dead(s):
    return np.zeros(int(RATE * s))


def background(s):      # another, weaker carrier on the channel: ~26 dB quieting, nothing on it
    return 0.02 * rng.standard_normal(int(RATE * s))


def test_a_weaker_carrier_under_ours_does_not_hold_the_squelch_open():
    # FG1 2026-10-07 17:17-17:21: short key-ups were logged as 21-28 s. PCFD radios quieted the
    # receiver ~40 dB; something else on the frequency held it at 21-28 dB for 20-30 s afterwards.
    audio = np.concatenate([hiss(3.0), strong(np.concatenate([dead(0.3), voice_like(2.0), mdc_burst(), dead(0.15)])),
                            background(20.0),
                            hiss(2.0), strong(voice_like(1.5)), hiss(2.0)])
    segs = run_squelch(audio, drop_db=10)
    assert len(segs) == 2                                  # the two key-ups; the background is not one
    assert 2.0 < segs[0].end - segs[0].start < 2.9 and len(segs[0].bursts) == 1
    assert 1.2 < segs[1].end - segs[1].start < 2.0
    # Without drop_db (paging channels) the old behaviour stands: one long transmission.
    assert run_squelch(audio)[0].end - run_squelch(audio)[0].start > 20


def test_a_key_up_over_a_background_carrier_is_its_own_transmission():
    # FG1 17:19:39: the squelch opened on the background, and two real key-ups (with their MDC IDs)
    # 10 s and 17 s in were buried in one 21 s "transmission".
    ku = strong(np.concatenate([dead(0.4), mdc_burst(), dead(0.2), voice_like(2.0), mdc_burst(), dead(0.15)]))
    segs = run_squelch(np.concatenate([hiss(3.0), background(6.0), ku, background(5.0), ku, background(3.0),
                                       hiss(2.0)]), drop_db=10)
    assert len(segs) == 2                                  # the background alone (no ID) is not a key-up
    for s in segs:
        assert 1.8 < s.end - s.start < 2.6 and len(s.bursts) == 2      # voice only: the IDs are cut off


def test_a_background_carrier_alone_does_not_hold_the_squelch_open():
    # FG1 17:17 and 17:20: the background opened the squelch by itself (no PTT-ID, nothing said) and
    # held RX lit for 23-28 s. After 4 s it's taken as the background; a key-up over it still opens.
    ku = strong(np.concatenate([dead(0.4), mdc_burst(), dead(0.2), voice_like(1.5), mdc_burst(), dead(0.15)]))
    clock, sq, lit, segs = {"t": 0.0}, None, 0, []
    sq = NoiseSquelch(clock=lambda: clock["t"], drop_db=10)
    audio = np.concatenate([hiss(3.0), background(12.0), ku, background(8.0), hiss(2.0)])
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        if (s := sq.feed(audio[i:i + FRAME])) is not None:
            segs.append(s)
        lit += sq.open
    assert len(segs) == 1 and len(segs[0].bursts) == 2
    assert lit * FRAME / RATE < 4.5 + 3.0           # 4 s to learn the background, then the key-up only
    # A radio without an ID that talks is kept, however long.
    talk = np.concatenate([np.concatenate([dead(0.3), voice_like(1.3)]) for _ in range(5)])  # pauses between words
    segs = run_squelch(np.concatenate([hiss(3.0), strong(talk), hiss(2.0)]), drop_db=10)
    assert len(segs) == 1 and segs[0].end - segs[0].start > 7.5
