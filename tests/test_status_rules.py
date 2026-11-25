"""An incident's status follows its units, never an edit; a late or quiet edit is labelled (the admin, 2026-11-25).

    back to pending   "an active incident with no units assigned should drop back to pending, even after being reopened,
                      and keep its elapsed time": Store.settle_unstaffed (the incident sources' ticker), SETTLE_S after
                      the last unit cleared (whoever cleared it), "Event status: pending (no units)"; created_at, the
                      number and accepted_at never move. A unit put back within SETTLE_S: no flap. In 60 Control mode a
                      call automation still has (the feed lists a unit of ours that was on it, or its close is waiting)
                      stays active until the county settles it (AutoDispatch.holds_active). Reopen: pending.
    quiet edit        "editing an incident should leave its status alone": no edit changes a status; a closed event is
                      edited only quietly (Quiet edit) and stays closed.
    labels            "an update made after the incident, or by a quiet edit, should be marked as such": events.label
                      "after close" / "quiet edit" on the timeline line, in the activity log and the event log CSV; a
                      live line (the close, a reopen, an emergency) never.

Invented calls, places and people only.
"""

from __future__ import annotations

import json

import pytest

from sizeup.cad import export
from sizeup.cad.api import summarize
from sizeup.cad.store import AFTER_CLOSE, QUIET_EDIT, SETTLE_S, UNSTAFFED, CadError
from tests.test_cad import Clock, by_name, local, make_cad, texts, uid
from tests.test_dispatch_modes import ADDR, AutoRig
from tests.test_incident_sources import T0

ADDRESS = "12 Example Ave"


def status(cad, iid):
    return cad.store.incident(iid)["status"]


def lines(cad, iid):
    return [(e["text"], e["label"]) for e in cad.store.full(iid)["events"]]


@pytest.fixture
def cad_clock(tmp_path):
    clock = Clock(local(2026, 10, 20, 14, 0))
    return make_cad(tmp_path, clock=clock), clock


def dispatched(cad, clock, *names):
    iid = cad.create({"type": "ALARM", "subtype": "COMM", "address": ADDRESS, "operator": "JD"})["id"]
    clock.t += 30
    cad.dispatch(iid, {"unit_ids": [uid(cad, n) for n in names], "operator": "JD"})
    return iid


# -- back to pending -------------------------------------------------------------------------------------------------


def test_the_last_unit_cleared_puts_it_back_to_pending_with_its_clock_kept(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60", "L31")
    before = cad.store.incident(iid)
    assert before["status"] == "active"
    clock.t += 600
    cad.unit_status(uid(cad, "E60"), {"status": "AV", "operator": "JD"})
    assert cad.settle_unstaffed() == [] and status(cad, iid) == "active"          # L31 still on it
    cad.unit_status(uid(cad, "L31"), {"status": "AV", "operator": "JD"})
    assert cad.settle_unstaffed() == [] and status(cad, iid) == "active"          # just cleared: not yet (debounce)
    clock.t += SETTLE_S
    assert cad.settle_unstaffed() == [iid]
    after = cad.store.incident(iid)
    assert after["status"] == "pending"
    for k in ("created_at", "accepted_at", "number", "emergency"):                 # received / elapsed clock kept
        assert after[k] == before[k], k
    assert lines(cad, iid)[-1] == (UNSTAFFED, "")
    ev = cad.store.full(iid)["events"][-1]
    assert (ev["kind"], ev["operator"], ev["at"]) == ("status", "automation", clock.t)
    assert cad.settle_unstaffed() == []                                            # said once
    assert [i["id"] for i in cad.store.incidents() if i["status"] == "pending" and i["emergency"]] == [iid]
    # a unit on it again: active as on any call
    clock.t += 60
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert status(cad, iid) == "active" and texts(cad, iid).count("Event status: active") == 2


def test_cleared_and_put_back_within_the_debounce_never_flaps(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    clock.t += 300
    cad.unit_status(uid(cad, "E60"), {"status": "AV", "operator": "JD"})          # a mistaken click ...
    clock.t += SETTLE_S - 1
    assert cad.settle_unstaffed() == []
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})          # ... put right at once
    clock.t += 60
    assert cad.settle_unstaffed() == [] and status(cad, iid) == "active"
    assert UNSTAFFED not in texts(cad, iid)


def test_a_unit_moved_to_another_call_leaves_the_first_pending(cad_clock):
    cad, clock = cad_clock
    a = dispatched(cad, clock, "E60")
    b = cad.create({"type": "MVA", "address": "40 Sample St", "operator": "JD"})["id"]
    clock.t += 60
    cad.dispatch(b, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})          # moved: off a, on b
    clock.t += SETTLE_S
    assert cad.settle_unstaffed() == [a]
    assert (status(cad, a), status(cad, b)) == ("pending", "active")


def test_returned_unit_makes_a_pending_call_active_again(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    clock.t += 300
    cad.unit_status(uid(cad, "E60"), {"status": "AV", "operator": "JD"})
    clock.t += SETTLE_S
    cad.settle_unstaffed()
    assert status(cad, iid) == "pending"
    cad.store.return_to_incident(iid, uid(cad, "E60"), cad.store.return_statuses()[0], "JD")
    assert status(cad, iid) == "active" and texts(cad, iid)[-1] == "Event status: active"


def test_reopened_goes_pending_and_an_old_active_with_no_units_is_settled(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    created = cad.store.incident(iid)["created_at"]
    clock.t += 1200
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    clock.t += 600
    assert cad.reopen(iid, {"operator": "JD"})["status"] == "pending"
    assert cad.store.incident(iid)["created_at"] == created
    # one reopened active before this change (no unit on it): the next sweep makes it pending
    with cad.store.tx() as db:
        db.execute("UPDATE incidents SET status = 'active' WHERE id = ?", (iid,))
    assert cad.settle_unstaffed() == [iid] and status(cad, iid) == "pending"


def test_non_emergency_attached_then_detached_is_open_again_and_never_queued_or_closed(cad_clock):
    cad, clock = cad_clock
    ne = cad.create({"type": "NOTIF", "subtype": "WATER", "address": "3 Sample Ln", "operator": "JD"})["id"]
    cad.attach(ne, {"unit_ids": [uid(cad, "E62")], "operator": "JD"})
    assert status(cad, ne) == "active"
    clock.t += 60
    cad.unit_status(uid(cad, "E62"), {"status": "AV", "operator": "JD"})
    clock.t += SETTLE_S
    done = cad.settle_unstaffed()
    assert done == [ne] and status(cad, ne) == "pending"
    assert texts(cad, ne)[-1] == "Event status: open (no units)"                  # "Open" on the boards
    closed = dispatched(cad, clock, "E61")
    cad.close(closed, {"disposition": "COMPLETE", "operator": "JD"})
    clock.t += 600
    assert cad.settle_unstaffed() == [] and status(cad, closed) == "closed"


def test_a_standby_never_settles(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    cad.unit_status(uid(cad, "E60"), {"status": "AV", "operator": "JD"})
    clock.t += 600
    cad.store.may_write = lambda: False
    assert cad.store.settle_unstaffed() == [] and status(cad, iid) == "active"


def test_the_ticker_runs_the_sweep(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    cad.unit_status(uid(cad, "E60"), {"status": "AV", "operator": "JD"})
    clock.t += SETTLE_S
    cad.sources._settle()
    assert status(cad, iid) == "pending"


# -- automation: 60 Control mode --------------------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


def test_60_control_a_radio_clear_while_the_county_still_lists_it_holds_active(rig):
    rig.created()                                                                  # E60, L31 listed: accepted, dispatched
    iid = rig.iid()
    assert rig.inc()["status"] == "active" and rig.here("E60") and rig.here("L31")
    rig.at(300)
    rig.radio("307-21-call_21-t0", T0 + 290, "Engine 60 back in service")
    rig.radio("307-22-call_22-t0", T0 + 295, "Ladder 31 back in service")
    assert rig.here("E60") is None and rig.here("L31") is None                    # automation cleared them
    rig.at(600)
    assert rig.cad.settle_unstaffed() == [] and rig.inc()["status"] == "active"   # the county still has them: no flap
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})                                    # the county's in service closes it
    assert rig.inc()["status"] == "closed"
    assert UNSTAFFED not in [e["text"] for e in rig.inc()["events"]]
    assert rig.cad.settle_unstaffed() == []


def test_60_control_the_feed_in_service_per_unit_lets_it_go_pending(rig):
    rig.created()
    rig.at(300)
    rig.radio("307-23-call_23-t0", T0 + 290, "Engine 60 back in service")
    rig.radio("307-24-call_24-t0", T0 + 295, "Ladder 31 back in service")
    rig.at(400)
    rig.change(unit_times={"E60": {"in_service": T0 + 380}, "L31": {"in_service": T0 + 385}})
    rig.at(420)
    assert rig.inc()["status"] == "active" and not rig.cad.auto.holds_active(rig.iid())   # no longer the county's
    assert rig.cad.settle_unstaffed() == [rig.iid()] and rig.inc()["status"] == "pending"


def test_60_control_a_person_clearing_the_units_lets_it_go_pending(rig):
    rig.created()
    rig.at(120)
    for n in ("E60", "L31"):
        rig.cad.unit_status(rig.uid(n), {"status": "AV", "operator": "Lt. Test"})  # a person's: not the county's now
    rig.at(120 + SETTLE_S)
    assert rig.cad.settle_unstaffed() == [rig.iid()] and rig.inc()["status"] == "pending"
    rig.at(200)
    rig.change(unit_times={"E60": {"responding": T0 + 190}})                       # nothing put back by the feed
    assert rig.here("E60") is None and rig.inc()["status"] == "pending"


def test_60_control_a_close_waiting_holds_it_active(rig):
    rig.created()
    rig.put_on(rig.iid(), "E62")                                                   # a person's unit keeps it open
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})
    assert rig.inc()["status"] == "active" and rig.step("close?")
    with rig.cad.store.tx() as db:                                                 # (E62 gone without a recheck)
        db.execute("UPDATE assignments SET cleared_at = ? WHERE cleared_at IS NULL", (rig.clock.t,))
    rig.at(1000)
    assert rig.cad.auto.holds_active(rig.iid()) is True
    assert rig.cad.settle_unstaffed() == [] and rig.inc()["status"] == "active"


def test_60_control_a_call_the_county_stopped_listing_is_not_held(rig):
    rig.created()
    rig.at(300)
    rig.radio("307-25-call_25-t0", T0 + 290, "Engine 60 back in service")
    rig.radio("307-26-call_26-t0", T0 + 295, "Ladder 31 back in service")
    rig.at(600)
    assert rig.cad.auto.holds_active(rig.iid()) is True
    with rig.cad.store.tx() as db:                                                 # the feed's snapshot dropped it
        db.execute("UPDATE source_reports SET gone_at = ? WHERE incident_id = ?", (rig.clock.t, rig.iid()))
    assert rig.cad.settle_unstaffed() == [rig.iid()] and rig.inc()["status"] == "pending"


def test_pcfd_mode_settles_with_no_hold(rig):
    rig.mode("pcfd")
    rig.created()
    rig.cad.accept(rig.iid(), {"operator": "Lt. Test"})
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.at(200)
    rig.cad.unit_status(rig.uid("E60"), {"status": "AV", "operator": "Lt. Test"})
    rig.at(200 + SETTLE_S)
    assert rig.cad.auto.holds_active(rig.iid()) is False
    assert rig.cad.settle_unstaffed() == [rig.iid()]


def test_undoing_an_automatic_close_puts_units_back_and_makes_it_active(rig):
    rig.created()
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})
    assert rig.inc()["status"] == "closed"
    c = rig.step("close")
    rig.cad.auto_undo(rig.iid(), {"step": c["id"], "operator": "Lt. Test"})
    assert rig.here("E60") and rig.here("L31") and rig.inc()["status"] == "active"


# -- quiet edit: never a status --------------------------------------------------------------------------------------


@pytest.mark.parametrize("quiet", [False, True])
def test_no_edit_changes_a_status(cad_clock, quiet):
    cad, clock = cad_clock
    pending = cad.create({"type": "MVA", "address": "40 Sample St", "operator": "JD"})["id"]
    active = dispatched(cad, clock, "E60")
    queued = cad.store.create_incident(type="ALARM", subtype="COMM", address="7 Test Pl", source="60 Control page",
                                       status="queued")
    for iid, was in ((pending, "pending"), (active, "active"), (queued, "queued")):
        body = {"type": "ALARM", "subtype": "RESD", "address": "9 Example Ave", "cross_street": "Sample St / Test Pl",
                "caller_name": "Pat Example", "remarks": "edited", "map_layers": {"cameras": True}, "operator": "JD"}
        if quiet:
            body["quiet"] = True
        out = cad.update(iid, body)
        assert out["status"] == was and status(cad, iid) == was, iid
        assert out["number"] == (None if was == "queued" else cad.store.incident(iid)["number"])
        last = cad.store.full(iid)["events"][-1]
        assert last["kind"] == "edit" and last["label"] == (QUIET_EDIT if quiet else "")


def test_a_closed_event_is_edited_only_quietly_and_stays_closed(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    clock.t += 1200
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    closed = cad.store.incident(iid)
    with pytest.raises(CadError, match="event is closed: reopen it, or Quiet edit"):
        cad.update(iid, {"cross_street": "Sample St", "operator": "JD"})
    clock.t += 3600
    out = cad.update(iid, {"type": "ALARM", "subtype": "RESD", "cross_street": "Sample St", "quiet": True,
                           "operator": "Robin Example <robin@example.org>"})
    assert (out["status"], out["closed_at"], out["disposition"]) == ("closed", closed["closed_at"], "COMPLETE")
    assert (out["subtype"], out["cross_street"]) == ("RESD", "Sample St")
    ev = cad.store.full(iid)["events"][-1]
    assert (ev["kind"], ev["label"], ev["operator"], ev["at"]) == ("edit", AFTER_CLOSE, "Robin Example <robin@example.org>",
                                                                    clock.t)
    assert ev["text"].startswith("Edited type: ")
    # a pin moved alone is still said (it isn't live)
    cad.update(iid, {"lat": 41.0, "lon": -73.66, "quiet": True, "operator": "JD"})
    assert lines(cad, iid)[-1] == ("Edited pin", AFTER_CLOSE)
    # a dismissed page: not at all
    page = cad.store.create_incident(type="ALARM", subtype="COMM", source="60 Control page", status="queued")
    cad.dismiss(page, {"operator": "JD"})
    with pytest.raises(CadError, match="event is dismissed"):
        cad.update(page, {"cross_street": "x", "quiet": True})


def test_quiet_must_be_a_boolean(cad_clock):
    cad, _ = cad_clock
    iid = cad.create({"type": "MVA", "operator": "JD"})["id"]
    with pytest.raises(CadError, match="quiet: true or false"):
        cad.update(iid, {"quiet": "yes"})


# -- labels ----------------------------------------------------------------------------------------------------------


def test_lines_after_close_are_labelled_and_the_live_ones_are_not(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    cad.note(iid, {"text": "on the way", "operator": "JD"})
    clock.t += 1200
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    clock.t += 300
    cad.note(iid, {"text": "caller called back: all fine", "operator": "JD"})
    clock.t += 300
    cad.reopen(iid, {"operator": "JD"})
    cad.note(iid, {"text": "back on it", "operator": "JD"})
    got = {t: label for t, label in lines(cad, iid)}
    assert got["on the way"] == "" and got["back on it"] == ""
    assert got["caller called back: all fine"] == AFTER_CLOSE
    assert all(label == "" for t, label in lines(cad, iid) if t.startswith(("Event closed", "Reopened")))


def test_labels_in_the_activity_log_and_the_event_log(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    n = cad.store.incident(iid)["number"]
    path = f"/api/cad/incident/{iid}"
    live = {"cross_street": "Example Ave", "operator": "JD"}
    assert summarize(path, live, cad.update(iid, live), cad) == (f"Edited {n}: cross_street", n)
    quiet = {"cross_street": "Test Pl", "quiet": True, "operator": "JD"}
    assert summarize(path, quiet, cad.update(iid, quiet), cad) == (f"Quiet edit {n}: cross_street", n)
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    late = {"cross_street": "Sample St", "quiet": True, "operator": "JD"}
    assert summarize(path, late, cad.update(iid, late), cad) == (f"Quiet edit (after close) {n}: cross_street", n)
    assert status(cad, iid) == "closed"
    csv = export.event_log_csv(cad.store).decode()
    assert "Edited cross street: Example Ave," in csv
    assert "Edited cross street: Test Pl (quiet edit)" in csv and "Edited cross street: Sample St (after close)" in csv


def test_a_late_edit_dispatches_nothing_and_puts_no_unit_back(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    n_disp, voices = len(cad.store.full(iid)["dispatches"]), len(cad.store.pending_voices())
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    cad.update(iid, {"type": "STRU", "subtype": "", "quiet": True, "operator": "JD"})
    assert len(cad.store.full(iid)["dispatches"]) == n_disp and len(cad.store.pending_voices()) == voices
    assert by_name(cad, "E60")["incident_id"] is None and status(cad, iid) == "closed"


def test_a_note_after_close_is_marked_in_the_radio_view(cad_clock):
    cad, clock = cad_clock
    iid = dispatched(cad, clock, "E60")
    cad.note(iid, {"text": "E60 on location", "units": ["E60"], "operator": "JD"})
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    cad.note(iid, {"text": "E60 left a card with the owner", "units": ["E60"], "operator": "JD"})
    notes = {r["text"]: r["label"] for r in cad.comms._notes("E60", "", None, None, None, 50)}
    assert notes == {"E60 on location": "", "E60 left a card with the owner": AFTER_CLOSE}


def test_an_older_file_gets_the_label_column_its_lines_live(tmp_path):
    cad = make_cad(tmp_path)
    iid = cad.create({"type": "MVA", "operator": "JD"})["id"]
    with cad.store.tx() as db:
        db.execute("ALTER TABLE events DROP COLUMN label")                       # as a file from before
    cad.store.shutdown()
    again = make_cad(tmp_path)
    assert {e["label"] for e in again.store.full(iid)["events"]} == {""}
