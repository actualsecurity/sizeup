"""Speech-to-text misreads of a street and a house number: how close a page's heard street is to a typed one,
which known street it snaps to, and whether two house numbers are one misheard as the other. Pure string functions,
no dependencies; sizeup/cad/sources.py uses them to match a page to a County CAD Times incident.

    name_sim   street names compared after folding case, spaces and doubled letters ("Sampleworth" / "Samplewerth"):
               1 - edit distance / longer length; a sound-alike (the same consonant skeleton) counts as STREET_SIM
               when the spelling is at least SOUND_FLOOR close. Names under SHORT letters must match exactly
    snap       the one known street a heard name is closest to: at least STREET_SIM, and SNAP_MARGIN clear of every
               other known street (two near ones: ambiguous, never snapped)
    misread    a heard house number that is the typed one with one digit inserted or dropped (a repeated digit, "20"
               heard as "220")

Thresholds: STREET_SIM 0.84 lets one edit through on a name of 7+ folded letters and two on 13+, never one on a name of
5-6 (those need the same sound); SNAP_MARGIN 0.08 is pagegeo.RIVAL, the margin the page geocoder already uses for "two
readings fit"."""

from __future__ import annotations

import re

STREET_SIM = 0.84
SOUND_FLOOR = 0.7
SNAP_MARGIN = 0.08
SHORT = 5


def fold(name: str) -> str:
    """'Whit  Temore' -> 'witemore': lower case, letters and digits only, doubled letters once."""
    s = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    return re.sub(r"(.)\1+", r"\1", s)


def edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def skeleton(name: str) -> str:
    """A sound key: the folded name's first letter, then its consonants (no vowels, h, w, y), ck/c/q -> k, ph -> f,
    each run once."""
    s = fold(name)
    if not s:
        return ""
    s = s.replace("ph", "f").replace("ck", "k")
    s = re.sub(r"[cq]", "k", s)
    rest = re.sub(r"[aeiouhwy]", "", s[1:])
    return re.sub(r"(.)\1+", r"\1", s[0] + rest)


def name_sim(a: str, b: str) -> float:
    """0..1: how alike two street names are as a page's speech-to-text might hear one for the other."""
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return 0.0
    if fa == fb:
        return 1.0
    if min(len(fa), len(fb)) < SHORT:
        return 0.0
    r = 1 - edit_distance(fa, fb) / max(len(fa), len(fb))
    if r >= SOUND_FLOOR and skeleton(fa) == skeleton(fb):
        r = max(r, STREET_SIM)
    return r


def snap(name: str, known) -> tuple[str, object]:
    """The known street a heard name is: known is [(name, value)]; ("snap", value) for one clear best, ("ambiguous",
    None) for two or more near it, ("none", None) for nothing close. Two entries with the same folded name and different
    values (one street in two forms) count as two."""
    scored = sorted(((name_sim(name, n), v) for n, v in known), key=lambda p: -p[0])
    if not scored or scored[0][0] < STREET_SIM:
        return "none", None
    best, value = scored[0]
    rivals = [s for s, v in scored[1:] if v != value and s >= best - SNAP_MARGIN]
    return ("ambiguous", None) if rivals else ("snap", value)


def misread(heard: str, typed: str) -> bool:
    """A house number speech-to-text heard with one digit inserted or dropped ("220" for "20", "17" for "177", "2" for
    "20"). Not the same number, not a substitution ("30" for "20": another house)."""
    a, b = re.sub(r"\D", "", heard or ""), re.sub(r"\D", "", typed or "")
    if not a or not b or a == b or abs(len(a) - len(b)) != 1 or a[0] == "0" or b[0] == "0":
        return False
    long, short = (a, b) if len(a) > len(b) else (b, a)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))
