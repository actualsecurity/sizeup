"""Roles on the hub (sizeup/cad/api.py WRITES, Settings.standing()): enforced only for a person
verified through Cloudflare Access. Admins change anything, dispatchers run calls, viewers look;
nobody is locked out (no admin yet: anyone may; the last admin can't be taken away)."""

import json

import pytest

from sizeup.cad.api import WRITES, CadApi, needs
from sizeup.cloud.access import Identity
from tests.test_access import hub, jwks, keys, jwt, req  # noqa: F401  (fixtures)
from tests.test_cad import make_cad, uid
from tests.test_settings import post

COREY, JULIAN, VAL, DEE = "corey@example.org", "julian@example.org", "val@example.org", "dee@example.org"


def call(api, path, body=None, email=None):
    code, raw, _ = api.post(path, json.dumps(body or {}).encode(), Identity(email=email) if email else None)
    return code, json.loads(raw) if raw[:1] == b"{" else raw


def people(cad, admin=True):
    """Corey (admin), Julian (dispatcher), Val (viewer), Dee (a deactivated dispatcher)."""
    ids = {}
    for name, email, role in (("Corey", COREY, "admin" if admin else "dispatcher"), ("Julian", JULIAN, "dispatcher"),
                              ("Val", VAL, "viewer"), ("Dee", DEE, "dispatcher")):
        ids[name] = post(cad, "users", action="add", name=name, email=email, role=role)["id"]
    post(cad, "users", action="deactivate", id=ids["Dee"])
    return ids


@pytest.fixture
def api(tmp_path):
    a = CadApi(make_cad(tmp_path))
    a.logged = []
    a.cad.log = a.logged.append
    a.ids = people(a.cad)
    return a


def settings_writes(cad, ids):
    """Every kind of Settings write (and the other admin-only CAD routes), with a body that would work."""
    e60 = uid(cad, "E60")
    exp = cad.settings_export()
    return [
        ("/api/cad/settings/event_types", {"action": "edit", "code": "STRU", "item": {"name": "Building fire"}}),
        ("/api/cad/settings/event_types", {"action": "reset_section"}),
        ("/api/cad/settings/unit_statuses", {"action": "move", "code": "AV", "dir": 1}),
        ("/api/cad/settings/event_class", {"action": "reset_section"}),
        ("/api/cad/settings/geo_town_order", {"action": "set", "towns": ["Rye Brook", "Port Chester"]}),
        ("/api/cad/settings/units", {"action": "edit", "code": "E60", "unit": {"spoken": "Engine Sixty"}}),
        ("/api/cad/settings/dispatch", {"action": "set", "key": "speed_mph", "value": 40}),
        ("/api/cad/settings/dispatch", {"action": "set", "key": "voice", "value": "af_heart"}),
        ("/api/cad/settings/users", {"action": "add", "name": "Pat"}),
        ("/api/cad/settings/users", {"action": "edit", "id": ids["Julian"], "role": "admin"}),
        ("/api/cad/settings/users", {"action": "activate", "id": ids["Dee"]}),
        ("/api/cad/settings/runcards", {"action": "reset_section"}),
        ("/api/cad/settings/import", {"action": "dry_run", "settings": exp}),
        ("/api/cad/settings/import", {"action": "apply", "settings": exp}),
        ("/api/cad/runcards", {"type": "STRU", "slots": [{"kind": "engine", "count": 1}]}),
        ("/api/cad/runcards/delete", {"type": "STRU"}),
        ("/api/cad/radios", {"system": "mdc", "radio_id": "0999", "unit": "E60"}),
        ("/api/cad/radios/delete", {"system": "mdc", "radio_id": "0611"}),
        (f"/api/cad/unit/{e60}/station", {"station": ""}),
    ]


def test_the_permission_map():
    assert needs("/api/cad/incident") == needs("/api/cad/incident/7") == needs("/api/cad/incident/7/dispatch") == "dispatch"
    for a in ("accept", "dismiss", "close", "note", "recommend", "preview", "upgrade", "attach", "link", "return"):
        assert needs(f"/api/cad/incident/7/{a}") == "dispatch", a
    assert needs("/api/cad/unit/3/status") == needs("/api/incident/abc-1/close") == "dispatch"
    for p in ("/api/cad/unit/3/station", "/api/cad/settings/users", "/api/cad/settings/import", "/api/cad/runcards",
              "/api/cad/runcards/delete", "/api/cad/radios", "/api/cad/radios/delete", "/api/talkgroup",
              "/api/talkgroup-group", "/api/mode", "/api/retranscribe"):
        assert needs(p) == "admin", p
    assert needs("/api/cad/settings/dispatch", {"action": "preview"}) == "read"
    assert needs("/api/cad/settings/dispatch", {"action": "set"}) == "admin"
    # not listed: admin (a new write route is safe until someone decides otherwise)
    assert needs("/api/cad/incident/7/explode") == needs("/api/cad/something-new") == needs("/api/whatever") == "admin"
    assert {need for _, need in WRITES} == {"dispatch", "admin"}


def test_whoami_says_the_role_and_that_it_is_enforced(api):
    _, raw, _ = api.get("/api/cad/whoami", "", Identity(email=JULIAN))
    me = json.loads(raw)
    assert (me["name"], me["role"], me["may"], me["active"], me["enforced"], me["bootstrap"], me["admins"]) == (
        "Julian", "dispatcher", "dispatcher", True, True, False, ["Corey"])
    dee = json.loads(api.get("/api/cad/whoami", "", Identity(email=DEE))[1])
    assert (dee["role"], dee["active"], dee["may"]) == ("dispatcher", False, "viewer")
    assert json.loads(api.get("/api/cad/whoami", "", Identity(email=COREY))[1])["may"] == "admin"
    # someone new: added as a dispatcher, may what a dispatcher may
    new = json.loads(api.get("/api/cad/whoami", "", Identity(email="new@example.org"))[1])
    assert (new["name"], new["role"], new["may"]) == ("new@example.org", "dispatcher", "dispatcher")
    # all-in-one: nobody known, nothing enforced
    assert json.loads(api.get("/api/cad/whoami", "")[1]) == {"email": "", "name": "", "callsign": "", "source": "none",
                                                             "role": "", "enforced": False}


def test_an_admin_changes_settings(api):
    for path, body in settings_writes(api.cad, api.ids):
        code, out = call(api, path, body, COREY)
        assert code == 200, (path, body, out)
    assert not [m for m in api.logged if "Refused" in m]       # each is in Corey's activity (tests/test_activity.py)


def test_a_dispatcher_gets_403_on_every_settings_write_and_nothing_changes(api):
    before = (api.cad.settings.audit_log(), api.cad.settings.overrides(), api.cad.settings.operators(),
              api.cad.run_cards(), api.cad.radio_registry()["radios"])
    for path, body in settings_writes(api.cad, api.ids):
        code, out = call(api, path, body, JULIAN)
        assert code == 403, (path, out)
        assert out == {"error": "Admins only (you: dispatcher)"}
    assert (api.cad.settings.audit_log(), api.cad.settings.overrides(), api.cad.settings.operators(),
            api.cad.run_cards(), api.cad.radio_registry()["radios"]) == before
    assert len(api.logged) == len(settings_writes(api.cad, api.ids))
    assert ("CAD activity: julian@example.org: Refused: settings/users (needs admin; dispatcher) (POST /api/cad/settings/users)"
            in "\n".join(api.logged))
    # the wording preview changes nothing: anyone may
    code, out = call(api, "/api/cad/settings/dispatch", {"action": "preview", "wording": {}}, JULIAN)
    assert code == 200 and out


def test_a_dispatcher_runs_calls(api):
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": "209 Westchester Ave"}, JULIAN)
    assert code == 200
    iid, e60, e61 = inc["id"], uid(api.cad, "E60"), uid(api.cad, "E61")
    for path, body in ((f"/api/cad/incident/{iid}", {"cross_street": "Main St"}),
                       (f"/api/cad/incident/{iid}/note", {"text": "Smoke showing"}),
                       (f"/api/cad/incident/{iid}/recommend", {}),
                       (f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [e60]}),
                       (f"/api/cad/unit/{e60}/status", {"status": "RS"}),
                       (f"/api/cad/unit/{e61}/status", {"status": "OS", "note": "pump"}),
                       (f"/api/cad/incident/{iid}/close", {"disposition": "COMPLETE"})):
        code, out = call(api, path, body, JULIAN)
        assert code == 200, (path, out)
    ev = api.cad.store.q("SELECT operator FROM events WHERE incident_id = ?", (iid,))
    assert ev and all(e["operator"] == "Julian <julian@example.org>" for e in ev)
    assert api.cad.store.incident(iid)["status"] == "closed"
    assert not [m for m in api.logged if "Refused" in m]


def test_a_viewer_and_a_deactivated_user_only_look(api):
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": "209 Westchester Ave"}, COREY)
    iid, e60 = inc["id"], uid(api.cad, "E60")
    for email, why in ((VAL, "Dispatchers and admins only (you: viewer)"),
                       (DEE, "Account deactivated (ask Corey)")):
        for path, body in (("/api/cad/incident", {"type": "STRU"}),
                           (f"/api/cad/incident/{iid}/note", {"text": "hi"}),
                           (f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [e60]}),
                           (f"/api/cad/unit/{e60}/status", {"status": "RS"})):
            assert call(api, path, body, email) == (403, {"error": why}), (email, path)
        code, out = call(api, "/api/cad/settings/users", {"action": "activate", "id": api.ids["Dee"]}, email)
        assert code == 403
        assert call(api, "/api/cad/settings/dispatch", {"action": "preview", "wording": {}}, email)[0] == 200
    assert len(api.cad.store.q("SELECT * FROM incidents")) == 1
    assert not api.cad.settings._operator(api.ids["Dee"])["active"]
    # reads are untouched
    assert api.get("/api/cad/state", "", Identity(email=VAL))[0] == 200
    assert api.get("/api/cad/settings", "", Identity(email=DEE))[0] == 200


def test_no_admin_yet_anyone_signed_in_may_then_only_the_admin(tmp_path):
    api = CadApi(make_cad(tmp_path))
    api.cad.log = lambda m: None
    ids = people(api.cad, admin=False)                           # Corey is a dispatcher too: nobody is an admin
    me = json.loads(api.get("/api/cad/whoami", "", Identity(email=JULIAN))[1])
    assert (me["may"], me["bootstrap"], me["admins"]) == ("admin", True, [])
    assert call(api, "/api/cad/settings/event_types", {"action": "edit", "code": "STRU", "item": {"name": "Fire"}}, VAL)[0] == 200
    assert call(api, "/api/cad/settings/users", {"action": "activate", "id": ids["Dee"]}, DEE)[0] == 200    # even Dee
    # an admin without a sign-in email doesn't count (can't be told apart from anyone typing a name)
    post(api.cad, "users", action="add", name="Old admin", role="admin")
    assert json.loads(api.get("/api/cad/whoami", "", Identity(email=JULIAN))[1])["bootstrap"] is True
    # the first person makes themself admin: from then on it's theirs
    assert call(api, "/api/cad/settings/users", {"action": "edit", "id": ids["Corey"], "role": "admin"}, COREY)[0] == 200
    code, out = call(api, "/api/cad/settings/users", {"action": "edit", "id": ids["Julian"], "role": "admin"}, JULIAN)
    assert code == 403 and "you: dispatcher" in out["error"]
    assert call(api, "/api/cad/settings/users", {"action": "edit", "id": ids["Julian"], "role": "admin"}, COREY)[0] == 200
    assert call(api, "/api/cad/settings/event_types", {"action": "reset_section"}, JULIAN)[0] == 200    # now an admin


def test_the_last_admin_cant_be_taken_away(api):
    corey = api.ids["Corey"]
    for body in ({"action": "edit", "id": corey, "role": "dispatcher"},          # demoted (by themself)
                 {"action": "edit", "id": corey, "active": False},
                 {"action": "deactivate", "id": corey},
                 {"action": "edit", "id": corey, "email": ""}):                    # no sign-in email: can't be them
        code, out = call(api, "/api/cad/settings/users", body, COREY)
        assert code == 409 and "Corey is the last admin" in out["error"], body
    assert api.cad.settings.admins()[0]["name"] == "Corey"
    # still fine: a rename, a callsign, the admin keeps their role
    assert call(api, "/api/cad/settings/users", {"action": "edit", "id": corey, "callsign": "2398"}, COREY)[0] == 200
    # with a second admin, either may step down (not both)
    assert call(api, "/api/cad/settings/users", {"action": "edit", "id": api.ids["Julian"], "role": "admin"}, COREY)[0] == 200
    assert call(api, "/api/cad/settings/users", {"action": "edit", "id": corey, "role": "dispatcher"}, COREY)[0] == 200
    code, out = call(api, "/api/cad/settings/users", {"action": "deactivate", "id": api.ids["Julian"]}, JULIAN)
    assert code == 409 and "Julian is the last admin" in out["error"]
    # demoted, Corey is now a dispatcher like any other
    assert call(api, "/api/cad/settings/users", {"action": "edit", "id": corey, "role": "admin"}, COREY)[0] == 403


def test_an_import_cant_remove_every_admin(api):
    data = json.loads(json.dumps(api.cad.settings_export()))
    for o in data["operators"]:
        if o["name"] == "Corey":
            o["role"] = "dispatcher"
    for action in ("dry_run", "apply"):
        code, out = call(api, "/api/cad/settings/import", {"action": action, "settings": data}, COREY)
        assert code == 409 and "no active admin" in out["error"] and "Corey" in out["error"], action
    # the file gives Corey's email to someone who isn't an admin: the same
    data = json.loads(json.dumps(api.cad.settings_export()))
    data["operators"] = [{"name": "Imposter", "role": "dispatcher", "email": COREY}]
    assert call(api, "/api/cad/settings/import", {"action": "apply", "settings": data}, COREY)[0] == 409
    assert api.cad.settings.admins()[0]["email"] == COREY
    # one admin left in the file (or no operators in it at all): fine
    data = json.loads(json.dumps(api.cad.settings_export()))
    for o in data["operators"]:
        o["role"] = "admin" if o["name"] == "Julian" else "dispatcher"
    assert call(api, "/api/cad/settings/import", {"action": "apply", "settings": data}, COREY)[0] == 200
    assert [a["name"] for a in api.cad.settings.admins()] == ["Julian"]
    data.pop("operators")
    assert call(api, "/api/cad/settings/import", {"action": "apply", "settings": data}, JULIAN)[0] == 200


def test_an_unknown_write_route_is_admin_only(api):
    code, out = call(api, "/api/cad/something-new", {}, JULIAN)
    assert code == 403 and "Admins only" in out["error"]
    assert call(api, "/api/cad/something-new", {}, COREY)[0] == 404                  # allowed, then not found
    assert call(api, "/api/cad/incident/1/explode", {}, JULIAN)[0] == 403


def test_all_in_one_records_roles_but_enforces_nothing(tmp_path):
    api = CadApi(make_cad(tmp_path))
    ids = people(api.cad)
    # anyone at the console (a typed or picked name) changes settings, as before
    assert call(api, "/api/cad/settings/users", {"action": "add", "name": "Pat", "operator": "Val"})[0] == 200
    assert call(api, "/api/cad/incident", {"type": "STRU", "operator": "Dee"})[0] == 200
    # and the last-admin guard is the hub's: here the role is only recorded
    assert call(api, "/api/cad/settings/users", {"action": "edit", "id": ids["Corey"], "role": "viewer", "operator": "Val"})[0] == 200
    assert api.cad.settings.admins() == []


def test_the_hub_enforces_roles_for_who_signed_in(keys, hub):  # noqa: F811
    cad = hub.app.cad
    ids = people(cad)
    as_ = lambda email: {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=email)}
    me = req(hub.url + "/api/cad/whoami", headers=as_(JULIAN))[1]
    assert (me["role"], me["may"], me["enforced"], me["admins"]) == ("dispatcher", "dispatcher", True, ["Corey"])
    # a dispatcher: calls yes, settings no (whatever operator the page claims)
    code, inc = req(hub.url + "/api/cad/incident", "POST", {"type": "STRU", "operator": "Corey"}, as_(JULIAN))
    assert code == 200
    code, out = req(hub.url + "/api/cad/settings/users", "POST", {"action": "add", "name": "Pat", "operator": "Corey"}, as_(JULIAN))
    assert (code, out) == (403, {"error": "Admins only (you: dispatcher)"})
    # the radio switches forwarded to the station are settings too; the monitor's close is a call
    for path, body in (("/api/talkgroup", {"tg": 307, "record": True}), ("/api/talkgroup-group", {"group": "Fire", "main": True}),
                       ("/api/mode", {"mode": "trunk"}), ("/api/retranscribe", {"all": True})):
        assert req(hub.url + path, "POST", body, as_(JULIAN))[0] == 403, path
        assert req(hub.url + path, "POST", body, as_(COREY))[0] != 403, path
    assert req(hub.url + "/api/incident/nope/close", "POST", {}, as_(JULIAN))[0] == 404     # allowed: no such incident
    assert req(hub.url + "/api/incident/nope/close", "POST", {}, as_(VAL))[0] == 403
    # the admin
    assert req(hub.url + "/api/cad/settings/users", "POST", {"action": "add", "name": "Pat"}, as_(COREY))[0] == 200
    code, out = req(hub.url + "/api/cad/settings/users", "POST", {"action": "deactivate", "id": ids["Corey"]}, as_(COREY))
    assert code == 409 and "last admin" in out["error"]
    # a viewer: nothing
    assert req(hub.url + f"/api/cad/incident/{inc['id']}/note", "POST", {"text": "hi"}, as_(VAL))[0] == 403
    assert [e["operator"] for e in cad.store.q("SELECT operator FROM events WHERE incident_id = ?", (inc["id"],))] == [
        "Julian <julian@example.org>"] * len(cad.store.q("SELECT 1 FROM events WHERE incident_id = ?", (inc["id"],)))
