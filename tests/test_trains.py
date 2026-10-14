"""Metro-North trains (sizeup/trains.py, #131): the MTA's GTFS-realtime feed read by a small protobuf
reader, the static GTFS's line and stations, where each train is (reported, or estimated between
stations), the near-the-tracks check and "Trains approaching" for an event, the schedule (only while
someone looks), the cache and backoff, the config, and the hub's API.

Never the MTA itself (conftest.py turns the default addresses off and refuses any host but this one).
The fixtures in tests/fixtures/mta/ are recorded from the MTA's feeds on 2026-10-14 at 11:33 EDT and
trimmed by tools/record_trains_fixtures.py (the New Haven Line's trains; the line's shapes, one trip a
train number); tests/fixtures/mta/seq/, eleven reads in a row recorded at 16:23 EDT the same day (--seq)."""

import json
import math
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
    # Mount Vernon East to Stamford: those two by Rye and Port Chester, and five more out along the line
    assert set(trains) == {"1351", "1322", "1353", "1553", "1324", "1522", "1551"}
    # 1551, the express from New Haven: the MTA puts it 3.2 km off the line (a bogus point by Mount Vernon West, on
    # the Harlem Line): never drawn there; it left Stamford for Grand Central, so it is estimated on that run
    x = trains["1551"]
    assert x["estimated"] is True and x["mode"] == "estimated" and x["segment"]["from"] == "124" and x["segment"]["to"] == "1"
    assert T.metres(x["lat"], x["lon"], 40.941681, -73.835236) > 2000 and model.line.project(x["lat"], x["lon"])[0] < 5
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
    t.update(pos=None, vstatus="", vstop="")                              # a train the feed doesn't follow: its times alone
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
    # the MTA reports it beyond Port Chester already (and has it leaving Stamford): not approaching
    left = [dict(t["stops"][0], status="Departed"), t["stops"][1]]
    lat, lon, _ = model.line.at(st["Port Chester"]["ch"] - 400)
    t2 = dict(t, stops=left, pos={"lat": lat, "lon": lon, "bearing": None, "ts": NOW})
    assert model.approaching({"trains": {"1599": t2}}, *PC, NOW)["inbound"] == []
    # reported between Stamford and Port Chester: timed from there
    lat, lon, _ = model.line.at(st["Greenwich"]["ch"])
    t3 = dict(t, stops=left, pos={"lat": lat, "lon": lon, "bearing": None, "ts": NOW})
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
    assert RT_EVERY_S == 5.0 and tr.every_s == 5.0                                           # every 5 s while the layer is on
    for _ in range(3):
        clock.t += RT_EVERY_S
        tr.get("/api/cad/trains", "have=1")
        tr.tick()
    assert fetch.n(RT_URL) == 5 and tr.track.at == NOW                                       # the same feed time: nothing new
    clock.t += DEMAND_S + 1                                                                   # nobody asked since: idle
    tr.tick()
    assert fetch.n(RT_URL) == 5
    clock.t += 3600
    tr.tick()
    assert fetch.n(RT_URL) == 5
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
    clock.t += 7
    d = json.loads(tr.get("/api/cad/trains", "")[1])
    assert d["configured"] and d["note"] == "" and d["source"] == "MTA" and d["terms"].startswith("https://www.mta.info/")
    assert d["at"] == NOW and d["fetched"] == d["fetched_at"] == clock.t - 7 and d["static_at"] == clock.t - 7
    assert "may not be real time" in d["disclaimer"]
    assert d["age"] == 7 and d["stale"] is False and d["every_s"] == 5 and d["rejected"] == 1 and d["rejected_why"] == {"off the line": 1}
    assert d["profile"] == {"accel": T.ACCEL, "decel": T.DECEL, "vmax": T.VMAX, "predict_s": T.PREDICT_S, "stale_s": T.STALE_S}
    assert isinstance(d["points"], int) and d["points_seen"] >= d["points"]
    clock.t += T.STALE_S
    assert json.loads(tr.get("/api/cad/trains", "have=0")[1])["stale"] is True
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
    assert len(lean) < len(full) - 4000 and len(lean) < 1500 + 1000 * len(e["trains"])     # the line: 4 KB and more
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
    assert [tr.refresh_rt() or tr.rt["next"] - clock.t for _ in range(4)] == [40.0, 60.0, 60.0, 60.0]   # to a minute
    assert BACKOFF_S == (10.0, 60.0)
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
    assert c.trains_rt_every_s == 5.0
    p.write_text("[trains]\nrt_every_s = 15\n")
    c = load(p)
    assert c.trains_rt_every_s == 15.0 and Trains.from_config(c, log=lambda m: None).every_s == 15.0
    for bad in ('enabled = "yes"', 'rt_url = "http://mta.example/rt"', 'bbox = [1, 2]', 'colour = 1', "rt_every_s = 1",
                "rt_every_s = 600", 'rt_every_s = "5"', "rt_every_s = true"):
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
    assert "const POLL_MS = 5000," in js and "const BACKOFF_MS = [10000, 60000];" in js        # every 5 s; a failure: to a minute
    assert "Math.min(BACKOFF_MS[1], BACKOFF_MS[0] * 2 ** (store.fails - 1))" in js and "console.warn(`trains: " in js
    assert "doesn't say how many cars" in js and "Length not given by the MTA" in js
    assert "localStorage" not in js


def test_a_train_without_the_mtas_count_looks_like_a_train_of_cars():
    """#164: not one long body but the look of a typical train, the same for every one, and the box says so."""
    js = (STATIC / "trains.js").read_text()
    assert "LOOK_CARS = 6" in js and "(cars | 0) || LOOK_CARS" in js
    assert "Length not given by the MTA (drawn as ${LOOK_CARS} cars for every train)" in js
    assert "PLAIN" not in js                                                         # no single-body train left


def test_a_train_on_the_line_is_its_cars_along_the_track_to_scale():
    """#164: on the line, the cars (85 ft each) along the track behind the train's front (to scale, bending with the
    track), filled when reported and an outline when estimated; off the line, the icon, also to scale when zoomed in.
    2026-10-14 ("these trains look wonky and are overlapped"): each direction on its own track (the hub's lanes, at the
    train's s), one smooth body for the consist (no per-car pieces to kink at the joints), the cab's nose over its
    front, thin gaps across it at the cars' ends from street zoom, every outline under every body."""
    js = (STATIC / "trains.js").read_text()
    assert "const M_PER_CAR = 25.9" in js and "NEAR_M = 100" in js
    assert "function onTrack(t, line, lanes)" in js and "if (!best || best.d > NEAR_M) return null;" in js
    assert "piece(tr, Math.min(a, b), Math.max(a, b))" in js and "const back = Math.cos(segB - b) >= 0 ? -1 : 1;" in js
    assert "const lane = lanes && (t.d > 0 ? lanes.outbound : t.d < 0 ? lanes.inbound : null);" in js
    assert "return { tr, s: t.s, back: -Math.sign(t.d) };" in js and "if (coords[0].length > 2) at = coords.map((c) => c[2]);" in js
    assert 'const cars = [part(s, s + back * len, "body"), part(s, s + back * M_PER_CAR, "cab")];' in js
    assert 'filter: part("gap"), minzoom: GAP_ZOOM' in js and "GAP_ZOOM = 16" in js
    assert js.index('id: "tr-cab-case"') < js.index('id: "tr-car", type') < js.index('id: "tr-gap"')
    assert "d.lanes = had.lanes" in js and 'ln(this.lanes.outbound, "outbound"), ln(this.lanes.inbound, "inbound")' in js
    assert 'filter: ["!", ["get", "onTrack"]]' in js and "map.getSource(SRC_CARS).setData(fc(cars));" in js
    assert '"line-color": ["case", ["get", "estimated"], warn, HALO]' in js and '"line-color": ["case", ["get", "estimated"], dark, warn]' in js
    assert 'const ICON_SIZE = ["interpolate", ["exponential", 2], ["zoom"], SCALE_FROM, MIN_SIZE, SCALE_TO, sizeAt(SCALE_TO)];' in js
    assert '"text-offset": ["interpolate", ["exponential", 2], ["zoom"],' in js                 # the label clears it as it grows
    assert "this.sig = \"\";                        // the cars lie along it" in js


# ---- the vehicle's status, fixes across reads, the reporting points (smooth trains) -------------------------------
# tests/fixtures/mta/seq/: eleven reads of the live feed (2026-10-14 16:23:52 to 16:25:54 EDT, a read every 6 s, one
# file a feed time), trimmed to the New Haven Line's trains with a vehicle position (tools/record_trains_fixtures.py
# --seq). In it: 1473, stopped at Port Chester, is given the point by Stamford (12 km away) while arriving at Rye,
# then Rye; 1471 (and others) the point 3.2 km off the line by Mount Vernon West.

SEQ = sorted((FIX / "seq").glob("*.pb"))
STAMFORD_BOGUS = (41.047958, -73.539017)
OFF_LINE_BOGUS = (40.941681, -73.835236)


def replay(model, n=None, log=None, points=None):
    """The seq reads through one Tracker; [(feed, tracker state after it)]."""
    tk = T.Tracker(points, log)
    out = []
    for f in SEQ[:n]:
        feed = parse_feed(f.read_bytes())
        tk.update(model, feed, feed["timestamp"])
        out.append(feed)
    return tk, out


def test_the_vehicle_status_and_the_stop_it_means_are_read(feed):
    vp = lambda *extra: msg((1, msg((1, "T1"))), (2, msg((1, 40.99), (2, -73.68))), (5, NOW), (8, msg((1, "1350"), (2, "1350"))), *extra)
    body = lambda v: msg((1, msg((1, "2.0"), (3, NOW))), (2, msg((1, "b"), (4, v))))
    t = parse_feed(body(vp((4, 1), (7, "115"))))["trains"]["1350"]
    assert t["vstatus"] == "STOPPED_AT" and t["vstop"] == "115"
    assert parse_feed(body(vp((4, 0), (7, "114"))))["trains"]["1350"]["vstatus"] == "INCOMING_AT"
    assert parse_feed(body(vp((4, 2))))["trains"]["1350"]["vstatus"] == "IN_TRANSIT_TO"
    t = parse_feed(body(vp()))["trains"]["1350"]
    assert t["vstatus"] == "" and t["vstop"] == ""                               # not given: not guessed (proto's default)
    a, b = feed["trains"]["1553"], feed["trains"]["1551"]
    assert (a["vstatus"], a["vstop"]) == ("INCOMING_AT", "124") and (b["vstatus"], b["vstop"]) == ("IN_TRANSIT_TO", "1")
    assert feed["trains"]["1353"]["vstatus"] == "" and feed["trains"]["1353"]["pos"] is None
    assert {s["status"] for t in feed["trains"].values() for s in t["stops"]} >= {"On-Time", "Departed", "Arriving", "Arrived"}


def test_the_point_by_stamford_given_for_a_train_at_rye_is_refused_and_it_stays_where_it_was(model):
    logs = []
    tk, feeds = replay(model, log=logs.append)
    by = {f["timestamp"]: f for f in feeds}
    t0, t_bogus, t_rye = 1792009432, 1792009518, 1792009554
    assert by[t_bogus]["trains"]["1473"]["pos"]["lat"] == pytest.approx(STAMFORD_BOGUS[0])
    assert by[t_bogus]["trains"]["1473"]["vstatus"] == "INCOMING_AT" and by[t_bogus]["trains"]["1473"]["vstop"] == "114"
    tk2, _ = replay(model, n=SEQ.index(FIX / "seq" / f"{t_bogus}.pb") + 1)
    f = tk2.fix("1473")                                       # the bogus read: still at Port Chester, since the first read
    st = {s["name"]: s for s in model.area_stations}
    assert abs(f["s"] - st["Port Chester"]["ch"]) < 300 and f["fix_at"] == t0 and f["first_read"] and not f["ok"]
    [x] = [x for x in model.trains(by[t_bogus], t_bogus, tk2) if x["train"] == "1473"]
    assert x["mode"] == "reported" and T.metres(x["lat"], x["lon"], *STAMFORD_BOGUS) > 10_000
    assert T.metres(x["lat"], x["lon"], st["Port Chester"]["lat"], st["Port Chester"]["lon"]) < 300
    f = tk.fix("1473")                                        # then Rye, as it said
    assert abs(f["s"] - st["Rye"]["ch"]) < 100 and f["fix_at"] == t_rye and not f["first_read"] and f["ok"]
    # the point 3.2 km off the line: never a fix (1471 is drawn by its times until the MTA puts it on the line)
    tk1, (f0,) = replay(model, n=1)
    assert tk1.fix("1471") is None and f0["trains"]["1471"]["pos"]["lat"] == pytest.approx(OFF_LINE_BOGUS[0])
    for x in model.trains(f0, t0, tk1):
        assert T.metres(x["lat"], x["lon"], *OFF_LINE_BOGUS) > 2000 and model.line.project(x["lat"], x["lon"])[0] < 300
    # counted once each, however many reads they stay, and why; logged, not every time
    assert tk.why == {"off the line": 3, "behind the stop it left": 2, "backwards": 1} and tk.rejected == 6
    assert 1 <= len(logs) <= 3 and "refused" in logs[0] and "off the line" in logs[0]
    # never a reporting point: nothing learned 3 km off the line, and the Stamford point only from 1573, really there
    assert all(model.line.project(p["lat"], p["lon"])[0] < 300 for p in tk.points.pts)
    by_s = {round(p["s"]): p for p in tk.points.pts}
    assert by_s[53233]["n"] == 1 and by_s[53233]["in"] == 1


def test_fix_at_is_the_feed_time_a_position_first_appeared(model):
    tk, feeds = replay(model)
    first, last = feeds[0]["timestamp"], feeds[-1]["timestamp"]
    # 1540: stopped at Stamford (there at our first read: its age unknown), gone a read, on, a bogus step back, on
    seen = {}
    tk = T.Tracker()
    for f in feeds:
        tk.update(model, f, f["timestamp"])
        x = tk.fix("1540")
        seen[f["timestamp"]] = (round(x["s"]), x["fix_at"], x["first_read"])
    assert seen[first] == (52953, first, True)
    assert seen[1792009445] == seen[first]                                     # unchanged: its fix_at stays
    assert seen[1792009457] == seen[first]                                     # no position this read: the last one stands
    assert seen[1792009481] == (54075, 1792009481, False)                      # moved: stamped with this feed's time
    assert seen[1792009506] == seen[1792009481]                                # 947 m back: refused, it stays
    assert seen[1792009530][:2] == (55709, 1792009530)
    assert seen[last] == seen[1792009530]
    # the same read again (the MTA rebuilds about every 12 s, we read every 5 s): nothing changes
    assert tk.update(model, feeds[-1], last + 5) is False and tk.fix("1540")["fix_at"] == 1792009530
    # a fix gone or refused for longer than FIX_MAX_S: the train is estimated again
    gone = {"timestamp": last + T.FIX_MAX_S + 30, "trains": {k: dict(t, pos=None) for k, t in feeds[-1]["trains"].items()}}
    tk.update(model, gone, gone["timestamp"])
    assert tk.fix("1540") is None


def train_at(model, s, stops, vstatus="IN_TRANSIT_TO", vstop="", n="1350", pos=True):
    lat, lon, _ = model.line.at(s)
    return {"id": n, "train": n, "trip_id": "T" + n, "route_id": "3", "start_date": "", "delay": None,
            "pos": {"lat": round(lat, 6), "lon": round(lon, 6), "bearing": None, "ts": NOW} if pos else None,
            "vstatus": vstatus, "vstop": vstop,
            "stops": [{"stop_id": sid, "arr": t, "dep": t, "delay": 0, "track": "", "status": status} for sid, status, t in stops]}


def known_points(*ss):
    pts = T.Points()
    for s in ss:
        for k in range(T.POINT_KNOWN_N):
            pts.learn(0.0, 0.0, s, 1, NOW + k)
    return pts


def test_hold_s_is_the_next_known_reporting_point_never_past_the_next_stop(model):
    st = {s["name"]: s for s in model.area_stations}
    rye, pc, gw, stam = (st[k]["ch"] for k in ("Rye", "Port Chester", "Greenwich", "Stamford"))
    out = [(st["Rye"]["id"], "Departed", NOW - 60), (st["Port Chester"]["id"], "On-Time", NOW + 120)]
    inn = [(st["Port Chester"]["id"], "Departed", NOW - 60), (st["Rye"]["id"], "On-Time", NOW + 120)]
    s0 = rye + 700
    pts = known_points(rye + 300, s0 + 600, s0 + 1100, pc + 500)

    def place(t, points, now=NOW):
        a = model.anchors(t)
        tk = T.Tracker(points)
        tk.update(model, {"timestamp": now, "trains": {t["id"]: t}}, now)
        return model.place(t, a, model.direction(t, a), now, tk.fix(t["id"]), points)
    # outbound between Rye and Port Chester: the next point ahead
    p = place(train_at(model, s0, out, vstop=st["Port Chester"]["id"]), pts)
    assert p["mode"] == "reported" and p["hold_kind"] == "reporting_point" and p["hold_s"] == pytest.approx(s0 + 600, abs=0.1)
    assert p["next"]["name"] == "Port Chester" and p["prev_s"] == pytest.approx(rye, abs=0.1)
    assert p["predict_until"] == NOW + T.PREDICT_S and p["fix_at"] == NOW
    seg = p["segment"]
    assert (seg["from"], seg["to"], seg["t_dep"], seg["t_arr"]) == (st["Rye"]["id"], st["Port Chester"]["id"], NOW - 60, NOW + 120)
    # inbound from the same spot: the next point the other way
    p = place(train_at(model, s0, inn, vstop=st["Rye"]["id"], n="1351"), pts)
    assert p["hold_kind"] == "reporting_point" and p["hold_s"] == pytest.approx(rye + 300, abs=0.1)
    # the next point is past the next stop: the stop
    p = place(train_at(model, pc - 400, out, vstop=st["Port Chester"]["id"]), pts)
    assert p["hold_kind"] == "stop" and p["hold_s"] == pytest.approx(pc, abs=0.1)
    # a point seen once is not known yet: the fallback, 1500 m on (Greenwich to Stamford: no stop that near)
    once = T.Points()
    once.learn(0.0, 0.0, gw + 1500, 1, NOW)
    exp = [(st["Greenwich"]["id"], "Departed", NOW - 60), (st["Stamford"]["id"], "On-Time", NOW + 600)]
    p = place(train_at(model, gw + 1000, exp, vstop=st["Stamford"]["id"]), once)
    assert p["hold_kind"] == "fallback" and p["hold_s"] == pytest.approx(gw + 1000 + T.HOLD_FALLBACK_M, abs=0.1)
    assert stam - gw > 3000
    # nor a point more than POINT_AHEAD_M ahead
    p = place(train_at(model, gw + 1000, exp, vstop=st["Stamford"]["id"]), known_points(gw + 1000 + T.POINT_AHEAD_M + 100))
    assert p["hold_kind"] == "fallback"
    # the fallback, too, never past the stop
    p = place(train_at(model, stam - 900, exp, vstop=st["Stamford"]["id"]), T.Points())
    assert p["hold_kind"] == "stop" and p["hold_s"] == pytest.approx(stam, abs=0.1)
    # a fix already just past its stop's mark (the platform's end): it holds where it is, never backwards
    p = place(train_at(model, pc + 150, out, vstatus="INCOMING_AT", vstop=st["Port Chester"]["id"]), pts)
    assert p["hold_s"] == pytest.approx(pc + 150, abs=0.5) and p["hold_kind"] == "stop"
    # stopped at a station (STOPPED_AT): nowhere to go until the feed says it left; no run to carry it on
    p = place(train_at(model, pc - 100, out, vstatus="STOPPED_AT", vstop=st["Port Chester"]["id"]), pts)
    assert p["mode"] == "stopped" and p["hold_s"] == pytest.approx(pc - 100, abs=0.5) and p["segment"] is None


def test_the_recorded_trains_carry_what_the_page_needs_to_move_them(model, feed):
    trains = {t["train"]: t for t in model.trains(feed, NOW)}
    a = trains["1553"]                                       # reported, arriving at Stamford inbound
    assert a["mode"] == "reported" and a["d"] == -1 and a["vehicle_status"] == "INCOMING_AT" and a["vehicle_stop_id"] == "124"
    assert a["next"]["name"] == "Stamford" and a["hold_s"] == a["next"]["s"] and a["hold_kind"] == "stop"
    assert a["fix_at"] == NOW and a["fix_age_unknown"] is True and a["segment"]["to"] == "124"
    assert isinstance(a["lag_s"], float)
    for t in trains.values():
        assert t["mode"] in ("reported", "estimated", "stopped") and t["estimated"] == (t["fix_at"] is None)
        assert (t["hold_s"] - t["s"]) * t["d"] >= 0                                       # never backwards
        if t["next"] is not None and t["mode"] != "stopped":
            assert (t["next"]["s"] - t["hold_s"]) * t["d"] >= -0.1 or (t["next"]["s"] - t["s"]) * t["d"] < 0   # not past the stop
        if t["segment"]:
            assert set(t["segment"]) == {"from", "to", "s0", "s1", "t_dep", "t_arr", "accel", "decel", "vmax"}
        if t["mode"] == "estimated":                         # where the hub draws it: the run itself
            assert t["s"] == pytest.approx(T.profile_s(NOW, t["segment"])[0], abs=0.1)


def test_a_train_the_feed_follows_leaves_a_stop_only_when_the_feed_says_so(model):
    st = {s["name"]: s for s in model.area_stations}
    stops = [(st["Mamaroneck"]["id"], "Departed", NOW - 400), (st["Rye"]["id"], "On-Time", NOW - 200),
             (st["Port Chester"]["id"], "On-Time", NOW + 100)]
    t = train_at(model, 0, stops, vstatus="", pos=False)
    t["stops"][0]["status"] = "Departed"
    [x] = model.trains({"timestamp": NOW, "trains": {"1350": t}}, NOW)
    # by its times it left Rye 200 s ago, but the MTA hasn't said so: at Rye, not on the way
    assert x["mode"] == "stopped" and x["s"] == pytest.approx(st["Rye"]["ch"], abs=0.1) and x["segment"]["to"] == st["Rye"]["id"]
    t["stops"][1]["status"] = "Departed"                     # now it has: on its run to Port Chester, as timed
    [x] = model.trains({"timestamp": NOW, "trains": {"1350": t}}, NOW)
    seg = x["segment"]
    assert x["mode"] == "estimated" and (seg["from"], seg["to"]) == (st["Rye"]["id"], st["Port Chester"]["id"])
    assert st["Rye"]["ch"] < x["s"] < st["Port Chester"]["ch"] and x["hold_s"] == pytest.approx(st["Port Chester"]["ch"], abs=0.1)
    t["stops"][2]["status"] = "Arrived"                      # the feed says it is there, ahead of its time
    [x] = model.trains({"timestamp": NOW, "trains": {"1350": t}}, NOW)
    assert x["mode"] == "stopped" and x["s"] == pytest.approx(st["Port Chester"]["ch"], abs=0.1)


def test_reporting_points_are_learned_one_per_place_and_known_once_seen_again(model):
    pts = T.Points()
    pts.learn(41.0, -73.66, 41_101.0, -1, NOW)
    pts.learn(41.0, -73.66, 41_110.0, 1, NOW + 300)             # within CLUSTER_M: the same point, the other way
    pts.learn(41.0, -73.65, 41_101.0 + T.CLUSTER_M + 30, 1, NOW + 400)
    assert len(pts) == 2 and pts.known == 1 and pts.dirty
    p = pts.pts[0]
    assert p["n"] == 2 and p["in"] == 1 and p["out"] == 1 and p["first"] == NOW and p["last"] == NOW + 300 and p["s"] == 41_101.0
    assert pts.ahead(40_000, 1) == 41_101.0 and pts.ahead(41_101, 1) is None and pts.ahead(42_000, -1) == 41_101.0
    # bogus positions teach nothing: the Stamford point for a train between Port Chester and Rye, the point off the line
    st = {s["name"]: s for s in model.area_stations}
    t = train_at(model, st["Port Chester"]["ch"] + 170, [(st["Greenwich"]["id"], "Departed", NOW - 300),
                 (st["Port Chester"]["id"], "Arrived", NOW - 10), (st["Rye"]["id"], "On-Time", NOW + 200)],
                 vstatus="STOPPED_AT", vstop=st["Port Chester"]["id"], n="1371")
    tk = T.Tracker()
    tk.update(model, {"timestamp": NOW, "trains": {"1371": t}}, NOW)
    for k, (lat, lon) in enumerate((STAMFORD_BOGUS, OFF_LINE_BOGUS)):
        bad = dict(t, pos={"lat": lat, "lon": lon, "bearing": None, "ts": NOW}, vstatus="INCOMING_AT", vstop=st["Rye"]["id"])
        tk.update(model, {"timestamp": NOW + 12 * (k + 1), "trains": {"1371": bad}}, NOW + 12 * (k + 1))
    assert len(tk.points) == 0 and tk.rejected == 2 and set(tk.why) == {"behind the stop it left", "off the line"}
    # an 11.6 km jump in a couple of minutes, all within the stretch its stops allow: too fast
    far = train_at(model, st["Stamford"]["ch"] - 200, [(st["Stamford"]["id"], "Departed", NOW - 60), (st["Rye"]["id"], "On-Time", NOW + 900)],
                   vstatus="", n="1371")
    tk = T.Tracker()
    tk.update(model, {"timestamp": NOW, "trains": {"1371": far}}, NOW)
    assert tk.fix("1371") is not None
    lat, lon, _ = model.line.at(st["Port Chester"]["ch"] + 170)
    tk.update(model, {"timestamp": NOW + 133, "trains": {"1371": dict(far, pos={"lat": lat, "lon": lon, "bearing": None, "ts": NOW})}}, NOW + 133)
    assert tk.why == {"too fast": 1}


def test_reporting_points_survive_a_restart_and_start_from_the_seed(tmp_path):
    seed = tmp_path / "seed.json"
    pts = T.Points()
    pts.learn(41.000123, -73.665456, 41_000.0, -1, NOW)
    pts.learn(41.000123, -73.665456, 41_000.0, 1, NOW + 60)
    seed.write_text(json.dumps(pts.doc()))
    clock, fetch = Clock(), FakeFetch()
    tr = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, cache_dir=tmp_path / "trains", fetch=fetch, log=lambda m: None, clock=clock, seed=seed)
    tr.tick()                                                  # the line: each point's s on it
    assert len(tr.points) == 1 and tr.points.known == 1 and tr.points.pts[0]["s"] != 41_000.0
    tr.points.learn(40.95, -73.75, 30_000.0, 1, NOW + 100)
    tr.stop()                                                  # saved (also every POINTS_SAVE_S while reading)
    doc = json.loads((tmp_path / "trains" / "points.json").read_text())
    assert len(doc["points"]) == 2 and {p["n"] for p in doc["points"]} == {2, 1}
    again = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, cache_dir=tmp_path / "trains", fetch=FakeFetch(), log=lambda m: None,
                   clock=Clock(), seed=seed)
    assert len(again.points) == 2 and again.points.known == 1 and not again.points.dirty     # the seed's not counted twice
    assert again.status()["points"] == 1 and again.status()["points_seen"] == 2
    shipped = json.loads(T.SEED_POINTS.read_text())             # data/: what we learned from the recorded feeds
    assert sum(1 for p in shipped["points"] if p["n"] >= T.POINT_KNOWN_N) >= 20
    m = Model(parse_static(GTFS))
    assert all(m.line.project(p["lat"], p["lon"])[0] < T.MAIN_M for p in shipped["points"])


# ---- the run between two stops (profile_s): the page ports it ------------------------------------------------------


def run(s0=0.0, s1=2300.0, t_dep=0.0, t_arr=180.0, **kw):
    return T.segment(s0, s1, t_dep, t_arr) | kw


def test_a_run_leaves_slowly_cruises_and_stops_at_the_next_station_on_time():
    seg = run()
    ts = [i * 0.5 for i in range(-10, 400)]
    xs = [T.profile_s(t, seg) for t in ts]
    assert T.profile_s(0, seg) == (0.0, 0.0) and T.profile_s(-30, seg) == (0.0, 0.0)
    assert T.profile_s(180, seg)[0] == pytest.approx(2300, abs=1e-6) and T.profile_s(180, seg)[1] == pytest.approx(0, abs=1e-6)
    assert T.profile_s(500, seg) == (2300.0, 0.0)
    assert all(b[0] >= a[0] for a, b in zip(xs, xs[1:]))                       # never backwards
    assert max(v for _, v in xs) <= T.VMAX
    # slow away and slow in: a tenth of the time in, well under a tenth of the way; the middle at speed
    assert T.profile_s(18, seg)[0] < 0.05 * 2300 and 2300 - T.profile_s(162, seg)[0] < 0.06 * 2300
    D, _, a, b, v, ta, tc, tb = T._shape(seg)
    assert ta + tc + tb == pytest.approx(180) and tc > 0 and v == pytest.approx(T.profile_s(90, seg)[1])
    assert T.profile_s(ta / 2, seg)[1] == pytest.approx(a * ta / 2) and T.profile_s(180 - tb / 2, seg)[1] == pytest.approx(b * tb / 2)
    # inbound (s falling) the same, mirrored
    back = run(2300.0, 0.0)
    assert T.profile_s(60, back)[0] == pytest.approx(2300 - T.profile_s(60, seg)[0]) and T.profile_s(180, back)[0] == pytest.approx(0, abs=1e-6)
    # profile_t, the inverse: when it reaches a point
    for s in (0, 1, 50, 700, 1600, 2299, 2300):
        assert T.profile_s(T.profile_t(s, seg), seg)[0] == pytest.approx(s, abs=1e-6)


def test_a_short_time_runs_a_triangle_and_an_impossible_one_arrives_late_never_past_the_stop():
    tri_T = 2 * math.sqrt(1500 * (1 / (2 * T.ACCEL) + 1 / (2 * T.DECEL)))   # just time to reach speed and brake
    seg = run(s1=1500.0, t_arr=tri_T)
    D, _, a, b, v, ta, tc, tb = T._shape(seg)
    assert tc == pytest.approx(0, abs=0.5) and v < T.VMAX and T.profile_s(seg["t_arr"], seg)[0] == pytest.approx(1500, abs=0.01)
    late = run(s1=1500.0, t_arr=40.0)                          # 1.5 km in 40 s: can't be done from a stand
    D, _, a, b, v, ta, tc, tb = T._shape(late)
    assert ta + tc + tb > 40 and tc == 0 and v == pytest.approx(math.sqrt(1500 / (1 / (2 * a) + 1 / (2 * b)))) and v < T.VMAX
    assert T.profile_s(40, late)[0] < 1500 and all(T.profile_s(t, late)[0] <= 1500 for t in range(0, 400))
    assert T.profile_s(ta + tc + tb, late) == (1500.0, 0.0)
    fast = run(s1=60_000.0, t_arr=1500.0)                      # 60 km in 25 min needs more than vmax: vmax, late
    D, _, a, b, v, ta, tc, tb = T._shape(fast)
    assert v == T.VMAX and tc > 0 and ta + tc + tb > 1500
    for bad in (run(t_arr=0.0), run(t_arr=-10.0)):             # no time at all: the quickest run, not a jump
        assert 0 < T.profile_s(30, bad)[0] < 2300
    assert T.profile_s(10, run(s1=0.0)) == (0.0, 0.0)          # nowhere to go


def test_an_express_keeps_cruising_past_the_stations_it_doesnt_stop_at(model):
    st = {s["name"]: s for s in model.area_stations}
    stam, harr = st["Stamford"]["ch"], st["Harrison"]["ch"]
    seg = run(stam, harr, NOW, NOW + 1200)
    t_pc = T.profile_t(st["Port Chester"]["ch"], seg)
    s, v = T.profile_s(t_pc, seg)
    D, _, a, b, cruise, ta, tc, tb = T._shape(seg)
    assert v == pytest.approx(cruise) and NOW + ta < t_pc < NOW + ta + tc            # at speed through Port Chester
    for name in ("Greenwich", "Port Chester", "Rye"):
        assert T.profile_s(T.profile_t(st[name]["ch"], seg), seg)[1] == pytest.approx(cruise)


# ---- each direction on its own track (data/mnr_tracks.json) ---------------------------------------------------------


@pytest.fixture(scope="module")
def tracks():
    return json.loads(T.TRACKS_FILE.read_text())


def side(model, lat, lon):
    """Metres left (+) or right (-) of the line, going outbound (toward New Haven)."""
    _, ch = model.line.project(lat, lon)
    la, lo, b = model.line.at(ch)
    return -math.cos(math.radians(b)) * (lon - lo) * T.MX + math.sin(math.radians(b)) * (lat - la) * T.MY


def test_the_mta_stops_inbound_trains_on_track_3_and_outbound_on_4_here(model, feed, tracks):
    """Why the lanes are tracks 3 and 4 (tools/fetch_train_tracks.py): the MTA's own track at our stations."""
    from collections import Counter
    area = {s["id"] for s in model.area_stations if s["name"] != "Stamford"}
    seen = {"inbound": Counter(), "outbound": Counter()}
    for t in feed["trains"].values():
        if t["route_id"] in model.ours:
            d = model.direction(t, model.anchors(t))
            seen[T.DIRS[d][0]].update(s["track"] for s in t["stops"] if s["stop_id"] in area and s["track"])
    assert set(seen["inbound"]) == {"3"} and seen["inbound"]["3"] > 100
    assert seen["outbound"].most_common(1)[0][0] == "4" and seen["outbound"]["4"] > 0.95 * sum(seen["outbound"].values())
    assert tracks["lanes"]["inbound"]["track"] == "3" and tracks["lanes"]["outbound"]["track"] == "4"
    assert "OpenStreetMap" in tracks["license"] and "ODbL" in tracks["license"]


def test_each_direction_has_its_own_track_on_its_side_of_the_line(static, tracks):
    m = Model(static, tracks=tracks)
    assert set(m.lanes) == {1, -1}
    lo, hi = m.area
    for s in range(int(lo), int(hi), 250):
        (a_lat, a_lon), (b_lat, b_lon) = m.where(1, s), m.where(-1, s)
        out, inn = side(m, a_lat, a_lon), side(m, b_lat, b_lon)
        assert out < -2 and inn > 0.5, s                  # outbound on the right (south, track 4), inbound on the left (3)
        assert 6 < T.metres(a_lat, a_lon, b_lat, b_lon) < 25, s
        assert abs(m.line.project(a_lat, a_lon)[1] - s) < 3 and abs(m.line.project(b_lat, b_lon)[1] - s) < 3
    lat, lon = m.where(1, 5_000)                         # outside the lanes (Manhattan): the line itself
    assert m.line.project(lat, lon)[0] < 0.5
    assert Model(static).lanes == {} and Model(static, tracks={"lanes": {}}).lanes == {}
    shifted = json.loads(json.dumps(tracks))             # measured on another line (s off by 200 m): not used
    for lane in shifted["lanes"].values():
        for c in lane["coords"]:
            c[2] += 200
    assert Model(static, tracks=shifted).lanes == {}


def test_trains_are_drawn_on_their_own_track_and_once(static, feed, tracks):
    m = Model(static, tracks=tracks)
    trains = m.trains(feed, NOW)
    for x in trains:                                     # on its lane, at its s
        lat, lon = m.where(x["d"], x["s"])
        assert T.metres(x["lat"], x["lon"], lat, lon) < 1.5 and (side(m, x["lat"], x["lon"]) < 0) == (x["d"] > 0)
    # the same train twice in the feed (a trip update without its vehicle, and the vehicle): drawn once, as reported
    a = json.loads(json.dumps(feed["trains"]["1322"]))
    b = dict(json.loads(json.dumps(a)), id="other", trip_id="other", pos=None, vstatus="", vstop="")
    for order in ([a, b], [b, a]):
        got = [x for x in m.trains({"timestamp": NOW, "trains": {t["id"]: t for t in order}}, NOW) if x["train"] == "1322"]
        assert len(got) == 1 and got[0]["mode"] == "reported" and got[0]["id"] == "1322"


def test_the_layer_sends_the_lanes_with_the_line(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    tr.get("/api/cad/trains", "")
    tr.tick()
    d = json.loads(tr.get("/api/cad/trains", "")[1])
    assert set(d["lanes"]) == {"outbound", "inbound"} and all(len(v) > 50 for v in d["lanes"].values())
    lo, hi = tr.model.area
    for v in d["lanes"].values():
        assert all(lo - 1501 <= s <= hi + 1501 for _, _, s in v) and [s for _, _, s in v] == sorted(s for _, _, s in v)
    assert "lanes" not in json.loads(tr.get("/api/cad/trains", f"have={d['static_at']}")[1])
    off = Trains(rt_url=RT_URL, gtfs_url=GTFS_URL, fetch=FakeFetch(), log=lambda m: None, clock=Clock(), tracks=tmp_path / "none.json")
    off.tick()
    assert off.model.lanes == {} and json.loads(off.get("/api/cad/trains", "")[1])["lanes"] == {}
