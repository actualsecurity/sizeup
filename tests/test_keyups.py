"""Key-ups cut out of one squelch opening (radio/fm.py, am.py keyups): two units talking back to back, the second keying
up before the hang ran out, are two transmissions, not one clip with both. Synthetic audio only; every radio ID here
is invented."""

import numpy as np
import pytest

from sizeup.radio import fleetsync, mdc1200
from sizeup.radio.am import CarrierSquelch
from sizeup.radio.fm import FRAME, RATE, NoiseSquelch
from tests.test_atc import envelope
from tests.test_page import PAGING, PLAN, held_pages, hiss, keyed, run_squelch, south2_page, strong, tone, voice_like

TRAFFIC = dict(drop_db=10.0, keyups="traffic", split_s=0.2, jump_db=10.0)      # config defaults, kind = "traffic"
PAGING_K = dict(PAGING, keyups="paging", jump_db=12.0)                          # ... kind = "paging"
AIRBAND = dict(split_s=0.15, jump_db=6.0)                                       # ... kind = "airband"
A_ID, B_ID = 0x2468, 0x1357                    # invented MDC unit IDs
PRE, POST = (0x01, 0x80), (0x01, 0x00)         # MDC PTT-ID: pre-ID (key-up), post-ID (release)


def mdc(unit, kind=PRE):
    return mdc1200.encode(*kind, unit)


def quiet(s, q_db=45.0):
    """Dead carrier: q_db of quieting against hiss() (amp 0.4)."""
    return 0.4 * 10 ** (-q_db / 20) * np.random.default_rng(int(s * 1000)).standard_normal(int(RATE * s))


def unit(talk_s, pre=None, post=None, q_db=45.0):
    """One key-up as the discriminator hears it: dead carrier, a pre-ID, speech, a post-ID, dead carrier."""
    parts = [quiet(0.15, q_db)]
    if pre is not None:
        parts += [mdc(pre), quiet(0.15, q_db)]
    parts += [voice_like(talk_s) + quiet(talk_s, q_db)]
    if post is not None:
        parts += [quiet(0.1, q_db), mdc(post, POST)]
    return np.concatenate(parts + [quiet(0.1, q_db)])


def ids(seg):
    out = []
    for b in [*seg.bursts, seg.audio]:
        out += [p.unit_id for p in mdc1200.decode(np.asarray(b, np.float64), RATE) if p.kind in ("pre-id", "post-id")]
    return sorted(set(out))


# -- traffic (FG1 / FG2) ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("gap", [0.25, 0.3, 0.4])
def test_a_reply_inside_the_hang_is_its_own_transmission(gap):
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(gap), unit(1.5), hiss(2.0)])
    assert len(run_squelch(audio, drop_db=10.0)) == 1             # before: the hang (0.45 s) joined them
    a, b = run_squelch(audio, **TRAFFIC)
    assert a.reason == "key-up" and b.reason == "quiet" and b.split == "gap"
    assert b.start - a.end == pytest.approx(gap, abs=0.06)        # the hiss between them dropped
    assert a.first_start == a.start and a.prev_start is None and not a.split
    assert b.first_start == a.start and b.prev_start == a.start
    assert a.end - a.start == pytest.approx(2.3, abs=0.2) and b.end - b.start == pytest.approx(1.8, abs=0.2)


@pytest.mark.parametrize("gap", [0.6, 1.0, 1.5])
def test_replies_after_the_hang_are_separate_as_before_and_not_linked(gap):
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(gap), unit(1.5), hiss(2.0)])
    segs = run_squelch(audio, **TRAFFIC)
    assert len(segs) == 2 and all(s.reason == "quiet" and s.first_start is None and not s.split for s in segs)


@pytest.mark.parametrize("dip", [0.05, 0.1, 0.15])
def test_a_short_fade_inside_one_key_up_stays_one(dip):
    # picket-fencing: the carrier gone for a frame or three, the same radio after it
    audio = np.concatenate([hiss(3.0), unit(1.5), hiss(dip), voice_like(1.5) + quiet(1.5), quiet(0.1), hiss(2.0)])
    segs = run_squelch(audio, **TRAFFIC)
    assert len(segs) == 1 and segs[0].first_start is None
    assert segs[0].end - segs[0].start == pytest.approx(1.85 + dip + 1.6, abs=0.2)


def test_a_fade_that_never_loses_the_carrier_stays_one():
    # a portable at 15 dB of quieting sags to 8 dB (over close_db, 6) for a second: one key-up
    audio = np.concatenate([hiss(3.0), unit(1.5, q_db=15.0), voice_like(1.0) + quiet(1.0, 8.0), unit(1.5, q_db=15.0),
                            hiss(2.0)])
    assert len(run_squelch(audio, **TRAFFIC)) == 1


def test_mdc_ids_at_a_short_gap_cut_it_and_each_piece_keeps_its_own_id():
    # A's post-ID, 0.12 s of hiss (under split_s: a gap alone wouldn't cut it), B's pre-ID
    audio = np.concatenate([hiss(3.0), unit(2.0, pre=A_ID, post=A_ID), hiss(0.12), unit(1.5, pre=B_ID, post=B_ID),
                            hiss(2.0)])
    one = run_squelch(audio, drop_db=10.0)
    assert len(one) == 1 and ids(one[0]) == sorted([A_ID, B_ID])  # before: one row with both radios
    a, b = run_squelch(audio, **TRAFFIC)
    assert a.reason == "key-up" and b.split == "id"
    assert ids(a) == [A_ID] and ids(b) == [B_ID]
    assert b.prev_start == a.start == b.first_start


def test_a_pre_id_alone_cuts_it_too():
    # A sends no ID; B's pre-ID after 0.12 s
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(0.12), unit(1.5, pre=B_ID), hiss(2.0)])
    a, b = run_squelch(audio, **TRAFFIC)
    assert b.split == "id" and ids(a) == [] and ids(b) == [B_ID]


def real_post_id(name="fg1_139430.wav"):
    """A post-ID burst as the station's discriminator recorded it on FG1 (tests/fixtures/mdc), with its dead air."""
    import wave
    from pathlib import Path
    with wave.open(str(Path(__file__).parent / "fixtures" / "mdc" / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float) / 32768


def test_a_recorded_post_id_then_a_quick_reply_is_two():
    # A talks and releases (its real post-ID, as recorded on FG1), 0.15 s later B keys up (no ID of its own)
    # (its dead air reads ~32 dB of quieting against hiss(): the rest of A and B at that level too)
    a_tx = np.concatenate([quiet(0.15, 32.0), voice_like(2.0) + quiet(2.0, 32.0), real_post_id()])
    audio = np.concatenate([hiss(3.0), a_tx, hiss(0.15), unit(1.5, q_db=32.0), hiss(2.0)])
    assert len(run_squelch(audio, drop_db=10.0)) == 1
    a, b = run_squelch(audio, **TRAFFIC)
    assert b.split == "id" and len(ids(a)) == 1 and ids(b) == []
    # the same radio keying again after 0.15 s (its post-ID each time): two key-ups, the same ID on each
    audio = np.concatenate([hiss(3.0), a_tx, hiss(0.15), a_tx, hiss(2.0)])
    a, b = run_squelch(audio, **TRAFFIC)
    assert b.split == "id" and len(ids(a)) == 1 and ids(a) == ids(b)


def test_a_fleetsync_pre_id_cuts_it():
    burst = fleetsync.encode(100, 2001, "pre-id")                 # invented fleet / unit
    b_tx = np.concatenate([quiet(0.15), burst, quiet(0.15), voice_like(1.5) + quiet(1.5), quiet(0.1)])
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(0.12), b_tx, hiss(2.0)])
    a, b = run_squelch(audio, **TRAFFIC)
    assert b.split == "id"
    found = [p.id for x in (*b.bursts, b.audio) for p in fleetsync.decode(np.asarray(x, np.float64), RATE)]
    assert found and set(found) == {found[0]}


def test_another_transmitter_after_a_short_drop_is_its_own():
    # A far off (18 dB of quieting), 0.1 s gone, B close by (45 dB): another radio
    audio = np.concatenate([hiss(3.0), unit(2.0, q_db=18.0), hiss(0.1), unit(1.5), hiss(2.0)])
    a, b = run_squelch(audio, **TRAFFIC)
    assert b.split == "level" and a.quieting_db < 25 < b.quieting_db
    # the same radio back at its own level after 0.1 s: one key-up
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(0.1), unit(1.5), hiss(2.0)])
    assert len(run_squelch(audio, **TRAFFIC)) == 1


def test_a_back_and_forth_is_every_key_up_with_its_neighbours_linked():
    parts = [hiss(3.0)]
    for k in range(4):
        parts += [unit(1.0 + 0.3 * k, pre=(A_ID, B_ID)[k % 2]), hiss(0.3)]
    segs = run_squelch(np.concatenate(parts + [hiss(2.0)]), **TRAFFIC)
    assert len(segs) == 4 and [ids(s) for s in segs] == [[A_ID], [B_ID], [A_ID], [B_ID]]
    assert all(s.first_start == segs[0].start for s in segs)
    assert [s.prev_start for s in segs] == [None] + [s.start for s in segs[:-1]]
    assert [s.reason for s in segs] == ["key-up"] * 3 + ["quiet"]


def test_keyups_off_is_the_old_squelch_frame_for_frame():
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(0.3), unit(1.5), hiss(1.0), unit(1.0), hiss(2.0)])
    old = run_squelch(audio, drop_db=10.0)
    new = run_squelch(audio, drop_db=10.0, keyups="", split_s=0.2, jump_db=10.0)
    assert [(s.start, s.end, s.reason) for s in old] == [(s.start, s.end, s.reason) for s in new]


# -- paging (South 2) ------------------------------------------------------------------------------------------------

def test_a_page_with_fades_and_a_re_key_is_never_cut():
    # a 0.6 s simulcast fade mid-voice, then (another test) the voice keyed again 1 s after the tones: one page each
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(0.6), keyed(voice_like(6.0)), hiss(6.0)])
    segs = run_squelch(audio, **PAGING_K)
    assert len(segs) == 1 and segs[0].first_start is None
    (page,) = held_pages(segs)
    assert page.kind == "page" and page.departments == ["Port Chester FD"]
    audio = np.concatenate([hiss(4.0), keyed(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0)])), hiss(1.0),
                            keyed(voice_like(5.0)), hiss(6.0)])
    (page,) = held_pages(run_squelch(audio, **PAGING_K))
    assert page.kind == "page" and len(page.voice) / RATE > 5.0


def page_frames(audio, **kw):
    """(segments, the frame each was handed back on)."""
    clock = {"t": 1000.0}
    sq = NoiseSquelch(clock=lambda: clock["t"], **kw)
    out = []
    for k, i in enumerate(range(0, len(audio) - FRAME + 1, FRAME)):
        clock["t"] += FRAME / RATE
        if (s := sq.feed(audio[i:i + FRAME])) is not None:
            out.append((s, k))
    return out


def test_a_page_closes_on_the_same_frame_with_key_ups_on():
    # nothing about a page's own timing changes (the pre-alert waits on it): same segments, same frames
    for audio in (np.concatenate([hiss(4.0), south2_page(), hiss(6.0)]),
                  np.concatenate([hiss(4.0), south2_page(), hiss(0.6), keyed(voice_like(6.0)), hiss(6.0)]),
                  np.concatenate([hiss(4.0), south2_page(), hiss(2.5), voice_like(5.0) + 0.4 * 10 ** (-8 / 20)
                                  * np.random.default_rng(3).standard_normal(RATE * 5), hiss(6.0)])):
        old, new = page_frames(audio, **PAGING), page_frames(audio, **PAGING_K)
        assert [(s.start, s.end, s.reason, s.cont, s.hold_until, k) for s, k in old] == \
            [(s.start, s.end, s.reason, s.cont, s.hold_until, k) for s, k in new]


def test_a_unit_answering_inside_the_page_hang_is_cut_off_the_page():
    # 1 s after the page a unit near the station answers (46 dB of quieting against the page's 26): another radio
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(1.0), strong(voice_like(2.0)), hiss(6.0)])
    assert len(run_squelch(audio, **PAGING)) == 1                  # before: one recording, the answer in the page
    segs = run_squelch(audio, **PAGING_K)
    assert len(segs) == 2 and segs[0].reason == "key-up" and segs[1].split == "level"
    page, answer = held_pages(segs)
    assert page.kind == "page" and page.departments == ["Port Chester FD"]
    assert len(page.voice) / RATE == pytest.approx(6.0, abs=0.5)
    assert answer.kind == "voice" and answer.keyup == {"parent": page.id, "prev": page.id, "split": "level"}
    assert page.keyup == {"parent": page.id, "prev": "", "split": ""}


def test_a_unit_with_an_id_in_the_continuation_window_is_not_joined_to_the_page():
    # the page closed (2 s gap: past the 1.5 s hang); a unit at the page's own level keys up with its pre-ID
    answer = keyed(np.concatenate([quiet(0.1), mdc(B_ID), quiet(0.15), voice_like(2.0)]))
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(2.0), answer, hiss(6.0)])
    old = run_squelch(audio, **PAGING)
    assert len(old) == 2 and old[1].cont                          # before: joined to the page as more of it
    segs = run_squelch(audio, **PAGING_K)
    assert len(segs) == 2 and not segs[1].cont and segs[1].split == "id"
    page, ans = held_pages(segs)
    assert page.kind == "page" and len(page.parts) == 1 and ans.kind == "voice"
    assert ans.keyup["parent"] == page.id and ans.keyup["split"] == "id"
    assert ids(segs[1]) == [B_ID]


def test_a_unit_with_an_id_inside_the_hang_is_cut_off_too():
    answer = keyed(np.concatenate([quiet(0.1), mdc(B_ID), quiet(0.15), voice_like(2.0)]))
    segs = run_squelch(np.concatenate([hiss(4.0), south2_page(), hiss(0.8), answer, hiss(6.0)]), **PAGING_K)
    assert len(segs) == 2 and segs[1].split == "id" and ids(segs[1]) == [B_ID]
    page, _ans = held_pages(segs)
    assert page.kind == "page" and len(page.voice) / RATE == pytest.approx(6.0, abs=0.5)


# -- airband (HPN, AM) -----------------------------------------------------------------------------------------------

def am(levels, **kw):
    sq = CarrierSquelch(3.5, **kw)
    return [s for f in envelope(levels, np.random.default_rng(7)) if (s := sq.feed(f)) is not None]


@pytest.mark.parametrize("gap", [0.15, 0.25, 0.35])
def test_tower_then_pilot_back_to_back_is_two_transmissions(gap):
    # the tower (+18 dB), the carrier gone for `gap`, an aircraft (+12 dB)
    levels = [(5, 0.01, False), (2.0, 0.08, True), (gap, 0.01, False), (1.5, 0.04, True), (3, 0.01, False)]
    assert len(am(levels)) == 1                                   # before: one clip, both voices
    a, b = am(levels, **AIRBAND)
    assert a.reason == "key-up" and b.split == "gap" and b.prev_start == a.start == b.first_start
    assert a.quieting_db == pytest.approx(18, abs=1) and b.quieting_db == pytest.approx(12, abs=1)
    assert a.end - a.start == pytest.approx(0.4 + 2.0 + min(gap, 0.2), abs=0.1)   # pre-roll + carrier + tail
    assert b.preroll_s == pytest.approx(min(gap, 0.4), abs=0.06) and not a.weak and not b.weak


def test_a_short_drop_into_another_carrier_level_is_two():
    levels = [(5, 0.01, False), (2.0, 0.08, True), (0.1, 0.01, False), (1.5, 0.025, True), (3, 0.01, False)]
    a, b = am(levels, **AIRBAND)
    assert b.split == "level"
    # the same carrier back after 0.1 s: one
    levels = [(5, 0.01, False), (2.0, 0.08, True), (0.1, 0.01, False), (1.5, 0.08, True), (3, 0.01, False)]
    assert len(am(levels, **AIRBAND)) == 1


def test_a_fading_aircraft_stays_one_transmission():
    # a weak aircraft (+4 dB) dipping to the floor for 0.3 s: never 6 dB under its own level, so the hang holds it
    levels = [(5, 0.01, False), (1.5, 0.016, True), (0.3, 0.01, False), (1.5, 0.016, True), (3, 0.01, False)]
    assert len(am(levels, **AIRBAND)) == 1
    # a strong one sagging 8 dB (still over the close level) for 0.3 s: one
    levels = [(5, 0.01, False), (1.5, 0.05, True), (0.3, 0.02, True), (1.5, 0.05, True), (3, 0.01, False)]
    assert len(am(levels, **AIRBAND)) == 1


def test_a_heterodyne_on_the_second_key_up_is_notched_in_that_piece_only():
    rng = np.random.default_rng(9)
    frames = envelope([(5, 0.01, False), (2.0, 0.08, True), (0.25, 0.01, False)], rng)
    b = envelope([(2.0, 0.04, True)], rng)
    t = np.arange(len(b) * FRAME) / RATE
    beat = 0.01 * np.sin(2 * np.pi * 455.0 * t)                   # a second carrier beating with it
    frames += [f + beat[k * FRAME:(k + 1) * FRAME] for k, f in enumerate(b)]
    frames += envelope([(3, 0.01, False)], rng)
    sq = CarrierSquelch(3.5, **AIRBAND)
    a, b = [s for f in frames if (s := sq.feed(f)) is not None]
    beat_hz = lambda seg: [f for f in seg.tones if abs(f - 455.0) < 10]     # noqa: E731
    assert beat_hz(b) and not beat_hz(a)                          # (the stand-in voice is a 400 Hz tone: notched too)


def test_a_short_answer_after_a_cut_is_kept():
    # "roger": 0.4 s of carrier, under the 0.6 s a fresh open needs, kept once the cut proved the key-up
    levels = [(5, 0.01, False), (2.0, 0.08, True), (0.2, 0.01, False), (0.4, 0.04, True), (3, 0.01, False)]
    a, b = am(levels, **AIRBAND)
    assert b.split == "gap" and 0.5 < b.end - b.start < 1.2


# -- config and the app ----------------------------------------------------------------------------------------------

def test_kind_defaults_and_config_keys(tmp_path):
    from sizeup.config import ChannelCfg, ConfigError, load
    assert (ChannelCfg("FG1", 453.0375, "traffic").split_s, ChannelCfg("FG1", 453.0375, "traffic").jump_db) == (0.2, 10.0)
    s2 = ChannelCfg("South 2", 470.2, "paging")
    assert (s2.split_s, s2.jump_db, s2.keyups) == (0.0, 12.0, "paging")
    twr = ChannelCfg("HPN Tower", 118.575, "airband")
    assert (twr.split_s, twr.jump_db, twr.keyups) == (0.15, 6.0, "")
    assert ChannelCfg("FG2", 458.0375, "traffic", split_s=0, jump_db=0).keyups == ""
    p = tmp_path / "c.toml"
    p.write_text('[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\nsplit_s = 0.3\njump_db = 0\n'
                 '[[radio.channels]]\nlabel = "HPN Tower"\nmhz = 118.575\nkind = "airband"\nsplit_s = 0\n')
    c = load(p)
    fg1, twr = c.channels
    assert (fg1.split_s, fg1.jump_db, fg1.keyups) == (0.3, 0.0, "traffic") and (twr.split_s, twr.jump_db) == (0.0, 6.0)
    p.write_text('[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\nsplit_s = 5\n')
    with pytest.raises(ConfigError, match="split_s"):
        load(p)


def test_receivers_get_their_channel_kinds_key_ups():
    from sizeup.app import make_receiver
    from sizeup.config import ChannelCfg
    from types import SimpleNamespace
    cfg = SimpleNamespace(rtl_fm="rtl_fm", channels=(), airband_multichannel=False)
    fg = make_receiver(cfg, ChannelCfg("FG1", 453.0375, "traffic", drop_db=10.0), "", lambda c, s: None)
    sq = fg.new_squelch()
    assert (sq.keyups, sq._split_frames, sq.jump_db) == ("traffic", 4, 10.0)
    s2 = make_receiver(cfg, ChannelCfg("South 2", 470.2, "paging"), "", lambda c, s: None).new_squelch()
    assert (s2.keyups, s2._split_frames, s2.jump_db) == ("paging", 0, 12.0)
    twr = make_receiver(cfg, ChannelCfg("HPN Tower", 118.575, "airband", squelch_db=3.5), "", lambda c, s: None)
    assert (twr.squelch._split_frames, twr.squelch.jump_db) == (3, 6.0)


def test_each_piece_is_its_own_row_with_its_own_id_and_linked(tmp_path):
    from sizeup.app import keyup_link
    from sizeup.radio.fm import Channel
    from sizeup.app import App
    from tests.test_app import cfg
    audio = np.concatenate([hiss(3.0), unit(2.0, pre=A_ID), hiss(0.3), unit(1.5, pre=B_ID), hiss(2.0)])
    a, b = run_squelch(audio, **TRAFFIC)
    assert keyup_link("FG1", a) == {"parent": f"FG1-{int(a.start * 1000)}", "prev": "", "split": ""}
    assert keyup_link("FG1", b) == {"parent": f"FG1-{int(a.start * 1000)}", "prev": f"FG1-{int(a.start * 1000)}",
                                    "split": "gap"}
    app = App(cfg(tmp_path))
    app._kind = {"FG1": "traffic"}
    sent = []
    app._stt = lambda t, audio, engine, label: sent.append((t, audio))     # the clip -> speech-to-text hand-off
    app._segment(Channel(453.0375, "FG1"), a)
    app._segment(Channel(453.0375, "FG1"), b)
    (ta, aa), (tb, ab) = sent
    assert ta.id == f"FG1-{int(a.start * 1000)}" and tb.id == f"FG1-{int(b.start * 1000)}" and ta.id != tb.id
    assert (ta.start, ta.end, tb.start, tb.end) == (a.start, a.end, b.start, b.end)
    assert len(aa) == len(a.audio) and len(ab) == len(b.audio)
    assert ta.radio_id == f"{A_ID:04X}" and tb.radio_id == f"{B_ID:04X}"
    assert ta.keyup == {"parent": ta.id, "prev": "", "split": ""}
    assert tb.keyup == {"parent": ta.id, "prev": ta.id, "split": "gap"}
    assert tb.as_dict()["keyup"] == tb.keyup                     # (in the traffic doc the hub gets)
    assert app.has_clip(ta.id) and app.has_clip(tb.id)
