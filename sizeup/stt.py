"""Speech-to-text through a persistent whisper.cpp server (model stays loaded on the GPU).

Same client as shortfinal. The model for fire radio is a general English one (large-v3-turbo
on the Mac); the ATC fine-tune is the wrong vocabulary here. An area prompt (streets, units,
incident types) can be passed per request.
"""

from __future__ import annotations

import io
import itertools
import json
import queue
import re
import threading
import time
import urllib.request
import uuid
import wave
from collections import deque
from typing import Callable

import numpy as np

NOISE_LABEL = re.compile(r"^[\[(*].*[\])*]$")  # "(static)", "[MUSIC]" — not speech
# What whisper says about a short burst of noise (a mic click, a squelch tail).
# What whisper says for dead air / a mic click (FG1 bench: "Thank you." or "Yes." on 6 of 6
# dead-air cuts). Rejected only when the audio has no voice energy or is very short.
HALLUCINATIONS = {"thank you", "thanks", "thank you very much", "yes", "you", "bye", "bye bye", "okay",
                  "ok", "thanks for watching", "thank you for watching", "so", "oh", "the end"}
SHORT_S = 1.5
# Queue priority (SttQueue), strict: a class is served only when every class above it is empty (#300). Pages,
# Fire/EMS 15 and the firegrounds first, the airband pre-alerts next, other live traffic, a person's re-run, then
# the talkgroups heard only as context (not "main" in Settings > Radios: other towns' fire, county police). #195:
# the station's whisper fell 9 h behind and the newest clips were refused and lost; #300: it fell 60-80 min behind
# because the context talkgroups alone bring more clips than it can do (each costs the same ~16 s, however short).
URGENT, HIGH, LIVE, RERUN, LOW = -2, -1, 0, 1, 2
# Another P25 system's `alert` talkgroups (#409, sizeup/tiers.py: police dispatch on MTA MRRS): a class of their own,
# under every class of ours (pages, Fire/EMS 15, the firegrounds, the airband, the Main talkgroups, a person's re-run)
# and shed when it waits: fire traffic never queues behind police. Their `text` talkgroups are LOW.
CONTEXT = 1.5
CONTEXT_MAX_WAIT_S = 600.0  # a context system's alert clip waiting this long is shed: a late pre-alert is no use
STALE = 3                   # (kept for callers: a class's stale items now go after its fresh ones, not after all)
QUEUE_MAX = 2000            # items waiting at most: past it the worst one goes (lowest priority, then oldest)
CHANNEL_CAP = 400           # one channel's items waiting at most: past it that channel's worst one goes
STALE_S = 600.0             # a live item waiting this long goes behind the fresh ones of its class (newest stale first) ...
MAX_WAIT_S = 3 * 3600.0     # ... and one waiting this long is skipped (said on it), never transcribed hours late
HIGH_MAX_WAIT_S = 300.0     # an airband clip waiting this long is shed: a late pre-alert is no use, and heavy airband
                            # (the AWS breaker open) must never hold up the live talkgroups below it for hours
LOW_MAX_WAIT_S = 900.0      # a context talkgroup's clip waiting this long is shed (skipped, said on it): whisper
                            # never falls further behind than this on traffic nobody acts on
BEHIND_S = 120.0            # the oldest live clip waiting this long: "behind" (context clips get one greedy pass)
RATE_WINDOW_S = 1800.0      # clips in / out per hour are counted over this long
LIVE_CLASSES = (URGENT, HIGH, LIVE)
SHED_AFTER = {HIGH: HIGH_MAX_WAIT_S, CONTEXT: CONTEXT_MAX_WAIT_S, LOW: LOW_MAX_WAIT_S}
SHED = "skipped: busy"      # what a shed clip's reason starts with: its status is "skipped", not "error" (re-runnable)
CLASS_NAMES = {URGENT: "urgent", HIGH: "high", LIVE: "live", RERUN: "rerun", CONTEXT: "context", LOW: "low"}


def wav_bytes(audio: np.ndarray, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    pcm = audio if audio.dtype == np.int16 else (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())
    return buf.getvalue()


DEEMPH_TAU = 750e-6     # land-mobile NFM de-emphasis (6 dB/octave, corner ~212 Hz)
TARGET_DBFS = -16.0     # loudness of the speech after processing
PEAK_LIMIT = 0.9
LINE_BAND = (1500.0, 5500.0)   # where the whine lives (FG1 bench: 2.5-5.4 kHz)
LINE_OVER_DB = 15.0            # a bin this far over its +/-40 Hz neighbourhood is a steady tone
LINE_HALF_HZ = 5.0             # notch width each side of a tone (voice harmonics move; tones don't)
BAND_TOP = (3300.0, 3700.0)    # flat to 3.3 kHz, eased off by 3.7 kHz (3.0/3.4 sounded like a tin can)


def notch_lines(X: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, list[float]]:
    """Notch steady tones out of a whole-clip spectrum.

    FG1 bench: a comb of pure tones 100 Hz apart (sidebands of the 100 Hz CTCSS on a beat,
    2.5-5.4 kHz, the same for the whole transmission) whined through every clip. Over a whole
    clip each tone is a line a few Hz wide, far above its neighbourhood; speech harmonics move
    with pitch and smear, so only the tones are cut (+/-5 Hz each). Spectral subtraction was
    tried first and made the voice sound like a tin can (musical noise, lost top end)."""
    df = f[1] - f[0] if len(f) > 1 else 1.0
    mag_db = 20 * np.log10(np.abs(X) + 1e-12)
    w = max(3, int(40.0 / df))
    c = np.concatenate([[0.0], np.cumsum(mag_db)])
    lo = np.clip(np.arange(len(f)) - w, 0, len(f))
    hi = np.clip(np.arange(len(f)) + w + 1, 0, len(f))
    baseline = (c[hi] - c[lo]) / (hi - lo)
    cand = (f >= LINE_BAND[0]) & (f <= LINE_BAND[1]) & (mag_db - baseline > LINE_OVER_DB)
    lines: list[float] = []
    gain = np.ones(len(f))
    for i in np.where(cand)[0]:
        if mag_db[i] < mag_db[max(0, i - 2):i + 3].max():
            continue                       # only the peak bin of each line
        lines.append(float(f[i]))
        gain[np.abs(f - f[i]) <= LINE_HALF_HZ] = 0.0
    return X * gain, lines


def radio_voice(audio: np.ndarray, rate: int = 16000, deemph: bool = True, nr: bool = True) -> np.ndarray:
    """FM discriminator output -> what a radio's speaker plays, at a steady loudness.

    De-emphasis (transmitters pre-emphasise voice), the whine's steady tones notched out (nr),
    300-3300 Hz band-pass (drops the CTCSS tone + DC drift below and the FM hiss above, which
    otherwise dominate the level), then
    set the *speech* loudness: RMS of the louder half of 20 ms blocks to TARGET_DBFS, with
    a peak limit. Used for both the clips people play and the audio speech-to-text gets.
    deemph=False for already-decoded digital voice (P25 calls from trunk-recorder)."""
    x = audio.astype(np.float64)
    if len(x) < 64:
        return x.astype(np.float32)
    X = np.fft.rfft(x - x.mean())
    f = np.fft.rfftfreq(len(x), 1 / rate)
    if deemph:
        X = X / (1 + 1j * 2 * np.pi * f * DEEMPH_TAU)
    if nr:
        X, _ = notch_lines(X, f)    # the whine (steady tones), nothing else
    # Gentle (raised-cosine) band edges: a brick wall rings audibly on short clips.
    gain = np.ones_like(f)
    t0, t1 = BAND_TOP
    gain[f < 200] = 0.0
    m = (f >= 200) & (f < 300)
    gain[m] = 0.5 - 0.5 * np.cos(np.pi * (f[m] - 200) / 100)
    gain[f > t1] = 0.0
    m = (f > t0) & (f <= t1)
    gain[m] = 0.5 + 0.5 * np.cos(np.pi * (f[m] - t0) / (t1 - t0))
    y = np.fft.irfft(X * gain, len(x))
    blk = rate // 50
    n = len(y) // blk
    if n >= 3:
        # Loudness of the speech itself: blocks 8 dB over the clip's dead-air floor (radio clips are
        # mostly dead air between words; averaging that in would leave the voice too quiet).
        e = np.sqrt((y[: n * blk].reshape(n, blk) ** 2).mean(axis=1)) + 1e-12
        db = 20 * np.log10(e)
        active = e[db > np.percentile(db, 5) + 8]
        speech_rms = float(np.sqrt(np.mean((active if len(active) else e) ** 2)))
    else:
        speech_rms = float(np.sqrt(np.mean(y ** 2)))
    if speech_rms <= 1e-9:
        return np.zeros(len(y), np.float32)
    # Gain from the speech level; clicks (key-up, squelch crash) are soft-clipped instead of
    # being allowed to turn the whole clip down. Only real speech peaks limit the gain.
    g = 10 ** (TARGET_DBFS / 20) / speech_rms
    body = float(np.percentile(np.abs(y), 99.5)) * g
    if body > PEAK_LIMIT:
        g *= PEAK_LIMIT / body
    z = y * g
    knee = 0.6
    over = np.abs(z) > knee
    z[over] = np.sign(z[over]) * (knee + (1 - knee) * np.tanh((np.abs(z[over]) - knee) / (1 - knee)))
    return z.astype(np.float32)


voice_band = radio_voice  # older name


class WhisperClient:
    def __init__(self, url: str, timeout_s: float = 60.0):
        self.url, self.timeout_s = url, timeout_s

    def transcribe(self, audio16k: np.ndarray, prompt: str = "", fast: bool = False) -> tuple[str, float | None]:
        """fast: one greedy pass, no temperature fallback (whisper-server decodes a hard clip up to 5 more times at
        higher temperatures): the context talkgroups' clips while the queue is behind (#300)."""
        pad = np.zeros(8000, dtype=np.float32)  # whisper likes a little silence around speech
        body_wav = wav_bytes(np.concatenate([pad, audio16k.astype(np.float32), pad]))
        boundary = uuid.uuid4().hex
        parts = []
        fields = [("response_format", "verbose_json"), ("temperature", "0.0")]
        if prompt:
            fields.append(("prompt", prompt))
        if fast:
            fields.append(("temperature_inc", "0.0"))
        for name, value in fields:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="tx.wav"\r\n'
                      "Content-Type: audio/wav\r\n\r\n").encode() + body_wav + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.url, data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            out = json.load(r)
        text = " ".join(str(out.get("text", "")).split())
        probs = [w.get("probability") for s in out.get("segments", []) for w in s.get("words", [])
                 if isinstance(w.get("probability"), (int, float))]
        return text, (round(float(np.mean(probs)), 2) if probs else None)


class SttQueue:
    """The whisper queue (#195, #300): strict priority, and never refuses the newest clip silently. put() past
    QUEUE_MAX (or past a channel's CHANNEL_CAP) drops the worst item instead (the lowest class, in it a stale clip
    before a fresh one, then the oldest), the new one only when it is the worst; get() takes URGENT, then HIGH, LIVE,
    RERUN, LOW: a class only when every class above it is empty. In a class the fresh items go in order, then the ones
    that waited STALE_S (the newest first). A live item that waited MAX_WAIT_S is skipped, a LOW one (a context
    talkgroup) LOW_MAX_WAIT_S and a HIGH one (airband) HIGH_MAX_WAIT_S: shed, so whisper never falls far behind on traffic nobody acts on. Every item dropped
    or skipped goes to on_drop(item, why): nothing is lost without a word on it."""

    def __init__(self, on_drop: Callable[[object, str], None] = lambda item, why: None,
                 clock: Callable[[], float] = time.time, maxsize: int = QUEUE_MAX, channel_cap: int = CHANNEL_CAP):
        self.on_drop, self.clock, self.maxsize, self.channel_cap = on_drop, clock, maxsize, channel_cap
        self._items: list[list] = []              # [priority, seq, item, audio, channel, at, fallback]
        self._cv = threading.Condition()
        self._seq = itertools.count()
        self.dropped = self.skipped = self.shed = 0
        self.last_drop: dict | None = None
        self._in: deque = deque()                 # (at, channel, priority, fallback) of each clip put, RATE_WINDOW_S

    def _key(self, e: list, now: float) -> tuple:
        prio, seq, at = e[0], e[1], e[5]
        if prio != RERUN and now - at >= STALE_S:
            return (prio, 1, -seq)                # waited too long: after its class's fresh ones, the newest first
        return (prio, 0, seq)

    def _worst(self, items: list[list], now: float) -> list:
        """The one to drop: the lowest class, in it a stale one before a fresh one, then the oldest."""
        def rank(x: list) -> tuple:
            k = self._key(x, now)
            return (k[0], k[1], -x[1])
        return max(items, key=rank)

    def qsize(self) -> int:
        with self._cv:
            return len(self._items)

    def put(self, entry: tuple, fallback: bool = False) -> bool:
        """entry (priority, seq-or-None, item, audio[, channel]). fallback: the hub's, for a clip AWS couldn't do.
        False: this item was the one dropped."""
        prio, _seq, item, audio = entry[:4]
        channel = entry[4] if len(entry) > 4 else None
        now = self.clock()
        e = [prio, next(self._seq), item, audio, channel, now, bool(fallback)]
        out: list[tuple[object, str]] = []
        with self._cv:
            self._items.append(e)
            if prio != RERUN:
                self._in.append((now, channel, prio, bool(fallback)))
                while self._in and now - self._in[0][0] > RATE_WINDOW_S:
                    self._in.popleft()
            mine = [x for x in self._items if channel is not None and x[4] == channel]
            if channel is not None and len(mine) > self.channel_cap:
                worst = self._worst(mine, now)
                self._items.remove(worst)
                out.append((worst[2], f"{channel}: {self.channel_cap} clips already waiting for speech-to-text"))
            if len(self._items) > self.maxsize:
                worst = self._worst(self._items, now)
                self._items.remove(worst)
                out.append((worst[2], f"speech-to-text queue full ({self.maxsize} waiting)"))
            self._cv.notify()
        self._dropped(out, "dropped")
        return all(x is not item for x, _ in out)

    def _dropped(self, out: list[tuple[object, str]], kind: str) -> None:
        for item, why in out:
            if kind == "dropped":
                self.dropped += 1
            elif kind == "shed":
                self.shed += 1
            else:
                self.skipped += 1
            self.last_drop = {"at": self.clock(), "why": why}
            try:
                self.on_drop(item, why)
            except Exception:  # the caller's trouble must not stop the queue
                pass

    def _take(self, now: float, skips: list, sheds: list) -> tuple | None:
        shed = [e for e in self._items if e[0] in SHED_AFTER and now - e[5] >= SHED_AFTER[e[0]]]
        old = [e for e in self._items if e[0] in LIVE_CLASSES and now - e[5] >= MAX_WAIT_S and e not in shed]
        for e in old + shed:
            self._items.remove(e)
        skips.extend((e[2], f"waited {int((now - e[5]) // 60)} min for speech-to-text: skipped, "
                                    "too far behind (Re-transcribe it if it matters)") for e in old)
        sheds.extend((e[2], f"{SHED}: speech-to-text {int((now - e[5]) // 60)} min behind, "
                            + ("an airband clip is no use late" if e[0] == HIGH else
                               "another system's talkgroup" if e[0] == CONTEXT else "a context talkgroup")
                            + " (Re-transcribe it if it matters)") for e in shed)
        if not self._items:
            return None
        e = min(self._items, key=lambda x: self._key(x, now))
        self._items.remove(e)
        return (e[0], e[1], e[2], e[3])

    def get(self, timeout: float | None = None) -> tuple:
        """(priority, seq, item, audio): the best item; queue.Empty after timeout (None: wait for one)."""
        skips: list[tuple[object, str]] = []
        sheds: list[tuple[object, str]] = []
        end = None if timeout is None else time.monotonic() + timeout
        try:
            with self._cv:
                while True:
                    got = self._take(self.clock(), skips, sheds)
                    if got is not None:
                        return got
                    left = None if end is None else end - time.monotonic()
                    if left is not None and left <= 0:
                        raise queue.Empty
                    self._cv.wait(left)
        finally:
            self._dropped(skips, "skipped")
            self._dropped(sheds, "shed")

    def get_nowait(self) -> tuple:
        return self.get(timeout=0)

    def behind(self) -> bool:
        """The oldest live clip (any class but re-runs) has waited BEHIND_S."""
        now = self.clock()
        with self._cv:
            return any(e[0] != RERUN and now - e[5] >= BEHIND_S for e in self._items)

    def stats(self) -> dict:
        """Depth by kind, the oldest clip's wait, where they come from: station status and health (the backlog
        alert says how many wait and why, sizeup/cloud/health.py). oldest_s: the oldest clip that matters
        (urgent, high, live); a context talkgroup's (low) is oldest_low_s."""
        now = self.clock()
        with self._cv:
            items = list(self._items)
            arrived = [x for x in self._in if now - x[0] <= RATE_WINDOW_S]
        live = [e for e in items if e[0] in LIVE_CLASSES]
        low = [e for e in items if e[0] == LOW]
        oldest = min((e[5] for e in live), default=None)
        oldest_low = min((e[5] for e in low), default=None)
        waiting: dict[str, int] = {}
        for e in items:
            if e[0] != RERUN:
                waiting[e[4] or "?"] = waiting.get(e[4] or "?", 0) + 1
        per_h = 3600.0 / RATE_WINDOW_S
        came: dict[str, int] = {}
        for _at, ch, _p, _f in arrived:
            came[ch or "?"] = came.get(ch or "?", 0) + 1
        return {"queued": len(items), "urgent": sum(1 for e in live if e[0] == URGENT),
                "context": sum(1 for e in items if e[0] == CONTEXT),       # another P25 system's alert clips (#409)
                "high": sum(1 for e in live if e[0] == HIGH), "live": len(live), "low": len(low),
                "rerun": sum(1 for e in items if e[0] == RERUN),
                "fallback": sum(1 for e in items if e[6]),
                "stale": sum(1 for e in live if now - e[5] >= STALE_S),
                "oldest_at": oldest, "oldest_s": round(now - oldest, 1) if oldest is not None else None,
                "oldest_low_s": round(now - oldest_low, 1) if oldest_low is not None else None,
                "top": sorted(waiting.items(), key=lambda kv: -kv[1])[:3],
                "in_h": round(len(arrived) * per_h),
                "in_low_h": round(sum(1 for x in arrived if x[2] == LOW) * per_h),
                "in_fallback_h": round(sum(1 for x in arrived if x[3]) * per_h),
                "in_top": [(ch, round(n * per_h)) for ch, n in sorted(came.items(), key=lambda kv: -kv[1])[:3]],
                "dropped": self.dropped, "skipped": self.skipped, "shed": self.shed, "last_drop": self.last_drop,
                "max": self.maxsize, "stale_s": STALE_S, "max_wait_s": MAX_WAIT_S, "low_max_wait_s": LOW_MAX_WAIT_S,
                "high_max_wait_s": HIGH_MAX_WAIT_S}


class Transcriber:
    """One worker thread: items with an `audio_for_stt()`-style accessor are transcribed in
    order and handed to on_done. whisper-server restarting -> retry with backoff. on_drop(item, why):
    an item the queue dropped or skipped (SttQueue), never transcribed."""

    RETRY_S = (2, 5, 8)                           # the backoff after each failed try (three tries)

    def __init__(self, client: WhisperClient, on_done: Callable[[object], None],
                 prompt: Callable[[object], str] = lambda item: "",
                 on_drop: Callable[[object, str], None] = lambda item, why: None):
        self.client, self.on_done, self.prompt = client, on_done, prompt
        self._q = SttQueue(self._dropped)
        self.on_drop = on_drop
        self._drops: queue.Queue = queue.Queue()
        self._drop_thread: threading.Thread | None = None
        self.last_error = ""
        self._working = False
        self._took: deque = deque(maxlen=50)      # (finished at, seconds) of the last transcriptions: whisper's pace

    def busy(self) -> int:
        """Transcriptions queued or running (the station's auto-update waits for none before a restart)."""
        return self._q.qsize() + (1 if self._working else 0)

    def submit(self, item, audio: np.ndarray, priority: int = LIVE, channel: str | None = None,
               fallback: bool = False) -> bool:
        """Queue it. False: it was dropped at once (the queue full of better items): on_drop has it already.
        fallback: the hub asked for it because Amazon Transcribe couldn't (counted: the backlog's cause)."""
        return self._q.put((priority, None, item, audio, channel), fallback=fallback)

    def stats(self) -> dict:
        out = dict(self._q.stats(), working=self._working, last_error=self.last_error)
        took = list(self._took)
        if took:
            out["service_s"] = round(sum(s for _t, s in took) / len(took), 1)       # seconds per clip, lately
            now = time.time()
            out["out_h"] = round(sum(1 for t, _s in took if now - t <= 600) * 6) if len(took) >= 5 else None
        return out

    def _dropped(self, item, why: str) -> None:
        """on_drop on its own thread: never in the context of submit(), whose caller may hold its own locks."""
        self._drops.put((item, why))
        if self._drop_thread is None:
            self._drop_thread = threading.Thread(target=self._drop_loop, name="stt-drops", daemon=True)
            self._drop_thread.start()

    def _drop_loop(self) -> None:
        while True:
            item, why = self._drops.get()
            try:
                self.on_drop(item, why)
            except Exception:  # the caller's trouble must not stop the next one
                pass

    def start(self) -> Transcriber:
        threading.Thread(target=self._run, name="stt", daemon=True).start()
        return self

    def _run(self) -> None:
        while True:
            self._working = False
            prio, _n, item, audio = self._q.get()
            self._working = True
            # behind: a context talkgroup's clip gets one greedy pass (no temperature fallback), #300
            fast = {"fast": True} if prio in (LOW, CONTEXT) and self._q.behind() else {}
            for attempt in range(3):
                try:
                    t0 = time.monotonic()
                    # No noise reduction for the model: on the FG1 bench it cut the whine 15 dB for listening
                    # but changed 1 of 4 real transcripts ("Testing one two" -> "Nothing wanted").
                    raw, conf = self.client.transcribe(radio_voice(audio, deemph=getattr(item, "fm", True), nr=False),
                                                      self.prompt(item), **fast)
                    self._took.append((time.time(), time.monotonic() - t0))
                    break
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:160]
                    time.sleep(self.RETRY_S[attempt])
            else:
                item.status = "error"
                self.on_done(item)
                continue
            said = re.sub(r"[^a-z0-9 ]", "", raw.lower()).strip()
            if (not raw or NOISE_LABEL.match(raw) or not said  # "." = squelch tail
                    or (said in HALLUCINATIONS
                        and (len(audio) / 16000 < SHORT_S or getattr(item, "low_energy", False)))):
                raw, status = "", "no_speech"
            else:
                status = "done"
            item.raw, item.confidence = raw, conf
            item.status = status                # last: a reader that sees "done" sees its text (and read_twice)
            self.on_done(item)
