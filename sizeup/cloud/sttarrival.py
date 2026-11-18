"""How long after its event each STT input reaches the hub (#677): so the hub's engines can tune how long they wait for
it (HubApp.read_stt_audio's wait_s) before taking the listening clip.

The STT input (sizeup/cloud/sttinput.py) travels on its own lane, sent only while no event waits on the station, so it
usually comes a moment after its traffic or page event. The delay is measured on the hub's clock alone:

    delay = when the STT input was stored  -  when its event was applied     (seconds)

It is negative when the STT input was here first (its event applied later: a hub that applies events after acking, a
resend): the true difference, not clamped, so p50 / p95 say how often waiting is needed at all. Each clip gives one
sample (a resent STT input or a replayed event is never counted twice); an event whose STT input never comes (an older
station, dropped, a channel not transcribed), or the other way round, gives none and is forgotten after PENDING_S.

    Arrival.event(cid)                         HubApp.apply: a traffic / page event applied
    Arrival.blob(cid, station, kind, channel)  HubApp.put_stt_input: its STT input stored
    Arrival.take()                             the samples since, as station-metrics records ({"k": "stt_in",
                                               "s": <channel>, "at", "delay", "kind": "page" | "traffic"}): HubApp
                                               adds them to the metrics store (sizeup/cloud/metricstore.py, kind "stt_in",
                                               rolled up hourly as "stt_delay": the Signal tab graphs it)
    stats(store, now)                          p50 / p95 per channel and per kind over the last hour and day, from the
                                               metrics store (Health: the hub's STT input arrival)
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from typing import Callable

KIND = "stt_in"                 # the metrics store's record kind ("stt" is the whisper queue's, #639)
PENDING_S = 3600.0              # an event or an STT input waiting this long for the other: forgotten (no sample)
PENDING_MAX = 20000             # ... and at most this many of each waiting (the oldest forgotten first)
DONE_MAX = 20000                # clips sampled already: a resend or a replay never counted twice
TAKE_MAX = 5000                 # samples not yet taken: at most this many (a store that can't take them)
WINDOWS = (("h1", 3600.0), ("d1", 86400.0))
STATS_MAX_DOCS = 50000          # one channel's day read at most this many samples
CHANNELS_MAX = 60               # Health's table: at most this many channels


def _pct(vals: list[float], p: float) -> float:
    """Nearest-rank percentile (as metricstore._pct)."""
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(math.ceil(p / 100.0 * len(s))) - 1))
    return s[k]


def summary(vals: list[float]) -> dict | None:
    """{n, p50, p95, early: how many negative (the STT input first)}; None with none."""
    if not vals:
        return None
    return {"n": len(vals), "p50": round(_pct(vals, 50), 2), "p95": round(_pct(vals, 95), 2),
            "early": sum(1 for v in vals if v < 0)}


class Arrival:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._events: OrderedDict[str, float] = OrderedDict()             # cid -> applied at (no STT input yet)
        self._blobs: OrderedDict[str, tuple] = OrderedDict()              # cid -> (stored at, station, kind, channel)
        self._done: OrderedDict[str, None] = OrderedDict()
        self._out: list[tuple[str, dict]] = []
        self.samples = 0
        self.lost = 0                                                     # samples not taken in time (TAKE_MAX)

    def _expire(self, now: float) -> None:
        for d in (self._events, self._blobs):
            while d and (len(d) > PENDING_MAX or now - _at(next(iter(d.values()))) > PENDING_S):
                d.popitem(last=False)

    def _sample(self, cid: str, event_at: float, blob: tuple) -> None:
        stored, station, kind, channel = blob
        self._done[cid] = None
        while len(self._done) > DONE_MAX:
            self._done.popitem(last=False)
        if len(self._out) >= TAKE_MAX:
            self.lost += 1
            return
        self.samples += 1
        self._out.append((station, {"k": KIND, "s": channel or "?", "at": round(max(stored, event_at), 3),
                                    "delay": round(stored - event_at, 3), "kind": kind}))

    def event(self, cid: str, at: float | None = None) -> None:
        """Its traffic / page event was applied (the first time only)."""
        now = self.clock() if at is None else at
        with self._lock:
            if cid in self._done or cid in self._events:
                return
            blob = self._blobs.pop(cid, None)
            if blob is not None:
                self._sample(cid, now, blob)                              # the STT input came first: negative
                return
            self._events[cid] = now
            self._expire(now)

    def blob(self, cid: str, station: str, kind: str, channel: str, at: float | None = None) -> None:
        """Its STT input was stored (the first time only)."""
        now = self.clock() if at is None else at
        with self._lock:
            if cid in self._done or cid in self._blobs:
                return
            b = (now, str(station or "station"), "page" if kind == "page" else "traffic", str(channel or ""))
            ev = self._events.pop(cid, None)
            if ev is not None:
                self._sample(cid, ev, b)
                return
            self._blobs[cid] = b
            self._expire(now)

    def take(self) -> dict[str, list[dict]]:
        """The samples since the last take, by station (metrics-store records)."""
        with self._lock:
            out, self._out = self._out, []
        by: dict[str, list[dict]] = {}
        for station, rec in out:
            by.setdefault(station, []).append(rec)
        return by

    def pending(self) -> dict:
        with self._lock:
            return {"events": len(self._events), "blobs": len(self._blobs), "untaken": len(self._out)}


def _at(v) -> float:
    return v[0] if isinstance(v, tuple) else v


def stats(store, now: float | None = None) -> dict | None:
    """p50 / p95 of the arrival delay over the last hour (h1) and day (d1): per kind (page / traffic) and per channel,
    from the metrics store's "stt_in" records. None when there are none in the day."""
    now = time.time() if now is None else now
    t0 = now - WINDOWS[-1][1]
    per: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for s in store.index().get("series") or []:
        if s.get("kind") != KIND or (s.get("last") or 0) < t0:
            continue
        for d in store.docs(s["station"], s["subject"], KIND, t0, now + 60, STATS_MAX_DOCS, newest=True):
            v = d.get("delay")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                kind = "page" if d.get("kind") == "page" else "traffic"
                per.setdefault((s["subject"], kind), []).append((d["at"], float(v)))
    if not per:
        return None

    def windows(pts: list[tuple[float, float]]) -> dict:
        return {name: summary([v for at, v in pts if at >= now - span]) for name, span in WINDOWS}

    kinds: dict[str, list] = {}
    for (_ch, kind), pts in per.items():
        kinds.setdefault(kind, []).extend(pts)
    channels = [dict(channel=ch, kind=kind, **windows(pts)) for (ch, kind), pts in per.items()]
    channels.sort(key=lambda c: (c["kind"] != "page", -((c["d1"] or {}).get("p95") or 0.0), c["channel"]))
    return {"at": now, "kinds": {k: windows(v) for k, v in sorted(kinds.items())},
            "channels": channels[:CHANNELS_MAX], "more": max(0, len(channels) - CHANNELS_MAX)}
