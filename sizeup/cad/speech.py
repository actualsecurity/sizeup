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

The engines' own number readers say "two hundred nine" and "two thousand three hundred ninety-eight";
on the air it's "two oh nine" and "twenty-three ninety-eight".
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


def words(text: str, spoken: dict[str, str] | None = None) -> str:
    """The written dispatch as the voice should say it (see the module doc)."""
    t = unit_codes(text, spoken)
    t = re.sub(r"(?<![\d:])\b([01]?\d|2[0-3]):([0-5]\d)\b(?!:\d)", lambda m: clock(m.group(1), m.group(2)), t)
    # A callsign written in pairs ("23 98", "77 05"): one number, said in pairs.
    t = re.sub(r"(?<![\d.])\b(\d{2}) (\d{2})\b(?![.:]\d| \d)", lambda m: house(m.group(1) + m.group(2)), t)
    t = re.sub(NUM, lambda m: house(m.group(1)), t)
    return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
