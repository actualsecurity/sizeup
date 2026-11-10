"""Metro-North's numbered tracks, catenary poles and mileposts (GitHub #371, sizeup/railassets.py): the train on its own
numbered track from the stops' tracks, "near pole N · MP x.x" for a train or an incident by the line, the layer.

Synthetic geometry: a straight line east along 41.0 N with four parallel tracks (3, 1, 2, 4 from north to south, 4 m
apart, as on the New Haven Line here), a catenary structure every 100 m and a milepost every tenth of a mile. Real
shapes: tests/fixtures/mta/railassets/ (a few features of each layer by Port Chester, from Metro-North's public GIS
service, 2026-11-09), measured against the recorded static feed's line.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from sizeup import railassets as R
from sizeup import trains as T
from sizeup.trains import Model, Trains

FIX = Path(__file__).parent / "fixtures" / "mta"
LAT, LON0 = 41.0, -73.70
DLAT = 1 / 110_574.0                       # a metre north
DLON = 1 / T.MX                            # a metre east
OFF = {"3": 6.0, "1": 2.0, "2": -2.0, "4": -6.0}   # metres north of the line
LEN_M = 8000.0
A_CH, B_CH = 2000.0, 6000.0                # two stations: Rye (2RY) and Port Chester (2PC), as far along
MP0_CH, MP0 = 1000.0, 20.0                 # MP 20.0 is 1000 m along; a tenth every 160.9 m
NOW = 1_800_000_000


def east(m: float) -> float:
    return LON0 + m * DLON


def static() -> dict:
    line = [[round(east(x), 7), LAT] for x in range(0, int(LEN_M) + 1, 500)]
    st = {"A": {"id": "A", "name": "Rye", "code": "2RY", "lat": LAT, "lon": east(A_CH), "ch": A_CH, "url": "", "tc": 0.0},
          "B": {"id": "B", "name": "Port Chester", "code": "2PC", "lat": LAT, "lon": east(B_CH), "ch": B_CH, "url": "", "tc": 200.0}}
    return {"line": line, "stations": st, "stops": {"A": "Rye", "B": "Port Chester"}, "routes": {}}


def gj(geom_type, coords, **props) -> dict:
    return {"type": "Feature", "geometry": {"type": geom_type, "coordinates": coords}, "properties": props}


def layers() -> dict:
    """The four layers as the service answers them (GeoJSON features), synthetic."""
    tracks = [gj("LineString", [[east(x), LAT + OFF[n] * DLAT] for x in range(0, int(LEN_M) + 1, 250)],
                 LineCode="NHL", TrkName=n, TrkType="Main Line") for n in ("3", "1", "2", "4")]
    tracks.append(gj("LineString", [[east(100), LAT + 30 * DLAT], [east(300), LAT + 30 * DLAT]],
                     LineCode="NHL", TrkName="5 (Cos Cob Yard) Siding", TrkType="Siding"))     # not a numbered track
    poles = [gj("LineString", [[east(x), LAT + 12 * DLAT], [east(x), LAT - 12 * DLAT]], LineCode="NHL", CB_ID=f"CB-{200 + i}",
                CB_ID2=f"0{200 + i}") for i, x in enumerate(range(0, int(LEN_M) + 1, 100))]
    poles.append(gj("LineString", [[east(50), LAT + 12 * DLAT], [east(50), LAT - 12 * DLAT]], LineCode="NHL", CB_ID="CB-243A"))
    whole = [gj("Point", [east(MP0_CH + k * 1609.344), LAT + 20 * DLAT], PointName=str(int(MP0) + k), LineCode="NHL") for k in range(5)]
    tenths = [gj("Point", [east(MP0_CH + k * 160.9344), LAT + 20 * DLAT], Name=f"{MP0 + k / 10:.1f}", Label=f".{k % 10}",
                 LineCode="NHL") for k in range(1, 40) if k % 10]
    return {"tracks": tracks, "poles": poles, "mileposts": whole, "tenths": tenths}


BBOX = (LON0 - 0.01, LAT - 0.01, east(LEN_M) + 0.01, LAT + 0.01)


@pytest.fixture(scope="module")
def doc():
    return R.parse(layers(), BBOX)


@pytest.fixture(scope="module")
def model(doc):
    return Model(static(), BBOX, None, doc)


# ---- the layers read ------------------------------------------------------------------------------------------------


def test_the_layers_read_into_tracks_poles_and_mileposts(doc):
    assert list(doc["tracks"]) == ["1", "2", "3", "4"]                  # numbered main tracks only (no siding)
    assert all(len(runs) == 1 and len(runs[0]) == 33 for runs in doc["tracks"].values())
    ids = [p["id"] for p in doc["poles"]]
    assert ids[:2] == ["200", "201"] and "243A" in ids and len(ids) == 82
    p = doc["poles"][0]
    assert p["lat"] == pytest.approx(LAT, abs=1e-7) and p["a"][1] > LAT > p["b"][1]   # the portal's middle, its two ends
    mps = [m["mp"] for m in doc["mileposts"]]
    assert mps == sorted(mps) and mps[0] == 20.0 and mps[-1] == 24.0 and 20.1 in mps and len(mps) == 41


def test_what_lies_outside_the_area_or_in_another_shape_is_left_out():
    lay = layers()
    lay["poles"] += [gj("LineString", [[-70.0, 42.0], [-70.0, 42.001]], CB_ID="CB-999"),        # far away
                     gj("Point", [east(10), LAT], CB_ID="CB-998"), {"geometry": None}, None,      # not a line, nothing
                     gj("LineString", [[east(10), LAT], [east(10), LAT + DLAT]], CB_ID="no number")]
    lay["tenths"] += [gj("Point", [east(10), LAT], Name="x.y"), gj("Point", "garbage", Name="20.5")]
    lay["tracks"] += [gj("MultiLineString", [[[east(0), LAT + 10 * DLAT], [east(9000), LAT + 10 * DLAT]],
                                             [[-70.0, 42.0], [-70.0, 42.1]]], TrkName="07")]   # one part in, one out
    d = R.parse(lay, BBOX)
    ids = {p["id"] for p in d["poles"]}
    assert not ids & {"999", "998"} and len(d["poles"]) == 82
    assert len(d["mileposts"]) == 41 and list(d["tracks"]) == ["1", "2", "3", "4", "7"] and len(d["tracks"]["7"]) == 1
    assert R.parse({}, BBOX) == {"tracks": {}, "poles": [], "mileposts": []}


def test_the_real_layers_by_port_chester_fit_the_mtas_line():
    """The service's own shapes (a few of each by Port Chester): its four main tracks lie 3, 1, 2, 4 from north to south
    along the static feed's line, the tenth mileposts 0.1 mile apart along it, and Port Chester is about MP 25.5."""
    lay = {k: json.loads((FIX / "railassets" / f"{k}.geojson").read_text())["features"] for k in ("tracks", "poles", "mileposts", "tenths")}
    d = R.parse(lay, T.DEFAULT_BBOX)
    m = Model(T.parse_static((FIX / "gtfsmnr.zip").read_bytes()), T.DEFAULT_BBOX, None, d)
    assert sorted(m.track_lanes) == ["1", "2", "3", "4"]
    s = m.by_code["2PC"]                                                # Port Chester
    north = sorted(m.track_lanes, key=lambda k: -m.track_lanes[k].at(s)[0])
    assert north == ["3", "1", "2", "4"]
    gaps = [b - a for (a, _), (b, _) in zip(m.ref.mps, m.ref.mps[1:])]
    assert len(m.ref.mps) >= 10 and all(150 < g < 172 for g in gaps)  # a tenth of a mile is 160.9 m
    assert m.ref.mp_at(s) == pytest.approx(25.5, abs=0.15)
    assert m.ref.poles and all(R.POLE.fullmatch(i) for _, i in m.ref.poles)
    pole_s, pole = m.ref.poles[0]
    assert m.ref.pole_at(pole_s + 10) == (pole, pytest.approx(10, abs=0.01))


# ---- the train on its numbered track ----------------------------------------------------------------------------


def stop(code_or_id, track="", status="", posted=True):
    return {"code": code_or_id, "name": code_or_id, "track": track, "posted": posted, "status": status}


def anchors(ta="", tb=""):
    return [({"stop_id": "A", "track": ta, "status": "Departed"}, A_CH), ({"stop_id": "B", "track": tb, "status": ""}, B_CH)]


def test_the_track_comes_from_the_stops_either_side(model):
    a = anchors()
    t = {"tt": {"prev": stop("2RY", "2"), "next": stop("2PC", "2")}}
    assert model.which_track(t, a, 0, 1, 3000) == ("2", "posted")
    # left on 1 (planned), going to 4 (posted): the nearer station's
    t = {"tt": {"prev": stop("2RY", "1", posted=False), "next": stop("2PC", "4")}}
    assert model.which_track(t, a, 0, 1, 2500) == ("1", "planned")
    assert model.which_track(t, a, 0, 1, 5500) == ("4", "posted")
    # only the next stop's known; leading zeros as the MTA may write them
    assert model.which_track({"tt": {"prev": None, "next": stop("2PC", "03")}}, a, 0, 1, 2500) == ("3", "posted")
    # no TrainTime: GTFS-realtime's track at the stops
    assert model.which_track({}, anchors("4", "4"), 0, 1, 3000) == ("4", "mta")
    # a track we have no line for (Stamford's 5), and no position: none
    assert model.which_track({"tt": {"next": stop("2PC", "5")}}, a, 0, 1, 3000) == ("", "")


def test_with_no_stop_track_a_gps_fix_takes_the_nearest_track(model):
    s = 3000
    lat2 = LAT + OFF["2"] * DLAT + 0.8 * DLAT                           # GPS by track 2
    t = {"tt": {"next": stop("2PC", "")}, "pos": {"src": "traintime", "lat": lat2, "lon": east(s)}}
    assert model.which_track(t, anchors(), 0, 1, s) == ("2", "nearest")
    far = dict(t, pos={"src": "traintime", "lat": LAT + 60 * DLAT, "lon": east(s)})
    assert model.which_track(far, anchors(), 0, 1, s) == ("", "")      # too far from any track: not a guess
    gtfs = dict(t, pos={"src": "gtfs", "lat": lat2, "lon": east(s)})  # a reporting point, not a GPS fix: no
    assert model.which_track(gtfs, anchors(), 0, 1, s) == ("", "")


def test_a_train_is_drawn_on_its_numbered_track_with_its_pole_and_milepost(model):
    feed = {"timestamp": NOW, "trains": {"1234": {
        "id": "1234", "train": "1234", "trip_id": "x", "route_id": "", "start_date": "", "delay": None, "vstatus": "IN_TRANSIT_TO",
        "vstop": "B", "pos": {"lat": LAT, "lon": east(4000), "ts": NOW},
        "stops": [{"stop_id": "A", "arr": NOW - 200, "dep": NOW - 180, "delay": 0, "track": "2", "status": "Departed"},
                  {"stop_id": "B", "arr": NOW + 200, "dep": NOW + 220, "delay": 0, "track": "2", "status": ""}]}}}
    (x,) = model.trains(feed, NOW)
    assert x["trk"] == "2" and x["trk_src"] == "mta" and x["d"] == 1
    assert x["lat"] == pytest.approx(LAT + OFF["2"] * DLAT, abs=2e-6)   # on track 2, not the line
    assert x["pole"] == str(200 + round(x["s"] / 100)) and x["mp"] == pytest.approx(MP0 + (x["s"] - MP0_CH) / T.MILE_M, abs=0.051)
    for st in feed["trains"]["1234"]["stops"]:                        # no track known: its direction's (here: the line)
        st["track"] = ""
    (y,) = model.trains(feed, NOW)
    assert "trk" not in y and abs(y["lat"] - LAT) < 1e-6 and "pole" in y


# ---- the nearest pole and milepost ------------------------------------------------------------------------------


def test_the_nearest_pole_and_milepost_of_a_point_by_the_line(model):
    ref = model.ref
    at = ref.at(LAT + 40 * DLAT, east(2520))                           # 40 m north of the line, 2520 m along
    assert at == {"pole": "225", "mp": pytest.approx(20.9, abs=0.05), "m": 40}
    assert R.label(at) == f"near pole 225 · MP {at['mp']:.1f}"
    assert ref.at(LAT + 200 * DLAT, east(2520)) == {}                  # 200 m off: none
    assert ref.mp_at(MP0_CH + 3 * 160.9344) == 20.3
    assert ref.mp_at(-1000) is None and ref.pole_at(LEN_M + 500) is None
    near = model.near(LAT + 60 * DLAT, east(3300))                     # an incident by the tracks
    assert near["kind"] == "track" and near["pole"] == "233" and near["mp"] == pytest.approx(21.4, abs=0.05)
    st = model.near(LAT + 30 * DLAT, east(B_CH + 20))                  # by Port Chester station: its pole and milepost too
    assert st["kind"] == "station" and st["pole"] == "260" and "mp" in st


def test_a_stray_milepost_is_left_out(doc):
    d = json.loads(json.dumps(doc))
    d["mileposts"].append({"mp": 99.9, "lon": east(1500), "lat": LAT})  # out of order along the line
    m = Model(static(), BBOX, None, d)
    assert 99.9 not in [mp for _, mp in m.ref.mps] and m.ref.mp_at(1500) == pytest.approx(20.3, abs=0.05)


# ---- no data: as before ---------------------------------------------------------------------------------------------


def test_with_no_assets_trains_are_drawn_as_before_and_nothing_says_a_pole():
    m = Model(static(), BBOX)
    assert m.track_lanes == {} and not m.ref and m.ref.at(LAT, east(3000)) == {}
    assert m.which_track({"tt": {"next": stop("2PC", "2")}}, anchors(), 0, 1, 3000) == ("", "")
    assert m.where(1, 3000, "2") == pytest.approx(m.line.at(3000)[:2])
    assert "pole" not in m.near(LAT + 30 * DLAT, east(3000)) and R.label({}) == ""
    assert R.RailRef(None, None).at(LAT, LON0) == {} and R.RailRef({"tracks": {"1": []}}, m.line).tracks == {}
    shifted = R.parse(layers(), BBOX)                                  # tracks 300 m off this line: not this line's
    for runs in shifted["tracks"].values():
        for run in runs:
            for p in run:
                p[1] += 300 * DLAT
    assert Model(static(), BBOX, None, shifted).track_lanes == {}


# ---- the provider: fetched, kept, served ---------------------------------------------------------------------------


class Fetch:
    def __init__(self, lay=None, fail=0, page=None):
        self.lay, self.fail, self.page, self.calls = lay or layers(), fail, page, []

    def __call__(self, url, timeout, limit, headers=None):
        self.calls.append((url, dict(headers or {})))
        if self.fail:
            from urllib.error import HTTPError
            raise HTTPError(url, self.fail, "nope", None, None)
        q = parse_qs(urlsplit(url).query)
        layer = int(urlsplit(url).path.split("/")[-2])
        key = {v[0]: k for k, v in R.LAYERS.items()}[layer]
        feats, off = self.lay[key], int(q["resultOffset"][0])
        n = self.page or len(feats) or 1
        body = {"type": "FeatureCollection", "features": feats[off:off + n]}
        if off + n < len(feats):
            body["exceededTransferLimit"] = True
        return 200, {}, json.dumps(body).encode()


def test_the_provider_reads_each_layer_in_our_area_pages_and_keeps_a_copy(tmp_path):
    f, logs = Fetch(page=30), []
    a = R.RailAssets(True, BBOX, tmp_path, fetch=f, log=logs.append, clock=lambda: NOW)
    assert a.doc is None and a.layer()["poles"] == [] and a.status()["n"] is None
    assert a.refresh()
    urls = [u for u, _ in f.calls]
    assert all(u.startswith(R.LINK + "/") and "f=geojson" in u and "outSR=4326" in u and "geometryType=esriGeometryEnvelope" in u for u in urls)
    assert sum("/7/query" in u for u in urls) == 3                      # 82 poles, 30 a page
    assert all(("maxAllowableOffset" in u) == ("/6/query" in u) for u in urls)   # only the tracks thinned
    assert all("Port Chester Fire Department" in h["User-Agent"] for _, h in f.calls)
    assert a.counts() == {"tracks": 4, "poles": 82, "mileposts": 41}
    lay = a.layer()
    assert len(lay["poles"]) == 82 and set(lay["poles"][0]) == {"id", "lon", "lat"}
    assert sum(m["whole"] for m in lay["mileposts"]) == 5
    b = R.RailAssets(True, BBOX, tmp_path, fetch=Fetch(fail=500), log=lambda m: None, clock=lambda: NOW + 9)
    assert b.doc is not None and b.st["at"] == NOW                      # a restart: the copy, nothing fetched
    assert not b.refresh() and b.st["error"] == "HTTP 500" and b.counts()["poles"] == 82   # a failure keeps the copy
    assert b.st["next"] == NOW + 9 + R.BACKOFF_S[0]


def test_an_answer_with_nothing_of_ours_or_an_error_is_refused(tmp_path):
    a = R.RailAssets(True, BBOX, tmp_path, fetch=Fetch(lay={"tracks": [], "poles": [], "mileposts": [], "tenths": []}),
                     log=lambda m: None, clock=lambda: NOW)
    assert not a.refresh() and "nothing in our area" in a.st["error"] and a.doc is None
    err = R.RailAssets(True, BBOX, None, fetch=lambda *x, **k: (200, {}, b'{"error":{"code":400,"message":"Invalid query"}}'),
                       log=lambda m: None, clock=lambda: NOW)
    assert not err.refresh() and "Invalid query" in err.st["error"]
    off = R.RailAssets(False, BBOX, tmp_path)
    assert not off.refresh() and off.status() == {"on": False, "why": "Trains off ([trains])"}


class FeedFetch:
    def __call__(self, url, timeout, limit, headers=None):
        return 200, {}, json.dumps({}).encode()


def test_the_trains_layer_sends_the_numbered_tracks_and_the_poles(tmp_path, monkeypatch):
    """Trains with the rail assets: a model with them once they are read (the page's ?have= changes with them),
    the tracks in the layer, the poles and mileposts at /api/cad/trains/railref; none: empty."""
    gtfs = (FIX / "gtfsmnr.zip").read_bytes()
    feed = lambda url, timeout, limit, headers=None: (200, {}, gtfs)
    lay = {k: json.loads((FIX / "railassets" / f"{k}.geojson").read_text())["features"] for k in ("tracks", "poles", "mileposts", "tenths")}
    clock = [float(NOW)]
    tr = Trains(rt_url="https://mta.example/rt", gtfs_url="https://mta.example/gtfs.zip", cache_dir=tmp_path / "trains", fetch=feed,
                log=lambda m: None, clock=lambda: clock[0], traintime=False, assets_fetch=Fetch(lay=lay))
    assert tr.refresh_static()
    d = json.loads(tr.get("/api/cad/trains", "")[1])
    assert d["tracks"] == {} and json.loads(tr.get(T.RAILREF_PATH, "")[1])["poles"] == []
    key = d["static_at"]
    clock[0] += 60
    assert tr.refresh_assets()
    assert sorted(tr.model.track_lanes) == ["1", "2", "3", "4"] and (tmp_path / "trains" / R.CACHE_NAME).is_file()
    d2 = json.loads(tr.get("/api/cad/trains", f"have={key}")[1])        # the page's copy is old now: sent again
    assert "static_same" not in d2 and sorted(d2["tracks"]) == ["1", "2", "3", "4"]
    assert all(len(v) > 3 and [c[2] for c in v] == sorted(c[2] for c in v) for v in d2["tracks"].values())
    assert "tracks" not in json.loads(tr.get("/api/cad/trains", f"have={d2['static_at']}")[1])
    ref = json.loads(tr.get(T.RAILREF_PATH, "")[1])
    assert len(ref["poles"]) == 12 and ref["source"] == R.SOURCE and tr.handles(T.RAILREF_PATH)
    # a restart: the copy read, the model built with it
    again = Trains(rt_url="https://mta.example/rt", gtfs_url="https://mta.example/gtfs.zip", cache_dir=tmp_path / "trains",
                   fetch=feed, log=lambda m: None, clock=lambda: clock[0], traintime=False, assets_fetch=Fetch(fail=500))
    assert again.model is not None and sorted(again.model.track_lanes) == ["1", "2", "3", "4"]


def test_the_data_sources_row_and_the_s3_copy():
    from sizeup import cachesync, datasources
    assert any(p.fullmatch("trains/rail_assets.json") for p, _ in cachesync.FILES)
    tr = Trains(rt_url="https://mta.example/rt", gtfs_url="https://mta.example/gtfs.zip", fetch=lambda *a, **k: (500, {}, b""),
                log=lambda m: None, traintime=False)
    (row,) = [s for s in datasources.trains_sources(tr) if s.key == "trains:railassets"]
    assert row.every == "monthly" and row.quiet and row.source == R.SOURCE and row.refresh == tr.refresh_assets
    assert row.status()["at"] is None


def test_the_page_draws_the_numbered_track_and_the_poles_box():
    js = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "trains.js").read_text()
    assert "const own = tracks && t.trk ? tracks[t.trk] : null;" in js
    assert "d.tracks = had.tracks" in js and 'keys() { return [[KEY, false], [REF_KEY, false]]; }' in js
    assert 'RAILREF = "/api/cad/trains/railref"' in js and '"Poles · mileposts"' in js
    assert "`Trk\\u00a0${t.trk}`" in js and "`near pole ${t.pole}`" in js and "refText(d.ref)" in js
