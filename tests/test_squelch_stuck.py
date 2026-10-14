"""GitHub #116: South 2 showed "● RX" all the time. What can hold the noise squelch (radio/fm.py) open with no
carrier, with synthetic audio: steady noise over the close threshold (a hiss reference learned high, a weak
carrier or spur on the station PC's antenna, noise after a page), continuations chaining on it, the audio
stopping, and the warning when a channel has been open too long. Normal pages must come through as before."""

import stat
import threading
import time

import numpy as np
import pytest

from sizeup.radio import fm
from sizeup.radio.fm import CHAIN_S, FRAME, RATE, STEADY_FRAMES, STUCK_S, Channel, NfmReceiver, NoiseSquelch
from tests.test_page import PAGING, PLAN, carrier, held_pages, hiss, keyed, south2_page, voice_like
from tests.test_sdr_auto_assign import DONGLES, rig  # noqa: F401  (fixture)

S = RATE // FRAME          # frames a second


def trace(audio, sq=None, **kw):
    """(segments, per-frame open flags, the squelch), fed 50 ms at a time on a clock that starts at 0."""
    clock = {"t": 0.0}
    sq = sq or NoiseSquelch(clock=lambda: clock["t"], **kw)
    segs, lit = [], []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        if (s := sq.feed(audio[i:i + FRAME])) is not None:
            segs.append(s)
        lit.append(sq.open)
    return segs, np.array(lit), sq


def longest_open_s(lit):
    best = run = 0
    for x in lit:
        run = run + 1 if x else 0
        best = max(best, run)
    return best / S


# -- steady noise over close_db -------------------------------------------------------------------------------

def test_noise_just_over_close_db_after_a_page_does_not_hold_rx():
    # A page, then the channel sits at 7 dB of "quieting" (between close 6 and open 10) for 4 minutes: the
    # page's long hang never ran out, and every 2 minutes the squelch split and went on (RX for good).
    audio = np.concatenate([hiss(4.0), south2_page(), carrier(RATE * 240, 7.0), hiss(10.0)])
    segs, lit, sq = trace(audio, **PAGING)
    page_end = 4.0 + len(south2_page()) / RATE
    # The page is kept whole, as one page; nothing waits on it (no continuation window after "steady").
    assert len(segs) == 1 and segs[0].reason == "steady" and segs[0].hold_until == 0
    assert segs[0].end - segs[0].start == pytest.approx(page_end - 4.0, abs=0.6)
    (page,) = held_pages(segs)
    assert page.kind == "page" and page.departments == ["Port Chester FD"]
    # RX goes out ~30 s after the voice stopped and stays out for the rest of the noise (no chain of
    # continuations, no reopening): the hiss reference was relearned from the noise.
    off = int((page_end + STEADY_FRAMES / S + 1.0) * S)
    assert lit[:off].any() and not lit[off:].any()
    assert sq.relearned == 1
    assert sq.hiss_db == pytest.approx(10 * np.log10(sq.levels[0]), abs=3)


@pytest.mark.parametrize("q", [7.0, 12.0, 20.0])
def test_a_steady_carrier_with_nothing_on_it_is_not_rx_for_minutes(q):
    # The hiss reference learned, then the floor drops by q dB and stays there (a weak unmodulated carrier or a
    # PC spur on the antenna; a reference learned high). Over open_db it opens; with nothing on it, it closes
    # 30 s later, no transmission is made of it, and it doesn't open again on the same level.
    audio = np.concatenate([hiss(4.0), carrier(RATE * 300, q)])
    segs, lit, sq = trace(audio, **PAGING)
    assert segs == []
    assert longest_open_s(lit) <= STEADY_FRAMES / S + 1
    assert not lit[int((4.0 + STEADY_FRAMES / S + 1) * S):].any()
    assert sq.relearned == (1 if q >= 10 else 0)
    if q >= 10:   # the floor adapted: the carrier's level is the idle reference now
        assert abs(sq.quieting_db) < 3


def test_a_real_page_still_opens_over_the_steady_level_and_after_it_goes():
    spur = 12.0
    page = south2_page()
    audio = np.concatenate([hiss(4.0), carrier(RATE * 60, spur),
                            page,                                    # a page while the spur is there (it captures)
                            carrier(RATE * 5, spur), hiss(20.0),     # the spur goes: the hiss is back
                            page, hiss(6.0)])                         # and a page on a clean channel
    segs, lit, sq = trace(audio, **PAGING)
    pages = held_pages(segs)
    assert [p.kind for p in pages] == ["page", "page"]
    assert all(p.departments == ["Port Chester FD"] and len(p.voice) / RATE == pytest.approx(6.0, abs=0.6) for p in pages)
    assert not lit[-int(4 * S):].any()


# -- continuations can't chain ----------------------------------------------------------------------------------

def test_continuations_do_not_chain_on_noise_blips_at_the_close_threshold():
    # After a page the channel bubbles: 0.5 s at 7 dB (over close_db), 2 s of hiss, for 3 minutes. Every
    # blip is inside the 3 s window, and a window used to open again after each: RX / RX hold for good.
    blips = np.concatenate([np.concatenate([carrier(RATE // 2, 7.0), hiss(2.0)]) for _ in range(72)])
    audio = np.concatenate([hiss(4.0), south2_page(), hiss(2.0), blips, hiss(4.0)])
    segs, lit, sq = trace(audio, **PAGING)
    page_end = 4.0 + len(south2_page()) / RATE
    # The page, then at most the first blip (opened in the page's window, nothing of its own: no new window).
    assert len(segs) <= 2 and segs[0].reason == "quiet"
    assert all(s.hold_until == 0 for s in segs[1:])
    assert not lit[int((page_end + 8.0) * S):].any()
    assert not sq.holding


def test_a_fading_page_continues_but_not_past_two_minutes():
    # A page that keeps fading and coming back weak (8 dB, with speech): joined while the page and its
    # continuations span under 2 minutes; after that the window is not opened again.
    weak = voice_like(10.0) + carrier(RATE * 10, 8.0)
    audio = np.concatenate([hiss(4.0), south2_page()] + [np.concatenate([hiss(2.5), weak]) for _ in range(14)] + [hiss(6.0)])
    segs, lit, sq = trace(audio, **PAGING)
    assert segs[0].reason == "quiet" and all(s.cont for s in segs[1:])
    assert all(s.start - segs[0].start < CHAIN_S + 15 for s in segs)
    assert segs[-1].hold_until == 0 and not lit[-int(4 * S):].any()
    assert len(held_pages(segs)) == 1 or held_pages(segs)[0].end - held_pages(segs)[0].start <= CHAIN_S


def test_normal_pages_are_unchanged():
    # One page, a fade inside the hang, a weak continuation inside the window: as #61 made them.
    tail = voice_like(5.0) + carrier(RATE * 5, 8.0)
    segs, lit, sq = trace(np.concatenate([hiss(4.0), south2_page(), hiss(2.5), tail, hiss(6.0)]), **PAGING)
    assert [s.reason for s in segs] == ["quiet", "quiet"] and segs[1].cont and segs[0].hold_until > 0
    assert segs[1].hold_until > 0                     # the continuation had speech of its own: it may continue
    (page,) = held_pages(segs)
    assert page.kind == "page" and len(page.parts) == 2 and sq.relearned == 0
    # A long page (60 s of speech) is one transmission, not cut by the steady rule.
    segs, lit, sq = trace(np.concatenate([hiss(4.0), south2_page(60.0), hiss(6.0)]), **PAGING)
    assert len(segs) == 1 and segs[0].end - segs[0].start > 63 and sq.relearned == 0


# -- the audio stopping -----------------------------------------------------------------------------------------

def fake_rtl_fm(tmp_path, audio):
    """An "rtl_fm" that writes this audio and then hangs (a stalled dongle): no device is opened."""
    raw = tmp_path / "audio.raw"
    raw.write_bytes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    exe = tmp_path / "rtl_fm"
    exe.write_text(f'#!/bin/sh\ncat "{raw}"\nexec sleep 30\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_rx_and_the_live_page_end_when_the_audio_stalls_mid_page(tmp_path, monkeypatch):
    from sizeup.radio.livepage import LivePaging
    from sizeup.radio.tap import LiveTap
    monkeypatch.setattr(fm, "STALL_S", 0.5)
    got, events, seen_open = [], [], threading.Event()
    exe = fake_rtl_fm(tmp_path, np.concatenate([hiss(4.0), south2_page()[: RATE * 8]]))   # cut mid-voice
    r = NfmReceiver(exe, "", None, Channel(470.2, "South 2"), lambda c, s: got.append(s), events.append, **PAGING)
    paging = LivePaging(PLAN)
    LiveTap(lambda label: "paging", paging).attach(r)
    orig = r._watch_open

    def watch():
        if r.squelch.open:
            seen_open.set()
        orig()
    r._watch_open = watch
    r.start()
    try:
        deadline = time.time() + 10
        while not got and time.time() < deadline:
            time.sleep(0.05)
        assert seen_open.is_set() and got and got[0].reason == "stall"
        st = r.status()
        assert not st["open"] and st["open_s"] is None and not st["stuck"] and "word" not in st
        assert r.squelch.busy_since is None and not r.squelch.holding
        cur = paging._ch.get("South 2")
        assert cur is not None and cur.closed_at is not None        # the live page was told the carrier went
    finally:
        r.stop()


# -- the warning ------------------------------------------------------------------------------------------------

def test_open_over_two_minutes_shows_stuck_and_is_logged_once():
    events = []
    r = NfmReceiver("rtl_fm", "", None, Channel(470.2, "South 2"), lambda c, s: None, events.append, **PAGING)
    r.state = "ok"
    clock = {"t": 0.0}
    r.clock = lambda: clock["t"]
    r.squelch = NoiseSquelch(clock=lambda: clock["t"], **PAGING)
    # One keyed carrier with someone talking for 150 s: split at 2 minutes and reopened at once, so RX is lit
    # without a break (what a squelch held by something with content on it would look like).
    audio = np.concatenate([hiss(4.0), keyed(voice_like(150.0)), hiss(4.0)])
    seen = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        r.squelch.feed(audio[i:i + FRAME])
        r._watch_open()
        if i % (RATE // 2) == 0:
            seen.append(r.status())
    early = [s for s in seen if s["open"] and s["open_s"] < STUCK_S]
    late = [s for s in seen if s["open"] and s["open_s"] >= STUCK_S]
    assert early and not any(s["stuck"] for s in early)
    assert late and all(s["stuck"] for s in late) and max(s["open_s"] for s in late) > 140
    s = late[-1]
    assert s["open_db"] == 10.0 and s["close_db"] == 6.0 and s["hiss_db"] is not None
    assert max(x["quieting_db"] for x in late) > 20
    stuck = [e for e in events if "(stuck?)" in e]
    assert len(stuck) == 1 and stuck[0].startswith("South 2: squelch open 2:00 continuously (stuck?): quieting ")
    assert "opens at 10, closes under 6" in stuck[0] and "idle hiss" in stuck[0]
    closed = [e for e in events if "closed after" in e]
    assert len(closed) == 1 and closed[0].startswith("South 2: squelch closed after 2:3")
    assert not r.status()["open"] and not r.status()["stuck"]


def test_the_radios_state_carries_the_open_time_and_an_off_receiver_is_never_stuck(rig):
    # The station's radios state (/api/state "radios", sent on to the hub): open_s / stuck for station health.
    plugged, logs, make = rig
    plugged[:] = DONGLES[:1]                       # one dongle: FG1 runs on it, South 2 is Off
    app = make()
    s2 = app._receiver("South 2")
    s2.state, s2.squelch.open, s2.squelch.open_since = "ok", True, time.time() - 300   # as if stuck
    rows = {r["label"]: r for r in app.radio_status()}
    assert rows["South 2"]["state"] == "off" and rows["South 2"]["open"] is False
    assert rows["South 2"]["open_s"] is None and rows["South 2"]["stuck"] is False
    for k in ("open_s", "stuck", "open_db", "close_db", "hiss_db", "floor_db"):
        assert k in rows["FG1"]
    assert s2.status()["stuck"] is True and s2.status()["open_s"] >= 300 - 1
