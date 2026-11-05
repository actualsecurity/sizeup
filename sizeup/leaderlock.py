"""The hub's single-writer lock (GitHub #247): one object in the S3 backups bucket, changed only with S3
conditional writes, so at most one hub task is ever the leader (writes the SQLite files, runs Litestream
replicate, runs the Cloudflare connector).

    s3://<LITESTREAM_BUCKET>/<LITESTREAM_PATH>/leader.json
        {"v": 1, "state": "held" | "released", "holder": <ECS task ARN or host:pid:random>,
         "generation": <fencing counter, +1 on every take>, "expires_at": <unix s>, "renewed_at": <unix s>,
         "phase": "starting" | "serving" | "draining" | "released", "nonce": <random, this write's>,
         "positions": {"cad": <txid>, "hub": ..., "audio": ...} | null   (the replica's, on a clean release),
         "clean": bool, "build": <git sha>}

    take    PUT If-None-Match: *          (no lock object yet: the very first start)
            PUT If-Match: <its ETag>      (released, or expired: expires_at + GRACE_S in the past)
            generation = the one before + 1. Two tasks taking at once: S3 lets exactly one through (412 for
            the other).
    renew   PUT If-Match: <our ETag>      every RENEW_S, expires_at = now + TTL_S. A 412 means someone else wrote
            it: unless the object there is our own write (its nonce is one of ours: a renew whose answer was
            lost), the lease is LOST and the caller stops writing at once (sizeup/handoff.py fences).
    release PUT If-Match: <our ETag>      state "released", with the final replica positions. Never a DELETE:
            the bucket is versioned, and a delete marker would turn the next take into an If-None-Match.

Self-fencing (the holder's side): the lease counts as held only until the monotonic time the last successful
renew was SENT plus TTL_S - SAFETY_S (20 s). A taker waits until the wall-clock expires_at + GRACE_S (35 s
after that same send). The 15 s between them covers clock skew between tasks (Amazon Time Sync: well under
a second), a slow kill and an S3 request already on the wire.

S3 requests are signed with the task role (sizeup/aws.py SigV4, no boto). IAM: conditional PutObject needs
s3:PutObject (If-None-Match) and s3:PutObject + s3:GetObject (If-Match), which the task role's "backups"
policy already grants on the bucket's every key: no IAM change.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from sizeup import aws

KEY = "leader.json"
TTL_S = 30.0          # the lease, from the time a renew is sent
RENEW_S = 10.0        # renewed this often
SAFETY_S = 10.0       # the holder stops writing TTL_S - SAFETY_S after its last successful renew was sent
GRACE_S = 5.0         # a taker waits this long past expires_at
TIMEOUT_S = 5.0       # one S3 request
NONCES = 8            # our own last writes, recognised after a lost answer


class LockError(RuntimeError):
    """S3 couldn't say (network, 5xx, 409 conflict, credentials): try again; nothing is known to have changed."""


class LockLost(RuntimeError):
    """Someone else wrote the lock: we are not the leader (any more). Stop writing at once."""


def holder_id() -> str:
    """This task: its ECS task ARN (task metadata v4), else host:pid:random."""
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "").strip()
    if uri:
        try:
            with urllib.request.urlopen(uri.rstrip("/") + "/task", timeout=2.0) as r:
                arn = str(json.loads(r.read()).get("TaskARN") or "")
            if arn:
                return arn
        except (OSError, ValueError):
            pass
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"


def short(holder: str) -> str:
    """A task ARN's id (arn:aws:ecs:...:task/<cluster>/<id>), for the log."""
    return (holder or "?").rsplit("/", 1)[-1][:40]


def txid(v) -> int | None:
    """A Litestream TXID (an int, or its 16-hex-digit string) as an int; None: not one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str) and re.fullmatch(r"[0-9a-fA-F]{1,16}", v.strip()):
        return int(v.strip(), 16)
    return None


class S3Object:
    """GET / conditional PUT of one S3 key: send(method, url, headers, body, timeout) -> (status, headers, body).
    Tests give a fake with the same conditional-write semantics (tests/test_handoff.py FakeS3)."""

    def __init__(self, bucket: str, key: str, region: str = "", endpoint: str = "",
                 send: Callable | None = None, creds: Callable[[], aws.Credentials] = aws.credentials):
        self.bucket, self.key, self.region = bucket, key, aws.region(region)
        self.url = (endpoint or f"https://{bucket}.s3.{self.region}.amazonaws.com").rstrip("/") + "/" + urllib.parse.quote(key)
        self.send, self.creds = send or _send, creds

    def _call(self, method: str, headers: dict, body: bytes = b"") -> tuple[int, dict, bytes]:
        import hashlib
        sha = hashlib.sha256(body).hexdigest()
        signed = aws.sign(method, self.url, {"x-amz-content-sha256": sha, **headers}, body, self.creds(), self.region, "s3",
                          payload_sha=sha)
        try:
            status, hdrs, data = self.send(method, self.url, {k: v for k, v in signed.items() if k != "Host"},
                                           body if method == "PUT" else None, TIMEOUT_S)
        except OSError as e:
            raise LockError(f"S3 {method}: {getattr(e, 'reason', e)}") from None
        if status == 403:
            aws.forget_credentials()
        return status, {k.lower(): v for k, v in (hdrs or {}).items()}, data or b""

    def get(self) -> tuple[dict | None, str]:
        """(the lock, its ETag); (None, "") when there is no lock object."""
        status, hdrs, data = self._call("GET", {})
        if status == 404:
            return None, ""
        if status != 200:
            raise LockError(f"S3 GET {self.key}: {status} {_code(data)}")
        try:
            doc = json.loads(data)
        except ValueError:
            raise LockError(f"S3 GET {self.key}: not JSON") from None
        if not isinstance(doc, dict):
            raise LockError(f"S3 GET {self.key}: not an object")
        return doc, hdrs.get("etag", "")

    def put(self, doc: dict, if_match: str = "", if_none_match: bool = False) -> str | None:
        """Write it if the condition holds: its new ETag; None: the condition failed (412: someone else wrote it).
        LockError: unknown (network, 5xx, 409 conflict): it may or may not have been written."""
        body = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
        hdrs = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if if_none_match:
            hdrs["If-None-Match"] = "*"
        elif if_match:
            hdrs["If-Match"] = if_match
        else:
            raise ValueError("an unconditional write of the lock: never")
        status, rh, data = self._call("PUT", hdrs, body)
        if status == 412 or (status == 404 and if_match):      # 404: there is no current version to match
            return None
        if status != 200:
            raise LockError(f"S3 PUT {self.key}: {status} {_code(data)}")
        etag = rh.get("etag", "")
        if not etag:
            raise LockError(f"S3 PUT {self.key}: no ETag in the answer")
        return etag


def _code(data: bytes) -> str:
    m = re.search(rb"<Code>([^<]{1,64})</Code>", data or b"")
    return m.group(1).decode() if m else ""


def _send(method: str, url: str, headers: dict, body, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read(1 << 20)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read(1 << 16)


def is_free(doc: dict | None, now: float, grace: float = GRACE_S) -> bool:
    """Can it be taken: no lock yet, released, or held but expired (expires_at + grace in the past)."""
    if doc is None:
        return True
    if doc.get("state") == "released":
        return True
    try:
        return now > float(doc.get("expires_at") or 0) + grace
    except (TypeError, ValueError):
        return False          # unreadable expiry: treated as held (a person looks at it)


class LeaderLock:
    """One task's view of the lock. Thread-safe: the supervisor's renew thread and its main loop share it."""

    def __init__(self, obj: S3Object, holder: str, build: str = "", ttl: float = TTL_S, safety: float = SAFETY_S,
                 grace: float = GRACE_S, wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print):
        self.obj, self.holder, self.build = obj, holder, build
        self.ttl, self.safety, self.grace = ttl, safety, grace
        self.wall, self.mono, self.log = wall, mono, log
        self._lock = threading.RLock()
        self.etag = ""                 # ours, while we hold it
        self.generation = 0            # ours, while we hold it (0: not held)
        self.phase = ""
        self.valid_until = 0.0         # monotonic: we may write until then
        self._nonces: list[str] = []
        self.last_seen: dict | None = None     # the lock as last read (anyone's)
        self.last_seen_etag = ""

    # -- reading -------------------------------------------------------------------------------------------

    def read(self) -> tuple[dict | None, str]:
        doc, etag = self.obj.get()
        with self._lock:
            self.last_seen, self.last_seen_etag = doc, etag
        return doc, etag

    def held(self) -> bool:
        """We hold it and may write now (monotonic, from our own last successful renew)."""
        with self._lock:
            return self.generation > 0 and self.mono() < self.valid_until

    def _doc(self, state: str, phase: str, expires: float, extra: dict | None = None) -> tuple[dict, str]:
        nonce = secrets.token_hex(8)
        now = self.wall()
        doc = {"v": 1, "state": state, "holder": self.holder, "generation": self.generation, "phase": phase,
               "expires_at": round(expires, 3), "renewed_at": round(now, 3), "nonce": nonce, "build": self.build,
               "positions": None, "clean": False, **(extra or {})}
        return doc, nonce

    def _remember(self, nonce: str) -> None:
        self._nonces = (self._nonces + [nonce])[-NONCES:]

    # -- take / renew / release ----------------------------------------------------------------------------

    def try_take(self, phase: str = "starting") -> dict | None:
        """Take it if it is free: the record we took it over from ({"_created": True}: there was no lock object
        yet), else None (held by someone else, or they beat us to it). LockError: S3 couldn't say."""
        doc, etag = self.read()
        if self._ours(doc) and doc.get("state") == "held":     # an earlier take of ours landed, its answer lost
            with self._lock:
                self.generation, self.etag, self.phase = int(doc.get("generation") or 0), etag, phase
                self.valid_until = self.mono() + max(0.0, float(doc.get("expires_at") or 0) - self.wall()) - self.safety
            self.log(f"leader: an earlier take of ours had landed: generation {self.generation}")
            return {"_adopted": True}
        if not is_free(doc, self.wall(), self.grace):
            return None
        prev_gen = int((doc or {}).get("generation") or 0)
        with self._lock:
            sent = self.mono()
            self.generation = prev_gen + 1
            new, nonce = self._doc("held", phase, self.wall() + self.ttl)
            self._remember(nonce)
            try:
                tag = self.obj.put(new, if_match=etag) if doc is not None else self.obj.put(new, if_none_match=True)
            except LockError:
                self.generation = 0     # unknown: if it did land, it expires in TTL_S and nobody writes meanwhile
                raise
            if tag is None:
                self.generation = 0
                return None
            self.etag, self.phase, self.valid_until = tag, phase, sent + self.ttl - self.safety
        why = "created" if doc is None else ("released by " if doc.get("state") == "released" else "expired, was ") + short(doc.get("holder", ""))
        self.log(f"leader: took the lock, generation {self.generation} ({why})")
        return doc if doc is not None else {"_created": True}

    def renew(self, phase: str | None = None) -> None:
        """Extend our lease (and say our phase). LockLost: someone else has it: stop writing at once.
        LockError: unknown; held() turns False by itself when renews keep failing."""
        with self._lock:
            if self.generation <= 0:
                raise LockLost("not held")
            ph = phase or self.phase
            sent = self.mono()
            new, nonce = self._doc("held", ph, self.wall() + self.ttl)
            self._remember(nonce)
            tag = self.obj.put(new, if_match=self.etag)
            if tag is not None:
                self.etag, self.phase, self.valid_until = tag, ph, sent + self.ttl - self.safety
                return
            self.etag = self._ours_after_412()
            # an EARLIER write of ours is what is there: we may write only as long as it says
            left = float((self.last_seen or {}).get("expires_at") or 0) - self.wall()
            self.valid_until = min(self.valid_until, self.mono() + left - self.safety)

    def _ours(self, doc: dict | None) -> bool:
        return bool(doc and doc.get("holder") == self.holder and doc.get("nonce") in self._nonces)

    def _ours_after_412(self) -> str:
        """A 412 on our own ETag: either an earlier write of ours landed but its answer was lost (the object there
        carries one of our nonces: adopt its ETag; its expiry is no later than the one we just tried), or the lock
        is someone else's (LockLost). LockError when it can't be read."""
        doc, etag = self.read()
        if self._ours(doc) and doc.get("generation") == self.generation:
            self.log("leader: an earlier write of ours had landed (its answer was lost): carrying on")
            return etag
        self.generation = 0
        self.valid_until = 0.0
        who = short((doc or {}).get("holder", "")) if doc else "nobody"
        raise LockLost(f"the lock was written by someone else (now: {who}, generation {(doc or {}).get('generation')})")

    def release(self, positions: dict | None, clean: bool) -> str:
        """Hand it back with the replica's final positions: the released record's ETag (retake() needs it).
        LockLost: it wasn't ours any more. LockError: unknown (then it simply expires)."""
        with self._lock:
            if self.generation <= 0:
                raise LockLost("not held")
            new, nonce = self._doc("released", "released", self.wall(), {"positions": positions, "clean": bool(clean)})
            self._remember(nonce)
            tag = self.obj.put(new, if_match=self.etag)
            if tag is None:
                tag = self._ours_after_412()     # LockLost unless a write of ours is what is there
                if (self.last_seen or {}).get("state") != "released":
                    tag = self.obj.put(new, if_match=tag)     # it was an earlier renew of ours: release on top of it
                    if tag is None:
                        tag = self._ours_after_412()
            gen = self.generation
            self.generation, self.valid_until, self.phase = 0, 0.0, "released"
            self.etag = tag
        self.log(f"leader: released the lock, generation {gen}"
                 + (f", positions {positions}" if positions else ", no positions (unclean)"))
        return tag

    def retake(self, released_etag: str) -> bool:
        """Take back a lock we released that nobody took (its ETag is still the one we released with)."""
        with self._lock:
            doc, etag = self.read()
            if not doc or etag != released_etag or doc.get("holder") != self.holder or doc.get("state") != "released":
                return False
            sent = self.mono()
            self.generation = int(doc.get("generation") or 0) + 1
            new, nonce = self._doc("held", "serving", self.wall() + self.ttl)
            self._remember(nonce)
            try:
                tag = self.obj.put(new, if_match=etag)
            except LockError:
                self.generation = 0
                raise
            if tag is None:
                self.generation = 0
                return False
            self.etag, self.phase, self.valid_until = tag, "serving", sent + self.ttl - self.safety
        self.log(f"leader: nobody took the lock: took it back, generation {self.generation}")
        return True

    def drop(self) -> None:
        """Forget we held it (fenced)."""
        with self._lock:
            self.generation, self.valid_until, self.etag = 0, 0.0, ""


def from_env(log: Callable[[str], None] = print) -> LeaderLock | None:
    """The hub's: LITESTREAM_BUCKET / LITESTREAM_PATH / LITESTREAM_REGION (deploy/aws/taskdef.json)."""
    bucket = os.environ.get("LITESTREAM_BUCKET", "").strip()
    if not bucket:
        return None
    prefix = (os.environ.get("LITESTREAM_PATH", "").strip() or "sizeup-hub").strip("/")
    obj = S3Object(bucket, f"{prefix}/{KEY}", os.environ.get("LITESTREAM_REGION", "").strip())
    return LeaderLock(obj, holder_id(), os.environ.get("SIZEUP_BUILD", ""), log=log)
