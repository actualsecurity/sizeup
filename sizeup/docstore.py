"""Where pre-plan site documents' bytes live (#459, sizeup/cad/preplan_docs.py): floor plans, site plans, inspection
sheets, Tier II, contact sheets, photos. Alarm, Knox and security layouts among them: private, and read only through the
hub, by someone signed in. There are no links to the store itself (no presigned URLs, no public objects).

    <prefix>/<ref>/original.<pdf|png|jpg>   what was uploaded, as stored (sizeup/docfiles.py: a PDF saved again without
                                            its actions or metadata, a picture without its metadata)
                  /plan.png                 a plan's picture (the map's overlay)

    S3DocStore   the hub: the private backup bucket (sizeup-aws 1-base.sh: Block Public Access, versioned, old versions
                 kept 30 days, SSE-S3 by default), prefix "preplan-docs": the task role already reads, writes and
                 deletes there (task-backups.json; nothing here lists the bucket), so no new IAM, no CORS. Every
                 PUT with SSE-S3 and checksums (sizeup/attachments.py S3). open() is a signed GET whose answer is read in CHUNK pieces as the hub
                 sends them on (READ_S on each read), never the whole file in memory. A delete removes the current
                 version; the bucket's lifecycle removes the old one 30 days later.
    FsDocStore   the same on a local folder (dev, tests: SIZEUP_PREPLAN_DOCS_DIR, else <the CAD's folder>/preplan-docs off
                 the hub), files 0600.

Both are served the same way (sizeup/cad/preplan_docs.py file(), sizeup/cad/api.py): the hub streams open()'s pieces
with the document's type, Content-Disposition (site-doc-<id>.<ext>), nosniff, no-store, no-referrer and a CSP that runs
nothing.

from_env(): the bucket (SIZEUP_PREPLAN_DOCS_BUCKET, prefix SIZEUP_PREPLAN_DOCS_PREFIX), else a folder, else None (a hub
with no bucket: uploads say "not configured"; a task's disk doesn't outlive a deploy).
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from sizeup import aws

PREFIX = "preplan-docs"
PREFIX_OK = re.compile(r"preplan-docs(-[a-z0-9]{1,20})?")     # the bucket's other prefixes are never ours to write or delete
SAFE_KEY = re.compile(r"[a-z0-9-]{1,40}(/[0-9a-f]{16}/(original\.(pdf|png|jpg)|plan\.png))")
CHUNK = 64 * 1024                 # a streamed document's pieces
READ_S = 15.0                     # S3: the connection, and each read of the answer


class DocStoreError(OSError):
    pass


class Opened:
    """A stored object being read: its length and its pieces; close() once done (or the client left)."""

    def __init__(self, length: int, pieces, close: Callable[[], None]):
        self.length, self.pieces, self._close = int(length), pieces, close

    def close(self) -> None:
        fn, self._close = self._close, None
        if fn is not None:
            fn()


class DocStore:
    name = ""
    prefix = PREFIX

    def key(self, ref: str, name: str) -> str:
        k = f"{self.prefix}/{ref}/{name}"
        if not SAFE_KEY.fullmatch(k):
            raise ValueError("bad document key")
        return k

    def put(self, key: str, body: bytes, ctype: str) -> None:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def open(self, key: str) -> Opened:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class FsDocStore(DocStore):
    def __init__(self, root: str | Path, prefix: str = PREFIX):
        self.root = Path(root)
        self.prefix = prefix
        self.name = f"folder {self.root}"

    def _path(self, key: str) -> Path:
        if not SAFE_KEY.fullmatch(key):
            raise DocStoreError("bad document key")
        return self.root / key

    def put(self, key: str, body: bytes, ctype: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_suffix(p.suffix + ".part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # never readable by others, not even briefly
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        tmp.replace(p)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise DocStoreError("missing") from None

    def open(self, key: str) -> Opened:
        try:
            f = open(self._path(key), "rb")
        except FileNotFoundError:
            raise DocStoreError("missing") from None
        size = os.fstat(f.fileno()).st_size

        def pieces():
            while b := f.read(CHUNK):
                yield b
        return Opened(size, pieces, f.close)

    def delete(self, key: str) -> None:
        p = self._path(key)
        p.unlink(missing_ok=True)
        try:
            p.parent.rmdir()
        except OSError:
            pass


def _urlopen(url: str, headers: dict, timeout: float):
    """(status, headers, a readable answer): the default opener (tests give their own)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers.items()), r
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e


class S3DocStore(DocStore):
    def __init__(self, bucket: str, prefix: str = PREFIX, region: str = "", endpoint: str = "", send: Callable | None = None,
                 creds: Callable[[], aws.Credentials] = aws.credentials, opener: Callable | None = None):
        from sizeup.attachments import S3
        self.s3 = S3(bucket, region, endpoint, send=send, creds=creds)
        self.bucket, self.prefix, self.creds = bucket, prefix, creds
        self.opener = opener or _urlopen
        self.name = f"s3://{bucket}/{prefix}/"

    def put(self, key: str, body: bytes, ctype: str) -> None:
        try:
            self.s3.put(key, body, ctype)
        except aws.AwsError as e:
            raise DocStoreError(f"S3 PUT: {e.code or e.status}") from None

    def get(self, key: str) -> bytes:
        try:
            return self.s3.get(key)
        except aws.AwsError as e:
            raise DocStoreError(f"S3 GET: {e.code or e.status}") from None

    def open(self, key: str) -> Opened:
        """A signed GET (headers, never a link), its answer read CHUNK at a time as it is sent on."""
        url = self.s3.url(key)
        empty = hashlib.sha256(b"").hexdigest()
        try:
            signed = aws.sign("GET", url, {"x-amz-content-sha256": empty}, b"", self.creds(), self.s3.region, "s3",
                              payload_sha=empty)
            status, hdrs, r = self.opener(url, {k: v for k, v in signed.items() if k != "Host"}, READ_S)
        except Exception as e:                   # the network, AWS, a malformed answer (http.client.HTTPException ...)
            raise DocStoreError(f"S3 GET: {getattr(e, 'code', '') or type(e).__name__}") from None
        if status != 200:
            r.close()
            raise DocStoreError(f"S3 GET: {status}")
        length = int({k.lower(): v for k, v in hdrs.items()}.get("content-length") or 0)

        def pieces():
            try:
                while b := r.read(CHUNK):
                    yield b
            except Exception as e:               # cut short mid-way: the server's send loop ends, close() still runs
                raise DocStoreError(f"S3 GET: {type(e).__name__}") from None
        return Opened(length, pieces, r.close)

    def delete(self, key: str) -> None:
        try:
            status, _, _ = self.s3._call("DELETE", key, {})
        except aws.AwsError as e:
            raise DocStoreError(f"S3 DELETE: {e.code or e.status}") from None
        if status not in (200, 204, 404):
            raise DocStoreError(f"S3 DELETE: {status}")


def from_env(log: Callable[[str], None] = print, default_dir: str | Path | None = None) -> DocStore | None:
    bucket = os.environ.get("SIZEUP_PREPLAN_DOCS_BUCKET", "").strip()
    prefix = (os.environ.get("SIZEUP_PREPLAN_DOCS_PREFIX", "").strip().strip("/") or PREFIX)
    taken = {os.environ.get("LITESTREAM_PATH", "").strip().strip("/"), "sizeup-hub", "replay-fixtures", "cache"}
    if not PREFIX_OK.fullmatch(prefix) or prefix in taken:
        log("pre-plan documents: SIZEUP_PREPLAN_DOCS_PREFIX must be preplan-docs or preplan-docs-<name> (never the "
            "database's or another prefix of the bucket): not configured")
        return None
    if bucket:
        st: DocStore = S3DocStore(bucket, prefix)
    elif os.environ.get("SIZEUP_PREPLAN_DOCS_DIR", "").strip():
        st = FsDocStore(os.environ["SIZEUP_PREPLAN_DOCS_DIR"].strip(), prefix)
    elif default_dir is not None:
        st = FsDocStore(default_dir, prefix)
    else:
        log("pre-plan documents: not configured (SIZEUP_PREPLAN_DOCS_BUCKET)")
        return None
    log(f"pre-plan documents: {st.name}")
    return st
