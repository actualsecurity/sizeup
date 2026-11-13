// Pre-plan site documents (#459, sizeup/cad/preplan_docs.py): a building's floor plans, drawn on the map of every call
// there, and its other documents (pre-plan and inspection sheets, hazmat / Tier II, contacts, photos), opened in the app.
//
//   The call     GET /api/cad/preplan/docs?incident=<id>, again only when the call's building or the write generation
//                (state preplan_docs.gen) moves. Beside the call's map (#d-docs): "Site docs (3)", a title per row
//                (and its floor); a click opens it in the viewer. A floor or site plan that has been aligned is drawn
//                on the map at once: floor 1 (or ground) first; the floor chips switch it, or Off; the opacity slider
//                is the viewer's (localStorage OPACITY_KEY, in a try).
//   The plan     an "image" source (its four corners: top left, top right, bottom right, bottom left) and a raster
//                layer, under the pre-plan points (preplan.js) and so under the route lines, rigs' lines and the
//                firehouses; the rigs and call pins are DOM marks over every map layer. Over the county layers.
//                Its picture comes from the hub itself (same origin: WebGL needs no CORS), streamed from the store:
//                /api/cad/preplan/docs/<id>/plan, behind the sign-in like every page.
//   Align        dispatchers and admins, a plan's menu: it starts on the footprint's oriented box (the smallest
//                rectangle around the county footprint; "N" its north-up box), the image's long side along the
//                box's, its top as near north as the box allows; then drag the middle to move, the round handle to
//                rotate, the square one to scale, a corner on its own; Save sends the four corners.
//   Uploads      dispatchers and admins: "+ Doc": a file (PDF, PNG, JPEG, at most 25 MB), a title, a type, a floor, a
//                PDF plan's page, Admin only (admins). The file goes as the request's body, its details in the
//                X-Sizeup-Doc header. Edit, Align, Delete from the row's menu.
//   Search       the Search page's Pre-plan tab: the building's documents under its points (preplan.js search()).
// Uses cad.js's helpers (el, $, show, setText, api, operator, canDo, UI, cad), after preplan.js and maplayers.js.
"use strict";

window.PreplanDocs = (() => {
  // sizeup/cad/preplan_docs.py KINDS, in its order
  const KINDS = { floor_plan: "Floor plan", site_plan: "Site plan", preplan: "Pre-plan sheet", inspection: "Inspection",
                  hazmat: "Hazmat / Tier II", contacts: "Contacts", photo: "Photo", other: "Other" };
  const OVERLAY = ["floor_plan", "site_plan"];
  const SRC = "ppd-plan", LAYER = "ppd-plan";
  // the lowest of these the map has: the plan goes right under it (preplan.js's layers, then cadmap.js's lines)
  const ABOVE = ["pp-edit-fp", "pp-icons", "pp-labels", "cm-lines", "cm-trail", "cm-routes", "firehouse"];
  const OPACITY_KEY = "sizeup.planOpacity";
  const MAX_MB = 25;

  // ---- pure helpers (tests/test_preplan_docs.py runs these in node) -------------------------------------------------
  const KX = (lat) => 111320 * Math.cos(lat * Math.PI / 180), KY = 110574;
  const mean = (c) => [c.reduce((s, p) => s + p[0], 0) / c.length, c.reduce((s, p) => s + p[1], 0) / c.length];
  const toM = (o, p) => [(p[0] - o[0]) * KX(o[1]), (p[1] - o[1]) * KY];
  const fromM = (o, m) => [o[0] + m[0] / KX(o[1]), o[1] + m[1] / KY];
  const add = (a, b, k = 1) => [a[0] + b[0] * k, a[1] + b[1] * k];
  const len = (v) => Math.hypot(v[0], v[1]);
  function hull(pts) {                                    // the convex hull (monotone chain), counter-clockwise
    const p = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lo = [], hi = [];
    for (const q of p) { while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], q) <= 0) lo.pop(); lo.push(q); }
    for (const q of p.reverse()) { while (hi.length >= 2 && cross(hi[hi.length - 2], hi[hi.length - 1], q) <= 0) hi.pop(); hi.push(q); }
    return lo.slice(0, -1).concat(hi.slice(0, -1));
  }
  // the footprint's ring ([lon, lat] ...) -> its smallest enclosing rectangle: {center: [lon, lat], angle (its long
  // side, radians counter-clockwise from east, 0 to pi), w (long), h (short)}, metres
  function obb(ring) {
    const o = mean(ring), pts = hull(ring.map((p) => toM(o, p)));
    let best = null;
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i], b = pts[(i + 1) % pts.length];
      const ang = Math.atan2(b[1] - a[1], b[0] - a[0]), c = Math.cos(ang), s = Math.sin(ang);
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (const p of pts) {
        const x = p[0] * c + p[1] * s, y = -p[0] * s + p[1] * c;
        x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
      }
      const area = (x1 - x0) * (y1 - y0);
      if (!best || area < best.area - 1e-9) {
        const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
        best = { area, angle: ang, w: x1 - x0, h: y1 - y0, m: [cx * c - cy * s, cx * s + cy * c] };
      }
    }
    if (!best) return null;
    if (best.h > best.w) best = { ...best, angle: best.angle + Math.PI / 2, w: best.h, h: best.w };
    const angle = ((best.angle % Math.PI) + Math.PI) % Math.PI;
    return { center: fromM(o, best.m), angle, w: best.w, h: best.h };
  }
  // the plan's four corners (TL, TR, BR, BL) on the footprint's box: the oriented one, or (north) its lon / lat box.
  // The image's long side along the box's long side; of the two ways round, its top the more northerly.
  function snap(ring, imgW, imgH, north) {
    let b;
    if (north) {
      const o = mean(ring), m = ring.map((p) => toM(o, p));
      const xs = m.map((p) => p[0]), ys = m.map((p) => p[1]);
      const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
      b = { center: fromM(o, [(x0 + x1) / 2, (y0 + y1) / 2]), angle: 0, w: x1 - x0, h: y1 - y0 };
    } else b = obb(ring);
    if (!b) return null;
    const u = [Math.cos(b.angle), Math.sin(b.angle)], v = [-u[1], u[0]];
    const wide = imgW >= imgH;
    let pick = null;
    for (const [r, eR, eU] of [[u, b.w, b.h], [v, b.h, b.w], [[-u[0], -u[1]], b.w, b.h], [[-v[0], -v[1]], b.h, b.w]]) {
      if (imgW !== imgH && b.w !== b.h && (eR >= eU) !== wide) continue;
      const up = [-r[1], r[0]], score = up[1] * 10 + r[0];
      if (!pick || score > pick.score) pick = { r, up, eR, eU, score };
    }
    const { r, up, eR, eU } = pick;
    const c = [0, 0];
    const m = [add(add(c, r, -eR / 2), up, eU / 2), add(add(c, r, eR / 2), up, eU / 2),
               add(add(c, r, eR / 2), up, -eU / 2), add(add(c, r, -eR / 2), up, -eU / 2)];
    return m.map((p) => fromM(b.center, p));
  }
  const inM = (c) => { const o = mean(c); return { o, m: c.map((p) => toM(o, p)) }; };
  // drag the whole plan from one point to another
  function move(c, to, from) {
    const { o, m } = inM(c), d = [toM(o, to)[0] - toM(o, from)[0], toM(o, to)[1] - toM(o, from)[1]];
    return m.map((p) => fromM(o, add(p, d)));
  }
  function rotate(c, a) {                               // about its middle, counter-clockwise
    const { o, m } = inM(c), co = Math.cos(a), si = Math.sin(a);
    return m.map(([x, y]) => fromM(o, [x * co - y * si, x * si + y * co]));
  }
  function scale(c, k) {
    const { o, m } = inM(c);
    return m.map((p) => fromM(o, [p[0] * k, p[1] * k]));
  }
  const setCorner = (c, i, ll) => c.map((p, j) => (j === i ? [ll[0], ll[1]] : p));
  // what the hub takes (preplan_docs.py check_corners): convex, clockwise (not mirrored), every side 1 to 1000 m
  function valid(c) {
    if (!Array.isArray(c) || c.length !== 4) return false;
    const { m } = inM(c);
    for (let i = 0; i < 4; i++) {
      const a = m[i], b = m[(i + 1) % 4], d = m[(i + 2) % 4];
      const side = len([b[0] - a[0], b[1] - a[1]]);
      if (!(side >= 1 && side <= 1000)) return false;
      if ((b[0] - a[0]) * (d[1] - b[1]) - (b[1] - a[1]) * (d[0] - b[0]) >= 0) return false;
    }
    return true;
  }
  const sizeM = (c) => { const { m } = inM(c); return [len([m[1][0] - m[0][0], m[1][1] - m[0][1]]), len([m[3][0] - m[0][0], m[3][1] - m[0][1]])]; };
  // floors in building order: basements, ground, 1, 2 ..., the roof; anything else after; none last (preplan_docs.py)
  const FLOOR = /^(?:(b|basement|sub|cellar)\s*(\d*)|(\d+)|(g|ground|lobby)|(m|mezz\w*)|(roof|r|attic|penthouse|ph))$/i;
  function floorKey(f) {
    const t = (f || "").trim(), m = FLOOR.exec(t);
    if (!t) return [5, 0, ""];
    if (!m) return [4, 0, t.toLowerCase()];
    if (m[1]) return [0, -(m[2] ? Number(m[2]) : 1), ""];
    if (m[3]) return [1, Number(m[3]), ""];
    if (m[4]) return [1, 0, ""];
    if (m[5]) return [1, 1.5, ""];
    return [3, 0, ""];
  }
  const cmpKey = (a, b) => { for (let i = 0; i < 3; i++) { if (a[i] < b[i]) return -1; if (a[i] > b[i]) return 1; } return 0; };
  const floorOrder = (fs) => [...fs].sort((a, b) => cmpKey(floorKey(a), floorKey(b)));
  const levelOf = (v) => (!v ? "" : /^\d+$/.test(v) ? `Fl ${v}` : v);
  const drawable = (docs) => (docs || []).filter((d) => OVERLAY.includes(d.kind) && d.corners && d.plan !== false);
  // the plan a call shows first: floor 1 (or ground), else the lowest floor plan, else a site plan; null: none aligned
  function pickPlan(docs) {
    const ds = drawable(docs);
    const floors = ds.filter((d) => d.kind === "floor_plan").sort((a, b) => cmpKey(floorKey(a.floor), floorKey(b.floor)));
    const d = floors.find((x) => floorKey(x.floor)[0] === 1) || floors[0] || ds.find((x) => x.kind === "site_plan");
    return d ? d.id : null;
  }
  const countText = (n) => `Site docs (${n})`;
  const beforeId = (ids) => ids.find((id) => ABOVE.includes(id)) || null;

  const api_ = { KINDS, OVERLAY, ABOVE, obb, snap, move, rotate, scale, setCorner, valid, sizeM, floorKey, floorOrder,
                 pickPlan, countText, beforeId };
  if (typeof document === "undefined") return api_;

  // ---- the state ----------------------------------------------------------------------------------------------------
  const st = { inc: null, readonly: true, docs: null, key: "", fetching: "", pick: {}, opacity: prefOpacity(), align: null,
               search: null };
  function prefOpacity() {
    try { const v = Number(localStorage.getItem(OPACITY_KEY)); return v >= 0.1 && v <= 1 ? v : 0.75; } catch (_) { return 0.75; }
  }
  function setOpacity(v) {
    st.opacity = v;
    try { localStorage.setItem(OPACITY_KEY, String(v)); } catch (_) { /* this page view only */ }
    for (const m of maps) m.sync();
  }
  const gen = () => ((window.cad && cad.state && cad.state.preplan_docs) || { gen: 0 }).gen;
  const editor = () => window.canDo && canDo("dispatch");
  const day = (t) => (window.AddrHistory ? AddrHistory.day(t) : "");
  const fileUrl = (d, dl) => `/api/cad/preplan/docs/${d.id}/file` + (dl ? "?dl=1" : "");
  // versioned by its page and size: another page or picture is another URL, so the map loads it again (updateImage)
  const planUrl = (d) => `/api/cad/preplan/docs/${d.id}/plan?v=${d.page || 0}-${d.plan_w}x${d.plan_h}`;

  // a context: the call's ({inc}) or the Search page's building ({where})
  const ctxKey = (ctx) => (ctx.inc ? `i${ctx.inc.id}` : `w${ctx.where}`);
  function chosen(ctx, docs) {
    const k = ctxKey(ctx);
    const ids = drawable(docs).map((d) => d.id);
    if (k in st.pick && (st.pick[k] === 0 || ids.includes(st.pick[k]))) return st.pick[k];
    return pickPlan(docs);
  }

  function load(force) {
    const inc = st.inc;
    if (!inc) return;
    const key = JSON.stringify([inc.id, gen(), inc.bldg_key || "", inc.lat, inc.lon]);
    if (!force && (st.key === key || st.fetching === key)) return;
    st.fetching = key;
    api("GET", `/api/cad/preplan/docs?incident=${encodeURIComponent(inc.id)}`)
      .then((d) => { if (st.fetching !== key) return; st.key = key; st.fetching = ""; st.docs = d.docs || []; st.configured = d.configured; redraw(); })
      .catch(() => { if (st.fetching === key) { st.fetching = ""; st.key = key; st.docs = []; redraw(); } });
  }
  function redraw() {
    if (st.inc) renderBox($("#d-docs"), { inc: st.inc }, st.docs || [], st.readonly, $("#d-docs-form"));
    if (st.search) renderBox($("#sv-pp-docs"), { where: st.search.where }, st.search.docs || [], !editor(), $("#sv-pp-docs-form"));
    for (const m of maps) m.sync();
  }

  // ---- the call's box: "Site docs (3)", the plan's floors and opacity, a row per document --------------------------------
  function card(inc, opts = {}) {
    if (!st.inc || st.inc.id !== inc.id) { if (st.align && st.align.ctx.inc) cancelAlign(); st.docs = null; st.key = ""; }
    st.inc = inc;
    st.readonly = !!opts.readonly;
    load(false);
    renderBox($("#d-docs"), { inc }, st.docs || [], st.readonly, $("#d-docs-form"));
    for (const m of maps) if (m.isCall()) m.sync();
  }
  function renderBox(box, ctx, docs, readonly, host) {
    if (!box) return;
    const located = ctx.where || !!(ctx.inc && (ctx.inc.bldg_key || ctx.inc.lat != null));
    const pick = chosen(ctx, docs);
    const sig = JSON.stringify([ctxKey(ctx), docs, readonly, located, pick]);      // (not the opacity: its slider is in it)
    show(box, docs.length > 0 || (!readonly && located));
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    const head = el("div", "pp-head");
    head.append(el("span", "pp-title", docs.length ? countText(docs.length) : "Site docs"), el("span", "spacer"));
    if (!readonly && located) {
      const add = el("button", "sm quiet", "+ Doc");
      add.type = "button";
      add.title = `Upload a floor plan or document (PDF, PNG, JPEG, at most ${MAX_MB} MB)`;
      add.addEventListener("click", () => form({ ctx, host }));
      head.append(add);
    }
    const parts = [head];
    const plans = drawable(docs);
    if (plans.length) {
      const row = el("div", "row ppd-plans");
      const seg = el("span", "seg");
      seg.setAttribute("role", "group");
      seg.setAttribute("aria-label", "Plan on the map");
      const chip = (label, id, title) => {
        const b = el("button", "sm", label);
        b.type = "button";
        b.title = title;
        b.setAttribute("aria-pressed", String(pick === id));
        b.addEventListener("click", () => { st.pick[ctxKey(ctx)] = id; redraw(); });
        seg.append(b);
      };
      chip("Off", 0, "No plan on the map");
      const floors = plans.filter((d) => d.kind === "floor_plan").sort((a, b) => cmpKey(floorKey(a.floor), floorKey(b.floor)));
      for (const d of floors) chip(d.floor || "?", d.id, d.title);
      for (const d of plans.filter((x) => x.kind === "site_plan")) chip("Site", d.id, d.title);
      const op = el("input", "ppd-opacity");
      Object.assign(op, { type: "range", min: "0.1", max: "1", step: "0.05", value: String(st.opacity) });
      op.setAttribute("aria-label", "Plan opacity");
      op.title = "Plan opacity (yours)";
      op.addEventListener("input", () => setOpacity(Number(op.value)));
      row.append(seg, op);
      parts.push(row);
    }
    const list = el("ol", "pp-list");
    for (const d of docs) list.append(docRow(d, ctx, readonly, host));
    parts.push(list);
    box.replaceChildren(...parts);
  }
  function docRow(d, ctx, readonly, host) {
    const li = el("li", "pp-row" + (d.nearby ? " near" : ""));
    const go = el("button", "pp-go", d.title);
    go.type = "button";
    go.title = [KINDS[d.kind], levelOf(d.floor), d.addr_label].filter(Boolean).join(" · ");
    go.addEventListener("click", () => view(d));
    li.append(go);
    if (d.floor) li.append(el("span", "dim small", levelOf(d.floor)));
    if (OVERLAY.includes(d.kind) && !d.corners && !readonly) li.append(el("span", "chip warn", "Align"));
    if (d.admin_only) li.append(el("span", "chip", "Admin"));
    if (!readonly && window.UI && UI.menu) li.append(UI.menu.button(`Site doc: ${d.title}`, () => [
      { label: "Open", run: () => view(d) },
      ...(OVERLAY.includes(d.kind) ? [{ label: "Align", hint: "on the map", run: () => align(d, ctx, host) }] : []),
      { label: "Edit", run: () => form({ ctx, host, doc: d }) },
      { label: "Delete", danger: true, run: () => remove(d) },
    ], { key: `ppd:${d.id}` }));
    return li;
  }
  async function remove(d) {
    if (!(await UI.confirm(`Delete ${d.title}?`, { ok: "Delete" }))) return;
    try {
      await api("POST", `/api/cad/preplan/docs/${d.id}`, { action: "delete", operator: operator() });
      UI.toast("Deleted");
      refetch();
    } catch (e) { UI.toast(e.message, "err"); }
  }
  function refetch() {
    if (st.align) cancelAlign();
    load(true);
    if (st.search) loadSearch();
  }

  // ---- the viewer: the document itself, in the app ----------------------------------------------------------------------
  let viewer = null;
  function buildViewer() {
    const ov = el("div", "overlay");
    ov.hidden = true;
    const panel = el("div", "dialog tall");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-labelledby", "ppd-title");
    const head = el("div", "dialog-head");
    const title = el("h2", "dialog-title");
    title.id = "ppd-title";
    const x = el("button", "icon quiet");
    x.type = "button";
    x.setAttribute("aria-label", "Close");
    x.title = "Close (Esc)";
    x.append(window.Icon ? Icon("x") : "✕");
    x.addEventListener("click", () => closeViewer());
    head.append(title, el("span", "spacer"), x);
    const body = el("div", "dialog-body ppd-body");
    const foot = el("div", "dialog-foot");
    const sub = el("span", "dim small");
    const open = el("a", "nav sm", "Open");
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.title = "In a new tab";
    const dl = el("a", "nav sm", "Download");
    dl.rel = "noopener noreferrer";
    foot.append(sub, el("span", "spacer"), open, dl);
    panel.append(head, body, foot);
    ov.append(panel);
    ov.addEventListener("click", (e) => { if (e.target === ov) closeViewer(); });
    document.body.append(ov);
    viewer = { ov, title, body, sub, open, dl };
  }
  function view(d) {
    if (!viewer) buildViewer();
    setText(viewer.title, d.title);
    setText(viewer.sub, [KINDS[d.kind], levelOf(d.floor), d.pages > 1 ? `${d.pages} pages` : "", `${d.by || "?"} ${day(d.created_at)}`]
      .filter(Boolean).join(" · "));
    viewer.open.href = fileUrl(d);
    viewer.dl.href = fileUrl(d, true);
    let node;
    if (d.mime === "application/pdf") {
      node = el("iframe", "ppd-frame");
      node.title = d.title;
      node.referrerPolicy = "no-referrer";
    } else {
      node = el("img", "ppd-img");
      node.alt = d.title;
      node.referrerPolicy = "no-referrer";
    }
    node.src = fileUrl(d);
    viewer.body.replaceChildren(node);
    if (window.UI && UI.dialog) UI.dialog.open(viewer.ov, { onClose: () => viewer.body.replaceChildren() });
    else viewer.ov.hidden = false;
  }
  function closeViewer() {
    if (!viewer) return;
    if (window.UI && UI.dialog) UI.dialog.close(viewer.ov);
    else viewer.ov.hidden = true;
    viewer.body.replaceChildren();
  }

  // ---- the upload / edit form -------------------------------------------------------------------------------------------
  // opts: {ctx: {inc} | {where}, host, doc?: edit it}
  function form(opts) {
    cancelAlign();
    const d = opts.doc || null, host = opts.host;
    if (!host) return;
    const f = el("form", "pp-form card");
    f.autocomplete = "off";
    const head = el("div", "pp-form-head");
    head.append(el("b", "", d ? "Edit site doc" : "Site doc"));
    const file = el("input");
    file.type = "file";
    file.accept = "application/pdf,image/png,image/jpeg,.pdf,.png,.jpg,.jpeg";
    file.setAttribute("aria-label", "File");
    const title = Object.assign(el("input"), { type: "text", maxLength: 80, placeholder: "Title", value: d ? d.title : "", spellcheck: false });
    title.setAttribute("aria-label", "Title");
    const kind = el("select");
    kind.setAttribute("aria-label", "Type");
    for (const [k, v] of Object.entries(KINDS)) kind.append(new Option(v, k));
    kind.value = d ? d.kind : "floor_plan";
    const floor = Object.assign(el("input", "pp-level"), { type: "text", maxLength: 20, placeholder: "Floor", value: d ? d.floor || "" : "" });
    floor.setAttribute("aria-label", "Floor");
    floor.title = "1, 2, B1, Roof";
    const page = Object.assign(el("input", "pp-level"), { type: "number", min: "1", step: "1", placeholder: "Page", value: d && d.page ? String(d.page) : "" });
    page.setAttribute("aria-label", "PDF page");
    page.title = "The PDF page drawn on the map";
    const admin = el("label", "check");
    const adminCb = el("input");
    adminCb.type = "checkbox";
    adminCb.checked = !!(d && d.admin_only);
    admin.append(adminCb, el("span", "", "Admin only"));
    admin.title = "Only admins see it";
    const fields = el("div", "row pp-fields");
    fields.append(kind, floor, page);
    if (canDo("admin")) fields.append(admin);
    const pdfPlan = () => OVERLAY.includes(kind.value) && (d ? d.mime === "application/pdf" : !!(file.files[0] && /pdf$/i.test(file.files[0].type || file.files[0].name)));
    const sync = () => { show(page, pdfPlan()); };
    kind.addEventListener("change", sync);
    file.addEventListener("change", () => {
      const fl = file.files[0];
      if (fl && !title.value) title.value = fl.name.replace(/\.[a-z0-9]{1,5}$/i, "").slice(0, 80);
      sync();
    });
    const err = el("span", "error");
    const save = el("button", "primary", d ? "Save" : "Upload");
    save.type = "submit";
    const cancel = el("button", "", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => { host.replaceChildren(); show(host, false); });
    const foot = el("div", "row");
    foot.append(err, el("span", "spacer"), cancel, save);
    f.append(head, ...(d ? [] : [file]), title, fields, foot);
    sync();
    f.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      setText(err, "");
      const meta = { title: title.value, kind: kind.value, floor: floor.value };
      if (pdfPlan() && page.value) meta.page = Number(page.value);
      if (canDo("admin")) meta.admin_only = adminCb.checked;
      try {
        if (d) {
          const body = { ...meta, operator: operator() };
          if (d.page && body.page === d.page) delete body.page;
          if (!canDo("admin")) delete body.admin_only;
          await api("POST", `/api/cad/preplan/docs/${d.id}`, body);
          UI.toast("Saved");
        } else {
          const fl = file.files[0];
          if (!fl) { setText(err, "Pick a file"); return; }
          if (fl.size > MAX_MB * 1024 * 1024) { setText(err, `At most ${MAX_MB} MB`); return; }
          Object.assign(meta, opts.ctx.inc ? { incident_id: opts.ctx.inc.id } : { where: opts.ctx.where }, { operator: operator() });
          await UI.busy(upload(fl, meta), save);
          UI.toast("Uploaded");
        }
        host.replaceChildren();
        show(host, false);
        refetch();
      } catch (e) { setText(err, e.message); }
    });
    host.replaceChildren(f);
    show(host, true);
    (d ? title : file).focus({ preventScroll: true });
  }
  async function upload(fl, meta) {
    const r = await fetch("/api/cad/preplan/docs", {
      method: "POST", credentials: "same-origin", body: fl,
      headers: { "Content-Type": "application/octet-stream", "X-Sizeup-Doc": encodeURIComponent(JSON.stringify(meta)) },
    });
    let data = null;
    try { data = await r.json(); } catch (_) { /* not JSON */ }
    if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
    return data;
  }

  // ---- the plan on a map ----------------------------------------------------------------------------------------------
  const maps = new Set();
  class PlanMap {
    // control: maplayers.js's box for a console map, or {map, profile: "search"} for the Search page's building map
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.drawn = null;                                   // {id, url, corners, opacity}
      maps.add(this);
      this.sync();
    }
    ready() { try { return this.styled || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }
    isCall() {
      return this.control.profile === "incident" || !!(this.map.getContainer().closest && this.map.getContainer().closest(".cm-big"));
    }
    // what this map draws: the plan being aligned on it, else its call's (or building's) chosen plan
    want() {
      const a = st.align;
      if (a && a.map === this) return { d: a.doc, corners: a.corners, opacity: Math.min(st.opacity, 0.7) };
      let ctx = null, docs = null;
      if (this.control.profile === "search") { if (st.search) { ctx = { where: st.search.where }; docs = st.search.docs; } }
      else if (this.isCall() && st.inc && this.control.focused && this.control.focused.id === st.inc.id) { ctx = { inc: st.inc }; docs = st.docs; }
      if (!ctx || !docs) return null;
      const id = chosen(ctx, docs);
      const d = id ? docs.find((x) => x.id === id) : null;
      return d && d.corners ? { d, corners: d.corners, opacity: st.opacity } : null;
    }
    sync() {
      if (!this.ready()) {
        if (!this.waiting) { this.waiting = true; this.map.once("idle", () => { this.waiting = false; this.sync(); }); }
        return;
      }
      this.styled = true;
      const w = this.want(), map = this.map;
      try {
        if (!w) {
          if (map.getLayer(LAYER)) map.removeLayer(LAYER);
          if (map.getSource(SRC)) map.removeSource(SRC);
          this.drawn = null;
          return;
        }
        const url = planUrl(w.d);
        const ids = () => (map.getLayersOrder ? map.getLayersOrder() : (map.getStyle().layers || []).map((l) => l.id));
        if (!map.getSource(SRC)) {
          map.addSource(SRC, { type: "image", url, coordinates: w.corners });
          map.addLayer({ id: LAYER, type: "raster", source: SRC, paint: { "raster-opacity": w.opacity, "raster-fade-duration": 0 } },
                       beforeId(ids()));
        } else if (!this.drawn || this.drawn.url !== url) {
          map.getSource(SRC).updateImage({ url, coordinates: w.corners });
        } else if (JSON.stringify(this.drawn.corners) !== JSON.stringify(w.corners)) {
          map.getSource(SRC).setCoordinates(w.corners);
        }
        if (!this.drawn || this.drawn.opacity !== w.opacity) map.setPaintProperty(LAYER, "raster-opacity", w.opacity);
        const order = ids(), b = beforeId(order.filter((id) => id !== LAYER));
        if (b && order[order.indexOf(LAYER) + 1] !== b) map.moveLayer(LAYER, b);    // pre-plan points came in later: under them
        this.drawn = { url, corners: w.corners, opacity: w.opacity };
      } catch (e) { console.error("site doc plan", e.message); }
    }
    refresh() { this.sync(); }
    keys() { return []; }
    panel() { return null; }
  }
  const callMap = () => [...maps].find((m) => m.control.profile === "incident" && m.map.getContainer().closest("#d-map")) || null;
  function searchPlanMap() {
    const s = st.search;
    const pm = s && s.pmOf && s.pmOf();
    if (!pm) return null;
    if (!s.plan || s.plan.map !== pm.map) s.plan = new PlanMap({ map: pm.map, profile: "search" });
    return s.plan;
  }

  // ---- align: snap to the footprint, then move, rotate, scale, a corner --------------------------------------------------
  function cancelAlign() {
    const a = st.align;
    if (!a) return;
    st.align = null;
    for (const mk of a.marks) mk.remove();
    if (a.host) { a.host.replaceChildren(); show(a.host, false); }
    a.map.sync();
  }
  async function align(d, ctx, host) {
    cancelAlign();
    const pm = ctx.inc ? callMap() : searchPlanMap();
    if (!pm || !host) return;
    let fp = null;
    try { fp = (await api("GET", `/api/cad/preplan/docs/${d.id}`)).footprint; } catch (e) { UI.toast(e.message, "err"); return; }
    const ring = !fp ? null : fp.type === "Polygon" ? fp.coordinates[0] : fp.type === "MultiPolygon" ? fp.coordinates.map((p) => p[0]).flat(1) : null;
    const around = () => {                               // no footprint: a 20 m box on the building's point
      const o = d.lon != null ? [d.lon, d.lat] : pm.map.getCenter().toArray();
      return [fromM(o, [-10, 10]), fromM(o, [10, 10]), fromM(o, [10, -10]), fromM(o, [-10, -10])];
    };
    const fit = (north) => (ring && snap(ring, d.plan_w || 1, d.plan_h || 1, north)) || around();
    const a = { doc: d, ctx, host, map: pm, corners: d.corners || fit(false), marks: [], start: null };
    st.align = a;

    const f = el("form", "pp-form card");
    const head = el("div", "pp-form-head");
    head.append(el("b", "", "Align plan"), el("span", "dim small", d.title));
    const btn = (label, title, fn) => { const b = el("button", "sm", label); b.type = "button"; b.title = title; b.addEventListener("click", fn); return b; };
    const tools = el("div", "row");
    tools.append(btn("Fit", "Snap to the footprint", () => set(fit(false))), btn("Fit N", "Snap to the footprint, north up", () => set(fit(true))),
                 btn("↻ 90°", "Turn a quarter", () => set(rotate(a.corners, -Math.PI / 2))));
    const err = el("span", "error");
    const save = el("button", "primary", "Save");
    save.type = "submit";
    const cancel = el("button", "", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", cancelAlign);
    const foot = el("div", "row");
    foot.append(err, el("span", "spacer"), cancel, save);
    f.append(head, tools, foot);
    f.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!valid(a.corners)) { setText(err, "Not a plan the right way round"); return; }
      try {
        await api("POST", `/api/cad/preplan/docs/${d.id}`, { corners: a.corners, operator: operator() });
        UI.toast("Aligned");
        st.pick[ctxKey(ctx)] = d.id;
        refetch();
      } catch (e) { setText(err, e.message); }
    });
    host.replaceChildren(f);
    show(host, true);

    // the handles: the corners, the middle (move), a round one beyond the top (rotate), a square one past the bottom right (scale)
    const map = pm.map;
    const mark = (cls, title) => {
      const e = el("div", `ppd-h ${cls}`);
      e.title = title;
      const mk = new maplibregl.Marker({ element: e, draggable: true, anchor: "center" }).setLngLat(a.corners[0]).addTo(map);
      a.marks.push(mk);
      return mk;
    };
    const corners = [0, 1, 2, 3].map((i) => mark("corner", "Drag this corner"));
    const mid = mark("mid", "Drag to move");
    const rot = mark("rot", "Drag to rotate");
    const scl = mark("scl", "Drag to scale");
    const places = () => {
      const c = a.corners, o = mean(c), m = c.map((p) => toM(o, p));
      const top = [(m[0][0] + m[1][0]) / 2, (m[0][1] + m[1][1]) / 2], h = sizeM(c)[1], tl = len(top) || 1;
      const out = Math.max(3, h * 0.2);
      return { o, rot: fromM(o, add(top, [top[0] / tl, top[1] / tl], out)), scl: fromM(o, [m[2][0] * 1.15, m[2][1] * 1.15]) };
    };
    function set(c, keep) {
      a.corners = c;
      const p = places();
      corners.forEach((mk, i) => { if (keep !== mk) mk.setLngLat(c[i]); });
      if (keep !== mid) mid.setLngLat(p.o);
      if (keep !== rot) rot.setLngLat(p.rot);
      if (keep !== scl) scl.setLngLat(p.scl);
      setText(err, valid(c) ? "" : "Not a plan the right way round");
      pm.sync();
    }
    const at = (mk) => mk.getLngLat().toArray();
    corners.forEach((mk, i) => mk.on("drag", () => set(setCorner(a.corners, i, at(mk)), mk)));
    for (const mk of [mid, rot, scl]) mk.on("dragstart", () => { a.start = { corners: a.corners, at: at(mk), o: mean(a.corners) }; });
    mid.on("drag", () => set(move(a.start.corners, at(mid), a.start.at), mid));
    rot.on("drag", () => {
      const o = a.start.o, p0 = toM(o, a.start.at), p1 = toM(o, at(rot));
      set(rotate(a.start.corners, Math.atan2(p1[1], p1[0]) - Math.atan2(p0[1], p0[0])), rot);
    });
    scl.on("drag", () => {
      const o = a.start.o, k = len(toM(o, at(scl))) / (len(toM(o, a.start.at)) || 1);
      if (k > 0.05 && k < 20) set(scale(a.start.corners, k), scl);
    });
    for (const mk of [mid, rot, scl]) mk.on("dragend", () => set(a.corners));
    set(a.corners);
    const b = new maplibregl.LngLatBounds();
    for (const p of a.corners) b.extend(p);
    map.fitBounds(b, { padding: 80, maxZoom: 20, duration: 300 });
  }

  // ---- the Search page's Pre-plan tab: the building's documents (preplan.js search() calls this) -------------------------
  async function loadSearch() {
    const s = st.search;
    if (!s || !s.where) return;
    const seq = ++s.seq;
    try {
      const d = await api("GET", `/api/cad/preplan/docs?where=${encodeURIComponent(s.where)}`);
      if (seq !== s.seq) return;
      s.docs = d.docs || [];
    } catch (_) {
      if (seq !== s.seq) return;
      s.docs = [];
    }
    redraw();
    const pm = searchPlanMap();
    if (pm) pm.sync();
  }
  function search(where, pmOf) {
    if (st.align && st.align.ctx.where !== undefined) cancelAlign();
    st.search = st.search || { seq: 0, docs: [], plan: null };
    st.search.where = (where || "").trim();
    st.search.pmOf = pmOf;
    st.search.docs = [];
    if (!st.search.where) { show($("#sv-pp-docs"), false); return; }
    loadSearch();
  }
  function closeSearch() {
    if (!st.search) return;
    st.search.seq++;
    if (st.align && st.align.ctx.where !== undefined) cancelAlign();
  }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new PlanMap(control) });
  if (window.cad && cad.hooks) cad.hooks.push(() => { if (st.inc) load(false); });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && st.align && !ev.defaultPrevented) cancelAlign(); });
  return { ...api_, card, search, closeSearch, view };
})();
