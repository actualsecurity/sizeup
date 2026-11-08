"""Which way an aircraft near HPN is going (#279): departures off HPN were labelled inbound. Arrivals and departures
on the same runway share its heading, so PhaseMemory decides from the vertical rate, the distance to the field (and
along a published approach) over the recent reports and whether it was on the field lately, never from the runway or
approach it lines up with alone; when those disagree it says "near" ("near HPN" on the map).

Every track here is synthetic and continuous: flown leg by leg from HPN's runway ends and published approach paths,
one report every 5 s, the pressure altitude optionally off by the day's altimeter setting, the vertical rate
optionally not reported."""

import json
import math
import random

import pytest

from sizeup import aircraft as A
from sizeup.aircraft import ARRIVING, DEPARTING, GROUND, LOW, NEAR, OVERFLIGHT, PhaseMemory, Plane, Procedures, classify

PROC = Procedures()
T0 = 1792003000.0
STEP_S = 5.0
HEX = "~279a"                                   # not an ICAO address: no registry lookups, no real aircraft
END = {e: (lat, lon) for _, e, lat, lon in A.RUNWAY_ENDS}
HDG = {e: A.bearing(*END[e], *END[o]) for e, o in (("34", "16"), ("16", "34"), ("29", "11"), ("11", "29"))}
ELEV = A.ELEV_FT
LAG = math.ceil((A.VS_SMOOTH_S + A.SWITCH_S) / STEP_S)    # reports a change of mind may take: the smoothed rate, the debounce


def move(lat: float, lon: float, brg: float, dist_nm: float) -> tuple[float, float]:
    r = math.radians(brg)
    return lat + dist_nm / 60 * math.cos(r), lon + dist_nm / 60 * math.sin(r) / math.cos(math.radians(lat))


def fly_legs(start, trk, alt, gs, legs, baro=0.0, vr_reported=True, mode_c=False, category="A2"):
    """Reports every STEP_S along legs of (seconds, {vr: ft/min, turn: deg/s, gs: target kt, ground: bool}): the
    position, track, speed and altitude change continuously (speed by at most 4 kt/s). alt is true MSL; the report's
    pressure altitude is off by baro; mode_c: in 100 ft steps, jittering across a step (+/- 60 ft of noise)."""
    lat, lon = start
    out = []
    noise = random.Random(279)
    for seconds, leg in legs:
        for _ in range(round(seconds / STEP_S)):
            vr = leg.get("vr", 0.0)
            reported = round(alt + baro)
            if mode_c:
                reported = int(round((alt + baro + noise.uniform(-60, 60)) / 100) * 100)
            out.append(Plane(hex=HEX, lat=lat, lon=lon, track=trk % 360, gs=gs, category=category,
                             alt_baro="ground" if leg.get("ground") else reported,
                             baro_rate=vr if vr_reported else None))
            trk += leg.get("turn", 0.0) * STEP_S / 2
            lat, lon = move(lat, lon, trk, gs * STEP_S / 3600)
            trk += leg.get("turn", 0.0) * STEP_S / 2
            gs += max(-20.0, min(20.0, leg.get("gs", gs) - gs))
            alt = max(ELEV, alt + vr * STEP_S / 60)
    return out


def phases_of(reports, memory=None, t0=T0):
    memory = memory or PhaseMemory(PROC)
    return [memory.phase(r, t0 + k * STEP_S) for k, r in enumerate(reports)], memory


def kinds(phases):
    return [p.kind for p in phases]


def changes(ks):
    return sum(1 for a, b in zip(ks, ks[1:]) if a != b)


def takeoff(end, seen_from_nm, baro=0.0, vr_reported=True, climb_s=90, extra=()):
    """First seen rolling seen_from_nm up runway `end` (no taxi seen), lifting off, climbing straight out."""
    start = move(*END[end], HDG[end], seen_from_nm)
    return fly_legs(start, HDG[end], ELEV, 90.0, [(15, {"gs": 140.0}), (climb_s, {"vr": 1500.0, "gs": 150.0}), *extra],
                    baro=baro, vr_reported=vr_reported)


def airborne_from(reports):
    """The first report off the runway (by the true climb, whether the rate is reported or not)."""
    return next(i for i, (a, b) in enumerate(zip(reports, reports[1:])) if b.alt_baro != a.alt_baro)


# ---- departures: never inbound -------------------------------------------------------------------------------------


@pytest.mark.parametrize("baro", [0.0, 150.0, 300.0])
def test_a_34_departure_first_seen_mid_runway_is_departed_whatever_the_altimeter(baro):
    reports = takeoff("34", 0.3, baro=baro)
    assert any(classify(r, PROC).kind == ARRIVING for r in reports[:3])     # the snapshot alone reads "34": the bug
    phases, _ = phases_of(reports)
    k = kinds(phases)
    assert ARRIVING not in k and NEAR not in k, k
    up = airborne_from(reports)
    assert set(k[:up]) == {GROUND} and set(k[up:]) == {DEPARTING}
    assert 1.0 < phases[up + 6].dist_nm < 2.5                                # the issue's 1-2.5 nm out


def test_a_34_departure_turning_west_stays_departed():
    reports = takeoff("34", 0.2, baro=150.0, climb_s=40, extra=[(20, {"vr": 1500.0, "turn": -3.0}), (60, {"vr": 1200.0})])
    k = kinds(phases_of(reports)[0])
    assert ARRIVING not in k and NEAR not in k and set(k[airborne_from(reports):]) == {DEPARTING}
    assert 250 < reports[-1].track < 290


def test_a_departure_with_no_vertical_rate_reported_is_still_departed():
    reports = takeoff("34", 0.3, baro=150.0, vr_reported=False)
    k = kinds(phases_of(reports)[0])
    assert ARRIVING not in k and k[-12:] == [DEPARTING] * 12, k


@pytest.mark.parametrize("end", ["16", "11", "29"])
def test_departures_off_the_other_runways_are_departed(end):
    reports = takeoff(end, 0.2, baro=150.0)
    k = kinds(phases_of(reports)[0])
    assert ARRIVING not in k and NEAR not in k and set(k[airborne_from(reports):]) == {DEPARTING}, k


def test_a_departure_first_seen_airborne_is_departed():
    full = takeoff("34", 0.2)
    reports = full[airborne_from(full) + 2:]                                # the feed missed the ground and the roll
    assert set(kinds(phases_of(reports)[0])) == {DEPARTING}


# ---- arrivals ---------------------------------------------------------------------------------------------------------------


def final_34(from_nm=5.0, gs=130.0, down_to_ft=0.0, baro=0.0, legs_after=(), category="A2"):
    """On the 34 final from from_nm on a 3 degree path to down_to_ft over the threshold, then legs_after."""
    vr = -gs * 318 / 60                                     # 318 ft a nm at gs kt
    s = (from_nm - down_to_ft / 318) * 3600 / gs
    return fly_legs(move(*END["34"], HDG["34"] + 180, from_nm), HDG["34"], ELEV + 318 * from_nm, gs,
                    [(s - s % STEP_S, {"vr": vr}), *legs_after], baro=baro, category=category)


def test_an_arrival_on_the_34_final_reads_inbound_on_its_approach_then_down():
    reports = final_34(legs_after=[(20, {"gs": 60.0}), (15, {"gs": 20.0, "ground": True})])
    phases, _ = phases_of(reports)
    k = kinds(phases)
    n = len(final_34())
    assert set(k[:n]) == {ARRIVING} and phases[0].approach in ("ILS 34", "LOC 34", "RNAV (GPS) Y 34")
    assert all(p.runway == "34" for p in phases[:n]) and k[-1] == GROUND


def test_the_whole_rnp_z_34_final_reads_arriving():
    """The published path opens from the field (6 nm out to 7.6) before it curves in over Port Chester."""
    gj = json.loads(A.PROCEDURES_FILE.read_text())
    path = next(f for f in gj["features"] if f["properties"].get("kind") == "approach_path"
                and f["properties"].get("name") == "RNAV (RNP) Z 34" and f["properties"].get("segment") == "final")
    pts = [(lat, lon) for lon, lat in path["geometry"]["coordinates"]]
    seg = [A.nm(*a, *b) for a, b in zip(pts, pts[1:])]
    total, gs = sum(seg), 140.0
    reports, along = [], 0.0
    while along < total - 0.4:                              # along the path, 5 s apart, to 0.4 nm short of the end
        i, rest = 0, along
        while rest > seg[i]:
            rest -= seg[i]
            i += 1
        a, b = pts[i], pts[i + 1]
        pos = move(*a, A.bearing(*a, *b), rest)
        to_go = total - along
        reports.append(Plane(hex=HEX, lat=pos[0], lon=pos[1], track=A.bearing(*a, *b), gs=gs, category="A2",
                             alt_baro=round(ELEV + 300 * to_go), baro_rate=-gs * 300 / 60))
        along += gs * STEP_S / 3600
    dists = [A.nm(*A.ARP, r.lat, r.lon) for r in reports]
    assert max(dists) > dists[0] + 1.5                                  # it does open from the field on the way
    phases, _ = phases_of(reports)
    assert set(kinds(phases)) == {ARRIVING}, kinds(phases)
    assert all(p.approach == "RNAV (RNP) Z 34" for p in phases if p.dist_nm > 3)


def test_a_go_around_from_short_final_is_near_never_departed():
    reports = final_34(down_to_ft=250.0, legs_after=[(90, {"vr": 1000.0})])
    phases, _ = phases_of(reports)
    k = kinds(phases)
    climb = next(i for i, r in enumerate(reports) if r.baro_rate > 0)
    assert set(k[:climb]) == {ARRIVING} and set(k[climb + LAG:]) == {NEAR} and DEPARTING not in k, k


@pytest.mark.parametrize("baro", [0.0, 150.0])
def test_a_balked_landing_at_50_ft_is_near_never_departed(baro):
    reports = final_34(down_to_ft=50.0, baro=baro, legs_after=[(5, {"vr": 0.0}), (90, {"vr": 1000.0})])
    phases, _ = phases_of(reports)
    k = kinds(phases)
    level = next(i for i, r in enumerate(reports) if r.baro_rate >= 0)
    assert DEPARTING not in k and GROUND not in k and set(k[:level]) == {ARRIVING}, k
    assert set(k[level + LAG:]) == {NEAR}, k


def test_a_touch_and_go_lands_then_departs():
    reports = final_34(down_to_ft=0.0, baro=150.0, legs_after=[(10, {"gs": 80.0, "ground": True}),
                                                                (90, {"vr": 900.0, "gs": 100.0})])
    k = kinds(phases_of(reports)[0])
    n = len(final_34())
    assert set(k[:n]) == {ARRIVING} and set(k[n:n + 2]) == {GROUND} and set(k[n + 2:n + 4]) <= {GROUND, DEPARTING}
    assert set(k[n + 4:]) == {DEPARTING}, k


def test_an_emergency_return_to_34_reads_arriving_on_the_final():
    """Off 34, up to 1,000 ft, a 180 to the left, back down the west side, a 180 onto the 34 final at 2 nm and down:
    within 5 minutes of leaving the runway."""
    legs = [(15, {"gs": 120.0}), (40, {"vr": 1500.0, "gs": 120.0}), (60, {"turn": -3.0}), (95, {}), (60, {"turn": -3.0}),
            (60, {"vr": -650.0, "gs": 110.0})]
    reports = fly_legs(move(*END["34"], HDG["34"], 0.2), HDG["34"], ELEV, 90.0, legs, baro=150.0)
    phases, _ = phases_of(reports)
    k = kinds(phases)
    final = round((15 + 40 + 60 + 95 + 60) / STEP_S)
    assert final * STEP_S < A.FIELD_RECENT_S
    assert A.nm(*END["34"], reports[final].lat, reports[final].lon) < 2.6
    assert ARRIVING not in k[:final]
    assert k[final + LAG:] == [ARRIVING] * (len(k) - final - LAG), k
    assert {p.approach for p in phases[final + LAG:]} <= {"ILS 34", "LOC 34", "RNAV (GPS) Y 34", "RNAV (RNP) Z 34", None}
    assert all(p.runway == "34" for p in phases[final + LAG:] if p.approach)


def test_a_level_departure_turning_back_past_the_field_does_not_flap():
    legs = [(15, {"gs": 120.0}), (40, {"vr": 1500.0, "gs": 120.0}), (60, {"turn": -3.0}), (180, {})]
    reports = fly_legs(move(*END["34"], HDG["34"], 0.2), HDG["34"], ELEV, 90.0, legs, baro=150.0)
    k = kinds(phases_of(reports)[0])
    air = k[airborne_from(reports):]
    assert ARRIVING not in air and DEPARTING in air and changes(air) <= 2, air


# ---- neither --------------------------------------------------------------------------------------------------------------


def test_an_overflight_is_never_arriving_or_departing():
    high = fly_legs(move(*A.ARP, 150.0, 4.0), 330.0, 8000, 300.0, [(100, {})])     # over the field on the runway's line
    assert set(kinds(phases_of(high)[0])) == {OVERFLIGHT}
    lower = fly_legs(move(*A.ARP, 150.0, 4.0), 330.0, 3500, 200.0, [(140, {})])   # under the ceiling: low traffic
    assert set(kinds(phases_of(lower)[0])) == {LOW}


def test_a_level_aircraft_passing_near_is_not_inbound_or_departed():
    across = fly_legs(move(*A.ARP, 225.0, 2.5), 75.0, 1500, 110.0, [(150, {})])     # about a mile south-east, level
    assert set(kinds(phases_of(across)[0])) == {LOW}
    # level and lined up on the 34 final, first seen: the runway alone does not make it an arrival
    lat, lon = move(*END["34"], HDG["34"] + 180, 2.0)
    first = Plane(hex=HEX, lat=lat, lon=lon, alt_baro=1500, track=HDG["34"], gs=110.0, baro_rate=0.0, category="A2")
    assert classify(first, PROC).kind == ARRIVING and PhaseMemory(PROC).phase(first, T0).kind == NEAR
    # lined up on the 34 final but climbing: not inbound
    up = Plane(hex=HEX, lat=lat, lon=lon, alt_baro=1000, track=HDG["34"], gs=120.0, baro_rate=400.0, category="A2")
    assert classify(up, PROC).kind == ARRIVING and PhaseMemory(PROC).phase(up, T0).kind == NEAR


def test_the_layer_reads_a_departure_departing_through_its_track_store(tmp_path):
    """Through the track store and the page's answer, a look every report; then the trend from a trail alone."""
    ly = A.AircraftLayer(feeds=(), registry=False, cache_dir=tmp_path, procedures=PROC, log=lambda m: None)
    reports = takeoff("34", 0.3, baro=150.0)
    for k, r in enumerate(reports):
        ly.tracks.update(A.Snapshot(T0 + k * STEP_S, (Plane(**{**r.__dict__, "seen_pos": 0.0}),)))
        v = ly._view()
        assert v[0]["phase"] != ARRIVING
    assert v[0]["phase"] == DEPARTING and v[0]["appr"] is None
    tracks = A.Tracks()
    for k, r in enumerate(reports):
        tracks.update(A.Snapshot(T0 + k * STEP_S, (Plane(**{**r.__dict__, "seen_pos": 0.0}),)))
    t = tracks.tracks[HEX]
    now = T0 + (len(reports) - 1) * STEP_S
    assert PhaseMemory._trend((), now, A.nm(*A.ARP, t.plane.lat, t.plane.lon), t.trail) == "opening"
    assert NEAR in A.SHOWN


def test_a_misread_arrival_is_cleared_by_a_strong_climb_away():
    """Lined up and level (never seen descending) is all an arrival reading had: climbing hard away clears it."""
    mem = PhaseMemory(PROC)
    lat, lon = move(*END["34"], HDG["34"] + 180, 1.5)
    level = fly_legs((lat, lon), HDG["34"], ELEV + 600, 120.0, [(30, {})])
    k = kinds(phases_of(level, mem)[0])
    assert ARRIVING in k and not mem._descended.get(HEX)                      # level and closing: inbound, per #279
    last = level[-1]
    climb = fly_legs(move(last.lat, last.lon, HDG["34"], 0.1), HDG["34"], ELEV + 600, 130.0, [(120, {"vr": 1500.0})])
    later = kinds(phases_of(climb, mem, T0 + len(level) * STEP_S)[0])
    assert ARRIVING not in later[LAG:] and later[-1] == DEPARTING and HEX not in mem._inbound, later


@pytest.mark.parametrize("category", ["A2", ""])
def test_a_landing_with_no_ground_flag_then_a_taxi_and_parked_reads_ground_never_departed(category):
    """Many GA transponders never say "ground": landed at a +200 ft altimeter offset, slowed, off the runway at
    15 kt, parked for 25 minutes; with its category, and with none (Mode S, MLAT, older ADS-B)."""
    reports = final_34(baro=200.0, category=category, legs_after=[(20, {"gs": 40.0}), (10, {"gs": 15.0, "turn": 9.0}), (60, {"gs": 15.0}),
                                                (10, {"gs": 0.0}), (1500, {"gs": 0.0})])
    phases, mem = phases_of(reports)
    k = kinds(phases)
    n = len(final_34())
    assert set(k[:n]) == {ARRIVING} and DEPARTING not in k and NEAR not in k, k
    assert k[-300:] == [GROUND] * 300 and HEX not in mem._departed


@pytest.mark.parametrize("gs,repeat", [(90.0, False), (90.0, True), (140.0, True)])
def test_a_level_rnp_z_34_arrival_on_the_opening_stretch_does_not_flap(gs, repeat):
    """Reports 2 s apart (at 90 kt: 0.05 nm a report), or every position sent twice (a merged feed with no fresh fix),
    level, along the first part of the RNP Z 34 final, which leads away from the field."""
    gj = json.loads(A.PROCEDURES_FILE.read_text())
    path = next(f for f in gj["features"] if f["properties"].get("kind") == "approach_path"
                and f["properties"].get("name") == "RNAV (RNP) Z 34" and f["properties"].get("segment") == "final")
    (lon0, lat0), (lon1, lat1) = path["geometry"]["coordinates"][:2]
    trk = A.bearing(lat0, lon0, lat1, lon1)
    mem, k = PhaseMemory(PROC), []
    for i in range(60):                                     # 2 minutes
        lat, lon = move(lat0, lon0, trk, gs * 2 * (i // 2 * 2 if repeat else i) / 3600)
        r = Plane(hex=HEX, lat=lat, lon=lon, track=trk, gs=gs, category="A2", alt_baro=round(ELEV + 4000), baro_rate=0.0)
        k.append(mem.phase(r, T0 + 2 * i).kind)
    d = [A.nm(*A.ARP, *move(lat0, lon0, trk, gs * 2 * (i // 2 * 2 if repeat else i) / 3600)) for i in (0, 59)]
    assert d[1] > d[0] + 0.15                               # opening from the field all the while
    assert classify(r, PROC).approach == "RNAV (RNP) Z 34"
    assert changes(k) <= 1 and k[-1] == ARRIVING and k.count(ARRIVING) >= 45, k


def test_mode_c_altitude_steps_make_no_vertical_rate():
    """No rate reported, the altitude in 100 ft steps jittering across one: a level final stays put once it reads
    arriving; a takeoff roll reads on the ground throughout."""
    level = fly_legs(move(*END["34"], HDG["34"] + 180, 6.0), HDG["34"], ELEV + 1500, 120.0, [(150, {})],
                     vr_reported=False, mode_c=True)
    assert len({r.alt_baro for r in level}) == 2
    k = kinds(phases_of(level)[0])
    assert changes(k) <= 1 and k[-1] == ARRIVING, k
    roll = fly_legs(move(*END["34"], HDG["34"], 0.2), HDG["34"], ELEV, 60.0, [(20, {"gs": 130.0})], baro=150.0,
                    vr_reported=False, mode_c=True)
    assert len({r.alt_baro for r in roll}) == 2
    assert set(kinds(phases_of(roll)[0])) == {GROUND}


@pytest.mark.parametrize("category", ["A7", "", "B4"])
def test_a_slow_helicopter_orbiting_or_hovering_near_the_field_is_never_on_the_ground(category):
    """Flying slow and low near HPN (an orbit at 300 ft and 40 kt 1.5 nm out, a hover at 200-380 ft): never "on the
    ground at HPN" (the alarm would say so of an airborne helicopter in trouble), never departing."""
    orbit = fly_legs(move(*A.ARP, 200.0, 1.5), 90.0, ELEV + 300, 40.0, [(300, {"turn": 4.0})], category=category)
    assert max(A.nm(*A.ARP, r.lat, r.lon) for r in orbit) < 2.0
    k = kinds(phases_of(orbit)[0])
    assert GROUND not in k and DEPARTING not in k and ARRIVING not in k, k
    hover = [Plane(hex=HEX, lat=move(*A.ARP, 120.0, 0.8)[0], lon=move(*A.ARP, 120.0, 0.8)[1], track=0.0, gs=0.0,
                   alt_baro=round(ELEV + 200 + 180 * abs(math.sin(i / 7))), baro_rate=0.0, category=category) for i in range(60)]
    assert set(kinds(phases_of(hover)[0])) <= {LOW, NEAR}       # (its track points up the 34 final: near, at most)
    # lifted off the pad here (on the ground, then up), then hovering at 300 ft: flying, not on the ground
    pad = [Plane(hex=HEX, lat=hover[0].lat, lon=hover[0].lon, gs=0.0, alt_baro="ground", category=category)] * 3
    up = [Plane(**{**r.__dict__, "alt_baro": round(ELEV + 300)}) for r in hover[:40]]
    k = kinds(phases_of(pad + up)[0])
    assert k[:3] == [GROUND] * 3 and GROUND not in k[3:], k


def test_a_slow_helicopter_descent_reads_ground_only_on_touchdown():
    """Down to the field from 800 ft at 30 kt and 400 ft/min: on the ground only below ~150 ft, or once it says so."""
    down = fly_legs(move(*A.ARP, 300.0, 0.9), 120.0, ELEV + 800, 30.0, [(110, {"vr": -400.0, "gs": 20.0})], category="A7")
    land = [Plane(**{**down[-1].__dict__, "alt_baro": "ground", "gs": 0.0, "baro_rate": 0.0})] * 3
    phases, _ = phases_of(down + land)
    for r, p in zip(down, phases):
        assert p.kind != GROUND or r.alt_baro - ELEV < 150, (r.alt_baro, p.kind)
    assert kinds(phases)[-3:] == [GROUND] * 3 and DEPARTING not in kinds(phases)


@pytest.mark.parametrize("category,agl,gs,trk", [("A7", 300, 60.0, None), ("A7", 380, 45.0, None), ("", 300, 60.0, None),
                                                 ("B4", 300, 45.0, None), ("A7", 300, 60.0, 60.0), ("A2", 300, 120.0, None)])
def test_a_low_pass_over_the_runway_is_never_on_the_ground_or_departed(category, agl, gs, trk):
    """Along 16/34 at a few hundred feet (helicopters cross HPN like this; a fixed-wing low pass at approach speed),
    or across it: on the runway's outline below 400 ft, but not a roll."""
    if trk is None:                                         # along the runway, from 1.5 nm south of the 34 end
        start, trk = move(*END["34"], HDG["34"] + 180, 1.5), HDG["34"]
    else:                                                   # across it, through the middle
        start = move(*A.ARP, trk + 180, 1.5)
    reports = fly_legs(start, trk, ELEV + agl, gs, [(round(3.5 * 3600 / gs / STEP_S) * STEP_S, {})], category=category)
    assert any(A.on_runway(r.lat, r.lon) for r in reports)
    k = kinds(phases_of(reports)[0])
    assert GROUND not in k and DEPARTING not in k, k


# ---- what real feeds do (#279's replay of an hour of HPN traffic) ------------------------------------------------------


def test_a_departure_first_seen_climbing_off_the_runway_is_departed_from_the_first_report():
    """No roll seen: the first report is 110 ft up over runway 34, climbing (the snapshot reads it on the RNP Z 34)."""
    reports = fly_legs(move(*END["34"], HDG["34"], 0.25), HDG["34"], ELEV + 110, 80.0,
                       [(60, {"vr": 800.0, "gs": 100.0})], baro=100.0, category="A1")
    k = kinds(phases_of(reports)[0])
    assert k == [DEPARTING] * len(k), k


def test_a_noisy_reported_rate_does_not_flip_a_level_arrival():
    """Light aircraft report a rate that jumps +-450 ft/min from report to report while level: inbound on the 34
    final at 2,000 ft, it reads arriving and stays so."""
    reports = fly_legs(move(*END["34"], HDG["34"] + 180, 7.0), HDG["34"], ELEV + 1600, 110.0, [(150, {})], category="A1")
    noisy = [Plane(**{**r.__dict__, "baro_rate": (448.0, -192.0, 384.0, -448.0, 128.0)[i % 5]}) for i, r in enumerate(reports)]
    k = kinds(phases_of(noisy)[0])
    assert changes(k) <= 1 and k[-1] == ARRIVING, k


def test_a_stuck_reported_rate_through_a_takeoff_is_not_ground_once_up():
    """A feed's rate stuck at +128 ft/min while the altitude climbs 275 ft in 5 s: the altitude says climbing."""
    reports = takeoff("34", 0.2, baro=100.0)
    stuck = [Plane(**{**r.__dict__, "baro_rate": 128.0}) for r in reports]
    k = kinds(phases_of(stuck)[0])
    up = airborne_from(reports)
    assert ARRIVING not in k and GROUND not in k[up + 2:] and k[-1] == DEPARTING, k



def test_an_emergency_is_never_held_by_the_debounce_and_a_held_phase_has_a_fresh_distance(monkeypatch):
    """Squawking 7700 off 34 and turning back to land: its phase changes on the report the reading does (the alarm and
    the pre-alert read it); without the emergency the change waits SWITCH_S, the held phase with this report's
    distance."""
    legs = [(15, {"gs": 120.0}), (40, {"vr": 1500.0, "gs": 120.0}), (60, {"turn": -3.0}), (95, {}), (60, {"turn": -3.0}),
            (60, {"vr": -650.0, "gs": 110.0})]
    reports = fly_legs(move(*END["34"], HDG["34"], 0.2), HDG["34"], ELEV, 90.0, legs, baro=150.0)
    sos = [Plane(**{**r.__dict__, "squawk": "7700"}) for r in reports]
    held = phases_of(reports)[0]
    emergency = kinds(phases_of(sos)[0])
    monkeypatch.setattr(A, "SWITCH_S", 0.0)
    raw = phases_of(reports)[0]
    assert emergency == kinds(raw)
    assert kinds(held) != kinds(raw) and ARRIVING in kinds(raw)
    first = kinds(raw).index(ARRIVING)
    assert emergency[first] == ARRIVING and kinds(held)[first] != ARRIVING
    for r, h, x in zip(reports, held, raw):
        if h.kind != x.kind:                                   # held: this report's distance, not the old one
            want = A.nm(*END[h.runway], r.lat, r.lon) if h.runway else A.nm(*A.ARP, r.lat, r.lon)
            assert h.dist_nm == pytest.approx(want)


@pytest.mark.parametrize("from_nm", [1.5, -0.3])
def test_a_slow_fixed_wing_low_pass_along_the_runway_is_not_a_roll(from_nm):
    """Level at 200 ft and 90 kt along 34, never descending: not on the ground (a roll speeds up), never departing;
    first seen over the runway it may read ground for that one report, not after."""
    reports = fly_legs(move(*END["34"], HDG["34"] + 180, from_nm), HDG["34"], ELEV + 200, 90.0, [(90, {})])
    assert sum(A.on_runway(r.lat, r.lon, r.track) for r in reports) >= 4
    k = kinds(phases_of(reports)[0])
    assert DEPARTING not in k and GROUND not in k[1:], k


# ---- a tight turn-back (#313) ---------------------------------------------------------------------------------------------


def turnback_16(turn_s, final_s, gs=70.0, baro=0.0):
    """Off 34 (rolling from 40 kt), up to 370 ft 20 s after lift-off, the engine fails: a 180 to the left in turn_s
    gliding, wings level on the 16 heading for final_s, the last report 0.05 nm short of the 16 threshold."""
    sink = -(370 - 20) / (turn_s + final_s) * 60

    def fly(start):
        legs = [(15, {"gs": gs}), (20, {"vr": 1110.0, "gs": gs}), (turn_s, {"vr": sink, "turn": -180.0 / turn_s}),
                (final_s + STEP_S, {"vr": sink})]
        return fly_legs(move(*END["34"], HDG["34"], start), HDG["34"], ELEV, 40.0, legs, baro=baro)

    last = fly(0.0)[-1]                                         # how far beyond the 16 threshold that ends up
    beyond = A.nm(*END["16"], last.lat, last.lon) * math.cos(math.radians(
        A.bearing(*END["16"], last.lat, last.lon) - (HDG["16"] + 180)))
    return fly(0.05 - beyond)


@pytest.mark.parametrize("turn_s,final_s", [(30, 10), (35, 10), (40, 10), (30, 15), (35, 15), (40, 15), (30, 20),
                                            (35, 20), (40, 20)])
@pytest.mark.parametrize("gs,baro", [(65.0, 0.0), (80.0, 150.0)])
def test_a_tight_turn_back_after_an_engine_failure_at_370_ft_reads_arriving(turn_s, final_s, gs, baro):
    """Down before 15 s of closing on the field can show: descending and lined up on the 16 final decides."""
    reports = turnback_16(turn_s, final_s, gs, baro)
    up = airborne_from(reports)
    fail = round((15 + 20) / STEP_S)
    assert reports[fail].alt_baro - ELEV - baro == pytest.approx(370, abs=5) and (fail - up) * STEP_S <= 25
    final = fail + round(turn_s / STEP_S)
    assert abs(A.wrap180(reports[final].track - HDG["16"])) < 1 and A.lined_up(reports[final], "16")
    assert not A.past_threshold(reports[-1].lat, reports[-1].lon, "16")
    k = kinds(phases_of(reports)[0])
    assert set(k[up:fail]) == {DEPARTING} and ARRIVING not in k[:final - 1], k
    assert k[-1] == ARRIVING and k[-2] == ARRIVING, k          # shown (after the debounce) before it is down
    sos = kinds(phases_of([Plane(**{**r.__dict__, "squawk": "7700"}) for r in reports])[0])
    assert sos[final:] == [ARRIVING] * (len(sos) - final), sos  # the emergency watch: from rolling out on final


def test_a_departure_descending_straight_out_is_not_arriving():
    """Under a low ceiling or an airspace shelf: up to 1,200 ft off 34, then down to 800 ft straight out. Descending
    and from the field, but not lined up on a runway end: never arriving."""
    reports = takeoff("34", 0.2, baro=150.0, climb_s=50, extra=[(30, {"vr": -800.0}), (60, {})])
    k = kinds(phases_of(reports)[0])
    assert ARRIVING not in k and k[-1] == DEPARTING, k
