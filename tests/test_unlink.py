"""Taking a transmission off an event (POST /api/cad/incident/<id>/unlink, the ⋯ menu's "Remove from
26-000007"). user, 2026-10-08: "how do i remove things from an inc if it was added". A manual link is
deleted; an auto one (unit, channel, dispatcher) is deleted and remembered, so the auto-linker never puts
it back; a manual add brings it back. Each is a line in the event's chronology and a sentence in the
person's activity; viewers can't."""

import json

import pytest

from sizeup.cad.api import CadApi, needs
from tests.test_radios import Clock, _event_with, cad, tx
from tests.test_roles import COREY, JULIAN, VAL, api, call  # noqa: F401  (fixture)


@pytest.fixture
def c(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    c.clock_ = clock
    return c


def post(c, path, body):
    code, raw, _ = CadApi(c).post(path, json.dumps(body).encode())
    return code, json.loads(raw)


def radio(c, iid):
    return {r["id"]: r["why"] for r in c.full(iid)["radio"]}


def chronology(c, iid):
    return [e["text"] for e in c.full(iid)["events"] if e["kind"] == "radio"]


def test_a_manual_link_removed_is_gone(c):
    a = _event_with(c, ["E63"], c.clock_)
    c.clock_.t += 5
    c.record_traffic(tx("307-m-call_1", c.clock_.t, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "60 Control to Port Chester"))
    assert post(c, f"/api/cad/incident/{a['id']}/link", {"traffic_id": "307-m-call_1", "operator": "Lt. Ruiz"})[1]["linked"]
    assert radio(c, a["id"]) == {"307-m-call_1": "manual"}
    code, r = post(c, f"/api/cad/incident/{a['id']}/unlink", {"traffic_id": "307-m-call_1",
                                                              "operator": "Corey <corey@example.org>"})
    assert code == 200 and r == {"unlinked": True, "why": "manual", "excluded": False, "incident_id": a["id"],
                                 "number": a["number"]}
    assert radio(c, a["id"]) == {}
    assert c.feed.rows()[0]["events"] == []
    assert not c.comms.query(event=a["number"], kind="radio")                       # off the Radio view's filter too
    assert not c.store.q("SELECT * FROM radio_link_exclusions")                     # a manual one: just gone
    lines = chronology(c, a["id"])
    assert lines[0].startswith("Radio traffic added: ") and "60 Ctrl OP 7 on Fire/EMS 15" in lines[0]
    assert lines[1].endswith("60 Ctrl OP 7 on Fire/EMS 15 removed from this event by Corey") and lines[1].startswith("Radio ")
    # again: nothing to remove; bad ones refused
    assert post(c, f"/api/cad/incident/{a['id']}/unlink", {"traffic_id": "307-m-call_1"})[1]["unlinked"] is False
    assert post(c, f"/api/cad/incident/{a['id']}/unlink", {"traffic_id": "nope"})[0] == 400
    assert post(c, f"/api/cad/incident/{a['id']}/unlink", {})[0] == 400
    assert post(c, "/api/cad/incident/9999/unlink", {"traffic_id": "307-m-call_1"})[0] == 400


def test_an_auto_link_removed_stays_removed_until_someone_adds_it(c, tmp_path):
    a = _event_with(c, ["E63", "E60"], c.clock_)
    c.clock_.t += 10
    t0 = c.clock_.t
    assert c.record_traffic(tx("FG1-2", t0, rig="E63", radio_id="6301")) == [a["id"]]            # unit
    assert c.record_traffic(tx("FG1-3", t0 + 1)) == [a["id"]]                                     # channel
    assert c.record_traffic(tx("307-9-call_4-t1", t0 + 2, "Fire/EMS 15", "E60", "E60", 307)) == [a["id"]]   # unit
    assert c.record_traffic(tx("307-9-call_4-t0", t0 + 1, "Fire/EMS 15", "", "60 Ctrl OP 6", 307)) == [a["id"]]
    assert radio(c, a["id"]) == {"FG1-2": "unit", "FG1-3": "channel", "307-9-call_4-t1": "unit", "307-9-call_4-t0": "dispatcher"}
    for tid in ("FG1-2", "FG1-3", "307-9-call_4-t0"):
        code, r = post(c, f"/api/cad/incident/{a['id']}/unlink", {"traffic_id": tid, "operator": "Lt. Ruiz"})
        assert code == 200 and r["unlinked"] and r["excluded"], tid
    assert radio(c, a["id"]) == {"307-9-call_4-t1": "unit"}
    lines = chronology(c, a["id"])
    assert any(x.endswith("E63 on FG1 removed from this event (auto: E63 assigned) by Lt. Ruiz") for x in lines)
    assert any("(auto: FG1, the only call with units)" in x for x in lines)
    assert any("(auto: 60 Control answering E60)" in x for x in lines)
    # the auto-linker sees them again (the transcript comes again, the unit's turn re-recorded): not put back
    assert c.record_traffic(tx("FG1-2", t0, rig="E63", radio_id="6301", text="again")) == []
    assert c.record_traffic(tx("FG1-3", t0 + 1, text="again")) == []
    c.record_traffic(tx("307-9-call_4-t1", t0 + 2, "Fire/EMS 15", "E60", "E60", 307, "again"))
    assert radio(c, a["id"]) == {"307-9-call_4-t1": "unit"}
    # a new transmission from the same rig is its own: linked as always
    assert c.record_traffic(tx("FG1-4", t0 + 5, rig="E63")) == [a["id"]]
    # a person adds one back: a manual link, and the exclusion is gone
    code, r = post(c, f"/api/cad/incident/{a['id']}/link", {"traffic_id": "FG1-2", "operator": "Lt. Ruiz"})
    assert code == 200 and r["linked"] and radio(c, a["id"])["FG1-2"] == "manual"
    assert {r["traffic_id"] for r in c.store.q("SELECT traffic_id FROM radio_link_exclusions")} == {"FG1-3", "307-9-call_4-t0"}
    # survives a restart
    c2 = cad(tmp_path, c.clock_)
    assert c2.record_traffic(tx("FG1-3", t0 + 1, text="once more")) == []
    assert "FG1-3" not in radio(c2, a["id"])


def test_each_row_says_every_event_it_is_on(c):
    a = _event_with(c, ["E63"], c.clock_)
    b = c.create({"type": "STRU", "address": "1 Main St"})
    c.clock_.t += 5
    c.record_traffic(tx("FG1-9", c.clock_.t, rig="E63"))
    post(c, f"/api/cad/incident/{b['id']}/link", {"traffic_id": "FG1-9", "operator": "x"})
    row = next(r for r in c.full(a["id"])["radio"] if r["id"] == "FG1-9")
    assert [(e["id"], e["why"]) for e in row["events"]] == [(a["id"], "unit"), (b["id"], "manual")]
    assert all("status" in e and "number" in e for e in row["events"])


def test_who_may_remove_and_what_their_activity_says(api):  # noqa: F811
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": "209 Westchester Ave"}, JULIAN)
    assert code == 200
    api.cad.record_traffic(tx("307-z-call_1", api.cad.clock(), "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "Port Chester"))
    path = f"/api/cad/incident/{inc['id']}"
    assert needs(f"{path}/unlink") == "dispatch"
    assert call(api, f"{path}/link", {"traffic_id": "307-z-call_1"}, VAL)[0] == 403
    assert call(api, f"{path}/link", {"traffic_id": "307-z-call_1"}, JULIAN)[0] == 200
    assert call(api, f"{path}/unlink", {"traffic_id": "307-z-call_1"}, VAL)[0] == 403
    code, r = call(api, f"{path}/unlink", {"traffic_id": "307-z-call_1", "operator": "someone else"}, JULIAN)
    assert code == 200 and r["unlinked"]
    acts = [(x["email"], x["kind"], x["summary"]) for x in api.cad.activity.rows()]
    assert (JULIAN, "action", f"Removed a transmission from {inc['number']}") in acts
    assert (JULIAN, "action", f"Linked a transmission to {inc['number']}") in acts
    assert any(e == VAL and k == "refused" and "unlink" in s for e, k, s in acts)
    line = [e for e in api.cad.full(inc["id"])["events"] if e["kind"] == "radio"][-1]
    assert line["text"].endswith("removed from this event by Julian") and line["operator"] == "Julian <julian@example.org>"
    assert call(api, f"{path}/unlink", {"traffic_id": "307-z-call_1"}, COREY)[1]["unlinked"] is False
