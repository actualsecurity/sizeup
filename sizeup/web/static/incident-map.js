// The incident view's map, resized by dragging (GitHub #210).
//
// User, 2026-11-02: "id also like to change the inc view so you can pull the map out larger and down more if you want
// ... id like to be able to make it pull down and to the left/right".
//
//   The handles  on the map's own edges (cad.html #d-map-w / -h / -c): the left edge makes it wider (the details column
//                gives way: the map is on the right, its right edge is the column's), the bottom edge taller, the
//                bottom-left corner both. The edges are separators (Panels.separator: drag, arrow keys 16 px, Home or a
//                double-click resets that one); the corner takes all four arrows, and Home or a double-click resets both.
//   Limits       height H_MIN px to 85% of the window; width W_MIN px (the layout's own 18rem least) to what leaves the
//                details DETAILS_MIN px. clamp() is the arithmetic (pure: tests/test_incident_map.py).
//   Remembered   per viewer: localStorage "sizeup.incidentMap" {w, h} (px, in try/catch, like the new call's splitter);
//                a size not set is the layout's own (style.css --map-h, cad.css .inc-grid).
//   The map      MapLibre's resize() once a frame while dragging and once after (Panels.resizeMaps), so it neither
//                stretches nor loses its view; after, the call is framed again (as on any size change), or, if the
//                dispatcher had moved the map, the incident's pin brought back into view if it left it.
//   Expand       the large map is its own overlay (cadmap.js openBig): over this size, and closing it comes back to it.
//   Narrow       one column (cad.css: the grid under 44rem), or a window NARROW_PX wide or less: the map's width is the
//                layout's, the left edge and the corner hidden; the height still applies.
"use strict";

window.IncidentMap = (() => {
  const KEY = "sizeup.incidentMap";
  const H_MIN = 300, H_MAX_VH = 0.85;           // the map's height: at least (the layout's 360 px, a bit less), at most
  const W_MIN = 288, DETAILS_MIN = 280;         // its width at least (18rem); the details column's at least
  const NARROW_PX = 1000, STEP = 16;

  // What a viewer asked for ({w, h}: px, null = the layout's own) within the room now ({grid: the grid's width, gap:
  // its column gap, vh: the window's height, wide: the two columns side by side}): {w, h, wMax, hMax}; w null when not wide.
  function clamp(want, room) {
    const hMax = Math.max(H_MIN, Math.floor(room.vh * H_MAX_VH));
    const wMax = Math.max(W_MIN, Math.floor(room.grid - room.gap - DETAILS_MIN));
    const fit = (v, lo, hi) => (v == null || !Number.isFinite(v) ? null : Math.round(Math.max(lo, Math.min(hi, v))));
    return { w: room.wide ? fit(want.w, W_MIN, wMax) : null, h: fit(want.h, H_MIN, hMax), wMax, hMax };
  }
  const api = { clamp, KEY, H_MIN, H_MAX_VH, W_MIN, DETAILS_MIN, NARROW_PX };
  if (typeof document === "undefined" || !document.getElementById("d-mapbox") || !window.Panels) return api;

  function saved() {
    try {
      const v = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
      const px = (x) => (Number.isFinite(x) && x > 0 ? x : null);
      return { w: px(v.w), h: px(v.h) };
    } catch (_) { return { w: null, h: null }; }
  }
  function remember() {
    try { localStorage.setItem(KEY, JSON.stringify({ w: st.w, h: st.h })); } catch (_) { /* this page view only */ }
  }
  const st = { ...saved(), seps: [] };
  const grid = document.getElementById("d-grid"), box = document.getElementById("d-mapbox");
  const hW = document.getElementById("d-map-w"), hH = document.getElementById("d-map-h"), hC = document.getElementById("d-map-c");
  const NARROW = matchMedia(`(max-width: ${NARROW_PX}px)`);

  function room() {
    const cs = getComputedStyle(grid);
    const cols = cs.gridTemplateColumns.trim().split(/\s+/).filter((c) => c && c !== "none").length;
    return { grid: grid.clientWidth, gap: parseFloat(cs.columnGap) || 0, vh: innerHeight, wide: !NARROW.matches && cols > 1 };
  }
  const now = () => clamp(st, room());
  const width = () => Math.round(box.getBoundingClientRect().width);
  const height = () => Math.round(box.getBoundingClientRect().height);
  const mapShown = () => !document.getElementById("d-map").hidden;

  function apply() {
    const r = room(), c = clamp(st, r);
    if (c.w == null) grid.style.removeProperty("--dmap-w"); else grid.style.setProperty("--dmap-w", `${c.w}px`);
    if (c.h == null) grid.style.removeProperty("--dmap-h"); else grid.style.setProperty("--dmap-h", `${c.h}px`);
    const on = mapShown();
    hW.hidden = hC.hidden = !(on && r.wide);
    hH.hidden = !on;
    for (const s of st.seps) s.sync();
    syncCorner();
  }

  // the size settled (a drag ended, a key, a reset): remembered; the map framed again, or its pin kept in view
  function settled() {
    remember();
    requestAnimationFrame(() => {
      const rm = window.CadMap && CadMap.detailMap && CadMap.detailMap();
      if (!rm || !rm.canvas.clientWidth) return;
      rm.map.resize();
      rm.size = `${rm.canvas.clientWidth}x${rm.canvas.clientHeight}`;
      if (!rm.userMoved) { rm.fit(true); return; }
      const inc = rm.focus;
      if (inc && inc.lat != null && inc.lon != null && !rm.map.getBounds().contains([inc.lon, inc.lat])) {
        rm.map.easeTo({ center: [inc.lon, inc.lat], duration: 400 });
      }
    });
  }
  function set(w, h, done) {
    const c = clamp({ w, h }, room());
    if (w !== undefined && c.w != null) st.w = c.w;
    if (h !== undefined && c.h != null) st.h = c.h;
    apply();
    if (done) settled();
  }
  function reset(which) {
    if (which !== "h") st.w = null;
    if (which !== "w") st.h = null;
    apply();
    Panels.resizeMaps();
    settled();
  }

  // ---- the edges: separators like the side columns' (panels.js) ---------------------------------------------------
  st.seps.push(Panels.separator(hW, {
    orient: "vertical", grow: -1, label: "Resize the map: wider to the left", controls: "d-mapbox",   // left: wider
    min: () => W_MIN, max: () => now().wMax, value: width, enabled: () => room().wide,
    set: (px, done) => set(px, undefined, done),
    reset: () => reset("w"),
  }));
  st.seps.push(Panels.separator(hH, {
    orient: "horizontal", grow: 1, label: "Resize the map: taller", controls: "d-mapbox",
    min: () => H_MIN, max: () => now().hMax, value: height,
    set: (px, done) => set(undefined, px, done),
    reset: () => reset("h"),
  }));

  // ---- the corner: both at once ------------------------------------------------------------------------------------
  hC.setAttribute("role", "separator");
  hC.setAttribute("aria-label", "Resize the map: wider to the left, taller down");
  hC.setAttribute("aria-controls", "d-mapbox");
  hC.title = "Resize the map: drag, or arrow keys; double-click to reset";
  hC.tabIndex = 0;
  function syncCorner() { hC.setAttribute("aria-valuetext", `${width()} by ${height()} pixels`); }
  hC.addEventListener("dblclick", () => reset("both"));
  hC.addEventListener("keydown", (e) => {
    if (e.key === "Home") { e.preventDefault(); reset("both"); return; }
    const d = { ArrowLeft: [STEP, 0], ArrowRight: [-STEP, 0], ArrowDown: [0, STEP], ArrowUp: [0, -STEP] }[e.key];
    if (!d) return;
    e.preventDefault();
    set(width() + d[0], height() + d[1], true);
    Panels.resizeMaps();
  });
  let frame = 0;
  hC.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !room().wide) return;
    e.preventDefault();
    const x0 = e.clientX, y0 = e.clientY, w0 = width(), h0 = height();
    hC.setPointerCapture(e.pointerId);
    hC.classList.add("on");
    document.body.classList.add("dmap-dragging");
    const move = (ev) => {
      set(w0 - (ev.clientX - x0), h0 + (ev.clientY - y0), false);          // left: wider; down: taller
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; Panels.resizeMaps(); });
    };
    const end = (ev) => {
      hC.removeEventListener("pointermove", move);
      hC.removeEventListener("pointerup", end);
      hC.removeEventListener("pointercancel", end);
      try { hC.releasePointerCapture(ev.pointerId); } catch (_) { /* released */ }
      hC.classList.remove("on");
      document.body.classList.remove("dmap-dragging");
      set(width(), height(), true);
      Panels.resizeMaps();
    };
    hC.addEventListener("pointermove", move);
    hC.addEventListener("pointerup", end);
    hC.addEventListener("pointercancel", end);
  });

  // the window, the columns or the view changed: re-clamped (what was asked for is kept), the handles shown or not
  new ResizeObserver(() => apply()).observe(grid);
  addEventListener("resize", apply);
  NARROW.addEventListener("change", apply);
  if (typeof cad !== "undefined") cad.hooks.push(apply);       // the map shown or not ("No map")
  apply();
  return { ...api, apply, reset, state: st };
})();
