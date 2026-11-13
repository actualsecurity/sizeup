"""The reception test's targets (GitHub #436): every trunked P25 system, the county's included, and nothing else.

    the county   a probe-only system per site of the county trunk-recorder's config (sizeup/radio/probes.py): its own
                 directory, trunk-recorder, state file and capture directory; the county's trunk-recorder, its files
                 and its dongles are never touched, and its fire dongle is never lent, not even to its own probe
    systems      every system of the systems config (MRRS), as before
    never        a conventional channel: FM paging, a fireground, the airband
    TG hits/h    grants on the system's alert talkgroups (the county's: our fire alert talkgroups)
    stopped      a test that didn't run its course has no verdict for a side that measured nothing

No hardware, no trunk-recorder: as tests/test_reception.py (its app, dongles and fake trunk-recorder lines). Every
radio ID is invented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sizeup.radio.claims as claims
from sizeup.radio import probes, reception, systems, trunkrecd
from tests.test_p25_systems import TR, ask_county, system_dir
from tests.test_reception import Clock, a_call, feed, lab, running, tr  # noqa: F401  (lab: a fixture)
from tests.test_trunk_recorder_unit import SERVICE_PID, FakePopen

COUNTY = "probe-westchester"


def grant(tg: int, radio: str = "4242098", system: str = COUNTY) -> str:
    return f"SIZEUP-UNIT {system} {radio} call {tg}"


def county_files(work: Path) -> dict:
    return {p.name: p.read_bytes() for p in (work / "config.json", work / "talkgroups.csv")}


# -- the county's probe: what it is -------------------------------------------------------------------------------------------


def test_the_county_probe_is_made_from_the_county_config_and_never_writes_to_it(tmp_path):
    work = system_dir(tmp_path).parent.parent
    before = county_files(work)
    (p,) = probes.county(work, {307, 333}, ["FG1", "Fire/EMS 15", "MRRS 770", "MRRS 773", "Off"])
    county = json.loads((TR / "config.json").read_text())
    assert (p.name, p.label, p.jobs, p.probe_only) == (COUNTY, "County", ("County probe",), True)
    assert list(p.frequencies) == [float(f) for f in county["systems"][0]["control_channels"]]
    assert p.sources[0].center == county["sources"][0]["center"] and p.reception["alert"] == [307, 333]
    d = work / "systems" / COUNTY
    conf = json.loads((d / "config.json").read_text())
    s = conf["systems"][0]
    assert (s["shortName"], s["talkgroupsFile"], s["recordUnknown"], s["unitScript"]) == (
        COUNTY, "talkgroups.csv", False, "../../unit-event.sh")
    assert conf["controlWarnRate"] == -1 and "sizeup" not in conf               # the decode rate every 3 s
    # its captures beside the county's, never in it
    cap = Path(conf["captureDir"])
    county_cap = (work / county["captureDir"]).resolve()
    assert cap.name == f"p25-{COUNTY}" and cap.parent == county_cap.parent and cap != county_cap
    assert (d / "talkgroups.csv").read_bytes() == (work / "talkgroups.csv").read_bytes()
    assert json.loads((d / probes.MARK).read_text())["probe"] is True
    assert county_files(work) == before                                        # the county's: read, never written
    # made again (every start): nothing rewritten that didn't change
    mtime = (d / "config.json").stat().st_mtime_ns
    probes.county(work, {307, 333})
    assert (d / "config.json").stat().st_mtime_ns == mtime and county_files(work) == before
    # never a system of the systems config: discover() leaves it out, and a release from before it can't load it
    assert [x.name for x in systems.discover(work, {307, 301})] == ["mrrs"]
    with pytest.raises(systems.SystemError_):
        systems.load(d)
    # the station's service supervises it like any other system (off until a test gives it a dongle)
    assert list(trunkrecd.system_dirs(work)) == ["mrrs", COUNTY]
    run = json.loads((d / trunkrecd.system_runtime_argv(["t", "--config=config.json"], d, {0: 4})[-1]
                      .split("=")[1]).read_text())
    assert run["sources"][0]["device"] == "rtl=4" and run["systems"][0]["shortName"] == COUNTY
    assert run["systems"][0]["control_channels"] == county["systems"][0]["control_channels"]
    assert county_files(work) == before


def test_a_probe_job_never_takes_a_jobs_name(tmp_path):
    work = system_dir(tmp_path).parent.parent
    logs: list[str] = []
    assert probes.county(work, (), ["County probe"], logs.append) == ()
    assert "a job name is taken" in logs[0]
    assert probes.county(tmp_path / "nowhere", (), log=logs.append) == ()        # no county config: no probe


def _service(tmp_path, found):
    exe = tmp_path / "trunk-recorder"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    work = system_dir(tmp_path).parent.parent
    conf = tmp_path / "config.toml"
    conf.write_text(f'[p25]\nrecorder = "{exe} --config=config.json"\nrecorder_dir = "{work}"\n')
    pop = FakePopen()
    kw = dict(config=str(conf), state=str(tmp_path / "state" / "p25_recorder.json"),
              status_path=str(tmp_path / "run" / "status.json"), claims=claims.DIR, popen=pop, found=found,
              killpg=pop.killpg, pid=SERVICE_PID, log=lambda m: None)
    probes.county(work, {307, 333})
    return trunkrecd.Supervisor(**kw), trunkrecd.SystemSupervisor(COUNTY, **kw), pop


def ask_probe(tmp_path, sdr: str, seq: int = 1) -> None:
    p = Path(trunkrecd.system_state_path(COUNTY, str(tmp_path / "state" / "p25_recorder.json")))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"on": True, "label": "County", "pid": None, "seq": seq,
                             "sources": {"0": {"sdr": sdr, "mode": "County probe", "name": sdr}}}))


def test_on_the_station_the_probe_runs_beside_the_county_and_yields_its_dongle_at_once(tmp_path):
    county, probe, pop = _service(tmp_path, lambda: [(0, "trunk"), (1, "spare")])
    ask_county(tmp_path, "trunk")
    assert county.step(100.0)["state"] == "ok"
    fire = pop.started[0]
    # the county's own dongle: never the probe's
    ask_probe(tmp_path, "trunk")
    st = probe.step(101.0)
    assert st["state"] == "held" and len(pop.started) == 1
    assert json.loads((claims.DIR / "trunk.json").read_text())["mode"] == "Fire/EMS 15"
    # a spare: its own trunk-recorder, in its own directory; the county's runs on, the same process
    ask_probe(tmp_path, "spare", seq=2)
    assert probe.step(102.0)["state"] == "ok" and len(pop.started) == 2
    assert pop.started[1].kw["cwd"].endswith(f"systems/{COUNTY}")
    assert county.step(103.0)["state"] == "ok" and fire.code is None and county.status["tr_pid"] == fire.pid
    # the county is given the spare: the probe stops at once and lets it go
    ask_county(tmp_path, "spare", seq=2)
    probe.step(104.0)
    assert pop.started[1].code is not None and probe.claims_held == {}
    assert json.loads((tmp_path / "state" / "p25_recorder.json").read_text())["sdr"] == "spare"    # written by the test


# -- the targets: the county and the systems, never a channel -----------------------------------------------------------------


def test_the_targets_are_the_county_and_the_systems_never_a_channel(lab):
    app = lab
    running(app)
    st = app.reception_state()
    assert [(x["name"], x["kind"]) for x in st["systems"]] == [(COUNTY, "county"), ("mrrs", "system")]
    jobs = [j["job"] for x in st["systems"] for j in x["jobs"]]
    assert jobs == ["County probe", "MRRS 770", "MRRS 773"]
    for fm in ("FG1", "South 2", "HPN Tower", "Fire/EMS 15"):                    # FM paging, fireground, airband, the trunk
        assert fm not in jobs
        err = app.reception_post({"action": "start", "system": COUNTY, "job": fm, "sdr": "spare"})
        assert err == "jobs: one or more of County probe, each once"
    # a probe's job is never a dongle job of Settings > Radios
    assert "County probe" not in app.modes and "County probe" not in app.state()["sdr"]["modes"]
    assert app.set_mode("County probe", "spare") is not None and app.assign.get("spare", "Off") == "Off"


def test_a_county_probe_never_touches_the_live_county_job_or_its_dongles(lab, monkeypatch):
    app, clock = lab, Clock()
    running(app, clock)
    pr = app.reception.probes[COUNTY]
    pr.recorder.status = lambda: {"state": "ok", "detail": "", "restarts": 0}
    work = Path(app.cfg.p25_recorder_dir)
    before, trunk_claim = county_files(work), (claims.DIR / "trunk.json").read_text()
    touched: list[str] = []
    for fn in ("start", "stop", "shutdown"):
        monkeypatch.setattr(app.recorder, fn, lambda *a, _fn=fn, **k: touched.append(_fn))
    # the county's own dongle: never lent, not even to its own probe
    err = app.reception_post({"action": "start", "system": COUNTY, "job": "County probe", "sdr": "trunk",
                              "pause": "Fire/EMS 15"})
    assert "protected: not lendable" in err and app.assign["trunk"] == "Fire/EMS 15"
    # a spare: the probe's own trunk-recorder on it, under its claim
    assert app.reception_post({"action": "start", "system": COUNTY, "job": "County probe", "sdr": "spare"}) is None
    assert app.assign["spare"] == "County probe" and pr.recorder.source_on(0) == "spare"
    assert json.loads((claims.DIR / "spare.json").read_text())["mode"] == "County probe"
    assert pr.recorder.workdir.endswith(f"systems/{COUNTY}") and pr.recorder.workdir != app.recorder.workdir
    feed(pr, clock, 476.075, [40] * 10, [tr("Started with Control Channel: 476.075000 MHz", sysname=COUNTY)])
    for line in (grant(307), grant(307, "4242396"), grant(333), grant(359)):
        pr.recorder.on_line(line)
    assert app.reception.on_call(pr, a_call(307, at=clock.t)) is True             # the test's: counted, never traffic
    run = app.reception_state()["active"]["running"]
    assert run["alert"]["grants"] == 3 and [t["tg"] for t in run["alert"]["tgs"]] == [307, 333]
    assert run["voice"]["calls"] == 1 and "4242098" not in json.dumps(run)
    clock.t = app.reception.active["phase_until"] + 1
    app.reception.tick()
    assert app.reception.active is None and app.assign["spare"] == "Off" and pr.recorder.sources == {}
    assert not (claims.DIR / "spare.json").exists()
    h = app.reception.history[0]
    assert h["status"] == "done" and h["system"] == COUNTY and h["halves"][0]["alert"]["grants"] == 3
    # the county's trunk-recorder, files, claim and dongle: as they were throughout
    assert touched == [] and app.recorder.on() == "trunk" and app.assign["trunk"] == "Fire/EMS 15"
    assert county_files(work) == before and (claims.DIR / "trunk.json").read_text() == trunk_claim
    assert not any(getattr(t, "system", "") == COUNTY for i in app.store.incidents for t in i.traffic)
    assert not any(getattr(t, "system", "") == COUNTY for t in app.store.loose)


def test_switching_a_dongle_lent_to_the_county_probe_ends_it_and_the_probe_stops(lab):
    app, clock = lab, Clock()
    running(app, clock)
    pr = app.reception.probes[COUNTY]
    assert app.reception_post({"action": "start", "system": COUNTY, "job": "County probe", "sdr": "air",
                               "pause": "HPN Tower"}) is None
    assert app.set_mode("Off", "air").startswith("reception test stopped")
    assert app.assign["air"] == "HPN Tower" and pr.recorder.sources == {}
    # the dongle lost its job some other way (unplugged): the probe never runs on without its test
    assert app.reception_post({"action": "start", "system": COUNTY, "job": "County probe", "sdr": "spare"}) is None
    app.assign["spare"] = "Off"
    pr.recorder.status = lambda: {"state": "ok", "detail": "", "restarts": 0}
    app.reception.tick()
    assert app.reception.active is None and pr.recorder.sources == {}
    assert app.reception.history[0]["status"] == "failed"


# -- TG hits/h: the system's alert talkgroups ---------------------------------------------------------------------------------


def test_tg_hits_are_grants_on_the_systems_alert_talkgroups():
    from tests.test_p25_systems import MRRS
    assert reception.alert_tgs(MRRS) == [9211, 9570]                     # its file's alert tier
    assert reception.alert_tgs(MRRS, lambda tg: "alert" if tg in (9212, 9213) else "text") == [9212]   # never encrypted
    clock = Clock()
    p = reception.Probe(MRRS, "MRRS 770", "spare", clock)
    for line in (grant(9211, system="mrrs"), grant(9211, "4242396", "mrrs"), grant(9570, system="mrrs"),
                 grant(9212, system="mrrs"), grant(9213, system="mrrs")):
        p.feed_line(line)
    clock.t += 1800
    r = p.result()
    assert r["alert"]["grants"] == 3 and r["alert"]["per_h"] == 6.0
    assert r["alert"]["tgs"] == [{"tg": 9211, "name": "PD Dispatch"}, {"tg": 9570, "name": "PC-Rye EMS"}]
    s = reception.summary(r)
    assert s["alert"]["per_h"] == 6.0 and s["windows"] == 0 and s["decoded_pct"] is None
    # a system with no alert talkgroup: no hits column to fill
    assert reception.Probe(MRRS, "MRRS 770", "spare", clock, alert=[]).result()["alert"]["per_h"] is None


# -- stopped is not "None" ----------------------------------------------------------------------------------------------------


def test_a_stopped_side_that_measured_nothing_has_no_verdict_a_finished_one_says_none(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    # stopped before a window: no verdict, no "0% decoded"
    assert app.reception_post({"action": "start", "job": "MRRS 770", "sdr": "spare"}) is None
    clock.t += 100
    app.reception_post({"action": "stop"})
    h = app.reception_state()["history"][0]
    assert h["status"] == "stopped" and h["verdict"] is None and h["halves"][0]["grade"] is None
    assert h["halves"][0]["decoded_pct"] is None and h["halves"][0]["seconds"] == 100.0
    # stopped after it measured: its numbers keep their verdict
    assert app.reception_post({"action": "start", "job": "MRRS 770", "sdr": "spare"}) is None
    feed(rt, clock, 769.90625, [0] * 50)
    app.reception_post({"action": "stop"})
    h = app.reception_state()["history"][0]
    assert h["status"] == "stopped" and h["halves"][0]["grade"] == "none" and h["halves"][0]["decoded_pct"] == 0.0
    # ran its course and heard nothing: None
    assert app.reception_post({"action": "start", "job": "MRRS 770", "sdr": "spare"}) is None
    clock.t = app.reception.active["phase_until"] + 1
    app.reception.tick()
    h = app.reception_state()["history"][0]
    assert h["status"] == "done" and h["verdict"]["grade"] == "none" and h["halves"][0]["grade"] == "none"
    assert reception.unjudged([{"cc": {"windows": 0}, "verdict": {"grade": "none"}}], "done")[0]["verdict"]
