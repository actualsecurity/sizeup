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


FG1_FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "fg1"


def wav(name):
    import wave
    with wave.open(str(FG1_FIXTURES / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float) / 32768


def carrier(n, q, hiss_amp=0.4):
    """Carrier noise that reads q dB of quieting against hiss(amp=hiss_amp)."""
    return hiss_amp * 10 ** (-q / 20) * rng.standard_normal(n)


def squelch_trace(audio, **kw):
    """(segments, per-frame open flags, the squelch) for audio fed through a NoiseSquelch."""
    clock = {"t": 0.0}
    sq = NoiseSquelch(clock=lambda: clock["t"], **kw)
    segs, lit = [], []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        if (s := sq.feed(audio[i:i + FRAME])) is not None:
            segs.append(s)
        lit.append(sq.open)
    return segs, np.array(lit), sq


@pytest.mark.parametrize("over_db", [16.0, 7.0])
def test_a_key_up_into_the_background_is_not_held_open_by_its_id(over_db):
    # FG1 2026-10-08 16:32:21: E63 keyed up (MDC post-ID 6301) and the background carrier came straight
    # in after it; RX stayed lit 51 s and the station logged "key-up (51.5 s, no speech) · unit 6301".
    # Replayed: the recorded ID burst and the background (the clip's first 12 s, looped) behind idle
    # hiss at which the background reads ~11 dB of quieting; the key-up 16 dB over it (as recorded)
    # or only 7 dB (under drop_db: only the rolling background rule can end it).
    from sizeup.radio.mdc1200 import decode
    burst, body = wav("FG1-1791491541708.mdc.wav")[:4000], wav("FG1-1791491541708.src.wav")
    body = np.concatenate([body] + [body[3 * FRAME:]] * 4)                      # ~56 s of it
    probe = NoiseSquelch()
    bg = np.median([probe.hf_level(body[i:i + FRAME]) for i in range(0, len(body) - FRAME + 1, FRAME)])
    amp = np.sqrt(bg * 10 ** (11 / 10) / (FRAME * 0.375))   # hiss whose 4-7.5 kHz level is 11 dB over it
    ku = np.concatenate([dead(0.3), voice_like(1.0), dead(0.1), burst])
    ku = ku + carrier(len(ku), 11 + over_db, amp)
    audio = np.concatenate([amp * rng.standard_normal(RATE * 4), ku, body, amp * rng.standard_normal(RATE * 2)])
    end = int((4.0 + len(ku) / RATE) * RATE / FRAME)                           # frame where the key-up ends
    segs, lit, _ = squelch_trace(audio, drop_db=10)
    assert len(segs) == 1                                                      # the key-up; no 50 s segment
    assert 1.1 < segs[0].end - segs[0].start < 1.7
    assert [p.unit_hex for b in segs[0].bursts for p in decode(b.astype(np.float32), RATE)] == ["6301"]
    assert not lit[end + int(4.5 * RATE / FRAME):].any()                       # closed within 4.5 s, and stays
    # Without drop_db (what the station ran: NfmReceiver dropped it on start) it is the 16:32 log.
    segs, _, _ = squelch_trace(audio)
    assert len(segs) == 1 and segs[0].end - segs[0].start > 50


def test_keying_up_silently_then_talking_keeps_the_talking():
    # 4 s of dead carrier reads as the background; speech on that same carrier opens it again.
    segs, lit, _ = squelch_trace(np.concatenate([hiss(3.0), strong(np.concatenate([dead(6.0), voice_like(3.0)])),
                                                 hiss(2.0)]), drop_db=10)
    assert len(segs) == 1
    from sizeup.radio.fm import speech_seconds
    assert 3.0 < segs[0].end - segs[0].start < 4.5 and speech_seconds(segs[0].audio) > 2.0
    assert segs[0].end == pytest.approx(12.0, abs=0.3)


def test_the_floor_follows_a_weaker_background_down():
    # A floor learnt from a strong carrier held silently must not block the key-ups after it, once
    # only a weaker background is left on the channel.
    ku = strong(np.concatenate([dead(0.4), mdc_burst(), dead(0.2), voice_like(1.5), mdc_burst(), dead(0.15)]))
    first = np.concatenate([hiss(3.0), strong(dead(5.0)), background(20.0)])
    segs, lit, sq = squelch_trace(first, drop_db=10)
    assert segs == [] and sq.floor is not None and sq.floor < 30                # ~26 dB, not ~46
    segs, lit, sq = squelch_trace(np.concatenate([first, ku, background(3.0), hiss(2.0)]), drop_db=10)
    assert len(segs) == 1 and len(segs[0].bursts) == 2 and 1.3 < segs[0].end - segs[0].start < 2.1
    assert segs[0].start == pytest.approx(28.6, abs=0.3)                       # opened at the key-up


def test_a_key_up_just_over_the_background_ends_where_it_did():
    # Quieting 7 dB over the background (under drop_db): the drop doesn't end it; 4 s of nothing does,
    # and the key-up is kept with its IDs.
    q = 26 + 7
    ku = np.concatenate([dead(0.4), mdc_burst(), dead(0.2), voice_like(2.0), mdc_burst(), dead(0.15)])
    ku = ku + carrier(len(ku), q)
    segs, lit, _ = squelch_trace(np.concatenate([hiss(3.0), ku, background(15.0), hiss(2.0)]), drop_db=10)
    assert len(segs) == 1 and len(segs[0].bursts) == 2 and 1.8 < segs[0].end - segs[0].start < 2.6
    end = int((3.0 + len(ku) / RATE) * RATE / FRAME)
    assert lit[end:end + 60].any() and not lit[end + 90:].any()               # closed within 4.5 s


def test_receiver_squelch_keeps_drop_db_across_restarts():
    from sizeup.radio.fm import Channel, NfmReceiver
    r = NfmReceiver("rtl_fm", "", None, Channel(154.0, "FG1"), lambda c, s: None, drop_db=10.0)
    assert r.squelch.drop_db == 10.0 and r.new_squelch().drop_db == 10.0


# -- GitHub #61: South 2 pages cut off by a fade (2026-10-13 13:04, the "time out" never recorded) ------

PAGING = dict(hang_s=1.5, continue_s=3.0)       # config defaults for kind = "paging"


def south2_page(voice_s=6.0):
    """Tones for Port Chester, then the dispatcher: one keyed transmission at ~26 dB quieting."""
    return keyed(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice_like(voice_s)]))


def held_pages(segs, label="South 2"):
    """Segments through the page assembler, then its window let run out: the pages, in order."""
    from sizeup.radio.page import PageHold
    hold, out = PageHold(PLAN), []
    for s in segs:
        done, _touched = hold.add(label, 470.2, s)
        out += done
    return out + hold.due(float("inf"))


def test_a_short_fade_mid_page_stays_one_page():
    # 0.6 s of no carrier in the middle of the voice: over the old 0.4 s hang, inside the paging one.
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(0.6), keyed(voice_like(6.0)), hiss(6.0)])
    segs = run_squelch(audio, **PAGING)
    assert len(segs) == 1 and segs[0].end - segs[0].start == pytest.approx(16.8, abs=0.3)
    (page,) = held_pages(segs)
    assert page.kind == "page" and page.departments == ["Port Chester FD"] and page.status == "pending"
    assert len(page.voice) / RATE == pytest.approx(12.8, abs=0.5)        # both halves of the voice
    g0, g1 = segs[0].breaks[0]
    assert g1 - g0 == pytest.approx(0.6, abs=0.1)
    gap = segs[0].audio[int((g0 + 0.05) * RATE):int((g1 - 0.05) * RATE)]
    assert np.abs(gap).max() == 0                                       # the hiss is muted, not kept
    # The old hang (traffic channels, and paging before #61) ends the page at the fade.
    assert len(run_squelch(audio)) == 2


def test_a_fade_that_comes_back_under_the_open_threshold_is_joined():
    # Under close_db for 2.5 s (past the 1.5 s hang: the squelch closes), then the page comes back at
    # ~8 dB of quieting: between close (6) and open (10). Before #61 it never reopened: the tail was lost.
    tail = voice_like(5.0) + carrier(RATE * 5, 8.0)
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(2.5), tail, hiss(6.0)])
    assert len(run_squelch(audio)) == 1                                  # the cut-off page (old behaviour)
    segs = run_squelch(audio, **PAGING)
    assert len(segs) == 2
    first, cont = segs
    assert first.reason == "quiet" and not first.cont and first.hold_until > first.end
    assert cont.cont and cont.start < first.hold_until and 7 < cont.q_open < 10
    (page,) = held_pages(segs)
    alone = assemble("South 2", 470.2, first, PLAN)
    # One page, one recording: the tones and where the voice starts are those of the first part ...
    assert page.kind == "page" and page.departments == ["Port Chester FD"]
    assert [(t.a_hz, t.b_hz, t.start, t.end) for t in page.tones] == [(t.a_hz, t.b_hz, t.start, t.end) for t in alone.tones]
    assert page.voice_offset_s == alone.voice_offset_s
    # ... and the audio runs on to the end of the tail, with the time between kept as silence.
    assert page.start == first.start and page.end == pytest.approx(cont.end, abs=0.01)
    assert len(page.audio) / RATE == pytest.approx(cont.end - first.start, abs=0.01)
    n0, n1 = len(first.audio), int(round((cont.start - first.end) * RATE))
    assert n1 / RATE == pytest.approx(2.5, abs=0.2)
    assert np.array_equal(page.audio[:n0], first.audio) and not page.audio[n0:n0 + n1].any()
    assert np.array_equal(page.audio[n0 + n1:], cont.audio)
    assert len(page.parts) == 2 and page.parts[1]["gap"] == pytest.approx(2.5, abs=0.2)


def test_a_separate_time_out_after_the_window_stays_separate():
    # A genuine end: 5 s of nothing, then 60 Control's "time out" as its own (full strength) key-up.
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(5.0), keyed(voice_like(3.0)), hiss(6.0)])
    segs = run_squelch(audio, **PAGING)
    assert len(segs) == 2 and not segs[1].cont
    page, timeout = held_pages(segs)
    assert page.kind == "page" and len(page.parts) == 1
    assert timeout.kind == "voice" and timeout.start == segs[1].start


def test_a_no_tone_transmission_in_the_window_joins_the_page():
    # The voice of a tones-only page, keyed again 1 s after the tones (full strength): one page.
    audio = np.concatenate([hiss(4.0), keyed(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0)])), hiss(2.5),
                            keyed(voice_like(5.0)), hiss(6.0)])
    segs = run_squelch(audio, **PAGING)
    assert len(segs) == 2 and segs[1].cont
    (page,) = held_pages(segs)
    assert page.kind == "page" and page.status == "pending" and len(page.voice) / RATE > 5.0
    # New tones in the window are a new page, not more of this one.
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(2.5), south2_page(), hiss(6.0)])
    pages = held_pages(run_squelch(audio, **PAGING))
    assert [p.kind for p in pages] == ["page", "page"] and all(len(p.parts) == 1 for p in pages)


def test_the_next_page_inside_the_hang_is_cut_off_as_its_own():
    # Re-keyed with new tones 1 s after the voice: the long hang holds the squelch open across it (one
    # segment), but the page is cut where the carrier came back, so neither loses its voice.
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(1.0), south2_page(), hiss(6.0)])
    segs = run_squelch(audio, **PAGING)
    assert len(segs) == 1 and len(segs[0].breaks) == 1
    first, second = held_pages(segs)
    for p in (first, second):
        assert p.kind == "page" and p.departments == ["Port Chester FD"] and p.status == "pending"
        assert len(p.voice) / RATE == pytest.approx(6.0, abs=0.5)
    assert second.start - first.end == pytest.approx(1.0, abs=0.1)


def test_max_length_still_splits_a_joined_page():
    from sizeup.radio.page import MAX_PAGE_S
    # One carrier for 150 s: the squelch splits it at 2 minutes and nothing waits on the first part.
    long_tx = keyed(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), voice_like(146.0)]))
    segs = run_squelch(np.concatenate([hiss(4.0), long_tx, hiss(4.0)]), **PAGING)
    assert len(segs) == 2 and segs[0].reason == "max length" and segs[0].hold_until == 0
    pages = held_pages(segs)
    assert len(pages) == 2 and pages[0].end - pages[0].start == pytest.approx(MAX_PAGE_S, abs=0.1)
    # A continuation that would take a page past 2 minutes is a page of its own.
    audio = np.concatenate([hiss(4.0), south2_page(100.0), hiss(2.5), voice_like(30.0) + carrier(RATE * 30, 8.0),
                            hiss(6.0)])
    segs = run_squelch(audio, **PAGING)
    assert len(segs) == 2 and segs[1].cont
    pages = held_pages(segs)
    assert len(pages) == 2 and all(p.end - p.start <= MAX_PAGE_S for p in pages)


def test_page_hold_waits_out_the_window_and_a_continuation_in_progress():
    from sizeup.radio.page import HOLD_SLACK_S, PageHold
    tail = voice_like(5.0) + carrier(RATE * 5, 8.0)
    first, cont = run_squelch(np.concatenate([hiss(4.0), south2_page(), hiss(2.5), tail, hiss(6.0)]), **PAGING)
    hold = PageHold(PLAN)
    done, touched = hold.add("South 2", 470.2, first)
    assert done == [] and touched[0][1] is False                     # shown as pending, not finalised
    page = touched[0][0]
    assert hold.due(first.hold_until) == []                          # not before the window (+ slack) ends
    assert hold.due(first.hold_until + HOLD_SLACK_S + 1, busy=lambda label: True) == []   # carrier back: waits
    done, touched = hold.add("South 2", 470.2, cont)
    assert done == [] and touched == [(page, True)]
    assert hold.due(cont.hold_until + HOLD_SLACK_S) == [page] and hold.held == {}


def test_traffic_channels_keep_the_short_hang_and_no_continuation():
    # FG1 and the other traffic channels: 0.4 s hang, no continuation, as before #61.
    from sizeup.config import ChannelCfg
    from sizeup.radio.fm import HANG_FRAMES, Channel, NfmReceiver
    fg1, s2 = ChannelCfg("FG1", 453.0375, "traffic"), ChannelCfg("South 2", 470.2, "paging")
    assert (fg1.hang_s, fg1.continue_s) == (None, 0.0) and (s2.hang_s, s2.continue_s) == (1.5, 3.0)
    r = NfmReceiver("rtl_fm", "", None, Channel(fg1.mhz, fg1.label), lambda c, s: None, drop_db=10.0,
                    hang_s=fg1.hang_s, continue_s=fg1.continue_s)
    sq = r.new_squelch()
    assert sq._hang_live == HANG_FRAMES and sq._cont_frames == 0 and sq.drop_db == 10.0
    talk = np.concatenate([hiss(3.0), strong(voice_like(3.0)), hiss(0.6), strong(voice_like(3.0)), hiss(3.0)])
    segs = run_squelch(talk, drop_db=10)
    assert len(segs) == 2 and all(not s.cont and s.hold_until == 0 for s in segs)
    # Weaker carriers coming back under open_db stay closed on traffic channels.
    segs = run_squelch(np.concatenate([hiss(3.0), strong(voice_like(3.0)), hiss(2.0),
                                       voice_like(3.0) + carrier(RATE * 3, 8.0), hiss(3.0)]), drop_db=10)
    assert len(segs) == 1


def test_a_page_in_progress_is_kept_when_the_audio_stops():
    # rtl_fm stalls mid-page (or the receiver is stopped): what was heard is a page, with the reason.
    clock = {"t": 0.0}
    sq = NoiseSquelch(clock=lambda: clock["t"], **PAGING)
    audio = np.concatenate([hiss(4.0), south2_page()])
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        assert sq.feed(audio[i:i + FRAME]) is None
    seg = sq.flush("stall")
    assert seg is not None and seg.reason == "stall" and seg.hold_until == 0 and not sq.open
    assert sq.flush("stall") is None


def test_recording_end_is_logged_with_why_it_ended():
    from sizeup.radio.page import recording_note
    tail = voice_like(5.0) + carrier(RATE * 5, 8.0)
    (page,) = held_pages(run_squelch(np.concatenate([hiss(4.0), south2_page(), hiss(2.5), tail, hiss(6.0)]), **PAGING))
    note = recording_note(page)
    assert note.startswith("South 2 recording end: page 17.") and "quiet after hang" in note
    assert "quieting 26." in note and "at open, 8." in note and "idle hiss" in note
    assert "1 continuation(s) joined (gap 2.5 s)" in note
    (page,) = held_pages(run_squelch(np.concatenate([hiss(4.0), south2_page(), hiss(6.0)]), **PAGING))
    assert recording_note(page).endswith("no continuation")


def test_rx_shows_hold_between_a_page_and_its_continuation_not_a_flicker():
    from sizeup.radio.fm import Channel, NfmReceiver
    r = NfmReceiver("rtl_fm", "", None, Channel(470.2, "South 2"), lambda c, s: None, **PAGING)
    r.state = "ok"
    clock = {"t": 0.0}
    r.squelch = NoiseSquelch(clock=lambda: clock["t"], **PAGING)
    tail = voice_like(5.0) + carrier(RATE * 5, 8.0)
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(2.5), tail, hiss(6.0)])
    words = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        r.squelch.feed(audio[i:i + FRAME])
        st = r.status()
        w = "RX" if st["open"] else st.get("word", "")
        if not words or words[-1] != w:
            words.append(w)
    # Open through the page and its 1.5 s hang, "RX hold" while the carrier is gone, RX again when it's back.
    assert words == ["", "RX", "RX hold", "RX", "RX hold", ""]
