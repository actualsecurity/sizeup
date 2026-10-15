// Aircraft near HPN on the CAD maps (GitHub #155; #88): shortfinal's PCFD-focused picture of Westchester County
// Airport's traffic, as a group in every map's Layers box (maplayers.js MapLayers.addGroup).
//
//   Layer        "Aircraft" (key "aircraft", off by default, in the viewer's or the event's set like the rest, #111;
//                on by itself for an event at the airport, like the Airport layer: maplayers.js focus): arrivals
//                (green), departures (blue), aircraft on the ground at HPN (grey) and low traffic in the area (amber:
//                LGA arrivals over Port Chester, helicopters), an emergency (red) whatever it is doing. Overflights
//                and other airports' ground traffic are left out. Drawn as shortfinal draws them (its map page:
//                renderPlanes / renderTrails, map-style.js): a jet, light-aircraft or helicopter silhouette by the
//                feed's emitter category, turned to its track and sized by phase (EMPHASIS; an emergency largest); the callsign, then
//                the approach or departure and the altitude, speed and type, on a dark chip up and to the right;
//                and every one with a short trail of its reported fixes (the hub's last TRAIL_S, aircraft.py) up
//                to the mark, in its colour, while the layer is on. Kept beneath the apparatus, the stations and the events.
//   Motion       glide.js (#179): between fixes the mark moves on along its track and speed, round its turn
//                (motion.js's Motion.arc, from the fix's own time), for at most HORIZON_S after the fix (shortfinal's
//                MAX_EXTRAPOLATE_S). A new fix moves that estimate, not the mark: the mark is drawn on from where it
//                was, slower while it is ahead of the estimate (never backwards) and faster while it is behind,
//                drifting across onto the track, until it meets it; only a miss over 0.3 nm (or 4 s of its travel),
//                a hard turn, or a change between fresh and stale jumps. Redrawn on animation frames while
//                something moves on a visible map, never while the page is hidden: as often as the fastest mark
//                moves STEP_PX on that map at its zoom (at most one every FRAME_MS, at least one every MAX_FRAME_MS),
//                so a zoomed-out map isn't redrawn 20 times a second for marks that move a pixel a second. A
//                position older than the hub's stale_s (15 s) is not moved: it is drawn hollow where it really
//                was, fading until the hub drops it (drop_s). No age is printed (the click says stale, or estimated).
//   Trails       its reported positions joined to the mark where it is drawn, every frame (no gap), as a
//                smoothed curve, its corners rounded inside (glide.js smooth()); one source with the marks, so they
//                move together.
//   Click        what it is (the feed's type, the FAA registry's make and model), what it is doing, how it was
//                located, and whether the mark is where it was reported or an estimate between reports.
//   Box          the group's note: emergencies first, the HPN movements (arriving, departing, on the ground),
//                how many are low in the area, each feed (our receiver, adsb.lol, adsb.fi) with how many it gave and
//                which is primary or resting, the credits and licence, the feed's time, our receiver's state.
//   Data         only ever from the hub: GET /api/cad/aircraft (sizeup/aircraft.py), every 2 s while a map has the
//                layer on and the page is visible (an unchanged answer is a 304; the hub still reads each feed at its
//                own rate: the primary API every 5 s); never in the state push (#146). The browser never talks to
//                adsb.lol, adsb.fi or the FAA.
//
// Colours come from the design's tokens (style.css); nothing here is a raw colour but the halo and the chip.
"use strict";

window.AircraftLayer = (() => {
  const URL = "/api/cad/aircraft";
  const KEY = "aircraft";
  const POLL_MS = 2000;
  const FRAME_MS = 50;                      // at most 20 redraws a second (on animation frames, while something moves) ...
  const MAX_FRAME_MS = 250;                 // ... at least 4
  const STEP_PX = 0.75;                     // ... in between, one each time the fastest mark has moved this far on screen
  const HORIZON_S = 10;                     // shortfinal's MAX_EXTRAPOLATE_S: never further past a fix than this
  const SRC = "ac-aircraft";                // marks and trails in one source: one setData a frame, drawn together
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
    // a squawk 7500 / 7600 / 7700 or its own emergency status (the hub raises the alarm and pushes): never drawn
    // smaller than the busiest phase, whatever it is doing; red, first (rank 0), its emergency on the label
    emergency: { scale: 1.3, label: 1.1, trail: 1.5 },
  };
  function emphasis(a) { return a.emerg ? EMPHASIS.emergency : (EMPHASIS[a.phase] || EMPHASIS.low); }
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

  // ---- the motion: one follower for every map (glide.js, motion.js's predictor), fed each real fix ------------------
  const glide = window.Glide && window.Motion ? Glide.follower(Motion.arc, { horizonS: HORIZON_S }) : null;
  let looks = null;                         // what each aircraft looks like this answer (look())
  function moved() {
    const d = store.data;
    if (!glide || !d) return;
    glide.staleS = staleS(d);
    const keep = new Set();
    for (const a of d.aircraft || []) {
      keep.add(a.hex);
      // the fix as it was when it was true (its age at the hub's read, plus the time since): the estimate runs from there
      glide.set(a.hex, { lon: a.lon, lat: a.lat, rot: a.trk || 0, at: Date.now() - ageNow(d, a) * 1000,
                         gs: a.gs, trk: a.trk, tr: a.tr, ground: a.alt === "ground" });
    }
    glide.forget(keep);
    looks = null;
  }
  // a frame: each map on screen redrawn once its fastest mark has moved STEP_PX there, only while something moves on
  // a visible map
  let raf = 0;
  const wanted = () => !!glide && anyOn() && document.visibilityState !== "hidden" && glide.busy();
  function animate() {
    if (wanted() && !raf && window.requestAnimationFrame) raf = requestAnimationFrame(frame);
  }
  function frame(ts) {
    raf = 0;
    if (!wanted()) return;
    for (const m of maps) {
      if (ts - (m.drawnAt || 0) >= m.frameMs() - 2) {
        m.drawnAt = ts;
        m.draw();
      }
    }
    raf = requestAnimationFrame(frame);
  }

  // what each aircraft looks like, worked out once an answer (colours read from the tokens), not every frame; and the
  // fastest of them (m/s, as it may be drawn while catching up)
  let fastest = 0;
  function look(d) {
    if (looks) return looks;
    looks = new Map();
    fastest = Math.max(0, ...(d.aircraft || []).filter((a) => a.alt !== "ground").map((a) => (a.gs || 0) * 1852 / 3600)) * 1.5;
    for (const a of d.aircraft || []) {
      const em = emphasis(a);
      const [kind, size] = iconFor(a);
      const basics = [fmtAlt(a.alt) + trend(a.vr), a.gs !== null && a.gs !== undefined ? `${a.gs} kt` : "", a.type].filter(Boolean).join("  ");
      const reported = [...(a.trail || [])];
      const last = reported[reported.length - 1];
      if (!last || last[0] !== a.lon || last[1] !== a.lat) reported.push([a.lon, a.lat]);   // its latest fix
      looks.set(a.hex, { kind, scale: size * em.scale, color: colour(a), cs: callsign(a), lscale: em.label, reported,
                         detail: [a.emerg ? a.emerg.toUpperCase() : "", phaseLine(a), basics].filter(Boolean).join("\n"),
                         rank: (a.emerg ? 0 : 10) + ({ arriving: 1, departing: 2, ground: 3, low: 4 }[a.phase] || 5),
                         width: 2 * Math.min(em.trail, 1.2), trailOpacity: Math.min(0.8, 0.45 * em.trail) });
    }
    return looks;
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
      map.addSource(SRC, { type: "geojson", data: fc([]),
                           attribution: '<a href="https://adsb.lol" target="_blank" rel="noopener">adsb.lol (ODbL)</a> · <a href="https://adsb.fi" target="_blank" rel="noopener">adsb.fi</a>' });
      for (const kind of ["jet", "light", "heli"]) {
        for (const hollow of [false, true]) {
          const name = `ac-${kind}${hollow ? "-old" : ""}`;
          if (!map.hasImage(name)) map.addImage(name, mark(kind, hollow), { pixelRatio: 2, sdf: true });
        }
      }
      if (!map.hasImage("ac-chip")) map.addImage("ac-chip", chip(), { pixelRatio: 2, stretchX: [[8, 24]], stretchY: [[8, 24]], content: [8, 8, 24, 24] });
      const vis = { visibility: "none" };
      const lines = ["==", ["geometry-type"], "LineString"], points = ["==", ["geometry-type"], "Point"];
      // shortfinal's "trails" layer: each in its colour, width and opacity by phase (renderTrails), up to the mark
      map.addLayer({ id: "ac-trail", type: "line", source: SRC, filter: lines, layout: { ...vis, "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": ["get", "color"], "line-width": ["get", "width"], "line-opacity": ["get", "opacity"] } }, before);
      map.addLayer({ id: "ac-mark", type: "symbol", source: SRC, filter: points,
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map",
                               "icon-allow-overlap": true, "icon-ignore-placement": true, "symbol-sort-key": ["get", "rank"],
                               "icon-size": ["interpolate", ["linear"], ["zoom"], 9, ["*", 0.5, ["get", "scale"]],
                                             12, ["*", 0.7, ["get", "scale"]], 15, ["*", 0.95, ["get", "scale"]]] },
                     paint: { "icon-color": ["get", "color"], "icon-halo-color": HALO, "icon-halo-width": 1,
                              "icon-opacity": ["get", "opacity"] } }, before);
      if (glyphs) {
        // shortfinal's "aircraft-label": the callsign, the rest smaller beneath it, on a chip up and to the right
        map.addLayer({ id: "ac-label", type: "symbol", source: SRC, filter: points, minzoom: 9,
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
      if (on && d) this.draw(true);
      this.panelChanged();
    }
    // each mark where glide.js draws it now, and its trail from its reported fixes up to it, as a curve: every frame
    // while something moves, and on each answer
    draw(force) {
      const d = store.data;
      if (!d || !this.added || !this.isOn()) return;
      if (!force && !this.map.getContainer().offsetParent) return;         // a map not on screen (the Expand closed)
      const trails = [], marks = [], now = Date.now(), lk = look(d);
      for (const a of d.aircraft || []) {
        const l = lk.get(a.hex);
        if (!l) continue;
        const age = ageNow(d, a), at = glide ? glide.where(a.hex, now) : null;
        const stale = glide ? glide.stale(a.hex) : age > staleS(d);
        const fade = alpha(d, age);
        const mark = at || { lon: a.lon, lat: a.lat, rot: a.trk || 0 };
        const pts = window.Glide ? Glide.smooth(Glide.line(l.reported, mark)) : l.reported;
        if (pts.length >= 2) {
          trails.push({ type: "Feature", geometry: { type: "LineString", coordinates: pts },
                        properties: { color: l.color, width: l.width, opacity: l.trailOpacity * fade } });
        }
        marks.push({ type: "Feature", geometry: { type: "Point", coordinates: [mark.lon, mark.lat] },
                     properties: { hex: a.hex, icon: `ac-${l.kind}${stale ? "-old" : ""}`, rot: mark.rot,
                                   scale: l.scale, color: l.color, opacity: fade, cs: l.cs, detail: l.detail,
                                   lscale: l.lscale, rank: l.rank } });
      }
      const src = this.map.getSource(SRC);
      if (src) src.setData(fc(trails.concat(marks)));
    }
    // how often to redraw this map: each time the fastest mark has moved STEP_PX on it, at its zoom
    frameMs() {
      try {
        const metresPerPx = 40075016.686 * Math.cos(this.map.getCenter().lat * Math.PI / 180) / (512 * 2 ** this.map.getZoom());
        const pxPerS = fastest / metresPerPx;
        return Math.max(FRAME_MS, Math.min(MAX_FRAME_MS, pxPerS > 0 ? 1000 * STEP_PX / pxPerS : MAX_FRAME_MS));
      } catch (_) { return FRAME_MS; }
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
      // reported or estimated: the mark moves on between reports; say so (the trail is its reported fixes up to it)
      const between = glide && glide.estimated(a.hex) && age <= staleS(d);
      lines.push(doing);
      if (a.eta !== null && a.eta !== undefined) lines.push(`ETA ${eta(a.eta)} at its present speed (an estimate)`);
      const vs = a.vr === null || a.vr === undefined ? "" : Math.abs(a.vr) <= 300 ? "level" : `${a.vr > 0 ? "climbing" : "descending"} ${Math.abs(a.vr).toLocaleString("en-US")} ft/min`;
      lines.push([fmtAlt(a.alt), vs, a.gs !== null && a.gs !== undefined ? `${a.gs} kt` : "", a.trk !== null && a.trk !== undefined ? `track ${a.trk}°` : ""].filter(Boolean).join(" · "));
      if (a.sq) lines.push(`Squawk ${a.sq}`);
      const reg = a.reg ? `Registration ${a.reg}${a.reg_src === "faa" ? " (FAA registry)" : ""}` : "Registration not known";
      lines.push([reg, a.airline].filter(Boolean).join(" · "));
      lines.push(`Position: ${POS[a.pos] || "as the feed gives it"}, via ${SRC_NAME[a.src] || a.src || "the feed"}${a.band ? ` (${a.band} MHz)` : ""}`
                 + `${age > staleS(d) ? " (stale)" : ""}`);
      if (between) lines.push(`Drawn at an estimate between reports: on from its last reported position by its track and speed (at most ${HORIZON_S} s), meeting each new report without jumping back`);
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
