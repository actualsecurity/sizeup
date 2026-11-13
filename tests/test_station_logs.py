"""Station logs to CloudWatch: the scrub, the station's shipper (journal -> spool -> hub), the hub's sink and the
admins' Logs view (sizeup/logscrub.py, sizeup/cloud/logship.py, sizeup/cloud/cwlogs.py). Synthetic data only: the
journal fixture has journalctl -o json's real shape with invented lines."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sizeup import aws, logscrub
from sizeup.cloud import cwlogs, logship
from sizeup.cloud.client import HubError
from sizeup.cloud.cwlogs import LogsError, StationLogs
from sizeup.cloud.logship import LogShipper

FIXTURE = Path(__file__).parent / "fixtures" / "journal-sizeup.jsonl"
JOURNAL = [json.loads(x) for x in FIXTURE.read_text().splitlines()]
T0 = 1762950000.0                   # the fixture's first entry
TOKEN = "st-Tok3n-0123456789abcdefXYZ"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    logscrub.forget()
    monkeypatch.delenv("SIZEUP_LOG_SHIP", raising=False)
    yield
    logscrub.forget()


# -- the scrub ---------------------------------------------------------------------------------------------------

def test_scrub_drops_a_registered_secret_and_redacts_credential_shapes():
    logscrub.secret(TOKEN, "Corr3ct-Horse-9")
    logscrub.secret_name("FEEDX_USER", "FEEDX_PASS")
    s = logscrub.scrub
    assert s(f"hub link: bearer {TOKEN} refused") is None                  # a known value: the whole line dropped
    assert s("feed login as Corr3ct-Horse-9") is None
    assert s("FEEDX_PASS read from the credstore") == "[credential] read from the credstore"
    assert s("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345") == "Authorization: [redacted] [redacted]"
    assert s("GET https://u:pw1@example.org/a?token=abc&page=2") == "GET https://[redacted]@example.org/a?token=[redacted]&page=2"
    assert s("aws AKIAABCDEFGHIJKLMNOP wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY") == "aws [aws-key] [redacted]"
    assert s('{"password": "x1", "user": "u"}') == '{"password": [redacted], "user": "u"}'
    assert s("cookie=Zq81LmXpTRe4vB2nW9yKs0Ha; path=/") == "cookie=[redacted]; path=/"
    assert s("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLXZhbHVl") == "jwt [redacted]"
    assert s("-----BEGIN OPENSSH PRIVATE KEY-----") is None
    # fail closed: a password named with something after it, not in a key=value shape
    assert s("the password is hunter7") is None and s("client secret abc123") is None
    # kept: a git sha, a uuid, numbers, and sizeup's own words about a password that carry none
    sha = "3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c"
    assert s(f"auto-update: on {sha}") == f"auto-update: on {sha}"
    assert s("adapter: password version 3 accepted") == "adapter: password version 3 accepted"
    assert s("password(s) refused in 24 h: 1") == "password(s) refused in 24 h: 1"
    assert s("FG1: carrier 4.2 s, quieting 18 dB") == "FG1: carrier 4.2 s, quieting 18 dB"
    assert logscrub.registered() == (2, 2)


def test_levels_and_repeats():
    assert logscrub.level(6, "hub link: connected") == "info"
    assert logscrub.level(6, "Traceback (most recent call last):") == "error"
    assert logscrub.level(6, "control channel lost, retrying") == "warning"
    assert logscrub.level(6, "14:00:10 debug: rx buffer 4096") == "debug"
    assert logscrub.level(3, "anything") == "error" and logscrub.level(7, "x") == "debug"
    assert logscrub.at_least("warning", "info") and not logscrub.at_least("debug", "info")
    r = logscrub.Repeats(burst=2, window_s=600)
    sent = [x for t in range(6) for x in r.allow("sizeup-station", f"FG1: carrier {t}.1 s", "info", T0 + t)]
    assert [m for _, _, _, m in sent] == ["FG1: carrier 0.1 s", "FG1: carrier 1.1 s"] and r.suppressed == 4
    assert r.flush(T0 + 100) == []
    out = r.flush(T0 + 700)
    assert [m for _, _, _, m in out] == ["[repeated 4× in 10 min] FG1: carrier 5.1 s"]


# -- the station -------------------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, answers):
        self.answers, self.sent = list(answers), []

    def request(self, method, path, body=None, timeout=None, gzip_body=False, raw_gzip=None):
        self.sent.append((path, json.loads(gzip.decompress(raw_gzip))))
        a = self.answers.pop(0) if self.answers else {"ok": True, "accepted": True}
        if isinstance(a, Exception):
            raise a
        return a

    def close(self):
        pass


def shipper(tmp_path, answers=(), journal=None, clock=None):
    c = FakeClient(answers)
    cfg = SimpleNamespace(station_id="pcfd", cloud_outbox=str(tmp_path / "outbox.sqlite"))
    now = clock or [T0 + 100]
    rows = list(JOURNAL) if journal is None else journal

    def read(cursor, since, limit):
        cur = [r["__CURSOR"] for r in rows]
        start = cur.index(cursor) + 1 if cursor in cur else 0
        return rows[start:start + limit]
    s = LogShipper(cfg, lambda t: c, log=lambda m: None, clock=lambda: now[0], journal=read)
    return s, c, now


def test_the_station_reads_filters_scrubs_spools_and_sends(tmp_path):
    s, c, now = shipper(tmp_path)
    assert s.step() == "ok"                       # read: the batch is open, nothing due yet
    assert s.pending and not s._batches() and not c.sent
    now[0] += logship.BATCH_S
    assert s.step() == "ok"
    assert len(c.sent) == 1 and not s._batches()
    path, body = c.sent[0]
    assert path == "/api/station/logs" and body["station"] == "pcfd"
    lines = body["lines"]
    msgs = [m for _, _, _, m in lines]
    assert all(isinstance(t, int) for t, _, _, _ in lines) and lines == sorted(lines, key=lambda r: r[0])
    # debug dropped (info is the default threshold); the cookie redacted; FG1's repeats cut at the burst (5)
    assert not any("debug" in m for m in msgs) and s.dropped_level == 1
    assert any(m.endswith("session=[redacted]; path=/") for m in msgs)
    assert sum(m.startswith("14:00:0") and "FG1" in m for m in msgs) == 5 and s.repeats.suppressed == 2
    units = {u for _, u, _, _ in lines}
    assert units == {"sizeup-station", "sizeup-whisper", "sizeup-trunk-recorder", "sizeup-update", "sizeup-health"}
    lv = {m: v for _, _, v, m in lines}
    assert lv["Traceback (most recent call last):"] == "error"
    assert lv["[2026-11-12 14:00:11] (warning) control channel lost, retuning"] == "warning"
    assert "bad byte � here" in msgs                     # not UTF-8: replaced, kept
    # the cursor moved past everything read, and is kept on disk
    assert (tmp_path / "logspool" / "cursor").read_text() == JOURNAL[-1]["__CURSOR"]
    st = s.stats()
    assert st["state"] == "ok" and st["sent_lines"] == len(lines) and st["spool_batches"] == 0


def test_a_known_secret_never_leaves_the_station(tmp_path):
    rows = [dict(JOURNAL[0], MESSAGE=f"hub link: token {TOKEN}", __CURSOR="c1"),
            dict(JOURNAL[0], MESSAGE="feed: user FEEDX_USER ok", __CURSOR="c2")]
    logscrub.secret(TOKEN)
    logscrub.secret_name("FEEDX_USER")
    s, c, now = shipper(tmp_path, journal=rows)
    s.step()
    s.spool(force=True)
    s.send()
    sent = json.dumps(c.sent)
    assert TOKEN not in sent and "FEEDX_USER" not in sent and "[credential]" in sent and s.dropped_secret == 1


def test_the_hub_away_keeps_the_spool_capped_and_sends_it_later(tmp_path, monkeypatch):
    s, c, now = shipper(tmp_path, answers=[HubError("can't reach the hub", 0)] * 3)
    s.step()
    s.spool(force=True)
    assert s.send() == "failed" and len(s._batches()) == 1 and s.state == "offline"
    assert s.send() == "waiting"                     # backing off
    now[0] = s.next_send
    assert s.send() == "failed"
    # the hub without CloudWatch set up: kept, and asked again in 10 min
    c.answers = [{"ok": True, "accepted": False, "retry_s": 600, "reason": "hub: not configured (no log group)",
                  "config": {"on": True, "level": "warning", "include": [], "exclude": ["sizeup-whisper"]}}]
    now[0] = s.next_send
    assert s.send() == "not configured" and len(s._batches()) == 1 and s.next_send == now[0] + 600
    assert s.config["level"] == "warning" and s.config["exclude"] == ["sizeup-whisper"]
    assert json.loads((tmp_path / "logspool" / "config.json").read_text())["level"] == "warning"
    now[0] = s.next_send
    assert s.send() == "sent" and not s._batches()
    # the cap: the oldest batch goes first
    monkeypatch.setattr(logship, "SPOOL_MAX_BYTES", 1)
    s2, _, _ = shipper(tmp_path / "b")
    for i in range(3):
        s2._add(T0 + i, "sizeup-station", "info", f"line {i}")
        s2._cursor_seen = f"c{i}"
        s2.spool(force=True)
    assert s2.dropped_spool >= 2 and len(s2._batches()) <= 1


def test_off_reads_nothing_and_forgets_the_cursor(tmp_path, monkeypatch):
    s, c, now = shipper(tmp_path)
    s.step()
    s.spool(force=True)
    monkeypatch.setenv("SIZEUP_LOG_SHIP", "off")
    assert s.step() == "off" and s.stats()["on"] is False and not (tmp_path / "logspool" / "cursor").exists()
    monkeypatch.delenv("SIZEUP_LOG_SHIP")
    s._set_config({"on": False})
    assert s.step() == "off" and s.reason == "off in Settings"


def test_no_journal_is_said_not_raised(tmp_path):
    def none(cursor, since, limit):
        raise RuntimeError("no journalctl here")
    s, c, now = shipper(tmp_path)
    s._journal = none
    assert s.step() == "no journalctl here" and s.stats()["state"] == "no journal"


# -- the hub -----------------------------------------------------------------------------------------------------

class FakeLogs:
    """CloudWatch Logs' JSON API, enough of it."""

    def __init__(self, group=cwlogs.STATION_GROUP, retention=30, deny=False):
        self.calls, self.group, self.retention, self.deny = [], group, retention, deny
        self.streams: dict[str, list] = {}
        self.queries = 0

    def __call__(self, target, body, reg, timeout=15.0):
        self.calls.append((target, body))
        if self.deny:
            raise aws.AwsError("AccessDeniedException: not authorized", "AccessDeniedException", 400)
        if target == "DescribeLogGroups":
            return {"logGroups": [{"logGroupName": self.group, "retentionInDays": self.retention, "storedBytes": 1234}]
                    if self.group else []}
        if target == "PutRetentionPolicy":
            self.retention = body["retentionInDays"]
            return {}
        if target == "CreateLogStream":
            if body["logStreamName"] in self.streams:
                raise aws.AwsError("ResourceAlreadyExistsException", "ResourceAlreadyExistsException", 400)
            self.streams[body["logStreamName"]] = []
            return {}
        if target == "PutLogEvents":
            assert len(body["logEvents"]) <= cwlogs.MAX_PUT_EVENTS
            ts = [e["timestamp"] for e in body["logEvents"]]
            assert ts == sorted(ts)
            self.streams[body["logStreamName"]].extend(body["logEvents"])
            return {}
        if target == "StartQuery":
            self.queries += 1
            return {"queryId": "q1"}
        if target == "GetQueryResults":
            st = cwlogs.STATION_GROUP
            return {"status": "Complete", "statistics": {"bytesScanned": 2048.0, "recordsMatched": 2.0},
                    "results": [
                        [{"field": "@timestamp", "value": "2025-11-12 12:20:11.000"},
                         {"field": "@message", "value": '{"unit":"sizeup-station","level":"error","msg":"Traceback"}'},
                         {"field": "@log", "value": f"000000000000:{st}"}, {"field": "@logStream", "value": "pcfd-2025-11-12"}],
                        [{"field": "@timestamp", "value": "2025-11-12 12:20:10.000"},
                         {"field": "@message", "value": "12:20:10 audio: s3 put failed (password: x9)"},
                         {"field": "@log", "value": "000000000000:/ecs/sizeup-hub"},
                         {"field": "@logStream", "value": "hub/hub/0123abcd"}]]}
        raise AssertionError(target)


def sink(fake, settings=None, status=None, clock=T0 + 60):
    store = {} if settings is None else settings
    return StationLogs(settings=lambda: store, station_status=lambda: status, log=lambda m: None, call=fake,
                       clock=lambda: clock)


def batch(lines, station="pcfd"):
    return gzip.compress(json.dumps({"v": 1, "station": station, "batch": "b1", "lines": lines}).encode())


GZ = {"Content-Encoding": "gzip"}


def test_not_configured_until_the_aws_step_has_run_and_the_station_keeps_its_batch():
    for fake, why in ((FakeLogs(group=""), "no log group"), (FakeLogs(deny=True), "AccessDeniedException")):
        sl = sink(fake)
        code, out = sl.ingest(batch([[int(T0 * 1000), "sizeup-station", "info", "x"]]), GZ)
        assert code == 200 and out["accepted"] is False and out["retry_s"] == 600 and why in out["reason"]
        assert "25-station-logs.sh" in out["reason"] and sl.status()["configured"] is False
        assert not any(t == "PutLogEvents" for t, _ in fake.calls)


def test_the_hub_filters_scrubs_again_and_writes_one_stream_a_day():
    fake = FakeLogs(retention=None)
    sl = sink(fake, settings={"exclude": ["sizeup-ais"], "level": "info"})
    ms = int(T0 * 1000)
    lines = [[ms + 2, "sizeup-station", "info", "FG1: carrier 4.2 s"],
             [ms + 1, "sizeup-station", "info", "GET https://u:pw1@example.org/x"],
             [ms + 3, "sizeup-ais", "info", "ais: 4 vessels"],                     # excluded
             [ms + 4, "sizeup-station", "debug", "rx buffer"],                    # below the level
             [ms + 5, "sizeup-station", "info", "the password is hunter7"],       # fail closed
             [ms - 20 * 86400 * 1000, "sizeup-station", "info", "too old"],
             ["x", "bad", "info", "malformed"]]
    code, out = sl.ingest(batch(lines), GZ)
    assert code == 200 and out["accepted"] is True and out["n"] == 2 and out["config"]["exclude"] == ["sizeup-ais"]
    assert fake.retention == 30                                 # the group's retention brought to the setting
    (stream, evs), = fake.streams.items()
    assert stream == "pcfd-2025-11-12"
    assert [json.loads(e["message"]) for e in evs] == [
        {"unit": "sizeup-station", "level": "info", "msg": "GET https://[redacted]@example.org/x"},
        {"unit": "sizeup-station", "level": "info", "msg": "FG1: carrier 4.2 s"}]
    assert sl.today["lines"] == 2 and sl.today["dropped"] == 5
    # a second batch: the stream is not made again
    sl.ingest(batch([[ms + 9, "sizeup-station", "info", "again"]]), GZ)
    assert sum(t == "CreateLogStream" for t, _ in fake.calls) == 1
    # off in Settings: accepted and dropped (the station stops reading)
    off = sink(FakeLogs(), settings={"on": False})
    code, out = off.ingest(batch(lines), GZ)
    assert out["accepted"] is True and out["config"]["on"] is False


def test_a_big_batch_is_split_within_cloudwatchs_limits(monkeypatch):
    monkeypatch.setattr(cwlogs, "MAX_PUT_EVENTS", 100)
    fake = FakeLogs()
    sl = sink(fake)
    ms = int(T0 * 1000)
    code, out = sl.ingest(batch([[ms + i, "sizeup-station", "info", f"line {i}"] for i in range(250)]), GZ)
    assert out["n"] == 250 and sum(t == "PutLogEvents" for t, _ in fake.calls) == 3


def test_bad_bodies():
    sl = sink(FakeLogs())
    assert sl.ingest(None, {})[0] == 413
    assert sl.ingest(b"nope", GZ)[0] == 400
    assert sl.ingest(json.dumps({"lines": "x"}).encode(), {})[0] == 400


def test_queries_are_bounded_and_built_safely():
    sl = sink(FakeLogs(), clock=T0)
    b = sl.build({"source": ["station"], "unit": ["sizeup-station"], "level": ["warning"], "q": ['a"b/c|d']})
    assert b["groups"] == [cwlogs.STATION_GROUP] and b["end"] - b["start"] == cwlogs.DEFAULT_WINDOW_S + 1
    assert 'unit = "sizeup-station"' in b["query"] and 'level in ["warning", "error"]' in b["query"]
    assert '@message like /(?i)a\\"b\\/c\\|d/' in b["query"] and b["query"].rstrip().endswith("limit 200")
    h = sl.build({"unit": ["hub"], "level": ["error"]})
    assert h["groups"] == [cwlogs.HUB_GROUP] and "@logStream like /^hub\\//" in h["query"] and "traceback" in h["query"]
    a = sl.build({"source": ["all"], "level": ["warning"], "limit": ["9999"]})
    assert len(a["groups"]) == 2 and "ispresent(level)" in a["query"] and a["limit"] == cwlogs.MAX_ROWS
    for bad in ({"source": ["x"]}, {"unit": ["a b"]}, {"level": ["loud"]}, {"q": ["x" * 200]}, {"q": ["a\nb"]},
                {"start": [str(T0 - 8 * 86400)], "end": [str(T0)]}, {"start": [str(T0)], "end": [str(T0 - 1)]}):
        with pytest.raises(LogsError):
            sl.build(bad)


def test_a_query_answers_rows_with_the_hubs_lines_scrubbed_too():
    fake = FakeLogs()
    sl = sink(fake)
    out = sl.query({"source": ["all"]})
    assert out["scanned_bytes"] == 2048.0 and len(out["rows"]) == 2
    st, hub = out["rows"]
    assert st == {"t": pytest.approx(1762950011.0), "source": "station", "unit": "sizeup-station", "level": "error",
                  "msg": "Traceback"}
    assert hub["source"] == "hub" and hub["unit"] == "hub" and hub["level"] == "error"
    assert "x9" not in hub["msg"] and "[redacted]" in hub["msg"]
    assert "000000000000" not in json.dumps(out)              # never the account


# -- the API and Settings ----------------------------------------------------------------------------------------

def test_logs_are_a_signed_in_admins_and_settings_set_reset_and_audit(tmp_path):
    from sizeup.cloud.access import Identity
    from tests.test_push_settings import Rig
    from tests.test_roles import COREY, JULIAN, call
    rig = Rig(tmp_path)
    get = lambda p, q="", who=COREY: rig.api.get(p, q, Identity(email=who) if who else None)   # noqa: E731
    assert get("/api/cad/logs/status")[0] == 404                      # not the hub: nothing there
    fake = FakeLogs()
    rig.cad.station_logs = sink(fake, status={"logship": {"state": "ok", "spool_bytes": 0}})
    rig.cad.station_logs.settings = lambda: rig.cad.settings.section(cwlogs.SECTION)
    assert get("/api/cad/logs/status", who=None)[0] == 403             # never without a verified sign-in
    assert get("/api/cad/logs/status", who=JULIAN)[0] == 403           # a dispatcher
    code, raw, _ = get("/api/cad/logs/status")
    st = json.loads(raw)
    assert code == 200 and st["configured"] is True and st["station"]["state"] == "ok" and st["settings"]["level"] == "info"
    code, raw, _ = get("/api/cad/logs", "source=station&level=bogus")
    assert code == 400
    code, raw, _ = get("/api/cad/logs", "source=all")
    assert code == 200 and len(json.loads(raw)["rows"]) == 2
    post = lambda body, who=COREY: call(rig.api, "/api/cad/settings/station_logs", body, who)   # noqa: E731
    assert post({"action": "set", "key": "level", "value": "warning"})[0] == 200
    code, out = post({"action": "set", "key": "exclude", "value": "sizeup-ais, sizeup-adsb.service"})
    assert code == 200 and out["station_logs"]["exclude"]["value"] == ["sizeup-adsb", "sizeup-ais"]
    assert post({"action": "set", "key": "retention_days", "value": 90})[0] == 200
    for body in ({"action": "set", "key": "level", "value": "loud"}, {"action": "set", "key": "retention_days", "value": 31},
                 {"action": "set", "key": "on", "value": 1}, {"action": "set", "key": "nope", "value": 1},
                 {"action": "set", "key": "include", "value": ["a b"]}):
        assert post(body)[0] == 400, body
    assert post({"action": "set", "key": "level", "value": "error"}, who=JULIAN)[0] == 403
    assert rig.cad.station_logs.cfg() == {"on": True, "level": "warning", "include": [],
                                          "exclude": ["sizeup-adsb", "sizeup-ais"], "retention_days": 90}
    rig.cad.station_logs.check()
    assert fake.retention == 90
    assert post({"action": "reset", "key": "level"})[0] == 200 and rig.cad.station_logs.cfg()["level"] == "info"
    audit = rig.cad.settings_audit_log({"section": ["system"]})["audit"]
    assert [a["key"] for a in reversed(audit) if a["key"].startswith("station_logs/")] == [
        "station_logs/level", "station_logs/exclude", "station_logs/retention_days", "station_logs/level"]
    assert rig.cad.settings_get()["station_logs"]["retention_days"]["value"] == 90


def test_the_hub_takes_a_batch_over_the_station_link(tmp_path, monkeypatch):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import TOKEN as HUB_TOKEN
    from tests.test_cloud import hub_cfg
    app = HubApp(hub_cfg(tmp_path), log=lambda m: None)
    try:
        fake = FakeLogs()
        app.station_logs._call = fake
        app.station_logs.clock = lambda: T0 + 60
        body = batch([[int(T0 * 1000), "sizeup-station", "info", "hub link: connected"]])
        hdrs = {"Authorization": f"Bearer {HUB_TOKEN}", "Content-Encoding": "gzip"}
        code, raw, _ = app.hub.handle("POST", "/api/station/logs", "", hdrs, lambda n: body)
        assert code == 200 and json.loads(raw)["accepted"] is True
        assert app.cad.station_logs is app.station_logs and sum(map(len, fake.streams.values())) == 1
        code, _, _ = app.hub.handle("POST", "/api/station/logs", "", {"Authorization": "Bearer nope"}, lambda n: body)
        assert code == 401
    finally:
        app.close() if hasattr(app, "close") else None
