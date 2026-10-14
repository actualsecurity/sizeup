// Metro-North trains on the CAD maps (GitHub #131): the New Haven Line through Rye, Port Chester and
// Greenwich, and "Trains approaching" for an event on or near the tracks.
//
//   Layer        "Trains" in every map's Layers box (maplayers.js MapLayers.addGroup; key "trains", off by
//                default, in the viewer's or the event's set like the rest, #111): the line, its stations, and
//                each train on it now: an arrow the way it is going, its number, filled when the MTA reports
//                where it is, hollow when the position is our estimate (between the stations it is between,
//                by its times). A click: the train's line and destination, direction, next stop and time,
//                track, delay, status and whether the position is reported or estimated; a station or the
//                line: its name and the catenary note. Polled every 30 s only while a map has it on and the
//                page is visible (the hub reads the MTA only while someone asks).
//   Panel        TrainLayer.panel(inc, host) (cadmap.js, under the event's map): for an event within 150 m of
//                the tracks or a station, the next trains each way in the next 15 minutes at the nearest
//                station (or the event's point on the tracks): minutes away, stops or passes, delay, track;
//                times the MTA predicts, or ours (a pass between stations: "estimated"). Every 30 s while
//                the event is open and the panel on screen; hidden for any other event.
//   Data         only ever from the hub: GET /api/cad/trains and /api/cad/trains/near (sizeup/trains.py). The
//                browser never talks to the MTA. The MTA's terms: we say where the data is from, that times
//                may not be real time, and the feed's time.
"use strict";

window.TrainLayer = (() => {
  const URL = "/api/cad/trains", NEAR = "/api/cad/trains/near";
  const KEY = "trains";
  const POLL_MS = 30000, PANEL_MS = 30000;
  const SRC_LINE = "tr-line", SRC_ST = "tr-stations", SRC_TR = "tr-trains";
  const IDS = ["tr-line", "tr-station", "tr-station-label", "tr-train", "tr-train-label"];
  const CLICKABLE = ["tr-train", "tr-station", "tr-line"];
  const HALO = "#15181B";

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function token(name, fallback) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch (_) { return fallback; }
  }
  const tz = () => (window.cad && cad.scene && cad.scene.timezone) || "America/New_York";
  function hm(t, secs) {
    if (!t) return "";
    try {
      return new Date(t * 1000).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", ...(secs ? { second: "2-digit" } : {}),
                                                              hour12: false, timeZone: tz() });
    } catch (_) { return ""; }
  }
  function late(s) {
    if (s === null || s === undefined) return "";
    const m = Math.round(s / 60);
    return m >= 1 ? `${m} min late` : m <= -1 ? `${-m} min early` : "on time";
  }
  const fc = (features) => ({ type: "FeatureCollection", features });
  const pt = (lon, lat, props) => ({ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: props });

  // ---- the layer's data: one copy for every map on the page, fetched only while one has it on --------------------
  const store = { data: null, timer: null, loading: null, subs: new Set(), at: 0 };
  const maps = new Set();
  const anyOn = () => { for (const m of maps) if (m.isOn()) return true; return false; };
  function notify() { for (const fn of store.subs) { try { fn(store.data); } catch (e) { console.error(e); } } }
  async function load() {
    if (store.loading) return store.loading;
    store.loading = (async () => {
      try {
        const r = await fetch(URL, { credentials: "same-origin" });
        if (r.ok) { store.data = await r.json(); store.at = Date.now(); }
      } catch (_) { /* the hub is away: keep what we have */ }
      finally { store.loading = null; notify(); }
    })();
    return store.loading;
  }
  function kick() {                         // a map turned it on (or the page came back): fetch now, then every 30 s
    clearTimeout(store.timer);
    store.timer = null;
    if (!anyOn() || document.visibilityState === "hidden") return;
    if (Date.now() - store.at > POLL_MS / 2) load();
    store.timer = setTimeout(function tick() {
      store.timer = null;
      if (!anyOn() || document.visibilityState === "hidden") return;
      load().then(() => { if (!store.timer && anyOn()) store.timer = setTimeout(tick, POLL_MS); });
    }, POLL_MS);
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") kick(); });

  // ---- the marks: an arrow in a rounded tile, filled (reported) or hollow (estimated) -------------------------------
  function trainIcon(filled) {
    const c = document.createElement("canvas");
    c.width = c.height = 40;
    const g = c.getContext("2d");
    const col = token("--warn", "#E0A030");
    g.lineJoin = "round";
    g.beginPath();                          // the arrow (pointing up: icon-rotate turns it to the bearing)
    g.moveTo(20, 3); g.lineTo(33, 19); g.lineTo(26, 19); g.lineTo(26, 36); g.lineTo(14, 36); g.lineTo(14, 19); g.lineTo(7, 19); g.closePath();
    g.lineWidth = 4; g.strokeStyle = HALO; g.stroke();
    if (filled) { g.fillStyle = col; g.fill(); }
    else { g.fillStyle = HALO; g.fill(); g.lineWidth = 2.5; g.strokeStyle = col; g.stroke(); }
    return g.getImageData(0, 0, 40, 40);
  }

  class TrainMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.added = false;
      maps.add(this);
      store.subs.add(() => this.sync());
      this.map.on("click", (e) => this.clicked(e));
      if (this.isOn()) kick();
    }
    isOn() { return this.control.on(KEY, false); }
    keys() { return [[KEY, false]]; }
    refresh() { this.sync(); kick(); }
    ready() { try { return this.added || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }

    add() {
      const map = this.map;
      if (map.getSource(SRC_TR)) { this.added = true; return; }
      const glyphs = !!(map.getStyle() && map.getStyle().glyphs);
      const ids = (map.getStyle().layers || []).map((x) => x.id);
      const before = ids.find((id) => id.startsWith("ml-") || id === "cm-lines" || id === "firehouse");
      const line = token("--text-2", "#C9CED4"), text = token("--text", "#E6E8EB"), warn = token("--warn", "#E0A030");
      const attribution = '<a href="https://www.mta.info/developers" target="_blank" rel="noopener">MTA</a>';
      map.addSource(SRC_LINE, { type: "geojson", data: fc([]), attribution });
      map.addSource(SRC_ST, { type: "geojson", data: fc([]) });
      map.addSource(SRC_TR, { type: "geojson", data: fc([]) });
      if (!map.hasImage("tr-reported")) map.addImage("tr-reported", trainIcon(true), { pixelRatio: 2 });
      if (!map.hasImage("tr-estimated")) map.addImage("tr-estimated", trainIcon(false), { pixelRatio: 2 });
      const label = (field, size, extra) => (glyphs ? { "text-field": field, "text-font": ["Noto Sans Bold"], "text-size": size,
                                                        "text-optional": true, ...extra } : {});
      const vis = { visibility: "none" };
      map.addLayer({ id: "tr-line", type: "line", source: SRC_LINE, layout: { ...vis, "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": line, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 15, 3], "line-opacity": 0.8 } }, before);
      map.addLayer({ id: "tr-station", type: "circle", source: SRC_ST, layout: vis,
                     paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 3, 15, 6], "circle-color": line,
                              "circle-stroke-color": HALO, "circle-stroke-width": 1.5 } }, before);
      map.addLayer({ id: "tr-station-label", type: "symbol", source: SRC_ST, minzoom: 12,
                     layout: { ...vis, ...label(["get", "name"], 11, { "text-anchor": "top", "text-offset": [0, 0.8] }) },
                     paint: { "text-color": line, "text-halo-color": HALO, "text-halo-width": 1.5 } }, before);
      map.addLayer({ id: "tr-train", type: "symbol", source: SRC_TR,
                     layout: { ...vis, "icon-image": ["case", ["get", "estimated"], "tr-estimated", "tr-reported"], "icon-rotate": ["get", "bearing"],
                               "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-ignore-placement": true,
                               "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.6, 15, 1] } }, before);
      map.addLayer({ id: "tr-train-label", type: "symbol", source: SRC_TR, minzoom: 11,
                     layout: { ...vis, ...label(["get", "train"], 11, { "text-anchor": "left", "text-offset": [1.1, 0] }) },
                     paint: { "text-color": ["case", ["get", "estimated"], text, warn], "text-halo-color": HALO, "text-halo-width": 1.6 } }, before);
      for (const id of CLICKABLE) {
        map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
      }
      this.added = true;
    }

    sync() {
      if (!this.isOn() && !this.added) { this.panelChanged(); return; }
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      try { this.add(); } catch (e) { console.error("trains layer", e); return; }
      const d = store.data, on = this.isOn();
      for (const id of IDS) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
      if (on && d) {
        const sig = JSON.stringify([d.fetched, d.static_at, (d.trains || []).length]);
        if (sig !== this.sig) {
          this.sig = sig;
          this.map.getSource(SRC_LINE).setData(fc(d.line && d.line.length > 1 ? [{ type: "Feature", geometry: { type: "LineString", coordinates: d.line }, properties: {} }] : []));
          this.map.getSource(SRC_ST).setData(fc((d.stations || []).map((s) => pt(s.lon, s.lat, { id: s.id, name: s.name }))));
          this.map.getSource(SRC_TR).setData(fc((d.trains || []).map((t) => pt(t.lon, t.lat, { id: t.id, train: t.train, bearing: t.bearing || 0, estimated: !!t.estimated }))));
        }
      }
      this.panelChanged();
    }
    panelChanged() { if (!this.control.panel.hidden) this.control.renderPanel(); }

    clicked(e) {
      if (!this.isOn() || !this.added || !store.data) return;
      const layers = CLICKABLE.filter((id) => this.map.getLayer(id));
      const hits = this.map.queryRenderedFeatures(e.point, { layers });
      if (!hits.length) return;
      const f = hits.find((x) => x.layer.id === "tr-train") || hits.find((x) => x.layer.id === "tr-station") || hits[0];
      e.originalEvent && e.originalEvent.stopPropagation();
      setTimeout(() => this.show(f), 0);
    }

    show(f) {
      const d = store.data;
      const foot = `${d.label || "MTA"}${d.at ? `, feed ${hm(d.at, true)}` : ""}. ${d.disclaimer || "Times may not be real time"}.`;
      const icon = window.Icon ? Icon("gis-train") : null;
      this.control.close();
      if (f.layer.id === "tr-train") {
        const t = (d.trains || []).find((x) => x.id === f.properties.id);
        if (!t) return;
        const lines = [];
        lines.push(t.dir_label);
        if (t.next_stop) lines.push(`Next stop: ${t.next_stop}${t.next_time ? ` at ${hm(t.next_time)}` : ""}${t.track ? `, track ${t.track}` : ""}`);
        const said = (t.status || "").toLowerCase();         // the MTA's word, unless the delay already says it
        const status = [late(t.delay_s), said && said !== "on-time" && !(said === "late" && t.delay_s > 0) ? t.status : ""].filter(Boolean).join(" · ");
        if (status) lines.push(status);
        lines.push(t.estimated ? "Position estimated (between its stations, by its times)" : "Position reported by the MTA");
        lines.push(d.catenary);
        this.control.showInfo(`Train ${t.train}`, `${t.line ? `${t.line} Line` : "Metro-North"} to ${t.headsign}`, lines, foot, "", icon);
        return;
      }
      if (f.layer.id === "tr-station") {
        const s = (d.stations || []).find((x) => x.id === f.properties.id);
        this.control.showInfo(s ? `${s.name} station` : "Station", "Metro-North, New Haven Line", [d.catenary], foot, "", icon);
        return;
      }
      this.control.showInfo("New Haven Line", "Metro-North", [d.catenary], foot, "", icon);
    }

    // the group in the Layers box
    panel() {
      const d = store.data;
      const box = h("div", "ml-group");
      const head = h("div", "ml-ghead");
      head.append(h("b", "", "Trains"));
      const src = h("a", "dim small", "MTA");
      src.href = "https://www.mta.info/developers";
      src.target = "_blank";
      src.rel = "noopener";
      head.append(h("span", "spacer"), src);
      box.append(head);
      const lab = h("label", "check");
      const cb = h("input");
      cb.type = "checkbox";
      cb.checked = this.isOn();
      cb.dataset.key = KEY;
      cb.addEventListener("change", () => { this.control.setLayer(KEY, cb.checked); kick(); });
      const mark = window.Icon ? Icon("gis-train") : null;
      lab.append(cb, ...(mark ? [mark] : []), h("span", "", "Metro-North (New Haven Line)"));
      box.append(lab);
      if (!this.isOn()) return box;
      if (!d) { box.append(h("div", "ml-note", "Loading…")); return box; }
      if (!d.configured || d.note) { box.append(h("div", "ml-note", d.note || "Trains not configured on the hub")); return box; }
      const n = (d.trains || []).length;
      const note = h("div", "ml-note");
      note.append(`${n ? `${n} train${n === 1 ? "" : "s"} on the line here` : "No train on the line here now"}. Hollow: position estimated. `
                  + `Data: ${d.label || "MTA"}${d.at ? `, feed ${hm(d.at, true)}` : ""}. ${d.disclaimer || ""}. `);
      if (d.error) note.append(`(${d.error}) `);
      const terms = h("a", "", "Terms");
      terms.href = d.terms || "https://www.mta.info/developers/terms-and-conditions";
      terms.target = "_blank";
      terms.rel = "noopener";
      note.append(terms);
      box.append(note);
      return box;
    }
  }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new TrainMap(control) });

  // ---- the event's "Trains approaching" panel ---------------------------------------------------------------------
  const panels = new Map();               // host -> {inc, key, data, open, timer}
  const isOpenEvent = (inc) => !["closed", "dismissed"].includes(inc.status);
  function panel(inc, host) {
    if (!host) return;
    let st = panels.get(host);
    if (!st) {
      st = { inc: null, key: "", data: null, open: true, timer: null, seq: 0 };
      panels.set(host, st);
      st.timer = setInterval(() => {
        if (st.key && document.visibilityState !== "hidden" && !host.hidden) ask(st, host);
      }, PANEL_MS);
    }
    st.inc = inc || null;
    const ok = inc && inc.lat != null && inc.lon != null && isOpenEvent(inc);
    const key = ok ? JSON.stringify([inc.id, inc.lat, inc.lon]) : "";
    if (!ok) { st.key = ""; st.data = null; host.hidden = true; host.replaceChildren(); return; }
    if (key === st.key) return;
    st.key = key;
    st.data = null;
    host.hidden = true;
    ask(st, host);
  }
  function ask(st, host) {
    const inc = st.inc, key = st.key, seq = ++st.seq;
    if (!inc || !key) return;
    const p = new URLSearchParams({ lat: String(inc.lat), lon: String(inc.lon) });
    fetch(`${NEAR}?${p}`, { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (st.key !== key || seq !== st.seq) return;
      st.data = d;
      draw(st, host);
    }).catch(() => { /* keep the last answer; the next turn asks again */ });
  }
  function where(ref) {
    if (ref.kind === "station") return `at ${ref.name} station`;
    return `on the tracks ${ref.station_m >= 50 ? `${Math.round(ref.station_m / 10) * 10} m ${ref.side} of ${ref.name} station` : `by ${ref.name} station`}`;
  }
  function draw(st, host) {
    const d = st.data;
    if (!d || !d.near) { host.hidden = true; host.replaceChildren(); return; }
    host.hidden = false;
    const head = h("button", "ml-cams-head");
    head.type = "button";
    head.setAttribute("aria-expanded", String(st.open));
    if (window.Icon) head.append(Icon("gis-train"));
    head.append(h("b", "", "Trains approaching"), h("span", "dim small", where(d.ref)));
    head.append(h("span", "spacer"), h("span", "dim small", "MTA"), h("span", "car dim", st.open ? "▾" : "▸"));
    head.addEventListener("click", () => { st.open = !st.open; draw(st, host); });
    host.replaceChildren(head);
    if (!st.open) return;
    if (!d.configured || d.note) { host.append(h("div", "ml-note tr-note", d.note || "Trains not configured on the hub")); return; }
    const labels = d.dir_labels || { inbound: "Toward New York", outbound: "Toward New Haven" };
    const grid = h("div", "tr-dirs");
    for (const dir of ["inbound", "outbound"]) {
      const col = h("div", "tr-dir");
      col.append(h("div", "ml-cams-sub dim small", labels[dir]));
      const list = d[dir] || [];
      if (!list.length) col.append(h("div", "tr-none dim small", `No train in the next ${d.window_min || 15} min`));
      for (const t of list) {
        const row = h("div", "tr-row");
        row.append(h("span", "tr-min", t.min <= 0 ? "now" : `${t.min} min`));
        const what = h("span", "tr-what");
        what.append(h("b", "", `Train ${t.train}`), ` to ${t.headsign}`);
        const facts = [t.stops ? `stops${t.track ? `, track ${t.track}` : ""}` : "passes, no stop", `${hm(t.eta)}`, late(t.delay_s)].filter(Boolean);
        what.append(h("span", "dim small", ` · ${facts.join(" · ")}`));
        if (t.estimated) { const e = h("span", "tr-est small", "estimated"); e.title = "Our estimate from the MTA's times at the stations either side"; what.append(" ", e); }
        row.append(what);
        col.append(row);
      }
      grid.append(col);
    }
    host.append(grid);
    host.append(h("div", "ml-note tr-note", `${d.label || "MTA"}${d.at ? `, feed ${hm(d.at, true)}` : ""}. ${d.disclaimer || ""}.`
                                         + (d.error ? ` (${d.error})` : "")));
  }

  return { panel, load, maps, data: () => store.data, KEY };
})();
