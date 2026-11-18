"""A fire-related PD event when the FD is already there (sizeup/intel/engine.py _fire; admin, 2026-11-17: "the idea is
to get a jump or alert ahead of time mostly, or an FYI if things are changing; when fire related, the reported PD
dispatch might not be the right thing if fire is already on scene").

Before a push of a "fire_related" category (the police pack's fire_report, fire_call, hazmat, alarm), our incidents
at its place (the CAD's own and the incident sources', the county feed's too) first seen in the last fd_window_min:
  FD dispatched   no pre-alert (log); an FYI at priority 2 only when the words add something (people inside, ...)
  FD on scene     no pre-alert (log); an FYI only for a safety change (a weapon on scene, ...)
  none            pushed as before
and the words alone ("FD's out with ...") count as the FD being there. Everything here is invented (this repository
is public): the street (Example Rd), the numbers, the unit."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.intel.engine import IntelEngine, RecordingSink, fire_street_said
from sizeup.intel.events import MemoryEvents, SqlEvents
from sizeup.intel.model import Event, Place
from sizeup.intel import push as push_mod
from tests.test_intel import KEY, Clock, row

PC = "Port Chester"
LAT, LON = 41.0, -73.68
NOW = 10_000.0
TODAY = "Six, can you respond over to 12 Example Road? FD's out with a car fire in the roadway"   # the shape of the call
CAR_FIRE = "Car 6, respond to 12 Example Road for a car fire in the roadway"
OURS = {"id": 41, "number": "26-000041", "lat": LAT, "lon": LON, "label": "12 Example Rd, Port Chester",
        "address": "12 Example Rd, Port Chester", "address_raw": "", "created_at": NOW - 600, "closed_at": None,
        "fd": "dispatched"}
M_LAT = 1 / 111_320                                         # a metre north, in degrees


def at_12(text, _scope):
    """A stand-in gazetteer: "12 Example Road" is 12 Example Rd (confirmed); nothing else has a place."""
    if "12 example road" in text.lower():
        return Place("address", LAT, LON, 25.0, "12 Example Rd, Port Chester", PC, True, 0.8)
    return None


def engine(*incs, settings=None, locate=at_12):
    sink = RecordingSink()
    e = IntelEngine(settings={"intel": settings or {}}, events=MemoryEvents(), sink=sink, thread=False,
                    clock=Clock(NOW), locate=locate)
    e.fire_incidents = lambda since: [dict(i) for i in incs]
    return e, sink


def pushed(sink):
    return [(ev, d) for ev, d in sink.pushes if d.would_push]


def one(e, sink, text, tid="t1", at=NOW):
    out = e.feed(row(tid, text, at))
    [ev] = [x for x in out if x.category in ("fire_report", "fire_call", "hazmat", "alarm")] or [None]
    return ev


# -- the four answers ---------------------------------------------------------------------------------------------------

def test_todays_call_with_an_engine_on_scene_is_logged_and_linked():
    e, sink = engine({**OURS, "fd": "on_scene"})
    ev = one(e, sink, TODAY)
    assert ev.category == "fire_report" and ev.label_full == "12 Example Rd, Port Chester"
    assert ev.near_incident == 41 and ev.fd == "on_scene"
    assert ev.suppressed == "fd_on_scene" and ev.mode == "log" and ev.pushed_at is None
    assert pushed(sink) == []


def test_fd_dispatched_and_nothing_added_is_logged():
    e, sink = engine(OURS)
    ev = one(e, sink, CAR_FIRE)
    assert ev.near_incident == 41 and ev.fd == "dispatched" and ev.suppressed == "fd_dispatched" and ev.mode == "log"
    assert pushed(sink) == []


@pytest.mark.parametrize("extra", ["people still inside the car", "the driver is trapped", "the fire is spreading to the house",
                                   "one person injured", "they have propane tanks in the trunk"])
def test_fd_dispatched_and_the_words_add_something_is_a_low_priority_fyi(extra):
    e, sink = engine(OURS)
    ev = one(e, sink, f"{CAR_FIRE}, {extra}")
    assert ev.near_incident == 41 and ev.fd == "dispatched" and ev.suppressed == "" and ev.mode == "push"
    [(pev, d)] = pushed(sink)
    assert d.priority == 2 and d.message["priority"] == 2
    assert d.message["title"] == "FYI · PD · Smoke / fire reported · Port Chester"
    assert d.message["body"].endswith(" · FD dispatched · #26-000041")


def test_fd_on_scene_and_nothing_safety_is_suppressed_even_with_people_inside():
    e, sink = engine({**OURS, "fd": "on_scene"})
    ev = one(e, sink, f"{CAR_FIRE}, people still inside the car")             # news to us, not to the crew on scene
    assert ev.suppressed == "fd_on_scene" and pushed(sink) == []


@pytest.mark.parametrize("extra", ["caller says there's a weapon on scene", "the driver is fighting with bystanders",
                                   "there was an explosion", "officer down"])
def test_fd_on_scene_and_a_safety_change_is_a_safety_fyi(extra):
    e, sink = engine({**OURS, "fd": "on_scene"})
    ev = one(e, sink, f"{CAR_FIRE}, {extra}")
    assert ev.fd == "on_scene" and ev.suppressed == "" and ev.near_incident == 41
    [(_ev, d)] = pushed(sink)
    assert d.priority == 4 and d.message["title"].startswith("FYI · ")
    assert d.message["body"].endswith(" · FD on scene · #26-000041")


def test_no_incident_pushes_as_before():
    e, sink = engine()
    ev = one(e, sink, CAR_FIRE)
    assert ev.fd == "" and ev.near_incident is None and ev.suppressed == ""
    [(_ev, d)] = pushed(sink)
    assert d.priority == 4 and d.message["title"] == "PD · Smoke / fire reported · Port Chester"
    assert "FD" not in d.message["body"]


def test_no_hook_pushes_as_before():
    e, sink = engine()
    e.fire_incidents = None
    ev = one(e, sink, CAR_FIRE)
    assert ev.fd == "" and [d.priority for _e, d in pushed(sink)] == [4]


def test_a_broken_lookup_pushes_as_before():
    logs = []
    e, sink = engine()

    def boom(_since):
        raise RuntimeError("db gone")
    e.fire_incidents, e.log = boom, logs.append
    one(e, sink, CAR_FIRE)
    assert len(pushed(sink)) == 1 and logs == ["intel: fire incidents: RuntimeError"]


# -- the words alone ------------------------------------------------------------------------------------------------------

def test_fds_out_with_it_with_no_incident_of_ours_is_logged():
    e, sink = engine()                                       # another department's call, not in our feed
    ev = one(e, sink, TODAY)
    assert ev.fd == "there" and ev.near_incident is None and ev.suppressed == "fd_there" and pushed(sink) == []


@pytest.mark.parametrize("text", [
    "respond to 12 Example Road, car fire, the FD's on scene with it",
    "respond to 12 Example Road, fire is on location with a car fire",
    "respond to 12 Example Road, car fire, FD are there with it",
])
def test_the_other_ways_of_saying_the_fd_is_there(text):
    e, sink = engine()
    ev = one(e, sink, text)
    assert ev.fd == "there" and ev.suppressed == "fd_there" and pushed(sink) == []


@pytest.mark.parametrize("text", [
    "respond to 12 Example Road for a car fire in the roadway, FD is en route",        # dispatched elsewhere: a jump
    "respond to 12 Example Road, car fire, I'll be notifying the FD",
])
def test_fd_notified_or_en_route_is_still_a_pre_alert(text):
    e, sink = engine()
    ev = one(e, sink, text)
    assert ev.fd == "" and len(pushed(sink)) == 1


def test_fds_out_with_it_and_people_trapped_is_an_fyi_without_a_number():
    e, sink = engine()
    one(e, sink, f"{TODAY}, the driver is trapped")
    [(ev, d)] = pushed(sink)
    assert ev.fd == "there" and d.priority == 2 and d.message["title"].startswith("FYI · ")
    assert d.message["body"].endswith(" · FD there")


# -- the window and the radius ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("created, closed, linked", [
    (NOW - 59 * 60, None, True),                             # first seen 59 min before
    (NOW - 61 * 60, None, False),                            # 61 min: too old
    (NOW - 50 * 60, NOW - 30 * 60, True),                    # closed within the window
    (NOW + 10 * 60, None, True),                             # made just after (the radio heard first)
    (NOW + 40 * 60, None, False),                            # ... but not that long after
])
def test_the_window(created, closed, linked):
    e, sink = engine({**OURS, "created_at": created, "closed_at": closed})
    ev = one(e, sink, CAR_FIRE)
    assert (ev.near_incident == 41) is linked and (ev.suppressed == "fd_dispatched") is linked
    assert len(pushed(sink)) == (0 if linked else 1)


def test_the_window_is_a_setting():
    e, _sink = engine({**OURS, "created_at": NOW - 40 * 60}, settings={"fd_window_min": 30})
    assert one(e, _sink, CAR_FIRE).near_incident is None
    e, _sink = engine({**OURS, "created_at": NOW - 80 * 60}, settings={"fd_window_min": 90})
    assert one(e, _sink, CAR_FIRE).near_incident == 41


@pytest.mark.parametrize("metres, linked", [(0, True), (250, True), (300, True), (330, False), (500, False)])
def test_the_radius(metres, linked):
    # no address in common: only the distance (300 m and half the place's 25 m radius)
    inc = {**OURS, "label": "", "address": "", "lat": LAT + metres * M_LAT}
    e, sink = engine(inc)
    ev = one(e, sink, CAR_FIRE)
    assert (ev.near_incident == 41) is linked


def test_the_radius_is_a_setting():
    inc = {**OURS, "label": "", "address": "", "lat": LAT + 500 * M_LAT}
    e, sink = engine(inc, settings={"fd_radius_m": 600})
    assert one(e, sink, CAR_FIRE).near_incident == 41


@pytest.mark.parametrize("address, linked", [
    ("12 EXAMPLE ROAD", True),                               # the same address, typed (no point)
    ("40 Example Rd", True),                                 # the same block
    ("140 Example Rd", False),                               # the next block
    ("12 Sample St", False),                                 # another street
])
def test_the_same_address_or_block_without_a_point(address, linked):
    inc = {**OURS, "label": "", "address": address, "lat": None, "lon": None}
    e, sink = engine(inc)
    assert (one(e, sink, CAR_FIRE).near_incident == 41) is linked


def test_on_scene_wins_over_dispatched_nearby():
    e, sink = engine(OURS, {**OURS, "id": 42, "number": "26-000042", "fd": "on_scene", "created_at": NOW - 1200})
    ev = one(e, sink, CAR_FIRE)
    assert ev.near_incident == 42 and ev.suppressed == "fd_on_scene"


# -- no place -------------------------------------------------------------------------------------------------------------

def test_no_place_matches_an_open_incidents_street_in_the_words():
    e, sink = engine(OURS, locate=lambda _t, _s: None)
    ev = one(e, sink, "car fire on Example Road near the gas station")
    assert ev.precision == "none" and ev.near_incident == 41 and ev.suppressed == "fd_dispatched"
    assert pushed(sink) == []


@pytest.mark.parametrize("text, inc", [
    ("car fire in the roadway, unknown location", OURS),                       # no street said
    ("car fire on Example Road", {**OURS, "closed_at": NOW - 60}),             # closed: only by place, never by a street
    ("car fire on Example Street", OURS),                                      # another suffix
])
def test_no_place_and_no_street_match_pushes_as_before(text, inc):
    e, sink = engine(inc, locate=lambda _t, _s: None)
    ev = one(e, sink, text)
    assert ev.near_incident is None and ev.fd == "" and len(pushed(sink)) == 1


def test_fire_street_said_reads_an_intersection_too():
    inc = {"address": "Example Rd & Sample St"}
    assert fire_street_said(inc, ["smoke at sample street"]) and fire_street_said(inc, ["fire on example road"])
    assert not fire_street_said({"address": "12 Main"}, ["fire on main"])         # no suffix: too loose


# -- what it leaves alone -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, category", [
    ("respond to 12 Example Road, shots fired", "weapons"),
    ("respond to 12 Example Road, MVA with injuries", "mva_injury"),
])
def test_other_categories_are_never_checked(text, category):
    e, sink = engine({**OURS, "fd": "on_scene"})
    [ev] = e.feed(row("t1", text, NOW))
    assert ev.category == category and ev.near_incident is None and ev.fd == "" and ev.suppressed == ""
    assert [p.category for p, _d in pushed(sink)] == [category]


@pytest.mark.parametrize("text, category", [
    ("respond to 12 Example Road, odor of gas in the basement", "hazmat"),
    ("respond to 12 Example Road, commercial fire alarm", "alarm"),
])
def test_gas_and_alarms_are_fire_related(text, category):
    e, sink = engine({**OURS, "fd": "on_scene"})
    [ev] = e.feed(row("t1", text, NOW))
    assert ev.category == category and ev.suppressed == "fd_on_scene" and pushed(sink) == []


def test_a_pre_alert_pushed_before_the_fd_was_known_is_linked_never_lowered():
    e, sink = engine()
    ev = one(e, sink, CAR_FIRE)
    assert ev.pushed_at is not None
    e.fire_incidents = lambda since: [{**OURS, "fd": "on_scene", "created_at": NOW + 60}]
    one(e, sink, "car fire at 12 Example Road, still burning", "t2", NOW + 90)
    [ev] = e.events.open_events()
    assert ev.near_incident == 41 and ev.fd == "" and ev.mode == "push" and ev.suppressed == "" and ev.priority == 4
    assert len(pushed(sink)) == 1


def test_a_held_call_whose_next_words_add_something_becomes_an_fyi():
    e, sink = engine(OURS)
    ev = one(e, sink, CAR_FIRE)
    assert ev.suppressed == "fd_dispatched"
    one(e, sink, "car fire at 12 Example Road, caller says the driver is trapped", "t2", NOW + 30)
    [(ev, d)] = pushed(sink)
    assert ev.suppressed == "" and d.priority == 2 and d.message["title"].startswith("FYI · ")


def test_the_fire_is_out_is_not_the_fd_being_there():
    e, sink = engine()
    ev = one(e, sink, "respond to 12 Example Road, car fire, the fire is out with no extension")
    assert ev.fd == ""


def test_a_pushed_fyi_number_passes_the_scrub():
    ev = Event(domain="police", pack="police", category="fire_report", precision="address", label_full="12 Example Rd",
               confirmed=True, first_at=NOW, level="high", fd="on_scene")
    msg = push_mod.compose(ev, "PD", {"label": "Smoke / fire reported"}, number="26-000041")
    assert msg["title"].startswith("FYI · ") and msg["body"].endswith("FD on scene · #26-000041")
    assert push_mod.scrub(msg) == ""
    assert push_mod.scrub({"title": "", "body": "radio 7000123"}) == "radio_id"            # the scrub is otherwise as was


# -- the CAD's side -------------------------------------------------------------------------------------------------------

def test_the_cad_reads_its_own_and_the_feeds_incidents_with_where_the_fd_is(tmp_path):
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    now = cad.clock()
    with cad.store.tx() as db:
        def inc(n, status, created, closed=None, emergency=1):
            return db.execute(
                "INSERT INTO incidents (number, type, status, source, created_at, closed_at, lat, lon, address, geo_label, "
                "emergency) VALUES (?, 'FIRE', ?, 'manual', ?, ?, 41.0, -73.68, '12 Example Rd', '12 Example Rd, Port "
                "Chester', ?)", (n, status, created, closed, emergency)).lastrowid
        on = inc("26-000101", "active", now - 600)                    # a unit of ours on scene
        disp = inc("26-000102", "active", now - 500)                  # dispatched only
        cleared = inc("26-000103", "active", now - 450)               # on scene, then cleared
        feed = inc(None, "queued", now - 400)                         # the county feed's, queued: its unit on location
        feed_gone = inc(None, "queued", now - 350)                    # ... back in service
        inc("26-000104", "closed", now - 3000, now - 2000)            # closed within the window
        inc(None, "dismissed", now - 300)
        inc("26-000105", "active", now - 300, emergency=0)            # a notification
        inc("26-000106", "active", now - 5 * 3600)                    # too old
        u = db.execute("INSERT INTO units (name, kind, status, status_at) VALUES ('E99', 'engine', 'on scene', ?)",
                       (now,)).lastrowid
        db.execute("INSERT INTO assignments (incident_id, unit_id, dispatched_at, onscene_at) VALUES (?, ?, ?, ?)",
                   (on, u, now - 590, now - 300))
        db.execute("INSERT INTO assignments (incident_id, unit_id, dispatched_at) VALUES (?, ?, ?)", (disp, u, now - 490))
        db.execute("INSERT INTO assignments (incident_id, unit_id, dispatched_at, onscene_at, cleared_at) VALUES (?, ?, ?, ?, ?)",
                   (cleared, u, now - 440, now - 400, now - 100))
        for ref, iid, times in (("A1", feed, {"dispatched": now - 400, "on_location": now - 200}),
                                ("A2", feed_gone, {"dispatched": now - 350, "on_location": now - 200, "in_service": now - 50})):
            db.execute("INSERT INTO source_reports (source, ref, rank, label, incident_id, first_seen, updated_at, data) "
                       "VALUES ('xmpl', ?, 1, 'Feed', ?, ?, ?, ?)",
                       (ref, iid, now - 400, now, json.dumps({"units": ["X1"], "unit_times": {"X1": times}, "closed": False})))
    got = {g["id"]: g for g in cad.intel_fire_incidents(now - 3600)}
    assert {i: g["fd"] for i, g in got.items() if i in (on, disp, cleared, feed, feed_gone)} == {
        on: "on_scene", disp: "dispatched", cleared: "dispatched", feed: "on_scene", feed_gone: "dispatched"}
    assert len(got) == 6 and got[on]["number"] == "26-000101" and got[on]["address"] == "12 Example Rd"
    assert cad.intel.fire_incidents == cad.intel_fire_incidents


def test_the_event_store_takes_the_fd_column_on_an_old_table(tmp_path):
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    ev = Event(domain="police", pack="police", category="fire_report", first_at=NOW, fd="on_scene", near_incident=7,
               suppressed="fd_on_scene")
    cad.intel.events.save(ev)
    assert cad.intel.events.get(ev.id).fd == "on_scene"
    db = sqlite3.connect(tmp_path / "old.sqlite")
    db.execute("CREATE TABLE intel_events (id INTEGER PRIMARY KEY, domain TEXT NOT NULL, pack TEXT NOT NULL, category "
               "TEXT NOT NULL, subject TEXT NOT NULL, source_key TEXT NOT NULL DEFAULT '', first_at REAL NOT NULL, last_at REAL NOT NULL, until REAL NOT NULL, "
               "status TEXT NOT NULL, updated_at REAL NOT NULL)")
    db.commit()

    class Store:                                             # the few calls SqlEvents makes when it starts
        import threading
        lock = threading.RLock()

    st = Store()
    st.db = db
    SqlEvents(st)
    assert "fd" in {r[1] for r in db.execute("PRAGMA table_info(intel_events)")}


def test_the_admin_shows_it_terse(tmp_path):
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    with cad.store.tx() as db:
        iid = db.execute("INSERT INTO incidents (number, type, status, source, created_at) VALUES ('26-000201', 'FIRE', "
                         "'active', 'manual', ?)", (cad.clock(),)).lastrowid
    ev = Event(domain="police", pack="police", category="fire_report", first_at=NOW, last_at=NOW, fd="on_scene",
               near_incident=iid, mode="push")
    cad.intel.events.save(ev)
    [shown] = [cad.intel_admin.shown(r) for r in cad.intel.events.rows("id = ?", (ev.id,))]
    assert shown["fd"] == "on_scene" and shown["fd_shown"] == "FD on scene · #26-000201"


def test_the_settings_take_the_window_and_the_radius():
    from sizeup.intel.admin import clean_value
    from sizeup.cad.store import CadError
    assert clean_value("fd_window_min", 45, {}) == 45 and clean_value("fd_radius_m", 400, {}) == 400
    with pytest.raises(CadError):
        clean_value("fd_radius_m", 5, {})
