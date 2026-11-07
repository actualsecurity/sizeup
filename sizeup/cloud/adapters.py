"""The hub's side of the station's incident-source adapters (#301): a hub never loads or runs one.

The guard is #297's (sizeup/sources/runner.py require_station / HubRefused, the check load_adapters makes before it looks
at any entry point). Until that is on main, the same check is here under the same names; once it is, the import below
is all that runs (and the fallback can go).

    discover(role)       the installed adapters' entry-point names (metadata only, nothing imported); HubRefused on the
                         hub before anything is looked at
    hub_guard(role, log) at the hub's start (sizeup/app.py make_app): an adapter installed in the hub's environment is
                         named in the log as refused, never imported
"""

from __future__ import annotations

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

__all__ = ["GROUP", "HubRefused", "discover", "hub_guard", "require_station"]


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
