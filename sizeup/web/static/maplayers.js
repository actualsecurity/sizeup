// Highway layers on the CAD maps (GitHub #73): a Layers control on every RigMap (the event's map, the
// large map, the Overview and /monitor), the camera viewer, the event's "Cameras nearby" row, and the
// hook the "@" place search uses for cameras.
//
//   Layers        Cameras, Incidents & closures, Roadwork, Message signs (the NYS Thruway Authority's public
//                 feeds and 511NY's, through the hub: GET /api/cad/roads, sizeup/roads.py; each item says its
//                 source), Mileposts, Exits (the gazetteer's, #67). Each one on / off per viewer (localStorage
//                 "sizeup.mapLayers"); cameras and incidents on at first. Signs from zoom 11, exits and mileposts from 12
//                 (the tenths of a mile from 14). The box says which sources are on and when each last updated
//                 ("Data: New York State Thruway Authority", "511NY, updated hh:mm"); no source at all: the
//                 four are greyed with the reason; the rest still work.
//   A camera      opens the viewer (a dialog: fits a 390 px phone): the snapshot through the hub
//                 (/api/cad/cameras/<id>/image: the browser never talks to 511NY or the camera), its name,
//                 road and direction, and when the image was taken; a new one every 10 s while it is open
//                 and the page is visible (paused when hidden).
//   An event      its nearest working cameras (up to 3 within 1.5 km) as thumbnails under its map.
//   Places        MapLayers.searchPlaces(q): cameras for the "@" place search ({kind: "camera", camera,
//                 label, sub, lat, lon}); MapLayers.openPlace(p) opens one (true when it was a camera).
//                 Anything with data-camera="<id>" (a camera chip in a note) opens the viewer when clicked.
//
// The sources' terms (sizeup/roads.py): every event, sign and snapshot shows when it was published or
// updated, and the map's attribution names each source that is on, with a link.
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
  ];
  const SOURCE_NOTE = { "511NY": "powered by 511NY", Thruway: "Data: New York State Thruway Authority" };
  const attributionFor = (d) => ((d && d.sources) || []).filter((s) => s.configured)      // the map's corner is small: names only
    .map((s) => `<a href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.short === "Thruway" ? "NYS Thruway Authority" : s.label)}</a>`).join(" · ");

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const esc = (s) => String(s == null ? "" : s);

  // ---- per-viewer choices (localStorage may be missing or refuse: then just this page view) --------------
  const prefs = (() => {
    const p = Object.fromEntries(LAYERS.map((l) => [l.key, l.on]));
    try {
      const saved = JSON.parse(localStorage.getItem(PREF_KEY) || "null");
      if (saved && typeof saved === "object") for (const l of LAYERS) if (typeof saved[l.key] === "boolean") p[l.key] = saved[l.key];
    } catch (_) { /* defaults */ }
    return p;
  })();
  function savePrefs() {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (_) { /* not remembered */ }
  }

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
    return { "ml-cam": cam(info), "ml-cam-off": cam(dim), "ml-inc": tri(err), "ml-closed": closed, "ml-work": work,
             "ml-sign": sign(true), "ml-sign-blank": sign(false), "ml-mp": tag(ok), "ml-exit": tag("#1F7A45") };
  }

  // ---- GeoJSON from the data ------------------------------------------------------------------------------
  const pt = (o, props) => ({ type: "Feature", properties: props, geometry: { type: "Point", coordinates: [o.lon, o.lat] } });
  const fc = (features) => ({ type: "FeatureCollection", features });
  function features(d, key) {
    if (!d) return fc([]);
    if (key === "cameras") return fc((d.cameras || []).map((c) => pt(c, { id: c.id, icon: c.off ? "ml-cam-off" : "ml-cam", off: !!c.off })));
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
  const available = (d, l) => !!d && (l.feed ? !!d.configured : !!d.highway_places);

  // ---- one map's layers and its Layers control --------------------------------------------------------------
  const maps = new Set();

  class MapRoads {
    constructor(map, host, ctrls, opts = {}) {
      this.map = map;
      map.on("zoomend", () => { if (!this.panel.hidden) this.renderPanel(); });   // the "zoom in" hints follow the zoom
      this.host = host;
      this.opts = opts;
      this.ready = false;
      this.sigs = {};
      this.btn = h("button", "cm-btn sm");
      this.btn.type = "button";
      if (window.Icon) this.btn.append(Icon("layers"));
      this.btn.append("Layers");
      this.btn.title = "Map layers: cameras, incidents, roadwork, signs, mileposts, exits";
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
      subscribe((d) => this.update(d));
    }

    init() {
      const map = this.map, set = icons();
      for (const [name, img] of Object.entries(set)) if (!map.hasImage(name)) map.addImage(name, img, { pixelRatio: 2 });
      const before = map.getLayer("firehouse") ? "firehouse" : undefined;
      const glyphs = !!(map.getStyle() && map.getStyle().glyphs);
      for (const l of LAYERS) {
        const src = `ml-${l.key}`;
        if (map.getSource(src)) continue;
        const attr = l.key === "cameras" ? attributionFor(store.data) : "";
        map.addSource(src, { type: "geojson", data: fc([]), ...(attr ? { attribution: attr } : {}) });
        const vis = prefs[l.key] ? "visible" : "none";
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

    applyVisibility() {
      if (!this.ready) return;
      for (const l of LAYERS) {
        for (const id of [`ml-${l.key}`, `ml-${l.key}-tenths`]) {
          if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", prefs[l.key] ? "visible" : "none");
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
      const head = h("div", "ml-head");
      head.append(h("b", "", "Layers"));
      const x = h("button", "icon quiet sm");
      x.type = "button";
      x.setAttribute("aria-label", "Close");
      x.append(window.Icon ? Icon("x") : "✕");
      x.addEventListener("click", () => { this.close(); this.btn.focus(); });
      head.append(h("span", "spacer"), x);
      const list = h("div", "ml-list");
      for (const l of LAYERS) {
        const ok = available(d, l);
        const lab = h("label", "check");
        const cb = h("input");
        cb.type = "checkbox";
        cb.checked = !!prefs[l.key];
        cb.disabled = !ok;
        cb.addEventListener("change", () => setLayer(l.key, cb.checked));
        lab.append(cb, h("span", "", l.label));
        if (ok && l.minzoom > 0 && this.map && this.map.getZoom() < l.minzoom) lab.append(h("span", "dim small", "zoom in"));
        const feed = l.key === "incidents" || l.key === "roadwork" ? "events" : l.key;
        if (ok && l.feed && (d.sources || []).some((s) => s.status && s.status[feed] && s.status[feed].error)) {
          lab.append(h("span", "ml-err small", "feed error"));
        }
        list.append(lab);
      }
      this.panel.replaceChildren(head, list);
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

    showInfo(title, sub, lines, foot, cls) {
      const head = h("div", "ml-head");
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

  function setLayer(key, on) {
    prefs[key] = !!on;
    savePrefs();
    for (const m of maps) { m.applyVisibility(); if (!m.panel.hidden) m.renderPanel(); }
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
  const viewer = { ov: null, id: null, timer: null, url: null, taken: 0, busy: false, tick: null };

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
    const msg = h("div", "ml-cam-msg dim");
    frame.append(img, msg);
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
    viewer.el = { title, img, msg, where, time, foot };
    document.addEventListener("visibilitychange", () => {
      if (!viewer.id) return;
      if (document.visibilityState === "hidden") stopRefresh();
      else { refreshCamera(); startRefresh(); }
    });
  }

  function startRefresh() {
    stopRefresh();
    viewer.timer = setInterval(refreshCamera, CAM_REFRESH_MS);
    viewer.tick = setInterval(showTime, 1000);
  }
  function stopRefresh() {
    clearInterval(viewer.timer);
    clearInterval(viewer.tick);
    viewer.timer = viewer.tick = null;
    if (viewer.id) viewer.el.time.textContent = viewer.taken ? `Image ${hhmm(viewer.taken)}, paused` : "Paused";
  }
  function showTime() {
    if (!viewer.id || !viewer.taken) return;
    viewer.el.time.textContent = `Image ${when(viewer.taken)} (${ago(viewer.taken)})${viewer.stale ? ", camera not answering" : ""}`;
  }

  async function refreshCamera() {
    const id = viewer.id;
    if (!id || viewer.busy) return;
    viewer.busy = true;
    try {
      const r = await fetch(`/api/cad/cameras/${encodeURIComponent(id)}/image`, { credentials: "same-origin", cache: "no-store" });
      if (id !== viewer.id) return;
      if (!r.ok) {
        const e = await r.json().catch(() => null);
        throw new Error((e && e.error) || `${r.status} ${r.statusText}`);
      }
      const blob = await r.blob();
      if (id !== viewer.id) return;
      const url = URL.createObjectURL(blob);
      const old = viewer.url;
      viewer.url = url;
      viewer.el.img.src = url;
      viewer.el.img.hidden = false;
      if (old) setTimeout(() => URL.revokeObjectURL(old), 1000);
      viewer.taken = Number(r.headers.get("X-Image-Time")) || Date.now() / 1000;
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
    viewer.stale = false;
    const el = viewer.el;
    el.title.textContent = c ? c.name : `Camera ${id}`;
    el.where.textContent = c ? [c.road, c.dir, c.mile ? `MP ${c.mile}` : ""].filter(Boolean).join(" · ") || "" : "";
    const src = ((store.data && store.data.sources) || []).find((s) => c && s.short === c.source);
    const a = h("a", "", SOURCE_NOTE[c && c.source] || (src ? src.label : "the source"));
    a.href = src ? src.link : "#";
    a.target = "_blank";
    a.rel = "noopener";
    el.foot.replaceChildren("Snapshot through the hub, ", a, ". A new one every 10 s while this is open.");
    el.img.hidden = true;
    el.img.alt = c ? `Traffic camera: ${c.name}` : "Traffic camera";
    el.msg.hidden = false;
    el.msg.textContent = c && c.off ? `This camera is marked out of service by ${c.source || "its source"}: trying anyway…` : "Loading…";
    el.time.textContent = "";
    if (viewer.url) { URL.revokeObjectURL(viewer.url); viewer.url = null; }
    if (window.UI && UI.dialog) UI.dialog.open(viewer.ov, { onClose: stopCamera, opener });
    else viewer.ov.hidden = false;
    refreshCamera();
    if (document.visibilityState !== "hidden") startRefresh();
    return true;
  }
  function stopCamera() {
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

  // ---- "Cameras nearby" under the event's map ---------------------------------------------------------------
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

  const near = { row: null, inc: null, sig: "", timer: null, mapbox: null };
  function nearby(inc, mapbox) {
    near.inc = inc || null;
    if (mapbox) near.mapbox = mapbox;
    if (!near.mapbox) return;
    if (!near.row) {
      near.row = h("div", "ml-near");
      near.row.hidden = true;
      near.mapbox.after(near.row);
      subscribe(() => drawNear());
      near.timer = setInterval(() => {                       // new thumbnails now and then, while visible
        if (document.visibilityState !== "hidden" && !near.row.hidden) drawNear(true);
      }, THUMB_REFRESH_MS);
    }
    drawNear();
  }
  function drawNear(force) {
    const inc = near.inc, row = near.row;
    const cams = inc && inc.lat != null && inc.lon != null && !near.mapbox.hidden ? nearest(inc.lat, inc.lon) : [];
    const sig = JSON.stringify(cams.map((c) => c.id));
    if (sig === near.sig && !force) return;
    near.sig = sig;
    row.hidden = !cams.length;
    if (!cams.length) { row.replaceChildren(); return; }
    const stamp = Math.floor(Date.now() / THUMB_REFRESH_MS);
    const list = h("div", "ml-near-list");
    for (const c of cams) {
      const b = h("button", "ml-thumb");
      b.type = "button";
      b.dataset.camera = c.id;
      b.title = `${c.name}${c.road ? `, ${c.road}` : ""}${c.dir ? ` ${c.dir}` : ""} (${c.km.toFixed(1)} km)`;
      const img = h("img", "ml-thumb-img");
      img.alt = "";
      img.loading = "lazy";
      img.src = `/api/cad/cameras/${encodeURIComponent(c.id)}/image?t=${stamp}`;
      img.addEventListener("error", () => img.classList.add("ml-broken"));
      const cap = h("span", "ml-thumb-cap", c.name);
      b.append(img, cap);
      b.addEventListener("click", () => openCamera(c.id, b));
      list.append(b);
    }
    const head = h("div", "ml-near-head");
    if (window.Icon) head.append(Icon("camera"));
    head.append(h("b", "", "Cameras nearby"), h("span", "dim small", [...new Set(cams.map((c) => c.source || "511NY"))].join(" · ")));
    row.replaceChildren(head, list);
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

  return { attach, openCamera, closeCamera, nearby, nearest, searchPlaces, openPlace, prefs, setLayer, LAYERS,
           load, data: () => store.data, maps };
})();
