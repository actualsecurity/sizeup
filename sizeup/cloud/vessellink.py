"""Our AIS receiver's vessels, from the station to the hub (GitHub #402).

The station's decoder (sizeup-ais.service, sizeup/radio/aisd.py) keeps each vessel's latest AIS messages in
MESSAGES. Only with [vessels.receiver] enabled, this reads them every READ_S into a Fleet (sizeup/vessels.py: the same
merge the hub does) and POSTs the hub /api/station/vessels (the station token, like every /api/station/ call),
gzipped: {v, at, vessels: [the Fleet's records], rx: the decoder's status}. The hub keeps them as the Vessels layer's
"receiver" feed (sizeup/vessels.py StationFeed).

Bounded: vessels are slow and few (tens in the box), so each post is the whole list, as often as the hub says (its
answer's "every": 5 s while someone looks at the layer or a marine call is open, else 30 s). Hub offline: nothing
queues; the next try waits (1 s doubling to 60 s). Never in the station's outbox, never in the state push.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Callable

from sizeup.cloud.client import HubError
from sizeup.radio import aisd
from sizeup.vessels import RX_KEYS, Fleet, ingest_messages

READ_S = 2.0
EVERY_S = (5.0, 30.0)          # the hub's "every", kept within these
BACKOFF_S = (1.0, 60.0)
PATH = "/api/station/vessels"


class VesselForwarder:
    """The station's thread: read, post; the stats for the station's status."""

    def __init__(self, cfg, client: Callable[[float], object], log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, status: Callable[[], dict | None] | None = None):
        self.cfg, self._client, self.log, self.clock = cfg, client, log, clock
        self.enabled = bool(getattr(cfg, "vessels_receiver", False))
        self.path = getattr(cfg, "vessels_receiver_json", "") or aisd.MESSAGES
        self.status = status or aisd.read_status
        self.fleet = Fleet(getattr(cfg, "vessels_bbox", None) or Fleet().bbox)
        self.every = EVERY_S[1]
        self.next_post = 0.0
        self.fails = 0
        self.posts = self.dropped_posts = 0
        self.vessels = 0
        self.last_ok: float | None = None
        self.last_error, self.error_since = "", None
        self.reading = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.c = None

    def read(self) -> bool:
        try:
            doc = json.loads(Path(self.path).read_bytes())
        except (OSError, ValueError) as e:
            self.reading = f"waiting for {self.path} (is sizeup-ais running?): {type(e).__name__}"
            return False
        ingest_messages(doc, self.fleet, "receiver")
        self.reading = self.path
        return True

    def rx(self) -> dict:
        try:
            st = self.status() or {}
        except Exception:  # noqa: BLE001
            st = {}
        return {k: st.get(k) for k in RX_KEYS if k in st}

    def step(self, now: float | None = None) -> str:
        """Read, and post if due: "posted" | "waiting" | "failed" | "off"."""
        now = self.clock() if now is None else now
        if not self.enabled:
            return "off"
        self.read()
        if now < self.next_post:
            return "waiting"
        recs = self.fleet.records(now)
        self.vessels = len(recs)
        body = {"v": 1, "at": now, "vessels": recs, "rx": self.rx()}
        try:
            if self.c is None:
                self.c = self._client(10.0)
            r = self.c.request("POST", PATH, body, timeout=10.0, gzip_body=True)
        except HubError as e:
            self.fails += 1
            self.dropped_posts += 1
            wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(self.fails - 1, 30))
            self.next_post = now + wait * random.uniform(0.9, 1.1)
            if self.error_since is None or self.fails in (1, 10) or self.fails % 100 == 0:
                self.log(f"vessels to the hub: {e}; dropped (live data isn't queued), next try in {wait:.0f} s")
            self.last_error, self.error_since = str(e)[:200], self.error_since or now
            return "failed"
        self.posts += 1
        if isinstance(r, dict):
            try:
                self.every = min(EVERY_S[1], max(EVERY_S[0], float(r.get("every") or EVERY_S[1])))
            except (TypeError, ValueError):
                self.every = EVERY_S[1]
        if self.error_since is not None:
            self.log(f"vessels to the hub: back after {now - self.error_since:.0f} s")
        self.fails, self.last_ok, self.last_error, self.error_since = 0, now, "", None
        self.next_post = now + self.every
        return "posted"

    def start(self) -> VesselForwarder:
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hub-vessels", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 - the forwarder must never die
                self.log(f"vessels to the hub: {type(e).__name__}: {e}")
            self._stop.wait(READ_S)
        if self.c is not None:
            try:
                self.c.close()
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> dict:
        now = self.clock()
        return {"enabled": self.enabled, "reading": self.reading, "every_s": self.every, "posts": self.posts,
                "dropped_posts": self.dropped_posts, "vessels": self.vessels, "last_ok": self.last_ok,
                "error": self.last_error, "error_since": self.error_since,
                "ok": self.last_ok is not None and now - self.last_ok < max(60.0, 3 * self.every) and not self.last_error}
