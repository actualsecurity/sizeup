// The Airport layer (GitHub #87): Westchester County Airport (HPN) on every CAD map, through the Layers box
// (maplayers.js registers it as "Airport" with its "Approaches" sub-toggle).
//
//   Airport       the airport outline at any zoom (a thin line, nothing else at low zoom); from zoom 12 the
//                 runways with their numbers at the ends, taxiways (lettered from 14), aprons; from 13 the
//                 hangars and buildings and the points of interest: the terminal, the FBOs, the tower, the
//                 Command Post / EOC staging area, the entrances. On by itself for an event at the airport
//                 (within 1.5 km of the field, or typed Airport): MapLayers calls focus(inc) from each map.
//   Approaches    off by default: the final approach paths to runways 16 and 34 (FAA CIFP) and the fixes on
//                 them, labelled (CZIMR, ISLET ...), so a dispatcher can read an aircraft's position
//                 ("inside CZIMR") against the map.
//   Data          GET /api/cad/airport (data/khpn_airport.geojson, tools/fetch_airport.py): OSM aeroway
//                 features (ODbL, © OpenStreetMap contributors), FAA CIFP and Chart Supplement (public domain).
//                 Fetched once per page; every map draws from the same copy.
//
// Colours come from the design's tokens (style.css); nothing here is a raw colour but the halo.
"use strict";

window.AirportLayer = (() => {
  const URL = "/api/cad/airport";
  const store = { data: null, loading: null, subs: new Set(), failed: false };
  const SRC = "ap-airport", SRC_APP = "ap-approaches";
  const AIRPORT_LAYERS = ["ap-outline", "ap-apron", "ap-building", "ap-taxiway", "ap-taxiway-label", "ap-runway", "ap-runway-end",
                          "ap-gate", "ap-poi", "ap-poi-label"];
  const APPROACH_LAYERS = ["ap-approach", "ap-approach-label", "ap-fix", "ap-fix-label"];

  function token(name, fallback) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch (_) { return fallback; }
  }
  const HALO = "#15181B";

  async function load() {
    if (store.data || store.loading) return store.loading;
    store.loading = (async () => {
      try {
        const r = await fetch(URL, { credentials: "same-origin" });
        if (!r.ok) throw new Error(`${r.status}`);
        const d = await r.json();
        if (d && d.features && d.airport) store.data = d;
        else store.failed = true;
      } catch (_) {
        store.failed = true;                 // an older hub: the Layers box greys the airport with the reason
      } finally {
        store.loading = null;
        for (const fn of store.subs) { try { fn(store.data); } catch (e) { console.error(e); } }
      }
    })();
    return store.loading;
  }
  function subscribe(fn) {
    store.subs.add(fn);
    if (store.data || store.failed) fn(store.data);
    else load();
  }
  const ready = () => !!store.data;
  const note = () => (store.failed ? "Airport layer unavailable here" : "");
  const credit = () => (store.data ? "Airport: OpenStreetMap (ODbL), FAA CIFP" : "");

  // ---- where the field is: an event there turns the layer on, and the map looks at the whole field ---------
  function km(a, b, c, d) {
    const p = Math.PI / 180;
    const x = Math.sin((c - a) * p / 2) ** 2 + Math.cos(a * p) * Math.cos(c * p) * Math.sin((d - b) * p / 2) ** 2;
    return 12742 * Math.asin(Math.min(1, Math.sqrt(x)));
  }
  function near(inc) {
    if (!inc) return false;
    if (inc.type === "AIRPORT") return true;
    const a = store.data && store.data.airport;
    if (!a || inc.lat == null || inc.lon == null) return false;
    return km(inc.lat, inc.lon, a.lat, a.lon) <= (a.near_km || 1.5);
  }
  function focusBounds(inc) {               // [[w, s], [e, n]] of the field for an event there, else null
    const a = store.data && store.data.airport;
    if (!a || !a.bbox || !near(inc)) return null;
    return [[a.bbox[0], a.bbox[1]], [a.bbox[2], a.bbox[3]]];
  }

  // ---- the layers on one map -------------------------------------------------------------------------------
  const fc = (features) => ({ type: "FeatureCollection", features });
  function split(d) {
    const air = [], app = [];
    for (const f of (d && d.features) || []) (f.properties.layer === "approaches" ? app : air).push(f);
    return { air: fc(air), app: fc(app) };
  }
  const is = (kind) => ["==", ["get", "kind"], kind];
  const isIn = (...kinds) => ["in", ["get", "kind"], ["literal", kinds]];

  function fixIcon() {                      // a small triangle, the chart symbol for a fix
    const c = document.createElement("canvas");
    c.width = c.height = 24;
    const g = c.getContext("2d");
    g.fillStyle = token("--info", "#6AA6E8"); g.strokeStyle = HALO; g.lineWidth = 2; g.lineJoin = "round";
    g.beginPath(); g.moveTo(12, 3); g.lineTo(21, 20); g.lineTo(3, 20); g.closePath(); g.fill(); g.stroke();
    return g.getImageData(0, 0, 24, 24);
  }

  function init(map, before, glyphs) {
    if (map.getSource(SRC)) return;
    const line = token("--text-2", "#C9CED4"), dim = token("--dim", "#9AA3AD"), info = token("--info", "#6AA6E8");
    const warn = token("--warn", "#E0A030"), text = token("--text", "#E6E8EB");
    const label = (field, size, extra) => (glyphs ? { "text-field": field, "text-font": ["Noto Sans Bold"], "text-size": size,
                                                      "text-optional": true, ...extra } : {});
    const paintText = { "text-color": text, "text-halo-color": HALO, "text-halo-width": 1.5 };
    map.addSource(SRC, { type: "geojson", data: fc([]), attribution: '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OSM</a>' });
    map.addSource(SRC_APP, { type: "geojson", data: fc([]) });
    if (!map.hasImage("ap-fix")) map.addImage("ap-fix", fixIcon(), { pixelRatio: 2 });
    const add = (spec) => { spec.layout = { visibility: "none", ...(spec.layout || {}) }; map.addLayer(spec, before); };
    // the field: its outline at every zoom, the surfaces from 12
    add({ id: "ap-outline", type: "line", source: SRC, filter: is("aerodrome"),
          paint: { "line-color": info, "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1, 14, 2], "line-opacity": 0.8,
                   "line-dasharray": [3, 2] } });
    add({ id: "ap-apron", type: "fill", source: SRC, minzoom: 12, filter: is("apron"),
          paint: { "fill-color": dim, "fill-opacity": 0.18 } });
    add({ id: "ap-building", type: "fill", source: SRC, minzoom: 13, filter: is("building"),
          paint: { "fill-color": dim, "fill-opacity": ["case", ["get", "hangar"], 0.45, 0.3], "fill-outline-color": line } });
    add({ id: "ap-taxiway", type: "line", source: SRC, minzoom: 12, filter: isIn("taxiway", "taxilane"),
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": warn, "line-opacity": ["case", is("taxiway"), 0.8, 0.45],
                   "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1.2, 16, ["case", is("taxiway"), 4, 2.5]] } });
    add({ id: "ap-taxiway-label", type: "symbol", source: SRC, minzoom: 14, filter: is("taxiway"),
          layout: { "symbol-placement": "line", "symbol-spacing": 200, ...label(["get", "label"], 11, {}) },
          paint: { "text-color": warn, "text-halo-color": HALO, "text-halo-width": 1.5 } });
    add({ id: "ap-runway", type: "line", source: SRC, minzoom: 11, filter: is("runway"),
          layout: { "line-cap": "butt" },
          paint: { "line-color": line, "line-width": ["interpolate", ["linear"], ["zoom"], 11, 3, 14, 9, 17, 26], "line-opacity": 0.9 } });
    add({ id: "ap-runway-end", type: "symbol", source: SRC, minzoom: 12, filter: is("runway_end"),
          layout: { "icon-allow-overlap": true, "text-allow-overlap": true, ...label(["get", "label"], 13, {}) },
          paint: paintText });
    add({ id: "ap-gate", type: "symbol", source: SRC, minzoom: 16, filter: is("gate"),
          layout: label(["get", "label"], 10, { "text-anchor": "top", "text-offset": [0, 0.3] }),
          paint: { "text-color": dim, "text-halo-color": HALO, "text-halo-width": 1.2 } });
    add({ id: "ap-poi", type: "circle", source: SRC, minzoom: 13, filter: is("poi"),
          paint: { "circle-radius": ["case", ["get", "staging"], 6, 4], "circle-color": ["case", ["get", "staging"], warn, info],
                   "circle-stroke-color": HALO, "circle-stroke-width": 1.5 } });
    add({ id: "ap-poi-label", type: "symbol", source: SRC, minzoom: 13, filter: is("poi"),
          layout: label(["get", "label"], 11, { "text-anchor": "left", "text-offset": [0.8, 0], "text-max-width": 14 }),
          paint: paintText });
    // the approaches: the finals and their fixes
    add({ id: "ap-approach", type: "line", source: SRC_APP, filter: is("approach"),
          paint: { "line-color": info, "line-width": 1.5, "line-opacity": 0.7, "line-dasharray": [4, 3] } });
    add({ id: "ap-approach-label", type: "symbol", source: SRC_APP, minzoom: 10, filter: is("approach"),
          layout: { "symbol-placement": "line", "symbol-spacing": 400, ...label(["get", "label"], 10, {}) },
          paint: { "text-color": info, "text-halo-color": HALO, "text-halo-width": 1.5 } });
    add({ id: "ap-fix", type: "symbol", source: SRC_APP, filter: is("fix"),
          layout: { "icon-image": "ap-fix", "icon-size": ["case", ["get", "faf"], 0.8, 0.6], "icon-allow-overlap": true } });
    add({ id: "ap-fix-label", type: "symbol", source: SRC_APP, minzoom: 9, filter: is("fix"),
          layout: label(["get", "label"], ["case", ["get", "faf"], 12, 10], { "text-anchor": "top", "text-offset": [0, 0.7] }),
          paint: { "text-color": info, "text-halo-color": HALO, "text-halo-width": 1.5 } });
    subscribe((d) => {
      if (!d || !map.getSource(SRC)) return;
      const { air, app } = split(d);
      map.getSource(SRC).setData(air);
      map.getSource(SRC_APP).setData(app);
    });
  }

  function apply(map, airportOn, approachesOn) {
    if (!map.getSource(SRC)) return;
    for (const id of AIRPORT_LAYERS) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", airportOn ? "visible" : "none");
    for (const id of APPROACH_LAYERS) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", airportOn && approachesOn ? "visible" : "none");
  }

  // An Airport event's facts (#105), the same line as sizeup/cad/airport.py summary(): "Alert 2 · Boeing 737 · unsafe gear ·
  // 12 min · RWY 16 · 140 souls (Level 5) · 12,000 lb fuel · DG: none". level(): the Westchester souls Level 1-5.
  const FIELDS = ["acft_type", "acft_nature", "acft_eta", "acft_runway", "acft_souls", "acft_level", "acft_fuel", "acft_cargo"];
  function level(souls) {
    const n = Number(souls);
    if (souls === null || souls === undefined || souls === "" || !Number.isFinite(n) || n < 1) return null;
    return n <= 2 ? 1 : n <= 10 ? 2 : n <= 25 ? 3 : n <= 50 ? 4 : 5;
  }
  function facts(inc) {               // [label, value] pairs, only what is set
    const out = [];
    if (inc.acft_type) out.push(["Aircraft", inc.acft_type]);
    if (inc.acft_nature) out.push(["Nature", inc.acft_nature]);
    if (inc.acft_eta !== null && inc.acft_eta !== undefined && inc.acft_eta !== "") out.push(["ETA", `${inc.acft_eta} min`]);
    if (inc.acft_runway) out.push(["Runway", `RWY ${inc.acft_runway}`]);
    const lv = inc.acft_souls !== null && inc.acft_souls !== undefined ? (inc.acft_level || level(inc.acft_souls)) : inc.acft_level;
    if (inc.acft_souls !== null && inc.acft_souls !== undefined && inc.acft_souls !== "") out.push(["Souls", `${inc.acft_souls} souls${lv ? ` (Level ${lv})` : ""}`]);
    else if (lv) out.push(["Level", `Level ${lv}`]);
    if (inc.acft_fuel) out.push(["Fuel", `${inc.acft_fuel} fuel`]);
    if (inc.acft_cargo) out.push(["Dangerous goods", `DG: ${inc.acft_cargo}`]);
    return out;
  }
  function summary(inc, subtypeName = "") {
    const f = facts(inc);
    if (!f.length) return "";
    const sub = inc.subtype || "";
    const head = /^ALERT-[123]$/.test(sub) ? `Alert ${sub.slice(-1)}` : subtypeName;
    return (head ? [head] : []).concat(f.map((x) => x[1])).join(" · ");
  }

  return { load, subscribe, ready, note, credit, near, focusBounds, init, apply, data: () => store.data, FIELDS, level, facts, summary };
})();
