#!/usr/bin/env python3
"""Seed the MTA's reporting points (sizeup/trains.py Points) from saved reads of the Metro-North realtime feed.

The MTA's train positions snap to fixed reporting points on the line, about 1 to 1.6 km apart; the hub learns
them as it reads the feed (state dir, trains/points.json) and carries a train on no further than the next one
it knows. This replays saved reads through the same judge the hub uses (sizeup/trains.py Tracker: a refused
position, off the line, outside its stops, backwards or too fast, teaches nothing) and adds what they teach to
data/mnr_reporting_points.json, the seed a new hub starts from.

    python tools/learn_trains_points.py DIR_OR_PB...           # each DIR: one capture, a .pb per read
    python tools/learn_trains_points.py --jsonl POLL.jsonl BASE.pb   # or vehicle-only polls over one full read

Reads more than RESET_S apart start a new run (what was there at its first read teaches nothing, as at a hub's
start). Run it once per capture: a second run over the same reads counts them again.

--jsonl: lines {"feed_ts", "vps": {train: [lat, lon, ts, current_status, stop_id]}} (a quick poller's output),
laid over BASE.pb's trips (their stops and statuses) as the feed of that time.

Sources and terms: sizeup/trains.py (MTA developer data, https://www.mta.info/developers).
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup import trains as T  # noqa: E402

GTFS = ROOT / "tests" / "fixtures" / "mta" / "gtfsmnr.zip"     # the line (its longest shape) and stations


def feeds_from(paths: list[str]) -> list[dict]:
    files = []
    for p in map(Path, paths):
        files += sorted(p.glob("*.pb")) if p.is_dir() else [p]
    return [T.parse_feed(f.read_bytes()) for f in files]


def feeds_from_jsonl(path: str, base: str) -> list[dict]:
    trips = T.parse_feed(Path(base).read_bytes())["trains"]
    out = []
    for line in Path(path).read_text().splitlines():
        row = json.loads(line)
        if "vps" not in row:
            continue
        trains = {}
        for tid, t in trips.items():
            v = row["vps"].get(t["train"])
            u = copy.deepcopy(t)
            u["pos"], u["vstatus"], u["vstop"] = None, "", ""
            if v:
                u["pos"] = {"lat": v[0], "lon": v[1], "bearing": None, "ts": v[2]}
                u["vstatus"], u["vstop"] = T.VEHICLE_STATUS.get(v[3], ""), v[4] or ""
            trains[tid] = u
        out.append({"timestamp": row["feed_ts"], "trains": trains})
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--jsonl", nargs=2, metavar=("POLL.jsonl", "BASE.pb"))
    ap.add_argument("--gtfs", default=str(GTFS))
    ap.add_argument("--out", default=str(T.SEED_POINTS))
    a = ap.parse_args(argv)
    model = T.Model(T.parse_static(Path(a.gtfs).read_bytes()))
    points = T.Points()
    out = Path(a.out)
    if out.is_file():
        points.merge(json.loads(out.read_text()), model.line)
    feeds = feeds_from(a.paths) + (feeds_from_jsonl(*a.jsonl) if a.jsonl else [])
    feeds.sort(key=lambda f: f["timestamp"] or 0)
    tk, last, refused = None, None, {}
    for f in feeds:
        at = f["timestamp"]
        if tk is None or last is None or at - last > T.RESET_S:
            if tk is not None:
                for k, v in tk.why.items():
                    refused[k] = refused.get(k, 0) + v
            tk = T.Tracker(points)
        tk.update(model, f, at)
        last = at
    if tk is not None:
        for k, v in tk.why.items():
            refused[k] = refused.get(k, 0) + v
    doc = points.doc() | {"note": "Seed for the hub's learned reporting points (state dir trains/points.json); built by "
                                  "tools/learn_trains_points.py from recorded reads of the MTA's feed"}
    head = json.dumps({k: v for k, v in doc.items() if k != "points"}, separators=(",", ":"))[:-1]
    out.write_text(head + ',"points":[\n' + ",\n".join(json.dumps(q, separators=(",", ":")) for q in doc["points"]) + "\n]}\n")
    print(f"{out}: {len(points)} points, {points.known} known (seen {T.POINT_KNOWN_N}+ times) from {len(feeds)} reads; "
          f"refused {refused}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
