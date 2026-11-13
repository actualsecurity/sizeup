"""The radio-intelligence pipeline's values (sizeup/intel): what a rule hit, what an event is, what the policy decided.

They carry no transcript text: an event names its transmissions by id (traffic_ids) and its place by the gazetteer's
label. Replay (tools, the data session's PR 7) reads the same fields the live store keeps.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

MODES = ("off", "shadow", "log", "map", "push")      # the ladder: each step does everything the one before does
PRECISIONS = ("address", "intersection", "block", "landmark", "street", "town", "none")
STATUSES = ("open", "ended", "expired", "dismissed")
CHANGES = ("raised", "updated", "ended", "expired")


def mode_rank(m: str) -> int:
    try:
        return MODES.index(m)
    except ValueError:
        return 0


def lowest(*modes: str) -> str:
    """The lowest rung of the ladder among these (a pack's mode caps its categories', a rule's caps what it raises)."""
    ms = [m for m in modes if m]
    return min(ms, key=mode_rank) if ms else "off"


@dataclass(frozen=True)
class Hit:
    """One rule that matched one transmission. Every hit is reported (shadow and below-threshold too): recall."""
    traffic_id: str
    rule_id: str
    rule_rev: int
    weight: float
    category: str
    pack: str
    at: float
    event_id: int | None = None


@dataclass
class Place:
    """Where an event is, as sure as the words make it. label is the gazetteer's (an address, never transcript text)."""
    precision: str = "none"            # PRECISIONS
    lat: float | None = None
    lon: float | None = None
    radius_m: float = 0.0
    label: str = ""                    # "100 Example Ave, Port Chester" (the console and the push: user, 2026-11-12)
    town: str = ""
    confirmed: bool = False            # the words agree (a read-back, the cross streets); unconfirmed: never drawn exact
    where_score: float = 0.0
    alts: list = field(default_factory=list)     # [{label, lat, lon, kind, town, confidence}]
    basis: str = ""                    # why ("between A and B", "sounds like what was said")

    @property
    def key(self) -> str:
        """The same-place key (events merge on it): the label, else nothing."""
        return self.label.lower() if self.label and self.precision != "none" else ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    id: int | None = None
    domain: str = "police"
    pack: str = ""
    category: str = ""
    subject: str = ""                  # the dedup key: pack:category:place, else pack:category:source
    source_key: str = ""
    system: str = ""
    service: str = ""
    dept: str = ""
    first_at: float = 0.0
    last_at: float = 0.0
    until: float = 0.0
    status: str = "open"
    what_score: float = 0.0
    level: str = "low"
    rules: list = field(default_factory=list)          # [[rule id, rev, weight]]
    ruleset_hash: str = ""
    where_score: float = 0.0
    precision: str = "none"
    lat: float | None = None
    lon: float | None = None
    radius_m: float = 0.0
    label_full: str = ""
    town: str = ""
    alts: list = field(default_factory=list)
    basis: str = ""
    confirmed: bool = False
    traffic_ids: list = field(default_factory=list)
    mode: str = "log"                  # the mode it was raised (or last escalated) at
    priority: int = 3
    pushed_at: float | None = None
    push_count: int = 0
    suppressed: str = ""               # rate | quiet | shadow | merged | scrub | off | "" (pushed, or not to push)
    updated_at: float = 0.0
    near_incident: int | None = None   # scene-safety overlay only (PR 9); never a radio link
    incident_id: int | None = None     # only after a person's Accept, and only for a pack with accept: true

    def place(self) -> Place:
        return Place(self.precision, self.lat, self.lon, self.radius_m, self.label_full, self.town, self.confirmed,
                     self.where_score, list(self.alts), self.basis)

    def set_place(self, p: Place) -> None:
        self.precision, self.lat, self.lon, self.radius_m = p.precision, p.lat, p.lon, p.radius_m
        self.label_full, self.town, self.confirmed, self.where_score = p.label, p.town, p.confirmed, p.where_score
        self.alts, self.basis = list(p.alts), p.basis

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    """What the policy says about one event change: the verdict only (the live sink is what sends)."""
    mode: str
    would_push: bool
    suppressed: str = ""
    priority: int = 3
    repush: bool = False
    message: dict | None = None        # the composed, scrubbed push ({title, body, tags, priority, click}) when would_push
