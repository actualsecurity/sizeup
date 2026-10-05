import json
import os
import subprocess
import sys
import time

import sizeup.radio.claims as claims


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_claim_file_format_and_rules():
    # Agreed with shortfinal: <dir>/<serial>.json = {"app", "pid", "mode", "since"}.
    assert claims.claim("v4", "FG1")
    c = json.loads((claims.DIR / "v4.json").read_text())
    assert c["app"] == "sizeup" and c["pid"] == os.getpid() and c["mode"] == "FG1" and abs(c["since"] - time.time()) < 5
    assert claims.other("v4") is None                                   # ours
    claims.release("v4")
    assert not (claims.DIR / "v4.json").exists()

    live = {"app": "shortfinal", "pid": os.getppid(), "mode": "TWR", "since": time.time()}
    (claims.DIR / "adsb.json").write_text(json.dumps(live))
    assert claims.other("adsb")["mode"] == "TWR"
    assert not claims.claim("adsb", "FG1")                              # held by the other app: refused
    claims.release("adsb")                                              # never removes another app's claim
    assert (claims.DIR / "adsb.json").exists()

    (claims.DIR / "00000001.json").write_text(json.dumps(dict(live, pid=dead_pid())))
    assert claims.other("00000001") is None and not (claims.DIR / "00000001.json").exists()   # stale: dropped
    assert claims.claim("00000001", "South 2")
