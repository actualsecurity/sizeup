// Moving marks on the CAD maps, between real fixes: predict a little, then ease onto the next fix instead of jumping.
// The user: "like on shortfinal i think we approximated the radius of turns etc and then corrected when the real data
// came in ... would be nice to do the same so its less stop motion."
//
//   shortfinal    its map page, positionNow(): from the last report, on along its track at its speed, never more
//                 than MAX_EXTRAPOLATE_S (10 s) past it, not on the ground or under 30 kt; redrawn every ANIMATE_MS
//                 (100 ms). It gets a push a second, so it simply jumps to each new report.
//   here          the same rule and limits, in a tracker any layer can use (Motion.tracker) with the prediction as a
//                 plug-in: Motion.arc for aircraft bends the line by the turn rate the hub sends (aircraft.py, the
//                 feed's track_rate or the last two fixes, clamped to a standard-rate turn), the arc of the turn it
//                 is in. Fixes come every 2-5 s here, not every second, so a new fix is eased into over EASE_MS
//                 (the correction shrinks to nothing) rather than jumped to; a correction bigger than snapM is a
//                 re-acquired target and jumps.
//   trains        Motion.rail (below): a train moves in one number, s, its distance along the line on its own
//                 direction's track, on the hub's run profile (profile_s, ported 1:1 from sizeup/trains.py) and
//                 never past the hub's bounds; Motion.railFollow replaces the eased lon/lat offset with a speed
//                 along the track that closes on the prediction (never across it, no steps; back only to where a
//                 fix says it stopped: eased a few metres, or put there at once and marked). A GPS fix is carried on
//                 at its speed, slowed only on evidence (braking, its next stop, a fix gone quiet), and the drawn
//                 train's pace changes no faster than its consist can; a big correction is glided onto (#375, #390).
//
// Nothing fake: the prediction is only ever between a real fix and the next one, at most horizonS after its fix; a
// fix older than the layer's stale age is drawn where it really was (eased back there), never moved on.
"use strict";

window.Motion = (() => {
  const R = 6371008.8, DEG = Math.PI / 180, KT = 1852 / 3600;
  const wrap180 = (d) => ((d + 180) % 360 + 360) % 360 - 180;
  const easeLeft = (k) => (k >= 1 ? 0 : k <= 0 ? 1 : (1 - k) ** 3);          // the share of a correction still to go
  function metres(a, b) {
    const x = (b.lon - a.lon) * DEG * Math.cos(((a.lat + b.lat) / 2) * DEG), y = (b.lat - a.lat) * DEG;
    return Math.hypot(x, y) * R;
  }

  // Aircraft: on along the track at the ground speed, round the arc of its turn (tr, deg/s, + right).
  // Not on the ground, under 30 kt or without a track (shortfinal's positionNow): null, it stays at its fix.
  function arc(fix, dt) {
    if (fix.ground || fix.gs === null || fix.gs === undefined || fix.gs < 30 || fix.trk === null || fix.trk === undefined) return null;
    const v = fix.gs * KT, w = (fix.tr || 0) * DEG, h = fix.trk * DEG;
    let e, n;
    if (Math.abs(w) < 1e-4) { e = v * dt * Math.sin(h); n = v * dt * Math.cos(h); }
    else { e = (v / w) * (Math.cos(h) - Math.cos(h + w * dt)); n = (v / w) * (Math.sin(h + w * dt) - Math.sin(h)); }
    const lat = fix.lat + n / R / DEG;
    return { lon: fix.lon + e / (R * Math.cos(fix.lat * DEG)) / DEG, lat, rot: fix.trk + (fix.tr || 0) * dt };
  }

  // One tracker per layer: set() each real fix, where() each frame.
  //   predict(fix, dtS) -> {lon, lat, rot} | null   (null: not moving; drawn at its fix; {..., moving: false}: drawn
  //                                                  there, but it has reached its limit and is held, not moving)
  //   fix: {lon, lat, rot, at (ms, this browser's clock, when the position was true), ...what predict reads}
  // Two optional hooks, for a layer whose marks aren't free points (trains: one number along a track):
  //   rest(fix)                    where it is drawn without a prediction (default: its fix's lon, lat, rot)
  //   follow(it, target, now)      the drawn position from the one drawn last (it.shown) and the prediction now;
  //                                it.fresh: a new fix since the last frame; it.stale: frozen. Sets it.chase while
  //                                it is still catching up (busy). Replaces the eased offset (easeMs, snapM).
  class Tracker {
    constructor(predict, { horizonS = 10, staleS = 15, easeMs = 1000, snapM = 2000, rest = null, follow = null } = {}) {
      Object.assign(this, { predict, horizonS, staleS, easeMs, snapM, rest, follow });
      this.items = new Map();       // id -> {fix, off: {lon, lat, rot}, easeAt, shown, stale, moving}
    }
    target(it, now) {
      const age = (now - it.fix.at) / 1000;
      it.stale = age > this.staleS;
      const p = it.stale ? null : this.predict(it.fix, Math.min(this.horizonS, Math.max(0, age)));
      it.moving = !!p && p.moving !== false && age < this.horizonS;
      return p || (this.rest ? this.rest(it.fix) : { lon: it.fix.lon, lat: it.fix.lat, rot: it.fix.rot });
    }
    // start easing from where it is drawn now onto `to` (a new fix, or its own fix once stale)
    rebase(it, to, now) {
      const s = it.shown;
      it.off = s && metres(s, to) <= this.snapM ? { lon: s.lon - to.lon, lat: s.lat - to.lat, rot: wrap180((s.rot || 0) - (to.rot || 0)) } : null;
      it.easeAt = now;
    }
    set(id, fix, now = Date.now()) {
      let it = this.items.get(id);
      if (!it) { this.items.set(id, { fix, off: null, easeAt: 0, shown: null, stale: false, moving: false }); return; }
      if (this.follow) {                       // drawn on from where it is now; follow() corrects onto the new fix
        if (it.shown) this.where(id, now);
        it.fix = fix;
        it.fresh = true;
        return;
      }
      const same = it.fix.lon === fix.lon && it.fix.lat === fix.lat && it.fix.at === fix.at;
      if (!same) this.where(id, now);          // where it is drawn now, by the fix it had
      it.fix = fix;
      if (!same) this.rebase(it, this.target(it, now), now);
    }
    where(id, now = Date.now()) {
      const it = this.items.get(id);
      if (!it) return null;
      if (this.follow) {
        it.shown = this.follow(it, this.target(it, now), now);
        it.fresh = false;
        return it.shown;
      }
      const was = it.stale;
      let t = this.target(it, now);
      if (it.shown && it.stale && !was) { this.rebase(it, t, now); t = this.target(it, now); }   // gone stale: back to its fix
      const k = it.off ? easeLeft((now - it.easeAt) / this.easeMs) : 0;
      if (!k) it.off = null;
      const p = k ? { lon: t.lon + it.off.lon * k, lat: t.lat + it.off.lat * k, rot: (t.rot || 0) + it.off.rot * k } : t;
      it.shown = p;
      return p;
    }
    stale(id) { const it = this.items.get(id); return !!(it && it.stale); }
    forget(keep) { for (const id of [...this.items.keys()]) if (!keep.has(id)) this.items.delete(id); }
    // anything that moves between frames (predicted or still easing): the caller's animation can rest otherwise
    busy() { for (const it of this.items.values()) if (it.moving || it.off || it.chase) return true; return false; }
  }

  // ---- trains: along the track, in s ------------------------------------------------------------------------------
  // The run between two stops: sizeup/trains.py's _shape / profile_s / profile_t, line for line, so where the hub
  // estimates a train and where the page carries it on agree (tests/test_train_motion.py runs both on the same
  // runs). seg: {s0, s1, t_dep, t_arr, accel, decel, vmax}, the hub's segment with its profile settings in it
  // (accel 0.5 m/s^2 away from a stop, decel 0.75 m/s^2 braking into one, vmax 36 m/s). It leaves s0 from rest at
  // t_dep, accelerates, cruises (on through any station it doesn't stop at), brakes and stops at s1 at t_arr. v
  // solves D = v T - k v^2 (k = 1/(2 accel) + 1/(2 decel)), so the run takes exactly T = t_arr - t_dep; no such v
  // within vmax: the quickest run, which arrives late, never past s1.
  function shape(seg) {
    const D = Math.abs(seg.s1 - seg.s0);
    const sign = seg.s1 >= seg.s0 ? 1 : -1;
    const a = seg.accel, b = seg.decel, vmax = seg.vmax;
    if (D <= 0) return [0, sign, a, b, 0, 0, 0, 0];
    const k = 1 / (2 * a) + 1 / (2 * b);
    const T = seg.t_arr - seg.t_dep;
    const disc = T * T - 4 * k * D;
    let v = T > 0 && disc >= 0 ? (2 * D) / (T + Math.sqrt(disc)) : Infinity;
    if (v > vmax) v = Math.min(vmax, Math.sqrt(D / k));
    return [D, sign, a, b, v, v / a, Math.max(0, (D - k * v * v) / v), v / b];
  }
  // profile_s: [s (metres along the line), speed (m/s)] at time t (epoch s)
  function profileS(t, seg) {
    const [D, sign, a, b, v, ta, tc, tb] = shape(seg);
    const tau = t - seg.t_dep;
    if (D <= 0 || tau <= 0) return [Number(seg.s0), 0];
    let x, sp;
    if (tau < ta) { x = 0.5 * a * tau * tau; sp = a * tau; }
    else if (tau < ta + tc) { x = 0.5 * a * ta * ta + v * (tau - ta); sp = v; }
    else if (tau < ta + tc + tb) { const u = ta + tc + tb - tau; x = D - 0.5 * b * u * u; sp = b * u; }
    else { x = D; sp = 0; }
    return [seg.s0 + sign * Math.min(D, Math.max(0, x)), sp];
  }
  // profile_t, profileS's inverse: when the run reaches s
  function profileT(s, seg) {
    const [D, sign, a, b, v, ta, tc, tb] = shape(seg);
    if (D <= 0) return Number(seg.t_dep);
    const x = Math.min(D, Math.max(0, (s - seg.s0) * sign));
    const xa = 0.5 * a * ta * ta;
    let tau;
    if (x <= xa) tau = Math.sqrt((2 * x) / a);
    else if (x <= xa + v * tc) tau = ta + (x - xa) / v;
    else tau = ta + tc + tb - Math.sqrt(Math.max(0, (2 * (D - x)) / b));
    return seg.t_dep + tau;
  }

  // The train predictor. fix (trains.js, from one answer of the hub's): {s, d (+1 toward New Haven, -1 toward New
  // York), train, mode (reported | estimated | stopped), t0 (the feed's time, epoch s, the hub's clock), at (t0 on
  // this browser's clock, ms), fixAt (when the MTA's position first appeared; null: our estimate), hold (hold_s),
  // stop (the next stop's s), until (predict_until; an estimate: t0 + the hub's predict_s), seg, lag (lag_s), v (a GPS
  // fix's speed, m/s: MTA TrainTime, #362; it then runs on from it (gpsRun) instead of the run's profile), a (the
  // hub's filtered acceleration, m/s^2: falling, it brakes)}.
  // dt: seconds since t0. Returns {s, v, d, train, lim, moving}; lim: the furthest it may be drawn on this fix.
  //   stopped     where the hub has it (at the station): not moved until the hub says it left (its mode flips and
  //               a segment comes), never on the timetable alone.
  //   reported    on the run from its fix, the run shifted by how late it reached the fix (lag).
  //   estimated   on the run as the hub times it (drawn hollow).
  // Never behind its fix, never past hold_s, never past the next stop, never after `until`: there it is held.
  // A train with no run (no segment): held at its fix.
  const STOP_ZONE_M = 500;            // a stopped train's fix this close before its stop's mark: standing at it
  // A GPS fix (MTA TrainTime) runs on at its speed (#390: "going quick, decelerating going hollow then picking speed
  // back up"): no slowing on its age alone. It slows only on evidence:
  //   braking     the hub's filter says its speed is falling (a < RAIL_BRAKE_A, confirmed by the distance moved or a
  //               second reading: trains.py speed_check): braked at that rate to a stop (an emergency brake as hard
  //               as the feed shows it: the hub clamps a at 1.5 m/s^2);
  //   its stop    a scheduled stop ahead: braked into it at an ordinary stop's rate (the consist's bs, ~0.8 m/s^2),
  //               the speed never over sqrt(2 bs (distance left)), so it arrives at the stop's mark at rest;
  //   no news     a fix older than RAIL_CRUISE_S (20 s: past the oldest a fix normally is when read, 16 s, a little
  //               under the oldest it gets before the next read): eased to a stop at the consist's service brake (b).
  // A train that stops mid-track with no sign of it before the feed goes quiet is drawn on at its speed until the
  // fix is RAIL_CRUISE_S old (the trade-off for a steady train: see #390), then eased; the next fix glides it back.
  const RAIL_BRAKE_A = -0.2, RAIL_CRUISE_S = 20, RAIL_STEP_S = 0.1;
  // what a consist can do (the hub's dyn, trains.py consist): the M8's when the hub doesn't say
  const DYN = { a0: 0.89, pw: 12.4, b: 1.34, bs: 0.8, vmax: 35.8 };
  const dynOf = (f) => (f && f.dyn && typeof f.dyn.b === "number" ? f.dyn : DYN);
  const accelAt = (dyn, v) => Math.min(dyn.a0, dyn.pw / Math.max(1, v));      // tapering at speed: power / mass
  // [distance on, speed now] tau seconds after a GPS fix at speed v (m/s), its acceleration a (m/s^2, the hub's), room
  // metres to a scheduled stop ahead (Infinity: none), dyn the consist (dynOf)
  function gpsRun(v, a, tau, room = Infinity, dyn = DYN) {
    const brake = a < RAIL_BRAKE_A ? a : 0;
    if (!brake && !(room < Infinity) && tau <= RAIL_CRUISE_S) return [v * tau, v];   // the usual case: steady
    let x = 0, t = 0, u = v;
    const cap = (x_) => (room < Infinity ? Math.sqrt(2 * dyn.bs * Math.max(0, room - x_)) : Infinity);
    u = Math.min(u, cap(0));
    while (t < tau - 1e-9 && u > 0) {
      const h = Math.min(RAIL_STEP_S, tau - t);
      let du = brake * h;
      if (!brake && t + h > RAIL_CRUISE_S) du = -dyn.b * Math.min(h, t + h - RAIL_CRUISE_S);   // (braking already: on so)
      const u1 = Math.max(0, Math.min(u + du, cap(x + u * h)));
      x += (u + u1) * h / 2;
      u = u1;
      t += h;
    }
    if (room < Infinity && (x > room || (u === 0 && room - x < 2 * dyn.bs * RAIL_STEP_S))) x = room;   // (on its mark)
    return [x, u];
  }
  function rail(fix, dt) {
    const d = fix.d >= 0 ? 1 : -1, s0 = fix.s;
    let lim = typeof fix.hold === "number" ? fix.hold : s0;
    if (typeof fix.stop === "number" && (lim - fix.stop) * d > 0 && (fix.stop - s0) * d >= 0) lim = fix.stop;
    if ((lim - s0) * d < 0) lim = s0;
    const out = { s: s0, v: 0, d, train: fix.train, lim, moving: false, est: fix.fixAt == null && fix.mode === "estimated" };
    if (fix.mode === "stopped") {
      // standing at its stop: may stay drawn up to the stop's mark (where it was carried before it stopped), not moved.
      // A GPS fix (v: TrainTime) says where it stopped, mid-track or not: there, nowhere else (#375).
      const gps = typeof fix.v === "number" && fix.fixAt != null;
      if (!gps && typeof fix.stop === "number" && (fix.stop - s0) * d >= 0 && (fix.stop - s0) * d <= STOP_ZONE_M) out.lim = fix.stop;
      if (gps) out.lim = s0;
      return out;
    }
    const T = fix.t0 + dt, until = typeof fix.until === "number" ? fix.until : fix.t0;
    if (typeof fix.v === "number" && fix.v > 0 && fix.fixAt != null) {
      // a GPS fix with its speed (MTA TrainTime, #362): on along the track from when it was taken (gpsRun), braked into
      // its next stop when that is its limit
      const tau = Math.max(0, Math.min(T, until) - fix.fixAt);
      const room = typeof fix.stop === "number" && lim === fix.stop ? Math.max(0, (fix.stop - s0) * d) : Infinity;
      const a = typeof fix.a === "number" ? fix.a : 0, dyn = dynOf(fix);
      if (a < RAIL_BRAKE_A) {                                         // braking: where it comes to rest is its limit
        const rest = Math.min(room, (fix.v * fix.v) / (2 * -a));
        if ((s0 + d * rest - lim) * d < 0) out.lim = lim = s0 + d * rest;
      }
      let [x, v] = gpsRun(fix.v, a, tau, room, dyn);
      let s = s0 + d * x;
      if ((s - lim) * d >= 0) { s = lim; v = 0; }                     // held at its limit
      out.s = s;
      out.v = T < until ? v : 0;
      out.moving = T < until && v > 0 && (lim - s) * d > 0;
      return out;
    }
    if (!fix.seg || (fix.fixAt != null && typeof fix.lag !== "number")) return out;
    let [s, v] = profileS(Math.min(T, until) - (fix.fixAt != null ? fix.lag : 0), fix.seg);
    if ((s - s0) * d < 0) { s = s0; v = 0; }                         // never behind its fix
    if ((s - lim) * d >= 0) { s = lim; v = 0; }                       // held at its limit
    out.s = s;
    out.v = T < until ? v : 0;
    out.moving = T < until && (lim - s) * d > 0;
    return out;
  }
  // without a prediction (the feed stale): where the hub has it, its fix
  const railRest = (fix) => ({ ...rail(fix, 0), s: fix.s, v: 0, moving: false });

  // Corrections along the track (Tracker's follow), each frame from where it was drawn last (it.shown) at the speed it
  // was drawn at (it.u) and the rate that speed was changing (it.acc), so the train never steps, stops or lunges
  // between fixes (#375), and its pace changes no faster than its consist can (#390: dynOf, the hub's dyn):
  //   jump         drawn at the new position at once: nothing drawn yet, more than JUMP_M away, the other way,
  //                or another train number (a turn-back at Stamford).
  //   stale        frozen where it is: nothing moves on old data.
  //   glide        (#390: "fine with an overshoot if we keep it smooth ... then we nudge it back like we did for planes")
  //                a correction no bounded change of pace can make. Drawn beyond this fix's limit (where it stopped,
  //                or will stop, or a held train) while still running: braked to rest at its service brake (on past
  //                it, smoothly), then glided back. A new fix more than GLIDE_AHEAD_M ahead, or the first real fix of
  //                a train drawn on our estimate: glided on to it. A glide follows the latest fix's position over
  //                GLIDE_MIN_S to GLIDE_MAX_S (by its size), as an aircraft eases onto a new fix (Tracker's offset)
  //                but smoother: its offset from the prediction follows a cubic from the speed it was drawn at to rest
  //                on it (no jump, no step in speed). Busy and marked (snapped: trains.js rings it faintly) meanwhile.
  //   otherwise    on at the train's own speed (the prediction's, from the fix's own speed: never a decayed one, so a
  //                lag can't grow, #379) plus the gap over TAU_S, the correction at most SHARE of its speed (+
  //                CORR_MIN_MPS) either way: a new fix's error is absorbed over the next few seconds, no surge. Past
  //                BIG_M off, a little more (at most EXTRA_SHARE of its speed + 2 m/s; no faster than the consist's
  //                vmax unless the train itself is); a train with no GPS speed (GTFS-realtime only: its reports come
  //                1 to 1.6 km apart, its run an estimate) by SHARE_EST, and a report more than GLIDE_EST_M ahead is glided onto. Never backwards; braked into lim as a train stops
  //                (sqrt(2 bs room); harder, up to its full brake b, only when it must). The drawn speed eases onto
  //                that over RAMP_S, its change at most the consist's acceleration (accelAt) or brake (b), and that
  //                change itself eased (JERK_MPS3): no step in speed or in pace.
  // (Measured on the live shape, #375: fixes reach the page 3 to 16 s old, a new one every 3 to 12 s, ±10 m of GPS noise.)
  const JUMP_M = 2000, TAU_S = 6, RAMP_S = 1, CATCH_MAX_MPS = 30, SLOW_SHARE = 0.3, SNUG_M = 3, FRAME_MAX_S = 1;
  const SHARE = 0.15, SHARE_EST = 0.4, EXTRA_SHARE = 0.25, CORR_MIN_MPS = 0.5, BIG_M = 30, JERK_MPS3 = 1.5;
  const GLIDE_AHEAD_M = 400, GLIDE_EST_M = 100, GLIDE_MIN_S = 2, GLIDE_MAX_S = 4, GLIDE_MPS = 30, LIM_SLACK_M = 0.5;
  // the glide's offset from the prediction k (0..1) of the way through: from off0 (moving at m0 = its rate, metres per
  // glide) to 0 at rest on it (cubic Hermite)
  const hermite = (off0, m0, k) => off0 * (2 * k ** 3 - 3 * k * k + 1) + m0 * (k ** 3 - 2 * k * k + k);
  function glideFrom(it, sh, tg, now) {
    const off0 = (sh.s - tg.s) * tg.d, T = Math.max(GLIDE_MIN_S, Math.min(GLIDE_MAX_S, Math.abs(off0) / GLIDE_MPS));
    const u = typeof it.u === "number" ? it.u : 0;                        // drawn speed now, along d (signed)
    it.glide = { off0, m0: (u - Math.max(0, tg.v || 0)) * T, at: now, T };
    it.acc = 0;
  }
  function railFollow(it, tg, now) {
    const sh = it.shown, dt = it.lastAt ? Math.min(FRAME_MAX_S, Math.max(0, (now - it.lastAt) / 1000)) : 0;
    it.lastAt = now;
    const d = tg.d, dyn = dynOf(it.fix);
    if (!sh || sh.d !== d || sh.train !== tg.train || !(Math.abs(tg.s - sh.s) <= JUMP_M)) {
      it.chase = 0; it.u = Math.max(0, tg.v || 0); it.acc = 0; it.glide = null; return tg;
    }
    if (it.stale) { it.chase = 0; it.u = 0; it.acc = 0; it.glide = null; return { ...sh, v: 0, moving: false }; }
    const gap = (tg.s - sh.s) * d, v = Math.max(0, tg.v || 0), room = (tg.lim - sh.s) * d;
    let u0 = typeof it.u === "number" ? it.u : v;
    const beyond = -room > LIM_SLACK_M || (!tg.moving && gap < -LIM_SLACK_M);           // past where this fix allows
    if (beyond && !it.glide && u0 > 0) {
      // (#390: "fine with an overshoot if we keep it smooth"): still running on, braked to rest at its service brake ...
      let acc = typeof it.acc === "number" ? it.acc : 0;
      acc += Math.max(-JERK_MPS3 * dt, Math.min(JERK_MPS3 * dt, -dyn.b - acc));
      const u1 = Math.max(0, u0 + acc * dt);
      const s = sh.s + d * (u0 + u1) / 2 * dt;
      it.u = u1; it.acc = u1 > 0 ? acc : 0; it.chase = 1;
      return { ...tg, s, v: u1 };
    }
    const gps = !!(it.fix && typeof it.fix.v === "number" && it.fix.fixAt != null);
    if (it.glide ? it.fresh : (beyond                                                 // ... then glided back from rest
        || (it.fresh && gap > GLIDE_AHEAD_M)                                            // far behind a new fix
        || (it.fresh && !gps && gap > GLIDE_EST_M)                                      // a GTFS-realtime report ahead
        || (it.fresh && sh.est && !tg.est))) {                                          // a real fix of an estimated train
      glideFrom(it, sh, tg, now);
    }
    if (it.glide) {
      const g = it.glide, k = Math.min(1, (now - g.at) / 1000 / g.T);
      let s = tg.s + d * hermite(g.off0, g.m0, k);
      if (k >= 1) { it.glide = null; s = tg.s; }
      const u = dt ? ((s - sh.s) * d) / dt : v;                                        // (negative: gliding back)
      it.u = it.glide ? u : v; it.acc = 0; it.chase = 1;
      return { ...tg, s, v: Math.max(0, it.u) };
    }
    // (the band by the fix's own speed while it runs, not a prediction eased for want of news: a lag can't grow, #379)
    const vref = Math.max(v, tg.moving && gps ? it.fix.v : 0);
    const extra = Math.max(0, Math.abs(gap) - BIG_M) / TAU_S;
    const band = (gps ? SHARE : SHARE_EST) * vref + CORR_MIN_MPS + (gps ? Math.min(EXTRA_SHARE * vref + 2, extra) : extra);
    let want = v + Math.max(-band, Math.min(band, CATCH_MAX_MPS, gap / TAU_S));
    if (want > v && gps) want = Math.min(want, Math.max(vref * (1 + SHARE) + CORR_MIN_MPS, dyn.vmax || Infinity));
    if (tg.moving) want = Math.max(want, (Math.abs(gap) > BIG_M ? SLOW_SHARE : 1 - SHARE) * v);
    else if (gap > 0) want = Math.max(CORR_MIN_MPS, Math.min(v + CATCH_MAX_MPS, gap / RAMP_S));   // on to a held train
    const rest = tg.moving ? room : Math.min(room, gap);                // where it comes to rest: lim, or a held train's s
    let u = Math.max(0, u0), acc = typeof it.acc === "number" ? it.acc : 0;
    // braked into it as a train stops (less the way it runs while its braking builds up: JERK_MPS3)
    want = Math.max(0, Math.min(want, Math.sqrt(2 * dyn.bs * Math.max(0, rest - u * (dyn.bs / JERK_MPS3) / 2))));
    let aim = (want - u) / RAMP_S;
    const need = rest > 0 ? (u * u) / (2 * rest) : 0;                    // the braking that stops it there
    if (need >= 0.9 * dyn.bs) aim = Math.min(aim, -need);
    aim = Math.max(-dyn.b, Math.min(accelAt(dyn, u), aim));
    acc += Math.max(-JERK_MPS3 * dt, Math.min(JERK_MPS3 * dt, aim - acc));
    if ((want - u) * acc > 0 && Math.abs(acc) * dt > Math.abs(want - u)) acc = dt ? (want - u) / dt : 0;   // not past want
    const u1 = Math.max(0, u + acc * dt);
    let s = sh.s + d * (u + u1) / 2 * dt;
    u = u1;
    if (u === 0) acc = Math.max(0, acc);
    const end = !tg.moving && gap >= 0 ? tg.s : tg.lim;               // onto this fix's limit (or a held train) at rest;
    if ((s - end) * d >= 0 && u <= 1) { s = end; u = 0; acc = 0; }   // faster: on past it, braked, and glided back
    if (!v && Math.abs(gap) <= 0.05) { s = tg.s; u = 0; acc = 0; }    // settled on a held train
    it.u = u; it.acc = acc;
    it.chase = u > 0.01 || gap > SNUG_M ? 1 : 0;                       // busy: still moving, or behind
    return { ...tg, s, v: u };
  }
  // gliding onto a correction (railFollow's glide): the page marks it, faintly
  const snapped = (it) => !!(it && it.glide);
  // the trains' tracker: no horizon of its own (rail stops at `until`), stale with the feed (staleS: the hub's)
  const railTracker = (opts) => new Tracker(rail, { horizonS: Infinity, staleS: 60, ...opts, rest: railRest, follow: railFollow });

  return { tracker: (predict, opts) => new Tracker(predict, opts), arc, metres, wrap180,
           profileS, profileT, rail, railFollow, railTracker, gpsRun, snapped,
           RAIL: { JUMP_M, TAU_S, RAMP_S, CATCH_MAX_MPS, SLOW_SHARE, SNUG_M, STOP_ZONE_M, RAIL_CRUISE_S, RAIL_BRAKE_A,
                   SHARE, SHARE_EST, EXTRA_SHARE, CORR_MIN_MPS, BIG_M, JERK_MPS3, GLIDE_AHEAD_M, GLIDE_EST_M, GLIDE_MIN_S, GLIDE_MAX_S,
                   GLIDE_MPS, DYN } };
})();
