"""[radio] auto_assign (issue #56): a dongle that appears with no job a person chose gets FG1, then the
county P25 system (Fire/EMS 15), then South 2. Fakes only: no dongle is ever opened (librtlsdr lookups
are faked, rtl_fm and trunk-recorder don't exist), and claims live in a temp dir (conftest.py)."""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

import sizeup.app as appmod
import sizeup.radio.claims as claims
from sizeup.app import App
from sizeup.config import ChannelCfg, ConfigError, load
from sizeup.radio.recorder import TrunkRecorder
from tests.test_app import cfg

P25 = "Fire/EMS 15"
DONGLES = [(0, "RTL2838UHIDIR", "00000001"), (1, "Blog V4", "v4"), (2, "RTL2838UHIDIR", "adsb"),
           (3, "RTL2838UHIDIR", "spare")]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """(plugged list to edit, log lines, make(**cfg changes) -> started App). Every app is stopped."""
    plugged: list[tuple[int, str, str]] = []
    logs: list[str] = []
    apps: list[App] = []
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    monkeypatch.setattr(appmod, "SDR_WATCH_S", 3600.0)          # tests look for arrivals themselves
    monkeypatch.setattr(appmod, "log", logs.append)
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    monkeypatch.setattr(appmod.sdr_lookup, "resolve",
                        lambda serial, *a: next((i for i, _p, s in plugged if s == serial), None))
    monkeypatch.setattr(TrunkRecorder, "one_dongle", lambda self: (True, ""))
    (tmp_path / "clips").mkdir(exist_ok=True)
    base = replace(cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"),
                   channels=(ChannelCfg("South 2", 470.2, "paging", "fire-page"),
                             ChannelCfg("FG1", 453.0375, "traffic", "fire-fg")),
                   sdr_auto=True, sdr_auto_assign=True, sdr_names={"00000001": "V3 #2", "v4": "V4", "adsb": "V3"},
                   p25_dir=str(tmp_path / "p25"), p25_talkgroups={307: P25},
                   p25_recorder=str(tmp_path / "no-trunk-recorder") + " --config=x", p25_recorder_dir=str(tmp_path))

    def make(**kw) -> App:
        app = App(replace(base, **kw)).start()
        apps.append(app)
        return app

    yield plugged, logs, make
    for a in apps:
        a.stop()


def jobs(app: App) -> dict[str, tuple[str, str | None]]:
    return {x["serial"]: (x["mode"], x["set_by"]) for x in app.state()["sdr"]["sdrs"]}


def saved(tmp_path) -> dict:
    return json.loads((tmp_path / "sdr_modes.json").read_text())


@pytest.mark.parametrize("n, want", [
    (1, ["FG1"]),
    (2, ["FG1", P25]),
    (3, ["FG1", P25, "South 2"]),
    (4, ["FG1", P25, "South 2", "Off"]),            # all three covered: the fourth stays Off
])
def test_priority_order_with_one_to_four_dongles(rig, n, want):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:n]
    app = make()
    assert [app.assign.get(s, "Off") for _i, _p, s in DONGLES[:n]] == want
    assert [jobs(app)[s] for _i, _p, s in DONGLES[:n]] == [(m, "auto" if m != "Off" else None) for m in want]
    assert "sdr: V3 #2 (00000001) → FG1 (auto)" in logs
    if n == 4:
        assert "sdr: spare stays Off (auto: FG1, Fire/EMS 15, South 2 already covered)" in logs
    on = {m: getattr(app._receiver(m).device, "serial", None) for m in ("FG1", "South 2")}
    assert on == {m: (s if m in want else None) for (_i, _p, s), m in zip(DONGLES, ["FG1", P25, "South 2"])
                  if m != P25}                                     # each channel started on its dongle
    assert (app.recorder._thread is not None) == (P25 in want)
    mine = {s: json.loads((claims.DIR / f"{s}.json").read_text())["mode"] for _i, _p, s in DONGLES[:n]
            if (claims.DIR / f"{s}.json").exists()}
    assert mine == {s: m for (_i, _p, s), m in zip(DONGLES, want) if m != "Off"}


def test_hot_plugged_dongle_gets_the_next_job(rig, tmp_path):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:1]
    app = make()
    assert app.assign == {"00000001": "FG1"}
    plugged.append(DONGLES[1])                                     # plugged in while running
    assert app._sdr_arrivals() == [P25]
    assert jobs(app)["v4"] == (P25, "auto") and "sdr: V4 (v4) → Fire/EMS 15 (auto)" in logs
    assert app._sdr_arrivals() == []                               # seen already: nothing more
    assert saved(tmp_path)["_set_by"] == {"00000001": "auto", "v4": "auto"}


def test_the_watcher_thread_assigns_a_hot_plugged_dongle(rig, monkeypatch):
    plugged, logs, make = rig
    monkeypatch.setattr(appmod, "SDR_WATCH_S", 0.05)
    app = make()
    assert app.assign == {}
    plugged.append(DONGLES[0])
    from tests.test_app import wait
    assert wait(lambda: app.assign.get("00000001") == "FG1")


def test_person_choices_including_off_survive_a_restart(rig, tmp_path):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:3]
    app = make()
    assert [app.assign[s] for s in ("00000001", "v4", "adsb")] == ["FG1", P25, "South 2"]
    assert app.set_mode("Off", "00000001") is None                 # a person: FG1's dongle Off
    assert app.set_mode("FG1", "adsb") is None                     # a person: FG1 here (South 2 stops)
    assert app.set_mode(P25, "v4") is None                         # clicking the auto job: now the person's
    assert jobs(app) == {"00000001": ("Off", "person"), "v4": (P25, "person"), "adsb": ("FG1", "person")}
    assert saved(tmp_path)["_set_by"] == {"00000001": "person", "v4": "person", "adsb": "person"}
    app.stop()

    logs.clear()
    again = make()                                                 # restart: South 2 is free, but
    assert again.assign == {"00000001": "Off", "v4": P25, "adsb": "FG1"}   # the person's Off stays Off
    assert "sdr: V3 #2 (00000001) stays Off (set by a person)" in logs
    plugged.pop(0)
    again._sdr_arrivals()
    plugged.insert(0, DONGLES[0])                                  # replugged: still the person's Off
    again._sdr_arrivals()
    assert again.assign["00000001"] == "Off" and jobs(again)["00000001"] == ("Off", "person")


def test_a_job_moved_away_by_a_person_leaves_an_off_auto_may_fill_on_restart(rig, tmp_path):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:2]
    app = make()
    assert app.set_mode("FG1", "v4") is None                       # FG1 moves to the V4: V3 #2 goes Off
    assert jobs(app) == {"00000001": ("Off", None), "v4": ("FG1", "person")}
    assert app._sdr_arrivals() == []                               # nothing reshuffled while running
    app.stop()
    again = make()
    assert again.assign == {"00000001": P25, "v4": "FG1"}           # nobody chose that Off


def test_dongle_held_by_another_app_is_skipped(rig, tmp_path):
    plugged, logs, make = rig
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "00000001.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getppid(), "mode": "TWR",
                                                          "since": 1.0}))
    plugged[:] = DONGLES[:2]
    app = make()
    assert app.assign == {"v4": "FG1"}                              # FG1 goes to the next free dongle
    assert "sdr: V3 #2 (00000001) in use by shortfinal (TWR), not auto-assigned" in logs
    (claims.DIR / "adsb.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getppid(), "mode": "ADS-B",
                                                      "since": 1.0}))
    plugged.append(DONGLES[2])                                     # hot-plugged, but shortfinal has it
    assert app._sdr_arrivals() == [] and app.assign.get("adsb", "Off") == "Off"
    assert json.loads((claims.DIR / "00000001.json").read_text())["app"] == "shortfinal"   # untouched


def test_auto_job_that_fails_to_start_is_not_retried_or_moved(rig, monkeypatch):
    plugged, logs, make = rig
    app = make()
    real = claims.claim
    monkeypatch.setattr(claims, "claim", lambda serial, mode: False)   # taken between the check and the start
    plugged.append(DONGLES[0])
    assert app._sdr_arrivals() == [] and "00000001" not in app.assign
    assert "sdr: V3 #2 (00000001) → FG1 (auto) didn't start: left Off" in logs
    monkeypatch.setattr(claims, "claim", real)
    plugged.clear()
    app._sdr_arrivals()
    plugged.append(DONGLES[0])                                     # replugged: not tried again this run
    assert app._sdr_arrivals() == [] and "00000001" not in app.assign


def test_auto_job_that_keeps_failing_is_logged_once_and_left(rig):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:2]
    app = make()
    fg = app._receiver("FG1")
    fg.status = lambda: {"label": "FG1", "state": "down", "restarts": 5, "detail": "dongle 'V3 #2' busy"}
    app._sdr_arrivals()
    app._sdr_arrivals()
    failing = [ln for ln in logs if "keeps failing" in ln]
    assert failing == ["sdr: V3 #2 (00000001) → FG1 (auto) keeps failing (dongle 'V3 #2' busy); "
                       "left as is: pick its job in the web app"]
    assert app.assign == {"00000001": "FG1", "v4": P25}             # nothing moved


def test_legacy_sdr_modes_without_provenance(rig, tmp_path):
    plugged, logs, make = rig
    (tmp_path / "sdr_modes.json").write_text(json.dumps({"00000001": "Off", "v4": "FG1"}))   # before _set_by
    plugged[:] = DONGLES[:2]
    app = make()
    assert jobs(app) == {"00000001": (P25, "auto"), "v4": ("FG1", None)}   # its Off was only ever a default
    assert saved(tmp_path) == {"00000001": P25, "v4": "FG1", "_set_by": {"00000001": "auto"}}


def test_auto_assign_off_keeps_new_dongles_off(rig, tmp_path):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:2]
    app = make(sdr_auto_assign=False)
    assert app.assign == {} and jobs(app) == {"00000001": ("Off", None), "v4": ("Off", None)}
    plugged.append(DONGLES[2])
    assert app._sdr_arrivals() == [] and "adsb" not in app.assign
    assert not any(t.name == "sdr-watch" for t in __import__("threading").enumerate() if t.is_alive())
    assert not any(ln.startswith("sdr:") for ln in logs)
    assert app.set_mode("FG1", "v4") is None and app.assign == {"v4": "FG1"}


def test_no_reshuffle_when_a_dongle_goes_away(rig):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:3]
    app = make()
    before = dict(app.assign)
    assert before == {"00000001": "FG1", "v4": P25, "adsb": "South 2"}
    plugged.pop(0)                                                 # FG1's dongle unplugged
    assert app._sdr_arrivals() == [] and app.assign == before      # nobody else picks up FG1
    assert jobs(app)["00000001"] == ("FG1", "auto")                # shown, unplugged, still FG1
    plugged.append(DONGLES[3])                                     # a new one: FG1 still counts as covered
    assert app._sdr_arrivals() == [] and app.assign.get("spare", "Off") == "Off"
    plugged.insert(0, DONGLES[0])                                  # back: it keeps FG1
    assert app._sdr_arrivals() == [] and app.assign == before


def test_p25_is_skipped_when_it_cannot_run_on_one_dongle(rig, monkeypatch):
    plugged, logs, make = rig
    monkeypatch.setattr(TrunkRecorder, "one_dongle", lambda self: (False, "trunk-recorder not installed (x)"))
    plugged[:] = DONGLES[:3]
    app = make()
    assert app.assign == {"00000001": "FG1", "v4": "South 2"}       # the third stays Off
    assert logs.count("sdr: auto-assign skips Fire/EMS 15: trunk-recorder not installed (x)") == 1


def test_auto_assign_order_from_the_config(rig):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:2]
    app = make(sdr_auto_order=("South 2", "FG1"))
    assert app.assign == {"00000001": "South 2", "v4": "FG1"}


def test_trunk_recorder_one_dongle_check(tmp_path):
    exe = tmp_path / "trunk-recorder"
    r = TrunkRecorder(f"{exe} --config=config.json", str(tmp_path))
    assert r.one_dongle() == (False, f"trunk-recorder not installed ({exe})")
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    shipped = json.loads((appmod.ROOT / "deploy" / "trunk-recorder" / "config.json").read_text())
    (tmp_path / "config.json").write_text(json.dumps(shipped))
    assert r.one_dongle() == (True, "")                            # the shipped config: one source
    src = shipped["sources"][0]
    lo, hi = src["center"] - src["rate"] / 2, src["center"] + src["rate"] / 2
    assert all(lo < f < hi for f in shipped["systems"][0]["control_channels"])
    shipped["sources"].append(dict(src, center=478000000))
    (tmp_path / "config.json").write_text(json.dumps(shipped))
    assert r.one_dongle() == (False, "trunk-recorder's config.json has 2 sources: one dongle can't run it")


def test_config_auto_assign_defaults_on_and_reads_the_order(tmp_path):
    base = ('[radio]\nsdrs = "auto"\n[[radio.channels]]\nlabel = "South 2"\nmhz = 470.2\nkind = "paging"\n'
            '[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\n'
            '[p25]\nrecorder = "trunk-recorder --config=config.json"\n')
    p = tmp_path / "c.toml"
    p.write_text(base)
    c = load(p)
    assert c.sdr_auto_assign is True and c.sdr_auto_order == ("FG1", P25, "South 2")
    p.write_text(base.replace('sdrs = "auto"\n', 'sdrs = "auto"\nauto_assign = false\n'
                              'auto_assign_order = ["south2", "p25", "fg1", "FG1"]\n'))
    c = load(p)
    assert c.sdr_auto_assign is False and c.sdr_auto_order == ("South 2", P25, "FG1")
    for bad in ('auto_assign_order = ["fg2"]', 'auto_assign_order = "fg1"', 'auto_assign = "yes"'):
        p.write_text(base.replace('sdrs = "auto"\n', f'sdrs = "auto"\n{bad}\n'))
        with pytest.raises(ConfigError, match="radio.auto_assign"):
            load(p)


def test_station_config_turns_it_on(tmp_path, monkeypatch):
    from tests.test_station_install import station_config
    c = station_config(tmp_path, monkeypatch)
    assert c.sdr_auto and c.sdr_auto_assign and c.sdr_auto_order == ("FG1", P25, "South 2")


def test_the_hub_shows_who_set_each_dongle(tmp_path):
    """The station's sdr rows reach the hub's monitor as sent: "set_by" survives; an older station
    without it shows no "auto" (the page treats a missing set_by as unknown)."""
    from tests.test_cloud import TOKEN, HubRun, call, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        rows = [{"serial": "00000001", "name": "V3 #2", "mode": "FG1", "present": True, "set_by": "auto", "held_by": None},
                {"serial": "v4", "name": "V4", "mode": "Off", "present": True, "set_by": "person", "held_by": None},
                {"serial": "adsb", "name": "V3", "mode": "South 2", "present": True, "held_by": None}]
        status = {"radios": [], "sdr": {"shared": True, "mode": "FG1", "modes": ["South 2", "FG1", P25, "Off"],
                                        "switching": False, "sdrs": rows}}
        assert call(hub.url + "/api/station/state", "POST", {"at": 0, "status": status}, token=TOKEN)[0] == 200
        from tests.test_app import wait
        assert wait(lambda: (hub.get("/api/state")["sdr"] or {}).get("sdrs"))   # the page's state is pushed each second
        got = {x["serial"]: x.get("set_by") for x in hub.get("/api/state")["sdr"]["sdrs"]}
        assert got == {"00000001": "auto", "v4": "person", "adsb": None}
    finally:
        hub.close()
