// Following a mark and lighting it up on the CAD maps: one helper for the aircraft (aircraft.js, #352) and our own
// units (cadmap.js, #358), so both behave the same.
//
//   MapFollow.Follower   one per map and layer: start(id, at) centres the mark (zoomed in) and fires sizeup:follow
//                        (cadmap.js: no refitting meanwhile; the map's other follower lets go), pan(lonlat) keeps it
//                        centred (eased over PAN_MS, at most every PAN_MS, only once it is a pixel off, never over a
//                        zoom: a moveend at most 4 times a second), chip(text, tip, flags) the "Following ..." chip at
//                        the foot of the map with ✕. Any pan of the viewer's (a mouse or one-finger drag, the
//                        arrow keys, a sideways trackpad swipe), Recenter (sizeup:recenter), ✕ or stop() lets go (a
//                        pinch or the wheel zooms and keeps following); zooming is round the centre (the mark) meanwhile. takeMap(map): a pick or a focus
//                        (centred and ringed, not followed) lets every follower on that map go.
//   MapFollow.Lights     one per layer: the mark picked from a list or a chip (pick: ringed for HIGHLIGHT_MS once
//                        centred) and the ones whose row or card is hovered (hover: a wider ring).
//   MapFollow.shownMap   the first of a layer's maps on screen.
//
// No drawing here: each layer draws its own ring (aircraft: a GeoJSON source; units: their chips' classes).
"use strict";

window.MapFollow = (() => {
  const FOLLOW_ZOOM = 12;                   // centred at least this close
  const PAN_MS = 250;                       // following: eased onto the mark at most this often
  const EASE_MS = 600;                      // the first move onto it
  const HIGHLIGHT_MS = 12000;               // a picked mark, ringed this long once centred
  const STOP_TIP = "A drag or Recenter stops following";

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function ago(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s} s`;
    const m = Math.round(s / 60);
    return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
  }

  class Follower {
    // opts: {cls (the chip's classes), textCls, icon (Icon name), onStop()}
    constructor(map, host, opts = {}) {
      this.map = map;
      this.host = host;
      this.opts = opts;
      this.id = "";
      this.panAt = 0;
      this.chipEl = null;
      // Any pan of the viewer's lets go at once; a zoom (a pinch, the wheel, +/-) keeps following round the mark (#375).
      // MapLibre's own events carry the viewer's input as originalEvent; our eases carry none. While a finger or the
      // mouse is down on the map no pan is eased (grab): an easeTo resets MapLibre's gestures, so a press-and-hold
      // drag never got to its dragstart and the map stayed on the mark.
      this.grab = false;
      map.on("dragstart", (e) => { if (userPan(e && e.originalEvent)) this.stop(); });
      map.on("movestart", (e) => {             // the arrow keys pan (+ and - zoom)
        const o = e && e.originalEvent;
        if (o && o.type === "keydown" && /^Arrow/.test(o.key || "")) this.stop();
      });
      let box = null;
      try { box = map.getCanvasContainer ? map.getCanvasContainer() : null; } catch (_) { /* a stand-in map */ }
      if (box && box.addEventListener) {
        const down = () => { this.grab = true; };
        const up = (ev) => { if (!(ev && ev.touches && ev.touches.length)) this.grab = false; };
        box.addEventListener("mousedown", down);
        box.addEventListener("touchstart", down, { passive: true });
        for (const [k, o] of [["mouseup", false], ["touchend", { passive: true }], ["touchcancel", { passive: true }], ["blur", false]]) {
          try { window.addEventListener(k, up, o); } catch (_) { /* no window */ }
        }
        // a sideways two-finger swipe on a trackpad (the wheel's x): a pan to the viewer; MapLibre zooms on the wheel's y
        box.addEventListener("wheel", (ev) => {
          if (!ev.ctrlKey && Math.abs(ev.deltaX || 0) > 2 * Math.abs(ev.deltaY || 0)) this.stop();
        }, { passive: true });
      }
      map.on("sizeup:recenter", () => this.stop());
      map.on("sizeup:follow", (e) => { if (e && e.follower && e.follower !== this) this.stop(); });   // one at a time
      map.on("sizeup:takemap", () => this.stop());   // a pick or a focus (centred, ringed) takes the map from any follower
    }
    // follow id: centred at `at` ({lon, lat}, if known), zoomed in to at least `zoom`
    start(id, at, zoom = FOLLOW_ZOOM) {
      if (!id) return;
      this.id = id;
      for (const z of [this.map.scrollZoom, this.map.touchZoomRotate]) {     // zoom round the centre (the mark) meanwhile
        try { if (z.isEnabled()) { if (z.disable) z.disable(); z.enable({ around: "center" }); } } catch (_) { /* an older MapLibre */ }
      }
      this.map.fire("sizeup:follow", { follower: this });
      if (at && typeof at.lon === "number" && typeof at.lat === "number") {
        try { this.map.easeTo({ center: [at.lon, at.lat], zoom: Math.max(this.map.getZoom(), zoom), duration: EASE_MS }); } catch (_) { /* */ }
        this.panAt = performance.now() + EASE_MS - PAN_MS;                   // its first pan after that ease
      }
    }
    stop() {
      if (!this.id) return;
      this.id = "";
      for (const z of [this.map.scrollZoom, this.map.touchZoomRotate]) {
        try { if (z.isEnabled()) { if (z.disable) z.disable(); z.enable(); } } catch (_) { /* */ }
      }
      if (this.chipEl) this.chipEl.hidden = true;
      if (this.opts.onStop) this.opts.onStop();
    }
    // a frame while following: the map eased under the mark (never while it moves otherwise: an ease, a zoom)
    pan(lonlat) {
      if (!this.id || !lonlat) return false;
      const now = performance.now();
      const ours = now < (this.panAt || 0) + PAN_MS + 60;                    // moving: our last pan, finishing
      if (this.grab) return false;                                           // the viewer's finger or mouse is down
      if (!(now - (this.panAt || 0) >= PAN_MS && !this.map.isZooming() && (!this.map.isMoving() || ours))) return false;
      const [lon, lat] = lonlat, p = this.map.project([lon, lat]), c = this.map.project(this.map.getCenter());
      if (Math.hypot(p.x - c.x, p.y - c.y) < 1) return false;
      this.panAt = now;
      this.map.easeTo({ center: [lon, lat], duration: PAN_MS, easing: (t) => t });
      return true;
    }
    chipShown() { return !!this.chipEl && !this.chipEl.hidden; }
    // the chip at the foot of the map: its words, its tooltip (the facts, then how to stop), flags toggled as classes
    chip(text, tip, flags) {
      if (!this.chipEl) {
        const c = h("div", this.opts.cls || "ac-follow");
        c.setAttribute("role", "status");
        c.addEventListener("click", (e) => e.stopPropagation());
        const x = h("button", "mini quiet", "✕");
        x.type = "button";
        x.title = "Stop following";
        x.setAttribute("aria-label", "Stop following");
        x.addEventListener("click", (e) => { e.stopPropagation(); this.stop(); });
        this.chipText = h("span", this.opts.textCls || "ac-follow-t");
        c.append(...(window.Icon && this.opts.icon ? [Icon(this.opts.icon)] : []), this.chipText, x);
        this.host.append(c);
        this.chipEl = c;
      }
      if (this.chipText.textContent !== text) this.chipText.textContent = text;
      const title = (tip ? `${tip}\n` : "") + STOP_TIP;
      if (this.chipEl.title !== title) this.chipEl.title = title;
      for (const [k, on] of Object.entries(flags || {})) this.chipEl.classList.toggle(k, !!on);
      this.chipEl.hidden = false;
    }
  }

  class Lights {
    // onChange(): a picked mark's time is up (a layer that redraws on a loop of its own needs none)
    constructor(onChange) {
      this.onChange = onChange || null;
      this.picked = null;                   // {id, until (ms)}
      this.hot = "";                        // the hovered ids, space-separated
      this.timer = 0;
    }
    pick(id, ms = HIGHLIGHT_MS) {
      this.picked = { id, until: Date.now() + ms };
      if (this.onChange) {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => { this.timer = 0; this.onChange(); }, ms + 50);
      }
    }
    isPicked(id) { return !!this.picked && this.picked.id === id && Date.now() < this.picked.until; }
    anyPicked() { return !!this.picked && Date.now() < this.picked.until; }
    // the ids whose row or card is hovered ([] for none): true when that changed
    hover(ids) {
      const k = (ids || []).filter(Boolean).join(" ");
      if (k === this.hot) return false;
      this.hot = k;
      return true;
    }
    hovered(id) { return !!this.hot && this.hot.split(" ").includes(id); }
    hoveredIds() { return this.hot ? this.hot.split(" ") : []; }
  }

  // the first of these maps on screen (each {map}: a MapLibre map)
  function shownMap(maps) {
    return [...maps].find((m) => { try { return !!m.map.getContainer().offsetParent; } catch (_) { return false; } }) || null;
  }

  // a drag of the viewer's: a mouse, or one finger (two are a pinch: a zoom round the mark)
  function userPan(o) {
    if (!o) return false;
    const t = o.touches;
    return !(t && t.length > 1);
  }

  const takeMap = (map) => { try { map.fire("sizeup:takemap"); } catch (_) { /* */ } };

  return { Follower, Lights, shownMap, takeMap, ago, FOLLOW_ZOOM, PAN_MS, EASE_MS, HIGHLIGHT_MS };
})();
