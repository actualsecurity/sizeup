// Resizable side columns on /cad (GitHub #126): a drag handle on the inner edge of the left column (the queue)
// and the right one (the radio feed and the units), the same grid for the Overview and Dispatch views; on the
// wall board (/monitor, the same grid without the queue) the right one only.
//
// User, 2026-10-14: "can we set it so we can drag the left and right side panels smaller/larger (to a
// reasonable limit)".
//
//   Limits     left 220-480 px, right 280-560 px, and the middle never under 560 px (a side gives way first).
//   Remembered per screen and viewer: localStorage "sizeup.panels" {cad: {left, right}, monitor: {right}} (in
//              try/catch: storage may be missing or refuse); a double-click on a handle, or Home, puts that side
//              back to the layout's own width (style.css --rail-w / --side-w).
//   Keyboard   each handle is a focusable separator (role="separator", aria-valuenow / min / max); the arrow keys
//              move it 16 px, Home resets.
//   The maps   follow during a drag (MapLibre's resize() once a frame) and once more after it.
//   Narrow     under 1101 px the columns stack (cad.css): the handles are hidden and the widths not applied.
"use strict";

window.Panels = (() => {
  const main = document.querySelector("main.cad-main");
  if (!main) return null;
  const KEY = "sizeup.panels";
  const SCREEN = BOARD ? "monitor" : "cad";
  const STEP = 16, MID_MIN = 560, NARROW = matchMedia("(max-width: 1100px)");
  const SIDES = {
    left: { col: main.querySelector(":scope > .queue"), min: 220, max: 480, prop: "--rail-w", label: "Resize the queue column" },
    right: { col: main.querySelector(":scope > .units"), min: 280, max: 560, prop: "--side-w", label: "Resize the radio and units column" },
  };
  if (BOARD) delete SIDES.left;                       // the board has no queue column

  function saved() { try { return (JSON.parse(localStorage.getItem(KEY) || "{}") || {})[SCREEN] || {}; } catch (_) { return {}; } }
  function remember(widths) {
    try {
      const all = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
      all[SCREEN] = widths;
      localStorage.setItem(KEY, JSON.stringify(all));
    } catch (_) { /* this page view only */ }
  }
  const widths = {};
  for (const [k, v] of Object.entries(saved())) if (SIDES[k] && Number.isFinite(v)) widths[k] = v;

  // The width a side may have now: its own limits, and the middle kept at MID_MIN (the side gives way first).
  function clamp(side, px) {
    const s = SIDES[side];
    const other = Object.keys(SIDES).filter((k) => k !== side).reduce((n, k) => n + SIDES[k].col.getBoundingClientRect().width, 0);
    const room = main.clientWidth - other - MID_MIN;
    return Math.round(Math.max(s.min, Math.min(s.max, px, room)));
  }
  function apply() {
    for (const [side, s] of Object.entries(SIDES)) {
      if (widths[side] != null && !NARROW.matches) main.style.setProperty(s.prop, `${clamp(side, widths[side])}px`);
      else main.style.removeProperty(s.prop);
    }
    place();
  }
  function current(side) { return Math.round(SIDES[side].col.getBoundingClientRect().width); }

  // ---- the handles: absolutely placed over the column edge (the grid's own children, out of its flow) ----------
  for (const [side, s] of Object.entries(SIDES)) {
    const hd = document.createElement("div");
    hd.className = `col-handle col-handle-${side}`;
    hd.setAttribute("role", "separator");
    hd.setAttribute("aria-orientation", "vertical");
    hd.setAttribute("aria-label", s.label);
    hd.setAttribute("aria-valuemin", String(s.min));
    hd.setAttribute("aria-valuemax", String(s.max));
    hd.tabIndex = 0;
    hd.title = `${s.label}: drag, or arrow keys; double-click to reset`;
    if (s.col && !s.col.id) s.col.id = `col-${side}`;
    if (s.col) hd.setAttribute("aria-controls", s.col.id);
    main.append(hd);
    s.handle = hd;
    hd.addEventListener("pointerdown", (e) => drag(side, e));
    hd.addEventListener("dblclick", () => reset(side));
    hd.addEventListener("keydown", (e) => key(side, e));
  }
  function place() {
    const m = main.getBoundingClientRect();
    for (const [side, s] of Object.entries(SIDES)) {
      const r = s.col.getBoundingClientRect();
      const edge = side === "left" ? r.right - m.left : r.left - m.left;           // the column's inner edge
      s.handle.style.insetInlineStart = `${Math.round(edge)}px`;
      s.handle.setAttribute("aria-valuenow", String(current(side)));
    }
  }

  function set(side, px, keep = true) {
    widths[side] = clamp(side, px);
    main.style.setProperty(SIDES[side].prop, `${widths[side]}px`);
    place();
    if (keep) remember({ ...widths });
  }
  function reset(side) {
    delete widths[side];
    main.style.removeProperty(SIDES[side].prop);
    remember({ ...widths });
    place();
    resizeMaps();
  }
  function key(side, e) {
    const dir = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
    if (e.key === "Home") { e.preventDefault(); reset(side); return; }
    if (!dir) return;
    e.preventDefault();
    set(side, current(side) + (side === "left" ? dir : -dir) * STEP);       // the right column grows leftwards
    resizeMaps();
  }

  // ---- dragging: pointer capture, so the drag carries on over the map ---------------------------------------------
  let frame = 0;
  function resizeMaps() {
    if (window.MapLayers && MapLayers.maps) for (const m of MapLayers.maps) { try { m.map.resize(); } catch (_) { /* gone */ } }
  }
  function drag(side, e) {
    if (e.button !== 0 || NARROW.matches) return;
    e.preventDefault();
    const hd = SIDES[side].handle;
    const x0 = e.clientX, w0 = current(side);
    hd.setPointerCapture(e.pointerId);
    hd.classList.add("on");
    document.body.classList.add("col-dragging");
    const move = (ev) => {
      const dx = ev.clientX - x0;
      set(side, w0 + (side === "left" ? dx : -dx), false);
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; resizeMaps(); });
    };
    const end = (ev) => {
      hd.removeEventListener("pointermove", move);
      hd.removeEventListener("pointerup", end);
      hd.removeEventListener("pointercancel", end);
      try { hd.releasePointerCapture(ev.pointerId); } catch (_) { /* released */ }
      hd.classList.remove("on");
      document.body.classList.remove("col-dragging");
      remember({ ...widths });
      resizeMaps();
    };
    hd.addEventListener("pointermove", move);
    hd.addEventListener("pointerup", end);
    hd.addEventListener("pointercancel", end);
  }

  // the window (or the board's zoom) changed: the widths re-clamped, the handles re-placed
  new ResizeObserver(() => apply()).observe(main);
  NARROW.addEventListener("change", apply);
  apply();
  return { widths, set, reset, SIDES };
})();
