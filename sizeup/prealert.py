"""Phone pre-alert from Tapout: the ESP32 on the PCFD headquarters phone line (repo
firehouse-ring-alerter) posts to ntfy.sh when the station's phone rings. A ring is often the first
sign of a call, before 60 Control pages it, so the CAD shows it at the top of the 60 Control column
as a pre-alert (user, 2026-10-08: "lets receive that feed as a possible pre-alert of a possible
incoming call").

    ring topic   real ring alerts only, max priority:
                            title "STATION PHONE RINGING" (a commissioning test: "[TEST] STATION PHONE RINGING"),
                            message "Incoming call on the station line at 2026-06-26 14:03:21 EDT."
                            or, delivered late, "Phone rang ~45s ago at ... (alert delayed by network)."
    status topic "Tapout heartbeat" every 6 h ([prealert] heartbeat_every_min; it was hourly)
                            ("Ring detector still online (... seq 12). ..."), "Tapout restarted" at boot,
                            warnings ("Detector line STUCK active", "ALERT DELIVERY FAILED", "Low memory"),
                            OTA lifecycle. The heartbeat is the silent-failure canary; any message there
                            proves Tapout is alive. Nothing for heartbeat_stale_min (default: the period
                            + 10 % + 15 min, 411 min) -> "Tapout: no heartbeat for ..." in warning. A
                            fault (priority 4+) or "Tapout restarted UNEXPECTEDLY" warns with its title
                            until the next heartbeat or restart, its recovery ("Detector line recovered")
                            or the stale window (PreAlerts.fault). Header order: no heartbeat, ring feed
                            offline, device fault.

The topics' names are not in the repo: they come from Settings > System > Push notifications (the CAD settings, the
DB), overridden by SIZEUP_PREALERT_RING_TOPIC / _STATUS_TOPIC, else [prealert] ring_topic / status_topic
(sizeup/notify.py Pushes.prealert_topics). A topic not set = that feed is off (NoFeed), and Health says "not configured";
a change in Settings is picked up within seconds (sync_topics). They are also protected: notify.py never publishes to them.

READ ONLY. The ring topic is the fire crew's real alert channel: sizeup never publishes to it, or to
Tapout's status topic (user, 2026-10-08: "never send to it, we can read from it though"). Every request in
this module is a GET (_get, the only place a request is made; tests/test_prealert.py checks it). The one
place sizeup publishes is sizeup/notify.py, to topics of our own ([alerts] ntfy_topic, status_topic), and it refuses these.

Subscribing: one thread per topic on ntfy's JSON stream (GET <server>/<topic>/json, one JSON
object per line: "open", "keepalive" about every 45 s, "message"). A dropped stream reconnects with
backoff (1 s doubling to 60 s; a 429 rate limit waits at least 2 min, or what Retry-After says) and
resumes with since=<last message id>, so nothing is missed or repeated; the first connection asks
since=10m (a ring just before a restart is not lost; old ones are not news). The database dedupes by
ntfy id as well. The status topic's first connection looks back further (status_since(): a heartbeat period and a
half, 9 h, and at least heartbeat_stale_min), so a fresh start finds the last heartbeat. ntfy.sh keeps messages
about 12 h (its cache-duration); asking further back is harmless (ntfy returns what it still has), and Tapout's last
heartbeat is also kept in the CAD database (meta tapout_status), so a restart shows it before ntfy answers.

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
subscribe (the hub does; two would double-alert); Settings > Station > Hub link turning forwarding
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
from sizeup.config import HEARTBEAT_EVERY_MIN, heartbeat_stale_min as derive_stale_min
from sizeup.netcut import cut, socket_of

SOURCE = "Tapout HQ phone"
TOPIC = re.compile(r"[-_A-Za-z0-9]{1,64}")
FIRST_SINCE = "10m"            # the first connection's window (later ones: since=<last id>)
LIST_S = 24 * 3600             # shown and in the state
KEEP_S = 90 * 24 * 3600        # kept in the database (the CSV export)
MATCH_BEFORE_S = 120           # an event made this long before the ring still matches it
# Tapout's high-priority status messages that are not faults (it is fine): an OTA image no newer than what runs
NOT_A_FAULT = re.compile(r"\bOTA REJECTED\b.*\bnot newer than\b.*\brunning\b", re.I | re.S)
# A fault -> the message that ends it: (the fault's title, the recovery's title). A recovery never matches its fault.
# A heartbeat or a restart also ends any fault, though a heartbeat doesn't (yet) say the detector is armed.
RECOVERIES = (
    (re.compile(r"\bdetector line\b.*\bstuck\b", re.I), re.compile(r"\bdetector line\b.*\brecovered\b", re.I)),
    (re.compile(r"\bnot armed\b", re.I), re.compile(r"\barmed\b", re.I)),
)
from sizeup import USER_AGENT
UA = USER_AGENT + " ntfy read-only subscriber"

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


def status_since(every_min: float, stale_min: float) -> str:
    """The status feed's first look back, as ntfy's since=: a heartbeat period and a half (6 h -> 540m), and at least
    the stale threshold, so the last heartbeat (on time or overdue) is in it and a fresh start shows "ok, heartbeat
    5 h ago", not "no heartbeat yet" until the next one. Past ntfy's cache (ntfy.sh: 12 h) ntfy just returns what
    it has: nothing heard -> "no heartbeat yet" (unknown, not a warning)."""
    return f"{max(1, int(round(max(1.5 * every_min, stale_min))))}m"


def _get(url: str, timeout: float):
    """The one request this module makes: a GET (never a POST / PUT: the ring topic pages the crew)."""
    return urlopen(Request(url, method="GET", headers={"User-Agent": UA, "Accept": "application/x-ndjson"}),
                   timeout=timeout)


def _hms(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%H:%M:%S") if at else ""


def _ts(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%m/%d/%Y %H:%M:%S") if at else ""


def _recovers(title: str, fault: str) -> bool:
    """Does a status message with this title end that fault (RECOVERIES)?"""
    return any(f.search(fault) and r.search(title) and not f.search(title) for f, r in RECOVERIES)


def _short(text: str, n: int = 60) -> str:
    """A title for the header: one line, at most n characters."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


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
                 first_since: str = FIRST_SINCE, clock: Callable[[], float] = time.time, label: str = "feed"):
        if not TOPIC.fullmatch(topic):
            raise ValueError(f"ntfy topic: letters, digits, - and _ ({topic!r})")
        self.server, self.topic, self.on_message, self.log = server.rstrip("/"), topic, on_message, log
        self.first_since, self.clock, self.label = first_since, clock, label    # label: what logs call it (never the name)
        self.last_id = ""                      # resume point: since=<this>
        self.connected = False
        self.connected_at: float | None = None
        self.last_event_at: float | None = None
        self.down_since: float | None = None   # not connected since (None: never tried yet / connected)
        self.last_error = ""
        self.failures = 0
        self.connects = 0
        self._stop = threading.Event()
        self._sock = None                      # the open stream's socket (stop() shuts it down)
        self._thread: threading.Thread | None = None

    def url(self) -> str:
        return f"{self.server}/{self.topic}/json?since={quote(self.last_id or self.first_since, safe='')}"

    def start(self) -> NtfyStream:
        self._thread = threading.Thread(target=self._run, name=f"ntfy-{self.label}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Returns at once, whatever the reader is doing (#546). Never resp.close() here: the reader thread is blocked in
        resp.readline() holding the buffer's lock, and BufferedReader.close() waits for that lock, i.e. for ntfy's next
        keepalive (~45 s; a deploy's drain was killed at its 30 s limit). The socket is shut down instead: the blocked
        read wakes with an error or end of stream, the reader sees _stop and ends quietly (no log, no reconnect), and
        closes the response itself. Idempotent."""
        self._stop.set()
        cut(self._sock)

    def join(self, timeout: float | None = None) -> bool:
        """After stop(): wait for the reader thread to end. True when it has."""
        t = self._thread
        if t is not None:
            t.join(timeout)
        return t is None or not t.is_alive()

    def state(self) -> dict:
        return {"feed": self.label, "configured": True, "connected": self.connected, "connected_at": self.connected_at,
                "last_event_at": self.last_event_at, "down_since": self.down_since, "error": self.last_error}

    def _delay(self, e: Exception | None) -> float:
        if isinstance(e, HTTPError) and e.code == 429:
            try:
                ra = float(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                ra = 0.0
            return min(max(self.BACKOFF_429, ra), self.RETRY_AFTER_MAX)
        return min(self.BACKOFF_START * (2 ** min(max(0, self.failures - 1), 30)), self.BACKOFF_MAX)

    def _run(self) -> None:
        while not self._stop.is_set():
            err: Exception | None = None
            try:
                self._read()
                self.failures = 0                 # the server ended a good stream: reconnect at once-ish
            except HTTPError as e:
                err = e
                if not self._stop.is_set():
                    self.last_error = f"HTTP {e.code}" + (" (rate limited)" if e.code == 429 else "")
            except (URLError, OSError, ValueError) as e:
                err = e
                if not self._stop.is_set():
                    self.last_error = f"{type(e).__name__}: {getattr(e, 'reason', None) or e}"
            except Exception as e:  # noqa: BLE001 - e.g. http.client on a socket closed under it
                # (AttributeError "'NoneType' object has no attribute 'peek'", seen 2026-10-13): never let
                # the listener thread die, or rings stop arriving until the hub restarts.
                err = e
                if not self._stop.is_set():
                    self.last_error = f"{type(e).__name__}: {e}"
            if self.connected or self.down_since is None:
                self.down_since = self.clock()
            self.connected = False
            if self._stop.is_set():               # stop() cut the socket under the read: not an error, never retried
                break
            if err is not None:
                self.failures += 1
            wait = self._delay(err) if err is not None else self.BACKOFF_START
            if err is not None and (self.failures in (1, 5) or self.failures % 30 == 0 or isinstance(err, HTTPError)):
                self.log(f"prealert: ntfy {self.label}: {self.last_error}; retrying in {wait:.0f} s")
            self._stop.wait(wait)

    def _read(self) -> None:
        resp = _get(self.url(), self.CONNECT_TIMEOUT)
        try:
            sock = socket_of(resp)
            self._sock = sock                      # set before _stop is looked at: a stop() from now on cuts it
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
                        self.log(f"prealert: ntfy {self.label}: message {mid}: {type(e).__name__}: {e}")
                if ev in ("open", "keepalive", "message"):
                    self.last_event_at = now
                    if not self.connected:          # some servers skip "open"
                        self.connected, self.connected_at, self.down_since = True, now, None
                        self.connects += 1
        finally:
            self._sock = None
            try:
                resp.close()                       # this thread's own read has returned: never waits on a lock
            except Exception:  # noqa: BLE001
                pass


# -- what a message says -------------------------------------------------------------------------------


class NoFeed:
    """A feed with no topic set: does nothing (never a request), not connected, never "down"."""
    topic = ""
    server = ""
    on_message = None
    first_since = FIRST_SINCE
    last_id = ""
    connected = False
    connected_at = last_event_at = down_since = None
    last_error = ""

    def __init__(self, label: str = "feed"):
        self.label = label

    def start(self) -> NoFeed:
        return self

    def stop(self) -> None:
        pass

    def state(self) -> dict:
        return {"feed": self.label, "configured": False, "connected": False, "connected_at": None, "last_event_at": None,
                "down_since": None, "error": ""}


class OffFeed(NoFeed):
    """A topic is set but outbound is off here (sizeup/outbound.py: a hub started by hand, all-in-one): never a request.
    It keeps the topic it stands for, so sync_topics() sees nothing changed and doesn't make it again every 2 s."""

    def __init__(self, label: str, topic: str, why: str):
        super().__init__(label)
        self.topic, self.why = topic, why

    def state(self) -> dict:
        return dict(super().state(), configured=True, error="not subscribed: outbound off")


def _outbound_off(server: str) -> str:
    from sizeup import outbound
    return outbound.check("prealert", server)


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
    """A message on the status topic -> {kind: heartbeat | boot | other, at, title, message, priority, seq?,
    unexpected?}. "Tapout restarted UNEXPECTEDLY" (newer firmware: a crash or watchdog reset) is a boot, unexpected."""
    title = str(msg.get("title") or "")[:200]
    text = str(msg.get("message") or "")[:1000]
    tags = msg.get("tags") if isinstance(msg.get("tags"), list) else []
    kind = ("heartbeat" if title == "Tapout heartbeat" or "green_heart" in tags
            else "boot" if re.fullmatch(r"Tapout restarted(\s+unexpectedly)?[.!]?", title.strip(), re.I) else "other")
    out = {"kind": kind, "at": float(msg.get("time") or time.time()), "title": title, "message": text,
           "priority": int(msg.get("priority") or 3), "id": str(msg.get("id") or "")}
    if kind == "boot" and "unexpectedly" in title.lower():
        out["unexpected"] = True
    m = re.search(r"\bseq (\d+)", text) if kind == "heartbeat" else re.search(r"Boot #(\d+)", text)
    if m:
        out["seq"] = int(m.group(1))
    return out


# -- the pre-alerts ----------------------------------------------------------------------------------------


class PreAlerts:
    """The CAD's phone pre-alerts and Tapout's health. Built by the app on the CAD of record
    (App: all-in-one or hub), attached as cad.prealert."""

    def __init__(self, cad, server: str = "https://ntfy.sh", ring_topic: str = "", status_topic: str = "",
                 expire_min: float = 5.0, heartbeat_stale_min: float | None = None, log: Callable[[str], None] = print,
                 topics: Callable[[], tuple[str, str]] | None = None, heartbeat_every_min: float = HEARTBEAT_EVERY_MIN):
        """ring_topic / status_topic: fixed ("" = that feed off); topics(): (ring, status) asked every few seconds instead
        (the app: env over Settings over the config), so Settings can set or change them with no restart.
        heartbeat_every_min: Tapout's heartbeat period; heartbeat_stale_min None: derived from it (config.py)."""
        self.cad, self.store, self.log = cad, cad.store, log
        self.clock = cad.clock
        self.expire_s = float(expire_min) * 60
        self.every_s = float(heartbeat_every_min) * 60
        self.stale_s = float(derive_stale_min(heartbeat_every_min) if heartbeat_stale_min is None
                             else heartbeat_stale_min) * 60
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
        self._topics = topics or (lambda: (ring_topic, status_topic))
        ring, status = self._wanted()
        self.ring_stream = self._feed("ring", ring)
        self.status_stream = self._feed("status", status)
        self.prune()

    def _wanted(self) -> tuple[str, str]:
        try:
            ring, status = self._topics()
        except Exception as e:  # noqa: BLE001 - Settings unreadable: the feeds stay as they are
            self.log(f"prealert: topics unreadable ({type(e).__name__})")
            return (self.ring_stream.topic, self.status_stream.topic) if hasattr(self, "status_stream") else ("", "")
        ok = lambda t: (t or "").strip() if TOPIC.fullmatch((t or "").strip()) else ""      # noqa: E731
        return ok(ring), ok(status)

    def _feed(self, which: str, topic: str, last_id: str = ""):
        """The stream for a topic ("" = none set: NoFeed). The status feed's first look goes back a heartbeat period and a
        half (status_since): the last heartbeat is still there, so "heartbeat 5 h ago" survives a restart without
        waiting up to 6 h for the next one."""
        if not topic:
            return NoFeed(which)
        if off := _outbound_off(self.server):    # a hub on a restored copy: not the department's real feed (logged by
            return OffFeed(which, topic, off)    # sync_topics / start)
        if which == "ring":
            f = NtfyStream(self.server, topic, self.ring, self.log, label=which)
        else:
            f = NtfyStream(self.server, topic, self.status, self.log, label=which,
                           first_since=status_since(self.every_s / 60, self.stale_s / 60))
        f.last_id = last_id
        return f

    def sync_topics(self) -> bool:
        """Follow the topics in effect: a feed whose topic was set, changed or cleared is stopped and made again (a new
        topic starts fresh; the same one resumes). True when something changed. Never logs a name."""
        with self._lock:
            ring, status = self._wanted()
            changed = False
            for which, topic in (("ring", ring), ("status", status)):
                cur = getattr(self, f"{which}_stream")
                if cur.topic == topic and not (isinstance(cur, OffFeed) and not _outbound_off(self.server)):
                    continue
                cur.stop()
                new = self._feed(which, topic)
                setattr(self, f"{which}_stream", new)
                if self.running:
                    new.start()
                if isinstance(new, OffFeed):
                    self.log(f"prealert: {which} feed set, not subscribed ({new.why})")
                else:
                    self.log(f"prealert: {which} feed {'set' if topic else 'cleared'}"
                             + (f" ({'subscribed' if self.running else 'will subscribe'}, read-only)" if topic else ""))
                changed = True
            return changed

    # -- lifecycle ----------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stopping.is_set()

    def start(self) -> PreAlerts:
        """Subscribe (again: Settings > Station > Hub link turned forwarding off). A restart resumes
        each stream after its last message."""
        with self._lock:
            if self.running:
                return self
            self.sync_topics()                          # the topics in effect now (Settings, env, config)
            if self._stopping.is_set():               # stopped before: fresh threads, same resume point
                old = (self.ring_stream, self.status_stream)
                self.ring_stream = self._feed("ring", old[0].topic, old[0].last_id)
                self.status_stream = self._feed("status", old[1].topic, old[1].last_id)
                self._stopping = threading.Event()
            self.ring_stream.start()
            self.status_stream.start()
            self._thread = threading.Thread(target=self._expiry, args=(self._stopping,), name="prealert-expiry", daemon=True)
            self._thread.start()
        feeds = (("rings", self.ring_stream), ("Tapout status", self.status_stream))
        on = [n for n, f in feeds if f.topic and not isinstance(f, OffFeed)]
        off = [f for _, f in feeds if isinstance(f, OffFeed)]
        if off and not on:
            self.log(f"prealert: not subscribed ({off[0].why})")
            return self
        self.log(f"prealert: subscribed (read-only) to {self.server}: " + (" and ".join(on) if on else "nothing")
                 + (f"; pre-alerts expire after {self.expire_s / 60:g} min" if on
                    else " (no topic set: Settings > System > Push notifications)"))
        return self

    def stop(self) -> None:
        """Returns at once (#546: first in a deploy's drain). The lock only to mark it stopped and see which streams are
        running (sync_topics starts no new one after this); the streams are stopped outside it, and each stop() only
        cuts its socket. Idempotent."""
        with self._lock:
            self._stopping.set()
            streams = (self.ring_stream, self.status_stream)
        for s in streams:
            s.stop()

    def _expiry(self, stopping: threading.Event) -> None:
        n = 0
        while not stopping.wait(2.0):
            try:
                if self.sync_topics():
                    self.on_change()
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
                # Never a reason to check the CAD feed early (the user's decision): the incident sources are not poked
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
        auto = inc["source"] in ("60 Control page", "CAD feed")      # a page, or an external CAD feed's (#297)
        if inc["status"] in ("closed", "dismissed") and not auto:
            return False
        return auto or bool(inc["emergency"])

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
            w = h.get("warning")
            if s["kind"] == "other" and w and s["at"] > w.get("at", 0) and _recovers(s["title"], w["title"]):
                h["recovered"] = dict(s, of=w.get("id"))   # "Detector line recovered" ends "Detector line STUCK active"
            elif (s["kind"] == "other" and s["priority"] >= 4
                    and not NOT_A_FAULT.search(f"{s['title']}\n{s['message']}")
                    and (not w or s["at"] >= w.get("at", 0))):
                h["warning"] = s                    # the firmware's "high" status: a device fault (fault())
            self.health = h
            with self.store.lock:
                self.store.db.execute("INSERT INTO meta (key, value) VALUES ('tapout_status', ?) "
                                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(h),))
        self.on_change()

    def fault(self, now: float | None = None) -> dict | None:
        """The device fault in effect: the newest high-priority status message ("ALERT DELIVERY FAILED", "Detector line
        STUCK active", "Ring detector NOT armed", ...; not NOT_A_FAULT) or an unexpected restart, when nothing ended it:
        a later heartbeat or restart, or its recovery ("Detector line recovered", RECOVERIES). A normal-priority message
        (an OTA step) says nothing about it. One older than the stale window is over too (the heartbeat would have
        ended it): never a warning for ever."""
        now = self.clock() if now is None else now
        h = self.health
        hb, boot, w, rec = h.get("heartbeat"), h.get("boot"), h.get("warning"), h.get("recovered")
        ends = [x["at"] for x in (hb, boot) if x]
        if w and rec and rec.get("of") == w.get("id"):
            ends.append(rec["at"])
        if w and now - w["at"] <= self.stale_s and not any(t > w["at"] for t in ends):
            return w
        if (boot and boot.get("unexpected") and now - boot["at"] <= self.stale_s
                and (not hb or boot["at"] > hb["at"])):
            return boot
        return None

    def tapout(self, now: float | None = None) -> dict:
        """{state: ok | warn | unknown, word, detail}: "Tapout: ok, heartbeat 12 min ago" and the like.
        Only what was heard: never heard -> "no heartbeat yet". Any message on the status topic (a heartbeat, a restart,
        a warning, OTA) proves Tapout is alive: the newest of them is what goes stale. A device fault (fault()) warns
        with its title: "Tapout: ALERT DELIVERY FAILED, 3 min ago", "Tapout: restarted unexpectedly 2 min ago"."""
        now = self.clock() if now is None else now
        h = self.health
        alive = [x for x in (h.get("heartbeat"), h.get("boot"), h.get("last")) if x]
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
            rec = h.get("recovered")
            if rec and rec.get("of") == w.get("id"):
                parts.append(f"{rec['title']} {_ts(rec['at'])}")
            elif hb and hb["at"] > w["at"] and not (h.get("boot") and w["at"] < h["boot"]["at"] < hb["at"]):
                parts.append(f"cleared by the heartbeat {_ts(hb['at'])} "
                             "(a heartbeat doesn't prove the detector is armed)")
        feed = self.status_stream
        if not self.ring_stream.topic and not feed.topic:
            return {"state": "unconfigured", "word": "not configured", "at": None,
                    "detail": "No pre-alert topic is set: Settings > System > Push notifications (admins)"}
        if feed.topic and not feed.connected and feed.down_since is not None:
            parts.append(f"status feed not connected since {_ts(feed.down_since)}"
                         + (f": {feed.last_error}" if feed.last_error else ""))
        if not self.ring_stream.topic:
            parts.append("ring topic not set: no pre-alerts (Settings > System > Push notifications)")
        elif not self.ring_stream.connected and self.ring_stream.down_since is not None:
            parts.append(f"ring feed not connected since {_ts(self.ring_stream.down_since)}"
                         + (f": {self.ring_stream.last_error}" if self.ring_stream.last_error else ""))
        detail = "; ".join(parts)
        if last is None:
            return {"state": "unknown", "word": "no heartbeat yet", "at": None,
                    "detail": detail or ("nothing heard from Tapout yet" if feed.topic
                                                  else "status topic not set: no heartbeat to watch")}
        age = now - last["at"]
        what = {"heartbeat": "heartbeat", "boot": "restarted"}.get(last["kind"], "heard")
        b = h.get("boot")
        if b and b.get("unexpected") and now - b["at"] < LIST_S:
            detail = "; ".join(["restarted unexpectedly " + _ts(b["at"]) + (f": {b['message']}" if b["message"] else ""),
                                *([detail] if detail else [])])
        if last["kind"] == "other" and not (w and w.get("id") == last.get("id") and now - w["at"] < LIST_S):
            detail = "; ".join([f"last message {_ts(last['at'])}: {last['title']}", *parts])
        if age > self.stale_s:
            return {"state": "warn", "word": f"no heartbeat for {_ago(age)}", "at": last["at"], "detail": detail}
        if self.ring_stream.topic and not self.ring_stream.connected and self.ring_stream.down_since is not None:
            return {"state": "warn", "word": f"ring feed offline since {_hms(self.ring_stream.down_since)}",
                    "at": last["at"], "detail": detail}
        if f := self.fault(now):
            word = (f"restarted unexpectedly {_ago(now - f['at'])} ago" if f["kind"] == "boot"
                    else f"{_short(f['title']) or 'warning'}, {_ago(now - f['at'])} ago")
            return {"state": "warn", "word": word, "at": last["at"], "detail": detail, "fault": True}
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

    def feed_health(self) -> dict:
        """Settings > Station > Health's row for the pre-alert feed: ok / error, or not configured (no topic set: the first-run hint)."""
        ring = self.ring_stream
        if not ring.topic:
            return {"key": "prealert", "label": "Phone pre-alert (Tapout)", "state": "not configured",
                    "why": "Set the ring topic in Settings > System > Push notifications", "at": None}
        state = "ok" if ring.connected else ("error" if ring.down_since is not None else "starting")
        return {"key": "prealert", "label": "Phone pre-alert (Tapout)", "state": state,
                "why": ring.last_error if state == "error" else "", "at": ring.last_event_at}

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
