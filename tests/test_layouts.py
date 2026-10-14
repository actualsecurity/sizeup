"""The Overview's layouts (GitHub #147): Map · Cameras · Side by side · Stacked, picked in the Overview's head
(the console and the wall board), remembered per screen; the map | cameras divider (panels.js's separator, #139); the
cameras on the map (camoverlay.js), a toggle in the Overview map's Layers box since #168, not a layout ("overlay
should only be an option in layers, not here"), a saved Overlay layout read once as Map with the toggle on; a phone
with Map and Cameras only. Static checks: the page, the scripts and the styles agree. The layouts themselves are
checked in a browser at 1366, 1920 and 390 (the PR has the numbers)."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
MODES = ["map", "cameras", "side", "stacked"]


def read(name):
    return (STATIC / name).read_text()


def in_try(js: str, at: int) -> bool:
    t = js.rfind("try {", 0, at)
    return t >= 0 and js.count("{", t, at) > js.count("}", t, at)


def test_the_picker_and_the_view():
    html = read("cad.html")
    head = re.search(r'<div class="ov-head view-head">(.*?)</div>\s*<div id="ov-pages"', html, re.S).group(1)
    picker = re.search(r'<span id="ov-layout" class="seg ov-layout" role="group" aria-label="Overview layout">(.*?)</span>', head, re.S).group(1)
    assert re.findall(r'data-layout="(\w+)"', picker) == MODES
    assert picker.count('aria-pressed="true"') == 1 and 'data-layout="map" aria-pressed="true"' in picker
    assert '<button id="ov-swap"' in head and "hidden" in re.search(r'<button id="ov-swap"[^>]*>', head).group(0)
    view = re.search(r'<div id="ov-view" class="ov-view">(.*?)</section>', html, re.S).group(1)
    assert [m for m in re.findall(r'(ov-mapbox|id="ov-split"|id="cw-wall")', view)] == ["ov-mapbox", 'id="ov-split"', 'id="cw-wall"']
    order = [html.index(f'src="/static/{s}"') for s in ("maplayers.js", "cadmap.js", "panels.js", "camwall.js", "camoverlay.js")]
    assert order == sorted(order)                                    # the separator before its users, the tiles before the overlay


def test_remembered_per_screen_with_the_old_switch_read_once():
    js = read("camwall.js")
    assert 'const KEY = "sizeup.overviewLayout", OLD_KEY = "sizeup.cameraWall"' in js
    assert 'const SCREEN = BOARD ? "monitor" : "overview"' in js
    assert 'const MODES = ["map", "cameras", "side", "stacked"]' in js
    assert 'if (!s.mode && (JSON.parse(localStorage.getItem(OLD_KEY)' in js and 's = { mode: "cameras" }' in js
    for m in re.finditer(r"localStorage\.\w+Item", js):
        assert in_try(js, m.start()), m.group(0)
    assert "all[SCREEN] = { ...L }" in js                           # {mode, swap, side, stacked} per screen


def test_a_phone_has_map_and_cameras_only():
    js = read("camwall.js")
    assert 'const PHONE = matchMedia("(max-width: 640px)")' in js
    assert 'PHONE.matches && !["map", "cameras"].includes(L.mode) ? "map" : L.mode' in js
    css = read("cadmap.css")
    assert ('@media (max-width: 640px) { .ov-layout button:is([data-layout="side"], [data-layout="stacked"]) { display: none; } }'
            in css)


def test_each_layout_lays_out():
    css = read("cadmap.css")
    assert '.ov-grid:is([data-layout="side"], [data-layout="stacked"]) .ov-mapbox { flex: 0 0 var(--ov-split, 50%); }' in css
    assert '.ov-grid[data-layout="side"] .ov-view { flex-direction: row; }' in css
    assert '.ov-grid[data-swap="true"] .ov-mapbox { order: 3; }' in css            # stacked, the cameras on top
    for m in ("cameras", "side", "stacked"):
        assert f'html.board .ov-grid[data-layout="{m}"]' in css                    # the wall board gives the view more room
    js = read("camwall.js")
    assert 'const wallShown = () => ["cameras", "side", "stacked"].includes(mode())' in js
    assert 'show(mapbox, m !== "cameras")' in js and "show(split, split2())" in js
    assert 'grid.dataset.layout = m' in js and 'grid.dataset.swap = String(m === "stacked" && L.swap)' in js


def test_the_divider_is_the_139_separator():
    js = read("camwall.js")
    sep = js[js.index("const sep = Panels.separator(split, {"):js.index("});", js.index("const sep = Panels.separator(split, {"))]
    for k in ("orient: () =>", "grow: () =>", "min: () =>", "max: () =>", "value: mapPx", "enabled: split2", "set: (px, done)", "reset: () =>"):
        assert k in sep, k
    assert "L[mode()] = 0.5" in sep                                                 # Home / double-click: half and half
    assert "const SPLIT_MIN = 200" in js
    panels = read("panels.js")
    assert "function separator(hd, o)" in panels and "return { widths, SIDES, separator, resizeMaps }" in panels
    assert "for (const m of MapLayers.maps) { try { m.map.resize(); }" in panels    # the map resizes live
    css = read("cadmap.css")
    assert ".ov-split { position: relative; flex: 0 0 var(--s-6); cursor: row-resize; touch-action: none; }" in css   # a 24 px target
    assert '.ov-split[data-orient="vertical"] { cursor: col-resize; }' in css


def test_overlay_is_not_a_layout():
    html = read("cad.html")
    assert 'data-layout="overlay"' not in html and ">Overlay<" not in html
    for name in ("cadmap.css", "cad.css"):
        assert 'data-layout="overlay"' not in read(name), name                     # no CSS left for the old layout
    js = read("camwall.js")
    assert '"overlay"' not in js[js.index("const MODES"):js.index("\n", js.index("const MODES"))]


def test_a_saved_overlay_layout_opens_as_map_with_the_toggle_on():
    js = read("camwall.js")
    load = js[js.index("  function load() {"):js.index("  function save() {")]
    assert 'if (s.mode === "overlay") { s = { ...s, mode: "map" }; wasOverlay = true; }' in load
    assert load.index('s.mode === "overlay"') < load.index("MODES.includes(s.mode)")   # before the unknown-mode default
    assert 'const OVERLAY = "camoverlay"' in js
    assert 'if (wasOverlay) { MapLayers.setLayer(OVERLAY, true, "overview"); save(); }' in js   # once: saved as Map from then on
    ml = read("maplayers.js")
    assert 'function setLayer(key, on, profile = "overview")' in ml and "const KEY_RE = /^[a-z][a-z0-9_:-]{0,47}$/;" in ml
    assert re.match(r"^[a-z][a-z0-9_:-]{0,47}$", "camoverlay")


def test_the_overlay_is_a_layers_toggle_on_the_overview_map():
    js = read("camoverlay.js")
    assert "const KEY = E.OVERLAY;" in js and "OVERLAY," in read("camwall.js")
    assert 'MapLayers.addGroup({ attach: (control) => (isOverview(control) ? new OverlayGroup(control) : {}) });' in js
    assert 'closest("#ov-map")' in js                                               # the Overview's map only
    assert "const isOn = () => !!(o.control && o.control.on(KEY, false));" in js    # off by default, the viewer's set
    assert "keys() { return [[KEY, false]]; }" in js and "refresh() { render(); }" in js
    assert "this.control.setLayer(KEY, cb.checked)" in js and "cb.dataset.key = KEY;" in js
    assert "this.top = true;" in js                                                 # drawn by Cameras, not at the box's foot
    assert "localStorage" not in js                                                 # remembered by maplayers.js's prefs
    assert 'const overlay = mode !== "cameras" && isOn();' in js                      # wherever the map shows
    ml = read("maplayers.js")
    assert "for (const g of this.groups) { const el = g.top ? groupPanel(g) : null; if (el) panel.append(el); }" in ml
    assert "for (const g of this.groups) { const el = g.top ? null : groupPanel(g); if (el) panel.append(el); }" in ml
    assert ml.index("g.top ? groupPanel(g)") > ml.index("panel.append(list);")


def test_a_camera_is_one_stream_across_the_wall_and_the_overlay():
    js = read("camwall.js")
    assert "x !== t && x.id === t.id && (x.live || x.starting)" in js
    assert "live < budget() && !twin(t)) { live++; startLive(t); }" in js           # the hub's per-screen budget, one slot
    assert 'if (was && !twin(t)) fetch(streamUrl(t, "close")' in js                 # the twin's use isn't closed under it
    assert 'if (!t.visible || (t.set === "wall" && !wallShown())) continue;' in js   # a hidden wall's tiles don't restart


def test_the_overlay():
    js = read("camoverlay.js")
    assert "const MIN_ZOOM = 10" in js and "SIZES = [[15, 208], [14, 176], [13, 144], [12, 112], [11, 88], [MIN_ZOOM, 72]]" in js
    assert 'E.tile(c, "overlay", { compact: true })' in js                          # camwall.js's tiles: the same budget and rules
    assert 'for (const ev of ["move", "zoom", "resize", "rotate", "pitch"]) o.map.on(ev, schedule)' in js
    assert "requestAnimationFrame(() => { o.frame = 0; place(); })" in js           # once a frame while the map moves
    assert "CANDIDATES(W, H)" in js and "inside(r, box) && !placed.some((q) => overlaps(r, q))" in js   # nudged, kept on the map
    assert 'document.createElementNS(SVG, "line")' in js                            # the leader to the exact spot
    assert '"co-mark co-down"' in js and 'el("span", "ml-down-badge", "!")' in js    # down: the dim "!" only
    assert 'mode === "cameras" ? [] : cams.filter((c) => c.pin)' in js               # the event's cameras ringed without the overlay
    assert "t.el.hidden = !on" in js                                                # off the map or too far out: no tile, no stream
    wall = read("camwall.js")
    assert 'if (!wallShown()) for (const t of w.tiles.values()) if (t.set === "wall") stopLive(t)' in wall
    assert "STOP_MS" in wall and "now - t.away >= STOP_MS" in wall                   # a map pan past a tile doesn't churn streams
    css = read("cadmap.css")
    assert ".co-far .co-tile, .co-far .co-leader { display: none; }" in css
    assert ".co-layer { position: absolute; inset: 0; z-index: 1; pointer-events: none; overflow: hidden; }" in css


def test_the_event_cameras_stand_out_in_every_layout():
    css = read("cadmap.css")
    assert ".cw-tile.pinned" in css                                                 # the wall's tiles and the overlay's
    assert ".co-leader.pinned line" in css and ".co-ring-dot" in css and ".co-ring-lbl" in css   # the overlay's lines, the map's rings
