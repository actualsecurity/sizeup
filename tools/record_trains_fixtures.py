#!/usr/bin/env python3
"""Record the Metro-North fixtures for tests/test_trains.py (GitHub #131) from the MTA's public feeds.

    python tools/record_trains_fixtures.py                 # fetch both feeds now (the shared User-Agent)
    python tools/record_trains_fixtures.py RT.pb GTFS.zip  # or trim files already downloaded
    python tools/record_trains_fixtures.py --seq N [S]     # N reads of the realtime feed S s apart (12): a sequence
    python tools/record_trains_fixtures.py --seq A.pb ...  # or trim a sequence already downloaded

Writes tests/fixtures/mta/:
  mnr-rt.pb         the GTFS-realtime feed, only the New Haven Line's trains (and its branches'): the header
                    and those entities byte for byte as the MTA sent them (no re-encoding)
  gtfsmnr.zip       the static GTFS trimmed: routes, stops and agency whole; the New Haven Line's longest
                    shape and one shorter one; one trip per train number in the realtime feed, with its
                    stop_times (what the line's run times are built from)
  recorded.json     when and from where
  seq/<time>.pb     (--seq) a run of realtime reads, one file a feed time (a read whose header time hasn't
                    moved is skipped): the header and the New Haven Line's trains that carry a vehicle position
                    in any of the reads (in every read, with or without one), byte for byte; what the fixes
                    across reads are tested on (bogus positions, fix_at)
  seq/recorded.json when and from where

Sources and terms: sizeup/trains.py (MTA developer data, https://www.mta.info/developers).
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup import trains as T  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "mta"


def entities(body: bytes) -> tuple[bytes, list[bytes]]:
    """(the header field's raw bytes, each entity field's raw bytes), as they are in the message."""
    header, ents, i = b"", [], 0
    while i < len(body):
        start = i
        key, i = T._varint(body, i)
        if key & 7 != 2:
            raise ValueError("unexpected wire type at the top level")
        n, i = T._varint(body, i)
        i += n
        if key >> 3 == 1:
            header = body[start:i]
        elif key >> 3 == 2:
            ents.append(body[start:i])
    return header, ents


def rows(z: zipfile.ZipFile, name: str) -> tuple[list[str], list[dict]]:
    with z.open(name) as f:
        r = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        return list(r.fieldnames or []), list(r)


def write_csv(z: zipfile.ZipFile, name: str, fields: list[str], data: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    w.writerows(data)
    z.writestr(name, buf.getvalue())


def trim_seq(bodies: list[bytes], src: str) -> int:
    """--seq: each read's header and the New Haven Line's trains with a vehicle position in any read, into OUT/seq/."""
    out = OUT / "seq"
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT / "gtfsmnr.zip") as z:      # the routes the static fixture says are ours
        ours = {r["route_id"] for r in rows(z, "routes.txt")[1] if r["route_long_name"] in T.LINES}
    moving = {t["train"] for rt in bodies for t in T.parse_feed(rt)["trains"].values() if t["route_id"] in ours and t["pos"]}
    kept, seen = [], set()
    for rt in bodies:
        header, ents = entities(rt)
        ts = T.parse_feed(rt)["timestamp"]
        if ts in seen:
            continue
        seen.add(ts)
        keep = [e for e in ents if any(t["train"] in moving and t["route_id"] in ours for t in T.parse_feed(e)["trains"].values())]
        (out / f"{ts}.pb").write_bytes(header + b"".join(keep))
        kept.append({"feed_timestamp": ts, "entities": len(keep)})
    (out / "recorded.json").write_text(json.dumps({"recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source": src, "reads": kept},
                                                  indent=1) + "\n")
    print(f"{out}: {len(kept)} reads")
    return 0




def main(argv: list[str]) -> int:
    if argv[:1] == ["--seq"]:
        rest = argv[1:]
        if rest and all(a.endswith(".pb") for a in rest):
            return trim_seq([Path(a).read_bytes() for a in sorted(rest)], "local files")
        n, every = int(rest[0]) if rest else 20, float(rest[1]) if len(rest) > 1 else 12.0
        bodies = []
        for k in range(n):
            t0 = time.time()
            bodies.append(T.http_get(T.RT_URL, 30, T.MAX_RT_BYTES)[2])
            if k < n - 1:
                time.sleep(max(0.0, every - (time.time() - t0)))
        return trim_seq(bodies, T.RT_URL)
    if len(argv) == 2:
        rt, gtfs = Path(argv[0]).read_bytes(), Path(argv[1]).read_bytes()
        src = "local files"
    else:
        _, _, rt = T.http_get(T.RT_URL, 30, T.MAX_RT_BYTES)
        _, _, gtfs = T.http_get(T.GTFS_URL, 120, T.MAX_STATIC_BYTES)
        src = f"{T.RT_URL} and {T.GTFS_URL}"
    feed = T.parse_feed(rt)
    zin = zipfile.ZipFile(io.BytesIO(gtfs))
    rfields, routes = rows(zin, "routes.txt")
    ours = {r["route_id"] for r in routes if r["route_long_name"] in T.LINES}
    main_route = {r["route_id"] for r in routes if r["route_long_name"] == T.MAIN}
    keep_ents = []
    header, ents = entities(rt)
    for e in ents:
        one = T.parse_feed(e)                     # an entity alone parses as a message with no header
        if any(t["route_id"] in ours for t in one["trains"].values()):
            keep_ents.append(e)
    numbers = {t["train"] for t in feed["trains"].values() if t["route_id"] in ours}

    tfields, trips = rows(zin, "trips.txt")
    sfields, shapes = rows(zin, "shapes.txt")
    by_shape: dict[str, list[dict]] = {}
    for r in shapes:
        by_shape.setdefault(r["shape_id"], []).append(r)
    nh_shapes = {t["shape_id"] for t in trips if t["route_id"] in main_route}
    lengths = {sid: sum(T.metres(float(a["shape_pt_lat"]), float(a["shape_pt_lon"]), float(b["shape_pt_lat"]), float(b["shape_pt_lon"]))
                        for a, b in zip(sorted(by_shape[sid], key=lambda r: int(r["shape_pt_sequence"])),
                                        sorted(by_shape[sid], key=lambda r: int(r["shape_pt_sequence"]))[1:]))
               for sid in nh_shapes if sid in by_shape}
    ranked = sorted(lengths, key=lengths.get, reverse=True)
    keep_shapes = {ranked[0], ranked[len(ranked) // 2]}
    kept, seen = [], set()
    for t in trips:
        if t["route_id"] in ours and t["trip_short_name"] in numbers and t["trip_short_name"] not in seen:
            seen.add(t["trip_short_name"])
            kept.append(t | {"shape_id": t["shape_id"] if t["shape_id"] in keep_shapes else ranked[0]})
    for sid in keep_shapes:                       # both shapes on at least one New Haven trip
        if not any(t["shape_id"] == sid and t["route_id"] in main_route for t in kept):
            t = next(t for t in kept if t["route_id"] in main_route)
            kept.append(t | {"trip_id": t["trip_id"] + "-shape", "shape_id": sid})
    ids = {t["trip_id"] for t in kept}
    stfields, stop_times = rows(zin, "stop_times.txt")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mnr-rt.pb").write_bytes(header + b"".join(keep_ents))
    with zipfile.ZipFile(OUT / "gtfsmnr.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("agency.txt", "stops.txt", "routes.txt"):
            z.writestr(name, zin.read(name))
        write_csv(z, "trips.txt", tfields, kept)
        write_csv(z, "shapes.txt", sfields, [r for sid in sorted(keep_shapes) for r in by_shape[sid]])
        write_csv(z, "stop_times.txt", stfields, [r for r in stop_times if r["trip_id"] in ids])
    (OUT / "recorded.json").write_text(json.dumps({
        "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source": src, "feed_timestamp": feed["timestamp"],
        "entities": len(keep_ents), "trips": len(kept), "shapes": sorted(keep_shapes)}, indent=1) + "\n")
    print(f"{OUT}: {len(keep_ents)} trains, {len(kept)} trips, shapes {sorted(keep_shapes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
