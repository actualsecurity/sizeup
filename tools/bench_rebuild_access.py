#!/usr/bin/env python3
"""CPU cost of two hub hot paths, on a SYNTHETIC CAD in this process (no hub, no AWS, nothing but loopback):

    rebuild   Cad.state() after a store change (the whole CAD state built again: every transmission stored moves the
              rev), with open incidents each carrying radio rows on a fire channel. Most of it used to be the
              status-word repair (sizeup/cad/statushints.py decorate -> Phrases.repair), run for every row each time.
    access    AccessVerifier.verify / identify of one Cloudflare Access token, as every signed-in request does
              (sizeup/cloud/access.py): an RS256 check in pure Python unless the verified token is kept.

The same script measures any checkout, so the before and after come from one tool:

    PYTHONPATH=<checkout> python tools/bench_rebuild_access.py [--incidents 10] [--rows 40] [--reps 40]

Everything is invented: the addresses (Example Ave), the transcripts, a throwaway RSA key made here, the token's
team / audience / email. The CAD is a temporary SQLite file, deleted afterwards. Numbers are CPU (thread_time) per
call on this machine; Fargate's 0.5 vCPU is slower.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# -- loopback only: refuse any other connection before sizeup is imported ---------------------------------------------
_connect = socket.socket.connect


def _local_only(self, addr):
    if not isinstance(addr, (str, bytes)) and str(addr[0]) not in ("127.0.0.1", "::1", "localhost"):
        raise ConnectionRefusedError(f"bench: outbound refused ({addr[0]})")
    return _connect(self, addr)


socket.socket.connect = _local_only

import sizeup  # noqa: E402
from sizeup.cad.service import Cad, Settings  # noqa: E402
from sizeup.cloud import access  # noqa: E402

TEXTS = [  # invented transmissions in the radio's shape: some with the status words misheard, most ordinary
    "Engine {n} on location, two story frame, smoke showing from the second floor",
    "Engine {n} is fine.",
    "Engine {n} around 1412.",
    "Ladder 31 on vacation.",
    "Engine {n} to 60 Control, we'll take the hydrant at the corner",
    "Ladder 31 on location, going to the roof",
    "23 91 to 60 Control, holding one engine and one ladder, all other units can return",
    "Engine {n} responding",
    "60 Control, Engine {n} was fine, 1412.",
    "Engine {n}, primary search negative, second floor",
]


def pcts(xs: list[float], scale: float) -> dict:
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))] * scale   # noqa: E731
    return {"n": len(xs), "p50": round(q(50), 3), "p95": round(q(95), 3), "mean": round(statistics.fmean(xs) * scale, 3)}


# -- rebuild ------------------------------------------------------------------------------------------------------------

def bench_rebuild(n_inc: int, rows: int, reps: int, tmp: Path) -> dict:
    cad = Cad(Settings(db=str(tmp / "cad.sqlite")), log=lambda m: None, clock=time.time)
    now = time.time()
    tids = []
    for i in range(n_inc * rows):                       # stored before the calls: only the links put them on one
        n = 60 + i % 5
        tid = f"FG1-bench-{i:05d}"
        cad.record_traffic({"id": tid, "start": now - 4000 + i * 2, "end": now - 3998 + i * 2, "source": "FG1",
                            "talkgroup": None, "unit": "", "rig": f"E{n}" if i % 3 == 0 else "", "radio_id": "",
                            "text": TEXTS[i % len(TEXTS)].format(n=n), "status": "done"})
        tids.append(tid)
    units = [u["id"] for u in cad.store.units()]
    for i in range(n_inc):
        inc = cad.create({"type": "STRU", "address": f"{100 + i} Example Ave", "operator": "bench",
                          "remarks": f"Invented call {i}"})
        try:
            cad.dispatch(inc["id"], {"unit_ids": [units[(i * 3 + k) % len(units)] for k in range(3)], "operator": "bench"})
        except Exception:  # noqa: BLE001 - a unit already out: fine
            pass
        for _ in range(rows):
            cad.link_traffic(inc["id"], {"traffic_id": tids.pop(), "operator": "bench"})
    st = cad.state()
    on = sum(len(i.get("radio") or []) for i in st["incidents"])
    fixed = sum(1 for i in st["incidents"] for r in (i.get("radio") or []) if r.get("fixed_text"))

    def run(k: int) -> list[float]:
        xs = []
        for _ in range(k):
            cad.store.rev += 1                          # a store change (as any transmission stored makes one)
            c0 = time.thread_time()
            cad.state()
            xs.append(time.thread_time() - c0)
        return xs

    run(3)                                              # warm
    rebuild = pcts(run(reps), 1000)
    idle = []
    for _ in range(reps):
        c0 = time.thread_time()
        cad.state()
        idle.append(time.thread_time() - c0)
    return {"open_incidents": n_inc, "radio_rows_on_incidents": on, "rows_repaired": fixed,
            "state_kb": round(len(json.dumps(st, separators=(",", ":"))) / 1024), "rebuild_cpu_ms": rebuild,
            "idle_state_cpu_ms": pcts(idle, 1000)}


# -- access -------------------------------------------------------------------------------------------------------------

def _prime(bits: int, rnd: random.Random) -> int:
    while True:
        n = rnd.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if n % 65537 == 1 or any(n % p == 0 for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)):
            continue
        d, r = n - 1, 0
        while d % 2 == 0:
            d, r = d // 2, r + 1
        for _ in range(24):
            x = pow(rnd.randrange(2, n - 2), d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                break
        else:
            return n


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def bench_access(reps: int) -> dict:
    rnd, e = random.Random(7), 65537
    while True:
        p, q = _prime(1024, rnd), _prime(1024, rnd)
        if p != q and (p * q).bit_length() == 2048:
            break
    n, d = p * q, pow(e, -1, (p - 1) * (q - 1))
    team, aud, kid = "bench.example.org", "b" * 64, "bench-k1"
    jwk = {"kid": kid, "kty": "RSA", "alg": "RS256", "use": "sig",
           "n": _b64(n.to_bytes(256, "big")), "e": _b64(e.to_bytes(3, "big"))}
    v = access.AccessVerifier(team, aud, fetch=lambda url: {"keys": [jwk]}, log=lambda m: None)
    t = int(time.time())
    h64 = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    p64 = _b64(json.dumps({"aud": [aud], "iss": f"https://{team}", "email": "someone@example.org", "sub": "u-1",
                           "iat": t, "nbf": t - 5, "exp": t + 86400, "type": "app", "nonce": "n" * 40}).encode())
    digest = hashlib.sha256(f"{h64}.{p64}".encode()).digest()
    pre = access.SHA256_PREFIX + digest
    em = b"\x00\x01" + b"\xff" * (256 - len(pre) - 3) + b"\x00" + pre
    token = f"{h64}.{p64}.{_b64(pow(int.from_bytes(em, 'big'), d, n).to_bytes(256, 'big'))}"
    assert v.identity(token).email == "someone@example.org"

    def per_call(fn) -> float:
        c0 = time.thread_time()
        for _ in range(reps):
            fn()
        return round((time.thread_time() - c0) / reps * 1e6, 1)
    header, cookie = {access.HEADER: token}, {"Cookie": f"theme=dark; CF_Authorization={token}; other=1"}
    return {"rs256_check_us": per_call(lambda: access.rsa_pkcs1_sha256_verify(n, e, f"{h64}.{p64}".encode(),
                                                                             base64.urlsafe_b64decode(token.split(".")[2] + "=="))),
            "verify_us": per_call(lambda: v.verify(token)),
            "identify_header_us": per_call(lambda: v.identify(header)),
            "identify_cookie_us": per_call(lambda: v.identify(cookie))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--incidents", type=int, default=10)
    ap.add_argument("--rows", type=int, default=40, help="radio rows linked to each open incident")
    ap.add_argument("--reps", type=int, default=40, help="rebuilds measured")
    ap.add_argument("--access-reps", type=int, default=2000)
    a = ap.parse_args()
    root = Path(sizeup.__file__).resolve().parent.parent
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"sizeup {root} @ {sha or '?'}  python {sys.version.split()[0]}  load {' '.join(f'{x:.1f}' for x in os.getloadavg())}")
    with tempfile.TemporaryDirectory(prefix="sizeup-bench-") as d:
        r = bench_rebuild(a.incidents, a.rows, a.reps, Path(d))
    print(f"rebuild: {r['open_incidents']} open incidents, {r['radio_rows_on_incidents']} radio rows on them "
          f"({r['rows_repaired']} repaired), state {r['state_kb']} KB")
    print(f"  Cad.state() after a change  CPU ms  p50 {r['rebuild_cpu_ms']['p50']}  p95 {r['rebuild_cpu_ms']['p95']}"
          f"  (n {r['rebuild_cpu_ms']['n']})")
    print(f"  Cad.state() unchanged       CPU ms  p50 {r['idle_state_cpu_ms']['p50']}  p95 {r['idle_state_cpu_ms']['p95']}")
    x = bench_access(a.access_reps)
    print(f"access: one token, {a.access_reps} calls each, CPU us per call")
    print(f"  RS256 check alone {x['rs256_check_us']}  verify {x['verify_us']}  identify (header) "
          f"{x['identify_header_us']}  identify (cookie) {x['identify_cookie_us']}")
    print(json.dumps({"sha": sha, "rebuild": r, "access": x}))


if __name__ == "__main__":
    main()
