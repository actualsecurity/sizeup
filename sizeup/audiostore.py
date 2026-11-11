"""Where the hub's audio bytes live (GitHub #414): S3, with only their metadata in audio.sqlite (sizeup/cloud/hubstore.py
AudioDb), so the database (and with it Litestream's replica and a handover's restore) stays small.

    s3://<SIZEUP_AUDIO_BUCKET>/audio/<kind>/<yyyy>/<mm>/<dd>/<id>.wav     kind: clip | dispatch; the date is the clip's
                                                                          own (UTC), so a key never changes

The bytes are the WAV as the station sent it (8 kHz mono 16-bit PCM: 57.6 MB per hour of speech, ~16 MB per hour of
the day at our traffic, measured 2026-11-10). Nothing is re-encoded: the transcription, Replay and the attachments
copy (sizeup/attachments.py) all take that WAV.

    S3AudioStore      the hub: one private bucket (sizeup-aws 18-audio-storage.sh: Block Public Access, SSE-S3, TLS
                      only, versioning off, a lifecycle hard max on audio/clip/), signed with the task role
                      (sizeup/aws.py, no boto3). PUT is idempotent (same key, same bytes); deletes go in DeleteObjects
                      calls of at most 1000 keys; list() (ListObjectsV2) is for the orphan sweep only. The task role
                      has s3:ListBucket on the bucket, so a missing key answers 404; a 403 is an error (never "not
                      found"), and only an expired / invalid token makes the credentials be fetched again. No
                      presigned URLs: the hub reads the bytes and serves them itself, behind Cloudflare Access.
    FsAudioStore      the same on a local folder (tests, a dev hub; SIZEUP_AUDIO_DIR)
    DiskCache         a small LRU of recent clips on the task's disk, so live listening and replays don't go to S3
    Retention         the days kept per channel / talkgroup (Settings > System > Audio retention), and the same as one
                      SQL expression for the prune

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
from typing import Callable, Iterator
from urllib.parse import quote
from xml.sax.saxutils import escape

from sizeup import aws

PREFIX = "audio"
KINDS = ("clip", "dispatch")
DELETE_MAX = 1000                     # keys per DeleteObjects call (S3's limit)
TIMEOUTS = {"PUT": 15.0, "GET": 15.0, "POST": 30.0}   # s: the uploader's PUT, the players' GET, a DeleteObjects
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,160}")
TOKEN_CODES = ("ExpiredToken", "InvalidToken", "TokenRefreshRequired", "InvalidAccessKeyId")   # 403s: new credentials
DAY = 86400.0


class StoreError(OSError):
    """The store couldn't do it (network, S3's 5xx, a 403, a missing key). An OSError, so the attachments copy
    (sizeup/attachments.py _transient) tries again later instead of storing an attachment without its audio. missing:
    the object isn't there (S3's NoSuchKey): no use trying again. config: the store itself refuses (another 404, a
    NoSuchBucket, a 403 AccessDenied): its setup, tried again and said loudly."""

    def __init__(self, msg: str = "", missing: bool = False, config: bool = False):
        super().__init__(msg)
        self.missing, self.config = missing, config


# -- the retention per channel / talkgroup (Settings > System > Audio retention; sizeup/cad/settings.py) --------------
# The settings section's keys: "_default" (every clip without its own), a channel's label ("FG1", "South 2", "HPN
# Tower") or "tg:<n>" (a P25 talkgroup, as in Settings > Radios; no leading zeros), each -> days. A talkgroup's own
# beats its channel's name, which beats the default. Read at every prune, so a change applies to the clips already
# kept (retroactive).
RETENTION_SECTION = "audio_retention"
DEFAULT_KEY = "_default"
# "_format": how new clips are kept. Only "wav" (the station's 8 kHz 16-bit PCM, uncompressed) for now: the place a
# lossless (FLAC) or lossy (Opus) encoding would be chosen later; nothing else is accepted until one is written.
FORMAT_KEY = "_format"
# The transcripts and metadata of a clip (the CAD's radio log): their own section, the same keys; never kept shorter than
# the clip's audio (the prune uses the longer of the two).
TRANSCRIPT_SECTION = "transcript_retention"
TRANSCRIPT_DAYS = (1.0, 3650.0)
TRANSCRIPT_DEFAULT_DAYS = 365.0
COST_GB_MONTH = 0.023                 # S3 Standard, us-east-1, $ per GB-month (Settings shows what is kept and its cost)
FORMATS = ("wav",)
RETENTION_DAYS = (0.5, 365.0)         # min, max; the bucket's lifecycle hard max (18-audio-storage.sh) is HARD_MAX_DAYS
HARD_MAX_DAYS = 400
TG_KEY = re.compile(r"tg:(\d{1,9})")
CHANNEL_KEY = re.compile(r"[^\x00-\x1f:]{1,60}")


def tg_key(talkgroup: int) -> str:
    return f"tg:{int(talkgroup)}"


def retention_key(channel: str, talkgroup: int | None) -> str:
    return tg_key(talkgroup) if talkgroup is not None else channel


def _days(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


class Retention:
    """The settings section over default_days ([storage] audio_keep_hours / 24): days per talkgroup, per channel, the
    default. keep_days() for one clip, sql() for all of them in the database (the same precedence)."""

    def __init__(self, ov: dict | None, default_days: float):
        ov = ov or {}
        self.tg: dict[int, float] = {}
        self.ch: dict[str, float] = {}
        for k, v in ov.items():
            d = _days(v)
            if d is None or k == DEFAULT_KEY:
                continue
            m = TG_KEY.fullmatch(k)
            if m:
                self.tg[int(m.group(1))] = d
            elif CHANNEL_KEY.fullmatch(k) and not k.startswith("_"):
                self.ch[k] = d
        self.default = _days(ov.get(DEFAULT_KEY)) or float(default_days)

    def keep_days(self, channel: str, talkgroup: int | None) -> float:
        if talkgroup is not None and int(talkgroup) in self.tg:
            return self.tg[int(talkgroup)]
        if channel and channel in self.ch:
            return self.ch[channel]
        return self.default

    def shortest_days(self) -> float:
        return min([self.default, *self.tg.values(), *self.ch.values()])

    def sql(self, now: float) -> tuple[str, list]:
        """(an SQL expression over the audio table's talkgroup and channel: the time before which a clip is due,
        its parameters)."""
        if not self.tg and not self.ch:
            return "?", [now - self.default * DAY]
        parts, params = ["CASE"], []
        for t, d in self.tg.items():
            parts.append("WHEN talkgroup = ? THEN ?")
            params += [t, now - d * DAY]
        for c, d in self.ch.items():
            parts.append("WHEN channel = ? THEN ?")
            params += [c, now - d * DAY]
        parts.append("ELSE ? END")
        params.append(now - self.default * DAY)
        return " ".join(parts), params


def keep_days(ov: dict, channel: str, talkgroup: int | None, default_days: float) -> float:
    """The days a clip on this channel / talkgroup is kept (Retention.keep_days)."""
    return Retention(ov, default_days).keep_days(channel, talkgroup)


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
    delete(keys) -> the keys gone (a key that was never there counts as gone); pages(prefix) -> lists of
    (key, last modified) (the orphan sweep)."""

    name = "?"

    def put(self, key: str, body: bytes) -> str:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def delete(self, keys: list[str]) -> set[str]:
        raise NotImplementedError

    def pages(self, prefix: str) -> Iterator[list[tuple[str, float]]]:
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
            raise StoreError(f"{key}: {type(e).__name__}", missing=isinstance(e, FileNotFoundError)) from None

    def delete(self, keys: list[str]) -> set[str]:
        gone = set()
        for k in keys:
            try:
                self._path(k).unlink(missing_ok=True)
                gone.add(k)
            except OSError:
                pass
        return gone

    def pages(self, prefix: str) -> Iterator[list[tuple[str, float]]]:
        page: list[tuple[str, float]] = []
        base = self.root.resolve()
        for p in sorted((self.root / prefix).rglob("*.wav")):
            try:
                page.append((p.resolve().relative_to(base).as_posix(), p.stat().st_mtime))
            except (OSError, ValueError):
                continue
            if len(page) >= DELETE_MAX:
                yield page
                page = []
        if page:
            yield page


class S3AudioStore(AudioStore):
    """One bucket through sizeup/attachments.py's S3 (SigV4 with the task role; tests give a fake send)."""

    def __init__(self, bucket: str, region: str = "", endpoint: str = "", send: Callable | None = None,
                 creds: Callable[[], aws.Credentials] = aws.credentials):
        from sizeup.attachments import S3, _send
        base = send or _send

        def bounded(method, url, headers, body, timeout):
            return base(method, url, headers, body, min(timeout, TIMEOUTS.get(method, timeout)))

        # a 403 forgets the cached credentials only when they are the reason (an expired / invalid token): an
        # AccessDenied on one key is that key's error, not a reason to fetch the task role's credentials again
        self.s3 = S3(bucket, region, endpoint, send=bounded, creds=creds, forget=lambda data: _code(data) in TOKEN_CODES)
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
            raise StoreError(str(e), missing=e.status == 404 and e.code == "NoSuchKey",
                             config=e.status in (403, 404) and e.code != "NoSuchKey") from None

    def delete(self, keys: list[str]) -> set[str]:
        """DeleteObjects, at most DELETE_MAX keys a call (Quiet: only the failures come back). The keys gone. An answer
        that isn't a <DeleteResult> (a proxy's page, a truncated body) is a failure: nothing counts as gone."""
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
            text = (data or b"").decode(errors="replace")
            if status != 200 or not re.search(r"<DeleteResult[\s>/]", text):
                raise StoreError(f"S3 DeleteObjects: {status} {_code(data) or 'no DeleteResult'}")
            failed = {_unxml(m) for m in re.findall(r"<Error>\s*<Key>(.*?)</Key>", text, re.S)}
            gone.update(x for x in chunk if x not in failed)
        return gone

    def pages(self, prefix: str) -> Iterator[list[tuple[str, float]]]:
        """ListObjectsV2 under prefix, a page (at most 1000 keys) at a time: [(key, last modified)]."""
        token = ""
        for _ in range(100000):
            q = f"list-type=2&max-keys=1000&prefix={quote(prefix, safe='')}"
            if token:
                q = f"continuation-token={quote(token, safe='')}&" + q
            try:
                status, _, data = self.s3._call("GET", "", {}, query=q)
            except aws.AwsError as e:
                raise StoreError(str(e)) from None
            text = (data or b"").decode(errors="replace")
            if status != 200 or "<ListBucketResult" not in text:
                raise StoreError(f"S3 ListObjectsV2: {status} {_code(data)}")
            page = []
            for block in re.findall(r"<Contents>(.*?)</Contents>", text, re.S):
                k = re.search(r"<Key>(.*?)</Key>", block, re.S)
                t = re.search(r"<LastModified>(.*?)</LastModified>", block, re.S)
                if k and t:
                    page.append((_unxml(k.group(1)), datetime.fromisoformat(t.group(1).replace("Z", "+00:00")).timestamp()))
            if page:
                yield page
            m = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", text, re.S)
            if "<IsTruncated>true</IsTruncated>" not in text or not m:
                return
            token = _unxml(m.group(1))


def _code(data: bytes) -> str:
    m = re.search(rb"<Code>([^<]{1,64})</Code>", data or b"")
    return m.group(1).decode() if m else ""


def _unxml(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


class DiskCache:
    """Recent clips on the task's own disk (lost at a deploy, which is fine): at most max_bytes, the least recently
    used dropped first. Keyed by the object key. "Used" is the file's mtime (wall clock), touched on every hit, so
    the order is the same after a restart."""

    def __init__(self, root: str | Path, max_bytes: int = 256 * 1024 * 1024, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.clock = clock
        self._mu = threading.Lock()
        self._files: dict[str, tuple[float, int]] = {}     # name -> (last used: wall clock, size)
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

    def _touch(self, n: str, size: int) -> None:
        now = self.clock()
        self._files[n] = (now, size)
        try:
            os.utime(self.root / n, (now, now))
        except OSError:
            pass

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
            self._touch(n, len(b))
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
            self._touch(n, len(body))
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
