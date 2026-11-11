// Vessels (AIS) on the CAD maps (sizeup/vessels.py, docs/vessels.md): ships and boats on western Long Island Sound,
// as a group in every map's Layers box (maplayers.js MapLayers.addGroup), drawn the way the aircraft are (aircraft.js).
//
//   Layer        "Vessels" (key "vessels", off by default, in the viewer's or the event's set like the rest). On by
//                itself on a marine call's own map (a boat or water subtype, or a marine unit on it; a DPT placeholder
//                never counts); unticking it there holds for that call.
//   Marks        a hull turned to its heading (else its course), shaped and coloured by its type: ship (cargo green,
//                tanker amber, passenger blue), tug, small craft (pleasure, fishing, other), sail; law enforcement and
//                SAR in the alert red. No heading and no course: a dot. A position older than the hub's stale_s
//                (3 min) is hollow and fades until the hub drops it (drop_s, 10 min). Its name (else its MMSI) and
//                type · speed on a chip from LABEL_ZOOM. Kept beneath the aircraft, the apparatus, stations and pins.
//   Motion       motion.js's Motion.tracker with a gentle dead-reckon (drift): on along its course at its speed, for at
//                most HORIZON_S after the report and slowing past HOLD_S; a new report is eased into over EASE_MS
//                (no surge: the mark never jumps forward to catch up). Redrawn only while something moves, at most
//                every FRAME_MS, at least every MAX_FRAME_MS, never while the page is hidden.
//   Click        follows it (mapfollow.js Follower: one follower at a time, a drag or Recenter lets go) and opens its
//                box, kept live (MapFollow.LiveInfo): name, type, length, speed, course, status, destination, age.
//   Event        a marine call's "Vessels nearby" under its map (panel(inc, host), cadmap.js): the vessels its text
//                names (by name or MMSI, the hub's matching) first, then the NEAREST nearest; a click follows one.
//                The named ones are ringed on every map with the layer on while the call is selected.
//   Data         only from the hub: GET /api/cad/vessels every POLL_MS while a map has the layer on and the page is
//                visible (304 when unchanged); GET /api/cad/vessels/near?event= for the panel. The browser never
//                talks to aisstream.io or our receiver.
"use strict";

window.VesselLayer = (() => {
  const URL = "/api/cad/vessels", NEAR = "/api/cad/vessels/near";
  const KEY = "vessels";
  const POLL_MS = 5000, PANEL_MS = 10000;
  const HORIZON_S = 90, HOLD_S = 30;        // dead-reckoned at most this long after a report, slowing after HOLD_S
  const EASE_MS = 4000;                     // a new report eased into over this
  const FRAME_MS = 100, MAX_FRAME_MS = 1000, STEP_PX = 1;
  const LABEL_ZOOM = 12;
  const SRC = "vs-vessels", HL_SRC = "vs-hl";
  const IDS = ["vs-mark", "vs-label", "vs-hl-ring", "vs-hl-mark", "vs-hl-label"];
  const MARKS = ["vs-hl-mark", "vs-mark"];
  const HALO = "#15181B";
  const KT = 1852 / 3600;
  const FOLLOW_ZOOM = 13;
  const AIS_TIP = "AIS only: small craft, kayaks and paddleboards often carry none, and many recreational boats transmit only when under way.";
  // type class -> its shape and colour token
  const CLASS = {
    cargo: { shape: "ship", token: ["--ok", "#3FB86B"] },
    tanker: { shape: "ship", token: ["--warn", "#E0A030"] },
    passenger: { shape: "ship", token: ["--info", "#6AA6E8"] },
    tug: { shape: "tug", token: ["--text-2", "#C9CED4"] },
    pleasure: { shape: "boat", token: ["--text", "#E6E8EB"] },
    sailing: { shape: "sail", token: ["--text", "#E6E8EB"] },
    fishing: { shape: "boat", token: ["--text-2", "#C9CED4"] },
    law: { shape: "boat", token: ["--err", "#EE6475"] },
    sar: { shape: "boat", token: ["--err", "#EE6475"] },
    other: { shape: "boat", token: ["--dim", "#9AA3AD"] },
    unknown: { shape: "boat", token: ["--dim", "#9AA3AD"] },
  };
  const SRC_NAME = { receiver: "Our receiver", aisstream: "aisstream.io" };

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function token(name, fallback) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch (_) { return fallback; }
  }
  const fc = (features) => ({ type: "FeatureCollection", features });
  const has = (v) => v !== null && v !== undefined;
  const ago = MapFollow.ago;
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  const nameOf = (v) => v.name || `MMSI ${v.id}`;
  const course = (v) => (has(v.hdg) ? v.hdg : has(v.cog) ? v.cog : null);
  const speed = (v) => (has(v.sog) ? `${v.sog < 10 ? v.sog.toFixed(1) : Math.round(v.sog)} kn` : "");
  // the terse facts (a mark's hover, the follow chip): name · type · speed · course
  function brief(v) {
    return [nameOf(v), v.label, speed(v), has(course(v)) ? `${course(v)}°` : ""].filter(Boolean).join(" · ");
  }

  // ---- marine calls (the hub decides too: sizeup/vessels.py is_marine) ---------------------------------------------
  const MARINE_SUBTYPES = ["BOAT", "WATER", "ACFT-WATER"];
  const placeholder = (name) => /^DPT/i.test(String(name || ""));       // DPT239 ...: never located or followed
  function isMarine(inc) {
    if (!inc) return false;
    if (MARINE_SUBTYPES.includes(String(inc.subtype || "").toUpperCase())) return true;
    return (inc.assignments || []).some((a) => a && a.kind === "marine" && !placeholder(a.name) && !a.cleared_at);
  }

  // ---- the data: one copy for every map, fetched only while one has the layer on -------------------------------------
  const store = { data: null, timer: null, loading: null, at: 0, failed: false };
  const maps = new Set();
  const anyOn = () => { for (const m of maps) if (m.isOn()) return true; return false; };
  async function load() {
    if (store.loading) return store.loading;
    store.loading = (async () => {
      try {
        const r = await fetch(URL, { credentials: "same-origin" });
        if (r.ok) { store.data = await r.json(); store.at = Date.now(); store.failed = false; } else store.failed = true;
      } catch (_) { store.failed = true; }
      finally { store.loading = null; moved(); for (const m of maps) m.sync(); animate(); }
    })();
    return store.loading;
  }
  function kick() {
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

  const staleS = (d) => (d && d.stale_s) || 180;
  const dropS = (d) => (d && d.drop_s) || 600;
  // its age now: at the hub's answer, plus the time since
  const ageNow = (v) => (v.age || 0) + Math.max(0, (Date.now() - store.at) / 1000);
  function alpha(age) {
    const s = staleS(store.data), drop = dropS(store.data);
    return age <= s ? 1 : Math.max(0.3, 1 - 0.7 * (age - s) / Math.max(1, drop - s));
  }

  // ---- motion: a gentle dead-reckon on Motion.tracker (motion.js's public API; its internals are not touched) --------
  // On along its course over ground at its speed; past HOLD_S it slows (half rate) and stops at HORIZON_S. Not moving
  // (under half a knot, or no course): null, it stays at its report.
  function drift(fix, dt) {
    if (!has(fix.sog) || fix.sog < 0.5 || !has(fix.cog)) return null;
    const t = dt <= HOLD_S ? dt : HOLD_S + (dt - HOLD_S) / 2;
    const d = fix.sog * KT * t, c = fix.cog * Math.PI / 180, lat0 = fix.lat * Math.PI / 180;
    return { lon: fix.lon + (d * Math.sin(c)) / (6371008.8 * Math.cos(lat0)) / (Math.PI / 180),
             lat: fix.lat + (d * Math.cos(c)) / 6371008.8 / (Math.PI / 180), rot: fix.rot };
  }
  const tracker = window.Motion ? Motion.tracker(drift, { horizonS: HORIZON_S, staleS: 180, easeMs: EASE_MS, snapM: 1500 }) : null;
  function moved() {
    const d = store.data;
    if (!d || !tracker) return;
    tracker.staleS = staleS(d);
    const keep = new Set();
    for (const v of d.vessels || []) {
      keep.add(v.id);
      tracker.set(v.id, { lon: v.lon, lat: v.lat, rot: course(v) || 0, at: store.at - (v.age || 0) * 1000, sog: v.sog, cog: v.cog });
    }
    tracker.forget(keep);
  }
  const where = (v) => (tracker && tracker.where(v.id, Date.now())) || { lon: v.lon, lat: v.lat, rot: course(v) || 0 };
  let raf = 0;
  const wanted = () => anyOn() && document.visibilityState !== "hidden" && !!tracker && tracker.busy();
  function animate() { if (wanted() && !raf && window.requestAnimationFrame) raf = requestAnimationFrame(frame); }
  function frame(ts) {
    raf = 0;
    if (!wanted()) return;
    for (const m of maps) if (ts - (m.drawnAt || 0) >= m.frameMs() - 2) { m.drawnAt = ts; m.draw(); }
    raf = requestAnimationFrame(frame);
  }

  // ---- the marks: original, simple hulls (bow up), white for MapLibre to tint (SDF) -----------------------------------
  const SHAPES = {
    ship: [[32, 2], [45, 18], [45, 62], [19, 62], [19, 18]],
    tug: [[32, 8], [46, 22], [46, 58], [18, 58], [18, 22]],
    boat: [[32, 8], [43, 25], [42, 58], [22, 58], [21, 25]],
    sail: [[32, 4], [41, 22], [40, 60], [24, 60], [23, 22]],
  };
  function mark(kind, hollow) {
    const S = 64, c = document.createElement("canvas");
    c.width = c.height = S;
    const g = c.getContext("2d");
    g.fillStyle = g.strokeStyle = "#fff";
    g.lineJoin = "round";
    g.lineWidth = 4;
    g.beginPath();
    if (kind === "dot") g.arc(32, 32, 12, 0, Math.PI * 2);
    else { SHAPES[kind].forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y))); g.closePath(); }
    if (hollow) g.stroke(); else g.fill();
    if (kind === "sail") {                    // the sail: a line across, off the mast
      g.lineWidth = 3;
      g.beginPath(); g.moveTo(32, 14); g.lineTo(32, 52); g.moveTo(32, 18); g.lineTo(54, 46); g.stroke();
    }
    if (kind === "tug" && !hollow) {          // a tug's wheelhouse, cut out
      g.globalCompositeOperation = "destination-out";
      g.fillRect(26, 30, 12, 10);
      g.globalCompositeOperation = "source-over";
    }
    return g.getImageData(0, 0, S, S);
  }
  function chip() {
    const S = 32, c = document.createElement("canvas");
    c.width = c.height = S;
    const g = c.getContext("2d");
    g.globalAlpha = 0.8;
    g.fillStyle = HALO;
    g.beginPath(); g.roundRect(1, 1, S - 2, S - 2, 8); g.fill();
    return g.getImageData(0, 0, S, S);
  }
  function look(v) {
    const c = CLASS[v.cls] || CLASS.unknown;
    const kind = has(course(v)) ? c.shape : "dot";
    const size = v.len >= 100 ? 1.25 : v.len >= 40 ? 1.05 : 0.85;
    return { kind, color: token(...c.token), scale: size };
  }

  // ---- the vessels a selected marine call names (panel()): ringed on every map with the layer on ----------------------
  const named = { ids: new Set(), sig: "" };
  function setNamed(ids) {
    const sig = [...ids].sort().join(" ");
    if (sig === named.sig) return;
    named.ids = new Set(ids);
    named.sig = sig;
    for (const m of maps) m.draw(true);
  }

  class VesselMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.added = false;
      this.offFor = null;                   // the marine call this viewer unticked the layer on
      this.live = null;
      maps.add(this);
      this.map.on("click", (e) => this.clicked(e));
      this.fol = new MapFollow.Follower(this.map, control.host, { cls: "ac-follow", textCls: "ac-follow-t", icon: "vessel",
                                                                 onStop: () => this.draw(true) });
      if (this.isOn()) kick();
    }
    get following() { return this.fol.id; }
    focusedMarine() { const f = this.control.focused; return this.control.profile === "incident" && isMarine(f) ? f : null; }
    isOn() {
      const f = this.focusedMarine();
      return this.control.on(KEY, false) || (!!f && this.offFor !== f.id);
    }
    keys() { return [[KEY, false]]; }
    refresh() { this.sync(); kick(); }
    ready() { try { return this.added || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }

    add() {
      const map = this.map;
      if (map.getSource(SRC)) { this.added = true; return; }
      const glyphs = !!(map.getStyle() && map.getStyle().glyphs);
      const ids = (map.getStyle().layers || []).map((x) => x.id);
      // under the aircraft (when drawn), the map layers' marks, the call line and the stations; the pins, rigs and chips
      // are HTML markers, always above
      const before = ids.find((id) => id === "ac-trail" || id.startsWith("ml-") || id === "cm-lines" || id === "firehouse");
      map.addSource(SRC, { type: "geojson", data: fc([]) });
      for (const kind of ["ship", "tug", "boat", "sail", "dot"]) {
        for (const hollow of [false, true]) {
          const name = `vs-${kind}${hollow ? "-old" : ""}`;
          if (!map.hasImage(name)) map.addImage(name, mark(kind, hollow), { pixelRatio: 2, sdf: true });
        }
      }
      if (!map.hasImage("vs-chip")) map.addImage("vs-chip", chip(), { pixelRatio: 2, stretchX: [[8, 24]], stretchY: [[8, 24]], content: [8, 8, 24, 24] });
      const vis = { visibility: "none" };
      const iconSize = ["interpolate", ["linear"], ["zoom"], 9, ["*", 0.45, ["get", "scale"]], 12, ["*", 0.7, ["get", "scale"]],
                        15, ["*", 1.0, ["get", "scale"]]];
      map.addLayer({ id: "vs-mark", type: "symbol", source: SRC,
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map",
                               "icon-allow-overlap": true, "icon-ignore-placement": true, "icon-size": iconSize },
                     paint: { "icon-color": ["get", "color"], "icon-halo-color": HALO, "icon-halo-width": 1, "icon-opacity": ["get", "opacity"] } }, before);
      const label = (size, color) => ({
        layout: { ...vis, "text-field": ["format", ["get", "name"], { "font-scale": 1.0 }, "\n", {},
                                         ["get", "detail"], { "font-scale": 0.82, "text-font": ["literal", ["Noto Sans Regular"]] }],
                  "text-font": ["Noto Sans Bold"], "text-size": size, "text-anchor": "bottom-left", "text-justify": "left",
                  "text-offset": [1.0, -0.5], "icon-image": "vs-chip", "icon-text-fit": "both", "icon-text-fit-padding": [3, 6, 2, 6] },
        paint: { "text-color": color, "text-opacity": ["get", "opacity"], "icon-opacity": ["*", 0.85, ["get", "opacity"]] } });
      if (glyphs) map.addLayer({ id: "vs-label", type: "symbol", source: SRC, minzoom: LABEL_ZOOM, ...label(11, ["get", "color"]) }, before);
      map.addSource(HL_SRC, { type: "geojson", data: fc([]) });
      map.addLayer({ id: "vs-hl-ring", type: "circle", source: HL_SRC, layout: vis,
                     paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 9, 12, 13, 15, 18], "circle-color": "rgba(0,0,0,0)",
                              "circle-stroke-color": ["get", "ringc"], "circle-stroke-width": ["get", "ringw"], "circle-stroke-opacity": ["get", "opacity"] } }, before);
      map.addLayer({ id: "vs-hl-mark", type: "symbol", source: HL_SRC,
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map",
                               "icon-allow-overlap": true, "icon-ignore-placement": true, "icon-size": iconSize },
                     paint: { "icon-color": ["get", "color"], "icon-halo-color": HALO, "icon-halo-width": 1.5, "icon-opacity": ["get", "opacity"] } }, before);
      if (glyphs) {
        const l = label(12, ["get", "ringc"]);
        Object.assign(l.layout, { "text-allow-overlap": true, "text-ignore-placement": true, "icon-allow-overlap": true, "icon-ignore-placement": true });
        map.addLayer({ id: "vs-hl-label", type: "symbol", source: HL_SRC, ...l }, before);
      }
      const canvas = map.getCanvas();
      for (const id of MARKS) {
        map.on("mouseenter", id, () => { canvas.style.cursor = "pointer"; });
        map.on("mousemove", id, (e) => {
          const vid = e.features && e.features[0] && e.features[0].properties.id;
          const v = vid && find(vid);
          const t = v ? brief(v) : "";
          if (canvas.title !== t) canvas.title = t;
        });
        map.on("mouseleave", id, () => { canvas.style.cursor = ""; canvas.title = ""; });
      }
      this.added = true;
    }

    sync() {
      if (!this.isOn() && !this.added) { this.panelChanged(); return; }
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      try { this.add(); } catch (e) { console.error("vessels layer", e); return; }
      const on = this.isOn();
      if (!on) this.fol.stop();
      if (this.live) this.live.refresh();
      for (const id of IDS) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
      if (on && store.data) this.draw(true);
      this.panelChanged();
    }
    draw(force) {
      const d = store.data;
      if (!d || !this.added || !this.isOn()) return;
      if (!force && !this.map.getContainer().offsetParent) return;
      const marks = [], hl = [], ring = token("--accent-text", "#EE6475");
      for (const v of d.vessels || []) {
        const age = ageNow(v), stale = age > staleS(d), lk = look(v), at = stale ? { lon: v.lon, lat: v.lat, rot: course(v) || 0 } : where(v);
        const props = { id: v.id, icon: `vs-${lk.kind}${stale ? "-old" : ""}`, rot: lk.kind === "dot" ? 0 : at.rot, color: lk.color,
                        scale: lk.scale, opacity: alpha(age), name: nameOf(v), detail: [v.label, speed(v)].filter(Boolean).join(" · ") };
        const f = { type: "Feature", geometry: { type: "Point", coordinates: [at.lon, at.lat] }, properties: props };
        const isNamed = named.ids.has(v.id);
        if (isNamed || this.following === v.id) {
          Object.assign(props, isNamed ? { ringc: ring, ringw: 3, color: ring } : { ringc: token("--text", "#E6E8EB"), ringw: 2 });
          hl.push(f);
        } else marks.push(f);
      }
      const src = this.map.getSource(SRC);
      if (src) src.setData(fc(marks));
      const hs = this.map.getSource(HL_SRC);
      if (hs && (hl.length || this.hlShown)) hs.setData(fc(hl));
      this.hlShown = hl.length > 0;
      if (this.following) this.track(hl.find((f) => f.properties.id === this.following), force);
    }
    follow(id) {
      const v = find(id);
      if (!v) return;
      this.fol.start(id, where(v), FOLLOW_ZOOM);
      this.draw(true);
      animate();
    }
    track(f, force) {
      if (f) this.fol.pan(f.geometry.coordinates);
      const v = find(this.following);
      if (!v) { this.fol.stop(); return; }                        // dropped by the hub (no report for 10 min)
      if (!force && this.fol.chipShown()) return;
      this.fol.chip(`Following ${nameOf(v)}`, brief(v), { alerted: named.ids.has(v.id) });
    }
    frameMs() {
      try {
        const fastest = Math.max(0, ...((store.data && store.data.vessels) || []).map((v) => (v.sog || 0) * KT));
        const metresPerPx = 40075016.686 * Math.cos(this.map.getCenter().lat * Math.PI / 180) / (512 * 2 ** this.map.getZoom());
        const pxPerS = fastest / metresPerPx;
        return Math.max(FRAME_MS, Math.min(MAX_FRAME_MS, pxPerS > 0 ? 1000 * STEP_PX / pxPerS : MAX_FRAME_MS));
      } catch (_) { return MAX_FRAME_MS; }
    }
    panelChanged() { if (this.control.panel && !this.control.panel.hidden) this.control.renderPanel(); }

    clicked(e) {
      if (!this.isOn() || !this.added || !store.data || !this.map.getLayer("vs-mark")) return;
      const hits = this.map.queryRenderedFeatures(e.point, { layers: MARKS.filter((id) => this.map.getLayer(id)) });
      if (!hits.length) return;
      e.originalEvent && e.originalEvent.stopPropagation();
      const id = hits[0].properties.id;
      setTimeout(() => { this.follow(id); this.show(id); }, 0);
    }
    // the box, kept up to date while open (MapFollow.LiveInfo); a made-up vessel:
    //   ⛴ EXAMPLE TUG                       Tug · 28 m
    //   6.2 kn · 210° · Under way
    //   → EXAMPLE HARBOR
    //   MMSI 999000001 · WDX0000
    //   aisstream.io · 12 s ago                         ⓘ
    show(id) {
      const old = this.live;
      this.live = null;
      if (old) old.stop();
      this.control.close();
      const box = new MapFollow.LiveInfo(this.control, () => this.view(id), { onClose: () => { if (this.live === box) this.live = null; } });
      this.live = box.open ? box : null;
    }
    view(id) {
      const v = find(id);
      if (!v) return null;
      const age = ageNow(v), stale = age > staleS(store.data), lines = [];
      if (stale) lines.push(h("div", "warn", `No report for ${ago(age * 1000)}`));
      if (named.ids.has(v.id)) lines.push(h("div", "warn", "Named in the call"));
      lines.push([speed(v), has(v.cog) ? `${v.cog}°` : "", v.nav].filter(Boolean).join(" · ") || "No speed or course");
      if (has(v.hdg) && v.hdg !== v.cog) lines.push(`Heading ${v.hdg}°`);
      if (v.dest) lines.push(`→ ${v.dest}`);
      lines.push([`MMSI ${v.id}`, v.cs, v.imo ? `IMO ${v.imo}` : ""].filter(Boolean).join(" · "));
      const size = [has(v.len) ? `${v.len} m` : "", has(v.beam) ? `${v.beam} m beam` : ""].filter(Boolean).join(" × ");
      const tip = [`Position: ${SRC_NAME[v.src] || v.src || "AIS"}${v.c === "B" ? " (class B)" : v.c === "LR" ? " (long range, coarse)" : ""}`,
                   tracker && !stale ? `Drawn on between reports by its course and speed (at most ${HORIZON_S} s)` : "",
                   AIS_TIP, store.data && store.data.credit ? `Data: ${store.data.credit}` : ""].filter(Boolean).join("\n");
      return { title: nameOf(v), sub: [v.label, size].filter(Boolean).join(" · "), lines,
               foot: [SRC_NAME[v.src] || v.src || "", `${ago(age * 1000)} ago`].filter(Boolean).join(" · "),
               icon: window.Icon ? Icon("vessel") : null, tip };
    }

    // the group in the Layers box
    panel() {
      const d = store.data, box = h("div", "ml-group"), head = h("div", "ml-ghead");
      head.append(h("b", "", "Vessels"), h("span", "spacer"), h("span", "dim small", "AIS"));
      box.append(head);
      const lab = h("label", "check"), cb = h("input");
      cb.type = "checkbox";
      cb.checked = this.isOn();
      cb.dataset.key = KEY;
      const f = this.focusedMarine(), forced = !!f && this.isOn() && !this.control.on(KEY, false);
      cb.addEventListener("change", () => {
        if (!cb.checked && f) this.offFor = f.id;                 // off for this marine call
        this.control.setLayer(KEY, cb.checked);
        this.sync();
        kick();
      });
      lab.append(cb, ...(window.Icon ? [Icon("vessel")] : []), h("span", "", "Ships & boats"), ...(forced ? [h("span", "dim small", "marine call")] : []));
      lab.title = `Western Long Island Sound\n${AIS_TIP}`;
      box.append(lab);
      if (!this.isOn()) return box;
      if (!d) { box.append(h("div", "ml-note", store.failed ? "Unavailable" : "Loading…")); return box; }
      if (!d.configured || d.note) { box.append(h("div", "ml-note", cap(d.note) || "No AIS source")); return box; }
      const list = d.vessels || [], moving = list.filter((v) => (v.sog || 0) >= 0.5).length;
      box.append(h("div", "ml-note", `${list.length} vessels · ${moving} moving`));
      box.append(h("div", "ml-note", `Faded: no report >${Math.round(staleS(d) / 60)} min`));
      const feeds = (d.feeds || []).map((x) => `${SRC_NAME[x.key] || x.label}: ${x.state === "resting" ? `resting (${x.error})` : x.state}`);
      const fl = h("div", "ml-note", feeds.join(" · "));
      fl.title = AIS_TIP;
      box.append(fl);
      return box;
    }
  }

  function find(id) { return id ? ((store.data && store.data.vessels) || []).find((v) => v.id === id) || null : null; }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new VesselMap(control) });

  // a vessel followed on a map on screen (the panel's row): the layer on there, then followed once the data is in
  function follow(id, tries = 25) {
    const m = MapFollow.shownMap(maps);
    if (!m) return;
    if (!m.isOn()) { m.control.setLayer(KEY, true); kick(); }
    if (!m.added || !find(id)) { if (tries > 0) setTimeout(() => follow(id, tries - 1), 400); return; }
    m.follow(id);
    m.show(id);
  }

  // ---- the event's "Vessels nearby" (cadmap.js detail): a marine call's named and nearest vessels --------------------
  const panels = new Map();                 // host -> {inc, key, data, open, timer, seq}
  function panel(inc, host) {
    if (!host) return;
    let st = panels.get(host);
    if (!st) {
      st = { inc: null, key: "", data: null, open: true, timer: null, seq: 0 };
      panels.set(host, st);
      st.timer = setInterval(() => { if (st.key && document.visibilityState !== "hidden") ask(st, host); }, PANEL_MS);
    }
    st.inc = inc || null;
    const ok = isMarine(inc) && !["closed", "dismissed"].includes(inc.status);
    const key = ok ? JSON.stringify([inc.id, inc.lat, inc.lon]) : "";
    if (!ok) { st.key = ""; st.data = null; host.hidden = true; host.replaceChildren(); setNamed([]); return; }
    if (key === st.key) return;
    st.key = key;
    st.data = null;
    ask(st, host);
  }
  function ask(st, host) {
    const inc = st.inc, key = st.key, seq = ++st.seq;
    if (!inc || !key) return;
    return fetch(`${NEAR}?event=${encodeURIComponent(inc.id)}`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null)).then((d) => {
        if (st.key !== key || seq !== st.seq || !d) return;
        st.data = d;
        setNamed((d.named || []).map((v) => v.id));
        draw(st, host);
      }).catch(() => { /* keep the last answer */ });
  }
  function row(v, tag) {
    const r = h("button", "ac-row");
    r.type = "button";
    r.append(h("span", "ac-tag", tag), h("b", "", nameOf(v)),
             h("span", "dim small", [v.label, has(v.dist) ? `${v.dist.toFixed(1)} nm` : "", speed(v), has(course(v)) ? `${course(v)}°` : ""].filter(Boolean).join(" · ")));
    if (ageNow(v) > staleS(st0())) r.append(h("span", "ac-stale small", "old"));
    r.title = `${brief(v)}\nClick: follow it on the map`;
    r.addEventListener("click", () => follow(v.id));
    return r;
  }
  const st0 = () => store.data;
  function draw(st, host) {
    const d = st.data;
    host.hidden = false;
    const head = h("button", "ml-cams-head");
    head.type = "button";
    head.setAttribute("aria-expanded", String(st.open));
    if (window.Icon) head.append(Icon("vessel"));
    head.append(h("b", "", "Vessels nearby"), h("span", "spacer"), h("span", "dim small", "AIS"), h("span", "car dim", st.open ? "▾" : "▸"));
    head.title = AIS_TIP;
    head.addEventListener("click", () => { st.open = !st.open; draw(st, host); });
    host.replaceChildren(head);
    if (!st.open) return;
    if (!d.configured || d.note) { host.append(h("div", "ml-note", cap(d.note) || "No AIS source")); return; }
    const list = h("div", "ac-list"), seen = new Set();
    for (const v of d.named || []) { seen.add(v.id); list.append(row(v, "named")); }
    for (const v of d.nearest || []) if (!seen.has(v.id)) list.append(row(v, "near"));
    if (!list.children.length) list.append(h("div", "ml-note", "None within 10 nm"));
    host.append(list);
  }

  return { load, maps, data: () => store.data, KEY, isMarine, panel, follow, drift, named: () => [...named.ids], VesselMap };
})();
