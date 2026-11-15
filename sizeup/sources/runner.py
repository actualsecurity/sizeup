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

How often poll() is called (every()): max(MIN_POLL_S, poll_s), the gentle floor, for every adapter but one whose poll()
only reads memory (local_poll(): the adapter's own local_poll = True, or its source id in SIZEUP_INCIDENT_SOURCES_SNAPSHOT):
that one is read every max(SNAPSHOT_MIN_S, poll_s), so a new event is handed on within poll_s of the adapter having it.
Only the reads are faster. Its health() is still asked at the floor's pace, its poll_now() comes no sooner or oftener
than before (hold(): measured from the floor-paced reads, not every read), a poll() that raises backs off from the
floor, and a read that finds nothing changed sends, writes and logs nothing.

Two clocks: clock() (wall, the station's) for every time that is sent (seen_at, first_seen, at), and mono() for every
interval the runner keeps (health's pace, heartbeats, "seen" lists, triggers and their hold, skipped-event lines,
pruning), so a wall clock stepped back or forward never silences a source's health or holds a trigger.
"""

from __future__ import annotations

import importlib
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from sizeup.sources import (ENTRY_POINT_GROUP, ENV_MODULES, ENV_PATH, ENV_SNAPSHOT, FIRST_SEEN_MAX_S, MAX_BACKOFF_S,
                            MAX_SEEN_IDS, MIN_POLL_S, RAISE_FAILING_AFTER, RESERVED,
                            NOT_TRIGGERS, SEEN_EVERY_S, SNAPSHOT_MIN_S, SNAPSHOT_RECHECK_S, SOURCE_ID, TRIGGER_MAX, TRIGGER_MIN_S,
                            TRIGGER_WINDOW_S, Adapter, FeedEvent, Health, SourceContext, SourceError)
from sizeup.sources.resets import Resets      # #297 owns it; #305's sizeup/cloud/adapters.py imports it from here

HEARTBEAT_S = 60.0
PRUNE_S = 6 * 3600.0          # an event no poll has had this long is forgotten here
PRUNE_MAX = 5000              # ... and at most this many are remembered per adapter
SKIP_LOGS = 50                # a memory-read adapter's "event skipped" lines: this many different ones remembered
NEVER = float("-inf")         # a pacing time not yet set (the monotonic clock may start near 0 at boot)


def snapshot_sources(env: dict | None = None) -> frozenset[str]:
    """The source ids SIZEUP_INCIDENT_SOURCES_SNAPSHOT names: the admin says their poll() only reads memory."""
    env = os.environ if env is None else env
    return frozenset(x.strip() for x in (env.get(ENV_SNAPSHOT) or "").split(",") if x.strip())


def local_poll(a, named: Iterable[str] = ()) -> str:
    """Who says this adapter's poll() only reads memory, no request made ('' when nobody does: the floor stands). An
    adapter that has local_poll at all decides: only exactly True lifts the floor, and anything else (False included)
    keeps it whatever SIZEUP_INCIDENT_SOURCES_SNAPSHOT says. Only one without the attribute can be named there."""
    try:
        if not hasattr(a, "local_poll"):
            return ENV_SNAPSHOT if a.source in named else ""
        return "its local_poll" if getattr(a, "local_poll") is True else ""
    except Exception:  # noqa: BLE001 - an attribute that raises says nothing: the floor stands
        return ""


def every(a, named: Iterable[str] = ()) -> float:
    """The seconds between poll() calls: poll_s, never under MIN_POLL_S; a memory read never under SNAPSHOT_MIN_S."""
    return max(SNAPSHOT_MIN_S if local_poll(a, named) else MIN_POLL_S, float(a.poll_s))


def pace(a, named: Iterable[str] = ()) -> str:
    """How often poll() is really called, for the log: "every 30 s", "every 30 s (the floor; poll_s 5 s)", "memory
    read every 5 s (its local_poll)"."""
    s, why = every(a, named), local_poll(a, named)
    if why:
        return f"memory read every {s:g} s ({why})"
    return f"every {s:g} s" + ("" if s == float(a.poll_s) else f" (the floor; poll_s {float(a.poll_s):g} s)")


def first_seen(ev, now: float) -> float:
    """When the event was first had: the adapter's own time when it gives a believable one (a time, not later than now,
    at most FIRST_SEEN_MAX_S old; both are the station's clock), else now (the hand-off)."""
    v = getattr(ev, "first_seen", None)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= now - v <= FIRST_SEEN_MAX_S:
        return float(v)
    return now


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
    named = snapshot_sources(env)
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
            log(f"incident sources: {a.label} ({a.source}, rank {a.rank}, {pace(a, named)}) from {where}")
    for x in sorted(named - seen):
        log(f"incident sources: {ENV_SNAPSHOT} names {x[:32]!r}: no such source")
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

    def __init__(self, a: Adapter, named: Iterable[str] = ()):
        self.a = a
        self.fast = bool(local_poll(a, named))     # poll() only reads memory: read every poll_s, under the floor
        self.every = every(a, named)               # the seconds between polls
        # Every interval below is on the runner's monotonic clock (SourceRunner.mono), never the wall clock
        self.asked = NEVER                         # when health() was last asked (fast: at the floor's pace)
        self.skips: dict[str, float] = {}          # fast: "event skipped" line -> when last logged
        self.sent: dict[str, dict] = {}            # event_id -> the event as last sent
        self.first: dict[str, float] = {}          # event_id -> when first seen here
        self.health: tuple | None = None           # (state, reason) last sent
        self.health_at = NEVER
        self.error = ""                            # poll() raised: why
        self.fails = 0
        self.wake = threading.Event()              # trigger(): poll now
        self.reason = ""                           # ... and why (poll_now is called on the adapter's own thread)
        self.triggered = NEVER                     # the last trigger taken
        self.triggers: list[float] = []            # ... and those within TRIGGER_WINDOW_S (at most TRIGGER_MAX)
        # When poll() was last called (a triggered poll waits TRIGGER_MIN_S). fast: the last read at the floor's pace
        # (MIN_POLL_S after the one before) or after poll_now(), never every read: triggers wait as they always did
        self.polled: float | None = None
        self.last_in: dict[str, float] = {}        # event_id -> when it was last in a poll (sent is pruned by it)
        self.snapshot: list[str] = []              # the event ids in the last good poll
        self.seen_at = NEVER                       # when "source_seen" was last sent
        self.target = None                         # where the sink sent them (see SourceRunner.target)


class SourceRunner:
    def __init__(self, adapters: Iterable[Adapter], sink: Callable[[str, dict], None], log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, role: str = "station", target: Callable[[], object] | None = None,
                 env: dict | None = None, mono: Callable[[], float] | None = None):
        """clock: the wall clock, for the times sent. mono: the intervals kept here; time.monotonic by default, the
        clock given when it is not time.time (a test's fake clock paces both)."""
        require_station(role)
        named = snapshot_sources(env)              # the same environment load_adapters logged the pace from
        self.runs = [_Run(a, named) for a in adapters]
        self.sink, self.log, self.clock = sink, log, clock
        self.mono = mono if mono is not None else (time.monotonic if clock is time.time else clock)
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
        a, now, t = r.a, self.clock(), self.mono()  # now: sent; t: paces
        self._retarget(r)
        failing = bool(r.fails)
        if not r.fast or r.polled is None or (t - r.polled >= MIN_POLL_S and not r.reason):
            r.polled = t                            # (a trigger still waiting is measured from the read it met)
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
                if self._event(r, ev, now, t):
                    ids.append(ev.event_id)
            except Exception as e:  # noqa: BLE001 - one bad event never drops the rest
                self._skipped(r, f"event skipped ({type(e).__name__})", t)
        if ok:
            r.snapshot = ids
            self._seen(r, now, t)
        self._prune(r, t)
        # health(): after every poll; a memory read's at the floor's pace (and when poll() raised, or just stopped
        # raising, or nothing was sent yet), so the hub hears of its health exactly as often as before
        if not r.fast or not ok or failing or r.health is None or t - r.asked >= MIN_POLL_S:
            self._health(r, now, t)
        if not r.fails:
            return r.every
        return min(MAX_BACKOFF_S, max(MIN_POLL_S, float(a.poll_s)) * (2 ** min(r.fails - 1, 10)))   # from the floor

    def _skipped(self, r: _Run, what: str, t: float) -> None:
        """An event left out, logged; a memory read's the same line at most every MIN_POLL_S (never once per read)."""
        if r.fast:
            if t - r.skips.get(what, NEVER) < MIN_POLL_S:
                return
            if len(r.skips) >= SKIP_LOGS:
                r.skips.clear()
            r.skips[what] = t
        self.log(f"incident sources: {r.a.label}: {what}")

    def _event(self, r: _Run, ev, now: float, t: float) -> bool:
        """One event of a poll: sent when new or changed. False when it breaks the interface (skipped)."""
        a = r.a
        try:
            if not isinstance(ev, FeedEvent):
                raise SourceError(f"not a FeedEvent: {type(ev).__name__}")
            if ev.source != a.source:
                raise SourceError(f"source {ev.source!r} from the adapter {a.source!r}")
            d = ev.check().as_dict()
        except SourceError as e:
            self._skipped(r, f"event skipped: {e}", t)
            return False
        r.last_in[ev.event_id] = t
        if r.sent.get(ev.event_id) == d:
            return True
        first = r.first.get(ev.event_id)
        if first is None:                           # the adapter's own first-seen time when it gives a believable one
            first = r.first[ev.event_id] = first_seen(ev, now)
        self.sink("source_event", {"source": a.source, "label": a.label, "rank": a.rank, "seen_at": now,
                                   "first_seen": first, "event": d})
        r.sent[ev.event_id] = d
        return True

    def _seen(self, r: _Run, now: float, t: float) -> None:
        """Every SEEN_EVERY_S: the ids in the last good snapshot ("source_seen"), so the hub can flag an open report
        the feed no longer lists. complete: False when the list was cut at MAX_SEEN_IDS (the hub flags nothing then)."""
        if t - r.seen_at < SEEN_EVERY_S:
            return
        a = r.a
        self.sink("source_seen", {"source": a.source, "label": a.label, "rank": a.rank, "at": now,
                                  "ids": r.snapshot[:MAX_SEEN_IDS], "complete": len(r.snapshot) <= MAX_SEEN_IDS})
        r.seen_at = t

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
                r.health, r.seen_at = None, NEVER
            r.target = t

    @staticmethod
    def _prune(r: _Run, now: float) -> None:
        """Events no poll has had for PRUNE_S are forgotten here (one coming back is sent again: the hub sees no change).
        now: the runner's monotonic clock, as last_in is."""
        old = [k for k, t in r.last_in.items() if now - t > PRUNE_S]
        if len(r.last_in) - len(old) > PRUNE_MAX:
            old += sorted((k for k in r.last_in if k not in old), key=r.last_in.get)[:len(r.last_in) - len(old) - PRUNE_MAX]
        for k in old:
            r.last_in.pop(k, None)
            r.sent.pop(k, None)
            r.first.pop(k, None)

    def _health(self, r: _Run, now: float, t: float) -> None:
        a = r.a
        r.asked = t
        stopped = False
        try:
            h = a.health().check()
            stopped = h.state == "failing"              # the adapter itself says so (it stopped): shown at once
        except Exception as e:  # noqa: BLE001
            h = Health("failing", f"health(): {type(e).__name__}")
        if r.error and h.state == "ok":                 # poll() raised though health() says ok: it isn't
            h = Health("failing" if r.fails >= RAISE_FAILING_AFTER else "backing_off", r.error)
        key = (h.state, h.reason)
        if key == r.health and t - r.health_at < HEARTBEAT_S:
            return
        if key != r.health:
            self.log(f"incident sources: {a.label}: {h.state.replace('_', ' ')}" + (f" ({h.reason})" if h.reason else ""))
        r.health, r.health_at = key, t
        self.sink("source_health", {"source": a.source, "label": a.label, "rank": a.rank, "state": h.state,
                                    "reason": h.reason[:200], "since": h.since, "at": now, "stopped": stopped})

    # -- threads ---------------------------------------------------------------------------------------------------

    def trigger(self, reason: str) -> int:
        """Something that may be a call coming (paging tones, a radio pre-alert, an airport alert): each adapter's optional
        earlier poll. Gentle whatever wakes it (tone pairs, pre-alerts and phone rings come in bursts): at most one per
        max(poll_s, TRIGGER_MIN_S) per adapter, at most TRIGGER_MAX per TRIGGER_WINDOW_S, the poll itself never sooner
        than TRIGGER_MIN_S after the last one (_loop), and none while it is failing or backing off (the adapter's own
        pacing stands during an outage). Only sets a flag and the reason: never runs adapter code here (the caller may
        hold the paging decoder's lock); the adapter's own thread calls poll_now(reason) before that poll. How many
        were woken."""
        if str(reason or "").strip() in NOT_TRIGGERS:
            return 0                           # the phone ring: no trigger, no hold, no slot taken
        now, n = self.mono(), 0
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
                r.asked = NEVER                     # its health right after, as after any poll
        except Exception as e:  # noqa: BLE001
            self.log(f"incident sources: {r.a.label}: reset: {type(e).__name__}")

    def _poll_now(self, r: _Run) -> bool:
        """On the adapter's own thread: its poll_now(reason), when it was triggered. True when it was called."""
        reason, r.reason = r.reason, ""
        if not reason:
            return False
        r.polled = self.mono()                      # the triggered poll: the next trigger waits from here
        hook = getattr(r.a, "poll_now", None)
        if not callable(hook):
            return False
        try:
            hook(reason)
        except Exception as e:  # noqa: BLE001
            self.log(f"incident sources: {r.a.label}: poll_now: {type(e).__name__}")
        return True

    def hold(self, r: _Run) -> float:
        """Seconds a triggered poll still waits: never sooner than TRIGGER_MIN_S after the last poll."""
        return 0.0 if r.polled is None else max(0.0, TRIGGER_MIN_S - (self.mono() - r.polled))

    def _held(self, r: _Run) -> float:
        """Seconds the waiting trigger is still held (0: none waits, or it is due). A memory-read adapter is read on
        while its trigger waits: one that began failing or left "ok" meanwhile loses the trigger (none during an
        outage, as trigger() has it)."""
        if not r.reason:
            return 0.0
        if r.fast and (r.fails or (r.health is not None and r.health[0] != "ok")):
            r.reason = ""
            return 0.0
        return self.hold(r)

    def _loop(self, r: _Run) -> None:
        while not self._stop.is_set():
            r.wake.clear()
            h = self._held(r)
            # A trigger waits out its hold. An adapter that asks its source waits here; a memory read goes on being
            # read meanwhile, and its poll_now() comes when the hold is over, exactly as late as it always did
            if h > 0 and not r.fast and self._stop.wait(h):
                break
            self._reset_due(r)
            told = (h <= 0 or not r.fast) and self._poll_now(r)
            try:
                wait = self.step(r)
            except Exception as e:  # noqa: BLE001 - the sink failing: try again later
                self.log(f"incident sources: {r.a.label}: {type(e).__name__}")
                wait = MAX_BACKOFF_S
            r.wake.wait(self._next(r, wait, told))

    def _next(self, r: _Run, wait: float, told: bool) -> float:
        """The loop's wait after a step. A memory read only: read again SNAPSHOT_RECHECK_S after its poll_now() (the
        adapter's own thread fetches after poll_now returns; this read only picks up what that brought, and asks
        the source nothing), and no later than a waiting trigger's hold ends."""
        if not r.fast:
            return wait
        if told:
            return min(wait, SNAPSHOT_RECHECK_S)
        if r.reason and not r.fails:
            return min(wait, max(self.hold(r), 0.05))
        return wait

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
    """ctx_for(where) for load_adapters: each adapter's state dir under state_root/sources/. Its log lines start
    "[adapter] ": the station's log shipper keeps them on the station (cloud/logship.py ADAPTER_TAG, #548)."""
    def tagged(line: str) -> None:
        # every line: journald splits a message on its newlines, and each part is an entry of its own
        log("\n".join(f"[adapter] {x}" for x in (str(line).splitlines() or [""])))

    def make(where: str) -> SourceContext:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in where)[:64]
        return SourceContext(log=tagged, state_dir=str(Path(state_root) / "sources" / safe), clock=clock)
    return make
