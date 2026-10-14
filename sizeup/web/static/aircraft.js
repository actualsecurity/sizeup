// Aircraft near HPN on the CAD maps (GitHub #155; #88): shortfinal's PCFD-focused picture of Westchester County
// Airport's traffic, as a group in every map's Layers box (maplayers.js MapLayers.addGroup).
//
//   Layer        "Aircraft" (key "aircraft", off by default, in the viewer's or the event's set like the rest, #111;
//                on by itself for an event at the airport, like the Airport layer: maplayers.js focus): arrivals
//                (green), departures (blue), aircraft on the ground at HPN (grey) and low traffic in the area (amber:
//                LGA arrivals over Port Chester, helicopters), an emergency (red) whatever it is doing. Overflights
//                and other airports' ground traffic are left out. Each one is drawn where the feed last placed it,
//                turned to its track, with its callsign, altitude and type; an arrival's or departure's last few
//                minutes as a trail. A position older than the hub's stale_s (15 s) is drawn hollow and says how
//                old it is: nothing is moved on by guesswork (no dead reckoning). A click: what it is (the feed's
//                type, the FAA registry's make and model), what it is doing, how it was located.
//   Box          the group's note: emergencies first, the HPN movements (arriving, departing, on the ground),
//                how many are low in the area, each feed (our receiver, adsb.lol, adsb.fi) with how many it gave and
//                which is primary or resting, the credits and licence, the feed's time, our receiver's state.
//   Data         only ever from the hub: GET /api/cad/aircraft (sizeup/aircraft.py), every 5 s while a map has the
//                layer on and the page is visible (an unchanged answer is a 304); never in the state push (#146).
//                The browser never talks to adsb.lol or the FAA.
//
// Colours come from the design's tokens (style.css); nothing here is a raw colour but the halo.
"use strict";

window.AircraftLayer = (() => {
  const URL = "/api/cad/aircraft";
  const KEY = "aircraft";
  const POLL_MS = 5000;
  const SRC = "ac-aircraft", SRC_TRAIL = "ac-trails";
  const IDS = ["ac-trail", "ac-mark", "ac-label"];
  const HALO = "#15181B";
  const PHASE = {
    arriving: { token: "--ok", text: "Arriving HPN", tag: "▼" },
    departing: { token: "--info", text: "Departing HPN", tag: "▲" },
    ground: { token: "--text-2", text: "On the ground at HPN", tag: "GND" },
    low: { token: "--warn", text: "Low in the area", tag: "" },
    overflight: { token: "--dim", text: "Overflight", tag: "" },
    other_ground: { token: "--dim", text: "On the ground elsewhere", tag: "" },
  };
  // which feed each position came from (sizeup/aircraft.py combine(): our receiver first, then the freshest of the APIs)
  const SRC_NAME = { receiver: "our receiver", adsblol: "adsb.lol", adsbfi: "adsb.fi" };
  const POS = { adsb: "ADS-B (its own GPS position)", mlat: "MLAT (located by receivers' timing)", tisb: "TIS-B (a radar position rebroadcast)",
                adsr: "ADS-R (rebroadcast from the other band)", modes: "Mode S", adsc: "ADS-C", other: "other" };

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
  function hms(t) {
    if (!t) return "";
    try { return new Date(t * 1000).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: tz() }); } catch (_) { return ""; }
  }
  const fc = (features) => ({ type: "FeatureCollection", features });
  const fmtAlt = (a) => (a === "ground" ? "GND" : typeof a === "number" ? `${a.toLocaleString("en-US")} ft` : "alt ?");
  const trend = (vr) => (vr === null || vr === undefined ? "" : vr > 300 ? " ↑" : vr < -300 ? " ↓" : "");
  function eta(s) {
    if (s === null || s === undefined) return "";
    return s < 60 ? "under a minute" : `about ${Math.round(s / 60)} min`;
  }

  // ---- the data: one copy for every map on the page, fetched only while one has the layer on ----------------------
  const store = { data: null, timer: null, loading: null, subs: new Set(), at: 0, failed: false };
  const maps = new Set();
  const anyOn = () => { for (const m of maps) if (m.isOn()) return true; return false; };
  function notify() { for (const fn of store.subs) { try { fn(store.data); } catch (e) { console.error(e); } } }
  async function load() {
    if (store.loading) return store.loading;
    store.loading = (async () => {
      try {
        // the browser revalidates (If-None-Match): an unchanged answer is a 304 and the copy it has
        const r = await fetch(URL, { credentials: "same-origin" });
        if (r.ok) { store.data = await r.json(); store.at = Date.now(); store.failed = false; } else store.failed = true;
      } catch (_) { store.failed = true; /* the hub is away: keep what we have; it goes stale on screen */ }
      finally { store.loading = null; notify(); }
    })();
    return store.loading;
  }
  function kick() {                         // a map turned it on (or the page came back): now, then every 5 s
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

  // how old each position is now: its age at the feed's read, plus the time since that read
  function ageNow(d, a) {
    const since = d && d.fetched ? Math.max(0, Date.now() / 1000 - d.fetched) : 0;
    return (a.age || 0) + since;
  }
  const staleS = (d) => (d && d.stale_s) || 15;
  function colour(a) { return a.emerg ? token("--err", "#EE6475") : token((PHASE[a.phase] || PHASE.low).token, "#9AA3AD"); }
  function callsign(a) { return a.cs || (a.hex || "").replace(/^~/, "").toUpperCase(); }
  function phaseLine(a) {
    if (a.phase === "arriving") {
      const where = a.appr && a.appr !== "maneuvering" ? `▼ ${a.appr}` : a.rwy ? `▼ RWY ${a.rwy}` : "▼ inbound";
      return [where, a.dist !== null && a.dist !== undefined ? `${a.dist.toFixed(1)} nm` : ""].filter(Boolean).join(" · ");
    }
    if (a.phase === "departing") return "▲ departed HPN";
    return "";
  }

  // ---- the marks: a plane or a helicopter, drawn white for MapLibre to tint (SDF); hollow when old ------------------
  function mark(kind, hollow) {
    const S = 48, c = document.createElement("canvas");
    c.width = c.height = S;
    const g = c.getContext("2d");
    g.fillStyle = g.strokeStyle = "#fff";
    g.lineJoin = "round";
    g.lineWidth = 3;
    g.beginPath();
    if (kind === "heli") {                  // a round cabin, a tail boom, the rotor across
      g.arc(24, 22, 8, 0, Math.PI * 2);
      g.moveTo(22.5, 30); g.lineTo(22.5, 42); g.lineTo(25.5, 42); g.lineTo(25.5, 30);
      g.moveTo(9, 22); g.lineTo(39, 22);
      g.moveTo(24, 7); g.lineTo(24, 37);
    } else {                                // nose up: icon-rotate turns it to the track
      g.moveTo(24, 4); g.lineTo(27, 10); g.lineTo(27, 19); g.lineTo(43, 27); g.lineTo(43, 31); g.lineTo(27, 27);
      g.lineTo(26, 37); g.lineTo(32, 41); g.lineTo(32, 44); g.lineTo(24, 42); g.lineTo(16, 44); g.lineTo(16, 41);
      g.lineTo(22, 37); g.lineTo(21, 27); g.lineTo(5, 31); g.lineTo(5, 27); g.lineTo(21, 19); g.lineTo(21, 10); g.closePath();
    }
    if (hollow) g.stroke();
    else { g.fill(); if (kind === "heli") g.stroke(); }
    return g.getImageData(0, 0, S, S);
  }

  class AircraftMap {
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
      if (map.getSource(SRC)) { this.added = true; return; }
      const glyphs = !!(map.getStyle() && map.getStyle().glyphs);
      const ids = (map.getStyle().layers || []).map((x) => x.id);
      const before = ids.find((id) => id.startsWith("ml-") || id === "cm-lines" || id === "firehouse");
      map.addSource(SRC_TRAIL, { type: "geojson", data: fc([]) });
      map.addSource(SRC, { type: "geojson", data: fc([]),
                           attribution: '<a href="https://adsb.lol" target="_blank" rel="noopener">adsb.lol (ODbL)</a> · <a href="https://adsb.fi" target="_blank" rel="noopener">adsb.fi</a>' });
      for (const [name, kind, hollow] of [["ac-plane", "plane", false], ["ac-plane-old", "plane", true],
                                          ["ac-heli", "heli", false], ["ac-heli-old", "heli", true]]) {
        if (!map.hasImage(name)) map.addImage(name, mark(kind, hollow), { pixelRatio: 2, sdf: true });
      }
      const vis = { visibility: "none" };
      map.addLayer({ id: "ac-trail", type: "line", source: SRC_TRAIL, layout: { ...vis, "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": ["get", "color"], "line-width": 2, "line-opacity": 0.55 } }, before);
      map.addLayer({ id: "ac-mark", type: "symbol", source: SRC,
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map",
                               "icon-allow-overlap": true, "icon-ignore-placement": true, "symbol-sort-key": ["get", "rank"],
                               "icon-size": ["interpolate", ["linear"], ["zoom"], 9, 0.55, 14, 0.95] },
                     paint: { "icon-color": ["get", "color"], "icon-halo-color": HALO, "icon-halo-width": 1.5,
                              "icon-opacity": ["case", ["get", "old"], 0.75, 1] } }, before);
      if (glyphs) {
        map.addLayer({ id: "ac-label", type: "symbol", source: SRC, minzoom: 9,
                       layout: { ...vis, "text-field": ["get", "label"], "text-font": ["Noto Sans Bold"], "text-size": 11,
                                 "text-anchor": "left", "text-offset": [1.3, 0], "text-justify": "left", "text-optional": true,
                                 "symbol-sort-key": ["get", "rank"] },
                       paint: { "text-color": ["get", "color"], "text-halo-color": HALO, "text-halo-width": 1.6 } }, before);
      }
      map.on("mouseenter", "ac-mark", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "ac-mark", () => { map.getCanvas().style.cursor = ""; });
      this.added = true;
    }

    sync() {
      if (!this.isOn() && !this.added) { this.panelChanged(); return; }
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      try { this.add(); } catch (e) { console.error("aircraft layer", e); return; }
      const d = store.data, on = this.isOn();
      for (const id of IDS) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
      if (on && d) {
        const marks = [], trails = [];
        const old = staleS(d);
        for (const a of d.aircraft || []) {
          const age = ageNow(d, a), stale = age > old, col = colour(a);
          const heli = a.kind === "helicopter";
          const lines = [callsign(a), [fmtAlt(a.alt) + trend(a.vr), a.type].filter(Boolean).join("  ")];
          if (a.emerg) lines.push(a.emerg.toUpperCase());
          const ph = phaseLine(a);
          if (ph) lines.push(ph);
          if (stale) lines.push(`position ${Math.round(age)} s old`);
          const rank = (a.emerg ? 0 : 10) + ({ arriving: 1, departing: 2, ground: 3, low: 4 }[a.phase] || 5);
          marks.push({ type: "Feature", geometry: { type: "Point", coordinates: [a.lon, a.lat] },
                       properties: { hex: a.hex, icon: (heli ? "ac-heli" : "ac-plane") + (stale ? "-old" : ""), rot: heli ? 0 : (a.trk || 0),
                                     color: col, old: stale, label: lines.join("\n"), rank } });
          if (a.trail && a.trail.length > 1) {
            trails.push({ type: "Feature", geometry: { type: "LineString", coordinates: [...a.trail, [a.lon, a.lat]] }, properties: { color: col } });
          }
        }
        this.map.getSource(SRC).setData(fc(marks));
        this.map.getSource(SRC_TRAIL).setData(fc(trails));
      }
      this.panelChanged();
    }
    panelChanged() { if (!this.control.panel.hidden) this.control.renderPanel(); }

    clicked(e) {
      if (!this.isOn() || !this.added || !store.data || !this.map.getLayer("ac-mark")) return;
      const hits = this.map.queryRenderedFeatures(e.point, { layers: ["ac-mark"] });
      if (!hits.length) return;
      e.originalEvent && e.originalEvent.stopPropagation();
      const hex = hits[0].properties.hex;
      setTimeout(() => this.show(hex), 0);
    }

    show(hex) {
      const d = store.data;
      const a = ((d && d.aircraft) || []).find((x) => x.hex === hex);
      if (!a) return;
      const age = ageNow(d, a);
      const lines = [];
      if (a.emerg) lines.push(`EMERGENCY: ${a.emerg}`);
      const ph = PHASE[a.phase] || PHASE.low;
      let doing = ph.text;
      if (a.phase === "arriving") {
        doing += a.appr === "maneuvering" ? ", maneuvering close in (vectors, the pattern or a go-around)"
          : a.appr ? `, on the ${a.appr}` : a.rwy ? `, lined up on runway ${a.rwy}` : ", inbound";
        if (a.dist !== null && a.dist !== undefined) doing += ` · ${a.dist.toFixed(1)} nm to go`;
      } else if (a.dist !== null && a.dist !== undefined) doing += ` · ${a.dist.toFixed(1)} nm from the field`;
      if (age > staleS(d)) doing += ` (at its last position, ${Math.round(age)} s ago)`;
      lines.push(doing);
      if (a.eta !== null && a.eta !== undefined) lines.push(`ETA ${eta(a.eta)} at its present speed (an estimate)`);
      const vs = a.vr === null || a.vr === undefined ? "" : Math.abs(a.vr) <= 300 ? "level" : `${a.vr > 0 ? "climbing" : "descending"} ${Math.abs(a.vr).toLocaleString("en-US")} ft/min`;
      lines.push([fmtAlt(a.alt), vs, a.gs !== null && a.gs !== undefined ? `${a.gs} kt` : "", a.trk !== null && a.trk !== undefined ? `track ${a.trk}°` : ""].filter(Boolean).join(" · "));
      if (a.sq) lines.push(`Squawk ${a.sq}`);
      const reg = a.reg ? `Registration ${a.reg}${a.reg_src === "faa" ? " (FAA registry)" : ""}` : "Registration not known";
      lines.push([reg, a.airline].filter(Boolean).join(" · "));
      lines.push(`Position: ${POS[a.pos] || "as the feed gives it"}, via ${SRC_NAME[a.src] || a.src || "the feed"}${a.band ? ` (${a.band} MHz)` : ""}, `
                 + `${Math.round(age)} s old${age > staleS(d) ? " (stale)" : ""}`);
      lines.push("Arriving / departing / low is sizeup's reading of its track (shortfinal's rules), not ATC's word.");
      const what = [a.type, a.model ? `${a.model}${a.model_src === "faa" ? " (FAA registry)" : ""}` : "", a.kind].filter(Boolean);
      const foot = [d.credit ? `Data: ${d.credit}` : "", d.at ? `feed ${hms(d.at)}` : "", (d.registry && d.registry.credit) || ""].filter(Boolean).join(" · ");
      const icon = window.Icon ? Icon("aircraft") : null;
      this.control.close();
      this.control.showInfo(callsign(a), what.length ? what.join(" · ") : "Type not known", lines, foot, a.emerg ? "ac-emerg" : "", icon);
    }

    // the group in the Layers box
    panel() {
      const d = store.data;
      const box = h("div", "ml-group");
      const head = h("div", "ml-ghead");
      head.append(h("b", "", "Aircraft"));
      head.append(h("span", "spacer"));
      for (const [label, link] of [["adsb.lol", "https://adsb.lol"], ["adsb.fi", "https://adsb.fi"]]) {
        const src = h("a", "dim small", label);
        src.href = link;
        src.target = "_blank";
        src.rel = "noopener";
        head.append(src);
      }
      box.append(head);
      const lab = h("label", "check");
      const cb = h("input");
      cb.type = "checkbox";
      cb.checked = this.isOn();
      cb.dataset.key = KEY;
      cb.addEventListener("change", () => { this.control.setLayer(KEY, cb.checked); kick(); });
      const icon = window.Icon ? Icon("aircraft") : null;
      lab.append(cb, ...(icon ? [icon] : []), h("span", "", "Near HPN: arrivals, departures, low traffic"));
      box.append(lab);
      if (!this.isOn()) return box;
      if (!d) { box.append(h("div", "ml-note", store.failed ? "Aircraft unavailable here" : "Loading…")); return box; }
      if (!d.configured || d.note) { box.append(h("div", "ml-note", d.note || "Aircraft not configured on the hub")); return box; }
      const list = d.aircraft || [];
      for (const a of list.filter((x) => x.emerg)) box.append(h("div", "ml-note ac-emerg", `${callsign(a)}: ${a.emerg}`));
      const ours = list.filter((a) => ["arriving", "departing", "ground"].includes(a.phase)).slice(0, 8);
      if (ours.length) {
        const rows = h("div", "ac-list");
        for (const a of ours) {
          const row = h("button", "ac-row");
          row.type = "button";
          const tag = a.phase === "arriving" ? `▼ ${a.rwy || "IN"}` : PHASE[a.phase].tag;
          row.append(h("span", `ac-tag ${a.phase}`, tag), h("b", "", callsign(a)),
                     h("span", "dim small", [a.type, fmtAlt(a.alt), a.dist !== null && a.dist !== undefined ? `${a.dist.toFixed(1)} nm` : ""].filter(Boolean).join(" · ")));
          if (ageNow(d, a) > staleS(d)) row.append(h("span", "ac-stale small", "stale"));
          row.addEventListener("click", () => this.show(a.hex));
          rows.append(row);
        }
        box.append(rows);
      } else box.append(h("div", "ml-note", "None arriving, departing or on the ground at HPN"));
      const low = list.filter((a) => a.phase === "low").length;
      const note = h("div", "ml-note");
      note.append(`${low ? `${low} low in the area` : "Nothing low in the area"}. Hollow: position older than ${staleS(d)} s. `
                  + `Data: ${d.credit || "none yet"}${d.at ? `, feed ${hms(d.at)}` : ""}. `
                  + `${(d.registry && d.registry.credit) || ""}`);
      for (const f of (d.feeds || []).filter((x) => x.terms)) {
        const t = h("a", "", `${f.label} terms`);
        t.href = f.terms;
        t.target = "_blank";
        t.rel = "noopener";
        note.append(" ", t);
      }
      box.append(note);
      // each feed: our receiver first, then the primary and the other API; how many it gave, or why it rests
      const feeds = (d.feeds || []).map((f) => `${SRC_NAME[f.key] || f.label}${f.role === "primary" ? " (primary)" : ""}: `
        + (f.resting ? `resting (${f.resting})` : f.ok ? `${f.n} aircraft, ${f.shown} shown` : "no answer yet"));
      if (feeds.length) box.append(h("div", "ml-note", feeds.join(" · ")));
      if (d.fetched && Date.now() / 1000 - d.fetched > 30) box.append(h("div", "ml-note ac-emerg", `No new aircraft data since ${hms(d.fetched)}`));
      if (d.error) box.append(h("div", "ml-note", `(${d.error})`));
      const rx = d.receiver;
      if (rx) {
        const bands = Object.entries(rx.bands || {}).map(([b, s]) => `${b} MHz ${s.error ? `: ${s.error}` : `${s.n} aircraft`}`);
        const who = rx.held_by ? `held by ${rx.held_by.app}${rx.held_by.mode ? ` (${rx.held_by.mode})` : ""}` : "";
        box.append(h("div", "ml-note", ["Our receiver" + (rx.sdr ? ` on ${rx.name || rx.sdr}` : ""), who, ...bands, rx.note].filter(Boolean).join(" · ")));
      }
      return box;
    }
  }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new AircraftMap(control) });

  return { load, maps, data: () => store.data, KEY };
})();
