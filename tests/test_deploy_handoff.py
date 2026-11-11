"""The hot/cold deploy's other pieces (GitHub #247): the web server's drain and write fence, the station's quick
retries, the cache mirror's fence, the Cloudflare connector wrapper and the entrypoint (as scripts), the real
Litestream calls (against a fake `litestream`), the task definition, the deploy workflow, and the CAD page's
"Reconnecting…" pill (node)."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")


def until(cond, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def get(url, data=None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# -- the web server: drain and fence ---------------------------------------------------------------------------------

def test_drain_answers_new_requests_503_retry_after_and_lets_the_one_in_flight_finish():
    from sizeup.web.server import WebServer
    started, release = threading.Event(), threading.Event()

    def close_incident(iid):
        started.set()
        release.wait(5)
        return True
    ws = WebServer("127.0.0.1", 0, {}, lambda: {"time": 0}, lambda cid: None, close_incident)
    serving = threading.Thread(target=ws.serve_forever, daemon=True)
    serving.start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    assert get(base + "/healthz")[0] == 200
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", get(base + "/api/incident/x/close", b"{}")))
    t.start()
    assert started.wait(5)
    ws.drain(wait_s=5)
    code, headers, body = get(base + "/healthz")
    assert code == 503 and headers["Retry-After"] == "2" and json.loads(body)["updating"] is True
    assert get(base + "/api/cad/state")[0] == 503
    assert serving.is_alive()                         # still waiting for the one in flight
    release.set()
    t.join(5)
    assert out["r"][0] == 200                          # it finished
    serving.join(5)
    assert not serving.is_alive()                      # then the server stopped (main() stops the app next)


def test_a_write_is_refused_while_the_fence_says_the_lease_isnt_ours():
    from sizeup.web.server import WebServer

    class F:
        good = True

        def ok(self):
            return self.good
    f = F()
    closed = []
    ws = WebServer("127.0.0.1", 0, {}, lambda: {"time": 0}, lambda cid: None, lambda iid: closed.append(iid) or True, fence=f)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    try:
        assert get(base + "/api/incident/a/close", b"{}")[0] == 200
        f.good = False
        code, headers, _ = get(base + "/api/incident/b/close", b"{}")
        assert code == 503 and headers["Retry-After"] == "2"
        assert get(base + "/healthz")[0] == 200            # reads go on (the watchdog ends the process)
        assert closed == ["a"]
    finally:
        ws.shutdown()


def test_draining_ends_the_stations_long_poll_at_once():
    from sizeup.cloud.hub import Jobs
    j = Jobs()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("jobs", j.take(20.0)))
    t0 = time.monotonic()
    t.start()
    time.sleep(0.1)
    j.close()
    t.join(5)
    assert out["jobs"] == [] and time.monotonic() - t0 < 2
    j.put("tts", {}, ttl=60)
    assert j.take(0.0) == []                            # nothing more is handed out here


# -- the station's retries -----------------------------------------------------------------------------------------

def test_the_station_backoff_is_seconds_through_a_deploy_and_longer_only_for_an_outage():
    from sizeup.cloud.client import Backoff, HubError
    now = [0.0]
    b = Backoff(first=1.0, fast_cap=5.0, cap=60.0, fast_for=180.0, clock=lambda: now[0], jitter=lambda a, z: 1.0)
    waits = []
    for _ in range(8):
        w = b.fail(HubError("hub said 502", 502))
        waits.append(w)
        now[0] += w
    assert waits == [1, 2, 4, 5, 5, 5, 5, 5]           # a deploy's gap of seconds: tried again within 5 s
    assert b.fail(HubError("updating", 503, retry_after=2.0)) == 2.0      # the hub's own Retry-After
    now[0] = 200.0                                      # an outage over 3 min: back off to the cap
    assert [b.fail() for _ in range(3)][-1] == 60
    b.ok()
    assert b.fail() == 1.0


def test_the_hubs_retry_after_reaches_the_station():
    from sizeup.cloud.client import HubClient, HubError

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"error": "the hub is updating", "updating": true}'
            self.send_response(503)
            self.send_header("Retry-After", "2")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(HubError) as e:
            HubClient(f"http://127.0.0.1:{srv.server_address[1]}", "t" * 20).request("GET", "/api/station/jobs")
        assert e.value.status == 503 and e.value.retry_after == 2.0 and "updating" in str(e.value)
    finally:
        srv.shutdown()


def test_every_link_thread_uses_the_quick_backoff():
    src = (ROOT / "sizeup" / "cloud" / "station.py").read_text()
    assert src.count("Backoff(first=1.0") == 3 and "backoff.fail(e)" in src
    assert "min(backoff * 2, 60.0)" not in src


# -- the cache mirror ------------------------------------------------------------------------------------------------

def _mirror(tmp_path, fence=None):
    from sizeup import aws
    from sizeup.cachesync import CacheMirror
    puts = []

    def send(method, url, headers, body, timeout):
        if method == "PUT":
            puts.append(url)
        return 200, b""
    m = CacheMirror(tmp_path, "bucket", send=send, creds=lambda: aws.Credentials("AKIDEXAMPLE", "x"), log=lambda s: None,
                    fence=fence)
    return m, puts


def test_the_cache_mirror_uploads_nothing_while_not_the_leader(tmp_path):
    (tmp_path / "roads511.json").write_text("{}")
    ours = [False]
    m, puts = _mirror(tmp_path, fence=lambda: ours[0])
    assert m.scan() == 0 and puts == []
    ours[0] = True
    assert m.scan() == 1 and len(puts) == 1


def test_what_the_standby_restored_isnt_uploaded_again_by_the_app(tmp_path):
    from sizeup.cachesync import _sha256
    (tmp_path / "gis").mkdir()
    (tmp_path / "gis" / "parcels.geojson").write_text('{"type":"FeatureCollection"}')
    standby, _ = _mirror(tmp_path)
    standby.files["gis/parcels.geojson"] = {"sha": _sha256(tmp_path / "gis/parcels.geojson"), "up": 0, "fails": 0,
                                            "retry": 0.0, "error": "", "restored": True}
    standby.save_manifest()
    app, puts = _mirror(tmp_path)
    assert app.load_manifest() == 1
    assert app.scan() == 0 and puts == []


# -- the connector wrapper (deploy/hub/connector.sh), as a script --------------------------------------------------

FAKE_CLOUDFLARED = """#!/bin/sh
echo "$@" > "$OUT/args"
trap 'echo term >> "$OUT/log"; exit 0' TERM
echo started >> "$OUT/log"
if [ -n "${FAKE_EXIT:-}" ]; then sleep 0.3; exit "$FAKE_EXIT"; fi
while :; do sleep 0.05; done
"""


class Connector:
    def __init__(self, tmp_path, **env):
        self.shared, self.out = tmp_path / "shared", tmp_path / "out"
        self.shared.mkdir()
        self.out.mkdir()
        fake = tmp_path / "cloudflared"
        fake.write_text(FAKE_CLOUDFLARED)
        fake.chmod(0o755)
        self.beating = True
        self.beat_value = None
        threading.Thread(target=self._beat, daemon=True).start()
        e = dict(os.environ, SIZEUP_SHARED_DIR=str(self.shared), CLOUDFLARED=str(fake), OUT=str(self.out),
                 CONNECTOR_TICK="0.05", CONNECTOR_STALE_S="2", **env)
        self.p = subprocess.Popen(["sh", str(ROOT / "deploy/hub/connector.sh")], env=e, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)

    def _beat(self):
        while self.beating:
            tmp = self.shared / "hub-alive.tmp"            # atomic, as handoff._write: cat never reads it half-written
            tmp.write_text(f"{self.beat_value or int(time.time())}\n")
            os.replace(tmp, self.shared / "hub-alive")
            time.sleep(0.2)

    def lead(self):
        (self.shared / "leader").write_text("2\n")

    def log(self):
        p = self.out / "log"
        return p.read_text().split() if p.exists() else []

    def wait(self, timeout=10):
        try:
            return self.p.wait(timeout)
        finally:
            self.beating = False


def test_connector_a_standby_never_starts_cloudflared_and_stops_at_once(tmp_path):
    c = Connector(tmp_path)
    time.sleep(0.4)
    c.p.send_signal(signal.SIGTERM)
    assert c.wait() == 0
    assert c.log() == [] and not (c.out / "args").exists()


def test_connector_starts_on_the_leader_file_outlives_sigterm_and_stops_when_the_hub_says(tmp_path):
    c = Connector(tmp_path)
    time.sleep(0.3)
    assert c.log() == []
    c.lead()
    until(lambda: c.log() == ["started"], what="cloudflared started")
    assert (c.out / "args").read_text().split() == [
        "tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:20241", "--grace-period", "3s", "run",
        "--url", "http://localhost:8090"]
    c.p.send_signal(signal.SIGTERM)                    # ECS stops the task: the connector stays through the handoff
    time.sleep(0.6)
    assert c.p.poll() is None and c.log() == ["started"]
    (c.shared / "leader").unlink()                     # the supervisor: the new leader serves
    assert c.wait() == 0
    assert c.log() == ["started", "term"]


def test_connector_stops_when_the_hub_goes_quiet(tmp_path):
    c = Connector(tmp_path)
    c.lead()
    until(lambda: c.log() == ["started"])
    c.beat_value = int(time.time()) - 60               # the hub container is gone or frozen
    assert c.wait() == 0
    assert c.log() == ["started", "term"]


def test_connector_exits_with_cloudflareds_own_code(tmp_path):
    c = Connector(tmp_path, FAKE_EXIT="7")
    c.lead()
    assert c.wait() == 7


# -- the entrypoint, as a script -----------------------------------------------------------------------------------

def _entry(tmp_path, **env):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "python"
    fake.write_text('#!/bin/sh\necho "$@" > "$OUT/args"\nenv > "$OUT/env"\n')
    fake.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    e = {"PATH": f"{bin_}:/usr/bin:/bin", "OUT": str(out), "SIZEUP_STATE_DIR": str(tmp_path / "data"),
         "SIZEUP_RUN_DIR": str(tmp_path / "run"), "SIZEUP_STATION_TOKEN": "x" * 20, **env}
    r = subprocess.run(["sh", str(ROOT / "deploy/hub/entrypoint.sh")], env=e, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    envs = dict(line.split("=", 1) for line in (out / "env").read_text().splitlines() if "=" in line)
    return (out / "args").read_text().split(), envs


def test_entrypoint_runs_the_supervisor_on_fargate(tmp_path):
    args, env = _entry(tmp_path, LITESTREAM_BUCKET="b", AWS_REGION="us-east-1")
    assert args == ["-m", "sizeup.handoff", "--config", "/app/deploy/hub/config.hub.toml",
                    "--litestream-config", "/app/deploy/hub/litestream.yml"]
    assert env["LITESTREAM_REGION"] == "us-east-1" and env["LITESTREAM_PATH"] == "sizeup-hub"
    assert env["LITESTREAM_SOCKET"] == str(tmp_path / "run" / "litestream.sock")
    assert (tmp_path / "run").is_dir()


def test_entrypoint_without_a_bucket_is_just_sizeup(tmp_path):
    args, _ = _entry(tmp_path)
    assert args == ["-m", "sizeup", "--config", "/app/deploy/hub/config.hub.toml"]


# -- the real Litestream calls, against a fake `litestream` -----------------------------------------------------------

FAKE_LITESTREAM = r'''#!{py}
import json, os, sys
a = sys.argv[1:]
mode = os.environ.get("FAKE_LS", "")
print("time=... level=INFO msg=\"a log line on stdout\"")
if a[0] == "restore" and "-dry-run" in a:
    if mode == "none":
        print("Error: no matching backup files available", file=sys.stderr); sys.exit(1)
    if mode == "s3down":
        print("Error: operation error S3: ListObjectsV2, https response error StatusCode: 503", file=sys.stderr); sys.exit(1)
    print(json.dumps({{"source": a[-1], "max_txid": "00000000000001f4", "files": []}}, indent=2))
elif a[0] == "restore":
    print(json.dumps({{"db_path": a[a.index("-o") + 1], "txid": "00000000000001f5"}}, indent=2))
elif a[0] == "sync":
    assert "-wait" in a and "-json" in a and a[a.index("-socket") + 1] == "/tmp/ls.sock"
    rep = 498 if mode == "behind" else 500
    print(json.dumps({{"db_path": a[-1], "txid": 500, "replica_txid": rep, "duration_ms": 3}}, indent=2))
'''


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    from sizeup.handoff import Env
    b = tmp_path / "bin"
    b.mkdir()
    ls = b / "litestream"
    ls.write_text(FAKE_LITESTREAM.format(py=sys.executable))
    ls.chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    return Env("cfg.toml", tmp_path / "data", "ls.yml", "/tmp/ls.sock", 8090)


def test_litestream_plan_restore_and_sync_answers_are_read(real_env, monkeypatch, tmp_path):
    assert real_env.plan("cad") == 500
    assert real_env.restore("cad", tmp_path / "standby" / "cad.sqlite") == 501
    assert real_env.sync("cad", 5) == 500
    monkeypatch.setenv("FAKE_LS", "none")
    assert real_env.plan("cad") is None                 # no backup at all
    monkeypatch.setenv("FAKE_LS", "s3down")
    with pytest.raises(RuntimeError):
        real_env.plan("cad")                             # S3 trouble is never "no backup"
    monkeypatch.setenv("FAKE_LS", "behind")
    with pytest.raises(RuntimeError, match="replica at 498"):
        real_env.sync("cad", 5)                          # not every change in S3: not a clean release


def test_a_followers_position_is_its_txid_sidecar(real_env, tmp_path):
    out = tmp_path / "standby" / "hub.sqlite"
    assert real_env.applied(out) is None
    out.parent.mkdir(parents=True)
    Path(f"{out}-txid").write_text("00000000000000ff\n")
    assert real_env.applied(out) == 255


def test_litestream_config_has_the_control_socket_and_logs_on_stderr():
    text = (ROOT / "deploy/hub/litestream.yml").read_text()
    assert "socket:\n  enabled: true\n  path: ${LITESTREAM_SOCKET}" in text and "stderr: true" in text
    assert "exec" not in text.split("dbs:")[1]


# -- the task definition and the deploy -------------------------------------------------------------------------------

def _taskdef():
    return json.loads((ROOT / "deploy/aws/taskdef.json").read_text())


def test_the_task_definition_hands_over():
    td = _taskdef()
    hub, cf = (next(c for c in td["containerDefinitions"] if c["name"] == n) for n in ("hub", "cloudflared"))
    assert td["volumes"][0] == {"name": "shared"}
    for c in (hub, cf):
        assert c["stopTimeout"] == 120 and c["essential"] is True
        assert c["mountPoints"][0] == {"sourceVolume": "shared", "containerPath": "/run/sizeup-shared"}
        assert c["linuxParameters"]["initProcessEnabled"] is True
        assert "dependsOn" not in c                    # ECS would stop them in order, and health is the standby's
    assert "127.0.0.1:8089/healthz" in hub["healthCheck"]["command"][1] and hub["healthCheck"]["startPeriod"] == 300
    assert cf["image"] == hub["image"] and cf["entryPoint"] == ["/app/deploy/hub/connector.sh"]
    assert [s["name"] for s in cf["secrets"]] == ["TUNNEL_TOKEN"]
    assert "TUNNEL_TOKEN" not in {s["name"] for s in hub["secrets"]}


# -- the DVR's EFS volume and the attachments bucket (#322, #323; sizeup-aws 14-dvr-storage.sh) ---------------------------

EFS_FS = re.compile(r"fs-[0-9a-f]{8,40}")
EFS_AP = re.compile(r"fsap-[0-9a-f]{8,40}")


def test_the_task_definition_mounts_the_dvr_on_efs_and_names_the_attachments_bucket():
    td = _taskdef()
    hub, cf = (next(c for c in td["containerDefinitions"] if c["name"] == n) for n in ("hub", "cloudflared"))
    # 0.5 vCPU with 2 GB (Fargate: 1-4 GB at 512): 1 GB already peaked at 72-80% before 24/7 recording, the radio
    # backfill and clip uploads
    assert (td["cpu"], td["memory"]) == ("512", "2048")
    vols = {v["name"]: v for v in td["volumes"]}
    assert set(vols) == {"shared", "dvr"}
    efs = vols["dvr"]["efsVolumeConfiguration"]
    assert efs["transitEncryption"] == "ENABLED"                     # the file system policy denies plain NFS
    assert efs["authorizationConfig"]["iam"] == "ENABLED"            # ... and allows only the task role,
    assert efs["authorizationConfig"]["accessPointId"].startswith("fsap-")   # through the access point (/dvr, 10001)
    assert efs["fileSystemId"].startswith("fs-") and "rootDirectory" not in efs   # the access point sets the root
    assert hub["mountPoints"] == [{"sourceVolume": "shared", "containerPath": "/run/sizeup-shared"},
                                  {"sourceVolume": "dvr", "containerPath": "/data/dvr", "readOnly": False}]
    assert cf["mountPoints"] == [{"sourceVolume": "shared", "containerPath": "/run/sizeup-shared"}]   # the hub's only
    env = {e["name"]: e["value"] for e in hub["environment"]}
    acct = td["taskRoleArn"].split(":")[4]
    assert env["SIZEUP_ATTACHMENTS_BUCKET"] == f"sizeup-clips-{acct}"     # 14-dvr-storage.sh's CLIPS
    assert "SIZEUP_ATTACHMENTS_BUCKET" not in {e["name"] for e in cf["environment"]}


def test_the_hub_sees_the_efs_mount_as_the_dvrs_shared_folder(monkeypatch, tmp_path):
    # The mount point is the hub's default dvr_dir (the state folder's dvr/: SIZEUP_STATE_DIR=/data in the image) and
    # dvr_shared = "auto" takes it as shared: 12 h of the response area. Fargate mounts EFS with TLS through the
    # local stunnel, so /proc/self/mounts shows an nfs4 mount from 127.0.0.1.
    from sizeup.config import load
    from sizeup.dvr import shared_mount
    assert "SIZEUP_STATE_DIR=/data" in (ROOT / "Dockerfile").read_text()
    monkeypatch.setenv("SIZEUP_STATE_DIR", "/data")
    cfg = load(ROOT / "deploy" / "hub" / "config.hub.toml")
    assert cfg.roads_enabled and cfg.roads_dvr and cfg.roads_dvr_shared == "auto" and cfg.roads_dvr_dir == ""
    root = Path(cfg.cad_db).parent / "dvr"                          # sizeup/app.py: the DVR's folder
    hub = next(c for c in _taskdef()["containerDefinitions"] if c["name"] == "hub")
    mount = next(m["containerPath"] for m in hub["mountPoints"] if m["sourceVolume"] == "dvr")
    assert str(root) == mount == "/data/dvr"
    mounts = tmp_path / "mounts"
    mounts.write_text("overlay / overlay rw,relatime 0 0\n"
                      "127.0.0.1:/ /data/dvr nfs4 rw,relatime,vers=4.1,rsize=1048576,wsize=1048576,hard,proto=tcp,"
                      "port=20049,timeo=600,retrans=2,sec=sys 0 0\n"
                      "tmpfs /run/sizeup-shared tmpfs rw 0 0\n")
    assert shared_mount(root, str(mounts)) == "nfs4"


def test_the_hub_turns_attachments_on_from_the_task_definitions_bucket(monkeypatch, tmp_path):
    from sizeup.attachments import Attachments
    from tests.test_cad import make_cad
    hub = next(c for c in _taskdef()["containerDefinitions"] if c["name"] == "hub")
    env = {e["name"]: e["value"] for e in hub["environment"]}
    monkeypatch.setenv("SIZEUP_ATTACHMENTS_BUCKET", env["SIZEUP_ATTACHMENTS_BUCKET"])
    monkeypatch.setenv("AWS_REGION", env["AWS_REGION"])
    a = Attachments.from_env(make_cad(tmp_path).store, log=lambda m: None)
    assert a.configured and a.s3.bucket == env["SIZEUP_ATTACHMENTS_BUCKET"] and a.prefix == "attachments"
    monkeypatch.delenv("SIZEUP_ATTACHMENTS_BUCKET")                  # the rollback: no bucket, nothing copied
    assert not Attachments.from_env(make_cad(tmp_path / "b").store, log=lambda m: None).configured


def test_the_task_definition_names_real_efs_ids_not_placeholders():
    # The ids sizeup-aws 14-dvr-storage.sh printed: a placeholder (REPLACE_ME) or a malformed id would fail every new
    # task and stall the deploy.
    text = (ROOT / "deploy/aws/taskdef.json").read_text()
    efs = next(v for v in json.loads(text)["volumes"] if v["name"] == "dvr")["efsVolumeConfiguration"]
    assert "REPLACE_ME" not in text, "deploy/aws/taskdef.json still has REPLACE_ME placeholders"
    assert EFS_FS.fullmatch(efs["fileSystemId"]), efs["fileSystemId"]
    assert EFS_AP.fullmatch(efs["authorizationConfig"]["accessPointId"]), efs["authorizationConfig"]["accessPointId"]


def test_the_deploy_workflow_puts_the_new_image_in_both_containers():
    # Interlock: without this the connector container would run a stale image tag. The workflow change can only
    # be pushed with a token that has the `workflow` scope (see the PR): until it is, this fails, and the deploy
    # (which runs only after the tests pass) never registers a task definition the connector can't start.
    wf = (ROOT / ".github/workflows/deploy.yml").read_text()
    assert "container-name: cloudflared" in wf
    assert "steps.td2.outputs.task-definition" in wf


def test_the_image_ships_cloudflared_pinned_and_the_shared_folder():
    df = (ROOT / "Dockerfile").read_text()
    assert "CLOUDFLARED_VERSION=2026.10.7" in df and "sha256sum -c -" in df
    assert 'VOLUME ["/run/sizeup-shared"]' in df and "chown sizeup:sizeup /run/sizeup-shared" in df
    assert os.access(ROOT / "deploy/hub/connector.sh", os.X_OK)


def test_the_updating_page_reloads_the_whole_url_hash_included():
    # #316: a bare <meta http-equiv=refresh> goes to the page's own URL; with a hash (/cad#settings/radios) the
    # browser takes that as a jump within the page and never asks the server again. location.reload() keeps it all.
    from sizeup.web.updating import UPDATING_HTML, serve_updating
    srv = serve_updating("127.0.0.1", 0)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/cad", headers={"Accept": "text/html"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        body = e.value.read()
    finally:
        srv.shutdown()
    assert e.value.code == 503 and body == UPDATING_HTML
    page = body.decode()
    assert "location.reload()" in page and "update in progress" in page
    head, _, rest = page.partition("<noscript>")
    assert "http-equiv=refresh" not in head + rest.partition("</noscript>")[2]   # only for a browser without script


# -- the CAD page: "Reconnecting… (update in progress)" ----------------------------------------------------------------

HARNESS = r"""
const fs = require("fs");
let now = 0;
const timers = [];
let nextId = 1;
global.window = global;
global.Date = class extends Date { static now() { return now; } };
global.setTimeout = (fn, ms) => { const id = nextId++; timers.push({ id, at: now + ms, fn }); return id; };
global.setInterval = (fn, ms) => { const id = nextId++; timers.push({ id, at: now + ms, fn, every: ms }); return id; };
global.clearInterval = global.clearTimeout = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); };
const body = { kids: [], append(n) { n.isConnected = true; this.kids.push(n); } };
global.document = { body, createElement(tag) {
  const n = { tag, isConnected: false, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
              remove() { this.isConnected = false; body.kids = body.kids.filter((x) => x !== this); } };
  return n; } };
let answer = { ok: false, status: 530 };
global.fetch = async () => { if (answer === "throw") throw new TypeError("Failed to fetch"); return answer; };
const streams = [];
global.EventSource = class { constructor(url) { this.url = url; this.readyState = 0; streams.push(this); } };
const flush = () => new Promise((r) => setImmediate(r));
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const t = timers[0];
    if (!t || t.at > end) break;
    now = t.at;
    if (t.every) t.at += t.every; else timers.shift();
    t.fn();
    await flush();
  }
  now = end;
  await flush();
}
eval(fs.readFileSync(process.argv[2], "utf8") + "\nglobal.LiveConn = LiveConn; global.liveState = liveState;");
const out = {};
const pill = () => body.kids.filter((n) => n.className === "pill reconnect-pill" && n.isConnected);
(async () => {
  // a blip shows nothing; out of reach for a moment: the pill; back: gone
  LiveConn.down(0);
  await advance(1000);
  out.blip = pill().length;
  await advance(800);
  out.shown = pill().map((n) => [n.textContent, n.attrs.role]);
  LiveConn.up();
  out.after_up = pill().length;

  // the page: the stream refused (502/530 during the handoff) -> polls, the pill, the stream opened again
  const states = [];
  liveState("/api/cad/stream", "/api/cad/state", (s) => states.push(s));
  await advance(3100);
  out.streams = streams.length;
  streams[0].readyState = 2;                      // the browser gave up on it (not a stream answer)
  streams[0].onerror();
  await advance(2000);
  out.pill_during = pill().length;
  out.reopened = streams.length;
  answer = { ok: true, status: 200, json: async () => ({ time: 1, units: [] }), headers: { get: () => "b.1" } };
  await advance(2100);                            // the next poll gets through
  out.pill_after = pill().length;
  out.states = states.length;
  streams[streams.length - 1].onmessage({ data: JSON.stringify({ _delta: 1, time: 2 }), lastEventId: "b.2" });
  out.last = states[states.length - 1];
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_page_says_reconnecting_retries_and_reopens_the_stream(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(HARNESS)
    r = subprocess.run([NODE, str(h), str(STATIC / "live.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["blip"] == 0                             # under 1.5 s: nothing shown
    assert out["shown"] == [["Reconnecting… (update in progress)", "status"]]
    assert out["after_up"] == 0
    assert out["streams"] == 1 and out["reopened"] == 2 and out["pill_during"] == 1
    assert out["pill_after"] == 0 and out["states"] >= 1
    assert out["last"] == {"time": 2, "units": []}


def test_the_cad_api_retries_only_what_is_safe_to_send_again():
    js = (STATIC / "cad.js").read_text()
    body = js[js.index("async function apiCall"):js.index("function syncList")]
    assert 'r.status === 503 && data && data.updating' in body         # refused before anything was done: again
    assert 'method === "GET" && LiveConn.unreachable(r.status)' in body  # a read that didn't reach the hub: again
    assert 'if (method !== "GET" || attempt >= API_RETRY_MS.length) throw e;' in body   # a write: never twice
