"""Noise openings and the per-channel squelch (sizeup/radio/fm.py, radio/tap.py, sizeup/channels.py, Settings > Radios >
Squelch): a squelch that opens on one noise frame is never "TX" on the live indicator (the incident card's TX line)
and is counted as a short open; a real key-up still shows within a key-up's time; open / close / min are set per
channel, audited, sent from the hub with the channel choices and applied live on the running receiver; a chattering
squelch is a health hint. Synthetic audio only."""

import numpy as np
import pytest

from sizeup import channels
from sizeup.app import App
from sizeup.cad.store import CadError
from sizeup.radio.fm import FRAME, MIN_OPEN_S, RATE, Channel, NfmReceiver, NoiseSquelch
from sizeup.radio.tap import LiveTap
from tests.test_cloud import HubRun, base, hub_cfg, station, station_cfg
from tests.test_livetap import Rig, frames, hiss, keyed, quiet, voice
from tests.test_app import wait

rng = np.random.default_rng(11)


def blips(n, every_s=1.5, frames_long=1):
    """Hiss with n noise openings: a frame (or a few) where the hiss dips, as an impulse or a birdie would make it."""
    parts = [hiss(3.0)]
    for _ in range(n):
        parts += [0.01 * rng.standard_normal(FRAME * frames_long), hiss(every_s)]
    return np.concatenate(parts)


def test_a_noise_opening_is_never_tx_and_counts_as_short():
    r = Rig()
    lit, opened = [], []
    x = blips(6)
    for f in frames(x):
        r.feed(f)
        opened.append(r.sq.open)
        lit.append(bool(r.live.state()))
    assert any(opened)                          # the squelch did open on them (the old indicator lit each time)
    assert not any(lit) and not r.segs          # never TX, nothing kept
    assert len(r.sq.shorts) == 6
    assert r.sq.short_per_min(r.clock.t) == pytest.approx(6 * 60 / 300)


def test_a_real_key_up_still_shows_at_once_from_when_it_opened():
    r = Rig()
    x = np.concatenate([hiss(3.0), keyed(voice(2.0)), hiss(1.0)])
    t = r.feed(x, until=lambda r: bool(r.live.state()))
    assert t is not None and r.sq.open
    [e] = r.live.state()
    assert e["since"] == pytest.approx(r.sq._start) and e["id_on_release"]
    assert t - r.sq._start <= MIN_OPEN_S + 2 * FRAME / RATE     # shown ~0.3 s in, dated from the key-up
    r.feed(x[int((t - 1000.0) * RATE):])
    assert len(r.segs) == 1 and not r.sq.shorts


def test_min_open_s_drops_a_short_carrier_and_keeps_it_off_the_indicator():
    x = np.concatenate([hiss(3.0), keyed(quiet(0.5)), hiss(1.0)])
    r = Rig()
    r.feed(x)
    assert len(r.segs) == 1                     # 0.5 s of carrier: kept at the default 0.3 s
    r = Rig()
    r.sq.min_frames = round(0.8 * RATE / FRAME)
    lit = []
    for f in frames(x):
        r.feed(f)
        lit.append(bool(r.live.state()))
    assert not r.segs and not any(lit) and len(r.sq.shorts) == 1


def test_the_receiver_takes_new_settings_live_through_the_tap():
    r = NfmReceiver("rtl_fm", "", None, Channel(453.0375, "FG1"), lambda ch, seg: None)    # never started
    LiveTap(lambda _l: "traffic").attach(r)
    for f in frames(blips(2)):
        r.squelch.feed(f)
    st = r.status()
    assert (st["open_db"], st["close_db"], st["min_open_s"]) == (10.0, 6.0, 0.3) and st["short_per_min"] == 0.4
    assert r.set_squelch(14.0, 8.0, 0.5) and not r.set_squelch(14.0, 8.0, 0.5)
    st = r.status()                             # the running squelch (under the tap), not just the next one
    assert (st["open_db"], st["close_db"], st["min_open_s"]) == (14.0, 8.0, 0.5)
    assert r.squelch._sq.open_db == 14.0
    fresh = r.new_squelch()                     # an rtl_fm restart: the same settings, the count carried over
    assert (fresh.open_db, fresh.close_db, fresh.min_frames, len(fresh.shorts)) == (14.0, 8.0, 10, 2)


def test_paging_reception_unchanged_at_the_defaults():
    """South 2's squelch at the defaults keeps a page whole: tones, a fade inside, voice (#61's hang and window)."""
    from tests.test_livetap import tone
    clock = [1000.0]
    sq = NoiseSquelch(clock=lambda: clock[0], hang_s=1.5, continue_s=3.0)
    x = np.concatenate([hiss(3.0), keyed(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), quiet(0.3), voice(2.0)])),
                        hiss(3.0)])
    segs = []
    for f in frames(x):
        clock[0] += FRAME / RATE
        if (s := sq.feed(f)) is not None:
            segs.append(s)
    assert len(segs) == 1 and segs[0].end - segs[0].start > 6.0 and not sq.shorts


# -- the setting --------------------------------------------------------------------------------------------------


def test_clean_squelch_and_the_signature():
    assert channels.clean_squelch("FG1", {"open_db": 12.26, "min_open_s": 0.52}) == {"open_db": 12.5, "min_open_s": 0.5}
    for bad in ({"open_db": 2}, {"close_db": "6"}, {"min_open_s": 5}, {"open_db": 8, "close_db": 9}, {"open_db": True}):
        with pytest.raises(ValueError):
            channels.clean_squelch("FG1", bad)
    with pytest.raises(ValueError):
        channels.clean_squelch("tg:307", {"open_db": 12})
    ov = {"FG1": {"engine": "local"}}
    assert channels.sig(ov, True) == channels.sig(ov, True, None, None, {}) == channels.sig(ov, True, None, None, None)
    assert channels.sig(ov, True, None, None, {"FG1": {"open_db": 12.0}}) != channels.sig(ov, True)


def test_settings_set_audit_apply_live_and_reset(tmp_path):
    app = App(base(clips_dir=str(tmp_path / "clips")))
    fg1 = app._receiver("FG1")
    assert (fg1.open_db, fg1.close_db, fg1.squelch.min_frames) == (10.0, 6.0, 6)
    app.cad.settings.post("squelch", {"action": "set", "key": "FG1", "open_db": 13, "min_open_s": 0.5, "operator": "admin@example.org"})
    assert (fg1.open_db, fg1.squelch.open_db, fg1.squelch.min_frames) == (13.0, 13.0, 10)     # live, no restart
    assert app._receiver("South 2").open_db == 10.0                                           # per channel
    a = app.cad.settings.audit_log("radios")[0]
    assert a["key"] == "squelch/FG1" and a["operator"] == "admin@example.org"
    page = app.cad.settings.squelch_page()
    row = next(c for c in page["channels"] if c["key"] == "FG1")
    assert row["set"] == {"open_db": 13.0, "min_open_s": 0.5} and row["running"]["open_db"] == 13.0
    assert row["default"] == {"open_db": 10.0, "close_db": 6.0, "min_open_s": MIN_OPEN_S}
    assert "short_per_min" in row and "hiss_db" in row
    with pytest.raises(CadError):                       # close at or over open (this one's 13)
        app.cad.settings.post("squelch", {"action": "set", "key": "FG1", "close_db": 13, "operator": "admin@example.org"})
    app.cad.settings.post("squelch", {"action": "set", "key": "FG1", "open_db": None, "operator": "admin@example.org"})
    assert fg1.open_db == 10.0 and app.cad.settings.squelch_overrides() == {"FG1": {"min_open_s": 0.5}}
    app.cad.settings.post("squelch", {"action": "reset", "key": "FG1", "operator": "admin@example.org"})
    assert app.cad.settings.squelch_overrides() == {} and fg1.squelch.min_frames == 6
    assert fg1.squelch.open_db == 10.0


def test_a_noise_opening_is_not_talking_on_the_watcher(tmp_path):
    """The app's 0.2 s watcher (App._watch_fm_once) also waits for keyed: an open squelch isn't "talking" yet."""
    app = App(base(clips_dir=str(tmp_path / "clips")))
    fg1 = app._receiver("FG1")
    app.tap.live = None                         # only the watcher here
    for f in frames(np.concatenate([hiss(3.0), 0.01 * rng.standard_normal(FRAME)])):
        fg1.squelch.feed(f)
    assert fg1.squelch.open and not fg1.squelch.keyed
    app._watch_fm_once()
    assert app.live.state() == []
    for f in frames(keyed(quiet(0.4))):
        fg1.squelch.feed(f)
    assert fg1.squelch.keyed
    app._watch_fm_once()
    assert [e["channel"] for e in app.live.state()] == ["FG1"]


def test_the_hub_sends_the_squelch_and_the_station_applies_it_live(tmp_path):
    from tests.test_channels import hub_post
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        code, _ = hub_post(hub, "squelch", {"action": "set", "key": "FG1", "open_db": 15, "close_db": 9})
        assert code == 200
        assert wait(lambda: st._receiver("FG1").squelch.open_db == 15.0, 10)
        assert st._receiver("FG1").close_db == 9.0 and st.cad.settings.audit_log("radios")[0]["operator"] == "hub"
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        page = hub.get("/api/cad/squelch")
        row = next(c for c in page["channels"] if c["key"] == "FG1")
        assert row["set"] == {"open_db": 15.0, "close_db": 9.0}
        assert wait(lambda: next(c for c in hub.get("/api/cad/squelch")["channels"] if c["key"] == "FG1")
                    ["running"]["open_db"] == 15.0, 15)
        assert row["default"]["open_db"] == 10.0          # the station's, as it reported them
        assert hub_post(hub, "squelch", {"action": "reset", "key": "FG1"})[0] == 200
        assert wait(lambda: st._receiver("FG1").squelch.open_db == 10.0, 10)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_an_older_station_gets_no_squelch_argument(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app = hub.app
        app.hub.status = {"channels": {"sig": "x"}}         # a station before this: no squelch_ok
        app._sq_ov = {"FG1": {"open_db": 14.0}}
        want = app.channel_desired()
        assert want["squelch"] is None and want["sig"] == channels.sig(want["prefs"], want["aws"]["ok"], want["tones"],
                                                                          want["aws"]["reserve"])
        app.hub.status = {"channels": {"sig": want["sig"]}}
        assert "older sizeup" in app.channels_sync()["detail"]
        app.hub.status = {"channels": {"sig": "x", "squelch_ok": True}}
        assert app.channel_desired()["squelch"] == {"FG1": {"open_db": 14.0}}
    finally:
        hub.close()



# -- telling real key-ups from interference ------------------------------------------------------------------------


def test_the_ctcss_tone_of_a_key_up_with_or_without_speech():
    from sizeup.radio.fm import ctcss_tone
    t = np.arange(int(2.5 * RATE)) / RATE
    dead = 0.02 * rng.standard_normal(len(t))
    pl100 = 0.05 * np.sin(2 * np.pi * 100.0 * t)
    assert ctcss_tone(dead + pl100) == 100.0                # a bare key-up from one of ours
    assert ctcss_tone(dead) is None                         # a carrier with no tone: not ours, or not a radio
    assert ctcss_tone(dead + 0.05 * np.sin(2 * np.pi * 156.7 * t)) == 156.7
    assert ctcss_tone(voice(2.5) + dead + pl100) == 100.0  # speech's own low harmonics don't win
    assert ctcss_tone(voice(2.5) + dead) is None
    assert ctcss_tone(hiss(2.5)) is None and ctcss_tone(np.zeros(RATE)) is None and ctcss_tone(dead[:RATE // 4]) is None


def test_key_ups_by_tone_reach_the_squelch_page(tmp_path):
    from sizeup.radio.fm import Segment
    app = App(base(clips_dir=str(tmp_path / "clips")))
    t = np.arange(int(2.0 * RATE)) / RATE
    dead = (0.02 * rng.standard_normal(len(t))).astype(np.float32)
    now = 2_000_000_000.0
    tone = (dead + 0.05 * np.sin(2 * np.pi * 100.0 * t)).astype(np.float32)
    assert app._keyup("FG1", Segment(now - 100, now - 98, tone, 25.0), False, False) == 100.0
    assert app._keyup("FG1", Segment(now - 50, now - 48, dead, 14.0), False, False) is None
    assert app._keyup("FG1", Segment(now - 20, now - 17, tone, 30.0), True, True) == 100.0
    app._keyup("FG1", Segment(now - 7200, now - 7198, dead, 14.0), False, False)            # over an hour ago
    k = app.keyups("FG1", now)
    assert (k["n"], k["no_speech"], k["tones"]) == (3, 2, {"100": 2, "none": 1})
    assert k["bare_s"] == [2.0, 2.0] and k["bare_q"] == [14.0, 25.0] and len(k["recent"]) == 3
    row = next(r for r in app.radio_status() if r["label"] == "FG1")
    assert "keyups" in row and "keyups" not in next(r for r in app.radio_status() if r["label"] == "South 2")
