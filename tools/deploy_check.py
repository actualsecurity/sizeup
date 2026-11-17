#!/usr/bin/env python3
"""A deploy that checks itself (GitHub #79): after the hub is deployed, wait for the station to run the new
build, and fail the workflow when it doesn't (.github/workflows/deploy.yml, docs/cloud.md "Station health and
alerts"). Stdlib only: CI runs it with the runner's python3, no install.

    python3 tools/deploy_check.py --url https://sizeup.example.org --build <sha> [--timeout 900] [--every 15]

Reads GET <url>/api/station/status (read only, sizeup/cloud/health.py) with
    SIZEUP_STATUS_TOKEN            the hub's status token (Authorization: Bearer; it can read this and nothing else)
    SIZEUP_ACCESS_CLIENT_ID        a Cloudflare Access service token allowed through to /api/station/
    SIZEUP_ACCESS_CLIENT_SECRET    (CF-Access-Client-Id / CF-Access-Client-Secret)

Success: the hub answers with this build and the station reports it (or an equivalent one: a docs-only change
the station recorded as "no runtime change"); the station's update log line is printed. Failure (exit 1): the
station's update failed or was rolled back, auto-update is paused there, the token is refused, or the time ran
out. Exit 2: bad arguments or no token (the workflow skips the step with a warning before that).

    python3 tools/deploy_check.py replicas --build <sha> [--timeout 600] [--every 20] [--taskdef deploy/aws/taskdef.json]

After the deploy serves (2026-11-13: a corrupt cad.sqlite replica sat unnoticed until the next takeover failed): the hub
verifies its own replicas (sizeup/replicawatch.py: the new leader's followers, checked 2 min after it serves) and keeps
the results in ONE object, s3://<LITESTREAM_BUCKET>/<LITESTREAM_PATH>/replica-watch.json. This reads only that object
(`aws s3api get-object`: the deploy role may GetObject that one key and nothing else, sizeup-aws step 28; no database
ever leaves AWS) until the leader of this build has said it serves and every file has a check after that. Exit 1 when a
file's check since then is failing (corrupt, or not verifiable), or nothing was verified in time; an older leader's
results in the record are waited past, never judged. It prints SQLite's check lines and the
times, never anything of the files' contents.

    python3 tools/deploy_check.py pick --repo <owner/name> --sha <the tested commit> [--run-id <this run>] [--guard]

Before anything is built: WHICH commit to deploy, so PRs can be merged back to back (docs/cloud.md "Back-to-back
merges"). The deploy run was started by one `test` run on main, but a newer commit on main may have passed its tests
too (main's test runs are never cancelled, so they finish in any order). It picks the newest commit on main, from this
one up to main's tip, that has a successful `test` push run; and nothing when that commit, or a newer one, is what the
last deploy put on ECS (the newest deploy run whose "deploy <sha>" job got past "Roll the ECS service"). So an older
commit is never deployed after a newer one, and no commit twice in a row. --guard (the deploy job's first step, for
a re-run of an old run): exit 1 when a commit newer than --sha is already on ECS. GitHub API reads only (GITHUB_TOKEN
or GH_TOKEN: contents and actions read). Writes `sha=<sha or empty>` to $GITHUB_OUTPUT when set. Exit 1 when GitHub
can't be asked: nothing is deployed (the next deploy catches up).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

OK, WAIT, FAIL = "ok", "wait", "fail"


def same_build(a, b) -> bool:
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def verdict(status: dict | None, build: str, error: str = "", code: int = 0) -> tuple[str, str]:
    """(OK | WAIT | FAIL, what to say) for one answer from the hub (status None: none, error / code why)."""
    if status is None:
        if code in (401, 403):
            return FAIL, f"the hub refused the status request (HTTP {code}): check SIZEUP_STATUS_TOKEN and the Access service token"
        return WAIT, f"no answer from the hub yet ({error or f'HTTP {code}'})"
    hub = status.get("hub_build") or ""
    if not same_build(hub, build):
        return WAIT, f"the hub still answers with {hub[:7] or 'no build'}, waiting for {build[:7]}"
    st = status.get("station") or {}
    if not st or not st.get("reported"):
        return WAIT, "no station has reported to the new hub yet"
    if st.get("silent"):
        return WAIT, "the station is silent (no state post for a few minutes)"
    state, text = st.get("state") or "unknown", st.get("text") or ""
    last = st.get("last") or {}
    line = last.get("line") if same_build(last.get("to"), build) else ""
    if state == "ok":                       # on this build, or one the station recorded as equivalent (docs only)
        return OK, line or text
    if state == "failed":
        return FAIL, f"the station's update failed: {text}" + (f"\n  {line}" if line else "")
    if state == "paused":
        return FAIL, f"auto-update is paused on the station: {text}"
    return WAIT, f"station: {text or state}"


def fetch(url: str, token: str, access_id: str = "", access_secret: str = "",
          timeout: float = 20.0) -> tuple[dict | None, str, int]:
    """(the status, error, HTTP code)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "sizeup-deploy-check"}
    if access_id and access_secret:
        headers.update({"CF-Access-Client-Id": access_id, "CF-Access-Client-Secret": access_secret})
    req = urllib.request.Request(url.rstrip("/") + "/api/station/status", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(1 << 20)
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}", e.code
    except (urllib.error.URLError, OSError) as e:
        return None, str(getattr(e, "reason", e)), 0
    if "json" not in ctype:                 # Cloudflare Access's sign-in page: the service token didn't pass
        return None, "not JSON (Cloudflare Access sign-in page? check the service token)", 403
    try:
        d = json.loads(body)
    except ValueError:
        return None, "not JSON", 0
    return (d, "", 200) if isinstance(d, dict) else (None, "not a JSON object", 0)


def wait_for(get: Callable[[], tuple[dict | None, str, int]], build: str, timeout: float = 900.0, every: float = 15.0,
             clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
             out: Callable[[str], None] = print) -> tuple[bool, str]:
    """Poll until OK (True), FAIL or timeout (False): (ok, the last thing said). Says each change once."""
    t0, said, msg = clock(), "", ""
    while True:
        status, err, code = get()
        v, msg = verdict(status, build, err, code)
        if msg != said:
            out(f"[{int(clock() - t0):4d} s] {msg}")
            said = msg
        if v == OK:
            return True, msg
        if v == FAIL:
            return False, msg
        if clock() - t0 >= timeout:
            return False, f"the station didn't report {build[:7]} within {timeout / 60:g} min (last: {msg})"
        sleep(min(every, max(0.0, timeout - (clock() - t0))))


# -- the replicas, as the new leader verified them -----------------------------------------------------------------------

DBS = ("cad", "hub", "audio")
KEY = "replica-watch.json"


def taskdef_backup(path: str) -> tuple[str, str]:
    """(bucket, prefix) from the task definition's hub container (LITESTREAM_BUCKET / LITESTREAM_PATH)."""
    d = json.loads(Path(path).read_text())
    env = {}
    for c in d.get("containerDefinitions") or []:
        if c.get("name") == "hub":
            env = {e.get("name"): e.get("value") for e in c.get("environment") or []}
    bucket = str(env.get("LITESTREAM_BUCKET") or "").strip()
    if not bucket:
        raise ValueError(f"{path}: no LITESTREAM_BUCKET in the hub container")
    return bucket, (str(env.get("LITESTREAM_PATH") or "").strip() or "sizeup-hub").strip("/")


def fetch_watch(bucket: str, key: str, timeout: float = 30.0) -> dict | None:
    """The record (None: not there yet). RuntimeError: AWS refused or couldn't be reached."""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "w.json"
        try:
            r = subprocess.run(["aws", "s3api", "get-object", "--bucket", bucket, "--key", key, str(out)],
                               capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise RuntimeError(f"aws s3api get-object: {type(e).__name__}") from None
        if r.returncode != 0:
            if "NoSuchKey" in r.stderr:
                return None
            raise RuntimeError(f"aws s3api get-object: {r.stderr.strip()[-200:]}")
        try:
            doc = json.loads(out.read_text())
        except ValueError:
            raise RuntimeError(f"{key}: not JSON") from None
    return doc if isinstance(doc, dict) else None


def _hm(t) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(float(t))) + " UTC" if isinstance(t, (int, float)) else "?"


def _what(db: str, c: dict) -> str:
    """One file's last check, as the log says it: corrupt (the check said so) or not verifiable (it couldn't check)."""
    state = "corrupt" if c.get("ok") is False else "not verifiable" if c.get("ok") is None else "ok"
    detail = " · ".join(str(c.get("result") or "").splitlines()[:3])[:400]
    return (f"{db}.sqlite: {state} ({c.get('mode') or 'check'} by the {c.get('by') or '?'} at {_hm(c.get('at'))})"
            + (f": {detail}" if state != "ok" and detail else ""))


def replica_verdict(doc: dict | None, build: str) -> tuple[str, str]:
    """(OK | WAIT | FAIL, what to say) for the record as read now. Only checks made since this build's leader serves
    count: the record still holds the old leader's last results until the new one checks (2026-11-15: an old leader's
    "corrupt", from before the deploy, failed it in 0 s while the new leader's first check, minutes later, passed)."""
    if doc is None:
        return WAIT, f"no {KEY} yet"
    checks = doc.get("checks") if isinstance(doc.get("checks"), dict) else {}
    failing = doc.get("failing") if isinstance(doc.get("failing"), dict) else {}
    lead = doc.get("leader") if isinstance(doc.get("leader"), dict) else {}
    if not same_build(lead.get("build"), build):
        return WAIT, f"the leader of {build[:7]} hasn't said it serves yet (now: {str(lead.get('build') or '?')[:7]})"
    since = float(lead.get("serving_at") or 0)
    waiting, bad, good = [], [], []
    for db in DBS:
        c = checks.get(db) if isinstance(checks.get(db), dict) else None
        if c is None or float(c.get("at") or 0) < since:
            waiting.append(db)
        elif c.get("ok") is True:
            good.append(_what(db, c))
        elif c.get("ok") is False:
            bad.append(_what(db, c))
        elif db in failing:                           # not verifiable twice running (or a failed restore), since
            bad.append(_what(db, c))
        else:
            waiting.append(db)                        # not verifiable once: the hub says so after two (failing)
    if bad:
        return FAIL, "; ".join(bad)
    if waiting:
        return WAIT, f"the leader (serving since {_hm(since)}) hasn't verified {', '.join(waiting)} yet"
    return OK, "; ".join(good)


def wait_replicas(get: Callable[[], dict | None], build: str, timeout: float = 600.0, every: float = 20.0,
                  clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                  out: Callable[[str], None] = print) -> tuple[bool, str]:
    t0, said, msg = clock(), "", ""
    while True:
        try:
            v, msg = replica_verdict(get(), build)
        except RuntimeError as e:
            v, msg = WAIT, f"can't read {KEY}: {e}"
        if msg != said:
            out(f"[{int(clock() - t0):4d} s] {msg}")
            said = msg
        if v != WAIT:
            return v == OK, msg
        if clock() - t0 >= timeout:
            return False, f"not verified within {timeout / 60:g} min of the deploy (last: {msg})"
        sleep(min(every, max(0.0, timeout - (clock() - t0))))


def replicas_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="deploy_check.py replicas", description="wait for the new leader to verify the replicas")
    ap.add_argument("--build", required=True)
    ap.add_argument("--taskdef", default="deploy/aws/taskdef.json")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--every", type=float, default=20.0)
    a = ap.parse_args(argv)
    try:
        bucket, prefix = taskdef_backup(a.taskdef)
    except (OSError, ValueError) as e:
        print(f"replica check: {e}", file=sys.stderr)
        return 2
    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    print(f"replica check: waiting up to {a.timeout / 60:g} min for the leader of {a.build[:7]} to verify the replicas "
          f"({prefix}/{KEY}; the first check is 2 min after it serves)")
    ok, msg = wait_replicas(lambda: fetch_watch(bucket, f"{prefix}/{KEY}"), a.build, a.timeout, a.every)
    if ok:
        print(f"::notice title=Replicas verified::{msg}" if gh else f"replicas verified: {msg}")
        return 0
    print(f"::error title=Replica check failed::{msg} · a takeover or cold start from a bad replica would fail; see "
          "Settings > Station > Health > Replicas and docs/cloud.md \"Replica checks\"" if gh else f"replica check FAILED: {msg}")
    return 1


# -- which commit to deploy: the newest tested one on main --------------------------------------------------------------

SHA40 = re.compile(r"[0-9a-f]{40}")
DEPLOY_JOB = re.compile(r"deploy ([0-9a-f]{40})")    # deploy.yml's deploy job: name: deploy ${{ needs.pick.outputs.sha }}
ROLL_STEP = "Roll the ECS service"                   # its step that updates the ECS service


class GitHubError(Exception):
    pass


def github_get(token: str, timeout: float = 20.0, tries: int = 3) -> Callable[[str], dict]:
    """GET api.github.com/<path> → JSON; 404 → {} (an unknown commit); else, after `tries`, GitHubError."""
    def get(path: str) -> dict:
        req = urllib.request.Request(f"https://api.github.com/{path}", headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sizeup-deploy-check", **({"Authorization": f"Bearer {token}"} if token else {})})
        last = ""
        for i in range(tries):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read())
                    return d if isinstance(d, dict) else {}
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {}
                last = f"HTTP {e.code} for {path}"
                if e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = f"{getattr(e, 'reason', e)} for {path}"
            time.sleep(2 * (i + 1))
        raise GitHubError(last)
    return get


def compare(get: Callable[[str], dict], repo: str, base: str, head: str) -> str:
    """GitHub's compare status of head against base: "identical", "ahead" (head is a descendant of base), "behind",
    "diverged", or "" (unknown commit)."""
    return get(f"repos/{repo}/compare/{base}...{head}?per_page=1").get("status") or ""


def newer_on(get: Callable[[str], dict], repo: str, sha: str, tip: str) -> list[str] | None:
    """The commits after `sha` up to `tip`, oldest first; None when `sha` isn't `tip` or an ancestor of it."""
    out: list[str] = []
    for page in range(1, 50):
        c = get(f"repos/{repo}/compare/{sha}...{tip}?per_page=100&page={page}")
        if c.get("status") not in ("ahead", "identical"):
            return None
        got = [x["sha"] for x in c.get("commits") or []]
        out += got
        if not got or len(out) >= int(c.get("total_commits") or 0):
            break
    return out


def last_deployed(get: Callable[[str], dict], repo: str, workflow: str = "deploy.yml", skip_run: str = "",
                  runs: int = 30) -> tuple[str, str]:
    """(sha, run id) of the newest deploy run whose "deploy <sha>" job rolled the ECS service (whatever its later checks
    said: that is what ECS runs), or ("", ""). Deploy runs share one concurrency group, so they run in the order they
    were created, and a re-run of an old one is refused by --guard when something newer is deployed."""
    listing = get(f"repos/{repo}/actions/workflows/{workflow}/runs?status=completed&per_page={runs}")
    for run in listing.get("workflow_runs") or []:
        if str(run.get("id")) == str(skip_run) or run.get("conclusion") == "skipped":   # (skipped: no job ran)
            continue
        for job in get(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs") or []:
            m = DEPLOY_JOB.fullmatch(job.get("name") or "")
            if m and any(s.get("name") == ROLL_STEP and s.get("conclusion") == "success" for s in job.get("steps") or []):
                return m.group(1), str(run["id"])
    return "", ""


def pick(get: Callable[[str], dict], repo: str, sha: str, branch: str = "main", tests: str = "test.yml",
         deploys: str = "deploy.yml", run_id: str = "") -> tuple[str, str]:
    """(the commit to deploy, or "" for none; why). `sha`: the commit whose `test` run just passed on `branch`."""
    tip = (get(f"repos/{repo}/branches/{branch}").get("commit") or {}).get("sha") or ""
    if not SHA40.fullmatch(tip):
        raise GitHubError(f"no tip for {branch}")
    after = newer_on(get, repo, sha, tip)
    if after is None:
        return "", f"{sha[:7]} is not on {branch} (tip {tip[:7]}): nothing to deploy"
    passed = {r.get("head_sha") for r in get(f"repos/{repo}/actions/workflows/{tests}/runs?branch={branch}&event=push"
                                             f"&status=success&per_page=100").get("workflow_runs") or []}
    target = next(s for s in reversed([sha, *after]) if s == sha or s in passed)
    why = f"{target[:7]}: the newest commit on {branch} that passed `test`" + (
        f" ({after.index(target) + 1} commit(s) after {sha[:7]}, which started this run)" if target != sha else "")
    done, run = last_deployed(get, repo, deploys, run_id)
    if done == target:
        return "", f"{target[:7]} is already deployed (deploy run {run}): nothing to do"
    if done and compare(get, repo, target, done) == "ahead":
        return "", f"{done[:7]}, newer than {target[:7]}, is already deployed (deploy run {run}): nothing to do"
    return target, why


def guard(get: Callable[[str], dict], repo: str, sha: str, deploys: str = "deploy.yml", run_id: str = "") -> tuple[bool, str]:
    """(False, why) when a commit newer than `sha` is already deployed: a re-run of an old deploy run never rolls back."""
    done, run = last_deployed(get, repo, deploys, run_id)
    if done and done != sha and compare(get, repo, sha, done) == "ahead":
        return False, (f"{done[:7]}, newer than {sha[:7]}, is already deployed (deploy run {run}): this run won't roll "
                       "back (re-run the whole workflow to pick the newest commit, or run deploy by hand on a ref)")
    return True, f"nothing newer than {sha[:7]} is deployed" + (f" (last: {done[:7]}, run {run})" if done else "")


def pick_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="deploy_check.py pick", description="the newest tested commit on main to deploy")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--sha", required=True, help="the commit whose test run started this deploy (with --guard: the one about to be deployed)")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--run-id", default="", help="this deploy run (never counted as the last deploy)")
    ap.add_argument("--guard", action="store_true", help="only check that nothing newer than --sha is deployed")
    a = ap.parse_args(argv)
    if not SHA40.fullmatch(a.sha):
        print("pick: --sha must be a full 40-digit commit", file=sys.stderr)
        return 2
    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    get = github_get((os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip())
    try:
        if a.guard:
            ok, why = guard(get, a.repo, a.sha, run_id=a.run_id)
            print(why if ok else (f"::error title=A newer commit is deployed::{why}" if gh else f"refused: {why}"))
            return 0 if ok else 1
        sha, why = pick(get, a.repo, a.sha, a.branch, run_id=a.run_id)
    except (GitHubError, KeyError, TypeError, ValueError) as e:
        print(f"::error title=Could not ask GitHub::{e}; nothing was deployed (the next deploy catches up)" if gh
              else f"pick: could not ask GitHub: {e}")
        return 1
    print(f"::notice title={'Deploying' if sha else 'Not deploying'}::{why}" if gh else f"{'deploy' if sha else 'skip'}: {why}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"sha={sha}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["replicas"]:
        return replicas_main(argv[1:])
    if argv[:1] == ["pick"]:
        return pick_main(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("SIZEUP_HUB_URL", ""))
    ap.add_argument("--build", required=True, help="the git sha just deployed")
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds (default 900: 15 min)")
    ap.add_argument("--every", type=float, default=15.0, help="seconds between polls")
    a = ap.parse_args(argv)
    token = os.environ.get("SIZEUP_STATUS_TOKEN", "").strip()
    if not a.url or not token or not same_build(a.build, a.build):
        print("deploy check: --url (or SIZEUP_HUB_URL), --build <sha> and SIZEUP_STATUS_TOKEN are needed", file=sys.stderr)
        return 2
    aid, asec = os.environ.get("SIZEUP_ACCESS_CLIENT_ID", "").strip(), os.environ.get("SIZEUP_ACCESS_CLIENT_SECRET", "").strip()
    print(f"deploy check: waiting up to {a.timeout / 60:g} min for the station to run {a.build[:7]} ({a.url})")
    ok, msg = wait_for(lambda: fetch(a.url, token, aid, asec), a.build, a.timeout, a.every)
    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    if ok:
        print(f"::notice title=Station updated::{msg}" if gh else f"station updated: {msg}")
        return 0
    print(f"::error title=Station not updated::{msg}" if gh else f"station NOT updated: {msg}", file=sys.stderr if not gh else sys.stdout)
    return 1


if __name__ == "__main__":
    sys.exit(main())
