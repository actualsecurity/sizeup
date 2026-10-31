// Aircraft between fixes, drawn without stepping backwards, and their trails as curves (GitHub #179).
// The user: "we shouldnt push it backwards unless there is a real reason to (like its over a certain distance)
// otherwise we should slow it down slightly / speed it up slightly if we missed the location guess ... we should
// also always smooth out the radius on the trails"; and the trail must run right up to the plane, no gap.
//
//   the estimate  motion.js's predictor (Motion.arc: on along the reported track at the ground speed, round its
//                 turn) from the latest fix at the time the fix was true, at most horizonS (10 s) past it, never
//                 once it is stale (15 s): the same limits as before.
//   a new fix     moves the estimate, not the mark. Each frame the mark is drawn from where it was drawn last,
//                 and its miss against the estimate is closed (converge()): along the track by drawing it slower
//                 when it is ahead (down to SLOW of its speed, never backwards) and faster when it is behind (up
//                 to FAST), across the track by drifting over, both over about CONVERGE_S. A miss over snapM()
//                 (0.3 nm, or SNAP_S of its travel when that is less), a heading off by more than SNAP_DEG, a new
//                 aircraft, or a change between fresh and stale is a jump: a real reason.
//   the trail     the reported positions (the hub's trail and the latest fix) joined to the mark where it is
//                 drawn (line()), smoothed by Chaikin's corner cutting (smooth()): the corners rounded and the
//                 curve always inside them, never overshooting a reported point; it ends exactly on the mark.
//
// Pure functions (no map, no clock of its own): tests/test_aircraft_motion.py runs them in node.
"use strict";

(function (root) {
  const R = 6371008.8, DEG = Math.PI / 180, KT = 1852 / 3600;
  const OPTS = {
    CONVERGE_S: 3,      // a miss is closed over about this many seconds (the time constant)
    SLOW: 0.5,          // ahead of the estimate: drawn at no less than this share of its speed, so never backwards
    FAST: 1.5,          // behind it: at no more than this share
    SNAP_M: 556,        // 0.3 nm: a bigger miss is not chased, it jumps (a re-acquired target, a bad fix)
    SNAP_S: 4,          // ... nor one bigger than this many seconds of its own travel, when that is less
    SNAP_DEG: 45,       // ... nor a heading off by more than this (it turned hard between fixes)
    EASE_S: 0.5,        // a mark that is not moving (ground, under 30 kt, past the horizon) eases onto its spot
    TURN_S: 0.4,        // the drawn heading follows the estimate's with this time constant
  };
  const SETTLED_M = 0.5;   // closer than this (and the heading within a degree): caught up, nothing left to animate
  const PASSES = 3;        // Chaikin passes: a trail of n points becomes about 8n, the corners well rounded
  const CUT_M = 100;       // a corner is cut at most this far along each leg (a quarter of the leg when that is less)
  const SAME_M = 1;        // reported points closer than this are one point

  const wrap180 = (d) => ((d + 180) % 360 + 360) % 360 - 180;
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  // metres east and north of ref, on a flat patch of the earth (fine over a few km)
  function xy(ref, lon, lat) {
    return [(lon - ref.lon) * DEG * R * Math.cos(ref.lat * DEG), (lat - ref.lat) * DEG * R];
  }
  function lonlat(ref, x, y) {
    return [ref.lon + x / (R * Math.cos(ref.lat * DEG)) / DEG, ref.lat + y / R / DEG];
  }
  function metres(a, b) { const [x, y] = xy(a, b.lon, b.lat); return Math.hypot(x, y); }

  // How big a miss may be closed by changing speed rather than jumping: SNAP_S of its travel (v, m/s), at most SNAP_M.
  // A mark that is not moving eases onto its spot from anywhere within SNAP_M.
  function snapM(v, o = OPTS) { return v > 0 ? Math.min(o.SNAP_M, o.SNAP_S * v) : o.SNAP_M; }

  // One frame. prev: where the mark was drawn last frame {lon, lat, rot}; from: where the estimate (by the fix it has
  // now) put it at that frame's time; to: where the estimate puts it now {lon, lat, rot, v (m/s along its track, 0:
  // not moving)}; dt: seconds since the last frame. Returns where to draw it now {lon, lat, rot, snap}.
  function converge(prev, from, to, dt, o = OPTS) {
    if (!prev || !from) return { lon: to.lon, lat: to.lat, rot: to.rot || 0, snap: true };
    const [ex, ey] = xy(from, prev.lon, prev.lat);              // the miss: metres east / north of the estimate
    const v = to.v > 0 ? to.v : 0;
    const turn = wrap180((prev.rot || 0) - (from.rot || 0));
    if (Math.hypot(ex, ey) > snapM(v, o) || Math.abs(turn) > o.SNAP_DEG) return { lon: to.lon, lat: to.lat, rot: to.rot || 0, snap: true };
    dt = Math.max(0, dt);
    let x, y;
    if (v > 0) {
      const h = (to.rot || 0) * DEG, ux = Math.sin(h), uy = Math.cos(h);       // along its track
      const along = ex * ux + ey * uy, across = ex * uy - ey * ux;             // + ahead of the estimate; + right of it
      // close the along-track miss over CONVERGE_S, but by no more than the speed limits allow: drawn at
      // (1 - closed / (v dt)) of its speed, between SLOW and FAST
      const closed = clamp(along * (1 - Math.exp(-dt / o.CONVERGE_S)), -(o.FAST - 1) * v * dt, (1 - o.SLOW) * v * dt);
      const a = along - closed, c = across * Math.exp(-dt / o.CONVERGE_S);
      x = a * ux + c * uy;
      y = a * uy - c * ux;
    } else {
      const k = Math.exp(-dt / o.EASE_S);
      x = ex * k;
      y = ey * k;
    }
    const [lon, lat] = lonlat(to, x, y);
    return { lon, lat, rot: (to.rot || 0) + turn * Math.exp(-dt / o.TURN_S), snap: false };
  }

  // One per layer: set() each fix as it comes, where() each frame.
  //   predict(fix, dtS) -> {lon, lat, rot} | null   (Motion.arc; null: not moving, drawn at its fix)
  //   fix: {lon, lat, rot, at (ms, this browser's clock, when the position was true), gs (kt), ...what predict reads}
  class Follower {
    constructor(predict, { horizonS = 10, staleS = 15, ...o } = {}) {
      this.predict = predict;
      this.horizonS = horizonS;
      this.staleS = staleS;
      this.o = { ...OPTS, ...o };
      this.items = new Map();        // id -> {fix, shown, t, stale, moving, predicted, settled, snaps}
    }
    // where the latest fix puts it at time t (ms): moved on at most horizonS, never once stale
    estimate(fix, t) {
      const age = (t - fix.at) / 1000;
      const stale = age > this.staleS;
      const p = stale ? null : this.predict(fix, Math.min(this.horizonS, Math.max(0, age)));
      const moving = !!p && age < this.horizonS;
      const at = p || fix;
      return { lon: at.lon, lat: at.lat, rot: at.rot ?? fix.rot ?? 0, v: moving ? (fix.gs || 0) * KT : 0, stale, moving,
               predicted: !!p && age > 0 };
    }
    set(id, fix) {
      const it = this.items.get(id);
      if (it) it.fix = fix;
      else this.items.set(id, { fix, shown: null, t: 0, stale: false, moving: false, predicted: false, settled: false, snaps: 0 });
    }
    where(id, now = Date.now()) {
      const it = this.items.get(id);
      if (!it) return null;
      if (it.shown && it.t === now) return it.shown;             // another map, the same frame
      const to = this.estimate(it.fix, now);
      const p = it.shown && to.stale === it.stale
        ? converge(it.shown, this.estimate(it.fix, it.t), to, (now - it.t) / 1000, this.o)
        : { lon: to.lon, lat: to.lat, rot: to.rot, snap: true };  // new, or gone stale / fresh again: drawn where it is
      if (p.snap) it.snaps += 1;
      it.shown = { lon: p.lon, lat: p.lat, rot: p.rot };
      it.t = now;
      it.stale = to.stale;
      it.moving = to.moving;
      it.predicted = to.predicted;
      it.settled = metres(to, p) < SETTLED_M && Math.abs(wrap180(p.rot - to.rot)) < 1;
      return it.shown;
    }
    stale(id) { const it = this.items.get(id); return !!(it && it.stale); }
    // drawn somewhere the reports don't put it: moved on from its fix (or held at the horizon), or still catching up
    estimated(id) { const it = this.items.get(id); return !!(it && (it.predicted || !it.settled)); }
    forget(keep) { for (const id of [...this.items.keys()]) if (!keep.has(id)) this.items.delete(id); }
    // anything to animate: something moving, or a mark still catching up with its estimate
    busy() { for (const it of this.items.values()) if (it.moving || !it.settled) return true; return false; }
  }

  // The trail to draw: the reported positions ([lon, lat], oldest first) up to the mark where it is drawn
  // ({lon, lat, rot}). The newest ones the mark has not reached yet (ahead of it, inside 45 degrees of its heading
  // and closer than SNAP_M: while it catches up from behind) wait until it has, so the line never runs out ahead
  // of the plane and back. Only from the newest back: the first one behind it keeps every older one.
  function line(reported, mark, o = OPTS) {
    const pts = [];
    for (const p of reported) {
      const last = pts[pts.length - 1];
      if (!last || metres({ lon: last[0], lat: last[1] }, { lon: p[0], lat: p[1] }) >= SAME_M) pts.push(p);
    }
    if (!mark) return pts;
    const h = (mark.rot || 0) * DEG, ux = Math.sin(h), uy = Math.cos(h);
    while (pts.length) {
      const [x, y] = xy(mark, pts[pts.length - 1][0], pts[pts.length - 1][1]);
      const along = x * ux + y * uy, across = Math.abs(x * uy - y * ux);
      if (Math.hypot(x, y) < SAME_M || (along > 0 && across <= along && along <= o.SNAP_M)) pts.pop();
      else break;
    }
    pts.push([mark.lon, mark.lat]);
    return pts;
  }

  // The line as a curve: Chaikin's corner cutting, PASSES times (each corner cut at a quarter of each leg, never
  // more than CUT_M in): the corners rounded, the curve always inside them (it never swings out past a reported
  // point the way a curve forced through every point does), straight legs left straight, the first point and the
  // last (the mark) kept exactly.
  function smooth(pts) {
    const n = pts.length;
    if (n < 3) return pts.slice();
    const ref = { lon: pts[n - 1][0], lat: pts[n - 1][1] };
    let P = pts.map((p) => xy(ref, p[0], p[1]));
    for (let k = 0; k < PASSES; k++) {
      const Q = [P[0]];
      for (let i = 0; i < P.length - 1; i++) {
        const [ax, ay] = P[i], [bx, by] = P[i + 1];
        const L = Math.hypot(bx - ax, by - ay);
        if (L < 1e-6) continue;
        const c = Math.min(0.25, CUT_M / L);
        Q.push([ax + c * (bx - ax), ay + c * (by - ay)], [bx - c * (bx - ax), by - c * (by - ay)]);
      }
      Q.push(P[P.length - 1]);
      P = Q;
    }
    const out = P.map(([x, y]) => lonlat(ref, x, y));
    out[0] = pts[0];
    out[out.length - 1] = pts[n - 1];
    return out;
  }

  root.Glide = { OPTS, KT, follower: (predict, opts) => new Follower(predict, opts), converge, snapM, line, smooth, metres, wrap180 };
})(typeof window !== "undefined" ? window : globalThis);
