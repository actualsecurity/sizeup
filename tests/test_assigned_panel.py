"""The event's Assigned panel (user, 2026-10-08).

Add unit: a unit heard on the air responding (a chief, an engine) that nobody toned out goes on the event as
Responding, or On location ("we need to 'add someone responding' even if they were not on the original
dispatch"). POST incident/<id>/attach {unit_ids, status}: no dispatch logged, no Disp time, nothing said.

Incident command: one unit per event ("there also can only be one inc commander, so the option should minus
from others if its set on someone new"); whoever held it goes back to On location."""
import threading

import pytest

from sizeup.cad.api import needs
from sizeup.cad.store import CadError
from tests.test_radios import Clock, _event_with, cad, uid
from tests.test_roles import COREY, JULIAN, VAL, api, call  # noqa: F401  (fixture)


def row(c, iid, name):
    return next(a for a in c.full(iid)["assignments"] if a["name"] == name and not a["cleared_at"])


def unit(c, name):
    return next(u for u in c.state()["units"] if u["name"] == name)


@pytest.fixture
def quiet(tmp_path, monkeypatch):
    """A CAD whose voice and transmitter fail the test if anything tries to say a word."""
    clock = Clock()
    c = cad(tmp_path, clock)
    c.clock_ = clock

    def no(*a, **k):
        raise AssertionError("nothing is rendered or keyed for an added unit")
    inc = _event_with(c, ["E60"], clock)                  # dispatched first, with the voice
    monkeypatch.setattr(c, "_render", no)
    monkeypatch.setattr(c, "_send", no)
    monkeypatch.setattr(c.transmitter, "transmit", no)
    c.inc = inc
    return c


def test_a_chief_added_responding_to_an_emergency_event(quiet):
    c, iid = quiet, quiet.inc["id"]
    before = len(c.full(iid)["dispatches"])
    c.clock_.t += 90
    r = c.attach(iid, {"unit_ids": [uid(c, "2395")], "status": "RS", "operator": "Lt. Ruiz"})
    assert r == {"ok": True, "moved": [], "status": "RS"}
    a = row(c, iid, "2395")
    assert a["status"] == "RS" and a["enroute_at"] == c.clock_.t
    assert a["dispatched_at"] is None and a["onscene_at"] is None              # no Disp: there was no dispatch
    assert a["next"] == ["OL", "ST", "AV", "CE", "OS"]                          # the usual buttons
    ev = c.full(iid)["events"][-1]
    assert (ev["kind"], ev["text"], ev["operator"]) == ("status", "2395 added responding (not dispatched)", "Lt. Ruiz")
    assert len(c.full(iid)["dispatches"]) == before                            # nothing in the dispatch log
    assert c.store.one("SELECT new_status, incident_id FROM unit_history ORDER BY id DESC LIMIT 1") == {
        "new_status": "RS", "incident_id": iid}
    # the board and the Overview see it committed to the event, like a dispatched unit
    u = unit(c, "2395")
    assert (u["status"], u["incident_id"], u["incident_number"]) == ("RS", iid, c.inc["number"])
    card = next(i for i in c.state()["overview"] if i["id"] == iid)
    assert {"name": "2395", "status": "RS", "kind": "chief"} in card["units"]
    # the normal buttons from here: on location stamps OnLoc
    c.clock_.t += 60
    c.unit_status(uid(c, "2395"), {"status": "OL", "operator": "Lt. Ruiz"})
    assert row(c, iid, "2395")["onscene_at"] == c.clock_.t


def test_added_on_location_and_before_any_dispatch(tmp_path, monkeypatch):
    clock = Clock()
    c = cad(tmp_path, clock)
    monkeypatch.setattr(c, "_render", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no voice")))
    iid = c.create({"type": "STRU", "address": "209 Westchester Ave"})["id"]
    assert c.store.incident(iid)["status"] == "pending"
    c.attach(iid, {"unit_ids": [uid(c, "E63")], "status": "OL", "operator": "x"})
    a = row(c, iid, "E63")
    assert (a["status"], a["dispatched_at"], a["enroute_at"], a["onscene_at"]) == ("OL", None, None, clock.t)
    inc = c.full(iid)
    assert inc["status"] == "active" and inc["dispatches"] == []
    assert inc["events"][-2]["text"] == "E63 added on location (not dispatched)"
    # a non-emergency event takes one too
    ne = c.create({"type": "NOTIF", "address": "10 Grove St"})["id"]
    c.attach(ne, {"unit_ids": [uid(c, "2393")], "status": "RS", "operator": "x"})
    assert row(c, ne, "2393")["status"] == "RS"


def test_already_assigned_out_of_service_and_bad_requests(quiet):
    c, iid = quiet, quiet.inc["id"]
    e60, e61, l31 = uid(c, "E60"), uid(c, "E61"), uid(c, "L31")
    with pytest.raises(CadError, match="Already on this event: E60"):
        c.attach(iid, {"unit_ids": [e60], "status": "RS", "operator": "x"})
    c.unit_status(e61, {"status": "OS", "note": "pump"})
    with pytest.raises(CadError, match="Not available: E61"):
        c.attach(iid, {"unit_ids": [e61], "status": "RS", "operator": "x"})
    for bad in ("DP", "AV", "OS", "nope", ""):
        with pytest.raises(CadError, match="status: RS"):
            c.attach(iid, {"unit_ids": [l31], "status": bad, "operator": "x"})
    with pytest.raises(CadError, match="operator"):
        c.attach(iid, {"unit_ids": [l31], "status": "RS"})
    with pytest.raises(CadError, match="Another agency"):
        c.attach(iid, {"unit_ids": [uid(c, "77A4")], "status": "RS", "operator": "x"})
    q = c.store.create_incident(source="60 Control page", status="queued")
    with pytest.raises(CadError, match="Accept first"):
        c.attach(q, {"unit_ids": [l31], "status": "RS", "operator": "x"})
    assert [a["name"] for a in c.full(iid)["assignments"]] == ["E60"]          # nothing half-done
    # on another call: moved off it, noted on both, and the answer says so (as a dispatch does)
    other = c.create({"type": "STRU", "address": "1 Main St"})["id"]
    c.attach(other, {"unit_ids": [l31], "status": "RS", "operator": "x"})
    n = c.store.incident(other)["number"]
    r = c.attach(iid, {"unit_ids": [l31], "status": "RS", "operator": "x"})
    assert r["moved"] == [f"L31 (from {n})"]
    assert all(a["cleared_at"] for a in c.full(other)["assignments"] if a["name"] == "L31")
    assert c.full(other)["events"][-1]["text"] == "L31 reassigned to another event"
    assert unit(c, "L31")["incident_id"] == iid
    # closed: refused
    c.close(other, {"disposition": c.lists.disposition_codes[0]})
    with pytest.raises(CadError, match="closed"):
        c.attach(other, {"unit_ids": [uid(c, "E62")], "status": "RS", "operator": "x"})


def test_permissions_and_activity(api):  # noqa: F811
    assert needs("/api/cad/incident/7/attach", {"status": "RS"}) == "dispatch"
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": "209 Westchester Ave"}, JULIAN)
    iid, chief = inc["id"], uid(api.cad, "2395")
    path, body = f"/api/cad/incident/{iid}/attach", {"unit_ids": [chief], "status": "RS"}
    assert call(api, path, body, VAL) == (403, {"error": "Dispatchers and admins only (you: viewer)"})
    assert api.cad.store.unit(chief)["incident_id"] is None
    code, out = call(api, path, body, JULIAN)
    assert code == 200 and out["status"] == "RS"
    ev = next(e for e in api.cad.full(iid)["events"] if "2395" in e["text"])
    assert (ev["text"], ev["operator"]) == ("2395 added responding (not dispatched)", "Julian <julian@example.org>")
    acts = api.cad.activity.rows(JULIAN, "action")
    assert acts[0]["summary"] == f"Added 2395 to {inc['number']} responding (not dispatched)"
    assert call(api, path, body, COREY) == (400, {"error": "Already on this event: 2395"})


# -- incident command: one per event --------------------------------------------------------------------------

def ic(c, iid):
    return [a["name"] for a in c.full(iid)["assignments"] if not a["cleared_at"] and a["status"] == "IC"]


def test_command_transfers_and_the_old_commander_goes_back_on_location(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "L31", "E60", "2392"], clock)["id"]
    b = _event_with(c, ["E61"], clock)["id"]
    for n in ("E63", "L31", "E60", "2392", "E61"):
        c.unit_status(uid(c, n), {"status": "OL"})
    c.unit_status(uid(c, "E61"), {"status": "IC"})                          # command on another event
    clock.t += 10
    out = c.unit_status(uid(c, "2392"), {"status": "IC", "operator": "Lt. Ruiz"})
    assert "command_from" not in out
    assert c.full(a)["events"][-1]["text"] == "Command: 2392"
    n = len(c.full(a)["events"])
    clock.t += 10
    out = c.unit_status(uid(c, "E63"), {"status": "IC", "operator": "Lt. Ruiz"})
    assert out["command_from"] == ["2392"]
    assert ic(c, a) == ["E63"] and row(c, a, "2392")["status"] == "OL"
    ev = c.full(a)["events"][n:]
    assert [(e["text"], e["operator"]) for e in ev] == [("Command: E63 (from 2392)", "Lt. Ruiz")]   # one line
    assert c.store.one("SELECT old_status, new_status, note FROM unit_history WHERE unit_id = ? ORDER BY id DESC LIMIT 1",
                       (uid(c, "2392"),)) == {"old_status": "IC", "new_status": "OL", "note": "command to E63"}
    assert ic(c, b) == ["E61"]                                                # the other event keeps its own
    # 2392 still has the button to take it back
    assert "IC" in row(c, a, "2392")["next"]
    c.unit_status(uid(c, "2392"), {"status": "IC"})
    assert ic(c, a) == ["2392"] and c.full(a)["events"][-1]["text"] == "Command: 2392 (from E63)"
    # added or returned straight into command: the same rule
    c.attach(a, {"unit_ids": [uid(c, "2395")], "status": "IC", "operator": "x"})
    assert ic(c, a) == ["2395"] and c.full(a)["events"][-1]["text"] == "Command: 2395 (from 2392)"
    c.unit_status(uid(c, "L31"), {"status": "AV"})
    c.return_unit(a, {"unit_id": uid(c, "L31"), "status": "IC"})
    assert ic(c, a) == ["L31"] and ic(c, b) == ["E61"]
    # a status with no event (the board, no call) is just a status
    c.unit_status(uid(c, "E62"), {"status": "IC"})
    assert ic(c, a) == ["L31"]


def test_one_commander_after_rapid_and_concurrent_changes(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    names = ["E63", "L31", "E60", "2392", "2393", "2395"]
    a = _event_with(c, names, clock)["id"]
    for n in names:
        c.unit_status(uid(c, n), {"status": "OL"})
    for n in names * 3:
        c.unit_status(uid(c, n), {"status": "IC"})
        assert ic(c, a) == [n]
    go = threading.Barrier(len(names))

    def take(n):
        go.wait()
        c.unit_status(uid(c, n), {"status": "IC"})
    ts = [threading.Thread(target=take, args=(n,)) for n in names]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(ic(c, a)) == 1
    assert [u["name"] for u in c.state()["units"] if u["status"] == "IC"] == ic(c, a)     # the board too


def test_command_in_the_activity(api):  # noqa: F811
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": "209 Westchester Ave"}, JULIAN)
    iid, e63, chief = inc["id"], uid(api.cad, "E63"), uid(api.cad, "2392")
    assert call(api, f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [e63, chief]}, JULIAN)[0] == 200
    for u in (chief, e63):
        assert call(api, f"/api/cad/unit/{u}/status", {"status": "IC"}, JULIAN)[0] == 200
    assert api.cad.activity.rows(JULIAN, "action")[0]["summary"] == f"Set E63 to IC on {inc['number']}; command from 2392"
