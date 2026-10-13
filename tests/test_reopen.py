"""Reopen a closed or dismissed event, and the queue's Closed 12h folded to start (#89).

user, 2026-10-13: "closed should be minimized to start, and we might need to 'reopen' old incidents too"
"""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi, needs
from sizeup.cad.store import TZ, CadError
from sizeup.cloud.access import Identity
from tests.test_cad import Clock, by_name, local, make_cad, texts, uid
from tests.test_roles import COREY, JULIAN, VAL, api, call  # noqa: F401  (fixtures)

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"


def closed_with_units(cad, clock):
    iid = cad.create({"type": "MVA", "address": "Boston Post Rd", "operator": "JD"})["id"]
    e60 = uid(cad, "E60")
    cad.dispatch(iid, {"unit_ids": [e60], "operator": "JD"})
    clock.t += 60
    cad.unit_status(e60, {"status": "OL", "operator": "JD"})
    clock.t += 1800
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    return iid


def test_closed_with_units_reopens_active_disposition_into_the_chronology(tmp_path):
    clock = Clock(local(2026, 10, 13, 16, 0))
    cad = make_cad(tmp_path, clock=clock)
    iid = closed_with_units(cad, clock)
    before = cad.store.full(iid)
    assert (before["status"], before["disposition"]) == ("closed", "COMPLETE") and before["closed_at"] == clock.t
    clock.t += 600                                              # 16:41, closed at 16:31
    inc = cad.reopen(iid, {"operator": "Julian <julian@example.org>"})
    assert (inc["status"], inc["disposition"], inc["closed_at"], inc["number"]) == ("active", "", None, before["number"])
    full = cad.store.full(iid)
    assert full["events"][-1]["kind"] == "reopened" and full["events"][-1]["operator"] == "Julian <julian@example.org>"
    assert texts(cad, iid)[-1] == "Reopened by Julian; was Complete at 16:31"
    # no unit comes back by itself: E60 stays cleared and available; the dispatcher adds units again
    assert all(a["cleared_at"] for a in full["assignments"]) and by_name(cad, "E60")["incident_id"] is None
    assert [i["id"] for i in cad.store.incidents() if i["status"] == "active"] == [iid]
    cad.attach(iid, {"unit_ids": [uid(cad, "E60")], "status": "OL", "operator": "JD"})
    assert by_name(cad, "E60")["incident_id"] == iid
    # and it closes again, with a new disposition
    cad.close(iid, {"disposition": "RESCUE", "operator": "JD"})
    assert cad.store.incident(iid)["disposition"] == "RESCUE"
    assert "Event closed: Rescue; cleared E60" in texts(cad, iid)


def test_closed_without_units_reopens_pending(tmp_path):
    clock = Clock(local(2026, 10, 13, 9, 0))
    cad = make_cad(tmp_path, clock=clock)
    iid = cad.create({"type": "ALARM", "subtype": "CO-RESD", "address": "1 Main St", "operator": "JD"})["id"]
    cad.close(iid, {"disposition": "CANCELLED", "operator": "JD"})
    clock.t += 86400 * 2                                        # two days on: the day is said
    inc = cad.reopen(iid, {"operator": "JD"})
    assert (inc["status"], inc["emergency"], inc["disposition"], inc["closed_at"]) == ("pending", 1, "", None)
    assert texts(cad, iid)[-1] == "Reopened by JD; was Cancelled at 10/13 09:00"
    assert cad.recommend(iid, {})["units"]                      # dispatchable again


def test_dismissed_page_reopens_to_the_60_control_queue(tmp_path):
    clock = Clock(local(2026, 10, 13, 12, 0))
    cad = make_cad(tmp_path, clock=clock)
    iid = cad.store.create_incident(type="ALARM", address="209 Westchester Ave", source="60 Control page",
                                    source_ref="monitor:x", status="queued")
    cad.dismiss(iid, {"operator": "JD", "reason": "Rye's call"})
    assert cad.store.incident(iid)["status"] == "dismissed"
    clock.t += 300
    inc = cad.reopen(iid, {"operator": "JD"})
    assert (inc["status"], inc["number"], inc["closed_at"]) == ("queued", None, None)
    assert texts(cad, iid)[-2:] == ["Dismissed: Rye's call", "Reopened by JD; was dismissed at 12:00"]
    assert [i["id"] for i in cad.store.incidents() if i["status"] == "queued"] == [iid]
    acc = cad.accept(iid, {"operator": "JD"})                   # waiting to accept, as before
    assert acc["status"] == "pending" and re.fullmatch(r"\d{2}-000001", acc["number"])


def test_closed_non_emergency_reopens_open(tmp_path):
    clock = Clock(local(2026, 10, 13, 12, 0))
    cad = make_cad(tmp_path, clock=clock)
    iid = cad.create({"type": "NOTIF", "subtype": "WATER", "address": "10 Grove St", "operator": "JD"})["id"]
    cad.attach(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})     # units on it, still no dispatch
    cad.close(iid, {"disposition": "NOTIF MADE", "operator": "JD"})
    inc = cad.reopen(iid, {})
    assert (inc["status"], inc["emergency"], inc["disposition"]) == ("pending", 0, "")
    assert texts(cad, iid)[-1] == "Reopened; was Notification made at 12:00"
    assert [i["id"] for i in cad.store.incidents() if i["status"] in ("pending", "active") and not i["emergency"]] == [iid]


def test_an_open_event_cant_be_reopened(tmp_path):
    cad = make_cad(tmp_path)
    iid = cad.create({"type": "MVA", "operator": "JD"})["id"]
    with pytest.raises(CadError, match="Event pending: not closed"):
        cad.reopen(iid, {"operator": "JD"})
    a = CadApi(cad)
    code, raw, _ = a.post(f"/api/cad/incident/{iid}/reopen", json.dumps({"operator": "JD"}).encode())
    assert (code, json.loads(raw)) == (400, {"error": "Event pending: not closed"})
    assert a.post("/api/cad/incident/999/reopen", b"{}")[0] == 400


def test_reopen_is_a_dispatcher_write_audited_and_refused_to_a_viewer(api):  # noqa: F811
    assert needs("/api/cad/incident/7/reopen") == "dispatch"
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": "209 Westchester Ave"}, JULIAN)
    iid, n = inc["id"], inc["number"]
    assert call(api, f"/api/cad/incident/{iid}/reopen", {}, JULIAN) == (400, {"error": "Event pending: not closed"})
    assert call(api, f"/api/cad/incident/{iid}/close", {"disposition": "COMPLETE"}, JULIAN)[0] == 200
    assert call(api, f"/api/cad/incident/{iid}/reopen", {}, VAL) == (403, {"error": "Dispatchers and admins only (you: viewer)"})
    closed = api.cad.store.incident(iid)
    assert closed["status"] == "closed"
    code, out = call(api, f"/api/cad/incident/{iid}/reopen", {}, JULIAN)
    assert code == 200 and out["status"] == "pending" and out["number"] == n
    assert api.cad.store.q("SELECT operator FROM events WHERE incident_id = ? AND kind = 'reopened'", (iid,)) \
        == [{"operator": "Julian <julian@example.org>"}]            # the signed-in person, whatever the page sent
    assert texts(api.cad, iid)[-1] == f"Reopened by Julian; was Complete at {datetime.fromtimestamp(closed['closed_at'], TZ):%H:%M}"
    rows =[(r["email"], r["kind"], r["summary"], r["incident_number"]) for r in api.cad.activity.rows()]
    assert (VAL, "refused", f"Refused: incident/{iid}/reopen (needs dispatch; viewer)", n) in rows
    assert (JULIAN, "action", f"Reopened {n}: pending", n) in rows
    # an admin may too, and the activity row for a page without a number says so
    assert call(api, f"/api/cad/incident/{iid}/close", {"disposition": "COMPLETE"}, COREY)[0] == 200
    assert call(api, f"/api/cad/incident/{iid}/reopen", {}, COREY)[0] == 200
    pid = api.cad.store.create_incident(type="ALARM", source="60 Control page", source_ref="monitor:p", status="queued")
    assert call(api, f"/api/cad/incident/{pid}/dismiss", {}, JULIAN)[0] == 200
    assert call(api, f"/api/cad/incident/{pid}/reopen", {}, JULIAN)[1]["status"] == "queued"
    assert api.cad.activity.rows()[0]["summary"] == "Reopened a 60 Control page: queued"


def test_the_page_folds_closed_12h_with_its_count_and_offers_reopen():
    html = (STATIC / "cad.html").read_text()
    js = (STATIC / "cad.js").read_text()
    css = (STATIC / "cad.css").read_text()
    # the section starts folded: a real button, aria-expanded, the list hidden, the count next to the name
    sec = re.search(r'<section id="q-closed-sec"[^>]*>(.*?)</section>', html, re.S).group(1)
    assert re.search(r'<button id="q-closed-toggle" class="sec-toggle" type="button" aria-expanded="false" aria-controls="q-closed"', sec)
    assert 'Closed 12h</span><span id="q-closed-n"' in sec and '<ol id="q-closed" class="list q closed" hidden>' in sec
    assert ':is(.other-section, .q-fold).open .sec-toggle .caret' in css
    # the choice is remembered per browser, through the try/catch store() like the operator name
    assert 'store("sizeup.cad.closedOpen")' in js and 'store("sizeup.cad.closedOpen", open ? "1" : "0")' in js
    assert '$("#q-closed-toggle").addEventListener("click"' in js and 'cad.closedOpen = closedFold();' in js
    assert 'tog.setAttribute("aria-expanded", String(cad.closedOpen))' in js and "`· ${closed.length}`" in js
    # Reopen: in the closed event's header, the closed row's "⋯" (the queue and Records), locked for a viewer
    assert '<button id="d-reopen" hidden' in html and 'show($("#d-reopen"), !open)' in js
    assert '$("#d-reopen").addEventListener("click"' in js and "#d-reopen" in re.search(r"const RO_DISPATCH = (.*?);", js, re.S).group(1)
    assert 'label: "Reopen"' in js and "/api/cad/incident/${iid}/reopen" in js
    assert js.count("closedMenuItems(") == 3 and "UI.menu.button(`Actions: ${i.number}`, () => closedMenuItems(i)" in js
    assert "reopened" in re.search(r"const EVENT_KINDS = \{(.*?)\}", js).group(1)
    # the wall board shows no queue (nothing to fold) and no menus
    assert "html.board :is(.queue," in css and "html.board :is(.feed-more, .menu-btn) { display: none; }" in css
