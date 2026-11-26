"""Champion and challenger (#639 per-channel models): how a challenger model's transcript of a clip is scored against
the champion's, per domain, and how the samples add up (n, the mean of each side, the difference and its 95% interval).

    normalize()     NORM: the pinned folding both texts go through before anything is compared: lower case, spelled
                    numbers in digits as radio says them (sizeup/stt_terms.py written(): "sixty one" 61, "ten four"
                    10-4), street suffixes in full ("Rd" road), punctuation out. A change to it (or to written()'s
                    number words) is a new NORM, and the board compares one NORM's samples only.
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

SUFFIXES = {"rd": "road", "st": "street", "ave": "avenue", "av": "avenue", "blvd": "boulevard", "ln": "lane",
            "dr": "drive", "pl": "place", "ct": "court", "hwy": "highway", "pkwy": "parkway", "ter": "terrace",
            "tpke": "turnpike", "cir": "circle", "sq": "square", "pk": "park"}
# the folding's version: its rules here and stt_terms' number words (a change to either is another NORM)
NORM = "norm/2:" + __import__("hashlib").sha1(repr((sorted(SUFFIXES.items()), sorted(stt_terms._ONES.items()),
                                                    sorted(stt_terms._TENS.items()))).encode()).hexdigest()[:6]
MIN_N = 20                  # samples before a verdict (and before a promotion's watch can roll it back)
# a promote's evidence (the admin's rule): at least this many scored samples of the channel's domain, and the verdict
# "challenger better"; shown before the admin confirms (HubApp.stt_promote)
MIN_N_PROMOTE = {"fire": 40, "police": 40, "air": 40, "other": 30}
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
    return [SUFFIXES.get(w, w) for w in _TOKEN.findall(t)]


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
    """The refs (phrases: a unit's spoken name, an address's words, a callsign as said, a term) said in the text: each
    ref's NORM tokens in a row in it."""
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


def side(domain: str, metrics: dict, other: dict | None = None) -> float | None:
    """A side's score: the mean of its domain's metrics that apply to both sides (other: the other side's), so the two
    are always averaged over the same metrics."""
    vals = [metrics[m] for m in METRICS.get(domain, METRICS["other"])
            if metrics.get(m) is not None and (other is None or other.get(m) is not None)]
    return round(sum(vals) / len(vals), 4) if vals else None


def _scores(out: dict) -> dict:
    d = out.get("domain", "other")
    a, b = out.get("champ") or {}, out.get("chall") or {}
    a["score"], b["score"] = side(d, a, b), side(d, b, a)
    return out


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
    times = [time_score(c) for c in checks] if domain == "fire" else [None, None]
    if any(t is not None for t in times):       # (a time read by one side and none by the other: the other's 0)
        times = [t if t is not None else 0.0 if getattr(checks[i], "result", "") == "no_time" else None
                 for i, t in enumerate(times)]
    for i, (name, text) in enumerate((("champ", champ), ("chall", chall))):
        m: dict = {}
        if domain == "fire":
            m["time"] = times[i]
        for k, (a, b) in hits.items():
            mine, other = (a, b) if i == 0 else (b, a)
            m[k] = _share(mine, other)
        if edit:
            e = wer(edit, text)
            m["edit"] = None if e is None else round(max(0.0, 1.0 - e), 4)
            ids = identifiers(edit)
            m["ids"] = round(len(ids & identifiers(text)) / len(ids), 4) if ids else None
        out[name] = m
    return _scores(out)


def with_edit(s: dict, champ: str, chall: str, edit: str) -> dict:
    """A sample scored again once a person corrected its clip (the edit and identifier recall added)."""
    out = json.loads(json.dumps(s))
    for name, text in (("champ", champ), ("chall", chall)):
        m = out.get(name) or {}
        e = wer(edit, text)
        m["edit"] = None if e is None else round(max(0.0, 1.0 - e), 4)
        ids = identifiers(edit)
        m["ids"] = round(len(ids & identifiers(text)) / len(ids), 4) if ids else None
        out[name] = m
    return _scores(out)


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


ROLLBACK_Z = 2.58           # the rollback's interval: 99% (it is looked at again at each checkpoint)


def should_roll_back(cmp: dict) -> bool:
    """After a promotion the model it replaced runs as the challenger: roll back when, with enough samples, it scores
    better and even the low end of a 99% interval of the difference is past ROLLBACK_MARGIN (the hub looks only at
    checkpoints, n = 20, 40, 80 ...: never after every sample)."""
    if cmp.get("n", 0) < MIN_N or cmp.get("diff") is None or cmp.get("ci") is None:
        return False
    return cmp["diff"] - cmp["ci"] * ROLLBACK_Z / 1.96 > ROLLBACK_MARGIN


def promote_ok(board: dict) -> tuple[bool, str]:
    """May this board's challenger be promoted (an admin's action): (yes, the evidence said; or why not)."""
    need = MIN_N_PROMOTE.get(str(board.get("domain") or "other"), MIN_N_PROMOTE["other"])
    n = int(board.get("n") or 0)
    if not board.get("challenger"):
        return False, "no challenger"
    if n < need:
        return False, f"{n} of {need} samples"
    if board.get("verdict") != "challenger better":
        return False, f"verdict: {board.get('verdict') or 'none'}"
    return True, (f"{board['challenger']} {board.get('chall', 0):.3f} vs {board.get('champion')} "
                  f"{board.get('champ', 0):.3f} over {n} clips (diff {board.get('diff', 0):+.3f} ± {board.get('ci') or 0:.3f})")


def drop_context(refs: dict, context=(), prompt: str = "") -> dict:
    """The refs without what was in the context sent to the models (the admin's rule: a model that only echoes its
    context, the ADS-B list, a talker's name or a call sign, never scores for it): each ref whose NORM tokens are a
    context item, or (prompt) in the prompt, is left out. The hub passes the names it sent (HubApp._pool_models
    ctx_names), never the channel's vocabulary: both sides get that alike."""
    sent = [" ".join(normalize(c)) for c in context or () if c]
    joined = f" {' '.join(normalize(prompt))} " if prompt else ""
    out: dict = {}
    for k, v in (refs or {}).items():
        if not isinstance(v, list):
            out[k] = v
            continue
        keep = []
        for r in v:
            t = " ".join(normalize(r))
            if not t or t in sent or (joined and f" {t} " in joined):
                continue
            keep.append(r)
        out[k] = keep
    return out


# -- shadow evidence against AWS (#878): a channel on Amazon Transcribe has AWS as its champion, so a challenger's
# evidence can't come from a GPU champion; its GPU candidates decode sampled clips in shadow and are scored with AWS's
# text as the reference: identifier recall (AWS's identifiers the candidate has), agreement (1 - word error against
# AWS), the dispatcher's trailing time (each side's match, fire dispatch), and edit-WER once a person corrected the
# clip (each side against the correction). A recommendation only: only an admin moves a channel off AWS.
SHADOW_PCT = 10.0             # a channel's clips sampled for its GPU candidates (on by default; the admin's off)
MIN_N_SHADOW = 40             # samples before "matches or beats AWS"
SHADOW_IDS_MIN = 0.9          # ... identifier recall against AWS at least this
SHADOW_AGREE_MIN = 0.8        # ... agreement at least this
SHADOW_MARGIN = 0.02          # ... and no worse than AWS on the time and the edits (each with 10 samples or more)


def vs_reference(domain: str, ref: str, cand: str, checks: tuple = (None, None), context=(), prompt: str = "") -> dict:
    """One shadow sample (numbers only): {norm, domain, ref: "aws", agree, ids (None: AWS said none), time: [aws,
    cand] (fire dispatch, None when not read)}. An identifier that was in the context sent to the model (context,
    prompt) never counts for it."""
    domain = DOMAINS.get(domain, domain if domain in METRICS else "other")
    ids = identifiers(ref) - identifiers(", ".join(str(c) for c in context or ()) + ". " + str(prompt or ""))
    times = [time_score(c) for c in checks] if domain == "fire" else [None, None]
    if any(t is not None for t in times):
        times = [t if t is not None else 0.0 if getattr(checks[i], "result", "") == "no_time" else None
                 for i, t in enumerate(times)]
    return {"norm": NORM, "domain": domain, "ref": "aws", "agree": agreement(ref, cand),
            "ids": round(len(ids & identifiers(cand)) / len(ids), 4) if ids else None, "time": times}


def shadow_summary(samples: list[dict], edits: list[tuple] = ()) -> dict:
    """A channel's shadow samples of one model: n, agreement and identifier recall (each its mean and n), the time
    match of each side (n), edit-WER of each side against people's corrections (edits: [(aws word error, the model's)],
    n), and the recommendation: "matches or beats AWS on N clips" or "not yet"."""
    def mean(xs):
        xs = [x for x in xs if isinstance(x, (int, float))]
        return (round(sum(xs) / len(xs), 4) if xs else None), len(xs)
    agree, n_agree = mean(s.get("agree") for s in samples)
    ids, n_ids = mean(s.get("ids") for s in samples)
    tpairs = [s["time"] for s in samples if isinstance(s.get("time"), list) and len(s["time"]) == 2
              and all(isinstance(x, (int, float)) for x in s["time"])]
    t_aws = round(sum(p[0] for p in tpairs) / len(tpairs), 4) if tpairs else None
    t_cand = round(sum(p[1] for p in tpairs) / len(tpairs), 4) if tpairs else None
    e_aws = round(sum(1 - e[0] for e in edits) / len(edits), 4) if edits else None
    e_cand = round(sum(1 - e[1] for e in edits) / len(edits), 4) if edits else None
    n = len(samples)
    ok = (n >= MIN_N_SHADOW and ids is not None and ids >= SHADOW_IDS_MIN and agree is not None
          and agree >= SHADOW_AGREE_MIN
          and (len(tpairs) < 10 or t_cand >= t_aws - SHADOW_MARGIN)
          and (len(edits) < 10 or e_cand >= e_aws - SHADOW_MARGIN))
    return {"n": n, "agree": agree, "agree_n": n_agree, "ids": ids, "ids_n": n_ids,
            "time": {"aws": t_aws, "model": t_cand, "n": len(tpairs)},
            "edit": {"aws": e_aws, "model": e_cand, "n": len(edits)},
            "verdict": f"matches or beats AWS on {n} clips" if ok else "not yet", "ok": ok}


def sampled(clip_id: str, model: str, pct: float) -> bool:
    """This clip is one of the challenger's pct% (the same answer every time it is asked: a clip id's hash)."""
    import hashlib
    if pct <= 0:
        return False
    return int(hashlib.sha1(f"{clip_id}|{model}".encode()).hexdigest()[:8], 16) % 10000 < pct * 100
