// Metro-North trains on the CAD maps (GitHub #131): the New Haven Line through Rye, Port Chester and
// Greenwich, and "Trains approaching" for an event on or near the tracks.
//
//   Layer        "Trains" in every map's Layers box (maplayers.js MapLayers.addGroup; key "trains", off by
//                default, in the viewer's or the event's set like the rest, #111): the line from Mount Vernon
//                East to Stamford (the hub's [trains] bbox), its stations, and each train on it now, drawn as a
//                train lying along its own direction's track (the hub's lanes: outbound on track 4, the south
//                side, inbound on track 3, the north, as the MTA's stops say) the way it is going (#151): a cab
//                with its windscreen at the
//                front, the body behind; its number and where it is headed beside it, on the side away from
//                the other direction's trains. One length for every train: the MTA's feed says nothing of how
//                many cars a train has (no consist, sizeup/trains.py); should it ("cars"), that many cars are
//                drawn. Filled when the MTA reports where it is, hollow when the position is our estimate
//                (between the stations it is between, by its times). The heading is the line's own there (the
//                feed has no bearing). A click: the train's line and destination, direction, next stop and time,
//                track, delay, status and whether the position is reported or estimated; a station or the
//                line: its name and the catenary note. Polled every 5 s only while a map has it on and the
//                page is visible (the hub reads the MTA only while someone asks, as often); a poll names the
//                line it already has (?have=static_at), so the answer is only the trains (a few KB). A failed
//                poll waits longer each time (10 s doubling to a minute) and says so in the console.
//   Motion       between the MTA's reports a train glides on along its own track (motion.js Motion.railTracker,
//                the aircraft's tracker with the trains' predictor): in s, its distance along the line, on the
//                hub's run profile (profile_s, ported 1:1), never past hold_s (its next known reporting point),
//                never past its next stop, never backwards, never after predict_until; then it is held. A train
//                stopped at a station stays there until the hub says it left. A new report is caught up along
//                the track (never across it); one behind what is drawn holds the train until the prediction
//                meets it; a big jump, the other way or another train number is drawn at once. A feed older
//                than the hub's stale_s (or one the hub calls stale): frozen and drawn faded. Redrawn about 10
//                times a second (ANIMATE_MS) only while one moves, a map has the layer on and the page is
//                visible: one setData for the cars and one for the labels a frame, none when nothing changed.
//                The click box says "predicted" while the train is drawn ahead of the MTA's report.
//   Panel        TrainLayer.panel(inc, host) (cadmap.js, under the event's map): for an event within 150 m of
//                the tracks or a station, the next trains each way in the next 15 minutes at the nearest
//                station (or the event's point on the tracks): minutes away, stops or passes, delay, track;
//                times the MTA predicts, or ours (a pass between stations: "estimated"). Every 30 s while
//                the event is open and the panel on screen; hidden for any other event.
//   Tracks       (#371) the line's four tracks (the hub's "tracks", Metro-North's GIS, in runs: a gap is drawn as
//                one), numbered from zoom 17. A train lies along the track the hub puts it on ("lane": the track
//                posted or planned for the stop it left or is going to, kept until another stop says otherwise;
//                else its direction's outer track). "Trk 2" in its box only near the stop that said so ("trk").
//   Poles · MP   (#371) a second box in the group, "Poles · mileposts" (key "railref", off by default): the catenary
//                structures' numbers from street zoom, the mileposts (whole miles from zoom 12, tenths from 15), from
//                GET /api/cad/trains/railref once while on. A train's box and the event's panel say "near pole 249 ·
//                MP 25.3" when the hub knows them (within 150 m of the line).
//   Data         only ever from the hub: GET /api/cad/trains and /api/cad/trains/near (sizeup/trains.py). The
//                browser never talks to the MTA. The MTA's terms: we say where the data is from, that times
//                may not be real time, and the feed's time.
"use strict";

window.TrainLayer = (() => {
  const URL = "/api/cad/trains", NEAR = "/api/cad/trains/near";
  const ROUTE = "/api/cad/trains/route", DEPS = "/api/cad/trains/departures";   // MTA TrainTime (#362)
  const DEP_STATIONS = ["2PC", "2RY"];        // Port Chester, Rye: their next departures in the station's box
  const KEY = "trains", REF_KEY = "railref", RAILREF = "/api/cad/trains/railref";   // poles and mileposts (#371)
  const POLL_MS = 5000, PANEL_MS = 30000;
  const ANIMATE_MS = 100;                    // ~10 redraws a second while a train moves (motion.js)
  const PREDICTED_M = 5;                     // drawn this far ahead of the MTA's report: "predicted"
  const STALE_OPACITY = 0.45;                // a train frozen on an old feed: faded
  const BACKOFF_MS = [10000, 60000];         // a failed poll: 10 s doubling to a minute
  const SRC_LINE = "tr-line", SRC_ST = "tr-stations", SRC_TR = "tr-trains", SRC_CARS = "tr-cars", SRC_ROUTE = "tr-route";
  const SRC_REF = "tr-railref", REF_IDS = ["tr-pole", "tr-pole-label", "tr-mp", "tr-mp-label"];
  const CAR_IDS = ["tr-car-case", "tr-cab-case", "tr-car", "tr-cab", "tr-gap"];
  const IDS = ["tr-line", "tr-line-label", "tr-route", "tr-station", "tr-station-label", ...CAR_IDS, "tr-train", "tr-train-label"];
  const MAX_CARS = 16;
  const CLICKABLE = ["tr-train", "tr-car", "tr-cab", "tr-gap", "tr-station", "tr-line"];
  const TRAIN_HIT = ["tr-train", "tr-car", "tr-cab", "tr-gap"];
  const HALO = "#15181B";

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
  function hm(t, secs) {
    if (!t) return "";
    try {
      return new Date(t * 1000).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", ...(secs ? { second: "2-digit" } : {}),
                                                              hour12: false, timeZone: tz() });
    } catch (_) { return ""; }
  }
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);     // the hub's why_not() is lower case ("off (…)")
  function late(s) {
    if (s === null || s === undefined) return "";
    const m = Math.round(s / 60);
    return m >= 1 ? `${m} min late` : m <= -1 ? `${-m} min early` : "on time";
  }
  const fc = (features) => ({ type: "FeatureCollection", features });
  const pt = (lon, lat, props) => ({ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: props });

  // ---- the layer's data: one copy for every map on the page, fetched only while one has it on --------------------
  const store = { data: null, timer: null, loading: null, subs: new Set(), at: 0, fails: 0 };
  const maps = new Set();
  const anyOn = () => { for (const m of maps) if (m.isOn()) return true; return false; };
  function notify() { for (const fn of store.subs) { try { fn(store.data); } catch (e) { console.error(e); } } }
  async function load() {
    if (store.loading) return store.loading;
    const t0 = performance.now();               // map:trains: asked -> drawn (#284)
    store.loading = (async () => {
      try {
        const had = store.data;                // the line and stations we hold: the hub leaves them out when unchanged
        const have = had && had.static_at != null && had.line && had.line.length ? `?have=${encodeURIComponent(had.static_at)}` : "";
        const r = await fetch(URL + have, { credentials: "same-origin" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        if (d.static_same && had) { d.line = had.line; d.stations = had.stations; d.lanes = had.lanes; d.tracks = had.tracks; d.railref = had.railref; }
        store.data = d;
        store.at = Date.now();
        store.fails = 0;
        moved(d, store.at);
      } catch (e) {                            // the hub is away: keep what we have, ask less often
        store.fails += 1;
        console.warn(`trains: ${e.message || e}; next poll in ${Math.round(nextPoll() / 1000)} s`);
      }
      finally { store.loading = null; notify(); animate(); try { performance.clearMeasures("map:trains"); performance.measure("map:trains", { start: t0 }); } catch (_) { /* an older browser */ } }
    })();
    return store.loading;
  }
  const nextPoll = () => (store.fails ? Math.min(BACKOFF_MS[1], BACKOFF_MS[0] * 2 ** (store.fails - 1)) : POLL_MS);
  function kick() {                         // a map turned it on (or the page came back): fetch now, then every 5 s
    clearTimeout(store.timer);
    store.timer = null;
    animate();
    if (!anyOn() || document.visibilityState === "hidden") return;
    if (Date.now() - store.at > POLL_MS / 2) load();
    store.timer = setTimeout(function tick() {
      store.timer = null;
      if (!anyOn() || document.visibilityState === "hidden") return;
      load().then(() => { if (!store.timer && anyOn()) store.timer = setTimeout(tick, nextPoll()); });
    }, nextPoll());
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") kick(); else animate(); });
  // the hub pushed a new read through the state stream (trains_rev, #362): fetch it now (kick skips a fresh answer)
  if (window.addEventListener) window.addEventListener("sizeup:trains", () => { if (anyOn() && document.visibilityState !== "hidden") kick(); });

  // ---- the motion: one tracker for every map (motion.js Motion.railTracker), fed each answer's trains ---------------
  // The hub's times are its clock's: the feed's time t0 on this browser's clock is t0 + skew, skew the smallest
  // (least delayed) of the last few answers' Date.now() - (at + age), so a browser clock that is off moves nothing.
  const motion = window.Motion && Motion.railTracker ? Motion.railTracker() : null;
  const skews = [];
  function fixOf(t, d, at, got) {
    if (typeof t.s !== "number" || !(t.d === 1 || t.d === -1)) return null;
    const prof = d.profile || {};
    if (t.pos_src === "traintime" && typeof t.fix_at === "number" && typeof d.now === "number") {
      // a TrainTime fix keeps its own time (#362): fresh while GTFS-realtime is stale, stale on its own
      at = got - (d.now - t.fix_at) * 1000;
      d = { ...d, at: t.fix_at };
    }
    return { s: t.s, d: t.d, train: t.train, mode: t.mode || (t.estimated ? "estimated" : "reported"), t0: d.at, at,
             fixAt: t.fix_at != null ? t.fix_at : null, hold: t.hold_s, stop: t.next && typeof t.next.s === "number" ? t.next.s : null,
             until: typeof t.predict_until === "number" ? t.predict_until : d.at + (prof.predict_s || 120),
             seg: t.segment ? { accel: prof.accel, decel: prof.decel, vmax: prof.vmax, ...t.segment } : null, lag: t.lag_s,
             v: t.pos_src === "traintime" && typeof t.v === "number" ? t.v : null };   // GPS speed, m/s (MTA TrainTime, #362)
  }
  function moved(d, got) {
    if (!d) return;
    if (motion) motion.staleS = (d.profile && d.profile.stale_s) || 60;
    let at = -Infinity;                         // no feed time, or the hub says the feed is stale: frozen
    if (!d.stale && typeof d.at === "number" && typeof d.age === "number") {
      skews.push(got - (d.at + d.age) * 1000);
      if (skews.length > 12) skews.shift();
      at = d.at * 1000 + Math.min(...skews);
    }
    store.feedAt = at;
    if (!motion) return;
    const keep = new Set();
    for (const t of d.trains || []) {
      const f = fixOf(t, d, at, got);
      if (!f) continue;
      keep.add(t.id);
      motion.set(t.id, f, got);
    }
    motion.forget(keep);
    frame.at = -1;
  }
  // where each train is drawn now: one where() a train a frame, whichever maps draw it
  const frame = { at: -1, pos: new Map() };
  function positions(now) {
    if (frame.at === now) return frame.pos;
    frame.at = now;
    frame.pos = new Map();
    const d = store.data;
    if (motion && d) for (const t of d.trains || []) { const p = motion.where(t.id, now); if (p) frame.pos.set(t.id, p); }
    return frame.pos;
  }
  let animTimer = null;
  function animate() {                         // redraw ~10 a second only while a train moves on a visible map
    const want = !!motion && anyOn() && document.visibilityState !== "hidden" && motion.busy();
    if (want && !animTimer) {
      animTimer = setInterval(() => { const now = Date.now(); for (const m of maps) m.draw(false, now); animate(); }, ANIMATE_MS);
    } else if (!want && animTimer) {
      clearInterval(animTimer);
      animTimer = null;
    }
  }

  // ---- the marks: a train, pointing up (icon-rotate turns it to its heading), filled (reported) or hollow ----------
  // (estimated). Cars, the first with the cab's windscreen: the MTA's count when it gives one, otherwise the look of
  // a typical train (LOOK_CARS), the same for every train; the box says the length isn't the MTA's (#164). Drawn at
  // pixelRatio 2, a car about to scale at street zoom.
  const CAR = 30, W = 20, X0 = 4, X1 = 16, CX = 10, TOP = 3, LOOK_CARS = 6;
  function trainIcon(filled, cars) {
    const n = Math.max(1, Math.min(MAX_CARS, (cars | 0) || LOOK_CARS));
    const H = TOP * 2 + n * CAR - 3;
    const c = document.createElement("canvas");
    c.width = W;
    c.height = H;
    const g = c.getContext("2d");
    const col = token("--warn", "#E0A030"), dark = token("--bg", HALO);
    g.lineJoin = "round";
    const body = (top, bottom, cab) => {      // one car: a rounded nose when it is the cab
      g.beginPath();
      if (cab) { g.moveTo(X0, top + 14); g.bezierCurveTo(X0, top + 5, X0 + 4, top, CX, top); g.bezierCurveTo(X1 - 4, top, X1, top + 5, X1, top + 14); }
      else { g.moveTo(X0, top + 3); g.quadraticCurveTo(X0, top, X0 + 3, top); g.lineTo(X1 - 3, top); g.quadraticCurveTo(X1, top, X1, top + 3); }
      g.lineTo(X1, bottom - 3); g.quadraticCurveTo(X1, bottom, X1 - 3, bottom);
      g.lineTo(X0 + 3, bottom); g.quadraticCurveTo(X0, bottom, X0, bottom - 3);
      g.closePath();
      g.lineWidth = 4; g.strokeStyle = HALO; g.stroke();
      if (filled) { g.fillStyle = col; g.fill(); }
      else { g.fillStyle = dark; g.fill(); g.lineWidth = 2.5; g.strokeStyle = col; g.stroke(); }
      g.fillStyle = filled ? dark : col;      // the windows
      if (cab) g.fillRect(X0 + 4, top + 8, X1 - X0 - 8, 4);
      for (let y = top + (cab ? 18 : 6); y + 4 <= bottom - 5; y += 8) g.fillRect(CX - 2, y, 4, 4);
    };
    for (let i = 0; i < n; i++) body(TOP + i * CAR, TOP + i * CAR + CAR - 3, i === 0);
    return g.getImageData(0, 0, W, H);
  }
  const iconName = (t) => `tr-${t.estimated ? "est" : "rep"}-${Math.max(0, Math.min(MAX_CARS, t.cars | 0))}`;
  function ensureIcon(map, t) {
    const name = iconName(t);
    if (!map.hasImage(name)) map.addImage(name, trainIcon(!t.estimated, t.cars), { pixelRatio: 2 });
    return name;
  }
  // To scale (#164): a car is an M8's 85 ft, so from SCALE_FROM in the train is as long on the map as on the
  // ground; zoomed further out it keeps MIN_SIZE so it can still be seen. With ["exponential", 2] on zoom the size
  // between the two stops is exactly the ground's (metres per pixel halve with each zoom), and so is anything that
  // grows with it, as the label's offset does.
  const M_PER_CAR = 25.9, LAT = 41.0, MIN_SIZE = 0.5, SCALE_TO = 22;
  const MPP0 = (40075016.686 * Math.cos((LAT * Math.PI) / 180)) / 512;          // metres per pixel at zoom 0
  const sizeAt = (z) => (M_PER_CAR * 2 ** z) / MPP0 / (CAR / 2);               // a car is CAR / 2 css px at size 1
  const SCALE_FROM = Math.log2((MIN_SIZE * (CAR / 2) * MPP0) / M_PER_CAR);      // where sizeAt(z) = MIN_SIZE (~14)
  const ICON_SIZE = ["interpolate", ["exponential", 2], ["zoom"], SCALE_FROM, MIN_SIZE, SCALE_TO, sizeAt(SCALE_TO)];
  const TEXT_PX = 11;
  const carsOf = (t) => Math.max(1, Math.min(MAX_CARS, (t.cars | 0) || LOOK_CARS));
  // The label beside the train's middle, on the side its right hand is (so the two directions' labels part): the
  // anchor on the screen axis nearest that side, just clear of a train hl css px half long and hw half wide (laid
  // along that axis), in ems.
  function labelPlace(bearing, hl, hw) {
    const b = ((Number(bearing) || 0) * Math.PI) / 180;
    const nx = Math.cos(b), ny = Math.sin(b);  // the train's right (screen x east, y down)
    const alongY = Math.abs(ny) >= Math.abs(nx);
    const ext = alongY ? hl * Math.abs(nx) + hw * Math.abs(ny) : hl * Math.abs(ny) + hw * Math.abs(nx);
    const r = (ext + 4) / TEXT_PX;
    if (alongY) return ny > 0 ? ["top", [0, r]] : ["bottom", [0, -r]];
    return nx > 0 ? ["left", [r, 0]] : ["right", [-r, 0]];
  }
  const iconHalf = (t, size) => [(size * (TOP * 2 + carsOf(t) * CAR - 3)) / 4, (size * W) / 4];   // css px
  const trackHalf = (t, z) => [(carsOf(t) * M_PER_CAR) / 2 / (MPP0 / 2 ** z), 4];

  // ---- the cars along the track (#164): the train is as long on the map as on the ground and bends with the track;
  // its width stays legible. Each direction on its own track (the hub's lanes: the outer track each way, from
  // OpenStreetMap, measured in the line's own s), so an inbound and an outbound train never share one line; a train
  // is laid back from its front (t.s) along its lane, the way it came. One smooth body for the whole consist (no
  // joints to kink on a curve), the cab's rounded nose over its front, and thin gaps across it at each car's end from
  // street zoom (GAP_ZOOM); zoomed out, one solid body. Without lanes (or s), the nearest point of the line itself;
  // a train no line passes within NEAR_M of keeps its icon (above).
  const GAP_M = 1.2, GAP_ZOOM = 16, NEAR_M = 100, R_EARTH = 6371008.8, RAD = Math.PI / 180;
  const tracks = new WeakMap();
  function trackOf(coords) {                    // a line in local metres, and where along it each point is
    if (!coords || coords.length < 2) return { xy: null };
    let tr = tracks.get(coords);
    if (tr) return tr;
    const k = Math.cos(coords[Math.floor(coords.length / 2)][1] * RAD);
    const xy = coords.map(([lon, lat]) => [lon * RAD * R_EARTH * k, lat * RAD * R_EARTH]);
    let at = [0];                               // a lane: the line's s at each point; the line: metres along it
    if (coords[0].length > 2) at = coords.map((c) => c[2]);
    else for (let i = 1; i < xy.length; i++) at.push(at[i - 1] + Math.hypot(xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1]));
    tr = { xy, at, k };
    tracks.set(coords, tr);
    return tr;
  }
  const toLonLat = (tr, [x, y]) => [x / (R_EARTH * tr.k * RAD), y / (R_EARTH * RAD)];
  function along(tr, s) {                       // the point s along the line
    const at = tr.at, xy = tr.xy;
    s = Math.max(at[0], Math.min(at[at.length - 1], s));
    let lo = 1, hi = at.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (at[mid] < s) lo = mid + 1; else hi = mid; }
    const i = lo;
    const f = at[i] > at[i - 1] ? (s - at[i - 1]) / (at[i] - at[i - 1]) : 0;
    return [i, [xy[i - 1][0] + f * (xy[i][0] - xy[i - 1][0]), xy[i - 1][1] + f * (xy[i][1] - xy[i - 1][1])]];
  }
  function piece(tr, a, b) {                    // the line from a to b along it, as lon/lat
    const [i, p] = along(tr, a), [j, q] = along(tr, b);
    const out = [p];
    for (let k = i; k < j; k++) out.push(tr.xy[k]);
    out.push(q);
    return out.map((c) => toLonLat(tr, c));
  }
  // where the train's front is on which track, and which way its cars lie: its own lane at its s; else the nearest
  // point of the line, the cars back the way its bearing says it came
  function frontOf(t, line, lanes, tracks) {
    const own = runAt(tracks, t.lane, t.s);                         // its numbered track's run there (#371)
    const lane = own || (lanes && (t.d > 0 ? lanes.outbound : t.d < 0 ? lanes.inbound : null));
    const tr = trackOf(lane);
    if (tr.xy && typeof t.s === "number" && t.s >= tr.at[0] && t.s <= tr.at[tr.at.length - 1]) return { tr, s: t.s, back: -Math.sign(t.d) };
    const lt = trackOf(line);
    if (!lt.xy) return null;
    const P = [t.lon * RAD * R_EARTH * lt.k, t.lat * RAD * R_EARTH];
    let best = null;
    for (let i = 1; i < lt.xy.length; i++) {
      const [ax, ay] = lt.xy[i - 1], [bx, by] = lt.xy[i], dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
      const f = L2 ? Math.max(0, Math.min(1, ((P[0] - ax) * dx + (P[1] - ay) * dy) / L2)) : 0;
      const d = Math.hypot(ax + f * dx - P[0], ay + f * dy - P[1]);
      if (!best || d < best.d) best = { d, s: lt.at[i - 1] + f * Math.sqrt(L2), dx, dy };
    }
    if (!best || best.d > NEAR_M) return null;
    const segB = Math.atan2(best.dx, best.dy), b = (Number(t.bearing) || 0) * RAD;
    const back = Math.cos(segB - b) >= 0 ? -1 : 1;           // going up the line: its cars lie down the line
    return { tr: lt, s: best.s + (t.ds || 0), back };         // (ds: carried on along the line since the hub's s)
  }
  // {cars: [features], mid: [lon, lat]} for a train on a track, or null
  function onTrack(t, line, lanes, tracks) {
    const f = frontOf(t, line, lanes, tracks);
    if (!f) return null;
    const { tr, s, back } = f, n = carsOf(t), len = n * M_PER_CAR;
    const part = (a, b, kind) => ({ type: "Feature", geometry: { type: "LineString", coordinates: piece(tr, Math.min(a, b), Math.max(a, b)) },
                                    properties: { id: t.id, estimated: !!t.estimated, stale: !!t.stale, kind } });
    const cars = [part(s, s + back * len, "body"), part(s, s + back * M_PER_CAR, "cab")];
    for (let k = 1; k < n; k++) cars.push(part(s + back * (k * M_PER_CAR - GAP_M / 2), s + back * (k * M_PER_CAR + GAP_M / 2), "gap"));
    return { cars, mid: toLonLat(tr, along(tr, s + (back * len) / 2)[1]) };
  }

  const towards = (t) => (t.headsign ? `to ${t.headsign}` : t.dir_label || "");

  // ---- MTA TrainTime in the train's box (#362): one data line, the cars as load bars (details on hover) ------------
  const LOADS = { EMPTY: [1, "Empty"], MANY_SEATS: [2, "Many seats"], FEW_SEATS: [3, "Few seats"], SRO: [4, "Standing"], NO_DATA: [0, ""] };
  const routes = new Map();                   // route key -> its line's runs (the hub's, clipped to our area)
  function headline(t) {                      // "NH east · 6 cars (M8) · 11 min late · MP 25.4 · next Port Chester trk 2"
    const tt = t.tt || {};
    const dir = t.d > 0 ? "east" : t.d < 0 ? "west" : "";
    const fleet = tt.fleet ? (/DIESEL/.test(tt.fleet) ? "diesel" : tt.fleet) : "";
    const n = tt.cars, sched = tt.cars_sched && tt.cars_sched !== n ? `/${tt.cars_sched}` : "";
    const cars = n ? `${n}${sched} car${n === 1 && !sched ? "" : "s"}${fleet ? ` (${fleet})` : ""}` : "";
    const nx = tt.next;
    const trk = nx && nx.track ? ` trk\u00a0${nx.track}${nx.posted ? "" : " planned"}` : "";
    return [[tt.branch, dir].filter(Boolean).join(" "), cars, late(tt.late_s != null ? tt.late_s : t.delay_s),
            nx ? `next ${nx.name}${trk}` : t.next_stop ? `next ${t.next_stop}` : ""]
      .filter(Boolean).join(" · ");
  }
  // ---- the numbered tracks, poles and mileposts (#371) ------------------------------------------------------------
  // a numbered track's run covering s ([[lon, lat, s]...]), or null (a gap in it, or no such track)
  function runAt(tracks, k, s) {
    const runs = tracks && k ? tracks[k] : null;
    if (!runs || typeof s !== "number") return null;
    return runs.find((r) => r.length > 1 && s >= r[0][2] && s <= r[r.length - 1][2]) || null;
  }
  // the pole and milepost at s along the line (the hub's railref: [[s, id]], [[s, mp]]): {pole, mp}, each when known
  const POLE_M = 150, MP_SLACK_M = 400;
  function refAt(rr, s) {
    const out = {};
    if (!rr || typeof s !== "number") return out;
    let best = null;
    for (const [ps, id] of rr.poles || []) if (Math.abs(ps - s) <= POLE_M && (!best || Math.abs(ps - s) < Math.abs(best[0] - s))) best = [ps, id];
    if (best) out.pole = best[1];
    const m = rr.mps || [];
    if (m.length > 1 && s >= m[0][0] - MP_SLACK_M && s <= m[m.length - 1][0] + MP_SLACK_M) {
      let i = 1;
      while (i < m.length - 1 && m[i][0] < s) i++;
      const [s0, v0] = m[i - 1], [s1, v1] = m[i];
      out.mp = Math.round((s1 > s0 ? v0 + ((v1 - v0) * (s - s0)) / (s1 - s0) : v0) * 10) / 10;
    }
    return out;
  }
  // "Trk 2 · near pole 249 · MP 25.3" for a train drawn at s (the pole and milepost where it is drawn now, not where
  // the hub last placed it); who said the track, and for which stop, on hover. placeRow: the line as data ({k, text,
  // title}, or null: a box's row); placeLine: as an element.
  const TRK_WHO = { posted: "posted", planned: "planned", mta: "MTA" };
  function placeRow(t, s, rr) {
    const at = rr && rr.poles ? refAt(rr, s) : { pole: t.pole, mp: t.mp };
    const text = [t.trk ? `Trk\u00a0${t.trk}` : "", at.pole ? `near pole ${at.pole}` : "",
                  typeof at.mp === "number" ? `MP\u00a0${at.mp.toFixed(1)}` : ""].filter(Boolean).join(" · ");
    if (!text) return null;
    const title = [t.trk ? `Track ${t.trk}: ${TRK_WHO[t.trk_src] || "the MTA's"} for ${t.trk_at || "its stop"}` : "",
                   at.pole ? `Pole: CB ${at.pole} (catenary structure, Metro-North GIS)` : "",
                   typeof at.mp === "number" ? "Milepost from Grand Central" : ""].filter(Boolean).join("\n");
    return { k: "place", text, title };
  }
  function placeLine(t, s, rr) {
    const r = placeRow(t, s, rr);
    if (!r) return null;
    const n = h("div", "", r.text);
    n.title = r.title;
    return n;
  }
  function refText(ref) {                     // the event's panel (#371): "pole 249 · MP 25.3"
    return [ref.pole ? `pole ${ref.pole}` : "", typeof ref.mp === "number" ? `MP ${ref.mp.toFixed(1)}` : ""].filter(Boolean).join(" · ");
  }
  const refStore = { data: null, loading: null, at: 0 };
  function loadRef() {                        // the poles and mileposts: once while the box is on (they change monthly)
    if (refStore.loading || (refStore.data && Date.now() - refStore.at < 6 * 3600e3)) return refStore.loading || Promise.resolve();
    refStore.loading = fetch(RAILREF, { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null))
      .then((j) => { if (j) { refStore.data = j; refStore.at = Date.now(); } })
      .catch(() => { /* the hub is away: the box asks again when turned on */ })
      .finally(() => { refStore.loading = null; for (const m of maps) m.sync(); });
    return refStore.loading;
  }
  function carBars(tt) {
    const cars = tt.car_loads || [];
    if (!cars.length) return null;
    const row = h("div", "tr-load"), bars = h("span", "tr-cars");
    cars.forEach((c, i) => {
      const [lv, word] = LOADS[c.load] || [0, ""];
      const b = h("span", c.loco ? "tr-car loco" : ["tr-car", "tr-car l1", "tr-car l2", "tr-car l3", "tr-car l4"][lv] || "tr-car");
      b.append(h("span"));
      b.title = [`Car ${i + 1}`, [c.type, c.n].filter((x) => x !== null && x !== undefined && x !== "").join(" "),
                 c.loco ? "locomotive" : word || "no load data", c.pax != null && !c.loco ? `${c.pax} aboard` : ""].filter(Boolean).join(" · ");
      bars.append(b);
    });
    const occ = (LOADS[tt.occupancy] || [0, ""])[1];
    row.append(bars, h("span", "small", [occ, tt.pax != null ? `~${tt.pax} aboard` : ""].filter(Boolean).join(" · ")));
    return row;
  }

  class TrainMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.added = false;
      maps.add(this);
      store.subs.add(() => this.sync());
      this.map.on("click", (e) => this.clicked(e));
      if (this.isOn()) kick();
      else if (this.isRefOn()) this.sync();     // the poles and mileposts alone (#371)
    }
    isOn() { return this.control.on(KEY, false); }
    isRefOn() { return this.control.on(REF_KEY, false); }
    keys() { return [[KEY, false], [REF_KEY, false]]; }
    refresh() { this.sync(); kick(); }
    ready() { try { return this.added || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }

    add() {
      const map = this.map;
      if (map.getSource(SRC_TR)) { this.added = true; return; }
      const glyphs = !!(map.getStyle() && map.getStyle().glyphs);
      const ids = (map.getStyle().layers || []).map((x) => x.id);
      const before = ids.find((id) => id.startsWith("ml-") || id === "cm-lines" || id === "firehouse");
      const line = token("--text-2", "#C9CED4"), text = token("--text", "#E6E8EB"), warn = token("--warn", "#E0A030");
      const attribution = '<a href="https://www.mta.info/developers" target="_blank" rel="noopener">MTA</a>';
      map.addSource(SRC_LINE, { type: "geojson", data: fc([]), attribution });
      map.addSource(SRC_ST, { type: "geojson", data: fc([]) });
      map.addSource(SRC_TR, { type: "geojson", data: fc([]) });
      map.addSource(SRC_CARS, { type: "geojson", data: fc([]) });
      map.addSource(SRC_ROUTE, { type: "geojson", data: fc([]) });
      ensureIcon(map, { estimated: false });
      ensureIcon(map, { estimated: true });
      const label = (field, size, extra) => (glyphs ? { "text-field": field, "text-font": ["Noto Sans Bold"], "text-size": size,
                                                        "text-optional": true, ...extra } : {});
      const vis = { visibility: "none" };
      map.addLayer({ id: "tr-line", type: "line", source: SRC_LINE, layout: { ...vis, "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": line, "line-opacity": 0.8,     // four tracks 4 m apart (#371): thin enough to tell apart
                              "line-width": ["interpolate", ["linear"], ["zoom"], 10, ["case", ["has", "trk"], 1, 1.5],
                                             15, ["case", ["has", "trk"], 1.2, 3], 18, ["case", ["has", "trk"], 2, 3]] } }, before);
      map.addLayer({ id: "tr-line-label", type: "symbol", source: SRC_LINE, filter: ["has", "trk"], minzoom: 17,      // its number
                     layout: { ...vis, ...label(["get", "trk"], 10, { "symbol-placement": "line", "symbol-spacing": 400 }) },
                     paint: { "text-color": line, "text-halo-color": HALO, "text-halo-width": 1.4 } }, before);
      // the clicked train's own route (MTA TrainTime's line, #362) while its box is open
      map.addLayer({ id: "tr-route", type: "line", source: SRC_ROUTE, layout: { ...vis, "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": token("--info", "#5AA9E6"), "line-width": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 15, 5],
                              "line-opacity": 0.85 } }, before);
      map.addLayer({ id: "tr-station", type: "circle", source: SRC_ST, layout: vis,
                     paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 3, 15, 6], "circle-color": line,
                              "circle-stroke-color": HALO, "circle-stroke-width": 1.5 } }, before);
      map.addLayer({ id: "tr-station-label", type: "symbol", source: SRC_ST, minzoom: 12,
                     layout: { ...vis, ...label(["get", "name"], 11, { "text-anchor": "top", "text-offset": [0, 0.8] }) },
                     paint: { "text-color": line, "text-halo-color": HALO, "text-halo-width": 1.5 } }, before);
      // a train on its track: one body along it, to scale; filled when the MTA reported it, an outline when it is our
      // estimate; the front car's nose rounded (the cab); the cars' ends as thin gaps across it from GAP_ZOOM. Every
      // outline under every body, so the cab's outline never shows on the car behind it.
      const dark = token("--bg", HALO);
      const fade = ["case", ["get", "stale"], STALE_OPACITY, 1];        // frozen on an old feed: faded
      const carW = ["interpolate", ["linear"], ["zoom"], 10, 4, 15, 6, 17, 9, 19, 28];
      const caseW = ["interpolate", ["linear"], ["zoom"], 10, 7, 15, 9, 17, 12, 19, 32];
      const part = (kind) => ["==", ["get", "kind"], kind];
      const lay = (cap) => ({ ...vis, "line-cap": cap, "line-join": "round" });
      map.addLayer({ id: "tr-car-case", type: "line", source: SRC_CARS, filter: part("body"), layout: lay("butt"),
                     paint: { "line-color": ["case", ["get", "estimated"], warn, HALO], "line-width": caseW, "line-opacity": fade } }, before);
      map.addLayer({ id: "tr-cab-case", type: "line", source: SRC_CARS, filter: part("cab"), layout: lay("round"),
                     paint: { "line-color": ["case", ["get", "estimated"], warn, HALO], "line-width": caseW, "line-opacity": fade } }, before);
      map.addLayer({ id: "tr-car", type: "line", source: SRC_CARS, filter: part("body"), layout: lay("butt"),
                     paint: { "line-color": ["case", ["get", "estimated"], dark, warn], "line-width": carW, "line-opacity": fade } }, before);
      map.addLayer({ id: "tr-cab", type: "line", source: SRC_CARS, filter: part("cab"), layout: lay("round"),
                     paint: { "line-color": ["case", ["get", "estimated"], dark, warn], "line-width": carW, "line-opacity": fade } }, before);
      map.addLayer({ id: "tr-gap", type: "line", source: SRC_CARS, filter: part("gap"), minzoom: GAP_ZOOM, layout: lay("butt"),
                     paint: { "line-color": ["case", ["get", "estimated"], warn, HALO], "line-width": carW, "line-opacity": fade } }, before);
      // a train off the line: an icon lying flat along its heading, to scale once zoomed in (ICON_SIZE)
      map.addLayer({ id: "tr-train", type: "symbol", source: SRC_TR, filter: ["!", ["get", "onTrack"]],
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-rotate": ["get", "bearing"],
                               "icon-rotation-alignment": "map", "icon-pitch-alignment": "map",
                               "icon-allow-overlap": true, "icon-ignore-placement": true,
                               "icon-size": ICON_SIZE }, paint: { "icon-opacity": fade } }, before);
      // its number from zoom 11; where it is headed under it from 13 (the whole stretch would be a crowd of words)
      const num = ["format", ["get", "train"], {}];
      const both = ["format", ["get", "train"], {}, "\n", {}, ["get", "towards"], { "font-scale": 0.85 }];
      map.addLayer({ id: "tr-train-label", type: "symbol", source: SRC_TR, minzoom: 11,
                     layout: { ...vis, ...label(["step", ["zoom"], num, 13, both], 11,
                                                { "text-anchor": ["get", "anchor"],                          // clear of the train as it grows
                                                  "text-offset": ["interpolate", ["exponential", 2], ["zoom"],
                                                                  SCALE_FROM, ["array", "number", 2, ["get", "off_lo"]],
                                                                  SCALE_TO, ["array", "number", 2, ["get", "off_hi"]]] }) },
                     paint: { "text-color": ["case", ["get", "estimated"], text, warn], "text-halo-color": HALO, "text-halo-width": 1.6, "text-opacity": fade } }, before);
      // the catenary structures' numbers and the mileposts (#371): poles from street zoom, whole miles from 12, tenths from 15
      map.addSource(SRC_REF, { type: "geojson", data: fc([]), attribution: "Metro-North GIS" });
      const isMp = ["==", ["get", "kind"], "mp"], isPole = ["==", ["get", "kind"], "pole"];
      const mpZoom = ["any", ["get", "whole"], [">=", ["zoom"], 15]];
      map.addLayer({ id: "tr-pole", type: "circle", source: SRC_REF, filter: isPole, minzoom: 15, layout: vis,
                     paint: { "circle-radius": 2.5, "circle-color": line, "circle-stroke-color": HALO, "circle-stroke-width": 1 } }, before);
      map.addLayer({ id: "tr-pole-label", type: "symbol", source: SRC_REF, filter: isPole, minzoom: 16,
                     layout: { ...vis, ...label(["get", "id"], 10, { "text-anchor": "left", "text-offset": [0.5, 0] }) },
                     paint: { "text-color": line, "text-halo-color": HALO, "text-halo-width": 1.4 } }, before);
      map.addLayer({ id: "tr-mp", type: "circle", source: SRC_REF, filter: ["all", isMp, ["get", "whole"]], minzoom: 12, layout: vis,
                     paint: { "circle-radius": 3, "circle-color": warn, "circle-stroke-color": HALO, "circle-stroke-width": 1 } }, before);
      map.addLayer({ id: "tr-mp-label", type: "symbol", source: SRC_REF, filter: ["all", isMp, mpZoom], minzoom: 12,
                     layout: { ...vis, ...label(["get", "label"], 10, { "text-anchor": "right", "text-offset": [-0.6, 0] }) },
                     paint: { "text-color": warn, "text-halo-color": HALO, "text-halo-width": 1.4 } }, before);
      for (const id of CLICKABLE) {
        map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
      }
      this.added = true;
    }

    sync() {
      if (!this.isOn() && !this.isRefOn() && !this.added) { this.panelChanged(); return; }
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      try { this.add(); } catch (e) { console.error("trains layer", e); return; }
      const d = store.data, on = this.isOn(), refOn = this.isRefOn();
      for (const id of IDS) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
      for (const id of REF_IDS) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", refOn ? "visible" : "none");
      if (refOn) this.syncRef();
      if (on && d) {
        if (d.line !== this.line) {             // the line and stations: only when the hub sent new ones
          this.line = d.line;
          this.lanes = d.lanes && d.lanes.outbound && d.lanes.inbound ? d.lanes : null;
          this.tracks = d.tracks && Object.keys(d.tracks).length ? d.tracks : null;   // the numbered tracks (#371)
          this.sig = "";                        // the cars lie along it: lay them again
          const ln = (coords, dir) => ({ type: "Feature", geometry: { type: "LineString", coordinates: coords.map((c) => [c[0], c[1]]) }, properties: { dir } });
          const drawn = this.tracks ? Object.entries(this.tracks).flatMap(([k, runs]) => runs.map((c) => ({ ...ln(c, k), properties: { trk: k } })))   // every numbered track
            : this.lanes ? [ln(this.lanes.outbound, "outbound"), ln(this.lanes.inbound, "inbound")]   // each way's own track
            : d.line && d.line.length > 1 ? [ln(d.line, "")] : [];
          this.map.getSource(SRC_LINE).setData(fc(drawn));
          this.map.getSource(SRC_ST).setData(fc((d.stations || []).map((s) => pt(s.lon, s.lat, { id: s.id, name: s.name }))));
        }
        if (d.trains !== this.trains) { this.trains = d.trains; this.sig = ""; }   // a new answer: draw it
        this.draw(true, Date.now());
      }
      this.panelChanged();
    }
    syncRef() {                                 // the poles and mileposts (#371): set once a copy is here
      const r = refStore.data;
      if (!r) { loadRef(); return; }
      if (this.refShown === r) return;
      this.refShown = r;
      this.map.getSource(SRC_REF).setData(fc([
        ...(r.poles || []).map((p) => pt(p.lon, p.lat, { kind: "pole", id: p.id })),
        ...(r.mileposts || []).map((m) => pt(m.lon, m.lat, { kind: "mp", whole: !!m.whole, label: m.whole ? `MP ${m.mp}` : m.mp.toFixed(1) }))]));
    }
    // The trains where motion.js has them now (each answer, then every ANIMATE_MS while one moves): each train's
    // cars rebuilt along its track at the drawn s; its label and click target with it. One setData for the cars and
    // one for the labels, and none when nothing drawn has changed.
    draw(force, now = Date.now()) {
      const d = store.data;
      if (!d || !this.added || !this.isOn()) return;
      if (!force && !this.map.getContainer().offsetParent) return;          // a map not on screen (the Expand closed)
      const pos = positions(now), drawn = new Map(), key = [];
      const old = !!d.stale || (typeof store.feedAt === "number"                            // the feed's age
                                && !((now - store.feedAt) / 1000 <= ((d.profile && d.profile.stale_s) || 60)));
      for (const t of d.trains || []) {
        const own = t.pos_src === "traintime" ? !!t.stale : old;              // each train by its own freshest source (#362)
        const p = pos.get(t.id), stale = motion && pos.has(t.id) ? motion.stale(t.id) : own;
        const s = p ? Math.round(p.s * 10) / 10 : t.s;
        // ahead of the MTA's report while on its way (a stopped train held at its station's mark isn't a prediction)
        drawn.set(t.id, { s, stale, ahead: !t.estimated && t.mode !== "stopped" && typeof t.s === "number" && (s - t.s) * t.d > PREDICTED_M });
        key.push(`${t.id}:${s}:${stale ? 1 : 0}`);
      }
      const sig = key.join("|");
      this.drawn = drawn;
      if (sig === this.sig) return;                                          // nothing moved: no setData
      this.sig = sig;
      const cars = [];
      this.map.getSource(SRC_TR).setData(fc((d.trains || []).map((x) => {
        const w = drawn.get(x.id), t = { ...x, s: w.s, ds: typeof x.s === "number" ? w.s - x.s : 0, stale: w.stale };
        const on = onTrack(t, this.line, this.lanes, this.tracks);
        if (on) cars.push(...on.cars);
        const half = on ? (z) => trackHalf(t, z) : (z) => iconHalf(t, z === SCALE_TO ? sizeAt(SCALE_TO) : MIN_SIZE);
        const [anchor, off_lo] = labelPlace(t.bearing, ...half(SCALE_FROM)), off_hi = labelPlace(t.bearing, ...half(SCALE_TO))[1];
        const [lon, lat] = on ? on.mid : [t.lon, t.lat];
        return pt(lon, lat, { id: t.id, train: t.train, towards: towards(t), bearing: t.bearing || 0, estimated: !!t.estimated,
                              stale: w.stale, onTrack: !!on, icon: on ? "" : ensureIcon(this.map, t), anchor, off_lo, off_hi });
      })));
      this.map.getSource(SRC_CARS).setData(fc(cars));
    }
    panelChanged() { if (!this.control.panel.hidden) this.control.renderPanel(); }
    drawnS(t) { const w = this.drawn && this.drawn.get(t.id); return w && typeof w.s === "number" ? w.s : t.s; }   // (#371)
    // where the train is drawn (#278: a short phrase on the box; the method in its "i"): reported (the MTA's time for
    // it), predicted (carried on past that report), estimated, or held on an old feed. No distances: nothing finer
    // than the data has.
    where(t, d) {
      const w = (this.drawn && this.drawn.get(t.id)) || {};
      const upTo = t.hold_kind === "stop" && t.next && t.next.name ? `not past ${t.next.name}` : "not past its next reporting point";
      const at = t.fix_at ? `${t.fix_age_unknown ? "by " : ""}${hm(t.fix_at, true)}` : "";
      let say, why;
      if (!t.estimated) {
        say = w.ahead ? `Predicted from ${at}` : `Reported ${at}${t.mode === "stopped" ? " · stopped" : ""}`;
        why = w.ahead ? `Predicted: on its run since the MTA's report, ${upTo}` : "Reported by the MTA";
      } else {
        say = t.mode === "stopped" ? "Estimated · at station" : "Estimated";
        why = t.mode === "stopped" ? "Estimated: at the station, the MTA hasn't said it left"
          : "Estimated: on its run between its stations, by the MTA's times";
      }
      if (w.stale) why += ". Feed out of date: held where it was";
      return { say: say.trim(), why, stale: w.stale ? `Stale${d.at ? ` · ${hm(d.at, true)}` : ""}` : "" };
    }

    clicked(e) {
      if (!this.isOn() || !this.added || !store.data) return;
      const layers = CLICKABLE.filter((id) => this.map.getLayer(id));
      const hits = this.map.queryRenderedFeatures(e.point, { layers });
      if (!hits.length) return;
      const f = hits.find((x) => TRAIN_HIT.includes(x.layer.id)) || hits.find((x) => x.layer.id === "tr-station") || hits[0];
      e.originalEvent && e.originalEvent.stopPropagation();
      setTimeout(() => this.show(f), 0);
    }

    show(f) {
      const d = store.data;
      const foot = ["MTA", d.at ? hm(d.at, true) : ""].filter(Boolean).join(" · ");
      const credit = `${d.label || "MTA"}. ${d.disclaimer || "Times may not be real time"}.`;
      const icon = window.Icon ? Icon("gis-train") : null;
      this.control.close();
      if (TRAIN_HIT.includes(f.layer.id)) {
        const t = (d.trains || []).find((x) => x.id === f.properties.id);
        if (!t) return;
        const lines = [];
        const pos = this.where(t, d);
        if (pos.stale) lines.push(h("div", "warn", pos.stale));      // #278: up top, in the warning colour
        if (t.tt) return this.showTT(t, d, pos, icon, credit);       // MTA TrainTime's facts (#362)
        lines.push([t.dir_label, t.next_stop ? `Next ${t.next_stop}${t.next_time ? ` ${hm(t.next_time)}` : ""}` : "",
                    t.track && t.track !== t.trk ? `Trk ${t.track}` : ""]
          .filter(Boolean).join(" · "));
        const said = (t.status || "").toLowerCase();         // the MTA's word, unless the delay already says it
        const status = [late(t.delay_s), said && said !== "on-time" && !(said === "late" && t.delay_s > 0) ? t.status : "",
                        pos.say, t.cars ? `${t.cars} car${t.cars === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ");
        if (status) lines.push(status);
        const place = placeLine(t, this.drawnS(t), d.railref);
        if (place) lines.push(place);
        lines.push(d.catenary);
        const tip = [pos.why, t.cars ? "Cars: from the MTA" : `Cars: not given by the MTA (drawn as ${LOOK_CARS})`, credit].join("\n");
        this.control.showInfo(`Train ${t.train}`, `${t.line ? `${t.line} Line` : "Metro-North"} → ${t.headsign}`, lines, foot, "", icon, tip);
        return;
      }
      if (f.layer.id === "tr-station") {
        const s = (d.stations || []).find((x) => x.id === f.properties.id);
        const tto = d.traintime && d.traintime.state !== "off";
        const deps = s && tto && DEP_STATIONS.includes(s.code) ? h("div", "tr-deps", "Departures…") : null;
        this.control.showInfo(s ? `${s.name} station` : "Station", "New Haven Line", [...(deps ? [deps] : []), d.catenary],
                              deps ? "MTA TrainTime" : foot, "", icon, credit);
        if (deps) { const stop = this.departures(s.code, deps); this.watchInfo(deps, stop); }
        return;
      }
      this.control.showInfo("New Haven Line", "Metro-North", [d.catenary], foot, "", icon, credit);
    }

    // A train with MTA TrainTime's facts (#362): one data line, held / cancelled up top, the cars' load as bars, the
    // branch's alerts; its route drawn while the box is open. Source and time in the footer, the rest on hover.
    showTT(t, d, pos, icon, credit) {
      const tt = t.tt, lines = [];
      if (pos.stale) lines.push(h("div", "warn", pos.stale));
      if (tt.canceled || tt.held) lines.push(h("div", "warn", tt.canceled ? "Cancelled" : "Held"));
      const head = h("div", "", headline(t));
      if (tt.next && tt.next.track) head.title = `Track ${tt.next.track} at ${tt.next.name}: ${tt.next.posted ? "posted" : "planned, not posted yet"}`
        + (tt.next.track_change ? " (track change)" : "");
      lines.push(head);
      const place = placeLine(t, this.drawnS(t), d.railref);
      if (place) lines.push(place);
      const spd = typeof tt.speed_mph === "number" && t.pos_src === "traintime" ? `${Math.round(tt.speed_mph)} mph` : "";
      lines.push([pos.say.replace(/ (by )?\d\d:\d\d(:\d\d)?/, ""), spd].filter(Boolean).join(" · "));   // its time: in the footer
      const bars = carBars(tt);
      if (bars) lines.push(bars);
      const al = d.alerts || [];
      if (al.length) {
        const a = h("div", "tr-alert", `${al[0].status ? `${al[0].status}: ` : ""}${al[0].header}${al.length > 1 ? ` (+${al.length - 1})` : ""}`);
        a.title = al.map((x) => `${x.status ? `${x.status}: ` : ""}${x.header}`).join("\n");
        lines.push(a);
      }
      lines.push(d.catenary);
      const gps = t.pos_src === "traintime";
      const foot = [gps ? `MTA TrainTime (${(tt.loc_src || "").toLowerCase() || "?"})` : "MTA", hm(gps ? tt.loc_at : d.at, true)].filter(Boolean).join(" · ");
      const tip = [pos.why, gps ? "Position, speed and cars: MTA TrainTime" : "Position: MTA GTFS-realtime; cars: MTA TrainTime",
                   "TrainTime is an undocumented MTA API; GTFS-realtime is used when it fails", credit].join("\n");
      this.control.showInfo(`Train ${t.train}`, `${t.line ? `${t.line} Line` : "Metro-North"}${t.headsign ? ` → ${t.headsign}` : ""}`,
                            lines, foot, "", icon, tip);
      this.showRoute(tt.route);
      this.watchInfo(head, () => this.showRoute(""));
    }
    // the box closed, or shows something else now: fn() once
    watchInfo(node, fn) {
      const info = this.control.info;
      if (!info || !window.MutationObserver) return;
      const mo = new MutationObserver(() => { if (info.hidden || !info.contains(node)) { mo.disconnect(); fn(); } });
      mo.observe(info, { attributes: true, attributeFilter: ["hidden"], childList: true });
    }
    showRoute(key) {
      const src = this.map.getSource(SRC_ROUTE);
      this.routeKey = key || "";
      if (!src) return;
      const draw = (runs) => {
        if (this.routeKey !== key) return;
        src.setData(fc((runs || []).map((r) => ({ type: "Feature", geometry: { type: "LineString", coordinates: r }, properties: {} }))));
      };
      if (!key || this.tracks) { src.setData(fc([])); return; }   // the real tracks drawn (#371): a route line isn't one
      if (routes.has(key)) { draw(routes.get(key)); return; }
      fetch(`${ROUTE}?key=${encodeURIComponent(key)}`, { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => { if (j) { if ((j.runs || []).length) routes.set(key, j.runs); draw(j.runs); } })
        .catch(() => { /* no line: the box still says it all */ });
    }
    // a station's next departures (MTA TrainTime /countdown), every 30 s while its box is open; a stop function
    departures(code, box) {
      let timer = null, stopped = false;
      const row = (x) => {
        const r = h("div", "", [hm(x.est), x.train, x.to_name, x.track ? `trk ${x.track}${x.posted ? "" : " planned"}` : "",
                               x.canceled ? "cancelled" : x.held ? "held" : late(x.late_s), x.cars ? `${x.cars} cars` : ""]
          .filter(Boolean).join(" · "));
        r.title = [`Scheduled ${hm(x.sched)}`, x.dir === "E" ? "eastbound" : "westbound", x.fleet,
                   (LOADS[x.occupancy] || [0, ""])[1]].filter(Boolean).join(" · ");
        return r;
      };
      const load = async () => {
        let j = null;
        try {
          const r = await fetch(`${DEPS}?station=${encodeURIComponent(code)}`, { credentials: "same-origin" });
          j = r.ok ? await r.json() : null;
        } catch (_) { /* the hub is away: try again on the next turn */ }
        if (stopped) return;
        if (j) {
          const rows = (j.rows || []).slice(0, 6).map(row);
          box.replaceChildren(...(rows.length ? rows : [h("div", "dim", j.at ? "None in the next hour" : j.error ? `Departures: ${j.error}` : "Departures…")]));
        }
        timer = setTimeout(load, j && j.at ? PANEL_MS : 3000);
      };
      load();
      return () => { stopped = true; clearTimeout(timer); };
    }

    // the group in the Layers box
    panel() {
      const d = store.data;
      const box = h("div", "ml-group");
      const head = h("div", "ml-ghead");
      head.append(h("b", "", "Trains"));
      const src = h("a", "dim small", "MTA");
      src.href = "https://www.mta.info/developers";
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
      const mark = window.Icon ? Icon("gis-train") : null;
      lab.append(cb, ...(mark ? [mark] : []), h("span", "", "Metro-North"));
      lab.title = "New Haven Line";
      box.append(lab);
      const rlab = h("label", "check"), rcb = h("input");          // the poles and mileposts (#371)
      rcb.type = "checkbox";
      rcb.checked = this.isRefOn();
      rcb.dataset.key = REF_KEY;
      rcb.addEventListener("change", () => { this.control.setLayer(REF_KEY, rcb.checked); this.sync(); });
      rlab.append(rcb, h("span", "", "Poles · mileposts"));
      const r = refStore.data;
      rlab.title = ["Catenary structure numbers and mileposts", r ? `${(r.poles || []).length} poles · ${(r.mileposts || []).length} mileposts` : "",
                    r && r.error ? r.error : "", "Metro-North GIS"].filter(Boolean).join("\n");
      box.append(rlab);
      if (!this.isOn()) return box;
      if (!d) { box.append(h("div", "ml-note", "Loading…")); return box; }
      if (!d.configured || d.note) { box.append(h("div", "ml-note", cap(d.note) || "Not configured")); return box; }
      const n = (d.trains || []).length;
      const span = (d.stations || []).length > 1 ? ` · ${d.stations[0].name}–${d.stations[d.stations.length - 1].name}` : "";
      box.append(h("div", "ml-note", `${n} train${n === 1 ? "" : "s"}${span}`));
      // the legend on screen (hollow, faded = stale); the method behind an "i" (a tap on a tablet)
      const legend = h("div", "ml-note", "Hollow: estimated · faded: stale, held ");
      const how = "Between reports a train runs on along its track, never past its next reporting point or stop; "
        + "faded: the feed is out of date and it is held where it was. "
        + ((d.trains || []).some((t) => t.cars) ? "Cars as the MTA gives them." : `Drawn as ${LOOK_CARS} cars (the MTA doesn't say).`)
        + ` ${d.disclaimer || ""}.`;
      if (window.Help && Help.info) legend.append(Help.info(how, "How trains are drawn")); else legend.title = how;
      box.append(legend);
      const note = h("div", "ml-note");
      note.append(["MTA", d.at ? hm(d.at, true) : ""].filter(Boolean).join(" · "), " ");
      if (d.error) note.append(`(${d.error}) `);
      const terms = h("a", "", "Terms");
      terms.href = d.terms || "https://www.mta.info/developers/terms-and-conditions";
      terms.target = "_blank";
      terms.rel = "noopener";
      note.append(terms);
      box.append(note);
      return box;
    }
  }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new TrainMap(control) });

  // ---- the event's "Trains approaching" panel ---------------------------------------------------------------------
  const panels = new Map();               // host -> {inc, key, data, open, timer}
  const isOpenEvent = (inc) => !["closed", "dismissed"].includes(inc.status);
  function panel(inc, host) {
    if (!host) return;
    let st = panels.get(host);
    if (!st) {
      st = { inc: null, key: "", data: null, open: true, timer: null, seq: 0 };
      panels.set(host, st);
      st.timer = setInterval(() => {
        if (st.key && document.visibilityState !== "hidden" && !host.hidden) ask(st, host);
      }, PANEL_MS);
    }
    st.inc = inc || null;
    const ok = inc && inc.lat != null && inc.lon != null && isOpenEvent(inc);
    const key = ok ? JSON.stringify([inc.id, inc.lat, inc.lon]) : "";
    if (!ok) { st.key = ""; st.data = null; host.hidden = true; host.replaceChildren(); return; }
    if (key === st.key) return;
    st.key = key;
    st.data = null;
    host.hidden = true;
    ask(st, host);
  }
  function ask(st, host) {
    const inc = st.inc, key = st.key, seq = ++st.seq;
    if (!inc || !key) return;
    const p = new URLSearchParams({ lat: String(inc.lat), lon: String(inc.lon) });
    fetch(`${NEAR}?${p}`, { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (st.key !== key || seq !== st.seq) return;
      st.data = d;
      draw(st, host);
    }).catch(() => { /* keep the last answer; the next turn asks again */ });
  }
  function where(ref) {
    if (ref.kind === "station") return `at ${ref.name} station`;
    return `on the tracks ${ref.station_m >= 50 ? `${Math.round(ref.station_m / 10) * 10} m ${ref.side} of ${ref.name} station` : `by ${ref.name} station`}`;
  }
  function draw(st, host) {
    const d = st.data;
    if (!d || !d.near) { host.hidden = true; host.replaceChildren(); return; }
    host.hidden = false;
    const head = h("button", "ml-cams-head");
    head.type = "button";
    head.setAttribute("aria-expanded", String(st.open));
    if (window.Icon) head.append(Icon("gis-train"));
    head.append(h("b", "", "Trains approaching"), h("span", "dim small", [where(d.ref), refText(d.ref)].filter(Boolean).join(" · ")));
    head.append(h("span", "spacer"), h("span", "dim small", "MTA"), h("span", "car dim", st.open ? "▾" : "▸"));
    head.addEventListener("click", () => { st.open = !st.open; draw(st, host); });
    host.replaceChildren(head);
    if (!st.open) return;
    if (!d.configured || d.note) { host.append(h("div", "ml-note tr-note", cap(d.note) || "Not configured")); return; }
    const labels = d.dir_labels || { inbound: "Toward New York", outbound: "Toward New Haven" };
    const grid = h("div", "tr-dirs");
    for (const dir of ["inbound", "outbound"]) {
      const col = h("div", "tr-dir");
      col.append(h("div", "ml-cams-sub dim small", labels[dir]));
      const list = d[dir] || [];
      if (!list.length) col.append(h("div", "tr-none dim small", `None in ${d.window_min || 15} min`));
      for (const t of list) {
        const row = h("div", "tr-row");
        row.append(h("span", "tr-min", t.min <= 0 ? "now" : `${t.min} min`));
        const what = h("span", "tr-what");
        what.append(h("b", "", `Train ${t.train}`), ` to ${t.headsign}`);
        const facts = [t.stops ? `stops${t.track ? ` · trk ${t.track}` : ""}` : "no stop", `${hm(t.eta)}`, late(t.delay_s)].filter(Boolean);
        what.append(h("span", "dim small", ` · ${facts.join(" · ")}`));
        if (t.estimated) { const e = h("span", "tr-est small", "est."); e.title = "Estimated from the MTA's times at the stations either side"; what.append(" ", e); }
        row.append(what);
        col.append(row);
      }
      grid.append(col);
    }
    host.append(grid);
    const foot = h("div", "ml-note tr-note", ["MTA", d.at ? hm(d.at, true) : "", d.error ? `(${d.error})` : ""].filter(Boolean).join(" · "));
    const credit = `${d.label || "MTA"}. ${d.disclaimer || "Times may not be real time"}.`;
    if (window.Help && Help.info) foot.append(" ", Help.info(credit, "Train times")); else foot.title = credit;
    host.append(foot);
  }

  return { panel, load, maps, data: () => store.data, KEY };
})();
