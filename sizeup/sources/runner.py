"""The station's side of the external incident sources (#297, the interface: sizeup/sources/__init__.py).

load_adapters() finds the adapters (entry points, then SIZEUP_INCIDENT_SOURCES); SourceRunner runs each on a thread of its
own and hands what changed to sink(kind, data):

    "source_event"   {source, label, rank, seen_at, first_seen, event: FeedEvent.as_dict()}: a new event, or one that
                     changed since it was last sent (the whole event: the hub works out what changed)
    "source_health"  {source, label, rank, state, reason, since, at}: on every change, and every HEARTBEAT_S

On a station the sink is the outbox to the hub (App._emit: durable, in order); all-in-one it is the CAD's Sources here. A
failing adapter never stops the others or the station: poll() raising is "failing" with its reason, and the runner
backs off (doubling poll_s up to MAX_BACKOFF_S) while the adapter's own health() still says what it says.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from sizeup.sources import (ENTRY_POINT_GROUP, ENV_MODULES, ENV_PATH, MAX_BACKOFF_S, MIN_POLL_S, RESERVED, SOURCE_ID,
                            Adapter, FeedEvent, Health, SourceContext, SourceError)

HEARTBEAT_S = 60.0


def _factories(env: dict | None = None) -> list[tuple[str, Callable]]:
    """(where, factory) for every entry point in ENTRY_POINT_GROUP and every module:factory in SIZEUP_INCIDENT_SOURCES."""
    env = os.environ if env is None else env
    out: list[tuple[str, Callable]] = []
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            out.append((f"entry point {ep.name}", ep.load))
    except Exception as e:  # noqa: BLE001 - broken metadata: the module paths still load
        out.append((f"entry points ({type(e).__name__}: {e})", None))
    for d in (env.get(ENV_PATH) or "").split(os.pathsep):
        if d.strip() and d.strip() not in sys.path:
            sys.path.insert(0, d.strip())
    for spec in (env.get(ENV_MODULES) or "").split(","):
        spec = spec.strip()
        if not spec:
            continue
        mod, _, attr = spec.partition(":")

        def load(mod=mod, attr=attr or "make"):
            return getattr(importlib.import_module(mod), attr)
        out.append((spec, load))
    return out


def load_adapters(ctx_for: Callable[[str], SourceContext], log: Callable[[str], None] = print,
                  env: dict | None = None) -> list[Adapter]:
    """Every adapter the station has, checked; one that can't load is logged and left out (never fatal)."""
    found: list[Adapter] = []
    seen: set[str] = set()
    for where, load in _factories(env):
        if load is None:
            log(f"incident sources: {where}")
            continue
        try:
            factory = load()
            made = factory(ctx_for(where))
            items = [made] if hasattr(made, "poll") else list(made)
        except Exception as e:  # noqa: BLE001 - an adapter's bug never stops the station
            log(f"incident sources: {where} not loaded: {type(e).__name__}: {e}")
            continue
        for a in items:
            why = check_adapter(a)
            if not why and a.source in seen:
                why = f"source {a.source!r} twice"
            if why:
                log(f"incident sources: {where}: {why}: left out")
                continue
            seen.add(a.source)
            found.append(a)
            log(f"incident sources: {a.label} ({a.source}, rank {a.rank}, every {a.poll_s:g} s) from {where}")
    return found


def check_adapter(a) -> str:
    """Why an adapter breaks the interface ('' when it doesn't)."""
    for k in ("source", "label", "rank", "poll_s", "poll", "health"):
        if not hasattr(a, k):
            return f"no {k}"
    if not isinstance(a.source, str) or not SOURCE_ID.fullmatch(a.source) or a.source in RESERVED:
        return f"source {a.source!r}: [a-z0-9][a-z0-9_-]{{0,31}}, not {', '.join(RESERVED)}"
    if not isinstance(a.label, str) or not a.label.strip():
        return "label: text"
    if not isinstance(a.rank, int) or isinstance(a.rank, bool) or not 1 <= a.rank <= 99:
        return "rank: 1 to 99"
    if not isinstance(a.poll_s, (int, float)) or isinstance(a.poll_s, bool) or a.poll_s <= 0:
        return "poll_s: seconds"
    return ""


class _Run:
    """One adapter's state in the runner."""

    def __init__(self, a: Adapter):
        self.a = a
        self.sent: dict[str, dict] = {}            # event_id -> the event as last sent
        self.first: dict[str, float] = {}          # event_id -> when first seen here
        self.health: tuple | None = None           # (state, reason) last sent
        self.health_at = 0.0
        self.error = ""                            # poll() raised: why
        self.fails = 0


class SourceRunner:
    def __init__(self, adapters: Iterable[Adapter], sink: Callable[[str, dict], None], log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time):
        self.runs = [_Run(a) for a in adapters]
        self.sink, self.log, self.clock = sink, log, clock
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def __bool__(self) -> bool:
        return bool(self.runs)

    # -- one step (the thread's loop calls it; tests call it directly) -------------------------------------------------

    def step(self, r: _Run) -> float:
        """Poll one adapter once, send what changed and its health. The seconds until the next poll."""
        a, now = r.a, self.clock()
        try:
            events = list(a.poll())
            r.error, r.fails = "", 0
        except Exception as e:  # noqa: BLE001 - the feed's trouble: "failing", never the station's
            events = []
            r.fails += 1
            r.error = f"{type(e).__name__}: {e}"[:200]
        for ev in events:
            self._event(r, ev, now)
        self._health(r, now)
        wait = max(MIN_POLL_S, float(a.poll_s))
        return min(MAX_BACKOFF_S, wait * (2 ** min(r.fails - 1, 10))) if r.fails else wait

    def _event(self, r: _Run, ev, now: float) -> None:
        a = r.a
        try:
            if not isinstance(ev, FeedEvent):
                raise SourceError(f"not a FeedEvent: {type(ev).__name__}")
            if ev.source != a.source:
                raise SourceError(f"source {ev.source!r} from the adapter {a.source!r}")
            d = ev.check().as_dict()
        except SourceError as e:
            self.log(f"incident sources: {a.label}: event skipped: {e}")
            return
        if r.sent.get(ev.event_id) == d:
            return
        first = r.first.setdefault(ev.event_id, now)
        self.sink("source_event", {"source": a.source, "label": a.label, "rank": a.rank, "seen_at": now,
                                   "first_seen": first, "event": d})
        r.sent[ev.event_id] = d

    def _health(self, r: _Run, now: float) -> None:
        a = r.a
        try:
            h = a.health().check()
        except Exception as e:  # noqa: BLE001
            h = Health("failing", f"health(): {type(e).__name__}: {e}"[:200])
        if r.error and h.state == "ok":                 # poll() raised though health() says ok: it isn't
            h = Health("failing", r.error)
        key = (h.state, h.reason)
        if key == r.health and now - r.health_at < HEARTBEAT_S:
            return
        if key != r.health:
            self.log(f"incident sources: {a.label}: {h.state.replace('_', ' ')}" + (f" ({h.reason})" if h.reason else ""))
        r.health, r.health_at = key, now
        self.sink("source_health", {"source": a.source, "label": a.label, "rank": a.rank, "state": h.state,
                                    "reason": h.reason[:200], "since": h.since, "at": now})

    # -- threads ---------------------------------------------------------------------------------------------------

    def _loop(self, r: _Run) -> None:
        while not self._stop.is_set():
            try:
                wait = self.step(r)
            except Exception as e:  # noqa: BLE001 - the sink failing: try again later
                self.log(f"incident sources: {r.a.label}: {type(e).__name__}: {e}")
                wait = MAX_BACKOFF_S
            self._stop.wait(wait)

    def start(self) -> SourceRunner:
        for r in self.runs:
            t = threading.Thread(target=self._loop, args=(r,), name=f"source-{r.a.source}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        for r in self.runs:
            close = getattr(r.a, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:  # noqa: BLE001
                    self.log(f"incident sources: {r.a.label}: close: {type(e).__name__}: {e}")

    def state(self) -> list[dict]:
        """The station page's rows: each adapter, its health as last sent and how many events it has seen."""
        return [{"source": r.a.source, "label": r.a.label, "rank": r.a.rank,
                 "state": (r.health or ("", ""))[0], "reason": (r.health or ("", ""))[1], "events": len(r.sent)}
                for r in self.runs]


def context_for(state_root: str | Path, log: Callable[[str], None], clock: Callable[[], float] = time.time):
    """ctx_for(where) for load_adapters: each adapter's state dir under state_root/sources/."""
    def make(where: str) -> SourceContext:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in where)[:64]
        return SourceContext(log=log, state_dir=str(Path(state_root) / "sources" / safe), clock=clock)
    return make
