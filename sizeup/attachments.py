"""Incident attachments (#323): camera clips and radio transmissions kept with an incident for a year, the same way
for both kinds, in a private S3 bucket, every attach / view / download / export / delete audited.

    s3://<SIZEUP_ATTACHMENTS_BUCKET>/attachments/<incident id>/<attachment id>/
        camera   part-0000.ts ...   the DVR's recorded segments (sizeup/dvr.py) concatenated as they are (no re-encode),
                                    at most PART_BYTES each, a segment never split between parts
        radio    audio.wav          the transmission's clip as the hub has it (sizeup/cloud/hubstore.py AudioDb)
                 transmission.json  its transcript (and the alternative one), talker, radio ID, rig, channel, talkgroup
        both     manifest.json      what it is: kind, source, range, incident, who attached it and when, the note, every
                                    object's size and sha256; written last
    Every PUT with SSE-S3 (x-amz-server-side-encryption: AES256), Content-MD5 and x-amz-checksum-sha256 (S3 checks the
    bytes; Object Lock wants one of them); every version it makes is recorded (attachments.versions). The bucket
    (sizeup-aws 14-dvr-storage.sh): Block Public Access, versioning, Object Lock in governance mode (365 days: nothing
    can be overwritten away or deleted early), lifecycle: current objects expire after 365 days. The hub task role
    reads and writes only there. No share links, no presigned URLs: every byte goes through the hub behind Access.

The record (the CAD store, cad.sqlite, so Litestream keeps it):
    attachments        one row each: kind (camera | radio), source (camera id | channel), source_name, talkgroup, ref
                       (radio: the transmission id), start / end, bytes, objects [{name, key, bytes, sha256}],
                       segments (camera: [part, offset, bytes, start, duration, new stream] for the player), note, status (saving |
                       stored | failed | deleted | expired), who attached it and when, expires_at, deleted_at / _by
    attachment_audit   attach | view | download | export | delete | failed: when, who, which attachment and incident,
                       a detail (a delete: every object and its sha256). Never pruned: a deleted attachment keeps its
                       record and its audit trail.

Camera clips: a dispatcher or admin picks a range (at most MAX_CLIP_S) within what the DVR has, POST
/api/cad/cameras/<id>/dvr/clip; the row says "saving" at once and the upload runs in the background (at most SAVERS at
a time). Radio: every transmission linked to an incident (by hand, or by the auto-linking in sizeup/cad/comms.py) is
copied once it has settled (SETTLE_S after it ended: its transcript is in): sync_radio(), every SYNC_S. Idempotent (one
row per incident and transmission, whatever its status: a deleted one is not copied again); a copy that failed is
tried again, at most TRIES times. Once copied, the incident keeps it for a year whatever the audio DB's own retention.

Only the leader writes (may_write: the app's fence, sizeup/fence.py). Without a bucket (no SIZEUP_ATTACHMENTS_BUCKET)
nothing is copied, a save says "Incident attachments not configured" and the lists still read.

Deleting (admins only, Attachments.delete, the one place that deletes anything): every version it ever wrote is
deleted by its version id with x-amz-bypass-governance-retention: true (the task role has s3:BypassGovernanceRetention
and s3:DeleteObjectVersion for this; nothing else in sizeup sends either), so the bytes are really gone; the row stays
as "deleted" and the audit keeps what was deleted, with every object's sha256. A delete that fails part way says so
(audited "failed") and can be asked again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from sizeup import aws

RETENTION_S = 365 * 86400        # a camera clip; a radio one is kept until an admin deletes it (expires_at NEVER)
NEVER = 0.0
MAX_CLIP_S = 3600.0               # a camera clip, at most
EDGE_S = 1.0                      # a range may start / end this far past what is recorded
PART_BYTES = 8 * 1024 * 1024      # a camera clip's parts, at most (a bigger single segment is a part of its own)
SAVERS = 2                        # camera clips uploaded at once
SETTLE_S = 300.0                  # a linked transmission is copied this long after it ended (its transcript is in)
SYNC_S = 60.0
SYNC_BATCH = 50
TRIES = 5                         # a copy refused for good (a 4xx but 403, a bad transmission), at most this often
RETRY_S = (60.0, 3600.0)          # a failed copy tried again after 60 s doubling to an hour (network, 5xx, 403: forever)
SAVING_STALE_S = 1800.0           # a "saving" row this old (the task went away mid-upload): failed
VIEW_AUDIT_S = 600.0              # a view by the same person of the same attachment: audited at most this often
EXPORT_MAX_BYTES = 4 * 1024 ** 3
EXPORTS = 1                       # incident exports at once (each streams, holding one object in memory at a time)
TIMEOUT_S = 60.0
ID_RE = re.compile(r"[0-9a-f]{16}")
KINDS = ("camera", "radio")
ACTIONS = ("attach", "view", "download", "export", "delete", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
  id TEXT PRIMARY KEY,                  -- 16 hex digits
  incident_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                   -- camera | radio
  source TEXT NOT NULL,                 -- camera id | channel (FG1, Fire/EMS 15 ...)
  source_name TEXT NOT NULL DEFAULT '', -- the camera's name | the talker
  talkgroup INTEGER,
  ref TEXT NOT NULL DEFAULT '',         -- radio: the transmission (radio_traffic.id)
  start REAL NOT NULL,
  end REAL NOT NULL,
  bytes INTEGER NOT NULL DEFAULT 0,
  objects TEXT NOT NULL DEFAULT '[]',   -- [{name, key, bytes, sha256}], manifest.json last
  versions TEXT NOT NULL DEFAULT '[]',  -- every [key, S3 version id] written (a retry's too): what a delete removes
  segments TEXT NOT NULL DEFAULT '[]',  -- camera: [[part, offset, bytes, start, duration, new stream], ...]
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,                 -- saving | stored | failed | deleted | expired
  error TEXT NOT NULL DEFAULT '',
  tries INTEGER NOT NULL DEFAULT 0,     -- refused for good this often (TRIES at most)
  attempts INTEGER NOT NULL DEFAULT 0,  -- failed this often in all (the backoff)
  retry_at REAL,                        -- a failed one: not before this
  created_at REAL NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  expires_at REAL NOT NULL,
  deleted_at REAL,
  deleted_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS attachments_incident ON attachments(incident_id, start);
CREATE UNIQUE INDEX IF NOT EXISTS attachments_radio ON attachments(incident_id, ref) WHERE kind = 'radio';
CREATE TABLE IF NOT EXISTS attachment_audit (
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  attachment_id TEXT NOT NULL DEFAULT '',
  incident_id INTEGER,
  action TEXT NOT NULL,                 -- attach | view | download | export | delete | failed
  who TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS attachment_audit_attachment ON attachment_audit(attachment_id, at);
CREATE INDEX IF NOT EXISTS attachment_audit_incident ON attachment_audit(incident_id, at)
"""


class AttachError(ValueError):
    """The request can't be done (a bad range, nothing recorded, not configured): the 400's reason."""


class NotConfigured(AttachError):
    pass


NOT_CONFIGURED = "Incident attachments not configured (no bucket: SIZEUP_ATTACHMENTS_BUCKET)"


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- S3 -------------------------------------------------------------------------------------------------------------

class S3:
    """PUT / GET (a byte range) / DELETE of single objects in one bucket, signed with the task role (sizeup/aws.py, no
    boto3). send(method, url, headers, body, timeout) -> (status, headers, body): tests give a fake."""

    def __init__(self, bucket: str, region: str = "", endpoint: str = "", send: Callable | None = None,
                 creds: Callable[[], aws.Credentials] = aws.credentials, forget: Callable[[bytes], bool] | None = None):
        self.bucket, self.region = bucket, aws.region(region)
        self.base = (endpoint or f"https://{bucket}.s3.{self.region}.amazonaws.com").rstrip("/")
        self.send, self.creds = send or _send, creds
        self.forget = forget or (lambda data: True)     # a 403's body -> fetch the credentials again (sizeup/audiostore.py)

    def url(self, key: str) -> str:
        return f"{self.base}/{urllib.parse.quote(key)}"

    def _call(self, method: str, key: str, headers: dict, body: bytes = b"", query: str = "") -> tuple[int, dict, bytes]:
        sha = sha256(body)
        url = self.url(key) + (f"?{query}" if query else "")
        signed = aws.sign(method, url, {"x-amz-content-sha256": sha, **headers}, body, self.creds(), self.region, "s3",
                          payload_sha=sha)
        try:
            status, hdrs, data = self.send(method, url, {k: v for k, v in signed.items() if k != "Host"},
                                           body if method in ("PUT", "POST") else None, TIMEOUT_S)
        except OSError as e:
            raise aws.AwsError(f"S3 {method}: {getattr(e, 'reason', e)}", "Network") from None
        if status == 403 and self.forget(data or b""):
            aws.forget_credentials()
        return status, {k.lower(): v for k, v in (hdrs or {}).items()}, data or b""

    def put(self, key: str, body: bytes, ctype: str = "application/octet-stream", kind: str = "") -> str:
        """kind: the object's kind tag (x-amz-tagging kind=camera | radio): the bucket's lifecycle expires camera clips
        after a year and never a radio one (sizeup-aws 19-incident-audio-forever.sh; s3:PutObjectTagging)."""
        digest = hashlib.sha256(body).digest()
        status, h, data = self._call("PUT", key, {
            "Content-Type": ctype, "Content-Length": str(len(body)),
            "Content-MD5": base64.b64encode(hashlib.md5(body).digest()).decode(),
            "x-amz-checksum-sha256": base64.b64encode(digest).decode(),
            "x-amz-server-side-encryption": "AES256",
            **({"x-amz-tagging": f"kind={kind}"} if kind else {})}, body)
        if status != 200:
            raise aws.AwsError(f"S3 PUT {key}: {status} {_code(data)}", _code(data) or f"HTTP {status}", status)
        return h.get("x-amz-version-id", "")

    def get(self, key: str, first: int | None = None, last: int | None = None) -> bytes:
        hdrs = {"Range": f"bytes={first}-{last}"} if first is not None else {}
        status, _, data = self._call("GET", key, hdrs)
        if status not in (200, 206):
            raise aws.AwsError(f"S3 GET {key}: {status} {_code(data)}", _code(data) or f"HTTP {status}", status)
        return data

    def list_versions(self, prefix: str) -> list[tuple[str, str]]:
        """Every version and delete marker under prefix: [(key, version id)] (ListObjectVersions, s3:ListBucketVersions
        on that prefix): what an admin's delete removes, a version no PUT's answer told us of included."""
        out, marker, vmarker = [], "", ""
        for _ in range(1000):
            pairs = [("prefix", prefix), ("versions", "")]
            if marker:
                pairs += [("key-marker", marker), ("version-id-marker", vmarker)]
            q = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}" for k, v in sorted(pairs))
            status, _, data = self._call("GET", "", {}, query=q)
            if status != 200:
                raise aws.AwsError(f"S3 list versions {prefix}: {status} {_code(data)}", _code(data) or f"HTTP {status}", status)
            text = data.decode(errors="replace")
            for block in re.findall(r"<(?:Version|DeleteMarker)>(.*?)</(?:Version|DeleteMarker)>", text, re.S):
                k = re.search(r"<Key>(.*?)</Key>", block, re.S)
                v = re.search(r"<VersionId>(.*?)</VersionId>", block, re.S)
                if k and v:
                    out.append((_unxml(k.group(1)), _unxml(v.group(1))))
            if "<IsTruncated>true</IsTruncated>" not in text:
                return out
            marker = _unxml((re.search(r"<NextKeyMarker>(.*?)</NextKeyMarker>", text, re.S) or [None, ""])[1])
            vmarker = _unxml((re.search(r"<NextVersionIdMarker>(.*?)</NextVersionIdMarker>", text, re.S) or [None, ""])[1])
        return out

    def delete_version(self, key: str, version: str) -> None:
        """An admin's delete (Attachments.delete only): the version itself, past its governance lock. Without a version
        id (a bucket without versioning, the tests' dev setups): the key."""
        q = f"versionId={urllib.parse.quote(version, safe='')}" if version else ""
        hdrs = {"x-amz-bypass-governance-retention": "true"} if version else {}
        status, _, data = self._call("DELETE", key, hdrs, query=q)
        if status not in (200, 204, 404):
            raise aws.AwsError(f"S3 DELETE {key}: {status} {_code(data)}", _code(data) or f"HTTP {status}", status)


def _unxml(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


class _Sink:
    """A write-only file for zipfile (no tell, no seek: it writes data descriptors), whose bytes are taken as they come."""

    def __init__(self):
        self.parts: list[bytes] = []

    def write(self, b) -> int:
        self.parts.append(bytes(b))
        return len(b)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out, self.parts = b"".join(self.parts), []
        return out


def _transient(e: Exception) -> bool:
    """A failure worth trying again without counting it: the network, S3's 5xx, a 403 (credentials being renewed)."""
    if isinstance(e, aws.AwsError):
        return e.status == 0 or e.status == 403 or e.status >= 500 or e.code in ("Network", "NoCredentials")
    return isinstance(e, OSError)


def _code(data: bytes) -> str:
    m = re.search(rb"<Code>([^<]{1,64})</Code>", data or b"")
    return m.group(1).decode() if m else ""


def _send(method: str, url: str, headers: dict, body, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read(1 << 16)


# -- a body sent in pieces --------------------------------------------------------------------------------------------

class Stream:
    """A response body too big to hold (a clip's download, an incident's export): its length and its pieces.
    sizeup/web/server.py writes the pieces; close() runs once it is done (or the client left)."""

    def __init__(self, length: int | None, pieces: Callable[[], Iterator[bytes]],
                 on_close: Callable[[], None] | None = None):
        """length: None when it isn't known before the end (an export: sent without Content-Length, the connection
        closed after it)."""
        self.length, self._pieces, self._on_close = (None if length is None else int(length)), pieces, on_close

    def __len__(self) -> int:
        return self.length or 0

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._pieces())

    def close(self) -> None:
        if self._on_close is not None:
            fn, self._on_close = self._on_close, None
            fn()

    def read_all(self) -> bytes:
        try:
            return b"".join(self)
        finally:
            self.close()


# -- the store --------------------------------------------------------------------------------------------------------

class Attachments:
    def __init__(self, store, s3: S3 | None, log: Callable[[str], None] = print, clock: Callable[[], float] | None = None,
                 prefix: str = "attachments", may_write: Callable[[], bool] | None = None):
        self.store, self.s3, self.log = store, s3, log
        self.clock = clock or store.clock
        self.prefix = prefix.strip("/") or "attachments"
        self.may_write = may_write or (lambda: True)
        # the app hands in: the DVR (sizeup/dvr.py, or None), a camera's details, a transmission's audio
        self.dvr: Callable[[], object] = lambda: None
        self.camera: Callable[[str], dict | None] = lambda cid: None
        self.audio: Callable[[str], bytes | None] = lambda tid: None
        self._pool = ThreadPoolExecutor(max_workers=SAVERS, thread_name_prefix="attach")
        self._seen: dict[tuple[str, str], float] = {}     # (who, id) -> when a view was last audited
        self._mu = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._exports = threading.BoundedSemaphore(EXPORTS)
        self._resumed = False
        with store.lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    store.db.execute(stmt)
            cols = {r["name"] for r in store.db.execute("PRAGMA table_info(attachments)")}
            for col, decl in (("versions", "TEXT NOT NULL DEFAULT '[]'"), ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                              ("retry_at", "REAL")):
                if col not in cols:                       # a table from before these (this branch's own test runs)
                    store.db.execute(f"ALTER TABLE attachments ADD COLUMN {col} {decl}")
            # radio attachments are kept until an admin deletes them (incident audio forever): the rows from before too
            store.db.execute("UPDATE attachments SET expires_at = 0, status = CASE status WHEN 'expired' THEN 'stored' "
                             "ELSE status END WHERE kind = 'radio' AND expires_at != 0")

    @classmethod
    def from_env(cls, store, log: Callable[[str], None] = print, may_write=None) -> Attachments:
        bucket = os.environ.get("SIZEUP_ATTACHMENTS_BUCKET", "").strip()
        s3 = S3(bucket) if bucket else None
        att = cls(store, s3, log=log, prefix=os.environ.get("SIZEUP_ATTACHMENTS_PREFIX", "attachments"), may_write=may_write)
        log(f"attachments: s3://{bucket}/{att.prefix}/, camera clips {RETENTION_S // 86400} days, radio until deleted" if s3 is not None
            else f"attachments: {NOT_CONFIGURED}: nothing is copied")
        return att

    @property
    def configured(self) -> bool:
        return self.s3 is not None

    def _need(self) -> S3:
        if self.s3 is None:
            raise NotConfigured(NOT_CONFIGURED)
        if not self.may_write():
            raise AttachError("not the leader: try again in a moment")
        return self.s3

    def key(self, iid: int, aid: str, name: str) -> str:
        return f"{self.prefix}/{int(iid)}/{aid}/{name}"

    # -- the audit ------------------------------------------------------------------------------------------------

    def audit(self, action: str, aid: str, iid: int | None, who: str, detail: str = "") -> None:
        with self.store.lock:
            self.store.db.execute("INSERT INTO attachment_audit (at, attachment_id, incident_id, action, who, detail) "
                                  "VALUES (?, ?, ?, ?, ?, ?)", (self.clock(), aid, iid, action, who[:200], detail[:4000]))
        self.log(f"attachments: {action} {aid or '-'} (incident {iid})")   # (no names, no note: the server log)

    def viewed(self, row: dict, who: str, action: str = "view") -> None:
        """A view (the playlist, the audio) at most every VIEW_AUDIT_S per person and attachment; downloads always."""
        if action == "view":
            now = self.clock()
            with self._mu:
                if now - self._seen.get((who, row["id"]), -1e18) < VIEW_AUDIT_S:
                    return
                self._seen[(who, row["id"])] = now
        self.audit(action, row["id"], row["incident_id"], who)

    def audit_rows(self, iid: int | None = None, aid: str = "", limit: int = 500) -> list[dict]:
        where, args = [], []
        if iid is not None:
            where.append("incident_id = ?")
            args.append(iid)
        if aid:
            where.append("attachment_id = ?")
            args.append(aid)
        return self.store.q("SELECT * FROM attachment_audit" + (f" WHERE {' AND '.join(where)}" if where else "")
                            + " ORDER BY at DESC, id DESC LIMIT ?", (*args, limit))

    # -- rows -------------------------------------------------------------------------------------------------------

    def row(self, aid: str) -> dict | None:
        if not ID_RE.fullmatch(aid or ""):
            return None
        return self.store.one("SELECT * FROM attachments WHERE id = ?", (aid,))

    @staticmethod
    def public(r: dict) -> dict:
        objs = json.loads(r["objects"] or "[]")
        out = {k: r[k] for k in ("id", "incident_id", "kind", "source", "source_name", "talkgroup", "ref", "start", "end",
                                 "bytes", "note", "status", "error", "created_at", "created_by", "expires_at",
                                 "deleted_at", "deleted_by")}
        out["objects"] = [{k: o[k] for k in ("name", "bytes", "sha256")} for o in objs]
        out["segments"] = len(json.loads(r["segments"] or "[]"))
        base = f"/api/cad/attachments/{r['id']}"
        if r["status"] == "stored":
            if r["kind"] == "camera":
                out["play"] = f"{base}/index.m3u8"
            elif any(o["name"] == "audio.wav" for o in objs):
                out["play"] = f"{base}/audio.wav"
            out["download"] = f"{base}/download"
        return out

    def list(self, iid: int, deleted: bool = False) -> list[dict]:
        """An incident's attachments in time order (deleted and expired ones only with deleted)."""
        rows = self.store.q("SELECT * FROM attachments WHERE incident_id = ?" +
                            ("" if deleted else " AND status NOT IN ('deleted', 'expired')") + " ORDER BY start, created_at",
                            (int(iid),))
        return [self.public(r) for r in rows]

    def _incident(self, iid) -> dict:
        try:
            iid = int(iid)
        except (TypeError, ValueError):
            raise AttachError("incident_id: an event's id") from None
        inc = self.store.one("SELECT id, number, status FROM incidents WHERE id = ?", (iid,))
        if inc is None:
            raise AttachError(f"no event {iid}")
        return inc

    def _new_id(self) -> str:
        return secrets.token_hex(8)

    def _wrote(self, aid: str, key: str, version: str) -> None:
        """A PUT went through: its version, kept at once (a failed upload's too), so a delete can remove it."""
        with self.store.lock:
            r = self.store.db.execute("SELECT versions FROM attachments WHERE id = ?", (aid,)).fetchone()
            vs = json.loads(r["versions"] if r else "[]")
            vs.append([key, version])
            self.store.db.execute("UPDATE attachments SET versions = ? WHERE id = ?", (json.dumps(vs), aid))

    def _set(self, aid: str, **cols) -> None:
        with self.store.lock:
            self.store.db.execute(f"UPDATE attachments SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                                  (*cols.values(), aid))

    # -- camera clips -----------------------------------------------------------------------------------------------

    def save_camera(self, cid: str, body: dict, operator: str, wait: bool = False) -> dict:
        """POST /api/cad/cameras/<id>/dvr/clip {from, to, incident_id, note?}: the row ("saving"); the upload runs in
        the background (wait: here, for the tests)."""
        self._need()
        dvr = self.dvr()
        if dvr is None:
            raise AttachError("no camera recording here")
        cid = dvr.alias(cid)                      # an id merged away (#320/#324): the camera kept for it
        inc = self._incident(body.get("incident_id"))
        try:
            t0, t1 = float(body["from"]), float(body["to"])
        except (KeyError, TypeError, ValueError):
            raise AttachError("from, to: unix seconds") from None
        if not t1 > t0:
            raise AttachError("to must be after from")
        if t1 - t0 > MAX_CLIP_S + 1e-6:
            raise AttachError(f"a clip is at most {MAX_CLIP_S / 60:.0f} min")
        note = str(body.get("note") or "").strip()[:500]
        st = dvr.status(cid)
        if not st.get("segments"):
            raise AttachError(f"nothing recorded on camera {cid}")
        if t0 < st["from"] - EDGE_S or t1 > st["to"] + EDGE_S:
            raise AttachError(f"outside what is recorded ({iso(st['from'])} to {iso(st['to'])})")
        segs = dvr.segments(cid, t0, t1)
        if not segs:
            raise AttachError("nothing recorded in that range")
        cam = self.camera(cid) or {}
        aid, now = self._new_id(), self.clock()
        start, end = segs[0]["t0"], segs[-1]["t0"] + segs[-1]["dur"]
        with self.store.lock:
            self.store.db.execute(
                "INSERT INTO attachments (id, incident_id, kind, source, source_name, start, end, note, status, created_at, "
                "created_by, expires_at) VALUES (?, ?, 'camera', ?, ?, ?, ?, ?, 'saving', ?, ?, ?)",
                (aid, inc["id"], cid, str(cam.get("name") or "")[:120], start, end, note, now, operator, now + RETENTION_S))
        self.audit("attach", aid, inc["id"], operator,
                   f"camera {cid} {iso(start)} to {iso(end)} ({len(segs)} segments)" + (f"; note: {note}" if note else ""))
        out = self.public(self.row(aid))
        job = (aid, inc, cid, cam, segs, note, operator, now)
        dvr.hold(cid, True)                       # its folder stays put until the upload is over
        if wait:
            self._upload_camera(*job)
            return self.public(self.row(aid))
        self._pool.submit(self._upload_camera, *job)
        return out

    def _upload_camera(self, aid: str, inc: dict, cid: str, cam: dict, segs: list[dict], note: str, operator: str,
                       now: float) -> None:
        s3 = self.s3
        try:
            objects, index, part, pn, total = [], [], bytearray(), 0, 0

            def flush() -> None:
                nonlocal part, pn
                if not part:
                    return
                name = f"part-{pn:04d}.ts"
                key = self.key(inc["id"], aid, name)
                if not self.may_write():
                    raise AttachError("lost the lease mid-upload")
                self._wrote(aid, key, s3.put(key, bytes(part), "video/MP2T", "camera"))
                objects.append({"name": name, "key": key, "bytes": len(part), "sha256": sha256(part)})
                part, pn = bytearray(), pn + 1

            dvr = self.dvr()
            for s in segs:
                try:
                    b = dvr.read_segment(cid, s["key"], s["path"]) if dvr is not None else Path(s["path"]).read_bytes()
                except OSError as e:
                    raise AttachError(f"segment {s['key']} gone before it was copied ({type(e).__name__})") from None
                if part and len(part) + len(b) > PART_BYTES:
                    flush()
                index.append([pn, len(part), len(b), round(s["t0"], 3), round(s["dur"], 3), int(bool(s.get("disc")))])
                part += b
                total += len(b)
            flush()
            manifest = {"v": 1, "id": aid, "kind": "camera",
                        "incident": {"id": inc["id"], "number": inc.get("number") or ""},
                        "source": {"camera": cid, "name": cam.get("name") or "", "road": cam.get("road") or "",
                                   "lat": cam.get("lat"), "lon": cam.get("lon"), "video": cam.get("source") or ""},
                        "from": iso(segs[0]["t0"]), "to": iso(segs[-1]["t0"] + segs[-1]["dur"]),
                        "created_by": operator, "created_at": iso(now), "note": note,
                        "expires_at": iso(now + RETENTION_S),
                        "segments": [{"part": p, "offset": o, "bytes": n, "start": iso(t), "duration": d, "seq": s["key"],
                                      "discontinuity": bool(x)} for (p, o, n, t, d, x), s in zip(index, segs)],
                        "objects": [{k: o[k] for k in ("name", "bytes", "sha256")} for o in objects]}
            objects.append(self._put_manifest(inc["id"], aid, manifest))
            self._set(aid, status="stored", bytes=total, objects=json.dumps(objects), segments=json.dumps(index), error="")
            self.log(f"attachments: {aid}: camera {cid}, {len(segs)} segments, {total / 1e6:.1f} MB in {len(objects) - 1} part(s)")
        except Exception as e:  # noqa: BLE001 - the row says why; never kills the pool
            why = f"{type(e).__name__}: {e}"[:300]
            self._set(aid, status="failed", error=why)
            self.audit("failed", aid, inc["id"], operator, why)
        finally:
            dvr = self.dvr()
            if dvr is not None:
                dvr.hold(cid, False)

    def _put_manifest(self, iid: int, aid: str, manifest: dict) -> dict:
        body = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode()
        key = self.key(iid, aid, "manifest.json")
        self._wrote(aid, key, self.s3.put(key, body, "application/json", manifest.get("kind") or ""))
        return {"name": "manifest.json", "key": key, "bytes": len(body), "sha256": sha256(body)}

    # -- radio --------------------------------------------------------------------------------------------------------

    def sync_radio(self, limit: int = SYNC_BATCH) -> int:
        """Every transmission linked to an event and settled (SETTLE_S after it ended) that isn't in the store yet, or
        whose copy failed (at most TRIES): copied. The number copied."""
        if self.s3 is None or not self.may_write():
            return 0
        now = self.clock()
        todo = self.store.q(
            "SELECT l.traffic_id AS tid, l.incident_id AS iid, l.why, l.unit FROM radio_links l "
            "JOIN radio_traffic t ON t.id = l.traffic_id JOIN incidents i ON i.id = l.incident_id "
            "LEFT JOIN attachments a ON a.kind = 'radio' AND a.incident_id = l.incident_id AND a.ref = l.traffic_id "
            "WHERE t.end < ? AND t.end > ? AND (a.id IS NULL OR (a.status = 'failed' AND a.tries < ? "
            "AND COALESCE(a.retry_at, 0) <= ?)) ORDER BY t.start LIMIT ?",
            (now - SETTLE_S, now - RETENTION_S, TRIES, now, limit))
        n = 0
        for x in todo:
            if self._stop.is_set() or not self.may_write():
                break
            by = (x["unit"] or "manual") if x["why"] == "manual" else f"auto ({x['why']})"
            got = self._copy_radio(x["tid"], x["iid"], by)
            if got == "failed":
                break                                 # S3 in trouble: the rest wait for the next round
            n += got == "copied"
        return n

    def uncopied(self, ids: list[str]) -> set[str]:
        """Of these transmissions, the ones linked to an incident (any link: the ones sync_radio copies) without a
        stored copy for each of their incidents: the live audio store must not let them go yet (#414 follow-up). A
        deleted copy (an admin's decision) holds nothing."""
        out: set[str] = set()
        for k in range(0, len(ids), 500):
            chunk = list(ids[k:k + 500])
            marks = ",".join("?" * len(chunk))
            out.update(r["tid"] for r in self.store.q(
                f"SELECT l.traffic_id AS tid FROM radio_links l LEFT JOIN attachments a ON a.kind = 'radio' "
                f"AND a.incident_id = l.incident_id AND a.ref = l.traffic_id AND a.status IN ('stored', 'deleted') "
                f"WHERE l.traffic_id IN ({marks}) AND a.id IS NULL", chunk))
        return out

    def copy_radio(self, tid: str, iid: int, by: str = "") -> bool:
        """One transmission into the store for one event (idempotent: an existing row is left alone unless its copy
        failed). True when it was copied now."""
        return self._copy_radio(tid, iid, by) == "copied"

    def _copy_radio(self, tid: str, iid: int, by: str = "") -> str:
        """copied | skipped | failed. A failure is tried again after RETRY_S (doubling); one that is S3's or the
        network's (_transient) never counts against TRIES."""
        s3 = self._need()
        t = self.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        if t is None:
            return "skipped"
        inc = self._incident(iid)
        now = self.clock()
        with self.store.lock:
            old = self.store.db.execute("SELECT id, status, tries FROM attachments WHERE kind = 'radio' AND incident_id = ? "
                                        "AND ref = ?", (inc["id"], tid)).fetchone()
            if old is not None and not (old["status"] == "failed" and old["tries"] < TRIES):
                return "skipped"
            if old is None:
                aid = self._new_id()
                self.store.db.execute(
                    "INSERT INTO attachments (id, incident_id, kind, source, source_name, talkgroup, ref, start, end, status, "
                    "created_at, created_by, expires_at) VALUES (?, ?, 'radio', ?, ?, ?, ?, ?, ?, 'saving', ?, ?, ?)",
                    (aid, inc["id"], t["channel"], t["talker"] or t["rig"] or t["radio_id"] or "", t["talkgroup"], tid,
                     t["start"], t["end"], now, by, NEVER))
            else:
                aid = old["id"]
                self.store.db.execute("UPDATE attachments SET status = 'saving' WHERE id = ?", (aid,))
        try:
            meta = {k: t.get(k) for k in ("id", "call_id", "turn", "start", "end", "channel", "talkgroup", "talker",
                                           "radio_id", "rig", "dispatcher", "text", "status", "model", "alt_text",
                                           "alt_model", "emergency", "id_note", "mhz", "tones")}
            meta["start_iso"], meta["end_iso"] = iso(t["start"]), iso(t["end"])
            objects = []
            wav = self.audio(tid)
            if wav:
                key = self.key(inc["id"], aid, "audio.wav")
                self._wrote(aid, key, s3.put(key, wav, "audio/wav", "radio"))
                objects.append({"name": "audio.wav", "key": key, "bytes": len(wav), "sha256": sha256(wav)})
            body = (json.dumps(meta, indent=1, sort_keys=True, default=str) + "\n").encode()
            key = self.key(inc["id"], aid, "transmission.json")
            self._wrote(aid, key, s3.put(key, body, "application/json", "radio"))
            objects.append({"name": "transmission.json", "key": key, "bytes": len(body), "sha256": sha256(body)})
            manifest = {"v": 1, "id": aid, "kind": "radio", "incident": {"id": inc["id"], "number": inc.get("number") or ""},
                        "source": {"transmission": tid, "channel": t["channel"], "talkgroup": t["talkgroup"],
                                   "talker": t["talker"], "radio_id": t["radio_id"], "rig": t["rig"]},
                        "from": iso(t["start"]), "to": iso(t["end"]), "created_by": by, "created_at": iso(now),
                        "note": "", "expires_at": None, "audio": bool(wav),
                        "objects": [{k: o[k] for k in ("name", "bytes", "sha256")} for o in objects]}
            objects.append(self._put_manifest(inc["id"], aid, manifest))
            self._set(aid, status="stored", bytes=sum(o["bytes"] for o in objects[:-1]), objects=json.dumps(objects),
                      error="" if wav else "no audio kept for it: the transcript only")
        except Exception as e:  # noqa: BLE001
            why = f"{type(e).__name__}: {e}"[:300]
            with self.store.lock:
                r = self.store.db.execute("SELECT attempts FROM attachments WHERE id = ?", (aid,)).fetchone()
                n = (r["attempts"] if r else 0) + 1
                wait = min(RETRY_S[1], RETRY_S[0] * 2 ** (n - 1))
                self.store.db.execute("UPDATE attachments SET status = 'failed', error = ?, attempts = ?, retry_at = ?, "
                                      "tries = tries + ? WHERE id = ?", (why, n, now + wait, 0 if _transient(e) else 1, aid))
            self.audit("failed", aid, inc["id"], by, why)
            return "failed"
        self.audit("attach", aid, inc["id"], by, f"radio {tid} on {t['channel']} at {iso(t['start'])}"
                   + ("" if wav else " (no audio)"))
        return "copied"

    # -- housekeeping -----------------------------------------------------------------------------------------------

    def resume(self) -> int:
        """The leader before this one went away mid-upload (a deploy: its queue went with it): every camera clip still
        "saving", or failed as interrupted, uploaded again under the same id, when its range is still recorded (else
        failed: "no longer recorded"). Once, after the DVR has read its index. The number queued."""
        dvr = self.dvr()
        if self.s3 is None or not self.may_write() or dvr is None or not getattr(dvr, "scanned", lambda: True)():
            return 0
        self._resumed = True
        rows = self.store.q("SELECT * FROM attachments WHERE kind = 'camera' AND (status = 'saving' OR "
                            "(status = 'failed' AND error LIKE 'interrupted%')) ORDER BY created_at")
        n = 0
        for r in rows:
            cid = dvr.alias(r["source"]) if hasattr(dvr, "alias") else r["source"]
            segs = dvr.segments(cid, r["start"], r["end"])
            whole = bool(segs) and segs[0]["t0"] <= r["start"] + 0.01 and segs[-1]["t0"] + segs[-1]["dur"] >= r["end"] - 0.01
            if not whole:
                self._set(r["id"], status="failed", error="interrupted, and no longer recorded")
                self.audit("failed", r["id"], r["incident_id"], r["created_by"], "interrupted; the range is no longer recorded")
                continue
            inc = self._incident(r["incident_id"])
            self._set(r["id"], status="saving", error="")
            dvr.hold(cid, True)
            self._pool.submit(self._upload_camera, r["id"], inc, cid, self.camera(cid) or {}, segs, r["note"],
                              r["created_by"], r["created_at"])
            self.log(f"attachments: {r['id']}: an interrupted camera clip uploaded again")
            n += 1
        return n

    def sweep(self) -> None:
        """A "saving" row left by a task that went away: failed (a radio one is tried again; a camera one, by resume() on
        the next leader); a camera clip past a year: expired (S3's lifecycle removes the objects). A radio one never
        expires (NEVER, #414 follow-up): an incident keeps its audio until an admin deletes it."""
        now = self.clock()
        with self.store.lock:
            self.store.db.execute("UPDATE attachments SET status = 'failed', error = 'interrupted (a deploy?)', "
                                  "tries = tries + 1 WHERE status = 'saving' AND created_at < ?", (now - SAVING_STALE_S,))
            self.store.db.execute("UPDATE attachments SET status = 'expired' WHERE status = 'stored' AND kind != 'radio' "
                                  "AND expires_at > 0 AND expires_at < ?", (now,))

    def start(self) -> Attachments:
        if self.s3 is not None and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="attachments", daemon=True)
            self._thread.start()
        return self

    def _first_round(self) -> None:
        """At start: the interrupted uploads (once the DVR has read its index), before the first SYNC_S."""
        for _ in range(120):
            if self._stop.wait(1.0) or self._resumed:
                return
            try:
                self.resume()
            except Exception as e:  # pragma: no cover
                self.log(f"attachments: resume: {type(e).__name__}: {e}")
                return

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self) -> None:
        self._first_round()
        while not self._stop.wait(SYNC_S):
            if not self.may_write():
                continue
            try:
                if not self._resumed:
                    self.resume()
                self.sweep()
                n = self.sync_radio()
                if n:
                    self.log(f"attachments: {n} linked transmissions copied")
            except Exception as e:  # pragma: no cover - the next round tries again
                self.log(f"attachments: {type(e).__name__}: {e}")

    # -- delete -------------------------------------------------------------------------------------------------------

    def delete(self, aid: str, operator: str) -> dict:
        """Admins only (sizeup/cad/api.py): every version written for it deleted past its governance lock (the only
        caller of S3.delete_version), the row kept as "deleted", the audit with every object's sha256. A delete that
        fails part way: audited, the row as it was (asked again, it deletes what is left)."""
        s3 = self._need()
        r = self.row(aid)
        if r is None:
            raise AttachError(f"no attachment {aid}")
        if r["status"] in ("deleted", "expired"):
            raise AttachError(f"attachment {aid} is {r['status']} already")
        if r["status"] == "saving":
            raise AttachError(f"attachment {aid} is still being saved: delete it once it is stored or failed")
        objs = json.loads(r["objects"] or "[]")
        versions = [tuple(v) for v in json.loads(r["versions"] or "[]")]
        listed = "listed"
        try:                                          # a PUT that went through but whose answer never came: listed here
            versions += s3.list_versions(f"{self.prefix}/{int(r['incident_id'])}/{aid}/")
        except aws.AwsError as e:
            listed = f"not listed ({e.code or e.status})"
            self.log(f"attachments: {aid}: versions {listed}: the recorded ones only")
        versions += [(o["key"], "") for o in objs if not any(k == o["key"] for k, _ in versions)]
        left = list(dict.fromkeys(versions))
        n_versions = len(left)
        try:
            while left:
                s3.delete_version(*left[0])
                left.pop(0)
        except aws.AwsError as e:
            with self.store.lock:
                self.store.db.execute("UPDATE attachments SET versions = ? WHERE id = ?",
                                      (json.dumps([list(v) for v in left]), aid))
            self.audit("failed", aid, r["incident_id"], operator, f"delete: {e}; {len(left)} versions left")
            raise AttachError(f"not deleted: {e}") from None
        self._set(aid, status="deleted", deleted_at=self.clock(), deleted_by=operator, versions="[]")
        r["_versions"] = f"{n_versions} versions ({listed})"
        self.audit("delete", aid, r["incident_id"], operator,
                   f"{r['kind']} {r['source']} {iso(r['start'])} to {iso(r['end'])}; {r['_versions']}; "
                   + "; ".join(f"{o['name']} {o['bytes']} B sha256 {o['sha256']}" for o in objs))
        return self.public(self.row(aid))

    # -- playback and download ------------------------------------------------------------------------------------

    def _stored(self, aid: str) -> dict:
        r = self.row(aid)
        if r is None or r["status"] != "stored":
            raise KeyError(aid)
        if self.s3 is None:
            raise NotConfigured(NOT_CONFIGURED)
        return r

    def playlist(self, aid: str) -> bytes:
        r = self._stored(aid)
        if r["kind"] != "camera":
            raise KeyError(aid)
        segs = json.loads(r["segments"])
        target = max(1, int(max(s[4] for s in segs) + 0.999))
        out = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-PLAYLIST-TYPE:VOD",
               "#EXT-X-MEDIA-SEQUENCE:0"]
        prev = None
        for n, seg in enumerate(segs):
            t0, dur = seg[3], seg[4]
            new = len(seg) > 5 and bool(seg[5])                # the DVR's new-stream mark (#324)
            if prev is not None and (abs(t0 - prev) > 1.5 or new):
                out.append("#EXT-X-DISCONTINUITY")
            out += [f"#EXT-X-PROGRAM-DATE-TIME:{iso(t0)}", f"#EXTINF:{dur:.3f},", f"{n}.ts"]
            prev = t0 + dur
        out.append("#EXT-X-ENDLIST")
        return ("\n".join(out) + "\n").encode()

    def segment(self, aid: str, n: int) -> bytes:
        r = self._stored(aid)
        segs = json.loads(r["segments"])
        if r["kind"] != "camera" or not 0 <= n < len(segs):
            raise KeyError(aid)
        part, off, size = segs[n][:3]
        key = json.loads(r["objects"])[part]["key"]
        return self.s3.get(key, off, off + size - 1)

    def audio_wav(self, aid: str) -> bytes:
        r = self._stored(aid)
        o = next((o for o in json.loads(r["objects"]) if o["name"] == "audio.wav"), None)
        if o is None:
            raise KeyError(aid)
        return self.s3.get(o["key"])

    def download(self, aid: str) -> tuple[Stream, str, str]:
        """(the body, its file name, its type): a camera clip as one .ts (its parts in order), a radio one as its .wav
        (no audio kept: its transmission.json)."""
        r = self._stored(aid)
        objs = json.loads(r["objects"])
        stamp = datetime.fromtimestamp(r["start"], timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", r["source"])[:40]
        if r["kind"] == "camera":
            parts = [o for o in objs if o["name"].endswith(".ts")]
            return (Stream(sum(o["bytes"] for o in parts), lambda: (self.s3.get(o["key"]) for o in parts)),
                    f"{stamp}_{safe}_{aid}.ts", "video/MP2T")
        o = next((o for o in objs if o["name"] == "audio.wav"), None) or next(o for o in objs if o["name"] == "transmission.json")
        ext = "wav" if o["name"] == "audio.wav" else "json"
        return (Stream(o["bytes"], lambda: iter([self.s3.get(o["key"])])), f"{stamp}_{safe}_{aid}.{ext}",
                "audio/wav" if ext == "wav" else "application/json")

    def export(self, iid: int, who: str) -> tuple[Stream, str]:
        """An incident's attachments as one zip (stored, not compressed), streamed as it is made (nothing on the disk;
        the first bytes go at once, well inside Cloudflare's 100 s): each attachment's objects as they are kept under
        <start>_<kind>_<id>/, then manifest.json: the incident, who exported it and when, and every file with its size
        and sha256, each checked against the sha256 recorded when it was attached ("verified"). EXPORTS at a time (else
        "try again shortly"). Audited when it ends (or is cut short)."""
        if self.s3 is None:
            raise NotConfigured(NOT_CONFIGURED)
        inc = self._incident(iid)
        rows = self.store.q("SELECT * FROM attachments WHERE incident_id = ? AND status = 'stored' ORDER BY start, created_at",
                            (inc["id"],))
        total = sum(o["bytes"] for r in rows for o in json.loads(r["objects"]))
        if total > EXPORT_MAX_BYTES:
            raise AttachError(f"too big to export at once ({total / 1024 ** 3:.1f} GB)")
        if not self._exports.acquire(blocking=False):
            raise AttachError("another export is running: try again shortly")
        state = {"files": [], "verified": True, "done": False, "released": False}

        def pieces() -> Iterator[bytes]:
            sink = _Sink()
            z = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
            for r in rows:
                folder = f"{datetime.fromtimestamp(r['start'], timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{r['kind']}_{r['id']}"
                for o in json.loads(r["objects"]):
                    body = self.s3.get(o["key"])
                    got = sha256(body)
                    ok = got == o["sha256"]
                    state["verified"] &= ok
                    z.writestr(f"{folder}/{o['name']}", body)
                    state["files"].append({"path": f"{folder}/{o['name']}", "attachment": r["id"], "kind": r["kind"],
                                           "bytes": len(body), "sha256": got, "recorded_sha256": o["sha256"], "verified": ok})
                    del body
                    yield sink.take()
            manifest = {"v": 1, "incident": {"id": inc["id"], "number": inc.get("number") or ""},
                        "exported_by": who, "exported_at": iso(self.clock()), "verified": state["verified"],
                        "attachments": [self.public(r) for r in rows], "files": state["files"]}
            z.writestr("manifest.json", json.dumps(manifest, indent=1, sort_keys=True) + "\n")
            z.close()
            state["done"] = True
            yield sink.take()

        def done() -> None:
            if not state["released"]:
                state["released"] = True
                self._exports.release()
                self.audit("export", "", inc["id"], who, f"{len(rows)} attachments, {len(state['files'])} files, "
                           + (("verified" if state["verified"] else "A SHA256 MISMATCH") if state["done"] else "cut short"))
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", inc.get("number") or f"event-{inc['id']}")
        return Stream(None, pieces, done), f"{name}_attachments.zip"
