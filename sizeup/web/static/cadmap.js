// sizeup CAD maps: the working event's map (bigger, with an Expand overlay), the Overview page
// (every open call: a large map, a card each, who is talking), which is also the whole wall board (/monitor).
//
// Every map shows everything (the open incidents' pins, every rig the server placed) and focuses on
// the current call. Where a rig goes is decided on the server (sizeup/cad/mapview.py, state.map):
//
//   available / out of service      at its home firehouse (greyed when out of service)
//   dispatched / responding         at its firehouse, with a dashed line to its call
//   on location                     fanned out around the call's pin
//   on a call, no home station      the outer ring at the call (outlined: not there yet)
//   live position (future AVL)      where the feed says (state.map marker place "position")
//   cleared a call, on its way home  faded, part way from the call to its firehouse (an estimate: place
//                                    "returning"), with a thin line home
//   no home station, not on a call  not drawn: listed in "Station not set" (click one to set it)
//
// This file only lays the server's markers out: chips fanned around their anchor in screen pixels
// (so they stay neat at every zoom), the dashed lines, the pins, the station picker. A live AVL
// position arrives as a marker with place "position" (its fix's at, heading, speed, source).
//
// Our units, followed and lit up (#358; the follow, its chip and the rings' state: mapfollow.js, shared with the
// aircraft):
//
//   Follow       a click on a rig, or Follow on its unit-board row or its row on an incident: centred (zoom 15 at
//                least) and kept centred as it moves, "Following E60 · On scene · 12 s ago" at the foot with ✕ (the
//                age only for a live fix). A drag, Recenter, ✕, another call framed, or the rig no longer on the map
//                lets go. Following another rig, or an aircraft, on the same map lets go of this one.
//   Rings        transmitting (Radio.talkingFor): a pulsing ring and a halo; picked (Follow, a talker chip:
//                CadMap.focusUnit) ringed for HIGHLIGHT_MS once centred; its board or incident row hovered: wider;
//                followed: a plain ring.
//   Live fixes   glide.js's follower with motion.js's Motion.arc, as for aircraft: drawn on between fixes along its
//                heading and speed, eased onto each new fix. Older than the hub's avl.stale_s: hollow, faded, its
//                age on the chip; older than avl.max_age_s the hub places it by status again (mapview.py).
//
// Highway layers (cameras, incidents, signs, mileposts, exits) and their Layers button: maplayers.js,
// attached to every RigMap; the event's "Cameras nearby" row from detail().
//
// RigLayer and RigMap use nothing from cad.js; the CAD glue at the
// bottom (CadMap.detail, CadMap.overview) uses cad.js's helpers and state.
"use strict";

window.CadMap = (() => {
  const STATION_R = 34, INC_R = 30, RING_GAP = 24, CHIP_W = 48;   // px: the fan-out around an anchor
  const FAR_ZOOM = 13.6;               // below this: smaller chips, tighter rings
  const FOCUS_MAX_ZOOM = 16.3;
  const LINE = "#E0A030", HOME_LINE = "#AEB6C0";      // to a call | a returning rig's way home (thin)
  const LOOK_NAMES = { available: "available", oos: "out of service", dispatched: "dispatched",
                       responding: "responding", onscene: "on location" };
  // No call open: a call farther than this from home is not framed (#218). Our mutual-aid neighbours (Rye, Harrison,
  // Greenwich, Mamaroneck, White Plains, Stamford, the airport) are within it; Long Island's shore (14 km and more,
  // across the Sound: a bad pick or geocode) is not.
  const AREA_KM = 12;
  const HOME_PAD = 0.02;               // no home bounds in the scene (an older hub): this much around its centre, degrees

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const statusOf = (lists, code) => ((lists && lists.unit_statuses) || []).find((s) => s.code === code) || { code, name: code };
  const typeOf = (lists, code) => ((lists && lists.event_types) || []).find((t) => t.code === code) || null;
  function talkState(name) {
    const t = window.Radio && Radio.talkingFor(name);
    return t ? (t.ended ? "talked" : "talking") : "";
  }

  // ---- our units followed and lit up (#358): one state for every map ----------------------------------------------
  const UNIT_ZOOM = 15;                // a rig followed or picked: centred at least this close
  const KT_MS = 1852 / 3600;           // m/s in a knot (glide.js reads kt; the ingest shape's speed is m/s)
  const FOCUS_WAIT_MS = 5000;          // a pick waits this long for a map to show the rig (the Overview opening)
  const STALE_S = 60, MAX_AGE_S = 300; // an older hub: mapview.py's POSITION_STALE_S / POSITION_MAX_AGE_S
  const rigMaps = new Set(), rigLayers = new Set();
  const lights = window.MapFollow ? new MapFollow.Lights(() => redrawRigs()) : null;
  const glide = window.Glide && window.Motion ? Glide.follower(Motion.arc, { horizonS: 10, staleS: STALE_S }) : null;
  const skew = () => (typeof cad !== "undefined" && cad && cad.skew) || 0;
  const nowS = () => Date.now() / 1000 + skew();
  const agoS = (s) => (window.MapFollow ? MapFollow.ago(s * 1000) : `${Math.round(s)} s`);
  function redrawRigs() { for (const l of rigLayers) l.draw(); }
  const avlOf = (M) => ({ stale_s: (M && M.avl && M.avl.stale_s) || STALE_S, max_age_s: (M && M.avl && M.avl.max_age_s) || MAX_AGE_S });
  // a live fix: how old (s), stale, and how faded (full until stale, down to 0.35 by the time the hub drops it)
  function fixAge(m, M) {
    if (m.place !== "position" || typeof m.at !== "number") return null;
    const a = avlOf(M), age = Math.max(0, nowS() - m.at), stale = age > a.stale_s;
    return { age, stale, fade: stale ? Math.max(0.35, 1 - 0.65 * (age - a.stale_s) / Math.max(1, a.max_age_s - a.stale_s)) : 1 };
  }
  // each live fix handed to the follower once a state (the fix as it was when it was true, on this browser's clock)
  let fedFor = null;
  function feedGlide(M) {
    if (!glide || !M || M === fedFor) return;
    fedFor = M;
    glide.staleS = avlOf(M).stale_s;
    const keep = new Set();
    for (const m of M.markers || []) {
      if (m.place !== "position" || typeof m.at !== "number") continue;
      keep.add(m.unit);
      const hd = typeof m.heading === "number" ? m.heading : null;
      glide.set(m.unit, { lon: m.lon, lat: m.lat, rot: hd || 0, at: (m.at - skew()) * 1000, trk: hd,
                          gs: typeof m.speed === "number" ? m.speed / KT_MS : null });
    }
    glide.forget(keep);
  }
  // frames while a map on screen has a live fix or follows a rig: the fixes drawn on, the follow kept centred, and
  // once a second the chips (a fix's age, stale) and the follow chip's words
  let raf = 0, slowAt = 0;
  const wanted = () => (typeof document === "undefined" || document.visibilityState !== "hidden") && [...rigLayers].some((l) => l.moving());
  function animate() { if (!raf && typeof requestAnimationFrame === "function" && wanted()) raf = requestAnimationFrame(frame); }
  function frame(ts) {
    raf = 0;
    if (!wanted()) return;
    const slow = ts - slowAt >= 1000;
    if (slow) slowAt = ts;
    for (const l of rigLayers) if (l.moving()) l.tick(slow);
    raf = requestAnimationFrame(frame);
  }
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("visibilitychange", animate);
    // a talker chip (radio.js's strip, the Overview's): that rig centred on a map, ringed for a while
    document.addEventListener("click", (e) => {
      const c = e.target && e.target.closest && e.target.closest(".talk-chip[data-rig]");
      if (c) focusUnit(c.dataset.rig);
    });
  }
  const onMap = (name) => !!(typeof cad !== "undefined" && cad && cad.state && cad.state.map
                             && (cad.state.map.markers || []).some((m) => m.unit === name));
  // the map to show a rig on: the large one if open, else the first on screen (never the new call's)
  function shownRigMap() {
    if (views.bigOpen && views.big) return views.big;
    return window.MapFollow ? MapFollow.shownMap([...rigMaps].filter((r) => r.fol)) : null;
  }
  let pending = null;
  function pickUnit(name, follow) {
    if (!name || !onMap(name)) return false;
    pending = { name, follow, until: Date.now() + FOCUS_WAIT_MS };
    if (!shownRigMap()) { const b = document.getElementById("overview-open"); if (b) b.click(); }
    tryPick();
    return true;
  }
  function tryPick() {
    const p = pending;
    if (!p || Date.now() > p.until) { pending = null; return; }
    const rm = shownRigMap();
    if (!rm || !rm.layer.chips.has(p.name)) { setTimeout(tryPick, 300); return; }
    pending = null;
    try { rm.host.scrollIntoView({ block: "nearest" }); } catch (_) { /* */ }
    if (p.follow) rm.followUnit(p.name); else rm.focusUnit(p.name);
  }
  const followUnit = (name) => pickUnit(name, true);     // a unit-board or incident row's Follow
  const focusUnit = (name) => pickUnit(name, false);     // a talker chip: centred and ringed, not followed
  function hoverUnits(names) { if (lights && lights.hover(names)) redrawRigs(); }   // a row hovered: its rig ringed wider

  // ---- the area's frame (#218): what a map with no call open (the Overview, the wall board) looks at ---------------
  // Our area (scene.home.bounds: Port Chester and Rye Brook, roads.AREA_BBOX) and every open call with a real point
  // within AREA_KM of home. Never a layer's features (a district's polygon runs out over the Sound), a far point (a
  // pick or a geocode across the water had the Overview showing Long Island) or a missing one read as 0.
  function goodLL(lon, lat) {          // a point that can be on our map: numbers, in range, neither one 0
    return typeof lon === "number" && typeof lat === "number" && Number.isFinite(lon) && Number.isFinite(lat)
      && Math.abs(lon) <= 180 && Math.abs(lat) <= 90 && lon !== 0 && lat !== 0;
  }
  function km([lon1, lat1], [lon2, lat2]) {
    const r = Math.PI / 180, a = Math.sin(((lat2 - lat1) * r) / 2) ** 2
      + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
    return 12742 * Math.asin(Math.min(1, Math.sqrt(a)));
  }
  function homeBox(home) {             // [w, s, e, n]: the scene's bounds, else a box around its centre
    const b = home && home.bounds, c = home && home.center;
    if (Array.isArray(b) && b.length === 4 && goodLL(b[0], b[1]) && goodLL(b[2], b[3]) && b[0] < b[2] && b[1] < b[3]) return b.slice();
    if (Array.isArray(c) && goodLL(c[0], c[1])) return [c[0] - HOME_PAD, c[1] - HOME_PAD, c[0] + HOME_PAD, c[1] + HOME_PAD];
    return null;
  }
  // [[lon, lat], ...] of the calls that count: a real point within `within` km of home (its centre, else its box's)
  function areaCalls(home, calls, within = AREA_KM) {
    const box = homeBox(home);
    if (!box) return [];
    const c = home.center && goodLL(home.center[0], home.center[1]) ? home.center : [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2];
    return (calls || []).filter((p) => p && goodLL(p.lon, p.lat) && km(c, [p.lon, p.lat]) <= within).map((p) => [p.lon, p.lat]);
  }
  // [[w, s], [e, n]]: our area and the calls in it; null with no home at all
  function areaBounds(home, calls, within = AREA_KM) {
    const box = homeBox(home);
    if (!box) return null;
    let [w, s, e, n] = box;
    for (const [lon, lat] of areaCalls(home, calls, within)) {
      w = Math.min(w, lon); e = Math.max(e, lon); s = Math.min(s, lat); n = Math.max(n, lat);
    }
    return [[w, s], [e, n]];
  }

  // ---- RigLayer: pins, rig chips, lines and the station box on a MapLibre map -------------------

  class RigLayer {
    // opts: {host (the map's box, for the overlays), pins, unset, editable, operator(), onPick(id)}
    constructor(map, opts) {
      this.map = map;
      this.opts = opts;
      this.pins = new Map();
      this.chips = new Map();
      this.data = null;
      this.focus = null;
      this.lineSig = "";
      this.far = false;
      this.unsetOpen = false;
      this.fol = null;                 // the map's follower (RigMap: mapfollow.js), none on a manual map
      this.onChip = null;              // RigMap: a rig clicked (follow it)
      rigLayers.add(this);
      if (opts.unset) {
        this.unsetBox = h("div", "cm-unset");
        this.unsetBox.hidden = true;
        opts.host.append(this.unsetBox);
      }
      this.pop = h("div", "cm-pop");
      this.pop.hidden = true;
      this.pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); this.closePicker(); } });
      opts.host.append(this.pop);
      const init = () => {
        if (!map.getSource("cm-lines")) {
          map.addSource("cm-lines", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
          map.addLayer({
            id: "cm-lines", type: "line", source: "cm-lines",
            layout: { "line-cap": "round", "line-join": "round" },
            paint: { "line-color": ["get", "color"], "line-width": ["case", ["get", "home"], 1.6, 2.5],
                     "line-opacity": ["case", ["get", "home"], 0.7, 0.9], "line-dasharray": [1.6, 1.6] },
          }, map.getLayer("firehouse") ? "firehouse" : undefined);
        }
        this.ready = true;
        this.draw();
      };
      // A map created here waits for its style; one already up (the monitor's) may be busy loading
      // tiles (isStyleLoaded() false, "load" long gone): try now, else when it's idle.
      const tryInit = () => {
        if (this.ready) return;
        try { init(); } catch (e) { map.once("idle", tryInit); }
      };
      if (map.isStyleLoaded() || map.loaded()) tryInit();
      else { map.once("load", tryInit); map.once("idle", tryInit); }
      map.on("zoom", () => {
        const far = map.getZoom() < FAR_ZOOM;
        if (far !== this.far) { this.far = far; opts.host.classList.toggle("cm-far", far); this.draw(); }
      });
    }

    update(data, focus) {
      this.data = data;
      this.focus = focus || null;
      feedGlide(data && data.map);
      this.draw();
    }

    draw() {
      if (!this.ready || !this.data || !this.data.map) return;
      const M = this.data.map, lists = this.data.lists;
      if (this.opts.pins) this.drawPins(M.incidents || [], lists);
      this.drawChips(M.markers || [], lists);
      this.drawLines(M.markers || [], lists);
      if (this.unsetBox) this.drawUnset(M.unset || []);
      this.followTick(true);
      animate();
    }

    // where a rig's chip goes now: a live fix drawn on by the follower, else where the hub put it
    where(m) {
      const at = glide && m.place === "position" ? glide.where(m.unit, Date.now()) : null;
      return at ? [at.lon, at.lat] : [m.lon, m.lat];
    }
    shown() { try { return !!this.map.getContainer().offsetParent; } catch (_) { return false; } }
    // anything to animate here: a live fix, or a rig followed
    moving() { return !!(this.ready && this.shown() && ((this.fol && this.fol.id) || this.hasFix)); }
    tick(slow) {
      if (slow) { this.draw(); return; }
      // (every frame, not only while glide.busy(): a fix set in the same millisecond as the last draw reads as settled)
      for (const rec of this.chips.values()) if (rec.m.place === "position") rec.marker.setLngLat(this.where(rec.m));
      this.followTick(false);
    }
    // following: the map kept on the rig, and the chip's words; gone from the map: let go
    followTick(force) {
      const f = this.fol;
      if (!f || !f.id) return;
      const rec = this.chips.get(f.id);
      if (!rec) { f.stop(); return; }
      const ll = rec.marker.getLngLat();
      f.pan([ll.lng, ll.lat]);
      if (!force && f.chipShown()) return;
      const m = rec.m, st = statusOf(this.data && this.data.lists, m.status), fx = fixAge(m, this.data && this.data.map);
      const text = [`Following ${m.unit}`, st.name, fx ? `${agoS(fx.age)} ago` : ""].filter(Boolean).join(" · ");
      f.chip(text, rec.el.title, { stale: !!(fx && fx.stale) });
    }

    drawPins(incidents, lists) {
      const want = new Map(incidents.map((p) => [p.id, p]));
      const f = this.focus;
      if (f && f.lat != null && f.lon != null) want.set(f.id, f);
      for (const [id, p] of want) {
        const isFocus = !!f && f.id === id;
        const t = typeOf(lists, p.type);
        const color = t ? t.color : "#ff4d3d";
        const label = [p.number || (p.status === "queued" ? "Page" : ""), t ? t.name : ""].filter(Boolean).join(" ");
        const sig = JSON.stringify([p.lat, p.lon, color, label, isFocus, !!f, p.status, p.address]);
        let rec = this.pins.get(id);
        if (rec && rec.sig === sig) continue;
        if (rec) rec.marker.remove();
        let e;
        if (isFocus) {                                  // the open call: the pulsing pin (its rigs fan out around it)
          e = h("div", "inc-marker cm-focus");
          e.title = label;
          e.append(h("i", "ring"), h("i", "ring r2"), h("i", "dot"));
        } else {
          e = h("button", "cm-pin" + (f ? " dim" : "") + (p.status === "queued" ? " queued" : ""));
          e.type = "button";
          e.append(h("i", "dot"), h("span", "lbl", label || "Incident"));
          e.title = `${label}${p.address ? ", " + p.address : ""}`;
          e.addEventListener("click", (ev) => { ev.stopPropagation(); if (this.opts.onPick) this.opts.onPick(id); });
        }
        e.style.setProperty("--c", color);
        const marker = new maplibregl.Marker({ element: e, anchor: "center" }).setLngLat([p.lon, p.lat]).addTo(this.map);
        this.pins.set(id, { marker, sig });
      }
      for (const [id, rec] of this.pins) if (!want.has(id)) { rec.marker.remove(); this.pins.delete(id); }
    }

    // Each anchor's chips on rings around it: on location (ring 0) close in, not-there-yet (ring 1) outside.
    // At a firehouse a full ring from the bottom (its name is drawn to the right); at a call an arc over
    // the top and sides, leaving the bottom for the call's label.
    offsets(markers) {
      const k = this.far ? 0.72 : 1, w = CHIP_W * k;
      const groups = new Map();
      for (const m of markers) {
        if (!groups.has(m.group)) groups.set(m.group, [[], []]);
        groups.get(m.group)[m.ring ? 1 : 0].push(m);
      }
      const out = new Map();
      for (const [, rings] of groups) {
        let r = 0;
        rings.forEach((list, ring) => {
          if (!list.length) return;
          const m0 = list[0];
          if (m0.place === "position" || m0.place === "returning") { list.forEach((m) => out.set(m.unit, [0, 0])); return; }
          const n = list.length, deg = Math.PI / 180;
          if (m0.place === "station") {
            r = Math.max(ring ? r + RING_GAP * k : STATION_R * k, (n * w) / (2 * Math.PI));
            list.forEach((m, i) => {
              const a = Math.PI / 2 + (2 * Math.PI * i) / n + (ring ? Math.PI / n : 0);
              out.set(m.unit, [Math.round(r * Math.cos(a)), Math.round(r * Math.sin(a) * 0.82)]);
            });
            return;
          }
          const step = n > 1 ? Math.min(270 / (n - 1), 62) * deg : 0;       // centred on the top, 270° at most
          const need = step ? w / (2 * Math.sin(step / 2)) : 0;                // neighbours a chip apart
          r = Math.max(ring ? r + RING_GAP * k : INC_R * k, need);
          list.forEach((m, i) => {
            const a = -Math.PI / 2 + (i - (n - 1) / 2) * step;
            out.set(m.unit, [Math.round(r * Math.cos(a)), Math.round(r * Math.sin(a) * 0.82)]);
          });
        });
      }
      return out;
    }

    drawChips(markers, lists) {
      const offs = this.offsets(markers);
      const seen = new Set();
      const M = this.data && this.data.map;
      this.hasFix = false;
      for (const m of markers) {
        seen.add(m.unit);
        const st = statusOf(lists, m.status);
        const talk = talkState(m.unit);
        const off = offs.get(m.unit) || [0, 0];
        const onFocus = !!this.focus && m.incident_id === this.focus.id;
        const fx = fixAge(m, M);                               // a live fix: its age, stale (hollow, faded, aged)
        if (fx) this.hasFix = true;
        const age = fx && fx.stale ? agoS(fx.age) : "";
        // the rings (#358): picked (for a while), its row hovered (wider), followed (plain); transmitting: talk
        const lit = lights ? [lights.isPicked(m.unit), lights.hovered(m.unit), !!this.fol && this.fol.id === m.unit] : [false, false, false];
        const sig = JSON.stringify([m.lon, m.lat, off, m.look, m.ring, m.place, st.bg, talk, m.tag, m.station, m.incident_number,
          m.returning_from, onFocus, !!this.focus, this.far, age, fx && Math.round(fx.fade * 20), lit, m.source]);
        let rec = this.chips.get(m.unit);
        if (!rec) {
          const e = h("button", "cm-rig");
          e.type = "button";
          e.addEventListener("click", (ev) => { ev.stopPropagation(); this.chipClick(this.chips.get(m.unit).m); });
          const marker = new maplibregl.Marker({ element: e, anchor: "center" }).setLngLat(this.where(m)).addTo(this.map);
          rec = { marker, el: e, sig: "" };
          this.chips.set(m.unit, rec);
        }
        rec.m = m;
        if (fx) rec.marker.setLngLat(this.where(m));            // drawn on between fixes (glide.js), every draw
        if (rec.sig === sig) continue;
        rec.sig = sig;
        const e = rec.el;
        const keep = [...e.classList].filter((c) => c.startsWith("maplibregl-")).join(" ");   // the marker's own
        e.className = `${keep} cm-rig look-${m.look} at-${m.place}` + (m.ring ? " outer" : "") + (talk ? ` ${talk}` : "")
          + (this.focus && m.incident_id && !onFocus ? " other" : "") + (onFocus ? " mine" : "")
          + (fx && fx.stale ? " stale" : "") + (lit[0] ? " hl" : "") + (lit[1] ? " hot" : "") + (lit[2] ? " followed" : "");
        e.style.setProperty("--c", st.bg || "#9AA3AD");
        e.style.opacity = fx && fx.stale ? String(Math.round(fx.fade * 100) / 100) : "";
        e.replaceChildren(h("span", "n", m.unit));
        if (m.tag) e.append(h("span", "tag", m.tag));
        if (age) e.append(h("span", "age", age));
        e.title = [m.unit, st.name,
                   fx ? `${fx.stale ? "last fix" : "live"} ${agoS(fx.age)} ago${m.source ? ` (${m.source})` : ""}` : "",
                   m.place === "returning" ? `returning from ${m.returning_from} (est. position)` : "",
                   m.station ? `home ${m.station}` : (m.fire ? "no home station" : ""),
                   m.incident_number ? `on ${m.incident_number}` : "",
                   talk === "talking" ? "TX now" : talk ? "TX ended" : ""].filter(Boolean).join(", ");
        e.setAttribute("aria-label", e.title);
        rec.marker.setLngLat(this.where(m)).setOffset(off);
      }
      for (const [name, rec] of this.chips) if (!seen.has(name)) { rec.marker.remove(); this.chips.delete(name); }
    }

    drawLines(markers, lists) {
      const feats = new Map();
      for (const m of markers) {
        if (!m.line_to) continue;
        const k = JSON.stringify([m.lon, m.lat, m.line_to]);
        const color = m.home_line ? HOME_LINE : statusOf(lists, m.status).bg || LINE;
        if (!feats.has(k)) feats.set(k, { type: "Feature", properties: { color, home: !!m.home_line },
          geometry: { type: "LineString", coordinates: [[m.lon, m.lat], m.line_to] } });
      }
      const fc = { type: "FeatureCollection", features: [...feats.values()] };
      const sig = JSON.stringify(fc);
      if (sig === this.lineSig) return;
      this.lineSig = sig;
      const src = this.map.getSource("cm-lines");
      if (src) src.setData(fc);
    }

    drawUnset(unset) {
      const box = this.unsetBox;
      const sig = JSON.stringify([unset, this.unsetOpen, !!this.editable()]);
      if (box._sig === sig) return;
      box._sig = sig;
      box.hidden = !unset.length;
      if (!unset.length) return;
      const names = unset.map((u) => u.unit);
      const head = h("button", "cm-unset-head");
      head.type = "button";
      head.setAttribute("aria-expanded", String(this.unsetOpen));
      const shown = names.slice(0, 4).join(", ") + (names.length > 4 ? ` +${names.length - 4}` : "");
      head.append(h("b", "", "No station "), h("span", "", this.unsetOpen ? `${names.length} rigs` : shown),
                  h("span", "car", this.unsetOpen ? "▾" : "▸"));
      head.title = "Rigs with no home firehouse: not drawn until on a call";
      head.addEventListener("click", (e) => { e.stopPropagation(); this.unsetOpen = !this.unsetOpen; this.draw(); });
      box.replaceChildren(head);
      if (!this.unsetOpen) return;
      const list = h("div", "cm-unset-list");
      for (const u of unset) {
        const b = h("button", "cm-rig look-unset", u.unit);
        b.type = "button";
        b.title = (u.station ? `"${u.station}" not on the map. ` : "") + (u.on ? `On ${u.on}. ` : "")
          + (this.editable() ? "Set home station" : "");
        b.disabled = !this.editable();
        b.addEventListener("click", (e) => { e.stopPropagation(); this.openPicker(u.id, u.unit, ""); });
        list.append(b);
      }
      
      box.append(list);
    }

    editable() {             // opts.editable: true, or a function (cad.js canDo: only an admin sets a home station)
      const e = this.opts.editable;
      return typeof e === "function" ? !!e() : !!e;
    }

    // a rig clicked: followed (#358), and its home-station picker (an admin) or its box (its call one click away);
    // a map that follows nothing (the new call's): its call opened, as before
    chipClick(m) {
      if (this.onChip) this.onChip(m);
      if (this.editable() && m.fire && m.id != null) return this.openPicker(m.id, m.unit, m.station, m);
      if (this.onChip) return this.openInfo(m);
      if (m.incident_id && this.opts.onPick) this.opts.onPick(m.incident_id);
    }

    openInfo(m) {
      const st = statusOf(this.data && this.data.lists, m.status);
      const head = h("div", "cm-pop-head");
      head.append(h("b", "", m.unit));
      const line = h("div", "dim small", [st.name, m.incident_number ? `on ${m.incident_number}` : ""].filter(Boolean).join(" "));
      const row = h("div", "row");
      if (m.incident_id && this.opts.onPick && (!this.focus || this.focus.id !== m.incident_id)) {
        const go = h("button", "mini", `Open ${m.incident_number || "call"}`);
        go.type = "button";
        go.addEventListener("click", () => { this.closePicker(); this.opts.onPick(m.incident_id); });
        row.append(go);
      }
      const x = h("button", "mini quiet", "Close");
      x.type = "button";
      x.addEventListener("click", (e) => { e.stopPropagation(); this.closePicker(); });
      row.append(x);
      this.pop.replaceChildren(head, line, row);
      this.pop.hidden = false;
    }

    openPicker(id, name, current, m) {
      const stations = (this.data && this.data.map && this.data.map.stations) || [];
      const pop = this.pop;
      const sel = h("select");
      const none = h("option", "", "Not set");
      none.value = "";
      sel.append(none, ...stations.map((s) => { const o = h("option", "", s.name); o.value = s.name; return o; }));
      sel.value = current || "";
      const save = h("button", "primary", "Save");
      const cancel = h("button", "", "Cancel");
      const err = h("div", "error");
      save.type = cancel.type = "button";
      const head = h("div", "cm-pop-head");
      head.append(h("b", "", `${name} home station`));
      pop.replaceChildren(head);
      if (m) {
        const st = statusOf(this.data.lists, m.status);
        const line = h("div", "dim small", [st.name, m.incident_number ? `on ${m.incident_number}` : ""].filter(Boolean).join(" "));
        pop.append(line);
        if (m.incident_id && this.opts.onPick && (!this.focus || this.focus.id !== m.incident_id)) {
          const go = h("button", "mini", `Open ${m.incident_number || "call"}`);
          go.type = "button";
          go.addEventListener("click", () => { this.closePicker(); this.opts.onPick(m.incident_id); });
          line.append(" ", go);
        }
      }
      const row = h("div", "row");
      row.append(sel, save, cancel);
      pop.append(row, err);
      pop.hidden = false;
      sel.focus();
      cancel.addEventListener("click", () => this.closePicker());
      save.addEventListener("click", async () => {
        save.disabled = true;
        err.textContent = "";
        try {
          const r = await fetch(`/api/cad/unit/${id}/station`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ station: sel.value, operator: this.opts.operator ? this.opts.operator() : "" }),
          });
          const d = await r.json().catch(() => null);
          if (!r.ok) throw new Error((d && d.error) || `${r.status} ${r.statusText}`);
          this.closePicker();
        } catch (e) {
          err.textContent = e.message;
          save.disabled = false;
        }
      });
    }

    closePicker() {
      this.pop.hidden = true;
      this.pop.replaceChildren();
    }

    // Where a map with a call open should look: the call and the stations its rigs come from (live positions
    // too), the firehouses when it has none of those; points that can't be on the map left out. No call open: the
    // area (RigMap.framePoints, areaBounds).
    focusPoints() {
      const M = (this.data && this.data.map) || {};
      const f = this.focus, pts = [];
      if (!f) return pts;
      if (goodLL(f.lon, f.lat)) pts.push([f.lon, f.lat]);
      for (const m of M.markers || []) if (m.incident_id === f.id && m.place !== "incident" && goodLL(m.lon, m.lat)) pts.push([m.lon, m.lat]);
      if (!pts.length) for (const s of M.stations || []) pts.push(s.ll);
      return pts;
    }
  }

  // ---- RigMap: a map of its own with the layer, Recenter / Expand / Close and a note ---------------

  class RigMap {
    // opts: {scene, pins, unset, editable, operator, onPick, onExpand, onClose, manual, onRecenter}; manual: the owner
    // moves the camera (the new call's map, newcall-map.js: #204), update() never fits; Recenter asks onRecenter
    constructor(host, opts) {
      this.host = host;
      this.opts = opts;
      host.classList.add("cm-host");
      this.canvas = h("div", "cm-canvas");
      this.note = h("div", "cm-note");
      this.note.hidden = true;
      const ctrls = h("div", "cm-ctrls");
      const btn = (text, title, fn, icon) => {
        const b = h("button", "cm-btn sm");
        if (icon && window.Icon) b.append(Icon(icon));
        b.append(text);
        b.type = "button";
        b.title = title;
        b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
        ctrls.append(b);
        return b;
      };
      btn("Recenter", opts.manual ? "Back to the location" : "Back to the call and its rigs", () => {
        this.userMoved = false;
        this.map.fire("sizeup:recenter");             // an aircraft being followed (aircraft.js) is let go
        if (opts.onRecenter) opts.onRecenter(); else this.fit(true);
      }, "locate");
      if (opts.onExpand) btn("Expand", "Large map (Esc closes)", opts.onExpand, "expand");
      if (opts.onClose) btn("Close", "Esc", opts.onClose, "x");
      host.append(this.canvas, ctrls, this.note);
      const sc = opts.scene;
      this.map = new maplibregl.Map({
        container: this.canvas, style: buildMapStyle(sc), center: sc.home.center, zoom: sc.home.zoom,
        attributionControl: { compact: true }, maxPitch: 0, dragRotate: false, fadeDuration: 0,
      });
      this.map.touchZoomRotate.disableRotation();
      this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      this.map.on("load", () => {
        this.map.addImage("firehouse", firehouseIcon(), { pixelRatio: 2 });
        this.map.getSource("firehouses").setData({
          type: "FeatureCollection",
          features: (sc.firehouses || []).map((f) => ({ type: "Feature", properties: { name: f.name, address: f.address },
            geometry: { type: "Point", coordinates: f.ll } })),
        });
      });
      this.map.on("movestart", (e) => { if (e.originalEvent) this.userMoved = true; });
      this.map.on("sizeup:follow", () => { this.userMoved = true; });   // following an aircraft: no refit until Recenter
      this.layer = new RigLayer(this.map, { ...opts, host });
      // following a rig (#358, mapfollow.js): not on the new call's map, which its owner moves
      this.fol = !opts.manual && window.MapFollow
        ? new MapFollow.Follower(this.map, host, { cls: "ac-follow cm-follow", textCls: "ac-follow-t", icon: "locate",
                                                   onStop: () => this.layer.draw() })
        : null;
      this.layer.fol = this.fol;
      if (this.fol) this.layer.onChip = (m) => this.followUnit(m.unit);
      rigMaps.add(this);
      // Cameras, incidents, roadwork, signs, mileposts, exits and their Layers button (maplayers.js, #73)
      this.roads = window.MapLayers ? MapLayers.attach(this.map, host, ctrls, { layers: opts.layers }) : null;   // its set: "overview" | "incident" (#111)
      this.focusId = undefined;
      this.fitKey = null;
      this.userMoved = false;
      this.size = "";
    }

    update(data, focus, note) {
      const size = `${this.canvas.clientWidth}x${this.canvas.clientHeight}`;
      if (size !== this.size) {                       // shown, or the box changed size
        this.size = size;
        this.map.resize();
        if (this.canvas.clientWidth) this.fitKey = null;
      }
      this.layer.update(data, focus);
      this.focus = focus || null;
      if (this.roads && this.roads.focus) this.roads.focus(this.focus);     // an event at the airport: its layer on (#87)
      this.note.textContent = note || "";
      this.note.hidden = !note;
      const fid = focus ? focus.id : null;
      if (fid !== this.focusId) {                     // another call (or none): look at it whatever was done before
        this.focusId = fid;
        this.userMoved = false;
        if (this.fitKey !== null) this.map.fire("sizeup:recenter");   // framed again: a followed aircraft let go
        this.fitKey = null;
        this.layer.closePicker();
        this.clearPlace();
      }
      const key = JSON.stringify(this.framePoints());
      if (!this.focus) {                              // no call open: the calls changed, so all of them again, as
        if (this.callsKey !== undefined && key !== this.callsKey) this.userMoved = false;   // Recenter does (#218)
        this.callsKey = key;                          // (a view switch or a resize leaves a map the viewer moved)
      } else this.callsKey = undefined;
      if (key !== this.fitKey && this.canvas.clientWidth) {
        const first = this.fitKey === null;
        this.fitKey = key;
        if (!this.userMoved && !this.opts.manual) this.fit(!first);
      }
    }

    // a rig followed (centred, zoomed in, kept centred; ringed for a while) or only picked (centred, ringed)
    followUnit(name) {
      const rec = this.fol && this.layer.chips.get(name);
      if (!rec) return false;
      const ll = rec.marker.getLngLat();
      this.fol.start(name, { lon: ll.lng, lat: ll.lat }, UNIT_ZOOM);
      if (lights) lights.pick(name);
      this.layer.draw();
      return true;
    }
    focusUnit(name) {
      const rec = this.layer.chips.get(name);
      if (!rec) return false;
      if (this.fol && this.fol.id && this.fol.id !== name) this.fol.stop();
      const ll = rec.marker.getLngLat();
      this.userMoved = true;                           // stay on it until Recenter
      try { this.map.easeTo({ center: [ll.lng, ll.lat], zoom: Math.max(this.map.getZoom(), UNIT_ZOOM), duration: 600 }); } catch (_) { /* */ }
      if (lights) lights.pick(name);
      this.layer.draw();
      return true;
    }

    framePoints() {                  // what the frame is made of: the call's points, or the open calls in our area
      if (this.focus) return this.layer.focusPoints();
      return areaCalls(this.opts.scene.home, ((this.layer.data && this.layer.data.map) || {}).incidents);
    }

    // A place named in a note ("@Lyon Park", #67): a pin, the map moved to it, its name in a box at the
    // top (where the station picker opens). onSet (a dispatcher, an open call): "Set as location" makes it
    // the event's location. One at a time; another call, or Close, takes it away.
    showPlace(p, onSet) {
      this.clearPlace();
      const pin = h("div", "cm-place");
      pin.append(h("span", "dot"));
      pin.title = p.label;
      const box = h("div", "cm-pop cm-place-pop");
      const head = h("div", "row");
      head.append(h("b", "", p.label));
      if (p.sub) head.append(h("span", "town", p.sub));
      const btns = h("div", "row");
      if (onSet) {
        const b = h("button", "mini", "Set as location");
        b.type = "button";
        b.title = "Make this the event's location (in the chronology)";
        b.addEventListener("click", async (e) => { e.stopPropagation(); if (await onSet()) this.clearPlace(); });
        btns.append(b);
      }
      const x = h("button", "mini quiet", "Close");
      x.type = "button";
      x.addEventListener("click", (e) => { e.stopPropagation(); this.clearPlace(); });
      btns.append(x);
      box.append(head, h("div", "dim small", "Named in a note: the event stays where it is"), btns);
      box.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); this.clearPlace(); } });
      this.host.append(box);
      this.place = [new maplibregl.Marker({ element: pin, anchor: "center" }).setLngLat([p.lon, p.lat]).addTo(this.map), box];
      this.userMoved = true;                           // stay on the place until Recenter
      this.map.easeTo({ center: [p.lon, p.lat], zoom: Math.max(this.map.getZoom(), 15.5), duration: 500 });
    }

    clearPlace() {
      if (this.place) { this.place[0].remove(); this.place[1].remove(); }
      this.place = null;
    }

    fit(animate) {
      const pts = this.layer.focusPoints();
      const sc = this.opts.scene;
      const w = this.canvas.clientWidth, ht = this.canvas.clientHeight;
      if (!w || !ht) return;
      const dur = animate ? 600 : 0;
      if (!this.focus) {                               // no call open: our area and the open calls in it (#218)
        const area = areaBounds(sc.home, ((this.layer.data && this.layer.data.map) || {}).incidents);
        if (!area) return this.map.jumpTo({ center: sc.home.center, zoom: sc.home.zoom });
        const p = Math.max(8, Math.min(40, Math.floor(Math.min(w, ht) / 10)));     // the buttons above, a pin's label below
        return this.map.fitBounds(area, { padding: { top: Math.min(p + 34, Math.floor(ht / 3)), bottom: Math.min(p + 28, Math.floor(ht / 3)),
                                                     left: p, right: p }, maxZoom: FOCUS_MAX_ZOOM, duration: dur });
      }
      if (!pts.length) return this.map.jumpTo({ center: sc.home.center, zoom: sc.home.zoom });
      const b = new maplibregl.LngLatBounds(pts[0], pts[0]);
      pts.forEach((p) => b.extend(p));
      const field = window.AirportLayer ? AirportLayer.focusBounds(this.focus) : null;   // at the airport: the whole field (#87)
      if (field) { b.extend(field[0]); b.extend(field[1]); }
      const pad = Math.max(10, Math.min(90, Math.floor(Math.min(w, ht) / 3.5)));   // room for the chips around the edge
      if (!field && (pts.length === 1 || (b.getEast() - b.getWest() < 1e-4 && b.getNorth() - b.getSouth() < 1e-4))) {
        return this.map.easeTo({ center: b.getCenter(), zoom: 16, duration: dur });
      }
      const top = Math.min(pad + 34, Math.floor(ht / 2.5));        // below the Recenter / Expand buttons
      this.map.fitBounds(b, { padding: { top, bottom: pad, left: pad, right: pad }, maxZoom: FOCUS_MAX_ZOOM, duration: dur });
    }
  }

  // ---- the CAD console's maps (uses cad.js: cad, $, el, show, setText, selectIncident, ...) ----------

  const views = { detail: null, big: null, overview: null, bigOpen: false };
  const mapData = () => ({ map: cad.state && cad.state.map, lists: cad.state && cad.state.lists });
  const mapsOk = () => !!(window.maplibregl && cad.scene && cad.scene.home);
  const noPoint = (inc) => (inc && (inc.lat == null || inc.lon == null) ? "No map point" : "");

  function detail(inc) {
    const box = $("#d-map"), nomap = $("#d-nomap");
    if (window.TrainLayer) TrainLayer.panel(inc, $("#d-trains"));      // "Trains approaching" near the tracks (#131)
    if (!mapsOk()) {
      show(box, false);
      show(nomap, true);
      setText(nomap, "No map");
      if (window.MapLayers) MapLayers.cameras(inc, $("#d-cams"));
      return;
    }
    show(nomap, false);
    show(box, true);
    if (window.MapLayers) MapLayers.cameras(inc, $("#d-cams"));          // the Cameras panel under the map (#73, #95)
    if (!views.detail) {
      views.detail = new RigMap(box, { scene: cad.scene, pins: true, unset: true, editable: () => canDo("admin"), operator,
                                       onPick: (id) => selectIncident(id), onExpand: openBig, layers: "incident" });
    }
    views.detail.update(mapData(), inc, noPoint(inc));
    if (views.bigOpen) views.big.update(mapData(), inc, noPoint(inc));
  }

  function place(p, onSet) {           // a note's "@place" on the working event's map (cad.js showPlace)
    if (!views.detail || !mapsOk() || $("#d-map").hidden) return false;
    views.detail.showPlace(p, onSet);
    return true;
  }

  function openBig() {
    let ov = $("#cm-overlay");
    if (!ov) {
      ov = el("div", "overlay cm-overlay");
      ov.id = "cm-overlay";
      const panel = el("div", "cm-big");
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-label", "Map");
      ov.append(panel);
      document.body.append(ov);
      ov.hidden = false;
      views.big = new RigMap(panel, { scene: cad.scene, pins: true, unset: true, editable: () => canDo("admin"), operator,
                                      onPick: (id) => { closeBig(); selectIncident(id); }, onClose: closeBig, layers: "overview" });
    }
    UI.dialog.open(ov, { onClose: () => { views.bigOpen = false; } });   // focus in, Esc / the scrim close (ui.js)
    views.bigOpen = true;
    const inc = cad.mode === "view" ? selected() : null;
    views.big.size = "";
    views.big.focusId = undefined;                     // look at the call again
    views.big.update(mapData(), inc, noPoint(inc));
  }
  function closeBig() {
    const ov = $("#cm-overlay");
    if (ov) UI.dialog.close(ov);
    views.bigOpen = false;
  }

  // ---- Overview: every open call at once ---------------------------------------------------------
  // The console's Overview is the map (or the cameras) beside the queue, which lists the calls (#217: its own cards
  // repeated them); the wall board, with no queue, keeps a card per call and the Non-emergency list (.ov-side).

  const ACTIVE_FIRST = { active: 0, pending: 1, queued: 2 };

  function cardFill(li, o) {
    const live = callLive(o);
    const color = typeColor(o.type) || "#9AA3AD";
    li.className = `ov-card item ${o.status}` + (live.length ? " live" : "");
    li.style.setProperty("--c", color);
    const head = el("div", "ov-h");
    const es = eventStatus(o);
    head.append(el("b", "ov-n", incTitle(o)), stBadge(el("span"), es.name, es.bg));
    if (o.joint_call) head.append(el("span", "chip joint", o.joint_call));
    head.append(el("span", "spacer"), since(el("span", "ov-el", ""), o.created_at));
    const type = el("div", "ov-type", typeLabel(o.type, o.subtype) || "No type");
    const addr = el("div", "ov-addr" + (o.address ? "" : " dim"), o.address || "No location");
    li.replaceChildren(head, type, addr);
    const ap = o.type === "AIRPORT" ? AirportLayer.summary(o, o.subtype ? nameOf("event_subtypes", o.subtype) : "") : "";
    if (ap) li.append(el("div", "ov-airport", ap));       // "Alert 2 · B737 · unsafe gear · 12 min · RWY 16 · 140 souls (Level 5)" (#105)
    const chips = el("div", "chips ov-units");
    if (o.units.length) {
      for (const u of o.units) {
        const c = statusChip(u.status, `${u.name} ${u.status}`);
        const t = Radio.talkingFor(u.name);
        if (t && !t.ended) c.classList.add("talking");
        chips.append(c);
      }
    } else chips.append(el("span", "dim small", o.status === "queued" ? "Page: accept" : "No units"));
    li.append(chips);
    if (live.length) {
      const l = el("div", "ov-live");
      l.append(el("b", "", "TX "),
               el("span", "", live.map((e) => `${Radio.who(e) || "Radio"} ${e.channel}`).join(", ")));
      li.append(l);
    }
    if (o.radio) {
      const r = el("div", "ov-radio");
      const who = o.radio.rig || o.radio.talker || (o.radio.dispatcher ? "60 Control" : "radio");
      r.append(el("span", "t", fmtTime(o.radio.start)), el("b", "", who), el("span", "dim", ` ${o.radio.channel}`));
      if (o.radio.text) r.append(el("span", "q", ` “${o.radio.text}”`));
      else r.append(el("span", "dim", " —"));
      li.append(r);
    }
    if (o.note) {
      const n = el("div", "ov-note");
      n.append(el("span", "t", fmtTime(o.note.at)), el("span", "k", o.note.kind === "note" ? "note" : o.note.kind),
               el("span", "", o.note.text));
      li.append(n);
    }
  }

  function overview(on) {
    const box = $("#overview");
    show(box, on);
    if (on) show($("#d-empty"), false);             // the overview is the landing view (and all of the wall board)
    if (!on || !cad.state) return;
    const ov = cad.state.overview || [];
    const emerg = ov.filter((o) => o.emergency && o.status !== "queued")
      .sort((a, b) => (ACTIVE_FIRST[a.status] - ACTIVE_FIRST[b.status]) || (a.created_at - b.created_at));
    const pages = ov.filter((o) => o.status === "queued");
    const ne = ov.filter((o) => !o.emergency && o.status !== "queued");
    const n = (k) => emerg.filter((o) => o.status === k).length;
    const counts = [["Active", n("active")], ["Pending", n("pending")], ["Non-emerg", ne.length], ...(pages.length ? [["Pages", pages.length]] : [])];
    const csig = JSON.stringify(counts), cbox = $("#ov-counts");
    if (cbox._sig !== csig) { cbox._sig = csig; cbox.replaceChildren(...counts.map(([k, v]) => el("span", "", `${k} ${v}`))); }
    // talking now, with the call each one belongs to
    const talk = ((cad.state.talking || []).filter((e) => Radio.shown(e))).slice(0, 8);
    const tbox = $("#ov-talk");
    const tsig = JSON.stringify(talk.map((e) => [e.key, e.rig, e.ended, e.channel, e.alias, e.id_on_release]).concat(ov.map((o) => [o.id, o.units.length])));
    if (tbox._sig !== tsig) {
      tbox._sig = tsig;
      tbox.replaceChildren(el("span", "dim small", talk.length ? "TX" : "No TX"));
      for (const e of talk) {
        const c = el("span", Radio.chipClass(e));
        c.append(el("span", "", Radio.label(e)));
        if (e.rig) { c.dataset.rig = e.rig.split(/,\s*/)[0]; c.title = "Click: show on map"; }   // #358: centred, ringed
        const on = ov.filter((o) => callLive(o).includes(e) || (e.rig && o.units.some((u) => e.rig.split(/,\s*/).includes(u.name))));
        if (on.length) c.append(el("span", "on", on.map((o) => incTitle(o)).join(", ")));
        tbox.append(c);
      }
    }
    // pages from 60 Control
    const pbox = $("#ov-pages");
    show(pbox, !!pages.length);
    syncList(pbox, pages, {
      key: (o) => String(o.id), sig: (o) => JSON.stringify([o.type, o.subtype, o.address]),
      make: () => { const b = el("button", "ov-page"); b.type = "button"; b.addEventListener("click", () => selectIncident(Number(b.dataset.key))); return b; },
      fill: (b, o) => b.replaceChildren(el("b", "", "60 Control "), el("span", "", `${typeLabel(o.type, o.subtype) || "No type"}, ${o.address || "No location"}`),
                                        el("span", "dim", " Accept / Dismiss")),
      empty: "",
    });
    if (BOARD) syncList($("#ov-cards"), emerg, {
      key: (o) => String(o.id),
      sig: (o) => JSON.stringify([o, callLive(o).map((e) => [e.key, e.rig]), o.units.map((u) => talkState(u.name)), cad.listSig.length]),
      make: () => {
        const li = el("li", "ov-card item");
        li.tabIndex = 0;
        li.setAttribute("role", "button");
        li.addEventListener("click", () => selectIncident(Number(li.dataset.key)));
        li.addEventListener("keydown", (e) => { if (e.key === "Enter") selectIncident(Number(li.dataset.key)); });
        return li;
      },
      fill: cardFill,
      empty: "None",
    });
    if (BOARD) syncList($("#ov-nonemerg"), ne, {
      key: (o) => String(o.id), sig: (o) => JSON.stringify([o.number, o.type, o.subtype, o.address, o.units.map((u) => u.name)]),
      make: () => {
        const li = el("li", "ov-ne item");
        li.tabIndex = 0;
        li.addEventListener("click", () => selectIncident(Number(li.dataset.key)));
        li.addEventListener("keydown", (e) => { if (e.key === "Enter") selectIncident(Number(li.dataset.key)); });
        return li;
      },
      fill: (li, o) => {
        const t = el("span", "qt", typeLabel(o.type, o.subtype));
        t.style.setProperty("--c", typeColor(o.type) || "#8fb8d8");
        li.replaceChildren(el("b", "", incTitle(o)), t, el("span", "ov-addr-s", o.address || "No location"),
                           el("span", "spacer"), since(el("span", "dim small"), o.created_at));
        if (o.units.length) li.append(el("span", "dim small", o.units.map((u) => u.name).join(", ")));
      },
      empty: "None",
    });
    if (BOARD && window.MapLayers) {        // the wall board: the active highway event's two cameras, small (#95)
      const act = emerg.find((o) => o.status === "active" && o.lat != null) || emerg.find((o) => o.lat != null) || null;
      MapLayers.cameras(act, $("#ov-cams"), { small: true });
    }
    if (mapsOk()) {
      if (!views.overview) {
        views.overview = new RigMap($("#ov-map"), { scene: cad.scene, pins: true, unset: !BOARD, editable: () => canDo("admin") && !BOARD, operator,
                                                    onPick: (id) => selectIncident(id), layers: "overview" });
      }
      views.overview.update(mapData(), null, "");
    } else setText($("#ov-map"), "No map");
  }

  function goOverview() {
    cad.mode = "none";
    cad.sel = null;
    cad.rec = null;
    render();
    if (window.innerWidth <= 1100) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  if (document.getElementById("overview") && typeof cad !== "undefined") {    // the CAD console (loaded after cad.js)
    cad.hooks.push(() => overview(cad.mode === "none" && !cad.radio));
    $("#overview-open").addEventListener("click", () => {
      if (location.hash) history.pushState(null, "", location.pathname);     // out of Settings / Radio
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      goOverview();
    });
    $("#d-back").addEventListener("click", goOverview);
  }

  const detailMap = () => views.detail;   // the working event's map, for its resize handles (incident-map.js, #210)
  return { RigLayer, RigMap, detail, detailMap, place, overview, goOverview, openBig, closeBig, areaBounds, areaCalls, AREA_KM,
           followUnit, focusUnit, hoverUnits, onMap, lights };
})();
