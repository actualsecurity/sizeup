"""Web server: stdlib HTTP + server-sent events (same approach as shortfinal).

GET  /                         the display (static/index.html)
GET  /api/scene                station, home view, firehouses (static per run)
GET  /api/state                current state (see STATE below)
GET  /api/stream               SSE: the state, pushed every second
GET  /api/audio/<id>.wav       a page or transmission clip
POST /api/incident/<id>/close  close an incident by hand
GET  /healthz

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


class Broadcaster:
    def __init__(self, state_fn: Callable[[], dict], period_s: float = 1.0):
        self.state_fn, self.period_s = state_fn, period_s
        self._cond = threading.Condition()
        self._payload = b"{}"
        self._seq = 0
        self.stopped = False

    def start(self) -> None:
        threading.Thread(target=self._run, name="broadcast", daemon=True).start()

    def stop(self) -> None:
        self.stopped = True
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
            time.sleep(self.period_s)

    def latest(self) -> tuple[int, bytes]:
        with self._cond:
            return self._seq, self._payload

    def wait(self, seq: int, timeout: float = 15.0) -> tuple[int, bytes]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seq or self.stopped, timeout)
            return self._seq, self._payload


def make_handler(scene: bytes, b: Broadcaster, clip: Callable[[str], bytes | None],
                 close_incident: Callable[[str], bool]):
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
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/scene":
                return self._send(200, scene, "application/json")
            if path == "/api/state":
                return self._send(200, b.latest()[1], "application/json")
            if path == "/api/stream":
                return self._stream()
            if path.startswith("/api/audio/") and path.endswith(".wav"):
                wav = clip(path[len("/api/audio/"):-4])
                return self._send(200, wav, "audio/wav") if wav else self._send(404, b"no such clip\n", "text/plain")
            if path == "/healthz":
                return self._send(200, b'{"ok":true}', "application/json")
            self._send(404, b"not found\n", "text/plain")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/api/incident/") and path.endswith("/close"):
                ok = close_incident(path[len("/api/incident/"):-len("/close")])
                return self._send(200 if ok else 404, b'{"ok":%s}' % (b"true" if ok else b"false"),
                                  "application/json")
            self._send(404, b"not found\n", "text/plain")

        def _stream(self) -> None:
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
                 clip: Callable[[str], bytes | None], close_incident: Callable[[str], bool]):
        self.broadcaster = Broadcaster(state_fn)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(
            json.dumps(scene, separators=(",", ":")).encode(), self.broadcaster, clip, close_incident))
        self.httpd.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"

    def serve_forever(self) -> None:
        self.broadcaster.start()
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            self.broadcaster.stop()
            self.httpd.server_close()

    def shutdown(self) -> None:
        self.broadcaster.stop()
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()
