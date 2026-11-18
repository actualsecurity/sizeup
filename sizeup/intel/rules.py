"""Rule packs (data/intel/*.json) with Settings over them, compiled once.

A pack says which radio sources it reads (scope), what its categories are and the phrase rules that point at them:

    {"name": "police", "version": 1, "domain": "police", "label": "PD",
     "scope": {"services": ["police"], "sources": ["p25:mrrs:*"], "tiers": [], "systems": []},
     "accept": false, "window_s": 120, "threshold": 0.6, "context_factor": 0.5, "mode": "push",
     "towns": {"p25:mrrs:9211": ["Port Chester", "Rye", "Rye Brook"]},
     "categories": {"fire_report": {"label": "Smoke / fire reported", "ttl_min": 45, "priority": 4, "mode": "push",
                                    "tag": "fire"}},
     "rules": [{"id": "pd.smoke_seen", "v": 1, "category": "fire_report", "w": 0.7,
                "any": ["smoke ... (from|coming|showing)"], "not": ["no smoke"]}],
     "suppress": ["disregard", "training"]}

Phrases: words in order, case and punctuation aside; "..." a gap of up to GAP_WORDS words with no "no" / "not" in it
(sizeup/cad/alarms.py's grammar); "(a|b|c d)" one of the alternatives, each one or more words. A rule hits a text
when any of its phrases is in it and none of its "not" phrases is. "re:" in front: a regular expression instead
(at most RE_MAX characters, case-insensitive), for what words can't say ("10-\\d\\d"). An expression must not backtrack
without bound: its separator and word classes share no character ("[^\\w'.?!]+" beside "[\\w']+"; never "\\W+"
beside "[\\w']+", which both take an apostrophe), and each character of a run is taken by one alternative only (the
police pack's ellipsis, "\\.(?=\\.)|(?<=\\.)\\.(?!\\.)": a dot beside a dot, never a sentence's end).
tests/test_intel_packs.py times every pack's expressions on hostile texts, and an expression reads only the first
RE_TEXT_MAX characters.

Scope never names a bare talkgroup or callsign number: sources are source keys (sizeup/sourcekeys.py) or globs of them.
"exclude" (source keys or globs) takes sources back out: neighbor_police reads every MRRS police talkgroup except the
police pack's own (Port Chester / Rye / Rye Brook PD), with the police pack's rules ("rules_from": rules_from()).

Speech-to-text tolerance (a pack with "normalize": "<table>", data/intel/tables/<table>.json): the table's fixes
(Normalizer: sound-alikes such as "order of gas" -> "odor of gas", split and merged words, number words as digits) make
a second READING of a transmission. Each reading is judged on its own: a rule hits when, in the words as transcribed or
in the fixed words, one of its phrases is found and none of its "not" phrases is. So the rules a table makes hit are
always a superset of those hit without it (a fix can add a hit, never take one away), and a "not" phrase still guards
the reading it was written for. Write phrases in the table's spelling ("2nd alarm", "odor of gas"). A fix may not take
a negation away ("zero" -> "0" is refused: a negator before a gap must stay one). A "not" phrase must already be in the
table's spelling, every alternative with its fixed form beside it ("(no one|no 1|nobody) inside"): checked when the
pack compiles, since a "not" written only as heard would miss the fixed reading and let a hit through. A pack
without "normalize" matches exactly as before. Suppress phrases are tried on both readings (a drill said either way is a drill).

"area": the towns a county-wide pack's events belong to (Settings: intel_packs.<pack>.area; an empty list there is
the file's). With an area, the address lookup searches those towns (and the towns said), and an event placed outside
them, or whose words name only towns outside them, is out of area: kept at log, never raised (engine.py). No area
(the police pack, whose sources are local): every place counts, as before.

A category with "place": true (the police pack's fire_call) is held at log until its event has a place a crew can
drive to (an address, intersection, block or landmark) in the source's towns ("towns"), else the pack's area: a
burning smell with no address is no pre-alert yet; the read-back that places it lets it go (engine.py: only its own
words, or a read-back by the call's talker or units within minutes). "words": the words that tie a later transmission
to a category's event (default: every word of four letters or more in its rules' phrases). "defer_to": another
category of the pack that wins when both would raise for one call (fire_call defers to fire_report). "deescalate":
phrases (the grammar above) that mark the category's open event on the same source as "de-escalated" (the weapons
category: "the gun was secured", "the firearm has been recovered"): a note on the event, never a push and never an end
(engine.py _deescalate). "escalate" / "escalate_not": what says the category is live again after that note (the weapons
category: "pulled out a gun", "shots fired"; never with "in custody", "for the report", "when we arrived"): such a row
re-opens the event and pushes it again; any other row that says the category only takes the note off;
"escalate_always" ("shots fired") does so whatever "escalate_not" says. "cancel_ttl_min": a safety category of a police
pack that a nameless disregard marks "possibly cancelled" lives at most this long after it, and a second disregard or one
of the pack's "resolved" phrases ("unfounded", "the caller called back") ends it. A rule's "unless" phrases set its
"not" phrases aside ("pointed a gun at" beside "a bat in his bedroom").

"order": where the pack is listed (lowest first, then by file name; 0 when absent). "infer": false: the model behind
the rules (infer.py) is never asked about this pack's rows. A rule or a category with no "mode" is at "push": a pack
that must stay quiet writes "mode": "log" on the pack, every category and every rule (the engine takes the lowest).

Settings (sections intel_packs / intel_rules, sizeup/intel/settings.py) lay over the files: a pack's mode, threshold,
window; a category's mode and priority; a rule's mode, weight, on/off. load_packs() is pure (files or dicts in,
packs out), so replay can compare two rule sets (--diff A B) without a database.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from sizeup.intel.model import MODES

DATA = Path(__file__).resolve().parents[2] / "data" / "intel"
TABLES = DATA / "tables"        # not packs (read_files reads data/intel/*.json only): the normalisation tables
GAP = "..."
GAP_WORDS = 6
NEGATORS = frozenset({"no", "not", "none", "nobody", "nothing", "negative", "zero", "never"})
TOKEN = re.compile(r"(?:[A-Za-z]\.){2,}|[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*")
RE_MAX = 300
RE_TEXT_MAX = 4000              # a "re:" expression reads at most this many characters of a transmission (minutes of
                                # speech): bounds the time an expression with backtracking can take on a freak text
PHRASE_MAX = 120
RULES_MAX = 400
WILD = "*"                      # a rule for every category (a dispatch cue): counts only beside a real hit
NORM_SECTIONS = ("split", "sound", "numbers")
NORM_MAX = 2000                 # entries in one table
NORM_WORDS = 6                  # words in one entry's key or value


def words(text: str) -> list[str]:
    return [m.group(0).replace(".", "").replace("’", "'").lower() for m in TOKEN.finditer(text or "")]


class PhraseError(ValueError):
    pass


def _items(phrase: str) -> list:
    """A phrase as items: GAP, or a tuple of alternatives (each a tuple of words)."""
    p = str(phrase or "").replace("…", f" {GAP} ").strip()
    if not p:
        raise PhraseError("an empty phrase")
    if len(p) > PHRASE_MAX:
        raise PhraseError(f"phrase {p[:30]!r}: at most {PHRASE_MAX} characters")
    out: list = []
    for m in re.finditer(r"\(([^()]*)\)|(\.\.\.)|([^\s()]+)", p):
        if m.group(1) is not None:
            alts = tuple(tuple(words(a)) for a in m.group(1).split("|"))
            if not alts or any(not a for a in alts):
                raise PhraseError(f"phrase {p!r}: an empty alternative")
            out.append(alts)
        elif m.group(2):
            out.append(GAP)
        else:
            out += [((w,),) for w in words(m.group(3))]
    if "(" in re.sub(r"\([^()]*\)", "", p) or ")" in re.sub(r"\([^()]*\)", "", p):
        raise PhraseError(f"phrase {p!r}: unbalanced ( )")
    if not out or out[0] == GAP or out[-1] == GAP or any(a == b == GAP for a, b in zip(out, out[1:])):
        raise PhraseError(f"phrase {p!r}: \"...\" goes between two words")
    return out


def _match_at(ws: list[str], i: int, items: list) -> bool:
    if not items:
        return True
    it = items[0]
    if it == GAP:
        for g in range(GAP_WORDS + 1):
            if i + g > len(ws) or (g and ws[i + g - 1] in NEGATORS):
                return False
            if _match_at(ws, i + g, items[1:]):
                return True
        return False
    for alt in it:
        n = len(alt)
        if tuple(ws[i:i + n]) == alt and _match_at(ws, i + n, items[1:]):
            return True
    return False


class Normalizer:
    """A table of speech-to-text fixes, as data (data/intel/tables/<name>.json):

        {"version": 1,
         "split":   {"roll over": "rollover", "carbonmonoxide": "carbon monoxide"},     split or merged words
         "sound":   {"order of gas": "odor of gas"},                                    sound-alikes, with their context
         "numbers": {"two": "2", "second": "2nd"}}                                      number words as digits

    Keys and values are words (case and punctuation aside, as words() reads them), at most NORM_WORDS each. apply() is
    one pass over a transmission's words, left to right, the longest key first; what a fix wrote is never fixed again.
    It only ever adds a second reading, each judged on its own (Rule.hit): a table can't take a hit away, and a "not"
    phrase is checked against the reading its rule's phrase was found in. An entry whose heard words hold a negation
    word (NEGATORS) and whose meaning has none is refused: a fix must never turn "zero injuries" into a number."""

    def __init__(self, table: dict, name: str = ""):
        self.name = str(name or table.get("name") or "")
        self.version = int(table.get("version") or 1)
        self.map: dict[tuple, tuple] = {}
        for sec in NORM_SECTIONS:
            d = table.get(sec) or {}
            if not isinstance(d, dict):
                raise ValueError(f"table {self.name}.{sec}: an object of \"heard\": \"meant\"")
            for k, v in d.items():
                kw, vw = tuple(words(str(k))), tuple(words(str(v)))
                if not kw or not vw or len(kw) > NORM_WORDS or len(vw) > NORM_WORDS:
                    raise ValueError(f"table {self.name}.{sec} {str(k)[:30]!r}: 1 to {NORM_WORDS} words each side")
                if set(kw) & NEGATORS and not set(vw) & NEGATORS:
                    raise ValueError(f"table {self.name}.{sec} {' '.join(kw)!r}: takes a negation away")
                if kw in self.map and self.map[kw] != vw:
                    raise ValueError(f"table {self.name}: {' '.join(kw)!r} twice, with two meanings")
                if kw != vw:
                    self.map[kw] = vw
        if len(self.map) > NORM_MAX:
            raise ValueError(f"table {self.name}: at most {NORM_MAX} entries")
        self.longest = max((len(k) for k in self.map), default=0)
        self.first = frozenset(k[0] for k in self.map)
        self.digest = hashlib.sha256(json.dumps(sorted(self.map.items())).encode()).hexdigest()[:12]
        self._last: tuple = ((), None)

    def apply(self, ws: list[str]) -> list[str] | None:
        """The words with the table's fixes, or None when it changes nothing (the usual case: no second reading)."""
        key = tuple(ws)
        last = self._last                      # (read once: another thread may compile and ask too)
        if last[0] == key:                     # every rule of a pack asks about the same transmission
            return last[1]
        out: list[str] = []
        i, n, changed = 0, len(key), False
        while i < n:
            if key[i] in self.first:
                for size in range(min(self.longest, n - i), 0, -1):
                    got = self.map.get(key[i:i + size])
                    if got is not None:
                        out += got
                        i += size
                        changed = True
                        break
                else:
                    out.append(key[i])
                    i += 1
            else:
                out.append(key[i])
                i += 1
        res = out if changed else None
        self._last = (key, res)
        return res


@lru_cache(maxsize=8)
def table(name: str) -> Normalizer:
    """data/intel/tables/<name>.json, compiled (ValueError with the reason)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", str(name)):
        raise ValueError(f"normalize {name!r}: a table's name (lower-case letters, digits, _)")
    try:
        return Normalizer(json.loads((TABLES / f"{name}.json").read_text()), name)
    except OSError:
        raise ValueError(f"normalize {name!r}: no data/intel/tables/{name}.json") from None


def readings(text: str, ws: list[str], norm: "Normalizer | None") -> list[tuple[str, list[str]]]:
    """The transmission as transcribed, and after the pack's table when it changes anything."""
    alt = norm.apply(ws) if norm is not None else None
    return [(text, ws)] if alt is None else [(text, ws), (" ".join(alt), alt)]


def _found(p: "Phrase", text: str, ws: list[str], norm: Normalizer | None) -> bool:
    """The phrase in either reading (suppress phrases)."""
    return any(p.found(t, w) for t, w in readings(text, ws, norm))


class Phrase:
    """One compiled phrase (or "re:" expression)."""

    def __init__(self, src: str):
        self.src = str(src)
        if self.src.startswith("re:"):
            body = self.src[3:]
            if not body or len(body) > RE_MAX:
                raise PhraseError(f"expression: 1 to {RE_MAX} characters")
            try:
                self.rx = re.compile(body, re.I)
            except re.error as e:
                raise PhraseError(f"expression {body[:30]!r}: {e}") from None
            self.items = None
        else:
            self.rx = None
            self.items = _items(self.src)

    def found(self, text: str, ws: list[str]) -> bool:
        if self.rx is not None:
            return self.rx.search((text or "")[:RE_TEXT_MAX]) is not None
        return any(_match_at(ws, i, self.items) for i in range(len(ws)))


@dataclass
class Rule:
    id: str
    v: int
    category: str
    w: float
    any: list
    nots: list = field(default_factory=list)
    mode: str = "push"                # caps what this rule raises (shadow: counted, never scored)
    enabled: bool = True
    src: dict = field(default_factory=dict)
    norm: Normalizer | None = None    # the pack's table (None: the words as transcribed only)
    unless: list = field(default_factory=list)   # phrases that set the "not" phrases aside ("pointed a gun at")

    def hit(self, text: str, ws: list[str]) -> bool:
        """One reading where a phrase is found and no "not" phrase is, unless an "unless" phrase is (see the module's
        doc)."""
        if not self.enabled:
            return False
        return any(any(p.found(t, w) for p in self.any)
                   and (not any(p.found(t, w) for p in self.nots) or any(p.found(t, w) for p in self.unless))
                   for t, w in readings(text, ws, self.norm))


@dataclass
class Pack:
    name: str
    version: int
    domain: str
    label: str
    scope: dict
    accept: bool
    window_s: float
    threshold: float
    context_factor: float
    mode: str
    towns: dict
    categories: dict
    rules: list
    suppress: list
    src: dict = field(default_factory=dict)
    norm: Normalizer | None = None
    area: tuple = ()                  # the towns its events belong to (empty: anywhere)
    calm: dict = field(default_factory=dict)    # category -> its "deescalate" phrases, compiled
    rise: dict = field(default_factory=dict)    # category -> ("escalate", "escalate_not", "escalate_always"), compiled
    resolved: list = field(default_factory=list)   # phrases that end a "possibly cancelled" call with "cancel_ttl_min"

    def in_scope(self, row: dict) -> bool:
        """The row is one this pack reads: its service, its source key (a glob), its tier and system, as listed (an
        empty list: any), and its source key none of "exclude" (globs). Never a bare talkgroup number."""
        sc = self.scope or {}
        key = str(row.get("source") or row.get("source_key") or "")
        if sc.get("services") and str(row.get("service") or "") not in sc["services"]:
            return False
        if sc.get("sources") and not any(fnmatch.fnmatchcase(key, g) for g in sc["sources"]):
            return False
        if any(fnmatch.fnmatchcase(key, g) for g in sc.get("exclude") or ()):
            return False
        if sc.get("tiers") and str(row.get("tier") or "") not in sc["tiers"]:
            return False
        if sc.get("systems") and str(row.get("system") or "") not in sc["systems"]:
            return False
        return bool(key)

    def towns_for(self, source_key: str) -> list[str]:
        for g, towns in (self.towns or {}).items():
            if fnmatch.fnmatchcase(source_key, g):
                return list(towns)
        return []

    def category(self, name: str) -> dict:
        return self.categories.get(name) or {}

    def suppressed(self, text: str, ws: list[str]) -> bool:
        return any(_found(p, text, ws, self.norm) for p in self.suppress)

    def deescalated(self, text: str, ws: list[str]) -> list[str]:
        """The categories one of whose "deescalate" phrases the text says (either reading)."""
        return [c for c, ps in self.calm.items() if any(_found(p, text, ws, self.norm) for p in ps)]

    def escalates(self, cat: str, text: str, ws: list[str]) -> bool:
        """The text says the category is live again ("escalate", and none of "escalate_not")."""
        yes, no, always = self.rise.get(cat) or ((), (), ())
        if any(_found(p, text, ws, self.norm) for p in always):
            return True
        return (any(_found(p, text, ws, self.norm) for p in yes)
                and not any(_found(p, text, ws, self.norm) for p in no))

    def resolves(self, text: str, ws: list[str]) -> bool:
        """The text says a call is over ("resolved": "unfounded", "the caller called back")."""
        return any(_found(p, text, ws, self.norm) for p in self.resolved)

    def is_safety(self, cat: str) -> bool:
        return bool(self.category(cat).get("safety"))


def town_names() -> dict[str, str]:
    """{lower-case name: name as shown} of every town an area may name (data/westchester_towns.txt, the CAD's list)."""
    from sizeup.cad.sources import county_towns
    return {v.lower(): v for v in county_towns().values() if v}


def area(v, what: str) -> tuple:
    """A list of town names as an area (ValueError with the reason): each one of the county's towns."""
    if v is None:
        return ()
    if not isinstance(v, (list, tuple)) or len(v) > 40:
        raise ValueError(f"{what}: a list of at most 40 town names")
    known = town_names()
    out = []
    for t in v:
        name = known.get(str(t).strip().lower())
        if name is None:
            raise ValueError(f"{what}: {str(t)[:40]!r} isn't a town of data/westchester_towns.txt")
        if name not in out:
            out.append(name)
    return tuple(out)


def check_spelling(p: "Phrase", norm: "Normalizer", what: str) -> None:
    """A "not" phrase is in the table's spelling (ValueError naming the form to add): each alternative's fixed form is
    one of its alternatives too, and so is the phrase's own fixed form when it is words alone."""
    if p.items is None:
        return
    for item in p.items:
        if item == GAP:
            continue
        for alt in item:
            fixed = norm.apply(list(alt))
            if fixed is not None and tuple(fixed) not in item:
                raise ValueError(f"{what} {p.src!r}: add {' '.join(fixed)!r} beside {' '.join(alt)!r} (the table's spelling)")
    if all(item != GAP and len(item) == 1 for item in p.items):
        ws = [w for item in p.items for w in item[0]]
        fixed = norm.apply(ws)
        if fixed is not None and fixed != ws:
            raise ValueError(f"{what} {p.src!r}: write it as {' '.join(fixed)!r} (the table's spelling)")


def _num(v, lo: float, hi: float, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
        raise ValueError(f"{what}: a number from {lo:g} to {hi:g}")
    return float(v)


def _mode(v, what: str) -> str:
    if v not in MODES:
        raise ValueError(f"{what}: one of {', '.join(MODES)}")
    return v


def _phrases(xs, what: str) -> list[Phrase]:
    if not isinstance(xs, list):
        raise ValueError(f"{what}: a list of phrases")
    try:
        return [Phrase(x) for x in xs if str(x).strip()]
    except PhraseError as e:
        raise ValueError(f"{what}: {e}") from None


def compile_pack(d: dict, packs_ov: dict | None = None, rules_ov: dict | None = None) -> Pack:
    """One pack from its file's dict and the Settings over it (ValueError with the reason)."""
    name = str(d.get("name") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
        raise ValueError(f"pack name {name!r}: lower-case letters, digits, _")
    po = (packs_ov or {}).get(name) or {}
    ro = rules_ov or {}
    norm = table(str(d["normalize"])) if d.get("normalize") else None
    cats: dict = {}
    for c, cd in (d.get("categories") or {}).items():
        cd = dict(cd or {})
        cd.update({k: v for k, v in ((po.get("categories") or {}).get(c) or {}).items() if k in ("mode", "priority", "ttl_min")})
        cats[c] = {"label": str(cd.get("label") or c.replace("_", " ").capitalize()),
                   "ttl_min": _num(cd.get("ttl_min", 45), 1, 24 * 60, f"{name}.{c}.ttl_min"),
                   "priority": int(_num(cd.get("priority", 3), 1, 5, f"{name}.{c}.priority")),
                   "mode": _mode(cd.get("mode", "push"), f"{name}.{c}.mode"),
                   "tag": str(cd.get("tag") or ""), "safety": bool(cd.get("safety")), "moves": bool(cd.get("moves")),
                   "place": bool(cd.get("place")), "defer_to": str(cd.get("defer_to") or ""),
                   "words": [w for x in (cd.get("words") or []) for w in words(str(x))],
                   "deescalate": [p.src for p in _phrases(cd.get("deescalate") or [], f"{name}.{c}.deescalate")],
                   "escalate": [p.src for p in _phrases(cd.get("escalate") or [], f"{name}.{c}.escalate")],
                   "escalate_not": [p.src for p in _phrases(cd.get("escalate_not") or [], f"{name}.{c}.escalate_not")],
                   "escalate_always": [p.src for p in _phrases(cd.get("escalate_always") or [], f"{name}.{c}.escalate_always")],
                   "cancel_ttl_min": (_num(cd["cancel_ttl_min"], 1, 24 * 60, f"{name}.{c}.cancel_ttl_min")
                                      if cd.get("cancel_ttl_min") is not None else None),
                   "threshold": (_num(cd["threshold"], 0, 1, f"{name}.{c}.threshold") if cd.get("threshold") is not None else None)}
    for c, cd in cats.items():
        if cd["defer_to"] and (cd["defer_to"] not in cats or cd["defer_to"] == c):
            raise ValueError(f"{name}.{c}.defer_to: another of {name}'s categories")
    rules = []
    seen = set()
    for r in d.get("rules") or []:
        rid = str(r.get("id") or "")
        if not rid or rid in seen:
            raise ValueError(f"{name}: a rule with no id, or {rid!r} twice")
        seen.add(rid)
        o = ro.get(rid) or {}
        cat = str(r.get("category") or "")
        if cat != WILD and cat not in cats and cat != "routine":
            raise ValueError(f"rule {rid}: category {cat!r} isn't one of {name}'s")
        rules.append(Rule(rid, int(r.get("v") or 1), cat, _num(o.get("w", r.get("w", 0.5)), -1, 1, f"rule {rid} weight"),
                          _phrases(r.get("any") or [], f"rule {rid}"), _phrases(r.get("not") or [], f"rule {rid} not"),
                          _mode(o.get("mode", r.get("mode", "push")), f"rule {rid} mode"),
                          bool(o.get("enabled", r.get("enabled", True))), dict(r), norm,
                          _phrases(r.get("unless") or [], f"rule {rid} unless")))
    for rid, o in ro.items():                           # an admin's own rule (local.*)
        if rid.startswith("local.") and isinstance(o, dict) and o.get("pack") == name and rid not in seen:
            cat = str(o.get("category") or "")
            if cat not in cats:
                continue
            rules.append(Rule(rid, int(o.get("v") or 1), cat, _num(o.get("w", 0.5), -1, 1, f"rule {rid} weight"),
                              _phrases(o.get("any") or [], f"rule {rid}"), _phrases(o.get("not") or [], f"rule {rid} not"),
                              _mode(o.get("mode", "shadow"), f"rule {rid} mode"), bool(o.get("enabled", True)), dict(o),
                              norm))
    if norm is not None:                                # a "not" in heard spelling would miss the fixed reading
        for r in rules:
            for p in r.nots:
                check_spelling(p, norm, f"rule {r.id} not")
    if len(rules) > RULES_MAX:
        raise ValueError(f"{name}: at most {RULES_MAX} rules")
    return Pack(name=name, version=int(d.get("version") or 1), domain=str(d.get("domain") or name),
                label=str(d.get("label") or name.upper()), scope=dict(d.get("scope") or {}), accept=bool(d.get("accept")),
                window_s=_num(po.get("window_s", d.get("window_s", 120)), 10, 1800, f"{name}.window_s"),
                threshold=_num(po.get("threshold", d.get("threshold", 0.6)), 0, 1, f"{name}.threshold"),
                context_factor=_num(d.get("context_factor", 0.5), 0, 1, f"{name}.context_factor"),
                mode=_mode(po.get("mode", d.get("mode", "push")), f"{name}.mode"),
                towns=dict(d.get("towns") or {}), categories=cats, rules=rules,
                suppress=_phrases(d.get("suppress") or [], f"{name}.suppress"), src=dict(d), norm=norm,
                area=area(po.get("area") or d.get("area"), f"{name}.area"),
                calm={c: [Phrase(x) for x in cd["deescalate"]] for c, cd in cats.items() if cd["deescalate"]},
                rise={c: ([Phrase(x) for x in cd["escalate"]], [Phrase(x) for x in cd["escalate_not"]],
                          [Phrase(x) for x in cd["escalate_always"]])
                      for c, cd in cats.items() if cd["escalate"] or cd["escalate_always"]},
                resolved=_phrases(d.get("resolved") or [], f"{name}.resolved"))


def read_files(paths=None) -> list[dict]:
    """The pack files (data/intel/*.json by default), as dicts: by their "order" (lowest first), then by file name."""
    given = paths is not None
    if paths is None:
        paths = sorted(DATA.glob("*.json"))
    out = []
    for p in paths:
        out.append(json.loads(Path(p).read_text()))
    if not given:
        out.sort(key=lambda d: d.get("order") or 0)      # stable: the file names' order within one "order"
    return out


INHERITED = ("suppress", "normalize", "threshold", "window_s", "context_factor")


def rules_from(d: dict, others: dict) -> dict:
    """A pack with "rules_from": "<pack>" reads that pack's categories, rules and suppress phrases (and its normalize,
    threshold, window_s and context_factor, unless it says its own) as written in that pack's file: one list of rules,
    kept in one file, for two scopes (the police pack and neighbor_police: the same rules, another mode). Its rules'
    ids are "rule_prefix" (default "<its name>.") + theirs ("area.pd.barricade"), so Settings tune each pack's rules on
    their own (intel_rules is keyed by rule id) and the counts per rule stay apart; its categories' labels are
    "<its label>: <theirs>" (the Settings card names each category's control by its label; a push and the card show
    them without the prefix, push.category_label). The other pack's Settings
    and its admin's local.* rules are not taken. ValueError with the reason."""
    src = d.get("rules_from")
    if not src:
        return d
    name = str(d.get("name") or "")
    if not isinstance(src, str):
        raise ValueError(f"{name}.rules_from: a pack's name")
    base = others.get(src)
    if base is None and re.fullmatch(r"[a-z][a-z0-9_]{0,23}", str(src)) and (DATA / f"{src}.json").is_file():
        base = json.loads((DATA / f"{src}.json").read_text())
    if base is None or src == name:
        raise ValueError(f"{name}.rules_from: another pack's name ({src!r} isn't one)")
    if base.get("rules_from"):
        raise ValueError(f"{name}.rules_from: {src} takes its rules from another pack itself")
    if "rules" in d or "categories" in d:
        raise ValueError(f"{name}: rules_from, or rules and categories of its own, not both")
    prefix = str(d.get("rule_prefix") or f"{name}.")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}\.", prefix):
        raise ValueError(f"{name}.rule_prefix {prefix!r}: lower-case letters, digits, _, then a dot")
    out = dict(d)
    for k in INHERITED:
        if k not in d and k in base:
            out[k] = json.loads(json.dumps(base[k]))
    label = str(d.get("label") or name.upper())
    out["categories"] = {c: {**(cd or {}), "label": f"{label}: {(cd or {}).get('label') or c.replace('_', ' ').capitalize()}"}
                         for c, cd in (base.get("categories") or {}).items()}
    out["rules"] = [{**r, "id": prefix + str(r.get("id") or "")} for r in base.get("rules") or []]
    return out


def load_packs(paths_or_dicts=None, overrides: dict | None = None) -> list[Pack]:
    """Packs from files or dicts (None: data/intel/*.json) with Settings' overrides over them:
    {"intel_packs": {pack: {...}}, "intel_rules": {rule id: {...}}} (the other sections are ignored). Pure (a pack
    whose "rules_from" isn't among those given reads that pack's file)."""
    ov = overrides or {}
    srcs = []
    for x in (read_files() if paths_or_dicts is None else paths_or_dicts):
        srcs.append(x if isinstance(x, dict) else json.loads(Path(x).read_text()))
    by_name = {str(d.get("name") or ""): d for d in srcs}
    return [compile_pack(rules_from(d, by_name), ov.get("intel_packs") or {}, ov.get("intel_rules") or {}) for d in srcs]


def ruleset_hash(packs: list[Pack]) -> str:
    """A short hash of everything that decides a hit (the packs' sources as compiled, with their overrides)."""
    blob = json.dumps([[p.name, p.version, p.mode, p.threshold, p.window_s, p.categories, p.context_factor, p.towns,
                        p.scope, list(p.area),
                        [[r.id, r.v, r.w, r.mode, r.enabled, r.category, [x.src for x in r.any], [x.src for x in r.nots],
                          *([[x.src for x in r.unless]] if r.unless else [])]
                         for r in p.rules], [x.src for x in p.suppress],
                        *([[x.src for x in p.resolved]] if p.resolved else []),
                        *([[p.norm.name, p.norm.digest]] if p.norm is not None else [])] for p in packs],
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
