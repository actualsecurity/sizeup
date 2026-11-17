"""The change log, /api/v1/changes and /api/v1/stream (#561). The CAD is a real one (a file database, the board's units,
an incident taken and dispatched); the people and values are invented. Every principal is a test resolver's."""
from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sizeup.api.v1.changelog import Change, ChangeLog, Feed, Subscription, cad_follow
from sizeup.api.v1.handler import V1
from sizeup.api.v1.limits import Limits
from sizeup.api.v1.principal import Principal
from sizeup.api.v1.rev import rev_of
from sizeup.api.v1.switches import Switches
from sizeup.cad.api import CadApi
from sizeup.fence import FencedWrite
from sizeup.web.server import WebServer
from tests import openapi_spec as oas
from tests.test_radios import Clock, cad, uid

DOC = oas.spec()
ME = Principal("dev-0001", "member@example.org")
OTHER = Principal("dev-0002", "other@example.org")


class Box:
    def __init__(self, enabled=True, **flags):
        self.enabled, self.flags = enabled, {"stream": True, **flags}

    def switches(self, **kw):
        return Switches(lambda: self.enabled, lambda: self.flags, **kw)


def signed_in(principal) -> bool:
    """A device check that accepts every device (PR 2 supplies the real one: revoked and deactivated are refused)."""
    return True


def everyone(principal, change) -> bool:
    """A visibility predicate that shows every reference (PR 2 supplies the real one from role and sourcekeys)."""
    return True


class Rig:
    """A CAD, a change log in hub.sqlite beside it, and a V1 on them. The recorder is driven by hand (`settle()`) unless
    a test starts the real thing."""

    def __init__(self, tmp_path: Path, box: Box | None = None, live: bool = False, **feed_kw):
        self.clock = Clock()
        self.cad = cad(tmp_path, self.clock)
        self.store = self.cad.store
        self.cad.auto.set_mode("pcfd", "Lt. Ruiz")           # dispatching by hand is a PCFD-mode action
        self.box = box or Box()
        self.fence = [True]
        self.log = ChangeLog(tmp_path / "hub.sqlite", lambda: self.fence[0], clock=self.clock)
        kw = dict(batch_s=0.02, heartbeat_s=0.3, tick_s=0.05, fence_ok=lambda: self.fence[0], visible=everyone,
                  valid=signed_in)
        kw.update(feed_kw)
        self.feed = Feed(self.log, cad_follow(self.store), lambda: self.box.switches().allows("stream"), **kw)
        self.feed.bind(self.store)
        if live:
            self.feed.attach(self.store)                # (else the recorder runs when a test says: settle())
        self.v1 = V1(switches=self.box.switches(), resolver=lambda h: ME, changes=self.feed, log=lambda m: None)
        self.path = tmp_path

    def settle(self):
        """What the recorder does after a burst of commits, now."""
        return self.feed.recorder.flush()

    def incident(self, units=("E63",), address="209 Westchester Ave"):
        inc = self.cad.create({"type": "STRU", "address": address, "operator": "Lt. Ruiz"})
        self.cad.dispatch(inc["id"], {"unit_ids": [uid(self.cad, u) for u in units], "operator": "Lt. Ruiz"})
        return inc

    def get(self, query="", headers=None, path="/api/v1/changes"):
        status, body, ctype, h = self.v1.respond("GET", path, query, headers or {})
        return status, (json.loads(body) if isinstance(body, bytes) else body), h

    def close(self):
        self.feed.close()
        self.store.shutdown()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.settle()                      # the baseline: what is on the board when the log is first made
    yield r
    r.close()


def conforms(body, status, path="/api/v1/changes"):
    schema = oas.response_schema(DOC, "GET", path, str(status))
    assert schema is not None
    errs = list(Draft202012Validator(schema).iter_errors(body))
    assert not errs, [e.message for e in errs]


# -- rev -------------------------------------------------------------------

def test_rev_is_the_designs_content_hash():
    obj = {"b": 2, "a": {"z": "é", "y": [1, 2.5]}}
    want = hashlib.sha256(b'{"a":{"y":[1,2.5],"z":"\xc3\xa9"},"b":2}').hexdigest()[:16]
    assert rev_of(obj) == want and len(want) == 16
    assert rev_of({"a": 1, "b": 2}) == rev_of({"b": 2, "a": 1}) != rev_of({"a": 1, "b": 3})


# -- the log: references, ordering, no payload -----------------------------

def test_a_dispatched_call_is_logged_as_references_in_order(rig):
    inc = rig.incident(("E63", "L31"))
    out = rig.settle()
    assert [c.seq for c in out] == sorted(c.seq for c in out) and len({c.seq for c in out}) == len(out)
    kinds = {(c.kind, c.id) for c in out}
    assert ("incident", str(inc["id"])) in kinds
    assert {("unit", str(uid(rig.cad, "E63"))), ("unit", str(uid(rig.cad, "L31")))} <= kinds
    status, body, _ = rig.get("after=0")
    assert status == 200 and [c["seq"] for c in body["changes"]] == sorted(c["seq"] for c in body["changes"])
    assert body["head"] == out[-1].seq and body["cursor"] == body["head"] and body["more"] is False
    conforms(body, 200)


def test_an_entry_is_exactly_a_reference_and_no_payload_leaks(rig):
    rig.incident(address="17 Invented Ct")
    rig.cad.record_traffic({"id": "FG1-9", "start": rig.clock.t, "end": rig.clock.t + 2, "source": "FG1", 
                            "talkgroup": None,
                            "unit": "", "rig": "E63", "radio_id": "6301", "text": "made-up words on the radio",
                            "status": "done"})
    rig.settle()
    status, body, _ = rig.get("after=0")
    text = json.dumps(body)
    for leak in ("17 Invented Ct", "Westchester", "STRU", "made-up words", "Ruiz", "E63", "lat", "lon", "6301"):
        assert leak not in text, leak
    for c in body["changes"]:
        assert set(c) == {"seq", "epoch", "kind", "id", "rev", "at"}
        assert c["kind"] in ("incident", "unit") and len(c["rev"]) == 16
    rows = sqlite3.connect(rig.path / "hub.sqlite").execute("SELECT * FROM mobile_changelog").fetchall()
    assert all(len(r) == 5 for r in rows)                       # seq, kind, id, rev, at: nothing else is stored


def test_radio_traffic_is_not_a_change(rig):
    rig.cad.record_traffic({"id": "FG1-1", "start": rig.clock.t, "end": rig.clock.t + 2, "source": "FG1", 
                            "talkgroup": None,
                            "unit": "", "rig": "", "radio_id": "", "text": "words", "status": "done"})
    assert rig.settle() == []


def test_a_commit_that_changes_nothing_logs_nothing_and_a_real_change_logs_once(rig):
    rig.incident()
    rig.settle()
    head = rig.log.head()
    rig.store.meta_put("noise", "1")
    with rig.store.tx() as db:                                   # a commit that rewrites a row as it was
        db.execute("UPDATE units SET notes = notes")
    assert rig.settle() == [] and rig.log.head() == head
    e63 = uid(rig.cad, "E63")
    rig.store.set_unit_status(e63, "OL", "Lt. Ruiz")
    out = rig.settle()
    # (its event is on the call too)
    assert {(c.kind, c.id) for c in out} == {("unit", str(e63)), ("incident", "1")}
    assert rig.settle() == []


def test_a_closed_call_is_logged_once_more_and_then_left_alone(rig):
    inc = rig.incident()
    rig.settle()
    rig.cad.close(inc["id"], {"disposition": "FALSE", "operator": "Lt. Ruiz"})
    out = rig.settle()
    assert ("incident", str(inc["id"])) in {(c.kind, c.id) for c in out}
    key = ("incident", str(inc["id"]))
    # closed, still in the reads' list: kept as a head, no further entry
    assert key in rig.log.heads() and rig.settle() == []
    rig.clock.t += 2 * 86400                                                  # it leaves the list (closed long ago)
    with rig.store.tx():
        pass
    head = rig.log.head()
    assert rig.settle() == [] and key not in rig.log.heads() and rig.log.head() == head   # dropped: not a deletion


def test_a_deleted_object_is_a_row_with_no_rev(rig):
    rig.log.append([("unit", "999", "aaaaaaaaaaaaaaaa")])
    with rig.store.tx():                                                     # (a commit, so the recorder looks)
        pass
    out = rig.settle()                                                       # the scan does not find unit 999
    assert [(c.kind, c.id, c.rev) for c in out] == [("unit", "999", "")]


def test_the_cad_is_untouched_by_the_log(rig):
    rig.incident()
    tables = lambda: [r[0] for r in rig.store.db.execute("SELECT name FROM sqlite_master ORDER BY name")]
    t0 = tables()
    rig.settle()
    assert tables() == t0 and not [t for t in t0 if "changelog" in t]
    assert {r[0] for r in sqlite3.connect(rig.path / "hub.sqlite").execute("SELECT name FROM sqlite_master")} >= {
        "mobile_changelog", "mobile_changelog_head", "mobile_changelog_meta"}


# -- /changes --------------------------------------------------------------

def test_no_cursor_answers_the_head_and_no_changes(rig):
    rig.incident()
    rig.settle()
    status, body, _ = rig.get("")
    assert status == 200 and body["changes"] == [] and body["cursor"] == body["head"] == rig.log.head() > 0
    assert body["epoch"] == rig.log.epoch
    conforms(body, 200)


def test_paging_by_limit_and_the_cursor(rig):
    rig.incident(("E63", "L31", "E60"))
    rig.settle()
    head = rig.log.head()
    seen, after = [], 0
    for _ in range(20):
        status, body, _ = rig.get(f"after={after}&limit=2&epoch={rig.log.epoch}")
        assert status == 200 and len(body["changes"]) <= 2
        seen += [c["seq"] for c in body["changes"]]
        after = body["cursor"]
        if not body["more"]:
            break
    assert seen == sorted(set(seen)) and seen[-1] == head and after == head


@pytest.mark.parametrize("query,code", [("after=x", "bad_cursor"), ("after=-1", "bad_cursor"),
                                        ("after=1.5", "bad_cursor"),
                                        ("after=0&limit=0", "bad_limit"), ("after=0&limit=501", "bad_limit"),
                                        ("after=0&limit=abc", "bad_limit")])
def test_bad_input_is_a_400(rig, query, code):
    status, body, _ = rig.get(query)
    assert status == 400 and body["error"]["code"] == code
    conforms(body, 400)


def test_an_epoch_that_is_not_the_logs_is_a_409_and_so_is_a_cursor_from_the_future(rig):
    rig.incident()
    rig.settle()
    status, body, _ = rig.get("after=1&epoch=deadbeef0000")
    assert status == 409 and body["error"]["code"] == "epoch_changed" and body["error"]["epoch"] == rig.log.epoch
    conforms(body, 409)
    status, body, _ = rig.get(f"after={rig.log.head() + 5}&epoch={rig.log.epoch}")
    assert status == 409


def test_a_reset_is_a_new_epoch_and_the_old_cursor_is_refused(rig):
    rig.incident()
    rig.settle()
    old = (rig.log.epoch, rig.log.head())
    new = rig.log.reset()
    assert new != old[0]
    status, body, _ = rig.get(f"after={old[1]}&epoch={old[0]}")
    assert status == 409 and body["error"]["epoch"] == new
    rig.settle()                                                              # the log goes on from a new baseline
    assert rig.get(f"after=0&epoch={new}")[0] == 200


def test_retention_expiry_is_a_410_with_the_oldest_cursor_served(tmp_path):
    r = Rig(tmp_path)
    try:
        r.log.retention_s = 100.0
        r.settle()
        inc = r.incident()
        r.settle()
        old_head = r.log.head()
        r.clock.t += 500
        r.store.set_unit_status(uid(r.cad, "E63"), "OL", "Lt. Ruiz")
        r.settle()
        assert r.log.prune(force=True) == old_head                            # everything up to then is past retention
        status, body, _ = r.get("after=0")
        assert status == 410 and body["error"]["code"] == "cursor_too_old" and body["error"]["oldest"] == old_head
        conforms(body, 410)
        assert r.get(f"after={old_head}")[0] == 200                           # the oldest cursor is still served
        assert [c["seq"] for c in r.get(f"after={old_head}")[1]["changes"]][-1] == r.log.head()
    finally:
        r.close()


def test_the_log_is_bounded_in_rows_too(tmp_path):
    r = Rig(tmp_path)
    try:
        r.log.max_rows = 3
        r.settle()
        r.incident(("E63", "L31", "E60"))
        r.settle()
        assert r.log.head() > 3
        r.log.prune(force=True)
        assert len(r.log.since(0, 100)) == 3 and r.log.floor() == r.log.head() - 3
    finally:
        r.close()


def test_a_cursor_resumes_across_a_handoff_without_a_resnapshot(tmp_path):
    """The next task opens the same database: same epoch, same seq; what changed meanwhile is found by the heads."""
    r = Rig(tmp_path)
    r.settle()
    r.incident()
    r.settle()
    epoch, cursor = r.log.epoch, r.log.head()
    r.feed.close()
    r.store.set_unit_status(uid(r.cad, "L31"), "RS", "Lt. Ruiz")        # a change nobody logged (the old task is gone)
    r.log = ChangeLog(tmp_path / "hub.sqlite", clock=r.clock)                 # the new task
    r.feed = Feed(r.log, cad_follow(r.store), lambda: True, visible=everyone, valid=signed_in)
    r.feed.bind(r.store)
    r.v1 = V1(switches=r.box.switches(), resolver=lambda h: ME, changes=r.feed, log=lambda m: None)
    try:
        assert r.log.epoch == epoch
        out = r.settle()
        assert [(c.kind, c.id) for c in out] == [("unit", str(uid(r.cad, "L31")))] and out[0].seq == cursor + 1
        status, body, _ = r.get(f"after={cursor}&epoch={epoch}")
        assert status == 200 and [c["seq"] for c in body["changes"]] == [cursor + 1]
    finally:
        r.close()


# -- who sees what ---------------------------------------------------------

def test_the_kinds_and_a_per_person_predicate_narrow_what_is_returned(tmp_path):
    r = Rig(tmp_path, visible=lambda p, c: not (p == ME and c.kind == "unit"))
    try:
        r.settle()
        r.incident()
        r.settle()
        status, body, _ = r.get("after=0")
        assert body["changes"] and {c["kind"] for c in body["changes"]} == {"incident"}
        # the cursor still moves past the hidden
        assert body["cursor"] == body["head"]
        r.v1.resolver = lambda h: OTHER
        assert {c["kind"] for c in r.get("after=0")[1]["changes"]} == {"incident", "unit"}
        # another consumer's kinds (the machine feed)
        r.feed.kinds = frozenset({"unit"})
        assert {c["kind"] for c in r.get("after=0")[1]["changes"]} == {"unit"}
    finally:
        r.close()


# -- switches, auth, flags -------------------------------------------------

def test_switch_off_and_flag_off_are_404_and_no_resolver_or_log_runs(rig):
    for box in (Box(enabled=False), Box(stream=False)):
        resolved = []
        v1 = V1(switches=box.switches(), resolver=lambda h: resolved.append(1) or ME, changes=rig.feed)
        for path in ("/api/v1/changes", "/api/v1/stream"):
            assert v1.respond("GET", path, "after=0", {})[0] == 404
        assert resolved == []


def test_no_token_is_a_401_and_the_other_methods_are_405(rig):
    anon = V1(switches=rig.box.switches(), changes=rig.feed)
    for path in ("/api/v1/changes", "/api/v1/stream"):
        assert anon.respond("GET", path, "", {})[0] == 401
        assert rig.v1.respond("POST", path, "", {})[0] == 405


def test_the_routes_need_the_read_role_and_the_stream_flag():
    from sizeup.api.v1.routes import ROUTES
    got = {r.path: r for r in ROUTES}
    for p in ("/api/v1/changes", "/api/v1/stream"):
        assert (got[p].need, got[p].flag, got[p].role) == ("device", "stream", "read")


def test_a_fenced_task_answers_with_the_handover_signal_and_writes_nothing(rig):
    rig.incident()
    rig.settle()
    head = rig.log.head()
    rig.fence[0] = False
    with pytest.raises(FencedWrite):
        rig.get("after=0")                                                    # the server answers it as the 503
    with pytest.raises(FencedWrite):
        rig.get("", path="/api/v1/stream")
    rig.store.set_unit_status(uid(rig.cad, "L31"), "RS", "Lt. Ruiz")     # (the CAD's own fence is not this fence)
    assert rig.settle() == []
    rig.fence[0] = True
    assert rig.log.head() == head


def test_a_log_that_cannot_be_written_is_said_once_and_never_reaches_the_cad(tmp_path):
    said = []
    r = Rig(tmp_path, report=said.append)
    try:
        r.settle()
        r.log.append = lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error"))
        r.incident()
        assert r.settle() == [] and r.settle() == []
        assert said == ["api v1: the change log could not be written (OperationalError)"]
        assert r.store.q("SELECT count(*) AS n FROM incidents")[0]["n"] == 1
    finally:
        r.close()


# -- nothing at startup, nothing while the flag is off, lazily -------------

def test_nothing_is_made_or_written_until_the_flag_is_on(tmp_path):
    r = Rig(tmp_path, Box(stream=False), live=True)
    try:
        r.incident()
        r.store.set_unit_status(uid(r.cad, "E63"), "OL", "Lt. Ruiz")
        # no thread, either (a poke is synchronous)
        assert r.feed.recorder._thread is None
        assert not (tmp_path / "hub.sqlite").exists()
        assert r.feed.recorder.flush() == []
        assert not (tmp_path / "hub.sqlite").exists()
    finally:
        r.close()


def test_the_recorder_thread_writes_a_burst_of_commits_as_one_transaction(tmp_path):
    r = Rig(tmp_path, live=True, batch_s=1.0)
    try:
        # (the baseline's transaction is counted out)
        r.settle()
        before = r.log.commits
        # a burst of commits, well inside the window
        for _ in range(3):
            r.store.set_unit_status(uid(r.cad, "E63"), "RS", "Lt. Ruiz")
            r.store.set_unit_status(uid(r.cad, "E63"), "OL", "Lt. Ruiz")
        wait_for(lambda: r.log.head())
        assert [c.kind for c in r.log.since(0, 50)] == ["unit"]                # the last state, once: not six entries
        assert r.log.commits == before + 1                                     # and one transaction, not one a commit
        assert r.feed.recorder._thread.name == "v1-changelog"
    finally:
        r.close()


def test_a_cad_commit_never_waits_on_the_log(tmp_path):
    r = Rig(tmp_path, live=True)
    try:
        r.settle()
        t = time.time()
        for i in range(30):
            r.store.set_unit_status(uid(r.cad, "E63"), "RS" if i % 2 else "OL", "Lt. Ruiz")
        assert time.time() - t < 5                                              # (a poke is an Event.set)
    finally:
        r.close()


# -- the stream ------------------------------------------------------------

class Sse:
    """A client of /api/v1/stream over a real socket: events parsed as they come."""

    def __init__(self, port, path="/api/v1/stream", headers=None):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.conn.request("GET", path, headers=headers or {})
        self.sock = self.conn.sock
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        self.buf = b""

    def event(self, timeout=5.0):
        """The next event as (name, id, data), skipping comments; None at the end of the stream."""
        self.sock.settimeout(timeout)
        while True:
            while b"\n\n" not in self.buf:
                chunk = self.resp.fp.read1(4096) if hasattr(self.resp.fp, "read1") else self.resp.read(1)
                if not chunk:
                    return None
                self.buf += chunk
            raw, self.buf = self.buf.split(b"\n\n", 1)
            fields = {}
            for line in raw.decode().split("\n"):
                if line.startswith(":") or not line:
                    continue
                k, _, v = line.partition(":")
                fields[k] = v.strip() if k != "data" else json.loads(v)
            if "event" in fields:
                return fields["event"], fields.get("id"), fields.get("data")

    def until(self, name, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            e = self.event(timeout)
            if e is None or e[0] == name:
                return e
        raise AssertionError(f"no {name} event")

    def close(self):
        self.resp.close()                       # (the response holds the socket too)
        self.conn.close()


@pytest.fixture
def hub(tmp_path):
    """A real WebServer over a real CAD, mobile on with the stream flag, a test resolver (the person is the
    X-Test-Device header). Its change feed is fast."""
    box = Box()
    clock = Clock()
    c = cad(tmp_path, clock)
    c.auto.set_mode("pcfd", "Lt. Ruiz")
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda x: None, lambda i: False, cad=CadApi(c), role="hub",
                    mobile=box.switches(max_streams=3),
                    mobile_resolver=lambda h: Principal(h.get("X-Test-Device"), f"{h.get('X-Test-Device')}@example.org")
                    if h.get("X-Test-Device") else None)
    v1 = srv.httpd.RequestHandlerClass.v1
    v1.changes.heartbeat_s, v1.changes.tick_s, v1.changes.recorder.batch_s = 0.3, 0.05, 0.02
    v1.changes.visible, v1.changes.valid = everyone, signed_in
    v1.changes.recorder.prepare()             # the recorder's first run (a reader is told 503 until it has been)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    h = type("Hub", (), {})()
    h.srv, h.box, h.cad, h.v1, h.feed, h.port = srv, box, c, v1, v1.changes, srv.httpd.server_address[1]
    h.path, h.clock = tmp_path, clock
    h.head = lambda: h.feed.log.head()
    yield h
    srv.shutdown()
    h.feed.close()


def dev(name="dev-1"):
    return {"X-Test-Device": name}


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_the_stream_says_hello_then_each_change_with_its_cursor_and_server_time(hub):
    s = Sse(hub.port, headers=dev())
    assert s.status == 200 and s.resp.getheader("Content-Type") == "text/event-stream"
    name, id_, hello = s.event()
    assert name == "hello" and id_ is None and hello["epoch"] == hub.feed.log.epoch and hello["server_time"] > 0
    inc = hub.cad.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "Lt. Ruiz"})
    hub.cad.dispatch(inc["id"], {"unit_ids": [uid(hub.cad, "E63")], "operator": "Lt. Ruiz"})
    got = []
    while len(got) < 2:
        name, id_, d = s.event()
        if name == "change":
            got.append((id_, d))
    s.close()
    assert [d["seq"] for _, d in got] == sorted(d["seq"] for _, d in got)
    for id_, d in got:
        assert id_ == f"{d['epoch']}-{d['seq']}" and d["server_time"] > 0
        assert set(d) == {"seq", "epoch", "kind", "id", "rev", "at", "server_time"}
    assert {"incident", "unit"} & {d["kind"] for _, d in got}


def test_a_stream_resumes_from_last_event_id_with_what_it_missed_and_nothing_twice(hub):
    s = Sse(hub.port, headers=dev())
    s.event()
    hub.cad.create({"type": "STRU", "address": "5 Sample Way", "operator": "Lt. Ruiz"})
    first = s.until("change")
    s.close()
    wait_for(lambda: hub.v1.limits.streams() == 0)
    hub.cad.create({"type": "ALARM", "address": "8 Example Rd", "operator": "Lt. Ruiz"})        # while it was away
    wait_for(lambda: hub.head() > int(first[1].split("-")[1]))
    s2 = Sse(hub.port, headers={**dev(), "Last-Event-ID": first[1]})
    assert s2.status == 200
    name, _, hello = s2.event()
    assert name == "hello"
    nxt = s2.until("change")
    assert int(nxt[1].split("-")[1]) == int(first[1].split("-")[1]) + 1                         # the very next one
    s2.close()
    wait_for(lambda: hub.v1.limits.streams() == 0)
    # ?after= and ?epoch= do the same for a client that cannot set a header
    s3 = Sse(hub.port, f"/api/v1/stream?after={first[1].split('-')[1]}&epoch={hub.feed.log.epoch}", dev())
    assert s3.until("change")[1] == nxt[1]
    s3.close()


def test_resume_across_a_handoff_the_new_task_serves_the_same_cursor(hub):
    """The cursor is the database's: a second feed on the same file (the next task) answers the same resume."""
    hub.cad.create({"type": "STRU", "address": "5 Sample Way", "operator": "Lt. Ruiz"})
    wait_for(lambda: hub.head())
    epoch, head = hub.feed.log.epoch, hub.head()
    nxt = Feed(ChangeLog(hub.path / "hub.sqlite"), cad_follow(hub.cad.store), lambda: True, tick_s=0.05,
               visible=everyone, valid=signed_in)
    nxt.bind(hub.cad.store)
    nxt.recorder.prepare()
    v1 = V1(switches=hub.box.switches(), resolver=lambda h: ME, changes=nxt)
    status, body, _, _ = v1.respond("GET", "/api/v1/changes", f"after=0&epoch={epoch}", {})
    assert status == 200 and json.loads(body)["head"] == head
    nxt.close()


def test_resume_with_the_wrong_epoch_or_too_old_is_refused_before_the_stream_opens(hub):
    hub.cad.create({"type": "STRU", "address": "5 Sample Way", "operator": "Lt. Ruiz"})
    wait_for(lambda: hub.head())
    s = Sse(hub.port, headers={**dev(), "Last-Event-ID": f"badepoch0000-{hub.head()}"})
    assert s.status == 409 and json.loads(s.resp.read())["error"]["code"] == "epoch_changed"
    s = Sse(hub.port, headers={**dev(), "Last-Event-ID": "garbage"})
    assert s.status == 400
    hub.feed.log.max_rows = 1
    hub.cad.create({"type": "STRU", "address": "6 Sample Way", "operator": "Lt. Ruiz"})
    wait_for(lambda: hub.head() >= 2)
    hub.feed.log.prune(force=True)
    s = Sse(hub.port, f"/api/v1/stream?after=0&epoch={hub.feed.log.epoch}", dev())
    body = json.loads(s.resp.read())
    assert s.status == 410 and body["error"]["code"] == "cursor_too_old" and body["error"]["oldest"] >= 1


def test_a_reset_ends_an_open_stream_so_the_app_resnapshots(hub):
    s = Sse(hub.port, headers=dev())
    s.event()
    hub.feed.log.reset()
    e = s.until("close")
    assert e[2]["reason"] == "epoch_changed"
    assert s.event() is None
    s.close()


def test_a_heartbeat_comes_when_idle_and_moves_no_cursor(hub):
    s = Sse(hub.port, headers=dev())
    s.event()
    t = time.time()
    name, id_, d = s.event(3)
    assert name == "heartbeat" and id_ is None and d["server_time"] > 0 and time.time() - t < 2
    name2, _, _ = s.event(3)
    assert name2 == "heartbeat"
    s.close()


def test_the_default_heartbeat_is_fifteen_seconds_and_nothing_else_is_pushed_idle():
    from sizeup.api.v1.changelog import Feed as F
    assert F(ChangeLog(":memory:")).heartbeat_s == 15.0


def test_two_streams_per_device_the_third_is_a_429_and_a_closed_one_gives_its_slot_back(hub):
    a, b = Sse(hub.port, headers=dev("dev-1")), Sse(hub.port, headers=dev("dev-1"))
    assert (a.status, b.status) == (200, 200)
    a.event(), b.event()
    c = Sse(hub.port, headers=dev("dev-1"))
    body = json.loads(c.resp.read())
    assert c.status == 429 and body["error"]["code"] == "too_many_streams" and c.resp.getheader("Retry-After") == "10"
    other = Sse(hub.port, headers=dev("dev-2"))                                  # another device: a slot (global cap 3)
    assert other.status == 200
    full = Sse(hub.port, headers=dev("dev-3"))
    assert full.status == 429                                                    # the global cap
    a.close()
    wait_for(lambda: hub.v1.limits.streams() == 2)
    again = Sse(hub.port, headers=dev("dev-1"))
    assert again.status == 200
    for s in (b, other, again):
        s.close()
    wait_for(lambda: hub.v1.limits.streams() == 0)


def test_a_stream_holds_no_request_slot_and_the_console_still_answers(hub):
    s = [Sse(hub.port, headers=dev(f"dev-{i}")) for i in range(2)]
    for x in s:
        x.event()
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    c.request("GET", "/api/v1/meta")
    assert c.getresponse().status == 200
    assert hub.v1.limits.enter() and (hub.v1.limits.leave() or True)
    for x in s:
        x.close()


def test_a_slow_clients_queue_is_bounded_at_256_and_it_is_dropped_not_buffered(tmp_path):
    r = Rig(tmp_path, queue=256)
    try:
        r.settle()
        assert Feed(r.log).queue == 256                                                      # the real bound
        sub = r.feed.subscribe()
        r.feed._notify([Change(i, "unit", "1", "a" * 16, 1.0) for i in range(1, 257)])       # exactly the bound: kept
        assert not sub.dropped and len(sub.get(0.01)) == 256
        r.feed._notify([Change(i, "unit", "1", "a" * 16, 1.0) for i in range(257, 514)])     # one more than it holds
        assert sub.dropped and sub.get(0.01) == []
    finally:
        r.close()


def test_a_dropped_stream_ends_with_a_reason_and_resumes_from_the_database(hub):
    s = Sse(hub.port, headers=dev())
    s.event()
    wait_for(lambda: hub.feed._subs)
    # a burst past 256: no manual flag
    hub.feed._notify([Change(i, "unit", "1", "a" * 16, 1.0) for i in range(1, 300)])
    e = s.until("close")
    assert e[2]["reason"] == "slow"
    s.close()
    wait_for(lambda: hub.v1.limits.streams() == 0)
    for i in range(1, 5):
        hub.cad.create({"type": "STRU", "address": f"{i} Sample Way", "operator": "Lt. Ruiz"})
    wait_for(lambda: hub.head() >= 4)
    s2 = Sse(hub.port, headers={**dev(), "Last-Event-ID": f"{hub.feed.log.epoch}-0"})
    assert s2.status == 200
    assert s2.until("change")                                # everything it missed is served
    s2.close()


def test_a_fenced_hub_refuses_a_stream_with_the_503_and_an_open_one_ends_cleanly(hub):
    lease = [True]
    hub.srv.gate.fence = type("F", (), {"ok": staticmethod(lambda: lease[0])})()
    hub.feed.fence_ok = lambda: lease[0]
    s = Sse(hub.port, headers=dev())
    assert s.status == 200
    s.event()
    lease[0] = False
    e = s.until("close")
    assert e[2]["reason"] == "handover"
    assert s.event() is None
    s.close()
    wait_for(lambda: hub.v1.limits.streams() == 0)
    s = Sse(hub.port, headers=dev())
    body = json.loads(s.resp.read())
    assert s.status == 503 and body["error"]["code"] == "updating" and s.resp.getheader("Retry-After") == "2"
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    c.request("GET", "/api/v1/changes?after=0", headers=dev())
    assert c.getresponse().status == 503


def test_a_drain_ends_the_streams_and_writes_the_last_batch(hub):
    s = Sse(hub.port, headers=dev())
    s.event()
    hub.cad.create({"type": "STRU", "address": "5 Sample Way", "operator": "Lt. Ruiz"})
    hub.srv.drain(2)
    e = s.until("close")
    assert e[2]["reason"] == "stopping"
    s.close()


def test_turning_the_flag_or_the_switch_off_ends_an_open_stream_and_the_route_is_a_404(hub):
    s = Sse(hub.port, headers=dev())
    s.event()
    hub.box.flags["stream"] = False
    assert s.until("close")[2]["reason"] == "stopping"
    s.close()
    assert Sse(hub.port, headers=dev()).status == 404
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    c.request("GET", "/api/v1/changes", headers=dev())
    assert c.getresponse().status == 404
    hub.box.flags["stream"] = True
    s = Sse(hub.port, headers=dev())
    s.event()
    hub.box.enabled = False
    assert s.until("close")[2]["reason"] == "stopping"
    s.close()


def test_the_stream_carries_only_what_the_person_may_see(hub):
    hub.feed.visible = lambda p, c: c.kind == "incident"
    s = Sse(hub.port, headers=dev())
    s.event()
    inc = hub.cad.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "Lt. Ruiz"})
    hub.cad.dispatch(inc["id"], {"unit_ids": [uid(hub.cad, "E63")], "operator": "Lt. Ruiz"})
    kinds, raw = set(), ""
    end = time.time() + 1.5
    while time.time() < end:
        e = s.event(1.0)
        if e and e[0] == "change":
            kinds.add(e[2]["kind"])
            raw += json.dumps(e[2])
    s.close()
    assert kinds == {"incident"} and "Westchester" not in raw


def test_head_and_the_station_and_no_token(hub):
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    c.request("GET", "/api/v1/stream")
    assert c.getresponse().status == 401
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    c.request("HEAD", "/api/v1/stream", headers=dev())
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream" and r.read() == b""
    wait_for(lambda: hub.v1.limits.streams() == 0)


def test_changes_through_the_server_validates_against_the_spec(hub):
    hub.cad.create({"type": "STRU", "address": "5 Sample Way", "operator": "Lt. Ruiz"})
    wait_for(lambda: hub.head())
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    c.request("GET", f"/api/v1/changes?after=0&epoch={hub.feed.log.epoch}", headers=dev())
    r = c.getresponse()
    body = json.loads(r.read())
    assert r.status == 200 and r.getheader("Cache-Control") == "no-store"
    conforms(body, 200)


def test_a_stream_that_raises_is_a_clean_end_and_gives_its_slot_back(tmp_path):
    r = Rig(tmp_path)
    try:
        r.settle()
        status, body, ctype, h = r.v1.respond("GET", "/api/v1/stream", "", {})
        assert status == 200 and ctype == "text/event-stream" and r.v1.limits.streams() == 1

        def write(b):
            raise BrokenPipeError
        with pytest.raises(BrokenPipeError):
            body.run(write)
        body.close()
        body.close()                                                                  # once, however many times
        assert r.v1.limits.streams() == 0 and not r.feed._subs
    finally:
        r.close()


# -- cost and startup ------------------------------------------------------

def test_the_log_costs_a_few_wal_frames_per_batch_and_startup_writes_nothing(tmp_path, capsys):
    """Litestream ships each WAL frame (a 4 KB page): the cost of N changes on a seeded hub.sqlite, in one batch and one
    change at a time, and a hub start afterwards (init_state) still writes next to nothing."""
    from sizeup.app import init_state
    from sizeup.cloud.hubstore import MonitorDb
    from tests.test_startup_writes import STEADY_FRAMES, hub_with_a_radio_log, wal_frames_written
    cfg, paths = hub_with_a_radio_log(tmp_path)
    hub = paths[1]
    assert hub.name == "hub.sqlite"
    clock = Clock()
    log = ChangeLog(hub, clock=clock)
    # the three tables, made on first use
    first = wal_frames_written([hub], lambda: log.epoch)["hub"]
    assert first <= 12
    log.baseline([("unit", str(i), "0" * 16) for i in range(250)])                  # a board's heads, once
    n = 100
    entries = [("unit", str(i), f"{i:016x}") for i in range(n)]
    batch = wal_frames_written([hub], lambda: log.append(entries))["hub"]
    rev = [0]

    def one_by_one():
        for i in range(n):
            rev[0] += 1
            log.append([("unit", str(i), f"{rev[0] + 1000:016x}")])
    single = wal_frames_written([hub], one_by_one)["hub"]
    print(f"WAL frames on a seeded hub.sqlite: first use {first}; {n} changes in one batch {batch} "
          f"({batch / n:.2f} per change); {n} changes one transaction each {single} ({single / n:.2f} per change)")
    # a batch shares pages; alone: a few
    assert batch <= n // 4 + 6 and single <= 4 * n
    log.close()
    assert wal_frames_written(paths, lambda: init_state(cfg))["hub"] <= STEADY_FRAMES      # a start writes nothing
    # an older release opens it as before
    MonitorDb(hub).db.close()
    names = {r[0] for r in sqlite3.connect(hub).execute("SELECT name FROM sqlite_master")}
    assert names >= {"traffic", "mobile_changelog"}


def test_a_hub_start_with_mobile_off_makes_no_table_and_writes_nothing(tmp_path):
    from sizeup.app import init_state
    from tests.test_startup_writes import hub_with_a_radio_log, wal_frames_written
    cfg, paths = hub_with_a_radio_log(tmp_path)
    before = {r[0] for r in sqlite3.connect(paths[1]).execute("SELECT name FROM sqlite_master")}
    wal_frames_written(paths, lambda: init_state(cfg))
    assert {r[0] for r in sqlite3.connect(paths[1]).execute("SELECT name FROM sqlite_master")} == before
    assert not [n for n in before if "changelog" in n]


def test_the_server_makes_the_feed_only_for_a_file_database_and_never_for_a_station(tmp_path):
    c = cad(tmp_path, Clock())
    mem = cad(None, Clock())
    for role, cd, want in (("hub", c, True), ("station", c, False), ("hub", mem, False)):
        srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda x: None, lambda i: False, cad=CadApi(cd), role=role)
        assert (srv.httpd.RequestHandlerClass.v1.changes is not None) is want, (role, cd.store.path)
        srv.httpd.server_close()
    assert not (tmp_path / "hub.sqlite").exists()                  # (and a server that only started made no file)


# -- review round 1: locks, restores, fail closed, cursors -----------------

def test_a_commit_while_the_baseline_scans_never_deadlocks_the_cad(tmp_path):
    """A request-side baseline waits for the CAD's lock inside its scan while a CAD commit (which holds that lock) pokes
    the recorder: the poke must never wait on anything the baseline holds. Both threads must finish."""
    r = Rig(tmp_path, live=True)
    in_scan = threading.Event()
    r.feed.recorder.follow = [(k, lambda heads, scan=scan: (in_scan.set(), scan(heads))[1])
                              for k, scan in r.feed.recorder.follow]
    anchor = r.feed.recorder.anchor                   # (its first call into the CAD is the baseline's first wait)
    r.feed.recorder.anchor = anchor._replace(get=lambda key: (in_scan.set(), anchor.get(key))[1])
    prepare = r.feed.recorder.prepare

    holds_cad_lock = threading.Event()

    def commit():
        with r.store.tx() as db:                      # holds the CAD's lock...
            db.execute("UPDATE units SET notes = notes")
            holds_cad_lock.set()
            assert in_scan.wait(10)                   # ...until the baseline is about to call into the CAD...
            time.sleep(0.3)                           # ...and is blocked on that lock. (The one sleep: nothing outside
            #                                           sqlite can say "blocked on a lock"; the commit's poke follows.)
    b = threading.Thread(target=commit, daemon=True)
    a = threading.Thread(target=prepare, daemon=True)
    b.start()
    assert holds_cad_lock.wait(10)                    # the baseline starts only once the commit holds the CAD's lock
    a.start()
    a.join(10)
    b.join(10)
    stuck = a.is_alive() or b.is_alive()
    if not stuck:
        r.close()                                     # (a deadlocked rig is left: closing waits on its locks)
    assert not stuck, "the CAD's commit and the baseline wait on each other"


def make_old_copy(hub_path: Path, to: Path) -> None:
    """A consistent copy of hub.sqlite as it is now (what a Litestream restore of an older point would give back)."""
    src, dst = sqlite3.connect(hub_path), sqlite3.connect(to)
    src.backup(dst)
    src.close()
    dst.close()


def swap_in(new: Path, hub_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{hub_path}{suffix}").unlink(missing_ok=True)
    new.rename(hub_path)


def next_task(r: Rig) -> Rig:
    """The next task on the same CAD and the (possibly restored) hub.sqlite: a new log, feed and V1, prepared."""
    r.log = ChangeLog(r.path / "hub.sqlite", clock=r.clock)
    r.feed = Feed(r.log, cad_follow(r.store), lambda: True, visible=everyone, valid=signed_in)
    r.feed.bind(r.store)
    r.v1 = V1(switches=r.box.switches(), resolver=lambda h: ME, changes=r.feed, log=lambda m: None)
    r.feed.recorder.prepare()
    return r


def test_a_restored_older_hub_database_after_a_clean_drain_is_a_new_epoch_and_an_old_cursor_is_a_409(tmp_path):
    r = Rig(tmp_path, live=True)
    r.feed.recorder.prepare()
    make_old_copy(tmp_path / "hub.sqlite", tmp_path / "older.sqlite")             # the log as it was at its start
    r.incident()
    wait_for(lambda: r.log.head() >= 2)
    epoch, head = r.log.epoch, r.log.head()
    # a clean drain: the CAD is told the head
    r.feed.close()
    r.log.close()
    swap_in(tmp_path / "older.sqlite", tmp_path / "hub.sqlite")                   # ...and the older database comes back
    try:
        next_task(r)
        assert r.log.epoch != epoch and r.log.head() == 0
        status, body, _ = r.get(f"after={head}&epoch={epoch}")
        assert status == 409 and body["error"]["epoch"] == r.log.epoch
        assert r.get(f"after=1&epoch={epoch}")[0] == 409                           # below the restored head, too
    finally:
        r.close()


def test_a_different_or_older_cad_against_the_same_log_is_a_new_epoch(tmp_path):
    r = Rig(tmp_path / "one")
    r.settle()
    r.incident()
    r.settle()
    epoch = r.log.epoch
    r.feed.close()
    # a CAD restored to before: lower ids, no mark
    older = Rig(tmp_path / "two")
    (tmp_path / "one" / "hub.sqlite").replace(tmp_path / "two" / "hub.sqlite")
    older.log = ChangeLog(tmp_path / "two" / "hub.sqlite", clock=older.clock)
    older.feed = Feed(older.log, cad_follow(older.store), lambda: True, visible=everyone, valid=signed_in)
    older.feed.bind(older.store)
    older.v1.changes = older.feed
    older.feed.recorder.prepare()
    try:
        assert older.log.epoch != epoch
        assert older.get(f"after=1&epoch={epoch}")[0] == 409
        assert older.log.baselined() and older.get("")[1]["epoch"] == older.log.epoch
    finally:
        older.close()
        r.store.shutdown()


def test_a_log_lost_with_its_database_is_a_new_epoch_not_the_cads_old_one(tmp_path):
    r = Rig(tmp_path)
    r.settle()
    epoch = r.log.epoch
    r.feed.close()
    swap_in_path = tmp_path / "hub.sqlite"
    for suffix in ("", "-wal", "-shm"):
        # restored from before the log existed
        Path(f"{swap_in_path}{suffix}").unlink(missing_ok=True)
    try:
        next_task(r)
        assert r.log.epoch != epoch and r.store.meta_get("mobile_log_epoch") == r.log.epoch
    finally:
        r.close()


def test_a_clean_handoff_on_the_same_databases_keeps_the_epoch_and_the_cursor(tmp_path):
    r = Rig(tmp_path, live=True)
    r.feed.recorder.prepare()
    r.incident()
    wait_for(lambda: r.log.head() >= 2)
    epoch, head = r.log.epoch, r.log.head()
    r.feed.close()
    r.log.close()
    try:
        next_task(r)
        assert r.log.epoch == epoch and r.log.head() == head
        assert r.get(f"after={head}&epoch={epoch}")[0] == 200
    finally:
        r.close()


def test_the_feed_fails_closed_until_a_visibility_predicate_is_supplied(tmp_path):
    r = Rig(tmp_path, visible=None)
    try:
        r.settle()
        r.incident()
        r.settle()
        assert not r.feed.serving() and not r.feed.allowed(ME, Change(1, "unit", "1", "a" * 16, 1.0))
        for path in ("/api/v1/changes", "/api/v1/stream"):
            status, body, h = r.get("after=0", path=path)
            assert status == 503 and body["error"]["code"] == "unavailable" and h["Retry-After"] == "30"
        r.feed.visible = everyone
        assert r.get("after=0")[0] == 200
    finally:
        r.close()


def test_the_first_reader_is_told_to_retry_while_the_recorder_makes_the_baseline(tmp_path):
    r = Rig(tmp_path, live=True)
    try:
        status, body, h = r.get("after=0")
        assert status == 503 and body["error"]["code"] == "unavailable" and h["Retry-After"] == "1"
        assert body["error"]["retry_after_s"] == 1                                  # the header says what the body says
        wait_for(lambda: r.feed.recorder.ready)
        assert r.get("")[0] == 200
    finally:
        r.close()


@pytest.mark.parametrize("after", ["1_0", "+5", "%205", "5%20", "%EF%BC%95", "1e2", "0x10", "9" * 19])
def test_a_cursor_is_digits_and_nothing_else(rig, after):
    status, body, _ = rig.get(f"after={after}")
    assert status == 400 and body["error"]["code"] == "bad_cursor"
    assert rig.get(f"after=0&limit={after}")[0] == 400


def test_limits_the_largest_and_the_default(rig):
    rig.incident(("E63", "L31", "E60"))
    rig.settle()
    assert rig.get("after=0&limit=500")[0] == 200 and rig.get("after=0&limit=501")[0] == 400
    status, body, _ = rig.get("after=0")
    assert status == 200 and len(body["changes"]) <= 100 and body["head"] == rig.log.head()


def test_an_open_stream_is_cut_off_when_its_device_is_no_longer_valid(hub, monkeypatch):
    import sizeup.api.v1.stream as stream
    monkeypatch.setattr(stream, "VALID_EVERY_S", 0.05)
    s = Sse(hub.port, headers=dev())
    s.event()
    hub.feed.valid = lambda p: False                                                # revoked in Settings, mid-stream
    assert s.until("close")[2]["reason"] == "revoked"
    s.close()


def test_a_long_replay_looks_at_its_stop_conditions_between_pages(rig, monkeypatch):
    import sizeup.api.v1.stream as stream
    monkeypatch.setattr(stream, "BACKLOG_ROWS", 1)
    rig.incident(("E63", "L31", "E60"))
    rig.settle()
    assert rig.log.head() >= 4
    status, body, _, _ = rig.v1.respond("GET", "/api/v1/stream", "after=0", {})
    written = []
    why = body.run(lambda raw: written.append(raw), lambda: len([w for w in written if b"event: change" in w]) >= 2)
    body.close()
    assert why == "stopping" and len([w for w in written if b"event: change" in w]) == 2     # not the whole log


def test_the_recorder_reads_nothing_when_the_cad_has_not_committed(rig):
    scans = []
    rig.feed.recorder.follow = [(k, lambda heads, scan=scan: (scans.append(k), scan(heads))[1])
                                for k, scan in rig.feed.recorder.follow]
    rig.cad.create({"type": "STRU", "address": "5 Sample Way", "operator": "Lt. Ruiz"})   # (no dispatch: no voice render)
    rig.settle()
    n = len(scans)
    assert n >= 2 and rig.settle() == [] and rig.settle() == [] and len(scans) == n           # no commit: no scan
    rig.store.set_unit_status(uid(rig.cad, "E63"), "OL", "Lt. Ruiz")
    rig.settle()
    assert len(scans) > n


def test_a_prune_pass_deletes_at_most_its_batch_and_goes_on_at_the_next(tmp_path, monkeypatch):
    import sizeup.api.v1.changelog as cl
    monkeypatch.setattr(cl, "PRUNE_ROWS", 3)
    log = ChangeLog(tmp_path / "hub.sqlite", max_rows=2)
    log.append([("unit", str(i), f"{i:016x}") for i in range(10)])
    got = [log.prune(force=True) for _ in range(4)]
    assert got == [3, 3, 2, 0] and log.floor() == 8 and len(log.since(0, 100)) == 2
    log.close()


def test_a_closed_log_is_not_reopened(tmp_path):
    from sizeup.api.v1.changelog import ChangeLogClosed
    log = ChangeLog(tmp_path / "hub.sqlite")
    assert log.head() == 0
    log.close()
    with pytest.raises(ChangeLogClosed):
        log.head()
    log.close()                                                                     # (again: nothing)


def test_commit_listeners_never_fail_a_commit_and_a_rollback_calls_none(tmp_path):
    c = cad(tmp_path, Clock())
    store = c.store
    calls = []

    def boom():
        calls.append("boom")
        raise RuntimeError("a listener's bug")
    store.commit_listeners.extend([boom, lambda: calls.append("second")])
    with store.tx() as db:
        db.execute("UPDATE units SET notes = notes")
    assert calls == ["boom", "second"]                                              # later listeners still run
    calls.clear()
    with pytest.raises(ValueError):
        with store.tx() as db:
            db.execute("UPDATE units SET notes = notes")
            raise ValueError("rolled back")
    assert calls == []
    store.commit_listeners.clear()
    with store.tx() as db:                                                          # no listeners: nothing to call
        db.execute("UPDATE units SET notes = notes")
    store.shutdown()


def test_an_unavailable_answer_carries_a_retry_after_header_that_matches_its_body(tmp_path):
    v1 = V1(switches=Box().switches(), resolver=lambda h: ME, changes=None)
    status, body, _, h = v1.respond("GET", "/api/v1/changes", "", {})
    assert status == 503 and h["Retry-After"] == str(json.loads(body)["error"]["retry_after_s"]) == "30"


def test_the_feed_is_served_only_with_both_a_visibility_predicate_and_a_device_check(tmp_path):
    r = Rig(tmp_path, valid=None)
    try:
        r.settle()
        assert not r.feed.serving()                                   # visible without valid: still closed
        for path in ("/api/v1/changes", "/api/v1/stream"):
            status, body, h = r.get("after=0", path=path)
            assert status == 503 and body["error"]["code"] == "unavailable"
        r.feed.valid = signed_in
        assert r.feed.serving() and r.get("after=0")[0] == 200
        r.feed.visible = None
        assert not r.feed.serving()
    finally:
        r.close()


def test_extra_fields_are_in_the_envelope_by_keyword_only(rig):
    from sizeup.api.v1.errors import body, envelope
    assert envelope("epoch_changed", "m", extra={"epoch": "abc"}) == {"error": {"code": "epoch_changed", "message": "m",
                                                                                 "epoch": "abc"}}
    with pytest.raises(TypeError):
        envelope("c", "m", 5, {"epoch": "abc"})                       # not positional: a merge cannot misplace it
    assert json.loads(body("cursor_too_old", "m", extra={"oldest": 7}))["error"]["oldest"] == 7
    _, b, _ = rig.get("after=1&epoch=deadbeef0000")
    assert b["error"]["epoch"] == rig.log.epoch
    rig.incident()
    rig.settle()
    rig.log.retention_s = 1.0
    rig.clock.t += 10
    rig.store.set_unit_status(uid(rig.cad, "E63"), "OL", "Lt. Ruiz")
    rig.settle()
    rig.log.prune(force=True)
    _, b, _ = rig.get("after=0")
    assert b["error"]["code"] == "cursor_too_old" and b["error"]["oldest"] == rig.log.floor() > 0


def test_a_drain_with_the_flag_off_leaves_no_clean_marker(tmp_path):
    r = Rig(tmp_path, live=True)
    r.feed.recorder.prepare()
    r.store.set_unit_status(uid(r.cad, "E63"), "OL", "Lt. Ruiz")
    wait_for(lambda: r.log.head())
    r.box.flags["stream"] = False
    r.feed.close()
    assert r.store.meta_get("mobile_log_clean") is None
    r.store.shutdown()


def test_a_stream_whose_feed_is_closed_under_it_says_stopping(rig):
    status, body, _, _ = rig.v1.respond("GET", "/api/v1/stream", "", {})
    written = []

    def write(raw):
        written.append(raw)
        if b"event: hello" in raw:
            rig.feed.log.close()                                      # the drain closes the log under the open stream
    why = body.run(write)
    body.close()
    assert why == "stopping" and b'"reason":"stopping"' in written[-1]


# -- the log's rev is the rev the reads serve (phase B) --------------------------------------------------------------

def read(hub, path):
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=10)
    c.request("GET", path, headers=dev())
    r = c.getresponse()
    return r.status, json.loads(r.read())


def quiet(hub):
    """The CAD has stopped committing and the recorder has caught up with it."""
    wait_for(lambda: hub.feed.recorder._seen_version == hub.cad.store.rev)
    time.sleep(0.1)
    return hub.feed.recorder._seen_version == hub.cad.store.rev


def test_the_logs_rev_is_the_rev_the_reads_serve_for_units_and_incidents(hub):
    hub.box.flags["reads"] = True
    e63 = uid(hub.cad, "E63")
    inc = hub.cad.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "Lt. Ruiz"})
    hub.cad.store.assign(inc["id"], [e63], "Lt. Ruiz")                      # (no dispatch: no async voice render)
    hub.cad.store.set_unit_status(uid(hub.cad, "L31"), "RS", "Lt. Ruiz")
    assert quiet(hub)
    hub.clock.t += 2.0                                                       # past reads' 1 s list-cache floor
    status, units = read(hub, "/api/v1/units?limit=500")
    assert status == 200 and len(units["items"]) > 100
    heads = hub.feed.log.heads()
    for u in units["items"]:
        assert heads[("unit", u["id"])] == u["rev"], u["name"]              # every unit, the ones that changed included
    status, incs = read(hub, "/api/v1/incidents")
    assert status == 200 and incs["items"]
    for i in incs["items"]:
        assert heads[("incident", i["id"])] == i["rev"]
    changed = {(c.kind, c.id): c.rev for c in hub.feed.log.since(0, 100)}
    assert changed[("incident", str(inc["id"]))] == next(i["rev"] for i in incs["items"] if i["id"] == str(inc["id"]))
    assert changed[("unit", str(e63))] == next(u["rev"] for u in units["items"] if u["id"] == str(e63))
    # the snapshot's items carry the same revs
    status, snap = read(hub, "/api/v1/snapshot")
    assert {i["rev"] for i in snap["incidents"]} <= {i["rev"] for i in incs["items"]}


def test_a_field_the_api_does_not_show_is_not_a_change_and_the_log_never_hashes_a_raw_row(hub):
    hub.box.flags["reads"] = True
    e63 = uid(hub.cad, "E63")
    inc = hub.cad.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "Lt. Ruiz"})
    assert quiet(hub)
    head, entries = hub.head(), len(hub.feed.log.since(0, 1000))
    with hub.cad.store.tx() as db:                                           # raw columns reads' output leaves out
        db.execute("UPDATE units SET notes = 'bring the key' WHERE id = ?", (e63,))
        db.execute("UPDATE incidents SET caller_name = 'A. Caller', phone = '555-0100' WHERE id = ?", (inc["id"],))
    assert quiet(hub)
    assert hub.head() == head and len(hub.feed.log.since(0, 1000)) == entries     # no entry: the output is the same
    hub.clock.t += 2.0
    _, units = read(hub, "/api/v1/units?limit=500")
    assert hub.feed.log.heads()[("unit", str(e63))] == next(u["rev"] for u in units["items"] if u["id"] == str(e63))
    with hub.cad.store.tx() as db:                                           # a field it does show is
        db.execute("UPDATE units SET title = 'Duty Officer' WHERE id = ?", (e63,))
    assert quiet(hub)
    assert hub.head() == head + 1


def test_a_log_written_under_the_old_rev_definition_is_reset_at_its_first_run(tmp_path):
    """Phase A hashed raw rows and recorded no definition: a hub that ran it gets a new epoch (old cursors: 409)."""
    r = Rig(tmp_path)
    r.settle()
    r.incident()
    r.settle()
    epoch, head = r.log.epoch, r.log.head()
    assert head > 0
    r.feed.close()
    r.log.close()
    db = sqlite3.connect(tmp_path / "hub.sqlite")                            # make it a phase A log: no rev_def
    db.execute("DELETE FROM mobile_changelog_meta WHERE key = 'rev_def'")
    db.commit()
    db.close()
    try:
        next_task(r)
        assert r.log.epoch != epoch and r.log.since(0, 10) == []        # (seq goes on: it is never reused)
        # the CAD was told the new epoch, so no second reset follows
        assert r.store.meta_get("mobile_log_epoch") == r.log.epoch
        status, body, _ = r.get(f"after={head}&epoch={epoch}")
        assert status == 409 and body["error"]["code"] == "epoch_changed"
        epoch2 = r.log.epoch
        r.feed.close()
        r.log.close()
        next_task(r)                                                         # the next start: stable
        assert r.log.epoch == epoch2
    finally:
        r.close()


def test_a_new_log_records_the_rev_definition_without_a_reset(tmp_path):
    r = Rig(tmp_path)
    r.settle()
    epoch = r.log.epoch
    assert r.log._meta("rev_def") == "reads-serializers-v1"
    r.feed.close()
    r.log.close()
    try:
        next_task(r)
        assert r.log.epoch == epoch
    finally:
        r.close()
