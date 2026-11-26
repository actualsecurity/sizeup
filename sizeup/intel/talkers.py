"""Who keyed up: a row's talker radio ID through Settings > Radios > Radio IDs (#844, sizeup/cad/radios.py).

    Talkers(radios=None)       radios: the CAD's registry (Registry: name(system, radio_id), rev); None: no radio is
                               named (replay, tests, a station), so every row reads exactly as before
    talkers.of(row) -> Talker | None
                               the first of the row's radio IDs ("a,b": one per key-up) the registry names, on another
                               P25 system only (OTHER_P25: an agency's radios, labelled by agency and role; the county's
                               and the fireground's IDs are our rigs, never a talker label here)
    town_of(talker, towns, agency_towns) -> str
                               the talker's department's town among the source's towns, or ""
    town_why(...) -> (town, how)   ... and how it was found: "settings" | "name" | "initials" | "" (Settings shows it)

Talker(label, agency, role, console): the label as the department named it (its own entry's, else its range's: the
admin's words, at runtime, never in this repository), split into the agency ("<Town> PD") and the role words after it
("officer", "dispatch"); console: a dispatch console, so its transmissions are the dispatcher's (never a tie by talker
alone; the dispatch cue behind "console_cue": engine.py). A console only when the role starts with a console word
("dispatch", "dispatcher", "console", "control", "ctrl"): never after another word's "control" ("animal control",
"traffic control"), never a label with a portable, mobile, car or unit in it, and "HQ" or "desk" alone never are one
("<Agency> HQ portable 2", "desk sgt").

Cached: a radio is asked about once per registry revision (Registry.rev: bumped by every edit), never per row; the
registry itself is in memory (no DB query either way).

The town (the police pack's town prior, engine.py _town_prior): the pack's "agency_towns" ({agency: town}: the file's,
with Settings' intel_packs.<pack>.agency_towns over it) first; else an agency that is one of the source's towns, by
name or by its initials, with a department word or none after it ("<Town> PD", "<initials>PD"). Two towns with the
same initials: neither is guessed.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

CACHE_MAX = 4096
CONSOLE_WORDS = frozenset({"dispatch", "dispatcher", "disp", "console", "control", "ctrl"})
NOT_CONSOLE_BEFORE = frozenset({"animal", "traffic", "parking", "pest", "flood", "crowd"})   # "animal control"
NOT_CONSOLE_IN = frozenset({"portable", "mobile", "car", "unit", "cars", "units"})          # a radio in the field
ROLE_WORDS = CONSOLE_WORDS | {"hq", "desk",
                              "officer", "officers", "sergeant", "sgt", "lieutenant", "lt", "captain", "capt", "chief",
                              "detective", "det", "supervisor", "patrol", "car", "unit", "portable", "mobile", "radio",
                              "op", "position", "pos"}
DEPT_SUFFIXES = ("", "pd", "police", "policedepartment", "policedept", "fd", "fire", "firedepartment", "ems")
TOKEN = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class Talker:
    label: str
    agency: str = ""
    role: str = ""
    console: bool = False


def parse(label: str) -> Talker:
    """A label as agency and role: "<Agency> PD dispatch" -> ("<Agency> PD", "dispatch", console)."""
    lab = " ".join(str(label or "").split())
    toks = lab.split(" ")
    low = [t.lower().strip(".,:;#") for t in toks]
    i = next((k for k, t in enumerate(low) if t in ROLE_WORDS), len(toks))
    console = (i < len(low) and low[i] in CONSOLE_WORDS and not (i and low[i - 1] in NOT_CONSOLE_BEFORE)
               and not ({w.lower() for w in TOKEN.findall(lab)} & NOT_CONSOLE_IN))
    return Talker(lab, " ".join(toks[:i]), " ".join(toks[i:]), console)


def _norm(s: str) -> str:
    return "".join(TOKEN.findall(str(s or "").lower()))


def town_of(t: Talker | None, towns, agency_towns=None) -> str:
    """The talker's department's town, one of `towns` (the source's); "" when not known."""
    return town_why(t, towns, agency_towns)[0]


def town_why(t: Talker | None, towns, agency_towns=None) -> tuple[str, str]:
    """(town, how): how is "settings" (agency_towns), "name" or "initials" (guessed from the source's towns), or ""."""
    if t is None:
        return "", ""
    towns = [str(x) for x in towns or () if x]
    agency, label = _norm(t.agency), _norm(t.label)
    for k, town in (agency_towns or {}).items():
        if _norm(k) and _norm(k) in (agency, label) and (not towns or town in towns):
            return town, "settings"
    if not agency:
        return "", ""
    hits = [x for x in towns if any(agency == _norm(x) + s for s in DEPT_SUFFIXES)]
    if len(hits) == 1:
        return hits[0], "name"
    initials: dict[str, list[str]] = {}
    for x in towns:
        ws = TOKEN.findall(x.lower())
        if len(ws) > 1:
            initials.setdefault("".join(w[0] for w in ws), []).append(x)
    hits = [x for ini, xs in initials.items() if len(xs) == 1 and any(agency == ini + s for s in DEPT_SUFFIXES)
            for x in xs]
    return (hits[0], "initials") if len(hits) == 1 else ("", "")


class Talkers:
    def __init__(self, radios=None):
        self.radios = radios
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], Talker | None] = {}
        self._rev = None
        self.asked = 0                         # registry lookups made (the cache's misses)

    def of(self, row: dict) -> Talker | None:
        reg = self.radios
        system = str(row.get("system") or "")
        if reg is None or not system:
            return None
        from sizeup.cad.radios import OTHER_P25
        if system not in OTHER_P25:
            return None
        for rid in [x.strip() for x in str(row.get("radio_id") or "").split(",") if x.strip()]:
            t = self._one(reg, system, rid)
            if t is not None:
                return t
        return None

    def _one(self, reg, system: str, rid: str) -> Talker | None:
        rev = getattr(reg, "rev", None)
        k = (system, rid)
        with self._lock:
            if rev != self._rev or len(self._cache) > CACHE_MAX:
                self._cache.clear()
                self._rev = rev
            if k in self._cache:
                return self._cache[k]
        try:
            label = str(reg.name(system, rid) or "").strip()
        except Exception:  # noqa: BLE001 - no label is a row as before
            label = ""
        t = parse(label) if label else None
        with self._lock:
            self.asked += 1
            if self._rev == rev:               # an edit landed meanwhile: never cache an answer from before it
                self._cache[k] = t
        return t

    def labels(self) -> list[str]:
        """Every label the registry gives another system's radios (its entries and ranges): Settings' town guesses."""
        reg = self.radios
        if reg is None:
            return []
        from sizeup.cad.radios import OTHER_P25
        out: list[str] = []
        try:
            rows = list(reg.rows()) + list(reg.ranges())
        except Exception:  # noqa: BLE001 - no list, no guesses shown
            return []
        for r in rows:
            lab = " ".join(str(r.get("label") or "").split())
            if r.get("system") in OTHER_P25 and lab and lab not in out:
                out.append(lab)
        return out
