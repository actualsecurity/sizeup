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
from sizeup.radio import trunkrecd
from sizeup.radio.recorder import SystemUnitRecorder
from tests.test_health import access_hub, call, jwt, keys, jwks, people, req, station_status  # noqa: F401  (fixtures)
from tests.test_p25 import tr_call
from tests.test_p25_systems import MRRS, finder, service, system_dir
from tests.test_roles import COREY, DEE, JULIAN, VAL
from tests.test_trunk_recorder_unit import SERVICE_PID

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
    r = p.result()                                                  # 30 s in: 10 windows, as many as there should be
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
    assert (tgs[EMS]["grants"], tgs[EMS]["calls"], tgs[EMS]["calls_per_h"], tgs[PD]["calls_per_h"]) == (1, 0, 0.0, 120.0)
    assert (r["encrypted_grants"], r["unknown_grants"]) == (2, 1)      # counted: no radio ID, no talkgroup row
    assert "4100009" not in json.dumps(r) and "4100001" not in json.dumps(r)
    assert r["signal"]["voice_snr_db"] == 21.0 and r["signal"]["voice_db"] == -41.0 and r["signal"]["sweep"] is None
    assert r["voice"] == {"calls": 1, "audio_s": 4.0, "calls_per_h": 120.0, "err_per_s": 0.5}
    # the share is of the windows there should have been: an hour with only those ten logged is not 70% decoded
    clock.t += 3600 - 30
    late = p.result()["cc"]
    assert (late["windows"], late["expected"], late["decoded_pct"], late["logged_pct"]) == (10, 1200, 0.6, 0.8)


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
    said, seen = [], []

    class Proc:                                    # rtl_power: its output, or killed (a Stop) before it said anything
        def __init__(self, out=text.encode(), hang=False):
            self.out, self.hang, self.killed = out, hang, False

        def communicate(self, timeout=None):
            if self.hang and not self.killed:
                import subprocess
                raise subprocess.TimeoutExpired("rtl_power", timeout)
            return (b"" if self.killed else self.out), b""

        def kill(self):
            self.killed = True

    def popen(argv, **kw):
        said.append(argv)
        return Proc()
    out = reception.sweep("/x/rtl_power", 3, MRRS.covered([0]), 770312500.0, 2400000.0, popen=popen, started=seen.append)
    assert out == got and said[0][:5] == ["/x/rtl_power", "-d", "3", "-f", "769112500:771512500:6250"] and "-1" in said[0]
    assert len(seen) == 1                          # the process is handed out: Stop can kill it
    hung = Proc(hang=True)
    assert reception.sweep("/x/rtl_power", 3, MRRS.covered([0]), 770312500.0, 2400000.0, popen=lambda a, **k: hung) is None
    assert hung.killed

    def gone(argv, **kw):
        raise FileNotFoundError(argv[0])
    assert reception.sweep("/x/rtl_power", 3, MRRS.covered([0]), 770312500.0, 2400000.0, popen=gone) is None
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
    assert (reception.GOOD_PCT, reception.NONE_PCT, reception.CC_OK, reception.VOICE_ERR_BAD, reception.GOOD_RATE) == (
        90.0, 5.0, 10.0, 5.0, 30.0)


def test_the_hint_says_what_to_try_in_plain_words():
    v = side([40] * 40 + [0] * 60)["verdict"]
    assert v["grade"] == "marginal" and v["hint"].startswith(
        "Control channel decodes 40% of the time: try a directional (yagi) antenna toward Harrison")
    assert "no LNA near the 700 MHz cell bands" in v["hint"]
    v = side([0] * 100, lines=[])["verdict"]
    assert v["grade"] == "none" and v["hint"].startswith("No control channel on the 770 side: try the other side, then a directional")
    v = side([], lines=[])["verdict"]
    assert v["grade"] == "none" and "it didn't run on this dongle" in v["hint"]
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
    v = side([40] * 3600)["verdict"]
    assert v["grade"] == "good" and v["hint"] == ("No 9211 / 9570 grant in 3.0 h. Some traffic is carried over LTE and "
                                                  "never on RF. Compare with a scanner over the same time.")
    v = side([40] * 20, secs=60.0, job="MRRS 773")["verdict"]
    assert v["short"] and v["hint"].endswith("Short test (60 s).")


def test_good_needs_a_rate_floor_and_a_rate_that_was_not_logged_is_never_none():
    # decoded all the time, but weakly: not "good"
    v = side([20] * 100, grants=[PD], calls=[a_call(PD)])["verdict"]
    assert v["grade"] == "marginal" and v["hint"].startswith("Control channel decodes at 20/s (full strength is about 40): try a")
    assert side([30] * 100, grants=[PD], calls=[a_call(PD)])["verdict"]["grade"] == "good"
    # trunk-recorder logged no rate at all (a build that ignores "controlWarnRate": -1), yet the site decoded and
    # grants came: the control channel was there. Graded from those, and said
    r = side([], secs=900.0, grants=[PD], calls=[a_call(PD)])
    assert r["cc"]["windows"] == 0 and r["verdict"]["grade"] == "good"
    assert r["verdict"]["hint"] == "Decode rate not logged: graded from the site and the grants seen."
    assert side([], secs=900.0)["verdict"]["grade"] == "marginal"                       # the site alone
    assert side([], secs=900.0, grants=[PD], lines=[])["verdict"]["grade"] == "marginal"  # grants alone
    # only the low windows logged (the default warn rate): five bad ones in 15 min say nothing of the other 295
    r = side([3] * 5, secs=900.0, grants=[PD])
    assert r["cc"]["decoded_pct"] == 0.0 and r["verdict"]["grade"] == "good"
    assert r["verdict"]["hint"].startswith("Decode rate not logged (1.7% of the time): graded from the site and the grants")
    v = side([3] * 5, secs=900.0, lines=[])["verdict"]
    assert v["grade"] == "none" and v["hint"].startswith("No control channel on the 770 side (no decode rate, no site, no grant)")


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
    app.ended = []                                                 # type: ignore[attr-defined]
    hook = app.reception.on_end
    app.reception.on_end = lambda t: (app.ended.append(dict(t)), hook(t))[1]
    return app


def running(app, clock: Clock | None = None):
    """Started, each dongle on its job as a person set it; the reception tests run in the caller's thread, on a
    clock of the test's, with the system's trunk-recorder said to be up."""
    app.start()
    for sd, job in JOBS.items():
        if app.assign.get(sd) != job:
            assert app.set_mode(job, sd) is None
    app.reception.inline = True
    if clock is not None:
        app.reception.clock = clock
    rt = app.systems["mrrs"]
    rt.recorder.status = lambda: {"state": "ok", "detail": "", "restarts": 0}
    return rt


def crash(app) -> None:
    """sizeup killed (a crash, kill -9, the power): nothing of the reception test is ended or cleaned up. (The rest of
    the app is torn down so the next one in this process has the dongles to itself.)"""
    app.reception._stop.set()
    app.reception.shutdown = lambda: None
    app.stop()


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


def other_app_claims(serial: str, mode: str = "AM 118.575") -> None:
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / f"{serial}.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getppid(), "mode": mode,
                                                           "since": time.time()}))


def test_a_dongle_another_app_holds_is_refused_and_nothing_is_switched(lab):
    app = lab
    running(app)
    other_app_claims("spare", "HPN Tower")
    err = start(app)
    assert err == "SDR spare is in use by shortfinal (HPN Tower): switch it Off there to hand it over"
    assert app.reception.active is None and app.assign.get("spare", "Off") == "Off" and not app.reception.marker.exists()
    assert json.loads((claims.DIR / "spare.json").read_text())["app"] == "shortfinal"      # never touched
    row = next(d for d in app.reception_state()["dongles"] if d["serial"] == "spare")
    assert row["refuse"] == "in use by shortfinal" and row["held_by"] == {"app": "shortfinal", "mode": "HPN Tower"}
    assert any("reception test refused" in m and "in use by shortfinal" in m for m in app.logs)     # refused: logged
    # ... and one that goes to another app between the list and the click
    (claims.DIR / "spare.json").unlink()
    assert next(d for d in app.reception.dongles() if d["serial"] == "spare")["refuse"] == ""
    other_app_claims("air", "x")
    assert "in use by shortfinal" in start(app, "air", pause="HPN Tower")


def test_a_dongle_that_runs_a_job_is_lent_only_when_that_job_is_named(lab):
    app = lab
    rt = running(app)
    err = start(app, "air")
    assert err.startswith("SDR air runs HPN Tower: it is paused for the test and comes back after")
    assert start(app, "air", pause=True) == err and start(app, "air", pause="FG1") == err    # the job as it is now
    assert app.assign["air"] == "HPN Tower" and app.reception.active is None and rt.probe is None
    assert start(app, "air", pause="HPN Tower") is None
    st = app.reception_state()["active"]
    assert (st["was"], st["sdr"], st["plan"], st["step"]) == ("HPN Tower", "air", ["MRRS 770"], "running")
    assert app.assign["air"] == "MRRS 770" and rt.recorder.source_on(0) == "air"
    assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "MRRS 770"
    assert start(app, "spare") == "a reception test is already running: stop it first"
    assert any("HPN Tower paused" in m for m in app.logs)
    # its row in Settings > Radios says so, for everyone
    row = next(x for x in app.state()["sdr"]["sdrs"] if x["serial"] == "air")
    assert row["lent"] == {"label": "MRRS", "job": "MRRS 770", "paused": "HPN Tower"} and row["mode"] == "MRRS 770"
    assert next(x for x in app.state()["sdr"]["sdrs"] if x["serial"] == "spare")["lent"] is None


def test_fire_dongles_are_never_lent(lab):
    """The county trunk, paging and a fireground: not lendable in this version, whatever the request confirms."""
    app = lab
    running(app)
    rows = {d["serial"]: d for d in app.reception_state()["dongles"]}
    assert {sd: d["refuse"] for sd, d in rows.items()} == {
        "v4": "protected: not lendable", "trunk": "protected: not lendable", "page": "protected: not lendable",
        "air": "", "spare": ""}
    assert rows["trunk"]["protected"] == "county fire dispatch (the P25 trunk)" and rows["page"]["protected"] == "fire paging"
    assert rows["v4"]["protected"] == "a fireground channel"
    for sd, job in (("trunk", "Fire/EMS 15"), ("page", "South 2"), ("v4", "FG1")):
        for extra in ({}, {"pause": job}, {"pause": job, "protected": True}, {"pause": job, "protected": True, "force": True}):
            err = start(app, sd, **extra)
            assert "protected: not lendable to a reception test" in err and job in err, (sd, extra)
        assert app.assign[sd] == job and app.reception.active is None and not app.reception.marker.exists()
        assert json.loads((claims.DIR / f"{sd}.json").read_text())["mode"] == job
    assert app.recorder.on() == "trunk"                       # the county's trunk-recorder: never touched
    assert sum("reception test refused" in m for m in app.logs) == 12


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
    # the station's trunk-recorder service has its own deadline for it (the side's end and a grace)
    assert rt.recorder.until == app.reception.active["phase_until"] + reception.UNTIL_GRACE_S
    feed(rt, clock, 769.90625, [40] * 100, lock_lines())
    rt.recorder.on_line("SIZEUP-UNIT mrrs 4100001 call 9211")
    app.reception.tick()
    live = app.reception_state()["active"]
    assert live["running"]["cc"]["decoded_pct"] == 100.0 and live["running"]["talkgroups"][0]["grants"] == 1
    assert live["phase_until"] - live["phase_started"] == 900 and live["note"] == "whip on the roof"
    assert app.reception.health() is None
    clock.t = live["phase_until"] + 1
    app.reception.tick()
    assert app.reception.active is None and rt.probe is None and not app.reception.marker.exists()
    assert app.assign["air"] == "HPN Tower" and app.set_by["air"] == "person" and saved_modes(app)["air"] == "HPN Tower"
    assert rt.recorder.source_on(0) is None and json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower"
    assert rt.recorder.until is None and app.reception.owed is None and app.reception.health() is None
    h = app.reception_state()["history"][0]
    assert (h["status"], h["restored"], h["restored_ok"], h["was"], h["note"]) == (
        "done", "HPN Tower restored", True, "HPN Tower", "whip on the roof")
    # (100 windows in the 15 min: under half logged, so graded from the site and the grant, and said)
    assert h["verdict"]["grade"] == "good" and "Decode rate not logged" in h["verdict"]["hint"]
    assert h["halves"][0]["cc_mhz"] == 769.90625 and h["halves"][0]["site"] is True
    assert json.loads(app.reception.history_file.read_text())[0]["halves"][0]["cc"]["windows"] == 100   # the file: all of it
    assert [t["status"] for t in app.ended] == ["done"]       # told once, for the activity log
    # the antenna note can be written after, to compare antennas and positions
    assert app.reception_post({"action": "note", "id": h["id"], "note": "yagi, east window"}) is None
    assert json.loads(app.reception.history_file.read_text())[0]["note"] == "yagi, east window"
    assert app.reception_post({"action": "note", "id": "nope", "note": "x"}) == "no such test"


def test_the_dongle_is_claimed_all_the_way_through_the_hand_off(lab, monkeypatch):
    """No moment in which another app could take it: sizeup's claim is renamed, never removed, from the job it ran to
    the test and back."""
    app = lab
    running(app)
    seen = []
    real_release, real_claim = claims.release, claims.claim
    monkeypatch.setattr(claims, "release", lambda serial: (seen.append(("release", serial)), real_release(serial))[1])
    monkeypatch.setattr(claims, "claim", lambda serial, mode: (seen.append((mode, serial)), real_claim(serial, mode))[1])
    assert start(app, "air", pause="HPN Tower") is None
    assert [x for x in seen if x[1] == "air"] == [("MRRS test", "air"), ("MRRS 770", "air")]
    del seen[:]
    assert app.reception_post({"action": "stop"}) is None
    # (claimed for the job it gets back straight after the test's trunk-recorder stops, before the pause; never released)
    assert [x for x in seen if x[1] == "air"] == [("HPN Tower", "air")] * 2 and (claims.DIR / "air.json").exists()
    del seen[:]
    assert start(app) is None and app.reception_post({"action": "stop"}) is None        # a spare: released only at the end
    assert [x for x in seen if x[1] == "spare"] == [("MRRS test", "spare"), ("MRRS 770", "spare"), ("release", "spare")]


def test_a_spare_dongle_goes_back_to_off(lab):
    app, clock = lab, Clock()
    running(app, clock)
    assert start(app) is None and app.assign["spare"] == "MRRS 770" and saved_modes(app)["spare"] == "Off"
    assert app.reception_post({"action": "stop"}, "admin@example.org") is None
    assert app.assign["spare"] == "Off" and "spare" not in app.set_by and not (claims.DIR / "spare.json").exists()
    h = app.reception.history[0]
    assert (h["status"], h["why"], h["restored"]) == ("stopped", "stopped by a person", "Off again")


def test_who_started_a_test_is_kept_by_the_cad_of_record_never_with_the_test(lab):
    """The test itself (its marker, the history file, the log, the state a station reports) names no person: only an
    id. The app that is the CAD of record keeps id -> person, and writes the end in that person's activity."""
    app, clock = lab, Clock()
    running(app, clock)
    who = "admin@example.org"
    assert app.reception_post({"action": "start", "job": "MRRS 770", "sdr": "air", "pause": "HPN Tower"}, who) is None
    tid = app.reception.active["id"]
    assert app._reception_who(tid) == who and who not in app.reception.marker.read_text()
    assert app.reception_post({"action": "stop"}, who) is None
    assert who not in app.reception.history_file.read_text() and who not in json.dumps(app.reception_state())
    assert not [m for m in app.logs if who in m and not m.startswith("CAD activity: ")]   # (the CAD's own activity log)
    acts = app.cad.store.q("SELECT email, kind, summary, ok FROM user_activity WHERE route = 'reception test'")
    assert [(a["email"], a["kind"], a["summary"], a["ok"]) for a in acts] == [
        (who, "action", "Reception test stopped (stopped by a person): MRRS 770 on SDR air · none · HPN Tower restored", 1)]
    # a refused start keeps nothing; one from the station's own page (nobody signed in) has no row to write
    assert "protected" in app.reception_post({"action": "start", "job": "MRRS 770", "sdr": "page", "pause": "South 2"}, who)
    assert list(json.loads(app.cad.store.meta_get("reception_by"))) == [tid]
    # a station is handed the hub's id for the test, and nothing else
    assert app.reception_post({"action": "start", "job": "MRRS 770", "sdr": "spare"}, "", "0123456789ab") is None
    assert app.reception.active["id"] == "0123456789ab" and app._reception_who("0123456789ab") == ""


def test_switching_the_lent_dongle_in_settings_ends_the_test_and_restores_its_job(lab):
    """Off, or anything else, picked for the lent dongle in Settings > Radios (or from the hub): the same as Stop.
    The paused job comes back; the dongle is never left Off."""
    app, clock = lab, Clock()
    rt = running(app, clock)
    for picked in ("Off", "FG1", "MRRS 773", "HPN Tower"):
        assert start(app, "air", pause="HPN Tower") is None and app.assign["air"] == "MRRS 770"
        # the answer says what happened instead of the switch (so no "Radio mode: X" in the activity log)
        assert app.set_mode(picked, "air") == ("reception test stopped: SDR air was lent to it; HPN Tower comes back on it "
                                               f"(not switched to {picked})")
        assert app.assign["air"] == "HPN Tower" and app.set_by["air"] == "person" and app.reception.active is None, picked
        assert rt.recorder.source_on(0) is None and rt.probe is None and app.assign["v4"] == "FG1"
        h = app.reception.history[0]
        assert (h["status"], h["why"], h["restored"]) == ("stopped", "SDR air was switched in Settings > Radios",
                                                          "HPN Tower restored")
        assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower"
    # the test's system can't be given another dongle meanwhile (its calls are the test's), nor the lent dongle to ADS-B
    assert start(app, "air", pause="HPN Tower") is None
    assert app.set_mode("MRRS 773", "spare") == "a reception test is running on MRRS: stop it first"
    assert app.set_mode("MRRS 770", "spare") == "a reception test is running on MRRS: stop it first"
    assert app.set_adsb_receiver("air") == "SDR air is lent to a reception test: stop it first"
    assert app.assign["air"] == "MRRS 770" and app.reception.active is not None
    # the paused job moved to another dongle by a person: at the end the lent one is Off, and nobody's choice
    assert app.set_mode("HPN Tower", "spare") is None
    assert app.reception_post({"action": "stop"}) is None
    assert app.assign["air"] == "Off" and "air" not in app.set_by and app.assign["spare"] == "HPN Tower"
    assert app.reception.history[0]["restored"] == "HPN Tower now on another dongle: Off"
    assert "air" not in saved_modes(app)["_set_by"] and saved_modes(app)["air"] == "Off"


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
    # the dongle can't take the test's job at all: its own job is started on it again
    rt.recorder.status = lambda: {"state": "ok", "detail": "", "restarts": 0}
    real = app._start_mode
    app._start_mode = lambda mode, serial: False if mode == "MRRS 770" else real(mode, serial)
    assert start(app, "air", pause="HPN Tower") is None
    h = app.reception.history[0]
    assert h["status"] == "failed" and "was just taken" in h["why"] and h["restored"] == "HPN Tower restored"
    assert app.assign["air"] == "HPN Tower" and app.reception.active is None
    assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower"
    # an error inside a tick: the same, and the thread's loop survives anything
    app._start_mode = real
    assert start(app, "air", pause="HPN Tower") is None
    rt.recorder.status = lambda: 1 / 0
    app.reception._guarded(app.reception.tick)
    assert app.reception.history[0]["status"] == "failed" and "ZeroDivisionError" in app.reception.history[0]["why"]
    assert app.assign["air"] == "HPN Tower"
    app.reception._finish = lambda *a, **k: 1 / 0
    app.reception.active = {"id": "x"}
    app.reception._guarded(lambda: 1 / 0)                      # even ending it failing never raises out of the thread
    app.reception.active = None
    assert any("not ended yet: ZeroDivisionError" in m for m in app.logs)


def test_a_restore_the_other_app_gets_in_the_way_of_is_owed_and_retried_until_done(lab):
    """The dongle taken by the other app in the moment between the test's trunk-recorder and the paused job: the job is
    owed, said in the health report (a push: it paused a job), tried again every few seconds, and comes back."""
    app, clock = lab, Clock()
    running(app, clock)
    assert start(app, "air", pause="HPN Tower") is None
    real = app._stop_mode

    def stop_then_taken(mode, why, serial, release=True):
        real(mode, why, serial, release)
        if mode == "MRRS 770":
            other_app_claims("air")
    app._stop_mode = stop_then_taken
    assert app.reception_post({"action": "stop"}) is None
    app._stop_mode = real
    h = app.reception_state()["history"][0]
    assert h["status"] == "stopped" and h["restored_ok"] is False
    assert h["restored"] == "HPN Tower NOT restored yet (in use by shortfinal (AM 118.575)): retrying"
    assert app.assign["air"] == "Off" and saved_modes(app)["air"] == "HPN Tower"       # a restart would start it there
    assert json.loads((claims.DIR / "air.json").read_text())["app"] == "shortfinal"    # theirs: never taken back by force
    alert = app.reception.health()
    assert alert["push"] is True and alert["job"] == "HPN Tower" and alert["sdr"] == "air"
    assert alert["text"].startswith("HPN Tower on SDR air was not restored after the reception test (in use by shortfinal")
    assert app.reception_state()["alert"] == alert["text"]
    row = next(x for x in app.state()["sdr"]["sdrs"] if x["serial"] == "air")
    assert row["lent"] == {"label": "MRRS", "job": "", "paused": "HPN Tower", "owed": True}
    assert next(d for d in app.reception.dongles() if d["serial"] == "air")["refuse"] == "in use by shortfinal"
    from sizeup.cloud.health import Metrics, station_metrics
    assert station_metrics(app, Metrics(app.clips_dir))["reception"]["job"] == "HPN Tower"
    for _ in range(3):                                         # still theirs: tried, not done
        clock.t += reception.OWED_RETRY_S + 1
        app.reception.tick()
    assert app.assign["air"] == "Off" and app.reception.owed["tries"] == 4 and app.reception.owed_file.is_file()
    # a click on the owed dongle while the other app holds it never cancels the debt: its own job ("restore now") is
    # tried and refused, another job is refused by the usual checks, and it is still owed
    err = app.set_mode("HPN Tower", "air")
    assert err == ("SDR air: HPN Tower not restored yet (in use by shortfinal (AM 118.575)); still owed to it, retrying")
    assert app.set_mode("FG1", "air") == "SDR air is in use by shortfinal (AM 118.575)"
    assert app.reception.owed["was"] == "HPN Tower" and app.reception.owed_file.is_file() and app.assign["air"] == "Off"
    assert app.assign["v4"] == "FG1" and saved_modes(app)["air"] == "HPN Tower" and app.reception.health()["push"] is True
    (claims.DIR / "air.json").unlink()                         # they let go
    clock.t += reception.OWED_RETRY_S + 1
    app.reception.tick()
    assert app.assign["air"] == "HPN Tower" and app.set_by["air"] == "person" and app.reception.owed is None
    assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower" and not app.reception.owed_file.exists()
    h = app.reception_state()["history"][0]
    assert h["restored"] == "HPN Tower restored (after 6 tries)" and h["restored_ok"] is True
    assert app.reception.health() is None and app.reception_state()["alert"] is None
    assert [(t["restored_ok"], t["restored"][:12]) for t in app.ended] == [(False, "HPN Tower NO"), (True, "HPN Tower re")]


def test_an_owed_job_is_dropped_when_a_person_picks_something_else_for_the_dongle(lab):
    app, clock = lab, Clock()
    running(app, clock)
    assert start(app, "air", pause="HPN Tower") is None
    real = app._start_mode
    app._start_mode = lambda mode, serial: False if mode == "HPN Tower" else real(mode, serial)
    assert app.reception_post({"action": "stop"}) is None
    assert app.reception.owed["was"] == "HPN Tower" and app.assign["air"] == "Off"
    assert next(d for d in app.reception.dongles() if d["serial"] == "air")["refuse"] == "HPN Tower not restored yet"
    assert start(app, "air") == "SDR air: HPN Tower not restored yet"
    assert "still owed to it" in app.set_mode("HPN Tower", "air")      # "restore now": tried, can't be yet
    app._start_mode = real
    assert app.set_mode("HPN Tower", "air") is None            # ... and now it can: restored at the click
    assert app.assign["air"] == "HPN Tower" and app.reception.owed is None
    assert app.reception.history[0]["restored"].startswith("HPN Tower restored (after")
    # owed again; a person's other choice that WORKS ends the debt (Off too)
    app._start_mode = lambda mode, serial: False if mode == "HPN Tower" else real(mode, serial)
    assert start(app, "air", pause="HPN Tower") is None and app.reception_post({"action": "stop"}) is None
    assert app.reception.owed["was"] == "HPN Tower"
    assert app.set_mode("Off", "air") is None                  # a person's choice: theirs stands
    assert app.reception.history[0]["restored"] == "left on Off (picked by a person)"
    assert app.reception.owed is None and not app.reception.owed_file.exists() and app.reception.health() is None
    app._start_mode = real
    clock.t += 60
    app.reception.tick()
    assert app.assign["air"] == "Off" and saved_modes(app)["air"] == "Off"


def test_a_test_that_outlives_its_end_is_said_in_the_health_report(lab):
    app, clock = lab, Clock()
    running(app, clock)
    assert start(app, "air", pause="HPN Tower") is None
    end = app.reception.active["phase_until"]
    clock.t = end + reception.OVERDUE_S - 1                    # (its thread stalled: nothing ticks)
    assert app.reception.health() is None
    clock.t = end + 10 * 60
    a = app.reception.health()
    assert a == {"job": "HPN Tower", "sdr": "air", "since": end, "push": True,
                 "text": "SDR air: HPN Tower is still paused, 10 min after its reception test should have ended"}
    app.reception.tick()                                       # it ticks again: over, restored, the alert gone
    assert app.assign["air"] == "HPN Tower" and app.reception.health() is None
    assert start(app) is None                                  # a spare left lent: said, never pushed
    clock.t = app.reception.active["phase_until"] + 10 * 60
    assert app.reception.health()["push"] is False and "still lent" in app.reception.health()["text"]


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


def test_sizeup_stopping_ends_the_test_and_restores_the_job_before_it_exits(tmp_path, monkeypatch):
    """A clean stop (an update, a restart, a rollback): the test ends there, so whatever release starts next, an older
    one included, finds the dongle saved and claimed for its own job, the system's trunk-recorder asked for nothing,
    and no test left to end."""
    app, clock = make_app(tmp_path, monkeypatch), Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower", keep_min=10) is None
    feed(rt, clock, 769.90625, [40] * 30 + [0] * 30, lock_lines())
    app._call_system(rt, a_call(PD, at=clock.t))
    sample = app.reception.samples_dir / f"{app.reception.samples[0]['id']}.wav"
    order = []
    real_switch, real_stop = app.reception._switch, app.receivers[0].stop
    app.reception._switch = lambda *a: (order.append("restore"), real_switch(*a))[1]
    assert sample.is_file() and app.assign["air"] == "MRRS 770"
    for r in app.receivers:
        r.stop = (lambda f: lambda *a, **k: (order.append("receivers stop"), f(*a, **k))[1])(r.stop)
    app.stop()
    assert order[0] == "restore" and real_stop                 # before anything else of sizeup's is torn down
    assert not app.reception.marker.exists() and not sample.exists() and app.reception.active is None
    assert saved_modes(app)["air"] == "HPN Tower" and saved_modes(app)["_set_by"]["air"] == "person"
    assert rt.recorder.source_on(0) is None and rt.recorder.until is None
    h = json.loads(app.reception.history_file.read_text())[0]
    assert (h["status"], h["restored"], h["restored_ok"]) == ("interrupted", "HPN Tower restored", True)
    assert h["why"] == "sizeup stopped during the test (an update or a restart)"
    assert h["halves"][0]["cc"]["windows"] == 60 and h["halves"][0]["voice"]["calls"] == 1      # its numbers so far: kept

    again = make_app(tmp_path, monkeypatch)
    again.start()
    try:
        assert again.assign["air"] == "HPN Tower" and again.set_by["air"] == "person"
        assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower"
        assert again.systems["mrrs"].recorder.source_on(0) is None and again.reception.owed is None
        assert [h["status"] for h in again.reception_state()["history"]] == ["interrupted"]      # nothing more to end
    finally:
        again.stop()


def test_after_a_crash_the_next_start_gives_the_dongle_its_job_back(tmp_path, monkeypatch):
    app, clock = make_app(tmp_path, monkeypatch), Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower", keep_min=10) is None
    feed(rt, clock, 769.90625, [40] * 30 + [0] * 30, lock_lines())
    app._call_system(rt, a_call(PD, at=clock.t))
    clock.t += reception.SAVE_S
    app.reception.tick()                                       # its numbers so far are written down
    sample = app.reception.samples_dir / f"{app.reception.samples[0]['id']}.wav"
    left = Path(rt.system.capture_dir) / "mrrs" / "2026" / "open-call.wav"
    left.parent.mkdir(parents=True)
    left.write_bytes(b"RIFF")
    crash(app)
    assert app.reception.marker.is_file() and sample.is_file() and saved_modes(app)["air"] == "HPN Tower"

    again = make_app(tmp_path, monkeypatch)
    assert not sample.exists()                                 # the samples a hard kill left: gone before anything starts
    assert again.assign["air"] == "HPN Tower"                  # as saved: its own job, whatever release starts
    again.start()
    try:
        assert again.assign["air"] == "HPN Tower" and again.set_by["air"] == "person"
        assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower"
        assert again.systems["mrrs"].recorder.source_on(0) is None and again.systems["mrrs"].probe is None
        assert not again.reception.marker.exists() and again.reception.active is None and not left.exists()
        h = again.reception_state()["history"][0]
        assert h["status"] == "interrupted" and h["why"].startswith("sizeup stopped without ending the test")
        assert (h["restored"], h["restored_ok"]) == ("HPN Tower set back at the restart", True)
        assert h["halves"][0]["decoded_pct"] == 42.9 and h["halves"][0]["calls"] == 1 and h["verdict"]["grade"] == "marginal"
        assert again.reception_state()["samples"] == [] and any("interrupted by a restart" in m for m in again.logs)
        # owed until it is seen running there, then quietly done
        assert again.reception.owed["was"] == "HPN Tower" and again.reception.health() is None
        again.reception.clock = lambda: time.time() + 60
        again.reception.tick()
        assert again.reception.owed is None and not again.reception.owed_file.exists()
        assert again.reception_state()["history"][0]["restored"] == "HPN Tower set back at the restart"
        # a call of the test's that settles only now is never traffic
        again._call_system(again.systems["mrrs"], a_call(PD, name="late"))
        time.sleep(0.2)
        assert not again.said and not again.cad.store.q("SELECT * FROM radio_traffic")
    finally:
        again.stop()


def test_after_a_crash_the_trunk_recorder_service_is_stopped_and_the_claim_seen_gone_first(tmp_path, monkeypatch):
    """The Linux station: the system's trunk-recorder runs under its own service and outlives sizeup. sdr_modes.json
    naming the test's job (written by hand here, as a release from before this would have left it). At the start it
    is told to stop, again while its claim is still on the dongle, before the dongles are started; a dongle that
    still can't be had then is owed and retried."""
    app = make_app(tmp_path, monkeypatch)
    running(app)
    assert start(app, "air", pause="HPN Tower") is None
    crash(app)
    modes = saved_modes(app)
    modes["air"] = "MRRS 770"
    app._mode_file.write_text(json.dumps(modes))
    service_pid = os.getppid()
    monkeypatch.setattr(claims, "helpers", lambda: {service_pid})
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "air.json").write_text(json.dumps({"app": "sizeup", "pid": service_pid, "mode": "MRRS 770", "since": 1}))
    again = make_app(tmp_path, monkeypatch)
    assert again.assign["air"] == "MRRS 770"
    rec, stopped = again.systems["mrrs"].recorder, []
    rec.unit = True

    def stop(why=""):
        stopped.append((why, again.assign["air"]))
        if len(stopped) == 2:                                  # the service lets go only the second time it is told
            (claims.DIR / "air.json").unlink()
    rec.stop = stop
    again.reception.recover()
    assert stopped == [("the reception test was ended by a restart", "MRRS 770")] * 2
    assert not (claims.DIR / "air.json").exists()
    assert again.assign["air"] == "HPN Tower" and saved_modes(again)["air"] == "HPN Tower"
    assert again.reception.history[0]["status"] == "interrupted" and not again.reception.marker.exists()
    again.reception.recover()                                  # nothing left to do
    assert len(again.reception.history) == 1
    # the boot loop was refused the dongle after all (the service hung on to it): owed, said, retried, done
    other_app_claims("air", "MRRS 770")
    again.assign["air"] = "Off"                                # (what App.start does with a dongle it can't have)
    clock = Clock(time.time() + 100)
    again.reception.clock, again.reception.inline = clock, True
    again.reception.tick()
    assert again.reception.owed["tries"] == 1 and again.reception.health()["push"] is True
    assert again.reception.history[0]["restored_ok"] is False
    (claims.DIR / "air.json").unlink()
    clock.t += reception.OWED_RETRY_S + 1
    again.reception.tick()
    assert again.assign["air"] == "HPN Tower" and again.reception.owed is None
    assert again.reception.history[0]["restored"] == "HPN Tower restored (after 2 tries)"
    for r in again.receivers:
        r.stop()


def test_each_side_in_sequence_on_the_one_dongle(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower", jobs=["MRRS 770", "MRRS 773"], job=None) is None
    feed(rt, clock, 769.90625, [0] * 300, [tr("Started with Control Channel: 769.906250 MHz")])
    clock.t = app.reception.active["phase_until"] + 1
    app.reception.tick()
    t = app.reception.active
    assert t is not None and t["phase"] == 1 and app.assign["air"] == "MRRS 773" and saved_modes(app)["air"] == "HPN Tower"
    assert rt.recorder.source_on(0) is None and rt.recorder.source_on(1) == "air" and rt.probe.job == "MRRS 773"
    assert json.loads((claims.DIR / "air.json").read_text())["mode"] == "MRRS 773"
    assert rt.recorder.until == t["phase_until"] + reception.UNTIL_GRACE_S
    feed(rt, clock, 773.03125, [39] * 300, lock_lines(773.03125))
    live = app.reception_state()["active"]
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


def test_every_side_is_swept_before_trunk_recorder_takes_the_dongle(lab, tmp_path):
    app, clock = lab, Clock()
    running(app, clock)
    power = tmp_path / "rtl_power"
    power.write_text("#!/bin/sh\nexit 1\n")
    app.cfg = replace(app.cfg, rtl_fm=str(tmp_path / "rtl_fm"))
    asked = []

    def fake_sweep(binary, index, listed, center, rate, started=None):
        asked.append((binary, index, len(listed), center, json.loads((claims.DIR / "air.json").read_text())["mode"],
                      app.assign["air"], app.reception._lock.acquire(blocking=False)))
        app.reception._lock.release()
        return {"floor_db": -48.0, "channels": [{"mhz": round(listed[0] / 1e6, 5), "db": -30.0, "over_db": 18.0}]}
    app.reception.sweep_fn = fake_sweep
    assert start(app, "air", pause="HPN Tower", jobs=["MRRS 770", "MRRS 773"], job=None) is None
    # both sides, with the airband job off the dongle and the dongle claimed for the test, and the lock not held
    assert asked == [(str(power), 3, 4, 770312500.0, "MRRS test", "Off", True),
                     (str(power), 3, 3, 773081250.0, "MRRS test", "Off", True)]
    rt = app.systems["mrrs"]
    feed(rt, clock, 769.90625, [40] * 10, lock_lines())
    sig = app.reception_state()["active"]["running"]["signal"]
    assert sig["cc_over_db"] == 18.0 and sig["sweep"]["floor_db"] == -48.0
    clock.t = app.reception.active["phase_until"] + 1
    app.reception.tick()                                       # the next side: no second sweep, the dongle never free
    assert len(asked) == 2 and app.reception.probe.sweep["channels"][0]["mhz"] == 772.48125
    assert app.reception_post({"action": "stop"}) is None

    def boom(*a, **k):
        raise OSError("rtl_power gone")
    app.reception.sweep_fn = boom                              # a sweep that fails: the test goes on without it
    assert start(app, "air", pause="HPN Tower") is None
    assert app.assign["air"] == "MRRS 770" and app.reception.probe.sweep is None


def test_stop_is_immediate_during_the_sweep(lab, tmp_path):
    """The test's own thread, a sweep that hangs: Stop answers at once, kills rtl_power, and the job comes back."""
    import threading
    app = lab
    running(app)
    app.reception.inline = False
    (tmp_path / "rtl_power").write_text("#!/bin/sh\n")
    app.cfg = replace(app.cfg, rtl_fm=str(tmp_path / "rtl_fm"))
    sweeping, killed = threading.Event(), threading.Event()

    class Proc:
        kill = staticmethod(killed.set)

    def hanging(binary, index, listed, center, rate, started=None):
        started(Proc())
        sweeping.set()
        killed.wait(20)
        return None
    app.reception.sweep_fn = hanging
    assert start(app, "air", pause="HPN Tower") is None
    assert sweeping.wait(5) and app.reception.active["step"] == "starting" and app.assign["air"] == "Off"
    assert app.reception_state()["active"]["step"] == "starting"            # (the state never waits on the sweep)
    t0 = time.monotonic()
    assert app.reception_post({"action": "stop"}) is None
    assert time.monotonic() - t0 < 0.5 and killed.is_set()
    end = time.time() + 5
    while time.time() < end and app.reception.active is not None:
        time.sleep(0.02)
    h = app.reception.history[0]
    assert (h["status"], h["restored"], h["halves"]) == ("stopped", "HPN Tower restored", [])
    assert app.assign["air"] == "HPN Tower" and json.loads((claims.DIR / "air.json").read_text())["mode"] == "HPN Tower"


def test_an_end_that_raises_is_ended_again_and_stop_always_works(lab):
    """Whatever goes wrong while a test is being ended (here: the restore raises, once), it is ended again on the next
    tick, with the status it was ending with; it never sits there with its dongle."""
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, "air", pause="HPN Tower") is None
    real, calls = app.reception._restore, []

    def once(t):
        calls.append(1)
        if len(calls) <= 2:                                    # (the end is tried twice on the spot)
            raise RuntimeError("disk full")
        return real(t)
    app.reception._restore = once
    assert app.reception_post({"action": "stop"}) is None      # Stop answers; it didn't get through
    assert app.reception.active is not None and app.reception.probe is None and app.assign["air"] == "MRRS 770"
    assert app.reception_state()["active"]["step"] == "stopping" and any("not ended yet: RuntimeError" in m for m in app.logs)
    assert json.loads(app.reception.marker.read_text())["was"] == "HPN Tower"       # still written down
    app.reception.tick()
    h = app.reception.history[0]
    assert (h["status"], h["why"], h["restored"]) == ("stopped", "stopped by a person", "HPN Tower restored")
    assert app.reception.active is None and app.assign["air"] == "HPN Tower" and len(h["halves"]) == 1
    # a test left "running" with nothing measuring it (a bug elsewhere): Stop still ends it, and so does a tick
    for how in ("stop", "tick"):
        assert start(app, "air", pause="HPN Tower") is None
        app.reception.probe = rt.probe = None
        if how == "stop":
            assert app.reception_post({"action": "stop"}) is None
        else:
            app.reception.tick()
        assert app.reception.active is None and app.assign["air"] == "HPN Tower", how
    assert app.reception.history[0]["why"] == "the test lost its measurement"


def test_a_side_whose_trunk_recorder_was_not_running_says_so():
    clock = Clock()
    p = reception.Probe(MRRS, "MRRS 770", "spare", clock)
    feed(p, clock, 769.90625, [40] * 20, lock_lines())         # a minute of it, then it is down for the other fourteen
    p.feed_line("SIZEUP-UNIT mrrs 4100001 call 9211")
    p.ran(clock.t, True)
    for _ in range(14 * 60):
        clock.t += 1
        p.ran(clock.t, False)
    r = p.result()
    assert r["cc"]["down_pct"] == 93 and r["verdict"]["grade"] == "good"
    assert r["verdict"]["hint"].startswith("trunk-recorder wasn't running for 93% of the test: graded from the site and the grants")
    q = reception.Probe(MRRS, "MRRS 770", "spare", clock)
    q.ran(clock.t, False)
    clock.t += 600
    q.ran(clock.t, False)
    v = q.result()["verdict"]
    assert v["grade"] == "none" and v["hint"] == "trunk-recorder wasn't running for 100% of the test: nothing to judge."


def test_the_service_keeps_its_claim_from_one_side_of_a_test_to_the_next(tmp_path):
    """"MRRS 770" then "MRRS 773" on the one dongle: the supervisor's own claim is renamed, never released and never
    "held" against itself; the county's dongle is still never taken, and it still yields to the county at once."""
    _county, mrrs, pop, _logs, _d = service(tmp_path)
    p = Path(trunkrecd.system_state_path("mrrs", str(tmp_path / "state" / "p25_recorder.json")))
    p.parent.mkdir(parents=True, exist_ok=True)

    def ask(index, mode, seq):
        p.write_text(json.dumps({"on": True, "label": "MRRS", "pid": None, "seq": seq,
                                 "sources": {str(index): {"sdr": "pd-b", "mode": mode, "name": "pd-b"}}}))
    ask(0, "MRRS 770", 1)
    assert mrrs.step(100.0)["state"] == "ok" and json.loads((claims.DIR / "pd-b.json").read_text())["mode"] == "MRRS 770"
    seen = []
    real = trunkrecd.release
    trunkrecd.release = lambda *a, **k: (seen.append(a[0]), real(*a, **k))[1]
    try:
        ask(1, "MRRS 773", 2)
        st = mrrs.step(101.0)
    finally:
        trunkrecd.release = real
    assert st["state"] == "ok" and not seen and len(pop.started) == 2 and len(pop.running) == 1      # one restart, no gap
    assert json.loads((claims.DIR / "pd-b.json").read_text())["mode"] == "MRRS 773" and mrrs.claims_held == {"pd-b": "MRRS 773"}
    # someone else's claim under another job is still someone else's
    (claims.DIR / "pd-a.json").write_text(json.dumps({"app": "sizeup", "pid": SERVICE_PID, "mode": "Fire/EMS 15", "since": 1.0}))
    p.write_text(json.dumps({"on": True, "label": "MRRS", "pid": None, "seq": 3,
                             "sources": {"0": {"sdr": "pd-a", "mode": "MRRS 770", "name": "pd-a"}}}))
    st = mrrs.step(102.0)
    assert st["state"] == "held" and json.loads((claims.DIR / "pd-a.json").read_text())["mode"] == "Fire/EMS 15"
    # and the county naming this system's dongle stops it at once (#419), as before
    ask(1, "MRRS 773", 4)
    assert mrrs.step(103.0)["state"] == "ok"
    from tests.test_p25_systems import ask_county
    ask_county(tmp_path, "pd-b")
    st = mrrs.step(104.0)
    assert st["state"] == "held" and not pop.running and "pd-b" not in mrrs.claims_held


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
    assert app.set_talkgroup_tier(9563, "off", "mrrs") is None     # a talkgroup a person switched Off
    assert start(app, keep_min=10) is None
    rt.ingest.scan()
    for c in rt.ingest.scan():                                 # the call as its ingest hands it over
        rt.ingest.on_call(c)
    for line in ("SIZEUP-UNIT mrrs 4100009 call 9213", "SIZEUP-UNIT mrrs 4100004 call 31999", "SIZEUP-UNIT mrrs 4100002 call 9211"):
        rt.recorder.on_line(line)
    app._call_system(rt, a_call(EMS, at=clock.t, secs=90.0))
    app._call_system(rt, a_call(9563, at=clock.t))             # Off: counted, never a sample
    enc = a_call(PD, name="enc", at=clock.t)
    enc.encrypted = True
    app._call_system(rt, enc)                                  # a call that says it was encrypted: the same
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
    st = app.reception_state()
    run = st["active"]["running"]
    assert (run["voice"]["calls"], run["encrypted_grants"], run["unknown_grants"]) == (4, 1, 1)
    assert run["signal"]["voice_snr_db"] == 20.0
    assert [(s["tg"], s["name"], s["seconds"]) for s in st["samples"]] == [(EMS, "PC-Rye EMS", 90.0), (PD, "PD Dispatch", 2.5)]
    assert "4100002" not in json.dumps(st)
    sid = st["samples"][0]["id"]
    wav = app.reception_sample(sid)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels()) == (8000, 1) and w.getnframes() == 60 * 8000    # its first minute
    import base64
    assert base64.b64decode(app.reception_sample_b64(sid)) == wav and app.reception_sample_b64("0" * 12) == ""
    assert app.reception_sample("../../etc/passwd") is None and app.reception_sample("nope") is None
    assert len(sorted(app.reception.samples_dir.glob("*.wav"))) == 2
    # the test ends: what its trunk-recorder left on disk (a call still open) is deleted with it
    left = d / "9211-open-call.wav"
    left.write_bytes(b"RIFF")
    assert app.reception_post({"action": "stop"}) is None
    ended = app.reception.history[0]["ended"]
    assert not left.exists() and not any(Path(rt.system.capture_dir).iterdir())
    # the samples stay their ten minutes, then they are deleted
    clock.t += 9 * 60 - 62
    app.reception.tick()
    assert len(app.reception_state()["samples"]) == 2
    clock.t += 2 * 60
    app.reception.tick()
    assert app.reception_state()["samples"] == [] and not list(app.reception.samples_dir.glob("*.wav"))
    assert app.reception_sample(sid) is None
    # a call of the test's that settles after its end (trunk-recorder was still writing it) is never traffic ...
    for k, at in enumerate((ended - 30, ended + 5, ended + reception.LATE_CALL_S - 1)):
        app._call_system(rt, a_call(PD, name=f"late{k}", at=at))
    time.sleep(0.3)
    assert not app.said and not app.cad.store.q("SELECT * FROM radio_traffic") and not list(app.clips_dir.glob("*.wav"))
    assert not any(r["source"] == "p25:mrrs:9211" and r["count"] for r in app.state()["talkgroups"])
    # ... and once the system has a dongle of its own, its calls are its own again: by tier, as before
    assert app.set_mode("MRRS 770", "spare") is None
    app._call_system(rt, a_call(PD, name="after", at=ended + reception.LATE_CALL_S + 60))
    assert rt.probe is None and any(r["source"] == "p25:mrrs:9211" and r["count"] for r in app.state()["talkgroups"])


def test_keep_min_zero_keeps_no_audio(lab):
    app, clock = lab, Clock()
    rt = running(app, clock)
    assert start(app, keep_min=0) is None
    app._call_system(rt, a_call(PD, at=clock.t))
    assert app.reception.probe.calls == 1 and app.reception.samples == [] and not app.reception.samples_dir.exists()


# -- the station's trunk-recorder service: its own deadline for a test --------------------------------------------------------


def test_the_trunk_recorder_service_lets_a_tests_dongle_go_at_the_deadline_by_itself(tmp_path):
    """sizeup-station never comes back to end the test (it crashed and stays down): the service stops the system and
    removes its claim once the state's "until" has passed. A state without one runs for good, as before."""
    _county, mrrs, pop, _logs, _d = service(tmp_path)
    p = Path(trunkrecd.system_state_path("mrrs", str(tmp_path / "state" / "p25_recorder.json")))
    p.parent.mkdir(parents=True, exist_ok=True)
    asked = {"on": True, "label": "MRRS", "pid": None, "seq": 1, "until": 1000.0,
             "sources": {"0": {"sdr": "pd-b", "mode": "MRRS 770", "name": "pd-b"}}}
    p.write_text(json.dumps(asked))
    assert trunkrecd.wanted_system(str(p))["until"] == 1000.0
    st = mrrs.step(900.0)
    assert st["state"] == "ok" and len(pop.running) == 1 and (claims.DIR / "pd-b.json").exists()
    st = mrrs.step(1000.0)
    assert st["state"] == "off" and st["expired"] is True and "time is up" in st["detail"]
    assert not pop.running and not (claims.DIR / "pd-b.json").exists()
    assert mrrs.step(1001.0)["state"] == "off" and len(pop.started) == 1          # and it stays off
    for bad in (None, "soon", True, -5):
        p.write_text(json.dumps(dict(asked, until=bad, seq=2)))
        assert trunkrecd.wanted_system(str(p))["until"] is None
    assert mrrs.step(2000.0)["state"] == "ok" and len(pop.running) == 1           # no deadline: for good


def test_the_stations_recorder_writes_the_deadline_only_for_a_test(tmp_path, monkeypatch):
    d = system_dir(tmp_path)
    path = tmp_path / "state" / "p25_recorder.mrrs.json"
    r = SystemUnitRecorder("mrrs", "MRRS", "/x/trunk-recorder --config=config.json", str(d), state_path=str(path),
                           status_path=str(tmp_path / "run" / "status.mrrs.json"), wait_s=0.2)
    try:
        r.until = 5000.0
        r.give(0, finder("pd-b"), "MRRS 770")
        assert json.loads(path.read_text())["until"] == 5000.0 and trunkrecd.wanted_system(str(path))["on"]
        r.until = None
        r.give(1, finder("pd-a"), "MRRS 773")
        assert "until" not in json.loads(path.read_text())
    finally:
        r.shutdown()


# -- the pages: the station's own, and the hub's view of it ----------------------------------------------------------------------


def test_the_state_says_what_can_be_tested_and_on_which_dongle(lab):
    app = lab
    running(app)
    assert "reception" not in app.state()                      # never in the state everyone gets
    st = app.reception_state()
    assert st["systems"] == [{"name": "mrrs", "label": "MRRS", "watch": [PD, EMS],
                              "jobs": [{"job": "MRRS 770", "mhz": 770.3125}, {"job": "MRRS 773", "mhz": 773.08125}],
                              "caveat": "Some traffic is carried over LTE and never on RF."}]
    assert st["durations"] == [15, 60, 240, 0] and st["keep_min"] == 10.0 and st["active"] is None and st["history"] == []
    assert st["alert"] is None
    assert [(d["serial"], d["job"], d["refuse"]) for d in st["dongles"]] == [
        ("v4", "FG1", "protected: not lendable"), ("trunk", "Fire/EMS 15", "protected: not lendable"),
        ("page", "South 2", "protected: not lendable"), ("air", "HPN Tower", ""), ("spare", "Off", "")]


def test_the_station_reports_it_and_the_hub_may_ask_for_it(lab):
    from sizeup.cad.api import ADMIN_READS, DOES, WRITES
    from sizeup.cloud import station
    app = lab
    running(app)
    assert {"reception_post", "reception_sample_b64"} <= set(station.CALLS)

    class Link:                                                # StationLink._call, with nothing else of the link
        _status_now, _live_wake = False, type("W", (), {"set": lambda self: None})()
    Link.app = app
    out = station.StationLink._call(Link(), {"fn": "reception_post", "args": [
        {"action": "start", "job": "MRRS 770", "sdr": "spare"}, "", "0123456789ab"]})         # as the hub sends it
    assert out == {"ok": True, "result": None} and app.assign["spare"] == "MRRS 770"
    assert app.reception.active["id"] == "0123456789ab" and "by" not in app.reception.active
    out = station.StationLink._call(Link(), {"fn": "reception_post", "args": [{"action": "start", "job": "MRRS 770", "sdr": "air"}]})
    assert out["result"] == "a reception test is already running: stop it first"
    # the hub's Stop never waits for the dongle: it answers at once, the outcome is in the report that follows
    app.reception.inline = False
    out = station.StationLink._call(Link(), {"fn": "reception_post", "args": [{"action": "stop"}, "", ""]})
    assert out == {"ok": True, "result": None} and app.reception_state()["active"]["step"] == "stopping"
    app.reception.tick()
    h = app.reception_state()["history"][0]
    assert (h["id"], h["status"], h["why"], h["restored"]) == ("0123456789ab", "stopped", "stopped by a person", "Off again")
    assert "by" not in h and "by" not in json.loads(app.reception.history_file.read_text())[0]
    assert reception.ended_text(h) == "Reception test stopped (stopped by a person): MRRS 770 on SDR spare · none · Off again"
    # the POST is an admin's (no rule of its own: admin), the reads an admin's, and the start is in the activity
    assert not any(rx.fullmatch("/api/reception") for rx, _need in WRITES)
    assert {"/api/reception", "/api/reception/sample"} <= set(ADMIN_READS)
    say = next(fn for rx, fn in DOES if rx.fullmatch("/api/reception"))
    assert say(None, {"action": "start", "job": "MRRS 770", "sdr": "air", "pause": "HPN Tower"}, {}, None)[0] == (
        "Reception test started: MRRS 770 on SDR air, pausing HPN Tower")
    assert say(None, {"action": "stop"}, {}, None)[0] == "Reception test stopped"


def test_the_web_server_starts_stops_and_serves_a_sample(lab):
    import threading
    import urllib.error
    import urllib.request
    app, clock = lab, Clock()
    rt = running(app, clock)
    server = appmod.make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.headers, r.read()

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
        code, out = post({"action": "start", "job": "MRRS 770", "sdr": "page", "minutes": 15, "pause": "South 2", "protected": True})
        assert code == 400 and "protected: not lendable" in out["error"] and app.assign["page"] == "South 2"
        assert post({"action": "start", "job": "MRRS 770", "sdr": "air", "minutes": 15, "pause": "HPN Tower"}) == (200, {"ok": True})
        app._call_system(rt, a_call(PD, at=clock.t))
        headers, raw = get("/api/reception")
        st = json.loads(raw)["reception"]
        assert headers["Cache-Control"] == "no-store" and st["active"]["was"] == "HPN Tower" and len(st["samples"]) == 1
        server.poke()
        time.sleep(0.3)
        state = json.loads(get("/api/state")[1])
        assert "reception" not in state and "samples" not in json.dumps(state)
        assert next(x for x in state["sdr"]["sdrs"] if x["serial"] == "air")["lent"]["paused"] == "HPN Tower"
        headers, wav = get(f"/api/reception/sample?id={st['samples'][0]['id']}")
        assert headers["Content-Type"] == "audio/wav" and headers["Cache-Control"] == "no-store" and wav[:4] == b"RIFF"
        with pytest.raises(urllib.error.HTTPError) as e:
            get("/api/reception/sample?id=000000000000")
        assert e.value.code == 404
        assert post({"action": "stop"}) == (200, {"ok": True}) and app.assign["air"] == "HPN Tower"
    finally:
        server.httpd.shutdown()


RX_REPORT = {"systems": [{"name": "mrrs", "label": "MRRS", "jobs": [], "watch": [9211], "caveat": ""}], "durations": [15],
             "keep_min": 10.0, "dongles": [], "active": None, "alert": None,
             "samples": [{"id": "0123456789ab", "tg": 9211, "name": "PD Dispatch", "at": 1.0, "seconds": 4.0, "errors": 0,
                          "kept": 1.0, "keep_s": 600.0, "age_s": 5, "job": "MRRS 770"}],
             "history": [{"id": "aaaaaaaaaaaa", "system": "mrrs", "label": "MRRS", "sdr": "00000104", "sdr_name": "104",
                          "was": "HPN Tower", "plan": ["MRRS 770"], "minutes": 15, "started": 10.0, "ended": 910.0,
                          "status": "done", "why": "", "note": "whip on the roof", "verdict": {"grade": "marginal", "hint": "x"},
                          "restored": "HPN Tower restored", "restored_ok": True, "halves": []}]}


def test_on_the_hub_only_an_admin_sees_or_runs_a_reception_test(keys, access_hub):  # noqa: F811
    """End to end on a hub behind Access: the station's report (made up here) is an admin's; a dispatcher or a viewer
    gets a 403 from the state, the sample and the POST, and none of it is in the state everyone gets."""
    h = access_hub
    people(h.app.cad)
    as_ = lambda email: {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=email)}  # noqa: E731
    st = station_status()
    st["reception"] = RX_REPORT
    assert call(h.url + "/api/station/state", "POST", {"at": 1, "status": st})[0] == 200
    for who in (JULIAN, VAL, DEE):
        for path in ("/api/reception", "/api/reception/sample?id=0123456789ab"):
            code, out = req(h.url + path, headers=as_(who))
            assert code == 403 and out["error"].startswith("Admins only") and "whip" not in json.dumps(out), (who, path)
        code, out = req(h.url + "/api/reception", "POST", {"action": "start", "job": "MRRS 770", "sdr": "00000104"},
                        headers=as_(who))
        assert code == 403, who
        code, state = req(h.url + "/api/state", headers=as_(who))
        assert code == 200 and "reception" not in state and "whip on the roof" not in json.dumps(state)
    assert req(h.url + "/api/reception")[0] == 401             # nobody: not even that
    code, out = req(h.url + "/api/reception", headers=as_(COREY))
    assert code == 200 and out["reception"]["history"][0]["note"] == "whip on the roof"
    assert len(out["reception"]["samples"]) == 1


def activity(h) -> list[tuple]:
    return [(r["email"], r["kind"], r["summary"], r["ok"]) for r in
            h.app.cad.store.q("SELECT email, kind, summary, ok FROM user_activity WHERE route LIKE '%reception%' AND kind != 'signin' ORDER BY id")]


def test_the_hub_logs_refused_attempts_and_how_each_test_ended(keys, access_hub):  # noqa: F811
    h = access_hub
    people(h.app.cad)
    admin = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=COREY)}
    # refused (here: no station to run it; on the station: a held or protected dongle, its words): logged as refused
    code, out = req(h.url + "/api/reception", "POST", {"action": "start", "job": "MRRS 770", "sdr": "00000101",
                                                       "pause": "South 2"}, headers=admin)
    assert code == 400 and out["error"]
    assert [(a[0], a[1], a[3]) for a in activity(h)] == [(COREY, "refused", 0)] and "Refused: reception" in activity(h)[0][2]
    # a start that goes through: the station is sent the test's id, never who asked; the hub keeps who under it
    sent = []
    h.app._switch = lambda fn, *a: sent.append((fn, a)) and None
    body = {"action": "start", "job": "MRRS 770", "sdr": "00000104", "pause": "HPN Tower"}
    assert req(h.url + "/api/reception", "POST", body, headers=admin)[0] == 200
    (fn, (got, by, tid)), = sent
    assert (fn, got, by) == ("reception_post", body, "") and reception.SAMPLE_ID.fullmatch(tid)
    assert h.app._reception_who(tid) == COREY and COREY not in json.dumps(sent)
    assert activity(h)[1] == (COREY, "action", "Reception test started: MRRS 770 on SDR 00000104, pausing HPN Tower", 1)
    h.app._reception_who("aaaaaaaaaaaa", COREY)                # (the made-up report's tests below were his too)
    h.app._reception_who("bbbbbbbbbbbb", COREY)
    # the station's report: a test that ended is a row of who started it, once, with what became of the dongle
    st = station_status()
    st["reception"] = RX_REPORT
    for _ in range(3):
        assert call(h.url + "/api/station/state", "POST", {"at": 1, "status": st})[0] == 200
    assert activity(h)[2:] == [(COREY, "action", "Reception test done: MRRS 770 on SDR 104 · marginal · HPN Tower restored", 1)]
    # ... a Stop from here answers before the dongle is back: its outcome arrives the same way, late or not restored
    late = json.loads(json.dumps(RX_REPORT))
    late["history"].insert(0, dict(late["history"][0], id="bbbbbbbbbbbb", status="stopped", restored_ok=False,
                                   why="SDR 104 was switched in Settings > Radios",
                                   restored="HPN Tower NOT restored yet (in use by shortfinal (AM 118.575)): retrying"))
    st["reception"] = late
    assert call(h.url + "/api/station/state", "POST", {"at": 2, "status": st})[0] == 200
    assert activity(h)[3] == (COREY, "action", "Reception test stopped (SDR 104 was switched in Settings > Radios): MRRS 770 "
                              "on SDR 104 · marginal · HPN Tower NOT restored yet (in use by shortfinal (AM 118.575)): "
                              "retrying", 0)
    late["history"][0].update(restored="HPN Tower restored (after 3 tries)", restored_ok=True)
    for _ in range(2):
        assert call(h.url + "/api/station/state", "POST", {"at": 3, "status": st})[0] == 200
    assert len(activity(h)) == 5 and activity(h)[4][2].endswith("HPN Tower restored (after 3 tries)") and activity(h)[4][3] == 1


def test_the_hub_alerts_when_a_paused_job_is_not_given_back(keys, access_hub):  # noqa: F811
    h = access_hub
    st = station_status()
    st["health"]["reception"] = {"job": "HPN Tower", "sdr": "104", "since": 1.0, "push": True,
                                 "text": "HPN Tower on SDR 104 was not restored after the reception test (in use by "
                                         "shortfinal (AM 118.575)); retrying"}
    assert call(h.url + "/api/station/state", "POST", {"at": 1, "status": st})[0] == 200
    h.app.health.tick()
    a = next(x for x in h.app.health.alerts() if x["key"] == "reception")
    assert (a["severity"], a["title"]) == ("critical", "Reception test: job not restored") and "HPN Tower on SDR 104" in a["text"]
    assert not h.app.health.active["reception"].get("quiet")
    # a spare dongle left lent: in the panel, never pushed
    st["health"]["reception"] = {"job": "Off", "sdr": "104", "since": 1.0, "push": False, "text": "SDR 104: still lent, 10 min "
                                 "after its reception test should have ended"}
    assert call(h.url + "/api/station/state", "POST", {"at": 2, "status": st})[0] == 200
    h.app.health.tick()
    a = next(x for x in h.app.health.alerts() if x["key"] == "reception")
    live = h.app.health.active["reception"]
    assert a["severity"] == "warning" and live["quiet"] is True and h.app.health._notify_ok(live, time.time()) is False
    st["health"]["reception"] = None
    assert call(h.url + "/api/station/state", "POST", {"at": 3, "status": st})[0] == 200
    h.app.health.tick()
    assert h.app.health.model()["reception"] is None
