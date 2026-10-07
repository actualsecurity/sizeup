"""The dispatch voice: the Kokoro text-to-speech server (sizeup/tts_server.py), kept running by
sizeup, and the client the dispatcher calls.

    TtsService      starts the server on the TTS environment's Python (tools/setup_tts.sh) and keeps
                    it up, like WhisperService does whisper-server: one it started earlier (recorded in
                    state_path) is reused when its command line still matches the config and replaced
                    when it doesn't; a server on the port that sizeup didn't start is used, never killed
    synthesize()    text -> float samples at 16 kHz (the server's 24 kHz resampled), or TtsError, on
                    which the dispatcher falls back to macOS `say`
    VoiceCfg        what the dispatcher renders with ([dispatch] tts_engine, voice, speed, ...)
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from math import gcd
from pathlib import Path
from typing import Callable

import numpy as np

HOME = Path.home() / ".local" / "share" / "sizeup" / "tts"   # tools/setup_tts.sh
DEFAULT_PYTHON = str(HOME / "venv" / "bin" / "python")
SERVER = Path(__file__).with_name("tts_server.py")
DEFAULT_PORT = 8093
RATE = 16000
ENGINES = ("kokoro", "say")
LABELS = {"kokoro": "Kokoro"}

# Kokoro-82M v1.0's English voices (what setup_tts.sh installs); the server's own list wins when it's up.
KOKORO_VOICES = ("af_heart af_alloy af_aoede af_bella af_jessica af_kore af_nicole af_nova af_river af_sarah af_sky "
                 "am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa "
                 "bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis").split()


class TtsError(RuntimeError):
    """The TTS server is down, refused, failed or was too slow: the dispatcher uses `say`."""


@dataclass(frozen=True)
class VoiceCfg:
    engine: str = "say"            # "kokoro" (the server, `say` as fallback) | "say" (macOS only)
    voice: str = ""                # engine kokoro: a Kokoro voice ("af_heart"); engine say: a `say -v` voice
    speed: float = 1.0             # kokoro: 0.5-2.0 (0.92 = a touch slower than conversational)
    port: int = DEFAULT_PORT
    fallback_voice: str = "Samantha"
    rate_wpm: int = 0              # `say` words per minute (0 = the voice's own)
    timeout_s: float = 10.0        # longer than this -> `say`

    def label(self, fallback: bool = False) -> str:
        """'Voice: Kokoro af_heart', 'Voice: Samantha (fallback)', 'Voice: Samantha'."""
        if self.engine == "kokoro" and not fallback:
            return f"Voice: Kokoro {self.voice}"
        v = (self.fallback_voice if self.engine == "kokoro" else self.voice) or "macOS say"
        return f"Voice: {v}" + (" (fallback)" if fallback else "")


def voice_info(v: str) -> dict:
    """'af_heart' -> {id, name: 'Heart', accent: 'American', gender: 'female'}."""
    return {"id": v, "name": v[3:].replace("_", " ").title(),
            "accent": {"a": "American", "b": "British"}.get(v[:1], ""),
            "gender": {"f": "female", "m": "male"}.get(v[1:2], "")}


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Polyphase-style rational resampling (windowed-sinc low-pass), numpy only."""
    if src == dst or not len(x):
        return np.asarray(x, np.float32)
    g = gcd(src, dst)
    up, down = dst // g, src // g
    m = max(up, down)
    taps = 24 * m + 1
    n = np.arange(taps) - (taps - 1) / 2
    cutoff = 0.92 * 0.5 / m                         # just under the lower Nyquist, at the upsampled rate
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(taps, 8.6)
    h *= up / h.sum()
    y = np.zeros(len(x) * up)
    y[::up] = x
    y = np.convolve(y, h, mode="same")
    return y[::down].astype(np.float32)


def _decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                raise TtsError(f"TTS server sent {8 * w.getsampwidth()}-bit audio")
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0
            if w.getnchannels() > 1:
                x = x.reshape(-1, w.getnchannels()).mean(axis=1)
            return x, w.getframerate()
    except (wave.Error, EOFError) as e:
        raise TtsError(f"TTS server sent a bad WAV: {e}") from e


def synthesize(text: str, voice: str, speed: float = 1.0, port: int = DEFAULT_PORT, timeout: float = 10.0,
               rate: int = RATE) -> np.ndarray:
    """The text said by the Kokoro server, as float samples at `rate`. Raises TtsError."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/tts", method="POST",
                                 data=json.dumps({"text": text, "voice": voice, "speed": speed}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or str(e)
        except (ValueError, OSError, AttributeError):
            msg = str(e)
        raise TtsError(f"TTS server: {msg}") from e
    except (TimeoutError, OSError) as e:      # refused, reset, timed out (URLError is an OSError)
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise TtsError(f"TTS server took over {timeout:g} s") from e
        raise TtsError(f"TTS server not reachable: {reason}") from e
    x, sr = _decode_wav(data)
    if not len(x):
        raise TtsError("TTS server sent no audio")
    return resample(x, sr, rate)


def health(port: int = DEFAULT_PORT, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            d = json.loads(r.read())
        return d if isinstance(d, dict) and d.get("ok") else None
    except (OSError, ValueError):
        return None


class TtsService:
    """Keep the Kokoro server running. state: "ok" (ready) | "starting" | "down" (detail says why;
    dispatches then use the fallback voice)."""

    def __init__(self, python: str, port: int = DEFAULT_PORT, voice: str = "af_heart",
                 state_path: str | Path | None = None, log: Callable[[str], None] = print,
                 server: str | Path = SERVER):
        self.python, self.port, self.voice, self.log = python, port, voice, log
        self.argv = [python, "-I", str(server), "--port", str(port), "--voice", voice]
        self.state, self.detail = "starting", "starting the voice"
        self.voices: list[str] = []
        self.state_path = Path(state_path) if state_path else None
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()

    # -- the process sizeup started (recorded in state_path) ------------------------------------------

    def _ours(self) -> tuple[int, list[str]] | None:
        """(pid, argv) of the server sizeup last started, if that process is still it."""
        try:
            d = json.loads(self.state_path.read_text()) if self.state_path else None
            pid, argv = int(d["pid"]), list(d["argv"])
            args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return None
        return (pid, argv) if "tts_server.py" in args and f"--port {self.port}" in args else None

    def _replace_stale(self) -> None:
        ours = self._ours()
        if not ours or ours[1] == self.argv:
            return
        self.log(f"tts-server: config changed ({' '.join(ours[1][3:])} -> {' '.join(self.argv[3:])}), restarting it")
        try:
            os.kill(ours[0], signal.SIGTERM)
        except OSError:
            return
        for _ in range(40):
            if not self._listening():
                return
            time.sleep(0.25)

    def _listening(self) -> bool:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def _log_tail(self) -> str:
        try:
            lines = [ln for ln in self._log_path().read_text(errors="replace").splitlines() if ln.strip()]
            return lines[-1][-200:] if lines else ""
        except OSError:
            return ""

    def _log_path(self) -> Path:
        return self.state_path.with_suffix(".log") if self.state_path else Path(os.devnull)

    # -- running --------------------------------------------------------------------------------------

    def start(self) -> TtsService:
        threading.Thread(target=self._run, name="tts", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def check(self) -> bool:
        """One supervision step (the loop runs it every few seconds). True = ready."""
        h = health(self.port)
        if h:
            self.voices = list(h.get("voices") or [])
            self.state, self.detail = "ok", f"voice ready: Kokoro {self.voice} (CPU)"
            return True
        if self._proc is not None and self._proc.poll() is None:
            self.state, self.detail = "starting", f"loading Kokoro {self.voice}"
            return False
        if self._proc is not None:                       # ours, and it exited
            tail = self._log_tail()
            self.log(f"tts-server exited ({self._proc.returncode})" + (f": {tail}" if tail else ""))
            self._proc = None
            self.state, self.detail = "down", "voice server stopped" + (f": {tail}" if tail else "")
            return False
        if self._listening():
            self.state, self.detail = "down", f"port {self.port} is in use by something that isn't the voice server"
            return False
        if not Path(self.python).is_file():
            self.state, self.detail = "down", f"no TTS environment at {self.python}: run tools/setup_tts.sh"
            return False
        try:
            logf = open(self._log_path(), "wb")
            self._proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                          start_new_session=True)
            logf.close()
            self.state, self.detail = "starting", f"loading Kokoro {self.voice}"
            if self.state_path:
                try:
                    self.state_path.write_text(json.dumps({"pid": self._proc.pid, "argv": self.argv}))
                except OSError:
                    pass
        except OSError as e:
            self.state, self.detail = "down", f"cannot start the voice server: {e}"
        return False

    def _run(self) -> None:
        self._replace_stale()
        fails = 0
        while not self._stop.is_set():
            was = self.state
            ok = self.check()
            if ok and was != "ok":
                self.log(f"tts-server: {self.detail}")
            fails = 0 if self.state != "down" else fails + 1
            # ready: checked every 2 s (a dead server shows as down quickly); down: retried at once, then backing off
            self._stop.wait(2.0 if self.state != "down" else 1.0 if fails == 1 else min(60.0, 5.0 * 2 ** min(fails - 2, 4)))

    def status(self) -> dict:
        """A row of the app's radios/status list: Voice ready (ok) / starting / down."""
        return {"label": "Voice", "mhz": None, "state": self.state, "detail": self.detail, "open": False,
                "quieting_db": None, "restarts": 0, "last_heard": None}

    @staticmethod
    def say_status(voice: str) -> dict:
        """The row when [dispatch] tts_engine = "say" (no server)."""
        import shutil
        ok = bool(shutil.which("say"))
        return {"label": "Voice", "mhz": None, "state": "ok" if ok else "down",
                "detail": f"voice: macOS say {voice}" if ok else "no text-to-speech: dispatches are text only",
                "open": False, "quieting_db": None, "restarts": 0, "last_heard": None}


def voices(port: int = DEFAULT_PORT) -> list[dict]:
    """The Kokoro voices to pick from: the running server's, else the ones setup_tts.sh installs."""
    h = health(port, timeout=1.0)
    return [voice_info(v) for v in ((h or {}).get("voices") or KOKORO_VOICES)]
