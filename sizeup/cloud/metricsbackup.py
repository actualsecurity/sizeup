"""Copies of the hub's metrics.sqlite in the backups bucket (#605; sizeup/cloud/metricstore.py decides when): the store
isn't in Litestream, so a new task (a deploy) gets its history back from here.

    s3://<LITESTREAM_BUCKET>/<LITESTREAM_PATH>/metrics/copy-<0..SLOTS-1>.sqlite.gz

SLOTS copies, written round-robin: a new copy goes to an empty slot, else over the OLDEST one (LastModified), so the
last SLOTS copies are always there and one bad copy can never take the history with it. No DeleteObject is needed: the
task role's "backups" policy (Get / Put on the bucket's keys, ListBucket; sizeup-aws 2-iam.sh, as sizeup/cachesync.py
uses) covers all of it. Off unless LITESTREAM_BUCKET is set (Fargate), or with SIZEUP_METRICS_S3=off.

list() raises on any failure: only a listing that succeeded may say "there are no copies" (the store then starts
without one); a failed one is tried again later, and nothing is uploaded until it is settled.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import sqlite3
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from sizeup import aws
from sizeup.cachesync import _send, _sign_s3

SLOTS = 8
SUB = "metrics"
KEY = re.compile(r"copy-(\d)\.sqlite\.gz")
TIMEOUT_S = 120.0
LIST_TIMEOUT_S = 15.0
MAX_BYTES = 512 * 1024 * 1024
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
SQLITE_HEADER = b"SQLite format 3\x00"


class S3Copies:
    def __init__(self, bucket: str, prefix: str = "sizeup-hub", region: str = "",
                 send: Callable[[str, str, dict, object, float], tuple[int, bytes]] | None = None,
                 creds: Callable[[], aws.Credentials] = aws.credentials, endpoint: str = "", sub: str = SUB):
        self.bucket, self.region = bucket, aws.region(region)
        self.prefix = "/".join(p for p in (prefix.strip("/"), sub) if p)
        self.endpoint = (endpoint or f"https://{bucket}.s3.{self.region}.amazonaws.com").rstrip("/")
        self.send, self.creds = send or _send, creds
        self.name = f"s3://{bucket}/{self.prefix}/"

    @classmethod
    def from_env(cls, sub: str = SUB) -> S3Copies | None:
        """sub: the folder ("metrics"; "stt": the transcripts as written, sizeup/sttruns.py, #639)."""
        bucket = os.environ.get("LITESTREAM_BUCKET", "").strip()
        if not bucket or os.environ.get("SIZEUP_METRICS_S3", "").strip().lower() in ("0", "off", "no", "false"):
            return None
        return cls(bucket, os.environ.get("LITESTREAM_PATH", "").strip() or "sizeup-hub",
                   os.environ.get("LITESTREAM_REGION", "").strip(), sub=sub)

    def key(self, slot: int) -> str:
        return f"{self.prefix}/copy-{int(slot)}.sqlite.gz"

    def _call(self, method: str, url: str, body: bytes = b"", timeout: float = TIMEOUT_S, headers: dict | None = None) -> bytes:
        sha = hashlib.sha256(body).hexdigest()
        signed = _sign_s3(method, url, {"x-amz-content-sha256": sha, **(headers or {})}, sha, self.creds(), self.region)
        status, data = self.send(method, url, {k: v for k, v in signed.items() if k != "Host"},
                                 body if method == "PUT" else None, timeout)
        if status == 403:
            aws.forget_credentials()
        if status >= 300:
            code = re.search(rb"<Code>([^<]{1,64})</Code>", data or b"")
            raise aws.AwsError(f"S3 {method} {status}" + (f" {code.group(1).decode()}" if code else ""),
                               code.group(1).decode() if code else f"HTTP {status}", status)
        return data

    def list(self) -> list[dict]:
        """The copies there are: [{slot, key, at (LastModified, epoch s), size}], newest first. Raises when the
        listing fails (never "none" for an answer it didn't get)."""
        q = urllib.parse.urlencode([("list-type", "2"), ("prefix", self.prefix + "/")])
        doc = ET.fromstring(self._call("GET", f"{self.endpoint}/?{q}", timeout=LIST_TIMEOUT_S))
        out = []
        for c in doc.iter(f"{NS}Contents"):
            k = c.findtext(f"{NS}Key") or ""
            m = KEY.fullmatch(k[len(self.prefix) + 1:]) if k.startswith(self.prefix + "/") else None
            if not m or int(m.group(1)) >= SLOTS:
                continue
            try:
                at = datetime.fromisoformat((c.findtext(f"{NS}LastModified") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                at = 0.0
            out.append({"slot": int(m.group(1)), "key": k, "at": at, "size": int(c.findtext(f"{NS}Size") or 0)})
        return sorted(out, key=lambda c: c["at"], reverse=True)

    def get(self, slot: int) -> bytes:
        """The copy in that slot, unpacked (the gzip CRC checked: a torn one raises)."""
        data = self._call("GET", f"{self.endpoint}/{urllib.parse.quote(self.key(slot))}")
        if len(data) > MAX_BYTES:
            raise ValueError("too big")
        return gzip.decompress(data)

    def put(self, slot: int, raw: bytes, level: int = 6) -> int:
        """The file's bytes, gzipped (level: 1 quick ... 9 small), into that slot. The bytes sent."""
        gz = gzip.compress(raw, level, mtime=0)
        self._call("PUT", f"{self.endpoint}/{urllib.parse.quote(self.key(slot))}", gz,
                   headers={"Content-Type": "application/gzip", "Content-Length": str(len(gz))})
        return len(gz)


def next_slot(copies: list[dict]) -> int:
    """Where the next copy goes: an empty slot, else the oldest copy's."""
    used = {c["slot"] for c in copies}
    free = [k for k in range(SLOTS) if k not in used]
    if free:
        return free[0]
    return min(copies, key=lambda c: c["at"])["slot"]


# -- restoring from the copies: shared by the station metrics (sizeup/cloud/metricstore.py) and the transcripts as written
# (sizeup/sttruns.py, #639) ------------------------------------------------------------------------------------------------

def common_columns(db: sqlite3.Connection, table: str) -> list[str] | None:
    """The columns of `table` both this store (main) and an attached copy (snap) have, in this store's order; None when
    the copy hasn't the table, or lacks a key column or one here that must have a value (NOT NULL, no default)."""
    def info(schema: str) -> list[tuple]:
        return db.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    mine, theirs = info("main"), {r[1] for r in info("snap")}
    if not theirs:
        return None
    for _cid, name, _type, notnull, default, pk in mine:
        if name not in theirs and (pk or (notnull and default is None)):
            return None
    return [r[1] for r in mine if r[1] in theirs]


@contextmanager
def attached(db: sqlite3.Connection, data: bytes, folder: str | None, prefix: str):
    """A copy's bytes (an SQLite file) attached to db as "snap" while the block runs (ValueError: not an SQLite file),
    in a temporary file in `folder` (None: the system's), gone afterwards."""
    if data[:16] != SQLITE_HEADER:
        raise ValueError("not an SQLite file")
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".sqlite", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        db.execute("ATTACH DATABASE ? AS snap", (tmp,))
        try:
            yield
        finally:
            db.execute("DETACH DATABASE snap")
    finally:
        Path(tmp).unlink(missing_ok=True)


def merge_copies(copies: list[dict], get: Callable[[int], bytes], merge: Callable[[bytes], object],
                 log: Callable[[str], None], label: str) -> list:
    """The copies merged in: the newest (and the largest, if that is another one); and when neither can be read, the
    next ones, newest first, until one can. Each copy's trouble (torn: zlib.error, not SQLite, a table that doesn't fit)
    is said and the next is tried. merge(data)'s answers; RuntimeError when no copy could be read (tried again later,
    never taken for "nothing there")."""
    pick = [copies[0]] + ([c] if (c := max(copies, key=lambda c: c["size"])) is not copies[0] else [])
    out, last = [], None
    for c in pick + [c for c in copies if c not in pick]:
        if out and c not in pick:
            break                              # the newest (and largest) are in: enough
        try:
            out.append(merge(get(c["slot"])))
        except Exception as e:  # noqa: BLE001 - a torn or misfit copy: the next one is tried
            last = e
            log(f"{label}: copy {c['slot']} not read ({type(e).__name__}: {e})")
    if not out:
        raise RuntimeError(f"no copy could be read ({type(last).__name__ if last else 'none'})")
    return out
