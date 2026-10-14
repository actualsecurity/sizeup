// The cameras on the Overview's map (GitHub #147; a Layers toggle since #168): each camera in our area as a small tile
// pinned at its spot, live where the source streams (camwall.js's tiles: the same stream budget and rules, only tiles
// on screen play), snapshots otherwise. User, 2026-10-14: "... or overlay the cameras on the map too"; then (#168)
// "overlay should only be an option in layers, not here".
//
//   The toggle  "Camera overlay" in the Overview map's Layers box (MapLayers.addGroup, at the top: next to Cameras),
//               off by default, per viewer in the box's "overview" set (MapLayers.prefs, key CamWall.engine.OVERLAY;
//               the wall board's Overview shares it). The Overview's map only: the event's map has its Cameras panel,
//               and the tiles run on the Overview's camera list (camwall.js: polled only while the Overview shows).
//               On in Map, Side by side and Stacked (wherever the map shows).
//   Placing     a DOM layer over the map (not markers: one pass places every tile), re-placed once a frame while the
//               map moves. A tile sits just above its camera; when that would overlap a tile already placed it is
//               nudged (below, beside, further out: CANDIDATES, the first free spot) and a leader line runs to the
//               exact spot. The before / after cameras of an active highway event are placed first and highlighted.
//   Size        by zoom (SIZES: 72 px wide at zoom 10 to 208 at 15 and in); under MIN_ZOOM the tiles hide (the
//               camera icons and the marks stay).
//   Down        a down camera is the dim "!" mark only (a click opens the viewer, which says since when).
//   Off         no tiles: the event's before / after cameras ringed and labelled on the map, so they stand out either way.
// CamOverlay.place() re-places now; CamOverlay.state for the tests.
"use strict";

window.CamOverlay = (() => {
  if (!window.CamWall || !CamWall.engine) return null;
  const E = CamWall.engine;
  const MIN_ZOOM = 10, GAP = 6, STEM = 12, CAP = 18;
  const SIZES = [[15, 208], [14, 176], [13, 144], [12, 112], [11, 88], [MIN_ZOOM, 72]];
  // where a tile may go, relative to its camera (w, h: the tile): just above it, then below, beside, and the corners,
  // then the same further out (rings 2 and 3, for a crowded corner at a far zoom); the first free spot wins
  const DIRS = [[0, -1], [0, 1], [1, 0], [-1, 0], [1, -1], [-1, -1], [1, 1], [-1, 1]];
  const CANDIDATES = (w, h) => {
    const out = [];
    for (let k = 1; k <= 3; k++) {
      for (const [dx, dy] of DIRS) {
        const x = dx === 0 ? -w / 2 : dx > 0 ? STEM + (k - 1) * (w + GAP) : -w - STEM - (k - 1) * (w + GAP);
        const y = dy === 0 ? -h / 2 : dy > 0 ? STEM + (k - 1) * (h + GAP) : -h - STEM - (k - 1) * (h + GAP);
        out.push([x, y]);
      }
    }
    return out;
  };
  const SVG = "http://www.w3.org/2000/svg";
  const KEY = E.OVERLAY;
  const o = { control: null, map: null, layer: null, svg: null, tiles: new Map(), marks: new Map(), lines: new Map(), frame: 0, sig: "" };
  const isOverview = (control) => { try { return !!control.map.getContainer().closest("#ov-map"); } catch (_) { return false; } };
  const isOn = () => !!(o.control && o.control.on(KEY, false));

  // ---- the toggle: a group in the Overview map's Layers box (maplayers.js addGroup); other maps get none ------------
  class OverlayGroup {
    constructor(control) { this.control = control; this.top = true; o.control = control; }
    keys() { return [[KEY, false]]; }
    refresh() { render(); }
    panel() {
      const box = el("div", "ml-group");
      const head = el("div", "ml-ghead");
      head.append(el("b", "", "Camera overlay"));
      box.append(head);
      const lab = el("label", "check");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = isOn();
      cb.dataset.key = KEY;
      cb.addEventListener("change", () => this.control.setLayer(KEY, cb.checked));
      lab.append(cb, ...(window.Icon ? [Icon("camera")] : []), el("span", "", "Live camera views on the map"));
      box.append(lab);
      if (!isOn()) return box;
      const d = E.list(), s = (d && d.streams) || {};
      const cams = ((d && d.cameras) || []).filter((c) => !E.isDown(c));
      const n = Math.max(0, Number(s.per_viewer ?? s.streaming ?? 0));
      if (E.layout() === "cameras") box.append(el("div", "ml-note", "Shows with the map: Map, Side by side or Stacked"));
      else if (d && !d.configured) box.append(el("div", "ml-note", "No camera source is on at the hub"));
      else if (d) box.append(el("div", "ml-note", `${cams.length} in our area${n ? `, up to ${n} live at once` : ""}; from zoom ${MIN_ZOOM}`));
      return box;
    }
  }
  MapLayers.addGroup({ attach: (control) => (isOverview(control) ? new OverlayGroup(control) : {}) });

  function attach() {
    if (o.map) return true;
    if (!o.control) return false;
    o.map = o.control.map;
    o.layer = el("div", "co-layer");
    o.svg = document.createElementNS(SVG, "svg");
    o.svg.setAttribute("class", "co-lines");
    o.svg.setAttribute("aria-hidden", "true");
    o.layer.append(o.svg);
    o.map.getContainer().append(o.layer);
    for (const ev of ["move", "zoom", "resize", "rotate", "pitch"]) o.map.on(ev, schedule);
    return true;
  }
  function schedule() {
    if (!o.frame) o.frame = requestAnimationFrame(() => { o.frame = 0; place(); });
  }

  // ---- what is on the map: tiles and "!" marks for down cameras (the overlay on), rings for the pinned (off) ----------
  function render() {
    if (!attach()) return;
    const mode = E.layout(), d = E.list();
    const cams = (d && d.cameras) || [];
    const overlay = mode !== "cameras" && isOn();
    const want = overlay ? cams.filter((c) => !E.isDown(c)) : [];
    const marks = overlay ? cams.filter(E.isDown) : mode === "cameras" ? [] : cams.filter((c) => c.pin);
    const sig = JSON.stringify([mode, overlay, want.map((c) => [c.id, c.pin, c.lat, c.lon]), marks.map((c) => [c.id, c.pin, E.isDown(c)])]);
    if (sig === o.sig) return schedule();
    o.sig = sig;
    const keep = new Set();
    for (const c of want) {
      const t = E.tile(c, "overlay", { compact: true });
      keep.add(c.id);
      o.tiles.set(c.id, t);
      if (t.el.parentNode !== o.layer) o.layer.append(t.el);
    }
    for (const [id, t] of o.tiles) if (!keep.has(id)) { E.drop(t); o.tiles.delete(id); }
    const mk = new Set();
    for (const c of marks) {
      const k = `${c.id}:${E.isDown(c) ? "down" : "pin"}`;
      mk.add(k);
      let m = o.marks.get(k);
      if (!m) {
        m = { el: el("button", E.isDown(c) ? "co-mark co-down" : "co-mark co-ring"), cam: c };
        m.el.type = "button";
        m.el.dataset.camera = c.id;                         // a click opens the viewer (maplayers.js)
        o.marks.set(k, m);
        o.layer.append(m.el);
      }
      m.cam = c;
      m.el.replaceChildren(E.isDown(c) ? el("span", "ml-down-badge", "!") : el("span", "co-ring-dot"));
      if (c.pin && !E.isDown(c)) m.el.append(el("span", "co-ring-lbl", E.pinText(c)));
      m.el.setAttribute("aria-label", E.isDown(c) ? `camera down: ${c.name}` : `${E.pinText(c)}: ${c.name}`);
      m.el.title = m.el.getAttribute("aria-label");
    }
    for (const [k, m] of o.marks) if (!mk.has(k)) { m.el.remove(); o.marks.delete(k); }
    place();
    E.tick();
  }

  // ---- placing: once a frame while the map moves ------------------------------------------------------------------
  // the map's own furniture stays clear: its buttons along the top, the attribution along the bottom
  const EDGE = { x: 4, top: 44, bottom: 34 };
  const inside = (r, box) => r.x >= EDGE.x && r.y >= EDGE.top && r.x + r.w <= box.w - EDGE.x && r.y + r.h <= box.h - EDGE.bottom;
  const overlaps = (a, b) => a.x < b.x + b.w + GAP && b.x < a.x + a.w + GAP && a.y < b.y + b.h + GAP && b.y < a.y + a.h + GAP;
  function place() {
    if (!o.map || !o.layer) return;
    const z = o.map.getZoom();
    const far = z < MIN_ZOOM;
    o.layer.classList.toggle("co-far", far);
    const W = (SIZES.find(([zz]) => z >= zz) || SIZES[SIZES.length - 1])[1];
    const H = Math.round((W * 9) / 16) + CAP;
    const box = { w: o.map.getContainer().clientWidth, h: o.map.getContainer().clientHeight };
    const placed = [];
    const used = new Set();
    const labels = [];
    for (const m of o.marks.values()) {                    // the marks first: tiles keep off them
      const p = o.map.project([m.cam.lon, m.cam.lat]);
      m.el.style.transform = `translate(${Math.round(p.x)}px, ${Math.round(p.y)}px)`;
      placed.push({ x: p.x - 12, y: p.y - 12, w: 24, h: 24 });
      const lbl = m.el.querySelector(".co-ring-lbl");        // two pinned cameras close together: their labels stack
      if (lbl) {
        const w = lbl.offsetWidth || 90, h = lbl.offsetHeight || 16;
        let k = 0, r;
        do { r = { x: p.x + 16, y: p.y - h / 2 + k * (h + 2), w, h }; k++; } while (k < 6 && labels.some((q) => overlaps(r, q)));
        labels.push(r);
        lbl.style.transform = k > 1 ? `translateY(${(k - 1) * (h + 2)}px)` : "";
      }
    }
    // the pinned first, then top to bottom
    const tiles = [...o.tiles.values()].map((t) => ({ t, p: o.map.project([t.cam.lon, t.cam.lat]) }))
      .sort((a, b) => (!!b.t.cam.pin - !!a.t.cam.pin) || (a.p.y - b.p.y));
    for (const { t, p } of tiles) {
      t.el.style.inlineSize = `${W}px`;
      const on = !far && p.x >= 0 && p.x <= box.w && p.y >= 0 && p.y <= box.h;       // its camera on the map
      let best = null;
      if (on) {
        for (const [dx, dy] of CANDIDATES(W, H)) {
          const r = { x: p.x + dx, y: p.y + dy, w: W, h: H };
          if (inside(r, box) && !placed.some((q) => overlaps(r, q))) { best = r; break; }
        }
        if (!best) {                                         // nowhere free: just above it, kept on the map, overlapping
          const x = Math.max(EDGE.x, Math.min(box.w - EDGE.x - W, p.x - W / 2));
          const y = Math.max(EDGE.top, Math.min(box.h - EDGE.bottom - H, p.y - H - STEM));
          best = { x, y, w: W, h: H };
        }
        placed.push(best);
        t.el.style.transform = `translate(${Math.round(best.x)}px, ${Math.round(best.y)}px)`;
      }
      t.el.hidden = !on;
      line(t.id, on ? p : null, best, !!t.cam.pin);
      used.add(t.id);
    }
    for (const [id, l] of o.lines) if (!used.has(id)) { l.remove(); o.lines.delete(id); }
  }
  // the leader: from the camera's exact spot to the nearest point of its tile, and a dot on the spot
  function line(id, p, r, pinned) {
    let g = o.lines.get(id);
    if (!p || !r) { if (g) g.setAttribute("display", "none"); return; }
    if (!g) {
      g = document.createElementNS(SVG, "g");
      g.append(document.createElementNS(SVG, "line"), document.createElementNS(SVG, "circle"));
      o.svg.append(g);
      o.lines.set(id, g);
    }
    g.removeAttribute("display");
    g.setAttribute("class", pinned ? "co-leader pinned" : "co-leader");
    const x = Math.max(r.x, Math.min(p.x, r.x + r.w)), y = Math.max(r.y, Math.min(p.y, r.y + r.h));
    const [ln, dot] = g.children;
    ln.setAttribute("x1", p.x); ln.setAttribute("y1", p.y); ln.setAttribute("x2", x); ln.setAttribute("y2", y);
    dot.setAttribute("cx", p.x); dot.setAttribute("cy", p.y); dot.setAttribute("r", 3.5);
  }

  E.subscribe(render);
  cad.hooks.push(() => { if (!o.map && attach()) render(); });     // the Overview's map is made on its first show
  render();
  return { place, render, isOn, state: o, MIN_ZOOM, SIZES, KEY };
})();
