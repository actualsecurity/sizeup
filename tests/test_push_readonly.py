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


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-example")
    fake = FakeSns()
    cad = make_cad(tmp_path)
    people(cad)                                            # Casey admin, Jordan dispatcher, Val viewer, Dee deactivated
    pushes = notify.Pushes("http://127.0.0.1:9", "example-alerts-topic-0001", "example-status-topic-0002",
                           settings=lambda: {}, log=lambda m: None, retries=())
    env = {"SIZEUP_SNS_APNS_SANDBOX_ARN": f"arn:aws:sns:us-east-1:{ACCT}:app/APNS_SANDBOX/example-ios-dev",
           "SIZEUP_SNS_ENDPOINT": fake.url}
    switches = Switches(lambda: True, lambda: {"push": True})
    service = wire(pushes, tmp_path / "state", switches, cad.store, env=env, device_active=lambda d: True,
                   intel=cad.intel)
    v1 = V1(switches=switches, resolver=lambda h: Principal("dev-0001", CASEY), cad=cad)
    yield type("World", (), {"cad": cad, "fake": fake, "pushes": pushes, "service": service, "v1": v1,
                             "state": tmp_path / "state"})
    svc.install(None)
    fake.close()


def put(world, body: dict):
    raw = json.dumps(body).encode()
    return world.v1.respond("PUT", "/api/v1/devices/dev-0001/push", "", {}, "", lambda limit: raw)


def test_the_apps_whole_push_life_writes_nothing_to_the_cad(world):
    before = fingerprint(world.cad.store)
    assert len(before) > 10 and before["__rev__"] is not None           # the audit really saw the CAD's tables
    assert put(world, {"platform": "ios", "environment": "sandbox", "token": IOS_TOKEN})[0] == 200
    assert put(world, {"platform": "ios", "environment": "sandbox", "token": IOS_TOKEN,
                       "prefs": {"status": False}})[0] == 200                     # a replace
    for kind, title in ((notify.ALERTS, "MAYDAY"), (notify.STATUS, "Station silent 5 min")):
        world.pushes.notifier(kind).send(title, "heard: anything", 5, (), "", who={"units": [("E64", "")],
                                                                                    "incident": "26-000015"})
    assert world.service.flush(10)
    assert world.fake.published()                                               # a real push really went out
    for email in (CASEY, JORDAN, VAL, "dee@example.org", "stranger@example.org"):
        role_for(world.cad.store, email)
        world.service.role_of(email)
    for code in ("E64", "L31", "4821", "J. Doe"):
        unit_for(world.cad.store, code)
        world.service.unit_of(code)
    intel_labels(world.cad.intel)
    world.v1.respond("DELETE", "/api/v1/devices/dev-0001/push", "", {}, "", lambda limit: b"")
    put(world, {"platform": "ios", "environment": "sandbox", "token": IOS_TOKEN})
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


def test_the_push_code_never_calls_the_cads_writing_settings_methods():
    """role_for and unit_for are SELECTs (store.one), and nothing in sizeup/push calls Settings.person / standing, which
    add an operator for an unknown email."""
    import re
    from pathlib import Path
    for f in Path(__file__).resolve().parent.parent.joinpath("sizeup", "push").glob("*.py"):
        src = f.read_text()
        assert not re.search(r"\.(person|standing|settings_post|post)\(", src), f
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b[^\n]*\boperators\b", src, re.I), f
