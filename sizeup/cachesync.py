"""The map caches kept across hub restarts (GitHub #229): a copy of each in the S3 backups bucket.

Litestream backs up the hub's SQLite databases (deploy/hub/entrypoint.sh); the map layers' caches are plain
files beside them in the state dir, and a new Fargate task (a deploy, a retirement) started with none: every
county GIS layer, the camera lists, Metro-North's stations and the FAA registry fetched again, the map missing
them for minutes. This keeps a gzip copy of each in the same bucket, under the same prefix:

    s3://<LITESTREAM_BUCKET>/<LITESTREAM_PATH>/cache/<path in the state dir>.gz
        gis/<key>.geojson         a county GIS layer and its meta (sizeup/gis.py)
        gis/checks.json           when each layer was last checked, and how (unchanged / incremental / full)
        roads511.json             the camera lists, incidents and signs of each highway provider (sizeup/roads.py)
        roadprojects.json         road projects & closures (sizeup/roadprojects.py)
        trains/static.json        Metro-North's stations and line (sizeup/trains.py)
        trains/points.json        the train reporting points learned (sizeup/trains.py)
        trains/traintime_routes.json   Metro-North route lines from MTA TrainTime (sizeup/traintime.py, #362)
        aircraft/faa_registry.sqlite   the FAA registry as built (sizeup/aircraft.py)
        roadgraph.json            the road graph for the rigs' routes (sizeup/roadgraph.py)

    restore()   on start, before the providers read their caches (sizeup/app.py): every copy whose file is missing
                here (a new task's disk) is fetched and unpacked, a few at once, for at most RESTORE_S in all; what
                isn't back by then is fetched from its source as before. Never raises.
    start()     a background thread looks at those files every SCAN_S and uploads one that changed (its sha256 is
                not the one last uploaded or restored), at most once per its gap (the files saved every few minutes:
                once an hour). A failed upload is logged and tried again later (BACKOFF_S); it never stops anything.
                Only what changed is sent (#251: every copy went up again a minute after each start): the sha256
                of each file as last uploaded or restored is kept in the state dir (STATE_FILE), and a file this
                process knows nothing about is compared with S3's copy first: the gzip copy is made the same way
                every time (mtime 0), so its MD5 is the ETag S3 listed for it (a plain single-part ETag only); the
                same, it isn't sent again.

Credentials: the ECS task role (sizeup/aws.py credentials(); no keys anywhere). Its "backups" policy
(sizeup-aws/2-iam.sh) already allows s3:GetObject / PutObject on the bucket's every key and s3:ListBucket on the
bucket, so this needs no IAM change and no new AWS resource. Off unless LITESTREAM_BUCKET is set (Fargate only;
compose and a Mac have none), or with SIZEUP_CACHE_S3=off: then the caches are local only, as before.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from sizeup import aws

# what is kept: (the path in the state dir, the least time between two uploads of it)
FILES: tuple[tuple[re.Pattern, float], ...] = (
    (re.compile(r"gis/[a-z][a-z0-9_]{0,31}\.geojson"), 0.0),
    (re.compile(r"gis/checks\.json"), 600.0),
    (re.compile(r"roads511\.json"), 3600.0),           # saved on every events read: the camera lists are what matter
    (re.compile(r"roadprojects\.json"), 3600.0),
    (re.compile(r"trains/static\.json"), 0.0),
    (re.compile(r"trains/points\.json"), 3600.0),
    (re.compile(r"trains/traintime_routes\.json"), 0.0),
    (re.compile(r"aircraft/faa_registry\.sqlite"), 0.0),
    (re.compile(r"roadgraph\.json"), 0.0),               # the road graph (sizeup/roadgraph.py, #360): rebuilt monthly
)
DIRS = ("", "gis", "trains", "aircraft")    # the folders of the state dir those are in
SUB = "cache"                       # <prefix>/cache/...
STATE_FILE = "cachesync.json"       # in the state dir: {rel: {sha, up}} as last uploaded or restored (not itself kept)
PLAIN_ETAG = re.compile(r'"?([0-9a-f]{32})"?')     # a single-part upload's ETag: the MD5 of the object
MANIFEST = ".cachesync-restored.json"  # in the state dir: what the hub's standby restored (sizeup/handoff.py)
SCAN_S = 60.0
RESTORE_S = 45.0                    # restore() at most this long in all (the health check's start period is 60 s)
LIST_TIMEOUT_S = 10.0
WORKERS = 6
TIMEOUT_S = 60.0
BACKOFF_S = (300.0, 6 * 3600.0)     # a failed upload: 5 min doubling to 6 h
MAX_BYTES = 256 * 1024 * 1024       # a copy bigger than this is neither sent nor fetched
CHUNK = 1024 * 1024
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def gap_for(rel: str) -> float | None:
    """The least time between two uploads of this file; None: not one we keep."""
    for rx, gap in FILES:
        if rx.fullmatch(rel):
            return gap
    return None


def _send(method: str, url: str, headers: dict, body, timeout: float) -> tuple[int, bytes]:
    """One HTTPS request (urllib): (status, body). body: bytes or an open binary file (with Content-Length)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read(64 * 1024)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


class CacheMirror:
    def __init__(self, state_dir: str | Path, bucket: str, prefix: str = "sizeup-hub", region: str = "",
                 send: Callable[[str, str, dict, object, float], tuple[int, bytes]] | None = None,
                 creds: Callable[[], aws.Credentials] = aws.credentials, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, endpoint: str = "", fence: Callable[[], bool] | None = None):
        self.root = Path(state_dir)
        self.fence = fence                    # the hub's single-writer lock (sizeup/fence.py): False = never upload
        self.bucket, self.region = bucket, aws.region(region)
        self.prefix = "/".join(p for p in (prefix.strip("/"), SUB) if p)
        self.endpoint = (endpoint or f"https://{bucket}.s3.{self.region}.amazonaws.com").rstrip("/")
        self.send, self.creds, self.log, self.clock = send or _send, creds, log, clock
        self._lock = threading.Lock()
        self._save_mu = threading.RLock()     # one write of cachesync.json / the manifest at a time (#264)
        self.files: dict[str, dict] = {}      # rel -> {sha, up (when uploaded), fails, retry, error, restored}
        self.remote: dict[str, str] = {}      # rel -> the MD5 S3 has for its copy (a plain ETag), from the listing
        self.skipped = 0                      # uploads not made: S3 had that copy already
        self._load_state()
        self.status = {"restored": 0, "restore_s": None, "uploaded": 0, "error": "", "at": None}
        self._stop = threading.Event()
        self._closed = threading.Event()      # restore() is over (its deadline passed)
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls, state_dir, log: Callable[[str], None] = print, fence: Callable[[], bool] | None = None) -> CacheMirror | None:
        """The hub's: LITESTREAM_BUCKET / LITESTREAM_PATH / LITESTREAM_REGION (deploy/aws/taskdef.json), else None."""
        bucket = os.environ.get("LITESTREAM_BUCKET", "").strip()
        if not bucket or not state_dir or os.environ.get("SIZEUP_CACHE_S3", "").strip().lower() in ("0", "off", "no", "false"):
            return None
        return cls(state_dir, bucket, os.environ.get("LITESTREAM_PATH", "").strip() or "sizeup-hub",
                   os.environ.get("LITESTREAM_REGION", "").strip(), log=log, fence=fence)

    # -- S3 ---------------------------------------------------------------------------------------------------

    def key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}.gz"

    def _url(self, key: str = "", query: str = "") -> str:
        return f"{self.endpoint}/{urllib.parse.quote(key)}" + (f"?{query}" if query else "")

    def _call(self, method: str, url: str, body=b"", sha: str = aws.EMPTY_SHA256, headers: dict | None = None,
              timeout: float = TIMEOUT_S) -> bytes:
        hdrs = {"x-amz-content-sha256": sha, **(headers or {})}
        signed = _sign_s3(method, url, hdrs, sha, self.creds(), self.region)
        status, data = self.send(method, url, {k: v for k, v in signed.items() if k != "Host"}, body if method == "PUT" else None, timeout)
        if status == 403:
            aws.forget_credentials()
        if status >= 300:
            code = re.search(rb"<Code>([^<]{1,64})</Code>", data or b"")
            raise aws.AwsError(f"S3 {method} {status}" + (f" {code.group(1).decode()}" if code else ""),
                               code.group(1).decode() if code else f"HTTP {status}", status)
        return data

    def listing(self) -> dict[str, int]:
        """Every copy in the bucket: rel -> its size."""
        out, token = {}, ""
        for _ in range(20):
            q = [("list-type", "2"), ("prefix", self.prefix + "/")] + ([("continuation-token", token)] if token else [])
            doc = ET.fromstring(self._call("GET", self._url("", urllib.parse.urlencode(q)), timeout=LIST_TIMEOUT_S))
            for c in doc.iter(f"{NS}Contents"):
                k, size = c.findtext(f"{NS}Key") or "", int(c.findtext(f"{NS}Size") or 0)
                rel = k[len(self.prefix) + 1:-3] if k.startswith(self.prefix + "/") and k.endswith(".gz") else ""
                if rel and gap_for(rel) is not None:
                    out[rel] = size
                    m = PLAIN_ETAG.fullmatch((c.findtext(f"{NS}ETag") or "").strip())
                    if m:
                        self.remote[rel] = m.group(1)
            token = doc.findtext(f"{NS}NextContinuationToken") or ""
            if (doc.findtext(f"{NS}IsTruncated") or "").lower() != "true" or not token:
                break
        return out

    # -- restore ----------------------------------------------------------------------------------------------

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST

    def save_manifest(self) -> None:
        """What restore() put in place and its sha256: the hub's standby (sizeup/handoff.py) restores the caches
        before the app runs; the app's own mirror then knows them (load_manifest) and doesn't upload them again."""
        with self._save_mu:
            with self._lock:
                done = {rel: st["sha"] for rel, st in self.files.items() if st.get("restored") and st.get("sha")}
            try:
                self._write_atomic(self.manifest, json.dumps(done, sort_keys=True))
            except OSError as e:
                self.log(f"cachesync: manifest not written ({e})")

    def load_manifest(self) -> int:
        try:
            d = json.loads(self.manifest.read_text())
        except (OSError, ValueError):
            return 0
        n = 0
        for rel, sha in (d.items() if isinstance(d, dict) else ()):
            if gap_for(rel) is not None and isinstance(sha, str) and (self.root / rel).is_file():
                with self._lock:
                    self.files[rel] = {"sha": sha, "up": self.clock(), "fails": 0, "retry": 0.0, "error": "", "restored": True}
                n += 1
        return n

    def restore(self, deadline_s: float = RESTORE_S) -> int:
        """Every copy whose file is missing here, fetched and unpacked (a few at once, deadline_s in all). The
        number restored. Never raises: what isn't back is fetched from its source as before."""
        t0 = time.monotonic()
        known = self.load_manifest()
        if known:
            self.log(f"cachesync: {known} cache copies already restored before the app started")
        try:
            have = self.listing()
        except Exception as e:  # no credentials, no network, no permission: local only
            self._note_error(f"cache copies not listed ({type(e).__name__}: {e}); every cache fetched from its source")
            return 0
        todo = [rel for rel, size in sorted(have.items()) if 0 < size <= MAX_BYTES and not (self.root / rel).exists()]
        if not todo:
            self.log(f"cachesync: {len(have)} cache copies in S3, none needed here")
            return 0
        done: list[str] = []
        ex = ThreadPoolExecutor(WORKERS, thread_name_prefix="cache-restore")
        futs = {ex.submit(self._restore_one, rel): rel for rel in todo}
        finished, late = wait(futs, timeout=max(1.0, deadline_s - (time.monotonic() - t0)))
        self._closed.set()                               # one still on its way puts nothing in place now
        ex.shutdown(wait=False, cancel_futures=True)
        for f in finished:
            rel = futs[f]
            try:
                if f.result():
                    done.append(rel)
            except Exception as e:
                self.log(f"cachesync: {rel} not restored ({type(e).__name__}: {e})")
        took = time.monotonic() - t0
        self.status.update(restored=len(done), restore_s=round(took, 1))
        self.log(f"cachesync: {len(done)} of {len(todo)} cache copies restored from S3 in {took:.1f} s"
                 + (f"; {len(late)} not back in time (fetched from their sources)" if late else ""))
        return len(done)

    def _restore_one(self, rel: str) -> bool:
        dest = self.root / rel
        data = self._call("GET", self._url(self.key(rel)))
        if len(data) > MAX_BYTES:
            raise ValueError("too big")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".restore", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(gzip.decompress(data))         # checks the gzip CRC: a torn copy is never put in place
            if dest.exists() or self._closed.is_set():   # the provider wrote its own, or the providers have read theirs
                return False
            os.replace(tmp, dest)
        finally:
            Path(tmp).unlink(missing_ok=True)
        with self._lock:
            self.files[rel] = {"sha": _sha256(dest), "up": self.clock(), "fails": 0, "retry": 0.0, "error": "", "restored": True}
        self._save_state()
        return True

    # -- what was sent, kept across restarts ---------------------------------------------------------------------

    def _state_path(self) -> Path:
        return self.root / STATE_FILE

    def _load_state(self) -> None:
        try:
            doc = json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            return
        for rel, d in (doc.get("files") or {}).items() if isinstance(doc, dict) else ():
            if gap_for(rel) is not None and isinstance(d, dict) and isinstance(d.get("sha"), str):
                up = d.get("up") if isinstance(d.get("up"), (int, float)) else None
                self.files[rel] = {"sha": d["sha"], "up": up, "fails": 0, "retry": 0.0, "error": "", "restored": False}

    def _save_state(self) -> None:
        with self._save_mu:               # the snapshot and its write together: an older one never lands last
            with self._lock:
                doc = {"files": {rel: {"sha": st["sha"], "up": st["up"]} for rel, st in self.files.items() if st.get("sha")}}
            try:
                self._write_atomic(self._state_path(), json.dumps(doc, separators=(",", ":"), sort_keys=True))
            except OSError as e:  # only costs an upload after the next restart
                self.log(f"cachesync: {STATE_FILE} not saved ({type(e).__name__}: {e})")

    def _write_atomic(self, dest: Path, text: str) -> None:
        """dest replaced at once by text. The restore workers, the upload thread and the hub's supervisor (another
        process, the same folder) all save: one at a time here, and each through a temp file of its own (#264: two
        saves sharing "cachesync.tmp" raced, the second os.replace finding it gone)."""
        with self._save_mu:
            fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                os.replace(tmp, dest)
            finally:
                Path(tmp).unlink(missing_ok=True)

    # -- upload -----------------------------------------------------------------------------------------------

    def candidates(self) -> list[str]:
        """The files we keep that are in the state dir now (only the folders they live in are looked at)."""
        out = []
        for d in DIRS:
            folder = self.root / d if d else self.root
            try:
                kids = sorted(folder.iterdir()) if folder.is_dir() else []
            except OSError:
                continue
            for p in kids:
                rel = f"{d}/{p.name}" if d else p.name
                if gap_for(rel) is not None and p.is_file():
                    out.append(rel)
        return out

    def scan(self) -> int:
        """Upload what changed since it was last uploaded (or restored). The number uploaded. Never raises."""
        n, now = 0, self.clock()
        if self.fence is not None and not self.fence():
            return 0                              # not the hub's leader (any more): nothing goes to S3
        for rel in self.candidates():
            if self._stop.is_set():
                break
            st = self.files.setdefault(rel, {"sha": "", "up": None, "fails": 0, "retry": 0.0, "error": "", "restored": False})
            gap = gap_for(rel) or 0.0
            if now < st["retry"] or (st["up"] is not None and now - st["up"] < gap):
                continue
            try:
                sha = _sha256(self.root / rel)
                if sha == st["sha"]:
                    continue
                if not self._upload(rel, sha):           # S3 had it already: nothing sent
                    st.update(sha=sha, up=now, fails=0, retry=0.0, error="")
                    self.skipped += 1
                    self._save_state()
                    continue
            except FileNotFoundError:
                continue                                  # replaced or gone between the look and the read
            except Exception as e:  # never fatal: logged, tried again later
                st["fails"] += 1
                wait_s = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(st["fails"] - 1, 30))
                why = f"{type(e).__name__}: {e}"[:200]
                if why != st["error"]:
                    self.log(f"cachesync: {rel} not uploaded ({why}); again in {wait_s / 60:.0f} min")
                st.update(error=why, retry=now + wait_s)
                self.status["error"] = f"{rel}: {why}"
                continue
            st.update(sha=sha, up=now, fails=0, retry=0.0, error="")
            self.status.update(uploaded=self.status["uploaded"] + 1, at=now, error="")
            self._save_state()
            n += 1
        return n

    def _upload(self, rel: str, sha_raw: str) -> bool:
        """The file's gzip copy to S3; False when S3's copy is that one already (its ETag is our MD5): not sent."""
        src = self.root / rel
        with tempfile.TemporaryFile() as gz:
            h, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
            with open(src, "rb") as f, gzip.GzipFile(fileobj=_Hashing(gz, h, md5), mode="wb", compresslevel=6, mtime=0) as z:
                shutil.copyfileobj(f, z, CHUNK)
            size = gz.tell()
            if self.remote.get(rel) == md5.hexdigest():
                return False
            if size > MAX_BYTES:
                raise ValueError(f"{size // (1024 * 1024)} MB: too big to keep a copy")
            gz.seek(0)
            if self.fence is not None and not self.fence():
                raise RuntimeError("not the hub's leader: not uploaded")
            self._call("PUT", self._url(self.key(rel)), gz, h.hexdigest(),
                       {"Content-Type": "application/gzip", "Content-Length": str(size), "x-amz-meta-sha256": sha_raw},
                       timeout=max(TIMEOUT_S, size / 200_000))
        self.remote[rel] = md5.hexdigest()
        self.log(f"cachesync: {rel} kept in S3 ({size / 1e6:.2f} MB gzip)")
        return True

    # -- the thread -------------------------------------------------------------------------------------------

    def start(self) -> CacheMirror:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="cache-mirror", daemon=True)
            self._thread.start()
            self.log(f"cachesync: map caches kept in s3://{self.bucket}/{self.prefix}/ (every {SCAN_S:.0f} s)")
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(SCAN_S):
            try:
                self.scan()
            except Exception as e:  # pragma: no cover - never let the thread die
                self.log(f"cachesync: {type(e).__name__}: {e}")

    def _note_error(self, why: str) -> None:
        self.status["error"] = why[:300]
        self.log(f"cachesync: {why}")


class _Hashing:
    """A write-only file that hashes what goes through it (the gzip bytes' sha256, which S3 checks)."""

    def __init__(self, f, *hashes):
        self.f, self.hashes = f, hashes

    def write(self, b) -> int:
        for h in self.hashes:
            h.update(b)
        return self.f.write(b)

    def flush(self) -> None:
        self.f.flush()


def _sign_s3(method: str, url: str, headers: dict, payload_sha: str, creds: aws.Credentials, reg: str) -> dict:
    """aws.sign() with the payload's hash given (x-amz-content-sha256), not computed from the body: a streamed
    upload's hash is known before it is sent, and S3 checks the body against it."""
    return aws.sign(method, url, headers, b"", creds, reg, "s3", payload_sha=payload_sha)
