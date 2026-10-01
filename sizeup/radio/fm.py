"""Narrowband FM channel: rtl_fm -> 16 kHz audio -> noise squelch -> transmissions.

FM squelch works the opposite way to AM: with no carrier the discriminator puts out loud hiss
across the whole audio band; a carrier "quiets" it. So we watch the energy above the voice
band (4-7.5 kHz): the hiss level is learned as the 80th percentile of the last minute (a quiet
paging channel is idle most of the time), and the squelch opens when that band drops
`open_db` below it, closing again `close_db` below (hysteresis) after a short hang.

One rtl_fm per channel; if it dies, stalls or the dongle is unplugged, the loop restarts it
with backoff, forever (same pattern as shortfinal's receivers).
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

RATE = 16000
FRAME = RATE // 20             # 50 ms
HISTORY_FRAMES = 20 * 60       # hiss reference: last minute
LEARN_FRAMES = 40              # 2 s before the squelch may open
HANG_FRAMES = 8                # carrier may flicker for 0.4 s inside one transmission
PREROLL_FRAMES = 3
MIN_FRAMES = 6                 # under 0.3 s: a blip, not a transmission
MAX_FRAMES = 20 * 120          # split anything over 2 minutes (a long page is ~30-60 s)
STALL_S = 5.0
HF_BAND = (4000.0, 7500.0)


@dataclass
class Segment:
    start: float               # wall clock
    end: float
    audio: np.ndarray = field(repr=False)  # float32 -1..1, 16 kHz, as demodulated
    quieting_db: float = 0.0   # how far the carrier quieted the hiss (signal quality)


class NoiseSquelch:
    """Streaming FM noise squelch. feed() 50 ms frames; get finished Segments back."""

    def __init__(self, open_db: float = 10.0, close_db: float = 6.0,
                 clock: Callable[[], float] = time.time):
        self.open_db, self.close_db, self.clock = open_db, close_db, clock
        f = np.fft.rfftfreq(FRAME, 1 / RATE)
        self._hf = (f >= HF_BAND[0]) & (f <= HF_BAND[1])
        self._win = np.hanning(FRAME)
        self.levels: deque[float] = deque(maxlen=HISTORY_FRAMES)
        self.hiss = 0.0
        self.open = False
        self._frames: list[np.ndarray] = []
        self._hf_open: list[float] = []
        self._pre: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        self._quiet_run = 0
        self._start = 0.0
        self._n = 0

    def hf_level(self, frame: np.ndarray) -> float:
        spec = np.abs(np.fft.rfft(frame * self._win)) ** 2
        return float(spec[self._hf].mean()) + 1e-12

    @property
    def quieting_db(self) -> float | None:
        """Current quieting (dB below the hiss), for a live meter."""
        if not self.levels or self.hiss <= 0:
            return None
        return float(10 * np.log10(self.hiss / self.levels[-1]))

    def feed(self, frame: np.ndarray) -> Segment | None:
        lvl = self.hf_level(frame)
        self._n += 1
        if not self.open:
            self.levels.append(lvl)  # only idle frames teach the hiss level
        if self._n % 20 == 0 or self._n == LEARN_FRAMES:
            self.hiss = float(np.percentile(np.fromiter(self.levels, float), 80))
        q = 10 * np.log10(self.hiss / lvl) if self.hiss > 0 else 0.0
        if not self.open:
            if self._n >= LEARN_FRAMES and q >= self.open_db:
                self.open = True
                self._start = self.clock() - (len(self._pre) + 1) * FRAME / RATE
                self._frames = list(self._pre) + [frame]
                self._hf_open = [q]
                self._quiet_run = 0
            else:
                self._pre.append(frame)
            return None
        self._frames.append(frame)
        if q >= self.close_db:
            self._quiet_run = 0
            self._hf_open.append(q)
        else:
            self._quiet_run += 1
        if self._quiet_run > HANG_FRAMES or len(self._frames) >= MAX_FRAMES:
            keep = len(self._frames) - max(0, self._quiet_run - 1)
            frames, quieting = self._frames[:keep], float(np.median(self._hf_open))
            self._frames, self._hf_open, self._pre = [], [], deque(maxlen=PREROLL_FRAMES)
            self.open = False
            if len(frames) - PREROLL_FRAMES < MIN_FRAMES:
                return None
            audio = np.concatenate(frames).astype(np.float32)
            return Segment(self._start, self._start + len(audio) / RATE, audio, quieting)
        return None


@dataclass
class Channel:
    mhz: float
    label: str                 # "South 2", "FG1"
    ctcss: float | None = None


class NfmReceiver:
    """rtl_fm on one channel, feeding a NoiseSquelch; finished segments go to on_segment."""

    def __init__(self, rtl_fm: str, device: str, gain_db: float | None, channel: Channel,
                 on_segment: Callable[[Channel, Segment], None],
                 on_event: Callable[[str], None] = lambda m: None,
                 open_db: float = 10.0):
        self.rtl_fm, self.device, self.gain_db, self.channel = rtl_fm, device, gain_db, channel
        self.on_segment, self.on_event = on_segment, on_event
        self.open_db = open_db
        self.squelch = NoiseSquelch(open_db=open_db)
        self.state, self.detail, self.restarts = "starting", "", 0
        self.last_heard: float | None = None   # end of the last transmission
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    def argv(self) -> list[str]:
        cmd = [self.rtl_fm, "-M", "fm", "-f", f"{self.channel.mhz:.4f}M", "-s", str(RATE), "-E", "dc"]
        if self.gain_db is not None:
            cmd += ["-g", f"{self.gain_db:g}"]
        if self.device:
            cmd += ["-d", self.device]
        return cmd + ["-"]

    def start(self) -> NfmReceiver:
        threading.Thread(target=self._run, name=f"nfm-{self.channel.label}", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._kill()

    def status(self) -> dict:
        q = self.squelch.quieting_db
        return {"label": self.channel.label, "mhz": self.channel.mhz, "state": self.state,
                "detail": self.detail, "restarts": self.restarts, "open": self.squelch.open,
                "last_heard": self.last_heard,
                "quieting_db": round(q, 1) if q is not None else None}

    def _kill(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    pass

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(self.argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                              start_new_session=True)
            except OSError as e:
                self.state, self.detail = "down", f"cannot start rtl_fm: {e}"
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15)
                continue
            self.state, self.detail = "ok", f"{self.channel.label} {self.channel.mhz:.4f}"
            self.squelch = NoiseSquelch(open_db=self.open_db)
            got = self._pump(self._proc)
            self._kill()
            if self._stop.is_set():
                break
            self.restarts += 1
            self.state = "down"
            self.detail = "rtl_fm stopped" if got else "no audio from rtl_fm (dongle busy/unplugged?)"
            self.on_event(f"{self.channel.label} down: {self.detail}")
            self._stop.wait(2.0 if got else backoff)
            backoff = 2.0 if got else min(backoff * 2, 15)

    def _pump(self, proc: subprocess.Popen) -> bool:
        fd = proc.stdout.fileno()
        buf, got, last = b"", False, time.monotonic()
        need = FRAME * 2
        while not self._stop.is_set() and proc.poll() is None:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                if time.monotonic() - last > STALL_S:
                    self.detail = "rtl_fm stalled"
                    return got
                continue
            chunk = os.read(fd, 8192)
            if not chunk:
                return got
            got, last = True, time.monotonic()
            buf += chunk
            while len(buf) >= need:
                frame = np.frombuffer(buf[:need], dtype="<i2").astype(np.float64) / 32768.0
                buf = buf[need:]
                seg = self.squelch.feed(frame)
                if seg is not None:
                    self.last_heard = seg.end
                    try:
                        self.on_segment(self.channel, seg)
                    except Exception as e:
                        self.on_event(f"segment handler error: {type(e).__name__}: {e}")
        return got
