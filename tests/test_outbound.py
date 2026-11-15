"""sizeup/outbound.py: a hub started by hand (a restored production copy, docs/development.md) reaches nothing outside.

Armed off, every outbound path refuses before a byte leaves: ntfy (publish, the Notifier, the replica watch's Pusher),
every AWS request (sign / presign / json_call: S3 via attachments and the Listen links, CloudWatch Logs, SSM both
ways, Transcribe's streaming URL, the map caches' S3 signing), Polly and Bedrock. This machine's own addresses stay
allowed. The production hub (under its supervisor: a lease file), a station and SIZEUP_ALLOW_OUTBOUND=1 are on; not
armed (tests, tools, the supervisor's own process) nothing changes. Every value here is invented."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sizeup import aws, notify, outbound

ROOT = Path(__file__).resolve().parent.parent
CREDS = aws.Credentials("AKIDEXAMPLE", "secret-example")
TOPIC = "example-status-topic-0000"
REMOTE_S3 = "https://example-bucket.s3.us-east-1.amazonaws.com/a/b.wav"


@pytest.fixture(autouse=True)
def _disarmed_after():
    yield
    outbound.disarm()


@pytest.fixture
def off():
    outbound.arm("hub", env={})
    assert outbound.status() == {"armed": True, "on": False,
                                 "why": "hub without the supervisor's lease and without SIZEUP_ALLOW_OUTBOUND=1"}


def nothing_leaves(monkeypatch):
    """Every way out through urllib fails the test (the guard must refuse first)."""
    def boom(*a, **kw):
        raise AssertionError("a request left the process")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(notify, "urlopen", boom)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", boom)


def local_server():
    """A local HTTP server answering {} to everything, and what it was asked."""
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.append((self.path, self.rfile.read(n)))
            body = json.dumps({"id": "x1", "time": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", got


# -- who is on --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role, env, on", [
    ("hub", {"SIZEUP_LEASE_FILE": "/run/sizeup/lease.json"}, True),     # production: under the handoff supervisor
    ("station", {}, True),                                              # the station's own calls
    ("hub", {"SIZEUP_ALLOW_OUTBOUND": "1"}, True),
    ("", {"SIZEUP_ALLOW_OUTBOUND": "1"}, True),
    ("hub", {}, False),                                                 # a restored copy run by hand
    ("", {}, False),                                                    # all-in-one
    ("hub", {"SIZEUP_ALLOW_OUTBOUND": "true"}, False),                  # only "1" says yes
    ("hub", {"SIZEUP_LEASE_FILE": "  "}, False),
])
def test_who_may_reach_outside(role, env, on):
    assert outbound.decide(role, env)[0] is on
    line = outbound.arm(role, env)
    assert outbound.status()["on"] is on
    assert line.startswith("outbound: on" if on else "outbound: OFF")
    if not on:
        assert "SIZEUP_ALLOW_OUTBOUND=1" in line


def test_not_armed_nothing_changes():
    assert outbound.status() == {"armed": False, "on": True, "why": ""}
    assert outbound.check("ntfy", "https://ntfy.sh") == ""
    assert "Authorization" in aws.sign("GET", REMOTE_S3, {}, b"", CREDS, "us-east-1", "s3")


@pytest.mark.parametrize("url, refused", [("https://ntfy.sh", True), ("https://s3.amazonaws.com/x", True),
                                          ("http://127.0.0.1:9", False), ("http://localhost:8080/", False),
                                          ("http://[::1]:9/", False), ("http://127.0.0.1.example.org/", True)])
def test_only_this_machine_while_off(off, url, refused):
    assert bool(outbound.check("x", url)) is refused


# -- ntfy -------------------------------------------------------------------------------------------------------------


def test_publish_refuses_a_real_server_and_sends_nothing(off, monkeypatch):
    nothing_leaves(monkeypatch)
    with pytest.raises(notify.NotifyError, match="outbound off"):
        notify.publish("https://ntfy.sh", TOPIC, "Station silent", "invented")


def test_publish_to_a_local_ntfy_still_goes(off):
    srv, base, got = local_server()
    try:
        notify.publish(base, TOPIC, "Station silent", "invented")
        assert len(got) == 1 and json.loads(got[0][1])["topic"] == TOPIC
    finally:
        srv.shutdown()


def test_the_notifier_gives_up_without_sending(off, monkeypatch):
    nothing_leaves(monkeypatch)
    logs = []
    n = notify.Notifier("https://ntfy.sh", TOPIC, log=logs.append, sleep=lambda s: None)
    assert n.send("Station silent", "invented") and n.flush(5)
    assert n.sent == 0 and n.failed == 1 and "outbound off" in n.last_error


def test_the_replica_watch_pusher_pushes_nothing(off, monkeypatch):
    from sizeup.replicawatch import Pusher, WatchStore
    nothing_leaves(monkeypatch)
    logs = []
    p = Pusher(WatchStore(None), "https://ntfy.sh", TOPIC, log=logs.append, sleep=lambda s: None)
    assert p.push("failing", "Replica check failed", "invented") is False
    assert p.sent == [] and any("outbound off" in x for x in logs)


# -- AWS --------------------------------------------------------------------------------------------------------------


def test_sign_and_presign_refuse(off):
    with pytest.raises(aws.AwsError, match="outbound off") as e:
        aws.sign("PUT", REMOTE_S3, {}, b"x", CREDS, "us-east-1", "s3")
    assert e.value.code == "OutboundOff"
    with pytest.raises(aws.AwsError, match="outbound off"):
        aws.presign(REMOTE_S3, CREDS, "us-east-1", "s3")
    assert "Authorization" in aws.sign("PUT", "http://127.0.0.1:9/b/k", {}, b"x", CREDS, "us-east-1", "s3")


def test_json_call_refuses_before_the_network_and_a_local_endpoint_goes(off, monkeypatch):
    srv, base, got = local_server()
    try:
        assert aws.json_call("transcribe", "Transcribe.GetVocabulary", {"VocabularyName": "v"}, "us-east-1",
                             endpoint=base, creds=CREDS) == {"id": "x1", "time": 1}
        assert len(got) == 1
        nothing_leaves(monkeypatch)
        with pytest.raises(aws.AwsError, match="outbound off"):
            aws.json_call("transcribe", "Transcribe.GetVocabulary", {"VocabularyName": "v"}, "us-east-1", creds=CREDS)
    finally:
        srv.shutdown()


def test_s3_puts_and_the_listen_link_refuse_without_sending(off):
    from sizeup.attachments import S3
    from sizeup.intel.listen import S3ListenStore
    sent = []
    s3 = S3("example-bucket", "us-east-1", send=lambda *a: sent.append(a) or (200, {}, b""), creds=lambda: CREDS)
    with pytest.raises(aws.AwsError, match="outbound off"):
        s3.put("a/b.wav", b"RIFF", "audio/wav")
    store = S3ListenStore("example-bucket", "us-east-1", send=lambda *a: sent.append(a) or (200, {}, b""),
                          creds=lambda: CREDS)
    with pytest.raises(aws.AwsError, match="outbound off"):
        store.presign("a/b.wav", 3600, "audio/wav")
    assert sent == []


def test_the_map_caches_s3_signing_refuses(off):
    from sizeup.cachesync import _sign_s3
    with pytest.raises(aws.AwsError, match="outbound off"):
        _sign_s3("PUT", "https://example-bucket.s3.us-east-1.amazonaws.com/sizeup-hub/cache/x.gz", {},
                 aws.EMPTY_SHA256, CREDS, "us-east-1")


def test_cloudwatch_logs_refuse(off, monkeypatch):
    from sizeup.cloud import cwlogs
    nothing_leaves(monkeypatch)
    monkeypatch.setattr(aws, "credentials", lambda now=None: CREDS)
    with pytest.raises(aws.AwsError, match="outbound off"):
        cwlogs._call("DescribeLogGroups", {}, "us-east-1")


def test_ssm_both_ways_refuse_without_sending(off):
    from sizeup.cloud import adapterssm
    sent = []
    reads = {adapterssm.KEY_ID: "AKIAEXAMPLE000000000", adapterssm.SECRET: "secret-example-0123456789",
             adapterssm.REGION: "us-east-1"}
    with pytest.raises(adapterssm.SsmError) as e:                      # the station's read (exempt there: role)
        adapterssm.get(["TEST_FEED_PASS"], read=reads.get, send=lambda *a: sent.append(a) or (200, b"{}"))
    assert e.value.code == "OutboundOff"
    from sizeup.cloud.adapterpass import AdapterPassword, SaveRefused
    ap = AdapterPassword("/sizeup-station/adapter/TEST_FEED_PASS", "1234abcd-12ab-34cd-56ef-1234567890ab",
                         region="us-east-1", send=lambda *a: sent.append(a) or (200, b"{}"), creds=lambda: CREDS)
    with pytest.raises(SaveRefused) as e:                              # the hub's write
        ap._call("AmazonSSM.DescribeParameters", b"{}")
    assert e.value.code == "OutboundOff" and e.value.status == 503
    assert ap.describe(fresh=True).get("error")
    assert sent == []


def test_transcribe_streaming_url_refuses(off):
    with pytest.raises(aws.AwsError, match="outbound off"):
        aws.presign("wss://transcribestreaming.us-east-1.amazonaws.com:8443/stream-transcription-websocket?x=1", CREDS,
                    "us-east-1", "transcribe")


# -- Polly, Bedrock ---------------------------------------------------------------------------------------------------


def test_polly_refuses_before_connecting(off, monkeypatch):
    from sizeup import polly
    import http.client
    monkeypatch.setattr(polly, "credentials", lambda: (_ for _ in ()).throw(AssertionError("no credentials asked")))
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", lambda self: (_ for _ in ()).throw(AssertionError("left")))
    with pytest.raises(polly.PollyError, match="outbound off") as e:
        polly.request("Engine 60 respond", "Matthew", "neural", False, "us-east-1")
    assert e.value.code == "OutboundOff"


def test_bedrock_refuses_before_a_client_is_made(off):
    from sizeup.intel import infer
    p = infer.BedrockProvider("anthropic.claude-haiku-5-5", "us-east-1")
    p.client = lambda: (_ for _ in ()).throw(AssertionError("a client was made"))
    with pytest.raises(infer.OutboundOff, match="outbound off"):
        p.run(infer.Request("invented words", {"fire": "Fire"}, ["Example Town"], "FG1"))


# -- the start, /healthz, Health --------------------------------------------------------------------------------------


def test_a_hub_started_by_hand_says_so_at_start(tmp_path, monkeypatch, capsys):
    from sizeup import app as app_mod
    for k in ("SIZEUP_LEASE_FILE", "SIZEUP_ALLOW_OUTBOUND", "SIZEUP_APP_GATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", "example-station-token-0123456789")
    lines = []
    monkeypatch.setattr(app_mod, "log", lines.append)
    assert app_mod.main(["--config", str(ROOT / "deploy/hub/config.hub.toml"), "--init-state"]) == 0
    assert any(x.startswith("outbound: OFF (hub without the supervisor's lease") for x in lines), lines
    assert outbound.status()["on"] is False


def test_under_the_supervisor_it_is_on_and_healthz_is_as_before(tmp_path, monkeypatch):
    from sizeup import app as app_mod
    monkeypatch.setenv("SIZEUP_LEASE_FILE", str(tmp_path / "lease.json"))
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", "example-station-token-0123456789")
    lines = []
    monkeypatch.setattr(app_mod, "log", lines.append)
    assert app_mod.main(["--config", str(ROOT / "deploy/hub/config.hub.toml"), "--init-state"]) == 0
    assert "outbound: on (under the hub's supervisor (lease))" in lines
    assert outbound.status()["on"] is True


def test_healthz_and_the_health_panel_show_it_off(tmp_path, off):
    from sizeup.app import make_app
    from tests.test_cloud import hub_cfg
    app = make_app(hub_cfg(tmp_path))
    try:
        assert app.health_extra()["outbound"].startswith("off: hub without the supervisor's lease")
        assert app.health.panel()["outbound"]["on"] is False          # Settings > Station > Health: "Outbound off"
        outbound.arm("hub", {"SIZEUP_LEASE_FILE": "/x"})
        assert "outbound" not in app.health_extra()
        assert app.health.panel()["outbound"]["on"] is True
    finally:
        app.stop()
    js = (ROOT / "sizeup/web/static/health.js").read_text()
    assert '"Outbound off"' in js and "d.outbound" in js


NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_health_page_still_parses():
    r = subprocess.run([NODE, "--check", str(ROOT / "sizeup/web/static/health.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# -- forwarding through Settings, a final refusal, the pre-alert feed -------------------------------------------------


def test_forwarding_to_a_hub_counts_as_a_station():
    assert outbound.forwarding(True) == ""                              # not armed: nothing to say
    outbound.arm("", {})
    assert outbound.forwarding(True).startswith("outbound: on (forwarding to a hub")
    assert outbound.status()["on"] is True and outbound.forwarding(True) == ""
    assert outbound.forwarding(False).startswith("outbound: OFF (all-in-one")
    outbound.arm("station", {})
    assert outbound.forwarding(False) == "" and outbound.status()["on"] is True    # a station stays one
    outbound.arm("hub", {})
    assert outbound.forwarding(True) == "" and outbound.status()["on"] is False     # a hub's decision never moves


def test_an_all_in_one_config_forwarding_by_cloud_json_is_on_and_follows_the_switch(tmp_path, monkeypatch):
    from sizeup.app import make_app
    from tests.test_stationmode import cfg_
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", "example-station-token-0123456789")
    state = tmp_path / "state"
    state.mkdir()
    (state / "cloud.json").write_text(json.dumps({"forward": True, "hub_url": "http://127.0.0.1:9"}))
    outbound.arm("", {})                                                # main(): an all-in-one config
    assert outbound.status()["on"] is False
    app = make_app(cfg_(tmp_path))
    try:
        assert app.station.forward is True and outbound.status()["on"] is True
        assert outbound.status()["why"].startswith("forwarding to a hub")
        app.station.set({"forward": False, "operator": "Test"})
        assert outbound.status()["on"] is False
    finally:
        app.station.set({"forward": False})
        app.stop()


def test_a_refused_push_is_dropped_at_once_not_retried(off, monkeypatch):
    nothing_leaves(monkeypatch)
    slept, logs = [], []
    n = notify.Notifier("https://ntfy.sh", TOPIC, log=logs.append, sleep=slept.append)
    assert n.send("Station silent", "invented") and n.flush(5)
    assert slept == [] and n.failed == 1 and n.sent == 0
    assert [x for x in logs if x.startswith("notify: not sent")] == [logs[-1]] and "outbound off" in logs[-1]
    with pytest.raises(notify.NotifyOff):
        notify.publish("https://ntfy.sh", TOPIC, "x", "y")


def test_the_replica_watch_does_not_try_again_either(off, monkeypatch):
    from sizeup.replicawatch import Pusher, WatchStore
    nothing_leaves(monkeypatch)
    slept = []
    p = Pusher(WatchStore(None), "https://ntfy.sh", TOPIC, log=lambda m: None, sleep=slept.append)
    assert p.push("failing", "Replica check failed", "invented") is False and slept == []


def test_the_phone_pre_alert_does_not_subscribe_while_off(off, tmp_path, monkeypatch):
    """Off: no request, and quiet. The feed stands for its topic, so the 2 s topic check sees nothing changed: one log
    line in all, no screen refresh, never "subscribed", never a topic's name (as Settings hands them in, topics())."""
    from sizeup import prealert
    from tests.test_cad import make_cad
    nothing_leaves(monkeypatch)
    logs, changes = [], []
    pa = prealert.PreAlerts(make_cad(tmp_path), "https://ntfy.sh", log=logs.append,
                            topics=lambda: ("example-ring-topic-5678", "example-heartbeat-topic-5678"))
    pa.on_change = lambda: changes.append(1)
    try:
        pa.start()
        time.sleep(4.5)                                                # past two of the expiry loop's 2 s checks
        assert changes == [], len(changes)                              # no screen refresh every 2 s
        assert [x for x in logs if x.startswith("prealert:")] == [
            "prealert: not subscribed (prealert: outbound off (hub without the supervisor's lease and without "
            "SIZEUP_ALLOW_OUTBOUND=1))"], logs
        assert isinstance(pa.ring_stream, prealert.OffFeed) and isinstance(pa.status_stream, prealert.OffFeed)
        assert not any("example-" in x or "subscribed (read-only)" in x for x in logs)
        assert pa.ring_stream.state()["error"] == "not subscribed: outbound off"
    finally:
        pa.stop()
    outbound.arm("hub", {"SIZEUP_ALLOW_OUTBOUND": "1"})
    assert isinstance(prealert.PreAlerts(make_cad(tmp_path / "b"), "https://ntfy.sh", "example-ring-topic-5678",
                                         log=lambda m: None)._feed("ring", "example-ring-topic-5678"), prealert.NtfyStream)
