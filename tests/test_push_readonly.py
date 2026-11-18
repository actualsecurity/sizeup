"""The app is read-only situational awareness (owner direction): nothing in push writes the CAD. Its own bookkeeping is the
one allowed write: `push_devices` in hub.sqlite. This fingerprints EVERY table of the real cad.sqlite (and the store's
revision) before and after the app's whole push life: register, replace, a real alert through the real
`Pushes.notifier(kind).send` into a fake SNS, the role and unit lookups of the live wiring, unregister and the revoke hook.
Invented values."""
from __future__ import annotations

import hashlib
import json

import pytest

from sizeup import notify
from sizeup.api.v1.handler import V1
from sizeup.api.v1.principal import Principal
from sizeup.api.v1.switches import Switches
from sizeup.push import service as svc
from sizeup.push.wire import intel_labels, role_for, unit_for, wire
from tests.test_cad import make_cad
from tests.test_push_sns import ACCT, FakeSns, IOS_TOKEN
from tests.test_roles import CASEY, JORDAN, VAL, people


def fingerprint(store) -> dict:
    """{table: sha256 of every row in rowid order} for every table of cad.sqlite, plus the store's revision counter."""
    out = {}
    for t in store.q("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name = t["name"]
        try:
            rows = store.q(f'SELECT * FROM "{name}" ORDER BY rowid')
        except Exception:  # noqa: BLE001 - a WITHOUT ROWID table
            rows = store.q(f'SELECT * FROM "{name}"')
        out[name] = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    out["__rev__"] = store.rev
    return out


@pytest.fixture(params=["wired", "unwired", "revoked"])
def world(request, tmp_path, monkeypatch):
    """The production wiring over a real CAD, a fake SNS and three device_active cases: a source that says yes, none wired
    (the branch that logs its warning) and one that says the device is revoked."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-example")
    fake = FakeSns()
    cad = make_cad(tmp_path)
    people(cad)                                            # Casey admin, Jordan dispatcher, Val viewer, Dee deactivated
    logs: list[str] = []
    pushes = notify.Pushes("http://127.0.0.1:9", "example-alerts-topic-0001", "example-status-topic-0002",
                           settings=lambda: {"devices": {"dispatch": {"on": True}}}, log=lambda m: None, retries=())
    env = {"SIZEUP_SNS_APNS_SANDBOX_ARN": f"arn:aws:sns:us-east-1:{ACCT}:app/APNS_SANDBOX/example-ios-dev",
           "SIZEUP_SNS_ENDPOINT": fake.url}
    switches = Switches(lambda: True, lambda: {"push": True})
    active = {"wired": lambda d: True, "unwired": None, "revoked": lambda d: False}[request.param]
    service = wire(pushes, tmp_path / "state", switches, cad.store, env=env, device_active=active, intel=cad.intel,
                   log=logs.append)
    v1 = V1(switches=switches, resolver=lambda h: Principal("dev-0001", CASEY), cad=cad)
    yield type("World", (), {"cad": cad, "fake": fake, "pushes": pushes, "service": service, "v1": v1, "logs": logs,
                             "mode": request.param, "state": tmp_path / "state"})
    svc.install(None)
    fake.close()


def put(world, body: dict):
    raw = json.dumps(body).encode()
    return world.v1.respond("PUT", "/api/v1/devices/dev-0001/push", "", {}, "", lambda limit: raw)


def intel_title(cad) -> str:
    """A real intel push title: a loaded pack's label, one of its categories' labels and a town of the county list."""
    pack = cad.intel.packs[0]
    cat = next(c["label"] for c in pack.categories.values() if c.get("label"))
    return f"{pack.label} \u00b7 {cat} \u00b7 Port Chester"


def test_the_apps_whole_push_life_writes_nothing_to_the_cad(world):
    before = fingerprint(world.cad.store)
    assert len(before) > 10 and before["__rev__"] is not None           # the audit really saw the CAD's tables
    reg = {"platform": "ios", "environment": "sandbox", "token": IOS_TOKEN, "prefs": {"dispatch": True}}
    assert put(world, reg)[0] == 200
    assert put(world, {**reg, "prefs": {"dispatch": True, "status": True}})[0] == 200          # a replace
    who = {"units": [("E64", "")], "incident": "26-000015", "address": "100 Main St"}
    world.pushes.notifier(notify.ALERTS).send("MAYDAY", "heard: anything", 5, (), "", who=who)
    world.pushes.notifier(notify.STATUS).send("Station silent 5 min", "x", 3, (), "", who=who)
    world.pushes.notifier(notify.INTEL).send(intel_title(world.cad), "100 Example Ave \u00b7 14:32 \u00b7 high", 4, (), "")
    world.pushes.fan_out("dispatch", "Structure fire", "x", 3, who, "", "inc-1")       # policy on: the builders run
    assert world.service.flush(10)
    if world.mode == "revoked":
        assert world.fake.published() == []                             # a revoked device gets nothing at all
    else:
        kinds = {p["message"]["APNS_SANDBOX"]["kind"] for p in world.fake.published()}
        assert kinds == {"alerts", "status", "intel", "dispatch"}       # every kind's builder really ran
    assert (sum("device_active not wired" in m for m in world.logs) >= 1) is (world.mode == "unwired")
    for email in (CASEY, JORDAN, VAL, "dee@example.org", "stranger@example.org"):
        role_for(world.cad.store, email)
        world.service.role_of(email)
    for code in ("E64", "L31", "4821", "J. Doe"):
        unit_for(world.cad.store, code)
        world.service.unit_of(code)
    intel_labels(world.cad.intel)
    world.v1.respond("DELETE", "/api/v1/devices/dev-0001/push", "", {}, "", lambda limit: b"")
    put(world, reg)
    assert svc.drop_device("dev-0001") is True                                  # the revoke hook
    assert fingerprint(world.cad.store) == before                               # not one CAD table, not the revision


def test_the_only_thing_push_writes_is_its_own_table_in_hub_sqlite(world):
    import sqlite3
    put(world, {"platform": "ios", "environment": "sandbox", "token": IOS_TOKEN})
    hub = sqlite3.connect(world.state / "hub.sqlite")
    assert hub.execute("SELECT count(*) FROM push_devices").fetchone()[0] == 1
    assert {r[0] for r in hub.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == {"push_devices"}
    hub.close()
    assert "push_devices" not in fingerprint(world.cad.store)


def test_a_person_unknown_to_the_cad_is_not_added_by_a_push_or_a_lookup(world):
    before = fingerprint(world.cad.store)
    assert role_for(world.cad.store, "newcomer@example.org") is None
    assert world.service.role_of("newcomer@example.org") is None
    assert fingerprint(world.cad.store) == before                             # no operators row appeared (person() would)


def test_the_push_code_never_calls_the_cads_writing_methods_or_writes_another_table():
    """Source backstop over sizeup/push/*.py and sizeup/api/v1/push.py: no Settings.person / standing / settings_post, no
    store.tx(, and every INSERT / UPDATE / DELETE / CREATE / ALTER / DROP names push_devices (the TABLE constant) only."""
    import re
    from pathlib import Path
    from sizeup.push.devices import TABLE
    assert TABLE == "push_devices"
    root = Path(__file__).resolve().parent.parent
    files = [*root.joinpath("sizeup", "push").glob("*.py"), root / "sizeup" / "api" / "v1" / "push.py"]
    assert len(files) >= 7
    forbidden = re.compile(r"\.(person|standing|settings_post|post)\(|settings\.person|store\.tx\(|\.tx\(")
    sql = re.compile(r"\b(INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM|CREATE TABLE(?: IF NOT EXISTS)?|ALTER TABLE|DROP TABLE)"
                     r"\s+(\{?\w+\}?)")
    for f in files:
        src = f.read_text()
        assert not forbidden.search(src), f
        for m in sql.finditer(src):
            assert m.group(2) in ("{TABLE}", "push_devices"), (f.name, m.group(0))
