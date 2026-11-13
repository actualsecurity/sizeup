"""A fenced hub task writes nothing while it serves GETs (#501, sizeup/fence.py GuardedConnection).

Only the leader writes (sizeup/handoff.py, leaderlock.py). The Gate refuses a fenced task's POSTs, but GETs are served,
and some of them wrote as a side effect: the activity log (a sign-in, a search, a CSV), a first sign-in's operator row,
when someone was last seen, History here's and Search's address keys. Now the three databases' own connections skip
every write and roll back every COMMIT while the fence says the lease isn't ours; the leader writes as before.
All data here is made up."""

import hashlib
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from sizeup import fence as fence_mod
from sizeup.app import make_app, make_server
from sizeup.cad.api import CadApi
from sizeup.cad.store import Store
from sizeup.cloud.access import Identity
from sizeup.fence import GuardedConnection, is_write
from tests.test_access import AUD, TEAM, Jwks, Key, jwt
from tests.test_cad import make_cad
from tests.test_cloud import HubRun, hub_cfg
from tests.test_roles import COREY, people

NEWBIE = "newbie@example.org"


class Switch:
    """may_write: True (the leader) until flipped."""

    def __init__(self):
        self.ours = True

    def __call__(self) -> bool:
        return self.ours


# -- the guard ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql, write", [
    ("SELECT 1", False), ("  select * from units", False), ("-- a note\nSELECT 1", False), ("/* x */ SELECT 1", False),
    ("BEGIN IMMEDIATE", False), ("COMMIT", False), ("ROLLBACK", False), ("VALUES (1)", False),
    ("PRAGMA user_version", False), ("PRAGMA table_info(units)", False), ("PRAGMA busy_timeout = 100", False),
    ("PRAGMA foreign_keys = ON", False), ("PRAGMA query_only = 1", False),
    ("INSERT INTO meta VALUES ('a', 'b')", True), ("update units set notes = ''", True), ("DELETE FROM meta", True),
    ("REPLACE INTO meta VALUES ('a', 'b')", True), ("CREATE TABLE x (a)", True), ("DROP TABLE x", True),
    ("ALTER TABLE x ADD COLUMN b", True), ("VACUUM", True), ("PRAGMA user_version = 3", True),
    ("PRAGMA incremental_vacuum(10)", True), ("PRAGMA wal_checkpoint(TRUNCATE)", True), ("PRAGMA journal_mode = WAL", True),
    ("WITH a AS (SELECT 1) SELECT * FROM a", False), ("WITH a AS (SELECT 1) INSERT INTO meta SELECT 'k', 'v' FROM a", True),
    ("-- INSERT\nSELECT 1", False), ("", True),
])
def test_what_counts_as_a_write(sql, write):
    assert is_write(sql) is write


def connect(path):
    db = sqlite3.connect(str(path), isolation_level=None, factory=GuardedConnection)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v TEXT)")
    return db


def test_a_fenced_connection_reads_and_writes_nothing(tmp_path):
    db, sw, said = connect(tmp_path / "x.sqlite"), Switch(), []
    db.may_write, db.log = sw, said.append
    db.execute("INSERT INTO t VALUES ('a', '1')")                    # the leader: as ever
    db.commit()
    sw.ours = False
    before = db.total_changes
    cur = db.execute("INSERT INTO t VALUES ('b', '2')")
    assert cur.rowcount == 0 and cur.fetchone() is None and list(cur) == []
    assert db.execute("UPDATE t SET v = 'x' WHERE k = 'a'").rowcount == 0
    db.executemany("INSERT INTO t VALUES (?, ?)", [("c", "3")])
    db.executescript("INSERT INTO t VALUES ('d', '4'); DELETE FROM t;")
    assert db.execute("SELECT k, v FROM t").fetchall() == [("a", "1")]   # reads go on
    assert db.total_changes == before and db.skipped == 4
    assert len(said) == 1 and "INSERT INTO t" in said[0] and "'b'" not in said[0]   # once a minute, never the values
    sw.ours = True
    db.execute("INSERT INTO t VALUES ('e', '5')")                    # ours again: written
    assert [r[0] for r in db.execute("SELECT k FROM t ORDER BY k")] == ["a", "e"]


def test_a_transaction_the_lease_ran_out_in_keeps_nothing(tmp_path):
    db, sw = connect(tmp_path / "x.sqlite"), Switch()
    db.may_write = sw
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO t VALUES ('a', '1')")                    # written while ours
    sw.ours = False
    db.execute("INSERT INTO t VALUES ('b', '2')")                    # skipped
    db.execute("COMMIT")                                             # a ROLLBACK
    assert not db.in_transaction and db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    sw.ours = True
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES ('a', '1')")
    sw.ours = False
    db.commit()                                                      # the method too
    sw.ours = True
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_the_store_commits_only_while_ours_and_opens_as_before(tmp_path):
    store = Store(tmp_path / "cad.sqlite")                          # opened (migrated): may_write starts True
    sw = Switch()
    store.may_write = sw
    rev, rolled = store.rev, []
    store.after_rollback = lambda: rolled.append(1)
    sw.ours = False
    with store.tx() as db:
        db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    store.meta_put("k2", "v2")
    assert store.meta_get("k") is None and store.meta_get("k2") is None and store.rev == rev and rolled == [1]
    sw.ours = True
    with store.tx() as db:
        db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    assert store.meta_get("k") == "v" and store.rev == rev + 1
    again = Store(tmp_path / "cad.sqlite")                          # a takeover's --init-state: its own store
    assert again.may_write() and again.meta_get("k") == "v"


def test_in_memory_stores_are_unchanged():
    store = Store()
    assert store.may_write()
    store.meta_put("k", "v")
    assert store.meta_get("k") == "v"


# -- the CAD: the leader records, a fenced task doesn't ------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    a = CadApi(make_cad(tmp_path))
    a.ids = people(a.cad)
    a.sw = Switch()
    a.cad.may_write = a.sw
    return a


def changes(api) -> int:
    return api.cad.store.db.total_changes


def test_the_leader_still_records_activity(api):
    who = Identity(email=COREY, session="1760000000")
    api.visit(who, ("203.0.113.9", "UA"), "GET /cad")
    api.get("/api/cad/search", "text=smoke", who)
    api.get("/api/cad/search.csv", "text=smoke", who)
    kinds = [r["kind"] for r in api.cad.activity.rows(email=COREY)]
    assert kinds == ["search", "search", "signin"]
    assert api.cad.activity.seen(COREY)[0]["last_seen"] is not None


def test_a_fenced_task_records_nothing_but_answers(api):
    api.sw.ours = False
    n = changes(api)
    who = Identity(email=COREY, session="1760000001")
    api.visit(who, ("203.0.113.9", "UA"), "GET /cad")
    for path, q in (("/api/cad/search", "text=smoke"), ("/api/cad/search.csv", "text=smoke"), ("/api/cad/preplan.csv", ""),
                    ("/api/cad/whoami", ""), ("/api/cad/state", ""), ("/api/cad/history", ""),
                    ("/api/cad/export/activity.csv", "")):
        assert api.get(path, q, who)[0] == 200, path
    assert changes(api) == n and api.cad.activity.rows() == []
    assert api.cad.activity.seen(COREY)[0]["last_seen"] is None


def test_a_first_sign_in_on_a_fenced_task_is_let_in_and_not_added(api):
    api.sw.ours = False
    n, ops = changes(api), api.cad.settings.operators()
    code, raw, _ = api.get("/api/cad/whoami", "", Identity(email=NEWBIE))
    me = json.loads(raw)
    assert code == 200 and (me["email"], me["name"], me["role"], me["may"], me["source"]) == (
        NEWBIE, NEWBIE, "dispatcher", "dispatcher", "access")
    st = api.cad.settings.standing(NEWBIE)
    assert st["operator"] == NEWBIE and st["admins"] == ["Corey"]
    assert api.cad.settings.operators() == ops and changes(api) == n and api.cad.settings.audit_log("users")[0]["key"] != NEWBIE
    # ... a dispatcher's POST is the leader's to take (the Gate answers 503 there first); a viewer's read is still a read
    api.sw.ours = True
    api.cad.settings.person(NEWBIE)                                  # the leader: added, as before
    assert NEWBIE in [o["email"] for o in api.cad.settings.operators()]


def test_history_here_and_search_key_nothing_on_a_fenced_task(api):
    c = api.cad
    inc = c.create({"type": "STRU", "address": "100 Example St", "operator": "Corey"})
    c.close(inc["id"], {"disposition": "NO ACTION", "operator": "Corey"})
    api.sw.ours = False
    queued = c.store.q("SELECT id FROM addr_queue")
    n = changes(api)
    assert api.get(f"/api/cad/incident/{inc['id']}/here", "", None)[0] == 200
    assert api.get("/api/cad/search", "where=100+Example+St", None)[0] == 200
    assert c.store.q("SELECT id FROM addr_queue") == queued and changes(api) == n
    api.sw.ours = True
    c.addrs.catch_up()                                               # the leader keys them
    assert c.store.q("SELECT id FROM addr_queue") == []


# -- the hub, end to end: a real lease file, the server's Gate, the three databases --------------------------------------

def req(url, method="GET", headers=None):
    r = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(url, body, headers):
    r = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def file_bytes(db_path) -> dict:
    """The database file and its WAL (the -shm is the readers' and writers' locks, not data)."""
    out = {}
    for p in (db_path, db_path.with_name(db_path.name + "-wal")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    return out


@pytest.fixture(scope="module")
def keys():
    return (Key("k1", 1),)


@pytest.fixture
def fenced_hub(tmp_path, keys, monkeypatch):
    lease = tmp_path / "lease.json"

    def put(valid_s: float) -> None:
        lease.write_text(json.dumps({"generation": 3, "valid_until": time.monotonic() + valid_s, "holder": "test"}))
    put(3600)
    monkeypatch.setenv("SIZEUP_LEASE_FILE", str(lease))
    jw = Jwks(keys)
    h = HubRun.__new__(HubRun)                                       # HubRun's, with the fence on the server too
    h.app = make_app(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD)).start()
    assert h.app.fence is not None and h.app.fence.ok()
    h.server = make_server(h.app, h.app.fence)
    h.server.access.certs_url, h.server.access.log = jw.url, (lambda m: None)
    threading.Thread(target=h.server.serve_forever, daemon=True).start()
    h.url = f"http://127.0.0.1:{h.server.httpd.server_address[1]}"

    def fence() -> None:
        put(-1)
        h.app.fence._read_at = -1e9                                  # read the lease file again now, not in 0.5 s
        assert not h.app.fence.ok()
    h.fence = fence
    h.dir = tmp_path / "hub"
    yield h
    h.close()
    jw.close()


def test_a_fenced_hub_serves_its_pages_and_writes_none_of_its_databases(fenced_hub, keys):
    h = fenced_hub
    admin = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=COREY)}
    assert req(h.url + "/api/cad/whoami", headers=admin)[0] == 200           # the leader: signed in, added
    code, inc = post(h.url + "/api/cad/incident", {"type": "STRU", "address": "100 Example St"}, admin)
    assert code == 200
    iid = inc["id"]
    assert post(h.url + f"/api/cad/incident/{iid}/note", {"text": "Made-up note"}, admin)[0] == 200
    code, old = post(h.url + "/api/cad/incident", {"type": "STRU", "address": "200 Sample Ave"}, admin)
    assert post(h.url + f"/api/cad/incident/{old['id']}/close", {"disposition": "NO ACTION"}, admin)[0] == 200
    leader_rows = h.app.cad.activity.rows()
    assert any(r["kind"] == "signin" for r in leader_rows)                   # the leader records

    h.fence()
    dbs = {"cad": h.app.cad.store.db, "hub": h.app.mdb.db, "audio": h.app.audio.db}
    wrote = {k: [] for k in dbs}
    for k, db in dbs.items():
        db.set_trace_callback(lambda s, k=k: wrote[k].append(s) if is_write(s) or s.split()[0].upper() == "COMMIT" else None)
    totals = {k: db.total_changes for k, db in dbs.items()}
    files = {k: file_bytes(h.dir / f"{k}.sqlite") for k in dbs}
    new = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=NEWBIE, iat=int(time.time()) - 5)}     # a first sign-in
    again = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=COREY, iat=int(time.time()) - 7)}    # a new Access login
    pages = ["/cad", "/", "/api/cad/state", "/api/state", "/api/cad/whoami", "/api/cad/history", f"/api/cad/incident/{iid}",
             f"/api/cad/incident/{old['id']}/here", "/api/cad/search?text=Made-up", "/api/cad/search?where=200+Sample+Ave",
             "/api/cad/search.csv?text=Made-up", "/api/cad/preplan.csv", "/api/cad/export/incidents.csv",
             "/api/cad/export/activity.csv", "/api/cad/activity", "/api/cad/settings", "/api/cad/runcards", "/api/cad/comms",
             "/api/cad/feed", "/api/cad/premise?all=1", "/api/cad/radios", "/api/cad/admin", "/api/cad/alarms",
             f"/api/cad/incident/{iid}/attachments", "/static/cad.js", "/healthz"]
    for who in (new, again):
        for p in pages:
            for method in ("GET", "HEAD"):
                code, body = req(h.url + p, method, who)
                assert code == 200, (method, p, code, body[:200])
    code, body = req(h.url + "/api/cad/whoami", headers=new)
    me = json.loads(body)
    assert (me["email"], me["name"], me["source"]) == (NEWBIE, NEWBIE, "access")             # let in ...
    assert NEWBIE not in [o["email"] for o in h.app.cad.settings.operators()]                 # ... not added
    code, body = req(h.url + "/api/cad/incident/" + str(iid), headers=again)
    assert "Made-up note" in body.decode()                                                    # reads are served
    assert post(h.url + "/api/cad/incident", {"type": "STRU"}, admin)[0] == 503             # a POST: the Gate's 503
    assert wrote == {k: [] for k in dbs}
    assert {k: db.total_changes for k, db in dbs.items()} == totals
    assert {k: file_bytes(h.dir / f"{k}.sqlite") for k in dbs} == files
    assert h.app.cad.activity.rows() == leader_rows
    assert h.app.cad.store.db.skipped > 0                                    # (they were tried: and skipped)


def test_the_guard_is_on_every_hub_database(fenced_hub):
    h = fenced_hub
    for db in (h.app.cad.store.db, h.app.mdb.db, h.app.audio.db):
        assert isinstance(db, GuardedConnection) and db.may_write()
    h.fence()
    for db in (h.app.cad.store.db, h.app.mdb.db, h.app.audio.db):
        assert not db.may_write()
    h.app.mdb.put("k", "v")
    h.app.audio.put("clip-1", b"RIFF" + b"\0" * 40)
    assert h.app.mdb.get("k") == ""
    assert h.app.audio.db.execute("SELECT COUNT(*) FROM audio WHERE id = 'clip-1'").fetchone()[0] == 0


def test_skip_log_says_once_a_minute(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(fence_mod.time, "monotonic", lambda: t[0])
    db, said = sqlite3.connect(":memory:", isolation_level=None, factory=GuardedConnection), []
    db.execute("CREATE TABLE t (a)")
    db.may_write, db.log = (lambda: False), said.append
    for _ in range(3):
        db.execute("INSERT INTO t VALUES (1)")
    t[0] += fence_mod.SKIP_LOG_EVERY_S
    db.execute("INSERT INTO t VALUES (1)")
    assert len(said) == 2 and "4 so far" in said[1]
