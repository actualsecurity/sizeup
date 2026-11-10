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
        "detail_zoom": 15.0, "active_zoom": 12.0, "board_detail_zoom": 16.0, "board_active_zoom": 10.0}
    for z in mapview.DISPLAY_DEFAULTS.values():
        assert mapview.DISPLAY_LIMITS[0] <= z <= mapview.DISPLAY_LIMITS[1]
    assert mapview.DISPLAY_DEFAULTS["active_zoom"] < mapview.DISPLAY_DEFAULTS["detail_zoom"]       # active kept longer
    assert mapview.DISPLAY_DEFAULTS["board_active_zoom"] < mapview.DISPLAY_DEFAULTS["active_zoom"]  # read from afar
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
    ex = c.settings.export()
    assert ex["overrides"]["map_display"] == {"detail_zoom": 14.5}
    c.settings.post("map_display", {"action": "reset", "key": "detail_zoom", **OP})
    assert c.state()["map"]["display"]["detail_zoom"] == 15.0 and "map_display" not in c.settings.overrides()
    audit = [(a["key"], a["old"], a["new"]) for a in reversed(c.settings.audit_log("system")) if a["key"].startswith("map_display/")]
    assert audit == [("map_display/detail_zoom", "15", "14.5"), ("map_display/detail_zoom", "14.5", "15")]
    ex["overrides"]["map_display"] = {"detail_zoom": 99}
    try:
        c.settings.post("import", {"action": "dry_run", "settings": ex, **OP})
        raise AssertionError("imported")
    except Exception as e:  # noqa: BLE001
        assert "map_display" in str(e)


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
const zoomTo = (z) => { rm.map.zoom = z; rm.map.emit("zoomend"); };
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
zoomTo(11); out.z11 = snap();
zoomTo(15.5); out.z155 = snap();
zoomTo(12); out.z12 = snap();
out.reused = chip("E60").el === els[0] && chip("E63").el === els[1];
zoomTo(13); const b = L.badges.get("PCFD Headquarters"); out.badgeEl = b.el.className; b.el.fire("click"); out.ease = rec.eases.slice(-1)[0];
""", page_state())
    hq = ["PCFD Headquarters", "HQ · 2", "PCFD Headquarters: E60, E61"]
    assert o["z13"] == {"E60": "folded", "E61": "folded", "E62": "dot", "E63": "full", "L31": "full", "badges": [hq]}
    assert o["act"] == [True, True, False]
    assert o["z11"] == {"E60": "folded", "E61": "folded", "E62": "dot", "E63": "dot", "L31": "dot", "badges": [hq]}
    assert o["z155"] == {"E60": "full", "E61": "full", "E62": "full", "E63": "full", "L31": "full", "badges": []}
    assert o["z12"]["E63"] == "full" and o["z12"]["E60"] == "folded"                  # active_zoom itself: full
    assert o["reused"] is True                                                      # the same markers throughout
    assert "cm-badge" in o["badgeEl"] and o["ease"]["zoom"] == 15                     # a click: to detail_zoom
    assert o["off"]["E63"] != [0, 0] and o["off"]["E62"] != [0, 0]                  # fanned round the badge


@needs_node
def test_followed_picked_transmitting_or_hovered_are_always_full(tmp_path):
    o = run(tmp_path, SNAP + r"""
zoomTo(11);
rm.followUnit("E60");                     // (zooms the stand-in to 15: back out, as a person would)
zoomTo(11); out.followed = snap();
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
setMap({ ...IN.map, display: { ...IN.map.display, detail_zoom: 14, active_zoom: 18 } }); out.capped = [kind("E63"), CadMap.zoomsOf(cad.state.map)];
const bh = new El("div");
const b = new CadMap.RigMap(bh, { scene: { home: { center: [-73.68, 41.02], zoom: 13 }, firehouses: [] }, pins: true,
                                  editable: () => false, onPick: () => {}, follow: false, board: true });
b.map.zoom = 13; b.update({ map: IN.map, lists: IN.lists }, null, "");
const bk = (u) => { const c = b.layer.chips.get(u).el.className; return c.includes("folded") ? "folded" : c.includes("rig-dot") ? "dot" : "full"; };
out.board13 = [bk("E60"), bk("E63"), bk("L31")];
b.map.zoom = 15.5; b.map.emit("zoomend"); out.board155 = [bk("E60"), bk("E63")];
b.map.zoom = 9.5; b.map.emit("zoomend"); out.board95 = [bk("E60"), bk("E63")];
""", page_state())
    assert o["dflt"]["E60"] == "folded"
    assert o["edited"]["E60"] == "full" and o["edited"]["badges"] == []
    assert o["capped"][0] == "dot" and o["capped"][1] == {"detail": 14, "active": 14}     # active never later than idle
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
zoomTo(13);
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
""", with_now(data, age=0.5))
    assert o["frames"] == [0, True, True]
    assert o["midzoom"] == [0, "folded"]
    assert o["zoomend"] == [1, "full"]
    assert o["sameband"] == 1                                                       # same band: nothing laid out


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
