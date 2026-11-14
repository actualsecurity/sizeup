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
file is failing (corrupt, or not verifiable), or nothing was verified in time. It prints SQLite's check lines and the
times, never anything of the files' contents.
"""

from __future__ import annotations

import argparse
import json
import os
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
    """(OK | WAIT | FAIL, what to say) for the record as read now."""
    if doc is None:
        return WAIT, f"no {KEY} yet"
    checks = doc.get("checks") if isinstance(doc.get("checks"), dict) else {}
    failing = doc.get("failing") if isinstance(doc.get("failing"), dict) else {}
    if failing:
        return FAIL, "; ".join(_what(db, checks.get(db) or {}) for db in sorted(failing))
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
          "Settings > Health > Replicas and docs/cloud.md \"Replica checks\"" if gh else f"replica check FAILED: {msg}")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["replicas"]:
        return replicas_main(argv[1:])
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
