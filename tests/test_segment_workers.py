"""A finished transmission is handled off the receiver's reader (radio/fm.py SegmentWorkers): one worker per channel, in
order, bounded, flushed at a stop. The reader keeps reading rtl_fm's pipe while App._segment works on a long
transmission (2 minutes: about 1 s on a fast PC, 4 s on the station's i5, against a pipe that holds about 2 s), and
what must be immediate stays on the reader: the emergency alarm at the key-up and a page going out (radio/tap.py)."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import numpy as np

from sizeup.radio import fm
from sizeup.radio.am import AmReceiver, CarrierSquelch
from sizeup.radio.fm import FRAME, RATE, Channel, NfmReceiver, NoiseSquelch, Segment, SegmentWorkers
from sizeup.radio.live import LiveTalk
from sizeup.radio.livepage import LivePaging
from sizeup.radio.tap import LiveTap
from tests.test_keyups import B_ID, EMERGENCY, TRAFFIC, both_ids, idle, mdc, quiet, unit, voice_like
from tests.test_page import PAGING, PLAN, hiss, keyed, south2_page
from tests.test_squelch_stuck import fake_rtl_fm
from tests.test_sdr_auto_assign import DONGLES, rig  # noqa: F401  (fixture)


def wait(cond, s=30.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


class _Live:
    def fm_open(self, label, since):
        pass

    def fm_ids(self, label, ids):
        pass


class _Pipe:
    """rtl_fm's stdout as NfmReceiver._pump reads it."""

    def __init__(self, fd):
        self.stdout, self._fd = self, fd

    def fileno(self):
        return self._fd

    def poll(self):
        return None


class _Clocked:
    """The receiver's squelch on a clock that moves 50 ms a frame (the audio is read faster than real time), each
    frame fed counted."""

    def __init__(self, sq, clock, fed):
        self.__dict__.update(_sq=sq, _clock=clock, _fed=fed)

    def feed(self, frame):
        self._clock["t"] += FRAME / RATE
        seg = self._sq.feed(frame)
        self._fed.append(self._clock["t"])
        return seg

    def flush(self, reason="stopped"):
        return self._sq.flush(reason)

    def __getattr__(self, name):
        return getattr(self._sq, name)

    def __setattr__(self, name, value):
        setattr(self._sq, name, value)


def receiver(label, kind, on_segment, mdc=None, paging=None, clock=None, **kw):
    """A receiver as App makes one, with the live tap on it and its workers as start() sets them up (no rtl_fm):
    (receiver, tap, frames fed, events, the alarms the tap raised)."""
    clock, fed, events, alarms = clock or {"t": 1000.0}, [], [], []
    r = NfmReceiver("rtl_fm", "", None, Channel(453.0375, label), on_segment, events.append, **kw)
    r.new_squelch = lambda: NoiseSquelch(clock=lambda: clock["t"], **kw)
    tap = LiveTap(lambda _l: kind, paging, _Live(), mdc=mdc, clock=lambda: clock["t"])
    tap.on_emergency = lambda lab, since, found: alarms.append((since, list(found))) or f"alarm-{len(alarms)}"
    tap.attach(r)
    r.squelch = _Clocked(r.squelch, clock, fed)
    r.segments = SegmentWorkers(label, r.on_segment, r.on_event)
    return r, tap, fed, events, alarms


def pump(r, audio):
    """The receiver's own reader (_pump) on this audio, written into a pipe as fast as it is read, then its flush."""
    rd, wr = os.pipe()
    pcm = memoryview((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())

    def write():
        try:
            n = 0
            while n < len(pcm):
                n += os.write(wr, pcm[n:n + 65536])
        finally:
            os.close(wr)

    def read():
        try:
            r._pump(_Pipe(rd))
            r._flush("rtl_fm ended")
        finally:
            os.close(rd)
    threading.Thread(target=write, daemon=True).start()
    t = threading.Thread(target=read, daemon=True)
    t.start()
    return t


def held_first(got):
    """An on_segment whose first transmission takes as long as the test says (App._segment on a long one)."""
    held, release = threading.Event(), threading.Event()

    def on_segment(ch, seg):
        got.append(seg)
        if len(got) == 1:
            held.set()
            release.wait(60)
    return on_segment, held, release


# -- the reader is never held up --------------------------------------------------------------------------------------

def test_a_long_transmissions_handling_never_holds_up_the_reader():
    got = []
    on_segment, held, release = held_first(got)
    r, _tap, fed, _events, _alarms = receiver("FG1", "traffic", on_segment, **TRAFFIC)
    audio = np.concatenate([idle(3.0), unit(4.0), idle(1.5, 1), unit(2.0), idle(1.5, 2), unit(1.0), idle(2.0, 3)])
    t = pump(r, audio)
    try:
        assert held.wait(30)
        n0 = len(fed)
        # the first is still being handled, and the reader reads on to the end of the audio (3 s of it and more)
        t.join(30)
        assert not t.is_alive() and len(fed) - n0 >= 60 and len(got) == 1
        assert r.pending("FG1") and r.segments.pending("FG1") == 3      # (the one being handled, and two waiting)
    finally:
        release.set()
    assert r.segments.drain(30) and not r.pending()
    assert len(got) == 3 and [s.start for s in got] == sorted(s.start for s in got)


def test_without_its_workers_a_receiver_hands_each_transmission_on_at_once_as_before():
    got = []
    r = NfmReceiver("rtl_fm", "", None, Channel(453.0375, "FG1"), lambda ch, seg: got.append(seg))   # never started
    seg = Segment(1.0, 2.0, np.zeros(RATE, np.float32))
    r._deliver(seg)
    assert got == [seg] and r.segments is None and not r.pending() and r.drain(0.1)


# -- order, bounds ----------------------------------------------------------------------------------------------------

def test_each_channels_transmissions_are_handled_in_order_and_one_channel_never_waits_for_another():
    rng = np.random.default_rng(3)
    done, gate = [], threading.Event()

    def handler(ch, seg):
        if ch.label == "A" and seg.start == 0:
            gate.wait(30)                       # A's first takes long
        time.sleep(float(rng.uniform(0, 0.004)))
        done.append((ch.label, seg.start))
    w = SegmentWorkers("dongle", handler, lambda m: None)
    a, b = Channel(126.4, "A"), Channel(120.8, "B")     # one airband dongle, two channels (radio/am.py)
    for k in range(30):
        w.put(a, Segment(float(k), k + 0.5, np.zeros(10, np.float32)))
        w.put(b, Segment(float(k), k + 0.5, np.zeros(10, np.float32)))
    assert wait(lambda: len([x for x in done if x[0] == "B"]) == 30)    # B all handled while A's first waits
    assert not [x for x in done if x[0] == "A"] and w.pending("A") == 30 and w.pending() == 30
    gate.set()
    assert w.drain(30)
    for label in "AB":
        assert [t for lab, t in done if lab == label] == [float(k) for k in range(30)]


def test_falling_behind_is_logged_and_a_full_queue_waits_never_drops(monkeypatch):
    monkeypatch.setattr(fm, "SEG_BEHIND", 2)
    monkeypatch.setattr(fm, "SEG_QUEUE_MAX", 3)
    events, done, gate = [], [], threading.Event()

    def handler(ch, seg):
        gate.wait(30)
        done.append(seg.start)
    w = SegmentWorkers("FG1", handler, events.append)
    ch = Channel(453.0375, "FG1")
    for k in range(3):
        w.put(ch, Segment(float(k), k + 0.5, np.zeros(10, np.float32)))
    assert any("falling behind" in e for e in events)
    fourth = threading.Thread(target=w.put, args=(ch, Segment(3.0, 3.5, np.zeros(10, np.float32))), daemon=True)
    fourth.start()
    fourth.join(0.3)
    assert fourth.is_alive() and any("the receiver waits" in e for e in events)    # the reader waits: nothing dropped
    gate.set()
    fourth.join(10)
    assert w.drain(10) and done == [0.0, 1.0, 2.0, 3.0] and any("caught up" in e for e in events)


def test_a_handler_error_is_logged_and_the_next_transmission_still_handled():
    events, done = [], []

    def handler(ch, seg):
        if seg.start == 0:
            raise ValueError("boom")
        done.append(seg.start)
    w = SegmentWorkers("FG1", handler, events.append)
    for k in range(3):
        w.put(Channel(453.0375, "FG1"), Segment(float(k), k + 0.5, np.zeros(10, np.float32)))
    assert w.drain(10) and done == [1.0, 2.0] and events == ["segment handler error: ValueError: boom"]


# -- what stays immediate ---------------------------------------------------------------------------------------------

def test_an_emergency_while_a_long_transmission_is_being_handled_alarms_at_once():
    got = []
    on_segment, held, release = held_first(got)
    r, tap, _fed, _events, alarms = receiver("FG1", "traffic", on_segment, mdc=both_ids, **TRAFFIC)
    b_tx = np.concatenate([quiet(0.15), mdc(B_ID, EMERGENCY), quiet(0.15), voice_like(1.5) + quiet(1.5), quiet(0.1)])
    audio = np.concatenate([idle(3.0), unit(4.0), idle(1.5, 1), b_tx, idle(2.0, 2)])
    t = pump(r, audio)
    try:
        assert held.wait(30)
        # the key-up after it: its button alarmed by the reader, the first transmission still in its handler
        assert wait(lambda: alarms) and len(got) == 1 and not release.is_set()
        assert alarms == [(alarms[0][0], [(f"{B_ID:04X}", True)])]
    finally:
        release.set()
    t.join(30)
    assert r.segments.drain(30)
    a, b = got
    assert tap.alarm_key("FG1", a.start, a.end) == "" and tap.alarm_key("FG1", b.start, b.end) == "alarm-1"
    assert [h for h, _e in tap.ids("FG1", b.start, b.end)] == [f"{B_ID:04X}"] and len(alarms) == 1


def test_a_page_shows_live_while_the_transmission_before_it_is_being_handled():
    got = []
    on_segment, held, release = held_first(got)
    clock = {"t": 1000.0}
    paging = LivePaging(PLAN, clock=lambda: clock["t"])
    r, _tap, fed, _events, _alarms = receiver("South 2", "paging", on_segment, paging=paging, clock=clock, **PAGING)
    audio = np.concatenate([hiss(4.0), keyed(voice_like(20.0)), hiss(8.0), south2_page(), hiss(8.0)])
    t = pump(r, audio)
    try:
        assert held.wait(30)
        # the tones go out live, pair by pair, while the voice-only transmission before is still in its handler
        assert wait(lambda: any(p["names"] == ["Port Chester FD"] for e in paging.state() for p in e["pairs"]))
        assert len(got) == 1 and not release.is_set()
    finally:
        release.set()
    t.join(30)
    assert r.segments.drain(30) and len(got) == 2


# -- a stop flushes ---------------------------------------------------------------------------------------------------

def test_a_stop_hands_on_what_the_squelch_held_and_everything_waiting(tmp_path, monkeypatch):
    got = []

    def on_segment(ch, seg):
        time.sleep(0.3)                     # slow: several wait for the worker when it stops
        got.append(seg)
    # four key-ups, the audio ending inside the last one (the squelch still open when it stops)
    audio = np.concatenate([idle(3.0)] + [x for k in range(3) for x in (unit(1.0), idle(1.5, k + 1))] + [unit(3.0)])
    r = NfmReceiver(fake_rtl_fm(tmp_path, audio[:-RATE]), "", None, Channel(453.0375, "FG1"), on_segment,
                    lambda m: None, **TRAFFIC)
    r.start()
    try:
        assert wait(lambda: r.squelch.open and r.segments.pending() + len(got) == 3)
        left = r.segments.pending()
    finally:
        r.stop()
    assert left >= 1 and r.drain(30)
    assert len(got) == 4 and got[-1].reason == "stopped" and [s.start for s in got] == sorted(s.start for s in got)


def test_the_app_lets_every_receiver_finish_before_it_finalises_the_held_pages(tmp_path):
    from sizeup.app import App
    from tests.appcfg import cfg
    app = App(cfg(tmp_path))
    said = []
    app.receivers = [SimpleNamespace(channel=Channel(470.2, "South 2"), stop=lambda: said.append("stop"),
                                     drain=lambda timeout: said.append(("drain", timeout > 9)) or True)]
    app.pagehold.flush = lambda: said.append("pages") or []
    app.stop()
    assert said == ["stop", ("drain", True), "pages"]


# -- airband: the AGC and the tone notch on the worker ----------------------------------------------------------------

def am_audio(seed=0):
    """An AM envelope: floor, then a carrier with a voice and a heterodyne tone on it, then floor again."""
    g = np.random.default_rng(seed)
    t = np.arange(int(RATE * 4)) / RATE
    floor = lambda s: 0.01 + 0.002 * g.standard_normal(int(RATE * s))     # noqa: E731
    up = 0.2 * (1 + 0.5 * voice_like(4.0) + 0.05 * np.sin(2 * np.pi * 1020.0 * t)) + 0.002 * g.standard_normal(len(t))
    return np.concatenate([floor(4.0), up, floor(2.0)])


def am_segments(sq, audio):
    out = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        if (s := sq.feed(audio[i:i + FRAME])) is not None:
            out.append(s)
    if (s := sq.flush()) is not None:
        out.append(s)
    return out


def test_a_deferred_airband_transmission_is_the_same_once_its_worker_has_finished_it():
    clock = {"t": 0.0}

    def tick():
        clock["t"] += FRAME / RATE
        return clock["t"]
    audio = am_audio()
    now = CarrierSquelch(open_db=3.5, clock=tick)
    (a,) = am_segments(now, audio)
    clock["t"] = 0.0
    later = CarrierSquelch(open_db=3.5, clock=tick)
    later.defer = True
    (b,) = am_segments(later, audio)
    assert b.pending is not None and len(b.audio) == 0 and not hasattr(b, "tones")     # nothing of it done yet
    assert (b.start, b.end, b.quieting_db, b.snr_db, b.carrier_dbfs) == (a.start, a.end, a.quieting_db, a.snr_db,
                                                                         a.carrier_dbfs)
    fm.finish(b)
    assert b.pending is None and np.array_equal(a.audio, b.audio) and a.tones == b.tones and len(a.audio) > RATE


def test_a_started_airband_receiver_defers_its_squelches_work_and_one_never_started_does_not():
    r = AmReceiver("rtl_fm", "", 36.4, Channel(126.4, "NY App"), lambda c, s: None, open_db=3.5)
    assert r.squelch.defer is False and r.new_squelch().defer is False
    r.segments = SegmentWorkers("NY App", r.on_segment, r.on_event)          # (as start() does)
    assert r.new_squelch().defer is True and r._carrier_squelch().defer is True     # (each other frequency's too)


# -- what reads the transmission later --------------------------------------------------------------------------------

def test_a_transmission_handled_after_the_next_carrier_opened_leaves_that_one_live():
    live = LiveTalk(resolve=lambda *a: None, tg_name=lambda tg: "", main=lambda tg: False, clock=lambda: 2000.0)
    live.fm_open("FG1", 1000.0)                 # A
    live.fm_ids("FG1", ["2468"])
    live.fm_open("FG1", 1003.0)                 # B keys up before A's transmission is handled
    live.fm_segment("FG1", ["2468"], 1000.0, 1002.5)
    assert live._fm["FG1"]["since"] == 1003.0                   # B is still talking
    assert not [e for e in live._recent if e["radio_id"] == "2468"]    # A's ID was shown live: not flashed again
    live.fm_segment("FG1", ["1357"], 1003.0, 1005.0)
    assert "FG1" not in live._fm and [e["radio_id"] for e in live._recent] == ["1357"]


def test_a_page_isnt_finalised_while_its_continuation_waits_for_the_worker(tmp_path):
    from sizeup.app import App
    from tests.appcfg import cfg
    from tests.test_app import tone, voice
    app = App(cfg(tmp_path))
    app._kind = {"South 2": "paging"}
    sent, waiting = [], [False]
    app.events = lambda kind, data, clip: sent.append(kind)
    app.receivers = [SimpleNamespace(channel=Channel(470.2, "South 2"),
                                     squelch=SimpleNamespace(open=False, continuing=False),
                                     pending=lambda label=None: waiting[0])]
    head = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(4.0)]).astype(np.float32)
    ch, now = Channel(470.2, "South 2"), time.time()
    first = Segment(now, now + len(head) / RATE, head, 25.0, reason="quiet", hold_until=now + len(head) / RATE + 4.5)
    app._segment(ch, first)
    waiting[0] = True                       # its continuation closed, and is still waiting for the worker
    app._release_pages(now=first.hold_until + 1.0)
    assert "page" not in sent
    waiting[0] = False
    app._release_pages(now=first.hold_until + 1.0)
    assert sent.count("page") == 1


def test_a_station_switches_threads_every_millisecond(monkeypatch):
    from sizeup import app as app_mod
    got = []
    monkeypatch.setattr(app_mod.sys, "setswitchinterval", got.append)
    monkeypatch.setattr(app_mod, "make_app", lambda cfg, fence=None: SimpleNamespace(start=lambda: "started"))
    for role in ("station", ""):
        got.clear()
        assert app_mod.start_app(SimpleNamespace(cloud_role=role)) == "started"
        assert got == [app_mod.STATION_SWITCH_S] == [0.001]
