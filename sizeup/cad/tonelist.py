"""The tone book (Settings > Radios > Tones, #115, #122): agencies, each its places and a service, with their tone pairs.

An entity is one or more **places** (municipalities or communities: Port Chester, Mamaroneck Village, Armonk) and a
**service** (fire | ems | police | other), shown "Port Chester · Fire" or "Port Chester · Rye · Rye Brook · EMS" (user,
2026-10-14: "only <village/town name> and then <service category>", and "their tones should be multiple places
combined with the service category"). Places keep the order given; a place can be in several entities (Rye · Fire and
Port Chester · Rye · Rye Brook · EMS). It has aliases (the names it has gone by: "Port Chester FD", "PCVAC"), ours and
notes. Each of its pairs (A/B Hz) has a role, a free label ("All call", "Chiefs", "Station 1") shown after the service
("Port Chester · Fire · Chiefs"); a source, confirmed or estimated, last heard, times heard.

Built at load, never written into the tone list (data/westchester_tones.csv; [[tones]] in the config): every row's name
is read as places + service (parse_name: "Mamaroneck Village FD" -> Mamaroneck Village, fire; "PCVAC" -> Port Chester,
Rye, Rye Brook, ems from data/westchester_ems.csv; a name with no service word -> fire, said so in the report), the rows
of the same places + service are one entity, their names kept as aliases. What an admin changes is kept as overrides
in the Settings section "tones" (sizeup/cad/settings.py):

    key "832.5/1201.0"               a pair added here ("Name these tones", #80, or Add pair):
                                     {a_hz, b_hz, places, service, role?, confirmed?, page?}; #84's / #115's entries
                                     {name, a_hz, b_hz, ours, page?, type?, confirmed?} are still read (the name parsed)
    key "435.1/435.1@Larchmont FD"   an edit to that row of the tone list (A/B Hz @ its name there): {list, a_hz, b_hz,
                                     places, service, role?, confirmed?} (moved to another entity, split into a new one,
                                     given a role) or #115's {list, name, ...}; {list, a_hz, b_hz, deleted: true}: taken out
    key "entity:<places>|<service>"  an entity (places casefolded, sorted, "+"-joined): {places, service, ours?, notes?,
                                     aliases?, merged_into?: {places, service}} (merged: its pairs are the other's)

confirmed: false = an estimate (the plan tags the name " (est.)", which a heard or listed one outranks). Ours is the
entity's: set here, else a pair named ours (#84), and PCFD's and Rye Brook's always (the config's names, locked; user:
the Port Chester-Rye-Rye Brook EMS is not ours). A pair under two entities (within the plan's tolerance) is a conflict:
it matches both until someone moves, merges or takes one out, and the page shows both.

Heard: the pages in the CAD's communications log (radio_traffic.tones, 90 days unlinked) and unknown_tones.jsonl
(every page with a pair nobody's code matched, #72), one count per page. A pair heard that the plan doesn't match is
"heard but unnamed": decodes of one pair (within the plan's tolerance) are one row, with its latest page to play.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from sizeup.cad.store import CadError
from sizeup.radio.tones import SERVICE_LABELS, ToneCode, TonePlan, base_name, pair_key

SERVICES = ("fire", "ems", "police", "other")
TYPES = SERVICES
LABEL = dict(zip(SERVICES, SERVICE_LABELS))
TONE_HZ = (250.0, 3500.0)          # the decoder's band (radio/tones.py)
TAGS = (" (heard)", " (est.)")
SEP = " · "
MAX_UNNAMED = 100
MAX_PLACES = 8
DATA = Path(__file__).resolve().parent.parent.parent / "data"
ENTITY = "entity:"

# How the county's files spell a place the tone list shortens (westchester_departments.csv's notes: 'apparatus list
# says "Briarcliff"', "Croton", "Mohegan").
PLACE_ALIASES = {"briarcliff": "Briarcliff Manor", "croton": "Croton-on-Hudson", "mohegan": "Lake Mohegan",
                 "mohegan lake": "Lake Mohegan", "pc": "Port Chester", "hastings-on-hudson": "Hastings"}
# The service a name ends with (the longest first). "FD/EMS": the EMS side of a combined department.
SERVICE_WORDS = [
    ("ems", r"fd/ems|volunteer ambulance corps|ambulance corps|ambulance|ems|vac|medics?|als"),
    ("fire", r"volunteer fire (?:department|dept\.?|company|co\.?)|vol\.? fire|fire department|fire dept\.?|"
             r"fire district|fire co\.?|fire|fd|vfd"),
    ("police", r"police department|police dept\.?|police|pd"),
    ("other", r"des|oem|dpw|correctional facility"),
]
SERVICE_RE = [(s, re.compile(rf"[\s,-]*\b(?:{w})$", re.I)) for s, w in SERVICE_WORDS]
BAD_PLACE = re.compile(r"[|+@·]|\((?:heard|est\.)\)")


@dataclass(frozen=True)
class Parsed:
    places: tuple[str, ...]
    service: str
    role: str = ""
    sure: bool = True
    why: str = ""                   # what was assumed, for the report


def display(places: Iterable[str], service: str, role: str = "") -> str:
    return SEP.join([*places, LABEL.get(service, service)]) + (f"{SEP}{role}" if role else "")


def ekey(places: Iterable[str], service: str) -> str:
    return "+".join(sorted({" ".join(p.split()).casefold() for p in places})) + f"|{service}"


def canon_place(place: str) -> str:
    p = " ".join(place.replace("_", " ").split()).strip(" ,-")
    return PLACE_ALIASES.get(p.casefold(), p)


def tag_of(name: str) -> str:
    return next((t for t in TAGS if name.endswith(t)), "")


def list_key(c: ToneCode) -> str:
    """A tone-list row as the override that edits it names it: 'A/B@its name there'."""
    return f"{pair_key(c.a_hz, c.b_hz)}@{base_name(c.name)}"


def is_list_key(key: str) -> bool:
    return "@" in key and not key.startswith(ENTITY)


def _rows(path: Path) -> list[dict]:
    try:
        text = path.read_text()
    except OSError:
        return []
    return list(csv.DictReader(ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")))


@lru_cache(maxsize=4)
def known_names(data: Path = DATA) -> dict[str, tuple[tuple[str, ...], str, str, str]]:
    """Agency names the county's files give places and a service for: casefold name -> (places, service, source,
    the name as written)."""
    out: dict[str, tuple[tuple[str, ...], str, str, str]] = {}
    for r in _rows(data / "westchester_ems.csv"):
        places = tuple(canon_place(p) for p in (r.get("places") or "").split("+") if p.strip())
        svc = (r.get("service") or "").strip()
        if not places or svc not in SERVICES:
            continue
        for n in [display(places, svc)] + [a for a in (r.get("aliases") or "").split("|") if a.strip()]:
            out.setdefault(n.strip().casefold(), (places, svc, r.get("source") or "", n.strip()))
    return out


@lru_cache(maxsize=4)
def _county_place_set(data: Path = DATA) -> frozenset[str]:
    """The departments' names as they are (most are places) and the EMS file's places, casefolded."""
    out = {canon_place(r.get("department") or "").casefold() for r in _rows(data / "westchester_departments.csv")}
    out |= {canon_place(p).casefold() for r in _rows(data / "westchester_ems.csv")
            for p in (r.get("places") or "").split("+") if p.strip()}
    return frozenset(x for x in out if x)


def _split_label(full: str) -> tuple[tuple[str, ...], str, str] | None:
    """"Port Chester · Rye · EMS · Chiefs" -> (places, service, role); None when no part is a service."""
    parts = [p.strip() for p in full.split(SEP.strip())]
    for i, p in enumerate(parts):
        svc = next((s for s, lab in LABEL.items() if lab.casefold() == p.casefold()), None)
        if svc and i > 0:
            return tuple(canon_place(x) for x in parts[:i]), svc, " ".join(parts[i + 1:]).strip()
    return None


def parse_name(name: str, data: Path = DATA) -> Parsed:
    """A name as places + service (+ role): "Mamaroneck Village FD" -> Mamaroneck Village, fire; "PCVAC" -> Port
    Chester, Rye, Rye Brook, ems (westchester_ems.csv); "Port Chester · Fire · Chiefs" as it reads. A name with no
    service word is Fire, not sure; "A-B <service>" with A and B both county places is both, not sure."""
    full = " ".join((name or "").removesuffix(" (heard)").removesuffix(" (est.)").split())
    lab = _split_label(full) if SEP.strip() in full else None
    if lab:
        return Parsed(lab[0], lab[1], lab[2])
    n = full
    hit = known_names(data).get(n.casefold())
    if hit:
        sure = hit[2] != "local"
        why = "" if sure else f"{n!r}: {display(hit[0], hit[1])} (to verify)"
        if sure and len(hit[0]) > 1:
            why = f"{n!r}: {display(hit[0], hit[1])} (combined)"
        return Parsed(hit[0], hit[1], "", sure, why)
    m = re.fullmatch(r"(.+?)\s+volunteer", n, re.I)
    if m:                                                         # "Peekskill Volunteer": which volunteers isn't said
        return Parsed((canon_place(m[1]),), "fire", "Volunteer", False,
                      f"{n!r}: Fire assumed, role Volunteer (an ambulance corps?)")
    rest, svc = n, None
    for s, rx in SERVICE_RE:
        mm = rx.search(rest)
        if mm and mm.start() > 0:
            rest, svc = rest[:mm.start()], s
            break
    if svc is not None:
        for _s, rx in SERVICE_RE:                                 # "Greenburgh Police EMS": the place alone
            mm = rx.search(rest)
            if mm and mm.start() > 0:
                rest = rest[:mm.start()]
    whys = []
    place = canon_place(rest)
    if place.casefold() != rest.strip(" ,-").casefold():
        whys.append(f"place read as {place!r}")
    places: tuple[str, ...] = (place,)
    known = _county_place_set(data)
    if "-" in place and place.casefold() not in known:
        parts = [canon_place(x) for x in place.split("-")]
        if len(parts) > 1 and all(p.casefold() in known for p in parts):
            places = tuple(parts)
            whys.append(f"combined: {', '.join(parts)}")
    if svc is None:
        whys.insert(0, "no service word, Fire assumed")
        return Parsed(places, "fire", "", False, f"{n!r}: " + "; ".join(whys))
    return Parsed(places, svc, "", len(places) == 1, (f"{n!r}: " + "; ".join(whys)) if whys else "")


# -- an override, checked ---------------------------------------------------------------------------------------

def _text(v, what: str, n: int, required: bool = True) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise CadError(f"{what}: text")
    v = " ".join(v.split())
    if required and not v:
        raise CadError(f"{what}: required")
    if len(v) > n:
        raise CadError(f"{what}: at most {n} characters")
    return v


def _places(v: dict, what: str) -> list[str]:
    """places: [names] (or place: a name), each a place alone, once each, in the order given."""
    raw = v.get("places")
    if raw is None and v.get("place") not in (None, ""):
        raw = [v.get("place")]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PLACES:
        raise CadError(f"{what}places: 1 to {MAX_PLACES} place names")
    out: list[str] = []
    for i, x in enumerate(raw):
        p = canon_place(_text(x, f"{what}places[{i}]", 60))
        if BAD_PLACE.search(p) or any(LABEL[s].casefold() == p.casefold() for s in SERVICES):
            raise CadError(f"{what}places[{i}]: a place's name alone")
        if p.casefold() not in {o.casefold() for o in out}:
            out.append(p)
    return out


def _service(v, what: str) -> str:
    if v not in SERVICES:
        raise CadError(f"{what}: {', '.join(SERVICES)}")
    return v


def clean(v, where: str = "") -> tuple[str, dict]:
    """An override value checked -> (its key, the value stored). Only the fields given are kept (a #84 or #115 entry
    stays as it was, so its signature and audit don't change). CadError."""
    what = f"{where}: " if where else ""
    if not isinstance(v, dict):
        raise CadError(f"{what}an object {{a_hz, b_hz, places, service}} or an entity {{places, service}}")
    if "a_hz" not in v:
        return _clean_entity(v, what)
    hz = {}
    for k in ("a_hz", "b_hz"):
        x = v.get(k)
        if k == "b_hz" and x in (None, ""):
            hz[k] = None
            continue
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not TONE_HZ[0] <= x <= TONE_HZ[1]:
            raise CadError(f"{what}{k}: {TONE_HZ[0]:.0f} to {TONE_HZ[1]:.0f} Hz")
        hz[k] = round(float(x), 1)
    key = pair_key(hz["a_hz"], hz["b_hz"])
    listed = _text(v.get("list"), f"{what}list", 60, required=False)
    if listed:
        key += f"@{listed}"
    deleted = v.get("deleted", False)
    if not isinstance(deleted, bool):
        raise CadError(f"{what}deleted: true or false")
    if deleted:
        if not listed:
            raise CadError(f"{what}deleted: only a row of the tone list (list: its name there)")
        return key, {"list": listed, "a_hz": hz["a_hz"], "b_hz": hz["b_hz"], "deleted": True}
    out: dict = {"list": listed} if listed else {}
    if "name" in v and v.get("places") is None and v.get("place") in (None, ""):     # #84 / #115: a name
        name = _text(v.get("name"), f"{what}name", 60)
        if tag_of(name):
            raise CadError(f"{what}name: the department's name alone")
        out.update({"name": name, "a_hz": hz["a_hz"], "b_hz": hz["b_hz"]})
    else:                                                                             # #122: places + service
        out["places"] = _places(v, what)
        out["service"] = _service(v.get("service"), f"{what}service")
        out.update({"a_hz": hz["a_hz"], "b_hz": hz["b_hz"]})
    if "ours" in v or "name" in out:
        ours = v.get("ours", False)
        if not isinstance(ours, bool):
            raise CadError(f"{what}ours: true or false")
        out["ours"] = ours
    if v.get("role") not in (None, ""):
        out["role"] = _text(v["role"], f"{what}role", 40)
        if SEP.strip() in out["role"] or tag_of(out["role"]):
            raise CadError(f"{what}role: a plain label")
    if v.get("type") not in (None, ""):
        out["type"] = _service(v["type"], f"{what}type")
    if v.get("confirmed") is not None:
        if not isinstance(v["confirmed"], bool):
            raise CadError(f"{what}confirmed: true or false")
        out["confirmed"] = v["confirmed"]
    page = _text(v.get("page"), f"{what}page", 80, required=False)
    if page and not listed:
        out["page"] = page
    return key, out


def _clean_entity(v: dict, what: str) -> tuple[str, dict]:
    out: dict = {"places": _places(v, what), "service": _service(v.get("service"), f"{what}service")}
    if v.get("ours") is not None:
        if not isinstance(v["ours"], bool):
            raise CadError(f"{what}ours: true or false")
        out["ours"] = v["ours"]
    if v.get("notes") not in (None, ""):
        out["notes"] = _text(v["notes"], f"{what}notes", 300)
    al = v.get("aliases")
    if al not in (None, []):
        if not isinstance(al, list) or len(al) > 20:
            raise CadError(f"{what}aliases: a list of up to 20 names")
        out["aliases"] = list(dict.fromkeys(_text(a, f"{what}aliases", 60) for a in al))
    mi = v.get("merged_into")
    if mi is not None:
        if not isinstance(mi, dict):
            raise CadError(f"{what}merged_into: {{places, service}}")
        into = {"places": _places(mi, f"{what}merged_into."), "service": _service(mi.get("service"), f"{what}merged_into.service")}
        if ekey(into["places"], into["service"]) == ekey(out["places"], out["service"]):
            raise CadError(f"{what}merged_into: another entity")
        out["merged_into"] = into
    return ENTITY + ekey(out["places"], out["service"]), out


# -- the book -----------------------------------------------------------------------------------------------------

@dataclass
class Pair:
    id: str
    a_hz: float
    b_hz: float | None
    places: tuple[str, ...]
    service: str
    role: str
    confirmed: bool
    origin: str                     # list | heard (the list's air rows) | config | named (added here)
    source: str = ""
    fetched: str = ""
    state: str = "list"             # list | edited | deleted | named
    list_name: str = ""             # its name in the tone list (or as #84 named it)
    page: str = ""
    legacy_ours: bool = False       # #84: "ours" on the pair
    parsed: Parsed | None = None
    entity: str = ""                # the entity it is under (merges followed)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class Entity:
    key: str
    places: list[str]
    service: str
    aliases: list[str] = field(default_factory=list)
    notes: str = ""
    ours: bool = False
    ours_locked: bool = False
    edited: bool = False
    merged_from: list[str] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)

    @property
    def name(self) -> str:
        return display(self.places, self.service)


def _tag(p: Pair) -> str:
    return "" if p.confirmed and p.origin != "heard" else " (heard)" if p.confirmed else " (est.)"


class Book:
    """The entities and their pairs: the tone list read as places + service, with the overrides on it."""

    def __init__(self, base: Iterable[ToneCode], ov: dict, locked: Iterable[str] = (), tolerance: float = 0.015,
                 data: Path = DATA):
        self.ov, self.tolerance, self.data = ov, tolerance, data
        self.locked = set(locked)
        self.pairs: list[Pair] = []
        seen = set()
        for c in base:
            k = list_key(c)
            if k in seen:
                continue                # the same code twice (the list's and the config's [[tones]]): one pair
            seen.add(k)
            name = base_name(c.name)
            ps = parse_name(name, data)
            tag = tag_of(c.name)
            p = Pair(k, c.a_hz, c.b_hz, ps.places, ps.service, ps.role, tag != " (est.)",
                     "config" if c.source == "config" else "heard" if tag == " (heard)" else "list",
                     c.source, c.fetched, list_name=name, parsed=ps)
            o = ov.get(k)
            if o is not None:
                if o.get("deleted"):
                    p.state = "deleted"
                else:
                    self._apply(p, o)
                    p.state = "edited"
            self.pairs.append(p)
        for k, o in ov.items():
            if k.startswith(ENTITY) or k in seen or o.get("deleted"):
                continue                # a row of a list that has changed under it: as a pair added here
            p = Pair(k, float(o["a_hz"]), float(o["b_hz"]) if o.get("b_hz") else None, (), "fire", "", True, "named",
                     state="named", page=o.get("page", ""))
            self._apply(p, o)
            self.pairs.append(p)
        self.entity_ov = {k[len(ENTITY):]: o for k, o in ov.items() if k.startswith(ENTITY)}
        self.entities: dict[str, Entity] = {}
        for p in self.pairs:
            p.entity = self.resolve(ekey(p.places, p.service))
            e = self._entity(p.entity, p.places, p.service)
            e.pairs.append(p)
            if p.list_name and p.list_name != e.name and p.list_name not in e.aliases:
                e.aliases.append(p.list_name)
        for k, o in self.entity_ov.items():
            target = self.resolve(k)
            e = self._entity(target, o["places"], o["service"])
            if target != k:
                d = display(o["places"], o["service"])
                e.merged_from.append(d)
                e.aliases += [a for a in [d, *o.get("aliases", [])] if a not in e.aliases]
            else:
                e.edited = True
                e.notes = o.get("notes", "")
                e.aliases += [a for a in o.get("aliases", []) if a not in e.aliases]
        self.entities = {k: e for k, e in self.entities.items() if e.pairs or e.edited}
        self.locked_keys = locked_keys = {self.resolve(ekey(ps.places, ps.service))
                                          for ps in (parse_name(n, data) for n in self.locked)}
        for k, e in self.entities.items():
            o = self.entity_ov.get(k) or {}
            e.ours_locked = k in locked_keys
            legacy = any(p.legacy_ours for p in e.pairs if p.state != "deleted")
            e.ours = e.ours_locked or (o["ours"] if "ours" in o else legacy)
        live = [p for p in self.pairs if p.state != "deleted"]
        for i, p in enumerate(live):
            for q in live[i + 1:]:
                if p.entity != q.entity and _same(tolerance, p.a_hz, p.b_hz, q.a_hz, q.b_hz):
                    for a, b in ((p, q), (q, p)):
                        n = self.entities[b.entity].name
                        if n not in a.conflicts:
                            a.conflicts.append(n)

    def _apply(self, p: Pair, o: dict) -> None:
        if o.get("places"):
            p.places, p.service = tuple(o["places"]), o["service"]
        elif o.get("name"):
            ps = parse_name(o["name"], self.data)
            p.places, p.service = ps.places, ps.service if ps.sure or not o.get("type") else o["type"]
            p.role = ps.role or p.role
            if p.origin == "named":
                p.list_name, p.parsed = o["name"], ps
        p.role = o.get("role", p.role)
        p.confirmed = o.get("confirmed", p.confirmed)
        p.legacy_ours = bool(o.get("ours"))

    def resolve(self, key: str) -> str:
        """An entity key with its merges followed."""
        seen = set()
        while key in self.entity_ov and self.entity_ov[key].get("merged_into") and key not in seen:
            seen.add(key)
            mi = self.entity_ov[key]["merged_into"]
            key = ekey(mi["places"], mi["service"])
        return key

    def _entity(self, key: str, places, service: str) -> Entity:
        e = self.entities.get(key)
        if e is None:
            o = self.entity_ov.get(key)
            if o:
                places, service = o["places"], o["service"]
            else:
                for mo in self.entity_ov.values():          # spelled as the merge into it says
                    mi = mo.get("merged_into")
                    if mi and ekey(mi["places"], mi["service"]) == key:
                        places, service = mi["places"], mi["service"]
            e = self.entities[key] = Entity(key, list(places), service)
        return e

    # -- the plan, ours ------------------------------------------------------------------------------------------

    def codes(self) -> list[ToneCode]:
        """The plan: each pair named "Places · Service[ · Role]" (tagged by confirmed / heard)."""
        out = []
        for p in self.pairs:
            if p.state == "deleted":
                continue
            e = self.entities[p.entity]
            out.append(ToneCode(display(e.places, e.service, p.role) + _tag(p), p.a_hz, p.b_hz,
                                source="named" if p.origin == "named" else p.source, fetched=p.fetched))
        return out

    def ours_names(self) -> set[str]:
        """The names that are ours (IncidentStore, LivePaging): the ours entities' and the config's own."""
        return set(self.locked) | {e.name for e in self.entities.values() if e.ours}

    def monitor_names(self) -> set[str]:
        """Whose pages the CAD queues: PCFD's and Rye Brook's entities (the config's names)."""
        return set(self.locked) | {e.name for e in self.entities.values() if e.ours_locked}

    def find(self, name: str) -> Entity | None:
        """The entity a name (its own, an alias, or a name read as places + service) is."""
        for e in self.entities.values():
            if name == e.name or name in e.aliases:
                return e
        ps = parse_name(name, self.data)
        return self.entities.get(self.resolve(ekey(ps.places, ps.service)))

    def pair(self, pid: str) -> Pair | None:
        return next((p for p in self.pairs if p.id == pid), None)

    # -- what Settings shows -------------------------------------------------------------------------------------

    def table(self, events: list[Heard], meta: dict) -> list[dict]:
        """One row per entity with its pairs: Settings > Radios > Tones. meta: override key -> {operator, at}."""
        out = []
        for e in self.entities.values():
            pairs, epages, elast = [], set(), None
            for p in sorted(e.pairs, key=lambda p: (p.state == "deleted", p.role.casefold(), p.a_hz)):
                pages, last = set(), None
                for h in events:
                    if _same(self.tolerance, h.a_hz, h.b_hz, p.a_hz, p.b_hz):
                        pages.add(h.page)
                        last = h.at if last is None else max(last, h.at)
                if p.state != "deleted":
                    epages |= pages
                    if last is not None:
                        elast = last if elast is None else max(elast, last)
                m = meta.get(p.id) or {}
                orig = p.parsed
                pairs.append({"id": p.id, "a_hz": p.a_hz, "b_hz": p.b_hz, "role": p.role, "confirmed": p.confirmed,
                              "origin": p.origin, "source": p.source, "fetched": p.fetched, "state": p.state,
                              "list": ({"name": p.list_name, "entity": display(orig.places, orig.service, orig.role),
                                        "sure": orig.sure} if orig else None),
                              "page": p.page, "conflicts": p.conflicts, "heard": len(pages), "last": last,
                              "operator": m.get("operator", ""), "at": m.get("at")})
            m = meta.get(ENTITY + e.key) or {}
            out.append({"key": e.key, "name": e.name, "places": e.places, "service": e.service, "aliases": e.aliases,
                        "notes": e.notes, "ours": e.ours, "ours_locked": e.ours_locked, "edited": e.edited,
                        "merged_from": e.merged_from, "pairs": pairs, "count": sum(p["state"] != "deleted" for p in pairs),
                        "conflict": any(p["conflicts"] for p in pairs if p["state"] != "deleted"),
                        "heard": len(epages), "last": elast, "operator": m.get("operator", ""), "at": m.get("at")})
        out.sort(key=lambda r: (r["places"][0].casefold() if r["places"] else "", len(r["places"]),
                                SERVICES.index(r["service"])))
        return out

    def report(self) -> dict:
        """What reading the list as entities did (#122): the entities made of several rows or names, every name read
        with an assumption, and the pairs under two entities."""
        merged = [{"entity": e.name, "names": sorted({p.list_name for p in e.pairs if p.list_name}),
                   "pairs": [pair_key(p.a_hz, p.b_hz) for p in e.pairs]}
                  for e in self.entities.values()
                  if len(e.pairs) > 1 or len({p.list_name for p in e.pairs if p.list_name}) > 1]
        unsure = sorted({p.parsed.why for p in self.pairs if p.parsed and p.parsed.why})
        conflicts = sorted({" / ".join(sorted([self.entities[p.entity].name, *p.conflicts])) + f": {pair_key(p.a_hz, p.b_hz)}"
                            for p in self.pairs if p.conflicts})
        return {"merged": sorted(merged, key=lambda m: m["entity"]), "unsure": unsure, "conflicts": conflicts}


# -- what was heard ---------------------------------------------------------------------------------------------

@dataclass
class Heard:
    """One decoded pair on one page."""
    page: str
    at: float
    channel: str
    a_hz: float
    b_hz: float | None
    voice_at: float | None = None


def heard(comms_rows: Iterable[dict], jsonl: Path | None) -> list[Heard]:
    """Every pair on every page: the communications log's page rows, and unknown_tones.jsonl for pages it no longer
    has (pruned, or from before #64). Newest first."""
    out: list[Heard] = []
    seen: set[str] = set()
    for r in comms_rows:
        try:
            tones = json.loads(r["tones"]) if r.get("tones") else []
        except (TypeError, ValueError):
            continue
        seen.add(str(r["id"]))
        for t in tones:
            try:
                out.append(Heard(str(r["id"]), float(r["start"]), r.get("channel") or "", float(t["a_hz"]),
                                 float(t["b_hz"]) if t.get("b_hz") else None, r.get("voice_at")))
            except (KeyError, TypeError, ValueError):
                continue
    if jsonl is not None:
        try:
            lines = jsonl.read_text().splitlines()
        except OSError:
            lines = []
        for ln in lines:
            try:
                d = json.loads(ln)
                pid = str(d.get("page") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                for a, b in d.get("pairs") or []:
                    out.append(Heard(pid, float(d["at"]), str(d.get("channel") or ""), float(a), float(b) if b else None))
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
    out.sort(key=lambda h: -h.at)
    return out


def _near(tol: float, hz: float | None, want: float | None) -> bool:
    if want is None or hz is None:
        return want is None and hz is None
    return abs(hz - want) <= tol * want


def _same(tol: float, a: float, b: float | None, ca: float, cb: float | None) -> bool:
    return _near(tol, a, ca) and _near(tol, b, cb)


def unnamed(events: list[Heard], plan: TonePlan, has_clip=None) -> list[dict]:
    """The pairs heard that the plan doesn't match, one row per pair (its decodes within the tolerance), newest first:
    {a_hz, b_hz, first, last, count, page (the latest), channel, has_audio}."""
    rows: list[dict] = []
    for h in events:                     # newest first: the first decode of a pair is its latest
        if plan.match_pair(h.a_hz, h.b_hz):
            continue
        row = next((r for r in rows if _same(plan.tolerance, h.a_hz, h.b_hz, r["a_hz"], r["b_hz"])), None)
        if row is None:
            if len(rows) >= MAX_UNNAMED:
                continue
            rows.append({"a_hz": round(h.a_hz, 1), "b_hz": round(h.b_hz, 1) if h.b_hz else None, "first": h.at,
                         "last": h.at, "pages": {h.page}, "page": h.page, "channel": h.channel})
        else:
            row["first"] = min(row["first"], h.at)
            row["pages"].add(h.page)
    for r in rows:
        r["count"] = len(r.pop("pages"))
        r["has_audio"] = bool(has_clip(r["page"])) if has_clip else True
    return rows


# -- places to pick from ------------------------------------------------------------------------------------------

def county_places(data: Path = DATA) -> list[str]:
    """The county's places: the fire departments' (westchester_departments.csv, read as places + service:
    "Westchester County Police" is Westchester County) and the EMS agencies' (westchester_ems.csv)."""
    out: list[str] = []
    for r in _rows(data / "westchester_departments.csv"):
        d = (r.get("department") or "").strip()
        if d:
            out += list(parse_name(re.sub(r"\s*\(.*\)$", "", d), data).places)
    out += [canon_place(p) for r in _rows(data / "westchester_ems.csv") for p in (r.get("places") or "").split("+") if p.strip()]
    return out


def places(book: Book, data: Path = DATA) -> list[str]:
    """One canonical list of places, once each (any case): the book's and the county's."""
    seen: dict[str, str] = {}
    for p in [p for e in book.entities.values() for p in e.places] + county_places(data):
        if p and p.casefold() not in seen:
            seen[p.casefold()] = p
    return sorted(seen.values(), key=str.casefold)


def entity_choices(book: Book) -> list[dict]:
    """The entities to pick from (the dialog, the unnamed list): [{key, name, places, service, aliases, ours}], plus the
    county's agencies not in the book yet (westchester_ems.csv)."""
    out = {e.key: {"key": e.key, "name": e.name, "places": e.places, "service": e.service, "aliases": e.aliases,
                   "ours": e.ours} for e in book.entities.values()}
    for n in sorted(book.locked):                             # PCFD's and Rye Brook's, pairs or not
        ps = parse_name(n, book.data)
        k = book.resolve(ekey(ps.places, ps.service))
        if k not in out:
            out[k] = {"key": k, "name": display(ps.places, ps.service), "places": list(ps.places), "service": ps.service,
                      "aliases": [n], "ours": True}
    for n, (pl, svc, _src, written) in known_names(book.data).items():
        k = book.resolve(ekey(pl, svc))
        if k not in out:
            out[k] = {"key": k, "name": display(pl, svc), "places": list(pl), "service": svc, "aliases": [], "ours": False}
        e = out[k]
        if n != e["name"].casefold() and not any(a.casefold() == n for a in e["aliases"]):
            e["aliases"] = [*e["aliases"], written]
    return sorted(out.values(), key=lambda e: (e["places"][0].casefold(), len(e["places"]), SERVICES.index(e["service"])))
