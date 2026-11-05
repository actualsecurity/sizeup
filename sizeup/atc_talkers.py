"""Who is talking on HPN Tower (GitHub #252): the callsign heard, matched to ADS-B, tower or aircraft, shown like unit IDs.

User (#252): "ATC is AM voice, with no MDC/FleetSync-style IDs. Identify the talker anyway, honestly labelled".

Speech-derived, never a radio ID: the registry's MDC / FleetSync IDs (sizeup/cad/radios.py) are not touched, and nothing
here is written onto radio_traffic's talker / rig / radio_id. Each airband transcript (sizeup/atc.py's channels) gets one
record in atc_talkers, read back onto its Radio row as "atc" (with_atc), only while Settings > System > ATC talkers >
"Identify ATC talkers" is on (on by default; off: the raw transmissions, as before #252).

    the callsign    sizeup/atc.py's find_mentions (shortfinal core/callsign.py): airline telephony from HPN's operators
                    (data/atc/khpn_operators.csv), the extra spoken forms in data/atc/telephony_extra.csv and FAA JO 7340.2
                    (data/airport/airlines.csv): "JetBlue one two three four" / "jetblue twelve thirty four" -> JBU1234;
                    N-numbers in phonetics ("November four one Delta Whiskey" -> N41DW); a shortened tail after first
                    contact ("one delta whiskey" -> ...1DW). No callsign heard: no record, nothing labelled.
    the facility    "Westchester Tower" / "Ground" / "Clearance" as the speaker's own name (right after the callsign:
                    the controller) or as the one called (before it: the pilot); "contact ground" is an instruction.
    who is talking  shortfinal's speaker rule (its _speaker, extended): a controller starts with the callsign and gives an
                    instruction ("JetBlue 1234, runway three four, cleared to land": the tower, to JBU1234); a pilot calls
                    the facility, reports ("... with you", "ready for departure"), or reads back and ends with the callsign
                    ("cleared to land three four, JetBlue 1234": JBU1234 speaking). Still undecided: the exchange (the
                    transmission before, about the same aircraft, within REPLY_S: the other side answers), then the carrier
                    (the tower's transmitter is close and steady: a carrier within the tower's own spread of what the
                    channel's tower transmissions measured is the tower's; far under it, an aircraft). Else unknown.
    the match       the aircraft the Aircraft layer had near HPN (sizeup/aircraft.py: within AREA_NM) at any time within
                    WINDOW_S of the transmission (the layer's pictures are kept, Sightings, and every heard / probable one is
                    matched again as newer pictures come in, until WINDOW_S after it):
                      confirmed   ADS-B has that exact callsign (flight) or N-number: one aircraft
                      probable    one digit off (or a shortened tail, or the same flight number on a similar-sounding
                                  airline), ONE such aircraft near HPN, and its phase fits the words ("cleared to land" ->
                                  arriving, "taxi" -> on the ground ...)
                      heard       anything else: as heard, not on ADS-B (two candidates: heard, never picked)
                    A spoken N-number is never matched to an airline flight's own tail (sizeup/atc.py _tail).
    the exchange    consecutive transmissions on the channel about the same aircraft, each within EXCHANGE_GAP_S of the one
                    before (one transmission with no callsign between two of them joins, unlabelled): "JBU1234 landing 34".
    the map         the Aircraft layer's answer carries "atc" while Settings' "Show ATC talker on the map" is on: who talked
                    in the last TALK_HOLD_S (the "talking" ring) and each aircraft's recent transmissions (its info box).

The pre-alerts (sizeup/atc.py AtcWatch, sizeup/cad/airport_alerts.py) are untouched: their aircraft is their own matcher's.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable, Iterable, Sequence

from sizeup import atc

WINDOW_S = 120.0          # an aircraft active within this of the transmission may be the one
AREA_NM = 15.0            # ... and near HPN (sizeup/aircraft.py RADIUS_NM)
REMATCH_S = WINDOW_S + 30.0   # heard / probable ones are matched again this long (newer ADS-B pictures)
REMATCH_EVERY_S = 5.0
SIGHTINGS_KEEP_S = 10 * 60.0
EXCHANGE_GAP_S = 45.0     # a transmission this soon after the last one about the same aircraft: the same exchange
REPLY_S = 30.0            # the exchange's turn-taking is used this soon after the transmission before
RECENT_KEEP_S = 15 * 60.0  # transmissions kept in memory per channel (the exchanges, the rematch)
TALK_HOLD_S = 10.0        # the map's "talking" ring after a transmission is in
MAP_TX_S = 30 * 60.0      # an aircraft's info box: its transmissions this far back
MAP_TX_MAX = 8
LEVEL_MIN = 5             # tower transmissions measured before the carrier is used at all
KEEP_S = 90 * 24 * 3600.0
VERSION = 1

CHIP = {"tower": "TWR", "ground": "GND", "clearance": "CLR"}
FACILITY_WORDS = {"tower": "tower", "ground": "ground", "clearance": "clearance", "delivery": "clearance"}
FACILITY_NAMES = ("westchester", "white plains", "hpn", "county")
INSTRUCTION = ("contact", "monitor", "switch", "over", "with", "call", "advise", "freq", "frequency", "to")
NOT_FACILITY_BEFORE = {"the", "on", "of", "a", "this", "that", "my", "our", "your"}
NOT_FACILITY_AFTER = {"speed", "stop", "level", "floor", "crew", "power", "fire", "track", "light", "lights"}
GREETINGS = ("hello", "good morning", "good afternoon", "good evening", "good day", "with you", "checking in", "check in",
             "howdy")
# shortfinal's PILOT_CUES: reports / requests after the callsign ("... with you at three thousand", "ready for departure")
PILOT_CUES = ("inbound", "with you", "checking in", "request", "requesting", "ready", "unable", "climbing", "descending",
              "passing", "leveling", "level at", "holding short", "looking", "with information", "have information",
              "have the numbers", "with the numbers", "wilco", "my", "we have", "we are", "were", "we re")
TRAILING_OK = {"thanks", "thank", "you", "bye", "good", "day", "sir", "heavy", "super", "ma", "am", "cheers", "so", "long",
               "see", "ya"}

# What the words say the aircraft is doing (a probable match must fit it) and the exchange's name.
PHASE_CUES = [
    (re.compile(r"\bcleared to land\b|\bgo around\b|\bnumber (?:one|two|three|four|1|2|3|4)\b|\b(?:on |short )?final\b"
                r"|\b(?:left|right) (?:base|downwind)\b|\bstraight in\b|\binbound\b|\bvisual approach\b|\bils\b|\brnav\b"), "arriving"),
    (re.compile(r"\bcleared for (?:take ?off|the option)\b|\bline up and wait\b|\bready for departure\b|\bcontact departure\b"
                r"|\bfly runway heading\b|\bready to go\b"), "departing"),
    (re.compile(r"\btaxi\b|\bhold short\b|\bramp\b|\bgate\b|\bparking\b|\bfbo\b|\bpush ?back\b|\bcross runway\b|\bexit\b|\bvacate\b"),
     "ground"),
]
FITS = {"arriving": {"arriving", "low"}, "departing": {"departing", "ground", "low"}, "ground": {"ground", "arriving"}}
ACTIVITY = [
    (re.compile(r"\bcleared to land\b"), "landing"), (re.compile(r"\bgo around\b"), "go-around"),
    (re.compile(r"\bcleared for take ?off\b"), "takeoff"), (re.compile(r"\bline up and wait\b"), "line up"),
    (re.compile(r"\bcontact departure\b"), "to departure"), (re.compile(r"\btaxi\b|\bhold short\b"), "taxi"),
    (re.compile(r"\b(?:on |short )?final\b|\bbase\b|\bdownwind\b|\binbound\b|\bapproach\b"), "inbound"),
    (re.compile(r"\bready for departure\b|\bready to go\b"), "ready"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS atc_talkers (
  traffic_id TEXT PRIMARY KEY,          -- radio_traffic.id (an airband transmission)
  channel TEXT NOT NULL,
  at REAL NOT NULL,                     -- the transmission's start
  hex TEXT NOT NULL DEFAULT '',         -- the aircraft it is about, when matched (confirmed / probable)
  exchange TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL,                   -- the record (JSON: talker, addressed, aircraft, exchange, phrases, why)
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS atc_talkers_at ON atc_talkers(at);
CREATE INDEX IF NOT EXISTS atc_talkers_hex ON atc_talkers(hex, at)
"""


# -- the words --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Facility:
    kind: str            # tower | ground | clearance
    start: int
    end: int
    instruction: bool    # "contact ground": named as where to go, not as who talks


def facilities(words: Sequence[str]) -> list[Facility]:
    """The facility names said ("Westchester Tower", "ground", "clearance delivery"), in order."""
    out = []
    i = 0
    while i < len(words):
        w = words[i]
        kind = FACILITY_WORDS.get(w)
        if kind is None:
            i += 1
            continue
        start, named = i, False
        if i >= 1 and words[i - 1] in FACILITY_NAMES:
            start, named = i - 1, True
        elif i >= 2 and " ".join(words[i - 2:i]) in FACILITY_NAMES:
            start, named = i - 2, True
        end = i + 1
        if kind == "clearance" and end < len(words) and words[end] == "delivery":
            end += 1
        prev = words[start - 1] if start >= 1 else ""
        nxt = words[end] if end < len(words) else ""
        if not named and (prev in NOT_FACILITY_BEFORE or nxt in NOT_FACILITY_AFTER):
            i = end
            continue
        if kind == "clearance" and not named and start > 0:     # "copy the clearance", "your clearance": not a name
            i = end
            continue
        before = words[max(0, start - 2):start]
        out.append(Facility(kind, start, end, any(b in INSTRUCTION for b in before)))
        i = end
    return out


def phase_cue(norm: str) -> str:
    """arriving | departing | ground | '': what the words say the aircraft is doing (the first that fits, in order)."""
    for rx, cue in PHASE_CUES:
        if rx.search(norm):
            return cue
    return ""


def activity(norm: str) -> str:
    for rx, what in ACTIVITY:
        if rx.search(norm):
            return what
    return ""


def phrases(norm: str) -> list[str]:
    """The ATC phrases in the words (shown on the chip's title: why it was read the way it was)."""
    out = []
    for rx, _ in PHASE_CUES + ACTIVITY:
        m = rx.search(norm)
        if m and m.group(0) not in out:
            out.append(m.group(0))
    return out[:6]


STOP = "xstopx"            # a comma or a full stop, kept as a word: a flight number never runs on across it
_PUNCT = re.compile(r"[,.;:?!]+")


def clauses(text: str) -> str:
    """The transcript with its punctuation as STOP words ("JetBlue 1234, ten miles south" is JBU1234, not JBU123410)."""
    return _PUNCT.sub(f" {STOP} ", text or "")


def _plain(words: Sequence[str]) -> list[str]:
    return [w for w in words if w != STOP]


def _has(phrases_: Iterable[str], words: Sequence[str]) -> bool:
    s = f" {' '.join(words)} "
    return any(f" {p} " in s for p in phrases_)


def speaker(words: Sequence[str], spans: Sequence, facs: Sequence[Facility]) -> tuple[str, str, Facility | None]:
    """(atc | aircraft | unknown, why, the facility named) from where the callsign sits (shortfinal's _speaker): every
    mention of the same aircraft in spans (atc.Mention: start, end in words)."""
    first = min(spans, key=lambda m: m.start)
    last = max(spans, key=lambda m: m.end)
    named = [f for f in facs if not f.instruction]
    after = next((f for f in named if 0 <= f.start - first.end <= 1 or (f.start - first.end == 2 and words[first.end] == STOP)),
                 None)
    if after is not None and len(_plain(words[: first.start])) <= 3:
        return "atc", f"self-ID: {' '.join(words[after.start:after.end])}", after
    head = _plain(words[: first.start])
    before = next((f for f in named if f.end <= first.start), None)
    if before is not None:
        return "aircraft", f"calls {' '.join(words[before.start:before.end])}", before
    if _has(GREETINGS, head):
        return "aircraft", "greets the facility", None
    tail = _plain(words[last.end:])
    if len(tail) <= 1 or all(w in TRAILING_OK for w in tail):
        return "aircraft", "ends with the callsign (a readback)", None
    if _has(PILOT_CUES, words[first.end:]):
        return "aircraft", "a pilot's report or request", None
    if len(_plain(words[: first.start])) <= 1:
        return "atc", "starts with the callsign (an instruction)", None
    return "unknown", "", None


# -- ADS-B: the pictures kept, the match ------------------------------------------------------------------------------


class Sightings:
    """The Aircraft layer's pictures (sizeup/aircraft.py view), by ICAO address: first and last seen near HPN, the latest
    copy. around(at) = everything near HPN within WINDOW_S of a moment."""

    def __init__(self, keep_s: float = SIGHTINGS_KEEP_S, area_nm: float = AREA_NM):
        self.keep_s, self.area_nm = keep_s, area_nm
        self.seen: dict[str, dict] = {}
        self._lock = threading.Lock()

    def add(self, planes: Iterable[dict], now: float) -> None:
        with self._lock:
            for a in planes:
                if not isinstance(a, dict) or not a.get("hex"):
                    continue
                d = a.get("dist")
                if d is not None and d > self.area_nm:
                    continue
                cur = self.seen.get(a["hex"])
                first = cur["first"] if cur else now
                self.seen[a["hex"]] = {"first": min(first, now), "last": max(now, cur["last"] if cur else now),
                                       "a": {k: v for k, v in a.items() if k != "trail"}}
            for h in [h for h, s in self.seen.items() if now - s["last"] > self.keep_s]:
                del self.seen[h]

    def around(self, at: float, window_s: float = WINDOW_S) -> list[dict]:
        with self._lock:
            return [dict(s["a"]) for s in self.seen.values() if s["first"] - window_s <= at <= s["last"] + window_s]


@dataclass
class Match:
    status: str                    # confirmed | probable | heard
    confidence: float
    plane: dict | None = None
    why: str = ""


def _fits(plane: dict, cue: str) -> bool:
    ph = plane.get("phase") or ""
    if not cue:
        return ph in ("arriving", "departing", "ground", "low") or not ph
    return ph in FITS.get(cue, set()) or not ph


def _distinct(planes: Iterable[dict]) -> list[dict]:
    out, seen = [], set()
    for a in planes:
        if a.get("hex") not in seen:
            seen.add(a.get("hex"))
            out.append(a)
    return out


def match(m, words: Sequence[str], index: atc.TelephonyIndex, planes: Sequence[dict], cue: str = "") -> Match:
    """One mention (atc.Mention) against the aircraft around: confirmed (exact, one), probable (one near match whose phase
    fits), else heard. Never picks among several."""
    planes = [a for a in planes if atc._p(a) >= atc.MIN_P]
    if m.designators and not m.abbrev:
        want = {d + m.suffix for d in m.designators}
        exact = _distinct(a for a in planes if (a.get("flight") or "").strip().upper() in want)
    elif not m.abbrev:
        exact = _distinct(a for a in planes if atc._tail(a) == "N" + m.suffix)
    else:
        exact = []
    if len(exact) == 1:
        return Match("confirmed", 1.0, exact[0], "on ADS-B: the same callsign")
    if len(exact) > 1:
        return Match("heard", 0.0, None, f"{len(exact)} aircraft on ADS-B with that callsign: not picked")
    near: list[dict] = []
    if m.abbrev:
        for a in planes:
            tail = atc._tail(a)
            if tail and len(m.suffix) >= 3 and tail[1:].endswith(m.suffix):
                near.append(a)
        conf, how = 0.6, "the end of its tail"
    elif m.designators:
        for a in planes:
            fl = (a.get("flight") or "").strip().upper()
            if fl[:3] in m.designators and len(fl) > 3 and atc._edit_distance(fl[3:], m.suffix) == 1:
                near.append(a)
        if m.phrase_end >= 0 and len(m.suffix) >= 2:          # the number clear, the airline misheard (shortfinal)
            heard = ["".join(words[k:m.phrase_end]) for k in {m.start, max(0, m.start - 1)}]
            for a in planes:
                fl = (a.get("flight") or "").strip().upper()
                tel = index.telephony.get(fl[:3])
                if len(fl) > 3 and fl[:3].isalpha() and fl[:3] not in m.designators and fl[3:] == m.suffix and tel \
                        and max(SequenceMatcher(None, h, tel).ratio() for h in heard) >= 0.6:
                    near.append(a)
        conf, how = 0.7, "one character off"
    else:
        for a in planes:
            tail = atc._tail(a)
            if tail and atc._edit_distance(tail[1:], m.suffix) == 1:
                near.append(a)
        conf, how = 0.7, "one character off"
    near = _distinct(near)
    if not near:
        return Match("heard", 0.0, None, "not on ADS-B near HPN")
    if len(near) > 1:
        return Match("heard", 0.0, None, f"{len(near)} near matches on ADS-B: not picked")
    a = near[0]
    if not _fits(a, cue):
        return Match("heard", 0.0, None, f"one near match ({a.get('flight') or a.get('reg')}), but it is "
                                         f"{a.get('phase') or 'elsewhere'}, not {cue or 'near HPN'}")
    return Match("probable", conf, a, f"{how}, the only one near HPN" + (f", {a.get('phase')}" if a.get("phase") else ""))


def _rank(mt: Match) -> int:
    return {"confirmed": 2, "probable": 1}.get(mt.status, 0)


def _same_aircraft(a, b) -> bool:
    return (a.plane is not None and b.plane is not None and a.plane.get("hex") == b.plane.get("hex"))


# -- one transmission -------------------------------------------------------------------------------------------------


@dataclass
class Tx:
    id: str
    channel: str
    at: float
    end: float
    text: str
    carrier_db: float | None = None
    record: dict | None = None
    written: float = 0.0          # when its record was last written (the map's ring)
    words: list = field(default_factory=list)


def _aircraft_rec(mt: Match, m, role: str) -> dict:
    a = mt.plane or {}
    cs = (a.get("flight") or a.get("reg") or "").strip().upper() if mt.plane else m.text
    out = {"kind": "aircraft", "callsign": cs, "spoken": m.text, "status": mt.status, "confidence": round(mt.confidence, 2),
           "role": role, "why": mt.why}
    if mt.plane:
        out.update({k: a[k] for k in ("hex", "type", "reg", "phase", "rwy") if a.get(k)})
    return out


def _facility_rec(kind: str, status: str, why: str) -> dict:
    return {"kind": kind, "callsign": CHIP.get(kind, kind.upper()), "status": status, "why": why}


def channel_facility(channel: str) -> str:
    c = (channel or "").lower()
    return "ground" if "ground" in c or "gnd" in c else "clearance" if "clearance" in c or "delivery" in c else "tower"


def level_says(levels: Sequence[float], carrier: float | None) -> str:
    """tower | aircraft | '': the carrier against the tower's own (the channel's transmissions the words said were the
    tower's). The tower is close and steady: within its own spread it is the tower's; far under it, an aircraft's."""
    if carrier is None or len(levels) < LEVEL_MIN:
        return ""
    med = statistics.median(levels)
    mad = statistics.median(abs(x - med) for x in levels)
    spread = max(1.0, 1.4826 * mad)
    if abs(carrier - med) <= 2 * spread:
        return "tower"
    if carrier < med - max(6.0, 3 * spread):
        return "aircraft"
    return ""


def analyze(tx: Tx, index: atc.TelephonyIndex, planes: Sequence[dict], before: Sequence[dict] = (),
            levels: Sequence[float] = ()) -> dict | None:
    """The record for one transmission, or None when no callsign was heard (nothing labelled). before: the records of
    the transmissions just before on the channel (newest last), for the exchange's turn-taking."""
    text = clauses(tx.text)
    words = atc.words_of(text)
    tx.words = words
    mentions = atc.find_mentions(text, index)
    if not mentions:
        return None
    norm = atc.normalize(tx.text)
    cue = phase_cue(norm)
    scored = [(m, match(m, words, index, planes, cue)) for m in mentions]
    best_m, best = max(scored, key=lambda s: (_rank(s[1]), -s[0].start))
    spans = [m for m, mt in scored if m is best_m or _same_aircraft(mt, best)
             or (best.plane is None and m.text == best_m.text)]
    facs = facilities(words)
    role, why, fac = speaker(words, spans, facs)
    how = "words"
    if role == "unknown":
        prev = before[-1] if before else None
        if prev and prev.get("aircraft") and tx.at - float(prev.get("end") or prev.get("at") or 0) <= REPLY_S \
                and _same_subject(prev["aircraft"], _aircraft_rec(best, best_m, "")):
            pr = "atc" if prev["talker"]["kind"] != "aircraft" else "aircraft"
            if prev["talker"]["kind"] != "unknown":
                role = "aircraft" if pr == "atc" else "atc"
                why, how = f"answers the {'tower' if pr == 'atc' else 'aircraft'} (the exchange)", "exchange"
        if role == "unknown":
            says = level_says(levels, tx.carrier_db)
            if says:
                role = "atc" if says == "tower" else "aircraft"
                why, how = f"carrier {tx.carrier_db:.0f} dB: {'steady like the tower' if says == 'tower' else 'well under the tower'}", "carrier"
    ac = _aircraft_rec(best, best_m, {"atc": "addressed", "aircraft": "speaking"}.get(role, "mentioned"))
    if role == "atc":
        kind = fac.kind if fac else channel_facility(tx.channel)
        talker = _facility_rec(kind, "heard" if fac else "probable", why)
        addressed = ac
    elif role == "aircraft":
        talker = dict(ac, why=f"{ac['why']}; {why}" if ac.get("why") else why)
        addressed = _facility_rec(fac.kind, "heard", why) if fac else None
    else:
        talker = {"kind": "unknown", "callsign": "", "status": "", "why": "a callsign heard; who is speaking isn't clear"}
        addressed = None
    return {"v": VERSION, "id": tx.id, "channel": tx.channel, "at": tx.at, "end": tx.end, "talker": talker,
            "addressed": addressed, "aircraft": ac, "role": role, "how": how, "cue": cue, "activity": activity(norm),
            "runway": (atc.runways(norm) or [""])[0], "phrases": phrases(norm),
            "carrier_db": None if tx.carrier_db is None else round(float(tx.carrier_db), 1),
            "others": [m.text for m in mentions if m is not best_m and m.text != best_m.text][:3],
            "note": "speech-derived (the words heard), not a radio ID"}


# -- exchanges --------------------------------------------------------------------------------------------------------


def _key(ac: dict | None) -> tuple[str, str]:
    if not ac:
        return "", ""
    return (ac.get("hex") or "", (ac.get("callsign") or "").upper())


def _same_subject(a: dict | None, b: dict | None) -> bool:
    (ha, ca), (hb, cb) = _key(a), _key(b)
    if not (ca or ha) or not (cb or hb):
        return False
    if ha and hb:
        return ha == hb
    if ca == cb:
        return True
    for x, y in ((ca, cb), (cb, ca)):         # "...1DW" (shortened) is N41DW's
        if x.startswith("…") and len(x) >= 4 and y.endswith(x[1:]):
            return True
    sa, sb = (a or {}).get("spoken", "").upper(), (b or {}).get("spoken", "").upper()
    return bool(sa and sb and sa == sb)


def exchanges(items: Sequence[dict], gap_s: float = EXCHANGE_GAP_S) -> dict[str, str]:
    """{transmission id: exchange id}: consecutive transmissions on a channel about the same aircraft (record "aircraft"),
    each within gap_s of the one before; one with no callsign between two of them joins (unlabelled). items: {id,
    channel, at, aircraft (or None)}. An exchange's id is its first transmission's."""
    out: dict[str, str] = {}
    by: dict[str, list[dict]] = {}
    for it in sorted(items, key=lambda x: (x["at"], x["id"])):
        by.setdefault(it.get("channel") or "", []).append(it)
    for lst in by.values():
        cur: dict | None = None             # {xid, subject, last}
        between: list[dict] = []
        for it in lst:
            ac = it.get("aircraft")
            if not ac:
                if cur is not None and it["at"] - cur["last"] <= gap_s:
                    between.append(it)
                else:
                    cur, between = None, []
                continue
            if cur is not None and len(between) <= 1 and it["at"] - cur["last"] <= gap_s and _same_subject(cur["subject"], ac):
                for b in between:
                    out[b["id"]] = cur["xid"]
                out[it["id"]] = cur["xid"]
                cur["last"] = it["at"]
                if _rank_rec(ac) >= _rank_rec(cur["subject"]):
                    cur["subject"] = ac
            else:
                cur = {"xid": it["id"], "subject": ac, "last": it["at"]}
                out[it["id"]] = it["id"]
            between = []
    return out


def _rank_rec(ac: dict | None) -> int:
    return {"confirmed": 2, "probable": 1}.get((ac or {}).get("status", ""), 0)


# -- the service ------------------------------------------------------------------------------------------------------


class AtcTalkers:
    """The CAD's ATC talker records (the module doc): the app hands it every airband transcript (heard) and the Aircraft
    layer's pictures (observe); the Radio rows (decorate) and the aircraft map (map_payload) read them back."""

    def __init__(self, cad, channels: Iterable[str], index: atc.TelephonyIndex | None = None,
                 aircraft: Callable[[], list[dict]] = lambda: [], log: Callable[[str], None] = print,
                 clock: Callable[[], float] | None = None):
        self.cad, self.store = cad, cad.store
        self.channels = set(channels)
        self._index = index
        self.aircraft, self.log = aircraft, log
        self.clock = clock or getattr(cad, "clock", None) or time.time
        self.sightings = Sightings()
        self.recent: dict[str, deque[Tx]] = {}
        self.levels: dict[str, deque[float]] = {}
        self._lock = threading.RLock()
        self._rematched = 0.0
        self.seen = self.labelled = 0
        with self.store.lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.store.db.execute(stmt)
            self.store.db.execute("DELETE FROM atc_talkers WHERE at < ?", (self.clock() - KEEP_S,))

    # -- the switches (Settings > System > ATC talkers; sizeup/cad/settings.py) --------------------------------------

    @property
    def identify(self) -> bool:
        return bool(getattr(self.cad, "atc_identify", True))

    @property
    def on_map(self) -> bool:
        return self.identify and bool(getattr(self.cad, "atc_map", True))

    @property
    def index(self) -> atc.TelephonyIndex:
        if self._index is None:
            self._index = load_index()
        return self._index

    def handles(self, channel: str) -> bool:
        return channel in self.channels

    # -- in -----------------------------------------------------------------------------------------------------------

    def observe(self, planes: Iterable[dict], now: float | None = None) -> None:
        """A new picture from the Aircraft layer: kept; the recent heard / probable transmissions matched again."""
        now = self.clock() if now is None else now
        self.sightings.add(list(planes or []), now)
        if self.channels and self.identify and now - self._rematched >= REMATCH_EVERY_S:
            self._rematched = now
            try:
                self.rematch(now)
            except Exception as e:  # noqa: BLE001 - the aircraft layer goes on
                self.log(f"atc talkers: rematch: {type(e).__name__}: {e}")

    def heard(self, t: dict) -> dict | None:
        """One airband transcript {id, source, start, end, text, carrier_db?} (new, or new text): its record, or None
        (no callsign heard, not an airband channel, or Identify is off)."""
        ch = str(t.get("source") or "")
        if ch not in self.channels or not self.identify:
            return None
        text = str(t.get("text") or "").strip()
        tid = str(t.get("id") or "")
        if not tid:
            return None
        now = self.clock()
        try:
            cur = self.aircraft() or []
        except Exception:  # noqa: BLE001
            cur = []
        if cur:
            self.sightings.add(cur, now)
        start = float(t.get("start") or now)
        cdb = t.get("carrier_db")
        tx = Tx(tid, ch, start, float(t.get("end") or start), text, float(cdb) if isinstance(cdb, (int, float)) else None)
        with self._lock:
            self.seen += 1
            q = self.recent.setdefault(ch, deque())
            old = next((x for x in q if x.id == tid), None)
            if old is not None:
                q.remove(old)
            q.append(tx)
            ordered = sorted(q, key=lambda x: x.at)
            self.recent[ch] = deque(x for x in ordered if now - x.at <= RECENT_KEEP_S or x is tx)
            self._analyze(tx, ch)
            changed = self._regroup(ch)
        stale = tx.record is None and (old is not None and old.record is not None or self.store.one(
            "SELECT 1 AS x FROM atc_talkers WHERE traffic_id = ?", (tid,)) is not None)
        self._write(changed, delete=[tid] if stale else [])
        if tx.record:
            self.labelled += 1
        return tx.record

    def _before(self, tx: Tx, ch: str) -> list[dict]:
        return [x.record for x in self.recent.get(ch, ()) if x.at < tx.at and x.record and x.record.get("talker")][-3:]

    def _analyze(self, tx: Tx, ch: str) -> None:
        planes = self.sightings.around(tx.at)
        rec = analyze(tx, self.index, planes, self._before(tx, ch), list(self.levels.get(ch, ())))
        if rec is not None and rec["talker"]["kind"] in CHIP and rec["how"] == "words" and tx.carrier_db is not None:
            self.levels.setdefault(ch, deque(maxlen=50)).append(float(tx.carrier_db))   # the tower's own carrier
        tx.record = rec

    def _regroup(self, ch: str) -> list[Tx]:
        """The exchanges on a channel again; the transmissions whose record changed (to be written)."""
        q = list(self.recent.get(ch, ()))
        ids = exchanges([{"id": x.id, "channel": ch, "at": x.at, "aircraft": (x.record or {}).get("aircraft")} for x in q])
        groups: dict[str, list[Tx]] = {}
        for x in q:
            if x.id in ids:
                groups.setdefault(ids[x.id], []).append(x)
        changed = []
        for x in q:
            xid = ids.get(x.id)
            g = groups.get(xid, []) if xid else []
            if xid and len(g) >= 1:
                subj = max((y.record["aircraft"] for y in g if y.record), key=_rank_rec, default=None)
                act = next((y.record["activity"] for y in reversed(g) if y.record and y.record.get("activity")), "")
                rwy = next((y.record["runway"] for y in reversed(g) if y.record and y.record.get("runway")), "")
                ex = {"id": xid, "subject": (subj or {}).get("callsign", ""), "status": (subj or {}).get("status", ""),
                      "hex": (subj or {}).get("hex", ""), "activity": act, "runway": rwy, "n": len(g)}
            else:
                ex = None
            if x.record is None and ex is not None:
                x.record = {"v": VERSION, "id": x.id, "channel": ch, "at": x.at, "end": x.end, "talker": None,
                            "addressed": None, "aircraft": None, "role": "", "how": "", "exchange": ex,
                            "note": "no callsign heard: not labelled; between two transmissions of the exchange"}
                changed.append(x)
                continue
            if x.record is not None and x.record.get("talker") is None and ex is None:
                x.record = None                    # a bystander no longer between two of an exchange
                changed.append(x)
                continue
            if x.record is not None and x.record.get("exchange") != ex:
                x.record["exchange"] = ex
                changed.append(x)
        return changed

    def rematch(self, now: float | None = None) -> int:
        """The transmissions of the last REMATCH_S not confirmed yet, against the pictures kept since: how many changed."""
        now = self.clock() if now is None else now
        n = 0
        for ch in list(self.recent):
            with self._lock:
                todo = [x for x in self.recent[ch] if now - x.at <= REMATCH_S and x.record and x.record.get("aircraft")
                        and x.record["aircraft"].get("status") != "confirmed"]
                changed = []
                for x in todo:
                    before = json.dumps(x.record, sort_keys=True)
                    ex = x.record.get("exchange")
                    self._analyze(x, ch)
                    if x.record is not None and ex is not None:
                        x.record["exchange"] = ex
                    if json.dumps(x.record, sort_keys=True) != before:
                        changed.append(x)
                if changed:
                    changed = list({id(x): x for x in changed + self._regroup(ch)}.values())
            if changed:
                self._write(changed)
                n += len(changed)
        return n

    def _write(self, txs: Sequence[Tx], delete: Sequence[str] = ()) -> None:
        if not txs and not delete:
            return
        now = self.clock()
        with self.store.tx() as db:
            for tid in delete:
                db.execute("DELETE FROM atc_talkers WHERE traffic_id = ?", (tid,))
            for x in txs:
                if x.record is None:
                    db.execute("DELETE FROM atc_talkers WHERE traffic_id = ?", (x.id,))
                    continue
                ac = x.record.get("aircraft") or {}
                x.written = now
                db.execute("INSERT INTO atc_talkers (traffic_id, channel, at, hex, exchange, data, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(traffic_id) DO UPDATE SET channel = excluded.channel, "
                           "at = excluded.at, hex = excluded.hex, exchange = excluded.exchange, data = excluded.data, "
                           "updated_at = excluded.updated_at",
                           (x.id, x.channel, x.at, ac.get("hex", "") if ac.get("status") in ("confirmed", "probable") else "",
                            (x.record.get("exchange") or {}).get("id", ""), json.dumps(x.record, separators=(",", ":")), now))

    # -- out ----------------------------------------------------------------------------------------------------------

    def decorate(self, rows: list[dict]) -> list[dict]:
        """Each airband row with its record as "atc" (in place), while Identify is on; nothing otherwise."""
        if not self.identify or not self.channels:
            return rows
        ids = [r["id"] for r in rows if r.get("channel") in self.channels and r.get("kind", "radio") == "radio" and r.get("id")]
        got: dict[str, dict] = {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for r in self.store.q(f"SELECT traffic_id, data FROM atc_talkers WHERE traffic_id IN ({','.join('?' * len(part))})", part):
                try:
                    got[r["traffic_id"]] = json.loads(r["data"])
                except (TypeError, ValueError):
                    pass
        for r in rows:
            if r.get("id") in got:
                r["atc"] = got[r["id"]]
        return rows

    def map_payload(self, now: float | None = None) -> dict | None:
        """For the Aircraft layer's answer (sizeup/aircraft.py extra), while "Show ATC talker on the map" is on:
        {talking: [{hex, tx, role, until}], tx: {hex: [{id, at, end, text, role, status, talker}]}}."""
        if not self.on_map or not self.channels:
            return None
        now = self.clock() if now is None else now
        talking = []
        with self._lock:
            for q in self.recent.values():
                for x in q:
                    ac = (x.record or {}).get("aircraft") or {}
                    if ac.get("hex") and ac.get("status") in ("confirmed", "probable") and x.written \
                            and now - x.written <= TALK_HOLD_S and now - x.end <= TALK_HOLD_S + 60:
                        talking.append({"hex": ac["hex"], "tx": x.id, "role": ac.get("role", ""), "status": ac["status"],
                                        "until": round(x.written + TALK_HOLD_S, 1)})
        tx: dict[str, list] = {}
        for r in self.store.q("SELECT a.traffic_id, a.hex, a.data, t.text FROM atc_talkers a LEFT JOIN radio_traffic t "
                              "ON t.id = a.traffic_id WHERE a.hex != '' AND a.at >= ? ORDER BY a.at DESC LIMIT 400",
                              (now - MAP_TX_S,)):
            lst = tx.setdefault(r["hex"], [])
            if len(lst) >= MAP_TX_MAX:
                continue
            try:
                d = json.loads(r["data"])
            except (TypeError, ValueError):
                continue
            ac = d.get("aircraft") or {}
            lst.append({"id": r["traffic_id"], "at": d.get("at"), "end": d.get("end"), "role": ac.get("role", ""),
                        "status": ac.get("status", ""), "talker": (d.get("talker") or {}).get("callsign", ""),
                        "text": (r.get("text") or "")[:200]})
        return {"talking": talking, "tx": tx, "hold_s": TALK_HOLD_S}

    def state(self) -> dict:
        return {"channels": sorted(self.channels), "identify": self.identify, "map": self.on_map,
                "seen": self.seen, "labelled": self.labelled, "aircraft_known": len(self.sightings.seen)}


_INDEX: atc.TelephonyIndex | None = None
EXTRA_FILE = atc.ROOT / "data" / "atc" / "telephony_extra.csv"


def load_index() -> atc.TelephonyIndex:
    """HPN's operators, then the extra spoken forms (data/atc/telephony_extra.csv: "Republic" for RPA ...), then FAA
    JO 7340.2: one index, built once."""
    global _INDEX
    if _INDEX is None:
        import csv
        rows: list[tuple[str, str]] = []
        for path in (atc.OPERATORS_FILE, EXTRA_FILE, atc.AIRLINES_FILE):
            try:
                with open(path, newline="") as f:
                    rows += [(r["designator"], r["telephony"]) for r in csv.DictReader(f)
                             if r.get("designator") and r.get("telephony")]
            except (OSError, KeyError):
                pass
        _INDEX = atc.TelephonyIndex(rows)
    return _INDEX
