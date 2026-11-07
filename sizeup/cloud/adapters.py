"""The hub's side of the station's incident-source adapters (#301): a hub never loads or runs one.

The guard is #297's (sizeup/sources/runner.py require_station / HubRefused, the check load_adapters makes before it looks
at any entry point). Until that is on main, the same check is here under the same names; once it is, the import below
is all that runs (and the fallback can go).

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

try:
    from sizeup.sources.runner import HubRefused, require_station
except ImportError:                                  # before #297 (PR #304) is merged: the same check
    class HubRefused(RuntimeError):  # type: ignore[no-redef]
        """Adapters run on the station only: the hub never discovers, imports or runs one."""

    def require_station(role: str) -> None:  # type: ignore[misc]
        if (role or "").strip().lower() == "hub":
            raise HubRefused("incident-source adapters run on the station only, never on the hub")

GROUP = "sizeup.incident_sources"         # = sizeup.sources.ENTRY_POINT_GROUP and autoupdate.ADAPTER_GROUP

SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")      # = sizeup.sources.SOURCE_ID

from sizeup.procenv import credential  # noqa: E402,F401 - an adapter's credentials: credential("FEED_USER")

__all__ = ["GROUP", "HubRefused", "Resets", "SOURCE_ID", "credential", "discover", "hub_guard", "request_reset",
           "require_station"]


def discover(role, entry_points: Callable | None = None) -> list[str]:
    require_station(str(role or ""))
    if entry_points is None:
        from importlib.metadata import entry_points
    return sorted({e.name for e in entry_points(group=GROUP)})


def hub_guard(role, log: Callable[[str], None], entry_points: Callable | None = None) -> list[str]:
    """The adapters found installed on the hub, each refused and never loaded ([] on a station)."""
    try:
        require_station(str(role or ""))
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


class Resets:
    """Reset requests, each run on its adapter's own thread: request() from anywhere, run_due() by the runner's loop for
    that adapter (before its next poll)."""

    def __init__(self):
        self._want: dict[str, str] = {}             # source -> who asked
        self._lock = threading.Lock()
        self.wake: Callable[[str], None] | None = None   # the runner's: cut a back-off short for that source

    def request(self, source: str, by: str = "") -> None:
        with self._lock:
            self._want[source] = by
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
            by = self._want.pop(source)
        who = f" (asked by {by})" if by else ""
        fn = getattr(adapter, "reset", None)
        if not callable(fn):
            log(f"incident sources: {source}: reset{who}: it has no reset(): nothing to do")
            return "no reset(): nothing to do"
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - an adapter's bug never stops the station
            log(f"incident sources: {source}: reset{who} failed: {type(e).__name__}")    # (its type only: no secrets)
            return f"failed: {type(e).__name__}"
        log(f"incident sources: {source}: reset{who}")
        return "reset"


def request_reset(runner, source: str, by: str = "", log: Callable[[str], None] = print) -> dict:
    """A reset of one source ("" = every source that has reset()), queued for its own thread. {ok, result | error}."""
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
        resets.request(src, by)
        out.append(f"{src}: reset asked (runs before its next poll)")
    log(f"incident sources: reset of {source or 'all'} asked by {by or 'someone'}: {'; '.join(out)}")
    return {"ok": True, "result": "; ".join(out)}


RESET_EVERY_S = 300.0


class ResetLimit:
    """One reset per source per RESET_EVERY_S (each server: the hub's, the station's): a shared account isn't hammered.
    blocked() before; done() only once the station said ok. "" (all) is blocked by any source's, and counts for all."""

    MAX = 64

    def __init__(self, every_s: float = RESET_EVERY_S, clock: Callable[[], float] | None = None):
        import time
        self.every_s, self.clock = every_s, clock or time.time
        self._at: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked(self, source: str) -> float:
        """0 = go; else the seconds until a reset of source may."""
        now = self.clock()
        with self._lock:
            keys = list(self._at) if not source else [source, "*"]
            last = max((self._at.get(k, -1e18) for k in keys), default=-1e18)
        return max(0.0, self.every_s - (now - last))

    def done(self, source: str, sources: list[str] | tuple = ()) -> None:
        now = self.clock()
        with self._lock:
            for k in ([source] if source else ["*", *sources]):
                self._at[k] = now
            if len(self._at) > self.MAX:            # bounded: the oldest go
                for k in sorted(self._at, key=self._at.get)[:len(self._at) - self.MAX]:
                    del self._at[k]
