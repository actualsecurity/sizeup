"""Time a hub start locally (#251): how long until the CAD is served, and until the map layers are loaded.

    python tools/startup_bench.py --caches DIR [--runs 3]

DIR holds recorded map caches as a hub's state dir has them (gis/*.geojson, roads511.json, trains/, weather.json):
public data only. Each run copies them into a fresh temp state dir with new, empty databases, runs the hub the
way the container does (`--init-state`, then the app; no Litestream, no S3), with every outside request sent to a
dead proxy (nothing is fetched, nothing leaves this machine), and reports, from the process start:

    init-state      the `--init-state` step (the entrypoint runs it before Litestream starts)
    healthz         the first 200 from /healthz
    cad             the first 200 from /api/cad/state (the CAD is usable)
    layers          /api/cad/gis says no layer is loading any more

Cloudflare Access is off in the copy of deploy/hub/config.hub.toml it runs with. Nothing here touches AWS.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(tmp: Path, port: int) -> Path:
    text = (ROOT / "deploy/hub/config.hub.toml").read_text()
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip()
        if key in ("access_team", "access_aud"):
            line = f'{key} = ""'
        elif key == "talkgroups_file":
            line = f'talkgroups_file = "{ROOT / "deploy/trunk-recorder/talkgroups.csv"}"'
        elif key == "port":
            line = f"port = {port}"
        out.append(line)
    p = tmp / "config.toml"
    p.write_text("\n".join(out) + "\n")
    return p


def _get(port: int, path: str) -> tuple[int, bytes]:
    try:
        with DIRECT.open(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except OSError:
        return 0, b""


def run_once(caches: Path, timeout: float = 180.0) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="sizeup-bench-"))
    state = tmp / "state"
    shutil.copytree(caches, state)
    port = _free_port()
    cfg = _config(tmp, port)
    env = dict(os.environ, SIZEUP_STATE_DIR=str(state), SIZEUP_STATION_TOKEN="bench-" + "0" * 32,
               HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
               http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9", NO_PROXY="", no_proxy="",
               SIZEUP_PREALERT_SERVER="http://127.0.0.1:9", AWS_ACCESS_KEY_ID="bench", AWS_SECRET_ACCESS_KEY="bench",
               AWS_REGION="us-east-1", PYTHONPATH=str(ROOT))
    for k in ("LITESTREAM_BUCKET", "SIZEUP_ALERT_TOPIC", "SIZEUP_STATUS_TOPIC", "SIZEUP_511NY_USER", "SIZEUP_511NY_PASS"):
        env.pop(k, None)
    out: dict = {}
    t0 = time.monotonic()
    subprocess.run([sys.executable, "-m", "sizeup", "--config", str(cfg), "--init-state"], env=env, cwd=ROOT,
                   check=True, capture_output=True)
    out["init_state_s"] = round(time.monotonic() - t0, 2)
    log = open(tmp / "app.log", "wb")
    t1 = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "sizeup", "--config", str(cfg)], env=env, cwd=ROOT,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        while time.monotonic() - t1 < timeout and proc.poll() is None:
            def at() -> float:                  # when the answer came (not when it was asked)
                return round(time.monotonic() - t1, 2)
            if "healthz_s" not in out and _get(port, "/healthz")[0] == 200:
                out["healthz_s"] = at()
            if "healthz_s" in out and "cad_s" not in out and _get(port, "/api/cad/state")[0] == 200:
                out["cad_s"] = at()
            if "cad_s" in out:
                code, body = _get(port, "/api/cad/gis")
                if code == 200:
                    layers = json.loads(body).get("layers") or []
                    if layers and not any(l.get("loading") for l in layers):
                        out["layers_s"] = at()
                        break
            time.sleep(0.05)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        for line in (tmp / "app.log").read_text(errors="replace").splitlines():
            if "layers: map caches read in" in line:      # the app's own count (#251), from its background loader
                out["layers_log"] = line.split("layers: ", 1)[1]
        shutil.rmtree(tmp, ignore_errors=True)
    out["total_from_init_s"] = round(out["init_state_s"] + out.get("cad_s", float("nan")), 2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--caches", required=True, type=Path)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    for i in range(args.runs):
        print(json.dumps(run_once(args.caches)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
