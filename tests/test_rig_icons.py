"""Compact station badges, a firehouse's rig list, rig icons by apparatus type (GitHub #384).

The user, on the Overview at a normal zoom: the folded station badges ("HQ · 9", "South End · 3") "are still way too
big", covering the firehouse names and each other. Asked for: a count chip about the size of a rig marker on the
firehouse icon, kept clear of its name; a click on a firehouse (or its chip) lists its rigs (each one followed or lit
up from there, #358) with a Zoom to; rigs drawn by apparatus type (engine, ladder, tower ladder, rescue, utility SUV for
the chiefs', duty and utility cars, and a generic mark), with #376's rules kept.

  the hub     mapview.icon_of: a unit's kind in data/cad_lists.json -> its icon; every marker carries it.
  the page    cadmap.js in node with the stand-in MapLibre from test_unit_follow.py; map-style.js's firehouse-badge
              mark (the label placement's space for the chip); cadmap.css's masks.

The units are the real board's codes; their stations, statuses and the call are made up."""

import json
import re

from sizeup.cad import mapview
from sizeup.cad.lists import ROOT
from sizeup.cad.service import Cad, Settings

from tests.test_unit_follow import STATIC, Clock, needs_node, run

OP = {"operator": "Lt. Example"}


# ---- the hub: the type mapping ---------------------------------------------------------------------------------------


def test_each_apparatus_class_has_its_icon_and_everything_else_the_generic_one():
    units = json.loads((ROOT / "data" / "cad_lists.json").read_text())["units"]
    want = {"E60": "engine", "E14": "engine", "L31": "ladder", "L30": "ladder", "TL2": "tower", "R40": "rescue",
            "2391": "suv", "2398": "suv", "2661": "suv", "U65": "suv", "U42": "suv", "CHIEF239": "suv",
            "M40": "other", "DPT239": "", "239EOC": "other", "77A1": "other", "PCPD": "other", "239_Eng": "other"}
    for code, icon in want.items():
        assert mapview.icon_of({"name": code, **units[code]}) == icon, code
    by_kind = {"engine": "engine", "ladder": "ladder", "tower": "tower", "rescue": "rescue", "chief": "suv", "utility": "suv"}
    for code, u in units.items():                          # every unit on the board: by its kind, a known icon
        icon = mapview.icon_of({"name": code, **u})
        if code.upper().startswith("DPT"):                   # a department placeholder: no icon, never drawn
            assert icon == "", code
            continue
        assert icon in mapview.ICONS, code
        named = u.get("kind") in ("", "other", None) and code.upper().startswith("CHIEF")   # CHIEF239: a chief's car
        assert icon == by_kind.get(u.get("kind"), "suv" if named else "other"), code
    # an older or odd unit: no kind, a new kind, a code-only chief's car with no kind
    assert mapview.icon_of({"name": "X1"}) == mapview.icon_of({"name": "X1", "kind": "hovercraft"}) == "other"
    assert mapview.icon_of({"name": "CHIEF999", "kind": ""}) == "suv"
    assert mapview.icon_of({"name": "CHIEF999", "kind": "ems"}) == "other"   # another agency's: never ours by name


def test_every_marker_carries_its_icon():
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    for n in ("E60", "L31", "TL2", "R40", "2391", "U65", "M40"):
        c.unit_station(uid(n), {"station": c.firehouses[0]["name"], **OP})
    ms = {m["unit"]: m["icon"] for m in c.state()["map"]["markers"]}
    assert ms == {"E60": "engine", "L31": "ladder", "TL2": "tower", "R40": "rescue", "2391": "suv", "U65": "suv", "M40": "other"}


def test_each_icon_is_defined_once_as_a_mask_and_they_differ():
    css = (STATIC / "cadmap.css").read_text()
    masks = {n: re.findall(r'\.cm-ty\.ty-%s \{ --ty: url\("(data:image/svg\+xml,[^"]+)"\); \}' % n, css) for n in mapview.ICONS}
    assert all(len(v) == 1 for v in masks.values()), {k: len(v) for k, v in masks.items()}
    assert len({v[0] for v in masks.values()}) == len(mapview.ICONS)          # six different silhouettes
    assert "mask: var(--ty)" in css and "background-color: currentColor" in css     # monochrome, in the text colour
    js = (STATIC / "cadmap.js").read_text()
    m = re.search(r'const ICONS = new Set\((\[[^\]]*\])\)', js)
    assert set(json.loads(m.group(1))) == set(mapview.ICONS)


# ---- the page, in node --------------------------------------------------------------------------------------------------


def page_state(**kw):
    """Made up: at HQ E60, E61, TL2, 2391 available, E62 out of service, E63 responding to a made-up call, L31 on location
    at it; at South End E64 and U66 available."""
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    hq, se = c.firehouses[0]["name"], next(f["name"] for f in c.firehouses if f.get("short") == "South End")
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    for name in ("E60", "E61", "E62", "E63", "L31", "TL2", "2391"):
        c.unit_station(uid(name), {"station": hq, **OP})
    for name in ("E64", "U66"):
        c.unit_station(uid(name), {"station": se, **OP})
    c.unit_status(uid("E62"), {"status": "OS", **OP})
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, **OP})
    c.attach(inc["id"], {"unit_ids": [uid("L31")], "status": "OL", **OP})
    c.attach(inc["id"], {"unit_ids": [uid("E63")], "status": "RS", **OP})
    st = c.state()
    return {"map": st["map"], "lists": st["lists"], "static": str(STATIC), "hq": hq, "se": se,
            "firehouses": c.firehouses, **kw}


SNAP = r"""
const zoomTo = (z) => { rm.userMoved = true; rm.map.zoom = z; rm.map.emit("zoomend"); };    // a person zooming
const kind = (u) => { const c = cls(u); return c.includes("folded") ? "folded" : c.includes("rig-dot") ? "dot" : "full"; };
rm.opts.scene.firehouses = IN.firehouses;
rm.map.addSource("firehouses"); L.fhSig = null; L.draw();         // the style's firehouse source (a stand-in style here)
const clickHouse = (name) => rm.map.emit("click", { features: [{ properties: { name } }] });   // the "firehouse" layer's
const rows = () => L.pop.find("cm-st-row");
const rowOf = (u) => rows().find((r) => r.dataset.unit === u);
"""


@needs_node
def test_the_chips_carry_their_type_icon(tmp_path):
    o = run(tmp_path, SNAP + r"""
zoomTo(15.5);
const ty = (u) => chip(u).el.children[0].className;
out.full = Object.fromEntries(["E60", "L31", "TL2", "2391", "E62", "E63"].map((u) => [u, [ty(u), chip(u).el.children[1].textContent, chip(u).el.title]]));
zoomTo(9.5); out.dot = [kind("E63"), ty("E63")];          // a dot keeps it (shown when the dot opens under the pointer)
""", page_state())
    f = o["full"]
    assert {u: v[0] for u, v in f.items()} == {"E60": "cm-ty ty-engine", "L31": "cm-ty ty-ladder", "TL2": "cm-ty ty-tower",
                                               "2391": "cm-ty ty-suv", "E62": "cm-ty ty-engine", "E63": "cm-ty ty-engine"}
    assert f["TL2"][1] == "TL2" and f["TL2"][2].startswith("TL2 (tower ladder), Available")
    assert f["2391"][2].startswith("2391 (car), ")
    assert o["dot"] == ["dot", "cm-ty ty-engine"]
    css = (STATIC / "cadmap.css").read_text()
    assert ".cm-rig.rig-dot > *, .cm-rig.rig-dot::before { display: none; }" in css           # a plain dot ...
    assert ".cm-rig.rig-dot:hover > .cm-ty { display: inline-block; }" in css               # ... opening to icon + code


@needs_node
def test_the_count_chip_is_small_on_the_icon_and_the_names_keep_clear_of_it(tmp_path):
    o = run(tmp_path, SNAP + r"""
zoomTo(13);
const b = L.badges.get(IN.hq);
out.badge = [b.el.textContent, b.el.className, b.marker.off, b.el.title, b.el.attrs["aria-label"]];
out.src = (rm.map.sources.firehouses && rm.map.sources.firehouses.data) ? rm.map.sources.firehouses.data.features.map((f) => [f.properties.name, f.properties.badge]) : null;
// every other mark at the firehouse (chips, dots) clear of the chip: their boxes never meet
const at = (u) => chip(u).marker.off;
out.marks = ["E62", "E63"].map((u) => [u, kind(u), at(u)]);
const S = eval(fs.readFileSync(IN.static + "/map-style.js", "utf8") + "; ({ buildMapStyle, badgeSpaceIcon })");
const st = S.buildMapStyle({ glyphs: "https://example.invalid/{fontstack}/{range}.pbf" });
out.layers = st.layers.map((l) => l.id).filter((id) => id.startsWith("firehouse"));
out.reserve = st.layers.find((l) => l.id === "firehouse-badge");
out.label = st.layers.find((l) => l.id === "firehouse").layout;
const img = S.badgeSpaceIcon(); out.img = [img.width, img.height, img.data.length, Math.max(...img.data)];
zoomTo(15.5); out.after = [L.badges.size, rm.map.sources.firehouses.data.features.filter((f) => f.properties.badge).length];
""", page_state())
    text, cls, off, title, aria = o["badge"]
    hq = page_state()["hq"]
    assert text == "4" and "cm-badge" in cls                      # the idle ones at HQ: a count, nothing more
    assert title.startswith(f"{hq}: ") and sorted(title.split(": ")[1].split(", ")) == ["2391", "E60", "E61", "TL2"]
    assert aria == f"{hq}, 4 rigs in quarters"
    assert off == [0, -13]                                        # on top of the icon, not under it
    css = (STATIC / "cadmap.css").read_text()
    rule = css[css.index("  .cm-badge {"):css.index("  .cm-badge:hover")]
    assert "block-size: var(--s-4)" in rule and "min-inline-size: var(--s-4)" in rule and "var(--fs-2xs)" in rule
    assert "--mono" not in rule                                   # no long monospace label any more
    # Its box (16 px: var(--s-4); up to 2 digits) against the name's four places (map-style.js: text-variable-anchor,
    # text-radial-offset em from the icon's centre, text-size by zoom, MapLibre's 2 px text padding, a line 1.2 em):
    # at every zoom the chips are drawn at, it meets only the place over the icon ("bottom"), never the first ("left":
    # to the right of the icon) nor the others; a firehouse's name always has somewhere to go.
    lab = o["label"]
    stops = lab["text-size"][3:]
    def size(z):
        pts = list(zip(stops[::2], stops[1::2]))
        if z <= pts[0][0]:
            return pts[0][1]
        for (z0, s0), (z1, s1) in zip(pts, pts[1:]):
            if z <= z1:
                return s0 + (s1 - s0) * (z - z0) / (z1 - z0)
        return pts[-1][1]
    def meets(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
    chip = (off[0] - 8, off[1] - 8, off[0] + 8, off[1] + 8)
    for z in (10, 11, 12, 13, 14, 15, 16):
        ts = size(z)
        r_, hh, w = lab["text-radial-offset"] * ts, 0.6 * ts + 2, 3 * ts + 2      # the shortest name: 3 em wide
        places = {"left": (r_ - 2, -hh, r_ + w, hh), "right": (-r_ - w, -hh, -r_ + 2, hh),
                  "top": (-w / 2, r_ - 2, w / 2, r_ + 2 * hh), "bottom": (-w / 2, -r_ - 2 * hh, w / 2, -r_ + 2)}
        assert set(places) == set(lab["text-variable-anchor"])
        assert [k for k, b in places.items() if meets(chip, b)] == ["bottom"], z
    # the other marks at the firehouse fan out under it, clear of the chip's box (a dot up to 12 px, a chip 62 x 18)
    for u, k, (x, y) in o["marks"]:
        hw, hh_ = (6, 6) if k == "dot" else (31, 9)
        assert not meets(chip, (x - hw, y - hh_, x + hw, y + hh_)), (u, k, x, y)
    # the label's placement avoids it: an invisible mark of the chip's size at the chip's place, placed before the
    # names (above the firehouse layer), counted in the collisions (not ignore-placement), only where there is a chip
    assert o["layers"] == ["firehouse", "firehouse-badge"]
    r = o["reserve"]
    assert r["source"] == "firehouses" and r["filter"] == ["==", ["get", "badge"], 1]
    assert r["layout"]["icon-offset"] == off and r["layout"]["icon-ignore-placement"] is False
    assert r["layout"]["icon-allow-overlap"] is True and r["paint"]["icon-opacity"] == 0
    assert o["img"] == [16, 16, 16 * 16 * 4, 0]                   # the chip's size, clear
    assert o["src"] is not None and dict(o["src"])[page_state()["hq"]] == 1 and dict(o["src"])[page_state()["se"]] == 1
    assert sum(b for _, b in o["src"]) == 2
    assert o["after"] == [0, 0]                                   # zoomed in: no chips, the names free again


@needs_node
def test_a_click_on_a_firehouse_or_its_chip_lists_its_rigs(tmp_path):
    o = run(tmp_path, SNAP + r"""
zoomTo(13);
clickHouse(IN.hq);
out.house = [L.pop.hidden, L.pop.find("cm-pop-head")[0].textContent,
             rows().map((r) => [r.dataset.unit, r.children[0].className, r.children[2].textContent, r.style["--c"] || null, r.disabled])];
L.closePicker(); out.closed = L.pop.hidden;
L.badges.get(IN.se).el.fire("click");
out.chip = [L.pop.hidden, rows().map((r) => r.dataset.unit)];
clickHouse("Brooksville Engine & Hose #5"); out.none = [L.pop.find("cm-pop-head")[0].textContent, rows().length];
""", page_state())
    hidden, head, rows = o["house"]
    assert hidden is False and head == f"{page_state()['hq']} 7 rigs"
    # active first (on a call), then available, then out of service; each with its type and status, in its status colour
    assert [r[0] for r in rows] == ["E63", "L31", "2391", "E60", "E61", "TL2", "E62"]
    assert [r[1] for r in rows] == ["cm-ty ty-engine", "cm-ty ty-ladder", "cm-ty ty-suv", "cm-ty ty-engine", "cm-ty ty-engine",
                                    "cm-ty ty-tower", "cm-ty ty-engine"]
    assert [r[2] for r in rows] == ["Responding", "On location", "Available", "Available", "Available", "Available", "Out of service"]
    assert all(r[3] for r in rows) and not any(r[4] for r in rows)
    assert o["closed"] is True
    assert o["chip"] == [False, ["E64", "U66"]]
    assert o["none"] == ["Brooksville Engine & Hose #5 No rigs", 0]


@needs_node
def test_the_open_list_keeps_to_the_state(tmp_path):
    """A new state that changes a row (E60 dispatched) lays the list out again, with no focus taken; one that changes
    nothing leaves it be; once the box is another's (a rig's), the list is not put back."""
    o = run(tmp_path, SNAP + r"""
zoomTo(13); clickHouse(IN.hq);
const first = L.pop.children[1];
setMap({ ...IN.map }); out.same = L.pop.children[1] === first;
const moved = IN.map.markers.map((m) => (m.unit === "E60" ? { ...m, status: "DP", look: "dispatched" } : m));
focused = null; setMap({ ...IN.map, markers: moved });
out.changed = [L.pop.children[1] === first, rows().map((r) => r.dataset.unit).slice(0, 3), rowOf("E60").children[2].textContent, focused];
L.openInfo(cad.state.map.markers.find((m) => m.unit === "E61"));
setMap({ ...IN.map }); out.info = [L.stationOpen, rows().length];
""", page_state())
    assert o["same"] is True
    assert o["changed"][0] is False and o["changed"][1] == ["E60", "E63", "L31"] and o["changed"][2] == "Dispatched"
    assert o["changed"][3] is None
    assert o["info"] == [None, 0]


@needs_node
def test_a_rig_in_the_list_is_followed_or_lit_up(tmp_path):
    o = run(tmp_path, SNAP + r"""
zoomTo(13);
out.before = kind("TL2");
clickHouse(IN.hq);
rowOf("E60").fire("mouseenter"); out.hover = [kind("E60"), cls("E60").includes("hot")];
rowOf("E60").fire("mouseleave"); out.unhover = kind("E60");
rowOf("TL2").fire("click");
out.follow = [rm.fol.id, kind("TL2"), L.pop.hidden, rec.eases.slice(-1)[0].zoom >= 15];
// the wall board: no follower; the list rings it (centred, picked) instead
const bh = new El("div");
const b = new CadMap.RigMap(bh, { scene: { home: { center: [-73.68, 41.02], zoom: 13 }, firehouses: IN.firehouses }, pins: true,
                                  editable: () => false, onPick: () => {}, follow: false, board: true });
b.map.zoom = 13; b.update({ map: IN.map, lists: IN.lists }, null, ""); b.userMoved = true; b.map.emit("zoomend");
const bk = (u) => { const c = b.layer.chips.get(u).el.className; return c.includes("folded") ? "folded" : c.includes("rig-dot") ? "dot" : "full"; };
b.map.emit("click", { features: [{ properties: { name: IN.hq } }] });
const brow = b.layer.pop.find("cm-st-row").find((r) => r.dataset.unit === "E61");
out.board = [b.fol, brow.disabled, bk("E61")];
brow.fire("click");
out.boardPick = [bk("E61"), b.layer.chips.get("E61").el.className.includes("hl"), b.userMoved];
""", page_state())
    assert o["before"] == "folded"
    assert o["hover"] == ["full", True] and o["unhover"] == "folded"       # #358's row ring, from the list
    assert o["follow"] == ["TL2", "full", True, True]                     # followed (#358): centred, out of its badge
    assert o["board"] == [None, False, "folded"]
    assert o["boardPick"] == ["full", True, True]


@needs_node
def test_zoom_to_frames_the_firehouse_where_its_rigs_draw_one_by_one(tmp_path):
    o = run(tmp_path, SNAP + r"""
rm.userMoved = false; rm.map.zoom = 12; rm.map.emit("zoomend");
clickHouse(IN.hq);
L.pop.byText("Zoom to").fire("click");
const e = rec.eases.slice(-1)[0];
out.ease = [e.center, e.zoom, rm.userMoved, L.pop.hidden];
rm.map.emit("zoomend");                                             // the ease done
out.sizes = Object.fromEntries(["E60", "E61", "TL2", "2391", "E62", "E63"].map((u) => [u, kind(u)]));
out.badges = L.badges.has(IN.hq);
// already closer: kept as close
rm.map.zoom = 16.5; clickHouse(IN.hq); L.pop.byText("Zoom to").fire("click"); out.closer = rec.eases.slice(-1)[0].zoom;
// the wall board: its own detail zoom
const bh = new El("div");
const b = new CadMap.RigMap(bh, { scene: { home: { center: [-73.68, 41.02], zoom: 13 }, firehouses: IN.firehouses }, pins: true,
                                  editable: () => false, onPick: () => {}, follow: false, board: true });
b.map.zoom = 12; b.update({ map: IN.map, lists: IN.lists }, null, "");
b.layer.openStation(IN.hq); b.layer.pop.byText("Zoom to").fire("click"); out.board = rec.eases.slice(-1)[0].zoom;
""", page_state())
    hq = next(f for f in page_state()["firehouses"] if f["name"] == page_state()["hq"])
    assert o["ease"] == [hq["ll"], 15, True, True]                       # detail_zoom; a move by hand (until Recenter)
    assert set(o["sizes"].values()) == {"full"} and o["badges"] is False
    assert o["closer"] == 16.5
    assert o["board"] == 16                                              # board_detail_zoom


@needs_node
def test_no_active_rig_is_hidden_and_376s_rules_hold(tmp_path):
    """At every zoom from 8 to 17, by hand and on the automatic frame, with the list open or not: an active rig is never
    folded (on the frame: always a full chip); out of service never full below detail_zoom nor folded; idle ones fold only
    into a firehouse's chip, which counts exactly them."""
    o = run(tmp_path, SNAP + r"""
out.rows = [];
for (const auto of [false, true]) for (const open of [false, true]) for (let z = 8; z <= 17; z += 0.5) {
  rm.userMoved = !auto; rm.map.zoom = z; rm.map.emit("zoomend");
  if (open) clickHouse(IN.hq); else L.closePicker();
  const ms = cad.state.map.markers;
  const badgeCount = Object.fromEntries([...L.badges.entries()].map(([k, r]) => [k, Number(r.el.textContent)]));
  out.rows.push({ auto, open, z, badgeCount, ms: ms.map((m) => [m.unit, CadMap.rigClass(m), kind(m.unit), m.station, m.place]) });
}
""", page_state())
    for r in o["rows"]:
        folded = {}
        for unit, c, k, station, place in r["ms"]:
            if c == "act":
                assert k != "folded", r
                if r["auto"]:
                    assert k == "full", (unit, r["z"])
            if c == "oos":
                assert k != "folded" and (k == "dot" or r["z"] >= 15), (unit, r["z"])
            if k == "folded":
                assert c == "idle" and place == "station", (unit, r["z"])
                folded[station] = folded.get(station, 0) + 1
        assert folded == r["badgeCount"], r["z"]
        assert all(n >= 2 for n in folded.values())


def test_the_page_wires_it():
    js = (STATIC / "cadmap.js").read_text()
    assert 'this.map.on("click", "firehouse"' in js and "badgeSpaceIcon()" in js
    tick = js[js.index("    tick() {"):js.index("    followTick(force) {")]
    assert "openStation" not in tick and "syncFirehouses" not in tick and "cm-ty" not in tick     # nothing per frame
    assert "BADGE_AT = [0, -13]" in js
    style = (STATIC / "map-style.js").read_text()
    assert '"icon-offset": [0, -13]' in style                     # the same place as the chip


# ---- department placeholders (DPT239 ...): never on the map (the user's rule) --------------------------------------------


def test_department_placeholders_are_never_drawn_listed_or_given_an_icon():
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    hq = c.firehouses[0]["name"]
    for n in ("DPT239", "DPT266", "E60", "E61"):
        c.unit_station(uid(n), {"station": hq, **OP})
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, **OP})
    c.attach(inc["id"], {"unit_ids": [uid("DPT239"), uid("L31")], "status": "OL", **OP})
    m = c.state()["map"]
    names = [x["unit"] for x in m["markers"]] + [x["unit"] for x in m["unset"]]
    assert "L31" in names and "E60" in names
    assert not any(n.upper().startswith("DPT") for n in names)                 # on a call, housed: neither
    for code in ("DPT239", "DPT266", "dpt7700", "DPT242"):
        u = {"name": code, "kind": "other"}
        assert mapview.is_placeholder(u) and mapview.icon_of(u) == ""
        assert mapview.place(u, "onscene", {"name": hq, "ll": [-73.66692, 41.00253]},
                             {"id": 1, "lat": 41.0, "lon": -73.67}, None, 0.0, fire=True) is None
    assert not mapview.is_placeholder({"name": "CHIEF239"}) and not mapview.is_placeholder({"name": "239EOC"})


@needs_node
def test_a_placeholder_that_reaches_the_page_is_ignored(tmp_path):
    """An older hub's (or a feed's) DPT marker at HQ and at the call: no chip, not in the count, not in the list, not
    followable."""
    data = page_state()
    e60 = next(m for m in data["map"]["markers"] if m["unit"] == "E60")
    l31 = next(m for m in data["map"]["markers"] if m["unit"] == "L31")
    data["map"]["markers"] += [dict(e60, unit="DPT239", id=9001, kind="other", icon="other"),
                               dict(l31, unit="DPT266", id=9002, kind="other", icon="other", station=e60["station"])]
    o = run(tmp_path, SNAP + r"""
zoomTo(13);
out.chips = [L.chips.has("DPT239"), L.chips.has("DPT266")];
out.count = L.badges.get(IN.hq).el.textContent;
clickHouse(IN.hq); out.rows = rows().map((r) => r.dataset.unit);
out.follow = [CadMap.followUnit("DPT239"), CadMap.focusUnit("DPT266"), rm.followUnit("DPT239")];
zoomTo(15.5); out.full = L.chips.has("DPT239");
""", data)
    assert o["chips"] == [False, False] and o["full"] is False
    assert o["count"] == "4"                                                    # 2391, E60, E61, TL2 only
    assert not any(u.startswith("DPT") for u in o["rows"]) and len(o["rows"]) == 7
    assert o["follow"] == [False, False, False]
