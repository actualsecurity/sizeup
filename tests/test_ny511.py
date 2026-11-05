"""511NY on CARS (GitHub #73, #188): the public signs feed, the CARS API's events (HTTP Basic), the CARS-Hub's CCTV
inventory (HTTP Basic), and what the hub does with them (sizeup/roads.py, sizeup/roadprojects.py, sizeup/datasources.py,
tools/ny511_probe.py). No network: fixtures in tests/fixtures/511ny/:

  cars_signs_recorded.json      RECORDED: nine records of https://nyapi.carsprogram.org/signs.json as served on
                                2026-11-04 (Last-Modified: Wed, 04 Nov 2026 21:44:39 GMT; public data, no login):
                                seven around Port Chester (the Hutch at Exit 19A, I-287 near Exit 7 and at the HRP,
                                I-684, NY 100, the Bronx River Pkwy) and two far away (New York City, Region 9)
  cars_events_constructed.json  CONSTRUCTED from the CARS API's Swagger definition (carsapi-definition.json, 1.2.0:
                                CARSEvent, Detail, Lane, LinkLocation, ZonedDateTime, ...): no access yet to record
  cars_cctv_recorded.xml        RECORDED 2026-11-04: https://nysdot.carsprogram.org/hub/data/cctv.xml (TMDD's CCTV
                                schema), trimmed: six NYSDOT cameras around Port Chester (the Thruway's I-95 exit 21
                                one among them), one in Queens, one NYC DOT camera
"""

from __future__ import annotations

import base64
import gzip
import json
import threading
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sizeup import datasources as ds
from sizeup import roadprojects as rw
from sizeup import roads as r
from sizeup.roadprojects import Feed, RoadProjects, windows
from sizeup.roads import BasicAuth, Roads

FIX = Path(__file__).parent / "fixtures" / "511ny"
TFIX = Path(__file__).parent / "fixtures" / "thruway"
RWFIX = Path(__file__).parent / "fixtures" / "roadwork"
PUBLISHED = "Wed, 04 Nov 2026 21:44:39 GMT"
PUB = parsedate_to_datetime(PUBLISHED).timestamp()
NOW = 1_791_915_000.0                     # 2026-10-13 15:30 EDT: the constructed events' afternoon
USER, PASS = "pcfd-511-user", "S3cret pass-for-511NY!"
TOKEN = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
WAITING = "waiting for credentials (SSM /sizeup-hub/511ny-user and /sizeup-hub/511ny-pass)"
EVENTS, SIGNS, HUB = "https://cars.example/api/events", "https://signs.example/signs.json", "https://hub.example/hub/data/cctv.xml"
STILLS = "https://public.carsprogram.org/cameras/"
TURLS = {"cameras": "https://thruway.example/cameras", "events": "https://thruway.example/events",
         "signs": "https://thruway.example/signs"}


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Fetch:
    """CARS as it answers: the API and the CARS-Hub only with the right Authorization, the signs with an ETag and
    Last-Modified (304 to a matching If-None-Match); the Thruway's recordings. Every call and its headers kept."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.bodies = {EVENTS: fx("cars_events_constructed.json"), SIGNS: fx("cars_signs_recorded.json"),
                       HUB: fx("cars_cctv_recorded.xml"), TURLS["cameras"]: (TFIX / "trafficcameras.xml").read_bytes(),
                       TURLS["events"]: (TFIX / "events.xml").read_bytes(), TURLS["signs"]: (TFIX / "dmsstatus.xml").read_bytes()}

    def __call__(self, url, timeout, limit, headers=None):
        headers = dict(headers or {})
        self.calls.append((url, headers))
        if url in (EVENTS, HUB) and headers.get("Authorization") != f"Basic {TOKEN}":
            raise HTTPError(url, 401, "Unauthorized", None, None)
        if url == SIGNS:
            if headers.get("If-None-Match") == '"s1"':
                return 304, {"etag": '"s1"'}, b""
            return 200, {"content-type": "application/json", "etag": '"s1"', "last-modified": PUBLISHED}, self.bodies[SIGNS]
        if url in self.bodies:
            return 200, {"content-type": "application/json"}, self.bodies[url]
        if url.startswith(STILLS):
            return 200, {"content-type": "image/jpeg"}, b"\xff\xd8jpeg"
        raise AssertionError(f"unexpected fetch {url}")

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


def make(fetch=None, clock=None, user=USER, password=PASS, hub="", imagery=True, thruway=False, tmp_path=None, logs=None,
         county=False):
    logs = [] if logs is None else logs
    rd = Roads("", {"events": EVENTS, "signs": SIGNS}, cache=(tmp_path / "roads511.json") if tmp_path else None,
               fetch=fetch or Fetch(), log=logs.append, clock=clock or Clock(), gazetteer=Path("/nonexistent"),
               thruway=thruway, thruway_urls=TURLS if thruway else {}, county=county, auth=BasicAuth(user, password),
               county_urls={"cameras": "https://county.example/cameras"} if county else {},
               auth_feeds=("events",), carshub=True, carshub_url=hub, carshub_imagery=imagery)
    rd.logs = logs
    rd.pump_threads = False
    return rd


# ---- the signs: recorded, our area, the time put right ------------------------------------------------------------


def test_the_recorded_signs_our_area_their_text_and_status():
    signs = r.parse_cars_signs(fx("cars_signs_recorded.json"), r.DEFAULT_BBOX, {"published": PUB, "now": PUB + 60})
    by = {s["id"].split("-")[-1]: s for s in signs}
    assert len(signs) == 7 and "501" not in by and not any("Region 9" in s["id"] for s in signs)   # NYC, Region 9: not ours
    hutch = by["198080"]
    assert (hutch["name"], hutch["road"], hutch["dir"], hutch["status"]) == ("HRP NB South of Exit 19A [PVMS-166]", "Hutch",
                                                                             "Northbound", "on")
    assert hutch["pages"] == [["LOW", "BRIDGES", "AHEAD"], ["TRUCKS", "EXIT", "NOW"]] and hutch["lines"][:3] == ["LOW", "BRIDGES", "AHEAD"]
    assert hutch["mile"] == "13.9" and hutch["where"] == "907W N at MP 13.9 (White Plains)" and hutch["agency"] == "NY Signs - Region 3"
    assert by["173080"]["status"] == "fault" and by["173080"]["lines"] == []           # ERROR_OR_FAILURE: no message
    assert by["428080"]["status"] == "blank" and by["428080"]["road"] == "I-287" and by["428080"]["dir"] == "Westbound"
    assert by["397080"]["lines"] == ["TIME TO GMCB VIA", "I287 EX4 - 13 MN", "NY119 - 18 MN"]
    # Region 3's times are as given (2 min before the file), never moved
    assert hutch["updated"] == 1793828582.0 and hutch["updated"] < PUB


def test_a_region_whose_times_run_hours_ahead_is_put_right_by_the_file_not_by_its_neighbours():
    # New York City's signs ran up to 5 h after the file's Last-Modified (Eastern wall time written as UTC)
    nyc = (-74.3, 40.5, -73.7, 40.95)
    body = fx("cars_signs_recorded.json")
    raw = next(x for x in json.loads(body) if x["id"] == "nyRegion11Signs*501")["lastUpdated"] / 1000
    s = r.parse_cars_signs(body, nyc, {"published": PUB, "now": PUB + 60})
    assert [x["id"] for x in s] == ["nyRegion11Signs*501"]
    assert raw > PUB + 300 and s[0]["updated"] == raw - 5 * 3600 <= PUB              # EST: 5 h back
    assert r.parse_cars_signs(fx("cars_signs_recorded.json"), r.DEFAULT_BBOX, {"published": None, "now": None})[0]["updated"]
    # a time later than the file whatever the offset: no time at all, never a future one
    far = json.dumps([{"id": "x*1", "status": "BLANK", "lastUpdated": (PUB + 30 * 3600) * 1000, "agencyId": "z",
                       "location": {"routeId": "I-95", "latitude": 41.0, "longitude": -73.67}}]).encode()
    assert r.parse_cars_signs(far, r.DEFAULT_BBOX, {"published": PUB, "now": PUB})[0]["updated"] is None


def test_the_array_is_read_one_record_at_a_time():
    assert list(r.iter_json_array(' [ {"a": 1} , {"b": [2, 3]} ] ')) == [{"a": 1}, {"b": [2, 3]}]
    assert list(r.iter_json_array("[]")) == []
    for bad in ("{}", '[{"a": 1}', '[{"a": }]'):
        with pytest.raises(ValueError):
            list(r.iter_json_array(bad))


def test_signs_merge_with_the_thruway_once_where_they_are_the_same_sign():
    """I-287 WB near Exit 7: NYSDOT's VMS 286 and the Thruway's DMSCW4W, 26 m apart, both blank: one (511NY's).
    The same two saying different things: both. The Thruway's other signs: theirs."""
    f = Fetch()
    rd = make(f, thruway=True)
    assert rd.refresh("signs")
    signs = rd.merged("signs")
    ids = {s["id"] for s in signs}
    assert "nySigns:Region 3:*Region 3-428080" in ids and "DMSCW4W" not in ids
    assert "DMSNE11N" in ids and sum(s["source"] == "Thruway" for s in signs) == 7 and sum(s["source"] == "511NY" for s in signs) == 7
    doc = json.loads(f.bodies[SIGNS])
    vms = next(x for x in doc if x["id"].endswith("428080"))
    vms["status"], vms["display"] = "DISPLAYING_MESSAGE", {"pages": [{"lines": ["CRASH AT EXIT 8", "RIGHT LANE CLOSED"]}]}
    f.bodies[SIGNS] = json.dumps(doc).encode()
    rd.ny.status["signs"]["etag"] = ""
    assert rd.refresh("signs")
    ids = {s["id"] for s in rd.merged("signs")}
    assert {"nySigns:Region 3:*Region 3-428080", "DMSCW4W"} <= ids                  # not the same text: both shown
    # the same text, but facing the other way: two signs
    a = {"id": "a", "lat": 41.0, "lon": -73.67, "lines": ["X"], "dir": "Northbound"}
    assert r.sign_text(a) == "X" and r.sign_text({"lines": ["  x  ", "y"]}) == "X Y"


# ---- the events: the documented shape, normalised --------------------------------------------------------------------


def events():
    return {e["id"]: e for e in r.parse_cars_events(fx("cars_events_constructed.json"))}


def test_cars_events_our_area_only_and_never_one_not_for_us():
    ev = events()
    assert set(ev) == {"NYSDOT-CARS-31001", "NYSDOT-CARS-31002", "NYSDOT-CARS-31003", "NYSDOT-CARS-31007"}
    # 31004: ORIGINATING_AGENCY access; 31005: ENDED; 31006: Albany


def test_a_live_incident_with_its_lanes_and_no_made_up_end():
    e = events()["NYSDOT-CARS-31001"]
    assert (e["layer"], e["road"], e["dir"], e["subtype"], e["mile"], e["cross_from"]) == \
        ("incidents", "I-287", "Westbound", "crash", "8.7", "Exit 10")
    assert e["description"] == "Crash on I-287 westbound at Exit 10 (Rye Brook). Right lane closed."
    assert e["lanes"] == "1 of 3 lanes affected (westbound)" and e["closed"] and e["closure"] == "lanes"
    assert e["severity"] == "priority 2" and e["start"] == 1791918300.0 and e["updated"] == 1791919200.0
    assert e["end"] is None                                   # duration-was-specified false: CARS's end isn't anyone's


def test_a_full_closure_and_its_reopening():
    e = events()["NYSDOT-CARS-31003"]
    assert e["layer"] == "incidents" and e["closed"] and e["full_closure"] and e["closure"] == "full"
    assert e["lanes"] == "2 of 2 lanes affected" and e["description"] == "Road closed. Downed wires"
    assert e["end"] == e["reopen"] == 1791928800.0 and e["cross_from"] == "Hillandale Rd"


def test_planned_roadwork_with_its_occurrences_detour_and_line():
    e = events()["NYSDOT-CARS-31002"]
    assert e["layer"] == "roadwork" and e["road"] == "NY 120A" and (e["cross_from"], e["cross_to"]) == ("Westchester Ave", "Glen Ave")
    assert e["occurrences"] == [[1791939600.0, 1791968400.0], [1792026000.0, 1792054800.0]]     # 9 PM - 5 AM, two nights
    assert e["detour"] == "Required detour via Glen Ave, N Regent St" and e["hours_text"] == "Mon, Tue, Wed, Thu, Fri, night"
    assert e["line"][0] == [-73.6729, 41.005] and len(e["line"]) == 3 and e["link"] == "https://511ny.org/"
    assert e["description"] == "Road construction. Drainage work; flaggers on site." and not e["closed"]


def test_511ny_wins_the_tie_with_the_thruway():
    """The crash at I-287 WB exit 10: 511NY's (NYSDOT-CARS-31001) and the Thruway's (CAD-262860247), 19 m apart."""
    rd = make(thruway=True)
    for f in ("events", "signs"):
        assert rd.refresh(f)
    ids = {e["id"]: e["source"] for e in rd.merged("events")}
    assert ids["NYSDOT-CARS-31001"] == "511NY" and "CAD-262860247" not in ids
    assert ids["CAD-262740341"] == "Thruway"                                  # the Thruway's roadwork elsewhere stays


def test_road_projects_take_511ny_planned_work_dated_and_fold_wzdx_into_it():
    rd = make(thruway=True)
    rd.refresh("events")
    wz = Feed("wzdx", "511NY", "511NY work zones (WZDx)", "https://511ny.org/", "", "https://cars.example/api/wzdx", "wzdx", 300,
              auth=BasicAuth(USER, PASS))
    f = Fetch()
    f.bodies["https://cars.example/api/wzdx"] = (RWFIX / "wzdx_constructed.geojson").read_bytes()
    p = RoadProjects([wz], roads=rd, clock=Clock(), fetch=lambda u, t, n, h: (f.calls.append((u, h)) or
                                                                              (200, {}, f.bodies[u])), log=lambda m: None)
    p.ask()
    assert p.tick() == 1 and f.calls[-1][1]["Authorization"] == f"Basic {TOKEN}"    # the WZDx: the same username, password
    by = {i["id"]: i for i in p.items()}
    king = by["511ny:NYSDOT-CARS-31002"]
    assert (king["source"], king["closure"], king["cross_from"], king["cross_to"]) == ("511NY", "lanes", "Westchester Ave", "Glen Ave")
    assert king["detour"] == "Required detour via Glen Ave, N Regent St" and king["geometry"]["type"] == "LineString"
    assert windows(king, NOW) == [[1791939600.0, 1791968400.0], [1792026000.0, 1792054800.0]] and rw.dated(king)
    assert rw.state(windows(king, NOW), NOW) == "upcoming" and rw.state(windows(king, 1791940000.0), 1791940000.0) == "active"
    assert windows(king, 1792060000.0) == []                                     # after its last night: ended
    main = by["511ny:NYSDOT-CARS-31007"]
    assert "wzdx:wz-2" not in by and main["also"] == ["511NY (wzdx)"]           # the same N Main St paving: 511NY's events win
    assert "511ny:NYSDOT-CARS-31001" not in by                                  # an incident: the incidents layer, not here
    blob = json.dumps(p.public())
    assert PASS not in blob and TOKEN not in blob


# ---- the schedule and the conditional GET ---------------------------------------------------------------------------


def test_every_five_minutes_with_if_none_match_and_a_304_keeps_the_signs(tmp_path):
    clock, f = Clock(), Fetch()
    rd = make(f, clock, tmp_path=tmp_path)
    assert rd.ny.every["signs"] == rd.ny.every["events"] == 300.0
    assert sorted(rd.due()) == [("511ny", "events"), ("511ny", "signs")]
    assert rd.refresh("signs") and "If-None-Match" not in f.calls[-1][1]
    n = len(rd.merged("signs"))
    clock.t += 299
    assert ("511ny", "signs") not in rd.due()
    clock.t += 1
    assert ("511ny", "signs") in rd.due()
    assert rd.refresh("signs")
    assert f.calls[-1][1]["If-None-Match"] == '"s1"' and f.calls[-1][1]["If-Modified-Since"] == PUBLISHED
    assert len(rd.merged("signs")) == n and rd.ny.status["signs"]["at"] == clock.t       # a 304: kept, and when we looked
    assert rd.ny.status["signs"]["next"] == clock.t + 300
    assert rd.refresh("signs", fresh=True) and "If-None-Match" not in f.calls[-1][1]       # Refresh now: afresh
    again = make(Fetch(), clock, tmp_path=tmp_path)                                      # a restart keeps the validators
    assert again.ny.status["signs"]["etag"] == '"s1"' and len(again.merged("signs")) == n
    with pytest.raises(Exception, match="once every 5 minutes"):
        from tests.test_roads import load_cfg
        load_cfg(tmp_path, "[roads]\nsigns_every_min = 2\n")


def test_http_get_asks_for_gzip_and_never_carries_the_authorization_along_a_redirect():
    seen: list[dict] = []
    body = json.dumps([{"a": 1}]).encode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append({"path": self.path, "auth": self.headers.get("Authorization", ""),
                         "enc": self.headers.get("Accept-Encoding", "")})
            if self.path == "/moved":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}/there")
                self.end_headers()
                return
            z = gzip.compress(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(z)))
            self.end_headers()
            self.wfile.write(z)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        auth = BasicAuth(USER, PASS).header()
        code, hdrs, got = r.http_get(f"http://127.0.0.1:{port}/feed", 5, 1024 * 1024, auth)
        assert code == 200 and got == body and seen[-1]["enc"] == "gzip" and seen[-1]["auth"] == f"Basic {TOKEN}"
        code, _, got = r.http_get(f"http://127.0.0.1:{port}/moved", 5, 1024 * 1024, auth)
        assert code == 200 and got == body and seen[-1]["path"] == "/there" and seen[-1]["auth"] == ""
        with pytest.raises(ValueError, match="more than"):
            r.http_get(f"http://127.0.0.1:{port}/feed", 5, 4)
    finally:
        srv.shutdown()


# ---- the credentials: from the environment, only in the header, never logged ----------------------------------------


def test_the_username_and_password_come_from_the_environment_and_only_go_in_the_header(tmp_path, monkeypatch):
    from tests.test_roads import load_cfg
    monkeypatch.setenv("SIZEUP_511NY_USER", USER)
    monkeypatch.setenv("SIZEUP_511NY_PASS", PASS)
    monkeypatch.setattr(r, "NY511_URLS", {"events": EVENTS, "signs": SIGNS, "wzdx": "https://cars.example/api/wzdx",
                                          "hub": "https://hub.example/hub/"})
    cfg = load_cfg(tmp_path, "")
    logs: list[str] = []
    f = Fetch()
    rd = Roads.from_config(cfg, log=logs.append)
    rd.fetch = rd.ny.fetch = f
    assert rd.ny.urls["events"] == EVENTS and rd.ny.urls["signs"] == SIGNS and rd.ny.feed_on("events")
    assert rd.refresh("events", "511ny") and rd.refresh("signs", "511ny")
    hdr = dict(f.calls)
    assert hdr[EVENTS]["Authorization"] == f"Basic {TOKEN}" and "Authorization" not in hdr[SIGNS]
    assert repr(rd.auth) == "BasicAuth(set)" and PASS not in repr(rd.ny.auth) and PASS not in str(vars(rd.ny).get("auth"))
    # refused: said, waits 30 min, and nothing of them anywhere
    f.bodies[EVENTS] = b""
    rd.ny.auth = BasicAuth(USER, "wrong-password-xyz")
    rd.ny.status["events"]["etag"] = ""
    assert not rd.refresh("events", "511ny")
    st = rd.ny.status["events"]
    assert st["error"].startswith("511NY refused the username and password") and st["next"] > st.get("checked", 0)
    blob = json.dumps(rd.public()) + "\n".join(logs) + json.dumps(rd.ny.status)
    for secret in (PASS, TOKEN, "wrong-password-xyz", USER):
        assert secret not in blob
    assert r.redact(f"failed for {USER}:{PASS} {TOKEN}", BasicAuth(USER, PASS).secrets()) == "failed for ***:*** ***"
    # the WZDx feed of road projects: the same pair
    feeds = {x.name: x for x in rw.feeds_from_config(cfg)}
    assert feeds["wzdx"].on and feeds["wzdx"].headers() == {"Authorization": f"Basic {TOKEN}"}
    assert PASS not in json.dumps(feeds["wzdx"].public()) and PASS not in feeds["wzdx"].redact(f"x {PASS} y")


def test_a_refused_password_waits_half_an_hour_and_says_so():
    clock, f = Clock(), Fetch()
    rd = make(f, clock, password="not-the-password")
    assert not rd.refresh("events")
    st = rd.ny.status["events"]
    assert st["error"] == "511NY refused the username and password (HTTP 401)" and st["next"] == NOW + r.REFUSED_S
    assert "not-the-password" not in "\n".join(rd.logs)


# ---- waiting for the credentials: said everywhere, nothing fetched -----------------------------------------------------


def test_without_credentials_the_signs_still_come_and_everything_else_says_it_waits(tmp_path, monkeypatch):
    from tests.test_roads import load_cfg
    monkeypatch.setattr(r, "NY511_URLS", {"events": EVENTS, "signs": SIGNS, "wzdx": "https://cars.example/api/wzdx",
                                          "hub": "https://hub.example/hub/"})
    cfg = load_cfg(tmp_path, "")
    f = Fetch()
    logs: list[str] = []
    rd = Roads.from_config(cfg, log=logs.append)
    for p in rd.providers:
        p.fetch = f
    assert rd.configured and rd.ny.feed_on("signs") and not rd.ny.feed_on("events")
    assert rd.ny.feed_why("events") == WAITING and rd.due() == [("511ny", "signs")]
    assert rd.refresh("events") is False and f.urls() == []
    assert rd.refresh("signs") and f.urls() == [SIGNS]
    pub = rd.public()
    assert f"511NY incidents and closures: {WAITING}" in pub["note"]
    src = {s["name"]: s for s in pub["sources"]}
    assert src["511ny"]["status"]["events"]["why"] == WAITING and src["511ny"]["label"] == "511NY (NYSDOT)"
    assert not src["carshub"]["configured"] and src["carshub"]["status"]["cameras"]["why"] == WAITING
    assert rd.carshub.urls["cameras"] == HUB
    rd.start()
    rd.stop()
    assert sum(WAITING in m for m in logs) == 2                                   # the events and the CARS-Hub's cameras
    # Settings > Data sources: every 511NY feed has its row, every 5 min, Refresh now only for what is on
    reg = ds.DataSources(clock=Clock(), spawn=lambda fn, name: fn())
    for s in ds.roads_sources(rd):
        reg.register(s)
    rows = {x["key"]: x for x in reg.public()["sources"]}
    assert set(rows) == {"roads:511ny:events", "roads:511ny:signs", "roads:carshub:cameras"}
    ev = rows["roads:511ny:events"]
    assert not ev["on"] and ev["why"] == WAITING and not ev["refreshable"] and ev["builtin"] == "every 5 min"
    sg = rows["roads:511ny:signs"]
    assert sg["on"] and sg["refreshable"] and sg["builtin"] == "every 5 min" and sg["min_gap_s"] == 300 and sg["items"] == 7
    assert sg["name"] == "511NY message signs" and sg["attribution"] == "powered by 511NY"
    hub = rows["roads:carshub:cameras"]
    assert not hub["on"] and hub["why"] == WAITING and hub["name"] == "511NY CARS-Hub cameras (CCTV XML)"
    assert hub["builtin"] == "every 1 h" and hub["min_gap_s"] == 300
    # Road projects: 511NY's roadwork and its WZDx wait too
    p = RoadProjects.from_config(cfg, roads=rd, log=lambda m: None)
    srcs = {s["name"]: s for s in p.sources()}
    assert srcs["511ny"]["why"] == WAITING and srcs["wzdx"]["why"] == WAITING and not srcs["wzdx"]["on"]


def test_ny511_off_and_each_feed_off(tmp_path, monkeypatch):
    from tests.test_roads import load_cfg
    monkeypatch.setattr(r, "NY511_URLS", {"events": EVENTS, "signs": SIGNS, "wzdx": "https://cars.example/api/wzdx",
                                          "hub": "https://hub.example/hub/"})
    off = Roads.from_config(load_cfg(tmp_path, "[roads]\nny511 = false\n"), log=lambda m: None)
    assert not off.ny.configured and off.carshub is None and "511NY off" in off.public()["note"]
    no_signs = Roads.from_config(load_cfg(tmp_path, '[roads]\nsigns_url = "off"\n'), log=lambda m: None)
    assert no_signs.ny.urls["signs"] == "" and no_signs.ny.urls["events"] == EVENTS
    assert no_signs.carshub.urls["cameras"] == HUB
    no_hub = Roads.from_config(load_cfg(tmp_path, '[roads]\nny511_hub = "off"\n'), log=lambda m: None)
    assert no_hub.carshub.urls["cameras"] == "" and no_hub.carshub.feed_why("cameras") == 'off ([roads] ny511_hub = "off")'


# ---- the CARS-Hub's CCTV inventory: recorded, compared with ours, merged ------------------------------------------------


def test_the_cctv_inventory_our_area_one_camera_at_a_time():
    cams = {c["id"]: c for c in r.parse_carshub_cctv(fx("cars_cctv_recorded.xml"))}
    assert set(cams) == {"35992", "34715", "34353", "35589", "36011", "34259"}             # Queens and NYC DOT: not ours
    c = cams["35992"]
    assert (c["name"], c["road"], c["owner"], c["has_video"], c["off"]) == ("Exit 16 Purchase St MM 16.6", "Hutch", "NYSDOT", True, False)
    assert (c["lat"], c["lon"]) == (41.022393, -73.709693)                                  # microdegrees
    assert c["url"] == STILLS + "NYSDOT/R8_039.flv.png" and c["video"] == "https://s51.nysdot.skyvdn.com/rtplive/R8_039/playlist.m3u8"
    assert c["updated"] == 1793818122.0                                                     # 20261104 134842 -0500
    assert cams["35589"]["dir"] == "Westbound" and cams["36011"]["dir"] == "Southbound"     # from the name: none given
    assert cams["34259"]["video"] == "" and cams["34259"]["road"] == "NY 120A"
    assert r.tmdd_time(None) is None
    with pytest.raises(ValueError, match="bad XML"):
        r.parse_carshub_cctv(b"<cctvInventories><inventory-item>")


def county_fetch(f: Fetch) -> Fetch:
    f.bodies["https://county.example/cameras"] = (Path(__file__).parent / "fixtures" / "county" / "cameras.json").read_bytes()
    return f


def test_the_cctv_cameras_are_compared_with_ours_and_logged():
    f = county_fetch(Fetch())
    rd = make(f, hub=HUB, thruway=True, county=True, imagery=False)
    assert rd.carshub.feed_on("cameras") and rd.carshub.every["cameras"] == 3600.0
    assert rd.refresh("cameras", "thruway") and rd.refresh("cameras", "county")
    assert rd.refresh("cameras", rd.carshub)
    assert f.urls().count(HUB) == 1 and dict(f.calls)[HUB]["Authorization"] == f"Basic {TOKEN}"
    cmp = rd.camera_compare
    # I-95 exit 21 SB is the Thruway's 1ne01370s (7 m), I-287 WB east of exit 9 its 1cw00780w (39 m), the King St SB
    # approach the county's wc23 (the same name; not its SB Willett approach, wc24, at the same corner)
    assert (cmp["n"], cmp["known"], cmp["known_by"], cmp["new"]) == (6, 3, {"County": 1, "Thruway": 2}, 3)
    assert cmp["new_by_road"] == {"Hutch": 3} and set(cmp["new_ids"]) == {"35992", "34715", "34353"}
    text = "6 cameras in our area: 3 we have (County 1, Thruway 2), 3 new (Hutch 3); not shown ([roads] carshub_imagery = false)"
    assert any(text in m for m in rd.logs) and rd.carshub.status["cameras"]["note"] == text
    assert not any(c["source"] == "511NY" for c in rd.merged("cameras")) and rd.camera("35992") is None


def test_the_new_cctv_cameras_join_ours_and_live_video_wins_a_tie():
    f = county_fetch(Fetch())
    rd = make(f, hub=HUB, thruway=True, county=True)
    for p in ("thruway", "county"):
        rd.refresh("cameras", p)
    rd.refresh("cameras", rd.carshub)
    ids = [c["id"] for c in rd.merged("cameras")]
    assert "36011" not in ids and "35589" not in ids and {"1ne01370s", "1cw00780w"} <= set(ids)   # the Thruway's (live video)
    assert "34259" not in ids and "wc23" in ids                              # the county's: neither has video, ours kept
    assert {"35992", "34715", "34353"} <= set(ids) and {"wc24", "wc25", "wc26"} <= set(ids)
    assert ids.index("35992") > ids.index("1ne01370s")
    c = rd.camera("35992")
    assert c["source"] == "511NY" and c["has_video"] and c["cadence"] == 60
    code, body, ctype, h = rd.image("35992")
    assert code == 200 and ctype == "image/jpeg" and h["ETag"] and "Authorization" not in f.calls[-1][1]
    # live video through the hub's HLS path, under its stream cap
    f.bodies["https://s51.nysdot.skyvdn.com/rtplive/R8_039/playlist.m3u8"] = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=284570\nchunklist_w1.m3u8\n"
    code, body, ctype, _ = rd.stream("35992", "playlist.m3u8")
    assert code == 200 and b"chunklist_w1.m3u8" in body and rd.streams_open() == 1
    wall = rd.area()
    assert "35992" in [x["id"] for x in wall["cameras"]] and wall["streams"]["max"] == r.STREAM_MAX
    # a CARS camera with video where ours is a picture only: the CARS one in its place
    king = next(c for c in rd.carshub.data["cameras"] if c["id"] == "34259")
    king.update(video="https://s7.nysdot.skyvdn.com/rtplive/R8_X/playlist.m3u8", has_video=True)
    rd._changed()
    ids = [c["id"] for c in rd.merged("cameras")]
    assert "34259" in ids and "wc23" not in ids and ids.count("34259") == 1


def test_same_camera_by_place_direction_and_name():
    a = {"name": "I-95 MP 013.80 SB at Interchange 21", "road": "I-95", "dir": "Southbound", "lat": 40.987337, "lon": -73.680267}
    b = {"name": "I-95 at Exit 21", "road": "I-95", "dir": "Southbound", "lat": 40.9874, "lon": -73.68029}
    assert r.same_camera(a, b) and not r.same_camera(a, b | {"dir": "Northbound"})
    assert not r.same_camera(a, b | {"lat": 40.99})                                       # 300 m: another camera
    king = {"name": "SB King Approach - King St/Willett Ave/North Pearl St", "road": "NY 120A", "dir": "Southbound",
            "lat": 41.003494, "lon": -73.663581}
    willett = king | {"name": "SB Wilett Approach - King St/Willett Ave/North Pearl St", "road": "Willett Ave"}
    assert not r.same_camera(king, willett) and r.same_camera(king, dict(king))


def test_the_carshub_comparison_by_itself():
    ours = [{"id": "t1", "name": "I-95 exit 20", "road": "I-95", "lat": 41.0, "lon": -73.67, "source": "Thruway"},
            {"id": "c1", "name": "Main St & Elm", "lat": 41.01, "lon": -73.66, "source": "County"}]
    new = [{"id": "a", "name": "I-95 NB", "lat": 41.0005, "lon": -73.67, "road": "I-95"},
           {"id": "b", "name": "Main St & Elm", "lat": 41.0101, "lon": -73.6601, "road": "US 1"},
           {"id": "c", "name": "HRP x", "lat": 41.03, "lon": -73.68, "road": "Hutchinson River Pkwy"},
           {"id": "d", "name": "", "lat": 41.04, "lon": -73.69, "road": ""}]
    cmp = r.compare_cameras(new, ours)
    assert (cmp["known"], cmp["known_by"], cmp["new"], cmp["new_ids"]) == (2, {"County": 1, "Thruway": 1}, 2, ["c", "d"])
    assert r.compare_text(cmp) == "4 cameras in our area: 2 we have (County 1, Thruway 1), 2 new ((no road) 1, Hutch 1)"


# ---- the probe tool: one request per feed, nothing secret printed or saved ----------------------------------------------


def test_the_probe_makes_one_request_per_feed_and_prints_no_secret(tmp_path, monkeypatch):
    import tools.ny511_probe as probe
    monkeypatch.setattr(r, "NY511_URLS", {"events": EVENTS, "signs": SIGNS, "wzdx": "https://cars.example/api/wzdx",
                                          "hub": "https://hub.example/hub/"})
    monkeypatch.setattr(r, "THRUWAY_URLS", TURLS)
    monkeypatch.setattr(r, "COUNTY_URLS", {"cameras": "https://county.example/cameras"})
    f = Fetch()
    f.bodies["https://county.example/cameras"] = (Path(__file__).parent / "fixtures" / "county" / "cameras.json").read_bytes()
    out: list[str] = []
    kc = {probe.KC_USER: USER, probe.KC_PASS: PASS}
    asked: list[str] = []
    page = (b'<html><title>CARS-Hub</title><a href="data/cctv.xml">data/cctv.xml</a><a href="schemas/CCTV.xsd">s</a>'
            b'<a href="data/feu-g.xml">x</a></html>')
    f.bodies["https://hub.example/hub/index.jsf"] = page
    code = probe.main(["--keychain", "--save", str(tmp_path / "raw"), "--hub", "--hub-base", "https://hub.example/hub/"],
                      fetch=f, out=out.append, keychain_get=lambda s: asked.append(s) or kc.get(s, ""))
    text = "\n".join(out)
    assert code == 0 and asked == [probe.KC_USER, probe.KC_PASS]
    assert sorted(f.urls()) == sorted([SIGNS, EVENTS, HUB, TURLS["cameras"], "https://county.example/cameras",
                                       "https://hub.example/hub/index.jsf"])                       # once each, no loop
    assert "7 in our area: on 5, " in text and "blank 1" in text and "fault 1" in text and "Hutch 2" in text
    assert "4 in our area: incidents 2, roadwork 2" in text
    assert "6 cameras in our area, 5 with video" in text and "public.carsprogram.org 6" in text
    assert "== cameras: 6 cameras in our area: 3 we have (County 1, Thruway 2), 3 new (Hutch 3)" in text
    assert "new:  35992 Hutch        Exit 16 Purchase St MM 16.6  (video)" in text
    assert "feeds: https://hub.example/hub/data/cctv.xml, https://hub.example/hub/data/feu-g.xml" in text
    assert "schemas: https://hub.example/hub/schemas/CCTV.xsd" in text and "asks for a login" not in text
    for secret in (USER, PASS, TOKEN):
        assert secret not in text
    saved = sorted(p.name for p in (tmp_path / "raw").iterdir())
    assert "signs.json" in saved and "events.json" in saved and "cctv.xml" in saved and "signs.headers.json" in saved
    for p in (tmp_path / "raw").iterdir():
        assert PASS.encode() not in p.read_bytes() and TOKEN.encode() not in p.read_bytes()
    # without credentials: the signs only, and it says why
    out.clear()
    f2 = Fetch()
    monkeypatch.delenv("SIZEUP_511NY_USER", raising=False)
    assert probe.main([], fetch=f2, out=out.append) == 0 and f2.urls() == [SIGNS]
    assert "not found (the events and CARS-Hub are skipped)" in "\n".join(out)
    # refused: exit 1, and the hint
    out.clear()
    monkeypatch.setenv("SIZEUP_511NY_USER", USER)
    monkeypatch.setenv("SIZEUP_511NY_PASS", "nope-not-it")
    assert probe.main(["--no-known", "--hub-base", "https://hub.example/hub/"], fetch=Fetch(), out=out.append) == 1
    assert "HTTP 401" in "\n".join(out) and "HTTP Basic" in "\n".join(out) and "nope-not-it" not in "\n".join(out)
