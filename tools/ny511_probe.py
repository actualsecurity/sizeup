#!/usr/bin/env python3
"""One look at 511NY's feeds on CARS, by hand, before anything goes to the hub (GitHub #73, #188).

    .venv/bin/python tools/ny511_probe.py [--keychain] [--save DIR] [--hub] [--hub-events] [--wzdx] [--no-known]

One request per feed, never a loop (511NY asks for once every 5 minutes at most): the public message signs
(signs.json), the CARS API's events and the CARS-Hub's CCTV inventory (data/cctv.xml), both HTTP Basic with 511NY's
username and password; with --wzdx the API's WZDx too, with --hub-events the CARS-Hub's FEU-g events and Waze CIFS
feeds (6-8 MB each), and with --hub the CARS-Hub's public page and the feeds and schemas it lists (no login: the
page is public and each feed asks for HTTP Basic itself). For each it prints the HTTP status and content type, the
size, the records and their top-level keys (or the XML's element names), and how many fall in our area ([roads]
bbox: Port Chester, Rye Brook and the neighbours), parsed by sizeup/roads.py itself. The CCTV cameras are compared
with the ones sizeup already has (the Thruway's list and the county DPW's, one public request each, unless
--no-known): how many are the same camera (within 150 m, sizeup.roads.same_camera), how many look new, by road.

The username and password:
    SIZEUP_511NY_USER, SIZEUP_511NY_PASS           from the environment, or with --keychain from the macOS Keychain:
    security find-generic-password -a pcfd -s sizeup-511ny-carshub-user -w   (and -s sizeup-511ny-carshub-pass)
Nothing of them is ever printed, logged or written: they go only into the request's Authorization header (not
carried along a redirect), and anything printed is scrubbed of them; no cookie or session is kept. --save DIR
writes each response's body and headers as received (for fixtures; the responses carry no credentials), and
nothing by default. Exit 0: every feed tried answered 200; 1: one didn't; 2: bad arguments.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sizeup import roads as r  # noqa: E402

ACCOUNT = "pcfd"
KC_USER, KC_PASS = "sizeup-511ny-carshub-user", "sizeup-511ny-carshub-pass"
LIMIT = 64 * 1024 * 1024


def keychain(service: str) -> str:
    """A generic password from the login keychain; "" when there is none. Its value never reaches the terminal."""
    try:
        out = subprocess.run(["security", "find-generic-password", "-a", ACCOUNT, "-s", service, "-w"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


class Probe:
    def __init__(self, auth: r.BasicAuth, bbox, save: Path | None, extra_secrets=(), fetch=r.http_get, out=print):
        self.auth, self.bbox, self.save, self.fetch = auth, bbox, save, fetch
        self.secrets = tuple(x for x in (*auth.secrets(), *extra_secrets) if x)
        self._out = out
        self.ok = True

    def say(self, text: str = "") -> None:
        self._out(r.redact(text, self.secrets))

    def get(self, name: str, url: str, auth: bool) -> tuple[int, dict, bytes] | None:
        self.say(f"\n== {name}: GET {url}" + (" (HTTP Basic)" if auth else " (public)"))
        try:
            code, headers, body = self.fetch(url, r.TIMEOUT_S * 3, LIMIT, self.auth.header() if auth else None)
        except HTTPError as e:
            ctype = (e.headers or {}).get("Content-Type", "") if e.headers else ""
            try:
                text = e.read(400).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - only for the message
                text = ""
            self.say(f"   HTTP {e.code} {ctype}  {' '.join(text.split())[:200]}")
            if e.code in (401, 403):
                self.say("   refused: the API takes HTTP Basic (its Swagger definition); check the username and password")
            self.ok = False
            return None
        except (URLError, OSError, ValueError) as e:
            self.say(f"   failed: {type(e).__name__}: {getattr(e, 'reason', e)}")
            self.ok = False
            return None
        self.say(f"   HTTP {code}  {headers.get('content-type', '?')}  {len(body):,} bytes"
                 + (" (sent gzipped)" if headers.get("content-encoding", "").lower() == "gzip" else "")
                 + "".join(f"  {k}: {headers[k]}" for k in ("last-modified", "etag", "age", "x-total-count") if headers.get(k)))
        if self.save is not None:
            self.save.mkdir(parents=True, exist_ok=True)
            ext = ".xml" if body.lstrip()[:1] == b"<" else ".json"
            (self.save / f"{name}{ext}").write_bytes(body)
            (self.save / f"{name}.headers.json").write_text(json.dumps(headers, indent=1, sort_keys=True) + "\n")
            self.say(f"   saved {self.save / (name + ext)}")
        if code != 200:
            self.ok = False
        return code, headers, body

    def shape(self, body: bytes) -> None:
        """The records and their top-level keys, or the XML's element names."""
        text = body.decode("utf-8-sig", errors="replace").lstrip()
        if text[:1] == "<":
            try:
                root = ET.fromstring(text)
            except ET.ParseError as e:
                self.say(f"   not well-formed XML: {e}")
                return
            tags = collections.Counter(e.tag.split("}")[-1] for e in root.iter())
            self.say(f"   XML root <{root.tag.split('}')[-1]}>, {sum(tags.values()):,} elements; names: "
                     + ", ".join(f"{t} {n}" for t, n in tags.most_common(25)))
            return
        try:
            doc = json.loads(text)
        except ValueError as e:
            self.say(f"   not JSON: {e}")
            return
        if isinstance(doc, dict):
            self.say("   a JSON object; keys: " + ", ".join(list(doc)[:30]))
            doc = next((v for v in doc.values() if isinstance(v, list)), [])
        if isinstance(doc, list):
            keys = collections.Counter(k for x in doc if isinstance(x, dict) for k in x)
            self.say(f"   {len(doc):,} records; keys: " + ", ".join(f"{k} {n}" for k, n in keys.most_common(30)))

    def published(self, headers: dict) -> float | None:
        from email.utils import parsedate_to_datetime
        try:
            return parsedate_to_datetime(headers["last-modified"]).timestamp()
        except (KeyError, TypeError, ValueError, IndexError):
            return None


def known_cameras(probe: Probe) -> list[dict]:
    """The cameras sizeup has in our area: the Thruway's list and the county DPW's (one public request each)."""
    out = []
    for name, url, parse in (("thruway cameras", r.THRUWAY_URLS["cameras"], r.parse_thruway_cameras),
                             ("county cameras", r.COUNTY_URLS["cameras"], r.parse_county_cameras)):
        got = probe.get(name.replace(" ", "_"), url, False)
        if got and got[0] == 200:
            cams = parse(r.decode(got[2]), probe.bbox)
            src = r.THRUWAY if name.startswith("thruway") else r.COUNTY
            for c in cams:
                c["source"] = src
            probe.say(f"   {len(cams)} in our area")
            out += cams
    return out


def _points_in(body: bytes, bbox) -> int:
    """Positions in our area in an XML feed: each <latitude> with its next <longitude> (microdegrees or degrees), and
    Waze CIFS's "lat lon" polylines' first points."""
    import re
    text = body.decode("utf-8", errors="replace")
    n = 0
    for lat, lon in re.findall(r"<latitude>\s*(-?[\d.]+)\s*</latitude>\s*<longitude>\s*(-?[\d.]+)\s*</longitude>", text):
        la, lo = r._microdeg(lat), r._microdeg(lon)
        n += r.in_bbox(la, lo, bbox)
    for lat, lon in re.findall(r"<polyline>\s*(-?[\d.]+)\s+(-?[\d.]+)", text):
        n += r.in_bbox(float(lat), float(lon), bbox)
    return n


def main(argv: list[str] | None = None, fetch=r.http_get, out=print, keychain_get=keychain) -> int:
    ap = argparse.ArgumentParser(description="One request to each of 511NY's feeds on CARS (no loops).")
    ap.add_argument("--keychain", action="store_true", help=f"read the username and password from the macOS Keychain "
                    f"(account {ACCOUNT}: {KC_USER}, {KC_PASS})")
    ap.add_argument("--save", metavar="DIR", help="save each raw response (body and headers) into DIR")
    ap.add_argument("--hub", action="store_true", help="list the CARS-Hub page's feeds and schemas (one public GET)")
    ap.add_argument("--hub-events", action="store_true", help="also the CARS-Hub's FEU-g and Waze CIFS feeds (one GET each)")
    ap.add_argument("--hub-base", default=r.CARS_HUB, help="the CARS-Hub's base address ([roads] ny511_hub)")
    ap.add_argument("--wzdx", action="store_true", help="also the CARS API's WZDx feed (one more request)")
    ap.add_argument("--no-known", action="store_true", help="don't fetch the Thruway's and the county's camera lists")
    ap.add_argument("--bbox", default=",".join(f"{x:g}" for x in r.DEFAULT_BBOX), help="west,south,east,north")
    a = ap.parse_args(argv)
    try:
        bbox = tuple(float(x) for x in a.bbox.split(","))
        assert len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]
    except (ValueError, AssertionError):
        out("--bbox: west,south,east,north")
        return 2
    if a.keychain:
        user, password = keychain_get(KC_USER), keychain_get(KC_PASS)
        where = f"the Keychain ({KC_USER}, {KC_PASS})"
    else:
        user, password = os.environ.get(r.ENV_USER, ""), os.environ.get(r.ENV_PASS, "")
        where = f"the environment ({r.ENV_USER}, {r.ENV_PASS})"
    base = a.hub_base.rstrip("/") + "/"
    auth = r.BasicAuth(user, password)
    probe = Probe(auth, bbox, Path(a.save) if a.save else None, fetch=fetch, out=out)
    probe.say(f"511NY probe: area {bbox}; credentials from {where}: "
              + ("username and password found" if auth else "not found (the events and CARS-Hub are skipped)"))

    got = probe.get("signs", r.NY511_URLS["signs"], False)
    if got and got[0] == 200:
        probe.shape(got[2])
        signs = r.parse_cars_signs(got[2], bbox, {"published": probe.published(got[1]), "now": None})
        by = collections.Counter(s.get("status") or "?" for s in signs)
        probe.say(f"   {len(signs)} in our area: " + ", ".join(f"{k} {v}" for k, v in by.most_common())
                  + "; by road: " + ", ".join(f"{k} {v}" for k, v in collections.Counter(s["road"] for s in signs).most_common()))
        pub = probe.published(got[1])
        if pub is not None:
            import time
            probe.say(f"   published {(time.time() - pub) / 60:.0f} min ago (Last-Modified)")

    if auth:
        got = probe.get("events", r.NY511_URLS["events"], True)
        if got and got[0] == 200:
            probe.shape(got[2])
            evs = r.parse_cars_events(got[2], bbox)
            by = collections.Counter(e["layer"] for e in evs)
            probe.say(f"   {len(evs)} in our area: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
            for e in evs[:8]:
                probe.say(f"     {e['layer']:9} {e['road'] or '?'} {e['dir']}: {e['description'][:100]}")
        if a.wzdx:
            got = probe.get("wzdx", r.NY511_URLS["wzdx"], True)
            if got and got[0] == 200:
                probe.shape(got[2])
                from sizeup.roadprojects import parse_wzdx
                try:
                    probe.say(f"   {len(parse_wzdx(json.loads(got[2]), r.SOURCE, bbox))} work zones in our area")
                except ValueError as e:
                    probe.say(f"   not WZDx JSON: {e}")
        got = probe.get("cctv", r.hub_cctv(base), True)
        if got and got[0] == 200:
            probe.shape(got[2])
            try:
                cams = r.parse_carshub_cctv(got[2], bbox)
            except ValueError as e:
                probe.say(f"   not parsed: {e}")
                cams = []
            vid = sum(c["has_video"] for c in cams)
            probe.say(f"   {len(cams)} cameras in our area, {vid} with video; by road: "
                      + ", ".join(f"{k} {v}" for k, v in collections.Counter(c["road"] for c in cams).most_common()))
            hosts = collections.Counter(urlsplit(u).hostname for c in cams for u in (c["url"], c["video"]) if u)
            probe.say("   picture and video hosts: " + ", ".join(f"{k} {v}" for k, v in hosts.most_common()))
            if cams and not a.no_known:
                ours = known_cameras(probe)
                cmp = r.compare_cameras(cams, ours)
                probe.say("\n== cameras: " + r.compare_text(cmp))
                new = [c for c in cams if c["id"] in set(cmp["new_ids"])]
                for c in new[:40]:
                    probe.say(f"     new: {c['id']:>6} {c['road'] or '?':12} {c['name'][:70]}" + ("  (video)" if c["has_video"] else ""))
        if a.hub_events:
            for name, path in (("feu-g", "data/feu-g.xml"), ("waze-cifs", "data/waze/cifs.xml")):
                got = probe.get(name, base + path, True)
                if got and got[0] == 200:
                    probe.shape(got[2])
                    probe.say(f"   positions in our area: {_points_in(got[2], bbox)}")
    if a.hub:
        got = probe.get("hub", base + "index.jsf", False)
        if got and got[0] == 200:
            import re
            links = re.findall(r'href="((?:data|schemas)/[^"]+)"', got[2].decode("utf-8", errors="replace"))
            probe.say("   feeds: " + ", ".join(base + x for x in links if x.startswith("data/")))
            probe.say("   schemas: " + ", ".join(base + x for x in links if x.startswith("schemas/")))
            if re.search(r"captcha|one-time|two-factor|<input[^>]+type=\"password\"", got[2].decode("utf-8", "replace"), re.I):
                probe.say("   the page asks for a login (a password field or a captcha): stopped here, nothing posted")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    sys.exit(main())
