"""A unit that cleared a call that is still open can go back to it (user, 2026-10-07)."""
import pytest

from sizeup.cad.store import CadError
from tests.test_radios import Clock, _event_with, cad, uid


def test_cleared_unit_goes_back_to_an_open_call(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "L30"], clock)
    e63, l30 = uid(c, "E63"), uid(c, "L30")
    clock.t += 5
    c.unit_status(e63, {"status": "OL"})
    first_on = next(x for x in c.full(a["id"])["assignments"] if x["unit_id"] == e63)["onscene_at"]
    clock.t += 60
    c.unit_status(e63, {"status": "AV"})                         # E63 clears; the alarm is still open
    row = next(x for x in c.full(a["id"])["assignments"] if x["unit_id"] == e63)
    assert row["cleared_at"] and row["return"] == ["RS", "OL", "ET", "IC"]
    clock.t += 30
    c.return_unit(a["id"], {"unit_id": e63, "status": "RS", "operator": "Corey"})
    row = next(x for x in c.full(a["id"])["assignments"] if x["unit_id"] == e63)
    assert row["cleared_at"] is None and row["status"] == "RS" and row["return"] == []
    assert row["onscene_at"] == first_on                                # the first times are kept
    c.unit_status(e63, {"status": "OL"})
    assert any("E63 returned (responding)" in e["text"] for e in c.full(a["id"])["events"])
    # Not while it's on another call, nor out of service, nor once the call is closed.
    c.unit_status(l30, {"status": "AV"})
    b = c.create({"type": "STRU", "address": "1 Main St"})
    c.dispatch(b["id"], {"unit_ids": [l30], "operator": "x"})
    with pytest.raises(CadError, match="clear it there"):
        c.return_unit(a["id"], {"unit_id": l30, "status": "RS"})
    with pytest.raises(CadError):
        c.return_unit(a["id"], {"unit_id": e63, "status": "AV"})   # only responding / on location
    c.unit_status(e63, {"status": "AV"})
    c.close(a["id"], {"disposition": c.lists.disposition_codes[0]})
    with pytest.raises(CadError, match="closed"):
        c.return_unit(a["id"], {"unit_id": e63, "status": "RS"})
    assert all(x["return"] == [] for x in c.full(a["id"])["assignments"])


def test_board_status_puts_the_unit_on_the_chosen_call_and_off_the_old_one(tmp_path):
    # User 2026-10-07: "it should prompt us ... to select the call they are now responding to and
    # remove them from the old call if they were on one".
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63"], clock)
    b = c.create({"type": "STRU", "address": "1 Main St"})
    e63, e64 = uid(c, "E63"), uid(c, "E64")
    clock.t += 5
    # E64 wasn't on anything: responding to b puts it there.
    c.unit_status(e64, {"status": "RS", "incident_id": b["id"], "operator": "Corey"})
    on_b = {x["unit_id"]: x for x in c.full(b["id"])["assignments"] if not x["cleared_at"]}
    assert e64 in on_b and on_b[e64]["status"] == "RS" and on_b[e64]["enroute_at"]
    assert c.full(b["id"])["status"] == "active"
    # E63 moves from a to b: off a (noted there), on b.
    clock.t += 5
    c.unit_status(e63, {"status": "OL", "incident_id": b["id"], "operator": "Corey"})
    assert all(x["cleared_at"] for x in c.full(a["id"])["assignments"] if x["unit_id"] == e63)
    assert any("E63 reassigned" in e["text"] for e in c.full(a["id"])["events"])
    on_b = {x["unit_id"]: x for x in c.full(b["id"])["assignments"] if not x["cleared_at"]}
    assert on_b[e63]["status"] == "OL"
    # "No call: status only" takes it off the call it was on.
    clock.t += 5
    c.unit_status(e64, {"status": "AK", "incident_id": 0})
    assert all(x["cleared_at"] for x in c.full(b["id"])["assignments"] if x["unit_id"] == e64)
    # Free / out-of-service statuses don't ask (and clear as before); a closed call is refused.
    c.unit_status(e63, {"status": "AV", "incident_id": b["id"]})
    assert all(x["cleared_at"] for x in c.full(b["id"])["assignments"] if x["unit_id"] == e63)
    c.close(a["id"], {"disposition": c.lists.disposition_codes[0]})
    with pytest.raises(CadError, match="isn't open"):
        c.unit_status(e63, {"status": "RS", "incident_id": a["id"]})
