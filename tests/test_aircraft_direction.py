"""Which way an aircraft near HPN is going (#279): departures off HPN were labelled inbound. Arrivals and departures
on the same runway share its heading, so PhaseMemory decides from the vertical rate, the distance to the field over
the recent reports and whether it was on the field lately, never from the runway or approach it lines up with alone;
when those disagree it says "near" ("near HPN" on the map).

Every track here is synthetic: built from HPN's runway ends and its published approach paths, one report every 5 s."""

import json
import math

from sizeup import aircraft as A
from sizeup.aircraft import ARRIVING, DEPARTING, GROUND, LOW, NEAR, OVERFLIGHT, PhaseMemory, Plane, Procedures, classify

PROC = Procedures()
T0 = 1792003000.0
STEP_S = 5.0
THR34 = (41.06000083, -73.70140237)
THR16 = (41.07555312, -73.71330558)
RWY34 = A.bearing(*THR34, *THR16)          # ~330 true: landing on 34, and taking off on it
ELEV = A.ELEV_FT


def move(lat: float, lon: float, brg: float, dist_nm: float) -> tuple[float, float]:
    r = math.radians(brg)
    return lat + dist_nm / 60 * math.cos(r), lon + dist_nm / 60 * math.sin(r) / math.cos(math.radians(lat))


def report(lat, lon, alt, trk, gs, vr, **kw) -> Plane:
    return Plane(hex="~279a", lat=lat, lon=lon, alt_baro=alt, track=trk, gs=gs, baro_rate=vr, category="A2", **kw)


def fly(reports, memory=None, t0=T0):
    """Each report in turn, STEP_S apart, through one memory: the phases it reads."""
    memory = memory or PhaseMemory(PROC)
    return [memory.phase(r, t0 + k * STEP_S) for k, r in enumerate(reports)], memory


def straight(start, brg, alt, gs, vr, n, trk=None, **kw):
    """n reports from start along brg at gs kt, the altitude changing by vr ft/min."""
    out, (lat, lon) = [], start
    for _ in range(n):
        out.append(report(lat, lon, round(alt), trk if trk is not None else brg, gs, vr, **kw))
        lat, lon = move(lat, lon, brg, gs * STEP_S / 3600)
        alt += vr * STEP_S / 60
    return out


def on_ground_at_34():
    return [report(*THR34, "ground", RWY34, 5.0, 0.0) for _ in range(3)]


def takeoff_roll():
    """From the 34 threshold up the runway, ADS-B saying airborne and the altitude the field's: the reports that used to
    read as "lined up on runway 34"."""
    return [report(*move(*THR34, RWY34, d), int(ELEV), RWY34, gs, 0.0) for d, gs in ((0.02, 70.0), (0.15, 100.0), (0.35, 125.0))]


def climb_out_34(n=10):
    start = move(*THR16, RWY34, 0.1)
    return straight(start, RWY34, ELEV + 150, 140.0, 1500.0, n)


def kinds(phases):
    return [p.kind for p in phases]


# ---- departures ---------------------------------------------------------------------------------------------------------


def test_a_straight_out_departure_off_34_is_departed_never_inbound():
    roll = takeoff_roll()
    assert any(classify(r, PROC).kind == ARRIVING for r in roll)          # the snapshot alone lines it up on 34: the bug
    phases, mem = fly(on_ground_at_34() + roll + climb_out_34())
    assert ARRIVING not in kinds(phases)
    assert kinds(phases[:6]) == [GROUND] * 6                                  # parked, then the roll: on the field
    assert kinds(phases[6:]) == [DEPARTING] * 10
    last = phases[-1]
    assert 1.0 < last.dist_nm < 3.0 and last.approach is None
    assert A.approach_phase(climb_out_34()[-1], mem._departed.get("~279a"), T0 + 20 * STEP_S, 40.0) == A.OUTBOUND


def test_a_departure_first_seen_airborne_is_still_departed():
    phases, _ = fly(climb_out_34())                                       # the feed missed the ground and the roll
    assert set(kinds(phases)) == {DEPARTING}


def test_a_departure_turning_west_stays_departed():
    out = climb_out_34(8)
    lat, lon, alt = out[-1].lat, out[-1].lon, out[-1].alt_baro
    turn = []
    for k in range(1, 7):                                                  # 330 -> 270 over 30 s, still climbing
        trk = RWY34 - 10 * k
        lat, lon = move(lat, lon, trk, 140 * STEP_S / 3600)
        alt += 125
        turn.append(report(lat, lon, alt, trk, 140.0, 1500.0))
    west = straight((lat, lon), 270.0, alt, 150.0, 1200.0, 12)
    phases, _ = fly(on_ground_at_34() + takeoff_roll() + out + turn + west)
    assert ARRIVING not in kinds(phases) and NEAR not in kinds(phases)
    assert set(kinds(phases[6:])) == {DEPARTING}


# ---- arrivals -----------------------------------------------------------------------------------------------------------


def final_34(from_nm=5.0, to_nm=0.3, gs=130.0):
    """The 34 final: on the extended centreline, descending on a 3 degree path to the threshold."""
    out, d = [], from_nm
    while d >= to_nm:
        out.append(report(*move(*THR34, RWY34 + 180, d), round(ELEV + 50 + 318 * d), RWY34, gs, -700.0))
        d -= gs * STEP_S / 3600
    return out


def test_an_arrival_on_the_34_final_is_inbound_on_its_approach():
    phases, _ = fly(final_34())
    assert set(kinds(phases)) == {ARRIVING}
    assert all(p.runway == "34" for p in phases) and phases[0].approach in ("ILS 34", "LOC 34", "RNAV (GPS) Y 34")
    assert phases[-1].dist_nm < 0.5


def test_the_rnp_z_34_lineup_is_still_matched():
    gj = json.loads(A.PROCEDURES_FILE.read_text())
    path = next(f for f in gj["features"] if f["properties"].get("kind") == "approach_path"
                and f["properties"].get("name") == "RNAV (RNP) Z 34" and f["properties"].get("segment") == "final")
    pts = [(lat, lon) for lon, lat in path["geometry"]["coordinates"]]
    to_go = [sum(A.nm(*a, *b) for a, b in zip(pts[i:], pts[i + 1:])) for i in range(len(pts))]
    reports = []
    for i in range(len(pts) - 1):
        if not 1.0 < to_go[i] < 8.0:
            continue                                    # the curved part over Port Chester and the short final
        a, b = pts[i], pts[i + 1]
        reports.append(report(*a, round(ELEV + 300 * to_go[i]), A.bearing(*a, *b), 140.0, -800.0))
    assert len(reports) >= 5
    phases, _ = fly(reports)
    assert set(kinds(phases)) == {ARRIVING} and {p.approach for p in phases} == {"RNAV (RNP) Z 34"}


def test_a_go_around_is_near_hpn_not_departed_or_inbound():
    final = final_34(to_nm=0.6)
    last = final[-1]
    climb = straight((last.lat, last.lon), RWY34, last.alt_baro, 130.0, 1000.0, 24)  # over the field and on north
    phases, _ = fly(final + climb)
    assert set(kinds(phases[:len(final)])) == {ARRIVING}
    after = kinds(phases[len(final):])
    assert set(after) == {NEAR}, after


def test_a_touch_and_go_lands_then_departs():
    final = final_34(to_nm=0.1)
    roll = [report(*move(*THR34, RWY34, d), int(ELEV), RWY34, 90.0, 0.0) for d in (0.25, 0.45, 0.65)]
    roll.append(report(*move(*THR34, RWY34, 0.85), "ground", RWY34, 95.0, 0.0))
    up = straight(move(*THR16, RWY34, 0.1), RWY34, ELEV + 200, 100.0, 900.0, 14)
    phases, _ = fly(final + roll + up)
    k = kinds(phases)
    assert set(k[:len(final)]) == {ARRIVING}
    assert GROUND in k[len(final):len(final) + len(roll)]
    assert set(k[len(final) + len(roll):]) == {DEPARTING}


# ---- neither --------------------------------------------------------------------------------------------------------------


def test_an_overflight_is_never_arriving_or_departing():
    high = straight(move(*A.ARP, 150.0, 4.0), 330.0, 8000, 300.0, 0.0, 16)   # over the field on the runway's line
    phases, _ = fly(high)
    assert set(kinds(phases)) == {OVERFLIGHT}
    lower = straight(move(*A.ARP, 150.0, 4.0), 330.0, 3500, 200.0, 0.0, 24)  # under the ceiling: low traffic
    phases, _ = fly(lower)
    assert set(kinds(phases)) == {LOW}


def test_a_level_aircraft_passing_near_is_not_inbound_or_departed():
    # level at 1,500 ft, passing about a mile south-east of the field: closing, then opening
    phases, _ = fly(straight(move(*A.ARP, 225.0, 2.5), 75.0, 1500, 110.0, 0.0, 30))
    assert set(kinds(phases)) == {LOW}
    # level and lined up on the 34 final, first seen: the runway alone does not make it an arrival
    first = report(*move(*THR34, RWY34 + 180, 2.0), 1500, RWY34, 110.0, 0.0)
    assert classify(first, PROC).kind == ARRIVING and PhaseMemory(PROC).phase(first, T0).kind == NEAR


def test_disagreeing_signals_read_near():
    mem = PhaseMemory(PROC)
    # lined up on the 34 final but climbing: not inbound
    up = report(*move(*THR34, RWY34 + 180, 1.5), 1000, RWY34, 120.0, 400.0)
    assert classify(up, PROC).kind == ARRIVING and mem.phase(up, T0).kind == NEAR
    # climbing away close in, but closing on the field over the last reports (it turned back): not departed
    mem = PhaseMemory(PROC)
    toward = straight(move(*A.ARP, 330.0, 3.0), 150.0, 1500, 120.0, 0.0, 6)
    phases, mem = fly(toward, mem)
    back = report(*move(*A.ARP, 330.0, 2.0), 1600, 330.0, 120.0, 800.0)
    assert classify(back, PROC).kind == DEPARTING and mem.phase(back, T0 + 6 * STEP_S).kind == NEAR


def test_the_layer_reads_a_departure_departing_through_its_track_store(tmp_path):
    """Through the track store and the page's answer, a look every report; then a first look at a climb-out whose
    trail is already 40 s long: the trend from the trail's distances (it is opening)."""
    ly = A.AircraftLayer(feeds=(), registry=False, cache_dir=tmp_path, procedures=PROC, log=lambda m: None)
    for k, r in enumerate(on_ground_at_34() + takeoff_roll() + climb_out_34(8)):
        ly.tracks.update(A.Snapshot(T0 + k * STEP_S, (Plane(**{**r.__dict__, "seen_pos": 0.0}),)))
        v = ly._view()
        assert v[0]["phase"] != ARRIVING
    assert v[0]["phase"] == DEPARTING and v[0]["appr"] is None
    tracks = A.Tracks()
    for k, r in enumerate(climb_out_34(9)):
        tracks.update(A.Snapshot(T0 + k * STEP_S, (Plane(**{**r.__dict__, "seen_pos": 0.0}),)))
    t = tracks.tracks["~279a"]
    mem = PhaseMemory(PROC)
    assert mem._trend("~279a", T0 + 8 * STEP_S, A.nm(*A.ARP, t.plane.lat, t.plane.lon), t.trail) == "opening"
    assert NEAR in A.SHOWN
