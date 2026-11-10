"""An alert's aircraft on the map, and following an aircraft (aircraft.js, airport-alerts.js, cadmap.js).

The user: click a plane on the map to zoom to it and follow it (kept centred as it moves; a "Following <tail>" chip
with ✕; a drag or Recenter stops it). An airport pre-alert that names an aircraft (HPN ATC matched to ADS-B, or an
ADS-B emergency) rings that aircraft on the map in the alert's colour, above the rest; hovering its card rings it
wider; clicking the card's aircraft line follows it; not on the map: its last fix, faded, with the fix's age. Gone
with the alert (accepted, dismissed, expired); every aircraft of every open alert is ringed.

  the API      each pre-alert's aircraft carry their ICAO address ("hex": the map's key) and their fix time ("pos_at"), from the Aircraft layer's real answer (tests/fixtures/aircraft: fictional
               tails, recorded positions)
  the card     airport-alerts.js in node with a minimal DOM: the aircraft line is a button that follows it; the card's
               hover rings it wider; the column hands the open alerts to the map
  the map      aircraft.js in node with a stand-in MapLibre map, fed the hub's answer from the same fixtures: the
               ringed source, the last fix, follow (eased at most 4 times a second, no fetch), and what stops it"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import sizeup.app as app_mod
from sizeup import atc
from sizeup.app import App
from sizeup.cad.service import Cad, Settings
from tests.test_aircraft import Clock, both, layer
from tests.test_adsb_receiver import PreWatch, squawk

NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def hexes(row):
    """The aircraft a pre-alert names, by ICAO address, as the page reads them (airport-alerts.js, aircraft.js)."""
    return [x["hex"] for x in row["data"].get("aircraft") or [] if x.get("hex")]


def hub_answer():
    """The Aircraft layer's real answer (the two recorded adsb.lol reads), and the layer."""
    clock = Clock()
    ly = layer(clock)
    return both(ly, clock), ly, clock


def atc_planes(ly, clock, monkeypatch):
    """What HPN ATC matches callsigns against: App._atc_aircraft over that layer (its clock, not the wall's)."""
    monkeypatch.setattr(app_mod.time, "time", lambda: clock.t)
    return App._atc_aircraft(SimpleNamespace(aircraft=ly))


# ---- the API ---------------------------------------------------------------------------------------------------------


def test_the_aircraft_atc_matches_against_carry_their_fix_time(monkeypatch):
    d, ly, clock = hub_answer()
    planes = atc_planes(ly, clock, monkeypatch)
    assert planes and len(planes) >= len(d["aircraft"])
    for p in planes:
        assert p["pos_at"] == ly.st["at"] - p["age"]
    assert all(p["pos_at"] <= ly.st["at"] for p in planes)


def test_an_atc_alert_names_its_aircraft_by_icao_address_and_its_fix(monkeypatch):
    d, ly, clock = hub_answer()
    planes = atc_planes(ly, clock, monkeypatch)
    sr22 = next(p for p in planes if p.get("cs") == "N47DQ")               # the SR22 on the ILS 34 (fictional tail)
    cad = Cad(Settings(), log=lambda m: None, clock=clock)
    w = atc.AtcWatch(cad.airport_alerts, ["HPN Tower"], aircraft=lambda: planes, log=lambda m: None, clock=clock)
    a = w.heard({"id": "HPNTower-1", "source": "HPN Tower", "start": clock.t - 3, "end": clock.t,
                 "text": "Westchester tower, November four seven delta quebec, mayday mayday mayday, engine failure"})
    assert a and a["data"]["aircraft"][0]["hex"] == sr22["hex"]
    (row,) = cad.state()["airport_alerts"]["open"]
    assert hexes(row) == [sr22["hex"]]                                     # the map's key
    ac = row["data"]["aircraft"][0]
    assert ac["pos_at"] == sr22["pos_at"] and (ac["lat"], ac["lon"]) == (sr22["lat"], sr22["lon"])
    json.dumps(cad.state())                                                # as the state push sends it
    # gone from the open list (so from the map) once dismissed
    cad.airport_alert(row["id"], "dismiss", {"operator": "Lt. Example"})
    assert cad.state()["airport_alerts"]["open"] == []


def test_heard_but_not_on_adsb_names_no_aircraft_key(monkeypatch):
    d, ly, clock = hub_answer()
    cad = Cad(Settings(), log=lambda m: None, clock=clock)
    w = atc.AtcWatch(cad.airport_alerts, ["HPN Tower"], aircraft=lambda: [], log=lambda m: None, clock=clock)
    w.heard({"id": "HPNTower-1", "source": "HPN Tower", "start": clock.t - 3, "end": clock.t,
             "text": "Westchester tower, November one two three alfa bravo, mayday mayday mayday, engine failure"})
    (row,) = cad.state()["airport_alerts"]["open"]
    assert hexes(row) == [] and row["data"]["aircraft"][0]["hex"] is None


def test_an_adsb_emergency_prealert_names_its_aircraft_and_its_fix(tmp_path):
    w = PreWatch(tmp_path)
    for _ in range(4):
        w.feed(squawk())
    (p,) = w.items()
    assert hexes(p) == ["a5bf1b"]
    ac = p["data"]["aircraft"][0]
    assert ac["hex"] == "a5bf1b" and isinstance(ac["pos_at"], float) and ac["pos_at"] <= w.clock.t


# ---- the page, in node -------------------------------------------------------------------------------------------------

DOM = r"""
const fs = require("fs");
global.window = global;
const IN = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = {};
class CL {
  constructor(e) { this.e = e; }
  _s() { return new Set((this.e.className || "").split(/\s+/).filter(Boolean)); }
  add(c) { const s = this._s(); s.add(c); this.e.className = [...s].join(" "); }
  remove(c) { const s = this._s(); s.delete(c); this.e.className = [...s].join(" "); }
  contains(c) { return this._s().has(c); }
  toggle(c, on) { if (on === undefined ? !this.contains(c) : on) this.add(c); else this.remove(c); }
}
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.style = {}; this.on = {};
                     this.attrs = {}; this.className = ""; this._t = ""; this.hidden = false; this.title = ""; this.classList = new CL(this);
                     this.offsetParent = {}; this.clientWidth = 900; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  append(...xs) { for (const x of xs) { this.children.push(x); if (x && typeof x === "object") x.parent = this; } }
  replaceChildren(...xs) { this.children = [...xs]; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  fire(t, ev) { for (const f of this.on[t] || []) f({ stopPropagation() {}, preventDefault() {}, ...(ev || {}) }); }
  contains(x) { return x === this || this.children.some((c) => c && c.contains && c.contains(x)); }
  scrollIntoView() { this.scrolled = true; }
  blur() { for (let e = this; e; e = e.parent) e.fire("focusout", { relatedTarget: null }); }   // focusout bubbles
  getContext() { return new Proxy({}, { get: (t, k) => (k === "getImageData" ? () => ({}) : () => {}), set: () => true }); }
  all() { const r = [this]; for (const c of this.children) if (c && c.all) r.push(...c.all()); return r; }
  find(cls) { return this.all().filter((e) => e.classList.contains(cls)); }
}
global.document = { createElement: (t) => new El(t), addEventListener() {}, visibilityState: "visible",
                    getElementById: () => null, documentElement: new El("html") };
global.UI = { toast() {} };
global.addEventListener = () => {};
"""


def run_node(tmp_path, scripts, body, data=None, pre=""):
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    src = DOM + pre + "".join(f'eval(fs.readFileSync({json.dumps(str(STATIC / s))}, "utf8"));\n' for s in scripts)
    h.write_text(src + body + "\nconsole.log(JSON.stringify(out));\n")
    d.write_text(json.dumps(data or {}))
    r = subprocess.run([NODE, str(h), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])     # (an async body prints its own, last)


def open_alerts(monkeypatch, tmp_path):
    """An ATC pre-alert naming the SR22 (as the state push carries it), and an ADS-B emergency one."""
    d, ly, clock = hub_answer()
    planes = atc_planes(ly, clock, monkeypatch)
    cad = Cad(Settings(), log=lambda m: None, clock=clock)
    w = atc.AtcWatch(cad.airport_alerts, ["HPN Tower"], aircraft=lambda: planes, log=lambda m: None, clock=clock)
    w.heard({"id": "HPNTower-1", "source": "HPN Tower", "start": clock.t - 3, "end": clock.t,
             "text": "Westchester tower, November four seven delta quebec, mayday mayday mayday, engine failure"})
    pw = PreWatch(tmp_path)
    for _ in range(4):
        pw.feed(squawk(hex_="a0bead"))
    return d, cad.state()["airport_alerts"]["open"] + pw.items()


@needs_node
def test_the_card_links_its_aircraft_to_the_map(tmp_path, monkeypatch):
    d, items = open_alerts(monkeypatch, tmp_path)
    o = run_node(tmp_path, ["airport-alerts.js"], r"""
const calls = [];
window.AircraftLayer = { alerts: (xs) => calls.push(["alerts", xs.map((a) => a.data.aircraft.map((x) => x.hex).filter(Boolean))]), hover: (h) => calls.push(["hover", h]),
                         follow: (h) => calls.push(["follow", h]) };
const list = new El("ol");
AirportAlerts.column(list, { open: IN.items }, { api() {}, operator: () => "", time: () => "12:00", readonly: true, alarms: null });
out.cards = list.children.map((li) => ({ hex: li.dataset.hex, follow: li.find("aa-follow").map((b) => [b.tagName, b.dataset.hex, b.textContent, b.title]),
                                         plain: li.find("aa-acft").filter((e) => !e.classList.contains("aa-follow")).length }));
const li = list.children[0];
li.fire("mouseenter");
li.find("aa-follow")[0].fire("click");
li.fire("mouseleave");
list.children[1].find("aa-follow")[0].fire("click");
// accepted / dismissed / expired: no longer open, so the map is handed none
AirportAlerts.column(list, { open: [] }, { api() {}, operator: () => "", time: () => "", readonly: true });
out.calls = calls;
""", {"items": items})
    atc_hex = hexes(items[0])[0]
    assert o["cards"][0]["hex"] == atc_hex
    (tag, hx, text, title), = o["cards"][0]["follow"]
    assert tag == "BUTTON" and hx == atc_hex and text.startswith("N47DQ (SR22)") and "ADS-B" in text and "not the alert's confidence" in title and title.endswith("Click: follow it on the map")
    assert o["cards"][1]["hex"] == "a0bead" and o["cards"][1]["follow"][0][1] == "a0bead"   # the emergency's Aircraft line
    # (the follow click takes the focus off its line: the ring stays wide only while the pointer is on the card)
    assert o["calls"] == [["alerts", [[atc_hex], ["a0bead"]]], ["hover", [atc_hex]], ["follow", atc_hex], ["hover", [atc_hex]],
                          ["hover", []], ["follow", "a0bead"], ["hover", []], ["alerts", []]]


@needs_node
def test_a_card_for_an_aircraft_not_on_adsb_has_no_link(tmp_path):
    item = {"id": 7, "level": "medium", "title": "HPN ATC: MAYDAY: N123AB", "source": "atc", "source_name": "HPN ATC",
            "confidence": 0.6, "at": 1.0, "count": 1,
            "data": {"aircraft": [{"callsign": "N123AB", "hex": None, "spoken": "N123AB", "match": 0.0}]}}
    o = run_node(tmp_path, ["airport-alerts.js"], r"""
window.AircraftLayer = { alerts() {}, hover() {}, follow() {} };
const list = new El("ol");
AirportAlerts.column(list, { open: IN.items }, { api() {}, operator: () => "", time: () => "", readonly: true });
const li = list.children[0];
out.hex = li.dataset.hex || null; out.follow = li.find("aa-follow").length; out.line = li.find("aa-acft").map((e) => e.textContent);
""", {"items": [item]})
    assert o == {"hex": None, "follow": 0, "line": ["N123AB · heard, not on ADS-B"]}


MAP = r"""
const rec = { fetches: 0, sets: {}, jumps: [], eases: [], fired: [], zoomOpts: [], info: [], zooming: false };
global.fetch = async () => { rec.fetches++; return { ok: true, json: async () => IN.d }; };
function fakeMap() {
  const on = {}, sources = {}, layers = {};
  let center = { lng: -73.7, lat: 41.07 }, zoom = 10;
  const zh = () => ({ isEnabled: () => true, enable: (o) => rec.zoomOpts.push(o === undefined ? null : o) });
  const m = {
    on(t, a, b) { const k = b ? `${t}:${a}` : t; (on[k] = on[k] || []).push(b || a); },
    once() {}, fire(t) { rec.fired.push(t); for (const f of on[t] || []) f({}); },
    emit(t, e) { for (const f of on[t] || []) f(e || {}); },
    getSource: (id) => sources[id], addSource(id) { sources[id] = { setData: (d) => { (rec.sets[id] = rec.sets[id] || []).push(d); } }; },
    addLayer(l) { layers[l.id] = l; }, getLayer: (id) => layers[id], setLayoutProperty() {}, hasImage: () => false, addImage() {},
    getStyle: () => ({ glyphs: "x", layers: [] }), isStyleLoaded: () => true, loaded: () => true,
    getCanvas: () => m.canvas, canvas: { style: {}, title: "" }, getContainer: () => m.container, container: new El("div"),
    getCenter: () => center, getZoom: () => zoom, isMoving: () => rec.zooming, isZooming: () => rec.zooming,
    project: (ll) => { const [lng, lat] = Array.isArray(ll) ? ll : [ll.lng, ll.lat]; return { x: lng * 1e5, y: -lat * 1e5 }; },
    jumpTo(o) { rec.jumps.push(o.center); center = { lng: o.center[0], lat: o.center[1] }; },
    easeTo(o) { rec.eases.push(o); center = { lng: o.center[0], lat: o.center[1] }; zoom = o.zoom; },
    queryRenderedFeatures: () => [], scrollZoom: zh(), touchZoomRotate: zh(),
  };
  return m;
}
let group = null;
window.MapLayers = { addGroup: (g) => { group = g; } };
const host = new El("div");
const control = { map: fakeMap(), host, profile: IN.profile || "overview", on: (k, d) => !!IN.layerOn, setLayer() {},
                  panel: { hidden: true }, renderPanel() {}, close() {}, showInfo: (...a) => rec.info.push([a[0], a[1]]) };
"""

LOADED = r"""
const m = group.attach(control);
const flush = () => new Promise((r) => setTimeout(r, 0));
const hl = () => { const s = rec.sets["ac-hl"] || []; return s.length ? s[s.length - 1].features.map((f) => f.properties) : []; };
const main = () => { const s = rec.sets["ac-aircraft"] || []; return s.length ? s[s.length - 1].features.filter((f) => f.geometry.type === "Point").map((f) => f.properties.hex) : []; };
"""


def map_input(d, **kw):
    d = dict(d)
    d.pop("fetched", None)          # ages as recorded (the page adds the time since "fetched": not this test's clock)
    return {"d": d, **kw}


@needs_node
def test_an_alerts_aircraft_is_ringed_above_the_rest_in_its_colour_and_cleared_with_it(tmp_path, monkeypatch):
    d, items = open_alerts(monkeypatch, tmp_path)
    atc_hex = hexes(items[0])[0]
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  out.onBefore = m.isOn();                                   // the viewer has the layer off
  await AircraftLayer.load();
  AircraftLayer.alerts(IN.items);
  out.onWithAlert = m.isOn();                                // the Overview's map: on while an alert names an aircraft
  await flush(); m.draw(true);
  out.ringed = hl(); out.main = main();
  AircraftLayer.hover([IN.atc]);
  out.hovered = hl().find((p) => p.hex === IN.atc);
  AircraftLayer.hover([]);
  out.after = hl().find((p) => p.hex === IN.atc);
  AircraftLayer.alerts([]);                                  // accepted, dismissed or expired
  out.onAfter = m.isOn();
  control.on = () => true;                                   // (the viewer's own layer on: the marks drawn plain)
  m.draw(true);
  out.cleared = hl(); out.mainAfter = main().includes(IN.atc);
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(d, items=items, atc=atc_hex), pre=MAP)
    assert o["onBefore"] is False and o["onWithAlert"] is True and o["onAfter"] is False
    by = {p["hex"]: p for p in o["ringed"]}
    assert set(by) == {atc_hex, "a0bead"}                       # every open alert's aircraft
    assert atc_hex not in o["main"]                              # drawn once, in the ringed source (above the rest)
    r = by[atc_hex]
    assert r["ringc"] == "#EE6475" and r["color"] == "#EE6475" and r["ringw"] == 3 and r["glow"] > 0 and r["cs"] == "N47DQ"
    lost = by["a0bead"]                                          # the emergency's aircraft isn't in this answer: its last fix
    assert lost["icon"].endswith("-old") and lost["opacity"] < 1 and lost["detail"].startswith("last fix ")
    assert o["hovered"]["ringw"] == 5 and o["hovered"]["rr"] > r["rr"] and o["hovered"]["glow"] > r["glow"]
    assert o["after"]["ringw"] == 3
    assert o["cleared"] == [] and o["mainAfter"] is True


@needs_node
def test_a_clicked_aircraft_is_followed_until_a_drag_or_recenter(tmp_path):
    d, ly, clock = hub_answer()
    moving = next(a for a in d["aircraft"] if a["alt"] != "ground" and (a["gs"] or 0) > 60 and a["age"] < 5)   # moving on screen
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  await AircraftLayer.load();
  await flush();
  const hex = IN.hex;
  control.map.queryRenderedFeatures = () => [{ properties: { hex } }];
  m.clicked({ point: [0, 0], originalEvent: { stopPropagation() {} } });
  await flush();
  out.ease = rec.eases[0]; out.fired = [...rec.fired]; out.zoomOpts = [...rec.zoomOpts]; out.info = rec.info[0];
  const chip = host.find("ac-follow")[0];
  out.chip = [chip.find("ac-follow-t")[0].textContent, chip.hidden, chip.title];
  out.ringed = hl().map((p) => [p.hex, p.ringc]);
  // frames every 20 ms for 1.6 s: the map eased onto the mark at most every 250 ms (after the 600 ms zoom-in),
  // over 250 ms, and nothing fetched for it
  const f0 = rec.fetches, e0 = rec.eases.length, t0 = performance.now();
  while (performance.now() - t0 < 1600) { const t = performance.now(); while (performance.now() - t < 20) {} m.draw(); }
  out.pans = rec.eases.slice(e0); out.fetches = rec.fetches - f0;
  out.at = hl().find((p) => p.hex === hex) ? true : false;
  // never over a zoom (a wheel, a pinch)
  rec.zooming = true; const z0 = rec.eases.length; const t2 = performance.now();
  while (performance.now() - t2 < 600) { const t = performance.now(); while (performance.now() - t < 20) {} m.draw(); }
  out.overZoom = rec.eases.length - z0; rec.zooming = false;
  // a pinch (two fingers) keeps following; a one-finger drag lets it go: the chip hidden, zooming round the pointer again
  control.map.emit("dragstart", { originalEvent: { touches: [{}, {}] } });
  out.afterPinch = m.following;
  control.map.emit("dragstart", { originalEvent: { touches: [{}] } });
  out.afterDrag = [m.following, chip.hidden, rec.zoomOpts.slice(-1)[0]];
  // follow again, then Recenter (cadmap.js fires sizeup:recenter) lets it go too
  m.follow(hex);
  control.map.emit("sizeup:recenter");
  out.afterRecenter = m.following;
  m.follow(hex);
  chip.find("mini")[0].fire("click");                        // the chip's ✕
  out.afterX = [m.following, chip.hidden];
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(d, hex=moving["hex"], layerOn=True), pre=MAP)
    assert o["ease"]["zoom"] >= 12 and o["ease"]["duration"] > 0
    assert abs(o["ease"]["center"][0] - moving["lon"]) < 0.05 and abs(o["ease"]["center"][1] - moving["lat"]) < 0.05
    assert "sizeup:follow" in o["fired"] and {"around": "center"} in o["zoomOpts"]
    assert o["info"][0] == (moving.get("cs") or moving["hex"].upper())       # its box opens as before
    text, hidden, title = o["chip"]
    assert not hidden and text == f"Following {o['info'][0]}" and "Stop following" not in text
    assert title.startswith(o["info"][0]) and " kt" in title and "A drag or Recenter stops following" in title
    assert o["ringed"] == [[moving["hex"], "#E6EDF3"]]                       # followed (no alert): a plain ring
    assert o["fetches"] == 0 and o["at"]
    assert 2 <= len(o["pans"]) <= 5                                          # ~1 s of panning, at most 4 a second
    assert all(p["duration"] == 250 and "zoom" not in p for p in o["pans"])
    lon0, lat0 = o["pans"][0]["center"]
    assert abs(lon0 - moving["lon"]) < 0.05 and abs(lat0 - moving["lat"]) < 0.05
    assert o["overZoom"] == 0 and o["afterPinch"] == moving["hex"]
    assert o["afterDrag"] == ["", True, None]
    assert o["afterRecenter"] == "" and o["afterX"] == ["", True]


@needs_node
def test_following_an_alerts_aircraft_from_its_card_with_only_its_last_fix(tmp_path, monkeypatch):
    d, items = open_alerts(monkeypatch, tmp_path)
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  await AircraftLayer.load();
  AircraftLayer.alerts(IN.items);
  await flush();
  AircraftLayer.follow("a0bead");                           // the card's aircraft line; not in the answer
  await flush();
  out.ease = rec.eases[0]; out.following = m.following; out.scrolled = !!control.map.container.scrolled;
  out.chip = host.find("ac-follow-t")[0].textContent; out.info = rec.info[0];
  out.chipAlert = host.find("ac-follow")[0].classList.contains("alerted");
  // a phone's map (390 px): followed, but no box over it
  control.map.container.clientWidth = 390;
  const n = rec.info.length;
  AircraftLayer.follow(IN.atc);
  await flush();
  out.phone = [m.following, rec.info.length - n];
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(d, items=items, atc=hexes(items[0])[0]), pre=MAP)
    em = items[1]["data"]["aircraft"][0]
    assert o["following"] == "a0bead" and o["scrolled"] and o["chipAlert"]
    assert o["ease"]["center"] == [em["lon"], em["lat"]] and o["ease"]["zoom"] >= 12
    assert o["chip"].startswith("Following N47DQ · last fix ")
    assert o["info"][0] == "N47DQ"
    assert o["phone"] == [hexes(items[0])[0], 0]


@needs_node
def test_an_events_map_rings_alerted_aircraft_only_with_the_layer_on(tmp_path, monkeypatch):
    d, items = open_alerts(monkeypatch, tmp_path)
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  AircraftLayer.alerts(IN.items);
  out.off = m.isOn();                                       // an event's map: the alert doesn't turn it on
  control.on = () => true;
  await AircraftLayer.load(); await flush(); m.draw(true);
  out.ringed = hl().map((p) => p.hex).sort();
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(d, items=items, profile="incident"), pre=MAP)
    assert o["off"] is False and o["ringed"] == sorted(h for it in items for h in hexes(it))


def test_recenter_and_a_new_frame_let_a_followed_aircraft_go():
    js = (STATIC / "cadmap.js").read_text()
    assert 'this.map.fire("sizeup:recenter")' in js and js.count('"sizeup:recenter"') == 2
    assert 'this.map.on("sizeup:follow", () => { this.userMoved = true; })' in js
    ac = (STATIC / "aircraft.js").read_text() + (STATIC / "mapfollow.js").read_text()     # the follow, shared (#358)
    # following moves the map on the frames that already move the marks: no timer, no fetch of its own
    assert "this.map.easeTo({ center: [lon, lat], duration: PAN_MS" in ac and "PAN_MS = 250" in ac and "!this.map.isMoving()" in ac
    css = (STATIC / "cadmap.css").read_text() + (STATIC / "cad.css").read_text()
    for c in (".ac-follow", ".ac-follow-t", ".aa-follow"):
        assert c in css, c


@needs_node
def test_a_card_lets_go_of_the_ring_when_redrawn_and_after_a_keyboard_follow(tmp_path, monkeypatch):
    d, items = open_alerts(monkeypatch, tmp_path)
    o = run_node(tmp_path, ["airport-alerts.js"], r"""
const calls = [];
window.AircraftLayer = { alerts() {}, hover: (h) => calls.push(h), follow() {} };
const deps = { api() {}, operator: () => "", time: () => "", readonly: true };
const list = new El("ol");
AirportAlerts.column(list, { open: IN.items }, deps);
list.children[0].fire("mouseenter");
AirportAlerts.column(list, { open: [{ ...IN.items[0], count: 9 }, IN.items[1]] }, deps);   // redrawn under the pointer
out.redrawn = calls.slice();
calls.length = 0;
const li = list.children[1];
li.fire("focusin");                                          // the keyboard: Tab to its aircraft line, Enter
li.find("aa-follow")[0].fire("click");
out.keyboard = calls.slice();
""", {"items": items})
    a, b = hexes(items[0]), hexes(items[1])
    assert o["redrawn"] == [a, []]
    assert o["keyboard"] == [b, []]                              # focus off after the follow: the ring back to normal


@needs_node
def test_unticking_the_layer_under_an_alert_holds_while_its_aircraft_moves(tmp_path, monkeypatch):
    d, items = open_alerts(monkeypatch, tmp_path)
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  await AircraftLayer.load();
  AircraftLayer.alerts(IN.items);
  out.on = m.isOn();
  const box = m.panel(), cb = box.all().find((e) => e.tagName === "INPUT");
  cb.checked = false; cb.fire("change");                     // the viewer unticks it
  out.off = m.isOn();
  // the ADS-B emergency's alert gets its aircraft rewritten (a new position, every 15 s on the hub)
  const moved = IN.items.map((a) => ({ ...a, data: { ...a.data, aircraft: a.data.aircraft.map((x) => ({ ...x, lat: x.lat + 0.01, pos_at: x.pos_at + 15 })) } }));
  AircraftLayer.alerts(moved);
  out.afterMove = m.isOn();
  AircraftLayer.alerts([...moved, { id: 99, level: "high", at: 1, data: { aircraft: [{ hex: "a0beef", callsign: "N987ZY" }] } }]);
  out.newAlert = m.isOn();                                   // another alert: on again
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(d, items=items), pre=MAP)
    assert o == {"on": True, "off": False, "afterMove": False, "newAlert": True}


@needs_node
def test_following_stops_with_the_layer_and_for_an_aircraft_long_gone(tmp_path, monkeypatch):
    d, items = open_alerts(monkeypatch, tmp_path)
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  await AircraftLayer.load();
  AircraftLayer.alerts(IN.items);
  await flush();
  m.follow(IN.atc);                                          // the layer on only for the alert
  const chip = host.find("ac-follow")[0];
  out.following = [m.following, chip.hidden];
  AircraftLayer.alerts([]);                                  // the alert accepted: the layer goes off, and the follow
  out.afterAlert = [m.following, chip.hidden, rec.zoomOpts.slice(-1)[0]];
  // the viewer's layer on: an aircraft no alert names, gone from the feed; let go once it has had no fix for 10 min
  control.on = () => true;
  m.follow(IN.atc);
  IN.d = { ...IN.d, aircraft: IN.d.aircraft.filter((a) => a.hex !== IN.atc) };
  await AircraftLayer.load(); m.draw(true);
  out.lostNow = [m.following, chip.find("ac-follow-t")[0].textContent];
  const real = Date.now; Date.now = () => real() + 11 * 60e3;
  m.draw(true);
  Date.now = real;
  out.lostLong = [m.following, chip.hidden];
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", map_input(d, items=items, atc=hexes(items[0])[0]), pre=MAP)
    atc_hex = hexes(items[0])[0]
    assert o["following"] == [atc_hex, False]
    assert o["afterAlert"] == ["", True, None]
    assert o["lostNow"][0] == atc_hex and o["lostNow"][1].startswith("Following N47DQ · last fix ")
    assert o["lostLong"] == ["", True]
