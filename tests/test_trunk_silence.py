"""Silence alerts (#258): only the whole P25 trunk system going quiet alerts ("P25 trunk silent"), never one talkgroup
or one conventional channel (those are hints in Settings > Station > Health, with when they were last heard); the control
channel lost (trunk-recorder's decode rate) alerts at once; an older config with [alerts] main_channels still loads."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sizeup.cloud import health as healthmod
from sizeup.cloud.health import CC_LOST_S, StationHealth, station_metrics
from sizeup.config import ConfigError, load
from sizeup.radio.recorder import CC_RUN_GAP_S, CC_SILENT_S, ControlChannel, TrunkRecorder
from tests.test_health import T0, FakeHub, FakeNotifier, cfg as hcfg, radio, station_status

P25 = "Fire/EMS 15"
DAY, NIGHT = 2, 0


@pytest.fixture(autouse=True)
def daytime(monkeypatch):
    """Every moment in the 12-18 part of the day unless a test says otherwise."""
    monkeypatch.setattr(healthmod, "_part", lambda t: DAY)


class Rig:
    def __init__(self, **kw):
        self.hub, self.n = FakeHub(), FakeNotifier()
        self.h = StationHealth(self.hub, hcfg(**kw), None, self.n, lambda m: None, clock=lambda: self.hub.now)

    def at(self, dt, status):
        self.hub.now = T0 + dt
        self.hub.last_state = self.hub.now
        self.hub.status = status
        self.h.observe(status)
        return self.h.tick()

    def keys(self):
        return sorted(a["key"] for a in self.h.alerts())


def p25(last_any, tgs=None, cc=None, others=None, rec="ok"):
    """A station whose P25 system's last call (any talkgroup) was at T0 + last_any; tgs {"tg:307": T0 offset};
    others: more radio rows."""
    rows = [radio(P25, None, last_heard=T0 + last_any if last_any is not None else None)] + list(others or [])
    s = station_status(radios=rows)
    s["health"]["heard"] = {k: {"at": T0 + v, "name": P25 if k == "tg:307" else k} for k, v in (tgs or {}).items()}
    s["health"]["recorder"] = {"state": rec, "restarts": 0, "detail": "", "expected": True,
                               **({"cc": cc} if cc is not None else {})}
    return s


# -- one talkgroup or one channel: a hint, never an alert ---------------------------------------------------------------

def test_tg307_silent_while_other_talkgroups_are_active_is_no_alert():
    r = Rig()
    t = 0.0
    for i in range(40):                                                  # everything talks every 4 min
        t = i * 240.0
        r.at(t, p25(t, {"tg:307": t, "tg:333": t}))
    last307 = t
    for dt in range(60, 3 * 3600, 60):                                  # the system keeps talking; 307 doesn't
        r.at(t + dt, p25(t + dt, {"tg:307": last307, "tg:333": t + dt}))
    assert r.keys() == [] and r.n.msgs == []
    m = r.h.model()
    c = next(c for c in m["channels"] if c["label"] == "tg:307")
    assert c["usual_now_s"] == 240.0 and "main" not in c and "alert_after_s" not in c
    assert c["hint"].startswith("quiet 2 h 59 min (last ") and "usual gap" in c["hint"]
    assert "only the whole P25 system going quiet is one" in c["hint"]
    assert [h["channel"] for h in m["hints"]] == ["tg:307"]
    assert m["trunk"]["quiet_s"] < 120


@pytest.mark.parametrize("label", ["FG1", "HPN Tower", "South 2", "FG2"])
def test_a_conventional_channel_silent_for_hours_is_a_hint_only(label):
    r = Rig()
    t = 0.0
    for i in range(40):                                                  # it talks every 5 min, the trunk every minute
        t = i * 300.0
        r.at(t, p25(t, others=[radio(label, last_heard=T0 + t)]))
    heard = t
    for dt in range(60, 14 * 3600, 600):                                 # 14 h of nothing on it; P25 carries on
        r.at(t + dt, p25(t + dt, others=[radio(label, last_heard=T0 + heard)]))
    assert r.keys() == [] and r.n.msgs == []
    m = r.h.model()
    (hint,) = m["hints"]
    assert hint["channel"] == label and hint["text"].startswith("quiet 13 h")
    assert "(last " in hint["text"] and "no alert" in hint["text"]


def test_an_old_main_channel_alert_from_before_clears():
    # #242's quiet:<chan> alert held over a redeploy: nothing raises it any more, so it clears (with its recovery).
    r = Rig()
    r.at(0, p25(0))
    r.h.active["quiet:FG1"] = {"key": "quiet:FG1", "severity": "warning", "title": "FG1 silent", "text": "...",
                               "since": T0 - 3600, "raised": T0, "pushed": True, "who": {}}
    ch = r.at(5, p25(5))
    assert [(k, a["key"]) for k, a in ch] == [("clear", "quiet:FG1")] and r.keys() == []


# -- the whole trunk ---------------------------------------------------------------------------------------------------

def test_the_whole_trunk_silent_past_the_window_alerts_and_recovers_on_a_call():
    r = Rig()
    r.at(0, p25(0, {"tg:307": 0}))
    st = p25(0, {"tg:307": 0})
    r.at(19 * 60, st)
    assert r.keys() == []                                                # 19 min: the day window is 20
    assert r.h.model()["trunk"]["window_s"] == 20 * 60
    ch = r.at(21 * 60, st)
    assert [(k, a["key"]) for k, a in ch] == [("start", "trunk")]
    (a,) = r.h.alerts()
    assert a["severity"] == "critical" and a["title"] == "P25 trunk silent" and a["since"] == T0
    assert a["text"].startswith("no calls on any talkgroup since ")
    assert "(trunk-recorder running) · check P25 dongle, antenna, control channel (alert after 20 min)" in a["text"]
    assert a["who"]["channel"] == P25 and a["who"]["dongle"] == "SDR 103"
    (msg,) = r.n.msgs
    assert msg["title"].startswith("P25 trunk silent 21 min") and msg["priority"] == 4
    ch = r.at(22 * 60, p25(22 * 60, {"tg:333": 22 * 60}))              # a call on any talkgroup clears it
    assert [(k, a["key"]) for k, a in ch] == [("clear", "trunk")]
    from sizeup.cloud.health import SETTLE_S
    r.at(22 * 60 + SETTLE_S, p25(22 * 60 + SETTLE_S, {"tg:333": 22 * 60 + SETTLE_S}))   # settled (#340)
    assert r.n.msgs[-1]["title"].startswith("Recovered: P25 trunk silent")


def test_the_night_window_and_the_learned_one(monkeypatch):
    r = Rig()
    monkeypatch.setattr(healthmod, "_part", lambda t: NIGHT)
    r.at(0, p25(0))
    r.at(59 * 60, p25(0))
    assert r.keys() == []                                                # 00-06: 60 min
    r.at(61 * 60, p25(0))
    assert r.keys() == ["trunk"]
    # A quiet system (a call every 10 min, 40 gaps learned) by day: 4 x 10 min = 40 min, longer than the default.
    monkeypatch.setattr(healthmod, "_part", lambda t: DAY)
    r = Rig()
    t = 0.0
    for i in range(41):
        t = i * 600.0
        r.at(t, p25(t))
    r.at(t + 30 * 60, p25(t))
    tk = r.h.model()["trunk"]
    assert tk["usual_s"] == 600.0 and tk["window_s"] == 40 * 60 and r.keys() == []
    r.at(t + 41 * 60, p25(t))
    (a,) = r.h.alerts()
    assert "usual gap 10 min; alert after 40 min" in a["text"]
    # A busy system (a call a minute) never shortens it below the default.
    r = Rig()
    for i in range(41):
        t = i * 60.0
        r.at(t, p25(t))
    assert r.h.model()["trunk"]["window_s"] == 20 * 60
    r.at(t + 10 * 60, p25(t))
    assert r.keys() == []


def test_the_window_from_the_config():
    r = Rig(alerts_p25_silent_min=30.0)
    r.at(0, p25(0))
    r.at(25 * 60, p25(0))
    assert r.keys() == [] and r.h.panel()["thresholds"]["trunk"]["day_min"] == 30


def test_only_while_trunk_recorder_runs_and_a_recorder_back_up_gets_the_window():
    r = Rig()
    r.at(0, p25(0, rec="down"))
    r.at(3 * 3600, p25(0, rec="down"))
    assert r.keys() == ["recorder"]                                     # down: the recorder alert, not the trunk's
    r.at(3 * 3600 + 5, p25(0))                                           # back up, no call yet
    assert r.keys() == []
    r.at(3 * 3600 + 19 * 60, p25(0))
    assert r.keys() == []
    r.at(3 * 3600 + 21 * 60, p25(0))
    (a,) = r.h.alerts()
    assert a["key"] == "trunk" and a["since"] == T0 + 3 * 3600 + 5
    st = p25(0)                                                          # its dongle Off: nothing expected of it
    st["health"]["recorder"] = {"state": "off", "restarts": 0, "detail": "", "expected": False}
    r.at(4 * 3600, st)
    assert r.keys() == []


def test_the_control_channel_lost_alerts_at_once_and_clears_when_it_decodes():
    r = Rig()
    r.at(0, p25(0))
    lost = {"rate": 0.0, "low_since": T0 + 60, "last_low": T0 + 60 + CC_LOST_S - 1, "retunes": 3, "last_retune": T0 + 90}
    r.at(60 + CC_LOST_S - 1, p25(0, cc=lost))
    assert r.keys() == []                                                # under 2 min of it
    ch = r.at(60 + CC_LOST_S, p25(0, cc=lost))                         # 3 min after a call: the window isn't up
    assert [(k, a["key"]) for k, a in ch] == [("start", "trunk")]
    (a,) = r.h.alerts()
    assert a["title"] == "P25 trunk silent" and a["since"] == T0 + 60
    assert a["text"].startswith("control channel lost: trunk-recorder decodes 0 msg/s from it since ")
    assert ", 3 retunes (trunk-recorder running) · check P25 dongle, antenna, control channel" in a["text"]
    ok = {"rate": None, "low_since": None, "last_low": T0 + 200, "retunes": 3, "last_retune": T0 + 90}
    ch = r.at(240, p25(235, cc=ok))
    assert [(k, a["key"]) for k, a in ch] == [("clear", "trunk")]
    assert r.h.model()["trunk"]["cc"]["retunes"] == 3


def test_control_channel_from_trunk_recorders_log():
    now = [T0]
    cc = ControlChannel(clock=lambda: now[0])
    head = "[2026-11-06 13:05:00.000000] (error)   [westchester]\t"
    cc.feed(head + "Unit ID set via Control Channel, ext: 41228\tcurrent: -1\t samples: 0")
    assert cc.status() == {"rate": None, "low_since": None, "last_low": None, "retunes": 0, "last_retune": None,
                           "silent_since": None}
    cc.feed(head + "Control Channel Message Decode Rate: 7/sec, count:  21")   # low, not lost
    st = cc.status()
    assert st["rate"] == 7.0 and st["low_since"] is None
    for k in range(5):
        now[0] = T0 + 3 * (k + 1)
        cc.feed(head + "Control Channel Message Decode Rate: 0/sec, count:  0")
    now[0] += 1
    cc.feed(head + "Retuning to Control Channel: 476.162500 MHz")
    st = cc.status()
    assert st["rate"] == 0.0 and st["low_since"] == T0 + 3 and st["retunes"] == 1
    now[0] = T0 + 15 + CC_RUN_GAP_S + 1                                    # no low line since: the run is over
    assert cc.status()["low_since"] is None and cc.status()["rate"] is None
    now[0] = T0 + 3600                       # only the low rates are logged (the default warn rate): none is fine
    assert cc.status()["silent_since"] is None


def test_every_rate_logged_then_none_is_a_hung_decoder_and_alerts():
    """#547: with "controlWarnRate": -1 trunk-recorder logs a healthy rate every 3 s too; once it has, none for
    CC_SILENT_S while it runs is a fault (the station's monitor), and the hub's "P25 trunk silent" after CC_LOST_S."""
    now = [T0]
    cc = ControlChannel(clock=lambda: now[0])
    head = "[2026-11-06 13:05:00.000000] (info)    [westchester]\t"
    for k in range(10):
        now[0] = T0 + 3 * k
        cc.feed(head + "Control Channel Message Decode Rate: 38/sec, count:  114")
    last = now[0]
    now[0] = last + CC_SILENT_S
    assert cc.status()["silent_since"] is None and cc.status()["rate"] is None
    now[0] = last + CC_SILENT_S + 1
    assert cc.status()["silent_since"] == last
    r = Rig()
    r.at(0, p25(0))
    healthy = {"rate": 38.0, "low_since": None, "last_low": None, "retunes": 0, "last_retune": None, "silent_since": None}
    r.at(30, p25(25, cc=healthy))
    assert r.keys() == [] and r.h.model()["trunk"]["cc"]["rate"] is None   # a healthy rate: "Decoding", not "Low"
    hung = dict(healthy, rate=None, silent_since=T0 + 30)
    r.at(30 + CC_LOST_S - 1, p25(25, cc=hung))
    assert r.keys() == []
    r.at(30 + CC_LOST_S, p25(25, cc=hung))
    (a,) = r.h.alerts()
    assert a["title"] == "P25 trunk silent" and a["since"] == T0 + 30
    assert a["text"].startswith("control channel: no decode rate from trunk-recorder since ")
    r.at(400, p25(25, cc=dict(healthy, rate=7.0)))                          # decoding again (low, under the warn rate)
    assert r.keys() == [] and r.h.model()["trunk"]["cc"]["rate"] == 7.0


def test_a_critical_alert_still_on_is_pushed_again_every_few_hours_then_recovers():
    """#547: "P25 trunk silent" was pushed once and never again for 22 h. A pushed critical alert still on is pushed
    again every remind_min (3 h), never more often; its recovery says it is over."""
    r = Rig()
    r.at(0, p25(0))
    for dt in range(60, 22 * 3600, 60):
        r.at(dt, p25(0))
    titles = [m["title"] for m in r.n.msgs]
    first = [t for t in titles if not t.startswith("Still")]
    still = [t for t in titles if t.startswith("Still")]
    assert len(first) == 1 and first[0].startswith("P25 trunk silent 21 min")
    # first pushed at 21 min, then at 3 h 21 min, 6 h 21 min ... 21 h 21 min: 7 reminders in 22 h, 3 h apart
    assert len(still) == 7 and still[0].startswith("Still: P25 trunk silent 3 h 21 min")
    r.at(22 * 3600, p25(22 * 3600 - 5))                                   # a call: clear, then settled
    r.at(22 * 3600 + healthmod.SETTLE_S, p25(22 * 3600 + healthmod.SETTLE_S - 5))
    rec = r.n.msgs[-1]
    assert rec["title"].startswith("Recovered: P25 trunk silent") and "7 reminder(s) sent" in rec["body"]
    assert len(r.n.msgs) == 9


def test_reminders_off_muted_or_warnings_are_not_repeated():
    r = Rig(alerts_remind_min=0.0)
    r.at(0, p25(0))
    for dt in range(60, 8 * 3600, 300):
        r.at(dt, p25(0))
    assert len(r.n.msgs) == 1                                             # 0: the first push only, as before
    r = Rig()
    r.h.mutes = lambda: {"trunk": {"until": T0 + 30 * 24 * 3600}}
    r.at(0, p25(0))
    for dt in range(60, 8 * 3600, 300):
        r.at(dt, p25(0))
    assert r.n.msgs == []                                                 # muted: nothing, reminders included


def test_the_station_reports_the_control_channel(tmp_path):
    rec = TrunkRecorder("no-trunk-recorder --config=x", str(tmp_path))
    rec.cc.feed("[westchester]\tControl Channel Message Decode Rate: 1/sec, count:  3")
    app = SimpleNamespace(receivers=[], recorder=rec, p25=object(), shared=False)
    h = station_metrics(app, healthmod.Metrics(str(tmp_path)))
    assert h["recorder"]["cc"]["rate"] == 1.0 and h["recorder"]["cc"]["low_since"]


# -- the config ----------------------------------------------------------------------------------------------------------

def test_config_an_old_main_channels_still_loads_and_the_p25_window(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[alerts]\nmain_channels = [307, "FG1"]\n')            # #242's: read, changes nothing
    c = load(p)
    assert not hasattr(c, "alerts_main_channels")
    assert (c.alerts_p25_silent_min, c.alerts_p25_silent_night_min) == (20.0, 60.0)
    p.write_text('[alerts]\nmain_channels = "FG1"\np25_silent_min = 30\np25_silent_night_min = 90\n')
    c = load(p)
    assert (c.alerts_p25_silent_min, c.alerts_p25_silent_night_min) == (30.0, 90.0)
    p.write_text("[alerts]\np25_silent_min = 1\n")
    with pytest.raises(ConfigError, match="alerts.p25_silent_min: a number from 5 to 720"):
        load(p)


# -- #547 review ---------------------------------------------------------------------------------------------------------

def test_a_trunk_recorder_back_after_minutes_off_is_not_silent_since_before_it_stopped():
    """Fire/EMS 15 off 10 min (a dongle moved and back, shortfinal's hand-over, absent then back), then a new
    trunk-recorder: before its first rate line (3-8 s) the old last line made it "silent since" 10 min ago, and the
    hub paged at once. Judged from the new one's start now."""
    now = [T0]
    cc = ControlChannel(clock=lambda: now[0])
    cc.running(T0)
    for k in range(10):
        now[0] = T0 + 3 * k
        cc.feed("[westchester]\tControl Channel Message Decode Rate: 38/sec, count:  114")
    cc.running(None)                                                     # stopped: never silent while off
    now[0] = T0 + 600
    assert cc.status()["silent_since"] is None
    cc.running(T0 + 600)                                                 # a new one
    now[0] = T0 + 605
    st = cc.status()
    assert st["silent_since"] is None
    r = Rig()
    r.at(0, p25(0))
    r.at(605, p25(600, cc=st))
    assert r.keys() == [] and r.n.msgs == []
    now[0] = T0 + 600 + CC_SILENT_S + 1                                  # it really never logs: from its start
    assert cc.status()["silent_since"] == T0 + 600


def test_the_unit_recorder_tells_its_control_channel_when_trunk_recorder_started(tmp_path):
    import json
    import time as _t
    from sizeup.radio.recorder import UnitRecorder
    st = tmp_path / "status.json"
    rec = UnitRecorder("no-trunk-recorder --config=x", str(tmp_path), state_path=str(tmp_path / "s.json"),
                       status_path=str(st))
    started = _t.time() - 5
    st.write_text(json.dumps({"at": _t.time(), "state": "ok", "on": True, "sdr": "", "started": started}))
    rec.status()
    assert rec.cc.run_start == started
    st.write_text(json.dumps({"at": _t.time(), "state": "absent", "on": True, "sdr": ""}))
    rec.status()
    assert rec.cc.run_start is None


def test_a_wall_clock_step_is_no_control_channel_silence():
    now, mono = [T0], [100.0]
    cc = ControlChannel(clock=lambda: now[0], mono=lambda: mono[0])
    cc.feed("[westchester]\tControl Channel Message Decode Rate: 38/sec, count:  114")
    now[0] += 150                                                        # NTP / WSL2 resync: 1 s really passed
    mono[0] += 1
    assert cc.status()["silent_since"] is None


def test_no_decode_rate_is_pushed_only_after_the_watchdog_had_its_chance():
    """The station's watchdog restarts a hung decoder at 2 min: the hub shows the alert then, and pushes it only at
    CC_SILENT_NOTIFY_S (5 min). One the watchdog healed is never pushed."""
    healthy = {"rate": 38.0, "low_since": None, "last_low": None, "retunes": 0, "last_retune": None, "silent_since": None}
    hung = dict(healthy, rate=None, silent_since=T0 + 30)
    r = Rig()
    r.at(0, p25(0))
    r.at(30 + CC_LOST_S, p25(25, cc=hung))
    assert r.keys() == ["trunk"] and r.n.msgs == []                      # on the panel, not pushed
    r.at(30 + 200, p25(225, cc=healthy))                                 # restarted, decoding: cleared, never pushed
    r.at(30 + 200 + healthmod.SETTLE_S + 60, p25(30 + 200 + healthmod.SETTLE_S + 50, cc=healthy))
    assert r.keys() == [] and r.n.msgs == []
    r = Rig()
    r.at(0, p25(0))
    r.at(30 + CC_LOST_S, p25(25, cc=hung))
    r.at(30 + healthmod.CC_SILENT_NOTIFY_S - 1, p25(25, cc=hung))
    assert r.n.msgs == []
    r.at(30 + healthmod.CC_SILENT_NOTIFY_S, p25(25, cc=hung))
    assert len(r.n.msgs) == 1 and r.n.msgs[0]["title"].startswith("P25 trunk silent")
