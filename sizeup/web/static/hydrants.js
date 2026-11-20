// Hydrants (#799, sizeup/cad/hydrants.py): the department's data over the county's hydrant layer, on every CAD map.
//
//   The marks    the county layer (gislayers.js) comes from the hub with the overlay in it (_k its key, _no the number,
//                _cls the NFPA 291 class, _gpm, _st the status, _miss, _hub). Each hydrant is drawn in its class's cap
//                colour (NFPA 291 Table 5.1: AA light blue >= 1500 GPM, A green 1000-1499, B orange 500-999, C red
//                < 500); no flow: a neutral grey (never red: red means under 500 GPM). Out of service or dry: a hollow
//                mark with a red X, placed first (never thinned out by its neighbours); restricted: the class mark with
//                an amber dot. From the label zoom the number and the class letter beside it (colour is never the only
//                cue). A small legend under the layer's toggle in the Layers box.
//   The card     a click: the class and GPM, the status, the town (the boundary's, not the district's), thread,
//                outlets, owner, asset id, where; GET /api/cad/hydrants/<key>. Admins: Edit (a dialog), Move (drag the
//                mark: Undo puts it back, Save asks with the distance), Remove / Restore, the history.
//   Add          admins: "+ Add" by the legend, tap the map, drag to adjust, then the form.
//   A call       its nearest hydrants (the state push: inc.hydrants {near, down}) beside its map (#d-hydrants); the out of
//                service ones within 300 m called out.
//   Settings     Settings > Hydrants (admins): search and filters, the list, CSV export and import (preview, then apply).
// Uses cad.js's helpers (el, $, show, setText, api, operator, canDo, UI, cad): loaded after it, before gislayers.js.
"use strict";

window.Hydrants = (() => {
  // sizeup/cad/hydrants.py CLASSES (NFPA 291 Table 5.1): class, least rated GPM, cap colour, the map's hex for it
  const CLASSES = [["AA", 1500, "Light blue", "#5CC8FF"], ["A", 1000, "Green", "#3FB86B"], ["B", 500, "Orange", "#FF9A3C"],
                   ["C", 0, "Red", "#E5484D"]];
  const UNK = "#8B949E";                       // no flow data: neutral
  const DOWN = ["oos", "dry"];
  const STATUS = { in_service: "In service", oos: "Out of service", restricted: "Restricted", dry: "Dry", unknown: "Unknown" };
  const SHORT = { oos: "OOS", dry: "Dry", restricted: "Restricted" };
  const THREADS = { nst: "NST (NH)", nyc: "NYC / NYFD pipe", storz4: "Storz 4\"", storz5: "Storz 5\"", other: "Other" };
  const OWNERS = { water_company: "Water company", municipal: "Municipal", private: "Private" };
  const FLAGS = [["", "All"], ["oos", "OOS"], ["restricted", "Restricted"], ["no_flow", "No flow"], ["moved", "Moved"],
                 ["missing", "Missing from county"], ["removed", "Removed"], ["hub", "Added"], ["edited", "Edited"]];
  const LEGEND = [["AA", "≥ 1500 gpm"], ["A", "1000–1499 gpm"], ["B", "500–999 gpm"], ["C", "< 500 gpm"], ["unk", "No flow data"],
                  ["down", "Out of service / dry"]];
  const SRC_NOTE = "NFPA 291 cap colours, rated flow at 20 psi residual";

  // ---- pure helpers (tests/test_hydrants.py runs these in node) ------------------------------------------------------
  const classOf = (gpm) => (gpm === null || gpm === undefined ? null : CLASSES.find((c) => gpm >= c[1])[0]);
  function ratedGpm(flow, s, r) {
    if (flow === null || flow === undefined) return null;
    if (s != null && r != null && s > 20 && s > r && r >= 0) return Math.round(flow * ((s - 20) / (s - r)) ** 0.54);
    return Math.round(flow);
  }
  const hex = (cls) => (CLASSES.find((c) => c[0] === cls) || [0, 0, 0, UNK])[3];
  const colorName = (cls) => (CLASSES.find((c) => c[0] === cls) || [0, 0, ""])[2];
  // the map's image for a hydrant's properties (the same rule as iconExpr)
  function imageFor(p) {
    if (DOWN.includes(p._st)) return "hy-down";
    const base = CLASSES.some((c) => c[0] === p._cls) ? `hy-${p._cls}` : "hy-unk";
    return p._st === "restricted" ? `${base}-r` : base;
  }
  function iconExpr() {
    const st = ["coalesce", ["get", "_st"], ""], cls = ["coalesce", ["get", "_cls"], ""];
    const by = (sfx) => ["match", cls, ...CLASSES.flatMap((c) => [c[0], `hy-${c[0]}${sfx}`]), `hy-unk${sfx}`];
    return ["case", ["in", st, ["literal", DOWN]], "hy-down", ["==", st, "restricted"], by("-r"), by("")];
  }
  // out of service first, then restricted: placed before (never thinned out by) their neighbours
  const sortExpr = () => ["case", ["in", ["coalesce", ["get", "_st"], ""], ["literal", DOWN]], 0, ["==", ["get", "_st"], "restricted"], 1, 2];
  // "9001 · A", "9002 · OOS" (beside the mark from the label zoom)
  function labelExpr() {
    const no = ["to-string", ["coalesce", ["get", "_no"], ["get", "HYD_NUMBER"], ""]];
    return ["concat", no,
      ["case", ["in", ["coalesce", ["get", "_st"], ""], ["literal", DOWN]], " · OOS", ["has", "_cls"], ["concat", " · ", ["get", "_cls"]], ""]];
  }
  const fmtDist = (m) => `${Math.round(m)} m`;
  // a call's row: "9001 · A 1200 gpm · OOS · 40 m"
  function line(h) {
    const cls = h.cls ? `${h.cls}${h.gpm != null ? ` ${h.gpm} gpm` : ""}` : "";
    return [h.number || "?", cls, SHORT[h.status] || "", h.dist_m != null ? fmtDist(h.dist_m) : ""].filter(Boolean).join(" · ");
  }
  const api_ = { CLASSES, DOWN, LEGEND, classOf, ratedGpm, imageFor, iconExpr, sortExpr, labelExpr, fmtDist, line };
  if (typeof document === "undefined") return api_;

  // ---- the marks: the county sprite's hydrant in each class's colour --------------------------------------------------
  const LIGHT = "#F2F3F5", DARK = "#15181B", AMBER = "#F5B83D";
  function paint(shape, fill, extra) {
    const px = 48, pad = 3, c = document.createElement("canvas");
    c.width = c.height = px;
    const g = c.getContext("2d"), k = (px - 2 * pad) / 24;
    g.translate(pad, pad); g.scale(k, k);
    g.lineJoin = "round"; g.lineCap = "round";
    const body = shape.body.map((d) => new Path2D(d));
    g.strokeStyle = fill ? LIGHT : LIGHT; g.lineWidth = 3 / k;
    for (const p of body) g.stroke(p);
    g.fillStyle = fill || DARK;
    for (const p of body) g.fill(p);
    if (!fill) { g.strokeStyle = UNK; g.lineWidth = 1.2; for (const p of body) g.stroke(p); }
    if (extra) extra(g);
    return g.getImageData(0, 0, px, px);
  }
  const cross = (g) => {                                // a red X over the hollow mark, on a light halo
    for (const [w, col] of [[5, LIGHT], [2.6, CLASSES[3][3]]]) {
      g.strokeStyle = col; g.lineWidth = w;
      g.beginPath(); g.moveTo(4, 4); g.lineTo(20, 20); g.moveTo(20, 4); g.lineTo(4, 20); g.stroke();
    }
  };
  const dot = (g) => { g.fillStyle = DARK; g.beginPath(); g.arc(19.5, 4.5, 4.4, 0, 2 * Math.PI); g.fill();
                       g.fillStyle = AMBER; g.beginPath(); g.arc(19.5, 4.5, 3.2, 0, 2 * Math.PI); g.fill(); };
  // gislayers.js: the images on one map (once): hy-AA ... hy-C, hy-unk, each -r (restricted), hy-down
  function addImages(map, shape) {
    if (!shape || map.hasImage("hy-down")) return;
    for (const [cls, col] of [...CLASSES.map((c) => [c[0], c[3]]), ["unk", UNK]]) {
      map.addImage(`hy-${cls}`, paint(shape, col), { pixelRatio: 2 });
      map.addImage(`hy-${cls}-r`, paint(shape, col, dot), { pixelRatio: 2 });
    }
    map.addImage("hy-down", paint(shape, null, cross), { pixelRatio: 2 });
  }

  // a small mark in the page: the class's colour with its letter (the legend, the card, a call's list)
  function mark(cls, st) {
    const down = DOWN.includes(st);
    const m = el("span", "hy-mark" + (down ? " down" : ""), down ? "✕" : cls || "?");
    if (!down) m.style.setProperty("--hy", cls ? hex(cls) : UNK);
    m.setAttribute("aria-hidden", "true");
    return m;
  }

  // ---- the legend under the layer's toggle (gislayers.js panel) -----------------------------------------------------------
  function legend(gm) {
    const box = el("div", "hy-legend small");
    for (const [k, tip] of LEGEND) {
      const s = el("span", "hy-key");
      s.append(k === "down" ? mark(null, "oos") : mark(k === "unk" ? null : k), el("span", "", k === "unk" ? "no flow" : k === "down" ? "OOS" : ""));
      s.title = k === "unk" || k === "down" ? tip : `Class ${k}: ${tip}`;
      box.append(s);
    }
    box.title = SRC_NOTE;
    if (canDo("admin") && !BOARD_() && gm) {
      const add = el("button", "sm quiet ro-ok", "+ Add");
      add.type = "button";
      add.title = "Add a hydrant the county layer lacks: tap the map";
      add.addEventListener("click", () => { gm.control.close(); place(gm); });
      box.append(add);
    }
    return box;
  }
  const BOARD_ = () => (typeof BOARD !== "undefined" && BOARD);

  // ---- the card -------------------------------------------------------------------------------------------------------------
  const day = (iso) => {
    if (!iso) return "";
    try { return new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); }
    catch (_) { return iso; }
  };
  const when = (t) => (t ? new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "");
  const st = { seq: 0, move: null, gen: null };

  function lines(h) {
    const out = [];
    const cl = el("div", "hy-line");
    cl.append(mark(h.cls, h.status));
    if (h.cls) {
      const t = el("span", "", `Class ${h.cls}${h.gpm != null ? ` · ${h.gpm} gpm` : ""}`);
      t.title = `${colorName(h.cls)} cap (${SRC_NOTE})` + (h.class_set ? ` · set by hand (flow says ${h.class_flow || "none"})` : "")
        + (h.static_psi != null && h.residual_psi != null && h.flow_gpm != null ? ` · ${h.flow_gpm} gpm flowed at ${h.residual_psi} psi residual` : "");
      cl.append(t);
    } else cl.append(el("span", "dim", "No flow data"));
    out.push(cl);
    if (h.status && h.status !== "unknown") {
      const down = DOWN.includes(h.status);
      const s = el("div", "", [h.status_label || STATUS[h.status], h.status_reason].filter(Boolean).join(": "));
      if (down || h.status === "restricted") s.className = down ? "err" : "warn";
      const dates = [h.status_since ? `since ${day(h.status_since)}` : "", h.status_until ? `back ${day(h.status_until)}` : ""].filter(Boolean);
      if (dates.length) s.append(el("span", "dim", ` · ${dates.join(" · ")}`));
      out.push(s);
    }
    const where = [h.location, h.district].filter(Boolean);           // (the town is the card's subtitle)
    if (where.length) out.push(where.join(" · "));
    const kit = [h.thread ? THREADS[h.thread] || h.thread : "", h.outlets, h.main_in ? `${h.main_in}" main` : ""].filter(Boolean);
    if (kit.length) out.push(kit.join(" · "));
    const own = [h.owner ? OWNERS[h.owner] || h.owner : "", h.asset_id ? `Asset ${h.asset_id}` : "", h.body_color ? `${h.body_color} body` : ""].filter(Boolean);
    if (own.length) out.push(own.join(" · "));
    const dates = [h.flow_date ? `Flow test ${day(h.flow_date)}` : "", h.inspected ? `Inspected ${day(h.inspected)}` : ""].filter(Boolean);
    if (dates.length) out.push(dates.join(" · "));
    if (h.notes) out.push(h.notes);
    const flags = [];
    if (h.missing) flags.push(["Missing from county", "The county's layer no longer has this hydrant: kept, check it"]);
    if (h.removed) flags.push(["Removed", "Hidden from every map"]);
    if (h.hub) flags.push(["Added here", "Not in the county's layer"]);
    if (h.nudge_m != null) flags.push([`Moved ${fmtDist(h.nudge_m)}`, "From the county's point"]);
    if (flags.length) {
      const f = el("div", "chips");
      for (const [t, tip] of flags) { const c = el("span", "chip" + (t.startsWith("Missing") ? " warn" : ""), t); c.title = tip; f.append(c); }
      out.push(f);
    }
    return out;
  }

  // a click on a hydrant (gislayers.js): what the layer had at once, then the hub's card
  function showCard(gm, props) {
    const key = props._k;
    const quick = { key, number: props._no || props.HYD_NUMBER || "", cls: props._cls || null, gpm: props._gpm ?? null,
                    status: props._st || "unknown", missing: !!props._miss, hub: !!props._hub, district: props.FIRENAME || "" };
    const draw = (h, more) => {
      const ls = lines(h);
      if (more) ls.push(...more);
      gm.control.close();
      gm.control.showInfo(`Hydrant ${h.number || "?"}`, h.town || (h.hub ? "Added here" : ""), ls,
                          ["Westchester County GIS", h.by ? `${h.by} ${when(h.updated_at)}` : ""].filter(Boolean).join(" · "), "hy-card",
                          null, SRC_NOTE);
    };
    draw(quick);
    if (!key) return;
    const seq = ++st.seq;
    api("GET", `/api/cad/hydrants/${encodeURIComponent(key)}`).then((d) => {
      if (seq !== st.seq) return;
      draw(d.hydrant, [...adminRow(gm, d.hydrant), ...(d.history && d.history.length ? [historyEl(d.history)] : [])]);
    }).catch((e) => { if (seq === st.seq) draw(quick, [el("div", "error", e.message)]); });
  }
  function adminRow(gm, h) {
    if (!canDo("admin") || BOARD_()) return [];
    const row = el("div", "row hy-acts");
    const btn = (t, tip, fn, cls = "sm") => { const b = el("button", cls, t); b.type = "button"; b.title = tip; b.addEventListener("click", fn); row.append(b); return b; };
    btn("Edit", "Status, flow, thread, outlets ...", () => { gm.control.closeInfo(); editor(h); });
    if (!h.removed) btn("Move", "Drag it to where it stands", () => { gm.control.closeInfo(); move(gm, h); });
    if (h.removed) btn("Restore", "Show it on the maps again", () => act(h, "restore"));
    else btn("Remove", h.hub ? "Delete this added hydrant (reversible)" : "Hide a county hydrant that isn't there (reversible)",
             () => remove(h), "sm danger-quiet");
    return [row];
  }
  function historyEl(history) {
    const d = el("details", "hy-hist small");
    d.append(el("summary", "dim", `History (${history.length})`));
    const ol = el("ol", "hy-hist-list");
    for (const r of history) {
      let o = {}, n = {};
      try { o = JSON.parse(r.old || "null") || {}; n = JSON.parse(r.new || "null") || {}; } catch (_) { /* as it is */ }
      const what = Object.keys(n).map((k) => (k in o ? `${k}: ${fmtVal(o[k])} → ${fmtVal(n[k])}` : `${k}: ${fmtVal(n[k])}`)).join(" · ");
      ol.append(el("li", "", `${when(r.at)} ${r.by || "?"} · ${what || "added"}`));
    }
    d.append(ol);
    return d;
  }
  const fmtVal = (v) => (v === null || v === undefined || v === "" ? "—" : typeof v === "number" ? String(Math.round(v * 1e6) / 1e6) : String(v));

  async function act(h, action) {
    try {
      await api("POST", `/api/cad/hydrants/${encodeURIComponent(h.key)}`, { action, operator: operator() });
      UI.toast(`Hydrant ${h.number || h.key} ${action === "remove" ? "removed" : "restored"}`);
      changed();
    } catch (e) { UI.toast(e.message, "err"); }
  }
  async function remove(h) {
    if (!(await UI.confirm(`Remove hydrant ${h.number || h.key} from every map? (Settings > Hydrants can restore it)`, { ok: "Remove" }))) return;
    act(h, "remove");
  }
  function changed() {
    st.changedAt = Date.now();
    if (window.GisLayers) GisLayers.load(true);     // the catalog's rev moved (past the browser's copy): the layer again
    if (S.list && canDo("admin")) loadList();
  }

  // ---- the editor (a dialog: any map, Settings) ---------------------------------------------------------------------------
  const FORM = [   // [field, label, kind, extra]
    ["number", "Number", "text", 20], ["status", "Status", "select", STATUS], ["status_reason", "Reason", "text", 200],
    ["status_since", "Since", "date"], ["status_until", "Expected back", "date"],
    ["flow_gpm", "Flow (gpm)", "number", [0, 20000, 1]], ["flow_date", "Flow test", "date"],
    ["static_psi", "Static psi", "number", [0, 300, 0.1]], ["residual_psi", "Residual psi", "number", [0, 300, 0.1]],
    ["nfpa_class", "Class", "select", null], ["thread", "Thread", "select", THREADS], ["outlets", "Outlets", "text", 60],
    ["main_in", "Main (in)", "number", [0, 60, 0.1]], ["owner", "Owner", "select", OWNERS], ["body_color", "Body colour", "text", 30],
    ["asset_id", "Asset id", "text", 40], ["location", "Location", "text", 120], ["inspected", "Inspected", "date"],
    ["notes", "Notes", "textarea", 500],
  ];
  const TIPS = { flow_gpm: "Flow as tested (with both pressures: the class uses the flow at 20 psi residual), or the rated capacity",
                 outlets: "e.g. 2x2.5, 1x4.5", number: "Empty: the county's", nfpa_class: "Empty: from the flow (NFPA 291)",
                 location: "Street, side, landmark", status_until: "Expected back in service" };

  // h: a hydrant ({key, ...}); at: [lon, lat] for a new one (no key)
  function editor(h, at) {
    const isNew = !h || !h.key;
    h = h || {};
    const ov = el("div", "overlay");
    const panel = el("div", "dialog");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    const head = el("div", "dialog-head");
    head.append(el("h2", "dialog-title", isNew ? "New hydrant" : `Hydrant ${h.number || h.key}`));
    if (!isNew) head.append(el("span", "dim small", [h.town, h.hub ? "added here" : `county ${h.county_number || "?"}`].filter(Boolean).join(" · ")));
    panel.setAttribute("aria-label", isNew ? "New hydrant" : `Hydrant ${h.number || h.key}`);
    const form = el("form", "dialog-body hy-form");
    form.autocomplete = "off";
    const inputs = {};
    for (const [k, label, kind, extra] of FORM) {
      const lab = el("label", "field" + (kind === "textarea" || k === "location" || k === "status_reason" ? " span2" : ""));
      lab.append(label);
      let inp;
      if (kind === "select") {
        inp = el("select");
        const opts = k === "nfpa_class" ? Object.fromEntries(CLASSES.map((c) => [c[0], `${c[0]} (${c[2].toLowerCase()})`])) : extra;
        if (k !== "status") inp.append(new Option(k === "nfpa_class" ? "From the flow" : "—", ""));
        for (const [v, t] of Object.entries(opts)) inp.append(new Option(t, v));
      } else if (kind === "textarea") {
        inp = el("textarea"); inp.rows = 2; inp.maxLength = extra;
      } else {
        inp = el("input");
        inp.type = kind;
        if (kind === "text") { inp.maxLength = extra; inp.spellcheck = false; }
        if (kind === "number") { [inp.min, inp.max, inp.step] = extra.map(String); }
      }
      const v = h[k];
      inp.value = v === null || v === undefined ? (k === "status" ? "unknown" : "") : String(v);
      if (k === "number" && !isNew && !h.hub && !(h.number && h.number !== h.county_number)) inp.value = "";
      if (k === "number" && !h.hub) inp.placeholder = h.county_number || "";
      if (TIPS[k]) lab.title = TIPS[k];
      lab.append(inp);
      inputs[k] = inp;
      form.append(lab);
    }
    const cls = el("div", "hy-line hy-cls small");
    const showCls = () => {
      const n = (x) => (inputs[x].value === "" ? null : Number(inputs[x].value));
      const g = ratedGpm(n("flow_gpm"), n("static_psi"), n("residual_psi"));
      const c = inputs.nfpa_class.value || classOf(g);
      cls.replaceChildren(mark(c, inputs.status.value), el("span", "", c ? `Class ${c}${g != null ? ` · ${g} gpm rated` : ""}` : "No class (no flow)"));
    };
    for (const k of ["flow_gpm", "static_psi", "residual_psi", "nfpa_class", "status"]) inputs[k].addEventListener("input", showCls);
    showCls();
    form.prepend(cls);
    const err = el("span", "error");
    const foot = el("div", "dialog-foot");
    if (!isNew && h.nudge_m != null && !h.missing) {
      const back = el("button", "quiet", `Reset position (${fmtDist(h.nudge_m)})`);
      back.type = "button";
      back.title = "Back to the county's point";
      back.addEventListener("click", async () => {
        try { await api("POST", `/api/cad/hydrants/${encodeURIComponent(h.key)}`, { action: "reset_position", operator: operator() });
              UI.toast("Position reset"); UI.dialog.close(ov); changed(); } catch (e) { setText(err, e.message); }
      });
      foot.append(back);
    }
    foot.append(err, el("span", "spacer"));
    const no = el("button", "", "Cancel");
    no.type = "button";
    no.addEventListener("click", () => UI.dialog.close(ov));
    const yes = el("button", "primary", isNew ? "Add" : "Save");
    yes.type = "submit";
    foot.append(no, yes);
    form.append(foot);
    panel.append(head, form);
    ov.append(panel);
    document.body.append(ov);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { operator: operator() };
      for (const [k, , kind] of FORM) {
        const v = inputs[k].value.trim();
        const was = h[k] === null || h[k] === undefined ? (k === "status" ? "unknown" : "") : String(h[k]);
        const cmp = k === "number" && !isNew && !h.hub ? (h.number !== h.county_number ? h.number : "") : was;
        if (isNew ? v !== "" && !(k === "status" && v === "unknown") : v !== cmp) body[k] = kind === "number" && v !== "" ? Number(v) : v;
      }
      try {
        if (isNew) {
          await api("POST", "/api/cad/hydrants", { ...body, lat: at[1], lon: at[0] });
          UI.toast("Hydrant added");
        } else if (Object.keys(body).length > 1) {
          await api("POST", `/api/cad/hydrants/${encodeURIComponent(h.key)}`, body);
          UI.toast(`Hydrant ${h.number || h.key} saved`);
        }
        UI.dialog.close(ov);
        changed();
      } catch (ex) { setText(err, ex.message); }
    });
    UI.dialog.open(ov, { focus: inputs.status, onClose: () => { ov.remove(); if (st.move && st.move.adding) endMove(); } });
  }

  // ---- move (and place a new one): a draggable mark, Undo, Save with the distance -------------------------------------
  const metres = (a, b) => {
    const ky = 110574, kx = 111320 * Math.cos(((a[1] + b[1]) / 2) * Math.PI / 180);
    return Math.hypot((b[1] - a[1]) * ky, (b[0] - a[0]) * kx);
  };
  function endMove() {
    const m = st.move;
    if (!m) return;
    st.move = null;
    if (m.marker) m.marker.remove();
    if (m.onClick) m.map.off("click", m.onClick);
    m.map.getCanvas().style.cursor = "";
    m.bar.remove();
  }
  function moveBar(gm, text) {
    const bar = el("div", "cm-pop hy-movebar");
    const msg = el("span", "", text);
    bar.append(msg, el("span", "spacer"));
    gm.map.getContainer().append(bar);
    return { bar, msg };
  }
  function dragMark(m, at) {
    const d = el("div", "hy-drag");
    d.append(mark(m.cls || null, null));
    m.marker = new maplibregl.Marker({ element: d, draggable: true, anchor: "center" }).setLngLat(at).addTo(m.map);
    m.marker.on("drag", () => m.update());
    m.marker.on("dragend", () => m.update());
  }
  function move(gm, h) {
    endMove();
    const start = [h.lon, h.lat], county = h.county_lon != null ? [h.county_lon, h.county_lat] : null;
    const { bar, msg } = moveBar(gm, "Drag the hydrant");
    const m = { map: gm.map, bar, cls: h.cls };
    m.update = () => {
      const ll = m.marker.getLngLat(), p = [ll.lng, ll.lat];
      const d = metres(start, p);
      setText(msg, `Moved ${fmtDist(d)}` + (county && !h.hub ? ` · ${fmtDist(metres(county, p))} from county` : ""));
    };
    st.move = m;
    dragMark(m, start);
    const b = (t, fn, cls = "sm") => { const x = el("button", cls, t); x.type = "button"; x.addEventListener("click", fn); bar.append(x); return x; };
    b("Undo", () => { m.marker.setLngLat(start); setText(msg, "Drag the hydrant"); });
    b("Cancel", endMove);
    b("Save", async () => {
      const ll = m.marker.getLngLat(), d = metres(start, [ll.lng, ll.lat]);
      if (d < 0.5) { endMove(); return; }
      if (!(await UI.confirm(`Move hydrant ${h.number || h.key} ${fmtDist(d)}?`, { ok: "Move", danger: false }))) return;
      try {
        await api("POST", `/api/cad/hydrants/${encodeURIComponent(h.key)}`, { lat: +ll.lat.toFixed(7), lon: +ll.lng.toFixed(7), operator: operator() });
        UI.toast(`Hydrant ${h.number || h.key} moved ${fmtDist(d)}`);
        endMove();
        changed();
      } catch (e) { UI.toast(e.message, "err"); }
    }, "sm primary");
    gm.map.easeTo({ center: start, zoom: Math.max(gm.map.getZoom(), 18), duration: 300 });
  }
  function place(gm) {
    endMove();
    const { bar, msg } = moveBar(gm, "Tap the map to place the hydrant");
    const m = { map: gm.map, bar, adding: true, update: () => {} };
    st.move = m;
    const next = el("button", "sm primary", "Next");
    next.type = "button";
    next.disabled = true;
    m.onClick = (e) => {
      if (!m.marker) { dragMark(m, [e.lngLat.lng, e.lngLat.lat]); next.disabled = false; setText(msg, "Drag to adjust"); }
      else m.marker.setLngLat(e.lngLat);
    };
    gm.map.on("click", m.onClick);
    gm.map.getCanvas().style.cursor = "crosshair";
    const cancel = el("button", "sm", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", endMove);
    next.addEventListener("click", () => { const ll = m.marker.getLngLat(); editor(null, [+ll.lng.toFixed(7), +ll.lat.toFixed(7)]); });
    bar.append(cancel, next);
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && st.move && !UI.dialog.isOpen()) endMove(); });

  // ---- a call: its nearest hydrants beside its map --------------------------------------------------------------------------
  function card(inc) {
    const box = $("#d-hydrants");
    if (!box) return;
    const d = inc && inc.hydrants;
    const near = (d && d.near) || [], down = (d && d.down) || [];
    const sig = JSON.stringify([inc && inc.id, near, down]);
    show(box, near.length > 0 || down.length > 0);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    const head = el("div", "pp-head");
    head.append(el("span", "pp-title", "Hydrants"), el("span", "spacer"));
    if (down.length) {
      const c = el("span", "chip warn", `${down.length} OOS nearby`);
      c.title = down.map(line).join("\n");
      head.append(c);
    }
    const list = el("ol", "pp-list");
    const shown = [...near, ...down.filter((x) => !near.some((n) => n.key === x.key))];
    for (const h of shown) {
      const li = el("li", "pp-row" + (DOWN.includes(h.status) ? " hy-down" : ""));
      const go = el("button", "pp-go", line(h));
      go.type = "button";
      go.prepend(mark(h.cls, h.status));
      go.title = h.cls ? `Class ${h.cls}: ${colorName(h.cls).toLowerCase()} cap` : "No flow data";
      go.addEventListener("click", () => {
        const gm = window.GisLayers && [...GisLayers.maps].find((x) => x.map.getContainer().closest("#d-map"));
        if (!gm) return;
        gm.map.easeTo({ center: [h.lon, h.lat], zoom: Math.max(gm.map.getZoom(), 17.5), duration: 400 });
        showCard(gm, { _k: h.key, _no: h.number, _cls: h.cls, _gpm: h.gpm, _st: h.status !== "unknown" ? h.status : undefined });
      });
      li.append(go);
      list.append(li);
    }
    box.replaceChildren(head, list);
  }

  // ---- Settings > Hydrants (admins) ---------------------------------------------------------------------------------------
  const S = { list: null };
  function mount(host) {
    if (!S.list || S.list.host !== host) {
      S.list = { host, q: "", flag: "", town: "", offset: 0, limit: 100, data: null, seq: 0, err: "", imp: null };
      draw();
    }
    loadList();
  }
  async function loadList() {
    const s = S.list;
    if (!s || !s.host.isConnected) return;
    const seq = ++s.seq;
    const q = new URLSearchParams({ offset: String(s.offset), limit: String(s.limit) });
    for (const k of ["q", "flag", "town"]) if (s[k]) q.set(k, s[k]);
    try {
      const d = await api("GET", `/api/cad/hydrants?${q}`);
      if (seq !== s.seq) return;
      s.data = d; s.err = "";
    } catch (e) { if (seq === s.seq) { s.data = null; s.err = e.message; } }
    draw();
  }
  function draw() {
    const s = S.list, d = s.data;
    const head = el("div", "s-head view-head");
    head.append(el("h2", "view-title", "Hydrants"), el("span", "spacer"));
    const exp = el("a", "btn sm ro-ok", "Export CSV");
    const q = new URLSearchParams();
    for (const k of ["q", "flag", "town"]) if (s[k]) q.set(k, s[k]);
    exp.href = `/api/cad/hydrants.csv?${q}`;
    exp.title = "Every overlay column, keyed by the hydrant key: edit and import back";
    const file = el("input");
    file.type = "file";
    file.accept = ".csv,text/csv";
    file.hidden = true;
    file.addEventListener("change", () => { if (file.files[0]) importFile(file.files[0]); file.value = ""; });
    const imp = el("button", "sm", "Import CSV");
    imp.type = "button";
    imp.title = "Columns by name (key required); only the columns the file has change; a preview first";
    imp.addEventListener("click", () => file.click());
    head.append(exp, imp, file);
    const bar = el("div", "row hy-filters");
    const search = el("input");
    Object.assign(search, { type: "search", placeholder: "Number, street, asset id", value: s.q });
    search.setAttribute("aria-label", "Search hydrants");
    let t = null;
    search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { s.q = search.value.trim(); s.offset = 0; loadList(); }, 250); });
    const town = el("select", "ro-ok");
    town.setAttribute("aria-label", "Town");
    town.append(new Option("Every town", ""));
    for (const x of (d && d.towns) || []) town.append(new Option(x, x));
    town.value = s.town;
    town.addEventListener("change", () => { s.town = town.value; s.offset = 0; loadList(); });
    bar.append(search, town);
    const chips = el("div", "seg hy-flags");
    chips.setAttribute("role", "group");
    chips.setAttribute("aria-label", "Show");
    const counts = (d && d.counts) || {};
    for (const [k, lab] of FLAGS) {
      const n = { "": counts.all, oos: counts.oos, missing: counts.missing, removed: counts.removed, hub: counts.hub, edited: counts.edited }[k];
      const b = el("button", "sm ro-ok", n != null ? `${lab} ${n}` : lab);
      b.type = "button";
      b.setAttribute("aria-pressed", String(s.flag === k));
      if (s.flag === k) b.classList.add("active");
      b.addEventListener("click", () => { s.flag = k; s.offset = 0; loadList(); });
      chips.append(b);
    }
    const body = el("div", "hy-body");
    if (s.imp) body.append(importView());
    if (!d) body.append(el("p", s.err ? "error" : "dim", s.err || "Loading"));
    else if (!d.county) body.append(el("p", "dim", "No county hydrant layer on this CAD"));
    if (d) {
      const tbl = el("table", "evtable hy-table");
      const th = el("tr");
      for (const c of ["No.", "Town", "Status", "Class", "Location", "Asset", "Changed"]) th.append(el("th", "", c));
      const thead = el("thead");
      thead.append(th);
      const tb = el("tbody");
      for (const h of d.rows) {
        const tr = el("tr", "hy-tr" + (h.removed ? " dim" : ""));
        tr.tabIndex = 0;
        const no = el("td");
        no.append(mark(h.cls, h.status), ` ${h.number || "?"}`);
        if (h.missing) no.append(el("span", "chip warn", "missing"));
        if (h.hub) no.append(el("span", "chip", "added"));
        if (h.removed) no.append(el("span", "chip", "removed"));
        tr.append(no, el("td", "", h.town || ""), el("td", DOWN.includes(h.status) ? "err" : h.status === "restricted" ? "warn" : "",
          h.status === "unknown" ? "" : STATUS[h.status] + (h.status_reason ? `: ${h.status_reason}` : "")),
          el("td", "", h.cls ? `${h.cls}${h.gpm != null ? ` · ${h.gpm}` : ""}` : ""), el("td", "", h.location || ""),
          el("td", "mono", h.asset_id || ""), el("td", "dim small", h.by ? `${h.by} ${when(h.updated_at)}` : ""));
        tr.title = `${h.key}${h.district ? ` · ${h.district}` : ""}`;
        const open = () => editor(h);
        tr.addEventListener("click", open);
        tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
        tb.append(tr);
      }
      tbl.append(thead, tb);
      body.append(tbl);
      const more = el("div", "row small");
      more.append(el("span", "dim", d.total ? `${s.offset + 1}–${s.offset + d.rows.length} of ${d.total}` : "None"), el("span", "spacer"));
      if (s.offset > 0) { const p = el("button", "sm ro-ok", "Previous"); p.type = "button"; p.addEventListener("click", () => { s.offset = Math.max(0, s.offset - s.limit); loadList(); }); more.append(p); }
      if (s.offset + d.rows.length < d.total) { const n = el("button", "sm ro-ok", "Next"); n.type = "button"; n.addEventListener("click", () => { s.offset += s.limit; loadList(); }); more.append(n); }
      body.append(more);
    }
    const had = s.host.contains(document.activeElement) && document.activeElement.type === "search";
    s.host.replaceChildren(head, bar, chips, body);
    if (had) { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }
  }
  async function importFile(f) {
    const s = S.list;
    if (f.size > 4 * 1024 * 1024) { UI.toast("CSV over 4 MB", "err"); return; }
    const text = await f.text();
    try {
      const pv = await api("POST", "/api/cad/hydrants/import", { action: "preview", csv: text, operator: operator() });
      s.imp = { name: f.name, text, pv };
    } catch (e) { s.imp = { name: f.name, text, err: e.message }; }
    draw();
  }
  function importView() {
    const s = S.list, im = s.imp;
    const card = el("section", "card hy-import");
    const head = el("div", "card-head");
    head.append(el("h2", "", `Import ${im.name}`), el("span", "spacer"));
    card.append(head);
    const close = el("button", "sm", "Cancel");
    close.type = "button";
    close.addEventListener("click", () => { s.imp = null; draw(); });
    if (im.err) { card.append(el("p", "error", im.err)); head.append(close); return card; }
    const pv = im.pv;
    card.append(el("p", "small", `${pv.rows} rows · ${pv.changed} changed · ${pv.new} new · ${pv.unchanged} unchanged`
      + (pv.error_count ? ` · ${pv.error_count} in error` : "") + (pv.ignored.length ? ` · ignored: ${pv.ignored.join(", ")}` : "")
      + ((pv.read_only || []).length ? ` · not imported (read-only): ${pv.read_only.join(", ")}` : "")));
    if (pv.errors.length) {
      const ol = el("ol", "hy-errs small");
      for (const e of pv.errors) ol.append(el("li", "error", `Line ${e.line}${e.key ? ` (${e.key})` : ""}: ${e.error}`));
      card.append(ol);
    }
    if (pv.preview.length) {
      const ol = el("ol", "hy-diff small");
      for (const c of pv.preview) {
        ol.append(el("li", "", `${c.key}${c.number ? ` ${c.number}` : ""}: ` + Object.entries(c.changes).map(([k, [a, b]]) => `${k} ${fmtVal(a)} → ${fmtVal(b)}`).join(" · ")));
      }
      card.append(ol);
    }
    const apply = el("button", "primary sm", `Apply ${pv.changed + pv.new}`);
    apply.type = "button";
    apply.disabled = !!pv.error_count || !(pv.changed + pv.new);
    apply.title = pv.error_count ? "Fix the rows in error first: nothing is applied while any is" : "";
    apply.addEventListener("click", async () => {
      try {
        const out = await api("POST", "/api/cad/hydrants/import", { action: "apply", csv: im.text, operator: operator() });
        UI.toast(`Imported: ${out.changed} changed, ${out.new} new`);
        s.imp = null;
        changed();
      } catch (e) { im.err = e.message; draw(); }
    });
    head.append(close, apply);
    return card;
  }

  // a write anywhere (the state's generation): the maps' layer and the list again
  if (window.cad && cad.hooks) cad.hooks.push(() => {
    const g = cad.state && cad.state.hydrants && cad.state.hydrants.gen;
    if (g == null || g === st.gen) return;
    const first = st.gen === null;
    st.gen = g;
    if (!first && Date.now() - (st.changedAt || 0) > 5000) changed();    // (not again for this page's own write)
  });

  return { ...api_, addImages, legend, show: showCard, card, mount, editor };
})();
