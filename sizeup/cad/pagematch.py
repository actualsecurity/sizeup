"""A page against the county feed's incident it may be: a scored decision, after the strict rules (sizeup/cad/sources.py
match_all: "match", "street only", "doubled", "misheard") found nothing to join. The feed is the primary, paging the
backup. A false join hides a real call, which is far worse than a duplicate card: a join needs the place itself, and
anything weaker is only a button for a person.

    candidates  every open incident a feed has whose feed event is still open (not closed, not in service), dispatched
                (else first seen) at most WINDOW_S before the page and AFTER_S after it
    strong      the page joins that incident (no card of its own; "Page hh:mm matched by: ..." on its timeline) only
                when ALL of:
                  - unique: it is the only such incident (two open county calls in the window: a person picks)
                  - no page is on it yet
                  - the street exactly the feed's (exact_street: the same name, directions before and after, and the
                    same suffix, said on both); a street the page misheard ("snapped") is weak at most
                  - the feed's location has a house number
                  - the feed is fresh: its last report "ok" and recent (the page's own event may simply be late)
                  - the page within STRONG_S (sources.SAME_CALL_S) of the feed's dispatch
                  - nothing against it (below)
                  - and the PLACE ITSELF: the named place (the feed's "@NAME", the department's common place at the
                    feed's address or its aliases, #575/#584/#612) said on the page, or the house number the same or
                    said twice and run together ("412412" for 412). A name the places list also has at another
                    address (a chain's two stores) never is, and one that is only town names, department or agency
                    words or the street's own words ("PORT CHESTER", "SAMPLE POST" on Sample Post Rd) is no name. A
                    cross street, the family of call or being the only open call are supporting evidence only (shown),
                    never enough
    against     on the street as typed, any house number but the same or doubled (one digit off, "418" or "413" for
                412, is the house next door); on a misheard street another number ("near" stays neutral there, a digit
                in or out is for it); another of the department's places on that street named, or the feed's own name
                when the list has it elsewhere too and the page says no number of ours; cross streets with none in
                common; types of no common family; apparatus on both sides, none in common (one building, two calls);
                another town the page says the call is in or goes to ("in / to <town>": a town named as a landmark,
                "by the Sample border", is no evidence); a mutual-aid page ("mutual aid", "to the City of ...")
                against an event that is not mutual aid
    weak        the street the same (or snapped), not strong, at most one thing against it and never another house
                number, another town or mutual aid: the page makes its own card exactly as before (its notes, the
                dispatch mode's handling unchanged), and the card shows "Merge into #N" (a person decides,
                Sources.merge_page; unmerge_page undoes it). Kept in page_merges only: never a "possibly the same" note
    none        anything else

Only ever a page's location (speech-to-text) against a feed's typed one. Pure: Sources gathers the incidents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WINDOW_S = 10 * 60.0          # the feed's dispatch at most this long before the page (a merge offered) ...
AFTER_S = 60.0                # ... or this long after it (the feed's event first seen after the page: its dispatch time)
STRONG_S = 5 * 60.0           # joined only this close (sources.SAME_CALL_S: "the 5-min rule alone decides")

# The families of call a type and subtype are (data/cad_lists.json): two types are compatible when they share one. An
# alarm, a hazard or an investigation with no subtype may be several. A car fire is never a building's alarm.
FAMILY_SUB = {
    ("HAZARD", "GAS"): {"gas"}, ("HAZARD", "GAS-LEAK"): {"gas"}, ("HAZARD", "GAS-LINE"): {"gas"},
    ("INVEST", "GAS-ODOR"): {"gas"}, ("INVEST", "GAS-OUT"): {"gas"}, ("ALARM", "GAS"): {"gas"},
    ("ALARM", "CO-COMM"): {"co"}, ("ALARM", "CO-RESD"): {"co"}, ("INVEST", "CO-NO"): {"co"}, ("INVEST", "CO-YES"): {"co"},
    ("ALARM", "COMM"): {"building"}, ("ALARM", "RESD"): {"building"}, ("ALARM", "TROUB"): {"building"},
    ("ALARM", "MED"): {"ems"}, ("INVEST", "INSIDE"): {"building", "gas", "co"}, ("INVEST", "OUTSIDE"): {"outside", "gas"},
    ("INVEST", "ELECT"): {"electric", "building"}, ("HAZARD", "WIRES"): {"electric"}, ("HAZARD", "TRANS"): {"electric"},
    ("HAZARD", "TREE"): {"tree"}, ("HAZARD", "TREERD"): {"tree"}, ("HAZARD", "HAZMAT"): {"hazmat"},
    ("HAZARD", "FLUID"): {"hazmat"}, ("HAZARD", "BOMB"): {"police"}, ("HAZARD", "AIRCRAFT"): {"airport"},
    ("SERVICE", "SPRINK"): {"water", "building"}, ("SERVICE", "WEATHER"): {"water", "tree"},
    ("MA", "EMS"): {"ems"}, ("MA", "EMS-A"): {"ems"}, ("MA", "FIRE"): {"building"},
}
FAMILY_TYPE = {
    "STRU": {"building"}, "BRUSH": {"outside"}, "MVF": {"vehicle"}, "MVA": {"vehicle"}, "ALS": {"ems"}, "BLS": {"ems"},
    "RESCUE": {"rescue"}, "AIRPORT": {"airport"}, "SERVICE": {"service", "water"},
    "ALARM": {"building", "co", "gas"}, "INVEST": {"building", "outside", "gas", "co", "electric"},
    "HAZARD": {"gas", "electric", "hazmat", "tree"},
}
NAME_NOISE = {"the", "of", "and", "at", "inc", "llc", "corp", "co"}


def families(typ: str, sub: str = "") -> frozenset:
    """The families a type is ("HAZARD", "GAS" -> {"gas"}); empty: no type, or one that says nothing (DRILL, NOTIF)."""
    typ, sub = str(typ or "").upper(), str(sub or "").upper()
    got = FAMILY_SUB.get((typ, sub))
    if got is None:
        got = FAMILY_TYPE.get(typ, set())
    return frozenset(got)


def number_fit(heard: str, typed: str, halved: str = "") -> str:
    """A page's house number (heard) against a feed's (typed): "exact"; "doubled" (said twice, run together: halved is
    the page's number halved, sources.halved); "misread" (a digit in or out, fuzzy.misread: only a misheard street's);
    "near" (the same length, one digit another: neither for nor against); "other" (another number); "" when either
    side has none."""
    from sizeup.cad.fuzzy import misread
    a, b = str(heard or "").lower(), str(typed or "").lower()
    if not a or not b:
        return ""
    if a == b:
        return "exact"
    if halved and halved.lower() == b:
        return "doubled"
    if misread(a, b):
        return "misread"
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    if da and len(da) == len(db) and sum(x != y for x, y in zip(da, db)) == 1:
        return "near"
    return "other"


def name_words(name: str) -> list[str]:
    """A place's name (or a page's text) as compared: the place search's words (commonplaces.name_key), no filler."""
    from sizeup.cad.commonplaces import name_key
    return [w for w in name_key(name).split() if w not in NAME_NOISE]


def name_key_of(name: str) -> str:
    return " ".join(name_words(name))


def says_name(text_words: list[str], name: str) -> bool:
    """The page's words (name_words of its text) say this place's name: its words, in order, side by side. A name of
    one short word ("Ace") is too loose: never."""
    w = name_words(name)
    if not w or (len(w) == 1 and len(w[0]) < 5):
        return False
    n = len(w)
    return any(text_words[i:i + n] == w for i in range(len(text_words) - n + 1))


@dataclass
class Evidence:
    """One feed incident against the page: for (what joins it), against (what says another call), and the verdict."""
    inc: dict
    street: str = ""                                  # "" | "same" (exact_street) | "snapped" (misheard: the map's)
    number: str = ""                                  # number_fit's
    feed_numbered: bool = False                       # the feed's location has a house number
    place: str = ""                                   # the named place said on the page (for)
    other_place: str = ""                             # another place on that street said on the page (against)
    elsewhere: str = ""                               # the feed's name said, no number of ours, the list has it at
                                                      # another address too (against: never the place itself)
    elsewhere_near: bool = False                      # ... on that street, or in a town the page names
    cross: str = ""                                   # "" | "match" | "differ"
    family: str = ""                                  # "" | "same" | "differ"
    units: str = ""                                   # "" | "common" (one not ours) | "own" (only ours) | "differ"
    town: str = ""                                    # "" | "differ"
    town_ours: bool = False                           # ... both the page's and the event's among our tones' towns
    mutual: str = ""                                  # "" | "differ" (a mutual-aid page, an event that isn't)
    fresh: bool = False                               # the feed's last report ok and recent
    taken: bool = False                               # a page is on it already
    lag_s: float = 0.0                                # the page's time - the feed's dispatch
    decision: str = "none"
    why: list[str] = field(default_factory=list)      # "matched by" / "not joined: ..." as the timeline says it

    def _number_for(self) -> bool:
        return self.number in ("exact", "doubled") or (self.number == "misread" and self.street == "snapped")

    def _number_against(self) -> bool:
        if self.street == "same":
            return self.number in ("misread", "near", "other")
        return self.number == "other"

    def _the_place(self) -> bool:
        """The place itself: its name said, or its house number (the same, or said twice)."""
        return bool(self.place) or self.number in ("exact", "doubled")

    def pros(self) -> list[str]:
        out = []
        if self.place:
            out.append(f"place {self.place}")
        if self._number_for():
            out.append({"exact": "house number", "doubled": "house number said twice",
                        "misread": "house number (a digit misheard)"}[self.number])
        if self.cross == "match":
            out.append("cross street")
        if self.family == "same":
            out.append("type family")
        return out

    def kinds(self) -> list[str]:
        """The evidence's kinds, for the log (never a location or a name)."""
        return ["street", *(k for k, on in (("place", self.place), ("number", self._number_for()),
                                             ("cross", self.cross == "match"), ("type", self.family == "same")) if on)]

    def cons(self) -> list[str]:
        out = []
        if self._number_against():
            out.append("another house number")
        if self.other_place:
            out.append(f"names {self.other_place}")
        if self.elsewhere:
            out.append(f"names {self.elsewhere} (also elsewhere)")
        if self.cross == "differ":
            out.append("other cross streets")
        if self.family == "differ":
            out.append("another kind of call")
        if self.units == "differ":
            out.append("other units")
        if self.town == "differ":
            out.append("another town")
        if self.mutual == "differ":
            out.append("mutual aid")
        return out


def decide(evs: list[Evidence]) -> Evidence | None:
    """The verdict on each candidate (decision, why) and the one to act on: the single strong one, else the nearest
    weak one, else None."""
    unique = len(evs) == 1
    for e in evs:
        pros, cons = e.pros(), e.cons()
        timely = -STRONG_S <= e.lag_s <= STRONG_S
        if (not e.street or e.town == "differ" or e.mutual == "differ" or e._number_against()
                or len(cons) > 1):
            e.decision = "none"
        elif (unique and not e.taken and e.street == "same" and e.feed_numbered and e.fresh and timely and not cons
              and e._the_place()):
            e.decision = "strong"
        else:
            e.decision = "weak"
        street = "street" + (" (as the map spells it)" if e.street == "snapped" else "")
        if e.decision == "strong":
            e.why = [street, "the only open county call", *pros]
        elif e.decision == "weak":
            e.why = [street, *pros] + [f"not joined: {x}" for x in (
                *([] if unique else ["another open county call"]), *(["a page is on it"] if e.taken else []),
                *([] if e.street == "same" else ["the street misheard"]),
                *([] if e.feed_numbered else ["no house number from the county"]),
                *([] if e.fresh else ["the county feed not fresh"]),
                *([] if timely else [f"{round(abs(e.lag_s) / 60)} min from the county's dispatch"]),
                *cons, *([] if e._the_place() else ["neither the place's name nor its number"]))]
    strong = [e for e in evs if e.decision == "strong"]
    if strong:
        return strong[0]
    weak = sorted((e for e in evs if e.decision == "weak"), key=lambda e: abs(e.lag_s))
    return weak[0] if weak else None


def exact_street(a: tuple, b: tuple) -> bool:
    """(pre, suffix, name, post) twice, strictly: the same name, the same directions before and after, and the same
    suffix said on both ("Sample Rd" is never "Sample", nor "N Sample Rd")."""
    return a[2] == b[2] and a[0] == b[0] and a[3] == b[3] and bool(a[1]) and a[1] == b[1]


MUTUAL = re.compile(r"\bmutual\s*aid\b", re.I)


def mutual_aid(text: str, typ: str = "") -> bool:
    """A mutual-aid call: its type MA, or the words "mutual aid"."""
    return str(typ or "").upper() == "MA" or bool(MUTUAL.search(text or ""))
