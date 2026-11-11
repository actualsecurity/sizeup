"""The Vessels layer on the page (#402, sizeup/web/static/vessels.js), in node with a stand-in MapLibre map (the aircraft
follow tests' harness, tests/test_aircraft_follow.py): off by default, on by itself on a marine call's own map; a click
follows the vessel (mapfollow.js: one follower, a drag lets go) and opens its live box; the gentle dead-reckon; a marine
call's "Vessels nearby" and the vessels it names ringed.

Synthetic data only: the hub's own answer (sizeup/vessels.py) built from invented MMSIs and names."""

from __future__ import annotations

import pytest

from sizeup.vessels import VesselLayer, from_aisstream, is_marine
from tests.test_aircraft_follow import MAP, needs_node, run_node
from tests.test_vessels import KEY, FakeWS, class_b, pos_report, static5

SCRIPTS = ["motion.js", "mapfollow.js", "vessels.js"]
LOADED = r"""
const m = group.attach(control);
const flush = () => new Promise((r) => setTimeout(r, 0));
const pts = (id) => { const s = rec.sets[id] || []; return s.length ? s[s.length - 1].features.map((f) => f.properties) : []; };
"""


def answer():
    """The hub's /api/cad/vessels answer: a tug under way, a class B pleasure boat, a ferry with no name yet."""
    ly = VesselLayer(aisstream_key=KEY, connect=lambda: FakeWS([]))
    t = ly.clock()
    ly.fleet.apply(from_aisstream(pos_report(mmsi=999000001, lat=40.975, lon=-73.665, sog=8.0, cog=90.0, hdg=90)), t, "aisstream")
    ly.fleet.apply(from_aisstream(static5(mmsi=999000001, name="TEST TUG ONE", stype=52)), t, "aisstream")
    ly.fleet.apply(from_aisstream(class_b(mmsi=999000002, lat=40.98, lon=-73.68, sog=0.0, cog=90.0)), t, "aisstream")
    ly.fleet.apply(from_aisstream(static5(mmsi=999000002, name="TEST BREEZE", stype=37, dest="")), t, "aisstream")
    ly.fleet.apply(from_aisstream(pos_report(mmsi=999000003, lat=40.99, lon=-73.70, sog=12.0, cog=None, hdg=511)), t, "aisstream")
    d = ly.public()
    d.pop("at")
    return d


@needs_node
def test_the_layer_is_off_by_default_and_on_by_itself_for_a_marine_call(tmp_path):
    o = run_node(tmp_path, SCRIPTS, LOADED + r"""
out.keys = m.keys();
out.offByDefault = m.isOn();
control.profile = "incident";
control.focused = { id: 7, subtype: "STRUCTURAL", assignments: [] };
out.otherCall = m.isOn();
control.focused = { id: 7, subtype: "WATER", assignments: [] };
out.marineCall = m.isOn();
const box = m.panel();
const cb = box.all().find((e) => e.tagName === "INPUT");
out.forcedTag = box.textContent.includes("marine call");
cb.checked = false; cb.fire("change");
out.afterUntick = m.isOn();                              // holds for this call
control.focused = { id: 8, subtype: "", assignments: [{ name: "M40", kind: "marine" }] };
out.nextCall = m.isOn();
control.focused = { id: 9, subtype: "", assignments: [{ name: "DPT239", kind: "marine" }] };
out.placeholder = m.isOn();                              // a department placeholder never makes it marine
""", {"d": {}}, pre=MAP)
    assert o["keys"] == [["vessels", False]]
    assert o["offByDefault"] is False and o["otherCall"] is False and o["marineCall"] is True and o["forcedTag"] is True
    assert o["afterUntick"] is False and o["nextCall"] is True and o["placeholder"] is False


@needs_node
def test_a_clicked_vessel_is_followed_with_its_live_box_until_a_drag(tmp_path):
    d = answer()
    o = run_node(tmp_path, SCRIPTS, LOADED + r"""
(async () => {
  await VesselLayer.load(); await flush();
  out.marks = pts("vs-vessels").map((p) => [p.id, p.icon, p.name, p.detail]);
  control.map.queryRenderedFeatures = () => [{ properties: { id: "999000001" } }];
  m.clicked({ point: [0, 0], originalEvent: { stopPropagation() {} } });
  await flush();
  out.ease = rec.eases[0]; out.fired = [...rec.fired]; out.info = rec.info[0];
  const chip = host.find("ac-follow")[0];
  out.chip = [chip.find("ac-follow-t")[0].textContent, chip.hidden];
  out.ringed = pts("vs-hl").map((p) => p.id);
  out.view = m.view("999000001");
  out.view.lines = out.view.lines.map((l) => (typeof l === "string" ? l : l.textContent));
  out.view.icon = null;
  control.map.emit("dragstart", { originalEvent: { touches: [{}] } });
  out.afterDrag = [m.following, chip.hidden];
  m.follow("999000001");
  control.map.emit("sizeup:follow", { follower: {} });   // another layer (an aircraft, a rig) followed: one at a time
  out.afterOther = m.following;
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {"d": d, "layerOn": True}, pre=MAP)
    marks = {m[0]: m for m in o["marks"]}
    assert marks["999000001"][1] == "vs-tug" and marks["999000001"][3] == "Tug · 8.0 kn"
    assert marks["999000002"][1] == "vs-boat" and marks["999000002"][2] == "TEST BREEZE"
    assert marks["999000003"][1] == "vs-dot" and marks["999000003"][2] == "MMSI 999000003"   # no course, no name yet
    assert abs(o["ease"]["center"][0] - -73.665) < 0.01 and o["ease"]["zoom"] >= 13 and "sizeup:follow" in o["fired"]
    assert o["chip"] == ["Following TEST TUG ONE", False] and o["ringed"] == ["999000001"]
    assert o["info"] == ["TEST TUG ONE", "Tug · 20 m × 7 m beam"]
    v = o["view"]
    assert v["lines"][0] == "8.0 kn · 90° · Under way" and "MMSI 999000001 · XX0001" in v["lines"]
    assert v["foot"].startswith("aisstream.io · ") and "small craft" in v["tip"]
    assert o["afterDrag"] == ["", True] and o["afterOther"] == ""


@needs_node
def test_dead_reckoning_is_gentle_and_only_for_a_moving_vessel(tmp_path):
    o = run_node(tmp_path, SCRIPTS, r"""
const fix = { lon: -73.66, lat: 40.97, sog: 10, cog: 90, rot: 90 };
const m = (dt) => { const p = VesselLayer.drift(fix, dt); return p ? Math.round(Motion.metres(fix, p)) : null; };
out.d10 = m(10); out.d30 = m(30); out.d90 = m(90);
out.slow = VesselLayer.drift({ ...fix, sog: 0.3 }, 30);
out.noCourse = VesselLayer.drift({ ...fix, cog: null }, 30);
const p = VesselLayer.drift(fix, 30);
out.east = p.lon > fix.lon && Math.abs(p.lat - fix.lat) < 1e-6;
""", {}, pre="window.MapLayers = { addGroup() {} };")
    kt = 1852 / 3600
    assert abs(o["d10"] - 10 * kt * 10) <= 1 and abs(o["d30"] - 10 * kt * 30) <= 1
    assert abs(o["d90"] - 10 * kt * 60) <= 1             # past 30 s at half rate: never a surge
    assert o["slow"] is None and o["noCourse"] is None and o["east"] is True


@needs_node
def test_a_marine_calls_named_vessel_is_ringed_and_its_nearest_listed(tmp_path):
    d = answer()
    near = {"id": 7, "marine": True, "configured": True, "note": "", "stale_s": 180,
            "named": [v for v in d["vessels"] if v["id"] == "999000002"],
            "nearest": [dict(v, dist=x) for v, x in zip(sorted(d["vessels"], key=lambda v: v["id"]), (0.2, 0.6, 1.4))]}
    o = run_node(tmp_path, SCRIPTS, LOADED + r"""
global.fetch = async (u) => { rec.fetches++; return { ok: true, json: async () => (String(u).includes("/near") ? IN.near : IN.d) }; };
(async () => {
  await VesselLayer.load(); await flush();
  const panelHost = new El("div");
  VesselLayer.panel({ id: 7, status: "active", subtype: "BOAT", lat: 40.975, lon: -73.665, assignments: [] }, panelHost);
  await flush(); await flush();
  out.hidden = panelHost.hidden;
  out.rows = panelHost.find("ac-row").map((r) => r.textContent);
  out.named = VesselLayer.named();
  m.draw(true);
  out.ringed = pts("vs-hl").map((p) => [p.id, p.ringc, p.color]);
  out.plain = pts("vs-vessels").map((p) => p.id);
  panelHost.find("ac-row")[0].fire("click");
  await flush();
  out.following = m.following;
  VesselLayer.panel({ id: 8, status: "active", subtype: "STRUCTURAL", lat: 40.9, lon: -73.6, assignments: [] }, panelHost);
  out.otherHidden = panelHost.hidden; out.namedAfter = VesselLayer.named();
  console.log(JSON.stringify(out)); process.exit(0);
})();
""", {"d": d, "near": near, "layerOn": True}, pre=MAP)
    assert o["hidden"] is False and len(o["rows"]) == 3
    assert o["rows"][0].startswith("namedTEST BREEZE") and o["rows"][1].startswith("nearTEST TUG ONE")
    assert "0.2 nm" in o["rows"][1] and "8.0 kn" in o["rows"][1]
    assert o["named"] == ["999000002"] and o["ringed"][0][0] == "999000002" and o["ringed"][0][1] == o["ringed"][0][2]
    assert "999000002" not in o["plain"] and o["following"] == "999000002"
    assert o["otherHidden"] is True and o["namedAfter"] == []


@needs_node
@pytest.mark.parametrize("inc", [
    {"subtype": "WATER"}, {"subtype": "boat"}, {"subtype": "ACFT-WATER"}, {"subtype": "STRUCTURAL"}, {"subtype": ""},
    {"subtype": "", "assignments": [{"name": "M40", "kind": "marine", "cleared_at": None}]},
    {"subtype": "", "assignments": [{"name": "M40", "kind": "marine", "cleared_at": 5.0}]},
    {"subtype": "", "assignments": [{"name": "DPT266", "kind": "marine", "cleared_at": None}]},
])
def test_the_page_and_the_hub_agree_on_a_marine_call(tmp_path, inc):
    o = run_node(tmp_path, SCRIPTS, "out.marine = VesselLayer.isMarine(IN.inc);", {"inc": inc}, pre="window.MapLayers = { addGroup() {} };")
    assert o["marine"] is is_marine(inc)
