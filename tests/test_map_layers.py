"""The map's Layers box (#111): the county GIS point layers are marks from the sprite, not dots; the box keeps its
place on a toggle; two remembered sets per viewer (Overview, an event's map) and an event's own set kept on the
event (sizeup/cad/store.py map_layers, POST /api/cad/incident/<id>) for dispatchers and up."""
import json
import re
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi, needs
from sizeup.config import ConfigError
from sizeup.gis import DEFAULT_LAYERS
from tests.test_cad import make_cad
from tests.test_gis import load_cfg
from tests.test_roles import JULIAN, VAL, call, people

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
ICONS = ("hydrant", "fire-station", "police", "hospital", "nursing-home", "school", "worship", "ambulance", "train", "bus",
         "petroleum", "tank")


def js(name):
    return (STATIC / name).read_text()


def symbol(sprite, name):
    m = re.search(rf'<symbol id="i-gis-{name}"([^>]*)>(.*?)</symbol>', sprite, re.S)
    assert m, name
    return m.group(1), m.group(2)


# ---- the marks -------------------------------------------------------------------------------------------------------


def test_every_point_layer_has_a_mark_and_the_sprite_has_each_one():
    sprite = (STATIC / "icons.svg").read_text()
    for l in DEFAULT_LAYERS:
        if l.kind == "point":
            assert l.icon, f"{l.key}: a point layer needs a mark"
        if l.icon:
            assert l.icon in ICONS and f'id="i-gis-{l.icon}"' in sprite, (l.key, l.icon)
        assert "icon" in l.public() and "sub" in l.public()
    assert {l.icon for l in DEFAULT_LAYERS if l.icon} == set(ICONS)        # every mark drawn is used
    by = {l.key: l for l in DEFAULT_LAYERS}
    assert by["hydrants"].icon == "hydrant" and by["fire_stations"].icon == "fire-station" and by["fire_stations"].ours_field
    assert by["tanks_towers"].icon == "tank" and by["tanks_towers"].kind == "polygon"   # the mark by its toggle; the map stays polygons
    for name in ICONS:                                           # a filled silhouette: no stroke, paths only, cuts marked
        attrs, body = symbol(sprite, name)
        assert 'viewBox="0 0 24 24"' in attrs and 'fill="currentColor"' in attrs and 'stroke="none"' in attrs, name
        assert not re.search(r"<(?!path\b)[a-z]", body), f"{name}: paths only (Path2D draws them on the map)"
        for cut in re.findall(r'<path class="cut"([^>]*)>', body):
            assert 'fill="var(--raised' in cut, name
    hydrant = symbol(sprite, "hydrant")[1]
    assert hydrant.count("M") >= 5                                # a bonnet, a body, two nozzles and a base


def test_the_marks_are_drawn_from_the_sprite_in_the_layers_color_and_thin_out_when_dense():
    g = js("gislayers.js")
    assert 'fetch(window.ICONS_SVG || "/static/icons.svg"' in g and "symbol[id^=\"i-gis-\"]" in g and "new Path2D(" in g
    assert 'classList.contains("cut")' in g and "g.fillStyle = DARK" in g
    assert "this.map.addImage(id, iconImage(shape, color((l.style || {}).color" in g
    assert "this.map.addImage(`${id}-ours`, iconImage(shape, token(\"--accent\"" in g       # ours: the accent
    sym = re.search(r"\} else if \(icon\) \{(.*?)\} else \{", g, re.S).group(1)
    assert 'type: "symbol"' in sym and '"icon-allow-overlap": false' in sym and '"icon-ignore-placement": false' in sym
    assert '"icon-size": ["interpolate", ["linear"], ["zoom"], minzoom, 0.55 * size, minzoom + 2, size]' in sym
    assert '"text-optional": true' in sym and 'ours(icon, `${icon}-ours`)' in sym
    assert "g.strokeStyle = LIGHT" in g and "g.lineWidth = 3 / k" in g                   # the light rim
    assert "lab.append(cb, ...(mark ? [mark] : []), h(\"span\", \"\", l.label))" in g      # by the toggle
    assert "iconEl(l, !!props._ours)" in g and "(l.sub || l.label)" in g                   # at the top of the click box
    assert 'Icon(`gis-${l.icon}`)' in g and 'el.style.setProperty("--c"' in g
    # lines and polygons as they were
    assert 'type: "fill"' in g and 'type: "fill-extrusion"' in g and 'type: "line"' in g
    poly = re.search(r'if \(kind === "polygon"\) \{(.*?)\} else if \(kind === "line"\)', g, re.S).group(1)
    assert "icon" not in poly
    ml = js("maplayers.js")
    assert "showInfo(title, sub, lines, foot, cls, icon)" in ml and "if (icon) head.append(icon);" in ml
    css = (STATIC / "cadmap.css").read_text()
    assert ".ml-head > .ico { color: var(--c, var(--text-2)); }" in css
    assert ".ml-fold .check > .ico { color: var(--c, var(--text-2)); }" in css
    by = {l.key: l for l in DEFAULT_LAYERS}
    assert by["petroleum"].style["size"] > 1 and by["petroleum"].style["color"] == "--warn"     # a fuel site: bigger, hazard color
    assert by["petroleum"].sub.startswith("Petroleum bulk storage") and "fire or spill" in by["petroleum"].note


def test_a_config_entry_sets_or_refuses_a_mark(tmp_path):
    c = load_cfg(tmp_path, '[[gis.layers]]\nkey = "hydrants"\nicon = "tank"\nsub = "A hydrant"\n')
    h = {l.key: l for l in c.gis_layers}["hydrants"]
    assert h.icon == "tank" and h.sub == "A hydrant" and h.label == "Hydrants"
    with pytest.raises(ConfigError, match="icon"):
        load_cfg(tmp_path, '[[gis.layers]]\nkey = "hydrants"\nicon = "Bad Icon"\n')


# ---- the box keeps its place ---------------------------------------------------------------------------------------


def test_the_layers_box_keeps_scroll_focus_and_folds_on_a_toggle():
    ml = js("maplayers.js")
    body = re.search(r"    renderPanel\(\) \{(.*?)\n    \}\n", ml, re.S).group(1)
    assert "const scroll = panel.scrollTop;" in body and "panel.scrollTop = scroll;" in body
    assert "document.activeElement.dataset.key" in body and 'panel.querySelector(`[data-key="${CSS.escape(active)}"]`)' in body
    assert 'panel.querySelectorAll("details[data-fold]")' in body and "g.panel(folds)" in body
    assert "cb.dataset.key = l.key;" in body and 'cb.addEventListener("change", () => this.setLayer(l.key, cb.checked));' in body
    g = js("gislayers.js")
    assert "fold.dataset.fold = group;" in g and 'fold.addEventListener("toggle", () => { if (touched) MapLayers.setFold(g, fold.open); });' in g
    assert "typeof folds[group] === \"boolean\" ? folds[group] : MapLayers.fold(group)" in g
    assert "cb.dataset.key = gisKey(l);" in g and "renderPanel" not in g                 # the control redraws, nothing here does
    css = (STATIC / "cadmap.css").read_text()
    assert re.search(r"\.ml-panel > \.ml-head \{ position: sticky; inset-block-start: calc\(-1 \* var\(--s-2\)\);", css)
    assert "max-block-size: min(70vh, calc(100% - var(--ctl-sm) - var(--s-6)))" in css



def test_the_incident_cameras_start_folded_to_a_count_and_remember_the_viewer():
    """The incident tab's Cameras panel is one line until opened (#149); the Overview's small one is unchanged."""
    ml = js("maplayers.js")
    assert 'const CAMS_OPEN_KEY = "sizeup.cad.camsOpen";' in ml
    assert 'st.open = st.small ? isOpenEvent(inc) : stored(CAMS_OPEN_KEY) === "1";' in ml
    assert 'if (!st.small) remember(CAMS_OPEN_KEY, st.open ? "1" : "0");' in ml
    body = re.search(r"  function drawCams\(host, st, fresh\) \{(.*?)\n  \}\n", ml, re.S).group(1)
    fold = body.index("if (!st.open) {")
    assert "ml-cams-count" in body[fold:] and 'parts.push(`${down.length} down`)' in body
    assert body.index("host.replaceChildren(head);\n    if (!st.open) return;") > fold     # nothing but the header when folded

# ---- the sets: the viewer's two, and an event's own ---------------------------------------------------------------


def test_two_remembered_sets_per_viewer_and_an_events_own_set():
    ml = js("maplayers.js")
    assert "const BASE_ON = Object.fromEntries(LAYERS.filter((l) => l.on).map((l) => [l.key, true]));" in ml
    assert "const PROFILES = { overview: { ...BASE_ON }, incident: { ...BASE_ON } };" in ml
    assert "const p = { overview: {}, incident: {}, folds: {}, hideDown: false };" in ml
    assert 'stored(PREF_KEY)' in ml and 'stored("sizeup.gisLayers", true)' in ml       # the pre-#111 GIS store: read once, then dropped
    lines = ml.splitlines()
    uses = [i for i, ln in enumerate(lines) if "localStorage." in ln]
    assert len(uses) == 2                                             # the read (and drop) and the write: each inside a try
    for i in uses:
        assert "try {" in lines[i], lines[i].strip()
    assert "localStorage" not in js("gislayers.js")
    assert 'this.profile = opts.layers === "incident" ? "incident" : "overview";' in ml
    on = re.search(r"    on\(key, dflt\) \{(.*?)\n    \}\n", ml, re.S).group(1)
    assert on.index("this.force[key]") < on.index("this.override") < on.index("prefs[this.profile][key]") < on.index("PROFILES[this.profile][key]")
    assert 'const PROFILE_ON = { overview: [], incident: ["hydrants", "buildings"] };' in js("gislayers.js")
    cm = js("cadmap.js")
    assert 'MapLayers.attach(this.map, host, ctrls, { layers: opts.layers })' in cm
    assert cm.count('layers: "overview"') == 2 and cm.count('layers: "incident"') == 1       # the Overview and the large map; the event's
    # a dispatcher's change on an open event's map is kept on the event; a viewer's stays theirs
    s = re.search(r"    setLayer\(key, on\) \{(.*?)\n    \}\n", ml, re.S).group(1)
    assert "if (canWrite() && OPEN.includes(this.focused.status)) this.saveOverride(set);" in s
    assert "else { this.overrideLocal = true; prefs[this.profile][key] = on; savePrefs(); }" in s
    assert "fetch(`/api/cad/incident/${encodeURIComponent(id)}`, { method: \"POST\"" in ml and "map_layers: set" in ml
    assert 'canDo("dispatch")' in ml and '"Reset to default"' in ml and '"Layers for this event"' in ml
    assert "this.saveOverride(null)" in ml                            # the reset drops the event's set
    assert "inc.map_layers && typeof inc.map_layers === \"object\"" in ml
    for key in ("gis:3d", "hideDown", "reset", "close"):
        assert f'dataset.key = "{key}"' in ml + js("gislayers.js"), key
    assert "AirportLayer.near(inc)" in ml and "this.force.airport = on" in ml          # the airport's "event here" still forces it


def test_an_events_map_layers_round_trip_and_who_may(tmp_path):
    api = CadApi(make_cad(tmp_path))
    api.logged = []
    api.cad.log = api.logged.append
    assert needs("/api/cad/incident/12") == "dispatch"
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": "209 Westchester Ave", "operator": "JD"})
    assert code == 200
    iid = inc["id"]
    layers = {"cameras": True, "incidents": True, "gis:hydrants": True, "gis:fire_districts": False, "gis:buildings": True}
    code, out = call(api, f"/api/cad/incident/{iid}", {"map_layers": layers, "operator": "JD"})
    assert code == 200
    assert api.cad.store.full(iid)["map_layers"] == layers
    assert next(i for i in api.cad.state()["incidents"] if i["id"] == iid)["map_layers"] == layers
    log = [e["text"] for e in api.cad.store.full(iid)["events"] if e["kind"] == "edit"]
    assert log[-1] == "Edited map layers: chosen for this event" and "hydrants" not in log[-1]
    assert api.cad.store.incident(iid)["map_layers"] == json.dumps(dict(sorted(layers.items())), separators=(",", ":"))
    code, out = call(api, f"/api/cad/incident/{iid}", {"map_layers": layers, "operator": "JD"})    # the same again: nothing
    assert code == 200 and len([e for e in api.cad.store.full(iid)["events"] if e["kind"] == "edit"]) == 1
    for bad in ({"cameras": "yes"}, {"Bad Key": True}, [1], {f"k{i}": True for i in range(65)}, "all"):
        code, out = call(api, f"/api/cad/incident/{iid}", {"map_layers": bad})
        assert code == 400 and out["error"].startswith("map_layers:"), bad
    code, out = call(api, f"/api/cad/incident/{iid}", {"map_layers": None, "operator": "JD"})       # "Reset to default"
    assert code == 200 and api.cad.store.full(iid)["map_layers"] is None
    assert [e["text"] for e in api.cad.store.full(iid)["events"] if e["kind"] == "edit"][-1] == "Edited map layers: the default again"
    code, out = call(api, f"/api/cad/incident/{iid}", {"map_layers": {}, "operator": "JD"})
    assert code == 200 and api.cad.store.full(iid)["map_layers"] is None
    # roles on the hub: a dispatcher may, a viewer may not (the page keeps a viewer's choice in the browser)
    people(api.cad)
    assert call(api, f"/api/cad/incident/{iid}", {"map_layers": layers}, JULIAN)[0] == 200
    assert call(api, f"/api/cad/incident/{iid}", {"map_layers": None}, VAL)[0] == 403
    assert api.cad.store.full(iid)["map_layers"] == layers
    # a closed event keeps what it had
    assert call(api, f"/api/cad/incident/{iid}/close", {"disposition": "COMPLETE", "operator": "JD"}, JULIAN)[0] == 200
    code, out = call(api, f"/api/cad/incident/{iid}", {"map_layers": None}, JULIAN)
    assert code == 400 and "closed" in out["error"]
    assert api.cad.store.full(iid)["map_layers"] == layers


def test_an_old_database_gets_the_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    api = CadApi(make_cad(tmp_path))
    api.cad.store.db.close() if hasattr(api.cad.store.db, "close") else None
    con = sqlite3.connect(str(tmp_path / "cad.sqlite"))
    con.execute("ALTER TABLE incidents DROP COLUMN map_layers")
    con.commit()
    con.close()
    again = CadApi(make_cad(tmp_path))
    assert "map_layers" in {r["name"] for r in again.cad.store.q("PRAGMA table_info(incidents)")}
    assert not db.exists()
