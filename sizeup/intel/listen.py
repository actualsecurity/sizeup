"""A "Listen" link for a radio pre-alert push: the transmissions around it, stitched into one small file a phone plays,
behind an unguessable S3 presigned URL that expires (user, 2026-11-12: no sign-in on this link, so it is short-lived
and random). Usable by any radio pre-alert (intel, and later the airport and 60 Control ones): a small interface.

    listen = Listen(store, rows, clip, settings, clock, log)
    url = listen.link(ref, sources, traffic_ids, first_at, last_at)     # None: off, not configured, nothing to play
    listen.refresh(now)                                                 # re-stitch as later transmissions arrive
    listen.status()                                                     # {configured, why, active}

What goes in: the pre-alert's own transmissions, and every transmission on the same sources from before_s before its
first to after_s after its last (Settings > System > Intel), oldest first, a short silence and a soft tick between two.
Never a source that is only recorded or encrypted (tiers record / off / activity: there is no audio of an encrypted
talkgroup anyway), never a row whose audio is gone.

The file: AAC in M4A, mono, at listen_kbps (24) when ffmpeg is here; else 8 kHz 16-bit WAV (the hub's image has no
ffmpeg: ~1 MB a minute, still fine on a phone). Its object key is random (listen/<32 random characters>.<ext>), never
the event's id, and carries no metadata; Content-Type and an inline Content-Disposition are set on the presigned GET.
While the after-window is open (and a little longer, transcripts come late) the file is stitched again whenever the
set of transmissions changes and written to the SAME key, so the link already on a phone plays the newest one.

The bucket: SIZEUP_AUDIO_BUCKET (the hub's audio bucket), prefix listen/. The hub's task role needs PutObject / GetObject
on listen/*, and a lifecycle rule removes listen/ objects after a day (sizeup-aws/27-listen-links.sh, run by a person).
Until then the first upload is refused: the status says "not configured" and it is tried again only after RETRY_S.
"""

from __future__ import annotations

import io
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

PREFIX = "listen/"
RATE = 8000
GAP_S = 0.35
TICK_S = 0.06
RETRY_S = 3600.0
LATE_S = 180.0                 # transcripts and clips arrive after the transmission: keep refreshing this much longer
MAX_CLIPS = 40
MAX_SECONDS = 600.0
EXCLUDED_TIERS = ("record", "off", "activity")
DEFAULTS = {"listen": True, "listen_expiry_min": 20, "listen_before_s": 45, "listen_after_s": 60, "listen_kbps": 24}


@dataclass
class Item:
    key: str
    sources: list
    traffic_ids: list
    first_at: float
    last_at: float
    clips: tuple = ()
    made_at: float = 0.0
    content_type: str = "audio/wav"
    bytes: int = 0
    versions: int = 0
    refs: list = field(default_factory=list)


def read_wav(data: bytes) -> np.ndarray:
    """16-bit PCM WAV -> float32 mono at RATE."""
    with wave.open(io.BytesIO(data)) as w:
        n, ch, width, rate = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError("16-bit PCM only")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != RATE and len(x):
        t = np.arange(0, len(x) / rate, 1.0 / RATE)
        x = np.interp(t, np.arange(len(x)) / rate, x).astype(np.float32)
    return x


def tick() -> np.ndarray:
    t = np.arange(int(TICK_S * RATE)) / RATE
    return (0.12 * np.sin(2 * np.pi * 1000 * t) * np.hanning(len(t))).astype(np.float32)


def stitch(clips: list[bytes]) -> np.ndarray:
    """The clips in order with a short silence, a soft tick and a short silence between two."""
    gap = np.zeros(int(GAP_S / 2 * RATE), dtype=np.float32)
    sep = np.concatenate([gap, tick(), gap])
    parts: list[np.ndarray] = []
    total = 0.0
    for c in clips:
        try:
            x = read_wav(c)
        except (wave.Error, ValueError, EOFError):
            continue
        if parts:
            parts.append(sep)
        parts.append(x)
        total += len(x) / RATE
        if total >= MAX_SECONDS:
            break
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def wav_bytes(x: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def encode(x: np.ndarray, kbps: int = 24, ffmpeg: str | None = None) -> tuple[bytes, str, str]:
    """(bytes, content type, extension): AAC / M4A with ffmpeg, else 8 kHz 16-bit WAV."""
    wav = wav_bytes(x)
    ff = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
    if ff:
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "in.wav"), os.path.join(d, "out.m4a")
            with open(src, "wb") as f:
                f.write(wav)
            try:
                subprocess.run([ff, "-nostdin", "-loglevel", "error", "-y", "-i", src, "-ac", "1", "-c:a", "aac",
                                "-b:a", f"{int(kbps)}k", "-movflags", "+faststart", dst], check=True, timeout=60,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with open(dst, "rb") as f:
                    return f.read(), "audio/mp4", "m4a"
            except (OSError, subprocess.SubprocessError):
                pass
    return wav, "audio/wav", "wav"


class S3ListenStore:
    """put / presign on the hub's audio bucket (sizeup/attachments.py S3, SigV4 with the task role)."""

    def __init__(self, bucket: str, region: str = "", endpoint: str = "", send=None, creds=None):
        from sizeup import aws
        from sizeup.attachments import S3
        self.aws = aws
        self.creds = creds or aws.credentials
        self.s3 = S3(bucket, region, endpoint, send=send, creds=self.creds)

    def put(self, key: str, body: bytes, ctype: str) -> None:
        self.s3.put(key, body, ctype)

    def presign(self, key: str, expires_s: int, ctype: str) -> str:
        from urllib.parse import quote
        url = f"{self.s3.url(key)}?response-content-disposition=inline&response-content-type={quote(ctype, safe='')}"
        return self.aws.presign(url, self.creds(), self.s3.region, "s3", expires=int(expires_s))


class Listen:
    def __init__(self, store, rows: Callable[[list, float, float], list[dict]], clip: Callable[[str], bytes | None],
                 settings: Callable[[], dict] | None = None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, ffmpeg: str | None = None, token=lambda: secrets.token_urlsafe(24)):
        self.store, self.rows, self.clip = store, rows, clip
        self.settings = settings or (lambda: {})
        self.clock, self.log, self.ffmpeg, self.token = clock, log, ffmpeg, token
        self.items: dict[str, Item] = {}
        self.why = "" if store is not None else "no audio bucket (SIZEUP_AUDIO_BUCKET)"
        self._refused_at = 0.0
        self._lock = threading.Lock()

    def cfg(self) -> dict:
        out = dict(DEFAULTS)
        try:
            out.update({k: v for k, v in (self.settings() or {}).items() if k in DEFAULTS})
        except Exception:  # noqa: BLE001 - the defaults
            pass
        return out

    @property
    def configured(self) -> bool:
        return self.store is not None and not (self._refused_at and self.clock() - self._refused_at < RETRY_S)

    def status(self) -> dict:
        return {"configured": self.configured, "why": self.why if not self.configured else "", "active": len(self.items),
                "enabled": bool(self.cfg()["listen"])}

    def _clips(self, it: Item, c: dict) -> list[str]:
        t0, t1 = it.first_at - float(c["listen_before_s"]), it.last_at + float(c["listen_after_s"])
        rows = self.rows(it.sources, t0, t1) or []
        excluded = {str(r["id"]) for r in rows if str(r.get("tier") or "") in EXCLUDED_TIERS}
        ids = {str(r["id"]): float(r.get("start") or 0) for r in rows if str(r["id"]) not in excluded}
        for tid in it.traffic_ids:                        # its own, even when the window query missed one
            if str(tid) not in excluded:
                ids.setdefault(str(tid), it.first_at)
        return [t for t, _s in sorted(ids.items(), key=lambda x: (x[1], x[0]))][:MAX_CLIPS]

    def _make(self, it: Item, c: dict) -> bool:
        """Stitch and upload (the same key); False when there is nothing to play or the bucket refused it."""
        ids = self._clips(it, c)
        audio = [(t, self.clip(t)) for t in ids]
        audio = [(t, a) for t, a in audio if a]
        if not audio:
            return False
        key_set = tuple(t for t, _a in audio)
        if key_set == it.clips and it.bytes:
            return True
        x = stitch([a for _t, a in audio])
        if not len(x):
            return False
        body, ctype, _ext = encode(x, int(c["listen_kbps"]), self.ffmpeg)
        try:
            self.store.put(it.key, body, ctype)
        except Exception as e:  # noqa: BLE001 - a link that can't be made is just not in the push
            self._refused_at = self.clock()
            self.why = f"not configured: the audio bucket refused listen/ ({type(e).__name__}); run sizeup-aws 27"
            self.log(f"listen: upload refused ({type(e).__name__}): no Listen links for an hour")
            return False
        it.clips, it.made_at, it.content_type, it.bytes, it.versions = key_set, self.clock(), ctype, len(body), it.versions + 1
        return True

    def link(self, ref: str, sources, traffic_ids, first_at: float, last_at: float) -> str | None:
        """A presigned URL that plays the transmissions around this pre-alert, or None."""
        c = self.cfg()
        if not c["listen"] or not self.configured:
            return None
        with self._lock:
            it = self.items.get(ref)
            if it is None:
                ext = "m4a" if (self.ffmpeg if self.ffmpeg is not None else shutil.which("ffmpeg")) else "wav"
                it = Item(key=f"{PREFIX}{self.token()}.{ext}", sources=list(sources), traffic_ids=list(traffic_ids),
                          first_at=float(first_at), last_at=float(last_at), refs=[ref])
            else:
                it.traffic_ids = list(dict.fromkeys([*it.traffic_ids, *traffic_ids]))
                it.last_at = max(it.last_at, float(last_at))
            if not self._make(it, c):
                return None
            self.items[ref] = it
        try:
            return self.store.presign(it.key, int(float(c["listen_expiry_min"]) * 60), it.content_type)
        except Exception as e:  # noqa: BLE001
            self.log(f"listen: presign failed ({type(e).__name__})")
            return None

    def refresh(self, now: float | None = None) -> int:
        """Stitch again what changed while an item's after-window is open; forget items past it. Uploads made."""
        now = self.clock() if now is None else now
        c = self.cfg()
        n = 0
        with self._lock:
            for ref, it in list(self.items.items()):
                end = it.last_at + float(c["listen_after_s"]) + LATE_S
                if now > max(end, it.first_at + float(c["listen_expiry_min"]) * 60):
                    del self.items[ref]
                    continue
                if now <= end:
                    before = it.versions
                    self._make(it, c)
                    n += it.versions - before
        return n
