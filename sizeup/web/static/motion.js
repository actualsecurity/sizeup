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
//                 re-acquired target and jumps. A trains predictor (along the track at speed) plugs in next.
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
  //   predict(fix, dtS) -> {lon, lat, rot} | null   (null: not moving; drawn at its fix)
  //   fix: {lon, lat, rot, at (ms, this browser's clock, when the position was true), ...what predict reads}
  class Tracker {
    constructor(predict, { horizonS = 10, staleS = 15, easeMs = 1000, snapM = 2000 } = {}) {
      Object.assign(this, { predict, horizonS, staleS, easeMs, snapM });
      this.items = new Map();       // id -> {fix, off: {lon, lat, rot}, easeAt, shown, stale, moving}
    }
    target(it, now) {
      const age = (now - it.fix.at) / 1000;
      it.stale = age > this.staleS;
      const p = it.stale ? null : this.predict(it.fix, Math.min(this.horizonS, Math.max(0, age)));
      it.moving = !!p && age < this.horizonS;
      return p || { lon: it.fix.lon, lat: it.fix.lat, rot: it.fix.rot };
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
      const same = it.fix.lon === fix.lon && it.fix.lat === fix.lat && it.fix.at === fix.at;
      if (!same) this.where(id, now);          // where it is drawn now, by the fix it had
      it.fix = fix;
      if (!same) this.rebase(it, this.target(it, now), now);
    }
    where(id, now = Date.now()) {
      const it = this.items.get(id);
      if (!it) return null;
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
    busy() { for (const it of this.items.values()) if (it.moving || it.off) return true; return false; }
  }

  return { tracker: (predict, opts) => new Tracker(predict, opts), arc, metres, wrap180 };
})();
