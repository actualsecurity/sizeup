// A call's building on the CAD maps (sizeup/cad/footprints.py): the county footprint(s) of the call's address, address
// range or named place, lit whole (one building with several street numbers lights up for any of them).
//
//   The data     the hub resolves it and sends it on the call (inc.building: {how, footprints: [{ref, geometry, addrs}]}:
//                the state push for an open call, the incident's full read for a closed one), so it is drawn at every zoom,
//                whether the Buildings layer is on or loaded at that zoom or not. Nothing is asked for here.
//   The maps     the call's map and the large map: that call's; the Overview and the wall board (/monitor), and the large
//                map with no call: every open call's. Redrawn only when what it shows changes (a new push, another call).
//   The look     an accent outline over a dark casing, a light fill of the same: readable on the graphite style, distinct
//                from the county layers' greys, the hydrants' marks and the selection; right over the county layers'
//                fills (the buildings'), under their lines, marks and labels (the hydrants, the heights), the pre-plan
//                marks, the rigs' lines and the firehouses.
//   Hover        the addresses the building holds ("40-44 Example St"), as the map's tooltip.
// Uses maplayers.js (MapLayers.addGroup: one per map) and cad.js's state (cad.state, cad.extra); loaded after both.
"use strict";

window.BldgHighlight = (() => {
  const SRC = "bh-src";
  const IDS = ["bh-fill", "bh-case", "bh-line"];
  const BELOW = ["pp-edit-fp", "pp-icons", "cm-lines", "cm-trail", "cm-routes", "firehouse"];   // preplan.js, cadmap.js
  const FILL = /^gis-.*-(fill|3d)$/;                                  // gislayers.js: a county polygon layer's fill
  const OPEN = ["queued", "pending", "active"];
  const DARK = "#15181B";

  // ---- pure helpers (tests/test_footprints.py runs these in node) ----------------------------------------------------
  // the calls' footprints as one collection, each footprint once (two calls at one building: the first's)
  function features(incs) {
    const seen = new Set(), out = [];
    for (const inc of incs || []) {
      for (const f of ((inc && inc.building) || {}).footprints || []) {
        if (!f || !f.geometry || seen.has(f.ref)) continue;
        seen.add(f.ref);
        out.push({ type: "Feature", geometry: f.geometry, properties: { ref: f.ref, addrs: f.addrs || "", inc: inc.id } });
      }
    }
    return { type: "FeatureCollection", features: out };
  }
  // the open calls with a building (the Overview's, the wall board's)
  const shown = (incs) => (incs || []).filter((i) => i && OPEN.includes(i.status) && i.building && (i.building.footprints || []).length);
  const title = (p) => (p && p.addrs) || "Call's building";
  const api = { features, shown, title, SRC, IDS };
  if (typeof document === "undefined" || !window.MapLayers || !MapLayers.addGroup) return api;

  function token(name, fallback) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch (_) { return fallback; }
  }
  const state = () => (typeof cad !== "undefined" && cad.state) || null;
  // what this map lights: its call (the newest copy of it), else, on the Overview / the wall board / the large map, every
  // open call's; any other map (the new call's) none
  function callsOf(m) {
    const all = (state() && state().incidents) || [];
    const f = m.control.focused;
    if (f) {
      const fresh = all.find((i) => i.id === f.id) || (typeof cad !== "undefined" && cad.extra && cad.extra.get && cad.extra.get(f.id)) || f;
      return [fresh];
    }
    const box = m.map.getContainer();
    return box.closest && box.closest("#ov-map, .cm-big") ? shown(all) : [];
  }

  const maps = new Set();
  class BHMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.sig = "";
      maps.add(this);
      this.sync();
    }
    ready() { try { return this.styled || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }
    order() { return this.map.getLayersOrder ? this.map.getLayersOrder() : (this.map.getStyle().layers || []).map((l) => l.id); }
    // the layer ours go under: the next after the last county fill (none: the first county layer or the lowest of BELOW)
    before() {
      const order = this.order().filter((id) => !IDS.includes(id));
      let last = -1;
      order.forEach((id, i) => { if (FILL.test(id)) last = i; });
      if (last >= 0) return order[last + 1] || null;
      return order.find((id) => id.startsWith("gis-") || BELOW.includes(id)) || null;
    }
    ensure() {
      const map = this.map;
      if (!map.getSource(SRC)) {
        const c = token("--accent-text", "#EE6475"), w = (a, b) => ["interpolate", ["linear"], ["zoom"], 12, a, 18, b];
        map.addSource(SRC, { type: "geojson", data: features([]) });
        const specs = [
          { id: "bh-fill", type: "fill", source: SRC, paint: { "fill-color": c, "fill-opacity": 0.16 } },
          { id: "bh-case", type: "line", source: SRC, layout: { "line-join": "round" },
            paint: { "line-color": DARK, "line-width": w(3.2, 6), "line-opacity": 0.8 } },
          { id: "bh-line", type: "line", source: SRC, layout: { "line-join": "round" }, paint: { "line-color": c, "line-width": w(1.6, 3) } },
        ];
        for (const s of specs) map.addLayer(s, this.before());
        const canvas = map.getCanvas();
        map.on("mousemove", "bh-fill", (e) => {
          const f = e.features && e.features[0];
          this.tip = f ? title(f.properties) : "";
          canvas.title = this.tip;
        });
        map.on("mouseleave", "bh-fill", () => { if (canvas.title === this.tip) canvas.title = ""; });
        map.on("styledata", () => this.keepOrder());        // a county layer added since: ours back over it
        this.styled = true;
      }
      this.keepOrder();
    }
    // ours right under the lowest of BELOW (the county layers go in under the rigs' lines too, so may land above)
    keepOrder() {
      if (this.moving || !this.map.getLayer("bh-line")) return;
      const order = this.order(), b = this.before(), at = order.indexOf("bh-fill");
      if (order[at + 1] === "bh-case" && order[at + 2] === "bh-line" && (order[at + 3] || null) === b) return;
      this.moving = true;
      try { for (const id of IDS) this.map.moveLayer(id, b); } catch (_) { /* the style is being replaced */ } finally { this.moving = false; }
    }
    sync() {
      if (!this.ready()) {
        if (!this.waiting) { this.waiting = true; this.map.once("idle", () => { this.waiting = false; this.sync(); }); }
        return;
      }
      try { this.ensure(); } catch (e) { console.error("building highlight", e); return; }
      const calls = callsOf(this);
      // (cad.hooks run on every render: the same building objects are not drawn again; a new push brings new ones)
      const objs = calls.map((i) => i && i.building);
      if (this.objs && objs.length === this.objs.length && objs.every((b, k) => b === this.objs[k])) return;
      this.objs = objs;
      const fc = features(calls);
      const sig = JSON.stringify(fc.features.map((f) => [f.properties.inc, f.properties.ref, f.properties.addrs]));
      if (sig === this.sig) return;
      this.sig = sig;
      this.map.getSource(SRC).setData(fc);
    }
    refresh() { this.sync(); }
    keys() { return []; }
  }

  MapLayers.addGroup({ attach: (control) => new BHMap(control) });
  if (typeof cad !== "undefined" && cad.hooks) cad.hooks.push(() => { for (const m of maps) m.sync(); });   // every push
  api.maps = maps;
  return api;
})();
