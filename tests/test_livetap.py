"""The live tap (sizeup/radio/tap.py): South 2 pages shown as they go out (radio/livepage.py, #63) and
FG1's MDC pre-ID shown at key-up (#65)."""


import numpy as np
import pytest

from sizeup.radio.fm import FRAME, RATE, Channel, NoiseSquelch
from sizeup.radio.live import LiveTalk
from sizeup.radio.livepage import HOLD_S, RECENT_S, LivePaging
from sizeup.radio.mdc1200 import decode as mdc_decode
from sizeup.radio.mdc1200 import encode
from sizeup.radio.tap import LiveTap
from sizeup.radio.tones import ToneCode, ToneDetector, TonePlan

rng = np.random.default_rng(5)
PLAN = TonePlan([ToneCode("Port Chester FD", 349.1, 311.1, "PCFD"), ToneCode("Rye Brook FD (est.)", 669.9, 1092.4),
                 ToneCode("Ardsley", 1031.8, 763.3), ToneCode("Scarsdale EMS (heard)", 928.1, 810.2)])
OURS = {"Port Chester FD", "Rye Brook FD"}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def tone(hz, s, amp=0.5):
    return amp * np.sin(2 * np.pi * hz * np.arange(int(RATE * s)) / RATE + rng.uniform(0, 6.28))


def quiet(s):
    return np.zeros(int(RATE * s))


def voice(s):
    t = np.arange(int(RATE * s)) / RATE
    ph = 2 * np.pi * np.cumsum(130 + 20 * np.sin(2 * np.pi * 1.1 * t)) / RATE
    syll = (np.sin(2 * np.pi * 3 * t) > 0.2).astype(float)
    return 0.3 * syll * sum(np.sin(k * ph) / k for k in range(1, 12))


def carrier(x):
    """As demodulated with the carrier up: a little noise under everything."""
    return x + 0.01 * rng.standard_normal(len(x))


def frames(x):
    return [x[i:i + FRAME] for i in range(0, len(x) - FRAME + 1, FRAME)]


def batch(x):
    d = ToneDetector(RATE)
    return [(round(e.a_hz, 1), e.b_hz and round(e.b_hz, 1)) for e in PLAN.match_all(d.feed(x) + d.flush())]


def live_feed(lp, x, since=0.0, channel="South 2", clock=None):
    for f in frames(x):
        lp.feed(channel, since, f)
        if clock is not None:
            clock.t += FRAME / RATE


PAGES = {
    "single": [(349.1, 311.1)],
    "stacked": [(349.1, 311.1), (669.9, 1092.4), (1031.8, 763.3)],
    "unknown": [(741.3, 623.7)],
}


@pytest.mark.parametrize("case", [*PAGES, "then_voice"])
def test_live_pairs_are_the_batch_pairs(case):
    pairs = PAGES.get(case, [(928.1, 810.2)])
    x = np.concatenate([quiet(0.3)] + [tone(h, d) for a, b in pairs for h, d in ((a, 1.0), (b, 3.0))]
                       + ([quiet(0.3), voice(3.0)] if case == "then_voice" else [quiet(0.4)]))
    x = carrier(x)
    want = batch(x)
    assert len(want) == len(pairs)
    lp = LivePaging(PLAN, OURS)
    live_feed(lp, x)
    lp.close("South 2")
    [st] = lp.state()
    got = [(p["a"], p["b"]) for p in st["pairs"]]
    assert len(got) == len(want)
    for (ga, gb), (wa, wb) in zip(got, want):
        assert ga == pytest.approx(wa, abs=1.5) and gb == pytest.approx(wb, abs=1.5)
    names = [p["names"] for p in st["pairs"]]
    if case == "unknown":
        assert names == [[]] and not st["ours"]                    # shown as its tones, never a guess
    if case == "stacked":
        assert names == [["Port Chester FD"], ["Rye Brook FD (est.)"], ["Ardsley"]]
        assert [p["ours"] for p in st["pairs"]] == [True, True, False] and st["ours"]
    assert st["phase"] == ("voice" if case == "then_voice" else "tones")


def test_a_pair_shows_before_its_b_tone_ends_and_stacked_ones_are_added():
    clock = Clock()
    changes = []
    lp = LivePaging(PLAN, OURS, clock=clock, on_change=lambda: changes.append(1))
    live_feed(lp, carrier(np.concatenate([quiet(0.2), tone(349.1, 1.0)])), clock=clock)
    assert lp.state() == []                                        # an A alone is nothing yet
    live_feed(lp, carrier(tone(311.1, 0.8)), clock=clock)          # 0.8 s into B: Port Chester, 2.2 s early
    [st] = lp.state()
    assert [(p["names"], p["ours"]) for p in st["pairs"]] == [(["Port Chester FD"], True)]
    assert st["channel"] == "South 2" and st["phase"] == "tones" and not st["ended"] and changes
    n = len(changes)
    live_feed(lp, carrier(tone(311.1, 2.2)), clock=clock)          # the rest of B: no churn on screen
    assert len(changes) - n <= 1
    live_feed(lp, carrier(np.concatenate([tone(741.3, 1.0), tone(623.7, 1.0)])), clock=clock)
    assert [p["names"] for p in lp.state()[0]["pairs"]] == [["Port Chester FD"], []]


def test_phases_tones_voice_ended_then_gone():
    clock = Clock()
    lp = LivePaging(PLAN, OURS, clock=clock)
    live_feed(lp, carrier(np.concatenate([tone(928.1, 1.0), tone(810.2, 3.0), quiet(0.4)])), clock=clock)
    assert lp.state()[0]["phase"] == "tones"                      # dead carrier after the tones: not voice
    live_feed(lp, carrier(voice(1.5)), clock=clock)
    st = lp.state()[0]
    assert st["phase"] == "voice" and st["pairs"][0]["names"] == ["Scarsdale EMS (heard)"]
    lp.close("South 2")
    assert lp.state()[0]["phase"] == "voice"                       # closed: still shown until the page is built
    assert lp.finished("South 2", "pg1")
    st = lp.state()[0]
    assert st["ended"] and st["page_id"] == "pg1" and st["until"] == pytest.approx(clock.t + RECENT_S)
    clock.t += RECENT_S + 0.1
    assert lp.state() == []
    # Plain voice on South 2 (no tones) is not a page: nothing shown, and finishing it says so.
    live_feed(lp, carrier(voice(3.0)), since=2000.0, clock=clock)
    assert lp.state() == [] and not lp.finished("South 2", "pg2")


def test_a_short_fade_does_not_end_the_page_and_a_dead_one_expires():
    clock = Clock()
    lp = LivePaging(PLAN, OURS, clock=clock)
    live_feed(lp, carrier(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0)])), since=1000.0, clock=clock)
    key = lp.state()[0]["key"]
    lp.close("South 2")                                            # the repeater fades for 0.6 s
    clock.t += 0.6
    assert lp.state()[0]["key"] == key
    live_feed(lp, carrier(np.concatenate([tone(669.9, 1.0), tone(1092.4, 3.0), quiet(0.3)])), since=clock.t, clock=clock)
    [st] = lp.state()
    assert st["key"] == key and [p["names"] for p in st["pairs"]] == [["Port Chester FD"], ["Rye Brook FD (est.)"]]
    lp.close("South 2")
    clock.t += HOLD_S - 0.5
    assert lp.state()                                              # still waiting for the page or the carrier
    clock.t += 1.0
    assert lp.state() == []                                        # nothing came: gone
    # A carrier long after an ended page is a new page.
    live_feed(lp, carrier(np.concatenate([tone(349.1, 1.0), tone(311.1, 1.0)])), since=clock.t + 30, clock=clock)
    assert lp.state()[0]["key"] != key


# -- the tap on a receiver's frames: the squelch, South 2 and FG1 --------------------------------------------


def hiss(s):
    return 0.4 * rng.standard_normal(int(RATE * s))


def keyed(x):
    t = np.arange(len(x)) / RATE
    return x + 0.1 * np.sin(2 * np.pi * 156.7 * t) + 0.02 * rng.standard_normal(len(x))


class Rig:
    """A receiver's squelch with the tap on it, fed frame by frame on a fake clock."""

    def __init__(self, label="FG1", kind="traffic"):
        self.clock = Clock()
        self.sq = NoiseSquelch(clock=self.clock)
        self.ch = Channel(453.0375 if kind == "traffic" else 470.2, label)
        resolve = lambda s, rid, a: {"6301": {"unit": "E63"}, "0611": {"unit": "E61"}}.get(rid)
        self.live = LiveTalk(resolve=resolve, clock=self.clock)
        self.paging = LivePaging(PLAN, OURS, clock=self.clock)
        self.tap = LiveTap(lambda _l: kind, self.paging, self.live,
                           mdc=lambda a: [(p.unit_hex, False) for p in mdc_decode(a, RATE)], clock=self.clock)
        self.segs = []

    def feed(self, x, until=None):
        """Frames in; stop (returning the clock) when until(self) is true."""
        for f in frames(x):
            self.clock.t += FRAME / RATE
            seg = self.sq.feed(f)
            self.tap.frame(self.ch, f, self.sq)
            if seg is not None:
                self.segs.append(seg)
            if until is not None and until(self):
                return self.clock.t
        return None


def opened_at(r):
    return r.sq._start if r.sq.open else None


@pytest.mark.parametrize("at", [0.0, 0.3, 0.8])
def test_fg1_pre_id_shows_the_talker_before_the_carrier_closes(at):
    r = Rig()
    pre = encode(0x01, 0x80, 0x6301)
    x = np.concatenate([hiss(3.0), keyed(np.concatenate([quiet(at), pre, voice(3.0)])), hiss(1.0)])
    keyup = 3.0
    seen = r.feed(x, until=lambda r: any(e["rig"] == "E63" for e in r.live.state()))
    assert seen is not None and r.sq.open and not r.segs            # named while the carrier is still up
    t_key = 1000.0 + keyup
    pre_end = at + len(pre) / RATE
    assert seen - t_key < pre_end + 0.5                            # within 0.5 s of the burst's end
    if at == 0.0:
        assert seen - t_key < 1.0                                  # an ID at key-up: under 1 s
    [e] = r.live.state()
    assert (e["radio_id"], e["rig"], e["id_on_release"], e["ended"]) == ("6301", "E63", False, False)
    r.feed(x[int((seen - 1000.0) * RATE):])
    assert len(r.segs) == 1
    seg = r.segs[0]
    assert r.tap.ids("FG1", seg.start, seg.end) == [("6301", False)]   # handed to the finished transmission


def test_fg1_pre_id_at_key_up_needs_the_pre_roll():
    # The carrier takes ~0.15 s to quiet the hiss, so the squelch opens a few frames in, and a burst sent
    # with the carrier has lost its preamble by then (E63, 2026-10-13 13:17: named only at release).
    burst = keyed(np.concatenate([encode(0x01, 0x80, 0x6301), voice(1.0)]))
    burst += hiss(len(burst) / RATE) * np.clip(1 - np.arange(len(burst)) / (0.15 * RATE), 0, 1)
    r = Rig()
    r.feed(np.concatenate([hiss(3.0), burst, hiss(1.0)]))
    seg = r.segs[0]
    assert mdc_decode(np.concatenate(seg.bursts + [seg.audio]).astype(np.float32), RATE) == []   # the segment: gone
    assert r.tap.ids("FG1", seg.start, seg.end) == [("6301", False)]                              # the tap's pre-roll


def test_fg1_post_id_only_is_id_on_release_until_the_close():
    r = Rig()
    post = encode(0x01, 0x00, 0x0611)
    x = np.concatenate([hiss(3.0), keyed(np.concatenate([voice(2.0), post, quiet(0.1)])), hiss(1.0)])
    r.feed(x, until=lambda r: r.sq.open and r.clock.t > 1000.0 + 3.0 + 1.6)
    [e] = r.live.state()
    assert e["id_on_release"] is True and e["radio_id"] == ""      # "FG1 · ID on release": no pre-ID found
    r.feed(x[int((r.clock.t - 1000.0) * RATE):])
    seg = r.segs[0]
    assert r.tap.ids("FG1", seg.start, seg.end) == []
    ids = list(dict.fromkeys(p.unit_hex for p in mdc_decode(np.concatenate(seg.bursts + [seg.audio]).astype(np.float32), RATE)))
    assert ids == ["0611"]
    r.live.fm_segment("FG1", ids, seg.start, seg.end)              # what App._segment does: the post-ID fills it in
    assert [(e["rig"], e["ended"]) for e in r.live.state()] == [("E61", True)]


def test_fg1_pre_and_post_ids_that_differ_are_both_kept(tmp_path):
    from sizeup.app import App
    from tests.test_app import FakeWhisper, cfg, wait
    app = App(cfg(tmp_path))
    app._kind = {"FG1": "traffic"}
    app.stt.client = FakeWhisper({k: "Engine 63 to command" for k in range(1, 8)})
    app.stt.start()
    r = Rig()
    r.live, r.tap = app.live, app.tap
    x = np.concatenate([hiss(3.0), keyed(np.concatenate([encode(0x01, 0x80, 0x6301), voice(2.0),
                                                         encode(0x01, 0x00, 0x0611), quiet(0.1)])), hiss(1.0)])
    seen = r.feed(x, until=lambda r: any(e["radio_id"] == "6301" for e in app.live.state()))
    assert seen is not None and r.sq.open
    r.feed(x[int((seen - 1000.0) * RATE):])
    seg = r.segs[0]
    app._segment(r.ch, seg)
    assert wait(lambda: any(t.status == "done" for t in app.store.loose))
    t = app.store.loose[-1]
    assert set(t.radio_id.split(", ")) == {"6301", "0611"}          # both, never one picked
    flashed = [(e["radio_id"], e["ended"]) for e in app.live.state()]
    assert ("0611", True) in flashed                               # the post-ID the live decode didn't have


def test_south2_through_the_tap_live_then_finished(tmp_path):
    r = Rig("South 2", "paging")
    x = np.concatenate([hiss(3.0), keyed(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), quiet(0.3), voice(2.0)])),
                        hiss(1.0)])
    t = r.feed(x, until=lambda r: bool(r.paging.state()))
    assert t is not None and r.sq.open and t - 1003.0 < 2.2          # Port Chester shown ~0.8 s into its B tone
    t = r.feed(x[int((t - 1000.0) * RATE):], until=lambda r: r.paging.state()[0]["phase"] == "voice")
    assert t is not None and r.sq.open
    r.feed(x[int((t - 1000.0) * RATE):])
    assert len(r.segs) == 1 and r.paging.state()[0]["phase"] == "voice"   # closed, the page not built yet
    from sizeup.radio.page import assemble
    page = assemble("South 2", 470.2, r.segs[0], PLAN)
    assert [t.names for t in page.tones] == [("Port Chester FD",)]   # the batch decode agrees
    r.paging.finished("South 2", page.id)
    assert r.paging.state()[0]["ended"]


def test_the_tap_rides_on_the_receivers_squelch_and_every_new_one(tmp_path):
    from sizeup.radio.fm import NfmReceiver
    got = []
    r = NfmReceiver("rtl_fm", "", None, Channel(453.0375, "FG1"), lambda ch, seg: got.append(seg))   # never started
    seen = []

    class Tap(LiveTap):
        def frame(self, channel, frame, squelch):
            seen.append((channel.label, squelch.open))
    Tap(lambda _l: "traffic").attach(r)
    for f in frames(hiss(3.0)):
        assert r.squelch.feed(f) is None
    assert len(seen) == 60 and seen[0] == ("FG1", False) and r.squelch.open is False
    assert r.status()["open"] is False                              # the receiver reads the squelch as before
    fresh = r.new_squelch()                                         # an rtl_fm restart: the tap is on the new one too
    fresh.feed(frames(hiss(0.05))[0])
    assert len(seen) == 61
