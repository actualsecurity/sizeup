#!/usr/bin/env python3
"""Fill a LOCAL sizeup with a few invented calls, so the pages have something on them (docs/development.md).

    python tools/dev_seed.py                                  all-in-one or a local hub on :8090: the incidents
    python tools/dev_seed.py --base http://127.0.0.1:8090 --token "$SIZEUP_STATION_TOKEN"
                                                              a local hub: also radio traffic, as the station sends it

Everything is invented: the addresses (Example Ave, Sample St), the remarks and the transcripts. Through the same HTTP
API the pages use (POST /api/cad/incident, /dispatch, /note, /close) and, with --token, the station's
POST /api/station/events (sizeup/cloud/hub.py): FG1 transmissions from the units on a call, so they land on its radio
log. Run it again for more. Only ever to this machine: any other host is refused.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

LOCAL = ("127.0.0.1", "localhost", "::1")
OPERATOR = "dev-seed"

# Invented calls near the map's home (config.example.toml [map]): never a real address or caller.
CALLS = [
    {"type": "STRU", "subtype": "RESD", "address": "100 Example Ave", "lat": 41.0118, "lon": -73.6652,
     "remarks": "Smoke from the second floor, occupants out", "units": ["E60", "E63", "L31"], "close": "",
     "radio": [("E60", "Engine 60 on scene, two story frame, smoke showing from the second floor"),
               ("E63", "Engine 63 is on scene, we'll take the hydrant at the corner"),
               ("L31", "Ladder 31 on location, going to the roof")]},
    {"type": "ALARM", "subtype": "COMM", "address": "25 Sample St", "lat": 41.0062, "lon": -73.6701,
     "remarks": "General alarm, keyholder en route", "units": ["E61"], "close": "FALSE",
     "radio": [("E61", "Engine 61 on scene, nothing showing, checking the panel")]},
    {"type": "MVA", "subtype": "NOINJ", "address": "Example Ave / Sample St", "lat": 41.0151, "lon": -73.6612,
     "remarks": "Two cars, no injuries reported", "units": [], "close": "", "radio": []},
]


def call(base: str, path: str, body: dict | None = None, token: str = "") -> dict:
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{req.method} {path}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}") from None


def silence_wav(seconds: float = 1.0, rate: int = 8000) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(int(seconds * rate) * 2))
    return base64.b64encode(buf.getvalue()).decode()


def traffic(seq: int, rig: str, radio_id: str, text: str, at: float) -> dict:
    """One FG1 transmission as the station sends it; its MDC radio ID (from this CAD's own registry) names the rig."""
    tid = f"FG1-seed-{secrets.token_hex(4)}"
    data = {"id": tid, "source": "FG1", "start": at, "end": at + 3, "text": text, "raw": text, "status": "done",
            "unit": rig, "rig": rig, "talkgroup": None, "radio_id": radio_id, "confidence": 0.9, "model": "dev-seed",
            "attach": True, "pairs": None}
    return {"seq": seq, "at": at, "type": "traffic", "data": data, "audio": silence_wav()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8090", help="the local sizeup (default %(default)s)")
    ap.add_argument("--token", default=os.environ.get("SIZEUP_STATION_TOKEN", ""),
                    help="a local hub's station token (default: $SIZEUP_STATION_TOKEN): adds radio traffic")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")
    host = urllib.parse.urlsplit(base).hostname or ""
    if host not in LOCAL:
        print(f"refused: {host or base} is not this machine (dev_seed only fills a local instance)", file=sys.stderr)
        return 2
    role = call(base, "/healthz").get("role", "")
    units = {u["name"]: u["id"] for u in call(base, "/api/cad/state").get("units", [])}
    radio_ids: dict[str, str] = {}                # unit -> an MDC radio ID of its (confirmed ones first)
    for r in sorted(call(base, "/api/cad/radios").get("radios", []), key=lambda r: r.get("source") != "confirmed"):
        if r.get("system") == "mdc" and r.get("unit"):
            radio_ids.setdefault(r["unit"], r["radio_id"])
    radio: list[tuple[int, dict]] = []            # (incident id, transmission)
    closes: list[tuple[int, str]] = []
    now = time.time()
    for c in CALLS:
        inc = call(base, "/api/cad/incident", {k: c[k] for k in ("type", "subtype", "address", "lat", "lon", "remarks")}
                   | {"operator": OPERATOR})
        iid = inc.get("id") or inc.get("incident", {}).get("id")
        ids = [units[n] for n in c["units"] if n in units]
        if ids:
            call(base, f"/api/cad/incident/{iid}/dispatch", {"unit_ids": ids, "operator": OPERATOR})
            call(base, f"/api/cad/incident/{iid}/note", {"text": "Command established",
                                                          "units": c["units"][:1], "operator": OPERATOR})
        for rig, text in c["radio"]:
            radio.append((iid, traffic(len(radio) + 1, rig, radio_ids.get(rig, ""), text, now + len(radio))))
        if c["close"]:
            closes.append((iid, c["close"]))
        print(f"incident {iid}: {c['type']}/{c['subtype']} {c['address']}" + (f" · {', '.join(c['units'])}" if ids else ""))
    if radio and args.token and role == "hub":
        r = call(base, "/api/station/events", {"epoch": f"dev-seed-{secrets.token_hex(4)}",
                                               "events": [ev for _, ev in radio]}, token=args.token)
        for iid, ev in radio:                     # a rig with no radio ID in the registry: put on its call by hand
            if not ev["data"]["radio_id"]:
                call(base, f"/api/cad/incident/{iid}/link", {"traffic_id": ev["data"]["id"], "operator": OPERATOR})
        print(f"radio traffic: {r.get('applied', 0)} transmission(s)")
    elif radio:
        print("radio traffic: skipped (a local hub and --token / SIZEUP_STATION_TOKEN add it)")
    for iid, disposition in closes:
        call(base, f"/api/cad/incident/{iid}/close", {"disposition": disposition, "operator": OPERATOR})
        print(f"incident {iid}: closed {disposition}")
    print(f"open {base}/cad")
    return 0


if __name__ == "__main__":
    sys.exit(main())
