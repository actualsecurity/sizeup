#!/usr/bin/env python3
"""Speech-to-text evaluation against hand-corrected transcripts (radio intel plan, PR 7; generalises tools/atc_eval.py's
WER and prompt-insertion measures from HPN ATC to every channel).

    python tools/stt_eval.py --db RESTORED.sqlite [--all] [--domain police] [--since 2026-11-01] [--until ...]
                             [--variant NAME=FROM..TO ...] [--prompt-words FILE] [--json OUT]
    python tools/stt_eval.py --jsonl stt-corrections.jsonl ...      (#428's JSONL: the admin export / the bundles)

The reference set is `stt_corrections` (sizeup/cad/sttfix.py): the rows an admin marked "reference" (--all: every
correction). It is PRIVATE: read from a locally restored cad.sqlite (the Litestream workflow, docs/cloud.md), opened
read-only (mode=ro), or from the admin export; never copied into the repo (--json refuses a path inside it). This tool
prints aggregates only (counts and rates): never a transcript, a talker, an address or an id. Delete the restored copy
when done, with its -wal and -shm files if SQLite left any.

Hypotheses: each transmission's stored transcripts, by engine: `text` under its `model` and, with the "both" engine
mode, `alt_text` under `alt_model` (AWS next to whisper on the same audio). The engine family is the model string's
("aws-transcribe ..." -> aws, anything else -> whisper). An engine that ran (its model is set) but wrote nothing is
scored as "": every reference word a deletion. --variant splits them by time (e.g. before and after the police prompt
or the AWS vocabulary went on): `--variant base=..2026-11-20 --variant prompt=2026-11-20..`. Only clips with both
engines are "paired" (the both-engines A/B); every figure is given for all clips and for the paired ones.

Words: lower case [a-z0-9]+, street suffixes abbreviated, ordinals as written ("first" -> 1st), and a run of spoken
numbers read as one numeral the way radio says them: digit by digit ("four five six" 456, "oh" a zero inside a run),
tens and teens ("sixty three" 63, "four fifty six" 456), hundreds ("one hundred" 100); single written digits in a run
join it ("4 5 6" 456).

Metrics, per engine (and variant):
    wer              word edits / reference words, numerals split into their digits ("123" = "one twenty three")
    key_recall       of the reference's tagged spans' words (address, callsign, category), the share found in the
                     hypothesis (as a multiset); untagged references: their address-like phrases and other numbers
    house_number     of the reference's addresses with a house number (tagged spans, else radiolog.addresses_in), the
                     share where the hypothesis has that number DIRECTLY before the street's words (a number elsewhere
                     in the clip never counts). After a unit word ("rescue one four five six example ave") the run
                     may carry the callsign's digits too: its last digits are the house number (456), never 1456
    street           ... whose street words (numbers kept: "route 1") the hypothesis has, in order
    right_block      of those with a house number of 100 or more, the share where the street is right and the number
                     before it is in the same hundred block (420 for 456: right block, wrong house). Under 100 there is
                     no block: not counted (block_addresses says how many were)
    prompt_insertion of the clips, the share whose hypothesis has a prompt / vocabulary phrase (--prompt-words: one
                     a line; default: sizeup/stt_vocab.py's police phrases when it has them) that the reference does
                     not; also per 100 hypothesis words. No phrases: "-" (not measured), never 0
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

from sizeup.cad.radiolog import UNIT_WORDS, addresses_in  # noqa: E402
from sizeup.cad.store import TZ  # noqa: E402

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "niner": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
ORDINALS = {"first": (1, "st"), "second": (2, "nd"), "third": (3, "rd"), "fourth": (4, "th"), "fifth": (5, "th"),
            "sixth": (6, "th"), "seventh": (7, "th"), "eighth": (8, "th"), "ninth": (9, "th"), "tenth": (10, "th")}
SUFFIX = {"street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln", "place": "pl",
          "court": "ct", "boulevard": "blvd", "terrace": "ter", "parkway": "pkwy", "highway": "hwy", "circle": "cir"}
NUMERAL = re.compile(r"\d+")
CALLSIGN_WORDS = set(UNIT_WORDS) | {"car", "unit", "sector", "patrol", "squad"}    # a run after these may hold a callsign


# -- words --------------------------------------------------------------------------------------------------------------

def _is_num(w: str, in_run: bool) -> bool:
    return (w in UNITS or w in TEENS or w in TENS or w == "hundred" or (w == "oh" and in_run)
            or (len(w) == 1 and w.isdigit()))


def _run(ws: list[str]) -> str:
    """A run of number words (and single digits) -> one numeral, the way radio says numbers."""
    out, i = "", 0
    while i < len(ws):
        w = ws[i]
        if w in TENS:
            v = TENS[w]
            if i + 1 < len(ws) and (ws[i + 1] in UNITS and UNITS[ws[i + 1]] > 0):
                v += UNITS[ws[i + 1]]
                i += 1
            chunk = str(v)
        elif w in TEENS:
            chunk = str(TEENS[w])
        elif w == "oh":
            chunk = "0"
        elif w.isdigit():
            chunk = w
        else:
            chunk = str(UNITS[w])
        if i + 1 < len(ws) and ws[i + 1] == "hundred" and len(chunk) == 1:
            i += 1
            rest = ""
            j = i + 1
            if j < len(ws) and ws[j] in TENS:            # "four hundred fifty six"
                rest = str(TENS[ws[j]] + (UNITS[ws[j + 1]] if j + 1 < len(ws) and ws[j + 1] in UNITS else 0))
                i = j + (1 if j + 1 < len(ws) and ws[j + 1] in UNITS else 0)
            elif j < len(ws) and ws[j] in TEENS:
                rest, i = str(TEENS[ws[j]]), j
            elif j < len(ws) and ws[j] in UNITS:
                rest, i = str(UNITS[ws[j]]), j
            chunk = str(int(chunk) * 100 + int(rest or 0))
        out += chunk
        i += 1
    return out


def words(text: str) -> list[str]:
    """Lower case words, street suffixes abbreviated, ordinals as written, runs of spoken numbers as one numeral."""
    raw = [SUFFIX.get(w, w) for w in re.findall(r"[a-z0-9]+", (text or "").lower())]
    out: list[str] = []
    run: list[str] = []
    for w in raw:
        if _is_num(w, bool(run)):
            run.append(w)
            continue
        if run:
            if w in ORDINALS and run[-1] in TENS and ORDINALS[w][0] < 10:     # "twenty first" -> 21st
                k, suf = ORDINALS[w]
                out.append(f"{_run(run[:-1]) if len(run) > 1 else ''}{TENS[run[-1]] + k}{suf}")
                run = []
                continue
            out.append(_run(run))
            run = []
        if w in ORDINALS:
            n, suf = ORDINALS[w]
            out.append(f"{n}{suf}")
        else:
            out.append(w)
    if run:
        out.append(_run(run))
    return out


def tokens(text: str) -> list[str]:
    """words(), numerals split into their digits (for WER and key recall: "123" = "one twenty three")."""
    out: list[str] = []
    for w in words(text):
        out.extend(w if w.isdigit() else [w])
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


def find_seq(hay: list[str], needle: list[str]) -> list[int]:
    n = len(needle)
    return [i for i in range(len(hay) - n + 1) if n and hay[i:i + n] == needle]


def contains_seq(hay: list[str], needle: list[str]) -> bool:
    return bool(find_seq(hay, needle))


# -- addresses ----------------------------------------------------------------------------------------------------------

def split_address(phrase: str) -> tuple[str, list[str]]:
    """'456 Example Ave' -> ('456', ['example', 'ave']); '100 Route 1' -> ('100', ['route', '1']); no leading
    number -> ('', words)."""
    ws = words(phrase)
    if ws and ws[0].isdigit():
        return ws[0], ws[1:]
    return "", ws


def ref_addresses(ref: str, tags: list[dict]) -> list[tuple[str, list[str]]]:
    tagged = [t.get("value") or ref[t["start"]:t["end"]] for t in tags if t.get("kind") == "address"]
    phrases = tagged if tagged else addresses_in(ref)
    return [a for a in (split_address(p) for p in phrases) if a[0] and a[1]]


def number_before(h: list[str], i: int, width: int) -> str | None:
    """The house number directly before the street at h[i]: the numeral there; after a unit word, a run longer than the
    house number carries the callsign's digits first: its last `width` digits."""
    if i == 0 or not h[i - 1].isdigit():
        return None
    n = h[i - 1]
    if len(n) > width and i >= 2 and h[i - 2] in CALLSIGN_WORDS:
        return n[-width:]
    return n


def score_address(hyp: str, number: str, street: list[str]) -> tuple[bool, bool, bool | None]:
    """(house number right, street right, right hundred block | None when the number is under 100: no block)."""
    h = words(hyp)
    at = find_seq(h, street)
    befores = [number_before(h, i, len(number)) for i in at]
    num_ok = number in befores
    block = None
    if int(number) >= 100:
        block = any(b is not None and len(b) == len(number) and int(b) // 100 == int(number) // 100 for b in befores)
    return num_ok, bool(at), block


# -- key tokens, prompt insertions ---------------------------------------------------------------------------------------

def key_tokens(ref: str, tags: list[dict]) -> list[str]:
    spans = [ref[t["start"]:t["end"]] for t in tags if t.get("kind") in ("address", "callsign", "category")]
    if not spans:                       # untagged: its addresses, and the numbers outside them (units, callsigns)
        addrs = addresses_in(ref)
        spans = addrs + [n for n in NUMERAL.findall(ref or "") if not any(n in a.split() for a in addrs)]
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
    h, r = words(hyp), words(ref)
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


def _hyps(pairs) -> dict:
    """{family: text} from (model, text) pairs: an engine whose model is set but text empty is "" (all deletions)."""
    out: dict = {}
    for model, text in pairs:
        if model or text:
            out.setdefault(family(model), text or "")
    return out


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
    return [{"start": r["start"], "ref": r["corrected"], "tags": json.loads(r["tags"] or "[]"), "domain": r["domain"],
             "hyps": _hyps(((r["model"], r["text"]), (r["alt_model"], r["alt_text"])))} for r in rows]


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
            out.append({"start": start, "ref": c["text"], "tags": [], "domain": "",
                        "hyps": _hyps((d.get("asr") or {}).items())})
    return out


# -- scoring --------------------------------------------------------------------------------------------------------------

def blank() -> dict:
    return {"clips": 0, "edits": 0, "ref_words": 0, "hyp_words": 0, "keys_found": 0, "keys": 0, "addresses": 0,
            "house_number": 0, "street": 0, "block_addresses": 0, "right_block": 0, "clips_inserted": 0,
            "insertions": 0}


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
        if c is not None:
            acc["block_addresses"] += 1
            acc["right_block"] += c
    if phrases:
        ins = prompt_insertions(hyp, ref, phrases)
        acc["insertions"] += ins
        acc["clips_inserted"] += ins > 0


def rates(acc: dict, measured_prompt: bool = True) -> dict:
    def r(a, b):
        return round(a / b, 3) if b else None
    return {"clips": acc["clips"], "ref_words": acc["ref_words"], "wer": r(acc["edits"], acc["ref_words"]),
            "key_recall": r(acc["keys_found"], acc["keys"]), "addresses": acc["addresses"],
            "house_number": r(acc["house_number"], acc["addresses"]), "street": r(acc["street"], acc["addresses"]),
            "block_addresses": acc["block_addresses"], "right_block": r(acc["right_block"], acc["block_addresses"]),
            "prompt_insertion": r(acc["clips_inserted"], acc["clips"]) if measured_prompt else None,
            "insertions_per_100w": r(100 * acc["insertions"], acc["hyp_words"]) if measured_prompt else None}


def evaluate(refs: list[dict], variants=(), phrases: list[str] | None = None) -> dict:
    """{"all": {group: rates}, "paired": {group: rates}, "references": n}; group = engine[/variant]."""
    ph = [t for t in (words(p) for p in (phrases or [])) if t]
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
    return {"references": len(refs), "prompt_phrases": len(ph),
            "all": {g: rates(a, bool(ph)) for g, a in sorted(groups.items())},
            "paired": {g: rates(a, bool(ph)) for g, a in sorted(paired.items())}}


COLS = ("clips", "wer", "key_recall", "house_number", "street", "right_block", "prompt_insertion", "insertions_per_100w")


def show(out: dict) -> None:
    print(f"references: {out['references']} (aggregates only) · prompt phrases {out['prompt_phrases'] or '-'}")
    for part in ("all", "paired"):
        if not out[part]:
            continue
        print(f"\n{part}:")
        print("  " + "engine".ljust(18) + "".join(c[:12].rjust(13) for c in COLS))
        for g, r in out[part].items():
            cells = ["-" if r[c] is None else (str(r[c]) if c == "clips" else f"{r[c]:.1%}" if c != "insertions_per_100w"
                                                   else f"{r[c]:.2f}") for c in COLS]
            print("  " + g.ljust(18) + "".join(x.rjust(13) for x in cells))


def outside_repo(p: Path) -> Path:
    """--json: never a path inside the repo (the aggregates of private data stay out of git)."""
    rp = p.expanduser().resolve()
    if rp == ROOT or ROOT in rp.parents:
        raise SystemExit(f"--json {p}: inside the repo; write it outside ({ROOT})")
    return rp


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
    ap.add_argument("--json", type=Path, help="also write the aggregates here (outside the repo)")
    a = ap.parse_args(argv)
    out_path = outside_repo(a.json) if a.json else None
    since, until = when(a.since), when(a.until)
    refs = from_db(a.db, a.all, a.domain, since, until) if a.db else from_jsonl(a.jsonl, a.all, since, until)
    phrases = ([ln.strip() for ln in a.prompt_words.read_text().splitlines() if ln.strip()] if a.prompt_words
               else default_prompt_words())
    out = evaluate(refs, parse_variants(a.variant), phrases)
    show(out)
    if out_path:
        out_path.write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
