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
// The probable path (#360): a rig going to a call with a route from the hub (state.map.routes, sizeup/cad/routes.py: by
// road, from its fix or its station, re-routed there as it moves) is drawn dashed in its status colour along that route
// from where its mark is drawn now (the glided mark), instead of the straight line; the follow chip adds "~3 min ·
// 1.2 mi". A followed rig with fixes also shows its faint trail (state.map.trails). No route (no graph yet, no road
// near): the straight line, as before. Redrawn with the call line, on the frames that move a mark: nothing here keeps
// the frame loop running.
//
// Decluttered by zoom (#376; the zooms: state.map.display, Settings > System > Map display, mapview.py DISPLAY_DEFAULTS):
//
//   idle (available, in quarters, returning)   below detail_zoom a dot; two or more idle at one firehouse: one count
//                                              chip on top of its icon, "6" (#384: its tooltip lists them; the
//                                              style's "firehouse-badge" mark keeps the firehouse's name clear of it),
//                                              the firehouse's other marks over the top
//   active (on a call: dispatched, responding, on location, staging ...)   full chips down to active_zoom (never
//                                              above detail_zoom nor ACTIVE_MAX), drawn over the rest; below it a
//                                              strong dot (a stale fix's dot not faded); on the map's automatic frame
//                                              (the area or the call, not moved by hand: RigMap.userMoved), and for
//                                              FRESH_S after its status changed (newly dispatched ...), a full chip
//   out of service                             below detail_zoom the smallest, faintest dot
//   followed, picked, transmitting, its row hovered   always the full chip; a dot under the pointer opens to its chip
//
// The wall board has its own pair (board_*). Laid out again only on zoomend (the band crossed) and on a new state,
// never on the frames: tick() moves marks, it never re-sizes or re-fans them. Markers are kept and re-used: a rig in a
// badge keeps its marker, hidden (folded), so a Follow or a pick still finds it.
//
// A firehouse clicked (#384: its icon, its name or its count chip): its rigs in the map's box, active first, each with
// its type icon and status; a click on one follows it (#358), or rings it where the map follows nothing (the wall
// board); Zoom to: the firehouse at detail_zoom, its rigs drawn one by one.
//
// Rigs by apparatus type (#384; the hub's marker "icon", mapview.icon_of): engine, ladder, tower (aerial platform),
// rescue, suv (chiefs', duty and utility cars), other. One monochrome silhouette each (cadmap.css .cm-ty, a CSS mask
// defined once), in the chip's text colour on its status colour; in the station list, in the status colour. Not in the
// dots (8-12 px: too small to read); a dot under the pointer opens to its chip, icon and all.
//
// Highway layers (cameras, incidents, signs, mileposts, exits) and their Layers button: maplayers.js,
// attached to every RigMap; the event's "Cameras nearby" row from detail().
//
// RigLayer and RigMap use nothing from cad.js; the CAD glue at the
// bottom (CadMap.detail, CadMap.overview) uses cad.js's helpers and state.
"use strict";

window.CadMap = (() => {
  const STATION_R = 34, INC_R = 30, RING_GAP = 24, CHIP_W = 62;   // px: the fan-out around an anchor (#384: icon + code)
  const DOT_R = 16, DOT_W = 14;        // px: the fan-out of dots only (#376)
  const BADGE_AT = [0, -13];           // px: a firehouse's count chip on top of its icon (#384; map-style.js reserves it)
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

  // ---- decluttered by zoom (#376) ----------------------------------------------------------------------------------
  const ZOOMS = { detail_zoom: 15, active_zoom: 10, board_detail_zoom: 16, board_active_zoom: 10 };   // mapview.py's
  const ACTIVE_MAX = 12;               // mapview.ACTIVE_MAX: an active rig full from this zoom at the latest
  const FRESH_S = 60;                  // s: a rig just made active (dispatched, responding, on location) full this long
  // by the hub's look (mapview.look_of): idle "available", out of service "oos", every other look (dispatched,
  // responding, onscene: committed to a call) active: drawn over the rest, chips longest
  const rigClass = (m) => (m.look === "available" ? "idle" : m.look === "oos" ? "oos" : "act");
  // {detail, active}: this map's zooms (the wall board's own pair); active never above detail
  function zoomsOf(M, board) {
    const d = (M && M.display) || {}, p = board ? "board_" : "";
    const z = (k) => (typeof d[p + k] === "number" && Number.isFinite(d[p + k]) ? d[p + k] : ZOOMS[p + k]);
    const detail = z("detail_zoom");
    return { detail, active: Math.min(z("active_zoom"), detail, ACTIVE_MAX) };
  }
  // the band a zoom is in: {detail: chips for all, active: chips for the active}; auto: the map on its automatic frame
  // (the area or the call, not moved by hand): the active rigs full at any zoom
  const bandOf = (zoom, Z, auto) => ({ detail: zoom >= Z.detail, active: !!auto || zoom >= Z.active });
  // Each marker's size in this band: "full" | "dot" | "folded" (in its firehouse's badge), and the badges (station name
  // -> its idle markers, two or more). lit(m): followed, picked, transmitting or its row hovered: always full.
  function declutter(markers, band, lit) {
    const size = new Map(), idle = new Map(), badges = new Map();
    for (const m of markers) {
      const c = rigClass(m), full = !!(lit && lit(m)) || (c === "act" ? band.active : band.detail);
      size.set(m.unit, full ? "full" : "dot");
      if (!full && c === "idle" && m.place === "station" && m.station) {
        if (!idle.has(m.station)) idle.set(m.station, []);
        idle.get(m.station).push(m);
      }
    }
    for (const [st, list] of idle) {
      if (list.length < 2) continue;
      badges.set(st, list);
      for (const m of list) size.set(m.unit, "folded");
    }
    return { size, badges };
  }
  // s left of an active rig's FRESH_S since its status changed (0: not fresh); the hub's status_at, on the hub's clock
  const freshLeft = (m) => (rigClass(m) === "act" && typeof m.status_at === "number"
    ? Math.max(0, FRESH_S - Math.max(0, nowS() - m.status_at)) : 0);
  // a rig's icon by apparatus type (#384): the hub's (mapview.icon_of), else the generic mark
  const ICONS = new Set(["engine", "ladder", "tower", "rescue", "suv", "other"]);
  const iconOf = (m) => (m && ICONS.has(m.icon) ? m.icon : "other");
  // a department placeholder (DPT239 ...: mapview.is_placeholder), not apparatus: the hub never sends one; if one comes,
  // it is never drawn, counted, listed or followed
  const placeholder = (m) => /^DPT/i.test(String((m && m.unit) || "").trim());
  const drawable = (M) => ((M && M.markers) || []).filter((m) => !placeholder(m));
  const ICON_NAMES = { engine: "engine", ladder: "ladder", tower: "tower ladder", rescue: "rescue", suv: "car", other: "" };
  const CLASS_ORDER = { act: 0, idle: 1, oos: 2 };
  // a firehouse's list's head (#384): "11 rigs · 7 in quarters · 2 on calls · 2 OOS", the parts that are not 0
  function stationCounts(rigs) {
    if (!rigs.length) return "No rigs";
    const n = (c) => rigs.filter((m) => rigClass(m) === c).length;
    return [`${rigs.length} rig${rigs.length === 1 ? "" : "s"}`, n("idle") && `${n("idle")} in quarters`,
            n("act") && `${n("act")} on call${n("act") === 1 ? "" : "s"}`, n("oos") && `${n("oos")} OOS`].filter(Boolean).join(" · ");
  }

  // ---- our units followed and lit up (#358): one state for every map ----------------------------------------------
  const UNIT_ZOOM = 15;                // a rig followed or picked: centred at least this close
  const KT_MS = 1852 / 3600;           // m/s in a knot (glide.js reads kt; the ingest shape's speed is m/s)
  const STALE_S = 60, MAX_AGE_S = 300, HORIZON_S = 8;   // an older hub: mapview.py's POSITION_STALE_S / _MAX_AGE_S / _HORIZON_S
  const MIN_MS = 2;                    // slower than this a rig is stopped: drawn at its fix, never drawn on
  const HOLD_M = 30;                   // a stopped rig's fix this close to where it is drawn: held there (never eased back)
  const LINE_MS = 250;                 // the lines from moving marks (to the call, the routes) re-set at most ~4 Hz
  const FRAME_MS = 50, MAX_FRAME_MS = 250, STEP_PX = 0.75;   // as aircraft.js: redrawn as often as the fastest moves STEP_PX
  const AGE_MS = 1000;                 // the chips' ages and the follow chip's words: once a second (a timer, not frames)
  const rigMaps = new Set(), rigLayers = new Set();
  const lights = window.MapFollow ? new MapFollow.Lights(() => redrawRigs()) : null;
  const skew = () => (typeof cad !== "undefined" && cad && cad.skew) || 0;
  const nowS = () => Date.now() / 1000 + skew();
  const agoS = (s) => (window.MapFollow ? MapFollow.ago(s * 1000) : `${Math.round(s)} s`);
  function redrawRigs() { for (const l of rigLayers) l.draw(); }
  const avlOf = (M) => ({ stale_s: (M && M.avl && M.avl.stale_s) || STALE_S, max_age_s: (M && M.avl && M.avl.max_age_s) || MAX_AGE_S,
                          horizon_s: (M && M.avl && M.avl.horizon_s) || HORIZON_S });
  // a live fix: how old (s), stale, and how faded (full until stale, down to 0.35 by the time the hub drops it)
  function fixAge(m, M) {
    if (m.place !== "position" || typeof m.at !== "number") return null;
    const a = avlOf(M), age = Math.max(0, nowS() - m.at), stale = age > a.stale_s;
    return { age, stale, fade: stale ? Math.max(0.35, 1 - 0.65 * (age - a.stale_s) / Math.max(1, a.max_age_s - a.stale_s)) : 1 };
  }
  // A rig between fixes (not Motion.arc: that is aircraft's, still below 30 kt): on in a straight line along its heading
  // at the fix's speed; slower than MIN_MS, or no heading: stopped, at its fix. glide.js holds it at horizon_s.
  function straight(fix, dt) {
    const v = (fix.gs || 0) * KT_MS;
    if (!(v >= MIN_MS) || typeof fix.trk !== "number") return null;
    const d = v * dt, h = fix.trk * Math.PI / 180, R = 6371008.8;
    return { lon: fix.lon + (d * Math.sin(h)) / (R * Math.cos(fix.lat * Math.PI / 180)) * 180 / Math.PI,
             lat: fix.lat + (d * Math.cos(h)) / R * 180 / Math.PI, rot: fix.trk };
  }
  // SNAP_S 10: a miss up to 10 s of its travel is closed by slowing or speeding it, not jumped (a rig's fixes are sparse)
  const glide = window.Glide ? Glide.follower(straight, { horizonS: HORIZON_S, staleS: STALE_S, SNAP_S: 10 }) : null;
  const drawn = new Map();             // unit -> [lon, lat] where its live fix was last drawn (held when stale or stopped)
  let fastest = 0;                     // m/s, the fastest moving rig (as it may be drawn catching up: x1.5)
  let fresh = 0;                       // frames still owed after a new state (a fix set this millisecond reads as settled)
  // each live fix handed to the follower once a state (the fix as it was when it was true, on this browser's clock)
  let fedFor = null;
  function feedGlide(M) {
    if (!glide || !M || M === fedFor) return;
    fedFor = M;
    const a = avlOf(M);
    glide.staleS = a.stale_s;
    glide.horizonS = a.horizon_s;
    const keep = new Set();
    fastest = 0;
    for (const m of M.markers || []) {
      if (m.place !== "position" || typeof m.at !== "number") continue;
      keep.add(m.unit);
      const hd = typeof m.heading === "number" ? m.heading : null, v = typeof m.speed === "number" ? m.speed : 0;
      if (v >= MIN_MS) fastest = Math.max(fastest, v * 1.5);
      glide.set(m.unit, { lon: m.lon, lat: m.lat, rot: hd || 0, at: (m.at - skew()) * 1000, trk: hd, gs: v / KT_MS });
    }
    glide.forget(keep);
    for (const k of [...drawn.keys()]) if (!keep.has(k)) drawn.delete(k);
    fresh = 2;
  }
  // where a live fix's rig is drawn now: drawn on by the follower; never stepped back for going stale (held where it
  // was drawn, hollow) or for a stopped fix just behind where it was drawn (held, < HOLD_M)
  function fixWhere(m, M) {
    const fx = fixAge(m, M), last = drawn.get(m.unit);
    const g = glide ? glide.where(m.unit, Date.now()) : null;
    let at = g ? [g.lon, g.lat] : [m.lon, m.lat];
    if (last && fx && fx.stale) at = last;
    else if (last && !((m.speed || 0) >= MIN_MS) && Glide.metres({ lon: last[0], lat: last[1] }, { lon: m.lon, lat: m.lat }) < HOLD_M) at = last;
    drawn.set(m.unit, at);
    return at;
  }
  // Frames only while something moves on a map on screen: a rig drawn on or catching up (glide.busy: parked and stale
  // ones settle and stop), a rig followed, or the frames owed after a new state; each map redrawn once its fastest rig
  // has moved STEP_PX there. The ages tick on a one-second timer while any live fix is drawn.
  let raf = 0, ageTimer = 0;
  const visible = () => typeof document === "undefined" || document.visibilityState !== "hidden";
  const wanted = () => visible() && [...rigLayers].some((l) => l.moving());
  function animate() {
    if (!raf && typeof requestAnimationFrame === "function" && wanted()) raf = requestAnimationFrame(frame);
    if (fixesShown() && !ageTimer) ageTimer = setInterval(ageTick, AGE_MS);
    else if (!fixesShown() && ageTimer) { clearInterval(ageTimer); ageTimer = 0; }
  }
  const fixesShown = () => visible() && [...rigLayers].some((l) => l.hasFix && l.shown());
  // the ages, once a second; no live fix on a map on screen any more (the Radio or Settings view): the timer stops itself
  function ageTick() {
    if (!fixesShown()) { clearInterval(ageTimer); ageTimer = 0; return; }
    for (const l of rigLayers) if (l.hasFix && l.shown()) l.draw();
  }
  function frame(ts) {
    raf = 0;
    if (!wanted()) return;
    for (const l of rigLayers) {
      if (l.moving() && ts - (l.tickedAt || 0) >= l.frameMs() - 2) { l.tickedAt = ts; l.tick(); }
    }
    if (fresh > 0) fresh -= 1;
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
                             && drawable(cad.state.map).some((m) => m.unit === name));
  // the map to show a rig on: the large one if open, else the first on screen that follows (never the new call's or
  // the wall board's)
  function shownRigMap() {
    if (views.bigOpen && views.big) return views.big;
    return window.MapFollow ? MapFollow.shownMap([...rigMaps].filter((r) => r.fol)) : null;
  }
  // A row's Follow or a talker chip: only on a map already on screen that shows the rig. Never a view switch (a new
  // call's draft, an edit form, an incident with no map shown stay as they are): false, and the caller flashes its row.
  function pickUnit(name, follow) {
    if (!name || !onMap(name)) return false;
    const rm = shownRigMap();
    if (!rm || !rm.layer.chips.has(name)) return false;
    return follow ? rm.followUnit(name) : rm.focusUnit(name);
  }
  const followUnit = (name) => pickUnit(name, true);     // a unit-board or incident row's Follow
  const focusUnit = (name) => pickUnit(name, false);     // a talker chip: centred and ringed, not followed
  function hoverUnits(names) { if (lights && lights.hover(names)) redrawRigs(); }   // a row hovered: its rig ringed wider

  // where a rig's mark is drawn now, for the lines from it (the call line, its route): a live fix where it was last
  // drawn on (fixWhere, on the frames), else where the hub put it
  const markAt = (m) => (m.place === "position" ? (drawn.get(m.unit) || [m.lon, m.lat]) : [m.lon, m.lat]);
  // ---- the probable path (#360) ----------------------------------------------------------------------------------
  const routeOf = (M, m) => ((M && M.routes) || []).find((r) => r.unit === m.unit && r.incident_id === m.incident_id) || null;
  // the route's points from the mark on: [mark, ...the rest]. The nearest segment to the mark, looked for near where the
  // hub last had the rig along it (along_m: from a little behind to ALONG_AHEAD_M ahead, as the mark is drawn on
  // between fixes), a tie going to the EARLIER segment: a route that doubles back on a street keeps its first leg.
  const ALONG_BACK_M = 100, ALONG_AHEAD_M = 1000;
  function routeFrom(coords, at, alongM) {
    if (!Array.isArray(coords) || coords.length < 2) return null;
    const kx = Math.cos((at[1] * Math.PI) / 180), M_DEG = 111320;
    const near = (lo, hi) => {
      let best = Infinity, bi = -1, run = 0;
      for (let i = 0; i < coords.length - 1; i++) {
        const [x1, y1] = coords[i], [x2, y2] = coords[i + 1];
        const dx = (x2 - x1) * kx, dy = y2 - y1, ll = dx * dx + dy * dy, seg = Math.sqrt(ll) * M_DEG;
        if (run + seg >= lo && run <= hi) {
          const t = ll ? Math.max(0, Math.min(1, (((at[0] - x1) * kx) * dx + (at[1] - y1) * dy) / ll)) : 0;
          const ex = (x1 + (x2 - x1) * t - at[0]) * kx, ey = y1 + (y2 - y1) * t - at[1], d = ex * ex + ey * ey;
          if (d < best) { best = d; bi = i; }
        }
        run += seg;
      }
      return bi;
    };
    let bi = typeof alongM === "number" ? near(alongM - ALONG_BACK_M, alongM + ALONG_AHEAD_M) : -1;
    if (bi < 0) bi = near(-Infinity, Infinity);
    const rest = coords.slice(bi + 1);
    if (rest.length > 1 && Math.abs(rest[0][0] - at[0]) < 1e-9 && Math.abs(rest[0][1] - at[1]) < 1e-9) rest.shift();
    return [at, ...rest];
  }

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
      this.badges = new Map();         // station name -> its badge (#376): {marker, el, sig}
      this.band = null;                // the zoom band the rigs are laid out for (#376): set on zoomend and a new state
      this.auto = () => false;         // RigMap: on its automatic frame (not moved by hand)
      this.data = null;
      this.focus = null;
      this.lineSig = "";
      this.routeSig = "";
      this.far = false;
      this.unsetOpen = false;
      this.fol = null;                 // the map's follower (RigMap: mapfollow.js), none on a manual map
      this.onFollow = null;            // RigMap: follow a rig (the picker's or the box's Follow)
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
        if (!map.getSource("cm-routes")) {            // the probable paths (#360) and a followed rig's trail
          map.addSource("cm-routes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
          map.addLayer({
            id: "cm-trail", type: "line", source: "cm-routes", filter: ["==", ["get", "trail"], true],
            layout: { "line-cap": "round", "line-join": "round" },
            paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.3 },
          }, map.getLayer("firehouse") ? "firehouse" : undefined);
          map.addLayer({
            id: "cm-routes", type: "line", source: "cm-routes", filter: ["!=", ["get", "trail"], true],
            layout: { "line-cap": "round", "line-join": "round" },
            paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.9, "line-dasharray": [1.4, 1.2] },
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
        if (far !== this.far) { this.far = far; opts.host.classList.toggle("cm-far", far); this.setBand(); this.draw(); }
      });
      // #376: laid out again only if the band changed; moveend too: a drag takes the map off its automatic frame
      const settle = () => { if (this.setBand()) this.draw(); };
      map.on("zoomend", settle);
      map.on("moveend", settle);
    }

    // the zoom band for the rigs' sizes (#376), from the map's zoom now and this map's zooms; true if it changed
    setBand() {
      let zoom;
      try { zoom = this.map.getZoom(); } catch (_) { return false; }
      const b = bandOf(zoom, zoomsOf(this.data && this.data.map, this.opts.board), this.auto());
      const changed = !this.band || b.detail !== this.band.detail || b.active !== this.band.active;
      this.band = b;
      return changed;
    }

    // opt.draw false: the data only (RigMap.update draws once, after it has framed the map)
    update(data, focus, opt) {
      this.data = data;
      this.focus = focus || null;
      feedGlide(data && data.map);
      if (opt && opt.draw === false) return;
      this.setBand();                  // the zooms may have changed (Settings > System > Map display)
      this.draw();
    }

    draw() {
      if (!this.ready || !this.data || !this.data.map) return;
      const M = this.data.map, lists = this.data.lists;
      if (this.opts.pins) this.drawPins(M.incidents || [], lists);
      this.drawChips(drawable(M), lists);
      this.drawLines(drawable(M), lists);
      if (this.unsetBox) this.drawUnset(M.unset || []);
      this.followTick(true);
      animate();
    }

    // where a rig's chip goes now: a live fix drawn on by the follower (fixWhere), else where the hub put it
    where(m) {
      return m.place === "position" && typeof m.at === "number" ? fixWhere(m, this.data && this.data.map) : [m.lon, m.lat];
    }
    shown() { try { return !!this.map.getContainer().offsetParent; } catch (_) { return false; } }
    // anything to animate here: a live fix moving or catching up (not a parked or a stale one: they settle), a rig
    // followed, or the frames owed after a new state
    moving() {
      return !!(this.ready && this.shown() && ((this.fol && this.fol.id) || (this.hasFix && (fresh > 0 || (glide && glide.busy())))));
    }
    // how often to tick this map: each time the fastest rig has moved STEP_PX on it at its zoom (aircraft.js frameMs)
    frameMs() {
      try {
        const metresPerPx = 40075016.686 * Math.cos(this.map.getCenter().lat * Math.PI / 180) / (512 * 2 ** this.map.getZoom());
        const pxPerS = fastest / metresPerPx;
        return Math.max(FRAME_MS, Math.min(MAX_FRAME_MS, pxPerS > 0 ? 1000 * STEP_PX / pxPerS : MAX_FRAME_MS));
      } catch (_) { return FRAME_MS; }
    }
    // a frame: the live fixes moved on, their lines to the call with them, the follow kept centred
    tick() {
      let moved = false;
      for (const rec of this.chips.values()) if (rec.m.place === "position") { rec.marker.setLngLat(this.where(rec.m)); moved = true; }
      // the call lines and routes with them, at most LINE_MS apart (setData re-tiles the source; the age timer's
      // redraw puts them on the settled marks)
      if (moved && this.data && this.data.map && Date.now() - (this.linesAt || 0) >= LINE_MS) {
        this.drawLines(drawable(this.data.map), this.data.lists);
      }
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
      const r = routeOf(this.data && this.data.map, m);      // by road (#360): "~3 min · 1.2 mi"
      const text = [`Following ${m.unit}`, st.name, r ? r.text : "", fx ? `${agoS(fx.age)} ago` : ""].filter(Boolean).join(" · ");
      f.chip(text, r ? `${rec.el.title}, ${r.text} by road (est.)` : rec.el.title, { stale: !!(fx && fx.stale) });
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
    // #376: sizes (declutter()): a folded rig takes no place; a dot is DOT_W wide. #384: a firehouse with a count chip:
    // an arc under it (the chip is on top of the icon).
    offsets(markers, sizes) {
      const k = this.far ? 0.72 : 1;
      const size = (m) => (sizes ? sizes.size.get(m.unit) : "full");
      const wOf = (m) => (size(m) === "dot" ? DOT_W : CHIP_W * k);
      const groups = new Map();
      for (const m of markers) {
        if (size(m) === "folded") continue;
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
          const dots = list.every((m) => size(m) === "dot"), gap = dots ? DOT_W : RING_GAP * k;
          const sum = list.reduce((a, m) => a + wOf(m), 0), w = Math.max(...list.map(wOf));
          const badge = m0.place === "station" && !!(sizes && sizes.badges.has(m0.station));
          if (m0.place === "station" && !badge) {
            r = Math.max(ring ? r + gap : dots ? DOT_R : STATION_R * k, sum / (2 * Math.PI));
            list.forEach((m, i) => {
              const a = Math.PI / 2 + (2 * Math.PI * i) / n + (ring ? Math.PI / n : 0);
              out.set(m.unit, [Math.round(r * Math.cos(a)), Math.round(r * Math.sin(a) * 0.82)]);
            });
            return;
          }
          // a call's: an arc centred on the top, 270° at most; a firehouse with a count chip on top of its icon
          // (#384): 140° centred on the bottom, clear of the chip and of the name's places either side
          const step = n > 1 ? Math.min((badge ? 140 : 270) / (n - 1), 62) * deg : 0;
          const need = step ? w / (2 * Math.sin(step / 2)) : 0;                // neighbours a chip apart
          r = Math.max(ring ? r + gap : badge ? (dots ? DOT_R : STATION_R * k) : dots ? DOT_R : INC_R * k, need);
          list.forEach((m, i) => {
            const a = (badge ? Math.PI / 2 : -Math.PI / 2) + (i - (n - 1) / 2) * step;
            out.set(m.unit, [Math.round(r * Math.cos(a)), Math.round(r * Math.sin(a) * 0.82)]);
          });
        });
      }
      return out;
    }

    drawChips(markers, lists) {
      // the rings (#358): picked (for a while), its row hovered (wider), followed (plain); transmitting: talk
      const lits = new Map(markers.map((m) => [m.unit, [...(lights ? [lights.isPicked(m.unit), lights.hovered(m.unit)] : [false, false]),
                                                         !!this.fol && this.fol.id === m.unit, talkState(m.unit)]]));
      if (!this.band) this.setBand();
      // #376: lit ones full; so is a rig just made active (FRESH_S), laid out again when the last of those runs out
      let soonest = 0;
      const sizes = declutter(markers, this.band, (m) => {
        const left = freshLeft(m);
        if (left > 0) soonest = soonest ? Math.min(soonest, left) : left;
        return left > 0 || lits.get(m.unit).some(Boolean);
      });
      clearTimeout(this.freshT);
      this.freshT = soonest ? setTimeout(() => this.draw(), Math.ceil(soonest * 1000) + 50) : 0;
      const offs = this.offsets(markers, sizes);
      const seen = new Set();
      const M = this.data && this.data.map;
      this.hasFix = false;
      for (const m of markers) {
        seen.add(m.unit);
        const st = statusOf(lists, m.status);
        const [picked, hot, followed, talk] = lits.get(m.unit), lit = [picked, hot, followed];
        const sz = sizes.size.get(m.unit), cl = rigClass(m);
        const off = offs.get(m.unit) || [0, 0];
        const onFocus = !!this.focus && m.incident_id === this.focus.id;
        const fx = fixAge(m, M);                               // a live fix: its age, stale (hollow, faded, aged)
        if (fx) this.hasFix = true;
        const age = fx && fx.stale ? agoS(fx.age) : "";
        const sig = JSON.stringify([m.lon, m.lat, off, m.look, m.ring, m.place, st.bg, talk, m.tag, m.station, m.incident_number,
          m.returning_from, onFocus, !!this.focus, this.far, age, fx && Math.round(fx.fade * 20), lit, m.source, sz, cl, m.icon]);
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
          + (cl === "act" ? " act" : "") + (sz === "dot" ? " rig-dot" : sz === "folded" ? " folded" : "")
          + (this.focus && m.incident_id && !onFocus ? " other" : "") + (onFocus ? " mine" : "")
          + (fx && fx.stale ? " stale" : "") + (lit[0] ? " hl" : "") + (lit[1] ? " hot" : "") + (lit[2] ? " followed" : "");
        e.style.setProperty("--c", st.bg || "#9AA3AD");
        // a stale fix fades; not an active rig's dot (#376: it stays plain to see)
        e.style.opacity = fx && fx.stale && !(sz === "dot" && cl === "act") ? String(Math.round(fx.fade * 100) / 100) : "";
        e.replaceChildren(h("i", `cm-ty ty-${iconOf(m)}`), h("span", "n", m.unit));     // #384: its type, then its code
        if (m.tag) e.append(h("span", "tag", m.tag));
        if (age) e.append(h("span", "age", age));
        e.title = [m.unit + (ICON_NAMES[iconOf(m)] ? ` (${ICON_NAMES[iconOf(m)]})` : ""), st.name,
                   fx ? `${fx.stale ? "last fix" : "live"} ${agoS(fx.age)} ago${m.source ? ` (${m.source})` : ""}` : "",
                   m.place === "returning" ? `returning from ${m.returning_from} (est. position)` : "",
                   m.station ? `home ${m.station}` : (m.fire ? "no home station" : ""),
                   m.incident_number ? `on ${m.incident_number}` : "",
                   talk === "talking" ? "TX now" : talk ? "TX ended" : ""].filter(Boolean).join(", ");
        e.setAttribute("aria-label", e.title);
        rec.marker.setLngLat(this.where(m)).setOffset(off);
      }
      for (const [name, rec] of this.chips) if (!seen.has(name)) { rec.marker.remove(); this.chips.delete(name); }
      this.drawBadges(sizes.badges, M, lists);
      if (this.stationOpen && !this.pop.hidden) {          // #384: the open list kept to the state (a status, a call)
        if (this.listSig(this.stationRigs(this.stationOpen)) !== this.stationSig) this.openStation(this.stationOpen, true);
      }
    }

    // #376: a firehouse's idle rigs below detail_zoom as one count chip (#384: on top of its icon, "6", the size of
    // a rig's mark); its tooltip lists them, a click lists the firehouse's rigs (openStation)
    drawBadges(badges, M, lists) {
      for (const [name, list] of badges) {
        const units = list.map((m) => m.unit);
        const sig = JSON.stringify([units, list[0].lon, list[0].lat]);
        let rec = this.badges.get(name);
        if (!rec) {
          const e = h("button", "cm-badge");
          e.type = "button";
          e.addEventListener("click", (ev) => { ev.stopPropagation(); this.openStation(name); });
          rec = { marker: new maplibregl.Marker({ element: e, anchor: "center" }).setLngLat([list[0].lon, list[0].lat])
                    .setOffset(BADGE_AT).addTo(this.map), el: e, sig: "" };
          this.badges.set(name, rec);
        }
        if (rec.sig === sig) continue;
        rec.sig = sig;
        const e = rec.el;
        e.replaceChildren(String(units.length));                         // neutral: its units' statuses may differ
        e.title = `${name}\n${units.length} in quarters: ${units.join(", ")}`;
        e.setAttribute("aria-label", `${name}, ${units.length} in quarters`);
        rec.marker.setLngLat([list[0].lon, list[0].lat]);
      }
      for (const [name, rec] of this.badges) if (!badges.has(name)) { rec.marker.remove(); this.badges.delete(name); }
      this.syncFirehouses(new Set(badges.keys()));
    }

    // The firehouses' source, each with its badge flag (#384): the style's "firehouse-badge" mark (map-style.js) holds
    // the count chip's corner in MapLibre's label placement, so a firehouse's name is placed clear of it (or left out).
    // Set again only when the firehouses with a chip change.
    syncFirehouses(badged) {
      const fhs = (this.opts.scene && this.opts.scene.firehouses) || [];
      const sig = JSON.stringify([fhs.map((f) => f.name), [...badged].sort()]);
      if (sig === this.fhSig) return;
      let src = null;
      try { src = this.map.getSource("firehouses"); } catch (_) { /* style not up yet */ }
      if (!src) return;
      this.fhSig = sig;
      src.setData({
        type: "FeatureCollection",
        features: fhs.map((f) => ({ type: "Feature", properties: { name: f.name, address: f.address, badge: badged.has(f.name) ? 1 : 0 },
                                    geometry: { type: "Point", coordinates: f.ll } })),
      });
    }

    // #384: a firehouse's rigs (home station), active first, then idle, then out of service, each in the hub's order
    stationRigs(name) {
      const ms = drawable(this.data && this.data.map);
      return ms.map((m, i) => [m, i]).filter(([m]) => m.station === name)
        .sort((a, b) => CLASS_ORDER[rigClass(a[0])] - CLASS_ORDER[rigClass(b[0])] || a[1] - b[1]).map(([m]) => m);
    }

    listSig(rigs) { return JSON.stringify(rigs.map((m) => [m.unit, m.status, m.icon, m.incident_number, this.chips.has(m.unit)])); }

    // #384: a firehouse clicked (its icon or its count chip): its rigs in the map's box, each with its type and status;
    // a click on one follows it (#358) or, where the map follows nothing, centres and rings it; hovered: ringed wider.
    // Zoom to: the firehouse at detail_zoom (its rigs drawn one by one).
    // refresh: the list open, laid out again on a new state that changes its rows (the focus left where it is)
    openStation(name, refresh) {
      if (!this.data || !this.data.map) return;
      const lists = this.data.lists, rigs = this.stationRigs(name);
      // a refresh: the row focused (by its unit) focused again; the row hovered: its ring kept, or cleared if it went
      const act = typeof document !== "undefined" ? document.activeElement : null;
      const focusUnit_ = refresh && act && this.pop.contains(act) && act.dataset ? act.dataset.unit || "" : "";
      if (!refresh) this.hoverRow = "";
      else if (this.hoverRow && !rigs.some((m) => m.unit === this.hoverRow)) { this.hoverRow = ""; hoverUnits([]); }
      this.stationSig = this.listSig(rigs);
      const go = this.onFollow || this.onFocus;
      const head = h("div", "cm-pop-head");
      head.append(h("b", "", name), " ", h("span", "dim small", stationCounts(rigs)));
      const list = h("div", "cm-st-list");
      for (const m of rigs) {
        const st = statusOf(lists, m.status);
        const b = h("button", "cm-st-row" + (rigClass(m) === "oos" ? " oos" : ""));
        b.type = "button";
        b.dataset.unit = m.unit;
        b.style.setProperty("--c", st.bg || "#9AA3AD");
        b.append(h("i", `cm-ty ty-${iconOf(m)}`), h("b", "n", m.unit), h("span", "s", st.name));
        if (m.incident_number) b.append(h("span", "dim", m.incident_number));
        b.title = [m.unit + (ICON_NAMES[iconOf(m)] ? ` (${ICON_NAMES[iconOf(m)]})` : ""), st.name,
                   go ? (this.onFollow ? "click: follow" : "click: show") : ""].filter(Boolean).join(", ");
        b.disabled = !go || !this.chips.has(m.unit);
        b.addEventListener("click", (e) => { e.stopPropagation(); this.closePicker(); go(m.unit); });
        b.addEventListener("mouseenter", () => { this.hoverRow = m.unit; hoverUnits([m.unit]); });
        b.addEventListener("mouseleave", () => { this.hoverRow = ""; hoverUnits([]); });
        list.append(b);
      }
      const row = h("div", "row");
      const z = h("button", "mini", "Zoom to");
      z.type = "button";
      z.title = `${name}: its rigs one by one`;
      z.addEventListener("click", (e) => { e.stopPropagation(); this.closePicker(); this.zoomStation(name); });
      const x = h("button", "mini quiet", "Close");
      x.type = "button";
      x.addEventListener("click", (e) => { e.stopPropagation(); this.closePicker(); });
      row.append(z, x);
      this.pop.replaceChildren(head, list, row);
      this.pop.hidden = false;
      this.stationOpen = name;
      if (refresh) {
        const again = focusUnit_ && [...list.children].find((r) => r.dataset.unit === focusUnit_);
        if (again && !again.disabled) again.focus({ preventScroll: true });
        else if (focusUnit_) this.pop.focus({ preventScroll: true });           // its row went: Escape still works
        return;
      }
      this.pop.tabIndex = -1;
      this.pop.focus({ preventScroll: true });          // Escape closes it (the pop's keydown); Tab: its rows
    }

    // #384: the firehouse centred at detail_zoom at least: its rigs at home drawn one by one (those out on calls are
    // where they are, drawn as ever). A move by hand as far as the frame goes (stays until Recenter); a follow let go.
    zoomStation(name) {
      const M = (this.data && this.data.map) || {};
      const fh = (M.stations || []).find((s) => s.name === name) || ((this.opts.scene && this.opts.scene.firehouses) || []).find((s) => s.name === name);
      const ll = fh ? fh.ll : null;
      if (!ll) return false;
      if (window.MapFollow) MapFollow.takeMap(this.map);
      if (this.onMoved) this.onMoved();
      const z = zoomsOf(M, this.opts.board).detail;
      try { this.map.easeTo({ center: ll, zoom: Math.max(this.map.getZoom(), z), duration: 500 }); } catch (_) { /* */ }
      return true;
    }

    drawLines(markers, lists) {
      this.linesAt = Date.now();
      const feats = new Map();
      const M = this.data && this.data.map;
      this.drawRoutes(markers, lists, M);
      for (const m of markers) {
        if (!m.line_to || (!m.home_line && routeOf(M, m))) continue;      // its route drawn instead (#360)
        const from = markAt(m);                                             // a live fix: from its mark
        const k = JSON.stringify([from, m.line_to]);
        const color = m.home_line ? HOME_LINE : statusOf(lists, m.status).bg || LINE;
        if (!feats.has(k)) feats.set(k, { type: "Feature", properties: { color, home: !!m.home_line },
          geometry: { type: "LineString", coordinates: [from, m.line_to] } });
      }
      const fc = { type: "FeatureCollection", features: [...feats.values()] };
      const sig = JSON.stringify(fc);
      if (sig === this.lineSig) return;
      this.lineSig = sig;
      const src = this.map.getSource("cm-lines");
      if (src) src.setData(fc);
    }

    // each route from the rig's mark as drawn now (a live fix: glided) on; the followed rig's trail up to its mark
    drawRoutes(markers, lists, M) {
      const src = this.map.getSource("cm-routes");
      if (!src) return;
      const feats = [];
      const trails = (M && M.trails) || {};
      for (const m of markers) {
        const color = statusOf(lists, m.status).bg || LINE;
        const r = m.line_to && !m.home_line ? routeOf(M, m) : null;
        const here = markAt(m);
        const path = r ? routeFrom(r.coords, here, r.along_m) : null;
        if (path && path.length >= 2) feats.push({ type: "Feature", properties: { color, unit: m.unit },
                                                   geometry: { type: "LineString", coordinates: path } });
        const t = this.fol && this.fol.id === m.unit ? trails[m.unit] : null;
        if (Array.isArray(t) && t.length >= 2) feats.push({ type: "Feature", properties: { color, trail: true, unit: m.unit },
                                                            geometry: { type: "LineString", coordinates: [...t, here] } });
      }
      const fc = { type: "FeatureCollection", features: feats };
      const sig = JSON.stringify(fc);
      if (sig === this.routeSig) return;
      this.routeSig = sig;
      src.setData(fc);
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
    // a rig clicked: as before #358 (an admin: its home-station picker; else its call opened). Following starts from a
    // row's ⌖, the picker's Follow, or (a rig on no call, where a click did nothing) its box's Follow.
    chipClick(m) {
      if (this.editable() && m.fire && m.id != null) return this.openPicker(m.id, m.unit, m.station, m);
      if (m.incident_id && this.opts.onPick) return this.opts.onPick(m.incident_id);
      if (this.onFollow) this.openInfo(m);
    }
    followButton(m) {
      const f = h("button", "mini", "Follow");
      f.type = "button";
      f.title = `Follow ${m.unit} on the map`;
      f.addEventListener("click", (e) => { e.stopPropagation(); this.closePicker(); this.onFollow(m.unit); });
      return f;
    }

    openInfo(m) {
      this.stationOpen = null;                         // the box is this rig's now (#384)
      const st = statusOf(this.data && this.data.lists, m.status);
      const head = h("div", "cm-pop-head");
      head.append(h("b", "", m.unit));
      const line = h("div", "dim small", st.name);
      const row = h("div", "row");
      const f = this.followButton(m);
      const x = h("button", "mini quiet", "Close");
      x.type = "button";
      x.addEventListener("click", (e) => { e.stopPropagation(); this.closePicker(); });
      row.append(f, x);
      this.pop.replaceChildren(head, line, row);
      this.pop.hidden = false;
      f.focus();                                       // Escape closes it (the pop's keydown)
    }

    openPicker(id, name, current, m) {
      this.stationOpen = null;                         // the box is the picker's now (#384)
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
        if (this.onFollow && this.chips.has(m.unit)) line.append(" ", this.followButton(m));   // #358
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
      if (this.stationOpen) hoverUnits([]);
      this.stationOpen = null;
      this.hoverRow = "";
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
    // what decides a refit: focusPoints, with a rig on a live fix by name, not where it is (#358: not on every fix)
    focusKey() {
      const M = (this.data && this.data.map) || {};
      const live = new Set((M.markers || []).filter((m) => m.place === "position" && this.focus && m.incident_id === this.focus.id)
        .map((m) => JSON.stringify([m.lon, m.lat])));
      return [...this.focusPoints().map((p) => (live.has(JSON.stringify(p)) ? "live" : p)),
              ...(M.markers || []).filter((m) => m.place === "position" && this.focus && m.incident_id === this.focus.id).map((m) => `rig:${m.unit}`)];
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
        if (this.layer.setBand && this.layer.setBand()) this.layer.draw();  // #376: the automatic frame again: active rigs full
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
        if (typeof badgeSpaceIcon === "function") this.map.addImage("firehouse-badge", badgeSpaceIcon());   // #384
        this.layer.fhSig = null;
        this.layer.syncFirehouses(new Set(this.layer.badges.keys()));
      });
      // #384: a click on the map (not a pan or a zoom: MapLibre's click), off a firehouse, closes a firehouse's list
      this.map.on("click", (e) => {
        if (!this.layer.stationOpen) return;
        let hit = e && e.features ? e.features.length : 0;
        if (!hit && e && e.point) {
          try { hit = this.map.queryRenderedFeatures(e.point, { layers: ["firehouse"] }).length; } catch (_) { /* */ }
        }
        if (!hit) this.layer.closePicker();
      });
      // #384: a firehouse (its icon or its name) clicked: its rigs
      this.map.on("click", "firehouse", (e) => {
        const f = e && e.features && e.features[0];
        if (f && f.properties && f.properties.name) this.layer.openStation(f.properties.name);
      });
      const cursor = (c) => () => { try { this.map.getCanvas().style.cursor = c; } catch (_) { /* */ } };
      this.map.on("mouseenter", "firehouse", cursor("pointer"));
      this.map.on("mouseleave", "firehouse", cursor(""));
      this.map.on("movestart", (e) => { if (e.originalEvent) this.userMoved = true; });
      this.map.on("sizeup:follow", () => { this.userMoved = true; });   // following an aircraft: no refit until Recenter
      this.layer = new RigLayer(this.map, { ...opts, host });
      this.layer.auto = () => !this.userMoved;        // #376: on the automatic frame, every active rig a full chip
      // following a rig (#358, mapfollow.js): not on the new call's map (its owner moves it), nor the wall board's
      this.fol = !opts.manual && opts.follow !== false && window.MapFollow
        ? new MapFollow.Follower(this.map, host, { cls: "ac-follow cm-follow", textCls: "ac-follow-t", icon: "locate",
                                                   onStop: () => this.layer.draw() })
        : null;
      this.layer.fol = this.fol;
      if (this.fol) this.layer.onFollow = (name) => this.followUnit(name);
      if (!opts.manual) this.layer.onFocus = (name) => this.focusUnit(name);     // #384: the station list, no follower
      this.layer.onMoved = () => { this.userMoved = true; };                      // #384: Zoom to, as a move by hand
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
      this.layer.update(data, focus, { draw: false });  // #376: drawn once below, in the band of the frame it ends on
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
      const key = JSON.stringify(this.focus ? this.layer.focusKey() : this.framePoints());
      if (!this.focus) {                              // no call open: the calls changed, so all of them again, as
        if (this.callsKey !== undefined && key !== this.callsKey) {
          this.userMoved = false;                     // Recenter does (#218)
          if (this.layer.stationOpen) this.layer.closePicker();   // #384: a firehouse's list never left over the calls
        }
        this.callsKey = key;                          // (a view switch or a resize leaves a map the viewer moved)
      } else this.callsKey = undefined;
      if (key !== this.fitKey && this.canvas.clientWidth) {
        const first = this.fitKey === null;
        this.fitKey = key;
        const following = !!(this.fol && this.fol.id);   // #358: never refit under a follow
        if (!following) { if (!this.userMoved && !this.opts.manual) this.fit(!first); }
      }
      if (this.layer.setBand) { this.layer.setBand(); this.layer.draw(); }   // #376: framed again (another call, the calls changed)
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
      if (window.MapFollow) MapFollow.takeMap(this.map);   // a rig's follower or an aircraft's: let go (#358)
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
                                                    onPick: (id) => selectIncident(id), layers: "overview", follow: !BOARD,
                                                    board: BOARD });       // the wall board's own zooms (#376)
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
           followUnit, focusUnit, hoverUnits, onMap, lights, routeFrom, declutter, zoomsOf, rigClass, iconOf };
})();
