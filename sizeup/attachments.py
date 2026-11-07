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
    bytes; Object Lock wants one of them). The bucket (sizeup-aws 14-dvr-storage.sh): Block Public Access, versioning,
    Object Lock in governance mode (365 days), lifecycle: current objects expire after 365 days. The hub task role may
    read and write only there. No share links, no presigned URLs: every byte goes through the hub behind Access.

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

Deleting (admins only) removes the objects with a plain DELETE: with Object Lock that leaves the locked versions hidden
behind a delete marker until their retention ends (a person with s3:BypassGovernanceRetention can remove them sooner);
the row stays as "deleted" and the audit keeps what was deleted, with every object's sha256.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
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

RETENTION_S = 365 * 86400
MAX_CLIP_S = 3600.0               # a camera clip, at most
EDGE_S = 1.0                      # a range may start / end this far past what is recorded
PART_BYTES = 8 * 1024 * 1024      # a camera clip's parts, at most (a bigger single segment is a part of its own)
SAVERS = 2                        # camera clips uploaded at once
SETTLE_S = 300.0                  # a linked transmission is copied this long after it ended (its transcript is in)
SYNC_S = 60.0
SYNC_BATCH = 50
TRIES = 5
SAVING_STALE_S = 1800.0           # a "saving" row this old (the task went away mid-upload): failed
VIEW_AUDIT_S = 600.0              # a view by the same person of the same attachment: audited at most this often
EXPORT_MAX_BYTES = 4 * 1024 ** 3
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
  segments TEXT NOT NULL DEFAULT '[]',  -- camera: [[part, offset, bytes, start, duration, new stream], ...]
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,                 -- saving | stored | failed | deleted | expired
  error TEXT NOT NULL DEFAULT '',
  tries INTEGER NOT NULL DEFAULT 0,
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
                 creds: Callable[[], aws.Credentials] = aws.credentials):
        self.bucket, self.region = bucket, aws.region(region)
        self.base = (endpoint or f"https://{bucket}.s3.{self.region}.amazonaws.com").rstrip("/")
        self.send, self.creds = send or _send, creds

    def url(self, key: str) -> str:
        return f"{self.base}/{urllib.parse.quote(key)}"

    def _call(self, method: str, key: str, headers: dict, body: bytes = b"") -> tuple[int, dict, bytes]:
        sha = sha256(body)
        url = self.url(key)
        signed = aws.sign(method, url, {"x-amz-content-sha256": sha, **headers}, body, self.creds(), self.region, "s3",
                          payload_sha=sha)
        try:
            status, hdrs, data = self.send(method, url, {k: v for k, v in signed.items() if k != "Host"},
                                           body if method == "PUT" else None, TIMEOUT_S)
        except OSError as e:
            raise aws.AwsError(f"S3 {method}: {getattr(e, 'reason', e)}", "Network") from None
        if status == 403:
            aws.forget_credentials()
        return status, {k.lower(): v for k, v in (hdrs or {}).items()}, data or b""

    def put(self, key: str, body: bytes, ctype: str = "application/octet-stream") -> str:
        digest = hashlib.sha256(body).digest()
        status, h, data = self._call("PUT", key, {
            "Content-Type": ctype, "Content-Length": str(len(body)),
            "Content-MD5": base64.b64encode(hashlib.md5(body).digest()).decode(),
            "x-amz-checksum-sha256": base64.b64encode(digest).decode(),
            "x-amz-server-side-encryption": "AES256"}, body)
        if status != 200:
            raise aws.AwsError(f"S3 PUT {key}: {status} {_code(data)}", _code(data) or f"HTTP {status}", status)
        return h.get("etag", "")

    def get(self, key: str, first: int | None = None, last: int | None = None) -> bytes:
        hdrs = {"Range": f"bytes={first}-{last}"} if first is not None else {}
        status, _, data = self._call("GET", key, hdrs)
        if status not in (200, 206):
            raise aws.AwsError(f"S3 GET {key}: {status} {_code(data)}", _code(data) or f"HTTP {status}", status)
        return data

    def delete(self, key: str) -> None:
        status, _, data = self._call("DELETE", key, {})
        if status not in (200, 204, 404):
            raise aws.AwsError(f"S3 DELETE {key}: {status} {_code(data)}", _code(data) or f"HTTP {status}", status)


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

    def __init__(self, length: int, pieces: Callable[[], Iterator[bytes]], on_close: Callable[[], None] | None = None):
        self.length, self._pieces, self._on_close = int(length), pieces, on_close

    def __len__(self) -> int:
        return self.length

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
        with store.lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    store.db.execute(stmt)

    @classmethod
    def from_env(cls, store, log: Callable[[str], None] = print, may_write=None) -> Attachments:
        bucket = os.environ.get("SIZEUP_ATTACHMENTS_BUCKET", "").strip()
        s3 = S3(bucket) if bucket else None
        att = cls(store, s3, log=log, prefix=os.environ.get("SIZEUP_ATTACHMENTS_PREFIX", "attachments"), may_write=may_write)
        log(f"attachments: s3://{bucket}/{att.prefix}/, kept {RETENTION_S // 86400} days" if s3 is not None
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
        self.log(f"attachments: {action} {aid or '-'} (incident {iid}) by {who or '-'}" + (f": {detail[:200]}" if detail else ""))

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
                s3.put(key, bytes(part), "video/MP2T")
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
        self.s3.put(key, body, "application/json")
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
            "WHERE t.end < ? AND t.end > ? AND (a.id IS NULL OR (a.status = 'failed' AND a.tries < ?)) "
            "ORDER BY t.start LIMIT ?", (now - SETTLE_S, now - RETENTION_S, TRIES, limit))
        n = 0
        for x in todo:
            if self._stop.is_set() or not self.may_write():
                break
            by = (x["unit"] or "manual") if x["why"] == "manual" else f"auto ({x['why']})"
            if self.copy_radio(x["tid"], x["iid"], by):
                n += 1
        return n

    def copy_radio(self, tid: str, iid: int, by: str = "") -> bool:
        """One transmission into the store for one event (idempotent: an existing row is left alone unless its copy
        failed). True when it was copied now."""
        s3 = self._need()
        t = self.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        if t is None:
            return False
        inc = self._incident(iid)
        now = self.clock()
        with self.store.lock:
            old = self.store.db.execute("SELECT id, status, tries FROM attachments WHERE kind = 'radio' AND incident_id = ? "
                                        "AND ref = ?", (inc["id"], tid)).fetchone()
            if old is not None and not (old["status"] == "failed" and old["tries"] < TRIES):
                return False
            if old is None:
                aid = self._new_id()
                self.store.db.execute(
                    "INSERT INTO attachments (id, incident_id, kind, source, source_name, talkgroup, ref, start, end, status, "
                    "created_at, created_by, expires_at) VALUES (?, ?, 'radio', ?, ?, ?, ?, ?, ?, 'saving', ?, ?, ?)",
                    (aid, inc["id"], t["channel"], t["talker"] or t["rig"] or t["radio_id"] or "", t["talkgroup"], tid,
                     t["start"], t["end"], now, by, now + RETENTION_S))
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
                s3.put(key, wav, "audio/wav")
                objects.append({"name": "audio.wav", "key": key, "bytes": len(wav), "sha256": sha256(wav)})
            body = (json.dumps(meta, indent=1, sort_keys=True, default=str) + "\n").encode()
            key = self.key(inc["id"], aid, "transmission.json")
            s3.put(key, body, "application/json")
            objects.append({"name": "transmission.json", "key": key, "bytes": len(body), "sha256": sha256(body)})
            manifest = {"v": 1, "id": aid, "kind": "radio", "incident": {"id": inc["id"], "number": inc.get("number") or ""},
                        "source": {"transmission": tid, "channel": t["channel"], "talkgroup": t["talkgroup"],
                                   "talker": t["talker"], "radio_id": t["radio_id"], "rig": t["rig"]},
                        "from": iso(t["start"]), "to": iso(t["end"]), "created_by": by, "created_at": iso(now),
                        "note": "", "expires_at": iso(now + RETENTION_S), "audio": bool(wav),
                        "objects": [{k: o[k] for k in ("name", "bytes", "sha256")} for o in objects]}
            objects.append(self._put_manifest(inc["id"], aid, manifest))
            self._set(aid, status="stored", bytes=sum(o["bytes"] for o in objects[:-1]), objects=json.dumps(objects),
                      error="" if wav else "no audio kept for it: the transcript only")
        except Exception as e:  # noqa: BLE001
            why = f"{type(e).__name__}: {e}"[:300]
            with self.store.lock:
                self.store.db.execute("UPDATE attachments SET status = 'failed', error = ?, tries = tries + 1 WHERE id = ?",
                                      (why, aid))
            self.audit("failed", aid, inc["id"], by, why)
            return False
        self.audit("attach", aid, inc["id"], by, f"radio {tid} on {t['channel']} at {iso(t['start'])}"
                   + ("" if wav else " (no audio)"))
        return True

    # -- housekeeping -----------------------------------------------------------------------------------------------

    def sweep(self) -> None:
        """A "saving" row left by a task that went away: failed (a radio one is tried again); past a year: expired
        (S3's lifecycle removes the objects)."""
        now = self.clock()
        with self.store.lock:
            self.store.db.execute("UPDATE attachments SET status = 'failed', error = 'interrupted (a deploy?)', "
                                  "tries = tries + 1 WHERE status = 'saving' AND created_at < ?", (now - SAVING_STALE_S,))
            self.store.db.execute("UPDATE attachments SET status = 'expired' WHERE status = 'stored' AND expires_at < ?", (now,))

    def start(self) -> Attachments:
        if self.s3 is not None and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="attachments", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self) -> None:
        while not self._stop.wait(SYNC_S):
            if not self.may_write():
                continue
            try:
                self.sweep()
                n = self.sync_radio()
                if n:
                    self.log(f"attachments: {n} linked transmissions copied")
            except Exception as e:  # pragma: no cover - the next round tries again
                self.log(f"attachments: {type(e).__name__}: {e}")

    # -- delete -------------------------------------------------------------------------------------------------------

    def delete(self, aid: str, operator: str) -> dict:
        """Admins (sizeup/cad/api.py): the objects deleted (S3: a delete marker over the locked versions), the row kept
        as "deleted", the audit with every object's sha256."""
        s3 = self._need()
        r = self.row(aid)
        if r is None:
            raise AttachError(f"no attachment {aid}")
        if r["status"] in ("deleted", "expired"):
            raise AttachError(f"attachment {aid} is {r['status']} already")
        objs = json.loads(r["objects"] or "[]")
        for o in objs:
            s3.delete(o["key"])
        self._set(aid, status="deleted", deleted_at=self.clock(), deleted_by=operator)
        self.audit("delete", aid, r["incident_id"], operator,
                   f"{r['kind']} {r['source']} {iso(r['start'])} to {iso(r['end'])}; "
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
        """An incident's attachments as one zip (stored, not compressed): each attachment's objects as they are kept
        under <start>_<kind>_<id>/, and manifest.json: the incident, who exported it and when, and every file with its
        size and sha256, each checked against the sha256 recorded when it was attached ("verified")."""
        if self.s3 is None:
            raise NotConfigured(NOT_CONFIGURED)
        inc = self._incident(iid)
        rows = self.store.q("SELECT * FROM attachments WHERE incident_id = ? AND status = 'stored' ORDER BY start, created_at",
                            (inc["id"],))
        total = sum(o["bytes"] for r in rows for o in json.loads(r["objects"]))
        if total > EXPORT_MAX_BYTES:
            raise AttachError(f"too big to export at once ({total / 1024 ** 3:.1f} GB)")
        fd, tmp = tempfile.mkstemp(prefix="sizeup-export-", suffix=".zip")
        os.close(fd)
        files, verified = [], True
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for r in rows:
                    folder = (f"{datetime.fromtimestamp(r['start'], timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
                              f"{r['kind']}_{r['id']}")
                    for o in json.loads(r["objects"]):
                        body = self.s3.get(o["key"])
                        got = sha256(body)
                        ok = got == o["sha256"]
                        verified &= ok
                        z.writestr(f"{folder}/{o['name']}", body)
                        files.append({"path": f"{folder}/{o['name']}", "attachment": r["id"], "kind": r["kind"],
                                      "bytes": len(body), "sha256": got, "recorded_sha256": o["sha256"], "verified": ok})
                manifest = {"v": 1, "incident": {"id": inc["id"], "number": inc.get("number") or ""},
                            "exported_by": who, "exported_at": iso(self.clock()), "verified": verified,
                            "attachments": [self.public(r) for r in rows], "files": files}
                z.writestr("manifest.json", json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        size = Path(tmp).stat().st_size

        def pieces() -> Iterator[bytes]:
            with open(tmp, "rb") as f:
                while chunk := f.read(1 << 20):
                    yield chunk
        self.audit("export", "", inc["id"], who, f"{len(rows)} attachments, {len(files)} files, {size} B, "
                   f"{'verified' if verified else 'A SHA256 MISMATCH'}")
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", inc.get("number") or f"event-{inc['id']}")
        return Stream(size, pieces, lambda: Path(tmp).unlink(missing_ok=True)), f"{name}_attachments.zip"
