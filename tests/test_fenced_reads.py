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
from sizeup.fence import FencedWrite, GuardedConnection, bookkeeping, is_write
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


def test_a_fenced_connection_refuses_a_write_out_loud_and_skips_only_bookkeeping(tmp_path):
    db, sw, said = connect(tmp_path / "x.sqlite"), Switch(), []
    db.may_write, db.log = sw, said.append
    db.execute("INSERT INTO t VALUES ('a', '1')")                    # the leader: as ever
    sw.ours = False
    before, refused = db.total_changes, fence_mod.refused()
    for write in (lambda: db.execute("INSERT INTO t VALUES ('b', '2')"),     # data: refused, never a quiet rowcount 0
                  lambda: db.execute("UPDATE t SET v = 'x' WHERE k = 'a'"),
                  lambda: db.executemany("INSERT INTO t VALUES (?, ?)", [("c", "3")]),
                  lambda: db.executescript("INSERT INTO t VALUES ('d', '4'); DELETE FROM t;")):
        with pytest.raises(FencedWrite):
            write()
    assert fence_mod.refused() == refused + 4 and db.refused == 4    # counted on this thread too
    with bookkeeping():                                              # a GET's side effect: skipped, nothing raised
        cur = db.execute("INSERT INTO t VALUES ('e', '5')")
        assert cur.rowcount == 0 and cur.fetchone() is None and list(cur) == []
    assert fence_mod.refused() == refused + 4 and db.skipped == 1
    assert db.execute("SELECT k, v FROM t").fetchall() == [("a", "1")]   # reads go on
    assert db.total_changes == before
    assert len(said) == 1 and "INSERT INTO t" in said[0] and "'b'" not in said[0]   # once a minute, never the values
    sw.ours = True
    db.execute("INSERT INTO t VALUES ('f', '6')")                    # ours again: written
    assert [r[0] for r in db.execute("SELECT k FROM t ORDER BY k")] == ["a", "f"]


def test_a_transaction_the_lease_ran_out_in_keeps_nothing_and_says_so(tmp_path):
    db, sw = connect(tmp_path / "x.sqlite"), Switch()
    db.may_write = sw
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO t VALUES ('a', '1')")                    # written while ours
    sw.ours = False
    with pytest.raises(FencedWrite):
        db.execute("COMMIT")                                         # a ROLLBACK, and refused
    assert not db.in_transaction and db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    sw.ours = True
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES ('a', '1')")
    sw.ours = False
    with pytest.raises(FencedWrite):
        db.commit()                                                  # the method too
    sw.ours = True
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES ('a', '1')")
    sw.ours = False
    with bookkeeping():
        db.execute("COMMIT")                                         # bookkeeping: rolled back, quietly
    sw.ours = True
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_the_store_commits_only_while_ours_and_opens_as_before(tmp_path):
    store = Store(tmp_path / "cad.sqlite")                          # opened (migrated): may_write starts True
    sw = Switch()
    store.may_write = sw
    rev, rolled = store.rev, []
    store.after_rollback = lambda: rolled.append(1)
    sw.ours = False
    with pytest.raises(FencedWrite):
        with store.tx() as db:
            db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    with pytest.raises(FencedWrite):
        store.meta_put("k2", "v2")
    with bookkeeping():
        with store.tx() as db:                                       # all bookkeeping: rolled back, nothing raised
            db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    assert store.meta_get("k") is None and store.meta_get("k2") is None and store.rev == rev and rolled == [1, 1]
    sw.ours = True
    with store.tx() as db:
        db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    assert store.meta_get("k") == "v" and store.rev == rev + 1
    again = Store(tmp_path / "cad.sqlite")                          # a takeover's --init-state: its own store
    assert again.may_write() and again.meta_get("k") == "v"


def test_the_guard_allows_until_the_app_wires_the_fence(tmp_path):
    """#507: Litestream replicates first, then the gate's "init" runs the migrations (init_state), then "run" builds the
    app (Cad's own opening writes: schema, seeds, sweeps), and only then does the App hand its fence to the databases.
    None of that may ever be refused: every connection opens allowing writes."""
    from sizeup.app import init_state
    from sizeup.cloud.hubstore import AudioDb, MonitorDb
    cfg = hub_cfg(tmp_path)
    init_state(cfg)                                                  # the migrations, as at the takeover
    state = tmp_path / "hub"
    for db in (Store(state / "cad.sqlite").db, MonitorDb(state / "hub.sqlite").db, AudioDb(state / "audio.sqlite").db):
        assert isinstance(db, GuardedConnection) and db.may_write() and db.refused == db.skipped == 0


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
    h.exits = []                                                     # fence.check(): recorded here, not os._exit
    h.app.fence.exit_fn, h.app.fence.kill, h.app.fence.log = h.exits.append, (lambda pid, sig: None), (lambda m: None)
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


def test_the_guard_is_on_every_hub_database_and_a_clip_is_refused_not_lost(fenced_hub):
    h = fenced_hub
    for db in (h.app.cad.store.db, h.app.mdb.db, h.app.audio.db):
        assert isinstance(db, GuardedConnection) and db.may_write()
    h.fence()
    for db in (h.app.cad.store.db, h.app.mdb.db, h.app.audio.db):
        assert not db.may_write()
    with pytest.raises(FencedWrite):
        h.app.mdb.put("k", "v")
    with pytest.raises(FencedWrite):                                 # the caller hears it: the station keeps the clip
        h.app.audio.put("clip-1", b"RIFF" + b"\0" * 40)
    assert h.app.mdb.get("k") == ""
    assert h.app.audio.db.execute("SELECT COUNT(*) FROM audio WHERE id = 'clip-1'").fetchone()[0] == 0


def test_skip_log_says_once_a_minute(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(fence_mod.time, "monotonic", lambda: t[0])
    db, said = sqlite3.connect(":memory:", isolation_level=None, factory=GuardedConnection), []
    db.execute("CREATE TABLE t (a)")
    db.may_write, db.log = (lambda: False), said.append
    with bookkeeping():
        for _ in range(3):
            db.execute("INSERT INTO t VALUES (1)")
        t[0] += fence_mod.SKIP_LOG_EVERY_S
        db.execute("INSERT INTO t VALUES (1)")
    assert len(said) == 2 and "skipped" in said[1] and "4 skipped" in said[1]


# -- #509's review: refused out loud, never half a transaction, no bypass ------------------------------------------------

@pytest.mark.parametrize("sql, write", [
    ("WITH a AS (SELECT 'delete me') SELECT replace(x, 'update', 'insert') FROM a", False),   # words in strings, names
    ('WITH "update" AS (SELECT 1) SELECT * FROM "update"', False),
    ("WITH a AS (SELECT 1), b AS (SELECT 2) UPDATE t SET v = 1", True),
    ("WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) DELETE FROM t WHERE k IN c", True),
    ("CREATE TEMP TABLE x (a)", False), ("CREATE TEMPORARY VIEW v AS SELECT 1", False), ("CREATE TABLE temp_x (a)", True),
    ("ATTACH 'other.sqlite' AS o", True), ("DETACH o", False), ("SAVEPOINT s", False), ("RELEASE s", False),
    ("ROLLBACK TO s", False),
])
def test_what_counts_as_a_write_at_statement_level(sql, write):
    assert is_write(sql) is write


def test_the_three_bypasses_are_closed(tmp_path):
    db, sw = connect(tmp_path / "x.sqlite"), Switch()
    db.may_write = sw
    sw.ours = False
    cur = db.cursor()                                                # 1. conn.cursor().execute(...)
    assert isinstance(cur, fence_mod.GuardedCursor)
    for write in (lambda: cur.execute("INSERT INTO t VALUES ('a', '1')"),
                  lambda: cur.executemany("INSERT INTO t VALUES (?, ?)", [("b", "2")]),
                  lambda: cur.executescript("INSERT INTO t VALUES ('c', '3');")):
        with pytest.raises(FencedWrite):
            write()
    assert cur.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0  # its reads go on
    sw.ours = True                                                   # 2. RELEASE of the outermost savepoint
    db.execute("SAVEPOINT s")
    db.execute("INSERT INTO t VALUES ('d', '4')")
    sw.ours = False
    with pytest.raises(FencedWrite):
        db.execute("RELEASE s")
    assert not db.in_transaction
    sw.ours = True
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    db.isolation_level = "DEFERRED"                                  # 3. `with conn:` (the module opens the transaction)
    with pytest.raises(FencedWrite):
        with db:
            db.execute("INSERT INTO t VALUES ('e', '5')")
            sw.ours = False
    sw.ours = True
    assert not db.in_transaction and db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    with db:                                                         # ours: as ever
        db.execute("INSERT INTO t VALUES ('f', '6')")
    assert db.execute("SELECT k FROM t").fetchall() == [("f",)]


@pytest.mark.parametrize("end", ["COMMIT", "END", "commit()", "tx"])
def test_a_flip_back_never_commits_half_a_transaction(tmp_path, end):
    """Refused (and swallowed by the caller) while fenced, then may_write() says True again before the end: the
    transaction is tainted and rolled back, never half committed."""
    store = Store(tmp_path / "cad.sqlite")
    sw = Switch()
    store.may_write = sw
    db = store.db

    def body(c):
        c.execute("INSERT INTO meta (key, value) VALUES ('a', '1')")          # written while ours
        sw.ours = False
        try:
            c.execute("INSERT INTO meta (key, value) VALUES ('b', '2')")      # refused: a caller that swallows it
        except FencedWrite:
            pass
        sw.ours = True                                                        # a late renew
    with pytest.raises(FencedWrite):
        if end == "tx":
            with store.tx() as c:
                body(c)
        else:
            db.execute("BEGIN IMMEDIATE")
            body(db)
            db.commit() if end == "commit()" else db.execute(end)
    assert not db.in_transaction and store.meta_get("a") is None and store.meta_get("b") is None
    with bookkeeping():                                                       # a skipped bookkeeping write taints too
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO meta (key, value) VALUES ('a', '1')")
        sw.ours = False
        db.execute("INSERT INTO meta (key, value) VALUES ('b', '2')")
        sw.ours = True
        db.execute("COMMIT")
    assert store.meta_get("a") is None
    store.meta_put("c", "3")                                                  # clean again afterwards
    assert store.meta_get("c") == "3"


def test_the_monitor_cache_is_not_poisoned_by_a_refused_sync(tmp_path):
    from sizeup.cloud.hubstore import MonitorDb
    from sizeup.incidents import IncidentStore
    mdb, sw, st = MonitorDb(tmp_path / "hub.sqlite"), Switch(), IncidentStore(set())
    mdb.may_write = sw
    assert mdb.sync(st, [{"id": "p-1", "start": 1.0}]) == 1
    written = dict(mdb._written)
    sw.ours = False
    with pytest.raises(FencedWrite):
        mdb.sync(st, [{"id": "p-2", "start": 2.0}])                 # p-2 new, p-1 gone: neither is
    assert mdb._written == written and not mdb.db.in_transaction    # as last COMMITTED
    sw.ours = True
    assert mdb.sync(st, [{"id": "p-2", "start": 2.0}]) == 2          # so the next sync writes both, nothing skipped
    assert [r[0] for r in mdb.db.execute("SELECT id FROM pages")] == ["p-2"]


def test_the_fence_is_sticky_and_one_per_process(tmp_path, monkeypatch):
    from sizeup import app as app_mod
    from sizeup.fence import EXIT_FENCED, Fence
    lease, now, exited = tmp_path / "lease.json", [100.0], []
    f = Fence(lease, mono=lambda: now[0], log=lambda m: None, exit_fn=exited.append, kill=lambda p, s: None)
    assert not f.ok()                                                # not ours yet: not latched (the start waits)
    lease.write_text(json.dumps({"generation": 3, "valid_until": 120.0}))
    now[0] += 1
    assert f.ok()
    now[0] = 121.0
    assert not f.ok()
    lease.write_text(json.dumps({"generation": 3, "valid_until": 500.0}))     # a late renew: never ours again here
    now[0] += 1
    assert not f.ok() and exited == []
    # the one fence: from main / run_gated through serve into the App, its databases, the Gate
    seen = {}

    def start_app(cfg, fence=None):
        seen["fence"] = fence
        raise SystemExit
    monkeypatch.setattr(app_mod, "start_app", start_app)
    with pytest.raises(SystemExit):
        app_mod.serve(hub_cfg(tmp_path), f)
    assert seen["fence"] is f
    g = Fence(lease, mono=lambda: 200.0, log=lambda m: None, exit_fn=exited.append, kill=lambda p, s: None)
    app = make_app(hub_cfg(tmp_path), g)
    try:
        assert app.fence is g and app.cad.may_write == g.ok and app.mdb.may_write() and app.audio.may_write()
        assert app.cad.store.db.on_refuse == app.mdb.db.on_refuse == app.audio.db.on_refuse == g.check
        assert make_server(app, app.fence).gate.fence is g
        lease.write_text(json.dumps({"generation": 3, "valid_until": 0.0}))
        g._read_at = -1e9
        with pytest.raises(FencedWrite):                             # the first refusal: check() stops the process
            app.cad.store.meta_put("k", "v")
        assert exited == [EXIT_FENCED]
    finally:
        g.exit_fn = lambda c: None
        app.stop()


class HubSwitch:
    """A hub whose lease we flip: the App's databases and the station ingest follow one switch (no Gate)."""

    def __init__(self, tmp_path):
        self.h = HubRun(hub_cfg(tmp_path))
        self.sw = Switch()
        a = self.h.app
        a._may_write_audio = self.sw
        a.cad.may_write = a.mdb.may_write = a.audio.may_write = self.sw


def batch(seq, tid, epoch="e-501"):
    from tests.test_cloud import _traffic_event
    return {"epoch": epoch, "station": "st-1", "events": [_traffic_event(seq, tid, time.time())]}


def test_an_events_batch_refused_while_fenced_is_resent_and_lands(tmp_path):
    from tests.test_cloud import TOKEN, call
    hs = HubSwitch(tmp_path)
    h, hub = hs.h, hs.h.app.hub
    try:
        assert call(h.url + "/api/station/events", "POST", batch(1, "FG1-100"))[1]["acked"] == 1
        hs.sw.ours = False
        code, body = call(h.url + "/api/station/events", "POST", batch(2, "FG1-200"))
        assert code == 503 and hub.seq == 1                             # refused: nothing acked
        assert not h.app.audio.has("FG1-200") and "FG1-200" not in h.app.audio._known
        # the refusal swallowed on the way (an `except Exception` in apply): still not acked, seq stays
        real = h.app.apply

        def swallowing(kind, data, audio):
            try:
                real(kind, data, audio)
            except Exception:  # noqa: BLE001
                pass
        h.app.apply = swallowing
        with pytest.raises(FencedWrite):
            hub._events(json.dumps(batch(2, "FG1-200")).encode())
        assert hub.seq == 1 and not h.app.audio.has("FG1-200")
        h.app.apply = real
        for path, method, body in (("/api/station/jobs?wait=0", "GET", None), ("/api/station/state", "POST", {"at": 1}),
                                   ("/api/station/jobs/abc", "POST", {"ok": True})):
            assert call(h.url + path, method, body)[0] == 503, path      # no job handed out, no result taken
        assert call(h.url + "/api/station/hello", "GET", None, token=TOKEN)[0] == 200
        hs.sw.ours = True                                                # the lease is back: the same batch again
        code, body = call(h.url + "/api/station/events", "POST", batch(2, "FG1-200"))
        assert code == 200 and body["acked"] == 2 and body["applied"] == 1
        assert h.app.audio.has("FG1-200") and h.get("/api/audio/FG1-200.wav")[:4] == b"RIFF"
    finally:
        h.close()


def test_no_data_path_answers_success_while_fenced(fenced_hub, keys, monkeypatch):
    """The Gate let these in (the lease went just after): each write refused, a 503, nothing stored; a refusal a handler
    swallows still turns its answer into a 503."""
    from tests.test_cloud import TOKEN
    h = fenced_hub
    admin = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=COREY)}
    code, inc = post(h.url + "/api/cad/incident", {"type": "STRU", "address": "100 Example St"}, admin)
    assert code == 200
    e60 = next(u["id"] for u in h.app.cad.store.units() if u["name"] == "E60")
    h.fence()
    h.server.gate.refuse = lambda write: False                       # past the Gate already
    files = {k: file_bytes(h.dir / f"{k}.sqlite") for k in ("cad", "hub", "audio")}
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    for path, body, who in (("/api/cad/incident", {"type": "STRU", "address": "300 Made Up Rd"}, admin),
                            (f"/api/cad/incident/{inc['id']}/note", {"text": "Made-up note"}, admin),
                            (f"/api/cad/incident/{inc['id']}/dispatch", {"unit_ids": [e60]}, admin),
                            ("/api/cad/settings/users", {"action": "add", "name": "Made Up", "email": "m@example.org",
                                                         "role": "viewer"}, admin),
                            ("/api/station/events", batch(1, "FG1-300"), bearer),
                            ("/api/station/jobs/abc", {"ok": True}, bearer)):
        code, out = post(h.url + path, body, who)
        assert code == 503, (path, code, out)
    assert req(h.url + "/api/station/jobs?wait=0", headers=bearer)[0] == 503
    assert h.app.cad.store.q("SELECT COUNT(*) AS n FROM dispatches")[0]["n"] == 0
    assert {k: file_bytes(h.dir / f"{k}.sqlite") for k in ("cad", "hub", "audio")} == files
    assert h.exits                                                   # the first refusal: fence.check() (here: recorded)

    def swallow(self, path, raw, who=None, client=None):
        try:
            self.cad.store.meta_put("made-up", "1")
        except Exception:  # noqa: BLE001 - a handler that hides it
            pass
        return 200, b'{"ok":true}', "application/json"
    monkeypatch.setattr(CadApi, "post", swallow)
    assert post(h.url + "/api/cad/incident", {"type": "STRU"}, admin)[0] == 503
    assert h.app.cad.store.meta_get("made-up") is None


def test_bookkeeping_is_still_skipped_quietly(fenced_hub, keys):
    h = fenced_hub
    h.fence()
    who = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=COREY, iat=int(time.time()) - 3)}
    for p in ("/api/cad/whoami", "/api/cad/search?text=x", "/api/cad/search.csv?text=x"):
        assert req(h.url + p, headers=who)[0] == 200
    assert h.app.cad.store.db.skipped > 0 and h.app.cad.store.db.refused == 0 and h.exits == []


class Leader:
    """A hub on a fixed port under its own lease file (a task of the hot/cold deploy)."""

    def __init__(self, tmp, port, monkeypatch):
        tmp.mkdir(parents=True, exist_ok=True)
        self.lease = tmp / "lease.json"
        self.put(3600)
        monkeypatch.setenv("SIZEUP_LEASE_FILE", str(self.lease))
        self.app = make_app(hub_cfg(tmp, port=port)).start()
        self.exits = []
        self.app.fence.exit_fn, self.app.fence.kill, self.app.fence.log = self.exits.append, (lambda p, s: None), (lambda m: None)
        self.server = make_server(self.app, self.app.fence)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def put(self, valid_s):
        self.lease.write_text(json.dumps({"generation": 3, "valid_until": time.monotonic() + valid_s, "holder": "t"}))

    def has(self, tid):
        return any(t.id == tid for t in list(self.app.store.loose) + [x for i in self.app.store.incidents for x in i.traffic])

    def close(self):
        self.server.shutdown()
        self.server.httpd.server_close()                             # the address free for the next task
        self.app.stop()


def test_a_station_upload_during_a_drain_lands_on_the_new_leader(tmp_path, monkeypatch):
    """The old leader drains (SIGTERM): the station's clip gets 503 + Retry-After, stays in its outbox; the old task
    stops and releases the lease; the new leader takes it and the outbox's retry delivers the clip there."""
    from tests.test_app import wait
    from tests.test_cloud import free_port, fm_segment, station, station_cfg
    port = free_port()
    a = Leader(tmp_path / "a", port, monkeypatch)
    st = station(station_cfg(tmp_path, f"http://127.0.0.1:{port}"), {3: "Engine 63 responding"})
    b = None
    try:
        first = fm_segment(st)
        assert wait(lambda: a.has(first), 15) and wait(lambda: st.link.outbox.pending()[0] == 0, 10)
        a.server.gate.draining.set()                                 # SIGTERM: draining
        time.sleep(0.01)
        tid = fm_segment(st)
        assert wait(lambda: st.link.status_row()["state"] != "ok", 15)
        assert st.link.outbox.pending()[0] >= 1 and not a.has(tid) and not a.app.audio.has(tid)
        a.close()                                                    # drained, stopped (the lease still ours) ...
        a.put(-1)                                                    # ... then released
        b = Leader(tmp_path / "b", port, monkeypatch)                # the new leader, on the same address
        assert wait(lambda: b.has(tid), 30) and wait(lambda: st.link.outbox.pending()[0] == 0, 10)
        assert b.app.audio.has(tid) and b.app.audio.get(tid)[:4] == b"RIFF"
        audio_a = sqlite3.connect(f"file:{tmp_path / 'a' / 'hub' / 'audio.sqlite'}?mode=ro", uri=True)
        assert audio_a.execute("SELECT COUNT(*) FROM audio WHERE id = ?", (tid,)).fetchone()[0] == 0   # never on A
        audio_a.close()
        assert a.exits == [] and b.exits == []
    finally:
        st.link.stop()
        if b is not None:
            b.close()
