"""The reception test (GitHub #410, sizeup/radio/reception.py): one dongle lent to one side of another P25 system
(MTA MRRS), measured instead of kept, and the job it paused given back.

No hardware, no trunk-recorder, no rtl_power, no network: trunk-recorder is a stream of lines in the shapes 5.2.0 logs
them (the timestamp, the level, "[mrrs]", tabs and colour codes as in tests/fixtures/trunk-recorder-sample.log; the
control channel lines as sizeup/radio/recorder.py and tests/test_trunk_silence.py have them), the dongles a list, the
claims a tmp folder (conftest.py), rtl_power a CSV written here. Every rate, level, call, unit and radio ID is invented.
"""

from __future__ import annotations

import io
import json
import os
import time
import wave
from dataclasses import replace
from pathlib import Path

import pytest

import sizeup.app as appmod
import sizeup.radio.claims as claims
from sizeup.config import ChannelCfg
from sizeup.radio import reception, systems
from sizeup.radio.p25 import Call, Turn, parse_call as load_call
from sizeup.radio.recorder import TrunkRecorder
from tests.test_app import FakeWhisper, cfg as app_cfg, voice
from tests.test_p25 import tr_call
from tests.test_p25_systems import MRRS, system_dir

PD, PD_SECURE, EMS = 9211, 9213, 9570
DONGLES = [(0, "Blog V4", "v4"), (1, "RTL2838UHIDIR", "trunk"), (2, "RTL2838UHIDIR", "page"), (3, "RTL2838UHIDIR", "air"),
           (4, "RTL2838UHIDIR", "spare")]
JOBS = {"v4": "FG1", "trunk": "Fire/EMS 15", "page": "South 2", "air": "HPN Tower"}


# -- a fake trunk-recorder: its lines -----------------------------------------------------------------------------------------


def tr(msg: str, level: str = "info", sysname: str = "mrrs") -> str:
    return f"[2026-11-10 09:00:00.000000] ({level})   [{sysname}]\t{msg}"


def rate_line(mhz: float, rate: int) -> str:
    return tr(f"freq: {mhz:.6f} MHz\tControl Channel Message Decode Rate: {rate}/sec, count:  {rate * 3}", "error")


def lock_lines(mhz: float = 769.90625, sysid: str = "26D", nac: str = "51", rfss: int = 81, site: int = 81) -> list[str]:
    return [tr(f"Started with Control Channel: {mhz:.6f} MHz"),
            tr(f"Decoding System ID {sysid} WACN: 91FE8 NAC: {nac}"),
            tr(f"Decoding System Site RFSS: {rfss:03d} SITE ID: {site:03d} ({rfss:03d}-{site:03d})")]


def uncovered_line(tg: int) -> str:
    return tr(f"\x1b[0;34m0C\x1b[0m\tTG: \x1b[35m{tg:>10}\x1b[0m\tFreq: 773.681250 MHz\t"
              "\x1b[36mNot Recording: no source covering Freq\x1b[0m", "error")


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def feed(probe_or_rt, clock: Clock, mhz: float, rates, lines=()) -> None:
    """The control channel's decode rate every 3 s, as "controlWarnRate": -1 logs it."""
    say = probe_or_rt.feed_line if isinstance(probe_or_rt, reception.Probe) else probe_or_rt.recorder.on_line
    for line in lines:
        say(line)
    for r in rates:
        clock.t += 3.0
        say(rate_line(mhz, r))


def a_call(tg: int, name: str = "1", secs: float = 4.0, errors: int = 0, signal=None, noise=None, at: float | None = None):
    at = time.time() if at is None else at
    return Call(f"{tg}-{name}", tg, "x", at, at + secs, ["4100001"], voice(secs, 1), "",
                [Turn(0.0, "4100001", "4100001", "", at=at, errors=errors)], system="mrrs", signal=signal, noise=noise)


# -- what a side measures -----------------------------------------------------------------------------------------------------


def test_the_shipped_system_logs_its_decode_rate_and_says_what_a_test_is_about():
    conf = json.loads((Path(MRRS.dir) / "config.json").read_text())
    assert conf["controlWarnRate"] == -1                       # trunk-recorder logs the rate every 3 s, good or bad
    assert MRRS.reception["watch"] == [PD, EMS] and "Harrison" in MRRS.reception["antenna"]
    assert "LTE" in MRRS.reception["caveat"] and conf["systems"][0]["control_channels"][0] == 769906250


def test_a_side_measures_the_control_channel_the_site_the_grants_and_the_voice():
    clock = Clock()
    p = reception.Probe(MRRS, "MRRS 770", "spare", clock)
    feed(p, clock, 769.90625, [0, 1], [tr("Started with Control Channel: 769.906250 MHz")])
    p.feed_line(tr("Retuning to Control Channel: 770.181250 MHz", "error").replace("\t", " "))
    feed(p, clock, 770.18125, [38, 40, 41, 39, 4, 40, 40, 40], lock_lines(770.18125)[1:])
    for line in ("SIZEUP-UNIT mrrs 4100001 call 9211", "SIZEUP-UNIT mrrs 4100002 call 9211",
                 "SIZEUP-UNIT mrrs 4100003 call 9570", "SIZEUP-UNIT mrrs 4100009 call 9213", "SIZEUP-UNIT mrrs 0 call 9213",
                 "SIZEUP-UNIT mrrs 4100004 call 31999", "SIZEUP-UNIT westchester 4100005 call 9211",
                 uncovered_line(PD)):
        p.feed_line(line)
    p.call(a_call(PD, secs=4.0, errors=2, signal=-41, noise=-62, at=clock.t))
    clock.t += 3600 - 30
    r = p.result()
    cc = r["cc"]
    assert (cc["found"], cc["mhz"], cc["rate"], cc["retunes"]) == (True, 770.18125, 35.2, 1)
    assert (cc["windows"], cc["decoded_pct"], cc["locked_pct"], cc["min"], cc["max"]) == (10, 70.0, 80.0, 0, 41)
    assert cc["dropout_s"] == 6 and cc["lock_s"] == 9.0             # the two windows before it locked; the 4/s one: 3 s
    assert [(f["mhz"], f["windows"], f["decoded_pct"]) for f in cc["per_freq"]] == [(769.90625, 2, 0.0), (770.18125, 8, 87.5)]
    assert cc["series"] == [28.3] and r["covers"] == [769.90625, 770.18125, 770.46875, 770.71875] and r["mhz"] == 770.3125
    assert r["site"] == {"seen": {"sysid": "26D", "wacn": "91FE8", "nac": "51", "rfss": 81, "site": 81}, "match": True,
                         "differs": []}
    tgs = {t["tg"]: t for t in r["talkgroups"]}
    assert [t["tg"] for t in r["talkgroups"]] == [PD, EMS]             # the watched ones first; nothing encrypted listed
    assert (tgs[PD]["grants"], tgs[PD]["uncovered"], tgs[PD]["calls"], tgs[PD]["audio_s"], tgs[PD]["err_per_s"]) == (2, 1, 1, 4.0, 0.5)
    assert (tgs[EMS]["grants"], tgs[EMS]["calls"], tgs[EMS]["calls_per_h"], tgs[PD]["calls_per_h"]) == (1, 0, 0.0, 1.0)
    assert (r["encrypted_grants"], r["unknown_grants"]) == (2, 1)      # counted: no radio ID, no talkgroup row
    assert "4100009" not in json.dumps(r) and "4100001" not in json.dumps(r)
    assert r["signal"]["voice_snr_db"] == 21.0 and r["signal"]["voice_db"] == -41.0 and r["signal"]["sweep"] is None
    assert r["voice"] == {"calls": 1, "audio_s": 4.0, "calls_per_h": 1.0, "err_per_s": 0.5}


def test_the_site_is_held_against_the_one_listed():
    listed = MRRS.site
    assert reception.site_check({}, listed)["match"] is None                            # nothing decoded
    assert reception.site_check({"nac": "51", "sysid": "26E"}, listed)["match"] is True   # hex, no leading zero
    bad = reception.site_check({"sysid": "1A2", "wacn": "91FE8", "nac": "3C1", "rfss": 81, "site": 12}, listed)
    assert bad["match"] is False and bad["differs"] == ["nac", "site", "sysid"]
    assert reception.site_check({"nac": "51"}, {})["match"] is None                     # nothing to hold it against


def test_a_call_json_gives_the_signal_and_noise_when_trunk_recorder_measured_them(tmp_path):
    name = "9211-1790954731.970_770181250.1-call_5"
    tr_call(tmp_path, name, [(4100002, 1.5, 0.3, "")], tg=PD)
    assert (c := load_call(tmp_path / f"{name}.json", {PD: "PD Dispatch"})).signal is None and c.noise is None   # 0 / 0
    meta = json.loads((tmp_path / f"{name}.json").read_text())
    meta.update(signal=-38, noise=-61, freq=770181250)
    (tmp_path / f"{name}.json").write_text(json.dumps(meta))
    c = load_call(tmp_path / f"{name}.json", {PD: "PD Dispatch"})
    assert (c.signal, c.noise, c.freq) == (-38.0, -61.0, 770181250.0)


def test_a_sweep_gives_each_listed_frequency_its_level_over_the_floor():
    lo, step, n = 769_112_500, 6250, 384
    vals = [-48.0 + (k % 3) * 0.2 for k in range(n)]
    for hz, db in ((769_906_250, -21.5), (770_181_250, -40.1)):
        vals[round((hz - lo) / step)] = db
    half = n // 2
    text = "\n".join(f"2026-11-10, 09:00:0{k}, {lo + k * half * step}, {lo + (k + 1) * half * step}, {step:.2f}, 4096, "
                     + ", ".join(f"{v:.1f}" for v in vals[k * half:(k + 1) * half]) for k in range(2))
    got = reception.parse_sweep(text, MRRS.covered([0]))
    assert got["floor_db"] == -47.8
    assert [(c["mhz"], c["db"], c["over_db"]) for c in got["channels"]][:2] == [(769.90625, -21.5, 26.3), (770.18125, -40.1, 7.7)]
    assert got["channels"][2]["over_db"] < 1 and len(got["channels"]) == 4
    assert reception.parse_sweep("", MRRS.covered([0])) is None and reception.parse_sweep("Error: no device\n", [1.0]) is None
    said = []

    class Done:
        stdout = text.encode()

    def run(argv, **kw):
        said.append(argv)
        return Done()
    out = reception.sweep("/x/rtl_power", 3, MRRS.covered([0]), 770312500.0, 2400000.0, run=run)
    assert out == got and said[0][:5] == ["/x/rtl_power", "-d", "3", "-f", "769112500:771512500:6250"] and "-1" in said[0]

    def gone(argv, **kw):
        raise FileNotFoundError(argv[0])
    assert reception.sweep("/x/rtl_power", 3, MRRS.covered([0]), 770312500.0, 2400000.0, run=gone) is None
    assert reception.rtl_power_path("/nowhere/rtl_fm") == ""


# -- the verdict ----------------------------------------------------------------------------------------------------------------


def side(rates, secs=None, grants=(), calls=(), lines=None, job="MRRS 770"):
    clock = Clock()
    p = reception.Probe(MRRS, job, "spare", clock)
    feed(p, clock, 769.90625, rates, lock_lines() if lines is None else lines)
    for tg in grants:
        p.feed_line(f"SIZEUP-UNIT mrrs 4100001 call {tg}")
    for c in calls:
        p.call(c)
    clock.t = p.started + (secs if secs is not None else len(rates) * 3.0 or 900.0)
    return p.result()


@pytest.mark.parametrize("decoded, grade", [(100, "good"), (90, "good"), (89, "marginal"), (40, "marginal"),
                                            (5, "marginal"), (4, "none"), (0, "none")])
def test_the_verdict_follows_the_share_of_the_time_the_control_channel_decodes(decoded, grade):
    r = side([40] * decoded + [3] * (100 - decoded))
    assert r["cc"]["decoded_pct"] == float(decoded) and r["verdict"]["grade"] == grade
    assert (reception.GOOD_PCT, reception.NONE_PCT, reception.CC_OK, reception.VOICE_ERR_BAD) == (90.0, 5.0, 10.0, 5.0)


def test_the_hint_says_what_to_try_in_plain_words():
    v = side([40] * 40 + [0] * 60)["verdict"]
    assert v["grade"] == "marginal" and v["hint"].startswith(
        "Control channel decodes 40% of the time: try a directional (yagi) antenna toward Harrison")
    assert "no LNA near the 700 MHz cell bands" in v["hint"]
    v = side([0] * 100, lines=[])["verdict"]
    assert v["grade"] == "none" and v["hint"].startswith("No control channel on the 770 side: try the other side, then a directional")
    v = side([], lines=[])["verdict"]
    assert v["grade"] == "none" and "didn't run on this dongle" in v["hint"]
    # a control channel that isn't the listed site's: never "good"
    v = side([40] * 100, lines=lock_lines(nac="3C1", site=12))["verdict"]
    assert v["grade"] == "marginal" and "Not the listed site: NAC 3C1 (listed 051), SITE 12 (listed 81)" in v["hint"]
    # voice that decodes with errors: marginal whatever the control channel
    v = side([40] * 100, grants=[PD], calls=[a_call(PD, secs=4.0, errors=40)])["verdict"]
    assert v["grade"] == "marginal" and "Voice decodes with 10 errors/s" in v["hint"]
    v = side([40] * 100, grants=[PD], calls=[a_call(PD, secs=4.0, errors=4)])["verdict"]
    assert v == {"grade": "good", "hint": "", "short": False}
    # granted on RF, its voice channel on the side this dongle doesn't cover
    v = side([40] * 100, grants=[PD, PD, EMS])["verdict"]
    assert v["grade"] == "good" and v["hint"].startswith("9211 / 9570: 3 grants, no voice on the 770 side")
    # nothing granted at all: run longer; after an hour, the LTE caveat
    assert side([40] * 100)["verdict"]["hint"] == "No 9211 / 9570 grant yet: run 1 h or more."
    v = side([40] * 100, secs=3 * 3600.0)["verdict"]
    assert "No 9211 / 9570 grant in 3.0 h. Some traffic is carried over LTE and never on RF. Compare with a scanner" in v["hint"]
    assert "trunk-recorder ran 2.8% of the test" in v["hint"]              # 100 windows in 3 h: it wasn't running
    v = side([40] * 20, secs=60.0, job="MRRS 773")["verdict"]
    assert v["short"] and v["hint"].endswith("Short test (60 s).")


def test_a_tests_verdict_is_its_best_sides():
    a, b = side([0] * 100, lines=[]), side([40] * 95 + [0] * 5, grants=[PD], calls=[a_call(PD)], job="MRRS 773")
    assert reception.overall([a, b])["grade"] == "good" and reception.overall([a, b])["job"] == "MRRS 773"
    assert reception.overall([a])["grade"] == "none" and reception.overall([]) is None
    s = reception.summary(b)
    assert (s["job"], s["grade"], s["cc_mhz"], s["decoded_pct"], s["site"]) == ("MRRS 773", "good", 769.90625, 95.0, True)
    assert [(w["tg"], w["grants"], w["calls"]) for w in s["watch"]] == [(PD, 1, 1), (EMS, 0, 0)]


# -- the test itself: an app with the fire jobs, an airband channel and a spare dongle ---------------------------------------


def make_app(tmp_path, monkeypatch, plugged=None):
    d = system_dir(tmp_path)
    plugged = list(DONGLES) if plugged is None else plugged
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
                channels=(ChannelCfg("FG1", 453.0375, "traffic", ""), ChannelCfg("South 2", 46.26, "paging", ""),
                          ChannelCfg("HPN Tower", 118.575, "airband", "", gain_db=36.4, squelch_db=3.5)),
                sdr_auto=True, sdr_auto_assign=False,
                p25_dir=str(tmp_path / "captures" / "p25"), p25_talkgroups={307: "Fire/EMS 15", 301: "60 Control"},
                p25_tg_info={307: {"name": "Fire/EMS 15", "group": "Fire/EMS", "encrypted": False},
                             301: {"name": "60 Control", "group": "Fire/EMS", "encrypted": False}},
                p25_recorder=str(tmp_path / "no-trunk-recorder") + " --config=config.json",
                p25_recorder_dir=str(d.parent.parent), p25_systems=(system,))
    app = appmod.App(c)
    said: list[str] = []

    class Spy(FakeWhisper):
        def transcribe(self, audio, prompt=""):
            said.append(prompt)
            return "words", 0.9
    app.stt.client = Spy({})
    app.said, app.logs, app.plugged = said, logs, plugged          # type: ignore[attr-defined]
    return app


def running(app, clock: Clock | None = None):
    """Started, each dongle on its job as a person set it; the reception tests switch in the caller's thread, on a
    clock of the test's, with the system's trunk-recorder said to be up."""
    app.start()
    for sd, job in JOBS.items():
        assert app.set_mode(job, sd) is None
    app.reception.inline = True
    if clock is not None:
        app.reception.clock = clock
    rt = app.systems["mrrs"]
    rt.recorder.status = lambda: {"state": "ok", "detail": "", "restarts": 0}
    return rt


@pytest.fixture
def lab(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.stt.start()
    yield app
    app.stop()


def start(app, sdr="spare", **o):
    return app.reception_post({"action": "start", "system": "mrrs", "job": "MRRS 770", "sdr": sdr, "minutes": 15, **o})


def saved_modes(app) -> dict:
    return json.loads(app._mode_file.read_text())


def test_a_dongle_another_app_holds_is_refused_and_nothing_is_switched(lab):
    app = lab
    running(app)
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "spare.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getppid(), "mode": "HPN Tower",
                                                       "since": time.time()}))
    err = start(app)
    assert err == "SDR spare is in use by shortfinal (HPN Tower): switch it Off there to hand it over"
    assert app.reception.active is None and app.assign.get("spare", "Off") == "Off" and not app.reception.marker.exists()
    assert json.loads((claims.DIR / "spare.json").read_text())["app"] == "shortfinal"      # never touched
    row = next(d for d in app.state()["reception"]["dongles"] if d["serial"] == "spare")
    assert row["refuse"] == "in use by shortfinal" and row["held_by"] == {"app": "shortfinal", "mode": "HPN Tower"}
    # ... and one that goes to another app between the list and the click
    (claims.DIR / "spare.json").unlink()
    assert next(d for d in app.reception.dongles() if d["serial"] == "spare")["refuse"] == ""
    (claims.DIR / "air.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getppid(), "mode": "x", "since": 1}))
    assert "in use by shortfinal" in start(app, "air", pause="HPN Tower")


def test_a_dongle_that_runs_a_job_is_lent_only_when_that_job_is_named(lab):
    app = lab
    rt = running(app)
    err = start(app, "air")
    assert err.startswith("SDR air runs HPN Tower: it is paused for the test and comes back after")
    assert start(app, "air", pause=True) == err and start(app, "air", pause="FG1") == err    # the job as it is now
    assert app.assign["air"] == "HPN Tower" and app.reception.active is None and rt.probe is None
    assert start(app, "air", pause="HPN Tower") is None
    st = app.state()["reception"]["active"]
    assert (st["was"], st["sdr"], st["plan"], st["step"]) == ("HPN Tower", "air", ["MRRS 770"], "running")
    assert app.assign["air"] == "MRRS 770" and rt.recorder.source_on(0) == "air"
    assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "MRRS 770"
    assert start(app, "spare") == "a reception test is already running: stop it first"
    assert any("HPN Tower paused" in m for m in app.logs)


def test_the_fire_trunk_paging_and_fireground_dongles_are_protected(lab):
    app = lab
    running(app)
    rows = {d["serial"]: d for d in app.state()["reception"]["dongles"]}
    assert {sd: bool(d["protected"]) for sd, d in rows.items()} == {
        "v4": True, "trunk": True, "page": True, "air": False, "spare": False}
    assert rows["trunk"]["protected"] == "county fire dispatch (the P25 trunk)" and rows["page"]["protected"] == "fire paging"
    for sd, job in (("trunk", "Fire/EMS 15"), ("page", "South 2"), ("v4", "FG1")):
        assert "Confirm with \"pause\"" in start(app, sd)
        err = start(app, sd, pause=job)                       # naming the job isn't enough for a fire job
        assert "NOT heard while the test runs" in err and "\"protected\": true" in err
        assert start(app, sd, pause=job, protected="yes") == err
        assert app.assign[sd] == job and app.reception.active is None
    assert app.recorder.on() == "trunk"                       # the county's trunk-recorder: never touched
    # explicitly confirmed: lent, and given back
    assert start(app, "page", pause="South 2", protected=True) is None
    assert app.assign["page"] == "MRRS 770"
    assert app.reception_post({"action": "stop"}) is None
    assert app.assign["page"] == "South 2" and app.set_by["page"] == "person"
    assert app.reception.history[0]["restored"] == "South 2 restored"


def test_other_refusals(lab, monkeypatch):
    app = lab
    running(app)
    assert "no SDR 'nope'" in start(app, "nope")
    assert "jobs: one or more of MRRS 770, MRRS 773" in start(app, job="FG1")
    assert "minutes: one of 15, 60, 240, 0" in start(app, minutes=7)
    assert start(app, jobs=["MRRS 770", "MRRS 773"], minutes=0) == "until stopped: one side at a time"
    assert "keep_min: 0 to 60" in start(app, keep_min=600)
    assert "no such system" in app.reception_post({"action": "start", "system": "nope", "job": "MRRS 770", "sdr": "spare"})
    assert app.reception_post({"action": "stop"}) == "no reception test is running"
    assert app.reception_post({"action": "wipe"}) == "action: start, stop or note"
    app.plugged.remove(DONGLES[4])
    appmod.sdr_lookup.devices = lambda *a, **k: list(app.plugged)
    assert "no SDR 'spare'" in start(app)                     # unplugged
    app.plugged.append(DONGLES[4])
    # the system already on a dongle for good: nothing to test, and its traffic is never diverted
    assert app.set_mode("MRRS 773", "spare") is None
    assert start(app, "air", pause="HPN Tower").startswith("MRRS already runs on a dongle (MRRS 773)")
    assert next(d for d in app.reception.dongles() if d["serial"] == "spare")["refuse"] == "runs MRRS 773"


def test_the_paused_job_comes_back_when_the_time_is_up(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower", note="  whip on the roof  ") is None
    # while it runs the dongle is saved on its own job: a restart of any release starts HPN Tower on it
    assert app.assign["air"] == "MRRS 770" and saved_modes(app)["air"] == "HPN Tower"
    assert saved_modes(app)["_set_by"]["air"] == "person"
    assert json.loads(app.reception.marker.read_text())["was"] == "HPN Tower"
    feed(rt, clock, 769.90625, [40] * 100, lock_lines())
    rt.recorder.on_line("SIZEUP-UNIT mrrs 4100001 call 9211")
    app.reception.tick()
    live = app.state()["reception"]["active"]
    assert live["running"]["cc"]["decoded_pct"] == 100.0 and live["running"]["talkgroups"][0]["grants"] == 1
    assert live["phase_until"] - live["phase_started"] == 900 and live["note"] == "whip on the roof"
    clock.t = live["phase_until"] + 1
    app.reception.tick()
    assert app.reception.active is None and rt.probe is None and not app.reception.marker.exists()
    assert app.assign["air"] == "HPN Tower" and app.set_by["air"] == "person" and saved_modes(app)["air"] == "HPN Tower"
    assert rt.recorder.source_on(0) is None and json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower"
    h = app.state()["reception"]["history"][0]
    assert (h["status"], h["restored"], h["was"], h["note"], h["verdict"]["grade"]) == (
        "done", "HPN Tower restored", "HPN Tower", "whip on the roof", "good")
    assert h["halves"][0]["cc_mhz"] == 769.90625 and h["halves"][0]["site"] is True
    assert json.loads(app.reception.history_file.read_text())[0]["halves"][0]["cc"]["windows"] == 100   # the file: all of it
    # the antenna note can be written after, to compare antennas and positions
    assert app.reception_post({"action": "note", "id": h["id"], "note": "yagi, east window"}) is None
    assert json.loads(app.reception.history_file.read_text())[0]["note"] == "yagi, east window"
    assert app.reception_post({"action": "note", "id": "nope", "note": "x"}) == "no such test"


def test_a_spare_dongle_goes_back_to_off_and_stays_out_of_auto_assign(lab):
    app, clock = lab, Clock()
    running(app, clock)
    assert start(app) is None and app.assign["spare"] == "MRRS 770" and saved_modes(app)["spare"] == "Off"
    assert app.reception_post({"action": "stop"}) is None
    assert app.assign["spare"] == "Off" and "spare" not in app.set_by and not (claims.DIR / "spare.json").exists()
    h = app.reception.history[0]
    assert (h["status"], h["why"], h["restored"]) == ("stopped", "stopped by a person", "Off again")


def test_the_paused_job_comes_back_when_the_test_fails(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower") is None
    rt.recorder.status = lambda: {"state": "absent", "detail": "trunk-recorder not installed (x)", "restarts": 0}
    app.reception.tick()
    assert app.reception.active is not None                    # a start takes a moment
    clock.t += reception.START_GRACE_S + 1
    app.reception.tick()
    h = app.reception.history[0]
    assert (h["status"], h["restored"]) == ("failed", "HPN Tower restored") and "trunk-recorder absent" in h["why"]
    assert app.assign["air"] == "HPN Tower" and app.reception.active is None and rt.probe is None
    assert h["verdict"]["grade"] == "none"
    # the dongle can't take the test's job at all (another app took it at that moment): the job never left
    rt.recorder.status = lambda: {"state": "ok", "detail": "", "restarts": 0}
    real = app._start_mode
    app._start_mode = lambda mode, serial: False if mode == "MRRS 770" else real(mode, serial)
    assert start(app, "air", pause="HPN Tower") is None
    h = app.reception.history[0]
    assert h["status"] == "failed" and "taken by another app" in h["why"] and h["restored"] == "HPN Tower restored"
    assert app.assign["air"] == "HPN Tower" and app.reception.active is None
    # an error inside a tick: the same
    app._start_mode = real
    assert start(app, "air", pause="HPN Tower") is None
    rt.recorder.status = lambda: 1 / 0
    app.reception._guarded(app.reception.tick)
    assert app.reception.history[0]["status"] == "failed" and "ZeroDivisionError" in app.reception.history[0]["why"]
    assert app.assign["air"] == "HPN Tower"


def test_a_dongle_unplugged_mid_test_keeps_its_job_written_down(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower") is None
    app.plugged.remove(DONGLES[3])
    rt.recorder.status = lambda: {"state": "absent", "detail": "SDR 'air' not plugged in", "restarts": 0}
    app.reception.tick()
    clock.t += reception.START_GRACE_S + 1
    app.reception.tick()
    h = app.reception.history[0]
    # its job is on it again, as for any unplugged dongle: it runs when the dongle is back
    assert h["status"] == "failed" and "not plugged in" in h["why"] and h["restored"] == "HPN Tower restored"
    assert app.assign["air"] == "HPN Tower" and saved_modes(app)["air"] == "HPN Tower" and rt.recorder.source_on(0) is None


def test_a_dongle_that_cant_be_opened_at_the_end_still_has_its_job_set_back(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower") is None
    app._ambiguity = lambda serial, devs=None: "Two dongles share serial air" if serial == "air" else ""
    assert app.reception_post({"action": "stop"}) is None
    h = app.reception.history[0]
    assert h["restored"] == "HPN Tower set for it, not started: SDR air: Two dongles share serial air"
    assert app.assign["air"] == "HPN Tower" and saved_modes(app)["air"] == "HPN Tower"
    assert rt.recorder.source_on(0) is None and not (claims.DIR / "air.json").exists()     # the test's job is off it


def test_a_person_who_gives_the_dongle_another_job_ends_the_test_and_keeps_their_choice(lab):
    app, clock = lab, Clock()
    running(app, clock)
    assert start(app) is None
    assert app.set_mode("MRRS 773", "spare") is None
    app.reception.tick()
    h = app.reception.history[0]
    assert h["status"] == "failed" and h["restored"] == "left on MRRS 773 (changed during the test)"
    assert app.assign["spare"] == "MRRS 773" and saved_modes(app)["spare"] == "MRRS 773"


def test_a_restart_mid_test_gives_the_dongle_its_job_back(tmp_path, monkeypatch):
    app, clock = make_app(tmp_path, monkeypatch), Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower", keep_min=10) is None
    feed(rt, clock, 769.90625, [40] * 30 + [0] * 30, lock_lines())
    app._call_system(rt, a_call(PD, at=clock.t))
    clock.t += reception.SAVE_S
    app.reception.tick()                                       # its numbers so far are written down
    sample = app.reception.samples_dir / f"{app.reception.samples[0]['id']}.wav"
    assert sample.is_file() and app.assign["air"] == "MRRS 770"
    app.stop()                                                 # the station restarts (an update, a crash, the power)
    assert app.reception.marker.is_file() and not sample.exists()

    again = make_app(tmp_path, monkeypatch)
    assert again.assign["air"] == "HPN Tower"                  # as saved: its own job, whatever release starts
    again.start()
    try:
        assert again.assign["air"] == "HPN Tower" and again.set_by["air"] == "person"
        assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower"
        assert again.systems["mrrs"].recorder.source_on(0) is None and again.systems["mrrs"].probe is None
        assert not again.reception.marker.exists() and again.reception.active is None
        h = again.state()["reception"]["history"][0]
        assert (h["status"], h["why"]) == ("interrupted", "sizeup restarted during the test")
        assert h["restored"] == "HPN Tower set back at the restart"
        assert h["halves"][0]["decoded_pct"] == 50.0 and h["halves"][0]["calls"] == 1 and h["verdict"]["grade"] == "marginal"
        assert not again.reception.samples_dir.exists() and again.state()["reception"]["samples"] == []
        assert any("interrupted by a restart" in m for m in again.logs)
    finally:
        again.stop()


def test_a_restart_finds_the_test_job_saved_by_an_older_file_and_still_sets_the_job_back(tmp_path, monkeypatch):
    """sdr_modes.json naming the test's job (written by hand here: a crash between the switch and the save), and the
    system's trunk-recorder still running under its own service: it is told to stop before the dongles are started."""
    app = make_app(tmp_path, monkeypatch)
    running(app)
    assert start(app, "air", pause="HPN Tower") is None
    app.stop()
    modes = saved_modes(app)
    modes["air"] = "MRRS 770"
    app._mode_file.write_text(json.dumps(modes))
    again = make_app(tmp_path, monkeypatch)
    assert again.assign["air"] == "MRRS 770"
    rec, stopped = again.systems["mrrs"].recorder, []
    rec.unit = True
    rec.stop = lambda why="": stopped.append((why, dict(again.assign)))
    again.reception.recover()
    assert again.assign["air"] == "HPN Tower" and saved_modes(again)["air"] == "HPN Tower"
    assert stopped and stopped[0][0] == "the reception test was ended by a restart" and stopped[0][1]["air"] == "MRRS 770"
    assert again.reception.history[0]["status"] == "interrupted" and not again.reception.marker.exists()
    again.reception.recover()                                  # nothing left to do
    assert len(again.reception.history) == 1


def test_each_side_in_sequence_on_the_one_dongle(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower", jobs=["MRRS 770", "MRRS 773"], job=None) is None
    feed(rt, clock, 769.90625, [0] * 50, [tr("Started with Control Channel: 769.906250 MHz")])
    clock.t = app.reception.active["phase_until"] + 1
    app.reception.tick()
    t = app.reception.active
    assert t is not None and t["phase"] == 1 and app.assign["air"] == "MRRS 773" and saved_modes(app)["air"] == "HPN Tower"
    assert rt.recorder.source_on(0) is None and rt.recorder.source_on(1) == "air" and rt.probe.job == "MRRS 773"
    feed(rt, clock, 773.03125, [39] * 60, lock_lines(773.03125))
    live = app.state()["reception"]["active"]
    assert [h["job"] for h in live["halves"]] == ["MRRS 770"] and live["running"]["job"] == "MRRS 773"
    clock.t = t["phase_until"] + 1
    app.reception.tick()
    h = app.reception.history[0]
    assert h["status"] == "done" and [(x["job"], x["verdict"]["grade"]) for x in h["halves"]] == [
        ("MRRS 770", "none"), ("MRRS 773", "good")]
    assert h["verdict"]["job"] == "MRRS 773" and h["halves"][1]["cc"]["mhz"] == 773.03125
    assert h["halves"][1]["covers"] == [772.48125, 773.03125, 773.68125]
    assert app.assign["air"] == "HPN Tower" and h["restored"] == "HPN Tower restored"


def test_until_stopped_ends_by_itself_after_a_day(lab):
    app, clock = lab, Clock()
    running(app, clock)
    assert start(app, minutes=0) is None
    t = app.reception.active
    assert t["phase_until"] - t["phase_started"] == reception.UNTIL_STOPPED_MAX_S
    clock.t = t["phase_until"] + 1
    app.reception.tick()
    assert app.reception.active is None and app.assign["spare"] == "Off" and app.reception.history[0]["status"] == "done"


def test_the_level_is_swept_before_trunk_recorder_takes_the_dongle(lab, tmp_path):
    app, clock = lab, Clock()
    running(app, clock)
    power = tmp_path / "rtl_power"
    power.write_text("#!/bin/sh\nexit 1\n")
    app.cfg = replace(app.cfg, rtl_fm=str(tmp_path / "rtl_fm"))
    asked = []

    def fake_sweep(binary, index, listed, center, rate):
        asked.append((binary, index, [round(f / 1e6, 5) for f in listed], center,
                      json.loads((claims.DIR / "air.json").read_text())["mode"], app.assign["air"]))
        return {"floor_db": -48.0, "channels": [{"mhz": 769.90625, "db": -30.0, "over_db": 18.0}]}
    app.reception.sweep_fn = fake_sweep
    assert start(app, "air", pause="HPN Tower") is None
    # the airband job was off the dongle and the dongle claimed for the test while rtl_power had it
    assert asked == [(str(power), 3, [769.90625, 770.18125, 770.46875, 770.71875], 770312500.0, "MRRS test", "Off")]
    rt = app.systems["mrrs"]
    feed(rt, clock, 769.90625, [40] * 10, lock_lines())
    sig = app.state()["reception"]["active"]["running"]["signal"]
    assert sig["cc_over_db"] == 18.0 and sig["sweep"]["floor_db"] == -48.0
    app.reception.sweep_fn = lambda *a: 1 / 0                  # a sweep that fails: the test goes on without it
    assert app.reception_post({"action": "stop"}) is None and start(app, "air", pause="HPN Tower") is None
    assert app.assign["air"] == "MRRS 770" and app.reception.probe.sweep is None


# -- nothing of a test is kept as traffic ---------------------------------------------------------------------------------------


def test_a_tests_calls_are_counted_never_transcribed_or_logged_and_the_sample_is_deleted(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    d = Path(rt.system.capture_dir) / "mrrs" / "2026" / "11" / "10"
    d.mkdir(parents=True)
    name = "9211-1790954731.970_770181250.1-call_5"
    tr_call(d, name, [(4100002, 1.5, 0.3, ""), (4100003, 1.0, 0.0, "")], tg=PD)
    meta = json.loads((d / f"{name}.json").read_text())
    meta.update(short_name="mrrs", talkgroup_tag="PD Dispatch", freq=770181250, signal=-40, noise=-60)
    (d / f"{name}.json").write_text(json.dumps(meta))
    assert start(app, keep_min=10) is None
    rt.ingest.scan()
    for c in rt.ingest.scan():                                 # the call as its ingest hands it over
        rt.ingest.on_call(c)
    for line in ("SIZEUP-UNIT mrrs 4100009 call 9213", "SIZEUP-UNIT mrrs 4100004 call 31999", "SIZEUP-UNIT mrrs 4100002 call 9211"):
        rt.recorder.on_line(line)
    app._call_system(rt, a_call(EMS, at=clock.t, secs=90.0))
    time.sleep(0.3)
    assert not app.said                                        # no speech-to-text
    assert not app.cad.store.q("SELECT * FROM radio_traffic") and not app.store.loose      # no row, no transmission
    assert not list(app.clips_dir.glob("*.wav"))               # no clip
    assert not (d / f"{name}.wav").exists() and not (d / f"{name}.json").exists()          # trunk-recorder's own: gone
    assert rt.activity.state() == {"unknown": [], "encrypted": []}                         # no radio ID kept as activity
    assert not rt.live.grants("mrrs", PD, 0, 2e10)
    clock.t += 61
    rt.activity.flush(everything=True)
    assert not app.cad.comms.activity_rows(0)
    st = app.state()["reception"]
    run = st["active"]["running"]
    assert (run["voice"]["calls"], run["encrypted_grants"], run["unknown_grants"]) == (2, 1, 1)
    assert run["signal"]["voice_snr_db"] == 20.0 and run["talkgroups"][0]["err_per_s"] == 0.4      # 0 + 1 errors in 2.5 s
    assert [(s["tg"], s["name"], s["seconds"]) for s in st["samples"]] == [(EMS, "PC-Rye EMS", 90.0), (PD, "PD Dispatch", 2.5)]
    assert "4100002" not in json.dumps(st)
    sid = st["samples"][0]["id"]
    wav = app.reception_sample(sid)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels()) == (8000, 1) and w.getnframes() == 60 * 8000    # its first minute
    import base64
    assert base64.b64decode(app.reception_sample_b64(sid)) == wav and app.reception_sample_b64("0" * 12) == ""
    assert app.reception_sample("../../etc/passwd") is None and app.reception_sample("nope") is None
    files = sorted(app.reception.samples_dir.glob("*.wav"))
    assert len(files) == 2
    # the test ends: the samples stay their ten minutes, then they are deleted
    assert app.reception_post({"action": "stop"}) is None
    clock.t += 9 * 60 - 62
    app.reception.tick()
    assert len(app.state()["reception"]["samples"]) == 2
    clock.t += 2 * 60
    app.reception.tick()
    assert app.state()["reception"]["samples"] == [] and not list(app.reception.samples_dir.glob("*.wav"))
    assert app.reception_sample(sid) is None
    # after the test the system's calls are its own again: by tier, as before
    app._call_system(rt, a_call(PD, name="after"))
    assert rt.probe is None and app.tg.state() and any(r["source"] == "p25:mrrs:9211" and r["count"] for r in app.state()["talkgroups"])


def test_keep_min_zero_keeps_no_audio(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, keep_min=0) is None
    app._call_system(rt, a_call(PD, at=clock.t))
    assert app.reception.probe.calls == 1 and app.reception.samples == [] and not app.reception.samples_dir.exists()


# -- the pages: the station's own, and the hub's view of it ----------------------------------------------------------------------


def test_the_state_says_what_can_be_tested_and_on_which_dongle(lab):
    app = lab
    running(app)
    st = app.state()["reception"]
    assert st["systems"] == [{"name": "mrrs", "label": "MRRS", "watch": [PD, EMS],
                              "jobs": [{"job": "MRRS 770", "mhz": 770.3125}, {"job": "MRRS 773", "mhz": 773.08125}],
                              "caveat": "Some traffic is carried over LTE and never on RF."}]
    assert st["durations"] == [15, 60, 240, 0] and st["keep_min"] == 10.0 and st["active"] is None and st["history"] == []
    assert [(d["serial"], d["job"], d["refuse"]) for d in st["dongles"]] == [
        ("v4", "FG1", ""), ("trunk", "Fire/EMS 15", ""), ("page", "South 2", ""), ("air", "HPN Tower", ""), ("spare", "Off", "")]


def test_the_station_reports_it_and_the_hub_may_ask_for_it(lab):
    from sizeup.cad.api import ADMIN_READS, DOES, WRITES
    from sizeup.cloud import station
    app = lab
    running(app)
    assert {"reception_post", "reception_sample_b64"} <= set(station.CALLS)

    class Link:                                                # StationLink._call, with nothing else of the link
        _status_now, _live_wake = False, type("W", (), {"set": lambda self: None})()
    Link.app = app
    out = station.StationLink._call(Link(), {"fn": "reception_post", "args": [{"action": "start", "job": "MRRS 770", "sdr": "spare"}]})
    assert out == {"ok": True, "result": None} and app.assign["spare"] == "MRRS 770"
    out = station.StationLink._call(Link(), {"fn": "reception_post", "args": [{"action": "start", "job": "MRRS 770", "sdr": "air"}]})
    assert out["result"] == "a reception test is already running: stop it first"
    # the POST is an admin's (no rule of its own: admin), the sample an admin's read, and both are in the activity
    assert not any(rx.fullmatch("/api/reception") for rx, _need in WRITES) and "/api/reception/sample" in ADMIN_READS
    say = next(fn for rx, fn in DOES if rx.fullmatch("/api/reception"))
    assert say(None, {"action": "start", "job": "MRRS 770", "sdr": "air", "pause": "HPN Tower"}, {}, None)[0] == (
        "Reception test started: MRRS 770 on SDR air, pausing HPN Tower")
    assert say(None, {"action": "stop"}, {}, None)[0] == "Reception test stopped"


def test_the_web_server_starts_stops_and_serves_a_sample(lab):
    import urllib.error
    import urllib.request
    app, clock = lab, Clock()
    rt = running(app, clock)
    import threading
    server = appmod.make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/reception", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        code, out = post({"action": "start", "job": "MRRS 770", "sdr": "air", "minutes": 15})
        assert code == 400 and "runs HPN Tower" in out["error"]
        assert post({"action": "start", "job": "MRRS 770", "sdr": "air", "minutes": 15, "pause": "HPN Tower"}) == (200, {"ok": True})
        app._call_system(rt, a_call(PD, at=clock.t))
        end, st = time.time() + 10, None
        while time.time() < end and not (st and st["active"] and st["samples"]):     # (the page's state is pushed each second)
            server.poke()
            time.sleep(0.2)
            with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
                st = json.loads(r.read())["reception"]
        assert st["active"]["was"] == "HPN Tower" and len(st["samples"]) == 1
        with urllib.request.urlopen(base + f"/api/reception/sample?id={st['samples'][0]['id']}", timeout=5) as r:
            assert r.headers["Content-Type"] == "audio/wav" and r.headers["Cache-Control"] == "no-store" and r.read()[:4] == b"RIFF"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/reception/sample?id=000000000000", timeout=5)
        assert e.value.code == 404
        assert post({"action": "stop"}) == (200, {"ok": True}) and app.assign["air"] == "HPN Tower"
    finally:
        server.httpd.shutdown()
