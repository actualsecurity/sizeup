"""GPU speech-to-text workers on the hub (#639): who a worker is (its own token), the pool's switches, and the worker
API. The queue, leases and fallbacks are sizeup/sttpool.py; the routing into it is the hub's (sizeup/cloud/hub.py).

Tokens
    sw1.<id>.<secret>   a worker's own token: the worker id (lower case letters, digits, - and _, at most 40) and 32
                        random bytes. Made here, shown to the admin ONCE (Settings > Station > STT workers), never
                        stored: hub.sqlite table "stt_workers" keeps sha256(token) only, made by the first add (never at
                        the hub's start). The same scheme as the stations' (sizeup/cloud/stations.py: why SHA-256 is
                        enough for 256 random bits, the dummy hash for an unknown id). A station's token opens none
                        of this, and a worker's opens nothing else.
    Rotate              a new token; the old one works ROTATE_GRACE_S more (none after a revoke).
    Revoke              refused from its next request; what it holds falls back at once.

The pool's switches (hub.sqlite meta "stt_pool", JSON): on (default on) and, per class, whether its clips go to the
pool first (default all). With no active worker the pool is off whatever they say: nothing changes without one.

The worker API (every request: Authorization: Bearer sw1...; at the edge, a Cloudflare Access service token: best its
own Access application scoped to /api/station/stt/*, so a worker's service token reaches nothing of the station's). JSON in and out; 401 {error, reason} for a token refused (no_token, token_unknown,
token_revoked); 409 {error} for a lease that is gone.

    POST /api/station/stt/hello                   {worker?, version, model, gpu?, state} -> {ok, worker, poll_s, classes, pool_on}
    POST /api/station/stt/state                   {state: online|gaming|paused, model, gpu, version, keep_awake?}
                                                  -> {ok, pool_on}
    POST /api/station/stt/claim                   {wait <= 25, state, model} -> {job: {lease, id, kind, channel, class,
                                                  prompt, seconds, lease_s, cap_at, fast, audio} | null}; fast: one
                                                  greedy pass (temperature_inc 0), the short-lease classes
    GET  /api/station/stt/jobs/<lease>/audio      audio/wav: 16 kHz mono 16-bit, padded, as whisper-server takes it
    POST /api/station/stt/jobs/<lease>/extend     {} -> {ok, lease_s, cap_at}
    POST /api/station/stt/jobs/<lease>/complete   {text, segments: [{start, end, text, words: [{word, start, end,
                                                  probability}]}], model, timing: {fetch_ms, infer_ms}} -> {ok}
    POST /api/station/stt/jobs/<lease>/release    {why} -> {ok}: the next engine takes it now
    POST /api/station/stt/logs                    gzipped worker.log lines (see "Its log" below) -> {ok, accepted}

The heartbeat contract: while it runs, a worker sends a request (state, extend or claim) at least every 6 s, during a
decode too. One unheard for 12 s (sttpool.LOST_S) loses every lease it holds, catch-up ones included, and those clips go
to the next engine. A pre-alert job (fast) is leased 4 s, renewable by extend to 8 s from the claim; a worker that goes
gaming or paused has its pre-alert jobs taken back at once (their complete / extend then answer 409). A worker holds two
leases at most, and a pre-alert job only when it holds none.

The LLM lane (sizeup/cloud/infer.py): a worker serving a local LLM says so in every request's models {stt, llm,
llm_version} and claims JSON classification jobs at /api/station/stt/infer/* with the same token; never while it holds
a pre-alert lease, and never counted in the STT leases above.

The self-update (deploy/stt-worker/worker.py Updater): a GPU worker that sends self_update {sha256, commit, auto, state,
bad} in its hello and state (its capability: a worker without it is never told) gets worker_update {mode, commit,
sha256, url} in the answer: the version the hub wants it on. mode auto (the default): this hub's own build, SIZEUP_BUILD
(the commit on main CI built the image from) and the SHA-256 of deploy/stt-worker/worker.py in the image, read once at
the start (worker_build); pin: an admin's commit and hash; hold: none (it stays on what it runs). Settings > Station >
STT workers sets it per worker (hub.sqlite meta "stt_update"). The hub only names a version: the worker fetches it from
GitHub itself, only from main of actualsecurity/sizeup, and only a file with exactly that hash (its url is never used).
A worker from before the move to deploy/ knows only tools/stt_worker/worker.py: the copy there is byte-identical (a test
checks), so it finds the same hash at the same commit, and the version it updates to knows the new path.

Keeping the PC awake (deploy/stt-worker/worker.py KeepAwake): a GPU worker on Windows says keep_awake {on, held} in its
hello and state. Health shows "keeps PC awake" while held, and a worker gone silent as "offline since HH:MM", with
"(asleep?)" when its last word said keep_awake off or not held, or its heartbeats just stopped while online (no pause,
gaming or clean stop first: sttpool.Pool.asleep). A hub from before ignores the key.

Its log (deploy/stt-worker/worker.py LogShip): POST /api/station/stt/logs, gzipped {v, worker, batch, lines: [[ms,
level, msg]]} -> {ok, accepted, n | retry_s, reason}; a GPU worker's own token only. Into CloudWatch Logs
/sizeup/stt-worker, a stream per worker (sizeup/cloud/cwlogs.py WorkerLogs).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from sizeup import sttpool

PREFIX = "sw1"
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TOKEN = re.compile(r"sw1\.([a-z0-9][a-z0-9_-]{0,39})\.([A-Za-z0-9_-]{43})")
NAME_MAX = 60
ROTATE_GRACE_S = 600.0
TOUCH_S = 300.0
META_POOL = "stt_pool"
META_LLM = "stt_llm"            # per GPU worker: its "LLM jobs" switch (JSON {id: bool}; absent: on)
META_UPDATE = "stt_update"      # per GPU worker: its self-update (JSON {id: {mode, commit?, sha256?}}; absent: auto)
UPDATE_MODES = ("auto", "hold", "pin")
UPDATE_REPO = "actualsecurity/sizeup"
WORKER_PATH = "deploy/stt-worker/worker.py"     # in this repo (worker.py UPDATE_FILE); tools/stt_worker/worker.py is a
                                                # byte-identical copy for the workers from before the move
WORKER_FILE = Path(__file__).resolve().parents[2].joinpath(*WORKER_PATH.split("/"))   # (the image: /app/deploy/...)
COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
API = "/api/station/stt/"
LEASE_PATH = re.compile(r"/api/station/stt/jobs/([0-9a-f]{16})/(audio|extend|complete|release)")
INFER_MAX_BODY = 32 * 1024     # an LLM job's answer (its 16 KB at most, sizeup/cloud/infer.py) and its counts
MAX_BODY = 1024 * 1024                  # a result: the text and its words (a 2-minute clip's are ~100 KB)
TEXT_MAX = 20000
SEGMENTS_MAX = 1000
WORDS_MAX = 4000
POLL_S = 25.0
AUDIO_WAIT_MAX_S = 3.0          # a job's audio: the station's STT input waited for this long at most; per class far less
                                # (sttpool.SRC_WAIT_S, counted from when the job was queued: a pre-alert clip 0.75 s), then
                                # the listening clip (#678: a flat 2 s was every pre-alert job's fetch while the input came
                                # ~2 s after its event)
AUDIO_WAIT_OLD_S = 2.0          # ... the flat wait for a station whose STT lane is not woken by the events' ack (one from
                                # before #678: its input comes ~2 s after its event; the hub deploys before the stations)
STATION_PREFIX = "station:"     # a station's whisper as a pool worker: "station:<station id>" (never a token's id)
STATION_ROUTES = (API + "hello", API + "state", API + "claim")            # what a station's identity may reach here
STATION_LEASE_ROUTES = ("audio", "extend", "complete", "release")
REFUSAL_LOG_S = 3600.0
_DUMMY = hashlib.sha256(b"sizeup: no such worker").hexdigest()

SCHEMA = """CREATE TABLE IF NOT EXISTS stt_workers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL, prev_hash TEXT, prev_until REAL,
  created REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '', rotated REAL, revoked REAL, last_used REAL)"""
COLUMNS = ("id", "name", "hash", "prev_hash", "prev_until", "created", "created_by", "rotated", "revoked", "last_used")

REASONS = {
    "no_token": "no worker token (Authorization: Bearer)",
    "token_unknown": "unknown worker token",
    "token_revoked": "this worker's token was revoked (Settings > Station > STT workers: Rotate issues a new one)",
}


class Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(REASONS[reason])
        self.reason = reason


class WorkerError(ValueError):
    """An admin's request that can't be done (fixed text, safe to show)."""


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def make_token(wid: str) -> str:
    return f"{PREFIX}.{wid}.{secrets.token_urlsafe(32)}"


def _same(a: str, b: str | None) -> bool:
    return hmac.compare_digest(a.encode(), (b or _DUMMY).encode())


def worker_url(commit: str) -> str:
    return f"https://raw.githubusercontent.com/{UPDATE_REPO}/{commit}/{WORKER_PATH}"


def worker_build(build: str | None = None, path: Path = WORKER_FILE) -> dict:
    """The GPU worker version this hub's build carries: {commit, sha256, url}; {} when it can't tell (no SIZEUP_BUILD,
    as in a local run, or no worker.py in the image). The commit is the build's own: the file at it is the image's."""
    import os
    build = (os.environ.get("SIZEUP_BUILD", "") if build is None else build).strip().lower()
    if not COMMIT.fullmatch(build):
        return {}
    try:
        sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return {}
    return {"commit": build, "sha256": sha, "url": worker_url(build)}


def clean_self_update(v) -> dict | None:
    """A worker's self_update, checked and bounded: {sha256, commit, auto, state, bad}; None when it sent none (a worker
    from before the self-update: never told a version)."""
    if not isinstance(v, dict):
        return None
    sha, commit = v.get("sha256"), v.get("commit")
    bad = v.get("bad") if isinstance(v.get("bad"), list) else []
    return {"sha256": sha if isinstance(sha, str) and SHA256.fullmatch(sha) else "",
            "commit": commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else "",
            "auto": v.get("auto") if isinstance(v.get("auto"), bool) else None,
            "state": " ".join(str(v.get("state") or "").split())[:80],
            "bad": [b for b in bad[:5] if isinstance(b, str) and SHA256.fullmatch(b)]}


def clean_keep_awake(v) -> dict:
    """A GPU worker's keep_awake {on, held, why} (deploy/stt-worker/worker.py KeepAwake): the booleans, and why it isn't
    held (40 characters); {} when it sent none (a worker from before, or not on Windows)."""
    if not isinstance(v, dict):
        return {}
    out = {k: v[k] for k in ("on", "held") if isinstance(v.get(k), bool)}
    if isinstance(v.get("why"), str) and v["why"].strip():
        out["why"] = " ".join(v["why"].split())[:40]
    return out


class WorkerRegistry:
    """The hub's workers (hub.sqlite "stt_workers") and the pool's switches (meta "stt_pool"). mdb: the MonitorDb."""

    def __init__(self, mdb, clock: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.mdb, self.clock, self.log = mdb, clock, log
        self._used: dict[str, float] = {}
        self._written: dict[str, float] = {}
        self._any: tuple[float, bool] = (0.0, False)     # (read at, any active worker): read at most once a second
        self._lock = threading.Lock()
        self.listeners: list[Callable[[], None]] = []    # called after an admin changes a switch (the LLM lane)

    def _q(self, sql: str, params=()) -> list[tuple]:
        try:
            with self.mdb._lock:
                return self.mdb.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise

    def _w(self, sql: str, params=()) -> int:
        with self.mdb._lock:
            self.mdb.db.execute(SCHEMA)
            n = self.mdb.db.execute(sql, params).rowcount
        self._any = (0.0, False)
        return n

    def row(self, wid: str) -> dict | None:
        r = self._q(f"SELECT {', '.join(COLUMNS)} FROM stt_workers WHERE id = ?", (wid,))
        return dict(zip(COLUMNS, r[0])) if r else None

    def rows(self) -> list[dict]:
        return [dict(zip(COLUMNS, r)) for r in self._q(f"SELECT {', '.join(COLUMNS)} FROM stt_workers ORDER BY created, id")]

    def active(self, wid: str) -> bool:
        """This worker exists and isn't revoked."""
        r = self.row(wid)
        return r is not None and r["revoked"] is None

    def any(self) -> bool:
        """An active (not revoked) worker exists: the pool can be on at all. Cached for a second (asked per clip)."""
        now = self.clock()
        at, yes = self._any
        if now - at < 1.0 and at:
            return yes
        yes = bool(self._q("SELECT 1 FROM stt_workers WHERE revoked IS NULL LIMIT 1"))
        self._any = (now, yes)
        return yes

    # -- the pool's switches --------------------------------------------------------------------------------------

    def pool_settings(self) -> dict:
        """{on, stations, classes: {name: bool}}: the defaults (on, the stations' whisper in the pool, every class) with
        what an admin changed."""
        out = {"on": True, "stations": {}, "classes": {sttpool.class_key(c): True for c in sttpool.CLASSES}}
        try:
            saved = json.loads(self.mdb.get(META_POOL, "") or "{}")
        except ValueError:
            saved = {}
        if isinstance(saved, dict):
            if isinstance(saved.get("on"), bool):
                out["on"] = saved["on"]
            if isinstance(saved.get("stations"), dict):          # (station id -> its whisper in the pool; off by default)
                out["stations"] = {str(k): v for k, v in saved["stations"].items() if isinstance(v, bool)}
            for k, v in (saved.get("classes") or {}).items() if isinstance(saved.get("classes"), dict) else ():
                if k in out["classes"] and isinstance(v, bool):
                    out["classes"][k] = v
        return out

    def enabled(self, cls: float) -> bool:
        """Do this class's clips go to the pool first now: the pool is on, the class is on, and a worker can be there (a
        GPU worker added, or a station's whisper an admin put in the pool: #639 phase 2)."""
        s = self.pool_settings()
        if not s["on"] or not s["classes"].get(sttpool.class_key(cls), False):
            return False
        return any(s["stations"].values()) or self.any()

    def stations_on(self, sid: str | None = None) -> bool:
        """This station's whisper is a pool worker (Settings > Station > Stations, per station; off by default). sid None:
        any station's is."""
        s = self.pool_settings()
        if not s["on"]:
            return False
        return any(s["stations"].values()) if sid is None else bool(s["stations"].get(sid, False))

    def set_pool(self, on=None, classes=None, stations=None) -> tuple[dict, dict]:
        old = self.pool_settings()
        new = {"on": old["on"], "stations": dict(old["stations"]), "classes": dict(old["classes"])}
        if on is not None:
            if not isinstance(on, bool):
                raise WorkerError("on: true or false")
            new["on"] = on
        if stations is not None:
            if not isinstance(stations, dict) or not all(isinstance(k, str) and isinstance(v, bool)
                                                         for k, v in stations.items()):
                raise WorkerError("stations: {station id: true or false}")
            new["stations"] = {**old["stations"], **{k[:64]: v for k, v in stations.items()}}
        if classes is not None:
            if not isinstance(classes, dict):
                raise WorkerError("classes: an object")
            for k, v in classes.items():
                if k not in new["classes"] or not isinstance(v, bool):
                    raise WorkerError(f"classes: {', '.join(new['classes'])}, each true or false")
                new["classes"][k] = v
        self.mdb.put(META_POOL, json.dumps(new, sort_keys=True))
        self._switched()
        return old, new

    # -- each worker's "LLM jobs" switch (the infer lane, sizeup/cloud/infer.py) -------------------------------------

    def llm_settings(self) -> dict:
        """{worker id: bool}: the switches an admin changed (a worker not in it: on)."""
        try:
            saved = json.loads(self.mdb.get(META_LLM, "") or "{}")
        except ValueError:
            saved = {}
        return {str(k): v for k, v in saved.items() if isinstance(v, bool)} if isinstance(saved, dict) else {}

    def llm_on(self, wid: str) -> bool:
        """May this worker be sent prompts (its LLM jobs on; on by default)."""
        return bool(self.llm_settings().get(wid, True))

    def set_llm(self, wid, on) -> tuple[bool, bool]:
        wid = self._id(wid)
        if self.row(wid) is None:
            raise WorkerError(f"no worker {wid}")
        if not isinstance(on, bool):
            raise WorkerError("on: true or false")
        saved = self.llm_settings()
        old = saved.get(wid, True)
        saved[wid] = on
        self.mdb.put(META_LLM, json.dumps(saved, sort_keys=True))
        self._switched()
        return old, on

    # -- each GPU worker's self-update: auto (the hub's build), hold, or pin {commit, sha256} ------------------------

    def update_settings(self) -> dict:
        """{worker id: {mode, commit?, sha256?}}: what an admin set (a worker not in it: auto)."""
        try:
            saved = json.loads(self.mdb.get(META_UPDATE, "") or "{}")
        except ValueError:
            saved = {}
        out = {}
        for k, v in (saved.items() if isinstance(saved, dict) else ()):
            if isinstance(v, dict) and v.get("mode") in UPDATE_MODES:
                if v["mode"] == "pin" and not (COMMIT.fullmatch(str(v.get("commit") or ""))
                                               and SHA256.fullmatch(str(v.get("sha256") or ""))):
                    continue
                out[str(k)] = {"mode": v["mode"], **({"commit": v["commit"], "sha256": v["sha256"]}
                                                     if v["mode"] == "pin" else {})}
        return out

    def update_of(self, wid: str) -> dict:
        return self.update_settings().get(wid, {"mode": "auto"})

    def set_update(self, wid, mode, commit=None, sha256=None) -> tuple[dict, dict]:
        wid = self._id(wid)
        if self.row(wid) is None:
            raise WorkerError(f"no worker {wid}")
        if mode not in UPDATE_MODES:
            raise WorkerError("mode: auto, hold or pin")
        new: dict = {"mode": mode}
        if mode == "pin":
            commit, sha256 = str(commit or "").strip().lower(), str(sha256 or "").strip().lower()
            if not COMMIT.fullmatch(commit):
                raise WorkerError("commit: the full 40-digit commit on main")
            if not SHA256.fullmatch(sha256):
                raise WorkerError("sha256: worker.py's SHA-256 at that commit, 64 hex digits")
            new.update(commit=commit, sha256=sha256)
        saved = self.update_settings()
        old = saved.get(wid, {"mode": "auto"})
        if mode == "auto":
            saved.pop(wid, None)
        else:
            saved[wid] = new
        self.mdb.put(META_UPDATE, json.dumps(saved, sort_keys=True))
        return old, new

    def _switched(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - a listener's trouble never undoes the admin's change
                self.log(f"stt workers: a switch listener failed: {type(e).__name__}")

    # -- who a request is ------------------------------------------------------------------------------------------

    def resolve(self, bearer: str) -> str:
        """The bearer -> the worker id it proves, or Refused."""
        if not bearer:
            raise Refused("no_token")
        m = TOKEN.fullmatch(bearer)
        row = self.row(m.group(1)) if m else None
        h = digest(bearer)
        now = self.clock()
        cur = _same(h, row["hash"] if row else None)
        prev = _same(h, row["prev_hash"] if row and (row["prev_until"] or 0) > now else None)
        if not row or not (cur or prev):
            raise Refused("token_unknown")
        if row["revoked"] is not None:
            raise Refused("token_revoked")
        return row["id"]

    def touch(self, wid: str) -> None:
        from sizeup import fence as fence_mod
        now = self.clock()
        self._used[wid] = now
        if now - self._written.get(wid, 0.0) < TOUCH_S:
            return
        self._written[wid] = now
        try:
            with fence_mod.bookkeeping():
                with self.mdb._lock:
                    self.mdb.db.execute("UPDATE stt_workers SET last_used = ? WHERE id = ?", (now, wid))
        except Exception as e:  # noqa: BLE001 - bookkeeping: never in the way of the worker's request
            self.log(f"stt workers: last used not saved: {type(e).__name__}")

    def last_used(self, wid: str, row: dict | None = None) -> float | None:
        mem = self._used.get(wid)
        db = (row or self.row(wid) or {}).get("last_used")
        return max(x for x in (mem, db) if x is not None) if (mem or db) else None

    # -- an admin's ------------------------------------------------------------------------------------------------

    @staticmethod
    def _id(wid) -> str:
        wid = str(wid or "").strip()
        if not ID.fullmatch(wid):
            raise WorkerError("id: lower case letters, digits, - and _ (at most 40), e.g. \"gpu-1\"")
        return wid

    @staticmethod
    def _name(name) -> str:
        name = " ".join(str(name or "").split())
        if len(name) > NAME_MAX:
            raise WorkerError(f"name: at most {NAME_MAX} characters")
        return name

    def add(self, wid, name="", by: str = "") -> str:
        wid, name = self._id(wid), self._name(name)
        if self.row(wid) is not None:
            raise WorkerError(f"{wid} exists: Rotate issues it a new token")
        token = make_token(wid)
        try:
            self._w("INSERT INTO stt_workers (id, name, hash, created, created_by) VALUES (?, ?, ?, ?, ?)",
                    (wid, name, digest(token), self.clock(), by or ""))
        except sqlite3.IntegrityError:
            raise WorkerError(f"{wid} exists: Rotate issues it a new token") from None
        self.log(f"stt workers: {wid} added by {by or 'someone'}")
        return token

    def rotate(self, wid, by: str = "") -> str:
        wid = self._id(wid)
        row = self.row(wid)
        if row is None:
            raise WorkerError(f"no worker {wid}")
        token = make_token(wid)
        now = self.clock()
        prev = (row["hash"], now + ROTATE_GRACE_S) if row["revoked"] is None else (None, None)
        self._w("UPDATE stt_workers SET hash = ?, prev_hash = ?, prev_until = ?, rotated = ?, revoked = NULL WHERE id = ?",
                (digest(token), *prev, now, wid))
        self.log(f"stt workers: {wid} token rotated by {by or 'someone'}")
        return token

    def revoke(self, wid, by: str = "") -> None:
        wid = self._id(wid)
        row = self.row(wid)
        if row is None:
            raise WorkerError(f"no worker {wid}")
        if row["revoked"] is not None:
            raise WorkerError(f"{wid} is revoked already")
        self._w("UPDATE stt_workers SET revoked = ?, prev_hash = NULL, prev_until = NULL WHERE id = ?", (self.clock(), wid))
        self.log(f"stt workers: {wid} revoked by {by or 'someone'}")

    def rename(self, wid, name) -> tuple[str, str]:
        wid, name = self._id(wid), self._name(name)
        row = self.row(wid)
        if row is None:
            raise WorkerError(f"no worker {wid}")
        self._w("UPDATE stt_workers SET name = ? WHERE id = ?", (name, wid))
        return row["name"], name

    def panel(self, live: Callable[[str], dict] | None = None) -> dict:
        """GET /api/cad/stt-workers: each worker (never a hash), its live state, and the pool's switches."""
        now = self.clock()
        out = []
        for r in self.rows():
            out.append({"id": r["id"], "name": r["name"], "created": r["created"], "created_by": r["created_by"],
                        "rotated": r["rotated"], "revoked": r["revoked"], "last_used": self.last_used(r["id"], r),
                        "grace_until": r["prev_until"] if (r["prev_until"] or 0) > now else None,
                        "llm_on": self.llm_on(r["id"]), "update_mode": self.update_of(r["id"]),
                        **((live or (lambda _w: {}))(r["id"]))})
        return {"at": now, "workers": out, "pool": self.pool_settings(), "active": self.any(),
                "grace_s": ROTATE_GRACE_S}

    def act(self, body: dict, by: str = "", note: Callable[[str, object, object], None] | None = None,
            on_revoke: Callable[[str], None] | None = None) -> dict:
        """POST /api/cad/stt-workers {action: add {id, name?} | rename {id, name} | rotate {id} | revoke {id} |
        llm {id, on} | pool {on?, classes?}}. The answer of add / rotate carries the token: the only time it exists outside the
        worker. note(key, old, new): the Settings audit (never a token)."""
        a = str(body.get("action") or "")
        note = note or (lambda k, o, n: None)
        if a == "add":
            token = self.add(body.get("id"), body.get("name"), by)
            wid = self._id(body.get("id"))
            note(f"stt workers/{wid}", None, f"added ({self._name(body.get('name')) or 'no name'})")
            return {"ok": True, "id": wid, "token": token}
        if a == "llm":                                 # a GPU worker's "LLM jobs" (the infer lane: its prompts)
            wid = self._id(body.get("id"))
            old, new = self.set_llm(wid, body.get("on"))
            if old != new:
                note(f"stt workers/{wid}/llm jobs", "on" if old else "off", "on" if new else "off")
            return {"ok": True, "id": wid, "llm_on": new}
        if a == "update":                              # a GPU worker's self-update: auto, hold, pin {commit, sha256}
            wid = self._id(body.get("id"))
            old, new = self.set_update(wid, body.get("mode"), body.get("commit"), body.get("sha256"))
            if old != new:
                said = lambda s: s["mode"] if s["mode"] != "pin" else f"pin {s['commit'][:12]} ({s['sha256'][:12]})"
                note(f"stt workers/{wid}/update", said(old), said(new))
            return {"ok": True, "id": wid, "update_mode": new}
        if a == "rotate":
            wid = self._id(body.get("id"))
            was = self.row(wid)
            token = self.rotate(wid, by)
            revoked = was is not None and was["revoked"] is not None
            note(f"stt workers/{wid}", "revoked" if revoked else "token in use",
                 "new token" + ("" if revoked else f" (old one {int(ROTATE_GRACE_S // 60)} min more)"))
            return {"ok": True, "id": wid, "token": token, "grace_s": 0 if revoked else ROTATE_GRACE_S}
        if a == "revoke":
            wid = self._id(body.get("id"))
            self.revoke(wid, by)
            note(f"stt workers/{wid}", "active", "revoked")
            if on_revoke is not None:
                on_revoke(wid)
            return {"ok": True, "id": wid}
        if a == "rename":
            wid = self._id(body.get("id"))
            old, new = self.rename(wid, body.get("name"))
            note(f"stt workers/{wid}/name", old, new)
            return {"ok": True, "id": wid}
        if a == "pool":
            old, new = self.set_pool(body.get("on"), body.get("classes"), body.get("stations"))
            if old["on"] != new["on"]:
                note("stt workers/pool", "on" if old["on"] else "off", "on" if new["on"] else "off")
            for sid in sorted(set(old["stations"]) | set(new["stations"])):
                was, now_ = bool(old["stations"].get(sid)), bool(new["stations"].get(sid))
                if was != now_:
                    note(f"stt workers/pool/station {sid}", "on" if was else "off", "on" if now_ else "off")
            for k, v in new["classes"].items():
                if old["classes"].get(k) != v:
                    note(f"stt workers/pool/{k}", "on" if old["classes"].get(k) else "off", "on" if v else "off")
            return {"ok": True, "pool": new}
        raise WorkerError("action: add, rename, rotate, revoke, llm, update or pool")


def _json(code: int, obj) -> tuple[int, bytes, str]:
    """An answer to a worker: strict JSON (sizeup/cloud/client.py dumps: a number that isn't finite goes as null, never
    the bare NaN; an object it can't write stays a TypeError, as the hub's own answers)."""
    from sizeup.cloud.client import dumps
    return code, dumps(obj, default=None), "application/json"


def _num(v) -> float | None:
    """A finite number, else None (NaN and infinities never get in)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _prob(v) -> float | None:
    """A probability: 0 to 1, else None."""
    f = _num(v)
    return f if f is not None and 0.0 <= f <= 1.0 else None


def clean_result(raw: dict) -> dict:
    """A worker's result, checked and bounded: {text, segments, words, model, timing, language}. ValueError. language:
    whisper's detected language when the worker sends it ("" when not: sizeup/stt_quality.py judges the words alone)."""
    text = raw.get("text")
    if not isinstance(text, str):
        raise ValueError("text: a string")
    segs = raw.get("segments") if isinstance(raw.get("segments"), list) else []
    words: list[list] = []
    clean_segs = []
    for s in segs[:SEGMENTS_MAX]:
        if not isinstance(s, dict):
            continue
        clean_segs.append({"start": _num(s.get("start")), "end": _num(s.get("end")),
                           "text": str(s.get("text") or "")[:2000]})
        for w in (s.get("words") if isinstance(s.get("words"), list) else [])[:WORDS_MAX]:
            if isinstance(w, dict) and len(words) < WORDS_MAX:
                words.append([str(w.get("word") or "")[:60], _prob(w.get("probability")), _num(w.get("start")),
                              _num(w.get("end"))])
    timing = raw.get("timing") if isinstance(raw.get("timing"), dict) else {}
    return {"text": " ".join(text.split())[:TEXT_MAX], "segments": clean_segs, "words": words,
            "model": str(raw.get("model") or "")[:80],
            "language": str(raw.get("language") or "")[:16] if isinstance(raw.get("language"), str) else "",
            "timing": {k: _num(timing.get(k)) for k in ("fetch_ms", "infer_ms") if _num(timing.get(k)) is not None}}


class WorkerApi:
    """Hub.handle's /api/station/stt/ routes. may_write(): this task is the leader (a standby hands out nothing)."""

    infer = None    # the LLM lane (sizeup/cloud/infer.py Lane), set by the hub: /api/station/stt/infer/* and models
    logs = None     # the workers' logs (sizeup/cloud/cwlogs.py WorkerLogs), set by the hub: /api/station/stt/logs

    def __init__(self, registry: WorkerRegistry, pool: sttpool.Pool, may_write: Callable[[], bool] = lambda: True,
                 log: Callable[[str], None] = print, station: Callable[[str], str | None] | None = None,
                 build: dict | None = None, src_fast: Callable[[], bool] = lambda: False):
        """station(bearer) -> the station id a station's own token proves (sizeup/cloud/stations.py), None when it
        isn't one: a station's whisper is a pool worker under its station identity ("station:<id>"), no token more.
        build: the worker version this hub carries (worker_build(), read here once when not given). src_fast(): the
        station sends each STT input at once after its event (#678: its status says stt_input.wake_on_ack), so a job's
        audio waits only its class's budget (sttpool.SRC_WAIT_S); else the old flat AUDIO_WAIT_OLD_S."""
        self.reg, self.pool, self.may_write, self.log, self.station = registry, pool, may_write, log, station
        self.src_fast = src_fast
        self._said: dict[tuple[str, str], float] = {}
        self.build = worker_build() if build is None else build

    def wanted(self, wid: str) -> dict:
        """The version this GPU worker should run: worker_update in its hello and state answers."""
        s = self.reg.update_of(wid)
        if s["mode"] == "hold":
            return {"mode": "hold"}
        if s["mode"] == "pin":
            return {"mode": "pin", "commit": s["commit"], "sha256": s["sha256"], "url": worker_url(s["commit"])}
        return {"mode": "auto", **self.build}

    def _pool_on(self) -> bool:
        return bool(self.reg.pool_settings()["on"]) and (self.reg.any() or self.reg.stations_on())

    def _refuse(self, e: Refused, hint: str = "") -> tuple[int, bytes, str]:
        now = time.time()
        key = (e.reason, hint)
        if now - self._said.get(key, 0.0) >= REFUSAL_LOG_S:
            self._said[key] = now
            self.log(f"stt workers: refused a request: {e.reason}" + (f" (worker {hint})" if hint else ""))
        return _json(401, {"error": str(e), "reason": e.reason})

    def handle(self, method: str, path: str, headers, body: Callable[[int], bytes | None] | None):
        auth = str(headers.get("Authorization") or "")
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        kind = "gpu"
        try:
            wid = self.reg.resolve(bearer)
        except Refused as e:
            sid = self.station(bearer) if self.station is not None and not bearer.startswith(PREFIX + ".") else None
            if not sid:
                m = TOKEN.fullmatch(bearer)
                return self._refuse(e, m.group(1) if m and self.reg.row(m.group(1)) is not None else "")
            if not self.reg.stations_on(sid):
                return _json(403, {"error": "this station's whisper isn't in the pool (Settings > Station > Stations)",
                                   "reason": "stations_off"})
            wid, kind = f"{STATION_PREFIX}{sid}", "station"
            # a station's whisper: only the transcription routes (a route added under /api/station/stt/ is a GPU
            # worker's alone until it is listed here: never, say, a language model's jobs with their prompts)
            lm = LEASE_PATH.fullmatch(path)
            if path not in STATION_ROUTES and not (lm and lm.group(2) in STATION_LEASE_ROUTES):
                return _json(403, {"error": "not a station's route", "reason": "gpu_only"})
        if not self.may_write():
            from sizeup import fence as fence_mod
            fence_mod.refuse(f"{method} {path}")    # a standby: 503 + Retry-After (sizeup/web/server.py)
        if kind == "gpu":
            self.reg.touch(wid)
        if path == API + "logs":
            return self._logs(wid, method, headers, body)
        try:
            if method == "GET":
                m = LEASE_PATH.fullmatch(path)
                if m and m.group(2) == "audio":
                    return self._audio(wid, m.group(1))
                return _json(404, {"error": "not found"})
            raw = body(INFER_MAX_BODY if path.startswith(API + "infer/") else MAX_BODY) if body is not None else b""
            if raw is None:
                return _json(413, {"error": "too large"})
            if path.startswith(API + "infer/"):
                # (the LLM lane reads its answers strictly itself: a NaN in a model's output must make the answer
                # invalid there, never pass as null: sizeup/cloud/infer.py Lane._answer)
                d = json.loads(raw or b"{}")
            else:
                from sizeup.cloud.client import loads
                d = loads(raw or b"{}")             # (NaN / Infinity / 1e999 from a worker: None, never a float)
            if not isinstance(d, dict):
                return _json(400, {"error": "a JSON object"})
            # the LLM lane (sizeup/cloud/infer.py): a GPU worker's own token (sw1) only, never a station's whisper
            gpu = bearer.startswith(PREFIX + ".") and not wid.startswith("station:")
            if path.startswith(API + "infer/"):
                if not gpu:
                    return _json(403, {"error": "GPU workers only", "reason": "not_a_gpu_worker"})
                if self.infer is None:
                    return _json(404, {"error": "not found"})
                return _json(*self.infer.handle(wid, path, d, len(raw)))
            if self.infer is not None and gpu:
                self.infer.seen(wid, d.get("state"), d.get("models"))
            state, model = d.get("state"), d.get("model")
            state = state if state in sttpool.STATES else None
            model = str(model)[:80] if isinstance(model, str) else None
            load = d.get("load") if isinstance(d.get("load"), dict) else None
            # the self-update: a GPU worker that says its version is told the one it should run (never a station's)
            su = clean_self_update(d.get("self_update")) if gpu else None
            told = {"worker_update": self.wanted(wid)} if su is not None else {}
            # keep_awake {on, held} and a clean stop's reason "shutdown": Health's "keeps PC awake" / "(asleep?)"
            ka = clean_keep_awake(d.get("keep_awake")) if gpu else None
            why = d.get("reason") if isinstance(d.get("reason"), str) else ""
            if path == API + "hello":
                self.pool.seen(wid, state, model, d.get("gpu"), d.get("version") if isinstance(d.get("version"), str) else None,
                               kind=kind, load=load, update=su, keep_awake=ka, reason=why)
                s = self.reg.pool_settings()
                return _json(200, {"ok": True, "worker": wid, "poll_s": POLL_S, "pool_on": self._pool_on(),
                                   "classes": [k for k, v in s["classes"].items() if v], **told})
            if path == API + "state":
                self.pool.seen(wid, state, model, d.get("gpu"), d.get("version") if isinstance(d.get("version"), str) else None,
                               kind=kind, load=load, update=su, keep_awake=ka, reason=why)
                return _json(200, {"ok": True, "pool_on": self._pool_on(), **told})
            if path == API + "claim":
                wait = _num(d.get("wait")) or 0.0
                job = self.pool.claim(wid, wait, state, model, kind=kind, load=load)
                return _json(200, {"job": job.public() if job is not None else None})
            m = LEASE_PATH.fullmatch(path)
            if m is None or m.group(2) == "audio":
                return _json(404, {"error": "not found"})
            self.pool.seen(wid)
            lease, what = m.group(1), m.group(2)
            if what == "extend":
                job = self.pool.extend(wid, lease)
                if job is None:
                    return _json(409, {"error": "lease lost"})
                return _json(200, {"ok": True, "lease_s": round(max(0.0, job.expires - self.pool.clock()), 1),
                                   "cap_at": job.cap_at})
            if what == "complete":
                res = clean_result(d)
                return _json(200, {"ok": True}) if self.pool.complete(wid, lease, res) else \
                    _json(409, {"error": "lease lost: the job went to another engine"})
            if what == "release":
                why = str(d.get("why") or "")[:160]
                return _json(200, {"ok": True}) if self.pool.release(wid, lease, why) else _json(409, {"error": "lease lost"})
        except (ValueError, RecursionError) as e:          # (a body nested too deep is no result either)
            return _json(400, {"error": f"bad request: {type(e).__name__}"})
        return _json(404, {"error": "not found"})

    def _logs(self, wid: str, method: str, headers, body) -> tuple[int, bytes, str]:
        """A GPU worker's own log lines (deploy/stt-worker/worker.py LogShip): gzipped, read by WorkerLogs (never parsed
        here); not "seen" (the worker's log thread says nothing of its link); never in the way of its clips."""
        from sizeup.cloud.cwlogs import WORKER_MAX_BODY
        if method != "POST" or self.logs is None:
            return _json(404, {"error": "not found"})
        raw = body(WORKER_MAX_BODY) if body is not None else b""
        try:
            code, out = self.logs.ingest(wid, raw, headers)
        except Exception as e:  # noqa: BLE001 - the worker keeps the batch and sends it again
            self.log(f"stt workers: {wid}: logs: {type(e).__name__}")
            return _json(503, {"error": f"logs: {type(e).__name__}"})
        return _json(code, out)

    def _audio(self, wid: str, lease: str) -> tuple[int, bytes, str]:
        job = self.pool.job_for(wid, lease)
        if job is None:
            return _json(409, {"error": "lease lost"})
        try:
            # (waiting for the station's own STT input, #671: its class's budget from when it was queued, half of what
            # is left of the lease at most, so the clip is still transcribed in time; else the listening clip, #678)
            now = self.pool.clock()
            try:
                fast = self.src_fast() is True
            except Exception:  # noqa: BLE001 - not known: the old wait
                fast = False
            wait = (min(AUDIO_WAIT_MAX_S, sttpool.src_wait_s(job, now)) if fast
                    else max(0.0, min(AUDIO_WAIT_OLD_S, (job.expires - now) / 2.0)))
            wav = job.audio(wait) if job.audio_wait else job.audio()
        except Exception as e:  # noqa: BLE001 - a store that can't be read now: the next engine takes it
            wav = None
            self.log(f"stt workers: {job.id}: audio not readable: {type(e).__name__}")
        if not wav:
            self.pool.abandon(job.lease, "no audio on the hub for it")
            return _json(410, {"error": "no audio for this job: it went to another engine"})
        return 200, wav, "audio/wav"
