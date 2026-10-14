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
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
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


def main(argv: list[str] | None = None) -> int:
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
