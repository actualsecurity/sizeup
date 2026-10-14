#!/usr/bin/env python3
"""Record the aircraft layer's test fixtures (tests/fixtures/aircraft/, GitHub #155).

    adsblol   two answers of adsb.lol's point query around HPN (the radius sizeup asks), ~25 s apart:
              tests/fixtures/aircraft/adsblol_hpn_1.json, adsblol_hpn_2.json (as served, untouched).
    faa       the FAA Releasable Aircraft Database (ReleasableAircraft.zip) trimmed to the US aircraft in
              those two answers plus two others: MASTER.txt and ACFTREF.txt with their real headers, the
              owner's name and address columns blanked (sizeup never reads them; the fixture shouldn't
              carry them either): tests/fixtures/aircraft/faa_registry.zip.

The receiver fixtures (readsb_aircraft.json, dump978_aircraft.json) are not recorded here: there is no
ADS-B receiver yet. readsb_aircraft.json is adsblol_hpn_2.json in readsb's file layout ("now" in seconds,
"aircraft" for "ac", adsb.lol's own dst/dir dropped); dump978_aircraft.json is built by hand from the
fields dump978-fa's skyaware writer emits (skyaware_writer.cc). The tests say so.

Run:  .venv/bin/python tools/record_aircraft_fixtures.py adsblol
      .venv/bin/python tools/record_aircraft_fixtures.py faa [ReleasableAircraft.zip]   (else downloaded, ~70 MB)
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sizeup import USER_AGENT  # noqa: E402
from sizeup.aircraft import ADSBLOL_URL, ARP, FAA_URL  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "aircraft"
RADIUS_NM = 20
EXTRA = ("A00001", "A00002")                      # two more rows, so a lookup has something to miss
REDACT = ("NAME", "STREET", "STREET2", "CITY", "STATE", "ZIP CODE", "COUNTY", "OTHER NAMES(1)", "OTHER NAMES(2)",
          "OTHER NAMES(3)", "OTHER NAMES(4)", "OTHER NAMES(5)")


def get(url: str) -> bytes:
    for _ in range(12):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as r:
                return r.read()
        except Exception as e:                    # adsb.lol rate-limits (429): wait and ask again
            print(f"{url}: {e}; again in 10 s")
            time.sleep(10)
    raise SystemExit(f"{url}: no answer")


def adsblol() -> None:
    url = ADSBLOL_URL.format(lat=f"{ARP[0]:.4f}", lon=f"{ARP[1]:.4f}", radius=RADIUS_NM)
    for n in (1, 2):
        body = get(url)
        json.loads(body)
        (OUT / f"adsblol_hpn_{n}.json").write_bytes(body)
        print(f"adsblol_hpn_{n}.json: {len(json.loads(body).get('ac') or [])} aircraft")
        if n == 1:
            time.sleep(25)


def faa(path: str | None) -> None:
    hexes = set(EXTRA)
    for n in (1, 2):
        for a in json.loads((OUT / f"adsblol_hpn_{n}.json").read_text()).get("ac") or []:
            hexes.add(str(a.get("hex", "")).upper())
    blob = Path(path).read_bytes() if path else get(FAA_URL)
    z = zipfile.ZipFile(io.BytesIO(blob))
    master, codes = [], set()
    with z.open("MASTER.txt") as f:
        r = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        head = next(r)
        col = {k.strip(): i for i, k in enumerate(head)}
        for row in r:
            if len(row) > col["MODE S CODE HEX"] and row[col["MODE S CODE HEX"]].strip() in hexes:
                for k in REDACT:
                    row[col[k]] = ""
                master.append(row)
                codes.add(row[col["MFR MDL CODE"]].strip())
    with z.open("ACFTREF.txt") as f:
        r = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        ref_head = next(r)
        ref = [row for row in r if row and row[0].strip() in codes]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        for name, h, rows in (("MASTER.txt", head, master), ("ACFTREF.txt", ref_head, ref)):
            s = io.StringIO()
            w = csv.writer(s, lineterminator="\r\n")
            w.writerow(h)
            w.writerows(rows)
            zo.writestr(name, "﻿" + s.getvalue())
    (OUT / "faa_registry.zip").write_bytes(out.getvalue())
    print(f"faa_registry.zip: {len(master)} aircraft, {len(ref)} models")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "adsblol":
        adsblol()
    elif what == "faa":
        faa(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        raise SystemExit(__doc__)
