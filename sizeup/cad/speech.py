"""Written dispatch -> the words a dispatcher says, the same for every voice engine.

    house numbers      5 "five", 15 "fifteen", 100 "one hundred", 209 "two oh nine",
                       169 "one sixty-nine", 1200 "twelve hundred", 4040 "forty forty"
    times              "Time out 15:13" -> "Time out fifteen thirteen" (15:00 "fifteen hundred",
                       07:30 "oh seven thirty")
    callsigns          "2398" / "23 98" -> "twenty-three ninety-eight"
    unit codes         by their spoken names (data/cad_lists.json units.spoken: "77A4" -> "77 Alpha 4"),
                       then "seventy-seven Alpha four"; "Engine 60" -> "Engine sixty",
                       "Engine 191" -> "Engine one ninety-one"
    any number         the same way: 1-2 digits as words, 3 as "two oh nine", 4 in pairs, longer
                       digit by digit (so a house number needs no street after it to be said right)
    highways           the geocoder's and the @ places' labels (sizeup/geo/places.py), in words:
                       "I-95 SB exit 21" -> "I ninety-five southbound exit twenty-one"
                       "I-287 EB MM 10.3" -> "I two eighty-seven eastbound mile marker ten point three"
                       "Hutch NB exit 19A (old 30S)" -> "the Hutchinson River Parkway northbound exit nineteen A"
                       "Merritt Pkwy SB exit 19B" -> "the Merritt Parkway southbound exit nineteen B"
                       "US-1" -> "Route one"; "NY-120A MM 2.3" -> "Route one twenty A mile marker two point three"

The engines' own number readers say "two hundred nine" and "two thousand three hundred ninety-eight";
on the air it's "two oh nine" and "twenty-three ninety-eight". The written dispatch keeps the short
forms (the timeline, the log); only the voice gets these words.
"""

from __future__ import annotations

import re

ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
NATO = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                ("Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet Kilo Lima Mike November Oscar "
                 "Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey X-ray Yankee Zulu").split()))
NUM = r"(?<![\d.:,/])\b(\d+)\b(?![.:,/]\d)"


def two(n: int) -> str:
    """0..99 -> words: 7 'seven', 23 'twenty-three'."""
    if n < 20:
        return ONES[n]
    t, o = divmod(n, 10)
    return TENS[t] + ("" if o == 0 else "-" + ONES[o])


def pair(s: str) -> str:
    """The second half of a number said in pairs: '09' 'oh nine', '00' 'hundred', '69' 'sixty-nine'."""
    if s == "00":
        return "hundred"
    if s[0] == "0":
        return "oh " + ONES[int(s[1])]
    return two(int(s))


def digits(s: str) -> str:
    return " ".join(ONES[int(c)] for c in s)


def house(num: str) -> str:
    """A house (or unit, or any 1-4 digit) number as dispatch says it."""
    num = num.lstrip("0") or "0"
    if len(num) <= 2:
        return two(int(num))
    if len(num) == 3:
        return f"{ONES[int(num[0])]} {'hundred' if num[1:] == '00' else pair(num[1:])}"
    if len(num) == 4:
        if num[1:] == "000":
            return f"{ONES[int(num[0])]} thousand"
        return f"{two(int(num[:2]))} {pair(num[2:])}"
    return digits(num)


def clock(hh: str, mm: str) -> str:
    """'15', '13' -> 'fifteen thirteen'; '15', '00' -> 'fifteen hundred'; '07', '30' -> 'oh seven thirty'."""
    h = int(hh)
    hw = "zero zero" if h == 0 else (("oh " if len(hh) == 2 and hh[0] == "0" else "") + two(h))
    return f"{hw} {pair(mm)}"


def unit_codes(text: str, spoken: dict[str, str] | None) -> str:
    """Unit codes in the text -> their spoken names ("77A4" -> "77 Alpha 4", "2398" -> "23 98").
    `spoken` is data/cad_lists.json's units -> spoken; a code like 77A4 not in it is spelled with
    the phonetic alphabet the same way."""
    if spoken:
        codes = sorted((c for c, s in spoken.items() if c and s and c != s and any(ch.isdigit() for ch in c)),
                       key=len, reverse=True)
        if codes:
            pat = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, codes)) + r")(?![\w-])")
            text = pat.sub(lambda m: spoken[m.group(1)], text)
    return re.sub(r"\b(\d{1,3})([A-Z])(\d{1,2})\b", lambda m: f"{m.group(1)} {NATO[m.group(2)]} {m.group(3)}", text)


# -- highways: the labels sizeup/geo/places.py writes ("I-95 SB exit 21", "I-287 EB MM 10.0",
# "Hutch NB exit 19A (old 30S)", "Merritt Pkwy SB exit 19B", "US-1 NB MM 0.8", "NY-120A MM 2.3"), in words.
BOUND = {"N": "northbound", "S": "southbound", "E": "eastbound", "W": "westbound"}
# A direction after the road ("SB"), or the slashed form a dispatcher types in remarks ("S/B", "s/b").
DIRECTION = re.compile(r"\b([NSEW])B\b|\b([NSEWnsew])/[Bb]\b")
INTERSTATE = re.compile(r"\b(?:I-|Interstate[- ])(\d{1,3})\b")
ROUTE = re.compile(r"\b(?:US|NY|CT|SR|Rte\.?|Route)[- ](\d{1,3})([A-Z])?\b")
MARKER = re.compile(r"\b(?:MM|mile marker) (\d+)(?:\.(\d+))?\b", re.I)
# "exit 19A" -> "exit nineteen A"; the renumbering note the @ search adds ("(old 30S)") is not said.
EXIT = re.compile(r"\b(exit) (\d+)([A-Z])?\b(?: \(old [^)]*\))?", re.I)
HWY_TOKEN = r"(?: [NSEW]B\b| [NSEW]/B\b| exit\b| MM\b)"
PARKWAYS = [
    (re.compile(r"\b(?:the )?(?:Hutch|Hutchinson(?: River)? (?:Pkwy|Parkway))\b"), "the Hutchinson River Parkway"),
    (re.compile(r"\b(?:the )?Merritt (?:Pkwy|Parkway)\b"), "the Merritt Parkway"),
    (re.compile(r"\b(?:the )?Merritt\b(?=" + HWY_TOKEN + ")"), "the Merritt Parkway"),   # "Merritt SB exit 19B"
]


def marker(whole: str, frac: str | None) -> str:
    """'10', '0' -> 'ten'; '10', '3' -> 'ten point three'; '14', '35' -> 'fourteen point three five'."""
    said = house(whole)
    if frac and frac.strip("0"):
        said += " point " + digits(frac)
    return said


def highways(text: str) -> str:
    """Highway locations as the voice says them: the road ("I ninety-five", "Route one twenty A", "the
    Hutchinson River Parkway"), the direction ("southbound"), the spot ("exit nineteen A", "mile marker
    ten point three"). Anything else is left for words()."""
    t = text
    for pat, name in PARKWAYS:
        t = pat.sub(name, t)
    t = INTERSTATE.sub(lambda m: f"I {house(m.group(1))}", t)
    t = ROUTE.sub(lambda m: f"Route {house(m.group(1))}" + (f" {m.group(2)}" if m.group(2) else ""), t)
    t = MARKER.sub(lambda m: f"mile marker {marker(m.group(1), m.group(2))}", t)
    t = EXIT.sub(lambda m: f"{m.group(1)} {house(m.group(2))}" + (f" {m.group(3).upper()}" if m.group(3) else ""), t)
    return DIRECTION.sub(lambda m: BOUND[(m.group(1) or m.group(2)).upper()], t)


def words(text: str, spoken: dict[str, str] | None = None) -> str:
    """The written dispatch as the voice should say it (see the module doc)."""
    t = unit_codes(highways(text), spoken)
    t = re.sub(r"(?<![\d:])\b([01]?\d|2[0-3]):([0-5]\d)\b(?!:\d)", lambda m: clock(m.group(1), m.group(2)), t)
    # A callsign written in pairs ("23 98", "77 05"): one number, said in pairs.
    t = re.sub(r"(?<![\d.])\b(\d{2}) (\d{2})\b(?![.:]\d| \d)", lambda m: house(m.group(1) + m.group(2)), t)
    t = re.sub(NUM, lambda m: house(m.group(1)), t)
    return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
