"""Our units followed and lit up on the map, AVL-ready (GitHub #358).

The user: "can we add the same follow function and highlight function with our rigs/units/apparatus too? we will get
rig location data in here soon enough". The aircraft layer's Follow and rings (#352) for our rigs, through one shared
helper (mapfollow.js), and live fixes drawn as aircraft are (glide.js's follower), with no AVL provider yet.

  the hub     Cad.positions stays the single input. mapview.ingest() checks the generic ingest shape a future provider
              hands in ({unit, lat, lon, at, heading?, speed?, source}); Cad._feed_position() feeds one (tests and dev
              only). A fix up to POSITION_STALE_S old is live; up to POSITION_MAX_AGE_S stale (still drawn there);
              older, the unit is placed by its status again. Without a fix, placement is exactly as before.
  the page    cadmap.js in node with a stand-in MapLibre (markers, a map that records its eases): click a rig to follow
              it, the chip's words, what lets go (a drag, Recenter, ✕, the rig gone, another follow on the map); the
              rings (transmitting, picked from a row or a talker chip, a row hovered); a stale fix hollow, faded and
              aged; a live fix drawn on between fixes, not jumped.

No AVL data exists yet (the source isn't chosen): the fixes are made up, in the shape of the ingest contract, at
made-up points near the firehouse the other map tests use."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sizeup.cad import mapview
from sizeup.cad.mapview import place
from sizeup.cad.service import Cad, Settings

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

HQ = {"name": "PCFD Headquarters", "ll": [-73.66692, 41.00253]}
INC = {"id": 7, "number": "26-000007", "lat": 41.0, "lon": -73.67}
E60 = {"id": 1, "name": "E60", "kind": "engine", "status": "AV", "board": "main"}
NOW = 1_791_400_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def fix(age, **kw):
    """A made-up fix in the ingest shape, `age` seconds old at NOW."""
    return {"unit": "E61", "lat": 41.0101, "lon": -73.6702, "at": NOW - age, "source": "test", **kw}


# ---- the hub: one input, the ingest shape, fresh / stale / too old ---------------------------------------------------


def test_a_fix_is_live_then_stale_then_ignored():
    _, pos = mapview.ingest(fix(0, heading=90.0, speed=12.5))
    live = place(E60, "enroute", HQ, INC, dict(pos, at=NOW - 12), NOW)
    assert (live["place"], live["lon"], live["lat"], live["stale"], live["age"]) == ("position", -73.6702, 41.0101, False, 12.0)
    assert (live["heading"], live["speed"], live["source"]) == (90.0, 12.5, "test")
    assert live["look"] == "responding" and live["line_to"] == [INC["lon"], INC["lat"]]
    stale = place(E60, "enroute", HQ, INC, dict(pos, at=NOW - mapview.POSITION_STALE_S - 30), NOW)
    assert (stale["place"], stale["stale"], stale["lat"]) == ("position", True, 41.0101)          # still drawn there
    gone = place(E60, "enroute", HQ, INC, dict(pos, at=NOW - mapview.POSITION_MAX_AGE_S - 1), NOW)
    assert gone == place(E60, "enroute", HQ, INC, None, NOW)                                      # by status again
    assert mapview.POSITION_STALE_S < mapview.POSITION_MAX_AGE_S


def test_status_placement_is_unchanged_without_a_fix():
    """No fix: the same markers, key for key, as before #358 (no at / age / stale on them)."""
    keys = {"unit", "id", "kind", "status", "look", "place", "group", "lon", "lat", "ring", "incident_id",
            "incident_number", "line_to", "station", "home_line",
            "status_at"}                                    # #376: when its status changed (a rig just dispatched)
    for role, inc in (("free", None), ("dispatched", INC), ("enroute", INC), ("onscene", INC), ("oos", None)):
        m = place(E60, role, HQ, inc, None, NOW)
        assert set(m) == keys, role
    assert place(E60, "onscene", HQ, INC, None, NOW)["place"] == "incident"
    assert place(E60, "free", HQ, None, None, NOW)["place"] == "station"
    s = mapview.map_slice([dict(E60, station=HQ["name"])], [], {"AV": "free"}.get, [HQ], positions={}, now=NOW)
    assert [(m["unit"], m["place"]) for m in s["markers"]] == [("E60", "station")]
    assert s["avl"] == {"stale_s": mapview.POSITION_STALE_S, "max_age_s": mapview.POSITION_MAX_AGE_S,
                        "horizon_s": mapview.POSITION_HORIZON_S}


def test_heading_speed_and_source_are_kept_only_when_good():
    base = {"lat": 41.01, "lon": -73.67, "at": NOW}
    assert mapview.fresh_position(base, NOW) == base                                             # as before: no extras
    good = mapview.fresh_position(dict(base, heading=359.5, speed=0.0, source=" vendor "), NOW)
    assert good == dict(base, heading=359.5, speed=0.0, source="vendor")
    bad = mapview.fresh_position(dict(base, heading=-5, speed="fast", source=""), NOW)
    assert bad == base                                                                           # the fix kept, not those
    for b in (dict(base, lat=float("nan")), dict(base, at=True), dict(base, lon=200)):
        assert mapview.fresh_position(b, NOW) is None


def test_a_fix_from_the_future_or_in_milliseconds_is_refused():
    base = {"lat": 41.01, "lon": -73.67}
    assert mapview.fresh_position(dict(base, at=NOW + 30), NOW)["at"] == NOW + 30          # clock skew: taken
    assert mapview.fresh_position(dict(base, at=NOW + mapview.POSITION_FUTURE_S + 1), NOW) is None
    assert mapview.fresh_position(dict(base, at=NOW * 1000), NOW) is None                # milliseconds
    with pytest.raises(ValueError, match="milliseconds"):
        mapview.ingest(dict(fix(0), at=NOW * 1000))
    with pytest.raises(ValueError):
        mapview.ingest(dict(fix(0), at=12345.0))                                       # not an epoch time


def test_the_speed_bounds_agree():
    """ingest and fresh_position take the same speeds: 0 to MAX_SPEED_MS inclusive."""
    top = mapview.MAX_SPEED_MS
    for v in (0.0, 10.0, top):
        _, pos = mapview.ingest(fix(0, speed=v))
        assert mapview.fresh_position(pos, NOW)["speed"] == v
    for v in (-0.1, top + 0.1):
        with pytest.raises(ValueError):
            mapview.ingest(fix(0, speed=v))
        assert "speed" not in mapview.fresh_position(dict(fix(0), speed=v), NOW)


def test_the_ingest_shape_is_checked():
    code, pos = mapview.ingest(fix(3, heading=270, speed=8))
    assert code == "E61" and pos == {"lat": 41.0101, "lon": -73.6702, "at": NOW - 3, "source": "test",
                                     "heading": 270.0, "speed": 8.0}
    assert mapview.ingest(fix(3)) == ("E61", {"lat": 41.0101, "lon": -73.6702, "at": NOW - 3, "source": "test"})
    for bad in ([], dict(fix(0), unit=""), dict(fix(0), lat="41"), dict(fix(0), lat=95), dict(fix(0), at=None),
                dict(fix(0), heading=400), dict(fix(0), speed=-1), {k: v for k, v in fix(0).items() if k != "source"}):
        with pytest.raises(ValueError):
            mapview.ingest(bad)


def test_fed_fixes_reach_the_maps_through_cad_positions_alone():
    clock = Clock()
    c = Cad(Settings(), log=lambda m: None, clock=clock)
    c._feed_position(fix(5, heading=45, speed=10))
    assert c.positions() == {"E61": {"lat": 41.0101, "lon": -73.6702, "at": NOW - 5, "source": "test", "heading": 45.0, "speed": 10.0}}
    st = c.state()
    json.dumps(st)                                                                               # as the push sends it
    m = next(x for x in st["map"]["markers"] if x["unit"] == "E61")
    assert (m["place"], m["stale"], m["heading"], m["speed"], m["source"], m["at"]) == ("position", False, 45.0, 10.0, "test", NOW - 5)
    assert next(u for u in st["units"] if u["name"] == "E61")["position"]["source"] == "test"
    assert st["map"]["avl"] == {"stale_s": 60.0, "max_age_s": 300.0, "horizon_s": 8.0}
    clock.t += 90
    m = next(x for x in c.state()["map"]["markers"] if x["unit"] == "E61")
    assert m["place"] == "position" and m["stale"] is True and m["age"] == 95.0
    clock.t += 300                                                                               # too old: by status (no station: not drawn)
    assert not any(x["unit"] == "E61" for x in c.state()["map"]["markers"])
    assert c.locations()["E61"] is None


def test_no_provider_feeds_it():
    """The source isn't chosen: nothing in the app sets Cad.positions or feeds fixes."""
    for p in (ROOT / "sizeup").rglob("*.py"):
        t = p.read_text()
        assert "._feed_position(" not in t.replace("Cad._feed_position(", ""), p          # (named in docs, never called)
        assert "cad.positions = " not in t and ".positions = lambda" not in t.replace("self.positions = lambda", ""), p


# ---- the page, in node --------------------------------------------------------------------------------------------------

HARNESS = r"""
const fs = require("fs");
global.window = global;
const IN = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = {};
class CL {
  constructor(e) { this.e = e; }
  _s() { return new Set((this.e.className || "").split(/\s+/).filter(Boolean)); }
  add(...cs) { const s = this._s(); cs.forEach((c) => s.add(c)); this.e.className = [...s].join(" "); }
  remove(c) { const s = this._s(); s.delete(c); this.e.className = [...s].join(" "); }
  contains(c) { return this._s().has(c); }
  toggle(c, on) { if (on === undefined ? !this.contains(c) : on) this.add(c); else this.remove(c); }
  [Symbol.iterator]() { return this._s()[Symbol.iterator](); }
}
let focused = null;
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.on = {}; this.attrs = {};
                     this.className = ""; this._t = ""; this.hidden = false; this.title = ""; this.classList = new CL(this);
                     this.offsetParent = {}; this.clientWidth = 900; this.clientHeight = 600; this.isConnected = true;
                     const st = {}; this.style = new Proxy(st, { get: (o, k) => (k === "setProperty" ? (a, b) => { o[a] = b; } : o[k]), set: (o, k, v) => { o[k] = v; return true; } }); }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  append(...xs) { for (const x of xs) { this.children.push(x); if (x && typeof x === "object") x.parent = this; } }
  replaceChildren(...xs) { this._t = ""; this.children = []; this.append(...xs); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  fire(t, ev) { for (const f of this.on[t] || []) f({ stopPropagation() {}, preventDefault() {}, ...(ev || {}) }); }
  click() { this.fire("click"); }
  remove() { this.isConnected = false; }
  focus() { focused = this; }
  contains(x) { return x === this || this.children.some((c) => c && c.contains && c.contains(x)); }
  scrollIntoView() { this.scrolled = true; }
  all() { const r = [this]; for (const c of this.children) if (c && c.all) r.push(...c.all()); return r; }
  find(cls) { return this.all().filter((e) => e.classList.contains(cls)); }
  byText(t) { return this.all().find((e) => e.tagName === "BUTTON" && e.textContent === t); }
  closest(sel) { const m = /^\.([\w-]+)\[data-([\w-]+)\]$/.exec(sel); for (let e = this; e; e = e.parent) if (e.classList && e.classList.contains(m[1]) && e.dataset[m[2]]) return e; return null; }
}
const docOn = {};
const overviewOpen = new El("button"); overviewOpen.addEventListener("click", () => { out.navigated = true; });
global.document = { createElement: (t) => new El(t), addEventListener: (t, f) => { (docOn[t] = docOn[t] || []).push(f); },
                    visibilityState: "visible", getElementById: (id) => (id === "overview-open" ? overviewOpen : null),
                    documentElement: new El("html") };
global.Icon = (n) => { const e = new El("i"); e.className = `ico i-${n}`; return e; };
const rec = { eases: [], fired: [], zoomOpts: [], fits: 0, moving: false };
class Marker {
  constructor(o) { this.el = o.element; this.ll = null; this.off = [0, 0]; }
  setLngLat(ll) { this.ll = Array.isArray(ll) ? { lng: ll[0], lat: ll[1] } : ll; return this; }
  getLngLat() { return this.ll; }
  setOffset(o) { this.off = o; return this; }
  addTo() { return this; }
  remove() {}
  getElement() { return this.el; }
}
class FakeMap {
  constructor(o) { this.container = o.container; this.h = {}; this.sources = {}; this.layers = {}; this.center = { lng: -73.68, lat: 41.02 }; this.zoom = 13;
                   const zh = () => ({ isEnabled: () => true, enable: (x) => rec.zoomOpts.push(x === undefined ? null : x), disableRotation() {} });
                   this.scrollZoom = zh(); this.touchZoomRotate = zh(); }
  on(t, a, b) { (this.h[t] = this.h[t] || []).push(b || a); }
  once() {}
  fire(t, p) { rec.fired.push(t); for (const f of this.h[t] || []) f(p || {}); }
  emit(t, e) { for (const f of this.h[t] || []) f(e || {}); }
  addControl() {} resize() {} isStyleLoaded() { return true; } loaded() { return true; }
  getSource(id) { return this.sources[id]; } addSource(id) { this.sources[id] = { setData: (d) => { this.sources[id].data = d; } }; }
  getLayer(id) { return this.layers[id]; } addLayer(l) { this.layers[l.id] = l; } addImage() {}
  getContainer() { return this.container; }
  getCenter() { return this.center; } getZoom() { return this.zoom; }
  isMoving() { return rec.moving; } isZooming() { return false; }
  project(ll) { const [lng, lat] = Array.isArray(ll) ? ll : [ll.lng, ll.lat]; return { x: lng * 1e5, y: -lat * 1e5 }; }
  easeTo(o) { rec.eases.push(o); this.center = { lng: o.center[0], lat: o.center[1] }; if (o.zoom !== undefined) this.zoom = o.zoom; }
  jumpTo(o) { this.center = { lng: o.center[0], lat: o.center[1] }; } fitBounds() { rec.fits++; }
}
class Bounds { constructor(a) { this.a = a; } extend() {} getCenter() { return this.a; } getEast() { return 1; } getWest() { return 0; } getNorth() { return 1; } getSouth() { return 0; } }
global.maplibregl = { Map: FakeMap, Marker, NavigationControl: class {}, LngLatBounds: Bounds };
global.buildMapStyle = () => ({});
global.firehouseIcon = () => ({});
global.talking = {};
global.Radio = { talkingFor: (n) => global.talking[n] || null };
global.cad = { state: { map: IN.map, lists: IN.lists }, skew: 0, mode: IN.mode || "none" };
const frames = [];                                         // requestAnimationFrame, run by hand
global.requestAnimationFrame = (f) => { frames.push(f); return frames.length; };
"""

LOAD = r"""
for (const f of ["motion.js", "glide.js", "mapfollow.js", "cadmap.js"]) eval(fs.readFileSync(IN.static + "/" + f, "utf8"));
const host = new El("div");
const rm = new CadMap.RigMap(host, { scene: { home: { center: [-73.68, 41.02], zoom: 13 }, firehouses: [] }, pins: true, unset: false,
                                     editable: () => !!IN.admin, onPick: (id) => { out.picked = id; } });
const L = rm.layer;
const setMap = (map, focus) => { cad.state.map = map; rm.update({ map, lists: IN.lists }, focus || null, ""); };
setMap(IN.map);
const chip = (u) => L.chips.get(u);
const cls = (u) => chip(u).el.className.split(/\s+/).filter((c) => !c.startsWith("maplibregl-"));
const follow = () => host.find("cm-follow")[0];
const followText = () => follow().find("ac-follow-t")[0].textContent;
const spin = (ms) => { const t = Date.now(); while (Date.now() - t < ms) {} };
const lng = (u) => chip(u).marker.getLngLat().lng;
const run = (n, ms) => { for (let i = 0; i < n; i++) { spin(ms); const fs_ = frames.splice(0); for (const f of fs_) f(performance.now()); } };
const withFix = (u, patch) => ({ ...cad.state.map, markers: cad.state.map.markers.map((m) => (m.unit === u ? { ...m, ...patch } : m)) });
"""


def page_state(**kw):
    """The hub's state for the page, made up: E60 and L31 at the firehouse (status placement, no fix; L31 on location at
    a made-up call), E61 responding to it with a live fix 12 s old, heading east at a realistic 10 m/s; the real lists."""
    clock = Clock()
    c = Cad(Settings(), log=lambda m: None, clock=clock)
    hq = c.firehouses[0]["name"]
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)
    for name in ("E60", "L31"):
        c.unit_station(uid(name), {"station": hq, "operator": "Lt. Example"})
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, "operator": "Lt. Example"})
    c.attach(inc["id"], {"unit_ids": [uid("L31")], "status": "OL", "operator": "Lt. Example"})
    c.attach(inc["id"], {"unit_ids": [uid("E61")], "status": "RS", "operator": "Lt. Example"})
    c._feed_position(fix(12, heading=90.0, speed=10.0))
    st = c.state()
    return {"map": st["map"], "lists": st["lists"], "static": str(STATIC), "inc": {"id": inc["id"], "lat": 41.0085, "lon": -73.66}, **kw}


def run(tmp_path, body, data):
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    h.write_text(HARNESS + LOAD + body + "\nconsole.log(JSON.stringify(out)); process.exit(0);\n")
    d.write_text(json.dumps(data))
    r = subprocess.run([NODE, str(h), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def with_now(data, age=None):
    """The fixes' times moved to this run's clock (the page ages them by its own); `age`: E61's fix this old now."""
    shift = time.time() - NOW
    for m in data["map"]["markers"]:
        if "at" in m:
            m["at"] = time.time() - age if age is not None else m["at"] + shift
    return data


M_PER_DEG_LON = 111_195 * 0.7547      # metres in a degree of longitude at 41° N


@needs_node
def test_a_plain_click_does_what_it_did_and_follow_comes_from_a_button(tmp_path):
    o = run(tmp_path, r"""
chip("E61").el.fire("click");                                // on a call, not an admin: its call opened, as before
out.e61 = [out.picked, rm.fol.id, rec.eases.length];
chip("E60").el.fire("click");                                // on no call: its box, with Follow (a click did nothing before)
const pop = host.find("cm-pop")[0];
out.box = [pop.hidden, rm.fol.id, rec.eases.length, focused && focused.textContent];
pop.byText("Follow").fire("click");
out.following = rm.fol.id; out.ease = rec.eases[0]; out.text = followText(); out.title = follow().title; out.cls = cls("E60");
out.fired = [...rec.fired]; out.zoomOpts = [...rec.zoomOpts];
rm.map.emit("dragstart", { originalEvent: { touches: [{}, {}] } }); out.pinch = rm.fol.id;
rm.map.emit("dragstart", { originalEvent: { touches: [{}] } }); out.drag = [rm.fol.id, follow().hidden, cls("E60").includes("followed")];
rm.followUnit("E60"); rm.map.emit("sizeup:recenter"); out.recenter = rm.fol.id;
rm.followUnit("E60"); follow().find("mini")[0].fire("click"); out.x = [rm.fol.id, follow().hidden];
rm.followUnit("E60"); setMap({ ...IN.map, markers: IN.map.markers.filter((m) => m.unit !== "E60") }); out.gone = [rm.fol.id, follow().hidden];
""", with_now(page_state()))
    assert o["e61"] == [o["e61"][0], "", 0] and o["e61"][0] is not None       # onPick, no follow, no zoom
    assert o["box"] == [False, "", 0, "Follow"]                                  # the box: Follow focused (Escape works)
    assert o["following"] == "E60" and o["ease"]["zoom"] >= 15
    assert o["text"] == "Following E60 · Available" and o["title"].endswith("A drag or Recenter stops following")
    assert "followed" in o["cls"] and "hl" in o["cls"] and "sizeup:follow" in o["fired"] and {"around": "center"} in o["zoomOpts"]
    assert o["pinch"] == "E60" and o["drag"] == ["", True, False]
    assert o["recenter"] == "" and o["x"] == ["", True] and o["gone"] == ["", True]


@needs_node
def test_an_admins_click_opens_the_picker_only_with_follow_in_it(tmp_path):
    o = run(tmp_path, r"""
chip("E60").el.fire("click");
const pop = host.find("cm-pop")[0];
out.picker = [pop.hidden, pop.textContent.includes("home station"), rm.fol.id, rec.eases.length];
pop.byText("Follow").fire("click");
out.after = [rm.fol.id, pop.hidden];
""", with_now(page_state(admin=True)))
    assert o["picker"] == [False, True, "", 0]                                   # no follow, no zoom from the click
    assert o["after"] == ["E60", True]


@needs_node
def test_the_wall_board_map_follows_nothing(tmp_path):
    o = run(tmp_path, r"""
const b = new CadMap.RigMap(new El("div"), { scene: { home: { center: [-73.68, 41.02], zoom: 13 }, firehouses: [] }, pins: true,
                                             editable: () => false, onPick: () => {}, follow: false });
b.update({ map: IN.map, lists: IN.lists }, null, "");
out.fol = b.fol; out.onFollow = b.layer.onFollow;
""", with_now(page_state()))
    assert o == {"fol": None, "onFollow": None}


@needs_node
def test_a_pick_never_switches_views(tmp_path):
    """A new call's draft (or an incident with no map shown) stays: no Overview opened, nothing followed."""
    o = run(tmp_path, r"""
rm.map.container.offsetParent = null;                        // no map on screen (the new-call form, an edit form)
out.follow = CadMap.followUnit("E60"); out.focus = CadMap.focusUnit("E60");
for (const f of docOn.click || []) { const c = new El("span"); c.className = "talk-chip"; c.dataset.rig = "L31"; f({ target: c }); }
out.state = [!!out.navigated, rm.fol.id, rec.eases.length];
rm.map.container.offsetParent = {};
out.shown = CadMap.followUnit("E60");
""", with_now(page_state(mode="new")))
    assert o["follow"] is False and o["focus"] is False and o["state"] == [False, "", 0]
    assert o["shown"] is True                                                    # a map already on screen: followed


@needs_node
def test_a_live_rig_moves_along_its_heading_between_fixes(tmp_path):
    """10 m/s east, a fresh fix: drawn on along its heading before the next fix comes (Motion.arc would hold it: < 30 kt)."""
    o = run(tmp_path, r"""
out.x0 = lng("E61");
run(40, 25);                                                  // a second of frames
out.x1 = lng("E61"); out.lat = chip("E61").marker.getLngLat().lat;
rm.followUnit("E61"); out.text = followText();
const e0 = rec.eases.length;
run(40, 25);
out.pans = rec.eases.slice(e0).map((e) => [e.duration, e.zoom]);
""", with_now(page_state(), age=0.5))
    moved = (o["x1"] - o["x0"]) * M_PER_DEG_LON
    assert 6 <= moved <= 16                                     # about 10 m in about a second, east
    assert abs(o["lat"] - 41.0101) < 1e-6                      # along its heading, not across it
    assert o["text"].startswith("Following E61 · Responding · ") and o["text"].endswith(" s ago")
    assert o["pans"] and all(d == 250 and z is None for d, z in o["pans"])


@needs_node
def test_a_rig_never_steps_back_stopping_or_going_stale(tmp_path):
    o = run(tmp_path, r"""
run(40, 25);
const ahead = lng("E61");
// stopped at a light: a fix with no speed a few metres behind where it is drawn: held, never eased back
const m0 = cad.state.map.markers.find((m) => m.unit === "E61");
setMap(withFix("E61", { lon: ahead - 8 / IN.mpd, speed: 0, at: Date.now() / 1000 }));
const xs = []; for (let i = 0; i < 30; i++) { run(1, 25); xs.push(lng("E61")); }
out.stop = [ahead, Math.min(...xs)];
// going stale: held where it was drawn (hollow), not snapped back to the raw fix
const real = Date.now; Date.now = () => real() + 70000;
setMap(withFix("E61", { lon: m0.lon, speed: 10, at: real() / 1000 - 5 }));
out.stale = [lng("E61"), cls("E61").includes("stale"), m0.lon];
Date.now = real;
""", dict(with_now(page_state(), age=0.5), mpd=M_PER_DEG_LON))
    ahead, lowest = o["stop"]
    assert lowest >= ahead - 1e-9
    x, stale, raw = o["stale"]
    assert stale and abs(x - ahead) < 1e-9 and x > raw


@needs_node
def test_the_frame_loop_goes_idle_when_nothing_moves(tmp_path):
    o = run(tmp_path, r"""
// a parked rig (no speed) and nothing followed
setMap(withFix("E61", { speed: 0, at: Date.now() / 1000 - 1 }));
run(200, 20);                                                 // (it eases onto the fix it was drawn ~80 m past, then settles)
out.parked = [frames.length, L.moving()];
// moving again: frames
setMap(withFix("E61", { speed: 10, heading: 90, at: Date.now() / 1000 - 0.5 }));
out.moving = [frames.length > 0, L.moving()];
// stale: settles, idle again
const real = Date.now; Date.now = () => real() + 90000;
setMap(withFix("E61", { at: real() / 1000 - 1 }));
run(200, 20);
out.stale = [frames.length, L.moving()];
Date.now = real;
""", with_now(page_state()))
    assert o["parked"] == [0, False]
    assert o["moving"] == [True, True]
    assert o["stale"] == [0, False]


@needs_node
def test_a_stale_fix_is_hollow_faded_and_aged(tmp_path):
    o = run(tmp_path, r"""
out.cls = cls("E61"); out.age = chip("E61").el.find("age").map((e) => e.textContent); out.opacity = chip("E61").el.style.opacity;
out.title = chip("E61").el.title;
rm.followUnit("E61");
out.text = followText(); out.stale = follow().classList.contains("stale");
out.e60 = [cls("E60"), chip("E60").el.find("age").length, chip("E60").el.style.opacity];
""", with_now(page_state(), age=90))
    assert "stale" in o["cls"] and "at-position" in o["cls"]
    assert o["age"] == ["2 min"] and 0.35 <= float(o["opacity"]) < 1
    assert "last fix 2 min ago (test)" in o["title"]
    assert o["text"] == "Following E61 · Responding · 2 min ago" and o["stale"] is True
    assert "stale" not in o["e60"][0] and o["e60"][1] == 0 and o["e60"][2] == ""   # status placement: as before


@needs_node
def test_the_rings_and_a_pick_takes_the_map_from_any_follower(tmp_path):
    o = run(tmp_path, r"""
out.plain = cls("L31");
talking.L31 = { rig: "L31", channel: "FG1", key: "k" };
setMap(IN.map);
out.talking = cls("L31");
CadMap.hoverUnits(["L31"]); out.hot = cls("L31");
CadMap.hoverUnits([]); out.unhot = cls("L31");
// following E60, and an aircraft-style follower on the same map: a talker chip's pick lets both go
rm.followUnit("E60");
const other = new MapFollow.Follower(rm.map, host, {}); other.id = "abc123";
for (const f of docOn.click || []) { const c = new El("span"); c.className = "talk-chip rig"; c.dataset.rig = "L31"; const i = new El("span"); c.append(i); f({ target: i }); }
out.ease = rec.eases.slice(-1)[0]; out.picked = cls("L31"); out.followers = [rm.fol.id, other.id];
const real = Date.now; Date.now = () => real() + 13000;
setMap(IN.map);
out.after = cls("L31");
Date.now = real;
out.none = CadMap.focusUnit("E99");
""", with_now(page_state()))
    assert not {"talking", "hot", "hl", "followed"} & set(o["plain"])
    assert "talking" in o["talking"] and "hot" in o["hot"] and "hot" not in o["unhot"]
    assert "hl" in o["picked"] and o["followers"] == ["", ""] and o["ease"]["zoom"] >= 15
    assert "hl" not in o["after"] and "talking" in o["after"]
    assert o["none"] is False


@needs_node
def test_no_refit_on_every_fix_or_while_following_and_the_line_starts_at_the_mark(tmp_path):
    o = run(tmp_path, r"""
setMap(cad.state.map, IN.inc);                               // E61's call open: framed once
const f0 = rec.fits;
for (let i = 1; i <= 3; i++) setMap(withFix("E61", { lon: -73.6702 + i * 0.0003, at: Date.now() / 1000 - 0.5 }), IN.inc);
out.fixes = rec.fits - f0;
run(20, 25);
const line = rm.map.sources["cm-lines"].data.features.find((f) => f.geometry.coordinates[1][0] === IN.inc.lon);
out.line = [line.geometry.coordinates[0][0], lng("E61")];
// a new rig on the call: framed again, unless one is followed
rm.followUnit("E61");
const f1 = rec.fits;
setMap({ ...cad.state.map, markers: cad.state.map.markers.map((m) => (m.unit === "E60" ? { ...m, incident_id: IN.inc.id } : m)) }, IN.inc);
out.following = rec.fits - f1;
""", with_now(page_state(), age=0.5))
    assert o["fixes"] == 0
    assert abs(o["line"][0] - o["line"][1]) < 1e-9                # from the drawn (glided) mark
    assert o["following"] == 0


@needs_node
def test_a_hovered_row_rings_on_focus_and_lets_go_when_removed(tmp_path):
    """cad.js's hoverRig / dropGoneHover, on their own (the rest of cad.js needs the page)."""
    src = (STATIC / "cad.js").read_text()
    grab = lambda name: src[src.index(f"function {name}("):src.index("\n}\n", src.index(f"function {name}(")) + 3]
    body = "let hoveredRow = null;\n" + grab("hoverRig") + grab("dropGoneHover") + r"""
const calls = [];
global.CadMap = { hoverUnits: (x) => calls.push(x) };
const li = new El("li"); li._rig = "E60";
const btn = new El("button"); li.append(btn);
hoverRig(li);
li.fire("focusin");
li.fire("focusout", { relatedTarget: btn });               // focus moved inside the row: still ringed
li.fire("mouseenter");
li.isConnected = false;                                     // the row redrawn away under the pointer
dropGoneHover();
dropGoneHover();
out.calls = calls;
"""
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    h.write_text(HARNESS + body + "\nconsole.log(JSON.stringify(out));\n")
    d.write_text("{}")
    r = subprocess.run([NODE, str(h), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["calls"] == [["E60"], ["E60"], []]


def test_the_rows_and_chips_offer_it():
    cad = (STATIC / "cad.js").read_text()
    units = cad[cad.index("function renderUnits()"):cad.index("function renderUnitLog()")]
    on = cad[cad.index("function renderOnUnits("):cad.index("function playForEvent(")]
    for part in (units, on):
        assert "followBtn(" in part and "hoverRig(li)" in part and "onMap(" in part and "dropGoneHover();" in part
    assert 'b.classList.add("nomap")' in cad                                         # no map on screen: the button flashes
    assert 'el("button", "mini quiet u-follow")' in cad and 'Icon("locate")' in cad and "CadMap.followUnit(name)" in cad
    assert "c.dataset.rig = e.rig.split" in (STATIC / "radio.js").read_text()
    assert "c.dataset.rig = e.rig.split" in (STATIC / "cadmap.js").read_text()      # the Overview's talker chips
    html = (STATIC / "cad.html").read_text()
    assert (html.index('src="/static/glide.js"') < html.index('src="/static/mapfollow.js"')
            < html.index('src="/static/aircraft.js"') < html.index('src="/static/cadmap.js"'))
    css = (STATIC / "cadmap.css").read_text() + (STATIC / "cad.css").read_text()
    for c in (".cm-rig.followed", ".cm-rig.hl", ".cm-rig.hot", ".cm-rig.at-position.stale", ".cm-rig .age", ".cm-follow", ".u-follow"):
        assert c in css, c


def test_aircraft_and_units_share_one_follow_helper():
    shared = (STATIC / "mapfollow.js").read_text()
    assert "class Follower" in shared and "class Lights" in shared
    for f in ("aircraft.js", "cadmap.js"):
        js = (STATIC / f).read_text()
        assert "new MapFollow.Follower(" in js and "MapFollow.Lights(" in js, f
        assert "z.enable({ around: \"center\" })" not in js, f                     # the follow itself lives in one place
