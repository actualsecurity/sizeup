"""The CAD's vocabulary, from one data file: data/cad_lists.json.

Everything the console offers as a list lives there, in the order the department's sheet
("[TEMPLATE] PCFD ECC OPS") has it. Each item has the sheet's code (what is stored, matched and
exported to CSV, so the exports line up with the old sheet) and a readable name (what the console
shows). A new export can replace the file without a code change:

    event_types                      {code, name, color}: Type dropdown and the colored type cells
    event_subtypes                   {code, name}: Subtype dropdown
    type_subtypes                    TYPE -> allowed subtypes (missing or empty = any subtype)
    dispositions                     {code, name}: Close event needs one
    received_via                     {code, name}: how the call came in
    event_class                      emergency / non-emergency by TYPE/SUBTYPE, */SUBTYPE or TYPE
    unit_statuses                    {code, name, role, bg, fg}: the status dropdown; the role is what
                                     the store does with it (free, oos, dispatched, enroute, onscene)
    event_statuses                   name + colors per event status
    unit_kinds, unit_boards, units   the two status boards (null = a blank separator row) and per-unit
                                     kind / spoken name / title / spare / department
    joint_response                   town -> the units of a department answering jointly with PCFD
                                     (Rye Brook): direct units, picked by kind after PCFD's own, first
                                     on an event in that town
    spoken                           TYPE, TYPE/SUBTYPE or */SUBTYPE -> as the dispatch voice says it
    dispatch_wording                 the dispatch as templates (new / add-on / upgrade, full response,
                                     remarks, lead-in: "Port Chester and Rye Brook Fire" when theirs go)
    dispatch_tones                   joint town -> its alert tones (null: not known, PCFD's are played)
    run_cards                        the default cards seeded once into the database
    geo_town_order, geo_town_aliases Location suggestions: which towns come first

load() checks it and raises ListsError (with the file and the reason) on anything inconsistent.
The file is the defaults: the Settings page's edits are kept in the CAD database and laid over it
(sizeup/cad/settings.py) before build() checks the result the same way. An item can be retired
("retired": true): still named on old events, off the pick lists (codes() is the active ones).

Units answer one of three ways (units.<code>.response, else from the flags): "fire" (PCFD's and the
joint-response department's apparatus: run cards by kind, "tick all", the dispatch), "mutual_aid"
(another department's: only when named or picked by hand) and "agency" (EMS, PD, utilities: the
unit_kinds marked attach_only; never on a run card, never dispatched or said on the air, only
attached to an event when they show up, with a short status set).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = ROOT / "data" / "cad_lists.json"
ROLES = ("free", "oos", "dispatched", "enroute", "onscene")
EVENT_STATUSES = ("queued", "pending", "open", "active", "closed", "dismissed")   # open: non-emergency, no units
CLASSES = ("emergency", "non-emergency")
CODE = re.compile(r"[A-Z0-9][A-Z0-9_\-]{0,23}")                  # types, subtypes, unit codes
LONG_CODE = re.compile(r"[A-Z0-9][A-Z0-9 _/\-]{0,39}")           # dispositions, statuses, received via
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
RESPONSES = ("fire", "mutual_aid", "agency")
LIST_SECTIONS = ("event_types", "event_subtypes", "dispositions", "received_via", "unit_statuses")


class ListsError(ValueError):
    """The data file is missing, not JSON, or inconsistent."""


def key(typ: str, subtype: str = "") -> str:
    """'ALARM', 'COMM' -> 'ALARM/COMM' (how run cards, colors and spoken phrases are keyed)."""
    return f"{typ}/{subtype}" if subtype else typ


@dataclass
class Lists:
    event_types: tuple[dict, ...]            # {"code", "name", "color"}
    event_subtypes: tuple[dict, ...]         # {"code", "name"}
    type_subtypes: dict[str, tuple[str, ...]]
    dispositions: tuple[dict, ...]
    received_via: tuple[dict, ...]
    event_class: dict[str, str]              # "NOTIF" / "*/WATER" / "RESCUE/WATER" -> emergency | non-emergency
    unit_statuses: tuple[dict, ...]          # {"code", "name", "role", "bg", "fg"}
    event_statuses: dict[str, dict]          # status -> {"name", "bg", "fg"}
    unit_kinds: tuple[dict, ...]             # {"id", "label"}
    unit_boards: tuple[dict, ...]            # {"id", "title", "units": [code | None]}
    units: dict[str, dict]
    spoken: dict[str, str]
    run_cards: tuple[dict, ...]              # {"type": key, "slots": [...]}
    geo_town_order: tuple[str, ...]
    geo_town_aliases: dict[str, str]
    unit_event_cell: dict = field(default_factory=dict)
    wording: dict = field(default_factory=dict)            # dispatcher.WORDING keys -> template
    joint_response: dict = field(default_factory=dict)     # town -> {"tag", "units": (codes), "quick_add": (codes)}
    already_statuses: tuple[str, ...] = ()                 # add-on: units on the event in these are said
    full_response_kinds: tuple[str, ...] = ()              # "Tick all available PCFD apparatus"
    status_flow: dict = field(default_factory=dict)        # status | "*" -> (next statuses), the incident's unit buttons
    lead_in_joint_only: dict = field(default_factory=dict) # town -> the lead-in when only its units go
    dispatch_tones: dict = field(default_factory=dict)     # joint town -> [(hz, s), ...] | None (not known yet)
    path: str = ""
    _names: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        for k in LIST_SECTIONS:
            self._names[k] = {x["code"]: x["name"] for x in getattr(self, k)}

    # -- codes and names ----------------------------------------------------------------------------

    def codes(self, which: str) -> tuple[str, ...]:
        """The active codes (what can be picked now); a retired one is still named (name())."""
        return tuple(x["code"] for x in getattr(self, which) if not x.get("retired"))

    def retired(self, which: str, code: str) -> bool:
        return any(x["code"] == code and x.get("retired") for x in getattr(self, which))

    def name(self, which: str, code: str) -> str:
        return self._names[which].get(code, code)

    @property
    def type_codes(self) -> tuple[str, ...]:
        return self.codes("event_types")

    @property
    def subtype_codes(self) -> tuple[str, ...]:
        return self.codes("event_subtypes")

    @property
    def disposition_codes(self) -> tuple[str, ...]:
        return self.codes("dispositions")

    @property
    def received_codes(self) -> tuple[str, ...]:
        return self.codes("received_via")

    def type_label(self, typ: str, subtype: str = "") -> str:
        """As the console shows it: 'Service / Boat'."""
        if not typ:
            return f"? / {self.name('event_subtypes', subtype)}" if subtype else ""
        t = self.name("event_types", typ)
        return f"{t} / {self.name('event_subtypes', subtype)}" if subtype else t

    # -- types ------------------------------------------------------------------------------------

    def subtypes_for(self, typ: str) -> tuple[str, ...]:
        sub = self.type_subtypes.get(typ)
        return tuple(s for s in sub if s in self.subtype_codes) if sub else self.subtype_codes

    def check_type(self, typ: str, subtype: str = "", required: bool = True) -> None:
        """Raises ValueError with the reason for a type / subtype code that isn't on the lists."""
        if not typ:
            if required:
                raise ValueError("type: required (one of the event types)")
            if subtype:
                raise ValueError("subtype: pick the type first")
            return
        if typ not in self.type_codes:
            if self.retired("event_types", typ):
                raise ValueError(f"type: {self.type_label(typ)} ({typ}) is retired")
            raise ValueError(f"type: {typ!r} is not an event type")
        if subtype and subtype not in self.subtypes_for(typ):
            if self.retired("event_subtypes", subtype):
                raise ValueError(f"subtype: {self.name('event_subtypes', subtype)} ({subtype}) is retired")
            raise ValueError(f"subtype: {subtype!r} is not an event subtype for {self.type_label(typ)}")

    def check_key(self, k: str) -> tuple[str, str]:
        """'ALARM/COMM' -> ('ALARM', 'COMM'), checked."""
        typ, _, sub = (k or "").partition("/")
        self.check_type(typ, sub)
        return typ, sub

    def phrase(self, typ: str, subtype: str = "", base: str = "") -> str:
        """As the dispatch voice says it: 'a commercial fire alarm'. TYPE/SUBTYPE, then */SUBTYPE, then
        TYPE; in a subtype's phrase {type} is the type's ('{type} with multiple injuries' -> 'a motor
        vehicle accident with multiple injuries'). base: the type's phrase from elsewhere (a run card)."""
        def generic(t: str) -> str:
            return ("an " if t[:1] in "aeiou" else "a ") + t
        sp = ((self.spoken.get(key(typ, subtype)) or self.spoken.get(f"*/{subtype}")) if subtype else None)
        if sp:
            return sp.replace("{type}", base or self.spoken.get(typ) or generic(self.type_label(typ).lower()))
        p = base or self.spoken.get(typ)
        if p:
            return p
        if not typ:
            return "a call for service"
        return generic(self.type_label(typ, subtype).replace(" / ", ", ").lower())

    def is_emergency(self, typ: str, subtype: str = "") -> bool:
        """TYPE/SUBTYPE, then */SUBTYPE, then TYPE; not listed = emergency."""
        for k in ([key(typ, subtype), f"*/{subtype}"] if subtype else []) + [typ]:
            if k in self.event_class:
                return self.event_class[k] == "emergency"
        return True

    # -- unit statuses ----------------------------------------------------------------------------

    @property
    def status_ids(self) -> tuple[str, ...]:
        return self.codes("unit_statuses")

    def role(self, status: str) -> str:
        """'' for a status that keeps the unit committed (acknowledged, dispatch other...)."""
        return next((s.get("role", "") for s in self.unit_statuses if s["code"] == status), "")

    def status_for(self, role: str) -> str:
        """The first status with that role: 'free' -> 'AVAILABLE', 'dispatched' -> 'DISPATCHED'."""
        return next(s["code"] for s in self.unit_statuses if s.get("role") == role and not s.get("retired"))

    @property
    def free(self) -> tuple[str, ...]:
        return tuple(s["code"] for s in self.unit_statuses if s.get("role") == "free" and not s.get("retired"))

    def agency_statuses(self) -> tuple[str, ...]:
        """Another agency's unit on an event (EMS, PD): on location, then cleared (the free status)."""
        return tuple(self.status_for(r) for r in ("onscene", "free") if any(
            s.get("role") == r and not s.get("retired") for s in self.unit_statuses))

    def next_statuses(self, status: str) -> tuple[str, ...]:
        """The buttons on a unit that is on an incident (status_flow): what can come next. Without a
        flow in the data file: every status but the current one."""
        if not self.status_flow:
            return tuple(c for c in self.status_ids if c != status)
        return tuple(c for c in self.status_flow.get(status, self.status_flow.get("*", ())) if c != status)

    def status_phrase(self, status: str) -> str:
        """'On location' -> 'on location' (for 'E63 on location')."""
        n = self.name("unit_statuses", status)
        return n[:1].lower() + n[1:] if n[1:2].islower() or len(n) < 2 else n

    # -- units ------------------------------------------------------------------------------------

    def board_units(self) -> list[tuple[str, str, int]]:
        """(code, board id, sort) for every unit on the boards, in board order."""
        out, k = [], 0
        for b in self.unit_boards:
            for code in b["units"]:
                if code:
                    out.append((code, b["id"], k))
                    k += 1
        return out

    def unit(self, code: str) -> dict:
        u = self.units.get(code, {})
        r = self.response(code)
        return {"kind": u.get("kind", "agency"), "spoken": u.get("spoken") or code, "title": u.get("title", ""),
                "spare": bool(u.get("spare")), "department": u.get("department", ""),
                "mutual_aid": r == "mutual_aid", "response": r, "joint": self.joint_town(code)}

    @property
    def attach_kinds(self) -> tuple[str, ...]:
        """Kinds that are other agencies (EMS, PD, utilities): attached, never dispatched."""
        return tuple(k["id"] for k in self.unit_kinds if k.get("attach_only"))

    def response(self, code: str) -> str:
        """fire | mutual_aid | agency (see the module doc)."""
        return unit_response(self.units.get(code, {}), self.attach_kinds)

    def station(self, code: str) -> str:
        """The firehouse the data file houses it in ('' = not set: the department hasn't said)."""
        return (self.units.get(code) or {}).get("station") or ""

    def joint_town(self, code: str) -> str:
        """'E14' -> 'Rye Brook': the town whose department answers jointly with PCFD ('' = PCFD's own or other)."""
        return next((t for t, j in self.joint_response.items() if code in j["units"]), "")

    @property
    def kind_ids(self) -> tuple[str, ...]:
        return tuple(k["id"] for k in self.unit_kinds)

    # -- towns ------------------------------------------------------------------------------------

    def town_of(self, municipality: str) -> str:
        m = (municipality or "").strip()
        return self.geo_town_aliases.get(m.lower(), m)

    def towns_typed(self, text: str) -> list[str]:
        """Towns named in what was typed, longest phrase first ('rye brook' is not also 'rye')."""
        t = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
        out = []
        for alias in sorted(self.geo_town_aliases, key=len, reverse=True):
            if f" {alias} " in t:
                t = t.replace(f" {alias} ", " | ")
                town = self.geo_town_aliases[alias]
                if town not in out:
                    out.append(town)
        return out

    def event_town(self, inc: dict) -> str:
        """Where an event is: its map point's town (the label ends ', Rye Brook'), else a town typed in
        the location ('940 King St, Rye Brook'), else ''."""
        if inc.get("lat") is not None and "," in (inc.get("geo_label") or ""):
            return self.town_of(inc["geo_label"].rsplit(",", 1)[1])
        typed = self.towns_typed(inc.get("address") or "")
        return typed[0] if typed else ""

    def joint_call(self, inc: dict) -> str:
        """'Rye Brook' when the event is in a joint-response town (their call: their units go first)."""
        t = self.event_town(inc)
        return t if t in self.joint_response else ""

    def town_rank(self, municipality: str) -> int:
        town = self.town_of(municipality)
        return self.geo_town_order.index(town) if town in self.geo_town_order else len(self.geo_town_order)

    # -- for the page -------------------------------------------------------------------------------

    def public(self) -> dict:
        return {"event_types": [dict(x) for x in self.event_types],
                "event_subtypes": [dict(x) for x in self.event_subtypes],
                "type_subtypes": {k: list(v) for k, v in self.type_subtypes.items()},
                "dispositions": [dict(x) for x in self.dispositions],
                "received_via": [dict(x) for x in self.received_via], "event_class": self.event_class,
                "unit_statuses": [dict(s) for s in self.unit_statuses], "event_statuses": self.event_statuses,
                "agency_statuses": list(self.agency_statuses()),
                "unit_event_cell": self.unit_event_cell, "unit_kinds": [dict(k) for k in self.unit_kinds],
                "unit_boards": [dict(b, units=list(b["units"])) for b in self.unit_boards],
                "spoken": self.spoken, "geo_town_order": list(self.geo_town_order),
                "joint_response": {t: {"tag": j["tag"], "units": list(j["units"]), "quick_add": list(j["quick_add"])}
                                   for t, j in self.joint_response.items()},
                "full_response": self.wording.get("full_response", ""),
                "full_response_kinds": list(self.full_response_kinds),
                "status_flow": {k: list(v) for k, v in self.status_flow.items()},
                "source": _shown_path(self.path)}


def unit_response(u: dict, attach_kinds) -> str:
    r = u.get("response")
    if r in RESPONSES:
        return r
    if u.get("mutual_aid"):
        return "mutual_aid"
    return "agency" if u.get("kind", "agency") in attach_kinds else "fire"


def _shown_path(p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return p


def _items(d: dict, k: str, where: str, pattern: re.Pattern, extra=()) -> tuple[dict, ...]:
    """A list of {code, name, ...} (or plain strings: code = name), codes unique and well formed."""
    v = d.get(k)
    if not isinstance(v, list) or not v:
        raise ListsError(f"{where}: {k} must be a non-empty list")
    out, seen = [], set()
    for i, x in enumerate(v):
        if isinstance(x, str):
            x = {"code": x}
        if not isinstance(x, dict) or not isinstance(x.get("code"), str):
            raise ListsError(f"{where}: {k}[{i}]: a code (text) or {{code, name}}")
        code = x["code"].strip()
        if not pattern.fullmatch(code):
            raise ListsError(f"{where}: {k}[{i}]: {code!r} is not an UPPERCASE code")
        if code in seen:
            raise ListsError(f"{where}: {k} lists {code!r} twice")
        seen.add(code)
        item = {"code": code, "name": str(x.get("name") or code).strip()}
        if x.get("retired"):
            item["retired"] = True
        for e in extra:
            if e in x and x[e] not in (None, ""):
                item[e] = x[e]
        for c in ("color", "bg", "fg"):
            if c in item and not (isinstance(item[c], str) and COLOR.fullmatch(item[c])):
                raise ListsError(f"{where}: {k}[{i}].{c}: #rrggbb")
        out.append(item)
    return tuple(out)


def read(path: str | Path | None = None) -> dict:
    """The data file as it is (the defaults), unchecked."""
    path = Path(path) if path else DEFAULT_PATH
    try:
        d = json.loads(path.read_text())
    except OSError as e:
        raise ListsError(f"{path}: cannot read ({e.strerror or e})") from None
    except ValueError as e:
        raise ListsError(f"{path}: not JSON ({e})") from None
    if not isinstance(d, dict):
        raise ListsError(f"{path}: a JSON object expected")
    return d


def load(path: str | Path | None = None) -> Lists:
    path = Path(path) if path else DEFAULT_PATH
    return build(read(path), str(path), path)


def build(d: dict, where: str, path: str | Path = "") -> Lists:
    """The lists from the file's contents (or the defaults with the Settings edits laid over)."""
    types = _items(d, "event_types", where, CODE, ("color",))
    subtypes = _items(d, "event_subtypes", where, CODE)
    dispositions = _items(d, "dispositions", where, LONG_CODE)
    received = _items(d, "received_via", where, LONG_CODE)
    statuses = _items(d, "unit_statuses", where, LONG_CODE, ("role", "bg", "fg"))
    type_codes, sub_codes = {t["code"] for t in types}, {s["code"] for s in subtypes}

    ts = d.get("type_subtypes") or {}
    if not isinstance(ts, dict):
        raise ListsError(f"{where}: type_subtypes must be an object (TYPE -> [SUBTYPE, ...])")
    type_subtypes = {}
    for t, subs in ts.items():
        if t not in type_codes:
            raise ListsError(f"{where}: type_subtypes: {t!r} is not in event_types")
        if not isinstance(subs, list) or any(s not in sub_codes for s in subs):
            raise ListsError(f"{where}: type_subtypes[{t!r}]: a list of event_subtypes codes")
        type_subtypes[t] = tuple(subs)

    def check_key(k: str, what: str) -> None:
        t, _, s = k.partition("/")
        if t not in type_codes or (s and s not in sub_codes):
            raise ListsError(f"{where}: {what}: {k!r} is not TYPE or TYPE/SUBTYPE from the lists")

    event_class = {}
    for k, v in (d.get("event_class") or {}).items():
        if v not in CLASSES:
            raise ListsError(f"{where}: event_class[{k!r}]: emergency or non-emergency")
        if k.startswith("*/"):
            if k[2:] not in sub_codes:
                raise ListsError(f"{where}: event_class: {k!r}: {k[2:]!r} is not in event_subtypes")
        else:
            check_key(k, "event_class")
        event_class[k] = v

    for i, s in enumerate(statuses):
        if s.get("role", "") not in ("",) + ROLES:
            raise ListsError(f"{where}: unit_statuses[{i}].role: one of {', '.join(ROLES)} (or none)")
    for need in ("free", "oos", "dispatched"):
        if not any(s.get("role") == need and not s.get("retired") for s in statuses):
            raise ListsError(f"{where}: unit_statuses: one status needs role {need!r}")

    es = d.get("event_statuses") or {}
    event_statuses = {}
    for st in EVENT_STATUSES:
        v = es.get(st) or {}
        event_statuses[st] = {"name": str(v.get("name") or st.capitalize()), "bg": str(v.get("bg") or ""),
                              "fg": str(v.get("fg") or "")}

    kinds = d.get("unit_kinds") or []
    if not kinds or not all(isinstance(k, dict) and isinstance(k.get("id"), str) for k in kinds):
        raise ListsError(f"{where}: unit_kinds: a list of {{id, label}}")
    kind_ids = {k["id"] for k in kinds}
    if any("|" in k for k in kind_ids):
        raise ListsError(f"{where}: unit_kinds: an id can't contain |")
    attach_kinds = {k["id"] for k in kinds if k.get("attach_only")}

    boards, seen = [], set()
    for i, b in enumerate(d.get("unit_boards") or []):
        if not isinstance(b, dict) or not isinstance(b.get("units"), list):
            raise ListsError(f"{where}: unit_boards[{i}]: an object with a units list")
        for code in b["units"]:
            if code is None:
                continue
            if not isinstance(code, str) or not CODE.fullmatch(code):
                raise ListsError(f"{where}: unit_boards[{i}]: {code!r} is not a unit code")
            if code in seen:
                raise ListsError(f"{where}: unit_boards: {code!r} is on the boards twice")
            seen.add(code)
        boards.append({"id": str(b.get("id") or f"board{i}"), "title": str(b.get("title") or ""), "units": list(b["units"])})
    if not seen:
        raise ListsError(f"{where}: unit_boards: no units")

    units = d.get("units") or {}
    for code, u in units.items():
        if not isinstance(u, dict):
            raise ListsError(f"{where}: units[{code!r}]: an object")
        if u.get("kind", "agency") not in kind_ids:
            raise ListsError(f"{where}: units[{code!r}].kind: one of {', '.join(sorted(kind_ids))}")
        if u.get("response") not in (None, "") + RESPONSES:
            raise ListsError(f"{where}: units[{code!r}].response: one of {', '.join(RESPONSES)}")
        if not isinstance(u.get("station", ""), str):
            raise ListsError(f"{where}: units[{code!r}].station: a firehouse name (text)")

    def agency(code: str) -> bool:
        return unit_response(units.get(code, {}), attach_kinds) == "agency"

    spoken = d.get("spoken") or {}
    for k, v in spoken.items():
        if k.startswith("*/"):
            if k[2:] not in sub_codes:
                raise ListsError(f"{where}: spoken: {k!r}: {k[2:]!r} is not in event_subtypes")
        else:
            check_key(k, "spoken")
        if not isinstance(v, str) or not v.strip():
            raise ListsError(f"{where}: spoken[{k!r}]: text")
        if "{" in v.replace("{type}", "") or "}" in v.replace("{type}", "") or ("{type}" in v and "/" not in k):
            raise ListsError(f"{where}: spoken[{k!r}]: the only placeholder is {{type}}, in a subtype's phrase")

    cards = d.get("run_cards") or []
    for i, c in enumerate(cards):
        if not isinstance(c, dict) or not isinstance(c.get("slots"), list):
            raise ListsError(f"{where}: run_cards[{i}]: an object with type and slots")
        check_key(str(c.get("type", "")), f"run_cards[{i}].type")
        for s in c["slots"]:
            if s.get("unit") and s["unit"] not in seen:
                raise ListsError(f"{where}: run_cards[{i}]: unit {s['unit']!r} is not on the boards")
            if s.get("unit") and agency(s["unit"]):
                raise ListsError(f"{where}: run_cards[{i}]: {s['unit']!r} is another agency's unit (never on a run card)")
            if not s.get("unit") and not set(str(s.get("kind", "")).split("|")) <= kind_ids - attach_kinds:
                raise ListsError(f"{where}: run_cards[{i}]: kind {s.get('kind')!r} is not a fire apparatus kind in unit_kinds")

    from sizeup.cad.dispatcher import WORDING
    dw = d.get("dispatch_wording") or {}
    wording = {}
    dummy = dict.fromkeys(("department", "units", "location", "type", "type_bare", "already", "info", "change",
                           "respond", "time", "full", "Full", "status", "remarks", "joint"), "x")
    for k, v in dw.items():
        if k in WORDING:
            if not isinstance(v, str) or not v.strip():
                raise ListsError(f"{where}: dispatch_wording.{k}: text")
            try:
                v.format(**dummy)
            except (KeyError, IndexError, ValueError) as e:
                raise ListsError(f"{where}: dispatch_wording.{k}: unknown or broken placeholder ({e})") from None
            wording[k] = v
    codes = {s["code"] for s in statuses}
    flow: dict[str, tuple[str, ...]] = {}
    for k, v in (d.get("status_flow") or {}).items():
        if (k != "*" and k not in codes) or not isinstance(v, list) or not set(v) <= codes:
            raise ListsError(f"{where}: status_flow[{k!r}]: a unit_statuses code (or \"*\") -> a list of unit_statuses codes")
        flow[k] = tuple(v)
    already = tuple(dw.get("already_statuses") or ())
    if not set(already) <= {s["code"] for s in statuses}:
        raise ListsError(f"{where}: dispatch_wording.already_statuses: codes from unit_statuses")
    fr_kinds = tuple(dw.get("full_response_kinds") or ())
    if not set(fr_kinds) <= kind_ids - attach_kinds:
        raise ListsError(f"{where}: dispatch_wording.full_response_kinds: fire apparatus ids from unit_kinds")

    joint = {}
    for town, j in (d.get("joint_response") or {}).items():
        if isinstance(j, list):
            j = {"units": j}
        if not isinstance(j, dict) or not isinstance(j.get("units"), list) or not j["units"]:
            raise ListsError(f"{where}: joint_response[{town!r}]: a list of unit codes, or {{tag, units, quick_add}}")
        codes = tuple(str(c) for c in j["units"])
        quick = tuple(str(c) for c in (j.get("quick_add") or codes))
        for c in codes + quick:
            if c not in units:
                raise ListsError(f"{where}: joint_response[{town!r}]: {c!r} is not in units")
            if unit_response(units[c], attach_kinds) != "fire":
                raise ListsError(f"{where}: joint_response[{town!r}]: {c!r} is mutual aid or an agency (joint units are direct)")
        if not set(quick) <= set(codes):
            raise ListsError(f"{where}: joint_response[{town!r}].quick_add: codes from its units")
        tag = str(j.get("tag") or "".join(w[0] for w in str(town).split()).upper())
        joint[str(town)] = {"tag": tag, "units": codes, "quick_add": quick}

    only = dw.get("lead_in_joint_only") or {}
    if not isinstance(only, dict) or not all(t in joint and isinstance(v, str) and v.strip() for t, v in only.items()):
        raise ListsError(f"{where}: dispatch_wording.lead_in_joint_only: joint_response town -> text")
    tones = {}
    for town, t in (d.get("dispatch_tones") or {}).items():
        ok = t is None or (isinstance(t, list) and 0 < len(t) <= 8 and all(
            isinstance(x, list) and len(x) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in x)
            and 100 <= x[0] <= 4000 and 0 < x[1] <= 10 for x in t))
        if town not in joint or not ok:
            raise ListsError(f"{where}: dispatch_tones[{town!r}]: a joint_response town -> null or [[hz, seconds], ...]")
        tones[town] = [tuple(float(v) for v in x) for x in t] if t else None

    order = tuple(str(t) for t in d.get("geo_town_order") or [])
    aliases = {str(k).lower(): str(v) for k, v in (d.get("geo_town_aliases") or {}).items()}
    for t in order:
        aliases.setdefault(t.lower(), t)

    return Lists(event_types=types, event_subtypes=subtypes, type_subtypes=type_subtypes, dispositions=dispositions,
                 received_via=received, event_class=event_class, unit_statuses=statuses, event_statuses=event_statuses,
                 unit_kinds=tuple({"id": k["id"], "label": str(k.get("label") or k["id"]), "attach_only": bool(k.get("attach_only"))}
                                  for k in kinds),
                 unit_boards=tuple(boards), units=units, spoken=dict(spoken), run_cards=tuple(cards),
                 geo_town_order=order, geo_town_aliases=aliases, unit_event_cell=dict(d.get("unit_event_cell") or {}),
                 wording=dict(WORDING, **wording), already_statuses=already, joint_response=joint, full_response_kinds=fr_kinds,
                 status_flow=flow,
                 lead_in_joint_only={str(t): str(v) for t, v in only.items()}, dispatch_tones=tones,
                 path=str(path))
