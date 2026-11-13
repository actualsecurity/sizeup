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
    poll_s: float        how often the runner calls poll(): 30 to 60 s (never under MIN_POLL_S); the adapter paces its
                         own requests too
    poll() -> Iterable[FeedEvent]
                         the events the feed knows now (open ones, and ones that changed lately): a snapshot each time.
                         An event left out is never deleted, but the runner sends the ids of each poll's snapshot every
                         SEEN_EVERY_S ("source_seen"), and the hub flags an open report the feed no longer lists. It may
                         raise: the runner reports the
                         source "backing_off", "failing" from the RAISE_FAILING_AFTER-th raise in a row (the exception's
                         TYPE only as the reason: never its text, which may hold a URL or a credential) and calls it again after poll_s, doubling to MAX_BACKOFF_S while it keeps
                         raising. A bad event is skipped on its own; the others still go
    health() -> Health   how the feed is doing now; asked after every poll()
    close() -> None      optional: called once when the station stops
    reset() -> None      optional: a person asked to start over (Settings > Health "Reset", or `sizeup-station adapter
                         reset`, #305), e.g. for an adapter that stopped itself. Called on the adapter's own runner
                         thread before its next poll(); its back-off starts over. Raising is logged by type only
    poll_now(reason: str) -> None
                         optional: something just happened that may be a call coming (reason: "paging tones", "radio
                         pre-alert", "phone pre-alert"). Called on the adapter's own runner thread (never the caller's),
                         right before an earlier poll() than poll_s would give. The runner keeps triggered polls gentle:
                         at most one per max(poll_s, TRIGGER_MIN_S) per adapter, at most TRIGGER_MAX per
                         TRIGGER_WINDOW_S, never sooner than TRIGGER_MIN_S after the last poll, and none while the
                         adapter is failing or its health isn't ok. poll_now must NEVER bypass the adapter's own pacing
                         or back-off: it is a hint, and an adapter that is not due may ignore it

Rules for every adapter (the source is someone else's system; the department reads it, nothing more):

    read only        never acknowledge, write, mark, claim or change anything at the source. Closure is derived
                     from what the feed shows (an in-service or cleared time, or the event leaving its open list), never
                     made there
    gentle           poll every 30 to 60 s (poll_s), back off on errors, and never more often because of poll_now
    honest           identify the client honestly (its real name and version as the user agent); no spoofed user
                     agents, no browser impersonation, no anti-detection of any kind
    closed           set closed=True from the feed's own in-service or cleared time (put it in times["in_service"] too)
    stays here       the feed's data goes to the hub and nowhere else: never logged beyond its receipt (ids, types
                     and locations stay out of logs), never in a push, an issue, a PR or CI output

A FeedEvent's fields, as the feed says them (never parsed here beyond the checks in FeedEvent.check):

    source       the adapter's source id
    event_id     the feed's event id, stable for the event's life (1..64 chars)
    type         the event type: the feed's wording ("STRUCTURE FIRE"), or one of our type codes ("STRU", "ALARM/COMM")
    location     the location text ("12 Example Ave")
    units        the unit codes on it, in order ("E1", "L2"): up to 60, each at most 32 characters
    times        the event's times, TIME_KEYS -> epoch seconds: dispatched (out), responding, on_location, en_route_to,
                 out_at, in_service. Leave a key out until it happens
    unit_times   per unit (optional): unit -> TIME_KEYS -> epoch seconds (up to 60 units)
    comments     (epoch seconds, text) pairs, oldest first; append only (a comment once sent is never changed)
    closed       the feed shows it closed: derived from its in-service or cleared time (never closed at the source).
                 The hub dismisses a queued incident only this feed made and no person touched; any other gets a note
    extra        adapter-specific fields, JSON-able (str keys; values str, number, bool, list or dict), at most
                 EXTRA_MAX_BYTES as JSON. Keys the hub shows on the incident (EXTRA_SHOWN): "jurisdiction" (str),
                 "run_numbers" ({group: run number}), "group_units" ({group: [unit, ...]}). Any other key is kept and
                 its changes are on the timeline, nothing more
    aliases      external ids of this source merged INTO this event (the feed merged B into A: A's aliases has "B").
                 The hub retires B: its report points at A, a timeline entry on both, and B's incident, if it is a
                 different one still waiting to be accepted, is dismissed as merged (an accepted one only gets the
                 note: a person decides). Keep a merged id in aliases for as long as the event is sent

Every change to an event (a field, a time, a new comment) is an entry on the incident's timeline on the hub.
"""

from __future__ import annotations

import json
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
MIN_POLL_S = 30.0                      # gentle polling: never more often than this, whatever poll_s says
MAX_BACKOFF_S = 300.0
RAISE_FAILING_AFTER = 3                # poll() raising: "backing_off" until this many in a row, then "failing"
MAX_UNITS, MAX_COMMENTS, MAX_TEXT = 60, 500, 2000
MAX_ALIASES = 50
MAX_UNIT_LEN = 32
MAX_EVENT_BYTES = 256 * 1024            # one event as JSON, comments included
EXTRA_MAX_BYTES = 16 * 1024
EXTRA_SHOWN = ("jurisdiction", "run_numbers", "group_units")
TRIGGER_MIN_S = 30.0                    # a triggered poll: never sooner than this after the last poll, nor the last trigger
TRIGGER_MAX, TRIGGER_WINDOW_S = 4, 600.0  # ... and at most this many triggered polls per window, per adapter
SEEN_EVERY_S = 120.0                    # the runner sends the ids still in the feed's snapshot this often ("source_seen")
MAX_SEEN_IDS = 2000
TRIGGERS = ("paging tones", "radio pre-alert", "phone pre-alert")


class SourceError(ValueError):
    """An adapter or an event that breaks the interface (the runner logs it and skips the event or the adapter)."""


@dataclass(frozen=True)
class Health:
    state: str                         # one of HEALTH_STATES
    reason: str = ""                   # short, for people: "HTTP 503", "no update in 20 min"; never a secret, a URL or a
                                       # server's text (it goes to the hub and on screen)
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
    extra: Mapping[str, object] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()

    def check(self) -> FeedEvent:
        """SourceError when it breaks the interface (any wrong type included); else itself."""
        try:
            self._check()
            size = len(json.dumps(self._dict(), allow_nan=False))
        except SourceError:
            raise
        except (TypeError, ValueError, AttributeError, KeyError, IndexError) as e:
            raise SourceError(f"not an event: {type(e).__name__}") from None
        if size > MAX_EVENT_BYTES:
            raise SourceError(f"too large: {size} bytes as JSON (at most {MAX_EVENT_BYTES})")
        return self

    def _check(self) -> None:
        if not isinstance(self.source, str) or not SOURCE_ID.fullmatch(self.source) or self.source in RESERVED:
            raise SourceError(f"source {self.source!r}: [a-z0-9][a-z0-9_-]{{0,31}}, not {', '.join(RESERVED)}")
        if not isinstance(self.event_id, str) or not 1 <= len(self.event_id) <= 64:
            raise SourceError("event_id: 1 to 64 characters")
        for k in ("type", "location"):
            if not isinstance(getattr(self, k), str):
                raise SourceError(f"{k}: text")
        if (not isinstance(self.units, (tuple, list)) or len(self.units) > MAX_UNITS
                or not all(isinstance(u, str) and u.strip() and len(u) <= MAX_UNIT_LEN for u in self.units)):
            raise SourceError(f"units: up to {MAX_UNITS} unit codes of at most {MAX_UNIT_LEN} characters")
        _times(self.times, "times")
        if not isinstance(self.unit_times, Mapping) or len(self.unit_times) > MAX_UNITS:
            raise SourceError(f"unit_times: a mapping of up to {MAX_UNITS} units")
        for u, t in self.unit_times.items():
            if not isinstance(u, str) or not u.strip() or len(u) > MAX_UNIT_LEN:
                raise SourceError(f"unit_times: unit codes of at most {MAX_UNIT_LEN} characters")
            _times(t, "unit_times")                 # never the unit's code: the station logs this text
        if not isinstance(self.comments, (tuple, list)) or len(self.comments) > MAX_COMMENTS:
            raise SourceError(f"comments: up to {MAX_COMMENTS}")
        for c in self.comments:
            if not isinstance(c, (tuple, list)) or len(c) != 2 or not _when(c[0]) or not isinstance(c[1], str):
                raise SourceError("comments: (epoch seconds, text) pairs")
        if not isinstance(self.closed, bool):
            raise SourceError("closed: true or false")
        if not isinstance(self.extra, Mapping) or not all(isinstance(k, str) for k in self.extra):
            raise SourceError("extra: a mapping with text keys")
        try:
            size = len(json.dumps(dict(self.extra), allow_nan=False))
        except (TypeError, ValueError):
            raise SourceError("extra: JSON values only (text, numbers, booleans, lists, mappings)") from None
        if size > EXTRA_MAX_BYTES:
            raise SourceError(f"extra: at most {EXTRA_MAX_BYTES} bytes as JSON")
        if (not isinstance(self.aliases, (tuple, list)) or len(self.aliases) > MAX_ALIASES
                or not all(isinstance(a, str) and 1 <= len(a) <= 64 and a != self.event_id for a in self.aliases)):
            raise SourceError(f"aliases: up to {MAX_ALIASES} other event ids of this source")

    def as_dict(self) -> dict:
        """The event as it goes to the hub (the outbox's JSON). SourceError when it breaks the interface."""
        try:
            return self._dict()
        except (TypeError, ValueError, AttributeError, KeyError, IndexError) as e:
            raise SourceError(f"not an event: {type(e).__name__}") from None

    def _dict(self) -> dict:
        return {"source": self.source, "event_id": self.event_id, "type": self.type[:MAX_TEXT],
                "location": self.location[:MAX_TEXT], "units": [u.strip() for u in self.units],
                "times": {k: float(v) for k, v in self.times.items()},
                "unit_times": {u.strip(): {k: float(v) for k, v in t.items()} for u, t in self.unit_times.items()},
                "comments": [[float(at), text[:MAX_TEXT]] for at, text in self.comments], "closed": bool(self.closed),
                "extra": json.loads(json.dumps(dict(self.extra))), "aliases": list(self.aliases)}

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
                     closed=bool(d.get("closed")), extra=dict(d.get("extra") or {}),
                     aliases=tuple(str(a) for a in d.get("aliases") or ()))
        except (TypeError, ValueError, IndexError, AttributeError) as e:
            raise SourceError(f"not an event: {type(e).__name__}") from None
        return ev.check()


def _when(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _times(t: Mapping[str, float], where: str) -> None:
    if not isinstance(t, Mapping):
        raise SourceError(f"{where}: a mapping of {', '.join(TIME_KEYS)}")
    if len(t) > len(TIME_KEYS):
        raise SourceError(f"{where}: at most {len(TIME_KEYS)} times")
    for k, v in t.items():
        if k not in TIME_KEYS:
            raise SourceError(f"{where}: keys are {', '.join(TIME_KEYS)}")
        if not _when(v):
            raise SourceError(f"{where}: epoch seconds")


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

    # Optional, so not in the Protocol (an adapter without them is still one): close() -> None, reset() -> None,
    # poll_now(reason: str) -> None. See the module's docstring.


Factory = Callable[[SourceContext], "Adapter | Iterable[Adapter]"]
