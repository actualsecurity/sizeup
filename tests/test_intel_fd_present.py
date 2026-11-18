"""A fire-related PD event at a place where the FD already is (sizeup/intel/engine.py _fire; admin, 2026-11-17: a
pre-alert is a jump on a call, or an FYI if things are changing; a PD dispatch of a fire the FD is already working is
neither).

Before a push of a "fire_related" category (the police pack's fire_report, fire_call, hazmat, alarm), our fire
incidents at its place (the CAD's own and the incident sources', the county feed's too):
  FD on scene     no pre-alert (log); an FYI only for a safety change
  FD dispatched   no pre-alert (log); an FYI at priority 2 when the words add something, 4 for a safety change
  FD cleared      a pre-alert as usual (a rekindle), saying when the FD was there
  none            a pre-alert as usual
and the words alone ("the fire department is already working it") count as the FD being there.

Every scene, street, number and unit here is invented (this repository is public)."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from sizeup.cad.service import fire_state
from sizeup.intel import push as push_mod
from sizeup.intel.engine import RELEASE_S, IntelEngine, RecordingSink, fire_street_said
from sizeup.intel.events import MemoryEvents, SqlEvents
from sizeup.intel.model import Event, Place
from tests.test_intel import Clock, row

PC = "Port Chester"
LAT, LON = 41.0, -73.68
NOW = 50_000.0
M_LAT = 1 / 111_320                                         # a metre north, in degrees

# the scenes (all invented)
DUMPSTER = "Car 3, check on smoke from a dumpster behind 20 Example Road"
DUMPSTER_WORKED = f"{DUMPSTER}, the fire department is already working it"
LAUNDROMAT = "Car 8, respond to 15 Sample Street, the laundromat, caller reports an odor of gas inside"
SCHOOL = "Car 2, respond to 30 Test Avenue, the elementary school, fire alarm activation"
REKINDLE = "Car 5, respond to 8 Example Lane, smoke showing from the roof again"

PLACES = {"20 example road": ("20 Example Rd", 0.0), "15 sample street": ("15 Sample St", 0.01),
          "30 test avenue": ("30 Test Ave", 0.02), "8 example lane": ("8 Example Ln", 0.03)}


def locate(text, _scope):
    """A stand-in gazetteer: the four addresses above, confirmed unless the words say "maybe"."""
    low = text.lower()
    for said, (label, dlat) in PLACES.items():
        if said in low:
            return Place("address", LAT + dlat, LON, 25.0, f"{label}, {PC}", PC, "maybe" not in low, 0.8)
    return None


def inc(n=41, label="20 Example Rd", dlat=0.0, typ="INVEST", fd="dispatched", made=-600.0, closed=None, fd_at=None, **kw):
    """One of our incidents as Cad.intel_fire_incidents gives it (made / closed: seconds from NOW)."""
    return {"id": n, "number": f"26-0000{n}", "type": typ, "lat": LAT + dlat, "lon": LON, "label": f"{label}, {PC}",
            "address": f"{label}, {PC}", "address_raw": "", "created_at": NOW + made,
            "closed_at": None if closed is None else NOW + closed, "fd": fd, "fd_at": fd_at, **kw}


def engine(*incs, settings=None, loc=locate):
    sink = RecordingSink()
    e = IntelEngine(settings={"intel": settings or {}}, events=MemoryEvents(), sink=sink, thread=False,
                    clock=Clock(NOW), locate=loc)
    seen = []

    def lookup(opened_since, closed_since):
        seen.append((opened_since, closed_since))
        return [dict(i) for i in incs]
    e.fire_incidents, e.asked = lookup, seen
    return e, sink


def pushed(sink):
    return [(ev, d) for ev, d in sink.pushes if d.would_push]


def one(e, text, tid="t1", at=NOW):
    out = e.feed(row(tid, text, at))
    fire = [x for x in out if x.category in ("fire_report", "fire_call", "hazmat", "alarm")]
    assert len(fire) == 1, [(x.category, x.suppressed) for x in out]
    return fire[0]


# -- on scene ---------------------------------------------------------------------------------------------------------

def test_on_scene_is_logged_and_linked():
    e, sink = engine(inc(fd="on_scene"))
    ev = one(e, DUMPSTER)
    assert ev.category == "fire_report" and ev.near_incident == 41 and ev.fd == "on_scene"
    assert ev.suppressed == "fd_on_scene" and ev.mode == "log" and ev.pushed_at is None and pushed(sink) == []


def test_on_scene_and_only_more_detail_is_still_logged():
    e, sink = engine(inc(fd="on_scene"))
    ev = one(e, f"{DUMPSTER}, someone may be inside the enclosure")
    assert ev.suppressed == "fd_on_scene" and pushed(sink) == []


@pytest.mark.parametrize("extra", ["a man with a knife on scene", "a fight broke out in the lot", "there was an explosion",
                                   "officer down"])
def test_on_scene_and_a_safety_change_is_a_safety_fyi(extra):
    e, sink = engine(inc(n=43, label="30 Test Ave", dlat=0.02, typ="ALARM", fd="on_scene"))
    ev = one(e, f"{SCHOOL}, {extra}")
    assert ev.category == "alarm" and ev.fd == "on_scene" and ev.near_incident == 43 and ev.suppressed == ""
    [(_ev, d)] = [(p, d) for p, d in pushed(sink) if p.category == "alarm"]     # (a weapon is its own pre-alert too)
    assert d.priority == 4 and d.message["title"] == "FYI · PD · Alarm · Port Chester"
    assert d.message["body"].endswith(" · FD on scene · #26-000043")


# -- dispatched -------------------------------------------------------------------------------------------------------

def test_dispatched_and_nothing_added_is_logged():
    e, sink = engine(inc(n=42, label="15 Sample St", dlat=0.01, typ="HAZARD"))
    ev = one(e, LAUNDROMAT)
    assert ev.category == "hazmat" and ev.near_incident == 42 and ev.fd == "dispatched"
    assert ev.suppressed == "fd_dispatched" and ev.mode == "log" and pushed(sink) == []


@pytest.mark.parametrize("extra", ["an employee is still inside", "two customers feeling dizzy and injured",
                                   "it is spreading to the store next door", "there are propane tanks in the back"])
def test_dispatched_and_the_words_add_something_is_a_low_priority_fyi(extra):
    e, sink = engine(inc(n=42, label="15 Sample St", dlat=0.01, typ="HAZARD"))
    ev = one(e, f"{LAUNDROMAT}, {extra}")
    assert ev.fd == "dispatched" and ev.suppressed == "" and ev.mode == "push"
    [(_ev, d)] = pushed(sink)
    assert d.priority == 2 and d.message["title"] == "FYI · PD · Gas / hazmat · Port Chester"
    assert d.message["body"].endswith(" · FD dispatched · #26-000042")


def test_dispatched_and_a_safety_change_is_at_the_safety_priority():
    e, sink = engine(inc(n=42, label="15 Sample St", dlat=0.01, typ="HAZARD"))
    one(e, f"{LAUNDROMAT}, the owner is assaulting a customer")
    [(_ev, d)] = pushed(sink)
    assert d.priority == 4 and d.message["title"].startswith("FYI · ")


# -- none, cleared ----------------------------------------------------------------------------------------------------

def test_no_incident_is_a_pre_alert_as_before():
    e, sink = engine()
    ev = one(e, DUMPSTER)
    assert ev.fd == "" and ev.near_incident is None and ev.suppressed == ""
    [(_ev, d)] = pushed(sink)
    assert d.priority == 4 and d.message["title"] == "PD · Smoke / fire reported · Port Chester" and "FD" not in d.message["body"]


def test_no_hook_or_a_broken_lookup_is_a_pre_alert_as_before():
    e, sink = engine()
    e.fire_incidents = None
    assert one(e, DUMPSTER).fd == "" and len(pushed(sink)) == 1
    logs = []
    e, sink = engine()

    def boom(_a, _b):
        raise RuntimeError("gone")
    e.fire_incidents, e.log = boom, logs.append
    one(e, DUMPSTER)
    assert len(pushed(sink)) == 1 and logs == ["intel: fire incidents: RuntimeError"]


def test_a_rekindle_after_the_fd_cleared_is_a_pre_alert_saying_when_it_was_there():
    from datetime import datetime
    cleared = NOW - 50 * 60
    e, sink = engine(inc(n=44, label="8 Example Ln", dlat=0.03, typ="STRU", fd="cleared", made=-2 * 3600,
                         closed=-50 * 60, fd_at=cleared))
    ev = one(e, REKINDLE)
    assert ev.near_incident == 44 and ev.fd == "cleared" and ev.suppressed == "" and ev.mode == "push"
    [(_ev, d)] = pushed(sink)
    assert d.priority == 4 and not d.message["title"].startswith("FYI")
    assert d.message["body"].endswith(f" · FD here {datetime.fromtimestamp(cleared).strftime('%H:%M')} · #26-000044")


def test_an_open_incident_whose_units_all_cleared_is_a_pre_alert_too():
    e, sink = engine(inc(n=44, label="8 Example Ln", dlat=0.03, typ="STRU", fd="cleared", made=-3600, fd_at=NOW - 1800))
    ev = one(e, REKINDLE)
    assert ev.fd == "cleared" and len(pushed(sink)) == 1


@pytest.mark.parametrize("made, held", [(-10 * 60, True), (-30 * 60, True), (-45 * 60, False)])
def test_an_open_incident_with_no_unit_known_yet_is_the_fd_toned_out_for_half_an_hour(made, held):
    e, sink = engine(inc(typ="", fd="", made=made))          # a page's own card: no type, no units read yet
    ev = one(e, DUMPSTER)
    assert ev.near_incident == 41
    assert (ev.fd, ev.suppressed, len(pushed(sink))) == (("dispatched", "fd_dispatched", 0) if held else ("", "", 1))


# -- the words alone --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    DUMPSTER_WORKED,
    f"{DUMPSTER}, FD on scene with it",
    f"{DUMPSTER}, the engine is on location",
    f"{DUMPSTER}, the fire chief's on scene",
])
def test_the_fd_said_to_be_there_with_no_incident_of_ours_is_logged(text):
    e, sink = engine()
    ev = one(e, text)
    assert ev.fd == "there" and ev.near_incident is None and ev.suppressed == "fd_there" and pushed(sink) == []


@pytest.mark.parametrize("text", [
    "Car 3, respond to 20 Example Road, caller says she has a fire there with smoke in the kitchen",
    "Car 3, respond to 20 Example Road, dumpster fire, fire department has been notified",
    "Car 3, respond to 20 Example Road, dumpster fire, FD is en route",
    "Car 3, respond to 20 Example Road, dumpster fire, the fire is out with no extension",
])
def test_a_caller_s_fire_there_or_the_fd_notified_is_still_a_pre_alert(text):
    e, sink = engine()
    one(e, text)
    assert [d.priority for _e, d in pushed(sink)] == [4]


def test_the_fd_said_to_be_there_and_more_detail_is_an_fyi_without_a_number():
    e, sink = engine()
    one(e, f"{DUMPSTER_WORKED}, a worker is trapped behind it")
    [(ev, d)] = pushed(sink)
    assert ev.fd == "there" and d.priority == 2 and d.message["body"].endswith(" · FD there")


# -- what counts as the FD's incident ----------------------------------------------------------------------------------

@pytest.mark.parametrize("typ, linked", [("STRU", True), ("INVEST", True), ("BRUSH", True), ("MA", True), ("", True),
                                         ("BLS", False), ("ALS", False), ("MVA", False), ("SERVICE", False),
                                         ("DRILL", False), ("RESCUE", False), ("AIRPORT", False)])
def test_only_fire_incidents_count(typ, linked):
    e, sink = engine(inc(typ=typ, made=-30 * 60))           # e.g. an aided call there half an hour ago
    ev = one(e, DUMPSTER)
    assert (ev.near_incident == 41) is linked and len(pushed(sink)) == (0 if linked else 1)


@pytest.mark.parametrize("text, n, label, dlat, typ, linked", [
    (LAUNDROMAT, 42, "15 Sample St", 0.01, "HAZARD", True),
    (LAUNDROMAT, 42, "15 Sample St", 0.01, "STRU", False),        # gas: a hazard or an investigation only
    (SCHOOL, 43, "30 Test Ave", 0.02, "STRU", True),
    (SCHOOL, 43, "30 Test Ave", 0.02, "HAZARD", False),           # an alarm: an alarm, a fire, an investigation
])
def test_the_types_are_per_category(text, n, label, dlat, typ, linked):
    e, _sink = engine(inc(n=n, label=label, dlat=dlat, typ=typ))
    assert (one(e, text).near_incident == n) is linked


@pytest.mark.parametrize("made, closed, linked", [
    (-8 * 3600, None, True),                                 # a long fire, still open
    (-13 * 3600, None, False),                               # open, but made more than 12 h ago
    (-2 * 3600, -59 * 60, True),                             # closed 59 min ago: linked (a rekindle: pushed)
    (-2 * 3600, -61 * 60, False),                            # closed 61 min ago
    (+10 * 60, None, True),                                  # made just after (the radio heard first)
    (+40 * 60, None, False),
])
def test_the_window(made, closed, linked):
    e, _sink = engine(inc(fd="on_scene" if closed is None else "cleared", made=made, closed=closed))
    ev = one(e, DUMPSTER)
    assert (ev.near_incident == 41) is linked
    assert e.asked == [(NOW - 12 * 3600, NOW - 60 * 60)]


def test_the_window_is_a_setting():
    e, _sink = engine(inc(fd="cleared", made=-3600, closed=-40 * 60), settings={"fd_window_min": 30})
    assert one(e, DUMPSTER).near_incident is None


@pytest.mark.parametrize("metres, linked", [(0, True), (140, True), (160, True), (170, False), (400, False)])
def test_the_radius_for_a_confirmed_address(metres, linked):
    i = inc(label="Some Other Place", dlat=metres * M_LAT)  # no address in common: the distance alone (150 m + 12.5)
    e, _sink = engine(i)
    assert (one(e, DUMPSTER).near_incident == 41) is linked


def test_the_radius_is_a_setting():
    e, _sink = engine(inc(label="Some Other Place", dlat=300 * M_LAT), settings={"fd_radius_m": 400})
    assert one(e, DUMPSTER).near_incident == 41


def test_no_radius_for_an_unconfirmed_reading():
    e, sink = engine(inc(label="Some Other Place", dlat=20 * M_LAT))
    ev = one(e, "Car 3, check on smoke from a dumpster behind maybe 20 Example Road")
    assert not ev.confirmed and ev.near_incident is None and len(pushed(sink)) == 1


def test_no_radius_for_a_street_reading():
    def street(_t, _s):
        return Place("street", LAT, LON, 300.0, f"Example Rd, {PC}", PC, True, 0.6)
    e, _sink = engine(inc(label="Some Other Place"), loc=street)
    assert one(e, DUMPSTER).near_incident is None


@pytest.mark.parametrize("address, point, linked", [
    ("20 EXAMPLE ROAD", False, True),                         # the same address as typed, no point
    ("44 Example Rd", False, True),                           # the same block, no point
    ("44 Example Rd", True, True),                            # the same block, its point beside it
    ("120 Example Rd", False, False),                         # the next block
    ("20 Sample St", False, False),                           # another street
])
def test_the_same_address_or_block(address, point, linked):
    i = inc(label=address, dlat=10 * M_LAT) if point else {**inc(label=address), "lat": None, "lon": None}
    e, _sink = engine(i)
    assert (one(e, DUMPSTER).near_incident == 41) is linked


def test_the_same_block_far_away_is_another_place():
    e, _sink = engine(inc(label="44 Example Rd", dlat=600 * M_LAT))         # its point 600 m off: another street
    assert one(e, DUMPSTER).near_incident is None


def test_the_town_of_a_raw_address_counts_when_there_is_no_normalized_one():
    i = {**inc(), "address": "", "label": "", "address_raw": "20 EXAMPLE RD RYE_BROOK", "lat": None, "lon": None}
    e, _sink = engine(i)
    assert one(e, DUMPSTER).near_incident is None
    i = {**i, "address_raw": "20 EXAMPLE RD PORT_CHESTER"}
    e, _sink = engine(i)
    assert one(e, DUMPSTER).near_incident == 41


def test_the_same_address_in_another_town_is_another_place():
    i = {**inc(), "address": "20 Example Rd, Rye Brook", "label": "20 Example Rd, Rye Brook"}
    e, _sink = engine(i)
    assert one(e, DUMPSTER).near_incident is None


def test_on_scene_wins_over_dispatched_at_the_place():
    e, _sink = engine(inc(), inc(n=45, fd="on_scene", made=-1200))
    ev = one(e, DUMPSTER)
    assert ev.near_incident == 45 and ev.suppressed == "fd_on_scene"


# -- no place ---------------------------------------------------------------------------------------------------------

NOWHERE = lambda _t, _s: None                                    # noqa: E731


def test_no_place_matches_an_open_incidents_street_in_the_sources_town():
    e, sink = engine(inc(), loc=NOWHERE)
    ev = one(e, "Car 3, smoke from a dumpster on Example Road")
    assert ev.precision == "none" and ev.near_incident == 41 and ev.suppressed == "fd_dispatched" and pushed(sink) == []


@pytest.mark.parametrize("text, i", [
    ("Car 3, smoke from a dumpster, location unknown", inc()),                    # no street said
    ("Car 3, smoke from a dumpster on Example Road", inc(fd="cleared", closed=-60)),   # closed: by place only
    ("Car 3, smoke from a dumpster on Example Street", inc()),                    # another suffix
    ("Car 3, smoke from a dumpster on Example Road in Rye Brook", inc()),         # another town said
    ("Car 3, smoke from a dumpster on Example Road",                              # a town not the source's, not said
     {**inc(), "address": "20 Example Rd, White Plains"}),
])
def test_no_place_and_no_street_match_is_a_pre_alert(text, i):
    e, sink = engine(i, loc=NOWHERE)
    ev = one(e, text)
    assert ev.near_incident is None and ev.fd == "" and len(pushed(sink)) == 1


def test_no_place_and_its_town_said_matches_outside_the_sources_towns():
    i = {**inc(), "address": "20 Example Rd, White Plains"}
    assert fire_street_said(i, ["smoke on example road in white plains"], {"white plains"}, {"port chester"})
    assert fire_street_said({"address": "Example Rd & Sample St, Port Chester"}, ["smoke at sample street"],
                            set(), {"port chester"})                       # an intersection's street
    assert not fire_street_said({"address": "12 Main, Port Chester"}, ["fire on main"], set(), {"port chester"})


# -- what it leaves alone, and later updates --------------------------------------------------------------------------

@pytest.mark.parametrize("text, category", [
    ("Car 3, respond to 20 Example Road, shots fired", "weapons"),
    ("Car 3, respond to 20 Example Road, MVA with injuries", "mva_injury"),
])
def test_other_categories_are_never_checked(text, category):
    e, sink = engine(inc(fd="on_scene"))
    [ev] = e.feed(row("t1", text, NOW))
    assert ev.category == category and ev.near_incident is None and ev.fd == "" and ev.suppressed == ""
    assert [p.category for p, _d in pushed(sink)] == [category]
    assert e.asked == []


def test_a_pre_alert_pushed_before_the_fd_was_known_is_linked_never_lowered():
    e, sink = engine()
    assert one(e, DUMPSTER).pushed_at is not None
    e.fire_incidents = lambda _a, _b: [inc(fd="on_scene", made=+60)]
    one(e, "smoke from the dumpster at 20 Example Road getting heavier", "t2", NOW + 90)
    [ev] = e.events.open_events()
    assert ev.near_incident == 41 and ev.fd == "" and ev.mode == "push" and ev.suppressed == "" and ev.priority == 4
    assert len(pushed(sink)) == 1


def test_a_held_call_whose_next_words_add_something_becomes_an_fyi():
    e, sink = engine(inc(n=42, label="15 Sample St", dlat=0.01, typ="HAZARD"))
    assert one(e, LAUNDROMAT).suppressed == "fd_dispatched"
    one(e, "odor of gas at 15 Sample Street, the manager is still inside", "t2", NOW + 30)
    [(ev, d)] = pushed(sink)
    assert ev.suppressed == "" and d.priority == 2


def test_a_pushed_fyi_keeps_its_priority_on_later_updates():
    e, sink = engine(inc(n=42, label="15 Sample St", dlat=0.01, typ="HAZARD"))
    one(e, f"{LAUNDROMAT}, an employee is still inside")
    e.fire_incidents = lambda _a, _b: [inc(n=42, label="15 Sample St", dlat=0.01, typ="HAZARD", fd="on_scene")]
    one(e, "odor of gas at 15 Sample Street, still strong", "t2", NOW + 40)
    [ev] = e.events.open_events()
    assert ev.fd == "dispatched" and ev.priority == 2 and ev.suppressed == "" and len(pushed(sink)) == 1


def test_a_hold_let_go_late_stays_at_log():
    e, sink = engine(inc(n=42, label="15 Sample St", dlat=0.01, typ="HAZARD"))
    assert one(e, LAUNDROMAT).suppressed == "fd_dispatched"
    e.fire_incidents = lambda _a, _b: []                     # the incident dismissed since
    one(e, "odor of gas at 15 Sample Street, still strong", "t2", NOW + RELEASE_S + 60)
    [ev] = e.events.open_events()
    assert ev.suppressed == "fd_late" and ev.mode == "log" and pushed(sink) == []


def test_a_rekindle_merged_into_a_held_call_is_a_pre_alert():
    from datetime import datetime
    e, sink = engine(inc())
    assert one(e, DUMPSTER).suppressed == "fd_dispatched"                    # the FD on its way: held
    cleared = NOW + 15 * 60                                                   # the FD clears and leaves
    e.fire_incidents = lambda _a, _b: [inc(fd="cleared", fd_at=cleared)]
    ev = one(e, "Car 3, smoke showing again from the dumpster behind 20 Example Road", "t2", NOW + 20 * 60)
    assert ev.fd == "cleared" and ev.suppressed == "" and ev.mode == "push"
    [(_ev, d)] = pushed(sink)
    assert d.priority == 4 and not d.message["title"].startswith("FYI")
    assert d.message["body"].endswith(f" · FD here {datetime.fromtimestamp(cleared).strftime('%H:%M')} · #26-000041")


def test_a_bare_chief_on_a_pd_channel_is_not_the_fd():
    e, sink = engine()
    assert one(e, f"{DUMPSTER}, the chief is on scene").fd == "" and len(pushed(sink)) == 1


def test_a_hold_let_go_soon_is_a_pre_alert():
    e, sink = engine(inc(n=42, label="15 Sample St", dlat=0.01, typ="HAZARD"))
    one(e, LAUNDROMAT)
    e.fire_incidents = lambda _a, _b: []
    one(e, "odor of gas at 15 Sample Street, still strong", "t2", NOW + 60)
    [(ev, d)] = pushed(sink)
    assert ev.suppressed == "" and d.priority == 4 and not d.message["title"].startswith("FYI")


def test_an_fyi_number_passes_the_scrub():
    ev = Event(domain="police", pack="police", category="fire_report", precision="address", label_full="20 Example Rd",
               confirmed=True, first_at=NOW, level="high", fd="on_scene")
    msg = push_mod.compose(ev, "PD", {"label": "Smoke / fire reported"}, number="26-000041")
    assert msg["title"].startswith("FYI · ") and msg["body"].endswith("FD on scene · #26-000041")
    assert push_mod.scrub(msg) == ""
    assert push_mod.scrub({"title": "", "body": "radio 7000123"}) == "radio_id"            # otherwise as before


# -- the CAD's side ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ours, feeds, closed, want", [
    ([{"onscene_at": 1.0, "cleared_at": None}], [], None, "on_scene"),
    ([{"onscene_at": None, "cleared_at": None}], [], None, "dispatched"),
    ([{"onscene_at": 1.0, "cleared_at": 5.0}], [], None, "cleared"),
    ([{"onscene_at": 1.0, "cleared_at": 5.0}, {"onscene_at": None, "cleared_at": None}], [], None, "dispatched"),
    ([], [{"units": ["X1"], "unit_times": {"X1": {"dispatched": 1.0, "on_location": 2.0}}}], None, "on_scene"),
    ([], [{"units": ["X1"], "unit_times": {"X1": {"dispatched": 1.0}}}], None, "dispatched"),
    ([], [{"units": ["X1"], "unit_times": {}}], None, "dispatched"),
    ([], [{"units": ["X1"], "unit_times": {"X1": {"on_location": 2.0, "in_service": 6.0}}}], None, "cleared"),
    ([], [{"units": ["X1"], "unit_times": {"X1": {"on_location": 2.0}}, "closed": True}], None, "cleared"),
    ([], [{"units": [], "unit_times": {}}], None, ""),
    ([], [], None, ""),
    ([{"onscene_at": 1.0, "cleared_at": None}], [], 9.0, "cleared"),
])
def test_where_the_fd_is(ours, feeds, closed, want):
    assert fire_state({"closed_at": closed}, ours, feeds)[0] == want


def test_the_cad_reads_its_own_and_the_feeds_incidents(tmp_path):
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    now = cad.clock()
    with cad.store.tx() as db:
        def add(n, status, created, closed=None, emergency=1, typ="STRU"):
            return db.execute(
                "INSERT INTO incidents (number, type, status, source, created_at, closed_at, lat, lon, address, geo_label, "
                "emergency) VALUES (?, ?, ?, 'manual', ?, ?, 41.0, -73.68, '20 Example Rd, Port Chester', "
                "'20 Example Rd, Port Chester', ?)", (n, typ, status, created, closed, emergency)).lastrowid
        on = add("26-000101", "active", now - 600)
        long_fire = add("26-000102", "active", now - 8 * 3600, typ="INVEST")
        feed = add(None, "queued", now - 400, typ="")
        rekindle = add("26-000103", "closed", now - 7000, now - 2000)
        add(None, "dismissed", now - 300)
        add("26-000104", "active", now - 300, emergency=0)
        add("26-000105", "closed", now - 9000, now - 5000)                   # closed too long ago
        add("26-000106", "active", now - 13 * 3600)                          # older than the open window asked
        u = db.execute("INSERT INTO units (name, kind, status, status_at) VALUES ('E99', 'engine', 'on scene', ?)",
                       (now,)).lastrowid
        db.execute("INSERT INTO assignments (incident_id, unit_id, dispatched_at, onscene_at) VALUES (?, ?, ?, ?)",
                   (on, u, now - 590, now - 300))
        db.execute("INSERT INTO source_reports (source, ref, rank, label, incident_id, first_seen, updated_at, data) "
                   "VALUES ('xmpl', 'A1', 1, 'Feed', ?, ?, ?, ?)",
                   (feed, now - 400, now, json.dumps({"units": ["X1"], "unit_times": {"X1": {"dispatched": now - 400}}})))
    got = {g["id"]: g for g in cad.intel_fire_incidents(now - 12 * 3600, now - 3600)}
    assert set(got) == {on, long_fire, feed, rekindle}
    assert [got[i]["fd"] for i in (on, long_fire, feed, rekindle)] == ["on_scene", "", "dispatched", "cleared"]
    assert got[rekindle]["fd_at"] == now - 2000 and got[on]["type"] == "STRU" and got[on]["number"] == "26-000101"
    assert cad.intel.fire_incidents == cad.intel_fire_incidents


def test_the_event_store_takes_the_fd_column_on_an_old_table(tmp_path):
    db = sqlite3.connect(tmp_path / "old.sqlite")
    db.execute("CREATE TABLE intel_events (id INTEGER PRIMARY KEY, domain TEXT NOT NULL, pack TEXT NOT NULL, category "
               "TEXT NOT NULL, subject TEXT NOT NULL, source_key TEXT NOT NULL DEFAULT '', first_at REAL NOT NULL, "
               "last_at REAL NOT NULL, until REAL NOT NULL, status TEXT NOT NULL, updated_at REAL NOT NULL)")
    db.commit()

    class Store:                                             # the few calls SqlEvents makes when it starts
        lock = threading.RLock()
    st = Store()
    st.db = db
    SqlEvents(st)
    assert "fd" in {r[1] for r in db.execute("PRAGMA table_info(intel_events)")}


def test_the_console_shows_it_terse_from_the_lookup(tmp_path):
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    cad.intel._numbers[7] = ("26-000207", None)
    ev = Event(domain="police", pack="police", category="fire_report", first_at=NOW, last_at=NOW, fd="on_scene",
               near_incident=7, mode="push")
    cad.intel.events.save(ev)
    assert cad.intel.events.get(ev.id).fd == "on_scene"
    [shown] = [cad.intel_admin.shown(r) for r in cad.intel.events.rows("id = ?", (ev.id,))]
    assert shown["fd"] == "on_scene" and shown["fd_shown"] == "FD on scene · #26-000207"


def test_the_settings_take_the_window_and_the_radius():
    from sizeup.cad.store import CadError
    from sizeup.intel.admin import clean_value
    from sizeup.intel.policy import DEFAULTS
    assert DEFAULTS["fd_radius_m"] == 150 and DEFAULTS["fd_window_min"] == 60
    assert clean_value("fd_window_min", 45, {}) == 45 and clean_value("fd_radius_m", 400, {}) == 400
    with pytest.raises(CadError):
        clean_value("fd_radius_m", 5, {})
