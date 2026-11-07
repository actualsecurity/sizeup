"""trunk-recorder as its own service on the station (GitHub #71): sizeup/radio/trunkrecd.py (the supervisor that
sizeup-trunk-recorder.service runs), sizeup/radio/recorder.py UnitRecorder (sizeup's side of the handover), the dongle
claims held by the service, the app switching a dongle to and from Fire/EMS 15 and adopting the trunk-recorder that runs
across a restart, and the installer / updater restarting the service only when what it runs changed.

No hardware, no systemd, no network: trunk-recorder is a fake Popen (or a shell script standing in for it), the dongles
a list, the claims a tmp folder (conftest.py), systemctl / runuser / timeout fakes on PATH. The "service" is a
Supervisor stepped by a thread of the test, with another pid than the test's (its parent's), as on the station."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import sizeup.app as appmod
import sizeup.radio.claims as claims
from sizeup.config import ChannelCfg
from sizeup.radio import recorder as rec
from sizeup.radio import trunkrecd
from tests.test_app import cfg as app_cfg, wait

ROOT = Path(__file__).resolve().parent.parent
STATION = ROOT / "deploy" / "station"
INSTALLER = STATION / "install-linux.sh"
P25 = "Fire/EMS 15"
# The service's pid, as on the station: alive, not this process, and never OTHER_PID. Not os.getppid(): a test run
# whose parent has gone (started in the background, by an agent, in a container) has launchd / init as its parent,
# pid 1 too, and the service then took shortfinal's claim as its own (#282). A child of ours that lives as long as we
# do (it exits when its stdin, our end of the pipe, closes).
_SERVICE = subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
SERVICE_PID = _SERVICE.pid
OTHER_PID = 1                       # alive, not ours: shortfinal's in these tests
assert SERVICE_PID not in (os.getpid(), OTHER_PID)


class FakeProc:
    def __init__(self, argv, pid, kw):
        self.argv, self.pid, self.kw, self.code, self.stdout = argv, pid, kw, None, None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code


class FakePopen:
    def __init__(self):
        self.started: list[FakeProc] = []
        self.signals: list[tuple[int, int]] = []

    def __call__(self, argv, **kw):
        p = FakeProc(list(argv), 7000 + len(self.started), kw)
        self.started.append(p)
        return p

    def killpg(self, pid, sig):
        self.signals.append((pid, sig))
        for p in self.started:
            if p.pid == pid and p.code is None:
                p.code = -sig

    @property
    def running(self) -> list[FakeProc]:
        return [p for p in self.started if p.code is None]


def workdir(tmp_path) -> tuple[Path, Path]:
    """trunk-recorder's working directory as install-linux.sh makes it, and its config.toml."""
    work = tmp_path / "tr"
    work.mkdir(exist_ok=True)
    c = json.loads((ROOT / "deploy" / "trunk-recorder" / "config.json").read_text())
    for s in c.get("systems", []):
        s["talkgroupsFile"], s["unitScript"] = "talkgroups.csv", "./unit-event.sh"
    (work / "config.json").write_text(json.dumps(c, indent=2))
    shutil.copy2(ROOT / "deploy" / "trunk-recorder" / "unit-event.sh", work / "unit-event.sh")
    exe = tmp_path / "trunk-recorder"
    if not exe.exists():
        exe.write_text("#!/bin/sh\nsleep 30\n")
        exe.chmod(0o755)
    conf = tmp_path / "config.toml"
    conf.write_text(f'[p25]\nrecorder = "{exe} --config=config.json"\nrecorder_dir = "{work}"\n')
    return work, conf


def supervisor(tmp_path, found=lambda: [(0, "v4"), (1, "spare")], popen=None, pid=SERVICE_PID):
    work, conf = workdir(tmp_path)
    pop = popen or FakePopen()
    logs: list[str] = []
    s = trunkrecd.Supervisor(config=str(conf), state=str(tmp_path / "state" / "p25_recorder.json"),
                             status_path=str(tmp_path / "run" / "status.json"), claims=claims.DIR, popen=pop,
                             found=found, killpg=getattr(pop, "killpg", os.killpg), pid=pid, log=logs.append)
    return s, pop, logs


def ask(tmp_path, on=True, sdr="v4", pid=None, seq=1, name="V4"):
    """What sizeup-station writes (UnitRecorder._write_state)."""
    p = tmp_path / "state" / "p25_recorder.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"on": on, "sdr": sdr, "name": name, "pid": os.getpid() if pid is None else pid,
                             "seq": seq}))


def claim_of(serial):
    p = claims.DIR / f"{serial}.json"
    return json.loads(p.read_text()) if p.exists() else None


# ---- the service: what it runs, and when it (re)starts -------------------------------------------------------------------

def test_nothing_asked_runs_nothing_and_looks_at_no_dongle(tmp_path):
    def found():
        raise AssertionError("looked at the dongles")
    s, pop, _ = supervisor(tmp_path, found=found)
    st = s.step(1000.0)
    assert st["state"] == "off" and pop.started == [] and not claims.DIR.exists()
    assert trunkrecd.read_status(s.status_path, now=1001.0)["state"] == "off"
    assert trunkrecd.read_status(s.status_path, now=1000.0 + trunkrecd.STALE_S + 1)["stale"] is True


def test_the_dongle_sizeup_hands_over_runs_trunk_recorder_on_its_index_and_the_service_takes_the_claim(tmp_path):
    s, pop, logs = supervisor(tmp_path)
    assert claims.claim("v4", P25)                          # sizeup writes its own claim, then hands the dongle over
    ask(tmp_path, sdr="v4")
    st = s.step(1000.0)
    assert st["state"] == "ok" and st["seq"] == 1 and st["pid"] == SERVICE_PID and len(pop.started) == 1
    p = pop.started[0]
    assert p.argv[-1] == "--config=config.runtime.json" and st["tr_pid"] == p.pid and st["index"] == 0
    assert p.kw["cwd"] == str(tmp_path / "tr") and p.kw["start_new_session"] is True
    conf = json.loads((tmp_path / "tr" / "config.runtime.json").read_text())
    assert [x["device"] for x in conf["sources"]] == ["rtl=0"]              # the index, never the serial
    # the script by its absolute path, or "./unit-event.sh" when that is too long for trunk-recorder's buffer: which
    # one depends on how deep tmp_path is (pytest-xdist's popen-gwN folders make it longer, #282)
    script = (tmp_path / "tr" / "unit-event.sh").resolve()
    assert [x["unitScript"] for x in conf["systems"]] == [
        str(script) if len(str(script)) <= trunkrecd.UNIT_SCRIPT_MAX else "./unit-event.sh"]
    # the claim is the service's now (sizeup's own was taken over), in the agreed format
    c = claim_of("v4")
    assert c["app"] == "sizeup" and c["pid"] == SERVICE_PID and c["mode"] == P25 and set(c) == {"app", "pid", "mode", "since"}
    assert any("trunk-recorder on V4 (device #0)" in m for m in logs)


def test_a_new_sizeup_handing_over_the_same_dongle_changes_nothing(tmp_path):
    """The update: sizeup-station restarts, the new process writes the same dongle with its own pid and seq. The
    trunk-recorder that runs is adopted: no restart, same PID, the claim untouched."""
    s, pop, _ = supervisor(tmp_path)
    ask(tmp_path, sdr="v4", pid=os.getpid(), seq=1)
    first = s.step(1000.0)["tr_pid"]
    since = claim_of("v4")["since"]
    ask(tmp_path, sdr="v4", pid=OTHER_PID, seq=2)          # another sizeup process (the old one is gone)
    for t in range(10):
        st = s.step(1001.0 + t)
    assert len(pop.started) == 1 and pop.signals == [] and st["tr_pid"] == first and st["state"] == "ok"
    assert st["seq"] == 2 and claim_of("v4")["since"] == since and claim_of("v4")["pid"] == SERVICE_PID


def test_moving_it_or_switching_it_off_stops_trunk_recorder_and_lets_the_dongle_go(tmp_path):
    s, pop, logs = supervisor(tmp_path)
    ask(tmp_path, sdr="v4")
    s.step(1000.0)
    ask(tmp_path, sdr="spare", seq=2, name="spare")
    st = s.step(1001.0)
    assert pop.signals == [(pop.started[0].pid, signal.SIGTERM)] and len(pop.started) == 2
    assert json.loads((tmp_path / "tr" / "config.runtime.json").read_text())["sources"][0]["device"] == "rtl=1"
    assert claim_of("v4") is None and claim_of("spare")["pid"] == SERVICE_PID and st["sdr"] == "spare"
    assert any("moved to spare" in m for m in logs)
    ask(tmp_path, on=False, sdr="", seq=3)
    st = s.step(1002.0)
    assert st["state"] == "off" and st["seq"] == 3 and pop.running == [] and claim_of("spare") is None


def test_shortfinal_or_the_adsb_decoder_holding_it_is_reported_and_never_fought(tmp_path):
    s, pop, _ = supervisor(tmp_path)
    claims.DIR.mkdir(parents=True, exist_ok=True)
    held = {"app": "shortfinal", "pid": OTHER_PID, "mode": "TWR", "since": time.time()}
    (claims.DIR / "v4.json").write_text(json.dumps(held))
    ask(tmp_path, sdr="v4")
    st = s.step(1000.0)
    assert st["state"] == "held" and "shortfinal holds V4 (TWR)" in st["detail"] and pop.started == []
    assert claim_of("v4") == held                                      # left alone
    (claims.DIR / "v4.json").write_text(json.dumps(dict(held, app="sizeup", mode="adsb")))   # sizeup, not the one that handed over
    assert s.step(1001.0)["state"] == "held" and pop.started == []
    (claims.DIR / "v4.json").unlink()                                  # let go: it starts
    assert s.step(1002.0)["state"] == "ok" and len(pop.started) == 1


def test_unplugged_exits_and_backoff(tmp_path):
    plugged: list[tuple[int, str]] = []
    s, pop, _ = supervisor(tmp_path, found=lambda: list(plugged))
    ask(tmp_path, sdr="v4")
    st = s.step(1000.0)
    assert st["state"] == "absent" and "not plugged in" in st["detail"] and pop.started == []
    assert claim_of("v4")["pid"] == SERVICE_PID                        # held while it is Fire/EMS 15's, as sizeup did
    plugged.append((2, "v4"))
    s.step(1001.0)
    assert pop.started == []                                           # looked for again every UNPLUGGED_S
    assert s.step(1000.0 + trunkrecd.UNPLUGGED_S)["state"] == "ok" and pop.started[0].argv[-1].endswith("runtime.json")
    pop.started[0].code = 1                                            # it dies
    s.lines.append("(error) something broke")
    st = s.step(1010.0)
    assert st["state"] == "down" and st["restarts"] == 1 and "exited (1): (error) something broke" in st["detail"]
    s.step(1011.0)
    assert len(pop.started) == 1                                       # backoff
    assert s.step(1010.0 + trunkrecd.BACKOFF_S[1])["state"] == "ok" and len(pop.started) == 2
    pop.started[1].code = 1                                            # the dongle went: plainly, no restart counted
    s.lines.append("(error)   Failed parsing Config: Wrong rtlsdr device index given.")
    st = s.step(1100.0)
    assert st["state"] == "absent" and "not connected" in st["detail"] and st["restarts"] == 1


def test_a_change_of_its_command_restarts_it_and_shutdown_releases_the_claim(tmp_path):
    s, pop, _ = supervisor(tmp_path)
    ask(tmp_path, sdr="v4")
    s.step(1000.0)
    conf = Path(s.config)
    conf.write_text(conf.read_text().replace("--config=config.json", "--config=config.json --log-level=info"))
    s.step(1001.0)
    assert len(pop.started) == 2 and pop.started[0].code == -signal.SIGTERM and "--log-level=info" in pop.started[1].argv
    s.shutdown()
    assert pop.running == [] and claim_of("v4") is None
    assert json.loads(Path(s.status_path).read_text())["state"] == "off"


def test_what_it_reads_and_refuses(tmp_path):
    work, conf = workdir(tmp_path)
    r = trunkrecd.recorder(str(conf))
    assert r["argv"][1:] == ["--config=config.json"] and r["workdir"] == str(work) and not r["problem"]
    conf.write_text('[p25]\nrecorder = "tr --config=config.json"\nrecorder_dir = "trdir"\n')
    assert trunkrecd.recorder(str(conf))["workdir"] == str(tmp_path.resolve() / "trdir")
    assert "no [p25] recorder" in trunkrecd.recorder(str(tmp_path / "none.toml"))["problem"] or \
        "can't be read" in trunkrecd.recorder(str(tmp_path / "none.toml"))["problem"]
    assert trunkrecd.wanted(str(tmp_path / "none.json"))["on"] is False
    ask(tmp_path, sdr="../../etc/x")
    assert trunkrecd.wanted(str(tmp_path / "state" / "p25_recorder.json"))["problem"]
    s, pop, _ = supervisor(tmp_path)
    assert s.step(1000.0)["state"] == "down" and pop.started == []      # never a path as a claim's name
    assert trunkrecd.resolve("v4", [(0, "v4"), (1, "v4")]) is None and trunkrecd.resolve("v4", None) is None


# ---- a real process: its output into trunk-recorder.log, read by sizeup line by line ------------------------------------

class Service:
    """The supervisor stepped every 20 ms in a thread: sizeup-trunk-recorder.service, in the test."""

    def __init__(self, s):
        self.s, self._stop = s, threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while not self._stop.is_set():
            self.s.step()
            self._stop.wait(0.02)

    def close(self):
        self._stop.set()
        self.t.join(timeout=5)
        self.s.shutdown()


def test_its_output_goes_to_the_log_and_sizeup_follows_it_through_a_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(trunkrecd, "LOG_MAX_BYTES", 2000)
    work, conf = workdir(tmp_path)
    exe = tmp_path / "trunk-recorder"
    exe.write_text("#!/bin/sh\necho '[westchester] Starting P25 Recorder'\n./unit-event.sh westchester 41228 call 307\n"
                   "b=0\nwhile [ $b -lt 6 ]; do i=0\n  while [ $i -lt 30 ]; do echo \"line $b-$i of trunk-recorder output\";"
                   " i=$((i+1)); done\n  b=$((b+1)); sleep 0.2\ndone\necho 'all said'\nsleep 30\n")
    exe.chmod(0o755)
    (work / trunkrecd.LOG).write_text("said before this sizeup started\n")
    s, _pop, _ = supervisor(tmp_path, popen=subprocess.Popen)
    got: list[str] = []
    u = rec.UnitRecorder(f"{exe} --config=config.json", str(work), state_path=s.state, status_path=s.status_path,
                         poll_s=0.02)
    u.on_line = got.append
    u.device_fn = lambda: "0"
    u.device_fn.serial = "v4"
    svc = Service(s)
    try:
        u.start()
        assert wait(lambda: "all said" in got, 15), got[-3:]
        want = ["[westchester] Starting P25 Recorder", "SIZEUP-UNIT westchester 41228 call 307"] + [
            f"line {b}-{i} of trunk-recorder output" for b in range(6) for i in range(30)] + ["all said"]
        assert got == want                                    # every line once, in order; nothing from before
        assert (work / "trunk-recorder.log.1").exists() and (work / trunk_log()).stat().st_size <= 2000 + 200
        st = u.status()
        assert st["state"] == "ok" and "Starting P25 Recorder" in st["detail"]
        pid = s.status["tr_pid"]
        u.stop("test")                                        # returns once the service has stopped it
        assert s.status["state"] == "off" and claim_of("v4") is None
        assert not _alive(pid)
    finally:
        u.shutdown()
        svc.close()


def trunk_log():
    return trunkrecd.LOG


def _alive(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ---- the claims: the service's is sizeup's own ---------------------------------------------------------------------------

def test_the_services_claim_is_sizeups_own_and_only_it_removes_it(monkeypatch):
    claims.DIR.mkdir(parents=True, exist_ok=True)
    mine = {"app": "sizeup", "pid": SERVICE_PID, "mode": P25, "since": time.time()}
    (claims.DIR / "v4.json").write_text(json.dumps(mine))
    assert claims.other("v4")["pid"] == SERVICE_PID                  # not a helper: another sizeup process
    monkeypatch.setattr(claims, "helpers", lambda: {SERVICE_PID})
    assert claims.other("v4") is None                                 # the service runs it for us
    assert claims.claim("v4", P25) and claim_of("v4") == mine         # its claim stands, not overwritten
    assert not claims.claim("v4", "FG1")                              # it hasn't let go: no other job there
    claims.release("v4")
    assert claim_of("v4") == mine
    (claims.DIR / "v4.json").write_text(json.dumps(dict(mine, app="shortfinal")))
    assert claims.other("v4")["app"] == "shortfinal" and not claims.claim("v4", P25)   # pids aren't enough


# ---- the app: switching to and from Fire/EMS 15, and a restart that leaves trunk-recorder running ------------------------

@pytest.fixture
def station(tmp_path, monkeypatch):
    """(make() -> a started App in unit mode, the service's FakePopen, the Service, logs). Every app is stopped."""
    plugged = [(0, "Blog V4", "v4"), (1, "RTL2838UHIDIR", "fire-fg")]
    logs: list[str] = []
    monkeypatch.setenv(rec.UNIT_ENV, "1")
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    monkeypatch.setattr(appmod, "SDR_WATCH_S", 3600.0)
    monkeypatch.setattr(appmod, "SDR_SETTLE_S", 0.0)
    monkeypatch.setattr(appmod, "log", logs.append)
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    monkeypatch.setattr(appmod.sdr_lookup, "resolve", lambda serial, *a: next((i for i, _p, s in plugged if s == serial), None))
    monkeypatch.setattr(rec.TrunkRecorder, "one_dongle", lambda self: (True, ""))
    monkeypatch.setattr(trunkrecd, "STATE", str(tmp_path / "state" / "p25_recorder.json"))
    monkeypatch.setattr(trunkrecd, "STATUS", str(tmp_path / "run" / "status.json"))
    s, pop, _ = supervisor(tmp_path, found=lambda: [(i, sd) for i, _p, sd in plugged])
    svc = Service(s)
    work = tmp_path / "tr"
    (tmp_path / "clips").mkdir(exist_ok=True)
    c = replace(app_cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"),
                channels=(ChannelCfg("FG1", 453.0375, "traffic", "fire-fg"),), sdr_auto=True, sdr_auto_assign=False,
                sdr_names={"v4": "V4"}, p25_dir=str(tmp_path / "p25"), p25_talkgroups={307: P25},
                p25_recorder=f"{tmp_path / 'trunk-recorder'} --config=config.json", p25_recorder_dir=str(work))
    (tmp_path / "p25").mkdir(exist_ok=True)
    apps = []

    def make():
        a = appmod.App(c).start()
        apps.append(a)
        return a
    yield make, pop, s, logs
    for a in apps:
        if not a._stopping.is_set():
            a.stop()
    svc.close()


def p25_row(app):
    return next(r for r in app.radio_status() if r["label"] == P25)


def test_switching_a_dongle_to_and_from_p25_and_a_restart_that_leaves_trunk_recorder_running(station, tmp_path):
    make, pop, s, logs = station
    app = make()
    assert isinstance(app.recorder, rec.UnitRecorder) and app.recorder.unit
    assert app.set_mode(P25, "v4") is None
    assert wait(lambda: s.status.get("state") == "ok" and len(pop.running) == 1)
    tr = pop.running[0]
    assert "rtl=0" in (tmp_path / "tr" / "config.runtime.json").read_text()
    assert claim_of("v4")["pid"] == SERVICE_PID                        # the service holds it ...
    assert next(x for x in app.sdr()["sdrs"] if x["serial"] == "v4")["held_by"] is None   # ... and that is sizeup's
    assert wait(lambda: p25_row(app)["state"] == "ok")

    # a Python-only update: this sizeup exits, a new one starts; trunk-recorder keeps running, same PID
    app.stop()
    assert tr.code is None and claim_of("v4")["pid"] == SERVICE_PID and pop.signals == []
    new = make()
    assert new.assign["v4"] == P25 and p25_row(new)["state"] == "ok"   # ok from its first look
    rows = []
    for _ in range(20):
        rows.append(p25_row(new)["state"])
        time.sleep(0.02)
    assert set(rows) == {"ok"} and pop.running == [tr] and len(pop.started) == 1 and pop.signals == []
    assert new.recorder.on() == "v4" and new._program_on(P25) == "v4"

    # the dongle to FG1: trunk-recorder stopped and the dongle let go before FG1 takes it
    assert new.set_mode("FG1", "v4") is None
    assert tr.code == -signal.SIGTERM and pop.running == []
    c = claim_of("v4")
    assert c["pid"] == os.getpid() and c["mode"] == "FG1"
    assert p25_row(new)["state"] == "off"
    # ... and back to P25
    assert new.set_mode(P25, "v4") is None
    assert wait(lambda: len(pop.running) == 1 and claim_of("v4")["pid"] == SERVICE_PID)
    assert claim_of("v4")["mode"] == P25
    # ... and over to the other dongle
    assert new.set_mode(P25, "fire-fg") is None
    assert wait(lambda: len(pop.running) == 1 and pop.running[0].argv and s.status.get("sdr") == "fire-fg"
                and s.status.get("state") == "ok")
    assert "rtl=1" in (tmp_path / "tr" / "config.runtime.json").read_text() and claim_of("v4") is None
    assert new.assign == {"v4": "Off", "fire-fg": P25}


def test_a_sizeup_with_no_dongle_on_p25_stops_what_the_service_still_runs(station, tmp_path):
    make, pop, s, logs = station
    app = make()
    assert app.set_mode(P25, "v4") is None
    assert wait(lambda: len(pop.running) == 1)
    app.stop()
    (tmp_path / "sdr_modes.json").write_text(json.dumps({"v4": "Off", "fire-fg": "Off"}))
    make()
    assert pop.running == [] and s.status["state"] == "off" and claim_of("v4") is None


def test_the_p25_row_says_when_the_service_is_not_running(tmp_path):
    u = rec.UnitRecorder("trunk-recorder --config=config.json", str(tmp_path), state_path=str(tmp_path / "s.json"),
                         status_path=str(tmp_path / "none.json"), wait_s=0.2)
    st = u.status()
    assert st["state"] == "down" and "sizeup-trunk-recorder" in st["detail"]
    u.stop("nothing ran")                                             # returns at once: nothing to wait for
    u.device_fn = lambda: "0"
    u.device_fn.serial = "v4"
    u.start()
    assert trunkrecd.wanted(str(tmp_path / "s.json"))["sdr"] == "v4"
    (tmp_path / "none.json").write_text(json.dumps({"state": "off", "on": False, "sdr": "", "at": time.time(), "pid": 5}))
    assert u.status()["state"] == "starting"                           # handed over, not taken yet
    assert u.helper_pids() == {5}
    u.shutdown()
    assert rec.make("x", str(tmp_path)).unit is False                  # the Mac / a bench: sizeup's child


# ---- the station: its unit, the installer, the updater ---------------------------------------------------------------------

def test_the_unit_runs_the_supervisor_as_the_sizeup_user_with_no_privilege():
    unit = (STATION / "systemd" / "sizeup-trunk-recorder.service").read_text()
    for want in ("User=sizeup", "Group=sizeup", "SupplementaryGroups=plugdev", "Environment=HOME=/var/lib/sizeup",
                 "ExecStart=/usr/bin/python3 -I /usr/local/lib/sizeup/trunkrecd.py run",
                 "RuntimeDirectory=sizeup-trunk-recorder", "Restart=always", "StartLimitIntervalSec=0",
                 "KillMode=control-group", "NoNewPrivileges=yes", "WantedBy=multi-user.target",
                 "LD_LIBRARY_PATH=/opt/sizeup/rtl-sdr/lib:/opt/sizeup/trunk-recorder/lib/trunk-recorder"):
        assert want in unit, want
    settings = [ln for ln in unit.splitlines() if ln and not ln.startswith("#")]
    assert not [ln for ln in settings if "sudo" in ln or "polkit" in ln.lower() or ln.startswith(("User=root", "AmbientCapabilities", "CapabilityBoundingSet"))]
    assert trunkrecd.RUN_DIR == "/run/sizeup-trunk-recorder" and trunkrecd.STATE.startswith("/var/lib/sizeup/state/")
    station = (STATION / "systemd" / "sizeup-station.service").read_text()
    assert "Environment=SIZEUP_TRUNK_RECORDER_UNIT=1" in station and rec.UNIT_ENV == "SIZEUP_TRUNK_RECORDER_UNIT"
    assert re.search(r"^Wants=.*\bsizeup-trunk-recorder\.service\b", station, re.M)
    assert "rtl_fm and trunk-recorder are its children" not in station
    # nothing anywhere gives the sizeup user sudo or a polkit rule for it
    for p in STATION.rglob("*"):
        if p.is_file() and p.suffix not in (".ps1",):
            text = p.read_text(errors="replace")
            assert "sudoers" not in text and "/etc/polkit-1" not in text, p.name


def test_the_installer_installs_the_service_and_the_cli_knows_it():
    sh = INSTALLER.read_text()
    for want in ('install -D -m 0644 "$sys/../../sizeup/radio/trunkrecd.py" "$LIB/trunkrecd.py"',
                 "sizeup-adsb.service sizeup-trunk-recorder.service; do", "tr_was=$(tr_sig)",
                 'trunk_recorder_service "$tr_was"', "sizeup/radio/trunkrecd.py sizeup/cloud/autoupdate.py",
                 "/etc/systemd/system/sizeup-trunk-recorder.service \\"):
        assert want in sh, want
    act = sh[sh.index("\nactivate() {"):sh.index("\n# healthy BUILD SECONDS")]
    # trunk-recorder is looked at before sizeup-station restarts (which then adopts it), after the units are in place
    assert act.index("systemctl daemon-reload") < act.index('trunk_recorder_service "$tr_was"') \
        < act.index("systemctl restart sizeup-station.service")
    assert act.index("tr_was=$(tr_sig)") < act.index('install -D -m 0644 "$sys/../../sizeup/radio/trunkrecd.py"')
    cli = (STATION / "sizeup-station").read_text()
    for want in ("-u sizeup-trunk-recorder.service", "/run/sizeup-trunk-recorder/status.json",
                 "systemctl stop sizeup-trunk-recorder.service", "systemctl start sizeup-trunk-recorder.service",
                 "sizeup-trunk-recorder is running"):
        assert want in cli, want
    r = subprocess.run([sys.executable, "-I", str(ROOT / "sizeup" / "radio" / "trunkrecd.py"), "help"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "usage: trunkrecd.py" in r.stderr


def shell_fn(name: str) -> str:
    m = re.search(rf"\n({name}\(\) {{\n.*?\n}}\n)", INSTALLER.read_text(), re.S)
    assert m, f"install-linux.sh has no {name}()"
    return m.group(1)


@pytest.fixture
def box(tmp_path):
    """install-linux.sh's tr_sig / trunk_recorder_service in bash, against a fake station: run(fn, *args) -> output."""
    t = tmp_path
    for d in ("lib", "opt/builds/tr-1", "opt/builds/tr-2", "opt/builds/rtl-1", "tr", "systemd", "bin"):
        (t / d).mkdir(parents=True, exist_ok=True)
    (t / "lib" / "trunkrecd.py").write_text("supervisor v1\n")
    (t / "systemd" / "sizeup-trunk-recorder.service").write_text("unit v1\n")
    (t / "opt" / "trunk-recorder").symlink_to(t / "opt" / "builds" / "tr-1")
    (t / "opt" / "rtl-sdr").symlink_to(t / "opt" / "builds" / "rtl-1")
    (t / "tr" / "config.json").write_text('{"sources": []}\n')
    (t / "tr" / "talkgroups.csv").write_text("Decimal,Alpha Tag\n307,Fire/EMS 15\n")
    (t / "active").write_text("0\n")
    fakes = {"runuser": 'shift 3\nexec "$@"\n', "timeout": 'shift\nexec "$@"\n',
             "systemctl": f'echo "$*" >> "{t}/systemctl.log"\n'
                          f'[ "$1" = is-active ] && exit "$(cat "{t}/active")"\nexit 0\n'}
    for n, body in fakes.items():
        (t / "bin" / n).write_text("#!/bin/sh\n" + body)
        (t / "bin" / n).chmod(0o755)
    script = ("set -euo pipefail\nsay() { echo \"$*\"; }\n"
              f"LIB={t}/lib OPT={t}/opt TR_DIR={t}/tr SVC_USER=sizeup TR_UNIT={t}/systemd/sizeup-trunk-recorder.service\n"
              + shell_fn("tr_sig") + shell_fn("trunk_recorder_service") + '"$@"\n')
    env = dict(os.environ, PATH=f"{t}/bin:/usr/bin:/bin:/sbin:/usr/sbin")

    def run(*args) -> str:
        r = subprocess.run(["bash", "-c", script, "-", *args], capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout.strip()

    def systemctl() -> list[str]:
        p = t / "systemctl.log"
        out = p.read_text().splitlines() if p.exists() else []
        p.unlink(missing_ok=True)
        return out
    return t, run, systemctl


def test_the_updater_restarts_trunk_recorder_only_when_what_it_runs_changed(box):
    t, run, systemctl = box
    was = run("tr_sig")
    assert re.fullmatch(r"[0-9a-f]{16}", was)
    # a Python-only update: sizeup's release changes, nothing trunk-recorder runs does (the installer rewrites
    # config.json and talkgroups.csv on every switch, with the same contents)
    (t / "tr" / "config.json").write_text((t / "tr" / "config.json").read_text())
    assert run("tr_sig") == was
    assert run("trunk_recorder_service", was) == "trunk-recorder: unchanged, left running (Fire/EMS 15 keeps its control channel)"
    assert systemctl() == ["enable --quiet sizeup-trunk-recorder.service", "is-active --quiet sizeup-trunk-recorder.service"]

    def changed(edit) -> None:
        before = run("tr_sig")
        edit()
        now = run("tr_sig")
        assert now != before
        assert run("trunk_recorder_service", before).startswith("trunk-recorder: (re)started")
        assert systemctl()[-1] == "restart sizeup-trunk-recorder.service"
    changed(lambda: (t / "tr" / "config.json").write_text('{"sources": [{"rate": 2400000}]}\n'))
    changed(lambda: (t / "tr" / "talkgroups.csv").write_text("Decimal,Alpha Tag\n307,Fire/EMS 15\n301,60 Control\n"))
    changed(lambda: ((t / "opt" / "trunk-recorder").unlink(), (t / "opt" / "trunk-recorder").symlink_to(t / "opt" / "builds" / "tr-2")))
    changed(lambda: (t / "lib" / "trunkrecd.py").write_text("supervisor v2\n"))
    changed(lambda: (t / "systemd" / "sizeup-trunk-recorder.service").write_text("unit v2\n"))
    # not running: started, whatever changed
    (t / "active").write_text("3\n")
    assert run("trunk_recorder_service", run("tr_sig")).startswith("trunk-recorder: (re)started")
    assert systemctl()[-1] == "restart sizeup-trunk-recorder.service"
    # a release from before #71: no unit, nothing done
    (t / "systemd" / "sizeup-trunk-recorder.service").unlink()
    assert run("trunk_recorder_service", "x") == "" and systemctl() == []


def test_going_back_to_a_release_from_before_the_service_removes_it_first(tmp_path):
    from sizeup.cloud.autoupdate import TR_UNIT, Paths, Updater
    opt, sysd = tmp_path / "opt", tmp_path / "systemd"
    (opt / "releases" / "old" / "deploy" / "station" / "systemd").mkdir(parents=True)
    (opt / "releases" / "new" / "deploy" / "station" / "systemd").mkdir(parents=True)
    (opt / "releases" / "new" / "deploy" / "station" / "systemd" / TR_UNIT).write_text("unit\n")
    (opt / "releases" / "old" / "BUILD").write_text("a" * 40 + "\n")
    sysd.mkdir()
    (sysd / TR_UNIT).write_text("unit\n")
    calls, lines = [], []
    u = Updater(Paths(opt=opt, systemd=sysd), out=lines.append)
    u.systemctl = lambda *a: calls.append(a)
    u.before_switch("new")
    assert calls == [] and (sysd / TR_UNIT).exists()                  # it has the service: its installer looks after it
    u.before_switch("old")
    assert calls == [("disable", "--now", TR_UNIT), ("daemon-reload",)] and not (sysd / TR_UNIT).exists()
    assert any("aaaaaaa runs trunk-recorder inside sizeup-station" in ln for ln in lines)
    calls.clear()
    u.before_switch("old")
    assert calls == []                                                # nothing left to remove
    src = (ROOT / "sizeup" / "cloud" / "autoupdate.py").read_text()
    act = src[src.index("    def activate(self, name: str"):]
    assert act.index("self.before_switch(name)") < act.index("self.installer(")


def test_two_dongles_with_its_serial_start_nothing(tmp_path):
    """#242: never a guess between two dongles with one serial, by the service either."""
    s, pop, _ = supervisor(tmp_path, found=lambda: [(1, "00000001"), (4, "00000001")])
    ask(tmp_path, sdr="00000001", name="V3 #2")
    st = s.step()
    assert st["state"] == "ambiguous" and "Two dongles share serial 00000001 (indexes 1 and 4)" in st["detail"]
    assert "sizeup-station sdr-serial 4" in st["detail"] and pop.started == []
    u = rec.UnitRecorder("x", str(tmp_path), state_path=s.state, status_path=s.status_path)
    u.serial = "00000001"
    assert u.status()["state"] == "ambiguous"                         # the row says "serial shared"
    u.shutdown()
    # as configured (no dongle handed over): a source naming the shared serial is left out; none left: not started
    work = tmp_path / "tr"
    c = json.loads((work / "config.json").read_text())
    c["sources"] = [dict(c["sources"][0], device="rtl=00000001")]
    (work / "config.json").write_text(json.dumps(c))
    ask(tmp_path, sdr="", seq=2)
    st = s.step(time.time() + 100)
    assert st["state"] == "ambiguous" and "not started" in st["detail"] and pop.started == []
