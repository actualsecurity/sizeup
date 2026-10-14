// Metro-North trains on the CAD maps (GitHub #131): the New Haven Line through Rye, Port Chester and
// Greenwich, and "Trains approaching" for an event on or near the tracks.
//
//   Layer        "Trains" in every map's Layers box (maplayers.js MapLayers.addGroup; key "trains", off by
//                default, in the viewer's or the event's set like the rest, #111): the line from Mount Vernon
//                East to Stamford (the hub's [trains] bbox), its stations, and each train on it now, drawn as a
//                train lying along the line the way it is going (#151): a cab with its windscreen at the
//                front, the body behind; its number and where it is headed beside it, on the side away from
//                the other direction's trains. One length for every train: the MTA's feed says nothing of how
//                many cars a train has (no consist, sizeup/trains.py); should it ("cars"), that many cars are
//                drawn. Filled when the MTA reports where it is, hollow when the position is our estimate
//                (between the stations it is between, by its times). The heading is the line's own there (the
//                feed has no bearing). A click: the train's line and destination, direction, next stop and time,
//                track, delay, status and whether the position is reported or estimated; a station or the
//                line: its name and the catenary note. Polled every 30 s only while a map has it on and the
//                page is visible (the hub reads the MTA only while someone asks); a poll names the line it
//                already has (?have=static_at), so the answer is only the trains (about 2 KB).
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
  const SRC_LINE = "tr-line", SRC_ST = "tr-stations", SRC_TR = "tr-trains", SRC_CARS = "tr-cars";
  const CAR_IDS = ["tr-car-case", "tr-car", "tr-cab-case", "tr-cab"];
  const IDS = ["tr-line", "tr-station", "tr-station-label", ...CAR_IDS, "tr-train", "tr-train-label"];
  const MAX_CARS = 16;
  const CLICKABLE = ["tr-train", "tr-car", "tr-cab", "tr-station", "tr-line"];
  const TRAIN_HIT = ["tr-train", "tr-car", "tr-cab"];
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
        const had = store.data;                // the line and stations we hold: the hub leaves them out when unchanged
        const have = had && had.static_at != null && had.line && had.line.length ? `?have=${encodeURIComponent(had.static_at)}` : "";
        const r = await fetch(URL + have, { credentials: "same-origin" });
        if (r.ok) {
          const d = await r.json();
          if (d.static_same && had) { d.line = had.line; d.stations = had.stations; }
          store.data = d;
          store.at = Date.now();
        }
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

  // ---- the marks: a train, pointing up (icon-rotate turns it to its heading), filled (reported) or hollow ----------
  // (estimated). Cars, the first with the cab's windscreen: the MTA's count when it gives one, otherwise the look of
  // a typical train (LOOK_CARS), the same for every train; the box says the length isn't the MTA's (#164). Drawn at
  // pixelRatio 2, a car about to scale at street zoom.
  const CAR = 30, W = 20, X0 = 4, X1 = 16, CX = 10, TOP = 3, LOOK_CARS = 6;
  function trainIcon(filled, cars) {
    const n = Math.max(1, Math.min(MAX_CARS, (cars | 0) || LOOK_CARS));
    const H = TOP * 2 + n * CAR - 3;
    const c = document.createElement("canvas");
    c.width = W;
    c.height = H;
    const g = c.getContext("2d");
    const col = token("--warn", "#E0A030"), dark = token("--bg", HALO);
    g.lineJoin = "round";
    const body = (top, bottom, cab) => {      // one car: a rounded nose when it is the cab
      g.beginPath();
      if (cab) { g.moveTo(X0, top + 14); g.bezierCurveTo(X0, top + 5, X0 + 4, top, CX, top); g.bezierCurveTo(X1 - 4, top, X1, top + 5, X1, top + 14); }
      else { g.moveTo(X0, top + 3); g.quadraticCurveTo(X0, top, X0 + 3, top); g.lineTo(X1 - 3, top); g.quadraticCurveTo(X1, top, X1, top + 3); }
      g.lineTo(X1, bottom - 3); g.quadraticCurveTo(X1, bottom, X1 - 3, bottom);
      g.lineTo(X0 + 3, bottom); g.quadraticCurveTo(X0, bottom, X0, bottom - 3);
      g.closePath();
      g.lineWidth = 4; g.strokeStyle = HALO; g.stroke();
      if (filled) { g.fillStyle = col; g.fill(); }
      else { g.fillStyle = dark; g.fill(); g.lineWidth = 2.5; g.strokeStyle = col; g.stroke(); }
      g.fillStyle = filled ? dark : col;      // the windows
      if (cab) g.fillRect(X0 + 4, top + 8, X1 - X0 - 8, 4);
      for (let y = top + (cab ? 18 : 6); y + 4 <= bottom - 5; y += 8) g.fillRect(CX - 2, y, 4, 4);
    };
    for (let i = 0; i < n; i++) body(TOP + i * CAR, TOP + i * CAR + CAR - 3, i === 0);
    return g.getImageData(0, 0, W, H);
  }
  const iconName = (t) => `tr-${t.estimated ? "est" : "rep"}-${Math.max(0, Math.min(MAX_CARS, t.cars | 0))}`;
  function ensureIcon(map, t) {
    const name = iconName(t);
    if (!map.hasImage(name)) map.addImage(name, trainIcon(!t.estimated, t.cars), { pixelRatio: 2 });
    return name;
  }
  // To scale (#164): a car is an M8's 85 ft, so from SCALE_FROM in the train is as long on the map as on the
  // ground; zoomed further out it keeps MIN_SIZE so it can still be seen. With ["exponential", 2] on zoom the size
  // between the two stops is exactly the ground's (metres per pixel halve with each zoom), and so is anything that
  // grows with it, as the label's offset does.
  const M_PER_CAR = 25.9, LAT = 41.0, MIN_SIZE = 0.5, SCALE_TO = 22;
  const MPP0 = (40075016.686 * Math.cos((LAT * Math.PI) / 180)) / 512;          // metres per pixel at zoom 0
  const sizeAt = (z) => (M_PER_CAR * 2 ** z) / MPP0 / (CAR / 2);               // a car is CAR / 2 css px at size 1
  const SCALE_FROM = Math.log2((MIN_SIZE * (CAR / 2) * MPP0) / M_PER_CAR);      // where sizeAt(z) = MIN_SIZE (~14)
  const ICON_SIZE = ["interpolate", ["exponential", 2], ["zoom"], SCALE_FROM, MIN_SIZE, SCALE_TO, sizeAt(SCALE_TO)];
  const TEXT_PX = 11;
  const carsOf = (t) => Math.max(1, Math.min(MAX_CARS, (t.cars | 0) || LOOK_CARS));
  // The label beside the train's middle, on the side its right hand is (so the two directions' labels part): the
  // anchor on the screen axis nearest that side, just clear of a train hl css px half long and hw half wide (laid
  // along that axis), in ems.
  function labelPlace(bearing, hl, hw) {
    const b = ((Number(bearing) || 0) * Math.PI) / 180;
    const nx = Math.cos(b), ny = Math.sin(b);  // the train's right (screen x east, y down)
    const alongY = Math.abs(ny) >= Math.abs(nx);
    const ext = alongY ? hl * Math.abs(nx) + hw * Math.abs(ny) : hl * Math.abs(ny) + hw * Math.abs(nx);
    const r = (ext + 4) / TEXT_PX;
    if (alongY) return ny > 0 ? ["top", [0, r]] : ["bottom", [0, -r]];
    return nx > 0 ? ["left", [r, 0]] : ["right", [-r, 0]];
  }
  const iconHalf = (t, size) => [(size * (TOP * 2 + carsOf(t) * CAR - 3)) / 4, (size * W) / 4];   // css px
  const trackHalf = (t, z) => [(carsOf(t) * M_PER_CAR) / 2 / (MPP0 / 2 ** z), 4];

  // ---- the cars along the track (#164): each an 85 ft piece of the line behind the train's front, so the train is
  // as long on the map as on the ground and bends with the track; its width stays legible. A train the line doesn't
  // pass within NEAR_M of keeps its icon (above).
  const GAP_M = 2, NEAR_M = 100, R_EARTH = 6371008.8, RAD = Math.PI / 180;
  const track = { coords: null, xy: null, at: null, k: 1 };
  function trackOf(coords) {                    // the line in local metres and the distance along it at each point
    if (track.coords === coords) return track;
    Object.assign(track, { coords, xy: null, at: null });
    if (!coords || coords.length < 2) return track;
    track.k = Math.cos(coords[Math.floor(coords.length / 2)][1] * RAD);
    track.xy = coords.map(([lon, lat]) => [lon * RAD * R_EARTH * track.k, lat * RAD * R_EARTH]);
    track.at = [0];
    for (let i = 1; i < track.xy.length; i++) track.at.push(track.at[i - 1] + Math.hypot(track.xy[i][0] - track.xy[i - 1][0], track.xy[i][1] - track.xy[i - 1][1]));
    return track;
  }
  const toLonLat = ([x, y]) => [x / (R_EARTH * track.k * RAD), y / (R_EARTH * RAD)];
  function along(tr, s) {                       // the point s metres along the line
    const at = tr.at, xy = tr.xy;
    s = Math.max(0, Math.min(at[at.length - 1], s));
    let i = 1;
    while (i < at.length - 1 && at[i] < s) i++;
    const f = at[i] > at[i - 1] ? (s - at[i - 1]) / (at[i] - at[i - 1]) : 0;
    return [i, [xy[i - 1][0] + f * (xy[i][0] - xy[i - 1][0]), xy[i - 1][1] + f * (xy[i][1] - xy[i - 1][1])]];
  }
  function piece(tr, a, b) {                    // the line from a to b metres along it, as lon/lat
    const [i, p] = along(tr, a), [j, q] = along(tr, b);
    const out = [p];
    for (let k = i; k < j; k++) out.push(tr.xy[k]);
    out.push(q);
    return out.map(toLonLat);
  }
  // {cars: [features], mid: [lon, lat]} for a train on the line, or null: its front where the MTA (or the estimate)
  // puts it, the cars behind it the way it came
  function onTrack(t, tr) {
    if (!tr.xy) return null;
    const P = [t.lon * RAD * R_EARTH * tr.k, t.lat * RAD * R_EARTH];
    let best = null;
    for (let i = 1; i < tr.xy.length; i++) {
      const [ax, ay] = tr.xy[i - 1], [bx, by] = tr.xy[i], dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
      const f = L2 ? Math.max(0, Math.min(1, ((P[0] - ax) * dx + (P[1] - ay) * dy) / L2)) : 0;
      const d = Math.hypot(ax + f * dx - P[0], ay + f * dy - P[1]);
      if (!best || d < best.d) best = { d, s: tr.at[i - 1] + f * Math.sqrt(L2), dx, dy };
    }
    if (!best || best.d > NEAR_M) return null;
    const segB = Math.atan2(best.dx, best.dy), b = (Number(t.bearing) || 0) * RAD;
    const back = Math.cos(segB - b) >= 0 ? -1 : 1;           // going up the line: its cars lie down the line
    const n = carsOf(t), cars = [];
    for (let k = 0; k < n; k++) {
      const s1 = best.s + back * k * M_PER_CAR, s2 = best.s + back * ((k + 1) * M_PER_CAR - GAP_M);
      cars.push({ type: "Feature", geometry: { type: "LineString", coordinates: piece(tr, Math.min(s1, s2), Math.max(s1, s2)) },
                  properties: { id: t.id, estimated: !!t.estimated, cab: k === 0 } });
    }
    return { cars, mid: toLonLat(along(tr, best.s + (back * n * M_PER_CAR) / 2)[1]) };
  }

  const towards = (t) => (t.headsign ? `to ${t.headsign}` : t.dir_label || "");

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
      map.addSource(SRC_CARS, { type: "geojson", data: fc([]) });
      ensureIcon(map, { estimated: false });
      ensureIcon(map, { estimated: true });
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
      // a train on the line: its cars along the track, to scale; filled when the MTA reported it, an outline when
      // it is our estimate; the front car's ends rounded (the cab)
      const dark = token("--bg", HALO);
      const carW = ["interpolate", ["linear"], ["zoom"], 10, 4, 15, 6, 17, 9, 19, 28];
      const caseW = ["interpolate", ["linear"], ["zoom"], 10, 7, 15, 9, 17, 12, 19, 32];
      for (const cab of [false, true]) {
        const id = cab ? "tr-cab" : "tr-car", filter = ["==", ["get", "cab"], cab];
        const layout = { ...vis, "line-cap": cab ? "round" : "butt", "line-join": "round" };
        map.addLayer({ id: `${id}-case`, type: "line", source: SRC_CARS, filter, layout,
                       paint: { "line-color": ["case", ["get", "estimated"], warn, HALO], "line-width": caseW } }, before);
        map.addLayer({ id, type: "line", source: SRC_CARS, filter, layout,
                       paint: { "line-color": ["case", ["get", "estimated"], dark, warn], "line-width": carW } }, before);
      }
      // a train off the line: an icon lying flat along its heading, to scale once zoomed in (ICON_SIZE)
      map.addLayer({ id: "tr-train", type: "symbol", source: SRC_TR, filter: ["!", ["get", "onTrack"]],
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-rotate": ["get", "bearing"],
                               "icon-rotation-alignment": "map", "icon-pitch-alignment": "map",
                               "icon-allow-overlap": true, "icon-ignore-placement": true,
                               "icon-size": ICON_SIZE } }, before);
      // its number from zoom 11; where it is headed under it from 13 (the whole stretch would be a crowd of words)
      const num = ["format", ["get", "train"], {}];
      const both = ["format", ["get", "train"], {}, "\n", {}, ["get", "towards"], { "font-scale": 0.85 }];
      map.addLayer({ id: "tr-train-label", type: "symbol", source: SRC_TR, minzoom: 11,
                     layout: { ...vis, ...label(["step", ["zoom"], num, 13, both], 11,
                                                { "text-anchor": ["get", "anchor"],                          // clear of the train as it grows
                                                  "text-offset": ["interpolate", ["exponential", 2], ["zoom"],
                                                                  SCALE_FROM, ["array", "number", 2, ["get", "off_lo"]],
                                                                  SCALE_TO, ["array", "number", 2, ["get", "off_hi"]]] }) },
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
        if (d.line !== this.line) {             // the line and stations: only when the hub sent new ones
          this.line = d.line;
          this.sig = "";                        // the cars lie along it: lay them again
          this.map.getSource(SRC_LINE).setData(fc(d.line && d.line.length > 1 ? [{ type: "Feature", geometry: { type: "LineString", coordinates: d.line }, properties: {} }] : []));
          this.map.getSource(SRC_ST).setData(fc((d.stations || []).map((s) => pt(s.lon, s.lat, { id: s.id, name: s.name }))));
        }
        const sig = JSON.stringify(d.trains || []);
        if (sig !== this.sig) {
          this.sig = sig;
          const tr = trackOf(this.line), cars = [];
          this.map.getSource(SRC_TR).setData(fc((d.trains || []).map((t) => {
            const on = onTrack(t, tr);
            if (on) cars.push(...on.cars);
            const half = on ? (z) => trackHalf(t, z) : (z) => iconHalf(t, z === SCALE_TO ? sizeAt(SCALE_TO) : MIN_SIZE);
            const [anchor, off_lo] = labelPlace(t.bearing, ...half(SCALE_FROM)), off_hi = labelPlace(t.bearing, ...half(SCALE_TO))[1];
            const [lon, lat] = on ? on.mid : [t.lon, t.lat];
            return pt(lon, lat, { id: t.id, train: t.train, towards: towards(t), bearing: t.bearing || 0, estimated: !!t.estimated,
                                  onTrack: !!on, icon: on ? "" : ensureIcon(this.map, t), anchor, off_lo, off_hi });
          })));
          this.map.getSource(SRC_CARS).setData(fc(cars));
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
      const f = hits.find((x) => TRAIN_HIT.includes(x.layer.id)) || hits.find((x) => x.layer.id === "tr-station") || hits[0];
      e.originalEvent && e.originalEvent.stopPropagation();
      setTimeout(() => this.show(f), 0);
    }

    show(f) {
      const d = store.data;
      const foot = `${d.label || "MTA"}${d.at ? `, feed ${hm(d.at, true)}` : ""}. ${d.disclaimer || "Times may not be real time"}.`;
      const icon = window.Icon ? Icon("gis-train") : null;
      this.control.close();
      if (TRAIN_HIT.includes(f.layer.id)) {
        const t = (d.trains || []).find((x) => x.id === f.properties.id);
        if (!t) return;
        const lines = [];
        lines.push(t.dir_label);
        if (t.next_stop) lines.push(`Next stop: ${t.next_stop}${t.next_time ? ` at ${hm(t.next_time)}` : ""}${t.track ? `, track ${t.track}` : ""}`);
        const said = (t.status || "").toLowerCase();         // the MTA's word, unless the delay already says it
        const status = [late(t.delay_s), said && said !== "on-time" && !(said === "late" && t.delay_s > 0) ? t.status : ""].filter(Boolean).join(" · ");
        if (status) lines.push(status);
        lines.push(t.estimated ? "Position estimated (between its stations, by its times)" : "Position reported by the MTA");
        lines.push(t.cars ? `${t.cars} car${t.cars === 1 ? "" : "s"} (from the MTA)` : `Length not given by the MTA (drawn as ${LOOK_CARS} cars for every train)`);
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
      const span = (d.stations || []).length > 1 ? ` (${d.stations[0].name} to ${d.stations[d.stations.length - 1].name})` : "";
      note.append(`${n ? `${n} train${n === 1 ? "" : "s"} on the line${span}` : `No train on the line${span} now`}. Hollow: position estimated. `
                  + ((d.trains || []).some((t) => t.cars) ? "Each drawn the way it is going, its cars as the MTA gives them. "
                     : `Each drawn the way it is going, as ${LOOK_CARS} cars for all (the MTA doesn't say how many cars). `)
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
