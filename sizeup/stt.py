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
from typing import Callable

import numpy as np

NOISE_LABEL = re.compile(r"^[\[(*].*[\])*]$")  # "(static)", "[MUSIC]" — not speech
# What whisper says about a short burst of noise (a mic click, a squelch tail).
# What whisper says for dead air / a mic click (FG1 bench: "Thank you." or "Yes." on 6 of 6
# dead-air cuts). Rejected only when the audio has no voice energy or is very short.
HALLUCINATIONS = {"thank you", "thanks", "thank you very much", "yes", "you", "bye", "bye bye", "okay",
                  "ok", "thanks for watching", "thank you for watching", "so", "oh", "the end"}
SHORT_S = 1.5
# Queue priority (SttQueue): what matters for dispatch first, a person's re-run next, anything that has waited
# too long last. #195: the station's whisper fell 9 h behind and the newest clips were refused and lost.
URGENT, LIVE, RERUN, STALE = -1, 0, 1, 2    # pages, Fire/EMS 15 and the firegrounds | other live traffic | re-runs
QUEUE_MAX = 2000            # items waiting at most: past it the worst one goes (lowest priority, then oldest)
CHANNEL_CAP = 400           # one channel's items waiting at most: past it that channel's worst one goes
STALE_S = 600.0             # a live item waiting this long goes behind everything fresh (newest stale first) ...
MAX_WAIT_S = 3 * 3600.0     # ... and one waiting this long is skipped (said on it), never transcribed hours late


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

    def transcribe(self, audio16k: np.ndarray, prompt: str = "") -> tuple[str, float | None]:
        pad = np.zeros(8000, dtype=np.float32)  # whisper likes a little silence around speech
        body_wav = wav_bytes(np.concatenate([pad, audio16k.astype(np.float32), pad]))
        boundary = uuid.uuid4().hex
        parts = []
        fields = [("response_format", "verbose_json"), ("temperature", "0.0")]
        if prompt:
            fields.append(("prompt", prompt))
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
    """The whisper queue (#195): never refuses the newest clip silently. put() past QUEUE_MAX (or past a
    channel's CHANNEL_CAP) drops the worst item instead (the lowest class, in it the oldest: a re-run before
    live traffic, a stale clip before a fresh one), the new one only when it is the worst; get() takes
    URGENT, then LIVE in order, then RERUN; a live item that has waited STALE_S goes after those (the newest
    stale first), and one that waited MAX_WAIT_S is skipped. Every item dropped or skipped goes to
    on_drop(item, why): nothing is lost without a word on it."""

    def __init__(self, on_drop: Callable[[object, str], None] = lambda item, why: None,
                 clock: Callable[[], float] = time.time, maxsize: int = QUEUE_MAX, channel_cap: int = CHANNEL_CAP):
        self.on_drop, self.clock, self.maxsize, self.channel_cap = on_drop, clock, maxsize, channel_cap
        self._items: list[list] = []              # [priority, seq, item, audio, channel, at]
        self._cv = threading.Condition()
        self._seq = itertools.count()
        self.dropped = self.skipped = 0
        self.last_drop: dict | None = None

    def _key(self, e: list, now: float) -> tuple:
        prio, seq, at = e[0], e[1], e[5]
        if prio in (URGENT, LIVE) and now - at >= STALE_S:
            return (STALE, prio, -seq)            # waited too long: after the fresh ones, the newest first
        return (prio, 0, seq)

    def _worst(self, items: list[list], now: float) -> list:
        """The one to drop: the lowest class (re-runs, then what waited too long), in it the oldest."""
        def rank(x: list) -> tuple:
            k = self._key(x, now)
            return (k[0], k[1], -x[1])
        return max(items, key=rank)

    def qsize(self) -> int:
        with self._cv:
            return len(self._items)

    def put(self, entry: tuple) -> bool:
        """entry (priority, seq-or-None, item, audio[, channel]). False: this item was the one dropped."""
        prio, _seq, item, audio = entry[:4]
        channel = entry[4] if len(entry) > 4 else None
        now = self.clock()
        e = [prio, next(self._seq), item, audio, channel, now]
        out: list[tuple[object, str]] = []
        with self._cv:
            self._items.append(e)
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
            else:
                self.skipped += 1
            self.last_drop = {"at": self.clock(), "why": why}
            try:
                self.on_drop(item, why)
            except Exception:  # the caller's trouble must not stop the queue
                pass

    def _take(self, now: float, skips: list) -> tuple | None:
        old = [e for e in self._items if e[0] in (URGENT, LIVE) and now - e[5] >= MAX_WAIT_S]
        for e in old:
            self._items.remove(e)
        skips.extend((e[2], f"waited {int((now - e[5]) // 60)} min for speech-to-text: skipped, "
                                    "too far behind (Re-transcribe it if it matters)") for e in old)
        if not self._items:
            return None
        e = min(self._items, key=lambda x: self._key(x, now))
        self._items.remove(e)
        return (e[0], e[1], e[2], e[3])

    def get(self, timeout: float | None = None) -> tuple:
        """(priority, seq, item, audio): the best item; queue.Empty after timeout (None: wait for one)."""
        skips: list[tuple[object, str]] = []
        end = None if timeout is None else time.monotonic() + timeout
        try:
            with self._cv:
                while True:
                    got = self._take(self.clock(), skips)
                    if got is not None:
                        return got
                    left = None if end is None else end - time.monotonic()
                    if left is not None and left <= 0:
                        raise queue.Empty
                    self._cv.wait(left)
        finally:
            self._dropped(skips, "skipped")

    def get_nowait(self) -> tuple:
        return self.get(timeout=0)

    def stats(self) -> dict:
        """Depth by kind and the oldest live item's wait: station status and health (the backlog alert)."""
        now = self.clock()
        with self._cv:
            items = list(self._items)
        live = [e for e in items if e[0] in (URGENT, LIVE)]
        oldest = min((e[5] for e in live), default=None)
        return {"queued": len(items), "urgent": sum(1 for e in live if e[0] == URGENT),
                "live": len(live), "rerun": sum(1 for e in items if e[0] == RERUN),
                "stale": sum(1 for e in live if now - e[5] >= STALE_S),
                "oldest_at": oldest, "oldest_s": round(now - oldest, 1) if oldest is not None else None,
                "dropped": self.dropped, "skipped": self.skipped, "last_drop": self.last_drop,
                "max": self.maxsize, "stale_s": STALE_S, "max_wait_s": MAX_WAIT_S}


class Transcriber:
    """One worker thread: items with an `audio_for_stt()`-style accessor are transcribed in
    order and handed to on_done. whisper-server restarting -> retry with backoff. on_drop(item, why):
    an item the queue dropped or skipped (SttQueue), never transcribed."""

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

    def busy(self) -> int:
        """Transcriptions queued or running (the station's auto-update waits for none before a restart)."""
        return self._q.qsize() + (1 if self._working else 0)

    def submit(self, item, audio: np.ndarray, priority: int = LIVE, channel: str | None = None) -> bool:
        """Queue it. False: it was dropped at once (the queue full of better items): on_drop has it already."""
        return self._q.put((priority, None, item, audio, channel))

    def stats(self) -> dict:
        return dict(self._q.stats(), working=self._working, last_error=self.last_error)

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
            _prio, _n, item, audio = self._q.get()
            self._working = True
            for attempt in range(3):
                try:
                    # No noise reduction for the model: on the FG1 bench it cut the whine 15 dB for listening
                    # but changed 1 of 4 real transcripts ("Testing one two" -> "Nothing wanted").
                    raw, conf = self.client.transcribe(radio_voice(audio, deemph=getattr(item, "fm", True), nr=False),
                                                      self.prompt(item))
                    break
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:160]
                    time.sleep(2 + 3 * attempt)
            else:
                item.status = "error"
                self.on_done(item)
                continue
            said = re.sub(r"[^a-z0-9 ]", "", raw.lower()).strip()
            if (not raw or NOISE_LABEL.match(raw) or not said  # "." = squelch tail
                    or (said in HALLUCINATIONS
                        and (len(audio) / 16000 < SHORT_S or getattr(item, "low_energy", False)))):
                raw, item.status = "", "no_speech"
            else:
                item.status = "done"
            item.raw, item.confidence = raw, conf
            self.on_done(item)
