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
"seq"}), status (status.<name>.json), log and dongle claims (SystemSupervisor). It runs only the sources of its
config.json that sizeup gave a dongle, with the control channels those cover; nothing asked, nothing runs. The county's
trunk-recorder above never restarts for another system's sake.

Supervision (as recorder.py did): an exit restarts it with backoff (2 s doubling to 30 s; back to 2 s after a minute of
running); "Wrong rtlsdr device index" / "No supported devices" mean the dongle is gone: "absent", looked for again every
ABSENT_S without counting a restart."""

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
STOP_S = 8.0                      # SIGTERM, then this long, then SIGKILL
STALE_S = 15.0                    # a status older than this: the service isn't running


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
    return {"argv": argv, "workdir": workdir, "problem": ""}


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
                 log=lambda m: print(m, flush=True)):
        self.config, self.state, self.status_path, self.claims = config, state, status_path, claims
        self.popen, self.found, self.killpg, self.log = popen, found, killpg, log
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

    tag = "p25"                 # its lines in the journal

    def _set(self, now: float, state: str, detail: str, **kw) -> None:
        changed = state != self.status.get("state")
        if changed:
            self.log(f"{self.tag}: {state}" + (f": {detail}" if detail else ""))
        self.status = {"state": state, "detail": detail, "since": now if changed else self.status.get("since"), **kw}

    def step(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        r, w = recorder(self.config), wanted(self.state)
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
        try:
            self.proc = self.popen(argv, cwd=r["workdir"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True, env=env)
        except OSError as e:
            self.restarts += 1
            self.backoff = min(self.backoff * 2, BACKOFF_S[1])
            self.next_start = now + self.backoff
            self._set(now, "down", f"cannot start trunk-recorder: {e}", **dict(base, restarts=self.restarts))
            return
        self.running, self.started_at = key, now
        self.lines.clear()
        self.said = ""
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

    def _drain(self, p, workdir: str) -> None:
        """Everything it says, into trunk-recorder.log (capped: LOG_MAX_BYTES, one previous file kept), which sizeup reads
        as it grows; the unit script's grant lines come the same way (its stdout is trunk-recorder's)."""
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
    """{on, sources: {index: {sdr, mode, name}}, label, pid, seq, problem}: what sizeup-station asked of one system."""
    try:
        w = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        w = None
    off = {"on": False, "sources": {}, "label": "", "pid": None, "seq": None, "problem": ""}
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
    return {"on": w.get("on") is True and bool(sources or problem), "sources": sources,
            "label": str(w.get("label") or "")[:60], "pid": pid, "seq": seq, "problem": problem}


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
    plugged in: one held by another app, or gone, and nothing starts (said in the status)."""

    def __init__(self, name: str, config: str = CONFIG, state: str = STATE, status_path: str = STATUS, **kw):
        super().__init__(config, system_state_path(name, state), system_status_path(name, status_path), **kw)
        self.county_state = state                    # the county's own wish: its dongle is never this system's
        self.setpriority = None                      # os.setpriority (the tests': a recorder)
        self.name = name
        self.tag = f"p25 {name}"
        self.claims_held: dict[str, str] = {}        # serial -> the mode it is claimed under

    def _recorder(self) -> dict:
        r = recorder(self.config)
        if r["problem"]:
            return r
        d = Path(r["workdir"]) / SYSTEMS / self.name
        return {"argv": [*without_config(r["argv"]), "--config=config.json"], "workdir": str(d),
                "problem": "" if (d / "config.json").is_file() else f"no {SYSTEMS}/{self.name}/config.json"}

    def _let_go(self, keep: set[str] = frozenset()) -> None:
        for sd in [s for s in self.claims_held if s not in keep]:
            release(sd, self.pid, self.claims, self.claims_held[sd])     # only the claim written for that job
            del self.claims_held[sd]

    def _taken(self, s: dict, handed: int | None) -> dict | None:
        """Why this dongle can't be this system's now, as a claim ({app, mode}); None: free (or already ours for
        this job). The county's trunk-recorder runs under this same process: a dongle its state names, or one
        claimed under another job (by this pid or by sizeup-station), is someone else's, never taken over."""
        county = wanted(self.county_state)
        if county["on"] and county["sdr"] == s["sdr"]:
            return {"app": APP, "mode": MODE}
        c = holder(s["sdr"], self.claims)
        if c is None:
            return None
        ours = c.get("app") == APP and _pid(c) in (self.pid, handed)
        return None if ours and c.get("mode") == s["mode"] else c

    def step(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        r, w = self._recorder(), wanted_system(self.state)
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
        if not w["on"]:
            self._set(now, "off", f"no dongle on {label} (sizeup: Settings > Radios)", **base)
        elif w["problem"]:
            self._set(now, "down", w["problem"], **base)
        elif r["problem"]:
            self._set(now, "absent", r["problem"], **base)
        elif not installed(r["argv"]):
            self._set(now, "absent", f"trunk-recorder not installed ({r['argv'][0]})", **base)
        elif self.proc is None:
            self._start_system(now, w, r, key, base)
        else:
            self._watch(now, base)
        self.status["at"] = now
        self._write()
        return self.status

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
                if len(same) > 1:
                    self._set(now, "ambiguous", f"SDR '{s['name']}': {duplicate_text(s['sdr'], same)}", **base)
                else:
                    self._set(now, "absent", "librtlsdr not found" if found is None
                              else f"SDR '{s['name']}' not plugged in", **base)
                return
            devs[i] = idx
        try:
            argv = system_runtime_argv(r["argv"], r["workdir"], devs)
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.next_start = now + ABSENT_S
            self._set(now, "down", f"cannot write trunk-recorder config: {e}", **base)
            return
        env = dict(os.environ)
        env["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "") or "/usr/bin:/bin"])
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
        self.running, self.started_at = key, now
        self.lines.clear()
        self.said = ""
        if getattr(self.proc, "stdout", None) is not None:
            self._drain_thread = threading.Thread(target=self._drain, args=(self.proc, r["workdir"]),
                                                  name=f"trunk-recorder-{self.name}-output", daemon=True)
            self._drain_thread.start()
        on = ", ".join(f"{w['sources'][i]['name']} (device #{devs[i]})" for i in sorted(devs))
        self.log(f"{self.tag}: trunk-recorder on {on}, pid {self.proc.pid}")
        self._set(now, "ok", "trunk-recorder starting", tr_pid=self.proc.pid, index=devs[min(devs)], argv=argv,
                  started=now, **base)

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
