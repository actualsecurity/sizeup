// sizeup CAD console (/cad): PCFD's own events, units and dispatch, laid out like the department's
// sheet ("[TEMPLATE] PCFD ECC OPS", LIVE CAD tab) it replaces. The lists (types, subtypes,
// dispositions, unit statuses, boards, colors) come from the server's data file
// (data/cad_lists.json) in every state push: codes are stored, readable names are shown.
//
// The server pushes the CAD state over SSE once a second (/api/cad/stream); every action is a
// POST to /api/cad/... and the next push shows its result. Nothing here invents data: empty
// lists say so, and what the server didn't say isn't shown.
//
// Transmit is off in this version: "Dispatch" assigns the units, logs the dispatch and plays
// its audio here. Nothing is keyed on a radio.
"use strict";

const $ = (sel) => document.querySelector(sel);
const LOST_AFTER_MS = 6000;

const cad = {
  scene: null, state: null, lastMsgAt: 0, skew: 0,
  sel: null,                 // the working event's id
  extra: new Map(),          // events opened from the Incident database that aren't in the state
  rec: null,                 // {iid, units, unfilled, already, card, note, checked:Set, extra:[], error}
  map: null, marker: null, mapSig: "",
  listSig: "", rcEditing: null, tab: "live",
  emergTouched: false,       // the operator set Emergency on Event info: the type no longer does
  dbAt: 0,
};

// ---- helpers ------------------------------------------------------------------------

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function setText(node, text) {
  text = text ?? "";
  if (node.textContent !== text) node.textContent = text;
}
function show(node, on) {
  if (node.hidden === on) node.hidden = !on;
}
const pad = (n) => String(n).padStart(2, "0");
const serverNow = () => Date.now() / 1000 + cad.skew;
const fmts = {};
function fmt(kind) {
  if (!fmts[kind]) {
    const timeZone = (cad.scene && cad.scene.timezone) || "America/New_York";
    const opts = {
      hms: { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" },
      md: { month: "2-digit", day: "2-digit" },
      date: { weekday: "short", month: "short", day: "numeric" },
      parts: { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" },
    }[kind];
    fmts[kind] = new Intl.DateTimeFormat("en-US", { ...opts, timeZone });
  }
  return fmts[kind];
}
function fmtRecv(unix) {            // the sheet's "10/07 18:58:33"
  if (!unix) return "";
  const d = new Date(unix * 1000);
  return `${fmt("md").format(d)} ${fmt("hms").format(d)}`;
}
function fmtDur(s) {                // h:mm:ss, like the sheet
  if (s === null || s === undefined || !isFinite(s)) return "";
  s = Math.max(0, Math.floor(s));
  return `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}
function since(node, unix) {        // a ticking elapsed (tick() updates every [data-since])
  if (unix) node.dataset.since = String(unix);
  else delete node.dataset.since;
  setText(node, unix ? fmtDur(serverNow() - unix) : "");
  return node;
}
// datetime-local <-> unix, in the station's time zone
function toLocalInput(unix) {
  if (!unix) return "";
  const p = Object.fromEntries(fmt("parts").formatToParts(new Date(unix * 1000)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour === "24" ? "00" : p.hour}:${p.minute}`;
}
function fromLocalInput(v) {
  if (!v) return null;
  const guess = Date.parse(v + ":00Z") / 1000;              // as if UTC, then correct by the zone's offset
  const back = toLocalInput(guess);
  const off = (Date.parse(back + ":00Z") / 1000) - guess;
  return guess - off;
}
function operator() {
  return $("#operator").value.trim();
}
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { /* private window / blocked storage: just not remembered */ }
  return null;
}
function textOn(bg) {               // readable text on a colored cell
  const m = /^#?([0-9a-f]{6})$/i.exec(bg || "");
  if (!m) return "#fff";
  const n = parseInt(m[1], 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#0c141c" : "#fff";
}
function paint(node, bg, fg) {
  node.style.background = bg || "";
  node.style.color = bg ? (fg || textOn(bg)) : "";
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

function syncList(box, items, { key, sig, make, fill, empty, cols }) {
  if (!items.length) {
    if (box.dataset.empty !== empty || box.children.length !== 1) {
      let node;
      if (box.tagName === "TBODY") {
        node = el("tr");
        const td = el("td", "empty", empty);
        td.colSpan = cols || 1;
        node.append(td);
      } else {
        node = el(box.tagName === "OL" || box.tagName === "UL" ? "li" : "div", "empty", empty);
      }
      box.replaceChildren(node);
      box.dataset.empty = empty;
    }
    return;
  }
  delete box.dataset.empty;
  const old = new Map();
  for (const c of [...box.children]) {
    if (c.dataset.key) old.set(c.dataset.key, c);
    else c.remove();
  }
  items.forEach((it, i) => {
    const k = key(it);
    let node = old.get(k);
    if (node) old.delete(k);
    else {
      node = make(it);
      node.dataset.key = k;
    }
    const s = sig(it);
    if (node._sig !== s) {
      node._sig = s;
      fill(node, it);
    }
    if (box.children[i] !== node) box.insertBefore(node, box.children[i] || null);
  });
  for (const node of old.values()) node.remove();
}

function flash(node, msg) {
  setText(node, msg || "");
}

// ---- the lists (from the data file) ----------------------------------------------------------

function L() {
  return (cad.state && cad.state.lists) || {};
}
function nameOf(list, code) {
  const x = (L()[list] || []).find((i) => i.code === code);
  return x ? x.name : code;
}
function typeLabel(t, s) {
  if (!t) return s ? `? / ${nameOf("event_subtypes", s)}` : "";
  return s ? `${nameOf("event_types", t)} / ${nameOf("event_subtypes", s)}` : nameOf("event_types", t);
}
function typeColor(t) {
  const x = (L().event_types || []).find((i) => i.code === t);
  return x ? x.color : "";
}
function typeCell(node, t, s) {
  node.className = "typecell";
  setText(node, typeLabel(t, s) || "Type not set");
  paint(node, t ? typeColor(t) : "#7a5c12");
  return node;
}
function unitStatus(code) {
  return (L().unit_statuses || []).find((s) => s.code === code) || { code, name: code };
}
function roleOf(code) {
  return unitStatus(code).role || "";
}
function eventStatus(inc) {         // what the sheet's STATUS cell shows
  const es = L().event_statuses || {};
  const k = inc.status === "pending" && !inc.emergency ? "open" : inc.status;
  return es[k] || { name: inc.status };
}
function statusBadge(node, inc) {
  const s = eventStatus(inc);
  node.className = "badge";
  setText(node, s.name);
  paint(node, s.bg, s.fg);
  return node;
}
function isEmergency(t, s) {        // data file event_class: TYPE/SUBTYPE, */SUBTYPE, TYPE; default emergency
  const c = L().event_class || {};
  const keys = (s ? [`${t}/${s}`, `*/${s}`] : []).concat([t]);
  for (const k of keys) if (k in c) return c[k] === "emergency";
  return true;
}
function subtypesFor(t) {
  const m = (L().type_subtypes || {})[t];
  const all = L().event_subtypes || [];
  return m && m.length ? all.filter((s) => m.includes(s.code)) : all;
}
function fillSelect(sel, items, blank) {
  const keep = sel.value;
  const opts = items.map(([v, t]) => { const o = el("option", "", t); o.value = v; return o; });
  if (blank !== undefined) { const o = el("option", "", blank); o.value = ""; opts.unshift(o); }
  sel.replaceChildren(...opts);
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
}
function fillTypes(sel, blank = "—") {
  fillSelect(sel, (L().event_types || []).map((t) => [t.code, t.name]), blank);
}
function fillSubtypes(sel, t, blank = "—") {
  fillSelect(sel, subtypesFor(t).map((s) => [s.code, s.name]), blank);
}

function fillListSelects() {
  const sig = JSON.stringify([L().event_types, L().event_subtypes, L().dispositions, L().received_via, L().type_subtypes]);
  if (sig === cad.listSig) return;
  cad.listSig = sig;
  for (const id of ["#f-type", "#e-type", "#w-acc-type", "#u-type"]) fillTypes($(id));
  fillTypes($("#rc-type"), undefined);
  fillSubtypes($("#f-subtype"), $("#f-type").value);
  fillSubtypes($("#e-subtype"), $("#e-type").value);
  fillSubtypes($("#w-acc-sub"), $("#w-acc-type").value);
  fillSubtypes($("#u-subtype"), $("#u-type").value);
  fillSubtypes($("#rc-subtype"), $("#rc-type").value, "Any subtype");
  const via = (L().received_via || []).map((v) => [v.code, v.name]);
  fillSelect($("#f-via"), via, "—");
  fillSelect($("#e-via"), via, "—");
  fillSelect($("#c-disp"), (L().dispositions || []).map((d) => [d.code, d.name]), "Pick a disposition…");
  const fr = L().full_response || "full department response";
  const label = fr.charAt(0).toUpperCase() + fr.slice(1);
  setText($("#r-full-label"), label);
  setText($("#u-full-label"), label);
}

// ---- audio ----------------------------------------------------------------------------

const player = new Audio();
function play(url) {
  if (!url) return;
  player.src = url;
  player.play().catch((e) => console.warn("audio:", e));
}

// ---- connection / clock ------------------------------------------------------------------

function renderConn() {
  const pill = $("#conn");
  const age = Date.now() - cad.lastMsgAt;
  if (!cad.lastMsgAt) { pill.className = "pill warn"; setText(pill, "connecting…"); }
  else if (age > LOST_AFTER_MS) { pill.className = "pill err"; setText(pill, "not updating"); }
  else { pill.className = "pill ok"; setText(pill, "live"); }
  document.body.classList.toggle("stale", !!cad.lastMsgAt && age > LOST_AFTER_MS);
}
function tick() {
  const now = new Date(serverNow() * 1000);
  setText($("#clock-local"), fmt("hms").format(now));
  setText($("#clock-date"), fmt("date").format(now));
  renderConn();
  const t = serverNow();
  for (const n of document.querySelectorAll("[data-since]")) setText(n, fmtDur(t - Number(n.dataset.since)));
}

// ---- events -----------------------------------------------------------------------------

function incidents() {
  return (cad.state && cad.state.incidents) || [];
}
function selected() {
  return incidents().find((i) => i.id === cad.sel) || cad.extra.get(cad.sel) || null;
}
function openAssignments(inc) {
  return (inc.assignments || []).filter((a) => !a.cleared_at);
}
function unitsText(inc) {
  return openAssignments(inc).map((a) => a.name).join(", ");
}
function isOpen(inc) {
  return ["queued", "pending", "active"].includes(inc.status);
}

function selectIncident(id) {
  cad.sel = id;
  cad.rec = null;
  show($("#edit-form"), false);
  show($("#upgrade-form"), false);
  show($("#r-preview-box"), false);
  $("#r-full").checked = false;
  flash($("#r-error"));
  flash($("#c-error"));
  $("#c-disp").value = "";
  if (cad.tab !== "live") setTab("live");
  render();
  recommend();
  if (window.innerWidth <= 1200) $("#working").scrollIntoView({ behavior: "smooth", block: "start" });
}

function eventRow() {
  const tr = el("tr", "ev");
  tr.tabIndex = 0;
  tr.addEventListener("click", () => selectIncident(Number(tr.dataset.key)));
  tr.addEventListener("keydown", (e) => { if (e.key === "Enter") selectIncident(Number(tr.dataset.key)); });
  return tr;
}
function fillEventRow(tr, inc, kind) {
  tr.className = "ev" + (inc.id === cad.sel ? " sel" : "");
  const cells = [el("td", "num", inc.number || "—"), el("td", "")];
  cells[1].append(typeCell(el("span"), inc.type, inc.subtype));
  cells.push(el("td", "loc", inc.address || "—"), el("td", "t", fmtRecv(inc.created_at)));
  if (kind === "nonemerg") cells.push(el("td", "t", inc.until_at ? fmtRecv(inc.until_at).slice(0, 11) : ""));
  else if (kind === "closed") cells.push(el("td", "", nameOf("dispositions", inc.disposition) || (inc.status === "dismissed" ? "Dismissed" : "")));
  else { const td = el("td"); td.append(statusBadge(el("span"), inc)); cells.push(td); }
  cells.push(el("td", "", unitsText(inc)));
  const dur = el("td", "t");
  if (kind === "closed") setText(dur, fmtDur((inc.closed_at || 0) - inc.created_at));
  else since(dur, inc.created_at);
  cells.push(dur);
  tr.replaceChildren(...cells);
}
function renderEventTables() {
  const all = incidents();
  const byAge = (a, b) => a.created_at - b.created_at;        // oldest first
  const sig = (i) => JSON.stringify([i.status, i.number, i.type, i.subtype, i.address, i.id === cad.sel, unitsText(i),
    i.until_at, i.disposition, i.emergency, cad.listSig.length]);
  const opts = (kind, cols) => ({ key: (i) => String(i.id), sig, make: eventRow, fill: (tr, i) => fillEventRow(tr, i, kind), cols });
  syncList($("#t-pending"), all.filter((i) => i.status === "pending" && i.emergency).sort(byAge),
    { ...opts("pending", 7), empty: "No pending events" });
  syncList($("#t-active"), all.filter((i) => i.status === "active" && i.emergency).sort(byAge),
    { ...opts("active", 7), empty: "No active events" });
  syncList($("#t-nonemerg"), all.filter((i) => (i.status === "pending" || i.status === "active") && !i.emergency).sort(byAge),
    { ...opts("nonemerg", 7), empty: "No non-emergency events" });
  syncList($("#t-closed"), all.filter((i) => i.status === "closed" || i.status === "dismissed"),
    { ...opts("closed", 7), empty: "None closed in the last 12 hours" });
  syncList($("#q-queued"), all.filter((i) => i.status === "queued").sort(byAge), {
    key: (i) => String(i.id), sig,
    make: () => {
      const li = el("li", "qi");
      li.tabIndex = 0;
      li.addEventListener("click", () => selectIncident(Number(li.dataset.key)));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") selectIncident(Number(li.dataset.key)); });
      return li;
    },
    fill: (li, inc) => {
      li.className = "qi" + (inc.id === cad.sel ? " sel" : "");
      const head = el("div", "qh");
      head.append(typeCell(el("span"), inc.type, inc.subtype), el("b", "", inc.address || "no location"),
        el("span", "spacer"), since(el("span", "qe"), inc.created_at));
      li.replaceChildren(head);
      if (inc.type_note) li.append(el("div", "qnote", inc.type_note));
      li.append(el("div", "dim small", `received ${fmtRecv(inc.created_at)} · click to check, then accept`));
    },
    empty: "Nothing from 60 Control waiting",
  });
}

// ---- the working event ------------------------------------------------------------------

function renderWorking() {
  const inc = cad.sel !== null ? selected() : null;
  show($("#w-empty"), !inc);
  show($("#w-body"), !!inc);
  if (cad.sel !== null && !inc) {
    setText($("#w-empty .big"), "That event is no longer listed");
    return;
  }
  setText($("#w-empty .big"), "No event open");
  if (!inc) return;
  const open = isOpen(inc);
  setText($("#w-number"), inc.number || "Not yet numbered");
  statusBadge($("#w-status"), inc);
  const cls = $("#w-class");
  cls.className = "chip " + (inc.emergency ? "emerg" : "nonemerg");
  setText(cls, inc.emergency ? "Emergency" : "Non-emergency");
  show($("#w-edit"), open);
  show($("#w-queued"), inc.status === "queued");
  if (inc.status === "queued") {
    setText($("#w-note"), inc.type_note || "");
    const ts = $("#w-acc-type");
    if (ts._for !== inc.id) {
      ts._for = inc.id;
      ts.value = inc.type || "";
      fillSubtypes($("#w-acc-sub"), ts.value);
      $("#w-acc-sub").value = inc.subtype || "";
    }
  }
  setText($("#w-f-number"), inc.number || "—");
  typeCell($("#w-f-type"), inc.type, inc.subtype);
  setText($("#w-f-received"), fmtRecv(inc.created_at));
  statusBadge($("#w-f-status"), inc);
  const loc = (inc.address || "—") + (inc.cross_street ? ` · cross ${inc.cross_street}` : "");
  setText($("#w-f-location"), loc);
  setText($("#w-f-units"), unitsText(inc) || "—");
  setText($("#w-f-caller"), inc.caller_name || "—");
  setText($("#w-f-phone"), inc.phone || "—");
  setText($("#w-f-caddr"), inc.caller_address || "—");
  setText($("#w-f-via"), inc.received_via ? nameOf("received_via", inc.received_via) : "—");
  show($("#w-f-until-w"), !!inc.until_at);
  setText($("#w-f-until"), inc.until_at ? fmtRecv(inc.until_at) : "");
  const dur = $("#w-f-duration");
  if (open) since(dur, inc.created_at);
  else { since(dur, null); setText(dur, fmtDur((inc.closed_at || 0) - inc.created_at)); }
  show($("#w-f-disp-w"), !!inc.disposition);
  setText($("#w-f-disp"), nameOf("dispositions", inc.disposition));
  setText($("#w-f-remarks"), inc.remarks || "");
  renderMap(inc);
  show($("#w-dispatch"), open && !!inc.emergency);
  show($("#w-attach"), open && !inc.emergency && inc.status !== "queued");
  show($("#w-closebox"), inc.status === "pending" || inc.status === "active");
  renderDispatchBox(inc);
  renderAttach(inc);
  renderOnUnits(inc);
  renderChrono(inc);
  show($("#note-form"), true);
}

function renderOnUnits(inc) {
  const rows = (inc.assignments || []).slice().sort((a, b) => (!!a.cleared_at - !!b.cleared_at));
  syncList($("#w-units"), rows, {
    key: (a) => String(a.id),
    sig: (a) => JSON.stringify([a.status, a.dispatched_at, a.enroute_at, a.onscene_at, a.cleared_at, cad.listSig.length]),
    make: () => el("li", "ou"),
    fill: (li, a) => {
      li.className = "ou" + (a.cleared_at ? " cleared" : "");
      const head = el("div", "ouh");
      head.append(el("b", "", a.name));
      if (!a.cleared_at) head.append(statusSelect(a.unit_id, a.status));
      const times = el("span", "dim small",
        [["dispatched", a.dispatched_at], ["responding", a.enroute_at], ["on location", a.onscene_at], ["cleared", a.cleared_at]]
          .filter(([, t]) => t).map(([k, t]) => `${k} ${fmt("hms").format(new Date(t * 1000))}`).join(" · "));
      head.append(times);
      li.replaceChildren(head);
    },
    empty: inc.status === "queued" ? "None: accept it first" : "No units on this event",
  });
}

function playForEvent(inc, e) {
  if (e.kind !== "dispatch") return null;
  const d = (inc.dispatches || []).find((x) => Math.abs(x.at - e.at) < 0.001);
  return d && d.has_audio ? d.id : null;
}
function renderChrono(inc) {
  syncList($("#w-chrono"), (inc.events || []).slice().reverse(), {
    key: (e) => String(e.id),
    sig: (e) => String(e.id),
    make: () => el("tr"),
    fill: (tr, e) => {
      tr.className = `k-${e.kind}`;
      const txt = el("td", "txt");
      const did = playForEvent(inc, e);
      if (did) {
        const b = el("button", "play mini", "▶");
        b.title = "Play this dispatch";
        b.addEventListener("click", () => play(`/api/cad/dispatch/${did}.wav`));
        txt.append(b);
      }
      txt.append(document.createTextNode(e.text));
      tr.replaceChildren(el("td", "t", fmtRecv(e.at)), txt, el("td", "by", e.operator || ""));
    },
    empty: "Nothing yet", cols: 3,
  });
}

// ---- recommendation / preview / dispatch / upgrade ----------------------------------------------

async function recommend(typ, sub) {
  // By id, not from the last push: a just-created event isn't in the state yet.
  const iid = cad.sel, inc = selected();
  if (iid === null || (inc && (!isOpen(inc) || !inc.emergency))) { cad.rec = null; if (inc) renderDispatchBox(inc); return; }
  const body = typ ? { type: typ, subtype: sub || "" } : {};
  try {
    const r = await api("POST", `/api/cad/incident/${iid}/recommend`, body);
    if (cad.sel !== iid) return;
    cad.rec = { iid, ...r, checked: new Set(r.units.map((u) => u.id)), extra: [], forType: typ || "" };
  } catch (e) {
    cad.rec = { iid, units: [], unfilled: [], already: [], card: null, note: "", checked: new Set(), extra: [], error: e.message };
  }
  cad.rec.sig = null;
  renderDispatchBox(selected());
}

function units() {
  return (cad.state && cad.state.units) || [];
}
function unitById(id) {
  return units().find((u) => u.id === id);
}
function freeCodes() {
  return (L().unit_statuses || []).filter((s) => s.role === "free").map((s) => s.code);
}
function canDispatch(u) {
  return roleOf(u.status) !== "oos";
}

function renderDispatchBox(inc) {
  if (!inc || !isOpen(inc) || !inc.emergency) return;
  show($("#r-pending"), inc.status === "queued");
  $("#r-dispatch").disabled = inc.status === "queued";
  const addOn = (inc.dispatches || []).length > 0;
  show($("#r-kind"), addOn);
  show($("#u-open"), addOn && inc.status !== "queued");
  setText($("#r-dispatch"), addOn ? "Dispatch (add-on)" : "Dispatch");
  const rec = cad.rec && cad.rec.iid === inc.id ? cad.rec : null;
  setText($("#r-card"), rec && rec.card ? `run card: ${rec.card_label || rec.card}${rec.forType ? " (new type)" : ""}` : "");
  const warn = rec ? [...(rec.error ? [rec.error] : []), ...(rec.note ? [rec.note] : []), ...rec.unfilled] : [];
  $("#r-unfilled").replaceChildren(...warn.map((w) => el("li", "", w)));
  const rows = rec ? [...rec.units, ...rec.extra.map((id) => ({ ...unitById(id), slot: "added" }))] : [];
  const sig = JSON.stringify([rows.map((u) => u.id), rec && [...rec.checked], rec && rec.already,
    rows.map((u) => { const x = unitById(u.id); return x ? [x.status, x.incident_id] : null; }), cad.listSig.length]);
  if (rec && rec.sig === sig) return;
  if (rec) rec.sig = sig;
  const list = $("#r-units");
  if (!rec) { list.replaceChildren(el("li", "empty", "Working out the run card…")); return; }
  const items = rows.map((u) => {
    const now = unitById(u.id) || u;
    const li = el("li", "ru");
    const lab = el("label");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = rec.checked.has(u.id);
    cb.addEventListener("change", () => {
      if (cb.checked) rec.checked.add(u.id); else rec.checked.delete(u.id);
      rec.sig = null;
    });
    const st = unitStatus(now.status);
    const chip = el("span", "badge", st.name);
    paint(chip, st.bg, st.fg);
    lab.append(cb, el("b", "", u.name), el("span", "dim small", u.slot || ""), chip);
    if (now.spare) lab.append(el("span", "dim small", "spare"));
    if (now.incident_id && now.incident_id !== inc.id) lab.append(el("span", "warn small", `on ${now.incident_number} (will be moved)`));
    li.append(lab);
    return li;
  });
  if (!items.length) {
    items.push(el("li", "empty", rec.already.length ? `On it: ${rec.already.join(", ")}. Nothing more on the run card.`
      : "Nothing recommended: add units below"));
  } else if (rec.already.length) {
    items.push(el("li", "dim small", `Already on it: ${rec.already.join(", ")}`));
  }
  list.replaceChildren(...items);
  const listed = new Set(rows.map((u) => u.id));
  const others = units().filter((u) => !listed.has(u.id) && canDispatch(u) && u.incident_id !== inc.id);
  fillSelect($("#r-add"), others.map((u) => [String(u.id),
    `${u.name} · ${unitStatus(u.status).name}${u.incident_number ? ` (on ${u.incident_number})` : ""}`]),
  others.length ? "Add another unit…" : "No other units can be dispatched");
  const ch = $("#r-channel");
  const chans = (cad.state.transmit && cad.state.transmit.channels) || [];
  if (ch.options.length !== chans.length) fillSelect(ch, chans.map((c) => [c, c]));
}

function checkedIds() {
  const rec = cad.rec;
  if (!rec) return [];
  return [...rec.units.map((u) => u.id), ...rec.extra].filter((id) => rec.checked.has(id));
}

function tickAll() {
  const inc = selected();
  if (!inc || !cad.rec) return;
  const kinds = L().full_response_kinds || [];
  const free = freeCodes();
  const listed = new Set([...cad.rec.units.map((u) => u.id), ...cad.rec.extra]);
  for (const u of units()) {
    if (!kinds.includes(u.kind) || u.mutual_aid || !free.includes(u.status) || u.incident_id) continue;
    if (!listed.has(u.id)) cad.rec.extra.push(u.id);
    cad.rec.checked.add(u.id);
  }
  cad.rec.sig = null;
  renderDispatchBox(inc);
}

function showPreview(title, text, url, note) {
  show($("#r-preview-box"), true);
  setText($("#r-preview-title"), title);
  setText($("#r-preview-text"), text);
  const a = $("#r-audio");
  show(a, !!url);
  if (url) { a.src = url; a.play().catch(() => {}); }
  setText($("#r-audio-note"), note || "");
}

async function busy(button, label, fn) {
  const was = button.textContent;
  button.disabled = true;
  setText(button, label);
  try { await fn(); } finally { button.disabled = false; setText(button, was); }
}

async function preview() {
  const inc = selected();
  flash($("#r-error"));
  if (!inc) return;
  const ids = checkedIds();
  if (!ids.length) return flash($("#r-error"), "Tick at least one unit.");
  await busy($("#r-preview"), "Rendering…", async () => {
    try {
      const r = await api("POST", `/api/cad/incident/${inc.id}/preview`, { unit_ids: ids, full: $("#r-full").checked });
      showPreview(`Preview${r.kind === "add_on" ? " (add-on)" : ""}: nothing assigned or logged`, r.text, r.audio_url, r.audio_note);
    } catch (e) {
      flash($("#r-error"), e.message);
    }
  });
}

function needOperator(errNode) {
  const op = operator();
  if (!op) { $("#operator").focus(); flash(errNode, "Enter your name (Operator, top right) first."); }
  return op;
}

async function dispatch() {
  const inc = selected();
  flash($("#r-error"));
  if (!inc) return;
  const op = needOperator($("#r-error"));
  if (!op) return;
  const ids = checkedIds();
  if (!ids.length) return flash($("#r-error"), "Tick at least one unit.");
  await busy($("#r-dispatch"), "Dispatching…", async () => {
    try {
      const r = await api("POST", `/api/cad/incident/${inc.id}/dispatch`,
        { unit_ids: ids, operator: op, channel: $("#r-channel").value, full: $("#r-full").checked });
      const how = r.transmitted ? `transmitted on ${r.channel}` : `logged, not transmitted (${r.detail})`;
      showPreview(`${r.kind === "add-on" ? "Add-on dispatch" : "Dispatch"} #${r.dispatch_id}: ${how}`
        + (r.moved.length ? ` · moved ${r.moved.join(", ")}` : ""), r.text, r.audio_url, r.audio_note);
      $("#r-full").checked = false;
      setTimeout(() => recommend(), 300);
    } catch (e) {
      flash($("#r-error"), e.message);
    }
  });
}

function upgradeBody() {
  const t = $("#u-type").value;
  return { type: t, subtype: t ? $("#u-subtype").value : "", info: $("#u-info").value.trim(),
    full: $("#u-full").checked, unit_ids: checkedIds() };
}
async function upgrade(e, previewOnly) {
  if (e) e.preventDefault();
  const inc = selected();
  flash($("#u-error"));
  if (!inc) return;
  const body = upgradeBody();
  if (!previewOnly) {
    body.operator = needOperator($("#u-error"));
    if (!body.operator) return;
    body.channel = $("#r-channel").value;
  } else body.preview = true;
  const b = previewOnly ? $("#u-preview") : $("#u-send");
  await busy(b, previewOnly ? "Rendering…" : "Sending…", async () => {
    try {
      const r = await api("POST", `/api/cad/incident/${inc.id}/upgrade`, body);
      if (previewOnly) showPreview("Upgrade preview: nothing changed or logged", r.text, r.audio_url, r.audio_note);
      else {
        const how = r.transmitted ? `transmitted on ${r.channel}` : `logged, not transmitted (${r.detail})`;
        showPreview(`Upgrade #${r.dispatch_id}: ${how}`, r.text, r.audio_url, r.audio_note);
        $("#upgrade-form").reset();
        show($("#upgrade-form"), false);
        setTimeout(() => recommend(), 300);
      }
    } catch (err) {
      flash($("#u-error"), err.message);
    }
  });
}

// ---- non-emergency: attach units --------------------------------------------------------------

function renderAttach(inc) {
  if (!inc || inc.emergency || !isOpen(inc)) return;
  const others = units().filter((u) => u.incident_id !== inc.id);
  const sig = JSON.stringify(others.map((u) => [u.id, u.status, u.incident_number]));
  const sel = $("#a-unit");
  if (sel._sig === sig) return;
  sel._sig = sig;
  fillSelect(sel, others.map((u) => [String(u.id),
    `${u.name} · ${unitStatus(u.status).name}${u.incident_number ? ` (on ${u.incident_number})` : ""}`]), "Pick a unit…");
}
async function attach() {
  const inc = selected(), id = Number($("#a-unit").value);
  flash($("#a-error"));
  if (!inc || !id) return;
  try {
    await api("POST", `/api/cad/incident/${inc.id}/attach`, { unit_ids: [id], operator: operator() });
    $("#a-unit").value = "";
  } catch (e) {
    flash($("#a-error"), e.message);
  }
}

// ---- event actions ----------------------------------------------------------------------

async function act(action, body = {}, errNode = null) {
  const inc = selected();
  if (!inc) return null;
  try {
    const r = await api("POST", `/api/cad/incident/${inc.id}/${action}`, { operator: operator(), ...body });
    if (action === "accept") setTimeout(() => recommend(), 300);
    return r;
  } catch (e) {
    if (errNode) flash(errNode, e.message); else alert(e.message);
    return null;
  }
}

async function closeEvent() {
  flash($("#c-error"));
  const d = $("#c-disp").value;
  if (!d) return flash($("#c-error"), "Pick a disposition first.");
  if (!confirm(`Close this event as "${nameOf("dispositions", d)}"? Units still on it are cleared.`)) return;
  const r = await act("close", { disposition: d }, $("#c-error"));
  if (r) $("#c-disp").value = "";
}

async function setUnitStatus(uid, status) {
  try {
    await api("POST", `/api/cad/unit/${uid}/status`, { status, operator: operator() });
  } catch (e) {
    alert(e.message);
  }
}
function statusSelect(uid, current) {
  const s = el("select", "st");
  s.title = "Change this unit's status (takes effect at once)";
  for (const st of (L().unit_statuses || [])) {
    const o = el("option", "", st.name);
    o.value = st.code;
    s.append(o);
  }
  if (![...s.options].some((o) => o.value === current)) s.append(Object.assign(el("option", "", current), { value: current }));
  s.value = current;
  const st = unitStatus(current);
  paint(s, st.bg, st.fg);
  s.addEventListener("change", () => setUnitStatus(uid, s.value));
  return s;
}

// ---- location box with gazetteer suggestions -------------------------------------------------

function addressBox(input, geoNote, list) {
  const box = { picked: null, timer: null, seq: 0 };
  const ours = () => ((L().geo_town_order || []).slice(0, 2));
  const note = () => {
    geoNote.className = "geo span2" + (box.picked ? "" : " warn");
    if (box.picked) setText(geoNote, `Map: ${box.picked.label} (${box.picked.kind}, ${Math.round(box.picked.confidence * 100)}%)`);
    else if (!input.value.trim()) setText(geoNote, "");
    else if (cad.state && !cad.state.geocoder) setText(geoNote, "Geocoder unavailable: saved as typed, no map pin.");
    else setText(geoNote, "No suggestion picked: saved as typed; placed on the map only if the gazetteer is sure.");
  };
  const lookup = async () => {
    const q = input.value.trim();
    const my = ++box.seq;
    if (q.length < 3) { show(list, false); return note(); }
    try {
      const r = await api("GET", `/api/cad/geocode?q=${encodeURIComponent(q)}`);
      if (my !== box.seq) return;
      const ms = r.matches || [];
      list.replaceChildren(...ms.map((m) => {
        const li = el("li", "");
        li.append(el("b", "", m.label));
        if (m.town) li.append(el("span", "town" + (ours().includes(m.town) ? " ours" : ""), m.town));
        li.append(el("span", "dim small", `${m.kind} · ${Math.round(m.confidence * 100)}%`));
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          box.picked = m;
          input.value = m.label;
          show(list, false);
          note();
        });
        return li;
      }));
      if (!ms.length) list.replaceChildren(el("li", "dim", r.error || "No match in the gazetteer: saved as typed"));
      show(list, true);
    } catch (e) {
      show(list, false);
    }
    note();
  };
  input.addEventListener("input", () => {
    box.picked = null;
    note();
    clearTimeout(box.timer);
    box.timer = setTimeout(lookup, 250);
  });
  input.addEventListener("blur", () => setTimeout(() => show(list, false), 150));
  input.addEventListener("focus", () => { if (list.children.length && !box.picked && input.value.trim()) show(list, true); });
  box.set = (value, picked) => { input.value = value || ""; box.picked = picked; show(list, false); note(); };
  box.geo = () => (box.picked ? { lat: box.picked.lat, lon: box.picked.lon, geo_label: box.picked.label } : {});
  return box;
}

// ---- event info (entry) / edit -------------------------------------------------------------

function entryClass() {
  const t = $("#f-type").value, s = $("#f-subtype").value;
  if (!cad.emergTouched) $("#f-emergency").checked = !t || isEmergency(t, s);
  const auto = !t || isEmergency(t, s);
  setText($("#f-class"), t ? (cad.emergTouched && $("#f-emergency").checked !== auto
    ? `(changed: the type says ${auto ? "emergency" : "non-emergency"})` : `(from the type)`) : "");
  show($("#f-until-w"), !$("#f-emergency").checked);
}

function clearEntry() {
  $("#entry-form").reset();
  newAddr.set("", null);
  cad.emergTouched = false;
  fillSubtypes($("#f-subtype"), "");
  entryClass();
  flash($("#f-error"));
}

async function acceptEvent(e) {
  e.preventDefault();
  flash($("#f-error"));
  const body = {
    type: $("#f-type").value, subtype: $("#f-subtype").value,
    address: $("#f-address").value.trim(), cross_street: $("#f-cross").value.trim(),
    caller_name: $("#f-name").value.trim(), caller_address: $("#f-caddr").value.trim(), phone: $("#f-phone").value.trim(),
    received_via: $("#f-via").value, remarks: $("#f-remarks").value.trim(), emergency: $("#f-emergency").checked,
    until_at: $("#f-emergency").checked ? null : fromLocalInput($("#f-until").value),
    operator: operator(), ...newAddr.geo(),
  };
  if (!body.type) { $("#f-type").focus(); return flash($("#f-error"), "Pick the event type."); }
  try {
    const inc = await api("POST", "/api/cad/incident", body);
    clearEntry();
    selectIncident(inc.id);
  } catch (err) {
    flash($("#f-error"), err.message);
  }
}

function openEdit() {
  const inc = selected();
  if (!inc) return;
  $("#e-type").value = inc.type || "";
  fillSubtypes($("#e-subtype"), inc.type);
  $("#e-subtype").value = inc.subtype || "";
  editAddr.set(inc.address, inc.lat != null ? { lat: inc.lat, lon: inc.lon, label: inc.geo_label || inc.address, kind: "current", confidence: 1 } : null);
  $("#e-cross").value = inc.cross_street || "";
  $("#e-name").value = inc.caller_name || "";
  $("#e-caddr").value = inc.caller_address || "";
  $("#e-phone").value = inc.phone || "";
  $("#e-via").value = inc.received_via || "";
  $("#e-emergency").checked = !!inc.emergency;
  $("#e-until").value = toLocalInput(inc.until_at);
  $("#e-remarks").value = inc.remarks || "";
  flash($("#e-error"));
  show($("#edit-form"), true);
}

async function saveEdit(e) {
  e.preventDefault();
  const inc = selected();
  if (!inc) return;
  const body = { type: $("#e-type").value, subtype: $("#e-subtype").value, cross_street: $("#e-cross").value.trim(),
    caller_name: $("#e-name").value.trim(), caller_address: $("#e-caddr").value.trim(), phone: $("#e-phone").value.trim(),
    received_via: $("#e-via").value, remarks: $("#e-remarks").value.trim(), until_at: fromLocalInput($("#e-until").value),
    operator: operator() };
  if ($("#e-emergency").checked !== !!inc.emergency) body.emergency = $("#e-emergency").checked;
  const addr = $("#e-address").value.trim();
  if (addr !== (inc.address || "") || (editAddr.picked && editAddr.picked.kind !== "current")) {
    body.address = addr;
    Object.assign(body, editAddr.geo());
  }
  try {
    await api("POST", `/api/cad/incident/${inc.id}`, body);
    show($("#edit-form"), false);
    if (body.type !== inc.type || body.subtype !== inc.subtype || "emergency" in body) setTimeout(() => recommend(), 300);
  } catch (err) {
    flash($("#e-error"), err.message);
  }
}

// ---- map ----------------------------------------------------------------------------------

function renderMap(inc) {
  const has = inc && inc.lat != null && inc.lon != null;
  show($("#w-nomap"), !has || !window.maplibregl || !cad.scene);
  show($("#w-map"), has && !!window.maplibregl && !!cad.scene);
  if (!has) { setText($("#w-nomap"), "No map point for this event"); return; }
  if (!window.maplibregl || !cad.scene) { setText($("#w-nomap"), "Map unavailable"); return; }
  const sig = JSON.stringify([inc.id, inc.lat, inc.lon]);
  if (!cad.map) {
    cad.map = new maplibregl.Map({
      container: $("#w-map"), style: buildMapStyle(cad.scene), center: [inc.lon, inc.lat], zoom: 16.5,
      attributionControl: { compact: true }, maxPitch: 0, dragRotate: false, fadeDuration: 0,
    });
    cad.map.on("load", () => {
      cad.map.addImage("firehouse", firehouseIcon(), { pixelRatio: 2 });
      cad.map.getSource("firehouses").setData({
        type: "FeatureCollection",
        features: (cad.scene.firehouses || []).map((f) => ({
          type: "Feature", properties: { name: f.name, address: f.address },
          geometry: { type: "Point", coordinates: f.ll },
        })),
      });
    });
  }
  if (sig === cad.mapSig) return;
  cad.mapSig = sig;
  cad.map.resize();                      // the box may have just been shown
  cad.map.jumpTo({ center: [inc.lon, inc.lat], zoom: 16.5 });
  if (cad.marker) cad.marker.remove();
  const m = el("div", "inc-marker");
  m.style.setProperty("--c", "#ff4d3d");
  m.append(el("i", "ring"), el("i", "dot"));
  cad.marker = new maplibregl.Marker({ element: m, anchor: "center" }).setLngLat([inc.lon, inc.lat]).addTo(cad.map);
}

// ---- unit boards ---------------------------------------------------------------------------

function renderBoards() {
  const box = $("#boards");
  const all = units();
  const boards = L().unit_boards || [];
  if (!all.length || !boards.length) {
    if (!box.querySelector(".empty")) box.replaceChildren(el("div", "empty", "No units on the boards"));
    return;
  }
  box.querySelector(":scope > .empty")?.remove();
  const byName = new Map(all.map((u) => [u.name, u]));
  const onBoards = new Set(boards.flatMap((b) => b.units));
  const off = all.filter((u) => !onBoards.has(u.name) && u.incident_id);    // off the boards but still committed
  const sets = boards.map((b) => [b.id, b.title, b.units]);
  if (off.length) sets.push(["off", "Not on the boards", off.map((u) => u.name)]);
  for (const [id, title, codes] of sets) {
    let sec = box.querySelector(`[data-board="${id}"]`);
    if (!sec) {
      sec = el("section", "panel");
      sec.dataset.board = id;
      const t = el("table", "board");
      t.innerHTML = "<thead><tr><th>Unit</th><th>Status</th><th>Event</th><th>Elapsed</th></tr></thead><tbody></tbody>";
      sec.append(el("h2", "", title), t);
      box.append(sec);
    }
    const rows = codes.map((c, i) => (c === null ? { gap: true, key: `gap-${i}` } : byName.get(c))).filter(Boolean);
    syncList(sec.querySelector("tbody"), rows, {
      key: (u) => (u.gap ? u.key : String(u.id)),
      sig: (u) => (u.gap ? "gap" : JSON.stringify([u.status, u.status_at, u.incident_number, u.incident_id === cad.sel, u.spare, cad.listSig.length])),
      make: (u) => el("tr", u.gap ? "gap" : "urow"),
      fill: (tr, u) => {
        if (u.gap) { tr.replaceChildren(Object.assign(el("td"), { colSpan: 4 })); return; }
        const name = el("td", "u", u.name);
        name.title = [u.title, u.spoken !== u.name ? `said "${u.spoken}"` : "", u.department, u.spare ? "spare" : ""].filter(Boolean).join(" · ");
        if (u.spare) name.append(el("span", "sp", "spare"));
        const stTd = el("td");
        stTd.append(statusSelect(u.id, u.status));
        const ev = el("td", "ev");
        if (u.incident_number) {
          const a = el("a", "", u.incident_number);
          a.href = "#";
          const c = L().unit_event_cell || {};
          paint(a, c.bg, c.fg);
          a.addEventListener("click", (e) => { e.preventDefault(); selectIncident(u.incident_id); });
          ev.append(a);
        }
        tr.replaceChildren(name, stTd, ev, since(el("td", "el"), u.status_at));
      },
      empty: "No units", cols: 4,
    });
  }
  for (const sec of box.querySelectorAll("[data-board]")) {
    if (!sets.some(([id]) => id === sec.dataset.board)) sec.remove();
  }
}

function renderUnitLog() {
  syncList($("#unit-log"), (cad.state && cad.state.unit_log) || [], {
    key: (e) => String(e.id), sig: (e) => String(e.id), make: () => el("li", "ev"),
    fill: (li, e) => {
      const meta = el("div", "meta");
      meta.append(el("span", "t", fmtRecv(e.at)));
      if (e.operator) meta.append(el("span", "", e.operator));
      li.replaceChildren(meta, el("div", "txt", e.text));
    },
    empty: "No status changes off an event",
  });
}

// ---- tabs: incident database, lists ---------------------------------------------------------------

function setTab(tab) {
  cad.tab = tab;
  for (const b of document.querySelectorAll(".tab")) b.classList.toggle("on", b.dataset.tab === tab);
  show($("#view-live"), tab === "live");
  show($("#view-db"), tab === "db");
  show($("#view-lists"), tab === "lists");
  if (tab === "live" && cad.map) setTimeout(() => cad.map.resize(), 0);
  render();
}

async function loadDatabase() {
  if (cad.tab !== "db" || Date.now() - cad.dbAt < 3000) return;
  cad.dbAt = Date.now();
  try {
    const [h, a] = await Promise.all([api("GET", "/api/cad/history"), api("GET", "/api/cad/admin")]);
    syncList($("#db-rows"), h.incidents, {
      key: (i) => String(i.id),
      sig: (i) => JSON.stringify([i.status, i.type, i.subtype, i.address, i.disposition, i.units, i.closed_at, cad.listSig.length]),
      make: eventRow,
      fill: (tr, i) => {
        tr.className = "ev";
        const type = el("td");
        type.append(typeCell(el("span"), i.type, i.subtype));
        const st = el("td");
        st.append(statusBadge(el("span"), i));
        tr.replaceChildren(el("td", "num", i.number), type, el("td", "loc", i.address || "—"), el("td", "t", fmtRecv(i.created_at)),
          el("td", "t", fmtRecv(i.closed_at)), st, el("td", "", nameOf("dispositions", i.disposition)),
          el("td", "", (i.units || []).join(", ")), el("td", "", i.received_via ? nameOf("received_via", i.received_via) : ""));
      },
      empty: "No events yet", cols: 9,
    });
    const rows = Object.entries(a.counts).map(([k, v]) => [k, String(v)]).concat([["Next event #", a.next_number], ["Database", a.db]]);
    $("#admin").replaceChildren(...rows.map(([k, v]) => { const d = el("div"); d.append(el("dt", "", k), el("dd", "", v)); return d; }));
  } catch (e) {
    $("#db-rows").replaceChildren(Object.assign(el("tr"), {}));
    $("#db-rows").firstChild.append(Object.assign(el("td", "empty", `Could not load: ${e.message}`), { colSpan: 9 }));
  }
}

// The Incident database opens an older event that isn't in the live state.
async function openFromDatabase(id) {
  if (!incidents().some((i) => i.id === id)) {
    try { cad.extra.set(id, await api("GET", `/api/cad/incident/${id}`)); } catch (e) { alert(e.message); return; }
  }
  selectIncident(id);
}

function renderLists() {
  if (cad.tab !== "lists") return;
  const l = L();
  setText($("#lists-source"), l.source ? `· from ${l.source}` : "");
  const box = $("#lists");
  if (box._sig === cad.listSig) return;
  box._sig = cad.listSig;
  const section = (title, items, paintFn) => {
    const s = el("div");
    s.append(el("h3", "", title));
    const ol = el("ol");
    for (const it of items) {
      const li = el("li");
      const n = el("span", "", it.name);
      if (paintFn) paintFn(n, it);
      li.append(n);
      if (it.code !== it.name) li.append(document.createTextNode(" "), el("code", "", it.code));
      ol.append(li);
    }
    s.append(ol);
    return s;
  };
  box.replaceChildren(
    section("Event types", l.event_types || [], (n, it) => { n.className = "typecell"; paint(n, it.color); }),
    section("Event subtypes", l.event_subtypes || []),
    section("Dispositions", l.dispositions || []),
    section("Unit statuses", l.unit_statuses || [], (n, it) => { n.className = "badge"; paint(n, it.bg, it.fg); }),
    section("Received via", l.received_via || []),
    section("Non-emergency (the rest are emergency)",
      Object.entries(l.event_class || {}).filter(([, v]) => v === "non-emergency").map(([k]) => ({ code: k, name: k.replace("*/", "any type / ") }))),
  );
}

// ---- run card editor -----------------------------------------------------------------------

function describeSlot(s) {
  return s.unit ? s.unit : `${s.count} × ${s.kind.replace("|", "/")}`;
}

function renderRunCards() {
  if ($("#rc-overlay").hidden) return;
  const cards = (cad.state && cad.state.run_cards) || [];
  show($("#rc-banner"), !!(cad.state && cad.state.run_cards_default));
  syncList($("#rc-list"), cards, {
    key: (c) => String(c.id),
    sig: (c) => JSON.stringify([c.type, c.slots, c.is_default, c.label, cad.rcEditing === c.type]),
    make: () => {
      const li = el("li", "rc");
      li.addEventListener("click", () => editCard(li._card));
      return li;
    },
    fill: (li, c) => {
      li._card = c;
      li.className = "rc" + (cad.rcEditing === c.type ? " sel" : "");
      const h = el("div", "row");
      const t = el("span");
      typeCell(t, ...c.type.split("/"));
      h.append(t, el("span", "spacer"));
      if (c.is_default) h.append(el("span", "chip default", "default"));
      li.replaceChildren(h, el("div", "dim small", c.slots.map(describeSlot).join(", ")), el("div", "dim small", `“…for ${c.phrase}”`));
    },
    empty: "No run cards: add one",
  });
}

function slotRow(slot) {
  const li = el("li", "slot");
  if (slot.unit !== undefined) {
    const s = el("select", "s-unit");
    fillSelect(s, units().map((u) => [u.name, u.name]));
    s.value = slot.unit || (units()[0] && units()[0].name) || "";
    li.append(el("span", "dim", "unit"), s);
  } else {
    const n = el("input", "s-count");
    n.type = "number"; n.min = "1"; n.max = "10"; n.value = String(slot.count || 1);
    const s = el("select", "s-kind");
    const kinds = (L().unit_kinds || []).map((k) => [k.id, k.label]);
    const ladder = kinds.some(([k]) => k === "ladder") && kinds.some(([k]) => k === "tower") ? [["ladder|tower", "Ladder or tower"]] : [];
    fillSelect(s, [...kinds.slice(0, 1), ...ladder, ...kinds.slice(1)]);
    s.value = slot.kind || "engine";
    li.append(n, el("span", "dim", "×"), s);
  }
  const up = el("button", "mini", "↑");
  up.type = "button";
  up.title = "Move up";
  up.addEventListener("click", () => { if (li.previousElementSibling) li.parentNode.insertBefore(li, li.previousElementSibling); });
  const rm = el("button", "mini", "✕");
  rm.type = "button";
  rm.title = "Remove";
  rm.addEventListener("click", () => li.remove());
  li.append(el("span", "spacer"), up, rm);
  return li;
}

function rcPlaceholder() {
  const t = $("#rc-type").value, s = $("#rc-subtype").value;
  const sp = L().spoken || {};
  $("#rc-spoken").placeholder = sp[s ? `${t}/${s}` : t] || sp[t] || "from the data file";
}

function editCard(c) {
  cad.rcEditing = c ? c.type : null;
  const [t, s] = c ? c.type.split("/") : [$("#rc-type").value || "", ""];
  $("#rc-type").value = t;
  fillSubtypes($("#rc-subtype"), t, "Any subtype");
  $("#rc-subtype").value = s || "";
  $("#rc-spoken").value = c ? c.spoken || "" : "";
  rcPlaceholder();
  $("#rc-slots").replaceChildren(...(c ? c.slots : [{ kind: "engine", count: 1 }]).map(slotRow));
  show($("#rc-delete"), !!c);
  flash($("#rc-error"));
  renderRunCards();
}

function slotsFromForm() {
  return [...$("#rc-slots").children].map((li) => {
    const u = li.querySelector(".s-unit");
    if (u) return { unit: u.value };
    return { kind: li.querySelector(".s-kind").value, count: Number(li.querySelector(".s-count").value) };
  });
}

async function saveCard(e) {
  e.preventDefault();
  flash($("#rc-error"));
  const t = $("#rc-type").value, s = $("#rc-subtype").value;
  const body = { type: s ? `${t}/${s}` : t, spoken: $("#rc-spoken").value.trim(), slots: slotsFromForm() };
  if (cad.rcEditing && cad.rcEditing !== body.type) body.rename_from = cad.rcEditing;
  try {
    const c = await api("POST", "/api/cad/runcards", body);
    cad.rcEditing = c.type;
    flash($("#rc-error"), "Saved.");
  } catch (err) {
    flash($("#rc-error"), err.message);
  }
}

async function deleteCard() {
  if (!cad.rcEditing || !confirm(`Delete the run card for ${typeLabel(...cad.rcEditing.split("/"))}?`)) return;
  try {
    await api("POST", "/api/cad/runcards/delete", { type: cad.rcEditing });
    editCard(null);
  } catch (err) {
    flash($("#rc-error"), err.message);
  }
}

function openRunCards() {
  show($("#rc-overlay"), true);
  const cards = (cad.state && cad.state.run_cards) || [];
  editCard(cards.find((c) => c.type === cad.rcEditing) || cards[0] || null);
}

// ---- state ----------------------------------------------------------------------------------

function render() {
  fillListSelects();
  renderEventTables();
  renderWorking();
  renderBoards();
  renderUnitLog();
  renderRunCards();
  renderLists();
  loadDatabase();
}

function onState(data) {
  cad.lastMsgAt = Date.now();
  if (data.error) { console.warn("CAD state error:", data.error); renderConn(); return; }
  const changed = !cad.state || cad.state.rev !== data.rev;
  cad.state = data;
  if (typeof data.time === "number") cad.skew = data.time - Date.now() / 1000;
  const tx = data.transmit || {};
  const pill = $("#tx-pill");
  pill.className = "pill " + (tx.enabled ? "tx-on" : "tx-off");
  setText(pill, tx.enabled ? "Transmit on" : "Transmit off");
  setText($("#tx-note"), tx.enabled ? "" : (tx.note || "Transmit is off") + ".");
  if (changed) cad.dbAt = 0;
  renderConn();
  render();
}

const newAddr = addressBox($("#f-address"), $("#f-geo"), $("#f-sugg"));
const editAddr = addressBox($("#e-address"), $("#e-geo"), $("#e-sugg"));

$("#tab-live").addEventListener("click", () => setTab("live"));
$("#tab-db").addEventListener("click", () => setTab("db"));
$("#tab-lists").addEventListener("click", () => setTab("lists"));
$("#entry-form").addEventListener("submit", acceptEvent);
$("#f-clear").addEventListener("click", clearEntry);
$("#f-type").addEventListener("change", () => { fillSubtypes($("#f-subtype"), $("#f-type").value); entryClass(); });
$("#f-subtype").addEventListener("change", entryClass);
$("#f-emergency").addEventListener("change", () => { cad.emergTouched = true; entryClass(); });
$("#w-edit").addEventListener("click", openEdit);
$("#w-close-view").addEventListener("click", () => { cad.sel = null; cad.rec = null; render(); });
$("#w-acc-type").addEventListener("change", () => fillSubtypes($("#w-acc-sub"), $("#w-acc-type").value));
$("#w-accept").addEventListener("click", () => act("accept", { type: $("#w-acc-type").value, subtype: $("#w-acc-sub").value }));
$("#w-dismiss").addEventListener("click", () => {
  const why = prompt("Dismiss this page: why? (optional)");
  if (why !== null) act("dismiss", { reason: why });
});
$("#e-type").addEventListener("change", () => fillSubtypes($("#e-subtype"), $("#e-type").value));
$("#edit-cancel").addEventListener("click", () => show($("#edit-form"), false));
$("#edit-form").addEventListener("submit", saveEdit);
$("#r-refresh").addEventListener("click", () => recommend());
$("#r-preview").addEventListener("click", preview);
$("#r-dispatch").addEventListener("click", dispatch);
$("#r-tickall").addEventListener("click", tickAll);
$("#r-add-btn").addEventListener("click", () => {
  const id = Number($("#r-add").value);
  if (!id || !cad.rec) return;
  cad.rec.extra.push(id);
  cad.rec.checked.add(id);
  cad.rec.sig = null;
  renderDispatchBox(selected());
});
$("#u-open").addEventListener("click", () => {
  const f = $("#upgrade-form");
  show(f, f.hidden);
  flash($("#u-error"));
  if (!f.hidden) $("#u-info").focus();
});
$("#u-type").addEventListener("change", () => fillSubtypes($("#u-subtype"), $("#u-type").value));
$("#u-recommend").addEventListener("click", () => {
  const t = $("#u-type").value;
  if (!t) return flash($("#u-error"), "Pick the new type first.");
  flash($("#u-error"));
  recommend(t, $("#u-subtype").value);
});
$("#u-preview").addEventListener("click", () => upgrade(null, true));
$("#u-cancel").addEventListener("click", () => { $("#upgrade-form").reset(); show($("#upgrade-form"), false); });
$("#upgrade-form").addEventListener("submit", (e) => upgrade(e, false));
$("#a-add").addEventListener("click", attach);
$("#c-close").addEventListener("click", closeEvent);
$("#note-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const inc = selected(), input = $("#note-text");
  if (!inc || !input.value.trim()) return;
  try {
    await api("POST", `/api/cad/incident/${inc.id}/note`, { text: input.value.trim(), operator: operator() });
    input.value = "";
    if (cad.extra.has(inc.id)) cad.extra.set(inc.id, await api("GET", `/api/cad/incident/${inc.id}`));
  } catch (err) {
    alert(err.message);
  }
});
$("#db-rows").addEventListener("click", (e) => {
  const tr = e.target.closest("tr.ev");
  if (tr) { e.stopPropagation(); openFromDatabase(Number(tr.dataset.key)); }
}, true);
$("#runcards-open").addEventListener("click", openRunCards);
$("#rc-close").addEventListener("click", () => show($("#rc-overlay"), false));
$("#rc-form").addEventListener("submit", saveCard);
$("#rc-type").addEventListener("change", () => { fillSubtypes($("#rc-subtype"), $("#rc-type").value, "Any subtype"); rcPlaceholder(); });
$("#rc-subtype").addEventListener("change", rcPlaceholder);
$("#rc-add-kind").addEventListener("click", () => $("#rc-slots").append(slotRow({ kind: "engine", count: 1 })));
$("#rc-add-unit").addEventListener("click", () => $("#rc-slots").append(slotRow({ unit: "" })));
$("#rc-new").addEventListener("click", () => editCard(null));
$("#rc-delete").addEventListener("click", deleteCard);
$("#operator").value = store("sizeup.cad.operator") || "";
$("#operator").addEventListener("input", () => store("sizeup.cad.operator", operator()));
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#rc-overlay").hidden) show($("#rc-overlay"), false);
});

async function boot() {
  try {
    cad.scene = await (await fetch("/api/scene")).json();
    const name = (cad.scene.station && cad.scene.station.name) || "sizeup";
    document.title = `CAD · ${name}`;
    setText($("#station"), name);
  } catch (e) {
    console.warn("no scene (map off):", e);
  }
  const es = new EventSource("/api/cad/stream");
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (err) { console.warn("bad state push:", err); return; }
    onState(data);
  };
  tick();
  setInterval(tick, 1000);
}

boot().catch((err) => {
  const pill = $("#conn");
  pill.className = "pill err";
  pill.textContent = "failed to load: " + err.message;
  console.error(err);
});
