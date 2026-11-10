"""MTA TrainTime (sizeup/traintime.py, #362): the undocumented backend behind radar.mta.info, merged into the
GTFS-realtime trains (sizeup/trains.py) by train number and run date: parse and validate, the merge, the fallback
to GTFS-realtime after bad polls or a change of shape, the back-off, the route lines and a station's departures.

Never the MTA itself (conftest.py turns TrainTime's address off and refuses any host but this one).
tests/fixtures/mta/traintime-locations.json is SYNTHETIC: made-up train and car numbers with the shape of
/locations?geometry=TRACK_TURF (field names, types and enums as the API gave them on 2026-11-09), placed on the
recorded GTFS line (tests/fixtures/mta/gtfsmnr.zip) between Rye and Port Chester."""

import copy
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sizeup import datasources
from sizeup import traintime as TT
from sizeup import trains as TRAINS
from sizeup.config import ConfigError, load
from sizeup.trains import DEMAND_S, Model, Trains, parse_feed, parse_static
from tests.test_trains import GTFS, NOW, Clock, msg

FIX = Path(__file__).parent / "fixtures" / "mta"
LOC = (FIX / "traintime-locations.json").read_bytes()
BBOX = (-73.845, 40.900, -73.530, 41.060)
RT_URL, GTFS_URL, TT_URL = "https://mta.example/rt", "https://mta.example/gtfs.zip", "https://tt.example"
# the recorded static feed's stop ids: Harrison, Rye, Port Chester, Greenwich
HS, RY, PC, GN = "112", "114", "115", "116"


def stu(sid, t, status="", track=""):
    return msg((4, sid), (2, msg((2, t))), (3, msg((2, t))), (1005, msg((1, track), (2, status))))


def gtfs_rt(start_date="20261014", ts=NOW) -> bytes:
    """A built GTFS-realtime feed: 9901 westbound (left Port Chester, Rye next), with a GTFS position back at Port
    Chester; 9902 eastbound (left Rye, Port Chester next); 9908, a train TrainTime doesn't have."""
    def tu(num, *stops):
        return msg((1, msg((1, f"T{num}"), (3, start_date), (5, "3"))), (3, msg((1, num), (2, num))), *((2, s) for s in stops))
    vp = msg((1, msg((1, "T9901"))), (2, msg((1, 41.000732), (2, -73.6647))), (5, ts), (8, msg((1, "9901"), (2, "9901"))))
    ents = [msg((1, "a"), (3, tu("9901", stu(GN, NOW - 240, "Departed"), stu(PC, NOW - 60, "Departed"), stu(RY, NOW + 120, "On-Time", "3"),
                                 stu(HS, NOW + 300, "On-Time")))),
            msg((1, "b"), (4, vp)),
            msg((1, "c"), (3, tu("9902", stu(RY, NOW - 30, "Departed"), stu(PC, NOW + 90, "On-Time", "2"), stu(GN, NOW + 400)))),
            msg((1, "d"), (3, tu("9908", stu(HS, NOW - 30, "Departed"), stu(RY, NOW + 150), stu(PC, NOW + 300))))]
    return msg((1, msg((1, "2.0"), (3, ts))), *((2, e) for e in ents))


class Fetch:
    """Both fetch signatures: trains.py's (url, timeout, limit, headers) -> (status, headers, body) and
    traintime.py's (url, timeout, limit) -> (status, body)."""

    def __init__(self, rt=None):
        self.rt = rt or gtfs_rt()
        self.loc = LOC
        self.geometry = None
        self.countdown = LOC
        self.calls: list[str] = []
        self.fail: dict[str, object] = {}

    def __call__(self, url, timeout, limit, headers=None):
        self.calls.append(url)
        for k, v in self.fail.items():
            if k in url:
                if isinstance(v, int):
                    raise HTTPError(url, v, "nope", None, None)
                if isinstance(v, Exception):
                    raise v
                return 200, v
        if url == RT_URL:
            return 200, {}, self.rt
        if url == GTFS_URL:
            return 200, {}, GTFS
        if TT.GEOMETRY in url:
            return 200, self.geometry
        if "/countdown/" in url:
            return 200, self.countdown
        if TT.LOCATIONS in url:
            return 200, self.loc
        raise AssertionError(url)

    def n(self, part):
        return sum(1 for u in self.calls if part in u)


def provider(tmp_path, **kw):
    clock, fetch = Clock(NOW + 5), Fetch()
    tr = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, cache_dir=tmp_path / "trains", fetch=fetch, tt_fetch=fetch, tt_url=TT_URL,
                log=lambda m: None, clock=clock, **kw)
    return tr, clock, fetch


def looking(tr, clock):
    tr.asked = clock.t                                  # a viewer has the layer on


@pytest.fixture(scope="module")
def model():
    return Model(parse_static(GTFS))


def recs():
    return TT.parse_locations(LOC)["records"]


def kept():
    return [r for r in recs() if TT.ours(r, BBOX)]                              # what the poller keeps


def by_num(rs):
    return {r["train"]: r for r in rs}


# ---- parse and validate ----------------------------------------------------------------------------------------------


def test_the_known_shape_is_read_and_a_bad_record_dropped():
    d = TT.parse_locations(LOC)
    assert d["total"] == 7 and d["dropped"] == 1                                  # 9907: no location
    r = by_num(d["records"])
    assert set(r) == {"9901", "9902", "9903", "9904", "1905", "9906"}
    a = r["9901"]
    assert (a["dir"], a["branch_id"], a["run_date"], a["loc_src"]) == ("W", "NH", "2026-10-14", "GPS")
    assert a["speed_mph"] == 52.3 and a["heading"] == 225.5 and a["loc_at"] == NOW - 10
    assert a["late_s"] == 660                                                      # otp -660: 11 min late
    assert a["fleet"] == "M8" and a["cars_actual"] == a["cars_sched"] == 6 and a["occupancy"] == "FEW_SEATS"
    assert [c["load"] for c in a["cars"]] == ["EMPTY", "MANY_SEATS", "FEW_SEATS", "SRO", "NO_DATA", "MANY_SEATS"]
    assert TT.passengers(a) == 4 + 21 + 48 + 95 + 17                               # the car with no count left out
    assert TT.milepost(a) == 23.0                                                  # westbound to Grand Central: 33.0 - 10.0
    assert TT.next_stop(a)["code"] == "2RY" and TT.next_stop(a)["track"] == "3" and TT.next_stop(a)["posted"]
    assert TT.route_key(a) == "2SM>0NY"
    b = r["9902"]
    assert b["heading"] is None                                                    # 0.0: "not given"
    assert TT.milepost(b) == 24.7 and b["cars_actual"] == 8 and b["cars_sched"] == 10 and b["late_s"] == -30
    assert TT.milepost(r["9906"]) == 26.2                                          # westbound to Grand Central: 72.2 - 46.0
    assert TT.milepost({**b, "stops": b["stops"][1:]}) is None                     # not from Grand Central: turf isn't a milepost
    assert r["9906"]["cars"][0]["loco"] and r["9906"]["cars"][0]["pax"] is None


def test_only_the_new_haven_line_and_trains_in_our_area_are_kept():
    keep = {r["train"] for r in recs() if TT.ours(r, BBOX)}
    assert keep == {"9901", "9902", "9903", "9906"}                               # NC in the area kept; Hudson, LIRR not


@pytest.mark.parametrize("body", [b'{"error": "gone"}', b"<html>maintenance</html>", b"[1, 2, 3]",
                                  json.dumps([{**x, "location": {"lat": 41, "lon": -73.6}} for x in json.loads(LOC)]).encode(),
                                  json.dumps([{**x, "train_num": 9901} for x in json.loads(LOC)]).encode()])
def test_a_change_of_shape_is_a_schema_error(body):
    with pytest.raises(TT.SchemaError):
        TT.parse_locations(body)


def test_odd_values_in_a_record_are_dropped_or_ignored_not_trusted():
    x = json.loads(LOC)[0]
    bad = copy.deepcopy(x)
    bad["location"]["latitude"] = "41.0"                                          # a string: the record goes
    assert TT.record(bad) is None
    odd = copy.deepcopy(x)
    odd["consist"]["cars"] = ["x", {"loading": "<b>", "number": True}]            # junk cars: no load trusted
    odd["status"] = "late"
    odd["details"]["stops"] = [{"code": "../x"}, {"code": "2RY", "sign_track": "<script>"}]
    r = TT.record(odd)
    assert r is not None and r["late_s"] is None and r["cars"][0]["load"] == "NO_DATA" and r["cars"][0]["n"] is None
    assert [s["code"] for s in r["stops"]] == ["2RY"] and r["stops"][0]["track"] == ""


def test_the_branch_alerts_are_metro_norths_new_haven_ones_once():
    al = TT.branch_alerts(recs())
    assert len(al) == 1 and al[0]["status"] == "Some Delays" and "synthetic" in al[0]["header"]


def test_a_polyline_decodes_and_garbage_raises():
    pts = [(-73.69, 40.98), (-73.68, 40.99), (-73.66, 41.0)]
    assert [(round(a, 6), round(b, 6)) for a, b in TT.decode_polyline(encode(pts))] == pts
    with pytest.raises(ValueError):
        TT.decode_polyline("~")


def encode(pts, precision=6):
    """Google's encoded polyline ([(lon, lat)]): what geometry=TRACK gives as details.polyline."""
    out, plat, plon, f = [], 0, 0, 10 ** precision
    for lon, lat in pts:
        for v, prev in ((round(lat * f), plat), (round(lon * f), plon)):
            d = v - prev
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        plat, plon = round(lat * f), round(lon * f)
    return "".join(out)


# ---- the merge ---------------------------------------------------------------------------------------------------------


def test_the_merge_matches_train_number_and_run_date(model):
    feed = parse_feed(gtfs_rt())
    m = TT.merge(feed, kept(), NOW, NOW, {"2RY": "Rye", "2PC": "Port Chester"}, {"9901": NOW - 60})   # GTFS's: a minute old
    a, b, c = m["trains"]["9901"], m["trains"]["9902"], m["trains"]["9908"]
    assert a["pos"]["src"] == "traintime" and a["pos"]["bearing"] == 225.5 and a["pos"]["ts"] == NOW - 10
    assert a["tt"]["next"] == {"code": "2RY", "name": "Rye", "track": "3", "posted": True, "track_change": False,
                               "sched": NOW - 420, "status": "EN_ROUTE"}
    assert a["tt"]["cars"] == 6 and a["tt"]["mp"] == 23.0 and a["tt"]["late_s"] == 660 and a["dir_hint"] == -1
    assert b["pos"]["src"] == "traintime" and b["pos"]["bearing"] is None          # SIGNAL: drawn; heading 0: none
    assert "tt" not in c and c is feed["trains"]["9908"]                         # not in TrainTime: as it was
    assert feed["trains"]["9901"]["pos"]["lat"] == pytest.approx(41.000732, abs=1e-5)   # the feed itself untouched
    added = {k for k in m["trains"] if k.startswith("tt:")}
    assert added == {"tt:9903:2026-10-14"}                                        # SCHED (9906) not drawn on its own
    other_day = TT.merge(parse_feed(gtfs_rt(start_date="20261013")), kept(), NOW)
    assert other_day["tt_misses"] == [("9901", "2026-10-13", ["2026-10-14"]), ("9902", "2026-10-13", ["2026-10-14"])]
    assert "tt" not in other_day["trains"]["9901"]                                # another run date: not the same train
    no_date = TT.merge(parse_feed(gtfs_rt(start_date="")), kept(), NOW)
    assert no_date["trains"]["9901"]["tt"]["cars"] == 6                           # no start date: by number


def test_an_old_or_scheduled_location_keeps_the_gtfs_position():
    feed = parse_feed(gtfs_rt())
    m = TT.merge(feed, kept(), NOW + TT.POS_MAX_AGE_S + 20)
    assert m["trains"]["9901"]["pos"] == feed["trains"]["9901"]["pos"] and m["trains"]["9901"]["tt"]["cars"] == 6


def test_the_map_gets_traintimes_position_and_facts(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    looking(tr, clock)
    tr.tick()
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == 1 and tr.tt.active()
    d = tr.public()
    t = {x["train"]: x for x in d["trains"]}
    a = t["9901"]
    assert a["pos_src"] == "traintime" and a["mode"] == "reported" and a["fix_at"] == NOW - 10 and not a["fix_age_unknown"]
    assert a["lat"] == pytest.approx(40.99512, abs=2e-3) and a["bearing"] == 226         # GPS, not the GTFS point at Port Chester
    assert a["cars"] == 6 and a["mp"] == 23.0 and a["tt"]["fleet"] == "M8" and a["tt"]["speed_mph"] == 52.3
    assert [c["load"] for c in a["tt"]["car_loads"]][:4] == ["EMPTY", "MANY_SEATS", "FEW_SEATS", "SRO"]
    assert a["tt"]["next"]["name"] == "Rye" and a["tt"]["route"] == "2SM>0NY"
    assert t["9902"]["mp"] == 24.7 and t["9902"]["cars"] == 8 and t["9902"]["tt"]["cars_sched"] == 10
    assert "tt" not in t["9908"] and "pos_src" not in t["9908"]                    # GTFS-realtime only
    assert t["9903"]["dir"] == "outbound" and t["9903"]["mp"] == 26.0              # a New Canaan train in our area
    assert len(d["alerts"]) == 1 and d["traintime"]["state"] == "ok"
    seen = len(tr.points)
    clock.t += 25
    fetch.loc = json.dumps([{**x, "location": {**x["location"], "latitude": x["location"]["latitude"] + 0.001,
                                               "timestamp": x["location"]["timestamp"] + 25}} if x.get("location") else x
                            for x in json.loads(LOC)]).encode()
    tr.tt.tick()
    assert len(tr.points) == seen                                                 # GPS fixes teach no reporting points


def test_fallback_to_gtfs_after_n_bad_polls_then_back(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    looking(tr, clock)
    tr.tick()
    tr.tt.tick()
    assert tr.public()["traintime"]["state"] == "ok"
    fetch.fail[TT.LOCATIONS] = 503
    for i in range(TT.FALLBACK_AFTER):
        clock.t = tr.tt.due_at()
        looking(tr, clock)
        tr.tt.tick()
        if i < TT.FALLBACK_AFTER - 1:
            assert tr.tt.st["bad"] == i + 1
    assert not tr.tt.active() and tr.tt.state()[0] == "fallback"
    tr.refresh_rt()
    d = tr.public()
    t = {x["train"]: x for x in d["trains"]}
    assert "tt" not in t["9901"] and "alerts" not in d                            # GTFS-realtime alone
    assert t["9901"]["lat"] == pytest.approx(41.000732, abs=2e-3)                 # its GTFS position, judged afresh
    assert d["traintime"]["state"] == "fallback" and "GTFS-realtime only" in d["traintime"]["why"]
    h = tr.feed_health()
    assert h["state"] == "fallback" and h["fallback"] == "GTFS-realtime" and "HTTP 503" in h["why"]
    reg = datasources.DataSources(log=lambda m: None, clock=clock, spawn=lambda fn, name: fn())
    for s in datasources.trains_sources(tr):
        reg.register(s)
    row = reg.row("trains:traintime")
    assert not row["ok"] and "GTFS-realtime only" in row["error"]
    del fetch.fail[TT.LOCATIONS]
    clock.t = tr.tt.due_at()
    looking(tr, clock)
    tr.tt.tick()
    assert tr.tt.active() and reg.row("trains:traintime")["ok"] and tr.feed_health()["state"] == "ok"
    assert "tt" in {x["train"]: x for x in tr.public()["trains"]}["9901"]


def test_a_change_of_shape_is_a_bad_poll_and_keeps_the_last_good_read_a_while(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    looking(tr, clock)
    tr.tick()
    tr.tt.tick()
    fetch.fail[TT.LOCATIONS] = b'{"trains": []}'                                  # a new shape
    clock.t = tr.tt.due_at()
    looking(tr, clock)
    tr.tt.tick()
    assert tr.tt.st["bad"] == 1 and "SchemaError" in tr.tt.st["error"]
    assert tr.tt.active()                                                          # one bad poll: the last good read still merged
    clock.t += TT.MAX_AGE_S
    assert not tr.tt.active() and tr.tt.state()[0] == "fallback"                  # too old now


def test_the_back_off(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    looking(tr, clock)
    fetch.fail[TT.LOCATIONS] = URLErrorLike()
    waits = []
    for _ in range(7):
        t0 = clock.t
        tr.tt.poll()
        waits.append(round(tr.tt.st["next"] - t0))
        clock.t = tr.tt.due_at()
    assert waits == [20, 40, 80, 160, 320, 600, 600]
    fetch.fail[TT.LOCATIONS] = 429
    t0 = clock.t
    tr.tt.poll()
    assert tr.tt.st["next"] - t0 == TT.RATE_LIMITED_S and "rate limit" in tr.tt.st["error"]
    fetch.fail[TT.LOCATIONS] = 403
    t0 = clock.t
    tr.tt.poll()
    assert tr.tt.st["next"] - t0 == TT.REFUSED_S
    n = fetch.n(TT.LOCATIONS)
    tr.tt.tick()                                                                    # resting: nothing fetched
    assert fetch.n(TT.LOCATIONS) == n


def URLErrorLike():
    from urllib.error import URLError
    return URLError("connection refused")


def view(tr, clock):
    """A page with the Trains layer on asks for it (every 5 s)."""
    tr.get("/api/cad/trains", "")


def test_fast_while_the_layer_is_on_idle_while_viewed_none_with_nobody_looking(tmp_path):
    tr, clock, fetch = provider(tmp_path, tt_every_s=1)
    assert tr.tt.every_s == 5.0                                                    # 5 to 60 s
    assert Trains(tt_every_s=600, log=lambda m: None).tt.every_s == 60.0
    tr.tick()
    tr.tt.tick()
    clock.t += 60
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == 0 and tr.tt.state()[0] == "idle"             # nobody looks, no emergency: nothing
    assert tr.tt.status()["rate"] == "none"
    view(tr, clock)                                                                # the layer comes on: fast at once
    assert tr.tt.interval() == 5.0 and tr.tt.status()["rate"] == "fast"
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == 1
    for _ in range(3):
        clock.t += 5
        view(tr, clock)
        tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == 4                                              # every 5 s while it is on
    clock.t += TT.VIEW_S + 1                                                        # the layer is off; an event panel still asks
    looking(tr, clock)
    assert tr.tt.interval() == TT.IDLE_S and tr.tt.status()["rate"] == "idle"
    n = fetch.n(TT.LOCATIONS)
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == n                                               # 16 s since the last: not yet
    clock.t += 14
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == n + 1                                           # then every 30 s while viewed
    clock.t += 30
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == n + 2
    clock.t += DEMAND_S + 30                                                        # nobody since: no more reads
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == n + 2


def test_fast_while_an_emergency_is_near_the_line(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    far = (41.06, -73.70)                                                           # ~7 km north of the line
    near = (40.9990, -73.6650)                                                      # beside Port Chester station
    tr.hot_points = lambda: [far]
    assert not tr.tt_fast() and not tr.tt.wanted()
    tr.hot_points = lambda: [far, near]
    assert not tr.tt_fast()                                                         # looked at every 15 s ...
    clock.t += TT.HOT_CHECK_S
    assert tr.tt_fast() and tr.tt.interval() == TT.FAST_S and tr.tt.wanted()       # ... then fast, nobody looking or not
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == 1
    tr.hot_points = lambda: (_ for _ in ()).throw(RuntimeError("db"))              # the CAD's trouble: not fast, never a crash
    clock.t += TT.HOT_CHECK_S
    assert not tr.tt_fast()


def test_only_a_recent_emergency_incident_counts(tmp_path):
    """The app's hot_points: emergency incidents, active or pending, created or changed in the last HOT_AGE_S."""
    from sizeup.cad.store import Store
    import sizeup.app as A
    import time as _t
    from sizeup.traintime import HOT_AGE_S
    db = Store(str(tmp_path / "cad.db"))
    now = _t.time()
    for i, (status, emergency, created, logged) in enumerate([("active", 1, now - 600, None), ("pending", 1, now - 3 * HOT_AGE_S, now - 60),
                                                             ("active", 1, now - 3 * HOT_AGE_S, None), ("active", 0, now - 60, None),
                                                             ("closed", 1, now - 60, None)]):
        db.db.execute("INSERT INTO incidents (id, status, emergency, lat, lon, created_at, address, source) VALUES (?, ?, ?, ?, ?, ?, '', 'manual')",
                      (i + 1, status, emergency, 41.0 + i / 1000, -73.66, created))
        if logged:
            db.db.execute("INSERT INTO events (incident_id, at, kind, text) VALUES (?, ?, 'note', 'x')", (i + 1, logged))
    db.db.commit()
    rows = db.q(A.HOT_INCIDENTS_SQL, (now - HOT_AGE_S,))
    assert sorted(round((r["lat"] - 41.0) * 1000) + 1 for r in rows) == [1, 2]     # new; old but touched; not stale / non-emergency / closed


def test_the_size_guard_and_back_off_override_the_fast_rate(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    view(tr, clock)
    tr.tt.tick()
    assert tr.tt.st["bytes"] == len(LOC) and tr.tt.interval() == TT.FAST_S          # the small answer: 5 s
    big = json.loads(LOC)
    for x in big:
        x.setdefault("details", {})["polyline"] = "_" * (TT.SMALL_MAX_BYTES // 5)   # the polylines came back
    fetch.loc = json.dumps(big).encode()
    clock.t += 5
    view(tr, clock)
    tr.tt.tick()
    assert tr.tt.st["bytes"] > TT.SMALL_MAX_BYTES and tr.tt.interval() == TT.IDLE_S and tr.tt.status()["big"]
    reg = datasources.DataSources(log=lambda m: None, clock=clock, spawn=lambda fn, name: fn())
    for src in datasources.trains_sources(tr):
        reg.register(src)
    assert "every 30 s only" in reg.row("trains:traintime")["error"]
    n = fetch.n(TT.LOCATIONS)
    for _ in range(5):
        clock.t += 5
        view(tr, clock)
        tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == n                                               # not 5 s: 30
    fetch.loc = LOC
    fetch.fail[TT.LOCATIONS] = 429
    clock.t += 5
    view(tr, clock)
    tr.tt.tick()
    for _ in range(10):                                                              # rate-limited: 5 min, however fast
        clock.t += 5
        view(tr, clock)
        tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == n + 1 and tr.tt.due_at() - clock.t > 200


def test_the_back_off_holds_the_route_lines_and_countdowns_too(tmp_path, model):
    tr, clock, fetch = provider(tmp_path)
    fetch.geometry = geometry_body(model)
    tr.tick()
    tr.get("/api/cad/trains/departures", "station=2PC")
    fetch.fail[TT.LOCATIONS] = 429
    view(tr, clock)
    tr.tt.tick()
    assert fetch.n(TT.LOCATIONS) == 1 and fetch.n(TT.GEOMETRY) == 0 and fetch.n("/countdown/") == 0
    clock.t += TT.RATE_LIMITED_S + 1
    del fetch.fail[TT.LOCATIONS]
    view(tr, clock)
    tr.get("/api/cad/trains/departures", "station=2PC")
    tr.tt.tick()
    assert fetch.n(TT.GEOMETRY) == 1 and fetch.n("/countdown/") == 1


def test_each_read_is_pushed_through_the_state_stream(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    pokes = []
    tr.on_change = lambda: pokes.append(tr.rev)
    tr.tick()
    rev = tr.rev
    looking(tr, clock)
    tr.tt.tick()
    assert pokes and tr.rev > rev                                                   # the read landed: a new rev, a poke


def test_the_user_agent_says_who_and_why():
    assert TT.UA == "Port Chester Fire Department - sizeup emergency situational awareness application (github.com/actualsecurity/sizeup)"


def test_off_by_config_or_without_an_address_nothing_is_fetched(tmp_path):
    tr, clock, fetch = provider(tmp_path, traintime=False)
    looking(tr, clock)
    tr.tick()
    tr.tt.tick()
    assert fetch.n("tt.example") == 0 and tr.tt.state()[0] == "off"
    assert all("tt" not in x for x in tr.public()["trains"])
    assert not Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, log=lambda m: None).tt.enabled       # conftest: BASE_URL off


# ---- the route lines, the departures --------------------------------------------------------------------------------


def geometry_body(model):
    doc = json.loads(LOC)
    for x in doc:
        if x.get("train_num") in ("9901", "9902"):
            pts = [model.line.at(ch)[:2] for ch in range(20000, 60000, 500)]     # Mount Vernon to past Stamford
            x["details"]["polyline"] = encode([(lon, lat) for lat, lon in pts])
    return json.dumps(doc).encode()


def test_route_lines_are_read_rarely_clipped_and_cached(tmp_path, model):
    tr, clock, fetch = provider(tmp_path)
    fetch.geometry = geometry_body(model)
    looking(tr, clock)
    tr.tick()
    tr.tt.tick()
    assert fetch.n(TT.GEOMETRY) == 1 and set(tr.tt.routes) == {"2SM>0NY", "0NY>2NH"}
    runs = tr.tt.route("2SM>0NY")
    w, s, e, n = BBOX
    m = TT.GEOMETRY_MARGIN_DEG
    assert runs and all(w - m <= lon <= e + m and s - m <= lat <= n + m for run in runs for lon, lat in run)
    code, body, _, _ = tr.get("/api/cad/trains/route", "key=2SM%3E0NY")
    assert code == 200 and json.loads(body)["runs"] == runs
    assert tr.get("/api/cad/trains/route", "key=../x")[0] == 400
    clock.t += 1800
    looking(tr, clock)
    tr.tt.tick()
    assert fetch.n(TT.GEOMETRY) == 1                                                # 9903's route missing, but under an hour ...
    clock.t += 1800
    looking(tr, clock)
    tr.tt.tick()
    assert fetch.n(TT.GEOMETRY) == 2                                                # ... then once an hour while one is missing
    again = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, cache_dir=tmp_path / "trains", fetch=fetch, tt_fetch=fetch, tt_url=TT_URL,
                   log=lambda m: None, clock=clock)
    assert again.tt.route("2SM>0NY") == runs                                       # from the cache: a restart fetches nothing
    assert (tmp_path / "trains" / "traintime_routes.json").is_file()


def test_a_stations_departures_only_while_its_box_asks(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    tr.tt.tick()
    assert fetch.n("/countdown/") == 0
    code, body, _, _ = tr.get("/api/cad/trains/departures", "station=2PC")
    d = json.loads(body)
    assert code == 200 and d["name"] == "Port Chester" and d["rows"] == [] and d["at"] is None
    tr.tt.tick()
    assert fetch.n("/countdown/2PC?") == 1
    d = json.loads(tr.get("/api/cad/trains/departures", "station=2PC")[1])
    rows = d["rows"]
    assert [r["train"] for r in rows] == ["9902"]                                  # 9901, 9903 have left; 9906 doesn't stop
    assert rows[0]["track"] == "2" and rows[0]["posted"] and rows[0]["to_name"] == "New Haven" and rows[0]["cars"] == 8
    assert all(r["train"] != "1905" for r in rows)                                 # Metro-North only
    tr.tt.tick()
    assert fetch.n("/countdown/") == 1                                             # fresh for 30 s
    clock.t += 31
    tr.tt.tick()
    assert fetch.n("/countdown/") == 1                                             # the box hasn't asked since: nothing
    assert tr.get("/api/cad/trains/departures", "station=XYZ")[0] == 400           # not a station on the map


def test_departures_sorted_by_expected_time():
    rows = TT.departures(recs(), "2GN", NOW)
    assert [r["train"] for r in rows] == ["9906", "9902"] and rows[0]["late_s"] == 120
    assert rows[0]["est"] == NOW + 60 + 120


# ---- the HTTP client, the config ---------------------------------------------------------------------------------------


def test_the_client_sends_our_agent_and_the_api_version_and_unpacks_gzip():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.update(path=self.path, ua=self.headers.get("User-Agent"), ver=self.headers.get("Accept-Version"),
                        enc=self.headers.get("Accept-Encoding"))
            body = gzip.compress(LOC)
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, body = TT.http_get(f"http://127.0.0.1:{httpd.server_address[1]}{TT.LOCATIONS}", 5, TT.MAX_BYTES)
        assert code == 200 and body == LOC
        assert seen["path"] == TT.LOCATIONS and seen["ver"] == "3.0" and seen["enc"] == "gzip"
        assert seen["ua"] == TT.UA
        with pytest.raises(ValueError):
            TT.http_get(f"http://127.0.0.1:{httpd.server_address[1]}/x", 5, 100)  # too big unpacked
    finally:
        httpd.shutdown()


def test_the_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    c = load(p)
    assert c.trains_traintime and c.trains_traintime_url == "" and c.trains_traintime_every_s == 5.0
    p.write_text('[trains]\ntraintime = false\ntraintime_url = "https://tt.example"\ntraintime_every_s = 30\n')
    c = load(p)
    assert not c.trains_traintime and c.trains_traintime_url == "https://tt.example" and c.trains_traintime_every_s == 30.0
    tr = Trains.from_config(c, log=lambda m: None)
    assert not tr.tt.enabled
    for bad in ("traintime = 1", "traintime_every_s = 4", "traintime_every_s = 90", 'traintime_url = "http://tt.example"'):
        p.write_text(f"[trains]\n{bad}\n")
        with pytest.raises(ConfigError):
            load(p)


def test_the_hubs_health_panel_lists_the_feed(tmp_path):
    from tests.test_cloud import HubRun, hub_cfg
    h = HubRun(hub_cfg(tmp_path, trains_traintime_url="https://tt.example", trains_traintime=False))
    try:
        feeds = h.app.health.panel()["feeds"]
        assert [f["key"] for f in feeds] == ["traintime"] and feeds[0]["state"] == "off" and feeds[0]["fallback"] == "GTFS-realtime"
        rows = {r["key"]: r for r in h.app.datasources.public()["sources"]}
        assert not rows["trains:traintime"]["on"] and "trains:traintime-routes" in rows
    finally:
        h.close()


# ---- along the track: noisy GPS fixes, filtered (trains.along_filter) -------------------------------------------------


def noisy_run(model, tr, clock, fetch, s0, v, n, seed=7, stop_after=None):
    """Polls every 5 s of 9902 (eastbound) with a GPS fix that misses by up to 40 m along and 20 m across the track."""
    import math
    import random
    rnd = random.Random(seed)
    doc = json.loads(LOC)
    out = []
    s_true = s0
    for k in range(n):
        speed = 0.0 if stop_after is not None and k >= stop_after else v
        lat, lon, b = model.line.at(s_true + rnd.uniform(-40, 40))
        off = rnd.uniform(-20, 20)                                                   # across the track
        lat += off * math.cos(math.radians(b)) / 110_574
        lon -= off * math.sin(math.radians(b)) / (111_320 * math.cos(math.radians(41)))
        x = next(r for r in doc if r.get("train_num") == "9902")
        x["location"].update(latitude=round(lat, 6), longitude=round(lon, 6), source="GPS", timestamp=int(clock.t) - 2,
                             speed=round(speed * 3600 / 1609.344 + rnd.uniform(-2, 2) * (speed > 0), 1))
        fetch.loc = json.dumps(doc).encode()
        view(tr, clock)
        tr.refresh_rt()
        tr.tt.poll()
        out.append({x["train"]: x for x in tr.public()["trains"]}["9902"])
        clock.t += 5
        s_true += speed * 5
    return out


def test_noisy_gps_moves_forward_only_on_the_track_at_a_smooth_speed(tmp_path, model):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    run = noisy_run(model, tr, clock, fetch, 39000.0, 20.0, 18)
    ss = [t["s"] for t in run]
    assert all(b >= a for a, b in zip(ss, ss[1:]))                                  # never a step back
    assert ss[-1] - ss[0] == pytest.approx(17 * 5 * 20.0, rel=0.2)                 # about the true distance
    for t in run:                                                                    # drawn on the track
        assert t["pos_src"] == "traintime" and model.line.project(t["lat"], t["lon"])[0] < 40
    vs = [t["v"] for t in run[3:]]
    assert all(abs(v - 20.0) < 4.0 for v in vs)                                     # a steady speed, not the jitter
    assert max(abs(b - a) for a, b in zip(vs, vs[1:])) < 2.0
    assert all(t["mode"] == "reported" for t in run[1:]) and run[-1]["predict_until"] == run[-1]["fix_at"] + TRAINS.TT_HORIZON_S


def test_a_stopped_train_stays_put_through_gps_jitter(tmp_path, model):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    run = noisy_run(model, tr, clock, fetch, 40000.0, 15.0, 10, stop_after=4)
    still = run[5:]
    assert all(t["mode"] == "stopped" and t["v"] == 0.0 for t in still)
    assert len({t["s"] for t in still}) == 1                                         # not one metre of jitter drawn
    ss = [t["s"] for t in run]
    assert all(b >= a for a, b in zip(ss, ss[1:]))


def test_the_along_track_filter():
    from sizeup.trains import along_filter
    assert along_filter(None, 1000.0, 1, NOW, 20.0) == (1000.0, 20.0, 0.0)
    last = {"s": 1000.0, "v": 20.0, "fix_at": NOW}
    s, v, a = along_filter(last, 1060.0, 1, NOW + 5, 20.0)                          # 40 m short of where it should be
    assert s == pytest.approx(1080.0) and 18.0 < v < 20.0 and a == pytest.approx((v - 20.0) / 5)
    assert along_filter(last, 990.0, 1, NOW + 1, 20.0)[0] >= 1000.0                 # a miss behind: held, never back
    s, v, a = along_filter({"s": 5000.0, "v": 10.0, "fix_at": NOW}, 4950.0, -1, NOW + 5, 10.0)
    assert s == pytest.approx(4950.0) and v == pytest.approx(10.0)                  # westbound: s falls as it goes


def test_the_first_stopped_fix_is_where_it_stopped_later_ones_are_jitter():
    """#375: a train that was moving and now reports under STOP_MPS is put where the fix says (forward only), not held
    up to STOP_JITTER_M short of it; once stopped, a fix less than STOP_JITTER_M ahead (or any behind) is jitter."""
    from sizeup.trains import STOP_JITTER_M, along_filter
    moving = {"s": 1000.0, "v": 20.0, "a": -0.75, "fix_at": NOW}
    assert along_filter(moving, 1090.0, 1, NOW + 5, 0.0) == (1090.0, 0.0, 0.0)       # 90 m on: there, not held 90 m short
    assert along_filter(moving, 980.0, 1, NOW + 5, 0.0) == (1000.0, 0.0, 0.0)        # behind: never back
    assert along_filter({**moving, "s": 5000.0}, 4910.0, -1, NOW + 5, 0.0)[0] == 4910.0   # westbound too
    stopped = {"s": 1090.0, "v": 0.0, "a": 0.0, "fix_at": NOW + 5}
    assert along_filter(stopped, 1090.0 + STOP_JITTER_M - 10, 1, NOW + 10, 0.0) == (1090.0, 0.0, 0.0)   # jitter: put
    assert along_filter(stopped, 1070.0, 1, NOW + 10, 0.0) == (1090.0, 0.0, 0.0)
    assert along_filter(stopped, 1090.0 + STOP_JITTER_M + 10, 1, NOW + 10, 0.0)[0] == 1090.0 + STOP_JITTER_M + 10


def test_the_filter_sends_its_braking():
    """#375: the speed falling between fixes is sent as a (m/s^2, from the feed's own speeds), and while braking the
    feed's speed is the train's (the filter's lags it) and the next prediction brakes too."""
    from sizeup.trains import along_filter
    last = {"s": 1000.0, "v": 20.0, "vf": 20.0, "a": 0.0, "fix_at": NOW}
    s, v, a = along_filter(last, 1000.0 + 20 * 4 - 0.375 * 16, 1, NOW + 4, 17.0)     # braking at 0.75 m/s^2
    assert a == pytest.approx(-0.75) and v == 17.0
    last = {"s": s, "v": v, "vf": 17.0, "a": a, "fix_at": NOW + 4}
    s2, v2, a2 = along_filter(last, s + 17 * 4 - 0.375 * 16, 1, NOW + 8, 14.0)
    assert s2 == pytest.approx(s + 17 * 4 - 0.375 * 16, abs=0.5) and a2 == pytest.approx(-0.75)   # predicted braking: no miss
    s3, v3, a3 = along_filter({"s": 1000.0, "v": 20.0, "fix_at": NOW}, 1100.0, 1, NOW + 5, 21.0)
    assert a3 >= 0                                                                  # (no feed speed before: the filter's)


# ---- review (#366): newer positions only, old fixes, midnight, staleness per train ---------------------------------


def test_a_newer_gtfs_position_is_kept():
    feed = parse_feed(gtfs_rt())
    m = TT.merge(feed, kept(), NOW + 5, NOW + 5, None, {"9901": NOW - 5})            # GTFS's appeared after TrainTime's fix
    assert m["trains"]["9901"]["pos"] == feed["trains"]["9901"]["pos"] and m["trains"]["9901"]["tt"]["cars"] == 6
    m = TT.merge(feed, kept(), NOW + 5, NOW + 5, None, {"9901": NOW - 10})           # as new: TrainTime's
    assert m["trains"]["9901"]["pos"]["src"] == "traintime"
    assert TT.POS_MAX_AGE_S <= 45
    m = TT.merge(feed, kept(), NOW - 10 + TT.POS_MAX_AGE_S + 1, None, None, {"9901": 0.0})
    assert m["trains"]["9901"]["pos"] == feed["trains"]["9901"]["pos"]              # too old, however old GTFS's is


def test_an_equal_or_older_gps_fix_changes_nothing(tmp_path):
    from sizeup.trains import along_filter
    last = {"s": 1000.0, "v": 20.0, "fix_at": NOW}
    assert along_filter(last, 1200.0, 1, NOW, 20.0) == (1000.0, 20.0, 0.0)           # the same time
    assert along_filter(last, 900.0, 1, NOW - 5, 20.0) == (1000.0, 20.0, 0.0)        # older
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    looking(tr, clock)
    tr.refresh_rt()
    tr.tt.tick()
    first = dict(tr.track.fixes["9901"])
    doc = json.loads(LOC)
    doc[0]["location"].update(latitude=doc[0]["location"]["latitude"] + 0.002, timestamp=doc[0]["location"]["timestamp"] - 3)
    fetch.loc = json.dumps(doc).encode()                                             # an older fix, somewhere else
    clock.t += 5
    view(tr, clock)
    tr.tt.tick()
    assert tr.track.fixes["9901"]["s"] == first["s"] and tr.track.fixes["9901"]["fix_at"] == first["fix_at"]


def test_the_same_position_again_with_a_newer_time_is_a_stopped_train(tmp_path):
    """#375: a GPS fix repeated exactly (lat/lon) with a newer timestamp hasn't moved, whatever speed it says: v = 0."""
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    looking(tr, clock)
    doc = json.loads(LOC)
    x = next(r for r in doc if r.get("train_num") == "9901")
    x["location"].update(timestamp=int(clock.t) - 4, speed=30.0)
    fetch.loc = json.dumps(doc).encode()
    tr.refresh_rt()
    tr.tt.tick()
    assert tr.track.fixes["9901"]["v"] > 0
    first = dict(tr.track.fixes["9901"])
    clock.t += 5
    x["location"].update(timestamp=int(clock.t) - 4)                                 # the same lat/lon, newer, "30 mph"
    fetch.loc = json.dumps(doc).encode()
    view(tr, clock)
    tr.tt.tick()
    rec = tr.track.fixes["9901"]
    assert rec["fix_at"] > first["fix_at"] and rec["v"] == 0.0 and rec["s"] == first["s"]
    t = {x["train"]: x for x in tr.public()["trains"]}["9901"]
    assert t["mode"] == "stopped" and t["v"] == 0.0


def test_the_same_signal_location_again_is_not_a_stop(tmp_path):
    """#379: a SIGNAL location is a fixed point (a signal's), so the same one again with a newer time says nothing about
    whether the train moved: its speed stands, it isn't stopped."""
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    looking(tr, clock)
    doc = json.loads(LOC)
    x = next(r for r in doc if r.get("train_num") == "9901")
    x["location"].update(timestamp=int(clock.t) - 4, speed=30.0, source="SIGNAL")
    fetch.loc = json.dumps(doc).encode()
    tr.refresh_rt()
    tr.tt.tick()
    clock.t += 5
    x["location"].update(timestamp=int(clock.t) - 4)                                 # the same signal, newer, "30 mph"
    fetch.loc = json.dumps(doc).encode()
    view(tr, clock)
    tr.tt.tick()
    rec = tr.track.fixes["9901"]
    assert rec["v"] > 10 and rec["vf"] == pytest.approx(30 * 0.44704) and rec["gps"] is False   # (the filter blends)
    assert {x["train"]: x for x in tr.public()["trains"]}["9901"]["mode"] != "stopped"


def test_a_speed_reading_is_checked_against_the_distance_moved():
    """#379, #390: the GPS speed (m/s) against the distance moved along the track over the two fixes' own times. One bad
    reading (0 at 20 m/s, the train still 100 m on in 5 s) is set aside and not braked on; a second that agrees is
    taken; a fall the distance moved agrees with is braked on; a steady reading with a jumpy position flags the
    position; a fall with no position to check it by (a SIGNAL location) is braked on only once two readings fall."""
    from sizeup.trains import speed_check
    last = {"s_raw": 1000.0, "fix_at": NOW, "vf": 20.0, "gps": True, "a": 0.0}
    r = speed_check(last, 1100.0, 1, NOW + 5, 0.0, True)
    assert r["bad"] == "speed" and r["vf"] == 20.0 and r["moved"] == pytest.approx(20.0) and not r["brake"]
    second = {**last, "s_raw": 1100.0, "fix_at": NOW + 5, "vf_held": r["held"]}
    r2 = speed_check(second, 1105.0, 1, NOW + 10, 0.2, True)                         # it did stop: the second agrees
    assert r2["bad"] == "" and r2["vf"] == 0.2 and r2["brake"]
    r3 = speed_check(last, 1000.0 + 5 * 18.5, 1, NOW + 5, 17.0, True)                # 20 -> 17, 92.5 m: braking, confirmed
    assert r3["bad"] == "" and r3["vf"] == 17.0 and r3["brake"]
    r4 = speed_check(last, 1160.0, 1, NOW + 5, 20.0, True)                           # steady 20, but 160 m in 5 s
    assert r4["bad"] == "position" and r4["vf"] == 20.0
    sig = {**last, "gps": False}
    r5 = speed_check(sig, 1100.0, 1, NOW + 5, 17.0, False)
    assert r5["moved"] is None and r5["fell"] and not r5["brake"]                    # one falling reading: not yet
    r6 = speed_check({**sig, "vf": 17.0, "fell": True, "fix_at": NOW + 5}, 1100.0, 1, NOW + 10, 14.0, False)
    assert r6["brake"]                                                               # two in a row: braking
    r7 = speed_check({**last, "gps": False}, 1100.0, 1, NOW + 5, 0.0, False)         # 20 -> 0 in 5 s: no train can
    assert r7["bad"] == "speed" and r7["vf"] == 20.0


def test_one_bad_speed_reading_is_no_stop_on_the_hub(tmp_path, model):
    """#379: a train at 20 m/s with one reading of 0 is neither stopped nor braked (the page draws it on); the next
    reading at 20 carries on."""
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    doc = json.loads(LOC)
    x = next(r for r in doc if r.get("train_num") == "9902")
    out = []
    for k, mph in enumerate((44.7, 44.7, 44.7, 0.0, 44.7, 44.7)):
        lat, lon, _ = model.line.at(39000.0 + 100.0 * k)
        x["location"].update(latitude=round(lat, 6), longitude=round(lon, 6), source="GPS", timestamp=int(clock.t) - 2, speed=mph)
        fetch.loc = json.dumps(doc).encode()
        view(tr, clock)
        tr.refresh_rt()
        tr.tt.poll()
        out.append({t["train"]: t for t in tr.public()["trains"]}["9902"])
        clock.t += 5
    bad = out[3]
    assert bad["mode"] == "reported" and bad["v"] > 15 and bad["a"] >= TRAINS.A_BRAKE
    assert bad["v_basis"]["bad"] == "speed" and bad["v_basis"]["gps"] == 0.0 and bad["v_basis"]["moved"] == pytest.approx(20.0, abs=1)
    assert all(t["mode"] == "reported" and t["v"] > 15 for t in out)


def test_what_a_consist_can_do():
    """#390: per fleet, with sources in trains.py: an EMU (M8, M7) by its rated figures whatever its length; a diesel
    push-pull slower to accelerate the longer (heavier) it is; an unknown fleet as the M8 (the quickest: the drawn train
    is never held back from the real one). The page gets it as each TrainTime train's dyn."""
    from sizeup.trains import consist
    m8 = consist("M8", 8)
    assert m8 == consist("M8", 4) == consist("", None) == consist("XYZ", 6)
    assert m8["a0"] == pytest.approx(0.89, abs=0.01) and m8["b"] == pytest.approx(1.34, abs=0.01)
    assert m8["vmax"] == pytest.approx(35.8, abs=0.1) and 0.7 <= m8["bs"] <= 0.9
    assert m8["pw"] / 30.0 < m8["a0"]                                                # tapering at speed
    d5, d9 = consist("MNR_DIESEL", 5), consist("MNR_DIESEL", 9)
    assert d9["a0"] < d5["a0"] < m8["a0"] and d9["pw"] < d5["pw"]
    assert consist("M7A", 6)["b"] == pytest.approx(3.0 * 0.44704, abs=0.01)


def test_a_traintime_train_tells_the_page_its_consist_and_speed_basis(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    looking(tr, clock)
    tr.refresh_rt()
    tr.tt.tick()
    t = {x["train"]: x for x in tr.public()["trains"]}["9901"]
    assert t["pos_src"] == "traintime" and t["dyn"] == TRAINS.consist("M8", 6)
    assert t["v_basis"] == {"gps": round(52.3 * 0.44704, 2), "moved": None, "bad": ""}
    assert t["hold_kind"] != "fallback" or (t["hold_s"] - t["s"]) * t["d"] == pytest.approx(TRAINS.HOLD_FALLBACK_TT_M, abs=0.2)


def test_just_after_midnight_the_service_date_matches(tmp_path):
    """A train that left at 23:50 runs on past midnight: GTFS-realtime's start_date and TrainTime's run_date are both
    the service date (the day before), so it still merges; a date that doesn't line up is logged once."""
    doc = json.loads(LOC)
    for x in doc:
        if isinstance(x.get("run_date"), str):
            x["run_date"] = "2026-10-13"
    rs = [r for r in TT.parse_locations(json.dumps(doc).encode())["records"] if TT.ours(r, BBOX)]
    m = TT.merge(parse_feed(gtfs_rt(start_date="20261013")), rs, NOW)
    assert m["trains"]["9901"]["tt"]["cars"] == 6 and not m["tt_misses"]
    logs = []
    tr = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, cache_dir=tmp_path / "t", fetch=Fetch(rt=gtfs_rt(start_date="20261015")),
                tt_fetch=Fetch(), tt_url=TT_URL, log=logs.append, clock=Clock(NOW + 5))
    tr.tick()
    tr.asked = tr.clock()
    tr.refresh_rt()
    tr.tt.tick()
    tr.refresh_rt()
    miss = [m for m in logs if "run date" in m]
    assert len(miss) == 2 and "train 9901: GTFS-realtime run date 2026-10-15, TrainTime has 2026-10-14" in miss[0]


def test_staleness_is_each_trains_own(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    looking(tr, clock)
    tr.refresh_rt()
    tr.tt.tick()
    clock.t += 90                                                                    # GTFS-realtime not read since: stale
    fresh = json.loads(LOC)
    for x in fresh:
        if x.get("location"):
            x["location"]["timestamp"] = int(clock.t) - 3
    fetch.loc = json.dumps(fresh).encode()
    view(tr, clock)
    tr.tt.poll()
    d = tr.public()
    t = {x["train"]: x for x in d["trains"]}
    assert d["stale"] and t["9901"]["pos_src"] == "traintime" and t["9901"]["stale"] is False
    assert isinstance(d["now"], float)
