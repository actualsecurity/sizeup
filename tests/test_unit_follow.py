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
            "incident_number", "line_to", "station", "home_line"}
    for role, inc in (("free", None), ("dispatched", INC), ("enroute", INC), ("onscene", INC), ("oos", None)):
        m = place(E60, role, HQ, inc, None, NOW)
        assert set(m) == keys, role
    assert place(E60, "onscene", HQ, INC, None, NOW)["place"] == "incident"
    assert place(E60, "free", HQ, None, None, NOW)["place"] == "station"
    s = mapview.map_slice([dict(E60, station=HQ["name"])], [], {"AV": "free"}.get, [HQ], positions={}, now=NOW)
    assert [(m["unit"], m["place"]) for m in s["markers"]] == [("E60", "station")]
    assert s["avl"] == {"stale_s": mapview.POSITION_STALE_S, "max_age_s": mapview.POSITION_MAX_AGE_S}


def test_heading_speed_and_source_are_kept_only_when_good():
    base = {"lat": 41.01, "lon": -73.67, "at": NOW}
    assert mapview.fresh_position(base, NOW) == base                                             # as before: no extras
    good = mapview.fresh_position(dict(base, heading=359.5, speed=0.0, source=" vendor "), NOW)
    assert good == dict(base, heading=359.5, speed=0.0, source="vendor")
    bad = mapview.fresh_position(dict(base, heading=-5, speed="fast", source=""), NOW)
    assert bad == base                                                                           # the fix kept, not those
    for b in (dict(base, lat=float("nan")), dict(base, at=True), dict(base, lon=200)):
        assert mapview.fresh_position(b, NOW) is None


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
    assert st["map"]["avl"] == {"stale_s": 60.0, "max_age_s": 300.0}
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
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.on = {}; this.attrs = {};
                     this.className = ""; this._t = ""; this.hidden = false; this.title = ""; this.classList = new CL(this);
                     this.offsetParent = {}; this.clientWidth = 900; this.clientHeight = 600;
                     const st = {}; this.style = new Proxy(st, { get: (o, k) => (k === "setProperty" ? (a, b) => { o[a] = b; } : o[k]), set: (o, k, v) => { o[k] = v; return true; } }); }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  append(...xs) { for (const x of xs) { this.children.push(x); if (x && typeof x === "object") x.parent = this; } }
  replaceChildren(...xs) { this._t = ""; this.children = [...xs]; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  fire(t, ev) { for (const f of this.on[t] || []) f({ stopPropagation() {}, preventDefault() {}, ...(ev || {}) }); }
  remove() {}
  focus() {}
  scrollIntoView() { this.scrolled = true; }
  all() { const r = [this]; for (const c of this.children) if (c && c.all) r.push(...c.all()); return r; }
  find(cls) { return this.all().filter((e) => e.classList.contains(cls)); }
  closest(sel) { const m = /^\.([\w-]+)\[data-([\w-]+)\]$/.exec(sel); for (let e = this; e; e = e.parent) if (e.classList && e.classList.contains(m[1]) && e.dataset[m[2]]) return e; return null; }
}
const docOn = {};
global.document = { createElement: (t) => new El(t), addEventListener: (t, f) => { (docOn[t] = docOn[t] || []).push(f); },
                    visibilityState: "visible", getElementById: () => null, documentElement: new El("html") };
global.Icon = (n) => { const e = new El("i"); e.className = `ico i-${n}`; return e; };
const rec = { eases: [], fired: [], zoomOpts: [], moving: false };
const markers = [];
class Marker {
  constructor(o) { this.el = o.element; this.ll = null; this.off = [0, 0]; this.gone = false; markers.push(this); }
  setLngLat(ll) { this.ll = Array.isArray(ll) ? { lng: ll[0], lat: ll[1] } : ll; return this; }
  getLngLat() { return this.ll; }
  setOffset(o) { this.off = o; return this; }
  addTo() { return this; }
  remove() { this.gone = true; }
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
  jumpTo(o) { this.center = { lng: o.center[0], lat: o.center[1] }; } fitBounds() {}
}
global.maplibregl = { Map: FakeMap, Marker, NavigationControl: class {}, LngLatBounds: class { constructor(a) { this.a = a; } extend() {} getCenter() { return this.a; } getEast() { return 0; } getWest() { return 0; } getNorth() { return 0; } getSouth() { return 0; } } };
global.buildMapStyle = () => ({});
global.firehouseIcon = () => ({});
global.talking = {};
global.Radio = { talkingFor: (n) => global.talking[n] || null };
global.cad = { state: { map: IN.map, lists: IN.lists }, skew: 0 };
"""

LOAD = r"""
for (const f of ["motion.js", "glide.js", "mapfollow.js", "cadmap.js"]) eval(fs.readFileSync(IN.static + "/" + f, "utf8"));
const host = new El("div");
const rm = new CadMap.RigMap(host, { scene: { home: { center: [-73.68, 41.02], zoom: 13 }, firehouses: [] }, pins: true, unset: false,
                                     editable: () => false, onPick: (id) => { out.picked = id; } });
const L = rm.layer;
rm.update({ map: IN.map, lists: IN.lists }, null, "");
const chip = (u) => L.chips.get(u);
const cls = (u) => chip(u).el.className.split(/\s+/).filter((c) => !c.startsWith("maplibregl-"));
const follow = () => host.find("cm-follow")[0];
const followText = () => follow().find("ac-follow-t")[0].textContent;
const spin = (ms) => { const t = Date.now(); while (Date.now() - t < ms) {} };
"""


def page_state():
    """The hub's state for the page, made up: E60 and L31 at the firehouse (status placement, no fix), E61 responding
    to a made-up call with a live fix 12 s old, heading east at 15 m/s; the real lists (status names and colours)."""
    clock = Clock()
    c = Cad(Settings(), log=lambda m: None, clock=clock)
    hq = c.firehouses[0]["name"]
    for name in ("E60", "L31"):
        c.unit_station(next(u["id"] for u in c.store.units() if u["name"] == name), {"station": hq, "operator": "Lt. Example"})
    c.unit_status(next(u["id"] for u in c.store.units() if u["name"] == "E61"), {"status": "RS", "operator": "Lt. Example"})
    c._feed_position(fix(12, heading=90.0, speed=15.0))
    st = c.state()
    return {"map": st["map"], "lists": st["lists"], "static": str(STATIC)}


def run(tmp_path, body, data):
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    h.write_text(HARNESS + LOAD + body + "\nconsole.log(JSON.stringify(out)); process.exit(0);\n")
    d.write_text(json.dumps(data))
    r = subprocess.run([NODE, str(h), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def with_now(data):
    """The fixes' times moved to this run's clock (the page ages them by its own)."""
    shift = time.time() - NOW
    for m in data["map"]["markers"]:
        if "at" in m:
            m["at"] += shift
    return data


@needs_node
def test_a_clicked_rig_is_followed_until_a_drag_recenter_x_or_it_leaves_the_map(tmp_path):
    o = run(tmp_path, r"""
const st = chip("E60").marker.getLngLat();
chip("E60").el.fire("click");
out.ease = rec.eases[0]; out.fired = [...rec.fired]; out.zoomOpts = [...rec.zoomOpts];
out.following = rm.fol.id; out.text = followText(); out.title = follow().title; out.cls = cls("E60");
out.station = [st.lng, st.lat]; out.info = host.find("cm-pop")[0].hidden;
// a two-finger pinch keeps it; a one-finger drag lets go
rm.map.emit("dragstart", { originalEvent: { touches: [{}, {}] } }); out.pinch = rm.fol.id;
rm.map.emit("dragstart", { originalEvent: { touches: [{}] } }); out.drag = [rm.fol.id, follow().hidden, cls("E60").includes("followed")];
CadMap.followUnit("E60"); rm.map.emit("sizeup:recenter"); out.recenter = rm.fol.id;
CadMap.followUnit("E60"); follow().find("mini")[0].fire("click"); out.x = [rm.fol.id, follow().hidden];
// an aircraft followed on the same map (mapfollow.js fires sizeup:follow with its follower): this one lets go
CadMap.followUnit("E60"); rm.map.fire("sizeup:follow", { follower: {} }); out.aircraft = rm.fol.id;
// the rig no longer on the map
CadMap.followUnit("E60");
rm.update({ map: { ...IN.map, markers: IN.map.markers.filter((m) => m.unit !== "E60") }, lists: IN.lists }, null, "");
out.gone = [rm.fol.id, follow().hidden];
""", with_now(page_state()))
    assert o["ease"]["zoom"] >= 15 and o["ease"]["center"] == o["station"]          # at the firehouse (status placement)
    assert "sizeup:follow" in o["fired"] and {"around": "center"} in o["zoomOpts"]
    assert o["following"] == "E60" and o["text"] == "Following E60 · Available"     # no fix: no age
    assert o["title"].startswith("E60, Available") and o["title"].endswith("A drag or Recenter stops following")
    assert "followed" in o["cls"] and "hl" in o["cls"]                              # ringed, and picked for a while
    assert o["info"] is False                                                       # its box (its call one click away)
    assert o["pinch"] == "E60" and o["drag"] == ["", True, False]
    assert o["recenter"] == "" and o["x"] == ["", True] and o["aircraft"] == "" and o["gone"] == ["", True]


@needs_node
def test_a_live_fix_is_followed_with_its_age_and_glides_onto_the_next(tmp_path):
    o = run(tmp_path, r"""
const m0 = IN.map.markers.find((m) => m.unit === "E61");
out.at0 = chip("E61").marker.getLngLat(); out.cls = cls("E61");
CadMap.followUnit("E61");
out.text = followText();
// the next fix, 150 m east: the mark drawn on toward it over the frames, never jumped there
const next = { ...m0, lon: m0.lon + 0.0018, at: m0.at + 10 };
rm.update({ map: { ...IN.map, markers: IN.map.markers.map((m) => (m.unit === "E61" ? next : m)) }, lists: IN.lists }, null, "");
const xs = [];
const e0 = rec.eases.length;
for (let i = 0; i < 40; i++) { spin(25); L.tick(false); xs.push(chip("E61").marker.getLngLat().lng); }
out.xs = xs; out.next = next.lon; out.pans = rec.eases.slice(e0).map((e) => [e.center, e.duration, e.zoom]);
""", with_now(page_state()))
    m = next(x for x in page_state()["map"]["markers"] if x["unit"] == "E61")
    assert abs(o["at0"]["lng"] - m["lon"]) < 0.001 and abs(o["at0"]["lat"] - m["lat"]) < 1e-4    # at its fix (drawn on a little)
    assert "at-position" in o["cls"] and "stale" not in o["cls"]
    assert o["text"] in ("Following E61 · Responding · 12 s ago", "Following E61 · Responding · 13 s ago")
    xs = o["xs"]
    assert xs[0] < o["next"] - 0.0005                                 # not jumped onto the new fix
    assert all(b >= a - 1e-9 for a, b in zip(xs, xs[1:]))            # never backwards
    assert xs[-1] > xs[0] + 0.0003, (xs[:3], xs[-3:])                     # moved on toward it
    assert o["pans"] and all(d == 250 and z is None for _, d, z in o["pans"])     # kept centred: eased, never zoomed


@needs_node
def test_a_stale_fix_is_hollow_faded_and_aged(tmp_path):
    data = with_now(page_state())
    for m in data["map"]["markers"]:
        if m["unit"] == "E61":
            m["at"] -= 78                                             # 90 s old: past the hub's 60 s, inside its 300 s
    o = run(tmp_path, r"""
out.cls = cls("E61"); out.age = chip("E61").el.find("age").map((e) => e.textContent); out.opacity = chip("E61").el.style.opacity;
out.title = chip("E61").el.title;
CadMap.followUnit("E61");
out.text = followText(); out.stale = follow().classList.contains("stale");
out.e60 = [cls("E60"), chip("E60").el.find("age").length, chip("E60").el.style.opacity];
""", data)
    assert "stale" in o["cls"] and "at-position" in o["cls"]
    assert o["age"] == ["2 min"] and 0.35 <= float(o["opacity"]) < 1
    assert "last fix 2 min ago (test)" in o["title"]
    assert o["text"] == "Following E61 · Responding · 2 min ago" and o["stale"] is True
    assert "stale" not in o["e60"][0] and o["e60"][1] == 0 and o["e60"][2] == ""   # status placement: as before


@needs_node
def test_the_rings_transmitting_picked_and_hovered(tmp_path):
    o = run(tmp_path, r"""
out.plain = cls("L31");
talking.L31 = { rig: "L31", channel: "FG1", key: "k" };
rm.update({ map: IN.map, lists: IN.lists }, null, "");
out.talking = cls("L31");
CadMap.hoverUnits(["L31"]); out.hot = cls("L31");
CadMap.hoverUnits([]); out.unhot = cls("L31");
// a talker chip (radio.js's strip): centred and ringed, not followed
const strip = new El("span"); strip.className = "talk-chip rig"; strip.dataset.rig = "L31";
const inner = new El("span"); strip.append(inner);
for (const f of docOn.click || []) f({ target: inner });
out.ease = rec.eases.slice(-1)[0]; out.picked = cls("L31"); out.following = rm.fol.id;
const real = Date.now; Date.now = () => real() + 13000;        // HIGHLIGHT_MS on
rm.update({ map: IN.map, lists: IN.lists }, null, "");
out.after = cls("L31");
Date.now = real;
// a rig not on any map: nothing
out.none = CadMap.focusUnit("E99");
""", with_now(page_state()))
    assert not {"talking", "hot", "hl", "followed"} & set(o["plain"])
    assert "talking" in o["talking"] and "hot" in o["hot"] and "hot" not in o["unhot"]
    assert "hl" in o["picked"] and o["following"] == "" and o["ease"]["zoom"] >= 15
    assert "hl" not in o["after"] and "talking" in o["after"]
    assert o["none"] is False


def test_the_rows_and_chips_offer_it():
    cad = (STATIC / "cad.js").read_text()
    units = cad[cad.index("function renderUnits()"):cad.index("function renderUnitLog()")]
    on = cad[cad.index("function renderOnUnits("):cad.index("function playForEvent(")]
    for part in (units, on):
        assert "followBtn(" in part and "hoverRig(li)" in part and "onMap(" in part
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
