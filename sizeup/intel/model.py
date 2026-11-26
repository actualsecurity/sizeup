"""The radio-intelligence pipeline's values (sizeup/intel): what a rule hit, what an event is, what the policy decided.

They carry no transcript text: an event names its transmissions by id (traffic_ids) and its place by the gazetteer's
label. Replay (tools, the data session's PR 7) reads the same fields the live store keeps.

The talker town (engine.py _talkers): the town of who said it (Settings > Radios labels, sizeup/intel/talkers.py), kept
beside the heard place, never in it. Event.shown() is the place as shown and pushed: the heard one, else "<Town>
(talker)" at precision "town" (no point: never drawn exact, never a site). The engine's own place checks (merge, the
re-push on a place found, our incidents, the FD) read the heard place only, so a heard place always wins and a later
one replaces the talker town.
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
    suppressed: str = ""               # rate | quiet | shadow | merged | scrub | off | out_of_area | near_incident | unplaced |
                                       # fd_on_scene | fd_dispatched | fd_there | fd_late | late | ""
                                       # (late: its rows read in time don't push it alone, engine._fresh)
    updated_at: float = 0.0
    note: str = ""                     # "possibly cancelled" (a nameless disregard of a safety call), "moved", "re-opened", "de-escalated",
                                       # "source withdrawn" (its only row's text emptied or unclear since)
    near_incident: int | None = None   # our own incident at its place (engine._incident, _fire): linked, never a radio link
    fd: str = ""                       # a fire-related event with the FD at its place (engine._fire): on_scene |
                                       # dispatched | there (said so) -> a push of it is an FYI; cleared -> a pre-alert
                                       # as usual ("FD here HH:MM")
    incident_id: int | None = None     # only after a person's Accept, and only for a pack with accept: true
    # who said it (engine.py _talkers; labels as Settings > Radios names the radios, never a raw ID): the row that first
    # said its category, and the dispatch console that answered it (an officer's radio only), each with its town
    stated_by: str = ""
    stated_town: str = ""
    answered_by: str = ""
    answered_town: str = ""
    talker_town: str = ""              # the town of who said it ("" none known): a coarse place (shown())

    def place(self) -> Place:
        return Place(self.precision, self.lat, self.lon, self.radius_m, self.label_full, self.town, self.confirmed,
                     self.where_score, list(self.alts), self.basis)

    def set_place(self, p: Place) -> None:
        self.precision, self.lat, self.lon, self.radius_m = p.precision, p.lat, p.lon, p.radius_m
        self.label_full, self.town, self.confirmed, self.where_score = p.label, p.town, p.confirmed, p.where_score
        self.alts, self.basis = list(p.alts), p.basis

    def shown(self) -> Place:
        """The place as shown and pushed: the heard one, else the talker town ("<Town> (talker)", precision "town", no
        point), else none."""
        if self.precision != "none" or not self.talker_town:
            return self.place()
        return Place(TOWN, label=talker_label(self.talker_town), town=self.talker_town, basis=TALKER)

    def as_dict(self) -> dict:
        return asdict(self)


TOWN, TALKER = "town", "talker"


def talker_label(town: str) -> str:
    return f"{town} (talker)" if town else ""


def initials(town: str) -> str:
    """"Port Chester" -> "PC", "Rye Brook" -> "RB", "Rye" -> "Rye" (a one-word town as it is)."""
    ws = str(town or "").split()
    return "".join(w[0].upper() for w in ws) if len(ws) > 1 else (ws[0] if ws else "")


def talkers_line(stated: str, stated_town: str, answered: str, answered_town: str) -> str:
    """"stated by: <label> (PC) · answered by: <label> (PC)": "" when neither is labelled."""
    if not (stated or answered):
        return ""
    one = lambda lab, town: (lab or "unlabelled") + (f" ({initials(town)})" if town else "")   # noqa: E731
    return " · ".join(x for x in (f"stated by: {one(stated, stated_town)}",
                                  f"answered by: {one(answered, answered_town)}" if answered else "") if x)


def shown_place(e: dict) -> dict:
    """A stored event's place fields as shown (the API's "place", "precision", "town", "basis"): {} when a place was
    heard or there is no talker town (as stored), else the talker town as Event.shown() has it."""
    t = e.get("talker_town") or ""
    if (e.get("precision") or "none") != "none" or not t:
        return {}
    return {"place": talker_label(t), "precision": TOWN, "town": t, "basis": TALKER}


def talkers_of(e: dict) -> dict:
    """Who said it, as the API gives it: {stated_by: {label, town} | None, answered_by: ..., line: talkers_line}."""
    one = lambda k: ({"label": e.get(k) or "", "town": e.get(k.split("_")[0] + "_town") or ""}       # noqa: E731
                     if e.get(k) or e.get(k.split("_")[0] + "_town") else None)
    return {"stated_by": one("stated_by"), "answered_by": one("answered_by"),
            "line": talkers_line(e.get("stated_by") or "", e.get("stated_town") or "", e.get("answered_by") or "",
                                 e.get("answered_town") or "")}


@dataclass(frozen=True)
class Decision:
    """What the policy says about one event change: the verdict only (the live sink is what sends)."""
    mode: str
    would_push: bool
    suppressed: str = ""
    priority: int = 3
    repush: bool = False
    message: dict | None = None        # the composed, scrubbed push ({title, body, tags, priority, click}) when would_push
