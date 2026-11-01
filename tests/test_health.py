"""Station health (sizeup/cloud/health.py, #79/#35): each alert condition starts, is rate-limited, clears;
the hints; the panel and status endpoints' permissions; the config's refusal of Tapout's topics."""
import json
import time
from types import SimpleNamespace

import pytest

from sizeup.cloud.autoupdate import describe
from sizeup.cloud.health import JOB_GRACE_S, Metrics, StationHealth, status_token
from sizeup.cloud.hubstore import MonitorDb
from sizeup.config import ConfigError, load
from tests.test_access import TEAM, AUD, jwt, keys, jwks, req  # noqa: F401  (fixtures)
from tests.test_cloud import TOKEN, HubRun, call, hub_cfg

HUB = "a" * 40
OLD = "b" * 40
T0 = 1_760_000_000.0


class FakeHub:
    def __init__(self):
        self.status, self.last_state, self.last_seen = None, None, None
        self.started, self.skew, self.build = T0, 0.0, HUB
        self.now = T0

    def connected(self):
        return self.last_state is not None and self.now - self.last_state < 15

    def version(self):
        v = (self.status or {}).get("version")
        return describe(v.get("build"), self.build, v.get("update") or {}, self.now) if isinstance(v, dict) else None


class FakeNotifier:
    enabled, server, sent, failed, last_error = True, "http://127.0.0.1:1", 0, 0, ""

    def __init__(self):
        self.msgs = []

    def send(self, title, body, priority=3, tags=(), click="", who=None):
        from sizeup.notify import compose
        self.msgs.append({"title": compose(title, who), "body": body, "priority": priority, "tags": tags, "click": click})
        return True


def cfg(**kw):
    d = dict(alerts_silent_min=3.0, alerts_behind_min=15.0, alerts_down_min=2.0, alerts_outbox_min=5.0,
             alerts_disk_pct=90.0, alerts_repeat_min=15.0, cloud_public_url="https://hub.example")
    d.update(kw)
    return SimpleNamespace(**d)


def radio(label, mhz=453.0, state="ok", **kw):
    return {"label": label, "mhz": mhz, "state": state, "detail": "", "open": False, "restarts": 0, "last_heard": None,
            "quieting_db": None, "open_s": None, "stuck": False, **kw}


def station_status(build=HUB, **kw):
    st = {"id": "pcfd", "name": "PCFD station",
          "radios": [radio("South 2", 470.2), radio("FG1"), radio("Fire/EMS 15", None),
                     {"label": "STT", "mhz": None, "state": "ok", "detail": ""}],
          "sdr": {"shared": True, "sdrs": [
              {"serial": "00000101", "name": "101", "mode": "South 2", "present": True},
              {"serial": "00000102", "name": "102", "mode": "FG1", "present": True},
              {"serial": "00000103", "name": "103", "mode": "Fire/EMS 15", "present": True}]},
          "outbox": {"pending": 0, "oldest": None, "dropped_audio": 0},
          "version": {"build": build, "hub": HUB, "update": {}},
          "health": {"started": T0 - 3600, "boot": T0 - 86400, "host": "station", "cpu_pct": 12.5, "load1": 0.4, "cpus": 4,
                     "mem_pct": 40.0, "disk_pct": 50.0, "disk_free": 100e9, "temp_c": 48.0,
                     "whisper": {"state": "ok", "detail": "base.en"},
                     "recorder": {"state": "ok", "restarts": 0, "detail": "", "expected": True},
                     "jobs": {"South 2": {"state": "ok", "restarts": 0, "expected": True},
                              "FG1": {"state": "ok", "restarts": 0, "expected": True}}}}
    st.update(kw)
    return st


class Rig:
    """A StationHealth on a fake hub with a clock and a fake notifier."""

    def __init__(self, db=None, **kw):
        self.hub, self.n, self.logs = FakeHub(), FakeNotifier(), []
        self.h = StationHealth(self.hub, cfg(**kw), db, self.n, self.logs.append, clock=lambda: self.hub.now)

    def post(self, status=None, at=None):
        """A state post from the station now."""
        self.hub.last_state = self.hub.now if at is None else at
        if status is not None:
            self.hub.status = status
            self.h.observe(status)

    def at(self, dt, post=True, status=None):
        self.hub.now = T0 + dt
        if post:
            self.post(status)
        return self.h.tick()

    def keys(self):
        return sorted(a["key"] for a in self.h.alerts())


# -- conditions --------------------------------------------------------------------------------------------------

def test_silent_starts_after_three_minutes_pushes_once_and_clears_with_a_recovery_notice():
    r = Rig()
    assert r.at(0, post=False) == [] and r.at(170, post=False) == []      # the hub just started: can't tell yet
    ch = r.at(181, post=False)
    assert [(k, a["key"]) for k, a in ch] == [("start", "silent")]
    (a,) = r.h.alerts()
    assert a["severity"] == "critical" and a["since"] == T0 and "since the hub started" in a["text"]
    assert r.n.msgs[0]["title"] == "Station silent 3 min · the station · never reported" and r.n.msgs[0]["priority"] == 4
    assert any(m.startswith("health: ALERT Station silent") for m in r.logs)
    r.at(200, post=False)
    assert len(r.n.msgs) == 1                                             # once, not every tick
    ch = r.at(210, status=station_status())
    assert [(k, a["key"]) for k, a in ch] == [("clear", "silent")] and r.keys() == []
    assert r.n.msgs[1]["title"].startswith("Recovered: Station silent") and any("health: cleared Station silent" in m for m in r.logs)
    # the station goes quiet: "since" its last post, the push says when it was last heard
    r.at(300)
    r.at(300 + 179, post=False)
    assert r.keys() == []
    r.at(300 + 181, post=False)
    assert r.keys() == ["silent"] and r.h.alerts()[0]["since"] == T0 + 300
    assert len(r.n.msgs) == 2                                              # within 15 min of the last push: not again yet
    r.at(181 + 900, post=False)
    assert r.n.msgs[-1]["title"] == "Station silent 13 min · PCFD station · last heard " + \
        __import__("time").strftime("%H:%M", __import__("time").localtime(T0 + 300))


def test_while_silent_nothing_else_starts_or_clears():
    r = Rig()
    r.at(0, status=station_status(radios=[radio("FG1", open=True, open_s=200.0, stuck=True)]))
    assert r.keys() == ["stuck:FG1"]
    r.at(400, post=False)                                                  # silent: the stuck flag is old news
    assert r.keys() == ["silent", "stuck:FG1"]
    r.hub.status = station_status()                                        # (nothing new arrives: no post)
    r.at(500, post=False)
    assert r.keys() == ["silent", "stuck:FG1"]
    r.at(510, status=station_status())                                     # back: judged again
    assert r.keys() == []


def test_behind_the_hub_for_fifteen_minutes():
    r = Rig()
    r.at(0, status=station_status(build=OLD))
    r.at(14 * 60, status=station_status(build=OLD))
    assert r.keys() == []
    r.at(15 * 60, status=station_status(build=OLD))
    (a,) = r.h.alerts()
    assert a["key"] == "behind" and a["since"] == T0 and "bbbbbbb" in a["text"] and "aaaaaaa" in a["text"]
    assert r.n.msgs[-1]["title"] == "Station behind the hub 15 min · PCFD station · station bbbbbbb, hub aaaaaaa"
    r.at(16 * 60, status=station_status(build=HUB))
    assert r.keys() == [] and r.n.msgs[-1]["title"].startswith("Recovered: Station behind the hub")
    # an update to the hub's build in progress counts too (stuck updating), a docs-only equivalent doesn't
    upd = {"state": "updating", "to": HUB, "since": T0 + 17 * 60}
    r.at(17 * 60, status=station_status(build=OLD, version={"build": OLD, "update": upd}))
    r.at(33 * 60, status=station_status(build=OLD, version={"build": OLD, "update": dict(upd, since=T0 + 33 * 60)}))
    assert r.keys() == ["behind"]
    r.at(34 * 60, status=station_status(build=OLD, version={"build": OLD, "update": {"equivalent": {HUB: OLD}}}))
    assert r.keys() == []


def test_a_failed_or_rolled_back_update_alerts_at_once_with_the_update_line():
    r = Rig()
    last = {"at": T0 - 10, "from": OLD, "to": HUB, "result": "rolled back", "why": "/healthz didn't answer",
            "line": f"update bbbbbbb -> aaaaaaa: FAILED (/healthz didn't answer): back on bbbbbbb: ok"}
    upd = {"state": "failed", "failed": {HUB: {"at": T0 - 10, "why": "/healthz didn't answer"}}, "last": last}
    r.at(0, status=station_status(build=OLD, version={"build": OLD, "update": upd}))
    (a,) = r.h.alerts()
    assert a["key"] == "update" and a["severity"] == "warning" and "failed" in a["text"] and "back on bbbbbbb: ok" in a["text"]
    upd2 = dict(upd, last=dict(last, result="rollback failed"))
    r.at(10, status=station_status(build=OLD, version={"build": OLD, "update": upd2}))
    assert r.h.alerts()[0]["severity"] == "critical" and "rollback too" in r.h.alerts()[0]["title"]
    r.hub.build = "c" * 40                                              # the hub moves on: a new try, not failed
    r.at(20, status=station_status(build=OLD, version={"build": OLD, "update": upd}))
    assert "update" not in r.keys()


def test_a_dongle_gone_at_once_and_a_job_not_running_after_its_grace():
    r = Rig()
    st = station_status()
    st["sdr"]["sdrs"][1]["present"] = False
    r.at(0, status=st)
    (a,) = r.h.alerts()
    assert a["key"] == "sdr:00000102" and a["severity"] == "critical" and "FG1" in a["text"]
    assert r.n.msgs[-1]["title"] == "SDR 102 gone · PCFD station · SDR 102 on FG1"
    st = station_status()
    st["health"]["jobs"]["South 2"] = {"state": "down", "restarts": 4, "detail": "rtl_fm exited", "expected": True}
    r.at(10, status=st)
    assert r.keys() == []                                               # the dongle's back; South 2 within its grace
    r.at(10 + JOB_GRACE_S, status=st)
    (a,) = r.h.alerts()
    assert a["key"] == "job:South 2" and "4 restarts" in a["text"] and "rtl_fm exited" in a["text"]
    assert a["who"] == {"station": "PCFD station", "channel": "South 2", "dongle": "SDR 101"}
    r.at(100, status=station_status())
    assert r.keys() == []


def test_one_dongle_per_channel_each_channel_is_a_job():
    r = Rig()
    st = station_status(sdr=None, radios=[radio("FG1", state="absent", detail="dongle 'fg1' not connected")])
    st["health"]["jobs"] = {"FG1": {"state": "absent", "restarts": 0, "expected": True}}
    r.at(0, status=st)
    r.at(JOB_GRACE_S, status=st)
    assert r.keys() == ["job:FG1"]


def test_whisper_or_trunk_recorder_down_for_two_minutes():
    r = Rig()
    st = station_status()
    st["health"]["whisper"] = {"state": "down", "detail": "cannot start whisper-server"}
    st["health"]["recorder"] = {"state": "down", "restarts": 3, "detail": "exited 1", "expected": True}
    r.at(0, status=st)
    r.at(119, status=st)
    assert r.keys() == []
    r.at(120, status=st)
    assert r.keys() == ["recorder", "whisper"]
    assert {a["key"]: a["severity"] for a in r.h.alerts()} == {"recorder": "critical", "whisper": "warning"}
    st2 = station_status()
    st2["health"]["recorder"] = {"state": "off", "restarts": 0, "detail": "", "expected": False}   # its dongle is Off
    st2["sdr"]["sdrs"][2]["mode"] = "Off"
    r.at(130, status=st2)
    assert r.keys() == []


def test_a_stuck_channel_and_the_outbox_backing_up_and_a_full_disk():
    r = Rig()
    r.hub.skew = 2.0                                                    # hub clock - station clock
    st = station_status(radios=[radio("FG1", open=True, open_s=150.0, stuck=True)],
                        outbox={"pending": 40, "oldest": T0 - 6 * 60 - 2.0, "dropped_audio": 0})
    st["health"]["disk_pct"] = 92.0
    r.at(0, status=st)
    assert r.keys() == ["disk", "outbox", "stuck:FG1"]
    a = {a["key"]: a for a in r.h.alerts()}
    assert a["outbox"]["since"] == T0 - 6 * 60 and "40 event(s)" in a["outbox"]["text"]
    assert a["stuck:FG1"]["since"] == T0 - 150 and a["disk"]["severity"] == "warning"
    st["health"]["disk_pct"] = 98.0
    r.at(5, status=st)
    assert {x["key"]: x["severity"] for x in r.h.alerts()}["disk"] == "critical"
    r.at(10, status=station_status(outbox={"pending": 3, "oldest": T0 + 7, "dropped_audio": 0}))
    assert r.keys() == []                                               # a fresh, short backlog is normal


def test_at_most_one_push_per_condition_per_fifteen_minutes_plus_its_recovery():
    r = Rig()
    stuck = station_status(radios=[radio("FG1", open=True, open_s=130.0, stuck=True)])
    calm = station_status()
    r.at(0, status=stuck)
    r.at(60, status=calm)
    assert [m["title"].split(" · ")[0] for m in r.n.msgs] == ["FG1 stuck open 2 min", "Recovered: FG1 stuck open"]
    r.at(120, status=stuck)                                             # again within 15 min: shown, not pushed
    assert r.keys() == ["stuck:FG1"] and len(r.n.msgs) == 2
    r.at(180, status=calm)                                              # its clear isn't pushed either
    assert len(r.n.msgs) == 2 and len(r.h.history) == 2
    r.at(300, status=stuck)
    r.at(899, status=stuck)
    assert len(r.n.msgs) == 2
    r.at(900, status=stuck)                                             # still on when the 15 min are up: pushed now
    assert len(r.n.msgs) == 3 and r.n.msgs[-1]["title"].startswith("FG1 stuck open")
    r.at(960, status=calm)
    assert r.n.msgs[-1]["title"].startswith("Recovered: FG1 stuck open")
    hist = r.h.panel()["history"]
    assert [h["pushed"] for h in hist] == [True, False, True]


def test_alerts_survive_a_hub_restart_without_a_second_push(tmp_path):
    db = MonitorDb(tmp_path / "hub.sqlite")
    r = Rig(db)
    r.at(0, status=station_status(build=OLD))
    r.at(15 * 60, status=station_status(build=OLD))
    assert r.keys() == ["behind"] and len(r.n.msgs) == 1
    r2 = Rig(db)                                                         # the redeployed hub
    r2.hub.now = T0 + 16 * 60
    r2.hub.started = r2.hub.now
    assert [a["key"] for a in r2.h.alerts()] == ["behind"]
    r2.at(16 * 60, post=False)                                           # nobody reported yet: kept
    assert [a["key"] for a in r2.h.alerts()] == ["behind"]
    r2.at(16 * 60 + 5, status=station_status(build=OLD))
    assert [a["key"] for a in r2.h.alerts()] == ["behind"] and r2.n.msgs == []
    r2.at(17 * 60, status=station_status(build=HUB))
    assert r2.h.alerts() == [] and r2.n.msgs[0]["title"].startswith("Recovered: Station behind the hub")


def test_a_channel_quiet_far_longer_than_usual_is_a_hint_not_an_alert():
    r = Rig()
    t = 0.0
    for i in range(12):                                                  # FG1 talks every ~5 min
        t = i * 300.0
        st = station_status(radios=[radio("FG1", last_heard=T0 + t)])
        r.at(t, status=st)
    ch = r.h.model()["channels"][0]
    assert ch["gaps"] == 11 and ch["typical_gap_s"] == 300.0 and ch["hint"] == ""
    r.at(t + 40 * 60, status=st)
    m = r.h.model()
    assert "nothing heard for 40 min" in m["channels"][0]["hint"] and m["hints"][0]["channel"] == "FG1"
    assert r.keys() == [] and r.n.msgs == []


def test_the_model_has_what_the_panel_shows():
    r = Rig()
    st = station_status(radios=[radio("FG1", last_heard=T0 - 30, quieting_db=12.5, restarts=1)])
    r.at(0, status=st)
    m = r.h.model()
    assert m["station"]["name"] == "PCFD station" and m["station"]["connected"] and m["station"]["last_state"] == T0
    assert m["build"]["state"] == "ok" and m["build"]["station"] == HUB and m["build"]["hub"] == HUB
    assert m["system"]["uptime_s"] == 3600 and m["system"]["cpu_pct"] == 12.5 and m["system"]["temp_c"] == 48.0
    assert m["processes"]["whisper"]["state"] == "ok" and m["processes"]["recorder"]["state"] == "ok"
    assert [(d["name"], d["job"], d["running"]) for d in m["dongles"]] == [("101", "South 2", True), ("102", "FG1", True),
                                                                            ("103", "Fire/EMS 15", True)]
    assert m["channels"][0]["quiet_s"] == 30 and m["channels"][0]["quieting_db"] == 12.5
    p = r.h.panel()
    assert p["notify"]["on"] and p["thresholds"]["silent_min"] == 3 and p["alerts"] == []


def test_an_older_station_without_health_still_gets_the_basics():
    r = Rig()
    st = station_status()
    del st["health"]
    st["radios"][3] = {"label": "STT", "mhz": None, "state": "down", "detail": "whisper died"}
    r.at(0, status=st)
    r.at(121, status=st)
    m = r.h.model()
    assert not m["health_reported"] and m["system"]["disk_pct"] is None
    assert r.keys() == ["whisper"]


def test_station_metrics_from_proc(tmp_path):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\nbtime 1700000000\n")
    (proc / "self" / "stat").write_text("123 (python3 x) S " + " ".join(["0"] * 18) + " 1000 0 0\n")
    (proc / "meminfo").write_text("MemTotal:       1000000 kB\nMemFree: 1 kB\nMemAvailable:    250000 kB\n")
    zone = tmp_path / "sys" / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("51234\n")
    m = Metrics(str(tmp_path), str(proc), str(tmp_path / "sys"))
    import os
    assert m.started == 1700000000 + 1000 / os.sysconf("SC_CLK_TCK")
    s = m.sample()
    assert s["mem_pct"] == 75.0 and s["temp_c"] == 51.2 and s["boot"] == 1700000000 and s["cpu_pct"] is None
    (proc / "stat").write_text("cpu  150 0 150 900 0 0 0 0 0 0\nbtime 1700000000\n")
    assert m.cpu_pct() == 50.0
    assert 0 <= s["disk_pct"] <= 100


# -- the endpoints -------------------------------------------------------------------------------------------------

def test_status_token_is_read_only_and_the_panel_needs_no_station_token(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        st_tok = status_token(TOKEN)
        assert st_tok and st_tok != TOKEN and status_token(TOKEN, "explicit-status-token-123") == "explicit-status-token-123"
        url = hub.url + "/api/station/status"
        assert call(url, token=None)[0] == 401 and call(url, token="wrong-token-wrong-token")[0] == 401
        code, body = call(url, token=st_tok)
        assert code == 200 and body["hub_build"] == hub.app.hub.build and body["station"]["reported"] is False
        assert call(url, token=TOKEN)[0] == 200
        assert not hub.app.hub.connected() and hub.app.hub.last_seen is None     # reading it isn't the station
        assert call(url, "POST", {}, token=st_tok)[0] == 405
        # the status token opens nothing else
        for path, method in (("/api/station/state", "POST"), ("/api/station/events", "POST"),
                             ("/api/station/jobs?wait=0", "GET"), ("/api/station/hello", "GET")):
            assert call(hub.url + path, method, {} if method == "POST" else None, token=st_tok)[0] == 401, path
        # the station reports: the status says so
        assert call(hub.url + "/api/station/state", "POST", {"at": 1, "status": station_status(build="")})[0] == 200
        code, body = call(url, token=st_tok)
        assert body["station"]["reported"] and body["station"]["connected"]
        # the panel: a person's page (no Access configured here: open like the rest of the CAD API)
        code, p = call(hub.url + "/api/cad/health", token=None)
        assert code == 200 and p["health"]["station"]["name"] == "PCFD station" and "history" in p
        # the banner's data: the CAD state's cloud
        assert hub.get("/api/cad/state")["cloud"]["alerts"] == []
        row = hub.get("/api/state")["radios"][0]
        assert row["label"] == "Station" and row["link"] == "#settings/system/health"
    finally:
        hub.close()


@pytest.fixture
def access_hub(tmp_path, jwks):
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD))
    h.server.access.certs_url = jwks.url
    h.server.access.log = lambda m: None
    yield h
    h.close()


def test_with_access_the_panel_needs_a_person_and_the_status_a_bearer(keys, access_hub):  # noqa: F811
    h = access_hub
    assert req(h.url + "/api/cad/health")[0] == 401
    assert req(h.url + "/api/cad/health", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 401    # not a person
    code, p = req(h.url + "/api/cad/health", headers={"Cf-Access-Jwt-Assertion": jwt(keys[0])})
    assert code == 200 and "health" in p
    assert req(h.url + "/api/station/status")[0] == 401
    assert req(h.url + "/api/station/status", headers={"Authorization": f"Bearer {status_token(TOKEN)}"})[0] == 200


# -- config ------------------------------------------------------------------------------------------------------

def _toml(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


@pytest.mark.parametrize("topic", ["pcfd-phone-prealert", "pcfd-status"])
def test_the_config_refuses_tapouts_topics(tmp_path, monkeypatch, topic):
    with pytest.raises(ConfigError, match="Tapout"):
        load(_toml(tmp_path, f'[alerts]\nntfy_topic = "{topic}"\n'))
    monkeypatch.setenv("SIZEUP_ALERT_TOPIC", topic)
    with pytest.raises(ConfigError, match="Tapout"):
        load(_toml(tmp_path, ""))


def test_the_config_refuses_the_configured_prealert_topics_too(tmp_path):
    with pytest.raises(ConfigError, match="Tapout"):
        load(_toml(tmp_path, '[prealert]\nring_topic = "firehouse-ring-xyz"\n[alerts]\nntfy_topic = "firehouse-ring-xyz"\n'))


def test_alerts_config_defaults_env_and_limits(tmp_path, monkeypatch):
    c = load(_toml(tmp_path, ""))
    assert c.alerts_ntfy_topic == "" and c.alerts_silent_min == 3 and c.alerts_behind_min == 15 and c.alerts_repeat_min == 15
    c = load(_toml(tmp_path, '[alerts]\nntfy_topic = "sizeup-health-8f2k1x9q"\nsilent_min = 5\n'))
    assert c.alerts_ntfy_topic == "sizeup-health-8f2k1x9q" and c.alerts_silent_min == 5
    monkeypatch.setenv("SIZEUP_ALERT_TOPIC", "sizeup-health-from-ssm-77")
    assert load(_toml(tmp_path, '[alerts]\nntfy_topic = "sizeup-health-8f2k1x9q"\n')).alerts_ntfy_topic == "sizeup-health-from-ssm-77"
    monkeypatch.delenv("SIZEUP_ALERT_TOPIC")
    for bad in ('ntfy_topic = "short"', "silent_min = 0", 'disk_pct = "x"', 'ntfy_server = "http://ntfy.example.org"', "nope = 1"):
        with pytest.raises(ConfigError):
            load(_toml(tmp_path, f"[alerts]\n{bad}\n"))
    monkeypatch.setenv("SIZEUP_STATUS_TOKEN", "short")
    with pytest.raises(ConfigError, match="status_token"):
        load(_toml(tmp_path, '[cloud]\nrole = "hub"\n'))


def test_the_hub_config_ships_without_a_topic():
    from pathlib import Path
    c = load(Path(__file__).resolve().parent.parent / "deploy" / "hub" / "config.hub.toml")
    assert c.alerts_ntfy_topic == "" and c.alerts_status_topic == "" and c.alerts_silent_min == 3 and c.alerts_behind_min == 15


# -- two topics: the alarms to [alerts] ntfy_topic, health / status to status_topic (sizeup/notify.py route()) --------
# Stand-ins shaped like the hub's real topics, which live only in SSM (a topic is readable by its name; this
# repository is public).
OUR_ALERTS, OUR_STATUS = "pcfd-xmpl-alerts", "pcfd-xmpl-status"


def test_the_status_topic_from_the_config_or_the_env_validated_like_the_alert_topic(tmp_path, monkeypatch):
    c = load(_toml(tmp_path, f'[alerts]\nntfy_topic = "{OUR_ALERTS}"\nstatus_topic = "{OUR_STATUS}"\n'))
    assert (c.alerts_ntfy_topic, c.alerts_status_topic) == (OUR_ALERTS, OUR_STATUS)
    assert load(_toml(tmp_path, "")).alerts_status_topic == ""
    monkeypatch.setenv("SIZEUP_STATUS_TOPIC", "pcfd-xmpl-status-from-ssm")
    c = load(_toml(tmp_path, f'[alerts]\nstatus_topic = "{OUR_STATUS}"\n'))
    assert c.alerts_status_topic == "pcfd-xmpl-status-from-ssm" and c.alerts_ntfy_topic == ""
    monkeypatch.delenv("SIZEUP_STATUS_TOPIC")
    with pytest.raises(ConfigError, match="status_topic"):
        load(_toml(tmp_path, '[alerts]\nstatus_topic = "short"\n'))


@pytest.mark.parametrize("topic", ["pcfd-phone-prealert", "pcfd-status", "PCFD-Status"])
def test_the_status_topic_refuses_tapouts_topics(tmp_path, monkeypatch, topic):
    with pytest.raises(ConfigError, match="Tapout"):
        load(_toml(tmp_path, f'[alerts]\nstatus_topic = "{topic}"\n'))
    monkeypatch.setenv("SIZEUP_STATUS_TOPIC", topic)
    with pytest.raises(ConfigError, match="SIZEUP_STATUS_TOPIC: .*Tapout"):
        load(_toml(tmp_path, ""))


def test_the_status_topic_refuses_the_configured_prealert_topics_too(tmp_path):
    with pytest.raises(ConfigError, match="Tapout"):
        load(_toml(tmp_path, '[prealert]\nstatus_topic = "firehouse-status-xyz"\n[alerts]\nstatus_topic = "firehouse-status-xyz"\n'))
    with pytest.raises(ConfigError, match="Tapout"):
        load(_toml(tmp_path, '[prealert]\nring_topic = "firehouse-ring-xyz"\n[alerts]\nstatus_topic = "firehouse-ring-xyz"\n'))


def test_a_topic_of_ours_with_status_in_its_name_is_not_tapouts(tmp_path, monkeypatch):
    """The refusal is the whole name: "status" (or "prealert") inside a name of ours is fine, for either topic."""
    for name in (OUR_STATUS, "pcfd-status-of-ours", "pcfd-phone-prealert-ours"):
        monkeypatch.setenv("SIZEUP_STATUS_TOPIC", name)
        monkeypatch.setenv("SIZEUP_ALERT_TOPIC", name.replace("status", "alerts"))
        c = load(_toml(tmp_path, '[prealert]\n'))                     # Tapout's default topics are the ones refused
        assert c.alerts_status_topic == name and c.alerts_ntfy_topic == name.replace("status", "alerts")


def _hub_pushes(tmp_path, ntfy, capsys, alerts, status):
    """A hub (not serving) with these topics: its startup lines, its alarms' and station health's notifiers, and
    the topic each kind's push landed on (a mayday on FG1, a station-health alert and its recovery)."""
    from sizeup.cloud.hub import HubApp
    from tests.test_alarms import tx
    app = HubApp(hub_cfg(tmp_path, alerts_ntfy_server=ntfy.url, alerts_ntfy_topic=alerts, alerts_status_topic=status),
                 log=print)
    try:
        lines = capsys.readouterr().out
        al, hn = app.cad.alarms.notifier, app.health.notifier
        t = tx("FG1-7", "mayday mayday mayday, Engine 64", rig="E64", unit="E64 P1", radio_id="6401", at=time.time())
        app.cad.record_traffic(t)
        app.cad.alarms.on_traffic(t)
        now = time.time()
        a = {"key": "silent", "severity": "critical", "title": "Station silent", "text": "no state post",
             "since": now - 300, "raised": now, "pushed": False, "who": {}}
        app.health._push(a, now)
        a["cleared"] = now
        app.health._push(a, now, recovered=True)
        assert al.flush(5) and hn.flush(5)
        got = [(m["body"] if "body" in m else m)["topic"] for m in ntfy.got]
        titles = [(m["body"] if "body" in m else m)["title"].split(" · ")[0] for m in ntfy.got]
        return lines, al, hn, sorted(zip(titles, got))
    finally:
        app.stop()


@pytest.fixture
def ntfy():
    from tests.test_notify import FakeNtfy
    f = FakeNtfy()
    yield f
    f.close()


def test_the_hub_routes_alarms_to_the_alerts_topic_and_health_to_the_status_topic(tmp_path, ntfy, capsys):
    lines, al, hn, got = _hub_pushes(tmp_path, ntfy, capsys, OUR_ALERTS, OUR_STATUS)
    assert al.topic == OUR_ALERTS and hn.topic == OUR_STATUS and al.current() is not hn.current()
    assert got == [("MAYDAY", OUR_ALERTS), ("Recovered: Station silent", OUR_STATUS), ("Station silent 5 min", OUR_STATUS)]
    assert "notify: alerts \u2192 topic set (config file), status \u2192 topic set (config file)" in lines
    assert OUR_ALERTS not in lines and OUR_STATUS not in lines            # the names stay secret


@pytest.mark.parametrize("which", ["alerts", "status"])
def test_with_one_topic_every_kind_goes_to_it(tmp_path, ntfy, capsys, which):
    only = OUR_ALERTS if which == "alerts" else OUR_STATUS
    lines, al, hn, got = _hub_pushes(tmp_path, ntfy, capsys, only if which == "alerts" else "",
                                     only if which == "status" else "")
    assert al.current() is hn.current() and al.topic == only               # one queue, in order, as before the split
    assert got == [("MAYDAY", only), ("Recovered: Station silent", only), ("Station silent 5 min", only)]
    other = "status" if which == "alerts" else "alerts"
    assert f"{other} \u2192 the {which} topic (no {other} topic set)" in lines and only not in lines


def test_with_no_topic_there_are_no_pushes(tmp_path, ntfy, capsys):
    lines, al, hn, got = _hub_pushes(tmp_path, ntfy, capsys, "", "")
    assert not al.enabled and not hn.enabled and got == []
    assert "notify: no pushes" in lines and "health: no push for station alerts" in lines


def test_the_taskdef_gives_the_hub_both_topics_from_ssm():
    from pathlib import Path
    td = json.loads((Path(__file__).resolve().parent.parent / "deploy" / "aws" / "taskdef.json").read_text())
    (hub,) = [c for c in td["containerDefinitions"] if c["name"] == "hub"]
    secrets = {s["name"]: s["valueFrom"] for s in hub["secrets"]}
    ssm = "arn:aws:ssm:us-east-1:504804195561:parameter/sizeup-hub/"
    assert secrets["SIZEUP_ALERT_TOPIC"] == ssm + "alert-topic"
    assert secrets["SIZEUP_STATUS_TOPIC"] == ssm + "status-topic"
    env = {e["name"] for e in hub.get("environment", [])}
    assert not env & {"SIZEUP_ALERT_TOPIC", "SIZEUP_STATUS_TOPIC"}       # never a plain value in the task definition


def test_a_real_station_reports_its_health_in_the_state_post(tmp_path):
    from tests.test_app import wait
    from tests.test_cloud import station, station_cfg
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        assert wait(lambda: isinstance((hub.app.hub.status or {}).get("health"), dict), 15)
        hl = hub.app.hub.status["health"]
        assert {"started", "disk_pct", "load1", "jobs", "whisper"} <= set(hl) and 0 < hl["disk_pct"] <= 100
        m = hub.app.health.model()
        assert m["health_reported"] and m["station"]["connected"] and m["system"]["uptime_s"] is not None
    finally:
        if st is not None:
            st.link.stop()
            st.stop()
        hub.close()


def test_the_connected_line_holds_still_between_polls():
    """Between two of the station's polls the row's text stays the same, so the state push has nothing to send (#146)."""
    from sizeup.cloud.hub import Hub
    now = time.time()
    link = SimpleNamespace(token="t", status={"name": "PC station"}, polling=0, skew=0.0, errors=0, last_error="",
                           connected=lambda: True, version=lambda: None)
    rows = []
    for ago in (0, 1, 2, 4):
        link.last_seen = now - ago
        rows.append(Hub.health(link))
    assert all(r == {"state": "ok", "word": "", "detail": "PC station: connected"} for r in rows)
    link.last_seen = now - 8
    late = Hub.health(link)
    assert late["word"] == "8 s" and late["detail"] == "PC station: connected, last seen 8 s ago"
