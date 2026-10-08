"""Nearest first: the ETA estimates (sizeup/cad/eta.py), where a rig is now (sizeup/cad/mapview.py
locate(): its station, on its way home from a call it just cleared, a live AVL fix) and the run card
recommendation ordered by them (sizeup/cad/runcards.py), with its Settings > Dispatch knobs.

User, 2026-10-07: "can we use the location information of where these apparatus are normally to
recommend certain ones based on proximity to the next call? whether they just cleared a call next
door should be taken into account too, if within reasonable time before them ending up back in station"
"""

import math
import time

import pytest

from sizeup.cad import eta, mapview, runcards
from sizeup.cad.mapview import locate, place
from sizeup.cad.service import Cad, Settings
from sizeup.cad.store import CadError

T0 = 1_791_400_000.0
FH = {f["name"]: f for f in mapview.load_firehouses()}
HQ, WP, SE, BV, RB = (FH[n] for n in ("PCFD Headquarters", "Washington Park", "South End (Putnam & Mellor)",
                                      "Brooksville Engine & Hose #5", "Rye Brook Firehouse"))
# The department's layout (live app, 2026-10-07); everything else has no station.
LAYOUT = {HQ["name"]: ("2392", "2393", "2398", "E60", "L31", "TL2", "R40", "U67"), SE["name"]: ("E61", "E63", "M40"),
          BV["name"]: ("E62",), WP["name"]: ("E64", "R41"), RB["name"]: ("2661", "E14", "L30", "U42")}
NEXT_DOOR_A = {"lat": 41.0045, "lon": -73.6745}           # a block east of Washington Park
NEXT_DOOR_B = {"lat": 41.0055, "lon": -73.6770}           # a block west of it
TODAY_STRU = ["E58", "E60", "E61", "L31", "R40", "2398", "E14", "L30"]   # board order (tests/test_cad.py)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path=None, clock=None, stations=True):
    c = Cad(Settings(db=str(tmp_path / "cad.sqlite") if tmp_path else ""), log=lambda m: None, clock=clock or Clock())
    if stations:
        for st, codes in LAYOUT.items():
            for code in codes:
                c.unit_station(uid(c, code), {"station": st, "operator": "test"})
    return c


def uid(c, name):
    return next(u["id"] for u in c.store.units() if u["name"] == name)


def rec(c, iid, **body):
    return c.recommend(iid, body)


def names(r):
    return [u["name"] for u in r["units"]]


def call(c, where=None, typ="STRU", sub=""):
    body = {"type": typ, "subtype": sub, "address": "test"}
    if where:
        body.update(where)
    return c.create(body)["id"]


def work_and_clear(c, clock, iid, *codes, minutes_ago=0.0):
    """The units go on location at iid and clear it (minutes_ago before now)."""
    now = clock.t
    clock.t = now - minutes_ago * 60 - 600
    c.store.assign(iid, [uid(c, n) for n in codes])
    for n in codes:
        c.unit_status(uid(c, n), {"status": "OL"})
    clock.t = now - minutes_ago * 60
    for n in codes:
        c.unit_status(uid(c, n), {"status": "AV"})
    clock.t = now


# -- the ETA model (pure) -------------------------------------------------------------------------------

def test_eta_math():
    p = eta.Params()
    assert (p.road_factor, p.speed_mph, p.turnout_min, p.return_margin_min, p.order) == (1.35, 25.0, 2.0, 3.0, "nearest")
    km = eta.haversine_km(HQ["ll"], WP["ll"])
    assert km == pytest.approx(0.824, abs=0.005)                  # 209 -> 464 Westchester Ave, as the crow flies
    assert eta.haversine_km([-73.0, 41.0], [-73.0, 42.0]) == pytest.approx(111.2, abs=0.1)   # a degree of latitude
    assert eta.drive_s(HQ["ll"], WP["ll"], p) == pytest.approx(km * 1.35 / (25 * 1.609344) * 3600)    # 25 mph
    assert eta.eta_s(HQ["ll"], WP["ll"], p, from_quarters=True) == pytest.approx(eta.drive_s(HQ["ll"], WP["ll"], p) + 120)
    assert eta.eta_s(HQ["ll"], WP["ll"], p, from_quarters=False) == eta.drive_s(HQ["ll"], WP["ll"], p)
    assert eta.eta_s(HQ["ll"], HQ["ll"], p, True) == 120.0                 # next door to the house: turnout only
    assert eta.drive_s(HQ["ll"], WP["ll"], eta.Params(speed_mph=12.5)) == pytest.approx(2 * eta.drive_s(HQ["ll"], WP["ll"], p))
    assert [eta.text(s) for s in (0, 29, 31, 150, 3600)] == ["~1 min est.", "~1 min est.", "~1 min est.", "~2 min est.",
                                                              "~60 min est."]
    assert eta.along([0, 0], [10, 20], 0.25) == [2.5, 5.0] and eta.along([0, 0], [10, 20], 7) == [10, 20]
    assert eta.return_window_s(HQ["ll"], WP["ll"], p) == pytest.approx(eta.drive_s(HQ["ll"], WP["ll"], p) + 180)
    assert eta.return_window_s(HQ["ll"], None, p) == 180.0
    for k, bad in (("road_factor", 0.5), ("speed_mph", 0), ("speed_mph", True), ("turnout_min", "2"), ("order", "x"),
                   ("quarters_status", "DP"), ("nope", 1)):
        with pytest.raises(ValueError):
            eta.check(k, bad, ("AQ", "AV"))
    assert eta.check("turnout_min", 1.234, ()) == 1.23 and eta.check("quarters_status", "", ()) == ""


# -- where a rig is now (pure) ----------------------------------------------------------------------------

E61 = {"id": 1, "name": "E61", "kind": "engine", "status": "AV", "board": "main"}
CLEAR = {"incident_id": 4, "number": "26-000004", **NEXT_DOOR_A}


def test_locate_station_returning_then_home():
    p = eta.Params()
    assert locate(E61, "free", SE)["where"] == "station"
    assert locate(E61, "free", SE)["on_road"] is False and locate(E61, "free", SE)["label"] == f"from {SE['name']}"
    assert locate(E61, "free", None) is None                                  # no station: unknown
    call_ll = [NEXT_DOOR_A["lon"], NEXT_DOOR_A["lat"]]
    window = eta.return_window_s(call_ll, SE["ll"], p)
    for k in (0.0, 0.25, 0.5, 0.99):
        loc = locate(E61, "free", SE, None, None, T0 + k * window, dict(CLEAR, cleared_at=T0), p)
        assert loc["where"] == "returning" and loc["on_road"] and loc["label"] == "returning from 26-000004"
        assert loc["ll"] == pytest.approx(eta.along(call_ll, SE["ll"], k))
    assert locate(E61, "free", SE, None, None, T0 + window + 1, dict(CLEAR, cleared_at=T0), p)["where"] == "station"
    # No station: near the call for the margin, then unknown.
    near = locate(E61, "free", None, None, None, T0 + 170, dict(CLEAR, cleared_at=T0), p)
    assert (near["where"], near["ll"], near["label"]) == ("near", call_ll, "near 26-000004 (station not set)")
    assert locate(E61, "free", None, None, None, T0 + 181, dict(CLEAR, cleared_at=T0), p) is None
    # Reported back in quarters (the in-quarters status), out of service, a call with no map point: its station.
    assert locate(dict(E61, status="AQ"), "free", SE, None, None, T0 + 60, dict(CLEAR, cleared_at=T0), p)["where"] == "station"
    assert locate(E61, "free", SE, None, None, T0 + 60, dict(CLEAR, cleared_at=T0), eta.Params(quarters_status=""))["where"] \
        == "returning"
    assert locate(E61, "oos", SE, None, None, T0 + 60, dict(CLEAR, cleared_at=T0), p)["where"] == "station"
    assert locate(E61, "free", SE, None, None, T0 + 60, dict(CLEAR, cleared_at=T0, lat=None, lon=None), p)["where"] == "station"


def test_locate_committed_frozen_where_it_took_the_call_and_avl_wins():
    p = eta.Params()
    inc = {"id": 9, "number": "26-000009", **NEXT_DOOR_B}
    call_ll = [NEXT_DOOR_A["lon"], NEXT_DOOR_A["lat"]]
    window = eta.return_window_s(call_ll, SE["ll"], p)
    took = dict(E61, status="DP", assigned_at=T0 + 0.5 * window)
    loc = locate(took, "dispatched", SE, inc, None, T0 + 2 * window, dict(CLEAR, cleared_at=T0), p)
    assert loc["where"] == "returning" and loc["ll"] == pytest.approx(eta.along(call_ll, SE["ll"], 0.5))
    assert locate(took, "onscene", SE, inc, None, T0 + 2 * window, dict(CLEAR, cleared_at=T0), p)["where"] == "incident"
    fix = {"lat": 41.01, "lon": -73.67, "at": T0 - 10}
    loc = locate(E61, "free", SE, None, fix, T0, dict(CLEAR, cleared_at=T0 - 30), p)
    assert (loc["where"], loc["ll"], loc["on_road"]) == ("position", [-73.67, 41.01], True)
    home = locate(E61, "free", SE, None, {"lat": SE["ll"][1], "lon": SE["ll"][0], "at": T0}, T0)
    assert home["where"] == "position" and home["on_road"] is False                 # in its own house: turnout counts
    stale = {"lat": 41.01, "lon": -73.67, "at": T0 - mapview.POSITION_MAX_AGE_S - 1}
    assert locate(E61, "free", SE, None, stale, T0)["where"] == "station"


def test_map_draws_returning_rigs_on_their_way_home_with_a_line_to_the_station():
    p = eta.Params()
    m = place(E61, "free", SE, None, None, T0 + 60, clear=dict(CLEAR, cleared_at=T0), params=p)
    assert (m["place"], m["look"], m["group"], m["home_line"], m["returning_from"]) == \
        ("returning", "available", "unit:E61", True, "26-000004")
    assert m["line_to"] == SE["ll"] and m["station"] == SE["name"]
    assert place(E61, "free", SE, None, None, T0 + 3600, clear=dict(CLEAR, cleared_at=T0), params=p)["place"] == "station"
    assert place(E61, "free", None, None, None, T0 + 60, clear=dict(CLEAR, cleared_at=T0), params=p) is None   # listed
    inc = {"id": 9, "number": "26-000009", **NEXT_DOOR_B}
    going = place(dict(E61, status="DP", assigned_at=T0 + 60), "dispatched", SE, inc, None, T0 + 120,
                  clear=dict(CLEAR, cleared_at=T0), params=p)
    assert going["place"] == "returning" and going["line_to"] == [inc["lon"], inc["lat"]] and not going["home_line"]


# -- the recommendation ----------------------------------------------------------------------------------

def test_no_stations_keeps_todays_order_exactly():
    c = make(stations=False)
    iid = call(c, {"lat": WP["ll"][1], "lon": WP["ll"][0]})
    r = rec(c, iid)
    assert names(r) == TODAY_STRU == names(runcards.recommend(c.store, "STRU", "", iid))
    assert all(u["reason"] == "station not set" and u["eta_s"] is None for u in r["units"])
    assert r["order"]["by"] == "nearest" and r["others"] == {}


def test_nearest_station_wins_within_a_slot_ties_keep_board_order():
    c = make()
    r = rec(c, call(c, {"lat": WP["ll"][1], "lon": WP["ll"][0]}))         # at Washington Park
    assert names(r) == ["E64", "E60", "E61", "L31", "R40", "2398", "E14", "L30"]
    by = {u["name"]: u for u in r["units"]}
    assert by["E64"]["reason"] == f"~2 min est. · from {WP['name']}" and by["E64"]["eta_s"] == 120   # turnout only
    assert by["E60"]["reason"] == f"~4 min est. · from {HQ['name']}"
    assert by["2398"]["slot"] == "2398" and by["E14"]["slot"] == "E14"   # named units stay named
    # A call at South End: its two engines first, in board order (same house, same estimate).
    r = rec(c, call(c, {"lat": SE["ll"][1], "lon": SE["ll"][0]}))
    assert names(r)[:3] == ["E61", "E63", "E60"]
    assert {u["name"]: u["eta_s"] for u in r["units"]}["E61"] == {u["name"]: u["eta_s"] for u in r["units"]}["E63"]
    # Units with no station come after those with one, in board order.
    card = c.save_run_card({"type": "ALARM/RESD", "slots": [{"kind": "engine", "count": 8}]})
    assert card["slots"] == [{"kind": "engine", "count": 8}]
    r = rec(c, call(c, {"lat": BV["ll"][1], "lon": BV["ll"][0]}, "ALARM", "RESD"))
    assert names(r) == ["E62", "E60", "E64", "E61", "E63", "E58", "E59", "E14"]   # spare E59 after regulars; RB's after PCFD's


def test_spares_mutual_aid_and_named_units_unchanged():
    c = make()
    c.unit_station(uid(c, "E59"), {"station": WP["name"]})                 # the spare, nearest of all
    c.unit_station(uid(c, "E191"), {"station": WP["name"]})                # Rye's (mutual aid): never by kind
    c.save_run_card({"type": "ALARM/RESD", "slots": [{"kind": "engine", "count": 9}, {"unit": "L31"}]})
    r = rec(c, call(c, {"lat": WP["ll"][1], "lon": WP["ll"][0]}, "ALARM", "RESD"))
    assert names(r) == ["E64", "E60", "E61", "E63", "E62", "E58", "E59", "E14", "L31"]
    assert "E191" not in names(r) and r["unfilled"] == ["only 8 of 9 engine available"]


def test_joint_response_policy_stays_above_proximity():
    c = make()
    # Not their call, though at their door: PCFD's nearest engine (Brooksville), not Rye Brook's E14.
    r = rec(c, call(c, {"lat": RB["ll"][1], "lon": RB["ll"][0]}, "ALARM", "COMM"))
    assert r["joint_call"] == "" and names(r)[0] == "E62"
    # Their call (in Rye Brook), even nearer Brooksville: their engine first, as today.
    iid = c.create({"type": "ALARM", "subtype": "COMM", "address": "1 Test St, Rye Brook",
                    "lat": BV["ll"][1] + 0.002, "lon": BV["ll"][0]})["id"]
    r = rec(c, iid)
    assert r["joint_call"] == "Rye Brook" and names(r)[0] == "E14"


def test_a_rig_that_just_cleared_next_door_beats_one_in_quarters_then_goes_home():
    clock = Clock()
    c = make(clock=clock)
    first = call(c, NEXT_DOOR_A, "ALARM", "COMM")
    work_and_clear(c, clock, first, "E61", minutes_ago=2)
    number = c.store.incident(first)["number"]
    nxt = call(c, NEXT_DOOR_B)
    r = rec(c, nxt)
    assert names(r)[:3] == ["E61", "E64", "E60"]                          # E61 is on the road nearby: no turnout
    e61 = r["units"][0]
    assert e61["reason"] == f"~1 min est. · returning from {number}" and e61["eta_s"] < r["units"][1]["eta_s"]
    # The return window: drive back to South End + 3 min margin. After it, E61 is home (and turnout counts).
    window = eta.return_window_s([NEXT_DOOR_A["lon"], NEXT_DOOR_A["lat"]], SE["ll"], c.eta)
    clock.t += window - 120 + 1
    c.store.rev += 1
    r = rec(c, nxt)
    assert names(r)[:3] == ["E64", "E60", "E61"]
    assert r["units"][2]["reason"].endswith(f"· from {SE['name']}")
    # The map drew it on its way home meanwhile.
    clock.t -= 120
    m = next(m for m in c.state()["map"]["markers"] if m["unit"] == "E61")
    assert m["place"] == "returning" and m["line_to"] == SE["ll"]


def test_back_in_quarters_status_and_unknown_station_while_returning():
    clock = Clock()
    c = make(clock=clock)
    first = call(c, NEXT_DOOR_A, "ALARM", "COMM")
    work_and_clear(c, clock, first, "E61", "E58", minutes_ago=1)
    number = c.store.incident(first)["number"]
    nxt = call(c, NEXT_DOOR_B)
    by = {u["name"]: u for u in rec(c, nxt)["units"]}
    assert by["E58"]["reason"] == f"~1 min est. · near {number} (station not set)"      # no station: near the call
    assert c.locations()["E61"]["where"] == "returning"
    c.unit_status(uid(c, "E61"), {"status": "AQ"})                       # "back in quarters"
    assert c.locations()["E61"]["where"] == "station"
    assert rec(c, nxt)["others"][uid(c, "E61")]["reason"].endswith(f"· from {SE['name']}")
    clock.t += 3 * 60                                                     # E58's margin is over: unknown again
    assert {u["name"]: u for u in rec(c, nxt)["units"]}.get("E58", {}).get("reason", "station not set") == "station not set"


def test_cancelled_en_route_does_not_count_as_returning():
    clock = Clock()
    c = make(clock=clock)
    first = call(c, NEXT_DOOR_A, "ALARM", "COMM")
    c.store.assign(first, [uid(c, "E61")])
    c.unit_status(uid(c, "E61"), {"status": "RS"})
    clock.t += 60
    c.unit_status(uid(c, "E61"), {"status": "CE"})
    clock.t += 60
    assert c.locations()["E61"]["where"] == "station"           # where it turned around is unknown: its station
    assert c.store.recent_clears(0) == {}


def test_no_incident_location_is_todays_order():
    c = make()
    iid = call(c)
    r = rec(c, iid)
    assert names(r) == TODAY_STRU
    assert r["order"] == {"by": "card", "note": "no map point on the event: run card order"}
    assert {u["name"]: u["reason"] for u in r["units"]}["E60"] == f"from {HQ['name']}"
    assert {u["name"]: u["reason"] for u in r["units"]}["E58"] == "station not set"


def test_settings_change_order_and_numbers_audited_and_kept(tmp_path):
    c = make(tmp_path)
    iid = call(c, {"lat": WP["ll"][1], "lon": WP["ll"][0]})
    c.settings_post("dispatch", {"action": "set", "key": "order", "value": "card", "operator": "Ann"})
    r = rec(c, iid)
    assert names(r) == TODAY_STRU and r["order"] == {"by": "card", "note": "run card order (Settings > Dispatch)"}
    assert {u["name"]: u["reason"] for u in r["units"]}["E60"] == f"~4 min est. · from {HQ['name']}"   # still estimated
    c.settings_post("dispatch", {"action": "set", "key": "turnout_min", "value": 0, "operator": "Ann"})
    c.settings_post("dispatch", {"action": "set", "key": "speed_mph", "value": 18, "operator": "Ann"})
    assert rec(c, iid)["others"][uid(c, "E64")]["eta_s"] == 0                # at the call's door, no turnout
    e60 = {u["name"]: u["eta_s"] for u in rec(c, iid)["units"]}["E60"]
    assert e60 == round(eta.drive_s(HQ["ll"], WP["ll"], eta.Params(speed_mph=18)))
    rows = c.settings_audit_log({"section": ["dispatch"]})["audit"]
    keys = [(e["key"], e["old"], e["new"], e["operator"]) for e in rows]
    assert ("eta/order", "nearest", "card", "Ann") in keys and ("eta/turnout_min", "2.0", "0.0", "Ann") in keys
    for bad in ({"key": "speed_mph", "value": 500}, {"key": "order", "value": "random"},
                {"key": "quarters_status", "value": "OS"}):
        with pytest.raises(CadError):
            c.settings_post("dispatch", {"action": "set", **bad})
    page = c.settings_get()["dispatch"]["eta"]
    assert page["order"]["value"] == "card" and page["order"]["edited"]
    assert {n["key"]: (n["value"], n["default"], n["edited"]) for n in page["numbers"]}["turnout_min"] == (0.0, 2.0, True)
    assert "AQ" in [s["code"] for s in page["quarters_status"]["choices"]]
    # Kept over a restart; the export carries them and imports back.
    c2 = make(tmp_path, stations=False)
    assert (c2.eta.order, c2.eta.turnout_min, c2.eta.speed_mph) == ("card", 0.0, 18.0)
    assert c2.settings_export()["overrides"]["dispatch"]["order"] == "card"
    c2.settings_post("dispatch", {"action": "reset", "key": "order"})
    assert c2.eta.order == "nearest"
    c2.settings_post("dispatch", {"action": "reset_section"})
    assert c2.eta == eta.Params()


def test_avl_position_overrides_station():
    clock = Clock()
    c = make(clock=clock)
    iid = call(c, {"lat": WP["ll"][1], "lon": WP["ll"][0]})
    c.positions = lambda: {"E58": {"lat": WP["ll"][1] + 0.0005, "lon": WP["ll"][0], "at": clock.t - 5},     # E58: no station
                           "E60": {"lat": 41.05, "lon": -73.70, "at": clock.t - 600}}                    # stale: ignored
    r = rec(c, iid)
    assert names(r)[:3] == ["E58", "E64", "E60"]
    assert r["units"][0]["reason"] == "~1 min est. · live position"
    assert r["units"][2]["reason"].endswith(f"from {HQ['name']}")


def test_others_carry_hints_for_adding_by_hand():
    c = make()
    r = rec(c, call(c, {"lat": WP["ll"][1], "lon": WP["ll"][0]}))
    picked = {u["id"] for u in r["units"]}
    hints = {k: v for k, v in r["others"].items()}
    assert uid(c, "E63") in hints and not picked & set(hints)
    assert hints[uid(c, "E63")]["reason"].endswith(f"· from {SE['name']}")
    assert uid(c, "E58") not in hints                                    # no station: no estimate to show


def test_stations_are_read_from_the_settings_units_override():
    c = make(stations=False)
    c.unit_station(uid(c, "E64"), {"station": WP["name"], "operator": "Ann"})
    assert c.settings.overrides()["units"]["E64"]["station"] == WP["name"]
    assert c.locations()["E64"]["where"] == "station" and c.locations()["E60"] is None
    assert math.isclose(c.locations()["E64"]["ll"][0], WP["ll"][0])


def test_a_speed_saved_in_kmh_is_taken_over_in_mph():
    # The setting was km/h before the switch to US units (2026-10-08): a saved value carries over.
    p = eta.Params().with_overrides({"speed_kmh": 40})
    assert p.speed_mph == 24.9
    assert eta.Params().with_overrides({"speed_kmh": 40, "speed_mph": 30}).speed_mph == 30   # mph wins once set
    assert eta.NUMBERS["speed_mph"][4] == "mph"
