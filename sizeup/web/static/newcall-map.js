// The new call's map (GitHub #204): a big map on the right of the New incident form that plots the location while the
// call is being taken, before it is created.
//
// Julian (a dispatcher), relayed 2026-11-02: "personally, I like a big map. So when im talking to someone, i can look at
// the location it plots on while im typing in the call to verify where it is or do a better pick ... I can look around
// for other addresses or cross streets that may be better. So i like having a map on the right side thats big".
//
//   The pin        the chosen location (cad.js addressBox: a suggestion taken, or a pick here); the map eases to it,
//                  the zoom kept sensible (16.5 from far out, else as it is). Its block's cross streets: the block
//                  between them drawn along the street, each end labelled (GET /api/cad/geocode: block).
//   Candidates     while the suggestion list is open, every suggestion a small mark; the highlighted one bigger, and the
//                  map eases to it as ↑ / ↓ move.
//   A click        what is there (GET /api/cad/geocode/here): the nearest address point, intersection and named place,
//                  each with "Use this location" (it fills Location, "address @ place" for a place, and the cross
//                  streets); the street under it with "Add as cross street"; or "Use this point" (the pin there, the
//                  location as typed). A click on another layer's mark (a camera, a hydrant) is that layer's.
//   Dragging the pin  moves the point and keeps the address as typed; then asks: the nearest address point's address
//                  instead, or keep. The address never changes by itself.
//   Layers         the map's usual Layers box, the "incident" set (maplayers.js, #111): what a viewer sees on an event's
//                  map; the other open calls' pins and the rigs as on every map. Made once (CadMap.RigMap, manual: this
//                  file moves the camera) and reused for every new call.
//   Size           a splitter between the fields and the map (Panels.separator): drag, arrow keys, double-click resets;
//                  the fields' share remembered per viewer (localStorage "sizeup.newCallMap", in try/catch). The map
//                  takes ~62% by default. Too narrow for both (under STACK_PX): the map under the fields.
//   Focus          nothing here is in the form's Tab order (the map, its buttons and the splitter come after Create, the
//                  splitter tabindex -1); a click on the map keeps what was typed, and focus goes back to the field
//                  after a pick here.
"use strict";

window.NewCallMap = (() => {
  const KEY = "sizeup.newCallMap";
  const SHARE = 0.38, SHARE_MIN = 0.22, SHARE_MAX = 0.7;     // the fields' share of the form's width
  const FIELDS_MIN = 300, MAP_MIN = 320, STACK_PX = 760;
  const NEAR_ZOOM = 16.5, ZOOM_LO = 15, ZOOM_HI = 18.5, EASE_MS = 450;
  const st = { rm: null, pin: null, probe: null, here: null, chosen: null, cands: [], idx: -1, share: saved(),
               sep: null, lastField: null, cTimer: 0, seq: 0 };

  function saved() {
    try {
      const v = Number((JSON.parse(localStorage.getItem(KEY) || "{}") || {}).fields);
      return Number.isFinite(v) && v >= SHARE_MIN && v <= SHARE_MAX ? v : SHARE;
    } catch (_) { return SHARE; }
  }
  function remember() {
    try { localStorage.setItem(KEY, JSON.stringify({ fields: st.share })); } catch (_) { /* this page view only */ }
  }
  const mapsOk = () => !!(window.maplibregl && window.CadMap && cad.scene && cad.scene.home);
  const isOpen = () => cad.mode === "new" && !$("#new-form").hidden;
  const fc = (features) => ({ type: "FeatureCollection", features });
  const pt = (lon, lat, props = {}) => ({ type: "Feature", properties: props, geometry: { type: "Point", coordinates: [lon, lat] } });

  // ---- the layout: fields | splitter | map ------------------------------------------------------------------------
  const body = () => $("#nc-body");
  function stacked() { return body().clientWidth < STACK_PX; }
  function layout() {
    const b = body();
    const stack = stacked();
    b.classList.toggle("nc-stacked", stack);
    show($("#nc-split"), !stack && mapsOk());
    if (stack) b.style.removeProperty("--nc-fields");
    else b.style.setProperty("--nc-fields", `${fieldsPx(st.share * b.clientWidth)}px`);
    if (st.sep) st.sep.sync();
    if (st.rm) st.rm.map.resize();
  }
  function fieldsPx(px) {
    const w = body().clientWidth;
    return Math.round(Math.max(FIELDS_MIN, Math.min(px, w - MAP_MIN, w * SHARE_MAX)));
  }
  function splitter() {
    if (st.sep || !window.Panels || !Panels.separator) return;
    const hd = $("#nc-split");
    st.sep = Panels.separator(hd, {
      orient: "vertical", label: "Resize the new call's map", controls: "nc-map-pane", grow: 1,
      min: () => FIELDS_MIN, max: () => Math.max(FIELDS_MIN, body().clientWidth - MAP_MIN),
      value: () => $("#nc-fields").getBoundingClientRect().width, enabled: () => !stacked(),
      set: (px, done) => {
        const w = body().clientWidth || 1;
        st.share = Math.max(SHARE_MIN, Math.min(SHARE_MAX, fieldsPx(px) / w));
        body().style.setProperty("--nc-fields", `${fieldsPx(px)}px`);
        if (done) remember();
      },
      reset: () => { st.share = SHARE; remember(); layout(); },
    });
    hd.tabIndex = -1;                  // not in the call's Tab order (#204); a click on it still focuses it for the arrows
    new ResizeObserver(() => { if (isOpen()) layout(); }).observe(body());
  }

  // ---- the map -----------------------------------------------------------------------------------------------------
  function make() {
    const host = $("#nc-map");
    st.rm = new CadMap.RigMap(host, { scene: cad.scene, pins: true, unset: false, editable: () => false, operator,
                                     onPick: () => {}, layers: "incident", manual: true, onRecenter: recenter });
    const map = st.rm.map;
    const init = () => {
      if (map.getSource("nc-cands")) return;
      const glyphs = !!(map.getStyle() && map.getStyle().glyphs);
      map.addSource("nc-block", { type: "geojson", data: fc([]) });
      map.addSource("nc-cands", { type: "geojson", data: fc([]) });
      map.addLayer({ id: "nc-block-line", type: "line", source: "nc-block", filter: ["==", ["geometry-type"], "LineString"],
                     layout: { "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": "#E0A030", "line-width": 7, "line-opacity": 0.55 } });
      map.addLayer({ id: "nc-block-ends", type: "circle", source: "nc-block", filter: ["==", ["geometry-type"], "Point"],
                     paint: { "circle-radius": 5, "circle-color": "#15181B", "circle-stroke-color": "#E0A030", "circle-stroke-width": 2.5 } });
      if (glyphs) {
        map.addLayer({ id: "nc-block-names", type: "symbol", source: "nc-block", filter: ["==", ["geometry-type"], "Point"],
                       layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Bold"], "text-size": 12, "text-anchor": "left",
                                 "text-offset": [0.8, 0], "text-optional": true, "text-allow-overlap": true },
                       paint: { "text-color": "#F2C46B", "text-halo-color": "#15181B", "text-halo-width": 1.8 } });
      }
      map.addLayer({ id: "nc-cands", type: "circle", source: "nc-cands",
                     paint: { "circle-radius": ["case", ["get", "on"], 8, 5],
                              "circle-color": ["case", ["get", "on"], "#C8102E", "#9AA3AD"],
                              "circle-opacity": ["case", ["get", "on"], 0.95, 0.75],
                              "circle-stroke-color": "#FFFFFF", "circle-stroke-width": ["case", ["get", "on"], 2, 1] } });
      draw();
    };
    if (map.isStyleLoaded()) init(); else map.once("load", init);
    map.on("click", clicked);
    $("#nc-fields").addEventListener("focusin", (e) => { st.lastField = e.target; });
  }
  function src(id) { return st.rm && st.rm.map.getSource(id); }

  function draw() {                    // the candidates and the chosen one's block
    const c = src("nc-cands"), b = src("nc-block");
    if (!c || !b) return;
    c.setData(fc(st.cands.filter((m) => m.lat != null && m !== st.chosen).map((m, i) =>
      pt(m.lon, m.lat, { on: st.cands.indexOf(m) === st.idx, label: m.label, i }))));
    const blk = st.chosen && st.chosen.block;
    const feats = [];
    if (blk && blk.path && blk.path.length > 1) feats.push({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: blk.path } });
    for (const e of (blk && blk.ends) || []) feats.push(pt(e.lon, e.lat, { name: e.name }));
    b.setData(fc(feats));
  }

  function ease(lon, lat, bounds) {
    const map = st.rm.map;
    if (bounds && bounds.length > 1) {
      const bb = new maplibregl.LngLatBounds(bounds[0], bounds[0]);
      bounds.forEach((p) => bb.extend(p));
      const w = map.getContainer().clientWidth, h = map.getContainer().clientHeight;
      const pad = Math.max(30, Math.min(110, Math.floor(Math.min(w, h) / 5)));
      map.fitBounds(bb, { padding: pad, maxZoom: Math.max(17, Math.min(ZOOM_HI, map.getZoom())), duration: EASE_MS });
      return;
    }
    const z = map.getZoom();
    map.easeTo({ center: [lon, lat], zoom: z < ZOOM_LO ? NEAR_ZOOM : Math.min(ZOOM_HI, z), duration: EASE_MS });
  }
  function recenter() {
    const m = st.chosen;
    if (m && m.lat != null) ease(m.lon, m.lat, m.block && m.block.path ? [...m.block.path, [m.lon, m.lat]] : null);
    else st.rm.map.easeTo({ center: cad.scene.home.center, zoom: cad.scene.home.zoom, duration: EASE_MS });
  }

  function setPin(m) {
    if (st.pin) { st.pin.remove(); st.pin = null; }
    if (!m || m.lat == null) return;
    const dot = el("div", "nc-pin");
    dot.title = "The new call's location: drag to move the point";
    st.pin = new maplibregl.Marker({ element: dot, anchor: "bottom", draggable: true }).setLngLat([m.lon, m.lat]).addTo(st.rm.map);
    st.pin.on("dragend", () => { const ll = st.pin.getLngLat(); dragged(ll.lat, ll.lng); });
  }

  // ---- what cad.js tells it ----------------------------------------------------------------------------------------
  function candidates(items, idx) {
    if (!st.rm) return;
    st.cands = (items || []).filter((m) => m && m.lat != null);
    st.idx = st.cands.indexOf((items || [])[idx]);
    draw();
    clearTimeout(st.cTimer);
    const m = st.cands[st.idx];
    if (m) st.cTimer = setTimeout(() => ease(m.lon, m.lat), 120);     // ↑ / ↓ held down: one move, not one per key
  }
  function chosen(m, how) {
    if (!st.rm) return;
    st.chosen = m && m.lat != null ? m : null;
    st.cands = [];
    st.idx = -1;
    setPin(st.chosen);
    draw();
    if (st.chosen && how !== "map" && how !== "drag") {
      const b = st.chosen.block && st.chosen.block.path;
      ease(st.chosen.lon, st.chosen.lat, b ? [...b, [st.chosen.lon, st.chosen.lat]] : null);
    }
  }

  // ---- a click: what is here ---------------------------------------------------------------------------------------
  function closeHere() {
    if (st.here) st.here.remove();
    if (st.probe) st.probe.remove();
    st.here = st.probe = null;
  }
  function backToField() {
    const f = st.lastField && document.contains(st.lastField) ? st.lastField : $("#f-address");
    f.focus({ preventScroll: true });
  }
  function hereBox(title, sub) {
    if (st.here) st.here.remove();
    const box = el("div", "cm-pop nc-here");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", title);
    const head = el("div", "row");
    head.append(el("b", "", title), el("span", "spacer"));
    const x = el("button", "mini quiet", "Close");
    x.type = "button";
    x.addEventListener("click", (e) => { e.stopPropagation(); closeHere(); });
    head.append(x);
    box.append(head);
    if (sub) box.append(el("div", "dim small", sub));
    box.addEventListener("click", (e) => e.stopPropagation());
    box.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closeHere(); backToField(); } });
    $("#nc-map").append(box);
    st.here = box;
    return box;
  }
  function choice(box, m, what, btnText, fn) {
    const row = el("div", "nc-choice");
    const t = el("div", "nc-what");
    t.append(el("span", "dim small", what), el("b", "", m ? locationText(m) || m.label : ""));
    if (m && m.town) t.append(el("span", "town", m.town));
    if (m && m.cross) t.append(el("span", "dim small", `Cross: ${m.cross}`));
    const b = el("button", "mini", btnText);
    b.type = "button";
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    row.append(t, b);
    box.append(row);
    return b;
  }
  function use(m) {
    newAddr.take(m, "map");
    closeHere();
    backToField();
  }
  function addCross(name) {
    const c = $("#f-cross"), v = c.value.trim();
    const have = v.split(/\s+and\s+|\s*\/\s*|,\s*/i).map((x) => x.trim().toLowerCase());
    if (!have.includes(name.toLowerCase())) c.value = v ? `${v} and ${name}` : name;
    newAddr.autoCross = "";            // the dispatcher's own now: a later pick leaves it
    closeHere();
    backToField();
  }
  async function lookHere(lat, lon) {
    const my = ++st.seq;
    const r = await api("GET", `/api/cad/geocode/here?lat=${lat.toFixed(6)}&lon=${lon.toFixed(6)}`);
    return my === st.seq ? r : null;
  }
  async function clicked(e) {
    if (!isOpen() || !st.rm) return;
    const map = st.rm.map;
    // another layer's mark (a camera, a hydrant, a firehouse) has its own box
    const marks = map.queryRenderedFeatures(e.point).filter((f) => f.source !== "base" && !String(f.layer.id).startsWith("nc-")
      && (f.layer.type === "symbol" || f.layer.type === "circle"));
    if (marks.length) return;
    const { lat, lng } = e.lngLat;
    closeHere();
    const probe = el("div", "nc-probe");
    st.probe = new maplibregl.Marker({ element: probe, anchor: "center" }).setLngLat([lng, lat]).addTo(map);
    let r;
    try { r = await lookHere(lat, lng); } catch (err) { r = { error: err.message }; }
    if (!r || !isOpen()) return;
    const box = hereBox("Here", r.error || "");
    let any = false;
    for (const [k, what] of [["address", "Address point"], ["place", "Place"], ["intersection", "Intersection"]]) {
      const m = r[k];
      if (!m) continue;
      any = true;
      choice(box, m, what, "Use this location", () => use(m));
    }
    if (r.street && r.street.name) {
      any = true;
      choice(box, { label: r.street.name }, "Street", "Add as cross street", () => addCross(r.street.name));
    }
    if (!any) box.append(el("div", "dim small", "No address point, intersection or place mapped right here."));
    const keep = el("button", "mini quiet", "Use this point");
    keep.type = "button";
    keep.title = "Put the pin here; the location stays as typed";
    keep.addEventListener("click", (ev) => {
      ev.stopPropagation();
      newAddr.move(lat, lng);
      st.chosen = { ...(newAddr.picked || {}), block: null };
      setPin(st.chosen);
      draw();
      closeHere();
      backToField();
    });
    const foot = el("div", "row");
    foot.append(keep);
    box.append(foot);
  }

  // ---- the pin dragged: the point moves; the address only if asked ------------------------------------------------
  async function dragged(lat, lon) {
    const typed = $("#f-address").value.trim();
    newAddr.move(lat, lon);
    st.chosen = { ...(newAddr.picked || {}), block: null };
    draw();
    closeHere();
    let r = null;
    try { r = await lookHere(lat, lon); } catch (_) { r = null; }
    if (!isOpen()) return;
    const box = hereBox("Pin moved", typed ? `The location still says “${typed}”, at the moved point.` : "");
    const a = r && r.address;
    if (a && locationText(a) !== typed) choice(box, a, "Nearest address point", "Use this address", () => use(a));
    const keep = el("button", "mini", typed ? "Keep the address, use the moved point" : "Keep the moved point");
    keep.type = "button";
    keep.addEventListener("click", (e) => { e.stopPropagation(); closeHere(); backToField(); });
    const foot = el("div", "row");
    foot.append(keep);
    box.append(foot);
  }

  // ---- open / follow the state ------------------------------------------------------------------------------------
  function open() {
    const ok = mapsOk();
    show($("#nc-map"), ok);
    show($("#nc-nomap"), !ok);
    setText($("#nc-nomap"), ok ? "" : "No map");
    $("#nc-body").classList.toggle("nc-nomap", !ok);
    splitter();
    layout();
    if (!ok) return;
    if (!st.rm) make();
    closeHere();
    st.chosen = null;
    st.cands = [];
    st.idx = -1;
    setPin(null);
    draw();
    st.rm.size = "";                   // shown again: resize, then the whole area until a location comes
    st.rm.update({ map: cad.state && cad.state.map, lists: cad.state && cad.state.lists }, null, "");
    st.rm.map.jumpTo({ center: cad.scene.home.center, zoom: cad.scene.home.zoom });
  }
  function sync() {                    // every render: the other calls' pins and the rigs, as on every map
    if (!st.rm || !isOpen()) { if (st.here && !isOpen()) closeHere(); return; }
    st.rm.update({ map: cad.state && cad.state.map, lists: cad.state && cad.state.lists }, null, "");
  }
  if (typeof cad !== "undefined" && document.getElementById("nc-map")) cad.hooks.push(sync);

  return { open, sync, candidates, chosen, layout, KEY };
})();
