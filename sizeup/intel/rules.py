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
(at most RE_MAX characters, case-insensitive), for what words can't say ("10-\\d\\d").

Scope never names a bare talkgroup or callsign number: sources are source keys (sizeup/sourcekeys.py) or globs of them.

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
from pathlib import Path

from sizeup.intel.model import MODES

DATA = Path(__file__).resolve().parents[2] / "data" / "intel"
GAP = "..."
GAP_WORDS = 6
NEGATORS = frozenset({"no", "not", "none", "nobody", "nothing", "negative", "zero", "never"})
TOKEN = re.compile(r"(?:[A-Za-z]\.){2,}|[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*")
RE_MAX = 200
PHRASE_MAX = 120
RULES_MAX = 400
WILD = "*"                      # a rule for every category (a dispatch cue): counts only beside a real hit


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
            return self.rx.search(text or "") is not None
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

    def hit(self, text: str, ws: list[str]) -> bool:
        return self.enabled and any(p.found(text, ws) for p in self.any) and not any(p.found(text, ws) for p in self.nots)


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

    def in_scope(self, row: dict) -> bool:
        """The row is one this pack reads: its service, its source key (a glob), its tier and system, as listed (an
        empty list: any). Never a bare talkgroup number."""
        sc = self.scope or {}
        key = str(row.get("source") or row.get("source_key") or "")
        if sc.get("services") and str(row.get("service") or "") not in sc["services"]:
            return False
        if sc.get("sources") and not any(fnmatch.fnmatchcase(key, g) for g in sc["sources"]):
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
        return any(p.found(text, ws) for p in self.suppress)


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
    cats: dict = {}
    for c, cd in (d.get("categories") or {}).items():
        cd = dict(cd or {})
        cd.update({k: v for k, v in ((po.get("categories") or {}).get(c) or {}).items() if k in ("mode", "priority", "ttl_min")})
        cats[c] = {"label": str(cd.get("label") or c.replace("_", " ").capitalize()),
                   "ttl_min": _num(cd.get("ttl_min", 45), 1, 24 * 60, f"{name}.{c}.ttl_min"),
                   "priority": int(_num(cd.get("priority", 3), 1, 5, f"{name}.{c}.priority")),
                   "mode": _mode(cd.get("mode", "push"), f"{name}.{c}.mode"),
                   "tag": str(cd.get("tag") or ""), "safety": bool(cd.get("safety")),
                   "threshold": (_num(cd["threshold"], 0, 1, f"{name}.{c}.threshold") if cd.get("threshold") is not None else None)}
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
                          bool(o.get("enabled", r.get("enabled", True))), dict(r)))
    for rid, o in ro.items():                           # an admin's own rule (local.*)
        if rid.startswith("local.") and isinstance(o, dict) and o.get("pack") == name and rid not in seen:
            cat = str(o.get("category") or "")
            if cat not in cats:
                continue
            rules.append(Rule(rid, int(o.get("v") or 1), cat, _num(o.get("w", 0.5), -1, 1, f"rule {rid} weight"),
                              _phrases(o.get("any") or [], f"rule {rid}"), _phrases(o.get("not") or [], f"rule {rid} not"),
                              _mode(o.get("mode", "shadow"), f"rule {rid} mode"), bool(o.get("enabled", True)), dict(o)))
    if len(rules) > RULES_MAX:
        raise ValueError(f"{name}: at most {RULES_MAX} rules")
    return Pack(name=name, version=int(d.get("version") or 1), domain=str(d.get("domain") or name),
                label=str(d.get("label") or name.upper()), scope=dict(d.get("scope") or {}), accept=bool(d.get("accept")),
                window_s=_num(po.get("window_s", d.get("window_s", 120)), 10, 1800, f"{name}.window_s"),
                threshold=_num(po.get("threshold", d.get("threshold", 0.6)), 0, 1, f"{name}.threshold"),
                context_factor=_num(d.get("context_factor", 0.5), 0, 1, f"{name}.context_factor"),
                mode=_mode(po.get("mode", d.get("mode", "push")), f"{name}.mode"),
                towns=dict(d.get("towns") or {}), categories=cats, rules=rules,
                suppress=_phrases(d.get("suppress") or [], f"{name}.suppress"), src=dict(d))


def read_files(paths=None) -> list[dict]:
    """The pack files (data/intel/*.json by default), as dicts."""
    if paths is None:
        paths = sorted(DATA.glob("*.json"))
    out = []
    for p in paths:
        out.append(json.loads(Path(p).read_text()))
    return out


def load_packs(paths_or_dicts=None, overrides: dict | None = None) -> list[Pack]:
    """Packs from files or dicts (None: data/intel/*.json) with Settings' overrides over them:
    {"intel_packs": {pack: {...}}, "intel_rules": {rule id: {...}}} (the other sections are ignored). Pure."""
    ov = overrides or {}
    srcs = []
    for x in (read_files() if paths_or_dicts is None else paths_or_dicts):
        srcs.append(x if isinstance(x, dict) else json.loads(Path(x).read_text()))
    return [compile_pack(d, ov.get("intel_packs") or {}, ov.get("intel_rules") or {}) for d in srcs]


def ruleset_hash(packs: list[Pack]) -> str:
    """A short hash of everything that decides a hit (the packs' sources as compiled, with their overrides)."""
    blob = json.dumps([[p.name, p.version, p.mode, p.threshold, p.window_s, p.categories, p.context_factor, p.towns,
                        p.scope,
                        [[r.id, r.v, r.w, r.mode, r.enabled, r.category, [x.src for x in r.any], [x.src for x in r.nots]]
                         for r in p.rules], [x.src for x in p.suppress]] for p in packs], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
