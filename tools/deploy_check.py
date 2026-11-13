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

    python3 tools/deploy_check.py replicas [--taskdef deploy/aws/taskdef.json] [--dbs cad,hub,audio] [--quick]

After the deploy serves (2026-11-13: a corrupt cad.sqlite replica sat unnoticed until the next takeover failed): each
replica's latest is restored from S3 (`litestream restore`, the runner's litestream: the Dockerfile's version) into a
temp dir and checked read-only with PRAGMA integrity_check (--quick: quick_check). The bucket and prefix are the task
definition's LITESTREAM_BUCKET / LITESTREAM_PATH; the region AWS_REGION; credentials the deploy role's (env), which may
read the backups (sizeup-aws step 28). Exit 1 with "<db>.sqlite: ..." for a file that is bad, has no backup or can't be
restored; nothing of the files' contents is printed (the log is public), only SQLite's check lines.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
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


# -- the replicas, restored and checked after the deploy ---------------------------------------------------------------

DBS = ("cad", "hub", "audio")


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


def litestream_config(bucket: str, prefix: str, region: str, dbs, work: Path) -> Path:
    """A litestream.yml for restoring only (deploy/hub/litestream.yml's replicas; the "path"s are never opened)."""
    lines = ["dbs:"]
    for db in dbs:
        lines += [f"  - path: {work / 'live' / (db + '.sqlite')}", "    replica:", "      type: s3",
                  f"      bucket: {bucket}", f"      path: {prefix}/{db}.sqlite"] + ([f"      region: {region}"] if region else [])
    cfg = work / "litestream.yml"
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


def litestream_restore(cfg: Path, db: str, work: Path, timeout: float = 900.0) -> Path:
    """The latest of db restored into work: its path. RuntimeError: no backup, or it couldn't."""
    out = work / "restored" / f"{db}.sqlite"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(["litestream", "restore", "-config", str(cfg), "-o", str(out), str(work / "live" / f"{db}.sqlite")],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the restore took over {timeout:.0f} s") from None
    except FileNotFoundError:
        raise RuntimeError("no litestream on this runner") from None
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "no matching backup files" in err or "no snapshots available" in err:
            raise RuntimeError("no backup in S3")
        raise RuntimeError(f"litestream restore failed: {err[-300:]}")
    if not out.exists():
        raise RuntimeError("litestream restore wrote nothing")
    return out


def integrity(path: Path, full: bool = True) -> tuple[bool, str]:
    """(ok, SQLite's answer): integrity_check (or quick_check), read-only, never the rows themselves."""
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = con.execute(f"PRAGMA {'integrity_check' if full else 'quick_check'}(20)").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return False, f"{type(e).__name__}: {e}"
    text = "; ".join(str(r[0]) for r in rows)
    return text == "ok", text


def check_replicas(dbs, restore: Callable[[str], Path], check: Callable[[Path], tuple[bool, str]],
                   out: Callable[[str], None] = print) -> list[tuple[str, bool, str]]:
    """Each replica restored and checked: [(db, ok, what to say)]. A restore that fails is a failure too."""
    res = []
    for db in dbs:
        t0 = time.monotonic()
        try:
            path = restore(db)
            ok, text = check(path)
            size = path.stat().st_size if path.exists() else 0
            msg = f"{db}.sqlite: {'ok' if ok else 'CORRUPT: ' + text[:500]} ({size / 1e6:.0f} MB, {time.monotonic() - t0:.0f} s)"
        except Exception as e:  # noqa: BLE001 - said, and a failure
            ok, msg = False, f"{db}.sqlite: can't be restored: {e}"
        out(msg)
        res.append((db, ok, msg))
    return res


def replicas_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="deploy_check.py replicas", description="restore each replica and integrity_check it")
    ap.add_argument("--taskdef", default="deploy/aws/taskdef.json")
    ap.add_argument("--dbs", default=",".join(DBS))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", ""))
    ap.add_argument("--quick", action="store_true", help="quick_check instead of integrity_check")
    a = ap.parse_args(argv)
    dbs = [x.strip() for x in a.dbs.split(",") if x.strip()]
    if not dbs or any(x not in DBS for x in dbs):
        print(f"replica check: --dbs from {', '.join(DBS)}", file=sys.stderr)
        return 2
    try:
        bucket, prefix = taskdef_backup(a.taskdef)
    except (OSError, ValueError) as e:
        print(f"replica check: {e}", file=sys.stderr)
        return 2
    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    work = Path(tempfile.mkdtemp(prefix="replica-check-"))
    try:
        cfg = litestream_config(bucket, prefix, a.region, dbs, work)
        print(f"replica check: restoring {', '.join(dbs)} from the backups ({prefix}/) and checking them")
        res = check_replicas(dbs, lambda db: litestream_restore(cfg, db, work), lambda p: integrity(p, not a.quick))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    bad = [m for _, ok, m in res if not ok]
    if not bad:
        print(f"::notice title=Replicas verified::{'; '.join(m for _, _, m in res)}" if gh else "replicas verified")
        return 0
    for m in bad:
        print(f"::error title=Replica bad::{m} · a takeover or cold start from it would fail; fix it before the next "
              "deploy (docs/cloud.md \"Replica checks\")" if gh else f"replica BAD: {m}")
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
