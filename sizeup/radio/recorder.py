"""Supervised trunk-recorder process (Fire/EMS 15 on the county P25 system).

trunk-recorder owns its dongle and writes one WAV + JSON per call into a capture directory,
which radio/p25.py ingests. This only keeps the process running: start, stop (frees the
dongle for another receiver on a one-dongle bench), restart with backoff if it dies.

It runs a copy of its config.json, config.runtime.json, with the dongle sizeup handed it and the
unit script (unit-event.sh next to the config) on every system: trunk-recorder then prints each
voice grant's radio ID to its output the moment the control channel sends it (radio/live.py).

Two ways to run it, one face to the app (sizeup/app.py):
    TrunkRecorder   a child of sizeup (the Mac, a bench): it stops and starts with sizeup.
    UnitRecorder    the Linux station (GitHub #71; sizeup-station.service sets SIZEUP_TRUNK_RECORDER_UNIT=1):
                    trunk-recorder runs under its own service, sizeup-trunk-recorder (radio/trunkrecd.py), which
                    a restart of sizeup doesn't touch. sizeup hands it the dongle through a state file, reads
                    its status, follows its log for the live talkers, and on its own exit leaves it running:
                    the next sizeup adopts the trunk-recorder that runs.

A dongle serial two dongles share (GitHub #242) is never handed to it: the app's lookup gives no index for it (the
recorder waits, state "ambiguous", and says why), and a source config.json names by such a serial ("rtl=00000001")
is left out of config.runtime.json (ambiguous_fn); with every source left out it doesn't start at all.

The control channel (GitHub #258), from trunk-recorder's own output (ControlChannel, `cc` on both): every 3 s it
checks how many control channel messages it decoded and, under its warn rate (10/sec), logs

    [westchester]   Control Channel Message Decode Rate: 0/sec, count:  0

and retunes to the next control channel under 2/sec ("Retuning to Control Channel: ..."). A healthy system logs none
of these, so a run of them under CC_LOW (2/sec), each within CC_RUN_GAP_S of the last, is the control channel lost:
its start goes to the hub in the station health (sizeup/cloud/health.py: the "P25 trunk silent" alert).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from sizeup.procenv import child_env
from sizeup.radio import claims, trunkrecd
from sizeup.radio.trunkrecd import (UNIT_SCRIPT, UNIT_SCRIPT_MAX, AmbiguousSource, rtl_serial,  # noqa: F401
                                    runtime_argv, unit_script_command)

LOG_MAX_BYTES = 5_000_000
UNIT_ENV = "SIZEUP_TRUNK_RECORDER_UNIT"     # "1": trunk-recorder is its own service (deploy/station/systemd)
CC_RATE = re.compile(r"Control Channel Message Decode Rate:\s*(?P<rate>[\d.]+)\s*/\s*sec", re.I)
CC_RETUNE = re.compile(r"Retuning to Control Channel", re.I)
CC_LOW = 2.0                # messages / s under this: trunk-recorder can't hold the control channel (it retunes)
CC_RUN_GAP_S = 15.0         # it says so every 3 s while it lasts: no such line this long, the run is over


class ControlChannel:
    """The control channel's decode rate as trunk-recorder logs it (feed() each line of its output). status():
    {"rate": the last rate it logged (None: none logged: fine), "low_since": the start of the current run under
    CC_LOW (None: not lost), "last_low": that run's last line, "retunes": count, "last_retune": when}."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.rate: float | None = None
        self.at: float | None = None             # when the last rate line came
        self.low_since: float | None = None
        self.last_low: float | None = None
        self.retunes = 0
        self.last_retune: float | None = None

    def feed(self, line: str) -> None:
        if "ontrol" not in line:
            return
        now = self.clock()
        m = CC_RATE.search(line)
        if m:
            try:
                rate = float(m.group("rate"))
            except ValueError:
                return
            self.rate, self.at = rate, now
            if rate < CC_LOW:
                if self.low_since is None or self.last_low is None or now - self.last_low > CC_RUN_GAP_S:
                    self.low_since = now
                self.last_low = now
            else:
                self.low_since = None
        elif CC_RETUNE.search(line):
            self.retunes += 1
            self.last_retune = now

    def status(self) -> dict:
        now = self.clock()
        lost = self.low_since is not None and self.last_low is not None and now - self.last_low <= CC_RUN_GAP_S
        if not lost:
            self.low_since = None
        recent = self.at is not None and now - self.at <= CC_RUN_GAP_S
        return {"rate": self.rate if recent else None, "low_since": self.low_since, "last_low": self.last_low,
                "retunes": self.retunes, "last_retune": self.last_retune}


def make_system(name: str, label: str, command: str, workdir: str,
                on_event: Callable[[str], None] = lambda m: None) -> SystemRecorder:
    """The recorder of one more system (#409; workdir: its own directory, <recorder_dir>/systems/<name>)."""
    if os.environ.get(UNIT_ENV) == "1":
        return SystemUnitRecorder(name, label, command, workdir, on_event)
    return SystemRecorder(name, label, command, workdir, on_event)


def make(command: str, workdir: str, on_event: Callable[[str], None] = lambda m: None) -> TrunkRecorder:
    """The recorder for this process: the station's service when its unit says so, else a child of sizeup."""
    if os.environ.get(UNIT_ENV) == "1":
        return UnitRecorder(command, workdir, on_event)
    return TrunkRecorder(command, workdir, on_event)


class TrunkRecorder:
    def __init__(self, command: str, workdir: str, on_event: Callable[[str], None] = lambda m: None):
        self.argv = shlex.split(os.path.expanduser(command))
        self.workdir = workdir
        self.on_event = on_event
        self.state, self.detail, self.restarts = "off", "", 0
        self.last_lines: deque[str] = deque(maxlen=30)
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Set by the app when it hands trunk-recorder a dongle: () -> current index, or None if
        # that dongle isn't plugged in. The source device in a generated config.runtime.json
        # becomes "rtl=<index>" (never the serial: osmosdr reads all-digit serials as indexes).
        self.device_fn: Callable[[], int | str | None] | None = None
        self.device_name = ""
        # Set by the app: serial -> why it can't be opened now (two dongles share it), "" if fine. A source of
        # config.json that names such a serial is left out of config.runtime.json (self.left_out says which).
        self.ambiguous_fn: Callable[[str], str] | None = None
        self.left_out: list[str] = []
        # Every line of its output, as it comes (sizeup/radio/live.py reads who is keying up from it).
        self.on_line: Callable[[str], None] | None = None
        self.cc = ControlChannel()              # the control channel's decode rate, from the same lines (#258)

    unit = False                # trunk-recorder is sizeup's child
    lower = False               # run trunk-recorder at a lower priority (SystemRecorder: a context system's)

    def _argv_for(self, dev: int | str | None = None) -> list[str]:
        """argv with --config pointing at a copy of its config (config.runtime.json) whose sources
        use dongle `dev` (None: as configured) and whose systems run the unit script."""
        self.left_out = []
        return runtime_argv(self.argv, self.workdir, dev, self.ambiguous_fn, self.left_out)

    def on(self) -> str | None:
        """The dongle it runs on (or waits for) now, None when it isn't running."""
        alive = self._thread is not None and self._thread.is_alive()
        return getattr(self.device_fn, "serial", None) if alive else None

    def shutdown(self) -> None:
        """sizeup is exiting: its child goes with it."""
        self.stop()

    @property
    def installed(self) -> bool:
        exe = self.argv[0] if self.argv else ""
        return bool(exe) and Path(exe).is_file() and os.access(exe, os.X_OK)

    def one_dongle(self) -> tuple[bool, str]:
        """(can run now on the one dongle sizeup hands it, why not). sizeup points every source in
        config.json at that dongle, so a config with more than one source would need more dongles
        than a job gets. The shipped config (deploy/trunk-recorder/config.json) has one: 2.4 MS/s
        at 477.19375 MHz covers the control channels 476.075-476.2375 and the voice channels."""
        if not self.installed:
            return False, f"trunk-recorder not installed ({self.argv[0] if self.argv else '?'})"
        argv = self.argv
        k = next((i for i, a in enumerate(argv) if a.startswith("--config")), None)
        name = ((argv[k].split("=", 1)[1] if "=" in argv[k] else (argv[k + 1] if k + 1 < len(argv) else ""))
                if k is not None else "config.json")
        try:
            n = len(json.loads((Path(self.workdir) / name).read_text()).get("sources", []))
        except (OSError, ValueError, AttributeError):
            return True, ""                      # no config of ours to read: run as configured
        if n > 1:
            return False, f"trunk-recorder's {name} has {n} sources: one dongle can't run it"
        return True, ""

    def start(self) -> TrunkRecorder:
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="trunk-recorder", daemon=True)
        self._thread.start()
        return self

    def stop(self, reason: str = "off") -> None:
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=8)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=3)
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=10)
        self.state, self.detail = "off", reason

    def status(self) -> dict:
        return {"label": "trunk-recorder", "mhz": None, "state": self.state, "detail": self.detail,
                "open": False, "quieting_db": None, "restarts": self.restarts, "last_heard": None}

    def _drain(self, p: subprocess.Popen) -> None:
        log = None
        try:  # everything trunk-recorder says, for diagnosis (gitignored)
            log = open(Path(self.workdir) / "trunk-recorder.log", "a", buffering=1)
        except OSError:
            pass
        path = Path(self.workdir) / "trunk-recorder.log"
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if log:
                log.write(line + "\n")
                try:  # capped: 5 MB, one previous file kept
                    if log.tell() > LOG_MAX_BYTES:
                        log.close()
                        os.replace(path, path.with_suffix(".log.1"))
                        log = open(path, "a", buffering=1)
                except OSError:
                    log = None
            if line:
                self.cc.feed(line)
            if line and self.on_line is not None:
                try:
                    self.on_line(line)
                except Exception as e:  # the live indicator must never stop the drain
                    self.detail = f"live talker parse error: {type(e).__name__}: {e}"
            if line:
                self.last_lines.append(line)
                low = line.lower()
                if "control channel" in low or "decoding" in low or "error" in low or "starting" in low:
                    self.detail = line[-160:]

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            if not self.installed:
                self.state, self.detail = "absent", f"trunk-recorder not installed ({self.argv[0] if self.argv else '?'})"
                self._stop.wait(15)
                continue
            dev = None
            if self.device_fn is not None:
                dev = self.device_fn()
                if dev is None:
                    why = getattr(self.device_fn, "why", "")
                    self.state, self.detail = ("ambiguous", f"SDR '{self.device_name}': {why}") if why else (
                        "absent", f"SDR '{self.device_name}' not plugged in")
                    self._stop.wait(15.0)
                    continue
            try:
                argv = self._argv_for(dev)
            except AmbiguousSource as e:
                if self.state != "ambiguous":
                    self.on_event(f"trunk-recorder not started: {e}")
                self.state, self.detail = "ambiguous", f"not started: {e}"
                self._stop.wait(15.0)
                continue
            except (OSError, ValueError) as e:
                self.state, self.detail = "down", f"cannot write trunk-recorder config: {e}"
                self._stop.wait(15.0)
                continue
            try:
                # trunk-recorder runs ffmpeg by name for call audio: make sure Homebrew's bin is on PATH.
                env = child_env()
                env["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "")])
                self._proc = subprocess.Popen(argv, cwd=self.workdir, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, start_new_session=True, env=env)
                if self.lower:
                    trunkrecd.lower_priority(self._proc.pid)
            except OSError as e:
                self.state, self.detail = "down", f"cannot start trunk-recorder: {e}"
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            self.state, self.detail = "ok", "trunk-recorder starting"
            if self.left_out:
                self.detail += f" (left out: {'; '.join(self.left_out)})"
                self.on_event(f"trunk-recorder: source(s) left out: {'; '.join(self.left_out)}")
            drain = threading.Thread(target=self._drain, args=(self._proc,), daemon=True)
            drain.start()
            started = time.monotonic()
            while not self._stop.is_set() and self._proc.poll() is None:
                self._stop.wait(1.0)
            if self._stop.is_set():
                break
            drain.join(2.0)                 # its last words read before we judge them (an unplugged dongle, not a crash)
            tail = " ".join(list(self.last_lines)[-5:])
            if "Wrong rtlsdr device index" in tail or "No supported devices" in tail or "No matching devices" in tail:
                # Dongle unplugged (bench, 2026-10-01): say so plainly, look again every 15 s, no log spam.
                self.state, self.detail = "absent", "dongle not connected (unplugged?)"
                self._stop.wait(15.0)
                continue
            self.restarts += 1
            self.state = "down"
            self.detail = f"trunk-recorder exited ({self._proc.returncode}): " + (self.last_lines[-1] if self.last_lines else "")
            self.on_event(self.detail)
            backoff = 2.0 if time.monotonic() - started > 60 else min(backoff * 2, 30)
            self._stop.wait(backoff)


class UnitRecorder(TrunkRecorder):
    """trunk-recorder under its own service (sizeup-trunk-recorder.service, sizeup/radio/trunkrecd.py), with
    TrunkRecorder's face to the app. Nothing here needs privilege: the service runs as the sizeup user and reads the
    state file this writes (trunkrecd.STATE), the way the ADS-B decoder reads state/adsb_receiver.json.

        start()      hand it the dongle (device_fn.serial; "" = config.json as it is): it runs within STEP_S
        stop()       Off: returns once the service says trunk-recorder is stopped and the dongle's claim is gone
                     (or after wait_s), so the next program can open the dongle
        shutdown()   sizeup is exiting (an update, a restart): trunk-recorder is left running, the next sizeup
                     adopts it by handing it the same dongle again (the service sees nothing to change)
        status()     the service's own status (trunkrecd.STATUS)

    Its output comes from trunk-recorder.log as the service writes it (from the end at start: nothing said before this
    process is replayed), line by line to on_line (radio/live.py), the unit script's grant lines included. The claim
    on its dongle is the service's while it runs; sizeup counts it as its own (claims.helpers)."""

    unit = True

    def __init__(self, command: str, workdir: str, on_event: Callable[[str], None] = lambda m: None,
                 state_path: str | None = None, status_path: str | None = None, poll_s: float = 0.1,
                 wait_s: float = 20.0):
        super().__init__(command, workdir, on_event)
        self.state_path = Path(state_path or trunkrecd.STATE)
        self.status_path = Path(status_path or trunkrecd.STATUS)
        self.poll_s, self.wait_s = poll_s, wait_s
        self.serial: str | None = None                 # handed over by this process ("" = as configured); None = off
        self._seq = 0
        self._tail_stop = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._helper: tuple[float, set[int]] = (0.0, set())
        claims.helpers = self.helper_pids

    # -- the handover -------------------------------------------------------------------------------------------------

    def _write_state(self, on: bool, sdr: str = "") -> int:
        self._seq = max(self._seq + 1, time.time_ns())
        w = {"on": on, "sdr": sdr, "name": (self.device_name or sdr) if on else "", "pid": os.getpid(),
             "seq": self._seq, "at": time.time()}
        p = self.state_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}")
        tmp.write_text(json.dumps(w))
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
        return self._seq

    def asked(self) -> dict:
        """What the service was last asked (by this process or the one before it)."""
        return trunkrecd.wanted(str(self.state_path))

    def start(self) -> UnitRecorder:
        sdr = str(getattr(self.device_fn, "serial", "") or "") if self.device_fn is not None else ""
        self._follow()                                 # before the handover: its first lines are read too
        try:
            self._write_state(True, sdr)
        except OSError as e:
            self.state, self.detail = "down", f"cannot hand Fire/EMS 15 to {trunkrecd.UNIT}: {e}"
            self.on_event(self.detail)
            return self
        self.serial = sdr
        return self

    def stop(self, reason: str = "off") -> None:
        before = self.asked()
        self.serial = None
        try:
            seq = self._write_state(False)
        except OSError as e:
            self.on_event(f"p25: cannot tell {trunkrecd.UNIT} to stop: {e}")
            return
        self.state, self.detail = "off", reason
        if not before["on"]:
            return                                     # nothing was running
        end = time.monotonic() + self.wait_s
        while time.monotonic() < end:
            st = trunkrecd.read_status(str(self.status_path))
            if st is None or st.get("stale"):
                return                                 # the service isn't running: nothing holds the dongle
            if st.get("seq") == seq and st.get("state") == "off":
                return
            time.sleep(self.poll_s)
        self.on_event(f"p25: {trunkrecd.UNIT} didn't stop trunk-recorder within {self.wait_s:.0f} s ({reason})")

    def shutdown(self) -> None:
        """sizeup is exiting: trunk-recorder keeps running (the next sizeup adopts it); only the log stops being read."""
        self._tail_stop.set()
        if self._tail_thread is not None:
            self._tail_thread.join(timeout=2)
            self._tail_thread = None

    def on(self) -> str | None:
        return self.serial or None

    def helper_pids(self) -> set[int]:
        """The service's pid, whose dongle claim is sizeup's own (claims.helpers); read at most once a second."""
        at, pids = self._helper
        if time.monotonic() - at < 1.0:
            return pids
        st = trunkrecd.read_status(str(self.status_path))
        pid = st.get("pid") if st else None
        pids = {pid} if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else set()
        self._helper = (time.monotonic(), pids)
        return pids

    def status(self) -> dict:
        st = trunkrecd.read_status(str(self.status_path))
        if st is None:
            self.state, self.detail = "down", f"the trunk-recorder service isn't running (systemctl status {trunkrecd.UNIT})"
        else:
            s = str(st.get("state") or "")
            self.state = {"held": "absent"}.get(s, s if s in ("ok", "off", "absent", "ambiguous", "down", "starting")
                                                else "down")
            self.detail = str(st.get("detail") or "")
            try:
                self.restarts = int(st.get("restarts") or 0)
            except (TypeError, ValueError):
                self.restarts = 0
            if self.serial is not None and not st.get("stale") and (not st.get("on") or st.get("sdr") != self.serial):
                self.state, self.detail = "starting", f"handing Fire/EMS 15 to {trunkrecd.UNIT}"
        return {"label": "trunk-recorder", "mhz": None, "state": self.state, "detail": self.detail,
                "open": False, "quieting_db": None, "restarts": self.restarts, "last_heard": None}

    # -- its output -----------------------------------------------------------------------------------------------------

    def _follow(self) -> None:
        """Read the log from its end now (opened here, not in the thread: nothing written after this is missed)."""
        if self._tail_thread is not None and self._tail_thread.is_alive():
            return
        try:
            f = open(Path(self.workdir) / trunkrecd.LOG, "rb")
            f.seek(0, os.SEEK_END)                     # said before this process: not replayed
        except OSError:
            f = None                                   # none yet: the one that appears is read from its start
        self._tail_stop.clear()
        self._tail_thread = threading.Thread(target=self._tail, args=(f,), name="trunk-recorder-log", daemon=True)
        self._tail_thread.start()

    def _line(self, raw: bytes) -> None:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            return
        self.last_lines.append(line)
        self.cc.feed(line)
        if self.on_line is not None:
            try:
                self.on_line(line)
            except Exception as e:  # the live indicator must never stop reading
                self.on_event(f"p25: live talker parse error: {type(e).__name__}: {e}")

    def _tail(self, f) -> None:
        """trunk-recorder.log as the service writes it: from where _follow opened it, then from the start of every
        new file (the service renames a full one to .log.1 and opens another)."""
        path = Path(self.workdir) / trunkrecd.LOG
        ino = os.fstat(f.fileno()).st_ino if f is not None else None
        buf = b""
        while not self._tail_stop.is_set():
            if f is None:
                try:
                    f = open(path, "rb")
                    ino = os.fstat(f.fileno()).st_ino
                except OSError:
                    self._tail_stop.wait(self.poll_s)
                    continue
            chunk = f.read(65536)
            if chunk:
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    self._line(raw)
                continue
            try:
                st = os.stat(path)
            except OSError:
                st = None
            if st is not None and st.st_ino == ino and st.st_size >= f.tell():
                self._tail_stop.wait(self.poll_s)       # nothing new
                continue
            if st is not None and st.st_ino == ino:
                f.seek(0)                               # cut short in place: from its start
                continue
            for raw in (buf + f.read()).split(b"\n"):   # renamed: the rest of the old one, then the new one
                self._line(raw)
            buf = b""
            f.close()
            f = None
        if f is not None:
            f.close()


class _Sources:
    """A SystemRecorder's device_fn: {source index: the dongle's current index}, or None (and why) while one of its
    dongles can't be opened (not plugged in, or its serial shared by two)."""

    def __init__(self, rec):
        self.rec, self.why = rec, ""

    def __call__(self) -> dict | None:
        out, self.why = {}, ""
        for i, (fn, _job) in sorted(self.rec.sources.items()):
            dev = fn()
            if dev is None:
                self.rec.device_name = getattr(fn, "label", "") or getattr(fn, "serial", "")
                self.why = getattr(fn, "why", "")
                return None
            out[i] = dev
        return out or None


class SystemRecorder(TrunkRecorder):
    """One more system's trunk-recorder (GitHub #409, sizeup/radio/systems.py), a child of sizeup (the Mac, a bench).
    It runs in the system's own directory on the sources of its config.json that have a dongle:

        give(index, finder, job)   source `index` gets a dongle (finder: the app's _dongle(serial)); it (re)starts
        take(index)                that source's dongle goes; it restarts on the rest, or stops with none left
        source_on(index)           the serial that source runs on, None when it has none

    Never auto-assigned, never the county's trunk-recorder: its own process, log and capture directory, at a lower
    priority than the county's."""

    lower = True

    def __init__(self, name: str, label: str, command: str, workdir: str,
                 on_event: Callable[[str], None] = lambda m: None):
        super().__init__(" ".join(shlex.quote(a) for a in [*trunkrecd.without_config(shlex.split(os.path.expanduser(command))),
                                                           "--config=config.json"]), workdir, on_event)
        self.name, self.label = name, label
        self.sources: dict[int, tuple[Callable, str]] = {}       # source index -> (dongle finder, its job)
        self.device_fn, self.device_name = _Sources(self), label

    def _argv_for(self, dev=None) -> list[str]:
        self.left_out = []
        return trunkrecd.system_runtime_argv(self.argv, self.workdir, dev or {})

    def source_on(self, index: int) -> str | None:
        got = self.sources.get(index)
        return getattr(got[0], "serial", None) if got else None

    def on(self) -> str | None:
        return trunkrecd.sources_sdr({i: {"sdr": getattr(fn, "serial", "")} for i, (fn, _j) in self.sources.items()}) or None

    def _apply(self, why: str) -> None:
        TrunkRecorder.stop(self, why)
        if self.sources:
            TrunkRecorder.start(self)

    def give(self, index: int, finder: Callable, job: str, apply: bool = True) -> None:
        """apply False: only noted (the app's start hands a system all its dongles, then start()s it once)."""
        self.sources[index] = (finder, job)
        if apply:
            self._apply(f"{job} on SDR {getattr(finder, 'label', '')}")

    def take(self, index: int, why: str = "off") -> None:
        if self.sources.pop(index, None) is not None:
            self._apply(why)

    def start(self) -> SystemRecorder:
        if self.sources:
            TrunkRecorder.start(self)
        return self

    def status(self) -> dict:
        return dict(super().status(), label=self.label, system=self.name)


class SystemUnitRecorder(SystemRecorder, UnitRecorder):
    """The same on the Linux station: sizeup-trunk-recorder's SystemSupervisor runs it (sizeup/radio/trunkrecd.py), told
    through the system's own state file ({"on", "sources": {index: {sdr, mode, name}}, "label"}). The claims on its
    dongles are the service's while it runs, and sizeup's own to sizeup (claims.helpers)."""

    unit = True

    def __init__(self, name: str, label: str, command: str, workdir: str,
                 on_event: Callable[[str], None] = lambda m: None, state_path: str | None = None,
                 status_path: str | None = None, poll_s: float = 0.1, wait_s: float = 20.0):
        others = claims.helpers                        # the county's recorder's (same service): still counted
        UnitRecorder.__init__(self, command, workdir, on_event,
                              state_path or trunkrecd.system_state_path(name),
                              status_path or trunkrecd.system_status_path(name), poll_s, wait_s)
        mine = self.helper_pids
        claims.helpers = lambda: set(others()) | set(mine())
        self.argv = [*trunkrecd.without_config(self.argv), "--config=config.json"]
        self.name, self.label = name, label
        self.sources = {}
        self.device_fn, self.device_name = _Sources(self), label

    def _write_state(self, on: bool, sdr: str = "") -> int:
        self._seq = max(self._seq + 1, time.time_ns())
        srcs = {str(i): {"sdr": str(getattr(fn, "serial", "") or ""), "mode": job,
                         "name": getattr(fn, "label", "") or str(getattr(fn, "serial", ""))}
                for i, (fn, job) in sorted(self.sources.items())} if on else {}
        w = {"on": bool(on and srcs), "sources": srcs, "label": self.label, "pid": os.getpid(), "seq": self._seq,
             "at": time.time()}
        p = self.state_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}")
        tmp.write_text(json.dumps(w))
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
        return self._seq

    def asked(self) -> dict:
        w = trunkrecd.wanted_system(str(self.state_path))
        return dict(w, sdr=trunkrecd.sources_sdr(w["sources"]))

    def _apply(self, why: str) -> None:
        if not self.sources:
            return UnitRecorder.stop(self, why)
        self._follow()
        try:
            seq = self._write_state(True)
        except OSError as e:
            self.state, self.detail = "down", f"cannot hand {self.label} to {trunkrecd.UNIT}: {e}"
            self.on_event(self.detail)
            return
        was, self.serial = self.serial, SystemRecorder.on(self)
        if was and set(was.split(",")) - set((self.serial or "").split(",")):
            # a dongle was taken from it: until the service has let it go, the next program can't open it
            end = time.monotonic() + self.wait_s
            while time.monotonic() < end:
                st = trunkrecd.read_status(str(self.status_path))
                if st is None or st.get("stale") or st.get("seq") == seq:
                    return
                time.sleep(self.poll_s)
            self.on_event(f"p25: {trunkrecd.UNIT} didn't let {self.label}'s dongle go within {self.wait_s:.0f} s ({why})")

    def start(self) -> SystemUnitRecorder:
        if self.sources:
            self._apply("start")
        return self

    def stop(self, reason: str = "off") -> None:
        self.sources = {}
        UnitRecorder.stop(self, reason)

    def shutdown(self) -> None:
        UnitRecorder.shutdown(self)

    def on(self) -> str | None:
        return SystemRecorder.on(self) if self.serial else None

    def status(self) -> dict:
        st = UnitRecorder.status(self)
        if st["state"] == "starting":
            st["detail"] = self.detail = f"handing {self.label} to {trunkrecd.UNIT}"
        return dict(st, label=self.label, system=self.name)
