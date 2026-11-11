"""More P25 systems than the county's (GitHub #409: MTA MRRS beside the county system): the systems as shipped
(deploy/trunk-recorder/systems/), the trunk-recorder each runs (sizeup/radio/trunkrecd.py SystemSupervisor,
sizeup/radio/recorder.py SystemRecorder), the calls of two systems side by side in one app, the talkgroup tiers
(sizeup/tiers.py), the source keys and the rows' fields (sizeup/sourcekeys.py, #415), and the rules that keep another
system's traffic context only.

No hardware, no trunk-recorder, no network: trunk-recorder is a fake Popen, the dongles a list, the claims a tmp folder
(conftest.py). Call JSON is in trunk-recorder 5.2.0's shape (tests/test_p25.py tr_call). Every transmission, unit and
radio ID here is invented."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

import sizeup.app as appmod
import sizeup.radio.claims as claims
from sizeup import channels, sourcekeys, tiers
from sizeup.config import ChannelCfg, load
from sizeup.radio import systems, trunkrecd
from sizeup.radio.p25 import Call, Turn
from sizeup.radio.recorder import SystemRecorder, SystemUnitRecorder, TrunkRecorder
from sizeup.stt import LIVE, LOW, URGENT
from sizeup.talkgroups import TalkgroupPrefs
from tests.test_app import FakeWhisper, cfg as app_cfg, voice, wait
from tests.test_p25 import T0, tr_call
from tests.test_radios import Clock, _event_with, cad, tx
from tests.test_trunk_recorder_unit import OTHER_PID, SERVICE_PID, FakePopen

ROOT = Path(__file__).resolve().parent.parent
TR = ROOT / "deploy" / "trunk-recorder"
MRRS = systems.load(TR / "systems" / "mrrs")
PD, PD_TAC, PD_SECURE, EMS = 9211, 9212, 9213, 9570


# -- the system as shipped ------------------------------------------------------------------------------------------------


def test_mrrs_is_defined_as_listed_and_runs_on_two_dongle_jobs():
    assert (MRRS.name, MRRS.label, MRRS.jobs) == ("mrrs", "MRRS", ("MRRS 770", "MRRS 773"))
    assert [round(f / 1e6, 5) for f in MRRS.frequencies] == [769.90625, 770.18125, 770.46875, 770.71875, 772.48125,
                                                             773.03125, 773.68125]
    # ~3.8 MHz: neither 2.4 MHz dongle takes in all of it, the two together do
    assert len(MRRS.covered([0])) == 4 and len(MRRS.covered([1])) == 3 and len(MRRS.covered([0, 1])) == 7
    assert MRRS.site["nac"] == "051" and MRRS.site["wacn"] == "91FE8" and MRRS.site["phase"] == 2
    conf = json.loads((TR / "systems" / "mrrs" / "config.json").read_text())
    assert conf["systems"][0]["modulation"] == "qpsk" and conf["systems"][0]["recordUnknown"] is False
    assert MRRS.capture_dir.endswith("captures/p25-mrrs")           # never the county's capture directory


def test_talkgroups_file_gives_tier_service_and_department():
    t = MRRS.talkgroups
    assert (t[PD]["tier"], t[PD]["service"], t[PD]["group"]) == ("alert", "police", "Police")
    assert (t[EMS]["tier"], t[EMS]["service"]) == ("alert", "ems")
    assert (t[9121]["tier"], t[9121]["service"], t[9121]["department"]) == ("text", "transit", "Metro-North")
    assert t[9049]["tier"] == "record" and t[9048]["department"] == "MTA PD"
    # encrypted: activity only, whatever the file says
    assert all(i["tier"] == "activity" for i in t.values() if i["encrypted"]) and t[PD_SECURE]["encrypted"]
    assert all(i["tier"] in tiers.TIERS for i in t.values())


def test_the_example_config_finds_the_system_and_the_county_keeps_its_own_list():
    c = load(ROOT / "config.example.toml")
    assert [s.name for s in c.p25_systems] == ["mrrs"]
    assert PD not in c.p25_talkgroups and PD not in c.p25_tg_info and 307 in c.p25_tg_info
    assert c.tier_keep_days == {"alert": 30.0, "text": 0.0, "record": 7.0}


def make_system(base: Path, name="east", tgs="9211,23fb,PD Dispatch,T,d,Police,X,1,alert\n", jobs=("East 1",), **over):
    d = base / "systems" / name
    d.mkdir(parents=True)
    conf = {"sizeup": {"label": name.title(), "jobs": list(jobs)},
            "sources": [{"center": 770312500, "rate": 2400000, "device": "rtl=x"}][:len(jobs)] or [],
            "systems": [{"shortName": name, "control_channels": [769906250], "talkgroupsFile": "talkgroups.csv"}],
            "captureDir": "cap"}
    conf.update(over)
    (d / "config.json").write_text(json.dumps(conf))
    (d / "talkgroups.csv").write_text("Decimal,Hex,Alpha Tag,Mode,Description,Tag,Category,Priority,Tier\n" + tgs)
    return d


def test_a_talkgroup_number_or_job_name_used_twice_is_refused(tmp_path):
    make_system(tmp_path)
    assert [s.name for s in systems.discover(tmp_path)] == ["east"]
    with pytest.raises(systems.SystemError_, match="talkgroup 9211 is already another system's"):
        systems.discover(tmp_path, taken_tgs={9211})
    with pytest.raises(systems.SystemError_, match="job name 'East 1'"):
        systems.discover(tmp_path, taken_jobs=["east 1"])
    make_system(tmp_path, "bad", tgs="12,c,X,T,d,G,X,1,loud\n", jobs=("Bad 1",))
    with pytest.raises(systems.SystemError_, match="Tier 'loud'"):
        systems.discover(tmp_path)


def test_config_load_leaves_out_a_system_that_reuses_a_county_talkgroup(tmp_path):
    shutil.copytree(TR, tmp_path / "tr", ignore=shutil.ignore_patterns("*.wav", "westchester"))
    f = tmp_path / "tr" / "systems" / "mrrs" / "talkgroups.csv"
    f.write_text(f.read_text() + '307,133,Clash,T,"x",Police,MRRS,3,record,police,\n')
    conf = tmp_path / "c.toml"
    conf.write_text(f'[p25]\ncapture_dir = ""\nrecorder_dir = "{tmp_path / "tr"}"\n'
                    f'talkgroups_file = "{tmp_path / "tr" / "talkgroups.csv"}"\n')
    c = load(conf)                                   # the app still starts: fire is never held up by another system
    assert c.p25_systems == () and 307 in c.p25_talkgroups
    assert "talkgroup 307 is already another system's" in c.p25_system_problems[0]


# -- source keys (#415) ---------------------------------------------------------------------------------------------------


def test_source_keys_are_qualified_and_the_old_ones_are_the_countys():
    assert sourcekeys.p25("mrrs", 9211) == "p25:mrrs:9211"
    assert sourcekeys.parse("tg:307") == ("westchester", 307) and sourcekeys.parse("p25:mrrs:307") == ("mrrs", 307)
    assert sourcekeys.parse("FG1") is None
    assert sourcekeys.legacy("p25:westchester:307") == "tg:307" and sourcekeys.legacy("p25:mrrs:307") == "p25:mrrs:307"
    assert sourcekeys.channel_key("", 307) == "tg:307" and sourcekeys.channel_key("mrrs", 9211) == "p25:mrrs:9211"
    assert sourcekeys.of_row("FG1", None) == "fm:FG1" and sourcekeys.of_row("HPN Tower", None, air=True) == "air:HPN Tower"
    assert channels.KEY.fullmatch("p25:mrrs:9211") and channels.is_tg("p25:mrrs:9211")
    assert channels.tg_of("tg:307") == ("", 307) and channels.tg_of("p25:mrrs:9211") == ("mrrs", 9211)
    assert [sourcekeys.service(g) for g in ("Fire/EMS", "County Police", "Hospital", "Metro-North", "Operations")] == [
        "fire", "police", "ems", "transit", "other"]
    assert sourcekeys.service("Metro-North", "police") == "police" and sourcekeys.service(kind="airband") == "air"


# -- tiers ----------------------------------------------------------------------------------------------------------------


def prefs(tmp_path) -> TalkgroupPrefs:
    return TalkgroupPrefs({307: {"name": "Fire/EMS 15", "group": "Fire/EMS", "encrypted": False}}, {307},
                          tmp_path / "talkgroups.json", others={"mrrs": MRRS.talkgroups})


def test_tiers_per_talkgroup_and_the_county_file_stays_as_an_older_release_reads_it(tmp_path):
    p = prefs(tmp_path)
    assert p.tier(307) is None and p.record(307) and p.main(307)              # the county's: its own switches
    assert (p.tier(PD, "mrrs"), p.record(PD, "mrrs"), p.main(PD, "mrrs")) == ("alert", True, False)
    assert (p.tier(9049, "mrrs"), p.record(9049, "mrrs")) == ("record", True)
    assert (p.tier(PD_SECURE, "mrrs"), p.record(PD_SECURE, "mrrs")) == ("activity", False)
    assert p.tier(307, "mrrs") == "record" and not p.known(307, "mrrs")       # the same number on another system: not 307
    p.set(307, record=False)
    assert p.set_tier(PD, "text", "mrrs") is None and p.tier(PD, "mrrs") == "text"
    assert p.set_tier(PD, "transcribe", "mrrs") is None                       # the older word for it
    assert "encrypted" in p.set_tier(PD_SECURE, "record", "mrrs") and p.set_tier(PD_SECURE, "off", "mrrs") is None
    assert "Record / Main" in p.set_tier(307, "alert") and "one of" in p.set_tier(PD, "loud", "mrrs")
    p.set(PD, main=True, system="mrrs")                                       # never Main
    assert not p.main(PD, "mrrs")
    p.set(PD, record=False, system="mrrs")
    assert p.tier(PD, "mrrs") == "off"
    p.set(PD, record=True, system="mrrs")
    assert p.tier(PD, "mrrs") == "alert"                                      # back to the file's
    # the county's choices: bare numbers only (a release from before this does int(key) on every one)
    assert json.loads((tmp_path / "talkgroups.json").read_text()) == {"307": {"record": False}}
    assert json.loads((tmp_path / "talkgroups_systems.json").read_text()) == {
        "p25:mrrs:9211": {"tier": "alert"}, "p25:mrrs:9213": {"tier": "off"}}
    again = prefs(tmp_path)
    assert again.tier(PD_SECURE, "mrrs") == "off" and not again.record(307)
    rows = {r["source"]: r for r in again.state()}
    assert rows["p25:mrrs:9211"] | {"count": 0, "last": None} == {
        "tg": PD, "name": "PD Dispatch", "group": "Police", "encrypted": False, "record": True, "main": False,
        "count": 0, "last": None, "dropped": 0, "system": "mrrs", "source": "p25:mrrs:9211", "key": "p25:mrrs:9211",
        "ours": False, "service": "police", "department": "", "tier": "alert"}
    assert (rows["p25:westchester:307"]["key"], rows["p25:westchester:307"]["tier"]) == ("tg:307", None)
    assert rows["p25:mrrs:9121"]["department"] == "Metro-North"


def test_a_new_source_is_record_only_until_a_tier_is_chosen():
    assert tiers.default(False) == "record" and tiers.default(True) == "activity"
    assert tiers.default(True, "alert") == "activity" and tiers.default(False, "activity") == "record"
    assert tiers.derived(True, True, False) == "text" and tiers.derived(False, True, True) == "activity"


def test_expected_storage_and_retention_days():
    s = tiers.storage({"alert": 3600.0, "record": 7200.0}, {"alert": 30.0, "text": 0.0, "record": 7.0}, 12.0)
    assert [(r["tier"], r["keep_days"], r["mb"]) for r in s["tiers"]] == [
        ("alert", 30.0, 1728.0), ("text", 0.0, 0.0), ("record", 7.0, 806.4)]
    assert s["mb"] == 2534.4 and s["usd_month"] == round(2.5344 * tiers.USD_PER_GB_MONTH, 4)
    assert tiers.keep_days({"record": 14}) == {"alert": 30.0, "text": 0.0, "record": 14.0}
    with pytest.raises(ValueError):
        tiers.keep_days({"off": 3})


# -- what trunk-recorder is handed ------------------------------------------------------------------------------------------


def system_dir(tmp_path) -> Path:
    """<recorder_dir>/systems/mrrs as install-linux.sh makes it."""
    work = tmp_path / "tr"
    d = work / "systems" / "mrrs"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TR / "unit-event.sh", work / "unit-event.sh")
    shutil.copy2(TR / "systems" / "mrrs" / "talkgroups.csv", d / "talkgroups.csv")
    c = json.loads((TR / "systems" / "mrrs" / "config.json").read_text())
    c["captureDir"], c["tempDir"] = str(tmp_path / "captures" / "p25-mrrs"), str(tmp_path / "captures" / "p25-mrrs-tmp")
    (d / "config.json").write_text(json.dumps(c))
    c0 = json.loads((TR / "config.json").read_text())
    (work / "config.json").write_text(json.dumps(c0))
    shutil.copy2(TR / "talkgroups.csv", work / "talkgroups.csv")
    return d


def test_runtime_config_has_only_the_sources_with_a_dongle_and_the_channels_they_cover(tmp_path):
    d = system_dir(tmp_path)
    argv = trunkrecd.system_runtime_argv(["/x/trunk-recorder", "--config=config.json"], d, {1: 3})
    assert argv == ["/x/trunk-recorder", "--config=config.runtime.json"]
    rt = json.loads((d / "config.runtime.json").read_text())
    assert "sizeup" not in rt and [s["device"] for s in rt["sources"]] == ["rtl=3"]
    assert rt["sources"][0]["center"] == 773081250
    assert rt["systems"][0]["control_channels"] == [772481250, 773031250, 773681250]
    assert rt["systems"][0]["unitScript"].endswith("unit-event.sh") and rt["systems"][0]["talkgroupsFile"] == "talkgroups.runtime.csv"
    head = (d / "talkgroups.runtime.csv").read_text().splitlines()
    assert head[0] == "Decimal,Hex,Alpha Tag,Mode,Description,Tag,Category,Priority"      # sizeup's columns left out
    assert head[1].startswith("9211,23fb,PD Dispatch,T,") and head[1].endswith(",Police,MRRS,1")
    both = json.loads(Path(d / trunkrecd.system_runtime_argv(["t"], d, {0: 0, 1: 1})[-1].split("=")[1]).read_text())
    assert [s["device"] for s in both["sources"]] == ["rtl=0", "rtl=1"] and len(both["systems"][0]["control_channels"]) == 7
    with pytest.raises(ValueError):
        trunkrecd.system_runtime_argv(["t"], d, {})


# -- the station's service: one supervisor per system -------------------------------------------------------------------------


def service(tmp_path, found=lambda: [(0, "v4"), (1, "pd-a"), (2, "pd-b")]):
    d = system_dir(tmp_path)
    exe = tmp_path / "trunk-recorder"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    conf = tmp_path / "config.toml"
    conf.write_text(f'[p25]\nrecorder = "{exe} --config=config.json"\nrecorder_dir = "{d.parent.parent}"\n')
    pop, logs = FakePopen(), []
    kw = dict(config=str(conf), state=str(tmp_path / "state" / "p25_recorder.json"),
              status_path=str(tmp_path / "run" / "status.json"), claims=claims.DIR, popen=pop, found=found,
              killpg=pop.killpg, pid=SERVICE_PID, log=logs.append)
    return trunkrecd.Supervisor(**kw), trunkrecd.SystemSupervisor("mrrs", **kw), pop, logs, d


def ask_system(tmp_path, sources: dict, on=True, seq=1):
    p = Path(trunkrecd.system_state_path("mrrs", str(tmp_path / "state" / "p25_recorder.json")))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"on": on, "label": "MRRS", "pid": None, "seq": seq, "sources": {
        str(i): {"sdr": sd, "mode": f"MRRS {770 + 3 * i}", "name": sd} for i, sd in sources.items()}}))


def ask_county(tmp_path, sdr="v4", seq=1):
    p = tmp_path / "state" / "p25_recorder.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"on": True, "sdr": sdr, "name": "V4", "pid": None, "seq": seq}))


def test_a_second_system_runs_beside_the_countys_and_never_restarts_it(tmp_path):
    county, mrrs, pop, logs, d = service(tmp_path)
    ask_county(tmp_path)
    assert county.step(100.0)["state"] == "ok"
    assert mrrs.step(100.0)["state"] == "off" and len(pop.started) == 1       # defined, but nothing asked: nothing runs
    fire = pop.started[0]
    ask_system(tmp_path, {0: "pd-a"})
    st = mrrs.step(101.0)
    assert (st["state"], st["sdr"], st["system"]) == ("ok", "0=pd-a", "mrrs")
    pd = pop.started[1]
    assert pd.kw["cwd"] == str(d) and pd.argv[-1] == "--config=config.runtime.json"
    assert json.loads((d / "config.runtime.json").read_text())["sources"][0]["device"] == "rtl=1"
    assert json.loads((claims.DIR / "pd-a.json").read_text()) | {"since": 0} == {
        "app": "sizeup", "pid": SERVICE_PID, "mode": "MRRS 770", "since": 0}
    # its second dongle: its own trunk-recorder restarts on both; the county's is the same process throughout
    ask_system(tmp_path, {0: "pd-a", 1: "pd-b"}, seq=2)
    mrrs.step(102.0)
    assert mrrs.step(103.0)["state"] == "ok" and pd.code is not None
    assert [s["device"] for s in json.loads((d / "config.runtime.json").read_text())["sources"]] == ["rtl=1", "rtl=2"]
    assert county.step(103.0)["state"] == "ok" and fire.code is None and county.status["tr_pid"] == fire.pid
    assert json.loads((claims.DIR / "v4.json").read_text())["mode"] == "Fire/EMS 15"
    # one taken back: restarted on the other, the dongle's claim let go
    ask_system(tmp_path, {1: "pd-b"}, seq=3)
    mrrs.step(104.0)
    assert not (claims.DIR / "pd-a.json").exists() and (claims.DIR / "pd-b.json").exists()
    assert mrrs.step(105.0)["sdr"] == "1=pd-b"
    ask_system(tmp_path, {}, on=False, seq=4)
    st = mrrs.step(106.0)
    assert st["state"] == "off" and st["seq"] == 4 and not (claims.DIR / "pd-b.json").exists()
    assert len(pop.running) == 1 and pop.running[0] is fire


def test_a_dongle_another_app_holds_is_never_taken_and_nothing_starts(tmp_path):
    _county, mrrs, pop, _logs, _d = service(tmp_path)
    claims.DIR.mkdir(parents=True, exist_ok=True)
    held = {"app": "shortfinal", "pid": OTHER_PID, "mode": "AM 118.575", "since": 1.0}
    (claims.DIR / "pd-b.json").write_text(json.dumps(held))
    ask_system(tmp_path, {0: "pd-a", 1: "pd-b"})
    st = mrrs.step(100.0)
    assert st["state"] == "held" and "shortfinal holds pd-b" in st["detail"] and not pop.started
    assert json.loads((claims.DIR / "pd-b.json").read_text()) == held          # untouched
    assert not (claims.DIR / "pd-a.json").exists()                             # all or nothing: the free one stays free
    (claims.DIR / "pd-b.json").unlink()
    assert mrrs.step(101.0)["state"] == "ok"


def test_a_missing_dongle_or_one_dongle_on_two_sources_starts_nothing(tmp_path):
    _county, mrrs, pop, _logs, _d = service(tmp_path, found=lambda: [(0, "v4"), (1, "pd-a")])
    ask_system(tmp_path, {0: "pd-a", 1: "pd-b"})
    st = mrrs.step(100.0)
    assert st["state"] == "absent" and "'pd-b' not plugged in" in st["detail"] and not pop.started
    ask_system(tmp_path, {0: "pd-a", 1: "pd-a"}, seq=2)
    assert mrrs.step(200.0)["state"] == "down" and not pop.started


def test_the_service_finds_the_systems_by_their_directories(tmp_path):
    d = system_dir(tmp_path)
    assert list(trunkrecd.system_dirs(d.parent.parent)) == ["mrrs"]
    made, more = [], {}

    class Fake:
        def __init__(self, name):
            self.name, self.steps, self.down = name, 0, ""
            made.append(self)

        def step(self):
            self.steps += 1

        def shutdown(self, why=""):
            self.down = why

    trunkrecd.step_systems(more, str(d.parent.parent), make=Fake)
    trunkrecd.step_systems(more, str(d.parent.parent), make=Fake)
    assert list(more) == ["mrrs"] and made[0].steps == 2 and len(made) == 1
    shutil.rmtree(d)
    trunkrecd.step_systems(more, str(d.parent.parent), make=Fake)
    assert not more and made[0].down


# -- sizeup's side of the handover ----------------------------------------------------------------------------------------------


def finder(serial, index=0):
    def find():
        return str(index)
    find.serial, find.label, find.why = serial, serial.upper(), ""  # type: ignore[attr-defined]
    return find


def test_the_stations_recorder_writes_the_systems_own_state(tmp_path, monkeypatch):
    monkeypatch.setattr(claims, "helpers", lambda: {4242})
    d = system_dir(tmp_path)
    r = SystemUnitRecorder("mrrs", "MRRS", "/x/trunk-recorder --config=config.json", str(d),
                           state_path=str(tmp_path / "state" / "p25_recorder.mrrs.json"),
                           status_path=str(tmp_path / "run" / "status.mrrs.json"), wait_s=0.2)
    try:
        assert claims.helpers() == {4242}                    # the county's service pid still counts as ours
        r.give(0, finder("pd-a"), "MRRS 770", apply=False)
        r.give(1, finder("pd-b"), "MRRS 773", apply=False)
        assert not (tmp_path / "state" / "p25_recorder.mrrs.json").exists()      # start(): once, with both
        r.start()
        w = trunkrecd.wanted_system(str(tmp_path / "state" / "p25_recorder.mrrs.json"))
        assert w["on"] and w["sources"] == {0: {"sdr": "pd-a", "mode": "MRRS 770", "name": "PD-A"},
                                            1: {"sdr": "pd-b", "mode": "MRRS 773", "name": "PD-B"}}
        assert (r.source_on(0), r.source_on(1), r.on()) == ("pd-a", "pd-b", "0=pd-a,1=pd-b")
        assert r.status()["state"] == "down" and r.status()["label"] == "MRRS"   # (no service in this test)
        r.take(0, "moved")
        assert trunkrecd.wanted_system(str(r.state_path))["sources"].keys() == {1} and r.source_on(0) is None
        r.take(1, "off")
        assert not trunkrecd.wanted_system(str(r.state_path))["on"] and r.on() is None
    finally:
        r.shutdown()


def test_a_bench_recorder_resolves_every_source_or_waits(tmp_path):
    d = system_dir(tmp_path)
    r = SystemRecorder("mrrs", "MRRS", "~/tr/trunk-recorder --config=config.json", str(d))
    assert r.argv[-1] == "--config=config.json" and r.device_fn() is None
    r.sources = {0: (finder("pd-a", 4), "MRRS 770"), 1: (finder("pd-b", 5), "MRRS 773")}
    assert r.device_fn() == {0: "4", 1: "5"}
    assert r._argv_for(r.device_fn())[-1] == "--config=config.runtime.json"
    gone = finder("pd-b")
    r.sources[1] = (lambda: None, "MRRS 773")
    r.sources[1][0].__dict__.update(serial="pd-b", label="PD-B", why="")
    assert r.device_fn() is None and r.device_name == "PD-B" and gone.serial == "pd-b"


# -- two systems in one app ---------------------------------------------------------------------------------------------------


DONGLES = [(0, "Blog V4", "v4"), (1, "RTL2838UHIDIR", "pd-a"), (2, "RTL2838UHIDIR", "pd-b"), (3, "RTL2838UHIDIR", "fg")]


@pytest.fixture
def two(tmp_path, monkeypatch):
    """(app, said, logs): an app with the county system and MRRS, speech-to-text a spy, no dongle opened."""
    d = system_dir(tmp_path)
    plugged = list(DONGLES)
    logs: list[str] = []
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    monkeypatch.setattr(appmod, "SDR_WATCH_S", 3600.0)
    monkeypatch.setattr(appmod, "SDR_SETTLE_S", 0.0)
    monkeypatch.setattr(appmod, "log", logs.append)
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    monkeypatch.setattr(appmod.sdr_lookup, "resolve", lambda serial, *a: next((i for i, _p, s in plugged if s == serial), None))
    monkeypatch.setattr(TrunkRecorder, "one_dongle", lambda self: (True, ""))
    (tmp_path / "clips").mkdir(exist_ok=True)
    system = systems.discover(d.parent.parent, {307, 301})[0]
    c = replace(app_cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"),
                channels=(ChannelCfg("FG1", 453.0375, "traffic", "fire-fg"),), sdr_auto=True, sdr_auto_assign=True,
                p25_dir=str(tmp_path / "captures" / "p25"), p25_talkgroups={307: "Fire/EMS 15", 301: "60 Control"},
                p25_tg_info={307: {"name": "Fire/EMS 15", "group": "Fire/EMS", "encrypted": False},
                             301: {"name": "60 Control", "group": "Fire/EMS", "encrypted": False}},
                p25_recorder=str(tmp_path / "no-trunk-recorder") + " --config=config.json",
                p25_recorder_dir=str(d.parent.parent), p25_systems=(system,))
    said: list[str] = []

    class Spy(FakeWhisper):
        def transcribe(self, audio, prompt=""):
            said.append(prompt)
            return "words", 0.9

    app = appmod.App(c)
    app.stt.client = Spy({})
    app.stt.start()
    yield app, said, logs
    app.stop()


def a_call(app, name, tg, system, radio="4100001", label="x", n=2.0):
    now = time.time()
    return Call(f"{tg}-{name}", tg, label, now, now + n, [radio], voice(n, 1), "", [Turn(0.0, radio, radio, "", at=now)],
                system=system)


def rows(app):
    return {r["id"]: r for r in app.cad.store.q("SELECT * FROM radio_traffic")}


def test_two_systems_calls_are_tagged_and_the_other_systems_stay_context(two):
    app, said, _logs = two
    rt = app.systems["mrrs"]
    assert app.modes == ["FG1", "Fire/EMS 15", "MRRS 770", "MRRS 773", "Off"]
    # a radio ID the county registry knows as one of our rigs: on MRRS it is someone else's number
    app.radios.save({"system": "p25", "radio_id": "4100001", "unit": "E61"})
    app._call(a_call(app, "1-call_1", 307, "westchester", label="Fire/EMS 15"))
    app._call_system(rt, a_call(app, "2-call_2", PD, "mrrs", label="PD Dispatch"))
    app._call_system(rt, a_call(app, "3-call_3", 9049, "mrrs", label="MTA PD D6-7-11 Tac 1"))      # record only
    app._call_system(rt, a_call(app, "4-call_4", PD_SECURE, "mrrs", label="PD Secure"))            # never recorded
    assert wait(lambda: len([r for r in rows(app).values() if r["status"] != "pending"]) == 3)
    got = rows(app)
    fire, pd, tac = got["307-1-call_1"], got["9211-2-call_2"], got["9049-3-call_3"]
    assert (fire["source"], fire["system"], fire["ours"], fire["service"], fire["rig"]) == (
        "p25:westchester:307", "westchester", 1, "fire", "E61")
    assert {k: pd[k] for k in ("source", "system", "group", "service", "ours", "dept", "tier", "safety", "rig",
                               "talkgroup", "channel", "radio_id", "status")} == {
        "source": "p25:mrrs:9211", "system": "mrrs", "group": "Police", "service": "police", "ours": 0, "dept": "",
        "tier": "alert", "safety": "", "rig": "", "talkgroup": PD, "channel": "PD Dispatch", "radio_id": "4100001",
        "status": "done"}
    assert (tac["tier"], tac["status"], tac["text"], tac["dept"]) == ("record", "off", "", "MTA PD")   # kept, no STT
    assert len(said) == 2 and "9213-4-call_4" not in got
    assert app.has_clip("9049-3-call_3") and not app.has_clip("9213-4-call_4")
    # never on our incident, never in the registry, never an alarm
    assert all(not t.id.startswith("9") for i in app.store.incidents for t in i.traffic)
    assert {t.id for t in app.store.loose} >= {"9211-2-call_2", "9049-3-call_3"}
    assert not [r for r in app.cad.store.q("SELECT * FROM radio_links")]
    assert not app.cad.store.q("SELECT * FROM alarms")
    tg = {r["source"]: r for r in app.state()["talkgroups"]}
    assert (tg["p25:mrrs:9211"]["count"], tg["p25:mrrs:9211"]["tier"], tg["p25:mrrs:9211"]["tier_set"]) == (1, "alert", True)
    assert (tg["p25:mrrs:9213"]["dropped"], tg["p25:westchester:307"]["tier"], tg["p25:westchester:307"]["tier_set"]) == (
        1, "text", False)
    # per-minute activity for every source that logged a row
    assert {r["source"] for r in app.cad.comms.activity_rows(0)} == {"p25:westchester:307", "p25:mrrs:9211", "p25:mrrs:9049"}


def test_stt_class_follows_the_tier_and_never_passes_fire_traffic(two):
    app, _said, _logs = two
    t = a_call(app, "x", PD, "mrrs")
    from sizeup.incidents import Traffic
    item = Traffic("i", "x", 0, 1, "", talkgroup=PD)
    assert app._stt_priority(item, "tg:307") == URGENT
    assert LIVE < app._stt_priority(item, "p25:mrrs:9211") < LOW and app._stt_priority(item, "p25:mrrs:9212") == LOW
    assert app.channel_pref("p25:mrrs:9211")["transcribe"] and not app.channel_pref("p25:mrrs:9049")["transcribe"]
    assert app.channel_pref("p25:mrrs:9211")["engine"] == "local" and t.system == "mrrs"
    assert app.set_talkgroup(PD, None, None, "record", "mrrs") is None
    assert not app.channel_pref("p25:mrrs:9211")["transcribe"]
    assert "no talkgroup 307 on mrrs" in app.set_talkgroup(307, None, None, "alert", "mrrs")
    assert app.set_talkgroup(307, None, None, "alert") and app.tg.main(307)      # the county's has no tier


def test_dongles_are_given_to_a_system_in_settings_never_by_auto_assign(two):
    app, _said, logs = two
    app.start()
    assert {sd: app.assign.get(sd, "Off") for _i, _p, sd in DONGLES} == {
        "v4": "FG1", "pd-a": "Fire/EMS 15", "pd-b": "Off", "fg": "Off"}                         # auto: our own jobs only
    rt = app.systems["mrrs"]
    assert rt.status(app.assign.values())["state"] == "off" and not rt.recorder.sources
    assert app.set_mode("MRRS 770", "pd-b") is None and app.set_mode("MRRS 773", "fg") is None
    assert (rt.recorder.source_on(0), rt.recorder.source_on(1)) == ("pd-b", "fg")
    assert json.loads((claims.DIR / "pd-b.json").read_text())["mode"] == "MRRS 770"
    st = app.state()
    assert [r["label"] for r in st["radios"]][:3] == ["FG1", "Fire/EMS 15", "MRRS"]
    sysrow = st["p25_systems"][0]
    assert [(j["job"], j["sdr"], j["mhz"]) for j in sysrow["jobs"]] == [("MRRS 770", "pd-b", 770.3125), ("MRRS 773", "fg", 773.08125)]
    assert len(sysrow["covered"]) == 7 and sysrow["name"] == "mrrs" and sysrow["site"]["nac"] == "051"
    assert app.recorder.on() == "pd-a"                                           # the county's: as it was
    assert app.set_mode("Off", "fg") is None
    assert rt.recorder.source_on(1) is None and rt.recorder.source_on(0) == "pd-b"
    assert len(app.state()["p25_systems"][0]["covered"]) == 4 and not (claims.DIR / "fg.json").exists()
    assert "1 of 2 dongles" in rt.status(app.assign.values())["detail"] or rt.status(app.assign.values())["state"] != "ok"
    from sizeup.cloud.health import Metrics, station_metrics
    jobs = station_metrics(app, Metrics(app.clips_dir))["jobs"]
    assert jobs["MRRS 770"]["expected"] and not jobs["MRRS 773"]["expected"] and jobs["MRRS 773"]["state"] == "off"


def test_ingest_takes_only_the_systems_own_talkgroups(two, tmp_path):
    app, _said, _logs = two
    rt = app.systems["mrrs"]
    d = Path(rt.system.capture_dir) / "mrrs" / "2026" / "11" / "10"
    d.mkdir(parents=True)
    for name, tg in (("9211-1790954731.970_770181250.1-call_5", PD), ("307-1790954731.970_770181250.1-call_6", 307)):
        tr_call(d, name, [(4100002, 1.5, 0.3, ""), (-1, 1.0, 0.0, "")], tg=tg)
        meta = json.loads((d / f"{name}.json").read_text())
        meta.update(short_name="mrrs", talkgroup_tag="PD Dispatch", freq=770181250)
        (d / f"{name}.json").write_text(json.dumps(meta))
    rt.ingest.scan()
    calls = rt.ingest.scan()
    # MRRS's own talkgroup 307 is not in its file: never taken (and never the county's Fire/EMS 15)
    assert [(c.talkgroup, c.label, c.system, len(c.turns)) for c in calls] == [(PD, "PD Dispatch", "mrrs", 2)]
    assert rt.ingest.record_all is False and app.p25.dir != rt.ingest.dir and T0


def test_unknown_talkgroups_are_counted_and_encrypted_ones_are_activity_only(two):
    app, _said, logs = two
    rt = app.systems["mrrs"]
    now = [1_800_000_000.0]
    rt.activity.clock = lambda: now[0]
    for line in ("SIZEUP-UNIT mrrs 4100007 call 9999", "SIZEUP-UNIT mrrs 4100008 call 9999",
                 "SIZEUP-UNIT mrrs 4100009 call 9213", "SIZEUP-UNIT mrrs 0 call 9213",
                 "SIZEUP-UNIT westchester 4100009 call 9213", "SIZEUP-UNIT mrrs 4100001 call 9211"):
        rt.recorder.on_line(line)
    st = rt.state(app.assign)
    assert [(u["tg"], u["source"], u["count"]) for u in st["unknown"]] == [(9999, "p25:mrrs:9999", 2)]
    assert sum("unknown talkgroup seen: 9999" in m for m in logs) == 1
    assert [(e["tg"], e["name"]) for e in st["encrypted"]] == [(9213, "PD Secure")] * 2 and "4100009" not in json.dumps(st)
    assert not app.cad.comms.activity_rows(0)                 # the minute isn't over
    now[0] += 61
    rt.activity.flush()
    assert app.cad.comms.activity_rows(0) == [{"source": "p25:mrrs:9213", "minute": 1_800_000_000 // 60, "calls": 2,
                                               "seconds": 0.0, "radios": "4100009"}]
    assert not rows(app)                                      # never a transmission row
    assert {r["source"]: r["count"] for r in app.state()["talkgroups"]}["p25:mrrs:9213"] == 2
    # the grants also name the key-ups of that system's finished calls, and only that system's
    assert [g[1] for g in rt.live.grants("mrrs", 9211, 0, 2e9)] == ["4100001"] and not app.live.grants("", 9211, 0, 2e9)


# -- the radio log (the hub) ------------------------------------------------------------------------------------------------


def test_another_systems_units_never_link_traffic_to_our_incidents(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E60"], clock)
    clock.t += 5
    t = clock.t
    ours = tx("307-9-call_4-t0", t, "Fire/EMS 15", "E60", "E60", 307, radio_id="6001")
    assert c.record_traffic(ours) == [a["id"]]
    # the same rig name and radio ID on another system (a police car "60", a colliding radio ID): no link by any rule
    theirs = dict(tx("9211-9-call_5-t0", t, "PD Dispatch", "E60", "E60", 9211, radio_id="6001"), system="mrrs",
                  source_key="p25:mrrs:9211", group="Police", service="police", tier="alert", ours=True)
    assert c.record_traffic(theirs) == []
    row = c.store.one("SELECT * FROM radio_traffic WHERE id = ?", ("9211-9-call_5-t0",))
    assert (row["source"], row["system"], row["ours"], row["tier"]) == ("p25:mrrs:9211", "mrrs", 0, "alert")
    assert c.radiolog.matches(row) == {}
    c.store.db.execute("UPDATE radio_traffic SET rig = '' WHERE id = ?", ("9211-9-call_5-t0",))
    assert c.comms.relink("p25", "6001", "E60") == []                         # naming a county radio: not its rows
    assert c.store.one("SELECT rig FROM radio_traffic WHERE id = ?", ("9211-9-call_5-t0",))["rig"] == ""
    assert c.comms.link("9211-9-call_5-t0", a["id"], "op").get("error") is None   # a person still can
    assert all(r["id"] != "9211-9-call_5-t0" for r in c.comms_query({})["rows"])      # nobody signed in: not read out
    with sourcekeys.request_context(object()):
        got = c.comms_query({})["rows"]
    pd = next(r for r in got if r["id"] == "9211-9-call_5-t0")
    assert {"source", "system", "group", "service", "ours", "dept", "tier", "safety"} <= set(pd)


def test_tier_retention_keeps_a_clip_past_the_usual_hours(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    day = 86400.0
    for tid, tier, age in (("a", "record", 3 * day), ("b", "record", 9 * day), ("c", "text", 3 * day), ("d", "alert", 20 * day)):
        c.record_traffic(dict(tx(f"9211-{tid}", clock.t - age, "PD Dispatch", "", "", 9211), system="mrrs",
                              source_key="p25:mrrs:9211", tier=tier))
    ids = ["9211-a", "9211-b", "9211-c", "9211-d"]
    assert c.comms.kept_for_tier(ids, {"alert": 30.0, "text": 0.0, "record": 7.0}) == {"9211-a", "9211-d"}
    per_day = c.comms.audio_per_day(30.0)
    assert set(per_day) == {"record", "text", "alert"} and per_day["record"] > per_day["text"] > 0


def test_rows_from_before_get_their_source_by_the_county_system(tmp_path):
    from sizeup.cad.store import Store
    path = tmp_path / "old.sqlite"
    s = Store(str(path))
    cols = ("source", "system", "group", "service", "ours", "dept", "tier", "safety")
    s.db.execute("INSERT INTO radio_traffic (id, start, end, channel, talkgroup, updated_at) VALUES ('t1', 1, 2, 'Fire/EMS 15', 307, 1)")
    s.db.execute("INSERT INTO radio_traffic (id, start, end, channel, talkgroup, updated_at) VALUES ('f1', 1, 2, 'FG1', NULL, 1)")
    s.db.execute("INSERT INTO radio_traffic (id, start, end, channel, talkgroup, mhz, updated_at) VALUES ('a1', 1, 2, 'HPN Tower', NULL, 118.575, 1)")
    s.db.commit()
    s.db.close()
    db = sqlite3.connect(path)                       # as it was before this release: none of the columns
    for index in ("source_start", "service_start", "system_start", "dept_start", "safety"):     # the Radio page's too (#415)
        db.execute(f"DROP INDEX radio_traffic_{index}")
    for col in cols:
        db.execute(f'ALTER TABLE radio_traffic DROP COLUMN "{col}"') if col != "source" else None
    db.execute("ALTER TABLE radio_traffic DROP COLUMN source")
    db.commit()
    db.close()
    s = Store(str(path))
    got = {r["id"]: (r["source"], r["system"], r["service"], r["ours"]) for r in s.q("SELECT * FROM radio_traffic")}
    assert got == {"t1": ("p25:westchester:307", "westchester", "", 0), "f1": ("fm:FG1", "analog", "fire", 1),
                   "a1": ("air:HPN Tower", "airband", "air", 0)}


def test_our_own_rules_name_none_of_the_other_systems_talkgroups():
    """Status hints, the 60 Control feed and the incident talkgroups are allow-lists of county talkgroups."""
    from sizeup.cad import statushints
    c = load(ROOT / "config.example.toml")
    mine = set(MRRS.talkgroups)
    assert not mine & set(statushints.Cfg().talkgroups) and not mine & set(c.p25_incident_talkgroups)
    assert not mine & set(c.p25_talkgroups) and not {"MRRS 770", "MRRS 773"} & set(c.sdr_auto_order)
    assert all(not k.startswith("p25:") for k in appmod.STT_FIRST)


def test_the_installer_and_the_hub_image_carry_the_systems():
    sh = (ROOT / "deploy" / "station" / "install-linux.sh").read_text()
    assert 'deploy/trunk-recorder/systems' in sh and 'c["captureDir"] = f"{captures}/p25-{name}"' in sh
    assert "COPY deploy/trunk-recorder/systems" in (ROOT / "Dockerfile").read_text()
    assert "!deploy/trunk-recorder/systems" in (ROOT / ".dockerignore").read_text()
    units = json.loads((ROOT / "data" / "pd_units.json").read_text())
    assert "mrrs" in units["systems"] and "radio_id" not in json.dumps(units["systems"]["mrrs"]["callsigns"])
    # the only radio IDs in the repo: the two headquarters seeds the wiki publishes
    assert [(x["radio_id"], x["said"], x["confidence"]) for x in units["systems"]["mrrs"]["radio_ids"]] == [
        ("4242098", "908", "low"), ("4242396", "266", "low")]
    assert np.isfinite(1.0)


# -- the review of #419: context audio, disk and speech-to-text never cost the fire side anything ------------------------------


def test_a_bad_system_directory_is_left_out_and_the_rest_starts(tmp_path):
    shutil.copytree(TR, tmp_path / "tr", ignore=shutil.ignore_patterns("*.wav", "westchester"))
    make_system(tmp_path / "tr", "bytes", jobs=("Bytes 1",))
    (tmp_path / "tr" / "systems" / "bytes" / "talkgroups.csv").write_bytes(b"Decimal,Alpha Tag\n1,\xff\xfe\n")
    make_system(tmp_path / "tr", "named", tgs="4000,fa0,FG1,T,d,Police,X,1,record\n", jobs=("Named 1",))
    make_system(tmp_path / "tr", "nested", tgs="4001,fa1,Y,T,d,Police,X,1,record\n", jobs=("Nested 1",),
                captureDir=str(tmp_path / "cap" / "p25" / "inside"))
    conf = tmp_path / "c.toml"
    conf.write_text(f'[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\nkind = "traffic"\n'
                    f'[p25]\ncapture_dir = "{tmp_path / "cap" / "p25"}"\nrecorder_dir = "{tmp_path / "tr"}"\n'
                    f'talkgroups_file = "{tmp_path / "tr" / "talkgroups.csv"}"\n')
    c = load(conf)
    assert [s.name for s in c.p25_systems] == ["mrrs"] and 307 in c.p25_talkgroups       # fire as ever
    why = " | ".join(c.p25_system_problems)
    assert "bytes" in why and "Unicode" in why and "talkgroup name 'FG1'" in why and "inside it" in why
    # the lists that name our own talkgroups are taken too: the airport incident's number on another system
    assert systems.discover(tmp_path / "tr", {333}, problems=[]) and len(c.p25_system_problems) == 3
    f = tmp_path / "tr" / "systems" / "mrrs" / "talkgroups.csv"
    f.write_text(f.read_text() + '333,14d,Clash,T,"x",Police,MRRS,3,record,police,\n')
    c = load(conf)
    assert not c.p25_systems and any("talkgroup 333 is already" in x for x in c.p25_system_problems)


def test_over_the_cap_context_clips_go_first_and_a_fire_clip_is_kept(tmp_path):
    from sizeup.cloud.hubstore import AudioDb
    clock = Clock()
    c = cad(tmp_path, clock)
    days = {"alert": 30.0, "text": 0.0, "record": 7.0}
    wav = b"RIFF" + b"\0" * 996
    audio = AudioDb(":memory:", 4000)                                    # room for four clips
    for k, (tier, age_h) in enumerate([("record", 40), ("record", 30), ("alert", 20), ("alert", 10), ("record", 2)]):
        tid = f"9211-c{k}"
        c.record_traffic(dict(tx(tid, clock.t - age_h * 3600, "PD Dispatch", "", "", 9211), system="mrrs",
                              source_key="p25:mrrs:9211", tier=tier))
        audio.put(tid, wav, at=clock.t - age_h * 3600)
    c.record_traffic(dict(tx("307-fire", clock.t - 60, "Fire/EMS 15", "E60", "E60", 307), system="westchester", ours=True))
    audio.put("307-fire", wav, at=clock.t - 60)                          # the newest clip, and over the cap now
    ctx = lambda ids: c.comms.context_clips(ids, days)                  # noqa: E731
    assert set(ctx(["9211-c0", "9211-c2", "307-fire"])) == {"9211-c0", "9211-c2"}
    # as #419 first had it (tier-kept clips exempt from the cap) the fire clip was the one deleted
    audio.prune_clips(clock.t - 12 * 3600, kept=c.comms.kept_for_events, context=ctx, context_max_bytes=10_000)
    assert audio.has("307-fire")
    assert [audio.has(f"9211-c{k}") for k in range(5)] == [False, False, True, True, True]   # oldest record tier first
    # context audio has its own budget, whatever room the cap has
    audio.max_bytes = 100_000
    audio.prune_clips(clock.t - 12 * 3600, kept=c.comms.kept_for_events, context=ctx, context_max_bytes=2000)
    assert [audio.has(f"9211-c{k}") for k in range(5)] == [False, False, True, True, False] and audio.has("307-fire")
    # a tier only extends past the usual hours: the text tier's old clip goes, the alert tier's stays
    c.record_traffic(dict(tx("9212-old", clock.t - 20 * 3600, "PD Tac", "", "", 9212), system="mrrs",
                          source_key="p25:mrrs:9212", tier="text"))
    audio.put("9212-old", wav, at=clock.t - 20 * 3600)
    audio.prune_clips(clock.t - 12 * 3600, kept=c.comms.kept_for_events, context=ctx, context_max_bytes=10_000)
    assert not audio.has("9212-old") and audio.has("9211-c2")
    # ... and with only fire clips over the cap, the oldest fire clip goes as before
    for k in range(6):
        audio.put(f"307-f{k}", wav, at=clock.t - 50 + k)
    audio.max_bytes = 3000
    audio.prune_clips(clock.t - 12 * 3600, kept=c.comms.kept_for_events, context=ctx, context_max_bytes=10_000)
    assert not any(audio.has(f"9211-c{k}") for k in range(5)) and audio.has("307-f5") and not audio.has("307-fire")


def test_the_janitor_cleans_every_systems_captures_within_a_budget(tmp_path):
    import os
    from sizeup import janitor
    cap, tmp = tmp_path / "captures" / "p25-mrrs", tmp_path / "captures" / "p25-mrrs-tmp"
    day = cap / "mrrs" / "2026" / "11" / "9"
    day.mkdir(parents=True)
    tmp.mkdir()
    now = 1_800_000_000.0

    def put(p, age_s, size=1000):
        p.write_bytes(b"x" * size)
        os.utime(p, (now - age_s, now - age_s))

    put(day / "9211-old.wav", 20 * 3600), put(day / "9211-old.json", 20 * 3600, 50)      # made while sizeup was down
    put(day / "9999-orphan.json", 20 * 3600, 50)                                         # its audio never came
    put(tmp / "stale.wav", 2 * 3600), put(tmp / "live.wav", 60)
    empty = cap / "mrrs" / "2026" / "11" / "8"
    empty.mkdir()
    fresh = cap / "mrrs" / "2026" / "11" / "10"
    fresh.mkdir()
    for k in range(5):
        put(fresh / f"9211-{k}.wav", 600 - k * 60), put(fresh / f"9211-{k}.json", 600 - k * 60, 50)
    got = janitor.clean_system(cap, 12 * 3600, max_bytes=3200, now=now)
    assert not day.exists() and not empty.exists() and not (tmp / "stale.wav").exists() and (tmp / "live.wav").exists()
    left = sorted(p.name for p in fresh.glob("*.wav"))
    assert left == ["9211-2.wav", "9211-3.wav", "9211-4.wav"] and got["over"] == 2 and got["bytes"] <= 3200
    assert janitor.clean_system(tmp_path / "none", 3600) == {"deleted": 0, "over": 0, "bytes": 0}


def test_the_app_cleans_its_systems_capture_directories(two):
    import os
    app, _said, _logs = two
    d = Path(app.systems["mrrs"].system.capture_dir) / "mrrs" / "2026" / "11" / "1"
    d.mkdir(parents=True)
    (d / "9211-x.wav").write_bytes(b"x" * 100)
    old = time.time() - 30 * 3600
    os.utime(d / "9211-x.wav", (old, old))
    app._clean()
    assert not d.exists()


def test_context_speech_to_text_is_under_every_class_of_ours_and_is_shed(two):
    from sizeup import stt
    from sizeup.incidents import Traffic
    from sizeup.radio.page import Page
    app, _said, _logs = two
    appmod.AIRBAND_FIRST.add("HPN Tower")
    try:
        t = Traffic("i", "x", 0, 1, "")
        ours = [app._stt_priority(Page.__new__(Page), "South 2"),          # a page
                app._stt_priority(t, "tg:307"), app._stt_priority(t, "FG1"), app._stt_priority(t, "FG2"),
                app._stt_priority(t, "HPN Tower"), app._stt_priority(t, "South 2"),      # voice on the paging channel
                stt.RERUN]                                                                # a person's re-run
        app.tg.set(301, main=True)
        ours.append(app._stt_priority(t, "tg:301"))                                       # a Main talkgroup
    finally:
        appmod.AIRBAND_FIRST.discard("HPN Tower")
    alert, text = app._stt_priority(t, "p25:mrrs:9211"), app._stt_priority(t, "p25:mrrs:9212")
    assert alert == stt.CONTEXT and text == stt.LOW and all(p < alert <= text for p in ours)
    assert stt.SHED_AFTER[stt.CONTEXT] <= 600 and stt.CONTEXT not in stt.LIVE_CLASSES     # shed; never "behind" by it
    # in the queue itself: a fire clip that comes later is still taken first, and a waiting context clip is shed
    clock, dropped = {"t": 100.0}, []
    q = stt.SttQueue(lambda item, why: dropped.append((item, why)), clock=lambda: clock["t"])
    q.put((stt.CONTEXT, None, "pd", None, "p25:mrrs:9211"))
    q.put((stt.LOW, None, "county-context", None, "tg:383"))
    q.put((stt.LIVE, None, "fire", None, "tg:301"))
    q.put((stt.URGENT, None, "page", None, "South 2"))
    assert [q.get_nowait()[2] for _ in range(4)] == ["page", "fire", "pd", "county-context"]
    q.put((stt.CONTEXT, None, "pd-late", None, "p25:mrrs:9211"))
    clock["t"] += stt.CONTEXT_MAX_WAIT_S + 1
    q.put((stt.LIVE, None, "fire-2", None, "tg:301"))
    assert q.get_nowait()[2] == "fire-2" and dropped and dropped[0][0] == "pd-late" and dropped[0][1].startswith(stt.SHED)


def test_another_systems_supervisor_never_takes_or_deletes_the_countys_claim(tmp_path):
    county, mrrs, pop, _logs, _d = service(tmp_path)
    ask_county(tmp_path)
    assert county.step(100.0)["state"] == "ok"
    mine = json.loads((claims.DIR / "v4.json").read_text())
    assert (mine["mode"], mine["pid"]) == ("Fire/EMS 15", SERVICE_PID)
    # a stale state file of MRRS's names the county's dongle (the same service pid holds its claim)
    ask_system(tmp_path, {0: "v4"})
    st = mrrs.step(101.0)
    assert st["state"] == "held" and "Fire/EMS 15" in st["detail"] and len(pop.started) == 1
    assert json.loads((claims.DIR / "v4.json").read_text()) == mine
    ask_system(tmp_path, {}, on=False, seq=2)                    # MRRS switched off: the county's claim stays
    assert mrrs.step(102.0)["state"] == "off"
    assert json.loads((claims.DIR / "v4.json").read_text()) == mine and county.step(102.0)["state"] == "ok"
    mrrs.shutdown()
    assert json.loads((claims.DIR / "v4.json").read_text()) == mine
    # a dongle claimed under another job by this same pid (the county's, before its state says so): not taken either
    (tmp_path / "state" / "p25_recorder.json").unlink()
    ask_system(tmp_path, {0: "v4", 1: "pd-b"}, seq=3)
    assert mrrs.step(103.0)["state"] == "held" and not (claims.DIR / "pd-b.json").exists()
    assert json.loads((claims.DIR / "v4.json").read_text()) == mine
    # and no claim file is written and removed while it waits
    before = sorted(p.name for p in claims.DIR.iterdir())
    for k in range(5):
        mrrs.step(104.0 + k)
    assert sorted(p.name for p in claims.DIR.iterdir()) == before


def test_the_same_files_written_again_restart_nothing_and_it_runs_at_a_lower_priority(tmp_path):
    _county, mrrs, pop, _logs, d = service(tmp_path)
    ask_system(tmp_path, {0: "pd-a"})
    assert mrrs.step(100.0)["state"] == "ok"
    assert "preexec_fn" not in pop.started[0].kw            # (never code between fork and exec: threads)
    (d / "config.json").write_text((d / "config.json").read_text())           # an update installs the same file again
    (d / "talkgroups.csv").write_text((d / "talkgroups.csv").read_text())
    assert mrrs.step(101.0)["state"] == "ok" and len(pop.started) == 1
    (d / "talkgroups.csv").write_text((d / "talkgroups.csv").read_text() + "9999,270f,New,T,x,Police,MRRS,3,record,police,\n")
    mrrs.step(102.0)
    assert mrrs.step(103.0)["state"] == "ok" and len(pop.started) == 2        # a real change: its own restart


def test_transcript_content_on_another_system_raises_nothing_of_ours(two):
    app, _said, _logs = two
    rt = app.systems["mrrs"]
    app.cad.incident({"type": "STRUCTURE FIRE", "address": "12 Invented Ave"}, "op") if hasattr(app.cad, "incident") else None
    app._call_system(rt, a_call(app, "7-call_7", PD, "mrrs", label="PD Dispatch"))
    assert wait(lambda: rows(app).get("9211-7-call_7", {}).get("status") == "done")
    for text in ("Mayday mayday mayday, Engine 61 trapped on the second floor", "Engine 61 on location",
                 "60 Control to Engine 61 respond to 12 Invented Ave for a structure fire", "emergency button activated"):
        assert app._retext("9211-7-call_7", text, "done", 0.9, "m.bin") in (True, False)
    assert rows(app)["9211-7-call_7"]["text"] == "emergency button activated"
    assert not app.cad.store.q("SELECT * FROM alarms") and not app.cad.store.q("SELECT * FROM radio_links")
    assert not app.cad.store.q("SELECT * FROM status_hints") and not app.cad.store.q("SELECT * FROM incoming")
    assert rows(app)["9211-7-call_7"]["dispatcher"] == 0
    # an encrypted call on a talkgroup the file lists as clear: nothing kept
    enc = a_call(app, "8-call_8", PD, "mrrs", label="PD Dispatch")
    enc.encrypted = True
    app._call_system(rt, enc)
    assert "9211-8-call_8" not in rows(app) and not app.has_clip("9211-8-call_8")
    # encrypted activity as the browser gets it: when and which talkgroup, never which radio
    rt.recorder.on_line("SIZEUP-UNIT mrrs 4100009 call 9213")
    assert app.state()["p25_systems"][0]["encrypted"] == []                   # nobody signed in here: not even that
    app.access_enforced = True
    assert app.state()["p25_systems"][0]["encrypted"][0].keys() == {"at", "tg", "name", "source"}


def test_the_hub_never_gives_another_systems_radio_a_rig_or_a_dispatcher(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    hub.radios.save({"system": "p25", "radio_id": "4100001", "unit": "E61"})
    now = time.time()
    base = {"start": now, "end": now + 2, "text": "", "raw": "Engine 61 on location", "status": "done", "unit": "60 Ctrl OP 6",
            "radio_id": "4100001", "pairs": [["4100001", "60 Ctrl OP 6"]], "model": "m.bin"}
    t = hub._traffic_of({**base, "id": "307-h1", "source": "Fire/EMS 15", "talkgroup": 307})
    assert t.rig == "E61"
    for d in ({**base, "pairs": None}, base):
        t = hub._traffic_of({**d, "id": "9211-h2", "source": "PD Dispatch", "talkgroup": 9211, "system": "mrrs",
                             "source_key": "p25:mrrs:9211", "tier": "alert", "group": "Police", "service": "police"})
        assert t.rig == "" and t.system == "mrrs"
    hub.apply("traffic", {**base, "id": "9211-h2", "source": "PD Dispatch", "talkgroup": 9211, "system": "mrrs",
                          "source_key": "p25:mrrs:9211", "tier": "alert", "group": "Police", "service": "police",
                          "attach": True}, None)
    row = hub.cad.store.one("SELECT * FROM radio_traffic WHERE id = '9211-h2'")
    assert (row["rig"], row["dispatcher"], row["ours"], row["system"], row["source"]) == ("", 0, 0, "mrrs", "p25:mrrs:9211")
    assert not hub.cad.store.q("SELECT * FROM alarms") and all(x.id != "9211-h2" for i in hub.store.incidents for x in i.traffic)
    hub.apply("activity", {"source": "p25:mrrs:9213", "minute": int(now // 60), "calls": 3, "radios": ["4100009"]}, None)
    assert hub.cad.comms.activity_rows(0, "p25:mrrs:9213")[0]["calls"] == 3
    # radio IDs in the activity table: a week at most
    hub.cad.comms.activity("p25:mrrs:9213", int(now // 60) - 8 * 1440, 1, ["4100009"])
    hub.cad.comms.prune()
    old = [r for r in hub.cad.comms.activity_rows(0, "p25:mrrs:9213") if r["minute"] < int(now // 60) - 1440]
    assert old and all(r["radios"] == "" for r in old)
    # version skew: a station from before this never gets a "p25:" channel key; this hub says what it knows
    hub._chan_ov = {"p25:mrrs:9211": {"engine": "aws"}, "tg:307": {"engine": "local"}}
    assert set(hub.channel_desired()["prefs"]) == {"tg:307"}
    hub.hub.status = {"p25_systems": []}
    assert "p25:mrrs:9211" in hub.channel_desired()["prefs"]
    assert json.loads(hub.hub._state(json.dumps({"at": now}).encode())[1])["caps"] == ["p25_systems"]


def test_the_station_keeps_context_out_of_an_older_hub_and_out_of_the_fire_audio_budget(tmp_path):
    from sizeup.cloud.outbox import Outbox
    from sizeup.cloud.station import StationLink
    box = Outbox(tmp_path / "o.sqlite", max_audio_bytes=10_000)
    for k in range(4):
        box.add("traffic", {"id": f"307-{k}"}, b"f" * 2000)
    for k in range(4):
        box.add("traffic", {"id": f"9211-{k}", "system": "mrrs"}, b"p" * 2000, context=True)
    got = {e["data"]["id"]: e["audio"] is not None for e in box.batch()}
    assert all(got[f"307-{k}"] for k in range(4))                              # a hub outage: no fire clip lost
    assert [got[f"9211-{k}"] for k in range(4)] == [False, False, False, True] and box.context_bytes == 2000
    sent = []

    class Box:
        def add(self, kind, data, audio, context=False):
            sent.append((kind, data.get("id"), context))

    link = StationLink.__new__(StationLink)
    link.outbox, link._wake = Box(), type("W", (), {"set": lambda self: None})()
    link.app = type("A", (), {"clip": lambda self, c: None})()
    link.emit("traffic", {"id": "9211-a", "system": "mrrs"})
    link.emit("activity", {"source": "p25:mrrs:9213"})
    link.emit("traffic", {"id": "307-a"})
    assert sent == [("traffic", "307-a", False)]                               # the hub hasn't said it knows systems
    link.hub_caps = frozenset({"p25_systems"})
    link.emit("traffic", {"id": "9211-b", "system": "mrrs"})
    assert sent[-1] == ("traffic", "9211-b", True)


def test_a_context_system_down_is_never_an_alert_and_details_carry_no_radio_ids(two):
    from sizeup.cloud.health import Metrics, no_ids, station_metrics
    app, _said, _logs = two
    app.start()
    assert app.set_mode("MRRS 770", "pd-b") is None
    rt = app.systems["mrrs"]
    rt.recorder.status = lambda: {"state": "down", "restarts": 4, "detail": "trunk-recorder exited (1): [mrrs] 12C TG: 9211 "
                                  "Freq: 770.181250 MHz Unit ID set via Control Channel, ext: 4100001"}
    jobs = station_metrics(app, Metrics(app.clips_dir))["jobs"]
    assert jobs["MRRS 770"]["context"] is True and "4100001" not in jobs["MRRS 770"]["detail"]
    assert "770.181250 MHz" in jobs["MRRS 770"]["detail"] and "4100001" not in rt.status(app.assign.values())["detail"]
    assert no_ids("Control Channel Message Decode Rate: 38/sec, count: 115") == "Control Channel Message Decode Rate: 38/sec, count: 115"
    src = (ROOT / "sizeup" / "cloud" / "health.py").read_text()
    assert 'if d.get("context"):\n                continue' in src


def test_old_airband_rows_and_a_cut_short_migration_are_put_right(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    c.record_traffic(tx("HPN-1", clock.t, "HPN Tower", "", "", None))
    c.store.db.execute("UPDATE radio_traffic SET source = 'fm:HPN Tower', system = 'analog', service = 'fire', ours = 1")
    assert c.comms.fix_air(["HPN Tower"]) == 1 and c.comms.fix_air(["HPN Tower"]) == 0
    r = c.store.one("SELECT * FROM radio_traffic")
    assert (r["source"], r["system"], r["service"], r["ours"]) == ("air:HPN Tower", "airband", "air", 0)
    c.store.db.execute("UPDATE radio_traffic SET source = ''")            # as a migration cut short left it
    c.store.db.commit()
    from sizeup.cad.store import Store
    assert Store(str(tmp_path / "cad.sqlite")).one("SELECT source FROM radio_traffic")["source"] == "fm:HPN Tower"


# -- the second review of #419 -----------------------------------------------------------------------------------------------


def test_a_running_system_yields_at_once_when_the_county_is_given_its_dongle(tmp_path):
    county, mrrs, pop, logs, _d = service(tmp_path)
    nice = []
    mrrs.setpriority = lambda which, pid, n: nice.append((pid, n))
    ask_system(tmp_path, {0: "pd-a", 1: "pd-b"})
    assert mrrs.step(100.0)["state"] == "ok" and len(pop.running) == 1
    pd = pop.started[0]
    assert nice == [(pd.pid, 5)]                                    # niced by its pid, after it started
    ask_county(tmp_path, sdr="pd-a")                                # Settings: Fire/EMS 15 now on one of MRRS's dongles
    st = mrrs.step(101.0)                                           # MRRS's state still names it: it stops anyway
    assert pd.code is not None and st["state"] == "held" and "Fire/EMS 15" in st["detail"]
    assert not (claims.DIR / "pd-a.json").exists() and not (claims.DIR / "pd-b.json").exists()
    assert county.step(101.5)["state"] == "ok" and len(pop.running) == 1 and pop.running[0] is not pd
    assert json.loads((claims.DIR / "pd-a.json").read_text())["mode"] == "Fire/EMS 15"
    assert mrrs.step(102.0)["state"] == "held" and len(pop.running) == 1          # never two programs on one dongle
    ask_system(tmp_path, {1: "pd-b"}, seq=2)                        # sizeup catches up: MRRS on what is left
    assert mrrs.step(103.0)["state"] == "ok" and len(pop.running) == 2


def test_a_fire_jobs_dongle_goes_to_another_system_only_with_a_confirm(two):
    app, _said, _logs = two
    app.start()
    assert app.assign["v4"] == "FG1" and app.assign["pd-a"] == "Fire/EMS 15"
    for sd, job in (("v4", "FG1"), ("pd-a", "Fire/EMS 15")):
        why = app.set_mode("MRRS 770", sd)
        assert why.startswith("confirm: ") and job in why and app.assign[sd] == job      # nothing moved
    assert app.fire_job("FG1") and app.fire_job("Fire/EMS 15") and not app.fire_job("MRRS 770") and not app.fire_job("Off")
    assert app.set_mode("MRRS 770", "pd-b") is None                 # a free dongle: one click, as any job
    assert app.set_mode("MRRS 773", "v4", confirm=True) is None and app.assign["v4"] == "MRRS 773"
    assert app.set_mode("FG1", "v4") is None                        # back to a fire job: no confirm asked
    src = (ROOT / "sizeup" / "web" / "server.py").read_text()
    assert 'set_mode(str(body["mode"]), sdr_arg, confirm=True) if body.get("confirm") is True' in src


def test_lists_a_person_edits_refuse_a_context_talkgroup_and_the_readers_leave_context_rows(two, tmp_path):
    from sizeup.cad import feed, statushints
    from sizeup.cad.lists import ListsError
    from sizeup.cad.store import CadError
    app, _said, _logs = two
    assert PD in sourcekeys.context_talkgroups() and 307 not in sourcekeys.context_talkgroups()
    f = tmp_path / "lists.json"
    lists = type("L", (), {"path": str(f)})()
    f.write_text(json.dumps({"status_hints": {"talkgroups": [307, PD]}}))
    with pytest.raises(ListsError, match="9211 is another P25 system's"):
        statushints.load_hint_cfg(lists, ("FG1",), (307, 333))
    f.write_text(json.dumps({"comms_feed": {"talkgroups": [301, EMS]}}))
    with pytest.raises(ListsError, match="9570 is another P25 system's"):
        feed.load_cfg(lists)
    for bad in (["FG1", "tg:9211"], ["p25:mrrs:9211"]):
        with pytest.raises(CadError):
            app.cad.alarms.settings_op("set", {"key": "channels", "value": bad}, "op")
    # readers, given a context row that carries one of OUR numbers and names (the worst case a guard has to hold in)
    now = time.time()
    row = dict(tx("307-ctx", now, "Fire/EMS 15", "", "60 Ctrl OP 6", 307, text="Mayday mayday, Engine 61 on location"),
               system="mrrs", source_key="p25:mrrs:307", tier="alert")
    app.cad.record_traffic(row)
    t = app.cad.store.one("SELECT * FROM radio_traffic WHERE id = '307-ctx'")
    assert sourcekeys.is_context(t) and not sourcekeys.is_context({"system": "westchester"}) and not sourcekeys.is_context({})
    assert app.cad.alarms.on_traffic(dict(row, emergency=False)) is None
    assert app.cad.alarms.channel_key(row) == "p25:mrrs:307"
    assert app.cad.sources.consider_radio("307-ctx") is None
    assert all(r["id"] != "307-ctx" for r in app.cad.feed.rows())
    hints = getattr(app.cad, "hints", None) or getattr(app.cad, "statushints", None)
    assert hints.consider("307-ctx") == [] and not app.cad.store.q("SELECT * FROM status_hints")
    assert not app.cad.store.q("SELECT * FROM alarms") and not app.cad.store.q("SELECT * FROM incoming")


def test_county_groups_still_show_beside_another_systems(two):
    app, _said, _logs = two
    tgs = app.state()["talkgroups"]
    county = [r for r in tgs if not r["tier_set"]]
    other = [r for r in tgs if r["tier_set"]]
    assert {r["group"] for r in county} == {"Fire/EMS"} and {r["system"] for r in county} == {"westchester"}
    assert {r["tg"] for r in county} == {307, 301} and len(other) == len(MRRS.talkgroups)
    assert all(r["system"] == "mrrs" and r["tier"] in tiers.TIERS for r in other)
    # Settings > Radios groups by the group name alone for the county's (as before: its sections, order and colours)
    # and by system + group for another system's, so neither swallows the other
    js = (ROOT / "sizeup" / "web" / "static" / "radio-controls.js").read_text()
    assert 'const gk = (r.tier_set ? `${r.system}\\u0001` : "") + (r.group || "");' in js
    assert "key: (x) => `g:${x.g}`" in js and "show(gsw, !other);" in js


def test_no_ids_hub_caps_and_queue_stats(tmp_path):
    from sizeup import stt
    from sizeup.cloud.health import no_ids
    from sizeup.cloud.station import StationLink
    assert no_ids("Unit ID set via Control Channel, ext: 4100001.") == "Unit ID set via Control Channel, ext: #."
    assert no_ids("src 4100001, tg 9211 (4100002)") == "src #, tg 9211 (#)"
    assert no_ids("center 770312500 rate: 2400000 · 476075000 Hz · 770.181250 MHz · 38/sec") == \
        "center 770312500 rate: 2400000 · 476075000 Hz · 770.181250 MHz · 38/sec"
    assert systems.no_ids("exited (1): radio 4100001!") == "exited (1): radio #!"
    link = StationLink.__new__(StationLink)
    link._caps_file = tmp_path / "hub_caps.json"
    link._set_caps(["p25_systems"])
    assert json.loads(link._caps_file.read_text()) == ["p25_systems"] and link.hub_caps == {"p25_systems"}
    src = (ROOT / "sizeup" / "cloud" / "station.py").read_text()
    assert 'with_name("hub_caps.json")' in src and "self.hub_caps = frozenset(str(c) for c in json.loads(self._caps_file.read_text()))" in src
    q = stt.SttQueue(lambda item, why: None)
    q.put((stt.CONTEXT, None, "pd", None, "p25:mrrs:9211"))
    q.put((stt.LIVE, None, "fire", None, "tg:301"))
    assert q.stats()["context"] == 1 and q.stats()["live"] == 1
    api = (ROOT / "sizeup" / "cad" / "api.py").read_text()
    assert '"/api/cad/comms/activity": "radio activity' in api and 'if path == "/api/cad/comms/activity":' in api


# -- another system's traffic is only ever shown to a signed-in person (#419) ---------------------------------------------------


def _serve(app, monkeypatch, access: bool):
    """The app's own web server on an ephemeral port. access: every request verified (a hub behind Cloudflare Access;
    here a stand-in that takes the email from a header), else none (the station's page, all-in-one, a hub without it)."""
    import threading
    from sizeup.cloud.access import Identity

    class Access:
        def identify(self, headers):
            email = headers.get("X-Test-Email")
            return Identity(email=email) if email else None

    monkeypatch.setattr(appmod, "access_verifier", lambda cfg: Access() if access else None)
    server = appmod.make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.httpd.server_address[1]}"


def _get(url, email=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"X-Test-Email": email} if email else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _with_context_traffic(app):
    """One fire transmission and one MRRS one, the MRRS one put on an incident by a person. -> (incident id, ids)."""
    rt = app.systems["mrrs"]
    app._call(a_call(app, "1-call_1", 307, "westchester", label="Fire/EMS 15"))
    app._call_system(rt, a_call(app, "2-call_2", PD, "mrrs", label="PD Dispatch"))
    rt.recorder.on_line("SIZEUP-UNIT mrrs 4100007 call 9999")
    rt.recorder.on_line("SIZEUP-UNIT mrrs 4100009 call 9213")
    assert wait(lambda: len([r for r in rows(app).values() if r["status"] == "done"]) == 2)
    iid = app.cad.create({"type": "STRU", "address": "12 Invented Ave", "operator": "op"})["id"]
    assert app.cad.comms.link("9211-2-call_2", iid, "op").get("error") is None
    assert app.cad.comms.link("307-1-call_1", iid, "op").get("error") is None
    return iid, ("307-1-call_1", "9211-2-call_2")


def _leaks(body) -> bool:
    text = json.dumps(body) if not isinstance(body, (bytes, str)) else str(body)
    return any(x in text for x in ("9211-2-call_2", "PD Dispatch\", \"start", "p25:mrrs:9211\", \"system", "4100009", "9999"))


@pytest.mark.parametrize("role", ["all-in-one", "station", "hub without Access"])
def test_with_no_sign_in_another_systems_traffic_is_not_served_at_all(two, monkeypatch, role):
    app, _said, _logs = two
    assert appmod.access_verifier(replace(app.cfg, cloud_role="station", cloud_access_team="t", cloud_access_aud="a")) is None
    assert appmod.access_verifier(replace(app.cfg, cloud_role="hub")) is None
    iid, (fire, pd) = _with_context_traffic(app)
    server, url = _serve(app, monkeypatch, access=False)
    try:
        assert not app.access_enforced and not app.cad.comms.context_broadcast
        server.broadcaster.poke() if hasattr(server.broadcaster, "poke") else None
        st = app.state()
        assert [t["id"] for t in st["loose_traffic"]] == [fire] and not st["p25_systems"][0]["encrypted"]
        assert not st["p25_systems"][0]["unknown"] and not _leaks(st)
        for path in ("/api/state", "/api/cad/state", "/api/cad/comms", "/api/cad/comms?channel=PD%20Dispatch",
                     "/api/cad/comms?q=words", "/api/cad/feed", *([f"/api/cad/incident/{iid}"] if iid else [])):
            code, body = _get(url + path)
            assert code == 200 and not _leaks(body), path
        code, body = _get(url + "/api/cad/comms")
        assert [r["id"] for r in body["rows"] if r.get("kind") == "radio"] == [fire]
        if iid:
            assert [r["id"] for r in _get(url + f"/api/cad/incident/{iid}")[1]["radio"]] == [fire]
        assert _get(url + f"/api/audio/{fire}.wav")[1][:4] == b"RIFF"
        assert _get(url + f"/api/audio/{pd}.wav")[0] == 404                    # its audio: not served, by any id
        assert _get(url + "/api/audio/9211-never-heard.wav")[0] == 404 and app.clip_is_context("9211-zzz")
        assert app.clip(pd)[:4] == b"RIFF"                                     # still recorded (and forwarded)
        assert rows(app)[pd]["text"] == "words"
    finally:
        server.shutdown()


def test_behind_access_a_signed_in_person_sees_it_and_nobody_else_gets_anything(two, monkeypatch):
    app, _said, _logs = two
    iid, (fire, pd) = _with_context_traffic(app)
    server, url = _serve(app, monkeypatch, access=True)
    me = "dispatcher@example.org"
    try:
        assert app.access_enforced and app.cad.comms.context_broadcast
        for path in ("/api/state", "/api/cad/comms", f"/api/audio/{pd}.wav", "/api/cad/state"):
            assert _get(url + path)[0] == 401, path                            # not signed in: nothing at all
        st = _get(url + "/api/state", me)[1]
        assert {t["id"] for t in st["loose_traffic"]} == {fire, pd}
        got = _get(url + "/api/cad/comms", me)[1]["rows"]
        assert {r["id"] for r in got if r.get("kind") == "radio"} == {fire, pd}
        assert next(r for r in got if r["id"] == pd)["system"] == "mrrs"
        assert _get(url + f"/api/audio/{pd}.wav", me)[1][:4] == b"RIFF"
        if iid:
            assert {r["id"] for r in _get(url + f"/api/cad/incident/{iid}", me)[1]["radio"]} == {fire, pd}
    finally:
        server.shutdown()


def test_the_predicate_is_one_and_defaults_to_no():
    assert sourcekeys.may_see_context() is False and sourcekeys.may_see_context(True) is True
    with sourcekeys.request_context(None):
        assert sourcekeys.may_see_context(True) is False           # a request with no verified person: never
    with sourcekeys.request_context(object()):
        assert sourcekeys.may_see_context(False) is True
    assert sourcekeys.may_see_context() is False


def test_the_hubs_shared_state_carries_context_only_behind_access(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    now = time.time()
    hub.apply("traffic", {"id": "9211-h9", "source": "PD Dispatch", "talkgroup": 9211, "system": "mrrs", "start": now,
                          "end": now + 2, "text": "", "raw": "made-up words", "status": "done", "unit": "", "radio_id": "",
                          "source_key": "p25:mrrs:9211", "tier": "alert", "group": "Police", "service": "police"}, b"RIFF....")
    assert not hub.access_enforced and all(t["id"] != "9211-h9" for t in hub.state()["loose_traffic"])
    assert hub.clip_is_context("9211-h9") and not hub.clip_is_context("307-h1")
    assert all(r["id"] != "9211-h9" for r in hub.cad.comms_query({})["rows"])
    hub.access_enforced = hub.cad.comms.context_broadcast = True                # (make_server: Access verifies everyone)
    assert any(t["id"] == "9211-h9" for t in hub.state()["loose_traffic"])
    assert any(r["id"] == "9211-h9" for r in hub.cad.comms_query({})["rows"])


def test_service_of_every_tag_in_the_shipped_talkgroup_files_and_main_is_the_countys(tmp_path):
    import csv
    want = {"County Police": "police", "DOT": "transit", "Fire/EMS": "fire", "Hospital": "ems", "Interop/Events": "other",
            "OEM": "other", "Operations": "other"}
    county = list(csv.DictReader((TR / "talkgroups.csv").read_text().splitlines()))
    assert {r["Tag"] for r in county} == set(want)
    assert all(sourcekeys.service(r["Tag"]) == want[r["Tag"]] for r in county)
    mrrs = list(csv.DictReader((TR / "systems" / "mrrs" / "talkgroups.csv").read_text().splitlines()))
    assert all(r["Service"] in sourcekeys.SERVICES for r in mrrs)               # every row says its own
    assert all(MRRS.talkgroups[int(r["Decimal"])]["service"] == r["Service"] for r in mrrs)
    by_tag = {"Police": "police", "EMS": "ems", "Metro-North": "transit", "MTA Police": "police", "Interop": "other"}
    assert {r["Tag"] for r in mrrs} == set(by_tag) and all(sourcekeys.service(t) == s for t, s in by_tag.items())
    # whole words only, and the listed service wins
    assert [sourcekeys.service(g) for g in ("Law Dispatch", "MTAPD Dispatch", "Fire Police", "Dispatch", "Batch")] == [
        "police", "police", "police", "other", "other"]
    assert sourcekeys.service("Law Dispatch", "transit") == "transit"
    # Main is the county's talkgroup of that number only, whatever a row of another system carries
    clock = Clock()
    c = cad(tmp_path, clock)
    c.record_traffic(tx("307-m1", clock.t, "Fire/EMS 15", "", "", 307))
    c.record_traffic(dict(tx("307-m2", clock.t, "Other 307", "", "", 307), system="mrrs", source_key="p25:mrrs:307"))
    c.comms.context_broadcast = True
    main, other = c.comms.split([307])
    assert [r["id"] for r in main] == ["307-m1"] and [r["id"] for r in other] == ["307-m2"]
