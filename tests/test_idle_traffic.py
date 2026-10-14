"""Idle traffic (#137): what an open page costs when nothing happens.

- The state push sends the whole state once, then only the sections that changed (idle: the clock), with an
  id a reconnect (Last-Event-ID), the stream's start (?since=) and the polling fallback (?since=) resume from;
  gzipped, one stream flushed per event.
- Static files: named by version from the pages (kept a year), else revalidated (ETag -> 304); gzip.
- The layers' data: ETag + max-age (the layer's refresh, else 300 s), must-revalidate; 304 when unchanged.
- The Radio view and the board ask /api/cad/comms only on screen and when the push's comms_rev moves;
  whoami once per page load, one request for every caller (node runs the page's own code, when installed)."""
import gzip
import http.client
import json
import re
import shutil
import subprocess
import threading
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad import dispatcher
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad, Settings
from sizeup.web.server import Broadcaster, WebServer
from tests import test_gis, test_roads
from tests.test_settings import post

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node")


# ---- the state push: sections, deltas, ids ---------------------------------------------------------------------


def snap_json(b: Broadcaster, since=None) -> dict:
    return json.loads(b.body(b.snapshot(), since))


def test_after_the_whole_state_only_what_changed_is_sent():
    b = Broadcaster(lambda: {})
    s1 = b.publish({"time": 1, "units": [{"name": "E60", "status": "AQ"}] * 50, "lists": {"big": "x" * 5000}, "rev": 3})
    whole = snap_json(b)
    assert whole == {"time": 1, "units": [{"name": "E60", "status": "AQ"}] * 50, "lists": {"big": "x" * 5000}, "rev": 3}
    assert "_delta" not in whole and b.latest()[1] == b.body(b.snapshot())
    s2 = b.publish({"time": 2, "units": [{"name": "E60", "status": "AQ"}] * 50, "lists": {"big": "x" * 5000}, "rev": 3})
    idle = b.body(b.snapshot(), s1)
    assert json.loads(idle) == {"_delta": 1, "time": 2} and len(idle) < 40          # idle: the clock alone
    b.publish({"time": 3, "units": [{"name": "E60", "status": "OS"}] * 50, "lists": {"big": "x" * 5000}, "rev": 4})
    assert set(snap_json(b, s2)) == {"_delta", "time", "units", "rev"}               # the lists stay where they are
    assert set(snap_json(b, s1)) == {"_delta", "time", "units", "rev"}               # further back: the same sections
    b.publish({"time": 4, "units": [], "rev": 4})                                     # a section went away
    assert snap_json(b, s2) == {"_delta": 1, "time": 4, "units": [], "rev": 4, "_gone": ["lists"]}


def test_ids_from_another_boot_or_the_future_get_the_whole_state():
    b = Broadcaster(lambda: {})
    seq = b.publish({"time": 1})
    assert b.since(b.ident(seq)) == seq
    assert b.since(None) is None and b.since("") is None and b.since("nonsense") is None
    assert b.since(f"{b.boot}.{seq + 5}") is None                                    # not ours yet
    assert b.since(f"deadbeef.{seq}") is None                                        # a restarted hub: everything again
    assert Broadcaster(lambda: {}).since(b.ident(seq)) is None


def test_a_broken_state_is_an_error_push_and_the_next_good_one_puts_it_all_back():
    calls = iter([{"time": 1, "units": [1]}, RuntimeError("db gone"), {"time": 3, "units": [1]}])

    def state():
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v
    b = Broadcaster(state)
    s1 = b.publish()
    b.publish()
    bad = snap_json(b, s1)
    assert bad["error"] == "RuntimeError: db gone" and bad["_gone"] == ["units"]
    s2 = b.snapshot()[0]
    b.publish()
    assert snap_json(b, s2) == {"_delta": 1, "time": 3, "units": [1], "_gone": ["error"]}


# ---- through the server ------------------------------------------------------------------------------------------


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    cad = Cad(Settings(), log=lambda m: None)
    g = test_gis.make(tmp_path)
    for key in ("hydrants", "fire_districts", "parcels"):
        g.refresh(key)
    r = test_roads.make(tmp_path)
    test_roads.everything(r)
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": time.time(), "radios": []},
                   lambda cid: None, lambda iid: False, cad=CadApi(cad), roads=r, gis=g)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    port = ws.httpd.server_address[1]

    def get(path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path, headers=headers or {})
        resp = c.getresponse()
        body = resp.read()
        c.close()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    deadline = time.time() + 5
    while ws.cad_broadcaster.snapshot()[0] < 2 and time.time() < deadline:   # the pushes have begun
        time.sleep(0.05)
    yield type("H", (), {"get": staticmethod(get), "port": port, "cad": cad, "ws": ws})
    ws.shutdown()


def read_events(port, path, n, headers=None, decode_gzip=False):
    """The first n events of an SSE stream: [(id, data dict)], and the response headers."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    h = {k.lower(): v for k, v in r.getheaders()}
    z = zlib.decompressobj(31) if decode_gzip else None
    buf, out = b"", []
    while len(out) < n:
        chunk = r.read1(65536)
        assert chunk, "the stream ended"
        buf += z.decompress(chunk) if z else chunk
        while b"\n\n" in buf and len(out) < n:
            ev, buf = buf.split(b"\n\n", 1)
            fields = dict(line.split(b": ", 1) for line in ev.split(b"\n") if b": " in line and not line.startswith(b":"))
            if b"data" in fields:
                out.append((fields.get(b"id", b"").decode(), json.loads(fields[b"data"])))
    c.close()
    return out, h


def test_the_cad_stream_sends_the_state_once_then_only_changes_and_resumes_from_its_id(hub):
    events, h = read_events(hub.port, "/api/cad/stream", 3)
    (id1, first), (_, second), (id3, third) = events
    assert h["cache-control"] == "no-store, no-transform" and "content-encoding" not in h
    assert "units" in first and "lists" in first and "_delta" not in first and id1.startswith(hub.ws.cad_broadcaster.boot + ".")
    assert second["_delta"] == 1 and "units" not in second and "lists" not in second and "time" in second
    assert len(json.dumps(second)) < 120                                              # idle: tiny
    # EventSource reconnecting (Last-Event-ID), or a page that has the state from a GET (?since=): no whole state
    again, _ = read_events(hub.port, "/api/cad/stream", 1, {"Last-Event-ID": id3})
    assert again[0][1]["_delta"] == 1 and "units" not in again[0][1]
    code, gh, _ = hub.get("/api/cad/state")
    since, _ = read_events(hub.port, f"/api/cad/stream?since={gh['x-state-id']}", 1)
    assert since[0][1]["_delta"] == 1 and "lists" not in since[0][1]
    stale, _ = read_events(hub.port, "/api/cad/stream", 1, {"Last-Event-ID": "0000.1"})   # another boot's id
    assert "units" in stale[0][1] and "_delta" not in stale[0][1]
    # a change shows in the next push: that section only
    hub.cad.create({"type": "ALARM", "address": "1 Main St", "operator": "JD"})
    for _id, ev in read_events(hub.port, "/api/cad/stream", 4, {"Last-Event-ID": id3})[0]:
        if "incidents" in ev:
            assert ev["incidents"][0]["address"] == "1 Main St" and "lists" not in ev
            break
    else:
        pytest.fail("the new incident was never pushed")


def test_the_stream_is_gzipped_for_a_browser_that_takes_it_and_each_event_arrives_whole(hub):
    events, h = read_events(hub.port, "/api/cad/stream", 3, {"Accept-Encoding": "gzip, deflate, br"}, decode_gzip=True)
    assert h["content-encoding"] == "gzip" and h["cache-control"] == "no-store, no-transform"   # no-transform: no proxy rework
    assert "units" in events[0][1] and events[1][1]["_delta"] == 1 and events[2][1]["_delta"] == 1
    events, h = read_events(hub.port, "/api/stream", 2, {"Accept-Encoding": "gzip"}, decode_gzip=True)    # the monitor's too
    assert "radios" in events[0][1] and events[1][1] == {"_delta": 1, "time": events[1][1]["time"]}


def test_polling_gets_only_what_changed_since_its_last_answer(hub):
    code, h, body = hub.get("/api/cad/state")
    whole = json.loads(body)
    assert code == 200 and "units" in whole and "comms_rev" in whole and "users_rev" in whole and h["x-state-id"]
    time.sleep(1.2)
    code, h2, body = hub.get(f"/api/cad/state?since={h['x-state-id']}")
    delta = json.loads(body)
    assert code == 200 and delta["_delta"] == 1 and "units" not in delta and "lists" not in delta and h2["x-state-id"] != h["x-state-id"]
    assert json.loads(hub.get("/api/cad/state?since=junk")[2]).keys() == whole.keys()    # unknown id: all of it
    code, h, body = hub.get("/api/state")                                              # the monitor state the same way
    assert code == 200 and "radios" in json.loads(body) and h["x-state-id"]
    time.sleep(1.2)
    assert set(json.loads(hub.get(f"/api/state?since={h['x-state-id']}")[2])) == {"_delta", "time"}


def test_comms_rev_in_the_state_is_what_comms_calls_its_rev(hub):
    st = json.loads(hub.get("/api/cad/state")[2])
    comms = json.loads(hub.get("/api/cad/comms")[2])
    assert st["comms_rev"] == comms["rev"]
    assert json.loads(hub.get(f"/api/cad/comms?rev={st['comms_rev']}")[2])["unchanged"] is True
    hub.cad.record_traffic({"id": "FG1-1", "start": time.time(), "end": time.time() + 2, "source": "FG1", "talkgroup": None,
                            "unit": "", "rig": "E60", "radio_id": "", "text": "on scene", "status": "done"})
    assert json.loads(hub.get("/api/cad/state")[2])["comms_rev"] != st["comms_rev"]


def test_users_rev_moves_only_when_settings_users_change(hub):
    before = json.loads(hub.get("/api/cad/state")[2])["users_rev"]
    hub.cad.create({"type": "ALARM", "address": "1 Main St", "operator": "JD"})
    assert json.loads(hub.get("/api/cad/state")[2])["users_rev"] == before             # an event: no
    post(hub.cad, "users", action="add", name="Corey", role="admin")
    assert json.loads(hub.get("/api/cad/state")[2])["users_rev"] != before


# ---- caching: static files and the layers --------------------------------------------------------------------------


def test_the_pages_name_each_static_file_by_version_and_a_named_version_is_kept(hub):
    code, h, page = hub.get("/cad")
    assert code == 200 and h["cache-control"] == "no-cache" and h["etag"]           # the page: revalidated each load
    assert hub.get("/cad", {"If-None-Match": h["etag"]})[0] == 304
    refs = re.findall(rb'/static/([\w./-]+)\?v=([0-9a-f]{10})', page)
    names = {r[0].decode() for r in refs}
    assert {"cad.js", "live.js", "radio-view.js", "style.css", "icons.svg", "vendor/maplibre-gl.js"} <= names
    assert not re.search(rb'(?:src|href)="/static/[\w./-]+\.(?:js|css)"', page)       # nothing left unversioned
    for name, v in set(refs):
        code, h, body = hub.get(f"/static/{name.decode()}?v={v.decode()}")
        assert code == 200 and h["cache-control"] == "private, max-age=31536000, immutable", name
    code, h, body = hub.get("/static/cad.js")                                          # unversioned: revalidated
    assert code == 200 and h["cache-control"] == "no-cache" and h["etag"]
    code, h304, body304 = hub.get("/static/cad.js", {"If-None-Match": h["etag"]})
    assert code == 304 and body304 == b"" and h304["etag"] == h["etag"] and "content-length" not in h304
    assert hub.get("/static/cad.js?v=0000000000")[1]["cache-control"] == "no-cache"  # an old version: not kept
    code, hz, gz = hub.get("/static/cad.js", {"Accept-Encoding": "gzip"})
    assert hz["content-encoding"] == "gzip" and gzip.decompress(gz) == body and len(gz) < len(body) / 2
    style = hub.get("/static/style.css")[2]                                            # a stylesheet's own @import too
    assert re.search(rb'/static/vendor/maplibre-gl\.css\?v=[0-9a-f]{10}', style)
    assert hub.get("/static/../server.py")[0] == 404 and hub.get("/static/nope.js")[0] == 404


def test_the_layers_data_is_cacheable_and_revalidates_with_304(hub):
    for path, cc in (("/api/cad/airport", "private, max-age=300, must-revalidate"),
                     ("/api/cad/gis", "private, max-age=300, must-revalidate"),
                     ("/api/cad/gis/hydrants", "private, max-age=300, must-revalidate"),
                     ("/api/cad/gis/hydrants?rev=1", "private, max-age=300, must-revalidate"),
                     ("/api/cad/roads", "no-cache")):
        code, h, body = hub.get(path)
        assert code == 200 and h["cache-control"] == cc and h["etag"], path
        code, h2, again = hub.get(path, {"If-None-Match": h["etag"]})
        assert code == 304 and again == b"" and h2["etag"] == h["etag"], path
    code, h, _ = hub.get("/api/cad/gis/parcels?bbox=-73.7,41.0,-73.69,41.01")              # by viewport: max-age its own
    assert code == 200 and "must-revalidate" in h["cache-control"] and h["etag"]
    rev = json.loads(hub.get("/api/cad/roads")[2])["rev"]
    code, h, body = hub.get(f"/api/cad/roads?rev={rev}")                               # the poll: never kept
    assert json.loads(body)["same"] is True and h["cache-control"] == "no-store" and "etag" not in h
    code, h, body = hub.get("/api/cad/airport", {"Accept-Encoding": "gzip"})
    assert h["content-encoding"] == "gzip" and json.loads(gzip.decompress(body))["features"]
    code, h, _ = hub.get("/api/scene")
    assert h["cache-control"] == "no-cache" and hub.get("/api/scene", {"If-None-Match": h["etag"]})[0] == 304
    assert hub.get("/api/cad/state")[1]["cache-control"] == "no-store"                 # live data: never kept


def test_the_page_asks_for_a_layer_by_its_rev_and_the_sprite_by_the_pages_version():
    gl = (STATIC / "gislayers.js").read_text()
    assert "?rev=${encodeURIComponent(l.rev)}" in gl and 'fetch(window.ICONS_SVG || "/static/icons.svg"' in gl
    icons = (STATIC / "icons.js").read_text()
    assert "window.ICONS_SVG" in icons and "`${window.ICONS_SVG}#i-${name}`" in icons
    for name in ("cadmap.js", "maplayers.js", "gislayers.js", "airport.js", "map-style.js"):   # nothing busts a cache
        # (a camera's live snapshot is the exception: ?t= asks for the new picture, maplayers.js)
        src = "\n".join(line for line in (STATIC / name).read_text().splitlines() if "/cameras/" not in line)
        assert not re.search(r"[?&](?:_|t|ts|nocache)=\$\{", src) and 'cache: "reload"' not in src, name


# ---- the page's own code (node) ---------------------------------------------------------------------------------------

RADIO_HARNESS = r"""
const fs = require("fs");
let now = 0, timers = [], calls = [];
global.setTimeout = (fn, ms) => { const t = { id: timers.length + 1, fn, at: now + ms, live: true }; timers.push(t); return t.id; };
global.clearTimeout = (id) => { const t = timers.find((x) => x.id === id); if (t) t.live = false; };
global.Date = { now: () => now };
const stubEl = () => new Proxy({ hidden: false, value: "", checked: false, options: [], dataset: {}, children: [],
  classList: { toggle() {}, contains: () => false, add() {} }, ls: {},
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }, setAttribute() {}, replaceChildren() {}, append() {},
  querySelector: () => null, reset() {} }, { get: (t, k) => (k in t ? t[k] : () => stubEl()) });
const els = {};
global.$ = (sel) => (els[sel] = els[sel] || stubEl());
const listeners = {};
const on = (o) => { o.addEventListener = (t, f) => { (listeners[t] = listeners[t] || []).push(f); }; return o; };
global.window = on(global);
global.document = on({ visibilityState: "visible" });
global.location = { hash: "#radio" };
global.history = { replaceState() {} };
global.localStorage = { getItem: () => null, setItem() {} };
global.Audio = class { addEventListener() {} pause() {} play() { return Promise.resolve(); } };
for (const n of ["el", "syncList", "radioRow", "evChip", "setText", "show", "opSpan", "talkSig", "fmtTime"]) global[n] = () => stubEl();
global.toLocalInput = () => ""; global.fromLocalInput = () => null;
global.Radio = { fillPaging() {} };
global.cad = { state: { comms_rev: "1.1" }, hooks: [], txRev: 0 };
global.api = async (m, p) => { calls.push(p); return { rev: cad.state && cad.state.comms_rev || "9.9", rows: [] }; };
eval(fs.readFileSync(process.argv[2], "utf8"));
const flush = () => new Promise((r) => setImmediate(r));
const fire = (t) => (listeners[t] || []).forEach((f) => f({}));
const push = () => cad.hooks.forEach((f) => f());
const pending = () => timers.filter((t) => t.live).map((t) => t.at - now);
async function advance(ms) { now += ms; for (const t of timers.filter((x) => x.live && x.at <= now)) { t.live = false; t.fn(); } await flush(); }
(async () => {
  const out = {};
  RadioView.open(""); await flush();
  out.opened = calls.length; out.timers = pending();
  push(); await flush(); out.samePush = calls.length;                       // the same comms_rev: nothing
  cad.state = { comms_rev: "2.1" }; push(); await flush(); out.newPush = calls.length;
  fire("sizeup:tx-changed"); await flush(); out.txChanged = calls.length;
  document.visibilityState = "hidden"; fire("visibilitychange"); out.hiddenTimers = pending();
  cad.state = { comms_rev: "3.1" }; push(); await advance(60000); out.hidden = calls.length;
  document.visibilityState = "visible"; fire("visibilitychange"); await flush(); out.visible = calls.length;
  location.hash = ""; cad.state = { comms_rev: "4.1" }; push(); await advance(120000); out.left = calls.length;
  out.leftTimers = pending(); out.leftPolling = RadioView.polling;
  location.hash = "#radio"; cad.state = { time: 1 }; RadioView.open(""); await flush(); out.oldHub = pending();
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_radio_view_asks_only_on_screen_and_when_comms_rev_moves(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(RADIO_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "radio-view.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["opened"] == 1 and out["timers"] == [30000]          # open: asked once, then a 30 s safety poll
    assert out["samePush"] == 1                                       # a push with the same comms_rev asks nothing
    assert out["newPush"] == 2                                        # a new one: asked at once
    assert out["txChanged"] == 3                                      # sizeup:tx-changed: at once
    assert out["hiddenTimers"] == []                                  # the page hidden: no timer left
    assert out["hidden"] == 3                                         # ... and no push or minute makes it ask
    assert out["visible"] == 4                                        # back in view: asked at once
    assert out["left"] == 4 and out["leftTimers"] == [] and out["leftPolling"] is False   # left the tab: silent for good
    assert out["oldHub"] == [2000]                                    # a hub without comms_rev: every 2 s as before


WHO_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (re) => { const m = src.match(re); if (!m) throw new Error("not found: " + re); return m[0]; };
let fetches = 0, resolveNext = [];
global.fetch = (url) => { fetches++; return new Promise((ok) => resolveNext.push(() => ok({ ok: true, json: async () => ({ source: "access", email: "a@b.c", name: "A" }) }))); };
const input = { value: "", classList: { add() {} }, closest: () => ({}) };
global.$ = () => input;
global.cad = { who: null };
global.renderOperators = () => {}; global.applyRoles = () => {};
eval(grab(/const whoFetch = [\s\S]*?\n\}\nasync function loadWho\(fresh\) \{[\s\S]*?\n\}\n/));
const settle = async () => { while (resolveNext.length) resolveNext.shift()(); for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
(async () => {
  const out = {};
  const four = [loadWho(), loadWho(), loadWho(), loadWho()];
  await settle(); await Promise.all(four);
  out.boot = fetches; out.who = cad.who && cad.who.email;
  await loadWho(); await settle(); out.again = fetches;
  const fresh = [loadWho(true), loadWho(true), loadWho()];
  await settle(); await Promise.all(fresh); out.fresh = fetches;
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_whoami_is_one_request_for_every_caller(tmp_path):
    h = tmp_path / "w.js"
    h.write_text(WHO_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == {"boot": 1, "who": "a@b.c", "again": 1, "fresh": 2}   # 4 at once: 1; again: cached; fresh x2: 1 more


def test_whoami_is_asked_again_only_on_visibility_a_refusal_or_a_users_change():
    src = (STATIC / "cad.js").read_text()
    assert src.count('fetch("/api/cad/whoami"') == 1
    callers = re.findall(r"^.*loadWho\((?:true)?\).*$", src, re.M)
    assert len(callers) == 4, callers                     # boot, a 401/403, users_rev moved, the page back in view
    on_state = re.search(r"^function onState\(data\) \{(.*?)^\}", src, re.S | re.M).group(1)
    assert "loadWho(true)" in on_state and "users_rev" in on_state and "changed && cad.who" not in on_state
    assert re.search(r"r\.status === 401 \|\| r\.status === 403\) && cad\.who\) loadWho\(true\)", src)
    assert re.search(r'visibilitychange", \(\) => \{[^}]*visible[^}]*loadWho\(true\)', src)


LIVE_HARNESS = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8") + "; global.liveMerge = liveMerge;");
const whole = { time: 1, units: [1, 2], lists: { a: 1 }, error: "x" };
const a = liveMerge(null, whole);
const b = liveMerge(a, { _delta: 1, time: 2 });
const c = liveMerge(b, { _delta: 1, units: [3], _gone: ["error"] });
console.log(JSON.stringify({ a: a === whole, b, c, sameLists: c.lists === whole.lists, orphan: liveMerge(null, { _delta: 1, time: 3 }) }));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_live_merges_deltas_into_the_whole_state(tmp_path):
    h = tmp_path / "l.js"
    h.write_text(LIVE_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "live.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["a"] is True
    assert out["b"] == {"time": 2, "units": [1, 2], "lists": {"a": 1}, "error": "x"}
    assert out["c"] == {"time": 2, "units": [3], "lists": {"a": 1}}
    assert out["sameLists"] is True and out["orphan"] is None


def test_the_pollers_stop_off_screen_statically():
    """What the node tests run, also checked where node isn't: the Radio view and the board ask on the push's
    comms_rev with a slow safety poll and nothing while hidden; live.js resumes by id."""
    rv = (STATIC / "radio-view.js").read_text()
    assert "SAFETY_MS = 30000" in rv and "cad.hooks.push(onPush)" in rv and "setInterval" not in rv
    assert re.search(r"function shown\(\) \{[^}]*location\.hash\.startsWith\(\"#radio\"\)", rv)
    assert re.search(r"async function poll\(\) \{\s*clearTimeout\(st\.timer\);\s*st\.timer = 0;\s*if \(!shown\(\)\) return;", rv)
    board = (STATIC / "board.js").read_text()
    assert "SAFETY_MS = 30000" in board and "comms_rev" in board and 'document.visibilityState === "hidden") return' in board
    live = (STATIC / "live.js").read_text()
    assert "?since=" in live and "X-State-Id" in live and "ev.lastEventId" in live
