"""[cloud] role = "station": the station's own page (static/station.html) instead of the CAD (user,
2026-10-13: "the local system doesnt show the cad bits just things relevant to the sdr station data
pulling/pushing"). The CAD's pages redirect to it, the CAD's API is refused with 409 (but for Settings >
Radios' channel and vocabulary settings, which the station page uses), the CAD's state push isn't started;
the hub and all-in-one serve the CAD as before, and the station still forwards and takes the hub's
channel choices. Real HTTP on ephemeral ports; nothing opens an SDR."""

import http.client
import json
import re
import threading
from pathlib import Path

from sizeup.app import make_app, make_server
from tests.test_app import wait
from tests.test_cloud import HubRun, all_traffic, base, call, fm_segment, hub_cfg, station, station_cfg

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"

# Every CAD route the console uses, as (method, path): all refused on a station
CAD_GETS = ("/api/cad/state", "/api/cad/stream", "/api/cad/settings", "/api/cad/settings/export", "/api/cad/settings/audit",
            "/api/cad/runcards", "/api/cad/history", "/api/cad/admin", "/api/cad/radios", "/api/cad/comms", "/api/cad/feed",
            "/api/cad/rigs", "/api/cad/whoami", "/api/cad/incident/1", "/api/cad/export/incidents.csv",
            "/api/cad/tts/voices", "/api/cad/geocode")
CAD_POSTS = ("/api/cad/incident", "/api/cad/incident/1/dispatch", "/api/cad/unit/1/status", "/api/cad/runcards",
             "/api/cad/radios", "/api/cad/settings/users", "/api/cad/settings/dispatch", "/api/cad/settings/event_types",
             "/api/cad/settings/import", "/api/cad/prealert/1/dismiss")


def serve(app):
    server = make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.httpd.server_address[1]


def raw(port, path, method="GET", body=None):
    """(status, headers, body) without following a redirect."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"} if body is not None else {})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def station_app(tmp_path, url="https://hub.example.org"):
    return make_app(station_cfg(tmp_path, url, cad_db=str(tmp_path / "st" / "cad.sqlite")))


def test_a_station_serves_its_page_and_refuses_the_cad(tmp_path):
    app = station_app(tmp_path)
    server, port = serve(app)
    try:
        assert server.cad_broadcaster is None                     # no CAD state rebuilt and pushed every second
        page = (STATIC / "station.html").read_bytes()
        for path in ("/", "/index.html", "/station", "/monitor", "/?from=cad"):
            code, _, body = raw(port, path)
            assert (code, re.sub(rb"\?v=[0-9a-f]{10}", b"", body)) == (200, page), path   # its /static/ names versioned (#137)
        for path, where in (("/cad", "/?from=cad"), ("/cad.html", "/?from=cad"), ("/comms", "/#monitor")):
            code, headers, _ = raw(port, path)
            assert (code, headers["Location"]) == (302, where), path
        for path in CAD_GETS:
            code, headers, body = raw(port, path)
            assert code == 409, path
            assert json.loads(body) == {"error": "this is the station: the CAD is on the hub (https://hub.example.org)",
                                        "hub_url": "https://hub.example.org"}, path
        for path in CAD_POSTS:
            code, _, body = raw(port, path, "POST", {"type": "STRU", "address": "1 Main St", "operator": "x"})
            assert code == 409 and "the CAD is on the hub" in json.loads(body)["error"], path
        assert app.cad.store.incidents() == []                   # nothing was written
        # what the station page uses
        st = json.loads(raw(port, "/api/state")[2])
        assert {"radios", "pages", "loose_traffic", "talking", "channels", "sdr"} <= set(st)
        assert json.loads(raw(port, "/api/scene")[2])["station"]["id"] == "t"
        assert json.loads(raw(port, "/healthz")[2])["role"] == "station"
        sm = json.loads(raw(port, "/api/station-mode")[2])
        assert sm["forward"] is True and sm["hub_url"] == "https://hub.example.org"
        assert raw(port, "/static/station.js")[0] == 200 and raw(port, "/static/radio-controls.js")[0] == 200
        assert raw(port, "/api/stream", "HEAD")[0] == 200
        # Settings > Radios on the station page: its vocabulary, and the channel switches (locked while it
        # forwards: the hub's choices win; the answer is the station's own, not the 409)
        assert "vocabulary" in json.loads(raw(port, "/api/cad/stt")[2])
        code, _, body = raw(port, "/api/cad/settings/channels", "POST", {"action": "set", "key": "FG1", "transcribe": False})
        assert code == 400 and "set on the hub" in json.loads(body)["error"]
        code, _, body = raw(port, "/api/cad/settings/stt", "POST", {"action": "set", "key": "vocabulary", "value": ["Engine-sixty"]})
        assert code == 200, body
    finally:
        server.shutdown()
        app.station.stop()


def test_the_station_page_with_forwarding_off_still_has_no_cad(tmp_path):
    app = station_app(tmp_path)
    app.station.set({"forward": False, "operator": "Lt. Ruiz"})
    server, port = serve(app)
    try:
        assert raw(port, "/")[0] == 200 and raw(port, "/cad")[0] == 302
        code, _, body = raw(port, "/api/cad/state")
        assert code == 409 and json.loads(body)["hub_url"] == "https://hub.example.org"
        # not forwarding: the station's own channel switches work again
        code, _, body = raw(port, "/api/cad/settings/channels", "POST",
                            {"action": "set", "key": "FG1", "transcribe": False, "operator": "Lt. Ruiz"})
        assert code == 200, body
        assert wait(lambda: json.loads(raw(port, "/api/state")[2])["channels"]["prefs"].get("FG1") == {"transcribe": False}, 5)
    finally:
        server.shutdown()


def test_the_hub_and_all_in_one_still_serve_the_cad(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        port = int(hub.url.rsplit(":", 1)[1])
        assert hub.server.cad_broadcaster is not None
        code, _, body = raw(port, "/cad")
        assert code == 200 and b"cad.js" in body and b"station.js" not in body
        assert raw(port, "/")[1]["Location"] == "/cad"
        assert "incidents" in hub.get("/api/cad/state")
        assert raw(port, "/api/station-mode")[0] == 409               # (the reverse case, as before)
    finally:
        hub.close()
    app = make_app(base(clips_dir=str(tmp_path / "aio" / "clips")))
    server, port = serve(app)
    try:
        assert server.cad_broadcaster is not None
        assert raw(port, "/")[1]["Location"] == "/cad"
        for path in ("/cad", "/monitor"):
            code, _, body = raw(port, path)
            assert code == 200 and b"cad.js" in body, path
        code, _, body = raw(port, "/api/cad/state")
        assert code == 200 and "incidents" in json.loads(body)
        code, _, body = raw(port, "/api/cad/incident", "POST", {"type": "STRU", "address": "1 Main St", "operator": "x"})
        assert code == 200 and len(app.cad.store.incidents()) == 1
    finally:
        server.shutdown()


def test_a_station_with_its_page_still_forwards_and_takes_the_hubs_channel_choices(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = server = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "Engine 63 on location"})
        server, port = serve(st)
        tid = fm_segment(st)
        # heard here: on the station page's monitor, and up at the hub
        assert wait(lambda: any(t["id"] == tid and t["text"] for t in all_traffic(json.loads(raw(port, "/api/state")[2]))), 15)
        assert wait(lambda: any(t["id"] == tid for t in all_traffic(hub.get("/api/state"))), 15)
        assert raw(port, f"/api/audio/{tid}.wav")[0] == 200
        # Settings > Radios on the hub: the choice arrives here and shows on the station page
        code, _ = call(hub.url + "/api/cad/settings/channels", "POST",
                       {"action": "set", "key": "FG1", "transcribe": False, "operator": "Lt. Ruiz"}, token=None)
        assert code == 200
        assert wait(lambda: json.loads(raw(port, "/api/state")[2])["channels"]["prefs"].get("FG1", {}).get("transcribe") is False, 10)
        assert st.cad.settings.audit_log("radios")[0]["operator"] == "hub"
        # the hub link on the station page: its Hub row, connected
        assert wait(lambda: [r["state"] for r in json.loads(raw(port, "/api/state")[2])["radios"] if r["label"] == "Hub"] == ["ok"], 10)
    finally:
        if server is not None:
            server.shutdown()
        if st is not None:
            st.link.stop()
        hub.close()


def test_the_station_page_shows_only_station_things():
    html = (STATIC / "station.html").read_text()
    nav = re.search(r'<nav class="tabs"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert re.findall(r'class="tab" href="(#\w+)"', nav) == ["#radios", "#monitor", "#settings"]
    assert re.findall(r'<span class="lbl">([^<]+)</span>', nav) == ["Radios", "Monitor", "Settings"]
    scripts = re.findall(r'<script src="/static/([\w.-]+)"', html)
    assert scripts[-1] == "station.js" and {"radio-controls.js", "hublink.js", "live.js"} <= set(scripts)
    for cad_only in ("cad.js", "settings.js", "board.js", "cadmap.js", "radio-view.js", "cad-feed.js", "prealert.js",
                     "radios-panel.js", "styleguide.js", "maplibre"):
        assert cad_only not in html, cad_only
    for gone in ("dispatch-open", "overview-open", "runcards-open", "new-incident", "tx-pill", "db-overlay",
                 'href="/cad"', 'href="/monitor"', "Run cards", "Dispatch"):
        assert gone not in html, gone
    # the header says it is the station: its id, the hub link's state, the CAD on the hub
    for need in ('class="cad-title">Station<', 'id="station"', 'id="hub-pill"', 'id="hub-cad"'):
        assert need in html, need
    js = (STATIC / "station.js").read_text()
    assert "/api/cad/" not in js                                   # only the monitor state and the hub link
    assert 'liveState("/api/stream", "/api/state", onState)' in js and '"/api/station-mode"' in js
    assert 'RadioControls.mount($("#radio-controls"))' in js and 'HubLink.mount($("#hub-card")' in js
    # the CAD's old links land on the matching view
    for old, view in (('h === "radio"', "monitor"), ('h.startsWith("settings/radios")', "radios"),
                      ('h.startsWith("settings")', "settings")):
        assert re.search(re.escape(old) + r'[^\n]*return "' + view + '"', js), old
    # the only CAD routes the page's scripts call are the ones the station serves
    from sizeup.web.server import STATION_CAD
    used = set()
    for name in ("station.js", "radio-controls.js", "hublink.js", "radio.js", "live.js", "ui.js", "help.js", "icons.js"):
        used |= set(re.findall(r'"(/api/cad/[\w/-]+)"', (STATIC / name).read_text()))
    assert used - {"/api/cad/rigs"} <= {p for _, p in STATION_CAD}   # (rigs: the talk alerts, which go without)


def test_the_cad_settings_station_card_is_the_same_hub_link_card():
    settings = (STATIC / "settings.js").read_text()
    assert 'HubLink.mount(card, {' in settings and "/api/station-mode" not in settings
    cad = (STATIC / "cad.html").read_text()
    assert cad.index('src="/static/hublink.js"') < cad.index('src="/static/settings.js"')
    hub = (STATIC / "hublink.js").read_text()
    for need in ('"/api/station-mode"', '"/api/station-mode/test"', '"Last contact"', '"Waiting to go up"', "Forward to hub"):
        assert need in hub, need
