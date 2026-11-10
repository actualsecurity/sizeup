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
//                 gently (full speed for 8 s of its age, then slowing; braked to a stop when its speed is falling):
//                 where a train stopped mid-track matters more than a smooth glide (#375).
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
  // A GPS fix (MTA TrainTime) is carried on gently (#375: "if a train stops mid track we want to know where
  // accurately"): at its full speed for RAIL_FULL_S of the fix's age, then slowing, v e^-(age - RAIL_FULL_S)/RAIL_DECAY_S
  // (at most RAIL_DECAY_S x v further, however old); a speed falling faster than RAIL_BRAKE_A (the hub's filter, a):
  // braked at that rate to a stop, the slower of the two.
  const RAIL_FULL_S = 8, RAIL_DECAY_S = 5, RAIL_BRAKE_A = -0.2, RAIL_STEP_S = 0.1;
  // [distance on, speed now] tau seconds after a GPS fix at speed v, braking at a (m/s^2, < 0) or not
  function gpsRun(v, a, tau) {
    const soft = (t) => (t <= RAIL_FULL_S ? v : v * Math.exp(-(t - RAIL_FULL_S) / RAIL_DECAY_S));
    if (!(a < RAIL_BRAKE_A)) {
      if (tau <= RAIL_FULL_S) return [v * tau, v];
      return [v * RAIL_FULL_S + v * RAIL_DECAY_S * (1 - Math.exp(-(tau - RAIL_FULL_S) / RAIL_DECAY_S)), soft(tau)];
    }
    const at = (t) => Math.min(Math.max(0, v + a * t), soft(t));           // braking, never faster than the soft horizon
    let x = 0, t = 0;
    while (t < tau) {                                                       // (trapezoid, RAIL_STEP_S)
      const h = Math.min(RAIL_STEP_S, tau - t);
      x += (at(t) + at(t + h)) * h / 2;
      t += h;
      if (at(t) === 0) break;                                               // stopped
    }
    return [x, at(tau)];
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
      return out;
    }
    const T = fix.t0 + dt, until = typeof fix.until === "number" ? fix.until : fix.t0;
    if (typeof fix.v === "number" && fix.v > 0 && fix.fixAt != null) {
      // a GPS fix with its speed (MTA TrainTime, #362): on along the track from when it was taken, gently (gpsRun)
      const tau = Math.max(0, Math.min(T, until) - fix.fixAt);
      let [x, v] = gpsRun(fix.v, typeof fix.a === "number" ? fix.a : 0, tau);
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
  // was drawn at (it.u), so the train never steps, stops or lunges between fixes (#375):
  //   jump         drawn at the new position at once: nothing drawn yet, more than JUMP_M away, the other way,
  //                or another train number (a turn-back at Stamford); the first real fix of a train drawn on our
  //                estimate (the estimate was a guess: no chase across it).
  //   stale        frozen where it is: nothing moves on old data.
  //   past lim     drawn beyond where this fix allows (its hold point, its stop, or where it stopped): back to it,
  //                never a long slide backwards: up to BACK_EASE_M eased back along the track over about BACK_S
  //                (at most BACK_MAX_MPS); more, put there at once, the jump marked (snapAt: trains.js rings it for
  //                SNAP_MARK_MS) so a correction is seen as one.
  //   otherwise    on at a speed that closes on the prediction: the train's own speed plus the gap over TAU_S (less
  //                when drawn ahead of it, but while the train runs never under SLOW_SHARE of its speed: slowed,
  //                not stopped; held, it comes to rest; never backwards), the drawn speed easing onto that over
  //                RAMP_S (no step in speed either), at most CATCH_MAX_MPS faster than the train; never past lim.
  // (Measured on the live shape, #375: fixes reach the page 3 to 16 s old every 5 s, with ±10 m of GPS noise.)
  const JUMP_M = 2000, TAU_S = 3, RAMP_S = 1, CATCH_MAX_MPS = 30, SLOW_SHARE = 0.3, SNUG_M = 3, FRAME_MAX_S = 1;
  const BACK_EASE_M = 30, BACK_S = 1.5, BACK_MIN_MPS = 2, BACK_MAX_MPS = 20, SNAP_MARK_MS = 2500;
  function railFollow(it, tg, now) {
    const sh = it.shown, dt = it.lastAt ? Math.min(FRAME_MAX_S, Math.max(0, (now - it.lastAt) / 1000)) : 0;
    it.lastAt = now;
    const d = tg.d;
    if (!sh || sh.d !== d || sh.train !== tg.train || !(Math.abs(tg.s - sh.s) <= JUMP_M) || (sh.est && !tg.est)) {
      it.chase = 0; it.u = Math.max(0, tg.v || 0); it.back = 0; return tg;
    }
    if (it.stale) { it.chase = 0; it.u = 0; return { ...sh, v: 0, moving: false }; }
    const over = (sh.s - tg.lim) * d;
    if (over > 0.5) {                                                  // beyond this fix's limit: back to it
      it.u = 0;
      if (over > BACK_EASE_M && (!it.back || it.fresh)) {                            // too far to slide: put there, marked
        it.chase = 1; it.snapAt = now;                                 // (busy while marked: the ring goes out)
        return { ...tg, s: tg.lim, v: 0 };
      }
      if (it.fresh || !it.back) it.back = Math.min(BACK_MAX_MPS, Math.max(BACK_MIN_MPS, over / BACK_S));
      it.chase = 1;
      const step = it.back * dt;
      if (step >= over) { it.chase = 0; it.back = 0; return { ...tg, s: tg.lim, v: 0 }; }
      return { ...tg, s: sh.s - d * step, v: 0 };
    }
    it.back = 0;
    const gap = (tg.s - sh.s) * d, v = Math.max(0, tg.v || 0);
    const want = Math.max(tg.moving ? SLOW_SHARE * v : 0, Math.min(v + CATCH_MAX_MPS, v + gap / TAU_S));
    let u = typeof it.u === "number" ? it.u : v;
    u += (want - u) * Math.min(1, dt / RAMP_S);
    if (u < 0.01 && want === 0) u = 0;
    let s = sh.s + d * u * dt;
    const room = (tg.lim - sh.s) * d;
    if (u * dt >= room) { s = tg.lim; u = 0; }                       // never past this fix's limit
    if (!v && Math.abs(gap) <= 0.05) { s = tg.s; u = 0; }             // settled on a held train
    it.u = u;
    it.chase = u > 0.01 || gap > SNUG_M || snapped(it, now) ? 1 : 0;   // busy: still moving, behind, or marked
    return { ...tg, s, v: u };
  }
  // put back (railFollow's snap) within the last SNAP_MARK_MS: the page marks it
  const snapped = (it, now) => !!(it && it.snapAt && now - it.snapAt < SNAP_MARK_MS);
  // the trains' tracker: no horizon of its own (rail stops at `until`), stale with the feed (staleS: the hub's)
  const railTracker = (opts) => new Tracker(rail, { horizonS: Infinity, staleS: 60, ...opts, rest: railRest, follow: railFollow });

  return { tracker: (predict, opts) => new Tracker(predict, opts), arc, metres, wrap180,
           profileS, profileT, rail, railFollow, railTracker, gpsRun, snapped,
           RAIL: { JUMP_M, TAU_S, RAMP_S, CATCH_MAX_MPS, SLOW_SHARE, SNUG_M, STOP_ZONE_M, RAIL_FULL_S, RAIL_DECAY_S, RAIL_BRAKE_A,
                   BACK_EASE_M, BACK_S, BACK_MAX_MPS, SNAP_MARK_MS } };
})();
