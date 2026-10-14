// Aircraft near HPN on the CAD maps (GitHub #155; #88): shortfinal's PCFD-focused picture of Westchester County
// Airport's traffic, as a group in every map's Layers box (maplayers.js MapLayers.addGroup).
//
//   Layer        "Aircraft" (key "aircraft", off by default, in the viewer's or the event's set like the rest, #111;
//                on by itself for an event at the airport, like the Airport layer: maplayers.js focus): arrivals
//                (green), departures (blue), aircraft on the ground at HPN (grey) and low traffic in the area (amber:
//                LGA arrivals over Port Chester, helicopters), an emergency (red) whatever it is doing. Overflights
//                and other airports' ground traffic are left out. Drawn as shortfinal draws them (its map page:
//                renderPlanes / renderTrails, map-style.js): a jet, light-aircraft or helicopter silhouette by the
//                feed's emitter category, turned to its track and sized by phase (EMPHASIS); the callsign, then
//                the approach or departure and the altitude, speed and type, on a dark chip up and to the right;
//                and every one with a short trail of its real fixes (the hub's last TRAIL_S, aircraft.py), in its
//                colour, while the layer is on. Kept beneath the apparatus, the stations and the events.
//   Motion       motion.js: between fixes the mark moves on along its track and speed, round its turn, for at most
//                HORIZON_S after the fix (shortfinal's MAX_EXTRAPOLATE_S), redrawn every ANIMATE_MS (shortfinal's),
//                and eases onto each new fix. A position older than the hub's stale_s (15 s) is not moved: it is
//                drawn hollow where it really was, fading until the hub drops it (drop_s). No age is printed (the click says stale).
//                A click: what it is (the feed's type, the FAA registry's make and model), what it is doing, how
//                it was located.
//   Box          the group's note: emergencies first, the HPN movements (arriving, departing, on the ground),
//                how many are low in the area, the source and its licence, the feed's time, our receiver's
//                state when one is set up.
//   Data         only ever from the hub: GET /api/cad/aircraft (sizeup/aircraft.py), every 2 s while a map has the
//                layer on and the page is visible (an unchanged answer is a 304; the hub reads adsb.lol at most
//                every 5 s); never in the state push (#146). The browser never talks to adsb.lol or the FAA.
//
// Colours come from the design's tokens (style.css); nothing here is a raw colour but the halo and the chip.
"use strict";

window.AircraftLayer = (() => {
  const URL = "/api/cad/aircraft";
  const KEY = "aircraft";
  const POLL_MS = 2000;
  const ANIMATE_MS = 100;                   // shortfinal's ANIMATE_MS: ~10 redraws a second while something moves
  const HORIZON_S = 10;                     // shortfinal's MAX_EXTRAPOLATE_S: never further past a fix than this
  const EASE_MS = 1000;                     // a new fix is eased into over this, not jumped to
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
  // shortfinal's EMPHASIS (its map page): how strongly each phase is drawn (the mark's size, the label's, the trail's)
  const EMPHASIS = {
    arriving: { scale: 1.15, label: 1.0, trail: 1.3 },
    departing: { scale: 1.05, label: 1.0, trail: 1.2 },
    ground: { scale: 0.85, label: 0.9, trail: 0.8 },
    low: { scale: 1.0, label: 1.0, trail: 1.0 },
  };
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
      finally { store.loading = null; moved(); notify(); animate(); }
    })();
    return store.loading;
  }
  function kick() {                         // a map turned it on (or the page came back): now, then every 2 s
    clearTimeout(store.timer);
    store.timer = null;
    animate();
    if (!anyOn() || document.visibilityState === "hidden") return;
    if (Date.now() - store.at > POLL_MS / 2) load();
    store.timer = setTimeout(function tick() {
      store.timer = null;
      if (!anyOn() || document.visibilityState === "hidden") return;
      load().then(() => { if (!store.timer && anyOn()) store.timer = setTimeout(tick, POLL_MS); });
    }, POLL_MS);
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") kick(); else animate(); });

  // how old each position is now: its age at the feed's read, plus the time since that read
  function ageNow(d, a) {
    const since = d && d.fetched ? Math.max(0, Date.now() / 1000 - d.fetched) : 0;
    return (a.age || 0) + since;
  }
  const staleS = (d) => (d && d.stale_s) || 15;
  // shortfinal's fade (TrackStore.alpha): full until stale, then down to a quarter by the time the hub drops it
  function alpha(d, age) {
    const s = staleS(d), drop = (d && d.drop_s) || 60;
    return age <= s ? 1 : Math.max(0.25, 1 - 0.75 * (age - s) / Math.max(1, drop - s));
  }
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

  // ---- the motion: one tracker for every map (motion.js), fed each real fix ----------------------------------------
  const motion = window.Motion ? Motion.tracker(Motion.arc, { horizonS: HORIZON_S, easeMs: EASE_MS }) : null;
  function moved() {
    const d = store.data;
    if (!motion || !d) return;
    motion.staleS = staleS(d);
    const keep = new Set();
    for (const a of d.aircraft || []) {
      keep.add(a.hex);
      motion.set(a.hex, { lon: a.lon, lat: a.lat, rot: a.trk || 0, at: Date.now() - ageNow(d, a) * 1000,
                          gs: a.gs, trk: a.trk, tr: a.tr, ground: a.alt === "ground" });
    }
    motion.forget(keep);
  }
  let animTimer = null;
  function animate() {                      // redraw ~10 a second only while something moves on a visible map
    const want = !!motion && anyOn() && document.visibilityState !== "hidden" && motion.busy();
    if (want && !animTimer) {
      animTimer = setInterval(() => { for (const m of maps) m.draw(); animate(); }, ANIMATE_MS);
    } else if (!want && animTimer) {
      clearInterval(animTimer);
      animTimer = null;
    }
  }

  // ---- the marks: shortfinal's silhouettes (jetPath / lightPath / heliPath), white for MapLibre to tint
  // (SDF); hollow when old. Nose up: icon-rotate turns them to the track. ------------------------------------------
  const SHAPES = {
    jet: [[32, 3], [35, 8], [35.5, 22], [60, 36], [60, 40], [35.5, 33], [35, 48], [43, 55], [43, 58],
          [32, 55], [21, 58], [21, 55], [29, 48], [28.5, 33], [4, 40], [4, 36], [28.5, 22], [29, 8]],
    light: [[32, 6], [35, 12], [35, 24], [61, 25], [61, 31], [35, 32], [34, 48], [43, 50], [43, 55],
            [32, 54], [21, 55], [21, 50], [30, 48], [29, 32], [3, 31], [3, 25], [29, 24], [29, 12]],
  };
  function mark(kind, hollow) {
    const S = 64, c = document.createElement("canvas");
    c.width = c.height = S;
    const g = c.getContext("2d");
    g.fillStyle = g.strokeStyle = "#fff";
    g.lineJoin = "round";
    g.lineWidth = 4;
    g.beginPath();
    if (kind === "heli") {                  // a cabin, the tail boom and its rotor, the main rotor across
      g.ellipse(32, 30, 8, 12, 0, 0, Math.PI * 2);
      g.rect(30.5, 38, 3, 20);
      g.rect(25, 55, 14, 3);
    } else {
      SHAPES[kind].forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y)));
      g.closePath();
    }
    if (hollow) g.stroke();
    else g.fill();
    if (kind === "heli") {
      g.lineWidth = 3;
      g.beginPath(); g.moveTo(8, 8); g.lineTo(56, 52); g.moveTo(56, 8); g.lineTo(8, 52); g.stroke();
    }
    return g.getImageData(0, 0, S, S);
  }
  // shortfinal's label chip (chipImage), in the halo's colour: stretched behind the text by icon-text-fit
  function chip() {
    const S = 32, c = document.createElement("canvas");
    c.width = c.height = S;
    const g = c.getContext("2d");
    g.globalAlpha = 0.8;
    g.fillStyle = HALO;
    g.beginPath(); g.roundRect(1, 1, S - 2, S - 2, 8); g.fill();
    g.globalAlpha = 0.16;
    g.strokeStyle = "#fff";
    g.lineWidth = 2;
    g.stroke();
    return g.getImageData(0, 0, S, S);
  }
  // shortfinal's iconFor(): the emitter category picks the silhouette and its size
  function iconFor(a) {
    if (a.kind === "helicopter" || a.cat === "A7") return ["heli", 0.85];
    if (a.cat === "A1") return ["light", 0.7];
    if (a.cat === "A2") return ["jet", 0.8];
    if (a.cat === "A5") return ["jet", 1.3];
    return ["jet", 1.0];
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
      map.addSource(SRC, { type: "geojson", data: fc([]), attribution: '<a href="https://adsb.lol" target="_blank" rel="noopener">adsb.lol (ODbL)</a>' });
      for (const kind of ["jet", "light", "heli"]) {
        for (const hollow of [false, true]) {
          const name = `ac-${kind}${hollow ? "-old" : ""}`;
          if (!map.hasImage(name)) map.addImage(name, mark(kind, hollow), { pixelRatio: 2, sdf: true });
        }
      }
      if (!map.hasImage("ac-chip")) map.addImage("ac-chip", chip(), { pixelRatio: 2, stretchX: [[8, 24]], stretchY: [[8, 24]], content: [8, 8, 24, 24] });
      const vis = { visibility: "none" };
      // shortfinal's "trails" layer: each in its colour, width and opacity by phase (renderTrails)
      map.addLayer({ id: "ac-trail", type: "line", source: SRC_TRAIL, layout: { ...vis, "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": ["get", "color"], "line-width": ["get", "width"], "line-opacity": ["get", "opacity"] } }, before);
      map.addLayer({ id: "ac-mark", type: "symbol", source: SRC,
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map",
                               "icon-allow-overlap": true, "icon-ignore-placement": true, "symbol-sort-key": ["get", "rank"],
                               "icon-size": ["interpolate", ["linear"], ["zoom"], 9, ["*", 0.5, ["get", "scale"]],
                                             12, ["*", 0.7, ["get", "scale"]], 15, ["*", 0.95, ["get", "scale"]]] },
                     paint: { "icon-color": ["get", "color"], "icon-halo-color": HALO, "icon-halo-width": 1,
                              "icon-opacity": ["get", "opacity"] } }, before);
      if (glyphs) {
        // shortfinal's "aircraft-label": the callsign, the rest smaller beneath it, on a chip up and to the right
        map.addLayer({ id: "ac-label", type: "symbol", source: SRC, minzoom: 9,
                       layout: { ...vis,
                                 "text-field": ["format", ["get", "cs"], { "font-scale": 1.0 }, "\n", {},
                                                ["get", "detail"], { "font-scale": 0.82, "text-font": ["literal", ["Noto Sans Regular"]] }],
                                 "text-font": ["Noto Sans Bold"], "text-size": ["*", 12, ["get", "lscale"]],
                                 "text-anchor": "bottom-left", "text-justify": "left", "text-offset": [1.1, -0.6],
                                 "icon-image": "ac-chip", "icon-text-fit": "both", "icon-text-fit-padding": [4, 7, 3, 7],
                                 "text-optional": false, "symbol-sort-key": ["get", "rank"] },
                       paint: { "text-color": ["get", "color"], "text-opacity": ["get", "opacity"],
                                "icon-opacity": ["*", 0.85, ["get", "opacity"]] } }, before);
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
        const trails = [];
        for (const a of d.aircraft || []) {
          const em = EMPHASIS[a.phase] || EMPHASIS.low;
          const pts = [...(a.trail || [])];
          const last = pts[pts.length - 1];
          if (!last || last[0] !== a.lon || last[1] !== a.lat) pts.push([a.lon, a.lat]);   // its last real fix, not the drawn mark
          if (pts.length < 2) continue;
          trails.push({ type: "Feature", geometry: { type: "LineString", coordinates: pts },
                        properties: { color: colour(a), width: 2 * Math.min(em.trail, 1.2),
                                      opacity: Math.min(0.8, 0.45 * em.trail) * alpha(d, ageNow(d, a)) } });
        }
        this.map.getSource(SRC_TRAIL).setData(fc(trails));
        this.draw(true);
      }
      this.panelChanged();
    }
    // the marks where motion.js has them now: every ANIMATE_MS while something moves, and on each answer
    draw(force) {
      const d = store.data;
      if (!d || !this.added || !this.isOn()) return;
      if (!force && !this.map.getContainer().offsetParent) return;         // a map not on screen (the Expand closed)
      const marks = [], now = Date.now();
      for (const a of d.aircraft || []) {
        const age = ageNow(d, a), at = motion ? motion.where(a.hex, now) : null;
        const stale = motion ? motion.stale(a.hex) : age > staleS(d);
        const em = EMPHASIS[a.phase] || EMPHASIS.low;
        const [kind, size] = iconFor(a);
        const basics = [fmtAlt(a.alt) + trend(a.vr), a.gs !== null && a.gs !== undefined ? `${a.gs} kt` : "", a.type].filter(Boolean).join("  ");
        const detail = [a.emerg ? a.emerg.toUpperCase() : "", phaseLine(a), basics].filter(Boolean).join("\n");
        const rank = (a.emerg ? 0 : 10) + ({ arriving: 1, departing: 2, ground: 3, low: 4 }[a.phase] || 5);
        marks.push({ type: "Feature", geometry: { type: "Point", coordinates: at ? [at.lon, at.lat] : [a.lon, a.lat] },
                     properties: { hex: a.hex, icon: `ac-${kind}${stale ? "-old" : ""}`, rot: at ? at.rot : (a.trk || 0),
                                   scale: size * em.scale, color: colour(a), opacity: alpha(d, age), cs: callsign(a), detail,
                                   lscale: em.label, rank } });
      }
      const src = this.map.getSource(SRC);
      if (src) src.setData(fc(marks));
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
      if (age > staleS(d)) doing += " (at its last position: no newer one)";
      lines.push(doing);
      if (a.eta !== null && a.eta !== undefined) lines.push(`ETA ${eta(a.eta)} at its present speed (an estimate)`);
      const vs = a.vr === null || a.vr === undefined ? "" : Math.abs(a.vr) <= 300 ? "level" : `${a.vr > 0 ? "climbing" : "descending"} ${Math.abs(a.vr).toLocaleString("en-US")} ft/min`;
      lines.push([fmtAlt(a.alt), vs, a.gs !== null && a.gs !== undefined ? `${a.gs} kt` : "", a.trk !== null && a.trk !== undefined ? `track ${a.trk}°` : ""].filter(Boolean).join(" · "));
      if (a.sq) lines.push(`Squawk ${a.sq}`);
      const reg = a.reg ? `Registration ${a.reg}${a.reg_src === "faa" ? " (FAA registry)" : ""}` : "Registration not known";
      lines.push([reg, a.airline].filter(Boolean).join(" · "));
      lines.push(`Position: ${POS[a.pos] || "as the feed gives it"}${a.band ? `, ${a.band} MHz` : ""}${age > staleS(d) ? " (stale)" : ""}`);
      lines.push("Arriving / departing / low is sizeup's reading of its track (shortfinal's rules), not ATC's word.");
      const what = [a.type, a.model ? `${a.model}${a.model_src === "faa" ? " (FAA registry)" : ""}` : "", a.kind].filter(Boolean);
      const foot = [d.feed ? `Data: ${d.feed.credit}` : "", d.at ? `feed ${hms(d.at)}` : "", (d.registry && d.registry.credit) || ""].filter(Boolean).join(" · ");
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
      const src = h("a", "dim small", (d && d.feed && d.feed.label) || "adsb.lol");
      src.href = (d && d.feed && d.feed.link) || "https://adsb.lol";
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
                  + `Data: ${d.feed ? `${d.feed.credit}` : "none yet"}${d.at ? `, feed ${hms(d.at)}` : ""}. `
                  + `${(d.registry && d.registry.credit) || ""}`);
      if (d.feed && d.feed.terms) {
        const t = h("a", "", "Licence");
        t.href = d.feed.terms;
        t.target = "_blank";
        t.rel = "noopener";
        note.append(" ", t);
      }
      box.append(note);
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
