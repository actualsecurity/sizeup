"""Deterministic post-ASR vocabulary correction for every engine (#879 part 3): the GPU workers', AWS's and the station's
whisper's text, matched against the channel's own private term list (sizeup/stt_terms.py), on the hub.

    correct(text)   Vocab.correct: the corrected text and each correction (heard -> to, rule, score). Three rules:
        unit        spoken numbers in a unit ID or call sign folded to its digits, only into a term of the list:
                    "engine ninety seven" -> "Engine 97", "ninety control" -> "90 Control" (made-up units). The
                    number words must be the whole run ("engine ninety seven one" is never "Engine 97 one"), or what is
                    left of it the trailing time. The prefix is one of UNIT_PREFIX ("Fire N" and "Battalion N" are
                    never folded, as transcribe.NO_RESPELL)
        ten         a ten-code of the list said in words, at a clause end: "ten four." -> "10-4."
        spell /     1-4 words matched to a name of the shared places list (a street, a town, a landmark: a term in
        sound       Title Case with no digit), by spelling (edit distance of the letters, spaces and doubled letters folded)
                    and sound (Double Metaphone, sizeup/dmetaphone.py): score = W_SPELL * spell + (1 - W_SPELL) * sound.
                    A street needs its suffix heard ("Samplewerth Street" -> "Sampleworth Street"; "St" = "Street").
                    "sound" when the sound keys are equal and the letters are not
    Safety          A correction is made only when:
                    - its score is at least ACCEPT (ACCEPT_ONE for one word to a one-word name), with spell >= SPELL_FLOOR
                      and sound >= SOUND_FLOOR, on at least MIN_CHARS letters;
                    - word for word alike when as many words were heard as the name has (WORD_FLOOR, WORD_SOUND_FLOOR:
                      "coming" is never "Common"), the same first letter unless spelled FIRST_FLOOR alike, and never a
                      word the name has that wasn't heard; one heard word only as the engine capitalised it, never a
                      sentence's first; no one-letter word; a possessive kept as heard;
                    - no other term of the whole list (any town's street, any phrase) scores within MARGIN of it (a
                      close rival: ambiguous, left alone);
                    - its term is in scope: a street listed under "streets: <town>, ..." only for the towns in
                      Config.stt_correct_towns (another town's street is a rival, never a target);
                    - it touches no trailing time (sizeup/stt_time.py readings: that is stt_time's) and no digit (a
                      numeral is never changed; the unit rule only turns words into digits);
                    - the words are not already a term of the list (exact, ignoring case and punctuation).
                    The channel's own list only (stt_terms.own_for: never another channel's).

Kept: the raw transcript is never changed. Each run records the corrected text beside it (sizeup/sttruns.py: corr_text,
corr_n, corr_rules [[heard, to, rule, score], ...], corr_v RULE@<list@version>). The Radio rows show the corrected text
(with the status words put back after it, sizeup/cad/statushints.py) and the heard words on hover ("fixed_text" /
"fixes"), computed when read like the status repair, so a stored row is never rewritten.

Which channels (Corrector.applies): Config.stt_correct_channels ([stt] correct_channels), by default the county's Fire/EMS
dispatch talkgroups (stt_time.DEFAULT_CHANNELS): off elsewhere until measured (tools/stt_correct_eval.py). Never logs a
term or a transcript: counts only."""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from sizeup import stt_terms, stt_time
from sizeup.dmetaphone import double_metaphone

RULE = "vcorr/1"
DEFAULT_CHANNELS = stt_time.DEFAULT_CHANNELS
DEFAULT_TOWNS = ("Port Chester", "Rye Brook", "Rye")
MAX_N = 4                      # words in a span at most
ACCEPT = 0.86
ACCEPT_ONE = 0.9               # one heard word to a one-word name
SPELL_FLOOR = 0.7
SOUND_FLOOR = 0.75
MIN_CHARS = 5                  # letters of the heard name at least (MIN_CHARS_ONE for one word)
MIN_CHARS_ONE = 6
WORD_FLOOR = 0.75              # word for word (as many words heard as the name has): each spelled this alike, ...
WORD_SOUND_FLOOR = 0.65        # ... or sounding the same and spelled this alike ("planes" / "plains")
FIRST_FLOOR = 0.9              # a name whose first letter differs from what was heard: spelled this alike at least
MARGIN = 0.08                  # a rival this close: ambiguous (sizeup/cad/fuzzy.py SNAP_MARGIN)
W_SPELL = 0.6
MAX_RUN = 6                    # number words in a unit ID's run at most
CACHE = 4096                   # corrected texts kept per channel list (the Radio rows' reads)

TOKEN = re.compile(r"[A-Za-z][A-Za-z']*|\d+(?:[-:.,]\d+)*")
WORD = re.compile(r"[A-Za-z][A-Za-z']*")
NAME_WORD = re.compile(r"[A-Za-z][A-Za-z'.]*")
NOT_LETTER = re.compile(r"[^a-z]")
DOUBLED = re.compile(r"(.)\1+")
STREETS = re.compile(r"\s*streets?\s*:\s*(.*)$", re.I)
NOTE = re.compile(r"\(.*?\)")
TEN_CODE = re.compile(r"10-\d{1,3}")
JOIN = re.compile(r"[\s\-]*")
STOP_AFTER = re.compile(r"\s*(?:[,.;!?]|$)")
NUMWORDS = set(stt_terms._ONES) | set(stt_terms._TENS) | {"hundred"}
UNIT_PREFIX = {"engine", "ladder", "tower", "rescue", "truck", "utility", "squad", "medic", "ambulance", "tanker",
               "brush", "marine", "car", "chief", "unit", "haz", "hazmat", "mat", "special", "foam", "air", "boat",
               "patrol", "sector", "control", "ems", "fast", "rit", "support", "command", "safety", "collapse"}
NO_FOLD = {"fire", "battalion", "to"}       # (transcribe.NO_RESPELL: "working fire two story")
SUFFIX = {"street": "street", "st": "street", "str": "street", "avenue": "avenue", "ave": "avenue", "av": "avenue",
          "road": "road", "rd": "road", "drive": "drive", "dr": "drive", "lane": "lane", "ln": "lane",
          "place": "place", "pl": "place", "court": "court", "ct": "court", "boulevard": "boulevard",
          "blvd": "boulevard", "parkway": "parkway", "pkwy": "parkway", "terrace": "terrace", "ter": "terrace",
          "circle": "circle", "cir": "circle", "way": "way", "turnpike": "turnpike", "tpke": "turnpike",
          "highway": "highway", "hwy": "highway", "plaza": "plaza", "square": "square", "sq": "square",
          "trail": "trail", "path": "path", "crescent": "crescent", "alley": "alley", "row": "row"}
CONNECT = {"of", "the", "and", "at", "on", "de", "la", "du", "von", "van", "a"}   # lower case inside a Title Case name
STOP = {"a", "an", "the", "and", "or", "but", "of", "on", "in", "at", "to", "for", "from", "by", "with", "into", "onto",
        "off", "up", "down", "out", "over", "is", "are", "was", "were", "be", "been", "being", "am", "it", "its", "it's",
        "that", "this", "these", "those", "there", "here", "where", "what", "who", "when", "how", "why", "which", "we",
        "you", "i", "he", "she", "they", "me", "us", "him", "her", "them", "my", "your", "our", "his", "their", "not",
        "no", "yes", "yeah", "ok", "okay", "copy", "copied", "received", "go", "going", "get", "got", "have", "has",
        "had", "do", "does", "did", "will", "would", "can", "could", "should", "all", "any", "some", "just", "now",
        "then", "so", "if", "as", "also", "again", "back", "about", "please", "thank", "thanks", "you're", "we're",
        "i'm", "that's", "there's", "let", "let's", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "oh", "o", "uh", "um", "be", "same", "other", "another", "side", "front", "rear", "near",
        "left", "right", "street", "avenue", "road", "lane", "drive", "place",
        "unit", "units", "engine", "ladder", "control", "command", "responding", "location", "scene", "route",
        "bound", "northbound", "southbound", "eastbound", "westbound", "inbound", "outbound"}
SENTENCE_END = re.compile(r"[.!?]\s*$")


@dataclass(frozen=True)
class Fix:
    heard: str                 # the raw text's words
    to: str                    # the term as written
    rule: str                  # unit | ten | spell | sound
    score: float
    at: int                    # where they start in the raw text


@dataclass(frozen=True)
class Result:
    text: str                  # the corrected text ("" when nothing changed)
    fixes: tuple[Fix, ...]
    v: str                     # RULE@<list>@<version>

    @property
    def n(self) -> int:
        return len(self.fixes)

    def rules_json(self) -> str:
        """stt_runs.corr_rules: [[heard, to, rule, score], ...] ("" for none)."""
        return json.dumps([[f.heard, f.to, f.rule, f.score] for f in self.fixes], separators=(",", ":"),
                          ensure_ascii=False) if self.fixes else ""


@dataclass(frozen=True)
class _Tok:
    text: str
    low: str
    start: int
    end: int
    digit: bool


@dataclass
class _Entry:
    written: str
    key: str                   # term_key(written)
    suffix: str                # a street's canonical suffix ("" none)
    fold: str                  # the name's letters (spaces and doubled letters folded)
    spaced: str                # its words' letters (doubled letters kept): the same is no correction
    parts: tuple[str, ...]     # each word folded
    codes: tuple[str, ...]     # the name's sound keys (primary, alternate)
    words: int                 # the name's words
    target: bool               # may be corrected into (Title Case, no digit, in scope)


def fold(s: str) -> str:
    """Letters only, lower case, doubled letters once ("Holly  Ridge" -> "holyridge")."""
    return DOUBLED.sub(r"\1", NOT_LETTER.sub("", s.lower()))


def _codes(words: list[str]) -> tuple[str, ...]:
    """A name's sound keys: each word's Double Metaphone keys joined (primary with primary, alternate with alternate),
    and, for more than one word, the keys of the words written as one ("Holly Ridge" as "Hollyridge": a word's first
    letter sounds otherwise)."""
    p, a = [], []
    for w in words:
        x, y = _dm(w)
        p.append(x)
        a.append(y or x)
    keys = ["".join(p), "".join(a)]
    if len(words) > 1:
        keys += list(_dm("".join(words)))
    return tuple(dict.fromkeys(c for c in keys if c))


_DM: dict[str, tuple[str, str]] = {}


def _dm(w: str) -> tuple[str, str]:
    k = w.lower()
    got = _DM.get(k)
    if got is None:
        got = double_metaphone(k, None)
        if len(_DM) < 200000:
            _DM[k] = got
    return got


def _word_ok(a: str, b: str) -> bool:
    """Two words (folded) alike enough to be one misheard: spelled WORD_FLOOR alike, or the same sound key and spelled
    WORD_SOUND_FLOOR alike ("coming" is never "common", "Harmon" never "Harrison")."""
    sp = _sim(a, b)
    return sp >= WORD_FLOOR or (sp >= WORD_SOUND_FLOOR and _dm(a)[0] == _dm(b)[0])


def _spaced(words) -> str:
    return " ".join(NOT_LETTER.sub("", w.lower()) for w in words)


def _deletes(code: str) -> set[str]:
    return {code} | {code[:i] + code[i + 1:] for i in range(len(code))}


def edit_distance(a: str, b: str, cap: int | None = None) -> int:
    """Levenshtein distance; with cap, any distance over it is returned as cap + 1 (stopped early)."""
    if len(a) < len(b):
        a, b = b, a
    if cap is not None and len(a) - len(b) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if cap is not None and min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _sim(a: str, b: str, floor: float = 0.0) -> float:
    """1 - edit distance / the longer length (0 under floor: stopped early)."""
    if not a or not b:
        return 0.0
    n = max(len(a), len(b))
    cap = int((1.0 - floor) * n + 1e-9) if floor > 0 else None
    d = edit_distance(a, b, cap)
    return 0.0 if cap is not None and d > cap else 1.0 - d / n


def _title(term: str) -> bool:
    ws = NAME_WORD.findall(term)
    return bool(ws) and ws[0][0].isupper() and all(w[0].isupper() or w.lower() in CONNECT for w in ws)


def towns_of(section: str) -> tuple[str, ...]:
    """A "streets: <town>, <town> (notes)" section's towns, () for any other section."""
    m = STREETS.match(section or "")
    if not m:
        return ()
    body = NOTE.sub("", m.group(1))
    return tuple(t.strip().casefold() for t in body.split(",") if t.strip())


def tokens(text: str) -> list[_Tok]:
    return [_Tok(m.group(), m.group().lower(), m.start(), m.end(), any(c.isdigit() for c in m.group()))
            for m in TOKEN.finditer(text)]


class Vocab:
    """One channel's term set, ready to match: built once per set version (Corrector caches it)."""

    def __init__(self, terms, sections: dict[str, str] | None = None, towns=DEFAULT_TOWNS, v: str = "",
                 names=None):
        """names: the term_keys that may be corrected into (the shared places list's; None: every Title Case term)."""
        self.v = v
        sections = sections or {}
        towns = {str(t).strip().casefold() for t in towns}
        self.exact: set[str] = set()
        self.entries: list[_Entry] = []
        index: dict[tuple[str, str], set[int]] = {}       # (suffix, a sound key less one letter) -> entries
        self.units: dict[tuple[str, ...], dict[str, str]] = {}     # prefix words -> {digits: term as written}
        self.signs: dict[tuple[str, ...], dict[str, str]] = {}     # words after the digits ("90 Control")
        self.tens: dict[str, str] = {}                              # "10-4" -> "10-4"
        seen: set[str] = set()
        for raw in terms:
            w = stt_terms.written(raw)
            k = stt_terms.term_key(w)
            if not k or k in seen:
                continue
            seen.add(k)
            self.exact.add(k)
            if stt_terms.has_number(w):
                self._numeric(w)
                continue
            words = WORD.findall(w)
            if not words:
                continue
            suffix = SUFFIX.get(words[-1].lower(), "") if len(words) >= 2 else ""
            name = words[:-1] if suffix else words
            sec = sections.get(stt_terms.term_key(raw), "") or sections.get(k, "")
            scope = towns_of(sec)
            target = (_title(w) and (names is None or stt_terms.term_key(raw) in names or k in names)
                      and (not scope or any(t in towns for t in scope)))
            e = _Entry(w, k, suffix, fold("".join(name)), _spaced(name), tuple(fold(x) for x in name), _codes(name),
                       len(name), target)
            if not e.fold:
                continue
            i = len(self.entries)
            self.entries.append(e)
            for c in e.codes:
                for d in _deletes(c):
                    index.setdefault((suffix, d), set()).add(i)
        self.index = {k: tuple(v) for k, v in index.items()}
        self._best: OrderedDict[tuple, object] = OrderedDict()
        self._lock = threading.Lock()

    def _numeric(self, w: str) -> None:
        toks = w.split()
        if TEN_CODE.fullmatch(w):
            self.tens[w] = w
            return
        if len(toks) >= 2 and toks[-1].isdigit() and all(t.isalpha() for t in toks[:-1]):
            pre = tuple(t.lower() for t in toks[:-1])
            if pre[0] in UNIT_PREFIX and not (set(pre) & NO_FOLD):
                self.units.setdefault(pre, {})[toks[-1]] = w
        elif len(toks) >= 2 and toks[0].isdigit() and all(t.isalpha() for t in toks[1:]):
            post = tuple(t.lower() for t in toks[1:])
            if post[0] in UNIT_PREFIX:
                self.signs.setdefault(post, {})[toks[0]] = w

    # -- a span's best name ---------------------------------------------------------------------------------------------

    def best(self, words: tuple[str, ...]) -> tuple[_Entry, float, float, float] | str | None:
        """The name these heard words are: (entry, score, spell, sound); "exact" (they are a name already, but for
        case, spacing or an abbreviated suffix); None (nothing, or ambiguous)."""
        with self._lock:
            if words in self._best:
                self._best.move_to_end(words)
                return self._best[words]  # type: ignore[return-value]
        got = self._best_of(words)
        with self._lock:
            self._best[words] = got
            if len(self._best) > 50000:
                self._best.popitem(last=False)
        return got

    def _best_of(self, words: tuple[str, ...]):
        suffix = SUFFIX.get(words[-1].lower(), "") if len(words) >= 2 else ""
        name = list(words[:-1] if suffix else words)
        gf = fold("".join(name))
        spaced = _spaced(name)
        parts = [fold(x) for x in name]
        codes = _codes(name)
        if not gf or not codes:
            return None
        ids: set[int] = set()
        for c in codes:
            for d in _deletes(c):
                ids.update(self.index.get((suffix, d), ()))
        scored: dict[str, tuple[float, float, float, _Entry]] = {}
        for i in ids:
            e = self.entries[i]
            if e.spaced == spaced:
                return "exact"
            if e.words > len(name):                  # never a word put in that wasn't heard
                continue
            spell = _sim(gf, e.fold, SPELL_FLOOR)
            if spell < SPELL_FLOOR or (gf[0] != e.fold[0] and spell < FIRST_FLOOR):
                continue
            if e.words == len(name) and not all(_word_ok(a, b) for a, b in zip(parts, e.parts)):
                continue
            sound = max(_sim(a, b) for a in codes for b in e.codes)
            score = round(W_SPELL * spell + (1 - W_SPELL) * sound, 3)
            if e.key not in scored or scored[e.key][0] < score:
                scored[e.key] = (score, spell, sound, e)
        if not scored:
            return None
        ranked = sorted(scored.values(), key=lambda x: -x[0])
        top = next((x for x in ranked if x[3].target), None)
        if top is None:
            return None
        score, spell, sound, e = top
        one = len(name) == 1 and not suffix          # one heard word: only ever to a one-word name, and stricter
        if one and e.words != 1:
            return None
        if (score < (ACCEPT_ONE if one else ACCEPT) or spell < SPELL_FLOOR or sound < SOUND_FLOOR
                or len(gf) < (MIN_CHARS_ONE if one else MIN_CHARS)):
            return None
        if any(x[3].key != e.key and x[0] >= score - MARGIN for x in ranked):
            return None                         # a close rival (any town's, any phrase): ambiguous
        return e, score, spell, sound

    # -- a transcript ---------------------------------------------------------------------------------------------------

    def correct(self, text: str) -> tuple[str, list[Fix]]:
        """(the corrected text, its corrections); the text as it is when nothing applies."""
        if not text or not text.strip():
            return text, []
        toks = tokens(text)
        if not toks:
            return text, []
        joined = [bool(JOIN.fullmatch(text[toks[i].end:toks[i + 1].start])) for i in range(len(toks) - 1)]
        guard = [(r.start, r.end) for r in stt_time.readings(text)]      # the trailing time: stt_time's

        def guarded(i: int, j: int) -> bool:
            s, e = toks[i].start, toks[j - 1].end
            return any(s < ge and gs < e for gs, ge in guard)

        def run_ok(i: int, j: int) -> bool:
            return all(joined[i:j - 1])

        keys = [stt_terms.term_key(t.text) for t in toks]
        exact: list[tuple[int, int]] = []
        for n in range(MAX_N, 0, -1):
            for i in range(len(toks) - n + 1):
                j = i + n
                if run_ok(i, j) and " ".join(keys[i:j]) in self.exact:
                    exact.append((i, j))
        cands: list[tuple[float, int, int, str, str]] = []          # (score, i, j, to, rule)
        self._fold(text, toks, joined, guarded, cands)
        for n in range(1, MAX_N + 1):
            for i in range(len(toks) - n + 1):
                j = i + n
                span = toks[i:j]
                if not run_ok(i, j) or any(t.digit for t in span) or guarded(i, j):
                    continue
                if span[0].low in STOP or (span[-1].low in STOP and SUFFIX.get(span[-1].low, "") == ""):
                    continue
                if any(len(t.low) < 2 for t in span):
                    continue
                if n == 1 and (not span[0].text[0].isupper() or i == 0 or SENTENCE_END.search(text[:span[0].start])):
                    continue                             # one word: only a name as the engine wrote one
                if (i, j) in exact:
                    continue
                said = [t.low for t in span]
                tail = ""                                # a possessive kept as heard: "Exampleton's" is the town's
                if said[-1].endswith("'s") and len(said[-1]) > 3:
                    said[-1], tail = said[-1][:-2], "'s"
                got = self.best(tuple(said))
                if got == "exact":
                    exact.append((i, j))
                    continue
                if not got:
                    continue
                e, score, spell, sound = got  # type: ignore[misc]
                rule = "sound" if sound == 1.0 and spell < 1.0 else "spell"
                cands.append((score, i, j, e.written + tail, rule))
        taken: list[tuple[int, int, str, str, float]] = []
        for score, i, j, to, rule in sorted(cands, key=lambda c: (-c[0], -(c[2] - c[1]), c[1])):
            if any(i < tj and ti < j for ti, tj, *_ in taken):
                continue
            bad = False
            for ei, ej in exact:
                if i < ej and ei < j:                    # a span that is a term already: only kept inside this one's
                    inner = stt_terms.term_key(" ".join(t.text for t in toks[ei:ej]))
                    if not (i <= ei and ej <= j and f" {inner} " in f" {stt_terms.term_key(to)} "):
                        bad = True
                        break
            if bad:
                continue
            taken.append((i, j, to, rule, score))
        if not taken:
            return text, []
        out, fixes, pos = [], [], 0
        for i, j, to, rule, score in sorted(taken):
            s, e = toks[i].start, toks[j - 1].end
            out.append(text[pos:s])
            out.append(to)
            fixes.append(Fix(text[s:e], to, rule, round(score, 3), s))
            pos = e
        out.append(text[pos:])
        return "".join(out), fixes

    def _fold(self, text: str, toks: list[_Tok], joined: list[bool], guarded, cands: list) -> None:
        """Spoken numbers in a unit ID, a call sign or a ten-code, folded to the list's own term."""
        n = len(toks)

        def num_run(j: int) -> int:
            """The end of the run of number words from j (j itself: none)."""
            k = j
            while k < n and toks[k].low in NUMWORDS and k - j < MAX_RUN and (k == j or joined[k - 1]):
                k += 1
            return k

        def rest_ok(a: int, b: int) -> bool:            # what is left of the run: nothing, or the trailing time
            return a >= b or guarded(a, b)

        for i in range(n):
            for p in (3, 2, 1):                          # "Engine sixty", "Tower Ladder two"
                if i + p >= n or not all(joined[i:i + p]):
                    continue
                pre = tuple(t.low for t in toks[i:i + p])
                units = self.units.get(pre)
                if not units:
                    continue
                j = i + p
                if toks[j].low in ("oh", "o"):
                    continue
                k = num_run(j)
                for end in range(k, j, -1):
                    digits = stt_terms._digits([t.low for t in toks[j:end]])
                    if digits in units and rest_ok(end, k) and not guarded(i, end):
                        cands.append((1.0, i, end, units[digits], "unit"))
                        break
                break
            # a run of number words: a call sign after it ("sixty control"), or a ten-code ("ten four.")
            if toks[i].low in NUMWORDS and (i == 0 or not joined[i - 1] or toks[i - 1].low not in NUMWORDS) \
                    and toks[i].low not in ("oh", "o"):
                k = num_run(i)
                if self.tens and toks[i].low == "ten" and k - i >= 2 and not guarded(i, k):
                    w = stt_terms.written(" ".join(t.low for t in toks[i:k]))
                    if w in self.tens and STOP_AFTER.match(text, toks[k - 1].end):      # at a clause end only
                        cands.append((1.0, i, k, self.tens[w], "ten"))
                for post, signs in self.signs.items():
                    m = len(post)
                    if k + m <= n and all(joined[k - 1:k + m - 1]) and tuple(t.low for t in toks[k:k + m]) == post:
                        digits = stt_terms._digits([t.low for t in toks[i:k]])
                        if digits in signs and not guarded(i, k + m):
                            cands.append((1.0, i, k + m, signs[digits], "unit"))


class Corrector:
    """The hub's: each channel's Vocab from the term lists in use (terms() -> a stt_terms.TermStore, or None), which list
    a channel takes (own_for_key), the channels it is on and the towns whose streets it may correct into."""

    def __init__(self, terms: Callable[[], object], own_for_key: Callable[[str], str | None],
                 channels=DEFAULT_CHANNELS, towns=DEFAULT_TOWNS, log: Callable[[str], None] = print):
        self.terms, self.own_for_key, self.log = terms, own_for_key, log
        self.channels = frozenset(channels)
        self.towns = tuple(towns)
        self._vocab: dict[str, Vocab] = {}
        self._cache: OrderedDict[tuple, Result] = OrderedDict()
        self._lock = threading.Lock()
        self._failed = ""

    def applies(self, key: str) -> bool:
        return bool(key) and key in self.channels

    def warm(self, keys=()) -> None:
        """Each list's matcher built ahead (after a read of the lists, on its thread), for the channels it is on."""
        for key in dict.fromkeys(keys):
            try:
                self.vocab(key)
            except Exception as e:  # noqa: BLE001 - built when first asked instead
                self.log(f"stt correct: {type(e).__name__}")

    def vocab(self, key: str) -> Vocab | None:
        """The channel's Vocab (its own list, places woven in), None for none."""
        store = self.terms()
        if store is None or not hasattr(store, "get"):
            return None
        own = self.own_for_key(key)
        ts = store.get(own) if own else None
        if ts is None or not ts.terms:
            return None
        v = ts.vocab_v
        got = self._vocab.get(own)
        if got is not None and got.v == v:
            return got
        secs: dict[str, str] = {}
        allsecs = store.sections() if hasattr(store, "sections") else {}
        for name in (stt_terms.SHARED, own):
            secs.update(allsecs.get(name) or {})
        names = set(allsecs[stt_terms.SHARED]) if stt_terms.SHARED in allsecs else set()   # (places only)
        vb = Vocab(ts.terms, secs, self.towns, v, names)
        with self._lock:
            self._vocab[own] = vb
        self.log(f"stt correct: {own}: {len(vb.entries)} names, {sum(len(u) for u in vb.units.values())} unit IDs, "
                 f"{len(vb.tens)} ten-codes ({v})")
        return vb

    def correct(self, text: str, key: str) -> Result | None:
        """The correction of one transcript on its channel; None: not on (the channel's switch off, no list, no text)."""
        if not text or not self.applies(key):
            return None
        try:
            vb = self.vocab(key)
            if vb is None:
                return None
            ck = (vb.v, text)
            with self._lock:
                hit = self._cache.get(ck)
                if hit is not None:
                    self._cache.move_to_end(ck)
                    return hit
            fixed, fixes = vb.correct(text)
            res = Result(fixed if fixes and fixed != text else "", tuple(fixes), f"{RULE}@{vb.v}")
            with self._lock:
                self._cache[ck] = res
                if len(self._cache) > CACHE:
                    self._cache.popitem(last=False)
            return res
        except Exception as e:  # noqa: BLE001 - never in the way of a transcript (no text in the log)
            why = type(e).__name__
            if why != self._failed:
                self._failed = why
                self.log(f"stt correct: {why}")
            return None

    def row(self, r: dict) -> tuple[str, list[dict]]:
        """A Radio row's text as shown and its corrections ([{heard, to, rule}], the hover's), for
        sizeup/cad/statushints.py: (r["text"], []) when nothing applies."""
        text = r.get("text") or ""
        key = row_key(r)
        res = self.correct(text, key) if text else None
        if res is None or not res.fixes:
            return text, []
        return res.text, [{"heard": f.heard, "to": f.to, "rule": f.rule} for f in res.fixes]


def row_key(r: dict) -> str:
    """A radio log row's channel key (App.traffic_key's): its channel, "tg:<n>" for the county's talkgroup."""
    from sizeup import sourcekeys
    tg = r.get("talkgroup")
    if tg is None or tg == "":
        return str(r.get("channel") or "")
    try:
        return sourcekeys.channel_key(str(r.get("system") or ""), int(tg))
    except (TypeError, ValueError):
        return ""


def channels_of(items) -> tuple[str, ...]:
    """[stt] correct_channels as keys (stt_time.channels_of: a number is a county talkgroup)."""
    return stt_time.channels_of(items)
