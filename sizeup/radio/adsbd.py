"""The station's ADS-B decoder (GitHub #235): readsb on the ONE dongle named for ADS-B, and nothing else.

The ADS-B dongle has a 1090 MHz filter and an LNA in front of it (user, #235: "this one we will have to be specific
about and cant auto assign because itll have a filter LNA in line"). So it is chosen by its exact USB serial only:
[aircraft.receiver] sdr in /etc/sizeup/config.toml, or Settings > Radios (an admin, with a confirm), which sizeup-station
keeps in OVERRIDE. sizeup's own radios and auto-assign never take it (sizeup/app.py: it is ignored there), and this
never opens any other dongle. With no dongle named, nothing runs and no dongle is touched.

    sizeup-adsb.service (deploy/station/systemd): the sizeup user runs, with the system python3 (stdlib only, like
    sizeup/cloud/autoupdate.py; installed as /usr/local/lib/sizeup/adsbd.py by install-linux.sh):
        python3 -I adsbd.py run        supervise readsb (the service)
        python3 -I adsbd.py print      the readsb command line it would run now, or why none
        python3 -I adsbd.py status     the last status it wrote
        python3 -I adsbd.py usbfs      as root: the kernel's usbfs buffer limit to 0 (unlimited), when it can be (#269);
                                       sizeup-adsb's ExecStartPre (+, root) and sizeup-usbfs.service at boot run it

What runs: readsb (wiedehopf, pinned and built by install-linux.sh, /opt/sizeup/readsb/bin/readsb) with
    --device-type rtlsdr --device <index>   the dongle's CURRENT index, looked up by its exact serial right before each
                                            start (sizeup/radio/sdr.py's rule: readsb also matches a serial by prefix
                                            or suffix, so a serial is never passed)
    --gain auto | <dB>                      [aircraft.receiver] gain; auto by default (shortfinal, 2026-10-02: an LNA
                                            in front makes a fixed high gain overload the dongle)
    --enable-biastee                        only with [aircraft.receiver] bias_tee = true: 4.5 V up the coax for an LNA
                                            that is powered that way. Off by default: an LNA with its own supply, or a
                                            plain filter, must not get it
    --lat/--lon HPN's reference point       for decoding surface positions (not the station's own location)
    --write-json JSON_DIR --write-json-every 1   aircraft.json every second, read by the station's forwarder
                                            (sizeup/cloud/aircraftlink.py) and sent on to the hub
No network ports: readsb's networking stays off.

The shared dongle claims (~/.local/state/rtl-claims, sizeup/radio/claims.py, agreed with shortfinal): while readsb
runs, this process holds the dongle's claim (app "sizeup", mode "adsb"). If another app (shortfinal) holds it, nothing
starts: the status says who has it, and it is retried every few seconds until they let it go. It never fights for it.

Supervision (shortfinal's core/supervisor.py, simplified): a readsb that exits is restarted with backoff (2 s doubling
to 30 s); one whose aircraft.json stops updating, or whose dongle stops delivering samples, is restarted at once; one
that decodes nothing for NO_SIGNAL_S says "no signal" (an antenna or LNA fault, or a quiet sky) and is restarted on a
slow schedule. A change of the dongle, gain or bias-tee (the config, or Settings) restarts it with the new settings.

Two dongles with the dongle's serial (#242): nothing starts (state "ambiguous", the fix in its detail) until one is
renamed: readsb never guesses between them.

The status (STATUS, every STEP_S, atomically): {at, state: off | refused | missing | held | unplugged | ambiguous |
starting | ok | no_signal | down, detail, sdr, source: config | settings, gain, bias_tee, pid, index, since, restarts, held_by,
messages_per_s, aircraft, with_pos, last_message_at, json, argv, and when it failed: cause, holder, tail, recovery, code}.

Why it failed (#262; the station can't always be reached for journalctl): readsb's output is kept (the last
TAIL_LINES lines of each run, and still logged). When it exits, the cause is read from its words (classify(): busy,
not_found, permissions, bad_args, library, lost, unknown) and looked into, read-only: is the serial in librtlsdr's list,
at which index; who has its USB device open (/proc/*/fd where readable, the shared claims, a kernel driver). If that is
sizeup's OWN leftover job (an rtl_fm or trunk-recorder under sizeup-station / sizeup-trunk-recorder, this user's), it is
stopped and readsb retried at once: a reserved dongle never runs another job. Another app's process never is: the
status says "held by <it>". cause, holder and the last DETAIL_LINES lines (tail) go to Settings and the hub. sizeup-station reads it for Settings > Radios and the
station health it sends the hub (sizeup/cloud/health.py).

The USB buffer limit (#269): the kernel gives all usbfs users together usbfs_memory_mb (16 MB by default) for their
transfers, and readsb (16 x 128 KiB), every rtl_fm and trunk-recorder on one host share it. With about six dongles it
runs out: librtlsdr says "Failed to submit transfer" and "echo 0 > /sys/module/usbcore/parameters/usbfs_memory_mb",
readsb "rtlsdr_read_async returned unexpectedly" a few seconds in (cause "usbfs"). The station sets it to 0 (no limit)
as root: install-linux.sh, sizeup-usbfs.service at boot, and sizeup-adsb's ExecStartPre (WSL2 included, where usbcore
is built in and modprobe.d can't). This process (the sizeup user) can't write it: it reports it (status usbfs_mb), and
after SMALL_AFTER such failures in a row runs readsb with smaller USB transfers (--sdr-buffer-size SMALL_BUF_KIB)."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections import deque
from pathlib import Path

CONFIG = "/etc/sizeup/config.toml"
OVERRIDE = "/var/lib/sizeup/state/adsb_receiver.json"      # Settings > Radios (sizeup/app.py set_adsb_receiver)
RUN_DIR = "/run/sizeup-adsb"                               # the service's RuntimeDirectory (tmpfs)
JSON_DIR = RUN_DIR + "/json"                               # readsb's --write-json
AIRCRAFT_JSON = JSON_DIR + "/aircraft.json"
STATUS = RUN_DIR + "/decoder.json"
READSB = "/opt/sizeup/readsb/bin/readsb"
LIBRTLSDR = ("/opt/sizeup/rtl-sdr/lib/librtlsdr.so.0", "librtlsdr.so.0", "/opt/homebrew/lib/librtlsdr.dylib")
REF = (41.06695, -73.70757)       # HPN's airport reference point (sizeup/aircraft.py ARP), for surface positions
APP, MODE = "sizeup", "adsb"
SERIAL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
GAINS = (0.0, 60.0)               # an R820T's range in dB (readsb rounds to the nearest step)

STEP_S = 2.0
GRACE_S = 20.0                    # don't judge a fresh start
STALE_S = 10.0                    # aircraft.json not rewritten this long: readsb is stuck, restart
STALL_S = 60.0                    # the dongle's sample count not moving this long: deaf, restart
NO_SIGNAL_S = 120.0               # nothing decoded this long: "no signal"
NO_SIGNAL_RESTART_S = (300.0, 1800.0)
BACKOFF_S = (2.0, 30.0)
USBFS = "module/usbcore/parameters/usbfs_memory_mb"        # under /sys: the kernel's usbfs buffer limit, MB (0: none)
SMALL_AFTER = 2                   # this many usbfs / lost-USB exits in a row: readsb with smaller USB transfers
SMALL_BUF_KIB = 32                # readsb --sdr-buffer-size (KiB; its default 128): 16 x 32 KiB in flight, not 2 MiB
RATE_WINDOW_S = 60.0              # messages / s over the last minute


def gain_problem(gain) -> str:
    """"" for a gain readsb takes ("auto", or dB in GAINS), else what is wrong."""
    if isinstance(gain, str) and gain.strip().lower() == "auto":
        return ""
    try:
        g = float(gain)
    except (TypeError, ValueError):
        return 'auto (the default) or a number of dB, e.g. 40'
    return "" if GAINS[0] <= g <= GAINS[1] else f"{GAINS[0]:g} to {GAINS[1]:g} dB, or auto"


def serial_problem(serial: str) -> str:
    """"" for a serial the decoder may use, else why not (sizeup/config.py says the same)."""
    if not serial:
        return ""
    if len(serial) > 32 or not set(serial) <= SERIAL_CHARS:
        return "the dongle's exact USB serial: letters, digits, . _ - (at most 32)"
    if serial.isdigit():
        return ("an all-digit serial is read as a device index by readsb and the rtl tools: give the dongle a name "
                "first (rtl_eeprom -d <index> -s adsb1090)")
    return ""


# -- what to run -----------------------------------------------------------------------------------------------------


def settings(config: str = CONFIG, override: str = OVERRIDE) -> dict:
    """{sdr, source, gain, bias_tee, problem}: the config's [aircraft.receiver], the dongle as Settings named it
    (OVERRIDE {"sdr": ...}, "" = none) over the config's."""
    try:
        with open(config, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        data, err = {}, f"{config}: {type(e).__name__}"
    else:
        err = ""
    ac = data.get("aircraft") if isinstance(data.get("aircraft"), dict) else {}
    rc = ac.get("receiver") if isinstance(ac.get("receiver"), dict) else {}
    sdr, source = str(rc.get("sdr") or "").strip(), "config" if rc.get("sdr") else ""
    try:
        ov = json.loads(Path(override).read_text())
    except (OSError, ValueError):
        ov = None
    if isinstance(ov, dict) and isinstance(ov.get("sdr"), str):
        sdr, source = ov["sdr"].strip(), "settings"
    gain = rc.get("gain", "auto")
    gain = "auto" if isinstance(gain, bool) or gain_problem(gain) or str(gain).strip().lower() == "auto" else f"{float(gain):g}"
    bias = rc.get("bias_tee", False) is True
    problem = serial_problem(sdr) or (f"the config can't be read ({err})" if err and not sdr else "")
    return {"sdr": sdr, "source": source, "gain": gain, "bias_tee": bias, "problem": problem}


def readsb_argv(binary: str, index: int, gain: str = "auto", bias_tee: bool = False, json_dir: str = JSON_DIR,
                lat: float = REF[0], lon: float = REF[1], buffer_kib: int | None = None) -> list[str]:
    """readsb on device `index` (resolved from the exact serial just before), aircraft.json every second; buffer_kib:
    its USB transfer size (the fallback when the usbfs limit keeps running out, #269), else readsb's own 128."""
    argv = [binary, "--device-type", "rtlsdr", "--device", str(int(index)), "--gain", str(gain),
            "--lat", f"{lat:.5f}", "--lon", f"{lon:.5f}", "--write-json", json_dir, "--write-json-every", "1"]
    if bias_tee:
        argv.append("--enable-biastee")
    if buffer_kib:
        argv += ["--sdr-buffer-size", str(int(buffer_kib))]
    return argv


# -- the kernel's usbfs buffer limit (#269) ---------------------------------------------------------------------------


def usbfs_mb(sys_root: str = "/sys") -> int | None:
    """usbfs_memory_mb now (0: no limit); None when there is no such file (no usbcore) or it can't be read."""
    try:
        return int((Path(sys_root) / USBFS).read_text().strip())
    except (OSError, ValueError):
        return None


def set_usbfs(sys_root: str = "/sys") -> tuple[int | None, int | None, str]:
    """usbfs_memory_mb to 0 when it isn't already: (before, after, what went wrong or ""). Never raises: a missing or
    read-only file (no usbcore, a container, not root) is said, not an error."""
    before = usbfs_mb(sys_root)
    if before is None:
        return None, None, f"no {Path(sys_root) / USBFS}"
    if before == 0:
        return 0, 0, ""
    try:
        (Path(sys_root) / USBFS).write_text("0\n")
    except OSError as e:
        return before, usbfs_mb(sys_root), f"can't write it ({e.strerror or type(e).__name__})"
    after = usbfs_mb(sys_root)
    return before, after, "" if after == 0 else f"still {after} after writing 0"


def usbfs_main(sys_root: str = "/sys") -> int:
    """adsbd.py usbfs (root: install-linux.sh, sizeup-usbfs.service, sizeup-adsb's ExecStartPre). Always 0: a station
    whose limit can't be lifted still runs (readsb falls back to smaller transfers)."""
    before, after, err = set_usbfs(sys_root)
    if err:
        print(f"usbfs_memory_mb: {err}" + (f" (it is {after})" if after is not None and "still" not in err else ""))
    elif before != after:
        print(f"usbfs_memory_mb: {before} -> 0 (no limit on USB transfer memory, #269)")
    else:
        print("usbfs_memory_mb: 0 (no limit)")
    return 0


# -- the dongles (librtlsdr's USB strings: nothing is opened) -----------------------------------------------------------

_lib = None


def _load():
    global _lib
    if _lib is None:
        for cand in LIBRTLSDR:
            try:
                _lib = ctypes.CDLL(cand)
                break
            except OSError:
                continue
        else:
            _lib = False
    return _lib or None


def devices() -> list[tuple[int, str]] | None:
    """[(index, serial)] plugged in now; None when librtlsdr can't be loaded."""
    lib = _load()
    if lib is None:
        return None
    out = []
    for i in range(int(lib.rtlsdr_get_device_count())):
        m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
        if lib.rtlsdr_get_device_usb_strings(i, m, p, s) == 0:
            out.append((i, s.value.decode(errors="replace")))
    return out


def duplicate_text(serial: str, indexes: list[int]) -> str:
    """sizeup/radio/sdr.py duplicate_text()'s words (this file is stdlib only, installed on its own)."""
    words = {2: "Two", 3: "Three", 4: "Four"}
    ix = [str(i) for i in indexes]
    listed = ix[0] if len(ix) == 1 else ", ".join(ix[:-1]) + " and " + ix[-1]
    return (f"{words.get(len(ix), str(len(ix)))} dongles share serial {serial} (indexes {listed}): sizeup can't tell "
            f"them apart. Fix: sizeup-station sdr-serial {ix[-1]} <new-serial>")


def resolve(serial: str, found: list[tuple[int, str]] | None) -> int | None:
    """The index of the one dongle whose serial is exactly `serial`; None if it isn't there (or twice)."""
    hits = [i for i, s in (found or []) if s == serial]
    return hits[0] if len(hits) == 1 else None


# -- the shared claims (sizeup/radio/claims.py's format; stdlib only here) ------------------------------------------------


def claims_dir() -> Path:
    return Path.home() / ".local" / "state" / "rtl-claims"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return False


def holder(serial: str, d: Path | None = None) -> dict | None:
    """The live claim on this dongle (any app), or None. A stale one (its pid gone) is removed."""
    p = (d or claims_dir()) / f"{serial}.json"
    try:
        c = json.loads(p.read_text())
        pid = int(c["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if _alive(pid):
        return c
    try:
        p.unlink()
    except OSError:
        pass
    return None


def other(serial: str, d: Path | None = None) -> dict | None:
    """A live claim by anyone but this process."""
    c = holder(serial, d)
    return c if c and (c.get("app") != APP or int(c.get("pid", -1)) != os.getpid()) else None


def claim(serial: str, d: Path | None = None) -> bool:
    if other(serial, d):
        return False
    d = d or claims_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{serial}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"app": APP, "pid": os.getpid(), "mode": MODE, "since": time.time()}))
        os.replace(tmp, d / f"{serial}.json")
    except OSError:
        pass                          # can't record it: still run (the dongle itself is the real lock)
    return True


def release(serial: str, d: Path | None = None) -> None:
    c = holder(serial, d)
    if c and c.get("app") == APP and int(c.get("pid", -1)) == os.getpid():
        try:
            ((d or claims_dir()) / f"{serial}.json").unlink()
        except OSError:
            pass


# -- health from readsb's own output ---------------------------------------------------------------------------------------


class Output:
    """readsb's aircraft.json (rewritten every second: "now", "messages" = the total decoded) and stats.json (the
    dongle's samples): messages / s over the last minute, aircraft (with a position), the last message's time."""

    def __init__(self, json_dir: str = JSON_DIR):
        self.dir = Path(json_dir)
        self.reset()

    def reset(self) -> None:
        self.counts: list[tuple[float, int]] = []          # (time, messages total)
        self.last_message_at: float | None = None
        self.samples: tuple[float, float] | None = None    # (value, when it last moved)

    def read(self, now: float) -> dict:
        try:
            ac = json.loads((self.dir / "aircraft.json").read_text())
        except (OSError, ValueError) as e:
            return {"ok": False, "why": f"no aircraft.json yet ({type(e).__name__})"}
        at = float(ac.get("now") or 0)
        msgs = int(ac.get("messages") or 0)
        planes = [a for a in ac.get("aircraft") or [] if isinstance(a, dict)]
        if not self.counts or msgs != self.counts[-1][1]:
            if self.counts and msgs > self.counts[-1][1]:
                self.last_message_at = at
            self.counts.append((at, msgs))
        self.counts = [c for c in self.counts if at - c[0] <= RATE_WINDOW_S] or self.counts[-1:]
        rate = None
        if len(self.counts) >= 2 and self.counts[-1][0] > self.counts[0][0]:
            rate = max(0.0, (self.counts[-1][1] - self.counts[0][1]) / (self.counts[-1][0] - self.counts[0][0]))
        try:
            st = json.loads((self.dir / "stats.json").read_text())
            samples = float(((st.get("total") or {}).get("local") or {}).get("samples_processed") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            samples = None
        if samples is not None and (self.samples is None or samples != self.samples[0]):
            self.samples = (samples, now)
        return {"ok": True, "age": now - at, "messages_per_s": None if rate is None else round(rate, 1),
                "aircraft": len(planes), "with_pos": sum(1 for a in planes if "lat" in a and "lon" in a),
                "last_message_at": self.last_message_at,
                "samples_still_s": None if self.samples is None else now - self.samples[1]}


# -- why readsb stopped (#262): its own words, then a read-only look at the dongle ------------------------------------------

TAIL_LINES = 20                   # readsb's last lines kept per run
DETAIL_LINES = 3                  # sent on (status "tail")
LINE_MAX = 160
# What readsb (sdr_rtlsdr.c, v3.16.21) and librtlsdr print, lower case: the first rule that matches is the cause.
CAUSE_RULES = (
    ("library", ("error while loading shared libraries", "cannot open shared object file")),
    ("permissions", ("libusb_error_access", "usb_open error -3", "please fix the device permissions", "permission denied")),
    ("busy", ("usb_claim_interface error -6", "resource busy", "libusb_error_busy", "claimed by second instance",
              "kernel driver is active")),
    ("not_found", ("no supported devices found", "no device matching", "no matching devices", "usb_open error -4",
                   "libusb_error_no_device", "error querying device")),
    # librtlsdr's "Failed to submit transfer N / Please increase your allowed usbfs buffer size ... echo 0 >
    # /sys/module/usbcore/parameters/usbfs_memory_mb", then readsb's "lost the USB device" (#269): the limit, not the dongle
    ("usbfs", ("usbfs_memory_mb", "allowed usbfs buffer size", "failed to submit transfer")),
    ("lost", ("lost the usb device", "rtlsdr_read_async returned unexpectedly")),
    ("bad_args", ("unrecognized option", "invalid option", "requires an argument", "--help' for more", "usage: readsb",
                  "unknown device type")),
)
CAUSE_WORDS = {"busy": "dongle busy", "not_found": "dongle not found", "permissions": "no permission to open the dongle",
               "bad_args": "readsb refused its command line", "library": "a library readsb needs is missing",
               "lost": "the dongle went away mid-run (USB)",
               "usbfs": "the kernel's USB transfer memory ran out (usbfs_memory_mb)", "unknown": "readsb failed"}
# sizeup's own programs that open a dongle, and the services they run under: the only processes this ever stops, and
# only when one holds the ADS-B dongle (a reserved dongle never runs another job). shortfinal's, or anyone else's: never.
OWN_PROGRAMS = ("rtl_fm", "trunk-recorder")
OWN_UNITS = ("sizeup-station.service", "sizeup-trunk-recorder.service")
RTL_PROGRAMS = ("rtl_fm", "rtl_tcp", "rtl_sdr", "rtl_power", "rtl_433", "rtl_airband", "readsb", "dump1090",
                "dump1090-fa", "trunk-recorder", "acarsdec", "dumpvdl2", "gqrx", "op25")
PROC, SYS = "/proc", "/sys"
STOP_WAIT_S = 3.0


def clean(line) -> str:
    """One output line, printable and short (readsb prints nothing secret; still: no control characters, capped)."""
    if isinstance(line, bytes):
        line = line.decode(errors="replace")
    return "".join(ch for ch in str(line) if ch.isprintable()).strip()[:LINE_MAX]


def classify(lines: list[str]) -> tuple[str, str]:
    """(cause, the line that says so) from readsb's last lines: busy | not_found | permissions | bad_args | library |
    usbfs | lost | unknown (and "" when no line said it)."""
    for cause, needles in CAUSE_RULES:
        for ln in lines:
            if any(n in ln.lower() for n in needles):
                return cause, ln
    return "unknown", ""


def usb_device(serial: str, sys_root: str = SYS) -> dict | None:
    """The dongle with this exact serial in sysfs (nothing opened): {node: /dev/bus/usb/BBB/DDD, driver: a kernel driver
    bound to one of its interfaces (dvb_usb_rtl28xxu), or ""}. None when sysfs has no such serial."""
    base = Path(sys_root) / "bus" / "usb" / "devices"
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return None
    for d in entries:
        try:
            if ":" in d.name or (d / "serial").read_text().strip() != serial:
                continue
            bus, dev = int((d / "busnum").read_text()), int((d / "devnum").read_text())
        except (OSError, ValueError):
            continue
        driver = ""
        for itf in sorted(base.glob(f"{d.name}:*")):
            try:
                drv = os.path.basename(os.readlink(itf / "driver"))
            except OSError:
                continue
            if drv and drv != "usbfs":
                driver = drv
                break
        return {"node": f"/dev/bus/usb/{bus:03d}/{dev:03d}", "driver": driver}
    return None


def _proc_info(pdir: Path) -> dict:
    def rd(name: str) -> str:
        try:
            return (pdir / name).read_bytes().decode(errors="replace")
        except OSError:
            return ""
    uid = None
    for ln in rd("status").splitlines():
        if ln.startswith("Uid:"):
            try:
                uid = int(ln.split()[1])
            except (IndexError, ValueError):
                pass
    unit = ""
    for ln in rd("cgroup").splitlines():
        for part in reversed(ln.split(":")[-1].split("/")):
            if part.endswith(".service") or part.endswith(".scope"):
                unit = part
                break
        if unit:
            break
    argv = [a for a in rd("cmdline").split("\0") if a]
    comm = rd("comm").strip() or (os.path.basename(argv[0]) if argv else "")
    return {"pid": int(pdir.name), "comm": comm, "argv": argv[:24], "uid": uid, "unit": unit}


def holders(node: str, proc_root: str = PROC, skip: tuple = ()) -> tuple[list[dict], int]:
    """([the processes with `node` open], how many processes' handles couldn't be read: another user's). Read-only."""
    out, hidden = [], 0
    try:
        pids = [p for p in Path(proc_root).iterdir() if p.name.isdigit() and int(p.name) not in skip]
    except OSError:
        return [], 0
    for p in pids:
        try:
            fds = list((p / "fd").iterdir())
        except PermissionError:
            hidden += 1
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) == node:
                    out.append(_proc_info(p))
                    break
            except OSError:
                continue
    return out, hidden


def rtl_programs(proc_root: str = PROC, skip: tuple = ()) -> list[dict]:
    """Programs that open RTL-SDRs running now, any user's (/proc/<pid>/comm is readable): who MAY hold the dongle
    when the handles themselves can't be read."""
    out = []
    try:
        pids = [p for p in Path(proc_root).iterdir() if p.name.isdigit() and int(p.name) not in skip]
    except OSError:
        return []
    for p in pids:
        try:
            comm = (p / "comm").read_text().strip()
        except OSError:
            continue
        if comm in RTL_PROGRAMS or comm.startswith("rtl_"):
            out.append(_proc_info(p))
    return out


def channels(config: str = CONFIG) -> dict[str, str]:
    """{"453.0375": "FG1", ...}: the config's [[radio.channels]], to name an rtl_fm by its frequency."""
    try:
        with open(config, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    radio = data.get("radio") if isinstance(data.get("radio"), dict) else {}
    out = {}
    for c in radio.get("channels") or []:
        if isinstance(c, dict) and isinstance(c.get("mhz"), (int, float)) and c.get("label"):
            out[f"{float(c['mhz']):.4f}"] = str(c["label"])
    return out


def job_of(h: dict, chans: dict[str, str]) -> str:
    """The sizeup job a holder runs: an rtl_fm's channel (by its -f), trunk-recorder's P25; "" when unknown."""
    if h.get("comm") == "trunk-recorder":
        return "P25"
    a = h.get("argv") or []
    if "-f" in a and a.index("-f") + 1 < len(a):
        f = a[a.index("-f") + 1].rstrip("Mm")
        try:
            return chans.get(f"{float(f):.4f}", f"{float(f):.4f} MHz")
        except ValueError:
            return ""
    return ""


def is_own(h: dict, uid: int | None = None) -> bool:
    """sizeup's own job: one of its programs, run by this user, under one of its own services. Nothing else is."""
    if uid is None:
        uid = os.getuid() if hasattr(os, "getuid") else -1
    text = " ".join([h.get("unit") or "", *(h.get("argv") or [])]).lower()
    return (h.get("comm") in OWN_PROGRAMS and h.get("uid") is not None and h.get("uid") == uid
            and h.get("unit") in OWN_UNITS and "shortfinal" not in text)


def describe(h: dict, job: str = "") -> str:
    """'rtl_fm (pid 812, sizeup-station's FG1 job)' or 'rtl_tcp (pid 77, shortfinal.service)'."""
    unit = h.get("unit") or ""
    name = unit[:-len(".service")] if unit.endswith(".service") else unit
    who = (f"{name}'s {job} job" if job and name.startswith("sizeup") else unit
           or (f"uid {h['uid']}" if h.get("uid") is not None else ""))
    return f"{h.get('comm') or '?'} (pid {h.get('pid')}" + (f", {who}" if who else "") + ")"


def in_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


# -- the supervisor --------------------------------------------------------------------------------------------------------


class Decoder:
    """One step every STEP_S (step(now)); all state in self.status. popen / found / clock are for the tests."""

    def __init__(self, config: str = CONFIG, override: str = OVERRIDE, binary: str = READSB, json_dir: str = JSON_DIR,
                 status_path: str = STATUS, claims: Path | None = None, popen=subprocess.Popen, found=devices,
                 log=lambda m: print(m, flush=True), proc_root: str = PROC, sys_root: str = SYS, kill=os.kill,
                 sleep=time.sleep, uid: int | None = None):
        self.config, self.override, self.binary, self.json_dir = config, override, binary, json_dir
        self.status_path, self.claims, self.popen, self.found, self.log = status_path, claims, popen, found, log
        self.proc_root, self.sys_root, self.kill, self.sleep, self.uid = proc_root, sys_root, kill, sleep, uid
        self.lines: deque[str] = deque(maxlen=TAIL_LINES)    # readsb's output, this run
        self.reader: threading.Thread | None = None
        self.fail_detail = ""                                 # what the last failure said (before "; retry in")
        self.stopped_pids: list[int] = []                     # sizeup's own leftovers stopped (#262)
        self.said: tuple | None = None                        # (cause, holder) last logged
        self.proc: subprocess.Popen | None = None
        self.running: tuple | None = None          # (sdr, gain, bias_tee) of the readsb that runs
        self.claimed = ""
        self.started_at: float | None = None
        self.next_start = 0.0
        self.fails = 0
        self.restarts = 0
        self.no_signal_since: float | None = None
        self.no_signal_wait = NO_SIGNAL_RESTART_S[0]
        self.usb_fails = 0                                    # usbfs / lost-USB exits in a row (#269)
        self.buffer_kib: int | None = None                    # SMALL_BUF_KIB once they kept coming
        self.out = Output(json_dir)
        self.status: dict = {"state": "starting", "detail": "", "since": None}

    def _set(self, now: float, state: str, detail: str, **kw) -> None:
        """The status now; `since` is when it last changed state (logged then)."""
        changed = state != self.status.get("state")
        if changed:
            self.log(f"adsb: {state}" + (f": {detail}" if detail else ""))
        self.status = {"state": state, "detail": detail, "since": now if changed else self.status.get("since"), **kw}

    def stop(self, why: str = "") -> None:
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
            except OSError:
                pass
            self.log("adsb: readsb stopped" + (f" ({why})" if why else ""))
        if self.claimed:
            release(self.claimed, self.claims)
            self.claimed = ""
        self.running, self.started_at = None, None
        self.out.reset()

    def _fail(self, now: float, why: str) -> None:
        self.fails += 1
        wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(self.fails - 1, 30))
        self.next_start = now + wait
        self.restarts += 1
        self.stop(why)
        self.log(f"adsb: {why}; trying again in {wait:.0f} s")

    def step(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        s = settings(self.config, self.override)
        want = (s["sdr"], s["gain"], s["bias_tee"])
        base = {"sdr": s["sdr"], "source": s["source"], "gain": s["gain"], "bias_tee": s["bias_tee"],
                "restarts": self.restarts, "json": str(Path(self.json_dir) / "aircraft.json"),
                "usbfs_mb": usbfs_mb(self.sys_root), "buffer_kib": self.buffer_kib}
        if self.proc is not None and self.running != want:
            self.stop("its dongle, gain or bias-tee changed")
            self.fails, self.next_start = 0, 0.0
        if not s["sdr"]:
            self.stop("no dongle named for ADS-B")
            self._set(now, "off", "no dongle named for ADS-B ([aircraft.receiver] sdr, or Settings > Radios)", **base)
        elif s["problem"]:
            self.stop(s["problem"])
            self._set(now, "refused", s["problem"], **base)
        elif not os.access(self.binary, os.X_OK):
            self._set(now, "missing", f"{self.binary} isn't installed (sudo sizeup-station update builds it)", **base)
        elif self.proc is None:
            self._start(now, s, want, base)
        else:
            self._watch(now, s, base)
        self.status["at"] = now
        self._write()
        return self.status

    def _start(self, now: float, s: dict, want: tuple, base: dict) -> None:
        c = other(s["sdr"], self.claims)
        if c:
            held = {"app": c.get("app"), "mode": c.get("mode")}
            self._set(now, "held", f"{c.get('app', 'another app')} holds {s['sdr']}"
                                   + (f" ({c.get('mode')})" if c.get("mode") else "")
                                   + ": not started (switch it Off there to hand it over)", held_by=held,
                      cause="claimed", holder=f"{c.get('app', 'another app')} ({c.get('mode', '?')})", **base)
            return
        if now < self.next_start:
            first = (self.fail_detail if self.status.get("state") == "down" and self.fail_detail
                     else self.status.get("detail", "").split(";")[0])
            self.status.update(base, detail=f"{first}; retry in {self.next_start - now:.0f} s")
            return
        try:
            found = self.found()
        except Exception as e:  # noqa: BLE001 - librtlsdr in a bad way: like not plugged in
            found = None
            self.log(f"adsb: dongle lookup failed: {type(e).__name__}: {e}")
        idx = resolve(s["sdr"], found)
        if idx is None:
            same = [i for i, x in (found or []) if x == s["sdr"]]
            self.next_start = now + 5.0
            if len(same) > 1:              # #242: never a guess between them (one may be a radio, not behind the filter)
                self._set(now, "ambiguous", duplicate_text(s["sdr"], same) + ": readsb not started", present=True, **base)
                return
            why = "librtlsdr not found" if found is None else f"{s['sdr']} isn't plugged in"
            if found is not None:
                why += f" (plugged in: {', '.join(x for _i, x in found) or 'none'})" + (
                    ": WSL? usbipd attach it again on Windows" if in_wsl() else "")
            self._set(now, "unplugged", why, present=False, cause="library" if found is None else "not_found", **base)
            return
        if not claim(s["sdr"], self.claims):
            return                                     # taken between the look and now: held, next step says by whom
        self.claimed = s["sdr"]
        mb = usbfs_mb(self.sys_root)
        if mb:                        # a limit: lifted when this can (root; the unit's ExecStartPre did it at start)
            before, after, err = set_usbfs(self.sys_root)
            if not err:
                self.log(f"adsb: usbfs_memory_mb {before} -> 0")
            base["usbfs_mb"] = after
        argv = readsb_argv(self.binary, idx, s["gain"], s["bias_tee"], self.json_dir, buffer_kib=self.buffer_kib)
        try:
            Path(self.json_dir).mkdir(parents=True, exist_ok=True)
            for stale in ("aircraft.json", "stats.json"):
                (Path(self.json_dir) / stale).unlink(missing_ok=True)
            self.lines.clear()
            self.proc = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
            out = getattr(self.proc, "stdout", None)
            self.reader = None
            if out is not None:
                self.reader = threading.Thread(target=self._drain, args=(out,), name="readsb-out", daemon=True)
                self.reader.start()
        except OSError as e:
            self._fail(now, f"readsb can't start: {e}")
            self._set(now, "down", f"readsb can't start: {e}", **base)
            return
        self.running, self.started_at = want, now
        self.no_signal_since = None
        self.fail_detail = ""
        self.log(f"adsb: readsb on {s['sdr']} (device #{idx}), gain {s['gain']}, bias-tee "
                 f"{'ON' if s['bias_tee'] else 'off'}, pid {self.proc.pid}"
                 + (f", USB transfers {self.buffer_kib} KiB" if self.buffer_kib else "")
                 + (f" (usbfs_memory_mb {base['usbfs_mb']})" if base.get("usbfs_mb") else ""))
        self._set(now, "starting", f"readsb on {s['sdr']} (device #{idx})", pid=self.proc.pid, index=idx,
                  present=True, argv=argv, **base)

    def _watch(self, now: float, s: dict, base: dict) -> None:
        keep = {k: self.status.get(k) for k in ("pid", "index", "argv")}
        code = self.proc.poll()
        if code is not None:
            self._exited(now, s, base, code)
            return
        o = self.out.read(now)
        nums = {k: o.get(k) for k in ("messages_per_s", "aircraft", "with_pos", "last_message_at")}
        if now - (self.started_at or now) < GRACE_S:
            self._set(now, "starting", f"readsb on {s['sdr']}, starting", present=True, **keep, **nums, **base)
            return
        if not o["ok"] or o["age"] > STALE_S:
            why = o.get("why") or f"aircraft.json not updated for {o['age']:.0f} s"
            self._fail(now, f"readsb stuck: {why}")
            self._set(now, "down", f"readsb stuck ({why}): restarting", **base)
            return
        if o["samples_still_s"] is not None and o["samples_still_s"] > STALL_S:
            self._fail(now, f"no samples from the dongle for {o['samples_still_s']:.0f} s")
            self._set(now, "down", "the dongle stopped delivering samples: restarting", **base)
            return
        quiet = now - (o["last_message_at"] or self.started_at or now)
        if quiet > NO_SIGNAL_S:
            if self.no_signal_since is None:
                self.no_signal_since = now
            elif now - self.no_signal_since >= self.no_signal_wait:
                self.no_signal_wait = min(self.no_signal_wait * 2, NO_SIGNAL_RESTART_S[1])
                self.no_signal_since = None
                self.restarts += 1
                self.stop("no signal")
                self._set(now, "down", f"nothing decoded for {quiet:.0f} s: restarting readsb", **base)
                return
            self._set(now, "no_signal", f"nothing decoded for {quiet:.0f} s: check the antenna, the filter and the LNA"
                                        + (" (bias-tee is on)" if s["bias_tee"] else " (bias-tee is off: does the LNA "
                                                                                     "need power from it?)"),
                      present=True, **keep, **nums, **base)
            return
        self.no_signal_since, self.no_signal_wait = None, NO_SIGNAL_RESTART_S[0]
        if self.fails and now - (self.started_at or now) > 120:
            self.fails = 0
            self.usb_fails = 0
        rate = o["messages_per_s"]
        self.said = None
        self._set(now, "ok", f"{o['aircraft']} aircraft ({o['with_pos']} with a position)"
                             + (f", {rate:g} messages/s" if rate is not None else ""),
                  present=True, **keep, **nums, **base)

    # -- readsb's output, and why it stopped (#262) -------------------------------------------------------------------

    def _drain(self, out) -> None:
        """readsb's stdout + stderr: kept (the last TAIL_LINES) and logged to the journal as before."""
        try:
            for raw in iter(out.readline, b""):
                ln = clean(raw)
                if ln:
                    self.lines.append(ln)
                    self.log(f"readsb: {ln}")
        except (OSError, ValueError):
            pass

    def _exited(self, now: float, s: dict, base: dict, code: int) -> None:
        if self.reader is not None:
            self.reader.join(timeout=1.0)      # its last words arrive with the pipe's end
        lines = list(self.lines)
        d = self.diagnose(s["sdr"], lines)
        stopped = self.recover(d)
        self._usb_fallback(d)
        words = CAUSE_WORDS.get(d["cause"], d["cause"])
        if d["holder"]:
            words += f" (held by {d['holder']}" + ("; stopped it, retrying" if stopped else "") + ")"
        elif d["cause"] == "unknown":
            words = f"readsb exited with code {code}"
        if d["hint"]:
            words += f": {d['hint']}"
        said = d["line"] or (lines[-1] if lines else "")
        detail = words + (f" · readsb: {said}" if said else "")
        self._fail(now, f"readsb exited with code {code}: {d['cause']}" + (f" ({said})" if said else ""))
        if stopped:
            self.next_start, self.fails = now + BACKOFF_S[0], 0     # the dongle is free now: straight back
        self.fail_detail = detail
        if (d["cause"], d["holder"]) != self.said:            # the journal: once per cause, not every retry
            self.said = (d["cause"], d["holder"])
            self.log(f"adsb: decoder down: {detail}")
        self._set(now, "down", detail, cause=d["cause"], holder=d["holder"], tail=lines[-DETAIL_LINES:],
                  recovery=f"stopped {d['holder']}" if stopped else "", present=d["present"], index=d["index"],
                  code=code, **base)

    def _usb_fallback(self, d: dict) -> None:
        """The usbfs limit (or a USB loss, which it looks like without librtlsdr's line) SMALL_AFTER times in a row:
        readsb from now on with SMALL_BUF_KIB transfers (#269). Said in the hint either way."""
        if d["cause"] not in ("usbfs", "lost"):
            self.usb_fails = 0
            return
        self.usb_fails += 1
        mb = usbfs_mb(self.sys_root)
        if self.buffer_kib is None and self.usb_fails >= SMALL_AFTER:
            self.buffer_kib = SMALL_BUF_KIB
            self.log(f"adsb: {self.usb_fails} USB failures in a row: readsb with {SMALL_BUF_KIB} KiB transfers from now on")
        hint = []
        if mb:
            hint.append(f"usbfs_memory_mb is {mb} (0 = no limit; sudo systemctl restart sizeup-adsb sets it)")
        elif mb == 0 and d["cause"] == "usbfs":
            hint.append("usbfs_memory_mb is already 0")
        if self.buffer_kib:
            hint.append(f"readsb now uses {self.buffer_kib} KiB USB transfers")
        elif d["cause"] == "usbfs" or mb:
            hint.append(f"smaller USB transfers after {SMALL_AFTER} in a row")
        if hint and not d["hint"]:
            d["hint"] = "; ".join(hint)

    def diagnose(self, serial: str, lines: list[str]) -> dict:
        """Why readsb stopped, read-only: {cause, line, holder, own: [pids sizeup may stop], present, index, hint}.
        readsb's own words first; then is the serial in librtlsdr's list (and at which index), and who has its USB
        device open (/proc/*/fd where readable, the shared claims, a kernel driver)."""
        cause, line = classify(lines)
        out = {"cause": cause, "line": line, "holder": "", "own": [], "present": None, "index": None, "hint": ""}
        try:
            found = self.found()
        except Exception:  # noqa: BLE001 - a report, never in the way
            found = None
        if found is not None:
            hits = [i for i, x in found if x == serial]
            out["present"] = bool(hits)
            out["index"] = hits[0] if len(hits) == 1 else None
            if not hits and cause in ("unknown", "not_found", "lost"):
                out["cause"] = "not_found"
                seen = ", ".join(x for _i, x in found) or "none"
                out["hint"] = (f"serial {serial} isn't in the dongle list (plugged in: {seen})"
                               + (" (WSL: usbipd attach it again on Windows)" if in_wsl() else ""))
                return out
        if out["cause"] not in ("busy", "unknown", "permissions"):
            return out
        usb = usb_device(serial, self.sys_root)
        if usb and usb["driver"]:
            out["holder"] = f"the kernel's {usb['driver']} driver"
            out["cause"] = "busy"
            return out
        found_holder = []
        if usb:
            found_holder, hidden = holders(usb["node"], self.proc_root, skip=(os.getpid(),))
        else:
            hidden = 0
        chans = channels(self.config) if found_holder else {}
        c = holder(serial, self.claims)
        claimed = c if c and not (c.get("app") == APP and int(c.get("pid", -1)) == os.getpid()) else None
        if found_holder:
            out["cause"] = "busy" if out["cause"] == "unknown" else out["cause"]
            uid = self.uid
            for h in found_holder:
                if claimed and claimed.get("app") != APP and int(claimed.get("pid", -1)) == h["pid"]:
                    continue                              # another app's claim names this process: theirs
                if is_own(h, uid):
                    out["own"].append(h["pid"])
            names = [describe(h, job_of(h, chans) if is_own(h, uid) else "") for h in found_holder]
            out["holder"] = ", ".join(names)
        elif claimed:
            out["holder"] = f"{claimed.get('app', 'another app')} ({claimed.get('mode', '?')}, its claim)"
        elif out["cause"] == "busy":
            maybe = [h for h in rtl_programs(self.proc_root, skip=(os.getpid(),)) if h.get("comm") != "readsb"]
            if maybe:
                out["holder"] = "maybe " + ", ".join(describe(h) for h in maybe[:3])
            elif hidden:
                out["hint"] = f"{hidden} processes of other users couldn't be checked"
        return out

    def recover(self, d: dict) -> list[int]:
        """Stop sizeup's OWN leftover job on the ADS-B dongle (d["own"]: an rtl_fm or trunk-recorder under sizeup's
        services): a reserved dongle never runs another job. Never another app's process. The pids stopped."""
        done = []
        for pid in d.get("own") or []:
            try:
                self.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError as e:
                self.log(f"adsb: couldn't stop pid {pid}: {e}")
                continue
            gone = False
            for _ in range(int(STOP_WAIT_S * 10)):
                if not (Path(self.proc_root) / str(pid)).exists():
                    gone = True
                    break
                self.sleep(0.1)
            if not gone:
                try:
                    self.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            self.log(f"adsb: stopped sizeup's leftover pid {pid} on the ADS-B dongle ({d.get('holder')})")
            done.append(pid)
        self.stopped_pids += done
        return done

    def _write(self) -> None:
        try:
            p = Path(self.status_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.{os.getpid()}")
            tmp.write_text(json.dumps(self.status, separators=(",", ":")))
            os.chmod(tmp, 0o644)
            os.replace(tmp, p)
        except OSError:
            pass


def read_status(path: str | None = None, now: float | None = None) -> dict | None:
    """The decoder's last status (sizeup-station: Settings > Radios, station health); None when there is none. One older
    than 30 s says so: the service isn't running."""
    try:
        st = json.loads(Path(path or STATUS).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    now = time.time() if now is None else now
    if isinstance(st.get("at"), (int, float)) and now - st["at"] > 30:
        st = dict(st, state="down", detail=f"the decoder service hasn't reported for {now - st['at']:.0f} s "
                                           "(systemctl status sizeup-adsb)")
    return st


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "run"
    if cmd == "print":
        s = settings()
        if not s["sdr"] or s["problem"]:
            print(s["problem"] or "no dongle named for ADS-B: nothing runs")
            return 0
        idx = resolve(s["sdr"], devices())
        print(" ".join(readsb_argv(READSB, idx if idx is not None else -1, s["gain"], s["bias_tee"]))
              + ("" if idx is not None else f"   ({s['sdr']} isn't plugged in now)"))
        return 0
    if cmd == "status":
        st = read_status()
        print(json.dumps(st, indent=2) if st else "no status yet (is sizeup-adsb running?)")
        return 0
    if cmd == "usbfs":
        return usbfs_main()
    if cmd != "run":
        print("usage: adsbd.py run | print | status | usbfs", file=sys.stderr)
        return 2
    d = Decoder()
    stopping = []

    def bye(*_a):
        stopping.append(1)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    d.log(f"adsb: decoder supervisor up ({READSB}); the dongle named in {CONFIG} [aircraft.receiver] sdr or Settings")
    try:
        while not stopping:
            try:
                d.step()
            except Exception as e:  # noqa: BLE001 - supervision must never die
                d.log(f"adsb: {type(e).__name__}: {e}")
            for _ in range(int(STEP_S * 10)):
                if stopping:
                    break
                time.sleep(0.1)
    finally:
        d.stop("the service is stopping")
        d.status.update(state="off", detail="the decoder service stopped", at=time.time())
        d._write()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
