"""Our ADS-B receiver's aircraft, from the station to the hub (GitHub #235; the piece left from #155 / #88).

The station's decoder (sizeup-adsb.service, sizeup/radio/adsbd.py) writes aircraft.json every second. This reads it
every READ_S and POSTs the hub /api/station/aircraft (the station token, like every /api/station/ call) with what
changed, gzipped: one compact list per aircraft (sizeup/aircraft.py WIRE: ICAO address, lat / lon to 5 decimals, altitude,
ground speed, track, vertical rate, squawk, emergency, callsign, the position's and the last message's age, signal, ...).
The hub keeps them as the aircraft layer's "receiver" feed (StationFeed): our positions first while fresh, adsb.lol /
adsb.fi when we lose a plane (#169's rules).

Bounded: live data, so stale is useless.
  * Only what the hub's map and emergency watch can use: aircraft with a position within FORWARD_AREA x the radius of HPN
    (what the hub's track store keeps) and below FORWARD_CEILING_FT (or on the ground); at most WIRE_MAX, the nearest.
  * Only what changed: an aircraft whose position, altitude, speed, track, squawk ... are as last sent goes again only
    every REFRESH_S (so the hub knows it is still heard); one that is gone is listed once in "gone".
  * As often as the hub says (its answer's "every"): FORWARD_FAST_S (1 s) while someone has the map open or an
    emergency is on, FORWARD_SLOW_S (5 s) otherwise. An aircraft whose squawk turns to an emergency goes at once.
  * Hub offline: nothing queues. The delta is dropped, the next try waits (1 s doubling to 30 s), and the first post
    that gets through is a full list ("full": the hub starts afresh). The same when the hub says it missed one.
  * Never in the station's outbox (that is for events that must arrive) and never in any state push (#146).

What it costs (tests/test_adsb_receiver.py test_what_forwarding_costs, on the recorded traffic): about 55 bytes per
aircraft per post gzipped (the recorded 10 moving aircraft: 550 bytes, 1.1 KB as plain JSON), plus the request's
headers (~0.5 KB with the Access headers). So 10 to 20 moving aircraft near HPN are about 1 to 1.6 KB/s while someone
looks at the map, a fifth of that otherwise; parked and unchanged aircraft cost a resend every REFRESH_S only.
"""

from __future__ import annotations

import os
import random
import threading
import time
from pathlib import Path
from typing import Callable

from sizeup.aircraft import (ARP, EMERGENCY_SQUAWKS, FORWARD_AREA, FORWARD_CEILING_FT, FORWARD_FAST_S, FORWARD_SLOW_S,
                             RADIUS_NM, WIRE_MAX, ReceiverFeed, Snapshot, http_get, nm, wire)
from sizeup.cloud.client import HubError
from sizeup.radio import adsbd

READ_S = 1.0
REFRESH_S = 5.0                # an unchanged aircraft still heard: sent again this often
BACKOFF_S = (1.0, 30.0)        # the hub didn't answer: the next try after this (doubling)
RX_EVERY_S = 30.0              # the receiver's own status ("rx") goes up when it changes, or this often
PATH = "/api/station/aircraft"


def _emergency(rec: list) -> bool:
    return rec[7] in EMERGENCY_SQUAWKS or bool(rec[8])


class Deltas:
    """What to send for each new snapshot (no network here: AircraftForwarder posts it)."""

    def __init__(self, radius_nm: float = RADIUS_NM, cap: int = WIRE_MAX):
        self.area_nm = radius_nm * FORWARD_AREA
        self.cap = cap
        self.sent: dict[str, tuple[tuple, float]] = {}    # hex -> (what was sent, when)
        self.full = True                                  # the next one is everything (start, or after a failure)
        self.dropped = 0                                  # aircraft over the cap in the last snapshot
        self.count = 0                                    # aircraft the hub can use in the last snapshot

    def reset(self) -> None:
        self.sent.clear()
        self.full = True

    def wanted(self, snap: Snapshot) -> list:
        """The aircraft the hub can use, nearest first, at most cap: [(distance, wire record)]."""
        out = []
        for p in snap.planes:
            if not p.has_position:
                continue
            alt = p.altitude_ft
            if alt is not None and alt > FORWARD_CEILING_FT:
                continue
            d = nm(ARP[0], ARP[1], p.lat, p.lon)
            if d > self.area_nm:
                continue
            out.append((d, wire(p)))
        out.sort(key=lambda x: x[0])
        self.dropped = max(0, len(out) - self.cap)
        self.count = min(len(out), self.cap)
        return out[:self.cap]

    @staticmethod
    def _key(rec: list, snap_now: float) -> tuple:
        """What counts as a change: every field but the ages and the signal, plus when the position was taken."""
        pos_at = None if rec[10] is None else round(snap_now - rec[10], 1)
        return tuple(rec[1:10]) + tuple(rec[13:]) + (pos_at,)

    def build(self, snap: Snapshot, now: float, due: bool) -> tuple[dict | None, bool]:
        """(the post's {full, ac, gone} or None when there is nothing to send, urgent: an emergency changed). due: the
        hub's interval has passed (else only an urgent change goes)."""
        recs = self.wanted(snap)
        here = {r[0] for _d, r in recs}
        if self.full:                                      # everything, when due (a failure's backoff holds it too)
            return ({"full": True, "ac": [r for _d, r in recs], "gone": []} if due else None), False
        ac, urgent = [], False
        for _d, r in recs:
            k = self._key(r, snap.now)
            have = self.sent.get(r[0])
            changed = have is None or have[0] != k
            if have is not None and _emergency(r) and (have[0][6:8] != k[6:8]):
                urgent = True                              # its squawk / emergency just changed to one
            elif have is None and _emergency(r):
                urgent = True
            if changed or now - have[1] >= REFRESH_S:
                ac.append(r)
        gone = [h for h in self.sent if h not in here]
        if not (due or urgent) or not (ac or gone):
            return None, urgent
        return {"full": False, "ac": ac, "gone": gone}, urgent

    def sent_ok(self, body: dict, snap: Snapshot, now: float) -> None:
        if body.get("full"):
            self.sent.clear()
        for r in body["ac"]:
            self.sent[r[0]] = (self._key(r, snap.now), now)
        for h in body["gone"]:
            self.sent.pop(h, None)
        self.full = False


class AircraftForwarder:
    """The station's thread: read, build, post; the stats for the station's status and health."""

    def __init__(self, app, cfg, client: Callable[[float], object], log: Callable[[str], None] = print,
                 source: Callable[[], Snapshot | None] | None = None, clock: Callable[[], float] = time.time):
        self.app, self.cfg, self._client, self.log, self.clock = app, cfg, client, log, clock
        self.deltas = Deltas(getattr(cfg, "aircraft_radius_nm", RADIUS_NM))
        self.source = source or self._read
        self.epoch = os.urandom(6).hex()
        self.seq = -1
        self.every = FORWARD_SLOW_S
        self.next_post = 0.0
        self.fails = 0
        self.last_ok: float | None = None
        self.last_error = ""
        self.error_since: float | None = None
        self.posts = self.bytes = self.dropped_posts = 0
        self.aircraft = 0
        self.last_rx: tuple[dict, float] | None = None
        self.reading = ""                    # where it reads, or why not ("" before the first look)
        self._feed: ReceiverFeed | None = None
        self._feed_src: tuple = ()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.c = None

    # -- the source -----------------------------------------------------------------------------------------------

    def sources(self) -> tuple[str, str]:
        """(1090, 978): [aircraft.receiver] json_1090 / json_978 as configured, else the decoder's aircraft.json once a
        dongle is named for ADS-B (the config's, or Settings')."""
        c = self.cfg
        s1090, s978 = getattr(c, "aircraft_receiver_1090", ""), getattr(c, "aircraft_receiver_978", "")
        if not (s1090 or s978) and getattr(self.app, "adsb_sdr", ""):
            s1090 = adsbd.AIRCRAFT_JSON
        return s1090, s978

    def _read(self) -> Snapshot | None:
        src = self.sources()
        if not any(src):
            self.reading = "no ADS-B receiver here (no dongle named for it)"
            return None
        if src != self._feed_src:
            self._feed, self._feed_src = ReceiverFeed(src[0], src[1], http_get), src
        if src[0] and "://" not in src[0] and not Path(src[0]).exists() and not src[1]:
            self.reading = f"waiting for {src[0]} (is the decoder running?)"
            return None
        try:
            snap = self._feed.read()
        except Exception as e:  # noqa: BLE001 - a decoder that stopped: say so, try again next second
            self.reading = str(e)[:200]
            return None
        self.reading = " + ".join(x for x in src if x)
        return snap

    def rx(self) -> dict:
        """The receiver as the station sees it (its decoder's status), for the hub's map box and Settings."""
        try:
            row = self.app.adsb_row() if hasattr(self.app, "adsb_row") else {}
        except Exception:  # noqa: BLE001
            row = {}
        dec = row.get("decoder") or {}
        return {"sdr": row.get("serial", ""), "name": row.get("name", ""), "present": row.get("present"),
                "held_by": row.get("held_by"), "state": dec.get("state") or ("off" if not row.get("serial") else ""),
                "detail": dec.get("detail") or self.reading, "messages_per_s": dec.get("messages_per_s"),
                "aircraft": dec.get("aircraft"), "last_message_at": dec.get("last_message_at"),
                "gain": row.get("gain"), "bias_tee": row.get("bias_tee"), "source": row.get("source"),
                "cause": dec.get("cause") or "", "holder": dec.get("holder") or ""}

    # -- one turn -------------------------------------------------------------------------------------------------

    def step(self, now: float | None = None) -> str:
        """Read, and post if due: "posted" | "nothing" | "waiting" | "failed" | "no source"."""
        now = self.clock() if now is None else now
        snap = self.source()
        if snap is None:
            if self.deltas.sent:                    # the receiver went away: the hub drops them by its own clock
                self.deltas.reset()
            return "no source"
        due = now >= self.next_post
        body, _urgent = self.deltas.build(snap, now, due)
        self.aircraft = self.deltas.count
        if body is None:
            return "nothing" if due else "waiting"
        self.seq += 1
        body.update(v=1, epoch=self.epoch, seq=self.seq, at=now)
        rx = self.rx()
        if self.last_rx is None or self.last_rx[0] != rx or now - self.last_rx[1] >= RX_EVERY_S or body["full"]:
            body["rx"] = rx
        try:
            if self.c is None:
                self.c = self._client(10.0)
            r = self.c.request("POST", PATH, body, timeout=10.0, gzip_body=True)
        except HubError as e:
            self.fails += 1
            self.dropped_posts += 1
            wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** (self.fails - 1))
            self.next_post = now + wait * random.uniform(0.9, 1.1)
            if self.error_since is None or self.fails in (1, 10) or self.fails % 100 == 0:
                self.log(f"aircraft to the hub: {e}; dropped (live data isn't queued), next try in {wait:.0f} s")
            self.last_error, self.error_since = str(e)[:200], self.error_since or now
            self.deltas.reset()                     # the hub may have missed it: everything, next time
            return "failed"
        self.posts += 1
        self.bytes += int(getattr(self.c, "last_sent", 0) or 0)
        self.deltas.sent_ok(body, snap, now)
        if "rx" in body:
            self.last_rx = (rx, now)
        if isinstance(r, dict):
            if r.get("full"):
                self.deltas.full = True
            try:
                self.every = min(FORWARD_SLOW_S * 2, max(FORWARD_FAST_S, float(r.get("every") or FORWARD_SLOW_S)))
            except (TypeError, ValueError):
                self.every = FORWARD_SLOW_S
        if self.error_since is not None:
            self.log(f"aircraft to the hub: back after {now - self.error_since:.0f} s")
        self.fails, self.last_ok, self.last_error, self.error_since = 0, now, "", None
        self.next_post = now + self.every
        return "posted"

    # -- the thread -----------------------------------------------------------------------------------------------

    def start(self) -> AircraftForwarder:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hub-aircraft", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = self.clock()
            try:
                self.step(t0)
            except Exception as e:  # noqa: BLE001 - the forwarder must never die
                self.log(f"aircraft to the hub: {type(e).__name__}: {e}")
            self._stop.wait(max(0.05, READ_S - (self.clock() - t0)))
        if self.c is not None:
            try:
                self.c.close()
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> dict:
        now = self.clock()
        return {"reading": self.reading, "every_s": self.every, "posts": self.posts, "bytes": self.bytes,
                "dropped_posts": self.dropped_posts, "aircraft": self.aircraft, "over_cap": self.deltas.dropped,
                "last_ok": self.last_ok, "error": self.last_error, "error_since": self.error_since,
                "ok": self.last_ok is not None and now - self.last_ok < max(15.0, 3 * self.every) and not self.last_error}
