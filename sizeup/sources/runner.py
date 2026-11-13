"""The station's side of the external incident sources (#297, the interface: sizeup/sources/__init__.py).

load_adapters() finds the adapters (entry points, then SIZEUP_INCIDENT_SOURCES); SourceRunner runs each on a thread of its
own and hands what changed to sink(kind, data):

    "source_event"   {source, label, rank, seen_at, first_seen, event: FeedEvent.as_dict()}: a new event, or one that
                     changed since it was last sent (the whole event: the hub works out what changed)
    "source_health"  {source, label, rank, state, reason, since, at, stopped}: on every change, and every HEARTBEAT_S.
                     stopped: the adapter's own health() says "failing" (the hub shows it at once, no hysteresis)
    "source_seen"    {source, label, rank, at, ids, complete}: the event ids in the last good poll's snapshot, every
                     SEEN_EVERY_S (the hub flags an open report the feed no longer lists)

On a station the sink is the outbox to the hub (App._emit: durable, in order); all-in-one it is the CAD's Sources here. A
failing adapter never stops the others or the station: poll() raising is "backing_off" with its reason ("failing" from
the RAISE_FAILING_AFTER-th raise in a row: one timeout is no outage), and the runner
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

from sizeup.sources import (ENTRY_POINT_GROUP, ENV_MODULES, ENV_PATH, MAX_BACKOFF_S, MAX_SEEN_IDS, MIN_POLL_S,
                            RAISE_FAILING_AFTER, RESERVED,
                            SEEN_EVERY_S, SOURCE_ID, TRIGGER_MAX, TRIGGER_MIN_S, TRIGGER_WINDOW_S, Adapter, FeedEvent,
                            Health, SourceContext, SourceError)
from sizeup.sources.resets import Resets      # #297 owns it; #305's sizeup/cloud/adapters.py imports it from here

HEARTBEAT_S = 60.0
PRUNE_S = 6 * 3600.0          # an event no poll has had this long is forgotten here
PRUNE_MAX = 5000              # ... and at most this many are remembered per adapter


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


class HubRefused(RuntimeError):
    """Adapters run on the station only: the hub never discovers, imports or runs one (it gets their events through the
    station's outbox, and the adapters' credentials never reach it)."""


def require_station(role: str) -> None:
    """HubRefused on the hub ([cloud] role = "hub"); a station, or all-in-one (the station's machine with no hub)."""
    if (role or "").strip().lower() == "hub":
        raise HubRefused("incident-source adapters run on the station only, never on the hub")


def load_adapters(ctx_for: Callable[[str], SourceContext], log: Callable[[str], None] = print,
                  env: dict | None = None, role: str = "station") -> list[Adapter]:
    """Every adapter the station has, checked; one that can't load is logged and left out (never fatal). HubRefused
    on the hub, before any entry point or module is looked at."""
    require_station(role)
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
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001 - an adapter's bug (even a SystemExit) never stops the station
            log(f"incident sources: {where} not loaded: {type(e).__name__}")
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
        self.wake = threading.Event()              # trigger(): poll now
        self.reason = ""                           # ... and why (poll_now is called on the adapter's own thread)
        self.triggered = 0.0                       # the last trigger taken
        self.triggers: list[float] = []            # ... and those within TRIGGER_WINDOW_S (at most TRIGGER_MAX)
        self.polled: float | None = None           # when poll() was last called (a triggered poll waits TRIGGER_MIN_S)
        self.last_in: dict[str, float] = {}        # event_id -> when it was last in a poll (sent is pruned by it)
        self.snapshot: list[str] = []              # the event ids in the last good poll
        self.seen_at = 0.0                         # when "source_seen" was last sent
        self.target = None                         # where the sink sent them (see SourceRunner.target)


class SourceRunner:
    def __init__(self, adapters: Iterable[Adapter], sink: Callable[[str, dict], None], log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, role: str = "station", target: Callable[[], object] | None = None):
        require_station(role)
        self.runs = [_Run(a) for a in adapters]
        self.sink, self.log, self.clock = sink, log, clock
        # Where the sink sends them now (the app: forwarding to the hub or not). When it changes, everything is sent again:
        # what went to the local CAD while station mode was Off never reached the hub
        self.target = target
        # A remote reset (#305: an adapter that stopped itself): queued here from anywhere,
        # run by that adapter's own thread before its next poll (never the job's thread)
        self.resets = Resets()
        self.resets.wake = self._wake_source
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def __bool__(self) -> bool:
        return bool(self.runs)

    # -- one step (the thread's loop calls it; tests call it directly) -------------------------------------------------

    def step(self, r: _Run) -> float:
        """Poll one adapter once, send what changed and its health. The seconds until the next poll."""
        a, now = r.a, self.clock()
        self._retarget(r)
        r.polled = now
        try:
            events = list(a.poll())
            r.error, r.fails = "", 0
            ok = True
        except Exception as e:  # noqa: BLE001 - the feed's trouble (backing off, then failing), never the station's
            events, ok = [], False
            r.fails += 1
            r.error = type(e).__name__              # the type only: its text may hold a URL or a credential
        ids = []
        for ev in events:
            try:
                if self._event(r, ev, now):
                    ids.append(ev.event_id)
            except Exception as e:  # noqa: BLE001 - one bad event never drops the rest
                self.log(f"incident sources: {a.label}: event skipped ({type(e).__name__})")
        if ok:
            r.snapshot = ids
            self._seen(r, now)
        self._prune(r, now)
        self._health(r, now)
        wait = max(MIN_POLL_S, float(a.poll_s))
        return min(MAX_BACKOFF_S, wait * (2 ** min(r.fails - 1, 10))) if r.fails else wait

    def _event(self, r: _Run, ev, now: float) -> bool:
        """One event of a poll: sent when new or changed. False when it breaks the interface (skipped)."""
        a = r.a
        try:
            if not isinstance(ev, FeedEvent):
                raise SourceError(f"not a FeedEvent: {type(ev).__name__}")
            if ev.source != a.source:
                raise SourceError(f"source {ev.source!r} from the adapter {a.source!r}")
            d = ev.check().as_dict()
        except SourceError as e:
            self.log(f"incident sources: {a.label}: event skipped: {e}")
            return False
        r.last_in[ev.event_id] = now
        if r.sent.get(ev.event_id) == d:
            return True
        first = r.first.setdefault(ev.event_id, now)
        self.sink("source_event", {"source": a.source, "label": a.label, "rank": a.rank, "seen_at": now,
                                   "first_seen": first, "event": d})
        r.sent[ev.event_id] = d
        return True

    def _seen(self, r: _Run, now: float) -> None:
        """Every SEEN_EVERY_S: the ids in the last good snapshot ("source_seen"), so the hub can flag an open report
        the feed no longer lists. complete: False when the list was cut at MAX_SEEN_IDS (the hub flags nothing then)."""
        if now - r.seen_at < SEEN_EVERY_S:
            return
        a = r.a
        self.sink("source_seen", {"source": a.source, "label": a.label, "rank": a.rank, "at": now,
                                  "ids": r.snapshot[:MAX_SEEN_IDS], "complete": len(r.snapshot) <= MAX_SEEN_IDS})
        r.seen_at = now

    def _retarget(self, r: _Run) -> None:
        """The sink sends somewhere else now (station mode turned on or off): everything again, health included."""
        if self.target is None:
            return
        try:
            t = self.target()
        except Exception:  # noqa: BLE001
            return
        if t != r.target:
            if r.target is not None:
                r.sent.clear()
                r.health, r.seen_at = None, 0.0
            r.target = t

    @staticmethod
    def _prune(r: _Run, now: float) -> None:
        """Events no poll has had for PRUNE_S are forgotten here (one coming back is sent again: the hub sees no change)."""
        old = [k for k, t in r.last_in.items() if now - t > PRUNE_S]
        if len(r.last_in) - len(old) > PRUNE_MAX:
            old += sorted((k for k in r.last_in if k not in old), key=r.last_in.get)[:len(r.last_in) - len(old) - PRUNE_MAX]
        for k in old:
            r.last_in.pop(k, None)
            r.sent.pop(k, None)
            r.first.pop(k, None)

    def _health(self, r: _Run, now: float) -> None:
        a = r.a
        stopped = False
        try:
            h = a.health().check()
            stopped = h.state == "failing"              # the adapter itself says so (it stopped): shown at once
        except Exception as e:  # noqa: BLE001
            h = Health("failing", f"health(): {type(e).__name__}")
        if r.error and h.state == "ok":                 # poll() raised though health() says ok: it isn't
            h = Health("failing" if r.fails >= RAISE_FAILING_AFTER else "backing_off", r.error)
        key = (h.state, h.reason)
        if key == r.health and now - r.health_at < HEARTBEAT_S:
            return
        if key != r.health:
            self.log(f"incident sources: {a.label}: {h.state.replace('_', ' ')}" + (f" ({h.reason})" if h.reason else ""))
        r.health, r.health_at = key, now
        self.sink("source_health", {"source": a.source, "label": a.label, "rank": a.rank, "state": h.state,
                                    "reason": h.reason[:200], "since": h.since, "at": now, "stopped": stopped})

    # -- threads ---------------------------------------------------------------------------------------------------

    def trigger(self, reason: str) -> int:
        """Something that may be a call coming (paging tones, a radio or phone pre-alert): each adapter's optional
        earlier poll. Gentle whatever wakes it (tone pairs, pre-alerts and phone rings come in bursts): at most one per
        max(poll_s, TRIGGER_MIN_S) per adapter, at most TRIGGER_MAX per TRIGGER_WINDOW_S, the poll itself never sooner
        than TRIGGER_MIN_S after the last one (_loop), and none while it is failing or backing off (the adapter's own
        pacing stands during an outage). Only sets a flag and the reason: never runs adapter code here (the caller may
        hold the paging decoder's lock); the adapter's own thread calls poll_now(reason) before that poll. How many
        were woken."""
        now, n = self.clock(), 0
        for r in self.runs:
            if now - r.triggered < max(TRIGGER_MIN_S, float(r.a.poll_s)):
                continue
            if r.fails or (r.health is not None and r.health[0] != "ok"):
                continue                       # an outage: the adapter's own pacing (and back-off) stands
            r.triggers = [t for t in r.triggers if now - t < TRIGGER_WINDOW_S]
            if len(r.triggers) >= TRIGGER_MAX:
                continue
            r.triggers.append(now)
            r.triggered, r.reason = now, str(reason or "")[:40]
            r.wake.set()
            n += 1
        return n

    def _wake_source(self, source: str) -> None:
        for r in self.runs:
            if r.a.source == source:
                r.wake.set()

    def _reset_due(self, r: _Run) -> None:
        """On the adapter's own thread: a reset asked for it runs now, and its back-off starts over."""
        try:
            if self.resets.run_due(r.a, self.log) == "reset":
                r.fails, r.error = 0, ""
        except Exception as e:  # noqa: BLE001
            self.log(f"incident sources: {r.a.label}: reset: {type(e).__name__}")

    def _poll_now(self, r: _Run) -> None:
        """On the adapter's own thread: its poll_now(reason), when it was triggered."""
        reason, r.reason = r.reason, ""
        hook = getattr(r.a, "poll_now", None)
        if reason and callable(hook):
            try:
                hook(reason)
            except Exception as e:  # noqa: BLE001
                self.log(f"incident sources: {r.a.label}: poll_now: {type(e).__name__}")

    def hold(self, r: _Run) -> float:
        """Seconds a triggered poll still waits: never sooner than TRIGGER_MIN_S after the last poll."""
        return 0.0 if r.polled is None else max(0.0, TRIGGER_MIN_S - (self.clock() - r.polled))

    def _loop(self, r: _Run) -> None:
        while not self._stop.is_set():
            r.wake.clear()
            if r.reason and (h := self.hold(r)) > 0 and self._stop.wait(h):
                break
            self._reset_due(r)
            self._poll_now(r)
            try:
                wait = self.step(r)
            except Exception as e:  # noqa: BLE001 - the sink failing: try again later
                self.log(f"incident sources: {r.a.label}: {type(e).__name__}")
                wait = MAX_BACKOFF_S
            r.wake.wait(wait)

    def start(self) -> SourceRunner:
        for r in self.runs:
            t = threading.Thread(target=self._loop, args=(r,), name=f"source-{r.a.source}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        for r in self.runs:
            r.wake.set()
            close = getattr(r.a, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:  # noqa: BLE001
                    self.log(f"incident sources: {r.a.label}: close: {type(e).__name__}")


def context_for(state_root: str | Path, log: Callable[[str], None], clock: Callable[[], float] = time.time):
    """ctx_for(where) for load_adapters: each adapter's state dir under state_root/sources/."""
    def make(where: str) -> SourceContext:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in where)[:64]
        return SourceContext(log=log, state_dir=str(Path(state_root) / "sources" / safe), clock=clock)
    return make
