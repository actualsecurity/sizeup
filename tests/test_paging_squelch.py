"""The paging channel's squelch: static opens are not pages (App._static_open), its Open / Close set by an admin inside
a guard (Settings._paging_guard: over the idle max, under the pages), audited, sent to a station that takes it and
applied live; the advisory suggestion may suggest a raise when static is frequent (sizeup/cloud/metricsinsight.py).

Every number is made up: static that quiets the hiss 9-13 dB for under a second or two, pages around 30 dB, an Open of
10 dB (the receiver's default). Synthetic audio only."""

from __future__ import annotations

import json
import random

import numpy as np
import pytest

from sizeup import channels
from sizeup.app import App
from sizeup.cad.store import CadError
from sizeup.cloud import metricsinsight as mi
from sizeup.radio.fm import FRAME, RATE, Channel, Segment
from tests.test_app import PAGE_TEXT, FakeWhisper, tone, voice, wait
from tests.test_cloud import HubRun, base, hub_cfg, station, station_cfg
from tests.test_signal_insight import CFG, DAY, NOW, floor_minutes, transmissions

rng = np.random.default_rng(23)
SOUTH = Channel(470.2, "South 2")
ADMIN = "admin@example.org"


def hiss(s, amp=0.4):
    """No carrier: the discriminator's wideband noise."""
    return (amp * rng.standard_normal(int(RATE * s))).astype(np.float32)


def static(s, down_db=11.5):
    """A static burst: the hiss pushed down a little (down_db of quieting), still flat noise: no tones, no speech."""
    return hiss(s, 0.4 * 10 ** (-down_db / 20))


def keyed(audio, ctcss=156.7):
    """Carrier up: quiet (about 26 dB of quieting), the sub-audible tone under the audio."""
    t = np.arange(len(audio)) / RATE
    return (audio + 0.1 * np.sin(2 * np.pi * ctcss * t) + 0.02 * rng.standard_normal(len(audio))).astype(np.float32)


def squelch_segments(app, audio, t0=2_000_000_000.0):
    """audio through a fresh copy of South 2's own squelch (its hang and continuation window): the segments."""
    sq = app._receiver("South 2").new_squelch()
    clock = {"t": t0}
    sq.clock = lambda: clock["t"]
    out = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        clock["t"] += FRAME / RATE
        s = sq.feed(audio[i:i + FRAME])
        if s is not None:
            out.append(s)
    return out


@pytest.fixture
def app(tmp_path, monkeypatch):
    a = App(base(clips_dir=str(tmp_path / "clips")))
    a.metrics_seen = []
    monkeypatch.setattr(a, "_metric_tx", lambda label, kind, seg, **kw: a.metrics_seen.append((label, kind)))
    return a


def seg(dur, q_max, quieting=None, audio=None, **kw):
    now = 2_000_000_000.0
    a = hiss(dur, 0.01) if audio is None else audio
    s = Segment(now, now + len(a) / RATE, a, q_max - 1.0 if quieting is None else quieting, **kw)
    s.q_max = q_max
    return s


# -- static opens are not pages ----------------------------------------------------------------------------------------

def test_static_opens_through_the_real_squelch_are_dropped(app):
    audio = np.concatenate([hiss(3.0)] + [p for _ in range(4) for p in (static(0.8), hiss(2.0))])
    segs = squelch_segments(app, audio)
    assert len(segs) == 4 and all(9.0 <= s.q_max < 14.0 and s.end - s.start < 2.0 for s in segs)   # the old "pages"
    for s in segs:
        app._segment(SOUTH, s)
    assert app._static_n == {"South 2": 4}
    assert app.metrics_seen == [("South 2", "noise")] * 4          # noise, as on a traffic channel
    assert list(app.pages) == [] and app.pagehold.held == {}         # no page, no Radio row, nothing to transcribe
    assert app.state()["pages"] == [] and app.store.active() is None


def test_a_static_open_never_reaches_speech_to_text_or_an_incident(app):
    """Even if speech-to-text would hear a whole dispatch in it, a static open is never transcribed or made a call."""
    app.stt.client = FakeWhisper({1: PAGE_TEXT})
    app.stt.start()
    app._segment(SOUTH, seg(1.0, 12.0))
    app._release_pages(now=2_100_000_000.0)
    assert not app.pages and not app.store.incidents and not app.state()["loose_traffic"]


def test_a_real_tone_page_at_30_db_still_passes(app):
    tx = keyed(np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]))
    [s] = squelch_segments(app, np.concatenate([hiss(3.0), tx.astype(np.float32), hiss(2.0)]))
    assert s.q_max > 20
    app._segment(SOUTH, s)
    assert app.metrics_seen == [("South 2", "page")] and not app._static_n
    assert [p.kind for p in app.pages] == ["page"] and app.pages[0].tones


def test_a_short_tone_burst_still_passes(app):
    """Tones decide: a short, weak transmission with a tone in it is never static."""
    a = (0.5 * np.sin(2 * np.pi * 1000.0 * np.arange(int(1.6 * RATE)) / RATE)).astype(np.float32)
    s = seg(1.6, 11.0, audio=a)
    assert app._static_open("South 2", s, []) and not app._static_open("South 2", s, ["a tone"])


@pytest.mark.parametrize("dur, q_max, why", [(6.0, 30.0, "long voice page at 30 dB"),
                                             (5.0, 12.0, "weak 12 dB 5-s voice page"),
                                             (1.5, 20.0, "short but strongly quieted"),
                                             (2.0, 11.0, "two seconds: not short")])
def test_voice_pages_that_are_not_static_still_pass(app, dur, q_max, why):
    app._segment(SOUTH, seg(dur, q_max, audio=voice(dur)))
    assert app.metrics_seen == [("South 2", "page")], why
    assert [p.kind for p in app.pages] == ["voice"] and not app._static_n


def test_a_continuation_or_a_key_up_piece_is_never_static(app):
    for kw in ({"cont": True}, {"split": "level"}):
        assert not app._static_open("South 2", seg(0.8, 11.0, **kw), [])
    burst = seg(0.8, 11.0)
    burst.bursts = [np.zeros(800, np.float32)]
    assert not app._static_open("South 2", burst, [])
    assert app._static_open("South 2", seg(0.8, 11.0), [])


def test_the_gate_follows_the_running_open(app):
    s = seg(1.0, 15.0)
    assert not app._static_open("South 2", s, [])                    # 15 >= 10 + 4
    app._receiver("South 2").set_squelch(14.0, 10.0, None)
    assert app._static_open("South 2", s, [])                        # 15 < 14 + 4


# -- the guard ---------------------------------------------------------------------------------------------------------

class Levels:
    """Stands in for the hub's metric insight (Insight.paging_levels)."""

    def __init__(self, idle_max=9.0, page_db=30.0, pages=6):
        self.lv = {"idle_max": idle_max, "page_db": page_db, "pages": pages}
        self.forgot = []

    def paging_levels(self, key):
        return dict(self.lv) if key == "South 2" else None

    def forget(self, key):
        self.forgot.append(key)


def post(app, **body):
    return app.cad.settings.post("squelch", {"action": "set", "key": "South 2", "operator": ADMIN, **body})


def test_open_is_guarded_by_the_idle_max_and_the_pages(app):
    app.cad.station_metrics_insight = Levels(idle_max=9.0, page_db=30.0)
    with pytest.raises(CadError, match="idle max 9"):
        post(app, open_db=10.5)                                     # 9 + 2 = 11 at least
    with pytest.raises(CadError, match="pages could be missed"):
        post(app, open_db=24.5)                                     # 30 - 6 = 24 at most
    with pytest.raises(CadError, match="under open"):
        post(app, open_db=14, close_db=14)
    assert app.cad.settings.squelch_overrides() == {} and app._receiver("South 2").squelch.params == (10.0, 6.0, 6)
    out = post(app, open_db=24)
    assert out["guard"] == {"idle_max": 9.0, "page_db": 30.0, "pages": 6}
    r = post(app, open_db=13.5)                                     # Close: Open less the default hysteresis
    assert app.cad.settings.squelch_overrides() == {"South 2": {"open_db": 13.5, "close_db": 9.5}}
    assert app._receiver("South 2").squelch.params == (13.5, 9.5, 6)    # live, the Min the receiver's
    assert r["guard"]["page_db"] == 30.0 and "South 2" in app.cad.station_metrics_insight.forgot


def test_without_numbers_it_stays_near_the_default(app):
    """No idle numbers: never under the default; no pages: at most 6 dB over it."""
    with pytest.raises(CadError, match="no idle numbers"):
        post(app, open_db=9.5)
    with pytest.raises(CadError, match="no recent pages"):
        post(app, open_db=16.5)
    post(app, open_db=16, close_db=11)
    assert app._receiver("South 2").squelch.params == (16.0, 11.0, 6)


def test_a_change_is_audited_who_when_old_and_new_and_reset_goes_back(app):
    app.cad.station_metrics_insight = Levels()
    post(app, open_db=14, close_db=10)
    a = app.cad.settings.audit_log("radios")[0]
    assert a["key"] == "squelch/South 2" and a["operator"] == ADMIN and a["at"]
    assert (json.loads(a["old"]), json.loads(a["new"])) == ({"open_db": 10.0, "close_db": 6.0}, {"open_db": 14.0, "close_db": 10.0})
    post(app, open_db=15)
    a = app.cad.settings.audit_log("radios")[0]
    assert (json.loads(a["old"]), json.loads(a["new"])) == ({"open_db": 14.0, "close_db": 10.0}, {"open_db": 15.0, "close_db": 11.0})
    app.cad.settings.post("squelch", {"action": "reset", "key": "South 2", "operator": ADMIN})
    assert app._receiver("South 2").squelch.params == (10.0, 6.0, 6) and app.cad.settings.squelch_overrides() == {}
    assert app.cad.settings.audit_log("radios")[0]["new"] == "default"
    post(app, open_db=14)
    post(app, open_db=None)                                         # null: the default too
    assert app.cad.settings.squelch_overrides() == {} and app._receiver("South 2").squelch.params[0] == 10.0


def test_only_admins_may_set_it():
    from sizeup.cad.api import needs
    assert needs("/api/cad/settings/squelch") == "admin"


def test_an_import_keeps_a_paging_squelch_only_inside_its_guard(app):
    app.cad.station_metrics_insight = Levels(idle_max=9.0, page_db=30.0)
    data = app.cad.settings.export()
    data["overrides"]["squelch"] = {"South 2": {"open_db": 27.0, "close_db": 20.0}}
    r = app.cad.settings.post("import", {"action": "apply", "settings": data, "operator": ADMIN})
    assert any("South 2" in n and "skipped (paging" in n for n in r["notes"]) and app._sq_ov == {}
    data["overrides"]["squelch"] = {"South 2": {"open_db": 14.0, "close_db": 10.0, "min_open_s": 1.0}}
    app.cad.settings.post("import", {"action": "apply", "settings": data, "operator": ADMIN})
    assert app._sq_ov == {"South 2": {"open_db": 14.0, "close_db": 10.0}}
    assert app._receiver("South 2").squelch.params == (14.0, 10.0, 6)


# -- hub -> station -----------------------------------------------------------------------------------------------------

def test_the_hub_sends_the_paging_squelch_and_the_station_applies_it_live(tmp_path):
    from tests.test_channels import hub_post
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        assert wait(lambda: any(c["key"] == "South 2" and c["editable"]
                                for c in hub.get("/api/cad/squelch")["channels"]), 10)
        fg1 = st._receiver("FG1").squelch.params
        code, _ = hub_post(hub, "squelch", {"action": "set", "key": "South 2", "open_db": 14, "close_db": 10})
        assert code == 200
        assert wait(lambda: st._receiver("South 2").squelch.params == (14.0, 10.0, 6), 10)
        assert st._receiver("FG1").squelch.params == fg1                # the other channels untouched
        assert st.cad.settings.audit_log("radios")[0]["operator"] == "hub"
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        assert hub_post(hub, "squelch", {"action": "set", "key": "South 2", "open_db": 25})[0] >= 400   # no pages: 16 at most
        assert hub_post(hub, "squelch", {"action": "reset", "key": "South 2"})[0] == 200
        assert wait(lambda: st._receiver("South 2").squelch.params == (10.0, 6.0, 6), 10)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_an_older_station_is_never_sent_a_paging_squelch(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app = hub.app
        rows = [{"label": "South 2", "channel_kind": "paging", "min_open_s": 0.3,
                 "squelch_default": {"open_db": 10.0, "close_db": 6.0, "min_open_s": 0.3}},
                {"label": "FG1", "channel_kind": "traffic", "min_open_s": 0.3,
                 "squelch_default": {"open_db": 10.0, "close_db": 6.0, "min_open_s": 0.3}}]
        app._sq_ov = {"South 2": {"open_db": 14.0, "close_db": 10.0}, "FG1": {"open_db": 12.0}}
        app.hub.status = {"channels": {"sig": "x", "squelch_ok": True}, "radios": rows}       # before paging
        want = app.channel_desired()
        assert want["squelch"] == {"FG1": {"open_db": 12.0}}
        app.hub.status = {"channels": {"sig": want["sig"], "squelch_ok": True}, "radios": rows}
        assert "paging squelch" in app.channels_sync()["detail"]
        app.hub.status = {"channels": {"sig": "x", "squelch_ok": True, "squelch_paging_ok": True}, "radios": rows}
        assert app.channel_desired()["squelch"] == {"South 2": {"open_db": 14.0, "close_db": 10.0},
                                                    "FG1": {"open_db": 12.0}}
    finally:
        hub.close()


def test_fit_squelch_agrees_on_both_sides():
    kinds = {"South 2": "paging", "FG1": "traffic"}
    d = lambda k: {"open_db": 10.0, "close_db": 6.0, "min_open_s": 0.3}       # noqa: E731
    m = {"South 2": {"open_db": 14.0, "min_open_s": 1.0}, "FG1": {"open_db": 12.0}}
    assert channels.fit_squelch(m, kinds, d)[0] == {"FG1": {"open_db": 12.0}}                 # an older station's
    assert channels.fit_squelch(m, kinds, d, paging=True)[0] == {"South 2": {"open_db": 14.0}, "FG1": {"open_db": 12.0}}
    kept, why = channels.fit_squelch({"South 2": {"min_open_s": 1.0}}, kinds, d, paging=True)
    assert kept == {} and "Open / Close only" in why[0]


# -- the metrics: static is never a page; the advisory may raise ----------------------------------------------------------

def static_pages(n, q=(9.0, 13.0), seed=7):
    """What the station recorded as "page" before the gate: short static at 9-13 dB."""
    r = random.Random(seed)
    return sorted(({"k": "tx", "at": NOW - 7 * DAY + r.random() * 7 * DAY, "kind": "page", "dur": round(r.uniform(0.4, 1.6), 2),
                    "q": round(r.uniform(*q) - 0.5, 1), "qo": round(r.uniform(*q) - 1, 1), "qx": round(r.uniform(*q), 1),
                    "qc": 6.5} for _ in range(n)), key=lambda d: d["at"])


def test_real_page_tells_static_from_pages():
    assert not channels.real_page({"kind": "page", "dur": 0.8, "qx": 12.0, "q": 11.0}, 10.0)
    assert channels.real_page({"kind": "page", "dur": 0.8, "qx": 15.0}, 10.0)
    assert channels.real_page({"kind": "page", "dur": 5.0, "q": 12.0}, 10.0)
    assert channels.real_page({"kind": "page", "dur": 0.8, "q": 12.0, "cont": 1}, 10.0)
    assert not channels.real_page({"kind": "noise", "dur": 5.0, "q": 30.0})
    assert "noise" not in mi.REAL_KINDS


def test_static_never_counts_as_pages_and_frequent_static_suggests_a_raise():
    floor = floor_minutes(NOW - 7 * DAY, 7 * 1440, q90=lambda k, r: r.gauss(5.0, 0.4), kind="paging")
    pages = transmissions(NOW - 7 * DAY, 7 * DAY, [random.Random(k).uniform(28, 32) for k in range(20)], kind="page")
    noise = static_pages(200)
    out = mi.suggest(floor, pages + noise, CFG, NOW)
    assert out["pages"] == 20 and out["static"] == 200 and 28 <= out["page_db"] <= 32
    assert out["real"] == 20                                         # the static "pages" never among the real ones
    assert out["open"] > out["cur_open"] == 10.0 and out["open"] >= 13.5     # over the static's peaks
    assert out["open"] <= out["page_db"] - mi.PAGE_GAP_DB
    assert out["info"] == "paging: advisory" and not out["apply"] and "static" in out["why"]
    # the same static recorded as "noise" (the gate, now): the same
    as_noise = [dict(d, kind="noise") for d in noise]
    assert mi.suggest(floor, pages + as_noise, CFG, NOW)["open"] == out["open"]
    # rare static: never above the running Open
    calm = mi.suggest(floor, pages + noise[:5], CFG, NOW)
    assert calm["open"] <= 10.0 and "static" not in calm["why"]


def test_the_guard_numbers_come_before_a_day_of_data():
    floor = floor_minutes(NOW - 3 * 3600, 180, q90=lambda k, r: r.gauss(9.0, 0.3), kind="paging")
    out = mi.suggest(floor, transmissions(NOW - 3 * 3600, 3 * 3600, [30.0, 31.0, 29.0], kind="page"), CFG, NOW)
    assert out["state"] == "short" and out["idle_max"] is not None and 9.5 <= out["idle_max"] <= 11.0
    assert out["page_db"] == 30.0                                    # the pages' median at open


def test_page_level_shift_ignores_static_recorded_as_pages():
    before = transmissions(NOW - 2 * DAY, DAY, [30.0] * 6, kind="page")
    after = transmissions(NOW - DAY, DAY, [30.0] * 6, kind="page", seed=4) + static_pages(40)
    assert mi.check_levels(before, after, CFG, paging=True) == []
