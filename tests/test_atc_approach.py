"""NY Approach and HPN Ground (GitHub #260): airband channel choices beside HPN Tower. NY Approach's frequency follows the
FAA Chart Supplement's hours (126.4 0700-2300, 120.8 2300-0700 local), retuned between transmissions; the ADS-B match
is the facility's (Ground: the field; Approach: out to 40 NM, HPN's arrivals and departures). Synthetic audio, fake
ADS-B and fake rtl_fm only: no dongle, no network."""

from __future__ import annotations

import json
import stat
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup import atc_scope
from sizeup import atc_talkers as T
from sizeup import aircraft as A
from sizeup.aircraft import AircraftLayer, Plane, approach_phase
from sizeup.app import App
from sizeup.cad.service import Cad, Settings
from sizeup.cloud import configupdate
from sizeup.config import AUTO_ORDER, ChannelCfg, ConfigError, load
from sizeup.radio import am
from sizeup.radio.am import AmReceiver, CarrierSquelch, Schedule
from sizeup.radio.fm import FRAME, RATE, Channel
from sizeup.tz import TZ
from tests.test_atc import FIVE, envelope
from tests.test_sdr_auto_assign import DONGLES, P25, rig  # noqa: F401 (rig: the fixture)

ROOT = Path(__file__).resolve().parent.parent
STATION = ROOT / "deploy" / "station"
DAYNIGHT = ((7 * 60, 126.4), (23 * 60, 120.8))
APP = ChannelCfg("NY Approach", 126.4, "airband", "nyapp", 36.4, 3.5, schedule=DAYNIGHT)
APP3 = ChannelCfg("NY Approach 124.65", 124.65, "airband", "nyapp2", 36.4, 3.5)
GND = ChannelCfg("HPN Ground", 121.825, "airband", "hpn-gnd", 36.4, 3.5)
EIGHT = FIVE + (APP, APP3, GND)


def local(y, mo, d, h, mi=0, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp()


def utc(y, mo, d, h, mi=0, s=0) -> float:
    from datetime import timezone
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


# -- the frequencies (FAA Chart Supplement via airnav.com/airport/KHPN) and the templates -------------------------------


def test_the_templates_have_them_as_choices_never_auto_assigned(tmp_path, monkeypatch):
    from tests.test_station_install import station_config
    c = station_config(tmp_path, monkeypatch)
    ch = {x.label: x for x in c.channels}
    assert (ch["NY Approach"].mhz, ch["NY Approach"].kind, ch["NY Approach"].schedule) == (126.4, "airband", DAYNIGHT)
    assert (ch["NY Approach 124.65"].mhz, ch["NY Approach 124.65"].schedule) == (124.65, ())
    assert (ch["HPN Ground"].mhz, ch["HPN Ground"].kind) == (121.825, "airband")
    new = {"NY Approach", "NY Approach 124.65", "HPN Ground"}
    assert not new & set(c.sdr_auto_order) and not new & set(AUTO_ORDER)          # never auto-assigned
    assert c.sdr_auto_order[-1] == "HPN Tower"
    assert new <= set(c.stt_aws_channels)                                          # AWS by default, like HPN Tower
    hub = load(ROOT / "deploy" / "hub" / "config.hub.toml")
    assert new <= {x.label for x in hub.channels if x.kind == "airband"} and new <= set(hub.stt_aws_channels)
    ex = load(ROOT / "config.example.toml")
    assert new <= {x.label for x in ex.channels} and not new & set(ex.sdr_auto_order)


def write(tmp_path, chan: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text('[radio]\nsdrs = "auto"\n[[radio.channels]]\nlabel = "NY Approach"\n' + chan)
    return p


def test_the_schedule_in_the_config(tmp_path):
    good = 'mhz = 126.4\nkind = "airband"\nschedule = [{ at = "23:00", mhz = 120.8 }, { at = "07:00", mhz = 126.4 }]\n'
    assert load(write(tmp_path, good)).channels[0].schedule == DAYNIGHT          # in time order
    for bad, why in (
            ('mhz = 126.4\nkind = "airband"\nschedule = [{ at = "07:00", mhz = 126.4 }]\n', "two or more"),
            ('mhz = 126.4\nkind = "airband"\nschedule = [{ at = "7am", mhz = 126.4 }, { at = "23:00", mhz = 120.8 }]\n', "HH:MM"),
            ('mhz = 126.4\nkind = "airband"\nschedule = [{ at = "24:00", mhz = 126.4 }, { at = "23:00", mhz = 120.8 }]\n', "HH:MM"),
            ('mhz = 126.4\nkind = "airband"\nschedule = [{ at = "07:00", mhz = 126.4 }, { at = "07:00", mhz = 120.8 }]\n', "twice"),
            ('mhz = 126.4\nkind = "airband"\nschedule = [{ at = "07:00", mhz = 126.4 }, { at = "23:00", mhz = 453.0 }]\n', "airband voice"),
            ('mhz = 124.65\nkind = "airband"\nschedule = [{ at = "07:00", mhz = 126.4 }, { at = "23:00", mhz = 120.8 }]\n', "not one of"),
            ('mhz = 126.4\nkind = "airband"\nschedule = [{ at = "07:00", mhz = 126.4, x = 1 }, { at = "23:00", mhz = 120.8 }]\n', "a list"),
            ('mhz = 453.0375\nkind = "traffic"\nschedule = [{ at = "07:00", mhz = 126.4 }, { at = "23:00", mhz = 120.8 }]\n', "airband channels only")):
        with pytest.raises(ConfigError, match=why):
            load(write(tmp_path, bad))


# -- the schedule: which frequency when, local time, DST ------------------------------------------------------------------


@pytest.mark.parametrize("t, want", [
    (local(2026, 7, 1, 6, 59, 59), 120.8), (local(2026, 7, 1, 7), 126.4),          # summer (EDT)
    (local(2026, 7, 1, 22, 59, 59), 126.4), (local(2026, 7, 1, 23), 120.8),
    (local(2026, 7, 2, 0, 30), 120.8),                                             # past midnight: still the night's
    (utc(2026, 7, 1, 11), 126.4), (utc(2026, 7, 1, 10, 59), 120.8),                # 07:00 EDT is 11:00 UTC
    (utc(2026, 12, 1, 11, 30), 120.8), (utc(2026, 12, 1, 12), 126.4),              # 07:00 EST is 12:00 UTC
    (utc(2026, 12, 2, 3, 59), 126.4), (utc(2026, 12, 2, 4), 120.8),                # 23:00 EST is 04:00 UTC
    # DST starts 2026-03-08 (02:00 EST -> 03:00 EDT): that morning 07:00 is 11:00 UTC, an hour before the day before
    (utc(2026, 3, 7, 11, 30), 120.8), (utc(2026, 3, 8, 11, 30), 126.4),
    # DST ends 2026-11-01 (02:00 EDT -> 01:00 EST): that morning 07:00 is 12:00 UTC; 23:00 that night is 04:00 UTC
    (utc(2026, 10, 31, 11, 30), 126.4), (utc(2026, 11, 1, 11, 30), 120.8), (utc(2026, 11, 1, 12), 126.4),
    (utc(2026, 11, 2, 3, 30), 126.4), (utc(2026, 11, 2, 4), 120.8),
])
def test_the_schedule_switches_at_the_published_hours_in_local_time(t, want):
    assert Schedule(DAYNIGHT).mhz_at(t) == want


def test_the_schedule_says_itself():
    assert Schedule(DAYNIGHT).describe() == "126.4 07:00-23:00, 120.8 23:00-07:00"
    assert Schedule(((0, 1.0),)).mhz_at(local(2026, 1, 1, 12)) == 1.0


# -- retuning: only between transmissions -----------------------------------------------------------------------------------


def receiver(clock):
    events = []
    r = AmReceiver("rtl_fm", "", 36.4, Channel(126.4, "NY Approach"), lambda c, s: None, events.append, open_db=3.5,
                   schedule=DAYNIGHT)
    r.clock = lambda: clock[0]
    r.squelch = CarrierSquelch(open_db=3.5, clock=lambda: clock[0])
    return r, events


def feed(r, clock, frames):
    for f in frames:
        clock[0] += FRAME / RATE
        r._deliver(r.squelch.feed(f))


def test_never_retuned_mid_transmission():
    rng = np.random.default_rng(3)
    clock = [local(2026, 7, 1, 22, 59, 50)]
    r, events = receiver(clock)
    feed(r, clock, envelope([(4.0, 0.01, False)], rng))                     # the floor learned, 126.4 by day
    assert r.retune_due() is None
    feed(r, clock, envelope([(9.0, 0.1, True)], rng))                       # a transmission across 23:00
    assert clock[0] > local(2026, 7, 1, 23) and r.squelch.busy_since is not None
    assert r.retune_due() is None                                          # open: never cut
    clock[0] += am.RETUNE_FORCE_S + 1                                       # ... not even long overdue
    assert r.retune_due() is None
    clock[0] -= am.RETUNE_FORCE_S + 1
    feed(r, clock, envelope([(0.3, 0.01, False)], rng))                     # the carrier drops: in its hang
    assert r.squelch.busy_since is not None and r.retune_due() is None
    feed(r, clock, envelope([(0.5, 0.01, False)], rng))                     # closed: the clip was delivered
    assert r.squelch.busy_since is None and r.last_heard is not None
    assert r.retune_due() is None                                          # ... a read-back may follow: wait
    feed(r, clock, envelope([(am.RETUNE_QUIET_S + 0.1, 0.01, False)], rng))
    assert r.retune_due() == 120.8                                         # quiet: now
    r._retune_to = 120.8
    assert r._retuning()
    r._tune()                                                              # (what _run does before the next start)
    assert r.channel.mhz == 120.8 and r.channel.label == "NY Approach" and not r._retuning()
    assert r.argv("3")[r.argv("3").index("-f") + 1] == "120.8000M"
    assert events == ["NY Approach: retuned 126.4 -> 120.8 MHz (schedule: 126.4 07:00-23:00, 120.8 23:00-07:00)"]
    assert r.retune_due() is None and r.status()["schedule"] == "126.4 07:00-23:00, 120.8 23:00-07:00"


def test_a_busy_channel_retunes_late_but_still_between_transmissions():
    rng = np.random.default_rng(4)
    clock = [local(2026, 7, 2, 6, 59, 55)]
    r, _ = receiver(clock)
    r.channel = Channel(120.8, "NY Approach")
    feed(r, clock, envelope([(4.0, 0.01, False)], rng))
    talk = [(1.5, 0.1, True), (1.0, 0.01, False)] * 3                       # key-ups 1 s apart: never 2 s quiet
    for _ in range(int(am.RETUNE_FORCE_S / 7.5) + 10):
        feed(r, clock, envelope(talk, rng))
        if r.squelch.busy_since is None and r.retune_due() is not None:
            break
    late = clock[0] - local(2026, 7, 2, 7)
    assert late >= am.RETUNE_FORCE_S - 10 and r.squelch.busy_since is None and r.retune_due() == 126.4


def fake_rtl_fm(tmp_path) -> str:
    """An "rtl_fm" that writes each start's arguments to a file and then AM noise (a quiet channel) forever."""
    log = tmp_path / "starts.txt"
    exe = tmp_path / "rtl_fm"
    exe.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec "{sys.executable}" -I -c "'
                   "import os,sys,time,random\n"
                   "while True:\n"
                   " sys.stdout.buffer.write(bytes(random.randrange(60, 70) for _ in range(2400)))\n"
                   " sys.stdout.flush(); time.sleep(0.05)\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_the_receiver_restarts_rtl_fm_on_the_new_frequency_and_isnt_down(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "RETUNE_CHECK_S", 0.0)
    clock = [local(2026, 7, 1, 22, 59, 58)]
    events = []
    r = AmReceiver(fake_rtl_fm(tmp_path), "", 36.4, Channel(120.8, "NY Approach"), lambda c, s: None, events.append,
                   open_db=3.5, schedule=DAYNIGHT)
    r.clock = lambda: clock[0]
    r.start()
    starts = tmp_path / "starts.txt"
    try:
        deadline = time.time() + 10
        while not (starts.exists() and r.state == "ok") and time.time() < deadline:
            time.sleep(0.05)
        assert "-f 126.4000M" in starts.read_text()                        # started on the day frequency (22:59)
        clock[0] = local(2026, 7, 1, 23, 0, 5)
        while starts.read_text().count("\n") < 2 and time.time() < deadline:
            time.sleep(0.05)
        lines = starts.read_text().splitlines()
        assert len(lines) >= 2 and "-f 120.8000M" in lines[1] and r.channel.mhz == 120.8
        assert r.restarts == 0 and not any("down" in e for e in events)    # a retune, not a failure
        assert any("retuned 126.4 -> 120.8" in e for e in events)
    finally:
        r.stop()


def test_each_transmission_carries_the_frequency_it_was_heard_on():
    cad = Cad(Settings(airband=("NY Approach",)), log=lambda m: None)
    t = {"id": "NYApproach-1", "source": "NY Approach", "start": 100.0, "end": 103.0, "text": "", "status": "pending",
         "mhz": 120.8}
    cad.comms.record(t)
    cad.comms.record(dict(t, text="New York Approach, JetBlue 1234, descend and maintain four thousand", status="done",
                          mhz=None))                                       # the transcript later: the frequency stays
    rows = {r["id"]: r for r in cad.comms_query({"type": ["radio"]})["rows"]}
    assert rows["NYApproach-1"]["mhz"] == 120.8
    js = (ROOT / "sizeup" / "web" / "static" / "cad.js").read_text()
    assert "Heard on ${+t.mhz} MHz" in js
    assert "r.schedule ||" in (ROOT / "sizeup" / "web" / "static" / "radio-controls.js").read_text()


# -- the station: choices, never auto-assigned -------------------------------------------------------------------------------


def test_they_are_choices_and_never_auto_assigned(rig):
    plugged, logs, make = rig
    plugged[:] = DONGLES + [(4, "RTL2838UHIDIR", "five"), (5, "RTL2838UHIDIR", "nyapp"), (6, "RTL2838UHIDIR", "seven"),
                            (7, "RTL2838UHIDIR", "eight")]
    app = make(channels=EIGHT, sdr_auto_order=AUTO_ORDER)
    got = [app.assign.get(s, "Off") for _i, _p, s in plugged]
    assert got == ["FG1", P25, "South 2", "FG2", "HPN Tower", "Off", "Off", "Off"]   # the spare dongles wait for a person
    modes = app.state()["sdr"]["modes"]
    assert {"NY Approach", "NY Approach 124.65", "HPN Ground"} <= set(modes)
    assert app.set_mode("NY Approach", "nyapp") is None
    r = app._receiver("NY Approach")
    assert isinstance(r, AmReceiver) and r.schedule is not None and r.schedule.describe().startswith("126.4 07:00-23:00")
    fm = {x["key"]: x for x in app.channels_state()["fm"]}
    assert fm["NY Approach"]["schedule"] == "126.4 07:00-23:00, 120.8 23:00-07:00" and "schedule" not in fm["HPN Ground"]


# -- the one-time config update: safe, rollback-guarded ---------------------------------------------------------------------


def before_260() -> str:
    from tests.test_fg2 import before_260 as strip
    return strip((STATION / "config.station.toml").read_text())


def release(tmp_path, name, code: str) -> Path:
    rel = tmp_path / name
    (rel / "sizeup").mkdir(parents=True)
    (rel / "sizeup" / "config.py").write_text(code)
    return rel


def test_an_installed_station_gains_them_once(tmp_path, monkeypatch):
    from tests.test_fg2 import installed
    conf = installed(tmp_path, monkeypatch, before_260())
    marks = tmp_path / "etc" / "config-updates"
    marks.write_text("fg2\ntones-47\nhpn-atc\nadsb-receiver\n")
    before = load(conf)
    said = configupdate.run(conf, marks, leaving=release(tmp_path, "rel-new", (ROOT / "sizeup" / "config.py").read_text()))
    assert len(said) == 1 and said[0].startswith("approach-ground: NY Approach (126.4 MHz 0700-2300, 120.8 2300-0700")
    kept = conf.with_name("config.toml.before-approach-ground").read_text()
    assert conf.read_text() == kept + "".join(b for _l, _m, b in configupdate.APPROACH_GROUND)    # only added to
    after = load(conf)
    assert [c.label for c in after.channels][-3:] == ["NY Approach", "NY Approach 124.65", "HPN Ground"]
    assert after.channels[-3].schedule == DAYNIGHT and after.sdr_auto_order == before.sdr_auto_order   # not auto-assigned
    assert 'auto_assign_order = ["fg1", "p25", "south2", "fg2", "hpn tower"]' in conf.read_text()
    assert marks.read_text().endswith("approach-ground\n")
    conf.write_text(kept)                                                  # taken out by hand: stays out
    assert configupdate.run(conf, marks) == []


def test_it_waits_while_the_release_to_go_back_to_cant_read_the_schedule(tmp_path, monkeypatch):
    from tests.test_fg2 import installed
    conf = installed(tmp_path, monkeypatch, before_260())
    marks = tmp_path / "etc" / "config-updates"
    marks.write_text("fg2\ntones-47\nhpn-atc\nadsb-receiver\n")
    same = conf.read_text()
    old = release(tmp_path, "rel-233", 'KINDS = ("paging", "traffic", "airband")\n')     # reads airband, not a schedule
    assert configupdate.reads_airband(old) and not configupdate.reads_schedule(old)
    said = configupdate.run(conf, marks, leaving=old)
    assert said == ["approach-ground: deferred: the release this switch would go back to (rel-233) can't read an "
                    "airband channel's schedule: NY Approach and HPN Ground are added at the next update"]
    assert conf.read_text() == same and "approach-ground" not in marks.read_text()      # tried again next time
    assert configupdate.reads_schedule(None) and not configupdate.reads_schedule(tmp_path / "nowhere")
    # the next update: the release to go back to is the one that reads it
    said = configupdate.run(conf, marks, leaving=release(tmp_path, "rel-260", (ROOT / "sizeup" / "config.py").read_text()))
    assert said[0].startswith("approach-ground: NY Approach") and load(conf).channels[-1].label == "HPN Ground"


def test_only_the_missing_ones_and_never_in_the_way_of_hpn_tower():
    has_ground = ('[radio]\nsdrs = "auto"\nauto_assign_order = ["fg1"]\n[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\n'
                  '[[radio.channels]]\nlabel = "Ground"\nmhz = 121.825\nkind = "airband"\n')
    text, said = configupdate.add_approach_ground(has_ground)
    assert said.endswith("already there: HPN Ground") and "auto_assign_order" not in said
    d = tomllib.loads(text)
    assert [c["label"] for c in d["radio"]["channels"]] == ["FG1", "Ground", "NY Approach", "NY Approach 124.65"]
    assert d["radio"]["auto_assign_order"] == ["fg1"]
    assert "already has" in configupdate.add_approach_ground(text)[1] and configupdate.add_approach_ground(text)[0] == text
    assert configupdate.add_approach_ground("[web]\nport = 1\n")[1].startswith("approach-ground: no [[radio.channels]]")
    # a station that got #260's channels before HPN Tower (hpn-atc couldn't edit its order) still gets HPN Tower
    text, said = configupdate.add_hpn_atc(configupdate.add_approach_ground(
        '[radio]\nsdrs = "auto"\n[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\n')[0])
    assert said.startswith("hpn-atc: HPN Tower") and tomllib.loads(text)["radio"]["channels"][-1]["label"] == "HPN Tower"


# -- the ADS-B match: the facility's aircraft -----------------------------------------------------------------------------------


def plane(hex_, flight, phase, dist, **kw):
    return {"hex": hex_, "flight": flight, "reg": "", "type": "", "phase": phase, "dist": dist, **kw}


FAR_IN = plane("a00001", "JBU1234", "inbound", 30.0)          # 30 NM out, descending toward HPN: Approach's
CLOSE = plane("a00002", "DAL45", "arriving", 4.0)
ON_FIELD = plane("a00003", "EJA521", "ground", 0.4)
FAR_LOW = plane("a00004", "RPA4546", "low", 30.0)


def test_the_facility_and_its_aircraft():
    assert [atc_scope.facility(x) for x in ("HPN Tower", "NY Approach", "NY Approach 124.65", "HPN Ground", "HPN GND",
                                            "Clearance Delivery", "NY Departure")] == \
        ["tower", "approach", "approach", "ground", "ground", "ground", "approach"]
    planes = [FAR_IN, CLOSE, ON_FIELD, FAR_LOW, plane("a00005", "N1", "outbound", 39.0), plane("a00006", "N2", "inbound", 41.0)]
    hexes = lambda ch: [a["hex"] for a in atc_scope.planes_for(ch, planes)]   # noqa: E731
    assert hexes("NY Approach") == ["a00001", "a00002", "a00005"]          # out to 40 NM, arrivals and departures
    assert hexes("HPN Tower") == ["a00002", "a00003"]                      # as before #260: near HPN
    assert hexes("HPN Ground") == ["a00003"]                               # on the airport only
    assert atc_scope.widest_nm(["HPN Tower"]) == 15.0 and atc_scope.widest_nm(["HPN Tower", "NY Approach"]) == 40.0


def test_approach_matches_out_to_40_nm_and_tower_and_ground_dont():
    chans = ("HPN Tower", "NY Approach", "HPN Ground")
    clock = [1_800_000_000.0]
    cad = Cad(Settings(airband=chans), log=lambda m: None, clock=lambda: clock[0])
    tk = cad.atc_talkers
    tk.observe([FAR_IN, CLOSE, ON_FIELD], clock[0])
    assert tk.sightings.area_nm == 40.0

    def say(i, ch, text):
        t = {"id": f"{ch.replace(' ', '')}-{i}", "source": ch, "start": clock[0], "end": clock[0] + 3, "text": text}
        cad.comms.record(dict(t, status="done"))
        return tk.heard(t)
    r = say(1, "NY Approach", "JetBlue one two three four, New York Approach, descend and maintain four thousand")
    assert r["aircraft"]["status"] == "confirmed" and r["aircraft"]["hex"] == "a00001"
    assert r["talker"]["callsign"] == "APP"                                # the controller said its name
    r = say(2, "HPN Tower", "JetBlue one two three four, runway three four, cleared to land")
    assert r["aircraft"]["status"] == "heard"                              # 30 NM out: not the tower's
    r = say(3, "HPN Ground", "Execjet five twenty one, taxi to runway one six")
    assert r["aircraft"]["status"] == "confirmed" and r["talker"]["callsign"] == "GND"
    r = say(4, "HPN Ground", "Delta four five, runway three four, cleared to land")
    assert r["aircraft"]["status"] == "heard"                              # airborne: not on Ground
    r = say(5, "NY Approach", "New York Approach, JetBlue one two three four, with you descending through eight thousand")
    assert r["talker"]["callsign"] == "JBU1234" and r["addressed"]["callsign"] == "APP"
    assert T.facilities(["cleared", "visual", "approach", "runway"]) == []   # a procedure, not the facility


def test_the_pre_alert_takes_the_facilitys_aircraft():
    from sizeup.atc import AtcWatch, Rules

    class Alerts:
        def __init__(self):
            self.got = []

        def raise_alert(self, a):
            self.got.append(a)
            return {"id": len(self.got)}, True
    al = Alerts()
    w = AtcWatch(al, ["NY Approach", "HPN Tower"], Rules.load(), aircraft=lambda: [FAR_IN, CLOSE])
    w.heard({"id": "a", "source": "NY Approach", "start": 1.0, "end": 2.0,
             "text": "JetBlue one two three four declaring an emergency, engine fire"})
    w.heard({"id": "b", "source": "HPN Tower", "start": 1.0, "end": 2.0,
             "text": "JetBlue one two three four declaring an emergency, engine fire"})
    assert al.got[0].aircraft[0]["hex"] == "a00001"                       # Approach: matched 30 NM out
    assert al.got[1].aircraft[0]["hex"] is None                           # the tower: not its traffic


# -- the Aircraft layer: wider while NY Approach is heard, the map unchanged ----------------------------------------------------


def ac(**kw) -> Plane:
    base = dict(hex="abc123", lat=41.06, lon=-73.70, track=330.0, gs=220.0, alt_baro=9000, baro_rate=-1000.0, category="A3")
    return Plane(**{**base, **kw})


NORTH_30 = dict(lat=41.0669531 + 0.5, lon=-73.7075661)         # 30 NM north of HPN


def test_inbound_and_outbound_out_to_40_nm():
    assert approach_phase(ac(**NORTH_30, track=180.0), None, 0.0, 40.0) == A.INBOUND        # pointed at HPN, descending
    assert approach_phase(ac(**NORTH_30, track=0.0), None, 0.0, 40.0) == ""                 # descending away: not ours
    assert approach_phase(ac(**NORTH_30, track=180.0, alt_baro=20000), None, 0.0, 40.0) == ""   # above N90
    assert approach_phase(ac(**NORTH_30, track=180.0), None, 0.0, 20.0) == ""               # outside the radius
    climbing_away = ac(**NORTH_30, track=0.0, baro_rate=1500.0)
    assert approach_phase(climbing_away, None, 0.0, 40.0) == ""                             # not seen leaving HPN
    assert approach_phase(climbing_away, 100.0, 200.0, 40.0) == A.OUTBOUND                  # an HPN departure
    close = ac(lat=41.0669531 + 0.15, lon=-73.7075661, track=0.0, baro_rate=1500.0)         # 9 NM, climbing away
    assert approach_phase(close, None, 0.0, 40.0) == A.OUTBOUND
    assert approach_phase(ac(**NORTH_30, track=180.0, alt_baro="ground"), None, 0.0, 40.0) == ""


def test_the_layer_widens_for_approach_and_the_map_stays():
    from tests.test_aircraft import Clock, FakeFetch, layer
    clock = Clock()
    fetch = FakeFetch()
    ly = layer(clock, fetch)
    seen = []
    ly.on_view = lambda view, now: seen.append(list(view))
    ly.refresh()
    assert fetch.calls[-1].endswith("/15") and ly.wide == []
    ly.widen(atc_scope.APPROACH_NM)
    assert ly.widened() and ly.query_nm() == 40.0
    ly.refresh()
    assert fetch.calls[-1].endswith("/40")
    ly.tracks.update(A.Snapshot(clock.t, (ac(hex="f00001", flight="JBU1234", **NORTH_30, track=180.0),)))
    view = ly._view()
    assert "f00001" not in {a["hex"] for a in view}                       # never drawn
    wide = {a["hex"]: a for a in ly.wide}
    assert wide["f00001"]["phase"] == A.INBOUND and wide["f00001"]["dist"] == pytest.approx(30.0, abs=0.2)
    assert not {a["hex"] for a in view} & set(wide) and all(a["phase"] in (A.INBOUND, A.OUTBOUND) for a in ly.wide)
    clock.t += A.WIDE_HOLD_S + 1
    assert not ly.widened() and ly.query_nm() == 15.0
    ly.refresh()
    assert fetch.calls[-1].endswith("/15")


def test_an_approach_transcript_widens_the_layer():
    widened = []
    stub = SimpleNamespace(forwarding=lambda: False, _kind={"NY Approach": "airband", "HPN Tower": "airband"},
                           aircraft=SimpleNamespace(widen=widened.append), atc=None, cad=SimpleNamespace(atc_talkers=None))
    t = SimpleNamespace(id="x", source="HPN Tower", start=1.0, end=2.0, text="roger")
    App._atc(stub, t)
    assert widened == []
    App._atc(stub, SimpleNamespace(**dict(vars(t), source="NY Approach")))
    assert widened == [40.0]
    stub.forwarding = lambda: True                                          # a station forwarding: the hub does it
    App._atc(stub, SimpleNamespace(**dict(vars(t), source="NY Approach")))
    assert widened == [40.0]


def test_the_template_block_and_the_update_agree():
    tpl = tomllib.loads((STATION / "config.station.toml").read_text())["radio"]["channels"]
    upd = tomllib.loads("".join(b for _l, _m, b in configupdate.APPROACH_GROUND))["radio"]["channels"]
    assert [c for c in tpl if c["label"] in {"NY Approach", "NY Approach 124.65", "HPN Ground"}] == upd
    assert json.dumps(configupdate.APPROACH_SCHEDULE) == json.dumps(upd[0]["schedule"])
