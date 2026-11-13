#!/usr/bin/env python3
"""Replay / backtest the intel pipeline over a locally restored cad.sqlite (radio intel plan, PR 7; the engine:
sizeup/intel/replay.py). Prints aggregates only: counts, rates, metres, seconds. Never a transcript, a talker, an
address, an id or anything of the county CAD's.

    python tools/intel_replay.py --db RESTORED.sqlite [--since 2026-11-01] [--until ...] [--services police,fire]
                                 [--rules PACK.json] [--labels [MAP.json]] [--json OUT]
    python tools/intel_replay.py --db RESTORED.sqlite --diff A.json B.json [--labels]      the same rows, two rule sets
    python tools/intel_replay.py --db RESTORED.sqlite --since ... --until ... --capture NAME
                                 a private `mrrs-intel` replay set (tests/replay.py): LOCAL ONLY, never pushed
    python tools/intel_replay.py --set mrrs-intel/NAME [--pin]          replay a captured set (--pin: its expected.json)

--db: a copy restored from the hub's Litestream backup (docs/cloud.md: litestream restore), opened read-only
(SQLite refuses every write on the connection). Delete the copy when done: it holds real data.
--labels: auto-labels from the county CAD incidents ALREADY STORED in that copy (sizeup/intel/labels.py: a pure read
of source_reports and incidents; no request reaches the county's application). MAP.json optionally overrides the
classes: {"types": {"fire": ["STRU", ...]}, "categories": {"fire_report": "fire"}}.
--rules: a pack file for the intel core's loader (else the shipped packs and the copy's Settings overrides).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup.cad.store import TZ  # noqa: E402
from sizeup.intel import labels as lab  # noqa: E402
from sizeup.intel import replay as rp  # noqa: E402

KIND = "mrrs-intel"


def when(v: str) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        d = datetime.fromisoformat(v)
        return (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()


def pct(x) -> str:
    return "-" if x is None else f"{x:.1%}"


def num(x, unit="") -> str:
    return "-" if x is None else f"{x:.0f}{unit}" if abs(x) >= 10 else f"{x:.1f}{unit}"


def show(m: dict) -> None:
    print(f"rows {m['rows']} · {m['days']} d · ruleset {m['ruleset'] or '-'} · hits {m['hits']} · events {m['events']} "
          f"· pushes {m['pushes']} ({m['pushes_per_day']}/day) · shed clips {m['shed']}")
    r = m.get("recall")
    if r:
        print(f"recall {pct(r['recall'])} ({r['matched']}/{r['county_incidents']} county incidents) · lead median "
              f"{num(r['lead_s_median'], ' s')} · p90 {num(r['lead_s_p90'], ' s')}")
        for cls, v in r["by_class"].items():
            print(f"  {cls}: {v['matched']}/{v['n']}")
    p = m["place"]
    print(f"place: address-resolved {pct(p['address_resolved'])} of {p['placed']} · error median "
          f"{num(p['error_m_median'], ' m')} · p90 {num(p['error_m_p90'], ' m')} · false place {pct(p['false_place'])}")
    lt = m["latency"]
    print(f"latency: feed p50 {num(lt['feed_ms_p50'], ' ms')} · p90 {num(lt['feed_ms_p90'], ' ms')} · max "
          f"{num(lt['feed_ms_max'], ' ms')} · raise median {num(lt['raise_s_median'], ' s')} · p90 {num(lt['raise_s_p90'], ' s')}")
    for title, part in (("category", m["categories"]), ("rule", m["rules"])):
        if not part:
            continue
        print(f"{title:<28}{'hits':>7}{'events':>8}{'pushes':>8}{'labelled':>10}{'precision':>11}")
        for k, v in part.items():
            print(f"  {k[:26]:<26}{v.get('hits', ''):>7}{v['events']:>8}{v['pushes']:>8}{v['labelled']:>10}"
                  f"{pct(v['precision']):>11}")


def label_map(arg) -> tuple[dict | None, dict | None]:
    if not arg or arg is True:
        return None, None
    d = json.loads(Path(arg).read_text())
    types = {k: tuple(str(c).upper() for c in v) for k, v in (d.get("types") or {}).items()} or None
    cats = {str(k).lower(): v for k, v in (d.get("categories") or {}).items()} or None
    return types, cats


def capture(rows: list[dict], incidents: list[dict], name: str) -> Path:
    """A private replay set of the rows and their labels: <cache>/mrrs-intel/<name>-<UTC>/ (tests/replay.py). Local
    only: tools/replay_sync.py never pushes this kind (it holds police transcripts)."""
    from tests.replay import cache_dir, set_id, write_manifest
    d = cache_dir() / KIND / set_id(name)
    d.mkdir(parents=True, exist_ok=False)
    for fn, items in (("rows.jsonl.gz", rows), ("incidents.jsonl.gz", incidents)):
        with gzip.open(d / fn, "wt", encoding="utf-8") as f:
            for x in items:
                f.write(json.dumps(x, separators=(",", ":")) + "\n")
    lo = rows[0]["start"] if rows else time.time()
    hi = rows[-1]["end"] if rows else lo
    write_manifest(d, KIND, source="a local restore of the hub's cad.sqlite", how=" ".join(sys.argv),
                   captured_from=lo, captured_to=hi,
                   files={"rows.jsonl.gz": "radio_traffic rows (the replay's columns), oldest first",
                          "incidents.jsonl.gz": "county CAD auto-labels: {t, cls, lat, lon} only"},
                   notes="PRIVATE, local only: never pushed (tools/replay_sync.py), never in git")
    return d


def load_set(spec: str):
    from tests.replay import cache_dir, find_sets
    kind, _, name = spec.partition("/")
    if kind != KIND or not name:
        raise SystemExit(f"--set {KIND}/<set>")
    s = next((x for x in find_sets(KIND) if x.path.name == name), None)
    if s is None:
        raise SystemExit(f"no set {spec} in {cache_dir()}")
    return s, list(s.jsonl("rows.jsonl.gz")), list(s.jsonl("incidents.jsonl.gz"))


def pin(s, m: dict) -> None:
    """The set's expected.json: the metrics the regression test holds the pipeline to (and the manifest again)."""
    from tests.replay import write_manifest
    keep = {"events": m["events"], "pushes": m["pushes"], "recall": (m.get("recall") or {}).get("recall"),
            "false_place": m["place"]["false_place"], "precision": {k: v["precision"] for k, v in m["categories"].items()},
            "ruleset": m["ruleset"]}
    (s.path / "expected.json").write_text(json.dumps(keep, indent=1) + "\n")
    files = {f["path"]: f["what"] for f in s.manifest.get("files", [])}
    files["expected.json"] = "the pinned aggregates (tools/intel_replay.py --pin)"
    mf = s.manifest
    write_manifest(s.path, KIND, source=mf.get("source", ""), how=mf.get("how", ""),
                   captured_from=time.time(), captured_to=time.time(), files=files, notes=mf.get("notes", ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", type=Path, help="a locally restored cad.sqlite (read-only)")
    src.add_argument("--set", help=f"a private replay set: {KIND}/<name>")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--services", default="", help="only these services' rows (police,fire ...)")
    ap.add_argument("--rules", type=Path)
    ap.add_argument("--diff", nargs=2, type=Path, metavar=("A", "B"))
    ap.add_argument("--labels", nargs="?", const=True, default=None, metavar="MAP.json")
    ap.add_argument("--capture", metavar="NAME")
    ap.add_argument("--pin", action="store_true")
    ap.add_argument("--json", type=Path, help="also write the aggregates here (never into the repo)")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except ImportError as e:
        print(f"the intel core (sizeup/intel/engine.py, PR 1) isn't on this branch yet: {e}", file=sys.stderr)
        return 2


def run(a) -> int:
    types, cats = label_map(a.labels)
    s = None
    if a.db:
        con = rp.open_ro(a.db)
        try:
            rows = rp.load_rows(con, when(a.since), when(a.until), [x.strip() for x in a.services.split(",")])
            incidents = None
            if a.labels or a.capture:
                lo = rows[0]["start"] if rows else 0.0
                hi = rows[-1]["end"] if rows else 0.0
                incidents = lab.county_incidents(con, lo, hi, types)
        finally:
            con.close()
    else:
        s, rows, incidents = load_set(a.set)
        if not a.labels:
            incidents = None
    if a.capture:
        d = capture(rows, incidents or [], a.capture)
        print(f"captured {len(rows)} rows, {len(incidents or [])} labels: {d} (local only)")
        return 0
    if a.diff:
        ra, rb = rp.replay(rows, rules=a.diff[0]), rp.replay(rows, rules=a.diff[1])
        ma, mb = rp.metrics(ra, incidents, cats), rp.metrics(rb, incidents, cats)
        out = rp.diff(ra, rb, ma, mb)
        print(f"B against A over {len(rows)} rows: events +{out['events_gained']} -{out['events_lost']} · pushes "
              f"+{out['pushes_gained']} -{out['pushes_lost']}")
        for k in out["a"]:
            print(f"  {k}: {out['a'][k]} -> {out['b'][k]}")
    else:
        out = rp.metrics(rp.replay(rows, rules=a.rules), incidents, cats)
        show(out)
        if a.pin and s is not None:
            pin(s, out)
            print(f"pinned: {s.path / 'expected.json'}")
    if a.json:
        a.json.write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
