"""Web server: stdlib HTTP + server-sent events (same approach as shortfinal).

GET  /                         the display (static/index.html)
GET  /api/scene                station, home view, firehouses (static per run)
GET  /api/state                current state (see STATE below)
GET  /api/stream               SSE: the state, pushed every second
GET  /api/audio/<id>.wav       a page or transmission clip
POST /api/incident/<id>/close  close an incident by hand
POST /api/talkgroup {"tg": 307, "record": bool?, "main": bool?}  per-talkgroup switches (state "talkgroups")
POST /api/talkgroup-group {"group": "County Police", "record": bool?, "main": bool?}  a whole section
POST /api/mode {"mode": ...}  one shared dongle: hand it to a channel or trunk-recorder (state "sdr")
GET  /healthz                   {"ok": true, version, build, role}: cheap, for the container health check
GET|POST /api/station/...      [cloud] role = "hub" only: the station's ingest and job queue, bearer token
                               (sizeup/cloud/hub.py)
GET  /cad                      the PCFD dispatch console (static/cad.html); its API is /api/cad/... (sizeup/cad/api.py)
GET  /comms                    every transmission with its rig, radio ID, channel and events (static/comms.html)

STATE = {
  "time": unix s,
  "radios": [{"label", "mhz", "state", "detail", "open", "quieting_db", ...}],
  "incidents": [Incident.as_dict(), newest first],   # sizeup/incidents.py
  "pages": [Page.as_dict() + {"dispatch": Dispatch.as_dict() | None}, newest first, last 50],
  "loose_traffic": [Traffic.as_dict()],              # traffic with no active incident
}
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

STATIC_DIR = Path(__file__).parent / "static"
MAX_CAD_BODY = 65536


class Broadcaster:
    MIN_GAP_S = 0.25   # poke() pushes at once, but never more often than this

    def __init__(self, state_fn: Callable[[], dict], period_s: float = 1.0):
        self.state_fn, self.period_s = state_fn, period_s
        self._wake = threading.Event()
        self._cond = threading.Condition()
        self._payload = b"{}"
        self._seq = 0
        self.stopped = False

    def start(self) -> None:
        threading.Thread(target=self._run, name="broadcast", daemon=True).start()

    def poke(self) -> None:
        """Something changed that should be on screen now (a radio keyed up): push early."""
        self._wake.set()

    def stop(self) -> None:
        self.stopped = True
        self._wake.set()
        with self._cond:
            self._cond.notify_all()

    def _run(self) -> None:
        while not self.stopped:
            try:
                body = json.dumps(self.state_fn(), separators=(",", ":")).encode()
            except Exception as e:  # never let one bad state kill the display
                body = json.dumps({"time": time.time(), "error": f"{type(e).__name__}: {e}"}).encode()
            with self._cond:
                self._payload, self._seq = body, self._seq + 1
                self._cond.notify_all()
            time.sleep(self.MIN_GAP_S)
            self._wake.wait(max(0.0, self.period_s - self.MIN_GAP_S))
            self._wake.clear()

    def latest(self) -> tuple[int, bytes]:
        with self._cond:
            return self._seq, self._payload

    def wait(self, seq: int, timeout: float = 15.0) -> tuple[int, bytes]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seq or self.stopped, timeout)
            return self._seq, self._payload


def make_handler(scene: bytes, b: Broadcaster, clip: Callable[[str], bytes | None],
                 close_incident: Callable[[str], bool], set_mode: Callable[[str], str | None] | None = None,
                 set_talkgroup: Callable[..., str | None] | None = None,
                 set_talkgroup_group: Callable[..., str | None] | None = None, retranscribe=None,
                 cad=None, cad_b: Broadcaster | None = None, station=None, health: bytes = b'{"ok":true}'):
    """cad: a CadApi (sizeup/cad/api.py) and cad_b its own broadcaster (the console's state is
    small and separate from the monitor's, so neither page carries the other's). station: the hub's
    ingest (sizeup/cloud/hub.py Hub), which checks the station's token before reading any body."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path in ("/cad", "/cad.html") and cad is not None:
                return self._static("cad.html")
            if path in ("/comms", "/comms.html") and cad is not None:
                return self._static("comms.html")
            if path == "/api/cad/stream" and cad_b is not None:
                return self._stream(cad_b)
            if path.startswith("/api/cad/") and cad is not None:
                return self._send(*cad.get(path, url.query))
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/scene":
                return self._send(200, scene, "application/json")
            if path == "/api/state":
                return self._send(200, b.latest()[1], "application/json")
            if path == "/api/stream":
                return self._stream(b)
            if path.startswith("/api/audio/") and path.endswith(".wav"):
                wav = clip(path[len("/api/audio/"):-4])
                return self._send(200, wav, "audio/wav") if wav else self._send(404, b"no such clip\n", "text/plain")
            if path == "/healthz":
                return self._send(200, health, "application/json")
            if path.startswith("/api/station/") and station is not None:
                return self._send(*station.handle("GET", path, url.query, self.headers, None))
            self._send(404, b"not found\n", "text/plain")

        def _body(self, limit: int) -> bytes | None:
            """The request body, or None when it's missing a length or bigger than limit."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            return self.rfile.read(n) if 0 <= n <= limit else None

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/api/station/") and station is not None:
                code, body, ctype = station.handle("POST", path, "", self.headers, self._body)
                if code >= 400:              # the body may be unread (a refused token): don't reuse the connection
                    self.close_connection = True
                return self._send(code, body, ctype)
            if path.startswith("/api/cad/") and cad is not None:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if not 0 <= n <= MAX_CAD_BODY:
                    return self._send(400, b'{"error":"bad or too large body (64 KB max)"}', "application/json")
                return self._send(*cad.post(path, self.rfile.read(n)))
            if path == "/api/talkgroup" and set_talkgroup is not None:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(n, 1024)) or b"{}")
                    err = set_talkgroup(int(body["tg"]), body.get("record"), body.get("main"))
                except (ValueError, KeyError, TypeError) as e:
                    err = f"bad request: {e}"
                if err:
                    return self._send(400, json.dumps({"error": err}).encode(), "application/json")
                return self._send(200, b'{"ok":true}', "application/json")
            if path == "/api/talkgroup-group" and set_talkgroup_group is not None:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(n, 1024)) or b"{}")
                    err = set_talkgroup_group(str(body["group"]), body.get("record"), body.get("main"))
                except (ValueError, KeyError, TypeError) as e:
                    err = f"bad request: {e}"
                if err:
                    return self._send(400, json.dumps({"error": err}).encode(), "application/json")
                return self._send(200, b'{"ok":true}', "application/json")
            if path == "/api/mode" and set_mode is not None:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(n, 1024)) or b"{}")
                    err = set_mode(str(body["mode"]), str(body["sdr"]) if body.get("sdr") else None)
                except (ValueError, KeyError, TypeError) as e:
                    err = f"bad request: {e}"
                if err:
                    return self._send(400, json.dumps({"error": err}).encode(), "application/json")
                return self._send(200, b'{"ok":true}', "application/json")
            if path == "/api/retranscribe" and retranscribe is not None:
                # {"ids": ["307-...", ...]} re-runs those; {"all": true} every recording still on disk
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                    ids = None if body.get("all") else [str(i) for i in body["ids"]][:2000]
                    queued = retranscribe(ids)
                except (ValueError, KeyError, TypeError, AttributeError) as e:
                    return self._send(400, json.dumps({"error": f"bad request: {e}"}).encode(), "application/json")
                return self._send(200, json.dumps({"queued": queued}).encode(), "application/json")
            if path.startswith("/api/incident/") and path.endswith("/close"):
                ok = close_incident(path[len("/api/incident/"):-len("/close")])
                return self._send(200 if ok else 404, b'{"ok":%s}' % (b"true" if ok else b"false"),
                                  "application/json")
            self._send(404, b"not found\n", "text/plain")

        def _stream(self, b: Broadcaster) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            seq, payload = b.latest()
            try:
                while not b.stopped:
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    self.wfile.flush()
                    seq, payload = b.wait(seq)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def _static(self, rel: str) -> None:
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                return self._send(404, b"not found\n", "text/plain")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

    return Handler


class WebServer:
    def __init__(self, host: str, port: int, scene: dict, state_fn: Callable[[], dict],
                 clip: Callable[[str], bytes | None], close_incident: Callable[[str], bool],
                 set_mode: Callable[[str], str | None] | None = None,
                 set_talkgroup: Callable[..., str | None] | None = None,
                 set_talkgroup_group: Callable[..., str | None] | None = None,
                 retranscribe: Callable[[list[str] | None], int] | None = None, cad=None, station=None,
                 health: dict | None = None):
        self.broadcaster = Broadcaster(state_fn)
        self.cad_broadcaster = Broadcaster(cad.cad.state) if cad is not None else None
        self.httpd = ThreadingHTTPServer((host, port), make_handler(
            json.dumps(scene, separators=(",", ":")).encode(), self.broadcaster, clip, close_incident, set_mode,
            set_talkgroup, set_talkgroup_group, retranscribe, cad, self.cad_broadcaster, station,
            json.dumps(health or {"ok": True}, separators=(",", ":")).encode()))
        self.httpd.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"

    def serve_forever(self) -> None:
        self.broadcaster.start()
        if self.cad_broadcaster:
            self.cad_broadcaster.start()
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            self.broadcaster.stop()
            if self.cad_broadcaster:
                self.cad_broadcaster.stop()
            self.httpd.server_close()

    def poke(self) -> None:
        self.broadcaster.poke()
        if self.cad_broadcaster:
            self.cad_broadcaster.poke()

    def shutdown(self) -> None:
        self.broadcaster.stop()
        if self.cad_broadcaster:
            self.cad_broadcaster.stop()
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()
