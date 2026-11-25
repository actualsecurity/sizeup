"""The GPU STT worker's own log to CloudWatch: the worker's side (deploy/stt-worker/worker.py scrub_line, LogShip:
scrubbed, spooled, bounded, sent and deleted only once accepted, never in a clip's way) and the hub's
(POST /api/station/stt/logs, sizeup/cloud/cwlogs.py WorkerLogs: a stream per worker in /sizeup/stt-worker, made on
first use; "not shipped: permission" until the task role may). CloudWatch is a fake that answers as its JSON API does
(errors as aws.AwsError with its code); no network. Tokens, ids and lines are made up."""
from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from sizeup import aws
from sizeup.cloud import cwlogs
from tests.stt_worker_module import W

TOKEN = "sw1.gpu-1." + "Qx7" * 14 + "a"                           # (43 characters, as the hub makes them)
CF_ID = "0123456789abcdef0123456789abcdef.access"
CF_SECRET = "f" * 24 + "0123456789abcdef" * 2 + "e" * 8               # 64 hex


def cfg_for(tmp_path, **kw) -> W.Config:
    return W.Config(hub_url="https://hub.example.org", token=TOKEN, cf_client_id=CF_ID, cf_client_secret=CF_SECRET,
                    state_dir=str(tmp_path / "state"), log_file="-", **kw)


class FakeHub:
    """The worker's Hub for the log thread: answers (a dict, or a HubError to raise) in turn; records each batch."""

    def __init__(self, *answers):
        self.answers = list(answers) or [{"ok": True, "accepted": True}]
        self.batches: list[dict] = []
        self.headers: list[dict] = []
        self.closed = 0

    def request(self, method, path, body=None, timeout=None, raw=False, data=None, extra=None):
        assert (method, path) == ("POST", "/api/station/stt/logs") and body is None
        self.batches.append(json.loads(gzip.decompress(data)))
        self.headers.append(extra)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return dict(a, n=len(self.batches[-1]["lines"])) if a.get("accepted") else a

    def close(self):
        self.closed += 1


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def shipped(tmp_path):
    """A LogShip on the worker's logger, as `run` puts it there; taken off after the test."""
    made = []

    def make(hub=None, clock=None, **kw):
        ship = W.LogShip(cfg_for(tmp_path, **kw), hub=hub or FakeHub(), clock=clock or Clock())
        W.log.addHandler(ship)
        made.append(ship)
        return ship
    old = W.log.level
    W.log.setLevel(logging.INFO)
    yield make
    for s in made:
        W.log.removeHandler(s)
    W.log.setLevel(old)


# --- the scrub on the PC ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line, gone", [
    (f"hub refused {TOKEN}", TOKEN.split(".")[2]),
    ("Authorization: Bearer sw1.gpu-2.AAAAbbbbCCCCdddd1234567890abcdefghijklmnopq", "AAAAbbbb"),
    ("headers {'Authorization': 'Bearer abc.def-ghi'}", "abc.def-ghi"),
    (f"CF-Access-Client-Id: {CF_ID}", CF_ID),
    (f"cf_access_client_secret={CF_SECRET}", CF_SECRET),
    (f"a secret of a kind: {CF_SECRET}", CF_SECRET),
    ("id 9f8e7d6c5b4a39281706f5e4d3c2b1a0.access refused", "9f8e7d6c5b4a39281706f5e4d3c2b1a0"),
    ("token=Zm9vYmFy and the rest", "Zm9vYmFy"),
    ('{"password": "hunter2-hunter2"}', "hunter2-hunter2"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eXoifQ.c2lnbmF0dXJlc2lnbmF0dXJl here", "eyJhbGciOiJIUzI1NiJ9"),
    ("key Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb7c in a line", "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb7c"),
])
def test_a_token_like_string_never_leaves_the_pc(line, gone):
    out = W.scrub_line(line)
    assert gone not in out and "[redacted]" in out


def test_known_values_go_first_in_any_line():
    assert W.scrub_line("x=ab12CD34ef56 y", ["ab12CD34ef56"]) == "x=[redacted] y"


@pytest.mark.parametrize("line", [
    "self-update: 3300a3a09476 (commit 9d4c2b1a0f8e) is on main, checked and staged: switching when idle",
    "infer 1a2b3c4d5e6f7a8b classify: 812 ms, 512+40 tokens",
    "hub refused the token (token_unknown): trying again in 300 s",
    "worker.py is now 3300a3a094768c8a363c322c857e1f57cb051e819e0bc686a650624a99dbe3d7"[:40],
    "commit 3b7e1f0c9a2d4e6b8c0a1f3e5d7b9c2a4e6f8a0b",                      # a commit (40 hex) stays
    r"can't start whisper-server: [WinError 2] C:\Users\someone\AppData\Local\sizeup-stt\whisper",
])
def test_ordinary_lines_pass_unchanged(line):
    assert W.scrub_line(line) == line


def test_a_long_line_is_cut_after_the_scrub():
    out = W.scrub_line("x" * 5000 + " " + TOKEN)
    assert len(out) <= W.LOG_LINE_MAX + 6 and out.endswith(" [cut]")
    out = W.scrub_line("x" * (W.LOG_LINE_MAX - 20) + " " + TOKEN)        # a token across the cut: never half kept
    assert TOKEN.split(".")[2][:10] not in out


# --- what is taken: worker.log's own lines, nothing else ----------------------------------------------------------

def test_only_the_workers_own_lines_and_never_a_servers_output(tmp_path, shipped):
    ship = shipped()
    W.log.info("job %s: done in %d ms", "a1b2", 812)
    W.log.debug("a debug line stays on the PC")
    proc = W.WhisperProcess(["whisper-server"], tmp_path / "whisper-server.log")
    proc._logger().info("whisper-server: the text it heard (a transcript)")      # its own file, never shipped
    llama = W.WhisperProcess(["llama-server"], tmp_path / "llama-server.log", name="llama-server")
    llama._logger().info("llama-server: a prompt")
    logging.getLogger("sizeup.other").warning("another logger")
    try:
        raise ValueError("boom")
    except ValueError:
        W.log.exception("unexpected error: trying again in %d s", 5)
    rows = ship._buf
    assert [r[1] for r in rows] == ["info", "error"]
    assert rows[0][2] == "job a1b2: done in 812 ms"
    assert "Traceback (most recent call last)" in rows[1][2] and "ValueError: boom" in rows[1][2]
    assert all(isinstance(r[0], int) and abs(r[0] / 1000 - time.time()) < 60 for r in rows)


def test_the_redact_filters_secret_and_the_scrub_both_apply(tmp_path, shipped):
    cfg = cfg_for(tmp_path)
    W.log.addFilter(f := W.Redact(cfg.secrets()))
    try:
        ship = shipped()
        W.log.warning("a line with %s and %s in it", TOKEN, CF_SECRET)
    finally:
        W.log.removeFilter(f)
    (row,) = ship._buf
    assert TOKEN.split(".")[2] not in row[2] and CF_SECRET not in row[2]


def test_logging_never_fails_or_waits_on_the_shipper(tmp_path, shipped):
    """emit only keeps the line: a hub that hangs holds the log thread, never the one that logs (the clips)."""
    gate = threading.Event()

    class Hanging(FakeHub):
        def request(self, *a, **kw):
            gate.wait(10)
            return {"ok": True, "accepted": True}
    ship = shipped(hub=Hanging(), clock=time.monotonic)
    W.log.info("first")
    ship.spool()
    stop = threading.Event()
    t = threading.Thread(target=ship.loop, args=(stop.is_set, stop.wait), daemon=True)
    t.start()
    try:
        time.sleep(0.2)                                    # (the log thread is inside request now)
        t0 = time.monotonic()
        for i in range(500):
            W.log.info("job %d done", i)
        assert time.monotonic() - t0 < 1.0
    finally:
        stop.set()
        gate.set()
        t.join(5)
    assert not t.is_alive()


def test_memory_is_bounded(tmp_path, shipped, monkeypatch):
    monkeypatch.setattr(W, "LOG_MEM_LINES", 50)
    ship = shipped()
    for i in range(80):
        W.log.info("line %d", i)
    assert len(ship._buf) == 50 and ship._buf[0][2] == "line 30" and ship.dropped == 30


# --- spool and send ------------------------------------------------------------------------------------------------

def test_a_batch_is_spooled_sent_and_deleted_once_accepted(tmp_path, shipped):
    hub = FakeHub()
    clock = Clock()
    ship = shipped(hub=hub, clock=clock)
    W.log.info("one")
    W.log.warning("two")
    assert ship.due() is False                               # not a minute yet, not many lines
    clock.t += W.LOG_BATCH_S
    assert ship.due() is True
    ship.step()
    (b,) = hub.batches
    assert b["v"] == 1 and b["worker"] == "gpu-1" and [r[1:] for r in b["lines"]] == [["info", "one"], ["warning", "two"]]
    assert hub.headers == [{"Content-Encoding": "gzip"}]
    assert not list((tmp_path / "state" / "logspool").glob("*.json.gz"))
    assert ship.state == "ok" and ship.last_ok is not None and ship.sent_lines == 2
    assert ship.stats() == {"state": "ok", "last_ok": ship.last_ok, "batches": 0, "waiting": 0, "dropped": 0,
                            "rejected": 0}


def test_many_lines_go_in_several_batches_oldest_first(tmp_path, shipped, monkeypatch):
    monkeypatch.setattr(W, "LOG_BATCH_LINES", 10)
    hub = FakeHub()
    ship = shipped(hub=hub)
    for i in range(25):
        W.log.info("line %d", i)
    assert ship.due() is True
    ship.step()
    assert [len(b["lines"]) for b in hub.batches] == [10, 10, 5]
    assert [r[2] for b in hub.batches for r in b["lines"]] == [f"line {i}" for i in range(25)]


@pytest.mark.parametrize("answer, wait, state", [
    ({"ok": True, "accepted": False, "retry_s": 600, "reason": "hub: not configured (permission)"}, 600,
     "not shipped: hub: not configured (permission)"),
    (W.HubError("hub said 503", 503), W.LOG_RETRY_S[0], "not shipped: hub said 503"),
    (W.HubError("can't reach the hub (TimeoutError: timed out)"), W.LOG_RETRY_S[0], "not shipped: hub unreachable"),
    (W.HubError("hub said 404", 404), W.LOG_NOT_THERE_S, "not shipped: the hub doesn't take logs yet"),
    (W.HubError("hub refused the token (token_revoked)", 401, reason="token_revoked"), 300.0,
     "not shipped: refused (token_revoked)"),
])
def test_a_batch_not_taken_stays_on_the_pc_and_goes_later(tmp_path, shipped, answer, wait, state):
    hub = FakeHub(answer, {"ok": True, "accepted": True})
    clock = Clock()
    ship = shipped(hub=hub, clock=clock)
    W.log.info("kept")
    ship.spool()
    ship.send()
    assert len(list(ship.dir.glob("*.json.gz"))) == 1 and ship.state == state
    assert ship.next_send == pytest.approx(clock.t + wait)
    clock.t += wait - 1
    assert ship.send() == "waiting" and len(hub.batches) == 1
    clock.t += 1
    ship.send()
    assert len(hub.batches) == 2 and hub.batches[1] == hub.batches[0] and not list(ship.dir.glob("*.json.gz"))


def test_failures_back_off(tmp_path, shipped):
    hub = FakeHub(W.HubError("hub said 502", 502))
    clock = Clock()
    ship = shipped(hub=hub, clock=clock)
    W.log.info("x")
    ship.spool()
    waits = []
    for _ in range(7):
        clock.t = ship.next_send
        ship.send()
        waits.append(ship.next_send - clock.t)
    assert waits == [30.0, 60.0, 120.0, 300.0, 600.0, 600.0, 600.0]


def test_a_batch_the_hub_will_never_take_is_deleted_and_counted(tmp_path, shipped):
    hub = FakeHub(W.HubError("hub said 400", 400), {"ok": True, "accepted": True})
    ship = shipped(hub=hub)
    W.log.info("a")
    ship.spool()
    W.log.info("b")
    ship.spool()
    ship.send()
    assert ship.rejected == 1 and [b["lines"][0][2] for b in hub.batches] == ["a", "b"]
    assert not list(ship.dir.glob("*.json.gz"))


def test_the_spool_is_bounded_oldest_first(tmp_path, shipped, monkeypatch):
    monkeypatch.setattr(W, "LOG_SPOOL_MAX_FILES", 3)
    ship = shipped(hub=FakeHub(W.HubError("hub said 503", 503)))
    for i in range(5):
        W.log.info("batch %d", i)
        ship.spool()
    files = sorted(ship.dir.glob("*.json.gz"))
    assert len(files) == 3 and ship.dropped == 2
    assert [json.loads(gzip.decompress(p.read_bytes()))["lines"][0][2] for p in files] == ["batch 2", "batch 3",
                                                                                          "batch 4"]
    monkeypatch.setattr(W, "LOG_SPOOL_MAX_BYTES", 1)                       # bytes too
    W.log.info("batch 5")
    ship.spool()
    assert not list(ship.dir.glob("*.json.gz"))


def test_an_unwritable_spool_keeps_the_lines_in_memory(tmp_path, shipped):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "logspool").write_text("a file where the folder goes")
    ship = shipped()
    W.log.info("kept in memory")
    assert ship.spool() is False and [r[2] for r in ship._buf][0] == "kept in memory"


def test_what_is_left_at_the_stop_is_sent_by_the_next_start(tmp_path, shipped):
    first = shipped(hub=FakeHub(W.HubError("can't reach the hub")))
    W.log.info("the last line before an update's restart")
    first.close_spool()
    W.log.removeHandler(first)
    hub = FakeHub()
    second = shipped(hub=hub)
    W.log.info("the first line after it")
    second.step()                                            # (its own line waits for its minute)
    assert [r[2] for b in hub.batches for r in b["lines"]] == ["the last line before an update's restart"]
    second.spool()
    second.send()
    assert hub.batches[-1]["lines"][0][2] == "the first line after it"
    assert len({b["batch"] for b in hub.batches}) == 2


def test_log_ship_off_and_on_by_default(tmp_path):
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"log_ship": False}))
    assert W.load_config(p, env={}).log_ship is False and W.Config().log_ship is True


def test_status_says_the_last_shipment(tmp_path):
    cfg = cfg_for(tmp_path)
    W.write_json(cfg.status_path, {"updated": 1000.0, "state": "online", "worker": "gpu-1", "pid": 1, "hub": "ok",
                                   "logs": {"state": "not shipped: hub: not configured (permission)", "last_ok": None,
                                            "batches": 3, "waiting": 0, "dropped": 0, "rejected": 0}})
    out = W._status_text(cfg, now=1001.0)
    assert "logs: not shipped: hub: not configured (permission), none shipped yet; 3 batch(es) waiting" in out


# --- the hub: WorkerLogs and the route -----------------------------------------------------------------------------

class FakeCloudWatch:
    """CloudWatch Logs' JSON API as the hub calls it (sizeup/aws.py json_call): its answers' shapes, errors as AwsError
    with CloudWatch's codes. deny: actions answered AccessDeniedException."""

    def __init__(self, groups=None, deny=()):
        self.groups = dict(groups or {})                  # name -> retention
        self.streams: dict[tuple[str, str], list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.deny = set(deny)

    def __call__(self, target, body, region, timeout=15.0):
        self.calls.append((target, body))
        if target in self.deny:
            raise aws.AwsError(f"User is not authorized to perform: logs:{target}", code="AccessDeniedException")
        if target == "DescribeLogGroups":
            return {"logGroups": [{"logGroupName": g, "retentionInDays": r, "storedBytes": 0}
                                  for g, r in self.groups.items() if g.startswith(body["logGroupNamePrefix"])]}
        if target == "CreateLogGroup":
            if body["logGroupName"] in self.groups:
                raise aws.AwsError("The specified log group already exists", code="ResourceAlreadyExistsException")
            self.groups[body["logGroupName"]] = None
            return {}
        if target == "PutRetentionPolicy":
            self.groups[body["logGroupName"]] = body["retentionInDays"]
            return {}
        if target == "CreateLogStream":
            key = (body["logGroupName"], body["logStreamName"])
            if key in self.streams:
                raise aws.AwsError("The specified log stream already exists", code="ResourceAlreadyExistsException")
            self.streams[key] = []
            return {}
        if target == "PutLogEvents":
            key = (body["logGroupName"], body["logStreamName"])
            if body["logGroupName"] not in self.groups:
                raise aws.AwsError("The specified log group does not exist.", code="ResourceNotFoundException")
            if key not in self.streams:
                raise aws.AwsError("The specified log stream does not exist.", code="ResourceNotFoundException")
            ts = [e["timestamp"] for e in body["logEvents"]]
            assert ts == sorted(ts) and max(ts) - min(ts) < 24 * 3600 * 1000
            self.streams[key] += body["logEvents"]
            return {"nextSequenceToken": "0"}
        raise AssertionError(target)


def batch(lines, worker="gpu-1") -> bytes:
    return gzip.compress(json.dumps({"v": 1, "worker": worker, "batch": "1-1", "lines": lines}).encode())


GZ = {"Content-Encoding": "gzip"}


def test_the_hub_makes_the_group_on_first_use_with_the_stations_retention():
    cw = FakeCloudWatch()
    wl = cwlogs.WorkerLogs(settings=lambda: {"retention_days": 60}, call=cw, log=lambda m: None)
    now = time.time() * 1000
    code, ans = wl.ingest("gpu-1", batch([[now, "info", "one"], [now + 1, "error", f"two {TOKEN}"]]), GZ)
    assert (code, ans) == (200, {"ok": True, "accepted": True, "n": 2})
    assert ("CreateLogGroup", {"logGroupName": "/sizeup/stt-worker", "logGroupClass": "INFREQUENT_ACCESS"}) in cw.calls
    assert cw.groups == {"/sizeup/stt-worker": 60}
    evs = cw.streams[("/sizeup/stt-worker", "gpu-1")]          # one stream per worker id
    msgs = [json.loads(e["message"]) for e in evs]
    assert msgs[0] == {"worker": "gpu-1", "level": "info", "msg": "one"}
    assert TOKEN.split(".")[2] not in msgs[1]["msg"] and msgs[1]["msg"].startswith("two sw1.gpu-1.")   # scrubbed again
    assert wl.view("gpu-1") == {"at": pytest.approx(time.time(), abs=5), "lines": 2}
    code, ans = wl.ingest("gpu-1", batch([[now + 2, "info", "three"]]), GZ)            # no second check, no new stream
    assert ans["accepted"] and [c[0] for c in cw.calls].count("CreateLogStream") == 1
    assert [c[0] for c in cw.calls].count("DescribeLogGroups") == 1


def test_the_stream_is_the_authenticated_worker_never_the_batchs_own_word():
    cw = FakeCloudWatch({"/sizeup/stt-worker": 30})
    wl = cwlogs.WorkerLogs(call=cw, log=lambda m: None)
    wl.ingest("gpu-1", batch([[time.time() * 1000, "info", "x"]], worker="gpu-2"), GZ)
    assert list(cw.streams) == [("/sizeup/stt-worker", "gpu-1")]


def test_without_permission_the_worker_keeps_its_logs_and_health_says_so():
    said = []
    cw = FakeCloudWatch(deny={"CreateLogGroup", "PutLogEvents", "CreateLogStream", "PutRetentionPolicy"})
    clock = Clock(time.time())
    wl = cwlogs.WorkerLogs(call=cw, log=said.append, clock=clock)
    code, ans = wl.ingest("gpu-1", batch([[clock.t * 1000, "info", "x"]]), GZ)
    assert code == 200 and ans == {"ok": True, "accepted": False, "retry_s": cwlogs.RETRY_S,
                                   "reason": "hub: not configured (permission)"}
    assert wl.view("gpu-1") == {"at": None, "lines": 0, "error": "permission"}
    assert said == ["stt worker logs: not configured (permission)"]
    n = len(cw.calls)
    wl.ingest("gpu-1", batch([[clock.t * 1000, "info", "x"]]), GZ)        # not asked again within CHECK_S
    assert len(cw.calls) == n
    cw.deny.clear()                                                         # the admin applied the policy
    clock.t += cwlogs.CHECK_S
    code, ans = wl.ingest("gpu-1", batch([[clock.t * 1000, "info", "x"]]), GZ)
    assert ans["accepted"] and "error" not in wl.view("gpu-1")
    # a role that may make the group but not write it: the same
    cw2 = FakeCloudWatch({"/sizeup/stt-worker": 30}, deny={"PutLogEvents"})
    wl2 = cwlogs.WorkerLogs(call=cw2, log=lambda m: None)
    code, ans = wl2.ingest("gpu-1", batch([[time.time() * 1000, "info", "x"]]), GZ)
    assert ans["accepted"] is False and wl2.view("gpu-1")["error"] == "permission"


@pytest.mark.parametrize("raw, headers, code", [
    (None, GZ, 413),
    (b"not gzip", GZ, 400),
    (gzip.compress(b"[1, 2]"), GZ, 400),
    (gzip.compress(b"x" * (cwlogs.WORKER_MAX_INFLATED + 1)), GZ, 413),
    (batch([[0, "info", "x"]] * (cwlogs.WORKER_MAX_LINES + 1)), GZ, 400),
])
def test_a_batch_the_hub_cant_take(raw, headers, code):
    wl = cwlogs.WorkerLogs(call=FakeCloudWatch(), log=lambda m: None)
    assert wl.ingest("gpu-1", raw, headers)[0] == code


def test_bad_lines_are_dropped_not_the_batch():
    cw = FakeCloudWatch({"/sizeup/stt-worker": 30})
    wl = cwlogs.WorkerLogs(call=cw, log=lambda m: None)
    now = time.time() * 1000
    raw = gzip.compress(json.dumps({"lines": [[now, "info", "ok"], [now, "info"], ["x", "info", "y"], [True, "info", "y"],
                                              [now - 20 * 86400 * 1000, "info", "too old"], [now, "loud", "level?"],
                                              [now, "info", "-----BEGIN RSA PRIVATE KEY----- MIIE"]]}).encode())
    code, ans = wl.ingest("gpu-1", raw, GZ)
    assert ans["n"] == 2
    msgs = [json.loads(e["message"]) for e in cw.streams[("/sizeup/stt-worker", "gpu-1")]]
    assert [(m["level"], m["msg"]) for m in msgs] == [("info", "ok"), ("info", "level?")]


def test_the_route_end_to_end_from_the_worker_client(tmp_path, monkeypatch):
    """The worker's own LogShip and Hub client against a real hub: its batch lands in its stream; Health shows it; a
    station's whisper may not post logs; without permission the batch stays on the PC and Health says why."""
    from tests import contracts as c
    lv = c.Live(tmp_path)
    try:
        cw = FakeCloudWatch()
        lv.hub.app.worker_logs._call = cw
        cfg = W.Config(hub_url=lv.url, token=lv.worker, state_dir=str(tmp_path / "w"), log_file="-")
        ship = W.LogShip(cfg)
        rec = logging.LogRecord("sizeup.stt_worker", logging.INFO, __file__, 1, "job %s done", ("a1",), None)
        ship.emit(rec)
        ship.spool()
        ship.send()
        assert ship.state == "ok" and not list(ship.dir.glob("*.json.gz"))
        (e,) = cw.streams[("/sizeup/stt-worker", c.WORKER)]
        assert json.loads(e["message"]) == {"worker": c.WORKER, "level": "info", "msg": "job a1 done"}
        # Health: the worker's row shows its last batch
        lv.call("POST", "/api/station/stt/state", {"state": "paused", "model": "m"}, token="sw1")
        (w,) = [x for x in lv.hub.app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert w["logs"]["at"] and w["logs"]["lines"] == 1 and "error" not in w["logs"]
        # a station's whisper: not its route
        lv.hub.app.hub.workers.set_pool(stations={c.STATION: True})
        code, ans = lv.call("POST", "/api/station/stt/logs", {"lines": []}, token="sz1")
        assert code == 403
        # no permission: kept on the PC, Health says so
        lv.hub.app.worker_logs = wl = cwlogs.WorkerLogs(call=FakeCloudWatch(deny={"DescribeLogGroups"}),
                                                        log=lambda m: None)
        lv.hub.app.stt_api.logs = wl
        ship.emit(rec)
        ship.spool()
        ship.send()
        assert ship.state == "not shipped: hub: not configured (permission)"
        assert len(list(ship.dir.glob("*.json.gz"))) == 1
        (w,) = [x for x in lv.hub.app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert w["logs"]["error"] == "permission"
    finally:
        lv.close()


def test_a_hub_without_worker_logs_answers_404(tmp_path):
    from tests import contracts as c
    lv = c.Live(tmp_path)
    try:
        lv.hub.app.stt_api.logs = None
        code, _ = lv.call("POST", "/api/station/stt/logs", {"lines": []}, token="sw1")
        assert code == 404
        lv.hub.app.stt_api.logs = cwlogs.WorkerLogs(call=FakeCloudWatch(), log=lambda m: None)
        code, ans = lv.call("GET", "/api/station/stt/logs", None, token="sw1")
        assert (code, ans) == (404, {"error": "not found"})
    finally:
        lv.close()


def test_the_health_page_shows_the_logs_cell():
    js = (Path(__file__).resolve().parents[1] / "sizeup" / "web" / "static" / "health.js").read_text()
    assert '"Last seen", "Logs"]' in js and "not shipped: ${l.error}" in js
