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
//   Click        follows it (Follow, below), and its box: what it is (the feed's type, the FAA registry's make and
//                model), what it is doing, how it was located, and whether the mark is where it was reported or an
//                estimate between reports.
//   Box          the group's note: emergencies first, the HPN movements (arriving, departing, on the ground),
//                how many are low in the area, each feed (our receiver, adsb.lol, adsb.fi) with how many it gave and
//                which is primary or resting, the credits and licence, the feed's time, our receiver's state.
//   Talking      who is talking on HPN Tower (#252, sizeup/atc_talkers.py; the answer's "atc", while Settings > System >
//                ATC talkers > "Show ATC talker on the map" is on): an aircraft whose callsign was heard and matched on
//                ADS-B (confirmed or probable, never a guess) gets a pulsing ring while its transmission is in (TALK_HOLD_S
//                on the hub) or plays here (cad.js sizeup:playing), a steady one while the tower is calling it; its box
//                lists its recent transmissions with ▶. The tower (#257) is a "TWR" mark at the control tower (the hub's
//                atc.tower: the airport data's), dim, lit and pulsing while the tower transmits (its transmission in
//                within TALK_HOLD_S) or one of its transmissions plays here. A Radio row's aircraft chip (cad.js) calls focus(hex): the layer
//                on, the map centred on it, ringed for a while, its box open. Speech-derived: never a radio ID.
//   Follow       (#352) a click on an aircraft centres it (zoom 12 at least) and keeps it centred as it moves: on the
//                frames that already move the marks, the map is eased onto it over PAN_MS, at most every PAN_MS and
//                only once it is a pixel off (never over a zoom; no fetch, no redraw of its own), zooming round the
//                centre meanwhile (a pinch too); a "Following <tail>" chip with ✕ at the foot of the map. A drag,
//                Recenter (cadmap.js fires sizeup:recenter), ✕, the layer going off, or LOST_MS with no fix and no
//                alert naming it stops it.
//                Hovering a mark: its tail, type, altitude, speed and phase as the canvas's title.
//   Alerts       (#352) an open airport pre-alert naming aircraft (airport-alerts.js calls alerts(items); its data's
//                aircraft by hex, sizeup/cad/airport_alerts.py): each one ringed and haloed in its alert's colour (high
//                --accent-text, medium --warn, low --text-2), drawn above the other aircraft (its own source, ac-hl:
//                only the ringed and the followed ones, a handful); its card hovered (hover(hexes)): a wider ring and a
//                brighter halo. Not on the map (out of range, lost): its last fix, hollow and faded, "last fix 4 min".
//                The Overview's maps turn the layer on while one is open (unticking it holds until another alert or
//                aircraft is named; an alert's aircraft moving doesn't count).
//                Gone with the alert (accepted, dismissed, expired).
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
  const HL_SRC = "ac-hl";                   // the alerted and followed ones (a handful): drawn above the rest
  const IDS = ["ac-trail", "ac-ring", "ac-mark", "ac-label", "ac-twr", "ac-twr-label", "ac-hl-glow", "ac-hl-ring", "ac-hl-mark", "ac-hl-label"];
  const MARKS = ["ac-hl-mark", "ac-mark"];  // what a click or a hover finds
  const TWR_SRC = "ac-tower";               // the tower's mark (#257): its own source, never one of the aircraft
  const PING_MS = 1400;                     // one "talking" ring pulse (shortfinal's)
  const HIGHLIGHT_MS = 12000;               // a chip's aircraft, ringed this long once centred
  const FOCUS_WAIT_MS = 20000;              // ... waited for this long (the layer just turned on, the data coming)
  const HALO = "#15181B";
  const FOLLOW_ZOOM = 12;                   // a click centres the aircraft at least this close
  const PAN_MS = 250;                       // following: the map eased onto the aircraft at most this often (moveend 4/s)
  const LOST_MS = 600e3;                    // ... and let go once it has had no fix this long, no alert naming it
  const NARROW_PX = 520;                    // a map this narrow: following from a card opens no box over the aircraft
  const SEEN_KEEP_MS = 3600e3;              // each aircraft's last fix here, kept this long (an alert's lost one)
  const ALERT_TOKEN = { high: ["--accent-text", "#EE6475"], medium: ["--warn", "#E0A030"], low: ["--text-2", "#9AA3AD"] };
  const ALERT_RANK = { high: 0, medium: 1, low: 2 };
  const PHASE = {
    arriving: { token: "--ok", text: "Arriving HPN", tag: "▼" },
    departing: { token: "--info", text: "Departing HPN", tag: "▲" },
    near: { token: "--warn", text: "Near HPN", tag: "" },
    ground: { token: "--text-2", text: "On ground · HPN", tag: "GND" },
    low: { token: "--warn", text: "Low nearby", tag: "" },
    overflight: { token: "--dim", text: "Overflight", tag: "" },
    other_ground: { token: "--dim", text: "On ground elsewhere", tag: "" },
  };
  // shortfinal's EMPHASIS (its map page): how strongly each phase is drawn (the mark's size, the label's, the trail's)
  const EMPHASIS = {
    arriving: { scale: 1.15, label: 1.0, trail: 1.3 },
    departing: { scale: 1.05, label: 1.0, trail: 1.2 },
    near: { scale: 1.0, label: 1.0, trail: 1.1 },
    ground: { scale: 0.85, label: 0.9, trail: 0.8 },
    low: { scale: 1.0, label: 1.0, trail: 1.0 },
    // a squawk 7500 / 7600 / 7700 or its own emergency status (the hub raises the alarm and pushes): never drawn
    // smaller than the busiest phase, whatever it is doing; red, first (rank 0), its emergency on the label
    emergency: { scale: 1.3, label: 1.1, trail: 1.5 },
  };
  function emphasis(a) { return a.emerg ? EMPHASIS.emergency : (EMPHASIS[a.phase] || EMPHASIS.low); }
  // which feed each position came from (sizeup/aircraft.py combine(): our receiver first, then the freshest of the APIs)
  const SRC_NAME = { receiver: "Our receiver", adsblol: "adsb.lol", adsbfi: "adsb.fi" };
  const POS = { adsb: "ADS-B (its own GPS position)", mlat: "MLAT (located by receivers' timing)", tisb: "TIS-B (a radar position rebroadcast)",
                adsr: "ADS-R (rebroadcast from the other band)", modes: "Mode S", adsc: "ADS-C", other: "other" };
  // #278: the caveats the box used to spell out, now behind its "i"
  const PHASE_TIP = "Arriving / departing / near / low is sizeup's reading of its track (shortfinal's rules), not ATC's word.";

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
  const fmtAlt = (a) => (a === "ground" ? "GND" : typeof a === "number" ? `${a.toLocaleString("en-US")} ft` : "alt unknown");
  const trend = (vr) => (vr === null || vr === undefined ? "" : vr > 300 ? " ↑" : vr < -300 ? " ↓" : "");
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);     // the hub's why_not() is lower case ("off (…)")
  function eta(s) {
    if (s === null || s === undefined) return "";
    return s < 60 ? "<1 min" : `~${Math.round(s / 60)} min`;
  }

  // ---- the data: one copy for every map on the page, fetched only while one has the layer on ----------------------
  const store = { data: null, timer: null, loading: null, subs: new Set(), at: 0, failed: false };
  const maps = new Set();
  const anyOn = () => { for (const m of maps) if (m.isOn()) return true; return false; };
  function notify() { for (const fn of store.subs) { try { fn(store.data); } catch (e) { console.error(e); } } }
  async function load() {
    if (store.loading) return store.loading;
    const t0 = performance.now();               // map:aircraft: asked -> drawn (#284)
    store.loading = (async () => {
      try {
        // the browser revalidates (If-None-Match): an unchanged answer is a 304 and the copy it has
        const r = await fetch(URL, { credentials: "same-origin" });
        if (r.ok) { store.data = await r.json(); store.at = Date.now(); store.failed = false; } else store.failed = true;
      } catch (_) { store.failed = true; /* the hub is away: keep what we have; it goes stale on screen */ }
      finally { store.loading = null; moved(); notify(); animate(); try { performance.clearMeasures("map:aircraft"); performance.measure("map:aircraft", { start: t0 }); } catch (_) { /* an older browser */ } }
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
      const where = a.appr ? `▼ ${a.appr}` : a.rwy ? `▼ RWY ${a.rwy}` : "▼ inbound";
      return [where, a.dist !== null && a.dist !== undefined ? `${a.dist.toFixed(1)} nm` : ""].filter(Boolean).join(" · ");
    }
    if (a.phase === "departing") return "▲ departed HPN";
    if (a.phase === "near") return "near HPN";
    return "";
  }

  // ---- who is talking (#252): the hub's "atc" (talking, tx by hex), what plays here, a chip's aircraft ---------------
  const talk = { playing: "", highlight: null, pending: null };
  const nowS = () => Date.now() / 1000 + ((window.cad && cad.skew) || 0);
  const atcOf = (d) => (d && d.atc && !(window.cad && cad.state && cad.state.atc && cad.state.atc.map === false) ? d.atc : null);
  window.addEventListener("sizeup:playing", (e) => {
    const m = /\/api\/audio\/([^#?]+?)\.wav/.exec((e.detail && e.detail.url) || "");
    talk.playing = m ? decodeURIComponent(m[1]) : "";
    for (const mp of maps) mp.draw(true);
    animate();
  });
  // talking (it is speaking: pulsing), called (the tower to it: steady), selected (a chip's), or ""
  function ringFor(d, hex) {
    const a = atcOf(d);
    if (a) {
      const mine = (a.tx && a.tx[hex]) || [];
      const p = talk.playing && mine.find((x) => x.id === talk.playing);
      if (p) return p.role === "speaking" ? "talking" : "called";
      const live = (a.talking || []).find((x) => x.hex === hex && x.until > nowS());
      if (live) return live.role === "speaking" ? "talking" : "called";
    }
    return talk.highlight && talk.highlight.hex === hex && Date.now() < talk.highlight.until ? "selected" : "";
  }
  function ringsOn(d) {
    const a = atcOf(d);
    return !!((a && ((a.talking || []).some((x) => x.until > nowS()) || talk.playing || towerLit(d)))
              || (talk.highlight && Date.now() < talk.highlight.until));
  }
  // the tower (#257): lit while it transmits (its last transmission in within the hub's hold) or one of its plays here
  function towerLit(d) {
    const tw = (atcOf(d) || {}).tower;
    return !!(tw && ((tw.until || 0) > nowS() || (talk.playing && (tw.tx || []).includes(talk.playing))));
  }
  function towerFc(d, now) {
    const tw = (atcOf(d) || {}).tower;
    if (!tw || typeof tw.lat !== "number" || typeof tw.lon !== "number") return fc([]);
    const lit = towerLit(d), ping = lit ? (now % PING_MS) / PING_MS : 0;
    return fc([{ type: "Feature", geometry: { type: "Point", coordinates: [tw.lon, tw.lat] },
                 // #275: closed (06:00-23:00 local open), "CTAF" instead of "TWR"; an older hub sends no "open"
                 properties: { lit: lit ? 1 : 0, color: lit ? token("--ok", "#3FB950") : token("--dim", "#6E7681"),
                               name: tw.open === false ? "CTAF" : "TWR",
                               rr: 1 + 0.6 * ping, ringo: lit ? 1 - 0.7 * ping : 0.6 } }]);
  }

  // ---- the open pre-alerts' aircraft (airport-alerts.js), and each one's last fix here --------------------------------
  const alerted = { by: new Map(), sig: "", key: "", hover: "" };   // sig: what is drawn; key: which alerts and aircraft   // hex -> {level, ids, cs, type, lon, lat, at (ms), alt}
  const seen = new Map();                   // hex -> its last fix this page saw {lon, lat, at (ms), rot, kind, cs, type, alt}
  function alerts(items) {
    const by = new Map();
    for (const a of items || []) {
      const level = ALERT_TOKEN[a.level] ? a.level : "medium";
      for (const x of (a.data && a.data.aircraft) || []) {
        if (!x || !x.hex) continue;
        const was = by.get(x.hex);
        if (was) { was.ids.push(a.id); if (ALERT_RANK[level] < ALERT_RANK[was.level]) was.level = level; continue; }
        by.set(x.hex, { level, ids: [a.id], cs: x.callsign || "", type: x.type || x.model || "", alt: x.alt,
                        lon: x.lon, lat: x.lat, at: (x.pos_at || a.at || 0) * 1000 });
      }
    }
    const sig = JSON.stringify([...by].map(([k, v]) => [k, v.level, v.ids, v.lon, v.lat]));
    if (sig === alerted.sig) return;
    alerted.by = by;
    alerted.sig = sig;
    // an ADS-B emergency's alert has its aircraft rewritten every 15 s: unticking holds until another alert or aircraft
    alerted.key = JSON.stringify([...by].map(([k, v]) => [k, v.ids]));
    if (alerted.hover && !alerted.hover.split(" ").some((x) => by.has(x))) alerted.hover = "";
    for (const m of maps) m.sync();
    kick();
  }
  // a card hovered (its aircraft, or none): ringed wider
  function hover(hexes) {
    const k = (hexes || []).filter(Boolean).join(" ");
    if (k === alerted.hover) return;
    alerted.hover = k;
    for (const m of maps) m.draw(true);
  }
  const hovered = (hex) => !!alerted.hover && alerted.hover.split(" ").includes(hex);
  function alertColour(hex) {
    const v = alerted.by.get(hex);
    return v ? token(...ALERT_TOKEN[v.level]) : "";
  }
  // where an aircraft not in the answer was last: this page's own last fix, else its alert's (the newer)
  function lastFix(hex) {
    const s = seen.get(hex), a = alerted.by.get(hex);
    const al = a && typeof a.lon === "number" && typeof a.lat === "number" ? { lon: a.lon, lat: a.lat, at: a.at, rot: 0, kind: "jet", cs: a.cs, type: a.type, alt: a.alt } : null;
    if (s && (!al || s.at >= al.at)) return s;
    return al;
  }
  function ago(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s} s`;
    const m = Math.round(s / 60);
    return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
  }
  // the terse facts (a mark's hover, the follow chip's): tail · type · altitude · speed · phase
  function brief(a) {
    const ph = a.phase === "arriving" ? `arriving${a.rwy ? ` ${a.rwy}` : ""}` : a.phase === "departing" ? "departing"
      : a.phase === "ground" ? "on ground" : a.phase === "near" ? "near HPN" : a.phase === "low" ? "low nearby" : "";
    return [callsign(a), a.type, a.alt !== undefined && a.alt !== null ? fmtAlt(a.alt) : "",
            a.gs !== null && a.gs !== undefined ? `${a.gs} kt` : "", ph].filter(Boolean).join(" · ");
  }

  // ---- the motion: one follower for every map (glide.js, motion.js's predictor), fed each real fix ------------------
  const glide = window.Glide && window.Motion ? Glide.follower(Motion.arc, { horizonS: HORIZON_S }) : null;
  let looks = null;                         // what each aircraft looks like this answer (look())
  function moved() {
    const d = store.data;
    if (!d) return;
    const now = Date.now();
    for (const a of d.aircraft || []) {
      seen.set(a.hex, { lon: a.lon, lat: a.lat, at: now - ageNow(d, a) * 1000, rot: a.trk || 0, kind: iconFor(a)[0],
                        cs: callsign(a), type: a.type, alt: a.alt });
    }
    for (const [k, v] of seen) if (now - v.at > SEEN_KEEP_MS) seen.delete(k);
    if (!glide) return;
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
  const wanted = () => anyOn() && document.visibilityState !== "hidden" && ((!!glide && glide.busy()) || ringsOn(store.data));
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
                         rank: (a.emerg ? 0 : 10) + ({ arriving: 1, departing: 2, near: 3, ground: 4, low: 5 }[a.phase] || 6),
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
      this.following = "";                  // the aircraft this map keeps centred (its hex), or ""
      this.offSig = null;                   // the alerts this viewer unticked the layer under
      maps.add(this);
      store.subs.add(() => this.sync());
      this.map.on("click", (e) => this.clicked(e));
      // a drag, or Recenter (cadmap.js), takes the map back from the aircraft
      // (a pinch is two fingers: a zoom round the aircraft, still following)
      this.map.on("dragstart", (e) => {
        const t = e && e.originalEvent && e.originalEvent.touches;
        if (!(t && t.length > 1) && !this.map.isZooming()) this.unfollow();
      });
      this.map.on("sizeup:recenter", () => this.unfollow());
      if (this.isOn()) kick();
    }
    // on: the viewer's (or the event's) set; on the Overview's maps also while an open pre-alert names an aircraft
    isOn() { return this.control.on(KEY, false) || this.alertOn(); }
    alertOn() { return this.control.profile !== "incident" && alerted.by.size > 0 && this.offSig !== alerted.key; }
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
      // who is talking (#252): a ring round the mark, pulsing while it speaks (shortfinal's ping)
      map.addLayer({ id: "ac-ring", type: "circle", source: SRC, filter: ["all", points, ["has", "ring"]], layout: vis,
                     paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, ["*", 9, ["get", "rr"]], 12, ["*", 13, ["get", "rr"]], 15, ["*", 18, ["get", "rr"]]],
                              "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": ["get", "ringc"], "circle-stroke-width": 2.5,
                              "circle-stroke-opacity": ["get", "ringo"] } }, before);
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
      // HPN's tower (#257): a "TWR" mark at the control tower (the airport data), lit and pulsing while it transmits
      map.addSource(TWR_SRC, { type: "geojson", data: fc([]) });
      map.addLayer({ id: "ac-twr", type: "circle", source: TWR_SRC, layout: vis,
                     paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, ["*", 7, ["get", "rr"]], 12, ["*", 10, ["get", "rr"]], 15, ["*", 14, ["get", "rr"]]],
                              "circle-color": HALO, "circle-opacity": 0.85, "circle-stroke-color": ["get", "color"],
                              "circle-stroke-width": 2.5, "circle-stroke-opacity": ["get", "ringo"] } }, before);
      if (glyphs) {
        map.addLayer({ id: "ac-twr-label", type: "symbol", source: TWR_SRC, minzoom: 10,
                       layout: { ...vis, "text-field": ["get", "name"], "text-font": ["Noto Sans Bold"], "text-size": 10,
                                 "text-allow-overlap": true, "text-ignore-placement": true },
                       paint: { "text-color": ["get", "color"] } }, before);
      }
      // the alerted and followed ones: a halo and a ring in the alert's colour, the mark and its label above the rest
      map.addSource(HL_SRC, { type: "geojson", data: fc([]) });
      const radius = (k) => ["interpolate", ["linear"], ["zoom"], 9, ["*", 9 * k, ["get", "rr"]], 12, ["*", 13 * k, ["get", "rr"]],
                             15, ["*", 18 * k, ["get", "rr"]]];
      map.addLayer({ id: "ac-hl-glow", type: "circle", source: HL_SRC, layout: vis,
                     paint: { "circle-radius": radius(1.5), "circle-color": ["get", "ringc"], "circle-opacity": ["get", "glow"],
                              "circle-blur": 0.6 } }, before);
      map.addLayer({ id: "ac-hl-ring", type: "circle", source: HL_SRC, layout: vis,
                     paint: { "circle-radius": radius(1), "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": ["get", "ringc"],
                              "circle-stroke-width": ["get", "ringw"], "circle-stroke-opacity": ["get", "ringo"] } }, before);
      map.addLayer({ id: "ac-hl-mark", type: "symbol", source: HL_SRC,
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map",
                               "icon-allow-overlap": true, "icon-ignore-placement": true,
                               "icon-size": ["interpolate", ["linear"], ["zoom"], 9, ["*", 0.5, ["get", "scale"]],
                                             12, ["*", 0.7, ["get", "scale"]], 15, ["*", 0.95, ["get", "scale"]]] },
                     paint: { "icon-color": ["get", "color"], "icon-halo-color": HALO, "icon-halo-width": 1.5,
                              "icon-opacity": ["get", "opacity"] } }, before);
      if (glyphs) {
        map.addLayer({ id: "ac-hl-label", type: "symbol", source: HL_SRC,
                       layout: { ...vis,
                                 "text-field": ["format", ["get", "cs"], { "font-scale": 1.0 }, "\n", {},
                                                ["get", "detail"], { "font-scale": 0.82, "text-font": ["literal", ["Noto Sans Regular"]] }],
                                 "text-font": ["Noto Sans Bold"], "text-size": 13,
                                 "text-anchor": "bottom-left", "text-justify": "left", "text-offset": [1.3, -0.8],
                                 "text-allow-overlap": true, "text-ignore-placement": true,
                                 "icon-image": "ac-chip", "icon-text-fit": "both", "icon-text-fit-padding": [4, 7, 3, 7],
                                 "icon-allow-overlap": true, "icon-ignore-placement": true },
                       paint: { "text-color": ["get", "ringc"], "text-opacity": ["get", "opacity"],
                                "icon-opacity": ["*", 0.9, ["get", "opacity"]] } }, before);
      }
      const canvas = map.getCanvas();
      for (const id of MARKS) {
        map.on("mouseenter", id, () => { canvas.style.cursor = "pointer"; });
        map.on("mousemove", id, (e) => {
          const hex = e.features && e.features[0] && e.features[0].properties.hex;
          const a = hex && store.data && (store.data.aircraft || []).find((x) => x.hex === hex);
          const t = a ? brief(a) : hex && lastFix(hex) ? `${brief(lastFix(hex))} · last fix ${ago(Date.now() - lastFix(hex).at)}` : "";
          if (canvas.title !== t) canvas.title = t;
        });
        map.on("mouseleave", id, () => { canvas.style.cursor = ""; canvas.title = ""; });
      }
      this.added = true;
    }

    sync() {
      if (!this.isOn() && !this.added) { this.panelChanged(); return; }
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      try { this.add(); } catch (e) { console.error("aircraft layer", e); return; }
      const d = store.data, on = this.isOn();
      if (!on) this.unfollow();                      // the layer off (unticked, or its alert gone): nothing to follow
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
      const trails = [], marks = [], hl = [], now = Date.now(), lk = look(d), live = new Set();
      for (const a of d.aircraft || []) {
        live.add(a.hex);
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
        const ring = ringFor(d, a.hex);
        const props = { hex: a.hex, icon: `ac-${l.kind}${stale ? "-old" : ""}`, rot: mark.rot,
                        scale: l.scale, color: l.color, opacity: fade, cs: l.cs, lscale: l.lscale, rank: l.rank - (ring ? 20 : 0),
                        detail: ring === "talking" ? `» TALKING\n${l.detail}` : ring === "called" ? `« TWR calling\n${l.detail}` : l.detail };
        if (ring) {
          const ping = ring === "talking" ? (now % PING_MS) / PING_MS : 0;
          Object.assign(props, { ring, rr: 1 + 0.6 * ping, ringo: ring === "talking" ? 1 - 0.7 * ping : 0.9,
                                 ringc: ring === "talking" ? token("--ok", "#3FB950") : ring === "called" ? token("--info", "#58A6FF")
                                   : token("--text", "#E6EDF3") });
        }
        const feat = { type: "Feature", geometry: { type: "Point", coordinates: [mark.lon, mark.lat] }, properties: props };
        if (alerted.by.has(a.hex) || this.following === a.hex) hl.push(this.lit(feat, a.hex, false));
        else marks.push(feat);
      }
      // an alerted (or followed) aircraft not in the answer: its last fix, hollow and faded, with its age
      for (const hex of new Set([...alerted.by.keys(), this.following])) {
        if (!hex || live.has(hex)) continue;
        const f = lastFix(hex);
        if (!f) continue;
        hl.push(this.lit({ type: "Feature", geometry: { type: "Point", coordinates: [f.lon, f.lat] },
                           properties: { hex, icon: `ac-${f.kind || "jet"}-old`, rot: f.rot || 0, scale: 1, color: token("--text-2", "#9AA3AD"),
                                         opacity: 0.55, cs: f.cs || callsign({ hex }), lscale: 1, detail: `last fix ${ago(now - f.at)}` } }, hex, true));
      }
      const src = this.map.getSource(SRC);
      if (src) src.setData(fc(trails.concat(marks)));
      const hs = this.map.getSource(HL_SRC);
      if (hs && (hl.length || this.hlShown)) hs.setData(fc(hl));
      this.hlShown = hl.length > 0;
      if (this.following) this.track(hl.find((f) => f.properties.hex === this.following), force);
      const tw = this.map.getSource(TWR_SRC);
      if (tw) tw.setData(towerFc(d, now));
    }
    // a mark in the ringed source: its alert's colour (the mark too), wider while its card is hovered; a followed one
    // that no alert names: a plain ring in the text colour
    lit(f, hex, lost) {
      const p = f.properties, ac = alertColour(hex), hot = hovered(hex);
      Object.assign(p, ac ? { color: lost ? p.color : ac, ringc: ac, rr: hot ? 1.6 : 1.25, ringw: hot ? 5 : 3, ringo: lost ? 0.6 : 1,
                               glow: lost ? 0.12 : hot ? 0.45 : 0.25, scale: p.scale * (hot ? 1.25 : 1.1) }
        : { ringc: token("--text", "#E6EDF3"), rr: 1.25, ringw: 2, ringo: lost ? 0.5 : 0.9, glow: 0 });
      return f;
    }
    // follow an aircraft: centred (zoomed in), kept centred on the frames that move it, the chip at the foot
    follow(hex) {
      if (!hex) return;
      this.following = hex;
      for (const z of [this.map.scrollZoom, this.map.touchZoomRotate]) {     // zoom round the centre (the aircraft) meanwhile
        try { if (z.isEnabled()) z.enable({ around: "center" }); } catch (_) { /* an older MapLibre */ }
      }
      this.map.fire("sizeup:follow");                                       // cadmap.js: no refitting while it follows
      const a = store.data && (store.data.aircraft || []).find((x) => x.hex === hex);
      const at = (a && ((glide && glide.where(hex, Date.now())) || a)) || lastFix(hex);
      if (at) {
        try { this.map.easeTo({ center: [at.lon, at.lat], zoom: Math.max(this.map.getZoom(), FOLLOW_ZOOM), duration: 600 }); } catch (_) { /* */ }
        this.panAt = performance.now() + 600 - PAN_MS;                      // its first pan after that ease
      }
      this.draw(true);
      animate();
    }
    unfollow() {
      if (!this.following) return;
      this.following = "";
      for (const z of [this.map.scrollZoom, this.map.touchZoomRotate]) {
        try { if (z.isEnabled()) z.enable(); } catch (_) { /* */ }
      }
      if (this.chipEl) this.chipEl.hidden = true;
      this.draw(true);
    }
    // each frame while following: the map moved under the mark (never while it is moving otherwise: an ease, a zoom),
    // and the chip
    // eased over PAN_MS to where it is now, at most every PAN_MS and only once it is a pixel off centre: smooth, and
    // a moveend at most 4 times a second (gislayers.js fetches only once the view leaves what it has)
    track(f, force) {
      const now = performance.now();
      const ours = now < (this.panAt || 0) + PAN_MS + 60;                    // moving: our last pan, finishing
      if (f && now - (this.panAt || 0) >= PAN_MS && !this.map.isZooming() && (!this.map.isMoving() || ours)) {
        const [lon, lat] = f.geometry.coordinates, p = this.map.project([lon, lat]), c = this.map.project(this.map.getCenter());
        if (Math.hypot(p.x - c.x, p.y - c.y) >= 1) {
          this.panAt = now;
          this.map.easeTo({ center: [lon, lat], duration: PAN_MS, easing: (t) => t });
        }
      }
      const hex = this.following, a = store.data && (store.data.aircraft || []).find((x) => x.hex === hex), lf = !a && lastFix(hex);
      if (!a && !alerted.by.has(hex) && (!lf || Date.now() - lf.at > LOST_MS)) { this.unfollow(); return; }   // gone for good
      if (!force && this.chipEl && !this.chipEl.hidden) return;              // the chip's words change with an answer, not a frame
      const name = a ? callsign(a) : (lf && lf.cs) || callsign({ hex });
      if (!this.chipEl) {
        const c = h("div", "ac-follow");
        c.setAttribute("role", "status");
        c.addEventListener("click", (e) => e.stopPropagation());
        const x = h("button", "mini quiet", "✕");
        x.type = "button";
        x.title = "Stop following";
        x.setAttribute("aria-label", "Stop following");
        x.addEventListener("click", (e) => { e.stopPropagation(); this.unfollow(); });
        this.chipText = h("span", "ac-follow-t");
        c.append(...(window.Icon ? [Icon("aircraft")] : []), this.chipText, x);
        this.control.host.append(c);
        this.chipEl = c;
      }
      const text = `Following ${name}` + (a ? "" : lf ? ` · last fix ${ago(Date.now() - lf.at)}` : " · lost");
      if (this.chipText.textContent !== text) this.chipText.textContent = text;
      const tip = a ? brief(a) : lf ? brief(lf) : "";
      if (this.chipEl.title !== tip) this.chipEl.title = tip + (tip ? "\n" : "") + "A drag or Recenter stops following";
      this.chipEl.classList.toggle("alerted", alerted.by.has(hex));
      this.chipEl.hidden = false;
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
      const hits = this.map.queryRenderedFeatures(e.point, { layers: MARKS.filter((id) => this.map.getLayer(id)) });
      if (!hits.length) return;
      e.originalEvent && e.originalEvent.stopPropagation();
      const hex = hits[0].properties.hex;
      setTimeout(() => { this.follow(hex); this.show(hex); }, 0);
    }

    // #278: the target layout, one fact a line (made-up aircraft):
    //   ✈ ABC123                      Phenom 300 · E55P
    //   Departing HPN · 1.6 nm
    //   2,300 ft ↑1,800 fpm · 180 kt · 277°
    //   Sqk 1234 · N000XX · Example Air
    //   adsb.fi · 10:21:46                              ⓘ  (where the position came from, how, and the caveats)
    show(hex) {
      const d = store.data;
      const a = ((d && d.aircraft) || []).find((x) => x.hex === hex);
      if (!a) { this.showLost(hex); return; }
      const age = ageNow(d, a), stale = age > staleS(d);
      const has = (v) => v !== null && v !== undefined;
      const lines = [];
      if (a.emerg) lines.push(`EMERGENCY ${a.emerg}`);
      // #278: an old position is said up top, in the warning colour, with the time it was reported (never only in the foot)
      const fixAt = (d.at || d.fetched || 0) - (a.age || 0);   // a.age is at the feed's time (d.at)
      if (stale) lines.push(h("div", "warn", `Stale · ${hms(fixAt)}`));
      const ph = PHASE[a.phase] || PHASE.low;
      const where = a.phase !== "arriving" ? "" : a.appr ? a.appr : a.rwy ? `RWY ${a.rwy}` : "";
      lines.push([ph.text, where, has(a.dist) ? `${a.dist.toFixed(1)} nm` : "", has(a.eta) ? `ETA ${eta(a.eta)}` : ""]
        .filter(Boolean).join(" · "));
      const vs = !has(a.vr) || a.alt === "ground" ? "" : Math.abs(a.vr) <= 300 ? "level"
        : `${a.vr > 0 ? "↑" : "↓"}${Math.abs(a.vr).toLocaleString("en-US")} fpm`;
      lines.push([[fmtAlt(a.alt), vs].filter(Boolean).join(" "), has(a.gs) ? `${a.gs} kt` : "", has(a.trk) ? `${a.trk}°` : ""]
        .filter(Boolean).join(" · "));
      const who = [a.airline, a.op && a.op !== a.airline ? a.op : ""].filter(Boolean);
      const ids = [a.sq ? `Sqk ${a.sq}` : "", a.reg || "", ...who].filter(Boolean);
      if (ids.length) lines.push(ids.join(" · "));
      lines.push(...this.heardLines(d, a));
      const what = [a.model, a.type, a.kind].filter(Boolean);
      const foot = [SRC_NAME[a.src] || a.src || "", d.at ? hms(d.at) : ""].filter(Boolean).join(" · ");
      // the method and the caveats: hover or tap the "i"
      const between = glide && glide.estimated(a.hex) && !stale;
      const tip = [
        `Position: ${POS[a.pos] || "as the feed gives it"}${a.band ? `, ${a.band} MHz` : ""}`,
        a.reg ? `Registration from ${a.reg_src === "faa" ? "the FAA registry" : "the feed"}` : "",
        a.model ? `Model from ${a.model_src === "faa" ? "the FAA registry" : "the feed"}` : "",
        a.det ? `Type and registration from ${SRC_NAME[a.det] || a.det}, matched by its ICAO address` : "",
        a.op && a.op !== a.airline && a.op_src && a.op_src !== "airline" ? `Operator from ${SRC_NAME[a.op_src] || a.op_src}` : "",
        has(a.eta) ? "ETA: at its present speed" : "",
        between ? `Drawn on between reports by its track and speed (at most ${HORIZON_S} s)` : "",
        PHASE_TIP,
        [d.credit ? `Data: ${d.credit}` : "", (d.registry && d.registry.credit) || ""].filter(Boolean).join(" · "),
      ].filter(Boolean).join("\n");
      const icon = window.Icon ? Icon("aircraft") : null;
      this.control.close();
      this.control.showInfo(callsign(a), what.join(" · "), lines, foot, a.emerg ? "ac-emerg" : "", icon, tip);
    }

    // an alerted one not on the map now: where it was last, and when
    showLost(hex) {
      const f = lastFix(hex);
      if (!f) return;
      const lines = [h("div", "warn", `Not on ADS-B now · last fix ${hms(f.at / 1000)} (${ago(Date.now() - f.at)} ago)`)];
      if (f.alt !== undefined && f.alt !== null) lines.push(fmtAlt(f.alt));
      this.control.close();
      this.control.showInfo(f.cs || callsign({ hex }), f.type || "", lines, "", "", window.Icon ? Icon("aircraft") : null,
                            "Out of range or lost: drawn hollow at its last reported position");
    }

    // its recent transmissions on HPN Tower (#252): ▶, the time, who said it, the words; speech-derived, never a radio ID
    heardLines(d, a) {
      const at = atcOf(d), tx = (at && at.tx && at.tx[a.hex]) || [];
      if (!tx.length) return [];
      const head = h("div", "ac-heard-head", `HPN Tower · ${tx.length}`);
      head.title = "Matched by the callsign in the words, not a radio ID";
      return [head, ...tx.map((x) => {
        const row = h("div", "ac-tx");
        const b = h("button", "play mini");
        b.type = "button";
        b.setAttribute("aria-label", "Play");
        if (window.Icon && Icon.play) Icon.play(b); else b.textContent = "▶";
        const url = `/api/audio/${encodeURIComponent(x.id)}.wav`;
        if (window.playBtn) playBtn(b, url); else b.addEventListener("click", () => window.play && play(url));
        const who = x.role === "speaking" ? "Pilot" : x.role === "addressed" ? `${x.talker || "TWR"} →` : "Mention";
        row.append(b, h("span", "dim small", hms(x.at)), h("span", `ac-tx-who ${x.status}`, who),
                   h("span", "ac-tx-text", x.text || "…"));
        if (x.status === "probable") row.title = "Probable: one character off, the only near match at HPN";
        return row;
      })];
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
      const forced = this.alertOn() && !this.control.on(KEY, false);
      cb.addEventListener("change", () => {
        if (!cb.checked && this.alertOn()) this.offSig = alerted.key;     // off until the next alert
        this.control.setLayer(KEY, cb.checked);
        this.sync();
        kick();
      });
      const icon = window.Icon ? Icon("aircraft") : null;
      lab.append(cb, ...(icon ? [icon] : []), h("span", "", "Near HPN"), ...(forced ? [h("span", "dim small", "alert")] : []));
      lab.title = "Arrivals, departures, ground and low traffic" + (forced ? "\nOn while a pre-alert names an aircraft" : "");
      box.append(lab);
      if (!this.isOn()) return box;
      if (!d) { box.append(h("div", "ml-note", store.failed ? "Unavailable" : "Loading…")); return box; }
      if (!d.configured || d.note) { box.append(h("div", "ml-note", cap(d.note) || "Not configured")); return box; }
      const list = d.aircraft || [];
      for (const a of list.filter((x) => x.emerg)) box.append(h("div", "ml-note ac-emerg", `${callsign(a)}: ${a.emerg}`));
      const ours = list.filter((a) => ["arriving", "departing", "near", "ground"].includes(a.phase)).slice(0, 8);
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
      } else box.append(h("div", "ml-note", "None at HPN"));
      const low = list.filter((a) => a.phase === "low").length;
      box.append(h("div", "ml-note", `${low} low nearby`));
      // the hollow-mark legend: a staleness rule, so on screen (and the "i" for a tap)
      box.append(h("div", "ml-note", `Hollow: stale (>${staleS(d)} s)`));
      const note = h("div", "ml-note");
      note.append([d.credit || "No data yet", d.at ? hms(d.at) : "", (d.registry && d.registry.credit) || ""].filter(Boolean).join(" · "));
      for (const f of (d.feeds || []).filter((x) => x.terms)) {
        const t = h("a", "", `${f.label} terms`);
        t.href = f.terms;
        t.target = "_blank";
        t.rel = "noopener";
        note.append(" ", t);
      }
      box.append(note);
      // each feed: our receiver first, then the primary and the other API; how many it gave, or why it rests
      const feeds = (d.feeds || []).map((f) => `${SRC_NAME[f.key] || f.label}${f.role === "primary" ? "*" : ""}: `
        + (f.resting ? `resting (${f.resting})` : f.ok ? `${f.n} (${f.shown} shown)` : "waiting"));
      if (feeds.length) { const fl = h("div", "ml-note", feeds.join(" · ")); fl.title = "Aircraft each feed gave (* primary)"; box.append(fl); }
      if (d.fetched && Date.now() / 1000 - d.fetched > 30) box.append(h("div", "ml-note ac-emerg", `No data since ${hms(d.fetched)}`));
      if (d.error) box.append(h("div", "ml-note", `(${d.error})`));
      const rx = d.receiver;
      if (rx) {
        const bands = Object.entries(rx.bands || {}).map(([b, s]) => `${b} MHz: ${s.error ? s.error : `${s.n} aircraft`}`);
        const who = rx.held_by && !(rx.held_by.app === "sizeup" && rx.held_by.mode === "adsb")
          ? `held by ${rx.held_by.app}${rx.held_by.mode ? ` (${rx.held_by.mode})` : ""}` : "";
        const dec = rx.decoder && rx.decoder.state === "ok" ? `${rx.decoder.messages_per_s ?? 0} msg/s` : "";
        box.append(h("div", "ml-note", ["Receiver" + (rx.station ? " (station)" : "") + (rx.sdr ? ` · ${rx.name || rx.sdr}` : ""),
                                         who, dec, ...bands, rx.note].filter(Boolean).join(" · ")));
      }
      return box;
    }
  }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new AircraftMap(control) });

  // A Radio row's aircraft chip (#252): that aircraft on a map on screen (else the Overview's), the layer turned on,
  // centred, ringed for HIGHLIGHT_MS, its box open. Waits FOCUS_WAIT_MS for the layer's data; gone by then: nothing.
  const shownMap = () => [...maps].find((m) => { try { return !!m.map.getContainer().offsetParent; } catch (_) { return false; } });
  function focus(hex, opts) {
    if (!hex) return;
    talk.pending = { hex, until: Date.now() + FOCUS_WAIT_MS, follow: !!(opts && opts.follow) };
    if (!shownMap()) { const b = document.getElementById("overview-open"); if (b) b.click(); }
    tryFocus();
  }
  function tryFocus() {
    const p = talk.pending;
    if (!p || Date.now() > p.until) { talk.pending = null; return; }
    const m = shownMap();
    if (m && !m.isOn()) { m.control.setLayer(KEY, true); kick(); }
    const a = m && store.data && (store.data.aircraft || []).find((x) => x.hex === p.hex);
    // following (an alert card's aircraft line): its last fix will do once the layer's data is in
    if (p.follow && m && m.added && store.data && (a || lastFix(p.hex))) {
      talk.pending = null;
      try { m.map.getContainer().scrollIntoView({ block: "nearest" }); } catch (_) { /* */ }
      m.follow(p.hex);
      if (m.map.getContainer().clientWidth >= NARROW_PX) m.show(p.hex);   // a phone's map: the box would hide it
      return;
    }
    if (!a || !m.added) { setTimeout(tryFocus, 400); return; }
    talk.pending = null;
    talk.highlight = { hex: a.hex, until: Date.now() + HIGHLIGHT_MS };
    const at = (glide && glide.where(a.hex, Date.now())) || a;
    try { m.map.easeTo({ center: [at.lon, at.lat], zoom: Math.max(m.map.getZoom(), 12), duration: 600 }); } catch (_) { /* */ }
    m.draw(true);
    m.show(a.hex);
    animate();
  }

  // an alert card's aircraft line (airport-alerts.js): that aircraft followed on a map on screen
  const follow = (hex) => focus(hex, { follow: true });
  return { load, maps, data: () => store.data, KEY, focus, follow, alerts, hover, ringFor: (hex) => ringFor(store.data, hex) };
})();
