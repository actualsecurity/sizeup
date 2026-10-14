#!/usr/bin/env python3
"""The New Haven Line's two outer tracks through our area, one a direction, for the Trains layer: each train is drawn
on its own direction's track, not both on one centre line (user, 2026-10-14: "these trains look wonky and are
overlapped, we need to fix that onto their own inbound/outbound lines").

Which track each way (checked 2026-10-14): the MTA's own track numbers at the stations in our area (the realtime
feed's railroad extension, ~800 train-stops over 40 minutes of reads): inbound trains stop on track 3 at every
station from Stamford to Mount Vernon East, outbound on track 4 (Stamford also 2 and 5; New Rochelle twice 2).
OpenStreetMap maps the four tracks one way each with railway:track_ref; measured against the MTA's line (the
static feed's shape), across the whole area, left to right going outbound: 3 (about 4 m north of it), 1 (on it),
2, 4 (about 7 m south): right-hand running on the outer tracks, the stations' platforms. Expresses may run the
middle two between stops; the feed doesn't say, so every train is drawn on its direction's outer track.

    python tools/fetch_train_tracks.py                  # ask the Overpass API (the shared User-Agent)
    python tools/fetch_train_tracks.py --osm FILE.json  # or an Overpass answer already saved

Writes data/mnr_tracks.json: {source, license, built, lanes: {outbound: {track, coords: [[lon, lat, s]...]},
inbound: {...}}}: each the track sampled every STEP_M along the MTA's line (s, metres from Grand Central, as
sizeup/trains.py measures it) from LANE_MARGIN_M before our area to as far after, at the nearest point of that
track (within SNAP_M, on its side of the line, square across: within ALONG_M of it along), simplified to
SIMPLIFY_M. A sample with no such track near (none
in our area today) takes its neighbours' offset from the line.

Data: © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright), via the Overpass API.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup import USER_AGENT  # noqa: E402
from sizeup import trains as T  # noqa: E402

OVERPASS = "https://overpass-api.de/api/interpreter"
GTFS = ROOT / "tests" / "fixtures" / "mta" / "gtfsmnr.zip"
TRACKS = {"outbound": "4", "inbound": "3"}          # the MTA's track at our stations, each way (above)
SIDE = {"outbound": -1, "inbound": 1}                # which side of the line going outbound: right (south), left
STEP_M = 10.0
SNAP_M = 25.0
ALONG_M = 3.0                                        # the track's point square across from the line's, give or take
SIMPLIFY_M = 0.3
LANE_MARGIN_M = 1500.0                               # as much line as the layer draws past the area (Trains.public)


def query(bbox) -> str:
    w, s, e, n = bbox
    return (f'[out:json][timeout:120];way["railway"="rail"]["name"="New Haven Line"]["railway:track_ref"~"^(3|4)$"]'
            f"({s - 0.01},{w - 0.02},{n + 0.01},{e + 0.02});out tags geom;")


def lane(model: T.Model, ways: list[list[tuple[float, float]]], side: int, lo: float, hi: float) -> list[list[float]]:
    """[[lon, lat, s]] along the line from lo to hi: each sample at the nearest point of these ways."""
    segs = [(a, b) for w in ways for a, b in zip(w, w[1:])]
    segs = [(T._xy(a[1], a[0]), T._xy(b[1], b[0])) for a, b in segs]
    cell = 200.0
    grid: dict[tuple[int, int], list] = {}
    for (ax, ay), (bx, by) in segs:
        for gx in range(int(min(ax, bx) // cell) - 1, int(max(ax, bx) // cell) + 2):
            for gy in range(int(min(ay, by) // cell) - 1, int(max(ay, by) // cell) + 2):
                grid.setdefault((gx, gy), []).append(((ax, ay), (bx, by)))
    out: list[tuple[float, tuple[float, float] | None, float | None]] = []
    s = lo
    while s <= hi + 1e-6:
        lat, lon, b = model.line.at(s)
        px, py = T._xy(lon, lat)
        nx, ny = -math.cos(math.radians(b)), math.sin(math.radians(b))       # the line's left, going outbound
        best = None
        for (ax, ay), (bx, by) in grid.get((int(px // cell), int(py // cell)), []):
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            f = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
            qx, qy = ax + f * dx, ay + f * dy
            d = math.hypot(qx - px, qy - py)
            off = (qx - px) * nx + (qy - py) * ny
            ahead = (qx - px) * math.sin(math.radians(b)) + (qy - py) * math.cos(math.radians(b))
            if d <= SNAP_M and off * side > 0.5 and abs(ahead) <= ALONG_M and (best is None or d < best[0]):
                best = (d, (qx, qy), off)
        out.append((s, best[1] if best else None, best[2] if best else None))
        s += STEP_M
    known = [(s, off) for s, q, off in out if q is not None]
    if not known:
        raise SystemExit("no track of this side near the line")
    pts = []
    for s, q, off in out:
        if q is None:                                    # its neighbours' offset from the line
            before = [o for k, o in known if k <= s][-1:] or [known[0][1]]
            after = [o for k, o in known if k >= s][:1] or [known[-1][1]]
            off = (before[0] + after[0]) / 2
            lat, lon, b = model.line.at(s)
            px, py = T._xy(lon, lat)
            q = (px - math.cos(math.radians(b)) * off, py + math.sin(math.radians(b)) * off)
        pts.append((q[0] / T.MX, q[1] / T.MY, s))
    keep = {(round(x, 9), round(y, 9)) for x, y in T.simplify([(x, y) for x, y, _ in pts], SIMPLIFY_M)}
    return [[round(x, 7), round(y, 7), round(s, 1)] for x, y, s in pts if (round(x, 9), round(y, 9)) in keep]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--osm", help="an Overpass answer already saved (JSON)")
    ap.add_argument("--gtfs", default=str(GTFS))
    ap.add_argument("--out", default=str(T.TRACKS_FILE))
    a = ap.parse_args(argv)
    model = T.Model(T.parse_static(Path(a.gtfs).read_bytes()))
    if a.osm:
        doc = json.loads(Path(a.osm).read_text())
    else:
        req = Request(OVERPASS, data=urlencode({"data": query(T.DEFAULT_BBOX)}).encode(), headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=180) as r:
            doc = json.loads(r.read())
    lo, hi = max(0.0, model.area[0] - LANE_MARGIN_M), min(model.line.length, model.area[1] + LANE_MARGIN_M)
    lanes = {}
    for key, ref in TRACKS.items():
        ways = [[(g["lat"], g["lon"]) for g in e.get("geometry", [])] for e in doc.get("elements", [])
                if e.get("type") == "way" and e.get("tags", {}).get("name") == "New Haven Line"
                and e.get("tags", {}).get("railway:track_ref") == ref]
        lanes[key] = {"track": ref, "coords": lane(model, ways, SIDE[key], lo, hi)}
    out = {"source": "OpenStreetMap via the Overpass API: the New Haven Line's ways by railway:track_ref (tools/fetch_train_tracks.py)",
           "license": "ODbL 1.0, © OpenStreetMap contributors (https://www.openstreetmap.org/copyright)",
           "built": dt.date.today().isoformat(), "step_m": STEP_M, "lanes": lanes}
    Path(a.out).write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print(f"{a.out}: " + ", ".join(f"{k} (track {v['track']}) {len(v['coords'])} points" for k, v in lanes.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
