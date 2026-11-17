"""/api/v1 writes (#562): a unit's status, an alarm acknowledged, a note on an incident; Idempotency-Key, If-Match,
queued
(offline) actions, the audit. Against a real CAD seeded the way tools/dev_seed.py seeds one (an invented call near the
map's home, its units, an emergency alarm in the radio log's shape). Every person and device is invented."""
from __future__ import annotations

import json
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.request

from pathlib import Path

import pytest

from sizeup.api.v1 import writes
from sizeup.api.v1.handler import V1
from sizeup.api.v1.idempotency import (KEEP_S, MAX_PER_PRINCIPAL, MAX_STORED_BODY, STALE_RUNNING_S, Idempotency,
                                       fingerprint)
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
        self.cad.dispatch(self.open["id"], {"unit_ids": [uid(self.cad, "E60"), uid(self.cad, "E63")],
                                            "operator": "Lt. Ruiz"})
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


def if_match(rig, code):
    return {"If-Match": writes.unit_obj(rig.unit(code))["rev"]}


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
    status, out, _ = rig.post(status_path("e63"),
                              {"status": "OL", "incident_id": rig.open["id"], "note": "at the hydrant"})
    assert status == 200 and out["unit"]["code"] == "E63" and out["unit"]["incident_number"] == rig.open["number"]


def test_alarm_ack_goes_through_the_console_and_is_audited(rig):
    aid = rig.alarm["id"]
    status, out, _ = rig.post(f"/api/v1/alarms/{aid}/ack", key="b" * 16)
    assert status == 200 and out["alarm"]["id"] == aid and out["alarm"]["ack_at"] is not None
    # the name: the console's operator string has the e-mail
    assert out["alarm"]["ack_by"] == "Jordan"
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
    for i, (path, body) in enumerate(((status_path(), {"status": "NOPE"}), (status_path(), {}),
                                      (status_path(), {"status": "OL", "incident_id": "x"}),
                                      (note_path(rig.open["id"]), {}),
                                      (note_path(rig.open["id"]), {"text": "x", "units": ["E99"]}),
                                      (note_path(rig.open["id"]), {"text": "x" * 2001}))):
        status, out, _ = rig.post(path, body, key=f"bad-input-{i:04d}")
        assert status == 400 and out["error"]["code"] == "invalid" and out["error"]["message"], (path, body)


# -- roles: the console's, never who=None ----------------------------------------------------------------------------

def test_a_viewer_is_refused_every_write_in_v1s_envelope(rig):
    before = rig.unit("E60")["status"], len(rig.cad.store.full(rig.open["id"])["events"])
    for path, body in ((status_path(), {"status": "OL"}), (f"/api/v1/alarms/{rig.alarm['id']}/ack", {}),
                       (note_path(rig.open["id"]), {"text": "hello"})):
        status, out, _ = rig.post(path, body, token="val")
        assert status == 403 and out["error"]["code"] == "forbidden"
        assert "Dispatchers and admins only" in out["error"]["message"]
    assert before == (rig.unit("E60")["status"], len(rig.cad.store.full(rig.open["id"])["events"]))
    assert rig.cad.alarms.get(rig.alarm["id"])["ack_at"] is None
    # in the viewer's activity, as on the console
    assert [r["ok"] for r in rig.activity("refused")] == [False] * 3
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
    req = writes.Req(type("C", (), {"gateway": rig.v1.gateway, "principal": Principal("d", "no-at-sign"),
                                    "headers": {}, "body": b"",
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
    assert needs("/api/cad/unit/3/status") == needs("/api/cad/alarm/3/ack") == "dispatch"
    assert needs("/api/cad/incident/3/note") == "dispatch"


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
    # the old answer, no rewrite
    assert again[0] == 200 and again[1] == first[1] and rig.unit("E60")["status"] == "AV"


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
        state, owner = idem.claim("dev-1", f"key-{i:08d}", "fp")
        assert state == "new"
        idem.finish("dev-1", f"key-{i:08d}", owner, 200, "{}")
        t[0] += 0.001
    assert idem.count() == MAX_PER_PRINCIPAL                          # the oldest went first
    assert idem.claim("dev-1", f"key-{MAX_PER_PRINCIPAL + 49:08d}", "fp")[0] == "replay"
    # evicted: it would apply again (only after 1000 more)
    assert idem.claim("dev-1", "key-00000000", "fp")[0] == "new"
    t[0] += KEEP_S + 120
    assert idem.claim("dev-2", "key-fresh-1", "fp")[0] == "new" and idem.count() == 1


def test_the_table_is_made_on_the_first_write_in_hub_sqlite_only(tmp_path):
    r = Rig(tmp_path)
    hub = tmp_path / "hub.sqlite"
    # building the app and a refusal make nothing
    assert not hub.exists()
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
    # moved on since the app read it
    rig.cad.unit_status(uid(rig.cad, "E60"), {"status": "RS", "operator": "Lt. Ruiz"})
    status, out, _ = rig.post(status_path(), {"status": "OL"}, headers={"If-Match": stale})
    assert status == 412 and out["error"]["code"] == "precondition_failed"
    assert out["current"]["status"] == "RS" and out["current"]["rev"] == writes.unit_obj(rig.unit("E60"))["rev"]
    assert rig.unit("E60")["status"] == "RS"
    assert rig.post(status_path(), {"status": "OL"}, headers={"If-Match": out["current"]["rev"]},
                    key="c" * 12)[0] == 200


def test_if_match_is_optional_and_a_note_needs_none(rig):
    assert rig.post(status_path(), {"status": "OL"})[0] == 200
    # append-only
    assert rig.post(note_path(rig.open["id"]), {"text": "x"}, key="e" * 12,
                    headers={"If-Match": "stale"})[0] == 201


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
    assert status == 409 and out["error"]["reason"] == "unit_changed"
    assert out["error"]["message"].startswith("not applied")
    assert out["current"]["status"] == "AV" and rig.unit("E60")["status"] == "AV"


def test_client_at_is_recorded_in_the_audit_and_the_answer(rig):
    at = rig.clock.t - 20
    status, out, _ = rig.post(status_path(), {"status": "OL", "client_at": at}, headers=if_match(rig, "E60"))
    assert status == 200 and out["client_at"] == at and out["client_at_clamped"] is False
    assert out["received_at"] == rig.clock.t
    assert f"client_at={int(at)}" in rig.activity()[0]["ua"] and "clamped" not in rig.activity()[0]["ua"]
    # not late
    assert not any("queued on the phone" in e["text"] for e in rig.cad.store.full(rig.open["id"])["events"])


def test_a_client_at_more_than_five_minutes_ahead_is_clamped_and_flagged(rig):
    ok = rig.post(status_path(), {"status": "OL", "client_at": rig.clock.t + 299}, headers=if_match(rig, "E60"))
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
    rig.post(status_path("E63"), {"status": "OL", "client_at": rig.clock.t - 600}, key="d" * 12,
             headers=if_match(rig, "E63"))
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
    path = f"/api/cad/unit/{rig.unit('E60')['id']}/status"
    code, raw, _ = rig.api.post(path, b'{"status":"OL"}', Identity(email=JORDAN))
    assert code == 200 and json.loads(raw)["status"] == "OL"
    code, raw, _ = rig.api.post(path, b'{"status":"AV"}', Identity(email=VAL))
    assert code == 403 and "error" in json.loads(raw)


# -- review round 1 ----------------------------------------------------------------------------------------------------

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.\w+")


def keys_stored(rig) -> int:
    return rig.v1.gateway.idempotency().count()


def test_no_email_address_is_in_any_write_response(rig):
    """The console's operator string is "Name <email>": the alarm's ack_by, its 409 and its replay say the name only."""
    aid = rig.alarm["id"]
    answers = [rig.post(f"/api/v1/alarms/{aid}/ack"),                                          # applied
               rig.post(f"/api/v1/alarms/{aid}/ack"),                                          # replayed
               # 409: already acknowledged
               rig.post(f"/api/v1/alarms/{aid}/ack", key="other-key-0001"),
               rig.post(note_path(rig.open["id"]), {"text": "x"}, key="other-key-0002"),
               rig.post(status_path(), {"status": "OL"}, key="other-key-0003"),
               rig.post(note_path(rig.shut["id"]), {"text": "x"}, key="other-key-0004"),       # 409: closed
               rig.post(status_path(), {"status": "NOPE"}, key="other-key-0005")]              # 400
    assert [a[0] for a in answers] == [200, 200, 409, 201, 200, 409, 400]
    assert answers[1][2]["Idempotent-Replayed"] == "true"
    for status, out, _ in answers:
        assert not EMAIL.search(json.dumps(out)), (status, out)
    assert answers[2][1]["error"]["message"] == "not applied: already acknowledged by Jordan"
    assert answers[2][1]["current"]["ack_by"] == "Jordan"


def test_a_deactivated_persons_refusal_does_not_name_the_admins(rig):
    rig.cad.settings_post("users", {"action": "deactivate", "id": rig.ids["Jordan"]})
    before = rig.unit("E60")["status"]
    status, out, _ = rig.post(status_path(), {"status": "OL"})
    assert status == 403 and not EMAIL.search(json.dumps(out)) and "deactivated" in out["error"]["message"].lower()
    assert rig.unit("E60")["status"] == before and keys_stored(rig) == 0 and rig.notes() == []


def test_a_handler_gets_narrow_methods_not_the_cad(rig):
    acting = rig.v1.gateway.acting(DISPATCHER)
    assert not any(hasattr(acting, n) for n in ("cad", "store", "api", "db", "sql"))
    assert rig.v1.gateway.acting(Principal("dev", "no-address")) is None
    assert acting.unit("e60")["name"] == "E60" and acting.unit("X9") is None
    assert acting.incident(rig.open["id"])["id"] == rig.open["id"] and acting.incident(999) is None
    assert acting.alarm(rig.alarm["id"])["id"] == rig.alarm["id"] and acting.alarm(None) is None
    src = (Path(writes.__file__)).read_text()
    assert "SELECT" not in src and ".store" not in src and "import sqlite3" not in src


def test_the_late_line_goes_through_the_consoles_note_with_a_row_of_its_own(rig):
    before = len(rig.activity())
    rig.post(status_path("E63"), {"status": "OL", "client_at": rig.clock.t - 600}, headers=if_match(rig, "E63"))
    rows = rig.activity()[: len(rig.activity()) - before]
    assert [r["route"].rsplit("/", 1)[-1] for r in rows] == ["note", "status"]           # the note, then the status
    assert all("device=dev-jordan-1" in r["ua"] and f"client_at={int(rig.clock.t - 600)}" in r["ua"] for r in rows)
    line = next(t for t in rig.notes() if "E63 set to OL (queued on the phone at" in t)
    assert "applied" in line


def test_a_queued_status_needs_if_match_and_nothing_is_applied_without_it(rig):
    status, out, _ = rig.post(status_path(), {"status": "OL", "client_at": rig.clock.t - 30})
    assert status == 428 and out["error"]["code"] == "precondition_required"
    assert rig.unit("E60")["status"] != "OL" and keys_stored(rig) == 0
    # not queued: If-Match stays optional; queued with the current rev: applied
    assert rig.post(status_path(), {"status": "OL"}, key="plain-key-0001")[0] == 200


def test_client_at_older_than_seven_days_or_zero_is_a_400(rig):
    for i, at in enumerate((0, 1, rig.clock.t - 7 * 86400 - 1)):
        status, out, _ = rig.post(note_path(rig.open["id"]), {"text": "x", "client_at": at}, key=f"old-at-{i:06d}")
        assert status == 400 and "7 days" in out["error"]["message"]
    recent = {"text": "x", "client_at": rig.clock.t - 6 * 86400}
    assert rig.post(note_path(rig.open["id"]), recent, key="ok-at-0001")[0] == 201
    assert not any("1970" in t for t in rig.notes())


def test_a_stale_running_claim_is_taken_over_and_the_slow_workers_finish_changes_nothing():
    t = [1000.0]
    db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    idem = Idempotency(lambda: db, lambda: t[0])
    state, old = idem.claim("dev-1", "key-stale-01", "fp")
    assert state == "new"
    assert idem.claim("dev-1", "key-stale-01", "fp")[0] == "busy"
    t[0] += STALE_RUNNING_S + 1                                              # the first worker died (or is very slow)
    state, new = idem.claim("dev-1", "key-stale-01", "fp")
    assert state == "new" and new != old
    idem.finish("dev-1", "key-stale-01", old, 201, '{"slow":true}')          # the slow original comes back
    idem.release("dev-1", "key-stale-01", old)
    assert idem.claim("dev-1", "key-stale-01", "fp")[0] == "busy"            # still the new claim's, still running
    idem.finish("dev-1", "key-stale-01", new, 200, '{"fast":true}')
    assert idem.claim("dev-1", "key-stale-01", "fp") == ("replay", (200, '{"fast":true}'))


def test_an_answer_over_16_kb_is_not_kept_and_its_key_is_free():
    db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    idem = Idempotency(lambda: db, time.time)
    _, owner = idem.claim("dev-1", "key-big-0001", "fp")
    idem.finish("dev-1", "key-big-0001", owner, 200, "x" * (MAX_STORED_BODY + 1))
    assert idem.count() == 0 and idem.claim("dev-1", "key-big-0001", "fp")[0] == "new"
    _, owner = idem.claim("dev-1", "key-big-0002", "fp")
    idem.finish("dev-1", "key-big-0002", owner, 200, "x" * MAX_STORED_BODY)
    assert idem.claim("dev-1", "key-big-0002", "fp")[0] == "replay"


def test_a_refusal_before_the_write_leaves_no_key_and_the_key_works_afterwards(rig):
    cases = ((status_path("X99"), {"status": "OL"}, "dispatcher"), (note_path(999), {"text": "x"}, "dispatcher"),
             ("/api/v1/alarms/999/ack", {}, "dispatcher"), (note_path(rig.open["id"]), {"text": "x"}, "viewer"))
    for path, body, who in cases:
        status, _, _ = rig.post(path, body, token="val" if who == "viewer" else "jordan")
        assert status in (403, 404)
    assert keys_stored(rig) == 0
    assert rig.post(note_path(rig.open["id"]), {"text": "x"})[0] == 201          # the same key, now a real request


@pytest.mark.parametrize("header", [
    "SizeUp/1.0\nclient_at=1 (ios/18; 1)", "SizeUp/1.0 client_at=5 clamped (ios/18; 1)",
    "SizeUp/" + "9" * 500 + " (ios/1)",
    "SizeUp/1.0\r\nX-Forged: 1 (android/15)", "SizeUp/1.0=client_at=7 (ios/1)", "‮SizeUp/1éé (ios/1)"])
def test_x_client_cannot_forge_or_break_the_audit_tag(rig, header):
    rig.v1.resolver = lambda h: Principal("dev-" + "z" * 300 + "\n client_at=9", JORDAN)
    rig.post(note_path(rig.open["id"]), {"text": "hi"}, headers={"X-Client": header})
    ua = rig.activity()[0]["ua"]
    assert "\n" not in ua and "\r" not in ua and len(ua) <= 120 and ua.isascii()
    assert "client_at=" not in ua and ua.count("via=") == 1 and ua.count("device=") == 1 and ua.startswith("via=")


def test_the_ack_race_is_a_409_not_a_400(rig, monkeypatch):
    real = CadApi.post

    def lose_the_race(self, path, raw, who=None, client=("", "")):
        if path.endswith("/ack"):
            rig.cad.alarm_ack(rig.alarm["id"], {"operator": "Lt. Ruiz"})       # the other dispatcher, just now
            return 400, b'{"error":"alarm 1 was already acknowledged"}', "application/json"
        return real(self, path, raw, who, client)
    monkeypatch.setattr(CadApi, "post", lose_the_race)
    status, out, _ = rig.post(f"/api/v1/alarms/{rig.alarm['id']}/ack")
    assert status == 409 and out["error"]["reason"] == "already_acknowledged" and out["current"]["ack_by"] == "Lt. Ruiz"


def test_client_at_nan_and_the_300_second_boundary(rig):
    status, out, _ = rig.post(note_path(rig.open["id"]), raw=b'{"text": "x", "client_at": NaN}')
    assert status == 400 and out["error"]["code"] == "invalid"
    at300 = rig.post(note_path(rig.open["id"]), {"text": "a", "client_at": rig.clock.t + 300}, key="edge-300-0001")
    at301 = rig.post(note_path(rig.open["id"]), {"text": "b", "client_at": rig.clock.t + 301}, key="edge-301-0001")
    assert at300[1]["client_at_clamped"] is False and at301[1]["client_at_clamped"] is True


@pytest.mark.parametrize("key, ok", [("k" * 7, False), ("k" * 8, True), ("k" * 128, True), ("k" * 129, False)])
def test_key_length_bounds(rig, key, ok):
    status, out, _ = rig.post(note_path(rig.open["id"]), {"text": "x"}, key=key)
    assert (status == 201) is ok and (ok or out["error"]["code"] == "invalid_idempotency_key")


def test_the_master_switch_off_is_a_404_and_nothing_is_written(rig):
    rig.v1.switches = Switches(lambda: False, lambda: {"writes": True})
    for path in (status_path(), note_path(rig.open["id"]), f"/api/v1/alarms/{rig.alarm['id']}/ack"):
        status, out, _ = rig.post(path, {"status": "OL", "text": "x"})
        assert (status, out["error"]["code"]) == (404, "not_found")
    assert rig.unit("E60")["status"] != "OL" and rig.notes() == []


# -- the body hook, over the wire (handler.py, server.py _v1_body) ----------------------------------------------------

def raw_http(url, request: bytes, wait: float = 3.0) -> bytes:
    host, port = url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=wait) as s:
        s.sendall(request)
        out = b""
        try:
            while chunk := s.recv(65536):
                out += chunk
        except OSError:
            pass
        return out


def head(rig_url, extra: str, body: bytes = b"", path: str | None = None, method="POST", rig_=None) -> bytes:
    return (f"{method} {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer jordan\r\nIdempotency-Key: {UUID}\r\n"
            f"Connection: close\r\n{extra}\r\n").encode() + body


def test_a_post_with_no_length_or_a_chunked_body_is_a_411_and_applies_nothing(served):
    r, url = served
    path = note_path(r.open["id"])
    for extra, body in (("", b""), ("Transfer-Encoding: chunked\r\n", b'5\r\nhello\r\n0\r\n\r\n'),
                        ("Transfer-Encoding: chunked\r\nContent-Length: 20\r\n", b'{"text":"smuggled"}')):
        t0 = time.monotonic()
        out = raw_http(url, head(url, extra, body, path).replace(b"Connection: close\r\n", b""))
        assert out.startswith(b"HTTP/1.1 411") and b'"length_required"' in out
        assert time.monotonic() - t0 < 2.5                          # the server closed the connection itself
    assert r.notes() == [] and keys_stored(r) == 0


def test_a_body_that_never_arrives_is_a_408_and_holds_no_slot(served, monkeypatch):
    from sizeup.web import server as web_server
    r, url = served
    monkeypatch.setattr(web_server, "V1_BODY_TIMEOUT_S", 0.4)
    t0 = time.monotonic()
    out = raw_http(url, head(url, "Content-Length: 50\r\n", b'{"text":', note_path(r.open["id"])), wait=5)
    assert out.startswith(b"HTTP/1.1 408") and b'"request_timeout"' in out and time.monotonic() - t0 < 4
    stall = head(url, "Content-Length: 50\r\n", b"", note_path(r.open["id"])).replace(b"Connection: close", b"X: 1")
    short = raw_http(url, stall, wait=2)
    assert short.startswith(b"HTTP/1.1 408")
    assert r.notes() == [] and r.v1.limits._busy == 0                      # the concurrency slot is back


def test_a_kept_alive_connection_is_not_desynced_by_a_v1_post(served):
    import http.client
    r, url = served
    conn = http.client.HTTPConnection(url.removeprefix("http://"), timeout=5)
    h = {"Authorization": "Bearer jordan", "Content-Type": "application/json"}
    for i in range(3):
        conn.request("POST", note_path(r.open["id"]), json.dumps({"text": f"n{i}"}),
                     {**h, "Idempotency-Key": f"keep-alive-{i:04d}"})
        resp = conn.getresponse()
        assert resp.status == 201, resp.read()
        resp.read()
    assert r.notes()[-3:] == ["n0", "n1", "n2"]
    conn.close()


@pytest.fixture
def hook_server(monkeypatch):
    """A hub whose v1 has PUT and DELETE routes that echo their body: the hook push and the others reuse."""
    from sizeup.api.v1 import handler
    from sizeup.api.v1.routes import Route

    def echo(ctx):
        return 200, {"len": len(ctx.body), "body": ctx.body.decode("utf-8", "replace")}
    def route(method, path):
        return Route(method, path, echo, "device", "writes", "dispatch")
    monkeypatch.setattr(handler, "ROUTES", [route("PUT", "/api/v1/things/{id}"), route("DELETE", "/api/v1/things/{id}"),
                                            route("POST", "/api/v1/things")])
    srv, url = serve(role="hub", mobile=Switches(lambda: True, lambda: {"writes": True}),
                     mobile_resolver=lambda h: DISPATCHER if h.get("Authorization") == "Bearer jordan" else None)
    yield url
    srv.shutdown()


def test_put_and_delete_use_the_same_body_hook(hook_server):
    import http.client
    conn = http.client.HTTPConnection(hook_server.removeprefix("http://"), timeout=5)
    h = {"Authorization": "Bearer jordan"}
    conn.request("PUT", "/api/v1/things/1", '{"a":1}', h)
    resp = conn.getresponse()
    assert (resp.status, json.loads(resp.read())) == (200, {"len": 7, "body": '{"a":1}'})
    conn.request("DELETE", "/api/v1/things/1", headers=h)                   # no body at all: fine, same connection
    resp = conn.getresponse()
    assert (resp.status, json.loads(resp.read())["len"]) == (200, 0)
    conn.request("PUT", "/api/v1/things/1", "x" * 20000, h)                  # over 16 KB: 413 for every verb
    resp = conn.getresponse()
    assert resp.status == 413 and json.loads(resp.read())["error"]["code"] == "body_too_large"
    conn.close()
    out = raw_http(hook_server, b"PUT /api/v1/things/1 HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer jordan\r\n"
                                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 411")
    out = raw_http(hook_server, b"PUT /api/v1/things/1 HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer nobody\r\n"
                                b"Content-Length: 3\r\nConnection: close\r\n\r\nabc")
    assert out.startswith(b"HTTP/1.1 401")


def test_a_fenced_write_answers_the_envelope_and_leaves_no_key(served):
    r, url = served
    r.cad.store.may_write = lambda: False
    out = raw_http(url, head(url, "Content-Length: 13\r\n", b'{"text": "x"}\n'[:13], note_path(r.open["id"])))
    r.cad.store.may_write = lambda: True
    assert out.startswith(b"HTTP/1.1 503") and b'"error"' in out and b'"code"' in out
    assert keys_stored(r) == 0 and r.notes() == []
