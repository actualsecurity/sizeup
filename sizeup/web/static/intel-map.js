// Intel events as map pins (#937): the Intel page's map (intel-page.js) and Search's opt-in Intel events
// (search-view.js, on its own map). On Search's map (map-style.js plainMap: the same base layers, view and controls).
//
//   IntelMap.attach(map, prefix, {pick(id)})   once the map has loaded: its source, layers and glyphs; a click on a
//                                              pin calls pick(id). Returns {set(pins), select(id), center(lat, lon),
//                                              bounds()}.
//   set(pins, fixes)  pins: [{id, lat, lon, precision, radius_m, confirmed, level, category}]; fixes (optional):
//                [{id, lat, lon, label}], the place a note picked for an event (sizeup/intel/notes.py; the hub's map
//                read: corrected). Drawn only when they differ from the last set (true when drawn); never a draw per
//                frame, never a timer.
//
// A pin: the level's colour (the Intel page's level chips, its tokens), the category's glyph (KIND: fire, EMS, crash,
// hazmat, water, rescue, alarm, police, mayday); an unconfirmed place hollow, with its radius as a ring (metres, drawn
// by MapLibre from the zoom: no work here when the map moves). The selected pin ringed.
// Pins at the same point (a street's or a corner's one point, read for several events): a pin each, one over the
// other, the top one with a count badge (2-9, "9+"); a click there steps through them (the next one each click).
// A corrected place: a diamond in FIX (never a level's colour), a dashed line from the engine's pin when it has one;
// a click picks its event.
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
  const FIX = "#3FB86B";                                // a corrected place (style.css --ok): prealert-info.js too
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

  // the count badge (a pin with others under it), drawn once per map: 2-9, then "9+" (BADGE_MAX)
  const BADGE_MAX = 10;
  function badge(n) {
    const s = 32, c = document.createElement("canvas");
    c.width = c.height = s;
    const g = c.getContext("2d");
    g.beginPath(); g.arc(s / 2, s / 2, s / 2 - 2, 0, 2 * Math.PI);
    g.fillStyle = EDGE; g.fill();
    g.lineWidth = 2.5; g.strokeStyle = LIGHT; g.stroke();
    g.fillStyle = LIGHT; g.font = `700 ${n >= BADGE_MAX ? 15 : 19}px system-ui, sans-serif`;
    g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText(n >= BADGE_MAX ? "9+" : String(n), s / 2, s / 2 + 1);
    return g.getImageData(0, 0, s, s);
  }

  // a pin's feature: c its level's colour, k its glyph, h hollow (unconfirmed), r its radius (m) when hollow; n the
  // pins at its point, b its badge when it is the top one of them (drawn last: the last of its point in the list)
  function feature(p, n = 1, top = true) {
    const col = levelColors();
    const h = p.confirmed ? 0 : 1;
    return { type: "Feature", geometry: { type: "Point", coordinates: [p.lon, p.lat] },
             properties: { id: p.id, c: col[p.level] || col.map, k: `im-${kind(p.category)}${h ? "" : "-d"}`, h,
                           r: h ? Math.max(0, Math.round(p.radius_m || 0)) : 0, n,
                           b: n > 1 && top ? `im-n${Math.min(n, BADGE_MAX)}` : "" } };
  }
  // the pins as features, each knowing how many share its point
  function features(pins) {
    const at = new Map(), seen = new Map();
    const key = (p) => `${p.lat},${p.lon}`;
    for (const p of pins) at.set(key(p), (at.get(key(p)) || 0) + 1);
    return pins.map((p) => {
      const k = key(p), i = (seen.get(k) || 0) + 1;
      seen.set(k, i);
      return feature(p, at.get(k), i === at.get(k));
    });
  }
  const sigOf = (pins, fixes = []) => JSON.stringify([pins.map((p) => [p.id, p.lat, p.lon, p.confirmed ? 1 : 0, p.radius_m || 0, p.level, p.category]),
    fixes.map((f) => [f.id, f.lat, f.lon])]);
  // the corrected places: a diamond each, and a dashed line from the engine's pin of the same event (when it has one)
  function fixFeatures(fixes, pins) {
    const at = new Map(pins.map((p) => [p.id, p]));
    const out = [];
    for (const f of fixes) {
      out.push({ type: "Feature", geometry: { type: "Point", coordinates: [f.lon, f.lat] }, properties: { id: f.id } });
      const p = at.get(f.id);
      if (p && (p.lat !== f.lat || p.lon !== f.lon)) {
        out.push({ type: "Feature", geometry: { type: "LineString", coordinates: [[p.lon, p.lat], [f.lon, f.lat]] }, properties: { id: f.id } });
      }
    }
    return out;
  }
  function diamond() {
    const s = 32, c = document.createElement("canvas");
    c.width = c.height = s;
    const g = c.getContext("2d");
    g.translate(s / 2, s / 2);
    g.beginPath();
    g.moveTo(0, -12); g.lineTo(12, 0); g.lineTo(0, 12); g.lineTo(-12, 0); g.closePath();
    g.fillStyle = FIX; g.fill();
    g.lineWidth = 3; g.strokeStyle = LIGHT; g.stroke();
    return g.getImageData(0, 0, s, s);
  }

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
    if (!map.hasImage("im-fix")) map.addImage("im-fix", diamond(), { pixelRatio: 2 });
    for (let n = 2; n <= BADGE_MAX; n++) if (!map.hasImage(`im-n${n}`)) map.addImage(`im-n${n}`, badge(n), { pixelRatio: 2 });
    map.addSource(src, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    const fsrc = `${prefix}-fixes`;
    map.addSource(fsrc, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
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
    map.addLayer({ id: `${prefix}-n`, type: "symbol", source: src, filter: ["!=", ["get", "b"], ""], layout: {
      "icon-image": ["get", "b"], "icon-offset": [10, -10], "icon-allow-overlap": true, "icon-ignore-placement": true } });
    map.addLayer({ id: `${prefix}-fix-line`, type: "line", source: fsrc, filter: ["==", ["geometry-type"], "LineString"],
      paint: { "line-color": FIX, "line-width": 2, "line-dasharray": [2, 2], "line-opacity": 0.9 } });
    map.addLayer({ id: `${prefix}-fix`, type: "symbol", source: fsrc, filter: ["==", ["geometry-type"], "Point"], layout: {
      "icon-image": "im-fix", "icon-allow-overlap": true, "icon-ignore-placement": true } });
    const pin = `${prefix}-pin`, fix = `${prefix}-fix`;
    let selected = null;
    // the pins under the click, top first: the first, or the one after the selected one (a point's pins in turn);
    // a corrected place's diamond: its event
    for (const layer of [pin, fix]) {
      map.on("click", layer, (e) => {
        const ids = [...new Set((e.features || []).map((f) => Number(f.properties.id)))];
        if (!ids.length || !opts.pick) return;
        const i = ids.indexOf(selected);
        opts.pick(ids[i < 0 ? 0 : (i + 1) % ids.length]);
      });
      map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
    }
    let sig = null, pts = [], fxs = [];
    return {
      set(pins, fixes) {
        const s = sigOf(pins || [], fixes || []);
        if (s === sig) return false;                    // the same pins: nothing drawn
        sig = s;
        pts = pins || [];
        fxs = fixes || [];
        map.getSource(src).setData({ type: "FeatureCollection", features: features(pts) });
        map.getSource(fsrc).setData({ type: "FeatureCollection", features: fixFeatures(fxs, pts) });
        return true;
      },
      select(id) {
        selected = id == null ? null : Number(id);
        map.setFilter(`${prefix}-sel`, ["==", ["get", "id"], id == null ? -1 : Number(id)]);
      },
      center(la, lo) {
        if (la == null || lo == null) return;
        map.easeTo({ center: [lo, la], zoom: Math.max(map.getZoom(), 15), duration: 400 });
      },
      bounds() {
        if (!pts.length && !fxs.length) return null;
        const b = new maplibregl.LngLatBounds();
        for (const p of [...pts, ...fxs]) b.extend([p.lon, p.lat]);
        return b;
      },
    };
  }

  return { attach, kind, feature, features, sigOf, fixFeatures, KIND, KINDS, BADGE_MAX, FIX };
})();
