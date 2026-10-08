"""Web server: stdlib HTTP + server-sent events (same approach as shortfinal).

GET  /                         -> /cad (the CAD is the front page; without a CAD: the monitor)
GET  /monitor                  the radio monitor (static/index.html)
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
GET  /api/station-mode         not the hub (409 there): forwarding to the hub, its status and where each
                               secret came from, never a secret (sizeup/cloud/stationmode.py)
POST /api/station-mode {"forward": bool?, "hub_url": str?, "operator": str?}  on / off while running
POST /api/station-mode/test {"hub_url": str?}  the hub's /healthz and station path with the token
GET  /cad                      the PCFD dispatch console (static/cad.html); its API is /api/cad/... (sizeup/cad/api.py)
GET  /comms                    every transmission with its rig, radio ID, channel and events (static/comms.html)

Hub with [cloud] access_team + access_aud (sizeup/cloud/access.py): every request but /healthz and
/api/station/... (the station's bearer token) needs a verified Cloudflare Access token from a person
(401 otherwise: defence in depth should the origin ever be reached around Access), and the CAD API
gets who it is (the operator on every write, GET /api/cad/whoami). Every POST is then checked against
that person's role (sizeup/cad/api.py WRITES: the CAD's own in CadApi.post, the rest here before
anything is read): the talkgroup / mode switches and retranscribe are admin only.
Each verified request also goes to that person's activity (sizeup/cad/activity.py): their sign-in
(once per Access login), when they were last seen, what they did and what they were refused, with
the address (Cf-Connecting-Ip) and browser it came from.

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
                 cad=None, cad_b: Broadcaster | None = None, station=None, health: bytes = b'{"ok":true}',
                 access=None, access_log: Callable[[str], None] | None = None, station_mode=None):
    """cad: a CadApi (sizeup/cad/api.py) and cad_b its own broadcaster (the console's state is
    small and separate from the monitor's, so neither page carries the other's). station: the hub's
    ingest (sizeup/cloud/hub.py Hub), which checks the station's token before reading any body.
    access: an AccessVerifier (sizeup/cloud/access.py): who signed in, required on the hub.
    station_mode: Settings > System > Station (sizeup/cloud/stationmode.py StationMode), not on the hub."""
    hub_says = json.dumps({"error": "this is the hub: station forwarding is switched on the station"}).encode()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        who = None                       # the verified Access identity of the current request (access set)

        def log_message(self, *a) -> None:
            pass

        def log_request(self, code="-", size="-") -> None:
            """access_log (the hub): one line per request, so what a browser behind someone else's
            proxy actually fetched can be seen (ECC workstation, 2026-10-08: the page arrived without
            its CSS and scripts). Routine traffic is left out: health checks, the 2 s state polls and
            the station's own posts when they succeed."""
            if access_log is None:
                return
            path, code = urlsplit(self.path).path, int(code) if str(code).isdigit() else 0
            if path == "/healthz" or (code < 400 and (path in ("/api/state", "/api/cad/state", "/api/cad/whoami")
                                                      or path.startswith("/api/station/"))):
                return
            h = self.headers
            who = getattr(self.who, "email", None) or ("token" if h.get("Cf-Access-Jwt-Assertion") else "no-token")
            access_log(f"http {self.command} {path} {code} ip={h.get('Cf-Connecting-Ip', '-')} {who} "
                       f"ray={h.get('Cf-Ray', '-')} ua={(h.get('User-Agent') or '-')[:90]}")

        def _admit(self, path: str) -> bool:
            """access set: a person with a verified token, else a 401 (and False). /healthz (the
            container health check) and /api/station/ (its own bearer token) are not for people."""
            self.who = None
            if access is None or path == "/healthz" or path.startswith("/api/station/"):
                return True
            who = access.identify(self.headers)
            if who is not None and who.person:
                self.who = who
                if cad is not None:
                    cad.visit(who, self._client(), f"{self.command} {path}")
                return True
            self.close_connection = True     # a POST's body is left unread
            if path.startswith("/api/"):
                return self._send(401, b'{"error":"sign in through Cloudflare Access"}', "application/json") or False
            return self._send(401, b"<!doctype html><meta charset=utf-8><title>Sign in</title>"
                                   b"<p>Sign in through Cloudflare Access to use sizeup: reload this page.</p>",
                              "text/html; charset=utf-8") or False

        def _client(self) -> tuple[str, str]:
            """(ip, browser) for the activity log: the visitor's address as Cloudflare saw it."""
            h = self.headers
            return (h.get("Cf-Connecting-Ip") or "")[:64], (h.get("User-Agent") or "")[:120]

        def _did(self, path: str, body: dict | None = None) -> None:
            """One of the POSTs below went through: in the signed-in person's activity."""
            if self.who is not None and cad is not None:
                cad.did(path, self.who, body, None, self._client())

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    self.close_connection = True   # the client left first (a station restart, a closed tab)

        def _redirect(self, where: str) -> None:
            self.send_response(302)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            if not self._admit(path):
                return
            if path == "/" and cad is not None:   # user, 2026-10-07: "make .../cad the default page"
                return self._redirect("/cad")
            if path in ("/", "/monitor", "/index.html"):
                return self._static("index.html")
            if path in ("/cad", "/cad.html") and cad is not None:
                return self._static("cad.html")
            if path in ("/comms", "/comms.html") and cad is not None:
                return self._static("comms.html")
            if path == "/api/cad/stream" and cad_b is not None:
                return self._stream(cad_b)
            if path.startswith("/api/cad/") and cad is not None:
                return self._send(*cad.get(path, url.query, self.who))
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
            if path == "/api/station-mode":
                if station is not None:
                    return self._send(409, hub_says, "application/json")
                if station_mode is not None:
                    return self._send(200, json.dumps(station_mode.state()).encode(), "application/json")
            self._send(404, b"not found\n", "text/plain")

        def _body(self, limit: int) -> bytes | None:
            """The request body, or None when it's missing a length or bigger than limit."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            return self.rfile.read(n) if 0 <= n <= limit else None

        # HEAD: what GET would answer, without the body. Netskope's browser isolation checks a page
        # with HEAD before loading it; it got 501 here (ECC workstation, 2026-10-08).
        do_HEAD = do_GET

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if not self._admit(path):
                return
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
                return self._send(*cad.post(path, self.rfile.read(n), self.who, self._client()))
            if self.who is not None and cad is not None and (refused := cad.permit(path, self.who, client=self._client())):
                self.close_connection = True     # the body is left unread
                return self._send(*refused)
            if path == "/api/talkgroup" and set_talkgroup is not None:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(n, 1024)) or b"{}")
                    err = set_talkgroup(int(body["tg"]), body.get("record"), body.get("main"))
                except (ValueError, KeyError, TypeError) as e:
                    err = f"bad request: {e}"
                if err:
                    return self._send(400, json.dumps({"error": err}).encode(), "application/json")
                self._did(path, body)
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
                self._did(path, body)
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
                self._did(path, body)
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
                self._did(path, body)
                return self._send(200, json.dumps({"queued": queued}).encode(), "application/json")
            if path in ("/api/station-mode", "/api/station-mode/test") and (station is not None or station_mode is not None):
                raw = self._body(4096)
                if station is not None:
                    self.close_connection = True
                    return self._send(409, hub_says, "application/json")
                try:
                    body = json.loads(raw or b"{}") if raw is not None else None
                    if not isinstance(body, dict):
                        raise ValueError("a JSON object, at most 4 KB")
                    out = station_mode.test(body) if path.endswith("/test") else station_mode.set(body)
                except ValueError as e:
                    return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                self._did(path, body)
                return self._send(200, json.dumps(out).encode(), "application/json")
            if path.startswith("/api/incident/") and path.endswith("/close"):
                ok = close_incident(path[len("/api/incident/"):-len("/close")])
                if ok:
                    self._did(path)
                return self._send(200 if ok else 404, b'{"ok":%s}' % (b"true" if ok else b"false"),
                                  "application/json")
            self._send(404, b"not found\n", "text/plain")

        def _stream(self, b: Broadcaster) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")       # proxies: pass events through, don't buffer
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            seq, payload = b.latest()
            try:
                # Reconnect after 2 s; and 2 KB of comment so proxies that hold a response until they
                # have "enough" of it pass the first event straight on (pages also poll if this fails).
                self.wfile.write(b"retry: 2000\n:" + b" " * 2048 + b"\n\n")
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
                 health: dict | None = None, access=None, access_log: Callable[[str], None] | None = None,
                 station_mode=None):
        self.access = access
        self.broadcaster = Broadcaster(state_fn)
        self.cad_broadcaster = Broadcaster(cad.cad.state) if cad is not None else None
        self.httpd = ThreadingHTTPServer((host, port), make_handler(
            json.dumps(scene, separators=(",", ":")).encode(), self.broadcaster, clip, close_incident, set_mode,
            set_talkgroup, set_talkgroup_group, retranscribe, cad, self.cad_broadcaster, station,
            json.dumps(health or {"ok": True}, separators=(",", ":")).encode(), access, access_log, station_mode))
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
