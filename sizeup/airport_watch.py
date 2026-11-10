"""Airport alerts that build up over several transmissions (GitHub #369): an abnormal go-around heard on HPN ATC, the
aircraft followed through its frequency changes, and "alert level N" declared on 60 Control's fire radio.

The single-transmission rules (sizeup/atc.py, data/atc/rules.json) miss an emergency that is never called one: a go-around,
then "we have a flap fail indication", "we have to run a checklist", "we may go to a different airport", minutes apart and
mostly without a callsign. This watch keeps a short memory per aircraft instead.

    the aircraft    the talker of each airband transmission: the ATC talker record when the app has one
                    (sizeup/atc_talkers.py: the callsign heard, matched to ADS-B, or its exchange's subject), else the
                    callsign heard (sizeup/atc.py attribute), else the aircraft heard on the channel within EXCHANGE_S
                    (a pilot's "we have to run a checklist" right after the controller called them)
    go-around       "go around", "going around", "missed approach", "going missed" (GO_AROUND); or the aircraft's own
                    track: arriving in the last ADSB_ARRIVING_S, now near / departing / low and not descending (the
                    Aircraft layer's PhaseMemory, #279: a climb after an approach)
    abnormal        ABNORMAL: flaps, gear, hydraulics, engine, smoke / fire, a checklist being run, divert / "a different
                    airport", unable to land / extend, declaring / emergency, souls on board, fuel; a "fail indication",
                    troubleshooting, "some airspace". What a normal flight says never counts: "flaps 20", "gear down",
                    "checklist complete", "before landing checklist" (NOT). Speech-to-text mis-hearings are fixed first
                    (fuzzy(): "flat fail" -> flap fail, "flats" -> flaps, "go round" -> go around)
    the pre-alert   source "atc", kind "abnormal", subject acft:<callsign> (the same one sizeup/atc.py's rules make for the
                    aircraft, so the two never show twice):
                      a go-around (words or track) and an abnormal phrase from the same aircraft within WINDOW_S
                      two transmissions or more with abnormal phrases (one of them STRONG) within WINDOW_S
                    confidence: conf(); escalated (added to it, higher) on each further abnormal transmission and when
                    the track confirms the go-around. Every later transmission of the aircraft, on any of our airband
                    channels, is added to it while it is open
    hand-offs       "contact New York approach 120.8" or the pilot's readback ("20.8, <callsign>": the leading 1 dropped)
                    for the aircraft of an open pre-alert: its "Where" line says where it went ("NY App 120.8"), mapped to
                    our airband channels (#260: the channel whose frequency or schedule has it; received now when the
                    schedule has it on then, or an "also" frequency, #275); not ours: "off our channels · tracking by
                    ADS-B". Heard again on the tower ("on the visual 34", "tower, <callsign>"): back on HPN Tower
    kept open       while the aircraft is on ADS-B (tick(), every picture of the Aircraft layer): the pre-alert's details
                    are refreshed, so it doesn't expire while the aircraft is away
    closed          ADS-B: on the ground at HPN (landed); gone from ADS-B LOST_S having been LEAVE_NEAR_NM out or
                    more, or past LEAVE_NM (left the area: a divert); 60 Control: the alert canceled, "landed without
                    incident", "down safely"

60 Control (heard_radio(): every transcript on our fire channels and talkgroups, Cad._on_text, #297's hook):

    declared        "alert level 1/2/3" ("alert 2", "alert level two", "alert II") with the airport in it: HPN, the
                    airport, a runway, a final, an aircraft or its type, "declaring". The aircraft type, the problem,
                    the runway and the final distance as heard: "HPN Alert 1 · Citation · flaps · 5 mi final RWY 16"
    attached        to the open pre-alert of the tower for the same aircraft (the only one open with an aircraft, or the
                    only one whose type fits), its title the alert's from then on, and pushed again; else its own
                    (source "radio"). A readback or a repeat is added to the same; a higher level escalates it
    canceled        "alert (level 1) canceled", "landed without incident", "down safely", "on the ground safely": ended
    per level       the department: Alert 1 PCFD doesn't respond to but stands by for: a STANDBY pre-alert, never an
                    incident ("HPN Alert 1 · standby · ..."). Alert 2 and 3 PCFD responds to: the pre-alert and a QUEUED
                    Airport incident at the airport (never dispatched), which the county's event or the 60 Control page
                    attaches to when it comes (sizeup/cad/sources.py alert_incident; in 60 Control mode the county's
                    event makes it tracked). [atc] alert_level_incident = [2, 3] (the default) says which levels make one.
                    An upgrade (1 -> 2) escalates the same pre-alert into the incident; a cancel ends a pre-alert and is a
                    line on an incident's timeline (never closes it)
    first           both kinds poll the county's feed at once (Sources.poke: the station's sources_poll_now), so its
                    event comes as early as it can; ATC's is a "possible incoming" (the earliest warning, never an
                    incident), 60 Control's the highest (pushed at the top priority, listed first) in either dispatch mode

Never anything outside the CAD of record: the app feeds it there only (App._atc, Cad._on_text, the Aircraft layer's
on_view)."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Sequence

from sizeup import atc, atc_scope
from sizeup.cad import airport
from sizeup.cad.airport_alerts import AirportAlert
from sizeup.tz import TZ

WINDOW_S = 10 * 60.0            # a go-around and an abnormal phrase this close together: one story
EXCHANGE_S = 45.0               # a transmission with no callsign this soon after one with: the same aircraft's
KEEP_S = 30 * 60.0              # an aircraft with no pre-alert forgotten after this long unheard
ADSB_ARRIVING_S = 5 * 60.0      # arriving this recently, now climbing away near HPN: a go-around on the track
ADSB_GA_NM = 10.0
LOST_S = 10 * 60.0              # gone from ADS-B this long ...
LEAVE_NEAR_NM = 10.0            # ... having been this far out: it left the area
LEAVE_NM = 40.0                 # this far out: it left the area (NY Approach's own reach, sizeup/atc_scope.py)
LANDED_NM = 2.0                 # on the ground this close to HPN: landed
LEVEL_KEEP_S = 2 * 3600.0       # an alert level's pre-alert: a readback or a cancel this long after still finds it
INCIDENT_LEVELS = (2, 3)        # the alert levels PCFD responds to (the department): an incident; Alert 1: standby
ATTACH_S = 60 * 60.0            # a tower pre-alert updated this recently can take the alert level
MHZ_TOL = 0.006
RETURN_S = 60.0                 # heard on the tower this long after a hand-off: back (sooner, only with its words)

_FIX = [   # speech-to-text mis-hearings, on atc.normalize() text
    (re.compile(r"\bflat(?=\s+(?:fail|failure|failed|fault|indication|problem|problems|issue|issues|stuck|asymmetry|"
                r"malfunction|warning|light|lever|setting|system|jam|jammed)\b)"), "flap"),
    (re.compile(r"\bflats(?=\s+(?:fail|failure|failed|fault|indication|problem|problems|issue|issues|stuck|are|aren t|didn t|"
                r"won t|wont|will not|not|indication|system)\b)"), "flaps"),
    (re.compile(r"\bflabs?\b"), lambda m: "flaps" if m.group(0).endswith("s") else "flap"),
    (re.compile(r"\bgo(?:ing)? round\b"), lambda m: m.group(0).replace(" round", " around")),
    (re.compile(r"\bgoaround\b"), "go around"),
]

GO_AROUND = re.compile(r"\b(?:go(?:ing)? around|went around|missed approach|going missed|execut\w* (?:the )?missed|"
                       r"balked landing)\b")

# (kind, label, strong, pattern, not): an abnormal phrase. "strong" ones may stand on repeats alone.
_ABN = [
    ("flaps", "flaps", True,
     r"flaps? (?:\w+ )?(?:fail|failure|failed|fault|indication|problem|problems|issue|issues|stuck|asymmetry|asymmetric|"
     r"malfunction|warning|jam|jammed|split)|(?:no|zero|partial|without) flaps|flapless|"
     r"flaps? (?:won t|wont|will not|didn t|did not|aren t|are not|not) (?:\w+ )?(?:extend\w*|retract\w*|com\w+|deploy\w*|"
     r"work\w*|mov\w+)", ""),
    ("gear", "gear", True,
     r"(?:landing |nose |main )?gear (?:\w+ )?(?:fail|failure|failed|problem|problems|issue|issues|indication|unsafe|"
     r"malfunction|warning|stuck|disagree)|unsafe gear|gear (?:won t|wont|will not|didn t|did not|not) (?:\w+ )?"
     r"(?:down|up|extend\w*|retract\w*|lock\w*|com\w+)|no (?:gear|nose gear|main gear) (?:indication|light)", ""),
    ("hydraulics", "hydraulics", True, r"hydraulics?", ""),
    ("engine", "engine", True,
     r"engine (?:\w+ )?(?:fail|failure|failed|out|problem|problems|issue|issues|fire|shut ?down|shutdown|vibration|surge|"
     r"surging|rough|loss|quit|chip)|lost (?:an |the |one |our |number (?:one|two|1|2) )?engine|single engine (?:approach|"
     r"landing)|flame ?out|loss of (?:power|thrust)", ""),
    ("smoke", "smoke", True, r"smoke|smoking|fumes|burning smell|smell (?:of )?(?:smoke|burning)|fire",
     r"no smoking|non ?smoking|fire (?:it |them |her )?up|fired up|fire away|fire (?:drill|trucks?|department|rescue|crew)|"
     r"crash fire"),
    ("checklist", "checklist", False,
     r"(?:run|running|ran|work|working|go through|going through|do|doing|need to|have to|got to|gotta) (?:\w+ ){0,3}"
     r"(?:checklists?|qrh)|qrh|(?:emergency|abnormal|non ?normal) checklists?",
     r"checklists? (?:is |are |s )?(?:complete|completed|done|finished)|(?:before|after) (?:takeoff|take off|landing|start|"
     r"starting|taxi) checklists?|(?:descent|approach|climb|cruise|shutdown|parking) checklists?"),
    ("divert", "divert", True,
     r"divert\w*|diversion|(?:a |to |the )?different airport|another airport|alternate airport|"
     r"go(?:ing)? to (?:our |the )?alternate|land (?:somewhere )?else", ""),
    ("unable", "unable", False,
     r"unable (?:to )?(?:land|extend|retract|lower|raise|get (?:the )?(?:gear|flaps?)|maintain|hold altitude|control)", ""),
    ("emergency", "emergency", True,
     r"declar\w* (?:an |a )?(?:emergency|mayday)|emergency|may ?day|pan pan",
     r"emergency (?:locator|frequency|beacon)|no emergency|not (?:an |declaring )?(?:an )?emergency"),
    ("souls", "souls on board", True, r"souls|soles on board|(?:people|persons) on board", ""),
    ("fuel", "fuel", True,
     r"fuel (?:remaining|on board|emergency|leak|leaking|low|critical|imbalance|starvation)|minimum fuel|"
     r"low (?:on )?fuel|(?:hours?|minutes?) of fuel", ""),
    ("problem", "a problem", False,
     r"(?:fail|failure) indication|(?:we ve|we have|we got|we ve got|got) (?:a |an |some )?(?:problem|issue|indication|"
     r"malfunction|warning)|troubleshoot\w*|(?:some|block of|a block of) airspace|work(?:ing)? (?:the |a |this |our )?"
     r"(?:problem|issue)", ""),
]


@dataclass(frozen=True)
class Abn:
    kind: str
    label: str
    strong: bool
    rx: re.Pattern
    never: re.Pattern | None


ABNORMAL = [Abn(k, lab, st, re.compile(rf"(?<![a-z0-9])(?:{p})(?![a-z0-9])"),
                re.compile(rf"(?<![a-z0-9])(?:{n})(?![a-z0-9])") if n else None) for k, lab, st, p, n in _ABN]


def fuzzy(norm: str) -> str:
    """Mis-hearings fixed: "flat fail indication" -> "flap fail indication", "go round" -> "go around"."""
    for rx, rep in _FIX:
        norm = rx.sub(rep, norm)
    return norm


def abnormal(norm: str) -> list[tuple[Abn, str]]:
    """The abnormal phrases in a (normalized, fuzzy()-fixed) transmission: (rule, the words)."""
    out = []
    for a in ABNORMAL:
        if a.never is not None and a.never.search(norm):
            # the words that mean something normal: the rest of the transmission may still count
            rest = a.never.sub(" ", norm)
            m = a.rx.search(rest)
        else:
            m = a.rx.search(norm)
        if m:
            out.append((a, m.group(0)))
    return out


def go_around(norm: str) -> str:
    m = GO_AROUND.search(norm)
    return m.group(0) if m else ""


# -- frequencies ------------------------------------------------------------------------------------------------------

_NUMW = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "tree": 3, "four": 4, "fower": 4, "five": 5, "fife": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "niner": 9}
_TENSW = {"eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30}
_D = r"(?:\d|zero|oh|one|two|three|tree|four|fower|five|fife|six|seven|eight|nine|niner)"
_WHOLE = (rf"(?P<w>1[123]\d|1[123] \d|one (?:one|two|three) {_D}|(?:twenty|thirty)(?: {_D})?|1[89]|2\d|3[0-6])")
_FRAC = rf"(?P<f>\d{{1,3}}|{_D}(?: {_D}){{0,2}})"
FREQ = re.compile(rf"(?<![a-z0-9])(?:{_WHOLE}) (?:point |decimal |dot )?{_FRAC}(?![a-z0-9])")
_HANDOFF = re.compile(r"\b(?:contact|monitor|switch(?:ing)?(?: to)?|over to|going to|go to|change to|frequency change)\b")
_FACILITY = re.compile(r"\b(?P<f>approach|departure|center|centre|ground|tower|clearance|delivery|app|dep)\b")
_RETURN = re.compile(r"\b(?:on the (?:visual|ils|rnav|localizer|gps|approach)|tower\b|with you|westchester tower)")


def _num(tokens: str) -> int | None:
    parts = tokens.split()
    if all(p.isdigit() for p in parts):
        return int("".join(parts))
    if parts and parts[0] in _TENSW:
        v = _TENSW[parts[0]]
        if len(parts) == 2 and parts[1] in _NUMW:
            v += _NUMW[parts[1]]
        return v
    digits = [str(_NUMW[p]) if p in _NUMW else p for p in parts]
    return int("".join(digits)) if all(d.isdigit() for d in digits) else None


def frequency(norm: str) -> tuple[float, int] | None:
    """(MHz, where it was said) in a transmission: "120 point eight", "126 4", "twenty point eight" (a readback's
    dropped leading 1), "124 point 65". Airband voice only (118 to 137)."""
    for m in FREQ.finditer(norm):
        w = _num(m.group("w"))
        f = m.group("f")
        fr = "".join(str(_NUMW[p]) if p in _NUMW else p for p in f.split())
        if w is None or not fr.isdigit():
            continue
        if w < 100:
            w += 100
        mhz = float(f"{w}.{fr}")
        if 118.0 <= mhz <= 137.0:
            return round(mhz, 3), m.start()
    return None


@dataclass
class Where:
    label: str               # "NY App 120.8", "HPN Ground", "HPN Tower"
    channel: str = ""        # our airband channel, "" when none has it
    mhz: float | None = None
    receiving: bool = False
    at: float = 0.0

    def text(self) -> str:
        if self.channel and self.receiving:
            return self.label
        if self.channel:
            return f"{self.label} · not on now · tracking by ADS-B"
        return f"{self.label} · off our channels · tracking by ADS-B"


def _minute(at: float) -> int:
    d = datetime.fromtimestamp(at, TZ)
    return d.hour * 60 + d.minute


def _sched_mhz(c, at: float) -> float:
    if not c.schedule:
        return float(c.mhz)
    m = _minute(at)
    cur = c.schedule[-1][1]
    for t, f in sorted(c.schedule):
        if m >= t:
            cur = f
    return float(cur)


def _base(label: str) -> str:
    """'NY App 126.4/120.8' -> 'NY App'; 'HPN Ground' -> 'HPN Ground'."""
    return " ".join(w for w in re.split(r"\s+", re.sub(r"[\d./]+", " ", label)) if w)


class Channels:
    """Our airband channels (the config's ChannelCfg: label, mhz, schedule, also) as frequencies."""

    def __init__(self, channels: Iterable = ()):
        self.air = [c for c in channels if getattr(c, "kind", "airband") == "airband"]

    def owner(self, mhz: float):
        for c in self.air:
            if abs(c.mhz - mhz) < MHZ_TOL:
                return c
        for c in self.air:
            if any(abs(f - mhz) < MHZ_TOL for _t, f in c.schedule):
                return c
        return None

    def receiving(self, c, mhz: float, at: float) -> bool:
        if abs(_sched_mhz(c, at) - mhz) < MHZ_TOL:
            return True
        m = _minute(at)
        for other in self.air:
            for f, a, b, *_ in other.also:
                if abs(f - mhz) < MHZ_TOL and (a is None or b is None or a == b or (a <= m < b if a < b else (m >= a or m < b))):
                    return True
        return False

    def where(self, mhz: float | None, facility: str, at: float) -> Where:
        c = self.owner(mhz) if mhz is not None else None
        if c is None and mhz is None and facility:
            fac = {"app": "approach", "dep": "departure"}.get(facility, facility)
            cs = [x for x in self.air if atc_scope.facility(x.label) == ("approach" if fac in ("approach", "departure")
                                                                          else "ground" if fac in ("ground", "clearance", "delivery")
                                                                          else "tower")]
            if fac not in ("center", "centre") and len(cs) == 1:
                c = cs[0]
                mhz = _sched_mhz(c, at)
        if c is not None:
            base = _base(c.label)
            label = f"{base} {mhz:g}" if mhz is not None and base != c.label else c.label
            return Where(label, c.label, mhz, self.receiving(c, mhz, at) if mhz is not None else True, at)
        fac = {"app": "approach", "dep": "departure", "centre": "center"}.get(facility, facility)
        name = {"approach": "NY App", "departure": "NY Dep", "center": "NY Center", "ground": "Ground",
                "tower": "Tower"}.get(fac, "")
        label = " ".join(x for x in (name, f"{mhz:g}" if mhz is not None else "") if x) or "another frequency"
        return Where(label, "", mhz, False, at)


# -- aircraft types (60 Control says a type, ADS-B has an ICAO type code) -------------------------------------------------

TYPE_CODES = {"phenom": ("E50P", "E55P"), "citation": ("C5", "C6", "C7", "C25"), "cessna": ("C1", "C2", "C3", "C4", "C5", "C6", "C7"),
              "challenger": ("CL3", "CL6"), "gulfstream": ("GLF", "G2", "G15", "G28"), "global": ("GLEX", "GL5", "GL7"),
              "learjet": ("LJ",), "lear": ("LJ",), "falcon": ("FA", "F2T", "F900"), "king air": ("BE9", "BE2", "B35"),
              "pilatus": ("PC12", "PC24"), "embraer": ("E1", "E2", "E3", "E5", "E6"), "praetor": ("E545", "E550"),
              "legacy": ("E35L", "E545", "E550"), "hawker": ("H25", "HA4"), "boeing": ("B7",), "airbus": ("A3", "A2"),
              "piper": ("P2", "P3", "P4", "PA"), "cirrus": ("SR2", "SF5"), "beech": ("BE",), "beechcraft": ("BE",),
              "bonanza": ("BE3",), "baron": ("BE5",)}


def type_fits(said: str, code: str) -> bool | None:
    """Whether a type said ("Citation") fits an ADS-B type code ("C68A"); None when it can't say."""
    s, c = (said or "").lower(), (code or "").upper()
    if not s or not c:
        return None
    for k, codes in TYPE_CODES.items():
        if k in s:
            return any(c.startswith(x) for x in codes)
    return None


# -- the watch ----------------------------------------------------------------------------------------------------------


@dataclass
class Track:
    callsign: str
    hex: str | None = None
    type: str = ""
    goaround: list = field(default_factory=list)          # times (words)
    adsb_ga: float | None = None                         # the track's go-around (climb after approach)
    abn: list = field(default_factory=list)              # (at, kind, label, strong, tid)
    snippets: list = field(default_factory=list)
    last_at: float = 0.0
    alert_id: int | None = None
    where: Where | None = None
    home: str = ""                                       # the tower channel it was first heard on
    seen_at: float | None = None                         # last on ADS-B
    dist: float | None = None

    def key(self) -> str:
        return self.hex or self.callsign


def _same(a: str, b: str) -> bool:
    a, b = (a or "").upper().lstrip("…"), (b or "").upper().lstrip("…")
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 3 and long_.endswith(short)


LEVEL_WORDS = {"1": 1, "2": 2, "3": 3, "one": 1, "won": 1, "two": 2, "three": 3, "i": 1, "ii": 2, "iii": 3}
ALERT_LEVEL = re.compile(r"\balert\s+(?:level\s+)?(?P<n>[123]|one|won|two|three|i{1,3})\b")
LEVEL_CONTEXT = re.compile(r"\b(?:hpn|airport|westchester|runway|rwy|final|aircraft|airplane|plane|jet|declar\w*|"
                           r"souls|arff|inbound|approach|landing)\b")
CANCEL = re.compile(r"\balert\b(?:\s+\w+){0,3}?\s+(?:is\s+|has been\s+|been\s+)?(?:cancel+ed|canceling|cancel+ing|terminated|"
                    r"called off|downgraded)\b|\bcancel+(?:ing|ed)?\s+(?:the\s+)?alert\b|\blanded without (?:incident|"
                    r"issue|problems?)\b|\b(?:is|are|now)?\s*down safe(?:ly)?\b|\b(?:on the ground|landed) safe(?:ly)?\b|"
                    r"\bsafely on the ground\b")
FINAL = re.compile(r"\b(?P<n>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen)[ -]?(?:mile|miles|mi)"
                   r"\s+final\b")
PROBLEM_60 = [
    (re.compile(r"\b(?:flaps?|flat|flats)\s+(?:issue|issues|problem|problems|fail\w*|stuck|indication|malfunction)\b|"
                r"\b(?:no|partial) flaps\b"), "flaps"),
    (re.compile(r"\b(?:landing\s+|nose\s+|main\s+)?gear\b"), "gear"),
    (re.compile(r"\bhydraulic"), "hydraulics"),
    (re.compile(r"\bengine\b"), "engine"),
    (re.compile(r"\bsmoke\b"), "smoke"),
    (re.compile(r"\bfire\b"), "fire"),
    (re.compile(r"\bfuel\b"), "fuel"),
    (re.compile(r"\bbird"), "bird strike"),
    (re.compile(r"\btires?\b"), "tire"),
    (re.compile(r"\bmedical\b"), "medical"),
    (re.compile(r"\bpressuri"), "pressurization"),
]


def level_of(norm: str) -> int | None:
    m = ALERT_LEVEL.search(norm)
    return LEVEL_WORDS.get(m.group("n")) if m else None


def declaration(text: str, context: bool = True) -> dict | None:
    """60 Control's "alert level N" for the airport: {level, type, problem, runway, final_nm}; None when it isn't one
    (no level, a cancel, or nothing about the airport or an aircraft in it; context=False: an alert level is open
    already, so "upgrade to an alert level 2" needs none)."""
    norm = airport._norm(text)
    if CANCEL.search(norm):
        return None
    n = level_of(norm)
    if n is None:
        return None
    rest = ALERT_LEVEL.sub(" ", norm)
    if context and not (LEVEL_CONTEXT.search(rest) or airport._AIRPORT.search(rest) or airport._TYPES.search(rest)):
        return None
    f = airport.fields(norm)
    problem = next((name for rx, name in PROBLEM_60 if rx.search(rest)), "") or f.get("acft_nature", "")
    fin = FINAL.search(rest)
    fnm = None
    if fin:
        v = fin.group("n")
        fnm = int(v) if v.isdigit() else airport._NUM.get(v)
    return {"level": n, "type": f.get("acft_type", ""), "problem": problem, "runway": f.get("acft_runway", ""),
            "final_nm": fnm}


def level_title(d: dict) -> str:
    """'HPN Alert 1 · standby · Citation · flaps · 5 mi final RWY 16' (Alert 1: PCFD stands by; 2 and 3: responds)."""
    parts = [f"HPN Alert {d['level']}"] + (["standby"] if int(d["level"]) == 1 else [])
    if d.get("type"):
        parts.append(d["type"])
    if d.get("problem"):
        parts.append(d["problem"])
    fin = f"{d['final_nm']} mi final" if d.get("final_nm") else ""
    rw = f"RWY {d['runway']}" if d.get("runway") else ""
    if fin or rw:
        parts.append(" ".join(x for x in (fin, rw) if x))
    return " · ".join(parts)


class AirportWatch:
    """The CAD's watch (cad.airport_watch): heard_atc() for every airband transcript, heard_radio() for every fire one,
    tick() for every Aircraft layer picture. The module doc."""

    def __init__(self, alerts, airband: Iterable[str] = (), fire_channels: Iterable[str] = (),
                 fire_talkgroups: Iterable[int] = (301, 307, 333), log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time):
        self.alerts, self.log, self.clock = alerts, log, clock
        self.airband = set(airband)
        self.fire_channels = set(fire_channels)        # "": any channel that isn't airband nor P25
        self.fire_talkgroups = set(fire_talkgroups)
        self.channels = Channels(())
        self.aircraft: Callable[[], list[dict]] = lambda: []
        self.incident_levels: frozenset[int] = frozenset(INCIDENT_LEVELS)
        self.enabled = True                               # [atc] enabled (the app's)
        self.cad = getattr(alerts, "cad", None)
        self.store = getattr(alerts, "store", None)
        self._index: atc.TelephonyIndex | None = None
        self.tracks: dict[str, Track] = {}
        self._last: dict[str, tuple[float, str]] = {}   # channel -> (when, track key): the exchange
        self._arriving: dict[str, float] = {}           # hex -> last seen arriving (the track's go-around)
        self._adsb_ga: dict[str, float] = {}            # hex -> when its track showed a go-around
        self._lock = threading.RLock()
        self.raised = 0

    def configure(self, channels: Iterable = (), aircraft: Callable[[], list[dict]] | None = None,
                  incident_levels: Iterable[int] = (2, 3), enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.channels = Channels(channels)
        self.airband |= {c.label for c in self.channels.air}
        if aircraft is not None:
            self.aircraft = aircraft
        self.incident_levels = frozenset(int(x) for x in incident_levels)

    @property
    def index(self) -> atc.TelephonyIndex:
        if self._index is None:
            self._index = atc.TelephonyIndex.load()
        return self._index

    def _planes(self) -> list[dict]:
        try:
            return [a for a in (self.aircraft() or []) if isinstance(a, dict)]
        except Exception:  # noqa: BLE001
            return []

    # -- ADS-B ---------------------------------------------------------------------------------------------------------

    def _observe(self, planes: Sequence[dict], now: float) -> None:
        for p in planes:
            h = p.get("hex")
            if not h:
                continue
            ph = p.get("phase") or ""
            if ph == "arriving":
                self._arriving[h] = now
            elif ph in ("near", "departing", "low") and now - self._arriving.get(h, -1e9) <= ADSB_ARRIVING_S:
                vr = p.get("vr")
                d = p.get("dist")
                if (not isinstance(vr, (int, float)) or vr >= 0) and (not isinstance(d, (int, float)) or d <= ADSB_GA_NM):
                    self._adsb_ga.setdefault(h, now)
        for memo in (self._arriving, self._adsb_ga):
            for h in [h for h, t in memo.items() if now - t > 3600]:
                del memo[h]
        for tr in self.tracks.values():
            if tr.hex and tr.hex in self._adsb_ga and tr.adsb_ga is None:
                tr.adsb_ga = self._adsb_ga[tr.hex]

    # -- who ---------------------------------------------------------------------------------------------------------

    def _who(self, ch: str, text: str, rec: dict | None, planes: list[dict], at: float) -> tuple[str, str | None, dict]:
        """(callsign, hex, the plane): the talker record's aircraft, else the callsign heard, else the channel's last."""
        ac = (rec or {}).get("aircraft") or None
        if not ac and (rec or {}).get("exchange"):
            ex = rec["exchange"]
            ac = {"callsign": ex.get("subject"), "hex": ex.get("hex") or None} if ex.get("subject") else None
        if ac and ac.get("callsign"):
            h = ac.get("hex") or None
            return str(ac["callsign"]).upper(), h, next((p for p in planes if h and p.get("hex") == h), {})
        att = atc.attribute(text, self.index, atc_scope.planes_for(ch, planes))
        if att is not None and not att.after_number:
            if att.hex is not None and att.confidence >= 0.4:
                return (att.callsign or att.spoken).upper(), att.hex, next((p for p in planes if p.get("hex") == att.hex), {})
            if att.spoken:
                tr = next((t for t in self.tracks.values() if _same(t.callsign, att.spoken)), None)
                if tr is not None:
                    return tr.callsign, tr.hex, next((p for p in planes if tr.hex and p.get("hex") == tr.hex), {})
                if not att.spoken.startswith("…"):
                    return att.spoken.upper(), None, {}
        last = self._last.get(ch)
        if last and at - last[0] <= EXCHANGE_S and last[1] in self.tracks:
            tr = self.tracks[last[1]]
            return tr.callsign, tr.hex, next((p for p in planes if tr.hex and p.get("hex") == tr.hex), {})
        return "", None, {}

    def _track(self, cs: str, h: str | None, plane: dict, create: bool) -> Track | None:
        tr = None
        if h:
            tr = next((t for t in self.tracks.values() if t.hex == h), None)
        if tr is None and cs:
            tr = next((t for t in self.tracks.values() if _same(t.callsign, cs)), None)
        if tr is None:
            if not create or not cs:
                return None
            tr = Track(cs, h)
            self.tracks[tr.key()] = tr
        if h and not tr.hex:
            self.tracks.pop(tr.key(), None)
            tr.hex = h
            self.tracks[tr.key()] = tr
        if cs and (tr.callsign.startswith("…") or (plane and len(cs) > len(tr.callsign))):
            tr.callsign = cs
        if plane:
            tr.type = plane.get("type") or tr.type
        return tr

    # -- airband -------------------------------------------------------------------------------------------------------

    def heard_atc(self, t: dict, rec: dict | None = None) -> dict | None:
        """One airband transcript {id, source, start, end, text} (the written form, sizeup/atc_normalize.py), with its
        ATC talker record when there is one. The pre-alert it raised or added to."""
        ch = str(t.get("source") or "")
        text = str(t.get("text") or "").strip()
        if not text or (self.airband and ch not in self.airband):
            return None
        at = float(t.get("end") or t.get("start") or self.clock())
        now = self.clock()
        norm = fuzzy(atc.normalize(text))
        hits = abnormal(norm)
        ga = go_around(norm)
        planes = self._planes()
        with self._lock:
            self._observe(planes, now)
            self._forget(now)
            cs, h, plane = self._who(ch, text, rec, planes, at)
            tr = self._track(cs, h, plane, create=bool(hits or ga))
            if tr is None:
                return None
            if tr.hex and tr.adsb_ga is None and tr.hex in self._adsb_ga:
                tr.adsb_ga = self._adsb_ga[tr.hex]
            if plane:
                tr.seen_at, tr.dist = now, plane.get("dist") if isinstance(plane.get("dist"), (int, float)) else tr.dist
            self._last[ch] = (at, tr.key())
            tr.last_at = at
            if atc_scope.facility(ch) == "tower" and not tr.home:
                tr.home = ch
            snip = {"id": str(t.get("id") or ""), "at": at, "channel": ch, "text": text[:400],
                    "hits": [w for _a, w in hits] + ([ga] if ga else []), "context": not (hits or ga)}
            if ga:
                tr.goaround.append(at)
            for a, _w in hits:
                tr.abn.append((at, a.kind, a.label, a.strong, snip["id"]))
            if hits or ga:
                tr.snippets = [s for s in tr.snippets if s["id"] != snip["id"]][-10:] + [snip]
            moved = self._handoff(tr, ch, norm, at)
            if tr.alert_id is not None:
                return self._more(tr, snip, bool(hits or ga), moved)
            return self._maybe_raise(tr, ch, at)

    def _forget(self, now: float) -> None:
        for k in [k for k, tr in self.tracks.items() if tr.alert_id is None and now - tr.last_at > KEEP_S]:
            del self.tracks[k]

    def _handoff(self, tr: Track, ch: str, norm: str, at: float) -> bool:
        """A frequency change for the aircraft of an open pre-alert, or the aircraft heard elsewhere: its Where. True
        when it changed."""
        if tr.alert_id is None:
            return False
        old = tr.where.label if tr.where else ""
        fq = frequency(norm)
        fac = _FACILITY.search(norm)
        said_handoff = bool(_HANDOFF.search(norm))
        readback = fq is not None and len(norm[:fq[1]].split()) <= 2     # a readback starts with it: "20.8, <callsign>"
        if (fq is not None and (said_handoff or readback)) or (fq is None and said_handoff and fac
                                                               and fac.group("f") not in ("tower",)):
            w = self.channels.where(fq[0] if fq else None, fac.group("f") if fac else "", at)
            if w.channel != ch:
                tr.where = w
        elif tr.where is not None and ch == tr.where.channel:
            tr.where.receiving = True                     # heard there: on it
        elif tr.where is not None and ch != tr.where.channel and (not self.airband or ch in self.airband) \
                and (_RETURN.search(norm) or at - tr.where.at > RETURN_S):
            # heard on another of ours (the tower again: "on the visual 34"; not the hand-off's own last words)
            tr.where = Where(f"back on {ch}" if atc_scope.facility(ch) == "tower" else ch, ch, None, True, at)
        elif tr.where is None and ch in self.airband and atc_scope.facility(ch) != "tower":
            tr.where = Where(ch, ch, None, True, at)
        return (tr.where.label if tr.where else "") != old

    def _story(self, tr: Track, at: float) -> tuple[str, float] | None:
        """(rule, confidence) when the aircraft's last WINDOW_S makes a pre-alert, else None."""
        abn = [x for x in tr.abn if at - x[0] <= WINDOW_S]
        if not abn:
            return None
        ga = [g for g in tr.goaround if abs(at - g) <= WINDOW_S] or ([tr.adsb_ga] if tr.adsb_ga and at - tr.adsb_ga <= WINDOW_S else [])
        txs = {x[4] for x in abn}
        strong = any(x[3] for x in abn)
        adsb = 0.15 if tr.adsb_ga and at - tr.adsb_ga <= WINDOW_S else 0.0
        if ga:
            return "goaround", min(0.95, 0.6 + 0.1 * (len(txs) - 1) + (0.1 if strong else 0.0) + adsb)
        if len(txs) >= 2 and strong:
            return "repeated", min(0.95, 0.55 + 0.1 * (len(txs) - 2) + adsb)
        return None

    def _labels(self, tr: Track, at: float) -> list[str]:
        return list(dict.fromkeys(x[2] for x in tr.abn if at - x[0] <= 3 * WINDOW_S))

    def _lines(self, tr: Track, plane: dict | None = None) -> list[list[str]]:
        """The card's lines (the 60 Control alert's line kept)."""
        p = plane or {}
        acft = tr.callsign + (f" · {tr.type}" if tr.type else "")
        out = [["Aircraft", acft]]
        row = self._row(tr.alert_id)
        if not ((row or {}).get("data", {}).get("alert_level")):
            out.append(["Status", "possible incoming"])    # the earliest warning: never an incident by itself
        labels = self._labels(tr, tr.last_at)
        labels = [x for x in labels if x != "a problem"] or labels           # the vague one only when it's all there is
        out.append(["Heard", " · ".join((["go-around"] if tr.goaround else []) + labels) or "—"])
        if tr.adsb_ga:
            out.append(["ADS-B", "go-around (climb after approach)"])
        if p:
            bits = []
            if isinstance(p.get("alt"), (int, float)):
                bits.append(f"{int(p['alt']):,} ft")
            elif p.get("alt") == "ground":
                bits.append("GND")
            if isinstance(p.get("dist"), (int, float)):
                bits.append(f"{p['dist']} NM")
            if p.get("phase"):
                bits.append(p["phase"])
            if bits:
                out.append(["Track", " · ".join(bits)])
        if tr.where is not None:
            out.append(["Where", tr.where.text()])
        out += [list(x) for x in ((row or {}).get("data", {}).get("lines") or []) if x and x[0] in ("Alert", "Event")]
        return out

    def _acft(self, tr: Track, plane: dict) -> dict:
        a = {"callsign": tr.callsign, "hex": tr.hex, "spoken": tr.callsign, "match": 1.0 if tr.hex else 0.0}
        for k in ("type", "model", "reg", "lat", "lon", "alt", "gs", "trk", "phase", "rwy", "dist", "pos_at"):
            if plane.get(k) is not None:
                a[k] = plane[k]
        if tr.type and "type" not in a:
            a["type"] = tr.type
        return a

    def _alert(self, tr: Track, ch: str, at: float, rule: str, conf: float, snips: list[dict]) -> AirportAlert:
        plane = next((p for p in self._planes() if tr.hex and p.get("hex") == tr.hex), {})
        labels = self._labels(tr, at)
        things = ", ".join(([x for x in labels if x != "a problem"] or labels)[:3])     # the title: the three that say most
        head = "go-around" if rule == "goaround" else "abnormal"
        who = tr.callsign + (f" ({tr.type})" if tr.type else "")
        title = f"HPN ATC: {head} · {things} · {who}"
        detail = f"{'go-around and ' if rule == 'goaround' else 'repeated '}abnormal phrases · {ch}"
        return AirportAlert(source="atc", kind="abnormal", subject=f"acft:{tr.callsign}", title=title, detail=detail,
                            confidence=round(conf, 2), at=at, channel=ch, group="", snippets=snips,
                            aircraft=[self._acft(tr, plane)], lines=self._lines(tr, plane), rank=2,
                            event={"type": "AIRPORT", "subtype": ""})

    def _maybe_raise(self, tr: Track, ch: str, at: float) -> dict | None:
        st = self._story(tr, at)
        if st is None:
            return None
        rule, conf = st
        a = self._alert(tr, ch, at, rule, conf, list(tr.snippets))
        row = None
        lv = self._level_row()
        if lv is not None and not any((x or {}).get("hex") for x in lv["data"].get("aircraft") or []) \
                and type_fits(lv["data"].get("alert_level", {}).get("type", ""), tr.type) is not False:
            row = self.alerts.add_to(lv["id"], a)            # 60 Control declared it first: the same story
        if row is None:
            try:
                row, new = self.alerts.raise_alert(a)
            except Exception as e:  # noqa: BLE001 - a pre-alert never stops the radio
                self.log(f"airport watch: {type(e).__name__}: {e}")
                return None
            if new:
                self.raised += 1
                self._poke("airport pre-alert (ATC)")     # a page may be coming: the county's feed polled now
        if row is not None:
            tr.alert_id = row["id"]
            tr.where = tr.where or (Where(ch, ch, None, True, at) if ch else None)
            self.log(f"airport watch: {tr.callsign}: {rule} -> pre-alert #{row['id']} ({conf:.2f})")
        return row

    def _more(self, tr: Track, snip: dict, abn: bool, moved: bool) -> dict | None:
        """The aircraft of an open pre-alert heard again: the transmission added; escalated when abnormal."""
        st = self._story(tr, snip["at"]) if abn else None
        plane = next((p for p in self._planes() if tr.hex and p.get("hex") == tr.hex), {})
        if st is not None:
            a = self._alert(tr, snip["channel"], snip["at"], st[0], st[1], [snip])
        else:
            row = self._row(tr.alert_id)
            conf = float(row["confidence"]) if row else 0.5
            a = AirportAlert(source="atc", kind="abnormal", subject=f"acft:{tr.callsign}", title="", confidence=conf,
                             at=snip["at"], channel=snip["channel"], snippets=[snip], aircraft=[self._acft(tr, plane)],
                             lines=self._lines(tr, plane), rank=0)
        out = self.alerts.add_to(tr.alert_id, a)
        if out is None:
            tr.alert_id = None
            return None
        if moved and tr.where is not None:
            self.log(f"airport watch: {tr.callsign}: {tr.where.text()}")
        return out

    def _row(self, pid: int | None) -> dict | None:
        if pid is None:
            return None
        try:
            return self.alerts.get(pid)
        except Exception:  # noqa: BLE001
            return None

    # -- the Aircraft layer's pictures -----------------------------------------------------------------------------------

    def tick(self, planes: Sequence[dict] | None = None, now: float | None = None) -> None:
        """A new picture: the track's go-around; each open pre-alert's aircraft followed (kept open while it flies,
        ended when it lands at HPN or leaves the area)."""
        now = self.clock() if now is None else now
        planes = [p for p in (planes if planes is not None else self._planes()) if isinstance(p, dict)]
        with self._lock:
            self._observe(planes, now)
            for tr in list(self.tracks.values()):
                if tr.alert_id is None:
                    continue
                row = self._row(tr.alert_id)
                if row is None or row["status"] != "open":
                    tr.alert_id = None
                    continue
                p = next((x for x in planes if tr.hex and x.get("hex") == tr.hex), None)
                why = ""
                if p is not None:
                    tr.seen_at = now
                    d = p.get("dist")
                    tr.dist = d if isinstance(d, (int, float)) else tr.dist
                    on_ground = p.get("phase") == "ground" or p.get("alt") == "ground"
                    if on_ground and (tr.dist is None or tr.dist <= LANDED_NM):
                        why = "ADS-B: landed at HPN"
                    elif isinstance(d, (int, float)) and d > LEAVE_NM:
                        why = f"ADS-B: left the area ({d:g} NM out)"
                elif tr.hex and tr.seen_at is not None and now - tr.seen_at >= LOST_S \
                        and (tr.dist is None or tr.dist >= LEAVE_NEAR_NM):
                    why = "ADS-B: left the area (no longer received)"
                if why:
                    self._end(tr, why)
                elif p is not None:
                    self.alerts.update_id(tr.alert_id, lines=self._lines(tr, p), aircraft=[self._acft(tr, p)])

    def _end(self, tr: Track, why: str) -> None:
        if tr.alert_id is not None:
            self.alerts.end_id(tr.alert_id, why)
            self.log(f"airport watch: {tr.callsign}: {why}")
        self.tracks.pop(tr.key(), None)

    # -- 60 Control ----------------------------------------------------------------------------------------------------

    def handles_radio(self, channel: str, talkgroup) -> bool:
        if channel in self.airband:
            return False
        if talkgroup is not None:
            return talkgroup in self.fire_talkgroups
        return not self.fire_channels or channel in self.fire_channels

    def heard_radio(self, t: dict) -> dict | None:
        """One fire-radio transcript {id, channel, talkgroup, start, text}: an alert level declared or canceled."""
        text = str(t.get("text") or "").strip()
        if not self.enabled or not text or not self.handles_radio(str(t.get("channel") or ""), t.get("talkgroup")):
            return None
        at = float(t.get("start") or self.clock())
        norm = airport._norm(text)
        with self._lock:
            if CANCEL.search(norm):
                return self._cancel(t, text, at)
            d = declaration(text, context=self._level_row() is None)
            if d is None:
                return None
            return self._declared(t, text, at, d)

    def _level_row(self) -> dict | None:
        for r in self.alerts.open_rows(LEVEL_KEEP_S):
            if r["data"].get("alert_level"):
                return r
        return None

    def _tower_row(self, d: dict) -> dict | None:
        """The open tower / ADS-B pre-alert about an aircraft that this declaration is about: the only one, or the
        only one whose type fits."""
        rows = [r for r in self.alerts.open_rows(ATTACH_S) if r["source"] in ("atc", "adsb") and r["data"].get("aircraft")]
        if not rows:
            return None
        fits = [r for r in rows if any(type_fits(d.get("type", ""), (x or {}).get("type", "")) for x in r["data"]["aircraft"])]
        if len(fits) == 1:
            return fits[0]
        ok = [r for r in rows if not any(type_fits(d.get("type", ""), (x or {}).get("type", "")) is False
                                         for x in r["data"]["aircraft"])]
        return ok[0] if len(ok) == 1 else None

    def _snip(self, t: dict, text: str, at: float, hits: list[str]) -> dict:
        ch = str(t.get("channel") or "")
        return {"id": str(t.get("id") or ""), "at": at, "channel": ch, "text": text[:400], "hits": hits, "context": False}

    def _declared(self, t: dict, text: str, at: float, d: dict) -> dict | None:
        title = level_title(d)
        snip = self._snip(t, text, at, [f"alert level {d['level']}"])
        cur = self._level_row()
        prev = (cur or {}).get("data", {}).get("alert_level") or {}
        if cur is not None:
            # a readback or a repeat: the facts said before stand where this one says none
            d = {k: (v if v not in ("", None) else prev.get(k, v)) for k, v in d.items()}
            d["level"] = max(int(d["level"]), int(prev.get("level") or 0))
            title = level_title(d)
        target = cur or self._tower_row(d)
        if target is not None and not d.get("type"):     # the type the tower's aircraft has on ADS-B
            d["type"] = next((x.get("type") for x in target["data"].get("aircraft") or [] if (x or {}).get("type")), "")
            title = level_title(d)
        line = ["Alert", f"{title.removeprefix('HPN ')} · {str(t.get('channel') or '60 Control')} "
                         f"{datetime.fromtimestamp(at, TZ).strftime('%H:%M')}"]
        rank = 4 + int(d["level"])
        a = AirportAlert(source="radio", kind="alert_level", subject="alert-level", title=title,
                         detail=f"60 Control: alert level {d['level']} declared", confidence=1.0, at=at,
                         channel=str(t.get("channel") or ""), snippets=[snip], rank=rank, limit=False,
                         event={"type": "AIRPORT", "subtype": airport.ALERTS.get(str(d["level"]), "")})
        escalated = False
        if target is not None:
            lines = [x for x in (target["data"].get("lines") or []) if x[0] not in ("Alert", "Status")] + [line]
            a.lines = lines
            out = self.alerts.add_to(target["id"], a)
            if out is None:
                return None
            escalated = int(d["level"]) > int(prev.get("level") or 0)
            out = self.alerts.update_id(out["id"], data={"alert_level": d}) or out
            if escalated:
                self.alerts.repush(out["id"])            # it is an alert now (or a higher one): pushed again
        else:
            took = self._taken_level(d, title, text, at)
            if took is not None:                          # a person accepted it already: its event escalated
                return took
            a.lines, a.push = [line], False               # pushed below, once its level is on it (the top priority)
            out, _new = self.alerts.raise_alert(a)
            if out is None:
                return None
            out = self.alerts.update_id(out["id"], data={"alert_level": d}) or out
            self.alerts.repush(out["id"])                 # raised with its level: pushed at the top priority
            escalated = True
        if escalated:
            self.log(f"airport watch: {title} (pre-alert #{out['id']})")
            self._poke(f"airport alert level {d['level']}")
            if int(d["level"]) in self.incident_levels:
                out = self._incident(out, d, title, text, at) or out
        return out

    def _poke(self, why: str) -> None:
        """The county's feed polled now (#297's trigger: the station's sources_poll_now), so its event comes at once."""
        try:
            self.cad.sources.poke(why)
        except Exception as e:  # noqa: BLE001 - never in the way
            self.log(f"airport watch: poll now: {type(e).__name__}")

    def _incident(self, row: dict, d: dict, title: str, text: str, at: float) -> dict | None:
        """An alert level PCFD responds to ([atc] alert_level_incident, Alert 2 and 3 by default): a queued Airport incident
        at the airport (never dispatched), the pre-alert on it and still shown. One already made: its subtype raised and a
        line on its timeline. The county's event or the page attaches to it when it comes (Sources.alert_incident); in 60
        Control mode the county's event makes it a tracked incident (sizeup/cad/automode.py)."""
        cad, sub = self.cad, airport.ALERTS.get(str(d["level"]), "")
        try:
            if row.get("incident_id"):
                inc = cad.store.incident(row["incident_id"])
                if inc["status"] in ("queued", "pending", "active") and inc["subtype"] != sub:
                    cad.store.update_incident(inc["id"], {"subtype": sub}, "60 Control radio")
                with cad.store.tx() as db:
                    cad.store._event(db, inc["id"], "prealert", f"60 Control: {title.removeprefix('HPN ')}: “{text[:200]}”", at=at)
                return self.alerts.get(row["id"])
            from sizeup.cad.airport_alerts import remarks
            f = {"type": airport.TYPE, "subtype": sub, "received_via": "RADIO", "emergency": True,
                 "remarks": remarks(row)[:4000], "acft_runway": d.get("runway") or ""}
            if d.get("type"):
                f["acft_type"] = d["type"][:60]
            if d.get("problem"):
                f["acft_nature"] = d["problem"][:60]
            airport.default_location(f)
            from sizeup.cad.sources import ALERT_SOURCE
            iid = cad.store.create_incident(**f, source=ALERT_SOURCE, source_ref=f"alert-level:{row['id']}", status="queued")
            inc = cad.store.incident(iid)
            with cad.store.tx() as db:
                cad.store._event(db, iid, "prealert", f"60 Control: {title.removeprefix('HPN ')}: “{text[:200]}” · queued, "
                                 "not dispatched", at=at)
            self.alerts._link([row["id"]], iid, inc.get("number") or "")
            lines = [x for x in (row["data"].get("lines") or []) if x[0] != "Event"] + [["Event", "queued · not dispatched"]]
            self.alerts.update_id(row["id"], lines=lines)
            cad._prealert_match(inc)
            self.log(f"airport watch: {title}: event {iid} queued")
            return self.alerts.get(row["id"])
        except Exception as e:  # noqa: BLE001 - the pre-alert stays
            self.log(f"airport watch: alert level {d['level']}: no event ({type(e).__name__}: {e})")
            return None

    def _taken_level(self, d: dict, title: str, text: str, at: float) -> dict | None:
        """An alert level's pre-alert a person already accepted, with its event still open: a higher level is that
        event's (subtype, a line), not a second one."""
        if self.store is None:
            return None
        r = self.store.one("SELECT id FROM airport_alerts WHERE status IN ('accepted', 'ended') AND incident_id IS NOT NULL "
                           "AND data LIKE '%\"alert_level\"%' AND updated_at >= ? ORDER BY id DESC LIMIT 1",
                           (self.clock() - LEVEL_KEEP_S,))
        if r is None:
            return None
        row = self.alerts.get(r["id"])
        inc = self.store.incident(row["incident_id"])
        if inc["status"] not in ("queued", "pending", "active"):
            return None
        if int(d["level"]) in self.incident_levels:
            self._incident(row, d, title, text, at)
        else:
            with self.store.tx() as db:
                self.store._event(db, inc["id"], "prealert", f"60 Control: “{text[:200]}”", at=at)
        return self.alerts.get(r["id"])

    def _cancel(self, t: dict, text: str, at: float) -> dict | None:
        """"Alert canceled", "landed without incident", "down safely": the alert level's pre-alert (or, with none, the
        only open tower pre-alert this watch follows) ended; an event it made gets a line on its timeline, never
        closed by this (a person, or the county's in service, closes it)."""
        row = self._level_row()
        if row is None:
            open_ids = [tr.alert_id for tr in self.tracks.values() if tr.alert_id is not None]
            rows = [r for r in (self._row(i) for i in open_ids) if r and r["status"] == "open"]
            row = rows[0] if len(rows) == 1 else None
        if row is None:
            return self._cancel_event(t, text, at)
        snip = self._snip(t, text, at, ["canceled"])
        self.alerts.add_to(row["id"], AirportAlert(source="radio", kind="alert_level", subject="alert-level", title="",
                                                   confidence=float(row["confidence"]), at=at, snippets=[snip], rank=0))
        out = self.alerts.end_id(row["id"], f"60 Control: {text[:120]}")
        if row.get("incident_id"):
            self._note(row["incident_id"], text, at)
        for tr in [x for x in self.tracks.values() if x.alert_id == row["id"]]:
            self.tracks.pop(tr.key(), None)
        return out

    def _note(self, iid: int, text: str, at: float) -> None:
        with self.store.tx() as db:
            self.store._event(db, iid, "prealert", f"60 Control: “{text[:200]}” · not closed by this", at=at)
        self.log(f"airport watch: event {iid}: alert canceled noted (left open)")

    def _cancel_event(self, t: dict, text: str, at: float) -> dict | None:
        """An alert level's pre-alert a person already accepted (its event open): the cancel is a line on the event."""
        if self.store is None:
            return None
        r = self.store.one("SELECT a.incident_id FROM airport_alerts a JOIN incidents i ON i.id = a.incident_id "
                           "WHERE a.status = 'accepted' AND a.data LIKE '%\"alert_level\"%' AND a.updated_at >= ? AND "
                           "i.status IN ('queued', 'pending', 'active') ORDER BY a.id DESC LIMIT 1",
                           (self.clock() - LEVEL_KEEP_S,))
        if r is not None:
            self._note(r["incident_id"], text, at)
        return None

    def state(self) -> dict:
        return {"tracks": len(self.tracks), "following": sum(1 for t in self.tracks.values() if t.alert_id is not None),
                "raised": self.raised}
