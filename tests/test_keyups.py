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
PAGING_K = dict(PAGING, keyups="paging", jump_db=12.0)       # paging opted in (off by default: jump_db 0)
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


def test_a_weaker_carrier_after_a_short_drop_is_not_cut_and_nothing_is_lost():
    # 22 dB, 0.1 s gone, back at 12 dB for a second, then 22 dB again: no level cut downward, and the 12 dB stretch
    # (speech) is never dropped as a "background": exactly what the squelch without key-ups keeps
    audio = np.concatenate([hiss(3.0), unit(2.0, q_db=22.0), hiss(0.1), voice_like(1.0) + quiet(1.0, 12.0),
                            hiss(0.1), unit(1.5, q_db=22.0), hiss(2.0)])
    main = run_squelch(audio, drop_db=10.0)
    new = run_squelch(audio, **TRAFFIC)
    assert [(s.start, s.end) for s in new] == [(s.start, s.end) for s in main]


def test_a_piece_from_a_cut_is_never_dropped_as_a_background():
    # B (no ID; cut off A by a 0.3 s gap) at 20 dB, then a stronger carrier comes straight up over it: the rise rule
    # takes what came before for a background and keeps it only with an ID; a piece from a cut is kept, linked
    b_tx = voice_like(1.5) + quiet(1.5, 20.0)
    audio = np.concatenate([hiss(3.0), unit(2.0, q_db=20.0), hiss(0.3), b_tx, unit(1.5, q_db=45.0), hiss(2.0)])
    segs = run_squelch(audio, **TRAFFIC)
    b = next(s for s in segs if s.split == "gap")
    assert b.end - b.start > 1.3 and b.prev_start == segs[0].start


def flutter(seconds, mean_db, seed, drop_every=1.0):
    """A key-up whose quieting flutters (sigma 5 dB, frame to frame) with a 0.1 s full drop every drop_every s."""
    r = np.random.default_rng(seed)
    out, k = [], 0
    while k * FRAME < seconds * RATE:
        if k % int(drop_every * 20) == 10:
            out += [hiss(0.05), hiss(0.05)]
            k += 2
            continue
        q = max(0.0, r.normal(mean_db, 5.0))
        out.append(voice_like(0.05) + 0.4 * 10 ** (-q / 20) * r.standard_normal(FRAME))
        k += 1
    return np.concatenate(out)


@pytest.mark.parametrize("mean_db", [15.0, 22.0, 30.0])
def test_flutter_with_short_drops_makes_no_new_cuts(mean_db):
    for seed in range(8):
        audio = np.concatenate([hiss(3.0), flutter(8.0, mean_db, seed), hiss(2.0)])
        main = run_squelch(audio, drop_db=10.0)
        new = run_squelch(audio, **TRAFFIC)
        assert [(s.start, s.end) for s in new] == [(s.start, s.end) for s in main], (mean_db, seed)


def test_dead_carrier_then_a_late_pre_id_is_no_row_of_its_own():
    # keyed, 0.25 s of dead carrier, a 0.25 s dropout (flutter), then the APX's late pre-ID and the speech: one
    tx = np.concatenate([quiet(0.25), hiss(0.25), quiet(0.2), mdc(B_ID), quiet(0.15), voice_like(1.5) + quiet(1.5)])
    segs = run_squelch(np.concatenate([hiss(3.0), tx, hiss(2.0)]), **TRAFFIC)
    assert len(segs) == 1 and segs[0].first_start is None and ids(segs[0]) == [B_ID]


def same(a, b):
    """Two runs of segments are the same transmissions: times, reasons and audio."""
    return len(a) == len(b) and all((x.start, x.end, x.reason) == (y.start, y.end, y.reason)
                                    and np.array_equal(x.audio, y.audio) for x, y in zip(a, b))


@pytest.mark.parametrize("reply", [0.15, 0.25])
def test_a_short_reply_after_a_gap_is_held_and_kept_as_main_keeps_it(reply):
    # A talks, 0.3 s gone, a carrier shorter than min_open_s (0.3 s) ends the exchange: the cut is held, never made,
    # and the whole recording is the one transmission main makes (the reply's audio in it, no blip row)
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(0.3), voice_like(reply) + quiet(reply), hiss(2.0)])
    main, new = run_squelch(audio, drop_db=10.0), run_squelch(audio, **TRAFFIC)
    assert len(new) == 1 and same(main, new) and new[0].first_start is None


def test_a_short_reply_then_another_key_up_in_the_hang():
    # A, 0.3 s, B for 0.2 s (under min_open_s), 0.3 s, C: B stays with A (as main has it), C is cut off as its own
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(0.3), voice_like(0.2) + quiet(0.2), hiss(0.3), unit(1.5),
                            hiss(2.0)])
    (main,) = run_squelch(audio, drop_db=10.0)
    ab, c = run_squelch(audio, **TRAFFIC)
    assert ab.start == main.start and c.end == main.end and c.split == "gap" and c.prev_start == ab.start
    n = len(ab.audio)
    assert np.array_equal(ab.audio, main.audio[:n])                     # A, the gap and B: nothing of B lost
    assert ab.end - ab.start == pytest.approx(2.25 + 0.3 + 0.2, abs=0.1)


def test_a_back_and_forth_is_every_key_up_with_its_neighbours_linked():
    parts = [hiss(3.0)]
    for k in range(4):
        parts += [unit(1.0 + 0.3 * k, pre=(A_ID, B_ID)[k % 2]), hiss(0.3)]
    segs = run_squelch(np.concatenate(parts + [hiss(2.0)]), **TRAFFIC)
    assert len(segs) == 4 and [ids(s) for s in segs] == [[A_ID], [B_ID], [A_ID], [B_ID]]
    assert all(s.first_start == segs[0].start for s in segs)
    assert [s.prev_start for s in segs] == [None] + [s.start for s in segs[:-1]]
    assert [s.reason for s in segs[:3]] == ["key-up"] * 3 and segs[3].reason in ("quiet", "drop")   # (its own end)


def test_keyups_off_is_the_old_squelch_frame_for_frame():
    audio = np.concatenate([hiss(3.0), unit(2.0), hiss(0.3), unit(1.5), hiss(1.0), unit(1.0), hiss(2.0)])
    old = run_squelch(audio, drop_db=10.0)
    new = run_squelch(audio, drop_db=10.0, keyups="", split_s=0.2, jump_db=10.0)
    assert [(s.start, s.end, s.reason) for s in old] == [(s.start, s.end, s.reason) for s in new]


# -- traffic through the live tap (radio/tap.py) ---------------------------------------------------------------------

class _Live:
    def fm_open(self, label, since):
        pass

    def fm_ids(self, label, ids):
        pass


def mdc_ids(a):
    return [(p.unit_hex, False) for p in mdc1200.decode(np.asarray(a, np.float64), RATE) if p.kind in ("pre-id", "post-id")]


def both_ids(a):
    """MDC-1200 and FleetSync, emergency packets too: what the station's tap decodes (App._mdc)."""
    a = np.asarray(a, np.float64)
    return ([(p.unit_hex, p.kind == "emergency") for p in mdc1200.decode(a, RATE)
             if p.kind in ("pre-id", "post-id", "emergency")]
            + [(p.id, p.emergency) for p in fleetsync.decode(a, RATE) if p.kind in ("pre-id", "post-id", "emergency")])


def tap_run(audio, mdc=mdc_ids, **kw):
    """Segments through a squelch with the live tap on it, as App._segment gets them: (segment, the IDs the tap hands
    its row, the tap's "ID not decoded", its live alarm key, an emergency among those IDs), and the alarms raised."""
    from sizeup.radio.fm import Channel
    from sizeup.radio.tap import LiveTap, TapSquelch
    clock = {"t": 1000.0}
    tap = LiveTap(lambda _l: "traffic", None, _Live(), mdc=mdc, clock=lambda: clock["t"])
    alarms = []
    tap.on_emergency = lambda label, since, found: alarms.append((since, list(found))) or f"alarm-{len(alarms)}"
    sq = TapSquelch(NoiseSquelch(clock=lambda: clock["t"], **kw), tap, Channel(453.0375, "FG1"))
    out = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        if (s := sq.feed(audio[i:i + FRAME])) is not None:      # (the row is made now: NfmReceiver._deliver)
            got = tap.ids("FG1", s.start, s.end)
            out.append((s, [h for h, _e in got], tap.burst("FG1", s.start, s.end), tap.alarm_key("FG1", s.start, s.end),
                        any(e for _h, e in got)))
    return out, alarms


def tapped(audio, **kw):
    """Segments through a squelch with the live tap on it, each with the IDs the tap hands its row (App._segment)."""
    return [(s, i) for s, i, *_ in tap_run(audio, **kw)[0]]


def test_the_tap_never_hands_a_cut_piece_the_radio_before_it():
    audio = np.concatenate([hiss(3.0), unit(2.0, pre=A_ID), hiss(0.3), unit(1.5), hiss(2.0)])
    (a, ida), (b, idb) = tapped(audio, **TRAFFIC)
    assert ida == [f"{A_ID:04X}"] and idb == [] and b.split == "gap"
    audio = np.concatenate([hiss(3.0), unit(2.0, pre=A_ID), hiss(0.3), unit(1.5, pre=B_ID), hiss(2.0)])
    (a, ida), (b, idb) = tapped(audio, **TRAFFIC)
    assert ida == [f"{A_ID:04X}"] and idb == [f"{B_ID:04X}"]


def test_a_cut_that_is_never_made_leaves_the_ids_as_main():
    # A (its pre-ID), 0.3 s, a 0.2 s carrier with no ID: one transmission, with A's ID, exactly as main
    audio = np.concatenate([hiss(3.0), unit(2.0, pre=A_ID), hiss(0.3), voice_like(0.2) + quiet(0.2), hiss(2.0)])
    main, new = tapped(audio, drop_db=10.0), tapped(audio, **TRAFFIC)
    assert [(s.start, s.end, i) for s, i in new] == [(s.start, s.end, i) for s, i in main]
    assert [i for _s, i in new] == [[f"{A_ID:04X}"]]


def test_a_key_up_with_no_id_after_one_with_an_id_takes_none():
    # (on main too): 3 s after A's key-up with its pre-ID, C keys up with none: C's row has no ID, not A's
    audio = np.concatenate([hiss(3.0), unit(2.0, pre=A_ID), hiss(3.0), unit(1.5), hiss(2.0)])
    (a, ida), (c, idc) = tapped(audio, drop_db=10.0)
    assert ida == [f"{A_ID:04X}"] and idc == []


@pytest.mark.parametrize("talk", [0.3, 0.6, 0.9])
@pytest.mark.parametrize("gap", [0.12, 0.2, 0.3])
def test_a_short_key_up_with_no_id_never_takes_the_next_radios_pre_id(talk, gap):
    # #672: A talks briefly and sends no ID; B keys up 0.12-0.3 s later with its pre-ID. The squelch holds the cut until
    # B has min_open_s, and A's key-up window was still open: B's pre-ID was decoded onto A's row
    audio = np.concatenate([hiss(3.0), unit(talk), hiss(gap), unit(1.5, pre=B_ID), hiss(2.0)])
    (main, main_ids, *_), = tap_run(audio, drop_db=10.0)[0]
    # (main: one row, B's ID from the tap; at 0.9 s B's burst ends past A's 1.5 s window, so the bug never showed)
    assert main_ids == ([f"{B_ID:04X}"] if talk < 0.9 else [])
    ((a, ida, burst_a, *_), (b, idb, *_)), _alarms = tap_run(audio, **TRAFFIC)
    assert b.split in ("id", "gap") and ida == [] and idb == [f"{B_ID:04X}"]
    assert not burst_a                                                  # A sent none: not "ID not decoded" either


@pytest.mark.parametrize("gap", [0.12, 0.3])
def test_a_short_key_up_with_no_id_never_takes_the_next_radios_fleetsync_id(gap):
    burst = fleetsync.encode(100, 2001, "pre-id")                 # invented fleet / unit
    b_tx = np.concatenate([quiet(0.15), burst, quiet(0.15), voice_like(1.5) + quiet(1.5), quiet(0.1)])
    audio = np.concatenate([hiss(3.0), unit(0.6), hiss(gap), b_tx, hiss(2.0)])
    (_m, main_ids, *_), = tap_run(audio, mdc=both_ids, drop_db=10.0)[0]
    assert main_ids == ["100-2001"]
    ((a, ida, burst_a, *_), (b, idb, *_)), _alarms = tap_run(audio, mdc=both_ids, **TRAFFIC)
    assert ida == [] and idb == ["100-2001"] and not burst_a


def test_the_next_radios_emergency_alarm_is_raised_once_and_goes_with_its_row():
    # B keys up with the emergency button (MDC emergency) right after A's short no-ID key-up: the alarm is raised at
    # once, as before, and only once; B's row takes it over, A's has neither the ID nor the alarm
    b_tx = np.concatenate([quiet(0.15), mdc(B_ID, (0x00, 0x80)), quiet(0.15), voice_like(1.5) + quiet(1.5), quiet(0.1)])
    audio = np.concatenate([hiss(3.0), unit(0.6), hiss(0.12), b_tx, hiss(2.0)])
    ((a, ida, _, alarm_a, _), (b, idb, _, alarm_b, em_b)), alarms = tap_run(audio, mdc=both_ids, **TRAFFIC)
    assert ida == [] and alarm_a == "" and idb == [f"{B_ID:04X}"]
    assert len(alarms) == 1 and alarms[0][1] == [(f"{B_ID:04X}", True)] and alarm_b == "alarm-1" and em_b


def ramped(burst, ramp_s=0.1):
    """B keying up with its ID burst at once, its carrier still rising out of the hiss for ramp_s (the E63 case the
    pre-roll is for): the squelch counts the carrier back partway into the burst."""
    n = int(RATE * ramp_s)
    burst = np.concatenate([burst, np.zeros(max(0, n - len(burst)))])
    env = np.full(len(burst), 0.4 * 10 ** (-45 / 20))
    env[:n] = 0.4 * np.linspace(1.0, 10 ** (-45 / 20), n)
    rise = burst + env * np.random.default_rng(7).standard_normal(len(burst))
    return np.concatenate([rise, quiet(0.15), voice_like(1.5) + quiet(1.5), quiet(0.1)])


@pytest.mark.parametrize("gap", [0.2, 0.3])
@pytest.mark.parametrize("op", [PRE, (0x00, 0x80)], ids=["pre-id", "emergency"])
def test_the_next_radios_id_goes_with_it_when_its_burst_began_on_a_rising_carrier(gap, op):
    # B's carrier back (the squelch's new carrier) only partway into its burst: B's own buffer can't decode it; the ID
    # the tap decoded across the cut is B's row's, with the emergency and the alarm raised for it (once)
    audio = np.concatenate([hiss(3.0), unit(0.6), hiss(gap), ramped(mdc(B_ID, op)), hiss(2.0)])
    ((a, ida, burst_a, alarm_a, em_a), (b, idb, _, alarm_b, em_b)), alarms = tap_run(audio, mdc=both_ids, **TRAFFIC)
    assert b.split == "gap" and ida == [] and not burst_a and alarm_a == "" and not em_a
    assert idb == [f"{B_ID:04X}"]
    if op == PRE:
        assert not em_b and alarm_b == "" and alarms == []
    else:
        assert em_b and alarm_b == "alarm-1" and len(alarms) == 1


def test_the_drop_before_the_next_key_up_is_not_the_first_ones():
    # B's burst starts in the drop the cut was made at (its carrier rising for 0.2 s): measured up to the carrier back,
    # A's frames held part of it ("ID not decoded" on A's row); A's own end there is where the drop began
    audio = np.concatenate([hiss(3.0), unit(0.6), hiss(0.2), ramped(mdc(B_ID), 0.2), hiss(2.0)])
    ((a, ida, burst_a, *_), (b, idb, *_)), _alarms = tap_run(audio, mdc=both_ids, **TRAFFIC)
    assert b.split == "gap" and ida == [] and not burst_a and idb == [f"{B_ID:04X}"]


def test_each_key_up_keeps_its_own_ids_through_the_tap():
    # A's pre-ID and post-ID, 0.12 s, B's pre-ID: A's row A, B's row B (the cut piece's own frames hold A's)
    audio = np.concatenate([hiss(3.0), unit(0.6, pre=A_ID, post=A_ID), hiss(0.12), unit(1.5, pre=B_ID), hiss(2.0)])
    (a, ida), (b, idb) = tapped(audio, **TRAFFIC)
    assert ida == [f"{A_ID:04X}"] and idb == [f"{B_ID:04X}"]
    # A's post-ID only (its pre-ID window ends with the drop): A's, never B's
    audio = np.concatenate([hiss(3.0), unit(0.6, post=A_ID), hiss(0.12), unit(1.5, pre=B_ID), hiss(2.0)])
    (a, ida), (b, idb) = tapped(audio, **TRAFFIC)
    assert ida in ([], [f"{A_ID:04X}"]) and idb == [f"{B_ID:04X}"]
    # the same radio keying again: its ID on both rows
    audio = np.concatenate([hiss(3.0), unit(0.6, pre=A_ID), hiss(0.12), unit(1.5, pre=A_ID), hiss(2.0)])
    (a, ida), (b, idb) = tapped(audio, **TRAFFIC)
    assert ida == idb == [f"{A_ID:04X}"]


def test_a_held_cut_never_made_leaves_the_short_carriers_id_as_main():
    # A (no ID), 0.3 s, then only a pre-ID burst and 0.1 s of dead carrier (under min_open_s once the burst is left out):
    # the cut is never made, one row, and the tap hands it the burst's ID exactly as main does
    audio = np.concatenate([hiss(3.0), unit(0.6), hiss(0.3), quiet(0.05), mdc(B_ID), quiet(0.1), hiss(2.0)])
    main, new = tap_run(audio, drop_db=10.0)[0], tap_run(audio, **TRAFFIC)[0]
    assert len(new) == 1 and [(s.start, s.end, i, b) for s, i, b, *_ in new] == [(s.start, s.end, i, b)
                                                                                 for s, i, b, *_ in main]
    assert new[0][1] == [f"{B_ID:04X}"]


# -- paging (South 2) ------------------------------------------------------------------------------------------------

def at(audio, q_db):
    """Paging audio on a carrier that reads q_db of quieting against hiss()."""
    return audio + 0.4 * 10 ** (-q_db / 20) * np.random.default_rng(int(q_db * 10)).standard_normal(len(audio))


def page_defaults():
    """The NoiseSquelch a paging channel gets from its config defaults (App make_receiver)."""
    from sizeup.config import ChannelCfg
    c = ChannelCfg("South 2", 470.2, "paging")
    return dict(hang_s=c.hang_s, continue_s=c.continue_s, keyups=c.keyups, split_s=c.split_s or 0.0,
                jump_db=c.jump_db or None)


TONES = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0)])
PAGE_CASES = {
    # the carrier stepping up across a mid-voice fade (15->28, 18->31, 14->30 dB)
    **{f"step {a}->{b}": np.concatenate([hiss(4.0), at(np.concatenate([TONES, voice_like(4.0)]), a), hiss(0.6),
                                         at(voice_like(4.0), b), hiss(6.0)]) for a, b in ((15, 28), (18, 31), (14, 30))},
    "fade in the B tone": np.concatenate([hiss(4.0), at(np.concatenate([tone(349.1, 1.0), tone(311.1, 1.5)]), 15),
                                          hiss(0.6), at(np.concatenate([tone(311.1, 1.5), voice_like(5.0)]), 28),
                                          hiss(6.0)]),
    "mid-voice fade": np.concatenate([hiss(4.0), south2_page(), hiss(0.6), keyed(voice_like(6.0)), hiss(6.0)]),
    "2 s continuation +13 dB": np.concatenate([hiss(4.0), at(np.concatenate([TONES, voice_like(5.0)]), 15),
                                               hiss(2.0), at(voice_like(4.0), 28), hiss(6.0)]),
    "plain page": np.concatenate([hiss(4.0), south2_page(), hiss(6.0)]),
    "re-keyed after the tones": np.concatenate([hiss(4.0), keyed(TONES), hiss(1.0), keyed(voice_like(5.0)), hiss(6.0)]),
}


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


@pytest.mark.parametrize("case", list(PAGE_CASES))
def test_paging_is_the_squelch_before_key_ups_by_default(case):
    # off by default: every page closes on the same frame with the same audio and fields (the pre-alert waits on it)
    kw = page_defaults()
    assert kw["keyups"] == "" and kw["jump_db"] is None
    old, new = page_frames(PAGE_CASES[case], **PAGING), page_frames(PAGE_CASES[case], **kw)
    assert len(old) == len(new)
    for (o, ko), (n, kn) in zip(old, new):
        assert (o.start, o.end, o.reason, o.cont, o.hold_until, o.breaks, ko) == \
            (n.start, n.end, n.reason, n.cont, n.hold_until, n.breaks, kn)
        assert np.array_equal(o.audio, n.audio) and n.first_start is None and n.split == ""


def test_paging_opt_in_never_cuts_a_fade_inside_a_tone_or_a_short_one():
    # jump_db set on the channel: the B tone stepping up 13 dB across a 0.6 s fade stays one page (a tone rings into
    # the drop), and so does a fade no longer than the short hang
    segs = run_squelch(PAGE_CASES["fade in the B tone"], **PAGING_K)
    assert len(segs) == 1 and segs[0].first_start is None
    (page,) = held_pages(segs)
    assert page.kind == "page" and page.departments == ["Port Chester FD"]
    short = np.concatenate([hiss(4.0), at(np.concatenate([TONES, voice_like(4.0)]), 15), hiss(0.35),
                            at(voice_like(4.0), 30), hiss(6.0)])
    assert len(run_squelch(short, **PAGING_K)) == 1


def test_paging_opt_in_cuts_a_unit_answering_after_the_page():
    # 1 s after the page, a unit near the station (46 dB of quieting against the page's 26): another radio
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(1.0), strong(voice_like(2.0)), hiss(6.0)])
    assert len(run_squelch(audio, **PAGING)) == 1                  # off: one recording, the answer in the page
    segs = run_squelch(audio, **PAGING_K)
    assert len(segs) == 2 and segs[0].reason == "key-up" and segs[1].split == "level"
    page, answer = held_pages(segs)
    assert page.kind == "page" and page.departments == ["Port Chester FD"]
    assert len(page.voice) / RATE == pytest.approx(6.0, abs=0.5)
    assert answer.kind == "voice" and answer.keyup == {"parent": page.id, "prev": page.id, "split": "level"}
    assert page.keyup == {"parent": page.id, "prev": "", "split": ""}
    # ... and in the continuation window (2 s: past the 1.5 s hang): not joined to the page
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(2.0), strong(voice_like(2.0)), hiss(6.0)])
    assert run_squelch(audio, **PAGING)[1].cont                    # off: more of the page
    segs = run_squelch(audio, **PAGING_K)
    assert len(segs) == 2 and not segs[1].cont and segs[1].split == "level"
    page, ans = held_pages(segs)
    assert len(page.parts) == 1 and ans.kind == "voice" and ans.keyup["parent"] == page.id


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
    # an aircraft (+8 dB), 0.1 s gone, the tower (+18 dB): another, stronger transmitter
    levels = [(5, 0.01, False), (2.0, 0.025, True), (0.1, 0.01, False), (1.5, 0.08, True), (3, 0.01, False)]
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


def am_both(levels):
    """The same envelope through the squelch without key-ups (main) and with: (main's segments, these)."""
    clock = {"t": 1000.0}
    sq0 = CarrierSquelch(3.5, clock=lambda: clock["t"])
    sq1 = CarrierSquelch(3.5, clock=lambda: clock["t"], **AIRBAND)
    out0, out1 = [], []
    for f in envelope(levels, np.random.default_rng(7)):
        clock["t"] += FRAME / RATE
        if (x := sq0.feed(f)) is not None:
            out0.append(x)
        if (y := sq1.feed(f)) is not None:
            out1.append(y)
    return out0, out1


@pytest.mark.parametrize("reply", [0.3, 0.4, 0.5])
def test_a_short_readback_after_the_tower_is_one_transmission_as_main(reply):
    # tower, 0.25 s gone, a pilot's readback under 0.6 s of carrier, then nothing: the cut is held and never made
    main, new = am_both([(5, 0.01, False), (2.0, 0.08, True), (0.25, 0.01, False), (reply, 0.04, True),
                         (3, 0.01, False)])
    assert len(new) == 1 and same(main, new) and new[0].first_start is None


@pytest.mark.parametrize("reply", [0.6, 0.8])
def test_a_readback_of_0_6_s_or_more_is_its_own(reply):
    main, new = am_both([(5, 0.01, False), (2.0, 0.08, True), (0.25, 0.01, False), (reply, 0.04, True),
                         (3, 0.01, False)])
    assert len(main) == 1 and len(new) == 2 and new[1].split == "gap"
    assert new[0].reason == "key-up" and new[0].end - new[0].start == pytest.approx(0.4 + 2.0 + 0.2, abs=0.1)


def test_a_short_readback_then_another_key_up_in_the_hang():
    # tower, 0.25 s, a 0.3 s readback, 0.25 s, the tower again: the readback stays with the first, the second is cut
    main, new = am_both([(5, 0.01, False), (2.0, 0.08, True), (0.25, 0.01, False), (0.3, 0.04, True),
                         (0.25, 0.01, False), (1.5, 0.08, True), (3, 0.01, False)])
    assert len(main) == 1 and len(new) == 2 and new[1].split == "gap"
    assert new[0].start == main[0].start and new[1].end == pytest.approx(main[0].end, abs=1e-6)
    assert new[0].end - new[0].start == pytest.approx(0.4 + 2.0 + 0.25 + 0.3 + 0.2, abs=0.1)


def test_a_fade_recovering_on_a_ramp_is_not_cut():
    # a strong carrier fades to the floor for 0.1 s and recovers on a ramp (0.02 -> 0.03 -> 0.05): one transmission
    levels = [(5, 0.01, False), (2.0, 0.05, True), (0.1, 0.01, False), (0.05, 0.02, True), (0.05, 0.03, True),
              (1.5, 0.05, True), (3, 0.01, False)]
    assert len(am(levels, **AIRBAND)) == 1
    # back weaker after 0.1 s (not a full 0.15 s gap): the level rule is upward only
    levels = [(5, 0.01, False), (2.0, 0.08, True), (0.1, 0.01, False), (1.5, 0.025, True), (3, 0.01, False)]
    assert len(am(levels, **AIRBAND)) == 1


# -- config and the app ----------------------------------------------------------------------------------------------

def test_kind_defaults_and_config_keys(tmp_path):
    from sizeup.config import ChannelCfg, ConfigError, load
    assert (ChannelCfg("FG1", 453.0375, "traffic").split_s, ChannelCfg("FG1", 453.0375, "traffic").jump_db) == (0.2, 10.0)
    s2 = ChannelCfg("South 2", 470.2, "paging")
    assert (s2.split_s, s2.jump_db, s2.keyups) == (0.0, 0.0, "")              # paging: off by default
    assert ChannelCfg("South 2", 470.2, "paging", jump_db=12.0).keyups == "paging"
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
    assert (s2.keyups, s2._split_frames, s2.jump_db) == ("", 0, None)
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
    # each piece's own files under its own id: the clip people play and the exact audio speech-to-text was given
    import wave
    for t, seg in ((ta, a), (tb, b)):
        assert app.has_clip(t.id)
        with wave.open(str(app.clips_dir / f"{t.id}.src.wav")) as w:
            src = np.frombuffer(w.readframes(w.getnframes()), "<i2")
        assert np.array_equal(src, (np.clip(seg.audio, -1, 1) * 32767).astype(np.int16))
    # ... and each goes to the hub as its own event with its own clip (the station link: App._emit)
    sent_events = []
    app.events = lambda kind, data, clip: sent_events.append((kind, data["id"], data.get("keyup"), clip))
    for t in (ta, tb):
        t.raw, t.status = "copy", "done"
        app._transcribed(t)
    assert [(k, i, c) for k, i, _l, c in sent_events] == [("traffic", ta.id, ta.id), ("traffic", tb.id, tb.id)]
    assert sent_events[1][2] == {"parent": ta.id, "prev": ta.id, "split": "gap"}
    assert app.clip(ta.id) != app.clip(tb.id)


def test_the_next_radios_emergency_is_its_rows_with_the_live_alarm(tmp_path):
    # through App: the tap (App._mdc, App._fm_emergency) and App._segment, B's emergency burst on a rising carrier
    # right after A's short no-ID key-up: one alarm at key-up, B's row is the emergency and keeps that alarm's key
    from sizeup.app import App
    from sizeup.radio.fm import Channel
    from sizeup.radio.tap import TapSquelch
    from tests.test_app import cfg
    app = App(cfg(tmp_path))
    app._kind = {"FG1": "traffic"}
    alarms, rows = [], []
    app._alarm_now = lambda t: alarms.append(t)
    app._stt = lambda t, audio, engine, label: rows.append(t)
    clock = {"t": 1000.0}
    sq = TapSquelch(NoiseSquelch(clock=lambda: clock["t"], **TRAFFIC), app.tap, Channel(453.0375, "FG1"))
    audio = np.concatenate([hiss(3.0), unit(0.6), hiss(0.2), ramped(mdc(B_ID, (0x00, 0x80))), hiss(2.0)])
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        if (s := sq.feed(audio[i:i + FRAME])) is not None:
            app._segment(Channel(453.0375, "FG1"), s)
    ta, tb = rows
    live = alarms[0]                                    # raised by the tap at the key-up, before either row
    assert ta.radio_id == "" and not getattr(ta, "emergency", False) and getattr(ta, "id_note", "") != "undecoded"
    assert tb.radio_id == f"{B_ID:04X}" and tb.emergency and tb.alarm_key == live.id
    assert [t.id for t in alarms] == [live.id, tb.id]   # the row's own alarm carries the live one's key


# -- an ID burst on a slowly rising carrier (#694) -------------------------------------------------------------------

RAMPS = (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4)
EMERGENCY = (0x00, 0x80)                        # MDC emergency (the radio's button)
CONFIGS = [pytest.param(dict(drop_db=10.0), id="plain"), pytest.param(TRAFFIC, id="keyups")]


def idle(s, seed=0):
    """hiss() with its own noise, the same whatever ran before (these cases sit near what the decoders can take)."""
    return 0.4 * np.random.default_rng(1000 + seed).standard_normal(int(RATE * s))


def weak_start(burst, hold_s, q_db=8.0):
    """A radio keying up weak for hold_s (q_db of quieting: under open_db, the squelch stays shut), its ID burst at
    once, and then fully up."""
    n = int(RATE * hold_s)
    burst = np.concatenate([burst, np.zeros(max(0, n - len(burst)))])
    weak = burst + 0.4 * 10 ** (-q_db / 20) * np.random.default_rng(11).standard_normal(len(burst))
    return np.concatenate([weak, quiet(0.15), voice_like(1.5) + quiet(1.5), quiet(0.1)])


def fs(kind):
    return fleetsync.encode(100, 2001, kind)    # invented fleet / unit


def one_row(audio, kw, unit_id, emergency):
    """A single key-up through the live tap: its row names unit_id, and an emergency is alarmed once, with that row."""
    rows, alarms = tap_run(audio, mdc=both_ids, **kw)
    (_s, ids_, burst, key, em), = rows
    assert ids_ == [unit_id] and not burst
    if emergency:
        assert len(alarms) == 1 and alarms[0][1] == [(unit_id, True)] and key == "alarm-1" and em
    else:
        assert alarms == [] and key == "" and not em


@pytest.mark.parametrize("kw", CONFIGS)
@pytest.mark.parametrize("op", [PRE, EMERGENCY], ids=["pre-id", "emergency"])
@pytest.mark.parametrize("ramp", RAMPS)
def test_an_mdc_burst_on_a_slowly_rising_carrier_names_the_row_and_alarms_once(ramp, op, kw):
    # The burst begins with the carrier, which takes up to 0.4 s to quiet the hiss: the squelch opens up to ~0.3 s
    # into the burst, past the 0.2 s pre-roll (main: no ID from 0.35 s, and no alarm for the emergency button)
    one_row(np.concatenate([idle(3.0), ramped(mdc(B_ID, op), ramp), idle(2.0, 1)]), kw, f"{B_ID:04X}", op != PRE)


@pytest.mark.parametrize("kw", CONFIGS)
@pytest.mark.parametrize("kind", ["pre-id", "emergency"])
@pytest.mark.parametrize("ramp", RAMPS)
def test_a_fleetsync_burst_on_a_slowly_rising_carrier(ramp, kind, kw):
    # A FleetSync burst is short (45-90 ms): from a 0.2 s ramp on it is all in the worst of the hiss, and can't be
    # decoded from any of the audio. Then nothing is named and nothing alarmed (never a false one)
    tx = ramped(fs(kind), ramp)
    audio = np.concatenate([idle(3.0), tx, idle(2.0, 1)])
    if both_ids(tx):
        one_row(audio, kw, "100-2001", kind == "emergency")
    else:
        assert ramp >= 0.2
        rows, alarms = tap_run(audio, mdc=both_ids, **kw)
        assert [(i, k) for _s, i, _b, k, _e in rows] == [([], "")] and alarms == []


@pytest.mark.parametrize("kw", CONFIGS)
@pytest.mark.parametrize("burst", [mdc(B_ID, EMERGENCY), fs("emergency")], ids=["mdc", "fleetsync"])
@pytest.mark.parametrize("hold", [0.1, 0.2, 0.3, 0.4, 0.5])
def test_an_emergency_on_a_weak_start_before_the_squelch_opens_alarms_once_with_the_row(hold, burst, kw):
    # The radio comes up weak (8 dB of quieting, under open_db) for up to 0.5 s with its emergency packet at once:
    # the burst ends up to 0.3 s before the pre-roll (main: no alarm from 0.3 s)
    unit_id = "100-2001" if len(burst) < len(mdc(B_ID)) else f"{B_ID:04X}"
    one_row(np.concatenate([idle(3.0), weak_start(burst, hold), idle(2.0, 1)]), kw, unit_id, True)


@pytest.mark.parametrize("talk", [0.6, 2.0])
@pytest.mark.parametrize("op", [PRE, EMERGENCY], ids=["pre-id", "emergency"])
@pytest.mark.parametrize("ramp", RAMPS)
def test_after_a_key_up_the_next_radios_burst_on_a_rising_carrier_is_its_rows(ramp, op, talk):
    # A (no ID) unkeys, B keys up 0.2 s later on a slowly rising carrier with its pre-ID / emergency: cut at the key-up
    # (or closed and opened again), B's row has B's ID, an emergency is alarmed once with B's row, A's row has neither.
    # (main: B unnamed from a 0.05 s ramp with A at 0.6 s, #695; from 0.1 s with A at 2 s, past its window; no alarm)
    audio = np.concatenate([idle(3.0), unit(talk), idle(0.2, 1), ramped(mdc(B_ID, op), ramp), idle(2.0, 2)])
    rows, alarms = tap_run(audio, mdc=both_ids, **TRAFFIC)
    *a_rows, (_b, idb, _bb, key_b, em_b) = rows
    assert a_rows and all(i == [] and k == "" for _s, i, _b, k, _e in a_rows)
    assert idb == [f"{B_ID:04X}"]
    if op == PRE:
        assert alarms == [] and key_b == "" and not em_b
    else:
        assert len(alarms) == 1 and key_b == "alarm-1" and em_b


@pytest.mark.parametrize("op", [PRE, EMERGENCY], ids=["pre-id", "emergency"])
@pytest.mark.parametrize("burst", ["mdc", "fleetsync"])
def test_the_rise_rules_piece_with_no_row_hands_its_id_on(burst, op):
    # #695: after a key-up cut, the next radio's carrier comes up fast (0.05 s) with its ID at once; the rise rule cuts
    # its first ~0.2 s off as a background with no row. What the tap decoded there is the next piece's (its row)
    b = mdc(B_ID, op) if burst == "mdc" else fs("pre-id" if op == PRE else "emergency")
    unit_id = f"{B_ID:04X}" if burst == "mdc" else "100-2001"
    audio = np.concatenate([idle(3.0), unit(0.6), idle(0.2, 1), ramped(b, 0.05), idle(2.0, 2)])
    rows, alarms = tap_run(audio, mdc=both_ids, **TRAFFIC)
    ((_a, ida, _ba, key_a, _ea), (_b, idb, _bb, key_b, em_b)) = rows
    assert ida == [] and key_a == "" and idb == [unit_id]
    assert (len(alarms), key_b, em_b) == ((0, "", False) if op == PRE else (1, "alarm-1", True))


@pytest.mark.parametrize("kw", CONFIGS)
@pytest.mark.parametrize("seed", range(6))
def test_slow_carriers_and_hiss_make_no_id_and_no_alarm(seed, kw):
    # The look-back reads 0.4 s more hiss and rising carrier before each key-up: packets are CRC-checked, nothing in
    # it is an ID, and the "ID not decoded" flag is unchanged (only the pre-roll and the carrier are looked at)
    rng = np.random.default_rng(seed)
    parts = [idle(3.0, seed)]
    for ramp in (0.0, 0.1, 0.2, 0.3, 0.4):
        n = int(RATE * ramp)
        parts += [0.4 * np.linspace(1.0, 10 ** (-45 / 20), n) * rng.standard_normal(n), quiet(0.3),
                  voice_like(1.2) + quiet(1.2), quiet(0.1), idle(1.5, seed + 10)]
    rows, alarms = tap_run(np.concatenate(parts), mdc=both_ids, **kw)
    assert len(rows) == 5 and alarms == []
    assert all(i == [] and not b and k == "" for _s, i, b, k, _e in rows)


@pytest.mark.parametrize("op", [POST, PRE], ids=["post-id", "pre-id"])
def test_a_weak_radios_id_well_before_the_key_up_is_not_its(op):
    # A weak radio (6-8 dB of quieting: the squelch stays shut) sends its ID and unkeys; 0.3 s later another radio keys
    # up with no ID. That burst is in the look-back but ended before the pre-roll: not the key-up's
    weak = mdc(A_ID, op) + 0.4 * 10 ** (-7 / 20) * np.random.default_rng(5).standard_normal(len(mdc(A_ID, op)))
    audio = np.concatenate([idle(3.0), weak, idle(0.3, 1), unit(1.0), idle(2.0, 2)])
    for kw in (dict(drop_db=10.0), TRAFFIC):
        rows, alarms = tap_run(audio, mdc=both_ids, **kw)
        assert [i for _s, i, *_ in rows] == [[]] and alarms == []


def test_a_slow_emergency_is_its_rows_with_the_live_alarm(tmp_path):
    # through App (App._mdc, App._fm_emergency, App._segment): an emergency on a 0.4 s ramp, alarmed at the key-up,
    # once; the row is the emergency and keeps that alarm's key
    from sizeup.app import App
    from sizeup.radio.fm import Channel
    from sizeup.radio.tap import TapSquelch
    from tests.test_app import cfg
    app = App(cfg(tmp_path))
    app._kind = {"FG1": "traffic"}
    alarms, rows = [], []
    app._alarm_now = lambda t: alarms.append(t)
    app._stt = lambda t, audio, engine, label: rows.append(t)
    clock = {"t": 1000.0}
    sq = TapSquelch(NoiseSquelch(clock=lambda: clock["t"], **TRAFFIC), app.tap, Channel(453.0375, "FG1"))
    audio = np.concatenate([idle(3.0), ramped(mdc(B_ID, EMERGENCY), 0.4), idle(2.0, 1)])
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        if (s := sq.feed(audio[i:i + FRAME])) is not None:
            app._segment(Channel(453.0375, "FG1"), s)
    (t,) = rows
    live = alarms[0]                                    # raised by the tap at the key-up
    assert t.radio_id == f"{B_ID:04X}" and t.emergency and t.alarm_key == live.id
    assert getattr(t, "id_note", "") == ""
    assert [a.id for a in alarms] == [live.id, t.id]   # the row's own alarm carries the live one's key


def a_then(a_tx, gap, b_tx):
    rows, alarms = tap_run(np.concatenate([idle(3.0), a_tx, idle(gap, 1), b_tx, idle(2.0, 2)]), mdc=both_ids, **TRAFFIC)
    return [(i, k) for _s, i, _b, k, _e in rows], alarms


@pytest.mark.parametrize("gap", [0.1, 0.12])
@pytest.mark.parametrize("b_id", [B_ID, None])
def test_the_look_back_never_hands_the_radio_befores_post_id_to_the_next(b_id, gap):
    # A's post-ID right before a short drop, B keying up at once: the drop alone is B's pre-roll, never A's tail
    a_tx = np.concatenate([quiet(0.15), voice_like(0.8) + quiet(0.8), quiet(0.1), mdc(A_ID, POST), quiet(0.05)])
    rows, alarms = a_then(a_tx, gap, unit(1.5, pre=b_id))
    assert rows[0] == ([f"{A_ID:04X}"], "") and rows[-1][0] == ([f"{B_ID:04X}"] if b_id else []) and alarms == []


def test_the_radio_befores_emergency_is_not_the_next_ones_and_the_next_ones_own_is_raised():
    # A's emergency-only key-up, then B 0.1 s later: with no ID B's row is clean; with its own emergency it is raised
    # (a second alarm, B's, never under A's key)
    a_tx = np.concatenate([quiet(0.15), mdc(A_ID, EMERGENCY), quiet(0.15)])
    rows, alarms = a_then(a_tx, 0.1, unit(1.5))
    assert len(alarms) == 1 and rows[0] == ([f"{A_ID:04X}"], "alarm-1") and rows[-1] == ([], "")
    b_tx = np.concatenate([quiet(0.15), mdc(B_ID, EMERGENCY), quiet(0.15), voice_like(1.5) + quiet(1.5), quiet(0.1)])
    rows, alarms = a_then(a_tx, 0.1, b_tx)
    assert [a[1] for a in alarms] == [[(f"{A_ID:04X}", True)], [(f"{B_ID:04X}", True)]]
    assert rows[0] == ([f"{A_ID:04X}"], "alarm-1") and rows[-1] == ([f"{B_ID:04X}"], "alarm-2")


def test_an_emergency_at_the_end_of_the_radio_befores_transmission_stays_its():
    # A long transmission ending in an emergency packet (past the tap's window: its row alarms from the segment), B
    # 0.15 s later: the packet is in B's look-back, in A's carrier, never B's
    a_tx = np.concatenate([quiet(0.15), voice_like(2.0) + quiet(2.0), quiet(0.1), mdc(A_ID, EMERGENCY)])
    rows, alarms = a_then(a_tx, 0.15, unit(1.5))
    assert rows[-1] == ([], "") and alarms == []


C_ID = 0x3579                                   # invented


@pytest.mark.parametrize("kw", CONFIGS)
@pytest.mark.parametrize("c_id", [C_ID, None, B_ID])
def test_a_weak_radios_emergency_is_alarmed_on_its_own_and_never_names_the_radio_that_opens(c_id, kw):
    # B's emergency while its carrier is too weak to open the squelch (7 dB), then C keys up 0.3 s later. B's button is
    # alarmed once, at once, as B's. C's row is C's alone when C sends its own ID; with no ID (or B itself coming up
    # strong) the key-up is B's, and its row carries the emergency and that alarm
    weak = mdc(B_ID, EMERGENCY) + 0.4 * 10 ** (-7 / 20) * np.random.default_rng(5).standard_normal(len(mdc(B_ID)))
    rows, alarms = tap_run(np.concatenate([idle(3.0), weak, idle(0.3, 1), unit(1.5, pre=c_id), idle(2.0, 2)]),
                           mdc=both_ids, **kw)
    (_s, ids_, _b, key, em), = rows
    assert [a[1] for a in alarms] == [[(f"{B_ID:04X}", True)]]
    assert alarms[0][0] < _s.start - 0.2                  # keyed to the burst, not to C's carrier
    if c_id == C_ID:
        assert ids_ == [f"{C_ID:04X}"] and key == "" and not em
    else:
        assert f"{B_ID:04X}" in ids_ and key == "alarm-1" and em


@pytest.mark.parametrize("op", [POST, EMERGENCY], ids=["post-id", "emergency"])
@pytest.mark.parametrize("gap", [0.12, 0.2, 0.3])
@pytest.mark.parametrize("talk", [0.8, 3.0])
def test_a_burst_sent_to_the_last_moment_stays_with_its_radio(talk, gap, op):
    # A's post-ID / emergency is the very last thing on its carrier (no dead air after it), B keys up after the gap:
    # the burst's last partial frame is the drop's first. A's row has A (the tap and its segment's cut burst), B's
    # never has A's ID or A's alarm. (#690: B's row took them while A's key-up window was open; the cut burst lost its
    # last frame and didn't decode by itself)
    def seg_ids(seg):           # as App._mdc: each cut burst and the audio decoded on its own
        return [h for b in [*seg.bursts, seg.audio] for h, _e in both_ids(b)]
    a = np.concatenate([quiet(0.15), voice_like(talk) + quiet(talk), quiet(0.1), mdc(A_ID, op)])
    rows, alarms = tap_run(np.concatenate([idle(3.0), a, idle(gap, 1), unit(1.5, pre=B_ID), idle(2.0, 2)]),
                           mdc=both_ids, **TRAFFIC)
    (sa, ida, _ba, key_a, _ea), (sb, idb, _bb, key_b, _eb) = rows
    assert f"{A_ID:04X}" in ida and f"{A_ID:04X}" in seg_ids(sa) if talk < 1.0 else f"{A_ID:04X}" in seg_ids(sa)
    assert idb == [f"{B_ID:04X}"] and key_b == "" and f"{A_ID:04X}" not in seg_ids(sb)
    assert key_a == ("alarm-1" if op == EMERGENCY and talk < 1.0 else "")
    # and without a key-up after it, the segment still holds it (the long transmission's tap window is long past)
    rows, _ = tap_run(np.concatenate([idle(3.0), a, idle(2.0, 2)]), mdc=both_ids, **TRAFFIC)
    (s, *_), = rows
    assert f"{A_ID:04X}" in seg_ids(s)


@pytest.mark.parametrize("gap", [0.12, 0.3])
def test_a_second_press_right_after_is_its_own_alarm(gap):
    # A presses the button, then again 0.12 / 0.3 s after unkeying, the second burst on a slowly rising carrier (found
    # only with the look-back): two presses, two alarms; one burst across two carriers stays one alarm
    a = np.concatenate([quiet(0.15), mdc(A_ID, EMERGENCY), quiet(0.15), voice_like(0.5) + quiet(0.5), quiet(0.1)])
    rows, alarms = tap_run(np.concatenate([idle(3.0), a, idle(gap, 1), ramped(mdc(A_ID, EMERGENCY), 0.35),
                                           idle(2.0, 2)]), mdc=both_ids, **TRAFFIC)
    assert len(alarms) == 2 and rows[0][3] == "alarm-1" and rows[-1][3] == "alarm-2"
    rows, alarms = tap_run(np.concatenate([idle(3.0), unit(0.6), idle(0.1, 1), ramped(mdc(B_ID, EMERGENCY), 0.35),
                                           idle(2.0, 2)]), mdc=both_ids, **TRAFFIC)
    assert len(alarms) == 1 and rows[-1][1] == [f"{B_ID:04X}"] and rows[-1][3] == "alarm-1"
