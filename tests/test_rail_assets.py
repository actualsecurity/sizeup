"""Metro-North's numbered tracks, catenary poles and mileposts (GitHub #371, sizeup/railassets.py): the train on its own
numbered track from the stops' tracks, "near pole N · MP x.x" for a train or an incident by the line, the layer.

All geometry is synthetic (no Metro-North features in the repo): a straight line east along 41.0 N with four parallel
tracks (3, 1, 2, 4 from north to south, 4 m apart, as on the New Haven Line here), a catenary structure every 100 m and
a milepost every tenth of a mile; and the same laid along the recorded static feed's own line by Port Chester, in the
service's shape (GeoJSON features with its field names; the whole mileposts LineCode NHV, as some of its copies have).
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
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
    whole = [gj("Point", [east(MP0_CH + k * 1609.344), LAT + 20 * DLAT], PointName=str(int(MP0) + k), LineCode="NHV") for k in range(5)]
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


def smooth_line(line, s0: float, s1: float, step: float = 10.0, half: float = 150.0, passes: int = 3):
    """off(s, m) -> [lon, lat]: m metres to the left (going up the line) of a smooth curve through a real line
    (trains.Line) from s0 to s1. The static feed's line is a coarse polyline with kinks of 4 to 11 degrees every
    60 to 200 m; tracks offset square to its segments jump at each kink (#374's squiggly screenshots). Its points
    every step metres are averaged over +-half, passes times (about a Gaussian), and offset square to that."""
    ss = [s0 - 3 * half + k * step for k in range(int((s1 - s0 + 6 * half) // step) + 1)]
    pts = [T._xy(*line.at(s)[1::-1]) for s in ss]
    w = int(half // step)
    for _ in range(passes):
        pts = [(sum(q[0] for q in pts[max(0, i - w):i + w + 1]) / len(pts[max(0, i - w):i + w + 1]),
                sum(q[1] for q in pts[max(0, i - w):i + w + 1]) / len(pts[max(0, i - w):i + w + 1])) for i in range(len(pts))]

    def off(s, m):
        f = (s - ss[0]) / step
        i = max(1, min(len(pts) - 2, int(f)))
        t = min(1.0, max(0.0, f - i))
        x, y = pts[i][0] + t * (pts[i + 1][0] - pts[i][0]), pts[i][1] + t * (pts[i + 1][1] - pts[i][1])
        ux, uy = pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]
        n = math.hypot(ux, uy)
        return [(x - uy / n * m) / T.MX, (y + ux / n * m) / T.MY]
    return off


def along_line(line, s0: float, s1: float) -> dict:
    """The four layers laid along a real line (trains.Line) from s0 to s1, smoothly (smooth_line): tracks 3, 1, 2, 4
    to its left (going up the line, north here), a portal every 100 m numbered from 200, a milepost every tenth from
    MP 25.0 at s0."""
    off = smooth_line(line, s0, s1)
    steps = [s0 + k * 5 for k in range(int((s1 - s0) // 5) + 1)]
    tracks = [gj("LineString", [off(x, OFF[n]) for x in steps], LineCode="NHL", TrkName=n, TrkType="Main Line") for n in OFF]
    poles = [gj("LineString", [off(s0 + k * 100, 12), off(s0 + k * 100, -12)], LineCode="NHL", CB_ID=f"CB-{200 + k}")
             for k in range(int((s1 - s0) // 100) + 1)]
    mps = [(k, s0 + k * 160.9344) for k in range(int((s1 - s0) // 160.9344) + 1)]
    whole = [gj("Point", off(x, 20), PointName=str(25 + k // 10), LineCode="NHV") for k, x in mps if k % 10 == 0]
    tenths = [gj("Point", off(x, 20), Name=f"{25 + k / 10:.1f}", LineCode="NHL") for k, x in mps if k % 10]
    return {"tracks": tracks, "poles": poles, "mileposts": whole, "tenths": tenths}


def test_laid_along_the_mtas_line_the_tracks_lie_3_1_2_4_and_the_tenths_a_tenth_apart():
    """Measured along the recorded static feed's line (the projection and splitting a real copy goes through): the
    four tracks in the New Haven Line's order from north to south by Port Chester, the tenths 160.9 m apart."""
    static = T.parse_static((FIX / "gtfsmnr.zip").read_bytes())
    line = Model(static).line
    pc = next(st["ch"] for st in static["stations"].values() if st.get("code") == "2PC")
    d = R.parse(along_line(line, pc - 1200, pc + 1200), T.DEFAULT_BBOX)
    m = Model(static, T.DEFAULT_BBOX, None, d)
    assert sorted(m.track_lanes) == ["1", "2", "3", "4"] and all(len(v) == 1 for v in m.track_lanes.values())
    for s in (pc - 1000, pc, pc + 1000):
        assert sorted(m.track_lanes, key=lambda k: -m.track_at(k, s)[0]) == ["3", "1", "2", "4"]
    gaps = [b - a for (a, _), (b, _) in zip(m.ref.mps, m.ref.mps[1:])]
    assert len(m.ref.mps) == 15 and all(158 < g < 164 for g in gaps)   # a tenth of a mile is 160.9 m
    assert m.ref.mp_at(pc) == pytest.approx(25.7, abs=0.06) and m.ref.pole_at(pc) == ("212", pytest.approx(0, abs=1))


def test_a_track_with_a_gap_is_two_runs_and_nothing_is_drawn_across_it():
    lay = layers()
    lay["tracks"][1] = gj("MultiLineString", [[[east(x), LAT + OFF["1"] * DLAT] for x in range(0, 3001, 250)],
                                              [[east(x), LAT + OFF["1"] * DLAT] for x in range(5000, 8001, 250)]],
                          LineCode="NHL", TrkName="1", TrkType="Main Line")              # two parts: 2 km missing between
    lay["tracks"][3]["geometry"]["coordinates"][16][1] += 100 * DLAT                    # track 4: a point off the line (4000 m)
    lay["tracks"].append(gj("LineString", [[east(x), LAT + OFF["2"] * DLAT] for x in range(8000, 7000, -250)],
                            LineCode="NHL", TrkName="2"))                                # a second feature, drawn backwards
    m = Model(static(), BBOX, None, R.parse(lay, BBOX))
    assert [(round(l.s[0]), round(l.s[-1])) for l in m.track_lanes["1"]] == [(0, 3000), (5000, 8000)]
    assert m.track_at("1", 2000) is not None and m.track_at("1", 4000) is None and m.track_at("1", 5500) is not None
    assert m.where(1, 4000, "1") == pytest.approx(m.line.at(4000)[:2])  # in the gap: not a chord across it
    assert len(m.track_lanes["2"]) == 2 and all(l.s == sorted(l.s) for l in m.track_lanes["2"])
    assert [(round(l.s[0]), round(l.s[-1])) for l in m.track_lanes["4"]] == [(0, 3750), (4250, 8000)] and m.track_at("4", 4000) is None


def test_a_track_whose_s_jumps_more_than_its_step_is_two_runs():
    """A run is broken where its s jumps by more than the step itself plus TRACK_GAP_M (railassets.RailRef): here the
    line runs east 4 km, turns and comes back west 60 m to the north, and a track between the two legs projects onto
    the first leg and then the second. Its points are all by the line; one run would be a chord 4 km long."""
    line = T.Line([(round(east(x), 7), LAT) for x in range(0, 4001, 500)]
                  + [(round(east(x), 7), LAT + 60 * DLAT) for x in range(4000, -1, -500)])
    run = [[east(x), LAT + 25 * DLAT] for x in range(0, 2000, 100)] + [[east(x), LAT + 35 * DLAT] for x in range(2000, 3601, 100)]
    ref = R.RailRef({"tracks": {"1": [run]}}, line)
    runs = ref.tracks["1"]
    assert len(runs) == 2
    assert (round(runs[0][0][2]), round(runs[0][-1][2])) == (0, 1900)                  # the first leg
    first_leg_end = 4000 + 60
    assert all(c[2] > first_leg_end for c in runs[1]) and len(runs[1]) == 17          # the way back
    assert [c[2] for r in runs for c in r] == sorted(c[2] for r in runs for c in r)
    whole = R.RailRef({"tracks": {"1": [[[east(x), LAT + 25 * DLAT] for x in range(0, 3601, 100)]]}}, line)
    assert len(whole.tracks["1"]) == 1                                                  # (all on one leg: one run)


# ---- smooth tracks and the structures' ticks (#374) -----------------------------------------------------------------


def turns(run) -> list[float]:
    """The degrees a run [[lon, lat, ...]] turns at each inner point, left positive."""
    P = [T._xy(c[0], c[1]) for c in run]
    out = []
    for i in range(1, len(P) - 1):
        h1 = math.atan2(P[i][1] - P[i - 1][1], P[i][0] - P[i - 1][0])
        h2 = math.atan2(P[i + 1][1] - P[i][1], P[i + 1][0] - P[i][0])
        out.append(math.degrees((h2 - h1 + math.pi) % (2 * math.pi) - math.pi))
    return out


def smooth(run) -> tuple[float, float]:
    """(the most a run turns at one point, the most that changes from one point to the next): a zig-zag turns
    one way, then the other."""
    t = turns(run)
    return max(map(abs, t)), max(abs(a - b) for a, b in zip(t, t[1:]))


def heading(a, b) -> float:
    (ax, ay), (bx, by) = T._xy(*a[:2]), T._xy(*b[:2])
    return math.degrees(math.atan2(bx - ax, by - ay)) % 180      # an undirected line's, 0..180 from north


def test_the_tracks_laid_along_the_real_line_are_smooth_as_the_page_gets_them(tmp_path):
    """#374 "these lines look super squiggly": the static feed's line kinks 4 to 11 degrees every 60 to 200 m, so
    tracks laid square to its segments zig-zagged (the PR's screenshots). Laid along a smooth curve through it, a
    point every 5 m, and sent at DP_TRACK (1 cm; at 1 m, tracks 4 m apart wobble), no track turns more than 1.5
    degrees at a point (the line's own curves here) nor changes its turn by more than 1.5 degrees from
    one point to the next, and the four stay 4 m apart. Square to the line's own segments: an 8 degree zig-zag."""
    static = T.parse_static((FIX / "gtfsmnr.zip").read_bytes())
    line = Model(static).line
    pc = next(st["ch"] for st in static["stations"].values() if st.get("code") == "2PC")
    m = Model(static, T.DEFAULT_BBOX, None, R.parse(along_line(line, pc - 2500, pc + 2500), T.DEFAULT_BBOX))
    for k, lanes in m.track_lanes.items():
        sent = [[round(a, T.DP_TRACK), round(b, T.DP_TRACK)] for a, b, _ in lanes[0].coords]
        most, change = smooth(sent)
        assert len(lanes) == 1 and most < 1.5 and change < 1.5, k
    for s in range(int(pc - 2000), int(pc + 2000), 50):
        p1, p2 = m.track_at("1", s), m.track_at("2", s)
        assert 3.6 < T.metres(*p1, *p2) < 4.4
    square = lambda x: [line.at(x)[1] - math.cos(math.radians(line.at(x)[2])) * 6 * DLON,
                        line.at(x)[0] + math.sin(math.radians(line.at(x)[2])) * 6 * DLAT]
    assert smooth([square(x) for x in range(int(pc - 2400), int(pc + 2400), 20)])[1] > 6     # the old screenshots' way


def test_a_tracks_points_keep_their_own_order_never_sorted_by_s():
    """The other way to a zig-zag: points sorted by their s on a line that isn't clean. Here the line doubles back
    8 m every 200 m (as a schedule's shape can) and a straight track runs beside it: there the points' s come out of
    order, and sorted by s the track went back and forth. Kept in the service's order (s held where it steps back),
    it stays straight, and a train still finds its place on it."""
    line = T.Line([(round(east(x), 7), LAT + y * DLAT)
                   for k in range(10) for x, y in ((k * 200, 0), (k * 200 + 100, 0), (k * 200 + 92, 3), (k * 200 + 200, 0))])
    run = [[round(east(x), 7), LAT + 1.5 * DLAT] for x in range(0, 2001, 3)]
    ref = R.RailRef({"tracks": {"1": [run]}}, line)
    [out] = ref.tracks["1"]
    assert [c[:2] for c in out] == [c for c in run if any(c == o[:2] for o in out)] and len(out) > len(run) * 0.9
    assert smooth(out)[0] < 0.5 and [c[2] for c in out] == sorted(c[2] for c in out)
    lane = T.Lane(out)
    xs = [lane.at(x)[1] for x in range(int(out[0][2]) + 1, int(out[-1][2]), 25)]
    assert lane.coords == out and xs == sorted(xs) and len(xs) > 50
    by_s = sorted(([c[0], c[1], line.project(c[1], c[0])[1]] for c in run), key=lambda c: c[2])
    assert smooth(by_s)[0] > 90                                                      # (sorted by s: back and forth)


def test_the_services_digitising_slips_are_dropped():
    """The service's main tracks have a few hairpins doubling back 3 to 9 m and 1 cm steps at right angles: gone,
    the rest of the track as it was."""
    run = [[east(x), LAT] for x in range(0, 201, 10)]
    spiked = run[:8] + [[east(64), LAT], [east(72), LAT]] + run[8:]         # back 6 m, then on again
    stepped = run[:5] + [[east(40), LAT + 0.01 * DLAT]] + [[c[0], c[1] + 0.01 * DLAT] for c in run[5:]]
    for bad in (spiked, stepped):
        out = R._clean(bad)
        assert smooth(out)[0] < 1 and len(out) >= len(run) - 1
    assert R._clean(run) == run


def test_each_structure_is_a_tick_square_across_all_four_tracks():
    """TrainTime's look: a line square to the tracks through the structure, across all four and 3 m past the outer
    ones (6 m each side of the line here: 18 m), its first end to the left going up the line (north here)."""
    ref = Model(static(), BBOX, None, R.parse(layers(), BBOX)).ref
    assert len(ref.ticks) == len(ref.poles) == 82
    for (a, b) in (ref.ticks[i] for _, i in ref.poles):
        assert abs(heading(a, b) - 0) < 0.5 or abs(heading(a, b) - 180) < 0.5          # north-south: square to the tracks
        assert T.metres(a[1], a[0], b[1], b[0]) == pytest.approx(18, abs=0.2) and a[1] > b[1]
    ref = R.RailRef({"poles": R.parse(layers(), BBOX)["poles"]}, Model(static()).line)   # no tracks: square to the line
    a, b = ref.ticks["210"]
    assert abs(heading(a, b) - 0) < 0.5 and T.metres(a[1], a[0], b[1], b[0]) == pytest.approx(2 * R.TICK_HALF_M, abs=0.2)


def test_along_the_real_line_each_tick_is_square_to_the_tracks_there():
    static = T.parse_static((FIX / "gtfsmnr.zip").read_bytes())
    line = Model(static).line
    pc = next(st["ch"] for st in static["stations"].values() if st.get("code") == "2PC")
    m = Model(static, T.DEFAULT_BBOX, None, R.parse(along_line(line, pc - 2000, pc + 2000), T.DEFAULT_BBOX))
    [run] = m.ref.tracks["2"]
    for s, i in m.ref.poles:
        a, b = m.ref.ticks[i]
        k = min(range(1, len(run) - 1), key=lambda j: abs(run[j][2] - s))
        trk = heading(run[k - 1], run[k + 1])
        assert abs((heading(a, b) - trk) % 180 - 90) < 2, i                        # square to track 2 there
        assert T.metres(a[1], a[0], b[1], b[0]) == pytest.approx(18, abs=1)          # across all four, 3 m past


# ---- the train on its numbered track ----------------------------------------------------------------------------


def stop(code_or_id, track="", status="", posted=True):
    return {"code": code_or_id, "name": code_or_id, "track": track, "posted": posted, "status": status}


def anchors(ta="", tb=""):
    return [({"stop_id": "A", "track": ta, "status": "Departed"}, A_CH), ({"stop_id": "B", "track": tb, "status": ""}, B_CH)]


def track(model, t, s, a=None, d=1, mem=None):
    """(the track drawn, the evidence shown: only near the stop that said it)."""
    lane, ev, how = model.which_track(t, a or anchors(), 0, 1, s, d, mem)
    return lane, (ev["trk"], ev["src"], ev["name"]) if ev and how == "said" else None


def how(model, t, s, a=None, d=1, mem=None):
    """(the track drawn, how sure: said | held | outer)."""
    lane, _, sure = model.which_track(t, a or anchors(), 0, 1, s, d, mem)
    return lane, sure


def test_the_track_comes_from_the_stop_either_side_within_a_mile(model):
    t = {"tt": {"prev": stop("2RY", "2"), "next": stop("2PC", "2")}}
    assert track(model, t, 2500) == ("2", ("2", "posted", "2RY"))
    # left on 1 (planned), going to 4 (posted): the nearer stop's, only within TRACK_NEAR_M of it
    t = {"tt": {"prev": stop("2RY", "1", posted=False), "next": stop("2PC", "4")}}
    assert track(model, t, 2500) == ("1", ("1", "planned", "2RY"))
    assert track(model, t, 5500) == ("4", ("4", "posted", "2PC"))
    assert track(model, t, 4000, d=-1) == ("3", None)                   # 2 km from both: its direction's outer track
    assert track(model, t, 4000, d=1) == ("4", None)
    assert track(model, {"tt": {"next": stop("2PC", "03")}}, 5000) == ("3", ("3", "posted", "2PC"))   # leading zero
    assert track(model, {}, 5000, anchors("4", "2")) == ("2", ("2", "mta", "Port Chester"))         # GTFS-realtime's
    # one stop, two words: TrainTime's posted over GTFS-realtime's
    assert track(model, {"tt": {"next": stop("2PC", "1")}}, 5000, anchors("", "2")) == ("1", ("1", "posted", "2PC"))
    assert track(model, {"tt": {"next": stop("2PC", "5")}}, 5000) == ("4", None)   # no line of track 5 here


def test_no_gps_guess_between_tracks_4_m_apart(model):
    """#371 review: the tracks are closer than a phone-grade GPS fix can tell; a fix by track 2 with no stop's word
    is drawn on the direction's outer track, never "nearest"."""
    t = {"tt": {"next": stop("2PC", "")}, "pos": {"src": "traintime", "lat": LAT + OFF["2"] * DLAT, "lon": east(3000)}}
    assert track(model, t, 3000) == ("4", None) and track(model, t, 3000, d=-1) == ("3", None)
    assert not hasattr(T, "NEAREST_TRACK_M")


def test_a_trains_track_holds_until_a_stop_says_otherwise(model):
    mem = {}
    posted2 = {"tt": {"prev": stop("2RY", "2"), "next": stop("2PC", "")}}
    assert track(model, posted2, 2500, mem=mem) == ("2", ("2", "posted", "2RY"))
    assert track(model, {}, 4000, mem=mem) == ("2", None)               # between stations: still 2, no "Trk" shown
    # the merge flips to GTFS-realtime, which says 4 for Rye: a weaker word for the same stop: it stays on 2
    assert track(model, {}, 2600, anchors("4", ""), mem=mem) == ("2", ("2", "posted", "2RY"))
    # Port Chester's track (another stop's word): it moves
    assert track(model, {}, 5200, anchors("", "1"), mem=mem) == ("1", ("1", "mta", "Port Chester"))
    assert track(model, {"tt": {"next": stop("2PC", "3")}}, 5300, mem=mem) == ("3", ("3", "posted", "2PC"))   # a better word
    assert track(model, {}, 5300, anchors("", "1"), mem=mem) == ("3", ("3", "posted", "2PC"))


def test_the_same_track_from_a_weaker_source_keeps_the_stronger_one(model):
    """#371 review: posted 2, then GTFS-realtime 2, then GTFS-realtime 3, all for the same stop: still 2 (the GTFS word
    for 2 doesn't stand in for the posted one, so its 3 can't then replace it)."""
    mem = {}
    assert track(model, {"tt": {"next": stop("2PC", "2")}}, 5000, mem=mem) == ("2", ("2", "posted", "2PC"))
    assert track(model, {}, 5000, anchors("", "2"), mem=mem) == ("2", ("2", "posted", "2PC"))
    assert track(model, {}, 5000, anchors("", "3"), mem=mem) == ("2", ("2", "posted", "2PC"))
    assert mem["ev"]["src"] == "posted"
    assert track(model, {"tt": {"next": stop("2PC", "1", posted=False)}}, 5000, mem=mem) == ("2", ("2", "posted", "2PC"))
    assert track(model, {"tt": {"next": stop("2PC", "4")}}, 5000, mem=mem) == ("4", ("4", "posted", "2PC"))   # a posted change


def test_a_merge_alternating_sources_between_close_stations_doesnt_flip_the_track(doc):
    """#371 review: stations 2.6 km apart, the train between them within TRACK_NEAR_M of both; the merge alternates
    TrainTime (next stop Port Chester, posted 2) with GTFS-realtime only (it last left Rye, on 4). Only a stop ahead of
    the one it is drawn by moves it: going east, 2 throughout once Port Chester has said; going west, Rye is ahead."""
    st = static()
    st["stations"]["B"] |= {"ch": 4600.0, "lon": east(4600)}
    m = Model(st, BBOX, None, doc)
    tt = {"tt": {"next": stop("2PC", "2")}}
    gtfs = [({"stop_id": "A", "track": "4", "status": "Departed"}, A_CH), ({"stop_id": "B", "track": "", "status": ""}, 4600.0)]
    mem, drawn = {}, []
    for k in range(10):
        s = 3100 + 40 * k
        drawn.append(track(m, {}, s, gtfs, mem=mem)[0] if k % 2 == 0 else track(m, tt, s, mem=mem)[0])
    assert drawn == ["4"] + ["2"] * 9                                   # Rye's 4 until Port Chester (ahead) said 2
    west = {}
    assert track(m, tt, 3300, d=-1, mem=west)[0] == "2"
    assert track(m, {}, 3250, gtfs, d=-1, mem=west) == ("4", ("4", "mta", "Rye"))   # Rye: ahead going west
    assert track(m, tt, 3200, d=-1, mem=west)[0] == "4"                 # and Port Chester, behind now, doesn't move it back


def test_a_held_track_is_marked_held_and_dropped_beyond_4_km(model):
    """#371 review: between stations the last-said track is held (shown as held, not as said), only up to TRACK_HOLD_M
    from its stop; beyond (an express may cross over), and with nothing ever said, the outer track: not known."""
    mem = {}
    assert how(model, {"tt": {"prev": stop("2RY", "2")}}, 2500, mem=mem) == ("2", "said")
    assert how(model, {}, A_CH + T.TRACK_NEAR_M + 100, mem=mem) == ("2", "held")
    assert how(model, {}, A_CH + T.TRACK_HOLD_M - 10, mem=mem) == ("2", "held")
    assert how(model, {}, A_CH + T.TRACK_HOLD_M + 10, mem=mem) == ("4", "outer")
    assert mem["ev"]["name"] == "2RY"                                   # (kept: a stop behind it still can't move it)
    assert how(model, {}, 3000, anchors("1", ""), mem=mem) == ("2", "said")
    assert how(model, {}, 4000) == ("4", "outer") and how(model, {}, 4000, d=-1) == ("3", "outer")


def test_a_train_is_drawn_on_its_numbered_track_with_its_pole_and_milepost(model):
    feed = {"timestamp": NOW, "trains": {"1234": {
        "id": "1234", "train": "1234", "trip_id": "x", "route_id": "", "start_date": "", "delay": None, "vstatus": "IN_TRANSIT_TO",
        "vstop": "B", "pos": {"lat": LAT, "lon": east(5000), "ts": NOW},
        "stops": [{"stop_id": "A", "arr": NOW - 200, "dep": NOW - 180, "delay": 0, "track": "2", "status": "Departed"},
                  {"stop_id": "B", "arr": NOW + 200, "dep": NOW + 220, "delay": 0, "track": "2", "status": ""}]}}}
    tracker = T.Tracker()
    tracker.update(model, feed, NOW)
    (x,) = model.trains(feed, NOW, tracker)
    assert x["lane"] == "2" and (x["trk"], x["trk_src"], x["trk_at"]) == ("2", "mta", "Port Chester") and x["d"] == 1
    assert x["lane_how"] == "said" and "lane_at" not in x
    assert x["lat"] == pytest.approx(LAT + OFF["2"] * DLAT, abs=6e-6)   # on track 2, not the line (to the page's 5 decimals)
    assert x["pole"] == str(200 + round(x["s"] / 100)) and x["mp"] == pytest.approx(MP0 + (x["s"] - MP0_CH) / T.MILE_M, abs=0.051)
    assert tracker.trk_mem["1234"]["ev"]["trk"] == "2"
    fresh = json.loads(json.dumps(feed))
    for st in fresh["trains"]["1234"]["stops"]:                        # no track known, nothing remembered: the outer one
        st["track"] = ""
    (y,) = model.trains(fresh, NOW)
    assert y["lane"] == "4" and "trk" not in y and y["lat"] == pytest.approx(LAT + OFF["4"] * DLAT, abs=6e-6)
    assert y["lane_how"] == "outer"
    (z,) = model.trains(fresh, NOW, tracker)                            # remembered: still on 2
    assert z["lane"] == "2"
    # forgotten TRACK_MEM_S after it was last drawn: this train's and any other's
    tracker.trk_mem["9999"] = {"ev": dict(tracker.trk_mem["1234"]["ev"]), "seen": NOW - T.TRACK_MEM_S - 1}
    tracker.trk_mem["1234"]["seen"] = NOW - T.TRACK_MEM_S + 1
    (z,) = model.trains(fresh, NOW, tracker)
    assert z["lane"] == "2" and "9999" not in tracker.trk_mem
    tracker.trk_mem["1234"]["seen"] = NOW - T.TRACK_MEM_S - 1
    (z,) = model.trains(fresh, NOW, tracker)
    assert z["lane"] == "4" and z["lane_how"] == "outer" and "ev" not in tracker.trk_mem["1234"]


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
    assert m.which_track({"tt": {"next": stop("2PC", "2")}}, anchors(), 0, 1, 5000, 1, {}) == ("", None, "")
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


def where_ok(where: str, props: dict) -> bool:
    """The service's WHERE as we send it: "F='v'", "F IN ('a','b')", joined by AND."""
    for part in re.split(r"\s+AND\s+", where.strip()):
        m = re.fullmatch(r"(\w+)\s*=\s*'([^']*)'", part) or re.fullmatch(r"(\w+)\s+IN\s+\(([^)]*)\)", part)
        assert m, part
        ok = [v.strip().strip("'") for v in m.group(2).split(",")] if " IN " in part else [m.group(2)]
        if props.get(m.group(1)) not in ok:
            return False
    return True


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
        feats, off = [f for f in self.lay[key] if where_ok(q["where"][0], f["properties"])], int(q["resultOffset"][0])
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
    ua = "Port Chester Fire Department - sizeup emergency situational awareness application (github.com/actualsecurity/sizeup)"
    assert f.calls and all(h["User-Agent"] == ua for _, h in f.calls)  # exactly ours
    assert a.counts() == {"tracks": 4, "poles": 82, "mileposts": 41}
    lay = a.layer()
    assert len(lay["poles"]) == 82 and set(lay["poles"][0]) == {"id", "lon", "lat"}
    assert sum(m["whole"] for m in lay["mileposts"]) == 5              # the whole miles (LineCode NHV) through our WHERE
    b = R.RailAssets(True, BBOX, tmp_path, fetch=Fetch(fail=500), log=lambda m: None, clock=lambda: NOW + 9)
    assert b.doc is not None and b.st["at"] == NOW                      # a restart: the copy, nothing fetched
    assert not b.refresh() and b.st["error"] == "HTTP 500" and b.counts()["poles"] == 82   # a failure keeps the copy
    assert b.st["next"] == NOW + 9 + R.BACKOFF_S[0]
    assert "maxAllowableOffset=1e-06" in next(u for u, _ in f.calls if "/6/query" in u)    # 10 cm (#374)
    old = json.loads((tmp_path / R.CACHE_NAME).read_text()) | {"thin": 0.000005}            # thinned to 0.5 m: due again
    (tmp_path / R.CACHE_NAME).write_text(json.dumps(old))
    c = R.RailAssets(True, BBOX, tmp_path, fetch=Fetch(fail=500), log=lambda m: None, clock=lambda: NOW + 9)
    assert c.doc is not None and c.st["at"] is None and c.counts()["poles"] == 82


def test_the_whole_mileposts_are_asked_for_under_either_line_code():
    """#371 review: the service's whole mileposts (layer 2) say LineCode NHV, the rest NHL."""
    assert where_ok(R.LAYERS["mileposts"][1], {"LineCode": "NHV"}) and where_ok(R.LAYERS["mileposts"][1], {"LineCode": "NHL"})
    assert not where_ok("LineCode='NHL'", {"LineCode": "NHV"})          # (what we asked before)
    assert where_ok(R.LAYERS["tracks"][1], {"LineCode": "NHL", "TrkType": "Main Line"})
    assert not where_ok(R.LAYERS["tracks"][1], {"LineCode": "NHL", "TrkType": "Siding"})


def test_a_rate_limit_or_refusal_rests_and_data_sources_says_so(tmp_path):
    from sizeup import datasources
    clock = [float(NOW)]
    a = R.RailAssets(True, BBOX, tmp_path, fetch=Fetch(fail=429), log=lambda m: None, clock=lambda: clock[0])
    assert not a.refresh() and "rate limit" in a.st["error"] and a.st["next"] == NOW + R.RATE_LIMITED_S
    assert "resting" in datasources._resting(a.st, lambda: clock[0])
    b = R.RailAssets(True, BBOX, tmp_path, fetch=Fetch(fail=403), log=lambda m: None, clock=lambda: clock[0])
    assert not b.refresh() and "refused" in b.st["error"] and datasources._resting(b.st, lambda: clock[0])


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
    static = T.parse_static(gtfs)
    pc = next(st["ch"] for st in static["stations"].values() if st.get("code") == "2PC")
    lay = along_line(Model(static).line, pc - 550, pc + 550)
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
    assert all(len(runs) == 1 and len(r) > 3 and [c[2] for c in r] == sorted(c[2] for c in r) for runs in d2["tracks"].values() for r in runs)
    rr = d2["railref"]                                                  # the poles' and mileposts' s: read at the drawn s
    assert len(rr["poles"]) == 12 and rr["poles"][0][1] == "200" and [s for s, _ in rr["mps"]] == sorted(s for s, _ in rr["mps"])
    same = json.loads(tr.get("/api/cad/trains", f"have={d2['static_at']}")[1])
    assert "tracks" not in same and "railref" not in same
    ref = json.loads(tr.get(T.RAILREF_PATH, "")[1])
    assert len(ref["poles"]) == 12 and ref["source"] == R.SOURCE and tr.handles(T.RAILREF_PATH)
    assert all(len(p["tick"]) == 2 and p["tick"] == tr.model.ref.ticks[p["id"]] for p in ref["poles"])   # each one's tick (#374)
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
    assert "const own = runAt(tracks, t.lane, t.s);" in js and "function refAt(rr, s)" in js
    assert js.count("placeRow(t, w, d.railref)") == 3 and "`Pole: CB ${at.pole}" in js and "nearest" not in js.split("function placeRow")[1][:900]
    assert "d.tracks = had.tracks; d.railref = had.railref;" in js and 'keys() { return [[KEY, false], [REF_KEY, false]]; }' in js
    assert 'RAILREF = "/api/cad/trains/railref"' in js and '"Poles · mileposts"' in js
    assert 'id: "tr-line-label", type: "symbol", source: SRC_LINE, filter: ["has", "trk"]' in js         # each track numbered
    assert "if (!key || this.tracks) { src.setData(fc([])); return; }" in js                       # no route line posing as a track
    assert "`Trk\\u00a0${t.trk}`" in js and "`near pole ${at.pole}`" in js and "refText(d.ref)" in js


# ---- the page: the box's place row and the train's style (trains.js in node) ---------------------------------------

NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
PAGE = r"""
const fs = require("fs");
global.window = global;
global.document = { visibilityState: "visible", addEventListener() {}, documentElement: {},
                    createElement: () => ({ getContext: () => null, append() {} }) };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
eval(fs.readFileSync(process.argv[2], "utf8"));
const IN = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
"""


def page(tmp_path, body: str, data) -> dict:
    h, d = tmp_path / "page.js", tmp_path / "in.json"
    h.write_text(PAGE + body + "\nconsole.log(JSON.stringify(out));\n")
    d.write_text(json.dumps(data))
    r = subprocess.run([NODE, str(h), str(Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "trains.js"),
                        str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_box_reads_the_pole_and_milepost_at_the_drawn_s_and_falls_back_with_no_rail_data(tmp_path, model):
    """The place row reads the hub's railref at the train's drawn s (not the hub's last s); with no rail data on the
    hub (railref {poles: [], mps: []}) or none sent, the train's own milepost (TrainTime's) still shows (#371 review)."""
    rr = {"poles": [[round(s, 1), i] for s, i in model.ref.poles], "mps": [[round(s, 1), v] for s, v in model.ref.mps]}
    t = {"s": 3000.0, "trk": "2", "trk_src": "posted", "trk_at": "Rye", "lane": "2", "lane_how": "said", "pole": "230", "mp": 21.2}
    out = page(tmp_path, """
const P = TrainLayer.placeRow;
out.drawn = P(IN.t, { s: 3420 }, IN.rr);
out.hub = P(IN.t, {}, IN.rr);
out.empty = P(IN.t, { s: 3420 }, { poles: [], mps: [] });
out.none = P(IN.t, { s: 3420 }, undefined);
out.ttOnly = P({ s: 3000, mp: 27.3 }, { s: 3420 }, { poles: [], mps: [] });
out.held = P({ ...IN.t, trk: undefined, lane_how: "held", lane_at: "Rye" }, { s: 3420 }, IN.rr);
out.outer = P({ s: 3000, lane: "4", lane_how: "outer" }, { s: 3420 }, IN.rr);
out.nothing = P({ s: 3000 }, { s: 3420 }, { poles: [], mps: [] });
out.carried = P({ s: 3000, mp: 27.3, pos_src: "traintime" }, { s: 3000 + 1609.344 }, { poles: [], mps: [] });   // #378's mpAt
""", {"t": t, "rr": rr})
    at = model.ref.at_s(3420)
    assert out["drawn"]["text"] == f"Trk 2 · near pole {at['pole']} · MP {at['mp']:.1f}" and at["pole"] == "234"
    assert out["hub"]["text"] == "Trk 2 · near pole 230 · MP 21.2"
    assert out["drawn"]["title"].startswith("Track 2: posted for Rye\nPole: CB 234")
    assert out["empty"]["text"] == out["none"]["text"] == "Trk 2 · near pole 230 · MP 21.2"   # the hub's own
    assert out["ttOnly"] == {"k": "place", "text": "MP 27.3", "title": "Milepost from Grand Central, where the train is drawn"}
    assert out["held"]["text"].startswith("Trk 2? · ") and out["held"]["title"].startswith("Track: last said for Rye\n")
    assert "Trk" not in out["outer"]["text"] and out["outer"]["title"].startswith("Track not known; drawn on outer track\n")
    assert out["nothing"] is None
    assert out["carried"]["text"] == "MP\u00a028.3"                         # no GIS milepost: the hub's, carried to the drawn s


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_train_on_a_held_or_default_track_is_dashed_not_faded(tmp_path, model):
    """#374 re-review: an unsure track (held, or the outer default) is a dashed stripe down the body at full opacity;
    faded stays for a stale feed alone, and a stale train has no cab over its body (a brighter cab at partial
    opacity)."""
    tracks = {k: [[[round(a, 7), round(b, 7), round(c, 1)] for a, b, c in lane.coords] for lane in lanes]
              for k, lanes in model.track_lanes.items()}
    out = page(tmp_path, """
const run = (how, stale) => TrainLayer.onTrack({ id: "x", s: 3000, d: 1, lane: "2", lane_how: how, stale, lat: 41, lon: -73.7, bearing: 90 },
                                                 null, null, IN.tracks).cars.map((f) => [f.properties.kind, f.properties.unsure]);
out.said = run("said"); out.held = run("held"); out.outer = run("outer"); out.stale = run("said", true);
""", {"tracks": tracks})
    assert not any(u for _, u in out["said"]) and all(u for _, u in out["held"]) and all(u for _, u in out["outer"])
    assert [k for k, _ in out["said"]][:2] == ["body", "cab"] and "cab" not in [k for k, _ in out["stale"]]
    js = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "trains.js").read_text()
    assert 'const fade = ["case", ["get", "stale"], STALE_OPACITY, 1];' in js and "UNSURE_OPACITY" not in js
    unsure = js[js.index('id: "tr-unsure"'):js.index('id: "tr-gap"')]
    assert '["==", ["get", "unsure"], true]' in unsure and '"line-dasharray"' in unsure    # (the icon's features have none)
    assert js.index('id: "tr-cab", type') < js.index('id: "tr-unsure"') < js.index('id: "tr-gap"') and '"tr-unsure", "tr-gap"]' in js
    assert '"Hollow: estimated · faded: stale feed · dashed: track not certain "' in js
    # the GTFS-only box: the next stop's track said as the next stop's, never as the drawn one
    assert "`Next ${t.next_stop}${t.next_time ? ` ${hm(t.next_time)}` : \"\"}${t.track ? ` trk\\u00a0${t.track}` : \"\"}`" in js
    assert "`Trk ${t.track}`" not in js


# ---- the poles' look and their layer (#374) -------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_page_draws_each_structure_as_a_tick_with_a_tag_beyond_its_end(tmp_path):
    """TrainTime's look: the hub's tick as a line, the number on a yellow tag that sits beyond its east end (a
    milepost's label reads to the west of it: at the west end the two collided and the tag was left out; north-south:
    the first end, above or below it); no tick: the tag at the structure. The tags outrank the mileposts' labels."""
    out = page(tmp_path, """
out.f = TrainLayer.poleFeatures([{ id: "236", lon: -73.70, lat: 41.0, tick: [[-73.70, 41.00008], [-73.70, 40.99992]] },
                                 { id: "237", lon: -73.69, lat: 41.0, tick: [[-73.69011, 41.0], [-73.68989, 41.0]] },
                                 { id: "238", lon: -73.68, lat: 41.0 },
                                 { id: "239", lon: -73.67, lat: 41.0, tick: [[-73.66989, 41.00002], [-73.67011, 40.99998]] }]);
""", {})
    f = out["f"]
    assert [(x["geometry"]["type"], x["properties"]["kind"], x["properties"]["id"]) for x in f] == [
        ("LineString", "pole", "236"), ("Point", "pole-tag", "236"), ("LineString", "pole", "237"), ("Point", "pole-tag", "237"),
        ("Point", "pole-tag", "238"), ("LineString", "pole", "239"), ("Point", "pole-tag", "239")]
    assert f[1]["geometry"]["coordinates"] == [-73.70, 41.00008] and f[1]["properties"]["anchor"] == "bottom"   # above the north end
    assert f[3]["geometry"]["coordinates"] == [-73.68989, 41.0] and f[3]["properties"]["anchor"] == "left"     # right of the east end
    assert f[4]["geometry"]["coordinates"] == [-73.68, 41.0]
    assert f[6]["geometry"]["coordinates"] == [-73.66989, 41.00002] and f[6]["properties"]["anchor"] == "left"   # east end first
    js = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "trains.js").read_text()
    pole = js[js.index('map.addLayer({ id: "tr-pole", type: "line"'):js.index('map.addLayer({ id: "tr-mp"')]
    pole += js[js.index('map.addLayer({ id: "tr-pole-label"'):js.index("for (const id of CLICKABLE)")]
    assert js.index('id: "tr-mp-label"') < js.index('id: "tr-pole-label"')   # placed later in the stack: placed first, wins
    assert pole.count("minzoom: POLE_ZOOM") == 2 and "const POLE_ZOOM = 16" in js and 'POLE_TAG = "#F2D13A"' in js
    assert '"icon-image": POLE_CHIP, "icon-text-fit": "both"' in pole and '"text-optional": false' in pole
    assert "allow-overlap" not in pole and "ignore-placement" not in pole            # a tag that would overlap: left out
    assert 'type: "circle", source: SRC_REF, filter: isPole' not in js                # no dots any more


MAPS = r"""
const fs = require("fs");
global.window = global;
const store = JSON.parse(process.argv[4] || "{}");
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
                        removeItem: (k) => { delete store[k]; } };
const el = () => { const n = { children: [], dataset: {}, style: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  listeners: {}, hidden: false, append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; },
  setAttribute() {}, addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
  querySelector: () => null, querySelectorAll: () => [], contains: () => false, focus() {}, getContext: () => null };
  return n; };
global.document = { visibilityState: "visible", addEventListener() {}, documentElement: {}, createElement: el, activeElement: null };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
global.fetch = () => new Promise(() => {});
global.CSS = { escape: (s) => s, supports: () => false };
global.performance = { now: () => 0 };
global.setInterval = () => 0;
const map = { on() {}, once() {}, off() {}, isStyleLoaded: () => false, loaded: () => false, getZoom: () => 17, getLayer: () => null,
              getSource: () => null, getStyle: () => ({ layers: [] }), getContainer: () => ({}), getCanvas: () => ({ style: {} }) };
eval(fs.readFileSync(process.argv[2], "utf8"));
eval(fs.readFileSync(process.argv[3], "utf8"));
const control = MapLayers.attach(map, el(), el(), {});
const tm = control.groups[0];
const find = (n, key) => { if (n.dataset && n.dataset.key === key) return n; for (const c of n.children || []) { const f = find(c, key); if (f) return f; } return null; };
const out = { on: control.on(TrainLayer.REF_KEY, false), keys: tm.keys(), effective: control.effective()[TrainLayer.REF_KEY] };
const box = find(tm.panel(), TrainLayer.REF_KEY);
out.checked = box.checked;
out.boxes = (function count(n) { return (n.dataset && n.dataset.key === TrainLayer.REF_KEY ? 1 : 0) + (n.children || []).reduce((a, c) => a + count(c), 0); })(tm.panel());
if (process.argv[5] === "tick") { box.checked = true; for (const f of box.listeners.change) f(); }
out.after = control.on(TrainLayer.REF_KEY, false);
out.store = store;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_poles_and_mileposts_is_a_layer_off_by_default_and_kept_per_viewer(tmp_path):
    """#374 "set it as a layer so it's not always shown": "Poles · mileposts" is a box in the map's Layers control
    (maplayers.js, the Trains group's keys), off for a viewer who never chose, and their choice kept
    (localStorage "sizeup.mapLayers", as every layer's): ticked, it is on again on the next page; one box only."""
    h = tmp_path / "maps.js"
    h.write_text(MAPS)
    static = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"

    def run(store, act=""):
        r = subprocess.run([NODE, str(h), str(static / "maplayers.js"), str(static / "trains.js"), json.dumps(store), act],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout.strip().splitlines()[-1])
    first = run({})
    assert first["on"] is False and first["effective"] is False and first["checked"] is False and first["boxes"] == 1
    assert ["railref", False] in first["keys"]
    ticked = run({}, "tick")
    assert ticked["after"] is True and json.loads(ticked["store"]["sizeup.mapLayers"])["overview"]["railref"] is True
    again = run(ticked["store"])
    assert again["on"] is True and again["checked"] is True
