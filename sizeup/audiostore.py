"""Where the hub's audio bytes live (GitHub #414): S3, with only their metadata in audio.sqlite (sizeup/cloud/hubstore.py
AudioDb), so the database (and with it Litestream's replica and a handover's restore) stays small.

    s3://<SIZEUP_AUDIO_BUCKET>/audio/<kind>/<yyyy>/<mm>/<dd>/<id>.wav     kind: clip | dispatch; the date is the clip's
                                                                          own (UTC), so a key never changes

The bytes are the WAV as the station sent it (8 kHz mono 16-bit PCM: 57.6 MB per hour of speech, ~16 MB per hour of
the day at our traffic, measured 2026-11-10). Nothing is re-encoded: the transcription, Replay and the attachments
copy (sizeup/attachments.py) all take that WAV.

    S3AudioStore      the hub: one private bucket (sizeup-aws 15-audio-storage.sh: Block Public Access, SSE-S3, TLS
                      only, versioning off, a lifecycle hard max on audio/clip/), signed with the task role
                      (sizeup/aws.py, no boto3). PUT is idempotent (same key, same bytes); deletes go in DeleteObjects
                      calls of at most 1000 keys. No presigned URLs: the hub reads the bytes and serves them itself,
                      behind Cloudflare Access.
    FsAudioStore      the same on a local folder (tests, a dev hub; SIZEUP_AUDIO_DIR)
    DiskCache         a small LRU of recent clips on the task's disk, so live listening and replays don't go to S3

from_env(): SIZEUP_AUDIO_BUCKET (S3), else SIZEUP_AUDIO_DIR (a folder), else None: the bytes stay in audio.sqlite as
before #414.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from sizeup import aws

PREFIX = "audio"
KINDS = ("clip", "dispatch")
DELETE_MAX = 1000                     # keys per DeleteObjects call (S3's limit)
TIMEOUTS = {"PUT": 8.0, "GET": 15.0, "POST": 30.0}   # s: a clip's PUT on the ingest path is short (the retry is the spool's)
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,160}")


class StoreError(RuntimeError):
    pass


# -- the retention per channel / talkgroup (Settings > System > Audio retention; sizeup/cad/settings.py) --------------
# The settings section's keys: "_default" (every clip without its own), a channel's label ("FG1", "South 2", "HPN
# Tower") or "tg:<n>" (a P25 talkgroup, as in Settings > Radios), each -> days. A talkgroup's own beats its channel's
# name, which beats the default. Read at every prune, so a change applies to the clips already kept (retroactive).
RETENTION_SECTION = "audio_retention"
DEFAULT_KEY = "_default"
RETENTION_DAYS = (0.5, 365.0)         # min, max; the bucket's lifecycle hard max (15-audio-storage.sh) is 400 days
TG_KEY = re.compile(r"tg:(\d{1,9})")
CHANNEL_KEY = re.compile(r"[^\x00-\x1f:]{1,60}")


def retention_key(channel: str, talkgroup: int | None) -> str:
    return f"tg:{talkgroup}" if talkgroup is not None else channel


def keep_days(ov: dict, channel: str, talkgroup: int | None, default_days: float) -> float:
    """The days a clip on this channel / talkgroup is kept: its talkgroup's, its channel's, the default override's,
    else default_days ([storage] audio_keep_hours)."""
    for k in ((f"tg:{talkgroup}",) if talkgroup is not None else ()) + ((channel,) if channel else ()) + (DEFAULT_KEY,):
        v = ov.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
    return float(default_days)


def shortest_days(ov: dict, default_days: float) -> float:
    vals = [float(v) for v in ov.values() if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
    return min(vals + [float(ov.get(DEFAULT_KEY) or default_days)])


def object_key(kind: str, at: float, aid: str, prefix: str = PREFIX) -> str:
    """audio/<kind>/<yyyy>/<mm>/<dd>/<id>.wav (the clip's own UTC date)."""
    if kind not in KINDS:
        raise ValueError(f"kind: {kind!r}")
    if not SAFE.fullmatch(aid) or ".." in aid:
        raise ValueError(f"bad audio id {aid[:40]!r}")
    d = datetime.fromtimestamp(float(at or 0), timezone.utc)
    return f"{prefix}/{kind}/{d:%Y/%m/%d}/{aid}.wav"


class AudioStore:
    """put(key, bytes) -> etag; get(key) -> bytes (StoreError when it isn't there or can't be read);
    delete(keys) -> the keys gone (a key that was never there counts as gone)."""

    name = "?"

    def put(self, key: str, body: bytes) -> str:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def delete(self, keys: list[str]) -> set[str]:
        raise NotImplementedError


class FsAudioStore(AudioStore):
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.name = f"folder {self.root}"

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise StoreError(f"bad key {key!r}")
        return p

    def put(self, key: str, body: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(body)
        tmp.replace(p)
        return '"' + hashlib.md5(body).hexdigest() + '"'

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as e:
            raise StoreError(f"{key}: {type(e).__name__}") from None

    def delete(self, keys: list[str]) -> set[str]:
        gone = set()
        for k in keys:
            try:
                self._path(k).unlink(missing_ok=True)
                gone.add(k)
            except OSError:
                pass
        return gone


class S3AudioStore(AudioStore):
    """One bucket through sizeup/attachments.py's S3 (SigV4 with the task role; tests give a fake send)."""

    def __init__(self, bucket: str, region: str = "", endpoint: str = "", send: Callable | None = None,
                 creds: Callable[[], aws.Credentials] = aws.credentials):
        from sizeup.attachments import S3, _send
        base = send or _send

        def bounded(method, url, headers, body, timeout):       # the ingest path's PUT and the players' GET: short
            return base(method, url, headers, body, min(timeout, TIMEOUTS.get(method, timeout)))

        self.s3 = S3(bucket, region, endpoint, send=bounded, creds=creds)
        self.bucket = bucket
        self.name = f"s3://{bucket}"

    def put(self, key: str, body: bytes) -> str:
        try:
            self.s3.put(key, body, "audio/wav")
        except aws.AwsError as e:
            raise StoreError(str(e)) from None
        return '"' + hashlib.md5(body).hexdigest() + '"'     # a single PUT's ETag (SSE-S3): its MD5, checked by S3 (Content-MD5)

    def get(self, key: str) -> bytes:
        try:
            return self.s3.get(key)
        except aws.AwsError as e:
            raise StoreError(str(e)) from None

    def delete(self, keys: list[str]) -> set[str]:
        """DeleteObjects, at most DELETE_MAX keys a call (Quiet: only the failures come back). The keys gone."""
        gone: set[str] = set()
        for k in range(0, len(keys), DELETE_MAX):
            chunk = keys[k:k + DELETE_MAX]
            body = ("<Delete><Quiet>true</Quiet>" + "".join(f"<Object><Key>{escape(x)}</Key></Object>" for x in chunk)
                    + "</Delete>").encode()
            try:
                status, _, data = self.s3._call("POST", "", {
                    "Content-Type": "application/xml", "Content-Length": str(len(body)),
                    "Content-MD5": base64.b64encode(hashlib.md5(body).digest()).decode()}, body, query="delete=")
            except aws.AwsError as e:
                raise StoreError(str(e)) from None
            if status != 200:
                raise StoreError(f"S3 DeleteObjects: {status} {_code(data)}")
            failed = {_unxml(m) for m in re.findall(r"<Error>\s*<Key>(.*?)</Key>", data.decode(errors="replace"), re.S)}
            gone.update(x for x in chunk if x not in failed)
        return gone


def _code(data: bytes) -> str:
    m = re.search(rb"<Code>([^<]{1,64})</Code>", data or b"")
    return m.group(1).decode() if m else ""


def _unxml(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


class DiskCache:
    """Recent clips on the task's own disk (lost at a deploy, which is fine): at most max_bytes, the least recently
    used dropped first. Keyed by the object key."""

    def __init__(self, root: str | Path, max_bytes: int = 256 * 1024 * 1024):
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self._mu = threading.Lock()
        self._files: dict[str, tuple[float, int]] = {}     # name -> (last used, size)
        self._bytes = 0
        try:
            for p in self.root.glob("*.wav"):
                st = p.stat()
                self._files[p.name] = (st.st_mtime, st.st_size)
                self._bytes += st.st_size
        except OSError:
            pass

    @staticmethod
    def _name(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:40] + ".wav"

    def get(self, key: str) -> bytes | None:
        n = self._name(key)
        with self._mu:
            if n not in self._files:
                return None
            try:
                b = (self.root / n).read_bytes()
            except OSError:
                self._bytes -= self._files.pop(n)[1]
                return None
            self._files[n] = (time.monotonic(), len(b))
            return b

    def put(self, key: str, body: bytes) -> None:
        if len(body) > self.max_bytes // 4:
            return
        n = self._name(key)
        with self._mu:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                tmp = self.root / (n + ".tmp")
                tmp.write_bytes(body)
                tmp.replace(self.root / n)
            except OSError:
                return
            if n in self._files:
                self._bytes -= self._files[n][1]
            self._files[n] = (time.monotonic(), len(body))
            self._bytes += len(body)
            if self._bytes > self.max_bytes:
                for old, (_, size) in sorted(self._files.items(), key=lambda kv: kv[1][0]):
                    if self._bytes <= self.max_bytes:
                        break
                    try:
                        (self.root / old).unlink(missing_ok=True)
                    except OSError:
                        continue
                    del self._files[old]
                    self._bytes -= size

    def drop(self, keys) -> None:
        with self._mu:
            for k in keys:
                n = self._name(k)
                if n in self._files:
                    try:
                        (self.root / n).unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._bytes -= self._files.pop(n)[1]

    @property
    def bytes(self) -> int:
        return self._bytes


def from_env(log: Callable[[str], None] = print) -> AudioStore | None:
    bucket = os.environ.get("SIZEUP_AUDIO_BUCKET", "").strip()
    if bucket:
        return S3AudioStore(bucket)
    folder = os.environ.get("SIZEUP_AUDIO_DIR", "").strip()
    if folder:
        return FsAudioStore(folder)
    return None
