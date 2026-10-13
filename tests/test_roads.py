"""The highway layers (sizeup/roads.py, #73, #86): cameras, events and message signs for the maps from
the NYS Thruway Authority's public feeds and from 511NY's, merged; the camera snapshot proxy; and the
mile markers and exits from the gazetteer.

Never a provider or a camera itself (conftest.py refuses any host but this one, and turns the Thruway
provider's addresses off: tests hand in their own). The 511NY feeds are the fixtures in
tests/fixtures/511ny/, in the shapes 511NY has documented (not recordings: access is by request,
https://511ny.org/help/24; re-record them from the approved feeds under the same names). The Thruway
feeds in tests/fixtures/thruway/ are recorded from webdataapi.thruway.ny.gov on 2026-10-13, trimmed
to the records around Port Chester plus two elsewhere."""

import json
import re
import sqlite3
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sizeup import roads as roads_mod
from sizeup.config import ConfigError, load
from sizeup.roads import (IMAGE_STALE_S, IMAGE_TTL_S, REFUSED_S, UA, Roads, decode, highway_places, parse_cameras,
                          parse_events, parse_signs, parse_thruway_cameras, parse_thruway_events, parse_thruway_signs)
from tests.test_access import AUD, TEAM, jwks, jwt, keys, req  # noqa: F401  (fixtures)
from tests.test_cloud import HubRun, hub_cfg
from tests.test_settings import post

FIX = Path(__file__).parent / "fixtures" / "511ny"
TFIX = Path(__file__).parent / "fixtures" / "thruway"
TURLS = {"cameras": "https://thruway.example/api/data/trafficcameras/xml", "events": "https://thruway.example/api/data/events/xml",
         "signs": "https://thruway.example/api/data/dmsstatus/xml"}
KEY = "SECRETkey-0123456789abcdef"
URLS = {"events": "https://feeds.example/events?key={key}&format=json",
        "cameras": "https://feeds.example/cameras?key={key}",
        "signs": "https://feeds.example/signs?key={key}&format=json"}
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 50


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


class Clock:
    def __init__(self, t=1_791_915_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeFetch:
    """The fetch Roads is given: feeds and images from the fixtures, every URL recorded."""

    def __init__(self, cameras="cameras.json"):
        self.urls: list[str] = []
        self.feeds = {"events": fixture("events.json"), "cameras": fixture(cameras), "signs": fixture("signs.json")}
        self.thruway = {"events": (TFIX / "events.xml").read_bytes(), "cameras": (TFIX / "trafficcameras.xml").read_bytes(),
                        "signs": (TFIX / "dmsstatus.xml").read_bytes()}
        self.fail: dict[str, Exception] = {}
        self.image = (200, {"content-type": "image/jpeg", "last-modified": formatdate(1_791_914_990, usegmt=True)}, JPEG)

    def __call__(self, url, timeout, limit):
        self.urls.append(url)
        for feed, body in self.feeds.items():
            if url.startswith(f"https://feeds.example/{feed}?"):
                if feed in self.fail:
                    raise self.fail[feed]
                return 200, {"content-type": "application/json"}, body
        for feed, body in self.thruway.items():
            if url == TURLS[feed]:
                if f"thruway:{feed}" in self.fail:
                    raise self.fail[f"thruway:{feed}"]
                return 200, {"content-type": "application/xml"}, body
        if url.startswith("https://511ny.org/map/Cctv/") or url.startswith("https://nyssnapshot.com/thumbs/"):
            if "image" in self.fail:
                raise self.fail["image"]
            return self.image
        raise AssertionError(f"unexpected fetch {url}")


def make(tmp_path=None, key=KEY, urls=URLS, fetch=None, clock=None, thruway=False, county=False, **kw):
    """A Roads with 511NY on (the fixtures) and, with thruway=True, the Thruway's feeds (the fixtures) too."""
    logs = []
    r = Roads(key, urls, cache=(tmp_path / "roads511.json") if tmp_path else None, fetch=fetch or FakeFetch(),
              log=logs.append, clock=clock or Clock(), gazetteer=kw.pop("gazetteer", Path("/nonexistent")),
              thruway=thruway, thruway_urls=kw.pop("thruway_urls", TURLS if thruway else {}), county=county, **kw)
    r.logs = logs
    return r


def everything(r: Roads):
    for f in ("events", "signs", "cameras"):
        assert r.refresh(f), (f, [p.status[f] for p in r.providers])


# ---- parsing --------------------------------------------------------------------------------------------


def test_events_parse_into_layers_and_only_our_area():
    ev = parse_events(decode(fixture("events.json")))
    by = {e["id"]: e for e in ev}
    assert set(by) == {"NYSDOT-1001", "NYSDOT-1002", "NYSDOT-1003"}          # the Albany crash is far outside
    crash, work, ramp = by["NYSDOT-1001"], by["NYSDOT-1002"], by["NYSDOT-1003"]
    assert (crash["layer"], crash["closed"], crash["road"], crash["dir"], crash["severity"]) == (
        "incidents", False, "I-95", "Northbound", "Major")
    assert crash["lanes"] == "1 Right lane blocked" and crash["updated"] == 1791914700 and crash["end"] is None
    assert (work["layer"], work["closed"], work["end"]) == ("roadwork", True, 1792000000)   # lanes closed overnight
    assert (ramp["layer"], ramp["closed"]) == ("incidents", True)                          # a closure: IsFullClosure
    assert "Description" not in json.dumps(ev) and "url" not in ev[0]


def test_cameras_from_the_api_json_and_the_inventory_xml():
    cams = {c["id"]: c for c in parse_cameras(decode(fixture("cameras.json")))}
    assert set(cams) == {"4616", "4620", "4631"}
    assert cams["4616"] == {"id": "4616", "name": "I-95 at Exit 21 - Port Chester", "road": "I-95", "dir": "Northbound",
                            "lat": 41.0012, "lon": -73.6748, "url": "https://511ny.org/map/Cctv/4616",
                            "video": "https://s58.nysdot.skyvdn.com:443/rtplive/R11_008/playlist.m3u8", "has_video": True,
                            "off": False, "cadence": 60}
    assert cams["4631"]["off"] and cams["4631"]["dir"] == ""                  # Disabled; "Unknown" direction dropped
    assert not cams["4620"]["has_video"]
    xml = {c["id"]: c for c in parse_cameras(decode(fixture("cameras.xml")))}
    assert set(xml) == {"NYSDOT-R11-008", "CTDOT-I95-GREENWICH-2"}           # the Bronx one is west of our box
    pc = xml["NYSDOT-R11-008"]
    assert (pc["name"], pc["road"], pc["dir"], pc["url"], pc["has_video"], pc["off"]) == (
        "I-95 at Exit 21 - Port Chester", "I-95", "Southbound", "https://511ny.org/map/Cctv/9001", True, False)
    assert xml["CTDOT-I95-GREENWICH-2"]["off"]                                 # its only view is disabled


def test_signs_keep_their_lines_and_blank_is_blank():
    signs = {s["id"]: s for s in parse_signs(decode(fixture("signs.json")))}
    assert set(signs) == {"VMS-95-NB-21", "VMS-287-EB-9"}
    assert signs["VMS-95-NB-21"]["lines"] == ["CRASH AT EXIT 21", "RIGHT LANE BLOCKED", "EXPECT DELAYS"]
    assert signs["VMS-287-EB-9"]["lines"] == []


def test_the_bbox_is_what_is_kept():
    near_albany = (-73.9, 42.6, -73.6, 42.9)
    assert [e["id"] for e in parse_events(decode(fixture("events.json")), near_albany)] == ["NYSDOT-9001"]
    assert [c["id"] for c in parse_cameras(decode(fixture("cameras.json")), near_albany)] == ["5001"]


def test_an_html_page_or_junk_is_an_error_not_an_empty_map():
    # what 511ny.org's old /api/... paths answer now: the new site's page
    with pytest.raises(ValueError, match="HTML"):
        decode(b"<!doctype html><html><head><script>fetch('/configs/main.json')</script>")
    with pytest.raises(ValueError):
        decode(b"<Cameras><Camera>")
    with pytest.raises(ValueError):
        decode(b"nope")


# ---- the client: schedule, cache, no key -------------------------------------------------------------------


def test_a_refresh_reads_each_feed_with_the_key_and_nothing_else_carries_it(tmp_path):
    f = FakeFetch()
    r = make(tmp_path, fetch=f)
    assert r.configured and r.why_not() == "" and r.due() == [("511ny", "cameras"), ("511ny", "events"), ("511ny", "signs")]
    everything(r)
    assert sorted(f.urls) == sorted(u.replace("{key}", KEY) for u in URLS.values())
    pub = r.public()
    assert pub["configured"] and pub["attribution"] == "powered by 511NY" and pub["link"] == "https://511ny.org/"
    assert [c["id"] for c in pub["cameras"]] == ["4631", "4620", "4616"]
    assert all(set(c) == {"id", "name", "road", "dir", "mile", "lat", "lon", "has_video", "off", "source", "cadence", "down",
                          "down_since", "last_image"} for c in pub["cameras"])
    assert all(c["source"] == "511NY" for c in pub["cameras"]) and all(e["source"] == "511NY" for e in pub["events"])
    assert [s["name"] for s in pub["sources"]] == ["511ny"]
    st = pub["sources"][0]["status"]
    assert {s: st[s]["n"] for s in st} == {"cameras": 3, "events": 3, "signs": 2} and pub["sources"][0]["at"] == r.clock()
    for blob in (json.dumps(pub), "\n".join(r.logs)):                     # no key, feed or camera address
        assert KEY not in blob and "feeds.example" not in blob and "Cctv" not in blob
    cache = (tmp_path / "roads511.json").read_text()
    assert KEY not in cache and "feeds.example" not in cache and "511ny.org/map/Cctv/4616" in cache   # (for the proxy)
    assert r.due() == []                                                       # each again after its interval


def test_the_schedule_respects_the_limit(tmp_path):
    clock = Clock()
    r = make(tmp_path, clock=clock)
    everything(r)
    clock.t += 89
    assert r.ny.due() == []
    clock.t += 2
    assert r.ny.due() == ["events"]
    clock.t += 300
    assert r.ny.due() == ["events", "signs"]
    clock.t += 3600
    assert r.ny.due() == ["cameras", "events", "signs"]
    with pytest.raises(ConfigError, match="10 calls a minute"):
        load_cfg(tmp_path, "[roads]\nevents_every_s = 20\n")
    # whatever is asked for, never faster than the floors (5 calls a minute for all three)
    fast = make(every={"cameras": 1, "events": 1, "signs": 1})
    assert fast.ny.every == {"cameras": 600.0, "events": 60.0, "signs": 60.0}


def test_the_disk_cache_saves_calls_after_a_restart(tmp_path):
    clock = Clock()
    make(tmp_path, clock=clock).refresh("cameras")
    f = FakeFetch()
    again = make(tmp_path, fetch=f, clock=clock)
    assert [c["id"] for c in again.public()["cameras"]] == ["4631", "4620", "4616"]
    assert again.due() == [("511ny", "events"), ("511ny", "signs")] and f.urls == []
    other_area = make(tmp_path, clock=clock, bbox=(-73.9, 42.6, -73.6, 42.9))
    assert other_area.public()["cameras"] == []                                # another area: not this cache
    (tmp_path / "roads511.json").write_text("{not json")
    assert make(tmp_path, clock=clock).public()["cameras"] == []               # a broken cache: fetched afresh


def test_no_key_or_no_feed_address_fetches_nothing_and_says_so(tmp_path):
    f = FakeFetch()
    r = make(tmp_path, key="", fetch=f)
    assert not r.configured and "no key" in r.why_not()
    assert not r.refresh("events") and r.due() == [] and f.urls == []
    r.start()
    assert r._thread is None and "no camera, incident or sign source" in r.logs[-1] and "no key" in r.logs[-2]
    pub = r.public()
    assert (pub["configured"], pub["cameras"], pub["events"], pub["signs"]) == (False, [], [], [])
    assert "SIZEUP_511NY_KEY" in pub["note"]
    code, body, _, _ = r.get("/api/cad/cameras/4616/image", "")
    assert code == 404 and b"no camera source is configured" in body
    assert json.loads(r.get("/api/cad/roads", "")[1])["configured"] is False
    bare = make(urls={})
    assert not bare.configured and "feed addresses" in bare.why_not()
    # a feed address with no {key} needs no key
    assert make(key="", urls={"events": "https://feeds.example/events?format=json"}).configured
    assert not (tmp_path / "roads511.json").exists()


def test_errors_back_off_and_never_show_the_key(tmp_path):
    clock = Clock()
    f = FakeFetch()
    r = make(tmp_path, fetch=f, clock=clock)
    f.fail["events"] = HTTPError(URLS["events"].replace("{key}", KEY), 401, "Unauthorized", None, None)
    assert not r.refresh("events")
    st = r.public()["sources"][0]["status"]["events"]
    assert "refused the key" in st["error"] and r.ny.status["events"]["next"] == clock.t + REFUSED_S
    f.fail["signs"] = HTTPError("", 429, "Too Many Requests", None, None)
    assert not r.refresh("signs") and "rate limit" in r.ny.status["signs"]["error"]
    f.fail["cameras"] = OSError(f"connection reset fetching ...?key={KEY}")
    assert not r.refresh("cameras") and r.ny.status["cameras"]["next"] == clock.t + 600    # its floor, not an hour
    f.feeds["events"] = b"<!doctype html><html></html>"
    del f.fail["events"]
    assert not r.refresh("events") and "HTML" in r.ny.status["events"]["error"]
    assert r.ny.status["events"]["next"] == clock.t + 120                       # the second failure: 2 min
    blob = json.dumps(r.public()) + "\n".join(r.logs)
    assert KEY not in blob and "***" in "\n".join(r.logs)
    # a good read clears the error
    f.feeds["events"] = fixture("events.json")
    assert r.refresh("events") and r.ny.status["events"]["error"] == ""


def test_the_layers_api_answers_same_rev_cheaply(tmp_path):
    r = make(tmp_path)
    everything(r)
    code, body, ctype, _ = r.get("/api/cad/roads", "")
    doc = json.loads(body)
    assert code == 200 and ctype == "application/json" and len(doc["events"]) == 3
    assert json.loads(r.get("/api/cad/roads", f"rev={doc['rev']}")[1]) == {"rev": doc["rev"], "same": True}
    assert "cameras" in json.loads(r.get("/api/cad/roads", "rev=0")[1])


# ---- near an event, and the place search ("@") ---------------------------------------------------------------


def test_the_nearest_working_cameras_to_an_event():
    r = make()
    everything(r)
    near = r.nearest(41.0006, -73.6739)                                         # the I-95 Exit 21 crash
    assert [c["id"] for c in near] == ["4616"] and near[0]["km"] < 0.2          # 4620 is 1.9 km away
    assert [c["id"] for c in r.nearest(41.0006, -73.6739, within_km=3)] == ["4616", "4620"]   # 4631 is off
    assert r.nearest(41.0006, -73.6739, n=1)[0]["id"] == "4616"
    assert r.nearest(41.08, -73.56) == []                                       # nothing within 1.5 km
    code, body, _, _ = r.get("/api/cad/cameras", "near=41.0006,-73.6739&n=2")
    assert code == 200 and [c["id"] for c in json.loads(body)["cameras"]] == ["4616"]
    assert r.get("/api/cad/cameras", "near=nowhere")[0] == 400 and r.get("/api/cad/cameras", "")[0] == 400


def test_cameras_in_the_place_search():
    r = make()
    everything(r)
    hits = r.search_places("I-95 exit 21 camera")
    assert [h["camera"] for h in hits] == ["4616"]
    assert hits[0] | {"score": 0} == {"kind": "camera", "label": "Camera: I-95 at Exit 21 - Port Chester",
                                      "sub": "I-95 Northbound", "lat": 41.0012, "lon": -73.6748, "source": "511NY",
                                      "camera": "4616", "score": 0}
    assert [h["camera"] for h in r.search_places("hutch exit 30 camera")] == ["4631"]
    assert [h["camera"] for h in r.search_places("287 cam")] == ["4620"]        # "287" finds I-287
    assert [h["camera"] for h in r.search_places("i95")] == ["4616"]
    assert {h["camera"] for h in r.search_places("rye brook")} == {"4620", "4631"}
    asked, not_asked = r.search_places("rye brook camera"), r.search_places("rye brook")
    assert asked[0]["score"] > not_asked[0]["score"]                            # "camera" puts them first
    assert r.search_places("") == [] and r.search_places("king street") == []
    assert len(r.search_places("camera")) == 3
    code, body, _, _ = r.get("/api/cad/cameras", "q=exit+21+camera")
    assert json.loads(body)["cameras"][0]["camera"] == "4616"


# ---- the snapshot proxy --------------------------------------------------------------------------------------


def test_a_snapshot_is_fetched_once_for_everyone_then_again_after_the_ttl():
    clock, f = Clock(), FakeFetch()
    r = make(fetch=f, clock=clock)
    everything(r)
    f.urls.clear()
    code, body, ctype, headers = r.get("/api/cad/cameras/4616/image", "")
    assert (code, body, ctype) == (200, JPEG, "image/jpeg")
    assert headers["X-Image-Time"] == "1791914990" and headers["Cache-Control"] == f"private, max-age={int(IMAGE_TTL_S)}"
    for _ in range(5):
        clock.t += 2
        assert r.image("4616")[1] == JPEG
    assert f.urls == ["https://511ny.org/map/Cctv/4616"]                        # one fetch for all of them
    clock.t += IMAGE_TTL_S
    r.image("4616")
    assert len(f.urls) == 2
    # many at once: still one fetch
    clock.t += IMAGE_TTL_S
    out = []
    ts = [threading.Thread(target=lambda: out.append(r.image("4616")[0])) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == [200] * 8 and len(f.urls) == 3


def test_a_camera_that_fails_serves_the_last_snapshot_a_while_then_502():
    clock, f = Clock(), FakeFetch()
    r = make(fetch=f, clock=clock)
    everything(r)
    assert r.image("4616")[0] == 200
    clock.t += IMAGE_TTL_S + 1
    f.fail["image"] = OSError("timed out")
    code, body, _, headers = r.image("4616")
    assert code == 200 and body == JPEG and headers["X-Image-Stale"] == "1"
    clock.t += IMAGE_STALE_S
    code, body, ctype, _ = r.image("4616")
    assert code == 502 and ctype == "application/json" and b"didn't answer" in body
    # not an image (a login page, an error page): refused, never passed on
    del f.fail["image"]
    f.image = (200, {"content-type": "text/html"}, b"<html>")
    assert r.image("4620")[0] == 502
    assert r.image("nope")[0] == 404
    assert r.get("/api/cad/cameras/..%2F/image", "")[0] == 404


def test_the_key_is_never_in_a_proxied_answer(tmp_path):
    f = FakeFetch()
    r = make(tmp_path, fetch=f)
    everything(r)
    answers = [r.get(p, q) for p, q in (("/api/cad/roads", ""), ("/api/cad/cameras", "q=camera"),
                                        ("/api/cad/cameras", "near=41,-73.67"), ("/api/cad/cameras/4616/image", ""),
                                        ("/api/cad/cameras/none/image", ""))]
    for code, body, ctype, headers in answers:
        assert KEY.encode() not in body and KEY not in json.dumps(headers)
        assert b"feeds.example" not in body


# ---- mile markers and exits (the gazetteer's places, #67) -------------------------------------------------------


def places_db(tmp_path) -> Path:
    p = tmp_path / "gazetteer.sqlite"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE places (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, "
               "address TEXT NOT NULL, muni TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, route TEXT NOT NULL, "
               "dir TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL, ref TEXT NOT NULL, aliases TEXT NOT NULL)")
    rows = [("exit", "I-95 NB exit 21", "Port Chester", 41.0010, -73.6760, "I-95", "NB", "21"),
            ("milemarker", "I-287 EB MM 10.0", "Rye Brook", 41.0160, -73.6900, "I-287", "EB", "10.0"),
            ("place", "Lyon Park", "Port Chester", 41.0, -73.66, "", "", ""),
            ("milemarker", "I-87 NB MM 6.0", "Colonie", 42.74, -73.77, "I-87", "NB", "6.0")]
    for k, n, m, lat, lon, route, d, v in rows:
        db.execute("INSERT INTO places (kind, name, category, address, muni, lat, lon, route, dir, value, source, ref, aliases) "
                   "VALUES (?, ?, '', '', ?, ?, ?, ?, ?, ?, 'test', '', '')", (k, n, m, lat, lon, route, d, v))
    db.commit()
    db.close()
    return p


def test_mileposts_and_exits_come_from_the_gazetteer_without_a_key(tmp_path):
    hp = highway_places(places_db(tmp_path))
    assert [e["label"] for e in hp["exits"]] == ["I-95 NB exit 21"]
    assert [m["label"] for m in hp["mileposts"]] == ["I-287 EB MM 10.0"]          # Albany's is outside our area
    (tmp_path / "x").mkdir()
    r = make(key="", gazetteer=places_db(tmp_path / "x"))
    pub = r.public()
    assert not pub["configured"] and pub["highway_places"] and pub["exits"][0]["value"] == "21"
    # before #67's places table: none, and nothing breaks
    plain = tmp_path / "plain.sqlite"
    sqlite3.connect(plain).execute("CREATE TABLE addresses (id INTEGER)").connection.close()
    assert highway_places(plain) == {} and highway_places(tmp_path / "missing.sqlite") == {}
    assert make(key="").public()["highway_places"] is False


# ---- the Thruway's feeds (recorded), and both providers at once --------------------------------------------------


def tfix(name: str):
    return decode((TFIX / name).read_bytes())


def test_thruway_cameras_parse_with_road_direction_and_mile():
    cams = {c["id"]: c for c in parse_thruway_cameras(tfix("trafficcameras.xml"))}
    assert len(cams) == 15                                                    # I-95 and I-287 around us; the two elsewhere dropped
    pc = cams["1ne01370s"]
    assert pc == {"id": "1ne01370s", "name": "I-95 at Interchange 21 (Route1/I-287)", "road": "I-95", "dir": "Southbound",
                  "mile": "13.8", "lat": 40.9874, "lon": -73.68029, "url": "https://nyssnapshot.com/thumbs/TA_058.flv.png",
                  "video": "https://s58.nysdot.skyvdn.com:443/rtplive/TA_058/playlist.m3u8", "has_video": True, "off": False,
                  "cadence": 60}
    assert {c["road"] for c in cams.values()} == {"I-95", "I-287"}
    assert all(c["dir"] in ("Northbound", "Southbound", "Eastbound", "Westbound", "") for c in cams.values())
    assert all(c["url"].startswith("https://nyssnapshot.com/") for c in cams.values())
    assert [c["id"] for c in parse_thruway_cameras(tfix("trafficcameras.xml"), (-73.9, 40.9, -73.85, 40.95))] == ["1ml00020s", "1ml00082n"]
    # not an XML feed at all: nothing, not a crash
    assert parse_thruway_cameras(decode(b"[]")) == []


def test_thruway_events_and_signs():
    ev = {e["id"]: e for e in parse_thruway_events(tfix("events.xml"))}
    assert set(ev) == {"CAD-262860247", "CAD-262740341"}                     # the 20 statewide, 2 here
    crash, work = ev["CAD-262860247"], ev["CAD-262740341"]
    assert (crash["layer"], crash["type"], crash["subtype"], crash["road"], crash["dir"], crash["mile"], crash["closed"]) == (
        "incidents", "incident", "crash", "I-287", "Westbound", "8.7", False)
    assert crash["description"] == "crash I-287 - Cross Westchester Expwy westbound at exit 10"
    assert crash["updated"] == 1791918300 and crash["end"] is None             # "10/13/2026 3:05 PM" Eastern; "Until further notice"
    assert (work["layer"], work["subtype"]) == ("roadwork", "long term road construction")
    # closed and lanes, from the wording the Thruway uses
    doc = decode(b'<events><event category="incident" eventid="X1" eventtype="crash" latitude="41.0" longitude="-73.67" '
                 b'route="I-95 - New England Thruway" direction="in both directions" updatetime="10/13/2026 4:05 PM" '
                 b'eventdesc="crash, all lanes closed I-95 northbound at exit 21" expirationdatetime="10/13/2026 6:00 PM"/>'
                 b'<event category="roadwork" eventid="X2" eventtype="road maintenance operations" latitude="41.0" longitude="-73.68" '
                 b'route="I-95 - New England Thruway" direction="southbound" updatetime="10/13/2026 4:05 PM" '
                 b'eventdesc="road maintenance operations, right lane closed I-95 southbound" expirationdatetime=""/></events>')
    x1, x2 = {e["id"]: e for e in parse_thruway_events(doc)}.values()
    assert x1["closed"] and x1["dir"] == "" and x1["lanes"] == "all lanes closed" and x1["end"] == 1791928800
    assert not x2["closed"] and x2["lanes"] == "right lane closed" and x2["layer"] == "roadwork"
    signs = {s["id"]: s for s in parse_thruway_signs(tfix("dmsstatus.xml"))}
    assert len(signs) == 8
    lit = signs["DMSNE11N"]
    assert (lit["name"], lit["road"], lit["dir"], lit["mile"]) == ("I-95 11.5 NB", "I-95", "Northbound", "11.5")
    assert lit["lines"] == ["FROM I-287", "TO GMC BRIDGE", "41 MIN"] and lit["updated"] == 1791924300
    assert signs["DMSNE12S"]["lines"] == []                                    # activemessage = 0


def test_the_thruway_alone_needs_no_key_and_the_layers_are_on(tmp_path):
    f = FakeFetch()
    r = make(tmp_path, key="", urls={}, fetch=f, thruway=True)
    assert r.configured and r.why_not() == "" and r.due() == [("thruway", "cameras"), ("thruway", "events"), ("thruway", "signs")]
    assert r.thruway.every == {"cameras": 3600.0, "events": 120.0, "signs": 300.0}
    everything(r)
    assert sorted(f.urls) == sorted(TURLS.values())
    pub = r.public()
    assert pub["configured"] and "511NY not configured" in pub["note"]      # said, but the layers are on
    assert len(pub["cameras"]) == 15 and len(pub["events"]) == 2 and len(pub["signs"]) == 8
    assert {c["source"] for c in pub["cameras"]} == {e["source"] for e in pub["events"]} == {"Thruway"}
    srcs = {s["name"]: s for s in pub["sources"]}
    assert srcs["thruway"]["configured"] and srcs["thruway"]["link"].startswith("https://www.thruway.ny.gov/")
    assert srcs["thruway"]["label"] == "New York State Thruway Authority" and srcs["thruway"]["at"] == r.clock()
    assert not srcs["511ny"]["configured"]
    r.start()
    assert "Thruway on (cameras every 3600 s, events every 120 s, signs every 300 s)" in "\n".join(r.logs)
    assert "511NY not configured" in "\n".join(r.logs)
    r.stop()
    # the snapshot proxy, through the Thruway's redirecting thumbs address (start() swept them: forget that)
    f.urls.clear()
    r._images.clear()
    code, body, ctype, headers = r.get("/api/cad/cameras/1ne01370s/image", "")
    assert (code, body, ctype) == (200, JPEG, "image/jpeg") and f.urls == ["https://nyssnapshot.com/thumbs/TA_058.flv.png"]
    # nearest to a point between the interchange 21 cameras: them, then the one before Connecticut
    assert [c["id"] for c in r.nearest(40.988, -73.675)] == ["1ne01420n", "1ne01370s", "1ne01470n"]
    assert [h["camera"] for h in r.search_places("i95 interchange 21 camera")] == ["1ne01370s"]
    assert r.search_places("i95 interchange 21 camera")[0]["source"] == "Thruway"
    hits = r.search_places("287 interchange 7 camera")                     # (a word matches the start of a word: "7" is 7.2 too)
    assert {h["camera"] for h in hits} >= {"1cw00400w", "1cw00430e"} and all(h["sub"].startswith("I-287") for h in hits)
    # the cache keeps both providers apart, and a restart reads it
    cache = json.loads((tmp_path / "roads511.json").read_text())
    assert set(cache["providers"]) == {"511ny", "thruway"} and len(cache["providers"]["thruway"]["cameras"]["items"]) == 15
    again = make(tmp_path, key="", urls={}, fetch=FakeFetch(), thruway=True, clock=r.clock)
    assert len(again.public()["cameras"]) == 15 and again.due() == []


def test_both_providers_merge_with_511ny_first(tmp_path):
    f = FakeFetch()
    # a 511NY camera 30 m from the Thruway's at interchange 21, and one 120 m away
    doc = json.loads(fixture("cameras.json"))
    doc[0].update(ID="4616", Latitude=40.9874 + 0.00027, Longitude=-73.68029)          # ~30 m north of 1ne01370s
    doc[1].update(ID="4620", Latitude=40.9874 + 0.0011, Longitude=-73.68029)           # ~120 m: another camera
    f.feeds["cameras"] = json.dumps(doc).encode()
    r = make(tmp_path, fetch=f, thruway=True)
    assert r.due() == [("511ny", "cameras"), ("511ny", "events"), ("511ny", "signs"),
                       ("thruway", "cameras"), ("thruway", "events"), ("thruway", "signs")]
    everything(r)
    pub = r.public()
    ids = [c["id"] for c in pub["cameras"]]
    assert "4616" in ids and "1ne01370s" not in ids                          # the same camera: 511NY's wins
    assert "4620" in ids and "1ne01420n" in ids                              # not the same: both stay
    assert len(pub["cameras"]) == 3 + 15 - 1
    assert [c["source"] for c in pub["cameras"][:3]] == ["511NY"] * 3 and pub["cameras"][-1]["source"] == "Thruway"
    assert len(pub["events"]) == 3 + 2 and len(pub["signs"]) == 2 + 8        # nothing within 50 m across sources
    assert [s["name"] for s in pub["sources"]] == ["511ny", "thruway"] and all(s["configured"] for s in pub["sources"])   # (county off here)
    assert pub["note"] == ""
    # per item: the camera endpoint and the proxy find either
    assert r.camera("4616")["source"] == "511NY" and r.camera("1cw00250w")["source"] == "Thruway"
    assert r.get("/api/cad/cameras/1cw00250w/image", "")[0] == 200
    near = r.nearest(40.98767, -73.68029)
    assert [c["id"] for c in near] == ["4616", "4620", "1ne01420n"]
    # a Thruway feed failing doesn't touch 511NY's, and says which
    f.fail["thruway:events"] = HTTPError("", 503, "Service Unavailable", None, None)
    assert r.refresh("events")                                               # 511NY's still read
    assert r.thruway.status["events"]["error"].startswith("HTTP 503") and r.ny.status["events"]["error"] == ""
    assert "Thruway events: HTTP 503" in "\n".join(r.logs)
    assert len(r.public()["events"]) == 3 + 2                                # the last good Thruway read stays
    # the 511NY key is nowhere in a merged answer either
    blob = json.dumps(pub) + (tmp_path / "roads511.json").read_text() + "\n".join(r.logs)
    assert KEY not in blob and "feeds.example" not in blob


def test_the_user_agent_says_who_we_are():
    assert UA.startswith("sizeup/") and "Port Chester FD" in UA and "contact pcfd-sizeup.cqmpy@dralias.com" in UA
    from sizeup import USER_AGENT
    assert UA == USER_AGENT                                                      # one header everywhere
    assert "Mozilla" not in UA and "Chrome" not in UA                        # a plain client, never a browser


# ---- the incident viewer's cameras: just before and just after, along the road (#95) ------------------------------

ROOT = Path(__file__).resolve().parent.parent
GAZ = ROOT / "data" / "gazetteer.sqlite"          # the real mile markers (I-95, I-287, the Hutch, US-1, I-684, ...)


def place(name: str) -> tuple[float, float]:
    db = sqlite3.connect(f"file:{GAZ}?mode=ro", uri=True)
    try:
        lat, lon = db.execute("SELECT lat, lon FROM places WHERE name = ?", (name,)).fetchone()
    finally:
        db.close()
    return lat, lon


@pytest.fixture
def highway():
    """The Thruway's recorded cameras on the real markers: 15 cameras on I-95 and I-287 around Port Chester."""
    r = make(thruway=True, key="", urls={}, gazetteer=GAZ)
    assert r.refresh("cameras")
    return r


def picks(o):
    return [(c["rel"], c["miles"], c["side"], c["facing"], c["id"]) for c in o["cameras"]]


def test_a_highway_event_names_its_road_direction_and_mileage(highway):
    r = highway
    lat, lon = place("I-95 SB exit 21")
    assert r.locate_on_road(lat, lon, "I-95 SB exit 21") == {"road": "I-95", "dir": "SB", "mile": 14.5}
    assert r.locate_on_road(lat, lon, "") == {"road": "I-95", "dir": "", "mile": 14.5}            # on the road, no direction
    assert r.locate_on_road(lat, lon, "", "I-95 southbound at exit 21")["dir"] == "SB"            # from a typed address
    assert r.locate_on_road(*place("I-287 EB MM 7.5"), "I-287 EB MM 7.5") == {"road": "I-287", "dir": "EB", "mile": 7.5}
    assert r.locate_on_road(*place("Hutch NB MM 18.8"), "Hutch NB MM 18.8") == {"road": "Hutch", "dir": "NB", "mile": 18.8}
    assert r.locate_on_road(41.0019, -73.6669, "Lyon Park, Port Chester")["road"] == "NY-120A"     # (N Main St is Route 120A)
    assert r.locate_on_road(41.0165, -73.6830, "Crawford Park, Rye Brook") is None                  # 1 km from any marked road
    assert r.locate_on_road(40.9880, -73.6790, "20 Clinton St")["road"] == "I-95"                   # 130 m from I-95: on it
    assert r.locate_on_road(None, None, "I-95 NB exit 21") is None
    # a road named in the label but the point far from it: the point wins (a mislabelled event)
    assert r.locate_on_road(41.0165, -73.6830, "I-95 NB exit 21") is None


def test_before_and_after_follow_the_direction_of_travel(highway):
    r = highway
    # southbound at exit 21 (MP 14.5): "before" is north of it (the way the traffic came), "after" south
    sb = r.cameras_for(*place("I-95 SB exit 21"), "I-95 SB exit 21")
    assert sb["highway"] == {"road": "I-95", "dir": "SB", "mile": 14.5}
    assert picks(sb) == [("before", 0.2, "north", False, "1ne01470n"), ("after", 0.3, "south", False, "1ne01420n")]
    # northbound at exit 21 (MP 13.7): a camera right there, then the one before (south) and after (north, on the NB side)
    nb = r.cameras_for(*place("I-95 NB exit 21"), "I-95 NB exit 21")
    assert picks(nb) == [("at", 0.0, "north", False, "1ne01370s"), ("before", 1.2, "south", True, "1ne01240n"),
                         ("after", 0.5, "north", True, "1ne01420n")]
    assert nb["cameras"][0]["name"] == "I-95 at Interchange 21 (Route1/I-287)"
    # no direction: both sides by compass
    assert picks(r.cameras_for(*place("I-95 MM 9.5"), "I-95 MM 9.5")) == [
        ("side", 0.3, "south", False, "1ne00915s"), ("side", 0.4, "north", False, "1ne00989s")]
    # I-287 eastbound at MM 7.5: before is west (the EB camera at interchange 9, over its carriageway), after east
    eb = r.cameras_for(*place("I-287 EB MM 7.5"), "I-287 EB MM 7.5")
    assert picks(eb) == [("before", 0.1, "west", True, "1cw00710e"), ("after", 0.6, "east", False, "1cw00780w")]
    wb = r.cameras_for(*place("I-287 EB MM 7.5"), "I-287 WB MM 7.5")
    assert picks(wb) == [("before", 0.6, "east", True, "1cw00780w"), ("after", 0.1, "west", False, "1cw00710e")]
    # each pick carries what the panel shows
    assert set(sb["cameras"][0]) >= {"id", "name", "road", "dir", "mile", "lat", "lon", "source", "rel", "miles", "side", "facing", "km"}


def test_a_camera_over_the_events_carriageway_wins_when_it_is_nearly_as_close(highway):
    r = highway
    # I-95 southbound at MP 9.5: the SB camera at 9.15 (0.3 mi) is "before"; northbound, the same camera is "after"
    # (no NB camera within 0.3 mi of it), not facing
    sb = picks(r.cameras_for(*place("I-95 MM 9.5"), "I-95 SB MM 9.5"))
    assert sb == [("before", 0.4, "north", True, "1ne00989s"), ("after", 0.3, "south", True, "1ne00915s")]
    nb = picks(r.cameras_for(*place("I-95 MM 9.5"), "I-95 NB MM 9.5"))
    assert nb == [("before", 0.3, "south", False, "1ne00915s"), ("after", 0.4, "north", False, "1ne00989s")]
    # a facing camera up to 0.3 mi further than the nearest is preferred: a fake NB one at 10.1 vs the SB one at 9.9
    fake = dict(r.camera("1ne00989s"), id="fakeNB", dir="Northbound", lat=40.96413, lon=-73.73698)   # ~MP 10.1
    r.thruway.data["cameras"].append(fake)
    r._changed()
    assert picks(r.cameras_for(*place("I-95 MM 9.5"), "I-95 NB MM 9.5"))[1] == ("after", 0.5, "north", True, "fakeNB")


def test_no_camera_within_two_miles_shows_nothing_and_off_the_highway_only_nearby(highway):
    r = highway
    hutch = r.cameras_for(*place("Hutch NB MM 18.8"), "Hutch NB MM 18.8")
    assert hutch["highway"]["road"] == "Hutch" and hutch["cameras"] == []        # no camera on the Hutch
    far = r.cameras_for(*place("US-1 MM 10.0"), "US-1 MM 10.0")                   # I-95's cameras beside it are I-95's
    assert far["highway"] == {"road": "US-1", "dir": "", "mile": 10.0} and far["cameras"] == []
    # off the highway: only what is within 300 m as the crow flies, as "nearby"
    park = r.cameras_for(41.0165, -73.6830, "Crawford Park, Rye Brook")
    assert park["highway"] is None and park["cameras"] == []
    close = r.cameras_for(40.9880, -73.6815, "a house 90 m from the interchange 21 camera")
    assert close["highway"]["road"] == "I-95"                                     # (that close to I-95 counts as on it)
    r2 = make(thruway=True, key="", urls={}, gazetteer=Path("/nonexistent"))      # no markers at all: nearby only
    assert r2.refresh("cameras")
    o = r2.cameras_for(40.9880, -73.6815, "I-95 SB exit 21")
    assert o["highway"] is None and [(c["rel"], c["id"]) for c in o["cameras"]] == [("nearby", "1ne01370s")]
    assert o["cameras"][0]["km"] < 0.3 and o["cameras"][0]["miles"] is None
    # a camera marked off is never picked
    r.camera("1ne01420n")["off"] = True
    r._changed()
    assert "1ne01420n" not in [c["id"] for c in r.cameras_for(*place("I-95 SB exit 21"), "I-95 SB exit 21")["cameras"]]


def test_another_roads_camera_at_the_interchange_is_not_this_roads(highway):
    r = highway
    # the I-287 camera at interchange 5 is ~1 km from I-95's markers: never on I-95; and I-95's cameras at
    # interchange 21 are within 300 m of I-287's last markers but say "I-95": not I-287's
    end = r.cameras_for(*place("I-287 EB MM 10.5"), "I-287 EB MM 10.5")
    assert end["highway"]["road"] == "I-287" and all(c["road"] == "I-287" for c in end["cameras"] if c["rel"] != "nearby")


def test_the_cameras_endpoint(highway):
    r = highway
    lat, lon = place("I-95 SB exit 21")
    code, body, ctype, _ = r.get("/api/cad/cameras", f"for={lat},{lon}&label=I-95+SB+exit+21")
    doc = json.loads(body)
    assert code == 200 and doc["highway"]["dir"] == "SB" and [c["rel"] for c in doc["cameras"]] == ["before", "after"]
    assert r.get("/api/cad/cameras", "for=nowhere")[0] == 400
    assert json.loads(r.get("/api/cad/cameras", "for=41.0165,-73.6830&label=Crawford+Park")[1]) == {
        "configured": True, "highway": None, "cameras": [], "down": []}


# ---- Westchester County DPW's cameras (#99), and cameras that are down ------------------------------------------

CFIX = Path(__file__).parent / "fixtures" / "county"
CURLS = {"cameras": "https://county.example/arcgis/rest/services/MappingWestchesterCounty/MapServer/124/query?f=json"}
OLD = formatdate(1_626_646_462, usegmt=True)                 # Sun, 18 Jul 2021: the county's dead-camera placeholder


class CountyFetch(FakeFetch):
    """FakeFetch plus the county's layer (recorded) and images: fresh by default; per-camera overrides."""

    def __init__(self):
        super().__init__()
        self.county: dict[str, object] = {}                  # camera number -> "old" | "404" | Exception

    def __call__(self, url, timeout, limit):
        if url.startswith("https://county.example/"):
            return 200, {"content-type": "application/json"}, (CFIX / "cameras.json").read_bytes()
        if url.startswith("https://nyssnapshot.com/thumbs/"):                        # the Thruway's, fresh too
            self.urls.append(url)
            return 200, {"content-type": "image/png", "last-modified": formatdate(self.now() - 20, usegmt=True)}, JPEG
        m = re.fullmatch(r"https://www\.westchestergov\.com/cctv/camera(\d+)\.jpg", url)
        if m:
            self.urls.append(url)
            how = self.county.get(m.group(1))
            if isinstance(how, Exception):
                raise how
            if how == "404":
                return 404, {"content-type": "text/html"}, b"<html>not found</html>"
            if how == "old":
                return 200, {"content-type": "image/jpeg", "last-modified": OLD}, JPEG
            return 200, {"content-type": "image/jpeg", "last-modified": formatdate(self.now() - 4, usegmt=True)}, JPEG
        return super().__call__(url, timeout, limit)


def county(tmp_path=None, clock=None, gazetteer=Path("/nonexistent"), thruway=True):
    clock = clock or Clock()
    f = CountyFetch()
    f.now = clock
    r = make(tmp_path, key="", urls={}, fetch=f, clock=clock, thruway=thruway, gazetteer=gazetteer, county=True, county_urls=CURLS)
    assert r.refresh("cameras")
    return r, f


def test_county_cameras_parse_from_the_arcgis_layer():
    from sizeup.roads import parse_county_cameras
    cams = {c["id"]: c for c in parse_county_cameras(json.loads((CFIX / "cameras.json").read_bytes()))}
    assert len(cams) == 26                                                       # the two in Yonkers are outside our box
    king = cams["wc23"]
    assert king == {"id": "wc23", "name": "SB King Approach - King St/Willett Ave/North Pearl St", "road": "King St",
                    "dir": "Southbound", "mile": "", "lat": 41.003494, "lon": -73.663581,
                    "url": "https://www.westchestergov.com/cctv/camera23.jpg", "video": "", "has_video": False, "off": False,
                    "cadence": 8}
    assert (cams["wc22"]["road"], cams["wc22"]["dir"]) == ("I-287", "Eastbound")         # "going East on I-287"
    assert (cams["wc4"]["road"], cams["wc4"]["mile"]) == ("BRP", "15.5")
    assert all(c["url"].startswith("https://") for c in cams.values())              # the layer says http://
    assert parse_county_cameras({"error": {"code": 400}}) == [] and parse_county_cameras([]) == []


def test_the_county_is_a_third_provider_with_its_own_attribution(tmp_path):
    r, f = county(tmp_path)
    pub = r.public()
    assert [s["short"] for s in pub["sources"]] == ["511NY", "Thruway", "County"]
    c = next(s for s in pub["sources"] if s["name"] == "county")
    assert c["configured"] and c["label"] == "Westchester County DPW" and c["link"] == "https://www.westchestergov.com/trafficcamera"
    assert c["status"]["cameras"]["n"] == 26 and not c["status"]["events"]["on"]        # cameras only
    assert r.county.every["cameras"] == 86400.0                                        # the list daily
    assert len(pub["cameras"]) == 15 + 26 and {x["source"] for x in pub["cameras"]} == {"Thruway", "County"}
    assert all({"cadence", "down", "down_since", "last_image"} <= set(x) for x in pub["cameras"])
    assert {x["cadence"] for x in pub["cameras"]} == {8, 60}
    assert r.get("/api/cad/cameras/wc23/image", "")[0] == 200 and f.urls[-1] == "https://www.westchestergov.com/cctv/camera23.jpg"
    assert "wc23" in {h["camera"] for h in r.search_places("king st camera")}
    assert json.loads(r.get("/api/cad/roads", "")[1])["configured"]
    # off and on
    assert make(key="", urls={}, county=False).county is None
    with pytest.raises(ConfigError, match="county"):
        load_cfg(tmp_path, '[roads]\ncounty = "yes"\n')
    assert load_cfg(tmp_path, "[roads]\ncounty = false\n").roads_county is False and load_cfg(tmp_path, "").roads_county is True


def test_a_stale_image_is_no_image_and_the_camera_is_down_until_a_fresh_one(tmp_path):
    clock = Clock()
    r, f = county(tmp_path, clock)
    f.county["68"] = "old"                                                           # the 2021 placeholder
    code, body, ctype, headers = r.get("/api/cad/cameras/wc68/image", "")
    doc = json.loads(body)
    assert code == 404 and ctype == "application/json" and doc["error"] == "no current image"
    assert doc["down"] and doc["down_since"] == clock.t and doc["last_image"] == 1_626_646_462
    assert headers["Cache-Control"] == "no-store"
    cam = next(c for c in r.public()["cameras"] if c["id"] == "wc68")
    assert cam["down"] and cam["down_since"] == clock.t and cam["last_image"] == 1_626_646_462
    assert "wc68" not in [c["id"] for c in r.nearest(41.01765, -73.72633, within_km=0.5)]   # never offered while down
    # back: a fresh image
    clock.t += 60
    del f.county["68"]
    assert r.get("/api/cad/cameras/wc68/image", "")[0] == 200
    assert not r.health("wc68")["down"] and r.health("wc68")["last_image"] == clock.t - 4


def test_three_failures_are_down_and_the_old_picture_is_not_served(tmp_path):
    clock = Clock()
    r, f = county(tmp_path, clock)
    assert r.get("/api/cad/cameras/wc23/image", "")[0] == 200                       # a good one first
    f.county["23"] = OSError("timed out")
    for i in range(2):                                                               # 1, 2: the last picture, marked stale
        clock.t += IMAGE_TTL_S + 1
        code, body, _, headers = r.get("/api/cad/cameras/wc23/image", "")
        assert code == 200 and headers.get("X-Image-Stale") == "1" and not r.health("wc23")["down"]
    clock.t += IMAGE_TTL_S + 1
    code, body, _, _ = r.get("/api/cad/cameras/wc23/image", "")                    # 3: down, and no old picture
    assert code == 404 and json.loads(body)["down"] and r.health("wc23")["down_since"] == clock.t
    assert r.health("wc23")["last_image"] is not None
    rev = r.rev
    clock.t += IMAGE_TTL_S + 1
    assert r.get("/api/cad/cameras/wc23/image", "")[0] == 404 and r.rev == rev     # still down: no churn
    f.county["24"] = "404"                                                           # a 404 counts as a failure
    for _ in range(3):
        clock.t += IMAGE_TTL_S + 1
        r.get("/api/cad/cameras/wc24/image", "")
    assert r.health("wc24")["down"] and r.health("wc24")["last_image"] is None
    # recovery clears it
    del f.county["23"]
    clock.t += IMAGE_TTL_S + 1
    assert r.get("/api/cad/cameras/wc23/image", "")[0] == 200 and not r.health("wc23")["down"]


def test_the_sweep_looks_at_every_camera_so_the_map_knows(tmp_path):
    clock = Clock()
    r, f = county(tmp_path, clock)
    f.county["68"] = f.county["8"] = "old"
    f.county["24"] = f.county["16"] = "404"
    n = r.sweep()
    assert n == 15 + 26 and len(f.urls) >= 41
    down = sorted(c["id"] for c in r.public()["cameras"] if c["down"])
    assert down == ["wc68", "wc8"]                                                   # stale: at once; 404s: not yet (1 of 3)
    assert r.sweep() == 0                                                            # looked at already
    clock.t += 901
    r.sweep(); clock.t += 901; r.sweep()
    assert sorted(c["id"] for c in r.public()["cameras"] if c["down"]) == ["wc16", "wc24", "wc68", "wc8"]


def test_down_cameras_are_demoted_in_the_picker_and_listed_apart(tmp_path):
    clock = Clock()
    r, f = county(tmp_path, clock, gazetteer=GAZ)
    lat, lon = place("I-287 EB MM 5.5")
    live = r.cameras_for(lat, lon, "I-287 EB MM 5.5")
    rels = [(c["rel"], c["id"]) for c in live["cameras"]]
    assert {i for r_, i in rels if r_ == "at"} == {"wc16", "wc22", "wc39"} and live["down"] == []   # the county's three at the spot
    assert ("before", "1cw00520e") in rels and next(i for r_, i in rels if r_ == "after") in ("wc68", "1cw00710e")
    f.county["16"] = "404"
    f.county["22"] = "old"
    for _ in range(3):
        clock.t += IMAGE_TTL_S + 1
        r.get("/api/cad/cameras/wc16/image", "")
    r.get("/api/cad/cameras/wc22/image", "")
    o = r.cameras_for(lat, lon, "I-287 EB MM 5.5")
    rels = [(c["rel"], c["id"]) for c in o["cameras"]]
    assert ("at", "wc39") in rels and ("before", "1cw00520e") in rels and not {"wc16", "wc22"} & {i for _, i in rels}
    assert {(c["rel"], c["id"], c["miles"]) for c in o["down"]} == {("down", "wc16", 0.0), ("down", "wc22", 0.0)}
    assert all(c["down"] and c["down_since"] for c in o["down"])
    assert {c["id"] for c in json.loads(r.get("/api/cad/cameras", f"for={lat},{lon}&label=I-287+EB+MM+5.5")[1])["down"]} == {"wc16", "wc22"}
    # off the highway too: a down camera close by is listed apart
    f.county["23"] = "old"
    r.get("/api/cad/cameras/wc23/image", "")
    o = r.cameras_for(41.0035, -73.6636, "King St & Willett Ave")
    assert [c["id"] for c in o["cameras"]] == [] or all(c["id"] != "wc23" for c in o["cameras"])
    assert [c["id"] for c in o["down"]] == ["wc23"] and o["down"][0]["km"] == 0.0


# ---- config ------------------------------------------------------------------------------------------------


def load_cfg(tmp_path, text: str):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return load(p)


def test_roads_config(tmp_path, monkeypatch):
    c = load_cfg(tmp_path, "")
    assert (c.roads_key, c.roads_events_url, c.roads_bbox) == ("", "", (-73.80, 40.92, -73.56, 41.10))
    c = load_cfg(tmp_path, '[roads]\nevents_url = "https://feeds.example/e?key={key}"\nbbox = [-73.9, 40.9, -73.5, 41.2]\n'
                           "cameras_every_min = 30\n")
    assert c.roads_events_url.endswith("{key}") and c.roads_bbox == (-73.9, 40.9, -73.5, 41.2) and c.roads_cameras_s == 1800
    monkeypatch.setenv("SIZEUP_511NY_KEY", KEY)
    assert load_cfg(tmp_path, '[roads]\nkey = "from-the-file-123"\n').roads_key == KEY       # the environment wins
    assert load_cfg(tmp_path, "[roads]\nenabled = false\n").roads_enabled is False
    assert c.roads_thruway is True and load_cfg(tmp_path, "[roads]\nthruway = false\n").roads_thruway is False
    with pytest.raises(ConfigError, match="thruway"):
        load_cfg(tmp_path, '[roads]\nthruway = "yes"\n')
    for bad, why in (('[roads]\nevents_url = "http://feeds.example/e"\n', "https://"),
                     (f'[roads]\nevents_url = "https://feeds.example/e?key={KEY}"\n', "never the key itself"),
                     ("[roads]\nbbox = [-73, 41, -74, 42]\n", "west < east"),
                     ("[roads]\nbbox = [-80, 40, -70, 45]\n", "3 degrees"),
                     ("[roads]\nsigns_every_min = 0.5\n", "10 calls a minute"),
                     ("[roads]\nnope = 1\n", "unknown key")):
        with pytest.raises(ConfigError, match=why):
            load_cfg(tmp_path, bad)
    monkeypatch.setenv("SIZEUP_511NY_KEY", "has space in it")
    with pytest.raises(ConfigError, match="no spaces"):
        load_cfg(tmp_path, "")


# ---- through the hub: a local fake 511NY, Cloudflare Access, viewers ----------------------------------------------


class Fake511:
    """511NY and its cameras on this machine: the feeds from the fixtures, a JPEG for any camera."""

    def __init__(self):
        self.requests: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                name = self.path.split("?")[0].strip("/")
                if name in ("events", "signs"):
                    body, ctype = fixture(f"{name}.json"), "application/json"
                elif name.startswith("api/data/"):                          # the Thruway's feeds, recorded
                    feed = name.split("/")[2]
                    body = (TFIX / f"{feed}.xml").read_bytes().replace(b"https://nyssnapshot.com/thumbs/",
                                                                       f"http://127.0.0.1:{fake.port}/snap/".encode())
                    ctype = "application/xml"
                elif name.startswith("snap/"):
                    body, ctype = JPEG, "image/png"
                elif name == "cameras":
                    body, ctype = (fixture("cameras.json").replace(b"https://511ny.org/map/Cctv/",
                                                                   f"http://127.0.0.1:{fake.port}/cctv/".encode())), "application/json"
                elif name.startswith("cctv/"):
                    body, ctype = JPEG, "image/jpeg"
                else:
                    body, ctype = b"no", "text/plain"
                self.send_response(200 if ctype != "text/plain" else 404)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def fake511():
    f = Fake511()
    yield f
    f.close()


def wait_for(cond, timeout=5.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_the_hub_serves_layers_and_snapshots_to_a_signed_in_viewer(tmp_path, keys, jwks, fake511):  # noqa: F811
    base = f"http://127.0.0.1:{fake511.port}"
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD, roads_key=KEY,
                       roads_events_url=base + "/events?key={key}&format=json", roads_cameras_url=base + "/cameras?key={key}",
                       roads_signs_url=base + "/signs?key={key}&format=json"))
    try:
        h.server.access.certs_url = jwks.url
        h.server.access.log = lambda m: None
        post(h.app.cad, "users", action="add", name="Corey", email="corey@example.org", role="admin")
        post(h.app.cad, "users", action="add", name="Val", email="val@example.org", role="viewer")
        assert wait_for(lambda: all(h.app.roads.ny.status[f]["at"] for f in ("events", "signs", "cameras")))
        assert all(f"key={KEY}" in p for p in fake511.requests if not p.startswith("/cctv/"))   # (the health sweep fetches images)
        assert req(h.url + "/api/cad/roads")[0] == 401                               # Access: a person only
        assert req(h.url + "/api/cad/cameras/4616/image")[0] == 401
        val = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email="val@example.org")}     # a viewer may look
        code, doc = req(h.url + "/api/cad/roads", headers=val)
        assert code == 200 and doc["configured"] and len(doc["cameras"]) == 3 and len(doc["signs"]) == 2
        code, img = req(h.url + "/api/cad/cameras/4616/image", headers=val)
        assert code == 200 and img == JPEG
        code, img = req(h.url + "/api/cad/cameras/4616/image", headers=val)
        assert sum(p.startswith("/cctv/4616") for p in fake511.requests) == 1         # the second from the cache
        code, near = req(h.url + "/api/cad/cameras?near=41.0006,-73.6739", headers=val)
        assert [c["id"] for c in near["cameras"]] == ["4616"]
        for path in ("/api/cad/roads", "/api/cad/cameras?q=camera", "/api/cad/cameras/4616/image"):
            raw = req(h.url + path, headers=val)[1]
            assert KEY not in (json.dumps(raw) if not isinstance(raw, bytes) else raw.decode("latin-1"))
        assert h.app.cad.place_providers[0]("exit 21 camera")[0]["camera"] == "4616"
        cache = (tmp_path / "hub" / "roads511.json").read_text()
        assert KEY not in cache and "127.0.0.1" in cache                            # snapshot URLs only, no key
    finally:
        h.close()


def test_a_hub_without_a_key_still_serves_the_page_and_says_not_configured(tmp_path):
    h = HubRun(hub_cfg(tmp_path))                 # (conftest: the Thruway's addresses are off in tests)
    try:
        doc = h.get("/api/cad/roads")
        assert doc["configured"] is False and "not configured" in doc["note"]
        code, body = req(h.url + "/api/cad/cameras/4616/image")
        assert code == 404
        assert h.app.roads._thread is None
    finally:
        h.close()


def test_the_hub_shows_the_thruway_without_any_key(tmp_path, monkeypatch, fake511):
    """#86: the Thruway's public feeds need no key, so a fresh hub has cameras and incidents from the start."""
    base = f"http://127.0.0.1:{fake511.port}/api/data"
    monkeypatch.setattr(roads_mod, "THRUWAY_URLS", {f: f"{base}/{n}/xml" for f, n in
                                                    (("cameras", "trafficcameras"), ("events", "events"), ("signs", "dmsstatus"))})
    h = HubRun(hub_cfg(tmp_path))
    try:
        assert h.app.roads.thruway is not None and h.app.roads.configured
        assert wait_for(lambda: all(h.app.roads.thruway.status[f]["at"] for f in ("events", "signs", "cameras")))
        doc = h.get("/api/cad/roads")
        assert doc["configured"] is True and "511NY not configured" in doc["note"]
        assert len(doc["cameras"]) == 15 and len(doc["events"]) == 2 and len(doc["signs"]) == 8
        assert {c["source"] for c in doc["cameras"]} == {"Thruway"}
        src = {s["name"]: s for s in doc["sources"]}
        assert src["thruway"]["configured"] and src["thruway"]["label"] == "New York State Thruway Authority" and src["thruway"]["at"]
        assert not src["511ny"]["configured"]
        code, body = req(h.url + "/api/cad/cameras/1ne01370s/image")
        assert code == 200 and body == JPEG
        assert all(p.startswith("/api/data/") or p.startswith("/snap/") for p in fake511.requests)
    finally:
        h.close()


# ---- the page (static: tools/ui_consistency.py checks it rendered, tests/test_design.py its classes) ------------

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"


def test_the_layers_control_is_on_every_map_with_its_six_layers():
    import re
    html = (STATIC / "cad.html").read_text()
    order = [html.index(f'src="/static/{n}"') for n in ("map-style.js", "maplayers.js", "cad.js", "cadmap.js")]
    assert order == sorted(order)                                    # loaded before the maps are made
    js = (STATIC / "maplayers.js").read_text()
    layers = re.findall(r'\{ key: "(\w+)", label: "([^"]+)", feed: (true|false), minzoom: (\d+), on: (true|false) \}', js)
    assert [(k, label, on) for k, label, _, _, on in layers] == [
        ("cameras", "Cameras", "true"), ("incidents", "Incidents & closures", "true"), ("roadwork", "Roadwork", "false"),
        ("signs", "Message signs", "false"), ("mileposts", "Mileposts", "false"), ("exits", "Exits", "false")]
    zooms = dict((k, int(z)) for k, _, _, z, _ in layers)
    assert zooms["mileposts"] == zooms["exits"] >= 12          # whole miles with the exits, only when zoomed in
    assert 'minzoom: 14, filter: ["!", ["get", "whole"]]' in js                      # the tenths later still
    # each viewer's choice, and localStorage only ever inside a try
    lines = js.splitlines()
    uses = [i for i, ln in enumerate(lines) if "localStorage." in ln]
    assert len(uses) == 2
    for i in uses:
        assert any("try {" in lines[j] for j in range(i - 2, i + 1)), f"localStorage outside a try: {lines[i].strip()}"
    # the browser only ever asks the hub: no 511NY API, key or camera host here
    assert "/api/cad/roads" in js and "/api/cad/cameras/" in js
    assert not re.search(r"carsprogram|511ny\.org/(api|map)|[?&]key=|skyvdn|nyssnapshot", js)
    assert "visibilitychange" in js and "CAM_REFRESH_MS = 10000" in js                # ~10 s, paused when hidden
    cadmap = (STATIC / "cadmap.js").read_text()
    assert "MapLayers.attach(this.map, host, ctrls)" in cadmap and 'MapLayers.cameras(inc, $("#d-cams"))' in cadmap
    assert 'MapLayers.cameras(act, $("#ov-cams"), { small: true })' in cadmap              # the wall board's two (#95)
    assert 'id="d-cams"' in html and 'id="ov-cams"' in html
    assert "/api/cad/cameras?" in js and "CAM_REFRESH_MS)" in js and 'aria-expanded' in js   # live, collapsible
    assert '"ml-cam-down"' in js and "Hide down cameras" in js and "cameras down" in js       # down cameras (#99)
    assert "Down since" in js and "Snapshot, updated every" in js
    sprite = (STATIC / "icons.svg").read_text()
    assert 'id="i-layers"' in sprite and 'id="i-camera"' in sprite
