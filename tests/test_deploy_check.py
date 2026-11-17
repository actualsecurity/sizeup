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


# -- pick: the newest tested commit on main (back-to-back merges) -------------------------------------------------------

REPO = "o/r"


class FakeGitHub:
    """The GitHub API as pick reads it: main's first-parent chain (oldest first), the commits whose `test` push run
    passed, and the deploy runs, newest first: (run id, conclusion, the sha its "deploy <sha>" job named, rolled)."""

    def __init__(self, chain, passed=(), deploys=(), side=()):
        self.chain, self.passed, self.deploys, self.paths = list(chain), set(passed), list(deploys), []
        self.side = set(side)                                              # commits off main (a branch's): diverged

    def __call__(self, path):
        from urllib.parse import parse_qs, urlsplit
        self.paths.append(path)
        u = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.split("/")
        if parts[3] == "branches":
            return {"commit": {"sha": self.chain[-1]}}
        if parts[3] == "compare":
            a, b = parts[4].split("...")
            if {a, b} & self.side:
                return {"status": "diverged", "total_commits": 1, "commits": [{"sha": b}]}
            if a not in self.chain or b not in self.chain:
                raise dc.GitHubError(f"HTTP 404 for {path}")               # github_get: an unknown commit
            ia, ib = self.chain.index(a), self.chain.index(b)
            status = "identical" if ia == ib else "ahead" if ib > ia else "behind"
            between = self.chain[ia + 1:ib + 1] if ib > ia else []
            per, page = int(q.get("per_page", 250)), int(q.get("page", 1))
            return {"status": status, "total_commits": len(between),
                    "commits": [{"sha": s} for s in between[(page - 1) * per:page * per]]}
        if parts[4:6] == ["workflows", "test.yml"]:
            assert q == {"branch": "main", "event": "push", "status": "success", "per_page": "100"}
            return {"workflow_runs": [{"head_sha": s} for s in sorted(self.passed)]}
        if parts[4:6] == ["workflows", "deploy.yml"]:
            return {"workflow_runs": [{"id": i, "conclusion": c} for i, c, *_ in self.deploys]}
        if parts[4] == "runs" and parts[6] == "jobs":
            for i, c, sha, rolled in self.deploys:
                if str(i) == parts[5]:
                    steps = [{"name": "Nothing newer is deployed", "conclusion": "success"},
                             {"name": dc.ROLL_STEP, "conclusion": "success" if rolled else "failure"}]
                    return {"jobs": [{"name": "pick", "conclusion": "success"},
                                     {"name": f"deploy {sha}" if sha else "deploy", "steps": steps}]}
            return {"jobs": []}
        raise AssertionError(path)


C = [f"{i:x}" * 40 for i in range(1, 8)]            # main: C[0] (oldest) .. C[6] (the tip)


def test_pick_deploys_the_newest_tested_commit_on_main_not_the_one_that_started_the_run():
    gh = FakeGitHub(C, passed={C[1], C[3], C[4]})                      # C[5], C[6]: still testing (or failed)
    sha, why = dc.pick(gh, REPO, C[1])
    assert sha == C[4] and "3 commit(s) after" in why
    assert dc.pick(gh, REPO, C[4])[0] == C[4]                          # the newest one itself
    gh.passed = {C[3]}
    assert dc.pick(gh, REPO, C[4])[0] == C[4]   # the run's own commit passed even if the listing hasn't caught up
    assert dc.pick(gh, REPO, C[6])[0] == C[6]


def test_pick_never_deploys_a_commit_older_than_or_the_same_as_the_last_deploy():
    gh = FakeGitHub(C, passed={C[1], C[2]}, deploys=[(9, "success", C[4], True)])
    sha, why = dc.pick(gh, REPO, C[1])           # a late test run for an older commit
    assert sha == "" and "is already deployed" in why and C[4][:7] in why
    gh.deploys = [(9, "success", C[2], True)]
    sha, why = dc.pick(gh, REPO, C[1])
    assert sha == "" and why.startswith(f"{C[2][:7]} is already deployed (deploy run 9)")
    gh.passed.add(C[5])
    assert dc.pick(gh, REPO, C[1])[0] == C[5]                          # something newer passed: that one
    assert dc.pick(gh, REPO, C[5], run_id="9")[0] == C[5]              # (this run itself never counts)


def test_the_last_deploy_is_what_ecs_runs_whatever_the_checks_after_said():
    gh = FakeGitHub(C, passed={C[3]}, deploys=[
        (12, "skipped", None, False),                                  # a failed test run's deploy: no job ran
        (11, "success", C[6], False),                                  # (impossible, but: never rolled, never counts)
        (10, "failure", C[3], True),                                   # rolled, then the station check failed
        (9, "success", C[2], True)])
    assert dc.last_deployed(gh, REPO) == (C[3], "10")
    assert not any("/runs/12/jobs" in p for p in gh.paths)
    assert dc.pick(gh, REPO, C[3])[0] == ""                            # not again
    gh.deploys = [(8, "success", None, True)]                         # before pick existed: "deploy", no sha
    assert dc.last_deployed(gh, REPO) == ("", "") and dc.pick(gh, REPO, C[3])[0] == C[3]


def test_pick_deploys_nothing_that_is_not_on_main_and_fails_closed_on_an_unknown_commit():
    gh = FakeGitHub(C, passed=set(C), side={"e" * 40})
    sha, why = dc.pick(gh, REPO, "e" * 40)                             # a commit off main (force-pushed away)
    assert sha == "" and "is not on main" in why
    with pytest.raises(dc.GitHubError):                                # unknown to GitHub: red, never "nothing"
        dc.pick(gh, REPO, "f" * 40)
    with pytest.raises(dc.GitHubError, match="no compare status"):     # an answer without a status: the same
        dc.compare(lambda path: {}, REPO, C[0], C[1])
    with pytest.raises(dc.GitHubError, match="no compare status"):
        dc.newer_on(lambda path: {"commits": []}, REPO, C[0], C[1])


def test_pick_reads_every_page_of_a_long_compare():
    chain = [f"{i:040x}" for i in range(1, 260)]
    gh = FakeGitHub(chain, passed={chain[0], chain[230]})
    assert dc.pick(gh, REPO, chain[0])[0] == chain[230]
    assert sum("page=" in p and "/compare/" in p for p in gh.paths) == 3


def test_the_guard_deploys_only_what_ecs_runs_or_a_descendant_of_it():
    # whatever the run history says (it says C[1] here): ECS runs C[4]
    gh = FakeGitHub(C, deploys=[(9, "success", C[1], True)], side={"e" * 40})
    ok, why = dc.guard(gh, REPO, C[2], C[4])
    assert not ok and f"ECS runs {C[4][:7]}; {C[2][:7]} is older than it" in why
    assert dc.guard(gh, REPO, C[4], C[4])[0]                           # the same commit again (a re-run): fine
    assert dc.guard(gh, REPO, C[5], C[4])[0]
    assert dc.guard(gh, REPO, C[2], None)[0]                           # no service yet: nothing to roll back
    ok, why = dc.guard(gh, REPO, C[5], "e" * 40)                       # ECS runs a branch's commit (by hand)
    assert not ok and "not a descendant of" in why
    with pytest.raises(dc.GitHubError):
        dc.guard(gh, REPO, C[5], "f" * 40)                             # unknown: fails closed (pick_main: exit 1)
    assert not any("actions" in p for p in gh.paths)                   # never the run history


def ecs(services, image=None, primary="arn:aws:ecs:r:1:task-definition/sizeup-hub:42", failures=()):
    """A fake `aws ecs ...` (the guard's two reads) and the calls it was asked."""
    asked = []

    def aws(args):
        asked.append(args)
        if args[1] == "describe-services":
            return {"failures": list(failures), "services": services if services is not None else [
                {"status": "ACTIVE", "deployments": [{"status": "ACTIVE", "taskDefinition": "old:41"},
                                                     {"status": "PRIMARY", "taskDefinition": primary}]}]}
        assert args[1] == "describe-task-definition" and args[3] == primary
        return {"taskDefinition": {"containerDefinitions": [{"name": "cloudflared", "image": "x/y:" + "0" * 40},
                                                            {"name": "hub", "image": image}]}}
    return aws, asked


def test_ecs_running_reads_the_primary_deployments_hub_image_tag():
    aws, asked = ecs(None, f"123.dkr.ecr.us-east-1.amazonaws.com/sizeup-hub:{C[3]}")
    assert dc.ecs_running("sizeup-hub", "sizeup-hub", aws=aws) == C[3]
    assert [a[1] for a in asked] == ["describe-services", "describe-task-definition"]
    missing = [{"arn": "arn:aws:ecs:r:1:service/c/s", "reason": "MISSING"}]
    assert dc.ecs_running("c", "s", aws=ecs([], failures=missing)[0]) is None                 # no service yet
    for gone in ("DRAINING", "INACTIVE"):
        assert dc.ecs_running("c", "s", aws=ecs([{"status": gone}])[0]) is None
    for odd in (ecs([])[0], ecs([], failures=[{"reason": "ACCESS_DENIED"}])[0], lambda args: {},
                ecs([{"status": "PENDING"}])[0]):
        with pytest.raises(dc.EcsError):                               # never "nothing runs" by default
            dc.ecs_running("c", "s", aws=odd)
    for image in ("123.dkr.ecr.us-east-1.amazonaws.com/sizeup-hub:latest", None, "host:5000/repo"):
        with pytest.raises(dc.EcsError, match="not tagged with a full sha"):              # fails closed
            dc.ecs_running("c", "s", aws=ecs(None, image)[0])
    with pytest.raises(dc.EcsError, match="no PRIMARY"):
        dc.ecs_running("c", "s", aws=ecs([{"status": "ACTIVE", "deployments": []}])[0])


def test_the_guard_fails_closed_when_aws_cannot_be_read(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    gh = FakeGitHub(C)
    monkeypatch.setattr(dc, "github_get", lambda token: gh)

    def refused(cluster, service):
        raise dc.EcsError("aws ecs describe-services: AccessDeniedException")
    monkeypatch.setattr(dc, "ecs_running", refused)
    assert dc.main(["pick", "--guard", "--repo", REPO, "--sha", C[5]]) == 1
    assert "::error title=Not deployed::could not check what ECS runs" in capsys.readouterr().out
    monkeypatch.setattr(dc, "ecs_running", lambda cluster, service: C[4])
    assert dc.main(["pick", "--guard", "--repo", REPO, "--sha", C[5]]) == 0
    assert dc.main(["pick", "--guard", "--repo", REPO, "--sha", C[3]]) == 1
    assert "is older than it" in capsys.readouterr().out


def test_pick_main_writes_the_output_and_says_why(monkeypatch, tmp_path, capsys):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    gh = FakeGitHub(C, passed={C[1], C[3]})
    monkeypatch.setattr(dc, "github_get", lambda token: gh)
    assert dc.main(["pick", "--repo", REPO, "--sha", C[1], "--run-id", "5"]) == 0
    assert out.read_text() == f"sha={C[3]}\n" and "::notice title=Deploying::" in capsys.readouterr().out
    gh.deploys = [(4, "success", C[3], True)]
    assert dc.main(["pick", "--repo", REPO, "--sha", C[1], "--run-id", "5"]) == 0
    assert out.read_text().endswith("sha=\n") and "::notice title=Not deploying::" in capsys.readouterr().out

    def down(path):
        raise dc.GitHubError(f"HTTP 502 for {path}")
    monkeypatch.setattr(dc, "github_get", lambda token: down)
    out.write_text("")
    assert dc.main(["pick", "--repo", REPO, "--sha", C[1]]) == 1
    assert out.read_text() == "" and "nothing was deployed" in capsys.readouterr().out   # no output: deploy skipped
    assert dc.main(["pick", "--repo", REPO, "--sha", "abc1234"]) == 2


def test_the_deploy_workflow_picks_first_and_names_the_job_after_the_commit():
    wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert 'python3 tools/deploy_check.py pick --repo "$GITHUB_REPOSITORY" --sha "$TESTED"' in wf
    assert "name: deploy ${{ needs.pick.outputs.sha }}" in wf and "if: needs.pick.outputs.sha != ''" in wf
    assert f"name: {dc.ROLL_STEP}" in wf
    guard = wf.index("deploy_check.py pick --guard")
    assert wf.index("needs: pick") < wf.index("aws-actions/configure-aws-credentials") < guard   # it reads ECS
    for later in ("taskdef_params.py", "amazon-ecr-login", "docker/build-push-action", "amazon-ecs-deploy-task-definition"):
        assert guard < wf.index(later), later
    assert "github.event.workflow_run.event == 'push'" in wf and "cancel-in-progress: false" in wf
    assert "&& 'deploy' || format('deploy-skipped-{0}', github.run_id)" in wf   # skipped runs: their own group
    # paused: pick still runs (to warn), outside the deploy queue
    assert wf.count("vars.DEPLOY_PAUSED != 'true' && github.event.workflow_run.conclusion == 'success'") == 1
    group = wf[wf.index("concurrency:"):wf.index("jobs:")]
    assert "github.event_name == 'workflow_dispatch' || (vars.DEPLOY_PAUSED != 'true'" in group
    assert "DEPLOY_PAUSED" not in wf[wf.index("  pick:"):wf.index("    runs-on:", wf.index("  pick:"))]


def pick_step(tmp_path, **env):
    """Runs the pick step's own shell script from deploy.yml (python3 there: a stub that records it was asked)."""
    import os
    import subprocess

    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    step = next(s for s in wf["jobs"]["pick"]["steps"] if s.get("id") == "pick")
    assert set(step["env"]) == {"GITHUB_TOKEN", "TESTED", "DEPLOY_PAUSED"}
    assert step["env"]["DEPLOY_PAUSED"] == "${{ vars.DEPLOY_PAUSED }}"
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "python3").write_text("#!/bin/sh\necho \"python3 $*\" >> \"$CALLS\"\necho sha=PICKED >> \"$GITHUB_OUTPUT\"\n")
    (stub / "python3").chmod(0o755)
    out, calls = tmp_path / "out", tmp_path / "calls"
    out.write_text(""), calls.write_text("")
    r = subprocess.run(["bash", "-e", "-c", step["run"]], capture_output=True, text=True, timeout=30, env={
        "PATH": f"{stub}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(out), "CALLS": str(calls),
        "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "5", "GITHUB_SHA": C[6], "GITHUB_REF_NAME": "main",
        "TESTED": C[3], **env})
    assert r.returncode == 0, r.stderr
    return r.stdout, out.read_text(), calls.read_text()


def test_paused_pick_warns_and_deploys_nothing_and_by_hand_still_deploys(tmp_path):
    said, out, calls = pick_step(tmp_path, GITHUB_EVENT_NAME="workflow_run", DEPLOY_PAUSED="true")
    assert said.startswith("::warning title=Deploys paused::DEPLOY_PAUSED is true: " + C[3][:7]) and "nothing was deployed" in said
    assert out == "sha=\n" and calls == ""                            # no sha: the deploy job is skipped
    said, out, calls = pick_step(tmp_path, GITHUB_EVENT_NAME="workflow_dispatch", DEPLOY_PAUSED="true")
    assert out == f"sha={C[6]}\n" and "by hand" in said and calls == ""
    for unpaused in ("", "false"):
        said, out, calls = pick_step(tmp_path, GITHUB_EVENT_NAME="workflow_run", DEPLOY_PAUSED=unpaused)
        assert "paused" not in said and out == "sha=PICKED\n"
        assert calls == f'python3 tools/deploy_check.py pick --repo {REPO} --sha {C[3]} --run-id 5\n'


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=b"", headers=None):
    import email.message
    import io
    import urllib.error
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("https://api.github.com/x", code, "x", h, io.BytesIO(body))


def test_github_get_retries_rate_limits_and_server_errors_and_fails_on_the_rest(monkeypatch):
    answers, slept = [], []

    def urlopen(req, timeout=None):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return FakeResponse(a)
    monkeypatch.setattr(dc.urllib.request, "urlopen", urlopen)
    get = dc.github_get("tok", sleep=slept.append)
    answers[:] = [http_error(403, b'{"message":"You have exceeded a secondary rate limit"}'),
                  http_error(403, b"{}", {"Retry-After": "7"}), http_error(502), b'{"ok": 1}']
    assert get("repos/o/r") == {"ok": 1} and slept == [10.0, 7.0, 6.0]
    for refusal in (http_error(404), http_error(403, b'{"message":"Resource not accessible by integration"}'),
                    http_error(401)):
        answers[:], slept[:] = [refusal], []
        with pytest.raises(dc.GitHubError, match=f"HTTP {refusal.code}"):                  # final at once
            get("repos/o/r/compare/a...b")
        assert slept == []
    answers[:], slept[:] = [http_error(429)] * 5, []
    with pytest.raises(dc.GitHubError, match="rate limited"):
        get("repos/o/r")
    assert len(slept) == 4


def test_main_test_runs_are_never_cancelled_only_a_prs():
    ci = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in ci
    assert "format('test-{0}-{1}', github.ref, github.run_id)" in ci                # main: a group per run
