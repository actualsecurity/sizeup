"""Supervised trunk-recorder process (Fire/EMS 15 on the county P25 system).

trunk-recorder owns its dongle and writes one WAV + JSON per call into a capture directory,
which radio/p25.py ingests. This only keeps the process running: start, stop (frees the
dongle for another receiver on a one-dongle bench), restart with backoff if it dies.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable


LOG_MAX_BYTES = 5_000_000


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

    @property
    def installed(self) -> bool:
        exe = self.argv[0] if self.argv else ""
        return bool(exe) and Path(exe).is_file() and os.access(exe, os.X_OK)

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
            try:
                # trunk-recorder runs ffmpeg by name for call audio: make sure Homebrew's bin is on PATH.
                env = dict(os.environ)
                env["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "")])
                self._proc = subprocess.Popen(self.argv, cwd=self.workdir, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, start_new_session=True, env=env)
            except OSError as e:
                self.state, self.detail = "down", f"cannot start trunk-recorder: {e}"
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            self.state, self.detail = "ok", "trunk-recorder starting"
            threading.Thread(target=self._drain, args=(self._proc,), daemon=True).start()
            started = time.monotonic()
            while not self._stop.is_set() and self._proc.poll() is None:
                self._stop.wait(1.0)
            if self._stop.is_set():
                break
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
