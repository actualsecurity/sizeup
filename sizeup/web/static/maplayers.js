// Highway layers on the CAD maps (GitHub #73): a Layers control on every RigMap (the event's map, the
// large map, the Overview and /monitor), the camera viewer, the event's "Cameras nearby" row, and the
// hook the "@" place search uses for cameras.
//
//   Layers        Cameras, Incidents & closures, Roadwork, Message signs (the NYS Thruway Authority's public
//                 feeds and 511NY's, through the hub: GET /api/cad/roads, sizeup/roads.py; each item says its
//                 source), Mileposts, Exits (the gazetteer's, #67), Airport with its Approaches sub-toggle (airport.js,
//                 #87: drawn by that module, listed and switched here; on by itself for an event at the airport,
//                 focus(inc)). Each one on / off per viewer (localStorage "sizeup.mapLayers"), as two remembered
//                 sets (#111): "overview" (the Overview map, the large map, the wall board) and "incident" (an
//                 event's map); cameras and incidents on in both at first. An event can have its own set: a
//                 dispatcher's change on its map is kept on the event (POST /api/cad/incident/<id> map_layers,
//                 a flat {key: on} over every layer, GIS ones as "gis:<key>"), so that event looks the same
//                 to everyone who opens it; "Reset to default" drops it. A viewer's change is theirs alone.
//                 Signs from zoom 11, exits and mileposts from 12
//                 (the tenths of a mile from 14). The box says which sources are on and when each last updated
//                 ("Data: New York State Thruway Authority", "511NY, updated hh:mm"); no source at all: the
//                 four are greyed with the reason; the rest still work.
//   A camera      opens the viewer (a dialog: fits a 390 px phone): the snapshot through the hub
//                 (/api/cad/cameras/<id>/image: the browser never talks to 511NY or the camera), its name,
//                 road and direction, and when the image was taken; a new one every 10 s while it is open
//                 and the page is visible (paused when hidden).
//   An event      its Cameras panel (#95, below): the camera just before and just after along the road, live,
//                 then any within 300 m; the wall board the two along the road for the active highway event.
//   Places        MapLayers.searchPlaces(q): cameras for the "@" place search ({kind: "camera", camera,
//                 label, sub, lat, lon}); MapLayers.openPlace(p) opens one (true when it was a camera).
//                 Anything with data-camera="<id>" (a camera chip in a note) opens the viewer when clicked.
//
// The sources' terms (sizeup/roads.py): every event, sign and snapshot shows when it was published or
// updated, and the map's attribution names each source that is on, with a link.
//
// Other scripts add their own groups to the same Layers box: MapLayers.addGroup({attach(control) ->
// {panel(folds) -> Element | null, keys() -> [[key, default]], refresh()}}) (gislayers.js: the county GIS
// layers, #91); attach() gets each map's control (its map, on(key, default), setLayer(key, on),
// showInfo(title, sub, lines, foot, cls, icon), close()); panel() is asked at every render, with each
// <details data-fold> group's open state from the box as it was (the rebuild keeps the scroll, the focus
// and the folds, #111); keys() the group's layers for the event's set; refresh() after a change.
"use strict";

window.MapLayers = (() => {
  const PREF_KEY = "sizeup.mapLayers";
  const POLL_MS = 60000, CAM_REFRESH_MS = 10000, THUMB_REFRESH_MS = 60000;
  const NEAR_KM = 1.5, NEAR_N = 3;
  const LAYERS = [      // key, label, from 511NY?, min zoom, default on
    { key: "cameras", label: "Cameras", feed: true, minzoom: 0, on: true },
    { key: "incidents", label: "Incidents & closures", feed: true, minzoom: 0, on: true },
    { key: "roadwork", label: "Roadwork", feed: true, minzoom: 0, on: false },
    { key: "signs", label: "Message signs", feed: true, minzoom: 11, on: false },
    { key: "mileposts", label: "Mileposts", feed: false, minzoom: 12, on: false },
    { key: "exits", label: "Exits", feed: false, minzoom: 12, on: false },
    { key: "airport", label: "Airport", feed: false, minzoom: 0, on: false, plugin: "AirportLayer" },          // airport.js (#87)
    { key: "approaches", label: "Approaches", feed: false, minzoom: 0, on: false, plugin: "AirportLayer", sub: "airport" },
  ];
  // The two remembered sets (#111) and what is on in each for a viewer who never chose: the layers' own
  // defaults in both (a group's own layers add theirs: gislayers.js PROFILE_ON).
  const BASE_ON = Object.fromEntries(LAYERS.filter((l) => l.on).map((l) => [l.key, true]));
  const PROFILES = { overview: { ...BASE_ON }, incident: { ...BASE_ON } };
  const OPEN = ["queued", "pending", "active"];
  const canWrite = () => { try { return !!(window.canDo && canDo("dispatch")); } catch (_) { return false; } };
  const SOURCE_NOTE = { "511NY": "powered by 511NY", Thruway: "Data: New York State Thruway Authority" };
  const attributionFor = (d) => ((d && d.sources) || []).filter((s) => s.configured)      // the map's corner is small: names only
    .map((s) => `<a href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.short === "Thruway" ? "NYS Thruway Authority" : s.label)}</a>`).join(" · ");
  const plugin = (l) => (l.plugin ? window[l.plugin] : null);

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const esc = (s) => String(s == null ? "" : s);

  // ---- per-viewer choices (localStorage may be missing or refuse: then just this page view) --------------
  // {overview: {key: on}, incident: {key: on}, folds: {group: open}, hideDown}; only what the viewer chose is
  // in a set (the rest: the profile's default). Before #111 one flat set was kept (and gislayers.js its own,
  // "sizeup.gisLayers"): read once as both sets.
  const KEY_RE = /^[a-z][a-z0-9_:-]{0,47}$/;
  // a stored value (drop: then forget it), or null when storage is missing or refuses
  function stored(key, drop = false) { try { const v = localStorage.getItem(key); if (drop) localStorage.removeItem(key); return v; } catch (_) { return null; } }
  const prefs = (() => {
    const p = { overview: {}, incident: {}, folds: {}, hideDown: false };
    try {
      const saved = JSON.parse(stored(PREF_KEY) || "null");
      if (saved && typeof saved === "object") {
        const flat = Object.fromEntries(Object.entries(saved).filter(([k, v]) => typeof v === "boolean" && k !== "hideDown"));
        for (const name of Object.keys(PROFILES)) {
          const set = saved[name] && typeof saved[name] === "object" ? saved[name] : flat;
          for (const [k, v] of Object.entries(set)) if (typeof v === "boolean" && KEY_RE.test(k)) p[name][k] = v;
        }
        if (saved.folds && typeof saved.folds === "object") for (const [k, v] of Object.entries(saved.folds)) if (typeof v === "boolean") p.folds[k] = v;
        if (typeof saved.hideDown === "boolean") p.hideDown = saved.hideDown;
      }
      const gis = JSON.parse(stored("sizeup.gisLayers", true) || "null");          // read once, then gone
      if (gis && typeof gis === "object") {
        for (const [k, v] of Object.entries(gis)) {
          if (typeof v !== "boolean") continue;
          if (k.startsWith("_open:")) p.folds[k.slice(6)] = v;
          else for (const name of Object.keys(PROFILES)) if (typeof p[name][`gis:${k}`] !== "boolean") p[name][`gis:${k}`] = v;
        }
      }
    } catch (_) { /* defaults */ }
    return p;
  })();
  // keep a value for this viewer (or not, when storage is missing or refuses)
  function remember(key, v) { try { localStorage.setItem(key, v); } catch (_) { /* not remembered */ } }
  function savePrefs() { remember(PREF_KEY, JSON.stringify(prefs)); }

  // ---- the data: one poll for every map on the page ------------------------------------------------------
  const store = { data: null, rev: "", timer: null, subs: new Set(), loading: null };
  async function load() {
    if (store.loading) return store.loading;
    store.loading = (async () => {
      try {
        const r = await fetch(`/api/cad/roads${store.rev !== "" ? `?rev=${encodeURIComponent(store.rev)}` : ""}`,
                              { credentials: "same-origin" });
        if (!r.ok) throw new Error(`${r.status}`);
        const d = await r.json();
        if (!d.same) {
          store.data = d;
          store.rev = String(d.rev);
          for (const fn of store.subs) { try { fn(d); } catch (e) { console.error(e); } }
        }
      } catch (e) {
        if (!store.data) {                  // an older hub, or offline: the controls say so, nothing breaks
          store.data = { configured: false, note: "Highway layers unavailable here", cameras: [], events: [], signs: [],
                         mileposts: [], exits: [], highway_places: false, status: {} };
          for (const fn of store.subs) fn(store.data);
        }
      } finally {
        store.loading = null;
      }
    })();
    return store.loading;
  }
  function subscribe(fn) {
    store.subs.add(fn);
    if (store.data) fn(store.data);
    if (!store.timer) {
      load();
      store.timer = setInterval(() => { if (document.visibilityState !== "hidden") load(); }, POLL_MS);
    }
  }
  const camById = (id) => ((store.data && store.data.cameras) || []).find((c) => String(c.id) === String(id));

  // ---- map icons: drawn once, in the design's colors (style.css tokens) -------------------------------------
  function token(name, fallback) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch (_) { return fallback; }
  }
  function canvas(w, ht, draw) {
    const c = document.createElement("canvas");
    c.width = w; c.height = ht;
    const g = c.getContext("2d");
    draw(g);
    return g.getImageData(0, 0, w, ht);
  }
  const DARK = "rgba(12, 14, 16, 0.94)";
  function icons() {
    const info = token("--info", "#6AA6E8"), warn = token("--warn", "#E0A030"), err = token("--err", "#EE6475");
    const dim = token("--dim", "#9AA3AD"), ok = token("--ok", "#3FB86B"), white = "#F2F3F5";
    const cam = (ring) => canvas(44, 44, (g) => {
      g.translate(22, 22);
      g.fillStyle = DARK; g.strokeStyle = ring; g.lineWidth = 3;
      g.beginPath(); g.arc(0, 0, 18.5, 0, 2 * Math.PI); g.fill(); g.stroke();
      g.fillStyle = white;
      g.beginPath(); g.roundRect(-11, -6.5, 15, 13, 2.5); g.fill();                   // the body
      g.beginPath(); g.moveTo(4, -1.5); g.lineTo(11, -6); g.lineTo(11, 6); g.lineTo(4, 1.5); g.closePath(); g.fill();   // the lens
    });
    const tri = (fill) => canvas(44, 40, (g) => {
      g.fillStyle = fill; g.strokeStyle = DARK; g.lineWidth = 3; g.lineJoin = "round";
      g.beginPath(); g.moveTo(22, 3); g.lineTo(41, 37); g.lineTo(3, 37); g.closePath(); g.fill(); g.stroke();
      g.fillStyle = DARK; g.fillRect(20, 14, 4, 12); g.fillRect(20, 29, 4, 4);
    });
    const closed = canvas(40, 40, (g) => {                                             // no entry: closed
      g.fillStyle = err; g.strokeStyle = DARK; g.lineWidth = 3;
      g.beginPath(); g.arc(20, 20, 17, 0, 2 * Math.PI); g.fill(); g.stroke();
      g.fillStyle = white; g.fillRect(9, 16.5, 22, 7);
    });
    const work = canvas(42, 42, (g) => {                                               // a work-zone diamond
      g.translate(21, 21); g.rotate(Math.PI / 4);
      g.fillStyle = warn; g.strokeStyle = DARK; g.lineWidth = 3;
      g.beginPath(); g.roundRect(-12.5, -12.5, 25, 25, 3); g.fill(); g.stroke();
      g.rotate(-Math.PI / 4); g.fillStyle = DARK;
      g.beginPath(); g.arc(-1, -6, 2.6, 0, 2 * Math.PI); g.fill();
      g.fillRect(-3.5, -2.5, 5, 7.5); g.fillRect(-8, 6, 16, 2.4);
    });
    const sign = (lit) => canvas(46, 30, (g) => {                                      // a message board
      g.fillStyle = DARK; g.strokeStyle = lit ? warn : dim; g.lineWidth = 3;
      g.beginPath(); g.roundRect(2, 2, 42, 26, 3); g.fill(); g.stroke();
      if (lit) { g.fillStyle = warn; for (const y of [9, 14, 19]) g.fillRect(9, y, 28, 2.4); }
    });
    const tag = (fill) => canvas(20, 20, (g) => {                                      // mileposts, exits
      g.fillStyle = fill; g.strokeStyle = white; g.lineWidth = 2;
      g.beginPath(); g.roundRect(2, 2, 16, 16, 3); g.fill(); g.stroke();
    });
    const camDown = canvas(44, 44, (g) => {                                           // dim, with a red "!" badge
      g.translate(22, 22);
      g.globalAlpha = 0.55;
      g.fillStyle = DARK; g.strokeStyle = dim; g.lineWidth = 3;
      g.beginPath(); g.arc(0, 0, 18.5, 0, 2 * Math.PI); g.fill(); g.stroke();
      g.fillStyle = white;
      g.beginPath(); g.roundRect(-11, -6.5, 15, 13, 2.5); g.fill();
      g.beginPath(); g.moveTo(4, -1.5); g.lineTo(11, -6); g.lineTo(11, 6); g.lineTo(4, 1.5); g.closePath(); g.fill();
      g.globalAlpha = 1;
      g.fillStyle = err; g.strokeStyle = DARK; g.lineWidth = 2;
      g.beginPath(); g.arc(13, -13, 7.5, 0, 2 * Math.PI); g.fill(); g.stroke();
      g.fillStyle = white; g.fillRect(12, -17.5, 2.2, 6); g.fillRect(12, -10.5, 2.2, 2.2);
    });
    return { "ml-cam": cam(info), "ml-cam-off": cam(dim), "ml-cam-down": camDown, "ml-inc": tri(err), "ml-closed": closed, "ml-work": work,
             "ml-sign": sign(true), "ml-sign-blank": sign(false), "ml-mp": tag(ok), "ml-exit": tag("#1F7A45") };
  }

  // ---- GeoJSON from the data ------------------------------------------------------------------------------
  const pt = (o, props) => ({ type: "Feature", properties: props, geometry: { type: "Point", coordinates: [o.lon, o.lat] } });
  const fc = (features) => ({ type: "FeatureCollection", features });
  function features(d, key) {
    if (!d) return fc([]);
    if (key === "cameras") {
      return fc((d.cameras || []).filter((c) => !(prefs.hideDown && c.down)).map((c) => pt(c, {
        id: c.id, icon: c.down ? "ml-cam-down" : c.off ? "ml-cam-off" : "ml-cam", off: !!c.off, down: !!c.down,
        label: c.down ? "camera down" : "" })));
    }
    if (key === "incidents" || key === "roadwork") {
      return fc((d.events || []).filter((e) => e.layer === key).map((e) => pt(e, {
        id: e.id, icon: key === "roadwork" ? "ml-work" : e.closed ? "ml-closed" : "ml-inc" })));
    }
    if (key === "signs") return fc((d.signs || []).map((s) => pt(s, { id: s.id, icon: (s.lines || []).length ? "ml-sign" : "ml-sign-blank" })));
    if (key === "mileposts") {                // whole miles from zoom 12 (with the exits), the tenths from 14
      return fc((d.mileposts || []).map((m) => pt(m, { label: m.value ? `${m.value}` : m.label,
                                                       whole: !m.value || Math.abs(Number(m.value) % 1) < 1e-6 })));
    }
    if (key === "exits") return fc((d.exits || []).map((x) => pt(x, { label: x.value ? `Exit ${x.value}` : x.label, name: x.label })));
    return fc([]);
  }
  const available = (d, l) => (l.plugin ? !!(plugin(l) && plugin(l).ready()) : !!d && (l.feed ? !!d.configured : !!d.highway_places));

  // ---- one map's layers and its Layers control --------------------------------------------------------------
  const maps = new Set();
  const groups = [];               // extra layer groups (addGroup), attached to every control, present and future
  function addGroup(g) {
    groups.push(g);
    for (const m of maps) { try { m.groups.push(g.attach(m)); if (!m.panel.hidden) m.renderPanel(); } catch (e) { console.error(e); } }
  }

  class MapRoads {
    constructor(map, host, ctrls, opts = {}) {
      this.map = map;
      map.on("zoomend", () => { if (!this.panel.hidden) this.renderPanel(); });   // the "zoom in" hints follow the zoom
      this.host = host;
      this.opts = opts;
      this.ready = false;
      this.sigs = {};
      this.force = {};          // layers on for this map's event (airport: an event at the field, #87), until unticked
      this.profile = opts.layers === "incident" ? "incident" : "overview";   // which remembered set (#111)
      this.focused = null;      // the map's event (focus(inc)) and, on an event's map, its own layer set
      this.focusId = null;
      this.override = null;     // the event's own {key: on}, or null: the profile's set
      this.overrideLocal = false;   // true: this viewer's unsaved copy (a viewer, or a closed event)
      this.pending = 0;         // saves in flight
      this.savedAt = 0;
      this.btn = h("button", "cm-btn sm");
      this.btn.type = "button";
      if (window.Icon) this.btn.append(Icon("layers"));
      this.btn.append("Layers");
      this.btn.title = "Map layers: cameras, incidents, roadwork, signs, mileposts, exits, the airport";
      this.btn.setAttribute("aria-expanded", "false");
      this.btn.setAttribute("aria-haspopup", "true");
      this.btn.addEventListener("click", (e) => { e.stopPropagation(); this.toggle(); });
      ctrls.append(this.btn);
      this.panel = h("div", "cm-pop ml-panel");
      this.panel.hidden = true;
      this.panel.setAttribute("role", "group");
      this.panel.setAttribute("aria-label", "Map layers");
      this.panel.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); this.close(); this.btn.focus(); } });
      this.panel.addEventListener("click", (e) => e.stopPropagation());
      this.info = h("div", "cm-pop ml-info");
      this.info.hidden = true;
      this.info.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); this.closeInfo(); } });
      this.info.addEventListener("click", (e) => e.stopPropagation());
      host.append(this.panel, this.info);
      map.on("click", () => { this.close(); this.closeInfo(); });
      const init = () => {                 // once the map's style and the first data are both in (the attribution)
        if (this.ready) return;
        this.mapReady = true;
        if (!store.data) return;
        try { this.init(); } catch (e) { map.once("idle", init); }
      };
      this._init = init;
      if (map.isStyleLoaded() || map.loaded()) init();
      else { map.once("load", init); map.once("idle", init); }
      maps.add(this);
      this.groups = [];
      for (const g of groups) { try { this.groups.push(g.attach(this)); } catch (e) { console.error(e); } }
      subscribe((d) => this.update(d));
    }

    init() {
      const map = this.map, set = icons();
      for (const [name, img] of Object.entries(set)) if (!map.hasImage(name)) map.addImage(name, img, { pixelRatio: 2 });
      const before = map.getLayer("firehouse") ? "firehouse" : undefined;
      const glyphs = !!(map.getStyle() && map.getStyle().glyphs);
      if (window.AirportLayer) {                       // the airport under the markers (airport.js, #87)
        AirportLayer.init(map, before, glyphs);
        AirportLayer.subscribe(() => { this.focus(this.focused); this.applyVisibility(); if (!this.panel.hidden) this.renderPanel(); });
      }
      for (const l of LAYERS) {
        if (l.plugin) continue;
        const src = `ml-${l.key}`;
        if (map.getSource(src)) continue;
        const attr = l.key === "cameras" ? attributionFor(store.data) : "";
        map.addSource(src, { type: "geojson", data: fc([]), ...(attr ? { attribution: attr } : {}) });
        const vis = this.on(l.key) ? "visible" : "none";
        if (l.key === "mileposts" || l.key === "exits") {
          if (l.key === "mileposts") {
            map.addLayer({ id: `${src}-tenths`, type: "symbol", source: src, minzoom: 14, filter: ["!", ["get", "whole"]],
              layout: { visibility: vis, "icon-image": "ml-mp", "icon-allow-overlap": true, "icon-size": 0.45,
                        ...(glyphs ? { "text-field": ["get", "label"], "text-font": ["Noto Sans Regular"], "text-size": 10,
                                       "text-anchor": "left", "text-offset": [0.7, 0], "text-optional": true } : {}) },
              paint: glyphs ? { "text-color": "#C9CED4", "text-halo-color": "#15181B", "text-halo-width": 1.4 } : {},
            }, before);
          }
          map.addLayer({ id: src, type: "symbol", source: src, minzoom: l.minzoom,
            ...(l.key === "mileposts" ? { filter: ["get", "whole"] } : {}),
            layout: { visibility: vis, "icon-image": l.key === "exits" ? "ml-exit" : "ml-mp", "icon-allow-overlap": true,
                      "icon-size": l.key === "exits" ? 0.9 : 0.6,
                      ...(glyphs ? { "text-field": ["get", "label"], "text-font": ["Noto Sans Bold"], "text-size": l.key === "exits" ? 12 : 11,
                                     "text-anchor": "left", "text-offset": [0.9, 0], "text-optional": true } : {}) },
            paint: glyphs ? { "text-color": "#E6E8EB", "text-halo-color": "#15181B", "text-halo-width": 1.6 } : {},
          }, before);
          continue;
        }
        map.addLayer({ id: src, type: "symbol", source: src, minzoom: l.minzoom,
          layout: { visibility: vis, "icon-image": ["get", "icon"], "icon-allow-overlap": true, "icon-ignore-placement": true,
                    "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.6, 14, 0.85, 18, 1] },
          paint: l.key === "cameras" ? { "icon-opacity": ["case", ["get", "off"], 0.6, 1] } : {},
        }, before);
        map.on("click", src, (e) => {
          const f = e.features && e.features[0];
          if (!f) return;
          e.originalEvent && e.originalEvent.stopPropagation();
          setTimeout(() => this.clicked(l.key, f.properties.id), 0);   // after the map's own click (it closes the box)
        });
        map.on("mouseenter", src, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", src, () => { map.getCanvas().style.cursor = ""; });
      }
      this.ready = true;
      if (store.data) this.update(store.data);
    }

    update(d) {
      if (!this.ready && this.mapReady && this._init) this._init();
      if (this.ready) {
        for (const l of LAYERS) {
          if (l.plugin) continue;
          const data = features(d, l.key), sig = JSON.stringify(data);
          if (this.sigs[l.key] === sig) continue;
          this.sigs[l.key] = sig;
          const src = this.map.getSource(`ml-${l.key}`);
          if (src) src.setData(data);
        }
        this.applyVisibility();
      }
      if (!this.panel.hidden) this.renderPanel();
    }

    // is a layer on here: forced for the event (the airport), else the event's own set, else the viewer's set
    // for this map's profile, else the default (a group's layers say theirs)
    on(key, dflt) {
      if (this.force[key]) return true;
      if (this.override && typeof this.override[key] === "boolean") return this.override[key];
      const mine = prefs[this.profile][key];
      if (typeof mine === "boolean") return mine;
      return dflt === undefined ? !!PROFILES[this.profile][key] : !!dflt;
    }
    perEvent() { return this.profile === "incident" && !!this.focused; }
    focus(inc) {                  // the map's event (cadmap.js RigMap.update): its own layer set (#111); at the airport, that layer comes on
      this.focused = inc || null;
      const id = inc ? inc.id : null;
      let changed = false;
      if (id !== this.focusId) { this.focusId = id; this.overrideLocal = false; this.pending = 0; this.savedAt = 0; changed = true; }
      if (!this.overrideLocal && !this.pending && Date.now() - this.savedAt > 5000) {     // the hub's copy (not a save still on its way)
        const set = this.perEvent() && inc.map_layers && typeof inc.map_layers === "object" ? inc.map_layers : null;
        if (JSON.stringify(set) !== JSON.stringify(this.override)) { this.override = set; changed = true; }
      }
      const on = !!(window.AirportLayer && AirportLayer.near(inc));
      if (on !== !!this.force.airport) { this.force.airport = on; changed = true; }
      if (changed) this.refresh();
    }
    // every layer as it shows on this map now: the event's set when one is made
    effective() {
      const set = {};
      for (const l of LAYERS) set[l.key] = this.on(l.key);
      for (const g of this.groups) { try { for (const [k, d] of (g.keys ? g.keys() : [])) set[k] = this.on(k, d); } catch (e) { console.error(e); } }
      return set;
    }
    // a toggle: on an event's map the event's own set (saved on the event by a dispatcher while it is open;
    // a viewer's, or a closed event's, stays here), else the viewer's set for this profile
    setLayer(key, on) {
      on = !!on;
      if (key in this.force) this.force[key] = false;
      if (this.perEvent()) {
        const set = this.effective();
        set[key] = on;
        this.override = set;
        if (canWrite() && OPEN.includes(this.focused.status)) this.saveOverride(set);
        else { this.overrideLocal = true; prefs[this.profile][key] = on; savePrefs(); }
        this.refresh();
        return;
      }
      prefs[this.profile][key] = on;
      savePrefs();
      for (const m of maps) if (m.profile === this.profile && !m.override) m.refresh();
    }
    saveOverride(set) {
      const id = this.focusId;
      if (id == null) return;
      this.pending++;
      this.savedAt = Date.now();
      let op = "";
      try { op = window.operator ? operator() : ""; } catch (_) { /* no operator field on this page */ }
      fetch(`/api/cad/incident/${encodeURIComponent(id)}`, { method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ map_layers: set, operator: op }) })
        .then(async (r) => {
          if (r.ok) return;
          let why = `${r.status}`;
          try { why = (await r.json()).error || why; } catch (_) { /* not JSON */ }
          throw new Error(why);
        })
        .catch((e) => { if (window.UI && UI.toast) UI.toast(`Map layers not saved: ${e.message}`, "err"); })
        .finally(() => { if (this.focusId === id) this.pending = Math.max(0, this.pending - 1); });
    }
    resetOverride() {             // "Reset to default": the viewer's set again (and the event's own set dropped)
      const saved = this.override && !this.overrideLocal;
      this.override = null;
      this.overrideLocal = false;
      if (saved && canWrite()) this.saveOverride(null);
      this.refresh();
    }
    useAsDefault() {              // "Use as default": the event's set becomes this viewer's set for every event
      if (!this.override) return;
      prefs[this.profile] = { ...this.override };
      savePrefs();
      if (window.UI && UI.toast) UI.toast("Saved as your default for events");
      for (const m of maps) if (m.profile === this.profile && !m.override) m.refresh();
    }
    refresh() {
      this.applyVisibility();
      for (const g of this.groups) { try { if (g.refresh) g.refresh(); } catch (e) { console.error(e); } }
      if (!this.panel.hidden) this.renderPanel();
    }

    applyVisibility() {
      if (!this.ready) return;
      if (window.AirportLayer) AirportLayer.apply(this.map, this.on("airport"), this.on("approaches"));
      for (const l of LAYERS) {
        if (l.plugin) continue;
        for (const id of [`ml-${l.key}`, `ml-${l.key}-tenths`]) {
          if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", this.on(l.key) ? "visible" : "none");
        }
      }
    }

    toggle() { if (this.panel.hidden) this.open(); else this.close(); }
    open() {
      this.closeInfo();
      this.renderPanel();
      this.panel.hidden = false;
      this.btn.setAttribute("aria-expanded", "true");
      const first = this.panel.querySelector("input:not([disabled])");
      if (first) first.focus({ preventScroll: true });
    }
    close() {
      this.panel.hidden = true;
      this.btn.setAttribute("aria-expanded", "false");
    }

    renderPanel() {
      const d = store.data;
      const panel = this.panel;
      // a rebuild keeps the viewer's place (#111): the scroll, the focused control and each group's fold
      const scroll = panel.scrollTop;
      const active = document.activeElement && panel.contains(document.activeElement) ? document.activeElement.dataset.key || "" : "";
      const folds = {};
      for (const f of panel.querySelectorAll("details[data-fold]")) folds[f.dataset.fold] = f.open;
      const head = h("div", "ml-head");
      head.append(h("b", "", this.perEvent() && this.override ? "Layers for this event" : "Layers"));
      const x = h("button", "icon quiet sm");
      x.type = "button";
      x.setAttribute("aria-label", "Close");
      x.dataset.key = "close";
      x.append(window.Icon ? Icon("x") : "✕");
      x.addEventListener("click", () => { this.close(); this.btn.focus(); });
      head.append(h("span", "spacer"), x);
      const list = h("div", "ml-list");
      for (const l of LAYERS) {
        const ok = available(d, l) && !(l.sub && !this.on(l.sub));
        const lab = h("label", "check" + (l.sub ? " ml-sub" : ""));
        const cb = h("input");
        cb.type = "checkbox";
        cb.checked = this.on(l.key);
        cb.disabled = !ok;
        cb.dataset.key = l.key;
        cb.addEventListener("change", () => this.setLayer(l.key, cb.checked));
        lab.append(cb, h("span", "", l.label));
        if (l.key === "airport" && this.force.airport && !prefs[this.profile].airport) lab.append(h("span", "dim small", "event here"));
        if (l.plugin && plugin(l) && !plugin(l).ready() && plugin(l).note()) lab.title = plugin(l).note();
        if (ok && l.minzoom > 0 && this.map && this.map.getZoom() < l.minzoom) lab.append(h("span", "dim small", "zoom in"));
        const feed = l.key === "incidents" || l.key === "roadwork" ? "events" : l.key;
        if (ok && l.feed && (d.sources || []).some((s) => s.status && s.status[feed] && s.status[feed].error)) {
          lab.append(h("span", "ml-err small", "feed error"));
        }
        list.append(lab);
      }
      if (d && (d.cameras || []).some((c) => c.down)) {                 // down cameras (#99): dim with a badge, or hidden
        const lab = h("label", "check ml-hide-down");
        const cb = h("input");
        cb.type = "checkbox";
        cb.checked = !!prefs.hideDown;
        cb.dataset.key = "hideDown";
        cb.addEventListener("change", () => setHideDown(cb.checked));
        const n = (d.cameras || []).filter((c) => c.down).length;
        lab.append(cb, h("span", "", `Hide down cameras (${n})`));
        list.append(lab);
      }
      panel.replaceChildren(head);
      if (this.perEvent()) panel.append(this.eventRow());
      panel.append(list);
      if (!d) this.panel.append(h("div", "dim small", "Loading…"));
      else if (!d.configured) this.panel.append(h("div", "dim small", "No camera or incident source is on: " + (d.note || "not configured on the hub")));
      if (d && !d.highway_places) this.panel.append(h("div", "dim small", "Mileposts and exits: not in the places database yet"));
      for (const s of (d && d.sources) || []) {                   // each source that is on, and when it last read
        if (!s.configured) continue;
        const line = h("div", "dim small ml-source");
        const a = h("a", "", SOURCE_NOTE[s.short] || s.label);
        a.href = s.link;
        a.target = "_blank";
        a.rel = "noopener";
        line.append(a, s.at ? `, updated ${hhmm(s.at)}` : ", waiting for the first read");
        this.panel.append(line);
      }
      if (d && d.configured && d.note) this.panel.append(h("div", "dim small", d.note));
      if (window.AirportLayer && AirportLayer.credit()) this.panel.append(h("div", "dim small", AirportLayer.credit()));
      for (const g of this.groups) {
        try { const el = g.panel && g.panel(folds); if (el) panel.append(el); } catch (e) { console.error(e); }
      }
      panel.scrollTop = scroll;
      if (active) {
        const el = panel.querySelector(`[data-key="${CSS.escape(active)}"]`);
        if (el) el.focus({ preventScroll: true });
      }
    }

    // on an event's map (#111): whose set this is, "Reset to default" and "Use as default" when the event has its own
    eventRow() {
      const row = h("div", "ml-event");
      if (!this.override) {
        row.append(h("span", "dim small", canWrite() && OPEN.includes(this.focused.status) ? "A change here is kept on this event" : "A change here is yours for this event"));
        return row;
      }
      row.append(h("span", "dim small", this.overrideLocal ? "Your layers for this event" : "Kept on this event for everyone"));
      const reset = h("button", "quiet sm", "Reset to default");
      reset.type = "button";
      reset.dataset.key = "reset";
      reset.addEventListener("click", () => this.resetOverride());
      const keep = h("button", "quiet sm", "Use as default");
      keep.type = "button";
      keep.dataset.key = "default";
      keep.title = "These layers on every event's map for you";
      keep.addEventListener("click", () => this.useAsDefault());
      row.append(reset, keep);
      return row;
    }

    clicked(key, id) {
      this.close();
      if (key === "cameras") return openCamera(id);
      const d = store.data || {};
      if (key === "signs") {
        const s = (d.signs || []).find((x) => String(x.id) === String(id));
        if (s) this.showInfo(s.name, [s.road, s.dir, s.mile ? `MP ${s.mile}` : ""].filter(Boolean).join(" "),
                             (s.lines || []).length ? s.lines : ["(blank)"],
                             `${s.source || "511NY"}` + (s.updated ? `, sign updated ${when(s.updated)}` : ""), "ml-sign-text");
        return;
      }
      const e = (d.events || []).find((x) => String(x.id) === String(id));
      if (!e) return;
      const kind = e.layer === "roadwork" ? "Roadwork" : e.closed ? "Closure" : "Incident";
      const title = kind + (e.subtype && e.subtype.toLowerCase() !== kind.toLowerCase() ? `: ${e.subtype}` : "");
      const sub = [e.road, e.dir, e.mile ? `MP ${e.mile}` : ""].filter(Boolean).join(" ");
      const lines = [e.description || "(no description)"];
      if (e.lanes) lines.push(`Lanes: ${e.lanes}`);
      if (e.end) lines.push(`Planned end ${when(e.end)}`);
      const t = e.updated || e.start;
      this.showInfo(title, sub, lines, `${e.source || "511NY"}` + (t ? ` updated ${when(t)}` : ""), "");
    }

    showInfo(title, sub, lines, foot, cls, icon) {
      const head = h("div", "ml-head");
      if (icon) head.append(icon);
      head.append(h("b", "", title));
      const x = h("button", "icon quiet sm");
      x.type = "button";
      x.setAttribute("aria-label", "Close");
      x.append(window.Icon ? Icon("x") : "✕");
      x.addEventListener("click", () => this.closeInfo());
      head.append(h("span", "spacer"), x);
      const body = h("div", "ml-lines" + (cls ? ` ${cls}` : ""));
      for (const l of lines) body.append(h("div", "", l));
      this.info.replaceChildren(head);
      if (sub) this.info.append(h("div", "dim small", sub));
      this.info.append(body);
      if (foot) this.info.append(h("div", "dim small", foot));
      this.info.hidden = false;
      x.focus({ preventScroll: true });
    }
    closeInfo() { this.info.hidden = true; }
  }

  // a viewer's set changed from outside a map's box (profile: "overview" | "incident")
  function setLayer(key, on, profile = "overview") {
    if (!PROFILES[profile] || !KEY_RE.test(key)) return;
    prefs[profile][key] = !!on;
    savePrefs();
    for (const m of maps) if (m.profile === profile && !m.override) m.refresh();
  }
  function setHideDown(on) {
    prefs.hideDown = !!on;
    savePrefs();
    for (const m of maps) {
      m.sigs.cameras = "";
      if (store.data) m.update(store.data);
      m.refresh();
    }
  }
  // a group's fold in the box (the County GIS sub-headings), remembered per viewer: true / false, undefined when never touched
  const fold = (name) => prefs.folds[name];
  function setFold(name, open) {
    prefs.folds[name] = !!open;
    savePrefs();
  }

  // ---- times ------------------------------------------------------------------------------------------------
  const tz = () => (window.cad && cad.scene && cad.scene.timezone) || "America/New_York";
  function hhmm(t) {
    try { return new Date(t * 1000).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz() }); }
    catch (_) { return ""; }
  }
  function when(t) {
    try {
      const d = new Date(t * 1000), now = new Date();
      const day = d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: tz() });
      const today = now.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: tz() });
      return (day === today ? "" : `${day} `) + hhmm(t);
    } catch (_) { return ""; }
  }
  function ago(t) {
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.floor(s / 60)} min ago` : `${Math.floor(s / 3600)} h ago`;
  }

  // ---- the camera viewer --------------------------------------------------------------------------------------
  const viewer = { ov: null, id: null, timer: null, url: null, taken: 0, busy: false, tick: null, down: null, cadence: 60,
                   live: false, hls: null, video: null };
  const cadenceText = (c) => { const n = (c && c.cadence) || 60; return n >= 60 ? "Snapshot, updated about every minute" : `Snapshot, updated every ${n} s`; };
  // Live video (#101): a camera with a stream plays it, muted, through the hub (/api/cad/cameras/<id>/stream/...),
  // with hls.js from cdnjs (or the browser's own HLS, Safari); anything wrong falls back to the snapshots. The
  // page says "Live" only while the stream plays; a snapshot is always "Snapshot, updated every N s".
  const HLS_JS = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.6.16/hls.min.js";
  // This page's viewer token (?v=, #125): the hub knows whose use a request is, and the viewer's close ends it at once
  // (GET .../stream/close?v=) instead of after the hub's idle timeout; the camera wall (camwall.js) has its own.
  const VIEWER = "dlg" + Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(36).padStart(2, "0")).join("").slice(0, 12);
  let hlsLoad = null;
  function loadHls() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!hlsLoad) {
      hlsLoad = new Promise((ok, no) => {
        const sc = document.createElement("script");
        sc.src = HLS_JS;
        sc.async = true;
        sc.onload = () => (window.Hls ? ok(window.Hls) : no(new Error("hls.js did not load")));
        sc.onerror = () => no(new Error("hls.js did not load"));
        document.head.append(sc);
      }).catch((e) => { hlsLoad = null; throw e; });
    }
    return hlsLoad;
  }
  function note(text) {                      // a small line over the picture (connecting, buffering, low frame rate)
    if (!viewer.el) return;
    viewer.el.msg.hidden = false;
    viewer.el.msg.textContent = text;
  }
  async function pollStatus() {              // while live: the hub's measure of the stream (its frame rate at the source)
    const id = viewer.id;
    if (!id || !viewer.live) return;
    try {
      const r = await fetch(`/api/cad/cameras/${encodeURIComponent(id)}/stream/status`, { credentials: "same-origin", cache: "no-store" });
      if (!r.ok || id !== viewer.id) return;
      const st = await r.json();
      viewer.fps = st.fps;
      if (st.fps != null && st.fps < 5) {
        viewer.el.time.textContent = `Live · low frame rate from the source (~${st.fps} fps)`;
        if (viewer.el.msg.hidden || viewer.el.msg.textContent.startsWith("Buffering")) note(`Low frame rate from the source (~${st.fps} fps): the picture updates about ${st.fps >= 1 ? "once a second" : "every few seconds"}`);
      } else if (viewer.el.msg.textContent.startsWith("Low frame rate")) viewer.el.msg.hidden = true;
    } catch (_) { /* next time */ }
  }
  function stopLive() {
    if (viewer.liveId) {
      fetch(`/api/cad/cameras/${encodeURIComponent(viewer.liveId)}/stream/close?v=${VIEWER}`, { credentials: "same-origin", cache: "no-store", keepalive: true }).catch(() => {});
      viewer.liveId = null;
    }
    clearInterval(viewer.statusTimer);
    viewer.statusTimer = null;
    viewer.fps = null;
    if (viewer.hls) { try { viewer.hls.destroy(); } catch (_) { /* gone */ } viewer.hls = null; }
    if (viewer.video) { try { viewer.video.pause(); viewer.video.removeAttribute("src"); viewer.video.load(); } catch (_) { /* gone */ } }
    if (viewer.el) { viewer.el.video.hidden = true; viewer.el.live.hidden = true; }
    viewer.live = false;
  }
  function liveFailed(why) {               // back to snapshots, and say so once
    if (!viewer.id) return;
    stopLive();
    viewer.el.msg.hidden = false;
    viewer.el.msg.textContent = `No live video (${why}): snapshots instead`;
    setTimeout(() => { if (viewer.el.msg.textContent.startsWith("No live video")) viewer.el.msg.hidden = true; }, 6000);
    showFoot();
    refreshCamera();
    if (document.visibilityState !== "hidden") startRefresh();
  }
  function startLive(id) {
    const url = `/api/cad/cameras/${encodeURIComponent(id)}/stream/playlist.m3u8?v=${VIEWER}`;
    viewer.liveId = String(id);
    const v = viewer.el.video;
    const playing = () => {
      if (viewer.id !== String(id)) return;
      viewer.live = true;
      stopRefresh();
      v.hidden = false;
      viewer.el.img.hidden = true;
      if (!viewer.el.msg.textContent.startsWith("Low frame rate")) viewer.el.msg.hidden = true;
      viewer.el.live.hidden = false;
      viewer.el.time.textContent = "Live" + (viewer.fps != null && viewer.fps < 5 ? ` · low frame rate from the source (~${viewer.fps} fps)` : "");
      showFoot();
      if (!viewer.statusTimer) viewer.statusTimer = setInterval(pollStatus, 5000);
    };
    v.onplaying = playing;
    v.onerror = () => liveFailed("the stream stopped");
    const native = () => {                                           // Safari on iPhone (no MSE): its own HLS
      if (!v.canPlayType("application/vnd.apple.mpegurl")) return liveFailed("no HLS here");
      v.src = url;
      v.play().catch(() => liveFailed("not allowed to play"));
    };
    loadHls().then((Hls) => {
      if (viewer.id !== String(id)) return;
      if (!Hls.isSupported()) return native();
      // Live tuning: sit 3 segments behind the edge with up to 30 s buffered (the hub keeps the newest segments
      // fetched ahead, so a segment is one local hop); a playlist that doesn't come in 8 s is a source that is
      // down (the hub already gave up at 6 s and says so): no retries, snapshots at once.
      const hls = new Hls({ enableWorker: false, lowLatencyMode: false, liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8,
                            maxBufferLength: 30, backBufferLength: 10, manifestLoadingTimeOut: 8000, manifestLoadingMaxRetry: 0,
                            levelLoadingTimeOut: 12000, levelLoadingMaxRetry: 2, fragLoadingTimeOut: 15000, fragLoadingMaxRetry: 2 });
      viewer.hls = hls;
      note("Connecting to the live stream…");
      hls.on(Hls.Events.ERROR, (_, data) => {
        if (!data) return;
        const code = data.response && data.response.code;
        if (data.fatal) {
          const r = code === 429 ? "the hub has enough streams open" : code === 503 ? "no live video from the source right now"
            : data.details === "manifestLoadTimeOut" ? "the source didn't answer" : data.details || "stream error";
          liveFailed(r);
        } else if (data.details === "bufferStalledError" || data.details === "bufferNudgeOnStall" || data.details === "bufferSeekOverHole") {
          note("Buffering…");
        }
      });
      hls.loadSource(url);
      hls.attachMedia(v);
      hls.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(() => liveFailed("not allowed to play")));
      v.onwaiting = () => { if (viewer.live) note("Buffering…"); };
    }).catch(() => native());                                       // hls.js didn't load: the browser's own, or snapshots
  }
  function showFoot() {
    const c = camById(viewer.id);
    const src = ((store.data && store.data.sources) || []).find((s) => c && s.short === c.source);
    const a = h("a", "", SOURCE_NOTE[c && c.source] || (src ? src.label : "the source"));
    a.href = src ? src.link : "#";
    a.target = "_blank";
    a.rel = "noopener";
    if (viewer.live) viewer.el.foot.replaceChildren("Live video through the hub (", a, "), muted, the newest segments fetched ahead. Only while this is open.");
    else viewer.el.foot.replaceChildren(`${cadenceText(c)}, through the hub (`, a, "). Looked at every 10 s while this is open.");
  }

  function buildViewer() {
    const ov = h("div", "overlay");
    ov.id = "ml-camera";
    ov.hidden = true;
    const panel = h("div", "dialog ml-cam-dialog");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-labelledby", "ml-cam-title");
    const head = h("div", "dialog-head");
    const title = h("h2", "dialog-title ml-cam-title");
    title.id = "ml-cam-title";
    const x = h("button", "icon quiet");
    x.type = "button";
    x.setAttribute("aria-label", "Close");
    x.title = "Close (Esc)";
    x.append(window.Icon ? Icon("x") : "✕");
    x.addEventListener("click", closeCamera);
    head.append(title, h("span", "spacer"), x);
    const body = h("div", "dialog-body ml-cam-body");
    const frame = h("div", "ml-cam-frame");
    const img = h("img", "ml-cam-img");
    img.alt = "";
    const video = h("video", "ml-cam-video");
    video.muted = true;
    video.autoplay = true;
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.setAttribute("muted", "");
    video.hidden = true;
    const live = h("span", "ml-live", "Live");
    live.hidden = true;
    live.setAttribute("aria-label", "Live video");
    const msg = h("div", "ml-cam-msg dim");
    frame.append(video, img, live, msg);
    const meta = h("div", "ml-cam-meta");
    const where = h("span", "ml-cam-where");
    const time = h("span", "ml-cam-time dim small");
    time.setAttribute("aria-live", "polite");
    meta.append(where, time);
    const foot = h("div", "dim small ml-cam-src");
    body.append(frame, meta, foot);
    panel.append(head, body);
    ov.append(panel);
    document.body.append(ov);
    viewer.ov = ov;
    viewer.el = { title, img, msg, where, time, foot, video, live };
    viewer.video = video;
    document.addEventListener("visibilitychange", () => {
      if (!viewer.id) return;
      if (document.visibilityState === "hidden") {
        stopRefresh();
        if (viewer.live) { try { video.pause(); if (viewer.hls) viewer.hls.stopLoad(); } catch (_) { /* fine */ } }
      } else if (viewer.live) {
        try { if (viewer.hls) viewer.hls.startLoad(); video.play().catch(() => {}); } catch (_) { /* fine */ }
      } else { refreshCamera(); startRefresh(); }
    });
  }

  function startRefresh() {                  // snapshots: at the source's own cadence (the county's 8 s, the Thruway's minute)
    stopRefresh();
    const c = camById(viewer.id);
    const every = Math.max(4000, ((c && c.cadence) || 60) * 1000);
    viewer.timer = setInterval(refreshCamera, Math.min(every, 60000));
    viewer.tick = setInterval(showTime, 1000);
  }
  function stopRefresh() {
    clearInterval(viewer.timer);
    clearInterval(viewer.tick);
    viewer.timer = viewer.tick = null;
    if (viewer.id) viewer.el.time.textContent = viewer.taken ? `Image ${hhmm(viewer.taken)}, paused` : "Paused";
  }
  function showTime() {
    if (!viewer.id) return;
    if (viewer.down) {
      const d = viewer.down;
      viewer.el.time.textContent = `Down since ${when(d.down_since)}` + (d.last_image ? ` (last image ${when(d.last_image)})` : " (no image)");
      return;
    }
    if (!viewer.taken) return;
    viewer.el.time.textContent = `Image ${when(viewer.taken)} (${ago(viewer.taken)})${viewer.stale ? ", camera not answering" : ""}`;
  }

  async function refreshCamera() {
    const id = viewer.id;
    if (!id || viewer.busy) return;
    viewer.busy = true;
    try {
      const cond = viewer.etag ? { "If-None-Match": viewer.etag } : {};
      const r = await fetch(`/api/cad/cameras/${encodeURIComponent(id)}/image`, { credentials: "same-origin", cache: "no-store", headers: cond });
      if (id !== viewer.id) return;
      if (r.status === 304) return;                           // the hub's picture is the one we show
      if (!r.ok) {
        const e = await r.json().catch(() => null);
        if (e && e.down) {                                   // down (#99): say so, not the old picture
          viewer.down = e;
          viewer.el.img.hidden = true;
          viewer.el.msg.hidden = false;
          viewer.el.msg.textContent = "Camera down: no current image";
          showTime();
          return;
        }
        throw new Error((e && e.error) || `${r.status} ${r.statusText}`);
      }
      viewer.down = null;
      const blob = await r.blob();
      if (id !== viewer.id) return;
      const url = URL.createObjectURL(blob);
      const old = viewer.url;
      viewer.url = url;
      viewer.el.img.src = url;
      viewer.el.img.hidden = false;
      if (old) setTimeout(() => URL.revokeObjectURL(old), 1000);
      viewer.taken = Number(r.headers.get("X-Image-Time")) || Date.now() / 1000;
      viewer.etag = r.headers.get("ETag") || null;
      viewer.stale = r.headers.get("X-Image-Stale") === "1";
      viewer.el.msg.hidden = true;
      showTime();
    } catch (e) {
      if (id !== viewer.id) return;
      viewer.el.msg.hidden = false;
      viewer.el.msg.textContent = `No image: ${e.message}`;
      if (!viewer.url) viewer.el.img.hidden = true;
    } finally {
      viewer.busy = false;
    }
  }

  function openCamera(id, opener) {
    if (!viewer.ov) buildViewer();
    const c = camById(id);
    viewer.id = String(id);
    viewer.taken = 0;
    viewer.etag = null;
    viewer.stale = false;
    viewer.down = c && c.down ? { down: true, down_since: c.down_since, last_image: c.last_image } : null;
    viewer.cadence = (c && c.cadence) || 60;
    const el = viewer.el;
    el.title.textContent = c ? c.name : `Camera ${id}`;
    el.where.textContent = c ? [c.road, c.dir, c.mile ? `MP ${c.mile}` : ""].filter(Boolean).join(" · ") || "" : "";
    stopLive();
    showFoot();
    el.img.hidden = true;
    el.img.alt = c ? `Traffic camera: ${c.name}` : "Traffic camera";
    el.msg.hidden = false;
    el.msg.textContent = c && c.down ? "Camera down: no current image"
      : c && c.off ? `This camera is marked out of service by ${c.source || "its source"}: trying anyway…` : "Loading…";
    el.time.textContent = "";
    if (viewer.down) showTime();
    if (viewer.url) { URL.revokeObjectURL(viewer.url); viewer.url = null; }
    if (window.UI && UI.dialog) UI.dialog.open(viewer.ov, { onClose: stopCamera, opener });
    else viewer.ov.hidden = false;
    refreshCamera();                                             // a picture at once, even when video follows
    if (document.visibilityState !== "hidden") startRefresh();
    if (c && c.has_video && !c.down && !c.off) startLive(c.id);
    return true;
  }
  function stopCamera() {
    stopLive();
    clearInterval(viewer.timer);
    clearInterval(viewer.tick);
    viewer.timer = viewer.tick = null;
    viewer.id = null;
    if (viewer.url) { URL.revokeObjectURL(viewer.url); viewer.url = null; }
    if (viewer.el) viewer.el.img.removeAttribute("src");
  }
  function closeCamera() {
    if (!viewer.ov) return;
    if (window.UI && UI.dialog) UI.dialog.close(viewer.ov);
    else { viewer.ov.hidden = true; stopCamera(); }
  }

  // ---- the incident viewer's Cameras panel (#95): just before and just after along the road, live ------------
  // MapLayers.cameras(inc, host, {small}): the hub picks them (GET /api/cad/cameras?for=lat,lon&label=, sizeup/roads.py
  // cameras_for): on a highway the camera just before and just after the event by our mile markers, in its direction
  // of travel when the location says one ("0.3 mi before (SB) · I-95 at Interchange 21"), else each side by compass
  // ("0.4 mi south"); then "Nearby" (within 300 m as the crow flies), lower. Collapsible, open while the event is
  // open; thumbnails through the proxy, a new one every 10 s while the page is visible; a click opens the viewer.
  // {small}: the wall board's strip for the active highway event (the two along the road only, smaller).
  function km(a, b, c, d) {
    const p = Math.PI / 180;
    const x = Math.sin((c - a) * p / 2) ** 2 + Math.cos(a * p) * Math.cos(c * p) * Math.sin((d - b) * p / 2) ** 2;
    return 12742 * Math.asin(Math.min(1, Math.sqrt(x)));
  }
  function nearest(lat, lon, n = NEAR_N, within = NEAR_KM) {
    return ((store.data && store.data.cameras) || []).filter((c) => !c.off)
      .map((c) => [km(lat, lon, c.lat, c.lon), c]).filter(([k]) => k <= within)
      .sort((x, y) => x[0] - y[0]).slice(0, n).map(([k, c]) => ({ ...c, km: k }));
  }

  const CAM_REL = { before: "before", after: "after", at: "at the location" };
  const panels = new Map();            // host -> {inc, key, data, open, timer, small}
  function cameras(inc, host, opts = {}) {
    if (!host) return;
    let st = panels.get(host);
    if (!st) {
      st = { inc: null, key: "", data: null, open: true, openFor: null, small: !!opts.small, rev: "" };
      panels.set(host, st);
      host.classList.add("ml-cams");
      if (st.small) host.classList.add("ml-cams-small");
      subscribe(() => { st.key = ""; cameras(st.inc, host); });            // the cameras changed: ask again
      st.timer = setInterval(() => {                                      // fresh snapshots while on screen
        if (document.visibilityState !== "hidden" && !host.hidden && st.open) drawCams(host, st, true);
      }, CAM_REFRESH_MS);
    }
    st.inc = inc || null;
    const ok = inc && inc.lat != null && inc.lon != null && store.data && store.data.configured;
    const key = ok ? JSON.stringify([inc.id, inc.lat, inc.lon, inc.geo_label || "", inc.address || ""]) : "";
    if (!ok) { st.key = ""; st.data = null; host.hidden = true; host.replaceChildren(); return; }
    if (inc.id !== st.openFor) {                   // the incident tab's panel starts folded to its one-line summary
      st.openFor = inc.id;                         // unless this viewer opened it last time (#149); the small one
      st.open = st.small ? isOpenEvent(inc) : stored(CAMS_OPEN_KEY) === "1";   // stays open while the event is
    }
    if (key === st.key) { drawCams(host, st, false); return; }
    st.key = key;
    const p = new URLSearchParams({ for: `${inc.lat},${inc.lon}`, label: inc.geo_label || "", address: inc.address || "" });
    fetch(`/api/cad/cameras?${p}`, { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (st.key !== key) return;
      st.data = d;
      drawCams(host, st, true);
    }).catch(() => { if (st.key === key) { st.data = null; host.hidden = true; } });
  }
  const isOpenEvent = (inc) => !["closed", "dismissed"].includes(inc.status);
  const CAMS_OPEN_KEY = "sizeup.cad.camsOpen";
  function camLabel(c) {
    if (c.rel === "nearby") return c.km != null ? `${Math.round(c.km * 1000)} m away` : "nearby";
    const mi = c.miles != null ? `${c.miles.toFixed(1)} mi ` : "";
    if (c.rel === "at") return "at the location";
    if (c.rel === "side") return `${mi}${c.side}`;
    return `${mi}${CAM_REL[c.rel] || c.rel}`;
  }
  function drawCams(host, st, fresh) {
    const d = st.data, inc = st.inc;
    const cams = (d && d.cameras) || [];
    const shown = st.small ? cams.filter((c) => c.rel !== "nearby") : cams;
    const down = st.small ? [] : ((d && d.down) || []);
    const sig = JSON.stringify([shown.map((c) => [c.id, c.rel, c.miles]), down.map((c) => c.id), st.open, st.downOpen, d && d.highway]);
    if (!shown.length && !down.length) { host.hidden = true; host.replaceChildren(); st.sig = ""; return; }
    host.hidden = false;
    const stamp = Math.floor(Date.now() / CAM_REFRESH_MS);
    if (sig === st.sig && !fresh) return;
    if (sig === st.sig) {                                                   // the same cameras: new pictures only
      for (const img of host.querySelectorAll("img[data-camera]")) img.src = `/api/cad/cameras/${encodeURIComponent(img.dataset.camera)}/image?t=${stamp}`;
      return;
    }
    st.sig = sig;
    const head = h("button", "ml-cams-head");
    head.type = "button";
    head.setAttribute("aria-expanded", String(st.open));
    if (window.Icon) head.append(Icon("camera"));
    const hw = d.highway;
    const title = hw ? `Cameras on ${hw.road}${hw.dir ? ` ${hw.dir}` : ""}` : "Cameras nearby";
    head.append(h("b", "", title));
    if (hw) head.append(h("span", "dim small", `MP ${hw.mile.toFixed(1)}`));
    const srcs = [...new Set(shown.map((c) => c.source || "511NY"))].join(" · ");
    if (!st.open) {                                        // folded: just how many, so it is one line
      const n = shown.length, parts = [];
      if (n) parts.push(n === 1 ? "1 camera" : `${n} cameras`);
      if (down.length) parts.push(`${down.length} down`);
      head.append(h("span", "dim small ml-cams-count", parts.join(" · ")));
    }
    head.append(h("span", "spacer"), h("span", "dim small", srcs), h("span", "car dim", st.open ? "▾" : "▸"));
    head.addEventListener("click", () => {
      st.open = !st.open; st.sig = "";
      if (!st.small) remember(CAMS_OPEN_KEY, st.open ? "1" : "0");
      drawCams(host, st, true);
    });
    host.replaceChildren(head);
    if (!st.open) return;
    const along = shown.filter((c) => c.rel !== "nearby"), near = shown.filter((c) => c.rel === "nearby");
    const list = (cs) => {
      const ul = h("div", "ml-near-list");
      for (const c of cs) {
        const b = h("button", "ml-thumb");
        b.type = "button";
        b.dataset.camera = c.id;
        b.title = `${c.name}${c.road ? `, ${c.road}` : ""}${c.dir ? ` ${c.dir}` : ""}${c.mile ? ` MP ${c.mile}` : ""} (${c.source || "511NY"}). Open live`;
        const img = h("img", "ml-thumb-img");
        img.alt = "";
        img.loading = "lazy";
        img.dataset.camera = c.id;
        img.src = `/api/cad/cameras/${encodeURIComponent(c.id)}/image?t=${stamp}`;
        img.addEventListener("error", () => img.classList.add("ml-broken"));
        const lbl = h("span", "ml-thumb-lbl" + (c.facing ? " facing" : ""), camLabel(c) + (c.rel !== "nearby" && hw && hw.dir && c.rel !== "at" ? ` (${hw.dir})` : ""));
        if (c.facing) lbl.title = "This camera is over the event's carriageway";
        b.append(img, lbl, h("span", "ml-thumb-cap", c.name));
        b.addEventListener("click", () => openCamera(c.id, b));
        ul.append(b);
      }
      return ul;
    };
    if (along.length) host.append(list(along));
    if (near.length) {
      if (along.length) host.append(h("div", "ml-cams-sub dim small", "Nearby"));
      host.append(list(near));
    }
    if (down.length) {                                     // folded away below the live ones (#99)
      const row = h("button", "ml-cams-down");
      row.type = "button";
      row.setAttribute("aria-expanded", String(!!st.downOpen));
      row.append(h("span", "ml-down-badge", "!"), h("span", "", down.length === 1 ? "1 camera down" : `${down.length} cameras down`),
                 h("span", "car dim", st.downOpen ? "▾" : "▸"));
      row.addEventListener("click", () => { st.downOpen = !st.downOpen; st.sig = ""; drawCams(host, st, true); });
      host.append(row);
      if (st.downOpen) {
        const ul = h("div", "ml-down-list");
        for (const c of down) {
          const b = h("button", "ml-down-item");
          b.type = "button";
          b.dataset.camera = c.id;
          b.setAttribute("aria-label", `camera down: ${c.name}`);
          b.append(h("span", "", c.name), h("span", "dim small", (c.miles != null ? `${c.miles.toFixed(1)} mi` : `${Math.round((c.km || 0) * 1000)} m`)
            + (c.down_since ? `, down since ${when(c.down_since)}` : "")));
          b.addEventListener("click", () => openCamera(c.id, b));
          ul.append(b);
        }
        host.append(ul);
      }
    }
  }
  function nearby(inc, mapbox) {            // (the #73 row: now the panel; kept for a page that still calls it)
    const host = mapbox && mapbox.parentElement && mapbox.parentElement.querySelector(".ml-cams");
    if (host) cameras(inc, host);
  }

  // ---- the place search hook ("@", #67) -----------------------------------------------------------------------
  async function searchPlaces(q, near) {
    if (!q || !store.data || !store.data.configured) return [];
    try {
      const p = new URLSearchParams({ q, n: "5" });
      if (near && near.lat != null) p.set("near", `${near.lat},${near.lon}`);
      const r = await fetch(`/api/cad/cameras?${p}`, { credentials: "same-origin" });
      if (!r.ok) return [];
      return (await r.json()).cameras || [];
    } catch (_) { return []; }
  }
  function openPlace(p, opener) {
    if (!p || p.kind !== "camera" || !p.camera) return false;
    return openCamera(p.camera, opener);
  }
  // a camera chip anywhere (a note's "@camera"): data-camera="<id>"
  document.addEventListener("click", (e) => {
    const t = e.target && e.target.closest && e.target.closest("[data-camera]");
    if (!t || t.classList.contains("ml-thumb")) return;
    e.preventDefault();
    openCamera(t.dataset.camera, t);
  });

  function attach(map, host, ctrls, opts) {
    try { return new MapRoads(map, host, ctrls, opts); } catch (e) { console.error("map layers:", e); return null; }
  }

  return { attach, addGroup, openCamera, closeCamera, cameras, nearby, nearest, searchPlaces, openPlace, prefs, setLayer, setHideDown,
           loadHls, subscribe,
           fold, setFold, PROFILES, LAYERS, load, data: () => store.data, maps };
})();
