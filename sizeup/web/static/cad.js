// sizeup CAD console (/cad): PCFD's own incidents, units and dispatch.
// The server pushes the CAD state over SSE once a second (/api/cad/stream); every action is a
// POST to /api/cad/... and the next push shows its result. Nothing here invents data: empty
// lists say so, and what the server didn't say isn't shown.
//
// Transmit is off in this version: "Dispatch" assigns the units, logs the dispatch and plays
// its audio here. Nothing is keyed on a radio.
"use strict";

const $ = (sel) => document.querySelector(sel);
const LOST_AFTER_MS = 6000;
const STATUS_COLORS = {
  available: "#46d27a", "available-in-quarters": "#2fa862", dispatched: "#ffb020", enroute: "#ff8a3d",
  onscene: "#ff4d5e", transporting: "#4da3ff", out_of_service: "#7d7ba3",
};
const KIND_GROUPS = [
  ["Chief officers", ["chief"]], ["Engines", ["engine"]], ["Ladders & towers", ["ladder", "tower"]],
  ["Rescue", ["rescue"]], ["Marine", ["boat"]],
];
const SLOT_KINDS = [["engine", "engine"], ["ladder|tower", "ladder or tower"], ["ladder", "ladder"],
  ["tower", "tower"], ["rescue", "rescue"], ["boat", "boat"], ["chief", "chief officer"]];

const cad = {
  scene: null, state: null, lastMsgAt: 0, skew: 0,
  sel: null,                 // selected incident id
  mode: "none",              // none | new | view
  rec: null,                 // {iid, units, unfilled, already, card, checked:Set, error}
  map: null, marker: null, mapSig: "",
  cardSig: "", rcEditing: null, openUnit: null,
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
function statusLabel(id) {
  const s = ((cad.state && cad.state.statuses) || []).find((x) => x.id === id);
  return s ? s.label : id;
}
function statusChip(status) {
  const c = el("span", "chip st", statusLabel(status));
  c.style.setProperty("--c", STATUS_COLORS[status] || "#9aa0b4");
  return c;
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

function syncList(box, items, { key, sig, make, fill, empty }) {
  if (!items.length) {
    if (box.dataset.empty !== empty || box.children.length !== 1) {
      box.replaceChildren(el(box.tagName === "OL" || box.tagName === "UL" ? "li" : "div", "empty", empty));
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
function renderClock() {
  setText($("#clock-local"), fmt("hms").format(new Date(serverNow() * 1000)));
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
  return incidents().find((i) => i.id === cad.sel) || null;
}
function openAssignments(inc) {
  return (inc.assignments || []).filter((a) => !a.cleared_at);
}
function incTitle(inc) {
  return inc.number || (inc.status === "pending" ? "PENDING" : inc.status.toUpperCase());
}

function queueItem() {
  const li = el("li", "qi");
  li.tabIndex = 0;
  li.addEventListener("click", () => selectIncident(Number(li.dataset.key)));
  li.addEventListener("keydown", (e) => { if (e.key === "Enter") selectIncident(Number(li.dataset.key)); });
  return li;
}
function fillQueueItem(li, inc) {
  li.className = `qi ${inc.status}` + (inc.id === cad.sel ? " sel" : "");
  const head = el("div", "qh");
  head.append(el("b", "qn", incTitle(inc)), el("span", "qt", inc.type || "—"));
  const right = el("span", "qe");
  if (inc.status === "pending" || inc.status === "active") since(right, inc.status === "active" ? inc.accepted_at || inc.created_at : inc.created_at);
  else right.textContent = fmtTime(inc.closed_at);
  head.append(el("span", "spacer"), right);
  const addr = el("div", "qa" + (inc.address ? "" : " dim"), inc.address || "no address");
  li.replaceChildren(head, addr);
  if (inc.status === "pending") li.append(el("div", "qs dim", "from 60 Control page · needs Accept or Dismiss"));
  const on = openAssignments(inc);
  if (on.length) {
    const chips = el("div", "chips");
    for (const a of on) {
      const c = statusChip(a.status);
      c.textContent = `${a.name} · ${statusLabel(a.status)}`;
      chips.append(c);
    }
    li.append(chips);
  }
}
function renderQueue() {
  const all = incidents();
  const sig = (i) => JSON.stringify([i.status, i.number, i.type, i.address, i.id === cad.sel,
    openAssignments(i).map((a) => [a.name, a.status]), i.closed_at]);
  const opts = { key: (i) => String(i.id), sig, make: queueItem, fill: fillQueueItem };
  syncList($("#q-pending"), all.filter((i) => i.status === "pending"), { ...opts, empty: "No pending pages" });
  syncList($("#q-active"), all.filter((i) => i.status === "active"), { ...opts, empty: "No active incidents" });
  syncList($("#q-closed"), all.filter((i) => i.status === "closed" || i.status === "dismissed"),
    { ...opts, empty: "None closed recently" });
}

// ---- incident detail ------------------------------------------------------------------

function selectIncident(id) {
  cad.sel = id;
  cad.mode = "view";
  cad.rec = null;
  show($("#edit-form"), false);
  show($("#r-preview-box"), false);
  flash($("#r-error"));
  render();
  recommend();
}

function renderDetail() {
  const inc = cad.mode === "view" ? selected() : null;
  show($("#new-form"), cad.mode === "new");
  show($("#d-empty"), cad.mode === "none" || (cad.mode === "view" && !inc));
  show($("#d-body"), !!inc);
  if (cad.mode === "view" && !inc) {
    setText($("#d-empty .big"), "That incident is no longer listed");
    return;
  }
  setText($("#d-empty .big"), "No incident selected");
  if (!inc) return;
  const open = inc.status === "pending" || inc.status === "active";
  setText($("#d-number"), incTitle(inc));
  const st = $("#d-status");
  st.className = `st-badge ${inc.status}`;
  setText(st, inc.status);
  setText($("#d-source"), inc.source === "manual" ? "entered by hand" : `from ${inc.source}`);
  show($("#d-accept"), inc.status === "pending");
  show($("#d-dismiss"), inc.status === "pending");
  show($("#d-close"), inc.status === "active");
  show($("#d-edit"), open);
  setText($("#d-type"), inc.type || "—");
  const addr = $("#d-addr");
  addr.className = "d-addr" + (inc.address ? "" : " dim");
  setText(addr, inc.address || "No address");
  setText($("#d-cross"), inc.cross_street ? `Cross: ${inc.cross_street}` : "");
  setText($("#d-geo"), inc.lat != null ? `Map: ${inc.geo_label || `${inc.lat.toFixed(5)}, ${inc.lon.toFixed(5)}`}`
    : "Not on the map (no gazetteer match picked)");
  $("#d-geo").className = "geo" + (inc.lat != null ? "" : " warn");
  setText($("#d-created"), fmtTime(inc.created_at, true));
  const elapsed = $("#d-elapsed");
  if (open) since(elapsed, inc.created_at);
  else { since(elapsed, null); setText(elapsed, fmtDur((inc.closed_at || 0) - inc.created_at)); }
  show($("#d-prio-w"), inc.priority != null);
  setText($("#d-prio"), inc.priority != null ? String(inc.priority) : "");
  setText($("#d-narr"), inc.narrative || "");
  show($("#d-narr"), !!inc.narrative);
  renderMap(inc);
  renderOnUnits(inc);
  renderDispatchBox(inc);
  renderDispatches(inc);
  renderEvents(inc);
  show($("#note-form"), true);
}

function renderOnUnits(inc) {
  const rows = (inc.assignments || []).slice().sort((a, b) => (!!a.cleared_at - !!b.cleared_at));
  syncList($("#d-units"), rows, {
    key: (a) => String(a.id),
    sig: (a) => JSON.stringify([a.status, a.dispatched_at, a.enroute_at, a.onscene_at, a.cleared_at]),
    make: () => el("li", "ou"),
    fill: (li, a) => {
      li.className = "ou" + (a.cleared_at ? " cleared" : "");
      const head = el("div", "ouh");
      head.append(el("b", "", a.name));
      if (!a.cleared_at) head.append(statusChip(a.status));
      const times = el("span", "dim small",
        [["disp", a.dispatched_at], ["enr", a.enroute_at], ["on scene", a.onscene_at], ["cleared", a.cleared_at]]
          .filter(([, t]) => t).map(([k, t]) => `${k} ${fmtTime(t, true)}`).join(" · "));
      head.append(times, el("span", "spacer"));
      if (!a.cleared_at) {
        for (const [s, label] of [["enroute", "En route"], ["onscene", "On scene"], ["transporting", "Transporting"], ["available", "Available"]]) {
          if (a.status === s) continue;
          const b = el("button", "mini", label);
          b.addEventListener("click", (e) => { e.stopPropagation(); setUnitStatus(a.unit_id, s); });
          head.append(b);
        }
      }
      li.replaceChildren(head);
    },
    empty: inc.status === "pending" ? "None: accept, then dispatch" : "No units dispatched",
  });
}

function renderEvents(inc) {
  syncList($("#d-events"), (inc.events || []).slice().reverse(), {
    key: (e) => String(e.id),
    sig: (e) => String(e.id),
    make: () => el("li", "ev"),
    fill: (li, e) => {
      const meta = el("div", "meta");
      meta.append(el("span", "t", fmtTime(e.at, true)), el("span", `ek ${e.kind}`, e.kind));
      if (e.operator) meta.append(el("span", "", `· ${e.operator}`));
      li.replaceChildren(meta, el("div", "txt", e.text));
    },
    empty: "Nothing yet",
  });
}

function renderDispatches(inc) {
  syncList($("#d-dispatches"), (inc.dispatches || []).slice().reverse(), {
    key: (d) => String(d.id),
    sig: (d) => String(d.id),
    make: () => el("li", "dp"),
    fill: (li, d) => {
      const meta = el("div", "meta");
      meta.append(el("span", "t", fmtTime(d.at, true)), el("span", "", d.operator ? `by ${d.operator}` : ""),
        el("span", "", d.channel ? `· ${d.channel}` : ""));
      meta.append(el("span", d.transmitted ? "txd" : "notx", d.transmitted ? "TRANSMITTED" : "NOT TRANSMITTED"));
      if (!d.transmitted && d.tx_note) meta.append(el("span", "", `(${d.tx_note})`));
      const row = el("div", "row");
      if (d.has_audio) {
        const b = el("button", "play", "▶");
        b.title = "Play this dispatch";
        b.addEventListener("click", () => play(`/api/cad/dispatch/${d.id}.wav`));
        row.append(b);
      }
      row.append(el("div", "txt", d.text));
      li.replaceChildren(meta, row);
      if (d.audio_note) li.append(el("div", "dim small", d.audio_note));
    },
    empty: "No dispatches yet",
  });
}

// ---- recommendation / preview / dispatch ----------------------------------------------------

async function recommend() {
  const inc = selected();
  if (!inc || (inc.status !== "pending" && inc.status !== "active")) { cad.rec = null; renderDispatchBox(inc); return; }
  const iid = inc.id;
  try {
    const r = await api("POST", `/api/cad/incident/${iid}/recommend`, {});
    if (cad.sel !== iid) return;
    cad.rec = { iid, ...r, checked: new Set(r.units.map((u) => u.id)), extra: [] };
  } catch (e) {
    cad.rec = { iid, units: [], unfilled: [], already: [], card: "", checked: new Set(), extra: [], error: e.message };
  }
  cad.rec.sig = null;
  renderDispatchBox(selected());
}

function unitById(id) {
  return ((cad.state && cad.state.units) || []).find((u) => u.id === id);
}

function renderDispatchBox(inc) {
  const box = $("#d-dispatch");
  const open = inc && (inc.status === "pending" || inc.status === "active");
  show(box, !!open);
  if (!open) return;
  show($("#r-pending"), inc.status === "pending");
  $("#r-dispatch").disabled = inc.status !== "active";
  const rec = cad.rec && cad.rec.iid === inc.id ? cad.rec : null;
  setText($("#r-card"), rec && rec.card ? `run card: ${rec.card}` : "");
  const ul = $("#r-unfilled");
  const warn = rec ? [...(rec.error ? [rec.error] : []), ...rec.unfilled] : [];
  ul.replaceChildren(...warn.map((w) => el("li", "", w)));
  const rows = rec ? [...rec.units, ...rec.extra.map((id) => ({ ...unitById(id), slot: "added" }))] : [];
  const sig = JSON.stringify([rows.map((u) => u.id), rec && [...rec.checked], rec && rec.already,
    rows.map((u) => { const x = unitById(u.id); return x ? [x.status, x.incident_id] : null; })]);
  if (rec && rec.sig === sig) return;
  if (rec) rec.sig = sig;
  const list = $("#r-units");
  if (!rec) { list.replaceChildren(el("li", "empty", "Working out the run card…")); return; }
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
    lab.append(cb, el("b", "", u.name), el("span", "dim small", u.slot || ""), el("span", "spacer"), statusChip(now.status));
    if (now.spare) lab.append(el("span", "chip spare", "spare"));
    if (now.incident_id && now.incident_id !== inc.id) lab.append(el("span", "warn small", `on ${now.incident_number} (will be moved)`));
    li.append(lab);
    return li;
  });
  if (!items.length) {
    items.push(el("li", "empty", rec.already.length ? `Already on it: ${rec.already.join(", ")}. Nothing more on the run card.`
      : "Nothing to recommend"));
  } else if (rec.already.length) {
    items.push(el("li", "dim small", `Already on it: ${rec.already.join(", ")}`));
  }
  list.replaceChildren(...items);
  // Add any other unit (not out of service, not already listed or on this incident).
  const listed = new Set(rows.map((u) => u.id));
  const sel = $("#r-add");
  const others = ((cad.state && cad.state.units) || []).filter((u) => !listed.has(u.id) && u.status !== "out_of_service"
    && u.incident_id !== inc.id);
  const keep = sel.value;
  sel.replaceChildren(el("option", "", others.length ? "Add another unit…" : "No other units in service"),
    ...others.map((u) => {
      const o = el("option", "", `${u.name} · ${statusLabel(u.status)}${u.incident_number ? ` (on ${u.incident_number})` : ""}`);
      o.value = String(u.id);
      return o;
    }));
  sel.firstChild.value = "";
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  const ch = $("#r-channel");
  const chans = (cad.state.transmit && cad.state.transmit.channels) || [];
  if (ch.options.length !== chans.length) {
    ch.replaceChildren(...chans.map((c) => { const o = el("option", "", c); o.value = c; return o; }));
  }
}

function checkedIds() {
  const rec = cad.rec;
  if (!rec) return [];
  const order = [...rec.units.map((u) => u.id), ...rec.extra];
  return order.filter((id) => rec.checked.has(id));
}

async function preview() {
  const inc = selected();
  flash($("#r-error"));
  if (!inc) return;
  const ids = checkedIds();
  if (!ids.length) return flash($("#r-error"), "Tick at least one unit.");
  const b = $("#r-preview");
  b.disabled = true;
  setText(b, "Rendering…");
  try {
    const r = await api("POST", `/api/cad/incident/${inc.id}/preview`, { unit_ids: ids });
    showPreview("Preview (nothing assigned or logged)", r.text, r.audio_url, r.audio_note);
  } catch (e) {
    flash($("#r-error"), e.message);
  } finally {
    b.disabled = false;
    setText(b, "▶ Preview dispatch");
  }
}

function showPreview(title, text, url, note) {
  show($("#r-preview-box"), true);
  setText($("#r-preview-box .dim"), title);
  setText($("#r-preview-text"), text);
  const a = $("#r-audio");
  show(a, !!url);
  if (url) { a.src = url; a.play().catch(() => {}); }
  setText($("#r-audio-note"), note || "");
}

async function dispatch() {
  const inc = selected();
  flash($("#r-error"));
  if (!inc) return;
  const op = operator();
  if (!op) { $("#operator").focus(); return flash($("#r-error"), "Enter your name (Operator, top right) first."); }
  const ids = checkedIds();
  if (!ids.length) return flash($("#r-error"), "Tick at least one unit.");
  const b = $("#r-dispatch");
  b.disabled = true;
  setText(b, "Dispatching…");
  try {
    const r = await api("POST", `/api/cad/incident/${inc.id}/dispatch`,
      { unit_ids: ids, operator: op, channel: $("#r-channel").value });
    const how = r.transmitted ? `Transmitted on ${r.channel}` : `Logged, NOT transmitted (${r.detail})`;
    showPreview(`Dispatch #${r.dispatch_id}: ${how}` + (r.moved.length ? ` · moved ${r.moved.join(", ")}` : ""),
      r.text, r.audio_url, r.audio_note);
    setTimeout(recommend, 300);
  } catch (e) {
    flash($("#r-error"), e.message);
  } finally {
    b.disabled = false;
    setText(b, "Dispatch");
  }
}

// ---- incident actions --------------------------------------------------------------------

async function act(action, body = {}) {
  const inc = selected();
  if (!inc) return;
  try {
    await api("POST", `/api/cad/incident/${inc.id}/${action}`, { operator: operator(), ...body });
    if (action === "accept") setTimeout(recommend, 300);
  } catch (e) {
    alert(e.message);
  }
}

async function setUnitStatus(uid, status) {
  let note = "";
  if (status === "out_of_service") {
    note = prompt("Out of service: why? (optional)") ?? null;
    if (note === null) return;
  }
  try {
    await api("POST", `/api/cad/unit/${uid}/status`, { status, operator: operator(), note });
  } catch (e) {
    alert(e.message);
  }
}

// ---- address box with gazetteer suggestions -------------------------------------------------

function addressBox(input, geoNote, list) {
  const box = { picked: null, timer: null, seq: 0 };
  const note = () => {
    geoNote.className = "geo span2" + (box.picked ? "" : " warn");
    if (box.picked) setText(geoNote, `Map: ${box.picked.label} (${box.picked.kind}, ${Math.round(box.picked.confidence * 100)}%)`);
    else if (!input.value.trim()) setText(geoNote, "");
    else if (cad.state && !cad.state.geocoder) setText(geoNote, "Geocoder unavailable: saved as typed, no map pin.");
    else setText(geoNote, "No match picked: saved as typed; placed on the map only if the gazetteer is sure.");
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
        li.append(el("b", "", m.label), el("span", "dim small", ` ${m.kind} · ${Math.round(m.confidence * 100)}%`));
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

// ---- new incident / edit ----------------------------------------------------------------

function fillTypeSelects() {
  const cards = (cad.state && cad.state.run_cards) || [];
  const sig = JSON.stringify(cards.map((c) => c.type));
  if (sig === cad.cardSig) return;
  cad.cardSig = sig;
  for (const id of ["#f-type", "#e-type"]) {
    const s = $(id), keep = s.value;
    s.replaceChildren(...cards.map((c) => { const o = el("option", "", c.type); o.value = c.type; return o; }));
    if ([...s.options].some((o) => o.value === keep)) s.value = keep;
  }
}

function openNew() {
  cad.mode = "new";
  cad.sel = null;
  $("#new-form").reset();
  newAddr.set("", null);
  flash($("#f-error"));
  render();
  $("#f-address").focus();
}

async function createIncident(e) {
  e.preventDefault();
  flash($("#f-error"));
  const body = {
    type: $("#f-type").value, priority: $("#f-priority").value ? Number($("#f-priority").value) : null,
    address: $("#f-address").value.trim(), cross_street: $("#f-cross").value.trim(),
    narrative: $("#f-narr").value.trim(), operator: operator(), ...newAddr.geo(),
  };
  try {
    const inc = await api("POST", "/api/cad/incident", body);
    selectIncident(inc.id);
  } catch (err) {
    flash($("#f-error"), err.message);
  }
}

function openEdit() {
  const inc = selected();
  if (!inc) return;
  $("#e-type").value = inc.type;
  $("#e-priority").value = inc.priority != null ? String(inc.priority) : "";
  editAddr.set(inc.address, inc.lat != null ? { lat: inc.lat, lon: inc.lon, label: inc.geo_label || inc.address, kind: "current", confidence: 1 } : null);
  $("#e-cross").value = inc.cross_street || "";
  flash($("#e-error"));
  show($("#edit-form"), true);
}

async function saveEdit(e) {
  e.preventDefault();
  const inc = selected();
  if (!inc) return;
  const body = { type: $("#e-type").value, priority: $("#e-priority").value ? Number($("#e-priority").value) : null,
    cross_street: $("#e-cross").value.trim(), operator: operator() };
  const addr = $("#e-address").value.trim();
  if (addr !== (inc.address || "") || (editAddr.picked && editAddr.picked.kind !== "current")) {
    body.address = addr;
    Object.assign(body, editAddr.geo());
  }
  try {
    await api("POST", `/api/cad/incident/${inc.id}`, body);
    show($("#edit-form"), false);
    if (body.type !== inc.type) setTimeout(recommend, 300);
  } catch (err) {
    flash($("#e-error"), err.message);
  }
}

// ---- map ----------------------------------------------------------------------------------

function renderMap(inc) {
  const has = inc && inc.lat != null && inc.lon != null;
  show($("#d-nomap"), !has || !window.maplibregl || !cad.scene);
  show($("#d-map"), has && !!window.maplibregl && !!cad.scene);
  if (!has) { setText($("#d-nomap"), "No map point for this incident"); return; }
  if (!window.maplibregl || !cad.scene) { setText($("#d-nomap"), "Map unavailable"); return; }
  const sig = JSON.stringify([inc.id, inc.lat, inc.lon]);
  if (!cad.map) {
    cad.map = new maplibregl.Map({
      container: $("#d-map"), style: buildMapStyle(cad.scene), center: [inc.lon, inc.lat], zoom: 16.5,
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

// ---- unit board ---------------------------------------------------------------------------

function renderUnits() {
  const units = (cad.state && cad.state.units) || [];
  const box = $("#units");
  if (!units.length) {
    if (!box.querySelector(".empty")) box.replaceChildren(el("div", "empty", "No units in the roster"));
    return;
  }
  box.querySelector(":scope > .empty")?.remove();
  for (const [title, kinds] of KIND_GROUPS) {
    const rows = units.filter((u) => kinds.includes(u.kind));
    let g = box.querySelector(`[data-group="${title}"]`);
    if (!rows.length) { if (g) g.remove(); continue; }
    if (!g) {
      g = el("section", "ugroup");
      g.dataset.group = title;
      g.append(el("h3", "", title), el("ol", "list ulist"));
      box.append(g);
    }
    syncList(g.querySelector("ol"), rows, {
      key: (u) => String(u.id),
      sig: (u) => JSON.stringify([u.status, u.status_at, u.incident_number, u.out_of_service, cad.openUnit === u.id]),
      make: () => {
        const li = el("li", "urow");
        li.addEventListener("click", (e) => {
          if (e.target.closest("button, a")) return;
          const id = Number(li.dataset.key);
          cad.openUnit = cad.openUnit === id ? null : id;
          renderUnits();
        });
        return li;
      },
      fill: (li, u) => {
        li.className = "urow" + (cad.openUnit === u.id ? " open" : "") + (u.status === "out_of_service" ? " oos" : "");
        const head = el("div", "uh");
        head.append(el("b", "un", u.name));
        if (u.spare) head.append(el("span", "chip spare", "spare"));
        head.append(el("span", "spacer"), statusChip(u.status), since(el("span", "dim small ut"), u.status_at));
        li.replaceChildren(head);
        const sub = [];
        if (u.incident_number) {
          const a = el("a", "inc-link", u.incident_number);
          a.href = "#";
          a.addEventListener("click", (e) => { e.preventDefault(); selectIncident(u.incident_id); });
          sub.push(a);
        }
        if (u.status === "out_of_service" && u.out_of_service) sub.push(el("span", "dim", u.out_of_service));
        if (sub.length) { const d = el("div", "usub"); d.append(...sub); li.append(d); }
        if (cad.openUnit === u.id) {
          const btns = el("div", "ubtns");
          for (const s of (cad.state.statuses || [])) {
            const b = el("button", "mini" + (s.id === u.status ? " on" : ""), s.label);
            b.style.setProperty("--c", STATUS_COLORS[s.id] || "#9aa0b4");
            b.disabled = s.id === u.status;
            b.addEventListener("click", () => setUnitStatus(u.id, s.id));
            btns.append(b);
          }
          li.append(btns);
        }
      },
      empty: "None",
    });
  }
}

function renderUnitLog() {
  syncList($("#unit-log"), (cad.state && cad.state.unit_log) || [], {
    key: (e) => String(e.id), sig: (e) => String(e.id), make: () => el("li", "ev"),
    fill: (li, e) => {
      const meta = el("div", "meta");
      meta.append(el("span", "t", fmtTime(e.at, true)));
      if (e.operator) meta.append(el("span", "", e.operator));
      li.replaceChildren(meta, el("div", "txt", e.text));
    },
    empty: "No status changes off an incident",
  });
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
    sig: (c) => JSON.stringify([c.type, c.slots, c.is_default, cad.rcEditing === c.type]),
    make: () => {
      const li = el("li", "rc");
      li.addEventListener("click", () => editCard(li._card));
      return li;
    },
    fill: (li, c) => {
      li._card = c;
      li.className = "rc" + (cad.rcEditing === c.type ? " sel" : "");
      const h = el("div", "row");
      h.append(el("b", "", c.type), el("span", "spacer"));
      if (c.is_default) h.append(el("span", "chip default", "default"));
      li.replaceChildren(h, el("div", "dim small", c.slots.map(describeSlot).join(", ")));
    },
    empty: "No run cards: add one",
  });
}

function slotRow(slot) {
  const li = el("li", "slot");
  const units = (cad.state && cad.state.units) || [];
  if (slot.unit !== undefined) {
    const s = el("select", "s-unit");
    s.append(...units.map((u) => { const o = el("option", "", u.name); o.value = u.name; return o; }));
    s.value = slot.unit || (units[0] && units[0].name) || "";
    li.append(el("span", "dim", "unit"), s);
  } else {
    const n = el("input", "s-count");
    n.type = "number"; n.min = "1"; n.max = "10"; n.value = String(slot.count || 1);
    const s = el("select", "s-kind");
    s.append(...SLOT_KINDS.map(([v, t]) => { const o = el("option", "", t); o.value = v; return o; }));
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

function editCard(c) {
  cad.rcEditing = c ? c.type : null;
  $("#rc-type").value = c ? c.type : "";
  $("#rc-spoken").value = c ? c.spoken || "" : "";
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
  const body = { type: $("#rc-type").value.trim(), spoken: $("#rc-spoken").value.trim(), slots: slotsFromForm() };
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
  if (!cad.rcEditing || !confirm(`Delete the run card "${cad.rcEditing}"?`)) return;
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
  fillTypeSelects();
  renderQueue();
  renderDetail();
  renderUnits();
  renderUnitLog();
  renderRunCards();
}

function onState(data) {
  cad.lastMsgAt = Date.now();
  if (data.error) { console.warn("CAD state error:", data.error); renderConn(); return; }
  cad.state = data;
  if (typeof data.time === "number") cad.skew = data.time - Date.now() / 1000;
  const tx = data.transmit || {};
  const pill = $("#tx-pill");
  pill.className = "pill " + (tx.enabled ? "tx-on" : "tx-off");
  setText(pill, tx.enabled ? "TRANSMIT ON" : "TRANSMIT OFF");
  setText($("#tx-note"), tx.enabled ? "" : (tx.note || "Transmit is off") + ".");
  renderConn();
  render();
}

const newAddr = addressBox($("#f-address"), $("#f-geo"), $("#f-sugg"));
const editAddr = addressBox($("#e-address"), $("#e-geo"), $("#e-sugg"));

$("#new-incident").addEventListener("click", openNew);
$("#new-cancel").addEventListener("click", () => { cad.mode = cad.sel ? "view" : "none"; render(); });
$("#new-form").addEventListener("submit", createIncident);
$("#d-accept").addEventListener("click", () => act("accept"));
$("#d-dismiss").addEventListener("click", () => {
  const why = prompt("Dismiss this page: why? (optional)");
  if (why !== null) act("dismiss", { reason: why });
});
$("#d-close").addEventListener("click", () => { if (confirm("Close this incident? Units still on it are cleared.")) act("close"); });
$("#d-edit").addEventListener("click", openEdit);
$("#edit-cancel").addEventListener("click", () => show($("#edit-form"), false));
$("#edit-form").addEventListener("submit", saveEdit);
$("#r-refresh").addEventListener("click", recommend);
$("#r-preview").addEventListener("click", preview);
$("#r-dispatch").addEventListener("click", dispatch);
$("#r-add-btn").addEventListener("click", () => {
  const id = Number($("#r-add").value);
  if (!id || !cad.rec) return;
  cad.rec.extra.push(id);
  cad.rec.checked.add(id);
  cad.rec.sig = null;
  renderDispatchBox(selected());
});
$("#note-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const inc = selected(), input = $("#note-text");
  if (!inc || !input.value.trim()) return;
  try {
    await api("POST", `/api/cad/incident/${inc.id}/note`, { text: input.value.trim(), operator: operator() });
    input.value = "";
  } catch (err) {
    alert(err.message);
  }
});
$("#runcards-open").addEventListener("click", openRunCards);
$("#rc-close").addEventListener("click", () => show($("#rc-overlay"), false));
$("#rc-form").addEventListener("submit", saveCard);
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
