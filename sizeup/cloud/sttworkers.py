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
    POST /api/station/stt/state                   {state: online|gaming|paused, model, gpu, version} -> {ok, pool_on}
    POST /api/station/stt/claim                   {wait <= 25, state, model} -> {job: {lease, id, kind, channel, class,
                                                  prompt, seconds, lease_s, cap_at, fast, audio} | null}; fast: one
                                                  greedy pass (temperature_inc 0), the short-lease classes
    GET  /api/station/stt/jobs/<lease>/audio      audio/wav: 16 kHz mono 16-bit, padded, as whisper-server takes it
    POST /api/station/stt/jobs/<lease>/extend     {} -> {ok, lease_s, cap_at}
    POST /api/station/stt/jobs/<lease>/complete   {text, segments: [{start, end, text, words: [{word, start, end,
                                                  probability}]}], model, timing: {fetch_ms, infer_ms}} -> {ok}
    POST /api/station/stt/jobs/<lease>/release    {why} -> {ok}: the next engine takes it now

The heartbeat contract: while it runs, a worker sends a request (state, extend or claim) at least every 6 s, during a
decode too. One unheard for 12 s (sttpool.LOST_S) loses every lease it holds, catch-up ones included, and those clips go
to the next engine. A pre-alert job (fast) is leased 4 s, renewable by extend to 8 s from the claim; a worker that goes
gaming or paused has its pre-alert jobs taken back at once (their complete / extend then answer 409). A worker holds two
leases at most, and a pre-alert job only when it holds none.
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
from typing import Callable

from sizeup import sttpool

PREFIX = "sw1"
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TOKEN = re.compile(r"sw1\.([a-z0-9][a-z0-9_-]{0,39})\.([A-Za-z0-9_-]{43})")
NAME_MAX = 60
ROTATE_GRACE_S = 600.0
TOUCH_S = 300.0
META_POOL = "stt_pool"
API = "/api/station/stt/"
LEASE_PATH = re.compile(r"/api/station/stt/jobs/([0-9a-f]{16})/(audio|extend|complete|release)")
MAX_BODY = 1024 * 1024                  # a result: the text and its words (a 2-minute clip's are ~100 KB)
TEXT_MAX = 20000
SEGMENTS_MAX = 1000
WORDS_MAX = 4000
POLL_S = 25.0
AUDIO_WAIT_MAX_S = 2.0          # a job's audio: the station's STT input waited for this long at most (it usually comes a
                                # moment after its event; one that doesn't come costs the worker this much of its lease)
STATION_PREFIX = "station:"     # a station's whisper as a pool worker: "station:<station id>" (never a token's id)
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


class WorkerRegistry:
    """The hub's workers (hub.sqlite "stt_workers") and the pool's switches (meta "stt_pool"). mdb: the MonitorDb."""

    def __init__(self, mdb, clock: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.mdb, self.clock, self.log = mdb, clock, log
        self._used: dict[str, float] = {}
        self._written: dict[str, float] = {}
        self._any: tuple[float, bool] = (0.0, False)     # (read at, any active worker): read at most once a second
        self._lock = threading.Lock()

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
        out = {"on": True, "stations": True, "classes": {sttpool.class_key(c): True for c in sttpool.CLASSES}}
        try:
            saved = json.loads(self.mdb.get(META_POOL, "") or "{}")
        except ValueError:
            saved = {}
        if isinstance(saved, dict):
            if isinstance(saved.get("on"), bool):
                out["on"] = saved["on"]
            if isinstance(saved.get("stations"), bool):
                out["stations"] = saved["stations"]
            for k, v in (saved.get("classes") or {}).items() if isinstance(saved.get("classes"), dict) else ():
                if k in out["classes"] and isinstance(v, bool):
                    out["classes"][k] = v
        return out

    def enabled(self, cls: float) -> bool:
        """Do this class's clips go to the pool first now: the pool is on, the class is on, and a worker can be there (a
        GPU worker added, or the stations' whisper in the pool: #639 phase 2)."""
        s = self.pool_settings()
        if not s["on"] or not s["classes"].get(sttpool.class_key(cls), False):
            return False
        return bool(s["stations"]) or self.any()

    def stations_on(self) -> bool:
        """The stations' whisper are pool workers (Settings > Station > STT workers, on by default)."""
        s = self.pool_settings()
        return bool(s["on"]) and bool(s["stations"])

    def set_pool(self, on=None, classes=None, stations=None) -> tuple[dict, dict]:
        old = self.pool_settings()
        new = {"on": old["on"], "stations": old["stations"], "classes": dict(old["classes"])}
        if on is not None:
            if not isinstance(on, bool):
                raise WorkerError("on: true or false")
            new["on"] = on
        if stations is not None:
            if not isinstance(stations, bool):
                raise WorkerError("stations: true or false")
            new["stations"] = stations
        if classes is not None:
            if not isinstance(classes, dict):
                raise WorkerError("classes: an object")
            for k, v in classes.items():
                if k not in new["classes"] or not isinstance(v, bool):
                    raise WorkerError(f"classes: {', '.join(new['classes'])}, each true or false")
                new["classes"][k] = v
        self.mdb.put(META_POOL, json.dumps(new, sort_keys=True))
        return old, new

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
                        **((live or (lambda _w: {}))(r["id"]))})
        return {"at": now, "workers": out, "pool": self.pool_settings(), "active": self.any(),
                "grace_s": ROTATE_GRACE_S}

    def act(self, body: dict, by: str = "", note: Callable[[str, object, object], None] | None = None,
            on_revoke: Callable[[str], None] | None = None) -> dict:
        """POST /api/cad/stt-workers {action: add {id, name?} | rename {id, name} | rotate {id} | revoke {id} |
        pool {on?, classes?}}. The answer of add / rotate carries the token: the only time it exists outside the
        worker. note(key, old, new): the Settings audit (never a token)."""
        a = str(body.get("action") or "")
        note = note or (lambda k, o, n: None)
        if a == "add":
            token = self.add(body.get("id"), body.get("name"), by)
            wid = self._id(body.get("id"))
            note(f"stt workers/{wid}", None, f"added ({self._name(body.get('name')) or 'no name'})")
            return {"ok": True, "id": wid, "token": token}
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
            if old["stations"] != new["stations"]:
                note("stt workers/pool/stations", "on" if old["stations"] else "off", "on" if new["stations"] else "off")
            for k, v in new["classes"].items():
                if old["classes"].get(k) != v:
                    note(f"stt workers/pool/{k}", "on" if old["classes"].get(k) else "off", "on" if v else "off")
            return {"ok": True, "pool": new}
        raise WorkerError("action: add, rename, rotate, revoke or pool")


def _json(code: int, obj) -> tuple[int, bytes, str]:
    return code, json.dumps(obj, separators=(",", ":")).encode(), "application/json"


def _num(v) -> float | None:
    """A finite number, else None (NaN and infinities never get in)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _prob(v) -> float | None:
    """A probability: 0 to 1, else None."""
    f = _num(v)
    return f if f is not None and 0.0 <= f <= 1.0 else None


def clean_result(raw: dict) -> dict:
    """A worker's result, checked and bounded: {text, segments, words, model, timing}. ValueError."""
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
            "timing": {k: _num(timing.get(k)) for k in ("fetch_ms", "infer_ms") if _num(timing.get(k)) is not None}}


class WorkerApi:
    """Hub.handle's /api/station/stt/ routes. may_write(): this task is the leader (a standby hands out nothing)."""

    def __init__(self, registry: WorkerRegistry, pool: sttpool.Pool, may_write: Callable[[], bool] = lambda: True,
                 log: Callable[[str], None] = print, station: Callable[[str], str | None] | None = None):
        """station(bearer) -> the station id a station's own token proves (sizeup/cloud/stations.py), None when it
        isn't one: a station's whisper is a pool worker under its station identity ("station:<id>"), no token more."""
        self.reg, self.pool, self.may_write, self.log, self.station = registry, pool, may_write, log, station
        self._said: dict[tuple[str, str], float] = {}

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
            if not self.reg.stations_on():
                return _json(403, {"error": "the stations' whisper isn't in the pool (Settings > Station > STT workers)",
                                   "reason": "stations_off"})
            wid, kind = f"{STATION_PREFIX}{sid}", "station"
        if not self.may_write():
            from sizeup import fence as fence_mod
            fence_mod.refuse(f"{method} {path}")    # a standby: 503 + Retry-After (sizeup/web/server.py)
        if kind == "gpu":
            self.reg.touch(wid)
        try:
            if method == "GET":
                m = LEASE_PATH.fullmatch(path)
                if m and m.group(2) == "audio":
                    return self._audio(wid, m.group(1))
                return _json(404, {"error": "not found"})
            raw = body(MAX_BODY) if body is not None else b""
            if raw is None:
                return _json(413, {"error": "too large"})
            d = json.loads(raw or b"{}")
            if not isinstance(d, dict):
                return _json(400, {"error": "a JSON object"})
            state, model = d.get("state"), d.get("model")
            state = state if state in sttpool.STATES else None
            model = str(model)[:80] if isinstance(model, str) else None
            load = d.get("load") if isinstance(d.get("load"), dict) else None
            if path == API + "hello":
                self.pool.seen(wid, state, model, d.get("gpu"), d.get("version") if isinstance(d.get("version"), str) else None,
                               kind=kind, load=load)
                s = self.reg.pool_settings()
                return _json(200, {"ok": True, "worker": wid, "poll_s": POLL_S, "pool_on": self._pool_on(),
                                   "classes": [k for k, v in s["classes"].items() if v]})
            if path == API + "state":
                self.pool.seen(wid, state, model, d.get("gpu"), d.get("version") if isinstance(d.get("version"), str) else None,
                               kind=kind, load=load)
                return _json(200, {"ok": True, "pool_on": self._pool_on()})
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

    def _audio(self, wid: str, lease: str) -> tuple[int, bytes, str]:
        job = self.pool.job_for(wid, lease)
        if job is None:
            return _json(409, {"error": "lease lost"})
        try:
            # (waiting for the station's own STT input, #671: half of what is left of the lease at most, so the clip is
            # still transcribed in time; else the listening clip)
            wait = max(0.0, min(AUDIO_WAIT_MAX_S, (job.expires - self.pool.clock()) / 2.0))
            wav = job.audio(wait) if job.audio_wait else job.audio()
        except Exception as e:  # noqa: BLE001 - a store that can't be read now: the next engine takes it
            wav = None
            self.log(f"stt workers: {job.id}: audio not readable: {type(e).__name__}")
        if not wav:
            self.pool.abandon(job.lease, "no audio on the hub for it")
            return _json(410, {"error": "no audio for this job: it went to another engine"})
        return 200, wav, "audio/wav"
