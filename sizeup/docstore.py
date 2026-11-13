"""Where pre-plan site documents' bytes live (#459, sizeup/cad/preplan_docs.py): floor plans, site plans, inspection
sheets, Tier II, contact sheets, photos. Alarm, Knox and security layouts among them: private, read only through links
that expire in minutes, never public.

    s3://<SIZEUP_PREPLAN_DOCS_BUCKET>/<prefix>/<ref>/original.<pdf|png|jpg>   what was uploaded (photos without their
                                                                           metadata: sizeup/docfiles.py)
                                                  /plan.png               a PDF floor plan's page, rendered (the overlay)

    S3DocStore   the hub: the private backup bucket (sizeup-aws 1-base.sh: Block Public Access, versioned, old versions
                 kept 30 days, SSE-S3 by default), prefix "preplan-docs": the task role already reads, writes and
                 deletes any key there (task-backups.json), so no new IAM. Every PUT with SSE-S3 and checksums
                 (sizeup/attachments.py S3). Reading: link() is a presigned GET (SigV4 query auth, the task role's
                 session) that expires in LINK_S, its answer's type, Content-Disposition and Cache-Control fixed by the
                 signature (response-content-type, response-content-disposition, response-cache-control): the browser
                 gets the bytes from S3's own origin, never the hub's, so a file can't read the hub's cookies or
                 pages. The map's overlay loads the plan with CORS (the bucket's CORS rule, sizeup-aws
                 22-preplan-docs.sh: GET from the hub's origin only). A delete removes the current version; the
                 bucket's lifecycle removes the old one 30 days later.
    FsDocStore   the same on a local folder (dev, tests: SIZEUP_PREPLAN_DOCS_DIR, else <the CAD's folder>/preplan-docs
                 off the hub). link() is None: the hub serves the bytes itself (nosniff, Content-Disposition, a
                 sandboxing CSP), behind the same sign-in.

from_env(): the bucket (SIZEUP_PREPLAN_DOCS_BUCKET, prefix SIZEUP_PREPLAN_DOCS_PREFIX), else a folder, else None (a hub
with no bucket: uploads say "not configured"; a task's disk doesn't outlive a deploy).
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import Callable

from sizeup import aws

LINK_S = 120                      # a presigned link, at most this long (s)
PREFIX = "preplan-docs"
SAFE_KEY = re.compile(r"[a-z0-9-]{1,40}(/[0-9a-f]{16}/(original\.(pdf|png|jpg)|plan\.png))")


class DocStoreError(OSError):
    pass


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

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def link(self, key: str, ctype: str, disposition: str, expires: int = LINK_S) -> str | None:
        """A URL the browser may GET for expires seconds, or None: the hub serves get() itself."""
        return None


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
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(body)
        os.chmod(tmp, 0o600)
        tmp.replace(p)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise DocStoreError("missing") from None

    def delete(self, key: str) -> None:
        p = self._path(key)
        p.unlink(missing_ok=True)
        try:
            p.parent.rmdir()
        except OSError:
            pass


class S3DocStore(DocStore):
    def __init__(self, bucket: str, prefix: str = PREFIX, region: str = "", endpoint: str = "", send: Callable | None = None,
                 creds: Callable[[], aws.Credentials] = aws.credentials):
        from sizeup.attachments import S3
        self.s3 = S3(bucket, region, endpoint, send=send, creds=creds)
        self.bucket, self.prefix, self.creds = bucket, prefix, creds
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

    def delete(self, key: str) -> None:
        try:
            status, _, _ = self.s3._call("DELETE", key, {})
        except aws.AwsError as e:
            raise DocStoreError(f"S3 DELETE: {e.code or e.status}") from None
        if status not in (200, 204, 404):
            raise DocStoreError(f"S3 DELETE: {status}")

    def link(self, key: str, ctype: str, disposition: str, expires: int = LINK_S) -> str | None:
        q = urllib.parse.urlencode([("response-cache-control", "private, no-store"),
                                    ("response-content-disposition", disposition), ("response-content-type", ctype)],
                                   quote_via=urllib.parse.quote)
        try:
            creds = self.creds()
        except aws.AwsError as e:
            raise DocStoreError(f"S3 link: {e.code}") from None
        # S3's query authentication signs no payload: UNSIGNED-PAYLOAD
        return aws.presign(f"{self.s3.url(key)}?{q}", creds, self.s3.region, "s3", min(int(expires), LINK_S),
                           payload_hash="UNSIGNED-PAYLOAD")


def from_env(log: Callable[[str], None] = print, default_dir: str | Path | None = None) -> DocStore | None:
    bucket = os.environ.get("SIZEUP_PREPLAN_DOCS_BUCKET", "").strip()
    prefix = (os.environ.get("SIZEUP_PREPLAN_DOCS_PREFIX", "").strip().strip("/") or PREFIX)
    if not re.fullmatch(r"[a-z0-9-]{1,40}", prefix):
        log("pre-plan documents: SIZEUP_PREPLAN_DOCS_PREFIX must be lowercase letters, digits and dashes: not configured")
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
