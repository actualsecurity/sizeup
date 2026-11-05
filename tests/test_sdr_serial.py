"""Two dongles, one serial (#242): the station spots it, alerts loudly, never opens either by that serial (no job, no
trunk-recorder source, no readsb), a main channel gone silent alerts, and `sizeup-station sdr-serial` renames one
safely. Fakes only: the device lists are made up, rtl_fm / trunk-recorder don't exist, rtl_eeprom is a script here
and systemctl is never run."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import sizeup.app as appmod
from sizeup.app import App
from sizeup.cloud import health as healthmod
from sizeup.cloud.health import StationHealth, station_metrics
from sizeup.config import ChannelCfg
from sizeup.radio import adsbd, sdr, sdrserial
from sizeup.radio.recorder import TrunkRecorder
from tests.test_app import cfg, wait
from tests.test_health import T0, FakeHub, FakeNotifier, cfg as hcfg, radio, station_status

P25 = "Fire/EMS 15"
V3_2 = (1, "RTL2838UHIDIR", "00000001")          # the station's "V3 #2"
NESDR = (4, "NESDR SMArt v5", "00000001")         # the new one, on its factory serial
V4 = (0, "Blog V4", "v4")


# -- spotting it ------------------------------------------------------------------------------------------------------

def test_duplicates_and_the_alert_text():
    devs = [V4, V3_2, (2, "RTL2838UHIDIR", "adsb"), NESDR]
    assert sdr.duplicates(devs) == {"00000001": [(1, "RTL2838UHIDIR"), (4, "NESDR SMArt v5")]}
    assert sdr.duplicates([V4, V3_2]) == {}
    text = sdr.duplicate_text("00000001", sdr.duplicates(devs)["00000001"])
    assert text.startswith("Two dongles share serial 00000001 (indexes 1 and 4): sizeup can't tell them apart. "
                           "Fix: sizeup-station sdr-serial 4 <new-serial>")
    assert "(renames #4, NESDR SMArt v5)" in text
    # The product on record for 00000001 (while it was V3 #2's alone) says which is the newcomer, whatever the order.
    swapped = [(1, "NESDR SMArt v5"), (4, "RTL2838UHIDIR")]
    assert "sdr-serial 1 <new-serial>" in sdr.duplicate_text("00000001", swapped, {"00000001": "RTL2838UHIDIR"})
    three = [(0, "a"), (2, "b"), (5, "c")]
    assert sdr.duplicate_text("x1", three).startswith("Three dongles share serial x1 (indexes 0, 2 and 5)")
    assert sdr.ambiguity("00000001", devs).startswith("serial 00000001 is on 2 dongles (indexes 1 and 4)")
    assert sdr.ambiguity("v4", devs) == ""


def test_lookup_never_guesses_between_two(monkeypatch):
    plugged = [V4, V3_2]
    monkeypatch.setattr(sdr, "devices", lambda *a, **k: list(plugged))
    assert sdr.lookup("00000001") == (1, "") and sdr.resolve("00000001") == 1
    plugged.append(NESDR)
    i, why = sdr.lookup("00000001")
    assert i is None and "2 dongles" in why and sdr.resolve("00000001") is None
    assert sdr.lookup("v4") == (0, "") and sdr.lookup("gone") == (None, "")


# -- the app: no job on an ambiguous serial ---------------------------------------------------------------------------

@pytest.fixture
def rig(tmp_path, monkeypatch):
    plugged: list[tuple[int, str, str]] = []
    logs: list[str] = []
    apps: list[App] = []
    monkeypatch.setattr(appmod, "SWITCH_PAUSE_S", 0.0)
    monkeypatch.setattr(appmod, "SDR_WATCH_S", 3600.0)
    monkeypatch.setattr(appmod, "SDR_SETTLE_S", 0.0)
    monkeypatch.setattr(appmod, "log", logs.append)
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    monkeypatch.setattr(TrunkRecorder, "one_dongle", lambda self: (True, ""))
    (tmp_path / "clips").mkdir(exist_ok=True)
    base = replace(cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"),
                   channels=(ChannelCfg("South 2", 470.2, "paging", "fire-page"),
                             ChannelCfg("FG1", 453.0375, "traffic", "fire-fg")),
                   sdr_auto=True, sdr_auto_assign=True, sdr_names={"00000001": "V3 #2", "v4": "V4"},
                   p25_dir=str(tmp_path / "p25"), p25_talkgroups={307: P25},
                   p25_recorder=str(tmp_path / "no-trunk-recorder") + " --config=x", p25_recorder_dir=str(tmp_path))

    def make(**kw) -> App:
        app = App(replace(base, **kw)).start()
        apps.append(app)
        return app

    yield plugged, logs, make
    for a in apps:
        a.stop()


def test_an_ambiguous_dongle_gets_no_job_and_its_saved_job_waits(rig, tmp_path):
    plugged, logs, make = rig
    plugged[:] = [V3_2, V4]
    app = make()
    assert app.assign == {"00000001": "FG1", "v4": P25}
    assert json.loads((tmp_path / "sdr_products.json").read_text()) == {"00000001": "RTL2838UHIDIR", "v4": "Blog V4"}
    app.stop()
    # The NESDR SMArt plugged in on 00000001 too, then the station restarts.
    plugged[:] = [V4, V3_2, NESDR]
    logs.clear()
    app = make()
    assert app.assign == {"00000001": "FG1", "v4": P25}               # the saved job is kept (for after the fix)
    fg1 = app._receiver("FG1")
    assert wait(lambda: fg1.state == "ambiguous")                     # ... but never started on either dongle
    assert "serial 00000001 is on 2 dongles (indexes 1 and 4)" in fg1.detail and "sdr-serial 4" in fg1.detail
    assert any(m.startswith("sdr: CRITICAL Two dongles share serial 00000001 (indexes 1 and 4)") for m in logs)
    st = app.sdr()
    row = next(r for r in st["sdrs"] if r["serial"] == "00000001")
    assert row["ambiguous"].startswith("serial 00000001 is on 2 dongles")
    assert next(r for r in st["sdrs"] if r["serial"] == "v4")["ambiguous"] == ""           # V4 keeps working
    (dup,) = st["duplicates"]
    assert dup["indexes"] == [1, 4] and dup["job"] == "FG1" and dup["rename"] == 4 and dup["name"] == "V3 #2"
    assert dup["text"].startswith("Two dongles share serial 00000001 (indexes 1 and 4): sizeup can't tell them apart. "
                                  "Fix: sizeup-station sdr-serial 4 <new-serial>")
    # A person can't pick a job for it either, nor name it for ADS-B; Off is fine.
    assert "2 dongles" in app.set_mode("South 2", "00000001")
    assert app.set_adsb_receiver("00000001") is not None
    # The station's health carries it to the hub.
    h = station_metrics(app, healthmod.Metrics(str(tmp_path)))
    assert h["usb"]["duplicates"][0]["serial"] == "00000001" and len(h["usb"]["devices"]) == 3
    # Unplug the NESDR: 00000001 is V3 #2's alone again, FG1 runs there by itself (not opened before).
    plugged.remove(NESDR)
    app._sdr_arrivals()
    assert "sdr: serial 00000001 is on one dongle again (V3 #2 (00000001)): usable" in logs
    assert fg1.device() == "1" and fg1.device.why == ""               # its next look (every 15 s) starts it on #1


def test_a_new_ambiguous_dongle_is_never_auto_assigned(rig):
    plugged, logs, make = rig
    plugged[:] = [V4, V3_2, NESDR]
    app = make()
    assert app.assign == {"v4": "FG1"}                                # V4 gets FG1; 00000001 nothing
    assert any("V3 #2 (00000001) not auto-assigned: serial 00000001 is on 2 dongles" in m for m in logs)


def test_one_dongle_per_channel_refuses_a_shared_serial_too(tmp_path, monkeypatch):
    plugged = [(0, "a", "fire-fg"), (1, "b", "fire-fg")]
    monkeypatch.setattr(appmod.sdr_lookup, "devices", lambda *a, **k: list(plugged))
    c = replace(cfg(tmp_path), rtl_fm=str(tmp_path / "no-rtl_fm"), channels=(ChannelCfg("FG1", 453.0375, "traffic", "fire-fg"),))
    app = App(c).start()
    try:
        r = app._receiver("FG1")
        assert wait(lambda: r.state == "ambiguous") and "2 dongles" in r.detail
        plugged.pop()
        assert r.device() == "fire-fg"                                # one again: passed to rtl_fm as configured
    finally:
        app.stop()


# -- trunk-recorder: never a source on an ambiguous serial -------------------------------------------------------------

def test_trunk_recorder_config_leaves_out_an_ambiguous_source(tmp_path):
    conf = {"sources": [{"device": "rtl=00000001", "center": 477193750}, {"device": "rtl=fire-p25,buflen=8", "center": 1}],
            "systems": [{"shortName": "westchester", "unitScript": ""}]}
    (tmp_path / "config.json").write_text(json.dumps(conf))
    r = TrunkRecorder("trunk-recorder --config=config.json", str(tmp_path))
    plugged = [V3_2, NESDR, (2, "x", "fire-p25")]
    r.ambiguous_fn = lambda serial: sdr.ambiguity(serial, plugged)
    r._argv_for()
    out = json.loads((tmp_path / "config.runtime.json").read_text())
    assert [s["device"] for s in out["sources"]] == ["rtl=fire-p25,buflen=8"]          # never rtl=00000001
    assert "rtl=00000001" in r.left_out[0] and "2 dongles" in r.left_out[0]
    # Every source ambiguous: no config, no start.
    (tmp_path / "config.json").write_text(json.dumps(dict(conf, sources=conf["sources"][:1])))
    (tmp_path / "config.runtime.json").unlink()
    from sizeup.radio.recorder import AmbiguousSource
    with pytest.raises(AmbiguousSource):
        r._argv_for()
    assert not (tmp_path / "config.runtime.json").exists()
    # The handed-over dongle (an index) is used as before; an index is never ambiguous.
    r._argv_for(3)
    assert json.loads((tmp_path / "config.runtime.json").read_text())["sources"][0]["device"] == "rtl=3"


def test_trunk_recorder_waits_and_says_why(tmp_path):
    exe = tmp_path / "trunk-recorder"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    (tmp_path / "config.json").write_text(json.dumps({"sources": [{"device": "rtl=00000001"}]}))
    r = TrunkRecorder(f"{exe} --config=config.json", str(tmp_path))
    r.ambiguous_fn = lambda serial: sdr.ambiguity(serial, [V3_2, NESDR])
    r.start()
    try:
        assert wait(lambda: r.state == "ambiguous") and "not started" in r.detail and r._proc is None
    finally:
        r.stop()
    # Handed a dongle by the app: its lookup said why there's no index.
    def find():
        return None
    find.why = "serial 00000001 is on 2 dongles (indexes 1 and 4): ..."
    r2 = TrunkRecorder(f"{exe} --config=config.json", str(tmp_path))
    r2.device_fn, r2.device_name = find, "V3 #2"
    r2.start()
    try:
        assert wait(lambda: r2.state == "ambiguous") and "V3 #2" in r2.detail and "2 dongles" in r2.detail
    finally:
        r2.stop()


def test_the_ads_b_decoder_never_starts_on_a_shared_serial(tmp_path):
    from tests.test_adsb_receiver import decoder
    d, pop = decoder(tmp_path, lambda: [(0, "adsb1090"), (3, "adsb1090")])
    st = d.step(1000.0)
    assert st["state"] == "ambiguous" and pop.started == []
    assert st["detail"].startswith("Two dongles share serial adsb1090 (indexes 0 and 3): sizeup can't tell them apart.")


# -- the hub: the alerts -------------------------------------------------------------------------------------------------

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


def dup_status():
    st = station_status()
    st["sdr"]["sdrs"][2]["ambiguous"] = "serial 00000001 is on 2 dongles (indexes 1 and 4): not opened ..."
    text = sdr.duplicate_text("00000001", [(1, "RTL2838UHIDIR"), (4, "NESDR SMArt v5")])
    st["health"]["usb"] = {"devices": [{"index": 1, "product": "RTL2838UHIDIR", "serial": "00000001"},
                                       {"index": 4, "product": "NESDR SMArt v5", "serial": "00000001"}],
                           "duplicates": [{"serial": "00000001", "indexes": [1, 4], "name": "V3 #2", "job": P25,
                                           "products": ["RTL2838UHIDIR", "NESDR SMArt v5"], "text": text}]}
    return st


def test_a_duplicate_serial_is_a_critical_alert_pushed_to_the_status_topic():
    r = Rig()
    r.at(0, station_status())
    assert r.h.alerts() == []
    ch = r.at(5, dup_status())
    assert [(k, a["key"]) for k, a in ch] == [("start", "dup:00000001")]
    (a,) = r.h.alerts()
    assert a["severity"] == "critical" and a["title"] == "Duplicate dongle serial 00000001"
    assert a["text"].startswith("Two dongles share serial 00000001 (indexes 1 and 4): sizeup can't tell them apart. "
                                "Fix: sizeup-station sdr-serial 4 <new-serial>")
    assert "V3 #2's job Fire/EMS 15 doesn't (re)start until then" in a["text"]
    assert a["who"]["dongle"] == "SDR V3 #2" and a["who"]["channel"] == P25 and a["who"]["detail"] == "indexes 1, 4"
    (msg,) = r.n.msgs
    assert msg["priority"] == 4 and msg["title"].startswith("Duplicate dongle serial 00000001") and "sdr-serial 4" in msg["body"]
    m = r.h.model()
    assert m["duplicates"][0]["indexes"] == [1, 4] and len(m["usb"]) == 2
    assert next(d for d in m["dongles"] if d["serial"] == "00000103")["ambiguous"].startswith("serial 00000001")
    ch = r.at(10, station_status())                                     # renamed: it clears, with a recovery push
    assert [(k, a["key"]) for k, a in ch] == [("clear", "dup:00000001")] and r.n.msgs[-1]["title"].startswith("Recovered")


def busy(r, label, every, n, start=0.0, extra=None):
    """`label` heard every `every` s, n times from `start`; the last time."""
    t = start
    for i in range(n):
        t = start + i * every
        r.at(t, extra(t) if extra else station_status(radios=[radio(label, last_heard=T0 + t)]))
    return t


def test_a_main_channel_silent_far_beyond_its_usual_gap_is_a_warning():
    r = Rig()
    t = busy(r, "FG1", 300.0, 40)                                      # FG1 talks every 5 min, 39 gaps learned
    st = station_status(radios=[radio("FG1", last_heard=T0 + t)])
    c = r.h.model()["channels"][0]
    assert c["main"] and c["usual_now_s"] == 300.0 and c["alert_after_s"] == 3600.0   # 4 x 5 min, but at least 1 h
    r.at(t + 59 * 60, st)
    assert [a["key"] for a in r.h.alerts()] == []                      # 59 min: a hint only
    assert r.h.model()["hints"]
    ch = r.at(t + 61 * 60, st)
    assert [(k, a["key"]) for k, a in ch] == [("start", "quiet:FG1")]
    (a,) = r.h.alerts()
    assert a["severity"] == "warning" and a["title"] == "FG1 silent"
    assert "usually hears something within 5 min" in a["text"] and "alert past 60 min" in a["text"]
    assert "two with one serial?" in a["text"]
    heard = station_status(radios=[radio("FG1", last_heard=T0 + t + 62 * 60)])
    assert [(k, a["key"]) for k, a in r.at(t + 62 * 60, heard)] == [("clear", "quiet:FG1")]


def test_not_a_main_channel_or_still_learning_or_stopped_never_alerts():
    r = Rig()
    t = busy(r, "FG2", 300.0, 40)                                       # not a main channel: hint only
    r.at(t + 3 * 3600, station_status(radios=[radio("FG2", last_heard=T0 + t)]))
    assert r.h.alerts() == []
    r = Rig()
    t = busy(r, "FG1", 300.0, 20)                                       # 19 gaps: still learning
    r.at(t + 3 * 3600, station_status(radios=[radio("FG1", last_heard=T0 + t)]))
    assert r.h.alerts() == [] and r.h.model()["channels"][0]["alert_after_s"] is None
    r = Rig()
    t = busy(r, "FG1", 300.0, 40)
    r.at(t + 3 * 3600, station_status(radios=[radio("FG1", state="off", last_heard=T0 + t)]))
    assert [a["key"] for a in r.h.alerts()] == []                       # Off: no program to be silent


def test_the_usual_gap_is_that_time_of_days(monkeypatch):
    # Busy by day (every 5 min), slow at night (every 40 min): a silence that starts by day but runs into the night
    # is judged by the night's gaps (4 x 40 min), one by day by the day's (1 h minimum).
    monkeypatch.setattr(healthmod, "_part", lambda t: 0 if ((t - T0) // 3600) % 24 >= 12 else 2)
    r = Rig()
    t = busy(r, "South 2", 300.0, 140)                                  # hours 0-11.6: "day" (part 2)
    t = busy(r, "South 2", 2400.0, 12, start=12 * 3600.0)               # hours 12-19.3: "night" (part 0)
    tod = r.h.tod["South 2"]
    assert len(tod["2"]) >= 100 and len(tod["0"]) >= 11
    day = 24 * 3600.0 + 3600.0                                          # the next day, 01:00 (day part)
    st = station_status(radios=[radio("South 2", last_heard=T0 + day)])
    r.at(day, st)
    c = r.h.model()["channels"][0]
    assert c["usual_now_s"] == 300.0 and c["alert_after_s"] == 3600.0
    late = 24 * 3600.0 + 11.5 * 3600                                    # 11:30: the silence will run into the night
    st = station_status(radios=[radio("South 2", last_heard=T0 + late)])
    r.at(late + 70 * 60, st)
    c = r.h.model()["channels"][0]
    assert c["usual_now_s"] == 2400.0 and c["alert_after_s"] == 4 * 2400.0
    assert r.h.alerts() == []                                           # 70 min into a slow night: fine


def test_a_talkgroup_silent_while_others_keep_recording_alerts():
    # #242: tg:307 stopped at 19:28 while the P25 row (any talkgroup) kept hearing calls.
    r = Rig()

    def st(t, tg307):
        s = station_status(radios=[radio(P25, None, last_heard=T0 + t)])
        s["health"]["heard"] = {"tg:307": {"at": T0 + tg307, "name": P25}, "tg:333": {"at": T0 + 10, "name": "Airport"}}
        return s
    t = busy(r, "tg:307", 240.0, 40, extra=lambda t: st(t, t))
    c = next(c for c in r.h.model()["channels"] if c["label"] == "tg:307")
    assert c["main"] and c["name"] == "Fire/EMS 15 (tg:307)" and c["usual_now_s"] == 240.0
    assert not next(c for c in r.h.model()["channels"] if c["label"] == "tg:333")["main"]
    for dt in range(60, 3800, 60):                                      # the system keeps talking; 307 doesn't
        r.at(t + dt, st(t + dt, t))
    keys = [a["key"] for a in r.h.alerts()]
    assert keys == ["quiet:tg:307"]
    (a,) = r.h.alerts()
    assert a["title"] == "Fire/EMS 15 (tg:307) silent" and "trunk-recorder" in a["text"] and a["who"]["channel"] == "Fire/EMS 15 (tg:307)"


def test_main_channels_from_the_config(tmp_path):
    from sizeup.config import ConfigError, load
    p = tmp_path / "c.toml"
    p.write_text('[alerts]\nmain_channels = [307, "FG1"]\n')
    assert load(p).alerts_main_channels == ("tg:307", "FG1")
    p.write_text("")
    assert load(p).alerts_main_channels == ("tg:307", "FG1", "South 2", "HPN Tower")
    p.write_text('[alerts]\nmain_channels = "FG1"\n')
    with pytest.raises(ConfigError):
        load(p)


# -- sizeup-station sdr-serial ---------------------------------------------------------------------------------------------

FAKE_EEPROM = r'''#!{py}
import json, sys
from pathlib import Path
log = Path({log!r})
args = sys.argv[1:]
with log.open("a") as f:
    f.write(json.dumps(args) + "\n")
if {fail_backup} and "-r" in args:
    sys.exit(1)
if "-r" in args:
    Path(args[args.index("-r") + 1]).write_bytes(b"\x28\x32" + bytes(254))
    print("Dump to file successful.", file=sys.stderr)
    sys.exit(0)
if "-s" in args:
    print("Write new configuration to device [y/n]? ", end="", file=sys.stderr)
    ans = sys.stdin.read(1)
    with log.open("a") as f:
        f.write(json.dumps(["answered", ans]) + "\n")
    if ans != "y":
        sys.exit(0)
    print("\nConfiguration successfully written.\nPlease replug the device for changes to take effect.", file=sys.stderr)
'''


class Station:
    """A fake station for sdrserial.main: dongles, systemctl, pgrep, the person at the keyboard, the replug."""

    def __init__(self, tmp_path, devs, fail_backup=False, answers=("y",), claims=None):
        self.devs = list(devs)
        self.calls: list[list[str]] = []
        self.out: list[str] = []
        self.answers = list(answers)
        self.t = 1_793_800_000.0
        self.replugged = None                          # (index, new serial) once written
        self.log = tmp_path / "eeprom.log"
        exe = tmp_path / "rtl_eeprom"
        exe.write_text(FAKE_EEPROM.format(py=sys.executable, log=str(self.log), fail_backup=fail_backup))
        exe.chmod(0o755)
        state = tmp_path / "state"
        (state / "captures").mkdir(parents=True)
        (state / "state").mkdir()
        self.state = state
        conf = tmp_path / "config.toml"
        conf.write_text('[web]\nport = 1\n[radio]\nsdrs = "auto"\nsdr_names = { v4 = "V4", "00000001" = "V3 #2" }\n')
        self.claims = tmp_path / "claims"
        self.claims.mkdir()
        import subprocess

        def run(argv, **kw):
            self.calls.append(list(argv))
            if argv[0] == "systemctl":
                if argv[1] == "is-active":
                    return subprocess.CompletedProcess(argv, 0 if argv[-1] in ("sizeup-station.service",
                                                                                 "sizeup-adsb.service",
                                                                                 "sizeup-update.timer") else 3, "", "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[0] == "pgrep":
                return subprocess.CompletedProcess(argv, 1, "", "")
            assert argv[0] == str(exe), argv                 # nothing else is ever run
            kw.pop("timeout", None)
            return subprocess.run(argv, capture_output=True, text=True, timeout=20, **kw)

        def sleep(s):
            self.t += s
            w = [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
            s_args = next((a for a in w if a and a[0] == "-d" and "-s" in a), None)
            if s_args and self.replugged is None and self.t > 1_793_800_010.0:
                i, new = int(s_args[1]), s_args[3]          # the person replugs it: the new serial shows
                self.devs = [(d[0], d[1], new) if d[0] == i else d for d in self.devs]
                self.replugged = (i, new)

        self.env = sdrserial.Env(devices=lambda: list(self.devs), run=run, ask=self.ask, say=self.out.append,
                                 sleep=sleep, clock=lambda: self.t, is_root=lambda: True, rtl_eeprom=str(exe),
                                 config=str(conf), state=str(state), tr_config=str(tmp_path / "tr.json"),
                                 backups=tmp_path / "backups", claims_dirs=[self.claims],
                                 api=lambda path: {"sdr": {"sdrs": [{"serial": s, "mode": "Off", "name": s}
                                                                    for _i, _p, s in self.devs]}})

    def ask(self, prompt):
        self.out.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def text(self):
        return "\n".join(self.out)

    def eeprom(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []


@pytest.mark.parametrize("new, why", [
    ("00000002", "needs a letter"), ("ab", "3 to 16"), ("a" * 17, "3 to 16"), ("hpn twr", "3 to 16"),
    ("hpn_twr", "3 to 16"), ("V4", "3 to 16"), ("v4y", ""), ("hpn-twr", ""), ("adsb1090", ""),
])
def test_the_new_serial_is_checked(new, why):
    got = sdrserial.serial_problem(new, [V4, V3_2, NESDR, (2, "x", "V4X")], "00000001")
    assert (why in got and got) if why else got == "", got
    assert "already the serial of #2" in sdrserial.serial_problem("v4x", [(2, "x", "V4X")], "00000001")
    assert "already" in sdrserial.serial_problem("00000001x", [(0, "x", "00000001x")], "00000001")


def test_picking_the_dongle_by_index_or_serial():
    devs = [V4, V3_2, NESDR]
    assert sdrserial.pick("4", devs) == (NESDR, "") and sdrserial.pick("v4", devs) == (V4, "")
    d, why = sdrserial.pick("00000001", devs)
    assert d is None and "say which by its index" in why and "sdr-serial 4" in why
    assert sdrserial.pick("9", devs)[0] is None


def test_dry_run_shows_the_plan_and_touches_nothing(tmp_path):
    s = Station(tmp_path, [V4, V3_2, NESDR])
    assert sdrserial.main(["4", "hpn-twr", "--dry-run"], s.env) == 0
    t = s.text()
    assert "Dongle #4: NESDR SMArt v5, serial 00000001 (shared with #1)  ->  hpn-twr" in t
    assert f"-d 4 -r {tmp_path / 'backups' / '00000001-'}" in t and "-d 4 -s hpn-twr" in t
    assert "Dry run: nothing stopped or written." in t
    assert [c for c in s.calls if c[0] != "systemctl" or c[1] != "is-active"] == [] and s.eeprom() == []
    assert not (tmp_path / "backups").exists()


def test_refused_up_front(tmp_path):
    s = Station(tmp_path, [V4, V3_2, NESDR])
    assert sdrserial.main(["00000001", "hpn-twr"], s.env) == 2 and "say which by its index" in s.text()
    assert sdrserial.main(["4", "12345"], s.env) == 2 and "needs a letter" in s.text()
    s.devs.append((2, "RTL2838UHIDIR", "hpn-twr"))
    assert sdrserial.main(["4", "HPN-TWR"], s.env) == 2 and "HPN-TWR is already the serial of #2" in s.text()
    s.devs.pop()
    s.env.is_root = lambda: False
    assert sdrserial.main(["4", "hpn-twr"], s.env) == 2 and "sudo" in s.text()
    assert s.eeprom() == [] and not [c for c in s.calls if c[:2] == ["systemctl", "stop"]]


def test_the_rename_backs_up_writes_waits_for_the_replug_and_restarts(tmp_path):
    s = Station(tmp_path, [V4, V3_2, NESDR])
    (s.state / "captures" / "sdr_modes.json").write_text(json.dumps({"00000001": P25, "v4": "FG1",
                                                                      "_set_by": {"00000001": "person"}}))
    (s.state / "captures" / "sdr_products.json").write_text(json.dumps({"00000001": "RTL2838UHIDIR"}))
    assert sdrserial.main(["4", "hpn-twr"], s.env) == 0, s.text()
    stops = [c for c in s.calls if c[:2] == ["systemctl", "stop"]]
    starts = [c for c in s.calls if c[:2] == ["systemctl", "start"]]
    assert stops == [["systemctl", "stop", "sizeup-station.service", "sizeup-adsb.service", "sizeup-update.timer"]]
    assert starts == [["systemctl", "start", "sizeup-station.service", "sizeup-adsb.service", "sizeup-update.timer"]]
    (backup,) = (tmp_path / "backups").iterdir()
    assert backup.name.startswith("00000001-") and backup.read_bytes()[:2] == b"\x28\x32"
    assert s.eeprom() == [["-d", "4", "-r", str(backup)], ["-d", "4", "-s", "hpn-twr"], ["answered", "y"]]
    # The order: stopped, then backed up, then written; started again last.
    i_stop = s.calls.index(stops[0])
    i_r = next(k for k, c in enumerate(s.calls) if "-r" in c)
    i_w = next(k for k, c in enumerate(s.calls) if "-s" in c)
    assert i_stop < i_r < i_w < s.calls.index(starts[0])
    t = s.text()
    assert "unplug dongle #4 (NESDR SMArt v5" in t and "leave the other 00000001 plugged in" in t
    assert "OK: hpn-twr is on one dongle; 00000001 is on #1" in t
    # The newcomer was renamed: what names 00000001 (V3 #2's job, its name) stays with V3 #2.
    assert "Those meant the other dongle with 00000001, #1 (RTL2838UHIDIR)" in t
    assert json.loads((s.state / "captures" / "sdr_modes.json").read_text())["00000001"] == P25
    assert "Jobs now:" in t and "#4  hpn-twr  NESDR SMArt v5: Off" in t


def test_renaming_the_original_moves_its_job_and_prints_the_config_lines(tmp_path):
    # Swapped: the person renames V3 #2 itself (#1, the product on record): its job goes with it.
    s = Station(tmp_path, [V4, V3_2, NESDR])
    modes = s.state / "captures" / "sdr_modes.json"
    modes.write_text(json.dumps({"00000001": P25, "_set_by": {"00000001": "person"}}))
    (s.state / "captures" / "sdr_products.json").write_text(json.dumps({"00000001": "RTL2838UHIDIR"}))
    assert sdrserial.main(["1", "v3-2", "--yes"], s.env) == 0, s.text()
    assert json.loads(modes.read_text()) == {"v3-2": P25, "_set_by": {"v3-2": "person"}}
    t = s.text()
    assert "Those meant this dongle (#1: it has the product 00000001 had" in t
    assert '[radio] sdr_names: "v3-2" = "V3 #2"   (in place of "00000001" = "V3 #2")' in t


def test_which_dongle_was_meant_unknown_changes_nothing(tmp_path):
    s = Station(tmp_path, [V4, V3_2, NESDR])
    modes = s.state / "captures" / "sdr_modes.json"
    modes.write_text(json.dumps({"00000001": P25}))
    assert sdrserial.main(["4", "hpn-twr", "--yes"], s.env) == 0
    assert json.loads(modes.read_text()) == {"00000001": P25}
    assert "can't be told" in s.text() and "If they meant this one (#4, NESDR SMArt v5)" in s.text()


def test_no_backup_no_write_and_the_services_come_back(tmp_path):
    s = Station(tmp_path, [V4, V3_2, NESDR], fail_backup=True)
    assert sdrserial.main(["4", "hpn-twr", "--yes"], s.env) == 1
    assert "EEPROM backup failed" in s.text() and "nothing written" in s.text()
    assert not any("-s" in c for c in s.eeprom())
    assert [c for c in s.calls if c[:2] == ["systemctl", "start"]]


def test_a_backup_that_isnt_an_eeprom_is_no_backup(tmp_path):
    s = Station(tmp_path, [V4, V3_2, NESDR])
    s.env.rtl_eeprom = str(tmp_path / "bad")
    (tmp_path / "bad").write_text("#!/bin/sh\nexit 0\n")              # "succeeds", writes nothing
    (tmp_path / "bad").chmod(0o755)
    real = s.env.run
    s.env.run = lambda argv, **kw: real(argv, **kw) if argv[0] != str(tmp_path / "bad") else __import__(
        "subprocess").run(argv, capture_output=True, text=True, **{k: v for k, v in kw.items() if k != "timeout"})
    assert sdrserial.main(["4", "hpn-twr", "--yes"], s.env) == 1 and "no backup file" in s.text()


def test_shortfinal_holding_it_is_waited_for_never_killed(tmp_path):
    import os
    s = Station(tmp_path, [V4, V3_2, NESDR], answers=["y", "", "q"])
    (s.claims / "00000001.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getpid(), "mode": "TWR"}))
    assert sdrserial.main(["4", "hpn-twr"], s.env) == 1
    t = s.text()
    assert "shortfinal holds 00000001 (TWR" in t and "switch that dongle Off in shortfinal" in t
    assert s.out.count("> ") == 2 and "still holds 00000001: nothing written" in t
    assert s.eeprom() == [] and (s.claims / "00000001.json").exists()
    assert not any(c[0] in ("kill", "pkill") for c in s.calls)
    assert [c for c in s.calls if c[:2] == ["systemctl", "start"]]     # ours started again


def test_no_replug_times_out_and_says_so(tmp_path):
    s = Station(tmp_path, [V4, V3_2, NESDR])
    s.replugged = "never"
    assert sdrserial.main(["4", "hpn-twr", "--yes", "--wait", "20"], s.env) == 1
    assert "hpn-twr hasn't shown up in 20 s" in s.text() and "backup is kept" in s.text()
    assert [c for c in s.calls if c[:2] == ["systemctl", "start"]]


def test_the_station_cli_runs_it_as_root_with_the_release():
    cli = (Path(__file__).parent.parent / "deploy" / "station" / "sizeup-station").read_text()
    assert "sdr-serial)" in cli and 'need_root sdr-serial "$@"' in cli and "-m sizeup.radio.sdrserial" in cli
    assert "sdr.duplicate_text" in cli                                  # status / doctor flag a shared serial
    readme = (Path(__file__).parent.parent / "deploy" / "station" / "README.md").read_text()
    assert "## Two dongles, one serial" in readme and "sdr-serial 4 hpn-twr" in readme and "adsb1090" in readme
    assert adsbd.duplicate_text("x1", [0, 3]) == sdr.duplicate_text("x1", [(0, ""), (3, "")]).split(" (renames")[0].split(
        "; nothing opens")[0]
