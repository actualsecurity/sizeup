"""The station's trunk-recorder as its own service (GitHub #71): an update of sizeup leaves P25 on its control channel.

trunk-recorder used to be a child of sizeup-station (sizeup/radio/recorder.py TrunkRecorder): every restart of sizeup, a
Python-only update included, stopped it, and Fire/EMS 15 was off the air until it had started again and locked on to
the control channel. On the station it now runs under its own unit, which a sizeup restart doesn't touch:

    sizeup-trunk-recorder.service (deploy/station/systemd): the sizeup user runs, with the system python3 (stdlib only,
    like adsbd.py; installed as /usr/local/lib/sizeup/trunkrecd.py by install-linux.sh):
        python3 -I trunkrecd.py run        supervise trunk-recorder (the service)
        python3 -I trunkrecd.py print      the command line it would run now, or why none
        python3 -I trunkrecd.py status     the last status it wrote

The handover, with no privilege at all (no sudo, no polkit rule, no root helper): sizeup-station still decides which
dongle runs Fire/EMS 15 (Settings > Radios, auto-assign, the hub's switch) and writes its choice to STATE, a file of the
sizeup user's like state/adsb_receiver.json; this service, the same user, reads it every STEP_S and does what it says:

    STATE   {"on": true, "sdr": "<serial>", "name": "V4", "pid": <sizeup's pid>, "seq": <n>}   sizeup's wish
            "on": false: nothing runs. "sdr": "": trunk-recorder's config.json as it is (no shared dongles).
    STATUS  {at, state: off | ok | absent | ambiguous | held | down, detail, on, sdr, name, seq, pid, tr_pid, index, restarts, argv,
             held_by}: what runs. "seq" is the last STATE seq it acted on; "pid" is this supervisor's (the claim's).

trunk-recorder is (re)started only when what it runs changes: the dongle in STATE, or [p25] recorder / recorder_dir in
/etc/sizeup/config.toml. A STATE rewritten with the same dongle (a new sizeup process taking over after an update:
another pid and seq) changes nothing, so the trunk-recorder that runs is adopted, same PID. Its build, its config.json
and talkgroups.csv, this file and its unit change only with an install or update, and install-linux.sh restarts the
service then, and only then (tr_sig).

What runs (as sizeup did it, sizeup/radio/recorder.py): trunk-recorder with a copy of its config.json,
config.runtime.json, whose sources use the dongle's CURRENT index ("rtl=<index>", looked up by its exact serial right
before each start: osmosdr reads an all-digit serial as an index) and whose systems run the unit script
(unit-event.sh). Its output, the unit script's "SIZEUP-UNIT ..." grant lines included, goes to trunk-recorder.log in its
working directory (capped at LOG_MAX_BYTES, one previous file kept), which sizeup reads as it grows
(recorder.py UnitRecorder) for who is talking; the call captures go to the capture directory sizeup watches, as before.

The shared dongle claims (~/.local/state/rtl-claims, sizeup/radio/claims.py, agreed with shortfinal): while Fire/EMS 15
is on a dongle this process holds its claim (app "sizeup", mode "Fire/EMS 15", this pid), so the claim stays right
while sizeup-station restarts. sizeup writes its own claim before it hands a dongle over; that one, by the pid in
STATE, is taken over. Anyone else's live claim (shortfinal, the ADS-B decoder) is never taken: the status says "held"
and nothing starts until they let go. sizeup counts this process's claims as its own (claims.helpers).

More systems than the county's (GitHub #409: MTA MRRS): each <recorder_dir>/systems/<name>/ (its own config.json and
talkgroups.csv, from deploy/trunk-recorder/systems/) is a second trunk-recorder under this service, with its own state
(p25_recorder.<name>.json: {"on", "sources": {"<source index>": {"sdr": <serial>, "mode": <its job>}}, "label", "pid",
"seq", "until": only for a reception test (#410): the time after which it is off by itself}), status (status.<name>.json), log and dongle claims (SystemSupervisor). It runs only the sources of its
config.json that sizeup gave a dongle, with the control channels those cover; nothing asked, nothing runs. The county's
trunk-recorder above never restarts for another system's sake.

Supervision (as recorder.py did): an exit restarts it with backoff (2 s doubling to 30 s; back to 2 s after a minute of
running); "Wrong rtlsdr device index" / "No supported devices" mean the dongle is gone: "absent", looked for again every
ABSENT_S without counting a restart. Another system's trunk-recorder (SystemSupervisor) may not exit when its dongles
are unplugged (it stayed up, silent), so its dongles are looked for on USB every UNPLUGGED_S while it runs: one gone
stops it ("absent"), and it starts again, never as one of the watchdog's capped restarts, once every one is back and
USB has settled, after the county's trunk-recorder (which may want the same hub's dongles) is up; a flapping dongle is
held back longer each time, each a restart counted (#592), its status saying until when ("hold_until", #637). An
antenna group's move (sizeup/radio/groups.py) waits for the county's trunk-recorder the same way (#637).

The watchdog (#547: a trunk-recorder that stayed up recorded nothing for 22 h): one that runs but has stopped working is
restarted (Watchdog). Faults, each judged from trunk-recorder's start at the earliest:
    calls     no new call file (a change under its config's captureDir, looked at every WATCH_SCAN_S: sizeup takes the
              files away within seconds) for its limit: the system's own recent rate (its "Concluding Recorded Call"
              lines), WATCH_FACTOR (10) times its mean gap between calls over the WATCH_BASE_S (3 h) before the last
              call, between watchdog_min_min (15 min) and watchdog_max_min (3 h); with fewer than
              WATCH_BASE_MIN (10) calls in those 3 h (a quiet night, a service just installed) the longest. Each restart
              with no call since the last one doubles it, so a really quiet 4 am is restarted at most once or twice
    decoder   with "controlWarnRate": -1 trunk-recorder logs its control channel's decode rate every 3 s; once such a line
              has said the rate is healthy (so it logs them all), none for CC_SILENT_S (2 min) is a hung decoder
    control   the decode rate under CC_LOW (2/s) for CC_LOST_RESTART_S (10 min): trunk-recorder's own retunes didn't help
At most watchdog_per_hour (3; 0: no watchdog) restarts in an hour; each is a line in the journal, and the status says
the last one (watchdog). [p25] watchdog_* in /etc/sizeup/config.toml change the limits (sizeup/config.py checks them).

Its output into the journal (#547, Shipper): the control channel's decode rate (when trunk-recorder first logs it, when
it is lost or back, and a summary every RATE_LOG_S), and its error / warning / control channel lines, radio IDs masked,
at most SHIP_BURST different lines per SHIP_WINDOW_S (then a count): the station's logs go to the hub. A watched
talkgroup's grants that weren't recorded (its config.json's "sizeup" "reception" "watch" list) are said as why
(encrypted, no recorder, off, no source), at most one line per talkgroup a minute (then a count)."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections import deque
from pathlib import Path

CONFIG = "/etc/sizeup/config.toml"
STATE = "/var/lib/sizeup/state/p25_recorder.json"          # sizeup-station writes (sizeup/radio/recorder.py UnitRecorder)
RUN_DIR = "/run/sizeup-trunk-recorder"                     # the service's RuntimeDirectory (tmpfs)
STATUS = RUN_DIR + "/status.json"
UNIT = "sizeup-trunk-recorder.service"
LIBRTLSDR = ("/opt/sizeup/rtl-sdr/lib/librtlsdr.so.0", "librtlsdr.so.0", "/opt/homebrew/lib/librtlsdr.dylib")
APP, MODE = "sizeup", "Fire/EMS 15"                        # the claim, as sizeup writes it (sizeup/app.py P25_MODE)
SERIAL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
LOG = "trunk-recorder.log"
LOG_MAX_BYTES = 5_000_000
UNIT_SCRIPT = "unit-event.sh"
# trunk-recorder 5.2's unit_script plugin builds "<unitScript> <shortName> <radioID> call <tg> <patches> &"
# in a 200-byte buffer (cut there: a cut "&" only makes it wait for the script): an absolute path
# longer than this leaves too little room, use ./ then (trunk-recorder's cwd is workdir).
UNIT_SCRIPT_MAX = 140
UNPLUGGED = ("Wrong rtlsdr device index", "No supported devices", "No matching devices")
SAYS = ("control channel", "decoding", "error", "starting")    # lines worth showing as the detail

STEP_S = 0.5
BACKOFF_S = (2.0, 30.0)
UNPLUGGED_S = 5.0                 # the dongle isn't on USB: look again this often
ABSENT_S = 15.0                   # trunk-recorder said its dongle is gone: start again this much later
# Another system's dongles unplugged under it and back (#592, SystemSupervisor):
RETURN_STABLE_S = 5.0             # back: the same (index, serial) list at two looks this far apart before it starts
RETURN_SETTLE_MAX_S = 60.0        # ... settling at most this long (a flapping dongle elsewhere): then the latest look
COUNTY_SETTLE_S = 5.0             # ... and the county's trunk-recorder, if it should run, up this long first
COUNTY_WAIT_S = 60.0              # ... waited for at most this long (the county down for its own reasons); the same
                                  # for an antenna group's move (#637: the county's dongle given back in the same step)
FLAP_S = 600.0                    # gone again this soon after a return: a flapping dongle (a restart counted)
FLAP_HOLD_S = (30.0, 120.0, 300.0, 600.0)   # ... then started no sooner than this after it went, per flap in a row
LONG_ABSENCE_S = 60.0             # gone longer than this: the watchdog's hour of restarts is forgotten at the return
STOP_S = 8.0                      # SIGTERM, then this long, then SIGKILL
STALE_S = 15.0                    # a status older than this: the service isn't running

# The watchdog (#547). The defaults, and why:
WATCH_SCAN_S = 30.0               # the capture directory looked at this often (only directories that changed are listed)
WATCH_BASE_S = 3 * 3600.0         # the call rate: the calls in the 3 h before the last one (the hour of day it stopped)
WATCH_BASE_MIN = 10               # fewer calls than this in it: no rate, the longest limit
WATCH_FACTOR = 10.0               # [p25] watchdog_factor: 10 mean gaps without a call (Poisson: e^-10, 1 in 22,000)
WATCH_MIN_S = 15 * 60.0           # [p25] watchdog_min_min: never sooner (calls come in bursts; the hub alerts at 20)
WATCH_MAX_S = 3 * 3600.0          # [p25] watchdog_max_min: never later (the 22 h of #547 would have been 3 h)
WATCH_PER_HOUR = 3                # [p25] watchdog_per_hour: restarts in any hour at most (0: no watchdog)
WATCH_DOUBLE_MAX_S = 24 * 3600.0  # restarts with no call between them double the limit, up to this
CC_RATE = re.compile(r"Control Channel Message Decode Rate:\s*(?P<rate>[\d.]+)\s*/\s*sec", re.I)
CALL_DONE = "Concluding Recorded Call"   # trunk-recorder's line at the end of each call it recorded
CC_LOW = 2.0                     # messages / s under this: the control channel is lost (trunk-recorder retunes)
CC_WARN = 10.0                    # trunk-recorder's default controlWarnRate: a rate line at or over it means all are logged
CC_RUN_GAP_S = 15.0               # (recorder.py's) a low line every 3 s while it lasts: none this long, the run is over
CC_SILENT_S = 120.0               # every rate line logged, and none this long: the decoder is hung
CC_LOST_RESTART_S = 600.0         # the control channel lost this long: restart (the hub alerts at 2 min)
# Its lines into the journal (#547)
SHIP_WINDOW_S = 300.0
SHIP_BURST = 10                   # different lines per window (digits ignored); the rest counted
RATE_LOG_S = 1800.0               # the decode rate summarised this often
RATE_SAY_S = 60.0                 # a lost / back line at most this often (a marginal rate flips every 3 s)
SHIP_LEVELS = re.compile(r"\((?:warning|error|fatal)\)", re.I)
SHIP_SKIP = ("SIZEUP-UNIT", "Unit ID set via Control Channel")     # per transmission, and they name radios
TR_STAMP = re.compile(r"^\[\d{4}-\d\d-\d\d [\d:.]+\]\s*")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# A watched talkgroup's call lines (the "watch" list of the "sizeup" "reception" block of a system's config.json): what
# trunk-recorder did with each grant, one line per talkgroup per WATCHED_EVERY_S at most, the rest counted. Its 5.2.0
# log_header, "[<sys>]\t<n>C\tTG: <tg>\tFreq: <f> MHz\t", then (monitor_systems.cc, source.cc, p25_recorder_impl.cc):
CALL_LINE = re.compile(r"\[(?P<sys>[^\]\s]+)\]\s+\d+C\s+TG:\s*(?P<tg>\d+)\b.*?\sFreq:\s*(?P<mhz>[\d.]+)\s*MHz\s+(?P<msg>.*)$")
CALL_KINDS = (     # (its words, the kind counted, what the journal says)
    ("Starting P25 Recorder", "recording", "grant, recording"),
    ("Not Recording: ENCRYPTED", "encrypted", "grant (encrypted, not recorded)"),     # "- src: <radio>": never said
    ("Not recording talkgroup - Priority is -1", "off", "not recorded, priority -1 (off)"),
    ("Not recording talkgroup. Priority is", "no_recorder", "not recorded, no recorder"),
    ("No Digital Recorders Available", "no_recorder", "not recorded, no recorder"),
    ("Not Recording: no source covering Freq", "no_source", "not recorded, no source covers"),
)
CALL_SHORT = {"recording": "recording", "encrypted": "encrypted", "off": "off", "no_recorder": "no recorder",
              "no_source": "no source"}
NO_RECORDER = re.compile(r"Priority is (?P<p>\d+) but only (?P<n>\d+) recorders")
WATCHED_EVERY_S = 60.0
# The split patch's lines (#638/#647: tr_patch in deploy/station/install-linux.sh), info level and each naming radios:
# never shipped, only counted (SplitTally), with the calls they came in (one "Starting P25 Recorder" per call), for the
# review that decides SIZEUP_TR_SPLIT per system. (its words, the kind counted)
SPLIT_KINDS = (
    ("sizeup split (log): would split", "would"),        # log: a cut it would have made
    ("sizeup split: ID flip", "flip"),                    # log / on: a radio's two IDs on one key-up, not a new talker
    ("sizeup split: ID alias", "alias"),                  # log / on: a new ID early in a turn, the same talker
    ("Unit ID changed on Voice Channel", "split"),        # on: a cut it made (sizeup/radio/live.py names the talker)
    ("Starting P25 Recorder", "calls"),                   # a call recorded
)
SPLIT_HEAD = re.compile(r"\[(?P<sys>[^\]\s]+)\]\s+(?P<call>\d+)C\b")    # log_header's "[<sys>]\t<n>C"
SPLIT_EVERY_S = 3600.0            # the counts said once an hour per system (and when trunk-recorder is stopped)


# -- what to run ----------------------------------------------------------------------------------------------------------


def unit_script_command(workdir: str | Path, value: str | None = None) -> str:
    """What goes in a system's "unitScript": the script as an absolute, shell-quoted path (the
    plugin hands it to system()), run by /bin/sh if it isn't executable. value: what config.json
    says ("" = off; a relative path is taken from workdir). '' when there is no script."""
    if value == "":
        return ""
    base = Path(workdir).resolve()
    if value and (value.startswith("/") or any(c.isspace() for c in value)):
        return value                                     # absolute already, or a command line: as given
    path = (base / value) if value else (base / UNIT_SCRIPT)
    if not path.is_file():
        return ""
    cmd = shlex.quote(str(path))
    if len(cmd) > UNIT_SCRIPT_MAX:
        cmd = "./" + shlex.quote(str(path.relative_to(base))) if path.is_relative_to(base) else cmd
    return cmd if os.access(path, os.X_OK) else f"/bin/sh {cmd}"


class AmbiguousSource(ValueError):
    """Every source in config.json names a dongle serial two or more plugged-in dongles share (#242)."""


def rtl_serial(device: str | None) -> str:
    """The dongle an osmosdr device string names ("rtl=fire-p25,buflen=..." -> "fire-p25"), "" for none."""
    for part in str(device or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "rtl":
            return v.strip()
    return ""


# The station's trunk-recorder patch (#638, install-linux.sh tr_patch): per system, SIZEUP_TR_SPLIT in its environment,
# from the system's config.json "sizeup": {"split": ...}: off (stock), log (stock; logs where it would cut), on.
SPLIT_ENV = "SIZEUP_TR_SPLIT"
SPLIT_MODES = ("off", "log", "on")
SPLIT_DEFAULT = "log"


def split_mode(argv: list[str], workdir: str | Path) -> str:
    """SIZEUP_TR_SPLIT for the trunk-recorder argv runs in workdir: its config's "sizeup": {"split"} (#638), SPLIT_DEFAULT
    when it says none (or something else)."""
    k = next((i for i, a in enumerate(argv) if a.startswith("--config")), None)
    name = (argv[k].split("=", 1)[1] if "=" in argv[k] else argv[k + 1]) if k is not None else "config.json"
    try:
        mode = (json.loads((Path(workdir) / name).read_text()).get("sizeup") or {}).get("split")
    except (OSError, ValueError, AttributeError):
        mode = None
    return mode if mode in SPLIT_MODES else SPLIT_DEFAULT


def runtime_argv(argv: list[str], workdir: str | Path, dev: int | str | None = None,
                 ambiguous=None, left_out: list[str] | None = None) -> list[str]:
    """argv with --config pointing at a copy of its config (config.runtime.json, written in workdir) whose sources
    use dongle `dev` (None: as configured) and whose systems run the unit script. ambiguous(serial) -> why it can't be
    opened now ("" if fine): with dev None, a source naming such a serial is left out (said in left_out), and with
    every source left out AmbiguousSource is raised (#242: never a guess between two dongles with one serial)."""
    argv = list(argv)
    k = next((i for i, a in enumerate(argv) if a.startswith("--config")), None)
    name = (argv[k].split("=", 1)[1] if "=" in argv[k] else argv[k + 1]) if k is not None else "config.json"
    src = Path(workdir) / name
    if dev is None and not src.is_file():
        return argv                                      # no config of ours to amend: run as configured
    conf = json.loads(src.read_text())
    conf.pop("sizeup", None)                             # sizeup's side of the file (#638)
    if dev is not None:
        for s in conf.get("sources", []):
            s["device"] = f"rtl={dev}"
    elif ambiguous is not None:
        keep, out = [], []
        for s in conf.get("sources", []):
            ser = rtl_serial(s.get("device"))
            why = ambiguous(ser) if ser else ""
            (out if why else keep).append(f"{s.get('device')}: {why}" if why else s)
        if left_out is not None:
            left_out[:] = out
        if out and not keep:
            raise AmbiguousSource("; ".join(out))
        conf["sources"] = keep
    for s in conf.get("systems", []):
        cmd = unit_script_command(workdir, s.get("unitScript"))
        if cmd:
            s["unitScript"] = cmd
        else:
            s.pop("unitScript", None)
    out = Path(workdir) / "config.runtime.json"
    out.write_text(json.dumps(conf, indent=2))
    if k is None:
        argv.append(f"--config={out.name}")
    elif "=" in argv[k]:
        argv[k] = f"--config={out.name}"
    else:
        argv[k + 1] = out.name
    return argv


def recorder(config: str = CONFIG) -> dict:
    """{argv, workdir, problem}: sizeup's [p25] recorder (its command line) and recorder_dir (its working directory; a
    relative one is the config's folder's), the same two sizeup-station reads."""
    try:
        with open(config, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return {"argv": [], "workdir": "", "problem": f"{config} can't be read ({type(e).__name__})"}
    p25 = data.get("p25") if isinstance(data.get("p25"), dict) else {}
    cmd = str(p25.get("recorder") or "").strip()
    if not cmd:
        return {"argv": [], "workdir": "", "problem": f"no [p25] recorder in {config}"}
    try:
        argv = shlex.split(os.path.expanduser(cmd))
    except ValueError as e:
        return {"argv": [], "workdir": "", "problem": f"[p25] recorder: {e}"}
    base = Path(config).resolve().parent
    d = str(p25.get("recorder_dir") or "")
    workdir = str(base / d) if d and not Path(d).is_absolute() else (d or str(base))
    return {"argv": argv, "workdir": workdir, "problem": "", "watch": watch_settings(p25)}


WATCH_KEYS = {"watchdog_min_min": (WATCH_MIN_S / 60, 5, 720), "watchdog_max_min": (WATCH_MAX_S / 60, 5, 1440),
              "watchdog_factor": (WATCH_FACTOR, 2, 100), "watchdog_per_hour": (WATCH_PER_HOUR, 0, 12)}


def watch_settings(p25: dict) -> dict:
    """The watchdog's limits from [p25] (WATCH_KEYS: default, lowest, highest; sizeup/config.py refuses a bad one, so
    here one out of range is only the default): {min_s, max_s, factor, per_hour}."""
    v = {}
    for k, (d, lo, hi) in WATCH_KEYS.items():
        x = p25.get(k, d)
        v[k] = float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and lo <= x <= hi else float(d)
    lo_s = v["watchdog_min_min"] * 60
    return {"min_s": lo_s, "max_s": max(lo_s, v["watchdog_max_min"] * 60), "factor": v["watchdog_factor"],
            "per_hour": int(v["watchdog_per_hour"])}


def capture_dir(argv: list[str], workdir: str | Path) -> str:
    """Where this trunk-recorder writes its calls: captureDir of the config.json argv names (relative: from workdir);
    "" when it can't be read (no watch on call files then)."""
    k = next((i for i, a in enumerate(argv) if a.startswith("--config")), None)
    name = ((argv[k].split("=", 1)[1] if "=" in argv[k] else (argv[k + 1] if k + 1 < len(argv) else ""))
            if k is not None else "config.json")
    try:
        d = json.loads((Path(workdir) / name).read_text()).get("captureDir")
    except (OSError, ValueError, AttributeError):
        return ""
    if not isinstance(d, str) or not d:
        return ""
    return os.path.normpath(d if os.path.isabs(d) else os.path.join(str(workdir), d))


def _dur(s: float) -> str:
    s = max(0, int(s))
    return f"{s} s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h {s % 3600 // 60} min"


def _hhmm(t: float) -> str:
    """The station's clock time of t ("14:32"): when a held start may go (#637)."""
    return time.strftime("%H:%M", time.localtime(t))


def _mmss(s: float) -> str:
    """sizeup/radio/fm.py _mmss()'s m:ss (this file is stdlib only, installed on its own)."""
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


LONG_NUMBER = re.compile(r"(?<![\d.])\d{5,}(?!\d|\.\d)")
NOT_AN_ID = re.compile(r"(?:rate|freq\w*|center|centre|hz|count|pid)\W*$", re.I)


def no_ids(detail) -> str:
    """trunk-recorder's own output lines can name a radio (its ID), so any long whole number in it is left out,
    whatever follows it (a full stop, a comma, a bracket); a frequency or a sample rate ("476075000 Hz",
    "rate: 2400000") stays. The one copy (stdlib: this file is installed on its own): sizeup/cloud/health.py and
    sizeup/radio/systems.py use it. An ID under 5 digits isn't told from a talkgroup or a count: such lines (unit
    grants, "Unit ID set via Control Channel") are never shipped (SHIP_SKIP)."""
    text = str(detail or "")

    def one(m) -> str:
        after = text[m.end():m.end() + 8].lstrip().lower()
        if after.startswith(("mhz", "hz", "khz", "/sec", "sps")) or NOT_AN_ID.search(text[:m.start()][-16:]):
            return m.group(0)                       # a frequency or a rate, by what is written around it
        return "#"
    return LONG_NUMBER.sub(one, text)[:200]


def call_line(line: str) -> tuple[int, str, str] | None:
    """(talkgroup, kind, what) of one of trunk-recorder's call lines that says what it did with a grant (CALL_KINDS),
    else None. Nothing of the line but its talkgroup, its frequency and the recorder counts is kept (no radio ID)."""
    if "TG:" not in line:
        return None
    m = CALL_LINE.search(ANSI.sub("", line))
    if not m:
        return None
    msg = m["msg"]
    for words, kind, what in CALL_KINDS:
        if words in msg:
            if kind == "no_source":
                what = f"{what} {float(m['mhz']):.4f} MHz"
            elif kind == "no_recorder":
                n = NO_RECORDER.search(msg)
                what = f"{what} (priority {n['p']}, {n['n']} free)" if n else f"{what} (none free)"
            return int(m["tg"]), kind, what
    return None


def split_line(line: str) -> tuple[str, str, int | None] | None:
    """(system, kind, call number) of one of the split patch's lines or a call's start (SPLIT_KINDS), else None. Only
    log_header's system and call number are kept: never the radio IDs the line names."""
    if "plit" not in line and "Unit ID changed" not in line and "Starting P25" not in line:
        return None
    for words, kind in SPLIT_KINDS:
        if words in line:
            m = SPLIT_HEAD.search(ANSI.sub("", line))
            return (m["sys"], kind, int(m["call"])) if m else ("", kind, None)
    return None


class SplitTally:
    """One system's split lines counted (split_line): would (log: cuts it would have made), split (on: cuts made),
    flip, alias, calls (recorded), and cut: the calls with at least one would / split, by call number (trunk-recorder
    numbers its calls from 0 at each start: restarted() forgets the numbers, never the counts)."""

    KEYS = ("would", "split", "flip", "alias", "calls", "cut")

    def __init__(self):
        self.n = dict.fromkeys(self.KEYS, 0)
        self._cut: deque[int] = deque(maxlen=64)     # the last calls cut (a call's cuts come close together)

    def add(self, kind: str, call: int | None) -> None:
        self.n[kind] += 1
        if kind in ("would", "split") and (call is None or call not in self._cut):
            self.n["cut"] += 1
            if call is not None:
                self._cut.append(call)

    def restarted(self) -> None:
        self._cut.clear()

    def any(self) -> bool:
        return any(self.n.values())

    def text(self, mode: str | None, when: str) -> str:
        """"split (log) last hour: 3 would split, 1 ID flips, 2 aliases, in 120 calls (2.5% of calls)": counts only; X%
        is the calls with a cut. mode None: "on" if a cut was made, else "log"."""
        n = self.n
        mode = mode if mode in ("log", "on") else ("on" if n["split"] else "log")
        cuts = f"{n['split']} splits" if mode == "on" else f"{n['would']} would split"
        pct = f" ({min(100.0, 100.0 * n['cut'] / n['calls']):.1f}% of calls)" if n["calls"] else ""
        return f"split ({mode}) {when}: {cuts}, {n['flip']} ID flips, {n['alias']} aliases, in {n['calls']} calls{pct}"


def watched_talkgroups(workdir: str | Path) -> dict[int, str]:
    """{talkgroup: its name} of the "watch" list in <workdir>/config.json's "sizeup" "reception" block (a system's,
    #410), named from its talkgroups file's Alpha Tag; {} when there is none or it can't be read."""
    import csv
    base = Path(workdir)
    try:
        conf = json.loads((base / "config.json").read_text())
        watch = ((conf.get("sizeup") or {}).get("reception") or {}).get("watch") or []
        tgs = [int(t) for t in watch if not isinstance(t, bool) and str(t).isdigit()]
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    names: dict[int, str] = {}
    for s in conf.get("systems") if isinstance(conf.get("systems"), list) else []:
        tg_file = s.get("talkgroupsFile") if isinstance(s, dict) else None
        if not tg_file:
            continue
        try:
            with open(base / tg_file, newline="") as f:
                for row in csv.DictReader(f):
                    if str(row.get("Decimal") or "").strip().isdigit():
                        names.setdefault(int(row["Decimal"]), str(row.get("Alpha Tag") or "").strip()[:40])
        except (OSError, ValueError, TypeError):
            pass
    return {t: names.get(t, "") for t in tgs}


class Watchdog:
    """Is a running trunk-recorder still working (#547)? scan() the capture directory, line() its output, fault() the
    reason to restart it now ("" = none), allowed() whether the hour's restarts allow one, restarted() after one.

    Two clocks: the call files' times are the wall clock's (their mtimes, against the wall `now`); everything else
    (its output's lines, the restarts, how long trunk-recorder has run) is time.monotonic()'s `mono`, so a step of the
    wall clock (NTP, WSL2 resyncing after the host slept) is never a decoder silent for that long."""

    def __init__(self):
        self.root = ""
        self.dirs: dict[str, tuple[int, list[str]]] = {}    # directory -> (its mtime, its subdirectories) as last listed
        self.calls: deque[float] = deque()                  # calls it concluded (its output), WATCH_BASE_S back
        self.said: float | None = None                       # ... the last of them
        self.last_call: float | None = None                  # the capture directory's last change: a call file written
        self.scanned: float | None = None
        self._lock = threading.Lock()
        self.restarts: deque[float] = deque()                # watchdog restarts, the last hour (mono)
        self.quiet = 0                                       # call-file restarts since the last new call file
        self.lost_n = 0                                      # control-channel-lost restarts since it last decoded well
        self.cc_ok_since: float | None = None                # (mono) decoding at CC_LOW or more since
        self.last: dict | None = None                        # the last restart: {at, why}
        self.capped = False                                  # the cap said (once per stretch)
        self.key = None
        self.cc_all = False                                  # every decode rate is logged ("controlWarnRate": -1)
        self.cc_at: float | None = None
        self.cc_low_since: float | None = None
        self.cc_last_low: float | None = None

    # -- the calls --------------------------------------------------------------------------------------------------------

    def started(self, now: float, root: str, key) -> None:
        """trunk-recorder (re)started: its capture directory; the control channel judged afresh (and whether all its
        rates are logged, when what runs changed)."""
        if root != self.root:
            self.root, self.dirs, self.last_call, self.scanned = root, {}, None, None
        if key != self.key:
            self.key, self.cc_all = key, False
        self.cc_at = self.cc_low_since = self.cc_last_low = self.cc_ok_since = None
        self.capped = False

    def scan(self, now: float, force: bool = False) -> bool:
        """The capture directory's last change (every WATCH_SCAN_S): its newest call JSON, or the newest change of a
        directory in it. sizeup takes each call's files away seconds after trunk-recorder writes them
        (app.py _drop_recorder_files), so a file is seldom there to see, but its directory changed when it came (and
        when sizeup took it, seconds later). True: something new since the last look. (now: monotonic, for how
        often; the times found are the wall clock's.) Only a new file ends a run of call-file restarts' doubling: a
        trunk-recorder that concludes calls but writes no file is not one that works."""
        if not self.root or (not force and self.scanned is not None and now - self.scanned < WATCH_SCAN_S):
            return False
        self.scanned = now
        newest = float("-inf")
        seen: dict[str, tuple[int, list[str]]] = {}
        todo = [self.root]
        while todo:
            d = todo.pop()
            try:
                m = os.stat(d).st_mtime_ns
            except OSError:
                continue
            if d != self.root:
                newest = max(newest, m / 1e9)                # (the root itself changes only with a new system)
            known = self.dirs.get(d)
            if known is not None and known[0] == m:
                subs = known[1]                              # nothing added or removed here: not listed again
            else:
                subs = []
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    subs.append(e.path)
                                elif e.name.endswith(".json"):
                                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                            except OSError:
                                continue
                except OSError:
                    continue
            seen[d] = (m, subs)
            todo.extend(subs)
        self.dirs = seen
        if newest == float("-inf") or (self.last_call is not None and newest <= self.last_call):
            return False
        self.last_call = newest
        self.quiet = 0
        return True

    def limit(self, cfg: dict) -> tuple[float, str]:
        """(how long without a new call file is a fault, why that long): from the calls trunk-recorder concluded in
        the WATCH_BASE_S before its last one."""
        with self._lock:
            end = self.said
            n = sum(1 for t in self.calls if end is not None and t >= end - WATCH_BASE_S)
        if n >= WATCH_BASE_MIN:
            gap = WATCH_BASE_S / n
            lim = min(max(cfg["factor"] * gap, cfg["min_s"]), cfg["max_s"])
            why = f"{n} calls in the 3 h before the last"
        else:
            lim, why = cfg["max_s"], f"{n} calls in the 3 h before the last: too few for a rate"
        if self.quiet:
            lim = min(lim * 2 ** self.quiet, max(WATCH_DOUBLE_MAX_S, lim))
            why += f"; doubled {self.quiet}× (no call since the last restart)"
        return lim, why

    # -- its output (the drain's thread) -----------------------------------------------------------------------------------

    def line(self, line: str, now: float) -> None:
        if CALL_DONE in line:                                # one per call it recorded (then its files are written)
            with self._lock:
                self.calls.append(now)
                self.said = now
                while self.calls and self.calls[0] < now - WATCH_BASE_S:
                    self.calls.popleft()
            return
        m = CC_RATE.search(line) if "ontrol" in line else None
        if not m:
            return
        try:
            rate = float(m.group("rate"))
        except ValueError:
            return
        self.cc_at = now
        if rate >= CC_WARN:
            self.cc_all = True
        if rate < CC_LOW:
            if self.cc_low_since is None or self.cc_last_low is None or now - self.cc_last_low > CC_RUN_GAP_S:
                self.cc_low_since = now
            self.cc_last_low = now
            self.cc_ok_since = None
        else:
            self.cc_low_since = None
            self.cc_ok_since = self.cc_ok_since or now
            if now - self.cc_ok_since >= CC_LOST_RESTART_S:
                self.lost_n = 0                              # it decodes well again: a restart did fix it

    # -- the verdict -----------------------------------------------------------------------------------------------------

    def lost_limit(self) -> float:
        """How long the control channel may be lost: CC_LOST_RESTART_S, doubled for each restart that didn't bring
        it back (a dead site or antenna: restarting can't fix that), up to WATCH_DOUBLE_MAX_S."""
        return min(CC_LOST_RESTART_S * 2 ** self.lost_n, max(WATCH_DOUBLE_MAX_S, CC_LOST_RESTART_S))

    def fault(self, now: float, mono: float, started: float, started_mono: float, cfg: dict) -> tuple[str, str]:
        """(kind, why) of the reason to restart now; ("", "") for none. now / started: the wall clock (call files);
        mono / started_mono: time.monotonic() (everything else)."""
        if cfg["per_hour"] <= 0:
            return "", ""
        if self.cc_all:
            last = max(self.cc_at or started_mono, started_mono)
            if mono - last >= CC_SILENT_S:
                return "decoder", f"no control channel decode rate logged for {_dur(mono - last)} (decoder hung?)"
        low = self.cc_low_since
        if low is not None and self.cc_last_low is not None and mono - self.cc_last_low <= CC_RUN_GAP_S:
            lost = mono - max(low, started_mono)
            if lost >= self.lost_limit():
                return "control", (f"control channel lost (under {CC_LOW:g} msg/s) for {_dur(lost)}"
                                   + (f" (limit doubled {self.lost_n}×: restarts didn't bring it back)"
                                      if self.lost_n else ""))
        if self.root:
            since = max(self.last_call or started, started)
            lim, why = self.limit(cfg)
            if now - since >= lim:
                return "calls", f"no new call file for {_dur(now - since)} (limit {_dur(lim)}: {why})"
        return "", ""

    def allowed(self, mono: float, cfg: dict) -> bool:
        while self.restarts and mono - self.restarts[0] >= 3600:
            self.restarts.popleft()
        return len(self.restarts) < cfg["per_hour"]

    def restarted(self, now: float, mono: float, kind: str, why: str) -> None:
        self.restarts.append(mono)
        self.last = {"at": now, "why": why}
        if kind == "calls":
            self.quiet += 1
        elif kind == "control":
            self.lost_n += 1

    def dongles_back(self) -> None:
        """Its dongles were unplugged and are back (#592): a fresh start, not one more restart. The hour's restarts and
        the doubled limits (restarts that didn't help, perhaps because the dongle was going) are forgotten, so the cap
        never holds back a trunk-recorder that has only now got its dongles again."""
        self.restarts.clear()
        self.quiet = self.lost_n = 0
        self.capped = False

    def report(self, mono: float, cfg: dict | None) -> dict:
        """For the status: the last call file (wall clock), the limit now, the restarts."""
        lim = self.limit(cfg)[0] if cfg and self.root else None
        with self._lock:
            n = sum(1 for t in self.calls if mono - t < WATCH_BASE_S)
        return {"last_call": self.last_call, "limit_s": lim, "calls_3h": n,
                "restarts_1h": sum(1 for t in self.restarts if mono - t < 3600), "last": self.last,
                "decode_rate_s": round(mono - self.cc_at, 1) if self.cc_at is not None else None}


class Shipper:
    """trunk-recorder's own lines worth seeing, into the journal (#547), filtered and rate-limited: line() each line of
    its output, tick() now and then (the count of lines held back). A watched talkgroup's grants that were NOT
    recorded (watch(): its config's reception "watch" list) are said as what became of them, "<tag>: TG 9212 PD Tac:
    grant (encrypted, not recorded)", at most one per talkgroup per WATCHED_EVERY_S, then "(+N more: ...)": no traffic
    is then told from traffic it can't record. A recorded one is traffic already: never said (systems.Activity counts
    it). The split patch's lines (#647: split_line) are counted per system and said once per SPLIT_EVERY_S and at
    split_flush() (trunk-recorder stopped), "<tag>: split (log) last hour: 3 would split, 1 ID flips, 2 aliases, in 120
    calls (2.5% of calls)": counts only, never a radio ID; nothing while its SIZEUP_TR_SPLIT (started()) is off."""

    def __init__(self, log, tag: str = "p25"):
        self.log, self.tag = log, tag
        self.watched: dict[int, str] = {}           # talkgroup -> its name
        self.calls: dict[int, dict] = {}            # talkgroup -> {"at": its last line, "held": {kind: n}}
        self.win: float | None = None
        self.keys: set[str] = set()
        self.held = 0
        self.rate_low: bool | None = None
        self.rate_said: float | None = None
        self.rates: list[float] = []
        self.split_mode: str | None = None          # SIZEUP_TR_SPLIT of the trunk-recorder that runs (None: not known)
        self.splits: dict[str, SplitTally] = {}     # system (log_header's short name) -> its counts since split_win
        self.split_win: float | None = None
        self._lock = threading.Lock()

    def watch(self, watched: dict[int, str]) -> None:
        with self._lock:
            self.watched = dict(watched)

    def line(self, line: str, now: float) -> None:
        if not line or any(s in line for s in SHIP_SKIP):
            return
        sp = split_line(line)
        if sp is not None:
            if self.split_mode != "off":
                self._split(*sp, now)
            if sp[1] != "calls":                         # a split line names radios: counted, never shipped
                return
        if self.watched and "TG:" in line:
            c = call_line(line)
            if c is not None and c[0] in self.watched:
                if c[1] != "recording":                  # a recorded call is traffic already: counted (Activity) only
                    self._call(*c, now)
                return
        m = CC_RATE.search(line) if "ontrol" in line else None
        if m:
            try:
                self._rate(float(m.group("rate")), now)
            except ValueError:
                pass
            return
        low = line.lower()
        if not (SHIP_LEVELS.search(line) or "control channel" in low or "retun" in low):
            return
        text = re.sub(r"\s+", " ", no_ids(TR_STAMP.sub("", ANSI.sub("", line)))).strip()[:200]
        key = re.sub(r"\d+", "#", text)
        with self._lock:
            self._roll(now)
            if key in self.keys or len(self.keys) >= SHIP_BURST:
                self.held += 1
                return
            self.keys.add(key)
        self.log(f"{self.tag}: trunk-recorder: {text}")

    def _call(self, tg: int, kind: str, what: str, now: float) -> None:
        with self._lock:
            c = self.calls.setdefault(tg, {"at": None, "held": {}})
            if c["at"] is not None and now - c["at"] < WATCHED_EVERY_S:
                c["held"][kind] = c["held"].get(kind, 0) + 1
                return
            more = self._more(c["held"])
            c["at"], c["held"] = now, {}
        self.log(f"{self._tg(tg)}: {what}" + (f" (+{more})" if more else ""))

    def _tg(self, tg: int) -> str:
        name = self.watched.get(tg)
        return f"{self.tag}: TG {tg}" + (f" {name}" if name else "")

    @staticmethod
    def _more(held: dict[str, int]) -> str:
        """"3 more: 2 encrypted, 1 recording" ("" for none)."""
        n = sum(held.values())
        return f"{n} more: " + ", ".join(f"{v} {CALL_SHORT.get(k, k)}" for k, v in
                                         sorted(held.items(), key=lambda kv: -kv[1])) if n else ""

    def _split(self, sys_: str, kind: str, call: int | None, now: float) -> None:
        with self._lock:
            said = self._split_roll(now)
            self.splits.setdefault(sys_, SplitTally()).add(kind, call)
        for x in said:
            self.log(x)

    def _split_roll(self, now: float, force: bool = False) -> list[str]:
        """(under the lock) The window's counts as lines once it is SPLIT_EVERY_S old (force: now, its length said), then
        a new window."""
        if self.split_win is None:
            self.split_win = now
            return []
        span = now - self.split_win
        if span < SPLIT_EVERY_S and not force:
            return []
        when = "last hour" if SPLIT_EVERY_S <= span < SPLIT_EVERY_S + 300 else f"last {_dur(span)}"
        said = []
        for name, t in sorted(self.splits.items()):
            if t.any():
                label = self.tag if not name or self.tag.split()[-1] == name else f"{self.tag} {name}"
                said.append(f"{label}: {t.text(self.split_mode, when)}")
            t.n = dict.fromkeys(t.KEYS, 0)              # the call numbers stay: a call cut across the hour counts once
        self.split_win = now
        return said

    def split_flush(self, now: float) -> None:
        """trunk-recorder stopped: the counts since the last line said now; the next start opens a new window."""
        with self._lock:
            said = self._split_roll(now, force=True) if self.split_win is not None else []
            self.split_win = None
        for x in said:
            self.log(x)

    def tick(self, now: float) -> None:
        said = []
        with self._lock:
            self._roll(now)
            if self.split_win is not None or self.split_mode not in (None, "off"):
                said.extend(self._split_roll(now))
            for tg, c in self.calls.items():            # a burst that went quiet: its count, once its minute is over
                if c["held"] and c["at"] is not None and now - c["at"] >= WATCHED_EVERY_S:
                    said.append(f"{self._tg(tg)}: +{self._more(c['held'])}")
                    c["at"], c["held"] = now, {}
        for x in said:
            self.log(x)

    def _roll(self, now: float) -> None:
        if self.win is not None and now - self.win < SHIP_WINDOW_S:
            return
        if self.held:
            self.log(f"{self.tag}: trunk-recorder: {self.held} more line(s) like these in {_dur(now - self.win)} "
                     f"(trunk-recorder.log)")
        self.win, self.keys, self.held = now, set(), 0

    def _rate(self, rate: float, now: float) -> None:
        self.rates.append(rate)
        low = rate < CC_LOW
        if self.rate_said is None:
            self.log(f"{self.tag}: trunk-recorder: control channel decode rate {rate:g}/s")
        elif low != self.rate_low and now - self.rate_said >= RATE_SAY_S:
            self.log(f"{self.tag}: trunk-recorder: control channel " + (f"lost: {rate:g}/s" if low else f"back: {rate:g}/s"))
        elif now - self.rate_said >= RATE_LOG_S:
            r = self.rates
            under = sum(1 for x in r if x < CC_LOW)
            self.log(f"{self.tag}: trunk-recorder: control channel {rate:g}/s (last {_dur(now - self.rate_said)}: "
                     f"{min(r):g}-{max(r):g}/s" + (f", {under} of {len(r)} under {CC_LOW:g}/s" if under else "") + ")")
        else:
            return
        self.rate_said, self.rate_low, self.rates = now, low, []

    def started(self, split: str | None = None, now: float | None = None) -> None:
        """trunk-recorder (re)started: its first decode rate is said again; split: its SIZEUP_TR_SPLIT (another than
        the last one's: the counts so far said first, as the last one's)."""
        self.rate_said = self.rate_low = None
        self.rates = []
        if split != self.split_mode and now is not None:
            self.split_flush(now)
        with self._lock:
            self.split_mode = split
            for t in self.splits.values():
                t.restarted()


def wanted(path: str = STATE) -> dict:
    """{on, sdr, name, pid, seq, problem}: what sizeup-station asked for (nothing asked: off)."""
    try:
        w = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        w = None
    if not isinstance(w, dict):
        return {"on": False, "sdr": "", "name": "", "pid": None, "seq": None, "problem": ""}
    sdr = w.get("sdr") if isinstance(w.get("sdr"), str) else ""
    pid = w.get("pid") if isinstance(w.get("pid"), int) and not isinstance(w.get("pid"), bool) else None
    seq = w.get("seq") if isinstance(w.get("seq"), int) and not isinstance(w.get("seq"), bool) else None
    problem = "" if not sdr or (len(sdr) <= 32 and set(sdr) <= SERIAL_CHARS) else f"not a dongle serial: {sdr[:40]!r}"
    return {"on": w.get("on") is True, "sdr": sdr, "name": str(w.get("name") or sdr)[:60], "pid": pid, "seq": seq,
            "problem": problem}


def installed(argv: list[str]) -> bool:
    return bool(argv) and Path(argv[0]).is_file() and os.access(argv[0], os.X_OK)


# -- the dongles (librtlsdr's USB strings: nothing is opened; adsbd.py's lookup) --------------------------------------------

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


_USB_LOCK = threading.Lock()
USB_LOCK_S = 10.0                 # another thread's lookup this long: this one gives up (None: can't look now)
UNREAD = "?"                      # the serial of a dongle whose strings couldn't be read (never a serial: SERIAL_CHARS)


def devices() -> list[tuple[int, str]] | None:
    """[(index, serial)] plugged in now; None when librtlsdr can't be loaded (or another lookup hangs). A dongle whose
    strings can't be read is (index, UNREAD): there, serial unknown (#592: a look with one is trusted less); one that
    has no serial string is (index, ""), like any other dongle that isn't ours. One lookup at a time in this process
    (the county's supervisor and the other systems' thread both look), as sizeup/radio/sdr.py's _lock does in
    sizeup-station, with a timeout so a hung one never holds the county's back; across processes none is needed:
    each lookup is its own libusb context and only reads descriptors, and sizeup-station has always looked while
    trunk-recorder runs."""
    lib = _load()
    if lib is None or not _USB_LOCK.acquire(timeout=USB_LOCK_S):
        return None
    out = []
    try:
        for i in range(int(lib.rtlsdr_get_device_count())):
            m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
            ok = lib.rtlsdr_get_device_usb_strings(i, m, p, s) == 0
            out.append((i, s.value.decode(errors="replace") if ok else UNREAD))
    finally:
        _USB_LOCK.release()
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


# -- the shared claims (sizeup/radio/claims.py's format; stdlib only here) -----------------------------------------------------


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


def claim(serial: str, me: int, handed: int | None = None, d: Path | None = None, mode: str = MODE) -> dict | None:
    """Hold this dongle's claim as `me`. A live claim of sizeup's own by `handed` (the sizeup-station process that handed
    the dongle over) is taken over; anyone else's is left alone and returned (nothing written). None: it is ours."""
    c = holder(serial, d)
    if c is not None and not (c.get("app") == APP and _pid(c) in (me, handed)):
        return c
    d = d or claims_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{serial}.{me}.tmp"
        tmp.write_text(json.dumps({"app": APP, "pid": me, "mode": mode, "since": time.time()}))
        os.replace(tmp, d / f"{serial}.json")
    except OSError:
        pass                          # can't record it: still run (the dongle itself is the real lock)
    return None


def release(serial: str, me: int, d: Path | None = None, mode: str | None = None) -> None:
    """Remove our own claim (never anyone else's; mode: only one written for that job, #409: the county's
    supervisor and another system's share this process and its pid)."""
    c = holder(serial, d)
    if c is not None and c.get("app") == APP and _pid(c) == me and (mode is None or c.get("mode") == mode):
        try:
            ((d or claims_dir()) / f"{serial}.json").unlink()
        except OSError:
            pass


def _pid(c: dict) -> int:
    try:
        return int(c.get("pid", -1))
    except (TypeError, ValueError):
        return -1


# -- the supervisor --------------------------------------------------------------------------------------------------------


class Supervisor:
    """One step every STEP_S (step(now)); all state in self.status. popen / found / killpg / pid are for the tests."""

    def __init__(self, config: str = CONFIG, state: str = STATE, status_path: str = STATUS, claims: Path | None = None,
                 popen=subprocess.Popen, found=devices, killpg=os.killpg, pid: int | None = None,
                 log=lambda m: print(m, flush=True), mono=time.monotonic):
        self.config, self.state, self.status_path, self.claims = config, state, status_path, claims
        self.popen, self.found, self.killpg, self.log = popen, found, killpg, log
        self.mono = mono                            # time.monotonic: the watchdog's clock (#547)
        self._m = 0.0                               # its reading at this step
        self.started_mono: float | None = None
        self.pid = os.getpid() if pid is None else pid
        self.proc: subprocess.Popen | None = None
        self.running: tuple | None = None          # (sdr, argv, workdir) of the trunk-recorder that runs
        self.claimed = ""
        self.started_at: float | None = None
        self.next_start = 0.0
        self.backoff = BACKOFF_S[0]
        self.restarts = 0
        self.lines: deque[str] = deque(maxlen=30)
        self.said = ""                              # its last line worth showing
        self._drain_thread: threading.Thread | None = None
        self.status: dict = {"state": "off", "detail": "", "since": None}
        self.watchdog = Watchdog()                  # #547: running but no longer working: restarted
        self.shipper = Shipper(self.log, self.tag)  # #547: its own lines worth seeing, into the journal
        self._wcfg: dict | None = None              # the watchdog's limits ([p25] watchdog_*), as last read

    tag = "p25"                 # its lines in the journal

    def _set(self, now: float, state: str, detail: str, **kw) -> None:
        changed = state != self.status.get("state")
        if changed:
            self.log(f"{self.tag}: {state}" + (f": {detail}" if detail else ""))
        self.status = {"state": state, "detail": detail, "since": now if changed else self.status.get("since"), **kw}

    def step(self, now: float | None = None, mono: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._m = self.mono() if mono is None else mono
        r, w = recorder(self.config), wanted(self.state)
        self._wcfg = r.get("watch")
        key = (w["sdr"], tuple(r["argv"]), r["workdir"]) if w["on"] else None
        if self.proc is not None and self.running != key:
            why = ("sizeup switched Fire/EMS 15 off" if not w["on"] else
                   f"Fire/EMS 15 moved to {w['name']}" if self.running and self.running[0] != w["sdr"] else
                   "[p25] recorder or recorder_dir changed")
            self.stop(why)
            self.backoff, self.next_start = BACKOFF_S[0], 0.0
        if self.claimed and self.claimed != (w["sdr"] if w["on"] else ""):
            release(self.claimed, self.pid, self.claims)
            self.claimed = ""
        if self.proc is None and key is not None and getattr(self, "asked_sdr", None) not in (None, w["sdr"]):
            # Fire/EMS 15 given another dongle while it waited (absent: ABSENT_S): the old one's wait isn't this one's
            self.backoff, self.next_start = BACKOFF_S[0], 0.0
        self.asked_sdr = w["sdr"] if key is not None else getattr(self, "asked_sdr", None)
        base = {"on": w["on"], "sdr": w["sdr"], "name": w["name"], "seq": w["seq"], "pid": self.pid,
                "restarts": self.restarts, "workdir": r["workdir"]}
        if not w["on"]:
            self._set(now, "off", "no dongle on Fire/EMS 15 (sizeup: Settings > Radios)", **base)
        elif w["problem"]:
            self._set(now, "down", w["problem"], **base)
        elif r["problem"]:
            self._set(now, "absent", r["problem"], **base)
        elif not installed(r["argv"]):
            self._set(now, "absent", f"trunk-recorder not installed ({r['argv'][0]})", **base)
        elif self.proc is None:
            self._start(now, w, r, key, base)
        else:
            self._watch(now, base)
        self.status["watchdog"] = self.watchdog.report(self._m, self._wcfg)
        self.status["at"] = now
        self._write()
        return self.status

    def _start(self, now: float, w: dict, r: dict, key: tuple, base: dict) -> None:
        name = w["name"] or w["sdr"]
        if w["sdr"] and self.claimed != w["sdr"]:
            c = claim(w["sdr"], self.pid, w["pid"], self.claims)
            if c is not None:
                self._set(now, "held", f"{c.get('app', 'another app')} holds {name}"
                                       + (f" ({c.get('mode')})" if c.get("mode") else "")
                                       + ": not started (switch it Off there to hand it over)",
                          held_by={"app": c.get("app"), "mode": c.get("mode")}, **base)
                return
            self.claimed = w["sdr"]
        if now < self.next_start:
            self.status.update(base)
            return
        idx = None
        try:
            found = self.found()
        except Exception as e:  # noqa: BLE001 - librtlsdr in a bad way: like not plugged in
            found = None
            self.log(f"p25: dongle lookup failed: {type(e).__name__}: {e}")

        def ambiguous(serial: str) -> str:
            same = [i for i, x in (found or []) if x == serial]
            return duplicate_text(serial, same) if len(same) > 1 else ""
        if w["sdr"]:
            idx = resolve(w["sdr"], found)
            if idx is None:
                self.next_start = now + UNPLUGGED_S
                if ambiguous(w["sdr"]):       # #242: never a guess between two dongles with one serial
                    self._set(now, "ambiguous", f"SDR '{name}': {ambiguous(w['sdr'])}", **base)
                else:
                    self._set(now, "absent", "librtlsdr not found" if found is None else f"SDR '{name}' not plugged in",
                              **base)
                return
        left_out: list[str] = []
        try:
            argv = runtime_argv(r["argv"], r["workdir"], idx, None if w["sdr"] else ambiguous, left_out)
        except AmbiguousSource as e:
            self.next_start = now + ABSENT_S
            self._set(now, "ambiguous", f"not started: {e}", **base)
            return
        except (OSError, ValueError) as e:
            self.next_start = now + ABSENT_S
            self._set(now, "down", f"cannot write trunk-recorder config: {e}", **base)
            return
        # trunk-recorder runs ffmpeg / sox by name for call audio
        env = dict(os.environ)
        env["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "") or "/usr/bin:/bin"])
        env[SPLIT_ENV] = split_mode(r["argv"], r["workdir"])
        try:
            self.proc = self.popen(argv, cwd=r["workdir"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True, env=env)
        except OSError as e:
            self.restarts += 1
            self.backoff = min(self.backoff * 2, BACKOFF_S[1])
            self.next_start = now + self.backoff
            self._set(now, "down", f"cannot start trunk-recorder: {e}", **dict(base, restarts=self.restarts))
            return
        self.running, self.started_at, self.started_mono = key, now, self._m
        self.lines.clear()
        self.said = ""
        self.watchdog.started(now, capture_dir(argv, r["workdir"]), key)
        self.shipper.started(env[SPLIT_ENV], self._m)
        if getattr(self.proc, "stdout", None) is not None:
            self._drain_thread = threading.Thread(target=self._drain, args=(self.proc, r["workdir"]),
                                                  name="trunk-recorder-output", daemon=True)
            self._drain_thread.start()
        where = f"{name} (device #{idx})" if w["sdr"] else "the dongle its config.json names"
        self.log(f"p25: trunk-recorder on {where}, pid {self.proc.pid}")
        if left_out:
            self.log(f"p25: source(s) left out: {'; '.join(left_out)}")
        self._set(now, "ok", "trunk-recorder starting" + (f" (left out: {'; '.join(left_out)})" if left_out else ""),
                  tr_pid=self.proc.pid, index=idx, argv=argv, started=now, **base)

    def _watch(self, now: float, base: dict) -> None:
        keep = {k: self.status.get(k) for k in ("tr_pid", "index", "argv", "started")}
        code = self.proc.poll()
        if code is None:
            if self.started_at is not None and now - self.started_at > 60:
                self.backoff = BACKOFF_S[0]
            self.shipper.tick(self._m)                  # (the drain's clock: time.monotonic, not the wall's)
            if self._watchdog(now, base):
                return
            self._set(now, "ok", self.said or "trunk-recorder starting", **keep, **base)
            return
        ran = now - (self.started_at or now)
        self.proc, self.running, self.started_at = None, None, None
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2)       # its last lines say why it stopped
        tail = " ".join(list(self.lines)[-5:])
        if any(m in tail for m in UNPLUGGED):
            # the dongle went (bench, 2026-10-01): say so plainly, look again every ABSENT_S, no log spam
            self.next_start = now + ABSENT_S
            self._set(now, "absent", "dongle not connected (unplugged?)", **base)
            return
        self.restarts += 1
        self.backoff = BACKOFF_S[0] if ran > 60 else min(self.backoff * 2, BACKOFF_S[1])
        self.next_start = now + self.backoff
        detail = f"trunk-recorder exited ({code}): " + (self.lines[-1] if self.lines else "")
        self.log(f"p25: {detail}; trying again in {self.backoff:.0f} s")
        self._set(now, "down", detail, **dict(base, restarts=self.restarts))

    def _watchdog(self, now: float, base: dict) -> bool:
        """#547: restart a trunk-recorder that runs but has stopped working (Watchdog.fault), within the hour's cap. True:
        it was stopped (the next step starts it again)."""
        cfg, wd = self._wcfg, self.watchdog
        if not cfg or self.started_at is None:
            return False
        wd.scan(self._m)
        kind, why = wd.fault(now, self._m, self.started_at, self.started_mono, cfg)
        if not why:
            wd.capped = False
            return False
        if not wd.allowed(self._m, cfg):
            if not wd.capped:
                wd.capped = True
                self.log(f"{self.tag}: watchdog: {why}; not restarted: {cfg['per_hour']} restart(s) in the last hour")
            return False
        self.log(f"{self.tag}: watchdog: {why}: restarting trunk-recorder "
                 f"({len(wd.restarts) + 1} of {cfg['per_hour']} this hour)")
        wd.restarted(now, self._m, kind, why)
        self.stop(f"watchdog: {why}")
        self.restarts += 1
        self.backoff, self.next_start = BACKOFF_S[0], 0.0
        self._set(now, "down", f"watchdog restarted trunk-recorder: {why}", **dict(base, restarts=self.restarts))
        return True

    def _drain(self, p, workdir: str) -> None:
        """Everything it says, into trunk-recorder.log (capped: LOG_MAX_BYTES, one previous file kept), which sizeup reads
        as it grows; the unit script's grant lines come the same way (its stdout is trunk-recorder's)."""
        try:
            self.shipper.watch(watched_talkgroups(workdir))  # its config's reception "watch" list, as it starts
        except Exception:  # noqa: BLE001 - a config it can't read watches nothing; the drain must still run
            pass
        path = Path(workdir) / LOG
        log = None
        try:
            log = open(path, "a", buffering=1)
        except OSError:
            pass
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if log is not None:
                try:
                    log.write(line + "\n")
                    if log.tell() > LOG_MAX_BYTES:
                        log.close()
                        os.replace(path, path.with_suffix(".log.1"))
                        log = open(path, "a", buffering=1)
                except (OSError, ValueError):
                    log = None
            if line:
                self.lines.append(line)
                low = line.lower()
                if any(w in low for w in SAYS):
                    self.said = line[-160:]
                try:
                    t = self.mono()                  # the watchdog's and the shipper's tick()'s clock
                    self.watchdog.line(line, t)
                    self.shipper.line(line, t)
                except Exception:  # noqa: BLE001 - the drain must never stop (trunk-recorder would block on its output)
                    pass
        if log is not None:
            try:
                log.close()
            except OSError:
                pass

    def stop(self, why: str = "") -> None:
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                self.killpg(p.pid, signal.SIGTERM)           # its process group: the unit scripts it runs too
                p.wait(timeout=STOP_S)
            except Exception:  # noqa: BLE001 - it must go
                try:
                    self.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
            self.log("p25: trunk-recorder stopped" + (f" ({why})" if why else ""))
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2)
            self._drain_thread = None
        self.running, self.started_at = None, None
        try:
            self.shipper.split_flush(self.mono())       # (#647) its split counts so far, after its last lines
        except Exception:  # noqa: BLE001 - a count, never in the way of a stop
            pass

    def shutdown(self, why: str = "the service is stopping") -> None:
        self.stop(why)
        if self.claimed:
            release(self.claimed, self.pid, self.claims)
            self.claimed = ""
        self.status.update(state="off", detail=f"the trunk-recorder service stopped ({why})", tr_pid=None,
                           at=time.time())
        self._write()

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


# -- more systems (#409): <recorder_dir>/systems/<name>/ ------------------------------------------------------------------------

SYSTEMS = "systems"
SYSTEM_NAME = re.compile(r"[a-z][a-z0-9_-]{0,23}")
RUNTIME_TALKGROUPS = "talkgroups.runtime.csv"
SIZEUP_COLUMNS = ("Tier", "Service", "Department")    # talkgroups.csv columns that are sizeup's own: not handed on
EDGE_HZ = 12_500                      # a channel this close to a source's edge isn't covered


def system_dirs(workdir: str | Path) -> dict[str, Path]:
    """{name: its directory} for every <workdir>/systems/<name>/config.json, by name."""
    base = Path(workdir) / SYSTEMS
    try:
        names = sorted(p.name for p in base.iterdir() if SYSTEM_NAME.fullmatch(p.name) and (p / "config.json").is_file())
    except OSError:
        return {}
    return {n: base / n for n in names}


def system_state_path(name: str, state: str = STATE) -> str:
    p = Path(state)
    return str(p.with_name(f"{p.stem}.{name}{p.suffix}"))


def system_status_path(name: str, status: str = STATUS) -> str:
    p = Path(status)
    return str(p.with_name(f"{p.stem}.{name}{p.suffix}"))


def covers(source: dict, hz: float) -> bool:
    """This source (center, rate) takes in a channel at hz."""
    try:
        return abs(float(hz) - float(source["center"])) <= float(source["rate"]) / 2 - EDGE_HZ
    except (KeyError, TypeError, ValueError):
        return False


def sources_sdr(sources: dict) -> str:
    """{source index: {sdr}} as one string ("0=serial-a,1=serial-b"): what a system runs on, for comparing."""
    return ",".join(f"{i}={sources[i].get('sdr', '')}" for i in sorted(sources))


def system_runtime_argv(argv: list[str], workdir: str | Path, devs: dict[int, int | str]) -> list[str]:
    """argv with --config pointing at config.runtime.json, written in workdir (a system's directory) from its
    config.json: only the sources in devs ({source index: the dongle's current index}), each on its dongle; each
    system's control channels only where one of those covers them (ValueError when none is); the unit script; the
    talkgroups file without sizeup's own columns; no "sizeup" block (sizeup's side of the file)."""
    base = Path(workdir)
    conf = json.loads((base / "config.json").read_text())
    conf.pop("sizeup", None)
    sources = []
    for i, s in enumerate(conf.get("sources", [])):
        if i in devs:
            sources.append(dict(s, device=f"rtl={devs[i]}"))
    if not sources:
        raise ValueError("no source of config.json has a dongle")
    conf["sources"] = sources
    for s in conf.get("systems", []):
        listed = list(s.get("control_channels") or [])
        cc = [f for f in listed if any(covers(src, f) for src in sources)]
        if listed and not cc:
            raise ValueError(f"[{s.get('shortName', '?')}] none of its {len(listed)} control channels is covered by "
                             "the dongle(s) it has")
        s["control_channels"] = cc
        cmd = unit_script_command(base, s.get("unitScript"))
        if cmd:
            s["unitScript"] = cmd
        else:
            s.pop("unitScript", None)
        tg = s.get("talkgroupsFile")
        if tg and (base / tg).is_file():
            s["talkgroupsFile"] = _runtime_talkgroups(base / tg)
    out = base / "config.runtime.json"
    out.write_text(json.dumps(conf, indent=2))
    return [*without_config(argv), f"--config={out.name}"]


def without_config(argv: list[str]) -> list[str]:
    """argv with its --config=<file> (or --config <file>) left out."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "--config":
            skip = True
        elif not a.startswith("--config="):
            out.append(a)
    return out


def _runtime_talkgroups(src: Path) -> str:
    """talkgroups.csv as trunk-recorder gets it (talkgroups.runtime.csv beside it): sizeup's own columns left out."""
    import csv
    import io
    rows = list(csv.reader(io.StringIO(src.read_text())))
    drop = {i for i, h in enumerate(rows[0]) if h.strip() in SIZEUP_COLUMNS} if rows else set()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow([c for i, c in enumerate(r) if i not in drop])
    out = src.with_name(RUNTIME_TALKGROUPS)
    out.write_text(buf.getvalue())
    return out.name


def wanted_system(path: str) -> dict:
    """{on, sources: {index: {sdr, mode, name}}, label, pid, seq, problem, until}: what sizeup-station asked of one
    system. until (#410): a time after which it is off whatever else the file says: a reception test's dongle is let
    go by this service itself when sizeup-station never comes back to end the test."""
    try:
        w = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        w = None
    off = {"on": False, "sources": {}, "label": "", "pid": None, "seq": None, "problem": "", "until": None,
           "group": False}
    if not isinstance(w, dict):
        return off
    pid = w.get("pid") if isinstance(w.get("pid"), int) and not isinstance(w.get("pid"), bool) else None
    seq = w.get("seq") if isinstance(w.get("seq"), int) and not isinstance(w.get("seq"), bool) else None
    sources, problem = {}, ""
    raw = w.get("sources") if isinstance(w.get("sources"), dict) else {}
    for k, v in raw.items():
        sdr = v.get("sdr") if isinstance(v, dict) and isinstance(v.get("sdr"), str) else ""
        if not str(k).isdigit() or not sdr or len(sdr) > 32 or not set(sdr) <= SERIAL_CHARS:
            problem = f"not a source and its dongle serial: {str(k)[:8]!r}: {str(sdr)[:40]!r}"
            continue
        sources[int(k)] = {"sdr": sdr, "mode": str(v.get("mode") or "P25")[:60], "name": str(v.get("name") or sdr)[:60]}
    if len({s["sdr"] for s in sources.values()}) != len(sources):
        problem = "one dongle on two sources"
    until = w.get("until")
    until = float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) and until > 0 else None
    return {"on": w.get("on") is True and bool(sources or problem), "sources": sources,
            "label": str(w.get("label") or "")[:60], "pid": pid, "seq": seq, "problem": problem, "until": until,
            # an antenna group's automatic move (sizeup/radio/groups.py), not a person's or a reception test's switch
            "group": w.get("group") is True}


_SIGS: dict[str, tuple] = {}      # path -> ((mtime, size), its content's digest)


def _file_sig(*paths: Path) -> tuple:
    """What these files hold (a digest, read again only when a file's time or size changed): an update that writes
    the same config.json again restarts nothing."""
    import hashlib
    out = []
    for p in paths:
        try:
            st = p.stat()
            seen = _SIGS.get(str(p))
            if seen is None or seen[0] != (st.st_mtime_ns, st.st_size):
                seen = _SIGS[str(p)] = ((st.st_mtime_ns, st.st_size), hashlib.sha1(p.read_bytes()).hexdigest())
            out.append((p.name, seen[1]))
        except OSError:
            out.append((p.name, ""))
    return tuple(out)


CONTEXT_NICE = 5       # a context system's trunk-recorder (#409): under the county's (the service is Nice=-5)


def lower_priority(pid: int, setpriority=None) -> None:
    """Nice CONTEXT_NICE for the process just started (after Popen, by its pid: no code runs between fork and exec)."""
    try:
        (setpriority or os.setpriority)(os.PRIO_PROCESS, pid, CONTEXT_NICE)
    except (OSError, AttributeError, ValueError):
        pass


class SystemSupervisor(Supervisor):
    """One more system's trunk-recorder (#409): the sources of <recorder_dir>/systems/<name>/config.json that sizeup
    gave a dongle (wanted_system), each dongle claimed under its own job. Restarted when its dongles, its config.json
    or its talkgroups.csv change; never because of the county's. Every dongle it is asked to use must be free and
    plugged in: one held by another app, or gone, and nothing starts (said in the status). One unplugged while it
    runs stops it ("absent"); it starts again, with "dongles back after m:ss", once every one is back (#592:
    _unplugged, _coming_back)."""

    def __init__(self, name: str, config: str = CONFIG, state: str = STATE, status_path: str = STATUS, **kw):
        super().__init__(config, system_state_path(name, state), system_status_path(name, status_path), **kw)
        self.county_state = state                    # the county's own wish: its dongle is never this system's
        self.county_status = status_path             # ... and what it runs (#592: the county starts first)
        self.setpriority = None                      # os.setpriority (the tests': a recorder)
        self.name = name
        self.tag = self.shipper.tag = f"p25 {name}"
        self.claims_held: dict[str, str] = {}        # serial -> the mode it is claimed under
        # #592: its dongles unplugged under it, and back
        self.ran_key = None                          # what last ran (a dongle missing then is one that went)
        self.gone_since: float | None = None         # when a dongle it ran on went (None: none went)
        self.next_look = 0.0                         # when to look on USB again while it runs
        self.missing: list[str] = []                 # the dongles missing at the last look ...
        self.missing_n = 0                           # ... at this many looks in a row
        self.back_list: tuple | None = None          # the (index, serial) list since back_since, while it comes back
        self.back_since = 0.0
        self.back_first: float | None = None         # its first look with every dongle back
        self.back_first_m: float | None = None       # ... on the monotonic clock (the county's wait: a clock step
                                                     # never lengthens it)
        self.back_at: float | None = None            # its last start after a return
        self.flaps = 0                               # returns in a row it went again soon after (FLAP_S)
        self.hold_until = 0.0                        # a flapping dongle's: no start before this
        self.county_wait: tuple | None = None        # (#637) (a group move's wish seq, since when, monotonic)
        self.county_done = None                      # ... the wish seq it has waited for (once per move, never again)

    def _recorder(self) -> dict:
        r = recorder(self.config)
        if r["problem"]:
            return r
        d = Path(r["workdir"]) / SYSTEMS / self.name
        return {"argv": [*without_config(r["argv"]), "--config=config.json"], "workdir": str(d),
                "problem": "" if (d / "config.json").is_file() else f"no {SYSTEMS}/{self.name}/config.json",
                "watch": r.get("watch")}

    def _let_go(self, keep: set[str] = frozenset()) -> None:
        for sd in [s for s in self.claims_held if s not in keep]:
            release(sd, self.pid, self.claims, self.claims_held[sd])     # only the claim written for that job
            del self.claims_held[sd]

    def _taken(self, s: dict, handed: int | None) -> dict | None:
        """Why this dongle can't be this system's now, as a claim ({app, mode}); None: free (or already ours for
        this job). The county's trunk-recorder runs under this same process: a dongle its state names, or one
        claimed under another job (by this pid or by sizeup-station), is someone else's, never taken over; only a
        claim this supervisor itself wrote for this system (claims_held) is its own under any of its jobs."""
        county = wanted(self.county_state)
        if county["on"] and county["sdr"] == s["sdr"]:
            return {"app": APP, "mode": MODE}
        c = holder(s["sdr"], self.claims)
        if c is None:
            return None
        if s["sdr"] in self.claims_held and c.get("app") == APP and _pid(c) == self.pid \
                and c.get("mode") == self.claims_held[s["sdr"]]:
            # this system's own claim, written here for another of its jobs (#410: the next side of a reception
            # test, "MRRS 770" then "MRRS 773" on the one dongle): kept and renamed, the dongle never free between
            return None
        ours = c.get("app") == APP and _pid(c) in (self.pid, handed)
        return None if ours and c.get("mode") == s["mode"] else c

    def step(self, now: float | None = None, mono: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._m = self.mono() if mono is None else mono
        r, w = self._recorder(), wanted_system(self.state)
        self._wcfg = r.get("watch")
        expired = w["on"] and w["until"] is not None and now >= w["until"]
        if expired:                                  # (#410) a reception test's deadline: stopped, its dongle let go
            w = dict(w, on=False)
        d = Path(r["workdir"]) if r["workdir"] else Path(".")
        sdr = sources_sdr(w["sources"])
        key = (sdr, tuple(r["argv"]), r["workdir"], _file_sig(d / "config.json", d / "talkgroups.csv")) if w["on"] else None
        label = w["label"] or self.name
        if self.proc is not None and self.running != key:
            self.stop(f"sizeup switched {label} off" if not w["on"] else f"{label}: its dongles or its config changed")
            self.backoff, self.next_start = BACKOFF_S[0], 0.0
        county = wanted(self.county_state)
        if self.proc is not None and county["on"] and county["sdr"] and county["sdr"] in self.claims_held:
            # The county's trunk-recorder was given one of this system's dongles: this one stops at once and lets
            # every dongle go (never two programs on one dongle; fire has it). _start_system then says "held".
            self.stop(f"{MODE} takes {county['name'] or county['sdr']}")
            self._let_go()
            self.backoff, self.next_start = BACKOFF_S[0], 0.0
        self._let_go({s["sdr"] for s in w["sources"].values()} if w["on"] else set())
        base = {"on": w["on"], "sdr": sdr, "name": label, "system": self.name, "seq": w["seq"], "pid": self.pid,
                "restarts": self.restarts, "workdir": r["workdir"]}
        if self.ran_key is not None and key != self.ran_key and self.proc is None:
            # (#592) another assignment or config: whatever went before is not this one's to come back, nor to wait for.
            # An antenna group's move (the wish says "group") keeps a flapping dongle's record (flaps, hold_until,
            # back_at): moving to a stand-in and back is no way around its hold. A person's switch or a reception
            # test's loan starts fresh.
            self.ran_key, self.gone_since, self.back_list, self.back_first = None, None, None, None
            if not w.get("group"):
                self.back_at, self.flaps, self.hold_until = None, 0, 0.0
            self.backoff, self.next_start = BACKOFF_S[0], 0.0
        if expired:
            self._set(now, "off", f"{label}: its reception test's time is up: stopped, its dongle let go", expired=True,
                      **base)
        elif not w["on"]:
            self._set(now, "off", f"no dongle on {label} (sizeup: Settings > Radios)", **base)
        elif w["problem"]:
            self._set(now, "down", w["problem"], **base)
        elif r["problem"]:
            self._set(now, "absent", r["problem"], **base)
        elif not installed(r["argv"]):
            self._set(now, "absent", f"trunk-recorder not installed ({r['argv'][0]})", **base)
        elif self.proc is None:
            self._start_system(now, w, r, key, base)
        elif not self._unplugged(now, w, base):
            self._watch(now, base)
            if self.proc is None and self.status.get("state") == "absent" and self.gone_since is None:
                # trunk-recorder exited saying its dongle is gone: a return as any other (settled, the county first)
                self.ran_key = key
                went = self._went(now)
                self.status.update(detail=self.status.get("detail", "") + went, restarts=self.restarts)
        self.status["watchdog"] = self.watchdog.report(self._m, self._wcfg)
        self.status["at"] = now
        self._write()
        return self.status

    def _unplugged(self, now: float, w: dict, base: dict) -> bool:
        """#592: a dongle unplugged under a running trunk-recorder need not make it exit: it can stay up, saying and
        recording nothing (the station, 2026-11-16: both MRRS dongles out, its status "ok" until an assignment changed).
        So while it runs, every UNPLUGGED_S, its dongles are looked for on USB; the same ones missing at two looks a
        second apart stop it ("absent", True), and _start_system starts it again once every one is back. Never on a
        look that can't be made (librtlsdr in a bad way). One trusted less takes three looks: a list with none of its
        dongles (an empty one: libusb failing), or one with as many dongles whose strings couldn't be read (UNREAD) as
        it misses (they may be its own). A dongle with no serial string ("") is another dongle, never a reason.
        Gone again within FLAP_S of a return: a restart counted, and the next start held back (FLAP_HOLD_S)."""
        if now < self.next_look:
            return False
        try:
            found = self.found()
        except Exception:  # noqa: BLE001 - can't look: judged at the next look
            found = None
        mine = [s for _k, s in sorted(w["sources"].items())]
        missing = [s["name"] for s in mine if s["sdr"] not in {x for _i, x in found or []}]
        if found is None or not missing:
            self.missing, self.missing_n, self.next_look = [], 0, now + UNPLUGGED_S
            return False
        unread = sum(1 for _i, x in found if x == UNREAD)
        self.missing_n = self.missing_n + 1 if missing == self.missing else 1
        self.missing = missing
        if self.missing_n < (3 if len(missing) == len(mine) or unread >= len(missing) else 2):
            self.next_look = now + 1.0                # looked at again in a second: never stopped on one look
            return False
        names = ", ".join(f"'{n}'" for n in missing)
        was = self.running
        self.stop(f"{w['label'] or self.name}: SDR {names} unplugged")
        self.missing, self.missing_n, self.next_look = [], 0, 0.0
        self.ran_key = was
        self.backoff, self.next_start = BACKOFF_S[0], 0.0
        detail = f"SDR {names} unplugged: trunk-recorder stopped until it is back" + self._went(now)
        self._set(now, "absent", detail, **dict(base, restarts=self.restarts))
        return True

    def _went(self, now: float) -> str:
        """(#592) A dongle it ran on went (now): when, to say at its return. Gone again within FLAP_S of a return is a
        flapping dongle (a loose plug, a failing hub port): a restart counted, and the next start held back, longer
        each time in a row (FLAP_HOLD_S), never one every few seconds. What to add to the status's detail."""
        self.gone_since, self.back_list, self.back_first = now, None, None
        back, self.back_at = self.back_at, None
        if back is None or now - back >= FLAP_S:
            self.flaps, self.hold_until = 0, 0.0
            return ""
        hold = FLAP_HOLD_S[min(self.flaps, len(FLAP_HOLD_S) - 1)]
        self.flaps += 1
        self.restarts += 1
        self.hold_until = now + hold
        return f" (again {_mmss(now - back)} after it came back: started no sooner than {_dur(hold)})"

    def _start_system(self, now: float, w: dict, r: dict, key: tuple, base: dict) -> None:
        # All or nothing, looked at before anything is written (no claim file made and removed every step while one
        # dongle is held): every dongle free, then each claimed under its own job.
        for i in sorted(w["sources"]):
            s = w["sources"][i]
            c = self._taken(s, w["pid"])
            if c is not None:
                self._let_go()
                self._set(now, "held", f"{c.get('app', 'another app')} holds {s['name']}"
                                       + (f" ({c.get('mode')})" if c.get("mode") else "")
                                       + ": not started (switch it Off there to hand it over)",
                          held_by={"app": c.get("app"), "mode": c.get("mode")}, **base)
                return
        for i in sorted(w["sources"]):
            s = w["sources"][i]
            if self.claims_held.get(s["sdr"]) == s["mode"]:
                continue
            c = claim(s["sdr"], self.pid, w["pid"], self.claims, s["mode"])
            if c is not None:                        # taken between the look and the claim
                self._let_go()
                self._set(now, "held", f"{c.get('app', 'another app')} holds {s['name']}: not started",
                          held_by={"app": c.get("app"), "mode": c.get("mode")}, **base)
                return
            self.claims_held[s["sdr"]] = s["mode"]
        if self.gone_since is None and now < self.hold_until:
            # a flapping dongle's hold (_went) holds a start on other dongles too (an antenna group's stand-in)
            self.next_start = min(self.hold_until, now + UNPLUGGED_S)
            self._set(now, "absent", self._held(), hold_until=self.hold_until, **dict(base, restarts=self.restarts))
            return
        if now < self.next_start:
            self.status.update(base)
            return
        try:
            found = self.found()
        except Exception as e:  # noqa: BLE001 - librtlsdr in a bad way: like not plugged in
            found = None
            self.log(f"{self.tag}: dongle lookup failed: {type(e).__name__}: {e}")
        devs: dict[int, int | str] = {}
        for i in sorted(w["sources"]):
            s = w["sources"][i]
            idx = resolve(s["sdr"], found)
            if idx is None:
                same = [k for k, x in (found or []) if x == s["sdr"]]
                self.next_start = now + UNPLUGGED_S
                went = ""
                if found is not None and not same and self.gone_since is None and self.ran_key == key:
                    went = self._went(now)       # (#592) it ran on it, and it went (trunk-recorder exited)
                if len(same) > 1:
                    self._set(now, "ambiguous", f"SDR '{s['name']}': {duplicate_text(s['sdr'], same)}", **base)
                else:
                    self._set(now, "absent", "librtlsdr not found" if found is None
                              else f"SDR '{s['name']}' not plugged in" + went, **dict(base, restarts=self.restarts))
                return
            devs[i] = idx
        if self.gone_since is not None:
            if self._coming_back(now, w, found, base):     # (its own wait for the county: never both)
                return
        elif w.get("group") and self._county_first(now, w, base):
            return                                         # (#637) a group move: the county first, once per move
        try:
            argv = system_runtime_argv(r["argv"], r["workdir"], devs)
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.next_start = now + ABSENT_S
            self._set(now, "down", f"cannot write trunk-recorder config: {e}", **base)
            return
        env = dict(os.environ)
        env["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "") or "/usr/bin:/bin"])
        env[SPLIT_ENV] = split_mode(r["argv"], r["workdir"])
        try:
            self.proc = self.popen(argv, cwd=r["workdir"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True, env=env)
            lower_priority(self.proc.pid, self.setpriority)
        except OSError as e:
            self.restarts += 1
            self.backoff = min(self.backoff * 2, BACKOFF_S[1])
            self.next_start = now + self.backoff
            self._set(now, "down", f"cannot start trunk-recorder: {e}", **dict(base, restarts=self.restarts))
            return
        self.running, self.started_at, self.started_mono = key, now, self._m
        self.lines.clear()
        self.said = ""
        self.ran_key = key
        self.county_wait, self.county_done = None, w["seq"]   # (#637) this move's wait is over: never again for it
        if self.gone_since is not None:
            # (#592) as an rtl_fm channel's "dongle '…' back after m:ss"; never one of the watchdog's capped restarts
            self.log(f"{self.tag}: dongles back after {_mmss(now - self.gone_since)}: restarting trunk-recorder")
            if now - self.gone_since > LONG_ABSENCE_S:
                self.watchdog.dongles_back()
            self.gone_since, self.back_list, self.back_first, self.back_at = None, None, None, now
        self.missing, self.missing_n, self.next_look = [], 0, now + UNPLUGGED_S
        self.watchdog.started(now, capture_dir(argv, r["workdir"]), key)
        self.shipper.started(env[SPLIT_ENV], self._m)
        if getattr(self.proc, "stdout", None) is not None:
            self._drain_thread = threading.Thread(target=self._drain, args=(self.proc, r["workdir"]),
                                                  name="trunk-recorder-system-output", daemon=True)
            self._drain_thread.start()
        on = ", ".join(f"{w['sources'][i]['name']} (device #{devs[i]})" for i in sorted(devs))
        self.log(f"{self.tag}: trunk-recorder on {on}, pid {self.proc.pid}")
        self._set(now, "ok", "trunk-recorder starting", tr_pid=self.proc.pid, index=devs[min(devs)], argv=argv,
                  started=now, **base)

    def _held(self) -> str:
        """(#637) A flapping dongle's hold, said as the time it ends: a held start is no fault (Receivers, Health)."""
        return f"held back until {_hhmm(self.hold_until)} after dropouts"

    def _county_first(self, now: float, w: dict, base: dict) -> bool:
        """(#637) An antenna group's move (the wish says "group"): when the county's trunk-recorder should run, it
        starts first, as at a return (_coming_back): this system's start waits until the county's status is "ok" on
        the dongle its wish names and it has run COUNTY_SETTLE_S, at most COUNTY_WAIT_S from the move (the county
        down for its own reasons). A group restore that gives the county its dongle back in the same step as this
        system's therefore opens the county's first. A county already up waits for nothing. Once per move (its wish's
        seq): a crash restart or a dongle's return later on the same wish never waits for it again (a return has its own
        wait, _coming_back), and a later restore of the same dongles (a new seq) waits again. Looked at only once its
        dongles are found (one not plugged in says so first). The cap on the monotonic clock. True while it waits."""
        county = wanted(self.county_state)
        if w["seq"] is not None and w["seq"] == self.county_done:
            return False
        if not county["on"] or not county["sdr"]:
            self.county_wait = None
            return False
        if self.county_wait is None or self.county_wait[0] != w["seq"]:
            self.county_wait = (w["seq"], self._m)
        if self._m - self.county_wait[1] >= COUNTY_WAIT_S:
            self.county_done = w["seq"]
            return False
        st = read_status(self.county_status, now) or {}
        started = st.get("started")
        if st.get("state") == "ok" and st.get("sdr") == county["sdr"] and isinstance(started, (int, float)) \
                and now - started >= COUNTY_SETTLE_S:
            return False
        self.next_start = now + 1.0
        self._set(now, "absent", f"not started yet ({MODE} starts first)", **dict(base, restarts=self.restarts))
        return True

    def _coming_back(self, now: float, w: dict, found: list, base: dict) -> bool:
        """(#592) Every dongle it ran on is back: True while it must still wait. USB settles first (the same (index,
        serial) list at looks RETURN_STABLE_S apart: indexes shift as dongles come back one by one); a flapping dongle
        waits out its hold (_went); and the county's trunk-recorder, when it should run, starts first and has run
        COUNTY_SETTLE_S (up to COUNTY_WAIT_S): a hub that dropped both comes back with new indexes, and the county's
        dongle is the one to open first. The indexes it starts on are this very look's (_start_system).
        Only the dongles up to the highest index of its own and the county's count for settling (only those can shift
        their indexes): a spare further up that comes and goes never holds it back; and settling ends after
        RETURN_SETTLE_MAX_S whatever moves (a power-starved hub), on the latest look."""
        county = wanted(self.county_state)
        ours = {s["sdr"] for s in w["sources"].values()} | ({county["sdr"]} if county["on"] and county["sdr"] else set())
        top = max((i for i, x in found if x in ours), default=-1)
        listed = tuple(sorted(e for e in found if e[0] <= top))
        if self.back_first is None:
            self.back_first, self.back_first_m = now, self._m
        if listed != self.back_list:
            self.back_list, self.back_since = listed, now
        names = ", ".join(f"'{s['name']}'" for _k, s in sorted(w["sources"].items()))
        why = ""
        if now - self.back_since < RETURN_STABLE_S and now - self.back_first < RETURN_SETTLE_MAX_S:
            why = "USB settling"
            self.next_start = min(self.back_since + RETURN_STABLE_S, self.back_first + RETURN_SETTLE_MAX_S)
        elif now < self.hold_until:
            why = self._held()
            self.next_start = min(self.hold_until, now + UNPLUGGED_S)
        else:
            since = self.back_first_m if self.back_first_m is not None else self._m
            if county["on"] and self._m - since < COUNTY_WAIT_S:
                st = read_status(self.county_status, now) or {}
                started = st.get("started")
                up = st.get("state") == "ok" and isinstance(started, (int, float)) and now - started >= COUNTY_SETTLE_S
                if not up:
                    why, self.next_start = f"{MODE} starts first", now + 1.0
        if not why:
            return False
        held = {"hold_until": self.hold_until} if why == self._held() else {}
        self._set(now, "absent", f"SDR {names} back: not started yet ({why})", **held,
                  **dict(base, restarts=self.restarts))
        return True

    def shutdown(self, why: str = "the service is stopping") -> None:
        self.stop(why)
        self._let_go()
        self.status.update(state="off", detail=f"the trunk-recorder service stopped ({why})", tr_pid=None,
                           at=time.time())
        self._write()


def read_status(path: str | None = None, now: float | None = None) -> dict | None:
    """The service's last status (sizeup-station: the P25 row, the station health); None when there is none. One older
    than STALE_S says so: the service isn't running."""
    try:
        st = json.loads(Path(path or STATUS).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    now = time.time() if now is None else now
    if not isinstance(st.get("at"), (int, float)) or now - st["at"] > STALE_S:
        age = f"for {now - st['at']:.0f} s " if isinstance(st.get("at"), (int, float)) else ""
        st = dict(st, state="down", stale=True, detail=f"the trunk-recorder service hasn't reported {age}"
                                                       f"(systemctl status {UNIT})")
    return st


def step_systems(more: dict, workdir: str, log=lambda m: None, make=None) -> None:
    """One step of every other system under <workdir>/systems (a new directory gets its supervisor; one that went is
    shut down). Each on its own: one failing never stops the next."""
    names = system_dirs(workdir) if workdir else {}
    for n in [n for n in more if n not in names]:
        more.pop(n).shutdown("its directory is gone")
    for n in names:
        if n not in more:
            more[n] = (make or SystemSupervisor)(n)
            log(f"p25 {n}: supervised (off until sizeup-station gives it a dongle)")
        try:
            more[n].step()
        except Exception as e:  # noqa: BLE001
            log(f"p25 {n}: {type(e).__name__}: {e}")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "run"
    if cmd == "print":
        r, w = recorder(), wanted()
        if r["problem"] or not w["on"] or w["problem"]:
            print(r["problem"] or w["problem"] or "sizeup has no dongle on Fire/EMS 15: nothing runs")
            return 0
        idx = resolve(w["sdr"], devices()) if w["sdr"] else None
        print(" ".join(shlex.quote(a) for a in r["argv"]) + f"   (in {r['workdir']}; sources: "
              + (f"rtl={idx}, {w['sdr']}" if idx is not None else f"{w['sdr']} isn't plugged in now" if w["sdr"]
                 else "as configured") + ")")
        return 0
    if cmd == "status":
        st = read_status()
        print(json.dumps(st, indent=2) if st else f"no status yet (is {UNIT} running?)")
        for n in system_dirs(recorder()["workdir"] or "."):
            print(f"-- {n}")
            print(json.dumps(read_status(system_status_path(n)), indent=2))
        return 0
    if cmd != "run":
        print("usage: trunkrecd.py run | print | status", file=sys.stderr)
        return 2
    s = Supervisor()
    stopping = []

    def bye(*_a):
        stopping.append(1)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    s.log(f"p25: trunk-recorder service up; it runs what sizeup-station asks in {STATE}")
    more: dict[str, SystemSupervisor] = {}          # the other systems (#409), as their directories are there

    def others() -> None:
        """In a thread of their own: stopping one of their trunk-recorders (up to STOP_S) never holds up the county's."""
        while not stopping:
            try:
                step_systems(more, recorder()["workdir"], s.log)
            except Exception as e:  # noqa: BLE001 - another system never stops the county's
                s.log(f"p25 systems: {type(e).__name__}: {e}")
            time.sleep(STEP_S)
    side = threading.Thread(target=others, name="p25-systems", daemon=True)
    side.start()
    try:
        while not stopping:
            try:
                s.step()
            except Exception as e:  # noqa: BLE001 - supervision must never die
                s.log(f"p25: {type(e).__name__}: {e}")
            for _ in range(int(STEP_S * 10)):
                if stopping:
                    break
                time.sleep(0.1)
    finally:
        side.join(timeout=STOP_S + 2)
        for x in list(more.values()):
            try:
                x.shutdown()
            except Exception:  # noqa: BLE001
                pass
        s.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
