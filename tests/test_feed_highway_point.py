"""A feed's call on a highway gets the map point the location box shows for it (sizeup/cad/service.py highway_point):
its route, direction and mile marker or exit read from its normalised form, whatever the feed types around it (an "@" in
front, a town after it, a marker and an exit both). A later report that moves it moves the point; an open incident left
with no point gets it on the next report, or from the start's sweep (Sources.backfill_points). Invented data only:
made-up event ids, towns, units and times; the markers are the place search's test fixture (tests/test_places.py)."""

from __future__ import annotations

import pytest

from sizeup.geo.gazetteer import Gazetteer
from sizeup.geo.places import PlaceIndex
from tests.test_county_event_runs import TOWN_A, TOWN_B, lines, our_towns  # noqa: F401 - a fixture
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0, Rig
from tests.test_places import fixture_gazetteer

MM14 = (40.99, -73.68)            # the fixture's I-95 MM 14.0 (filed under another town than the run's)
X21 = (41.005, -73.67)            # its I-95 NB exit 21
X19A = (41.035, -73.675)          # its Hutch SB exit 19A


def placed(rig):
    """The place search on (the fixture's markers); the gazetteer sure of nothing, so only the place search places."""
    if not hasattr(rig, "gaz"):
        rig.gaz = Gazetteer(fixture_gazetteer(rig.tmp / "gaz.sqlite"))
        rig.said = []
        rig.cad.sources.log = rig.said.append
    rig.cad.geocoder = lambda q: []
    rig.cad.place_source = lambda: PlaceIndex(rig.gaz)
    return rig


def unplaced(rig) -> None:
    """The place search down (as before the fix read a feed's highway): no point for anything."""
    rig.cad.place_source = None
    rig.cad._place_ix = None


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.tmp = tmp_path
    return placed(r)


@pytest.fixture
def auto(tmp_path, our_towns):  # noqa: F811 - the fixture
    r = AutoRig(tmp_path)
    r.tmp = tmp_path
    return placed(r)


def run(rig, ref: str, loc: str, units=("E61",), dt: float = 0, **kw) -> None:
    rig.feed.put(ref, type="MVA/INJURIES", location=loc, units=units, times={"dispatched": T0 + dt}, **kw)
    rig.poll()


def point(rig, iid: int) -> tuple:
    inc = rig.cad.store.incident(iid)
    return (inc["lat"], inc["lon"]) if inc["lat"] is not None else None


@pytest.mark.parametrize("loc, at, label", [
    ("@I95 NB 14.0 Exit 21 OUR TOWN A", MM14, "I-95 NB MM 14.0"),     # "@", marker and exit, town after it
    ("I95 NB 14.0 Exit 21 OUR TOWN A", MM14, "I-95 NB MM 14.0"),      # no "@"
    ("@I95 SB 14.0 OUR TOWN B", MM14, "I-95 SB MM 14.0"),             # the other carriageway, marker only
    ("@I95 NB Exit 21 OUR TOWN A", X21, "I-95 NB exit 21"),           # exit only
    ("@I95 NB 15.3 Exit 21 OUR TOWN A", X21, "I-95 NB exit 21"),      # a marker the place search lacks: the exit
    ("@HUTCH SB EXIT 19A OUR TOWN B", X19A, "Hutch SB exit 19A"),
    ("I-95 NB MM 14.0", MM14, "I-95 NB MM 14.0"),                     # as the location box writes it
])
def test_a_feed_s_highway_call_gets_the_location_box_s_point(rig, loc, at, label):
    run(rig, "F0090001", loc)
    [inc] = rig.incidents()
    assert point(rig, inc["id"]) == at and inc["geo_label"] == label
    assert inc["address_raw"] == loc                  # the feed's text as it was: only the point is new


def test_the_console_s_typed_highway_is_placed_the_same_way(rig):
    inc = rig.cad.create({"type": "MVA", "address": "@I95 NB 14.0 Exit 21 OUR TOWN A", "operator": "JD"})
    assert (inc["lat"], inc["lon"]) == MM14


def test_a_highway_with_nothing_known_says_once_why_and_stays_unplaced(rig):
    run(rig, "F0090001", "@I95 NB 31.7 OUR TOWN A")
    [inc] = rig.incidents()
    assert point(rig, inc["id"]) is None
    rig.at(60)
    rig.feed.change("F0090001", units=("E61", "E63"))
    rig.poll()
    why = [m for m in rig.said if "no map point" in m]
    assert why == [f"incident sources: {rig.feed.label}: event {inc['id']}: a highway location left with no map point "
                   "(no such marker or exit in the place search)"]
    assert "31.7" not in why[0]                           # the incident's id, never the feed's location ("logs")


def test_a_later_report_moving_the_call_moves_its_point(rig):
    run(rig, "F0090001", "@I95 NB 31.7 OUR TOWN A")       # unknown: no point
    [inc] = rig.incidents()
    rig.at(60)
    rig.feed.change("F0090001", location="@I95 NB 14.0 Exit 21 OUR TOWN A")
    rig.poll()
    assert point(rig, inc["id"]) == MM14
    rig.at(120)
    rig.feed.change("F0090001", location="@HUTCH SB EXIT 19A OUR TOWN B")
    rig.poll()
    assert point(rig, inc["id"]) == X19A and rig.cad.store.incident(inc["id"])["geo_label"] == "Hutch SB exit 19A"


def test_the_same_place_written_another_way_gives_a_missing_point(rig):
    unplaced(rig)                                         # created while it read nothing: no point
    run(rig, "F0090001", "@I95 NB 14.0 OUR TOWN A")
    [inc] = rig.incidents()
    assert point(rig, inc["id"]) is None
    placed(rig)
    rig.at(60)                                            # the same marker, its exit added: the same place
    rig.feed.change("F0090001", location="@I95 NB 14.0 Exit 21 OUR TOWN A")
    rig.poll()
    assert point(rig, inc["id"]) == MM14
    assert any(t.endswith("map point I-95 NB MM 14.0") for t in rig.feed_lines(inc["id"]))


def test_the_next_report_places_an_open_incident_left_with_no_point(rig):
    unplaced(rig)
    run(rig, "F0090001", "@I95 NB 14.0 Exit 21 OUR TOWN A")
    [inc] = rig.incidents()
    assert point(rig, inc["id"]) is None
    placed(rig)
    rig.at(60)
    rig.feed.change("F0090001", units=("E61", "E63"))     # the location as it was: placed anyway
    rig.poll()
    assert point(rig, inc["id"]) == MM14
    rig.at(90)
    rig.feed.change("F0090001", units=("E61",))
    rig.poll()
    assert sum(t.endswith("map point I-95 NB MM 14.0") for t in rig.feed_lines(inc["id"])) == 1


def test_a_page_first_with_no_point_then_the_feed_at_its_marker_places_it(rig):
    page = rig.page(text="Engine 61, Rescue 40. Motor vehicle accident, I 95 northbound mile marker 14.0.")
    assert page["lat"] is None
    rig.at(40)
    run(rig, "F0090001", "@I95 NB 14.0 Exit 21 OUR TOWN A", dt=-10)
    assert [i["id"] for i in rig.incidents()] == [page["id"]]
    assert point(rig, page["id"]) == MM14


@pytest.mark.parametrize("first, second", [(("F0090001", TOWN_A), ("F0090907", TOWN_B)),
                                           (("F0090907", TOWN_B), ("F0090001", TOWN_A))])
def test_one_call_in_both_of_our_towns_has_the_point_whichever_run_comes_first(auto, first, second):
    locs = {"F0090001": "@I95 NB 14.0 Exit 21 OUR TOWN A", "F0090907": "I95 NB 14.0 OUR TOWN B"}
    (ra, ea), (rb, eb) = first, second
    auto.feed.put(ra, type="MVA/INJURIES", location=locs[ra], units=("E61",), times={"dispatched": T0}, extra=ea)
    auto.poll()
    iid = auto.iid(ra)
    auto.at(90)
    auto.feed.put(rb, type="MVA/INJURIES", location=locs[rb], units=("E61", "E63"), times={"dispatched": T0 + 80},
                  extra=eb)
    auto.poll()
    assert auto.iid(rb) == iid and len(auto.incidents()) == 1
    assert point(auto, iid) == MM14


def test_a_person_s_pin_or_location_is_never_replaced_by_the_fill(rig):
    unplaced(rig)
    run(rig, "F0090001", "@I95 NB 14.0 OUR TOWN A")
    run(rig, "F0090002", "@HUTCH SB EXIT 19A OUR TOWN B", units=("E63",), dt=30)
    a, b = [i["id"] for i in rig.incidents()]
    rig.cad.sources.mark_manual(a, ["pin"])
    rig.cad.sources.mark_manual(b, ["location"])
    placed(rig)
    rig.at(60)
    rig.feed.change("F0090001", units=("E61", "E64"))
    rig.feed.change("F0090002", units=("E63", "E64"))
    rig.poll()
    assert point(rig, a) is None and point(rig, b) is None
    assert rig.cad.sources.backfill_points() == 0


def test_the_start_s_sweep_places_open_incidents_once_and_leaves_the_rest(rig):
    unplaced(rig)
    run(rig, "F0090001", "@I95 NB 14.0 Exit 21 OUR TOWN A")
    run(rig, "F0090002", "@I95 NB 31.7 OUR TOWN B", units=("E63",), dt=30)      # no such marker: stays unplaced
    run(rig, "F0090003", "@HUTCH SB EXIT 19A OUR TOWN B", units=("E64",), dt=40)
    a, b, c = [i["id"] for i in rig.incidents()]
    rig.cad.store.dismiss(c, operator="JD", reason="test")                    # not open: never touched
    placed(rig)
    assert rig.cad.sources.backfill_points() == 1
    assert point(rig, a) == MM14 and point(rig, b) is None and point(rig, c) is None
    assert rig.cad.sources.backfill_points() == 0                              # once: nothing more to do
    assert sum("map point I-95 NB MM 14.0" in t for t in lines(rig, a)) == 1


def test_the_sweep_writes_nothing_when_not_the_leader(rig):
    from sizeup.fence import FencedWrite
    unplaced(rig)
    run(rig, "F0090001", "@I95 NB 14.0 OUR TOWN A")
    placed(rig)
    rig.cad.store.may_write = lambda: False
    with pytest.raises(FencedWrite):
        rig.cad.sources.backfill_points()
    rig.cad.store.may_write = lambda: True
    assert point(rig, rig.incidents()[0]["id"]) is None


SURE = {"label": "600 Sample Ave", "lat": 41.02, "lon": -73.66, "confidence": 0.95, "municipality": "Sampleville"}


@pytest.mark.parametrize("text", ["600 Sample Ave by I-95 NB exit 21", "600 SAMPLE AVE I95 NB EXIT 21 RAMP"])
def test_an_address_the_gazetteer_is_sure_of_beats_a_highway_only_named_after_it(rig, text):
    rig.cad.geocoder = lambda q: [dict(SURE)]
    inc = rig.cad.create({"type": "MVA", "address": text, "operator": "JD"})
    assert (inc["lat"], inc["lon"]) == (SURE["lat"], SURE["lon"])


def test_a_highway_location_is_its_marker_even_when_the_gazetteer_guesses(rig):
    rig.cad.geocoder = lambda q: [dict(SURE)]
    run(rig, "F0090001", "@I95 NB 14.0 Exit 21 OUR TOWN A")
    assert point(rig, rig.incidents()[0]["id"]) == MM14


def test_an_exit_said_to_be_another_road_s_is_never_this_highway_s():
    from sizeup.dispatch import highway, highway_exit
    for text, want in [("I95 NB 15.3 Exit 21 OUR TOWN A", "21"), ("I-95 NB MM 15.3", ""),
                       ("I95 NB 15.3 by exit 3 of the Hutch", ""), ("I95 NB 15.3 near exit 3 to Hutch", ""),
                       ("I95 NB 15.3, Hutch SB exit 19A", "")]:
        assert highway_exit(text, highway(text)) == want, text


def test_a_report_that_changes_nothing_of_the_place_never_looks_again(rig):
    asked = []
    rig.cad.geocoder = lambda q: asked.append(q) or []
    run(rig, "F0090001", "12 NOWHERE LN OUR TOWN A")                    # not a highway: never looked for again
    run(rig, "F0090002", "@I95 NB 31.7 OUR TOWN A", units=("E63",), dt=20)   # a marker nobody has: once
    n = len(asked)
    for k, dt in enumerate((60, 90, 120)):
        rig.at(dt)
        rig.feed.change("F0090001", units=("E61", f"E6{k + 2}"))
        rig.feed.change("F0090002", units=("E63", f"E6{k + 4}"))
        rig.poll()
    assert len(asked) == n


def test_the_ruling_run_unplaced_a_joining_run_at_a_known_marker_never_sets_the_point(auto):
    auto.feed.put("F0090001", type="MVA/INJURIES", location="@I95 NB 14.3 OUR TOWN A", units=("E61",),
                  times={"dispatched": T0}, extra=TOWN_A)
    auto.poll()
    iid = auto.iid("F0090001")
    auto.at(90)
    auto.feed.put("F0090907", type="MVA/INJURIES", location="I95 NB 14.0 OUR TOWN B", units=("E61", "E63"),
                  times={"dispatched": T0 + 80}, extra=TOWN_B)
    auto.poll()
    assert auto.iid("F0090907") == iid and point(auto, iid) is None      # the first run rules the fields (#807)


def test_the_backfill_thread_places_after_the_addresses(rig):
    unplaced(rig)
    run(rig, "F0090001", "@I95 NB 14.0 Exit 21 OUR TOWN A")
    placed(rig)
    calls = []
    fenced = [True]
    real = rig.cad.sources.backfill_points

    def once_fenced():
        from sizeup.fence import FencedWrite
        calls.append(1)
        if fenced and fenced.pop():
            raise FencedWrite("lease ran out")
        return real()
    rig.cad.sources.backfill_points = once_fenced
    rig.cad.sources.backfill_run(delay_s=0, retry_s=0)
    assert len(calls) == 2 and point(rig, rig.incidents()[0]["id"]) == MM14
