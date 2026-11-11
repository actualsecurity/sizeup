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
            "M40": "other", "DPT239": "", "239EOC": "other", "77A1": "other", "PCPD": "other", "239_Eng": "other",
            "AP7": "engine", "AP11": "engine", "AP17": "engine"}                   # the airport's ARFF crash trucks
    for code, icon in want.items():
        assert mapview.icon_of({"name": code, **units[code]}) == icon, code
    # board-only codes (no units entry: kind "agency"): battalion and fire coordinators' cars; fire marshals generic
    boards = {c for b in json.loads((ROOT / "data" / "cad_lists.json").read_text())["unit_boards"] for c in b.get("units") or [] if c}
    for code, icon in {"BAT10": "suv", "BAT19": "suv", "FC1": "suv", "FC2": "suv", "FM1": "other", "FM42": "other"}.items():
        assert code in boards and code not in units, code
        assert mapview.icon_of({"name": code, "kind": "agency"}) == icon, code
    by_kind = {"engine": "engine", "ladder": "ladder", "tower": "tower", "rescue": "rescue", "chief": "suv", "utility": "suv"}
    for code, u in units.items():                          # every unit on the board: by its kind, a known icon
        icon = mapview.icon_of({"name": code, **u})
        if code.upper().startswith("DPT"):                   # a department placeholder: no icon, never drawn
            assert icon == "", code
            continue
        assert icon in mapview.ICONS, code
        by_code = "other"
        if u.get("kind") in ("", "other", None):                                  # CHIEF239: a chief's car; AP7: ARFF
            by_code = "suv" if code.upper().startswith("CHIEF") else "engine" if re.match(r"^AP\d+$", code) else "other"
        assert icon == by_kind.get(u.get("kind"), by_code), code
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
    assert title.startswith(f"{hq}\n4 in quarters: ") and sorted(title.split(": ")[1].split(", ")) == ["2391", "E60", "E61", "TL2"]
    assert aria == f"{hq}, 4 in quarters"
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
    assert hidden is False and head.replace("\u00a0", " ") == f"{page_state()['hq']} 7 rigs · 4 in quarters · 2 on calls · 1 OOS"
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
    c.store.assign(inc["id"], [uid("DPT239")], "Lt. Example")                  # as the county assigns it (#400)
    with c.store.tx() as db:                                                   # forced on location (the store refuses)
        db.execute("UPDATE units SET status = 'OL' WHERE id = ?", (uid("DPT239"),))
    c.attach(inc["id"], {"unit_ids": [uid("L31")], "status": "OL", **OP})
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
    """A placeholder's marker that reaches the page (the hub never sends one) at HQ and at the call, known by its unit
    row's flag or its own: no chip, not in the count, not in the list, not followable."""
    data = page_state()
    e60 = next(m for m in data["map"]["markers"] if m["unit"] == "E60")
    l31 = next(m for m in data["map"]["markers"] if m["unit"] == "L31")
    data["map"]["markers"] += [dict(e60, unit="DPT239", id=9001, kind="other", icon="other"),
                               dict(l31, unit="DPT266", id=9002, kind="other", icon="other", station=e60["station"])]
    o = run(tmp_path, SNAP + r"""
cad.state.units = [{ name: "DPT239", placeholder: true }, { name: "E60", placeholder: false }];   // their unit rows' flag
setMap({ ...IN.map, markers: IN.map.markers.map((m) => (m.unit === "DPT266" ? { ...m, placeholder: true } : m)) });
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


def test_a_placeholder_set_on_location_by_hand_is_still_nowhere():
    """A person sets DPT239 On location at a call (and gives it a home): it is still not drawn, not located (the
    recommendation's "where"), not in a count nor in the unset list; a fix fed for it is ignored too."""
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    hq = c.firehouses[0]["name"]
    for n in ("DPT239", "E60", "E61"):
        c.unit_station(uid(n), {"station": hq, **OP})
    inc = c.create({"type": "ALARM", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, **OP})
    c.store.assign(inc["id"], [uid("DPT239")], "Lt. Example")                  # as the county assigns it (#400)
    with c.store.tx() as db:                                                   # forced on location (the store refuses
        db.execute("UPDATE units SET status = 'OL' WHERE id = ?", (uid("DPT239"),))   # it now, #400: as a stale row)
    c._feed_position({"unit": "DPT239", "lat": 41.0101, "lon": -73.6702, "at": Clock()() - 5, "source": "test"})
    m = c.state()["map"]
    assert not any(x["unit"] == "DPT239" for x in m["markers"] + m["unset"])
    assert not any(x["unit"] == "DPT239" for x in m.get("routes") or [])
    where = c.locations()
    assert where["DPT239"] is None and where["E60"] is not None
    on = next(u for u in c.store.units() if u["name"] == "DPT239")
    assert on["status"] == "OL"                                                  # it is on the call, only not on the map
    assert mapview.locate(on, "onscene", {"name": hq, "ll": [-73.66692, 41.00253]},
                          {"id": inc["id"], "lat": 41.0085, "lon": -73.66}, None, 0.0) is None
    # the idle ones at HQ: E60 and E61 only (DPT239 counts nowhere)
    idle = [x["unit"] for x in m["markers"] if x["station"] == hq and x["look"] == "available"]
    assert sorted(idle) == ["E60", "E61"]


def test_is_placeholder_is_the_rows_flag_then_lists_placeholder():
    """The one rule (#400): a unit row's own placeholder flag, else the CAD's Lists.placeholder (cad_lists.json
    unit_placeholders); no pattern of its own."""
    from sizeup.cad.lists import load
    L = load()
    for code in ("DPT239", "DPT266", "dpt7700", "CHIEF239", "239EOC", "E60"):
        assert mapview.is_placeholder(code, L) is L.placeholder(code) is mapview.is_placeholder({"name": code}), code
        assert mapview.is_placeholder({"unit": code}) is L.placeholder(code), code
    assert mapview.is_placeholder({"name": "E60", "placeholder": True})                # the row's flag first
    assert not mapview.is_placeholder({"name": "DPT239", "placeholder": False})
    assert mapview.place({"name": "DPT239", "kind": "other"}, "free", {"name": "HQ", "ll": [-73.67, 41.0]}, lists=L) is None
    assert mapview.locate({"name": "DPT239"}, "free", {"name": "HQ", "ll": [-73.67, 41.0]}, lists=L) is None
    src = (ROOT / "sizeup" / "cad" / "mapview.py").read_text()
    body = src[src.index("def is_placeholder("):src.index("# By code where the kind")]
    assert "DPT" not in body.split('"""')[2] and "startswith" not in body                 # no fallback pattern left


def test_call_pins_over_the_count_chips_and_rigs_over_both():
    css = (STATIC / "cadmap.css").read_text()
    z = lambda sel: int(re.search(re.escape(sel) + r" \{[^}]*?z-index: (\d+)", css).group(1))  # noqa: E731
    assert z(".cm-badge") < z(".cm-pin") < z(".cm-rig")


@needs_node
def test_the_list_closes_when_the_calls_change_or_on_a_map_click(tmp_path):
    """Never left over the wall board: a change in the open calls closes it; a click on the map (off a firehouse)
    closes it; a pan or a zoom doesn't, nor a click on a firehouse (that one opens its list)."""
    o = run(tmp_path, SNAP + r"""
zoomTo(13); clickHouse(IN.hq);
rm.map.emit("movestart", { originalEvent: {} }); rm.map.emit("moveend"); rm.map.emit("zoomend");
setMap({ ...IN.map }); out.panned = [L.pop.hidden, L.stationOpen];
rm.map.emit("click", { point: { x: 1, y: 1 } }); out.clicked = [L.pop.hidden, L.stationOpen];
clickHouse(IN.se); out.house = [L.pop.hidden, L.stationOpen];
const p0 = IN.map.incidents[0];
setMap({ ...IN.map, incidents: [...IN.map.incidents, { ...p0, id: p0.id + 1, number: "26-999999", lat: 41.012, lon: -73.672 }] });
out.calls = [L.pop.hidden, L.stationOpen];
// another box (the station picker, a rig's) is not closed by a map click
L.openInfo(cad.state.map.markers.find((m) => m.unit === "E60")); rm.map.emit("click", { point: { x: 1, y: 1 } });
out.info = L.pop.hidden;
""", page_state())
    hq, se = page_state()["hq"], page_state()["se"]
    assert o["panned"] == [False, hq]
    assert o["clicked"] == [True, None]
    assert o["house"] == [False, se]
    assert o["calls"] == [True, None]
    assert o["info"] is False


@needs_node
def test_a_refresh_keeps_the_focused_row_and_the_hover_ring(tmp_path):
    o = run(tmp_path, SNAP + r"""
Object.defineProperty(document, "activeElement", { get: () => focused, configurable: true });
zoomTo(13); clickHouse(IN.hq);
rowOf("E61").focus();
rowOf("TL2").fire("mouseenter");
const moved = IN.map.markers.map((m) => (m.unit === "E60" ? { ...m, status: "DP", look: "dispatched" } : m));
setMap({ ...IN.map, markers: moved });
out.kept = [focused && focused.dataset.unit, focused === rowOf("E61"), cls("TL2").includes("hot"), kind("TL2")];
// the hovered rig leaves the firehouse's list (moved to another home): its ring cleared; the focused row gone: the box
const gone = moved.filter((m) => !["TL2", "E61"].includes(m.unit));
setMap({ ...IN.map, markers: gone });
out.gone = [L.hoverRow, CadMap.lights.hovered("TL2"), focused === L.pop, L.stationOpen === IN.hq];
""", page_state())
    assert o["kept"] == ["E61", True, True, "full"]
    assert o["gone"] == ["", False, True, True]


# ---- Store.units(): one row a unit, its latest open call, with incident_ids (no-dept-units, #400) -------------------------

HQ_FH = {"name": "PCFD Headquarters", "ll": [-73.66692, 41.00253], "short": "HQ"}
CALLS = [{"id": 11, "number": "26-000011", "status": "active", "lat": 41.0085, "lon": -73.66},
         {"id": 12, "number": "26-000012", "status": "active", "lat": 41.012, "lon": -73.672}]


def units_rows():
    """The #400 shape, made up: one row per unit; E60 on its latest call (12) and still on 11; E61 on one; E62 idle at
    home; DPT239 (a placeholder) on both."""
    row = lambda n, kind, st, inc, ids: {"id": len(n) * 100 + sum(map(ord, n)), "name": n, "kind": kind, "status": st,  # noqa: E731
                                         "board": "main", "station": HQ_FH["name"], "incident_id": inc, "incident_ids": ids}
    return [row("E60", "engine", "RS", 12, [11, 12]), row("E61", "engine", "OL", 11, [11]),
            row("E62", "engine", "AV", None, []), row("E63", "engine", "AV", None, []),
            row("DPT239", "other", "OL", 12, [11, 12])]


def slice_rows():
    role = {"RS": "enroute", "OL": "onscene", "AV": "free"}.get
    return mapview.map_slice(units_rows(), CALLS, role, [HQ_FH], positions={}, now=0.0)


def test_a_units_row_with_several_calls_is_one_marker_with_every_call():
    s = slice_rows()
    by = {m["unit"]: m for m in s["markers"]}
    assert sorted(by) == ["E60", "E61", "E62", "E63"]                            # one each; the placeholder none
    assert by["E60"]["incident_id"] == 12 and by["E60"]["incident_numbers"] == ["26-000012", "26-000011"]
    assert by["E61"]["incident_numbers"] == ["26-000011"] and by["E62"]["incident_numbers"] == []
    assert not any(u["unit"] == "DPT239" for u in s["unset"])


@needs_node
def test_the_list_shows_all_of_a_rigs_calls_and_counts_each_rig_once(tmp_path):
    s = slice_rows()
    data = page_state()
    data["map"] = {**data["map"], "markers": s["markers"], "incidents": s["incidents"], "unset": []}
    o = run(tmp_path, SNAP + r"""
zoomTo(13);
out.chips = [...L.chips.keys()].sort();
out.count = L.badges.has(IN.hq) ? L.badges.get(IN.hq).el.textContent : null;
clickHouse(IN.hq);
out.rows = rows().map((r) => [r.dataset.unit, r.children.length > 3 ? r.children[3].textContent : ""]);
rowOf("E60").fire("click"); out.follow = rm.fol.id;
""", data)
    assert o["chips"] == ["E60", "E61", "E62", "E63"]
    assert o["count"] == "2"                                                     # E62, E63 in quarters, once each
    assert o["rows"] == [["E60", "26-000012, 26-000011"], ["E61", "26-000011"], ["E62", ""], ["E63", ""]]
    assert o["follow"] == "E60"


# ---- placeholders on the map side, whatever Store.units()'s shape (the #400 review) ------------------------------------


def per_assignment_rows():
    """The other shape: one row per assignment (E60 and DPT239 on calls 11 and 12: two rows each)."""
    rows = []
    for u in units_rows():
        ids = u["incident_ids"] or [None]
        for i in ids:
            rows.append({k: v for k, v in u.items() if k != "incident_ids"} | {"incident_id": i})
    return rows


def test_either_units_shape_gives_one_marker_a_unit_and_no_placeholder():
    role = {"RS": "enroute", "OL": "onscene", "AV": "free"}.get
    for rows in (units_rows(), per_assignment_rows()):
        s = mapview.map_slice(rows, CALLS, role, [HQ_FH], positions={}, now=0.0)
        names = [m["unit"] for m in s["markers"]]
        assert sorted(names) == ["E60", "E61", "E62", "E63"], names              # one each
        e60 = next(m for m in s["markers"] if m["unit"] == "E60")
        assert e60["incident_id"] == 12 and e60["incident_numbers"] == ["26-000012", "26-000011"]
        assert not any(u["unit"] == "DPT239" for u in s["unset"])
    assert len(per_assignment_rows()) == 7


def test_a_placeholder_on_a_located_call_gets_no_incident_marker_nor_an_unset_entry():
    """DPT239 is kind other, response fire (is_fire true): on a call with a point, on location or not, with a station or
    none, free or on a call: never a marker (place "incident" included), never in map.unset."""
    role = {"RS": "enroute", "OL": "onscene", "AV": "free"}.get
    base = {"id": 9001, "name": "DPT239", "kind": "other", "response": "fire", "board": "main"}
    assert mapview.is_fire(base)
    for st in (HQ_FH["name"], ""):
        for status, inc in (("OL", 11), ("RS", 11), ("AV", None)):
            u = dict(base, status=status, station=st, incident_id=inc, incident_ids=[inc] if inc else [])
            s = mapview.map_slice([u], CALLS, role, [HQ_FH], positions={}, now=0.0)
            assert s["markers"] == [] and s["unset"] == [], (st, status)
            assert mapview.place(u, role(status), HQ_FH if st else None, CALLS[0] if inc else None, None, 0.0) is None


def test_a_placeholder_is_never_routed_nor_trailed():
    from sizeup.cad import eta, routes
    calls = []
    r = routes.Routes(graph=lambda: object(), params=lambda: eta.Params(), spawn=lambda fn: fn(), log=lambda m: None,
                      clock=lambda: 1000.0)
    r._route = lambda g, m, *a, **k: calls.append(m["unit"])                   # what would be routed
    r.penalties = lambda g, now: ({}, ())
    going = lambda n: {"unit": n, "look": "responding", "incident_id": 11, "line_to": [-73.66, 41.0085],  # noqa: E731
                       "lon": -73.6669, "lat": 41.0025, "place": "position", "at": 999.0}
    out = r.for_map([going("DPT239"), going("E60")], 1000.0, CALLS)
    assert calls == ["E60"] and "DPT239" not in r.trails and "E60" in r.trails
    assert all(x.get("unit") != "DPT239" for x in out["routes"])


def test_a_placeholder_is_never_recommended():
    c = Cad(Settings(), log=lambda m: None, clock=Clock())
    uid = lambda n: next(u["id"] for u in c.store.units() if u["name"] == n)  # noqa: E731
    for n in ("DPT239", "E60"):
        c.unit_station(uid(n), {"station": c.firehouses[0]["name"], **OP})
    inc = c.create({"type": "STRU", "address": "100 Example Ave", "lat": 41.0085, "lon": -73.6600, **OP})
    r = c.recommend(inc["id"], {})
    assert "E60" in [u["name"] for u in r["units"]]
    assert "DPT239" not in [u["name"] for u in r["units"]]
    assert uid("DPT239") not in (r.get("others") or {})


def test_the_page_never_offers_a_placeholder_to_follow_or_dispatch():
    js = (STATIC / "cad.js").read_text()
    assert "const onMap = (name) => !placeholderUnit(name) &&" in js and "return !!(u && u.placeholder);" in js               # every Follow button is gated on onMap
    for line in [ln for ln in js.splitlines() if "followBtn(" in ln and "function followBtn" not in ln]:
        assert "onMap(" in line, line
    assert 'r.units = (r.units || []).filter((u) => !placeholderUnit(u.name));' in js
    cm = (STATIC / "cadmap.js").read_text()
    assert "u.placeholder" in cm and "DPT" not in "".join(ln for ln in cm.splitlines() if not ln.strip().startswith("//"))
    assert "DPT" not in "".join(ln for ln in js.splitlines() if not ln.strip().startswith("//"))


def test_a_row_flagged_placeholder_is_skipped_whatever_its_code():
    """#400's final shape: one row a unit with placeholder and incident_ids. The row's flag first: a made-up "X239"
    flagged, dispatched (DP) on two located calls, is no marker (no place "incident"), not unset, not routed, not
    located; a real rig flagged False is drawn as ever."""
    role = {"DP": "dispatched", "RS": "enroute", "AV": "free"}.get
    ph = {"id": 9100, "name": "X239", "kind": "other", "response": "fire", "board": "main", "status": "DP",
          "station": HQ_FH["name"], "incident_id": 12, "incident_ids": [11, 12], "placeholder": True}
    e60 = {"id": 9101, "name": "E60", "kind": "engine", "board": "main", "status": "RS", "station": HQ_FH["name"],
           "incident_id": 11, "incident_ids": [11], "placeholder": False}
    assert mapview.is_placeholder(ph) and not mapview.is_placeholder(e60)
    assert mapview.is_placeholder({"name": "DPT239", "placeholder": True})
    s = mapview.map_slice([ph, e60], CALLS, role, [HQ_FH], positions={}, now=0.0)
    assert [m["unit"] for m in s["markers"]] == ["E60"] and s["unset"] == []
    assert mapview.locations([ph, e60], CALLS, role, [HQ_FH])["X239"] is None
    assert mapview.place(dict(ph, status="DP"), "dispatched", None, CALLS[1], None, 0.0) is None
    from sizeup.cad import eta, routes
    calls = []
    r = routes.Routes(graph=lambda: object(), params=lambda: eta.Params(), spawn=lambda fn: fn(), log=lambda m: None,
                      clock=lambda: 1000.0)
    r._route = lambda g, m, *a, **k: calls.append(m["unit"])
    r.penalties = lambda g, now: ({}, ())
    m = {"unit": "X239", "placeholder": True, "look": "dispatched", "incident_id": 12, "line_to": [-73.672, 41.012],
         "lon": -73.6669, "lat": 41.0025}
    r.for_map([m], 1000.0, CALLS)
    assert calls == []
