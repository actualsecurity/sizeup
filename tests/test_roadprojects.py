"""Road projects & closures (sizeup/roadprojects.py, #185): every source normalised to one record, the Today / This
week / All windows, the incident view's line, the villages' notices placed only by their own words, the Thruway's
roadwork folded in (the old Roadwork layer is gone: no double drawing), 511NY and WZDx off until a key.

Never a source itself (conftest.py turns the default addresses off and refuses any host but this one). Fixtures in
tests/fixtures/roadwork/, recorded 2026-10-15 unless named *_constructed:
  nysdot_capital.geojson      gis.dot.ny.gov CapitalProgramProjects layer 4 (In Construction), the hub's query, three
                              projects kept, coordinates to 5 places
  ctdot_projects.geojson      CTDOT_Project_Work_Areas, the hub's query (only the fields it asks for: no staff names or
                              e-mails), eight projects kept
  thruway_capitalprogram.xml  webdataapi.thruway.ny.gov capitalprogram/xml, trimmed to our area
  pc_alerts.xml, pc_news.xml  Port Chester's CivicPlus Alert Center and News Flash RSS
  gw_dpw.xml                  Greenwich's CivicPlus Public Works news RSS
  rb_news.xml                 Rye Brook's WordPress RSS, five items kept, contact addresses taken out
  *_constructed               511NY's events (its key is pending, and 511NY changed platforms on 2026-09-30: re-record
                              from the approved feed), a WZDx feed (none covers our area), a village notice that names a
                              block: in the sources' documented shapes, not recordings.
The Thruway's events are tests/fixtures/thruway/events.xml (test_roads.py)."""

import json
import re
from pathlib import Path

import pytest

import sizeup.roadprojects as rw
from sizeup.config import ConfigError
from sizeup.roads import Roads
from sizeup.roadprojects import (Feed, RoadProjects, note_line, parse_ctdot_projects, parse_nysdot_capital, parse_rss,
                                 parse_thruway_capital, parse_wzdx, road_related, state, windows)

FIX = Path(__file__).parent / "fixtures" / "roadwork"
TFIX = Path(__file__).parent / "fixtures" / "thruway"
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NOW = 1792087200.0                   # Thu 2026-10-15 14:00 EDT
KEY = "SECRETkey-0123456789abcdef"


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


@pytest.fixture(scope="module")
def gaz():
    from sizeup.geo.gazetteer import Gazetteer
    return Gazetteer()


class Fetch:
    """The fetch RoadProjects and Roads are given: bodies by URL, every call recorded with its conditional headers."""

    def __init__(self, bodies: dict):
        self.bodies = bodies
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, timeout, limit, headers=None):
        self.calls.append((url, dict(headers or {})))
        for k, v in self.bodies.items():
            if url.startswith(k):
                if v == 304:
                    return 304, {}, b""
                if isinstance(v, int):
                    from urllib.error import HTTPError
                    raise HTTPError(url, v, f"HTTP {v}", None, None)
                return 200, {"etag": '"e1"', "last-modified": "Wed, 14 Oct 2026 10:00:00 GMT"}, v
        raise OSError(f"no fixture for {url}")


def roads_with(fetch, key="", thruway=True) -> Roads:
    r = Roads(key, {"events": "https://feeds.example/events?key={key}"} if key else {}, county=False, thruway=thruway,
              thruway_urls={"events": "https://thruway.example/events"}, fetch=fetch, log=lambda m: None, clock=Clock())
    r.refresh("events")
    return r


# ---- the sources, each to the one record -----------------------------------------------------------------------------


def test_nysdot_projects_in_construction_one_record_per_project_with_its_completion_date():
    items = parse_nysdot_capital(json.loads(fx("nysdot_capital.geojson")))
    by = {r["id"]: r for r in items}
    assert set(by) == {"nysdot:813132", "nysdot:847316", "nysdot:881495"}
    us1 = by["nysdot:847316"]
    assert us1["geometry"]["type"] == "MultiPolygon" and len(us1["geometry"]["coordinates"]) == 6    # its six pieces, one record
    assert us1["kind"] == "project" and us1["source"] == "NYSDOT" and us1["closure"] == ""           # no closure claimed
    assert us1["end_day"] == "2027-10-26" and us1["start"] is None                                    # the date as given (a day)
    assert us1["title"].startswith("This project will correct existing pavement deficiencies on US Route 1")
    assert us1["link"] == "https://piyn.dot.ny.gov/PublicPIYN/ProjectDetails?pin=847316"
    assert windows(us1, NOW) == [[None, us1["end"]]] and state(windows(us1, NOW), NOW) == "active"
    assert windows(by["nysdot:881495"], NOW) == []             # its completion date (2026-09-07) has passed: not shown
    line = note_line(us1, [None, us1["end"]], NOW)
    assert line.startswith("Construction project: This project will correct existing pavement deficiencies")
    assert line.endswith("(NYSDOT: in construction, completion date Oct 26, 2027; no closure times given)")


def test_ctdot_work_areas_in_greenwich_road_work_only():
    items = parse_ctdot_projects(json.loads(fx("ctdot_projects.geojson")))
    ids = {r["id"] for r in items}
    # the rail power program (New Haven Line), the district-wide programs (20 towns, no town) and Stamford's: left out
    assert ids == {"ctdot:0056-0316", "ctdot:0056-0321", "ctdot:0056-0323"}
    pipe = next(r for r in items if r["id"] == "ctdot:0056-0321")
    assert pipe["title"] == "Pipe Replacement - Route 1" and pipe["end_day"] == "2026-07-11" and pipe["end_estimated"]
    assert windows(pipe, NOW) == []                             # its estimated completion has passed: not shown
    sig = next(r for r in items if r["id"] == "ctdot:0056-0323")
    assert sig["end_day"] == "2027-07-04" and state(windows(sig, NOW), NOW) == "active"
    assert "US-1" in sig["road"]
    blob = json.dumps(items)
    assert "@" not in blob and "ProjectManager" not in blob


def test_thruway_capital_program_active_projects_with_the_year_it_gives():
    items = parse_thruway_capital(fx("thruway_capitalprogram.xml"))
    assert [r["id"] for r in items] == ["thruwaycap:H371.1"]           # the completed ones are not shown
    r = items[0]
    assert r["end"] is None and r["no_end"] and r["end_text"] == "estimated completion 2027" and r["road"] == "I-95"
    assert r["geometry"]["type"] == "Point" and r["kind"] == "project"


def test_wzdx_lane_and_full_closures_with_their_dates():
    items = parse_wzdx(json.loads(fx("wzdx_constructed.geojson")), "511NY")
    by = {r["id"]: r for r in items}
    assert set(by) == {"wzdx:wz-1", "wzdx:wz-2"}                          # I-90 is far from here
    w = by["wzdx:wz-1"]
    assert (w["closure"], w["road"], w["cross_from"], w["cross_to"]) == ("full", "Westchester Ave", "N Pearl St", "King St")
    assert w["lanes"] == "2 of 2 lanes closed" and w["verified"] == {"start": True, "end": False}
    assert w["start"] == 1792069200 and w["end"] == 1792090800            # 9 AM to 3 PM today
    assert by["wzdx:wz-2"]["closure"] == "lanes"


def test_the_thruway_roadwork_comes_through_roads_not_a_second_fetch():
    f = Fetch({"https://thruway.example/events": (TFIX / "events.xml").read_bytes()})
    roads = roads_with(f)
    p = RoadProjects([], roads=roads, clock=Clock(), log=lambda m: None)
    items = p.items()
    assert [r["id"] for r in items] == ["thruway:CAD-262740341"]           # roadwork in our area; the crash stays an incident
    r = items[0]
    assert (r["road"], r["dir"], r["cross_from"], r["cross_to"]) == ("I-287", "Westbound", "exit 8", "exit 8 - I-87")
    assert r["end"] is None and r["no_end"] and r["end_text"] == "Until further notice" and r["start"] is None
    assert note_line(r, windows(r, NOW)[0], NOW) == \
        "Roadwork: I-287 between exit 8 and exit 8 - I-87 no end time given (Until further notice) (Thruway)"
    assert len(f.calls) == 1                                               # roads read it once; this module read nothing
    src = {s["name"]: s for s in p.sources()}
    assert src["thruway"]["on"] and src["thruway"]["n"] == 1
    assert not src["511ny"]["on"] and "511NY's key" in src["511ny"]["why"]


def test_511ny_roadwork_lights_up_with_the_key_hours_detour_and_its_line():
    body = fx("511ny_roadwork_constructed.json")
    f = Fetch({"https://feeds.example/events": body, "https://thruway.example/events": (TFIX / "events.xml").read_bytes()})
    roads = roads_with(f, key=KEY)
    p = RoadProjects([], roads=roads, clock=Clock(), log=lambda m: None)
    by = {r["id"]: r for r in p.items()}
    r = by["511ny:NYSDOT-2001"]
    assert r["hours"] == [{"days": [0, 1, 2, 3, 4], "from": "21:00", "to": "05:00"}]
    assert r["hours_text"] == "Weeknights 9:00 PM - 5:00 AM" and r["detour"] == "Northbound traffic use S Regent St to Willett Ave"
    assert r["geometry"]["type"] == "LineString" and r["geometry"]["coordinates"][0] == [-73.6642, 40.9996]
    assert r["closure"] == "lanes" and r["lanes"] == "1 Right lane closed" and r["start"] == 1791777600
    w = windows(r, NOW)
    assert w[0] == [1792112400.0, 1792141200.0]                             # tonight 9 PM to 5 AM (Thursday)
    assert all(w_[0] < w_[1] for w_ in w) and state(w, NOW) == "upcoming"
    assert state(w, 1792112400 + 60) == "active"
    assert "thruway:CAD-262740341" in by and "511ny:NYSDOT-2002" not in by      # Albany: not ours
    assert KEY not in json.dumps(p.public())


# ---- time -------------------------------------------------------------------------------------------------------------


def test_windows_daily_hours_over_midnight_weekdays_only_and_the_ends():
    r = {"start": 1791777600, "end": 1793354400, "hours": [{"days": [0, 1, 2, 3, 4], "from": "21:00", "to": "05:00"}]}
    w = windows(r, NOW)
    starts = [rw.datetime.fromtimestamp(s, rw.TZ).strftime("%a %H:%M") for s, _ in w]
    assert starts[:3] == ["Thu 21:00", "Fri 21:00", "Mon 21:00"]                # no weekend nights
    assert windows(r, 1793354400 + 1) == []                                     # ended
    assert windows({"start": None, "end": None}, NOW) == [[None, None]]
    assert state([[None, None]], NOW) == "active" and state([[NOW + 60, None]], NOW) == "upcoming" and state([], NOW) == "ended"


def test_the_line_says_what_where_until_and_whose_in_the_console_timezone():
    rec = {"closure": "full", "road": "Westchester Ave", "cross_from": "N Pearl St", "cross_to": "King St", "source": "511NY",
           "kind": "roadwork"}
    assert note_line(rec, [NOW - 3600, NOW + 3600], NOW) == "Road closed: Westchester Ave between N Pearl St and King St until 3:00 PM (511NY)"
    assert note_line(rec, [NOW + 7 * 3600, NOW + 15 * 3600], NOW) == \
        "Road closed: Westchester Ave between N Pearl St and King St from 9:00 PM until Oct 16 5:00 AM (511NY)"
    assert note_line(rec | {"closure": "lanes"}, [None, None], NOW) == \
        "Lane closure: Westchester Ave between N Pearl St and King St no end time given (511NY)"
    assert note_line(rec, [NOW - 7200, NOW - 3600], NOW).endswith("ended 1:00 PM (511NY)")


# ---- the villages' notices: placed only by their words -----------------------------------------------------------------


def test_recorded_notices_are_filtered_to_the_roads_and_listed_unplaced_with_the_flyer(gaz):
    f = Feed("pc_alerts", "Port Chester", "Village of Port Chester: Alert Center", "https://www.portchesterny.gov/AlertCenter.aspx",
             "", "https://pc.example/alerts", "rss", 1200, towns=("Port Chester",))
    p = RoadProjects([f], gaz=gaz, clock=Clock(), log=lambda m: None)
    placed, unplaced = p.notices_from(f, parse_rss(fx("pc_alerts.xml")) + parse_rss(fx("pc_news.xml")))
    assert placed == []
    assert [n["title"] for n in unplaced] == ["Various Utility Construction", "Festa Colón 2026 Has Been Rescheduled for THIS SUNDAY!"]
    util = unplaced[0]
    assert util["place_how"] == "details in the village's flyer (linked; pictures aren't read)"
    assert util["flyer"] == "https://www.portchesterny.gov/ImageRepository/Document?documentID=2891"
    assert util["link"] == "https://www.portchesterny.gov/AlertCenter.aspx?AID=42" and util["geometry"] is None
    assert util["start"] is None and util["end"] is None                          # the dates are in its words: not guessed
    rb = parse_rss(fx("rb_news.xml"))
    assert [r["title"] for r in rb if road_related(r)] == ["Ridge Street School Pick Up Route Suggestions", "Paving Update – July 31",
                                                           "Paving Notice Update"]
    fb = Feed("rb_news", "Rye Brook", "Village of Rye Brook: news", "https://ryebrookny.gov/", "", "https://rb.example/feed", "rss",
              1200, towns=("Rye Brook",))
    _, rb_unplaced = p.notices_from(fb, rb)
    assert [n["title"] for n in rb_unplaced] == ["Ridge Street School Pick Up Route Suggestions"]   # July's paving: old news
    assert [r["title"] for r in parse_rss(fx("gw_dpw.xml")) if road_related(r)] == [
        "Drainage Improvement Project – Valleywood Rd / Old Post Rd #6 / East Putnam Ave"]


def test_a_notice_naming_a_block_is_drawn_along_it_and_an_address_at_it(gaz):
    f = Feed("pc_alerts", "Port Chester", "Village of Port Chester: Alert Center", "https://www.portchesterny.gov/AlertCenter.aspx",
             "", "https://pc.example/alerts", "rss", 1200, towns=("Port Chester",))
    p = RoadProjects([f], gaz=gaz, clock=Clock(), log=lambda m: None)
    placed, unplaced = p.notices_from(f, parse_rss(fx("notice_constructed.xml")))
    by = {n["title"]: n for n in placed}
    block = by["Westchester Avenue Closure for Festa Colon"]
    assert block["geometry"]["type"] == "LineString" and len(block["geometry"]["coordinates"]) >= 3
    assert (block["road"], block["cross_from"], block["cross_to"]) == ("Westchester Ave", "N Pearl St", "King St")
    assert block["place_how"] == "the notice names Westchester Ave between N Pearl St and King St"
    water = by["Water Main Work"]
    assert water["geometry"]["type"] == "Point" and water["place_how"] == "the notice names 222 Grace Church St, Port Chester"
    # "Main Street" is not a Port Chester street (N Main St, S Main St are): not placed, never the nearest guess
    assert [n["title"] for n in unplaced] == ["Main Street paving"]
    assert unplaced[0]["place_how"] == "the notice names no street we can place"


# ---- one provider: merged, served, near an incident -----------------------------------------------------------------------


def provider(gaz, clock=None, **bodies):
    f = Fetch({"https://feeds.example/events": fx("511ny_roadwork_constructed.json"),
               "https://thruway.example/events": (TFIX / "events.xml").read_bytes(),
               "https://wzdx.example/": fx("wzdx_constructed.geojson"), "https://pc.example/alerts": fx("notice_constructed.xml"),
               "https://nysdot.example/": fx("nysdot_capital.geojson"), **bodies})
    roads = roads_with(f, key=KEY)
    feeds = [Feed("wzdx", "511NY", "511NY work zones (WZDx)", "https://511ny.org/", "", "https://wzdx.example/feed?key={key}", "wzdx",
                  300, key=KEY),
             Feed("pc_alerts", "Port Chester", "Village of Port Chester: Alert Center", "https://www.portchesterny.gov/AlertCenter.aspx",
                  "", "https://pc.example/alerts", "rss", 1200, towns=("Port Chester",)),
             Feed("nysdot", "NYSDOT", "NYSDOT: capital projects in construction", "https://gis.dot.ny.gov/", "",
                  "https://nysdot.example/query", "nysdot_capital", 86400)]
    p = RoadProjects(feeds, roads=roads, gaz=gaz, clock=clock or Clock(), fetch=f, log=lambda m: None)
    return p, f


def test_merged_one_per_work_zone_and_the_page_gets_windows(gaz):
    p, f = provider(gaz)
    p.ask()
    assert p.tick() == 3
    d = p.public()
    ids = [i["id"] for i in d["items"]]
    assert "511ny:NYSDOT-2001" in ids and "wzdx:wz-2" not in ids        # the same N Main St paving from WZDx: folded into 511NY's
    kept = next(i for i in d["items"] if i["id"] == "511ny:NYSDOT-2001")
    assert kept["also"] == ["511NY (wzdx)"]
    assert "wzdx:wz-1" in ids and "nysdot:847316" in ids and "thruway:CAD-262740341" in ids
    notice = next(i for i in d["items"] if i["kind"] == "notice" and i["road"] == "Westchester Ave")
    assert notice["id"].startswith("pc_alerts:")                                 # a notice is never folded into another
    assert next(i for i in d["items"] if i["id"] == "wzdx:wz-1")["state"] == "active"
    assert [n["title"] for n in d["notices"]] == ["Main Street paving"]
    assert KEY not in json.dumps(d) and "wzdx.example" not in json.dumps(d)      # never the key or a feed address


def test_an_incident_on_the_block_gets_the_line_and_one_far_away_does_not(gaz):
    p, _ = provider(gaz)
    p.ask()
    p.tick()
    near = p.near(41.0010, -73.6651, "125 Westchester Ave, Port Chester")
    texts = [x["text"] for x in near["items"]]
    assert texts[0] == "Road closed: Westchester Ave between N Pearl St and King St until 3:00 PM (511NY)"
    assert near["items"][0]["active_now"] and near["items"][0]["how"] == "at the location"
    assert any(t.startswith('Notice: Westchester Ave between N Pearl St and King St (Port Chester, posted Oct 14 10:00 AM): '
                            '"Westchester Avenue Closure for Festa Colon"') for t in texts)
    assert texts.index(next(t for t in texts if t.startswith("Construction project:"))) > 0      # in effect, after the closures
    # on the incident's street, farther than NEAR_M but within STREET_M: "on <road>"
    far_on = p.near(41.0063, -73.6721, "300 Westchester Ave")
    wz = [x for x in far_on["items"] if x["id"] == "wzdx:wz-1"]
    assert wz and wz[0]["how"].startswith("on Westchester Ave, ")
    assert all(x["id"] != "wzdx:wz-1" for x in p.near(41.0063, -73.6721, "12 Elm St")["items"])      # not its street: too far
    # a closed incident from yesterday morning: the closure (today 9 to 3) wasn't in effect then
    assert all(x["id"] != "wzdx:wz-1" for x in p.near(41.0010, -73.6651, "", 1791986400, 1791990000)["items"])
    # US-1 project footprint: an incident on it gets the project's line
    us1 = p.near(41.0021, -73.6634, "N Main St")
    assert any(x["id"] == "nysdot:847316" and x["kind"] == "project" for x in us1["items"])


def test_http_etag_conditional_reads_demand_and_the_key_never_shown(gaz):
    p, f = provider(gaz)
    assert p.tick() == 0 and all("feeds.example" in u or "thruway.example" in u for u, _ in f.calls)   # nobody asked: nothing read
    code, body, ctype, h = p.get("/api/cad/roadprojects", "")
    assert code == 200 and ctype == "application/json" and h["ETag"] and h["Cache-Control"] == "no-cache"
    assert p.tick() == 3
    code2, body2, _, h2 = p.get("/api/cad/roadprojects", "")
    assert h2["ETag"] != h["ETag"]
    assert p.get("/api/cad/roadprojects", "")[3]["ETag"] == h2["ETag"]          # the same items: the same tag (a 304)
    # the next read asks conditionally; a 304 keeps what we have
    for feed in p.feeds:
        feed.status["next"] = 0
    f.bodies = {k: 304 for k in f.bodies}
    assert p.tick() == 3
    assert all(c[1].get("If-None-Match") == '"e1"' and c[1].get("If-Modified-Since") for c in f.calls[-3:])
    assert len(p.public()["items"]) == len(json.loads(body2)["items"])
    for q in ("lat=x&lon=1", "lat=91&lon=0", "lat=41&lon=-73&from=yesterday"):
        assert p.get("/api/cad/roadprojects/near", q)[0] == 400
    code, body, _, _ = p.get("/api/cad/roadprojects/near", "lat=41.0010&lon=-73.6651&address=125+Westchester+Ave")
    assert code == 200 and json.loads(body)["items"]
    assert p.get("/api/cad/roadprojects/other", "")[0] == 404


def test_a_refused_key_is_never_logged_or_served(gaz):
    logs = []
    f = Fetch({"https://wzdx.example/": 401})
    feed = Feed("wzdx", "511NY", "511NY work zones (WZDx)", "https://511ny.org/", "", "https://wzdx.example/feed?key={key}", "wzdx",
                300, key=KEY)
    p = RoadProjects([feed], clock=Clock(), fetch=f, log=logs.append)
    p.ask()
    assert p.tick() == 0
    assert "refused" in feed.status["error"] and feed.status["next"] == NOW + rw.REFUSED_S
    assert KEY not in json.dumps(p.public()) and not any(KEY in m for m in logs)
    off = Feed("wzdx", "511NY", "511NY work zones (WZDx)", "https://511ny.org/", "", "https://wzdx.example/feed?key={key}", "wzdx", 300,
               why_off="waiting for 511NY's key and WZDx address ([roadwork] wzdx_url, SIZEUP_511NY_KEY)")
    assert not off.on and off.public()["why"].startswith("waiting for 511NY's key")


def test_the_cache_keeps_what_was_read_without_keys_or_addresses(gaz, tmp_path):
    p, f = provider(gaz)
    p.cache = tmp_path / "roadprojects.json"
    p.ask()
    p.tick()
    text = p.cache.read_text()
    assert KEY not in text and "example" not in text.replace("pc.example", "")
    q = RoadProjects([Feed(x.name, x.short, x.label, x.link, "", x.url, x.kind, x.every_s, key=x.key, towns=x.towns) for x in p.feeds],
                     cache=p.cache, clock=Clock(), fetch=f, log=lambda m: None)
    assert {i["id"] for i in q.items()} >= {"wzdx:wz-1", "nysdot:847316"}
    assert all(x.status["etag"] == '"e1"' for x in q.feeds)


# ---- config, wiring, the page -------------------------------------------------------------------------------------------


def test_config_roadwork_section(tmp_path):
    from tests.test_gis import load_cfg
    c = load_cfg(tmp_path, "")
    assert (c.roadwork_enabled, c.roadwork_projects, c.roadwork_notices, c.roadwork_wzdx_url) == (True, True, True, "")
    c = load_cfg(tmp_path, '[roadwork]\nnotices = false\nwzdx_url = "https://511ny.example/api/wzdx?key={key}"\n')
    assert not c.roadwork_notices and c.roadwork_wzdx_url.endswith("{key}")
    for bad in ('[roadwork]\nwzdx_url = "http://511ny.example/wzdx"\n', '[roadwork]\nwzdx_url = "https://x.example/wzdx?key=abc12345"\n',
                '[roadwork]\nprojects = "yes"\n', '[roadwork]\nother = 1\n'):
        with pytest.raises(ConfigError, match="roadwork"):
            load_cfg(tmp_path, bad)
    feeds = {f.name: f for f in rw.feeds_from_config(c)}
    assert set(feeds) == {"wzdx"}                      # conftest turned the public addresses off; the wzdx one is set
    assert feeds["wzdx"].url.endswith("{key}") and not feeds["wzdx"].on            # no key: off


def test_the_default_sources(monkeypatch):
    monkeypatch.undo()                                 # the module's own addresses (conftest turns them off for the rest)

    class Cfg:
        roads_bbox = (-73.80, 40.92, -73.56, 41.10)
        roads_key = ""
    feeds = {f.name: f for f in rw.feeds_from_config(Cfg())}
    assert set(feeds) == {"thruway_capital", "nysdot", "ctdot", "pc_alerts", "pc_news", "rb_news", "gw_dpw", "gw_alerts", "wzdx"}
    assert feeds["nysdot"].url.startswith("https://gis.dot.ny.gov/hostingny/rest/services/Projects/CapitalProgramProjects/FeatureServer/4/query")
    assert "geometry=-73.8,40.92,-73.56,41.1" in feeds["nysdot"].url and "f=geojson" in feeds["nysdot"].url
    assert "Email" not in feeds["ctdot"].url and "ProjectManager" not in feeds["ctdot"].url
    assert all(f.url.startswith("https://") for f in feeds.values() if f.url)
    assert feeds["pc_alerts"].every_s == 1200 and feeds["nysdot"].every_s == 86400
    assert not feeds["wzdx"].on


def test_the_hub_serves_the_layer(tmp_path):
    from tests.test_cloud import HubRun, hub_cfg
    h = HubRun(hub_cfg(tmp_path))
    try:
        d = h.get("/api/cad/roadprojects")
        assert d["configured"] and d["tz"] == "America/New_York" and isinstance(d["items"], list)
        names = {s["name"]: s for s in d["sources"]}
        assert not names["511ny"]["on"] and not names["wzdx"]["on"]
        n = h.get("/api/cad/roadprojects/near?lat=41.0010&lon=-73.6651&address=125+Westchester+Ave")
        assert n["items"] == []
    finally:
        h.close()


def test_the_page_draws_the_group_the_filter_and_the_incident_line():
    js = (STATIC / "roadprojects.js").read_text()
    html = (STATIC / "cad.html").read_text()
    assert 'id="d-roads"' in html and html.index('src="/static/maplayers.js"') < html.index('src="/static/roadprojects.js"') \
        < html.index('src="/static/cad.js"')
    assert 'const KEY = "roadwork";' in js and "MapLayers.addGroup({ attach: (control) => new RoadMap(control) })" in js
    assert 'const FILTERS = [["today", "Today"], ["week", "This week"], ["all", "All"]];' in js
    assert '"/api/cad/roadprojects"' in js and '"/api/cad/roadprojects/near"' in js
    assert "cad.hooks.push(renderIncident)" in js and 'aria-pressed' in js
    assert 'token("--err"' in js and 'token("--warn"' in js and 'token("--info"' in js       # the design's colours
    assert 'filter: ["all", line, ["!", now]]' in js and '"line-dasharray": [1.2, 1.2] } }, before);     // upcoming: dashed' in js
    assert "no end time given" in js and "start not given" in js
    lines = js.splitlines()
    uses = [ln for ln in lines if "localStorage." in ln]
    assert len(uses) == 2 and all("try {" in ln for ln in uses)
    assert not re.search(r"https?://(?!cdnjs)", js.replace("http://www.w3.org", ""))          # the browser asks only the hub
    ml = (STATIC / "maplayers.js").read_text()
    assert '{ key: "roadwork"' not in ml and '"ml-work"' not in ml                            # no second roadwork layer
    assert 'e.layer === key' in ml and 'if (key === "incidents") {' in ml
