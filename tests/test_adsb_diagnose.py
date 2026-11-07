"""Why the station's ADS-B decoder is down, said by the station itself (GitHub #262): readsb's own words classified, the
dongle looked into read-only (the device list, who has its USB device open, the claims, a kernel driver), sizeup's own
leftover job on the reserved dongle stopped (never another app's), the cause sent to the hub and shown, the station's
job manager never leaving a program on the ADS-B dongle, and the hub's "no word from the receiver" logged once then
every 5 minutes.

No real dongle, no real process is touched: /proc and /sys are folders in tmp_path, readsb a fake Popen whose output is
a BytesIO, kill a recorder. The readsb / librtlsdr lines are what readsb v3.16.21 (sdr_rtlsdr.c) and librtlsdr print."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from sizeup import aircraft as A
from sizeup.aircraft import AircraftLayer, StationFeed
from sizeup.cloud import health as H
from sizeup.radio import adsbd
from tests.test_aircraft import FI_URL, LOL_URL, T3, Clock, Feeds
from tests.test_sdr_auto_assign import DONGLES, rig  # noqa: F401 - the station app fixture

ROOT = Path(__file__).resolve().parent.parent
UID = os.getuid()

BUSY = ["rtlsdr: found 2 device(s):", "  0:  Realtek, RTL2838UHIDIR, SN: fg13", "  1:  Realtek, RTL2838UHIDIR, SN: adsb",
        "rtlsdr: using device #1: Generic RTL2832U OEM (Realtek, RTL2838UHIDIR, SN adsb)",
        "usb_claim_interface error -6",
        "FATAL: rtlsdr: error opening the RTLSDR device: Device or resource busy"]

# readsb's watchdog (sdr_rtlsdr.c): its words, with made-up lines around them (#315)
WEDGED = ["rtlsdr: using device #1: Generic RTL2832U OEM (Realtek, RTL2838UHIDIR, SN adsb)",
          "<3>SDR wedged, exiting! (check power supply / avoid using an USB extension / SDR might be defective)",
          "<3>Abnormal exit."]


# ---- the classification table ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("lines, cause", [
    (BUSY, "busy"),
    (["Kernel driver is active, or device is claimed by second instance of librtlsdr."], "busy"),
    (["usb_open error -3", "Please fix the device permissions, e.g. by installing the udev rules file rtl-sdr.rules",
      "FATAL: rtlsdr: error opening the RTLSDR device: Permission denied"], "permissions"),
    (["libusb: error [_get_usbfs_fd] libusb couldn't open USB device /dev/bus/usb/001/004: LIBUSB_ERROR_ACCESS"],
     "permissions"),
    (["FATAL: rtlsdr: no supported devices found."], "not_found"),
    (["No supported devices found."], "not_found"),
    (["rtlsdr: found 1 device(s):", "FATAL: rtlsdr: no device matching 'adsb' found."], "not_found"),
    (["usb_open error -4"], "not_found"),
    (["readsb: unrecognized option '--bogus'", "Try `readsb --help' or `readsb --usage' for more information."],
     "bad_args"),
    (["readsb: option '--gain' requires an argument"], "bad_args"),
    (["/opt/sizeup/readsb/bin/readsb: error while loading shared libraries: librtlsdr.so.0: cannot open shared object "
      "file: No such file or directory"], "library"),
    (["FATAL: rtlsdr_read_async returned unexpectedly, probably lost the USB device, bailing out"], "lost"),
    (WEDGED, "wedged"),
    (["Fri Nov  6 19:01:02 2026 EST  readsb starting up.", "something odd"], "unknown"),
    ([], "unknown"),
])
def test_readsbs_words_say_the_cause(lines, cause):
    got, line = adsbd.classify(lines)
    assert got == cause
    assert (line == "") == (cause == "unknown") and (not line or line in lines)


def test_output_lines_are_cleaned_and_capped():
    assert adsbd.clean(b"\x1b[31musb_claim_interface error -6\x1b[0m\n") == "[31musb_claim_interface error -6[0m"
    assert len(adsbd.clean("x" * 500)) == adsbd.LINE_MAX


# ---- a fake station: /proc, /sys, readsb ------------------------------------------------------------------------------

class Proc:
    def __init__(self, argv, lines=()):
        self.argv, self.pid, self.code = argv, 4321, None
        self.stdout = io.BytesIO("".join(f"{ln}\n" for ln in lines).encode())

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class Station:
    """tmp_path/proc and tmp_path/sys; the ADS-B dongle "adsb" is /dev/bus/usb/001/004."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.proc, self.sys = tmp_path / "proc", tmp_path / "sys"
        self.proc.mkdir()
        self.usb("1-1", "adsb", 1, 4)
        self.usb("1-2", "fg13", 1, 5)
        self.killed: list[tuple[int, int]] = []
        self.lines: list[str] = list(BUSY)
        self.found = [(0, "fg13"), (1, "adsb")]
        self.started: list[Proc] = []

    def usb(self, name, serial, bus, dev, driver=""):
        d = self.sys / "bus" / "usb" / "devices" / name
        d.mkdir(parents=True)
        (d / "serial").write_text(serial + "\n")
        (d / "busnum").write_text(f"{bus}\n")
        (d / "devnum").write_text(f"{dev}\n")
        itf = d.parent / f"{name}:1.0"
        itf.mkdir()
        if driver:
            (itf / "driver").symlink_to(self.root / "drivers" / driver)

    def process(self, pid, comm, argv, unit, uid=UID, holds="/dev/bus/usb/001/004"):
        p = self.proc / str(pid)
        (p / "fd").mkdir(parents=True)
        (p / "comm").write_text(comm + "\n")
        (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (p / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (p / "cgroup").write_text(f"0::/system.slice/{unit}\n")
        (p / "fd" / "0").symlink_to("/dev/null")
        if holds:
            (p / "fd" / "3").symlink_to(holds)

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        shutil.rmtree(self.proc / str(pid), ignore_errors=True)

    def popen(self, argv, **kw):
        assert kw.get("stdout") is not None and kw.get("stderr") is not None     # its output is kept
        p = Proc(argv, self.lines)
        self.started.append(p)
        return p

    def decoder(self, logs=None):
        exe = self.root / "readsb"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        conf = self.root / "config.toml"
        conf.write_text('[aircraft.receiver]\nsdr = "adsb"\n\n[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\n')
        return adsbd.Decoder(config=str(conf), override=str(self.root / "state" / "adsb_receiver.json"), binary=str(exe),
                             json_dir=str(self.root / "json"), status_path=str(self.root / "run" / "decoder.json"),
                             claims=self.root / "claims", popen=self.popen, found=lambda: list(self.found),
                             log=(logs.append if logs is not None else lambda m: None), proc_root=str(self.proc),
                             sys_root=str(self.sys), kill=self.kill, sleep=lambda s: None, uid=UID)

    def run_and_exit(self, d, code=1):
        assert d.step(1000.0)["state"] == "starting"
        self.started[-1].code = code
        return d.step(1002.0)


FG1_ARGV = ["/opt/sizeup/rtl-sdr/bin/rtl_fm", "-M", "fm", "-f", "453.0375M", "-s", "16000", "-E", "dc", "-d", "1", "-"]


# ---- sizeup's own leftover job: found and stopped --------------------------------------------------------------------

def test_sizeups_own_leftover_rtl_fm_on_the_adsb_dongle_is_named_stopped_and_readsb_retried(tmp_path):
    s = Station(tmp_path)
    s.process(812, "rtl_fm", FG1_ARGV, "sizeup-station.service")
    s.process(900, "python3", ["python3", "-m", "sizeup"], "sizeup-station.service", holds="")
    logs: list[str] = []
    d = s.decoder(logs)
    st = s.run_and_exit(d)
    assert st["state"] == "down" and st["cause"] == "busy" and st["code"] == 1
    assert st["holder"] == "rtl_fm (pid 812, sizeup-station's FG1 job)"
    assert st["detail"].startswith("dongle busy (held by rtl_fm (pid 812, sizeup-station's FG1 job); stopped it, retrying)")
    assert "readsb: usb_claim_interface error -6" in st["detail"]
    assert st["tail"] == BUSY[-2:] or st["tail"] == BUSY[-3:]
    assert len(st["tail"]) == adsbd.DETAIL_LINES and st["recovery"].startswith("stopped rtl_fm")
    assert s.killed == [(812, signal.SIGTERM)]                         # it went on SIGTERM: no SIGKILL
    assert st["present"] is True and st["index"] == 1
    assert d.next_start == 1002.0 + adsbd.BACKOFF_S[0] and d.fails == 0  # straight back: the dongle is free now
    assert any("stopped sizeup's leftover pid 812" in m for m in logs)
    assert any(m.startswith("readsb: usb_claim_interface error -6") for m in logs)   # still in the journal
    s.lines = ["rtlsdr: using device #1", "readsb starting up."]
    st = d.step(1004.0)
    assert st["state"] == "starting" and len(s.started) == 2 and "cause" not in st     # retried, the cause cleared
    on_disk = json.loads((tmp_path / "run" / "decoder.json").read_text())
    assert on_disk["state"] == "starting"


def test_a_leftover_that_ignores_sigterm_gets_sigkill(tmp_path):
    s = Station(tmp_path)
    s.process(812, "trunk-recorder", ["trunk-recorder", "--config=/var/lib/sizeup/tr.json"], "sizeup-trunk-recorder.service")
    stubborn = []
    s.kill = lambda pid, sig: stubborn.append((pid, sig))              # the process stays
    st = s.run_and_exit(s.decoder())
    assert stubborn == [(812, signal.SIGTERM), (812, signal.SIGKILL)]
    assert "sizeup-trunk-recorder's P25 job" in st["holder"]


def test_the_retry_text_keeps_the_cause(tmp_path):
    s = Station(tmp_path)
    s.process(77, "rtl_tcp", ["rtl_tcp", "-d", "1"], "shortfinal.service")
    d = s.decoder()
    s.run_and_exit(d)
    st = d.step(1003.0)
    assert st["state"] == "down" and st["cause"] == "busy" and "retry in" in st["detail"]
    assert st["detail"].startswith("dongle busy (held by rtl_tcp (pid 77, shortfinal.service))")


# ---- never another app's ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("comm, argv, unit, uid", [
    ("rtl_fm", FG1_ARGV, "shortfinal.service", UID),                     # shortfinal's own rtl_fm
    ("rtl_fm", FG1_ARGV, "sizeup-station.service", UID + 1),              # another user's
    ("rtl_fm", FG1_ARGV + ["--shortfinal"], "sizeup-station.service", UID),
    ("rtl_tcp", ["rtl_tcp", "-d", "1"], "sizeup-station.service", UID),    # not one of sizeup's programs
    ("rtl_fm", FG1_ARGV, "session-3.scope", UID),                          # someone at a shell
    ("dump1090", ["dump1090", "--device-index", "1"], "dump1090.service", UID),
])
def test_another_apps_process_is_never_stopped_only_named(tmp_path, comm, argv, unit, uid):
    s = Station(tmp_path)
    s.process(812, comm, argv, unit, uid=uid)
    st = s.run_and_exit(s.decoder())
    assert s.killed == [] and st["cause"] == "busy" and st["recovery"] == ""
    assert st["holder"].startswith(f"{comm} (pid 812") and "stopped it" not in st["detail"]
    assert not adsbd.is_own({"comm": comm, "argv": argv, "unit": unit, "uid": uid}, UID)


def test_a_process_another_apps_claim_names_is_theirs(tmp_path):
    s = Station(tmp_path)
    pid = os.getppid()                                                  # alive, so the claim is live
    s.process(pid, "rtl_fm", FG1_ARGV, "sizeup-station.service")
    (tmp_path / "claims").mkdir()
    d = s.decoder()
    assert d.step(1000.0)["state"] == "starting"
    (tmp_path / "claims" / "adsb.json").write_text(json.dumps({"app": "shortfinal", "pid": pid, "mode": "ADS-B"}))
    s.started[-1].code = 1
    d.step(1002.0)
    assert s.killed == []


def test_is_own_is_narrow():
    own = {"comm": "rtl_fm", "argv": FG1_ARGV, "unit": "sizeup-station.service", "uid": UID}
    assert adsbd.is_own(own, UID)
    assert adsbd.is_own(dict(own, comm="trunk-recorder", unit="sizeup-trunk-recorder.service"), UID)
    assert not adsbd.is_own(dict(own, uid=None), UID)
    assert not adsbd.is_own(dict(own, unit=""), UID)
    assert not adsbd.is_own(dict(own, comm="readsb"), UID)


def test_only_sizeups_programs_are_ever_signalled_in_the_source():
    src = (ROOT / "sizeup" / "radio" / "adsbd.py").read_text()
    assert src.count("self.kill(") == 2                                # SIGTERM, then SIGKILL: both in recover()
    rec = src[src.index("    def recover("):src.index("    def _write(")]
    assert "self.kill(pid, signal.SIGTERM)" in rec and 'd.get("own")' in rec
    assert adsbd.OWN_PROGRAMS == ("rtl_fm", "trunk-recorder")
    assert adsbd.OWN_UNITS == ("sizeup-station.service", "sizeup-trunk-recorder.service")


# ---- the other causes, looked into ------------------------------------------------------------------------------------

def test_the_serial_gone_from_the_list_says_not_found_and_what_is_there(tmp_path):
    s = Station(tmp_path)
    s.lines = ["FATAL: rtlsdr: no device matching '1' found."]
    d = s.decoder()
    assert d.step(1000.0)["state"] == "starting"
    s.found = [(0, "fg13")]
    s.started[-1].code = 1
    st = d.step(1002.0)
    assert st["cause"] == "not_found" and st["present"] is False and "serial adsb isn't in the dongle list" in st["detail"]
    assert "plugged in: fg13" in st["detail"] and s.killed == []


def test_unplugged_before_start_has_a_cause_too(tmp_path):
    s = Station(tmp_path)
    s.found = [(0, "fg13")]
    st = s.decoder().step(1000.0)
    assert st["state"] == "unplugged" and st["cause"] == "not_found" and "plugged in: fg13" in st["detail"]


def test_a_kernel_driver_on_the_dongle_is_named(tmp_path):
    s = Station(tmp_path)
    shutil.rmtree(s.sys / "bus" / "usb" / "devices" / "1-1")
    shutil.rmtree(s.sys / "bus" / "usb" / "devices" / "1-1:1.0")
    s.usb("1-1", "adsb", 1, 4, driver="dvb_usb_rtl28xxu")
    s.lines = ["Kernel driver is active, or device is claimed by second instance of librtlsdr."]
    st = s.run_and_exit(s.decoder())
    assert st["cause"] == "busy" and st["holder"] == "the kernel's dvb_usb_rtl28xxu driver" and s.killed == []


def test_busy_with_nobody_visible_names_the_rtl_programs_running(tmp_path):
    s = Station(tmp_path)
    s.process(55, "rtl_tcp", ["rtl_tcp"], "other.service", holds="")
    st = s.run_and_exit(s.decoder())
    assert st["holder"] == "maybe rtl_tcp (pid 55, other.service)" and s.killed == []


@pytest.mark.parametrize("lines, cause, words", [
    (["usb_open error -3", "Please fix the device permissions, e.g. by installing the udev rules file rtl-sdr.rules"],
     "permissions", "no permission to open the dongle"),
    (["readsb: unrecognized option '--bogus'"], "bad_args", "readsb refused its command line"),
    (["readsb: error while loading shared libraries: librtlsdr.so.0: cannot open shared object file"], "library",
     "a library readsb needs is missing"),
    (["what"], "unknown", "readsb exited with code 1"),
])
def test_each_cause_reads_plainly(tmp_path, lines, cause, words):
    s = Station(tmp_path)
    s.lines = lines
    st = s.run_and_exit(s.decoder())
    assert st["cause"] == cause and st["detail"].startswith(words) and s.killed == []
    assert f"readsb: {lines[-1]}"[:60] in st["detail"] or cause != "unknown"


def test_the_station_log_says_a_cause_once(tmp_path):
    s = Station(tmp_path)
    logs: list[str] = []
    d = s.decoder(logs)
    s.run_and_exit(d)
    d.step(1010.0)
    s.started[-1].code = 1
    d.step(1012.0)
    assert sum(1 for m in logs if m.startswith("adsb: decoder down:")) == 1


# ---- to the hub, and shown ---------------------------------------------------------------------------------------------

def _app(decoder):
    row = {"serial": "adsb", "name": "V3", "present": True, "held_by": None, "installed": True, "gain": "auto",
           "bias_tee": False, "decoder": decoder, "forward": None}
    return SimpleNamespace(adsb_sdr="adsb", adsb_row=lambda: row)


def test_the_cause_goes_into_the_stations_health():
    h = H.adsb_health(_app({"state": "down", "detail": "dongle busy (held by rtl_fm ...)", "cause": "busy",
                            "holder": "rtl_fm (pid 812, sizeup-station's FG1 job)", "tail": ["a", "b", "c", "d"],
                            "recovery": "stopped rtl_fm"}))
    assert h["cause"] == "busy" and h["holder"].startswith("rtl_fm") and h["tail"] == ["b", "c", "d"]
    assert h["recovery"] == "stopped rtl_fm" and h["state"] == "down"


def test_an_older_decoder_without_the_fields_still_reports():
    h = H.adsb_health(_app({"state": "down", "detail": "readsb exited with code 1"}))
    assert h["cause"] == "" and h["holder"] == "" and h["tail"] == [] and h["detail"] == "readsb exited with code 1"


def test_the_station_row_carries_the_new_fields():
    src = (ROOT / "sizeup" / "app.py").read_text()
    row = src[src.index("    def adsb_row("):src.index("    def _save_modes(")]
    for k in ('"cause"', '"holder"', '"tail"', '"recovery"'):
        assert k in row
    link = (ROOT / "sizeup" / "cloud" / "aircraftlink.py").read_text()
    assert '"cause": dec.get("cause")' in link and '"holder": dec.get("holder")' in link


def test_the_hub_keeps_the_cause_from_a_new_station_and_takes_an_old_one():
    sf = StationFeed(clock=lambda: 1000.0)
    sf.ingest({"epoch": "e", "seq": 0, "full": True, "ac": [], "gone": [],
               "rx": {"sdr": "adsb", "state": "down", "detail": "x", "cause": "busy", "holder": "rtl_fm", "new": 1}})
    assert sf.rx["cause"] == "busy" and sf.rx["holder"] == "rtl_fm" and "new" not in sf.rx
    sf.ingest({"epoch": "e", "seq": 1, "full": False, "ac": [], "gone": [], "rx": {"sdr": "adsb", "state": "ok"}})
    assert sf.rx == {"sdr": "adsb", "state": "ok"}
    # a new station to an older hub: everything an older hub reads is still there, the same
    old_keys = ("sdr", "name", "state", "detail", "held_by", "present", "messages_per_s", "aircraft", "last_message_at",
                "gain", "bias_tee", "source")
    assert set(old_keys) <= set(A.RX_KEYS)


def test_the_hub_logs_the_decoders_cause_once_per_change_and_when_it_is_back():
    logs: list[str] = []
    mon = SimpleNamespace(_adsb_said=None, log=logs.append)
    down = {"name": "V3", "state": "down", "cause": "busy", "holder": "rtl_fm (pid 812)", "tail": ["usb_claim_interface error -6"],
            "recovery": "stopped rtl_fm (pid 812)"}
    for _ in range(5):
        H.StationHealth._log_adsb(mon, down)
    assert len(logs) == 1 and "busy, held by rtl_fm (pid 812)" in logs[0] and "usb_claim_interface error -6" in logs[0]
    H.StationHealth._log_adsb(mon, dict(down, holder="", cause="not_found"))
    assert len(logs) == 2 and "not_found" in logs[1]
    H.StationHealth._log_adsb(mon, {"name": "V3", "state": "ok"})
    H.StationHealth._log_adsb(mon, {"name": "V3", "state": "ok"})
    assert len(logs) == 3 and "back" in logs[2]
    H.StationHealth._log_adsb(mon, {"name": "V3", "state": "down", "detail": "readsb exited with code 1"})   # an older station
    H.StationHealth._log_adsb(mon, None)
    assert len(logs) == 3
    src = (ROOT / "sizeup" / "cloud" / "health.py").read_text()
    assert 'self._log_adsb(m.get("adsb"))' in src


def test_the_receivers_page_and_health_show_the_cause():
    rc = (ROOT / "sizeup" / "web" / "static" / "radio-controls.js").read_text()
    assert "d.holder" in rc and "d.tail" in rc and "readsb: ${d.tail.join" in rc
    assert "d.cause, d.holder, d.tail" in rc                           # redrawn when they change
    assert "unplugged? see journalctl" not in (ROOT / "sizeup" / "radio" / "adsbd.py").read_text()
    hj = (ROOT / "sizeup" / "web" / "static" / "health.js").read_text()
    assert '["Cause"' in hj and '["readsb said"' in hj and "ad.holder ||" in hj


# ---- the hub's log: once, then every 5 minutes, then "back" ------------------------------------------------------------

def test_no_word_from_the_receiver_is_logged_once_then_every_5_minutes_then_back():
    clock = Clock(T3 + 0.5)
    logs: list[str] = []
    ly = AircraftLayer(feeds=("adsblol", "adsbfi"), adsblol_url=LOL_URL, adsbfi_url=FI_URL,
                       registry_url="https://faa.example/r.zip", fetch=Feeds(), clock=clock, log=logs.append,
                       station_feed=True)
    sf = ly.station_feed()
    sf.ingest({"epoch": "e", "seq": 0, "full": True, "ac": [], "gone": []})
    t0 = clock.t
    clock.t += A.RECEIVER_KEEP_S + 1
    for _ in range(int(A.RECEIVER_LOG_EVERY_S // 5) - 2):             # every 5 s for just under 5 min
        assert ly._read(sf, clock.t, 5.0) is False
        clock.t += 5.0
    said = [m for m in logs if "our ADS-B receiver" in m]
    assert len(said) == 1 and "no word from the station's receiver" in said[0]
    clock.t += 15.0
    ly._read(sf, clock.t, 5.0)
    said = [m for m in logs if "our ADS-B receiver" in m]
    assert len(said) == 2 and "failing since" in said[1] and "every 5 min" in said[1]
    sf.last = clock.t                                                   # the station is back
    assert ly._read(sf, clock.t, 5.0) is True
    assert logs[-1].startswith("aircraft: our ADS-B receiver back (after ") and t0 < clock.t
    sf.last = clock.t - A.RECEIVER_KEEP_S - 5                          # and gone again: said again at once
    ly._read(sf, clock.t, 5.0)
    assert "no word from the station's receiver" in logs[-1]


def test_the_api_feeds_are_logged_as_before():
    src = (ROOT / "sizeup" / "aircraft.py").read_text()
    block = src[src.index("            if not f.local:"):src.index("        fresh = sum(")]
    assert 'self.log(f"aircraft: {FEEDS[f.key][\'label\']}: {why}; resting {wait:.0f} s")' in block.splitlines()[1]


# ---- the station's job manager: never a program on the ADS-B dongle (the root cause) -----------------------------------

def _state(tmp_path) -> dict:
    (tmp_path / "state").mkdir(exist_ok=True)
    return {"cad_db": str(tmp_path / "state" / "cad.db")}


def test_reserving_a_dongle_stops_whatever_runs_on_it_even_when_its_job_was_not_recorded(rig, tmp_path):
    plugged, logs, make = rig
    plugged[:] = [DONGLES[2]]                                          # "adsb" only: FG1 goes there
    app = make(**_state(tmp_path))
    r = app._receiver("FG1")
    assert app._program_on("FG1") == "adsb"
    app.assign["adsb"] = "Off"                                         # its job lost track of (the #262 station)
    assert app.set_adsb_receiver("adsb") is None
    assert r._stop.is_set() and app._program_on("FG1") is None
    assert any("the ADS-B receiver's dongle: stopped" in m for m in logs)


def test_a_reserved_dongle_never_gets_a_job(rig, tmp_path):
    plugged, logs, make = rig
    plugged[:] = [DONGLES[2], DONGLES[3]]
    app = make(**_state(tmp_path))
    assert app.set_adsb_receiver("adsb") is None
    assert app._start_mode("FG1", "adsb") is False
    assert any("reserved for the ADS-B receiver: FG1 not started" in m for m in logs)
    assert app._program_on("FG1") != "adsb"


def test_a_job_moved_stops_its_old_program_even_off_the_books(rig):
    plugged, logs, make = rig
    plugged[:] = [DONGLES[2]]
    app = make()
    old = app._receiver("FG1")
    assert app._program_on("FG1") == "adsb"
    app.assign["adsb"] = "Off"                                         # nothing says FG1 runs there any more
    plugged.append(DONGLES[3])
    app._sdr_arrivals()
    app.set_mode("FG1", "spare")
    assert old._stop.is_set()                                          # not left running with nothing to reach it
    assert app._program_on("FG1") == "spare"


def test_the_sdr_watch_stops_a_program_found_on_the_adsb_dongle(rig):
    plugged, logs, make = rig
    plugged[:] = [DONGLES[2]]
    app = make()
    r = app._receiver("FG1")
    app.adsb_sdr = "adsb"                                              # reserved behind its back
    app._sdr_arrivals()
    assert r._stop.is_set()


# ---- the kernel's usbfs buffer limit (#269) ------------------------------------------------------------------------------

# What librtlsdr (rtl-sdr-blog) and readsb v3.16.21 print when usbfs_memory_mb runs out, a few seconds in
USBFS_LINES = ["rtlsdr: using device #1: Generic RTL2832U OEM (Realtek, RTL2838UHIDIR, SN adsb)",
               "Failed to submit transfer 15",
               "Please increase your allowed usbfs buffer size with the following command:",
               "echo 0 > /sys/module/usbcore/parameters/usbfs_memory_mb",
               "FATAL: rtlsdr_read_async returned unexpectedly, probably lost the USB device, bailing out"]


def usbfs_file(s: Station, value: str | None = "16") -> Path:
    f = s.sys / adsbd.USBFS
    if value is not None:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(value + "\n")
    return f


def test_the_usbfs_hint_is_its_own_cause_before_lost():
    assert adsbd.classify(USBFS_LINES)[0] == "usbfs"
    assert adsbd.classify(USBFS_LINES[-1:])[0] == "lost"          # the FATAL line alone: still "lost"
    assert "usbfs_memory_mb" in adsbd.CAUSE_WORDS["usbfs"]


def test_set_usbfs_lifts_the_limit_and_tolerates_a_missing_or_read_only_file(tmp_path, capsys):
    s = Station(tmp_path)
    assert adsbd.usbfs_mb(str(s.sys)) is None
    assert adsbd.set_usbfs(str(s.sys))[2].startswith("no ")           # no usbcore: said, not raised
    assert adsbd.usbfs_main(str(s.sys)) == 0
    f = usbfs_file(s, "16")
    assert adsbd.set_usbfs(str(s.sys)) == (16, 0, "") and f.read_text().strip() == "0"
    assert adsbd.usbfs_main(str(s.sys)) == 0 and "0 (no limit)" in capsys.readouterr().out
    f.write_text("16\n")
    f.chmod(0o444)
    try:
        if os.access(f, os.W_OK):
            pytest.skip("running as root: a read-only file is still writable")
        before, after, err = adsbd.set_usbfs(str(s.sys))
        assert (before, after) == (16, 16) and err.startswith("can't write it")
        assert adsbd.usbfs_main(str(s.sys)) == 0 and "can't write it" in capsys.readouterr().out
    finally:
        f.chmod(0o644)
    f.write_text("garbage\n")
    assert adsbd.usbfs_mb(str(s.sys)) is None


def test_readsb_dying_on_the_usbfs_limit_says_so_lifts_it_when_it_can_then_cuts_its_transfers(tmp_path):
    s = Station(tmp_path)
    f = usbfs_file(s, "16")
    f.chmod(0o444)                                     # the sizeup user can't write it
    if os.access(f, os.W_OK):
        f.chmod(0o644)
        pytest.skip("running as root")
    s.lines = USBFS_LINES
    logs: list[str] = []
    d = s.decoder(logs)
    try:
        st = s.run_and_exit(d)
        assert st["state"] == "down" and st["cause"] == "usbfs" and st["usbfs_mb"] == 16
        assert st["detail"].startswith("the kernel's USB transfer memory ran out (usbfs_memory_mb)")
        assert "usbfs_memory_mb is 16" in st["detail"] and "smaller USB transfers after 2" in st["detail"]
        assert "--sdr-buffer-size" not in s.started[-1].argv and st["buffer_kib"] is None
        assert s.killed == []
        d.next_start = 0                               # the second time: smaller transfers from now on
        assert d.step(1010.0)["state"] == "starting"
        s.started[-1].code = 1
        st = d.step(1012.0)
        assert st["cause"] == "usbfs" and "readsb now uses 32 KiB USB transfers" in st["detail"]
        d.next_start = 0
        st = d.step(1020.0)
        argv = s.started[-1].argv
        assert argv[argv.index("--sdr-buffer-size") + 1] == str(adsbd.SMALL_BUF_KIB) and st["buffer_kib"] == 32
        assert any("32 KiB transfers from now on" in m for m in logs)
    finally:
        f.chmod(0o644)


def test_the_decoder_lifts_the_limit_before_readsb_when_it_can_and_reports_it(tmp_path):
    s = Station(tmp_path)
    f = usbfs_file(s, "16")                            # writable here (as root would find it)
    logs: list[str] = []
    d = s.decoder(logs)
    st = d.step(1000.0)
    assert st["state"] == "starting" and st["usbfs_mb"] == 0 and f.read_text().strip() == "0"
    assert any("usbfs_memory_mb 16 -> 0" in m for m in logs)
    assert d.step(1002.0)["usbfs_mb"] == 0
    (tmp_path / "other").mkdir()
    s2 = Station(tmp_path / "other")                   # no such file: None, and readsb starts all the same
    assert s2.decoder().step(1000.0)["usbfs_mb"] is None


def test_the_usbfs_value_reaches_settings_health_and_the_hub():
    app = SimpleNamespace(adsb_sdr="adsb", adsb_row=lambda: {"serial": "adsb", "installed": True, "decoder": {
        "state": "down", "detail": "x", "cause": "usbfs", "usbfs_mb": 16, "buffer_kib": 32}})
    h = H.adsb_health(app)
    assert h["usbfs_mb"] == 16 and h["buffer_kib"] == 32 and h["cause"] == "usbfs"
    assert "usbfs_mb" in A.RX_KEYS
    app_py = (ROOT / "sizeup" / "app.py").read_text()
    assert '"usbfs_mb", "buffer_kib")} if st else None' in app_py
    js = (ROOT / "sizeup" / "web" / "static" / "health.js").read_text()
    assert "usbfs_memory_mb" in js and "ad.usbfs_mb" in js
    assert "d.usbfs_mb" in (ROOT / "sizeup" / "web" / "static" / "radio-controls.js").read_text()


def test_the_station_sets_usbfs_to_0_at_install_boot_and_before_readsb():
    st = ROOT / "deploy" / "station"
    unit = (st / "systemd" / "sizeup-adsb.service").read_text()
    assert "ExecStartPre=-+/usr/bin/python3 -I /usr/local/lib/sizeup/adsbd.py usbfs" in unit
    assert unit.index("ExecStartPre=") < unit.index("ExecStart=/usr/bin/python3")
    boot = (st / "systemd" / "sizeup-usbfs.service").read_text()
    for want in ("Type=oneshot", "ExecStart=-/usr/bin/python3 -I /usr/local/lib/sizeup/adsbd.py usbfs",
                 "Before=sizeup-station.service sizeup-adsb.service sizeup-trunk-recorder.service",
                 "WantedBy=multi-user.target"):
        assert want in boot, want
    assert "User=" not in boot                        # root: only root may write /sys
    sh = (st / "install-linux.sh").read_text()
    for want in ("for t in sizeup-usbfs.service sizeup-station.service", "systemctl enable --quiet sizeup-usbfs.service",
                 "systemctl restart sizeup-usbfs.service ||", "options usbcore usbfs_memory_mb=0",
                 "usbfs=/sys/module/usbcore/parameters/usbfs_memory_mb", '{ echo 0 > "$usbfs"; } 2>/dev/null',
                 "/etc/systemd/system/sizeup-usbfs.service /etc/systemd/system/sizeup-health.timer"):
        assert want in sh, want
    cli = (st / "sizeup-station").read_text()
    assert "usbfs_memory_mb" in cli and "sizeup-usbfs" in cli


# ---- a wedged dongle (#315): power / USB, and a longer wait when it keeps happening -----------------------------------

def test_wedged_wins_over_the_lost_line_that_may_follow_it():
    assert adsbd.classify(WEDGED + ["FATAL: rtlsdr_read_async returned unexpectedly, probably lost the USB device"])[0] \
        == "wedged"
    assert adsbd.classify(USBFS_LINES + WEDGED)[0] == "usbfs"           # the usbfs limit is the cause, the wedge its sign


def test_a_wedge_names_power_usb_and_the_dongle_and_the_bias_tee_only_when_it_is_on(tmp_path):
    s = Station(tmp_path)
    s.lines = WEDGED
    st = s.run_and_exit(s.decoder())
    assert st["state"] == "down" and st["cause"] == "wedged" and st["code"] == 1 and s.killed == []
    assert st["detail"].startswith("SDR wedged (stalled): check power; avoid USB extensions/unpowered hubs; "
                                   "try another dongle · readsb: <3>SDR wedged, exiting!")
    assert "bias-tee" not in st["detail"] and "wedges in 1 h" not in st["detail"]
    (tmp_path / "on").mkdir()
    s2 = Station(tmp_path / "on")
    s2.lines = WEDGED
    d = s2.decoder()
    conf = Path(d.config)
    conf.write_text(conf.read_text().replace('sdr = "adsb"', 'sdr = "adsb"\nbias_tee = true'))
    st = s2.run_and_exit(d)
    assert st["detail"].startswith("SDR wedged (stalled): check power (bias-tee on: the LNA draws from this port); ")


def wedge(s, d, at):
    d.next_start = 0
    assert d.step(at)["state"] == "starting"
    s.started[-1].code = 1
    return d.step(at + 17)


def test_repeated_wedges_back_off_5_then_15_minutes_and_say_so(tmp_path):
    s = Station(tmp_path)
    s.lines = WEDGED
    logs: list[str] = []
    d = s.decoder(logs)
    for i, at in enumerate((1000.0, 1100.0)):
        st = wedge(s, d, at)
        assert d.next_start - (at + 17) <= adsbd.BACKOFF_S[1] and "wedges in 1 h" not in st["detail"]
    st = wedge(s, d, 1200.0)
    assert d.next_start == 1217.0 + 300 and "; 3 wedges in 1 h: next try in 5 min · readsb:" in st["detail"]
    assert any("3 wedges in the last hour: waiting 5 min" in m for m in logs)
    st = d.step(1300.0)                                 # still waiting: says how long
    assert st["state"] == "down" and st["detail"].endswith("; retry in 217 s") and len(s.started) == 3
    st = wedge(s, d, 1600.0)
    assert d.next_start == 1617.0 + 900 and "4 wedges in 1 h: next try in 15 min" in st["detail"]
    h = H.adsb_health(SimpleNamespace(adsb_sdr="adsb", adsb_row=lambda: {"serial": "adsb", "installed": True,
                                                                          "decoder": st}))
    assert h["cause"] == "wedged" and "15 min" in h["detail"] and len(h["detail"]) <= 200


def test_wedges_older_than_an_hour_drop_out_of_the_count(tmp_path):
    s = Station(tmp_path)
    s.lines = WEDGED
    d = s.decoder()
    for at in (1000.0, 1100.0):
        wedge(s, d, at)
    st = wedge(s, d, 1000.0 + adsbd.WEDGE_WINDOW_S + 50)
    assert len(d.wedges) == 2 and "wedges in 1 h" not in st["detail"]


def test_a_ten_minute_run_forgets_the_wedges_and_another_cause_keeps_the_usual_backoff(tmp_path):
    s = Station(tmp_path)
    s.lines = WEDGED
    logs: list[str] = []
    d = s.decoder(logs)
    for at in (1000.0, 1100.0):
        wedge(s, d, at)
    for start, up, left in ((2000.0, adsbd.WEDGE_RESET_S - 1, 2), (3000.0, adsbd.WEDGE_RESET_S, 0)):
        d.next_start = 0
        assert d.step(start)["state"] == "starting"
        d.step(start + up)                             # (then restarted as stuck: no aircraft.json here)
        assert len(d.wedges) == left
    assert any("wedge count reset" in m for m in logs)
    s.lines = ["what"]
    st = wedge(s, d, 5000.0)
    assert st["cause"] == "unknown" and d.next_start - 5017.0 <= adsbd.BACKOFF_S[1]


def test_a_wedge_reaches_the_hub_alert():
    from tests.test_health import Rig, station_status
    detail = "SDR wedged (stalled): check power; avoid USB extensions/unpowered hubs; try another dongle"
    h = H.adsb_health(SimpleNamespace(adsb_sdr="adsb", adsb_row=lambda: {"serial": "adsb", "name": "LNA", "installed": True,
                                                                          "decoder": {"state": "down", "detail": detail,
                                                                                      "cause": "wedged"}}))
    r = Rig()
    st = station_status()
    st["health"]["adsb"] = h
    r.at(0, status=st)
    r.at(120, status=st)
    (a,) = [x for x in r.h.alerts() if x["key"] == "adsb"]
    assert "SDR wedged (stalled): check power" in a["text"]
