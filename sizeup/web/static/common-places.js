// Common places on the CAD maps: the department's place names with a point (the county's common places, imported in
// Settings > Lists > Place names, and the names made by hand there), as a layer in every map's Layers box.
//
//   Layer      "Common places" (maplayers.js MapLayers.addGroup; key "commonplaces", off by default): remembered like
//              the other layers (each viewer's Overview / incident set, an event's own set). One point per spot (the
//              hub's GET /api/cad/places/map, sizeup/cad/commonplaces.py map_points): a site once, at its primary;
//              names at one address together ("Example Burger +1").
//   Drawn      a small dot from DOT_ZOOM (town-wide, ~1,000 dots would only be noise) and the name from LABEL_ZOOM, one
//              symbol layer whose labels give way to each other and to the map's own (collision: no pile-up of names
//              on a busy block; zoom in and the hidden ones show). Map layers, never DOM marks: ~1,000 points cost
//              nothing to pan. Under the rigs' lines, the routes and the firehouses.
//   A click    on a dot or a name: a small box with each name at that spot, its addresses (all of a site's) and its
//              town; another layer's mark there (a pre-plan point, a firehouse, a camera) keeps its own click, and nothing
//              while a pre-plan point is being placed. Not on the new call's map: a click there is "what is here" (newcall-map.js), which lists the
//              names at that address and offers each as the location. A rig or a call pin (DOM marks) keep their own
//              clicks; a right-click / long press is Street View's (streetview.js) as before.
//   Data       asked when a map turns the layer on, then again only when the state's places_rev moves (a name added,
//              edited, removed, or a re-import: cad.js fires "sizeup:places"); the hub answers 304 while unchanged.
// Uses nothing from cad.js but the event: loaded after maplayers.js.
"use strict";

window.CommonPlaces = (() => {
  const URL = "/api/cad/places/map", KEY = "commonplaces";
  const SRC = "cp-places", IDS = ["cp-dot", "cp-label"];
  const DOT_ZOOM = 14, LABEL_ZOOM = 16;
  const DOT = "#B8A1E3", TEXT = "#D9DCE0", HALO = "#15181B";
  const st = { data: null, loading: null, stale: true };
  const maps = new Set();

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const empty = () => ({ type: "FeatureCollection", features: [] });
  const anyOn = () => [...maps].some((m) => m.isOn());

  // the hub's points: once a map has the layer on, again when the names change
  function load() {
    if (st.loading) return st.loading;
    st.stale = false;
    st.loading = fetch(URL, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((d) => { st.data = d && Array.isArray(d.features) ? d : empty(); for (const m of maps) m.draw(); })
      .catch((e) => { st.stale = true; console.error("common places", e.message); })
      .finally(() => { st.loading = null; });
    return st.loading;
  }
  function kick() { if ((st.stale || !st.data) && anyOn()) load(); }
  if (window.addEventListener) window.addEventListener("sizeup:places", () => { st.stale = true; kick(); });

  // one spot's box: each name, its addresses and town
  function describe(f) {
    const places = (f && f.places) || [];
    const lines = [];
    for (const p of places.length > 1 ? places : []) lines.push(h("b", "cp-name", p.name), ...lineOf(p));
    if (places.length === 1) lines.push(...lineOf(places[0]));
    const one = places[0] || { name: "", town: "" };
    const towns = [...new Set(places.map((p) => p.town).filter(Boolean))];
    const title = places.length > 1 ? `${places.length} places` : one.name;
    const foot = places.every((p) => p.hand) ? "Place names" : places.some((p) => p.hand) ? "Common places · place names" : "Common places";
    return [title, towns.join(", "), lines, foot];
  }
  const lineOf = (p) => (p.addresses || []).map((a) => h("div", "", a));

  class PlacesMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.added = false;
      this.sig = null;
      this.callMap = !!(control.host && control.host.id === "nc-map");     // newcall-map.js: its clicks are its own
      maps.add(this);
      this.map.on("click", (e) => this.clicked(e));
      this.sync();
    }
    isOn() { return this.control.on(KEY, false); }
    keys() { return [[KEY, false]]; }
    refresh() { this.sync(); }
    ready() { try { return this.added || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }

    add() {
      const map = this.map;
      if (map.getSource(SRC)) { this.added = true; return; }
      const ids = (map.getStyle().layers || []).map((x) => x.id);
      const before = ids.find((id) => id.startsWith("ml-") || id === "cm-lines" || id === "firehouse");
      const vis = { visibility: "none" };
      map.addSource(SRC, { type: "geojson", data: empty() });
      map.addLayer({ id: "cp-dot", type: "circle", source: SRC, minzoom: DOT_ZOOM, layout: vis,
                     paint: { "circle-color": DOT, "circle-radius": ["interpolate", ["linear"], ["zoom"], DOT_ZOOM, 2.5, 18, 5],
                              "circle-stroke-color": HALO, "circle-stroke-width": 1 } }, before);
      if (map.getStyle().glyphs) {
        map.addLayer({ id: "cp-label", type: "symbol", source: SRC, minzoom: LABEL_ZOOM,
                       layout: { ...vis, "text-field": ["get", "label"], "text-font": ["Noto Sans Regular"], "text-size": 11,
                                 "text-variable-anchor": ["top", "bottom", "left", "right"], "text-radial-offset": 0.7,
                                 "text-justify": "auto", "text-max-width": 9, "text-padding": 2 },
                       paint: { "text-color": TEXT, "text-halo-color": HALO, "text-halo-width": 1.4 } }, before);
      }
      for (const id of IDS) {
        if (!map.getLayer(id)) continue;
        map.on("mouseenter", id, () => { if (!this.callMap) map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
      }
      this.added = true;
    }
    sync() {
      const on = this.isOn();
      if (!on && !this.added) return;
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      try { this.add(); } catch (e) { console.error("common places layer", e); return; }
      for (const id of IDS) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
      if (on) { this.draw(); kick(); }
    }
    draw() {
      if (!this.added || !this.isOn() || !st.data || this.sig === st.data) return;
      this.sig = st.data;
      const src = this.map.getSource(SRC);
      if (src) src.setData(st.data);
    }
    // a click on a dot or a name: its box, unless another layer's mark is there too (a pre-plan point, a firehouse, a
    // camera, a hydrant: theirs, as gislayers.js yields to the highway layers), or a pre-plan point is being placed; an
    // area (a parcel, a district, a flood zone) drawn over the dots never takes it
    clicked(e) {
      if (this.callMap || !this.added || !this.isOn() || !st.data) return;
      if (window.Preplan && Preplan.editing && Preplan.editing()) return;
      const t = e.originalEvent && e.originalEvent.target;
      if (t && t.closest && t.closest(".maplibregl-marker")) return;          // a rig, a call pin: theirs
      const all = this.map.queryRenderedFeatures(e.point) || [];               // topmost first
      const mark = (f) => f.source !== "base" && f.layer && (f.layer.type === "symbol" || f.layer.type === "circle");
      if (all.some((f) => mark(f) && !IDS.includes(f.layer.id))) return;           // a pin, a firehouse, a camera
      const hit = all.find((f) => f.layer && IDS.includes(f.layer.id));          // a GIS area over it: still ours
      if (!hit) return;
      const f = st.data.features.find((x) => x.id === hit.id) || null;
      if (!f) return;
      setTimeout(() => this.show(f.properties), 0);    // after the map's own click (it closes the boxes)
    }
    show(props) {
      if (!this.control.showInfo) return;
      const [title, sub, lines, foot] = describe(props);
      this.control.close();
      this.control.showInfo(title, sub, lines, foot, "cp-info");
    }
    panel() {
      const box = h("div", "ml-group cp-group");
      const lab = h("label", "check");
      const cb = h("input");
      cb.type = "checkbox";
      cb.checked = this.isOn();
      cb.dataset.key = KEY;
      cb.addEventListener("change", () => this.control.setLayer(KEY, cb.checked));
      lab.append(cb, h("span", "", "Common places"));
      lab.title = "The county's common place names and the department's own (Settings), each at its address";
      if (this.isOn() && this.map.getZoom() < DOT_ZOOM) lab.append(h("span", "dim small", "zoom in"));
      box.append(lab);
      return box;
    }
  }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new PlacesMap(control) });
  return { describe, load, DOT_ZOOM, LABEL_ZOOM, KEY };
})();
