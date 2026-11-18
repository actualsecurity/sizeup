"""A "Listen" link for a radio pre-alert push: the transmissions around it, stitched into one small file a phone plays,
behind an unguessable S3 presigned URL that expires (user, 2026-11-12: no sign-in on this link, so it is short-lived
and random; 2026-11-17: an hour by default, and the file deleted once its link has expired). Usable by any radio
pre-alert (intel, and later the airport and 60 Control ones): a small interface.

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

The cleanup: a link is a bearer URL until it expires, and its file is no use after. The hub deletes each listen/ object
DELETE_MARGIN_S after the LAST presign of its key has expired (a refreshed key is signed again: only its final expiry
counts), on the listen thread. Nothing of this is stored, so after a restart (or a standby taking over) the first sweep,
once a link signed before this process led could no longer play (its start + listen_expiry_min + the margin), lists
listen/ (ListObjectsV2) and deletes what was written before then and isn't this process's own. That needs DeleteObject
on listen/* and ListBucket (prefix listen/); until the grant is there an AccessDenied is logged once, shown on the Health
row, and the cleanup rests CLEANUP_REST_S: the one-day lifecycle rule is the backstop.
Until then the first upload is refused (403): the status says "not configured" and it is tried again only after RETRY_S;
any other failure (a timeout, the network) is tried again after RETRY_SOON_S.

Nothing here makes the push wait: link() presigns the (random, not yet written) key and returns at once; the stitching
and the upload run on the "listen" thread, which also does refresh()'s work (the engine thread only queues it). The
presigned URL is signed with credentials that outlive it (else they are fetched again first).

The self-test: S3 checks a presigned URL only when it is played, so a link refused for its signature (or the grant)
looks fine here and fails on the phone. After the first upload in a process, the link a push carries is fetched once
the way a phone would (GET, Range bytes=0-0: a presigned GET's signature covers the method, so never HEAD), on a
"listen-check" thread of its own with a short timeout. ok, failed (the HTTP status and S3's <Code>) or unchecked (it
could not be reached); not ok, it is fetched again after a later upload, at most every CHECK_RETRY_S. One log line on the
first result and on each change, never the URL (a bearer link); status()["links"] and health() (Settings > Station >
Health, Intel) show it.
"""

from __future__ import annotations

import io
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import numpy as np

from sizeup import tz

PREFIX = "listen/"
RATE = 8000
GAP_S = 0.35
TICK_S = 0.06
RETRY_S = 3600.0               # the bucket refused (403: the IAM grant isn't there): tried again after this long
RETRY_SOON_S = 60.0            # anything else (a timeout, a 5xx, the network): tried again after this long
CRED_MARGIN_S = 300.0          # presign with credentials that outlive the link by at least this much
CHECK_TIMEOUT_S = 5.0          # the self-test's GET of a pushed link
CHECK_RETRY_S = 900.0          # a link not ok (refused, unreachable): fetched again after a later upload, at most this often
DELETE_MARGIN_S = 120.0        # a file is deleted this long after its last link expired
CLEANUP_REST_S = 3600.0        # the cleanup refused (AccessDenied): the lifecycle rule does it; tried again after this
MAX_PENDING = 2000             # deletes waiting at most (past it the oldest are left to the lifecycle rule)
LATE_S = 180.0                 # transcripts and clips arrive after the transmission: keep refreshing this much longer
MAX_CLIPS = 40
MAX_SECONDS = 600.0
EXCLUDED_TIERS = ("record", "off", "activity")
DEFAULTS = {"listen": True, "listen_expiry_min": 60, "listen_before_s": 45, "listen_after_s": 60, "listen_kbps": 24}


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
    url: str = ""                      # the last presigned URL handed out (a bearer link: never logged)
    signed_until: float = 0.0          # when the last link handed out expires


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

    def __init__(self, bucket: str, region: str = "", endpoint: str = "", send=None, creds=None, clock=time.time):
        from sizeup import aws
        from sizeup.attachments import S3
        self.aws = aws
        self.creds = creds or aws.credentials
        self.own_creds = creds is None
        self.clock = clock
        self.s3 = S3(bucket, region, endpoint, send=send, creds=self.creds)

    def put(self, key: str, body: bytes, ctype: str) -> None:
        self.s3.put(key, body, ctype)

    def presign(self, key: str, expires_s: int, ctype: str) -> str:
        from urllib.parse import quote
        url = f"{self.s3.url(key)}?response-content-disposition=inline&response-content-type={quote(ctype, safe='')}"
        c = self.creds()
        if c.expires is not None and c.expires - self.clock() < expires_s + CRED_MARGIN_S and self.own_creds:
            self.aws.forget_credentials()          # the task role's: fetched again, so the link outlives its expiry
            c = self.creds()
        return self.aws.presign(url, c, self.s3.region, "s3", expires=int(expires_s))

    def probe(self, url: str, timeout: float) -> tuple[int, bytes]:
        """GET a presigned URL as a phone would, its first byte only: (HTTP status, body). OSError: unreachable."""
        from sizeup import outbound
        why = outbound.check("aws s3", url)
        if why:
            raise OSError(why)
        status, _h, data = self.s3.send("GET", url, {"Range": "bytes=0-0"}, None, timeout)
        return int(status), data or b""

    def delete(self, key: str) -> None:
        """DeleteObject (a missing key is gone too). AwsError with the status (403: not permitted) otherwise."""
        try:
            status, _h, data = self.s3._call("DELETE", key, {})
        except OSError as e:
            raise self.aws.AwsError(f"S3 DELETE: {type(e).__name__}", "Network") from None
        if status not in (200, 204, 404):
            raise self.aws.AwsError(f"S3 DELETE: {status} {s3_code(data)}", s3_code(data), status)

    def list(self, prefix: str = PREFIX) -> list[tuple[str, float]]:
        """ListObjectsV2 under prefix: [(key, last modified)]. AwsError with the status (403: not permitted)."""
        from urllib.parse import quote

        from sizeup.audiostore import _unxml
        out: list[tuple[str, float]] = []
        token = ""
        for _ in range(100):
            q = f"list-type=2&max-keys=1000&prefix={quote(prefix, safe='')}"
            if token:
                q = f"continuation-token={quote(token, safe='')}&" + q
            try:
                status, _h, data = self.s3._call("GET", "", {}, query=q)
            except OSError as e:
                raise self.aws.AwsError(f"S3 LIST: {type(e).__name__}", "Network") from None
            text = (data or b"").decode(errors="replace")
            if status != 200 or "<ListBucketResult" not in text:
                raise self.aws.AwsError(f"S3 LIST: {status} {s3_code(data)}", s3_code(data), status)
            for block in re.findall(r"<Contents>(.*?)</Contents>", text, re.S):
                k = re.search(r"<Key>(.*?)</Key>", block, re.S)
                t = re.search(r"<LastModified>(.*?)</LastModified>", block, re.S)
                if k and t:
                    out.append((_unxml(k.group(1)), datetime.fromisoformat(t.group(1).replace("Z", "+00:00")).timestamp()))
            m = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", text, re.S)
            if "<IsTruncated>true</IsTruncated>" not in text or not m:
                break
            token = _unxml(m.group(1))
        return out


def s3_code(data: bytes) -> str:
    """S3's error <Code> in an XML body (SignatureDoesNotMatch, AccessDenied, ExpiredToken ...), else ""."""
    m = re.search(rb"<Code>([A-Za-z0-9.]{1,64})</Code>", data or b"")
    return m.group(1).decode() if m else ""


def refused(e: Exception) -> bool:
    """The bucket said no for good (403: the grant isn't there), not a passing failure."""
    return getattr(e, "status", 0) == 403


class Listen:
    def __init__(self, store, rows: Callable[[list, float, float], list[dict]], clip: Callable[[str], bytes | None],
                 settings: Callable[[], dict] | None = None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, ffmpeg: str | None = None, token=lambda: secrets.token_urlsafe(24),
                 tiers: Callable[[list], dict] | None = None, worker: bool = True,
                 spawn: Callable[[Callable[[], None]], None] | None = None):
        self.store, self.rows, self.clip, self.tiers = store, rows, clip, tiers
        self.settings = settings or (lambda: {})
        self.clock, self.log, self.ffmpeg, self.token = clock, log, ffmpeg, token
        self.items: dict[str, Item] = {}
        self.why = "" if store is not None else "no audio bucket (SIZEUP_AUDIO_BUCKET)"
        self._refused_at = 0.0
        self._retry_at = 0.0
        self._lock = threading.Lock()
        self.worker = worker
        self._jobs: list[str] = []
        self._wake = threading.Condition()
        self._thread: threading.Thread | None = None
        # the self-test of a pushed link (module doc): state ok | failed | unchecked, the HTTP status, S3's code
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name="listen-check", daemon=True).start())
        self.check: dict = {"state": "unchecked", "status": 0, "code": "", "at": 0.0}
        self._checked_at = 0.0
        self._checking = False
        self._logged: tuple | None = None
        # the cleanup (module doc): files whose last link has expired, deleted on the listen thread
        self._deletes: dict[str, float] = {}           # key -> when it may be deleted
        self._led_since = 0.0                          # the first link() / refresh() here: this process leads
        self._swept = False
        self._cleanup_at = 0.0                          # not before (after a refusal or a failure)
        self.cleanup: dict = {"state": "", "at": 0.0, "code": ""}     # "" (nothing yet) | ok | denied | failed

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
                "enabled": bool(self.cfg()["listen"]), "links": dict(self.check),
                "cleanup": {**self.cleanup, "pending": len(self._deletes)}}

    def health(self) -> dict | None:
        """Settings > Station > Health's Intel row for the links' self-test and the cleanup (the pack rows' shape), or
        None: off, or no bucket (the Intel card says "not configured")."""
        if self.store is None or not self.cfg()["listen"]:
            return None
        ck, cl = self.check, self.cleanup
        state, code, status = ck["state"], ck["code"], ck["status"]
        if state == "ok":
            text, level = "ok", "ok"
            title = f"A pushed Listen link played (HTTP {status}) at {tz.hhmm(ck['at'])}"
        elif state == "failed":
            text, level = f"refused ({code or status})", "warn"
            title = (f"A pushed Listen link was refused: HTTP {status}{f' {code}' if code else ''} at "
                     f"{tz.hhmm(ck['at'])}. Phones can't play these links. Checked again after a later push, at most "
                     f"every {int(CHECK_RETRY_S // 60)} min")
        else:
            text, level = "unchecked", "off"
            title = (f"Could not fetch a pushed Listen link ({code}) at {tz.hhmm(ck['at'])}" if ck["at"] else
                     "Checked after the first Listen link is uploaded")
        title += ". The self-test fetches a pushed link's first byte, as a phone would"
        if cl["state"] == "denied":
            text += " · cleanup not permitted"
            level = "warn"
            title += (f"\nCleanup: deleting or listing listen/ was refused ({cl['code'] or 'AccessDenied'}) at "
                      f"{tz.hhmm(cl['at'])}: files stay until the 1-day lifecycle rule removes them. The task role needs "
                      "DeleteObject on listen/* and ListBucket on listen/ (sizeup-aws 27)")
        elif cl["state"] == "failed":
            title += f"\nCleanup: failed ({cl['code']}) at {tz.hhmm(cl['at'])}: tried again in a minute"
        else:
            title += (f"\nCleanup: each file deleted {int(DELETE_MARGIN_S // 60)} min after its last link expired"
                      f" ({len(self._deletes)} waiting)")
        return {"pack": "listen", "label": "Listen links", "on": True, "level": level, "text": text, "title": title}

    # -- the self-test ------------------------------------------------------------------------------------------

    def _check_due(self) -> bool:
        if self._checking or self.check["state"] == "ok":
            return False
        return not self._checked_at or self.clock() - self._checked_at >= CHECK_RETRY_S

    def _maybe_check(self, it: Item) -> None:
        """After an upload (the listen thread): the link it carries fetched on a thread of its own, when due."""
        probe = getattr(self.store, "probe", None)
        url = it.url
        if probe is None or not url:
            return
        with self._lock:
            if not self._check_due():
                return
            self._checking = True
            self._checked_at = self.clock()
        try:
            self.spawn(lambda: self._check(probe, url))
        except Exception:  # noqa: BLE001 - no thread: tried again after the next upload
            with self._lock:
                self._checking = False

    def _check(self, probe, url: str) -> None:
        try:
            try:
                status, body = probe(url, CHECK_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 - unreachable (a timeout, the network, outbound off): unchecked
                res = {"state": "unchecked", "status": 0, "code": type(e).__name__}
            else:
                ok = 200 <= status < 300
                res = {"state": "ok" if ok else "failed", "status": status, "code": "" if ok else s3_code(body)}
            res["at"] = self.clock()
            with self._lock:
                self.check = res
                key = (res["state"], res["code"])
                changed, self._logged = key != self._logged, key
            if changed:
                why = res["code"] or (f"HTTP {res['status']}" if res["status"] else "")
                word = {"ok": "ok", "failed": "refused"}.get(res["state"], "unchecked")
                self.log(f"intel: listen links {word}" + (f" ({why})" if why and res["state"] != "ok" else ""))
        finally:
            with self._lock:
                self._checking = False

    def _ffmpeg(self) -> bool:
        return bool(self.ffmpeg if self.ffmpeg is not None else shutil.which("ffmpeg"))

    # -- the clips ------------------------------------------------------------------------------------------------

    def _clips(self, it: Item, c: dict) -> list[str]:
        t0, t1 = it.first_at - float(c["listen_before_s"]), it.last_at + float(c["listen_after_s"])
        rows = self.rows(it.sources, t0, t1) or []
        tier = {str(r["id"]): str(r.get("tier") or "") for r in rows}
        missing = [str(t) for t in it.traffic_ids if str(t) not in tier]
        if missing and self.tiers is not None:          # its own transmissions outside the window: their tiers too
            tier.update({str(k): str(v or "") for k, v in (self.tiers(missing) or {}).items()})
        start = {str(r["id"]): float(r.get("start") or 0) for r in rows}
        ok = [t for t in dict.fromkeys([*start, *map(str, it.traffic_ids)])
              if t in tier and tier[t] not in EXCLUDED_TIERS]          # unknown tier: never (fail closed)
        return sorted(ok, key=lambda t: (start.get(t, it.first_at), t))[:MAX_CLIPS]

    def _make(self, it: Item, c: dict) -> bool:
        """Stitch and upload (the same key); False when there is nothing to play or the upload failed."""
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
        except Exception as e:  # noqa: BLE001 - a link that can't be made is just a link that doesn't play
            if refused(e):
                self._refused_at = self.clock()
                self.why = f"not configured: the audio bucket refused listen/ ({type(e).__name__}); run sizeup-aws 27"
                self.log(f"listen: upload refused ({type(e).__name__}): no Listen links for an hour")
            else:
                self._retry_at = self.clock() + RETRY_SOON_S
                self.log(f"listen: upload failed ({type(e).__name__}): trying again in a minute")
            return False
        it.clips, it.made_at, it.content_type, it.bytes, it.versions = key_set, self.clock(), ctype, len(body), it.versions + 1
        self._maybe_check(it)
        return True

    # -- the interface ------------------------------------------------------------------------------------------

    def link(self, ref: str, sources, traffic_ids, first_at: float, last_at: float) -> str | None:
        """A presigned URL for this pre-alert's audio, at once (the file is written on the listen thread), or None."""
        c = self.cfg()
        if not c["listen"] or not self.configured:
            return None
        expires_s = int(float(c["listen_expiry_min"]) * 60)
        with self._lock:
            self._led_since = self._led_since or self.clock()
            it = self.items.get(ref)
            if it is None:
                ff = self._ffmpeg()
                it = Item(key=f"{PREFIX}{self.token()}.{'m4a' if ff else 'wav'}", sources=list(sources),
                          traffic_ids=list(traffic_ids), first_at=float(first_at), last_at=float(last_at), refs=[ref],
                          content_type="audio/mp4" if ff else "audio/wav")
                self.items[ref] = it
            else:
                it.traffic_ids = list(dict.fromkeys([*it.traffic_ids, *traffic_ids]))
                it.last_at = max(it.last_at, float(last_at))
            # before the presign, under the lock: refresh() never forgets (and so deletes) a key being signed again
            it.signed_until = max(it.signed_until, self.clock() + expires_s)
        try:
            url = self.store.presign(it.key, expires_s, it.content_type)
        except Exception as e:  # noqa: BLE001
            self.log(f"listen: presign failed ({type(e).__name__})")
            return None
        it.url = url
        self._queue(ref)
        return url

    def refresh(self, now: float | None = None) -> int:
        """Queue what may have new transmissions (its after-window still open); forget what is past (its file deleted
        DELETE_MARGIN_S after its last link expired). Never stitches, uploads or deletes here (the engine's thread calls
        it): the listen thread does. Items queued."""
        now = self.clock() if now is None else now
        c = self.cfg()
        n = 0
        with self._lock:
            self._led_since = self._led_since or now
            for ref, it in list(self.items.items()):
                end = it.last_at + float(c["listen_after_s"]) + LATE_S
                if now > max(end, it.signed_until):
                    del self.items[ref]
                    if it.signed_until:                 # signed (so maybe written): deleted after the last link expired
                        self._deletes[it.key] = it.signed_until + DELETE_MARGIN_S
                        while len(self._deletes) > MAX_PENDING:     # the lifecycle rule takes the oldest
                            self._deletes.pop(min(self._deletes, key=self._deletes.__getitem__))
                elif now <= end and now >= self._retry_at:
                    self._jobs.append(ref)
                    n += 1
        self._kick()
        return n

    # -- the listen thread ------------------------------------------------------------------------------------

    def _queue(self, ref: str) -> None:
        with self._lock:
            self._jobs.append(ref)
        self._kick()

    def _kick(self) -> None:
        if not self.worker:
            return
        with self._wake:
            self._wake.notify()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="listen", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            if not self.drain():
                with self._wake:
                    if not self._jobs and not self._wake.wait(timeout=60.0) and not self._jobs:
                        return

    def drain(self) -> int:
        """Do the queued work here and now (the listen thread; tests), then the deletes that are due. Uploads made."""
        n = 0
        while True:
            with self._lock:
                jobs, self._jobs = list(dict.fromkeys(self._jobs)), []
            if not jobs:
                self.clean()
                return n
            c = self.cfg()
            for ref in jobs:
                it = self.items.get(ref)
                if it is None or not self.configured:
                    continue
                before = it.versions
                self._make(it, c)
                n += it.versions - before

    # -- the cleanup (the listen thread) --------------------------------------------------------------------------

    def clean(self, now: float | None = None) -> int:
        """Delete the files whose last link has expired, and once (after a link signed before this process led could
        no longer play) sweep listen/ for older ones nobody here knows. Never raises; objects deleted."""
        now = self.clock() if now is None else now
        delete = getattr(self.store, "delete", None)
        if delete is None or not self._led_since or now < self._cleanup_at:
            return 0
        expiry_s = float(self.cfg()["listen_expiry_min"]) * 60
        with self._lock:
            due = [k for k, at in self._deletes.items() if at <= now]
        sweep = (not self._swept and hasattr(self.store, "list")
                 and now >= self._led_since + expiry_s + DELETE_MARGIN_S)
        if not due and not sweep:
            return 0
        gone = 0
        try:
            for k in due:
                delete(k)
                with self._lock:
                    self._deletes.pop(k, None)
                gone += 1
            if sweep:
                with self._lock:
                    mine = {it.key for it in self.items.values()} | set(self._deletes)
                old = [k for k, at in self.store.list(PREFIX)
                       if k.startswith(PREFIX) and k not in mine and at < self._led_since]
                for k in old:
                    delete(k)
                    gone += 1
                self._swept = True
        except Exception as e:  # noqa: BLE001 - the one-day lifecycle rule is the backstop
            code = str(getattr(e, "code", "") or type(e).__name__)
            was = self.cleanup["state"]
            if refused(e):
                self._cleanup_at = now + CLEANUP_REST_S
                self.cleanup = {"state": "denied", "at": now, "code": code}
                if was != "denied":
                    self.log("intel: listen cleanup not permitted, relying on the 1-day lifecycle")
            else:
                self._cleanup_at = now + RETRY_SOON_S
                self.cleanup = {"state": "failed", "at": now, "code": code}
                if was != "failed":
                    self.log(f"intel: listen cleanup failed ({code}): trying again in a minute")
            return gone
        if self.cleanup["state"] in ("denied", "failed"):
            self.log("intel: listen cleanup ok")
        self.cleanup = {"state": "ok", "at": now, "code": ""}
        return gone
