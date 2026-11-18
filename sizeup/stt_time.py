"""The dispatcher's trailing time (#727): on the county's Fire/EMS dispatch talkgroups the dispatcher ends most messages
with the time of day in 24-hour form ("... responding, 1550"). Each clip's air time is known, so this is a free check
on every such clip, with no human label.

    check(text, end)    the time read at the end of a transcript, compared with the clip's local air time
                        (America/New_York, sizeup/tz.py) at its end: a reading WINDOW_MIN (-2 ... +1 min of the clip
                        end's minute) fits. Check.result:
                            match           a reading fits: Check.text is the transcript with that time written the
                                            canonical way ("fifteen forty nine." -> "1549."), Check.flag "auto_label"
                                            (a candidate for automatic training labels, #728)
                            time_mismatch   a trailing time was read but none of its readings fits: the text is left
                                            alone, Check.flag "review"
                            no_time         nothing at the end reads as a time: counted as such, never a failure
                        The raw transcript is never changed here: the corrected text is a separate field of the run
                        that recorded it (sizeup/sttruns.py: time_check, time_said, time_off, time_conf, fixed_text,
                        fix_rule, label_flag), under RULE.

What reads as a trailing time (the last words, after any "hours" / "hrs" / "p.m."):
    digits      "1550", "1550.", "15:50", "15-49", "15.50", "1,550" (a numeral's comma), "0940"; "940" (a leading zero
                dropped); "15 50" (split); "1 5 5 0" (digit by digit; never "1-5-5-0", nearly always something
                else spelled out)
    words       "fifteen fifty", "fifteen forty-nine", "zero nine forty", "oh nine oh five", "twenty three hundred",
                mixed "15 fifty"; "nine forty" (the leading zero dropped)
    garbles     "fourty", "niner", "o" for zero, a "p.m." an engine added to a 12-hour reading (+12 h)
Each reading has a base confidence (how sure the shape is a time); a fit at the edge of the window costs a little.
A transcript can give several readings ("15 50": 1550; "twenty one fifty": 2150 or 0150?): the best that fits wins.

A whisper prompt with the clip's own HHMM in it ([stt] talker_prompt = "ids+time") would lead the engine to the right
time: a run's prompt_v says which prompt it had, so such runs can be told apart.

Cheap: one regex over the transcript's last TAIL_CHARS, run on the hub as each transcript arrives (every engine).
Which clips (applies()): the channels in Config.stt_time_channels ([stt] time_channels), by default the county's
Fire/EMS N dispatch talkgroups (DEFAULT_CHANNELS, "tg:<n>" keys as sizeup/sourcekeys.channel_key gives them; off
everywhere else), and of those only the dispatcher's (a talker alias sizeup/cad/radios.is_dispatcher knows) or one with
no talker: a unit's own transmission rarely ends with the time, and a number it ends with is its own business.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sizeup.tz import TZ

RULE = "ttime/1"
# Fire/EMS 10 ... 23 (deploy/trunk-recorder/talkgroups.csv; tests/test_stt_time.py checks they still are)
DEFAULT_CHANNELS = ("tg:111", "tg:113", "tg:303", "tg:103", "tg:305", "tg:307", "tg:105", "tg:107", "tg:309", "tg:311",
                    "tg:325", "tg:327", "tg:329", "tg:331")
WINDOW_MIN = (-2, 1)          # said - the clip end's minute, in minutes: this fits
TAIL_CHARS = 120
MATCH, MISMATCH, NO_TIME = "match", "time_mismatch", "no_time"
AUTO_LABEL, REVIEW = "auto_label", "review"
RESULTS = (MATCH, MISMATCH, NO_TIME)

UNITS = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "niner": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50}
ZEROS = {"zero", "oh", "o"}
AFTER = {"hours", "hour", "hrs", "hr"}    # said (or written) after the time
PM = ("pm", ("p", "m"))
TOKEN = re.compile(r"\d+(?:[:.,\-]\d+)*|[A-Za-z]+")

# a reading's base confidence, by its shape
CONF = {"digits": 0.95, "sep": 0.95, "words": 0.9, "mixed": 0.85, "split": 0.85, "bydigit": 0.85, "nozero": 0.8}
EDGE = 0.9                    # a fit at the window's edge (-2 or +1 min)
PM_COST = 0.8                 # a 12-hour reading made 24-hour


@dataclass(frozen=True)
class Check:
    result: str               # match | time_mismatch | no_time
    said: str = ""            # the reading, canonical ("1549"): the one that fits, else the best one read
    off_min: int | None = None   # said - the clip end's minute (None: no_time)
    conf: float | None = None
    text: str = ""            # the corrected transcript (match only)
    rule: str = ""            # RULE + ":" + the shape ("ttime/1:words")
    flag: str = ""            # auto_label (match) | review (time_mismatch) | ""


@dataclass(frozen=True)
class _Atom:
    kind: str                 # d (digits) | sep (a separated numeral, read already) | unit | zero | teen | tens | hundred
    value: int | str
    start: int
    end: int


@dataclass(frozen=True)
class _Reading:
    hhmm: int                 # hours * 100 + minutes
    conf: float
    shape: str
    start: int                # its characters in the transcript
    end: int


def _atom(tok: str, start: int, end: int) -> _Atom | None:
    if tok[0].isdigit():
        if tok.isdigit():
            return _Atom("d", tok, start, end)
        return _Atom("sep", tok, start, end)
    w = tok.lower()
    if w in ZEROS:
        return _Atom("zero", 0, start, end)
    if w in UNITS:
        return _Atom("unit", UNITS[w], start, end)
    if w in TEENS:
        return _Atom("teen", TEENS[w], start, end)
    if w in TENS:
        return _Atom("tens", TENS[w], start, end)
    if w == "hundred":
        return _Atom("hundred", 0, start, end)
    return None


def _sep(tok: str) -> tuple[int, str] | None:
    """A numeral with separators inside: "15:50", "15-49", "15.50" (HH, MM), "1,550" (a thousands comma). Not "1-5-5-0":
    on these talkgroups that is nearly always something else spelled out, never the time."""
    parts = re.split(r"[:.,\-]", tok)
    if len(parts) == 2 and "," in tok and len(parts[1]) == 3:
        d = "".join(parts)
        return (int(d), "digits") if len(d) == 4 else (int(d), "nozero") if len(d) == 3 else None
    if len(parts) == 2 and 1 <= len(parts[0]) <= 2 and len(parts[1]) == 2:
        return int(parts[0]) * 100 + int(parts[1]), "sep"
    return None


def _hours(a: list[_Atom]) -> tuple[int, bool] | None:
    """The hours part: (hours, all words)."""
    k = [x.kind for x in a]
    if k == ["d"] and len(a[0].value) <= 2:
        return int(a[0].value), False
    if k in (["unit"], ["zero"]):
        return a[0].value, True
    if k in (["zero", "unit"], ["zero", "zero"]):
        return a[1].value, True
    if k == ["teen"]:
        return a[0].value, True
    if k == ["tens"] and a[0].value == 20:
        return 20, True
    if k == ["tens", "unit"] and a[0].value == 20:
        return 20 + a[1].value, True
    return None


def _minutes(a: list[_Atom]) -> tuple[int, bool] | None:
    """The minutes part: (minutes, all words)."""
    k = [x.kind for x in a]
    if k == ["d"] and len(a[0].value) == 2:
        return int(a[0].value), False
    if k == ["hundred"]:
        return 0, True
    if k in (["zero", "unit"], ["zero", "zero"]):
        return a[1].value, True
    if k in (["teen"], ["tens"]):
        return a[0].value, True
    if k == ["tens", "unit"] and a[1].value:
        return a[0].value + a[1].value, True
    return None


def _valid(hhmm: int) -> bool:
    return 0 <= hhmm // 100 <= 23 and 0 <= hhmm % 100 <= 59


def _parse(a: list[_Atom]) -> list[tuple[int, str]]:
    """The readings of a run of number atoms taken whole: [(hhmm, shape)]."""
    out: list[tuple[int, str]] = []
    k = [x.kind for x in a]
    if len(a) == 1:
        x = a[0]
        if x.kind == "d" and len(x.value) == 4:
            out.append((int(x.value), "digits"))
        elif x.kind == "d" and len(x.value) == 3:
            out.append((int(x.value), "nozero"))
        elif x.kind == "sep":
            got = _sep(x.value)
            if got:
                out.append(got)
        return [r for r in out if _valid(r[0])]
    if len(a) == 4 and all(x.kind in ("unit", "zero") or (x.kind == "d" and len(x.value) == 1) for x in a):
        out.append((int("".join(str(x.value) for x in a)), "bydigit"))
    if k == ["d", "d"] and 1 <= len(a[0].value) <= 2 and len(a[1].value) == 2:
        out.append((int(a[0].value) * 100 + int(a[1].value), "split"))
    for cut in range(1, len(a)):
        h, m = _hours(a[:cut]), _minutes(a[cut:])
        if h is None or m is None or (k == ["d", "d"]):
            continue
        words = h[1] and m[1]
        shape = "mixed" if not words else "nozero" if [x.kind for x in a[:cut]] == ["unit"] else "words"
        out.append((h[0] * 100 + m[0], shape))
    return [r for r in out if _valid(r[0])]


def readings(text: str) -> list[_Reading]:
    """Every reading of a trailing time in text, best first (none: no trailing time)."""
    if not text:
        return []
    base = max(0, len(text) - TAIL_CHARS)
    toks = [(m.group(), base + m.start(), base + m.end()) for m in TOKEN.finditer(text[base:])]
    if base and toks:
        toks = toks[1:]                            # (the first may be cut)
    pm = False
    while toks and toks[-1][0].lower() in AFTER:
        toks.pop()
    if toks and toks[-1][0].lower() == "pm":
        toks.pop()
        pm = True
    elif len(toks) >= 2 and toks[-2][0].lower() == "p" and toks[-1][0].lower() == "m":
        del toks[-2:]
        pm = True
    while toks and toks[-1][0].lower() in AFTER:
        toks.pop()
    run: list[_Atom] = []
    for tok, s, e in reversed(toks):
        a = _atom(tok, s, e)
        if a is None or len(run) >= 6:
            break
        run.insert(0, a)
    best: dict[int, _Reading] = {}
    for i in range(len(run) - 1, -1, -1):
        for hhmm, shape in _parse(run[i:]):
            conf = CONF[shape]
            if pm:
                if hhmm // 100 >= 12:
                    continue
                hhmm, conf = hhmm + 1200, conf * PM_COST
            r = _Reading(hhmm, round(conf, 3), shape + ("+pm" if pm else ""), run[i].start, run[-1].end)
            if hhmm not in best or best[hhmm].conf < r.conf:
                best[hhmm] = r
    return sorted(best.values(), key=lambda r: -r.conf)


def _minute_of(t: float) -> int:
    d = datetime.fromtimestamp(float(t), TZ)
    return d.hour * 60 + d.minute


def _off(hhmm: int, minute: int) -> int:
    """said - minute, in minutes, the short way round midnight."""
    return ((hhmm // 100 * 60 + hhmm % 100) - minute + 720) % 1440 - 720


def canonical(hhmm: int) -> str:
    return f"{hhmm:04d}"


def check(text: str, end: float | None, start: float | None = None) -> Check | None:
    """The trailing-time check of one transcript against its clip's air time (end: the clip's end, epoch seconds; start
    stands in when the end is unknown). None: nothing to check (no text, no time)."""
    t = end if isinstance(end, (int, float)) else start if isinstance(start, (int, float)) else None
    if not text or not text.strip() or t is None:
        return None
    rs = readings(text)
    if not rs:
        return Check(NO_TIME)
    minute = _minute_of(t)
    lo, hi = WINDOW_MIN
    fits = []
    for r in rs:
        off = _off(r.hhmm, minute)
        if lo <= off <= hi:
            fits.append((round(r.conf * (EDGE if off in (lo, hi) else 1.0), 2), r, off))
    if not fits:
        r = rs[0]
        return Check(MISMATCH, canonical(r.hhmm), _off(r.hhmm, minute), round(r.conf, 2), "", f"{RULE}:{r.shape}", REVIEW)
    conf, r, off = max(fits, key=lambda f: (f[0], -abs(f[2])))
    fixed = text[:r.start] + canonical(r.hhmm) + text[r.end:]
    return Check(MATCH, canonical(r.hhmm), off, conf, fixed, f"{RULE}:{r.shape}", AUTO_LABEL)


def applies(key: str, talker: str, channels) -> bool:
    """A clip to check: its channel is one of channels, and its talker the dispatcher (or not known)."""
    if not key or key not in channels:
        return False
    if not talker:
        return True
    from sizeup.cad.radios import is_dispatcher
    return all(is_dispatcher(x.strip()) for x in str(talker).split(",") if x.strip())


def channels_of(items) -> tuple[str, ...]:
    """[stt] time_channels as keys: a number is a county talkgroup ("tg:<n>"), a string a channel key as written."""
    return tuple(f"tg:{c}" if isinstance(c, int) else str(c) for c in items)
