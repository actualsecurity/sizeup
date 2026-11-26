"""The radio-intelligence pipeline's values (sizeup/intel): what a rule hit, what an event is, what the policy decided.

They carry no transcript text: an event names its transmissions by id (traffic_ids) and its place by the gazetteer's
label. Replay (tools, the data session's PR 7) reads the same fields the live store keeps.

The talker town (engine.py _talkers): the town of who said it (Settings > Radios labels, sizeup/intel/talkers.py), kept
beside the heard place, never in it. One rule says which town an event is in (resolved_town), for the push's title and
body, the console, the Intel page, Search and the area gate alike: a heard place's town; else, with no place heard,
the town of the console a handed-off call went to, else its talker town (a known town only: known_town).
shown_place gives the coarse place of an unplaced one: "<Town> (handoff)" or "<Town> (talker)" at precision "town"
(no point: never drawn exact, never a site). The engine's own place checks (merge, the re-push on a place found, our incidents, the FD) read
the heard place only, so a heard place always wins and a later one replaces the talker town.
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

    def as_dict(self) -> dict:
        return asdict(self)


TOWN, TALKER, HANDOFF = "town", "talker", "handoff"


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


def _get(e, k: str):
    return e.get(k) if isinstance(e, dict) else getattr(e, k, None)


def heard(e) -> bool:
    """A place was heard in the words (an Event or a stored row)."""
    return (_get(e, "precision") or "none") != "none"


def known_town(t, towns=()) -> str:
    """t as a town's name when it is one: one of `towns` (a pack's area, a source's towns) or of the county's list
    (rules.town_names: data/westchester_towns.txt); "" for anything else (a console's label, a typo)."""
    t = str(t or "").strip()
    if not t:
        return ""
    for x in towns or ():
        if str(x).lower() == t.lower():
            return str(x)
    from sizeup.intel.rules import town_names        # (rules imports this module)
    try:
        return town_names().get(t.lower(), "")
    except Exception:  # noqa: BLE001 - no list: no town
        return ""


def handoff_town(e, towns=()) -> str:
    """The town of the console a call was handed to (#961's handoff record: a dict with "to_town", or a list whose
    third item is it; the last handoff when there are several), only when it is a known town (known_town); "" none.
    Read only when no place was heard."""
    h = _get(e, "handoff")
    if isinstance(h, list) and h and isinstance(h[-1], (list, dict)):
        h = h[-1]                         # several handoffs: the last
    if isinstance(h, dict):
        return known_town(h.get("to_town"), towns)
    if isinstance(h, (list, tuple)) and len(h) > 2:
        return known_town(h[2], towns)
    return ""


def _unheard(e, towns=()) -> tuple[str, str]:
    """(town, basis) of an event with no place heard: the handoff's town or its own (a handed-off call: the console
    that took it), "handoff"; else, never handed off, its talker town, "talker"; ("", "") none. Known towns only
    (known_town)."""
    t = handoff_town(e, towns) or known_town(_get(e, "town"), towns)
    if t:
        return t, HANDOFF
    if _get(e, "handoff"):
        return "", ""                     # handed off, its town not known: never the stater's town instead
    t = known_town(_get(e, "talker_town"), towns)
    return (t, TALKER) if t else ("", "")


def resolved_town(e, towns=()) -> str:
    """The one town an event is in (an Event or a stored row): its heard place's; else, with no place heard, the town
    of the console a handed-off call went to, else its talker town; "" when none (or not a known town). towns: a
    pack's area and the source's towns, known beside the county's list."""
    if heard(e):
        return _get(e, "town") or ""
    return _unheard(e, towns)[0]


def shown_place(e) -> dict:
    """An event's coarse place as shown (the API's "place", "precision", "town", "basis"; an Event or a stored row):
    {} when a place was heard or no town is known, else "<Town> (handoff)" or "<Town> (talker)" at precision town, no
    point; basis says which."""
    if heard(e):
        return {}
    t, basis = _unheard(e)
    if not t:
        return {}
    return {"place": f"{t} ({basis})", "precision": TOWN, "town": t, "basis": basis}


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
