"""Phone pre-alert from Tapout: the ESP32 on the PCFD headquarters phone line (repo
firehouse-ring-alerter) posts to ntfy.sh when the station's phone rings. A ring is often the first
sign of a call, before 60 Control pages it, so the CAD shows it at the top of the 60 Control column
as a pre-alert (user, 2026-10-08: "lets receive that feed as a possible pre-alert of a possible
incoming call").

    [prealert] ring_topic   (pcfd-phone-prealert)  real ring alerts only, max priority:
                            title "STATION PHONE RINGING" (a commissioning test: "[TEST] STATION PHONE RINGING"),
                            message "Incoming call on the station line at 2026-06-26 14:03:21 EDT."
                            or, delivered late, "Phone rang ~45s ago at ... (alert delayed by network)."
    [prealert] status_topic (pcfd-status)  "Tapout heartbeat" hourly ("Ring detector still online
                            (hourly check-in, seq 12). ..."), "Tapout restarted" at boot, warnings
                            ("Detector line STUCK active", "ALERT DELIVERY FAILED", "Low memory"),
                            OTA lifecycle. The heartbeat is the silent-failure canary.

READ ONLY. The ring topic is the fire crew's real alert channel: sizeup never publishes to it, or to
any ntfy topic (user, 2026-10-08: "never send to it, we can read from it though"). Every request in
this module is a GET (_get, the only place a request is made; tests/test_prealert.py checks it).

Subscribing: one thread per topic on ntfy's JSON stream (GET <server>/<topic>/json, one JSON
object per line: "open", "keepalive" about every 45 s, "message"). A dropped stream reconnects with
backoff (1 s doubling to 60 s; a 429 rate limit waits at least 2 min, or what Retry-After says) and
resumes with since=<last message id>, so nothing is missed or repeated; the first connection asks
since=10m (a ring just before a restart is not lost; old ones are not news). The database dedupes by
ntfy id as well.

A ring -> a pre-alert {at, source "Tapout HQ phone", message, ntfy id} (table prealerts in the CAD
SQLite, so the hub's Litestream keeps them; listed 24 h, kept 90 days) and a line in the CAD event
log. It ends one of three ways, each also in the event log:
    matched    a 60 Control page for Port Chester (Cad.from_monitor) or a new emergency event (Cad.create)
               within expire_min after the ring (or up to 2 min before it): linked to that event,
               whose chronology says so
    dismissed  by an operator (POST /api/cad/prealert/<id>/dismiss: dispatchers and admins; in the
               person's activity on the hub)
    expired    nothing came within expire_min
A "[TEST]" ring is shown as a test and never matched.

Runs on the CAD of record only: the hub, or all-in-one. A station forwarding to a hub doesn't
subscribe (the hub does; two would double-alert); Settings > System > Station turning forwarding
off or on starts or stops it (App.prealert_sync).
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from datetime import datetime
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sizeup.cad.store import TZ, CadError, OPERATOR_MAX

SOURCE = "Tapout HQ phone"
TOPIC = re.compile(r"[-_A-Za-z0-9]{1,64}")
FIRST_SINCE = "10m"            # the first connection's window (later ones: since=<last id>)
LIST_S = 24 * 3600             # shown and in the state
KEEP_S = 90 * 24 * 3600        # kept in the database (the CSV export)
MATCH_BEFORE_S = 120           # an event made this long before the ring still matches it
UA = "sizeup-prealert (read-only ntfy subscriber)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prealerts (
  id INTEGER PRIMARY KEY,
  ntfy_id TEXT NOT NULL UNIQUE,         -- the ntfy message id (dedupe across reconnects and restarts)
  topic TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL,                     -- when the phone rang (ntfy time, less a late alert's delay)
  received_at REAL NOT NULL,
  source TEXT NOT NULL,                 -- "Tapout HQ phone"
  title TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT '',
  test INTEGER NOT NULL DEFAULT 0,      -- "[TEST] STATION PHONE RINGING": a commissioning test
  status TEXT NOT NULL,                 -- open | matched | dismissed | expired
  outcome_at REAL,
  incident_id INTEGER,                  -- matched: the event
  operator TEXT NOT NULL DEFAULT '',    -- dismissed: who
  note TEXT NOT NULL DEFAULT ''         -- dismissed: why, expired: "received late"
);
CREATE INDEX IF NOT EXISTS prealerts_at ON prealerts(at);
"""
CSV_COLUMNS = ["Rang", "Received", "Source", "Title", "Message", "Test", "Outcome", "Outcome at", "Seconds to outcome",
               "Incident #", "Operator", "Note", "ntfy id"]


def _get(url: str, timeout: float):
    """The one request this module makes: a GET (never a POST / PUT: the ring topic pages the crew)."""
    return urlopen(Request(url, method="GET", headers={"User-Agent": UA, "Accept": "application/x-ndjson"}),
                   timeout=timeout)


def _hms(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%H:%M:%S") if at else ""


def _ts(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%m/%d/%Y %H:%M:%S") if at else ""


def _ago(s: float) -> str:
    s = max(0, int(s))
    if s < 120:
        return f"{s} s"
    if s < 90 * 60:
        return f"{round(s / 60)} min"
    h = s / 3600
    return f"{h:.0f} h" if h >= 10 or abs(h - round(h)) < 0.05 else f"{h:.1f} h"


# -- the ntfy stream ----------------------------------------------------------------------------------


class NtfyStream:
    """One topic's JSON stream, read forever on its own thread. on_message(msg dict) for each
    "message"; "open" / "keepalive" only say the stream is alive."""

    BACKOFF_START = 1.0
    BACKOFF_MAX = 60.0
    BACKOFF_429 = 120.0          # a rate limit: wait longer (or Retry-After, up to RETRY_AFTER_MAX)
    RETRY_AFTER_MAX = 600.0
    READ_TIMEOUT = 100.0         # ntfy sends a keepalive every 45 s: two missed = a dead stream
    CONNECT_TIMEOUT = 20.0

    def __init__(self, server: str, topic: str, on_message: Callable[[dict], None], log: Callable[[str], None] = print,
                 first_since: str = FIRST_SINCE, clock: Callable[[], float] = time.time):
        if not TOPIC.fullmatch(topic):
            raise ValueError(f"ntfy topic: letters, digits, - and _ ({topic!r})")
        self.server, self.topic, self.on_message, self.log = server.rstrip("/"), topic, on_message, log
        self.first_since, self.clock = first_since, clock
        self.last_id = ""                      # resume point: since=<this>
        self.connected = False
        self.connected_at: float | None = None
        self.last_event_at: float | None = None
        self.down_since: float | None = None   # not connected since (None: never tried yet / connected)
        self.last_error = ""
        self.failures = 0
        self.connects = 0
        self._stop = threading.Event()
        self._resp = None
        self._thread: threading.Thread | None = None

    def url(self) -> str:
        return f"{self.server}/{self.topic}/json?since={quote(self.last_id or self.first_since, safe='')}"

    def start(self) -> NtfyStream:
        self._thread = threading.Thread(target=self._run, name=f"ntfy-{self.topic}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - closing a half-dead socket
                pass

    def state(self) -> dict:
        return {"topic": self.topic, "connected": self.connected, "connected_at": self.connected_at,
                "last_event_at": self.last_event_at, "down_since": self.down_since, "error": self.last_error}

    def _delay(self, e: Exception | None) -> float:
        if isinstance(e, HTTPError) and e.code == 429:
            try:
                ra = float(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                ra = 0.0
            return min(max(self.BACKOFF_429, ra), self.RETRY_AFTER_MAX)
        return min(self.BACKOFF_START * (2 ** max(0, self.failures - 1)), self.BACKOFF_MAX)

    def _run(self) -> None:
        while not self._stop.is_set():
            err: Exception | None = None
            try:
                self._read()
                self.failures = 0                 # the server ended a good stream: reconnect at once-ish
            except HTTPError as e:
                err = e
                self.last_error = f"HTTP {e.code}" + (" (rate limited)" if e.code == 429 else "")
            except (URLError, OSError, ValueError) as e:
                err = e
                self.last_error = f"{type(e).__name__}: {getattr(e, 'reason', None) or e}"
            if self.connected or self.down_since is None:
                self.down_since = self.clock()
            self.connected = False
            self._resp = None
            if self._stop.is_set():
                break
            if err is not None:
                self.failures += 1
            wait = self._delay(err) if err is not None else self.BACKOFF_START
            if err is not None and (self.failures in (1, 5) or self.failures % 30 == 0 or isinstance(err, HTTPError)):
                self.log(f"prealert: ntfy {self.topic}: {self.last_error}; retrying in {wait:.0f} s")
            self._stop.wait(wait)

    def _read(self) -> None:
        resp = _get(self.url(), self.CONNECT_TIMEOUT)
        self._resp = resp
        try:
            sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(self.READ_TIMEOUT)
            while not self._stop.is_set():
                line = resp.readline()
                if not line:
                    return                         # the server closed the stream
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue                       # not one of ours: skip the line, keep the stream
                if not isinstance(msg, dict):
                    continue
                ev = msg.get("event")
                now = self.clock()
                if ev == "open":
                    if not self.connected:
                        self.connected, self.connected_at, self.down_since = True, now, None
                        self.connects += 1
                        self.last_error = ""
                elif ev == "message":
                    mid = str(msg.get("id") or "")
                    if not mid:
                        continue
                    self.last_id = mid
                    try:
                        self.on_message(msg)
                    except Exception as e:  # noqa: BLE001 - one bad message never stops the feed
                        self.log(f"prealert: ntfy {self.topic}: message {mid}: {type(e).__name__}: {e}")
                if ev in ("open", "keepalive", "message"):
                    self.last_event_at = now
                    if not self.connected:          # some servers skip "open"
                        self.connected, self.connected_at, self.down_since = True, now, None
                        self.connects += 1
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass


# -- what a message says -------------------------------------------------------------------------------


def parse_ring(msg: dict) -> dict:
    """A message on the ring topic -> {at, title, message, test}. Every message there is a ring (the
    topic carries nothing else); "[TEST]" in the title (or the test_tube tag) is a commissioning test."""
    title = str(msg.get("title") or "")[:200]
    text = str(msg.get("message") or "")[:1000]
    tags = msg.get("tags") if isinstance(msg.get("tags"), list) else []
    at = float(msg.get("time") or time.time())
    late = re.search(r"rang ~(\d{1,5})s ago", text)
    if late:
        at -= int(late.group(1))
    return {"at": at, "title": title or "STATION PHONE RINGING", "message": text,
            "test": title.upper().startswith(("[TEST]", "[SIM")) or "test_tube" in tags}


def parse_status(msg: dict) -> dict:
    """A message on the status topic -> {kind: heartbeat | boot | other, at, title, message, priority, seq?}."""
    title = str(msg.get("title") or "")[:200]
    text = str(msg.get("message") or "")[:1000]
    tags = msg.get("tags") if isinstance(msg.get("tags"), list) else []
    kind = ("heartbeat" if title == "Tapout heartbeat" or "green_heart" in tags
            else "boot" if title == "Tapout restarted" else "other")
    out = {"kind": kind, "at": float(msg.get("time") or time.time()), "title": title, "message": text,
           "priority": int(msg.get("priority") or 3), "id": str(msg.get("id") or "")}
    m = re.search(r"\bseq (\d+)", text) if kind == "heartbeat" else re.search(r"Boot #(\d+)", text)
    if m:
        out["seq"] = int(m.group(1))
    return out


# -- the pre-alerts ----------------------------------------------------------------------------------------


class PreAlerts:
    """The CAD's phone pre-alerts and Tapout's health. Built by the app on the CAD of record
    (App: all-in-one or hub), attached as cad.prealert."""

    def __init__(self, cad, server: str = "https://ntfy.sh", ring_topic: str = "pcfd-phone-prealert",
                 status_topic: str = "pcfd-status", expire_min: float = 5.0, heartbeat_stale_min: float = 90.0,
                 log: Callable[[str], None] = print):
        self.cad, self.store, self.log = cad, cad.store, log
        self.clock = cad.clock
        self.expire_s = float(expire_min) * 60
        self.stale_s = float(heartbeat_stale_min) * 60
        self.server = server.rstrip("/")
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_change: Callable[[], None] = lambda: None       # make_server: push the screens now
        with self.store.lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.store.db.execute(stmt)
        self.health = self._load_health()
        self.ring_stream = NtfyStream(server, ring_topic, self.ring, log)
        # The status topic's first look goes back a heartbeat period and a half: the last one is
        # still there, so "heartbeat 40 min ago" survives a restart without waiting an hour.
        self.status_stream = NtfyStream(server, status_topic, self.status, log,
                                        first_since=f"{max(1, int(round(heartbeat_stale_min)))}m")
        self.prune()

    # -- lifecycle ----------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stopping.is_set()

    def start(self) -> PreAlerts:
        """Subscribe (again: Settings > System > Station turned forwarding off). A restart resumes
        each stream after its last message."""
        with self._lock:
            if self.running:
                return self
            if self._stopping.is_set():               # stopped before: fresh threads, same resume point
                old = (self.ring_stream, self.status_stream)
                self.ring_stream, self.status_stream = (
                    NtfyStream(o.server, o.topic, o.on_message, self.log, o.first_since) for o in old)
                for new, o in zip((self.ring_stream, self.status_stream), old):
                    new.last_id = o.last_id
                self._stopping = threading.Event()
            self.ring_stream.start()
            self.status_stream.start()
            self._thread = threading.Thread(target=self._expiry, args=(self._stopping,), name="prealert-expiry", daemon=True)
            self._thread.start()
        self.log(f"prealert: subscribed (read-only) to {self.server}/{self.ring_stream.topic} (rings) and "
                 f"/{self.status_stream.topic} (Tapout status); pre-alerts expire after {self.expire_s / 60:g} min")
        return self

    def stop(self) -> None:
        with self._lock:
            self._stopping.set()
            self.ring_stream.stop()
            self.status_stream.stop()

    def _expiry(self, stopping: threading.Event) -> None:
        n = 0
        while not stopping.wait(2.0):
            try:
                if self.tick():
                    self.on_change()
                n += 1
                if n % 1800 == 0:               # about an hour
                    self.prune()
            except Exception as e:  # noqa: BLE001 - never take the CAD down
                self.log(f"prealert: {type(e).__name__}: {e}")

    def prune(self) -> int:
        with self.store.lock:
            return self.store.db.execute("DELETE FROM prealerts WHERE at < ?", (self.clock() - KEEP_S,)).rowcount

    # -- the ring topic ---------------------------------------------------------------------------------

    def _log(self, db, text: str, iid=None, operator: str = "", at: float | None = None) -> None:
        self.store._event(db, iid, "prealert", text, operator, at=at)

    def ring(self, msg: dict) -> dict | None:
        """A message on the ring topic: a new pre-alert (None: seen before)."""
        mid = str(msg.get("id") or "")[:64]
        if not mid:
            return None
        r = parse_ring(msg)
        now = self.clock()
        late = now - r["at"] > self.expire_s       # replayed after an outage: on record, not news
        with self._lock:
            with self.store.tx() as db:
                cur = db.execute("INSERT OR IGNORE INTO prealerts (ntfy_id, topic, at, received_at, source, title, message, "
                                 "test, status, outcome_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (mid, str(msg.get("topic") or self.ring_stream.topic)[:64], r["at"], now, SOURCE,
                                  r["title"], r["message"], int(r["test"]), "expired" if late else "open",
                                  now if late else None, "received late" if late else ""))
                if not cur.rowcount:
                    return None
                pid = cur.lastrowid
                self._log(db, f"Phone pre-alert ({SOURCE}){' TEST' if r['test'] else ''}: {r['title']}"
                              + (f": {r['message']}" if r["message"] else ""), at=r["at"])
                if late:
                    self._log(db, f"Phone pre-alert {_hms(r['at'])} expired: received late, "
                                  f"{_ago(now - r['at'])} after the ring", at=now)
            self.log(f"prealert: {'TEST ' if r['test'] else ''}phone ringing at {_hms(r['at'])} ({SOURCE})"
                     + (" (received late: expired)" if late else ""))
            if not late and not r["test"]:
                self._match_recent(pid, r["at"])
        self.on_change()
        return self.get(pid)

    def _match_recent(self, pid: int, at: float) -> None:
        """A call already made just before the ring (60 Control was quicker) matches it at once."""
        rows = self.store.q("SELECT * FROM incidents WHERE created_at >= ? AND created_at <= ? "
                            "AND id NOT IN (SELECT incident_id FROM prealerts WHERE incident_id IS NOT NULL) "
                            "ORDER BY created_at DESC", (at - MATCH_BEFORE_S, self.clock() + 1))
        for inc in rows:
            if self._counts(inc):
                self._matched([pid], inc)
                return

    def _counts(self, inc: dict) -> bool:
        """A 60 Control page for us (queued from the monitor) or a new emergency event."""
        if inc["status"] in ("closed", "dismissed") and inc["source"] != "60 Control page":
            return False
        return inc["source"] == "60 Control page" or bool(inc["emergency"])

    def match(self, inc: dict | None) -> list[int]:
        """Cad.create / Cad.from_monitor made an event: the open pre-alerts it answers are matched."""
        if not inc or not self._counts(inc):
            return []
        with self._lock:
            self.tick()
            created = float(inc["created_at"])
            ids = [r["id"] for r in self.store.q("SELECT id FROM prealerts WHERE status = 'open' AND test = 0 "
                                                 "AND at <= ? ORDER BY at", (created + 5,))]
            if ids:
                self._matched(ids, inc)
        if ids:
            self.on_change()
        return ids

    def _matched(self, ids: list[int], inc: dict) -> None:
        now = self.clock()
        with self.store.tx() as db:
            for pid in ids:
                p = db.execute("SELECT at FROM prealerts WHERE id = ?", (pid,)).fetchone()
                db.execute("UPDATE prealerts SET status = 'matched', outcome_at = ?, incident_id = ? WHERE id = ? "
                           "AND status = 'open'", (now, inc["id"], pid))
                gap = float(inc["created_at"]) - p["at"]
                when = f"{_ago(abs(gap))} {'before' if gap >= 0 else 'after'} this event"
                what = "60 Control page" if inc["source"] == "60 Control page" else "event"
                self._log(db, f"Phone pre-alert {_hms(p['at'])} matched: {SOURCE} rang {when} ({what})", iid=inc["id"], at=now)
        self.log(f"prealert: matched {len(ids)} pre-alert(s) to {inc.get('number') or 'a 60 Control page'} (event {inc['id']})")

    def tick(self, now: float | None = None) -> int:
        """Expire the open pre-alerts nobody answered in time. The number expired."""
        now = self.clock() if now is None else now
        with self._lock:
            old = self.store.q("SELECT id, at FROM prealerts WHERE status = 'open' AND at < ?", (now - self.expire_s,))
            if not old:
                return 0
            with self.store.tx() as db:
                for p in old:
                    db.execute("UPDATE prealerts SET status = 'expired', outcome_at = ? WHERE id = ? AND status = 'open'",
                               (now, p["id"]))
                    self._log(db, f"Phone pre-alert {_hms(p['at'])} expired: no call within {self.expire_s / 60:g} min", at=now)
        return len(old)

    def dismiss(self, pid: int, body: dict) -> dict:
        """POST /api/cad/prealert/<id>/dismiss {operator, reason?}: an operator says it's nothing."""
        op = body.get("operator") or ""
        reason = body.get("reason") or ""
        if not isinstance(op, str) or not isinstance(reason, str):
            raise CadError("operator, reason: text")
        op, reason = op.strip()[:OPERATOR_MAX], reason.strip()[:200]
        with self._lock:
            p = self.store.one("SELECT * FROM prealerts WHERE id = ?", (pid,))
            if p is None:
                raise CadError(f"no pre-alert {pid}")
            if p["status"] != "open":
                raise CadError(f"pre-alert {pid} is already {p['status']}")
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE prealerts SET status = 'dismissed', outcome_at = ?, operator = ?, note = ? WHERE id = ?",
                           (now, op, reason, pid))
                self._log(db, f"Phone pre-alert {_hms(p['at'])} dismissed" + (f": {reason}" if reason else ""),
                          operator=op, at=now)
        self.on_change()
        return self.get(pid)

    def get(self, pid: int) -> dict:
        rows = self._rows("p.id = ?", (pid,))
        if not rows:
            raise CadError(f"no pre-alert {pid}")
        return rows[0]

    def _rows(self, where: str, args=(), limit: int = 200) -> list[dict]:
        rows = self.store.q("SELECT p.*, i.number AS incident_number FROM prealerts p LEFT JOIN incidents i ON i.id = p.incident_id "
                            f"WHERE {where} ORDER BY p.at DESC, p.id DESC LIMIT ?", (*args, limit))
        for r in rows:
            r["test"] = bool(r["test"])
        return rows

    # -- the status topic: Tapout's health -------------------------------------------------------------

    def _load_health(self) -> dict:
        row = self.store.one("SELECT value FROM meta WHERE key = 'tapout_status'")
        try:
            h = json.loads(row["value"]) if row else {}
        except ValueError:
            h = {}
        return h if isinstance(h, dict) else {}

    def status(self, msg: dict) -> None:
        s = parse_status(msg)
        with self._lock:
            h = dict(self.health)
            prev = h.get(s["kind"])
            if prev and prev.get("id") == s["id"]:
                return
            if s["kind"] in ("heartbeat", "boot"):
                if not prev or s["at"] >= prev.get("at", 0):
                    h[s["kind"]] = s
            if not h.get("last") or s["at"] >= h["last"].get("at", 0):
                h["last"] = s
            if s["kind"] == "other" and s["priority"] >= 4:
                h["warning"] = s
            self.health = h
            with self.store.lock:
                self.store.db.execute("INSERT INTO meta (key, value) VALUES ('tapout_status', ?) "
                                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(h),))
        self.on_change()

    def tapout(self, now: float | None = None) -> dict:
        """{state: ok | warn | unknown, word, detail}: "Tapout: ok, heartbeat 12 min ago" and the like.
        Only what was heard: never heard -> "no heartbeat yet"."""
        now = self.clock() if now is None else now
        h = self.health
        alive = [x for x in (h.get("heartbeat"), h.get("boot")) if x]
        last = max(alive, key=lambda x: x["at"]) if alive else None
        hb = h.get("heartbeat")
        parts = []
        if hb:
            parts.append(f"last heartbeat {_ts(hb['at'])}" + (f" (seq {hb['seq']})" if "seq" in hb else ""))
        if h.get("boot"):
            b = h["boot"]
            parts.append(f"last restart {_ts(b['at'])}" + (f" (boot #{b['seq']})" if "seq" in b else ""))
        w = h.get("warning")
        if w and now - w["at"] < LIST_S:
            parts.append(f"warning {_ts(w['at'])}: {w['title']}" + (f": {w['message']}" if w["message"] else ""))
        feed = self.status_stream
        if not feed.connected and feed.down_since is not None:
            parts.append(f"status feed ({feed.topic}) not connected since {_ts(feed.down_since)}"
                         + (f": {feed.last_error}" if feed.last_error else ""))
        if not self.ring_stream.connected and self.ring_stream.down_since is not None:
            parts.append(f"ring feed ({self.ring_stream.topic}) not connected since {_ts(self.ring_stream.down_since)}"
                         + (f": {self.ring_stream.last_error}" if self.ring_stream.last_error else ""))
        detail = "; ".join(parts)
        if last is None:
            return {"state": "unknown", "word": "no heartbeat yet", "at": None,
                    "detail": detail or f"nothing heard from Tapout on {feed.topic} yet"}
        age = now - last["at"]
        what = "heartbeat" if last["kind"] == "heartbeat" else "restarted"
        if age > self.stale_s:
            return {"state": "warn", "word": f"no heartbeat for {_ago(age)}", "at": last["at"], "detail": detail}
        if not self.ring_stream.connected and self.ring_stream.down_since is not None:
            return {"state": "warn", "word": f"ring feed offline since {_hms(self.ring_stream.down_since)}",
                    "at": last["at"], "detail": detail}
        return {"state": "ok", "word": f"ok, {what} {_ago(age)} ago", "at": last["at"], "detail": detail}

    # -- what the screens get --------------------------------------------------------------------------

    def state(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        self.tick(now)
        items = self._rows("p.at >= ?", (now - LIST_S,), 50)
        return {"time": now, "source": SOURCE, "expire_s": self.expire_s, "items": items,
                "open": [p for p in items if p["status"] == "open"],
                "tapout": self.tapout(now),
                "feeds": [self.ring_stream.state(), self.status_stream.state()]}

    def csv(self) -> bytes:
        """GET /api/cad/export/prealerts.csv: every ring kept (90 days) with its outcome, oldest first."""
        rows = list(reversed(self._rows("1 = 1", (), 100000)))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([_ts(r["at"]), _ts(r["received_at"]), r["source"], r["title"], r["message"], "yes" if r["test"] else "",
                        r["status"], _ts(r["outcome_at"]),
                        f"{r['outcome_at'] - r['at']:.0f}" if r["outcome_at"] else "",
                        r["incident_number"] or (f"#{r['incident_id']}" if r["incident_id"] else ""),
                        r["operator"], r["note"], r["ntfy_id"]])
        return buf.getvalue().encode("utf-8-sig")
