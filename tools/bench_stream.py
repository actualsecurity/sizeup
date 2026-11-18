"""The SSE fan-out's CPU: one process serving N pages over /api/stream while a big section changes.

    PYTHONPATH=<checkout> python tools/bench_stream.py [--clients 200] [--change 1] [--seconds 15] [--gzip 1]

Runs the real WebServer (sizeup/web/server.py) in this process with a synthetic state: 254 invented units and 10
invented incidents with 60 radio rows each (~530 KB of JSON); every --change seconds (0: never) one note is added,
so that section is pushed again, as a note on a working incident does on the hub; and each push one of 150 rigs
moves on a ~12 KB "map" section (a small section that changes a little each time). The pages are --procs separate
processes (so their own CPU is not counted), each holding its share of the --clients streams as a browser does
(gzip when --gzip 1). Loopback only; nothing is read from or sent to anywhere else.

It measures this process's CPU over --seconds with no page open, then with the pages open, and prints, as one JSON
line: CPU ms per second for both and their difference (the streams' share), the delay from a push's own "time" to
its arrival at a page (every event, and the events that carry the changed section), the bytes on the wire in the
window, and how many pages stayed connected throughout (pages_whole: anything under --clients is a broken run).
The same script measures any checkout: point PYTHONPATH at it (`import sizeup` says which one it loaded).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import zlib

TIME = re.compile(rb'"time":([0-9.eE+-]+)')


# ---- the pages (a child process) ------------------------------------------------------------------------------------


def _connect(port: int) -> socket.socket:
    for attempt in range(20):            # (many pages at once can overflow the listen backlog: try again)
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            time.sleep(0.1 * (attempt + 1))
    raise SystemExit("could not connect")


def _page(port: int, gz: bool, until: float, stats: dict, lock: threading.Lock) -> None:
    s = _connect(port)
    s.settimeout(30)
    s.sendall(b"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1\r\n" + (b"Accept-Encoding: gzip\r\n" if gz else b"") + b"\r\n")
    head, buf, wire = b"", b"", 0
    while b"\r\n\r\n" not in head:
        head += s.recv(65536)
    head, rest = head.split(b"\r\n\r\n", 1)
    z = zlib.decompressobj(31) if b"content-encoding: gzip" in head.lower() else None
    if gz and z is None:
        raise SystemExit("asked for gzip and the stream is not gzipped")
    chunk, every, changed, whole = rest, [], [], True
    while True:
        if chunk:
            if time.time() >= stats["t0"]:
                wire += len(chunk)
            buf += z.decompress(chunk) if z else chunk
            while b"\n\n" in buf:
                ev, buf = buf.split(b"\n\n", 1)
                i = ev.find(b"\ndata: ")
                if i < 0:
                    continue
                now = time.time()
                m = TIME.search(ev, i, i + 200)
                if m and now >= stats["t0"]:
                    lag = (now - float(m.group(1))) * 1000
                    every.append(lag)
                    if b'"incidents":' in ev:
                        changed.append(lag)
        if time.time() > until:
            break
        try:
            chunk = s.recv(1 << 20)
        except TimeoutError:
            chunk = b""
            whole = False
        else:
            if not chunk:                # the server closed it
                whole = False
                break
    s.close()
    with lock:
        stats["every"] += every
        stats["changed"] += changed
        stats["wire"] += wire
        stats["pages"] += int(whole)


def pages_main(port: int, n: int, gz: bool, warm: float, seconds: float) -> None:
    t0 = time.time() + warm
    stats, lock = {"every": [], "changed": [], "wire": 0, "pages": 0, "t0": t0}, threading.Lock()
    ts = [threading.Thread(target=_page, args=(port, gz, t0 + seconds, stats, lock), daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
        time.sleep(0.005)
    for t in ts:
        t.join()
    stats.pop("t0")
    sys.stdout.write(json.dumps(stats))


# ---- the server (this process) ---------------------------------------------------------------------------------------


def _units() -> list[dict]:
    return [{"name": f"E{100 + i}", "kind": "Engine", "status": "AQ", "station": f"Station {i % 9}",
             "dept": "Example FD", "since": 1700000000 + i, "lat": 41.0 + i * 1e-4, "lon": -73.7 - i * 1e-4,
             "note": "", "crew": i % 5} for i in range(254)]


WORDS = ("engine ladder command side alpha bravo charley delta search primary secondary complete clear floor line "
         "rear front water supply hydrant hose stretch vent roof interior attack exposure smoke fire showing nothing "
         "copy received en route on scene available returning staging level one two three four").split()


def _words(rng, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def _incidents(v: int) -> list[dict]:
    import random
    rng, out = random.Random(7), []           # the same rows each time: only the notes change
    for n in range(10):
        radio = [{"id": f"T{n}-{k}-{rng.getrandbits(32):08x}", "start": 1700000000.0 + rng.random() * 9e5,
                  "end": 1700000000.0 + rng.random() * 9e5, "channel": "FG1", "source": "FG1", "talkgroup": None,
                  "rig": f"E{100 + k % 30}", "radio_id": f"{rng.getrandbits(20)}", "unit": f"E{100 + k % 30}",
                  "text": _words(rng, 40), "words": [{"w": w, "p": round(rng.random(), 3)} for w in _words(rng, 12).split()],
                  "fixed_text": None, "duration": round(rng.random() * 9, 2), "audio": f"/api/audio/T{n}-{k}.wav",
                  "links": [{"incident": f"26-{1000 + n}", "by": "auto", "score": round(rng.random(), 3)}]}
                 for k in range(60)]
        notes = [{"t": 1700000000 + j, "by": "JD", "text": f"note {j} on incident {n}"} for j in range(2 + (v if n == 0 else 0))]
        out.append({"id": f"26-{1000 + n}", "type": "STRUCTURE FIRE", "address": f"{n + 1} Example Ave",
                    "units": [{"name": f"E{100 + n * 3 + j}", "status": "OS"} for j in range(3)], "notes": notes,
                    "radio": radio, "opened": 1700000000 + n})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clients", type=int, default=200)
    ap.add_argument("--change", type=float, default=1.0, help="seconds between changes of the big section (0: never)")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--gzip", type=int, default=1)
    ap.add_argument("--procs", type=int, default=4, help="processes the pages run in")
    ap.add_argument("--pages", nargs=5, help=argparse.SUPPRESS)        # internal: port n gz warm seconds
    a = ap.parse_args()
    if a.pages:
        port, n, gz, warm, secs = a.pages
        return pages_main(int(port), int(n), gz == "1", float(warm), float(secs))

    import sizeup
    from sizeup.web.server import WebServer
    sys.setswitchinterval(0.001)                    # as the hub runs (sizeup/app.py HUB_SWITCH_S, #284)
    units, inc, v, last = _units(), _incidents(0), 0, time.time()

    def state() -> dict:
        nonlocal inc, v, last
        now = time.time()
        if a.change and now - last >= a.change:
            v, last = v + 1, now
            inc = _incidents(v)
        n = int(now)                                # each push: one rig moved (a ~12 KB section, as the map's)
        rigs = [{"name": f"E{100 + i}", "lat": round(41.0 + i * 7.31e-4 + (n % 97 if i == n % 150 else 0) * 1e-5, 6),
                 "lon": round(-73.7 - i * 9.17e-4, 6), "heading": (i * 37) % 360} for i in range(150)]
        return {"time": now, "units": units, "incidents": inc, "rev": v, "map": rigs}

    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, state, lambda cid: None, lambda iid: False)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    port = ws.httpd.server_address[1]
    time.sleep(1.5)

    def cpu_over(seconds: float) -> float:
        c0, t0 = time.process_time(), time.perf_counter()
        time.sleep(seconds)
        return (time.process_time() - c0) * 1000 / (time.perf_counter() - t0)

    idle = cpu_over(a.seconds)
    warm = 3.0 + a.clients / 100
    per = [a.clients // a.procs + (1 if i < a.clients % a.procs else 0) for i in range(a.procs)]
    kids = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "--pages", str(port), str(n), str(a.gzip),
                              str(warm), str(a.seconds)], stdout=subprocess.PIPE) for n in per if n]
    time.sleep(warm)
    busy = cpu_over(a.seconds)
    got = {"every": [], "changed": [], "wire": 0, "pages": 0}
    for k in kids:
        out, _ = k.communicate(timeout=a.seconds + 120)
        for key, val in json.loads(out).items():
            got[key] += val
    ws.shutdown()

    def pct(xs: list[float]) -> list[float] | None:
        if len(xs) < 2:
            return None
        q = statistics.quantiles(xs, n=100)
        return [round(statistics.median(xs), 1), round(q[94], 1), round(q[98], 1)]

    print(json.dumps({"sizeup": os.path.dirname(sizeup.__file__), "clients": a.clients, "gzip": bool(a.gzip),
                      "change_s": a.change, "seconds": a.seconds,
                      "cpu_ms_per_s": {"no_pages": round(idle, 1), "pages": round(busy, 1), "streams": round(busy - idle, 1)},
                      "lag_ms_p50_p95_p99": {"every": pct(got["every"]), "changed": pct(got["changed"])},
                      "pages_whole": got["pages"], "events": len(got["every"]), "wire_mb": round(got["wire"] / 1e6, 1)}))


if __name__ == "__main__":
    main()
