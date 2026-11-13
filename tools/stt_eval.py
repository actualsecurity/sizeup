#!/usr/bin/env python3
"""Speech-to-text evaluation against hand-corrected transcripts (radio intel plan, PR 7; generalises tools/atc_eval.py's
WER and prompt-insertion measures from HPN ATC to every channel).

    python tools/stt_eval.py --db state/cad.sqlite [--all] [--domain police] [--since 2026-11-01] [--until ...]
                             [--variant NAME=FROM..TO ...] [--prompt-words FILE] [--json OUT]
    python tools/stt_eval.py --jsonl stt-corrections.jsonl ...      (#428's JSONL: the admin export / the bundles)

The reference set is `stt_corrections` (sizeup/cad/sttfix.py): the rows an admin marked "reference" (--all: every
correction). It is PRIVATE: read from a locally restored cad.sqlite (the Litestream workflow, docs/cloud.md), opened
read-only, or from the admin export; never copied into the repo. This tool prints aggregates only (counts and rates):
never a transcript, a talker, an address or an id. Delete the restored copy when done.

Hypotheses: each transmission's stored transcripts, by engine: `text` under its `model` and, with the "both" engine
mode, `alt_text` under `alt_model` (AWS next to whisper on the same audio). The engine family is the model string's
("aws-transcribe ..." -> aws, anything else -> whisper). --variant splits them by time (e.g. before and after the
police prompt or the AWS vocabulary went on): `--variant base=..2026-11-20 --variant prompt=2026-11-20..`, so
"aws/prompt" and "whisper/base" are compared on their own clips. Only clips with a hypothesis from both engines are
"paired" (the both-engines A/B); every figure is given for all clips and for the paired ones.

Metrics, per engine (and variant):
    wer              word edits / reference words (lower case, [a-z0-9]+ words; spoken digits zero..nine as digits,
                     digit strings split into digits, so "one two three" = "123")
    key_recall       of the reference's tagged spans' words (address, callsign, category), the share found in the
                     hypothesis (as a multiset); untagged references: their address-like phrases and digit strings
    house_number     of the reference's addresses with a house number (tagged spans, else radiolog.addresses_in), the
                     share whose number the hypothesis has (as written or spoken)
    street           ... whose street name words the hypothesis has, in order
    right_block      ... whose street the hypothesis has and a house number in the same hundred block (420 for 456:
                     right block, wrong house): what a block-level push needs
    prompt_insertion of the clips, the share whose hypothesis has a prompt / vocabulary word (--prompt-words: one word
                     or phrase a line; default: sizeup/stt_vocab.py's police phrases when it has them) that the
                     reference does not: words the prompt put in the model's mouth. Also per 100 hypothesis words.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup.cad.radiolog import addresses_in  # noqa: E402
from sizeup.cad.store import TZ  # noqa: E402

DIGIT_WORDS = {"zero": "0", "oh": None, "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
               "seven": "7", "eight": "8", "nine": "9", "niner": "9"}
SUFFIX = {"street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln", "place": "pl",
          "court": "ct", "boulevard": "blvd", "terrace": "ter", "parkway": "pkwy", "highway": "hwy", "circle": "cir"}


# -- words --------------------------------------------------------------------------------------------------------------

def tokens(text: str) -> list[str]:
    """Lower case words, digits one per token ("123" and "one two three" alike), street suffixes abbreviated."""
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w.isdigit():
            out.extend(w)
        elif DIGIT_WORDS.get(w):
            out.append(DIGIT_WORDS[w])
        else:
            out.append(SUFFIX.get(w, w))
    return out


def edits(hyp: list[str], ref: list[str]) -> int:
    d = list(range(len(hyp) + 1))
    for i, rw in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev, d[j] = d[j], cur
    return d[len(hyp)]


def wer(hyp: str, ref: str) -> tuple[int, int]:
    """(word edits, reference words)."""
    h, r = tokens(hyp), tokens(ref)
    return edits(h, r), len(r)


def contains_seq(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


# -- addresses ----------------------------------------------------------------------------------------------------------

def split_address(phrase: str) -> tuple[str, list[str]]:
    """'456 Example Ave' -> ('456', ['example', 'ave']); no leading number -> ('', words)."""
    m = re.match(r"\s*(\d+)\s+(.*)", phrase or "")
    if m:
        return m.group(1), [t for t in tokens(m.group(2)) if not t.isdigit()]
    return "", [t for t in tokens(phrase) if not t.isdigit()]


def ref_addresses(ref: str, tags: list[dict]) -> list[tuple[str, list[str]]]:
    tagged = [t.get("value") or ref[t["start"]:t["end"]] for t in tags if t.get("kind") == "address"]
    phrases = tagged if tagged else addresses_in(ref)
    return [a for a in (split_address(p) for p in phrases) if a[0] and a[1]]


def hyp_numbers(hyp: str) -> list[str]:
    """The numbers the hypothesis has: as written ("456") and digit by digit, spoken or not ("four five six", "4 5 6")."""
    out = re.findall(r"\d+", hyp or "")
    run = ""
    for w in re.findall(r"[a-z0-9]+", (hyp or "").lower()):
        d = DIGIT_WORDS.get(w) or (w if len(w) == 1 and w.isdigit() else None)
        if d:
            run += d
            continue
        if len(run) > 1:
            out.append(run)
        run = ""
    if len(run) > 1:
        out.append(run)
    return out


def score_address(hyp: str, number: str, street: list[str]) -> tuple[bool, bool, bool]:
    """(house number right, street right, right hundred block: street right and a number in the same block)."""
    nums = hyp_numbers(hyp)
    street_ok = contains_seq(tokens(hyp), street)
    num_ok = number in nums
    block = street_ok and any(n.isdigit() and int(n) // 100 == int(number) // 100 and len(n) == len(number) for n in nums)
    return num_ok, street_ok, block


# -- key tokens, prompt insertions ---------------------------------------------------------------------------------------

def key_tokens(ref: str, tags: list[dict]) -> list[str]:
    spans = [ref[t["start"]:t["end"]] for t in tags if t.get("kind") in ("address", "callsign", "category")]
    if not spans:                       # untagged: its addresses, and the numbers outside them (units, callsigns)
        addrs = addresses_in(ref)
        spans = addrs + [n for n in re.findall(r"\d+", ref or "") if not any(n in a.split() for a in addrs)]
    out: list[str] = []
    for s in spans:
        out += tokens(s)
    return out


def key_recall(hyp: str, keys: list[str]) -> tuple[int, int]:
    have = Counter(tokens(hyp))
    found = 0
    for k, n in Counter(keys).items():
        found += min(n, have.get(k, 0))
    return found, len(keys)


def prompt_insertions(hyp: str, ref: str, phrases: list[list[str]]) -> int:
    """Prompt / vocabulary phrases in the hypothesis that the reference doesn't have."""
    h, r = tokens(hyp), tokens(ref)
    return sum(1 for p in phrases if contains_seq(h, p) and not contains_seq(r, p))


def default_prompt_words() -> list[str]:
    try:
        from sizeup import stt_vocab
        fn = getattr(stt_vocab, "police_phrases", None)
        return [str(p) for p in fn()] if fn else []
    except Exception:  # noqa: BLE001
        return []


# -- the reference set -----------------------------------------------------------------------------------------------------

def family(model: str) -> str:
    return "aws" if (model or "").lower().startswith("aws") else "whisper"


def when(v: str) -> float | None:
    """unix seconds, or an ISO date / time (the department's zone when it names none); '' -> None."""
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        d = datetime.fromisoformat(v)
        return (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()


def parse_variants(specs: list[str]) -> list[tuple[str, float | None, float | None]]:
    out = []
    for s in specs or []:
        name, _, rng = s.partition("=")
        a, _, b = rng.partition("..")
        if not name or "=" not in s or ".." not in rng:
            raise SystemExit(f"--variant NAME=FROM..TO (either side may be empty): {s!r}")
        out.append((name, when(a), when(b)))
    return out


def variant_of(start: float, variants) -> str | None:
    if not variants:
        return ""
    for name, a, b in variants:
        if (a is None or start >= a) and (b is None or start < b):
            return name
    return None


def from_db(path: Path, all_rows: bool, domain: str, since: float | None, until: float | None) -> list[dict]:
    """The references from a restored cad.sqlite, opened read-only (never written)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        where, args = ["1"], []
        if not all_rows:
            where.append("c.reference = 1")
        if domain:
            where.append("c.domain = ?")
            args.append(domain)
        if since is not None:
            where.append("t.start >= ?")
            args.append(since)
        if until is not None:
            where.append("t.start < ?")
            args.append(until)
        rows = con.execute("SELECT t.start, t.text, t.model, t.alt_text, t.alt_model, c.corrected, c.tags, c.domain "
                           "FROM stt_corrections c JOIN radio_traffic t ON t.id = c.traffic_id "
                           f"WHERE {' AND '.join(where)} ORDER BY t.start", args).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        hyps = {}
        if r["text"]:
            hyps.setdefault(family(r["model"]), r["text"])
        if r["alt_text"]:
            hyps.setdefault(family(r["alt_model"]), r["alt_text"])
        out.append({"start": r["start"], "ref": r["corrected"], "tags": json.loads(r["tags"] or "[]"),
                    "domain": r["domain"], "hyps": hyps})
    return out


def from_jsonl(path: Path, all_rows: bool, since: float | None, until: float | None) -> list[dict]:
    """The references from #428's JSONL (the admin export, a bundle): no tags there (addresses found in the text)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            c = d.get("correction")
            if not c or not c.get("text") or (not all_rows and not c.get("checked")):
                continue
            start = float(d.get("start") or 0)
            if (since is not None and start < since) or (until is not None and start >= until):
                continue
            hyps = {}
            for model, text in (d.get("asr") or {}).items():
                if text:
                    hyps.setdefault(family(model), text)
            out.append({"start": start, "ref": c["text"], "tags": [], "domain": "", "hyps": hyps})
    return out


# -- scoring --------------------------------------------------------------------------------------------------------------

def blank() -> dict:
    return {"clips": 0, "edits": 0, "ref_words": 0, "hyp_words": 0, "keys_found": 0, "keys": 0, "addresses": 0,
            "house_number": 0, "street": 0, "right_block": 0, "clips_inserted": 0, "insertions": 0}


def add(acc: dict, hyp: str, ref: str, tags: list[dict], phrases) -> None:
    e, n = wer(hyp, ref)
    acc["clips"] += 1
    acc["edits"] += e
    acc["ref_words"] += n
    acc["hyp_words"] += len(tokens(hyp))
    f, k = key_recall(hyp, key_tokens(ref, tags))
    acc["keys_found"] += f
    acc["keys"] += k
    for num, street in ref_addresses(ref, tags):
        a, b, c = score_address(hyp, num, street)
        acc["addresses"] += 1
        acc["house_number"] += a
        acc["street"] += b
        acc["right_block"] += c
    ins = prompt_insertions(hyp, ref, phrases) if phrases else 0
    acc["insertions"] += ins
    acc["clips_inserted"] += ins > 0


def rates(acc: dict) -> dict:
    def r(a, b):
        return round(a / b, 3) if b else None
    return {"clips": acc["clips"], "ref_words": acc["ref_words"], "wer": r(acc["edits"], acc["ref_words"]),
            "key_recall": r(acc["keys_found"], acc["keys"]), "addresses": acc["addresses"],
            "house_number": r(acc["house_number"], acc["addresses"]), "street": r(acc["street"], acc["addresses"]),
            "right_block": r(acc["right_block"], acc["addresses"]),
            "prompt_insertion": r(acc["clips_inserted"], acc["clips"]),
            "insertions_per_100w": r(100 * acc["insertions"], acc["hyp_words"])}


def evaluate(refs: list[dict], variants=(), phrases: list[str] | None = None) -> dict:
    """{"all": {group: rates}, "paired": {group: rates}, "references": n}; group = engine[/variant]."""
    ph = [t for t in (tokens(p) for p in (phrases or [])) if t]
    groups: dict[str, dict] = {}
    paired: dict[str, dict] = {}
    for x in refs:
        v = variant_of(x["start"], variants)
        if v is None:
            continue
        both = len(x["hyps"]) >= 2
        for fam, hyp in x["hyps"].items():
            g = f"{fam}/{v}" if v else fam
            add(groups.setdefault(g, blank()), hyp, x["ref"], x["tags"], ph)
            if both:
                add(paired.setdefault(g, blank()), hyp, x["ref"], x["tags"], ph)
    return {"references": len(refs), "all": {g: rates(a) for g, a in sorted(groups.items())},
            "paired": {g: rates(a) for g, a in sorted(paired.items())}}


COLS = ("clips", "wer", "key_recall", "house_number", "street", "right_block", "prompt_insertion", "insertions_per_100w")


def show(out: dict) -> None:
    print(f"references: {out['references']} (aggregates only)")
    for part in ("all", "paired"):
        if not out[part]:
            continue
        print(f"\n{part}:")
        print("  " + "engine".ljust(18) + "".join(c[:12].rjust(13) for c in COLS))
        for g, r in out[part].items():
            cells = ["-" if r[c] is None else (str(r[c]) if c == "clips" else f"{r[c]:.1%}" if c != "insertions_per_100w"
                                                   else f"{r[c]:.2f}") for c in COLS]
            print("  " + g.ljust(18) + "".join(x.rjust(13) for x in cells))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", type=Path, help="a locally restored cad.sqlite (opened read-only)")
    src.add_argument("--jsonl", type=Path, help="#428's JSONL (the admin export)")
    ap.add_argument("--all", action="store_true", help="every correction, not only the references")
    ap.add_argument("--domain", default="", help="fire | police | ems | atc | other (--db only)")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--variant", action="append", default=[], help="NAME=FROM..TO (ISO date or unix s; either side empty)")
    ap.add_argument("--prompt-words", type=Path, help="the prompt / vocabulary words, one word or phrase a line")
    ap.add_argument("--json", type=Path, help="also write the aggregates here (never into the repo)")
    a = ap.parse_args(argv)
    since, until = when(a.since), when(a.until)
    refs = from_db(a.db, a.all, a.domain, since, until) if a.db else from_jsonl(a.jsonl, a.all, since, until)
    phrases = ([ln.strip() for ln in a.prompt_words.read_text().splitlines() if ln.strip()] if a.prompt_words
               else default_prompt_words())
    out = evaluate(refs, parse_variants(a.variant), phrases)
    show(out)
    if a.json:
        a.json.write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
