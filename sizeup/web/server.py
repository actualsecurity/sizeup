"""Web server: stdlib HTTP + server-sent events (same approach as shortfinal).

GET  /                         -> /cad (the CAD is the front page)
GET  /monitor                  the wall board: the CAD page read-only, overview only, larger type (static/cad.html;
                               cad.js sees the path). For a second screen and for those not running the console.
GET  /api/scene                station, home view, firehouses (static per run)
GET  /api/state[?since=<id>]   current state (see STATE below); ?since=: only the sections changed since that
                               push (Broadcaster); X-State-Id: the push it is as of
GET  /api/stream[?since=<id>]  SSE: the whole state, then each second only the sections that changed (idle: the
                               clock); each event's id resumes a reconnect (Last-Event-ID); gzip when accepted
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
GET  /cad                      the PCFD dispatch console (static/cad.html); its API is /api/cad/... (sizeup/cad/api.py).
                               Views: Dispatch, Overview, Radio (/cad#radio?unit=&channel=&event=&q=: every transmission
                               with its rig, radio ID, channel and events), Settings (/cad#settings/<section>)
GET  /comms                    -> /cad#radio?<the same query> (the old communications page)
Caching (#137): /static/... named by version from the pages (?v=, kept a year), else revalidated (ETag, 304);
the layers' data (airport, GIS catalog and layers, roads) ETag + max-age, must-revalidate; text gzipped.
GET  /api/cad/roads, /api/cad/cameras, /api/cad/cameras/<id>/image
                               the map's highway layers and camera snapshots, proxied (sizeup/roads.py, #73)
GET  /api/cad/gis, /api/cad/gis/<key>[?bbox=]
                               the county GIS layers' catalog and each layer's GeoJSON (sizeup/gis.py, #91)
GET  /api/cad/trains, /api/cad/trains/near?lat=&lon=
                               Metro-North's trains on the map, and those approaching an event (sizeup/trains.py, #131)
GET  /api/cad/weather[/speech?id=, /radar/frames, /radar/<frame>/<z>/<x>/<y>.png]
                               NWS alerts, conditions, radar tiles, NOAA tides (sizeup/weather.py, #132)
GET  /api/cad/aircraft         aircraft near HPN, shortfinal's PCFD-focused picture (sizeup/aircraft.py, #155): polled by
                               the page only while the layer is on; ETag (304), never in the state push
GET  /api/cad/health           the hub only: Settings > System > Station health (sizeup/cloud/health.py, #79):
                               the station's health, its alerts and 24 h of their history; anyone signed in
GET  /api/cad/datasources, POST /api/cad/datasources/<key>/refresh
                               Settings > Data sources (sizeup/datasources.py, #186): every data pull, Refresh now
                               (sizeup/cad/api.py); polled by the panel while open, never in the state push

[cloud] role = "station" (the PC with the dongles; user, 2026-10-13: "the local system doesnt show the cad
bits just things relevant to the sdr station"): the station page instead of the CAD (static/station.html):
GET  /, /station, /monitor     the station page: Radios (receivers, dongles, channels, speech-to-text),
                               Monitor (what this station heard), Settings (the hub link, this install)
GET  /cad, /comms              -> /?from=cad, /#monitor (the page says where the CAD is; its script maps the
                               CAD's old #settings/... and #radio links to its views)
     /api/cad/...              409 {"error": "this is the station: the CAD is on the hub", hub_url}, but for
                               what the station page's Radios uses (STATION_CAD below): Settings > Radios'
                               channel and vocabulary settings, which the station keeps for its own radios
                               (the hub sends its choices with set_channel_prefs). The CAD itself (the
                               store: the radio-ID registry, those settings, the communications log) keeps
                               running inside; only its pages and API are off, and its state push.

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
  "prealert": {open, items, tapout, ...} | None,     # the phone pre-alert from Tapout (sizeup/prealert.py)
}
"""

from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import re
import secrets
import threading
import time
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

STATIC_DIR = Path(__file__).parent / "static"
MAX_CAD_BODY = 65536
# role = "station": the only CAD routes served, (method, path): what Settings > Radios (radio-controls.js) uses
STATION_CAD = {("GET", "/api/cad/stt"), ("POST", "/api/cad/settings/channels"), ("POST", "/api/cad/settings/stt")}

# Caching (#137). Static files: the pages name each by its content (/static/cad.js?v=<hash>, rewritten as the
# page is served), and a named version is kept a year; a file asked for without it (or an old one) is
# revalidated each use (ETag -> 304). The pages themselves: revalidated each load, so a deploy shows at once.
# Data that changes on its own schedule: ETag and max-age (the layer's refresh, else 300 s), must-revalidate.
IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "no-cache"
DATA_MAX_AGE = {"/api/cad/airport": 300, "/api/cad/gis": 300}      # path -> max-age, s (ETag from the body)
GZIP_MIN = 1024
COMPRESSIBLE = ("text/", "application/json", "application/javascript", "application/geo+json", "image/svg+xml")
VERSIONED = re.compile(r"""(["'(])/static/([A-Za-z0-9_\-./]+?\.(?:js|css|svg|png|ico|woff2?))(?=[#"')])""")


def etag_of(body: bytes) -> str:
    return '"' + hashlib.sha1(body).hexdigest()[:16] + '"'


def etag_matches(inm: str | None, etag: str) -> bool:
    """If-None-Match against our ETag (weak comparison, as for a GET)."""
    if not inm:
        return False
    bare = etag.removeprefix("W/")
    return inm.strip() == "*" or any(t.strip().removeprefix("W/") == bare for t in inm.split(","))


def accepts_gzip(headers) -> bool:
    return "gzip" in str(headers.get("Accept-Encoding") or "").lower()


def compressible(ctype: str) -> bool:
    return ctype.startswith(COMPRESSIBLE)


class Assets:
    """The static folder as served: each file's bytes, ETag, version (from its content) and gzip, re-read
    when it changes on disk. Pages (.html) and stylesheets have their /static/... references versioned
    (?v=<the file's version>), so a browser keeps each version for good and a deploy names new ones."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._cache: dict[str, tuple[tuple[int, int], dict]] = {}
        self._lock = threading.Lock()

    def get(self, rel: str) -> dict | None:
        """{"body", "etag", "version", "ctype"} or None when it isn't a file under the folder."""
        target = (self.root / rel).resolve()
        if self.root not in target.parents:
            return None
        try:
            st = target.stat()
        except OSError:
            return None
        if not target.is_file():
            return None
        stamp, refs = (st.st_mtime_ns, st.st_size), target.suffix in (".html", ".css")
        with self._lock:
            hit = self._cache.get(rel)
        if hit and hit[0] == stamp and not refs:
            return hit[1]
        body = target.read_bytes()
        if refs:                        # what it names may have changed even if it didn't
            body = VERSIONED.sub(self._ref, body.decode("utf-8")).encode("utf-8")
            if hit and hit[0] == stamp and hit[1]["body"] == body:
                return hit[1]
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        etag = etag_of(body)
        a = {"body": body, "etag": etag, "version": etag.strip('"')[:10], "ctype": ctype, "gz": None}
        with self._lock:
            self._cache[rel] = (stamp, a)
        return a

    def _ref(self, m: re.Match) -> str:
        a = self.get(m.group(2))
        return f"{m.group(1)}/static/{m.group(2)}?v={a['version']}" if a else m.group(0)

    def gz(self, a: dict) -> bytes:
        if a["gz"] is None:
            a["gz"] = gzip.compress(a["body"], 6)
        return a["gz"]


class Broadcaster:
    """The state, rebuilt every period_s (or at once on poke()), kept as sections: each top-level key
    serialized on its own, with the push (seq) it last changed at. A page is sent the whole state once
    and then only the sections that changed since what it has (#137: the CAD state is ~160 KB, most of
    it the unit list and the CAD lists, and it was sent whole every second to every open page, ~9 MB a
    minute each; idle, only "time" changes). Each push carries an id "<boot>.<seq>": the stream's SSE
    id (EventSource sends it back as Last-Event-ID when it reconnects) and the polling fallback's
    ?since= (X-State-Id on GET). A delta is {"_delta": 1, <changed sections>, "_gone": [removed keys]?};
    a page merges it into what it has (live.js). An id from another boot, or none: the whole state."""
    MIN_GAP_S = 0.25   # poke() pushes at once, but never more often than this

    def __init__(self, state_fn: Callable[[], dict], period_s: float = 1.0):
        self.state_fn, self.period_s = state_fn, period_s
        self.boot = secrets.token_hex(4)
        self._wake = threading.Event()
        self._cond = threading.Condition()
        self._seq = 0
        self._sections: dict[str, tuple[bytes, int]] = {}     # key -> (JSON, the seq it last changed at)
        self._gone: dict[str, int] = {}                      # key -> the seq it went away at
        self._full: tuple[int, bytes] = (0, b"{}")
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
            self.publish()
            time.sleep(self.MIN_GAP_S)
            self._wake.wait(max(0.0, self.period_s - self.MIN_GAP_S))
            self._wake.clear()

    def publish(self, st: dict | None = None) -> int:
        """Build the state now (or take st) and push it; returns its seq."""
        try:
            st = self.state_fn() if st is None else st
            if not isinstance(st, dict):
                raise TypeError(f"state is a {type(st).__name__}, not a dict")
            parts = {str(k): json.dumps(v, separators=(",", ":")).encode() for k, v in st.items()}
        except Exception as e:  # never let one bad state kill the display
            parts = {"time": json.dumps(time.time()).encode(), "error": json.dumps(f"{type(e).__name__}: {e}").encode()}
        with self._cond:
            seq, prev = self._seq + 1, self._sections
            sections = {k: (b, prev[k][1]) if k in prev and prev[k][0] == b else (b, seq) for k, b in parts.items()}
            gone = {k: n for k, n in self._gone.items() if k not in sections}
            gone.update({k: seq for k in prev if k not in sections})
            self._seq, self._sections, self._gone = seq, sections, gone
            self._cond.notify_all()
        return seq

    # -- what a page is sent ----------------------------------------------------------------------------------

    def snapshot(self) -> tuple[int, dict[str, tuple[bytes, int]], dict[str, int]]:
        """(seq, sections, gone): the dicts are replaced on each push, never changed, so safe to read unlocked."""
        with self._cond:
            return self._seq, self._sections, self._gone

    def ident(self, seq: int) -> str:
        return f"{self.boot}.{seq}"

    def since(self, ident: str | None) -> int | None:
        """The seq a page's id names, if it is from this boot and not ahead of us; else None (send it all)."""
        boot, _, n = (ident or "").strip().partition(".")
        if boot != self.boot or not n.isdigit():
            return None
        n = int(n)
        with self._cond:
            return n if 0 < n <= self._seq else None

    def body(self, snap, since: int | None = None) -> bytes:
        """The whole state (since None) or what changed after since, as JSON."""
        seq, sections, gone = snap
        if since is None:
            with self._cond:
                if self._full[0] == seq and seq:
                    return self._full[1]
            out = b"{" + b",".join(json.dumps(k).encode() + b":" + b for k, (b, _) in sections.items()) + b"}"
            with self._cond:
                if seq >= self._full[0]:
                    self._full = (seq, out)
            return out
        parts = [b'"_delta":1'] + [json.dumps(k).encode() + b":" + b for k, (b, at) in sections.items() if at > since]
        went = [k for k, at in gone.items() if at > since]
        if went:
            parts.append(b'"_gone":' + json.dumps(went).encode())
        return b"{" + b",".join(parts) + b"}"

    def latest(self) -> tuple[int, bytes]:
        """(seq, the whole state)."""
        snap = self.snapshot()
        return snap[0], self.body(snap)

    def wait(self, seq: int, timeout: float = 15.0) -> int:
        """Until a push after seq (or timeout, or stop); returns the newest seq."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seq or self.stopped, timeout)
            return self._seq


def make_handler(scene: bytes, b: Broadcaster, clip: Callable[[str], bytes | None],
                 close_incident: Callable[[str], bool], set_mode: Callable[[str], str | None] | None = None,
                 set_talkgroup: Callable[..., str | None] | None = None,
                 set_talkgroup_group: Callable[..., str | None] | None = None, retranscribe=None,
                 cad=None, cad_b: Broadcaster | None = None, station=None, health: bytes = b'{"ok":true}',
                 access=None, access_log: Callable[[str], None] | None = None, station_mode=None,
                 role: str = "", roads=None, gis=None, trains=None, weather=None, station_health=None, stream_gzip: bool = True,
                 aircraft=None):
    """cad: a CadApi (sizeup/cad/api.py) and cad_b its own broadcaster (the console's state is
    small and separate from the monitor's, so neither page carries the other's). station: the hub's
    ingest (sizeup/cloud/hub.py Hub), which checks the station's token before reading any body.
    access: an AccessVerifier (sizeup/cloud/access.py): who signed in, required on the hub.
    station_mode: Settings > System > Station (sizeup/cloud/stationmode.py StationMode), not on the hub.
    role: [cloud] role; "station" serves the station page and refuses the CAD's (see the top).
    roads: the highway layers (sizeup/roads.py Roads): /api/cad/roads, /api/cad/cameras..., GET only.
    gis: the county GIS layers (sizeup/gis.py Gis): /api/cad/gis..., GET only (ETag, gzip).
    trains: Metro-North's trains (sizeup/trains.py Trains): /api/cad/trains..., GET only.
    weather: weather and water (sizeup/weather.py Weather): /api/cad/weather..., GET only.
    aircraft: aircraft near HPN (sizeup/aircraft.py AircraftLayer): /api/cad/aircraft, GET only (ETag -> 304).
    stream_gzip: the state streams gzipped to a browser that takes it (#137).
    station_health: the hub's StationHealth (sizeup/cloud/health.py): GET /api/cad/health."""
    assets = Assets(STATIC_DIR)
    hub_says = json.dumps({"error": "this is the hub: station forwarding is switched on the station"}).encode()
    station_ui = role == "station"

    def station_says() -> bytes:
        url = (getattr(station_mode, "hub_url", "") or "") if station_mode is not None else ""
        return json.dumps({"error": "this is the station: the CAD is on the hub" + (f" ({url})" if url else ""),
                           "hub_url": url}).encode()

    def cad_refused(method: str, path: str) -> bool:
        return station_ui and path.startswith("/api/cad/") and (method, path) not in STATION_CAD

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
            if path == "/healthz" or (code < 400 and (path in ("/api/state", "/api/cad/state", "/api/cad/whoami", "/api/cad/health",
                                                                   "/api/cad/datasources")
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

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None, gz: bytes | None = None) -> None:
            """The answer. With an ETag the browser already has (If-None-Match): 304, no body. Text and JSON
            over GZIP_MIN go gzipped to a browser that takes it (gz: already compressed), unless the
            handler did that itself (Content-Encoding)."""
            h = dict(headers or {})
            if code == 200 and h.get("ETag") and etag_matches(self.headers.get("If-None-Match"), h["ETag"]):
                code, body = 304, b""
            elif code == 200 and len(body) >= GZIP_MIN and compressible(ctype) and "Content-Encoding" not in h:
                h.setdefault("Vary", "Accept-Encoding")
                if accepts_gzip(self.headers):
                    body = gz if gz is not None else gzip.compress(body, 5)
                    h["Content-Encoding"] = "gzip"
            self.send_response(code)
            if code != 304:
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
            for k, v in h.items():
                self.send_header(k, v)
            if "Cache-Control" not in h:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if code == 304:
                return
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    self.close_connection = True   # the client left first (a station restart, a closed tab)

        def _data(self, path: str, query: str, resp: tuple) -> tuple:
            """A data answer with the caching DATA_MAX_AGE gives its path (an ETag from the body, unless the
            handler set its own); /api/cad/roads asked whole (not ?rev=): revalidated each time."""
            code, body, ctype, h = (*resp, {}) if len(resp) == 3 else resp
            h = dict(h)
            if code == 200 and "ETag" not in h:
                if path in DATA_MAX_AGE:
                    h.update({"ETag": etag_of(body), "Cache-Control": f"private, max-age={DATA_MAX_AGE[path]}, must-revalidate"})
                elif path == "/api/cad/roads" and not query:
                    h.update({"ETag": etag_of(body), "Cache-Control": REVALIDATE})
            elif code == 200 and "Cache-Control" in h and "must-revalidate" not in h["Cache-Control"] and "max-age" in h["Cache-Control"]:
                h["Cache-Control"] += ", must-revalidate"      # a GIS layer: its ETag, max-age its refresh
            return code, body, ctype, h

        def _state(self, bc: Broadcaster, query: str, fresh) -> None:
            """GET the state: ?since=<id> (the polling fallback, live.js): only what changed since that push,
            as on the stream; else the whole state (fresh(): built now, else the latest push). X-State-Id:
            the push it is as of, for the next ?since=."""
            since = bc.since(next((v for k, v in parse_qsl(query) if k == "since"), None))
            snap = bc.snapshot()
            h = {"X-State-Id": bc.ident(snap[0])}
            if since is not None or fresh is None:
                return self._send(200, bc.body(snap, since), "application/json", h)
            code, body, ctype = fresh()[:3]
            return self._send(code, body, ctype, h)

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
            if station_ui:
                if path in ("/", "/index.html", "/station", "/monitor"):
                    return self._static("station.html")
                if path in ("/cad", "/cad.html"):
                    return self._redirect("/?from=cad")
                if path in ("/comms", "/comms.html"):
                    return self._redirect("/#monitor")
                if cad_refused("GET", path):
                    return self._send(409, station_says(), "application/json")
            if path in ("/", "/index.html") and cad is not None:   # user, 2026-10-07: "make .../cad the default page"
                return self._redirect("/cad")
            if path in ("/cad", "/cad.html", "/monitor") and cad is not None:
                return self._static("cad.html")
            if path in ("/comms", "/comms.html") and cad is not None:   # Comms is the CAD's Radio view now
                return self._redirect("/cad#radio" + (f"?{url.query}" if url.query else ""))
            if path == "/api/cad/stream" and cad_b is not None:
                return self._stream(cad_b)
            if path == "/api/cad/state" and cad_b is not None and cad is not None:
                return self._state(cad_b, url.query, lambda: cad.get(path, "", self.who))
            if roads is not None and roads.handles(path):        # the highway layers, camera snapshots (#73)
                return self._send(*self._data(path, url.query, roads.get(path, url.query, self.headers)))
            if gis is not None and gis.handles(path):            # the county GIS layers (#91)
                return self._send(*self._data(path, url.query, gis.get(path, url.query, self.headers)))
            if trains is not None and trains.handles(path):      # Metro-North's trains (#131)
                return self._send(*trains.get(path, url.query, self.headers))
            if weather is not None and weather.handles(path):    # NWS alerts, conditions, radar; NOAA tides (#132)
                return self._send(*weather.get(path, url.query, self.headers))
            if aircraft is not None and aircraft.handles(path):  # aircraft near HPN (#155)
                return self._send(*aircraft.get(path, url.query, self.headers))
            if path == "/api/cad/health" and station_health is not None:
                return self._send(200, json.dumps(station_health.panel(), separators=(",", ":"), default=str).encode(),
                                  "application/json")
            if path.startswith("/api/cad/") and cad is not None:
                return self._send(*self._data(path, url.query, cad.get(path, url.query, self.who)))
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):], url.query)
            if path == "/api/scene":
                return self._send(200, scene, "application/json", {"ETag": etag_of(scene), "Cache-Control": REVALIDATE})
            if path == "/api/state":
                return self._state(b, url.query, None)
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
            if cad_refused("POST", path):
                self.close_connection = True     # the body is left unread
                return self._send(409, station_says(), "application/json")
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
            """SSE: the whole state first (or, with an id from this boot, only what changed since: Last-Event-ID
            when EventSource reconnects, ?since= when the page already has the state from a GET), then each push's changed sections (Broadcaster), each with its id. Nothing
            changed but the clock: {"_delta":1,"time":...}, so the page knows the stream is alive.
            gzip (a browser that takes it): one stream, flushed at each event (Z_SYNC_FLUSH)."""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")       # proxies: pass events through, don't buffer
            self.send_header("Connection", "close")
            z = zlib.compressobj(6, zlib.DEFLATED, 31) if stream_gzip and accepts_gzip(self.headers) else None
            if z is not None:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            since = b.since(self.headers.get("Last-Event-ID")
                            or next((v for k, v in parse_qsl(urlsplit(self.path).query) if k == "since"), None))
            out = (lambda raw: z.compress(raw) + z.flush(zlib.Z_SYNC_FLUSH)) if z is not None else (lambda raw: raw)
            try:
                # Reconnect after 2 s; and 2 KB of comment so proxies that hold a response until they
                # have "enough" of it pass the first event straight on (pages also poll if this fails).
                # Gzipped, 2 KB of spaces would shrink to nothing: random hex instead.
                pad = secrets.token_hex(1024).encode() if z is not None else b" " * 2048
                self.wfile.write(out(b"retry: 2000\n:" + pad + b"\n\n"))
                while not b.stopped:
                    snap = b.snapshot()
                    body = b.body(snap, since)
                    self.wfile.write(out(b"id: " + b.ident(snap[0]).encode() + b"\ndata: " + body + b"\n\n"))
                    self.wfile.flush()
                    since = snap[0]
                    b.wait(since)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def _static(self, rel: str, query: str = "") -> None:
            a = assets.get(rel)
            if a is None:
                return self._send(404, b"not found\n", "text/plain")
            v = next((x for k, x in parse_qsl(query) if k == "v"), "")
            cc = IMMUTABLE if v and v == a["version"] else REVALIDATE      # an old version asked for: this one, not kept
            gz = assets.gz(a) if len(a["body"]) >= GZIP_MIN and compressible(a["ctype"]) and accepts_gzip(self.headers) else None
            self._send(200, a["body"], a["ctype"], {"ETag": a["etag"], "Cache-Control": cc}, gz=gz)

    return Handler


class WebServer:
    def __init__(self, host: str, port: int, scene: dict, state_fn: Callable[[], dict],
                 clip: Callable[[str], bytes | None], close_incident: Callable[[str], bool],
                 set_mode: Callable[[str], str | None] | None = None,
                 set_talkgroup: Callable[..., str | None] | None = None,
                 set_talkgroup_group: Callable[..., str | None] | None = None,
                 retranscribe: Callable[[list[str] | None], int] | None = None, cad=None, station=None,
                 health: dict | None = None, access=None, access_log: Callable[[str], None] | None = None,
                 station_mode=None, role: str = "", roads=None, gis=None, trains=None, weather=None, station_health=None,
                 aircraft=None):
        self.access = access
        self.broadcaster = Broadcaster(state_fn)
        # The CAD's own state push (the whole console rebuilt and sent once a second): not on a station,
        # whose page has no CAD (role = "station": its /api/cad/stream is refused).
        self.cad_broadcaster = Broadcaster(cad.cad.state) if cad is not None and role != "station" else None
        self.httpd = ThreadingHTTPServer((host, port), make_handler(
            json.dumps(scene, separators=(",", ":")).encode(), self.broadcaster, clip, close_incident, set_mode,
            set_talkgroup, set_talkgroup_group, retranscribe, cad, self.cad_broadcaster, station,
            json.dumps(health or {"ok": True}, separators=(",", ":")).encode(), access, access_log, station_mode, role,
            roads, gis, trains, weather, station_health, aircraft=aircraft))
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
