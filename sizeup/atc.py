"""HPN ATC pre-alerts (GitHub #233): what Westchester County Airport's tower says that could become a PCFD response.

User (#233): "now i want to do speech to text on this and all of the monitoring we discussed to get some pre-alerts
from the airport if anything related to an incident or issue that might cause an alert level response for us or
otherwise is brought up, example the other day there were some planes taking off that thought they saw a brush fire,
and so they called it in over ATC and we should have gotten a pre alert on that".

    the channel     an airband channel ([[radio.channels]] kind = "airband": HPN Tower 118.575 AM, sizeup/radio/am.py),
                    recorded and transcribed like any traffic channel (Amazon Transcribe by default, a priority channel;
                    the custom vocabulary sizeup-atc and whisper's prompt from data/atc/vocabulary.json)
    the rules       data/atc/rules.json: weighted regular expressions in categories (fire, emergency, ARFF, accident,
                    runway, gear, engine, bird, fuel, medical, laser), 'not' patterns for the words that mean something
                    else (no smoking, a drill), an ATIS / NOTAM / weather readout in which only the 'always' rules count.
                    Rules (match), not a model: every pre-alert says which words made it.
    the exchange    each transmission is read with the few before it on the same channel (atc_context, within
                    atc_context_s): a pilot's "we see smoke off the end" and the controller's "roll the trucks" are one
                    pre-alert, and an earlier hit adds to a later one at context_factor
    the aircraft    the callsign heard (shortfinal's core/callsign.py: airline telephony from FAA JO 7340.2 in
                    data/airport/airlines.csv plus data/atc/khpn_operators.csv, N-numbers in phonetics, abbreviated
                    tails), matched only against aircraft the Aircraft layer has near HPN (sizeup/aircraft.py): type,
                    position, phase. Heard but not on ADS-B: said so, never guessed
    the location    the runway and the words ("off the departure end of runway 16", "two miles south of the field",
                    "by Rye Lake"); a fire or smoke off the airport gets an APPROXIMATE point only when the words give
                    one (past a runway's departure end along its heading, or a compass direction and a distance from
                    the field), with a radius and the reason; otherwise none. Never a precise-looking point.
    the pre-alert   sizeup/cad/airport_alerts.py (source "atc"): the title ("HPN ATC: possible brush fire reported off
                    runway 16 departure"), the transcript snippets with their audio, the aircraft, the location words,
                    a confidence; accept (a pending event, never a dispatch) or dismiss; de-duplicated per subject
                    (atc_dedupe_min) and exchange; at most atc_max_per_hour; pushed to the alerts topic once

Runs on the CAD of record only (the hub, or all-in-one): App._atc(), next to the alarms, for every transcript of an
airband channel (new, or new text from Amazon Transcribe). ATC is public radio: the words go in the pre-alert and the
push as heard.
"""

from __future__ import annotations

import csv
import json
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Sequence

from sizeup.cad.airport_alerts import AirportAlert

ROOT = Path(__file__).resolve().parent.parent
RULES_FILE = ROOT / "data" / "atc" / "rules.json"
VOCABULARY_FILE = ROOT / "data" / "atc" / "vocabulary.json"
OPERATORS_FILE = ROOT / "data" / "atc" / "khpn_operators.csv"
AIRLINES_FILE = ROOT / "data" / "airport" / "airlines.csv"
VOCABULARY = "sizeup-atc"          # Amazon Transcribe custom vocabulary for the airband channels
NM_M = 1852.0
PROMPT_MAX = 760                   # whisper reads ~224 prompt tokens (shortfinal stt_prompt.MAX_CHARS)

# HPN (FAA Chart Supplement, as in sizeup/aircraft.py): the runway ends and the airport reference point
ARP = (41.0669531, -73.7075661)
RUNWAY_ENDS = {"16": (41.07555312, -73.71330558), "34": (41.06000083, -73.70140237),
               "11": (41.06705197, -73.71576050), "29": (41.06442797, -73.69999812)}
OPPOSITE = {"16": "34", "34": "16", "11": "29", "29": "11"}

_N = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "tree": 3, "four": 4, "fower": 4, "five": 5, "fife": 5,
      "six": 6, "seven": 7, "eight": 8, "nine": 9, "niner": 9, "ten": 10, "eleven": 11, "twelve": 12, "a": 1, "an": 1}
_DIGIT = r"(?:\d|zero|one|two|three|tree|four|fower|five|fife|six|seven|eight|nine|niner)"
_RUNWAY = re.compile(rf"\b(?:runway|rwy)\s+(?P<a>{_DIGIT})\s*(?P<b>{_DIGIT})(?!\s*{_DIGIT})|\b(?:runway|rwy)\s+(?P<n>16|34|11|29|sixteen|eleven)\b")
_DEPARTING = re.compile(rf"\b(?:departing|departure|departed|takeoff|take off|took off|climbing out)\b(?:\s+(?:off|from|on))?(?:\s+(?:of\s+)?(?:runway|rwy))?\s+(?P<a>{_DIGIT})\s*(?P<b>{_DIGIT})\b"
                        rf"|\b(?:runway|rwy)\s+(?P<c>{_DIGIT})\s*(?P<d>{_DIGIT})\s+(?:departure|departing)\b"
                        rf"|\bcleared for (?:takeoff|take off)\s+(?:runway\s+)?(?P<e>{_DIGIT})\s*(?P<f>{_DIGIT})\b"
                        rf"|\b(?:runway|rwy)\s+(?P<g>{_DIGIT})\s*(?P<h>{_DIGIT})\b[^.]{{0,20}}\bcleared for (?:takeoff|take off)\b")
_END = re.compile(r"\b(?:off|past|beyond|at|near) the (?P<which>departure |approach |far |)end\b(?: of (?:the )?(?:runway|rwy)(?: (?P<rw>[a-z0-9 ]{2,12}?))?\b)?")
_DIRS = {"north": 0, "northeast": 45, "north east": 45, "east": 90, "southeast": 135, "south east": 135, "south": 180,
         "southwest": 225, "south west": 225, "west": 270, "northwest": 315, "north west": 315}
_DIR = r"(?:north ?east|north ?west|south ?east|south ?west|north|south|east|west)"
_QTY = r"(?:\d+(?: \d)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|half a|a half|a couple(?: of)?|couple(?: of)?|a few)"
_COMPASS = re.compile(rf"\b(?:(?P<q>{_QTY}) (?:miles?|nm|nautical miles?) )?(?:to the |just |due )?(?P<d>{_DIR}) of (?:the )?(?P<of>field|airport|runway|westchester|tower)\b"
                      rf"|\b(?P<q2>{_QTY}) (?:miles?|nm) (?:to the )?(?P<d2>{_DIR})\b")
_CLOCK = re.compile(r"\b(?:at |about )?(?:our |my |your |the )?(?P<h>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve) ?o ?clock\b")
_SIDE = re.compile(r"\boff (?:our|my|the|your) (?P<s>left|right) (?:side|wing)?\b|\b(?:to|on) (?:our|my|the) (?P<s2>left|right)\b")
_DIST = re.compile(rf"\b(?P<q>{_QTY}) (?:miles?|nm)\b")


def normalize(text: str) -> str:
    """Lower case, apostrophes out ("o'clock" -> "oclock"), every other mark a space, one space between words."""
    t = (text or "").lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def _num(w: str | None) -> float | None:
    if not w:
        return None
    w = w.strip()
    if w in ("half a", "a half"):
        return 0.5
    if w.startswith(("a couple", "couple")):
        return 2.0
    if w == "a few":
        return 3.0
    if re.fullmatch(r"\d+ \d", w):
        a, b = w.split()
        return float(f"{a}.{b}")
    if w.isdigit():
        return float(w)
    return float(_N[w]) if w in _N else None


def _digit(w: str) -> str:
    return w if w.isdigit() else str(_N.get(w, ""))


def runways(norm: str) -> list[str]:
    """HPN runways said ("runway one six", "runway 16", "runway 1 6"), in order, each once."""
    out = []
    for m in _RUNWAY.finditer(norm):
        rw = (_digit(m.group("a")) + _digit(m.group("b"))) if m.group("a") else \
            {"sixteen": "16", "eleven": "11"}.get(m.group("n"), m.group("n"))
        if rw in RUNWAY_ENDS and rw not in out:
            out.append(rw)
    return out


def departure_runway(norm: str) -> str:
    """The runway a departure is on, when the words say so ("departing runway one six", "cleared for takeoff 16")."""
    for m in _DEPARTING.finditer(norm):
        for a, b in (("a", "b"), ("c", "d"), ("e", "f"), ("g", "h")):
            if m.group(a):
                rw = _digit(m.group(a)) + _digit(m.group(b))
                if rw in RUNWAY_ENDS:
                    return rw
    return ""


# -- the rules ------------------------------------------------------------------------------------------------------


def _rx(p: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9])(?:{p})(?![a-z0-9])")


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    weight: float
    rx: re.Pattern
    thing: str
    never: re.Pattern | None = None
    always: bool = False
    event_type: str = ""


@dataclass(frozen=True)
class Hit:
    rule: Rule
    text: str            # the words that matched


class Rules:
    """data/atc/rules.json, compiled (the file's _about says how to read it)."""

    def __init__(self, d: dict):
        self.version = int(d.get("version", 1))
        self.threshold = float(d.get("threshold", 0.5))
        self.context_factor = float(d.get("context_factor", 0.5))
        self.categories: dict[str, dict] = dict(d.get("categories") or {})
        self.rules: list[Rule] = []
        for r in d.get("rules") or []:
            if r["category"] not in self.categories:
                raise ValueError(f"rule {r['id']}: no category {r['category']!r}")
            w = float(r["weight"])
            if not 0 < w <= 1:
                raise ValueError(f"rule {r['id']}: weight 0 to 1")
            self.rules.append(Rule(r["id"], r["category"], w, _rx(r["pattern"]), r.get("thing") or r["id"],
                                   _rx(r["not"]) if r.get("not") else None, bool(r.get("always")), r.get("event_type", "")))
        cues = d.get("cues") or {}
        self.cues = {k: _rx(v) for k, v in cues.items() if isinstance(v, str)}
        sup = (d.get("suppress") or {}).get("pattern")
        self.suppress = re.compile(sup) if sup else None
        self.landmarks = [normalize(x) for x in d.get("landmarks") or [] if normalize(x)]

    @classmethod
    def load(cls, path: str | Path | None = None) -> Rules:
        return cls(json.loads(Path(path or RULES_FILE).read_text()))

    def hits(self, norm: str) -> list[Hit]:
        """The rules that match this (normalized) transmission: one hit per rule; 'not' and the readout suppression
        applied. A rule whose words are inside a longer rule's (smoke in "smoke in the cockpit") still counts once."""
        readout = bool(self.suppress and self.suppress.search(norm))
        out = []
        for r in self.rules:
            if readout and not r.always:
                continue
            m = r.rx.search(norm)
            if not m or (r.never is not None and r.never.search(norm)):
                continue
            out.append(Hit(r, m.group(0)))
        # the generic fire / smoke / emergency word that is part of a more specific hit adds nothing of its own
        spans = [h for h in out if h.rule.weight >= 0.6]
        return [h for h in out if h.rule.weight >= 0.6 or not any(h.text in s.text and h is not s for s in spans)]

    def cue(self, name: str, norm: str) -> bool:
        rx = self.cues.get(name)
        return bool(rx and rx.search(norm))


def score(weights: Iterable[float]) -> float:
    p = 1.0
    for w in weights:
        p *= 1.0 - max(0.0, min(0.99, w))
    return 1.0 - p


# -- callsigns (shortfinal core/callsign.py, ported: find_mentions, attribute, _match_tail) ----------------------------

DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "tree": "3", "four": "4", "fower": "4", "five": "5",
          "fife": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9"}
HOMOPHONES = {"for": "4", "fore": "4", "to": "2", "too": "2", "won": "1", "oh": "0"}
TEENS = {"ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
         "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19"}
TENS = {"twenty": "2", "thirty": "3", "forty": "4", "fifty": "5", "sixty": "6", "seventy": "7", "eighty": "8", "ninety": "9"}
TYPE_WORDS = {"cessna", "piper", "aerostar", "citation", "gulfstream", "challenger", "learjet", "lear", "falcon", "embraer",
              "phenom", "pilatus", "bonanza", "baron", "cirrus", "mooney", "diamond", "king", "beech", "beechcraft",
              "hawker", "global", "helicopter", "november", "twin", "skylane", "skyhawk", "saratoga", "seneca", "conquest",
              "eclipse"}
LETTERS = {"alfa": "A", "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E", "foxtrot": "F", "golf": "G",
           "hotel": "H", "india": "I", "juliet": "J", "juliett": "J", "kilo": "K", "lima": "L", "mike": "M", "november": "N",
           "oscar": "O", "papa": "P", "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U",
           "victor": "V", "whiskey": "W", "whisky": "W", "xray": "X", "yankee": "Y", "zulu": "Z"}
ALIAS_STOP = {"air", "jet", "jets", "express", "shuttle", "wings", "star", "sky", "airlines", "aviation", "cargo", "charter",
              "flight", "international", "service", "services", "lines", "line", "airways", "connection", "bird", "eagle", "king"}
MIN_P = 0.2


_SPELL = {v: k for k, v in LETTERS.items() if k not in ("alpha", "juliet", "whisky")}
_WRITTEN_TAIL = re.compile(r"\b(N?)(\d{1,5})([A-Z]{1,3})\b")
_WRITTEN_FLIGHT = re.compile(r"\b([A-Z]{3})(\d{1,4})\b")      # "MXY283", as sizeup/atc_snap.py writes it
ICAO = "icao"                                                   # its word: "icaomxy", then the digits


def words_of(text: str) -> list[str]:
    """The words, lower case. A tail written the way sizeup/atc_normalize.py writes it ("N123AB", "568LX") is read as
    it was said ("november one two three alfa bravo"), so the matcher sees the same characters either way."""
    t = _WRITTEN_TAIL.sub(lambda m: " ".join((["november"] if m.group(1) else []) + list(m.group(2))
                                             + [_SPELL[c] for c in m.group(3)]), text or "")
    t = _WRITTEN_FLIGHT.sub(lambda m: f"{ICAO}{m.group(1).lower()} {' '.join(m.group(2))}", t)
    return re.findall(r"[a-z]+|\d+", t.lower().replace("x-ray", "xray").replace("x ray", "xray"))


@dataclass(frozen=True)
class Mention:
    designators: tuple[str, ...]
    suffix: str
    start: int
    end: int
    abbrev: bool = False
    phrase_end: int = -1

    @property
    def text(self) -> str:
        if self.abbrev:
            return "…" + self.suffix
        return (self.designators[0] if self.designators else "N") + self.suffix


@dataclass(frozen=True)
class Attribution:
    hex: str | None
    callsign: str | None
    confidence: float          # 1.0 exact, 0.7 one off, 0.4 the only plausible one of that airline, 0.45-0.6 a tail piece
    spoken: str = ""


class TelephonyIndex:
    def __init__(self, rows: Iterable[tuple[str, str]]):
        self.phrases: dict[tuple[str, ...], list[str]] = {}
        self.telephony: dict[str, str] = {}
        last_word: dict[str, set[str]] = {}
        for code, tel in rows:
            ws = tuple(words_of(tel))
            if not ws or all(w in DIGITS or w.isdigit() for w in ws):
                continue
            if code not in self.phrases.get(ws, []):
                self.phrases.setdefault(ws, []).append(code)
            self.telephony.setdefault(code, "".join(ws))
            if len(ws) >= 2:
                last_word.setdefault(ws[-1], set()).add(code)
        for w, codes in last_word.items():
            if len(codes) <= 3 and w not in ALIAS_STOP and len(w) >= 5 and (w,) not in self.phrases:
                self.phrases[(w,)] = sorted(codes)
        self.max_len = max((len(p) for p in self.phrases), default=1)

    @classmethod
    def load(cls, airlines: str | Path | None = AIRLINES_FILE, operators: str | Path | None = OPERATORS_FILE) -> TelephonyIndex:
        rows: list[tuple[str, str]] = []
        for path, col in ((operators, "telephony"), (airlines, "telephony")):   # HPN's own operators first
            try:
                with open(path, newline="") as f:
                    rows += [(r["designator"], r[col]) for r in csv.DictReader(f) if r.get("designator") and r.get(col)]
            except (OSError, TypeError, KeyError):
                pass
        return cls(rows)


def _char(w: str) -> str:
    if w.isdigit() and len(w) <= 4:
        return w
    if w in DIGITS:
        return DIGITS[w]
    if w in LETTERS and w != "november":
        return LETTERS[w]
    return ""


def _suffix(words: Sequence[str], i: int, limit: int = 6) -> tuple[str, int]:
    out, j = "", i
    while j < len(words) and j - i < limit:
        w = words[j]
        c = _char(w)
        if not c and w in HOMOPHONES and j > i and j + 1 < len(words) and _char(words[j + 1]):
            c = HOMOPHONES[w]
        if not c and w in TEENS:
            c = TEENS[w]
        if not c and w in TENS:
            nxt = words[j + 1] if j + 1 < len(words) else ""
            if nxt in DIGITS and DIGITS[nxt] != "0":
                c, j = TENS[w] + DIGITS[nxt], j + 1
            else:
                c = TENS[w] + "0"
        if not c:
            break
        if c[0].isdigit() and out and out[-1].isalpha():
            break                                   # a tail's letters come last: "568LX 1 mile" is 568LX, then 1
        out += c
        j += 1
    return out, j


def find_mentions(text: str, index: TelephonyIndex) -> list[Mention]:
    words = words_of(text)
    found: list[Mention] = []
    i = 0
    while i < len(words):
        hit = None
        w = words[i]
        if w.startswith(ICAO) and len(w) == len(ICAO) + 3 and w[len(ICAO):].upper() in index.telephony:
            suf, j = _suffix(words, i + 1)            # a flight written by its designator ("MXY283")
            if suf and suf[0].isdigit():
                hit = Mention((w[len(ICAO):].upper(),), suf, i, j, phrase_end=i + 1)
        elif words[i] == "november" and not (i > 0 and words[i - 1] in LETTERS):
            suf, j = _suffix(words, i + 1)
            if len(suf) >= 2 and any(c.isdigit() for c in suf):
                hit = Mention((), suf, i, j)
        else:
            for n in range(min(index.max_len, len(words) - i), 0, -1):
                codes = index.phrases.get(tuple(words[i:i + n]))
                if codes:
                    suf, j = _suffix(words, i + n)
                    if suf and suf[0].isdigit():     # an airline flight number starts with a digit
                        hit = Mention(tuple(codes), suf, i, j, phrase_end=i + n)
                    break
        if hit:
            found.append(hit)
            i = hit.end
        else:
            i += 1
    covered = {k for m in found for k in range(m.start, m.end)}
    i = 0
    while i < len(words):
        if i in covered:
            i += 1
            continue
        suf, j = _suffix(words, i)
        typed = i > 0 and words[i - 1] in TYPE_WORDS
        tail_like = any(c.isalpha() for c in suf) or (typed and words[i - 1] != "november")
        if len(suf) >= 3 and any(c.isdigit() for c in suf) and tail_like and not any(k in covered for k in range(i, j)):
            found.append(Mention((), suf, i - 1 if typed else i, j, abbrev=True))
            i = j
        else:
            i += 1
    found.sort(key=lambda m: m.start)
    return found


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _p(a: dict) -> float:
    return a.get("p", 1.0)


AIRLINE = re.compile(r"[A-Z]{3}\d")


def _tail(a: dict) -> str:
    """The N-number an aircraft is called by: its callsign when that is one, else its registration, but never an
    airline flight's tail (JetBlue 1234 on N123JB is called "JetBlue 1234", not "November one two three ..."): a
    departure from shortfinal, whose one-off N-number match could land on an airliner's tail."""
    fl, reg = (a.get("flight") or "").upper(), (a.get("reg") or "").upper()
    if AIRLINE.match(fl):
        return ""
    for t in (fl, reg):
        if t.startswith("N") and len(t) > 2 and t[1].isdigit():
            return t
    return ""


def _match_tail(suffix: str, aircraft: Sequence[dict], digits_ok: bool = False) -> list[tuple[float, dict]]:
    tails = [suffix[i:] for i in range(len(suffix))
             if len(suffix) - i >= 3 and any(c.isalpha() for c in suffix[i:]) and any(c.isdigit() for c in suffix[i:])]
    scored = []
    for a in aircraft:
        if _p(a) < MIN_P:
            continue
        tail = _tail(a)
        if not tail:
            continue
        key, best = tail[1:], 0.0
        for piece in tails:
            if key.endswith(piece):
                best = max(best, 0.6)
            elif len(piece) >= 4 and key.startswith(piece):
                best = max(best, 0.5)
            elif len(piece) >= 4 and min(_edit_distance(piece, key[:len(piece)]), _edit_distance(piece, key[-len(piece):])) <= 1:
                best = max(best, 0.45)
        if digits_ok and suffix.isdigit() and len(suffix) >= 3 and key.endswith(suffix):
            best = max(best, 0.5)
        if best:
            scored.append((best, a))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    cands = [a for s, a in scored if s == top]
    if len(cands) == 1:
        return [(top, cands[0])]
    ranked = sorted(cands, key=lambda a: a.get("dist") if a.get("dist") is not None else 1e9)
    if ranked[0].get("dist") is None or ranked[0].get("dist") == ranked[1].get("dist"):
        return []
    return [(top, ranked[0])]


def _same_number_other_airline(m: Mention, words: Sequence[str], index: TelephonyIndex,
                               aircraft: Sequence[dict]) -> list[tuple[float, dict]]:
    if len(m.suffix) < 2 or m.phrase_end < 0:
        return []
    heard = ["".join(words[k:m.phrase_end]) for k in {m.start, max(0, m.start - 1)}]
    found = []
    for a in aircraft:
        fl = (a.get("flight") or "").upper()
        if len(fl) < 4 or not fl[:3].isalpha() or fl[:3] in m.designators or _p(a) < MIN_P or fl[3:] != m.suffix:
            continue
        tel = index.telephony.get(fl[:3])
        sim = max(SequenceMatcher(None, h, tel).ratio() for h in heard) if tel else 0.0
        if sim >= 0.6:
            found.append((sim, a))
    if found:
        return [(0.7, max(found, key=lambda f: (_p(f[1]), f[0]))[1])]
    return []


def attribute(text: str, index: TelephonyIndex, aircraft: Sequence[dict]) -> Attribution | None:
    """The best callsign in a transcript, matched to the aircraft list (dicts: hex, flight, reg, dist). An exactly heard
    callsign always wins; anything weaker must land on one aircraft. Heard with nothing to match: hex None, conf 0."""
    words = words_of(text)
    best: Attribution | None = None
    for m in find_mentions(text, index):
        if m.designators:
            cands = [a for a in aircraft if (a.get("flight") or "")[:3].upper() in m.designators]
            scored = []
            for a in cands:
                num = (a.get("flight") or "")[3:].upper()
                if num == m.suffix:
                    scored.append((1.0, a))
                elif _edit_distance(num, m.suffix) <= 1 and _p(a) >= MIN_P:
                    scored.append((0.7, a))
            plausible = [a for a in cands if _p(a) >= MIN_P]
            if not scored and len(plausible) == 1:
                scored.append((0.4, plausible[0]))
            if not cands:
                scored = _match_tail(m.suffix, aircraft, digits_ok=True)
            if max((s for s, _ in scored), default=0.0) < 0.7:
                scored = _same_number_other_airline(m, words, index, aircraft) or scored
        elif m.abbrev:
            scored = _match_tail(m.suffix, aircraft, digits_ok=True)
        else:
            scored = []
            for a in aircraft:
                reg = _tail(a)[1:]
                if not reg:
                    continue
                if reg == m.suffix:
                    scored.append((1.0, a))
                elif _p(a) < MIN_P:
                    continue
                elif reg and _edit_distance(reg, m.suffix) <= 1:
                    scored.append((0.7, a))
                elif len(m.suffix) >= 2 and reg.endswith(m.suffix[-3:]):
                    scored.append((0.5, a))
        if scored:
            conf, a = max(scored, key=lambda s: (s[0], _p(s[1])))
            att = Attribution(a.get("hex"), a.get("flight") or a.get("reg"), conf, m.text)
        else:
            att = Attribution(None, m.text, 0.0, m.text)
        if best is None or att.confidence > best.confidence:
            best = att
    return best


# -- location --------------------------------------------------------------------------------------------------------


def offset(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """The point dist_m from (lat, lon) on a true bearing (flat earth: fine for a few km)."""
    b = math.radians(bearing_deg)
    dlat = dist_m * math.cos(b) / 111_320.0
    dlon = dist_m * math.sin(b) / (111_320.0 * math.cos(math.radians(lat)))
    return round(lat + dlat, 5), round(lon + dlon, 5)


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def runway_heading(rw: str) -> float:
    """True heading of a takeoff or landing on runway rw (from its threshold toward the other end)."""
    a, b = RUNWAY_ENDS[rw], RUNWAY_ENDS[OPPOSITE[rw]]
    return bearing(a[0], a[1], b[0], b[1])


@dataclass
class Where:
    phrases: list = field(default_factory=list)   # the words, as heard
    runway: str = ""                             # the runway it is about
    departure: bool = False                      # ... off its departure end
    approach: bool = False                       # ... short of its approach end
    compass: tuple | None = None                 # (bearing, NM or None, "south of the field")
    dist_nm: float | None = None
    landmarks: list = field(default_factory=list)
    clock: str = ""
    side: str = ""


def where(norm: str, rules: Rules | None = None, departing: str = "") -> Where:
    """The location words in a (normalized) transmission. departing: the runway a departure was on (earlier words or
    ADS-B), for "off the end" said without a runway."""
    w = Where()
    rws = runways(norm)
    m = _END.search(norm)
    if m:
        which = m.group("which").strip()
        rw = runways("runway " + m.group("rw")) if m.group("rw") else []
        w.runway = (rw or rws or [departing] if (rw or rws or departing) else [""])[0]
        w.departure = which in ("departure", "far", "") and bool(w.runway)
        w.approach = which == "approach" and bool(w.runway)
        w.phrases.append(m.group(0).strip())
    elif rws:
        w.runway = rws[0]
    dep = departure_runway(norm)
    if dep and not w.runway:
        w.runway = dep
    c = _COMPASS.search(norm)
    if c:
        d = (c.group("d") or c.group("d2") or "").replace("  ", " ")
        q = _num(c.group("q") or c.group("q2"))
        key = d if d in _DIRS else d.replace(" ", "")
        if key in _DIRS:
            w.compass = (_DIRS[key], q, c.group(0).strip())
            w.phrases.append(c.group(0).strip())
    k = _CLOCK.search(norm)
    if k:
        w.clock = k.group(0).strip()
        w.phrases.append(w.clock)
    s = _SIDE.search(norm)
    if s:
        w.side = s.group("s") or s.group("s2") or ""
        w.phrases.append(s.group(0).strip())
    dm = _DIST.search(norm)
    if dm:
        w.dist_nm = _num(dm.group("q"))
    for lm in (rules.landmarks if rules else []):
        if re.search(rf"(?<![a-z0-9]){re.escape(lm)}(?![a-z0-9])", norm):
            w.landmarks.append(lm)
            w.phrases.append(lm)
    if w.runway and not any(f"runway" in p for p in w.phrases):
        w.phrases.append(f"runway {w.runway}")
    w.phrases = list(dict.fromkeys(w.phrases))
    return w


def place(w: Where) -> dict | None:
    """An approximate point from the words, or {placed: False, basis} when they give a place but not a point, or None.
    Past a runway's departure end along its heading (the distance said, else 1 NM), short of its approach end, or a
    compass direction with a distance from the field. Always approx, with a radius and the reason."""
    if w.runway and (w.departure or w.approach):
        d_nm = w.dist_nm if w.dist_nm and w.dist_nm <= 10 else 1.0
        hdg = runway_heading(w.runway)
        if w.departure:
            lat, lon = RUNWAY_ENDS[OPPOSITE[w.runway]]
            pt = offset(lat, lon, hdg, d_nm * NM_M)
            basis = f"about {d_nm:g} NM past the departure end of runway {w.runway}, on its heading"
        else:
            lat, lon = RUNWAY_ENDS[w.runway]
            pt = offset(lat, lon, (hdg + 180) % 360, d_nm * NM_M)
            basis = f"about {d_nm:g} NM short of the approach end of runway {w.runway}"
        radius = max(1000.0, d_nm * NM_M * 0.6)
        if not w.dist_nm:
            basis += " (no distance was said: 1 NM is a guess)"
        return {"lat": pt[0], "lon": pt[1], "approx": True, "radius_m": round(radius), "basis": basis, "runway": w.runway}
    if w.compass and w.compass[1]:
        brg, q, said = w.compass
        if q <= 10:
            pt = offset(ARP[0], ARP[1], brg, q * NM_M)
            return {"lat": pt[0], "lon": pt[1], "approx": True, "radius_m": round(max(800.0, q * NM_M * 0.5)),
                    "basis": f"“{said}”: {q:g} NM from the airport's reference point (miles taken as nautical)"}
    said = [p for p in w.phrases if not p.startswith("runway ")] or w.phrases
    if said:
        return {"placed": False, "approx": True, "basis": "; ".join(said)[:200] + " (not enough to place it on the map)",
                "runway": w.runway}
    return None


def where_text(w: Where) -> str:
    """For the title: ' off runway 16 departure', ' 2 miles south of the field', ' near Rye Lake', ''."""
    if w.runway and w.departure:
        return f" off runway {w.runway} departure"
    if w.runway and w.approach:
        return f" short of runway {w.runway}"
    if w.compass:
        return f" {w.compass[2]}"
    if w.landmarks:
        return f" near {w.landmarks[0].title()}"
    if w.runway:
        return f" near runway {w.runway}"
    return ""


# -- speech-to-text words ----------------------------------------------------------------------------------------------


def vocabulary(path: str | Path | None = None) -> dict:
    """data/atc/vocabulary.json: {phrases, prompt}."""
    try:
        d = json.loads(Path(path or VOCABULARY_FILE).read_text())
    except (OSError, ValueError):
        d = {}
    return {"phrases": [str(p) for p in d.get("phrases") or []], "prompt": str(d.get("prompt") or "")}


DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "niner"]
PHONETIC = {"A": "alfa", "B": "bravo", "C": "charlie", "D": "delta", "E": "echo", "F": "foxtrot", "G": "golf",
            "H": "hotel", "I": "india", "J": "juliett", "K": "kilo", "L": "lima", "M": "mike", "N": "november",
            "O": "oscar", "P": "papa", "Q": "quebec", "R": "romeo", "S": "sierra", "T": "tango", "U": "uniform",
            "V": "victor", "W": "whiskey", "X": "x-ray", "Y": "yankee", "Z": "zulu"}


def spell(s: str) -> str:
    return " ".join(DIGIT_WORDS[int(c)] if c.isdigit() else PHONETIC.get(c.upper(), c.lower()) for c in s)


_TEL: dict[str, str] | None = None


def telephony_words() -> dict[str, str]:
    """Designator -> its telephony as said ("JIA" -> "blue streak"): HPN's operators first, then FAA JO 7340.2."""
    global _TEL
    if _TEL is None:
        out: dict[str, str] = {}
        for path in (OPERATORS_FILE, AIRLINES_FILE):
            try:
                with open(path, newline="") as f:
                    for r in csv.DictReader(f):
                        if r.get("designator") and r.get("telephony"):
                            out.setdefault(r["designator"], " ".join(words_of(r["telephony"])))
            except OSError:
                pass
        _TEL = out
    return _TEL


def spoken_callsign(flight: str, telephony: dict[str, str]) -> str:
    """'JBU1234' -> 'jetblue one two three four'; 'N123AB' -> 'november one two three alfa bravo' (shortfinal)."""
    f = (flight or "").strip().upper()
    if len(f) > 3 and f[:3].isalpha() and f[3].isdigit() and telephony.get(f[:3]):
        return f"{telephony[f[:3]].lower()} {spell(f[3:])}"
    if f.startswith("N") and len(f) >= 3 and f[1].isdigit():
        return "november " + spell(f[1:])
    return ""


PROMPT_PHASES = {"arriving": 0, "departing": 1, "near": 2, "ground": 2, "low": 3}   # who talks to the tower first (#257)


def whisper_prompt(base: str, aircraft: Sequence[dict], telephony: dict[str, str]) -> str:
    """The vocabulary prompt, then the callsigns as said of the aircraft most likely on the frequency, as many as fit
    (shortfinal build_prompt): arriving, departing and on the ground at HPN before low traffic passing (#257:
    shortfinal's plausible.rank; not the nearest overall, so a helicopter low overhead doesn't push out the arrival
    on final), nearest first in each."""
    out = base
    for a in sorted(aircraft, key=lambda a: (PROMPT_PHASES.get(a.get("phase") or "", 4),
                                              a.get("dist") if a.get("dist") is not None else 1e9)):
        cs = spoken_callsign(a.get("flight") or a.get("reg") or "", telephony)
        if not cs:
            continue
        add = f" {cs},"
        if len(out) + len(add) > PROMPT_MAX:
            break
        out += add
    return out.rstrip(",")


# -- the watch -------------------------------------------------------------------------------------------------------


@dataclass
class Heard:
    id: str
    at: float
    channel: str
    text: str
    norm: str
    hits: list


FIRE = ("fire", "fire_off", "fire_acft", "fire_field")
# What a pre-alert is about when an exchange has several things in it: a MAYDAY / PAN-PAN / declared emergency first,
# then fire or a crash, then the rest; ARFF rolling is what follows from the others, so it names a pre-alert only alone.
PRIORITY = {"fire": 2, "accident": 2, "arff": 0}


def rank(h: Hit) -> int:
    if h.rule.category == "emergency":
        return 3 if h.rule.always and h.rule.weight >= 0.9 else 1
    return PRIORITY.get(h.rule.category, 1)


class AtcWatch:
    """Reads every airband transcript on the CAD of record; raises airport pre-alerts (the module doc)."""

    def __init__(self, alerts, channels: Iterable[str], rules: Rules | None = None, index: TelephonyIndex | None = None,
                 aircraft: Callable[[], list[dict]] = lambda: [], context: int = 4, context_s: float = 120.0,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.alerts, self.channels = alerts, set(channels)
        self.rules = rules or Rules.load()
        self.index = index or TelephonyIndex.load()
        self.aircraft, self.context, self.context_s, self.log, self.clock = aircraft, int(context), float(context_s), log, clock
        self._recent: dict[str, deque[Heard]] = {}
        self._lock = threading.Lock()
        self.seen = self.raised = 0

    def handles(self, channel: str) -> bool:
        return channel in self.channels

    def heard(self, t: dict) -> dict | None:
        """One transcript {id, source, start, end, text} on an airband channel. The pre-alert it raised or added to."""
        ch = str(t.get("source") or "")
        text = str(t.get("text") or "").strip()
        if ch not in self.channels or not text:
            return None
        at = float(t.get("end") or t.get("start") or self.clock())
        norm = normalize(text)
        hits = self.rules.hits(norm)
        with self._lock:
            self.seen += 1
            q = self._recent.setdefault(ch, deque(maxlen=max(1, self.context + 1)))
            q = deque((h for h in q if h.id != t.get("id")), maxlen=q.maxlen)    # new text for one already here
            self._recent[ch] = q
            ctx = [h for h in q if at - h.at <= self.context_s]
            me = Heard(str(t.get("id") or ""), at, ch, text, norm, hits)
            q.append(me)
        if not hits:
            return None
        conf = score([h.rule.weight for h in hits] + [h.rule.weight * self.rules.context_factor for c in ctx for h in c.hits])
        if conf < self.rules.threshold:
            return None
        alert = self.build(me, ctx, conf)
        try:
            row, new = self.alerts.raise_alert(alert)
        except Exception as e:  # noqa: BLE001 - a pre-alert must never stop the radio
            self.log(f"atc: {me.id}: {type(e).__name__}: {e}")
            return None
        if new:
            self.raised += 1
        return row

    # -- what the pre-alert says --------------------------------------------------------------------------------------

    def _top(self, hits: list) -> Hit:
        """The hit that names the pre-alert: the highest PRIORITY among the solid ones (weight 0.6 and up, else all),
        then the heaviest; the transmission's own before the earlier ones on a tie."""
        solid = [h for h in hits if h.rule.weight >= 0.6] or hits
        return max(solid, key=lambda h: (rank(h), h.rule.weight))

    def _category(self, hits: list, all_norm: str, w: Where) -> str:
        top = self._top(hits)
        if top.rule.category == "fire":
            ids = {h.rule.id for h in hits}
            if ids & {"smoke-aircraft", "engine-fire"} or self.rules.cue("aircraft", all_norm):
                return "fire_acft"
            if "brush-fire" in ids or self.rules.cue("off_airport", all_norm) or w.compass or w.landmarks or w.departure:
                return "fire_off"
            if "fire-on-field" in ids or self.rules.cue("field", all_norm):
                return "fire_field"
            return "fire"
        return top.rule.category

    def build(self, me: Heard, ctx: list[Heard], conf: float) -> AirportAlert:
        hits = list(me.hits)
        ctx_hits = [h for c in ctx for h in c.hits]
        allh = hits + ctx_hits
        all_norm = " . ".join([*(c.norm for c in ctx), me.norm])
        departing = next((departure_runway(c.norm) for c in reversed([*ctx, me]) if departure_runway(c.norm)), "")
        from sizeup.atc_scope import planes_for
        planes = planes_for(me.channel, self._planes())     # who can be on this frequency (#260: Ground, Approach)
        att = self._who(me, ctx, planes)
        if not departing and att and att.get("phase") == "departing" and att.get("rwy"):
            departing = str(att["rwy"])
        w = where(me.norm, self.rules, departing)
        if not w.phrases:                          # the place may have been said a moment before
            for c in reversed(ctx):
                w2 = where(c.norm, self.rules, departing)
                if w2.phrases:
                    w = w2
                    break
        if not w.runway and departing:
            w.runway = departing
            if self.rules.cue("off_airport", all_norm) and _END.search(all_norm):
                w.departure = True
        cat = self._category(allh, all_norm, w)
        top = self._top([h for h in allh if self._fits(h.rule.category, cat)] or allh)
        title = self._title(cat, top, w, att)
        info = self.rules.categories.get(cat) or {}
        event = {"type": top.rule.event_type or info.get("event_type") or "AIRPORT", "subtype": info.get("event_subtype") or ""}
        if top.rule.event_type:
            event["subtype"] = ""
        loc = place(w) if cat in FIRE or w.runway else None
        snippets = [self._snip(c, True) for c in ctx if c.hits or c is (ctx[-1] if ctx else None)] + [self._snip(me, False)]
        cs = (att or {}).get("callsign") or ""
        subject = ("fire" + (f":{w.runway}" if w.runway else "")) if cat in FIRE else (f"acft:{cs}" if cs else cat)
        detail = " · ".join(x for x in (f"{info.get('label', cat)}", f"heard: {', '.join(dict.fromkeys(h.text for h in allh))}",
                                         f"{me.channel}") if x)
        return AirportAlert(source="atc", kind=cat, subject=subject, title=title, detail=detail, confidence=round(conf, 2),
                            at=me.at, channel=me.channel, group=me.channel, snippets=snippets,
                            aircraft=[att] if att else [], location=loc, phrases=w.phrases, event=event, rank=rank(top))

    @staticmethod
    def _fits(rule_cat: str, cat: str) -> bool:
        return rule_cat == cat or (rule_cat == "fire" and cat in FIRE)

    @staticmethod
    def _snip(h: Heard, context: bool) -> dict:
        return {"id": h.id, "at": h.at, "channel": h.channel, "text": h.text[:400],
                "hits": [x.text for x in h.hits], "context": context}

    def _title(self, cat: str, top: Hit, w: Where, att: dict | None) -> str:
        thing = top.rule.thing
        who = (att or {}).get("callsign") or ""
        rw = f" runway {w.runway}" if w.runway else ""
        if cat == "fire_off":
            t = f"possible {thing} reported{where_text(w) or ' off the airport'}"
        elif cat == "fire_acft":
            t = f"{thing} reported" + (f" by {who}" if who else " on an aircraft") + (f",{rw}" if rw else "")
        elif cat == "fire_field":
            t = f"possible {thing} on the field" + (f" near{rw}" if rw and "runway" not in thing else "")
        elif cat == "fire":
            t = f"possible {thing} reported{where_text(w)}"
        elif cat == "emergency":
            t = f"{thing}" + (f": {who}" if who else "") + (f",{rw}" if rw else "")
        else:
            label = (self.rules.categories.get(cat) or {}).get("label", cat)
            t = f"{label} ({thing})" + (f": {who}" if who else "") + (f",{rw}" if rw else "")
        return f"HPN ATC: {t}"

    def _planes(self) -> list[dict]:
        try:
            return [a for a in (self.aircraft() or []) if isinstance(a, dict)]
        except Exception as e:  # noqa: BLE001
            self.log(f"atc: aircraft: {type(e).__name__}: {e}")
            return []

    def _who(self, me: Heard, ctx: list[Heard], planes: list[dict]) -> dict | None:
        """The aircraft: the callsign in this transmission, else in the ones before it (newest first)."""
        heard_only = None
        for h in [me, *reversed(ctx)]:
            att = attribute(h.text, self.index, planes)
            if att is None:
                continue
            if att.hex is not None and att.confidence >= 0.4:
                a = next((p for p in planes if p.get("hex") == att.hex), {})
                return {"callsign": att.callsign, "hex": att.hex, "spoken": att.spoken, "match": att.confidence,
                        **{k: a.get(k) for k in ("type", "model", "reg", "lat", "lon", "alt", "gs", "trk", "phase", "rwy",
                                                 "dist", "airline", "pos_at") if a.get(k) is not None}}
            if heard_only is None and att.spoken and not att.spoken.startswith("…"):
                heard_only = {"callsign": att.spoken, "hex": None, "spoken": att.spoken, "match": 0.0}
        return heard_only

    def state(self) -> dict:
        return {"channels": sorted(self.channels), "seen": self.seen, "raised": self.raised,
                "rules": len(self.rules.rules), "version": self.rules.version}
