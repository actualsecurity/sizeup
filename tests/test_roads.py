"""The highway layers (sizeup/roads.py, #73): 511NY's cameras, events and message signs for the maps,
the camera snapshot proxy, and the mile markers and exits from the gazetteer.

Never 511NY or a camera itself (conftest.py refuses any host but this one): the feeds are the
fixtures in tests/fixtures/511ny/, in the shapes 511NY has documented (the developer API's field
lists, and the camera inventory as XML with nested Views). They are not recorded from the live feeds:
access to those is by request (https://511ny.org/help/24); re-record them from the approved feeds
under the same names."""

import json
import sqlite3
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sizeup import roads as roads_mod
from sizeup.config import ConfigError, load
from sizeup.roads import (IMAGE_STALE_S, IMAGE_TTL_S, REFUSED_S, Roads, decode, highway_places, parse_cameras,
                          parse_events, parse_signs)
from tests.test_access import AUD, TEAM, jwks, jwt, keys, req  # noqa: F401  (fixtures)
from tests.test_cloud import HubRun, hub_cfg
from tests.test_settings import post

FIX = Path(__file__).parent / "fixtures" / "511ny"
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
        self.fail: dict[str, Exception] = {}
        self.image = (200, {"content-type": "image/jpeg", "last-modified": formatdate(1_791_914_990, usegmt=True)}, JPEG)

    def __call__(self, url, timeout, limit):
        self.urls.append(url)
        for feed, body in self.feeds.items():
            if url.startswith(f"https://feeds.example/{feed}?"):
                if feed in self.fail:
                    raise self.fail[feed]
                return 200, {"content-type": "application/json"}, body
        if url.startswith("https://511ny.org/map/Cctv/"):
            if "image" in self.fail:
                raise self.fail["image"]
            return self.image
        raise AssertionError(f"unexpected fetch {url}")


def make(tmp_path=None, key=KEY, urls=URLS, fetch=None, clock=None, **kw):
    logs = []
    r = Roads(key, urls, cache=(tmp_path / "roads511.json") if tmp_path else None, fetch=fetch or FakeFetch(),
              log=logs.append, clock=clock or Clock(), gazetteer=kw.pop("gazetteer", Path("/nonexistent")), **kw)
    r.logs = logs
    return r


def everything(r: Roads):
    for f in ("events", "signs", "cameras"):
        assert r.refresh(f), (f, r.status[f])


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
                            "lat": 41.0012, "lon": -73.6748, "url": "https://511ny.org/map/Cctv/4616", "has_video": True,
                            "off": False}
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
    assert r.configured and r.why_not() == "" and r.due() == ["cameras", "events", "signs"]
    everything(r)
    assert sorted(f.urls) == sorted(u.replace("{key}", KEY) for u in URLS.values())
    pub = r.public()
    assert pub["configured"] and pub["attribution"] == "powered by 511NY" and pub["link"] == "https://511ny.org/"
    assert [c["id"] for c in pub["cameras"]] == ["4631", "4620", "4616"]
    assert all(set(c) == {"id", "name", "road", "dir", "lat", "lon", "has_video", "off"} for c in pub["cameras"])
    assert {s: pub["status"][s]["n"] for s in pub["status"]} == {"cameras": 3, "events": 3, "signs": 2}
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
    assert r.due() == []
    clock.t += 2
    assert r.due() == ["events"]
    clock.t += 300
    assert r.due() == ["events", "signs"]
    clock.t += 3600
    assert r.due() == ["cameras", "events", "signs"]
    with pytest.raises(ConfigError, match="10 calls a minute"):
        load_cfg(tmp_path, "[roads]\nevents_every_s = 20\n")
    # whatever is asked for, never faster than the floors (5 calls a minute for all three)
    fast = make(every={"cameras": 1, "events": 1, "signs": 1})
    assert fast.every == {"cameras": 600.0, "events": 60.0, "signs": 60.0}


def test_the_disk_cache_saves_calls_after_a_restart(tmp_path):
    clock = Clock()
    make(tmp_path, clock=clock).refresh("cameras")
    f = FakeFetch()
    again = make(tmp_path, fetch=f, clock=clock)
    assert [c["id"] for c in again.public()["cameras"]] == ["4631", "4620", "4616"]
    assert again.due() == ["events", "signs"] and f.urls == []
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
    assert r._thread is None and "not configured" in r.logs[-1]
    pub = r.public()
    assert (pub["configured"], pub["cameras"], pub["events"], pub["signs"]) == (False, [], [], [])
    assert "SIZEUP_511NY_KEY" in pub["note"]
    code, body, _, _ = r.get("/api/cad/cameras/4616/image", "")
    assert code == 404 and b"not configured" in body
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
    st = r.public()["status"]["events"]
    assert "refused the key" in st["error"] and r.status["events"]["next"] == clock.t + REFUSED_S
    f.fail["signs"] = HTTPError("", 429, "Too Many Requests", None, None)
    assert not r.refresh("signs") and "rate limit" in r.status["signs"]["error"]
    f.fail["cameras"] = OSError(f"connection reset fetching ...?key={KEY}")
    assert not r.refresh("cameras") and r.status["cameras"]["next"] == clock.t + 600    # its floor, not an hour
    f.feeds["events"] = b"<!doctype html><html></html>"
    del f.fail["events"]
    assert not r.refresh("events") and "HTML" in r.status["events"]["error"]
    assert r.status["events"]["next"] == clock.t + 120                       # the second failure: 2 min
    blob = json.dumps(r.public()) + "\n".join(r.logs)
    assert KEY not in blob and "***" in "\n".join(r.logs)
    # a good read clears the error
    f.feeds["events"] = fixture("events.json")
    assert r.refresh("events") and r.status["events"]["error"] == ""


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
        assert wait_for(lambda: all(h.app.roads.status[f]["at"] for f in ("events", "signs", "cameras")))
        assert all(f"key={KEY}" in p for p in fake511.requests)
        assert req(h.url + "/api/cad/roads")[0] == 401                               # Access: a person only
        assert req(h.url + "/api/cad/cameras/4616/image")[0] == 401
        val = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email="val@example.org")}     # a viewer may look
        code, doc = req(h.url + "/api/cad/roads", headers=val)
        assert code == 200 and doc["configured"] and len(doc["cameras"]) == 3 and len(doc["signs"]) == 2
        code, img = req(h.url + "/api/cad/cameras/4616/image", headers=val)
        assert code == 200 and img == JPEG
        code, img = req(h.url + "/api/cad/cameras/4616/image", headers=val)
        assert sum(p.startswith("/cctv/") for p in fake511.requests) == 1             # the second from the cache
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
    h = HubRun(hub_cfg(tmp_path))
    try:
        doc = h.get("/api/cad/roads")
        assert doc["configured"] is False and "not configured" in doc["note"]
        code, body = req(h.url + "/api/cad/cameras/4616/image")
        assert code == 404
        assert h.app.roads._thread is None
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
    assert dict((k, int(z)) for k, _, _, z, _ in layers)["mileposts"] >= 14          # only when zoomed in
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
    assert "MapLayers.attach(this.map, host, ctrls)" in cadmap and "MapLayers.nearby(inc" in cadmap
    sprite = (STATIC / "icons.svg").read_text()
    assert 'id="i-layers"' in sprite and 'id="i-camera"' in sprite
