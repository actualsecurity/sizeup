"""The read resources of /api/v1 (#560): snapshot, incidents, one incident, units, alarms. A real Cad seeded with
invented calls (Example Ave, the units of data/cad_lists.json), a test resolver for the device principal, the
`reads` flag turned on through injected Switches. Everything is invented; no real address, caller or radio ID."""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import threading
import time

import pytest
from jsonschema import Draft202012Validator

from sizeup.api.v1 import paging
from sizeup.api.v1.handler import V1
from sizeup.api.v1.limits import Limits
from sizeup.api.v1.principal import Principal
from sizeup.api.v1.rev import rev_of
from sizeup.api.v1.serialize import SCRUB_MAX_CHARS, name_of, radio_rows, scrub
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
    # a dispatch starts two workers that commit later (the voice's audio, service._send; the radio log's units
    # backfill, radiolog.units_soon), which moves the CAD's change counter at a time of its own. Here the voice job is
    # taken and never run and the backfill runs inline, so a test's counts are its own. (Those late commits moved the
    # test's `(rev, total_changes)` check; a second rebuild could only happen before the 1 s rebuild floor.)
    c.voice.put = lambda job: True
    c.radiolog.inline = True
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
    own = ("/api/v1/meta", "/api/v1/changes", "/api/v1/stream")      # (the change log's routes are #561's)
    mine = [r for r in ROUTES if r.path not in own]
    assert {r.path for r in mine} == {"/api/v1/snapshot", "/api/v1/incidents", "/api/v1/incidents/{id}",
                                      "/api/v1/units", "/api/v1/alarms"}
    assert all((r.method, r.need, r.flag, r.role) == ("GET", "device", "reads", "read") for r in mine)


def test_no_cad_here_is_a_503_not_an_empty_answer():
    v1 = V1(switches=reads_on(), resolver=resolver)
    status, body, headers = get(v1, "/api/v1/snapshot")
    assert status == 503 and body["error"]["code"] == "unavailable" and headers["Retry-After"] == "5"


# -- rev and ETag ----------------------------------------------------------------------------------------------

def test_rev_of_is_exactly_the_agreed_function():
    """A literal: another pull request defines the identical function, and both must give this."""
    obj = {"b": [1, 2.5, None], "a": "\u00e9 x", "c": {"z": True}}
    assert rev_of(obj) == "49b40a1026b17706"
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


@pytest.mark.parametrize("path, query", [("/api/v1/snapshot", ""), ("/api/v1/incidents", ""),
                                         ("/api/v1/incidents/1", ""),
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
    assert pages == 1 and exact == seen  # a full page: no next, no empty page
    one, pages = walk(v1, "/api/v1/incidents", limit=1)
    assert pages == 7 and one == seen


def test_a_cursor_resumes_correctly_when_rows_are_added_or_change_state(tmp_path):
    c, ids = seed(tmp_path, calls=6)
    v1 = v1_for(c)
    _, first, _ = get(v1, "/api/v1/incidents", "limit=2")
    c.clock.t += 60
    c.create({"type": "ALARM", "address": "25 Sample St", "operator": "Casey"})         # newer than the cursor
    c.close(ids[3], {"operator": "Casey", "disposition": "NO ACTION"})  # a row after the cursor changes state
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


def test_no_cursor_and_an_empty_one_are_the_first_page(cad):
    v1 = v1_for(cad)
    first = get(v1, "/api/v1/incidents")[1]["items"]
    assert get(v1, "/api/v1/incidents", "after=")[1]["items"] == first and len(first) == 3


@pytest.mark.parametrize("after", ["!!!", "x" * 300, "e30", "bm90IGpzb24",  # junk, long, {}, not JSON
                                   paging.encode("units", [0, 1]),  # another list's cursor
                                   paging.encode("incidents", ["a", 1]), paging.encode("incidents", [1.0]),
                                   paging.encode("incidents", [True, 1]),
                                   paging.encode("incidents", [float("inf"), 1]),
                                   paging.encode("incidents", [float("-inf"), 1]),
                                   paging.encode("incidents", [float("nan"), 1])])
def test_a_bad_cursor_is_a_400(cad, after):
    status, body, _ = get(v1_for(cad), "/api/v1/incidents", "after=" + after)
    assert status == 400 and body["error"]["code"] == "bad_cursor"


def test_a_cursor_whose_row_was_deleted_still_resumes(tmp_path):
    c, ids = seed(tmp_path, calls=6)
    v1 = v1_for(c)
    first = get(v1, "/api/v1/incidents", "limit=2")[1]
    last = first["items"][-1]["id"]
    c.store.db.execute("DELETE FROM events WHERE incident_id = ?", (int(last),))
    c.store.db.execute("DELETE FROM assignments WHERE incident_id = ?", (int(last),))
    c.store.db.execute("DELETE FROM incidents WHERE id = ?", (int(last),))
    c.store.rev += 1
    c.clock.t += 2
    second = get(v1, "/api/v1/incidents", "limit=2&after=" + first["next"])[1]
    assert [i["id"] for i in second["items"]] == [str(i) for i in (ids[3], ids[2])]    # the position, not the row


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
    c.store.db.execute("INSERT OR IGNORE INTO radio_links (traffic_id, incident_id, why) VALUES (?, ?, 'manual')",
                       (tid, iid))


def detail(v1, c, who):
    s, body, _ = get(v1, "/api/v1/incidents/%d" % c.ids[0], who=who)
    assert s == 200
    return body


def test_a_category_kept_to_admins_is_not_in_a_viewers_or_dispatchers_incident(cad):
    for cat, text in (("access", "Knox box at the side door"), ("medical", "Oxygen in use in the rear bedroom")):
        cad.premise.create({"incident_id": cad.ids[0], "text": text, "category": cat, "operator": "Casey"})
    cad.preplan.create({"incident_id": cad.ids[0], "kind": "knox_box", "lat": LAT + 100 * 0.00009, "lon": LON,
                        "label": "Side door"})
    cad.preplan.create({"incident_id": cad.ids[0], "kind": "fdc", "lat": LAT + 100 * 0.00009, "lon": LON,
                        "label": "Side A"})
    post(cad, "premise_visibility", action="set", key="medical", value="admin")
    post(cad, "premise_visibility", action="set", key="knox_box", value="dispatcher")
    v1 = v1_for(cad)
    cats = lambda b: sorted(n["category"] for n in b["premise"])          # noqa: E731
    kinds = lambda b: sorted(p["kind"] for p in b["preplan"])              # noqa: E731
    assert (cats(detail(v1, cad, "val")), kinds(detail(v1, cad, "val"))) == (["access"], ["fdc"])
    assert (cats(detail(v1, cad, "jordan")), kinds(detail(v1, cad, "jordan"))) == (["access"], ["fdc", "knox_box"])
    assert (cats(detail(v1, cad, "casey")), kinds(detail(v1, cad, "casey"))) == (["access", "medical"],
                                                                                 ["fdc", "knox_box"])
    assert "Oxygen" not in json.dumps(detail(v1, cad, "val"))
    assert "Oxygen" not in json.dumps(get(v1, "/api/v1/snapshot")[1])


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
    assert {r["id"] for r in body["radio"]} == {"FG1-ours", "9211-p1"}  # a signed-in person: as the console
    text = json.dumps([get(v1, p, who="val")[1] for p in ("/api/v1/snapshot", "/api/v1/incidents/%d" % cad.ids[0])])
    assert "6401" not in text and "77777" not in text and "radio_id" not in text and '"rig"' not in text
    # the gate itself, against the console's own answers: a signed-in person's rows are the same ids as v1's, and the
    # rows of a reader with no context access (the console with nobody verified) are what radio_rows(..., False) keeps
    api = CadApi(cad)
    signed = json.loads(api.get(f"/api/cad/incident/{cad.ids[0]}", "", Identity(email=VAL))[1])["radio"]
    nobody = json.loads(api.get(f"/api/cad/incident/{cad.ids[0]}", "")[1])["radio"]
    assert {r["id"] for r in body["radio"]} == {r["id"] for r in signed} == {"FG1-ours", "9211-p1"}
    assert {r["id"] for r in nobody} == {"FG1-ours"}
    assert [r["id"] for r in radio_rows(signed, False)] == [r["id"] for r in nobody]
    assert {r["id"] for r in radio_rows(signed, True)} == {r["id"] for r in signed}


CALLER = {"caller_name": "Zed Quill; Ray", "caller_address": "9 Quillmere Ct; Apt 4", "phone": "555-0147"}
CALLER_BITS = ("Zed Quill", "Quill", "Ray", "9 Quillmere", "Quillmere", "Apt 4", "555-0147", "0147")


def whole_api(v1, c, who):
    """Every v1 answer a device could ask for, as one text."""
    paths = ["/api/v1/snapshot", "/api/v1/incidents", "/api/v1/units", "/api/v1/alarms"]
    paths += [f"/api/v1/incidents/{i}" for i in c.ids]
    return "\n".join(json.dumps(get(v1, p, who=who)[1], ensure_ascii=False) for p in paths)


def test_the_callers_details_are_nowhere_in_any_answer_for_any_role(tmp_path):
    """The callers name, address and phone (a name with a "; " in it too) are written into the chronology when a call
    is created ("Caller info: ...") and when it is edited ("Edited caller: ..."): neither reaches a device."""
    c, ids = seed(tmp_path, calls=2)
    c.ids = ids
    inc = c.create({"type": "STRU", "address": "100 Example Ave", "lat": LAT + 100 * 0.00009, "lon": LON,
                    "remarks": "Smoke showing", "operator": "Casey", **CALLER})
    c.ids.append(inc["id"])
    c.update(ids[0], {**CALLER, "operator": "Casey"})                                  # an edit line, too
    c.update(ids[0], {"caller_name": "Zed Quill; Ray; Sam", "remarks": "Smoke, now showing", "operator": "Casey"})
    link_radio(c, ids[0], "FG1-ours", "", "Engine 60 on scene")
    chronology = " ".join(e["text"] for i in c.ids for e in c.store.full(i)["events"])
    assert "Caller info:" in chronology and "Quill" in chronology                       # (the CAD does write them)
    v1 = v1_for(c)
    for who in ("val", "jordan", "casey"):
        text = whole_api(v1, c, who)
        for bit in CALLER_BITS:
            assert bit not in text, (who, bit)
        assert "Smoke, now showing" in text                                             # the rest of the call is there


def test_the_chronology_sends_text_only_for_kinds_it_knows_and_is_the_callers_never():
    from sizeup.api.v1.serialize import EVENT_TEXT_KINDS, event_text
    assert event_text("system", "Caller info: Zed Quill · 9 Quillmere Ct · 555-0147") == ""
    assert event_text("system", "Dispatch #4 transmitted") == ""                       # a kind not listed: no text
    for kind in ("feed", "radio", "prealert", "a-kind-added-later"):
        assert event_text(kind, "anything at all") == ""
    for kind in sorted(EVENT_TEXT_KINDS):                                              # every allow-listed kind
        assert event_text(kind, "Engine 60 on scene") == "Engine 60 on scene", kind
        for who in ("Casey <casey@example.org>", "casey@example.org"):                  # an operator string in the text
            said = event_text(kind, f"Engine 60 by {who} at 14:32; all clear from {who} at 14:40")
            assert "@" not in said and "example.org" not in said, (kind, said)
            assert said.startswith("Engine 60 by ") and "14:32" in said and "14:40" in said, kind
    assert event_text("note", "Caller info: leaked by hand") == ""
    for line in ("Edited caller: Zed Quill; Ray", "Edited remarks updated; phone: 555-0147",
                 "Edited location: 9 Sample St; caller address: 9 Quillmere Ct; Apt 4"):
        assert event_text("edit", line) == "Edited caller details"
    fine = "Edited type: Structure fire; remarks updated"
    assert event_text("edit", fine) == fine


def test_an_operators_email_is_in_no_line_whatever_the_operator_string_was(tmp_path):
    """alarms.all_clear writes the raw operator into the chronology line, reopened and the ack line strip only a
    trailing "<...>": a bare email (a person with no name yet) passes through the CAD intact, and never the API."""
    c, ids = seed(tmp_path, calls=3)
    c.ids = ids
    v1 = v1_for(c)
    for n, op in enumerate(("Casey <casey@example.org>", "casey@example.org")):
        t = tx(f"FG1-{n}", "Engine 60 mayday", rig="E60", unit="E60 P1", radio_id="6401", at=c.clock() - 1)
        c.record_traffic(t)
        a = c.alarms.on_traffic(t)
        c.alarm_ack(a["id"], {"operator": op})
        c.alarm_clear(a["id"], {"operator": op})
        c.close(ids[1 + n], {"operator": op, "disposition": "NO ACTION"})
        c.reopen(ids[1 + n], {"operator": op})
        c.clock.t += 2
    raw = " ".join(e["text"] for i in ids for e in c.store.full(i)["events"])
    assert "casey@example.org" in raw                                                  # (the CAD does write it)
    for who in ("val", "jordan", "casey"):
        text = whole_api(v1, c, who)
        assert "casey@example.org" not in text and "example.org" not in text, who
        assert "@" not in text, who          # nothing on the seeded surface can hold one (a named place's " @ " is
        assert "all clear from" in text and "acknowledged by" in text and "Reopened by" in text    # not seeded here)


def test_an_unidentified_talkers_radio_id_is_masked_in_alarms(cad):
    t = tx("FG1-5", "mayday mayday", rig="", unit="", radio_id="41228", at=cad.clock() - 1)
    cad.record_traffic(t)
    a = cad.alarms.on_traffic(t)
    assert cad.alarms.get(a["id"])["who"] == "radio 41228"                              # (the CAD says it)
    cad.clock.t += 2
    v1 = v1_for(cad)
    row = get(v1, "/api/v1/alarms")[1]["items"][0]
    assert row["who"] == "an unidentified radio"
    assert "41228" not in whole_api(v1, cad, "val")


def test_a_cache_warmed_by_one_role_never_serves_another_what_it_may_not_see(cad):
    cad.premise.create({"incident_id": cad.ids[0], "text": "Oxygen in use, rear bedroom", "category": "medical",
                        "operator": "Casey"})
    post(cad, "premise_visibility", action="set", key="medical", value="admin")
    v1 = v1_for(cad)
    path = f"/api/v1/incidents/{cad.ids[0]}"
    admin = get(v1, path, who="casey")[1]                                               # warms every cache
    assert [n["category"] for n in admin["premise"]] == ["medical"]
    viewer = get(v1, path, who="val")[1]
    assert viewer["premise"] == [] and "Oxygen" not in json.dumps(viewer) and viewer["rev"] != admin["rev"]
    same = ("/api/v1/snapshot", "/api/v1/incidents", "/api/v1/units", "/api/v1/alarms")
    for p in same:  # role-independent, byte for byte
        a, b = get(v1, p, who="casey")[1], get(v1, p, who="val")[1]
        assert {**a, "server_time": 0} == {**b, "server_time": 0}, p


def test_names_never_carry_an_email(cad):
    cad.note(cad.ids[0], {"text": "Hydrant out on the corner", "operator": "Casey <casey@example.org>"})
    v1 = v1_for(cad)
    text = json.dumps(detail(v1, cad, "val"))
    assert "casey@example.org" not in text
    assert {e["operator"] for e in detail(v1, cad, "val")["events"]} <= {"", "Casey"}
    assert name_of("Casey <casey@example.org>") == "Casey" and name_of("casey@example.org") == ""
    assert name_of("Lt. Ruiz") == "Lt. Ruiz"


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
    t = tx("FG1-9", "Engine 64 mayday mayday", rig="E64", unit="E64 P1", radio_id="6499", at=cad.clock() - 1)
    cad.record_traffic(t)
    a = cad.alarms.on_traffic(t)
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

def counted_builds(c):
    built = {"incidents": 0, "units": 0}
    for name in built:
        real = getattr(c.store, name)

        def counted(*a, _real=real, _k=name, **kw):
            built[_k] += 1
            return _real(*a, **kw)
        setattr(c.store, name, counted)
    return built


def test_a_burst_of_reads_costs_one_rebuild_and_writes_nothing(tmp_path):
    c, ids = seed(tmp_path, calls=5)
    v1 = v1_for(c)
    built = counted_builds(c)
    rev, writes = c.store.rev, c.store.db.total_changes
    for n in range(40):
        for p in ("/api/v1/snapshot", "/api/v1/incidents", "/api/v1/units"):
            assert get(v1, p, who=("val", "jordan", "casey")[n % 3])[0] == 200
    assert built == {"incidents": 1, "units": 1}
    assert (c.store.rev, c.store.db.total_changes) == (rev, writes)                  # read only: no commit, no row
    c.note(ids[0], {"text": "Update", "operator": "Casey"})                          # a change, inside the floor:
    get(v1, "/api/v1/snapshot")
    assert built["incidents"] == 1                                                   # the list is served as it was
    c.clock.t += 1.5                                                                 # past the rebuild floor
    get(v1, "/api/v1/snapshot")
    assert built["incidents"] == 2


def test_a_burst_of_writes_costs_one_rebuild_a_second_not_one_per_poll(tmp_path):
    c, ids = seed(tmp_path, calls=3)
    v1 = v1_for(c)
    built = counted_builds(c)
    get(v1, "/api/v1/incidents")
    for n in range(20):
        c.note(ids[0], {"text": f"Update {n}", "operator": "Casey"})                 # a write between every poll
        c.clock.t += 0.01
        assert get(v1, "/api/v1/incidents")[0] == 200
    assert built["incidents"] == 1
    c.clock.t += 1.0
    get(v1, "/api/v1/incidents")
    assert built["incidents"] == 2


def test_a_failing_build_is_run_once_a_second_not_once_per_waiting_request(cad):
    v1 = v1_for(cad)
    runs = []

    def broken(now=None):
        runs.append(1)
        return None                                                                  # the CAD's own swallowed fault
    cad.alarms_state = broken
    for _ in range(10):
        assert get(v1, "/api/v1/alarms")[0] == 503
    assert len(runs) == 1
    cad.clock.t += 1.5
    assert get(v1, "/api/v1/alarms")[0] == 503 and len(runs) == 2


def test_the_cache_ages_out_the_closed_window_even_with_no_write(tmp_path):
    c, ids = seed(tmp_path, calls=2)
    c.close(ids[1], {"operator": "Casey", "disposition": "NO ACTION"})
    v1 = v1_for(c)
    assert len(get(v1, "/api/v1/incidents")[1]["items"]) == 2
    c.clock.t += 25 * 3600                                                           # past the 24 h of closed calls
    assert [i["id"] for i in get(v1, "/api/v1/incidents")[1]["items"]] == [str(ids[0])]


# -- the spec, over real HTTP ----------------------------------------------------------------------------------------

def test_every_read_answers_in_the_specs_shape_over_http_and_a_304_has_no_body(cad):
    import urllib.error
    import urllib.request
    srv, url = serve(role="hub", cad=CadApi(cad), mobile=reads_on(), mobile_resolver=resolver)
    cad.record_traffic(tx("FG1-9", "Engine 64 mayday", rig="E64", unit="E64 P1", radio_id="6499", at=time.time() - 1))
    cad.alarms.on_traffic(tx("FG1-9", "Engine 64 mayday", rig="E64", unit="E64 P1", radio_id="6499",
                             at=time.time() - 1))
    link_radio(cad, cad.ids[0], "FG1-ours", "", "Engine 60 on scene")
    cad.premise.create({"incident_id": cad.ids[0], "text": "Knox box at the side door", "category": "access",
                        "operator": "Casey"})
    cad.preplan.create({"incident_id": cad.ids[0], "kind": "fdc", "lat": LAT + 100 * 0.00009, "lon": LON,
                        "label": "Side A"})

    def fetch(path, h=None):
        req = urllib.request.Request(url + path, headers={**hdr(), **(h or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
    try:
        for path, spec_path in (("/api/v1/snapshot", "/api/v1/snapshot"),
                                ("/api/v1/incidents?limit=2", "/api/v1/incidents"),
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


# -- the spec and the served shapes agree both ways -----------------------------------------------------------

EXAMPLES = {"Incident": "Incident", "Unit": "Unit", "Alarm": "Alarm", "IncidentDetail": "IncidentDetail"}


def test_every_example_file_validates_against_its_schema():
    import yaml
    for name in EXAMPLES:
        example = yaml.safe_load((oas.SPEC_DIR / "examples" / f"{name}.yaml").read_text())
        schema = oas.bundle(yaml.safe_load((oas.SPEC_DIR / "schemas" / f"{name}.yaml").read_text()),
                            oas.SPEC_DIR / "schemas" / f"{name}.yaml")
        errs = list(Draft202012Validator(schema).iter_errors(example))
        assert not errs, (name, [e.message for e in errs][:3])
        assert "Port Chester" not in json.dumps(example)  # fictional


def test_every_field_served_is_in_the_schema_and_every_one_always_served_is_required(cad):
    """Two ways: the served object has no field the spec does not name (so a field cannot be added unlisted), and the
    spec requires every field the server always sends."""
    cad.premise.create({"incident_id": cad.ids[0], "text": "Knox box", "category": "access", "operator": "Casey"})
    cad.preplan.create({"incident_id": cad.ids[0], "kind": "fdc", "lat": LAT + 100 * 0.00009, "lon": LON, "label": "A"})
    cad.alarms.on_traffic(tx("FG1-7", "Engine 60 mayday", rig="E60", unit="E60 P1", radio_id="6401",
                             at=cad.clock() - 1))
    link_radio(cad, cad.ids[0], "FG1-ours", "", "Engine 60 on scene")
    cad.clock.t += 2
    v1 = v1_for(cad)
    snap = get(v1, "/api/v1/snapshot")[1]
    det = get(v1, f"/api/v1/incidents/{cad.ids[0]}")[1]
    inc_schema = oas.response_schema(DOC, "GET", "/api/v1/incidents", 200)["properties"]["items"]["items"]
    unit_schema = oas.response_schema(DOC, "GET", "/api/v1/units", 200)["properties"]["items"]["items"]
    alarm_schema = oas.response_schema(DOC, "GET", "/api/v1/alarms", 200)["properties"]["items"]["items"]
    det_schema = oas.response_schema(DOC, "GET", "/api/v1/incidents/{id}", 200)
    for obj, schema in ((snap["incidents"][0], inc_schema), (snap["units"][0], unit_schema),
                        (snap["alarms"][0], alarm_schema)):
        assert set(obj) == set(schema["properties"]) == set(schema["required"])
    props = {**det_schema["allOf"][0]["properties"], **det_schema["allOf"][1]["properties"]}
    required = set(det_schema["allOf"][0]["required"]) | set(det_schema["allOf"][1]["required"])
    assert set(det) - {"rev"} <= set(props) and set(det) == required
    for key, row in (("events", det["events"][0]), ("dispatches", det["dispatches"][0]), ("premise", det["premise"][0]),
                     ("preplan", det["preplan"][0]), ("radio", det["radio"][0])):
        item = props[key]["items"]
        assert set(row) == set(item["properties"]) == set(item["required"]), key


def test_the_404_and_503_bodies_are_the_specs_error(cad):
    v1 = v1_for(cad)
    s, body, _ = get(v1, "/api/v1/incidents/99999")
    assert s == 404
    conforms(body, "GET", "/api/v1/incidents/{id}", 404)
    off = V1(switches=reads_on(), resolver=resolver)                                    # no CAD: a station
    for path, spec in (("/api/v1/snapshot", "/api/v1/snapshot"), ("/api/v1/units", "/api/v1/units"),
                       ("/api/v1/incidents", "/api/v1/incidents"), ("/api/v1/incidents/1", "/api/v1/incidents/{id}"),
                       ("/api/v1/alarms", "/api/v1/alarms")):
        s, body, h = get(off, path)
        assert s == 503 and h["Retry-After"] == "5"
        conforms(body, "GET", spec, 503)
        assert "503" in DOC["paths"][spec]["get"]["responses"]


# -- the reviewers' probes, as given (operator strings, emails of every shape, a talker's ID) ----------------

EMAIL_PROBES = ["Casey <a@x", "Casey <a@x.org> note", "Casey <a@x.org> and Bob", "Casey@Example.ORG;",
                "Casey <casey@example.org>", "casey@example.org", "josé@example.org", "a@exämple.org", "a@localhost",
                "a@[10.0.0.1]", "x@y.org@z.org", "Reopened by <a@b.org>@c.org; was closed", "a@b a@b a@b"]
ID_PROBES = ["rid 41228", "Radio  41228", "radio-41228", "radio\t41228", "radio #41228", "unit 41228", "ID 41228",
             "radio 41228"]


@pytest.mark.parametrize("probe", EMAIL_PROBES)
def test_no_email_form_survives_scrub_or_name_of(probe):
    for out in (scrub(probe), name_of(probe), scrub(probe, True), scrub(probe, identity=True),
                name_of(f"{probe} <x@y.org>")):
        assert "@" not in out, (probe, out)


@pytest.mark.parametrize("probe, scrubbed, name", [
    ("Casey <a@x", "Casey", "Casey"), ("Casey <a@x.org> note", "Casey note", "Casey note"),
    ("Casey <a@x.org> and Bob", "Casey and Bob", "Casey and Bob"), ("Casey@Example.ORG;", "an operator", ""),
    ("Casey <casey@example.org>", "Casey", "Casey"), ("casey@example.org", "an operator", ""),
    ("josé@example.org", "an operator", ""), ("a@localhost", "an operator", ""), ("a@[10.0.0.1]", "an operator", ""),
    ("x@y.org@z.org", "an operator", ""), ("Lt. Ruiz", "Lt. Ruiz", "Lt. Ruiz"),
    ("40 Example Rd @ Sample Market", "40 Example Rd @ Sample Market", "40 Example Rd @ Sample Market"),
    ("note <5 min> more", "note more", "note more")])
def test_scrub_and_name_of_exact_outputs(probe, scrubbed, name):
    assert scrub(probe) == scrubbed and name_of(probe) == name


@pytest.mark.parametrize("probe", ID_PROBES)
def test_every_radio_id_form_is_masked_in_an_alarms_words_and_identity_fields(probe):
    for out in (scrub(probe, True), scrub(probe, identity=True), scrub(f"EMERGENCY: {probe} mayday on FG1", True)):
        assert "41228" not in out and "unidentified radio" in out, (probe, out)


def test_the_mask_is_for_identity_fields_and_alarm_words_only():
    assert scrub("41228", identity=True) == "an unidentified radio" and scrub("41228", True) == "41228"
    assert scrub("12345 Example Ave, radio 41228 said", False) == "12345 Example Ave, radio 41228 said"   # as written
    assert scrub("E64 (77A4)", identity=True) == "E64 (77A4)"


N = 200_000
ADVERSARIAL = {"<": "<" * N, "a<": "a<" * (N // 2), "<a@": "<a@" * (N // 3), "a@": "a@" * (N // 2), "@": "@" * N,
               ".": "." * N, " ": " " * N, "-": "-" * N, "<+spaces+@": "<" + " " * N + "@",
               "radio ": "radio " * (N // 6),
               "radio 12345 ": "radio 12345 " * (N // 12), "radio+spaces": "radio" + " " * N + "x",
               "digits": "radio " + "1 " * (N // 2), "<a ": "<a " * (N // 3), "a.": "a." * (N // 2)}


def finishes(fn, arg):
    """fn(arg) on a thread: its result, and that it was done inside a generous 20 s (no tight clock)."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn(arg)), daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "still running after 20 s"
    return box[0]


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_200_kb_of_adversarial_input_completes_and_is_capped(name):
    text = ADVERSARIAL[name]
    for fn in (scrub, lambda t: scrub(t, True, True), name_of):
        out = finishes(fn, text)
        assert "@" not in out.replace(" @ ", "") or set(out) <= {"@"}, name        # (a standalone "@" stays)
        assert len(out) <= 6 * SCRUB_MAX_CHARS + 1                    # the cap bounds the work and the size


def test_the_cap_cuts_with_a_marker_and_short_text_has_none():
    long = "Smoke showing. " * 5000
    out = scrub(long)
    assert out.endswith("\u2026") and len(out) == len(long[:SCRUB_MAX_CHARS]) + 1
    assert scrub("Smoke showing") == "Smoke showing"
    assert scrub("x" * SCRUB_MAX_CHARS) == "x" * SCRUB_MAX_CHARS                    # exactly the cap: not cut
    assert scrub("x" * (SCRUB_MAX_CHARS + 1)).endswith("\u2026")


@pytest.mark.parametrize("form", ["a\uff20example.org", "a\uff20b", "a\ufe6b x.org", "a\u200b\uff20x", "<a\uff20x",
                                  "a\u200b@x", "a@\u202ex.org", "a\uff20b\uff20c", "\uff1ca@x\uff1e Casey",
                                  "Casey \uff1ca\uff20x.org\uff1e note"])
def test_compatibility_forms_of_the_at_sign_are_normalised_first(form):
    for out in (scrub(form), name_of(form), scrub(form, identity=True)):
        assert "@" not in out and "\uff20" not in out and "\ufe6b" not in out and "\u200b" not in out, (form, out)


@pytest.mark.parametrize("form", ["Radio No. 41228", "talker 41228", "tg 41228", "radio_41228", "radio no 41228",
                                  "radio 41 228", "radio 41,228", "RID-41228", "Rid:41228", "unit #41228"])
def test_more_radio_id_forms_are_masked_in_an_alarms_words(form):
    out = scrub(f"EMERGENCY: {form} mayday", True)
    assert not any(c.isdigit() for c in out) and "an unidentified radio mayday" in out, (form, out)


def test_the_radio_mask_leaves_small_numbers_and_other_words_alone():
    for text in ("radio 5 to command", "tg 3", "identity 41228 confirmed", "Radio check 5", "tgs 12345"):
        assert scrub(text, True) == text, text


def test_odd_operator_strings_and_a_bare_numeric_talker_never_reach_the_api(tmp_path):
    c, ids = seed(tmp_path, calls=4)
    c.ids = ids
    v1 = v1_for(c)
    ops = ["Casey <a@x", "Casey <casey@example.org> note", "Casey@Example.ORG;"]
    for n, op in enumerate(ops):
        t = tx(f"FG1-{n}", "Engine 60 mayday", rig="E60", unit="E60 P1", radio_id="6401", at=c.clock() - 1)
        c.record_traffic(t)
        a = c.alarms.on_traffic(t)
        for step in (lambda: c.alarm_ack(a["id"], {"operator": op}),
                     lambda: c.alarm_clear(a["id"], {"operator": op}),
                     lambda: c.close(ids[1 + n], {"operator": op, "disposition": "NO ACTION"}),
                     lambda: c.reopen(ids[1 + n], {"operator": op})):
            try:
                step()
            except Exception:  # noqa: BLE001 - the CAD may refuse an odd operator: what it did write is what is checked
                pass
        c.clock.t += 2
    t = tx("FG1-ID", "mayday mayday", rig="", unit="41228", radio_id="41228", at=c.clock() - 1)     # who() = "41228"
    c.record_traffic(t)
    a = c.alarms.on_traffic(t)
    assert c.alarms.get(a["id"])["who"] == "41228"                                      # (the CAD says it bare)
    c.clock.t += 2
    for who in ("val", "jordan", "casey"):
        text = whole_api(v1, c, who)
        assert "@" not in text and "41228" not in text, who
    assert get(v1, "/api/v1/alarms")[1]["items"][0]["who"] == "an unidentified radio"


def test_a_named_places_at_sign_survives(tmp_path):
    c, ids = seed(tmp_path, calls=1)
    c.ids = ids
    c.update(ids[0], {"address": "40 Example Rd @ Sample Market", "place": "Sample Market", "operator": "Casey"})
    c.clock.t += 2
    v1 = v1_for(c)
    assert "40 Example Rd @ Sample Market" in whole_api(v1, c, "val")


def test_a_note_moves_the_incidents_rev_and_last_event_id_in_the_list_and_the_detail(cad):
    v1 = v1_for(cad)
    row = lambda: {i["id"]: i for i in get(v1, "/api/v1/incidents")[1]["items"]}          # noqa: E731
    before = row()
    d0 = get(v1, f"/api/v1/incidents/{cad.ids[0]}")[1]
    assert isinstance(before[str(cad.ids[0])]["last_event_id"], int)
    assert d0["last_event_id"] == before[str(cad.ids[0])]["last_event_id"]
    cad.note(cad.ids[0], {"text": "Second alarm struck", "operator": "Casey <casey@example.org>"})
    cad.clock.t += 1.5                                                                  # past the list floor
    after = row()
    a0, b0 = after[str(cad.ids[0])], before[str(cad.ids[0])]
    assert a0["last_event_id"] > b0["last_event_id"] and a0["rev"] != b0["rev"]
    for other in cad.ids[1:]:                                                           # an untouched incident: same
        assert after[str(other)] == before[str(other)]
    assert get(v1, "/api/v1/snapshot")[1]["incidents"][-1]["last_event_id"] >= 0
    d1 = get(v1, f"/api/v1/incidents/{cad.ids[0]}")
    assert d1[0] == 200 and d1[1]["last_event_id"] == a0["last_event_id"] and d1[1]["rev"] != d0["rev"]
    assert [e["kind"] for e in d1[1]["events"]][-1] == "note"
    assert max(int(e["id"]) for e in d1[1]["events"]) == a0["last_event_id"]


def test_an_incident_with_no_events_has_a_null_last_event_id():
    from sizeup.api.v1.serialize import incident_summary
    assert incident_summary({"id": 1, "events": []})["last_event_id"] is None
    assert incident_summary({"id": 1, "events": [{"id": 4}, {"id": 9}, {"id": 7}]})["last_event_id"] == 9
