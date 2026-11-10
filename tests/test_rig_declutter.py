"""The rigs on the maps decluttered by zoom (GitHub #376).

The user: "can we make the map less cluttered with the rig objects too? can we make them smaller until you zoom in
pretty far? except when one is responding or at a call then maybe when you zoom out it makes it keeps them a bit
longer?"

  the hub     state.map.display: the zooms (mapview.DISPLAY_DEFAULTS, Settings > System > Map display: set, reset,
              audited, exported and checked on import like the other settings); the firehouses' short names.
  the page    cadmap.js in node with the stand-in MapLibre from test_unit_follow.py: idle rigs a dot or one badge per
              firehouse below detail_zoom, active ones full down to active_zoom, out of service the smallest; followed,
              picked, transmitting or hovered always full; the wall board's own zooms; laid out again on zoomend and a
              new state only, never on the frames or mid-zoom; markers re-used.

The units are the real board's codes; their stations, statuses, the call and the fix are made up."""

import json

from sizeup.cad import mapview
from sizeup.cad.service import Cad, Settings

from tests.test_unit_follow import STATIC, Clock, needs_node, run, with_now

OP = {"operator": "Lt. Example"}


# ---- the hub ------------------------------------------------------------------------------------------------------------


def test_the_zooms_reach_the_maps_with_their_defaults():
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    assert c.state()["map"]["display"] == mapview.DISPLAY_DEFAULTS == {
        "detail_zoom": 15.0, "active_zoom": 10.0, "board_detail_zoom": 16.0, "board_active_zoom": 10.0}
    for z in mapview.DISPLAY_DEFAULTS.values():
        assert mapview.DISPLAY_LIMITS[0] <= z <= mapview.DISPLAY_LIMITS[1]
    assert mapview.DISPLAY_DEFAULTS["active_zoom"] < mapview.DISPLAY_DEFAULTS["detail_zoom"]       # active kept longer
    assert mapview.DISPLAY_DEFAULTS["board_active_zoom"] <= mapview.DISPLAY_DEFAULTS["active_zoom"]  # read from afar
    shorts = {f["name"]: f.get("short") for f in c.state()["map"]["stations"]}
    assert shorts["PCFD Headquarters"] == "HQ" and all(shorts.values())


def test_map_display_settings_are_set_reset_audited_and_checked():
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    out = c.settings.post("map_display", {"action": "set", "key": "detail_zoom", "value": 14.5, **OP})
    assert out["map_display"]["detail_zoom"] == {"label": mapview.DISPLAY_LABELS["detail_zoom"], "value": 14.5, "default": 15.0,
                                                 "edited": True, "min": 8.0, "max": 20.0}
    assert c.state()["map"]["display"]["detail_zoom"] == 14.5                         # on the next push, no restart
    assert c.settings.page()["map_display"]["active_zoom"]["edited"] is False
    for bad in (25, "14", True, None, float("nan")):
        try:
            c.settings.post("map_display", {"action": "set", "key": "detail_zoom", "value": bad, **OP})
            raise AssertionError(bad)
        except Exception as e:  # noqa: BLE001
            assert "a zoom from 8 to 20" in str(e), bad
    out = c.settings.post("map_display", {"action": "set", "key": "active_zoom", "value": 12, **OP})     # the most
    assert out["map_display"]["active_zoom"]["max"] == out["map_display"]["board_active_zoom"]["max"] == mapview.ACTIVE_MAX == 12
    for key in ("active_zoom", "board_active_zoom"):          # an active rig can't be made a dot at the zooms watched
        try:
            c.settings.post("map_display", {"action": "set", "key": key, "value": 12.5, **OP})
            raise AssertionError(key)
        except Exception as e:  # noqa: BLE001
            assert "a zoom from 8 to 12" in str(e), key
    c.settings.post("map_display", {"action": "reset", "key": "active_zoom", **OP})
    ex = c.settings.export()
    assert ex["overrides"]["map_display"] == {"detail_zoom": 14.5}
    c.settings.post("map_display", {"action": "set", "key": "detail_zoom", "value": 14.5, **OP})    # the same: no entry
    c.settings.post("map_display", {"action": "reset", "key": "active_zoom", **OP})                 # at its default: none
    c.settings.post("map_display", {"action": "reset", "key": "detail_zoom", **OP})
    assert c.state()["map"]["display"]["detail_zoom"] == 15.0 and "map_display" not in c.settings.overrides()
    audit = [(a["key"], a["old"], a["new"]) for a in reversed(c.settings.audit_log("system")) if a["key"].startswith("map_display/")]
    assert audit == [("map_display/detail_zoom", "15", "14.5"), ("map_display/active_zoom", "10", "12"),
                     ("map_display/active_zoom", "12", "10"), ("map_display/detail_zoom", "14.5", "15")]
    for bad in ({"detail_zoom": 99}, {"active_zoom": 13}):
        ex["overrides"]["map_display"] = bad
        try:
            c.settings.post("import", {"action": "dry_run", "settings": ex, **OP})
            raise AssertionError(bad)
        except Exception as e:  # noqa: BLE001
            assert "map_display" in str(e), bad
    assert mapview.display_zooms({"active_zoom": 15, "board_active_zoom": 13})["active_zoom"] == 10.0   # stored: refused


# ---- the page, in node --------------------------------------------------------------------------------------------------


def page_state(**kw):
    """All housed at HQ (made up): E60 and E61 available, E62 out of service, E63 responding from there to a made-up
    call, L31 on location at it."""
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    hq = c.firehouses[0]["name"]
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    for name in ("E60", "E61", "E62", "E63", "L31"):
        c.unit_station(uid(name), {"station": hq, **OP})
    c.unit_status(uid("E62"), {"status": "OS", **OP})
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, **OP})
    c.attach(inc["id"], {"unit_ids": [uid("L31")], "status": "OL", **OP})
    c.attach(inc["id"], {"unit_ids": [uid("E63")], "status": "RS", **OP})
    st = c.state()
    return {"map": st["map"], "lists": st["lists"], "static": str(STATIC),
            "inc": {"id": inc["id"], "lat": 41.0085, "lon": -73.66}, **kw}


SNAP = r"""
const zoomTo = (z) => { rm.userMoved = true; rm.map.zoom = z; rm.map.emit("zoomend"); };    // a person zooming
const kind = (u) => { const c = cls(u); return c.includes("folded") ? "folded" : c.includes("rig-dot") ? "dot" : "full"; };
const snap = () => ({ E60: kind("E60"), E61: kind("E61"), E62: kind("E62"), E63: kind("E63"), L31: kind("L31"),
                      badges: [...L.badges.entries()].map(([k, r]) => [k, r.el.textContent, r.el.title]) });
"""


@needs_node
def test_sizes_by_zoom_and_status_and_the_firehouse_badge(tmp_path):
    o = run(tmp_path, SNAP + r"""
const els = ["E60", "E63"].map((u) => chip(u).el);
zoomTo(13); out.z13 = snap(); out.act = [cls("E63").includes("act"), cls("L31").includes("act"), cls("E60").includes("act")];
out.off = { E62: chip("E62").marker.off, E63: chip("E63").marker.off };
zoomTo(9.5); out.z95 = snap();
zoomTo(15.5); out.z155 = snap();
zoomTo(10); out.z10 = snap();
out.reused = chip("E60").el === els[0] && chip("E63").el === els[1];
zoomTo(13); const b = L.badges.get("PCFD Headquarters"); out.badgeEl = b.el.className; out.badgeC = b.el.style["--c"] || null;
b.el.fire("click"); out.ease = rec.eases.slice(-1)[0];
""", page_state())
    hq = ["PCFD Headquarters", "HQ · 2", "PCFD Headquarters: E60, E61"]
    assert o["z13"] == {"E60": "folded", "E61": "folded", "E62": "dot", "E63": "full", "L31": "full", "badges": [hq]}
    assert o["act"] == [True, True, False]
    assert o["z95"] == {"E60": "folded", "E61": "folded", "E62": "dot", "E63": "dot", "L31": "dot", "badges": [hq]}
    assert o["z155"] == {"E60": "full", "E61": "full", "E62": "full", "E63": "full", "L31": "full", "badges": []}
    assert o["z10"]["E63"] == "full" and o["z10"]["E60"] == "folded"                  # active_zoom itself: full
    assert o["reused"] is True                                                      # the same markers throughout
    assert "cm-badge" in o["badgeEl"] and o["ease"]["zoom"] == 15                     # a click: to detail_zoom
    assert o["badgeC"] is None                                                      # neutral, not one unit's status colour
    assert o["off"]["E63"][1] < 0 and o["off"]["E62"][1] <= 0                       # over the top: nothing under the badge


@needs_node
def test_followed_picked_transmitting_or_hovered_are_always_full(tmp_path):
    o = run(tmp_path, SNAP + r"""
zoomTo(9.5);
rm.followUnit("E60");                     // (zooms the stand-in to 15: back out, as a person would)
zoomTo(9.5); out.followed = snap();
rm.fol.stop(); L.draw(); out.picked = kind("E60");               // let go, still ringed (picked) for a while
const real = Date.now; Date.now = () => real() + 13000; L.draw(); out.unpicked = kind("E60");
CadMap.hoverUnits(["E63"]); out.hovered = snap(); CadMap.hoverUnits([]);
talking.E62 = { rig: "E62", channel: "FG1", key: "k" }; setMap(IN.map); out.talking = snap(); delete talking.E62;
setMap(IN.map); out.after = snap();
Date.now = real;
""", page_state())
    assert o["followed"]["E60"] == "full" and o["followed"]["E61"] == "dot" and o["followed"]["badges"] == []   # one idle left: a dot
    assert o["picked"] == "full" and o["unpicked"] == "folded"
    assert o["hovered"]["E63"] == "full" and o["hovered"]["L31"] == "dot"
    assert o["talking"]["E62"] == "full"
    assert o["after"] == {"E60": "folded", "E61": "folded", "E62": "dot", "E63": "dot", "L31": "dot",
                          "badges": [["PCFD Headquarters", "HQ · 2", "PCFD Headquarters: E60, E61"]]}


@needs_node
def test_the_zooms_come_from_the_state_and_the_wall_board_has_its_own(tmp_path):
    o = run(tmp_path, SNAP + r"""
zoomTo(13); out.dflt = snap();
setMap({ ...IN.map, display: { ...IN.map.display, detail_zoom: 12.5 } }); out.edited = snap();
setMap({ ...IN.map, display: { ...IN.map.display, detail_zoom: 11, active_zoom: 12 } }); out.capped = [kind("E63"), CadMap.zoomsOf(cad.state.map)];
setMap({ ...IN.map, display: { ...IN.map.display, detail_zoom: 18, active_zoom: 15 } }); out.max = [kind("E63"), CadMap.zoomsOf(cad.state.map)];
const bh = new El("div");
const b = new CadMap.RigMap(bh, { scene: { home: { center: [-73.68, 41.02], zoom: 13 }, firehouses: [] }, pins: true,
                                  editable: () => false, onPick: () => {}, follow: false, board: true });
b.map.zoom = 13; b.update({ map: IN.map, lists: IN.lists }, null, ""); b.userMoved = true; b.map.emit("zoomend");
const bk = (u) => { const c = b.layer.chips.get(u).el.className; return c.includes("folded") ? "folded" : c.includes("rig-dot") ? "dot" : "full"; };
out.board13 = [bk("E60"), bk("E63"), bk("L31")];
b.map.zoom = 15.5; b.map.emit("zoomend"); out.board155 = [bk("E60"), bk("E63")];
b.map.zoom = 9.5; b.map.emit("zoomend"); out.board95 = [bk("E60"), bk("E63")];
""", page_state())
    assert o["dflt"]["E60"] == "folded"
    assert o["edited"]["E60"] == "full" and o["edited"]["badges"] == []
    assert o["capped"] == ["full", {"detail": 11, "active": 11}]                     # active never later than idle
    assert o["max"] == ["full", {"detail": 18, "active": 12}]                       # nor than ACTIVE_MAX (a stored 15)
    assert o["board13"] == ["folded", "full", "full"]
    assert o["board155"] == ["folded", "full"]
    assert o["board95"] == ["folded", "dot"]


@needs_node
def test_no_layout_on_the_frames_or_mid_zoom(tmp_path):
    """A moving live fix runs the frame loop: the marks move, nothing is re-sized or re-fanned. A zoom in progress
    (zoom events) changes nothing until its zoomend."""
    data = page_state()
    for m in data["map"]["markers"]:
        if m["unit"] == "E63":                         # E63 on the road toward the call: a fresh fix, 10 m/s east
            m.update(place="position", group="unit:E63", lon=-73.665, lat=41.004, heading=90.0, speed=10.0, at=0, age=0.5,
                     stale=False, source="test")
    o = run(tmp_path, SNAP + r"""
zoomTo(14);
let lays = 0; const off0 = L.offsets.bind(L); L.offsets = (...a) => { lays++; return off0(...a); };
const before = JSON.stringify(snap()), x0 = lng("E63");
run(40, 25);
out.frames = [lays, JSON.stringify(snap()) === before, lng("E63") > x0];
rm.map.zoom = 16; rm.map.emit("zoom"); rm.map.emit("zoom");
out.midzoom = [lays, kind("E60")];
rm.map.emit("zoomend");
out.zoomend = [lays, kind("E60")];
rm.map.zoom = 16.2; rm.map.emit("zoomend");
out.sameband = lays;
rm.map.zoom = 13; rm.map.emit("zoom");                        // crossing FAR_ZOOM mid-zoom (smaller chips): redrawn,
out.far = [kind("E60"), L.band.detail, rm.host.classList.contains("cm-far")];   // in the band of the zoom it is at
""", with_now(data, age=0.5))
    assert o["frames"] == [0, True, True]
    assert o["midzoom"] == [0, "folded"]
    assert o["zoomend"] == [1, "full"]
    assert o["sameband"] == 1                                                       # same band: nothing laid out
    assert o["far"] == ["folded", False, True]


def test_the_page_wires_it():
    js = (STATIC / "cadmap.js").read_text()
    assert 'map.on("zoomend"' in js and "board: BOARD" in js
    tick = js[js.index("    tick() {"):js.index("    followTick(force) {")]
    assert "offsets(" not in tick and "drawChips(" not in tick and "declutter(" not in tick
    css = (STATIC / "cadmap.css").read_text()
    for c in (".cm-rig.rig-dot", ".cm-rig.folded", ".cm-badge", ".cm-rig.rig-dot.look-oos", ".cm-rig.rig-dot.act"):
        assert c in css, c
    s = (STATIC / "settings.js").read_text()
    assert "function sMapDisplay(" in s and 'sPost("map_display"' in s
    assert json.loads((STATIC.parent.parent.parent / "data" / "firehouses.json").read_text())["firehouses"][0]["short"] == "HQ"


AREA_BBOX = [-73.7067, 40.9866, -73.6552, 41.0739]      # sizeup/roads.py: the scene's home bounds (app.py)
FIT = r"""
// MapLibre's fitBounds, enough of it: the zoom that fits the box in the canvas less the padding (512 px tiles)
const merc = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / (2 * Math.PI);
const fitTo = (map, b, o) => {
  const p = o.padding, W = map.container.clientWidth - p.left - p.right, H = map.container.clientHeight - p.top - p.bottom;
  const dx = (b[1][0] - b[0][0]) / 360, dy = merc(b[1][1]) - merc(b[0][1]);
  map.zoom = Math.min(o.maxZoom, Math.log2(W / (512 * dx)), Math.log2(H / (512 * dy)));
  map.emit("zoomend");
};
"""


@needs_node
def test_the_overviews_automatic_frame_keeps_active_rigs_full_at_panel_sizes(tmp_path):
    """The console Overview framed on the area at common panel sizes lands near zoom 11-12: every active rig a full chip
    (the frame, and active_zoom 10), idle ones still small; moved by hand below active_zoom: dots; Recenter: full again."""
    o = run(tmp_path, SNAP + FIT + r"""
rm.opts.scene.home.bounds = IN.bbox;
rm.map.fitBounds = (b, opt) => fitTo(rm.map, b, opt);
out.sizes = [];
for (const [w, h] of [[733, 790], [900, 600], [700, 500]]) {
  rm.canvas.clientWidth = rm.map.container.clientWidth = w; rm.canvas.clientHeight = rm.map.container.clientHeight = h;
  rm.userMoved = false; rm.fit(false);
  out.sizes.push([w, h, Math.round(rm.map.zoom * 10) / 10, kind("E63"), kind("L31"), kind("E60")]);
}
rm.map.emit("movestart", { originalEvent: {} }); rm.map.zoom = 9.5; rm.map.emit("moveend"); out.moved = [kind("E63"), kind("L31")];
host.find("cm-btn")[0].fire("click"); out.recenter = [rm.userMoved, kind("E63"), kind("L31")];
""", page_state(bbox=AREA_BBOX))
    for w, h, z, e63, l31, e60 in o["sizes"]:
        assert 10.5 <= z <= 13, (w, h, z)
        assert (e63, l31) == ("full", "full"), (w, h, z)
        assert e60 == "folded", (w, h, z)                                            # idle: still the badge
    assert o["moved"] == ["dot", "dot"]
    assert o["recenter"] == [False, "full", "full"]


@needs_node
def test_every_status_has_its_class(tmp_path):
    """One case per status in data/cad_lists.json, on a call (as it would be when set there): its look from the hub
    (mapview.look_of), its class on the page (cadmap.js rigClass)."""
    from sizeup.cad.lists import load
    L = load()
    hq = {"name": "PCFD Headquarters", "ll": [-73.66692, 41.00253]}
    inc = {"id": 7, "number": "26-000007", "lat": 41.0, "lon": -73.67}
    codes = [s["code"] for s in L.unit_statuses]
    ms = [mapview.place({"id": 1, "name": "E60", "kind": "engine", "status": c, "board": "main"}, L.role(c), hq, inc, None, 0.0)
          for c in codes]
    o = run(tmp_path, "out.cls = IN.ms.map((m) => CadMap.rigClass(m));", page_state(ms=ms))
    got = dict(zip(codes, o["cls"]))
    want = {**{c: "act" for c in ("AK", "ST", "OA", "CB", "DO", "DP", "RS", "ET", "OL", "IC")},
            **{c: "idle" for c in ("AQ", "AV", "FR", "NR", "CN", "CE")}, **{c: "oos" for c in ("OS", "UNMANNED")}}
    assert got == want
    assert mapview.look_of("") == mapview.look_of(None) == mapview.look_of("something new") == mapview.ACTIVE_LOOK == "dispatched"


@needs_node
def test_a_stale_active_dot_is_not_faded(tmp_path):
    data = page_state()
    for m in data["map"]["markers"]:
        if m["unit"] == "E63":
            m.update(place="position", group="unit:E63", lon=-73.665, lat=41.004, heading=90.0, speed=0.0, at=0, age=0,
                     stale=True, source="test")
    o = run(tmp_path, SNAP + r"""
zoomTo(9.5); out.dot = [kind("E63"), cls("E63").includes("stale"), chip("E63").el.style.opacity];
zoomTo(14); out.chip = [kind("E63"), chip("E63").el.style.opacity];
""", with_now(data, age=200))
    assert o["dot"] == ["dot", True, ""]
    assert o["chip"][0] == "full" and 0.35 <= float(o["chip"][1]) < 1                # a chip still fades as before


@needs_node
def test_the_automatic_frame_not_the_zoom_keeps_active_rigs_full(tmp_path):
    """active_zoom at its most (12): the Overview's automatic frame lands under it (about 11.4) and the active rigs stay
    full chips only because the frame is automatic; moved by hand to 11.8: dots; Recenter: full again at once (before
    the frame's animation ends: its events are held back here); a hand move again, then the calls change (framed
    again, as Recenter): full."""
    data = page_state(bbox=AREA_BBOX)
    data["map"]["display"]["active_zoom"] = 12.0
    o = run(tmp_path, SNAP + FIT + r"""
rm.opts.scene.home.bounds = IN.bbox;
rm.canvas.clientWidth = rm.map.container.clientWidth = 733; rm.canvas.clientHeight = rm.map.container.clientHeight = 790;
// the frame's zoom set as the animation starts; its zoomend / moveend held back (they come later, if at all)
rm.map.fitBounds = (b, opt) => { const em = rm.map.emit; rm.map.emit = () => {}; fitTo(rm.map, b, opt); rm.map.emit = em; };
const pan = (z) => { rm.map.emit("movestart", { originalEvent: {} }); rm.map.zoom = z; rm.map.emit("moveend"); rm.map.emit("zoomend"); };
const both = () => [kind("E63"), kind("L31")];
rm.userMoved = false; rm.fit(false); rm.map.emit("moveend"); rm.map.emit("zoomend");
out.auto = [Math.round(rm.map.zoom * 10) / 10, ...both(), kind("E60")];
pan(11.8); out.moved = [rm.userMoved, ...both()];
host.find("cm-btn")[0].fire("click"); out.recenter = [rm.userMoved, Math.round(rm.map.zoom * 10) / 10, ...both()];
pan(11.8); out.moved2 = both();
const p0 = IN.map.incidents[0];
let draws = 0; const draw0 = L.draw.bind(L); L.draw = () => { draws++; draw0(); };
setMap({ ...IN.map, incidents: [...IN.map.incidents, { ...p0, id: p0.id + 1, number: "26-999999", lat: 41.012, lon: -73.672 }] });
out.calls = [rm.userMoved, ...both(), draws];                 // drawn once, in the band of the new frame
""", data)
    assert 11 <= o["auto"][0] < 12 and o["auto"][1:] == ["full", "full", "folded"]
    assert o["moved"] == [True, "dot", "dot"]
    assert o["recenter"][0] is False and o["recenter"][1] < 12 and o["recenter"][2:] == ["full", "full"]
    assert o["moved2"] == ["dot", "dot"]
    assert o["calls"] == [False, "full", "full", 1]


def test_a_marker_carries_its_status_time():
    clock = Clock()
    c = Cad(Settings(), log=lambda m: None, clock=clock)
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    for name in ("E60", "E63"):
        c.unit_station(uid(name), {"station": c.firehouses[0]["name"], **OP})
    clock.t += 30
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, **OP})
    c.attach(inc["id"], {"unit_ids": [uid("E63")], "status": "RS", **OP})
    units = {u["name"]: u for u in c.store.units()}
    assert units["E63"]["status_at"] == clock.t != units["E60"]["status_at"]
    ms = {m["unit"]: m for m in c.state()["map"]["markers"]}
    assert {"E60", "E63"} <= set(ms) and all(m["status_at"] == units[n]["status_at"] for n, m in ms.items())


@needs_node
def test_a_rig_just_made_active_is_full_for_a_minute(tmp_path):
    """Below active_zoom (moved by hand): E63's status set 10 s ago (just dispatched): a full chip, L31's an hour ago: a
    dot; an idle rig just set (E60) stays in its badge. When E63's minute runs out it is laid out again: a dot."""
    data = page_state()
    import time as _t
    now = _t.time()
    for m in data["map"]["markers"]:
        m["status_at"] = {"E63": now - 10, "E60": now - 5}.get(m["unit"], now - 3600)
    o = run(tmp_path, SNAP + r"""
zoomTo(9.5); out.fresh = snap(); out.timer = L.freshT ? L.freshT._idleTimeout : null;
out.ring = cls("E63").some((c) => ["hl", "hot", "followed"].includes(c));
const real = Date.now; Date.now = () => real() + 51000;
L.freshT._onTimeout(); out.later = snap(); out.timer2 = L.freshT;
Date.now = real;
""", data)
    assert o["fresh"]["E63"] == "full" and o["fresh"]["L31"] == "dot" and o["fresh"]["E60"] == "folded"
    assert 49_000 <= o["timer"] <= 50_100 and o["ring"] is False                    # no ring: only its size
    assert o["later"]["E63"] == "dot" and o["timer2"] == 0
