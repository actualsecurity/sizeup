"""A type moved under Notification as a subtype (Settings > Lists: the Drill type retired, a Drill subtype of
Notification) and what 60 Control automation does with a non-emergency event (sizeup/cad/automode.py "non-emergency",
Cad.moved_type). Synthetic only: a scripted feed (tests/fake_feed.py), made-up places, units and times."""

from __future__ import annotations

import pytest

from sizeup.cad.store import CadError
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0

pytestmark = pytest.mark.hand_dispatch_gate
DRILL_PAGE = ("Port Chester Fire Department, Engine 60, Ladder 31, respond for a drill. 12 Example Avenue, cross of "
              "Sample Street. Time out 1432.")


def post(cad, section, **body):
    return cad.settings_post(section, dict(body, operator=body.get("operator", "JD")))


def move_drill(cad, code: str = "DRILL", name: str = "Drill", retire: bool = True) -> None:
    """What the admin does in Settings > Lists: a Drill subtype, on Notification's subtypes, the Drill type retired."""
    post(cad, "event_subtypes", action="add", code=code, item={"name": name})
    post(cad, "type_subtypes", action="set", type="NOTIF", subtypes=[*cad.lists.type_subtypes["NOTIF"], code])
    if retire:
        post(cad, "event_types", action="retire", code="DRILL")


@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


# -- the type ---------------------------------------------------------------------------------------------------------


def test_a_feed_drill_follows_the_type_moved_under_notification(rig):
    cad = rig.cad
    assert cad.map_type("DRILL", "County feed") == ("DRILL", "", "")             # before the move: the Drill type
    move_drill(cad)
    assert cad.map_type("DRILL", "County feed") == ("NOTIF", "DRILL", "")
    assert cad.map_type("drill", "County feed") == ("NOTIF", "DRILL", "")
    typ, sub, note = cad.map_type("DRILL,SAMPLE", "County feed")                 # a feed's own subtype: kept as a note
    assert (typ, sub) == ("NOTIF", "DRILL") and note == "type from County feed: DRILL,SAMPLE"
    assert cad.map_type("NOTIF/DRILL", "County feed") == ("NOTIF", "DRILL", "")  # our code as it is
    assert cad.lists.is_emergency("NOTIF", "DRILL") is False                     # Notification's class, inherited


def test_the_subtype_is_found_by_the_old_types_name_too(rig):
    move_drill(rig.cad, code="TRAINING-EVOL", name="Drill")
    assert rig.cad.map_type("DRILL", "County feed") == ("NOTIF", "TRAINING-EVOL", "")


def test_a_deleted_type_still_follows_its_move(rig):
    move_drill(rig.cad, retire=False)
    try:
        post(rig.cad, "event_types", action="delete", code="DRILL")
    except CadError:
        pytest.skip("the data file's Drill type is in use here: only retiring applies")
    assert "DRILL" not in {t["code"] for t in rig.cad.lists.event_types}
    assert rig.cad.map_type("DRILL", "County feed") == ("NOTIF", "DRILL", "")


def test_an_active_type_a_word_never_a_type_or_two_offers_are_not_moved(rig):
    cad = rig.cad
    move_drill(cad, retire=False)                                                # the Drill type still active: it wins
    assert cad.map_type("DRILL", "County feed") == ("DRILL", "", "")
    assert cad.moved_type("POLICE") == ("", "")                                  # a subtype, never a type: no guess
    post(cad, "event_types", action="retire", code="DRILL")
    post(cad, "type_subtypes", action="set", type="SERVICE", subtypes=[*cad.lists.type_subtypes["SERVICE"], "DRILL"])
    assert cad.moved_type("DRILL") == ("", "")                                   # two types offer it: a person picks
    assert cad.map_type("DRILL", "County feed")[:2] == ("", "")


# -- 60 Control automation on a non-emergency event ----------------------------------------------------------------


def test_a_county_drill_is_accepted_as_a_notification_and_nothing_is_dispatched(rig):
    move_drill(rig.cad)
    before = {n: rig.status(n) for n in ("E60", "L31")}
    rig.created(type="DRILL")
    inc = rig.inc()
    assert (inc["type"], inc["subtype"], inc["emergency"]) == ("NOTIF", "DRILL", False)
    assert inc["status"] == "pending" and inc["number"]                          # accepted: Non-emergency events
    assert inc["assignments"] == [] and {n: rig.status(n) for n in before} == before
    lines = [e["text"] for e in inc["events"] if e["kind"] == "auto"]
    assert lines.count("Not dispatched (non-emergency event): E60, L31") == 1
    assert [s[0] for s in rig.steps()] == ["accept"]

    rig.at(30)
    rig.change(units=("E60", "L31", "E62"), unit_times={"E60": {"responding": T0 + 25}})
    inc = rig.inc()
    assert inc["assignments"] == [] and rig.status("E60") == before["E60"]
    assert [e["text"] for e in inc["events"] if e["kind"] == "auto"].count(
        "Not dispatched (non-emergency event): E62") == 1

    rig.at(600)
    rig.change(times={"dispatched": T0, "in_service": T0 + 590})                 # the feed's in service still closes it
    assert rig.inc()["status"] == "closed"


def test_a_page_naming_units_on_a_notification_dispatches_none(rig):
    move_drill(rig.cad)
    rig.created(type="DRILL", units=("E60",))
    rig.at(20)
    rig.page(text=DRILL_PAGE)                                                    # tones naming Engine 60 and Ladder 31
    inc = rig.inc()
    assert len(rig.incidents()) == 1 and inc["auto"]["paged_at"] == T0 + 20      # the page joined the feed's event
    assert inc["assignments"] == []
    assert any(e["text"] == "Not dispatched (non-emergency event): E60, L31" for e in inc["events"])
    assert (inc["type"], inc["subtype"]) == ("NOTIF", "DRILL")


def test_a_radio_responding_never_puts_a_unit_on_a_notification(rig):
    move_drill(rig.cad)
    rig.created(type="DRILL", units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    assert rig.on("E62") is None


def test_an_admin_rule_making_drills_emergency_brings_dispatch_back(rig):
    move_drill(rig.cad)
    post(rig.cad, "event_class", action="set", key="*/DRILL", value="emergency")
    rig.created(type="DRILL")
    inc = rig.inc()
    assert (inc["type"], inc["subtype"], inc["emergency"]) == ("NOTIF", "DRILL", True)
    assert {a["name"] for a in inc["assignments"]} == {"E60", "L31"}
