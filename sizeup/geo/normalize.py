"""Text normalisation shared by the gazetteer builder and the geocoder.

Speech-to-text output of a dispatch page ("respond to one two three Westchester
Avenue cross of Pearl Street") and the canonical street names in the address
data ("Westchester Ave") are both reduced to the same token form so they can be
compared: lower case, no punctuation, spoken numbers as digits, street types
abbreviated (``avenue`` -> ``ave``).
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- numbers

UNITS = {
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
    "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
    "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20,
    "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "sixtieth": 60, "seventieth": 70,
    "eightieth": 80, "ninetieth": 90,
}
MULTIPLIERS = {"hundred": 100, "thousand": 1000}
# "oh"/"o" are zero only inside a number ("five oh nine"), never on their own.
_ZERO_ONLY_INSIDE = {"oh", "o"}
NUMBER_WORDS = set(UNITS) | set(TEENS) | set(TENS) | set(MULTIPLIERS)


def ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _parse_run(words: list[str]) -> str | None:
    """Turn a run of spoken number words into a digit string (or an ordinal like 21st).

    Without hundred/thousand the chunks are concatenated the way people read
    house numbers: "one two three" -> 123, "one twenty three" -> 123,
    "fourteen thirty two" -> 1432, "five oh nine" -> 509, "twenty two" -> 22.
    With a multiplier it is an ordinary cardinal: "two hundred nine" -> 209.
    """
    ordinal = None
    if words and words[-1] in ORDINALS:
        ordinal = ORDINALS[words[-1]]
        words = words[:-1]
    if any(w in MULTIPLIERS for w in words):
        total = current = 0
        for w in words:
            if w in UNITS:
                current += UNITS[w]
            elif w in TEENS:
                current += TEENS[w]
            elif w in TENS:
                current += TENS[w]
            elif w == "hundred":
                current = (current or 1) * 100
            elif w == "thousand":
                total += (current or 1) * 1000
                current = 0
        value = total + current
        if ordinal is not None:
            return ordinal_suffix(value + ordinal)
        return str(value)
    chunks: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in TENS:
            nxt = words[i + 1] if i + 1 < len(words) else None
            if nxt in UNITS and UNITS[nxt] > 0 and nxt not in _ZERO_ONLY_INSIDE:
                chunks.append(str(TENS[w] + UNITS[nxt]))
                i += 2
                continue
            if nxt is None and ordinal is not None and ordinal < 10:
                # "twenty first" -> 21st
                return ordinal_suffix(TENS[w] + ordinal)
            chunks.append(str(TENS[w]))
        elif w in TEENS:
            chunks.append(str(TEENS[w]))
        elif w in UNITS:
            chunks.append(str(UNITS[w]))
        i += 1
    if ordinal is not None:
        if not chunks:
            return ordinal_suffix(ordinal)
        # "one twenty first" is not a thing; keep the ordinal on its own
        return "".join(chunks) + " " + ordinal_suffix(ordinal)
    return "".join(chunks) or None


# --------------------------------------------------------------------------- streets

# spoken / written variant -> canonical abbreviation (USPS style, as in the data)
STREET_TYPES = {
    "street": "st", "st": "st", "str": "st", "streets": "st",
    "avenue": "ave", "ave": "ave", "av": "ave", "avenues": "ave", "avenida": "ave",
    "road": "rd", "rd": "rd", "roads": "rd",
    "drive": "dr", "dr": "dr",
    "lane": "ln", "ln": "ln",
    "place": "pl", "pl": "pl",
    "court": "ct", "ct": "ct",
    "circle": "cir", "cir": "cir",
    "boulevard": "blvd", "blvd": "blvd", "boulevards": "blvd",
    "terrace": "ter", "ter": "ter", "terr": "ter",
    "parkway": "pkwy", "pkwy": "pkwy", "pky": "pkwy",
    "square": "sq", "sq": "sq",
    "crescent": "cres", "cres": "cres",
    "trail": "trl", "trl": "trl",
    "plaza": "plz", "plz": "plz",
    "landing": "lndg", "lndg": "lndg",
    "bluff": "blf", "blf": "blf",
    "close": "clos", "clos": "clos",
    "green": "grn", "grn": "grn",
    "highway": "hwy", "hwy": "hwy",
    "expressway": "expy", "expy": "expy",
    "turnpike": "tpke", "tpke": "tpke",
    "extension": "ext", "ext": "ext", "exd": "ext",
    "route": "rte", "rte": "rte", "rt": "rte",
    "way": "way", "mews": "mews", "run": "run",
    "saint": "st", "mount": "mt", "mt": "mt", "fort": "ft",
    "interstate": "i",
    "theatre": "theater", "centre": "center",
}
# tokens that end a street name (the "post type")
TYPE_ABBRS = {
    "st", "ave", "rd", "dr", "ln", "pl", "ct", "cir", "blvd", "ter", "pkwy", "sq", "cres",
    "trl", "plz", "lndg", "blf", "clos", "grn", "hwy", "expy", "tpke", "way", "mews", "run",
}
# abbreviation -> spoken word, for street-type words used inside a name ("Terrace Ave")
EXPAND = {
    "st": "street", "ave": "avenue", "rd": "road", "dr": "drive", "ln": "lane", "pl": "place",
    "ct": "court", "cir": "circle", "blvd": "boulevard", "ter": "terrace", "pkwy": "parkway",
    "sq": "square", "cres": "crescent", "trl": "trail", "plz": "plaza", "lndg": "landing",
    "blf": "bluff", "clos": "close", "grn": "green", "mt": "mount", "ft": "fort",
}
DIRECTIONS = {"n": "north", "s": "south", "e": "east", "w": "west"}
DIRECTION_WORDS = {v: k for k, v in DIRECTIONS.items()}
BOUND_WORDS = {"northbound", "southbound", "eastbound", "westbound"}

# Different names in the sources for the same road -> one key.
KEY_SYNONYMS = {
    "hutchinson river pkwy": "hutchinson pkwy",
    "new england thruway": "i 95",
    "cross westchester expy": "i 287",
}
# Extra spoken aliases: alias phrase (normalised) -> street key.
EXTRA_ALIASES = {
    "hutch": "hutchinson pkwy",
    "hutchinson river pkwy": "hutchinson pkwy",
    "hutchinson river": "hutchinson pkwy",
    "new england thruway": "i 95",
    "thruway": "i 95",
    "cross westchester expy": "i 287",
    "cross westchester": "i 287",
    "post rd": "boston post rd",
    "theodore fremd": "theodore fremd ave",
}

_PUNCT_SPLIT = re.compile(r"[.,;:!?()\[\]{}\"]+")
_DIGIT_HYPHEN = re.compile(r"^\d+(?:-\d+)+$")
BOUNDARY = "|"


def _raw_tokens(text: str) -> list[str]:
    text = text.lower()
    text = text.replace("&", " and ").replace("@", " at ").replace("#", " number ")
    text = re.sub(r"(\d),(\d{3})\b", r"\1\2", text)          # 1,234 -> 1234
    text = re.sub(r"(\d):(\d\d)\b", r"\1\2", text)           # 14:32 -> 1432
    text = re.sub(r"\b1/2\b", " ", text)                     # half addresses
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)        # twenty-two, port-chester
    text = re.sub(r"(?<=[a-z])-(?=\d)|(?<=\d)-(?=[a-z])", " ", text)  # i-95
    text = re.sub(r"\b([a-z])\.(?=\s|$)", r"\1", text)      # "n." -> "n"
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1\2", text)
    out: list[str] = []
    for part in _PUNCT_SPLIT.split(text):
        words = part.replace("/", " and ").split()
        for w in words:
            w = w.strip("-_*~'")
            if not w:
                continue
            if _DIGIT_HYPHEN.match(w):
                pieces = w.split("-")
                if all(len(p) == 1 for p in pieces):
                    w = "".join(pieces)           # 1-2-3 -> 123
                else:
                    w = pieces[0]                 # 51-53 -> 51
            else:
                w = re.sub(r"[^a-z0-9]", "", w)
                if not w:
                    continue
            out.append(w)
        out.append(BOUNDARY)
    while out and out[-1] == BOUNDARY:
        out.pop()
    return out


def tokenize(text: str) -> list[str]:
    """Normalise free text to tokens: digits for spoken numbers, abbreviated types.

    Punctuation becomes the boundary token ``|``, which stops number runs.
    """
    raw = _raw_tokens(text)
    out: list[str] = []
    i = 0
    while i < len(raw):
        w = raw[i]
        if w in NUMBER_WORDS or w in ORDINALS:
            if w in _ZERO_ONLY_INSIDE or (w in MULTIPLIERS):
                out.append(w)
                i += 1
                continue
            j = i
            run: list[str] = []
            while j < len(raw):
                x = raw[j]
                if x in NUMBER_WORDS:
                    run.append(x)
                    j += 1
                    continue
                if x in ORDINALS:
                    run.append(x)
                    j += 1
                    break
                if x == "and" and run and run[-1] in MULTIPLIERS and j + 1 < len(raw) \
                        and raw[j + 1] in NUMBER_WORDS:
                    j += 1                        # "one hundred and five"
                    continue
                break
            while run and run[-1] in _ZERO_ONLY_INSIDE:
                run.pop()
                j -= 1
            parsed = _parse_run(run)
            if parsed:
                out.extend(parsed.split())
                i = j
                continue
            out.append(w)
            i += 1
            continue
        out.append(STREET_TYPES.get(w, w))
        i += 1
    # merge adjacent digit tokens ("1 2 3" or "one 23"); a number said twice is a repeat, not one number
    # ("135 135 South Regent Street", 60 Control 2026-10-15: not 135135)
    merged: list[str] = []
    for t in out:
        if t.isdigit() and len(t) >= 2 and merged and merged[-1] == t:
            continue
        if t.isdigit() and merged and merged[-1].isdigit() and len(merged[-1]) < 5:
            merged[-1] += t
        else:
            merged.append(t)
    # "i 95", "interstate 287", "287 northbound" -> "i 287"
    final: list[str] = []
    for k, t in enumerate(merged):
        if t in {"95", "287", "684"}:
            prev = final[-1] if final else ""
            nxt = merged[k + 1] if k + 1 < len(merged) else ""
            if prev in {"i", "rte", "hwy"}:
                final[-1] = "i"
                final.append(t)
                continue
            if nxt in BOUND_WORDS or nxt == "exit":
                final.extend(["i", t])
                continue
        final.append(t)
    return [t for t in final if t != "the"]


def street_key(name: str) -> str:
    """Canonical lookup key for a street name from any source ("N. Main Street" -> "n main st")."""
    toks = [t for t in tokenize(name) if t != BOUNDARY and t not in BOUND_WORDS]
    toks = [DIRECTION_WORDS.get(t, t) if (k == 0 or k == len(toks) - 1) and len(toks) > 2
            else t for k, t in enumerate(toks)]
    key = " ".join(toks)
    return KEY_SYNONYMS.get(key, key)


def spaceless(tokens) -> str:
    return "".join(tokens)


def street_aliases(key: str) -> list[tuple[str, bool, float]]:
    """Spoken forms of a street key as (spaceless alias, typed, penalty factor).

    ``typed`` means the alias ends with a street type ("pearlst"); untyped
    aliases ("pearl") are weaker evidence and only used with corroboration.
    """
    toks = key.split()
    out: dict[str, tuple[bool, float]] = {}

    def add(ts: list[str], typed: bool, pen: float) -> None:
        if not ts:
            return
        s = spaceless(ts)
        if len(s) < 3:
            return
        if s not in out or out[s][1] < pen:
            out[s] = (typed, pen)

    variants: list[tuple[list[str], float]] = [(toks, 1.0)]
    # abbreviations inside the name spelled out, so misspellings still compare
    # ("ter ave" -> "terrace ave": "tirrace avenue" is close to that, not to "terave")
    if len(toks) > 1 and any(t in EXPAND for t in toks[:-1]):
        spelled = [("saint" if k == 0 and t == "st" else EXPAND.get(t, t)) if k < len(toks) - 1
                   else t for k, t in enumerate(toks)]
        variants.append((spelled, 1.0))
    # expanded / dropped directionals: "n main st" -> "north main st", "main st"
    if len(toks) > 1 and toks[0] in DIRECTIONS and toks[1] not in TYPE_ABBRS:
        variants.append(([DIRECTIONS[toks[0]]] + toks[1:], 1.0))
    if len(toks) > 2 and toks[0] in DIRECTIONS:
        variants.append((toks[1:], 0.9))
    if len(toks) > 2 and toks[-1] in DIRECTIONS:
        variants.append((toks[:-1] + [DIRECTIONS[toks[-1]]], 1.0))
        variants.append((toks[:-1], 0.9))
    for ts, pen in variants:
        typed = bool(ts) and (ts[-1] in TYPE_ABBRS or ts[0] == "i" or
                              (len(ts) > 1 and ts[-2] in TYPE_ABBRS and ts[-1] in DIRECTIONS)
                              or (len(ts) > 1 and ts[-2] in TYPE_ABBRS and ts[-1] in DIRECTION_WORDS))
        add(ts, typed, pen)
        if ts and ts[-1] in TYPE_ABBRS and len(ts) > 1:
            add(ts[:-1], False, pen)
    if toks and toks[0] == "i" and len(toks) == 2:
        add(["interstate", toks[1]], True, 1.0)
        add(["rte", toks[1]], True, 0.95)
    if len(toks) > 2 and toks[:2] in (["state", "rte"], ["us", "rte"], ["state", "hwy"]):
        add(["rte"] + toks[2:], True, 0.95)
    for alias, target in EXTRA_ALIASES.items():
        if target == key:
            ts = alias.split()
            add(ts, ts[-1] in TYPE_ABBRS or len(ts) > 1, 0.92)
    return [(s, typed, pen) for s, (typed, pen) in out.items()]
