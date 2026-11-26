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
//   Settings     Settings > Hydrants (admins): search and filters, the list, CSV export and import (preview, then apply),
//                "Untested hydrants: assume Class C" (#840).
//   Assumed C    (#840, on by default) a hydrant with no class (no flow test, no admin's class) is drawn red like Class C,
//                always marked: a star badge on its mark, "C*" by its number, "C (assumed)" on its card and in the legend,
//                "C*" in a call's list and in search (the layer's _asm, the API's cls_assumed). Off: grey, "no flow".
//   Show on map  (#840) a row of Settings > Hydrants, a search result, a call's nearest: the large map (cadmap.js's
//                Expand overlay) on the hydrant, its card open, the Hydrants layer on for that view; #map/hydrant/<key>
//                (reload, share in the console). Closing it goes back to where it came from.
//   Search       (#840) the Search view's Hydrants tab (search-view.js; GET /api/cad/hydrants/search): resultRow here.
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
  const ASSUMED = "C";                         // sizeup/cad/hydrants.py ASSUMED: an untested hydrant's class when assumed
  const ASM_TIP = "No flow test: assumed Class C until tested (Settings > Hydrants)";
  const LAYER_KEY = "gis:hydrants";
  const FOCUS_ZOOM = 18;
  // #map/hydrant/<key> (sizeup/cad/hydrants.py KEY_RE): the large map on one hydrant
  const ROUTE = /^#map\/hydrant\/([ch][1-9]\d{0,9}(?:-[1-9]\d{0,3})?)$/;
  const routeKey = (hash) => { const m = ROUTE.exec(hash || ""); return m ? m[1] : null; };
  const routeOf = (key) => `#map/hydrant/${key}`;

  // ---- pure helpers (tests/test_hydrants.py runs these in node) ------------------------------------------------------
  const classOf = (gpm) => (gpm === null || gpm === undefined ? null : CLASSES.find((c) => gpm >= c[1])[0]);
  function ratedGpm(flow, s, r) {
    if (flow === null || flow === undefined) return null;
    if (s != null && r != null && s > 20 && s > r && r >= 0) return Math.round(flow * ((s - 20) / (s - r)) ** 0.54);
    return Math.round(flow);
  }
  const hex = (cls) => (CLASSES.find((c) => c[0] === cls) || [0, 0, 0, UNK])[3];
  const colorName = (cls) => (CLASSES.find((c) => c[0] === cls) || [0, 0, ""])[2];
  // a hydrant's class as shown (#840): tested (or an admin's) as it is, assumed with its mark; "" for none.
  // long: the card's and the legend's "C (assumed)"; else "C*" (a call's list, search, the map's label)
  function clsText(h, long) {
    if (h.cls) return h.cls;
    if (h.cls_assumed) return long ? `${h.cls_assumed} (assumed)` : `${h.cls_assumed}*`;
    return "";
  }
  const clsTip = (h) => (h.cls ? `Class ${h.cls}: ${colorName(h.cls).toLowerCase()} cap` : h.cls_assumed ? `Class ${h.cls_assumed} (assumed): ${ASM_TIP}` : "No flow data");
  // the map's image for a hydrant's properties (the same rule as iconExpr)
  function imageFor(p) {
    if (DOWN.includes(p._st)) return "hy-down";
    const base = CLASSES.some((c) => c[0] === p._cls) ? `hy-${p._cls}` : p._asm ? `hy-${ASSUMED}x` : "hy-unk";
    return p._st === "restricted" ? `${base}-r` : base;
  }
  function iconExpr() {
    const st = ["coalesce", ["get", "_st"], ""], cls = ["coalesce", ["get", "_cls"], ""];
    const by = (sfx) => ["match", cls, ...CLASSES.flatMap((c) => [c[0], `hy-${c[0]}${sfx}`]),
                         ["case", ["has", "_asm"], `hy-${ASSUMED}x${sfx}`, `hy-unk${sfx}`]];
    return ["case", ["in", st, ["literal", DOWN]], "hy-down", ["==", st, "restricted"], by("-r"), by("")];
  }
  // out of service first, then restricted: placed before (never thinned out by) their neighbours
  const sortExpr = () => ["case", ["in", ["coalesce", ["get", "_st"], ""], ["literal", DOWN]], 0, ["==", ["get", "_st"], "restricted"], 1, 2];
  // "9001 · A", "9002 · OOS", "9004 · C*" (assumed, #840) beside the mark from the label zoom
  function labelExpr() {
    const no = ["to-string", ["coalesce", ["get", "_no"], ["get", "HYD_NUMBER"], ""]];
    return ["concat", no,
      ["case", ["in", ["coalesce", ["get", "_st"], ""], ["literal", DOWN]], " · OOS", ["has", "_cls"], ["concat", " · ", ["get", "_cls"]],
       ["has", "_asm"], ["concat", " · ", ["get", "_asm"], "*"], ""]];
  }
  // the legend's rows: with the setting on, every untested hydrant is an assumed C (no grey "no flow" left to show)
  const legendRows = (assume) => LEGEND.map((r) => (r[0] === "unk" && assume ? ["Cx", "assumed (untested)"] : r));
  const fmtDist = (m) => `${Math.round(m)} m`;
  // the flow test's checks (sizeup/cad/hydrants.py check: the hub refuses the errors too): {error, notes}
  const FLOW_HIGH = 2500, RATED_MAX = 10000;
  function flowCheck(flow, s, r) {
    const notes = [];
    if (s != null && r != null) {
      if (r >= s) return { error: "Residual must be under the static pressure", notes };
      if (r < 20 && s > 20 && flow != null) notes.push("Residual under 20 psi: rated flow extrapolated");
    } else if (flow != null && (s != null || r != null)) notes.push("One pressure only: class from the observed flow");
    if (flow != null) {
      if (flow > FLOW_HIGH) notes.push(`${flow} gpm is unusually high: check`);
      const rated = ratedGpm(flow, s, r);
      if (rated > RATED_MAX) return { error: `Rates ${rated} gpm at 20 psi (over ${RATED_MAX}): check the flow and pressures`, notes };
    }
    return { error: "", notes };
  }
  // a call's row: "9001 · A 1200 gpm · OOS · 40 m", "9004 · C* · 60 m" (assumed)
  function line(h) {
    const cls = h.cls ? `${h.cls}${h.gpm != null ? ` ${h.gpm} gpm` : ""}` : clsText(h);
    return [h.number || "?", cls, SHORT[h.status] || "", h.dist_m != null ? fmtDist(h.dist_m) : ""].filter(Boolean).join(" · ");
  }
  // the "@words" being typed at the caret of the Location box (the comment box's rule, cad.js placeTokenAt): {text, start, end}
  function atToken(value, pos, taken = []) {
    const before = value.slice(0, pos), at = before.lastIndexOf("@");
    if (at < 0 || (at > 0 && !/\s/.test(before[at - 1]))) return null;
    const text = before.slice(at + 1);
    if (!text.trim() || /^\s/.test(text) || text.length > 60 || /[@\n]/.test(text)) return null;
    if (taken.some((p) => text.toLowerCase().startsWith(p.label.toLowerCase()))) return null;
    return { text, start: at, end: pos };
  }
  // a picked place into the text: "IFO @sam" -> "IFO @Sample Market ", and the refs still named in it
  function takeAt(value, tok, p, places) {
    const after = value.slice(tok.end).replace(/^[A-Za-z0-9.-]*/, "");
    const lead = value.slice(0, tok.start) + "@" + p.label + (after.startsWith(" ") ? "" : " ");
    const ref = { label: p.label, lat: p.lat, lon: p.lon, kind: p.kind || "place", sub: p.sub || "", source: p.source || "",
                  ...(p.key ? { key: p.key } : {}) };                    // a hydrant's key (#840): its chip opens its card
    const next = [...places.filter((x) => x.label !== p.label), ref];
    return { value: lead + after, caret: lead.length, places: next };
  }
  const named = (text, places) => places.filter((p) => text.toLowerCase().includes(`@${p.label.toLowerCase()}`));
  const api_ = { CLASSES, DOWN, LEGEND, ASSUMED, classOf, ratedGpm, flowCheck, atToken, takeAt, named, imageFor, iconExpr, sortExpr,
                 labelExpr, legendRows, clsText, clsTip, fmtDist, line, routeKey, routeOf };
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
  // assumed (#840): a light star on a dark badge at the top left (the restricted dot is top right)
  const star = (g) => {
    g.fillStyle = DARK; g.beginPath(); g.arc(4.5, 4.5, 4.4, 0, 2 * Math.PI); g.fill();
    g.strokeStyle = LIGHT; g.lineWidth = 1.4; g.beginPath();
    for (let i = 0; i < 3; i++) {
      const a = Math.PI / 2 + i * Math.PI / 3, dx = 2.9 * Math.cos(a), dy = 2.9 * Math.sin(a);
      g.moveTo(4.5 - dx, 4.5 - dy); g.lineTo(4.5 + dx, 4.5 + dy);
    }
    g.stroke();
  };
  // gislayers.js: the images on one map (once): hy-AA ... hy-C, hy-unk, hy-Cx (assumed), each -r (restricted), hy-down
  function addImages(map, shape) {
    if (!shape || map.hasImage(`hy-${ASSUMED}x`)) return;
    const add = (id, img) => { if (!map.hasImage(id)) map.addImage(id, img, { pixelRatio: 2 }); };
    for (const [cls, col] of [...CLASSES.map((c) => [c[0], c[3]]), ["unk", UNK]]) {
      add(`hy-${cls}`, paint(shape, col));
      add(`hy-${cls}-r`, paint(shape, col, dot));
    }
    add(`hy-${ASSUMED}x`, paint(shape, hex(ASSUMED), star));
    add(`hy-${ASSUMED}x-r`, paint(shape, hex(ASSUMED), (g) => { star(g); dot(g); }));
    add("hy-down", paint(shape, null, cross));
  }

  // a small mark in the page: the class's colour with its letter (the legend, the card, a call's list); asm: the assumed
  // class (#840) when there is none, "C*" with a dashed edge
  function mark(cls, st, asm) {
    const down = DOWN.includes(st), a = !cls && !!asm;
    const m = el("span", "hy-mark" + (down ? " down" : a ? " asm" : ""), down ? "✕" : cls || (a ? `${asm}*` : "?"));
    if (!down) m.style.setProperty("--hy", cls || a ? hex(cls || asm) : UNK);
    m.setAttribute("aria-hidden", "true");
    return m;
  }
  // (cad.js's cad is a top-level const, not a property of the window)
  const CAD = () => (typeof cad !== "undefined" ? cad : null);
  const assumeOn = () => !(CAD() && cad.state && cad.state.hydrants && cad.state.hydrants.assume_c === false);

  // ---- the legend under the layer's toggle (gislayers.js panel) -----------------------------------------------------------
  function legend(gm) {
    const box = el("div", "hy-legend small");
    for (const [k, tip] of legendRows(assumeOn())) {
      const s = el("span", "hy-key");
      s.append(k === "down" ? mark(null, "oos") : k === "Cx" ? mark(null, null, ASSUMED) : mark(k === "unk" ? null : k),
               el("span", "", k === "unk" ? "no flow" : k === "down" ? "OOS" : k === "Cx" ? tip : ""));
      s.title = k === "unk" || k === "down" ? tip : k === "Cx" ? `${ASSUMED} (assumed): ${ASM_TIP}` : `Class ${k}: ${tip}`;
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

  // the Location text with each picked "@place" a chip: a click shows that place on this map (never the hydrant moved)
  function locationEl(h, gm) {
    const box = el("div", "hy-loc");
    if (h.location_qualifier && typeof qualText === "function") {   // "IFO": where at it (its name on hover)
      const q = el("span", "hy-qual", qualText(h.location_qualifier, ""));
      q.title = qualTitle(h.location_qualifier, "");
      box.append(q, " ");
    }
    const ps = named(h.location, h.location_places || []).sort((a, b) => b.label.length - a.label.length);
    let rest = h.location;
    while (rest) {
      let best = null;
      for (const p of ps) {
        const i = rest.toLowerCase().indexOf(`@${p.label.toLowerCase()}`);
        if (i >= 0 && (!best || i < best.i)) best = { i, p };
      }
      if (!best) { box.append(rest); break; }
      if (best.i) box.append(rest.slice(0, best.i));
      const b = el("button", "chip place-tag");
      b.type = "button";
      if (window.Icon) b.append(Icon("locate"));
      b.append(best.p.label);
      b.title = `${best.p.label}${best.p.sub ? `, ${best.p.sub}` : ""}: show it on the map`;
      const p = best.p;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if (p.kind === "hydrant" && p.key) { if (gm) focusOn(gm, p); else showOnMap(p.key); return; }   // #840
        if (gm) gm.map.easeTo({ center: [p.lon, p.lat], zoom: Math.max(gm.map.getZoom(), 18), duration: 400 });
      });
      box.append(b);
      rest = rest.slice(best.i + best.p.label.length + 1);
    }
    return box;
  }

  function lines(h, gm) {
    const out = [];
    const cl = el("div", "hy-line");
    cl.append(mark(h.cls, h.status, h.cls_assumed));
    if (h.cls) {
      const t = el("span", "", `Class ${h.cls}${h.gpm != null ? ` · ${h.gpm} gpm` : ""}`);
      t.title = `${colorName(h.cls)} cap (${SRC_NOTE})` + (h.class_set ? ` · set by hand (flow says ${h.class_flow || "none"})` : "")
        + (h.static_psi != null && h.residual_psi != null && h.flow_gpm != null ? ` · ${h.flow_gpm} gpm flowed at ${h.residual_psi} psi residual` : "");
      cl.append(t);
    } else if (h.cls_assumed) {                                          // #840: "C (assumed)", never a tested class
      const t = el("span", "", `${clsText(h, true)} · untested`);
      t.title = ASM_TIP;
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
    if (h.location) out.push(locationEl(h, gm));                       // "IFO @Sample Market": a chip per place
    if (h.district) out.push(h.district);                              // (the town is the card's subtitle)
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
  // got: the card's GET already made (Show on map)
  function showCard(gm, props, got) {
    const key = props._k;
    const quick = { key, number: props._no || props.HYD_NUMBER || "", cls: props._cls || null, gpm: props._gpm ?? null,
                    cls_assumed: props._cls ? null : props._asm || null,
                    status: props._st || "unknown", missing: !!props._miss, hub: !!props._hub, district: props.FIRENAME || "" };
    const draw = (h, more) => {
      const ls = lines(h, gm);
      if (more) ls.push(...more);
      gm.control.close();
      gm.control.showInfo(`Hydrant ${h.number || "?"}`, h.town || (h.hub ? "Added here" : ""), ls,
                          ["Westchester County GIS", h.by ? `${h.by} ${when(h.updated_at)}` : ""].filter(Boolean).join(" · "), "hy-card",
                          null, SRC_NOTE);
    };
    draw(quick);
    if (!key) return;
    const seq = ++st.seq;
    (got ? Promise.resolve(got) : api("GET", `/api/cad/hydrants/${encodeURIComponent(key)}`)).then((d) => {
      if (seq !== st.seq) return;
      draw(d.hydrant, [...notifRows(d.notifications), ...adminRow(gm, d.hydrant),
                       ...(d.history && d.history.length ? [historyEl(d.history)] : [])]);
    }).catch((e) => { if (seq === st.seq) draw(quick, [el("div", "error", e.message)]); });
  }
  // the notifications holding it out of service (sizeup/cad/notif.py): a click opens that one
  function notifRows(list) {
    return (list || []).map((n) => {
      const row = el("div", "row hy-notif");
      const b = el("button", "sm quiet ro-ok", `Notification ${n.number || n.id}`);
      b.type = "button";
      b.title = `Holds it OOS${n.status === "closed" ? " (closed)" : ""}: open it`;
      b.addEventListener("click", () => { if (typeof selectIncident === "function") selectIncident(n.id); });
      row.append(el("span", "dim small", "Held by"), b);
      return row;
    });
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
    ["flow_gpm", "Flow (gpm)", "number", [1, 5000, 1]], ["flow_date", "Flow test", "date"],
    ["static_psi", "Static psi", "number", [1, 300, 0.1]], ["residual_psi", "Residual psi", "number", [0, 300, 0.1]],
    ["nfpa_class", "Class", "select", null], ["thread", "Thread", "select", THREADS], ["outlets", "Outlets", "text", 60],
    ["main_in", "Main (in)", "number", [2, 24, 0.5]], ["owner", "Owner", "select", OWNERS], ["body_color", "Body colour", "text", 30],
    ["asset_id", "Asset id", "text", 40], ["location", "Location", "text", 160], ["inspected", "Inspected", "date"],
    ["notes", "Notes", "textarea", 500],
  ];
  const TIPS = { flow_gpm: "Flow as tested (with both pressures: the class uses the flow at 20 psi residual), or the rated capacity",
                 outlets: "e.g. 2x2.5, 1x4.5", number: "Empty: the county's", nfpa_class: "Empty: from the flow (NFPA 291)",
                 location: "Street, side, landmark", status_until: "Expected back in service" };

  // The Location box: "@" finds a common place, a business or an address (GET /api/cad/places, nearest the hydrant first:
  // the comment box's search); a picked one is written "@<label>" and kept with the text as a reference. Never the
  // hydrant's point. Empty: the nearest address or place as the placeholder, "Use" fills it in.
  function locationBox(lab, inp, h, at) {
    const wrap = el("div", "hy-locbox");
    // [qualifier ▾] in front (Settings > Lists > Location qualifiers): "IFO" + "@Sample Market"; typed "IFO " goes in it
    const qs = typeof qualSelect === "function" ? qualSelect(el("select", "loc-qual"), h.location_qualifier || "") : null;
    if (qs) {
      qs.setAttribute("aria-label", "Where at the location");
      qs.title = "Where at the location";
      inp.hyQual = qs;
      qualTyped(inp, qs);
      wrap.append(qs);
    }
    const list = el("ol", "sugg note-sugg");
    list.hidden = true;
    list.setAttribute("role", "listbox");
    inp.setAttribute("role", "combobox");
    inp.setAttribute("aria-autocomplete", "list");
    const pt = at ? [at[1], at[0]] : h.lat != null ? [h.lat, h.lon] : null;
    const L = { places: [...(h.location_places || [])], items: [], idx: 0, tok: null, seq: 0, timer: null };
    inp.hyPlaces = () => named(inp.value, L.places);
    const close = () => { L.items = []; list.hidden = true; inp.setAttribute("aria-expanded", "false"); };
    const take = (p) => {
      const t = takeAt(inp.value, L.tok, p, L.places);
      inp.value = t.value;
      L.places = t.places;
      inp.setSelectionRange(t.caret, t.caret);
      close();
    };
    const draw = () => {
      if (!L.items.length) return close();
      list.replaceChildren(...L.items.map((p, i) => {
        const li = el("li", i === L.idx ? "on" : "");
        li.setAttribute("role", "option");
        li.append(...(typeof suggestionHead === "function" ? suggestionHead(p) : [el("b", "", p.label)]));
        if (p.sub) li.append(el("span", "town", p.sub));
        li.addEventListener("mousedown", (e) => { e.preventDefault(); take(p); });
        return li;
      }), el("li", "hint dim small", "Tab take · Esc close"));
      list.hidden = false;
      inp.setAttribute("aria-expanded", "true");
    };
    inp.addEventListener("input", () => {
      L.tok = atToken(inp.value, inp.selectionStart ?? inp.value.length, named(inp.value, L.places));
      clearTimeout(L.timer);
      if (!L.tok || L.tok.text.trim().length < 2) return close();
      const q = L.tok.text.trim(), my = ++L.seq;
      L.timer = setTimeout(async () => {
        try {
          const r = await api("GET", `/api/cad/places?q=${encodeURIComponent(q)}&limit=6${pt ? `&near=${pt[0]},${pt[1]}` : ""}`);
          if (my !== L.seq) return;
          L.items = (r.places || []).filter((p) => p.lat != null && p.lon != null);
          L.idx = 0;
          draw();
        } catch (_) { close(); }
      }, 180);
    });
    inp.addEventListener("keydown", (e) => {
      if (!L.items.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); L.idx = (L.idx + (e.key === "ArrowDown" ? 1 : -1) + L.items.length) % L.items.length; draw(); }
      else if (e.key === "Tab" || e.key === "Enter") { e.preventDefault(); take(L.items[L.idx]); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    });
    inp.addEventListener("blur", () => setTimeout(close, 150));
    wrap.append(inp, list);
    if (!inp.value && pt) {                    // the nearest address or place: a suggestion only
      api("GET", `/api/cad/geocode/here?lat=${pt[0]}&lon=${pt[1]}`).then((d) => {
        const p = (d && (d.address || d.place)) || null;
        if (!p || !p.label || inp.value) return;
        inp.placeholder = `near ${p.label}`;
        const use = el("button", "sm quiet", "Use");
        use.type = "button";
        use.title = `Fill in "near @${p.label}"`;
        use.addEventListener("click", () => {
          const near = qs && [...qs.options].some((o) => o.value === "NEAR") && !qs.value;   // the list's Near, else the words
          if (near) qs.value = "NEAR";
          inp.value = near ? "" : "near ";
          L.tok = { text: "", start: inp.value.length, end: inp.value.length };
          take({ ...p, lat: p.lat, lon: p.lon });
          use.remove();
        });
        wrap.append(use);
      }).catch(() => { /* no geocoder here: no suggestion */ });
    }
    lab.append(wrap);
    return inp;
  }

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
      if (kind === "number" && inp.value !== "" && (Number(inp.value) < Number(inp.min) || Number(inp.value) > Number(inp.max))) {
        inp.removeAttribute("min"); inp.removeAttribute("max");      // saved before today's limits: the hub allows it untouched
      }
      if (k === "number" && !isNew && !h.hub && !(h.number && h.number !== h.county_number)) inp.value = "";
      if (k === "number" && !h.hub) inp.placeholder = h.county_number || "";
      if (TIPS[k]) lab.title = TIPS[k];
      if (k === "location") locationBox(lab, inp, h, at);
      else lab.append(inp);
      inputs[k] = inp;
      form.append(lab);
    }
    const cls = el("div", "hy-line hy-cls small");
    const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    for (const k of ["flow_date", "inspected"]) inputs[k].max = today;          // never in the future (the hub checks too)
    let fc = { error: "", notes: [] };
    const showCls = () => {
      const n = (x) => (inputs[x].value === "" ? null : Number(inputs[x].value));
      const s = n("static_psi"), r = n("residual_psi"), flow = n("flow_gpm");
      fc = flowCheck(flow, s, r);
      const g = s != null && r != null ? ratedGpm(flow, s, r) : ratedGpm(flow, null, null);
      const c = inputs.nfpa_class.value || classOf(g), asm = !c && assumeOn() ? ASSUMED : null;
      const parts = [mark(c, inputs.status.value, asm),
                     el("span", "", c ? `Class ${c}${g != null ? ` · ${g} gpm rated` : ""}` : asm ? `${asm} (assumed) · no flow` : "No class (no flow)")];
      if (fc.error) parts.push(el("span", "error", fc.error));
      for (const t of fc.notes) parts.push(el("span", "warn", t));
      cls.replaceChildren(...parts);
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
      showCls();
      const flowEdited = ["flow_gpm", "static_psi", "residual_psi"].some((k) => inputs[k].value.trim() !== String(h[k] ?? ""));
      if (fc.error && flowEdited) { setText(err, fc.error); return; }     // (saved before the rule, untouched: the hub allows it)
      const fl = Number(inputs.flow_gpm.value);
      if (inputs.flow_gpm.value !== "" && fl > FLOW_HIGH && String(fl) !== String(h.flow_gpm ?? "")
          && !(await UI.confirm(`${fl} gpm is unusually high for one hydrant. Save it anyway?`, { ok: "Save", danger: false }))) return;
      if (inputs.status_since.value && inputs.status_until.value && inputs.status_until.value < inputs.status_since.value
          && (inputs.status_since.value !== (h.status_since || "") || inputs.status_until.value !== (h.status_until || ""))) {
        setText(err, "Expected back is before Since"); return;
      }
      const body = { operator: operator() };
      const q = inputs.location.hyQual ? inputs.location.hyQual.value : (h.location_qualifier || "");
      if (q !== (h.location_qualifier || "")) body.location_qualifier = q;           // never the hydrant's point
      const lp = inputs.location.hyPlaces ? inputs.location.hyPlaces() : [];
      if (JSON.stringify(lp) !== JSON.stringify(named(inputs.location.value, h.location_places || []))
          || inputs.location.value.trim() !== (h.location || "")) body.location_places = lp;
      for (const [k, , kind] of FORM) {
        const v = inputs[k].value.trim();
        const was = h[k] === null || h[k] === undefined ? (k === "status" ? "unknown" : "") : String(h[k]);
        const cmp = k === "number" && !isNew && !h.hub ? (h.number !== h.county_number ? h.number : "") : was;
        if (isNew ? v !== "" && !(k === "status" && v === "unknown") : v !== cmp) body[k] = kind === "number" && v !== "" ? Number(v) : v;
      }
      try {
        let out = null;
        if (isNew) {
          out = await api("POST", "/api/cad/hydrants", { ...body, lat: at[1], lon: at[0] });
          UI.toast("Hydrant added", "ok", ((out && out.warnings) || []).join(" · "));
        } else if (Object.keys(body).length > 1) {
          out = await api("POST", `/api/cad/hydrants/${encodeURIComponent(h.key)}`, body);
          UI.toast(`Hydrant ${h.number || h.key} saved`, "ok", ((out && out.warnings) || []).join(" · "));
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
  function place(gm, preset) {
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
    next.addEventListener("click", () => { const ll = m.marker.getLngLat(); editor(preset || null, [+ll.lng.toFixed(7), +ll.lat.toFixed(7)]); });
    bar.append(cancel, next);
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && st.move && !UI.dialog.isOpen()) endMove(); });

  // ---- Show on map (#840): the large map on one hydrant, its card open, the layer on for that view ------------------------
  // a hydrant on a map already on screen (the call's): centred, its card (h: a row, a place: key, number, cls ...)
  async function focusOn(gm, h) {
    let got = null, x = h.hydrant || h;                                // an "@" suggestion: its hydrant's facts
    if (x.number === undefined) {                                       // a note's saved place: the key only; where it is now
      try { got = await api("GET", `/api/cad/hydrants/${encodeURIComponent(h.key)}`); x = got.hydrant; }
      catch (e) { UI.toast(`Hydrant: ${e.message}`, "err"); return; }
    }
    const at = got ? [x.lon, x.lat] : [h.lon, h.lat];
    gm.map.easeTo({ center: at, zoom: Math.max(gm.map.getZoom(), 17.5), duration: 400 });
    showCard(gm, { _k: h.key, _no: x.number, _cls: x.cls || undefined, _asm: x.cls ? undefined : x.cls_assumed || undefined,
                   _gpm: x.gpm, _st: x.status && x.status !== "unknown" ? x.status : undefined }, got);
  }
  // refocus: the key whose button is focused again once its list is drawn again after the map closes (keyboard users)
  const R = { key: null, closing: false, seq: 0, ring: null, gm: null, booted: false, refocus: null };
  function showOnMap(key) {
    if (!key) return;
    if (location.hash === routeOf(key)) { route(); return; }
    R.refocus = null;
    location.hash = routeOf(key);
    history.replaceState({ hyMap: key }, "", location.href);           // ours: closing it is Back (kept on Back / Forward)
  }
  function refocus(root) {
    const k = R.refocus;
    if (!k || !root) return;
    const b = [...root.querySelectorAll("button.hy-map")].find((x) => x.dataset.key === k);
    if (b) { R.refocus = null; b.focus({ preventScroll: false }); }
  }
  // the button: "Show on map" (Settings, search); compact: an icon (a call's list)
  function mapBtn(h, compact) {
    const b = el("button", compact ? "icon quiet sm hy-map" : "sm hy-map ro-ok");
    b.type = "button";
    b.dataset.key = h.key;
    if (window.Icon) b.append(Icon("expand"));
    if (!compact) b.append("Show on map");
    const what = `hydrant ${h.number || h.key}`;
    b.title = "Show on the large map";
    b.setAttribute("aria-label", `Show ${what} on the large map`);
    b.addEventListener("click", (e) => { e.stopPropagation(); showOnMap(h.key); });
    b.addEventListener("keydown", (e) => { if (e.key === "Enter") e.stopPropagation(); });
    return b;
  }
  function unfocus() {
    if (R.ring) { R.ring.remove(); R.ring = null; }
    if (R.gm) { R.gm.control.force[LAYER_KEY] = false; try { R.gm.control.refresh(); } catch (_) { /* gone */ } R.gm = null; }
  }
  // the large map closed (Esc, Close, the scrim): out of #map/... back where it came from
  // how: {picked: true} a call picked on it (cadmap.js): that call's view next, never Back to a list over it
  function bigClosed(how) {
    unfocus();
    if (R.closing || !routeKey(location.hash)) return;
    const key = R.key;
    R.key = null;
    R.seq++;                                                           // (a card still loading: dropped)
    if (how && how.picked) { history.replaceState(null, "", location.pathname + location.search); return; }
    if (history.state && history.state.hyMap) { R.refocus = key; history.back(); return; }
    history.replaceState(null, "", location.pathname + location.search);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  }
  async function route() {
    if (BOARD_()) return;
    const key = routeKey(location.hash);
    if (!key) {
      if (R.key) { R.key = null; R.closing = true; try { if (window.CadMap) CadMap.closeBig(); } finally { R.closing = false; } }
      return;
    }
    if (!CAD() || !cad.scene || !window.CadMap) return;          // (the first state calls again: hooks below)
    R.key = key;
    const seq = ++R.seq;
    let d;
    try { d = await api("GET", `/api/cad/hydrants/${encodeURIComponent(key)}`); } catch (e) { d = { error: e.message }; }
    if (seq !== R.seq || routeKey(location.hash) !== key) return;
    const h = d && d.hydrant;
    const rm = h && h.lat != null ? CadMap.showAt([h.lon, h.lat], FOCUS_ZOOM, { onClose: bigClosed }) : null;
    if (!rm) {
      UI.toast(h ? "No map here" : `Hydrant ${key}: ${(d && d.error) || "not found"}`, "err");
      bigClosed();
      return;
    }
    unfocus();
    const ring = el("div", "hy-focus");
    ring.setAttribute("aria-hidden", "true");
    R.ring = new maplibregl.Marker({ element: ring, anchor: "center" }).setLngLat([h.lon, h.lat]).addTo(rm.map);
    const gm = window.GisLayers && [...GisLayers.maps].find((x) => x.map === rm.map);
    if (!gm) return;
    R.gm = gm;
    gm.control.force[LAYER_KEY] = true;                                // on for this view (unticking it lets go)
    gm.control.refresh();
    showCard(gm, { _k: h.key, _no: h.number, _cls: h.cls || undefined, _asm: h.cls_assumed || undefined, _gpm: h.gpm,
                   _st: h.status !== "unknown" ? h.status : undefined, _miss: h.missing, _hub: h.hub, FIRENAME: h.district }, d);
  }
  if (!BOARD_()) {
    window.addEventListener("hashchange", route);
    if (CAD() && cad.hooks) cad.hooks.push(() => {                    // a reload on the address: once, with the scene
      if (R.booted || !cad.scene) return;
      R.booted = true;
      if (!R.key && routeKey(location.hash)) route();
    });
  }

  // ---- search (#840): a result's row in the Search view's Hydrants tab (search-view.js) ------------------------------------
  function resultRow(h) {
    const tr = el("tr", "ev hy-tr");                                   // (the button is its one tab stop)
    const no = el("td", "num");
    no.append(mark(h.cls, h.status, h.cls_assumed), ` ${h.number || "?"}`);
    if (h.hub) no.append(el("span", "chip", "added"));
    if (h.missing) no.append(el("span", "chip warn", "missing"));
    const cls = el("td", "");
    const ct = clsText(h);
    if (ct) { const c = el("span", "chip hy-cls" + (h.cls ? "" : " asm"), ct + (h.cls && h.gpm != null ? ` · ${h.gpm}` : "")); c.title = clsTip(h); cls.append(c); }
    const stt = el("td", "");
    if (h.status && h.status !== "unknown") {
      const c = el("span", "chip" + (DOWN.includes(h.status) ? " err" : h.status === "restricted" ? " warn" : ""), SHORT[h.status] || STATUS[h.status]);
      if (h.status_reason) c.title = h.status_reason;
      stt.append(c);
    }
    const loc = el("td", "loc", typeof qualText === "function" ? qualText(h.location_qualifier, h.location) : h.location || "");
    if (h.asset_id) loc.append(el("span", "dim small mono", ` ${h.asset_id}`));
    const act = el("td", "act");
    act.append(mapBtn(h));
    tr.append(no, el("td", "", h.town || ""), cls, stt, loc, act);
    tr.title = [h.district, h.key].filter(Boolean).join(" · ");
    tr.addEventListener("click", () => showOnMap(h.key));
    return tr;
  }

  // ---- a call: its nearest hydrants beside its map --------------------------------------------------------------------------
  function card(inc) {
    const box = $("#d-hydrants");
    if (!box) return;
    const d = inc && inc.hydrants;
    const near = (d && d.near) || [], down = (d && d.down) || [];
    const sig = JSON.stringify([inc && inc.id, near, down, d && d.linked, inc && inc.type, inc && inc.status]);
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
    const linked = new Set((d && d.linked) || []);            // held OOS by this notification (sizeup/cad/notif.py)
    const notif = inc && inc.type === "NOTIF" && ["queued", "pending", "active"].includes(inc.status);
    for (const h of shown) {
      const li = el("li", "pp-row" + (DOWN.includes(h.status) ? " hy-down" : "") + (linked.has(h.key) ? " hy-linked" : ""));
      const go = el("button", "pp-go", line(h));
      go.type = "button";
      go.prepend(mark(h.cls, h.status, h.cls_assumed));
      go.title = clsTip(h);
      go.addEventListener("click", () => {
        const gm = window.GisLayers && [...GisLayers.maps].find((x) => x.map.getContainer().closest("#d-map"));
        if (gm) focusOn(gm, h);
      });
      li.append(go);
      if (linked.has(h.key)) {
        const c = el("span", "chip warn", "linked");
        c.title = "Held OOS by this notification";
        li.append(c);
      } else if (notif && DOWN.includes(h.status) && canDo("admin") && !BOARD_()) {
        const b = el("button", "sm quiet", "Link");
        b.type = "button";
        b.title = "Keep this notification open until it is back in service";
        b.addEventListener("click", async () => {
          try { await api("POST", `/api/cad/incident/${inc.id}/hydrants`, { action: "link", key: h.key, operator: operator() }); }
          catch (e) { UI.toast(e.message, "err"); }
        });
        li.append(b);
      }
      li.append(mapBtn(h, true));
      list.append(li);
    }
    box.replaceChildren(head, list);
    refocus(box);
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
    if (d) {                                       // #840: the admin's switch (Settings, audited as System)
      const lab = el("label", "check hy-assume");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = d.assume_c !== false;
      cb.addEventListener("change", async () => {
        try {
          await api("POST", "/api/cad/settings/hydrant_display", { action: "set", key: "assume_c", value: cb.checked, operator: operator() });
          UI.toast(cb.checked ? "Untested hydrants: assumed Class C" : "Untested hydrants: no class");
          changed();
        } catch (e) { cb.checked = !cb.checked; UI.toast(e.message, "err"); }
      });
      lab.append(cb, el("span", "", "Untested hydrants: assume Class C"));
      lab.title = `${ASSUMED} (assumed): drawn red, marked ${ASSUMED}*, until a flow test`;
      bar.append(lab);
    }
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
      const mth = el("th");
      mth.append(el("span", "sr-only", "Map"));
      th.append(mth);
      const thead = el("thead");
      thead.append(th);
      const tb = el("tbody");
      for (const h of d.rows) {
        const tr = el("tr", "hy-tr" + (h.removed ? " dim" : ""));
        tr.tabIndex = 0;
        const no = el("td");
        no.append(mark(h.cls, h.status, h.cls_assumed), ` ${h.number || "?"}`);
        if (h.missing) no.append(el("span", "chip warn", "missing"));
        if (h.hub) no.append(el("span", "chip", "added"));
        if (h.removed) no.append(el("span", "chip", "removed"));
        tr.append(no, el("td", "", h.town || ""), el("td", DOWN.includes(h.status) ? "err" : h.status === "restricted" ? "warn" : "",
          h.status === "unknown" ? "" : STATUS[h.status] + (h.status_reason ? `: ${h.status_reason}` : "")),
          Object.assign(el("td", h.cls ? "" : "dim", h.cls ? `${h.cls}${h.gpm != null ? ` · ${h.gpm}` : ""}` : clsText(h)), { title: clsTip(h) }),
          el("td", "", typeof qualText === "function" ? qualText(h.location_qualifier, h.location) : h.location || ""),
          el("td", "mono", h.asset_id || ""), el("td", "dim small", h.by ? `${h.by} ${when(h.updated_at)}` : ""));
        const mt = el("td", "act");
        if (!h.removed) mt.append(mapBtn(h));
        tr.append(mt);
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
    else if (d) refocus(s.host);                                       // back from Show on map (#840)
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
    if ((pv.warnings || []).length) {
      const ol = el("ol", "hy-errs small");
      for (const w of pv.warnings) ol.append(el("li", "warn", `Line ${w.line}${w.key ? ` (${w.key})` : ""}: ${w.warning}`));
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
  if (CAD() && cad.hooks) cad.hooks.push(() => {     // (#840: it looked on the window, so another page's write was missed)
    const g = cad.state && cad.state.hydrants && cad.state.hydrants.gen;
    if (g == null || g === st.gen) return;
    const first = st.gen === null;
    st.gen = g;
    if (!first && Date.now() - (st.changedAt || 0) > 5000) changed();    // (not again for this page's own write)
  });

  // a hydrant a notification names that the list lacks (notif.js "+ Add"): tap the map, the form with its number
  const placeNew = (gm, number) => place(gm, number ? { number } : null);
  return { ...api_, addImages, legend, show: showCard, card, mount, editor, showOnMap, mapBtn, resultRow, mark, focusOn, refocus,
           placeNew };
})();
