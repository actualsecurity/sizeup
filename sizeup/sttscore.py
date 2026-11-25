"""Champion and challenger (#639 per-channel models): how a challenger model's transcript of a clip is scored against
the champion's, per domain, and how the samples add up (n, the mean of each side, the difference and its 95% interval).

    normalize()     NORM: the pinned folding both texts go through before anything is compared: lower case, spelled
                    numbers in digits as radio says them (sizeup/stt_terms.py written(): "sixty one" 61, "ten four"
                    10-4), punctuation out. A change to it is a new NORM (old and new scores never mix in one table).
    identifiers()   what matters in a transmission: unit IDs ("engine 60", "77 alpha 4"), ten-codes ("10-4"), numbers
                    of three digits or more (a box, an address, a callsign's number), callsigns as said ("delta 766").
    sample()        one clip's scores, per side, from what is known about it (refs, gathered when it was sampled):
                    fire/EMS   the dispatcher's trailing time (#727: read within the window: 1, a wrong time: 0),
                               the county CAD's units and address words found (of what either side found), people's edit
                    police     the channel's terms found (of what either side found), the CAD's, people's edit
                    air        the aircraft in ADS-B near the field found (of what either side found), people's edit
                    every      agreement (1 - word error between the two: shown, never ranked: it is the same for both)
                    and identifier recall against the edit, when there is one. A side's score: the mean of its
                    domain's metrics that apply to the clip; a clip where none applies isn't counted.
    compare()       the samples of a channel: n, each side's mean, the difference (challenger - champion) and its 95%
                    interval (normal: 1.96 sd / sqrt n), and a verdict: "challenger better" / "champion better" only
                    when the interval leaves 0 and n >= MIN_N; else "no difference yet".

Only numbers are kept for a sample (stt_runs.score, JSON): never the texts again, never the refs themselves."""

from __future__ import annotations

import json
import math
import re

from sizeup import stt_terms

NORM = "norm/1"
MIN_N = 20                  # samples before a verdict (and before a promotion's watch can roll it back)
ROLLBACK_MARGIN = 0.02      # a promoted model rolled back when it scores this much under the one it replaced (and the
                            # interval says so)
DOMAINS = {"fire": "fire", "ems": "fire", "police": "police", "air": "air"}
METRICS = {"fire": ("time", "cad", "edit", "ids"), "police": ("terms", "cad", "edit", "ids"),
           "air": ("aircraft", "edit", "ids"), "other": ("edit", "ids")}
UNIT_WORDS = ("engine", "ladder", "tower", "truck", "rescue", "squad", "battalion", "medic", "ambulance", "car",
              "unit", "utility", "marine", "chief", "control", "command")
PHONETIC = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo",
            "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform", "victor",
            "whiskey", "xray", "yankee", "zulu")
_TOKEN = re.compile(r"\d+(?:-\d+)?|[a-z]+")


def normalize(text: str) -> list[str]:
    """NORM's tokens of a text."""
    t = stt_terms.written(str(text or "")).lower().replace("x-ray", "xray")
    t = re.sub(r"(\d)\s*-\s*(\d)", r"\1-\2", t)
    return _TOKEN.findall(t)


def edits(ref: list, hyp: list) -> int:
    """Word-level edit distance."""
    if not ref:
        return len(hyp)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def wer(ref: str, hyp: str) -> float | None:
    """Word error rate of hyp against ref (NORM), None when ref has no words."""
    r, h = normalize(ref), normalize(hyp)
    return edits(r, h) / len(r) if r else None


def agreement(a: str, b: str) -> float | None:
    """1 - the edit distance over the longer of the two (symmetric), None when both are empty."""
    x, y = normalize(a), normalize(b)
    n = max(len(x), len(y))
    return round(1.0 - edits(x, y) / n, 4) if n else None


def identifiers(text: str) -> set[str]:
    """The identifiers in a text (NORM): unit IDs, phonetic IDs, ten-codes, numbers of three digits or more."""
    w = normalize(text)
    out: set[str] = set()
    for i, t in enumerate(w):
        if re.fullmatch(r"10-\d+", t):
            out.add(t)
        elif t.isdigit():
            if i > 0 and w[i - 1] in UNIT_WORDS:
                out.add(f"{w[i - 1]} {t}")
            elif i + 2 < len(w) and w[i + 1] in PHONETIC and w[i + 2].isdigit():
                out.add(f"{t} {w[i + 1]} {w[i + 2]}")
            elif len(t) >= 3:
                out.add(t)
    return out


def found(refs, text: str) -> set[str]:
    """The refs (phrases: a unit's spoken name, an address's words, a callsign as said, a term) said in the text:
    each ref's NORM tokens in a row in it; a ref with a number also counts when its number and the word before it
    are there (a callsign's airline said, its number heard)."""
    w = normalize(text)
    joined = f" {' '.join(w)} "
    hit: set[str] = set()
    for ref in refs:
        r = normalize(ref)
        if not r:
            continue
        if f" {' '.join(r)} " in joined:
            hit.add(" ".join(r))
    return hit


def _share(mine: set, other: set) -> float | None:
    both = mine | other
    return round(len(mine) / len(both), 4) if both else None


def time_score(check) -> float | None:
    """The dispatcher's trailing time (sizeup/stt_time.py Check): match 1, a wrong time 0, none read: n/a."""
    if check is None:
        return None
    return {"match": 1.0, "time_mismatch": 0.0}.get(getattr(check, "result", ""), None)


def side(domain: str, metrics: dict) -> float | None:
    """A side's score: the mean of its domain's metrics that apply."""
    vals = [metrics[m] for m in METRICS.get(domain, METRICS["other"]) if metrics.get(m) is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def sample(domain: str, champ: str, chall: str, refs: dict, checks: tuple = (None, None),
           edit: str | None = None) -> dict:
    """One clip's scores: {norm, domain, agree, champ: {metric: v, score}, chall: {...}}. refs: {cad: [...],
    aircraft: [...], terms: [...]} (what was known about the clip when it was sampled); checks: each side's trailing
    time check (fire dispatch); edit: a person's correction of the clip, when there is one."""
    domain = DOMAINS.get(domain, domain if domain in METRICS else "other")
    out = {"norm": NORM, "domain": domain, "agree": agreement(champ, chall), "champ": {}, "chall": {}}
    hits = {}
    for k in ("cad", "aircraft", "terms"):
        if refs.get(k):
            hits[k] = (found(refs[k], champ), found(refs[k], chall))
    for i, (name, text) in enumerate((("champ", champ), ("chall", chall))):
        m: dict = {}
        if domain == "fire":
            m["time"] = time_score(checks[i])
        for k, (a, b) in hits.items():
            mine, other = (a, b) if i == 0 else (b, a)
            m[k] = _share(mine, other)
        if edit:
            e = wer(edit, text)
            m["edit"] = None if e is None else round(max(0.0, 1.0 - e), 4)
            ids = identifiers(edit)
            m["ids"] = round(len(ids & identifiers(text)) / len(ids), 4) if ids else None
        m["score"] = side(domain, m)
        out[name] = m
    return out


def with_edit(s: dict, champ: str, chall: str, edit: str) -> dict:
    """A sample scored again once a person corrected its clip (the edit and identifier recall added)."""
    out = json.loads(json.dumps(s))
    for name, text in (("champ", champ), ("chall", chall)):
        m = out.get(name) or {}
        e = wer(edit, text)
        m["edit"] = None if e is None else round(max(0.0, 1.0 - e), 4)
        ids = identifiers(edit)
        m["ids"] = round(len(ids & identifiers(text)) / len(ids), 4) if ids else None
        m["score"] = side(out.get("domain", "other"), m)
        out[name] = m
    return out


def compare(samples: list[dict]) -> dict:
    """The samples of one channel and challenger (each sample()'s dict): n (both sides scored), each side's mean, the
    mean difference (challenger - champion) with its 95% half-interval, the verdict, and each metric's means."""
    pairs = [(s["champ"]["score"], s["chall"]["score"]) for s in samples
             if (s.get("champ") or {}).get("score") is not None and (s.get("chall") or {}).get("score") is not None]
    n = len(pairs)
    out: dict = {"n": n, "samples": len(samples), "champ": None, "chall": None, "diff": None, "ci": None,
                 "verdict": "no samples yet" if not samples else "no difference yet", "metrics": {}}
    agree = [s["agree"] for s in samples if isinstance(s.get("agree"), (int, float))]
    out["agree"] = round(sum(agree) / len(agree), 4) if agree else None
    for side_ in ("champ", "chall"):
        ms: dict = {}
        for s in samples:
            for k, v in (s.get(side_) or {}).items():
                if k != "score" and isinstance(v, (int, float)):
                    ms.setdefault(k, []).append(v)
        out["metrics"][side_] = {k: {"mean": round(sum(v) / len(v), 4), "n": len(v)} for k, v in ms.items()}
    if not n:
        return out
    a = sum(p[0] for p in pairs) / n
    b = sum(p[1] for p in pairs) / n
    d = [p[1] - p[0] for p in pairs]
    mean = sum(d) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in d) / (n - 1)) if n > 1 else 0.0
    ci = 1.96 * sd / math.sqrt(n) if n > 1 else None
    out.update(champ=round(a, 4), chall=round(b, 4), diff=round(mean, 4), ci=round(ci, 4) if ci is not None else None)
    if n >= MIN_N and ci is not None and abs(mean) > ci:
        out["verdict"] = "challenger better" if mean > 0 else "champion better"
    return out


def should_roll_back(cmp: dict) -> bool:
    """After a promotion the model it replaced runs as the challenger: roll back when it scores better by more than
    ROLLBACK_MARGIN, with enough samples and the interval saying so."""
    return (cmp.get("n", 0) >= MIN_N and cmp.get("diff") is not None and cmp.get("ci") is not None
            and cmp["diff"] > ROLLBACK_MARGIN and cmp["diff"] > cmp["ci"])


def sampled(clip_id: str, model: str, pct: float) -> bool:
    """This clip is one of the challenger's pct% (the same answer every time it is asked: a clip id's hash)."""
    import hashlib
    if pct <= 0:
        return False
    return int(hashlib.sha1(f"{clip_id}|{model}".encode()).hexdigest()[:8], 16) % 10000 < pct * 100
