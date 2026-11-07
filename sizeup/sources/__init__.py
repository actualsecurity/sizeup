"""External incident sources (#297): the plug-in point for read-only incident feeds on the station.

Incidents enter sizeup from ranked sources. An external CAD feed is the primary (rank 1; more feeds rank 2, 3, ...); the
paging decoder is the backup and ranks below every feed (sizeup/cad/sources.py PAGING_RANK). Pre-alerts (the phone ring,
radio phrases announcing a page) never make an incident. The adapters live OUTSIDE this repo (a private package installed
on the station); this module is everything they are built against. Standard library only.

Station only: adapters are discovered, imported and run on the station (or all-in-one), never on the hub. The hub refuses
with a hard check (sizeup/sources/runner.py require_station: HubRefused) before it looks at any entry point or module; it
only receives the station's forwarded events through the outbox, and the adapters' credentials never leave the station.

Discovery (sizeup/sources/runner.py load_adapters), both at once, each factory called once at start:

    entry point     group "sizeup.incident_sources" in the adapter package's metadata, e.g. in its pyproject.toml:
                        [project.entry-points."sizeup.incident_sources"]
                        county = "my_adapters.county:make"
    module path     SIZEUP_INCIDENT_SOURCES="my_adapters.county:make,other.mod:factory" in the station's environment
                    (/etc/sizeup/station.env), with SIZEUP_INCIDENT_SOURCES_PATH="/opt/sizeup-adapters" (os.pathsep-
                    separated directories put on sys.path first). Preferred on the station: a release's venv is rebuilt
                    when pyproject.toml changes, and a package installed into the old one is gone; a directory isn't.
                    Not a config.toml key, so switching back to an older release never meets a key it can't read.

A factory is `factory(ctx: SourceContext) -> Adapter | Iterable[Adapter]`. An adapter:

    source: str          stable id, SOURCE_ID ([a-z0-9][a-z0-9_-]{0,31}): "county"; never "paging"
    label: str           short name for people: "County feed"
    rank: int            1 = the primary; 2, 3 ... the next tiers (1..99)
    poll_s: float        how often the runner calls poll() (at least MIN_POLL_S); the adapter paces its own requests
    poll() -> Iterable[FeedEvent]
                         the events the feed knows now (open ones, and ones that changed lately): a snapshot each time.
                         An event left out is left as last seen (never deleted). It may raise: the runner reports the
                         source "failing" (the exception's type and message as the reason) and calls it again after
                         poll_s, doubling to MAX_BACKOFF_S while it keeps raising
    health() -> Health   how the feed is doing now; asked after every poll()
    close() -> None      optional: called once when the station stops

A FeedEvent's fields, as the feed says them (never parsed here beyond the checks in FeedEvent.check):

    source       the adapter's source id
    event_id     the feed's event id, stable for the event's life (1..64 chars)
    type         the event type: the feed's wording ("STRUCTURE FIRE"), or one of our type codes ("STRU", "ALARM/COMM")
    location     the location text ("12 Example Ave")
    units        the unit codes on it, in order ("E1", "L2")
    times        the event's times, TIME_KEYS -> epoch seconds: dispatched (out), responding, on_location, en_route_to,
                 out_at, in_service. Leave a key out until it happens
    unit_times   per unit (optional): unit -> TIME_KEYS -> epoch seconds
    comments     (epoch seconds, text) pairs, oldest first; append only (a comment once sent is never changed)
    closed       the feed closed it

Every change to an event (a field, a time, a new comment) is an entry on the incident's timeline on the hub.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Protocol, runtime_checkable

ENTRY_POINT_GROUP = "sizeup.incident_sources"
ENV_MODULES = "SIZEUP_INCIDENT_SOURCES"
ENV_PATH = "SIZEUP_INCIDENT_SOURCES_PATH"

TIME_KEYS = ("dispatched", "responding", "on_location", "en_route_to", "out_at", "in_service")
HEALTH_STATES = ("ok", "backing_off", "failing", "stale")
SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
RESERVED = ("paging",)
MIN_POLL_S = 5.0
MAX_BACKOFF_S = 300.0
MAX_UNITS, MAX_COMMENTS, MAX_TEXT = 60, 500, 2000


class SourceError(ValueError):
    """An adapter or an event that breaks the interface (the runner logs it and skips the event or the adapter)."""


@dataclass(frozen=True)
class Health:
    state: str                         # one of HEALTH_STATES
    reason: str = ""                   # short, for people: "HTTP 503", "no update in 20 min"
    since: float | None = None         # when it entered this state (epoch seconds), if the adapter knows

    def check(self) -> Health:
        if self.state not in HEALTH_STATES:
            raise SourceError(f"health state {self.state!r}: one of {', '.join(HEALTH_STATES)}")
        return self


@dataclass(frozen=True)
class FeedEvent:
    source: str
    event_id: str
    type: str = ""
    location: str = ""
    units: tuple[str, ...] = ()
    times: Mapping[str, float] = field(default_factory=dict)
    unit_times: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    comments: tuple[tuple[float, str], ...] = ()
    closed: bool = False

    def check(self) -> FeedEvent:
        """SourceError when it breaks the interface; else itself."""
        if not SOURCE_ID.fullmatch(self.source or "") or self.source in RESERVED:
            raise SourceError(f"source {self.source!r}: [a-z0-9][a-z0-9_-]{{0,31}}, not {', '.join(RESERVED)}")
        if not isinstance(self.event_id, str) or not 1 <= len(self.event_id) <= 64:
            raise SourceError("event_id: 1 to 64 characters")
        for k in ("type", "location"):
            if not isinstance(getattr(self, k), str):
                raise SourceError(f"{k}: text")
        if len(self.units) > MAX_UNITS or not all(isinstance(u, str) and u.strip() for u in self.units):
            raise SourceError(f"units: up to {MAX_UNITS} unit codes")
        _times(self.times, "times")
        for u, t in self.unit_times.items():
            _times(t, f"unit_times[{u!r}]")
        if len(self.comments) > MAX_COMMENTS:
            raise SourceError(f"comments: up to {MAX_COMMENTS}")
        for c in self.comments:
            if (not isinstance(c, tuple) or len(c) != 2 or not _when(c[0]) or not isinstance(c[1], str)):
                raise SourceError("comments: (epoch seconds, text) pairs")
        return self

    def as_dict(self) -> dict:
        """The event as it goes to the hub (the outbox's JSON)."""
        return {"source": self.source, "event_id": self.event_id, "type": self.type[:MAX_TEXT],
                "location": self.location[:MAX_TEXT], "units": [u.strip() for u in self.units],
                "times": {k: float(v) for k, v in self.times.items()},
                "unit_times": {u.strip(): {k: float(v) for k, v in t.items()} for u, t in self.unit_times.items()},
                "comments": [[float(at), text[:MAX_TEXT]] for at, text in self.comments], "closed": bool(self.closed)}

    @classmethod
    def from_dict(cls, d: dict) -> FeedEvent:
        """The hub's side: an event from the outbox, checked (SourceError when it isn't one)."""
        if not isinstance(d, dict):
            raise SourceError("an event: an object")
        try:
            ev = cls(source=str(d.get("source") or ""), event_id=str(d.get("event_id") or ""),
                     type=str(d.get("type") or ""), location=str(d.get("location") or ""),
                     units=tuple(str(u) for u in d.get("units") or ()),
                     times=dict(d.get("times") or {}),
                     unit_times={str(u): dict(t) for u, t in (d.get("unit_times") or {}).items()},
                     comments=tuple((c[0], c[1]) for c in d.get("comments") or ()),
                     closed=bool(d.get("closed")))
        except (TypeError, ValueError, IndexError, AttributeError) as e:
            raise SourceError(f"not an event: {e}") from None
        return ev.check()


def _when(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _times(t: Mapping[str, float], where: str) -> None:
    if not isinstance(t, Mapping):
        raise SourceError(f"{where}: a mapping of {', '.join(TIME_KEYS)}")
    for k, v in t.items():
        if k not in TIME_KEYS:
            raise SourceError(f"{where}: {k!r} is not one of {', '.join(TIME_KEYS)}")
        if not _when(v):
            raise SourceError(f"{where}[{k!r}]: epoch seconds")


@dataclass(frozen=True)
class SourceContext:
    """What a factory gets: log(line) (the station's log), state_dir (a directory of the adapter's own to keep state in,
    made on first use by the adapter) and clock() (epoch seconds; tests pass a fake one)."""
    log: Callable[[str], None]
    state_dir: str
    clock: Callable[[], float]


@runtime_checkable
class Adapter(Protocol):
    source: str
    label: str
    rank: int
    poll_s: float

    def poll(self) -> Iterable[FeedEvent]: ...

    def health(self) -> Health: ...


Factory = Callable[[SourceContext], "Adapter | Iterable[Adapter]"]
