"""HPN ATC transcripts in their written form (GitHub #257): what speech-to-text wrote, mapped to how ATC writes it.

Amazon Transcribe (and the station's whisper) hear HPN Tower well enough to get most words, but write HPN's own words
the way a general English model would: "R nav yankee three four", "RNA 3-4 Yankee", "R9834" for the RNAV (GPS) Y
approach to runway 34; "Twarf" for the fix TWORF; "Chester Tower" for a clipped "Westchester Tower"; "Lima X-ray" for
the letters of a tail; "clear to landing". normalize() maps those to one written form, so the Radio rows read like a
controller's strip and the talker matcher (sizeup/atc_talkers.py, #252) and the pre-alert rules (sizeup/atc.py) see
the same words every time. The transcript itself is never changed: the raw text stays what the engine wrote (the
comms log's text), and the written form is derived from it whenever it is shown or matched (so a better normaliser
improves old rows too). From shortfinal's tidy() / fix_facilities() (its core/stt.py), with HPN's approach data:

    facility    "Chester Tower", "Westchester Towers", "White Plains tower", "Lufthansa tower" (the ATC model's
                European airports): "Westchester Tower" (and Ground). A word is replaced only right before "tower" /
                "ground", only when it sounds like Westchester or is a known mishearing (data/atc/normalize.json).
    digits      spoken digits to numerals in runs ("three four" -> "34", "niner" -> 9); "3-4" -> "34".
    phrases     "clear to land(ing)", "climb to land" -> "cleared to land"; "clear(ed) for take off" -> "cleared for
                takeoff"; "line up in wait" -> "line up and wait".
    approaches  HPN's instrument approaches (FAA CIFP in data/airport/khpn_procedures.geojson): ILS 16/34, LOC 16/34,
                RNAV (GPS) Y 16/34, RNAV (RNP) Z 16/34. "R nav", "RNA", "RNF", "Rav", "arnav", "rn of" + Yankee/Zulu +
                runway, in either order -> "RNAV Yankee 34"; "GPS 34" -> "RNAV Yankee 34" (the only GPS approach to
                it); "R9834" (one token) -> "RNAV Yankee 34" (the Y approach is the one flown day to day); "localizer
                34" -> "LOC 34". A letter never heard is never added except in those two cases.
    fixes       the approach fixes by ident (TWORF, TAYSH, HAARP ...): every spelling in normalize.json's "fixes"
                anywhere; "fixes_in_context" (ordinary words: "to do" for TOEDO, "harp") and a close spelling
                (SequenceMatcher >= FIX_SIMILAR) only right after over / at / from / direct / via / crossing ...
    numbers     a runway, heading or squawk run into the digits after it, split where it ends ("runway 347 kilo
                zulu" -> "runway 34, 7 kilo zulu": a runway is two digits, one of the airport's runways).
    tails       two or more phonetic letters run together ("Lima X-ray" -> "LX"), joined to the digits before them
                when they can end a US tail (two letters at most: "568 Lima X-ray" -> "568LX"); "November 1 2 3 Alfa
                Bravo" -> "N123AB". Not after "taxiway", "via", "runway", "squawk" ... (a route or a number, not a
                tail). sizeup/atc.py's words_of() reads "568LX" back as five characters, so the matcher sees the same
                tail.
    telephony   misheard airline / operator telephony ("Moxie" -> "Moxy", "jet speed" -> "Jet Speed"); a number said in
                groups right after a telephony or a type word, joined ("JetBlue 12 34" -> "JetBlue 1234").

Nothing here guesses a callsign, a runway or an altitude that wasn't said: only the spelling changes.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "atc" / "normalize.json"
PROCEDURES_FILE = ROOT / "data" / "airport" / "khpn_procedures.geojson"
RUNWAYS = ("16", "34", "11", "29")
APPROACH_RUNWAYS = ("16", "34")           # HPN's instrument approaches (CIFP): runways 16 and 34 only
FIX_SIMILAR = 0.8                         # a word this close to a fix's ident, in a fix's context, is the fix

DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "tree": "3", "four": "4", "fower": "4",
               "five": "5", "fife": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9"}
LETTERS = {"alfa": "A", "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E", "foxtrot": "F",
           "golf": "G", "hotel": "H", "india": "I", "juliet": "J", "juliett": "J", "kilo": "K", "lima": "L", "mike": "M",
           "november": "N", "oscar": "O", "papa": "P", "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T",
           "uniform": "U", "victor": "V", "whiskey": "W", "whisky": "W", "x-ray": "X", "xray": "X", "yankee": "Y",
           "zulu": "Z"}
# A letter run is a taxiway or a route after these, not a tail ("taxi via Alfa Bravo")
# ... and the digits of an ATC number are never a tail's: "squawk 4521 kilo zulu" stays as said
NOT_TAIL_AFTER = {"taxiway", "taxiways", "via", "taxi", "on", "cross", "crossing", "and", "ramp", "spot", "information",
                  "runway", "rwy", "heading", "squawk", "altimeter", "wind", "maintain", "point", "decimal"}
NUMBER_WIDTH = {"heading": 3, "squawk": 4}    # an ATC number this many digits long: "heading 3407" is 340, then 7
FIX_CONTEXT = {"over", "at", "from", "direct", "to", "via", "crossing", "cross", "past", "inbound", "outside", "outbound",
               "until", "before", "after", "reaching", "reach", "abeam", "fix", "of", "passing", "hold", "holding"}
_W = r"[A-Za-z][A-Za-z'-]*"


@lru_cache(maxsize=1)
def _data() -> dict:
    try:
        d = json.loads(DATA_FILE.read_text())
    except (OSError, ValueError):
        d = {}
    idents: set[str] = set()
    try:
        for f in json.loads(PROCEDURES_FILE.read_text()).get("features", []):
            p = f.get("properties") or {}
            if p.get("kind") == "approach_fix" and p.get("ident"):
                idents.add(str(p["ident"]).upper())
    except (OSError, ValueError, AttributeError):
        pass
    fixes = {str(k).upper(): [str(x).lower() for x in v] for k, v in (d.get("fixes") or {}).items()}
    ctx = {str(k).upper(): [str(x).lower() for x in v] for k, v in (d.get("fixes_in_context") or {}).items()}
    idents |= set(fixes) | set(ctx)
    # 3-letter idents are VORs (CMK Carmel, IGN Kingston, SAX Sparta): said by name, never as a word
    idents = {i for i in idents if len(i) == 5}
    return {"idents": sorted(idents), "fixes": fixes, "fixes_in_context": ctx,
            "telephony": {str(k).lower(): str(v) for k, v in (d.get("telephony") or {}).items()},
            "facility": [str(x).lower() for x in d.get("facility_misheard") or []]}


def fixes() -> list[str]:
    """HPN's approach fixes (5-letter idents) the normaliser knows."""
    return list(_data()["idents"])


def _alts(words: list[str]) -> str:
    """Longest first, as a regex alternation of phrases whose words may be split by spaces or hyphens."""
    ws = sorted(set(words), key=len, reverse=True)
    return "|".join(r"[\s-]+".join(re.escape(p) for p in w.split()) for w in ws)


# -- the steps --------------------------------------------------------------------------------------------------------


def _hyphen_digits(t: str) -> str:
    """'3-4' -> '34', '1-6' -> '16' (Transcribe writes a spoken digit pair with a hyphen)."""
    return re.sub(r"\b(\d)(?:-(\d))+\b", lambda m: m.group(0).replace("-", ""), t)


_DIGIT_RUN = re.compile(r"\b(?:" + "|".join(sorted(DIGIT_WORDS, key=len, reverse=True)) + r"|\d+)"
                        r"(?:[\s-]+(?:" + "|".join(sorted(DIGIT_WORDS, key=len, reverse=True)) + r"|\d+)\b)+", re.I)


def _digits(t: str) -> str:
    """Runs of two or more spoken digits (or digits and numerals) to numerals: 'three four' -> '34', 'one six
    niner' -> '169'. A lone 'one' / 'oh' stays a word ('one mile', 'oh'); a lone 'niner' becomes 9."""
    def run(m: re.Match) -> str:
        parts = re.split(r"[\s-]+", m.group(0))
        lead = []
        while parts and parts[0].lower() == "oh":   # "oh" leading a run is the word, not zero
            lead.append(parts.pop(0))
        if len(parts) < 2 or all(p.isdigit() for p in parts):
            return m.group(0)            # numerals ("34 3") are left as written
        return " ".join(lead + ["".join(DIGIT_WORDS.get(p.lower(), p) for p in parts)])
    t = _DIGIT_RUN.sub(run, t)
    return re.sub(r"\bniner\b", "9", t, flags=re.I)


_PHRASES = [
    (re.compile(r"\b(?:clear(?:ed)?|clearly|climb|cleaned) to (?:land(?:ing)?|lane)\b", re.I), "cleared to land"),
    (re.compile(r"\bclear(?:ed)? for (?:the )?take[\s-]?off\b", re.I), "cleared for takeoff"),
    (re.compile(r"\bclear for the\b", re.I), "cleared for the"),
    (re.compile(r"\bline up (?:in|an|and) (?:wait|weight|weigh)\b", re.I), "line up and wait"),
    # "34, clear line, Flexjet 538" (shortfinal's captures and HPN Tower live): there is no "clear line" in ATC
    (re.compile(r"\bclear(?:ed)? (?:to )?(?:line|lane)\b(?! up)", re.I), "cleared to land"),
    (re.compile(r"\bgoing around\b", re.I), "going around"),
]


def _phrases(t: str) -> str:
    for rx, to in _PHRASES:
        t = rx.sub(to, t)
    return t


def _facility(t: str) -> str:
    """'Chester Tower', 'Westchester Towers', 'White Plains tower' -> 'Westchester Tower' (Ground too)."""
    heard = _data()["facility"] + ["westchester", "west chester"]
    rx = re.compile(rf"\b(?:an? )?(?P<w>{_alts(heard)}|{_W})[\s-]+(?P<f>towers?|grounds?)\b", re.I)

    def one(m: re.Match) -> str:
        w = m.group("w").lower().replace("-", " ")
        fac = "Tower" if m.group("f").lower().startswith("tower") else "Ground"
        known = w in heard or w.replace(" ", "") in heard
        if not known and SequenceMatcher(None, w.replace(" ", ""), "westchester").ratio() < 0.7:
            return m.group(0)
        lead = m.group(0)[: m.start("w") - m.start(0)]
        # "a White Plains tower" at the start: the "a" is the clipped greeting, not an article
        lead = "" if lead.strip().lower() in ("a", "an") and m.start(0) == 0 else lead
        return f"{lead}Westchester {fac}"
    return rx.sub(one, t)


_RNAV = r"(?:r[\s-]?nav|rnav|arnav|are[\s-]?nav|our[\s-]?nav|r[\s-]?n[\s-]?a[\s-]?v|rna|rnf|rnv|rav|rn[\s-]of|r[\s-]and[\s-]of)"
_GPS = r"(?:\(?\s*(?:gps|g\s?p\s?s|rnp)\s*\)?[\s-]+)?"
_LETTER = r"(?P<l>yankee|zulu|y|z)"
_RW = r"(?:runway[\s-]+)?(?P<rw>16|34)\b"
_APPROACH = [
    # "R nav (GPS) Yankee runway 34", "RNA Yankee 34"
    re.compile(rf"\b{_RNAV}[\s-]+{_GPS}(?:{_LETTER}[\s-]+)?{_RW}", re.I),
    # "RNA 3-4 Yankee" (the runway first)
    re.compile(rf"\b{_RNAV}[\s-]+{_GPS}(?:runway[\s-]+)?(?P<rw>16|34)[\s-]+{_LETTER}\b", re.I),
]
_R_I_VY = re.compile(r"\bR[\s-]+I[\s-]+V(?:y|[\s-]+y)\b", re.I)                # "R I Vy runway 34": RNAV Yankee
_R_TOKEN = re.compile(r"\bR(?:9|nav)?\d?(?P<rw>16|34)\b", re.I)                   # "R9834", "R934", "Rnav34"
_GPS_ONLY = re.compile(r"\b(?:(?:the[\s-]+)?gps[\s-]+(?:or[\s-]+)?)+(?:approach[\s-]+)?(?:runway[\s-]+)?(?P<rw>16|34)\b", re.I)
_ILS = re.compile(r"\b(?P<k>ils|i\.l\.s\.?|localizer|localiser|loc)[\s-]+(?:approach[\s-]+)?(?:runway[\s-]+)?(?P<rw>16|34)\b", re.I)


def _approaches(t: str) -> str:
    def rnav(m: re.Match) -> str:
        letter = (m.groupdict().get("l") or "").lower()
        name = {"y": "Yankee", "yankee": "Yankee", "z": "Zulu", "zulu": "Zulu"}.get(letter, "")
        return f"RNAV {name + ' ' if name else ''}{m.group('rw')}"
    t = _R_I_VY.sub("RNAV Yankee", t)
    t = re.sub(r"\bRNAV Yankee (?:runway[\s-]+)?(16|34)\b", r"RNAV Yankee \1", t, flags=re.I)
    for rx in _APPROACH:
        t = rx.sub(rnav, t)
    t = _R_TOKEN.sub(lambda m: f"RNAV Yankee {m.group('rw')}", t)
    t = _GPS_ONLY.sub(lambda m: ("the " if m.group(0).lower().startswith("the") else "") + f"RNAV Yankee {m.group('rw')}", t)
    t = _ILS.sub(lambda m: f"{'LOC' if m.group('k').lower().startswith('loc') else 'ILS'} {m.group('rw')}", t)
    return t


def _fixes(t: str) -> str:
    d = _data()
    for ident, heard in d["fixes"].items():
        t = re.sub(rf"\b(?:{_alts(heard + [ident.lower()])})\b", ident, t, flags=re.I)
    ctx = r"(?P<c>\b(?:" + "|".join(sorted(FIX_CONTEXT, key=len, reverse=True)) + r")[\s-]+(?:the[\s-]+)?)"
    for ident, heard in d["fixes_in_context"].items():
        t = re.sub(rf"{ctx}(?:{_alts(heard)})\b", lambda m, i=ident: m.group("c") + i, t, flags=re.I)
    idents = d["idents"]

    def close(m: re.Match) -> str:
        w = m.group("w")
        if w.upper() in idents or len(w) < 4 or w.lower() in LETTERS or w.lower() in DIGIT_WORDS:
            return m.group(0)
        best = max(idents, key=lambda i: SequenceMatcher(None, w.lower(), i.lower()).ratio(), default="")
        if best and SequenceMatcher(None, w.lower(), best.lower()).ratio() >= FIX_SIMILAR:
            return m.group("c") + best
        return m.group(0)
    return re.sub(rf"{ctx}(?P<w>[A-Za-z]{{4,7}})\b", close, t, flags=re.I)


_MERGED = re.compile(r"\b(?P<w>runway|rwy|heading|squawk)(?P<sp>\s+)(?P<num>\d{3,6})\b", re.I)


def _merged(t: str) -> str:
    """An ATC number speech-to-text ran into the digits after it, split where the number ends: "runway 347 kilo
    zulu" -> "runway 34, 7 kilo zulu" (the airport's runways: sizeup/atc.py runway_designators), "heading 3407" ->
    "heading 340, 7". Only where it can't be one number: a runway is two digits, a heading three, a squawk four."""
    from sizeup.atc import split_runway

    def one(m: re.Match) -> str:
        w, num = m.group("w"), m.group("num")
        width = NUMBER_WIDTH.get(w.lower())
        if width is None:
            sp = split_runway(num)
            if not sp:
                return m.group(0)
            head, tail = sp
        elif len(num) > width:
            head, tail = num[:width], num[width:]
        else:
            return m.group(0)
        return f"{w}{m.group('sp')}{head}, {tail}"
    return _MERGED.sub(one, t)


_LETTER_WORD = r"(?:" + "|".join(sorted((k for k in LETTERS if k != "november"), key=len, reverse=True)) + r")"
_TAIL = re.compile(rf"(?P<num>\b\d{{1,5}}[\s-]+)?(?P<run>\b{_LETTER_WORD}(?:[\s-]+{_LETTER_WORD})+)\b", re.I)
_NOVEMBER = re.compile(r"\bnovember[\s-]+(?P<num>\d{1,5})(?P<l>[A-Z]{0,2})\b", re.I)


def _tails(t: str) -> str:
    """Two or more phonetic letters in a row -> the letters, joined to the digits just before ('568 Lima X-ray' ->
    '568LX'); 'November 123AB' -> 'N123AB'."""
    def run(m: re.Match) -> str:
        before = re.findall(r"[A-Za-z]+", t[max(0, m.start() - 20):m.start()])
        if before and before[-1].lower() in NOT_TAIL_AFTER:
            return m.group(0)
        letters = "".join(LETTERS[w.lower()] for w in re.findall(_LETTER_WORD, m.group("run"), re.I))
        if m.group("num") and len(letters) > 2:
            return m.group(0)            # a US tail ends in at most two letters: "503 romeo papa romeo" isn't one
        return (m.group("num") or "").strip(" -") + letters
    t = re.sub(r"\bx[\s-]?ray\b", "X-ray", t, flags=re.I)
    t = _TAIL.sub(run, t)
    return _NOVEMBER.sub(lambda m: f"N{m.group('num')}{m.group('l').upper()}" if len(m.group("num") + m.group("l")) >= 3
                         else m.group(0), t)


def _telephony(t: str) -> str:
    # "Executive 505": EJA's telephony is EXECJET, and "executive" before a flight number is nothing else on HPN Tower
    t = re.sub(r"\bexecutive(?=[\s-]+\d)", "Execjet", t, flags=re.I)
    tel = _data()["telephony"]
    if not tel:
        return t
    return re.sub(rf"\b(?:{_alts(list(tel))})\b", lambda m: tel.get(re.sub(r"[\s-]+", " ", m.group(0).lower()),
                  tel.get(m.group(0).lower(), m.group(0))), t, flags=re.I)


@lru_cache(maxsize=1)
def _callers() -> str:
    """A regex of the words a callsign's number follows: HPN's telephony (data/atc: khpn_operators.csv,
    telephony_extra.csv, normalize.json) and aircraft types ("Cessna", "Citation")."""
    import csv
    names = set(_data()["telephony"].values())
    for f in ("khpn_operators.csv", "telephony_extra.csv"):
        try:
            with open(ROOT / "data" / "atc" / f, newline="") as fh:
                names |= {r["telephony"].strip() for r in csv.DictReader(fh) if r.get("telephony")}
        except (OSError, KeyError):
            pass
    from sizeup.atc import TYPE_WORDS
    names |= TYPE_WORDS - {"november"}
    return _alts([n.lower() for n in names if n])


def _groups(t: str) -> str:
    """A callsign's number said in groups and written apart ("JetBlue 12 34", "Cessna 4 5 6PR"), joined after a
    telephony or a type word: up to 4 digits for a flight number, 5 for a tail."""
    def one(m: re.Match) -> str:
        parts = m.group("num").split()
        digits = "".join(re.sub(r"\D", "", p) for p in parts)
        if len(digits) > 5 or any(re.search(r"[A-Z]", p) for p in parts[:-1]):
            return m.group(0)
        return f"{m.group('who')} {''.join(parts)}"
    return re.sub(rf"\b(?P<who>{_callers()})\s+(?P<num>\d{{1,3}}(?:\s+\d{{1,3}})*\s+\d{{1,3}}[A-Z]{{0,2}})\b",
                  one, t, flags=re.I)


@lru_cache(maxsize=4096)
def normalize(text: str) -> str:
    """The written form of an HPN ATC transcript (the module doc). '' stays ''; idempotent. Remembered for the last 4096
    texts: the Radio view asks for the same rows on every refresh, ~1 ms each (#284)."""
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    t = _hyphen_digits(t)
    t = _digits(t)
    t = _phrases(t)
    t = _facility(t)
    t = _approaches(t)
    t = _fixes(t)
    t = _merged(t)
    t = _tails(t)
    t = _telephony(t)
    t = _groups(t)
    t = re.sub(r"\b(Westchester (?:Tower|Ground)) (?=\1\b)", "", t)     # "Westchester Tower Westchester Tower"
    return t[:1].upper() + t[1:]
