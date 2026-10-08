"""Per-person activity on the hub (sizeup/cad/activity.py; user, 2026-10-08: "lets add some per user
logging too"): one "signed in" per Access login, every write as a sentence, every refusal, when each
person was last seen; theirs to read, an admin's to read for everyone; kept 90 days. All-in-one
records nothing new."""

import csv
import io
import json
import time

import pytest

from sizeup.cad import activity as activity_mod
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from tests.test_access import hub, jwks, keys, jwt, req, verifier  # noqa: F401  (fixtures)
from tests.test_cad import make_cad, uid
from tests.test_roles import COREY, DEE, JULIAN, VAL, call, people

IP, UA = "203.0.113.9", "Mozilla/5.0 (Macintosh) Chrome/129"


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture
def api(tmp_path):
    a = CadApi(make_cad(tmp_path, clock=Clock()))
    a.clock = a.cad.store.clock
    a.logged = []
    a.cad.log = a.logged.append
    a.ids = people(a.cad)
    return a


def rows(api, **kw):
    return api.cad.activity.rows(**kw)


def get(api, path, query="", email=None):
    code, raw, ctype = api.get(path, query, Identity(email=email) if email else None)
    return code, json.loads(raw) if "json" in ctype else raw


def test_one_sign_in_per_access_session(api, tmp_path):
    corey = Identity(email=COREY, session="1760000000")
    for _ in range(3):                                       # every request of the same login
        api.visit(corey, (IP, UA), "GET /cad")
    api.clock.t += 120
    api.visit(corey, ("198.51.100.7", UA), "GET /api/cad/state")
    (r,) = rows(api)
    assert (r["kind"], r["summary"], r["route"], r["ip"], r["ua"], r["ok"], r["name"]) == (
        "signin", "Signed in", "GET /cad", IP, UA, True, "Corey")
    api.visit(Identity(email=COREY, session="1760050000"), (IP, UA), "GET /cad")     # logged in again: a new token
    assert [r["kind"] for r in rows(api)] == ["signin", "signin"]
    api.visit(Identity(email=JULIAN, session="1760000000"), (IP, UA), "GET /cad")    # the same iat, someone else
    assert len(rows(api, kind="signin")) == 3
    # a restart doesn't sign everyone in again
    again = CadApi(make_cad(tmp_path, clock=api.clock))
    again.visit(Identity(email=COREY, session="1760050000"), (IP, UA), "GET /cad")
    assert len(again.cad.activity.rows(kind="signin")) == 3
    assert any("CAD activity: corey@example.org: Signed in (GET /cad) ip=203.0.113.9" in m for m in api.logged)


def test_the_token_says_which_login_it_is(keys, jwks):  # noqa: F811
    v = verifier(jwks)
    assert v.identity(jwt(keys[0], iat=1760000000)).session == "1760000000"
    assert v.identity(jwt(keys[0], jti="abc-123")).session == "abc-123"           # jti / nonce first, if there is one
    assert v.identity(jwt(keys[0], iat=None)).session == ""


def test_writes_are_readable_sentences_with_the_incident_number(api):
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "subtype": "RESD", "address": "209 Westchester Ave"}, JULIAN)
    assert code == 200
    iid, n, e60, l31 = inc["id"], inc["number"], uid(api.cad, "E60"), uid(api.cad, "L31")
    for path, body in ((f"/api/cad/incident/{iid}/recommend", {}),                  # changes nothing: not logged
                       (f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [e60, l31]}),
                       (f"/api/cad/incident/{iid}/note", {"text": "Smoke showing from the second floor", "units": ["E60"]}),
                       (f"/api/cad/unit/{e60}/status", {"status": "OS", "note": "pump"}),
                       (f"/api/cad/incident/{iid}", {"cross_street": "Main St", "operator": "Mallory"}),
                       (f"/api/cad/incident/{iid}/close", {"disposition": "COMPLETE"})):
        assert call(api, path, body, JULIAN)[0] == 200, path
    assert call(api, "/api/cad/settings/users", {"action": "edit", "id": api.ids["Julian"], "role": "viewer"}, COREY)[0] == 200
    assert call(api, "/api/cad/settings/dispatch", {"action": "set", "key": "speed_kmh", "value": 40}, COREY)[0] == 200
    assert call(api, "/api/cad/settings/dispatch", {"action": "preview", "wording": {}}, COREY)[0] == 200   # reads only
    got = [(r["email"], r["kind"], r["summary"], r["incident_number"], r["route"]) for r in reversed(rows(api))]
    assert got == [
        (JULIAN, "action", f"Created {n} STRU/RESD at 209 Westchester Ave", n, "POST /api/cad/incident"),
        (JULIAN, "action", f"Dispatched {n}: E60, L31", n, f"POST /api/cad/incident/{iid}/dispatch"),
        (JULIAN, "action", f"Note on {n} (E60): Smoke showing from the second floor", n, f"POST /api/cad/incident/{iid}/note"),
        (JULIAN, "action", "Set E60 to OS (pump)", None, f"POST /api/cad/unit/{e60}/status"),
        (JULIAN, "action", f"Edited {n}: cross_street", n, f"POST /api/cad/incident/{iid}"),
        (JULIAN, "action", f"Closed {n}: COMPLETE", n, f"POST /api/cad/incident/{iid}/close"),
        (COREY, "action", "Changed settings: users/Julian role", None, "POST /api/cad/settings/users"),
        (COREY, "action", "Changed settings: dispatch/speed_kmh set", None, "POST /api/cad/settings/dispatch"),
    ]
    assert all(r["ok"] for r in rows(api))
    # the chronology and the settings audit are as they were
    assert api.cad.store.q("SELECT operator FROM events WHERE incident_id = ? AND kind = 'created'", (iid,))[0]["operator"] \
        == "Julian <julian@example.org>"
    assert api.cad.settings.audit_log("users")[0]["operator"] == "Corey <corey@example.org>"
    assert f"CAD activity: julian@example.org: Dispatched {n}: E60, L31 [{n}] (POST /api/cad/incident/{iid}/dispatch) ip=-" \
        in api.logged


def test_refusals_are_logged(api):
    code, inc = call(api, "/api/cad/incident", {"type": "STRU"}, COREY)
    for path, body, email in (("/api/cad/settings/users", {"action": "add", "name": "Pat"}, JULIAN),
                              (f"/api/cad/incident/{inc['id']}/note", {"text": "hi"}, VAL),
                              (f"/api/cad/incident/{inc['id']}/note", {"text": "hi"}, DEE)):
        assert call(api, path, body, email)[0] == 403
    code, _ = call(api, "/api/cad/settings/users", {"action": "deactivate", "id": api.ids["Corey"]}, COREY)
    assert code == 409                                                                  # the last admin
    got = [(r["email"], r["kind"], r["summary"], r["incident_number"], r["ok"]) for r in reversed(rows(api, kind="refused"))]
    assert got == [
        (JULIAN, "refused", "Refused: settings/users (needs admin; dispatcher)", None, False),
        (VAL, "refused", f"Refused: incident/{inc['id']}/note (needs dispatch; viewer)", inc["number"], False),
        (DEE, "refused", f"Refused: incident/{inc['id']}/note (needs dispatch; dispatcher, deactivated)", inc["number"], False),
        (COREY, "refused", "Refused: settings/users (the last admin)", None, False),
    ]


def test_all_in_one_records_nothing_new(tmp_path):
    api = CadApi(make_cad(tmp_path))
    people(api.cad)
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "operator": "Val"})
    assert code == 200
    assert call(api, f"/api/cad/incident/{inc['id']}/note", {"text": "hi", "operator": "Val"})[0] == 200
    assert call(api, "/api/cad/settings/users", {"action": "add", "name": "Pat", "operator": "Val"})[0] == 200
    api.visit(None, (IP, UA), "GET /cad")
    assert api.cad.store.q("SELECT * FROM user_activity") == []
    assert all(o["last_seen"] is None for o in api.cad.store.q("SELECT last_seen FROM operators"))
    # nobody verified: the page reads (an empty list), as every read here
    code, out = get(api, "/api/cad/activity")
    assert code == 200 and out["rows"] == []


def test_your_own_activity_and_an_admin_everyones(api):
    api.visit(Identity(email=JULIAN, session="1"), (IP, UA), "GET /cad")
    call(api, "/api/cad/incident", {"type": "STRU"}, JULIAN)
    call(api, "/api/cad/settings/users", {"action": "add", "name": "Pat"}, JULIAN)          # refused
    call(api, "/api/cad/incident", {"type": "MVA"}, COREY)
    # Julian: his own, never someone else's
    code, out = get(api, "/api/cad/activity", "", JULIAN)
    assert code == 200 and not out["all"] and out["email"] == JULIAN
    assert [r["kind"] for r in out["rows"]] == ["refused", "action", "signin"] and {r["email"] for r in out["rows"]} == {JULIAN}
    assert [s["email"] for s in out["seen"]] == [JULIAN] and out["seen"][0]["last_ip"] == IP
    assert get(api, "/api/cad/activity", f"email={JULIAN}&kind=refused", JULIAN)[1]["rows"][0]["kind"] == "refused"
    code, out = get(api, "/api/cad/activity", f"email={COREY}", JULIAN)
    assert code == 403 and "Only an admin" in out["error"]
    assert get(api, "/api/cad/activity", "", VAL)[1]["rows"] == []                          # a viewer: theirs (none)
    # Corey (admin): anyone's, or everyone's
    code, out = get(api, "/api/cad/activity", "", COREY)
    assert code == 200 and out["all"] and {r["email"] for r in out["rows"]} == {JULIAN, COREY}
    assert {s["email"] for s in out["seen"]} == {COREY, JULIAN, VAL, DEE}
    assert [r["kind"] for r in get(api, "/api/cad/activity", f"email={JULIAN}&kind=action", COREY)[1]["rows"]] == ["action"]
    assert len(get(api, "/api/cad/activity", "limit=1", COREY)[1]["rows"]) == 1
    assert get(api, "/api/cad/activity", f"since={api.clock.t + 1}", COREY)[1]["rows"] == []
    assert get(api, "/api/cad/activity", "kind=nope", COREY)[0] == 400


def test_the_csv_is_for_admins(api):
    call(api, "/api/cad/incident", {"type": "STRU", "address": "209 Westchester Ave"}, JULIAN)
    code, out = get(api, "/api/cad/export/activity.csv", "", JULIAN)
    assert code == 403 and "Only an admin" in out["error"]
    code, raw = get(api, "/api/cad/export/activity.csv", f"email={JULIAN}", COREY)
    assert code == 200
    table = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert table[0] == activity_mod.CSV_COLUMNS
    assert table[1][1:7] == ["Julian", JULIAN, "action", "Created 26-000001 STRU at 209 Westchester Ave", "26-000001", "yes"]


def test_rows_older_than_90_days_are_dropped(api):
    act = api.cad.activity
    act.record(COREY, "action", "POST /api/cad/incident", "Created old")
    api.clock.t += 89 * 86400
    act.record(COREY, "action", "POST /api/cad/incident", "Created newer")
    api.clock.t += 2 * 86400                         # the first is 91 days old; a day since the last prune
    act.record(COREY, "action", "POST /api/cad/incident", "Created now")
    assert [r["summary"] for r in act.rows()] == ["Created now", "Created newer"]
    api.clock.t += 89 * 86400
    assert act.prune() == 1 and [r["summary"] for r in act.rows()] == ["Created now"]
    assert any("dropped 1 rows older than 90 days" in m for m in api.logged)


def test_last_seen_is_written_at_most_once_a_minute(api):
    seen = lambda: api.cad.store.one("SELECT last_seen, last_ip, last_ua FROM operators WHERE email = ?", (COREY,))
    corey = Identity(email=COREY, session="1")
    t0 = api.clock.t
    api.visit(corey, (IP, UA), "GET /cad")
    assert seen() == {"last_seen": t0, "last_ip": IP, "last_ua": UA}
    api.clock.t += 30
    api.visit(corey, ("198.51.100.7", "curl"), "GET /api/cad/state")
    assert seen()["last_seen"] == t0                                          # within the minute: not written
    api.clock.t += 31
    api.visit(corey, ("198.51.100.7", "curl"), "GET /api/cad/state")
    assert seen() == {"last_seen": t0 + 61, "last_ip": "198.51.100.7", "last_ua": "curl"}
    # someone not on the list yet: no row to stamp until they are (their first whoami adds them)
    pat = Identity(email="pat@example.org", session="1")
    api.visit(pat, (IP, UA), "GET /cad")
    api.get("/api/cad/whoami", "", pat)
    api.visit(pat, (IP, UA), "GET /api/cad/state")
    assert api.cad.store.one("SELECT last_seen FROM operators WHERE email = ?", ("pat@example.org",))["last_seen"] == api.clock.t
    # an admin's to see: not on the Settings page every reader gets
    assert "last_ip" not in api.cad.settings_get()["operators"][0]


def test_an_older_database_gets_the_table(tmp_path):
    cad = make_cad(tmp_path)
    cad.store.db.execute("DROP TABLE user_activity")
    cad.store.db.execute("ALTER TABLE operators DROP COLUMN last_ua")
    cad.store.db.close()
    cad = make_cad(tmp_path)
    assert cad.store.q("SELECT * FROM user_activity") == []
    assert "last_ua" in {r["name"] for r in cad.store.q("PRAGMA table_info(operators)")}


def test_the_hub_logs_who_signed_in_and_what_they_did(keys, hub):  # noqa: F811
    cad = hub.app.cad
    people(cad)
    as_ = lambda email, iat=1760000000: {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=email, iat=iat),
                                         "Cf-Connecting-Ip": IP, "User-Agent": UA}
    for _ in range(2):
        assert req(hub.url + "/api/cad/whoami", headers=as_(JULIAN))[0] == 200
    code, inc = req(hub.url + "/api/cad/incident", "POST", {"type": "STRU", "address": "209 Westchester Ave"}, as_(JULIAN))
    assert code == 200
    assert req(hub.url + "/api/talkgroup", "POST", {"tg": 307, "record": True}, as_(JULIAN))[0] == 403
    assert req(hub.url + "/api/talkgroup", "POST", {"tg": 307, "record": True}, as_(COREY))[0] != 403
    code, out = req(hub.url + "/api/cad/activity", headers=as_(JULIAN))
    assert code == 200
    assert [(r["kind"], r["summary"], r["ip"], r["ua"]) for r in reversed(out["rows"])] == [
        ("signin", "Signed in", IP, UA),
        ("action", f"Created {inc['number']} STRU at 209 Westchester Ave", IP, UA),
        ("refused", "Refused: talkgroup (needs admin; dispatcher)", IP, UA)]
    assert req(hub.url + "/api/cad/activity?email=" + COREY, headers=as_(JULIAN))[0] == 403
    assert req(hub.url + "/api/cad/export/activity.csv", headers=as_(JULIAN))[0] == 403
    code, out = req(hub.url + "/api/cad/activity?email=" + COREY, headers=as_(COREY))
    kinds = [(r["kind"], r["summary"]) for r in out["rows"]]
    assert ("signin", "Signed in") in kinds
    code, raw = req(hub.url + "/api/cad/export/activity.csv", headers=as_(COREY))
    assert code == 200 and b"Signed in" in raw
    # a new login: one more sign-in
    req(hub.url + "/api/cad/whoami", headers=as_(JULIAN, iat=1760090000))
    assert len(cad.activity.rows(email=JULIAN, kind="signin")) == 2
