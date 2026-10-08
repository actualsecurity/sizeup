// sizeup CAD console (/cad): PCFD's own incidents, units and dispatch.
// Left: new incident, the pages from 60 Control to accept, pending / active / non-emergency
// incidents, recently closed. Middle: the selected incident (or the new-incident form), its units,
// dispatch, dispatches and timeline. Right: the units board grouped by kind, with status buttons.
// The lists (types, subtypes, dispositions, unit statuses and their colors, the boards' units and
// order) come from the server's data file (data/cad_lists.json) in every state push: codes are
// stored, readable names are shown.
//
// The server pushes the CAD state over SSE once a second (/api/cad/stream); every action is a
// POST to /api/cad/... and the next push shows its result. Nothing here invents data: empty
// lists say so, and what the server didn't say isn't shown.
//
// Transmit is off in this version: "Dispatch" assigns the units, logs the dispatch and plays
// its audio here. Nothing is keyed on a radio.
//
// Views (the header's tabs): Dispatch (the selected incident), Overview (every open call, cadmap.js),
// Radio (/cad#radio?..., radio-view.js: every transmission, which replaced the Comms page) and Settings.
// /monitor is this same page as a read-only wall board (BOARD, board.js): the Overview only, larger type,
// nothing to press but Sound and Full screen, so it never disagrees with the console.
//
// Settings (/cad#settings/<section>, settings.js) edit the lists, run cards, units and wording; the
// edits come back in the next push like everything else. Retired items stay named on old events
// but are off the pick lists. Other agencies' units (EMS, PD, utilities: response "agency") are never
// dispatched or ticked: they are attached to an event when they show up.
"use strict";

const $ = (sel) => document.querySelector(sel);
const BOARD = document.documentElement.classList.contains("board");   // /monitor (cad.html's head script)
const LOST_AFTER_MS = 6000;
// The units board: the boards of the data file in order. One with group_by "kind" (main: PCFD's and
// Rye Brook's) shows its apparatus by kind (the data file's order within each), then its code-only
// units under rest_title; every other board is one group (EMS, each mutual-aid department, the MA
// requests, agencies & resources), folded until opened when the data file says so.
const KIND_GROUPS = [
  ["Chief officers", ["chief"]], ["Engines", ["engine"]], ["Ladders & towers", ["ladder", "tower"]],
  ["Rescue", ["rescue"]], ["Utility", ["utility"]], ["Marine", ["marine"]],
];

const cad = {
  scene: null, state: null, lastMsgAt: 0, skew: 0,
  who: null,                 // signed in through Cloudflare Access (whoami): {email, name, callsign, role, may, ...}
  sel: null,                 // selected incident id
  mode: "none",              // none (Overview) | new | view | idle (Dispatch with nothing selected)
  radio: false,              // the Radio view is open (#radio)
  extra: new Map(),          // incidents opened from Records that aren't in the live state
  rec: null,                 // {iid, units, unfilled, already, card, note, checked:Set, extra:[], error}
  map: null, marker: null, mapSig: "",
  listSig: "", rcEditing: null, openUnit: null,
  ask: null,                 // {uid, status, where}: asking why (out of service / unmanned)
  hooks: [],                 // more renderers (settings.js), called on every render
  openBoards: new Set(),     // folded boards (cad_lists.json unit_boards folded) opened here
  emergTouched: false,       // the operator set Emergency on the form: the type no longer does
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
      hm: { hour: "2-digit", minute: "2-digit", hourCycle: "h23" },
      hms: { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" },
      short: { month: "short", day: "numeric" },
      day: { weekday: "short", month: "short", day: "numeric" },
      md: { month: "2-digit", day: "2-digit" },
      parts: { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" },
    }[kind];
    fmts[kind] = new Intl.DateTimeFormat("en-US", { ...opts, timeZone });
  }
  return fmts[kind];
}
function fmtTime(unix, secs = false) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  const now = new Date(serverNow() * 1000);
  const t = fmt(secs ? "hms" : "hm").format(d);
  return fmt("short").format(d) === fmt("short").format(now) ? t : `${fmt("short").format(d)} ${t}`;
}
function fmtRecv(unix) {            // the sheet's "10/07 18:58:33" (Records)
  if (!unix) return "";
  const d = new Date(unix * 1000);
  return `${fmt("md").format(d)} ${fmt("hms").format(d)}`;
}
function fmtDur(s) {
  if (s === null || s === undefined || !isFinite(s)) return "";
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}
function since(node, unix) {        // a ticking "elapsed" (tick() updates every [data-since])
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
  const pick = $("#operator-pick");
  return (pick.hidden ? $("#operator").value : pick.value).trim();
}
// "Corey <corey@example.org>": an operator who signed in through Cloudflare Access (the hub,
// sizeup/cloud/access.py). Shown by name with the email as the tooltip; a typed name as it is.
function opParts(s) {
  const m = /^(.*\S) <([^<>\s]+@[^<>\s]+)>$/.exec(s || "");
  return m ? [m[1], m[2]] : [s || "", /^[^<>\s]+@[^<>\s]+$/.test(s || "") ? s : ""];
}
function opSpan(s, before = "") {
  const [name, email] = opParts(s);
  const sp = el("span", "", before + name);
  if (email) sp.title = email;
  return sp;
}
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { /* private window / blocked storage: just not remembered */ }
  return null;
}

// Every call to the server. A write marks the button that started it busy until the answer (ui.js).
function api(method, path, body) {
  const p = apiCall(method, path, body);
  return method === "GET" ? p : UI.busy(p);
}
async function apiCall(method, path, body) {
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
function jointOf(code) {             // Rye Brook's units: {town, tag, units, quick_add}, else null
  for (const [town, j] of Object.entries(L().joint_response || {})) if (j.units.includes(code)) return { town, ...j };
  return null;
}
function unitStatus(code) {
  return (L().unit_statuses || []).find((s) => s.code === code) || { code, name: code };
}
function roleOf(code) {
  return unitStatus(code).role || "";
}
function statusChip(code, text) {   // the old outline chip, in the data file's status color
  const st = unitStatus(code);
  const c = el("span", "chip st", text ?? st.name);
  c.style.setProperty("--c", st.bg || "#9AA3AD");
  c.title = st.code === (text ?? st.name) ? st.code : `${st.code}: ${st.name}`;
  return c;
}
function statusColors(node, st) {  // a status button (style.css .stat): its color, and its text color when filled
  node.classList.add("stat");
  node.style.setProperty("--c", st.bg || "#9AA3AD");
  if (st.fg) node.style.setProperty("--fg", st.fg);
  return node;
}
function eventStatus(inc) {         // Pending / Open / Active / Closed, with the data file's color
  const es = L().event_statuses || {};
  const k = inc.status === "pending" && !inc.emergency ? "open" : inc.status;
  return es[k] || { name: inc.status };
}
function stBadge(node, name, color) {
  node.className = "st-badge";
  setText(node, name);
  node.style.setProperty("--c", color || "#9AA3AD");
  return node;
}
function isEmergency(t, s) {        // data file event_class: TYPE/SUBTYPE, */SUBTYPE, TYPE; default emergency
  const c = L().event_class || {};
  const keys = (s ? [`${t}/${s}`, `*/${s}`] : []).concat([t]);
  for (const k of keys) if (k in c) return c[k] === "emergency";
  return true;
}
const live = (x) => !x.retired;     // retired: named on old events, off the pick lists
function subtypesFor(t) {            // the type -> subtype map: only those listed (none, or not in the map: no subtype)
  const m = (L().type_subtypes || {})[t] || [];
  return (L().event_subtypes || []).filter((s) => live(s) && m.includes(s.code));
}
function fillSelect(sel, items, blank) {
  const keep = sel.value;
  const opts = items.map(([v, t]) => { const o = el("option", "", t); o.value = v; return o; });
  if (blank !== undefined) { const o = el("option", "", blank); o.value = ""; opts.unshift(o); }
  sel.replaceChildren(...opts);
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
}
function fillTypes(sel, blank = "—") {
  fillSelect(sel, (L().event_types || []).filter(live).map((t) => [t.code, t.name]), blank);
}
function fillSubtypes(sel, t, blank = "—") {
  const subs = subtypesFor(t);
  fillSelect(sel, subs.map((s) => [s.code, s.name]), blank);
  sel.disabled = !subs.length;           // a type with no subtypes (BRUSH, BLS...): nothing to pick
  sel.title = t && !subs.length ? `${nameOf("event_types", t)} takes no subtype` : "";
}

function fillListSelects() {
  const sig = JSON.stringify([L().event_types, L().event_subtypes, L().dispositions, L().received_via, L().type_subtypes]);
  if (sig === cad.listSig) return;
  cad.listSig = sig;
  for (const id of ["#f-type", "#e-type", "#d-acc-type", "#u-type"]) fillTypes($(id));
  fillTypes($("#rc-type"), undefined);
  fillSubtypes($("#f-subtype"), $("#f-type").value);
  fillSubtypes($("#e-subtype"), $("#e-type").value);
  fillSubtypes($("#d-acc-sub"), $("#d-acc-type").value);
  fillSubtypes($("#u-subtype"), $("#u-type").value);
  fillSubtypes($("#rc-subtype"), $("#rc-type").value, "Any subtype");
  const via = (L().received_via || []).filter(live).map((v) => [v.code, v.name]);
  fillSelect($("#f-via"), via, "—");
  fillSelect($("#e-via"), via, "—");
  fillSelect($("#c-disp"), (L().dispositions || []).filter(live).map((d) => [d.code, d.name]), "Disposition");
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
  if (!cad.lastMsgAt) { pill.className = "pill warn"; setText(pill, "Connecting"); }
  else if (age > LOST_AFTER_MS) { pill.className = "pill err"; setText(pill, "No updates"); }
  else if (cad.live && cad.live.mode() === "polling") {
    pill.className = "pill ok"; setText(pill, "Polling");
    pill.title = "Stream blocked on this network: polling every 2 s";
  } else { pill.className = "pill ok"; setText(pill, "Live"); pill.title = ""; }
  document.body.classList.toggle("stale", !!cad.lastMsgAt && age > LOST_AFTER_MS);
}
function renderClock() {              // the same clock on the console and the board: date, local time, Zulu
  const d = new Date(serverNow() * 1000);
  setText($("#clock-date"), fmt("day").format(d));
  setText($("#clock-local"), fmt("hms").format(d));
  setText($("#clock-z"), `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`);
}

// ---- views: Dispatch / Overview / Radio (Settings: settings.js) ---------------------------------------

function leaveView() {               // an incident or the form opens: out of Radio and Settings
  if (location.hash) history.pushState(null, "", location.pathname);
  if (cad.radio || !$("#settings").hidden || !$("#styleguide").hidden) window.dispatchEvent(new HashChangeEvent("hashchange"));
}
function route() {                    // #radio?filters: the Radio view; anything else: the console's own
  const radio = !BOARD && location.hash.startsWith("#radio");
  const q = radio ? location.hash.slice("#radio".length).replace(/^\?/, "") : "";
  if (radio !== cad.radio || radio) {
    cad.radio = radio;
    show($("#radio-view"), radio);
    if (window.RadioView) { if (radio) RadioView.open(q); else RadioView.close(); }
    if (cad.state) render();
    if (radio && window.innerWidth <= 1100) $("#detail").scrollIntoView({ block: "start" });   // stacked: under the queue
  }
}
function renderTabs() {
  const settings = !$("#settings").hidden || !$("#styleguide").hidden;
  const tab = settings ? "settings" : cad.radio ? "radio" : cad.mode === "none" ? "overview" : "dispatch";
  for (const [id, k] of [["#dispatch-open", "dispatch"], ["#overview-open", "overview"], ["#radio-open", "radio"], ["#settings-open", "settings"]]) {
    const b = $(id);
    b.classList.toggle("on", tab === k);
    if (tab === k) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
}
function openDispatch() {             // the Dispatch tab: the incident last open, else the oldest working one
  leaveView();
  if (cad.mode === "new" || (cad.mode === "view" && selected())) return render();
  const order = { queued: 0, active: 1, pending: 2 };
  const next = incidents().filter((i) => i.status in order).sort((a, b) => (order[a.status] - order[b.status]) || (a.created_at - b.created_at))[0];
  if (next) return selectIncident(next.id);
  cad.mode = "idle";
  cad.sel = null;
  render();
}
function tick() {
  renderClock();
  renderConn();
  const now = serverNow();
  for (const n of document.querySelectorAll("[data-since]")) setText(n, fmtDur(now - Number(n.dataset.since)));
}

// ---- incidents: queue --------------------------------------------------------------------

function incidents() {
  return (cad.state && cad.state.incidents) || [];
}
function selected() {
  return incidents().find((i) => i.id === cad.sel) || cad.extra.get(cad.sel) || null;
}
function openAssignments(inc) {
  return (inc.assignments || []).filter((a) => !a.cleared_at);
}
function isOpen(inc) {
  return ["queued", "pending", "active"].includes(inc.status);
}
function incTitle(inc) {
  return inc.number || (inc.status === "queued" ? "Page" : "—");
}

function queueItem() {
  const li = el("li", "qi item");
  li.tabIndex = 0;
  li.addEventListener("click", () => selectIncident(Number(li.dataset.key)));
  li.addEventListener("keydown", (e) => { if (e.key === "Enter") selectIncident(Number(li.dataset.key)); });
  return li;
}
function fillQueueItem(li, inc) {
  li.className = `qi item ${inc.status}` + (inc.emergency ? "" : " nonemerg") + (inc.id === cad.sel ? " sel" : "");
  if (inc.id === cad.sel) li.setAttribute("aria-current", "true"); else li.removeAttribute("aria-current");
  const head = el("div", "qh");
  const t = el("span", "qt", typeLabel(inc.type, inc.subtype) || "No type");
  if (inc.type) t.style.setProperty("--c", typeColor(inc.type) || "#9AA3AD");
  else t.classList.add("warn");
  head.append(el("b", "qn", incTitle(inc)), t);
  if (inc.joint_call) head.append(el("span", "chip joint", inc.joint_call));
  const right = el("span", "qe");
  if (isOpen(inc)) since(right, inc.created_at);
  else right.textContent = fmtTime(inc.closed_at);
  head.append(el("span", "spacer"), right);
  const addr = el("div", "qa" + (inc.address ? "" : " dim"), inc.address || "No location");
  li.replaceChildren(head, addr);
  if (inc.status === "queued") {
    if (inc.type_note) li.append(el("div", "qs warn", inc.type_note));
    li.append(el("div", "qs dim", "Accept or Dismiss"));
  } else if (!inc.emergency && isOpen(inc)) {
    if (inc.until_at) li.append(el("div", "qs dim", `Until ${fmtTime(inc.until_at)}`));
  } else if (!isOpen(inc)) {
    li.append(el("div", "qs dim", inc.status === "dismissed" ? "Dismissed" : nameOf("dispositions", inc.disposition) || "Closed"));
  }
  const on = openAssignments(inc);
  if (on.length) {
    const chips = el("div", "chips");
    for (const a of on) chips.append(statusChip(a.status, `${a.name} ${a.status}`));
    li.append(chips);
  }
}
// The phone pre-alert (prealert.js, sizeup/prealert.py): Tapout heard the HQ phone ring.
function renderPrealert() {
  const pa = cad.state && cad.state.prealert;
  const deps = { api, operator, time: (t) => fmtTime(t, true), readonly: BOARD };
  PreAlert.column($(BOARD ? "#b-prealert" : "#q-prealert"), BOARD ? null : $("#tapout"), pa, deps);
  PreAlert.badge($("#prealert"), pa);
  PreAlert.notify(pa);
  show($("#csv-prealerts"), !!pa);
}
function renderQueue() {
  const all = incidents();
  const byAge = (a, b) => a.created_at - b.created_at;        // oldest first
  const sig = (i) => JSON.stringify([i.status, i.number, i.type, i.subtype, i.address, i.id === cad.sel, i.emergency,
    i.until_at, i.disposition, i.type_note, i.joint_call, openAssignments(i).map((a) => [a.name, a.status]), i.closed_at, cad.listSig.length]);
  const opts = { key: (i) => String(i.id), sig, make: queueItem, fill: fillQueueItem };
  syncList($("#q-queued"), all.filter((i) => i.status === "queued").sort(byAge), { ...opts, empty: "None" });
  syncList($("#q-pending"), all.filter((i) => i.status === "pending" && i.emergency).sort(byAge),
    { ...opts, empty: "None" });
  syncList($("#q-active"), all.filter((i) => i.status === "active" && i.emergency), { ...opts, empty: "None" });
  syncList($("#q-nonemerg"), all.filter((i) => (i.status === "pending" || i.status === "active") && !i.emergency).sort(byAge),
    { ...opts, empty: "None" });
  syncList($("#q-closed"), all.filter((i) => i.status === "closed" || i.status === "dismissed"),
    { ...opts, empty: "None" });
}

// ---- incident detail ------------------------------------------------------------------

function selectIncident(id) {
  if (BOARD) return;                  // the wall board only shows
  leaveView();
  cad.sel = id;
  cad.mode = "view";
  cad.rec = null;
  show($("#edit-form"), false);
  show($("#close-form"), false);
  show($("#upgrade-form"), false);
  show($("#r-preview-box"), false);
  $("#r-full").checked = false;
  flash($("#r-error"));
  flash($("#c-error"));
  render();
  recommend();
  if (window.innerWidth <= 1100) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderDetail() {
  renderTabs();
  if (cad.radio) {                    // the Radio view has the middle column (radio-view.js)
    for (const id of ["#new-form", "#d-empty", "#d-body"]) show($(id), false);
    return;
  }
  const inc = cad.mode === "view" ? selected() : null;
  show($("#new-form"), cad.mode === "new");
  show($("#d-empty"), cad.mode === "none" || cad.mode === "idle" || (cad.mode === "view" && !inc));
  show($("#d-body"), !!inc);
  if (cad.mode === "view" && !inc) {
    setText($("#d-empty .big"), "Not listed");
    return;
  }
  setText($("#d-empty .big"), "No incident");
  if (!inc) return;
  const open = isOpen(inc);
  setText($("#d-number"), incTitle(inc));
  const es = eventStatus(inc);
  stBadge($("#d-status"), es.name, es.bg);
  stBadge($("#d-class"), inc.emergency ? "Emergency" : "Non-emergency", inc.emergency ? "#EE6475" : "#8fb8d8");
  show($("#d-joint"), !!inc.joint_call);
  if (inc.joint_call) stBadge($("#d-joint"), inc.joint_call, "#6AA6E8");
  setText($("#d-source"), inc.source === "manual" ? "Manual" : inc.source);
  show($("#d-accept"), inc.status === "queued");
  show($("#d-dismiss"), inc.status === "queued");
  show($("#d-close"), inc.status === "pending" || inc.status === "active");
  show($("#d-edit"), open);
  if (!(inc.status === "pending" || inc.status === "active")) show($("#close-form"), false);
  show($("#d-queued"), inc.status === "queued");
  if (inc.status === "queued") {
    setText($("#d-qnote"), inc.type_note || "");
    const ts = $("#d-acc-type");
    if (ts._for !== inc.id) {
      ts._for = inc.id;
      ts.value = inc.type || "";
      fillSubtypes($("#d-acc-sub"), ts.value);
      $("#d-acc-sub").value = inc.subtype || "";
    }
  }
  const type = $("#d-type");
  setText(type, typeLabel(inc.type, inc.subtype) || "No type");
  type.style.setProperty("--c", inc.type ? typeColor(inc.type) || "#ff7a5c" : "#E0A030");
  const addr = $("#d-addr");
  addr.className = "d-addr" + (inc.address ? "" : " dim");
  setText(addr, inc.address || "No location");
  setText($("#d-cross"), inc.cross_street ? `Cross: ${inc.cross_street}` : "");
  setText($("#d-geo"), inc.lat != null ? `Map: ${inc.geo_label || `${inc.lat.toFixed(5)}, ${inc.lon.toFixed(5)}`}`
    : "No map point");
  $("#d-geo").className = "geo" + (inc.lat != null ? "" : " warn");
  setText($("#d-created"), fmtTime(inc.created_at, true));
  const elapsed = $("#d-elapsed");
  setText($("#d-elapsed-l"), open ? "Elapsed" : "Duration");
  if (open) since(elapsed, inc.created_at);
  else { since(elapsed, null); setText(elapsed, fmtDur((inc.closed_at || 0) - inc.created_at)); }
  show($("#d-via-w"), !!inc.received_via);
  setText($("#d-via"), inc.received_via ? nameOf("received_via", inc.received_via) : "");
  show($("#d-until-w"), !!inc.until_at);
  setText($("#d-until"), inc.until_at ? fmtTime(inc.until_at) : "");
  show($("#d-disp-w"), !!inc.disposition);
  setText($("#d-disp"), inc.disposition ? nameOf("dispositions", inc.disposition) : "");
  const caller = [inc.caller_name, inc.phone, inc.caller_address].filter(Boolean).join(" / ");
  setText($("#d-caller"), caller ? `Caller: ${caller}` : "");
  setText($("#d-narr"), inc.remarks || "");
  show($("#d-narr"), !!inc.remarks);
  renderMap(inc);
  renderOnUnits(inc);
  show($("#d-attach"), open && inc.status !== "queued");
  renderAttach(inc);
  show($("#d-dispatch"), open && !!inc.emergency);
  renderDispatchBox(inc);
  renderDispatches(inc);
  renderRadio(inc);
  renderEvents(inc);
}

// ---- the incident's radio traffic (sizeup/cad/comms.py) ------------------------------------------
// Every transmission from a radio on a unit assigned to it (dispatched / attached until it cleared),
// FG1 while it had units on it, and 60 Control's turns answering those units in the same P25 call.

const RADIO_WHY = { unit: "Unit", channel: "FG1", dispatcher: "60 Control", manual: "Added" };
function radioRow(t) {
  const li = el("li", "rt");
  const meta = el("div", "meta");
  const b = Icon.play(el("button", "play"));
  b.title = t.has_audio === false ? "Recording expired (12 h)" : "Play";
  b.disabled = t.has_audio === false;
  b.addEventListener("click", () => play(`/api/audio/${encodeURIComponent(t.id)}.wav`));
  const rr = el("button", "rerun", "↻");
  rr.title = "Re-transcribe";
  rr.disabled = t.has_audio === false;
  rr.addEventListener("click", async () => {
    rr.disabled = true;
    setText(rr, "…");
    try {
      const r = await fetch("/api/retranscribe", { method: "POST", headers: { "Content-Type": "application/json" },
                                                   body: JSON.stringify({ ids: [t.id] }) });
      if (!r.ok || !(await r.json()).queued) { setText(rr, "↻"); rr.disabled = false; rr.title = "Nothing to re-run"; }
    } catch (e) { setText(rr, "↻"); rr.disabled = false; }
  });
  meta.append(b, rr, el("span", "t", fmtTime(t.start, true)));
  const tag = Radio.rigTag({ rig: t.rig, radio_id: t.radio_id, unit: t.talker });
  if (tag) meta.append(tag);
  meta.append(el("span", "chip src", t.channel));
  if (t.why) {
    const w = el("span", `why ${t.why}`, RADIO_WHY[t.why] || t.why);
    w.title = t.why === "unit" ? `${t.link_unit || t.rig} was on this incident` : t.why === "channel"
      ? "Unidentified FG1 radio while this was the only call with units" : t.why === "manual"
      ? `Added${t.link_unit ? ` by ${t.link_unit}` : " by hand"}` : `60 Control answering ${t.link_unit || "a unit on it"}`;
    meta.append(w);
  }
  if (t.end > t.start) meta.append(el("span", "dim", `${Math.round(t.end - t.start)} s`));
  if (/^aws-transcribe/.test(t.model || "")) {
    const m = el("span", "chip stt-aws", "AWS");
    m.title = `Transcript: Amazon Transcribe${t.model !== "aws-transcribe" ? ` (${t.model})` : ""}`;
    meta.append(m);
  }
  const txt = el("div", "txt" + (t.text ? "" : " muted"), t.text || sttSaid(t));
  li.append(meta, txt);
  if (t.alt_model) {                     // "both": the other engine's text, to compare (Settings > Radios > Engine)
    const alt = el("div", "txt alt-txt");
    alt.append(el("span", "dim", `${t.alt_model}: `), document.createTextNode(t.alt_text || "(nothing)"));
    const tog = el("button", "mini icon alt-tog", "\u21c4");
    tog.type = "button";
    tog.setAttribute("aria-label", `Compare with ${t.alt_model}`);
    tog.title = `Compare with ${t.alt_model}`;
    const open = () => RADIO_ALT_OPEN.has(t.id);
    const paint = () => { show(alt, open()); tog.classList.toggle("active", open()); tog.setAttribute("aria-pressed", String(open())); };
    tog.addEventListener("click", () => { if (open()) RADIO_ALT_OPEN.delete(t.id); else RADIO_ALT_OPEN.add(t.id); paint(); });
    meta.insertBefore(tog, meta.children[2] || null);
    li.append(alt);
    paint();
  }
  li.classList.toggle("from-dispatch", !!t.dispatcher);
  return li;
}
const RADIO_ALT_OPEN = new Set();       // rows whose second transcript is shown (kept across redraws)
// What a transmission without text says (its status): sizeup/app.py, sizeup/channels.py.
function sttSaid(t) {
  return { no_speech: "Key-up, no speech", pending: "Transcribing", remote: "Transcribing on the hub (AWS)", off: "Not transcribed",
           error: "Transcription failed" }[t.status] || "—";
}
// A note tagged with units ("E63 stated that ..."), as a row among the transmissions.
function noteRow(e) {
  const li = el("li", "rt note");
  const meta = el("div", "meta");
  meta.append(el("span", "t", fmtTime(e.at, true)), el("span", "ek note", "note"));
  for (const u of tagsOf(e)) meta.append(unitChip(u));
  if (e.operator) meta.append(opSpan(e.operator));
  li.append(meta, el("div", "txt", noteText(e)));
  return li;
}
const tagsOf = (e) => (e.units || "").split(",").filter(Boolean);
function unitChip(u) {
  const c = el("button", "unit-tag", u);
  c.type = "button";
  c.title = `Only ${u}`;
  c.dataset.unitFilter = u;
  if (Radio.talkingFor(u)) c.classList.add("talking");
  return c;
}
// "E63 stated that ..." tagged E63 reads "E63 · stated that ...": the tag chip, then the rest.
function noteText(e) {
  const tags = tagsOf(e), m = (e.text || "").match(/^(\S+)\s+(.*)$/s);
  return m && tags.some((u) => u.toLowerCase() === m[1].toLowerCase()) ? m[2] : e.text;
}
function renderRadio(inc) {
  const rows = inc.radio;
  show($("#d-radio-card"), Array.isArray(rows));
  if (!Array.isArray(rows)) return;               // a server without the communications log
  if (cad.radioFor !== inc.id) { cad.radioFor = inc.id; cad.radioUnit = ""; }
  const notes = (inc.events || []).filter((e) => e.kind === "note" && e.units);
  const items = [...rows.map((t) => ({ kind: "radio", at: t.start, key: `r:${t.id}`, units: (t.rig || "").split(/,\s*/).filter(Boolean), t })),
                 ...notes.map((e) => ({ kind: "note", at: e.at, key: `n:${e.id}`, units: tagsOf(e), e }))]
    .sort((a, b) => b.at - a.at);
  const assigned = (inc.assignments || []).map((a) => a.name);
  const all = [...new Set([...assigned, ...items.flatMap((x) => x.units)])];
  const u = cad.radioUnit;
  const shownItems = u ? items.filter((x) => x.units.includes(u)) : items;
  const nr = items.filter((x) => x.kind === "radio").length, nn = items.length - nr;
  setText($("#d-radio-sum"), [nr ? `${nr} TX` : "", nn ? `${nn} note${nn === 1 ? "" : "s"}` : ""].filter(Boolean).join(" / "));
  $("#d-radio-all").href = `#radio?event=${encodeURIComponent(inc.number || inc.id)}` + (u ? `&unit=${encodeURIComponent(u)}` : "");
  const bar = $("#d-radio-units"), sig = JSON.stringify([all, u, all.map(talkSig)]);
  if (bar._sig !== sig) {
    bar._sig = sig;
    const mk = (code, text) => {
      const b = el("button", "mini uf" + (code === u ? " on" : "") + (code && Radio.talkingFor(code) ? " talking" : ""), text);
      b.type = "button";
      b.dataset.unitFilter = code;
      b.setAttribute("aria-pressed", String(code === u));
      return b;
    };
    bar.replaceChildren(mk("", "All"), ...all.map((c) => mk(c, c)));
    show(bar, all.length > 0);
  }
  syncList($("#d-radio"), shownItems, {
    key: (x) => x.key,
    sig: (x) => x.kind === "radio" ? JSON.stringify([x.t.text, x.t.status, x.t.rig, x.t.why, x.t.has_audio, talkSig(x.t.rig)])
      : JSON.stringify([x.e.text, x.e.units]),
    make: () => el("li"),
    fill: (li, x) => {
      const n = x.kind === "radio" ? radioRow(x.t) : noteRow(x.e);
      li.className = n.className;
      li.replaceChildren(...n.childNodes);
    },
    empty: "None",
  });
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-unit-filter]");
  if (!b || !b.closest("#d-body")) return;
  cad.radioUnit = cad.radioUnit === b.dataset.unitFilter ? "" : b.dataset.unitFilter;
  renderRadio(selected());
  $("#d-radio-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
});

// Status buttons (the old console's): one per status in the data file; out of service and
// unmanned ask why first (optional), right there.
// On the board, a working status (dispatched, acknowledged, responding, on location...) asks which
// call: the unit's current one preselected, the other open calls, or none. Moving it takes it off
// the old call (noted on both).
function callPicker(u, where, box) {
  const now = unitById(u.id) || u;
  const st = unitStatus(cad.pick.status);
  const form = el("form", "ask row pick");
  const sel = el("select");
  const open = incidents().filter((i) => ["pending", "active"].includes(i.status));
  const add = (value, text) => { const o = el("option", "", text); o.value = String(value); sel.append(o); };
  const cur = open.find((i) => i.id === now.incident_id);
  if (cur) add(cur.id, `${incTitle(cur)} ${typeLabel(cur.type, cur.subtype) || "No type"}, ${cur.address || ""} (current)`);
  for (const i of open) if (i !== cur) add(i.id, `${incTitle(i)} ${typeLabel(i.type, i.subtype) || "No type"}, ${i.address || ""}`);
  add(0, "No call");
  const ok = el("button", "mini on", `${st.name}`);
  ok.type = "submit";
  statusColors(ok, st);
  const cancel = el("button", "mini", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", (e) => { e.stopPropagation(); cad.pick = null; render(); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = cad.pick.status, iid = Number(sel.value);
    cad.pick = null;
    try {
      await api("POST", `/api/cad/unit/${u.id}/status`, { status, incident_id: iid, operator: operator() });
    } catch (err) { UI.toast(err.message, "err"); }
  });
  form.addEventListener("click", (e) => e.stopPropagation());
  form.append(el("span", "small", `${now.name || ""} ${st.code} on`), sel, ok, cancel);
  box.append(form);
  setTimeout(() => { if (document.activeElement !== sel) sel.focus(); }, 0);
  return box;
}

function statusButtons(u, where) {
  const box = el("div", "ubtns");
  if (where === "board" && cad.pick && cad.pick.uid === u.id) return callPicker(u, where, box);
  if (cad.ask && cad.ask.uid === u.id && cad.ask.where === where) {
    const st = unitStatus(cad.ask.status);
    const form = el("form", "ask row");
    const input = el("input");
    input.type = "text";
    input.maxLength = 200;
    input.placeholder = "Reason (optional)";
    input.value = cad.ask.text || "";
    input.addEventListener("input", () => { cad.ask.text = input.value; });
    const ok = el("button", "mini on", st.name);
    ok.type = "submit";
    statusColors(ok, st);
    const cancel = el("button", "mini", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", (e) => { e.stopPropagation(); cad.ask = null; render(); });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const status = cad.ask.status;
      cad.ask = null;
      setUnitStatus(u.id, status, input.value.trim());
    });
    form.addEventListener("click", (e) => e.stopPropagation());
    form.append(input, ok, cancel);
    box.append(form);
    setTimeout(() => { if (document.activeElement !== input) input.focus(); }, 0);
    return box;
  }
  if ((unitById(u.id) || u).response === "agency") return agencyButtons(u, where, box);
  for (const s of statusChoices(u, where)) {
    if (where === "inc" && s.code === u.status) continue;
    if (s.retired) continue;
    const b = el("button", "mini" + (s.code === u.status ? " on" : ""), s.name);
    statusColors(b, s);
    b.title = s.code;
    b.disabled = s.code === u.status;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      if (s.role === "oos") { cad.ask = { uid: u.id, status: s.code, where, text: "" }; render(); }
      else if (where === "board" && s.role !== "free") { cad.pick = { uid: u.id, status: s.code }; render(); }
      else setUnitStatus(u.id, s.code);
    });
    box.append(b);
  }
  return box;
}

// On an incident only the next steps (cad_lists.json status_flow, sent per assignment as `next`):
// Dispatched -> Responding, On location, Available, Out of service ... The board offers every status.
function statusChoices(u, where) {
  const all = L().unit_statuses || [];
  if (where !== "inc") return all;
  const flow = L().status_flow || {};
  const next = u.next || flow[u.status] || flow["*"];
  if (!next) return all;                               // a server without the flow: as before
  return next.map((c) => all.find((s) => s.code === c)).filter(Boolean);
}
function talkSig(name) {
  const t = Radio.talkingFor(name);
  return t ? `${t.key}|${t.ended ? 1 : 0}` : "";
}

// Another agency's unit (EMS, PD): on location when it shows up, then cleared. Nothing else.
function agencyButtons(u, where, box) {
  const now = unitById(u.id) || u;
  if (!now.incident_id) {
    if (where === "board") box.append(el("span", "dim small", "Attach only"));
    return box;
  }
  const codes = L().agency_statuses || [];
  codes.forEach((code, i) => {
    const s = unitStatus(code);
    if (code === now.status) return;
    const b = statusColors(el("button", "mini", i === codes.length - 1 && s.role === "free" ? "Cleared" : s.name), s);
    b.addEventListener("click", (e) => { e.stopPropagation(); setUnitStatus(u.id, code); });
    box.append(b);
  });
  return box;
}

function renderOnUnits(inc) {
  const rows = (inc.assignments || []).slice().sort((a, b) => (!!a.cleared_at - !!b.cleared_at));
  syncList($("#d-units"), rows, {
    key: (a) => String(a.id),
    sig: (a) => JSON.stringify([a.status, a.dispatched_at, a.enroute_at, a.onscene_at, a.cleared_at, cad.listSig.length,
      (a.next || []).join(), (a.return || []).join(), talkSig(a.name),
      cad.ask && cad.ask.uid === a.unit_id && cad.ask.where === "inc" ? cad.ask.status : null]),
    make: () => el("li", "ou"),
    fill: (li, a) => {
      li.className = "ou" + (a.cleared_at ? " cleared" : "");
      const head = el("div", "ouh");
      head.append(el("b", "", a.name));
      if (!a.cleared_at) head.append(statusChip(a.status));
      const times = el("span", "dim small times");
      times.append(...[["Disp", a.dispatched_at], ["Resp", a.enroute_at], ["OnLoc", a.onscene_at], ["Clr", a.cleared_at]]
        .filter(([, t]) => t).map(([k, t]) => el("span", "", `${k} ${fmtTime(t, true)}`)));
      head.append(times, el("span", "spacer"));
      const talk = !a.cleared_at && Radio.talkingFor(a.name);
      if (talk) head.append(el("span", "talk-badge", `${talk.ended ? "Ended" : "TX"} ${talk.channel}`));
      if (!a.cleared_at && isOpen(inc)) head.append(statusButtons({ id: a.unit_id, status: a.status, next: a.next }, "inc"));
      else if (a.cleared_at && isOpen(inc) && (a.return || []).length) head.append(returnButtons(inc, a));
      li.replaceChildren(head);
    },
    empty: "None",
  });
}

function playForEvent(inc, e) {
  if (e.kind !== "dispatch") return null;
  const d = (inc.dispatches || []).find((x) => Math.abs(x.at - e.at) < 0.001);
  return d && d.has_audio ? d.id : null;
}
const EVENT_KINDS = { note: "comment", status: "status", system: "system", attach: "attached", prealert: "pre-alert" };
function renderEvents(inc) {
  syncList($("#d-events"), (inc.events || []).slice().reverse(), {
    key: (e) => String(e.id),
    sig: (e) => `${e.id}|${e.units || ""}`,
    make: () => el("li", "ev"),
    fill: (li, e) => {
      const meta = el("div", "meta");
      meta.append(el("span", "t", fmtTime(e.at, true)), el("span", `ek ${e.kind}`, EVENT_KINDS[e.kind] || e.kind));
      if (e.operator) meta.append(opSpan(e.operator));
      const txt = el("div", "txt");
      const did = playForEvent(inc, e);
      if (did) {
        const b = Icon.play(el("button", "play mini"));
        b.title = "Play";
        b.addEventListener("click", () => play(`/api/cad/dispatch/${did}.wav`));
        txt.append(b);
      }
      if (e.kind === "note" && e.units) {
        for (const u of tagsOf(e)) txt.append(unitChip(u), document.createTextNode(" "));
        txt.append(document.createTextNode(noteText(e)));
      } else txt.append(document.createTextNode(e.text));
      li.replaceChildren(meta, txt);
    },
    empty: "None",
  });
}

const DISPATCH_KINDS = { dispatch: "dispatch", "add-on": "add-on", upgrade: "upgrade" };
function renderDispatches(inc) {
  show($("#d-dispatches-card"), !!inc.emergency || (inc.dispatches || []).length > 0);
  syncList($("#d-dispatches"), (inc.dispatches || []).slice().reverse(), {
    key: (d) => String(d.id),
    sig: (d) => String(d.id),
    make: () => el("li", "dp"),
    fill: (li, d) => {
      const meta = el("div", "meta");
      meta.append(el("span", "t", fmtTime(d.at, true)), el("span", `ek ${d.kind === "upgrade" ? "upgrade" : "dispatch"}`,
        DISPATCH_KINDS[d.kind] || d.kind || "dispatch"), d.operator ? opSpan(d.operator) : el("span", "", ""),
      el("span", "", d.channel || ""));
      const tx = el("span", d.transmitted ? "txd" : "notx", d.transmitted ? "Transmitted" : "Not transmitted");
      if (!d.transmitted && d.tx_note) tx.title = d.tx_note;
      meta.append(tx);
      const row = el("div", "row");
      if (d.has_audio) {
        const b = Icon.play(el("button", "play"));
        b.title = "Play";
        b.addEventListener("click", () => play(`/api/cad/dispatch/${d.id}.wav`));
        row.append(b);
      }
      row.append(el("div", "txt", d.text));
      li.replaceChildren(meta, row);
      if (d.remarks) li.append(el("div", "small", `Remarks: ${d.remarks}`));
      if (d.tones) li.append(el("div", d.tones.includes("not set") ? "warn small" : "dim small", `Tones: ${d.tones}`));
      if (d.audio_note) li.append(el("div", "dim small", d.audio_note));
    },
    empty: "None",
  });
}

// ---- recommendation / preview / dispatch / upgrade ----------------------------------------------

async function recommend(typ, sub) {
  // By id, not from the last push: a just-created incident isn't in the state yet.
  const iid = cad.sel, inc = selected();
  if (iid === null || (inc && (!isOpen(inc) || !inc.emergency))) { cad.rec = null; if (inc) renderDispatchBox(inc); return; }
  const body = typ ? { type: typ, subtype: sub || "" } : {};
  const prev = cad.rec && cad.rec.iid === iid ? cad.rec : null;
  try {
    const r = await api("POST", `/api/cad/incident/${iid}/recommend`, body);
    if (cad.sel !== iid) return;
    cad.rec = { iid, ...r, checked: new Set(r.units.map((u) => u.id)), extra: [], forType: typ || "" };
    syncFull(inc, prev, cad.rec);
  } catch (e) {
    cad.rec = { iid, units: [], unfilled: [], already: [], card: null, note: "", checked: new Set(), extra: [], error: e.message };
  }
  cad.rec.sig = null;
  renderDispatchBox(selected());
}

// A card with the full department ticks "Full department response" (said instead of each unit): on a
// first dispatch, or in the upgrade for the new type's card. Only when the card changes, so an operator's
// untick sticks through a refresh.
function syncFull(inc, prev, rec) {
  const was = prev && prev.forType === rec.forType ? !!prev.full : null;
  if (was === !!rec.full) return;
  if (rec.forType) { if (rec.full) $("#u-full").checked = true; return; }
  if (!rec.full) { if (was) $("#r-full").checked = false; return; }
  if (!inc || !(inc.dispatches || []).length) $("#r-full").checked = true;
}

function units() {
  return (cad.state && cad.state.units) || [];
}
function unitById(id) {
  return units().find((u) => u.id === id);
}
function canDispatch(u) {
  return roleOf(u.status) !== "oos" && u.response !== "agency";
}

const REMARKS_LONG = 200, REMARKS_MAX = 500;
function remarksNote() {
  const n = $("#r-remarks").value.trim().length, note = $("#r-remarks-note");
  note.className = n > REMARKS_LONG ? "warn small" : "dim small";
  setText(note, n > REMARKS_MAX ? `${n}/${REMARKS_MAX}: too long`
    : n > REMARKS_LONG ? `${n}/${REMARKS_MAX}: long, read twice` : n ? `${n}/${REMARKS_MAX}` : "");
}
// The box starts from the event's remarks for its first dispatch only, and empty after each dispatch
// (an add-on shouldn't repeat what was already said) and for 60 Control pages (their remarks are the
// page transcript, not something to read out again). The operator's edits stick until they dispatch.
function remarksPrefill(inc) {
  if ((inc.dispatches || []).length) return "";
  if (/60 control/i.test(inc.source || "") || /60 control/i.test(inc.received_via || "")) return "";
  return inc.remarks || "";
}
function syncRemarks(inc) {
  const n = (inc.dispatches || []).length, base = remarksPrefill(inc);
  const r = cad.remarks;
  if (r && r.iid === inc.id && r.n === n && (r.edited || r.base === base)) return;
  cad.remarks = { iid: inc.id, n, base, edited: false };
  $("#r-remarks").value = base;
  remarksNote();
}

function renderDispatchBox(inc) {
  if (!inc || !isOpen(inc) || !inc.emergency) return;
  syncRemarks(inc);
  show($("#r-pending"), inc.status === "queued");
  $("#r-dispatch").disabled = inc.status === "queued";
  const addOn = (inc.dispatches || []).length > 0;
  show($("#r-kind"), addOn);
  const jq = jointQuick();
  show($("#r-joint"), !!jq);
  if (jq) { setText($("#r-joint"), `Add ${jq.town}`); $("#r-joint").title = `Tick ${jq.quick_add.join(", ")} if available`; }
  show($("#u-open"), addOn && inc.status !== "queued");
  if (!$("#r-dispatch")._busy) setText($("#r-dispatch"), addOn ? "Dispatch add-on" : "Dispatch");
  const rec = cad.rec && cad.rec.iid === inc.id ? cad.rec : null;
  setText($("#r-card"), rec && rec.card ? `Card: ${rec.card_label || rec.card}${rec.forType ? " (new type)" : ""}` : "");
  const warn = rec ? [...(rec.error ? [rec.error] : []), ...(rec.note ? [rec.note] : []), ...rec.unfilled] : [];
  $("#r-unfilled").replaceChildren(...warn.map((w) => el("li", "", w)));
  const order = rec && rec.order;
  setText($("#r-order"), !order || !rec.units.length ? ""
    : order.by === "nearest" ? "Order: nearest first (est.)" : `Order: ${order.note}`);
  const rows = rec ? [...rec.units, ...rec.extra.map((id) => ({ ...unitById(id), slot: "added" }))] : [];
  const sig = JSON.stringify([rows.map((u) => u.id), rec && [...rec.checked], rec && rec.already,
    rows.map((u) => { const x = unitById(u.id); return x ? [x.status, x.incident_id] : null; }), cad.listSig.length]);
  if (rec && rec.sig === sig) return;
  if (rec) rec.sig = sig;
  const list = $("#r-units");
  if (!rec) { list.replaceChildren(el("li", "empty", "Loading")); return; }
  const items = rows.map((u) => {
    const now = unitById(u.id) || u;
    const li = el("li", "ru");
    const lab = el("label", "row");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = rec.checked.has(u.id);
    cb.addEventListener("change", () => {
      if (cb.checked) rec.checked.add(u.id); else rec.checked.delete(u.id);
      rec.sig = null;
    });
    lab.append(cb, el("b", "", u.name), el("span", "dim small", u.slot || ""));
    if (u.reason) lab.append(etaHint(u));
    lab.append(el("span", "spacer"), statusChip(now.status));
    if (now.spare) lab.append(el("span", "chip spare", "spare"));
    if (now.incident_id && now.incident_id !== inc.id) lab.append(el("span", "warn small", `On ${now.incident_number}: moves`));
    li.append(lab);
    return li;
  });
  if (!items.length) {
    items.push(el("li", "empty", rec.already.length ? `On it: ${rec.already.join(", ")}. Card filled` : "None"));
  } else if (rec.already.length) {
    items.push(el("li", "dim small", `On it: ${rec.already.join(", ")}`));
  }
  list.replaceChildren(...items);
  // Add any other unit (not out of service or unmanned, not already listed or on this incident).
  const listed = new Set(rows.map((u) => u.id));
  const others = units().filter((u) => !listed.has(u.id) && canDispatch(u) && u.incident_id !== inc.id && (u.board || u.incident_id));
  const hints = (rec && rec.others) || {};
  fillSelect($("#r-add"), others.map((u) => [String(u.id),
    `${u.name} ${u.status}${u.incident_number ? ` on ${u.incident_number}` : ""}`
    + (hints[u.id] ? `, ${etaSplit(hints[u.id]).join(" ")}` : "")]),
  others.length ? "Add unit" : "None available");
  const ch = $("#r-channel");
  const chans = (cad.state.transmit && cad.state.transmit.channels) || [];
  if (ch.options.length !== chans.length) fillSelect(ch, chans.map((c) => [c, c]));
}

// runcards.estimates' "~2 min est. · returning from 26-000004" shown as "≈2 min returning from 26-000004":
// the estimate (still marked as one) in bold, then where from.
function etaSplit(u) {
  const [t, ...why] = u.eta ? u.reason.split(" · ") : ["", u.reason];
  return [t.replace(/^~(\d+) min est\.$/, "≈$1 min"), why.join(" ")].filter(Boolean);
}
function etaHint(u) {
  const [t, why] = u.eta ? etaSplit(u) : ["", u.reason];
  const span = el("span", "eta" + (/^returning|^near/.test(why || "") ? " ret" : "") + (u.eta ? "" : " unk"));
  if (t) span.append(el("b", "", t), document.createTextNode(why ? ` ${why}` : ""));
  else span.textContent = why || "";
  span.title = u.eta ? "Estimate: straight line × road factor at average speed, plus turnout" : "No estimate";
  return span;
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
  const free = (L().unit_statuses || []).filter((s) => s.role === "free").map((s) => s.code);
  const listed = new Set([...cad.rec.units.map((u) => u.id), ...cad.rec.extra]);
  for (const u of units()) {
    if (!kinds.includes(u.kind) || u.response !== "fire" || u.mutual_aid || jointOf(u.name) || !free.includes(u.status) || u.incident_id || !u.board) continue;
    if (!listed.has(u.id)) cad.rec.extra.push(u.id);
    cad.rec.checked.add(u.id);
  }
  cad.rec.sig = null;
  renderDispatchBox(inc);
}

function jointQuick() {              // the first joint-response town with a quick-add list
  const e = Object.entries(L().joint_response || {}).find(([, j]) => (j.quick_add || []).length);
  return e ? { town: e[0], ...e[1] } : null;
}
function addJoint() {                 // "Add Rye Brook (E14, L30, 2662)": tick those that can go
  const inc = selected(), j = jointQuick();
  flash($("#r-error"));
  if (!inc || !cad.rec || !j) return;
  const free = (L().unit_statuses || []).filter((s) => s.role === "free").map((s) => s.code);
  const listed = new Set([...cad.rec.units.map((u) => u.id), ...cad.rec.extra]);
  const skipped = [];
  for (const code of j.quick_add) {
    const u = units().find((x) => x.name === code);
    if (!u || u.incident_id === inc.id) continue;
    if (!free.includes(u.status) || u.incident_id) { skipped.push(`${code} ${u.incident_number ? `on ${u.incident_number}` : unitStatus(u.status).name.toLowerCase()}`); continue; }
    if (!listed.has(u.id)) cad.rec.extra.push(u.id);
    cad.rec.checked.add(u.id);
  }
  if (skipped.length) flash($("#r-error"), `Not available: ${skipped.join(", ")}`);
  cad.rec.sig = null;
  renderDispatchBox(inc);
}

function showPreview(title, text, url, note, r) {
  show($("#r-preview-box"), true);
  setText($("#r-preview-title"), title);
  setText($("#r-preview-text"), text);
  const warns = (r && r.tone_warnings) || [];
  $("#r-preview-tones").className = warns.length ? "warn small" : "dim small";
  setText($("#r-preview-tones"), r && r.tones ? `Tones: ${r.tones}${warns.length ? `. ${warns.join("; ")}` : ""}` : "");
  const a = $("#r-audio");
  show(a, !!url);
  if (url) { a.src = url; a.play().catch(() => {}); }
  setText($("#r-audio-note"), note || "");
}

async function busy(button, label, fn) {
  const was = button.textContent;
  button.disabled = true;
  button._busy = true;
  button.setAttribute("aria-busy", "true");
  setText(button, label);
  try { await fn(); } finally {
    button.disabled = false; button._busy = false; button.removeAttribute("aria-busy"); setText(button, was); render();
  }
}

async function preview() {
  const inc = selected();
  flash($("#r-error"));
  if (!inc) return;
  const ids = checkedIds();
  if (!ids.length) return flash($("#r-error"), "Tick a unit");
  await busy($("#r-preview"), "Rendering", async () => {
    try {
      const r = await api("POST", `/api/cad/incident/${inc.id}/preview`,
        { unit_ids: ids, full: $("#r-full").checked, remarks: $("#r-remarks").value });
      showPreview(`Preview${r.kind === "add_on" ? " add-on" : ""}: not logged`, r.text, r.audio_url, r.audio_note, r);
    } catch (e) {
      flash($("#r-error"), e.message);
    }
  });
}

function needOperator(errNode) {
  const op = operator();
  if (!op) { ($("#operator-pick").hidden ? $("#operator") : $("#operator-pick")).focus(); flash(errNode, "Enter operator name (top right)"); }
  return op;
}

async function dispatch() {
  const inc = selected();
  flash($("#r-error"));
  if (!inc) return;
  const op = needOperator($("#r-error"));
  if (!op) return;
  const ids = checkedIds();
  if (!ids.length) return flash($("#r-error"), "Tick a unit");
  await busy($("#r-dispatch"), "Dispatching", async () => {
    try {
      const r = await api("POST", `/api/cad/incident/${inc.id}/dispatch`,
        { unit_ids: ids, operator: op, channel: $("#r-channel").value, full: $("#r-full").checked, remarks: $("#r-remarks").value });
      const how = r.transmitted ? `transmitted on ${r.channel}` : "logged, not transmitted";
      showPreview(`${r.kind === "add-on" ? "Add-on" : "Dispatch"} #${r.dispatch_id}: ${how}`
        + (r.moved.length ? `. Moved ${r.moved.join(", ")}` : ""), r.text, r.audio_url, r.audio_note, r);
      $("#r-full").checked = false;
      setTimeout(() => recommend(), 300);
    } catch (e) {
      flash($("#r-error"), e.message);
    }
  });
}

function upgradeBody() {
  const t = $("#u-type").value;
  return { type: t, subtype: t ? $("#u-subtype").value : "", info: $("#u-info").value.trim(), remarks: $("#u-remarks").value,
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
  await busy(b, previewOnly ? "Rendering" : "Sending", async () => {
    try {
      const r = await api("POST", `/api/cad/incident/${inc.id}/upgrade`, body);
      if (previewOnly) showPreview("Preview upgrade: not logged", r.text, r.audio_url, r.audio_note, r);
      else {
        const how = r.transmitted ? `transmitted on ${r.channel}` : "logged, not transmitted";
        showPreview(`Upgrade #${r.dispatch_id}: ${how}`, r.text, r.audio_url, r.audio_note, r);
        $("#upgrade-form").reset();
        show($("#upgrade-form"), false);
        setTimeout(() => recommend(), 300);
      }
    } catch (err) {
      flash($("#u-error"), err.message);
    }
  });
}

// ---- unit tags in the comment box -------------------------------------------------------------
// "@" starts a tag (user, 2026-10-07: "the add a unit should be with an @E63 to trigger it"): "@",
// "@E6", "@e63" offer the units on this incident first, then the fire units, then the rest (the
// server's order, inc.tag_units). Tab or Enter takes the highlighted one, arrows move, Esc closes.
// A taken code is a tag: the note is stored with it (who said it). "@E63" typed out in full counts
// too; a bare "E63" is just text, and so is an address like "a@b".

const noteTag = { tags: new Set(), items: [], idx: 0, tok: null };
function tagOrder(inc) {
  if (inc && Array.isArray(inc.tag_units)) return inc.tag_units;
  const on = ((inc && inc.assignments) || []).map((a) => a.name);
  return [...new Set([...on, ...units().filter((u) => u.board).map((u) => u.name)])];
}
function tokenAt(input) {              // the "@code" being typed at the caret (start: the "@")
  const pos = input.selectionStart ?? input.value.length;
  const m = input.value.slice(0, pos).match(/(?:^|\s)@([A-Za-z0-9-]*)$/);
  return m ? { text: m[1], start: pos - m[1].length - 1, end: pos } : null;
}
function mentionsOf(text) {             // codes written as "@code" (upper case)
  return new Set([...text.matchAll(/(?:^|\s)@([A-Za-z0-9-]+)/g)].map((m) => m[1].toUpperCase()));
}
function hideSugg() {
  const box = $("#note-sugg");
  noteTag.items = [];
  show(box, false);
  $("#note-text").setAttribute("aria-expanded", "false");
}
function suggestUnits() {
  const input = $("#note-text"), tok = tokenAt(input);
  const order = tagOrder(selected());
  const items = tok ? order.filter((c) => c.toLowerCase().startsWith(tok.text.toLowerCase())).slice(0, 8) : [];
  if (!items.length || (items.length === 1 && items[0] === tok.text && noteTag.tags.has(items[0]))) return hideSugg();
  if (noteTag.items.join() !== items.join()) noteTag.idx = 0;
  noteTag.items = items;
  noteTag.tok = tok;
  const on = new Set(((selected() || {}).assignments || []).filter((a) => !a.cleared_at).map((a) => a.name));
  const box = $("#note-sugg");
  box.replaceChildren(...items.map((c, i) => {
    const li = el("li", i === noteTag.idx ? "on" : "");
    li.id = `note-sugg-${i}`;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", String(i === noteTag.idx));
    li.append(el("b", "", c));
    const u = units().find((x) => x.name === c);
    if (on.has(c)) li.append(el("span", "town ours", "Assigned"));
    else if (u && (u.title || u.department)) li.append(el("span", "dim small", u.title || u.department));
    li.addEventListener("mousedown", (e) => { e.preventDefault(); takeUnit(c); });
    return li;
  }));
  box.append(el("li", "hint dim small", "Tab to tag, Esc to close"));
  const fresh = box.hidden;
  show(box, true);
  if (fresh) box.scrollIntoView({ block: "nearest" });
  input.setAttribute("aria-expanded", "true");
  input.setAttribute("aria-activedescendant", `note-sugg-${noteTag.idx}`);
}
function takeUnit(code) {
  const input = $("#note-text"), tok = noteTag.tok || tokenAt(input);
  if (!tok) return;
  const after = input.value.slice(tok.end).replace(/^[A-Za-z0-9-]*/, "");
  const lead = input.value.slice(0, tok.start) + "@" + code + (after.startsWith(" ") ? "" : " ");
  input.value = lead + after;
  input.setSelectionRange(lead.length, lead.length);
  noteTag.tags.add(code);
  renderNoteTags();
  hideSugg();
}
function noteKeys(e) {
  if (!noteTag.items.length) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    noteTag.idx = (noteTag.idx + (e.key === "ArrowDown" ? 1 : -1) + noteTag.items.length) % noteTag.items.length;
    suggestUnits();
  } else if (e.key === "Tab" || e.key === "Enter") {
    e.preventDefault();
    takeUnit(noteTag.items[noteTag.idx]);
  } else if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    hideSugg();
  }
}
function syncNoteTags() {               // a tag whose @code was deleted goes; an @code typed out in full comes
  const text = $("#note-text").value, said = mentionsOf(text);
  for (const t of [...noteTag.tags]) if (!said.has(t.toUpperCase())) noteTag.tags.delete(t);
  const tok = tokenAt($("#note-text"));
  for (const c of tagOrder(selected())) {
    if (said.has(c.toUpperCase()) && !(tok && tok.text.toUpperCase() === c.toUpperCase())) noteTag.tags.add(c);
  }
  renderNoteTags();
}
function noteUnits(text) {
  const said = mentionsOf(text);
  return [...noteTag.tags].filter((t) => said.has(t.toUpperCase()));
}
function renderNoteTags() {
  const box = $("#note-tags");
  const tags = [...noteTag.tags];
  box.replaceChildren(el("span", "dim small", "Tagged"), ...tags.map((t) => {
    const b = el("button", "unit-tag on", `${t} ✕`);
    b.type = "button";
    b.title = `Untag ${t}`;
    b.addEventListener("click", () => { noteTag.tags.delete(t); renderNoteTags(); });
    return b;
  }));
  show(box, tags.length > 0);
}

// ---- non-emergency: attach units --------------------------------------------------------------

function renderAttach(inc) {
  if (!inc || !isOpen(inc)) return;
  // An emergency event: only other agencies (EMS, PD) that show up; they are never dispatched.
  setText($("#a-title"), inc.emergency ? "Other agencies" : "Attach");
  $("#a-hint").dataset.help = inc.emergency ? "EMS, PD, utilities: attach them when they arrive. Never dispatched or toned out."
    : "Non-emergency: no run card, no dispatch.";
  const others = units().filter((u) => u.incident_id !== inc.id && (u.board || u.incident_id)
    && (!inc.emergency || u.response === "agency"));
  const sig = JSON.stringify([inc.emergency, others.map((u) => [u.id, u.status, u.incident_number])]);
  const sel = $("#a-unit");
  if (sel._sig === sig) return;
  sel._sig = sig;
  fillSelect(sel, others.map((u) => [String(u.id),
    `${u.name} ${u.status}${u.incident_number ? ` on ${u.incident_number}` : ""}`]), "Unit");
}
async function attach() {
  const inc = selected(), id = Number($("#a-unit").value);
  flash($("#a-error"));
  if (!inc || !id) return flash($("#a-error"), "Pick a unit");
  try {
    await api("POST", `/api/cad/incident/${inc.id}/attach`, { unit_ids: [id], operator: operator() });
    $("#a-unit").value = "";
  } catch (e) {
    flash($("#a-error"), e.message);
  }
}

// ---- incident actions --------------------------------------------------------------------

async function act(action, body = {}, errNode = null) {
  const inc = selected();
  if (!inc) return null;
  try {
    const r = await api("POST", `/api/cad/incident/${inc.id}/${action}`, { operator: operator(), ...body });
    if (action === "accept") setTimeout(() => recommend(), 300);
    return r;
  } catch (e) {
    if (errNode) flash(errNode, e.message); else UI.toast(e.message, "err");
    return null;
  }
}

async function closeIncident(e) {
  e.preventDefault();
  flash($("#c-error"));
  const d = $("#c-disp").value;
  if (!d) return flash($("#c-error"), "Pick a disposition");
  const r = await act("close", { disposition: d }, $("#c-error"));
  if (r) { $("#c-disp").value = ""; show($("#close-form"), false); }
}

// A unit that cleared a call that's still open goes back to it (responding, then on location).
async function returnUnit(iid, uid, status) {
  try {
    await api("POST", `/api/cad/incident/${iid}/return`, { unit_id: uid, status, operator: operator() });
  } catch (e) {
    UI.toast(e.message, "err");
  }
}
function returnButtons(inc, a) {
  const box = el("div", "ubtns");
  for (const code of a.return || []) {
    const s = unitStatus(code);
    const b = statusColors(el("button", "mini", s.name), s);
    b.title = `${a.name} back to this call: ${s.name}`;
    b.addEventListener("click", (e) => { e.stopPropagation(); returnUnit(inc.id, a.unit_id, code); });
    box.append(b);
  }
  return box;
}

async function setUnitStatus(uid, status, note = "") {
  try {
    await api("POST", `/api/cad/unit/${uid}/status`, { status, operator: operator(), note });
  } catch (e) {
    UI.toast(e.message, "err");
  }
}

// ---- location box with gazetteer suggestions -------------------------------------------------

function addressBox(input, geoNote, list) {
  const box = { picked: null, timer: null, seq: 0 };
  const ours = () => ((L().geo_town_order || []).slice(0, 2));
  const note = () => {
    geoNote.className = "geo span2" + (box.picked ? "" : " warn");
    if (box.picked) setText(geoNote, `Map: ${box.picked.label} (${box.picked.kind}, ${Math.round(box.picked.confidence * 100)}%)`);
    else if (!input.value.trim()) setText(geoNote, "");
    else if (cad.state && !cad.state.geocoder) setText(geoNote, "No geocoder: no map point");
    else setText(geoNote, "No match picked: saved as typed");
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
        li.append(el("span", "dim small", `${m.kind} ${Math.round(m.confidence * 100)}%`));
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          box.picked = m;
          input.value = m.label;
          show(list, false);
          note();
        });
        return li;
      }));
      if (!ms.length) list.replaceChildren(el("li", "dim", r.error || "No match"));
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

// ---- new incident / edit ----------------------------------------------------------------

function entryClass() {
  const t = $("#f-type").value, s = $("#f-subtype").value;
  const auto = !t || isEmergency(t, s);
  if (!cad.emergTouched) $("#f-emergency").checked = auto;
  setText($("#f-class"), t ? (cad.emergTouched && $("#f-emergency").checked !== auto
    ? `(type says ${auto ? "emergency" : "non-emergency"})` : "(by type)") : "");
  show($("#f-until-w"), !$("#f-emergency").checked);
}

function openNew() {
  leaveView();
  cad.mode = "new";
  cad.sel = null;
  cad.rec = null;
  $("#new-form").reset();
  newAddr.set("", null);
  cad.emergTouched = false;
  fillSubtypes($("#f-subtype"), "");
  entryClass();
  flash($("#f-error"));
  render();
  if (window.innerWidth <= 1100) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#f-type").focus();
}

async function createIncident(e) {
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
  if (!body.type) { $("#f-type").focus(); return flash($("#f-error"), "Pick a type"); }
  try {
    const inc = await api("POST", "/api/cad/incident", body);
    selectIncident(inc.id);
  } catch (err) {
    flash($("#f-error"), err.message);
  }
}

function keepOption(sel, code, label, why = "retired") {   // a retired type (or a subtype off the map) stays
  code = code || "";                                          // shown on the event that has it
  if (code && ![...sel.options].some((o) => o.value === code)) {
    const o = el("option", "", `${label} (${why})`);
    o.value = code;
    sel.append(o);
  }
  sel.value = code;
}
function keepSubtype(sel, t, s) {      // an old event's (or card's) subtype the map no longer allows: shown, not offered
  const gone = (L().event_subtypes || []).some((x) => x.code === s && x.retired);
  keepOption(sel, s, nameOf("event_subtypes", s), gone ? "retired" : `not for ${nameOf("event_types", t)}`);
  sel.disabled = sel.options.length < 2;
}

function openEdit() {
  const inc = selected();
  if (!inc) return;
  keepOption($("#e-type"), inc.type, typeLabel(inc.type));
  fillSubtypes($("#e-subtype"), inc.type);
  keepSubtype($("#e-subtype"), inc.type, inc.subtype);
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

function renderMap(inc) {               // the working event's map: cadmap.js (every call and rig, focused on this one)
  if (window.CadMap) CadMap.detail(inc);
}

// ---- unit board ---------------------------------------------------------------------------

function unitGroups() {
  // On a board (or off the boards but still committed), in the data file's order. The joint-response
  // units (Rye Brook's) are direct units: grouped by kind with PCFD's, tagged. [key, title, rows, board]
  const all = units().filter((u) => u.board || u.incident_id);
  const boards = L().unit_boards || [];
  const taken = new Set();
  const groups = [];
  const take = (key, title, pred, board) => {
    const rows = all.filter((u) => !taken.has(u.id) && pred(u));
    rows.forEach((u) => taken.add(u.id));
    if (rows.length) groups.push([key, title, rows, board]);
  };
  for (const b of boards) {
    if (b.group_by === "kind") {
      for (const [title, kinds] of KIND_GROUPS) {
        take(`${b.id}/${title}`, title, (u) => u.board === b.id && !u.mutual_aid && u.response !== "agency" && kinds.includes(u.kind), b);
      }
      take(`${b.id}/rest`, b.rest_title || b.title, (u) => u.board === b.id, b);
    } else {
      take(b.id, b.title, (u) => u.board === b.id, b);
    }
  }
  take("off", "Off board, on a call", () => true, null);
  return groups;
}

function renderUnits() {
  const box = $("#units");
  const groups = unitGroups();
  if (!groups.length) {
    if (!box.querySelector(".empty")) box.replaceChildren(el("div", "empty", "None"));
    return;
  }
  box.querySelector(":scope > .empty")?.remove();
  const keys = groups.map(([k]) => k);
  for (const g of [...box.querySelectorAll(":scope > .ugroup")]) if (!keys.includes(g.dataset.group)) g.remove();
  groups.forEach(([key, title, all, board], i) => {
    const fold = !!(board && board.folded);
    let g = [...box.children].find((c) => c.dataset.group === key);
    if (!g) {
      g = el("section", "ugroup");
      g.dataset.group = key;
      const h = el("h3", "", title);
      if (fold) {                               // folded: shown once opened (or for the units on a call)
        h.className = "fold";
        h.tabIndex = 0;
        h.setAttribute("role", "button");
        const flip = () => {
          if (cad.openBoards.has(board.id)) cad.openBoards.delete(board.id); else cad.openBoards.add(board.id);
          renderUnits();
        };
        h.addEventListener("click", flip);
        h.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
      }
      g.append(h, el("ol", "list ulist"));
    }
    let rows = all;
    if (fold) {
      const open = cad.openBoards.has(board.id);
      const on = all.filter((u) => u.incident_id);
      if (!open) rows = on;
      g.classList.toggle("folded", !open && !on.length);
      g.querySelector("h3").setAttribute("aria-expanded", String(open));
      setText(g.querySelector("h3"), `${open ? "▾" : "▸"} ${title} (${all.length}${on.length ? `, ${on.length} on calls` : ""})`);
    }
    if (box.children[i] !== g) box.insertBefore(g, box.children[i] || null);
    syncList(g.querySelector("ol"), rows, {
      key: (u) => String(u.id),
      sig: (u) => JSON.stringify([u.status, u.status_at, u.incident_number, u.out_of_service, u.spare, cad.openUnit === u.id,
        talkSig(u.name),
        cad.ask && cad.ask.uid === u.id && cad.ask.where === "board" ? cad.ask.status : null, cad.listSig.length,
        cad.pick && cad.pick.uid === u.id ? cad.pick.status : null]),
      make: () => {
        const li = el("li", "urow item");
        const flip = () => {
          const id = Number(li.dataset.key);
          cad.openUnit = cad.openUnit === id ? null : id;
          if (cad.ask && cad.ask.where === "board") cad.ask = null;
          renderUnits();
        };
        if (!BOARD) {
          li.tabIndex = 0;
          li.addEventListener("keydown", (e) => {
            if (e.target === li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); flip(); }
          });
        }
        li.addEventListener("click", (e) => {
          if (e.target.closest("button, a, input, form")) return;
          flip();
        });
        return li;
      },
      fill: (li, u) => {
        const oos = roleOf(u.status) === "oos";
        const talk = Radio.talkingFor(u.name);
        li.className = "urow item" + (cad.openUnit === u.id ? " open" : "") + (oos ? " oos" : "")
          + (talk ? (talk.ended ? " talked" : " talking") : "");
        const head = el("div", "uh");
        const name = el("b", "un", u.name);
        name.title = [u.title, u.spoken && u.spoken !== u.name ? `said "${u.spoken}"` : "", u.department, u.spare ? "spare" : ""]
          .filter(Boolean).join(", ");
        head.append(name);
        const j = jointOf(u.name);
        if (j) { const t = el("span", "chip joint", j.tag); t.title = `${j.town}, joint response`; head.append(t); }
        else if (u.mutual_aid && u.department
          && !((L().unit_boards || []).find((b) => b.id === u.board)?.title || "").includes(u.department)) {
          head.append(el("span", "chip dept", u.department));       // not already said by its group's title
        }
        if (u.title) { const t = el("span", "utitle dim small", u.title); t.title = u.title; head.append(t); }
        if (!BOARD) li.setAttribute("aria-expanded", String(cad.openUnit === u.id));
        if (u.spare) head.append(el("span", "chip spare", "spare"));
        if (talk) {
          const tb = el("span", "talk-badge", `${talk.ended ? "Ended" : "TX"} ${talk.channel}`);
          tb.title = [talk.radio_id ? `Radio ${talk.radio_id}` : "", talk.label || talk.alias || ""].filter(Boolean).join(", ");
          head.append(tb);
        }
        head.append(el("span", "spacer"), statusChip(u.status), since(el("span", "dim small ut"), u.status_at));
        li.replaceChildren(head);
        const sub = [];
        if (u.incident_number) {
          const a = el("a", "inc-link", u.incident_number);
          a.href = "#";
          a.addEventListener("click", (e) => { e.preventDefault(); selectIncident(u.incident_id); });
          sub.push(a);
        }
        if (oos && u.out_of_service) sub.push(el("span", "why", u.out_of_service));
        if (sub.length) { const d = el("div", "usub"); d.append(...sub); li.append(d); }
        if (cad.openUnit === u.id) li.append(statusButtons(u, "board"));
      },
      empty: "None",
    });
  });
}

function renderUnitLog() {
  syncList($("#unit-log"), (cad.state && cad.state.unit_log) || [], {
    key: (e) => String(e.id), sig: (e) => String(e.id), make: () => el("li", "ev"),
    fill: (li, e) => {
      const meta = el("div", "meta");
      meta.append(el("span", "t", fmtTime(e.at, true)));
      if (e.operator) meta.append(opSpan(e.operator));
      li.replaceChildren(meta, el("div", "txt", e.text));
    },
    empty: "None",
  });
}

// ---- records (incident database, CSV, counts) and lists ---------------------------------------

function openOverlay(id, on) {        // a dialog (ui.js: the focus goes in, stays in, and comes back)
  if (on) UI.dialog.open($(id)); else UI.dialog.close($(id));
}

async function renderRecords() {
  if ($("#db-overlay").hidden || Date.now() - cad.dbAt < 3000) return;
  cad.dbAt = Date.now();
  try {
    const [h, a] = await Promise.all([api("GET", "/api/cad/history"), api("GET", "/api/cad/admin")]);
    syncList($("#db-rows"), h.incidents, {
      key: (i) => String(i.id),
      sig: (i) => JSON.stringify([i.status, i.type, i.subtype, i.address, i.disposition, i.units, i.closed_at, cad.listSig.length]),
      make: () => {
        const tr = el("tr", "ev");
        tr.tabIndex = 0;
        tr.addEventListener("click", () => openFromRecords(Number(tr.dataset.key)));
        tr.addEventListener("keydown", (e) => { if (e.key === "Enter") openFromRecords(Number(tr.dataset.key)); });
        return tr;
      },
      fill: (tr, i) => {
        const type = el("td");
        const t = el("span", "qt", typeLabel(i.type, i.subtype) || "—");
        if (i.type) t.style.setProperty("--c", typeColor(i.type) || "#9AA3AD");
        type.append(t);
        const st = el("td");
        const es = eventStatus(i);
        st.append(stBadge(el("span"), es.name, es.bg));
        tr.replaceChildren(el("td", "num", i.number), type, el("td", "loc", i.address || "—"), el("td", "t", fmtRecv(i.created_at)),
          el("td", "t", fmtRecv(i.closed_at)), st, el("td", "", i.disposition ? nameOf("dispositions", i.disposition) : ""),
          el("td", "", (i.units || []).join(", ")), el("td", "", i.received_via ? nameOf("received_via", i.received_via) : ""));
      },
      empty: "None", cols: 9,
    });
    const rows = Object.entries(a.counts).map(([k, v]) => [k, String(v)]).concat([["Next number", a.next_number], ["Database", a.db]]);
    $("#admin").replaceChildren(...rows.map(([k, v]) => { const d = el("div"); d.append(el("dt", "", k), el("dd", "", v)); return d; }));
  } catch (e) {
    const tr = el("tr");
    tr.append(Object.assign(el("td", "empty", `Load failed: ${e.message}`), { colSpan: 9 }));
    $("#db-rows").replaceChildren(tr);
  }
}

// Records open an older incident that isn't in the live state.
async function openFromRecords(id) {
  if (!incidents().some((i) => i.id === id)) {
    try { cad.extra.set(id, await api("GET", `/api/cad/incident/${id}`)); } catch (e) { UI.toast(e.message, "err"); return; }
  }
  openOverlay("#db-overlay", false);
  selectIncident(id);
}

// ---- run card editor -----------------------------------------------------------------------

function describeSlot(s) {
  return s.all ? "full department" : s.unit ? s.unit : `${s.count} × ${s.kind.replace("|", "/")}`;
}

function renderRunCards() {
  if ($("#settings").hidden || $("#s-runcards").hidden) return;
  const cards = (cad.state && cad.state.run_cards) || [];
  show($("#rc-banner"), !!(cad.state && cad.state.run_cards_default));
  syncList($("#rc-list"), cards, {
    key: (c) => String(c.id),
    sig: (c) => JSON.stringify([c.type, c.slots, c.is_default, c.label, c.phrase, cad.rcEditing === c.type, cad.listSig.length]),
    make: () => {
      const li = el("li", "rc item");
      li.tabIndex = 0;
      li.addEventListener("click", () => editCard(li._card));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") editCard(li._card); });
      return li;
    },
    fill: (li, c) => {
      li._card = c;
      li.className = "rc item" + (cad.rcEditing === c.type ? " sel" : "");
      const h = el("div", "row");
      const [t, s] = c.type.split("/");
      h.append(el("b", "", typeLabel(t, s)), el("span", "spacer"));
      h.append(c.is_default ? el("span", "chip default", "default") : el("span", "chip edited", "edited"));
      li.replaceChildren(h, el("div", "dim small", c.slots.map(describeSlot).join(", ")), el("div", "dim small", `“…for ${c.phrase}”`));
    },
    empty: "None",
  });
}

function homeDepartment() {
  return L().home_department || "Port Chester FD";
}
function slotRow(slot) {
  const li = el("li", "slot");
  if (slot.all !== undefined) {          // every available PCFD apparatus not already on the card
    li.dataset.all = slot.all || homeDepartment();
    li.append(el("span", "", "Full department"), el("span", "dim", `· ${li.dataset.all}`));
  } else if (slot.unit !== undefined) {
    const s = el("select", "s-unit");
    fillSelect(s, units().filter((u) => u.board && u.response !== "agency").map((u) => [u.name, u.title ? `${u.name} ${u.title}` : u.name]));
    s.value = slot.unit || (units()[0] && units()[0].name) || "";
    li.append(el("span", "dim", "Unit"), s);
  } else {
    const n = el("input", "s-count");
    n.type = "number"; n.min = "1"; n.max = "10"; n.value = String(slot.count || 1);
    const s = el("select", "s-kind");
    const kinds = (L().unit_kinds || []).filter((k) => !k.attach_only && !k.by_name).map((k) => [k.id, k.label.toLowerCase()]);
    const ladder = kinds.some(([k]) => k === "ladder") && kinds.some(([k]) => k === "tower") ? [["ladder|tower", "ladder or tower"]] : [];
    fillSelect(s, [["any", "any apparatus"], ...kinds.slice(0, 1), ...ladder, ...kinds.slice(1)]);
    s.value = slot.kind || "engine";
    li.append(n, el("span", "dim", "×"), s);
  }
  const up = el("button", "mini icon", "↑");
  up.type = "button";
  up.title = "Move up";
  up.addEventListener("click", () => { if (li.previousElementSibling) li.parentNode.insertBefore(li, li.previousElementSibling); });
  const rm = el("button", "mini icon", "✕");
  rm.type = "button";
  rm.title = "Remove";
  rm.addEventListener("click", () => { li.remove(); rcAllSync(); });
  li.append(el("span", "spacer"), up, rm);
  return li;
}
function rcAllSync() {              // one full department per card
  const b = $("#rc-add-all"), has = !!$("#rc-slots").querySelector("li[data-all]");
  b.disabled = has;
  b.title = has ? "This card already calls the full department" : `Every available ${homeDepartment()} apparatus not already on the card`;
}

function rcPlaceholder() {
  const t = $("#rc-type").value, s = $("#rc-subtype").value;
  const sp = L().spoken || {};
  $("#rc-spoken").placeholder = (s && (sp[`${t}/${s}`] || sp[`*/${s}`])) || sp[t] || "Default";
}

function editCard(c) {
  cad.rcEditing = c ? c.type : null;
  const [t, s] = c ? c.type.split("/") : [$("#rc-type").value || "", ""];
  $("#rc-type").value = t;
  fillSubtypes($("#rc-subtype"), t, "Any subtype");
  keepSubtype($("#rc-subtype"), t, s || "");
  $("#rc-spoken").value = c ? c.spoken || "" : "";
  rcPlaceholder();
  $("#rc-slots").replaceChildren(...(c ? c.slots : [{ kind: "engine", count: 1 }]).map(slotRow));
  rcAllSync();
  show($("#rc-delete"), !!c);
  flash($("#rc-error"));
  renderRunCards();
}

function slotsFromForm() {
  return [...$("#rc-slots").children].map((li) => {
    if (li.dataset.all) return { all: li.dataset.all };
    const u = li.querySelector(".s-unit");
    if (u) return { unit: u.value };
    return { kind: li.querySelector(".s-kind").value, count: Number(li.querySelector(".s-count").value) };
  });
}

async function saveCard(e) {
  e.preventDefault();
  flash($("#rc-error"));
  const t = $("#rc-type").value, s = $("#rc-subtype").value;
  const body = { type: s ? `${t}/${s}` : t, spoken: $("#rc-spoken").value.trim(), slots: slotsFromForm(), operator: operator() };
  if (cad.rcEditing && cad.rcEditing !== body.type) body.rename_from = cad.rcEditing;
  try {
    const c = await api("POST", "/api/cad/runcards", body);
    cad.rcEditing = c.type;
    UI.toast("Run card saved");
  } catch (err) {
    flash($("#rc-error"), err.message);
  }
}

async function deleteCard() {
  if (!cad.rcEditing || !(await UI.confirm(`Delete run card ${typeLabel(...cad.rcEditing.split("/"))}?`, { ok: "Delete" }))) return;
  try {
    await api("POST", "/api/cad/runcards/delete", { type: cad.rcEditing, operator: operator() });
    editCard(null);
  } catch (err) {
    flash($("#rc-error"), err.message);
  }
}

function openRunCards() {           // the header button: Settings > Run cards
  location.hash = "#settings/runcards";
}
function showRunCards() {           // settings.js, when the Run cards section opens
  const cards = (cad.state && cad.state.run_cards) || [];
  editCard(cards.find((c) => c.type === cad.rcEditing) || cards[0] || null);
}

// ---- state ----------------------------------------------------------------------------------

function render() {
  fillListSelects();
  renderQueue();
  renderPrealert();
  renderDetail();
  renderUnits();
  renderUnitLog();
  renderRunCards();
  renderRecords();
  renderOperators();
  for (const fn of cad.hooks) fn();
  roLockAll();
}

// The operator field: a picker from Settings > Users once there are operators, else free text.
// Signed in through Cloudflare Access (whoami): that person, read-only (the server records them on
// every write whatever the page sends), named from Settings > Users.
function renderOperators() {
  const ops = (cad.state && cad.state.operators) || [];
  const pick = $("#operator-pick"), input = $("#operator");
  const sig = JSON.stringify(ops);
  if (cad.who) {
    show(pick, false);
    show(input, true);
    if (pick._sig !== sig) { pick._sig = sig; loadWho(); }   // renamed in Settings: show the new name
    return;
  }
  show(pick, ops.length > 0);
  show(input, ops.length === 0);
  if (pick._sig === sig) return;
  pick._sig = sig;
  const want = pick.value || store("sizeup.cad.operator") || "";
  fillSelect(pick, ops.map((n) => [n, n]), "Name");
  pick.value = ops.includes(want) ? want : "";
  pick.classList.toggle("unset", !pick.value);
}

function onState(data) {
  cad.lastMsgAt = Date.now();
  if (data.error) { console.warn("CAD state error:", data.error); renderConn(); return; }
  const changed = !cad.state || cad.state.rev !== data.rev;
  cad.state = data;
  if (typeof data.time === "number") cad.skew = data.time - Date.now() / 1000;
  const tx = data.transmit || {};
  const pill = $("#tx-pill");
  pill.className = "pill console-only " + (tx.enabled ? "tx-on" : "tx-off");
  setText(pill, tx.enabled ? "Transmit on" : "Transmit off");
  setText($("#tx-note"), tx.enabled ? "" : "Transmit off: logged only");
  $("#tx-note").title = tx.enabled ? "" : tx.note || "";
  if (changed) cad.dbAt = 0;
  if (changed && cad.who) loadWho();          // a role changed in Settings > Users: show it at once
  renderConn();
  renderCloud();
  renderTalking();
  render();
}

// [cloud] (docs/cloud.md). On the hub: the station's connection, next to the connection pill (radios,
// speech-to-text and the dispatch voice are the station's). On the station: this console is not the
// CAD of record, the hub's is.
function renderCloud() {
  const c = cad.state && cad.state.cloud;
  const pill = $("#cloud-pill"), banner = $("#cloud-banner");
  show(pill, !!c);
  show(banner, !!(c && c.role === "station"));
  if (!c) return;
  if (c.role === "hub") {
    pill.className = "pill " + (c.state === "ok" ? "ok" : "err");
    setText(pill, c.state === "ok" ? "Station online" : `Station ${c.word || "offline"}`);
    pill.title = c.detail || "";
    return;
  }
  // (Settings > System > Station switches forwarding on and off: no cloud, no pill and no banner)
  pill.className = "pill " + (c.state === "ok" ? "ok" : "warn");
  setText(pill, c.state === "ok" ? "Hub connected" : c.state === "starting" ? "Hub connecting"
    : c.blocked ? "Hub not sending" : "Hub unreachable");
  pill.title = c.detail || "";
  const url = c.hub_url || "the hub";
  if (banner.dataset.url !== url) {
    banner.replaceChildren("Station console, not the CAD of record. Changes here stay local. Hub: ",
      Object.assign(el("a", null, url), { href: `${url}/cad`, target: "_blank", rel: "noopener" }));
    banner.dataset.url = url;
  }
}

// Who is talking now: the header strip, the talk alerts, the unit board highlight (radio.js).
function renderTalking() {
  Radio.update((cad.state && cad.state.talking) || []);
}

const newAddr = addressBox($("#f-address"), $("#f-geo"), $("#f-sugg"));
const editAddr = addressBox($("#e-address"), $("#e-geo"), $("#e-sugg"));

$("#new-incident").addEventListener("click", openNew);
$("#new-cancel").addEventListener("click", () => { cad.mode = cad.sel ? "view" : "none"; render(); });
$("#new-form").addEventListener("submit", createIncident);
$("#f-type").addEventListener("change", () => { fillSubtypes($("#f-subtype"), $("#f-type").value); entryClass(); });
$("#f-subtype").addEventListener("change", entryClass);
$("#f-emergency").addEventListener("change", () => { cad.emergTouched = true; entryClass(); });
$("#d-acc-type").addEventListener("change", () => fillSubtypes($("#d-acc-sub"), $("#d-acc-type").value));
$("#d-accept").addEventListener("click", () => act("accept", { type: $("#d-acc-type").value, subtype: $("#d-acc-sub").value }));
$("#d-dismiss").addEventListener("click", () => {
  const why = prompt("Dismiss this page: why? (optional)");
  if (why !== null) act("dismiss", { reason: why });
});
$("#d-close").addEventListener("click", () => {
  const f = $("#close-form");
  show(f, f.hidden);
  flash($("#c-error"));
  if (!f.hidden) $("#c-disp").focus();
});
$("#c-cancel").addEventListener("click", () => show($("#close-form"), false));
$("#close-form").addEventListener("submit", closeIncident);
$("#d-edit").addEventListener("click", openEdit);
$("#e-type").addEventListener("change", () => {     // a new type offers only its subtypes; back to the event's: as it was
  const inc = selected(), t = $("#e-type").value;
  fillSubtypes($("#e-subtype"), t);
  if (inc && t === inc.type) keepSubtype($("#e-subtype"), t, inc.subtype);
});
$("#edit-cancel").addEventListener("click", () => show($("#edit-form"), false));
$("#edit-form").addEventListener("submit", saveEdit);
$("#r-refresh").addEventListener("click", () => recommend());
$("#r-preview").addEventListener("click", preview);
$("#r-remarks").addEventListener("input", () => { if (cad.remarks) cad.remarks.edited = true; remarksNote(); });
$("#r-dispatch").addEventListener("click", dispatch);
$("#r-tickall").addEventListener("click", tickAll);
$("#r-joint").addEventListener("click", addJoint);
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
$("#note-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const inc = selected(), input = $("#note-text");
  if (!inc || !input.value.trim()) return;
  try {
    const units = noteUnits(input.value);
    await api("POST", `/api/cad/incident/${inc.id}/note`, Object.assign({ text: input.value.trim(), operator: operator() },
                                                                       units.length ? { units } : {}));
    input.value = "";
    noteTag.tags.clear();
    renderNoteTags();
    hideSugg();
    if (cad.extra.has(inc.id)) { cad.extra.set(inc.id, await api("GET", `/api/cad/incident/${inc.id}`)); render(); }
  } catch (err) {
    UI.toast(err.message, "err");
  }
});
$("#note-text").addEventListener("input", () => { syncNoteTags(); suggestUnits(); });
$("#note-text").addEventListener("keydown", noteKeys);
$("#note-text").addEventListener("blur", () => setTimeout(hideSugg, 150));
$("#note-text").addEventListener("click", suggestUnits);
$("#runcards-open").addEventListener("click", openRunCards);
$("#dispatch-open").addEventListener("click", openDispatch);
window.addEventListener("hashchange", route);
if (BOARD) setText($("#page-title"), "Monitor");
Radio.mountAlerts($("#alerts"));
CadFeed.mount($("#feed"), { state: () => cad.state, selected, operator, api, radioRow, time: (u) => fmtTime(u, true) });
cad.hooks.push(CadFeed.render);
$("#rc-form").addEventListener("submit", saveCard);
$("#rc-type").addEventListener("change", () => {
  const t = $("#rc-type").value, [ct, cs] = (cad.rcEditing || "").split("/");
  fillSubtypes($("#rc-subtype"), t, "Any subtype");
  if (cs && t === ct) keepSubtype($("#rc-subtype"), t, cs);
  rcPlaceholder();
});
$("#rc-subtype").addEventListener("change", rcPlaceholder);
$("#rc-add-kind").addEventListener("click", () => $("#rc-slots").append(slotRow({ kind: "engine", count: 1 })));
$("#rc-add-unit").addEventListener("click", () => $("#rc-slots").append(slotRow({ unit: "" })));
$("#rc-add-all").addEventListener("click", () => {
  if (!$("#rc-slots").querySelector("li[data-all]")) $("#rc-slots").append(slotRow({ all: homeDepartment() }));
  rcAllSync();
});
$("#rc-new").addEventListener("click", () => editCard(null));
$("#rc-delete").addEventListener("click", deleteCard);
$("#db-close").addEventListener("click", () => openOverlay("#db-overlay", false));
for (const id of ["#db-overlay"]) {
  $(id).addEventListener("click", (e) => { if (e.target === e.currentTarget) openOverlay(id, false); });
}
$("#operator").value = store("sizeup.cad.operator") || "";
$("#operator").addEventListener("input", () => store("sizeup.cad.operator", operator()));
$("#operator-pick").addEventListener("change", () => {
  store("sizeup.cad.operator", operator());
  $("#operator-pick").classList.toggle("unset", !operator());
});
window.addEventListener("keydown", (e) => {        // Esc: the unit's "why?" box first (dialogs: ui.js)
  if (e.key === "Escape" && cad.ask) { cad.ask = null; render(); }
});

// ---- keyboard shortcuts (ui.js: "?" shows them; never while typing) ----------------------------------
// N new incident, / search, G then D / O / R / S a view, J / K the next / previous incident in the queue,
// Esc closes what is open (a dialog, the incident's open form, the new-incident form, an opened unit row).
function queueStep(dir) {
  const items = [...document.querySelectorAll(".queue .q > li.qi")].filter((n) => n.getClientRects().length);
  if (!items.length) return;
  const at = items.findIndex((n) => Number(n.dataset.key) === cad.sel);
  const next = items[at < 0 ? (dir > 0 ? 0 : items.length - 1) : Math.max(0, Math.min(items.length - 1, at + dir))];
  selectIncident(Number(next.dataset.key));
  next.focus({ preventScroll: true });
  next.scrollIntoView({ block: "nearest" });
}
function focusSearch() {
  const here = [...document.querySelectorAll("main:not([hidden]) input[type=search], main:not([hidden]) .tg-filter")]
    .find((n) => n.getClientRects().length && !n.closest("[hidden]"));
  if (here) { here.focus(); here.select(); return; }
  location.hash = "#radio";
  setTimeout(() => { $("#rv-q").focus(); $("#rv-q").select(); }, 0);
}
function escapeView(fromField) {
  if (fromField) return;
  for (const [form, cancel] of [["#upgrade-form", "#u-cancel"], ["#close-form", "#c-cancel"], ["#edit-form", "#edit-cancel"]]) {
    if (!$(form).hidden && !$("#d-body").hidden) { $(cancel).click(); return; }
  }
  if (cad.mode === "new") { $("#new-cancel").click(); return; }
  if (cad.openUnit !== null) { cad.openUnit = null; renderUnits(); }
}
if (!BOARD) {
  UI.keys({
    n: () => { if (!$("#new-incident").disabled) openNew(); },
    "/": focusSearch,
    j: () => queueStep(1),
    k: () => queueStep(-1),
    gd: () => $("#dispatch-open").click(),
    go: () => $("#overview-open").click(),
    gr: () => { location.hash = "#radio"; },
    gs: () => { location.hash = "#settings"; },
    escape: escapeView,
  });
} else UI.keys({ board: true });

// user, 2026-10-07: "can we get the auth info from the cloudflare access auth to identify email that
// was authed with one time code to set the operators name dynamically?"
async function loadWho() {
  let w;
  try {
    const r = await fetch("/api/cad/whoami");
    if (!r.ok) return;
    w = await r.json();
  } catch (e) { return; }
  if (!w || w.source !== "access") return;
  cad.who = w;
  const input = $("#operator"), tip = w.email + (w.name === w.email ? " (no name yet: Settings, Users)" : "");
  input.value = w.callsign ? `${w.name} ${w.callsign}` : w.name;
  input.readOnly = true;
  input.classList.add("access");
  input.title = tip;
  input.closest("label").title = tip;
  renderOperators();
  applyRoles();
}

// Roles (user, 2026-10-08: "admins should be able to set users, dispatchers should not, but first
// login should be able to set whatever i guess"). The server enforces them for whoever signed in
// through Cloudflare Access (sizeup/cad/api.py WRITES); this only shows it: Settings read-only for a
// dispatcher, every action off for a viewer or a deactivated user, each with a banner naming the
// admins. Without Access (whoami enforced: false) nothing changes.
function canDo(need) {             // "dispatch" (run calls) | "admin" (Settings, the radios)
  const w = cad.who;
  if (!w || !w.enforced) return true;
  return w.may === "admin" || (need === "dispatch" && w.may === "dispatcher");
}
function askAdmin() {
  const a = (cad.who && cad.who.admins) || [];
  return a.length ? `Ask an admin (${a.join(", ")})` : "Ask an admin";
}
// What a viewer can't press on the dispatch screen (reading, filtering, playing, the maps stay).
const RO_DISPATCH = "#new-incident, #new-form :is(input, select, textarea, #new-create), #d-accept, #d-dismiss, #d-edit, "
  + "#d-close, #d-queued select, #close-form :is(select, #c-close), #edit-form :is(input, select, textarea, #edit-save), "
  + "#d-attach :is(select, button:not(.help)), #d-dispatch :is(input, select, textarea, button:not(#u-cancel):not(.help)), #note-text, #note-add, "
  + ".ubtns button, .feed-add, .rv-add, .rv-pick :is(select, button), .pa-dismiss";
// In Settings for anyone but an admin: everything but the section tabs, filters and what only reads.
const RO_SETTINGS_KEEP = ".tab, [type=search], .ro-ok, .ro-ok *, .help";
function roLock(root, sel, keep) {
  for (const n of root.querySelectorAll(sel)) {
    if (!n.disabled && !(keep && n.matches(keep))) { n.disabled = true; n.dataset.ro = "1"; }
  }
}
function roUnlock() {
  for (const n of document.querySelectorAll("[data-ro]")) { n.disabled = false; delete n.dataset.ro; }
}
function roLockAll() {             // every render, and whatever appears later (the observer below)
  if (!cad.who || !cad.who.enforced) return;
  const main = document.querySelector(".cad-main");
  if (!canDo("dispatch")) roLock(main, RO_DISPATCH);
  if (!canDo("admin")) {
    roLock($("#settings .s-content"), "button, input, select, textarea", RO_SETTINGS_KEEP);
    roLock(main, "button.rerun");                     // speech-to-text again: the station's, admin only
  }
}
function applyRoles() {
  const w = cad.who;
  if (!w || !w.enforced) return;
  const viewOnly = !canDo("dispatch"), roSettings = !canDo("admin");
  document.body.classList.toggle("ro-dispatch", viewOnly);
  document.body.classList.toggle("ro-settings", roSettings);
  const banner = $("#role-banner");
  setText(banner, !w.active ? `Account deactivated: read-only. ${askAdmin()}`
    : `Read-only: viewer. ${askAdmin()}`);
  show(banner, viewOnly);
  const note = $("#s-role");
  const who = !w.active ? "account deactivated" : w.role === "viewer" ? "viewer" : "dispatcher";
  setText(note, w.bootstrap ? "No admin yet: anyone signed in can change settings. Make one in Users."
    : roSettings ? `Read-only: ${who}. ${askAdmin()}` : "");
  show(note, w.bootstrap || roSettings);
  note.classList.toggle("boot", !!w.bootstrap);
  const sig = `${w.may}|${w.active}`;
  if (applyRoles.sig !== sig) {            // unlock what the old role locked, then lock for the new one
    applyRoles.sig = sig;
    roUnlock();
    if (cad.state) render();
    if (window.sRender) sRender(true);
  }
  roLockAll();
}
// Settings, the Radios editor and the feed draw on their own too: lock what appears.
new MutationObserver(() => { if (cad.who && cad.who.enforced && !canDo("admin")) roLockAll(); })
  .observe(document.body, { childList: true, subtree: true });

async function boot() {
  try {
    cad.scene = await (await fetch("/api/scene")).json();
    // The header says "Port Chester FD"; the configured station name only when it says more.
    const name = (cad.scene.station && cad.scene.station.name) || "";
    const extra = name && name !== "Port Chester FD" ? name : "";
    const page = BOARD ? "Monitor" : "CAD";
    document.title = extra ? `PCFD ${page} · ${extra}` : `PCFD ${page}`;
    setText($("#station"), extra);
  } catch (e) {
    console.warn("no scene (map off):", e);
  }
  await loadWho();
  route();
  cad.live = liveState("/api/cad/stream", "/api/cad/state", onState);
  tick();
  setInterval(tick, 1000);
}

boot().catch((err) => {
  const pill = $("#conn");
  pill.className = "pill err";
  pill.textContent = "Load failed: " + err.message;
  console.error(err);
});
