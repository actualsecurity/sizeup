// Pre-plan points on the CAD maps (#456, sizeup/cad/preplan.py): where a building's Knox box, FDC, standpipe, valves,
// shutoffs, elevator, roof and gate access, hazmat and solar disconnect are, exactly, on the map of every call there.
//
//   The call     an open call's points come in the state push (inc.preplan: its building's, and when the call or the
//                point has no building key, those within 20 m: "nearby"); a kind an admin kept to some roles (the
//                state's preplan.restricted) never does: a dispatcher's page asks GET /api/cad/preplan?incident=<id>
//                for its own, again only when the write generation (state preplan.gen) moves. A closed call's: asked.
//                Drawn on the call's map (and the large map) with an icon per kind and the label; the list beside the
//                map (#d-preplan): "Knox box · Front door left", a click centres the map on it.
//   The layer    "Pre-plan" in every map's Layers box (maplayers.js MapLayers.addGroup): off until a viewer turns it on
//                (theirs, every map: localStorage PREF_KEY, in a try), every point in the viewport from LAYER_ZOOM, by
//                bbox (GET /api/cad/preplan?bbox=, padded; ETag: the browser revalidates), again only when the map
//                leaves the box it asked for or a write moves the generation. Nothing per frame: map events and pushes.
//   The order    its own source and layers, under the route lines, the rigs' lines and the firehouses (BELOW: the
//                rigs and the call pins are DOM marks, over every map layer), over the county layers.
//   The marks    an original drawing per kind on a light tile with a dark rim (DRAW): not a status colour; 36 px at
//                pixel ratio 2, 18 px on screen. A nearby point is fainter.
//   Editing      dispatchers and admins: "+ Point" by the list (or the Search page's Pre-plan tab: one building's
//                points on its own map): pick a kind, tap the map to place, drag to adjust; a label, a note, a level,
//                a priority, a review-by date. The building it belongs to comes from the hub (GET
//                /api/cad/preplan/where: the county footprint it is in or nearest within 30 m, or the call's), shown
//                with its footprint outlined, and can be picked ("This call", "Map point only"). Edit, move, delete
//                from the row's menu. Every write is in the Settings audit log on the hub.
// Uses cad.js's helpers (el, $, show, setText, api, operator, canDo, UI, cad): loaded after it, and after maplayers.js.
"use strict";

window.Preplan = (() => {
  // sizeup/cad/preplan.py KINDS, in its order
  const KINDS = { knox_box: "Knox box", fdc: "FDC", standpipe_riser: "Standpipe riser", sprinkler_valve: "Sprinkler valve",
                  alarm_panel: "Alarm panel", gas_shutoff: "Gas shutoff", electric_shutoff: "Electric shutoff",
                  water_shutoff: "Water shutoff", elevator: "Elevator", roof_access: "Roof access", gate_access: "Gate access",
                  hazmat_storage: "Hazmat storage", solar_disconnect: "Solar disconnect", other: "Other" };
  const PRIO = { 1: "High", 2: "Normal", 3: "Low" };
  const LINK = { inside: "in footprint", nearest: "nearest footprint", call: "call's building", point: "map point only" };
  const PREF_KEY = "sizeup.preplanLayer";
  const LAYER_ZOOM = 16;
  const SRC = "pp-points", EDIT_SRC = "pp-edit";
  const BELOW = ["cm-lines", "cm-trail", "cm-routes", "cm-route-lines", "firehouse"];
  const PAD = 0.3, MOVE_MS = 250;

  // ---- pure helpers (tests/test_preplan.py runs these in node) ------------------------------------------------------
  // "Knox box · Front door left · Fl 2" (a level that is a number is a floor; "B1", "Roof" as they are)
  const levelOf = (v) => (!v ? "" : /^\d+$/.test(v) ? `Fl ${v}` : v);
  const line = (p) => [KINDS[p.kind] || p.kind, p.label, levelOf(p.level)].filter(Boolean).join(" · ");
  // the layer to go under: the lowest of BELOW the map has (ids: the style's order, bottom first)
  const beforeId = (ids) => ids.find((id) => BELOW.includes(id)) || null;
  // the call's points, then the viewport's not already drawn: one feature each
  function features(own, box) {
    const seen = new Set(), out = [];
    for (const [p, mine] of [...own.map((p) => [p, true]), ...box.map((p) => [p, false])]) {
      if (seen.has(p.id)) continue;
      seen.add(p.id);
      out.push({ type: "Feature", geometry: { type: "Point", coordinates: [p.lon, p.lat] },
                 properties: { id: p.id, kind: p.kind, icon: `pp-${p.kind}`, text: p.label || KINDS[p.kind] || "",
                               nearby: !!p.nearby, own: mine, priority: p.priority || 2 } });
    }
    return { type: "FeatureCollection", features: out };
  }
  function padBox(b) {
    const dx = (b[2] - b[0]) * PAD, dy = (b[3] - b[1]) * PAD;
    return [b[0] - dx, b[1] - dy, b[2] + dx, b[3] + dy].map((x) => Math.round(x * 1e5) / 1e5);
  }
  const inside = (view, box) => !!box && view[0] >= box[0] && view[1] >= box[1] && view[2] <= box[2] && view[3] <= box[3];

  // ---- the marks: an original drawing per kind on a light tile (36 x 36, drawn at pixel ratio 2) ----------------------
  const INK = "#15181B", TILE = "#EEF1F4", RIM = "rgba(12, 14, 16, 0.94)";
  const DRAW = {
    knox_box: (g) => {                                   // a key
      g.lineWidth = 2.6; g.beginPath(); g.arc(12.5, 18, 4.5, 0, 2 * Math.PI); g.stroke();
      g.beginPath(); g.moveTo(17, 18); g.lineTo(28, 18); g.moveTo(24.5, 18); g.lineTo(24.5, 22.5); g.moveTo(28, 18); g.lineTo(28, 21.5); g.stroke();
    },
    fdc: (g) => {                                        // the Siamese: two inlets into one
      g.lineWidth = 2.6; g.beginPath(); g.moveTo(12, 12); g.lineTo(18, 20); g.lineTo(24, 12); g.moveTo(18, 20); g.lineTo(18, 29); g.stroke();
      for (const x of [12, 24]) { g.beginPath(); g.arc(x, 11, 3.6, 0, 2 * Math.PI); g.fill(); }
    },
    standpipe_riser: (g) => {                            // a riser with an outlet each side
      g.fillRect(16, 7, 4, 23);
      g.fillRect(20, 12, 6, 3); g.beginPath(); g.arc(27, 13.5, 2.6, 0, 2 * Math.PI); g.fill();
      g.fillRect(10, 21, 6, 3); g.beginPath(); g.arc(9, 22.5, 2.6, 0, 2 * Math.PI); g.fill();
    },
    sprinkler_valve: (g) => {                            // a valve: the bow tie, its stem and wheel
      g.beginPath(); g.moveTo(8, 14); g.lineTo(8, 28); g.lineTo(18, 21); g.closePath(); g.fill();
      g.beginPath(); g.moveTo(28, 14); g.lineTo(28, 28); g.lineTo(18, 21); g.closePath(); g.fill();
      g.lineWidth = 2.6; g.beginPath(); g.moveTo(18, 21); g.lineTo(18, 10); g.moveTo(12, 9); g.lineTo(24, 9); g.stroke();
    },
    alarm_panel: (g) => {                                // a bell
      g.beginPath(); g.moveTo(10, 25); g.quadraticCurveTo(11.5, 9, 18, 8.5); g.quadraticCurveTo(24.5, 9, 26, 25); g.closePath(); g.fill();
      g.fillRect(8, 24, 20, 2.6); g.beginPath(); g.arc(18, 28.5, 2.6, 0, 2 * Math.PI); g.fill();
    },
    gas_shutoff: (g) => {                                // a flame
      g.beginPath(); g.moveTo(18, 6); g.bezierCurveTo(29, 15, 27, 29, 18, 30); g.bezierCurveTo(9, 29, 8, 20, 14, 14);
      g.bezierCurveTo(14, 19, 16, 21, 18, 21); g.bezierCurveTo(19, 16, 16, 11, 18, 6); g.fill();
    },
    electric_shutoff: (g) => {                           // a bolt
      g.beginPath(); g.moveTo(21, 5); g.lineTo(10, 20); g.lineTo(17, 20); g.lineTo(14, 31); g.lineTo(26, 15); g.lineTo(19, 15);
      g.lineTo(22, 5); g.closePath(); g.fill();
    },
    water_shutoff: (g) => {                              // a drop
      g.beginPath(); g.moveTo(18, 6); g.quadraticCurveTo(26, 16, 26, 22); g.arc(18, 22, 8, 0, Math.PI); g.quadraticCurveTo(10, 16, 18, 6); g.fill();
    },
    elevator: (g) => {                                   // a car, up and down
      g.lineWidth = 2.2; g.strokeRect(10, 7, 16, 22);
      g.beginPath(); g.moveTo(18, 10); g.lineTo(13.5, 16); g.lineTo(22.5, 16); g.closePath(); g.fill();
      g.beginPath(); g.moveTo(13.5, 20); g.lineTo(22.5, 20); g.lineTo(18, 26); g.closePath(); g.fill();
    },
    roof_access: (g) => {                                // a ladder
      g.lineWidth = 2.6; g.beginPath(); g.moveTo(12, 6); g.lineTo(12, 30); g.moveTo(24, 6); g.lineTo(24, 30);
      for (const y of [10, 16, 22, 28]) { g.moveTo(12, y); g.lineTo(24, y); }
      g.stroke();
    },
    gate_access: (g) => {                                // a gate: posts, rails and a brace
      g.fillRect(7, 9, 3.4, 21); g.fillRect(25.6, 9, 3.4, 21);
      g.lineWidth = 2.4; g.beginPath(); g.moveTo(10, 13); g.lineTo(26, 13); g.moveTo(10, 25); g.lineTo(26, 25); g.moveTo(10, 25); g.lineTo(26, 13); g.stroke();
      for (const x of [8.7, 27.3]) { g.beginPath(); g.arc(x, 8, 2.4, 0, 2 * Math.PI); g.fill(); }
    },
    hazmat_storage: (g) => {                             // the 704 diamond
      g.lineWidth = 2.4; g.beginPath(); g.moveTo(18, 5); g.lineTo(31, 18); g.lineTo(18, 31); g.lineTo(5, 18); g.closePath(); g.stroke();
      g.lineWidth = 1.6; g.beginPath(); g.moveTo(11.5, 11.5); g.lineTo(24.5, 24.5); g.moveTo(24.5, 11.5); g.lineTo(11.5, 24.5); g.stroke();
    },
    solar_disconnect: (g) => {                           // the sun
      g.beginPath(); g.arc(18, 18, 5, 0, 2 * Math.PI); g.fill();
      g.lineWidth = 2.2; g.beginPath();
      for (let i = 0; i < 8; i++) { const a = i * Math.PI / 4; g.moveTo(18 + 8 * Math.cos(a), 18 + 8 * Math.sin(a)); g.lineTo(18 + 12 * Math.cos(a), 18 + 12 * Math.sin(a)); }
      g.stroke();
    },
    other: (g) => {                                      // a ringed dot
      g.lineWidth = 2.4; g.beginPath(); g.arc(18, 18, 8, 0, 2 * Math.PI); g.stroke();
      g.beginPath(); g.arc(18, 18, 3, 0, 2 * Math.PI); g.fill();
    },
  };
  const art = {};                                        // kind -> its canvas (made once)
  function drawn(kind) {
    if (art[kind]) return art[kind];
    const c = document.createElement("canvas");
    c.width = c.height = 36;
    const g = c.getContext("2d");
    g.fillStyle = TILE; g.strokeStyle = RIM; g.lineWidth = 2;
    g.beginPath(); g.roundRect(2, 2, 32, 32, 6); g.fill(); g.stroke();
    g.fillStyle = INK; g.strokeStyle = INK; g.lineCap = "round"; g.lineJoin = "round";
    (DRAW[kind] || DRAW.other)(g);
    art[kind] = c;
    return c;
  }
  function iconImg(kind, cls = "pp-ico") {               // the same mark in the page (the list, the kind picker)
    const i = el("img", cls);
    i.alt = "";
    i.src = drawn(kind).toDataURL();
    i.width = i.height = 18;
    return i;
  }

  // ---- the call's points ----------------------------------------------------------------------------------------------
  const st = { inc: null, readonly: true, fetched: null, fetching: "", edit: null, layerOn: prefOn(), search: null };
  function prefOn() {
    try { return localStorage.getItem(PREF_KEY) === "1"; } catch (_) { return false; }
  }
  function setPref(on) {
    st.layerOn = !!on;
    try { localStorage.setItem(PREF_KEY, on ? "1" : "0"); } catch (_) { /* this page view only */ }
    for (const m of maps) { m.box = null; m.refresh(); }
  }
  const pstate = () => (window.cad && cad.state && cad.state.preplan) || { gen: 0, restricted: [] };
  const mayMore = () => { const w = window.cad && cad.who; return !w || !w.enforced || w.may !== "viewer"; };
  // the push's copy, or this person's own (a restricted kind, a closed call): asked once per call, place and generation
  function pointsOf(inc) {
    if (!inc) return [];
    const restricted = pstate().restricted || [];
    const need = !Array.isArray(inc.preplan) || (restricted.length && mayMore());
    if (!need) return inc.preplan;
    const key = JSON.stringify([inc.id, pstate().gen, inc.bldg_key || "", inc.lat, inc.lon]);
    if (st.fetched && st.fetched.key === key) return st.fetched.points;
    if (st.fetching !== key) {
      st.fetching = key;
      api("GET", `/api/cad/preplan?incident=${encodeURIComponent(inc.id)}`)
        .then((d) => { st.fetched = { key, points: d.points || [] }; redraw(); })
        .catch(() => { st.fetched = { key, points: Array.isArray(inc.preplan) ? inc.preplan : [] }; redraw(); });
    }
    return Array.isArray(inc.preplan) ? inc.preplan : (st.fetched && st.fetched.key.startsWith(`[${inc.id},`) ? st.fetched.points : []);
  }

  // ---- one map's points: the call's and the layer's -------------------------------------------------------------------
  const maps = new Set();
  class PPMap {
    // control: maplayers.js's box for a console map, or {map, profile: "search"} for the Search page's building map
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.box = null;
      this.boxPts = [];
      this.fixed = null;                                 // the Search page's building: its points
      this.seq = 0;
      this.sig = "";
      this.timer = null;
      this.map.on("moveend", () => { clearTimeout(this.timer); this.timer = setTimeout(() => this.viewport(), MOVE_MS); });
      maps.add(this);
      this.refresh();
    }
    ready() { try { return this.styled || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }
    isCall() {
      return this.control.profile === "incident" || !!(this.map.getContainer().closest && this.map.getContainer().closest(".cm-big"));
    }
    own() {
      if (this.fixed) return this.fixed;
      return this.isCall() ? pointsOf(this.control.focused) : [];
    }
    ensure() {
      const map = this.map;
      for (const k of Object.keys(KINDS)) {
        if (!map.hasImage(`pp-${k}`)) { const c = drawn(k); map.addImage(`pp-${k}`, c.getContext("2d").getImageData(0, 0, 36, 36), { pixelRatio: 2 }); }
      }
      const ids = () => (map.getLayersOrder ? map.getLayersOrder() : (map.getStyle().layers || []).map((l) => l.id));
      if (!map.getSource(SRC)) {
        const glyphs = !!(map.getStyle() && map.getStyle().glyphs);
        map.addSource(SRC, { type: "geojson", data: features([], []) });
        map.addSource(EDIT_SRC, { type: "geojson", data: features([], []) });
        const specs = [
          { id: "pp-edit-fp", type: "line", source: EDIT_SRC, paint: { "line-color": "#F2F3F5", "line-width": 2.5, "line-dasharray": [2, 1.5] } },
          { id: "pp-icons", type: "symbol", source: SRC,
            layout: { "icon-image": ["get", "icon"], "icon-allow-overlap": true, "icon-ignore-placement": true,
                      "icon-size": ["case", ["==", ["get", "priority"], 1], 1.15, 1],
                      "symbol-sort-key": ["case", ["get", "own"], 0, 1] },
            paint: { "icon-opacity": ["case", ["get", "nearby"], 0.7, 1] } },
          ...(glyphs ? [{ id: "pp-labels", type: "symbol", source: SRC, minzoom: 15,
            layout: { "text-field": ["get", "text"], "text-font": ["Noto Sans Regular"], "text-size": 11, "text-anchor": "left",
                      "text-offset": [1.1, 0], "text-optional": true, "text-max-width": 10 },
            paint: { "text-color": "#E6E9EC", "text-halo-color": RIM, "text-halo-width": 1.4 } }] : []),
        ];
        for (const spec of specs) this.map.addLayer(spec, beforeId(ids()));
        map.on("click", "pp-icons", (e) => this.clicked(e));
        map.on("mouseenter", "pp-icons", () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", "pp-icons", () => { map.getCanvas().style.cursor = ""; });
        this.styled = true;
      }
      const order = ids(), b = beforeId(order), top = map.getLayer("pp-labels") ? "pp-labels" : "pp-icons";
      if (b && order[order.indexOf(top) + 1] !== b) {      // a county layer slipped in above ours: back under the lines
        for (const id of ["pp-edit-fp", "pp-icons", "pp-labels"]) if (map.getLayer(id)) map.moveLayer(id, b);
      }
    }
    sync() {
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      try { this.ensure(); } catch (e) { console.error("pre-plan layer", e); return; }
      const fc = features(this.own(), st.layerOn ? this.boxPts : []);
      const sig = JSON.stringify(fc.features.map((f) => [f.properties, f.geometry.coordinates]));
      if (sig === this.sig) return;
      this.sig = sig;
      this.map.getSource(SRC).setData(fc);
    }
    // the layer: the viewport's points from LAYER_ZOOM, asked again only when the map leaves the box or a write moved
    viewport() {
      if (!st.layerOn || this.map.getZoom() < LAYER_ZOOM) {
        if (this.boxPts.length) { this.boxPts = []; this.box = null; this.sync(); }
        return;
      }
      const b = this.map.getBounds();
      const view = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
      const gen = pstate().gen;
      if (this.box && this.box.gen === gen && inside(view, this.box.b)) return;
      if (this.map.getZoom() >= LAYER_ZOOM) {
        const box = padBox(view), seq = ++this.seq;
        fetch(`/api/cad/preplan?bbox=${box.join(",")}`, { credentials: "same-origin" })
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
          .then((d) => { if (seq !== this.seq) return; this.box = { b: box, gen }; this.boxPts = d.points || []; this.sync(); })
          .catch((e) => console.error("pre-plan layer", e.message));
      }
    }
    refresh() { this.sync(); this.viewport(); }
    keys() { return []; }                              // per viewer, never the event's set
    panel() {
      const lab = el("label", "check pp-toggle");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = st.layerOn;
      cb.dataset.key = "preplan";
      cb.addEventListener("change", () => setPref(cb.checked));
      lab.append(cb, iconImg("knox_box"), el("span", "", "Pre-plan"));
      lab.title = "Every pre-plan point in view: Knox boxes, FDCs, shutoffs (yours, every map)";
      if (st.layerOn && this.map.getZoom() < LAYER_ZOOM) lab.append(el("span", "dim small", "zoom in"));
      const box = el("div", "ml-group");
      box.append(lab);
      return box;
    }
    find(id) {
      return this.own().concat(this.boxPts).find((p) => p.id === id) || null;
    }
    clicked(e) {
      if (st.edit) return;                              // placing or moving one: the map is the editor's
      const f = e.features && e.features[0];
      const p = f && this.find(f.properties.id);
      if (!p || !this.control.showInfo) return;
      const lines = [];
      if (p.note) lines.push(p.note);
      if (p.level) lines.push(levelOf(p.level));
      lines.push([p.addr_label || "No building", LINK[p.link] || ""].filter(Boolean).join(" · "));
      if (p.review_due) lines.push(el("span", "chip warn", "Review due"));
      if (window.canDo && canDo("dispatch") && !st.readonly && this.isCall()) {
        const b = el("button", "sm", "Edit");
        b.type = "button";
        b.addEventListener("click", () => { this.control.closeInfo(); edit({ point: p, map: this, host: $("#d-preplan-form"), done: redraw }); });
        lines.push(b);
      }
      this.control.showInfo(KINDS[p.kind] || p.kind, p.label || "", lines, `Pre-plan · ${p.by || "?"} ${AddrHistoryDay(p.updated_at)}`, "", iconImg(p.kind));
    }
  }
  const AddrHistoryDay = (t) => (window.AddrHistory ? AddrHistory.day(t) : "");

  function redraw() {
    for (const m of maps) m.sync();
    if (st.inc) renderList(st.inc);
    if (st.search) renderSearch();
  }
  // the working call's map (cadmap.js views.detail, through maplayers.js's box on it)
  const callMap = () => [...maps].find((m) => m.control.profile === "incident" && m.map.getContainer().closest("#d-map")) || null;

  // ---- the list beside the call's map ------------------------------------------------------------------------------------
  function card(inc, opts = {}) {
    if (!st.inc || st.inc.id !== inc.id) { cancelEdit(); st.fetched = null; }
    st.inc = inc;
    st.readonly = !!opts.readonly;
    renderList(inc);
    for (const m of maps) if (m.isCall()) m.sync();
  }
  function renderList(inc) {
    const box = $("#d-preplan");
    if (!box) return;
    const pts = pointsOf(inc);
    const located = !!(inc.bldg_key || inc.lat != null);
    const sig = JSON.stringify([inc.id, pts, st.readonly, located]);
    show(box, pts.length > 0 || (!st.readonly && located));
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    const head = el("div", "pp-head");
    head.append(el("span", "pp-title", "Pre-plan"), el("span", "dim small", pts.length ? String(pts.length) : ""), el("span", "spacer"));
    if (!st.readonly && located) {
      const add = el("button", "sm quiet", "+ Point");
      add.type = "button";
      add.title = "Place a building feature on the map (Knox box, FDC, shutoff ...)";
      add.addEventListener("click", () => {
        const m = callMap();
        if (m) edit({ map: m, host: $("#d-preplan-form"), ctx: { incident_id: inc.id }, center: inc.lat != null ? [inc.lon, inc.lat] : null, done: redraw });
      });
      head.append(add);
    }
    const list = el("ol", "pp-list");
    for (const p of pts) list.append(row(p, () => callMap(), $("#d-preplan-form"), st.readonly));
    box.replaceChildren(head, list);
  }
  function row(p, mapOf, host, readonly) {
    const li = el("li", "pp-row" + (p.nearby ? " near" : ""));
    const go = el("button", "pp-go", line(p));
    go.type = "button";
    go.prepend(iconImg(p.kind));
    go.title = [p.note, p.addr_label].filter(Boolean).join(" · ") || line(p);
    go.addEventListener("click", () => {
      const m = mapOf();
      if (m) m.map.easeTo({ center: [p.lon, p.lat], zoom: Math.max(m.map.getZoom(), 18), duration: 400 });
    });
    li.append(go);
    if (p.nearby) {
      const n = el("span", "chip pp-near", "nearby");
      n.title = `${p.addr_label || "No building"}: within 20 m (this call or that point has no exact address)`;
      li.append(n);
    }
    if (p.review_due) li.append(el("span", "chip warn", "Review"));
    if (!readonly && window.UI && UI.menu) li.append(UI.menu.button(`Pre-plan: ${line(p)}`, () => [
      { label: "Edit", run: () => { const m = mapOf(); if (m) edit({ point: p, map: m, host, done: redraw }); } },
      { label: "Move", hint: "drag it on the map", run: () => { const m = mapOf(); if (m) edit({ point: p, map: m, host, done: redraw, move: true }); } },
      { label: "Delete", run: () => remove(p) },
    ], { key: `pp:${p.id}` }));
    return li;
  }
  async function remove(p) {
    try {
      await api("POST", `/api/cad/preplan/${p.id}`, { action: "delete", operator: operator() });
      UI.toast(`${KINDS[p.kind] || "Point"} deleted`);
      cancelEdit();
      refetch();
    } catch (e) { UI.toast(e.message, "err"); }
  }
  function refetch() {
    st.fetched = null;
    for (const m of maps) m.box = null;
    if (st.search) loadSearch();
    redraw();
    for (const m of maps) m.viewport();
  }

  // ---- the editor: a kind, a place, a label ----------------------------------------------------------------------------
  // opts: {map: PPMap, host, point?: edit it, ctx?: {incident_id} | {where} for a new one, center?, move?, done}
  function cancelEdit() {
    const e = st.edit;
    if (!e) return;
    st.edit = null;
    if (e.marker) e.marker.remove();
    if (e.onClick) e.map.map.off("click", e.onClick);
    e.map.map.getCanvas().style.cursor = "";
    try { const s = e.map.map.getSource(EDIT_SRC); if (s) s.setData(features([], [])); } catch (_) { /* gone */ }
    if (e.host) { e.host.replaceChildren(); show(e.host, false); }
  }
  function edit(opts) {
    cancelEdit();
    const p = opts.point || null, pm = opts.map, map = pm.map, host = opts.host;
    const e = { map: pm, host, kind: p ? p.kind : "knox_box", at: p ? [p.lon, p.lat] : null, link: "auto", marker: null, seq: 0 };
    st.edit = e;
    const f = el("form", "pp-form card");
    f.autocomplete = "off";
    const head = el("div", "pp-form-head");
    head.append(el("b", "", p ? `Edit ${KINDS[p.kind] || "point"}` : "Pre-plan point"));
    const hint = el("span", "dim small", p ? "" : "Tap the map to place");
    head.append(hint);
    const kinds = el("div", "pp-kinds");
    kinds.setAttribute("role", "radiogroup");
    kinds.setAttribute("aria-label", "Kind");
    const pick = (k) => {
      e.kind = k;
      for (const b of kinds.children) b.setAttribute("aria-checked", String(b.dataset.kind === k));
      if (e.marker) e.marker.getElement().replaceChildren(iconImg(k, "pp-mark-i"));
    };
    for (const [k, name] of Object.entries(KINDS)) {
      const b = el("button", "pp-kind");
      b.type = "button";
      b.dataset.kind = k;
      b.setAttribute("role", "radio");
      b.append(iconImg(k), el("span", "", name));
      b.addEventListener("click", () => pick(k));
      kinds.append(b);
    }
    const input = (ph, max, v, aria) => { const i = el("input"); Object.assign(i, { type: "text", maxLength: max, placeholder: ph, value: v || "", spellcheck: false }); i.setAttribute("aria-label", aria); return i; };
    const label = input("Label (Front door left)", 60, p && p.label, "Label");
    const level = input("Level", 20, p && p.level, "Level");
    level.className = "pp-level";
    const note = el("textarea");
    Object.assign(note, { rows: 2, maxLength: 300, placeholder: "Note", value: p ? p.note : "" });
    note.setAttribute("aria-label", "Note");
    const prio = el("select");
    prio.setAttribute("aria-label", "Priority");
    for (const [k, v] of Object.entries(PRIO)) prio.append(new Option(v, k));
    prio.value = String(p ? p.priority : 2);
    const review = Object.assign(el("input"), { type: "date", value: p && window.AddrHistory ? AddrHistory.isoDay(p.review_by) : "" });
    review.setAttribute("aria-label", "Review by");
    const revL = el("label", "inline");
    revL.append(el("span", "dim", "Review by"), " ", review);
    revL.title = "Shown as due from this day";
    const fields = el("div", "row pp-fields");
    fields.append(level, prio, revL);
    const bldg = el("div", "pp-bldg small");
    const err = el("span", "error");
    const save = el("button", "primary", "Save");
    save.type = "submit";
    const cancel = el("button", "", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", cancelEdit);
    const foot = el("div", "row");
    foot.append(err, el("span", "spacer"));
    if (p) {
      const del = el("button", "danger-quiet", "Delete");
      del.type = "button";
      del.addEventListener("click", () => remove(p));
      foot.append(del);
    }
    foot.append(cancel, save);
    f.append(head, kinds, label, note, fields, bldg, foot);
    pick(e.kind);

    // where: a draggable mark; the building it belongs to, from the hub
    const where = async () => {
      if (!e.at) { bldg.replaceChildren(); return; }
      const seq = ++e.seq;
      const q = new URLSearchParams({ lat: e.at[1].toFixed(7), lon: e.at[0].toFixed(7), link: e.link });
      if (p) q.set("point", p.id);
      else if (opts.ctx && opts.ctx.incident_id != null) q.set("incident_id", opts.ctx.incident_id);
      else if (opts.ctx && opts.ctx.where) q.set("where", opts.ctx.where);
      try {
        const a = await api("GET", "/api/cad/preplan/where?" + q);
        if (seq !== e.seq || st.edit !== e) return;
        const src = map.getSource(EDIT_SRC);
        if (src) src.setData(a.geometry ? { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: a.geometry }] } : features([], []));
        const txt = el("span", "", a.addr_label || "No building");
        const how = el("span", "dim", ` · ${LINK[a.link] || ""}${a.link === "nearest" && a.dist_m != null ? ` ${Math.round(a.dist_m)} m` : ""}`);
        how.title = a.footprints === false ? "No county footprints loaded here: linked to the call's building or the point" : "County building footprint";
        const parts = [el("span", "dim", "Building "), txt, how];
        const choices = Object.entries(a.choices || {});
        if (choices.length > 1) {
          const sel = el("select", "pp-link");
          sel.setAttribute("aria-label", "Building");
          sel.append(new Option("Auto", "auto"));
          for (const [k, v] of choices) sel.append(new Option({ building: `Footprint: ${v}`, call: `This call: ${v || "its point"}`, point: "Map point only" }[k] || v, k));
          sel.value = e.link;
          sel.addEventListener("change", () => { e.link = sel.value; where(); });
          parts.push(" ", sel);
        }
        bldg.replaceChildren(...parts);
      } catch (ex) { if (seq === e.seq) bldg.replaceChildren(el("span", "error", ex.message)); }
    };
    const place = (lngLat) => {
      e.at = [lngLat.lng, lngLat.lat];
      if (!e.marker) {
        const mk = el("div", "pp-mark");
        mk.append(iconImg(e.kind, "pp-mark-i"));
        e.marker = new maplibregl.Marker({ element: mk, draggable: true, anchor: "center" }).setLngLat(e.at).addTo(map);
        e.marker.on("dragend", () => { const ll = e.marker.getLngLat(); e.at = [ll.lng, ll.lat]; where(); });
      } else e.marker.setLngLat(e.at);
      setText(hint, "Drag to adjust");
      where();
    };
    if (p) place({ lng: p.lon, lat: p.lat });
    if (!p || opts.move) {
      e.onClick = (ev) => place(ev.lngLat);             // a tap places it (again); a drag adjusts
      map.on("click", e.onClick);
      map.getCanvas().style.cursor = "crosshair";
    }
    const c = p ? [p.lon, p.lat] : opts.center;
    if (c) map.easeTo({ center: c, zoom: Math.max(map.getZoom(), 18), duration: 300 });

    f.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!e.at) { setText(err, "Tap the map to place it"); return; }
      const body = { kind: e.kind, label: label.value, note: note.value, level: level.value, priority: Number(prio.value),
                     review_by: review.value || null, operator: operator() };
      const moved = !p || p.lon !== e.at[0] || p.lat !== e.at[1] || e.link !== "auto";
      if (moved) Object.assign(body, { lat: e.at[1], lon: e.at[0], link: e.link });
      try {
        if (p) await api("POST", `/api/cad/preplan/${p.id}`, body);
        else await api("POST", "/api/cad/preplan", { ...body, ...(opts.ctx || {}) });
        UI.toast(p ? `${KINDS[e.kind]} saved` : `${KINDS[e.kind]} added`);
        cancelEdit();
        refetch();
        if (opts.done) opts.done();
      } catch (ex) { setText(err, ex.message); }
    });
    host.replaceChildren(f);
    show(host, true);
    (p ? label : kinds.firstChild).focus({ preventScroll: true });
  }

  // ---- the Search page's Pre-plan tab: one building's points on its own map (all of them, and the CSV: admins) ----------
  async function loadSearch() {
    const s = st.search;
    if (!s) return;
    const seq = ++s.seq;
    const q = new URLSearchParams();
    if (s.where) q.set("where", s.where);
    try {
      const d = await api("GET", `/api/cad/preplan?${q}`);
      if (seq !== s.seq) return;
      s.data = d;
      s.err = "";
    } catch (e) {
      if (seq !== s.seq) return;
      s.data = null;
      s.err = e.message;
    }
    renderSearch(true);
  }
  function search(where) {
    st.search = st.search || { seq: 0, map: null, pm: null };
    st.search.where = (where || "").trim();
    show($("#sv-pp-add"), !!st.search.where && canDo("dispatch"));
    const csv = $("#sv-pp-csv");
    show(csv, canDo("admin"));
    if (!st.search.where && !canDo("admin")) {
      st.search.data = null;
      st.search.err = "Type an address";
      renderSearch(true);
      return;
    }
    loadSearch();
  }
  function searchMap() {
    const s = st.search;
    if (s.pm || !window.maplibregl || typeof buildMapStyle !== "function" || !cad.scene || !cad.scene.home) return s.pm;
    s.map = new maplibregl.Map({ container: $("#sv-pp-map"), style: buildMapStyle(cad.scene), center: cad.scene.home.center,
                                 zoom: cad.scene.home.zoom, attributionControl: { compact: true }, maxPitch: 0, dragRotate: false });
    s.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    s.pm = new PPMap({ map: s.map, profile: "search", focused: null });
    s.map.on("load", () => renderSearch(true));
    return s.pm;
  }
  function renderSearch(frame) {
    const s = st.search;
    if (!s) return;
    const d = s.data, pts = (d && d.points) || [];
    setText($("#sv-count"), d ? `${pts.length} point${pts.length === 1 ? "" : "s"}` + (d.key ? ` · ${d.key.label}` : "") : "");
    const body = $("#sv-pp-rows");
    if (!d) body.replaceChildren(el("li", "empty", s.err || "Loading"));
    else if (!pts.length) body.replaceChildren(el("li", "empty", "No pre-plan points"));
    else body.replaceChildren(...pts.map((p) => {
      const r = row({ ...p, nearby: false }, () => s.pm, $("#sv-pp-form"), !canDo("dispatch"));
      if (!s.where && p.addr_label) r.append(el("span", "dim small", p.addr_label));
      return r;
    }));
    const pm = searchMap();
    if (!pm) return;
    pm.fixed = pts;
    pm.sync();
    if (frame && s.map.loaded()) {
      s.map.resize();
      const at = pts.length ? null : d && d.point;
      if (pts.length) {
        const b = new maplibregl.LngLatBounds();
        for (const p of pts) b.extend([p.lon, p.lat]);
        s.map.fitBounds(b, { padding: 60, maxZoom: 18.5, duration: 0 });
      } else if (at) s.map.jumpTo({ center: [at[1], at[0]], zoom: 18 });
    }
  }
  function searchAdd() {
    const s = st.search;
    if (!s || !s.where || !s.pm) return;
    const at = s.data && s.data.point;
    edit({ map: s.pm, host: $("#sv-pp-form"), ctx: { where: s.where }, center: at ? [at[1], at[0]] : null, done: () => loadSearch() });
  }
  function closeSearch() { if (st.search) { st.search.seq++; if (st.edit && st.edit.map === st.search.pm) cancelEdit(); } }

  const api_ = { KINDS, PREF_KEY, LAYER_ZOOM, line, beforeId, features, padBox, inside };
  if (typeof document === "undefined") return api_;

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new PPMap(control) });
  if (window.cad && cad.hooks) cad.hooks.push(() => { for (const m of maps) if (m.box && m.box.gen !== pstate().gen) m.viewport(); });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && st.edit && !ev.defaultPrevented) cancelEdit(); });
  if ($("#sv-pp-add")) $("#sv-pp-add").addEventListener("click", searchAdd);
  if ($("#sv-pp-csv")) $("#sv-pp-csv").href = "/api/cad/preplan.csv";
  return { ...api_, card, search, closeSearch, edit, cancelEdit };
})();
