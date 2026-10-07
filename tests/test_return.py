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
    c.unit_status(e63, {"status": "ON LOCATION"})
    first_on = next(x for x in c.full(a["id"])["assignments"] if x["unit_id"] == e63)["onscene_at"]
    clock.t += 60
    c.unit_status(e63, {"status": "AVAILABLE"})                         # E63 clears; the alarm is still open
    row = next(x for x in c.full(a["id"])["assignments"] if x["unit_id"] == e63)
    assert row["cleared_at"] and row["return"] == ["RESPONDING", "ON LOCATION"]
    clock.t += 30
    c.return_unit(a["id"], {"unit_id": e63, "status": "RESPONDING", "operator": "Corey"})
    row = next(x for x in c.full(a["id"])["assignments"] if x["unit_id"] == e63)
    assert row["cleared_at"] is None and row["status"] == "RESPONDING" and row["return"] == []
    assert row["onscene_at"] == first_on                                # the first times are kept
    c.unit_status(e63, {"status": "ON LOCATION"})
    assert any("E63 returned (responding)" in e["text"] for e in c.full(a["id"])["events"])
    # Not while it's on another call, nor out of service, nor once the call is closed.
    c.unit_status(l30, {"status": "AVAILABLE"})
    b = c.create({"type": "STRU", "address": "1 Main St"})
    c.dispatch(b["id"], {"unit_ids": [l30], "operator": "x"})
    with pytest.raises(CadError, match="is on"):
        c.return_unit(a["id"], {"unit_id": l30, "status": "RESPONDING"})
    with pytest.raises(CadError):
        c.return_unit(a["id"], {"unit_id": e63, "status": "AVAILABLE"})   # only responding / on location
    c.unit_status(e63, {"status": "AVAILABLE"})
    c.close(a["id"], {"disposition": c.lists.disposition_codes[0]})
    with pytest.raises(CadError, match="closed"):
        c.return_unit(a["id"], {"unit_id": e63, "status": "RESPONDING"})
    assert all(x["return"] == [] for x in c.full(a["id"])["assignments"])
