"""/api/v1 writes (#562): a unit's status, an alarm acknowledged, a note on an incident; Idempotency-Key, If-Match, queued
(offline) actions, the audit. Against a real CAD seeded the way tools/dev_seed.py seeds one (an invented call near the
map's home, its units, an emergency alarm in the radio log's shape). Every person and device is invented."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request

import pytest

from sizeup.api.v1 import writes
from sizeup.api.v1.handler import V1
from sizeup.api.v1.idempotency import KEEP_S, MAX_PER_PRINCIPAL, Idempotency, fingerprint
from sizeup.api.v1.principal import Principal
from sizeup.api.v1.rev import rev_of
from sizeup.api.v1.switches import Switches
from sizeup.cad.api import CadApi
from sizeup.fence import FencedWrite
from tests.test_alarms import tx
from tests.test_api_v1 import serve
from tests.test_cad import make_cad, uid
from tests.test_roles import JORDAN, VAL, people

DISPATCHER = Principal("dev-jordan-1", JORDAN)
VIEWER = Principal("dev-val-1", VAL)
XCLIENT = "SizeUp/1.4.2 (ios/18.1; 412)"
UUID = "7c1f3a52-0b7d-4f43-9a6e-3d1f5b2a9c10"


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class Rig:
    """A CAD with people, one open call with E60 and E63 on it, one closed call and an unacknowledged alarm; a v1
    whose resolver says who the Authorization header is."""

    def __init__(self, tmp_path, features=None):
        self.clock = Clock()
        self.api = CadApi(make_cad(tmp_path, clock=self.clock))
        self.cad = self.api.cad
        self.cad.log = lambda m: None
        self.ids = people(self.cad)
        self.open = self.cad.create({"type": "STRU", "address": "100 Example Ave", "operator": "Lt. Ruiz"})
        self.cad.dispatch(self.open["id"], {"unit_ids": [uid(self.cad, "E60"), uid(self.cad, "E63")], "operator": "Lt. Ruiz"})
        self.shut = self.cad.create({"type": "ALARM", "address": "25 Sample St", "operator": "Lt. Ruiz"})
        self.cad.close(self.shut["id"], {"disposition": "FALSE", "operator": "Lt. Ruiz"})
        self.alarm = self.cad.alarms.on_traffic(tx(text="Engine 64 mayday mayday mayday", rig="E64", radio_id="6401",
                                                   at=self.clock.t - 5))
        who = {"Bearer jordan": DISPATCHER, "Bearer val": VIEWER}
        self.v1 = V1(switches=Switches(lambda: True, lambda: features if features is not None else {"writes": True}),
                     resolver=lambda h: who.get(h.get("Authorization")), cad=self.api)

    def post(self, path, body=None, key=UUID, token="jordan", headers=None, raw=None):
        raw = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        h = {"Authorization": f"Bearer {token}", "X-Client": XCLIENT, **({"Idempotency-Key": key} if key else {}),
             **(headers or {})}
        status, out, _, hdrs = self.v1.respond("POST", path, "", h, "203.0.113.7", lambda limit: raw)
        return status, json.loads(out), hdrs

    def activity(self, kind="action"):
        return self.cad.activity.rows(kind=kind)

    def notes(self, iid=None):
        return [e["text"] for e in self.cad.store.full(iid or self.open["id"])["events"] if e["kind"] == "note"]

    def unit(self, code):
        return next(u for u in self.cad.store.units() if u["name"] == code)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def status_path(code="E60"):
    return f"/api/v1/units/{code}/status"


def note_path(iid):
    return f"/api/v1/incidents/{iid}/notes"


# -- the three happy paths, through the console's own code -------------------------------------------------------------

def test_unit_status_goes_through_the_console_and_is_audited(rig):
    status, out, _ = rig.post(status_path(), {"status": "OL"})
    assert status == 200 and out["unit"]["code"] == "E60" and out["unit"]["status"] == "OL"
    assert out["unit"]["incident_id"] == rig.open["id"] and out["unit"]["rev"] == rev_of(
        {k: v for k, v in out["unit"].items() if k != "rev"})
    assert rig.unit("E60")["status"] == "OL"
    assert any("E60" in t for t in [e["text"] for e in rig.cad.store.full(rig.open["id"])["events"]][-1:])
    row = rig.activity()[0]                                      # the console's own sentence and route, plus the device
    assert row["email"] == JORDAN and row["route"] == f"POST /api/cad/unit/{rig.unit('E60')['id']}/status"
    assert row["summary"].startswith("Set E60 to OL")
    assert row["ua"] == "via=ios app=1.4.2 device=dev-jordan-1" and row["ip"] == "203.0.113.7"
    assert "client_at" not in out


def test_the_unit_code_is_case_insensitive_and_may_name_a_call(rig):
    status, out, _ = rig.post(status_path("e63"), {"status": "OL", "incident_id": rig.open["id"], "note": "at the hydrant"})
    assert status == 200 and out["unit"]["code"] == "E63" and out["unit"]["incident_number"] == rig.open["number"]


def test_alarm_ack_goes_through_the_console_and_is_audited(rig):
    aid = rig.alarm["id"]
    status, out, _ = rig.post(f"/api/v1/alarms/{aid}/ack", key="b" * 16)
    assert status == 200 and out["alarm"]["id"] == aid and out["alarm"]["ack_at"] is not None
    assert out["alarm"]["ack_by"] == rig.cad.settings.standing(JORDAN)["operator"]
    assert rig.cad.alarms.get(aid)["ack_at"] == out["alarm"]["ack_at"]
    row = rig.activity()[0]
    assert row["route"] == f"POST /api/cad/alarm/{aid}/ack" and "device=dev-jordan-1" in row["ua"]


def test_a_note_is_a_chronology_entry_by_the_person(rig):
    status, out, _ = rig.post(note_path(rig.open["id"]), {"text": "E60 stated the hydrant is out of service",
                                                           "units": ["E60"]})
    assert status == 201 and out == {"incident_id": rig.open["id"], "units": ["E60"], "places": []}
    ev = [e for e in rig.cad.store.full(rig.open["id"])["events"] if e["kind"] == "note"][-1]
    assert ev["text"] == "E60 stated the hydrant is out of service"
    assert ev["operator"] == rig.cad.settings.standing(JORDAN)["operator"]
    assert rig.activity()[0]["route"] == f"POST /api/cad/incident/{rig.open['id']}/note"


def test_the_consoles_validation_still_applies(rig):
    for path, body in ((status_path(), {"status": "NOPE"}), (status_path(), {}), (status_path(), {"status": "OL", "incident_id": "x"}),
                       (note_path(rig.open["id"]), {}), (note_path(rig.open["id"]), {"text": "x", "units": ["E99"]}),
                       (note_path(rig.open["id"]), {"text": "x" * 2001})):
        status, out, _ = rig.post(path, body, key=f"k-{abs(hash(path + str(body)))}")
        assert status == 400 and out["error"]["code"] == "invalid" and out["error"]["message"], (path, body)


# -- roles: the console's, never who=None ----------------------------------------------------------------------------

def test_a_viewer_is_refused_every_write_in_v1s_envelope(rig):
    before = rig.unit("E60")["status"], len(rig.cad.store.full(rig.open["id"])["events"])
    for path, body in ((status_path(), {"status": "OL"}), (f"/api/v1/alarms/{rig.alarm['id']}/ack", {}),
                       (note_path(rig.open["id"]), {"text": "hello"})):
        status, out, _ = rig.post(path, body, token="val")
        assert status == 403 and out["error"]["code"] == "forbidden" and "Dispatchers and admins only" in out["error"]["message"]
    assert before == (rig.unit("E60")["status"], len(rig.cad.store.full(rig.open["id"])["events"]))
    assert rig.cad.alarms.get(rig.alarm["id"])["ack_at"] is None
    assert [r["ok"] for r in rig.activity("refused")] == [False] * 3          # in the viewer's activity, as on the console
    assert "device=dev-val-1" in rig.activity("refused")[0]["ua"]


def test_a_deactivated_person_is_refused(rig):
    rig.cad.settings_post("users", {"action": "deactivate", "id": rig.ids["Jordan"]})
    status, out, _ = rig.post(status_path(), {"status": "OL"})
    assert status == 403 and "deactivated" in out["error"]["message"].lower()


def test_no_handler_reaches_the_console_without_a_person(rig, monkeypatch):
    seen = []
    real = CadApi.post

    def spy(self, path, raw, who=None, client=("", "")):
        seen.append(who)
        return real(self, path, raw, who, client)
    monkeypatch.setattr(CadApi, "post", spy)
    rig.post(status_path(), {"status": "OL"})
    rig.post(note_path(rig.open["id"]), {"text": "a"}, key="n" * 12)
    rig.post(f"/api/v1/alarms/{rig.alarm['id']}/ack", key="a" * 12)
    assert len(seen) == 3 and all(w is not None and w.email == JORDAN for w in seen)
    req = writes.Req(type("C", (), {"gateway": rig.v1.gateway, "principal": Principal("d", "no-at-sign"), "headers": {}, "body": b"",
                                    "peer": ""})(), lambda api: ("/x", 1))
    assert req.refusal[0] == 503 and len(seen) == 3                         # not a person: nothing reached the CAD


def test_the_token_and_the_flag_gate_everything(rig):
    assert rig.post(status_path(), {"status": "OL"}, token="nobody")[0] == 401
    off = Rig.__new__(Rig)
    off.__dict__ = rig.__dict__ | {"v1": V1(switches=Switches(lambda: True, lambda: {"reads": True}),
                                            resolver=lambda h: DISPATCHER, cad=rig.api)}
    for path in (status_path(), f"/api/v1/alarms/{rig.alarm['id']}/ack", note_path(rig.open["id"])):
        s, out, _ = off.post(path, {"status": "OL", "text": "x"})
        assert (s, out["error"]["code"]) == (404, "not_found")
    assert rig.unit("E60")["status"] != "OL"


def test_the_route_table_names_the_consoles_least_role():
    from sizeup.cad.api import needs
    assert [(r.need, r.flag, r.role) for r in writes.ROUTES] == [("device", "writes", "dispatch")] * 3
    assert needs("/api/cad/unit/3/status") == needs("/api/cad/alarm/3/ack") == needs("/api/cad/incident/3/note") == "dispatch"


# -- not found, closed ----------------------------------------------------------------------------------------------

def test_unknown_things_are_404(rig):
    for path, body, code in ((status_path("X99"), {"status": "OL"}, "no_such_unit"),
                             (note_path(999), {"text": "x"}, "no_such_incident"),
                             (note_path("abc"), {"text": "x"}, "no_such_incident"),
                             ("/api/v1/alarms/999/ack", {}, "no_such_alarm"),
                             ("/api/v1/alarms/zero/ack", {}, "no_such_alarm"),
                             (status_path(), {"status": "OL", "incident_id": 999}, "no_such_incident")):
        status, out, _ = rig.post(path, body)
        assert (status, out["error"]["code"]) == (404, code), path


def test_a_note_on_a_closed_incident_is_a_409_with_the_current_incident(rig):
    status, out, _ = rig.post(note_path(rig.shut["id"]), {"text": "late news", "client_at": rig.clock.t - 600})
    assert status == 409 and out["error"]["reason"] == "incident_closed"
    assert out["current"]["status"] == "closed" and out["current"]["disposition"] == "FALSE"
    assert out["current"]["rev"] == rev_of({k: v for k, v in out["current"].items() if k != "rev"})
    assert rig.notes(rig.shut["id"]) == []


def test_a_status_on_a_closed_incident_is_a_409_with_the_current_unit(rig):
    before = rig.unit("E61")["status"]
    status, out, _ = rig.post(status_path("E61"), {"status": "RS", "incident_id": rig.shut["id"]})
    assert status == 409 and out["error"]["reason"] == "incident_closed" and out["current"]["code"] == "E61"
    assert rig.unit("E61")["status"] == before


def test_an_alarm_already_acknowledged_is_a_409_with_the_current_alarm(rig):
    aid = rig.alarm["id"]
    rig.cad.alarm_ack(aid, {"operator": "Lt. Ruiz"})                      # a dispatcher at the console got there first
    status, out, _ = rig.post(f"/api/v1/alarms/{aid}/ack")
    assert status == 409 and out["error"]["reason"] == "already_acknowledged"
    assert out["current"]["ack_by"] == "Lt. Ruiz" and "acknowledged by Lt. Ruiz" in out["error"]["message"]


# -- Idempotency-Key -----------------------------------------------------------------------------------------------

def test_a_retry_with_the_same_key_and_body_is_applied_once(rig):
    body = {"text": "Water supply established"}
    first = rig.post(note_path(rig.open["id"]), body)
    again = rig.post(note_path(rig.open["id"]), {"text": "Water supply established"})
    assert first[0] == again[0] == 201 and first[1] == again[1]
    assert "Idempotent-Replayed" not in first[2] and again[2]["Idempotent-Replayed"] == "true"
    assert rig.notes().count("Water supply established") == 1
    assert len(rig.activity()) == 1                                             # one audit row, not two


def test_a_status_retry_replays_even_after_the_unit_moved_on(rig):
    first = rig.post(status_path(), {"status": "OL"})
    rig.cad.unit_status(uid(rig.cad, "E60"), {"status": "AV", "operator": "Lt. Ruiz"})     # someone else clears it
    again = rig.post(status_path(), {"status": "OL"})
    assert again[0] == 200 and again[1] == first[1] and rig.unit("E60")["status"] == "AV"     # the old answer, no rewrite


def test_the_same_key_with_another_body_or_path_is_a_422_and_applies_nothing(rig):
    assert rig.post(note_path(rig.open["id"]), {"text": "one"})[0] == 201
    for path, body in ((note_path(rig.open["id"]), {"text": "two"}), (status_path(), {"status": "OL"})):
        status, out, _ = rig.post(path, body)
        assert status == 422 and out["error"]["code"] == "idempotency_key_reused"
    assert rig.notes() == ["one"] and rig.unit("E60")["status"] != "OL"


def test_key_order_and_spacing_of_the_body_do_not_matter(rig):
    a = rig.post(status_path(), raw=b'{"status":"OL","note":"x"}')
    b = rig.post(status_path(), raw=b'{ "note": "x",  "status": "OL" }')
    assert a[0] == b[0] == 200 and b[2]["Idempotent-Replayed"] == "true"


def test_a_key_belongs_to_one_device(rig):
    rig.cad.settings_post("users", {"action": "edit", "id": rig.ids["Casey"], "role": "dispatcher"})
    casey = Principal("dev-casey-1", "casey@example.org")
    rig.v1.resolver = lambda h: casey if h.get("Authorization") == "Bearer casey" else DISPATCHER
    assert rig.post(note_path(rig.open["id"]), {"text": "mine"})[0] == 201
    other = rig.post(note_path(rig.open["id"]), {"text": "hers"}, token="casey")
    assert other[0] == 201 and "Idempotent-Replayed" not in other[2]               # the same key, another principal


def test_the_key_is_required_and_checked(rig):
    for key in (None, "", "short", "x" * 129, "has space in it!"):
        status, out, _ = rig.post(note_path(rig.open["id"]), {"text": "x"}, key=key)
        assert status == 400 and out["error"]["code"] in ("idempotency_key_required", "invalid_idempotency_key"), key
    assert rig.notes() == []


def test_a_correction_after_a_400_may_use_the_same_key(rig):
    assert rig.post(status_path(), {"status": "NOPE"})[0] == 400
    assert rig.post(status_path(), {"status": "OL"})[0] == 200


def test_a_request_still_running_is_a_409_in_progress(rig):
    idem = rig.v1.gateway.idempotency()
    idem.claim(DISPATCHER.device, UUID, fingerprint("POST", f"/api/cad/incident/{rig.open['id']}/note", {"text": "x"}))
    status, out, hdrs = rig.post(note_path(rig.open["id"]), {"text": "x"})
    assert status == 409 and out["error"]["reason"] == "in_progress" and hdrs["Retry-After"] == "1"
    assert rig.notes() == []


def test_a_write_that_raises_frees_its_key(rig, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(type(rig.cad.store), "note", boom)
    assert rig.post(note_path(rig.open["id"]), {"text": "x"})[0] == 500
    monkeypatch.undo()
    assert rig.post(note_path(rig.open["id"]), {"text": "x"})[0] == 201


def test_keys_are_purged_after_24_hours_and_bounded_per_principal(tmp_path):
    t = [1000.0]
    db = sqlite3.connect(tmp_path / "hub.sqlite", check_same_thread=False, isolation_level=None)
    idem = Idempotency(lambda: db, lambda: t[0])
    for i in range(MAX_PER_PRINCIPAL + 50):
        state, _ = idem.claim("dev-1", f"key-{i:08d}", "fp")
        assert state == "new"
        idem.finish("dev-1", f"key-{i:08d}", 200, "{}")
        t[0] += 0.001
    assert idem.count() == MAX_PER_PRINCIPAL                          # the oldest went first
    assert idem.claim("dev-1", f"key-{MAX_PER_PRINCIPAL + 49:08d}", "fp")[0] == "replay"
    assert idem.claim("dev-1", "key-00000000", "fp")[0] == "new"       # evicted: it would apply again (only after 1000 more)
    t[0] += KEEP_S + 120
    assert idem.claim("dev-2", "key-fresh-1", "fp")[0] == "new" and idem.count() == 1


def test_the_table_is_made_on_the_first_write_in_hub_sqlite_only(tmp_path):
    r = Rig(tmp_path)
    hub = tmp_path / "hub.sqlite"
    assert not hub.exists()                                                  # building the app and a refusal make nothing
    assert r.post(note_path(r.open["id"]), {"text": "x"}, key=None)[0] == 400
    assert r.post(note_path(r.open["id"]), {"text": "x"}, token="val")[0] == 403
    assert not hub.exists()
    assert r.post(note_path(r.open["id"]), {"text": "x"})[0] == 201
    names = {n for (n,) in sqlite3.connect(hub).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "v1_idempotency" in names
    assert not [n for (n,) in sqlite3.connect(tmp_path / "cad.sqlite").execute("SELECT name FROM sqlite_master")
                if "idempot" in n]


# -- If-Match ----------------------------------------------------------------------------------------------------

def test_if_match_the_current_rev_applies(rig):
    cur = writes.unit_obj(rig.unit("E60"))
    status, out, _ = rig.post(status_path(), {"status": "OL"}, headers={"If-Match": f'"{cur["rev"]}"'})
    assert status == 200 and out["unit"]["rev"] != cur["rev"]


def test_a_stale_if_match_is_a_412_with_the_current_unit(rig):
    stale = writes.unit_obj(rig.unit("E60"))["rev"]
    rig.cad.unit_status(uid(rig.cad, "E60"), {"status": "RS", "operator": "Lt. Ruiz"})      # moved on since the app read it
    status, out, _ = rig.post(status_path(), {"status": "OL"}, headers={"If-Match": stale})
    assert status == 412 and out["error"]["code"] == "precondition_failed"
    assert out["current"]["status"] == "RS" and out["current"]["rev"] == writes.unit_obj(rig.unit("E60"))["rev"]
    assert rig.unit("E60")["status"] == "RS"
    assert rig.post(status_path(), {"status": "OL"}, headers={"If-Match": out["current"]["rev"]}, key="c" * 12)[0] == 200


def test_if_match_is_optional_and_a_note_needs_none(rig):
    assert rig.post(status_path(), {"status": "OL"})[0] == 200
    assert rig.post(note_path(rig.open["id"]), {"text": "x"}, key="e" * 12, headers={"If-Match": "stale"})[0] == 201     # append-only


def test_the_rev_is_the_content_hash_exactly():
    assert rev_of({"b": 1, "a": "é"}) == rev_of({"a": "é", "b": 1})
    import hashlib
    assert rev_of({"code": "E60", "n": None}) == hashlib.sha256(b'{"code":"E60","n":null}').hexdigest()[:16]
    assert rev_of({"a": "é"}) == hashlib.sha256('{"a":"é"}'.encode()).hexdigest()[:16]     # no \u escapes


# -- queued actions and client_at ----------------------------------------------------------------------------------

def test_a_queued_status_on_a_unit_that_moved_on_is_a_409_not_applied(rig):
    rev = writes.unit_obj(rig.unit("E60"))["rev"]
    rig.cad.unit_status(uid(rig.cad, "E60"), {"status": "AV", "operator": "Lt. Ruiz"})
    status, out, _ = rig.post(status_path(), {"status": "OL", "client_at": rig.clock.t - 90}, headers={"If-Match": rev})
    assert status == 409 and out["error"]["reason"] == "unit_changed" and out["error"]["message"].startswith("not applied")
    assert out["current"]["status"] == "AV" and rig.unit("E60")["status"] == "AV"


def test_client_at_is_recorded_in_the_audit_and_the_answer(rig):
    at = rig.clock.t - 20
    status, out, _ = rig.post(status_path(), {"status": "OL", "client_at": at})
    assert status == 200 and out["client_at"] == at and out["client_at_clamped"] is False and out["received_at"] == rig.clock.t
    assert f"client_at={int(at)}" in rig.activity()[0]["ua"] and "clamped" not in rig.activity()[0]["ua"]
    assert not any("queued on the phone" in e["text"] for e in rig.cad.store.full(rig.open["id"])["events"])     # not late


def test_a_client_at_more_than_five_minutes_ahead_is_clamped_and_flagged(rig):
    ok = rig.post(status_path(), {"status": "OL", "client_at": rig.clock.t + 299})
    assert ok[1]["client_at_clamped"] is False and ok[1]["received_at"] == rig.clock.t
    status, out, _ = rig.post(note_path(rig.open["id"]), {"text": "clock is ahead", "client_at": rig.clock.t + 3600},
                           key="f" * 12)
    assert status == 201 and out["client_at_clamped"] is True and out["client_at"] == rig.clock.t + 3600
    row = rig.activity()[0]
    assert row["ua"].endswith("clamped") and row["at"] == rig.clock.t                  # ordered by receive time
    assert rig.notes()[-1] == "clock is ahead"                                          # never stamped in the future


def test_a_late_queued_note_says_when_it_was_written(rig):
    rig.post(note_path(rig.open["id"]), {"text": "Primary search complete", "client_at": rig.clock.t - 1800})
    assert rig.notes()[-1].startswith("Primary search complete [written ")
    rig.post(status_path("E63"), {"status": "OL", "client_at": rig.clock.t - 600}, key="d" * 12)
    assert any("E63 set to OL (queued on the phone at" in t for t in rig.notes())


@pytest.mark.parametrize("bad", ["yesterday", -1, True, float("inf"), [1]])
def test_a_bad_client_at_is_a_400(rig, bad):
    raw = json.dumps({"text": "x", "client_at": bad}).replace("Infinity", "1e999").encode()
    status, out, _ = rig.post(note_path(rig.open["id"]), raw=raw)
    assert status == 400 and out["error"]["code"] == "invalid"


@pytest.mark.parametrize("header, via, app", [
    ("SizeUp/1.4.2 (ios/18.1; 412)", "ios", "1.4.2"), ("SizeUp/2.0.0-rc1 (android/15; 88)", "android", "2.0.0-rc1"),
    ("SizeUp/1.0 (web/Safari; 1)", "web", "1.0"), ("curl/8.1", "api", "8.1"), ("", "api", "")])
def test_via_and_version_come_from_x_client(rig, header, via, app):
    rig.post(note_path(rig.open["id"]), {"text": "hi"}, headers={"X-Client": header})
    assert rig.activity()[0]["ua"].startswith(f"via={via} app={app or '-'} ")


# -- the server in front: body limits, the envelope, the fence ---------------------------------------------------------

def http(url, path, data, headers=None):
    req = urllib.request.Request(url + path, method="POST", data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def served(tmp_path):
    r = Rig(tmp_path)
    srv, url = serve(cad=r.api, role="hub", mobile=Switches(lambda: True, lambda: {"writes": True}),
                     mobile_resolver=r.v1.resolver)
    yield r, url
    srv.shutdown()


def test_over_the_wire_a_retry_replays_and_an_oversize_body_is_refused(served):
    r, url = served
    h = {"Authorization": "Bearer jordan", "Idempotency-Key": UUID, "Content-Type": "application/json"}
    body = json.dumps({"text": "Command established"}).encode()
    first, second = http(url, note_path(r.open["id"]), body, h), http(url, note_path(r.open["id"]), body, h)
    assert first[0] == second[0] == 201 and first[1] == second[1] and r.notes().count("Command established") == 1
    big = json.dumps({"text": "x" * 20000}).encode()
    status, out = http(url, note_path(r.open["id"]), big, {**h, "Idempotency-Key": "big-key-0001"})
    assert status == 413 and out["error"]["code"] == "body_too_large"
    assert http(url, note_path(r.open["id"]), body, {"Idempotency-Key": UUID})[0] == 401          # no device token
    assert http(url, note_path(r.open["id"]), b"[1]", {**h, "Idempotency-Key": "list-key-0001"}) == (
        400, {"error": {"code": "invalid", "message": "the body must be a JSON object"}})


def test_a_fenced_hub_answers_503_and_writes_nothing(served):
    r, url = served
    r.cad.store.may_write = lambda: False
    h = {"Authorization": "Bearer jordan", "Idempotency-Key": UUID}
    status, out = http(url, note_path(r.open["id"]), json.dumps({"text": "x"}).encode(), h)
    assert status == 503
    r.cad.store.may_write = lambda: True
    assert r.notes() == []


def test_fenced_the_key_store_refuses_before_the_cad_is_touched(rig):
    rig.cad.store.may_write = lambda: False
    with pytest.raises(FencedWrite):
        rig.post(note_path(rig.open["id"]), {"text": "x"})
    rig.cad.store.may_write = lambda: True
    assert rig.notes() == []


# -- the console is as it was --------------------------------------------------------------------------------------

def test_the_consoles_own_unit_status_post_is_unchanged_and_needs_no_key(rig):
    from sizeup.cloud.access import Identity
    code, raw, _ = rig.api.post(f"/api/cad/unit/{rig.unit('E60')['id']}/status", b'{"status":"OL"}', Identity(email=JORDAN))
    assert code == 200 and json.loads(raw)["status"] == "OL"
    code, raw, _ = rig.api.post(f"/api/cad/unit/{rig.unit('E60')['id']}/status", b'{"status":"AV"}', Identity(email=VAL))
    assert code == 403 and "error" in json.loads(raw)
