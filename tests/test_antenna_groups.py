"""Antenna groups (sizeup/radio/groups.py): dongles on one antenna feed locked to the jobs it carries, by priority.

The station as it is cabled: a UHF feed (the county's P25 Fire/EMS 15, South 2 paging, FG1) on three dongles, a
700/800 MHz feed on two for MRRS (the system as shipped, deploy/trunk-recorder/systems/mrrs: two sources, one job
each), and dongles on no group (FG2 here). Fakes only, as tests/test_sdr_auto_assign.py: the USB list is a Python list,
rtl_fm and trunk-recorder don't exist, claims live in a temp dir (conftest.py). Serials and names are made up."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

import sizeup.app as appmod
import sizeup.radio.claims as claims
from sizeup.config import ChannelCfg
from sizeup.radio import groups as antenna
from sizeup.radio import systems
from sizeup.radio.recorder import TrunkRecorder
from tests.test_app import cfg as app_cfg
from tests.test_p25_systems import MRRS, system_dir

P25 = "Fire/EMS 15"
UHF = ["uhf-a", "uhf-b", "uhf-c"]
M700 = ["m700-a", "m700-b"]
DONGLES = [(0, "RTL2838UHIDIR", "uhf-a"), (1, "Blog V4", "uhf-b"), (2, "RTL2838UHIDIR", "uhf-c"),
           (3, "RTL2838UHIDIR", "m700-a"), (4, "RTL2838UHIDIR", "m700-b"), (5, "RTL2838UHIDIR", "spare-1")]
GROUPS = [{"name": "UHF", "sdrs": UHF, "jobs": [P25, "South 2", "FG1"]},
          {"name": "700", "sdrs": M700, "jobs": ["MRRS 770", "MRRS 773"]}]


# -- the plan alone ----------------------------------------------------------------------------------------------------

G = antenna.Group("UHF", ("a", "b", "c"), (P25, "South 2", "FG1", "FG2"))
OWN = {"a": P25, "b": "South 2", "c": "FG1"}
FIRE = {P25, "South 2", "FG1", "FG2"}.__contains__


def test_every_dongle_there_runs_the_admins_mapping_as_it_is():
    assert antenna.plan(G, OWN, ["a", "b", "c"]) == (OWN, [])
    # the mapping wins over the order while nothing is missing: FG1 picked above South 2's place stays
    assert antenna.plan(G, {"a": "FG1", "b": P25, "c": "FG2"}, ["a", "b", "c"]) == (
        {"a": "FG1", "b": P25, "c": "FG2"}, [])


@pytest.mark.parametrize("gone, want, short", [
    ("c", {"a": P25, "b": "South 2"}, ["FG1"]),             # the lowest gone: nothing moves
    ("b", {"a": P25, "c": "South 2"}, ["FG1"]),             # South 2's dongle gone: FG1 gives its dongle up
    ("a", {"b": "South 2", "c": P25}, ["FG1"]),             # the county's: FG1's dongle runs it
])
def test_one_dongle_gone_the_rest_run_the_highest_jobs(gone, want, short):
    usable = [s for s in "abc" if s != gone]
    assert antenna.plan(G, OWN, usable, fire=FIRE) == (want, short)


def test_two_gone_one_left_runs_the_highest_and_a_spare_stands_in_first():
    assert antenna.plan(G, OWN, ["c"]) == ({"c": P25}, ["South 2", "FG1"])
    assert antenna.plan(G, OWN, []) == ({}, [P25, "South 2", "FG1"])
    g4 = antenna.Group("UHF", ("a", "b", "c", "d"), G.jobs)
    # d has no job of its own: it takes South 2 and FG1 keeps running
    assert antenna.plan(g4, OWN, ["a", "c", "d"], spares=["d"]) == ({"a": P25, "c": "FG1", "d": "South 2"}, [])
    # ... but not when it isn't a spare (a person switched it Off: handed to another app)
    assert antenna.plan(g4, OWN, ["a", "c", "d"]) == ({"a": P25, "c": "South 2", "d": "Off"}, ["FG1"])


def test_a_stand_in_already_running_the_job_keeps_it():
    g4 = antenna.Group("UHF", ("a", "b", "c", "d", "e"), G.jobs)
    got, _ = antenna.plan(g4, OWN, ["a", "c", "d", "e"], spares=["d", "e"], current={"e": "South 2"})
    assert got["e"] == "South 2" and got["d"] == "Off"


def test_a_fire_job_never_gives_its_dongle_to_a_job_that_isnt_one():
    g = antenna.Group("mixed", ("a", "b"), ("MRRS 770", P25))
    own = {"a": "MRRS 770", "b": P25}
    assert antenna.plan(g, own, ["b"], fire=FIRE) == ({"b": P25}, ["MRRS 770"])
    assert antenna.plan(g, own, ["a"], fire=FIRE) == ({"a": "MRRS 770"}, [P25])       # (nothing lower to take)


def test_the_countys_dongle_is_taken_only_for_a_higher_job_of_its_own_group():
    own = {"a": "South 2", "b": P25}
    low = antenna.Group("UHF", ("a", "b"), (P25, "South 2"))
    assert antenna.plan(low, own, ["b"], fire=FIRE) == ({"b": P25}, ["South 2"])       # the county's is higher
    high = antenna.Group("UHF", ("a", "b"), ("South 2", P25))
    assert antenna.plan(high, own, ["b"], fire=FIRE) == ({"b": "South 2"}, [P25])      # the admin put paging first


def test_a_job_that_cant_run_on_what_it_would_have_is_left():
    g = antenna.Group("700", ("a", "b"), ("MRRS 770", "MRRS 773"))
    own = {"a": "MRRS 770", "b": "MRRS 773"}
    never = lambda job, on: job != "MRRS 770"                    # noqa: E731 - as if 770 alone covered no control channel
    assert antenna.plan(g, own, ["b"], runnable=never) == ({"b": "MRRS 773"}, ["MRRS 770"])


def test_each_side_of_mrrs_alone_covers_control_channels_of_its_own():
    # the shipped system: one dongle per side; either alone still decodes a site (reduced coverage)
    assert MRRS.jobs == ("MRRS 770", "MRRS 773")
    assert len(MRRS.covered([0])) == 4 and len(MRRS.covered([1])) == 3


@pytest.mark.parametrize("raw, why", [
    ({"name": "UHF"}, "a list"),
    ([{"name": "", "sdrs": ["a"], "jobs": ["FG1"]}], "group name"),
    ([{"name": "UHF", "sdrs": [], "jobs": ["FG1"]}], "1-16 dongles"),
    ([{"name": "UHF", "sdrs": ["a"], "jobs": []}], "at least one job"),
    ([{"name": "UHF", "sdrs": ["a"], "jobs": ["Off"]}], "at least one job"),
    ([{"name": "UHF", "sdrs": ["a", "a"], "jobs": ["FG1"]}], "listed twice"),
    ([{"name": "UHF", "sdrs": ["a"], "jobs": ["FG1"]}, {"name": "uhf", "sdrs": ["b"], "jobs": ["FG2"]}], "two groups"),
    ([{"name": "A", "sdrs": ["a"], "jobs": ["FG1"]}, {"name": "B", "sdrs": ["a"], "jobs": ["FG2"]}], "one group each"),
    ([{"name": "A", "sdrs": ["a"], "jobs": ["FG1"]}, {"name": "B", "sdrs": ["b"], "jobs": ["FG1"]}], "one group each"),
])
def test_parse_refuses_what_cant_be_a_group(raw, why):
    got, err = antenna.parse(raw)
    assert got == [] and why in err


# -- the station ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def rig(tmp_path, monkeypatch):
    """(plugged list to edit, log lines, make(**cfg changes) -> App, not started). Every app is stopped."""
    d = system_dir(tmp_path)
    plugged: list[tuple[int, str, str]] = list(DONGLES)
    logs: list[str] = []
    apps: list = []
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    monkeypatch.setattr(appmod, "SDR_WATCH_S", 3600.0)          # tests look for themselves
    monkeypatch.setattr(appmod, "SDR_SETTLE_S", 0.0)
    monkeypatch.setattr(appmod, "GROUP_HOLD_S", 0.0)            # (the hold has its own test)
    monkeypatch.setattr(appmod, "log", logs.append)
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    monkeypatch.setattr(appmod.sdr_lookup, "resolve", lambda serial, *a: next((i for i, _p, s in plugged if s == serial), None))
    monkeypatch.setattr(TrunkRecorder, "one_dongle", lambda self: (True, ""))
    (tmp_path / "clips").mkdir(exist_ok=True)
    system = systems.discover(d.parent.parent, {307})[0]
    base = replace(app_cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"),
                   channels=(ChannelCfg("South 2", 470.2, "paging"), ChannelCfg("FG1", 453.0375, "traffic"),
                             ChannelCfg("FG2", 458.0375, "traffic")),
                   sdr_auto=True, sdr_auto_assign=True, sdr_names={"uhf-a": "V3 #1", "uhf-b": "V4", "uhf-c": "V3 #3"},
                   p25_dir=str(tmp_path / "captures" / "p25"), p25_talkgroups={307: P25},
                   p25_recorder=str(tmp_path / "no-trunk-recorder") + " --config=config.json",
                   p25_recorder_dir=str(d.parent.parent), p25_systems=(system,))

    def make(**kw):
        app = appmod.App(replace(base, **kw))
        apps.append(app)
        return app

    yield plugged, logs, make
    for a in apps:
        a.stop()


def mapped(app) -> None:
    """The admin's mapping on the station today: UHF a/b/c = Fire/EMS 15, South 2, FG1; MRRS 770/773; FG2 elsewhere."""
    for job, sd in ((P25, "uhf-a"), ("South 2", "uhf-b"), ("FG1", "uhf-c"), ("MRRS 770", "m700-a"),
                    ("MRRS 773", "m700-b"), ("FG2", "spare-1")):
        assert app.set_mode(job, sd, confirm=True) is None, job


def on(app, job):
    return app._program_on(job)


def grouped(make, **kw):
    app = make(**kw).start()
    mapped(app)
    assert app.set_antenna_groups(GROUPS) is None
    return app


def test_creating_groups_moves_nothing_and_survives_a_restart(rig, tmp_path):
    plugged, logs, make = rig
    app = make().start()
    mapped(app)
    before = dict(app.assign)
    assert app.set_antenna_groups(GROUPS) is None
    assert app.assign == before and {j: on(app, j) for j in before.values()} == {m: s for s, m in before.items()}
    assert "sdr: antenna groups: UHF: SDR V3 #1, V4, V3 #3 · Fire/EMS 15 > South 2 > FG1; 700: SDR m700-a, m700-b · " \
           "MRRS 770 > MRRS 773" in logs
    saved = json.loads((tmp_path / "antenna_groups.json").read_text())
    assert saved["groups"] == GROUPS
    rows = {r["serial"]: r for r in app.sdr()["sdrs"]}
    assert rows["uhf-b"]["group"] == "UHF" and "group" not in rows["spare-1"]
    assert app.sdr()["groups"][0] == {**GROUPS[0], "usable": UHF, "short": [], "unowned": [], "conflicts": {}}
    app.stop()
    again = make().start()
    assert [g.row() for g in again.groups] == GROUPS and again.assign == before
    assert again.set_antenna_groups([]) is None and again.groups == [] and again.sdr()["groups"] == []


def test_a_grouped_dongle_runs_only_its_groups_jobs_and_a_groups_job_only_there(rig):
    plugged, logs, make = rig
    app = grouped(make)
    assert app.set_mode("FG2", "uhf-c") == "SDR V3 #3 is locked to UHF: Fire/EMS 15, South 2, FG1"
    assert app.set_mode("MRRS 770", "uhf-a", confirm=True).startswith("SDR V3 #1 is locked to UHF")
    assert app.set_mode("FG1", "spare-1") == "FG1 is locked to UHF: SDR V3 #1, V4, V3 #3"
    assert app.set_mode("MRRS 773", "spare-1", confirm=True) == "MRRS 773 is locked to 700: SDR m700-a, m700-b"
    assert app.assign["uhf-c"] == "FG1" and app.assign["spare-1"] == "FG2"           # nothing moved
    # within the group it is a job pick as anywhere: FG1 moves to V4, whose South 2 ... is V3 #3's job no more
    assert app.set_mode("FG1", "uhf-b") is None
    assert (app.assign["uhf-b"], app.assign["uhf-c"], on(app, "FG1")) == ("FG1", "Off", "uhf-b")
    assert "SDR V3 #3: FG1 -> Off (moving to V4)" in logs
    assert app.set_mode("Off", "uhf-a") is None and on(app, P25) is None              # Off is always allowed


def test_saving_groups_that_dont_fit_what_runs_is_refused(rig, tmp_path):
    plugged, logs, make = rig
    (tmp_path / "state").mkdir()
    app = make(cad_db=str(tmp_path / "state" / "cad.sqlite")).start()
    mapped(app)
    bad = [{"name": "UHF", "sdrs": UHF, "jobs": [P25, "South 2"]}]
    assert app.set_antenna_groups(bad) == "SDR V3 #3 runs FG1: add FG1 to UHF, or switch SDR V3 #3 first"
    bad = [{"name": "UHF", "sdrs": ["uhf-a", "uhf-b"], "jobs": [P25, "South 2", "FG1"]}]
    assert app.set_antenna_groups(bad) == "FG1 runs on SDR V3 #3: add it to UHF, or switch it first"
    assert app.set_antenna_groups([{"name": "X", "sdrs": ["nope"], "jobs": ["FG2"]}]) == "X: no SDR 'nope' here"
    assert app.set_antenna_groups([{"name": "X", "sdrs": ["spare-1"], "jobs": ["HPN"]}]) == "X: no job 'HPN' here"
    assert app.groups == [] and not any("antenna groups:" in x for x in logs)
    assert app.set_mode("Off", "spare-1") is None
    assert app.set_adsb_receiver("spare-1") is None                                     # reserved for ADS-B now
    assert "reserved" in app.set_antenna_groups([{"name": "X", "sdrs": ["spare-1"], "jobs": ["FG2"]}])
    assert app.set_antenna_groups(GROUPS) is None
    assert "in antenna group UHF" in app.set_adsb_receiver("uhf-a")


def test_unplugged_the_rest_run_the_higher_jobs_and_the_mapping_comes_back(rig, tmp_path):
    plugged, logs, make = rig
    app = grouped(make)
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]                   # South 2's dongle unplugged
    app._sdr_arrivals()
    assert (on(app, "South 2"), on(app, P25), on(app, "FG1")) == ("uhf-c", "uhf-a", None)
    assert app.assign["uhf-c"] == "South 2" and app._own == {"uhf-c": "FG1"}
    assert "sdr: group UHF: short of dongles (SDR V4): FG1 not running" in logs
    assert "sdr: group UHF: SDR V3 #3: FG1 -> South 2 (for SDR V4; its own FG1 until then)" in logs
    # saved as the admin left it: a restart (or a switch back to an older release) starts the mapping
    saved = json.loads((tmp_path / "sdr_modes.json").read_text())
    assert (saved["uhf-b"], saved["uhf-c"]) == ("South 2", "FG1")
    rows = {r["serial"]: r for r in app.sdr()["sdrs"]}
    assert rows["uhf-c"]["own"] == "FG1" and rows["uhf-c"]["for"] == ["uhf-b"] and rows["uhf-b"]["on"] == "uhf-c"
    assert app.sdr()["groups"][0]["short"] == ["FG1"] and app.sdr()["groups"][0]["usable"] == ["uhf-a", "uhf-c"]
    st = {r["label"]: r["state"] for r in app.radio_status()}
    assert st["FG1"] == "off" and st["South 2"] != "off"
    app._sdr_arrivals()                                                    # looked at again: nothing more moves
    assert app.assign["uhf-c"] == "South 2" and logs.count("sdr: group UHF: SDR V3 #3: FG1 -> South 2 "
                                                           "(for SDR V4; its own FG1 until then)") == 1
    plugged.insert(1, DONGLES[1])                                          # back
    app._sdr_arrivals()
    assert (on(app, "South 2"), on(app, "FG1"), on(app, P25)) == ("uhf-b", "uhf-c", "uhf-a")
    assert app._own == {} and app.assign["uhf-c"] == "FG1" and app.sdr()["groups"][0]["short"] == []
    assert "sdr: group UHF: SDR V3 #3: South 2 -> FG1 (its own job again)" in logs
    assert "sdr: group UHF: every job running" in logs


def test_the_countys_dongle_gone_its_recorder_moves_inside_the_group_only(rig):
    plugged, logs, make = rig
    app = grouped(make)
    plugged[:] = [d for d in plugged if d[2] != "uhf-a"]
    app._sdr_arrivals()
    assert on(app, P25) == "uhf-c" and on(app, "South 2") == "uhf-b" and on(app, "FG1") is None
    assert on(app, "FG2") == "spare-1" and on(app, "MRRS 770") == "m700-a"           # never outside the group
    plugged.insert(0, DONGLES[0])
    app._sdr_arrivals()
    assert (on(app, P25), on(app, "FG1")) == ("uhf-a", "uhf-c")


def test_mrrs_on_one_dongle_runs_its_higher_side_and_the_county_is_never_touched(rig):
    plugged, logs, make = rig
    app = grouped(make)
    rt = app.systems["mrrs"]
    plugged[:] = [d for d in plugged if d[2] != "m700-a"]
    app._sdr_arrivals()
    assert (rt.recorder.source_on(0), rt.recorder.source_on(1)) == ("m700-b", None)      # 770 side on what is left
    assert app.sdr()["groups"][1]["short"] == ["MRRS 773"] and on(app, P25) == "uhf-a"
    plugged[:] = [d for d in plugged if d[2] != "m700-b"]                                # both gone
    app._sdr_arrivals()
    assert rt.recorder.source_on(0) is None and rt.recorder.source_on(1) is None
    assert app._own == {} and (app.assign["m700-a"], app.assign["m700-b"]) == ("MRRS 770", "MRRS 773")
    plugged[:] = list(DONGLES)
    app._sdr_arrivals()
    assert (rt.recorder.source_on(0), rt.recorder.source_on(1)) == ("m700-a", "m700-b")
    assert app.state()["p25_systems"][0]["jobs"][0]["sdr"] == "m700-a"


def test_ungrouped_dongles_and_jobs_behave_as_before_beside_groups(rig):
    plugged, logs, make = rig
    app = grouped(make)
    plugged[:] = [d for d in plugged if d[2] != "spare-1"]                # FG2's dongle (no group) unplugged
    app._sdr_arrivals()
    assert app.assign["spare-1"] == "FG2" and not app._own                 # unplugging moves nothing, as always
    plugged.append((6, "RTL2838UHIDIR", "spare-2"))                       # a new dongle: auto-assign as before ...
    assert app._sdr_arrivals() == ["FG2"]                                  # ... FG2's dongle is gone: it takes FG2
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]                  # a group short of a dongle
    app._sdr_arrivals()
    plugged.append((7, "RTL2838UHIDIR", "spare-3"))                       # never given the group's missing job
    assert app._sdr_arrivals() == [] and app.assign.get("spare-3", "Off") == "Off"
    assert "sdr: spare-3 → Off (auto: FG1, Fire/EMS 15, South 2, FG2 already covered)" in logs


def test_a_new_dongle_in_a_group_gets_the_groups_free_job_and_stands_in_as_a_spare(rig):
    plugged, logs, make = rig
    groups = [{"name": "UHF", "sdrs": [*UHF, "spare-1"], "jobs": [P25, "South 2", "FG1", "FG2"]}]
    app = make().start()
    for job, sd in ((P25, "uhf-a"), ("South 2", "uhf-b"), ("FG1", "uhf-c"), ("Off", "spare-1"), ("Off", "m700-a"),
                    ("Off", "m700-b")):
        assert app.set_mode(job, sd) is None
    app.set_by.pop("spare-1")                                             # an Off nobody picked (a new dongle)
    assert app.set_antenna_groups(groups) is None
    assert app.assign.get("spare-1", "Off") == "Off"                      # (auto-assign looks at arrivals only)
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    assert on(app, "South 2") == "spare-1" and on(app, "FG1") == "uhf-c"  # the spare stands in: FG1 keeps running
    assert app._own == {"spare-1": "Off"} and app.sdr()["groups"][0]["short"] == []
    plugged.insert(1, DONGLES[1])
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-b" and app.assign.get("spare-1") == "Off" and not app._own
    assert app.set_mode("Off", "spare-1") is None                         # a person's Off: never a spare
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-c" and app.assign.get("spare-1", "Off") == "Off"
    app.stop()
    plugged[:] = list(DONGLES)
    fresh = make()
    fresh.set_by.clear()
    fresh.assign["spare-1"] = "Off"
    fresh.start()                                                          # an Off spare arriving: the group's free job
    assert fresh.assign["spare-1"] == "FG2" and fresh.set_by["spare-1"] == "auto"


def test_a_person_picking_during_a_shortage_sets_the_mapping_and_the_plan_runs_it(rig):
    plugged, logs, make = rig
    app = grouped(make)
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    assert app.assign["uhf-c"] == "South 2"
    assert app.set_mode("Off", "uhf-b") is None                            # the gone dongle's job given up
    assert on(app, "FG1") == "uhf-c" and not app._own and app.assign["uhf-b"] == "Off"
    assert app.sdr()["groups"][0]["short"] == []


def test_a_short_glitch_moves_nothing(rig, monkeypatch):
    plugged, logs, make = rig
    monkeypatch.setattr(appmod, "GROUP_HOLD_S", 3600.0)
    app = grouped(make)
    app._sdr_arrivals()
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    plugged.insert(1, DONGLES[1])
    app._sdr_arrivals()
    assert app.assign["uhf-c"] == "FG1" and not app._own and on(app, "South 2") == "uhf-b"


def test_another_apps_dongle_is_never_taken_and_its_job_stands_in_elsewhere(rig):
    plugged, logs, make = rig
    app = grouped(make)
    assert app.set_mode("Off", "uhf-b") is None                            # a person hands V4 to shortfinal ...
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "uhf-b.json").write_text(json.dumps({"app": "shortfinal", "pid": 1, "mode": "scan", "since": 0}))
    app._sdr_arrivals()
    assert app.assign["uhf-b"] == "Off" and on(app, "FG1") == "uhf-c" and not app._own   # ... and nothing moves


def test_the_hub_sends_groups_to_the_station_and_audits_them(tmp_path):
    from sizeup.cad.api import needs, summarize
    from sizeup.cloud.station import CALLS
    assert "set_antenna_groups" in CALLS and needs("/api/antenna-groups") == "admin"
    s, _ = summarize("/api/antenna-groups", {"groups": GROUPS}, {}, None)
    assert s == "Antenna groups: UHF (3 SDRs: Fire/EMS 15 > South 2 > FG1); 700 (2 SDRs: MRRS 770 > MRRS 773)"
    assert summarize("/api/antenna-groups", {"groups": []}, {}, None)[0] == "Antenna groups: none"
    from sizeup.cloud.hub import HubApp
    assert HubApp.set_antenna_groups.__doc__


def test_health_says_a_group_is_short_and_where_its_job_runs():
    from sizeup.cloud.health import JOB_GRACE_S
    from tests.test_health import Rig, station_status
    r = Rig()
    st = station_status()
    # the station's report (App.sdr()) while South 2's dongle is gone: its stand-in runs South 2, FG1 doesn't run
    st["sdr"]["sdrs"][0].update(present=False, group="UHF", on="00000102")
    st["sdr"]["sdrs"][1].update(mode="South 2", group="UHF", own="FG1", **{"for": ["00000101"]})
    st["sdr"]["sdrs"][2].update(group="UHF")
    st["sdr"]["groups"] = [{"name": "UHF", "sdrs": ["00000101", "00000102", "00000103"],
                            "jobs": ["Fire/EMS 15", "South 2", "FG1"], "usable": ["00000102", "00000103"],
                            "short": ["FG1"]}]
    r.at(0, status=st)
    m = r.h.model()
    assert m["groups"] == [{"name": "UHF", "sdrs": ["101", "102", "103"], "usable": 2,
                            "jobs": ["Fire/EMS 15", "South 2", "FG1"], "short": ["FG1"], "unowned": [],
                            "parked": [], "conflicts": {}}]
    assert [(d["name"], d["group"], d["own"], d["on"]) for d in m["dongles"]] == [
        ("101", "UHF", "", "00000102"), ("102", "UHF", "FG1", ""), ("103", "UHF", "", "")]
    (a,) = r.h.alerts()
    assert a["key"] == "sdr:00000101" and a["text"] == ("dongle 101 (00000101) unplugged or not found · South 2 on SDR 102 "
                                                        "(UHF)")
    r.at(JOB_GRACE_S + 1, status=st)
    g = next(a for a in r.h.alerts() if a["key"] == "group:UHF")
    assert (g["severity"], g["title"], g["text"]) == ("warning", "Antenna group UHF short", "2 of 3 SDRs · not running: FG1")
    assert g["who"]["channel"] == "FG1"
    r.at(JOB_GRACE_S + 10, status=station_status())                          # back: both clear
    assert r.keys() == []


def test_the_station_page_posts_groups_and_gets_the_reason_when_refused(rig, monkeypatch):
    from tests.test_p25_systems import _get, _post, _serve
    plugged, logs, make = rig
    app = make().start()
    mapped(app)
    server, url = _serve(app, monkeypatch, access=False)
    try:
        code, body = _post(url + "/api/antenna-groups", {"groups": [{"name": "UHF", "sdrs": UHF, "jobs": [P25]}]})
        assert code == 400 and json.loads(body)["error"].startswith("SDR V4 runs South 2: add South 2 to UHF")
        assert _post(url + "/api/antenna-groups", {"groups": "UHF"})[0] == 400
        assert _post(url + "/api/antenna-groups", {"groups": GROUPS}) == (200, b'{"ok":true}')
        assert [g["name"] for g in app.state()["sdr"]["groups"]] == ["UHF", "700"]
        code, st = _get(url + "/api/state")
        assert code == 200 and "sdr" in st
    finally:
        server.shutdown()


def test_auto_assign_off_a_person_moving_a_job_during_a_shortage_leaves_no_dongle_idle(rig):
    plugged, logs, make = rig
    app = grouped(make, sdr_auto_assign=False)
    plugged[:] = [d for d in plugged if d[2] != "uhf-a"]                  # the county's dongle gone
    app._sdr_arrivals()
    assert on(app, P25) == "uhf-c" and on(app, "South 2") == "uhf-b"
    assert app.set_mode("FG1", "uhf-b") is None                           # FG1 is V4's now; V3 #3's own is Off
    assert {j: on(app, j) for j in (P25, "South 2", "FG1")} in (
        {P25: "uhf-c", "South 2": None, "FG1": None}, {P25: "uhf-b", "South 2": None, "FG1": None},
        {P25: "uhf-c", "South 2": None, "FG1": "uhf-b"}, {P25: "uhf-b", "South 2": None, "FG1": "uhf-c"})
    assert sum(1 for s in ("uhf-b", "uhf-c") if app.assign.get(s, "Off") != "Off") == 2   # both run something


def test_a_group_deleted_during_a_shortage_its_job_waits_on_its_own_dongle_again(rig):
    plugged, logs, make = rig
    app = grouped(make, sdr_auto_assign=False)
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-c"
    assert app.set_antenna_groups([GROUPS[1]]) == "confirm: Saving stops South 2 (a dongle of its group is gone)."
    assert app.groups == [antenna.Group("UHF", tuple(UHF), (P25, "South 2", "FG1")), antenna.Group(
        "700", tuple(M700), ("MRRS 770", "MRRS 773"))]                     # nothing saved without the confirm
    assert app.set_antenna_groups([GROUPS[1]], confirm="Saving stops South 2 (a dongle of its group is gone).") is None
    assert on(app, "FG1") == "uhf-c" and on(app, "South 2") == "uhf-b" and not app._own   # waits for V4, as before
    assert "sdr: South 2 waits on SDR V4 again (no antenna group)" in logs


def test_picking_the_job_a_stand_in_runs_makes_it_its_own(rig):
    plugged, logs, make = rig
    app = grouped(make)
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    assert app.set_mode("South 2", "uhf-c") is None                       # moves South 2 here for good
    assert not app._own and app.assign["uhf-c"] == "South 2" and app.assign.get("uhf-b") == "Off"
    plugged.insert(1, DONGLES[1])
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-c" and app.assign.get("uhf-b") == "FG1"     # back: the group's free job (auto)


def test_a_reception_test_lends_a_grouped_dongle_only_to_its_groups_jobs(rig):
    plugged, logs, make = rig
    app = grouped(make)
    for sd in ("m700-a", "m700-b", "spare-1"):
        assert app.set_mode("Off", sd) is None
    err = app.reception.start({"system": "mrrs", "jobs": ["MRRS 773"], "sdr": "spare-1", "minutes": 15})
    assert err == "MRRS 773 is locked to 700: SDR m700-a, m700-b"
    # a stand-in is never lent (its rows: a fire job is "protected" first, another system's job "runs ..." first)
    assert app.set_mode("Off", "uhf-c") is None
    app._own["uhf-c"] = "FG1"                                  # (as if it stood in with no job of its own running)
    assert {r["serial"]: r for r in app.reception.dongles()}["uhf-c"]["refuse"] == "standing in (antenna group)"


# -- the safety review (#609): renamed dongles, boot, restarts, flapping, saves, order, shared serials -------------------

def test_sdr_serial_moves_a_renamed_dongle_in_its_antenna_group_too(tmp_path):
    from tests.test_sdr_serial import P25 as SP25, V3_2, V4, Station
    from sizeup.radio import sdrserial
    s = Station(tmp_path, [V4, V3_2])
    (s.state / "captures" / "sdr_modes.json").write_text(json.dumps({"00000001": SP25, "_set_by": {"00000001": "person"}}))
    groups = s.state / "captures" / "antenna_groups.json"
    groups.write_text(json.dumps({"groups": [{"name": "UHF", "sdrs": ["v4", "00000001"], "jobs": [SP25, "South 2"]}]}))
    assert sdrserial.main(["1", "v3-2", "--yes"], s.env) == 0, s.text()
    assert json.loads(groups.read_text())["groups"] == [{"name": "UHF", "sdrs": ["v4", "v3-2"], "jobs": [SP25, "South 2"]}]
    assert "antenna_groups.json: v3-2 is in antenna group UHF instead of 00000001" in s.text()


def test_a_fire_job_saved_against_the_groups_keeps_running_and_says_so(rig, tmp_path):
    plugged, logs, make = rig
    app = grouped(make)
    app.stop()
    # as an older sdr-serial left it: the county's job moved to the new serial, the group still names the old one
    modes = json.loads((tmp_path / "sdr_modes.json").read_text())
    modes["uhf-new"] = modes.pop("uhf-a")
    (tmp_path / "sdr_modes.json").write_text(json.dumps(modes))
    plugged[0] = (0, "RTL2838UHIDIR", "uhf-new")
    again = make().start()
    assert on(again, P25) == "uhf-new" and again.assign["uhf-new"] == P25        # never turned Off by a lock
    assert any("CRITICAL uhf-new runs Fire/EMS 15 against the antenna groups" in x for x in logs)
    again._sdr_arrivals()
    assert on(again, P25) == "uhf-new"                                              # and never planned away
    g = again.sdr()["groups"][0]
    assert g["conflicts"] == {"uhf-new": P25} and g["unowned"] == []
    assert "add it to UHF" in again.set_antenna_groups(GROUPS)                      # the fix: the group, by hand
    fixed = [{**GROUPS[0], "sdrs": ["uhf-new", "uhf-b", "uhf-c"]}, GROUPS[1]]
    assert again.set_antenna_groups(fixed) is None and again.sdr()["groups"][0]["conflicts"] == {}


def test_a_group_listing_a_job_no_dongle_has_is_shown(rig):
    plugged, logs, make = rig
    app = grouped(make)
    assert app.set_mode("Off", "uhf-c") is None                                    # FG1: nobody's now
    assert app.sdr()["groups"][0]["unowned"] == ["FG1"]


def test_a_dongle_attaching_late_at_boot_moves_nothing(rig, monkeypatch):
    plugged, logs, make = rig
    app = grouped(make)
    app.stop()
    monkeypatch.setattr(appmod, "GROUP_HOLD_S", 3600.0)
    plugged[:] = [d for d in DONGLES if d[2] != "uhf-a"]                    # the county's dongle not attached yet
    again = make().start()
    starts = []
    real = again._start_mode
    monkeypatch.setattr(again, "_start_mode", lambda m, s: starts.append((m, s)) or real(m, s))
    again._sdr_arrivals()
    plugged.insert(0, DONGLES[0])                                           # attached a few seconds later
    again._sdr_arrivals()
    assert not again._own and starts == [] and on(again, P25) == "uhf-a" and on(again, "FG1") == "uhf-c"


def test_a_restart_during_a_fallback_resumes_the_stand_in_and_never_gives_the_job_to_the_gone_dongle(rig, tmp_path):
    plugged, logs, make = rig
    app = grouped(make)
    plugged[:] = [d for d in plugged if d[2] != "uhf-a"]                  # the county's dongle gone
    app._sdr_arrivals()
    assert on(app, P25) == "uhf-c" and app._own == {"uhf-c": "FG1"}
    assert json.loads((tmp_path / antenna.STANDINS).read_text()) == {"uhf-c": P25}
    assert json.loads((tmp_path / "sdr_modes.json").read_text())["uhf-c"] == "FG1"   # an older release: the mapping
    app.stop()
    again = make().start()                                                  # an update: still gone
    assert on(again, P25) == "uhf-c" and again._own == {"uhf-c": "FG1"} and again.assign["uhf-a"] == P25
    assert "sdr: group UHF: SDR V3 #3 runs Fire/EMS 15 again for SDR V3 #1 (as when sizeup stopped)" in logs
    again._sdr_arrivals()
    assert on(again, P25) == "uhf-c" and on(again, "FG1") is None
    plugged.insert(0, DONGLES[0])                                           # back
    again._sdr_arrivals()
    assert on(again, P25) == "uhf-a" and on(again, "FG1") == "uhf-c" and not again._own
    assert not (tmp_path / antenna.STANDINS).exists()


def test_a_flapping_dongle_gets_its_job_back_only_after_staying_longer_each_time(rig, monkeypatch):
    plugged, logs, make = rig
    app = grouped(make)
    moves = []
    real = app._start_mode
    monkeypatch.setattr(app, "_start_mode", lambda m, s: (moves.append((m, s)) if m == P25 else None) or real(m, s))
    for _ in range(5):                                                      # gone, back, gone, back ...
        plugged[:] = [d for d in plugged if d[2] != "uhf-a"]
        app._sdr_arrivals()
        plugged.insert(0, DONGLES[0])
        app._sdr_arrivals()
    # once back at once (first return), then held 2 min: the county stays on its working stand-in
    assert moves == [(P25, "uhf-c"), (P25, "uhf-a"), (P25, "uhf-c")], moves
    assert on(app, P25) == "uhf-c" and app._gflap["uhf-a"][0] == 1
    assert any("gone again" in x and "stayed 120 s" in x for x in logs)


def test_the_county_given_another_dongle_while_it_waits_starts_at_once(tmp_path):
    from tests.test_p25_systems import ask_county, service
    county, _mrrs, pop, _logs, _d = service(tmp_path)
    ask_county(tmp_path, sdr="gone-one")
    assert county.step(100.0)["state"] == "absent"
    county.next_start = 100.0 + 15.0                                        # as trunk-recorder's "device gone" left it
    ask_county(tmp_path, sdr="pd-a", seq=2)                                 # a stand-in
    assert county.step(101.0)["state"] == "ok" and len(pop.started) == 1


def test_a_save_that_would_stop_a_running_fire_job_or_ranks_fire_jobs_wrong_is_asked_twice(rig):
    plugged, logs, make = rig
    app = grouped(make)
    swapped = [{**GROUPS[0], "jobs": ["South 2", P25, "FG1"]}, GROUPS[1]]
    why = app.set_antenna_groups(swapped)
    assert why == ("confirm: UHF ranks Fire/EMS 15 below South 2: in a fallback the higher one takes the other's "
                   "dongle.") and app.groups[0].jobs == (P25, "South 2", "FG1")
    assert app.set_antenna_groups(swapped, confirm=why[9:]) is None and app.groups[0].jobs == ("South 2", P25, "FG1")
    assert app.set_antenna_groups(GROUPS) is None
    plugged[:] = [d for d in plugged if d[2] != "uhf-a"]
    app._sdr_arrivals()
    out = [{**GROUPS[0], "sdrs": ["uhf-a", "uhf-b"]}, GROUPS[1]]          # the stand-in taken out of the group
    assert app.set_antenna_groups(out).startswith("FG1 runs on SDR V3 #3")  # (what it owns doesn't fit: refused)
    assert on(app, P25) == "uhf-c"
    # a new order in a shortage: South 2, running on the stand-in, would stop
    plugged.insert(0, DONGLES[0])
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-c"
    why = app.set_antenna_groups([{**GROUPS[0], "jobs": [P25, "FG1", "South 2"]}, GROUPS[1]])
    assert why.startswith("confirm: Saving stops South 2 (a dongle of its group is gone). UHF ranks South 2 below FG1")
    assert on(app, "South 2") == "uhf-c" and app.groups[0].jobs == (P25, "South 2", "FG1")


def test_a_shared_serial_leaves_a_running_program_where_it_is(rig, monkeypatch):
    plugged, logs, make = rig
    app = grouped(make)
    monkeypatch.setattr(app, "_job_state", lambda job: "ok")
    plugged.append((6, "RTL2838UHIDIR", "uhf-b"))                         # a second dongle on V4's serial (#242)
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-b" and not app._own                    # running: kept
    monkeypatch.setattr(app, "_job_state", lambda job: "down")
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-c" and app._own == {"uhf-c": "FG1"}    # can't run there: stood in for


def test_a_corrupt_groups_file_is_said_loudly_and_shown(rig, tmp_path):
    from tests.test_health import Rig, station_status
    plugged, logs, make = rig
    (tmp_path / "antenna_groups.json").write_text("{not json")
    app = make()
    assert app.groups == [] and app.sdr()["groups_problem"] == "antenna_groups.json unreadable (JSONDecodeError)"
    assert any(x.startswith("sdr: CRITICAL antenna groups dropped") for x in logs)
    r = Rig()
    st = station_status()
    st["sdr"]["groups_problem"] = app.sdr()["groups_problem"]
    r.at(0, status=st)
    (a,) = r.h.alerts()
    assert a["key"] == "group" and a["title"] == "Antenna groups dropped" and "every dongle unlocked" in a["text"]



# -- the re-review (#609 at ec0e5fd): a confirm for one ask only, stale stand-ins, a person's switch starts fresh --------

def test_a_confirm_is_for_the_ask_confirmed_and_a_changed_ask_comes_back(rig):
    plugged, logs, make = rig
    app = grouped(make)
    swapped = [{**GROUPS[0], "jobs": ["South 2", P25, "FG1"]}, GROUPS[1]]
    first = app.set_antenna_groups(swapped)[9:]
    assert first == "UHF ranks Fire/EMS 15 below South 2: in a fallback the higher one takes the other's dongle."
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]                   # meanwhile South 2's dongle drops
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-c"
    reorder = [{**GROUPS[0], "jobs": [P25, "FG1", "South 2"]}, GROUPS[1]]
    ask = app.set_antenna_groups(reorder)[9:]
    assert ask.startswith("Saving stops South 2")
    # the person confirmed an earlier, other ask: refused, with the ask as it is now
    assert app.set_antenna_groups(reorder, confirm="UHF ranks South 2 below FG1: in a fallback the higher one takes "
                                                   "the other's dongle.") == "confirm: " + ask
    assert app.set_antenna_groups(reorder, confirm=True) == "confirm: " + ask        # (no blanket yes)
    assert on(app, "South 2") == "uhf-c" and app.groups[0].jobs == (P25, "South 2", "FG1")
    assert app.set_antenna_groups(reorder, confirm=ask) is None and on(app, "South 2") is None
    assert f"sdr: antenna groups saved with a confirm: {ask}" in logs
    from sizeup.cad.api import summarize
    s, _ = summarize("/api/antenna-groups", {"groups": reorder, "confirm": ask}, {}, None)
    assert s.endswith(f"· confirmed: {ask}") and s.startswith("Antenna groups: UHF (3 SDRs: Fire/EMS 15 > FG1 > South 2)")


def test_the_station_page_sends_the_ask_back_word_for_word(rig, monkeypatch):
    from tests.test_p25_systems import _post, _serve
    plugged, logs, make = rig
    app = grouped(make)
    server, url = _serve(app, monkeypatch, access=False)
    try:
        swapped = [{**GROUPS[0], "jobs": ["South 2", P25, "FG1"]}, GROUPS[1]]
        code, body = _post(url + "/api/antenna-groups", {"groups": swapped})
        ask = json.loads(body)["error"]
        assert code == 400 and ask.startswith("confirm: UHF ranks")
        assert _post(url + "/api/antenna-groups", {"groups": swapped, "confirm": True})[0] == 400
        assert _post(url + "/api/antenna-groups", {"groups": swapped, "confirm": ask[9:]}) == (200, b'{"ok":true}')
    finally:
        server.shutdown()
    js = (appmod.ROOT / "sizeup" / "web" / "static" / "radio-controls.js").read_text()
    assert "postGroups(groups, ask)" in js and "{ confirm }" in js


def test_a_stale_stand_in_is_not_resumed_when_it_isnt_plugged_in(rig, tmp_path):
    plugged, logs, make = rig
    app = grouped(make)
    app.stop()
    (tmp_path / antenna.STANDINS).write_text(json.dumps({"uhf-c": P25}))    # it stood in for V3 #1 when sizeup stopped
    plugged[:] = [d for d in DONGLES if d[2] != "uhf-c"]                  # now the stand-in is gone, V3 #1 is back
    again = make().start()
    assert on(again, P25) == "uhf-a" and not again._own                     # at once, on its own dongle
    assert not any("runs Fire/EMS 15 again" in x for x in logs)


@pytest.mark.parametrize("group, held", [(False, False), (True, True)])
def test_a_persons_switch_starts_a_flapping_system_fresh_and_a_group_move_keeps_its_hold(tmp_path, group, held):
    from tests.test_p25_systems import ask_system, service
    base = tmp_path
    usb = [(0, "v4"), (1, "pd-a"), (2, "pd-b")]
    _county, mrrs, pop, _logs, _d = service(base, found=lambda usb=usb: list(usb))
    ask_system(base, {0: "pd-a", 1: "pd-b"})
    assert mrrs.step(100.0)["state"] == "ok"
    usb.remove((1, "pd-a"))
    for t in (106.0, 107.0, 108.0, 109.0):
        mrrs.step(t)
    mrrs.flaps, mrrs.hold_until = 2, 400.0                              # as a flapping dongle left it
    from pathlib import Path
    from sizeup.radio import trunkrecd
    p = Path(trunkrecd.system_state_path("mrrs", str(base / "state" / "p25_recorder.json")))
    ask_system(base, {0: "pd-b"}, seq=2)
    if group:                                                            # as SystemUnitRecorder writes a group move
        w = json.loads(p.read_text())
        p.write_text(json.dumps({**w, "group": True}))
    st = mrrs.step(120.0)
    assert (st["state"] == "absent" and "held back" in st["detail"]) == held, st
    assert mrrs.flaps == (2 if group else 0)
    if group:
        assert mrrs.step(401.0)["state"] == "ok" and len(pop.started) == 2


def test_the_stations_recorder_marks_a_group_move_in_the_systems_wish(rig, tmp_path):
    from sizeup.radio.recorder import SystemUnitRecorder
    plugged, logs, make = rig
    rec = SystemUnitRecorder("mrrs", "MRRS", "trunk-recorder --config=config.json", str(tmp_path),
                             state_path=str(tmp_path / "w.json"), status_path=str(tmp_path / "s.json"), wait_s=0.0)
    rec.sources = {0: (type("F", (), {"serial": "m700-b", "label": "x"})(), "MRRS 770")}
    rec._write_state(True)
    assert "group" not in json.loads((tmp_path / "w.json").read_text())
    rec.group_move = True
    rec._write_state(True)
    assert json.loads((tmp_path / "w.json").read_text())["group"] is True


# -- #637: parked jobs, the county first on a group's restore, a P25 system's flap hold shown --------------------------

GP = antenna.Group("UHF", ("a", "b", "c", "d"), (P25, "South 2", "FG1", "FG2"), ("FG2",))


def test_a_parked_job_is_never_wanted_short_or_run_by_the_plan():
    # (a dongle can't have a parked job as its own on the station; the plan alone never runs one anyway)
    own = {"a": P25, "b": "South 2", "c": "FG1", "d": "FG2"}
    assert antenna.plan(GP, own, ["a", "b", "c", "d"]) == ({"a": P25, "b": "South 2", "c": "FG1", "d": "Off"}, [])
    got, short = antenna.plan(GP, {**own, "d": "Off"}, ["a", "c", "d"], spares=["d"], fire=FIRE)
    assert got == {"a": P25, "c": "FG1", "d": "South 2"} and short == [] and "FG2" not in got.values()


@pytest.mark.parametrize("parked, why", [
    ("FG1", "parked is a list"),
    ([1], "parked is a list"),
    (["FG2"], "FG2 parked but not one of its jobs"),
])
def test_parse_refuses_a_parked_job_that_isnt_the_groups(parked, why):
    got, err = antenna.parse([{"name": "UHF", "sdrs": ["a"], "jobs": ["FG1"], "parked": parked}])
    assert got == [] and why in err


def test_parked_jobs_keep_the_lists_order_and_none_leaves_the_file_as_before():
    (g,), err = antenna.parse([{"name": "UHF", "sdrs": ["a"], "jobs": [P25, "FG2", "FG1"], "parked": ["FG1", "FG2"]}])
    assert err == "" and g.parked == ("FG2", "FG1") and g.row()["parked"] == ["FG2", "FG1"]
    (g,), _ = antenna.parse([{"name": "UHF", "sdrs": ["a"], "jobs": ["FG1"]}])
    assert g.parked == () and "parked" not in g.row()


UHF4 = [*UHF, "spare-1"]


def uhf4(parked=()):
    return [{"name": "UHF", "sdrs": UHF4, "jobs": [P25, "South 2", "FG1", "FG2"],
             **({"parked": list(parked)} if parked else {})}, GROUPS[1]]


def park(app, groups):
    """Save groups that park a fireground: asked once ("Save anyway"), then saved with that very ask."""
    why = app.set_antenna_groups(groups)
    if why and why.startswith(app.CONFIRM) and "keeps a fire channel off" in why:
        why = app.set_antenna_groups(groups, confirm=why[len(app.CONFIRM):])
    return why


def runm(sups, usb_at, t_from: float, t_to: float, dt: float = 0.5) -> None:
    """tests/test_p25_systems.py run(), with the supervisors' monotonic clock the same as the wall clock."""
    t = t_from
    while t < t_to:
        usb_at(t)
        for x in sups:
            x.step(t, t)
        t = round(t + dt, 3)


def group_alerts(app):
    from sizeup.cloud.health import JOB_GRACE_S
    from tests.test_health import Rig, station_status
    r = Rig()
    st = station_status()
    st["sdr"]["groups"] = app.sdr()["groups"]
    r.at(0, status=st)
    r.at(JOB_GRACE_S + 1, status=st)
    return r.h.model()["groups"], [a for a in r.h.alerts() if a["key"].startswith("group:")]


def test_a_parked_job_says_no_check_is_never_picked_auto_assigned_or_stood_in_and_is_audited(rig, tmp_path):
    plugged, logs, make = rig
    app = make().start()
    mapped(app)
    # parking a job a dongle runs never stops it: refused, that dongle switched Off first
    assert app.set_antenna_groups(uhf4(["FG2"])) == "SDR spare-1 runs FG2: switch it Off before parking FG2"
    assert app.set_mode("Off", "spare-1") is None
    # unparked, a job no dongle has is a check warning (#609) ...
    assert app.set_antenna_groups(uhf4()) is None
    assert app.sdr()["groups"][0]["unowned"] == ["FG2"]
    _rows, alerts = group_alerts(app)
    assert [(a["title"], a["text"]) for a in alerts] == [("Antenna group UHF check", "no dongle has FG2")]
    # ... parked, it is off on purpose: shown as parked, no warning (a fireground: asked once)
    assert app.set_antenna_groups(uhf4(["FG2"])) == (app.CONFIRM + "Parking FG2 keeps a fire channel off with no "
                                                     "warning.")
    assert park(app, uhf4(["FG2"])) is None
    assert app.set_antenna_groups(uhf4(["FG2"])) is None                 # parked already: not asked again
    g = app.sdr()["groups"][0]
    assert (g["unowned"], g["parked"], g["short"], g["conflicts"]) == ([], ["FG2"], [], {})
    rows, alerts = group_alerts(app)
    assert alerts == [] and rows[0]["parked"] == ["FG2"] and rows[0]["unowned"] == []
    assert any(x.endswith("· Fire/EMS 15 > South 2 > FG1 > FG2 · parked FG2; 700: SDR m700-a, m700-b · "
                          "MRRS 770 > MRRS 773") for x in logs)
    from sizeup.cad.api import summarize
    s, _ = summarize("/api/antenna-groups", {"groups": uhf4(["FG2"])}, {}, None)
    assert s.startswith("Antenna groups: UHF (4 SDRs: Fire/EMS 15 > South 2 > FG1 > FG2; parked FG2); 700 (")
    # never picked, by a person or auto-assign
    assert app.set_mode("FG2", "spare-1") == "FG2 is parked in UHF: unpark it first"
    assert app.set_mode("FG2", "uhf-c") == "FG2 is parked in UHF: unpark it first"
    app.set_by.pop("spare-1")                                            # an Off nobody picked: a spare
    spare = next(d for d in DONGLES if d[2] == "spare-1")
    plugged.remove(spare)
    app._sdr_arrivals()
    plugged.append(spare)
    app._sdr_arrivals()                                                  # back: the group's free job would be FG2
    assert app.assign.get("spare-1", "Off") == "Off" and on(app, "FG2") is None
    # a fallback stands the spare in for South 2, never runs FG2
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    assert (on(app, "South 2"), on(app, "FG1"), on(app, "FG2")) == ("spare-1", "uhf-c", None)
    plugged.insert(1, DONGLES[1])
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-b" and app.assign.get("spare-1", "Off") == "Off" and on(app, "FG2") is None
    # kept across a restart; unparked, the spare's next arrival gets FG2 again
    assert json.loads((tmp_path / "antenna_groups.json").read_text())["groups"][0]["parked"] == ["FG2"]
    app.stop()
    again = make().start()
    assert again.groups[0].parked == ("FG2",) and again.assign.get("spare-1", "Off") == "Off"
    assert again.set_antenna_groups(uhf4()) is None
    again.set_by.pop("spare-1", None)
    plugged.remove(spare)
    again._sdr_arrivals()
    plugged.append(spare)
    again._sdr_arrivals()
    assert on(again, "FG2") == "spare-1"


def test_a_parked_fire_job_saved_on_a_dongle_keeps_running_and_is_a_check(rig, tmp_path):
    plugged, logs, make = rig
    app = make().start()
    mapped(app)
    assert app.set_mode("Off", "spare-1") is None
    assert park(app, uhf4(["FG2"])) is None
    app.stop()
    modes = json.loads((tmp_path / "sdr_modes.json").read_text())
    modes["spare-1"] = "FG2"                                             # put back by hand
    (tmp_path / "sdr_modes.json").write_text(json.dumps(modes))
    again = make().start()
    again._sdr_arrivals()
    assert on(again, "FG2") == "spare-1"                                 # a fire job: never stopped by a lock
    assert any(x.startswith("sdr: CRITICAL") and "FG2 is parked in UHF" in x for x in logs)
    assert again.sdr()["groups"][0]["conflicts"] == {"spare-1": "FG2"}
    _rows, alerts = group_alerts(again)
    assert [a["title"] for a in alerts] == ["Antenna group UHF check"]


def test_a_restore_of_both_systems_asks_for_the_county_first_whatever_the_groups_order(rig, monkeypatch):
    plugged, logs, make = rig
    app = make().start()
    mapped(app)
    assert app.set_antenna_groups([GROUPS[1], GROUPS[0]]) is None       # MRRS's group listed first
    plugged[:] = [d for d in plugged if d[2] not in ("uhf-a", "m700-a")]   # a hub with the county's and MRRS's
    app._sdr_arrivals()
    assert on(app, P25) == "uhf-c" and app.systems["mrrs"].recorder.source_on(0) == "m700-b"
    started: list[str] = []
    real = app._start_mode
    monkeypatch.setattr(app, "_start_mode", lambda job, sd: (started.append(job), real(job, sd))[1])
    plugged[:] = list(DONGLES)                                            # both back in the same step
    app._sdr_arrivals()
    assert (on(app, P25), app.systems["mrrs"].recorder.source_on(0)) == ("uhf-a", "m700-a")
    assert started[0] == P25 and "MRRS 770" in started, started


def _group_restore_service(tmp_path, usb):
    """The station's service as an antenna group's fallback left it: the county on a stand-in (c3), MRRS's 770 side
    on its 773 dongle (pd-b); then the restore: the county back on v4, MRRS on pd-a and pd-b, both asked at once."""
    from tests.test_p25_systems import ask_county, ask_system, service
    county, mrrs, pop, logs, d = service(tmp_path, found=lambda: list(usb))
    ask_county(tmp_path, sdr="c3")
    ask_system(tmp_path, {0: "pd-b"})
    return county, mrrs, pop, logs, d


def _ask_restore(tmp_path, group: bool):
    from pathlib import Path
    from sizeup.radio import trunkrecd
    from tests.test_p25_systems import ask_county, ask_system
    ask_county(tmp_path, sdr="v4", seq=2)
    ask_system(tmp_path, {0: "pd-a", 1: "pd-b"}, seq=2)
    if group:                                                            # as SystemUnitRecorder writes a group move
        p = Path(trunkrecd.system_state_path("mrrs", str(tmp_path / "state" / "p25_recorder.json")))
        p.write_text(json.dumps({**json.loads(p.read_text()), "group": True}))


@pytest.mark.parametrize("group", [True, False])
def test_a_group_restore_starts_the_county_first_and_the_other_system_once_it_is_up(tmp_path, group):
    from sizeup.radio import trunkrecd
    usb = [(0, "v4"), (1, "pd-a"), (2, "pd-b"), (3, "c3")]
    county, mrrs, pop, logs, d = _group_restore_service(tmp_path, usb)
    runm([mrrs, county], lambda t: None, 100.0, 110.0)                   # (MRRS's thread first: the race)
    assert county.status["state"] == "ok" and mrrs.status["state"] == "ok" and len(pop.started) == 2
    _ask_restore(tmp_path, group)
    starts: dict[int, tuple[float, str]] = {}
    details: list[str] = []

    def watch(t):
        for p in pop.started[2:]:
            starts.setdefault(p.pid, (t, "county" if p.kw["cwd"] != str(d) else "mrrs"))
        details.append(mrrs.status.get("detail", ""))
    runm([mrrs, county], watch, 110.0, 140.0)
    (tc, who_c), (tm, who_m) = sorted(starts.values())
    assert county.status["state"] == "ok" and mrrs.status["state"] == "ok" and county.status["sdr"] == "v4"
    if group:
        assert (who_c, who_m) == ("county", "mrrs") and tm - tc >= trunkrecd.COUNTY_SETTLE_S, starts
        assert f"not started yet ({trunkrecd.MODE} starts first)" in details
    else:                                                                # a person's switch: as before #637
        assert tm - tc < trunkrecd.COUNTY_SETTLE_S and not any("starts first" in x for x in details)


def test_a_group_move_waits_for_the_county_a_minute_at_most_and_not_at_all_when_it_is_up(tmp_path):
    from sizeup.radio import trunkrecd
    usb = [(1, "pd-a"), (2, "pd-b"), (3, "c3")]                         # the county's v4 isn't back: it never starts
    county, mrrs, pop, logs, d = _group_restore_service(tmp_path, usb)
    runm([mrrs, county], lambda t: None, 100.0, 110.0)
    _ask_restore(tmp_path, True)
    n = len(pop.started)
    at: list[float] = []
    runm([mrrs, county], lambda t: at.append(t) if len(pop.started) > n and len(at) == 0 else None, 110.0, 200.0)
    assert county.status["state"] == "absent" and mrrs.status["state"] == "ok"
    assert 110.0 + trunkrecd.COUNTY_WAIT_S <= at[0] <= 110.0 + trunkrecd.COUNTY_WAIT_S + 2, at
    # the county up for long: a group move of MRRS waits for nothing
    usb[:] = [(0, "v4"), (1, "pd-a"), (2, "pd-b"), (3, "c3")]
    runm([mrrs, county], lambda t: None, 200.0, 230.0)
    assert county.status["state"] == "ok"
    from tests.test_p25_systems import ask_system
    ask_system(tmp_path, {0: "pd-b"}, seq=3)
    from pathlib import Path
    p = Path(trunkrecd.system_state_path("mrrs", str(tmp_path / "state" / "p25_recorder.json")))
    p.write_text(json.dumps({**json.loads(p.read_text()), "group": True}))
    n = len(pop.started)
    runm([mrrs, county], lambda t: None, 230.0, 231.0)
    assert len(pop.started) == n + 1 and mrrs.status["sdr"] == "0=pd-b"


def test_a_held_system_says_until_when_on_receivers_and_health(tmp_path, monkeypatch):
    import time as _time
    from types import SimpleNamespace
    from sizeup.radio import trunkrecd
    from sizeup.radio.recorder import SystemUnitRecorder
    from tests.test_p25_systems import ask_system, service
    usb = [(0, "v4"), (1, "pd-a"), (2, "pd-b")]
    _county, mrrs, pop, _logs, _d = service(tmp_path, found=lambda: list(usb))
    ask_system(tmp_path, {0: "pd-a", 1: "pd-b"})
    assert mrrs.step(100.0)["state"] == "ok"
    # a return held back (_coming_back): the status says until when, and keeps it as hold_until
    usb.remove((1, "pd-a"))
    for t in (106.0, 107.0):
        mrrs.step(t)
    assert mrrs.status["state"] == "absent" and "hold_until" not in mrrs.status       # gone: not a hold
    mrrs.hold_until = 700.0
    usb.insert(1, (1, "pd-a"))
    for t in range(108, 120):
        st = mrrs.step(float(t))
    hhmm = _time.strftime("%H:%M", _time.localtime(700.0))
    assert (st["state"], st["hold_until"]) == ("absent", 700.0)
    assert st["detail"] == f"SDR 'pd-a', 'pd-b' back: not started yet (held back until {hhmm} after dropouts)"
    assert mrrs.step(701.0)["state"] == "ok" and "hold_until" not in mrrs.status
    # the station side passes it on (SystemUnitRecorder -> Runtime -> Receivers row, station health -> Health)
    sp = tmp_path / "s.json"
    rec = SystemUnitRecorder("mrrs", "MRRS", "trunk-recorder --config=config.json", str(tmp_path),
                             state_path=str(tmp_path / "w.json"), status_path=str(sp), wait_s=0.0)
    until = _time.time() + 600
    sp.write_text(json.dumps({"state": "absent", "detail": "held back until 14:32 after dropouts", "hold_until": until,
                              "at": _time.time(), "on": True}))
    assert rec.status()["hold_until"] == until
    rt = object.__new__(systems.Runtime)
    rt.system, rt.recorder = MRRS, rec
    rt.ingest = SimpleNamespace(status=lambda: {"label": "MRRS", "state": "ok", "detail": ""})
    row = rt.status(["MRRS 770"])
    assert (row["state"], row["hold_until"]) == ("absent", until)
    from sizeup.cloud.health import station_metrics
    app = SimpleNamespace(shared=True, assign={"m700-a": "MRRS 770"}, receivers=[], systems={"mrrs": rt})
    jobs = station_metrics(app, SimpleNamespace(sample=lambda: {}))["jobs"]
    assert jobs["MRRS 770"]["hold_until"] == until and "hold_until" not in jobs["MRRS 773"]
    from tests.test_health import Rig, station_status
    r = Rig()
    st = station_status()
    st["sdr"]["sdrs"][0].update(mode="MRRS 770")
    st["health"]["jobs"] = {"MRRS 770": dict(jobs["MRRS 770"])}
    r.at(0, status=st)
    d = next(x for x in r.h.model()["dongles"] if x["job"] == "MRRS 770")
    assert d["hold_until"] == until and not d["running"]
    assert not any(a["key"] == "job:MRRS 770" for a in r.h.alerts())   # (another system's: never an alert)
    root = appmod.ROOT / "sizeup" / "web" / "static"
    assert "held to ${fmtTime(x.hold_until)}" in (root / "health.js").read_text()
    js = (root / "radio-controls.js").read_text()
    assert "held to ${fmtTime(r.hold_until)}" in js and "held to ${fmtTime(x.hold_until)}" in js


def test_a_reception_test_never_runs_a_parked_job(rig):
    plugged, logs, make = rig
    app = grouped(make)
    for sd in ("m700-a", "m700-b"):
        assert app.set_mode("Off", sd) is None
    assert app.set_antenna_groups([GROUPS[0], {**GROUPS[1], "parked": ["MRRS 773"]}]) is None
    err = app.reception.start({"system": "mrrs", "jobs": ["MRRS 773"], "sdr": "m700-b", "minutes": 15})
    assert err == "MRRS 773 is parked in 700: unpark it first"


def test_every_group_restore_waits_for_the_county_not_only_the_first(tmp_path):
    # review: the wait was keyed on the start key, so a second restore of the same dongles found it long expired
    from sizeup.radio import trunkrecd
    from tests.test_p25_systems import ask_county, ask_system
    usb = [(0, "v4"), (1, "pd-a"), (2, "pd-b"), (3, "c3")]
    county, mrrs, pop, logs, d = _group_restore_service(tmp_path, usb)
    runm([mrrs, county], lambda t: None, 100.0, 110.0)
    gaps = []
    for k, t0 in enumerate((110.0, 400.0)):
        if k:                                                            # a second shortage: back on the stand-ins
            ask_county(tmp_path, sdr="c3", seq=10)
            ask_system(tmp_path, {}, on=False, seq=10)
            runm([mrrs, county], lambda t: None, t0 - 60, t0)
        _ask_restore(tmp_path, True)
        if k:                                                            # (a new wish: as SystemUnitRecorder writes one)
            from pathlib import Path
            for p in (tmp_path / "state" / "p25_recorder.json",
                      Path(trunkrecd.system_state_path("mrrs", str(tmp_path / "state" / "p25_recorder.json")))):
                p.write_text(json.dumps({**json.loads(p.read_text()), "seq": 11}))
        n, at = len(pop.started), {}

        def watch(t):
            for p in pop.started[n:]:
                at.setdefault("county" if p.kw["cwd"] != str(d) else "mrrs", t)
        runm([mrrs, county], watch, t0, t0 + 30)
        gaps.append(at["mrrs"] - at["county"])
    assert all(g >= trunkrecd.COUNTY_SETTLE_S for g in gaps), gaps


# -- the review of #702: the county and paging never parked, the county's wait once per move, monotonic caps ----------

def test_the_county_and_paging_are_never_parked_and_a_tested_job_isnt_either(rig):
    plugged, logs, make = rig
    app = make().start()
    mapped(app)
    for sd in ("uhf-a", "uhf-b", "spare-1"):
        assert app.set_mode("Off", sd) is None                           # both fire dongles switched Off ...
    assert app.set_antenna_groups(uhf4([P25])) == ("Fire/EMS 15 can't be parked (fire coverage): take it out of UHF, "
                                                   "or give it a dongle")
    assert app.set_antenna_groups(uhf4(["South 2"])).startswith("South 2 can't be parked (fire coverage)")
    assert app.set_antenna_groups(uhf4([P25, "South 2"])).startswith("Fire/EMS 15 can't be parked")
    assert app.groups == []                                              # ... "no dongle has" stays the warning
    assert sorted(app.sdr()["no_park"]) == sorted([P25, "South 2"])
    js = (appmod.ROOT / "sizeup" / "web" / "static" / "radio-controls.js").read_text()
    assert "pk.hidden = !parked && (sdr.no_park || []).includes(j)" in js
    # a running reception test's job: not parked under it
    for sd in ("m700-a", "m700-b"):
        assert app.set_mode("Off", sd) is None
    assert app.set_antenna_groups(GROUPS) is None
    assert app.reception.start({"system": "mrrs", "jobs": ["MRRS 773"], "sdr": "m700-b", "minutes": 15}) is None
    assert app.set_antenna_groups([GROUPS[0], {**GROUPS[1], "parked": ["MRRS 773"]}]) == (
        "a reception test runs MRRS 773: stop it before parking MRRS 773")


def _group_wish(tmp_path, sources, seq):
    from pathlib import Path
    from sizeup.radio import trunkrecd
    from tests.test_p25_systems import ask_system
    ask_system(tmp_path, sources, seq=seq)
    p = Path(trunkrecd.system_state_path("mrrs", str(tmp_path / "state" / "p25_recorder.json")))
    p.write_text(json.dumps({**json.loads(p.read_text()), "group": True}))


def test_the_countys_wait_is_once_per_move_never_for_a_crash_restart_or_an_unplug_later(tmp_path):
    from sizeup.radio import trunkrecd
    from tests.test_p25_systems import ask_county
    usb = [(1, "pd-a"), (2, "pd-b")]                                      # the county's v4 away: it never comes up
    county, mrrs, pop, logs, d = _group_restore_service(tmp_path, usb)
    ask_county(tmp_path, sdr="v4", seq=2)
    _group_wish(tmp_path, {0: "pd-a", 1: "pd-b"}, seq=5)                  # a group move, the county down
    runm([mrrs, county], lambda t: None, 100.0, 170.0)
    assert mrrs.status["state"] == "ok" and mrrs.county_done == 5         # waited its minute, once
    # a crash restart on the same wish: at its usual backoff, never the county's minute again
    pd = next(p for p in reversed(pop.started) if p.kw["cwd"] == str(d))
    pd.code = 1
    n = len(pop.started)
    at: list[float] = []
    runm([mrrs, county], lambda t: at.append(t) if len(pop.started) > n and not at else None, 170.0, 190.0)
    assert at and at[0] - 170.0 < 5, at
    # unplugged later on the same wish: "not plugged in", then the return's own wait only (never both stacked)
    details: list[str] = []
    usb[:] = [(2, "pd-b")]
    runm([mrrs, county], lambda t: details.append(mrrs.status.get("detail", "")), 190.0, 200.0)
    assert any("not plugged in" in x or "unplugged" in x for x in details), details[-3:]
    assert f"not started yet ({trunkrecd.MODE} starts first)" not in details      # (the move's wait: over)
    usb[:] = [(1, "pd-a"), (2, "pd-b")]
    n = len(pop.started)
    at.clear()
    runm([mrrs, county], lambda t: at.append(t) if len(pop.started) > n and not at else None, 200.0, 400.0)
    assert at and at[0] - 200.0 <= trunkrecd.COUNTY_WAIT_S + 2 * trunkrecd.UNPLUGGED_S, at


def test_a_wall_clock_step_back_never_lengthens_the_countys_wait(tmp_path):
    # the cap is on the monotonic clock: the wall clock stepping back (NTP, WSL resyncing) never extends it
    from sizeup.radio import trunkrecd
    from tests.test_p25_systems import ask_county
    county, mrrs, pop, logs, d = _group_restore_service(tmp_path, [])
    ask_county(tmp_path, sdr="v4", seq=2)
    _group_wish(tmp_path, {0: "pd-a", 1: "pd-b"}, seq=5)
    w, base = trunkrecd.wanted_system(mrrs.state), {}
    mrrs._m = 1000.0
    assert mrrs._county_first(500.0, w, base) is True
    mrrs._m = 1000.0 + trunkrecd.COUNTY_WAIT_S - 1
    assert mrrs._county_first(200.0, w, base) is True                    # the wall clock 5 min back: still the cap's
    mrrs._m = 1000.0 + trunkrecd.COUNTY_WAIT_S
    assert mrrs._county_first(200.0, w, base) is False and mrrs.county_done == 5
    assert mrrs._county_first(201.0, w, base) is False                   # once per move
