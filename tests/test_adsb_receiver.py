"""Our own ADS-B receiver (GitHub #235): the decoder on the station (sizeup/radio/adsbd.py, sizeup-adsb.service), the
dongle reserved for it (never a radio, never auto-assigned; named in Settings by an admin with a confirm), the station
forwarding what it hears to the hub (sizeup/cloud/aircraftlink.py), the hub's merge (our receiver first while it hears
a plane, adsb.lol / adsb.fi when we lose it), the details by ICAO address, and the squawk pre-alert beside the alarm
(#238's sizeup/cad/airport_alerts.py, source "adsb").

No network, no real dongle: readsb is a fake Popen, the dongles a list, the hub in-process; the claims folder is a tmp
dir. Aircraft data: the recorded adsb.lol / adsb.fi answers of tests/test_aircraft.py (public data), and planes made up
here (made-up operator names; no real person's)."""

import ast
import gzip
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from sizeup import aircraft as A
from sizeup.aircraft import AircraftLayer, Plane, Snapshot, StationFeed, combine, parse_snapshot, unwire, wire
from sizeup.cad.airport_alerts import AircraftPreAlerts
from sizeup.cloud import configupdate
from sizeup.cloud.aircraftlink import REFRESH_S, AircraftForwarder, Deltas
from sizeup.cloud.client import HubClient, HubError
from sizeup.config import STATION_LL, ConfigError, load
from sizeup.radio import adsbd
from tests.test_aircraft import FI3, FI_URL, LOL3, LOL_URL, T3, Clock, Feeds

ROOT = Path(__file__).resolve().parent.parent
STATION = ROOT / "deploy" / "station"
TEMPLATE = STATION / "config.station.toml"


# ---- the decoder on the station ------------------------------------------------------------------------------------------

class FakeProc:
    def __init__(self, argv):
        self.argv, self.pid, self.code = argv, 4000 + len(argv), None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class FakePopen:
    def __init__(self):
        self.started: list[FakeProc] = []

    def __call__(self, argv, **kw):
        p = FakeProc(list(argv))
        self.started.append(p)
        return p


def receiver_conf(tmp_path, body: str = "") -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def decoder(tmp_path, found, body='[aircraft.receiver]\nsdr = "adsb1090"\n'):
    exe = tmp_path / "readsb"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    pop = FakePopen()
    d = adsbd.Decoder(config=str(receiver_conf(tmp_path, body)), override=str(tmp_path / "state" / "adsb_receiver.json"),
                      binary=str(exe), json_dir=str(tmp_path / "json"), status_path=str(tmp_path / "run" / "decoder.json"),
                      claims=tmp_path / "claims", popen=pop, found=found, log=lambda m: None)
    return d, pop


def readsb_output(tmp_path, now, messages, n=3, samples=1000.0):
    j = tmp_path / "json"
    j.mkdir(exist_ok=True)
    (j / "aircraft.json").write_text(json.dumps({"now": now, "messages": messages, "aircraft": [
        {"hex": f"a0000{i}", "lat": 41.0, "lon": -73.7, "seen": 0.1} for i in range(n)] + [{"hex": "a00009", "seen": 3}]}))
    (j / "stats.json").write_text(json.dumps({"total": {"local": {"samples_processed": samples}}}))


def test_readsb_runs_on_the_named_dongles_index_with_auto_gain_and_bias_tee_off_by_default():
    argv = adsbd.readsb_argv("/opt/sizeup/readsb/bin/readsb", 2)
    assert argv[:3] == ["/opt/sizeup/readsb/bin/readsb", "--device-type", "rtlsdr"]
    assert argv[argv.index("--device") + 1] == "2"                     # the index, never the serial (prefix matches)
    assert argv[argv.index("--gain") + 1] == "auto" and "--enable-biastee" not in argv
    assert argv[argv.index("--write-json") + 1] == adsbd.JSON_DIR and argv[argv.index("--write-json-every") + 1] == "1"
    assert "--net" not in argv and not any(a.startswith("--net-") for a in argv)   # no network ports
    assert (float(argv[argv.index("--lat") + 1]), float(argv[argv.index("--lon") + 1])) == pytest.approx(
        STATION_LL, abs=1e-5)                                           # the station's own spot: South End (#271)
    assert adsbd.readsb_argv("readsb", 0, "38.6", True)[-1] == "--enable-biastee"
    assert adsbd.AIRCRAFT_JSON == adsbd.JSON_DIR + "/aircraft.json"


def test_what_runs_comes_from_the_config_and_settings_wins(tmp_path):
    conf = receiver_conf(tmp_path, '[aircraft.receiver]\nsdr = "adsb1090"\ngain = 38.6\nbias_tee = true\n')
    ov = tmp_path / "adsb_receiver.json"
    s = adsbd.settings(str(conf), str(ov))
    assert s == {"sdr": "adsb1090", "source": "config", "gain": "38.6", "bias_tee": True, "problem": "",
                 "lat": STATION_LL[0], "lon": STATION_LL[1]}
    ov.write_text(json.dumps({"sdr": "lna-adsb"}))
    assert adsbd.settings(str(conf), str(ov))["sdr"] == "lna-adsb" and adsbd.settings(str(conf), str(ov))["source"] == "settings"
    ov.write_text(json.dumps({"sdr": ""}))                              # Settings released it: none, whatever the config says
    assert adsbd.settings(str(conf), str(ov))["sdr"] == ""
    ov.write_text(json.dumps({"sdr": "00000001"}))
    assert "all-digit" in adsbd.settings(str(conf), str(ov))["problem"]
    none = adsbd.settings(str(receiver_conf(tmp_path, "[station]\nid = 'x'\n")), str(tmp_path / "missing.json"))
    assert none["sdr"] == "" and none["gain"] == "auto" and none["bias_tee"] is False
    assert adsbd.gain_problem("auto") == "" and adsbd.gain_problem(49.6) == "" and adsbd.gain_problem("loud")
    assert adsbd.gain_problem(99)


def test_no_dongle_named_runs_nothing_and_looks_at_no_dongle(tmp_path):
    def no(*a):
        raise AssertionError("looked for dongles with none named")
    d, pop = decoder(tmp_path, no, body="")
    st = d.step(1000.0)
    assert st["state"] == "off" and pop.started == [] and not (tmp_path / "claims").exists()
    assert json.loads((tmp_path / "run" / "decoder.json").read_text())["state"] == "off"


def test_the_named_dongle_runs_readsb_with_its_claim_and_restarts_when_its_settings_change(tmp_path):
    found = [(0, "v4"), (1, "adsb1090")]
    d, pop = decoder(tmp_path, lambda: found)
    st = d.step(1000.0)
    assert st["state"] == "starting" and len(pop.started) == 1
    argv = pop.started[0].argv
    assert argv[argv.index("--device") + 1] == "1" and "--enable-biastee" not in argv
    c = json.loads((tmp_path / "claims" / "adsb1090.json").read_text())
    assert c["app"] == "sizeup" and c["mode"] == "adsb" and c["pid"] == os.getpid()
    for t, msgs in ((1001.0, 100), (1011.0, 600), (1021.0, 1100)):
        readsb_output(tmp_path, t, msgs)
        st = d.step(t + 0.2)
    assert st["state"] == "ok" and st["messages_per_s"] == 50.0 and st["aircraft"] == 4 and st["with_pos"] == 3
    assert st["last_message_at"] == 1021.0 and "4 aircraft (3 with a position), 50 messages/s" in st["detail"]
    receiver_conf(tmp_path, '[aircraft.receiver]\nsdr = "adsb1090"\nbias_tee = true\n')   # the LNA needs power
    d.step(1022.0)
    assert pop.started[0].code is not None                            # stopped ...
    d.step(1023.0)
    assert len(pop.started) == 2 and pop.started[1].argv[-1] == "--enable-biastee"   # ... and back with bias-tee
    pop.started[1].code = 1                                           # readsb dies (the dongle pulled)
    st = d.step(1030.0)
    assert st["state"] == "down" and "exited with code 1" in st["detail"] and not (tmp_path / "claims" / "adsb1090.json").exists()
    d.step(1031.0)
    assert len(pop.started) == 2                                      # backing off
    d.step(1033.0)
    assert len(pop.started) == 3
    d.stop("test")
    assert not (tmp_path / "claims" / "adsb1090.json").exists()


def test_a_stuck_readsb_is_restarted_and_a_quiet_one_says_no_signal(tmp_path):
    d, pop = decoder(tmp_path, lambda: [(0, "adsb1090")])
    d.step(1000.0)
    readsb_output(tmp_path, 1000.0, 10)
    st = d.step(1000.0 + adsbd.GRACE_S + adsbd.STALE_S + 1)           # aircraft.json not rewritten
    assert st["state"] == "down" and "stuck" in st["detail"]
    (tmp_path / "b").mkdir()
    d2, pop2 = decoder(tmp_path / "b", lambda: [(0, "adsb1090")])
    d2.step(2000.0)
    readsb_output(tmp_path / "b", 2000.0, 10)
    for t in range(2002, 2000 + int(adsbd.NO_SIGNAL_S) + 30, 5):      # rewritten every second, nothing decoded
        readsb_output(tmp_path / "b", float(t), 10, samples=float(t))
        st = d2.step(t + 0.2)
    assert st["state"] == "no_signal" and "bias-tee is off" in st["detail"]


def test_shortfinal_holding_the_dongle_is_reported_and_never_fought(tmp_path):
    claims = tmp_path / "claims"
    claims.mkdir()
    held = {"app": "shortfinal", "pid": os.getppid(), "mode": "adsb", "since": 1.0}
    (claims / "adsb1090.json").write_text(json.dumps(held))
    d, pop = decoder(tmp_path, lambda: [(0, "adsb1090")])
    st = d.step(1000.0)
    assert st["state"] == "held" and st["held_by"] == {"app": "shortfinal", "mode": "adsb"} and pop.started == []
    assert "shortfinal holds adsb1090" in st["detail"]
    assert json.loads((claims / "adsb1090.json").read_text()) == held     # its claim untouched
    (claims / "adsb1090.json").unlink()                                     # they switch it Off: ours now
    assert d.step(1005.0)["state"] == "starting" and len(pop.started) == 1


def test_an_unplugged_dongle_or_two_with_one_serial_is_never_guessed(tmp_path):
    d, pop = decoder(tmp_path, lambda: [(0, "v4")])
    assert d.step(1000.0)["state"] == "unplugged" and pop.started == []
    d2, pop2 = decoder(tmp_path, lambda: [(0, "adsb1090"), (1, "adsb1090")])
    st = d2.step(1000.0)
    assert st["state"] == "ambiguous" and pop2.started == []          # #242: said so, with the fix
    assert st["detail"].startswith("Two dongles share serial adsb1090 (indexes 0 and 1): sizeup can't tell them apart. "
                                   "Fix: sizeup-station sdr-serial 1 <new-serial>")
    d3, _ = decoder(tmp_path, lambda: [(0, "00000001")], body='[aircraft.receiver]\nsdr = "00000001"\n')
    assert d3.step(1000.0)["state"] == "refused"


def test_an_old_status_says_the_service_is_not_running(tmp_path):
    p = tmp_path / "decoder.json"
    p.write_text(json.dumps({"state": "ok", "at": 1000.0}))
    assert adsbd.read_status(str(p), now=1010.0)["state"] == "ok"
    assert adsbd.read_status(str(p), now=1100.0)["state"] == "down"
    assert adsbd.read_status(str(tmp_path / "none.json")) is None


def test_the_decoder_runs_on_the_system_python_with_the_standard_library_only():
    tree = ast.parse((ROOT / "sizeup" / "radio" / "adsbd.py").read_text())
    mods = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
        {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert mods - {"__future__"} <= set(sys.stdlib_module_names), mods
    r = subprocess.run([sys.executable, "-I", str(ROOT / "sizeup" / "radio" / "adsbd.py"), "help"], capture_output=True, text=True)
    assert r.returncode == 2 and "usage: adsbd.py" in r.stderr


def test_the_installer_builds_a_pinned_readsb_and_runs_it_as_its_own_service():
    import re
    sh = (STATION / "install-linux.sh").read_text()
    assert re.search(r'READSB_TAG="v3\.\d+\.\d+"', sh) and re.search(r'READSB_COMMIT="[0-9a-f]{40}"', sh)
    for want in ('READSB_REPO="https://github.com/wiedehopf/readsb.git"', "RTLSDR=yes HAVE_BIASTEE=yes", 'RTLSDR_PREFIX="$RTLSDR_B"',
                 '"readsb=$READSB_B"', "libzstd-dev", 'install -D -m 0644 "$sys/../../sizeup/radio/adsbd.py" "$LIB/adsbd.py"',
                 'adsb_was=$(adsb_sig)', '[ "$adsb_was" = "$(adsb_sig)" ]', "rtl-sdr|trunk-recorder|whisper|readsb",
                 '"$rs/bin/readsb" --version'):
        assert want in sh, want
    unit = (STATION / "systemd" / "sizeup-adsb.service").read_text()
    for want in ("User=sizeup", "SupplementaryGroups=plugdev", "Environment=HOME=/var/lib/sizeup",
                 "ExecStart=/usr/bin/python3 -I /usr/local/lib/sizeup/adsbd.py run", "RuntimeDirectory=sizeup-adsb",
                 "Restart=always", "StartLimitIntervalSec=0", "KillMode=control-group", "WantedBy=multi-user.target"):
        assert want in unit, want
    assert adsbd.RUN_DIR == "/run/sizeup-adsb" and adsbd.OVERRIDE.startswith("/var/lib/sizeup/state/")
    cli = (STATION / "sizeup-station").read_text()
    assert "sizeup-adsb.service" in cli and "/run/sizeup-adsb/decoder.json" in cli


def test_an_installed_station_gets_the_receiver_section_once(tmp_path):
    text = TEMPLATE.read_text()
    old = text[:text.index("\n# Our own ADS-B receiver")] + text[text.index("\n[cloud]"):]
    conf, marks = tmp_path / "config.toml", tmp_path / "config-updates"
    conf.write_text(old.replace("/var/lib/sizeup/trunk-recorder/talkgroups.csv", f"{ROOT}/deploy/trunk-recorder/talkgroups.csv"))
    out = configupdate.run(conf, marks)
    assert any(x.startswith("adsb-receiver: [aircraft.receiver] added") for x in out)
    assert "adsb-receiver" in marks.read_text().split()
    c = load(conf)
    assert c.aircraft_sdr == "" and c.aircraft_gain == "auto" and c.aircraft_bias_tee is False and not c.aircraft_receiver
    assert (tmp_path / "config.toml.before-adsb-receiver").read_text() == old.replace(
        "/var/lib/sizeup/trunk-recorder/talkgroups.csv", f"{ROOT}/deploy/trunk-recorder/talkgroups.csv")
    assert configupdate.run(conf, marks) == []                        # once
    assert configupdate.add_adsb(text)[0] == text                     # the template has it already
    old_release = tmp_path / "rel" / "sizeup"
    old_release.mkdir(parents=True)
    (old_release / "config.py").write_text("# a release from before #235: no gain / bias keys\n")
    with pytest.raises(configupdate.Deferred):                        # a failed switch would go back to it: wait
        configupdate.add_adsb(old, tmp_path / "rel")
    assert configupdate.add_adsb(old, ROOT)[0] != old                 # this release reads it


def test_the_receiver_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[aircraft.receiver]\nenabled = true\nsdr = "adsb1090"\nbias_tee = true\n')
    c = load(p)
    assert c.aircraft_receiver_1090 == adsbd.AIRCRAFT_JSON and c.aircraft_gain == "auto" and c.aircraft_bias_tee is True
    assert "adsb1090" in c.sdr_ignore and c.aircraft_feeds[0] == "receiver"
    p.write_text('[aircraft.receiver]\nsdr = "adsb1090"\ngain = 38.6\n')
    assert load(p).aircraft_gain == "38.6" and load(p).aircraft_bias_tee is False
    for bad in ('gain = "loud"', "gain = 99", 'bias_tee = "yes"', 'sdr = "00000001"'):
        p.write_text(f"[aircraft.receiver]\n{bad}\n")
        with pytest.raises(ConfigError):
            load(p)


# ---- the reserved dongle: never a radio, never auto-assigned; named by an admin with a confirm -------------------------

@pytest.fixture
def station_app(tmp_path, monkeypatch):
    import sizeup.app as appmod
    from sizeup.config import ChannelCfg
    from tests.test_app import cfg as app_cfg
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    plugged = [(0, "Blog V4", "v4"), (1, "RTL2838UHIDIR", "adsb1090"), (2, "RTL2838UHIDIR", "fire-fg")]
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    monkeypatch.setattr(appmod.sdr_lookup, "resolve", lambda serial, *a: next((i for i, _p, s in plugged if s == serial), None))
    monkeypatch.setattr(appmod.claims, "DIR", tmp_path / "claims")
    monkeypatch.setattr(adsbd, "STATUS", str(tmp_path / "run" / "decoder.json"))
    c = replace(app_cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"), cad_db=str(tmp_path / "state" / "cad.sqlite"),
                channels=(ChannelCfg("South 2", 470.2, "paging", "fire-page"), ChannelCfg("FG1", 453.0375, "traffic", "fire-fg")),
                sdr_auto=True, sdr_auto_assign=True, sdr_auto_order=("fg1", "south2"), aircraft_sdr="adsb1090",
                sdr_ignore=("adsb1090",))                              # as sizeup/config.py makes it
    (tmp_path / "state").mkdir(exist_ok=True)
    return appmod, c, plugged


def test_the_adsb_dongle_is_never_a_radio_and_never_auto_assigned(station_app, tmp_path):
    appmod, c, plugged = station_app
    app = appmod.App(c).start()
    try:
        time.sleep(0.3)
        app._sdr_arrivals()
        st = app.state()["sdr"]
        assert "adsb1090" not in [x["serial"] for x in st["sdrs"]] and "adsb1090" not in app.assign
        assert st["adsb"]["serial"] == "adsb1090" and st["adsb"]["present"] is True and st["adsb"]["source"] == "config"
        assert app.set_mode("FG1", "adsb1090") is not None                   # never offered, never taken
        assert app._auto_assign("adsb1090") is None or "adsb1090" not in app.assign
        assert "adsb1090" not in app._plugged()
    finally:
        app.stop()


def test_naming_a_dongle_stops_its_job_and_settings_survives_a_restart_and_reaches_the_decoder(station_app, tmp_path):
    appmod, c, plugged = station_app
    app = appmod.App(c)
    try:
        assert app.set_mode("FG1", "fire-fg") is None and app.assign["fire-fg"] == "FG1"
        assert app.set_adsb_receiver("00000001") and "all-digit" in app.set_adsb_receiver("00000001")
        assert app.set_adsb_receiver("fire-fg") is None                      # the LNA dongle moved to fire-fg's USB slot
        assert "fire-fg" not in app.assign and "fire-fg" not in app.sdrs and app.adsb_sdr == "fire-fg"
        assert app.state()["sdr"]["adsb"]["serial"] == "fire-fg" and app.state()["sdr"]["adsb"]["source"] == "settings"
        assert "FG1" not in app.assign.values()                              # its job stopped first
        # the config's dongle is free again: Off, set by a person, never given a job by itself
        assert app.assign["adsb1090"] == "Off" and app.set_by["adsb1090"] == "person"
        assert app._auto_assign("adsb1090") is None and app.assign["adsb1090"] == "Off"
        # the decoder (another process) reads the same choice
        s = adsbd.settings(str(tmp_path / "no-config.toml"), str(tmp_path / "state" / "adsb_receiver.json"))
        assert s["sdr"] == "fire-fg" and s["source"] == "settings"
    finally:
        app.stop()
    again = appmod.App(c)
    try:
        assert again.adsb_sdr == "fire-fg" and "fire-fg" not in again.sdrs and again.assign.get("adsb1090") == "Off"
        assert again.set_adsb_receiver("") is None and again.adsb_sdr == ""   # released: none at all now
        assert again.assign["fire-fg"] == "Off" and again.set_by["fire-fg"] == "person"
    finally:
        again.stop()


def test_naming_it_takes_an_admin_and_a_confirm_and_the_hub_sends_it_to_the_station(station_app, tmp_path):
    import urllib.request
    from sizeup.cad.api import WRITES
    from sizeup.cloud.station import CALLS
    appmod, c, plugged = station_app
    assert next(role for rx, role in WRITES if rx.fullmatch("/api/adsb-receiver")) == "admin"
    assert "set_adsb_receiver" in CALLS
    app = appmod.App(c)
    server = appmod.make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.httpd.server_address[1]}/api/adsb-receiver"

    def post(body):
        req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        code, body = post({"sdr": "v4"})
        assert code == 400 and "confirm" in body["error"] and app.adsb_sdr == "adsb1090"
        assert post({"sdr": "v4", "confirm": True}) == (200, {"ok": True}) and app.adsb_sdr == "v4"
    finally:
        server.httpd.shutdown()
        app.stop()
    src = (ROOT / "sizeup" / "cloud" / "hub.py").read_text()
    assert 'return self._switch("set_adsb_receiver", serial or "")' in src


def test_settings_shows_the_reserved_row_and_asks_before_naming_or_releasing():
    js = (ROOT / "sizeup" / "web" / "static" / "radio-controls.js").read_text()
    for want in ("ADS-B receiver (reserved)", "Use for ADS-B", "UI.confirm(", "/api/adsb-receiver", "confirm: true", "ro()"):
        assert want in js, want


# ---- the station forwards what it hears to the hub -------------------------------------------------------------------------

def readsb_snap(now=T3, shift=0.0, changes=None):
    """The recorded traffic as our receiver would have it (readsb's layout): every aircraft, fresh."""
    d = json.loads(LOL3)
    recs = []
    for a in d["ac"]:
        a = dict(a, **(changes or {}).get(a["hex"], {}))
        if a.get("lat") is not None:
            a["lat"] += shift
        recs.append(a)
    return parse_snapshot({"now": now, "aircraft": recs}, band="1090")


def test_the_station_sends_only_what_changed_nearest_first_and_only_what_the_hub_can_use():
    snap = readsb_snap()
    extra = (Plane(hex="a0f350", lat=41.10, lon=-73.70, alt_baro=35000, seen=0.1, seen_pos=0.1),     # FL350 over the top
             Plane(hex="a0f040", lat=41.75, lon=-73.70, alt_baro=3000, seen=0.1, seen_pos=0.1),      # 40 nm north
             Plane(hex="a0f0ff", alt_baro=3000, seen=0.1))                                           # no position
    snap = Snapshot(snap.now, snap.planes + extra)
    d = Deltas()
    body, _ = d.build(snap, 100.0, True)
    hexes = [r[0] for r in body["ac"]]
    assert body["full"] and len(hexes) == 10 and not {"a0f350", "a0f040", "a0f0ff"} & set(hexes)
    dist = [A.nm(A.ARP[0], A.ARP[1], r[1], r[2]) for r in body["ac"]]
    assert dist == sorted(dist)                                       # nearest first
    assert all(len(str(r[1]).split(".")[1]) <= 5 for r in body["ac"])  # ~1 m
    d.sent_ok(body, snap, 100.0)
    assert d.build(snap, 101.0, True) == (None, False)                # nothing changed
    moved = readsb_snap(changes={"ab282b": {"lat": 41.0, "seen_pos": 0.0}})
    body, _ = d.build(moved, 102.0, True)
    assert [r[0] for r in body["ac"]] == ["ab282b"] and body["gone"] == [] and not body["full"]
    d.sent_ok(body, moved, 102.0)
    body, _ = d.build(moved, 100.0 + REFRESH_S, True)                 # still heard, unchanged: again every 5 s
    assert len(body["ac"]) == 9 and "ab282b" not in [r[0] for r in body["ac"]]
    d.sent_ok(body, moved, 100.0 + REFRESH_S)
    fewer = Snapshot(moved.now, tuple(p for p in moved.planes if p.hex != "a90a5f"))
    body, _ = d.build(fewer, 106.0, True)
    assert body["gone"] == ["a90a5f"] and body["ac"] == []
    capped = Deltas(cap=3)
    body, _ = capped.build(snap, 100.0, True)
    assert len(body["ac"]) == 3 and capped.dropped == 7


def test_an_emergency_squawk_goes_at_once_and_the_rest_waits_for_the_hubs_interval():
    d = Deltas()
    snap = readsb_snap()
    body, _ = d.build(snap, 100.0, True)
    d.sent_ok(body, snap, 100.0)
    moved = readsb_snap(shift=0.001)
    assert d.build(moved, 101.0, False) == (None, False)              # changed, not due: waits
    sq = readsb_snap(shift=0.001, changes={"a35692": {"squawk": "7700"}})
    body, urgent = d.build(sq, 101.0, False)
    assert urgent and body is not None and "a35692" in [r[0] for r in body["ac"]]


class FakeClient:
    """The hub as the forwarder's client: answers, or HubError while `down`."""

    def __init__(self):
        self.down, self.bodies, self.answer = False, [], {"ok": True, "full": False, "every": 1.0}
        self.last_sent = 0

    def request(self, method, path, body=None, timeout=None, gzip_body=False):
        assert method == "POST" and path == "/api/station/aircraft" and gzip_body
        raw = gzip.compress(json.dumps(body, separators=(",", ":")).encode(), 6)
        self.last_sent = len(raw)
        if self.down:
            raise HubError("can't reach the hub: ConnectionRefusedError")
        self.bodies.append(body)
        return dict(self.answer)

    def close(self):
        pass


def forwarder(snaps):
    fc = FakeClient()
    app = type("App", (), {"adsb_sdr": "adsb1090", "adsb_row": lambda self: {"serial": "adsb1090", "name": "LNA",
                                                                              "decoder": {"state": "ok"}}})()
    cfg = type("Cfg", (), {"aircraft_radius_nm": 15.0})()
    it = iter(snaps)
    f = AircraftForwarder(app, cfg, lambda timeout: fc, log=lambda m: None, source=lambda: next(it))
    return f, fc


def test_the_forwarder_drops_rather_than_queues_while_the_hub_is_offline_and_then_sends_everything():
    snaps = [readsb_snap(shift=i * 0.001) for i in range(12)]
    f, hub = forwarder(snaps)
    assert f.step(100.0) == "posted" and hub.bodies[0]["full"] and hub.bodies[0]["rx"]["sdr"] == "adsb1090"
    assert f.every == 1.0 and f.step(101.0) == "posted" and not hub.bodies[1]["full"]
    hub.down = True
    assert f.step(102.0) == "failed" and f.deltas.full and f.dropped_posts == 1
    assert f.step(102.5) == "waiting"                                 # backing off, nothing kept
    assert f.step(104.5) == "failed" and f.dropped_posts == 2
    hub.down = False
    assert f.step(107.0) == "posted"
    body = hub.bodies[-1]
    assert body["full"] and len(body["ac"]) == 10 and body["seq"] == 4   # everything, as of now: no backlog replayed
    assert [b["seq"] for b in hub.bodies] == [0, 1, 4]
    hub.answer = {"ok": True, "full": True, "every": 5.0}             # the hub missed one: everything next time
    f.step(108.0)
    assert f.every == 5.0 and f.deltas.full and f.step(109.0) == "waiting" and f.step(113.1) == "posted"
    assert hub.bodies[-1]["full"]


def test_what_forwarding_costs():
    """The recorded traffic (10 aircraft near HPN, all moving): bytes on the wire per post, gzipped."""
    snap = readsb_snap()
    d = Deltas()
    full, _ = d.build(snap, 100.0, True)
    d.sent_ok(full, snap, 100.0)
    moved = readsb_snap(shift=0.002)
    delta, _ = d.build(moved, 101.0, True)
    sizes = {}
    for name, b in (("full", full), ("delta", delta)):
        body = dict(b, v=1, epoch="0123456789ab", seq=1, at=101.0)
        sizes[name] = len(gzip.compress(json.dumps(body, separators=(",", ":")).encode(), 6))
    per_ac = sizes["delta"] / len(delta["ac"])
    assert len(delta["ac"]) == 10 and per_ac < 80, sizes               # ~50-60 bytes an aircraft
    assert per_ac * 20 < 1600                                          # 20 moving aircraft at 1 s: well under 2 KB/s


# ---- the hub: our receiver first while it hears a plane, the APIs when we lose it -----------------------------------------

def post_body(recs, seq=0, full=True, epoch="e1", gone=()):
    return {"v": 1, "epoch": epoch, "seq": seq, "full": full, "ac": [wire(p) for p in recs], "gone": list(gone)}


def test_the_wire_record_round_trips_and_nonsense_is_refused():
    p = readsb_snap().planes[0]
    r = unwire(wire(p))
    assert r["hex"] == p.hex and r["lat"] == round(p.lat, 5) and r["sq"] == p.squawk and r["fl"] == p.flight
    for bad in (None, [], ["zz", 1, 2], ["a00001"] * 3, [1] * 17):
        assert unwire(bad) is None
    assert unwire(["a00001", 999, 999] + [None] * 14)["lat"] is None


def test_the_receiver_wins_while_its_position_is_fresh_and_an_api_takes_a_plane_we_lost():
    api = parse_snapshot(json.loads(FI3))
    ab = next(p for p in api.planes if p.hex == "ab282b")
    rx_fresh = Snapshot(api.now, (replace(ab, seen_pos=3.0, lat=ab.lat + 0.001, feed=""),))   # older than adsb.fi's, < 10 s
    m = {p.hex: p for p in combine([("receiver", rx_fresh), ("adsbfi", api)]).planes}
    assert m["ab282b"].feed == "receiver" and m["ab282b"].lat == pytest.approx(ab.lat + 0.001)
    rx_lost = Snapshot(api.now, (replace(ab, seen_pos=A.RECEIVER_LOST_S + 2, feed=""),))
    m = {p.hex: p for p in combine([("receiver", rx_lost), ("adsbfi", api)]).planes}
    assert m["ab282b"].feed == "adsbfi"                               # we lost it: adsb.fi has it fresh
    only = Snapshot(api.now, (replace(ab, hex="a0beef", seen_pos=25.0),))
    assert {p.hex: p.feed for p in combine([("receiver", only), ("adsbfi", api)]).planes}["a0beef"] == "receiver"


def hub_layer(clock=None, fetch=None, **kw):
    return AircraftLayer(feeds=("adsblol", "adsbfi"), adsblol_url=LOL_URL, adsbfi_url=FI_URL, registry_url="https://faa.example/r.zip",
                         fetch=fetch or Feeds(), clock=clock or Clock(T3 + 0.5), log=lambda m: None, station_feed=True, **kw)


def lol_at(t: float) -> bytes:
    """adsb.lol's recorded answer as if it came at `t` (its own clock moved on: the same positions, newer)."""
    d = json.loads(LOL3)
    d["now"] = t * 1000
    return json.dumps(d).encode()


def test_the_hub_takes_the_stations_receiver_as_its_receiver_feed_and_lets_go_when_it_goes_quiet():
    clock = Clock(T3 + 0.5)
    ly = hub_layer(clock, Feeds(lol=(LOL3, lol_at(T3 + 11.5)), fi=(FI3, 503)))
    assert isinstance(ly.feeds[0], StationFeed) and ly.interval(ly.feeds[0], True) is None   # nothing yet: not read
    assert ly.receiver_status() is None and "receiver" not in [f["key"] for f in ly.feeds_public([])]
    heard = [p for p in readsb_snap(now=clock.t).planes if p.hex in ("ab282b", "a35692")]
    r = ly.station_ingest(post_body([replace(p, seen_pos=0.4) for p in heard]), 900)
    assert r == {"ok": True, "full": False, "every": A.FORWARD_SLOW_S}
    ly.asked = clock.t
    assert ly.station_ingest(post_body([], seq=1, full=False), 100)["every"] == A.FORWARD_FAST_S   # someone looks
    ly.refresh()
    src = {a["hex"]: a["src"] for a in ly.public()["aircraft"]}
    assert src["ab282b"] == "receiver" and src["a90a5f"] in ("adsblol", "adsbfi") and ly.receiver_healthy()
    rx = ly.public()["receiver"]
    assert rx["station"] and rx["forwarding"]["posts"] == 2 and rx["forwarding"]["aircraft"] == 2
    assert ly.station_ingest(post_body([], seq=5, full=False), 10)["full"] is True   # a post missed: ask for all
    clock.t += A.RECEIVER_KEEP_S + 1                                  # the station goes quiet
    for f in ly.feeds:
        f.st["rest_until"] = f.st["next"] = 0
    ly.refresh()
    assert not ly.receiver_healthy() and "receiver" not in {a["src"] for a in ly.public()["aircraft"]}
    assert "no word from the station's receiver" in ly.public()["receiver"]["note"]


def test_the_api_feeds_keep_their_polite_rates_with_the_receiver_up():
    ly = hub_layer()
    ly.station_ingest(post_body(list(readsb_snap().planes)), 900)
    assert ly.interval(ly.feeds[0], True) == 1.0 and ly.interval(ly.feeds[0], False) == 2.0   # in memory: free
    prim = ly.primary()
    assert ly.interval(prim, True) == A.PRIMARY_S and ly.interval(prim, False) == A.WATCH_S   # unchanged by #235
    other = next(f for f in ly.api_feeds() if f is not prim)
    assert ly.interval(other, True) == A.SECONDARY_S and ly.interval(other, False) is None


def test_a_forwarding_station_never_reads_the_apis_for_the_watch():
    fetch = Feeds()
    ly = AircraftLayer(feeds=("adsblol", "adsbfi"), adsblol_url=LOL_URL, adsbfi_url=FI_URL, fetch=fetch,
                       clock=Clock(T3 + 0.5), log=lambda m: None)
    ly.on_emergency = lambda ev: 1
    ly.active = lambda: False                                         # forwarding: the hub watches
    ly.tick()
    assert fetch.calls == [] and not ly.watching()
    ly.active = lambda: True
    ly.tick()
    assert fetch.calls == ["lol.example"]


def test_the_hub_endpoint_takes_gzip_with_the_station_token_only(tmp_path):
    from tests.test_cloud import TOKEN, HubRun, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        body = post_body(list(readsb_snap(now=time.time()).planes)[:3])
        ok = HubClient(hub.url, TOKEN).request("POST", "/api/station/aircraft", body, gzip_body=True)
        assert ok["ok"] and ok["every"] in (A.FORWARD_FAST_S, A.FORWARD_SLOW_S)
        assert len(hub.app.aircraft.station_feed().recs) == 3
        with pytest.raises(HubError) as e:
            HubClient(hub.url, "not-the-token-0000000000").request("POST", "/api/station/aircraft", body, gzip_body=True)
        assert e.value.status == 401
        with pytest.raises(HubError) as e:
            HubClient(hub.url, TOKEN).request("POST", "/api/station/aircraft", {"ac": "nope"})
        assert e.value.status == 400
        state = json.dumps(hub.app.cad.state())
        assert "ab282b" not in state and "a35692" not in state         # never in the state push (#146)
    finally:
        hub.close()


# ---- details by ICAO address ------------------------------------------------------------------------------------------------

def lol_with(recs, now_ms) -> bytes:
    return json.dumps({"now": now_ms, "ac": recs}).encode()


def test_a_plane_only_our_receiver_hears_keeps_its_details_from_the_apis_and_never_a_persons_name():
    clock = Clock(T3 + 0.5)
    company = {"hex": "a4d227", "flight": "N41DW", "r": "N41DW", "t": "SR22", "ownOp": "EXAMPLE FLYING LLC", "year": "2007",
               "lat": 41.04, "lon": -73.686, "alt_baro": 1500, "gs": 90, "track": 330, "seen_pos": 0.1, "seen": 0.1}
    person = {"hex": "a19e97", "flight": "EJA203", "r": "N203QS", "t": "CL35", "ownOp": "A PRIVATE PERSON",
              "lat": 41.05, "lon": -73.69, "alt_baro": 2500, "gs": 140, "track": 330, "seen_pos": 0.1, "seen": 0.1}
    fetch = Feeds(lol=(lol_with([company, person], T3 * 1000), lol_with([], (T3 + 30) * 1000)), fi=(404,))
    ly = hub_layer(clock, fetch)
    ly.registry.refresh()
    ly.refresh()                                                      # adsb.lol has both: their details are kept
    assert ly.details["a4d227"]["reg"] == "N41DW" and ly.details["a4d227"]["feed"] == "adsblol"
    clock.t += 30
    rx = [Plane(hex=h, flight=f, lat=lat, lon=-73.686, alt_baro=1400, gs=90.0, track=330.0, seen=0.2, seen_pos=0.2,
                source="adsb", band="1090") for h, f, lat in (("a4d227", "N41DW", 41.042), ("a19e97", "EJA203", 41.052))]
    ly.station_ingest(post_body(rx), 500)
    for f in ly.feeds:
        f.st["rest_until"] = f.st["next"] = 0
    ly.refresh()                                                      # adsb.lol has neither now; only we hear them
    v = {a["hex"]: a for a in ly.public()["aircraft"]}
    a = v["a4d227"]
    assert a["src"] == "receiver" and a["reg"] == "N41DW" and a["type"] == "SR22" and a["det"] == "adsblol"
    assert a["op"] == "Example Flying LLC" and a["op_src"] == "adsblol"   # the FAA lists an LLC: a company
    b = v["a19e97"]
    assert b["src"] == "receiver" and b["reg"] == "N203QS" and b["op_src"] in ("airline", "")   # its callsign's airline, if any
    assert "PRIVATE PERSON" not in json.dumps(ly.public()).upper()     # co-owned by people: never shown
    js = (ROOT / "sizeup" / "web" / "static" / "aircraft.js").read_text()
    assert "Position from ${SRC_NAME[a.src]" in js and "a.det" in js and "a.op" in js


# ---- the squawk pre-alert, beside the alarm ---------------------------------------------------------------------------------

def squawk(hex_="a4d227", sq="7700", lat=41.0400, lon=-73.6860, alt=900, feed="receiver", **kw) -> Snapshot:
    return Snapshot(0, (Plane(hex=hex_, flight="N41DW", reg="N41DW", type="SR22", alt_baro=alt, gs=80.0, track=330.0,
                              baro_rate=-500.0, squawk=sq, lat=lat, lon=lon, seen=0.2, seen_pos=0.2, category="A1",
                              source="adsb", band="1090" if feed == "receiver" else "", feed=feed, **kw),))


class PreWatch:
    """The aircraft watch, the CAD's alarms and its airport pre-alerts (#233's model, source "adsb"), driven by hand."""

    def __init__(self, tmp_path, drill=False):
        from tests.test_cad import make_cad
        self.cad = make_cad(tmp_path)
        if drill:
            self.cad.settings_post("alarms", {"action": "set", "key": "drill", "value": True, "operator": "Corey"})
        self.aa = self.cad.airport_alerts
        self.aa.notifier = self.pushed = PushLog()
        self.clock = Clock(T3)
        self.cad.clock = self.aa.clock = lambda: self.clock.t
        self.ly = self.layer()

    def layer(self):
        ly = AircraftLayer(adsblol_url=LOL_URL, registry_url="https://faa.example/r.zip", fetch=Feeds(), clock=self.clock,
                           log=lambda m: None)
        ly.on_emergency = self.cad.alarms.on_aircraft
        ly.on_clear = self.cad.alarms.aircraft_cleared
        ly.prealerts = AircraftPreAlerts(self.aa, self.cad)
        return ly

    def feed(self, snap, dt=5.0):
        self.clock.t += dt
        self.ly.tracks.update(Snapshot(self.clock.t, snap.planes))
        self.ly.view = self.ly._view()
        self.ly._watch(self.clock.t)

    def items(self, source="adsb"):
        return [r for r in self.aa._rows("1 = 1", (), 100) if r["source"] == source]


class PushLog:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, *a, **k):
        self.sent.append(title)
        return True


def test_a_squawk_puts_one_prealert_in_the_queue_beside_its_alarm_with_everything_known(tmp_path):
    w = PreWatch(tmp_path)
    for _ in range(4):
        w.feed(squawk())
    (p,), (al,) = w.items(), w.cad.alarms.history()
    d = p["data"]
    assert p["status"] == "open" and p["source"] == "adsb" and p["kind"] == "squawk" and d["alarm_id"] == al["id"]
    assert p["title"] == "AIRCRAFT EMERGENCY · N41DW · squawk 7700 (emergency)" and p["detail"].startswith("ADS-B: our receiver")
    lines = dict(d["lines"])
    assert lines["Aircraft"] == "N41DW · SR22"
    assert lines["Emergency"].startswith("squawk 7700 (emergency)")
    assert lines["Altitude"] == "900 ft, descending 500 ft/min, 80 kt"
    assert "nm SE of HPN" in lines["Position"] and "from the runway" in lines["Position"]
    assert lines["Phase"].startswith("arriving HPN")
    assert lines["Source"].startswith("our receiver on the station") and lines["Time"] and lines["ICAO address"] == "A4D227"
    assert "private owner" in lines["Operator"]                       # no operator known: and never a person's name
    assert d["aircraft"][0]["hex"] == "a4d227" and d["aircraft"][0]["alt"] == 900
    assert w.pushed.sent == []                                         # the alarm pushed already: not twice
    st = w.cad.state()["airport_alerts"]
    assert [x["id"] for x in st["open"]] == [p["id"]]
    assert any(e["kind"] == "prealert" and "Airport pre-alert (ADS-B)" in e["text"]
               for e in w.cad.store.q("SELECT * FROM events WHERE kind = 'prealert'"))


def test_its_details_are_brought_up_to_date_while_the_emergency_lasts(tmp_path):
    w = PreWatch(tmp_path)
    w.feed(squawk())
    w.feed(squawk(alt=700), A.PREALERT_UPDATE_S + 1)
    (p,) = w.items()
    assert dict(p["data"]["lines"])["Altitude"].startswith("700 ft") and p["data"]["aircraft"][0]["alt"] == 700
    assert p["count"] == 1                                             # updated in place, not added to


def test_one_event_shows_once_through_a_flicker_its_end_coming_back_and_a_hub_restart(tmp_path):
    w = PreWatch(tmp_path)
    w.feed(squawk())
    w.feed(squawk(sq="1200"), 20)
    w.feed(squawk(), 20)
    assert len(w.items()) == 1 and len(w.cad.alarms.history()) == 1
    w.feed(squawk(sq="1200"), 30)
    w.feed(squawk(sq="1200"), A.CLEAR_HOLD_S)                         # ended, as the alarm
    (p,) = w.items()
    assert p["status"] == "ended" and p["note"] == "ADS-B: squawk ended (now 1200)" and not w.cad.state()["airport_alerts"]["open"]
    w.feed(squawk(), 60)                                              # again within 30 min: the same item, open again
    (p2,) = w.items()
    assert p2["id"] == p["id"] and p2["status"] == "open" and len(w.cad.alarms.history()) == 1
    w.clock.t += 400                                                  # (past #238's 5 min de-duplication window)
    w.ly = w.layer()                                                  # the hub restarts while it still squawks
    w.feed(squawk())
    assert len(w.items()) == 1 and w.items()[0]["status"] == "open"
    assert w.items()[0]["data"]["alarm_id"] == max(a["id"] for a in w.cad.alarms.history())   # the newest alarm


def test_the_prealert_keeps_the_alarms_rate_limit_and_drill_mode(tmp_path):
    w = PreWatch(tmp_path)
    w.aa.max_per_hour = 2                                             # ATC's limit doesn't fold aircraft into each other
    planes = tuple(squawk(hex_=f"a0000{i}", lat=41.03 + i * 0.005).planes[0] for i in range(8))
    w.feed(Snapshot(0, planes))
    assert len(w.cad.alarms.history()) == A.MAX_ALARMS == 6 and len(w.items()) == 6     # #169's: 6 an hour
    assert len({r["subject"] for r in w.items()}) == 6
    (tmp_path / "drill").mkdir()
    d = PreWatch(tmp_path / "drill", drill=True)
    d.feed(squawk(feed="adsblol"))
    (p,) = d.items()
    assert p["data"]["drill"] and p["detail"].startswith("ADS-B: adsb.lol")


def test_an_airport_event_takes_it_and_a_dispatcher_accepts_or_dismisses_it(tmp_path):
    w = PreWatch(tmp_path)
    w.feed(squawk())
    (p,) = w.items()
    inc = w.cad.create({"type": "AIRPORT", "address": "240 Airport Rd", "operator": "Lt. Ruiz"})
    q = w.aa.get(p["id"])
    assert q["incident_id"] == inc["id"] and q["status"] == "open"    # on the event, still open: it ends with the squawk
    w.feed(squawk(hex_="a0bead", lat=41.03))
    r = next(x for x in w.items() if x["subject"].startswith("a0bead"))
    assert r["incident_id"] == inc["id"]                              # raised while that one Airport event is open
    out = w.cad.airport_alert(r["id"], "dismiss", {"operator": "Lt. Ruiz", "reason": "landed safely"})
    assert out["status"] == "dismissed" and out["operator"] == "Lt. Ruiz"
    acc = w.cad.airport_alert(p["id"], "accept", {"operator": "Lt. Ruiz"})
    assert acc["status"] == "accepted" and acc["incident"]["status"] == "pending"
    assert "Emergency: squawk 7700" in acc["incident"]["remarks"] and "ICAO address: A4D227" in acc["incident"]["remarks"]
    csv = w.cad.export("airport-alerts.csv").decode("utf-8-sig")
    assert "landed safely" in csv and "AIRCRAFT EMERGENCY" in csv and "accepted" in csv


def test_atc_and_adsb_share_one_queue_and_atc_is_unchanged(tmp_path):
    from sizeup.cad.airport_alerts import AirportAlert
    w = PreWatch(tmp_path)
    w.feed(squawk())
    a, new = w.aa.raise_alert(AirportAlert("atc", "fire_off", "fire:16", "HPN ATC: possible brush fire off runway 16",
                                           confidence=0.85, channel="HPN Tower",
                                           snippets=[{"id": "x1", "at": w.clock.t, "channel": "HPN Tower", "text": "smoke"}]))
    assert new and w.pushed.sent == ["HPN ATC: possible brush fire off runway 16"]     # ATC still pushes
    assert set(a["data"]) == {"snippets", "aircraft", "location", "phrases", "kinds", "event", "rank"}   # none of #235's
    inc = w.cad.create({"type": "AIRPORT", "address": "240 Airport Rd", "operator": "Lt. Ruiz"})
    assert w.aa.get(a["id"])["incident_id"] is None and w.aa.get(a["id"])["status"] == "open"   # ATC's: left to Accept
    assert {r["source"] for r in w.cad.state()["airport_alerts"]["open"]} == {"atc", "adsb"}
    assert w.aa.end("atc", "fire:16", "x")["status"] == "ended" and inc


def test_a_forwarding_station_raises_no_prealert_and_the_app_wires_it_on_the_cad_of_record():
    src = (ROOT / "sizeup" / "app.py").read_text()
    for want in ("self.aircraft.prealerts = AircraftPreAlerts(self.cad.airport_alerts, self.cad)",
                 "self.aircraft.active = lambda: not self.forwarding()"):
        assert want in src, want
    js = (ROOT / "sizeup" / "web" / "static" / "airport-alerts.js").read_text()
    assert "alarmWord" in js and "aa-lines" in js and "DRILL" in js
    assert not (ROOT / "sizeup" / "cad" / "airportalert.py").exists()
    assert not (ROOT / "sizeup" / "web" / "static" / "airport-prealert.js").exists()


def test_a_prealert_with_the_watch_off_here_is_never_raised(tmp_path):
    w = PreWatch(tmp_path)
    w.ly.active = lambda: False
    w.ly.on_emergency = lambda ev: None                               # App: None while forwarding
    w.feed(squawk())
    assert w.items() == []


# ---- station health ---------------------------------------------------------------------------------------------------------

def test_station_health_reports_the_decoder_and_alerts_when_it_is_down():
    from sizeup.cloud.health import adsb_health
    from tests.test_health import Rig, station_status
    row = {"serial": "adsb1090", "name": "LNA", "present": True, "held_by": None, "installed": True, "gain": "auto",
           "bias_tee": False, "decoder": {"state": "ok", "detail": "12 aircraft", "messages_per_s": 85.0, "aircraft": 12,
                                          "with_pos": 9, "last_message_at": 1_760_000_000.0, "restarts": 0},
           "forward": {"ok": True, "every_s": 1.0, "posts": 10, "bytes": 5000}}
    app = type("App", (), {"adsb_sdr": "adsb1090", "adsb_row": lambda self: row})()
    h = adsb_health(app)
    assert h["state"] == "ok" and h["messages_per_s"] == 85.0 and h["aircraft"] == 12 and h["forward"]["ok"]
    assert adsb_health(type("App", (), {"adsb_sdr": "", "adsb_row": lambda self: row})()) is None
    r = Rig()
    st = station_status()
    st["health"]["adsb"] = dict(h, state="down", detail="readsb exited with code 1")
    r.at(0, status=st)
    r.at(119, status=st)
    assert "adsb" not in r.keys()
    r.at(120, status=st)
    (a,) = [x for x in r.h.alerts() if x["key"] == "adsb"]
    assert a["title"] == "ADS-B receiver down" and "adsb.lol" in a["text"] and a["who"]["dongle"] == "SDR LNA"
    held = station_status()
    held["health"]["adsb"] = dict(h, state="held", held_by={"app": "shortfinal", "mode": "adsb"})
    r.at(130, status=held)
    assert "adsb" not in r.keys() and r.h.model()["adsb"]["held_by"]["app"] == "shortfinal"   # a handover: shown, not alerted
    js = (ROOT / "sizeup" / "web" / "static" / "health.js").read_text()
    assert "ADS-B receiver (station)" in js and "messages_per_s" in js
