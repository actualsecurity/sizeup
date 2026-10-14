"""Metro-North trains (sizeup/trains.py, #131): the MTA's GTFS-realtime feed read by a small protobuf
reader, the static GTFS's line and stations, where each train is (reported, or estimated between
stations), the near-the-tracks check and "Trains approaching" for an event, the schedule (only while
someone looks), the cache and backoff, the config, and the hub's API.

Never the MTA itself (conftest.py turns the default addresses off and refuses any host but this one).
The fixtures in tests/fixtures/mta/ are recorded from the MTA's feeds on 2026-10-14 at 11:33 EDT and
trimmed by tools/record_trains_fixtures.py (the New Haven Line's trains; the line's shapes, one trip a
train number)."""

import json
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sizeup import USER_AGENT
from sizeup import trains as T
from sizeup.config import ConfigError, load
from sizeup.trains import (BACKOFF_S, DEMAND_S, NEAR_M, REFUSED_S, RT_EVERY_S, STATIC_EVERY_S, Model, Trains, parse_feed,
                           parse_static, pb)
from tests.test_cloud import HubRun, hub_cfg

FIX = Path(__file__).parent / "fixtures" / "mta"
RT = (FIX / "mnr-rt.pb").read_bytes()
GTFS = (FIX / "gtfsmnr.zip").read_bytes()
NOW = 1_791_992_007                      # the recorded feed's own time: 2026-10-14 11:33:27 EDT
PC = (41.000732, -73.6647)               # Port Chester station
# The default area (#151): the line from Mount Vernon East to Stamford, about 31 km of it
AREA = ["Mt Vernon East", "Pelham", "New Rochelle", "Larchmont", "Mamaroneck", "Harrison", "Rye", "Port Chester", "Greenwich",
             "Cos Cob", "Riverside", "Old Greenwich", "Stamford"]
RT_URL, GTFS_URL = "https://mta.example/rt", "https://mta.example/gtfs.zip"


@pytest.fixture(scope="module")
def static():
    return parse_static(GTFS)


@pytest.fixture(scope="module")
def model(static):
    return Model(static)


@pytest.fixture(scope="module")
def feed():
    return parse_feed(RT)


# ---- the protobuf reader -------------------------------------------------------------------------------------


def varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def field(f: int, v) -> bytes:
    if isinstance(v, int):
        return varint(f << 3) + varint(v)
    if isinstance(v, float):
        return varint(f << 3 | 5) + struct.pack("<f", v)
    v = v.encode() if isinstance(v, str) else v
    return varint(f << 3 | 2) + varint(len(v)) + v


def msg(*fields) -> bytes:
    return b"".join(field(f, v) for f, v in fields)


def test_the_reader_reads_varints_negatives_floats_and_nested_messages():
    m = pb(msg((1, 150), (2, "hi"), (3, -300), (4, 40.5), (5, msg((1, 7)))))
    assert m[1] == [150] and m[2] == [b"hi"] and T._sint(m, 3) == -300
    assert T._float(m, 4) == 40.5 and pb(m[5][0]) == {1: [7]}
    with pytest.raises(ValueError):
        pb(b"\x0a\x05ab")                                  # a length past the end
    with pytest.raises(ValueError):
        pb(b"\x08\xff")                                    # a varint that never ends
    with pytest.raises(ValueError):
        parse_feed(b"\x0b\x00")                            # a group (wire type 3): not GTFS-realtime


def test_a_built_feed_gives_the_trip_update_and_the_vehicle_merged():
    stu = lambda sid, t, delay, track, status: msg((4, sid), (2, msg((1, delay), (2, t))), (3, msg((1, delay), (2, t))),
                                                   (1005, msg((1, track), (2, status))))
    tu = msg((1, msg((1, "T1"), (3, "20261014"), (5, "3"))), (3, msg((1, "1350"), (2, "1350"))),
             (2, stu("114", NOW + 60, 120, "3", "On-Time")), (2, stu("115", NOW + 300, 120, "3", "On-Time")), (2, msg((4, "116"), (5, 1))))
    vp = msg((1, msg((1, "T1"))), (2, msg((1, 40.99), (2, -73.68), (3, 60.0))), (5, NOW), (8, msg((1, "1350"), (2, "1350"))))
    body = msg((1, msg((1, "2.0"), (3, NOW))), (2, msg((1, "a"), (3, tu))), (2, msg((1, "b"), (4, vp))),
               (2, msg((1, "gone"), (2, 1), (3, tu))))
    f = parse_feed(body)
    assert f["timestamp"] == NOW and list(f["trains"]) == ["1350"]
    t = f["trains"]["1350"]
    assert t["route_id"] == "3" and t["start_date"] == "20261014"
    assert [s["stop_id"] for s in t["stops"]] == ["114", "115"]            # the SKIPPED stop left out
    assert t["stops"][0] == {"stop_id": "114", "arr": NOW + 60, "dep": NOW + 60, "delay": 120, "track": "3", "status": "On-Time"}
    assert t["pos"]["lat"] == pytest.approx(40.99, abs=1e-5) and t["pos"]["bearing"] == 60.0


def test_the_recorded_feed(feed):
    assert feed["timestamp"] == NOW and len(feed["trains"]) == 122
    t = feed["trains"]["1351"]                         # inbound, 4 min late, just past Harrison
    assert t["route_id"] == "3" and t["train"] == "1351"
    assert [s["stop_id"] for s in t["stops"]][:3] == ["124", "121", "120"] and len(t["stops"]) == 15
    assert {s["delay"] for s in t["stops"]} == {240} and t["stops"][8]["status"] == "Arriving" and t["stops"][8]["track"] == "3"
    assert t["pos"] == {"lat": pytest.approx(40.96978, abs=1e-5), "lon": pytest.approx(-73.711304, abs=1e-5), "bearing": None, "ts": NOW}
    assert sum(1 for t in feed["trains"].values() if t["pos"]) == 17
    assert all(s["arr"] or s["dep"] for t in feed["trains"].values() for s in t["stops"])


def test_what_a_vehicle_position_carries_no_bearing_and_no_car_count(feed):
    """#151: the recorded feed (like the live one, decoded 2026-10-14) gives a train's lat / lon, time and number,
    never its bearing or how many cars it has (no multi_carriage_details): nothing to draw cars from."""
    pos = [t["pos"] for t in feed["trains"].values() if t["pos"]]
    assert pos and all(p["bearing"] is None and p["ts"] for p in pos)
    assert not any("cars" in t for t in feed["trains"].values())
    for ent_b in pb(RT).get(2, []):
        vp = pb(ent_b).get(4)
        if vp:
            fields = set(pb(vp[0]))
            assert 11 not in fields and 9 not in fields and 10 not in fields      # no consist, no occupancy
            p = pb(vp[0]).get(2)
            assert p is None or set(pb(p[0])) <= {1, 2}                           # latitude, longitude: no bearing, speed


def test_a_car_count_comes_only_from_the_feed(model):
    """Should the MTA add multi_carriage_details (one per car), that count goes to the map; without it, none."""
    tu = msg((1, msg((1, "T1"), (5, "3"))), (3, msg((1, "1350"), (2, "1350"))),
             (2, msg((4, "114"), (2, msg((2, NOW + 60))))), (2, msg((4, "115"), (2, msg((2, NOW + 300))))))

    def feed_with(*cars):
        vp = msg((1, msg((1, "T1"))), (2, msg((1, 40.98612), (2, -73.68225))), (5, NOW), (8, msg((1, "1350"), (2, "1350"))),
                 *[(11, msg((1, f"c{i}"), (5, i))) for i in cars])
        return parse_feed(msg((1, msg((1, "2.0"), (3, NOW))), (2, msg((1, "a"), (3, tu), (4, vp)))))
    f = feed_with(*range(1, 9))
    assert f["trains"]["1350"]["cars"] == 8
    [t] = model.trains(f, NOW)
    assert t["train"] == "1350" and t["cars"] == 8
    [u] = model.trains(feed_with(), NOW)
    assert "cars" not in u
    assert feed_with(*range(40))["trains"]["1350"]["cars"] == T.MAX_CARS


# ---- the static feed: the line, its stations, the run times ------------------------------------------------------


def test_the_line_runs_from_grand_central_with_our_stations_on_it_in_order(static, model):
    first = static["line"][0]
    assert T.metres(first[1], first[0], 40.752998, -73.977056) < 300            # Grand Central first
    names = [s["name"] for s in model.area_stations]
    assert names == AREA
    st = {s["name"]: s for s in static["stations"].values()}
    assert "Glenbrook" not in st and "White Plains" not in st                      # branches and the Harlem Line: not on it
    assert 40_000 < st["Port Chester"]["ch"] < 42_000
    order = sorted(static["stations"].values(), key=lambda s: s["ch"])
    assert all(a["tc"] < b["tc"] for a, b in zip(order, order[1:]))               # the time coordinate only grows
    # Rye to Port Chester (2.3 km): a couple of minutes for a train that doesn't stop
    assert 60 < st["Port Chester"]["tc"] - st["Rye"]["tc"] < 300
    assert model.in_area(st["Port Chester"]["ch"]) and model.in_area(st["Stamford"]["ch"]) and model.in_area(st["New Rochelle"]["ch"])
    assert not model.in_area(st["Noroton Heights"]["ch"]) and not model.in_area(st["Wakefield"]["ch"])
    assert 30_000 < model.area[1] - model.area[0] < 33_000


def test_the_static_feed_without_the_new_haven_line_is_refused():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("routes.txt", "route_id,route_long_name\n1,Hudson\n")
        z.writestr("trips.txt", "route_id,trip_id,shape_id\n1,a,1\n")
        z.writestr("shapes.txt", "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n")
        z.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n")
    with pytest.raises(ValueError):
        parse_static(buf.getvalue())
    with pytest.raises(ValueError):
        parse_static(b"not a zip")


# ---- where the trains are -----------------------------------------------------------------------------------------


def test_the_trains_in_our_area_now_reported_by_the_mta(model, feed):
    trains = {t["train"]: t for t in model.trains(feed, NOW)}
    # Mount Vernon East to Stamford: those two by Rye and Port Chester, and four more out along the line
    assert set(trains) == {"1351", "1322", "1353", "1553", "1324", "1522"}
    assert trains["1324"]["next_stop"] == "Mt Vernon East" and trains["1553"]["dir"] == "inbound"
    assert trains["1522"]["headsign"] == "New Haven" and trains["1522"]["next_stop"] == "Stamford"   # the express, at New Rochelle
    a = trains["1351"]
    assert a["dir"] == "inbound" and a["dir_label"] == "Toward New York" and a["headsign"] == "Grand Central"
    assert a["next_stop"] == "Mamaroneck" and a["track"] == "3" and a["delay_s"] == 240 and a["status"] == "Arriving"
    assert a["estimated"] is False and a["line"] == "New Haven" and 200 < a["bearing"] < 290        # heading west-southwest
    b = trains["1322"]
    assert b["dir"] == "outbound" and b["headsign"] == "Stamford" and b["next_stop"] == "Riverside" and b["delay_s"] is None
    assert 20 < b["bearing"] < 120


def test_each_train_lies_along_the_line_the_way_it_is_going(model, feed):
    """#151: the feed has no bearing, so a train's heading is the line's own over about a train's length there,
    turned the way it is going; positions to about a metre."""
    import math
    for t in model.trains(feed, NOW):
        _, ch = model.line.project(t["lat"], t["lon"])
        h = model.line.heading(ch)
        want = h if t["dir"] == "outbound" else (h + 180) % 360
        assert abs((t["bearing"] - want + 180) % 360 - 180) <= 1, t["train"]
        assert t["lat"] == round(t["lat"], T.DP) and t["lon"] == round(t["lon"], T.DP)
    east = T.Line([(-73.70, 41.0), (-73.69, 41.0), (-73.68, 41.0)])
    assert east.heading(800) == pytest.approx(90, abs=0.5)
    # a kink: over 150 m the heading is the chord's, between the two segments' (not the short one's alone)
    kink = T.Line([(-73.70, 41.0), (-73.69, 41.0), (-73.689, 41.0006)])
    k = kink.heading(kink.ch[1])
    assert 50 < k < 90 and not math.isclose(k, kink.at(kink.ch[1] + 1)[2], abs_tol=5)


def test_a_train_without_a_position_is_estimated_between_its_stations(model, feed):
    t = json.loads(json.dumps(feed["trains"]["1322"]))
    t["pos"] = None
    for s in t["stops"]:                                                  # as the feed said it earlier
        s["status"] = "On-Time"
    one = {"timestamp": NOW, "trains": {"1322": t}}
    stops = {s["stop_id"]: s for s in t["stops"]}
    rye, pc = stops["114"]["dep"], stops["115"]["arr"]                    # Rye 11:24, Port Chester 11:27
    mid = (rye + pc) / 2
    [x] = model.trains(one, mid)
    assert x["estimated"] is True and x["next_stop"] == "Port Chester"
    st = {s["name"]: s for s in model.area_stations}
    d_rye = T.metres(x["lat"], x["lon"], st["Rye"]["lat"], st["Rye"]["lon"])
    d_pc = T.metres(x["lat"], x["lon"], st["Port Chester"]["lat"], st["Port Chester"]["lon"])
    assert 300 < d_rye < 2200 and 300 < d_pc < 2200                       # between the two, on the line
    assert model.line.project(x["lat"], x["lon"])[0] < 5
    [y] = model.trains(one, pc)                                           # at the station
    assert T.metres(y["lat"], y["lon"], st["Port Chester"]["lat"], st["Port Chester"]["lon"]) < 150
    assert model.trains(one, stops["124"]["arr"] + 600) == []             # long gone (its last stop, Stamford, 10 min ago)
    assert model.trains({"timestamp": NOW, "trains": {"x": dict(t, stops=t["stops"][:1])}}, NOW) == []


def test_other_lines_and_finished_trains_are_not_ours(model, feed):
    hudson = next(t for t in feed["trains"].values() if t["route_id"] == "3")
    other = dict(hudson, route_id="1")
    assert not model.ours_train(other, NOW)
    done = dict(feed["trains"]["1351"])
    assert not model.ours_train(done, max(s["arr"] for s in done["stops"]) + T.GONE_S + 1)


# ---- near the tracks, and the trains approaching ------------------------------------------------------------------


def offset(lat, lon, north_m, east_m):
    return lat + north_m / T.MY, lon + east_m / T.MX


def test_near_the_tracks_a_station_or_neither(model):
    ref = model.near(*PC)
    assert ref["kind"] == "station" and ref["name"] == "Port Chester" and ref["station"] == "115" and ref["m"] < 10
    # a point on the tracks between Rye and Port Chester, then 100 m and 400 m off them
    st = {s["name"]: s for s in model.area_stations}
    lat, lon, b = model.line.at((st["Rye"]["ch"] + st["Port Chester"]["ch"]) / 2)
    import math
    side = math.radians(b + 90)
    on = model.near(lat, lon)
    assert on["kind"] == "track" and on["m"] < 5 and on["name"] in ("Rye", "Port Chester") and on["station_m"] > 500
    near = model.near(*offset(lat, lon, 100 * math.cos(side), 100 * math.sin(side)))
    assert near["kind"] == "track" and 90 < near["m"] < 110
    assert model.near(*offset(lat, lon, 400 * math.cos(side), 400 * math.sin(side))) is None
    assert model.near(41.0130, -73.6640) is None              # downtown Port Chester, 1.4 km from the station
    assert NEAR_M == 150


def test_trains_approaching_port_chester_each_way_in_the_next_15_minutes(model, feed):
    a = model.approaching(feed, *PC, NOW)
    assert a["near"] and a["ref"]["name"] == "Port Chester" and a["window_min"] == 15 and "ch" not in a["ref"]
    inbound = {x["train"]: x for x in a["inbound"]}
    outbound = {x["train"]: x for x in a["outbound"]}
    # 1353 stops here at 11:48 (its predicted time, real); 1553, the express from New Haven 11 min late, passes (estimated)
    assert inbound["1353"]["stops"] is True and inbound["1353"]["estimated"] is False and inbound["1353"]["track"] == "3"
    assert inbound["1353"]["eta"] == NOW + 873 and inbound["1353"]["min"] == 15
    assert inbound["1553"]["stops"] is False and inbound["1553"]["estimated"] is True and inbound["1553"]["delay_s"] == 660
    assert inbound["1553"]["reported"] is True and inbound["1553"]["track"] == ""
    assert outbound["1522"]["stops"] is False and outbound["1522"]["estimated"] and outbound["1522"]["headsign"] == "New Haven"
    for x in a["inbound"] + a["outbound"]:
        assert NOW - 60 <= x["eta"] <= NOW + 900
    assert [x["eta"] for x in a["inbound"]] == sorted(x["eta"] for x in a["inbound"])
    # 1351 left Port Chester at 11:24 ("Departed"), 1322 at 11:27: neither is approaching
    assert "1351" not in inbound and "1322" not in outbound
    assert model.approaching(feed, 41.0130, -73.6640, NOW) == {"near": False}


def test_an_express_passing_a_point_between_stations_is_timed_by_the_run_times(model):
    st = {s["name"]: s for s in model.area_stations}
    stam = next(s for s in model.stations.values() if s["name"] == "Stamford")
    t = {"id": "9", "train": "1599", "route_id": "3", "pos": None, "delay": None, "stops": [
        {"stop_id": stam["id"], "arr": NOW, "dep": NOW, "delay": 0, "track": "3", "status": "On-Time"},
        {"stop_id": st["Harrison"]["id"], "arr": NOW + 1200, "dep": NOW + 1200, "delay": 0, "track": "3", "status": "On-Time"}]}
    f = {"timestamp": NOW, "trains": {"1599": t}}
    a = model.approaching(f, *PC, NOW)
    [x] = a["inbound"]
    assert x["stops"] is False and x["estimated"] is True
    # its 20 minutes shared by the run times: Port Chester well past halfway in time, before Harrison
    frac = (model.tc(stam["ch"]) - model.tc(st["Port Chester"]["ch"])) / (model.tc(stam["ch"]) - model.tc(st["Harrison"]["ch"]))
    assert x["eta"] == round(NOW + 1200 * frac) and 0.5 < frac < 0.9
    # the MTA reports it beyond Port Chester already: not approaching
    lat, lon, _ = model.line.at(st["Port Chester"]["ch"] - 400)
    t2 = dict(t, pos={"lat": lat, "lon": lon, "bearing": None, "ts": NOW})
    assert model.approaching({"trains": {"1599": t2}}, *PC, NOW)["inbound"] == []
    # reported between Stamford and Port Chester: timed from there
    lat, lon, _ = model.line.at(st["Greenwich"]["ch"])
    t3 = dict(t, pos={"lat": lat, "lon": lon, "bearing": None, "ts": NOW})
    [y] = model.approaching({"trains": {"1599": t3}}, *PC, NOW)["inbound"]
    assert y["reported"] and NOW < y["eta"] < x["eta"] + 600
    # beyond 15 minutes: not shown
    t4 = dict(t, stops=[dict(t["stops"][0], arr=NOW + 1800, dep=NOW + 1800), dict(t["stops"][1], arr=NOW + 3000, dep=NOW + 3000)])
    assert model.approaching({"trains": {"1599": t4}}, *PC, NOW)["inbound"] == []


# ---- the provider: schedule, cache, backoff ----------------------------------------------------------------------


class Clock:
    def __init__(self, t=NOW):
        self.t = float(t)

    def __call__(self):
        return self.t


class FakeFetch:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, int] = {}

    def __call__(self, url, timeout, limit, headers=None):
        self.calls.append((url, dict(headers or {})))
        if url in self.fail:
            from urllib.error import HTTPError
            raise HTTPError(url, self.fail[url], "nope", None, None)
        return 200, {"last-modified": "Mon, 12 Oct 2026 16:25:40 GMT"}, RT if url == RT_URL else GTFS

    def n(self, url):
        return sum(1 for u, _ in self.calls if u == url)


def provider(tmp_path, **kw):
    clock, fetch = Clock(), FakeFetch()
    tr = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, cache_dir=tmp_path / "trains", fetch=fetch, log=lambda m: None, clock=clock, **kw)
    return tr, clock, fetch


def test_the_realtime_feed_only_while_someone_looks(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    assert fetch.n(GTFS_URL) == 1 and fetch.n(RT_URL) == 0 and tr.model is not None      # the stations and line; no one looks
    clock.t += 600
    tr.tick()
    assert fetch.n(RT_URL) == 0 and fetch.n(GTFS_URL) == 1
    code, body, _, _ = tr.get("/api/cad/trains", "")                                       # the layer is on somewhere
    assert code == 200 and tr.asked == clock.t
    tr.tick()
    assert fetch.n(RT_URL) == 1
    clock.t += RT_EVERY_S - 1
    tr.tick()
    assert fetch.n(RT_URL) == 1
    clock.t += 1
    tr.tick()
    assert fetch.n(RT_URL) == 2
    clock.t += DEMAND_S + 1                                                                   # nobody asked since: idle
    tr.tick()
    assert fetch.n(RT_URL) == 2
    clock.t += STATIC_EVERY_S
    tr.tick()
    assert fetch.n(GTFS_URL) == 2                                                             # weekly
    assert all(h == {} for _, h in fetch.calls)                                              # no key: no key header


def test_the_event_panel_asks_only_when_the_event_is_near_the_tracks(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    code, body, _, _ = tr.get("/api/cad/trains/near", "lat=41.0130&lon=-73.6640")
    assert code == 200 and json.loads(body)["near"] is False and tr.asked == 0
    code, body, _, _ = tr.get("/api/cad/trains/near", f"lat={PC[0]}&lon={PC[1]}")
    assert code == 200 and json.loads(body)["near"] is True and tr.asked == clock.t
    tr.tick()
    d = json.loads(tr.get("/api/cad/trains/near", f"lat={PC[0]}&lon={PC[1]}")[1])
    assert {x["train"] for x in d["inbound"]} >= {"1353", "1553"} and d["at"] == NOW and d["disclaimer"]
    assert d["dir_labels"] == {"inbound": "Toward New York", "outbound": "Toward New Haven"}
    for q in ("", "lat=x&lon=1", "lat=91&lon=0"):
        assert tr.get("/api/cad/trains/near", q)[0] == 400
    assert tr.get("/api/cad/trains/other", "")[0] == 404


def test_the_layer_says_what_it_shows(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.get("/api/cad/trains", "")
    tr.tick()
    d = json.loads(tr.get("/api/cad/trains", "")[1])
    assert d["configured"] and d["note"] == "" and d["source"] == "MTA" and d["terms"].startswith("https://www.mta.info/")
    assert d["at"] == NOW and d["fetched"] == clock.t and d["static_at"] == clock.t and "may not be real time" in d["disclaimer"]
    assert [s["name"] for s in d["stations"]] == AREA and len(d["line"]) > 20
    w, s, e, n = T.DEFAULT_BBOX
    assert all(w - 0.03 < lon < e + 0.03 and s - 0.03 < lat < n + 0.03 for lon, lat in d["line"])
    assert {t["train"] for t in d["trains"]} >= {"1351", "1322"} and "catenary" in d["catenary"]
    assert "mta.example" not in json.dumps(d)                                               # the feed addresses stay here


def test_a_poll_that_holds_the_line_gets_only_the_trains(tmp_path):
    """#151: the line and stations are most of the answer and change weekly: a page that has them says so
    (?have=static_at) and gets the trains alone."""
    tr, clock, fetch = provider(tmp_path)
    tr.get("/api/cad/trains", "")
    tr.tick()
    full = tr.get("/api/cad/trains", "")[1]
    d = json.loads(full)
    assert d["line"] and d["stations"] and "static_same" not in d
    assert all(c == round(c, T.DP) for pt in d["line"] for c in pt)                    # about a metre
    lean = tr.get("/api/cad/trains", f"have={d['static_at']}")[1]
    e = json.loads(lean)
    assert e["static_same"] is True and "line" not in e and "stations" not in e and e["trains"] == d["trains"]
    assert len(lean) < len(full) / 2 and len(lean) < 4000
    for other in (f"have={d['static_at'] - 1}", "have=x", "have="):                     # another week's, or nonsense: all of it
        assert json.loads(tr.get("/api/cad/trains", other)[1])["line"] == d["line"]


def test_the_static_feed_is_cached_and_a_restart_fetches_nothing(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.tick()
    assert (tmp_path / "trains" / "static.json").is_file()
    again, _, fetch2 = provider(tmp_path)
    assert again.model is not None and again.st["next"] == clock.t + STATIC_EVERY_S
    again.tick()
    assert fetch2.calls == []
    off = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, cache_dir=tmp_path / "trains", bbox=(-74.5, 42.0, -74.4, 42.1),
                 fetch=FakeFetch(), log=lambda m: None, clock=Clock())
    assert off.model is None                                                                 # another area, no station: fetch afresh


def test_failures_back_off_and_a_refusal_waits_and_names_the_key(tmp_path):
    logs = []
    tr, clock, fetch = provider(tmp_path)
    tr.log = logs.append
    tr.tick()
    fetch.fail[RT_URL] = 503
    tr.asked = clock.t
    assert tr.refresh_rt() is False and tr.rt["next"] == clock.t + BACKOFF_S[0] and "HTTP 503" in tr.rt["error"]
    assert tr.refresh_rt() is False and tr.rt["next"] == clock.t + 2 * BACKOFF_S[0]
    fetch.fail[RT_URL] = 403
    assert tr.refresh_rt() is False and tr.rt["next"] == clock.t + REFUSED_S and "SIZEUP_MTA_KEY" in tr.rt["error"]
    del fetch.fail[RT_URL]
    assert tr.refresh_rt() and tr.rt["error"] == "" and tr.rt["fails"] == 0
    assert any("next try" in m for m in logs)
    bad = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, fetch=lambda *a, **k: (200, {}, b"junk"), log=lambda m: None, clock=Clock())
    assert bad.refresh_static() is False and bad.model is None and "not loaded yet" in bad.why_not()
    assert bad.refresh_rt() is False


def test_a_key_goes_as_the_x_api_key_header_and_off_means_off(tmp_path):
    tr, clock, fetch = provider(tmp_path, key="KEY-0123456789")
    tr.refresh_rt()
    assert fetch.calls[-1] == (RT_URL, {"x-api-key": "KEY-0123456789"})
    off = Trains(enabled=False, fetch=FakeFetch(), log=lambda m: None)
    assert not off.configured and "off" in off.why_not() and off.start() is off and off._thread is None
    d = json.loads(off.get("/api/cad/trains", "")[1])
    assert d["configured"] is False and d["trains"] == [] and d["note"]
    assert not Trains(log=lambda m: None).configured                           # (conftest: no default addresses in tests)


def test_the_client_names_sizeup(monkeypatch):
    seen = {}

    class R:
        status, headers = 200, {}

        def read(self, n):
            return b"x"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout):
        seen.update(dict(req.header_items()))
        return R()
    monkeypatch.setattr(T, "urlopen", fake)
    T.http_get("https://mta.example/x", 5, 10, {"x-api-key": "k"})
    assert seen["User-agent"] == USER_AGENT and seen["X-api-key"] == "k"


# ---- config --------------------------------------------------------------------------------------------------------


def test_the_trains_section(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("")
    c = load(p)
    assert c.trains_enabled and c.trains_rt_url == "" and c.trains_key == "" and c.trains_bbox == T.DEFAULT_BBOX
    w, s, e, n = c.trains_bbox                                                  # #151: Mount Vernon East to Stamford
    assert w < -73.832 < -73.5428 < e and s < 40.912 < 41.0466 < n and w > -73.8557   # (not Wakefield: the Harlem Line's)
    p.write_text('[trains]\nenabled = false\nrt_url = "https://x.example/rt"\nbbox = [-73.8, 40.9, -73.5, 41.1]\n')
    c = load(p)
    assert not c.trains_enabled and c.trains_rt_url == "https://x.example/rt" and c.trains_bbox == (-73.8, 40.9, -73.5, 41.1)
    monkeypatch.setenv("SIZEUP_MTA_KEY", "abcdefgh1234")
    assert load(p).trains_key == "abcdefgh1234"
    for bad in ('enabled = "yes"', 'rt_url = "http://mta.example/rt"', 'bbox = [1, 2]', 'colour = 1'):
        p.write_text(f"[trains]\n{bad}\n")
        with pytest.raises(ConfigError):
            load(p)


# ---- the hub ---------------------------------------------------------------------------------------------------------


class FakeMta:
    def __init__(self):
        self.hits: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                body = RT if self.path == "/rt" else GTFS
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_the_hub_serves_trains_and_never_fetches_the_realtime_feed_unasked(tmp_path, monkeypatch):
    mta = FakeMta()
    monkeypatch.setattr(T, "WAIT_FIRST_S", 10.0)
    h = HubRun(hub_cfg(tmp_path, trains_rt_url=f"http://127.0.0.1:{mta.port}/rt", trains_gtfs_url=f"http://127.0.0.1:{mta.port}/gtfs.zip"))
    try:
        import time
        deadline = time.time() + 10
        while h.app.trains.model is None and time.time() < deadline:
            time.sleep(0.05)
        assert h.app.trains.model is not None and mta.hits == ["/gtfs.zip"]
        d = h.get("/api/cad/trains")                    # the first look waits for the feed it wakes
        assert d["configured"] and d["fetched"] and mta.hits.count("/rt") == 1
        assert [s["name"] for s in d["stations"]] == AREA
        n = h.get(f"/api/cad/trains/near?lat={PC[0]}&lon={PC[1]}")
        assert n["near"] and n["ref"]["name"] == "Port Chester"
        assert (tmp_path / "hub" / "trains" / "static.json").is_file()
    finally:
        h.close()
        mta.close()


def test_a_hub_with_trains_off_still_answers(tmp_path):
    h = HubRun(hub_cfg(tmp_path, trains_enabled=False))
    try:
        d = h.get("/api/cad/trains")
        assert d["configured"] is False and d["trains"] == [] and h.app.trains._thread is None
        assert h.get(f"/api/cad/trains/near?lat={PC[0]}&lon={PC[1]}")["near"] is False
    finally:
        h.close()


# ---- the page ---------------------------------------------------------------------------------------------------------


STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"


def test_the_trains_layer_and_panel_only_ever_ask_the_hub():
    js = (STATIC / "trains.js").read_text()
    html = (STATIC / "cad.html").read_text()
    assert '<script src="/static/trains.js"></script>' in html and 'id="d-trains"' in html
    assert html.index("/static/maplayers.js") < html.index("/static/trains.js")
    assert "MapLayers.addGroup" in js and "/api/cad/trains" in js and "/api/cad/trains/near" in js
    assert "mta.info" not in js.replace("www.mta.info/developers", "")     # the browser never talks to the MTA (a link to the terms is fine)
    assert "estimated" in js.lower() and "may not be real time" in js.lower() or "disclaimer" in js
    cadmap = (STATIC / "cadmap.js").read_text()
    assert "TrainLayer.panel(" in cadmap


def test_trains_are_drawn_as_trains_along_the_line():
    """#151: each train a train lying flat along its heading (a cab and body, filled or hollow), its number and
    destination beside it; cars only from the MTA's count, never made up; the poll names the line it holds."""
    js = (STATIC / "trains.js").read_text()
    assert "function trainIcon(filled, cars)" in js and "ensureIcon(this.map, t)" in js
    assert '"icon-rotation-alignment": "map"' in js and '"icon-pitch-alignment": "map"' in js and '["get", "bearing"]' in js
    assert '"icon-image": ["get", "icon"]' in js and "trainIcon(!t.estimated, t.cars)" in js
    assert re.search(r"cars:\s*\d", js) is None and "t.cars" in js                     # no car count of our own
    assert '"text-anchor": ["get", "anchor"]' in js and '["get", "towards"]' in js and "labelPlace(t.bearing, ...half(SCALE_FROM))" in js
    assert "?have=" in js and "static_same" in js
    assert "doesn't say how many cars" in js and "Length not given by the MTA" in js
    assert "localStorage" not in js


def test_a_train_without_the_mtas_count_looks_like_a_train_of_cars():
    """#164: not one long body but the look of a typical train, the same for every one, and the box says so."""
    js = (STATIC / "trains.js").read_text()
    assert "LOOK_CARS = 6" in js and "(cars | 0) || LOOK_CARS" in js
    assert "Length not given by the MTA (drawn as ${LOOK_CARS} cars for every train)" in js
    assert "PLAIN" not in js                                                         # no single-body train left


def test_a_train_on_the_line_is_its_cars_along_the_track_to_scale():
    """#164: on the line, each car an 85 ft piece of the track behind the train's front (to scale, bending with the
    track), filled when reported and an outline when estimated; off the line, the icon, also to scale when zoomed in."""
    js = (STATIC / "trains.js").read_text()
    assert "const M_PER_CAR = 25.9" in js and "NEAR_M = 100" in js
    assert "function onTrack(t, tr)" in js and "if (!best || best.d > NEAR_M) return null;" in js
    assert "piece(tr, Math.min(s1, s2), Math.max(s1, s2))" in js and "const back = Math.cos(segB - b) >= 0 ? -1 : 1;" in js
    assert 'filter: ["!", ["get", "onTrack"]]' in js and "map.getSource(SRC_CARS).setData(fc(cars));" in js
    assert '"line-color": ["case", ["get", "estimated"], warn, HALO]' in js and '"line-color": ["case", ["get", "estimated"], dark, warn]' in js
    assert 'const ICON_SIZE = ["interpolate", ["exponential", 2], ["zoom"], SCALE_FROM, MIN_SIZE, SCALE_TO, sizeAt(SCALE_TO)];' in js
    assert '"text-offset": ["interpolate", ["exponential", 2], ["zoom"],' in js                 # the label clears it as it grows
    assert "this.sig = \"\";                        // the cars lie along it" in js
