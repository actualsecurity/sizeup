#!/usr/bin/env python3
"""Westchester County Airport (HPN) for the CAD (GitHub #87): the Airport map layer and the airport's places.

Port Chester FD responds to the airport's Alert 2 and above (user, 2026-10-13). This builds, from files
checked into data/airport/ (no network):

  data/khpn_airport.geojson      what the Airport layer draws (sizeup/web/static/airport.js, GET /api/cad/airport):
                                 the airport outline, runways with their numbered ends, taxiways, aprons, hangars
                                 and other buildings, the points of interest (terminal, FBOs, tower, the Command
                                 Post / EOC staging area, the entrances), and, as the "approaches" sub-layer, the
                                 final approach paths to runways 16 and 34 with their fixes (CZIMR, ISLET ...).
  places rows                    in data/gazetteer.sqlite (kind "place", category "airport", source "khpn-*"): what
                                 a note's "@" and the location box offer: the terminal, FBOs, hangars, gates, the
                                 tower, each runway end, each taxiway, the staging area, the entrances. Only these
                                 rows (and their part of meta 'sources') change; tools/fetch_places.py's build
                                 calls add_places() after its own rows, so a full rebuild keeps them.

Sources (each row and feature keeps its own: ``source`` / ``ref``):

* data/airport/khpn_surface.geojson: the aerodrome's aeroway features from OpenStreetMap via the Overpass API
  (© OpenStreetMap contributors, ODbL 1.0), fetched by the shortfinal project 2026-09-30 (its
  tools/fetch_airport_surface.py). source "khpn-osm", ref = the OSM id.
* data/airport/khpn_procedures.geojson: instrument approaches from the FAA CIFP, cycle 261001 (public
  domain), built by shortfinal's tools/fetch_procedures.py 2026-10-01. source "khpn-faa".
* Runway ends (RUNWAY_ENDS below): the FAA Chart Supplement (via airnav.com/airport/KHPN, checked 2026-09-30;
  shortfinal's khpn.toml). source "khpn-faa".
* Address points already in data/gazetteer.sqlite (NYS Address Points, CC0): 240 Airport Rd (the terminal:
  its address point; OSM's "Main Terminal" building is already a gazetteer landmark), 136 Tower Rd (Million Air's published address: "136 Tower Road, Hangar M", millionair.com /
  flightaware.com, 2026-10-13), 1 Airport Emergency Staging Way (the Command Post / EOC, the airport's
  staging area: user, 2026-10-13; the spot is the NYS address point, to be confirmed on the ground).
  TIGER intersections in the gazetteer (public domain) for the entrances. source "khpn-nys" / "khpn-tiger".

Not here: the ARFF (airport fire) station and the Signature FBO's office. OSM has neither as a named
feature (only an unnamed hangar "Signature" on the east side, kept as that) and the FAA diagram doesn't
place them: the department can add them in Settings > Places, or here once known.

Run:  .venv/bin/python tools/fetch_airport.py [--db data/gazetteer.sqlite] [--out data/khpn_airport.geojson]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "airport"
DEFAULT_DB = ROOT / "data" / "gazetteer.sqlite"
DEFAULT_OUT = ROOT / "data" / "khpn_airport.geojson"

ICAO, IATA, NAME = "KHPN", "HPN", "Westchester County Airport"
ADDRESS, ADDRESS_TOWN = "240 Airport Rd", "Rye Brook"       # the terminal's address (NYS address point: Rye Brook)
ARP = (41.0669531, -73.7075661)                               # airport reference point (FAA Chart Supplement)
NEAR_KM = 1.5                                                 # an event this close to the ARP is "at the airport"
# Runway ends (FAA Chart Supplement via airnav.com/airport/KHPN, 2026-09-30): id -> (lat, lon, true heading).
RUNWAY_ENDS = {
    "16": (41.07555312, -73.71330558), "34": (41.06000083, -73.70140237),
    "11": (41.06705197, -73.71576050), "29": (41.06442797, -73.69999812),
}
RUNWAYS = {"16/34": (6549, 150), "11/29": (4451, 150)}        # length, width in feet
# Points of interest that are not an OSM feature: (name, lat, lon, source, ref, address, aliases)
POINTS = [
    ("Airport Terminal (HPN)", 41.068787, -73.704333, "khpn-nys", "NYS address point 240 Airport Rd",
     f"{ADDRESS}, {ADDRESS_TOWN}", ["terminal", "main terminal", "airport terminal", "westchester county airport terminal"]),
    ("Airport Command Post / EOC (staging)", 41.068747, -73.712696, "khpn-nys", "NYS address point 1 Airport Emergency Staging Way",
     "1 Airport Emergency Staging Way, Harrison",
     ["Airport - Command Post/EOC", "airport command post", "airport EOC", "airport staging", "staging way",
      "command post", "EOC"]),
    ("Million Air (FBO, HPN)", 41.062881, -73.710248, "khpn-nys", "NYS address point 136 Tower Rd (Million Air's published address)",
     "136 Tower Rd, Harrison", ["million air", "millionair", "FBO"]),
    ("Airport entrance, Airport Rd at Rye Lake Ave (HPN)", 41.072953, -73.705803, "khpn-tiger",
     "TIGER intersection Airport Rd / Rye Lake Ave", "Airport Rd & Rye Lake Ave, Rye Brook",
     ["airport entrance", "airport road entrance", "terminal entrance", "main entrance"]),
    ("Airport entrance, Tower Rd at Purchase St (HPN)", 41.060502, -73.719019, "khpn-tiger",
     "TIGER intersection Tower Rd / Purchase St", "Tower Rd & Purchase St, Harrison",
     ["airport entrance", "tower road entrance", "west entrance", "purchase street entrance"]),
]
HPN_ALIASES = ["hpn", "khpn", "westchester county airport", "airport"]


def read(name: str) -> dict:
    return json.loads((SRC / name).read_text())


def centroid(geom: dict) -> tuple[float, float]:
    """(lat, lon) of a point, the middle vertex of a line, or the mean of a polygon's outer ring."""
    c = geom["coordinates"]
    if geom["type"] == "Point":
        return c[1], c[0]
    if geom["type"] == "LineString":
        return c[len(c) // 2][1], c[len(c) // 2][0]
    ring = c[0][:-1] if c[0][0] == c[0][-1] else c[0]
    return sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)


def rounded(geom: dict) -> dict:
    def r(x):
        return [r(y) for y in x] if isinstance(x, list) else round(x, 6)
    return {"type": geom["type"], "coordinates": r(geom["coordinates"])}


def bbox(features: list[dict]) -> list[float]:
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for y in c:
                walk(y)
    for f in features:
        walk(f["geometry"]["coordinates"])
    return [round(min(xs), 6), round(min(ys), 6), round(max(xs), 6), round(max(ys), 6)]


# --------------------------------------------------------------------------- the layer

def layer_features(surface: dict, procedures: dict) -> list[dict]:
    """The derived features: properties {layer: airport | approaches, kind, label, ...}, nothing else."""
    out = []

    def add(layer, kind, geom, source, ref, **props):
        out.append({"type": "Feature", "properties": {"layer": layer, "kind": kind, "source": source, "ref": ref, **props},
                    "geometry": rounded(geom)})

    for f in surface["features"]:
        p, g = f["properties"], f["geometry"]
        k = p.get("kind")
        ref = p.get("osm", "")
        if k == "aerodrome":
            add("airport", "aerodrome", g, "khpn-osm", ref, label=NAME)
        elif k == "runway":
            add("airport", "runway", g, "khpn-osm", ref, label=p.get("ref", ""))
        elif k in ("taxiway", "taxilane"):
            add("airport", k, g, "khpn-osm", ref, label=p.get("ref") or p.get("name") or "")
        elif k == "apron":
            add("airport", "apron", g, "khpn-osm", ref, label=p.get("ref") or p.get("name") or "")
        elif k in ("hangar", "building"):
            add("airport", "building", g, "khpn-osm", ref, label=p.get("name") or "", hangar=k == "hangar")
        elif k == "gate":
            name = p.get("name") or p.get("ref") or ""
            if name:
                add("airport", "gate", g, "khpn-osm", ref, label=f"Gate {name}")
        elif k == "tower" and p.get("name"):
            lat, lon = centroid(g)
            add("airport", "poi", {"type": "Point", "coordinates": [lon, lat]}, "khpn-osm", ref, label="Control tower")
    for rid, (lat, lon) in RUNWAY_ENDS.items():
        add("airport", "runway_end", {"type": "Point", "coordinates": [lon, lat]}, "khpn-faa",
            "FAA Chart Supplement", label=rid)
    for name, lat, lon, source, ref, address, _aliases in POINTS:
        short = name.split(" (")[0].replace("Airport entrance, ", "Entrance: ").replace("Airport Terminal", "Terminal")
        add("airport", "poi", {"type": "Point", "coordinates": [lon, lat]}, source, ref, label=short,
            staging=name.startswith("Airport Command Post"))
    sig = next((f for f in surface["features"] if f["properties"].get("name") == "Signature"), None)
    if sig is not None:
        lat, lon = centroid(sig["geometry"])
        add("airport", "poi", {"type": "Point", "coordinates": [lon, lat]}, "khpn-osm", sig["properties"].get("osm", ""),
            label="Signature (FBO)")
    # The approaches: the final segments to 16 and 34 (several procedures share a track) and the fixes on them.
    seen = set()
    for f in procedures["features"]:
        p = f["properties"]
        if p.get("kind") == "approach_path" and p.get("segment") == "final":
            key = (p.get("runway"), json.dumps(rounded(f["geometry"])["coordinates"]))
            if key in seen:
                continue
            seen.add(key)
            add("approaches", "approach", f["geometry"], "khpn-faa", f"CIFP {p.get('proc', '')}",
                label=f"{p.get('name', '')} final", runway=p.get("runway", ""))
    for f in procedures["features"]:
        p = f["properties"]
        if p.get("kind") == "approach_fix" and p.get("role") in ("FAF", "IF", "fix") and p.get("alt_ft") is not None:
            add("approaches", "fix", f["geometry"], "khpn-faa", "CIFP", label=p["ident"], role=p["role"],
                alt_ft=p["alt_ft"], faf=p["role"] == "FAF")
    return out


def build_layer(surface: dict, procedures: dict, today: str) -> dict:
    feats = layer_features(surface, procedures)
    field = [f for f in feats if f["properties"]["kind"] == "aerodrome"]
    return {
        "type": "FeatureCollection",
        "airport": {"icao": ICAO, "iata": IATA, "name": NAME, "lat": ARP[0], "lon": ARP[1], "near_km": NEAR_KM,
                    "address": ADDRESS, "town": ADDRESS_TOWN, "bbox": bbox(field or feats),
                    "runways": {k: {"length_ft": v[0], "width_ft": v[1]} for k, v in RUNWAYS.items()}},
        "sources": [
            {"code": "khpn-osm", "name": "OpenStreetMap via Overpass API (aeroway features of KHPN)",
             "attribution": "© OpenStreetMap contributors", "license": "ODbL 1.0", "fetched": "2026-09-30"},
            {"code": "khpn-faa", "name": "FAA CIFP cycle 261001 (approaches); FAA Chart Supplement (runway ends)",
             "license": "Public domain (US Government)", "fetched": "2026-10-01"},
            {"code": "khpn-nys", "name": "NYS Address Points (via data/gazetteer.sqlite)", "license": "CC0"},
            {"code": "khpn-tiger", "name": "US Census TIGER/Line intersections (via data/gazetteer.sqlite)", "license": "Public domain"},
        ],
        "built": today,
        "features": feats,
    }


# --------------------------------------------------------------------------- the places

RYE_BROOK_WEST = -73.7066     # Rye Brook village's western edge (TIGER): the field west of it is Harrison


def nearest_muni(db: sqlite3.Connection | None, lat: float, lon: float) -> str:
    """The municipality: Harrison west of Rye Brook's edge (the runways, Tower Rd), else that of the nearest
    address point (Rye Brook; Greenwich only past the state line on King St)."""
    if lon < RYE_BROOK_WEST:
        return "Harrison"
    if db is None:
        return ""
    row = db.execute("SELECT muni FROM addresses WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
                     "ORDER BY (lat - ?) * (lat - ?) + (lon - ?) * (lon - ?) * 0.57 LIMIT 1",
                     (lat - 0.02, lat + 0.02, lon - 0.03, lon + 0.03, lat, lat, lon, lon)).fetchone()
    return row[0] if row else ""


def place_rows(surface: dict, db: sqlite3.Connection | None = None) -> list[dict]:
    rows = []

    def row(name, lat, lon, source, ref, address="", aliases=()):
        al = list(dict.fromkeys([*aliases, *HPN_ALIASES]))
        rows.append({"kind": "place", "name": name, "category": "airport", "address": address,
                     "muni": nearest_muni(db, lat, lon), "lat": round(lat, 6), "lon": round(lon, 6),
                     "route": "", "dir": "", "value": "", "source": source, "ref": ref, "aliases": "|".join(al)})

    for name, lat, lon, source, ref, address, aliases in POINTS:
        row(name, lat, lon, source, ref, address, aliases)
    for f in surface["features"]:
        p, g = f["properties"], f["geometry"]
        k, ref = p.get("kind"), p.get("osm", "")
        if k == "hangar" and p.get("name") and p["name"] != "FBO":
            lat, lon = centroid(g)
            if p["name"] == "Signature":
                row("Signature (FBO hangar, HPN)", lat, lon, "khpn-osm", ref, "", ["signature", "signature aviation",
                                                                                   "signature flight support", "FBO"])
            else:
                row(f"{p['name']} (HPN)", lat, lon, "khpn-osm", ref, "", [p["name"], "hangar"])
        elif k == "gate" and (p.get("name") or p.get("ref")):
            lat, lon = centroid(g)
            n = p.get("name") or p.get("ref")
            row(f"Gate {n} (HPN)", lat, lon, "khpn-osm", ref, "", [f"gate {n}", "terminal gate"])
        elif k == "taxiway" and p.get("ref"):
            lat, lon = centroid(g)
            row(f"Taxiway {p['ref']} (HPN)", lat, lon, "khpn-osm", ref, "", [f"taxiway {p['ref']}", f"twy {p['ref']}", p["ref"]])
        elif k == "tower" and p.get("name"):
            lat, lon = centroid(g)
            row("Control tower (HPN)", lat, lon, "khpn-osm", ref, "", ["tower", "control tower", "atc tower"])
    for rid, (lat, lon) in RUNWAY_ENDS.items():
        rw = next(k for k in RUNWAYS if rid in k.split("/"))
        row(f"Runway {rid} end (HPN)", lat, lon, "khpn-faa", "FAA Chart Supplement", "",
            [f"runway {rid}", f"rwy {rid}", f"runway {rw}", f"threshold {rid}", rid])
    return rows


def add_places(db: sqlite3.Connection, rows: list[dict], today: str) -> int:
    """Replace the khpn-* rows of the places table and their entry in meta 'sources' / 'counts'."""
    cols = ["kind", "name", "category", "address", "muni", "lat", "lon", "route", "dir", "value", "source", "ref", "aliases"]
    db.execute("DELETE FROM places WHERE source LIKE 'khpn-%'")
    db.executemany(f"INSERT INTO places ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   [tuple(r[c] for c in cols) for r in rows])
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    srcs = [s for s in json.loads(meta.get("sources") or "[]") if not str(s.get("code", "")).startswith("khpn")]
    srcs.append({"part": "places", "code": "khpn", "name": "Westchester County Airport (tools/fetch_airport.py): OSM aeroway "
                "features (ODbL, © OpenStreetMap contributors), FAA Chart Supplement runway ends and CIFP 261001 "
                "approaches (public domain), NYS address points (CC0) and TIGER intersections (public domain) for the "
                "staging area, Million Air and the entrances", "license": "see each row's source (khpn-osm, khpn-faa, "
                "khpn-nys, khpn-tiger)", "fetched": today, "count": len(rows)})
    db.execute("INSERT OR REPLACE INTO meta VALUES ('sources', ?)", (json.dumps(srcs),))
    counts = json.loads(meta.get("counts") or "{}")
    counts.setdefault("places", {})["khpn"] = len(rows)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('counts', ?)", (json.dumps(counts),))
    db.commit()
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-places", action="store_true", help="only write the layer file")
    a = ap.parse_args(argv)
    today = dt.date.today().isoformat()
    surface, procedures = read("khpn_surface.geojson"), read("khpn_procedures.geojson")
    layer = build_layer(surface, procedures, today)
    Path(a.out).write_text(json.dumps(layer, separators=(",", ":")) + "\n")
    kinds = {}
    for f in layer["features"]:
        kinds[f["properties"]["kind"]] = kinds.get(f["properties"]["kind"], 0) + 1
    print(f"{a.out}: {len(layer['features'])} features ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))})", file=sys.stderr)
    if a.no_places:
        return 0
    if not Path(a.db).is_file():
        print(f"{a.db}: no gazetteer (build it with tools/fetch_addresses.py first)", file=sys.stderr)
        return 1
    db = sqlite3.connect(a.db)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'places'").fetchone():
            print(f"{a.db}: no places table (run tools/fetch_places.py first)", file=sys.stderr)
            return 1
        n = add_places(db, place_rows(surface, db), today)
    finally:
        db.close()
    print(f"{a.db}: {n} airport places", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
