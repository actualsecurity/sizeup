"""GitHub #547: trunk-recorder stayed up and "ok" for 22 h while recording nothing, nothing restarted it, its own output
never reached the journal, and an absent dongle or a stalled rtl_fm said nothing (or the wrong thing).

The trunk-recorder service's watchdog (sizeup/radio/trunkrecd.py Watchdog): a fake trunk-recorder (FakePopen, from
tests/test_trunk_recorder_unit.py) that stops producing call files, call files made in a tmp capture directory with
their times set, its output fed as trunk-recorder writes it (tests/fixtures/trunk-recorder-sample.log's shape). Its lines
into the journal (Shipper). The FM receivers' absent / back / stall lines (sizeup/radio/fm.py) with a fake rtl_fm.
No radio hardware, no systemd."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from sizeup.config import ConfigError, load
from sizeup.radio import fm, trunkrecd
from sizeup.radio.fm import Channel, NfmReceiver
from tests.test_page import PAGING, hiss
from tests.test_trunk_recorder_unit import ask, supervisor

FIX = Path(__file__).parent / "fixtures"
T0 = 1_763_100_000.0               # a Friday afternoon (any time: the watchdog goes by the system's own call rate)
MIN = 60.0
HEAD = "[2026-11-14 15:55:01.123456] ({level})   [westchester]\t"


class Rig:
    """The county's supervisor running a fake trunk-recorder whose capture directory is tmp_path/cap."""

    def __init__(self, tmp_path, watch: str = ""):
        self.s, self.pop, self.logs = supervisor(tmp_path)
        work = tmp_path / "tr"
        c = json.loads((work / "config.json").read_text())
        self.cap = tmp_path / "cap"
        c["captureDir"] = str(self.cap)
        (work / "config.json").write_text(json.dumps(c, indent=2))
        conf = tmp_path / "config.toml"
        conf.write_text(conf.read_text() + watch)
        self.n = 0
        ask(tmp_path)

    def call(self, t: float, keep: bool = False) -> None:
        """A call trunk-recorder recorded at t: its "Concluding Recorded Call" line, then its WAV and JSON in
        captureDir/<system>/<y>/<m>/<d>/, which sizeup takes away seconds later (app.py _drop_recorder_files; keep:
        a bench without sizeup's clip store, where they stay)."""
        self.s.watchdog.line(HEAD.format(level="info") + "\x1b[0;34m2C\x1b[0m\tTG: \x1b[35m      9999\x1b[0m\t"
                             "Freq: 476.075000 MHz\t\x1b[33mConcluding Recorded Call\x1b[0m - Last Update: 4s\t"
                             "Recorder last write:4.51275\tCall Elapsed: 8", t)
        d = self.cap / "westchester" / "2026" / "11" / "14"
        new = [p for p in (d, *d.parents) if p != self.cap and self.cap in p.parents and not p.exists()]
        d.mkdir(parents=True, exist_ok=True)
        self.n += 1
        for ext in (".wav", ".json"):
            p = d / f"9999-{int(t)}_{self.n}{ext}"
            p.write_text("{}" if ext == ".json" else "")
            os.utime(p, (t, t))
            if not keep:
                p.unlink()
        for p in [d, *new]:                                   # (the directories changed at t, as on the station)
            os.utime(p, (t, t))

    def step(self, t: float, mono: float | None = None) -> dict:
        """One step at wall time t (the monotonic clock reads t too unless said: a wall clock step)."""
        return self.s.step(t, t if mono is None else mono)

    @property
    def starts(self) -> int:
        return len(self.pop.started)

    def watchdog_lines(self) -> list[str]:
        return [x for x in self.logs if x.startswith("p25: watchdog: ")]


def run(r: Rig, t0: float, t1: float, every: float = 30.0, calls_every: float | None = None, keep=False) -> None:
    """Step the service from t0 to t1 (and a call every calls_every, when given)."""
    t, next_call = t0, t0
    while t < t1:
        if calls_every is not None and t >= next_call:
            r.call(t, keep)
            next_call += calls_every
        r.step(t)
        t += every


# -- calls stop -----------------------------------------------------------------------------------------------------------

def test_a_busy_system_that_stops_writing_calls_is_restarted_once_after_its_limit_and_not_before(tmp_path):
    r = Rig(tmp_path)
    run(r, T0, T0 + 3 * 3600, calls_every=MIN)               # a call a minute for 3 h: 10 gaps = 10 min, under 15
    assert r.starts == 1 and r.watchdog_lines() == []
    last = r.s.watchdog.last_call
    assert last is not None and last >= T0 + 3 * 3600 - 2 * MIN
    lim, why = r.s.watchdog.limit(r.s._wcfg)
    assert lim == trunkrecd.WATCH_MIN_S and why.startswith("18") and "calls in the 3 h before the last" in why
    t = T0 + 3 * 3600
    while t < last + trunkrecd.WATCH_MIN_S - 1:                # the calls stop: nothing for 15 min less a second
        r.step(t)
        t += 30
    r.step(last + trunkrecd.WATCH_MIN_S - 1)
    assert r.starts == 1 and r.s.status["state"] == "ok"
    st = r.step(last + trunkrecd.WATCH_MIN_S + 30)
    assert st["state"] == "down" and st["detail"].startswith("watchdog restarted trunk-recorder: no new call file for 15")
    (line,) = r.watchdog_lines()
    assert line.startswith("p25: watchdog: no new call file for 15 min (limit 15 min: ")
    assert line.endswith("restarting trunk-recorder (1 of 3 this hour)")
    assert r.pop.signals and r.pop.started[0].code is not None   # the old one stopped (its process group)
    st = r.step(last + trunkrecd.WATCH_MIN_S + 31)
    assert st["state"] == "ok" and r.starts == 2 and st["restarts"] == 1
    assert st["watchdog"]["restarts_1h"] == 1 and st["watchdog"]["last"]["why"].startswith("no new call file")
    t = last + trunkrecd.WATCH_MIN_S + 31
    for k in range(20):                                        # the new one records again: no more restarts
        t += 30
        r.call(t)
        r.step(t)
    assert r.starts == 2 and len(r.watchdog_lines()) == 1


def test_a_quiet_night_with_a_low_baseline_is_restarted_at_most_once(tmp_path):
    """12 calls in 3 h (a 15 min mean gap) then a quiet 6 h: the limit is 10 gaps (2 h 30 min); the one restart
    with no call since doubles it (5 h), so the night isn't a restart loop."""
    r = Rig(tmp_path)
    run(r, T0, T0 + 3 * 3600, every=60.0, calls_every=15 * MIN)
    last = r.s.watchdog.last_call
    lim, _ = r.s.watchdog.limit(r.s._wcfg)
    assert lim == pytest.approx(10 * 15 * MIN)
    run(r, T0 + 3 * 3600, last + lim - 30, every=60.0)
    assert r.starts == 1                                       # 2 h 29 min of quiet: fine
    run(r, last + lim - 30, last + 6 * 3600, every=60.0)
    assert r.starts == 2 and len(r.watchdog_lines()) == 1
    assert "doubled" not in r.watchdog_lines()[0]
    lim2, why2 = r.s.watchdog.limit(r.s._wcfg)
    assert lim2 == pytest.approx(2 * lim) and "doubled 1×" in why2


def test_too_few_calls_for_a_rate_waits_the_longest(tmp_path):
    r = Rig(tmp_path)
    run(r, T0, T0 + 3 * 3600, every=60.0, calls_every=40 * MIN, keep=True)   # 5 calls in 3 h: no rate (files kept)
    last = r.s.watchdog.last_call
    lim, why = r.s.watchdog.limit(r.s._wcfg)
    assert lim == trunkrecd.WATCH_MAX_S and "too few for a rate" in why
    run(r, T0 + 3 * 3600, last + trunkrecd.WATCH_MAX_S - 60, every=120.0)
    assert r.starts == 1
    run(r, last + trunkrecd.WATCH_MAX_S - 60, last + trunkrecd.WATCH_MAX_S + 120, every=60.0)
    assert r.starts == 2


def test_the_limits_come_from_config_toml_and_zero_restarts_is_no_watchdog(tmp_path):
    r = Rig(tmp_path, "watchdog_min_min = 30\nwatchdog_max_min = 60\nwatchdog_factor = 5\nwatchdog_per_hour = 0\n")
    run(r, T0, T0 + 3 * 3600, calls_every=MIN)
    assert r.s._wcfg == {"min_s": 1800.0, "max_s": 3600.0, "factor": 5.0, "per_hour": 0}
    run(r, T0 + 3 * 3600, T0 + 8 * 3600, every=120.0)          # 5 h without a call: no watchdog
    assert r.starts == 1 and r.watchdog_lines() == []
    p = tmp_path / "c.toml"
    for bad in ("watchdog_min_min = 1", "watchdog_per_hour = 13", "watchdog_factor = true"):
        p.write_text(f"[p25]\n{bad}\n")
        with pytest.raises(ConfigError, match=f"p25.{bad.split()[0]}: a number from"):
            load(p)
    p.write_text("[p25]\nwatchdog_min_min = 20\nwatchdog_per_hour = 2\n")
    load(p)                                                    # sizeup-station accepts what the service reads


# -- the control channel ------------------------------------------------------------------------------------------------

def rate(r: Rig, t: float, n: float) -> None:
    """A decode rate line as trunk-recorder writes it (the drain's thread feeds them in the service)."""
    r.s.watchdog.line(HEAD.format(level="info") + f"Control Channel Message Decode Rate: {n:g}/sec, count:  {int(n * 3)}",
                      t)


def test_a_hung_decoder_is_restarted_and_the_hours_cap_holds(tmp_path):
    """Every rate is logged ("controlWarnRate": -1, now in config.json): none for 2 min is a hung decoder. At most 3
    restarts in an hour, the cap said once; the next one an hour after the first."""
    r = Rig(tmp_path)
    r.step(T0)
    for k in range(10):
        rate(r, T0 + 3 * k, 38)
        r.step(T0 + 3 * k)
    hung = T0 + 27
    r.step(hung + trunkrecd.CC_SILENT_S - 1)
    assert r.starts == 1
    t = hung + trunkrecd.CC_SILENT_S
    r.step(t)
    assert r.starts == 1 and r.s.status["state"] == "down"
    assert r.watchdog_lines() == ["p25: watchdog: no control channel decode rate logged for 2 min (decoder hung?): "
                                  "restarting trunk-recorder (1 of 3 this hour)"]
    starts = [t]
    while t < hung + 3600 + 200:                               # each new one hangs too
        t += 10
        r.step(t)
        if r.s.status["state"] == "down" and r.s.status["detail"].startswith("watchdog"):
            starts.append(t)
    restarted = [x for x in r.logs if x.endswith("this hour)")]
    assert len(restarted) == 4                                 # 3 in the first hour, the 4th an hour after the 1st
    assert restarted[2].endswith("(3 of 3 this hour)") and restarted[3].endswith("(3 of 3 this hour)")
    capped = [x for x in r.logs if "not restarted" in x]
    assert len(capped) == 1 and capped[0].endswith("not restarted: 3 restart(s) in the last hour")
    assert starts[3] - starts[0] >= 3600


def test_healthy_rates_and_an_old_config_never_trip_it(tmp_path):
    r = Rig(tmp_path)
    r.step(T0)
    for k in range(1200):                                      # an hour of healthy rates, every 3 s
        rate(r, T0 + 3 * k, 35 + k % 5)
        if k % 10 == 0:
            r.call(T0 + 3 * k)
        r.step(T0 + 3 * k)
    assert r.starts == 1 and r.watchdog_lines() == []
    (tmp_path / "old").mkdir()
    r2 = Rig(tmp_path / "old")
    r2.step(T0)
    rate(r2, T0 + 3, 7)                                        # only the low ones logged (the default warn rate):
    run(r2, T0 + 3, T0 + 600, calls_every=MIN)                 # no line for minutes is no fault
    assert r2.starts == 1 and r2.watchdog_lines() == []


def test_the_control_channel_lost_for_ten_minutes_is_restarted(tmp_path):
    r = Rig(tmp_path)
    r.step(T0)
    t = T0
    while t < T0 + trunkrecd.CC_LOST_RESTART_S + 30:
        rate(r, t, 0)
        if t < T0 + 30:                                        # (the calls stop with it)
            r.call(t)
        r.step(t)
        t += 3
        if r.watchdog_lines():
            break
    (line,) = r.watchdog_lines()
    assert line.startswith("p25: watchdog: control channel lost (under 2 msg/s) for 10 min")
    assert t - 3 >= T0 + trunkrecd.CC_LOST_RESTART_S


# -- its lines into the journal ------------------------------------------------------------------------------------------

def test_its_own_lines_reach_the_journal_filtered_and_rate_limited():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append)
    for line in (FIX / "trunk-recorder-sample.log").read_text().splitlines():   # calls, grants, aliases: none
        sh.line(line, T0)
    sh.line(HEAD.format(level="info") + "SIZEUP-UNIT westchester 1234567 call 9999", T0)
    assert said == []
    sh.line(HEAD.format(level="info") + "Control Channel Message Decode Rate: 38/sec, count:  114", T0)
    assert said == ["p25: trunk-recorder: control channel decode rate 38/s"]
    for k in range(1, 100):                                    # healthy, every 3 s: not one line each
        sh.line(HEAD.format(level="info") + "Control Channel Message Decode Rate: 37/sec, count:  111", T0 + 3 * k)
    assert len(said) == 1
    t = T0 + 300
    sh.line(HEAD.format(level="error") + "Control Channel Message Decode Rate: 0/sec, count:  0", t)
    assert said[-1] == "p25: trunk-recorder: control channel lost: 0/s"
    sh.line(HEAD.format(level="error") + "Retuning to Control Channel: 476.112500 MHz", t + 1)
    for k in range(20):                                        # retuning every 3 s: one line, the rest counted
        sh.line(HEAD.format(level="error") + "Retuning to Control Channel: 476.212500 MHz", t + 3 * k + 2)
    sh.line(HEAD.format(level="error") + "Control Channel Message Decode Rate: 40/sec, count:  120", t + 30)
    assert said[-2:] == ["p25: trunk-recorder: control channel lost: 0/s",
                         "p25: trunk-recorder: (error) [westchester] Retuning to Control Channel: 476.112500 MHz"]
    sh.line(HEAD.format(level="info") + "Control Channel Message Decode Rate: 40/sec, count:  120", t + 61)
    assert said[-1] == "p25: trunk-recorder: control channel back: 40/s"
    n = len(said)
    for k in range(25):                                        # 25 different errors in one window: SHIP_BURST shown
        sh.line(HEAD.format(level="error") + f"recorder error kind {chr(65 + k)}: unit 1234567 on 476.075000 MHz", t + 70)
    sh.line(HEAD.format(level="warning") + "something else", t + 71)
    shown = said[n:]
    assert len(shown) <= trunkrecd.SHIP_BURST
    assert all("1234567" not in x and "#" in x for x in shown if "unit" in x)        # radio IDs masked
    assert all("476.075000 MHz" in x for x in shown if "unit" in x)                  # frequencies kept
    sh.tick(t + 70 + trunkrecd.SHIP_WINDOW_S)
    assert said[-1].startswith("p25: trunk-recorder: ") and " more line(s) like these in " in said[-1]
    assert said[-1].endswith(" min (trunk-recorder.log)") and int(said[-1].split()[2]) == 20 + 16 + 1   # retunes, errors past the burst, the warning
    sh.line(HEAD.format(level="info") + "Control Channel Message Decode Rate: 39/sec, count:  117",
            t + 61 + trunkrecd.RATE_LOG_S)
    assert said[-1].startswith("p25: trunk-recorder: control channel 39/s (last 30 min: ")


def test_the_supervisor_drains_its_output_into_the_journal(tmp_path):
    """The real drain thread: a trunk-recorder stand-in that prints a healthy rate, a call and an error."""
    r = Rig(tmp_path)

    class Out:
        def __init__(self, lines):
            self.lines = [x.encode() + b"\n" for x in lines] + [b""]

        def readline(self):
            return self.lines.pop(0) if self.lines else b""

    proc_lines = [HEAD.format(level="info") + "Control Channel Message Decode Rate: 38/sec, count:  114",
                  (FIX / "trunk-recorder-sample.log").read_text().splitlines()[0],
                  HEAD.format(level="error") + "Failed to open audio file for unit 1234567"]
    pop = r.pop
    orig = pop.__call__

    def popen(argv, **kw):
        p = orig(argv, **kw)
        p.stdout = Out(proc_lines)
        return p
    r.s.popen = popen
    r.step(T0)
    r.s._drain_thread.join(timeout=5)
    tr = [x for x in r.logs if x.startswith("p25: trunk-recorder: ")]
    assert tr == ["p25: trunk-recorder: control channel decode rate 38/s",
                  "p25: trunk-recorder: (error) [westchester] Failed to open audio file for unit #"]
    assert r.s.watchdog.cc_all is True


# -- the FM receivers: a dongle gone, back, a stall ---------------------------------------------------------------------

class QuickStop(threading.Event):
    """The receiver's stop event with its 15 s waits made short."""

    def wait(self, timeout=None):
        return super().wait(min(timeout or 0.01, 0.01))


def fake_rtl_fm(tmp_path, absent_runs: int, audio_s: float = 3.0) -> str:
    """An "rtl_fm" whose first absent_runs runs find no dongle (as rtl_fm says it), then one that gives audio and hangs."""
    raw = tmp_path / "audio.raw"
    raw.write_bytes((np.clip(hiss(audio_s), -1, 1) * 32767).astype("<i2").tobytes())
    n = tmp_path / "runs"
    n.write_text("0")
    exe = tmp_path / "rtl_fm"
    exe.write_text(f'#!/bin/sh\nk=$(cat "{n}")\necho $((k+1)) > "{n}"\n'
                   f'if [ "$k" -lt {absent_runs} ]; then echo "No supported devices found." >&2; exit 1; fi\n'
                   f'cat "{raw}"\nexec sleep 30\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def wait_for(pred, s=20.0):
    end = time.monotonic() + s
    while time.monotonic() < end and not pred():
        time.sleep(0.02)
    return pred()


def test_a_dongle_gone_is_said_once_and_once_when_it_is_back(tmp_path):
    events: list[str] = []
    r = NfmReceiver(fake_rtl_fm(tmp_path, absent_runs=5), "", None, Channel(470.2, "South 2"), lambda c, s: None,
                    events.append, **PAGING)
    r._stop = QuickStop()
    r.start()
    try:
        assert wait_for(lambda: any("back after" in e for e in events))
        gone = [e for e in events if "not connected" in e]
        assert gone == ["South 2: dongle 'any' not connected (looking again every 15 s)"]   # 5 looks, one line
        back = [e for e in events if "back after" in e]
        assert len(back) == 1 and back[0].startswith("South 2: dongle 'any' back after ")
    finally:
        r.stop()


def test_a_dongle_not_plugged_in_at_the_lookup_is_said_once(tmp_path):
    events: list[str] = []
    looks = {"n": 0}

    def find():
        looks["n"] += 1
        return None if looks["n"] <= 4 else "0"
    find.label = "FG Spare"
    r = NfmReceiver(fake_rtl_fm(tmp_path, absent_runs=0), find, None, Channel(470.2, "FG1"), lambda c, s: None,
                    events.append, **PAGING)
    r._stop = QuickStop()
    r.start()
    try:
        assert wait_for(lambda: any("back after" in e for e in events))
        assert [e for e in events if "not plugged in" in e] == ["FG1: SDR 'FG Spare' not plugged in (looking again every 15 s)"]
        assert len([e for e in events if "back after" in e]) == 1 and looks["n"] >= 5
    finally:
        r.stop()


def test_a_stall_is_said_as_a_stall_not_a_stop(tmp_path, monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(fm, "STALL_S", 0.5)
    r = NfmReceiver(fake_rtl_fm(tmp_path, absent_runs=0, audio_s=1.0), "", None, Channel(470.2, "South 2"),
                    lambda c, s: None, events.append, **PAGING)
    r.start()
    try:
        assert wait_for(lambda: any(e.startswith("South 2 down") for e in events))
        down = next(e for e in events if e.startswith("South 2 down"))
        assert down == "South 2 down: rtl_fm stalled: no audio for 0.5 s"
        assert "stopped" not in down
    finally:
        r.stop()


# -- review fixes ----------------------------------------------------------------------------------------------------

def test_radio_ids_are_masked_before_a_full_stop_and_rates_and_frequencies_stay():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append)
    sh.line(HEAD.format(level="error") + "Failed to open audio file for Unit 1234567.", T0)
    sh.line(HEAD.format(level="warning") + "source center 477193750 rate: 2400000 · 476075000 Hz · 38/sec", T0)
    assert said == ["p25: trunk-recorder: (error) [westchester] Failed to open audio file for Unit #.",
                    "p25: trunk-recorder: (warning) [westchester] source center 477193750 rate: 2400000 · "
                    "476075000 Hz · 38/sec"]
    from sizeup.cloud import health
    from sizeup.radio import systems
    assert health.no_ids is trunkrecd.no_ids and systems.no_ids is trunkrecd.no_ids          # one copy


def test_a_wall_clock_step_is_no_hung_decoder(tmp_path):
    """NTP, or WSL2 resyncing after the host slept, moves the wall clock 150 s at once: the decoder logged 3 s ago."""
    r = Rig(tmp_path)
    r.step(T0)
    for k in range(20):
        rate(r, T0 + 3 * k, 38)
        r.step(T0 + 3 * k)
    m = T0 + 60
    rate(r, m, 38)
    r.step(m + 150, mono=m + 1)                                # the wall clock jumps 150 s; 1 s has really passed
    for k in range(1, 20):
        rate(r, m + 3 * k, 38)
        r.step(m + 150 + 3 * k, mono=m + 3 * k)
    assert r.starts == 1 and r.watchdog_lines() == []


def test_the_control_channel_lost_for_hours_backs_off():
    """A dead site or antenna: restarting can't fix it. 10 min, then 20, 40, 80, 160: 5 restarts in 6 h, not 18."""
    wd = trunkrecd.Watchdog()
    cfg = trunkrecd.watch_settings({})
    wd.started(T0, "", "k")
    started, n, t = T0, 0, T0
    while t < T0 + 6 * 3600:
        wd.line(HEAD.format(level="error") + "Control Channel Message Decode Rate: 0/sec, count:  0", t)
        kind, why = wd.fault(t, t, started, started, cfg)
        if kind and wd.allowed(t, cfg):
            assert kind == "control"
            wd.restarted(t, t, kind, why)
            wd.started(t, "", "k")
            started, n = t, n + 1
        t += 3
    assert n == 5 and wd.lost_limit() == 32 * trunkrecd.CC_LOST_RESTART_S
    for k in range(250):                                       # it decodes well for over 10 min: back to 10 min
        wd.line(HEAD.format(level="info") + "Control Channel Message Decode Rate: 38/sec, count:  114", t + 3 * k)
    assert wd.lost_limit() == trunkrecd.CC_LOST_RESTART_S


def test_calls_concluded_but_no_files_written_still_back_off(tmp_path):
    """trunk-recorder keeps logging "Concluding Recorded Call" but writes no file: each restart doubles the limit
    (only a new file ends that)."""
    r = Rig(tmp_path)
    run(r, T0, T0 + 3 * 3600, calls_every=MIN)
    lim1, _ = r.s.watchdog.limit(r.s._wcfg)
    t = T0 + 3 * 3600
    while not r.watchdog_lines():
        r.s.watchdog.line(HEAD.format(level="info") + "Concluding Recorded Call - Last Update: 4s", t)   # no file
        r.step(t)
        t += 30
    r.s.watchdog.line(HEAD.format(level="info") + "Concluding Recorded Call - Last Update: 4s", t)
    lim2, why2 = r.s.watchdog.limit(r.s._wcfg)
    assert lim2 == 2 * lim1 and "doubled 1×" in why2
    r.step(t + 30)                                             # (started again)
    r.call(t + 60)                                             # a file again: back to its limit
    r.step(t + 60)
    assert r.s.watchdog.limit(r.s._wcfg)[0] == lim1


def test_a_receiver_stopped_while_its_dongle_is_gone_forgets_it():
    events: list[str] = []
    r = NfmReceiver("rtl_fm", "", None, Channel(470.2, "South 2"), lambda c, s: None, events.append, **PAGING)
    r._absent("dongle 'any' not connected")
    r.stop()
    assert r._gone is None
