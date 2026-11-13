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
    assert r.squelch._sq.open_db == 10.0        # taken whole at its next frame, never mid-frame
    r.squelch.feed(frames(hiss(0.05))[0])
    assert (r.squelch._sq.open_db, r.squelch._sq.close_db, r.squelch._sq.min_frames) == (14.0, 8.0, 10)
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
    assert (fg1.open_db, fg1.squelch.params) == (13.0, (13.0, 6.0, 10))                       # live, no restart
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
    assert app.cad.settings.squelch_overrides() == {} and fg1.squelch.params == (10.0, 6.0, 6)


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
        assert wait(lambda: st._receiver("FG1").squelch.params[0] == 15.0, 10)
        assert st._receiver("FG1").close_db == 9.0 and st.cad.settings.audit_log("radios")[0]["operator"] == "hub"
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        page = hub.get("/api/cad/squelch")
        row = next(c for c in page["channels"] if c["key"] == "FG1")
        assert row["set"] == {"open_db": 15.0, "close_db": 9.0}
        assert wait(lambda: next(c for c in hub.get("/api/cad/squelch")["channels"] if c["key"] == "FG1")
                    ["running"]["open_db"] == 15.0, 15)
        assert row["default"]["open_db"] == 10.0          # the station's, as it reported them
        assert hub_post(hub, "squelch", {"action": "reset", "key": "FG1"})[0] == 200
        assert wait(lambda: st._receiver("FG1").squelch.params[0] == 10.0, 10)
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
        app.hub.status = {"channels": {"sig": "x", "squelch_ok": True},
                          "radios": [{"label": "FG1", "channel_kind": "traffic", "min_open_s": 0.3,
                                      "squelch_default": {"open_db": 10.0, "close_db": 6.0, "min_open_s": 0.3}}]}
        assert app.channel_desired()["squelch"] == {"FG1": {"open_db": 14.0}}
    finally:
        hub.close()



# -- telling real key-ups from interference ------------------------------------------------------------------------


def radio_voice(s):
    """Speech as a radio sends it: high-passed at 300 Hz (radios keep their TX audio out of the CTCSS band)."""
    v = voice(s)
    F = np.fft.rfft(v)
    F[np.fft.rfftfreq(len(v), 1 / RATE) < 300] *= 0.03
    return np.fft.irfft(F, len(v))


def talk(f0=130.0, jitter=True, hpf=True, pauses=True, seed=0):
    """Synthetic speech at pitch f0 (steady, or wandering as a voice does), as a radio sends it (high-passed at
    300 Hz) or not; three 0.6 s words with 0.5 s of dead air between them, or 2.5 s of syllables without a pause."""
    r = np.random.default_rng(seed)

    def word(s):
        t = np.arange(int(RATE * s)) / RATE
        pitch = f0 * (1 + (0.15 * np.sin(2 * np.pi * 1.3 * t + r.uniform(0, 6))
                           + 0.06 * np.cumsum(r.standard_normal(len(t))) / np.sqrt(len(t)) * 20 if jitter else 0))
        ph = 2 * np.pi * np.cumsum(pitch) / RATE
        syll = (np.sin(2 * np.pi * 3 * t + r.uniform(0, 6)) > 0.2).astype(float)
        x = 0.3 * syll * sum(np.sin(k * ph) / k for k in range(1, 15))
        if hpf:
            F = np.fft.rfft(x)
            F[np.fft.rfftfreq(len(x), 1 / RATE) < 300] *= 0.03
            x = np.fft.irfft(F, len(x))
        return x
    x = np.concatenate([p for _ in range(3) for p in (word(0.6), np.zeros(int(0.5 * RATE)))]) if pauses else word(2.5)
    return x + 0.02 * r.standard_normal(len(x))


@pytest.mark.parametrize("hz", [67.0, 88.5, 97.4, 100.0, 127.3, 151.4, 156.7])
def test_the_pl_estimate_with_and_without_speech(hz):
    from sizeup.radio.fm import ctcss_estimate
    for dur in (2.5, 1.2):                                                  # a bare key-up
        t = np.arange(int(dur * RATE)) / RATE
        assert ctcss_estimate(0.02 * rng.standard_normal(len(t)) + 0.05 * np.sin(2 * np.pi * hz * t + 1.0)) == ("tone", hz)
    x = talk(seed=int(hz))                                                  # words with dead air between them
    t = np.arange(len(x)) / RATE
    assert ctcss_estimate(x + 0.05 * np.sin(2 * np.pi * hz * t + 1.0)) == ("tone", hz)   # never the tone next to it
    y = talk(pauses=False, seed=int(hz))                                    # no dead air: at most unsure
    assert ctcss_estimate(y + 0.05 * np.sin(2 * np.pi * hz * t[:len(y)] + 1.0))[0] == "unsure"


@pytest.mark.parametrize("f0", [85.0, 100.0, 120.0, 150.0, 175.0, 200.0])
def test_speech_without_a_pl_is_never_a_tone(f0):
    from sizeup.radio.fm import ctcss_estimate
    for jitter in (False, True):
        for hpf in (False, True):
            for pauses in (False, True):
                v = ctcss_estimate(talk(f0, jitter, hpf, pauses, seed=int(f0)))
                assert v[0] != "tone", (f0, jitter, hpf, pauses, v)


def test_the_pl_estimate_says_none_or_unsure_rather_than_guess():
    from sizeup.radio.fm import ctcss_estimate, pl_text
    t = np.arange(int(2.5 * RATE)) / RATE
    dead = 0.02 * rng.standard_normal(len(t))
    assert ctcss_estimate(dead) == ("none", None)                           # a carrier with no tone
    assert ctcss_estimate(hiss(2.5))[0] == "none"
    assert ctcss_estimate(dead + 0.05 * np.sin(2 * np.pi * 180.0 * t))[0] == "unsure"     # 180 Hz hum: not 179.9
    hum = 0.05 * np.sin(2 * np.pi * 60.0 * t)
    assert ctcss_estimate(dead + hum + 0.05 * np.sin(2 * np.pi * 100.0 * t))[0] == "unsure"   # hum as strong
    assert ctcss_estimate(dead[:RATE // 3] + 0.05 * np.sin(2 * np.pi * 100.0 * t[:RATE // 3])) == ("unsure", None)
    assert ctcss_estimate(np.zeros(RATE)) == ("unsure", None)               # an ID-only key-up (blanked)
    assert ctcss_estimate(radio_voice(2.5) + dead)[0] != "tone"
    assert [pl_text(e) for e in (("tone", 100.0), ("none", None), ("unsure", 97.4))] == [
        "PL est. 100", "PL est. none", "PL est. unsure"]


def test_key_ups_by_tone_reach_the_squelch_page(tmp_path):
    from sizeup.radio.fm import Segment
    app = App(base(clips_dir=str(tmp_path / "clips")))
    t = np.arange(int(2.0 * RATE)) / RATE
    dead = (0.02 * rng.standard_normal(len(t))).astype(np.float32)
    now = 2_000_000_000.0
    tone = (dead + 0.05 * np.sin(2 * np.pi * 100.0 * t)).astype(np.float32)
    assert app._keyup("FG1", Segment(now - 100, now - 98, tone, 25.0), False, False, "FG1-1") == ("tone", 100.0)
    assert app._keyup("FG1", Segment(now - 50, now - 48, dead, 14.0), False, False) == ("none", None)
    assert app._keyup("FG1", Segment(now - 20, now - 17, tone, 30.0), True, True) == ("tone", 100.0)
    app._keyup("FG1", Segment(now - 7200, now - 7198, dead, 14.0), False, False)            # over an hour ago
    k = app.keyups("FG1", now)
    assert (k["n"], k["no_speech"], k["tones"]) == (3, 2, {"100": 2, "none": 1})
    assert app._keyup_pl["FG1-1"] == ("tone", 100.0)                    # for its "no speech" log line
    assert k["bare_s"] == [2.0, 2.0] and k["bare_q"] == [14.0, 25.0] and len(k["recent"]) == 3
    row = next(r for r in app.radio_status() if r["label"] == "FG1")
    assert "keyups" in row and "keyups" not in next(r for r in app.radio_status() if r["label"] == "South 2")


# -- review: paging read-only, import, the station's own values, empty entries -------------------------------------


def test_a_paging_channels_squelch_is_read_only(tmp_path):
    app = App(base(clips_dir=str(tmp_path / "clips")))
    rows = {c["key"]: c for c in app.cad.settings.squelch_page()["channels"]}
    assert (rows["South 2"]["kind"], rows["South 2"]["editable"]) == ("paging", False) and rows["FG1"]["editable"]
    for body in ({"open_db": 30}, {"min_open_s": 2.0}, {"open_db": 11}):
        with pytest.raises(CadError, match="read-only"):
            app.cad.settings.post("squelch", {"action": "set", "key": "South 2", **body, "operator": "admin@example.org"})
    with pytest.raises(CadError):
        app.cad.settings.post("squelch", {"action": "set", "key": "NOPE", "open_db": 12, "operator": "admin@example.org"})
    assert app.cad.settings.squelch_overrides() == {} and app._receiver("South 2").squelch.params == (10.0, 6.0, 6)
    app.set_channel_prefs({}, None, None, {"South 2": {"open_db": 30.0}})   # nor from a hub: left out
    assert app._receiver("South 2").squelch.params == (10.0, 6.0, 6) and app.cad.settings.squelch_overrides() == {}
    data = app.cad.settings.export()                         # nor from an imported file: skipped, said so
    data["overrides"]["squelch"] = {"South 2": {"open_db": 30.0}}
    r = app.cad.settings.post("import", {"action": "apply", "settings": data})
    assert any("read-only" in n for n in r["notes"]) and app.cad.settings.squelch_overrides() == {}


def test_an_import_applies_the_squelch_live_and_drops_empty_entries(tmp_path):
    app = App(base(clips_dir=str(tmp_path / "clips")))
    data = app.cad.settings.export()
    data["overrides"]["squelch"] = {"FG1": {"open_db": 12.0}}
    app.cad.settings.post("import", {"action": "apply", "settings": data, "operator": "admin@example.org"})
    assert app._sq_ov == {"FG1": {"open_db": 12.0}} and app._receiver("FG1").squelch.params[0] == 12.0
    data["overrides"]["squelch"] = {"FG1": {}}
    app.cad.settings.post("import", {"action": "apply", "settings": data, "operator": "admin@example.org"})
    assert app._sq_ov == {} and app._receiver("FG1").squelch.params[0] == 10.0
    assert channels.norm_squelch({"FG1": {}, "South 2": {"open_db": 99}, "tg:307": {"open_db": 12}}) == {}


def test_an_import_on_the_hub_reaches_the_station(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: any(c["key"] == "FG1" for c in hub.app.cad.settings.squelch_page()["channels"]), 10)
        data = hub.app.cad.settings.export()
        data["overrides"]["squelch"] = {"FG1": {"open_db": 13.0}}
        hub.app.cad.settings.post("import", {"action": "apply", "settings": data, "operator": "admin@example.org"})
        assert hub.app._sq_ov == {"FG1": {"open_db": 13.0}}
        assert wait(lambda: st._receiver("FG1").squelch.params[0] == 13.0, 10)
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_close_is_checked_against_the_stations_own_open(tmp_path):
    from sizeup.config import ChannelCfg
    from tests.test_channels import hub_post
    # the hub's own config says FG1 opens at 15; the station's (the one that counts) at 10
    hub = HubRun(hub_cfg(tmp_path, channels=(ChannelCfg("South 2", 470.2, "paging"),
                                             ChannelCfg("FG1", 453.0375, "traffic", squelch_db=15.0))))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: any(c["key"] == "FG1" for c in hub.get("/api/cad/squelch")["channels"]), 10)
        row = next(c for c in hub.get("/api/cad/squelch")["channels"] if c["key"] == "FG1")
        assert row["default"]["open_db"] == 10.0
        code, body = hub_post(hub, "squelch", {"action": "set", "key": "FG1", "close_db": 12})
        assert code >= 400 and "under open (10" in str(body)
        code, _ = hub_post(hub, "squelch", {"action": "set", "key": "South 2", "open_db": 30})
        assert code >= 400                                            # paging: refused on the hub too
        st.set_channel_prefs({}, None, None, {"FG1": {"close_db": 12.0}})   # the station checks its own: left out
        assert st._sq_ov == {} and st._receiver("FG1").squelch.params == (10.0, 6.0, 6)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_station_local_squelch_edits_are_refused(tmp_path):
    """A station forwarding to a hub: the squelch is the hub's (channels_locked), and the station page has no route."""
    from sizeup.web.server import STATION_CAD
    assert not any("squelch" in p for _m, p in STATION_CAD)
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        with pytest.raises(CadError, match="hub"):
            st.cad.settings.post("squelch", {"action": "set", "key": "FG1", "open_db": 12})
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_a_stale_squelch_entry_never_blocks_the_channel_push(tmp_path):
    """A hub override for a channel the station no longer has as traffic (renamed, gone, now paging): the station
    leaves it out and logs it, the rest of the push applies; the hub leaves it out too (the signatures agree) and the
    tab lists it to delete."""
    app = App(base(clips_dir=str(tmp_path / "clips")))
    res = app.set_channel_prefs({"FG1": {"engine": "local"}}, None, None,
                                {"FG2": {"open_db": 12.0}, "South 2": {"open_db": 30.0}, "FG1": {"close_db": 12.0},
                                 "FG1x": {}})
    assert app.channel_pref("FG1")["engine"] == "local"                      # the channel choices still applied
    assert app.cad.settings.squelch_overrides() == {} and res["squelch"] == {}
    assert app._receiver("South 2").squelch.params == (10.0, 6.0, 6)
    app.set_channel_prefs({}, None, None, {"FG1": {"open_db": 12.0}, "FG2": {"open_db": 12.0}})
    assert app._sq_ov == {"FG1": {"open_db": 12.0}}
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        data = hub.app.cad.settings.export()
        data["overrides"]["squelch"] = {"FG2": {"open_db": 12.0}, "FG1": {"open_db": 14.0}}   # FG2: not on this station
        r = hub.app.cad.settings.post("import", {"action": "apply", "settings": data, "operator": "admin@example.org"})
        assert any("FG2" in n and "kept" in n for n in r["notes"])
        assert wait(lambda: st._receiver("FG1").squelch.params[0] == 14.0, 10)
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        page = hub.get("/api/cad/squelch")
        assert page["orphans"] == [{"key": "FG2", "set": {"open_db": 12.0}}]
        from tests.test_channels import hub_post
        assert hub_post(hub, "squelch", {"action": "reset", "key": "FG2"})[0] == 200          # Delete on the tab
        assert hub.get("/api/cad/squelch")["orphans"] == []
        bad = dict(data, overrides={**data["overrides"], "squelch": {"South 2": {"open_db": 30.0}, "FG1": {"open_db": 99}}})
        r = hub.app.cad.settings.post("import", {"action": "dry_run", "settings": bad})       # never the whole backup refused
        assert any("South 2" in n and "read-only" in n for n in r["notes"]) and any("FG1" in n and "skipped" in n
                                                                                   for n in r["notes"])
    finally:
        if st is not None:
            st.link.stop()
        hub.close()
