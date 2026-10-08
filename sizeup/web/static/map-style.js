// MapLibre style for the PCFD screens: a dark graphite, street-readable basemap (OpenMapTiles schema,
// served by OpenFreeMap) with street names and house numbers, under the firehouse
// layer. The incident marker is an HTML marker added by cadmap.js.
"use strict";

// Neutral graphite (the pages' tokens, style.css), so the department's type and status colors on top
// are the only color; water a muted blue, parks a muted green; firehouses in the department's red.
const PALETTE = {
  bg: "#15181B",
  residential: "#1A1D21",
  industrial: "#1C1E22",
  wood: "#17211C",
  grass: "#18241E",
  water: "#0F1D29",
  waterLine: "#1A3042",
  building: "#22262B",
  buildingEdge: "#2E333A",
  path: "#353B42",
  service: "#2D3238",
  minor: "#394047",
  tertiary: "#48505A",
  major: "#5C646E",
  motorway: "#76808B",
  rail: "#434950",
  roadLabel: "#BFC5CC",
  majorLabel: "#E6E8EB",
  placeLabel: "#D6DADF",
  suburbLabel: "#8A929C",
  waterLabel: "#5B85A8",
  houseNumber: "#858D97",
  poiLabel: "#8A929C",
  halo: "#15181B",
  firehouse: "#C8102E",
  firehouseCross: "#F2F3F5",
  firehouseLabel: "#D6DADF",
};

const ATTRIBUTION = '<a href="https://openfreemap.org">OpenFreeMap</a> © <a href="https://www.openmaptiles.org/">OpenMapTiles</a> ' +
  'Data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

// Road width (px) by zoom: thin lines across town, true-ish street widths at the incident.
const roadWidth = (z12, z14, z17, z19) =>
  ["interpolate", ["exponential", 1.6], ["zoom"], 12, z12, 14, z14, 17, z17, 19, z19];

function buildMapStyle(scene) {
  const font = ["Noto Sans Regular"];
  const bold = ["Noto Sans Bold"];
  const glyphs = scene.glyphs || undefined;
  const sources = {
    firehouses: { type: "geojson", data: { type: "FeatureCollection", features: [] } },
  };
  const layers = [{ id: "bg", type: "background", paint: { "background-color": PALETTE.bg } }];

  if (scene.tiles) {
    sources.base = { type: "vector", url: scene.tiles, attribution: ATTRIBUTION };
    const base = (id, type, layer, extra) => ({ id, type, source: "base", "source-layer": layer, ...extra });
    const cls = (...names) => ["in", ["get", "class"], ["literal", names]];
    const notTunnel = ["!=", ["get", "brunnel"], "tunnel"];
    const road = (id, classes, color, width, extra = {}) => base(id, "line", "transportation", {
      filter: ["all", cls(...classes), notTunnel],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": color, "line-width": width },
      ...extra,
    });

    layers.push(
      base("landuse-res", "fill", "landuse", {
        filter: cls("residential", "suburb", "neighbourhood"), paint: { "fill-color": PALETTE.residential },
      }),
      base("landuse-ind", "fill", "landuse", {
        filter: cls("industrial", "commercial", "retail", "railway"), paint: { "fill-color": PALETTE.industrial },
      }),
      base("landcover-wood", "fill", "landcover", {
        filter: cls("wood", "forest"), paint: { "fill-color": PALETTE.wood },
      }),
      base("landcover-grass", "fill", "landcover", {
        filter: cls("grass", "farmland", "wetland"), paint: { "fill-color": PALETTE.grass, "fill-opacity": 0.75 },
      }),
      base("park", "fill", "park", { paint: { "fill-color": PALETTE.grass, "fill-opacity": 0.6 } }),
      base("water", "fill", "water", { paint: { "fill-color": PALETTE.water } }),
      base("waterway", "line", "waterway", {
        paint: { "line-color": PALETTE.waterLine,
                 "line-width": ["interpolate", ["linear"], ["zoom"], 11, 0.8, 17, 4] },
      }),
      base("building", "fill", "building", {
        minzoom: 13.5,
        paint: { "fill-color": PALETTE.building,
                 "fill-outline-color": PALETTE.buildingEdge,
                 "fill-opacity": ["interpolate", ["linear"], ["zoom"], 13.5, 0.4, 15, 0.95] },
      }),
      base("tunnel", "line", "transportation", {
        filter: ["all", ["==", ["get", "brunnel"], "tunnel"], cls("motorway", "trunk", "primary", "secondary")],
        paint: { "line-color": PALETTE.major, "line-opacity": 0.4, "line-dasharray": [2, 2],
                 "line-width": roadWidth(0.8, 2, 8, 20) },
      }),
      road("road-path", ["path", "track"], PALETTE.path, roadWidth(0.3, 0.5, 1.5, 3),
           { minzoom: 15, paint: { "line-color": PALETTE.path, "line-width": roadWidth(0.3, 0.5, 1.5, 3),
                                   "line-dasharray": [2, 1.5] } }),
      road("road-service", ["service"], PALETTE.service, roadWidth(0.3, 0.8, 4, 12), { minzoom: 13 }),
      road("road-minor", ["minor"], PALETTE.minor, roadWidth(0.6, 1.6, 8, 24)),
      road("road-tertiary", ["tertiary"], PALETTE.tertiary, roadWidth(0.9, 2.2, 10, 28)),
      road("road-major", ["primary", "secondary", "trunk"], PALETTE.major, roadWidth(1.4, 3, 13, 34)),
      road("road-motorway", ["motorway"], PALETTE.motorway, roadWidth(1.8, 3.6, 15, 38)),
      base("rail", "line", "transportation", {
        filter: ["all", cls("rail", "transit"), notTunnel],
        paint: { "line-color": PALETTE.rail, "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 17, 3],
                 "line-dasharray": [3, 2] },
      }),
    );

    if (glyphs) {
      const halo = (color, width = 1.6) => ({
        "text-color": color, "text-halo-color": PALETTE.halo, "text-halo-width": width,
      });
      layers.push(
        base("water-label", "symbol", "water_name", {
          minzoom: 11,
          layout: { "text-field": ["get", "name"], "text-font": font, "text-size": 13,
                    "text-letter-spacing": 0.08, "text-max-width": 8 },
          paint: halo(PALETTE.waterLabel, 1.2),
        }),
        base("housenumber", "symbol", "housenumber", {
          minzoom: 17,
          layout: { "text-field": ["get", "housenumber"], "text-font": font, "text-size": 11 },
          paint: halo(PALETTE.houseNumber, 1.2),
        }),
        base("poi-label", "symbol", "poi", {
          minzoom: 16,
          filter: ["all", ["has", "name"], ["<=", ["coalesce", ["get", "rank"], 99], 30]],
          layout: { "text-field": ["get", "name"], "text-font": font, "text-size": 12,
                    "text-max-width": 8, "text-padding": 4 },
          paint: halo(PALETTE.poiLabel, 1.2),
        }),
        base("road-label-minor", "symbol", "transportation_name", {
          minzoom: 14,
          filter: cls("minor", "tertiary", "service"),
          layout: { "symbol-placement": "line", "text-field": ["get", "name"], "text-font": font,
                    "text-size": ["interpolate", ["linear"], ["zoom"], 14, 11, 17, 15, 19, 18],
                    "symbol-spacing": 320, "text-padding": 6 },
          paint: halo(PALETTE.roadLabel),
        }),
        base("road-label-major", "symbol", "transportation_name", {
          minzoom: 12,
          filter: cls("primary", "secondary", "trunk", "motorway"),
          layout: { "symbol-placement": "line",
                    "text-field": ["coalesce", ["get", "name"], ["get", "ref"]],
                    "text-font": bold,
                    "text-size": ["interpolate", ["linear"], ["zoom"], 12, 11, 14, 13, 17, 17, 19, 20],
                    "symbol-spacing": 360, "text-padding": 6 },
          paint: halo(PALETTE.majorLabel, 1.8),
        }),
        base("place-suburb", "symbol", "place", {
          minzoom: 12,
          filter: cls("suburb", "quarter", "neighbourhood", "hamlet"),
          layout: { "text-field": ["get", "name"], "text-font": font,
                    "text-size": ["interpolate", ["linear"], ["zoom"], 12, 11, 16, 14],
                    "text-transform": "uppercase", "text-letter-spacing": 0.1, "text-max-width": 8 },
          paint: halo(PALETTE.suburbLabel, 1.4),
        }),
        base("place-town", "symbol", "place", {
          maxzoom: 16,
          filter: cls("city", "town", "village"),
          layout: { "text-field": ["get", "name"], "text-font": bold,
                    "text-size": ["interpolate", ["linear"], ["zoom"], 9, 12, 13, 17, 15, 20],
                    "text-transform": "uppercase", "text-letter-spacing": 0.12, "text-max-width": 8 },
          paint: halo(PALETTE.placeLabel, 2),
        }),
      );
    }
  }

  // Firehouses (data from the scene, set by cadmap.js).
  layers.push({
    id: "firehouse", type: "symbol", source: "firehouses",
    layout: {
      "icon-image": "firehouse", "icon-allow-overlap": true, "icon-ignore-placement": true,
      "icon-size": ["interpolate", ["linear"], ["zoom"], 11, 0.75, 14, 1, 18, 1.25],
      ...(glyphs ? {
        "text-field": ["get", "name"], "text-font": bold,
        "text-size": ["interpolate", ["linear"], ["zoom"], 11, 11, 14, 13.5, 18, 16],
        "text-variable-anchor": ["left", "right", "top", "bottom"], "text-radial-offset": 1.25,
        "text-justify": "auto", "text-max-width": 10, "text-optional": true,
      } : {}),
    },
    paint: glyphs ? { "text-color": PALETTE.firehouseLabel, "text-halo-color": PALETTE.halo, "text-halo-width": 1.8 } : {},
  });

  return { version: 8, name: "pcfd", glyphs, sources, layers };
}

// Firehouse icon: a white Maltese cross on a dark disc ringed in the department's red (drawn once, 2x for crisp TVs).
function firehouseIcon() {
  const s = 56, c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d");
  g.translate(s / 2, s / 2);
  g.fillStyle = "rgba(12, 14, 16, 0.94)";
  g.strokeStyle = PALETTE.firehouse;
  g.lineWidth = 3;
  g.beginPath(); g.arc(0, 0, s / 2 - 3, 0, 2 * Math.PI); g.fill(); g.stroke();
  g.fillStyle = PALETTE.firehouseCross;
  for (let k = 0; k < 4; k++) {
    g.save();
    g.rotate((k * Math.PI) / 2);
    g.beginPath();
    g.moveTo(-3.5, -3); g.lineTo(-10.5, -19); g.lineTo(0, -14.5); g.lineTo(10.5, -19); g.lineTo(3.5, -3);
    g.closePath(); g.fill();
    g.restore();
  }
  g.beginPath(); g.arc(0, 0, 4.5, 0, 2 * Math.PI); g.fill();
  return g.getImageData(0, 0, s, s);
}
