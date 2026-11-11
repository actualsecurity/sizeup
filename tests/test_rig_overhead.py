"""Rigs drawn as the vehicles from overhead, the marker itself (GitHub #427).

The user, on #384's side-profile chips: "these are horrible icons ... why are the icons side profiles, it should be
overhead or something, we are god's eye view essentially. and the icons should be on top of the rigs ... just like the
planes look like planes, the rigs should look closer to that ... especially once we get their live location data".

  the hub     mapview.icon_of: the airport's crash trucks (AP7, AP11, AP17) get their own class, arff.
  the page    cadmap.js in node with the stand-in MapLibre from test_unit_follow.py: one drawing per class; the vehicle
              turned to its live fix's heading (the shorter way round), a default without one (nose out of its
              firehouse, nose to its call, north); its status on the body; drawn to scale close in; its code placed
              clear of the others; #376's rules and the placeholder rule kept.

Everything here is made up: the stations, statuses, the call at "100 Example Ave" and every fix (the ingest shape)."""

import math

from sizeup.cad import mapview
from sizeup.cad.service import Cad, Settings

from tests.test_unit_follow import NOW, STATIC, Clock, needs_node, run, with_now

OP = {"operator": "Lt. Example"}


def fix(unit, age, **kw):
    return {"unit": unit, "lat": 41.0101, "lon": -73.6702, "at": NOW - age, "source": "test", **kw}


def page_state(same=False, **kw):
    """Made up: at HQ E60, TL2, R40 and 2391 available, E62 out of service; L31 and AP7 on location at a made-up call; E63
    responding with a live fix heading 135; E64 responding with a live fix and no heading (stopped)."""
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    hq = c.firehouses[0]["name"]
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    for name in ("E60", "E62", "E63", "E64", "L31", "TL2", "R40", "2391"):
        c.unit_station(uid(name), {"station": hq, **OP})
    c.unit_status(uid("E62"), {"status": "OS", **OP})
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, **OP})
    c.attach(inc["id"], {"unit_ids": [uid("L31"), uid("AP7")], "status": "OL", **OP})
    c.attach(inc["id"], {"unit_ids": [uid("E63"), uid("E64")], "status": "RS", **OP})
    c._feed_position(fix("E63", 5, heading=135.0, speed=0.0 if same else 10.0))
    c._feed_position(fix("E64", 5) if same else fix("E64", 5, lat=41.0121, lon=-73.6650))   # same: parked side by side
    st = c.state()
    return {"map": st["map"], "lists": st["lists"], "static": str(STATIC), "hq": hq,
            "firehouses": c.firehouses, **kw}


PRE = r"""
const zoomTo = (z) => { rm.userMoved = true; rm.map.zoom = z; rm.map.emit("zoom"); rm.map.emit("zoomend"); };
const kind = (u) => { const c = cls(u); return c.includes("folded") ? "folded" : c.includes("rig-dot") ? "dot" : "full"; };
const rot = (u) => chip(u).el.style["--rot"];
const lbl = (u) => chip(u).el.children[1].className;
"""


def test_the_crash_trucks_have_their_own_class():
    assert "arff" in mapview.ICONS
    for code in ("AP7", "AP11", "AP17"):
        assert mapview.icon_of({"name": code, "kind": "other"}) == "arff"
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    uid = next(u["id"] for u in c.store.units() if u["name"] == "AP7")
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, **OP})
    c.attach(inc["id"], {"unit_ids": [uid], "status": "OL", **OP})
    assert {m["unit"]: m["icon"] for m in c.state()["map"]["markers"]}["AP7"] == "arff"


@needs_node
def test_one_drawing_per_class_front_up(tmp_path):
    o = run(tmp_path, PRE + r"""
zoomTo(15.5);
out.cls = Object.fromEntries(["E60", "L31", "TL2", "R40", "2391", "AP7"].map((u) => [u, chip(u).el.children[0].className]));
out.svg = Object.fromEntries(["E60", "L31", "TL2", "R40", "2391", "AP7"].map((u) => [u, chip(u).el.children[0].innerHTML]));
""", with_now(page_state()))
    assert o["cls"] == {"E60": "cm-veh v-engine", "L31": "cm-veh v-ladder", "TL2": "cm-veh v-tower", "R40": "cm-veh v-rescue",
                        "2391": "cm-veh v-suv", "AP7": "cm-veh v-arff"}
    svgs = o["svg"]
    assert len(set(svgs.values())) == 6                                       # six different drawings
    for u, svg in svgs.items():
        w, l = (float(x) for x in svg.split('viewBox="0 0 ')[1].split('"')[0].split())
        assert l > 2 * w, u                                                    # long side up: front (the cab) up
        assert 'class="b"' in svg and 'class="g"' in svg, u                    # a body to tint, and glass at the front
    assert 'class="rg hi"' in svgs["L31"] and 'class="m lo"' in svgs["L31"]    # the ladder's rungs close in; a line small
    assert 'class="m hi"' in svgs["TL2"] and 'class="m lo"' in svgs["TL2"]     # the boom likewise
    assert 'class="bk"' in svgs["TL2"]                                         # the platform
    assert svgs["E60"].count('class="h"') >= 6                                 # crosslays and the hose bed


@needs_node
def test_a_live_rig_turns_to_its_heading_the_shorter_way(tmp_path):
    o = run(tmp_path, PRE + r"""
zoomTo(15.5);
out.r = [rot("E63")];
const veh0 = chip("E63").el.children[0], lbl0 = chip("E63").el.children[1], n0 = lbl0.children[0];
out.same = [];
for (const hd of [200, 350, 10, 185]) {
  setMap(withFix("E63", { heading: hd, lat: 41.0101 + hd / 1e6, at: Date.now() / 1000 }));   // a new fix, a new heading
  out.r.push(rot("E63"));
  const e = chip("E63").el;                    // the same vehicle and label: a turn eases (CSS), nothing re-parsed
  out.same.push(e.children[0] === veh0 && e.children[1] === lbl0 && e.children[1].children[0] === n0);
}
out.stopped = rot("E64");                                  // a live fix with no heading: north
out.parts = [chip("E63").el.style["--as"], chip("E63").el.style["--ac"]];
""", with_now(page_state()))
    assert o["r"] == ["135deg", "200deg", "350deg", "370deg", "545deg"]       # 350 -> 10: on 20 degrees, not back 340
    assert o["same"] == [True, True, True, True]
    assert o["stopped"] == "0deg"
    s, c = (float(x) for x in o["parts"])
    assert math.isclose(s, abs(math.sin(math.radians(185))), abs_tol=1e-3) and math.isclose(c, abs(math.cos(math.radians(185))), abs_tol=1e-3)
    css = (STATIC / "cadmap.css").read_text()
    assert "rotate(var(--rot, 0deg)); transition: transform" in css            # eased round, not snapped


@needs_node
def test_without_a_heading_nose_out_of_the_firehouse_and_to_the_call(tmp_path):
    o = run(tmp_path, PRE + r"""
zoomTo(15.5);
out.rigs = ["E60", "TL2", "R40", "L31"].map((u) => [u, rot(u), chip(u).marker.off, chip(u).m.place]);
""", with_now(page_state()))
    for u, r, (x, y), place in o["rigs"]:
        bearing = math.degrees(math.atan2(x, -y)) % 360                       # the offset's bearing on screen
        want = bearing if place == "station" else (bearing + 180) % 360
        assert (x, y) != (0, 0), u
        assert abs((float(r[:-3]) - want + 180) % 360 - 180) <= 1, (u, r, want)


@needs_node
def test_the_status_is_on_the_vehicle(tmp_path):
    o = run(tmp_path, PRE + r"""
zoomTo(15.5);
out.c = Object.fromEntries(["E60", "E62", "E63", "L31"].map((u) => [u, [chip(u).el.style["--c"], cls(u)]]));
out.lbl = chip("E62").el.children[1].children[0].className;
""", with_now(page_state()))
    st = {s["code"]: s["bg"] for s in page_state()["lists"]["unit_statuses"]}
    c = o["c"]
    assert c["E60"][0] == st["AV"] and c["E63"][0] == st["RS"] and c["L31"][0] == st["OL"]   # the body's tint
    assert len({c["E60"][0], c["E63"][0], c["L31"][0]}) == 3
    assert "look-oos" in c["E62"][1] and "rv" in c["E62"][1]
    css = (STATIC / "cadmap.css").read_text()
    assert ".cm-rig.rv.look-oos .cm-veh .b { fill: #59616B; }" in css         # out of service: grey
    assert ".cm-rig.rv:is(.outer, .enroute) .cm-veh .b { stroke: #fff; stroke-width: 2.5px; stroke-dasharray: 4 3; }" in css
    assert ".cm-rig.rv.stale .cm-veh .b { fill: color-mix(" in css            # a stale fix: hollow
    assert "border-inline-start: 2px solid var(--c, var(--dim))" in css        # the code's edge: the status colour too


@needs_node
def test_close_in_it_is_drawn_to_scale(tmp_path):
    o = run(tmp_path, PRE + r"""
const ppm = () => rm.map.getContainer().style["--ppm"];
zoomTo(15.5); out.z15 = [ppm(), L.grow, chip("E60").marker.off];
zoomTo(19); out.z19 = [ppm(), L.grow, chip("E60").marker.off];
zoomTo(13); out.z13 = [ppm(), L.grow];
""", with_now(page_state()))
    lat = 41.02
    ppm19 = 512 * 2 ** 19 / (40075016.686 * math.cos(math.radians(lat)))
    assert o["z15"][0] in (None, "0") and o["z15"][1] == 1                    # far out: the least size, no restyle
    assert math.isclose(float(o["z19"][0]), ppm19, rel_tol=0.01)             # px a metre: the CSS draws it real size
    assert 10.5 * ppm19 > 2 * 32                                              # an engine is real length there, not 28 px
    assert o["z19"][1] > 1                                                    # the fan-out widens with it ...
    assert math.hypot(*o["z19"][2]) > math.hypot(*o["z15"][2])
    assert o["z13"] == ["0", 1]                                               # ... and back
    css = (STATIC / "cadmap.css").read_text()
    assert "--L: max(calc(var(--minl) * var(--k, 1) * 1px), calc(var(--len) * var(--ppm, 0) * 1px));" in css


@needs_node
def test_codes_are_placed_clear_of_each_other(tmp_path):
    o = run(tmp_path, PRE + r"""
zoomTo(15.5);
// E63 and E64 on live fixes at the same spot (stopped): the second code goes to another side
out.at = ["E63", "E64"].map((u) => chip(u).marker.getLngLat());
out.pair = [lbl("E63"), lbl("E64")];
out.all = [...L.chips.keys()].map((u) => [u, lbl(u), kind(u)]);
""", with_now(page_state(same=True)))
    a, b = o["pair"]
    assert o["at"][0] == o["at"][1]                                           # drawn on the same spot
    assert a != b and "lbl-none" not in a and "lbl-none" not in b             # active: never left out
    for u, c, k in o["all"]:
        if k == "full" and u in ("E63", "E64", "L31"):
            assert "lbl-none" not in c, u


@needs_node
def test_the_declutter_and_safety_rules_hold_with_vehicles(tmp_path):
    o = run(tmp_path, PRE + r"""
out.z = [];
for (const z of [9, 10.5, 12, 13.5, 15, 17, 19]) {
  for (const auto of [true, false]) {
    rm.userMoved = !auto; rm.map.zoom = z; rm.map.emit("zoom"); rm.map.emit("zoomend");
    out.z.push([z, auto, Object.fromEntries([...L.chips.keys()].map((u) => [u, [kind(u), chip(u).m.look, lbl(u)]]))]);
  }
}
// a placeholder on a live fix with a heading: never drawn
setMap({ ...cad.state.map, markers: [...cad.state.map.markers, { ...chip("E63").m, unit: "DPT239", icon: "", heading: 90, placeholder: true }] });
out.dpt = L.chips.has("DPT239");
""", with_now(page_state()))
    for z, auto, rigs in o["z"]:
        for u, (k, look, lb) in rigs.items():
            if look not in ("available", "oos"):
                assert k != "folded", (u, z)
                if auto:
                    assert k == "full" and "lbl-none" not in lb, (u, z)
            if look == "oos":
                assert k != "folded", (u, z)
    assert o["dpt"] is False


def test_nothing_is_built_per_frame():
    js = (STATIC / "cadmap.js").read_text()
    tick = js[js.index("    tick() {"):js.index("    followTick(force) {")]
    assert "vehEl" not in tick and "placeLabels" not in tick and "setProperty" not in tick
    zoom = js[js.index('      map.on("zoom", () => {'):js.index("      // #376: laid out again only if the band changed")]
    assert "setScale()" in zoom and "draw()" in zoom                          # one style property per zoom step


@needs_node
def test_status_is_not_colour_alone(tmp_path):
    """Review of #432: the label carries the status code; not there yet (dispatched, responding) is dashed at any place."""
    o = run(tmp_path, PRE + r"""
zoomTo(15.5);
out.lbl = Object.fromEntries(["E60", "E62", "E63", "L31"].map((u) => [u, chip(u).el.children[1].children.map((c) => c.textContent)]));
out.enroute = Object.fromEntries(["E60", "E63", "E64", "L31"].map((u) => [u, [cls(u).includes("enroute"), chip(u).m.place]]));
""", with_now(page_state()))
    st = {m["unit"]: m["status"] for m in page_state()["map"]["markers"]}
    for u, parts in o["lbl"].items():
        assert parts[:2] == [u, st[u][:3]], (u, parts)                          # E63 RS, L31 OL, E62 OS ...
    assert o["enroute"]["E63"] == [True, "position"] and o["enroute"]["E64"][0] is True   # on a live fix, not at the call
    assert o["enroute"]["E60"][0] is False and o["enroute"]["L31"][0] is False   # in quarters; on location


@needs_node
def test_full_detail_only_close_in(tmp_path):
    o = run(tmp_path, PRE + r"""
const near = () => rm.map.getContainer().classList.contains("cm-near");
zoomTo(15.5); out.z15 = near(); zoomTo(19); out.z19 = near(); zoomTo(14); out.z14 = near();
""", with_now(page_state()))
    assert o == {"z15": False, "z19": True, "z14": False}
    css = (STATIC / "cadmap.css").read_text()
    assert ":is(.cm-veh, .cm-vs) .hi, .cm-near .cm-veh .lo { display: none; }" in css   # small: the body, a thin aerial


def test_targets_and_zoom_cost():
    css = (STATIC / "cadmap.css").read_text()
    # at least 24 px to hit (--ctl-sm: 44 px on a coarse pointer, style.css), the drawing itself unchanged (--L x --W)
    assert "inline-size: max(var(--L), var(--ctl-sm)); block-size: max(var(--L), var(--ctl-sm));" in css
    style = (STATIC / "style.css").read_text()
    assert "--ctl-sm: calc(24px * var(--zoom));" in style and "--ctl-sm: 44px;" in style
    veh = css[css.index("  .cm-veh {"):css.index("  .cm-veh svg")]
    assert veh.count("drop-shadow") == 1
    js = (STATIC / "cadmap.js").read_text()
    scale = js[js.index("    setScale() {"):js.index("    scaleNow() {")]
    assert "requestAnimationFrame" in scale                                  # at most once a frame while zooming
    assert "this.scaleNow(); if (this.setBand()" in js                      # exact on zoomend
