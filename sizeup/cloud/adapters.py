"""The hub's side of the station's incident-source adapters (#301): a hub never loads or runs one.

The guard and Resets are #297's (PR #304): sizeup/sources/runner.py require_station / HubRefused, the check
load_adapters makes before it looks at any entry point, and sizeup/sources/resets.py Resets. They are imported LAZILY
(module __getattr__, and inside the functions here), never at this module's import, so neither import order makes a
cycle (#304's runner may import this module). Until #304 is on main the same names are defined here, used only when
that lazy import raises ImportError.

    discover(role)       the installed adapters' entry-point names (metadata only, nothing imported); HubRefused on the
                         hub before anything is looked at
    hub_guard(role, log) at the hub's start (sizeup/app.py make_app): an adapter installed in the hub's environment is
                         named in the log as refused, never imported

Reset (#301): an adapter may stop itself ("failing: login (manual reset needed)") to protect a shared account, and
nobody may be at the station. The OPTIONAL `Adapter.reset() -> None` (an addition to #297's interface) clears that.
It is asked for by an admin on the hub (Settings > Health, POST /api/sources/reset: a sources_reset job over the
station's authenticated jobs channel) or on the station (sudo sizeup-station adapter reset [source]), and always runs
on the adapter's own thread: request_reset() only queues it in the runner's Resets, and the runner's loop calls
Resets.run_due(adapter) before each poll (#297's SourceRunner: `self.resets = Resets()`, then
`self.resets.run_due(r.a, self.log)` in its per-adapter loop, and a wake of that thread if it is backing off). An
adapter without reset(): a no-op, said so.
"""

from __future__ import annotations

import re
import threading
from typing import Callable



class _HubRefused(RuntimeError):
    """(Before #304:) adapters run on the station only: the hub never discovers, imports or runs one."""


def _require_station(role: str) -> None:
    if (role or "").strip().lower() == "hub":
        raise _impl()[0]("incident-source adapters run on the station only, never on the hub")


def _impl() -> tuple:
    """(HubRefused, require_station, Resets): #304's when it's there, else these. Imported here, at call time."""
    try:
        from sizeup.sources.runner import HubRefused, require_station
    except ImportError:
        HubRefused, require_station = _HubRefused, _require_station
    try:
        from sizeup.sources.resets import Resets
    except ImportError:
        Resets = _Resets
    return HubRefused, require_station, Resets


def __getattr__(name: str):
    """HubRefused, require_station and Resets, resolved lazily (PEP 562): no import of sizeup.sources at load."""
    if name in ("HubRefused", "require_station", "Resets"):
        return dict(zip(("HubRefused", "require_station", "Resets"), _impl()))[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _guard(role) -> None:
    _impl()[1](str(role or ""))


GROUP = "sizeup.incident_sources"         # = sizeup.sources.ENTRY_POINT_GROUP and autoupdate.ADAPTER_GROUP

SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")      # = sizeup.sources.SOURCE_ID

from sizeup.procenv import credential  # noqa: E402,F401 - an adapter's credentials: credential("FEED_USER")

__all__ = ["GROUP", "HubRefused", "Resets", "SOURCE_ID", "credential", "discover", "hub_guard", "request_reset",
           "require_station"]


def discover(role, entry_points: Callable | None = None) -> list[str]:
    _guard(role)
    if entry_points is None:
        from importlib.metadata import entry_points
    return sorted({e.name for e in entry_points(group=GROUP)})


def hub_guard(role, log: Callable[[str], None], entry_points: Callable | None = None) -> list[str]:
    """The adapters found installed on the hub, each refused and never loaded ([] on a station)."""
    HubRefused = _impl()[0]
    try:
        _guard(role)
        return []
    except HubRefused as refused:
        why = str(refused)
    if entry_points is None:
        from importlib.metadata import entry_points
    try:
        found = sorted({e.name for e in entry_points(group=GROUP)})
    except Exception as e:  # noqa: BLE001 - broken metadata: nothing is loaded either way
        log(f"incident sources: entry points unreadable ({type(e).__name__}): none loaded on the hub")
        return []
    if found:
        log(f"incident sources: {', '.join(found)} installed on the hub: refused, never loaded ({why})")
    return found


class _Resets:
    """(Before #304's sizeup/sources/resets.py:) reset requests, each run on its adapter's own thread: request() from anywhere, run_due() by the runner's loop for
    that adapter (before its next poll)."""

    def __init__(self):
        self._want: dict[str, tuple[str, str]] = {}   # source -> (who asked, origin)
        self._lock = threading.Lock()
        self.wake: Callable[[str], None] | None = None   # the runner's: cut a back-off short for that source

    def request(self, source: str, by: str = "", origin: str = "") -> None:
        with self._lock:
            self._want[source] = (by, origin)
        if self.wake is not None:
            self.wake(source)

    def pending(self, source: str) -> bool:
        with self._lock:
            return source in self._want

    def run_due(self, adapter, log: Callable[[str], None] = print) -> str | None:
        """On the adapter's thread: its reset(), if one was asked for. What happened (None: nothing asked)."""
        source = str(getattr(adapter, "source", ""))
        with self._lock:
            if source not in self._want:
                return None
            by, origin = self._want.pop(source)
        who = f" (asked by {by})" if by else ""
        fn = getattr(adapter, "reset", None)
        if not callable(fn):
            log(f"incident sources: {source}: reset{who}: it has no reset(): nothing to do")
            return "no reset(): nothing to do"
        try:
            from sizeup.sources.resets import call_reset
            call_reset(fn, origin)
        except Exception as e:  # noqa: BLE001 - an adapter's bug never stops the station
            log(f"incident sources: {source}: reset{who} failed: {type(e).__name__}")    # (its type only: no secrets)
            return f"failed: {type(e).__name__}"
        log(f"incident sources: {source}: reset{who}")
        return "reset"


def request_reset(runner, source: str, by: str = "", log: Callable[[str], None] = print, origin: str = "") -> dict:
    """A reset of one source ("" = every source that has reset()), queued for its own thread. {ok, result | error}.
    origin: "hub" (an admin's Reset on the hub) | "local" (a person at the station): passed on to the adapter's
    reset(origin=...) when it takes it (only a local one may clear caps that guard a shared account)."""
    source = str(source or "").strip()
    if source and not SOURCE_ID.fullmatch(source):
        return {"ok": False, "error": "source: a source id"}
    runs = list(getattr(runner, "runs", None) or []) if runner is not None else []
    if not runs:
        return {"ok": False, "error": "no incident sources run on this station"}
    by = re.sub(r"[^\w@.+ -]", "", str(by or ""))[:120]
    adapters = {str(getattr(r.a, "source", "")): r.a for r in runs}
    if source and source not in adapters:
        return {"ok": False, "error": f"no source {source!r} here ({', '.join(sorted(adapters))})"}
    picked = [source] if source else sorted(adapters)
    resets = getattr(runner, "resets", None)
    out = []
    for src in picked:
        if not callable(getattr(adapters[src], "reset", None)):
            out.append(f"{src}: no reset(): nothing to do")
            continue
        if resets is None:
            return {"ok": False, "error": "this station's runner can't reset yet (#297: Resets)"}
        resets.request(src, by, origin)
        out.append(f"{src}: reset asked (runs before its next poll)")
    log(f"incident sources: reset of {source or 'all'} asked by {by or 'someone'}: {'; '.join(out)}")
    return {"ok": True, "result": "; ".join(out)}


RESET_EVERY_S = 300.0


class ResetLimit:
    """One reset per source per RESET_EVERY_S (each server: the hub's, the station's): a shared account isn't hammered.
    take() before (it marks the source pending, so two at once can't both go), finish() after: counted only when the
    station said ok. "" (all) is held up by any source's, and counts for all of them. Bounded."""

    MAX = 64

    def __init__(self, every_s: float = RESET_EVERY_S, clock: Callable[[], float] | None = None):
        import time
        self.every_s, self.clock = every_s, clock or time.time
        self._at: dict[str, float] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    def _wait(self, source: str, now: float) -> float:
        keys = list(self._at) if not source else [source, "*"]
        last = max((self._at.get(k, -1e18) for k in keys), default=-1e18)
        busy = bool(self._pending) if not source else bool({source, "*"} & self._pending)
        return self.every_s if busy else max(0.0, self.every_s - (now - last))

    def blocked(self, source: str) -> float:
        """0 = a reset of source may go now; else the seconds until it may."""
        with self._lock:
            return self._wait(source, self.clock())

    def take(self, source: str) -> float:
        """blocked(), and when 0 the source is marked pending until finish()."""
        with self._lock:
            wait = self._wait(source, self.clock())
            if not wait:
                self._pending.add(source or "*")
            return wait

    def finish(self, source: str, ok: bool, sources: list[str] | tuple = ()) -> None:
        with self._lock:
            self._pending.discard(source or "*")
            if ok:
                now = self.clock()
                for k in ([source] if source else ["*", *sources]):
                    self._at[k] = now
                if len(self._at) > self.MAX:            # bounded: the oldest go
                    for k in sorted(self._at, key=self._at.get)[:len(self._at) - self.MAX]:
                        del self._at[k]

    def done(self, source: str, sources: list[str] | tuple = ()) -> None:
        self.finish(source, True, sources)
