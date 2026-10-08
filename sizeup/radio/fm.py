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
MIN_FRAMES = 6                 # under 0.3 s of carrier: not worth keeping
DATA_SHARE = 0.75              # a frame with this much of its voice-band energy at 1000-2100 Hz ...
DATA_OVER_DB = 15.0            # ... and this far over the dead air is an MDC-1200 data burst
EDGE_SEARCH = 20               # look for a burst within 1 s of either end of the carrier (PCFD's APX
                               # 6000s send it 0.3-0.8 s in, 2026-10-07; 0.3 s missed every one)
MAX_FRAMES = 20 * 120          # split anything over 2 minutes (a long page is ~30-60 s)
STALL_S = 5.0
HF_BAND = (4000.0, 7500.0)
# A second, weaker carrier on a simplex channel. FG1 2026-10-07 17:17-17:21: PCFD radios quiet the
# receiver ~40 dB; something else on the frequency (no tone, pulsing ~1 Hz) held it at 21-28 dB for
# 20-30 s at a time, so short key-ups were logged as 21-28 s and real key-ups were buried inside.
# With drop_db set, a transmission also ends when quieting falls this far under the level it opened
# at, and while that background stays the squelch only opens for something this much stronger.
LEVEL_FRAMES = 6               # a transmission's level: median quieting over its first 0.3 s
EDGE_DB = 3.0                  # the cut between the two carriers: where quieting reaches the weaker one's
                               # level (+ this), so an MDC burst at the edge stays with its transmission
RISE_FRAMES = 3                # a stronger carrier inside an open one: this long (0.15 s) to count
BACKGROUND_FRAMES = 80         # 4 s with no PTT-ID and no speech: the background itself (it opened the
BACKGROUND_SPEECH_S = 0.3      # squelch on its own at 17:17 and 17:20); dropped, and kept as the floor.
BACKGROUND_STEP = 10           # Looked for every 0.5 s over the last 4 s, not just once 4 s after opening
                               # (FG1 2026-10-08 16:32: E63 keyed up, MDC ID 6301, and the background came
                               # straight in after it; RX stayed lit 51 s. Checked over everything since
                               # opening, the ID made all of it "not background"). The key-up is kept.
REOPEN_FRAMES = 20             # closed on a background carrier: the last 1 s is watched for an MDC ID or
REOPEN_STEP = 5                # speech (someone keyed silently, then talked), every 0.25 s
VOICE_BAND = (300.0, 3000.0)


@dataclass
class Segment:
    start: float               # wall clock
    end: float
    audio: np.ndarray = field(repr=False)  # float32 -1..1, 16 kHz, as demodulated (carrier-up part only)
    quieting_db: float = 0.0   # how far the carrier quieted the hiss (signal quality)
    bursts: list = field(default_factory=list, repr=False)  # MDC-1200 data bursts cut from the edges

def data_frames(frames: list[np.ndarray]) -> list[bool]:
    """Which 50 ms frames are an MDC-1200 burst (1200 baud MSK, 1200/1800 Hz: Motorola PTT-ID, sent
    on key-up and/or release). FG1 bench: +26 dB at 1000-3000 Hz, 14-20 dB over the voice."""
    if not frames:
        return []
    f = np.fft.rfftfreq(FRAME, 1 / RATE)
    voice = (f >= VOICE_BAND[0]) & (f <= VOICE_BAND[1])
    fsk = (f >= 1000) & (f <= 2100)
    win = np.hanning(FRAME)
    S = np.abs(np.fft.rfft(np.stack(frames) * win, axis=1)) ** 2
    ev, ef = S[:, voice].sum(axis=1) + 1e-20, S[:, fsk].sum(axis=1)
    db = 10 * np.log10(ev)
    floor = np.percentile(db, 5)
    return list((ef / ev >= DATA_SHARE) & (db >= floor + DATA_OVER_DB))


def speech_seconds(audio: np.ndarray, rate: int = RATE, margin_db: float = 8.0) -> float:
    """How much of a transmission has sound in the voice band above its own dead-air floor.

    With the squelch trimmed to the carrier-up part, a mic click is all dead air (carrier, nobody
    talking): ~0 s. Measured on the FG1 bench: dead air ~-45 dB, voice -28..-40 dB."""
    blk = rate // 50
    n = len(audio) // blk
    if n < 3:
        return 0.0
    X = np.fft.rfft(audio[: n * blk].reshape(n, blk) * np.hanning(blk), axis=1)
    f = np.fft.rfftfreq(blk, 1 / rate)
    band = (f >= VOICE_BAND[0]) & (f <= VOICE_BAND[1])
    e = 10 * np.log10((np.abs(X[:, band]) ** 2).mean(axis=1) + 1e-20)
    floor = np.percentile(e, 5)  # dead air: the pause after key-up / between words is short
    return float((e > floor + margin_db).sum() * blk / rate)


class NoiseSquelch:
    """Streaming FM noise squelch. feed() 50 ms frames; get finished Segments back.

    A segment is only the voice part of a transmission: no hiss before key-up, and no MDC-1200
    data burst at either end (Motorola PTT-ID on key-up / release). On the FG1 bench those were
    the loudest things in every clip, 14-25 dB over the voice, and the only content of a mic
    click. The bursts are kept on the segment (unit ID decoding)."""

    def __init__(self, open_db: float = 10.0, close_db: float = 6.0,
                 clock: Callable[[], float] = time.time, drop_db: float | None = None):
        self.open_db, self.close_db, self.clock, self.drop_db = open_db, close_db, clock, drop_db
        f = np.fft.rfftfreq(FRAME, 1 / RATE)
        self._hf = (f >= HF_BAND[0]) & (f <= HF_BAND[1])
        self._win = np.hanning(FRAME)
        self.levels: deque[float] = deque(maxlen=HISTORY_FRAMES)
        self.hiss = 0.0
        self._lvl = 0.0
        self.open = False
        self._frames: list[np.ndarray] = []
        self._qs: list[float] = []
        self._quiet_run = 0
        self._start = 0.0
        self._n = 0
        self._level: float | None = None    # this transmission's quieting (drop_db only)
        self._drop_run = 0
        self._rise_run = 0
        self.floor: float | None = None     # quieting of a background carrier still on the channel
        self._floor_gone = 0
        self._under: list[float] = []       # closed: a run of quieting well under the floor
        self._held: deque[tuple[np.ndarray, float]] = deque(maxlen=REOPEN_FRAMES)  # closed: the last 1 s
        self._up_db = open_db               # quieting that counts as carrier-up in this transmission

    def hf_level(self, frame: np.ndarray) -> float:
        spec = np.abs(np.fft.rfft(frame * self._win)) ** 2
        return float(spec[self._hf].mean()) + 1e-12

    @property
    def quieting_db(self) -> float | None:
        """Current quieting (dB below the hiss), for a live meter."""
        if not self._lvl or self.hiss <= 0:
            return None
        return float(10 * np.log10(self.hiss / self._lvl))

    def feed(self, frame: np.ndarray) -> Segment | None:
        lvl = self._lvl = self.hf_level(frame)
        self._n += 1
        if not self.open and self.floor is None:
            self.levels.append(lvl)  # only idle frames teach the hiss level
        if self._n % 20 == 0 or self._n == LEARN_FRAMES:
            self.hiss = float(np.percentile(np.fromiter(self.levels, float), 80))
        q = 10 * np.log10(self.hiss / lvl) if self.hiss > 0 else 0.0
        if not self.open:
            if self.floor is not None and self._watch_floor(frame, q):
                return None
            need = self.open_db if self.floor is None else max(self.open_db, self.floor + self.drop_db)
            if self._n >= LEARN_FRAMES and q >= need:
                self._begin(frame, q, self.clock() - FRAME / RATE)
            return None
        self._frames.append(frame)
        self._qs.append(q)
        self._quiet_run = 0 if q >= self.close_db else self._quiet_run + 1
        if self.drop_db is not None:
            if self._level is None and len(self._qs) >= LEVEL_FRAMES:
                self._level = float(np.median(self._qs[:LEVEL_FRAMES]))
            if self._level is not None:
                self._drop_run = self._drop_run + 1 if q < self._level - self.drop_db else 0
                self._rise_run = self._rise_run + 1 if q >= self._level + self.drop_db else 0
                if self._rise_run >= RISE_FRAMES:
                    # A stronger carrier came up over this one: what came before was the background.
                    n = len(self._frames) - RISE_FRAMES
                    while n > 1 and self._qs[n - 1] > self._level + EDGE_DB:
                        n -= 1
                    frames, qs, start, level, up = self._frames[:n], self._qs[:n], self._start, self._level, self._up_db
                    rest, rest_q = self._frames[n:], self._qs[n:]
                    self._begin(rest[0], rest_q[0], start + n * FRAME / RATE)
                    for f, v in zip(rest[1:], rest_q[1:]):
                        self._frames.append(f)
                        self._qs.append(v)
                    self._set_floor(level)
                    seg = self._segment(frames, qs, start, up)
                    return seg if seg is not None and seg.bursts else None   # no ID: not a key-up
                k = len(self._frames)
                if k >= BACKGROUND_FRAMES and (k - BACKGROUND_FRAMES) % BACKGROUND_STEP == 0:
                    win = self._frames[-BACKGROUND_FRAMES:]
                    if not any(data_frames(win)) and speech_seconds(np.concatenate(win)) < BACKGROUND_SPEECH_S:
                        return self._background(k - BACKGROUND_FRAMES)
                if self._drop_run > HANG_FRAMES and self._quiet_run <= HANG_FRAMES:
                    # Fell to a weaker carrier: this transmission ended where the drop began.
                    n = len(self._frames) - self._drop_run
                    self._set_floor(float(np.median(self._qs[n:])))
                    while n < len(self._qs) and self._qs[n] > self.floor + EDGE_DB:
                        n += 1
                    return self._close(n)
        if self._quiet_run > HANG_FRAMES or len(self._frames) >= MAX_FRAMES:
            return self._close(len(self._frames))
        return None

    def _begin(self, frame: np.ndarray, q: float, start: float, up_db: float | None = None) -> None:
        self.open, self._start = True, start
        self._frames, self._qs, self._quiet_run = [frame], [q], 0
        self._level, self._drop_run, self._rise_run = None, 0, 0
        self._up_db = self.open_db if up_db is None else up_db

    def _close(self, n: int) -> Segment | None:
        """Close the squelch; the transmission is its first n frames (the rest is dropped)."""
        frames, qs, start = self._frames[:n], self._qs[:n], self._start
        self._frames, self._qs, self.open = [], [], False
        return self._segment(frames, qs, start, self._up_db)

    def _set_floor(self, q: float | None) -> None:
        self.floor, self._floor_gone, self._under = q, 0, []
        self._held.clear()

    def _background(self, n: int) -> Segment | None:
        """The last 4 s (from frame n on) are the background carrier: no ID, nothing said. The
        transmission, if there was one, ended where it began: keep that part, drop the rest."""
        qs = self._qs[:n]
        self._set_floor(float(np.median(self._qs[n:])))
        floor = self.floor
        if n == 0 or not (any(data_frames(self._frames[:n]))
                          or speech_seconds(np.concatenate(self._frames[:n])) >= BACKGROUND_SPEECH_S
                          or sum(v >= floor + self.drop_db for v in qs) >= MIN_FRAMES):
            self._frames, self._qs, self.open = [], [], False      # the background all along
            return None
        if max(qs) >= floor + 2 * EDGE_DB:  # a stronger key-up: the background's start isn't part of it
            stop = max(1, n - BACKGROUND_STEP)
            while n > stop and qs[n - 1] < floor + EDGE_DB:
                n -= 1
        return self._close(n)

    def _watch_floor(self, frame: np.ndarray, q: float) -> bool:
        """Closed, with a background carrier on the channel. Follows it: gone once it's under close_db,
        lower once it's well under the floor (so a floor set by a strong carrier can't block the
        key-ups after it). True: an ID or speech was heard on it, and a transmission opened there."""
        self._floor_gone = self._floor_gone + 1 if q < self.close_db else 0
        if self._floor_gone > HANG_FRAMES:
            self._set_floor(None)
            return False
        if q < self.floor - self.drop_db:
            self._under.append(q)
            if len(self._under) > HANG_FRAMES:
                low = float(np.median(self._under))
                self._set_floor(low if low >= self.close_db else None)
                return False
        else:
            self._under = []
        self._held.append((frame, q))
        if self._n % REOPEN_STEP:
            return False
        # The carrier at the floor's level (frames from a weaker one coming in aren't compared with it)
        lo = max(self.close_db, self.floor - self.drop_db)
        on = [i for i, (_f, v) in enumerate(self._held) if v >= lo]
        if len(on) < REOPEN_FRAMES // 2:
            return False
        frames = [self._held[i][0] for i in on]
        if not any(data_frames(frames)) and speech_seconds(np.concatenate(frames)) <= BACKGROUND_SPEECH_S:
            return False
        held = list(self._held)[on[0]:]
        self._held.clear()
        self._begin(held[0][0], held[0][1], self.clock() - len(held) * FRAME / RATE, up_db=self.close_db)
        for f, v in held[1:]:
            self._frames.append(f)
            self._qs.append(v)
        return True

    def _segment(self, frames: list[np.ndarray], qs: list[float], start: float,
                 up_db: float | None = None) -> Segment | None:
        # Keep the carrier-up span: first to last frame fully quieted.
        # Carrier-up span: from the first fully quieted frame to the last frame still above the
        # hold-open level. (Ending at the last *fully* quieted frame cut a 60 s South 2 page at 17 s:
        # quieting dips under open_db during long pages while the carrier is still up.)
        up = [i for i, v in enumerate(qs) if v >= (self.open_db if up_db is None else up_db)]
        held = [i for i, v in enumerate(qs) if v >= self.close_db]
        if not up:
            return None
        i0, i1 = up[0], held[-1]
        quieting = float(np.median(qs[i0:i1 + 1]))
        body = frames[i0:i1 + 1]
        data = data_frames(body)
        bursts: list[np.ndarray] = []
        # Cut data bursts off both ends. Radios hold the carrier a moment around the ID
        # (FG1 bench: up to ~0.15 s of dead air beyond it), and the frames where the burst
        # starts/ends are only partly burst, so one more frame goes with it.
        lo, hi = 0, len(body) - 1
        for _ in range(2):
            k = next((j for j in range(lo, min(lo + EDGE_SEARCH, hi + 1)) if data[j]), None)
            if k is None:
                break
            e = k
            while e + 1 <= hi and data[e + 1]:
                e += 1
            bursts.append(np.concatenate(body[max(lo, k - 1):min(hi, e + 1) + 1]))
            lo = e + 2
        for _ in range(2):
            k = next((j for j in range(hi, max(hi - EDGE_SEARCH, lo - 1), -1) if data[j]), None)
            if k is None:
                break
            b = k
            while b - 1 >= lo and data[b - 1]:
                b -= 1
            bursts.append(np.concatenate(body[max(lo, b - 1):min(hi, k + 1) + 1]))
            hi = b - 2
        if hi - lo + 1 < MIN_FRAMES:
            if not bursts:
                return None
            # A key-up that is only an ID burst (+ dead carrier): still a transmission (who keyed),
            # kept at its length with the audio blanked, so nothing reads the data tones as speech.
            audio = np.zeros(len(body) * FRAME, np.float32)
            t0 = start + i0 * FRAME / RATE
            return Segment(t0, t0 + len(audio) / RATE, audio, quieting, bursts)
        audio = np.concatenate(body[lo:hi + 1]).astype(np.float32)
        t0 = start + (i0 + lo) * FRAME / RATE
        return Segment(t0, t0 + len(audio) / RATE, audio, quieting, bursts)


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
                 open_db: float = 10.0, drop_db: float | None = None):
        self.rtl_fm, self.device, self.gain_db, self.channel = rtl_fm, device, gain_db, channel
        self.on_segment, self.on_event = on_segment, on_event
        self.open_db, self.drop_db = open_db, drop_db
        self.squelch = self.new_squelch()
        self.state, self.detail, self.restarts = "starting", "", 0
        self.last_heard: float | None = None   # end of the last transmission
        self._stderr: deque[str] = deque(maxlen=20)
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    def new_squelch(self) -> NoiseSquelch:
        # Fresh on every rtl_fm start. (It used to be made here without drop_db, so FG1 ran with none
        # of the weaker-carrier rules live: 16:32 on 2026-10-08 was held open 51 s by the background.)
        return NoiseSquelch(open_db=self.open_db, drop_db=self.drop_db)

    def argv(self, dev: str | None = None) -> list[str]:
        cmd = [self.rtl_fm, "-M", "fm", "-f", f"{self.channel.mhz:.4f}M", "-s", str(RATE), "-E", "dc"]
        if self.gain_db is not None:
            cmd += ["-g", f"{self.gain_db:g}"]
        dev = self.device if dev is None and not callable(self.device) else dev
        if dev:
            cmd += ["-d", str(dev)]
        return cmd + ["-"]

    @property
    def device_name(self) -> str:
        return (getattr(self.device, "label", None) or getattr(self.device, "serial", None)
                or (self.device if isinstance(self.device, str) else "") or "any")

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

    def _drain(self, proc: subprocess.Popen) -> None:
        for line in iter(proc.stderr.readline, b""):
            self._stderr.append(line.decode(errors="replace").strip())

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            self._stderr.clear()
            dev = None
            if callable(self.device):  # a configured serial, looked up right now (never passed as-is)
                dev = self.device()
                if dev is None:
                    self.state, self.detail = "absent", f"SDR '{self.device_name}' not plugged in"
                    self._stop.wait(15.0)
                    continue
            try:
                self._proc = subprocess.Popen(self.argv(dev), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              start_new_session=True)
                threading.Thread(target=self._drain, args=(self._proc,), daemon=True).start()
            except OSError as e:
                self.state, self.detail = "down", f"cannot start rtl_fm: {e}"
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15)
                continue
            self.state, self.detail = "ok", f"{self.channel.label} {self.channel.mhz:.4f}"
            self.squelch = self.new_squelch()
            got = self._pump(self._proc)
            self._kill()
            if self._stop.is_set():
                break
            err = " ".join(self._stderr)
            if not got and ("No matching devices" in err or "No supported devices" in err):
                # Not plugged in (or a different serial): say so, look again every 15 s, stay quiet.
                self.state, self.detail = "absent", f"dongle '{self.device_name}' not connected"
                self._stop.wait(15.0)
                continue
            self.restarts += 1
            self.state = "down"
            if not got and ("usb_claim_interface" in err or "Failed to open" in err):
                self.detail = f"dongle '{self.device_name}' busy (in use by another program?)"
            else:
                self.detail = "rtl_fm stopped" if got else "no audio from rtl_fm"
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
