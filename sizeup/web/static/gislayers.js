// County GIS layers on the CAD maps (GitHub #91): Westchester County GIS open data (tax parcels, fire
// districts and municipal boundaries, hydrants, bridges, buildings, flood zones, rail ...) as one
// "County GIS" group in every map's Layers box (maplayers.js MapLayers.addGroup), one toggle per
// dataset under its group's sub-heading, each on / off per viewer (localStorage "sizeup.gisLayers").
//
//   The data     only ever from the hub: GET /api/cad/gis (the catalog: what layers there are, their style,
//                fields, zoom and when each was fetched; sizeup/gis.py) and GET /api/cad/gis/<key> (GeoJSON,
//                one fetch for every map on the page). A "tiled" layer (the parcels, the buildings: too big
//                to send whole) is asked for the map's viewport (?bbox=) from its minzoom up, a little
//                padded, again only when the map leaves the box it last asked for. The browser never talks
//                to the county.
//   The look     from the catalog's style: colors are design tokens ("--info") or hex; polygons a faint
//                fill with an outline, lines a stroke, points a dot with a label from zoom 15; a feature
//                marked ours (_ours: our fire district) is drawn in the department's accent. A layer with a
//                height field (the buildings) can be extruded: the "3D" button tilts the map.
//   A click      on a feature opens the same small box the highway layers use (the control's showInfo):
//                the configured fields, as the county names them, and the source line.
//   Attribution  "Westchester County GIS" on the group and in the map's corner (the sources' attribution).
"use strict";

window.GisLayers = (() => {
  const PREF_KEY = "sizeup.gisLayers";
  const CATALOG_MS = 300000;            // the catalog again every 5 min (refresh times, a re-fetched layer)
  const PAD = 0.35;                     // a tiled layer's box: the viewport plus this much each way
  const MOVE_MS = 250;
  const LABEL_ZOOM = 15;
  const ATTR = '<a href="https://gis.westchestercountyny.gov/" target="_blank" rel="noopener">Westchester County GIS</a>';

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function token(name, fallback) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch (_) { return fallback; }
  }
  const color = (v, fallback) => (typeof v === "string" && v.startsWith("--") ? token(v, fallback) : (v || fallback));
  const num = (v, fallback) => (typeof v === "number" && isFinite(v) ? v : fallback);

  // ---- per-viewer choices ------------------------------------------------------------------------------------
  const prefs = {};
  let prefsLoaded = false;
  function loadPrefs() {
    if (prefsLoaded) return;
    prefsLoaded = true;
    try {
      const saved = JSON.parse(localStorage.getItem(PREF_KEY) || "null");
      if (saved && typeof saved === "object") for (const [k, v] of Object.entries(saved)) if (typeof v === "boolean") prefs[k] = v;
    } catch (_) { /* defaults */ }
  }
  function savePrefs() {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (_) { /* not remembered */ }
  }
  const isOn = (l) => (typeof prefs[l.key] === "boolean" ? prefs[l.key] : !!l.on);   // "_open:<group>": a fold's state

  // ---- the catalog and the layers' data: one copy for every map on the page ----------------------------------------
  const store = { cat: null, timer: null, loading: null, layers: {}, subs: new Set() };
  const layersOf = (cat) => ((cat && cat.layers) || []);
  async function loadCatalog() {
    if (store.loading) return store.loading;
    store.loading = (async () => {
      try {
        const r = await fetch("/api/cad/gis", { credentials: "same-origin" });
        if (!r.ok) throw new Error(`${r.status}`);
        const d = await r.json();
        const was = store.cat;
        store.cat = d;
        for (const l of layersOf(d)) {                   // a layer fetched again on the hub: ask for it again
          const have = store.layers[l.key];
          if (have && have.rev !== l.rev && !l.tiled) { have.data = null; have.rev = l.rev; }
        }
        if (!was || JSON.stringify(layersOf(was).map((l) => [l.key, l.rev, l.error])) !== JSON.stringify(layersOf(d).map((l) => [l.key, l.rev, l.error]))) {
          for (const fn of store.subs) { try { fn(d); } catch (e) { console.error(e); } }
        }
      } catch (e) {
        if (!store.cat) {                                 // an older hub, or offline: the box says so
          store.cat = { configured: false, note: "County GIS layers unavailable here", layers: [] };
          for (const fn of store.subs) { try { fn(store.cat); } catch (_) { /* */ } }
        }
      } finally {
        store.loading = null;
      }
    })();
    return store.loading;
  }
  function subscribe(fn) {
    store.subs.add(fn);
    if (store.cat) fn(store.cat);
    if (!store.timer) {
      loadCatalog();
      store.timer = setInterval(() => { if (document.visibilityState !== "hidden") loadCatalog(); }, CATALOG_MS);
    }
  }
  const empty = () => ({ type: "FeatureCollection", features: [] });
  async function layerData(l) {
    // the whole layer, once per page (tiled ones go by viewport: tileFetch)
    const slot = store.layers[l.key] || (store.layers[l.key] = { data: null, rev: l.rev, loading: null });
    if (slot.data) return slot.data;
    if (slot.loading) return slot.loading;
    slot.loading = (async () => {
      try {
        const r = await fetch(`/api/cad/gis/${encodeURIComponent(l.key)}`, { credentials: "same-origin" });
        if (!r.ok) throw new Error(`${r.status}`);
        slot.data = await r.json();
        slot.rev = l.rev;
        return slot.data;
      } catch (e) {
        console.error("county GIS layer", l.key, e);
        return empty();
      } finally {
        slot.loading = null;
      }
    })();
    return slot.loading;
  }

  // ---- the look: the catalog's style on the design's tokens -------------------------------------------------------
  const DARK = "#15181B";
  const ours = (plain, accent) => ["case", ["boolean", ["get", "_ours"], false], accent, plain];
  function mapLayers(l, src, vis) {
    const s = l.style || {}, kind = l.kind || "point";
    const accent = token("--accent", "#C8102E");
    const out = [];
    const base = { source: src, minzoom: l.minzoom || 0, layout: { visibility: vis } };
    if (kind === "polygon") {
      const c = color(s.color, token("--info", "#6AA6E8"));
      out.push({ id: `${src}-fill`, type: "fill", ...base,
        paint: { "fill-color": ours(c, accent), "fill-opacity": num(s.fill, 0.12) } });
      out.push({ id: `${src}-line`, type: "line", ...base,
        layout: { ...base.layout, "line-join": "round" },
        paint: { "line-color": ours(c, accent), "line-width": ours(num(s.width, 1.2), num(s.width, 1.2) + 1.2),
                 "line-opacity": num(s.opacity, 0.9), ...(s.dash ? { "line-dasharray": [3, 2] } : {}) } });
      if (s.height) {                                                     // extruded when the map is tilted (3D)
        out.push({ id: `${src}-3d`, type: "fill-extrusion", source: src, minzoom: Math.max(l.minzoom || 0, 15),
          layout: { visibility: "none" },
          paint: { "fill-extrusion-color": c, "fill-extrusion-opacity": 0.75,
                   "fill-extrusion-height": ["max", 3, ["*", num(s.height_scale, 1), ["to-number", ["get", s.height], 3]]],
                   "fill-extrusion-base": 0 } });
      }
    } else if (kind === "line") {
      const c = color(s.color, token("--warn", "#E0A030"));
      out.push({ id: `${src}-line`, type: "line", ...base, layout: { ...base.layout, "line-join": "round", "line-cap": "round" },
        paint: { "line-color": ours(c, accent), "line-width": ["interpolate", ["linear"], ["zoom"], 10, num(s.width, 2) * 0.6, 15, num(s.width, 2)],
                 "line-opacity": num(s.opacity, 0.9), ...(s.dash ? { "line-dasharray": [2, 2] } : {}) } });
    } else {
      const c = color(s.color, token("--ok", "#3FB86B"));
      out.push({ id: `${src}-pt`, type: "circle", ...base,
        paint: { "circle-color": ours(c, accent), "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, num(s.radius, 5) * 0.6, 15, num(s.radius, 5)],
                 "circle-stroke-color": DARK, "circle-stroke-width": 1.5, "circle-opacity": num(s.opacity, 0.95) } });
    }
    return out;
  }
  function labelLayer(l, src, vis, glyphs) {
    const s = l.style || {};
    const f = s.label || (l.kind === "point" && l.fields && l.fields[0] ? l.fields[0][0] : "");
    if (!f || !glyphs) return null;
    const minzoom = Math.max(l.minzoom || 0, num(s.labelzoom, LABEL_ZOOM));
    const point = l.kind === "point";
    return { id: `${src}-label`, type: "symbol", source: src, minzoom,
      layout: { visibility: vis, "text-field": ["to-string", ["get", f]], "text-font": ["Noto Sans Regular"], "text-size": 11,
                ...(point ? { "text-anchor": "left", "text-offset": [0.8, 0] } : { "symbol-placement": l.kind === "line" ? "line" : "point" }),
                "text-optional": true, "text-max-width": 8 },
      paint: { "text-color": "#C9CED4", "text-halo-color": DARK, "text-halo-width": 1.4 } };
  }

  // ---- one map's County GIS layers -------------------------------------------------------------------------------------
  const maps = new Set();

  class GisMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.added = {};                    // key -> the map layer ids
      this.tiles = {};                    // key -> {box, seq}
      this.tilted = false;
      this.moveTimer = null;
      this.map.on("moveend", () => { clearTimeout(this.moveTimer); this.moveTimer = setTimeout(() => this.viewport(), MOVE_MS); });
      this.map.on("click", (e) => this.clicked(e));
      maps.add(this);
      loadPrefs();
      subscribe(() => this.sync());
    }

    // the style is up (once our sources are in it, it stays so: loaded() is false again while tiles load)
    ready() { try { return this.styled || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }

    // every configured layer on the map (hidden unless on); their data when on
    sync() {
      const cat = store.cat;
      if (!cat || !cat.configured) return;
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      const glyphs = !!(this.map.getStyle() && this.map.getStyle().glyphs);
      for (const l of layersOf(cat)) {
        const src = `gis-${l.key}`;
        if (!this.map.getSource(src)) {
          try {
            this.map.addSource(src, { type: "geojson", data: empty(), attribution: ATTR });
            const vis = isOn(l) ? "visible" : "none";
            const ids = [];
            for (const spec of mapLayers(l, src, vis)) { this.map.addLayer(spec, this.before(l, spec)); ids.push(spec.id); }
            const lab = labelLayer(l, src, vis, glyphs);
            if (lab) { this.map.addLayer(lab, this.before(l, lab)); ids.push(lab.id); }
            this.added[l.key] = ids;
            this.styled = true;
            for (const id of ids) {
              if (id.endsWith("-label") || id.endsWith("-3d")) continue;
              this.map.on("mouseenter", id, () => { this.map.getCanvas().style.cursor = "pointer"; });
              this.map.on("mouseleave", id, () => { this.map.getCanvas().style.cursor = ""; });
            }
          } catch (e) { console.error("county GIS layer", l.key, e); continue; }
        }
        if (isOn(l)) this.feed(l);
      }
      this.applyVisibility();
    }

    // polygons under everything of ours; lines and points under the highway layers and the firehouses
    before(l, spec) {
      const ids = (this.map.getStyle().layers || []).map((x) => x.id);
      const polygon = spec.type === "fill" || spec.type === "fill-extrusion";
      return ids.find((id) => (polygon && id.startsWith("gis-")) || id.startsWith("ml-") || id === "cm-lines" || id === "firehouse");
    }

    async feed(l) {
      if (l.tiled) return this.viewport(l);
      const data = await layerData(l);
      const src = this.map.getSource(`gis-${l.key}`);
      if (src && isOn(l)) src.setData(data);
    }

    // a tiled layer: the viewport (padded) from the hub, when the map has left the last box
    viewport(only) {
      const cat = store.cat;
      if (!cat) return;
      if (!this.ready()) { this.map.once("idle", () => this.viewport(only)); return; }
      for (const l of layersOf(cat)) {
        if (!l.tiled || (only && only.key !== l.key) || !isOn(l) || !this.map.getSource(`gis-${l.key}`)) continue;
        if (this.map.getZoom() < (l.minzoom || 0)) continue;
        const b = this.map.getBounds();
        const w = b.getWest(), s = b.getSouth(), e = b.getEast(), n = b.getNorth();
        const t = this.tiles[l.key] || (this.tiles[l.key] = { box: null, seq: 0 });
        if (t.box && w >= t.box[0] && s >= t.box[1] && e <= t.box[2] && n <= t.box[3]) continue;
        const dx = (e - w) * PAD, dy = (n - s) * PAD;
        const box = [w - dx, s - dy, e + dx, n + dy].map((x) => Math.round(x * 1e5) / 1e5);
        const seq = ++t.seq;
        fetch(`/api/cad/gis/${encodeURIComponent(l.key)}?bbox=${box.join(",")}`, { credentials: "same-origin" })
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
          .then((d) => {
            if (seq !== t.seq) return;
            t.box = box;
            const src = this.map.getSource(`gis-${l.key}`);
            if (src) src.setData(d);
          })
          .catch((e) => console.error("county GIS tile", l.key, e));
      }
    }

    applyVisibility() {
      const cat = store.cat;
      if (!cat || !this.ready()) return;
      for (const l of layersOf(cat)) {
        for (const id of this.added[l.key] || []) {
          if (!this.map.getLayer(id)) continue;
          const on = isOn(l) && (!id.endsWith("-3d") || this.tilted);
          this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
        }
      }
    }

    tilt(on) {
      this.tilted = !!on;
      try {
        if (on) { this.map.setMaxPitch(60); this.map.easeTo({ pitch: 55, duration: 400 }); }
        else { this.map.easeTo({ pitch: 0, duration: 400 }); setTimeout(() => { try { this.map.setMaxPitch(0); } catch (_) { /* */ } }, 450); }
      } catch (e) { console.error(e); }
      this.applyVisibility();
    }

    // a click on one of our features: the configured fields (a highway layer's feature on top wins there)
    clicked(e) {
      const cat = store.cat;
      if (!cat || !this.ready()) return;
      const all = (this.map.getStyle().layers || []).map((x) => x.id);
      const theirs = all.filter((id) => id.startsWith("ml-"));
      if (theirs.length && this.map.queryRenderedFeatures(e.point, { layers: theirs }).length) return;
      const ours = [];
      for (const l of layersOf(cat)) {
        if (!isOn(l)) continue;
        for (const id of this.added[l.key] || []) if (!id.endsWith("-label") && this.map.getLayer(id)) ours.push(id);
      }
      if (!ours.length) return;
      const hits = this.map.queryRenderedFeatures(e.point, { layers: ours });
      if (!hits.length) return;
      // points first, then lines, then polygons (queryRenderedFeatures: the topmost first within a kind)
      const rank = (f) => ({ circle: 0, line: 1 }[f.layer.type] ?? 2);
      hits.sort((a, b) => rank(a) - rank(b));
      const f = hits[0];
      const key = f.layer.id.replace(/^gis-/, "").replace(/-(fill|line|pt|3d)$/, "");
      const l = layersOf(cat).find((x) => x.key === key);
      if (!l) return;
      e.originalEvent && e.originalEvent.stopPropagation();
      setTimeout(() => this.show(l, f.properties || {}), 0);
    }

    show(l, props) {
      const fields = (l.fields && l.fields.length ? l.fields : Object.keys(props).filter((k) => !k.startsWith("_")).map((k) => [k, k]));
      const rows = [];
      let title = "";
      for (const [name, label] of fields) {
        const v = props[name];
        if (v === undefined || v === null || v === "") continue;
        const text = fmt(v);
        if (!title) { title = text; continue; }
        rows.push([label, text]);
      }
      const lines = rows.map(([k, v]) => `${k}: ${v}`);
      if (l.note) lines.push(l.note);
      const foot = `${store.cat.attribution || "Westchester County GIS"}${l.asof ? `, ${l.asof}` : ""}${l.at ? `, fetched ${day(l.at)}` : ""}`;
      this.control.close();
      this.control.showInfo(title || l.label, (props._ours ? "Ours: " : "") + l.label, lines.length ? lines : ["(no details)"], foot, "");
    }

    // the group in the Layers box
    panel() {
      const cat = store.cat;
      const box = h("div", "ml-group");
      const head = h("div", "ml-ghead");
      head.append(h("b", "", "County GIS"));
      const src = h("a", "dim small", "Westchester County GIS");
      src.href = (cat && cat.link) || "https://gis.westchestercountyny.gov/";
      src.target = "_blank";
      src.rel = "noopener";
      head.append(h("span", "spacer"), src);
      box.append(head);
      if (!cat) { box.append(h("div", "dim small", "Loading…")); return box; }
      if (!cat.configured || !layersOf(cat).length) { box.append(h("div", "dim small", cat.note || "No county GIS layers configured on the hub")); return box; }
      const zoom = this.map.getZoom();
      let group = null, fold = null, any3d = false;
      for (const l of layersOf(cat)) {
        if (l.group !== group) {
          group = l.group;
          fold = h("details", "ml-fold");
          const sum = h("summary", "ml-sub");
          sum.append(group, h("span", "ml-count dim small", ""));
          fold.append(sum);
          const on = layersOf(cat).filter((x) => x.group === group && isOn(x)).length;
          sum.lastChild.textContent = on ? `${on} on` : "";
          fold.open = typeof prefs[`_open:${group}`] === "boolean" ? prefs[`_open:${group}`] : on > 0 || group === "Parcels";
          const g = group;                 // a person's click (not the open set above, whose toggle event comes later)
          sum.addEventListener("click", () => setTimeout(() => { prefs[`_open:${g}`] = fold.open; savePrefs(); }, 0));
          box.append(fold);
        }
        const lab = h("label", "check");
        const cb = h("input");
        cb.type = "checkbox";
        cb.checked = isOn(l);
        cb.disabled = !l.at && !!l.error;
        cb.addEventListener("change", () => setLayer(l.key, cb.checked));
        lab.append(cb, h("span", "", l.label));
        if (l.asof) lab.append(h("span", "ml-note", l.asof));
        if (l.error && !l.at) lab.append(h("span", "ml-err small", "not loaded"));
        else if (l.minzoom > 0 && zoom < l.minzoom) lab.append(h("span", "dim small", "zoom in"));
        else if (l.at && l.n === 0) lab.append(h("span", "dim small", "nothing here"));
        if (l.note) lab.title = l.note;
        fold.append(lab);
        if (l.style && l.style.height) any3d = true;
      }
      if (any3d) {
        const row = h("label", "check");
        const cb = h("input");
        cb.type = "checkbox";
        cb.checked = this.tilted;
        cb.addEventListener("change", () => this.tilt(cb.checked));
        row.append(cb, h("span", "", "3D: tilt the map, buildings by height"));
        box.append(row);
      }
      const at = Math.max(0, ...layersOf(cat).map((l) => l.at || 0));
      const note = h("div", "ml-note");
      note.append(`Open data by ${cat.attribution || "Westchester County GIS"}` + (at ? `, fetched ${day(at)}` : "") + ". ");
      const terms = h("a", "", "Terms");
      terms.href = cat.terms || "https://gis.westchestercountyny.gov/";
      terms.target = "_blank";
      terms.rel = "noopener";
      note.append(terms);
      box.append(note);
      return box;
    }
  }

  function setLayer(key, on) {
    prefs[key] = !!on;
    savePrefs();
    for (const m of maps) { m.sync(); m.applyVisibility(); if (!m.control.panel.hidden) m.control.renderPanel(); }
  }

  function fmt(v) {
    if (typeof v === "number" && v > 1e11 && v < 1e14) return day(v / 1000);     // an ArcGIS date (epoch ms)
    if (typeof v === "number") return Number.isInteger(v) ? String(v) : (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2));
    const s = String(v).trim();
    if (/^\d{13}$/.test(s)) return day(Number(s) / 1000);
    return s.replace(/\s+/g, " ");
  }
  const tz = () => (window.cad && cad.scene && cad.scene.timezone) || "America/New_York";
  function day(t) {
    try { return new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: tz() }); }
    catch (_) { return ""; }
  }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new GisMap(control) });

  return { prefs, setLayer, catalog: () => store.cat, load: loadCatalog, maps };
})();
