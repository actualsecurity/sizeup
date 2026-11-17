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
