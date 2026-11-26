// Intel events as map pins (#937): the Intel page's map (intel-page.js) and Search's opt-in Intel events
// (search-view.js, on its own map). On Search's map (map-style.js plainMap: the same base layers, view and controls).
//
//   IntelMap.attach(map, prefix, {pick(id)})   once the map has loaded: its source, layers and glyphs; a click on a
//                                              pin calls pick(id). Returns {set(pins), select(id), center(lat, lon),
//                                              bounds()}.
//   set(pins)    [{id, lat, lon, precision, radius_m, confirmed, level, category}]: drawn only when they differ from
//                the last set (true when drawn); never a draw per frame, never a timer.
//
// A pin: the level's colour (the Intel page's level chips, its tokens), the category's glyph (KIND: fire, EMS, crash,
// hazmat, water, rescue, alarm, police, mayday); an unconfirmed place hollow, with its radius as a ring (metres, drawn
// by MapLibre from the zoom: no work here when the map moves). The selected pin ringed.
"use strict";

window.IntelMap = (() => {
  const KIND = {
    fire_report: "fire", fire_call: "fire", fd_call: "fire", working_fire: "fire", more_units: "fire", water_supply: "fire",
    mutual_aid: "fire", mayday: "mayday", evacuate: "mayday", collapse: "mayday", trapped: "rescue", rescue: "rescue",
    aided: "ems", ems_call: "ems", mci: "ems", mva_injury: "crash", crash: "crash", pursuit: "crash", hazmat: "hazmat",
    water_rescue: "water", alarm: "alarm", weapons: "police", barricade: "police", assault: "police", officer: "police",
    dispute: "police", edp: "police",
  };
  const KINDS = ["fire", "mayday", "rescue", "ems", "crash", "hazmat", "water", "alarm", "police", "other"];
  const kind = (category) => KIND[category] || "other";
  // the level chips' tokens (cad.css .il-lv-*), read once; fallbacks: style.css's values
  const TOKENS = { push: ["--accent-text", "#EE6475"], held: ["--warn", "#E0A030"], map: ["--text", "#E6E8EB"],
                   log: ["--text-2", "#C9CED4"], shadow: ["--text-2", "#C9CED4"] };
  let colors = null;
  function levelColors() {
    if (colors) return colors;
    let cs = null;
    try { cs = getComputedStyle(document.documentElement); } catch (_) { /* no page: the fallbacks */ }
    colors = {};
    for (const [lv, [tok, fb]] of Object.entries(TOKENS)) colors[lv] = ((cs && cs.getPropertyValue(tok)) || "").trim() || fb;
    return colors;
  }
  const BG = "#15181B";                                 // map-style.js PALETTE.bg: a hollow pin's inside
  const EDGE = "#0B0F14";                               // Search's pins' edge
  const LIGHT = "#F2F3F5";

  // the glyphs on clear, 32 px (pixelRatio 2), drawn once per map: light on a hollow pin, dark ("-d") on a filled one
  // (every level's colour is light enough for it)
  function glyph(k, ink) {
    const s = 32, c = document.createElement("canvas");
    c.width = c.height = s;
    const g = c.getContext("2d");
    g.fillStyle = g.strokeStyle = ink;
    g.lineWidth = 3;
    g.lineCap = g.lineJoin = "round";
    g.translate(s / 2, s / 2);
    g.beginPath();
    if (k === "fire") {                                 // a flame
      g.moveTo(0, -10); g.bezierCurveTo(7, -3, 8, 3, 6, 6); g.bezierCurveTo(4, 10, -4, 10, -6, 6);
      g.bezierCurveTo(-8, 2, -5, -2, -3, -3); g.bezierCurveTo(-3, 0, -1, 1, 0, 1); g.bezierCurveTo(1, -3, -1, -6, 0, -10);
      g.fill();
    } else if (k === "ems") {                           // a cross
      g.rect(-2.5, -8, 5, 16); g.rect(-8, -2.5, 16, 5); g.fill();
    } else if (k === "crash") {                         // a car
      g.moveTo(-8, 2); g.lineTo(-6, -4); g.lineTo(6, -4); g.lineTo(8, 2); g.lineTo(8, 5); g.lineTo(-8, 5); g.closePath(); g.fill();
      g.beginPath(); g.arc(-4.5, 6, 2.5, 0, 2 * Math.PI); g.arc(4.5, 6, 2.5, 0, 2 * Math.PI); g.fill();
    } else if (k === "hazmat") {                        // a warning triangle
      g.moveTo(0, -9); g.lineTo(9, 7); g.lineTo(-9, 7); g.closePath(); g.stroke();
      g.beginPath(); g.moveTo(0, -3); g.lineTo(0, 2); g.stroke();
    } else if (k === "water") {                         // waves
      for (const y of [-3, 4]) { g.moveTo(-8, y); g.bezierCurveTo(-5, y - 4, -3, y + 4, 0, y); g.bezierCurveTo(3, y - 4, 5, y + 4, 8, y); }
      g.stroke();
    } else if (k === "rescue") {                        // a ring buoy
      g.lineWidth = 4; g.arc(0, 0, 6.5, 0, 2 * Math.PI); g.stroke();
    } else if (k === "alarm") {                         // a bell
      g.moveTo(-7, 5); g.bezierCurveTo(-5, 2, -6, -7, 0, -7); g.bezierCurveTo(6, -7, 5, 2, 7, 5); g.closePath(); g.fill();
      g.beginPath(); g.arc(0, 7, 2, 0, 2 * Math.PI); g.fill();
    } else if (k === "police") {                        // a shield
      g.moveTo(0, -9); g.lineTo(7, -6); g.bezierCurveTo(7, 2, 4, 6, 0, 9); g.bezierCurveTo(-4, 6, -7, 2, -7, -6); g.closePath(); g.fill();
    } else if (k === "mayday") {                        // !
      g.lineWidth = 4; g.moveTo(0, -8); g.lineTo(0, 2); g.stroke();
      g.beginPath(); g.arc(0, 7, 2.2, 0, 2 * Math.PI); g.fill();
    } else {                                            // a dot
      g.arc(0, 0, 3.5, 0, 2 * Math.PI); g.fill();
    }
    return g.getImageData(0, 0, s, s);
  }

  // a pin's feature: c its level's colour, k its glyph, h hollow (unconfirmed), r its radius (m) when hollow
  function feature(p) {
    const col = levelColors();
    const h = p.confirmed ? 0 : 1;
    return { type: "Feature", geometry: { type: "Point", coordinates: [p.lon, p.lat] },
             properties: { id: p.id, c: col[p.level] || col.map, k: `im-${kind(p.category)}${h ? "" : "-d"}`, h,
                           r: h ? Math.max(0, Math.round(p.radius_m || 0)) : 0 } };
  }
  const sigOf = (pins) => JSON.stringify(pins.map((p) => [p.id, p.lat, p.lon, p.confirmed ? 1 : 0, p.radius_m || 0, p.level, p.category]));

  // metres -> px at the home latitude, by zoom (512 px tiles): MapLibre interpolates it (exponential 2), nothing here
  function ringRadius(lat) {
    const mpp0 = 78271.517 * Math.cos((lat * Math.PI) / 180);
    return ["interpolate", ["exponential", 2], ["zoom"], 0, ["/", ["get", "r"], mpp0], 22, ["/", ["*", ["get", "r"], 2 ** 22], mpp0]];
  }

  function attach(map, prefix, opts = {}) {
    const src = `${prefix}-pins`;
    for (const k of KINDS) {
      if (!map.hasImage(`im-${k}`)) map.addImage(`im-${k}`, glyph(k, LIGHT), { pixelRatio: 2 });
      if (!map.hasImage(`im-${k}-d`)) map.addImage(`im-${k}-d`, glyph(k, EDGE), { pixelRatio: 2 });
    }
    map.addSource(src, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    const home = typeof cad !== "undefined" && cad.scene && cad.scene.home;     // (cad.js's const: not on window)
    const lat = (home && home.center && home.center[1]) || 41;
    map.addLayer({ id: `${prefix}-ring`, type: "circle", source: src, filter: [">", ["get", "r"], 0], paint: {
      "circle-radius": ringRadius(lat), "circle-color": ["get", "c"], "circle-opacity": 0.08, "circle-stroke-width": 1,
      "circle-stroke-color": ["get", "c"], "circle-stroke-opacity": 0.7, "circle-pitch-alignment": "map" } });
    map.addLayer({ id: `${prefix}-pin`, type: "circle", source: src, paint: {
      "circle-radius": 9, "circle-color": ["case", ["==", ["get", "h"], 1], BG, ["get", "c"]],
      "circle-stroke-width": 2, "circle-stroke-color": ["case", ["==", ["get", "h"], 1], ["get", "c"], EDGE] } });
    map.addLayer({ id: `${prefix}-glyph`, type: "symbol", source: src, layout: {
      "icon-image": ["get", "k"], "icon-allow-overlap": true, "icon-ignore-placement": true },
      paint: { "icon-opacity": ["case", ["==", ["get", "h"], 1], 0.85, 1] } });
    map.addLayer({ id: `${prefix}-sel`, type: "circle", source: src, filter: ["==", ["get", "id"], -1], paint: {
      "circle-radius": 13, "circle-color": "rgba(0,0,0,0)", "circle-stroke-width": 2.5, "circle-stroke-color": LIGHT } });
    const pin = `${prefix}-pin`;
    map.on("click", pin, (e) => { const f = e.features && e.features[0]; if (f && opts.pick) opts.pick(Number(f.properties.id)); });
    map.on("mouseenter", pin, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", pin, () => { map.getCanvas().style.cursor = ""; });
    let sig = null, pts = [];
    return {
      set(pins) {
        const s = sigOf(pins || []);
        if (s === sig) return false;                    // the same pins: nothing drawn
        sig = s;
        pts = pins || [];
        map.getSource(src).setData({ type: "FeatureCollection", features: pts.map(feature) });
        return true;
      },
      select(id) { map.setFilter(`${prefix}-sel`, ["==", ["get", "id"], id == null ? -1 : Number(id)]); },
      center(la, lo) {
        if (la == null || lo == null) return;
        map.easeTo({ center: [lo, la], zoom: Math.max(map.getZoom(), 15), duration: 400 });
      },
      bounds() {
        if (!pts.length) return null;
        const b = new maplibregl.LngLatBounds();
        for (const p of pts) b.extend([p.lon, p.lat]);
        return b;
      },
    };
  }

  return { attach, kind, feature, sigOf, KIND, KINDS };
})();
