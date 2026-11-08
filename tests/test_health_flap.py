"""Health pushes that flap, and alerts muted (GitHub #340; sizeup/cloud/health.py, Settings > System > Push notifications).

The flapping case is the station's real ADS-B decoder (sizeup/radio/adsbd.py, #315/#319) on a dongle that wedges: readsb
runs ~17 s, prints its watchdog line and exits, and after 3 wedges in an hour the decoder waits 5 then 15 min between
tries. Its status goes through adsb_health() into the hub's StationHealth every 5 s, as on the hub. The decoder, readsb's
words and its JSON output are tests/test_adsb_diagnose.py's and tests/test_adsb_receiver.py's harness: no real dongle."""
from types import SimpleNamespace

from sizeup import errwatch
from sizeup.cloud import health as H
from sizeup.cloud.health import FLAP_N, SETTLE_FLAPPING_S, SETTLE_S
from tests.test_adsb_diagnose import WEDGED, Station
from tests.test_adsb_receiver import readsb_output
from tests.test_health import T0, Rig, station_status
from tests.test_push_settings import Rig as SettingsRig
from tests.test_roles import COREY, call

TICK = 5


def adsb(msgs):
    return [m for m in msgs if "ADS-B" in m["title"]]


class WedgingStation:
    """The station's ADS-B decoder on a wedging dongle, and the hub's health fed from it every TICK s."""

    def __init__(self, tmp_path, rig):
        self.s, self.r, self.tmp = Station(tmp_path), rig, tmp_path
        self.s.lines = WEDGED
        self.d = self.s.decoder()
        self.t = 0                          # seconds after T0, the hub's and the station's clock alike
        self.decoding = False               # the dongle swapped: readsb decodes instead of wedging

    def step(self):
        now = T0 + self.t
        p = self.s.started[-1] if self.s.started else None
        if p is not None and p.code is None and not self.decoding and now - self.started_at >= 17:
            p.code = 1                      # readsb's watchdog: "SDR wedged, exiting!"
        if self.decoding:
            readsb_output(self.tmp, now, 1000 + self.t, samples=now)
        n = len(self.s.started)
        st = self.d.step(now)
        if len(self.s.started) > n:
            self.started_at = now
        h = H.adsb_health(SimpleNamespace(adsb_sdr="adsb", adsb_row=lambda: {
            "serial": "adsb", "name": "LNA", "present": True, "installed": True, "decoder": st}))
        status = station_status()
        status["health"]["adsb"] = h
        self.r.at(self.t, status=status)
        self.t += TICK
        return st

    def run(self, seconds):
        states = []
        for _ in range(int(seconds // TICK)):
            states.append(self.step()["state"])
        return states


def test_a_wedging_adsb_decoder_for_hours_is_one_push_then_one_recovery_once_it_settles(tmp_path):
    r = Rig()
    w = WedgingStation(tmp_path, r)
    states = w.run(3 * 3600)
    # it really flapped: starting (the alert clears) and down (it comes back) over and over, waiting 15 min a time
    flips = sum(1 for a, b in zip(states, states[1:]) if (a == "down") != (b == "down"))
    assert flips >= 20 and "retry in" in w.d.status["detail"]
    downs = [m for m in r.logs if m.startswith("health: back ADS-B receiver down")]
    assert len(downs) >= 8                                                # back again and again: one episode
    pushes = adsb(r.n.msgs)
    assert len(pushes) == 1 and pushes[0]["title"].startswith("ADS-B receiver down")
    (a,) = [x for x in r.h.alerts() if x["key"] == "adsb"] or [None]
    assert a is None or a["since"] < T0 + 3600                            # the one episode, from the first time
    assert r.h.panel()["thresholds"]["flap_n"] == FLAP_N
    # a good dongle: readsb decodes; "Recovered" once it has stayed clear (30 min: it was flapping), and only once
    w.decoding = True
    w.d.next_start = 0
    w.run(SETTLE_S)
    assert len(adsb(r.n.msgs)) == 1                                       # 15 min clear: a flapping one waits longer
    w.run(SETTLE_FLAPPING_S - SETTLE_S + 60)
    assert w.d.status["state"] == "ok" and "adsb" not in r.keys()
    pushes = adsb(r.n.msgs)
    assert [m["title"].split(" · ")[0] for m in pushes] == [pushes[0]["title"].split(" · ")[0], "Recovered: ADS-B receiver down"]
    assert "before it settled" in pushes[1]["body"]
    w.run(3600)
    assert len(adsb(r.n.msgs)) == 2
    hist = [h for h in r.h.panel()["history"] if h["key"] == "adsb"]
    assert len(hist) == 1 and hist[0]["pushed"]                           # one episode on Settings > Health too


def test_back_after_it_settled_is_a_new_episode():
    r = Rig()
    stuck = station_status(radios=[{"label": "FG1", "mhz": 453.0, "state": "ok", "detail": "", "open": True,
                                     "open_s": 130.0, "stuck": True, "restarts": 0, "last_heard": None}])
    r.at(0, status=stuck)
    r.at(60, status=station_status())
    r.at(60 + SETTLE_S, status=station_status())
    r.at(60 + SETTLE_S + 10, status=stuck)
    titles = [m["title"].split(" · ")[0] for m in r.n.msgs]
    assert titles == ["FG1 stuck open 2 min", "Recovered: FG1 stuck open", "FG1 stuck open 2 min"]
    # a tick missed: it came back (its since) only after it had settled: the recovery first, then the new episode
    r2 = Rig()
    r2.at(0, status=stuck)
    r2.at(60, status=station_status())
    r2.at(60 + SETTLE_S + 300, status=stuck)
    assert [m["title"].split(" · ")[0] for m in r2.n.msgs] == titles


def adsb_down(state="down"):
    st = station_status()
    st["health"]["adsb"] = {"sdr": "adsb", "name": "LNA", "present": True, "installed": True, "state": state,
                            "detail": "SDR wedged (stalled): check power; avoid USB extensions/unpowered hubs; try another dongle",
                            "cause": "wedged"}
    return st


def test_a_muted_alert_is_on_health_but_never_pushed_nor_its_recovery():
    r = Rig()
    mutes = {"adsb": {"until": None}}
    r.h.mutes = lambda: mutes
    r.at(0, status=adsb_down())
    r.at(120, status=adsb_down())
    (a,) = [x for x in r.h.alerts() if x["key"] == "adsb"]
    assert a["muted"] is True and a["muted_until"] is None                # on Settings > Health, marked muted
    assert [x for x in r.h.panel()["alerts"] if x["key"] == "adsb"][0]["muted"]
    r.at(3600, status=adsb_down())
    assert adsb(r.n.msgs) == []
    r.at(3700, status=station_status())
    r.at(3700 + SETTLE_FLAPPING_S + 5, status=station_status())
    assert adsb(r.n.msgs) == [] and any(h["key"] == "adsb" for h in r.h.panel()["history"])
    # another kind isn't muted by it
    st = adsb_down()
    st["health"]["whisper"] = {"state": "down", "detail": "cannot start whisper-server"}
    t = 3700 + SETTLE_FLAPPING_S + 10
    r.at(t, status=st)
    r.at(t + 120, status=st)
    assert [m["title"].split(" · ")[0] for m in r.n.msgs if "Speech" in m["title"]] == ["Speech-to-text down 2 min"]
    assert adsb(r.n.msgs) == []


def test_a_mute_until_a_time_pushes_once_it_ends_and_says_it_flapped():
    r = Rig()
    r.h.mutes = lambda: {"adsb": {"until": T0 + 3600}}
    t = 0
    for _ in range(4):                                                    # down 10 min, up 30 s, four times
        r.at(t, status=adsb_down())
        r.at(t + 120, status=adsb_down())
        r.at(t + 600, status=adsb_down())
        r.at(t + 630, status=adsb_down("starting"))
        t += 660
    r.at(t, status=adsb_down())
    r.at(t + 120, status=adsb_down())
    assert adsb(r.n.msgs) == [] and [x for x in r.h.alerts() if x["key"] == "adsb"][0]["flaps"] >= FLAP_N
    r.at(3600, status=adsb_down())                                        # the mute is over, still down: pushed now
    (m,) = adsb(r.n.msgs)
    assert m["title"].startswith("ADS-B receiver down 60 min") and "flapping, back 4× in 1 h" in m["body"]


def test_mute_in_settings_audited_exported_and_read_by_health(tmp_path):
    rig = SettingsRig(tmp_path)
    post = lambda body: call(rig.api, "/api/cad/settings/alert_mutes", body, COREY)       # noqa: E731
    page = rig.cad.settings_get()["alert_mutes"]
    kinds = {k["key"]: k for k in page["kinds"]}
    assert {"adsb", "whisper", "stt_backlog", "job", "trunk", "silent", "hub_errors"} <= set(kinds)
    assert not any(k["muted"] for k in page["kinds"])                     # default: nothing muted
    code, out = post({"action": "mute", "key": "adsb"})
    assert code == 200 and next(k for k in out["alert_mutes"]["kinds"] if k["key"] == "adsb") == \
        {"key": "adsb", "label": "ADS-B receiver down", "muted": True, "until": None}
    now = rig.cad.store.clock()
    code, out = post({"action": "mute", "key": "whisper", "hours": 24})
    w = next(k for k in out["alert_mutes"]["kinds"] if k["key"] == "whisper")
    assert code == 200 and w["muted"] and abs(w["until"] - (now + 86400)) < 5
    for body in ({"action": "mute", "key": "nope"}, {"action": "mute", "key": "adsb", "hours": 0},
                 {"action": "mute", "key": "adsb", "hours": "24"}, {"action": "silence", "key": "adsb"}):
        assert post(body)[0] == 400
    h = H.StationHealth(SimpleNamespace(skew=0.0), SimpleNamespace(
        alerts_silent_min=3, alerts_behind_min=15, alerts_down_min=2, alerts_outbox_min=5, alerts_disk_pct=90,
        alerts_repeat_min=15, cloud_public_url=""), clock=lambda: now)
    h.mutes = lambda: rig.cad.settings.section(H.MUTE_SECTION)
    assert h.muted("adsb") and h.muted("whisper") and not h.muted("job:FG1") and not h.muted("hub_errors")
    assert not h.muted("whisper", now + 86400 + 60)                       # until a time: over by then
    assert post({"action": "unmute", "key": "adsb"})[0] == 200 and not h.muted("adsb")
    audit = rig.cad.settings_audit_log({"section": ["system"]})["audit"]
    rows = [(a["key"], a["old"], a["new"]) for a in reversed(audit) if a["key"].startswith("alert_mutes/")]
    assert rows[0] == ("alert_mutes/adsb", "pushed", "muted") and rows[-1] == ("alert_mutes/adsb", "muted", "pushed")
    assert rows[1][0] == "alert_mutes/whisper" and rows[1][2].startswith("muted until ")
    ex = rig.cad.settings_export()
    assert set(ex["overrides"]["alert_mutes"]) == {"whisper"}
    ex["overrides"]["alert_mutes"] = {"nope": {"until": None}}
    code, out = call(rig.api, "/api/cad/settings/import", {"action": "apply", "settings": ex}, COREY)
    assert code == 400 and "alert_mutes" in out["error"]


def test_hub_errors_muted_are_counted_not_pushed():
    from tests.test_errwatch import OVERFLOW, Clock, FakeNotifier, stamp
    clock, n, logs = Clock(), FakeNotifier(True), []
    muted = [True]
    w = errwatch.ErrorWatch(n, clock=clock, log=logs.append, muted=lambda: muted[0])
    for i in range(30):
        w.feed(stamp(clock.t, OVERFLOW.format(n=i)))
    clock.t += 30
    assert w.tick() and n.got == [] and any("(not pushed: muted)" in m for m in logs)


def test_the_hub_reads_the_mutes_from_settings(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    app = HubApp(hub_cfg(tmp_path), log=lambda m: None)
    try:
        assert not app.health.muted("adsb") and not app.error_watch.muted()
        app.cad.settings.post("alert_mutes", {"action": "mute", "key": "adsb", "operator": "Corey"})
        app.cad.settings.post("alert_mutes", {"action": "mute", "key": "hub_errors", "hours": 1, "operator": "Corey"})
        assert app.health.muted("adsb") and app.error_watch.muted() and not app.health.muted("whisper")
    finally:
        app.stop()
