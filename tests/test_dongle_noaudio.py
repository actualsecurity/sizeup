"""A dongle that opens but streams no audio (#703): counted failed after NOAUDIO_STALLS stalls with no audio within
NOAUDIO_WINDOW_S (after one USB reset), out of its antenna group's plan like an unplugged one, tried again idle at
NOAUDIO_PROBE_S, back when it streams, a person picks its job, or it is replugged. The station as
tests/test_antenna_groups.py fakes it (USB list, claims, no rtl_fm); the stalls are the receivers' own hook calls
(fm.py on_stall / on_audio), whose firing is tested on its own with a fake rtl_fm. Serials and names are made up."""

from __future__ import annotations

import json
import stat
import time
from types import SimpleNamespace

import pytest

import sizeup.app as appmod
import sizeup.radio.claims as claims
from sizeup.radio import fm, usbreset
from sizeup.radio.fm import Channel, NfmReceiver
from tests.test_antenna_groups import DONGLES, GROUPS, P25, grouped, on, park, rig, uhf4  # noqa: F401 - rig: the fixture


@pytest.fixture
def st(rig, monkeypatch):  # noqa: F811
    """rig, past the startup grace, with a USB reset that works (resets: the serials reset) and no /sys."""
    plugged, logs, make = rig
    resets: list[str] = []
    monkeypatch.setattr(appmod, "NOAUDIO_GRACE_S", 0.0)
    monkeypatch.setattr(appmod.usbreset, "reset", lambda sd: resets.append(sd) or "")
    monkeypatch.setattr(appmod.usbreset, "ident", lambda sd: None)
    return plugged, logs, make, resets


def stall(app, job, n=appmod.NOAUDIO_STALLS):
    """n stalls with no audio of job's receiver, as its thread reports them (fm.py on_stall)."""
    r = app._receiver(job)
    for _ in range(n):
        app._on_stall(r)
    return r


def out(app, job, n=2):
    """job's dongle out: a USB reset after the first NOAUDIO_STALLS, out at the next."""
    stall(app, job)
    return stall(app, job)


def test_a_stalling_paging_dongle_moves_paging_and_the_lower_job_loses_its_dongle_after_one_usb_reset(st):
    plugged, logs, make, resets = st
    app = grouped(make)
    stall(app, "South 2")
    assert resets == ["uhf-b"] and not app._noaudio                         # one USB reset first, not out yet
    assert any("3 stalls with no audio" in x and "USB reset" in x for x in logs)
    stall(app, "South 2", appmod.NOAUDIO_STALLS - 1)
    assert not app._noaudio
    stall(app, "South 2", 1)
    assert set(app._noaudio) == {"uhf-b"} and resets == ["uhf-b"]           # out: never a second reset
    assert sum("CRITICAL SDR V4 streams no audio (South 2)" in x for x in logs) == 1
    app._sdr_arrivals()
    # the paging job moves to the lowest job's dongle; the county's (higher) is never taken. FG1, with no healthy
    # dongle left, goes to the out one as a last resort and keeps retrying there (never nowhere)
    assert (on(app, P25), on(app, "South 2"), on(app, "FG1")) == ("uhf-a", "uhf-c", "uhf-b")
    assert app._own == {"uhf-c": "FG1", "uhf-b": "South 2"} and app.sdr()["groups"][0]["short"] == []
    row = {r["serial"]: r for r in app.sdr()["sdrs"]}["uhf-b"]
    assert row["noaudio"]["job"] == "South 2" and row["noaudio"]["retry_min"] == [2, 10, 30]
    assert sum("streams no audio" in x for x in logs) == 1


def test_the_lowest_jobs_dongle_out_takes_no_higher_ones_and_keeps_trying_on_its_own(st):
    plugged, logs, make, resets = st
    app = grouped(make)
    r = out(app, "FG1")
    app._sdr_arrivals()
    # nothing lower to take: the higher jobs keep their dongles; FG1's rtl_fm keeps retrying where it is (its own
    # receiver is the test: its first audio ends the "no audio")
    assert (on(app, P25), on(app, "South 2"), on(app, "FG1")) == ("uhf-a", "uhf-b", "uhf-c")
    assert app.sdr()["groups"][0]["short"] == [] and not app._own
    app._on_audio(r)
    app._sdr_arrivals()
    assert not app._noaudio and app.sdr()["groups"][0]["short"] == []


def test_where_no_reset_is_possible_it_is_out_at_the_first_stalls(st, monkeypatch):
    plugged, logs, make, resets = st
    monkeypatch.setattr(appmod.usbreset, "reset", lambda sd: "WSL: no USB reset through usbipd")
    app = grouped(make)
    stall(app, "South 2")
    assert set(app._noaudio) == {"uhf-b"}
    assert any("no USB reset (WSL: no USB reset through usbipd)" in x for x in logs)


def test_stalls_spread_out_or_broken_by_audio_never_add_up(st):
    plugged, logs, make, resets = st
    app = grouped(make)
    r = app._receiver("South 2")
    now = time.monotonic()
    # two stalls longer ago than the window: the next one is the first of its window
    app._stalls["uhf-b"] = [now - appmod.NOAUDIO_WINDOW_S - 30, now - appmod.NOAUDIO_WINDOW_S - 1]
    app._on_stall(r)
    assert len(app._stalls["uhf-b"]) == 1
    app._on_stall(r)
    app._on_audio(r)                                                         # audio between: they start over
    assert "uhf-b" not in app._stalls
    app._on_stall(r)
    app._on_stall(r)
    assert len(app._stalls["uhf-b"]) == 2 and not resets and not app._noaudio
    app._on_stall(r)
    assert resets == ["uhf-b"]                                               # (the third in the window counts)


def spared(make):
    """UHF with a spare (spare-1, Off by nobody): an out dongle's job goes there and the out dongle is idle."""
    from tests.test_antenna_groups import mapped
    app = make().start()
    mapped(app)
    assert app.set_mode("Off", "spare-1") is None
    assert app.set_antenna_groups(uhf4()) is None
    app.set_by.pop("spare-1")
    return app


def test_it_comes_back_when_it_streams_again_and_waits_longer_each_try_it_doesnt(st, monkeypatch):
    plugged, logs, make, resets = st
    app = spared(make)
    out(app, "South 2")
    app._sdr_arrivals()
    assert (on(app, "South 2"), on(app, "FG1")) == ("spare-1", "uhf-c")
    tries: list[str] = []
    monkeypatch.setattr(app, "_probe", lambda sd: tries.append(sd) or False)
    app._sdr_arrivals()
    assert tries == []                                                      # not before NOAUDIO_PROBE_S[0]
    app._noaudio["uhf-b"]["next"] = 0.0
    app._sdr_arrivals()
    assert _until(lambda: app._noaudio["uhf-b"]["tries"] == 1) and tries == ["uhf-b"]
    assert app._noaudio["uhf-b"]["next"] - time.monotonic() > appmod.NOAUDIO_PROBE_S[1] - 5     # then 10 min
    app._noaudio["uhf-b"]["next"] = 0.0
    monkeypatch.setattr(app, "_probe", lambda sd: True)                     # streams again
    app._sdr_arrivals()
    assert _until(lambda: not app._noaudio)
    app._sdr_arrivals()
    assert (on(app, "South 2"), on(app, "FG1")) == ("uhf-b", "uhf-c") and not app._own
    assert app.assign.get("spare-1", "Off") == "Off"
    assert any("SDR V4 streams again (tried with rtl_fm): back after" in x for x in logs)


def test_a_replug_ends_it(st):
    plugged, logs, make, resets = st
    app = grouped(make)
    out(app, "South 2")
    app._sdr_arrivals()
    plugged[:] = [d for d in plugged if d[2] != "uhf-b"]
    app._sdr_arrivals()
    app._sdr_arrivals()                                                     # (missing from two looks in a row)
    assert "uhf-b" in app._noaudio
    plugged.insert(1, DONGLES[1])
    app._sdr_arrivals()
    assert not app._noaudio and any("SDR V4 plugged in again" in x for x in logs)
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-b"


def test_a_new_usb_address_ends_it(st, monkeypatch):
    plugged, logs, make, resets = st
    at = [(1, 5)]
    monkeypatch.setattr(appmod.usbreset, "ident", lambda sd: at[0])
    app = grouped(make)
    out(app, "South 2")
    app._sdr_arrivals()
    assert app._noaudio["uhf-b"]["usb"] == (1, 5)
    at[0] = (1, 9)                                                          # replugged between two looks
    app._sdr_arrivals()
    assert not app._noaudio


def test_restart_update_switch_and_another_apps_claim_never_count(rig, monkeypatch):  # noqa: F811
    plugged, logs, make = rig
    monkeypatch.setattr(appmod.usbreset, "reset", lambda sd: "")
    app = grouped(make)
    stall(app, "South 2", 10)                                               # just started (NOAUDIO_GRACE_S)
    assert not app._stalls and not app._noaudio and not app._reset_tried
    monkeypatch.setattr(appmod, "NOAUDIO_GRACE_S", 0.0)
    app.station = SimpleNamespace(updates=SimpleNamespace(state=lambda: {"state": "updating"}))
    stall(app, "South 2", 10)                                               # an update under way
    assert not app._stalls and not app._noaudio
    app.station = None
    app.switching = True                                                    # a switch under way
    stall(app, "South 2", 10)
    app.switching = False
    assert not app._stalls and not app._noaudio
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "uhf-b.json").write_text(json.dumps({"app": "shortfinal", "pid": 1, "mode": "scan", "since": 0}))
    stall(app, "South 2", 10)                                               # another app holds it
    assert not app._stalls and not app._noaudio
    (claims.DIR / "uhf-b.json").unlink()
    app._stopping.set()                                                     # stopping
    stall(app, "South 2", 10)
    app._stopping.clear()
    assert not app._stalls and not app._noaudio
    stall(app, "South 2", 2)
    assert len(app._stalls["uhf-b"]) == 2                                   # counted again


def test_a_persons_pick_ends_it_and_a_persons_off_is_kept(st):
    plugged, logs, make, resets = st
    app = grouped(make)
    out(app, "South 2")
    app._sdr_arrivals()
    assert on(app, "South 2") == "uhf-c"
    assert app.set_mode("South 2", "uhf-b") is None                         # a person: "try it again"
    assert not app._noaudio and on(app, "South 2") == "uhf-b" and not app._own
    assert any("SDR V4 picked by a person (South 2): back after" in x for x in logs)
    stall(app, "South 2", 1)
    assert len(app._stalls["uhf-b"]) == 1                                   # its stalls count again, from none
    out(app, "FG1")
    app._sdr_arrivals()
    assert app.set_mode("Off", "uhf-c") is None                             # a person's Off: ends it, stays Off
    assert not app._noaudio
    app._sdr_arrivals()
    assert app.assign["uhf-c"] == "Off" and on(app, "FG1") is None


def test_a_parked_job_is_never_started_by_it(st):
    plugged, logs, make, resets = st
    app = make().start()
    from tests.test_antenna_groups import mapped
    mapped(app)
    assert app.set_mode("Off", "spare-1") is None
    assert park(app, uhf4(["FG2"])) is None
    app.set_by.pop("spare-1")                                               # a spare
    out(app, "South 2")
    app._sdr_arrivals()
    assert (on(app, "South 2"), on(app, "FG1"), on(app, "FG2")) == ("spare-1", "uhf-c", None)
    out(app, "FG1")
    app._sdr_arrivals()
    # FG1's dongle out too: nothing lower to take, the parked FG2 never runs, FG1 keeps trying where it is
    assert on(app, "FG2") is None and on(app, "FG1") == "uhf-c" and on(app, "South 2") == "spare-1"


def test_an_ungrouped_dongle_is_shown_and_alerted_once_and_ends_at_audio(st):
    from sizeup.cloud.health import StationHealth  # noqa: F401
    from tests.test_health import Rig, station_status
    plugged, logs, make, resets = st
    app = grouped(make)                                                     # FG2 on spare-1: in no group
    r = out(app, "FG2")
    app._sdr_arrivals()
    assert on(app, "FG2") == "spare-1" and "spare-1" in app._noaudio       # nothing to move to: stays, shown
    row = next(x for x in app.radio_status() if x["label"] == "FG2")
    assert (row["state"], row["word"]) == ("down", "no audio") and "SDR spare-1 streams no audio" in row["detail"]
    sdr = app.sdr()
    h = Rig()
    s = station_status()
    s["sdr"]["sdrs"][2].update(serial="spare-1", name="spare-1", mode="FG2",
                               noaudio=next(x for x in sdr["sdrs"] if x["serial"] == "spare-1")["noaudio"])
    h.at(0, status=s)
    h.at(5, status=s)
    h.at(60, status=s)
    (a,) = [a for a in h.h.alerts() if a["key"].startswith("noaudio:")]
    assert (a["key"], a["severity"], a["title"]) == ("noaudio:spare-1", "critical", "SDR spare-1 no audio")
    assert "opens but streams no audio since" in a["text"] and a["who"]["dongle"] == "SDR spare-1"
    d = next(x for x in h.h.model()["dongles"] if x["serial"] == "spare-1")
    assert d["noaudio"]["job"] == "FG2"
    app._on_audio(r)                                                        # its own retries got audio again
    assert not app._noaudio and any("SDR spare-1 streams again (FG2)" in x for x in logs)
    s["sdr"]["sdrs"][2].pop("noaudio")
    h.at(70, status=s)
    assert not [a for a in h.h.alerts() if a["key"].startswith("noaudio:")]
    root = appmod.ROOT / "sizeup" / "web" / "static"
    assert '"no audio", "hl-bad"' in (root / "health.js").read_text()
    assert '" · no audio"' in (root / "radio-controls.js").read_text()


# -- the receiver's hooks (fm.py) and the USB reset (usbreset.py) -----------------------------------------------------

def _until(pred, s=10.0):
    end = time.monotonic() + s
    while time.monotonic() < end and not pred():
        time.sleep(0.02)
    return pred()


def _exe(tmp_path, body: str) -> str:
    p = tmp_path / "rtl_fm"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.mark.parametrize("audio", [False, True])
def test_the_receiver_calls_on_stall_only_when_not_a_byte_came(tmp_path, monkeypatch, audio):
    monkeypatch.setattr(fm, "STALL_S", 0.3)
    body = ("head -c 9600 /dev/zero\n" if audio else "") + "exec sleep 30\n"
    r = NfmReceiver(_exe(tmp_path, body), "", None, Channel(470.2, "South 2"), lambda c, s: None, lambda m: None)
    calls: list[str] = []
    r.on_stall, r.on_audio = (lambda: calls.append("stall")), (lambda: calls.append("audio"))
    r.start()
    try:
        assert _until(lambda: r.restarts >= 1)
        assert calls[:1] == (["audio"] if audio else ["stall"]) and ("stall" in calls) != audio
    finally:
        r.stop()


def test_a_hook_that_raises_never_stops_the_receiver(tmp_path, monkeypatch):
    monkeypatch.setattr(fm, "STALL_S", 0.3)
    events: list[str] = []
    r = NfmReceiver(_exe(tmp_path, "exec sleep 30\n"), "", None, Channel(470.2, "South 2"), lambda c, s: None,
                    events.append)
    r.on_stall = lambda: 1 / 0
    r.start()
    try:
        assert _until(lambda: any("hook error: ZeroDivisionError" in e for e in events))
        assert not r._stop.is_set()
    finally:
        r.stop()


def _sys(tmp_path, devs):
    root = tmp_path / "sys"
    for name, vendor, serial, bus, dev in devs:
        d = root / name
        d.mkdir(parents=True)
        for k, v in (("idVendor", vendor), ("serial", serial), ("busnum", bus), ("devnum", dev)):
            (d / k).write_text(f"{v}\n")
    (root / "1-1:1.0").mkdir()                                              # an interface: never a device
    return root


def test_usb_reset_finds_the_one_dongle_and_never_under_wsl(tmp_path):
    root = _sys(tmp_path, [("1-1", "0bda", "uhf-b", 1, 7), ("1-2", "0bda", "dup", 1, 8), ("1-3", "0bda", "dup", 1, 9),
                           ("2-1", "1d6b", "uhf-c", 2, 3)])
    assert usbreset.ident("uhf-b", root) == (1, 7)
    assert usbreset.ident("dup", root) is None and usbreset.ident("uhf-c", root) is None
    assert usbreset.ident("x", tmp_path / "nope") is None
    assert usbreset.reset("uhf-b", root, wsl=lambda: True) == "WSL: no USB reset through usbipd"
    assert usbreset.reset("dup", root, wsl=lambda: False) == "not found once in /sys"
    why = usbreset.reset("uhf-b", root, dev=str(tmp_path / "dev"), wsl=lambda: False)
    assert why.startswith(f"can't open {tmp_path}/dev/001/007")
    node = tmp_path / "dev" / "001" / "007"
    node.parent.mkdir(parents=True)
    node.write_text("")                                                     # a plain file: the ioctl fails, said
    assert usbreset.reset("uhf-b", root, dev=str(tmp_path / "dev"), wsl=lambda: False).startswith("reset failed")
    assert usbreset.USBDEVFS_RESET == 0x5514


def test_the_constants_are_as_documented():
    assert (appmod.NOAUDIO_STALLS, appmod.NOAUDIO_WINDOW_S) == (3, 120.0)
    assert appmod.NOAUDIO_PROBE_S == (120.0, 600.0, 1800.0) and GROUPS


# -- the review of #710 -------------------------------------------------------------------------------------------------

def test_a_stand_in_that_goes_out_keeps_the_job(st):
    plugged, logs, make, resets = st
    app = grouped(make)
    out(app, "South 2")
    app._sdr_arrivals()
    assert (on(app, "South 2"), on(app, "FG1")) == ("uhf-c", "uhf-b")
    out(app, "South 2")                                                     # its stand-in (uhf-c) stalls too
    assert set(app._noaudio) == {"uhf-b", "uhf-c"}
    app._sdr_arrivals()
    # no healthy dongle for South 2 or FG1: each stays on an out one, retrying; neither stops
    assert (on(app, P25), on(app, "South 2"), on(app, "FG1")) == ("uhf-a", "uhf-c", "uhf-b")
    app._on_audio(app._receiver("South 2"))                                 # the hiccup is over on uhf-c
    app._on_audio(app._receiver("FG1"))                                     # ... and on uhf-b
    app._sdr_arrivals()
    assert not app._noaudio and (on(app, "South 2"), on(app, "FG1")) == ("uhf-b", "uhf-c") and not app._own


def test_out_survives_a_restart_at_the_same_usb_address_only(st, monkeypatch, tmp_path):
    plugged, logs, make, resets = st
    monkeypatch.setattr(appmod.usbreset, "ident", lambda sd: (1, 7) if sd == "uhf-b" else None)
    app = spared(make)
    out(app, "South 2")
    app._sdr_arrivals()
    assert on(app, "South 2") == "spare-1"
    saved = json.loads((tmp_path / "antenna_noaudio.json").read_text())
    assert saved["uhf-b"]["job"] == "South 2" and saved["uhf-b"]["usb"] == [1, 7]
    app.stop()
    again = make().start()                                                  # a restart or an update
    again._sdr_arrivals()
    assert "uhf-b" in again._noaudio and on(again, "South 2") == "spare-1" and on(again, "FG1") == "uhf-c"
    assert any("SDR V4 still out for no audio" in x for x in logs)
    again.stop()
    monkeypatch.setattr(appmod.usbreset, "ident", lambda sd: (1, 9) if sd == "uhf-b" else None)   # replugged meanwhile
    third = make().start()
    third._sdr_arrivals()
    assert not third._noaudio and on(third, "South 2") == "uhf-b"
    assert not (tmp_path / "antenna_noaudio.json").exists()


def test_a_failed_usb_look_is_never_a_replug(st, monkeypatch):
    plugged, logs, make, resets = st
    app = grouped(make)
    out(app, "South 2")
    app._sdr_arrivals()
    keep = list(plugged)
    for _ in range(3):
        plugged[:] = []                                                     # sdr.devices() failing: []
        app._sdr_arrivals()
    plugged[:] = keep
    app._sdr_arrivals()
    assert "uhf-b" in app._noaudio
    plugged[:] = [d for d in keep if d[2] != "uhf-b"]                       # missing once only: no replug either
    app._sdr_arrivals()
    plugged[:] = keep
    app._sdr_arrivals()
    assert "uhf-b" in app._noaudio


def test_the_airband_channelizers_stalls_never_count(tmp_path, monkeypatch):
    calls: list[str] = []

    class Multi(NfmReceiver):
        def _pump(self, proc):                                              # as radio/am.py's channelizer says it
            self.detail = "rtl_sdr stalled"
            return False
    monkeypatch.setattr(fm, "STALL_S", 0.3)
    r = Multi(_exe(tmp_path, "exec sleep 30\n"), "", None, Channel(470.2, "x"), lambda c, s: None, lambda m: None)
    r.on_stall = lambda: calls.append("stall")
    r.start()
    try:
        assert _until(lambda: r.restarts >= 1) and calls == []
    finally:
        r.stop()


def test_the_probe_skips_a_lent_or_held_dongle_and_a_refused_claim_is_no_try(st, monkeypatch):
    plugged, logs, make, resets = st
    app = spared(make)
    out(app, "South 2")
    app._sdr_arrivals()
    tried: list[str] = []
    monkeypatch.setattr(app, "_probe", lambda sd: tried.append(sd) or None)
    monkeypatch.setattr(app.reception, "holds", lambda sd: sd == "uhf-b")
    app._noaudio["uhf-b"]["next"] = 0.0
    app._sdr_arrivals()
    assert tried == [] and app._noaudio["uhf-b"]["next"] > 0                # a reception test has it: not tried
    monkeypatch.setattr(app.reception, "holds", lambda sd: False)
    app._noaudio["uhf-b"]["next"] = 0.0
    app._sdr_arrivals()
    assert _until(lambda: tried == ["uhf-b"] and "uhf-b" not in app._probing)
    assert app._noaudio["uhf-b"]["tries"] == 0                              # couldn't try (None): no try counted


@pytest.mark.parametrize("body, ok", [("exec cat /dev/zero\n", True), ("head -c 4096 /dev/zero\nexec sleep 30\n", False)])
def test_the_probe_wants_audio_the_whole_time_and_lets_go_only_its_own_claim(st, monkeypatch, tmp_path, body, ok):
    plugged, logs, make, resets = st
    monkeypatch.setattr(appmod, "NOAUDIO_PROBE_LEN_S", 1.5)
    app = spared(make)
    out(app, "South 2")
    app._sdr_arrivals()
    exe = _exe(tmp_path, body)
    monkeypatch.setattr(app, "_probe_argv", lambda idx, mhz: [exe])
    assert app._probe("uhf-b") is ok
    assert claims.holder("uhf-b") is None                                    # its own claim, let go
    claims.claim("uhf-b", "reception test")                                 # a claim of ours already there: kept
    app._probe("uhf-b")
    assert (claims.holder("uhf-b") or {}).get("mode") == "reception test"
    claims.release("uhf-b")
    (claims.DIR / "uhf-b.json").write_text(json.dumps({"app": "shortfinal", "pid": 1, "mode": "scan", "since": 0}))
    assert app._probe("uhf-b") is None                                       # another app's: not tried at all
    (claims.DIR / "uhf-b.json").unlink()


def test_the_usb_reset_runs_outside_the_lock(st, monkeypatch):
    plugged, logs, make, resets = st
    app = grouped(make)
    seen: list[bool] = []
    monkeypatch.setattr(appmod.usbreset, "reset", lambda sd: seen.append(app._noaudio_lock.locked()) or "")
    stall(app, "South 2")
    assert seen == [False]


# -- the confirming review of #710 (d4063af2) --------------------------------------------------------------------------

@pytest.mark.parametrize("since", ["Infinity", "NaN", "1e300", "true", "-1", '"x"'])
def test_a_bad_since_in_the_file_is_skipped_and_the_station_starts(st, tmp_path, since):
    plugged, logs, make, resets = st
    (tmp_path / "antenna_noaudio.json").write_text(
        '{"uhf-b": {"since": %s, "job": "South 2", "usb": null}, "uhf-c": {"since": %.1f, "job": "FG1", "usb": null}}'
        % (since, time.time() - 60))
    app = make().start()                                                    # no OverflowError / ValueError at start
    assert "uhf-b" not in app._noaudio and "uhf-c" in app._noaudio


def test_a_bad_entry_that_raises_is_logged_and_skipped(st, tmp_path, monkeypatch):
    plugged, logs, make, resets = st
    (tmp_path / "antenna_noaudio.json").write_text(json.dumps({"uhf-b": {"since": time.time() - 60, "job": "South 2",
                                                                         "usb": [1, 2]}}))
    monkeypatch.setattr(appmod.usbreset, "ident", lambda sd: 1 / 0)
    app = make().start()
    assert not app._noaudio and any("entry 'uhf-b' skipped (ZeroDivisionError)" in x for x in logs)


@pytest.mark.parametrize("body, ok", [("sleep 1.5\nexec cat /dev/zero\n", True), ("exec sleep 30\n", False)])
def test_the_probe_gives_a_slow_dongle_its_stall_time_to_open(st, monkeypatch, tmp_path, body, ok):
    plugged, logs, make, resets = st
    monkeypatch.setattr(appmod, "NOAUDIO_PROBE_LEN_S", 1.0)
    monkeypatch.setattr(fm, "STALL_S", 3.0)
    app = spared(make)
    out(app, "South 2")
    exe = _exe(tmp_path, body)
    monkeypatch.setattr(app, "_probe_argv", lambda idx, mhz: [exe])
    t0 = time.monotonic()
    assert app._probe("uhf-b") is ok
    assert time.monotonic() - t0 < 6


def test_saves_from_two_threads_never_share_a_temp_file(st, tmp_path):
    import threading
    plugged, logs, make, resets = st
    app = grouped(make)
    out(app, "South 2")
    errs: list[BaseException] = []

    def save():
        try:
            for _ in range(50):
                app._save_noaudio()
        except BaseException as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=save) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs and not any("couldn't keep the no-audio dongles" in x for x in logs)
    assert json.loads((tmp_path / "antenna_noaudio.json").read_text())["uhf-b"]["job"] == "South 2"
