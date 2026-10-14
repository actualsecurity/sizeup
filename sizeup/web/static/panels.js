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
//
// Panels.separator(handle, opts) is the separator itself, for any divider (the Overview's map | cameras split,
// camwall.js, #147): {orient: "vertical" (a left | right divider) | "horizontal" (top / bottom), label, controls,
// min(), max(), value() -> px, set(px, done) (done: the drag or key ended: remember it), reset(), enabled(), step
// (16), grow (+1: dragging right / down grows the value, -1: shrinks it)}; orient and grow may be functions. It takes the pointer (captured, so a drag
// carries on over a map), the keys (the arrows of its orientation, Home), the double-click, keeps aria-valuenow /
// min / max, and resizes the maps once a frame while dragging and once after.
"use strict";

window.Panels = (() => {
  const STEP = 16;
  let frame = 0;
  function resizeMaps() {
    if (window.MapLayers && MapLayers.maps) for (const m of MapLayers.maps) { try { m.map.resize(); } catch (_) { /* gone */ } }
  }

  function separator(hd, o) {
    // the orientation and the direction may change with the layout (camwall.js): functions are asked each time
    const vert = () => ((typeof o.orient === "function" ? o.orient() : o.orient) || "vertical") === "vertical";
    const growBy = () => (typeof o.grow === "function" ? o.grow() : o.grow) || 1;
    const step = o.step || STEP;
    hd.setAttribute("role", "separator");
    if (o.label) hd.setAttribute("aria-label", o.label);
    if (o.controls) hd.setAttribute("aria-controls", o.controls);
    hd.tabIndex = 0;
    if (o.label && !hd.title) hd.title = `${o.label}: drag, or arrow keys; double-click to reset`;
    const sync = () => {
      hd.setAttribute("aria-orientation", vert() ? "vertical" : "horizontal");
      hd.setAttribute("aria-valuemin", String(Math.round(o.min())));
      hd.setAttribute("aria-valuemax", String(Math.round(o.max())));
      hd.setAttribute("aria-valuenow", String(Math.round(o.value())));
    };
    const reset = () => { o.reset(); sync(); resizeMaps(); };
    hd.addEventListener("dblclick", reset);
    hd.addEventListener("keydown", (e) => {
      if (e.key === "Home") { e.preventDefault(); reset(); return; }
      if (o.enabled && !o.enabled()) return;
      const dir = (vert() ? { ArrowLeft: -1, ArrowRight: 1 } : { ArrowUp: -1, ArrowDown: 1 })[e.key];
      if (!dir) return;
      e.preventDefault();
      o.set(o.value() + growBy() * dir * step, true);
      sync();
      resizeMaps();
    });
    hd.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || (o.enabled && !o.enabled())) return;
      e.preventDefault();
      const vertical = vert(), grow = growBy();
      const p0 = vertical ? e.clientX : e.clientY, v0 = o.value();
      hd.setPointerCapture(e.pointerId);
      hd.classList.add("on");
      document.body.classList.add(vertical ? "col-dragging" : "row-dragging");
      const move = (ev) => {
        o.set(v0 + grow * ((vertical ? ev.clientX : ev.clientY) - p0), false);
        sync();
        if (!frame) frame = requestAnimationFrame(() => { frame = 0; resizeMaps(); });
      };
      const end = (ev) => {
        hd.removeEventListener("pointermove", move);
        hd.removeEventListener("pointerup", end);
        hd.removeEventListener("pointercancel", end);
        try { hd.releasePointerCapture(ev.pointerId); } catch (_) { /* released */ }
        hd.classList.remove("on");
        document.body.classList.remove("col-dragging", "row-dragging");
        o.set(o.value(), true);
        sync();
        resizeMaps();
      };
      hd.addEventListener("pointermove", move);
      hd.addEventListener("pointerup", end);
      hd.addEventListener("pointercancel", end);
    });
    sync();
    return { sync, reset };
  }

  // ---- the side columns -----------------------------------------------------------------------------------------
  const main = document.querySelector("main.cad-main");
  if (!main) return { separator, resizeMaps };
  const KEY = "sizeup.panels";
  const SCREEN = BOARD ? "monitor" : "cad";
  const MID_MIN = 560, NARROW = matchMedia("(max-width: 1100px)");
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
    if (s.col && !s.col.id) s.col.id = `col-${side}`;
    main.append(hd);
    s.handle = hd;
    s.sep = separator(hd, {
      orient: "vertical", label: s.label, controls: s.col && s.col.id, grow: side === "left" ? 1 : -1,   // the right one grows leftwards
      min: () => s.min, max: () => s.max, value: () => current(side), enabled: () => !NARROW.matches,
      set: (px, done) => {
        widths[side] = clamp(side, px);
        main.style.setProperty(s.prop, `${widths[side]}px`);
        place();
        if (done) remember({ ...widths });
      },
      reset: () => {
        delete widths[side];
        main.style.removeProperty(s.prop);
        remember({ ...widths });
        place();
      },
    });
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

  // the window (or the board's zoom) changed: the widths re-clamped, the handles re-placed
  new ResizeObserver(() => apply()).observe(main);
  NARROW.addEventListener("change", apply);
  apply();
  return { widths, SIDES, separator, resizeMaps };
})();
