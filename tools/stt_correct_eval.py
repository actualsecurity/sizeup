#!/usr/bin/env python3
"""The post-ASR vocabulary correction (#879, sizeup/stt_correct.py) measured offline: each engine's transcript before
and after, against the admins' corrections (#491).

    python tools/stt_correct_eval.py --jsonl stt-corrections.jsonl --terms DIR [--channels all|307,tg:303,...]
                                     [--towns "Town A,Town B"] [--config HUB.toml] [--all] [--json OUT]
    python tools/stt_correct_eval.py --db RESTORED.sqlite --terms DIR ...
    python tools/stt_correct_eval.py --db RESTORED.sqlite --terms DIR --unlabelled 100000     (no references: below)

--terms: a folder of the private term lists (<name>.txt or terms/<name>.txt, as [stt] terms_dir; never in the repo).
The references: #428's JSONL (the admin export, Settings > Radios) or a locally restored cad.sqlite opened read-only;
only the corrections an admin marked "reference" unless --all. Each clip's channel picks its list by the hub's own rule
(stt_terms.own_for_channel, as App._own_for_key: a county talkgroup by its group's service, a county incident talkgroup
with none known fire; another system's talkgroup by its service; an FM channel by its kind): --config, the hub's config
(its channels' kinds, the talkgroups file's groups, the incident talkgroups), else the row's own group and service (the
radio log keeps both; #428's JSONL has no group: the service the hub tagged) and an FM channel as a fire traffic channel
(an airband row: airband). Its switch: --channels
(default the corrector's own, stt_correct.DEFAULT_CHANNELS). Like tools/stt_eval.py it prints aggregates only (counts and rates): never a transcript,
a term or an id, and --json refuses a path inside the repo.

Metrics, per engine family (aws, whisper) and all together, before -> after:
    id_recall    identifiers of the reference (the list's names in Title Case, unit IDs, call signs, ten-codes, as
                 written: "Engine 97", "10-4", a street) that the transcript has word for word, as a multiset
    wer          word edits / reference words, words as written (lower case, [a-z0-9]+: "ninety seven" is not "97")
    wer_norm     tools/stt_eval.py's WER (spoken numbers read as numerals): what the correction does beyond the digits
    helped/hurt  clips whose edits went down / up
    time_same    clips whose trailing-time check (sizeup/stt_time.py: result and the time read) is the same after: the
                 correction must never touch it (time_changed: it did)
    changed, fixes, rules   clips changed, corrections made, by rule (unit, ten, spell, sound)

--unlabelled N: the newest N transcripts of the radio log (--db; with --jsonl every line), no reference needed: how often
it corrects, by rule, and the trailing-time safety only (what to look at before a channel is switched on).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup import sourcekeys, stt_correct, stt_terms, stt_time  # noqa: E402
from sizeup.config import Config  # noqa: E402

WORD = re.compile(r"[a-z0-9]+")
MAX_ID_WORDS = 8


def words(text: str) -> list[str]:
    return WORD.findall((text or "").lower())


def edits(hyp: list[str], ref: list[str]) -> int:
    d = list(range(len(hyp) + 1))
    for i, rw in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev, d[j] = d[j], cur
    return d[len(hyp)]


def identifiers(terms) -> set[tuple[str, ...]]:
    """The list's identifiers as written, as word tuples: names in Title Case and terms with a number."""
    out = set()
    for t in terms:
        w = stt_terms.written(t)
        if stt_terms.has_number(w) or stt_correct._title(w):
            ws = tuple(words(w))
            if 0 < len(ws) <= MAX_ID_WORDS:
                out.add(ws)
    return out


def find_ids(ws: list[str], ids: set[tuple[str, ...]]) -> Counter:
    """The identifiers in a text's words, longest first, never overlapping."""
    got: Counter = Counter()
    i = 0
    while i < len(ws):
        for n in range(min(MAX_ID_WORDS, len(ws) - i), 0, -1):
            k = tuple(ws[i:i + n])
            if k in ids:
                got[k] += 1
                i += n
                break
        else:
            i += 1
    return got


def has_ids(ws: list[str], want: Counter) -> int:
    """How many of the wanted identifiers (a multiset) the words have, word for word."""
    n = 0
    for k, c in want.items():
        m = sum(1 for i in range(len(ws) - len(k) + 1) if tuple(ws[i:i + len(k)]) == k)
        n += min(c, m)
    return n


# -- the references --------------------------------------------------------------------------------------------------------

def from_jsonl(path: Path, all_rows: bool, every: bool = False) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            c = d.get("correction")
            if not every and (not c or not c.get("text") or (not all_rows and not c.get("checked"))):
                continue
            out.append({"ref": (c or {}).get("text"), "end": d.get("end"), "asr": d.get("asr") or {},
                        "key": stt_correct.row_key(d), "group": d.get("group"), "service": d.get("service") or "",
                        "air": str(d.get("system") or "") == "airband"})
    return out


def from_db_unlabelled(path: Path, limit: int) -> list[dict]:
    from sizeup.intel.replay import open_ro
    con = open_ro(path)
    try:
        rows = con.execute('SELECT channel, talkgroup, system, "group", service, end, text, model, alt_text, alt_model '
                           "FROM radio_traffic WHERE text != '' ORDER BY start DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        asr = {r["model"] or "unknown": r["text"]}
        if r["alt_text"]:
            asr[r["alt_model"] or "alt"] = r["alt_text"]
        out.append({"ref": None, "end": r["end"], "asr": asr, **_where(dict(r))})
    return out


def from_db(path: Path, all_rows: bool) -> list[dict]:
    from sizeup.intel.replay import open_ro
    con = open_ro(path)                            # never the live database; never written
    try:
        rows = con.execute('SELECT t.channel, t.talkgroup, t.system, t."group", t.service, t.end, t.text, t.model, '
                           "t.alt_text, t.alt_model, "
                           "c.corrected FROM stt_corrections c JOIN radio_traffic t ON t.id = c.traffic_id"
                           + ("" if all_rows else " WHERE c.reference = 1") + " ORDER BY t.start").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        asr = {}
        if r["text"] or r["model"]:
            asr[r["model"] or "unknown"] = r["text"] or ""
        if r["alt_text"] or r["alt_model"]:
            asr[r["alt_model"] or "alt"] = r["alt_text"] or ""
        out.append({"ref": r["corrected"], "end": r["end"], "asr": asr, **_where(dict(r))})
    return out


def _where(r: dict) -> dict:
    """A radio log row's channel key and what picks its list (its group, its service, airband or not)."""
    return {"key": stt_correct.row_key(r), "group": r.get("group"), "service": r.get("service") or "",
            "air": str(r.get("system") or "") == "airband"}


class Channels:
    """Which list a clip's channel takes, by the hub's rule (stt_terms.own_for_channel, as App._own_for_key). cfg: the
    hub's Config (its channels' kinds, the talkgroups file's groups, the incident talkgroups), else each row's own."""

    def __init__(self, cfg=None):
        self.cfg = cfg
        self.kinds = {c.label: c.kind for c in cfg.channels} if cfg is not None else {}
        self.incident = cfg.p25_incident_talkgroups if cfg is not None else Config.p25_incident_talkgroups
        self.owns: dict[str, str | None] = {}

    def own_of(self, r: dict) -> str | None:
        key = str(r.get("key") or "")
        if key in self.owns:
            return self.owns[key]
        group, listed, kind = None, "", ""
        got = sourcekeys.parse(key) if key.startswith(("tg:", "p25:")) else None
        if got is not None:
            system, tg = got
            if system == sourcekeys.COUNTY:
                group = ((self.cfg.p25_tg_info.get(tg) or {}).get("group") or "") if self.cfg is not None \
                    else r.get("group")
                if group is None and r.get("service"):  # (#428's JSONL has no group: the service the hub tagged)
                    own = self.owns[key] = stt_terms.own_for(sourcekeys.p25(system, tg), str(r["service"]))
                    return own
            else:
                listed = str(r.get("service") or "")
        elif self.cfg is not None:
            kind = self.kinds.get(key, "")
        else:
            kind = "airband" if r.get("air") else "traffic"
        own = self.owns[key] = stt_terms.own_for_channel(key, kind, group, listed, self.incident)
        return own

    def own_for_key(self, key: str) -> str | None:
        """The Corrector's: what own_of() found for the key."""
        return self.owns.get(key)


# -- scoring -----------------------------------------------------------------------------------------------------------------

def family(model: str) -> str:
    return "aws" if (model or "").lower().startswith("aws") else "whisper"


def blank() -> dict:
    return {"clips": 0, "changed": 0, "fixes": 0, "rules": Counter(), "ids": 0, "ids_before": 0, "ids_after": 0,
            "ref_words": 0, "edits_before": 0, "edits_after": 0, "norm_words": 0, "norm_before": 0, "norm_after": 0,
            "helped": 0, "hurt": 0, "time_same": 0, "time_changed": 0}


def _pct(a: int, b: int) -> float | None:
    return round(100.0 * a / b, 1) if b else None


def evaluate(refs: list[dict], corrector: stt_correct.Corrector, ids_of) -> dict:
    from tools.stt_eval import wer as norm_wer
    acc: dict[str, dict] = {}
    corrector.warm({r["key"] for r in refs})         # (the matchers built first: correct() never builds one)
    for r in refs:
        ids = ids_of(r["key"])
        if ids is None:
            continue
        ref = words(r["ref"]) if r["ref"] is not None else None
        want = find_ids(ref, ids) if ref is not None else Counter()
        for model, hyp in r["asr"].items():
            res = corrector.correct(hyp or "", r["key"])
            after = res.text if res is not None and res.text else hyp or ""
            for k in (family(model), "all"):
                a = acc.setdefault(k, blank())
                a["clips"] += 1
                if res is not None and res.fixes:
                    a["changed"] += 1
                    a["fixes"] += len(res.fixes)
                    a["rules"].update(f.rule for f in res.fixes)
                if ref is not None:
                    a["ids"] += sum(want.values())
                    a["ids_before"] += has_ids(words(hyp), want)
                    a["ids_after"] += has_ids(words(after), want)
                    eb, ea = edits(words(hyp), ref), edits(words(after), ref)
                    a["ref_words"] += len(ref)
                    a["edits_before"] += eb
                    a["edits_after"] += ea
                    a["helped"] += ea < eb
                    a["hurt"] += ea > eb
                    nb, nw = norm_wer(hyp or "", r["ref"])
                    na, _ = norm_wer(after, r["ref"])
                    a["norm_words"] += nw
                    a["norm_before"] += nb
                    a["norm_after"] += na
                cb, ca = stt_time.check(hyp or "", r["end"]), stt_time.check(after, r["end"])
                if cb is not None:
                    same = (cb.result, cb.said) == ((ca.result, ca.said) if ca is not None else None)
                    a["time_same" if same else "time_changed"] += 1
    out = {}
    for k, a in acc.items():
        out[k] = {"clips": a["clips"], "changed": a["changed"], "fixes": a["fixes"], "rules": dict(a["rules"]),
                  "ids": a["ids"], "id_recall_before": _pct(a["ids_before"], a["ids"]),
                  "id_recall_after": _pct(a["ids_after"], a["ids"]),
                  "wer_before": _pct(a["edits_before"], a["ref_words"]), "wer_after": _pct(a["edits_after"], a["ref_words"]),
                  "wer_norm_before": _pct(a["norm_before"], a["norm_words"]),
                  "wer_norm_after": _pct(a["norm_after"], a["norm_words"]),
                  "helped": a["helped"], "hurt": a["hurt"], "time_same": a["time_same"], "time_changed": a["time_changed"]}
    return out


COLS = ("clips", "changed", "fixes", "ids", "id_recall_before", "id_recall_after", "wer_before", "wer_after",
        "wer_norm_before", "wer_norm_after", "helped", "hurt", "time_same", "time_changed")


def show(out: dict) -> None:
    print("engine   " + "  ".join(f"{c:>14}" for c in COLS) + "  rules")
    for k in sorted(out, key=lambda k: (k == "all", k)):
        row = out[k]
        print(f"{k:<8} " + "  ".join(f"{'-' if row[c] is None else row[c]!s:>14}" for c in COLS)
              + "  " + ", ".join(f"{r} {n}" for r, n in sorted(row["rules"].items())))


def outside_repo(p: Path) -> Path:
    rp = p.expanduser().resolve()
    if rp == ROOT or ROOT in rp.parents:
        raise SystemExit(f"--json {p}: inside the repo; write it outside ({ROOT})")
    return rp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", type=Path, help="a locally restored cad.sqlite (opened read-only)")
    src.add_argument("--jsonl", type=Path, help="#428's JSONL (the admin export)")
    ap.add_argument("--terms", type=Path, required=True, help="the private term lists' folder (outside the repo)")
    ap.add_argument("--channels", default="", help="all, or channel keys / county talkgroups, comma-separated "
                                                   "(default: the corrector's own)")
    ap.add_argument("--towns", default="", help="the towns whose streets it may correct into, comma-separated")
    ap.add_argument("--config", type=Path, help="the hub's config (its channels' kinds, talkgroup groups): each "
                                                "channel's list as the hub picks it")
    ap.add_argument("--all", action="store_true", help="every correction, not only the references")
    ap.add_argument("--json", type=Path, help="also write the aggregates here (outside the repo)")
    ap.add_argument("--unlabelled", type=int, default=0, metavar="N",
                    help="no references: the newest N transcripts (--db; --jsonl: every line)")
    a = ap.parse_args(argv)
    out_path = outside_repo(a.json) if a.json else None
    store = stt_terms.TermStore(stt_terms.DirFiles(a.terms), log=lambda m: None)
    store.refresh()
    if not store.sets():
        print(f"no term lists read from {a.terms}", file=sys.stderr)
        return 2
    every = a.channels.strip() == "all"
    chans = stt_correct.DEFAULT_CHANNELS if not a.channels.strip() or every else stt_correct.channels_of(
        [int(c) if c.strip().isdigit() else c.strip() for c in a.channels.split(",") if c.strip()])

    if a.config:
        from sizeup.config import load
        cfg = load(a.config)
    else:
        cfg = None
    where = Channels(cfg)
    own = where.own_for_key
    towns = tuple(t.strip() for t in a.towns.split(",") if t.strip()) or stt_correct.DEFAULT_TOWNS
    corrector = stt_correct.Corrector(lambda: store, own, chans, towns, log=lambda m: None)
    if every:
        corrector.applies = lambda key: bool(key)   # type: ignore[method-assign]
    id_sets: dict[str, set] = {}

    def ids_of(key: str):
        if not corrector.applies(key):
            return None
        o = own(key)
        ts = store.get(o) if o else None
        if ts is None:
            return None
        if o not in id_sets:
            id_sets[o] = identifiers(ts.terms)
        return id_sets[o]

    if a.unlabelled:
        refs = from_db_unlabelled(a.db, a.unlabelled) if a.db else [
            {**r, "ref": None} for r in from_jsonl(a.jsonl, True, every=True)]
    else:
        refs = from_db(a.db, a.all) if a.db else from_jsonl(a.jsonl, a.all)
    for r in refs:
        where.own_of(r)
    out = evaluate(refs, corrector, ids_of)
    show(out)
    if out_path:
        out_path.write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
