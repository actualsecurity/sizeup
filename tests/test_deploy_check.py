"""tools/deploy_check.py: the deploy waits for the station to run the new build (#79)."""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deploy_check", ROOT / "tools" / "deploy_check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

NEW, OLD = "c" * 40, "d" * 40


def status(hub=NEW, build=OLD, state="updating", text="updating", reported=True, silent=False, last=None):
    return {"ok": True, "hub_build": hub, "alerts": [],
            "station": {"reported": reported, "connected": True, "silent": silent, "build": build, "state": state,
                        "text": text, "last": last}}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(answers, timeout=900.0, every=15.0):
    """wait_for over a list of (status, err, code) answers (the last one repeats)."""
    c, said, n = Clock(), [], [0]

    def get():
        a = answers[min(n[0], len(answers) - 1)]
        n[0] += 1
        return a
    ok, msg = dc.wait_for(get, NEW, timeout, every, clock=c, sleep=c.sleep, out=said.append)
    return ok, msg, said, c.t, n[0]


def test_success_once_the_hub_and_then_the_station_are_on_the_new_build():
    line = f"update ddddddd -> ccccccc: ok in 74 s (built 40 s, waited 0 s for a quiet moment, web back 6 s after the switch)"
    ok, msg, said, t, polls = run([
        (None, "connection refused", 0),
        (status(hub=OLD, state="ok", text="up to date"), "", 200),
        (status(), "", 200),
        (status(build=NEW, state="ok", text="up to date (ccccccc)", last={"to": NEW, "line": line}), "", 200)])
    assert ok and msg == line and polls == 4 and t == 45
    assert [s.split("] ", 1)[1] for s in said] == ["no answer from the hub yet (connection refused)",
                                                   "the hub still answers with ddddddd, waiting for ccccccc",
                                                   "station: updating", line]


def test_a_docs_only_deploy_counts_as_success():
    ok, msg, *_ = run([(status(build=OLD, state="ok", text="up to date: ccccccc (no runtime change since ddddddd)",
                               last={"to": NEW, "result": "no runtime change", "line": "update ddddddd -> ccccccc: nothing the station runs changed"}), "", 200)])
    assert ok and "nothing the station runs changed" in msg


def test_failed_or_paused_fails_at_once_and_bad_credentials_too():
    ok, msg, _s, t, _n = run([(status(state="failed", text="update to ccccccc failed (preflight)",
                                      last={"to": NEW, "line": "update ...: not switched"}), "", 200)])
    assert not ok and "failed" in msg and "not switched" in msg and t == 0
    ok, msg, *_ = run([(status(state="paused", text="auto-update is off"), "", 200)])
    assert not ok and "paused" in msg
    ok, msg, *_ = run([(None, "HTTP 401", 401)])
    assert not ok and "SIZEUP_STATUS_TOKEN" in msg


def test_times_out_after_fifteen_minutes_saying_where_it_got_to():
    ok, msg, said, t, polls = run([(status(state="updating", text="updating ddddddd -> ccccccc"), "", 200)])
    assert not ok and t == 900 and "within 15 min" in msg and "updating ddddddd -> ccccccc" in msg
    assert len(said) == 1 and polls == 61                                 # each change said once
    ok, msg, *_ = run([(status(reported=False), "", 200)], timeout=60)
    assert not ok and "no station has reported" in msg
    ok, msg, *_ = run([(status(silent=True), "", 200)], timeout=60)
    assert not ok and "silent" in msg


class FakeHub:
    def __init__(self, answer, ctype="application/json", code=200):
        self.answer, self.ctype, self.code, self.headers = answer, ctype, code, []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                me.headers.append(dict(self.headers))
                body = json.dumps(me.answer).encode() if me.ctype == "application/json" else b"<html>sign in</html>"
                self.send_response(me.code)
                self.send_header("Content-Type", me.ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_fetch_sends_the_bearer_and_the_access_service_token():
    h = FakeHub(status(build=NEW, state="ok"))
    try:
        d, err, code = dc.fetch(h.url, "tok-123", "id.access", "secret")
        assert code == 200 and d["hub_build"] == NEW
        sent = {k.lower(): v for k, v in h.headers[0].items()}
        assert sent["authorization"] == "Bearer tok-123" and sent["cf-access-client-id"] == "id.access"
        assert sent["cf-access-client-secret"] == "secret"
        h.ctype = "text/html"                                            # Access's sign-in page
        assert dc.fetch(h.url, "tok-123")[2] == 403
        h.ctype, h.code = "application/json", 401
        assert dc.fetch(h.url, "bad")[2] == 401
    finally:
        h.close()


def test_main_needs_a_token_and_says_so(monkeypatch, capsys):
    monkeypatch.delenv("SIZEUP_STATUS_TOKEN", raising=False)
    assert dc.main(["--url", "https://hub.example", "--build", NEW]) == 2
    h = FakeHub(status(build=NEW, state="ok", last={"to": NEW, "line": "update x -> y: ok in 9 s"}))
    try:
        monkeypatch.setenv("SIZEUP_STATUS_TOKEN", "tok-123")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        assert dc.main(["--url", h.url, "--build", NEW, "--every", "0"]) == 0
        assert "::notice title=Station updated::update x -> y: ok in 9 s" in capsys.readouterr().out
    finally:
        h.close()


def test_the_workflow_runs_it_after_the_deploy_and_skips_without_the_secret():
    wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    step = wf.split("- name: Wait for the station to run this build", 1)[1]
    assert wf.index("amazon-ecs-deploy-task-definition") < wf.index("Wait for the station")
    assert "secrets.SIZEUP_STATUS_TOKEN" in step and "secrets.SIZEUP_ACCESS_CLIENT_ID" in step
    assert '-z "$SIZEUP_STATUS_TOKEN"' in step and "::warning" in step and "exit 0" in step
    assert "tools/deploy_check.py" in step and "--timeout 900" in step


@pytest.mark.parametrize("a,b,same", [(NEW, NEW[:7], True), (NEW, OLD, False), ("", NEW, False), ("abc", "abc", False)])
def test_same_build(a, b, same):
    assert dc.same_build(a, b) is same
