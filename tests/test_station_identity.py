"""Station identity on the hub (multi-station design A1, sizeup/cloud/stations.py): each station's own token, the shared
token as one station, the principal every /api/station/ route resolves, the payload's id checked against it, admin
management (Settings > Station > Stations and the CLI) and the station's side of a refusal.

Every id, name and token here is made up."""

from __future__ import annotations

import gzip
import hmac
import json
import re
import sqlite3
import threading
import urllib.request
from pathlib import Path

import pytest

from sizeup.cad.api import ADMIN_READS, CadApi, needs
from sizeup.cloud import hub as hub_mod
from sizeup.cloud import stations as st_mod
from sizeup.cloud.access import Identity
from sizeup.cloud.client import HubClient, HubError
from sizeup.cloud.health import status_token
from sizeup.cloud.hubstore import MonitorDb
from sizeup.cloud.stations import Principal, Refused, StationError, StationRegistry
from sizeup.web.server import WebServer
from tests.test_cad import make_cad
from tests.test_cloud import TOKEN, HubRun, call, hub_cfg
from tests.test_roles import CASEY, JORDAN, people
from tests.test_station_logs import GZ, T0, FakeLogs, sink

ROOT = Path(__file__).resolve().parent.parent
SHARED = "shared-station-token-0123456789"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def registry(tmp_path, shared=SHARED, **kw):
    clock = Clock()
    logs: list[str] = []
    reg = StationRegistry(MonitorDb(tmp_path / "hub.sqlite"), shared=shared, clock=clock, log=logs.append, **kw)
    return reg, clock, logs


def refused(fn, *a) -> str:
    with pytest.raises(Refused) as e:
        fn(*a)
    return e.value.reason


# -- tokens ----------------------------------------------------------------------------------------------------------

def test_a_token_is_issued_once_and_only_its_hash_is_kept(tmp_path):
    reg, _, _ = registry(tmp_path)
    tok = reg.add("st-north", "North", by="casey")
    assert re.fullmatch(r"sz1\.st-north\.[A-Za-z0-9_-]{43}", tok)
    assert reg.resolve(tok) == Principal("st-north", "own")
    raw = (tmp_path / "hub.sqlite").read_bytes() + (Path(f"{tmp_path / 'hub.sqlite'}-wal").read_bytes()
                                                    if Path(f"{tmp_path / 'hub.sqlite'}-wal").exists() else b"")
    secret = tok.split(".")[2]
    assert secret.encode() not in raw and tok.encode() not in raw
    assert status_token(tok).encode() not in raw
    row = reg.row("st-north")
    assert row["hash"] == st_mod.digest(tok) and row["status_hash"] == st_mod.digest(status_token(tok))
    with pytest.raises(StationError):
        reg.add("st-north")                          # exists: Rotate
    with pytest.raises(StationError):
        reg.add("North Station")                     # not an id
    assert reg.add("st-south") != reg.add("st-east")


def test_unknown_wrong_secret_and_malformed_tokens_are_refused_alike(tmp_path):
    reg, _, _ = registry(tmp_path)
    tok = reg.add("st-north")
    assert refused(reg.resolve, "") == "no_token"
    bad = tok[:-1] + ("A" if tok[-1] != "A" else "B")
    for t in (bad, "sz1.st-nobody." + "A" * 43, "sz1.st-north.short", "sz1..x", "nonsense-nonsense", SHARED + "x"):
        assert refused(reg.resolve, t) == "token_unknown", t
    reg.revoke("st-north")
    assert refused(reg.resolve, tok) == "token_revoked"
    assert refused(reg.resolve, bad) == "token_unknown"        # a guess never learns that st-north is revoked


def test_the_check_is_constant_time_for_known_and_unknown_ids(tmp_path, monkeypatch):
    """Every own-token check makes the same compare_digest calls (an unknown id against a dummy hash), so the time taken
    says neither whether the id exists nor how much of the secret matched."""
    reg, _, _ = registry(tmp_path)
    tok = reg.add("st-north")
    seen: list[tuple[int, int]] = []
    real = hmac.compare_digest
    monkeypatch.setattr(st_mod.hmac, "compare_digest", lambda a, b: seen.append((len(a), len(b))) or real(a, b))
    counts = []
    for t in (tok, tok[:-1] + "x", "sz1.st-nobody." + "A" * 43):
        seen.clear()
        try:
            reg.resolve(t)
        except Refused:
            pass
        counts.append(list(seen))
    assert counts[0] == counts[1] == counts[2] == [(64, 64), (64, 64)]


def test_rotate_keeps_the_old_token_for_the_grace_then_refuses_it(tmp_path):
    reg, clock, _ = registry(tmp_path)
    old = reg.add("st-north")
    new = reg.rotate("st-north", by="casey")
    assert new != old and reg.resolve(new).id == "st-north" and reg.resolve(old).id == "st-north"
    assert reg.resolve(status_token(old), read_only=True).id == "st-north"
    clock.t += st_mod.ROTATE_GRACE_S + 1
    assert refused(reg.resolve, old) == "token_unknown"
    assert refused(reg.resolve, status_token(old), True) == "token_unknown"
    assert reg.resolve(new).id == "st-north"


def test_revoke_is_immediate_for_the_token_its_previous_one_and_its_status_token(tmp_path):
    reg, _, _ = registry(tmp_path)
    old = reg.add("st-north")
    new = reg.rotate("st-north")
    reg.revoke("st-north", by="casey")
    for t in (new, old):
        assert refused(reg.resolve, t) in ("token_revoked", "token_unknown")
    assert refused(reg.resolve, new) == "token_revoked"
    assert refused(reg.resolve, status_token(new), True) == "token_revoked"
    with pytest.raises(StationError):
        reg.revoke("st-north")
    again = reg.rotate("st-north")                    # a new token: active again, the revoked ones never
    assert reg.resolve(again).id == "st-north"
    assert refused(reg.resolve, new) == "token_unknown" and refused(reg.resolve, old) == "token_unknown"


# -- the shared token --------------------------------------------------------------------------------------------------

def test_the_shared_token_is_one_station_the_first_id_it_posts_as(tmp_path):
    reg, _, logs = registry(tmp_path)
    p = reg.resolve(SHARED)
    assert p == Principal("", "shared")
    assert reg.claim(p, None) == ""                   # an old station's post with no id: as the shared station
    assert reg.claim(p, "pcfd-x") == "pcfd-x"         # the first id: pinned (and kept in hub.sqlite)
    assert any("pcfd-x" in m for m in logs)
    p = reg.resolve(SHARED)
    assert p.id == "pcfd-x" and reg.claim(p, "pcfd-x") == "pcfd-x" and reg.claim(p, "") == "pcfd-x"
    assert refused(reg.claim, p, "st-new") == "id_mismatch"     # a new station can't use the shared token
    again = StationRegistry(reg.mdb, shared=SHARED)               # a restart: still that station
    assert again.resolve(SHARED).id == "pcfd-x"
    assert refused(again.claim, again.resolve(SHARED), "st-new") == "id_mismatch"


def test_the_shared_token_station_from_config_and_an_own_token_never_claims_another_id(tmp_path):
    reg, _, _ = registry(tmp_path, legacy_id="st-hq")
    p = reg.resolve(SHARED)
    assert p.id == "st-hq" and reg.claim(p, "st-hq") == "st-hq"
    assert refused(reg.claim, p, "st-other") == "id_mismatch"
    tok = reg.add("st-b")
    own = reg.resolve(tok)
    assert reg.claim(own, "st-b") == "st-b" and reg.claim(own, None) == "st-b"
    assert refused(reg.claim, own, "st-hq") == "id_mismatch"


def test_two_first_posts_racing_on_the_shared_token_pin_one_id_and_refuse_the_other(tmp_path):
    """Both resolved before either pinned (no id yet): the second to claim must not get through as the first's id."""
    reg, _, _ = registry(tmp_path)
    p1, p2 = reg.resolve(SHARED), reg.resolve(SHARED)
    assert p1.id == p2.id == ""
    assert reg.claim(p1, "st-a") == "st-a"
    assert refused(reg.claim, p2, "st-b") == "id_mismatch"
    assert reg.claim(p2, "st-a") == "st-a" and reg.legacy_id() == "st-a"


def test_the_shared_token_station_can_be_fixed_without_a_deploy(tmp_path):
    """A wrong [cloud] legacy_station_id (or a wrongly learned id): set the right one (it outranks the config), or clear
    it (back to the config's, or learned again)."""
    reg, _, _ = registry(tmp_path, legacy_id="st-wrong")
    p = reg.resolve(SHARED)
    assert refused(reg.claim, p, "st-hq") == "id_mismatch"
    assert reg.set_shared_id("st-hq", "casey") == ("st-wrong", "st-hq")
    assert reg.claim(reg.resolve(SHARED), "st-hq") == "st-hq"
    assert StationRegistry(reg.mdb, shared=SHARED, legacy_id="st-wrong").legacy_id() == "st-hq"   # a restart: kept
    assert reg.set_shared_id("", "casey") == ("st-hq", "st-wrong")                                  # back to the config's
    learn, _, _ = registry(tmp_path / "b")
    learn.claim(learn.resolve(SHARED), "st-typo")
    learn.set_shared_id("", "casey")                                  # cleared: learned again from the next post
    assert learn.legacy_id() == "" and learn.claim(learn.resolve(SHARED), "st-hq") == "st-hq"
    with pytest.raises(StationError):
        reg.set_shared_id("x" * 65)


def test_retiring_the_shared_token_refuses_it_and_its_derived_status_token(tmp_path):
    reg, clock, _ = registry(tmp_path, legacy_id="st-hq")
    assert reg.resolve(status_token(SHARED), read_only=True) == Principal("st-hq", "status", True)
    reg.touch(reg.resolve(SHARED))
    with pytest.raises(StationError, match="used 0 s ago"):
        reg.retire_shared("casey")                    # a station still runs on it
    clock.t += st_mod.SHARED_BUSY_S + 1
    reg.retire_shared("casey")
    assert refused(reg.resolve, SHARED) == "token_retired"
    assert refused(reg.resolve, status_token(SHARED), True) == "token_retired"
    reg.restore_shared("casey")
    assert reg.resolve(SHARED).id == "st-hq"
    reg.retire_shared("casey", force=True)
    # a configured status token is its own secret: it outlives the shared token
    cfgd = StationRegistry(reg.mdb, shared=SHARED, configured_status="configured-status-0123456789", legacy_id="st-hq")
    assert cfgd.resolve("configured-status-0123456789", read_only=True).id == "st-hq"
    assert refused(cfgd.resolve, status_token(SHARED), True) == "token_unknown"


def test_a_status_token_reads_status_only(tmp_path):
    reg, _, _ = registry(tmp_path)
    tok = reg.add("st-north")
    assert reg.resolve(status_token(tok), read_only=True) == Principal("st-north", "status", True)
    assert refused(reg.resolve, status_token(tok)) == "read_only"
    assert reg.resolve(tok, read_only=True).id == "st-north"        # the station's own token reads it too


# -- every route, over HTTP --------------------------------------------------------------------------------------------

def test_every_station_route_is_listed_and_resolves_the_principal():
    """STATION_ROUTES is every path Hub.handle serves: a new route has to be listed, and the test below then checks it
    refuses what it should."""
    src = Path(hub_mod.__file__).read_text()
    served = set(re.findall(r'path == "(/api/station/[a-z/]+)"', src))
    listed = {p for _, p in hub_mod.STATION_ROUTES}
    assert served | {"/api/station/jobs/0123456789abcdef"} == listed
    assert "JOB_PATH.fullmatch(path)" in src
    handle = src.split("    def handle(")[1].split("    def _jobs(")[0]
    first_route = min(handle.index(f'"{p}"') for p in served if p != "/api/station/status")
    assert handle.index("self._principal(headers)") < first_route


def test_every_route_refuses_no_unknown_revoked_and_retired_tokens_and_takes_an_own_one(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="t"))
    try:
        reg = hub.app.hub.stations
        good = reg.add("t", "Test")                    # the existing station's own token (id "t", as it posts)
        gone = reg.add("st-gone")
        reg.revoke("st-gone")
        for method, path in hub_mod.STATION_ROUTES:
            body = {} if method == "POST" else None
            url = hub.url + path + ("?wait=0" if path.endswith("/jobs") else "")
            for tok, reason in ((None, "no_token"), ("sz1.t." + "A" * 43, "token_unknown"), (gone, "token_revoked")):
                code, out = call(url, method, body, token=tok)
                assert code == 401 and out["reason"] == reason, (path, tok, code, out)
                assert (tok or "@@") not in json.dumps(out)
            if path != "/api/station/status":
                code, out = call(url, method, body, token=status_token(good))
                assert code == 401 and out["reason"] == "read_only", path
            code, out = call(url, method, body, token=good)
            assert code != 401, (path, code, out)
            code, out = call(url, method, body, token=TOKEN)          # the shared token, as station "t"
            assert code != 401, (path, code, out)
        assert call(hub.url + "/api/station/status", token=status_token(good))[0] == 200
        assert call(hub.url + "/api/station/hello", token=good)[1] == {"ok": True, "station": "t", "token": "own"}
        reg.retire_shared("admin", force=True)
        code, out = call(hub.url + "/api/station/jobs?wait=0", token=TOKEN)
        assert code == 401 and out["reason"] == "token_retired"
        assert call(hub.url + "/api/station/jobs?wait=0", token=good)[0] == 200    # the station on its own token
    finally:
        hub.close()


def test_a_payload_naming_another_station_is_refused_and_logged_once_without_the_token(tmp_path):
    logs: list[str] = []
    hub = HubRun(hub_cfg(tmp_path))
    hub.app.hub.log = logs.append
    try:
        tok = hub.app.hub.stations.add("st-b")
        hub.app.hub.stations.log = logs.append
        for path, body in (("/api/station/events", {"epoch": "e1", "station": "t", "events": []}),
                           ("/api/station/state", {"at": 1.0, "status": {"id": "t", "name": "Test"}})):
            for _ in range(3):
                code, out = call(hub.url + path, "POST", body, token=tok)
                assert code == 401 and out["reason"] == "id_mismatch", out
                assert "st-b" in out["error"] and tok.split(".")[2] not in out["error"]
        said = [m for m in logs if "refused" in m]
        assert said == ["hub: station request refused: id_mismatch (station st-b)"]
        assert all(tok.split(".")[2] not in m for m in logs)
        # its own id, or none (an older station): taken, and Health sees the authenticated id
        assert call(hub.url + "/api/station/state", "POST", {"at": 1.0, "status": {"id": "st-b"}}, token=tok)[0] == 200
        assert call(hub.url + "/api/station/state", "POST", {"at": 1.0, "status": {"name": "x"}}, token=tok)[0] == 200
        assert hub.app.hub.status["id"] == "st-b"
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": []}, token=tok)[0] == 200
    finally:
        hub.close()


def test_a_second_station_is_refused_until_multi_station_and_said_on_health(tmp_path):
    """One outbox cursor, one status, one job queue until design A2-A4: a second station's posts would corrupt them, so
    they are refused (409 multi_station_off); its hello, status and logs (its own stream) still go through."""
    logs: list[str] = []
    hub = HubRun(hub_cfg(tmp_path, cloud_legacy_station_id="t"))
    hub.app.hub.log = logs.append
    try:
        assert call(hub.url + "/api/station/state", "POST", {"at": 1.0, "status": {"id": "t", "name": "Test"}})[0] == 200
        seen = hub.app.hub.last_seen
        own = hub.app.hub.stations.add("t")               # the same station on its own token: taken
        assert call(hub.url + "/api/station/jobs?wait=0", token=own)[0] == 200
        other = hub.app.hub.stations.add("st-b")
        for method, path, body in (("POST", "/api/station/events", {"epoch": "b", "station": "st-b", "events": []}),
                                   ("POST", "/api/station/state", {"at": 1.0, "status": {"id": "st-b"}}),
                                   ("GET", "/api/station/jobs?wait=0", None),
                                   ("POST", "/api/station/jobs/0123456789abcdef", {}),
                                   ("POST", "/api/station/aircraft", {}), ("POST", "/api/station/vessels", {})):
            code, out = call(hub.url + path, method, body, token=other)
            assert code == 409 and out["reason"] == "multi_station_off", (path, code, out)
        assert hub.app.hub.status["id"] == "t" and hub.app.hub.last_seen >= seen
        for method, path in (("GET", "/api/station/hello"), ("GET", "/api/station/status")):
            assert call(hub.url + path, method, token=other)[0] == 200
        assert [m for m in logs if "multi_station_off" in m] == ["hub: station request refused: multi_station_off (station st-b)"]
        hints = hub.app.health.model()["hints"]
        assert any(h["channel"] == "Stations" and "st-b" in h["text"] for h in hints)
        assert "st-b" in hub.app.hub.health()["detail"]
        assert "st-b" in hub.app.hub.stations.panel()["others"]
        c = HubClient(hub.url, other)
        with pytest.raises(HubError) as e:
            c.request("GET", "/api/station/jobs?wait=0")
        assert e.value.status == 409 and "multi_station_off" in str(e.value) and other.split(".")[2] not in str(e.value)
    finally:
        hub.close()


def test_the_shared_token_works_unchanged_for_the_existing_station_and_not_for_a_new_one(tmp_path):
    """Deployed with nothing configured: the station keeps posting on the shared token (as it does today), and that
    token is its id from then on; another id with it is refused."""
    hub = HubRun(hub_cfg(tmp_path))
    try:
        assert call(hub.url + "/api/station/jobs?wait=0")[0] == 200       # no id yet: fine
        assert call(hub.url + "/api/station/state", "POST", {"at": 1.0, "status": {"id": "t"}})[0] == 200
        assert hub.app.hub.stations.legacy_id() == "t"
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e", "station": "t", "events": []})[0] == 200
        code, out = call(hub.url + "/api/station/events", "POST", {"epoch": "e", "station": "st-new", "events": []})
        assert code == 401 and out["reason"] == "id_mismatch"
    finally:
        hub.close()


def test_the_station_log_stream_is_named_after_the_authenticated_id():
    fake = FakeLogs()
    sl = sink(fake)
    lines = [[int(T0 * 1000), "sizeup-station", "info", "hello"]]
    body = gzip.compress(json.dumps({"v": 1, "station": "st-b", "batch": "b", "lines": lines}).encode())
    code, out = sl.ingest(body, GZ, station=lambda claimed: "st-b" if claimed in ("st-b", None) else (_ for _ in ()).throw(
        Refused("id_mismatch")))
    assert code == 200 and out["accepted"] and list(fake.streams)[0].startswith("st-b-")
    body = gzip.compress(json.dumps({"v": 1, "batch": "b", "lines": lines}).encode())     # no id: the token's
    sl.ingest(body, GZ, station=lambda claimed: "st-b")
    assert all(s.startswith("st-b-") for s in fake.streams)
    with pytest.raises(Refused):
        sl.ingest(gzip.compress(json.dumps({"v": 1, "station": "st-a", "batch": "b", "lines": lines}).encode()), GZ,
                  station=lambda claimed: (_ for _ in ()).throw(Refused("id_mismatch")))


# -- no startup writes ---------------------------------------------------------------------------------------------------

def test_the_table_is_made_on_first_use_never_at_the_start(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        db = sqlite3.connect(Path(hub.app.cfg.cad_db).parent / "hub.sqlite")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        assert "stations" not in tables
        assert call(hub.url + "/api/station/jobs?wait=0")[0] == 200          # the shared token: no table either
        assert "stations" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        assert hub.app.hub.stations.panel()["stations"] == []
        hub.app.hub.stations.add("st-b")
        assert "stations" in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    finally:
        hub.close()


# -- admins: Settings > Station > Stations ----------------------------------------------------------------------------

@pytest.fixture
def admin(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    reg, clock, logs = registry(tmp_path, legacy_id="st-hq")
    cad.stations = reg
    return CadApi(cad), cad, reg


def j(res):
    return res[0], json.loads(res[1])


def test_only_a_signed_in_admin_reads_or_manages_stations(admin):
    api, cad, reg = admin
    assert "/api/cad/stations" in ADMIN_READS and needs("/api/cad/stations") == "admin"
    for who in (None, Identity(email=JORDAN)):
        assert api.get("/api/cad/stations", "", who)[0] == 403
        assert api.post("/api/cad/stations", json.dumps({"action": "add", "id": "st-x"}).encode(), who)[0] == 403
    assert reg.rows() == []
    code, out = j(api.get("/api/cad/stations", "", Identity(email=CASEY)))
    assert code == 200 and out["shared"]["station"] == "st-hq" and out["shared"]["configured"] is True


def test_add_rotate_revoke_rename_are_audited_without_a_token(admin):
    api, cad, reg = admin
    me = Identity(email=CASEY)
    code, out = j(api.post("/api/cad/stations", json.dumps({"action": "add", "id": "st-north", "name": "North"}).encode(), me))
    assert code == 200 and out["token"].startswith("sz1.st-north.") and out["status_token"] == status_token(out["token"])
    tokens = [out["token"], out["status_token"]]
    code, out = j(api.post("/api/cad/stations", json.dumps({"action": "rotate", "id": "st-north"}).encode(), me))
    assert code == 200 and out["token"] not in tokens
    tokens += [out["token"], out["status_token"]]
    for body in ({"action": "rename", "id": "st-north", "name": "North house"}, {"action": "revoke", "id": "st-north"}):
        assert api.post("/api/cad/stations", json.dumps(body).encode(), me)[0] == 200
    code, out = j(api.post("/api/cad/stations", json.dumps({"action": "add", "id": "Bad Id"}).encode(), me))
    assert code == 400 and "id:" in out["error"]
    code, panel = j(api.get("/api/cad/stations", "", me))
    (row,) = panel["stations"]
    assert row["id"] == "st-north" and row["name"] == "North house" and row["revoked"]
    assert "hash" not in json.dumps(panel) and not any(t in json.dumps(panel) for t in tokens)
    audit = cad.settings.audit_log("system")
    keys = [r["key"] for r in audit]
    assert {"stations/st-north", "stations/st-north/name"} <= set(keys)
    acts = cad.activity.rows(CASEY, "", None, 50)
    blob = json.dumps(audit) + json.dumps(acts)
    assert not any(t in blob for t in tokens) and "sz1." not in blob
    assert any(a["summary"] == "Stations: add st-north" for a in acts)


def test_an_admin_sets_or_clears_the_shared_token_station_audited(admin, tmp_path, capsys, monkeypatch):
    api, cad, reg = admin
    me = Identity(email=CASEY)
    assert api.post("/api/cad/stations", json.dumps({"action": "shared_id", "id": "st-x"}).encode(),
                    Identity(email=JORDAN))[0] == 403
    code, out = j(api.post("/api/cad/stations", json.dumps({"action": "shared_id", "id": "st-real"}).encode(), me))
    assert code == 200 and out["station"] == "st-real" and reg.legacy_id() == "st-real"
    assert j(api.get("/api/cad/stations", "", me))[1]["shared"]["pinned_by"] == "hub.sqlite"
    code, out = j(api.post("/api/cad/stations", json.dumps({"action": "shared_id", "id": ""}).encode(), me))
    assert out["station"] == "st-hq"                      # back to the config's ([cloud] legacy_station_id)
    rows = [(r["key"], r["old"], r["new"]) for r in cad.settings.audit_log("system")]
    assert ("stations/shared token id", "st-hq", "st-real") in rows and ("stations/shared token id", "st-real", "st-hq") in rows
    # the CLI, on the leader task
    state = tmp_path / "state"
    state.mkdir()
    MonitorDb(state / "hub.sqlite")
    lease(cli_env(monkeypatch, tmp_path))
    assert st_mod.main(["--state", str(state), "reset-shared-id", "--id", "st-real"]) == 0
    assert "st-real" in capsys.readouterr().out
    assert StationRegistry(MonitorDb(state / "hub.sqlite"), shared=SHARED, legacy_id="st-hq").legacy_id() == "st-real"


def test_the_new_token_answer_is_not_cached(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    reg, _, _ = registry(tmp_path)
    cad.stations = reg

    class Access:
        def identify(self, headers):
            e = headers.get("X-Test-User")
            return Identity(email=e) if e else None
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, cad=CadApi(cad), access=Access(),
                    role="hub")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.httpd.server_address[1]}/api/cad/stations"
        req = urllib.request.Request(url, method="POST", data=json.dumps({"action": "add", "id": "st-x"}).encode(),
                                     headers={"Content-Type": "application/json", "X-Test-User": CASEY})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.headers.get("Cache-Control") == "no-store"
            assert json.loads(r.read())["token"].startswith("sz1.st-x.")
    finally:
        srv.shutdown()


# -- the CLI (inside the hub task, in an emergency) ------------------------------------------------------------------

def cli_env(monkeypatch, tmp_path) -> Path:
    """The hub task's environment as `aws ecs execute-command` gives it: no SIZEUP_LEASE_FILE (only the supervisor's
    app child has it), the supervisor's run dir. Returns that run dir (made)."""
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    monkeypatch.delenv("SIZEUP_LEASE_FILE", raising=False)
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    monkeypatch.setenv("SIZEUP_RUN_DIR", str(run))
    monkeypatch.setenv("SIZEUP_CONFIG", str(tmp_path / "no-config.toml"))
    return run


def lease(run: Path, ours: bool = True) -> None:
    """The supervisor's lease file (sizeup/handoff.py): ours = valid now (the container's monotonic clock)."""
    import time as _t
    (run / "lease.json").write_text(json.dumps({"generation": 7, "holder": "task-a",
                                                "valid_until": _t.monotonic() + (30 if ours else -1)}))


def test_the_cli_writes_only_on_the_task_holding_the_lease(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    MonitorDb(state / "hub.sqlite")
    run = cli_env(monkeypatch, tmp_path)
    add = ["--state", str(state), "add", "st-x"]
    assert st_mod.main(add) == 3                                  # the supervisor's run dir, no lease: a standby
    assert "can't show it is the leader" in capsys.readouterr().err
    lease(run, ours=False)
    assert st_mod.main(add) == 3                                  # a lease that isn't current: refused
    assert "isn't the leader" in capsys.readouterr().err
    assert st_mod.main(["--state", str(state), "--no-lease", *add[2:]]) == 3   # --no-lease never beats a lease file
    lease(run)
    assert st_mod.main(add) == 0                                  # the leader
    # through SIZEUP_LEASE_FILE too, as the app child has it
    monkeypatch.setenv("SIZEUP_LEASE_FILE", str(run / "lease.json"))
    assert st_mod.leader_problem() is None
    # a hub without the supervisor (one process): only with --no-lease, and never where a supervisor runs
    monkeypatch.delenv("SIZEUP_LEASE_FILE")
    monkeypatch.setenv("SIZEUP_RUN_DIR", str(tmp_path / "nothing-here"))
    assert st_mod.leader_problem() is not None and st_mod.leader_problem(no_lease=True) is None
    monkeypatch.setenv("LITESTREAM_BUCKET", "made-up-bucket")
    assert st_mod.leader_problem(no_lease=True) is not None
    assert st_mod.main(["--state", str(state), "list"]) == 0      # reading needs no lease
    assert StationRegistry(MonitorDb(state / "hub.sqlite")).row("st-x") is not None


def test_the_cli_retire_asks_for_force_when_the_hub_used_the_shared_token_recently(tmp_path, capsys, monkeypatch):
    """The CLI is another process than the hub: it reads the shared token's last use from hub.sqlite (written at most
    every TOUCH_S), so a station still on it isn't cut off by accident."""
    state = tmp_path / "state"
    state.mkdir()
    hub_reg = StationRegistry(MonitorDb(state / "hub.sqlite"), shared=SHARED, legacy_id="st-hq")
    hub_reg.touch(hub_reg.resolve(SHARED))                        # the running hub: written to meta (the first touch)
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", SHARED)
    lease(cli_env(monkeypatch, tmp_path))
    assert st_mod.main(["--state", str(state), "retire-shared"]) == 1
    assert "s ago: a station still runs on it" in capsys.readouterr().err
    assert hub_reg.resolve(SHARED).id == "st-hq"
    assert st_mod.main(["--state", str(state), "retire-shared", "--force"]) == 0
    assert refused(hub_reg.resolve, SHARED) == "token_retired"


def test_the_cli_adds_lists_revokes_and_retires(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    MonitorDb(state / "hub.sqlite")
    cad = make_cad(state)                              # its cad.sqlite: the CLI writes the Settings audit there
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", SHARED)
    run = cli_env(monkeypatch, tmp_path)
    lease(run)                                         # this is the leader task (the supervisor's lease file)
    assert st_mod.main(["--state", str(state), "--by", "ops", "add", "st-cli", "--name", "CLI"]) == 0
    out = capsys.readouterr().out
    tok = re.search(r"SIZEUP_STATION_TOKEN=(sz1\.st-cli\.\S+)", out).group(1)
    assert status_token(tok) in out
    reg = StationRegistry(MonitorDb(state / "hub.sqlite"), shared=SHARED)
    assert reg.resolve(tok).id == "st-cli"
    assert st_mod.main(["--state", str(state), "list"]) == 0
    listed = capsys.readouterr().out
    assert "st-cli" in listed and "CLI" in listed and tok not in listed and "shared token: configured" in listed
    ops = ["--state", str(state), "--by", "ops"]
    assert st_mod.main([*ops, "revoke", "st-cli"]) == 0
    assert refused(reg.resolve, tok) == "token_revoked"           # the running hub's next request: refused
    assert st_mod.main([*ops, "revoke", "st-cli"]) == 1
    assert st_mod.main([*ops, "retire-shared"]) == 0
    assert refused(reg.resolve, SHARED) == "token_retired"
    assert st_mod.main([*ops, "restore-shared"]) == 0
    audit = cad.settings.audit_log("system")
    assert {(r["operator"], r["key"], r["new"]) for r in audit} >= {
        ("ops (CLI)", "stations/st-cli", "added (CLI)"), ("ops (CLI)", "stations/st-cli", "revoked"),
        ("ops (CLI)", "stations/shared token", "retired"), ("ops (CLI)", "stations/shared token", "restored")}
    assert tok not in json.dumps(audit)
    assert st_mod.main(["--state", str(tmp_path / "nowhere"), "list"]) == 2


def test_legacy_station_id_config(tmp_path, monkeypatch):
    from sizeup.config import ConfigError, load
    monkeypatch.delenv("SIZEUP_STATION_TOKEN", raising=False)
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path / "data"))
    p = tmp_path / "c.toml"
    p.write_text('[cloud]\nrole = "hub"\nlegacy_station_id = "st-hq"\n')
    assert load(p).cloud_legacy_station_id == "st-hq"
    p.write_text('[cloud]\nrole = "hub"\nlegacy_station_id = "' + "x" * 65 + '"\n')
    with pytest.raises(ConfigError, match="legacy_station_id"):
        load(p)
    # the hub's shared token is the station deploy/station/config.station.toml installs: no learning in production
    import tomllib
    station_id = tomllib.loads((ROOT / "deploy/station/config.station.toml").read_text())["station"]["id"]
    assert load(ROOT / "deploy/hub/config.hub.toml").cloud_legacy_station_id == station_id


# -- the station's side --------------------------------------------------------------------------------------------

def test_the_station_says_why_the_hub_refused_it_without_the_token(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        tok = hub.app.hub.stations.add("st-b")
        hub.app.hub.stations.revoke("st-b")
        c = HubClient(hub.url, tok)
        with pytest.raises(HubError) as e:
            c.request("GET", "/api/station/hello")
        msg = str(e.value)
        assert e.value.status == 401 and "token_revoked" in msg and "Rotate" in msg and tok.split(".")[2] not in msg
    finally:
        hub.close()


def test_a_refusal_is_logged_even_in_the_middle_of_an_outage():
    from sizeup.cloud.station import StationLink
    said: list[str] = []
    link = StationLink.__new__(StationLink)
    link._lock, link.log, link.error_since, link.last_error = threading.Lock(), said.append, None, ""
    link._fail(HubError("can't reach the hub: timeout"))
    link._fail(HubError("can't reach the hub: timeout"))
    refusal = HubError("hub refused this station (token_revoked): revoked", 401)
    link._fail(refusal)
    link._fail(refusal)
    assert said == ["hub link: can't reach the hub: timeout", "hub link: hub refused this station (token_revoked): revoked"]


def test_a_station_token_in_a_log_line_is_scrubbed_but_its_id_kept():
    from sizeup import logscrub
    tok = st_mod.make_token("st-north")
    out = logscrub.scrub(f"hub link: tried {tok} again") or ""
    assert tok.split(".")[2] not in out and "sz1.st-north." in out


def test_the_station_code_runs_on_the_station_python():
    """client.py and station.py run under the station's Python 3.12 (Ubuntu 24.04): nothing newer in what changed."""
    import ast
    for f in ("sizeup/cloud/client.py", "sizeup/cloud/station.py", "sizeup/cloud/stations.py"):
        ast.parse((ROOT / f).read_text(), feature_version=(3, 12))
