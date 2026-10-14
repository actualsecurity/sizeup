// County GIS layers on the CAD maps (GitHub #91): Westchester County GIS open data (tax parcels, fire
// districts and municipal boundaries, hydrants, bridges, buildings, flood zones, rail ...) as one
// "County GIS" group in every map's Layers box (maplayers.js MapLayers.addGroup), one toggle per
// dataset under its group's sub-heading, each on / off through the box's control (control.on / setLayer,
// key "gis:<layer>": the viewer's "overview" or "incident" set, or the event's own, #111); the folds too
// (MapLayers.fold / setFold). Nothing is kept here.
//
//   The data     only ever from the hub: GET /api/cad/gis (the catalog: what layers there are, their style,
//                fields, zoom and when each was fetched; sizeup/gis.py) and GET /api/cad/gis/<key> (GeoJSON,
//                one fetch for every map on the page). A "tiled" layer (the parcels, the buildings: too big
//                to send whole) is asked for the map's viewport (?bbox=) from its minzoom up, a little
//                padded, again only when the map leaves the box it last asked for. The browser never talks
//                to the county.
//   The look     from the catalog's style: colors are design tokens ("--info") or hex; polygons a faint
//                fill with an outline, lines a stroke, points a mark with a label from zoom 15; a feature
//                marked ours (_ours: our fire district) is drawn in the department's accent. A layer with a
//                height field (the buildings) can be extruded: the "3D" button tilts the map.
//   The marks    a point layer with an icon (the catalog's icon: "hydrant", "fire-station", ...; #111) is a
//                symbol layer: the silhouette from /static/icons.svg (i-gis-<icon>, read once, drawn with
//                Path2D) in the layer's color with a light rim, ours in the accent; small at the layer's
//                minimum zoom and full two zooms up (the style's size scales it); collision-aware, so a
//                dense block of hydrants thins out rather than smears; its label beside it when there is
//                room. The same icon stands by the layer's toggle and at the top of its click box
//                (icons.js Icon). A point layer without one is a dot.
//   One station  one mark (#162): a station layer's copies of the stations the Trains layer draws (trains.js:
//                the New Haven Line's, from the MTA, with the line's details) come from the hub marked _trains
//                and are left out while this map's Trains layer is on; with it off, every station shows.
//   A click      on a feature opens the same small box the highway layers use (the control's showInfo):
//                the configured fields, as the county names them, the layer's sub line, and the source.
//   Attribution  "Westchester County GIS" on the group and in the map's corner (the sources' attribution).
"use strict";

window.GisLayers = (() => {
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

  // ---- which are on: the box's control decides (the viewer's set for its map, or the event's own, #111) ------------
  // On for a viewer who never chose: the catalog's on (fire districts) and, on an event's map, these.
  const PROFILE_ON = { overview: [], incident: ["hydrants", "buildings"] };
  const gisKey = (l) => `gis:${l.key}`;
  const dflt = (l, profile) => !!l.on || (PROFILE_ON[profile] || []).includes(l.key);

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
        // ?rev: each version of a layer its own address, so the browser's copy (ETag, max-age) is never a stale one
        const r = await fetch(`/api/cad/gis/${encodeURIComponent(l.key)}${l.rev !== undefined && l.rev !== null ? `?rev=${encodeURIComponent(l.rev)}` : ""}`,
                              { credentials: "same-origin" });
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

  // ---- the marks: the sprite's silhouettes, drawn once per layer in its color ---------------------------------------
  const sprite = { paths: null, loading: null };          // "gis-hydrant" -> {body: [d, ...], cut: [d, ...]}
  function loadSprite() {
    if (sprite.paths) return Promise.resolve(sprite.paths);
    if (sprite.loading) return sprite.loading;
    sprite.loading = fetch(window.ICONS_SVG || "/static/icons.svg", { credentials: "same-origin" })   // icons.js: the page's copy
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`${r.status}`))))
      .then((text) => {
        const doc = new DOMParser().parseFromString(text, "image/svg+xml");
        const out = {};
        for (const sym of doc.querySelectorAll('symbol[id^="i-gis-"]')) {
          const body = [], cut = [];
          for (const el of sym.querySelectorAll("path")) (el.classList.contains("cut") ? cut : body).push(el.getAttribute("d") || "");
          out[sym.id.slice(2)] = { body, cut };
        }
        return out;
      })
      .catch((e) => { console.error("county GIS marks", e); return {}; })           // no sprite: dots
      .then((paths) => { sprite.paths = paths; sprite.loading = null; return paths; });
    return sprite.loading;
  }
  const ICON_PX = 24;                                     // a mark at full size, css px (drawn at 2x)
  const LIGHT = "#F2F3F5";
  function iconImage(shape, fill) {
    const px = ICON_PX * 2, pad = 3;
    const c = document.createElement("canvas");
    c.width = c.height = px;
    const g = c.getContext("2d");
    const k = (px - 2 * pad) / 24;
    g.translate(pad, pad);
    g.scale(k, k);
    g.lineJoin = "round";
    g.lineCap = "round";
    g.strokeStyle = LIGHT;
    g.lineWidth = 3 / k;                                  // a light rim, 1.5 css px outside the silhouette (the fill covers the rest)
    const body = shape.body.map((d) => new Path2D(d));
    for (const path of body) g.stroke(path);
    g.fillStyle = fill;
    for (const path of body) g.fill(path);
    g.fillStyle = DARK;                                   // the cuts (a door, a window, a band): the map's dark
    for (const d of shape.cut) g.fill(new Path2D(d));
    return g.getImageData(0, 0, px, px);
  }
  // the same mark in the box: Icon("gis-<icon>") from the sprite, in the layer's color (ours: the accent)
  function iconEl(l, accent) {
    if (!l.icon || !window.Icon) return null;
    const el = Icon(`gis-${l.icon}`);
    const c = l.style && l.style.color;
    el.style.setProperty("--c", accent ? "var(--accent-text)" : (typeof c === "string" && c.startsWith("--") ? `var(${c})` : (c || "var(--ok)")));
    return el;
  }

  // ---- the look: the catalog's style on the design's tokens -------------------------------------------------------
  const DARK = "#15181B";
  const ours = (plain, accent) => ["case", ["boolean", ["get", "_ours"], false], accent, plain];
  const labelField = (l) => (l.style && l.style.label) || (l.kind === "point" && l.fields && l.fields[0] ? l.fields[0][0] : "");
  const TEXT = { "text-color": "#C9CED4", "text-halo-color": DARK, "text-halo-width": 1.4 };
  // one station, one mark (#162): what a station layer (trains_field) shows while the map's Trains layer is on
  const NOT_TRAINS = ["!", ["boolean", ["get", "_trains"], false]];
  function mapLayers(l, src, vis, glyphs, icon) {
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
    } else if (icon) {
      // the mark: small at the layer's first zoom, full two zooms up; placed only where it fits (a dense block
      // thins out), its label beside it when there is room for that too
      const minzoom = l.minzoom || 0, size = num(s.size, 1), f = labelField(l);
      const layout = { ...base.layout, "icon-image": ours(icon, `${icon}-ours`), "icon-allow-overlap": false, "icon-ignore-placement": false,
                       "icon-padding": 1, "icon-size": ["interpolate", ["linear"], ["zoom"], minzoom, 0.55 * size, minzoom + 2, size] };
      if (glyphs && f) {
        const lz = Math.max(minzoom, num(s.labelzoom, LABEL_ZOOM));
        Object.assign(layout, { "text-field": ["step", ["zoom"], "", lz, ["to-string", ["get", f]]], "text-font": ["Noto Sans Regular"],
                                "text-size": 11, "text-anchor": "left", "text-offset": [0.95 * size + 0.25, 0], "text-optional": true,
                                "text-max-width": 8 });
      }
      out.push({ id: `${src}-pt`, type: "symbol", ...base, layout, paint: { "icon-opacity": num(s.opacity, 1), ...(glyphs && f ? TEXT : {}) } });
    } else {
      const c = color(s.color, token("--ok", "#3FB86B"));
      out.push({ id: `${src}-pt`, type: "circle", ...base,
        paint: { "circle-color": ours(c, accent), "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, num(s.radius, 5) * 0.6, 15, num(s.radius, 5)],
                 "circle-stroke-color": DARK, "circle-stroke-width": 1.5, "circle-opacity": num(s.opacity, 0.95) } });
    }
    return out;
  }
  function labelLayer(l, src, vis, glyphs, icon) {
    const s = l.style || {};
    const f = labelField(l);
    if (!f || !glyphs || (icon && l.kind === "point")) return null;       // a mark carries its own label
    const minzoom = Math.max(l.minzoom || 0, num(s.labelzoom, LABEL_ZOOM));
    const point = l.kind === "point";
    return { id: `${src}-label`, type: "symbol", source: src, minzoom,
      layout: { visibility: vis, "text-field": ["to-string", ["get", f]], "text-font": ["Noto Sans Regular"], "text-size": 11,
                ...(point ? { "text-anchor": "left", "text-offset": [0.8, 0] } : { "symbol-placement": l.kind === "line" ? "line" : "point" }),
                "text-optional": true, "text-max-width": 8 },
      paint: { ...TEXT } };
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
      subscribe(() => this.sync());
    }

    // on here? the control: the event's own set, else the viewer's set for this map, else the default
    isOn(l) { return this.control.on(gisKey(l), dflt(l, this.control.profile)); }
    // the Trains layer on this map (trains.js): it draws the stations marked _trains
    trainsOn() { return !!(window.TrainLayer && TrainLayer.KEY) && this.control.on(TrainLayer.KEY, false); }
    keys() { return layersOf(store.cat).map((l) => [gisKey(l), dflt(l, this.control.profile)]); }   // for the event's set
    refresh() { this.sync(); }

    // the layer's mark on this map (its image registered once per layer: the plain and the accent); "" = a dot
    iconFor(l) {
      const shape = l.icon && sprite.paths && sprite.paths[`gis-${l.icon}`];
      if (!shape) return "";
      const id = `gis-${l.key}`;
      try {
        if (!this.map.hasImage(id)) {
          this.map.addImage(id, iconImage(shape, color((l.style || {}).color, token("--ok", "#3FB86B"))), { pixelRatio: 2 });
          this.map.addImage(`${id}-ours`, iconImage(shape, token("--accent", "#C8102E")), { pixelRatio: 2 });
        }
      } catch (e) { console.error("county GIS mark", l.key, e); return ""; }
      return id;
    }

    // the style is up (once our sources are in it, it stays so: loaded() is false again while tiles load)
    ready() { try { return this.styled || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }

    // every configured layer on the map (hidden unless on); their data when on
    sync() {
      const cat = store.cat;
      if (!cat || !cat.configured) return;
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      if (!sprite.paths) { if (!this.spriteWait) { this.spriteWait = true; loadSprite().then(() => this.sync()); } return; }   // the marks first
      const glyphs = !!(this.map.getStyle() && this.map.getStyle().glyphs);
      for (const l of layersOf(cat)) {
        const src = `gis-${l.key}`;
        if (!this.map.getSource(src)) {
          try {
            this.map.addSource(src, { type: "geojson", data: empty(), attribution: ATTR });
            const vis = this.isOn(l) ? "visible" : "none";
            const ids = [];
            const icon = l.kind === "point" ? this.iconFor(l) : "";
            for (const spec of mapLayers(l, src, vis, glyphs, icon)) { this.map.addLayer(spec, this.before(l, spec)); ids.push(spec.id); }
            const lab = labelLayer(l, src, vis, glyphs, icon);
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
        if (this.isOn(l)) this.feed(l);
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
      if (src && this.isOn(l)) src.setData(data);
    }

    // a tiled layer: the viewport (padded) from the hub, when the map has left the last box
    viewport(only) {
      const cat = store.cat;
      if (!cat) return;
      if (!this.ready()) { this.map.once("idle", () => this.viewport(only)); return; }
      for (const l of layersOf(cat)) {
        if (!l.tiled || (only && only.key !== l.key) || !this.isOn(l) || !this.map.getSource(`gis-${l.key}`)) continue;
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
      const trains = this.trainsOn();
      for (const l of layersOf(cat)) {
        for (const id of this.added[l.key] || []) {
          if (!this.map.getLayer(id)) continue;
          const on = this.isOn(l) && (!id.endsWith("-3d") || this.tilted);
          this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
          if (l.trains_field) this.map.setFilter(id, trains ? NOT_TRAINS : null);   // the Trains layer's stations: there, not here too
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
        if (!this.isOn(l)) continue;
        for (const id of this.added[l.key] || []) if (!id.endsWith("-label") && this.map.getLayer(id)) ours.push(id);
      }
      if (!ours.length) return;
      const hits = this.map.queryRenderedFeatures(e.point, { layers: ours });
      if (!hits.length) return;
      // points first, then lines, then polygons (queryRenderedFeatures: the topmost first within a kind)
      const rank = (f) => ({ circle: 0, symbol: 0, line: 1 }[f.layer.type] ?? 2);
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
      this.control.showInfo(title || l.label, (props._ours ? "Ours: " : "") + (l.sub || l.label), lines.length ? lines : ["(no details)"], foot, "",
                            iconEl(l, !!props._ours));
    }

    // the group in the Layers box; folds: each group's open state as the box had it before this rebuild
    panel(folds = {}) {
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
          fold.dataset.fold = group;
          const sum = h("summary", "ml-sub");
          sum.append(group, h("span", "ml-count dim small", ""));
          fold.append(sum);
          const on = layersOf(cat).filter((x) => x.group === group && this.isOn(x)).length;
          sum.lastChild.textContent = on ? `${on} on` : "";
          // open as it was in the box a moment ago, else as this viewer left it, else when something in it is on
          const was = typeof folds[group] === "boolean" ? folds[group] : MapLayers.fold(group);
          fold.open = typeof was === "boolean" ? was : on > 0 || group === "Parcels";
          const g = group;
          let touched = false;             // a person's click or key on the heading (not the open set above, whose toggle comes later)
          sum.addEventListener("click", () => { touched = true; });
          fold.addEventListener("toggle", () => { if (touched) MapLayers.setFold(g, fold.open); });
          box.append(fold);
        }
        const lab = h("label", "check");
        const cb = h("input");
        cb.type = "checkbox";
        cb.checked = this.isOn(l);
        cb.disabled = !l.at && !!l.error;
        cb.dataset.key = gisKey(l);
        cb.addEventListener("change", () => this.control.setLayer(gisKey(l), cb.checked));
        const mark = iconEl(l, false);
        lab.append(cb, ...(mark ? [mark] : []), h("span", "", l.label));
        if (l.asof) lab.append(h("span", "ml-note", l.asof));
        if (l.error && !l.at) lab.append(h("span", "ml-err small", "not loaded"));
        else if (l.minzoom > 0 && zoom < l.minzoom) lab.append(h("span", "dim small", "zoom in"));
        else if (l.at && l.n === 0) lab.append(h("span", "dim small", "nothing here"));
        const tip = [l.note, l.trains_field && this.trainsOn() ? "Stations the Trains layer shows are drawn there, not here too" : ""];
        if (tip.some(Boolean)) lab.title = tip.filter(Boolean).join(". ");
        fold.append(lab);
        if (l.style && l.style.height) any3d = true;
      }
      if (any3d) {
        const row = h("label", "check");
        const cb = h("input");
        cb.type = "checkbox";
        cb.checked = this.tilted;
        cb.dataset.key = "gis:3d";
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

  return { catalog: () => store.cat, load: loadCatalog, loadSprite, maps, PROFILE_ON };
})();
