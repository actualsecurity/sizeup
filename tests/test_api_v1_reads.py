"""The read resources of /api/v1 (#560): snapshot, incidents, one incident, units, alarms. A real Cad seeded with invented
calls (Example Ave, the units of data/cad_lists.json), a test resolver for the device principal, the `reads` flag
turned on through injected Switches. Everything is invented; no real address, caller or radio ID."""
from __future__ import annotations

import hashlib
import itertools
import json
import time

import pytest
from jsonschema import Draft202012Validator

from sizeup.api.v1 import paging
from sizeup.api.v1.handler import V1
from sizeup.api.v1.limits import Limits
from sizeup.api.v1.principal import Principal
from sizeup.api.v1.rev import rev_of
from sizeup.api.v1.serialize import name_of, radio_rows
from sizeup.api.v1.switches import Switches
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from tests import openapi_spec as oas
from tests.test_address_history import fake_geocode
from tests.test_alarms import tx
from tests.test_api_v1 import serve
from tests.test_cad import make_cad, uid
from tests.test_roles import CASEY, JORDAN, VAL, people
from tests.test_settings import post

DOC = oas.spec()
READS = ("/api/v1/snapshot", "/api/v1/incidents", "/api/v1/incidents/1", "/api/v1/units", "/api/v1/alarms")
LAT, LON = 41.0, -73.66
TICK = itertools.count(0, 1)          # the rate limiter's clock: a second a request, so a test is never limited
TOKENS = {"Bearer tok-casey": CASEY, "Bearer tok-jordan": JORDAN, "Bearer tok-val": VAL}


def resolver(headers):
    email = TOKENS.get(headers.get("Authorization") or "")
    return Principal("dev-" + email.split("@")[0], email) if email else None


def hdr(who="val"):
    return {"Authorization": f"Bearer tok-{who}"}


def reads_on(**flags):
    return Switches(lambda: True, lambda: {"reads": True, **flags})


class Clock:
    def __init__(self, t=1_767_225_600.0):
        self.t = t

    def __call__(self):
        return self.t


def seed(tmp_path, calls=3, clock=None):
    """A CAD with Casey (admin), Jordan (dispatcher), Val (viewer), and `calls` incidents on Example Ave, the first one
    dispatched with two units."""
    c = make_cad(tmp_path, geocoder=fake_geocode, clock=clock or Clock())
    c.logged = []
    c.log = c.logged.append
    people(c)
    ids = []
    for n in range(calls):
        c.store.clock = c.clock
        inc = c.create({"type": "STRU", "address": f"{100 + 2 * n} Example Ave", "lat": LAT + (100 + 2 * n) * 0.00009,
                        "lon": LON, "remarks": f"Smoke showing {n}", "operator": "Casey <casey@example.org>"})
        ids.append(inc["id"])
        c.clock.t += 5 if hasattr(c.clock, "t") else 0
    c.dispatch(ids[0], {"unit_ids": [uid(c, "E60"), uid(c, "L31")], "operator": "Casey <casey@example.org>"})
    return c, ids


@pytest.fixture
def cad(tmp_path):
    c, ids = seed(tmp_path)
    c.ids = ids
    return c


def v1_for(cad, **flags):
    return V1(switches=reads_on(**flags), resolver=resolver, cad=cad, limits=Limits(clock=lambda: 0.0 + next(TICK)))


def get(v1, path, query="", who="val", **h):
    status, body, ctype, headers = v1.respond("GET", path, query, {**(hdr(who) if who else {}), **h})
    return status, (json.loads(body) if body else None), headers


def conforms(obj, method, path, status=200):
    schema = oas.response_schema(DOC, method, path, status)
    errs = list(Draft202012Validator(schema).iter_errors(obj))
    assert not errs, [e.message for e in errs][:3]


# -- who, and the flag -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", READS)
def test_no_principal_is_a_401_and_a_flag_off_is_a_404(cad, path):
    v1 = v1_for(cad)
    assert get(v1, path, who=None)[0] == 401
    assert get(v1, path, who="val")[0] in (200, 404)
    off = V1(switches=Switches(lambda: True, lambda: {"reads": False}), resolver=resolver, cad=cad)
    assert get(off, path)[0] == 404 and get(off, path, who=None)[0] == 404      # nothing is said before the flag
    master = V1(switches=Switches(lambda: False, lambda: {"reads": True}), resolver=resolver, cad=cad)
    assert get(master, path)[0] == 404


def test_the_routes_need_a_device_the_reads_flag_and_the_least_role():
    from sizeup.api.v1.routes import ROUTES
    mine = [r for r in ROUTES if r.path != "/api/v1/meta"]
    assert {r.path for r in mine} == {"/api/v1/snapshot", "/api/v1/incidents", "/api/v1/incidents/{id}",
                                      "/api/v1/units", "/api/v1/alarms"}
    assert all((r.method, r.need, r.flag, r.role) == ("GET", "device", "reads", "read") for r in mine)


def test_no_cad_here_is_a_503_not_an_empty_answer():
    v1 = V1(switches=reads_on(), resolver=resolver)
    status, body, headers = get(v1, "/api/v1/snapshot")
    assert status == 503 and body["error"]["code"] == "unavailable" and headers["Retry-After"] == "5"


# -- rev and ETag ----------------------------------------------------------------------------------------------

def test_rev_of_is_exactly_the_agreed_function():
    obj = {"b": [1, 2.5, None], "a": "é  \"q\"", "c": {"z": True, "y": False}}
    want = hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()[:16]
    assert rev_of(obj) == want and len(want) == 16
    assert rev_of({"a": 1, "b": 2}) == rev_of({"b": 2, "a": 1}) and rev_of({"a": 1}) != rev_of({"a": 2})


def test_each_object_carries_the_hash_of_the_rest_of_it(cad):
    v1 = v1_for(cad)
    _, snap, _ = get(v1, "/api/v1/snapshot")
    for kind in ("incidents", "units", "alarms"):
        for o in snap[kind]:
            body = {k: v for k, v in o.items() if k != "rev"}
            assert o["rev"] == rev_of(body)
    inc, unit = snap["incidents"][0], snap["units"][0]
    assert unit["rev"] == get(v1, "/api/v1/units", "limit=1")[1]["items"][0]["rev"]
    assert inc["rev"] != unit["rev"]


@pytest.mark.parametrize("path, query", [("/api/v1/snapshot", ""), ("/api/v1/incidents", ""), ("/api/v1/incidents/1", ""),
                                         ("/api/v1/units", ""), ("/api/v1/alarms", "")])
def test_if_none_match_is_a_304_with_no_body_until_it_changes(cad, path, query):
    v1 = v1_for(cad)
    status, obj, headers = get(v1, path, query)
    etag = headers["ETag"]
    assert status == 200 and etag == f'"{obj["rev"]}"'
    again = get(v1, path, query)
    assert again[1]["rev"] == obj["rev"] and again[2]["ETag"] == etag               # stable across requests
    for sent in (etag, "W/" + etag, obj["rev"], f'"x", {etag}', "*"):
        s, body, h = get(v1, path, query, **{"If-None-Match": sent})
        assert (s, body, h["ETag"]) == (304, None, etag), sent
    assert get(v1, path, query, **{"If-None-Match": '"0000000000000000"'})[0] == 200
    cad.note(cad.ids[0], {"text": "Second alarm", "operator": "Casey"})             # a write: new revs (the chronology)
    cad.update(cad.ids[0], {"remarks": "Second alarm struck", "operator": "Casey"})   # (and what a list shows)
    cad.alarms.on_traffic(tx("FG1-3", "Engine 60 mayday mayday", rig="E60", unit="E60 P1", radio_id="6401",
                             at=cad.clock() - 1))
    cad.clock.t += 2
    s, body, h = get(v1, path, query, **{"If-None-Match": etag})
    if path != "/api/v1/units":
        assert s == 200 and h["ETag"] != etag
    else:
        assert s == 304 and h["ETag"] == etag                                       # the units did not change


def test_a_role_changes_nothing_a_role_may_see_but_the_rev_is_per_what_it_sees(cad):
    v1 = v1_for(cad)
    assert get(v1, "/api/v1/snapshot", who="val")[1]["rev"] == get(v1, "/api/v1/snapshot", who="casey")[1]["rev"]
    cad.premise.create({"incident_id": cad.ids[0], "text": "Oxygen in use, rear bedroom", "category": "medical",
                        "operator": "Casey"})
    post(cad, "premise_visibility", action="set", key="medical", value="admin")
    revs = {w: get(v1, "/api/v1/incidents/%d" % cad.ids[0], who=w)[1]["rev"] for w in ("val", "jordan", "casey")}
    assert revs["val"] == revs["jordan"] != revs["casey"]


# -- paging ---------------------------------------------------------------------------------------------------

def walk(v1, path, query="", limit=3):
    seen, after, pages = [], None, 0
    while True:
        q = f"limit={limit}" + (f"&after={after}" if after else "") + (f"&{query}" if query else "")
        status, page, _ = get(v1, path, q)
        assert status == 200, page
        pages += 1
        seen += [i["id"] for i in page["items"]]
        after = page["next"]
        assert pages < 50
        if after is None:
            return seen, pages


def test_incidents_page_newest_first_with_no_repeat_and_a_last_page_without_next(tmp_path):
    c, ids = seed(tmp_path, calls=7)
    v1 = v1_for(c)
    seen, pages = walk(v1, "/api/v1/incidents")
    assert pages == 3 and seen == [str(i) for i in reversed(ids)]                     # 3 + 3 + 1, newest first
    exact, pages = walk(v1, "/api/v1/incidents", limit=7)
    assert pages == 1 and exact == seen                                               # a full page: no next, no empty page
    one, pages = walk(v1, "/api/v1/incidents", limit=1)
    assert pages == 7 and one == seen


def test_a_cursor_resumes_correctly_when_rows_are_added_or_change_state(tmp_path):
    c, ids = seed(tmp_path, calls=6)
    v1 = v1_for(c)
    _, first, _ = get(v1, "/api/v1/incidents", "limit=2")
    c.clock.t += 60
    c.create({"type": "ALARM", "address": "25 Sample St", "operator": "Casey"})         # newer than the cursor
    c.close(ids[3], {"operator": "Casey", "disposition": "NO ACTION"})                # a row after the cursor changes state
    _, second, _ = get(v1, "/api/v1/incidents", "limit=2&after=" + first["next"])
    assert [i["id"] for i in second["items"]] == [str(ids[3]), str(ids[2])]            # nothing skipped, none repeated
    assert second["items"][0]["status"] == "closed"


def test_state_narrows_to_open_or_recent(tmp_path):
    c, ids = seed(tmp_path, calls=4)
    c.close(ids[1], {"operator": "Casey", "disposition": "NO ACTION"})
    v1 = v1_for(c)
    assert [i["id"] for i in get(v1, "/api/v1/incidents", "state=recent")[1]["items"]] == [str(ids[1])]
    assert [i["id"] for i in get(v1, "/api/v1/incidents", "state=open")[1]["items"]] == \
        [str(i) for i in (ids[3], ids[2], ids[0])]
    assert get(v1, "/api/v1/incidents", "state=bogus")[0] == 400


def test_an_empty_cad_is_an_empty_page_not_an_error(tmp_path):
    c = make_cad(tmp_path)
    v1 = v1_for(c)
    s, page, _ = get(v1, "/api/v1/incidents")
    assert (s, page["items"], page["next"]) == (200, [], None)
    s, snap, _ = get(v1, "/api/v1/snapshot")
    assert (s, snap["incidents"], snap["alarms"]) == (200, [], []) and snap["units"]     # the board has its units
    assert get(v1, "/api/v1/alarms")[1]["items"] == []


@pytest.mark.parametrize("after", ["!!!", "x" * 300, "", "e30", "bm90IGpzb24",                       # junk, long, {}, not JSON
                                   paging.encode("units", [0, 1]),                                  # another list's cursor
                                   paging.encode("incidents", ["a", 1]), paging.encode("incidents", [1.0]),
                                   paging.encode("incidents", [True, 1]),
                                   paging.encode("incidents", [float("inf"), 1])])
def test_a_bad_cursor_is_a_400(cad, after):
    v1 = v1_for(cad)
    status, body, _ = get(v1, "/api/v1/incidents", "after=" + after)
    if after == "":
        assert status == 200                                       # no cursor: the first page
        return
    assert status == 400 and body["error"]["code"] == "bad_cursor"


@pytest.mark.parametrize("limit", ["0", "-1", "x", "1.5", "99999999"])
def test_a_bad_limit_is_a_400_and_a_big_one_is_clamped(cad, limit):
    v1 = v1_for(cad)
    status, body, _ = get(v1, "/api/v1/incidents", "limit=" + limit)
    if limit == "99999999":
        assert status == 200 and len(body["items"]) == 3
    else:
        assert status == 400 and body["error"]["code"] == "bad_request"


def test_the_caps_are_what_the_spec_says(cad):
    assert (paging.DEFAULT_LIMIT, paging.MAX_LIMIT) == (50, 200)
    assert "at most 200" in DOC["paths"]["/api/v1/incidents"]["get"]["description"]
    assert paging.limit_of({"limit": ["500"]}) == 200 and paging.limit_of({}) == 50
    assert paging.limit_of({"limit": ["900"]}, 100, 500) == 500


def test_units_page_in_board_order(cad):
    v1 = v1_for(cad)
    every = [u["id"] for u in cad.store.units()]
    seen, pages = walk(v1, "/api/v1/units", limit=10)
    assert seen == [str(i) for i in every] and pages == -(-len(every) // 10)
    assert get(v1, "/api/v1/units", "after=" + paging.encode("incidents", [1.0, 1]))[0] == 400
    s, page, _ = get(v1, "/api/v1/units", "limit=500")
    assert s == 200 and page["next"] is None
    e60 = next(u for u in page["items"] if u["name"] == "E60")
    assert e60["incident_ids"] == [str(cad.ids[0])] and e60["incident_number"]


def test_the_cursor_is_opaque_and_a_page_rev_covers_its_next(cad):
    v1 = v1_for(cad)
    _, p1, _ = get(v1, "/api/v1/incidents", "limit=1")
    _, p2, _ = get(v1, "/api/v1/incidents", "limit=1&after=" + p1["next"])
    assert p1["rev"] != p2["rev"] and not any(ch in p1["next"] for ch in '{}":,')


# -- what the person sees: the console's own rules -----------------------------------------------------------------

def link_radio(c, iid, tid, system, text, rid="6401", unit="E60 P1"):
    c.record_traffic({"id": tid, "start": c.clock() + 1, "end": c.clock() + 4, "channel": "FG1", "talkgroup": None,
                      "system": system, "source": "FG1", "unit": unit, "rig": "E60", "radio_id": rid, "text": text})
    c.store.db.execute("INSERT OR IGNORE INTO radio_links (traffic_id, incident_id, why) VALUES (?, ?, 'manual')", (tid, iid))


def detail(v1, c, who):
    s, body, _ = get(v1, "/api/v1/incidents/%d" % c.ids[0], who=who)
    assert s == 200
    return body


def test_a_category_kept_to_admins_is_not_in_a_viewers_or_dispatchers_incident(cad):
    for cat, text in (("access", "Knox box at the side door"), ("medical", "Oxygen in use in the rear bedroom")):
        cad.premise.create({"incident_id": cad.ids[0], "text": text, "category": cat, "operator": "Casey"})
    cad.preplan.create({"incident_id": cad.ids[0], "kind": "knox_box", "lat": LAT + 100 * 0.00009, "lon": LON,
                        "label": "Side door"})
    cad.preplan.create({"incident_id": cad.ids[0], "kind": "fdc", "lat": LAT + 100 * 0.00009, "lon": LON, "label": "Side A"})
    post(cad, "premise_visibility", action="set", key="medical", value="admin")
    post(cad, "premise_visibility", action="set", key="knox_box", value="dispatcher")
    v1 = v1_for(cad)
    cats = lambda b: sorted(n["category"] for n in b["premise"])          # noqa: E731
    kinds = lambda b: sorted(p["kind"] for p in b["preplan"])              # noqa: E731
    assert (cats(detail(v1, cad, "val")), kinds(detail(v1, cad, "val"))) == (["access"], ["fdc"])
    assert (cats(detail(v1, cad, "jordan")), kinds(detail(v1, cad, "jordan"))) == (["access"], ["fdc", "knox_box"])
    assert (cats(detail(v1, cad, "casey")), kinds(detail(v1, cad, "casey"))) == (["access", "medical"], ["fdc", "knox_box"])
    assert "Oxygen" not in json.dumps(detail(v1, cad, "val")) and "Oxygen" not in json.dumps(get(v1, "/api/v1/snapshot")[1])


def test_a_viewer_gets_nothing_the_console_hides_from_a_viewer(cad):
    """The same incident through the console's own routes, as the same person: what v1 sends is a subset."""
    for cat in ("access", "medical", "hazard"):
        cad.premise.create({"incident_id": cad.ids[0], "text": f"{cat} note", "category": cat, "operator": "Casey"})
    post(cad, "premise_visibility", action="set", key="medical", value="admin")
    link_radio(cad, cad.ids[0], "FG1-ours", "", "Engine 60 on scene")
    link_radio(cad, cad.ids[0], "9211-p1", "mrrs", "suspect fled northbound", rid="77777", unit="PD 4")
    api, v1 = CadApi(cad), v1_for(cad)
    for who, email in (("val", VAL), ("jordan", JORDAN), ("casey", CASEY)):
        code, raw, _ = api.get("/api/cad/premise", "where=Example", Identity(email=email))
        console_notes = {str(n["id"]) for n in json.loads(raw)["notes"]}
        mine = detail(v1, cad, who)
        assert {n["id"] for n in mine["premise"]} <= console_notes
        code, raw, _ = api.get(f"/api/cad/incident/{cad.ids[0]}", "", Identity(email=email))
        console_rows = {r["id"] for r in json.loads(raw)["radio"]}
        assert {r["id"] for r in mine["radio"]} <= console_rows
    assert {n["category"] for n in detail(v1, cad, "val")["premise"]} == {"access", "hazard"}


def test_another_p25_systems_rows_are_the_consoles_gate_and_never_carry_a_radio_id(cad):
    link_radio(cad, cad.ids[0], "FG1-ours", "", "Engine 60 on scene")
    link_radio(cad, cad.ids[0], "9211-p1", "mrrs", "suspect fled northbound", rid="77777", unit="PD 4")
    v1 = v1_for(cad)
    body = detail(v1, cad, "val")
    assert {r["id"] for r in body["radio"]} == {"FG1-ours", "9211-p1"}                # a signed-in person: as the console
    text = json.dumps([get(v1, p, who="val")[1] for p in ("/api/v1/snapshot", "/api/v1/incidents/%d" % cad.ids[0])])
    assert "6401" not in text and "77777" not in text and "radio_id" not in text and '"rig"' not in text
    # the gate itself: a reader who may not see context gets none of its rows (all-in-one, a hub with no Access)
    rows = [{"id": "a", "start": 1.0, "system": ""}, {"id": "b", "start": 2.0, "system": "mrrs", "text": "x"}]
    assert [r["id"] for r in radio_rows(rows, False)] == ["a"] and [r["id"] for r in radio_rows(rows, True)] == ["a", "b"]


def test_names_never_carry_an_email_and_the_caller_is_not_sent(cad):
    cad.update(cad.ids[0], {"caller_name": "Pat Example", "phone": "555-0100", "caller_address": "1 Sample St",
                            "operator": "Casey"})
    cad.note(cad.ids[0], {"text": "Hydrant out on the corner", "operator": "Casey <casey@example.org>"})
    v1 = v1_for(cad)
    text = json.dumps(detail(v1, cad, "val"))
    assert "casey@example.org" not in text and "Pat Example" not in text and "555-0100" not in text
    assert {e["operator"] for e in detail(v1, cad, "val")["events"]} <= {"", "Casey"}
    assert name_of("Casey <casey@example.org>") == "Casey" and name_of("casey@example.org") == "" and name_of("Lt. Ruiz") == "Lt. Ruiz"


def test_a_deactivated_person_reads_as_the_console_lets_a_viewer(cad):
    cad.premise.create({"incident_id": cad.ids[0], "text": "Medical note", "category": "medical", "operator": "Casey"})
    post(cad, "premise_visibility", action="set", key="medical", value="dispatcher")
    TOKENS["Bearer tok-dee"] = "dee@example.org"
    try:
        v1 = v1_for(cad)
        assert detail(v1, cad, "dee")["premise"] == []
        assert len(detail(v1, cad, "jordan")["premise"]) == 1
    finally:
        del TOKENS["Bearer tok-dee"]


# -- the incident ---------------------------------------------------------------------------------------------

def test_one_incident_has_its_chronology_and_units(cad):
    v1 = v1_for(cad)
    body = detail(v1, cad, "val")
    assert body["id"] == str(cad.ids[0]) and body["status"] in ("active", "pending") and body["number"]
    assert {u["name"] for u in body["units"]} == {"E60", "L31"}
    assert [e["kind"] for e in body["events"]][0] == "created" and any(e["kind"] == "dispatch" for e in body["events"])
    assert body["dispatches"] and body["server_time"] == cad.clock()


@pytest.mark.parametrize("raw", ["9999", "0", "abc", "1e3", "-1", "1" * 13, "%00"])
def test_no_such_incident_is_a_404(cad, raw):
    status, body, _ = get(v1_for(cad), "/api/v1/incidents/" + raw)
    assert status == 404 and body["error"]["code"] == "not_found"


# -- alarms ----------------------------------------------------------------------------------------------------

def test_alarms_open_then_recent_and_a_cad_fault_is_a_503(cad):
    cad.record_traffic(tx("FG1-9", "Engine 64 mayday mayday", rig="E64", unit="E64 P1", radio_id="6499", at=cad.clock() - 1))
    a = cad.alarms.on_traffic(tx("FG1-9", "Engine 64 mayday mayday", rig="E64", unit="E64 P1", radio_id="6499", at=cad.clock() - 1))
    v1 = v1_for(cad)
    cad.clock.t += 2
    s, body, _ = get(v1, "/api/v1/alarms")
    (row,) = body["items"]
    assert row["id"] == str(a["id"]) and row["state"] == "open" and row["who"] == "E64 P1" and "mayday" in row["what"]
    assert not {"radio_id", "rig", "talker", "unit", "keywords", "spans"} & set(row)
    cad.alarm_ack(a["id"], {"operator": "Jordan <jordan@example.org>"})
    cad.clock.t += 2
    row2 = get(v1, "/api/v1/alarms")[1]["items"][0]
    assert row2["state"] == "recent" and row2["ack_by"] == "Jordan" and row2["rev"] != row["rev"]
    assert [x["id"] for x in get(v1, "/api/v1/snapshot")[1]["alarms"]] == [str(a["id"])]
    cad.alarms_state = lambda now=None: None                                         # a fault the CAD swallowed
    cad.clock.t += 10
    for p in ("/api/v1/alarms", "/api/v1/snapshot"):
        s, body, h = get(v1, p)
        assert s == 503 and body["error"]["code"] == "unavailable" and h["Retry-After"] == "2"


# -- cost (#446) and read-only ----------------------------------------------------------------------------------

def test_a_burst_of_reads_costs_one_rebuild_and_writes_nothing(tmp_path):
    c, ids = seed(tmp_path, calls=5)
    v1 = v1_for(c)
    built = {"incidents": 0, "units": 0}
    for name, kind in (("incidents", "incidents"), ("units", "units")):
        real = getattr(c.store, name)

        def counted(*a, _real=real, _k=kind, **kw):
            built[_k] += 1
            return _real(*a, **kw)
        setattr(c.store, name, counted)
    rev, writes = c.store.rev, c.store.db.total_changes
    for _ in range(40):
        for p in ("/api/v1/snapshot", "/api/v1/incidents", "/api/v1/units"):
            assert get(v1, p, who=("val", "jordan", "casey")[_ % 3])[0] == 200
    assert built == {"incidents": 1, "units": 1}
    assert (c.store.rev, c.store.db.total_changes) == (rev, writes)                  # read only: no commit, no row
    c.note(ids[0], {"text": "Update", "operator": "Casey"})                          # a change: the next read rebuilds
    get(v1, "/api/v1/snapshot")
    assert built["incidents"] == 2


def test_the_cache_ages_out_the_closed_window_even_with_no_write(tmp_path):
    c, ids = seed(tmp_path, calls=2)
    c.close(ids[1], {"operator": "Casey", "disposition": "NO ACTION"})
    v1 = v1_for(c)
    assert len(get(v1, "/api/v1/incidents")[1]["items"]) == 2
    c.clock.t += 25 * 3600                                                           # past the 24 h of closed calls
    assert [i["id"] for i in get(v1, "/api/v1/incidents")[1]["items"]] == [str(ids[0])]


def test_what_a_snapshot_costs_on_a_seeded_board(tmp_path):
    c, ids = seed(tmp_path, calls=40)
    v1 = v1_for(c)
    t = time.perf_counter()
    status, body, ctype, headers = v1.respond("GET", "/api/v1/snapshot", "", hdr())
    cold = time.perf_counter() - t
    t = time.perf_counter()
    for _ in range(50):
        v1.respond("GET", "/api/v1/snapshot", "", hdr())
    warm = (time.perf_counter() - t) / 50
    print(f"snapshot: {len(json.loads(body)['incidents'])} incidents, {len(body)} bytes, cold {cold * 1000:.1f} ms, "
          f"warm {warm * 1000:.2f} ms")
    assert status == 200 and warm < 0.05 and cold < 2


# -- the spec, over real HTTP ----------------------------------------------------------------------------------------

def test_every_read_answers_in_the_specs_shape_over_http_and_a_304_has_no_body(cad):
    import urllib.error
    import urllib.request
    srv, url = serve(role="hub", cad=CadApi(cad), mobile=reads_on(), mobile_resolver=resolver)
    cad.record_traffic(tx("FG1-9", "Engine 64 mayday", rig="E64", unit="E64 P1", radio_id="6499", at=time.time() - 1))
    cad.alarms.on_traffic(tx("FG1-9", "Engine 64 mayday", rig="E64", unit="E64 P1", radio_id="6499", at=time.time() - 1))
    link_radio(cad, cad.ids[0], "FG1-ours", "", "Engine 60 on scene")
    cad.premise.create({"incident_id": cad.ids[0], "text": "Knox box at the side door", "category": "access",
                        "operator": "Casey"})
    cad.preplan.create({"incident_id": cad.ids[0], "kind": "fdc", "lat": LAT + 100 * 0.00009, "lon": LON, "label": "Side A"})

    def fetch(path, h=None):
        req = urllib.request.Request(url + path, headers={**hdr(), **(h or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
    try:
        for path, spec_path in (("/api/v1/snapshot", "/api/v1/snapshot"), ("/api/v1/incidents?limit=2", "/api/v1/incidents"),
                                (f"/api/v1/incidents/{cad.ids[0]}", "/api/v1/incidents/{id}"),
                                ("/api/v1/units?limit=3", "/api/v1/units"), ("/api/v1/alarms", "/api/v1/alarms")):
            status, headers, body = fetch(path)
            assert status == 200 and headers["Content-Type"].startswith("application/json"), path
            conforms(json.loads(body), "GET", spec_path)
            s2, h2, b2 = fetch(path, {"If-None-Match": headers["ETag"]})
            assert (s2, b2, h2["ETag"]) == (304, b"", headers["ETag"]), path
        status, _, body = fetch("/api/v1/incidents?after=nope")
        assert status == 400
        conforms(json.loads(body), "GET", "/api/v1/incidents", 400)
        assert fetch("/api/v1/incidents/777")[0] == 404
        assert fetch("/api/v1/snapshot", {"Authorization": "Bearer nope"})[0] == 401
    finally:
        srv.shutdown()
