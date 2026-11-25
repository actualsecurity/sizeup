// sizeup CAD Settings (/cad#settings/<section>): the lists, run cards, units, dispatch wording,
// users, records and exports, and a place for the Radios editor. Loaded after cad.js and uses its
// helpers ($, el, api, L(), fillSelect, operator(), ...).
//
// data/cad_lists.json is the defaults; every edit here is kept in the CAD database over it and
// takes effect at once for every console (the next state push). Each change records who made it
// (the Operator field) in the audit log (System). Codes never change: rename, recolor, reorder,
// retire (old events keep showing it), or delete what was never used. GET /api/cad/settings has
// everything this page shows; POST /api/cad/settings/<section> {action, ...} changes it.
// On the hub with Cloudflare Access only an admin may (sizeup/cad/api.py): for anyone else cad.js
// disables every control here but those marked ro-ok (they only read) and shows why (#s-role).
"use strict";

const S = {
  section: null, list: store("sizeup.cad.settings.list") || "event_types",
  data: null, sig: "", sigLive: "", loading: false, again: null, deferred: false, pressDeferred: false, deferFresh: false, pressing: 0, msgT: null,
  boardsOpen: null, audit: "", auditRows: null, auditFor: "", admin: null, importing: null, common: null,
  previewT: null, previewSeq: 0, tsOpen: new Set(), ftDraft: null,
  saved: "", pressT: null, refreshDue: false,                            // the section a save of ours just changed; the press's time limit
  act: null, actEmail: "", actKind: "", actKey: "", actAt: 0,
  rtab: store("sizeup.cad.settings.radios") || "receivers",        // Settings > Radios: the tab (#settings/radios/<tab>)
  toneQ: "", toneShow: "", toneType: "", toneEdit: null, unnamedEdit: null,      // Radios > Tones (#115, #122)
  toneOpen: new Set(), entEdit: null, entMerge: null, toneMove: null, toneAdd: null,
  pushSeq: 0, pushErr: {},                           // Push notifications: the newest read; a refusal, by field
  dsFocus: "", pushScrolled: "",                     // #settings/data/<key>: that row; #settings/system/pushes: that card
  stab: "", stationShown: false,                     // Settings > Station: the tab (#settings/station/<tab>)
};
// The sections, by what they are about: the CAD's lists and run cards, people, the radios, the station (its health, its
// logs, its hub link) and the hub's data sources, then System (records, backup, audit, the hub's own switches).
// Station (sStationTab): Health, Logs, Stations (each station's token, stations.js) and Adapter (the adapter's password,
// adapterpass.js) are the admins' on the hub (#203: the server refuses their data to anyone
// else), Hub link this machine's forwarding (not on the hub). #settings/health and #settings/system/health still land
// on Station > Health (old links in pushes and bookmarks).
const S_SECTIONS = [["lists", "Lists"], ["runcards", "Run cards"], ["units", "Units"], ["dispatch", "Dispatch"],
  ["users", "Users"], ["radios", "Radios"], ["station", "Station"], ["data", "Data sources"], ["hydrants", "Hydrants"], ["system", "System"]];
const S_STATION_TABS = [["health", "Health"], ["stations", "Stations"], ["workers", "STT workers"], ["logs", "Logs"], ["adapter", "Adapter"],
  ["link", "Hub link"]];
// Settings > Radios, one tab per part (#115; the Lists page's tabs): which RadioControls card, the Tones page and the
// Radio IDs editor each go under (data-rtab on each).
const S_RADIO_TABS = [["receivers", "Receivers"], ["channels", "Channels"], ["squelch", "Squelch"], ["signal", "Signal"], ["tones", "Tones"],
  ["ids", "Radio IDs"], ["vocabulary", "Vocabulary"], ["stt", "Speech-to-text"], ["emergencies", "Emergencies"]];
const S_RC_TABS = new Set(["receivers", "channels", "vocabulary", "stt"]);    // radio-controls.js's cards
// Channels' display names (#607): the radio rail's own store (sizeup/cad/radiofolders.py), so a name set in either place
// shows in both. The CAD's (the station's own page mounts RadioControls without: no names there)
const S_RC_NAMES = {
  load: async () => (await api("GET", "/api/cad/comms/sources")).folders || { rev: "", names: {} },
  save: async (key, name) => (await api("POST", "/api/cad/settings/radio_folders", { action: "name", key, name, operator: operator() }))
    .radio_folders || { rev: "", names: {} },
};
// ... and each source's service (fire, police, ...: the settings section radio_services), the rail's Edit sets it too
const S_RC_SERVICES = {
  load: async () => { const s = await api("GET", "/api/cad/comms/sources"); return { services: s.services || {}, waiting: s.retag_waiting || [] }; },
  save: async (key, service) => (await api("POST", "/api/cad/settings/radio_services", { action: "set", key, service, operator: operator() }))
    .radio_services || {},
};
const S_TONE_TYPES = [["fire", "Fire"], ["ems", "EMS"], ["police", "Police"], ["other", "Other"]];   // the services (#122)
const S_LISTS = [["event_types", "Event types"], ["event_subtypes", "Event subtypes"], ["type_subtypes", "Subtypes per type"],
  ["dispositions", "Dispositions"],
  ["received_via", "Received via"], ["unit_statuses", "Unit statuses"], ["location_qualifiers", "Location qualifiers"],
  ["event_class", "Emergency rules"], ["feed_types", "Feed types"], ["geo_town_order", "Town order"], ["place_names", "Place names"]];
const S_ROLES = [["", "Committed (on a call)"], ["free", "Available (in service)"], ["dispatched", "Dispatched (set on dispatch)"],
  ["enroute", "Responding (first en route)"], ["onscene", "On location (first on scene)"], ["oos", "Unavailable (out of service)"]];
const S_RESPONSES = [["fire", "Fire"], ["mutual_aid", "Mutual aid"], ["agency", "Other agency"]];
const S_OP_ROLES = [["admin", "Admin"], ["dispatcher", "Dispatcher"], ["viewer", "Viewer"]];

// ---- small builders -----------------------------------------------------------------------------

// Saved: a short toast (ui.js). An error stays at the top of the page until the next change.
function sMsg(text, ok) {
  const m = $("#s-msg");
  if (ok) { setText(m, ""); if (text) UI.toast(text); return; }
  setText(m, text || "");
}
function sBtn(text, title, fn, cls = "") {
  const b = el("button", cls, text);
  b.type = "button";
  if (title) b.title = title;
  b.addEventListener("click", (e) => { e.preventDefault(); fn(); });
  return b;
}
function sInput(value, max, placeholder, onChange, cls = "") {
  const i = el("input", cls);
  i.type = "text";
  i.maxLength = max;
  i.value = value || "";
  i.spellcheck = false;
  if (placeholder) { i.placeholder = placeholder; i.setAttribute("aria-label", placeholder); }
  if (onChange) i.addEventListener("change", () => onChange(i.value.trim()));
  return i;
}
function sSelect(items, value, onChange, label) {
  const s = el("select");
  fillSelect(s, items);
  s.value = value ?? "";
  if (label) s.setAttribute("aria-label", label);
  if (onChange) s.addEventListener("change", () => onChange(s.value));
  return s;
}
function sColor(value, onChange, label) {
  const c = el("input", "s-color");
  c.type = "color";
  c.value = (value || "#888888").toLowerCase();
  if (label) { c.title = label; c.setAttribute("aria-label", label); }
  if (onChange) c.addEventListener("change", () => onChange(c.value.toUpperCase()));
  return c;
}
function sState(state, retired) {
  const k = retired ? "retired" : state;
  return el("span", `chip s-state ${k}`, k);
}
function sConfirm(why) {          // "meaning to confirm": a guess the department still has to confirm
  const role = /^role/i.test(why || "");
  const c = el("span", "chip s-confirm", role ? "role unconfirmed" : "unconfirmed");
  c.title = why || "No name: shown as its code";
  return c;
}
function sCard(title, ...kids) {
  const c = el("div", "card s-card");
  if (title) c.append(title instanceof Node ? title : el("h2", "", title));
  c.append(...kids.filter(Boolean));
  return c;
}
// The section's explanation sits behind a "?" next to its title (help.js).
function sHelp(text) {
  return text ? Help.button(text) : null;
}
function sHead(title, about, ...tools) {
  const h = el("div", "s-head view-head");
  h.append(el("h2", "view-title", title), ...(about ? [sHelp(about)] : []), el("span", "spacer"), ...tools.filter(Boolean));
  return [h];
}
function sCardHead(title, info, ...tools) {          // a help button among the tools goes next to the title
  const h = el("div", "card-head");
  const kids = tools.filter(Boolean), help = kids.filter((t) => t.classList && t.classList.contains("help"));
  h.append(el("h2", "", title), ...help);
  if (info) h.append(el("span", "dim small", info));
  h.append(el("span", "spacer"), ...kids.filter((t) => !help.includes(t)));
  return h;
}
// A destructive change asks first (ui.js's confirm dialog), then runs fn.
function sAsk(text, fn, ok = "OK") {
  UI.confirm(text, { ok }).then((yes) => { if (yes) fn(); });
}
function short(v) {
  if (v === null || v === undefined || v === "") return "—";
  const t = typeof v === "string" ? v : JSON.stringify(v);
  return t.length > 140 ? t.slice(0, 137) + "…" : t;
}

// ---- a redraw never takes what someone is in the middle of ----------------------------------------
// The page's scroll position, the field with the focus (and its selection) and text typed but not saved outlive a
// redraw (sKeep); a card that only reports (Push notifications) is updated in place, node by node (sMorph).

const S_TEXTY = "input:not([type=file]):not([type=checkbox]):not([type=radio]):not([type=color]):not([type=range]), textarea";
// key -> field: its data-key; else its row's (data-row: a unit's or a list item's code), its label and which of them it
// is in that row; with no row, its label and which of them it is in the section (n: how many of each there are).
function sFields(root) {
  const out = new Map(), n = {};
  for (const f of root.querySelectorAll(S_TEXTY)) {
    let k = f.dataset.key;
    if (!k) {
      const row = f.closest("[data-row]");
      const b = (row && root.contains(row) ? `${row.dataset.row}/` : "") + (f.getAttribute("aria-label") || f.placeholder || f.name || f.type);
      n[b] = (n[b] || 0) + 1;
      k = `${b}#${n[b]}`;
      f._label = b;
      f._rowed = !!(row && root.contains(row));
    }
    out.set(k, f);
  }
  out.n = n;
  return out;
}
function sStamp(root) {            // what each field showed when it was drawn: differs = typed, not saved
  for (const f of root.querySelectorAll(S_TEXTY)) if (f._base === undefined) f._base = f.value;
}
function sKeep(root, build, text = true) {
  const sc = $("#settings .s-content"), page = document.scrollingElement;
  const top = sc ? sc.scrollTop : 0, ptop = page ? page.scrollTop : 0;
  const held = [];
  let was = {};
  if (text) {
    const fields = sFields(root);
    was = fields.n;
    for (const [k, f] of fields) {
      const on = f === document.activeElement;
      if (!on && (f._base === undefined || f.value === f._base)) continue;
      let a = null, b = null;
      try { a = f.selectionStart; b = f.selectionEnd; } catch (e) { /* a number field has none */ }
      held.push({ k, f, value: f.value, base: f._base, on, a, b });
    }
  }
  build();
  if (held.length) {
    const now = sFields(root);
    for (const h of held) {
      const f = now.get(h.k);
      if (!f) continue;
      // The same field: itself, or by its data-key, or by its row. Else by its place among its like, only while there
      // are as many of them as before and it still shows what it showed: rows that came, went or moved are not guessed
      // at (the text is dropped rather than put in another row's field).
      if (!(f === h.f || f.dataset.key || f._rowed) && (was[f._label] !== now.n[f._label] || (h.base !== undefined && f.value !== h.base))) continue;
      if (f !== h.f) { f._base = f.value; f.value = h.value; }
      if (h.on && document.activeElement !== f) {
        f.focus({ preventScroll: true });
        try { if (h.a !== null) f.setSelectionRange(h.a, h.b); } catch (e) { /* a number field */ }
      }
    }
  }
  sStamp(root);
  if (sc && sc.scrollTop !== top) sc.scrollTop = top;
  if (page && page.scrollTop !== ptop) page.scrollTop = ptop;
}
// old becomes neu without being rebuilt: same shape = only the text and attributes that differ change (a field keeps
// its element, its focus and what was typed in it); another shape = that part's children are replaced (sKeep).
const S_MORPH_ATTRS = ["class", "title", "placeholder", "aria-label", "aria-invalid", "aria-describedby", "href", "hidden", "data-help"];
function sMorph(old, neu) {
  const a = [...old.childNodes], b = [...neu.childNodes];
  // (a button is the same button only with the same label and the same data-act: the old node keeps its old click)
  const other = (c, n) => c.nodeType !== n.nodeType || c.nodeName !== n.nodeName || c.type !== n.type
    || (c.nodeType === 1 && (c.dataset.act !== n.dataset.act || (c.tagName === "BUTTON" && c.textContent !== n.textContent)));
  if (a.length !== b.length || a.some((c, i) => other(c, b[i]))) {
    sKeep(old, () => old.replaceChildren(...b));
    return;
  }
  a.forEach((c, i) => {
    const n = b[i];
    if (c.nodeType !== 1) { if (c.nodeValue !== n.nodeValue) c.nodeValue = n.nodeValue; return; }
    for (const at of S_MORPH_ATTRS) {
      const v = n.getAttribute(at);
      if (v === null) c.removeAttribute(at); else if (c.getAttribute(at) !== v) c.setAttribute(at, v);
    }
    if (c.type === "checkbox" || c.type === "radio") { if (c.checked !== n.checked) c.checked = n.checked; return; }
    if (c.tagName === "SELECT") {              // its options; the choice made in it stays
      if (c.innerHTML !== n.innerHTML) { c.replaceChildren(...n.childNodes); c.value = n.value; }
      return;
    }
    if (c.matches(S_TEXTY)) {
      const typed = c._base !== undefined && c.value !== c._base;
      c._base = n.value;
      if (!typed && c !== document.activeElement && c.value !== n.value) c.value = n.value;
      return;
    }
    sMorph(c, n);
  });
}
// A card whose buttons read their own fields and data (Emergency alarms, Intel, Audio retention, Camera wall) is never
// patched: it is drawn off the page and swapped in whole, only when it differs from what was drawn last, and never
// while a field in it has the focus or text typed and not saved, or a button of the section is being pressed (must:
// after a save of its own, which starts it clean).
function sBusy(root) {
  const a = document.activeElement;
  if (a && root.contains(a) && a.matches("input:not([type=file]):not([type=checkbox]), textarea, select")) return true;
  return [...root.querySelectorAll(S_TEXTY)].some((f) => f._base !== undefined && f.value !== f._base);
}
// What only reports and moves with the radio (a node marked data-live="<its name>": Intel's "in scope / considered" per
// pack, with every transcript) is no part of what the card is: its text, hover and class are left out of the
// signature and patched in place (sCardLive), so the card isn't swapped under a focused checkbox or a selection for it.
function sCardSig(card) {
  const live = [...card.querySelectorAll("[data-live]")].map((n) => [n, n.textContent, n.getAttribute("title"), n.className]);
  for (const [n] of live) { n.textContent = ""; n.removeAttribute("title"); n.className = ""; }
  const sig = card.innerHTML + JSON.stringify([...card.querySelectorAll("input, textarea, select")].map((f) => (f.type === "checkbox" ? f.checked : f.value)));
  for (const [n, text, title, cls] of live) {
    n.textContent = text;
    n.className = cls;
    if (title !== null) n.setAttribute("title", title);
  }
  return sig;
}
function sCardLive(card, next) {
  const now = new Map([...next.querySelectorAll("[data-live]")].map((n) => [n.dataset.live, n]));
  for (const n of card.querySelectorAll("[data-live]")) {
    const to = now.get(n.dataset.live);
    if (!to) continue;
    if (n.textContent !== to.textContent) n.textContent = to.textContent;
    if (n.title !== to.title) n.title = to.title;
    if (n.className !== to.className) n.className = to.className;
  }
}
function sCardSwap(card, next, must) {
  const sig = sCardSig(next);
  if (card.hidden !== next.hidden) card.hidden = next.hidden;
  if (card._sig === sig && !must) { sCardLive(card, next); return false; }
  if (!must && card._sig !== undefined && (sBusy(card) || sPressing())) return false;
  sStamp(next);
  sKeep(card, () => card.replaceChildren(...next.childNodes), false);
  card._sig = sig;
  return true;
}
async function sCardMount(card, mount, must) {          // mount(node): reads, then fills node (alarm.js, intel.js)
  if (!card) return;
  const seq = card._seq = (card._seq || 0) + 1;
  if (must) card._must = true;          // (it holds for whichever read lands: a newer one takes this one's place)
  const next = card.cloneNode(false);
  next.removeAttribute("id");
  await mount(next);
  if (seq !== card._seq) return;          // (a newer read is on its way)
  if (card._sig !== undefined && next.querySelector(":scope > .error")) return;          // a read that failed: what is shown stays
  sCardSwap(card, next, card._must);
  card._must = false;
}

// ---- load / save ----------------------------------------------------------------------------------

// The settings as they are now. The CAD's rev moves with every call, unit and transmission, the settings seldom (sHook
// reads them again at once only when settings_rev moves, else on a slower clock): the section is drawn again only when what it shows changed (or must: a save of ours), and then keeps the
// scroll position, the focus and typed text (fresh: the form that was just saved starts clean).
// "What it shows" is by section (S_PART_OF: a part of the settings drawn in one section only; a part not named is
// everyone's), and without what moves with the calls and the radio (S_LIVE_KEYS: a list item's "used 12×" and "3 now",
// a unit's status and its call, the clips and bytes kept per channel, the cameras streaming). Those redraw Lists and
// Units, which show them on every row (S_LIVE); in System their cards are updated in place (sRefresh).
const S_PART_OF = { lists: "lists", titles: "lists", units: "units", unit_kinds: "units", boards: "units", dispatch: "dispatch", reminders: "dispatch",
  operators: "users", tones: "radios", radio_emergencies: "radios", station_logs: "station",
  system: "system", audit_sections: "system", atc: "system", hub_errors: "system", map_display: "system",
  premise_visibility: "system", audio_retention: "system", camera_wall: "system", alert_mutes: "system", feed_types: "lists" };
const S_LIVE = new Set(["lists", "units"]);
const S_LIVE_KEYS = { lists: new Set(["used", "now"]), units: new Set(["used", "status", "incident_number"]),
  audio_retention: new Set(["clips", "bytes", "totals"]), camera_wall: new Set(["n", "streams"]),
  feed_types: new Set(["n", "last_at"]) };
function sSigs(d, sec) {           // [the section's settings, those and what moves with the calls]
  const quiet = [], live = [];
  for (const k of Object.keys(d).sort()) {
    if (k === "rev" || (S_PART_OF[k] && S_PART_OF[k] !== sec)) continue;
    let v = d[k];
    // (the retention rows come largest first: their order moves with the bytes)
    if (k === "audio_retention" && v && v.rows) v = { ...v, rows: [...v.rows].sort((a, b) => (a.key < b.key ? -1 : 1)) };
    const all = JSON.stringify(v), drop = S_LIVE_KEYS[k];
    live.push(k, all);
    quiet.push(k, drop ? JSON.stringify(v, (f, x) => (drop.has(f) ? undefined : x)) : all);
  }
  return [quiet.join("\n"), live.join("\n")];
}
async function sLoad(opts = {}) {
  if (S.loading) {
    S.again = { must: !!((S.again && S.again.must) || opts.must), fresh: !!((S.again && S.again.fresh) || opts.fresh) };
    return;
  }
  S.loading = true;
  if (cad.state) S.seenSettingsRev = cad.state.settings_rev;       // (sHook: read again when it moves)
  try {
    const d = await api("GET", "/api/cad/settings");
    S.loadedAt = Date.now();
    const [sig, live] = sSigs(d, S.section);
    const same = !!S.data && S.sigSec === S.section && sig === S.sig && (live === S.sigLive || !S_LIVE.has(S.section));
    S.data = d;
    S.sig = sig;
    S.sigLive = live;
    S.sigSec = S.section;
    if (!same || opts.must) sRender(false, !opts.fresh);
    if (same || S.deferred || S.pressDeferred) sRefresh();          // (a redraw that waits: what reports doesn't)
  } catch (e) {
    sMsg(`Load failed: ${e.message}`);
  } finally {
    S.loading = false;
    if (S.again) { const again = S.again; S.again = null; sLoad(again); }
  }
}
// No redraw (nothing in the section's settings changed, or it waits for a field): what is read apart from the settings
// or moves with the CAD catches up in place. System: the record counts, the audit log, the pushes sent, Emergency
// alarms and Intel (their own reads: another admin's change, the 24 h counts), the audio kept and the cameras on the
// wall. Users: who was seen when, at most every 15 s.
function sRefresh() {
  if (document.hidden) return;              // (not shown: its cards catch up once it is, sHook)
  if (sPressing()) { S.refreshDue = true; return; }          // (a row could change under the press: once it is over)
  S.refreshDue = false;
  if (S.section === "users") sActivity();
  if (S.section !== "system") return;
  sCounts();
  sAudit();
  const card = $("#s-pushes");
  if (card && card.dataset.built) sPushes(card);
  if (window.Alarm) sCardMount($("#s-alarms"), Alarm.mountSettings);
  if (window.IntelSettings) sCardMount($("#s-intel"), IntelSettings.mount);
  const kept = $("#s-audio-retention"), wall = $("#s-camera-wall");
  if (kept && S.data.audio_retention) sCardSwap(kept, sAudioRetention(S.data.audio_retention));
  if (wall && S.data.camera_wall) sCardSwap(wall, sCameraWall(S.data.camera_wall));
}

async function sPost(section, body, done) {
  const op = operator();
  if (!op) {
    sMsg("Enter operator name (top right)");
    ($("#operator-pick").hidden ? $("#operator") : $("#operator-pick")).focus();
    sRender(true, true);
    return null;
  }
  try {
    const r = await api("POST", `/api/cad/settings/${section}`, { ...body, operator: op });
    sMsg(done || "Saved", true);
    S.auditFor = "";
    S.saved = section;          // (its own card starts clean: sSystem)
    await sLoad({ must: true, fresh: true });
    return r;
  } catch (e) {
    sMsg(e.message);
    sRender(true, true);          // the switches go back to what is saved; what was typed stays
    return null;
  }
}

// ---- routing --------------------------------------------------------------------------------------

function sRoute() {
  const m = !BOARD && location.hash.match(/^#settings(?:\/([a-z]*))?(?:\/([a-z]+))?/);   // the wall board has no Settings
  const open = !!m;
  show($("#settings"), open);
  show(document.querySelector(".cad-main"), !open);
  document.body.classList.toggle("in-settings", open);
  if (window.renderTabs) renderTabs();
  if (!open) {
    S.section = null;
    if (cad.map) setTimeout(() => cad.map.resize(), 0);
    return;
  }
  let sec = m[1];
  if (sec === "tones") { sec = "radios"; S.rtab = "tones"; }          // #settings/tones: Radios > Tones
  if (sec === "health" || (sec === "system" && m[2] === "health")) {  // the old places (a push's link): Station > Health
    sec = "station";
    S.stab = "health";
    history.replaceState(null, "", "#settings/station/health");
  }
  if (sec === "station") {
    if (S_STATION_TABS.some(([k]) => k === m[2])) S.stab = m[2];
    sStationPick();          // (before the first state: the tab asked for is kept, the hash untouched)
  }
  S.dsFocus = sec === "data" ? m[2] || "" : "";
  if (location.hash !== "#settings/system/pushes") S.pushScrolled = "";
  if (sec === "radios") {
    if (S_RADIO_TABS.some(([k]) => k === m[2])) S.rtab = m[2];
    if (!S_RADIO_TABS.some(([k]) => k === S.rtab)) S.rtab = "receivers";
    store("sizeup.cad.settings.radios", S.rtab);
    const q = S.rtab === "signal" ? (location.hash.match(/\?[^#]*$/) || [""])[0] : "";   // a Health hint's ?s=FG1&at=
    if (location.hash !== `#settings/radios/${S.rtab}${q}`) history.replaceState(null, "", `#settings/radios/${S.rtab}${q}`);
  }
  if (!S_SECTIONS.some(([k]) => k === sec)) {
    sec = store("sizeup.cad.settings") || "lists";
    if (!S_SECTIONS.some(([k]) => k === sec)) sec = "lists";
    history.replaceState(null, "", `#settings/${sec}`);
  }
  S.section = sec;
  if (sec !== "station") store("sizeup.cad.settings", sec);      // (Station is never where Settings opens by itself)
  sStationTab();
  for (const a of document.querySelectorAll("#s-sections a")) {
    a.classList.toggle("on", a.dataset.s === sec);
    if (a.dataset.s === sec) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  for (const p of document.querySelectorAll(".s-panel")) show(p, p.dataset.s === sec);
  sMsg("");
  if (sec === "runcards") { showRunCards(); sRunTools(); }
  if (sec === "radios" && window.RadiosPanel) {        // the radio-ID work's editor (radios-panel.js)
    show($("#s-radios .s-note"), false);
    RadiosPanel.mount($("#settings-radios"), { operator });
  }
  if (sec === "radios") sRadioTab();
  if (sec === "hydrants" && window.Hydrants) Hydrants.mount($("#s-hydrants"));   // hydrants.js (#799): its own list
  if (S.data) {
    sRender(true);
    [S.sig, S.sigLive] = sSigs(S.data, sec);          // (drawn as it is: the load that follows redraws only what is new)
    S.sigSec = sec;
  }
  sLoad();
}

// force: now, whatever has the focus (a tab, an edit row, a refusal). keep: the scroll position, the focus and typed
// text outlive it (sKeep); without it only the scroll position does.
function sRender(force, keep = !force) {
  if (!S.data || !S.section) return;
  const panel = $(`#s-${S.section}`);
  const a = document.activeElement;
  if (!force && sPressing()) {          // between a button's press and its click: the button would be gone
    S.pressDeferred = true;
    S.deferFresh = S.deferFresh || !keep;
    return;
  }
  if (!force && panel.contains(a) && a.matches("input:not([type=file]):not([type=checkbox]), textarea, select")) {
    S.deferred = true;          // don't pull the field out from under someone typing
    S.deferFresh = S.deferFresh || !keep;
    return;
  }
  const fresh = S.deferFresh;
  S.deferred = S.pressDeferred = S.deferFresh = false;
  sStationTab();
  const fn = { lists: sLists, units: sUnits, dispatch: sDispatch, users: sUsers, system: sSystem, station: sStationPanel,
    radios: sRadios, data: sData }[S.section];
  if (fn) sKeep(panel, () => fn(panel), keep && !(fresh && !force));
  S.saved = "";
}
const S_PRESS_MS = 5000;           // a press with no release (the pointer left the window): over after this long
function sPressing() { return Date.now() - S.pressing < S_PRESS_MS; }
function sDeferredNow(pressed) {       // a redraw that waited: once the focus has left the section, or the press is over
  const p = S.section && $(`#s-${S.section}`);
  if (!p || sPressing()) return;
  if ((pressed && S.pressDeferred) || (S.deferred && !p.contains(document.activeElement))) sRender();
  if (pressed && S.refreshDue) sRefresh();
}

// ---- radios: the tabs, and Tones (sTonePage, #115; the rest of the section is radios-panel.js, radio-controls.js) ----

// The tab bar (the Lists page's), the tab's parts shown, the rest hidden; RadioControls polls only while its
// cards are on screen (mount again starts it).
function sRadioTab() {
  const panel = $("#s-radios");
  if (!panel) return;
  const bar = $("#s-radio-tabs");
  bar.replaceChildren(...S_RADIO_TABS.map(([k, t]) => {
    const b = sBtn(t, "", () => {
      S.rtab = k;
      store("sizeup.cad.settings.radios", k);
      history.replaceState(null, "", `#settings/radios/${k}`);
      sRadioTab();
      sRender(true);
    }, "tab" + (S.rtab === k ? " on" : ""));
    b.setAttribute("aria-pressed", String(S.rtab === k));
    return b;
  }));
  const rc = $("#settings-radio-controls");
  show(rc, S_RC_TABS.has(S.rtab));
  // receivers, talkgroups, vocabulary; the reception test card under Receivers only (#436)
  if (S_RC_TABS.has(S.rtab) && window.RadioControls) RadioControls.mount(rc, { reception: true, tab: S.rtab, names: S_RC_NAMES, services: S_RC_SERVICES,
                                                                       models: window.SttWorkersCard ? SttWorkersCard.models : null });
  for (const n of panel.querySelectorAll("[data-rtab]")) show(n, n.dataset.rtab === S.rtab);
  if (S.rtab === "squelch" && window.SquelchPanel) SquelchPanel.mount($("#settings-squelch"));   // squelch-panel.js
  if (S.rtab === "signal" && window.SignalPanel) SignalPanel.mount($("#settings-signal"));       // signal-panel.js (#606)
}

function sRadios() {
  const box = $("#settings-tones");
  if (box && S.rtab === "tones") box.replaceChildren(...sTonePage(S.data.tones));
  const em = $("#settings-emergencies");
  if (em && S.rtab === "emergencies" && S.data.radio_emergencies) em.replaceChildren(sEmergencies(S.data.radio_emergencies));
  const th = $("#settings-signal-thresholds");
  if (th && S.rtab === "signal" && S.data.signal_metrics && sIsHub()) th.replaceChildren(sSignalThresholds(S.data.signal_metrics));
}

// Settings > Radios > Signal (#606, sizeup/cloud/metricsinsight.py): the suggested squelch's margin and the shift
// warnings' thresholds. Admins, audited as Radios; the hub only (the metrics are the hub's).
function sSignalThresholds(d) {
  const card = sCard(sCardHead("Thresholds", "", sHelp("Suggested Open: the idle quieting's p90 (7 days, or since the last "
    + "dongle / gain change) plus the margin, never above more than the % of real transmissions (0: none); in doubt, none "
    + "is offered. Shift warnings: "
    + "after an SDR, gain, squelch, plug or restart event, the 6 h after against the day before; a Health hint, never a push.")));
  card.id = "s-signal-thresholds";
  for (const [key, v] of Object.entries(d)) {
    const n = el("input", "s-num");
    n.type = "number"; n.min = String(v.min); n.max = String(v.max); n.step = "0.5";
    n.value = String(v.value);
    n.disabled = !canDo("admin");
    n.setAttribute("aria-label", v.label);
    n.addEventListener("change", () => sPost("signal_metrics", { action: "set", key, value: Number(n.value) },
      `${v.label}: ${n.value}`));
    const row = el("div", "row");
    row.append(el("span", "s-eta-l", v.label), n, sState(v.edited ? "edited" : "default"), el("span", "spacer"));
    if (v.edited && canDo("admin")) row.append(sBtn("Reset", `Default: ${v.default}`,
      () => sPost("signal_metrics", { action: "reset", key }, `${v.label}: ${v.default}`)));
    card.append(row);
  }
  return card;
}

// Settings > Radios > Emergencies: whose emergencies (not ours) stand out on the Radio page: a banner over the list,
// the row marked, the full chip. Any other service's: a muted EM chip only. Presentation only. Admins, audited.
const S_EM_LABELS = { fire: "Fire", ems: "EMS", police: "Police", transit: "Transit", air: "Air", other: "Other" };
function sEmergencies(e) {
  const card = sCard(sCardHead("Emergencies", "", sHelp("Someone else's emergency button on these services stands out on the "
    + "Radio page (a banner over the list, the row marked). On any other service (a bus, DOT): a muted EM chip, its "
    + "detail in the hover. Ours (our fire channels, our rigs, scene safety) and a channel with no known service always "
    + "stand out. What is recorded, kept, "
    + "alarmed and pushed doesn't change.")));
  card.id = "s-radio-emergencies";
  const row = el("div", "row");
  row.append(el("span", "s-eta-l", "Prominent"));
  for (const k of e.choices) {
    const [lab, cb] = sCheck(S_EM_LABELS[k] || k, e.prominent.includes(k));
    cb.disabled = !canDo("admin");
    cb.addEventListener("change", () => {
      const v = e.choices.filter((x) => (x === k ? cb.checked : e.prominent.includes(x)));
      sPost("radio_emergencies", { action: "set", key: "prominent", value: v },
        `Prominent emergencies: ${v.map((x) => S_EM_LABELS[x] || x).join(", ") || "none"}`);
    });
    row.append(lab);
  }
  row.append(sState(e.edited ? "edited" : "default"), el("span", "spacer"));
  if (e.edited && canDo("admin")) row.append(sBtn("Reset", `Default: ${e.default.map((x) => S_EM_LABELS[x] || x).join(", ")}`,
    () => sPost("radio_emergencies", { action: "reset", key: "prominent" }, "Prominent emergencies: default")));
  card.append(row);
  return card;
}

const sHz = (v) => (v ? Number(v).toFixed(1) : "—");
const sPair = (a, b) => `${sHz(a)}/${b ? sHz(b) : "-"}`;
const sNoBreakDates = (t) => t.replace(/(\d{4})-(\d\d)-(\d\d)/g, "$1‑$2‑$3");   // a date never wraps
function sPairSource(p) {
  const by = p.operator ? opParts(p.operator)[0] : "";
  if (p.origin === "named") return `Named by ${by || "—"}${p.at ? `, ${fmtTime(p.at)}` : ""}`;
  if (p.origin === "config") return "Config ([[tones]])";
  const src = `${p.origin === "heard" ? "Heard on air" : "County list"} (${p.source || "?"}${p.fetched ? `, ${p.fetched}` : ""})`;
  return (p.state === "edited" || p.state === "deleted") ? `${src}; ${p.state} by ${by || "—"}${p.at ? `, ${fmtTime(p.at)}` : ""}` : src;
}
function sTd(cls, label, ...kids) {
  const td = el("td", cls);
  if (label) td.dataset.l = label;
  td.append(...kids.filter((k) => k !== null && k !== undefined));
  return td;
}
function sCheck(label, checked, title) {
  const l = el("label", "check");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = !!checked;
  l.append(cb, el("span", "", label));
  if (title) l.title = title;
  return [l, cb];
}
function sServiceSelect(value) {
  return sSelect(S_TONE_TYPES, value || "fire", null, "Service");
}
// Places + service + role, the way the dialog takes them (cad.js tonePicker): places from the one canonical list
// (new ones allowed), an entity's name or alias fills both.
function sPicker(d, init) {
  return tonePicker({ places: d.places || [], choices: d.choices || [] }, init || {});
}
const sEntOf = (d, key) => (d.entities || []).find((e) => e.key === key);

// The tone book (#122): one row per entity (its places and service, aliases, ours, pairs, last heard), opened to its
// pairs (A, B, role, status, source, heard). Search finds an entity by name, alias or Hz.
function sTonePage(d) {
  if (!d) return [el("div", "empty", "No tone list here")];
  const ents = d.entities || [];
  const t = el("table", "evtable s-tones s-ents");
  const head = el("tr");
  for (const h of ["", "Entity", "Aliases", "Ours", "Pairs", "Last heard", "Heard", ""]) head.append(el("th", "", h));
  t.append(el("thead"), el("tbody"));
  t.tHead.append(head);
  const body = t.tBodies[0];
  for (const e of ents) {
    body.append(S.entEdit === e.key ? sEntEditRow(d, e) : sEntRow(d, e));
    if (S.entMerge === e.key) body.append(sMergeRow(d, e));
    if (S.toneOpen.has(e.key) || S.toneQ.trim()) for (const p of e.pairs) {
      body.append(S.toneEdit === p.id ? sPairEditRow(d, e, p) : sPairRow(d, e, p));
      if (S.toneMove && S.toneMove.id === p.id) body.append(sMoveRow(d, e, p));
    }
  }
  const count = el("span", "dim small");
  const filter = () => {
    const q = S.toneQ.trim().toLowerCase();
    let n = 0;
    for (const tr of body.rows) {
      const e = sEntOf(d, tr.dataset.ent);
      if (!e) continue;
      const pairs = tr.dataset.pair ? e.pairs.filter((p) => p.id === tr.dataset.pair) : e.pairs;
      const hit = (p) => `${sPair(p.a_hz, p.b_hz)} ${p.a_hz} ${p.b_hz || ""} ${p.role}`.toLowerCase().includes(q);
      const entHit = `${e.name} ${e.aliases.join(" ")} ${e.notes}`.toLowerCase().includes(q);
      const ok = (!q || entHit || pairs.some(hit))
        && (!S.toneType || e.service === S.toneType)
        && (!S.toneShow || (S.toneShow === "ours" ? e.ours : S.toneShow === "conflict" ? (tr.dataset.pair ? pairs.some((p) => p.conflicts.length) : e.conflict)
          : S.toneShow === "unconfirmed" ? pairs.some((p) => !p.confirmed && p.state !== "deleted")
            : S.toneShow === "edited" ? e.edited || pairs.some((p) => ["edited", "named"].includes(p.state))
              : pairs.some((p) => p.state === "deleted")));
      tr.hidden = !ok;
      if (ok && !tr.dataset.pair) n++;
    }
    count.textContent = n === ents.length ? `${ents.length} entities` : `${n} of ${ents.length} entities`;
  };
  const q = el("input", "s-tone-q");
  Object.assign(q, { type: "search", placeholder: "Search a name, alias or Hz", value: S.toneQ, spellcheck: false });
  q.setAttribute("aria-label", "Search the tone book");
  q.addEventListener("input", () => {
    const had = !!S.toneQ.trim();
    S.toneQ = q.value;
    if (had !== !!S.toneQ.trim()) { sRender(true); setTimeout(() => { const n = $(".s-tone-q"); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } }, 0); return; }
    filter();
  });
  const showSel = sSelect([["", "All"], ["ours", "Ours"], ["conflict", "Conflicts"], ["unconfirmed", "Unconfirmed"],
    ["edited", "Edited here"], ["deleted", "Taken out"]], S.toneShow, (v) => { S.toneShow = v; filter(); }, "Show");
  const typeSel = sSelect([["", "Any service"], ...S_TONE_TYPES], S.toneType, (v) => { S.toneType = v; filter(); }, "Service");
  showSel.classList.add("ro-ok");
  typeSel.classList.add("ro-ok");
  const tools = el("div", "row s-tone-tools");
  tools.append(q, showSel, typeSel, count);
  filter();
  const conflicts = (d.report && d.report.conflicts) || [];
  const add = S.toneAdd ? null : sBtn("Add pair", "A pair for an entity (or a new one)", () => { S.toneAdd = {}; sRender(true); }, "primary");
  const card = sCard(sCardHead("Tone book", "", sHelp("Every agency the decoder knows as its places and a service "
    + "(Port Chester · Fire; Port Chester · Rye · Rye Brook · EMS), each with its tone pairs and a role for each pair "
    + "(All call, Chiefs, Station 1). Read from the county list (data/westchester_tones.csv: a name with no service word "
    + "is Fire) and the pairs named here; edits are kept over the list, never written into it: Reset takes them off. "
    + "Ours is the entity's: PCFD's and Rye Brook's pages become pending events. A pair under two entities is a "
    + "conflict: it matches both until you move, merge or take one out. Changes go to the station with the channel "
    + "choices: its live paging display names them."), add),
  ...(conflicts.length ? [el("p", "warnbox small", `${conflicts.length} pair${conflicts.length === 1 ? "" : "s"} under two entities: ${conflicts.join("; ")}`)] : []),
  ...(S.toneAdd ? [sToneAddForm(d)] : []), tools, el("div", "tscroll").appendChild(t).parentNode);
  return [card, sUnnamed(d)];
}

function sEntRow(d, e) {
  const tr = el("tr", "s-erow" + (e.conflict ? " conflict" : ""));
  tr.dataset.ent = e.key;
  const open = S.toneOpen.has(e.key) || !!S.toneQ.trim();
  const tog = sBtn(open ? "▾" : "▸", open ? "Close" : "Its pairs", () => {
    if (S.toneOpen.has(e.key)) S.toneOpen.delete(e.key); else S.toneOpen.add(e.key);
    sRender(true);
  }, "mini icon quiet ro-ok");
  tog.setAttribute("aria-expanded", String(open));
  const name = sTd("s-t-name", "", el("b", "", e.name), e.edited ? el("span", "chip s-state edited", "edited") : null,
    e.conflict ? el("span", "chip s-confirm", "conflict") : null);
  if (e.notes) name.title = e.notes;
  const ours = e.ours ? el("span", "chip s-state added", "ours") : el("span", "dim", "—");
  if (e.ours_locked) ours.title = "PCFD's or Rye Brook's: always ours";
  const acts = sTd("s-t-act");
  acts.append(sBtn("Edit", "Ours, notes, aliases", () => { S.entEdit = e.key; sRender(true); }, "mini"),
    sBtn("Merge", "Its pairs under another entity", () => { S.entMerge = e.key; sRender(true); }, "mini"),
    sBtn("Add pair", "", () => { S.toneAdd = { places: e.places, service: e.service }; sRender(true); }, "mini"));
  if (e.edited) acts.append(sBtn("Reset", "As the list reads it", () => sPost("tones", { action: "reset", key: `entity:${e.key}` }, `${e.name} reset`), "mini"));
  tr.append(sTd("s-t-tog", "", tog), name, sTd("s-t-src dim small", "Also", e.aliases.join(", ") || "—"), sTd("s-t-ours", "", ours),
    sTd("s-t-n", "Pairs", String(e.count)), sTd("s-t-last t", "Last heard", e.last ? fmtTime(e.last) : "—"),
    sTd("s-t-n", "Heard", String(e.heard || 0)), acts);
  return tr;
}

function sEntEditRow(d, e) {
  const tr = el("tr", "s-erow s-tedit");
  tr.dataset.ent = e.key;
  const [oursL, ours] = sCheck("Ours", e.ours, "Its pages become pending events");
  if (e.ours_locked) ours.disabled = true;
  const notes = sInput(e.notes, 300, "Notes", null, "s-name");
  const al = sInput(e.aliases.filter((a) => !e.pairs.some((p) => p.list && p.list.name === a)).join(", "), 600, "Other names, comma-separated", null, "s-name");
  const save = () => sPost("tones", { action: "entity", key: e.key, ...(e.ours_locked ? {} : { ours: ours.checked }), notes: notes.value.trim(),
    aliases: al.value.split(",").map((x) => x.trim()).filter(Boolean) }, `${e.name} saved`).then((r) => { if (r) { S.entEdit = null; sRender(true); } });
  tr.append(sTd("s-t-tog", ""), sTd("s-t-name", "", el("b", "", e.name), notes), sTd("s-t-src", "", al), sTd("s-t-ours", "", oursL),
    sTd("s-t-n", "", ""), sTd("s-t-last", "", ""), sTd("s-t-n", "", ""),
    sTd("s-t-act", "", sBtn("Cancel", "", () => { S.entEdit = null; sRender(true); }, "mini"), sBtn("Save", "", save, "mini primary")));
  setTimeout(() => notes.focus(), 0);
  return tr;
}

function sMergeRow(d, e) {
  const tr = el("tr", "s-prow s-tedit");
  tr.dataset.ent = e.key;
  const others = (d.entities || []).filter((x) => x.key !== e.key);
  const sel = sSelect(others.map((x) => [x.key, x.name]), "", null, "Merge into");
  const go = () => {
    const into = sEntOf(d, sel.value);
    if (!into) return;
    sAsk(`Put ${e.name}'s ${e.count} pair${e.count === 1 ? "" : "s"} under ${into.name}? (Reset on ${into.name}'s aliases undoes it.)`,
      () => sPost("tones", { action: "merge", key: e.key, into: { places: into.places, service: into.service } }, `${e.name} merged into ${into.name}`)
        .then((r) => { if (r) { S.entMerge = null; sRender(true); } }), "Merge");
  };
  const td = sTd("s-t-naming", "", el("span", "dim small", `Merge ${e.name} into`), sel,
    sBtn("Cancel", "", () => { S.entMerge = null; sRender(true); }, "mini"), sBtn("Merge", "", go, "mini primary"));
  td.colSpan = 8;
  tr.append(td);
  return tr;
}

function sPairRow(d, e, p) {
  const tr = el("tr", `s-prow ${p.state}` + (p.conflicts.length ? " conflict" : ""));
  tr.dataset.ent = e.key;
  tr.dataset.pair = p.id;
  const stateChip = { edited: ["edited", "edited"], named: ["added", "named here"], deleted: ["deleted", "taken out"] }[p.state];
  const status = p.confirmed ? el("span", "dim", "confirmed") : sConfirm("An estimate: a heard or listed name wins over it");
  if (!p.confirmed) status.textContent = "estimated";
  const role = sTd("s-t-name", "", el("span", "mono", sPair(p.a_hz, p.b_hz)), el("span", "", p.role ? ` · ${p.role}` : ""),
    stateChip ? el("span", `chip s-state ${stateChip[0]}`, stateChip[1]) : null,
    p.conflicts.length ? el("span", "chip s-confirm", `also ${p.conflicts.join(", ")}`) : null);
  const acts = sTd("s-t-act");
  if (p.state === "deleted") {
    acts.append(sBtn("Reset to list", "Back in the plan as the list has it", () => sPost("tones", { action: "reset", key: p.id }, `${sPair(p.a_hz, p.b_hz)} back`), "mini"));
  } else {
    acts.append(sBtn("Edit", "Role, confirmed", () => { S.toneEdit = p.id; sRender(true); }, "mini"),
      sBtn("Move", "Under another entity", () => { S.toneMove = { id: p.id, mode: "move" }; sRender(true); }, "mini"),
      sBtn("Split", "Into a new entity", () => { S.toneMove = { id: p.id, mode: "split" }; sRender(true); }, "mini"));
    if (p.state === "edited") acts.append(sBtn("Reset to list", `As the list has it: ${p.list ? p.list.name : ""}`, () =>
      sPost("tones", { action: "reset", key: p.id }, `${sPair(p.a_hz, p.b_hz)} as listed`), "mini"));
    acts.append(sBtn(p.origin === "named" ? "Remove" : "Take out", p.origin === "named" ? "Forget it: the pair is unknown again"
      : "Out of the plan (Reset to list brings it back)", () => sAsk(`${p.origin === "named" ? "Forget" : "Take out"} ${sPair(p.a_hz, p.b_hz)} Hz of ${e.name}?`,
      () => sPost("tones", { action: "delete", key: p.id }, `${sPair(p.a_hz, p.b_hz)} ${p.origin === "named" ? "removed" : "taken out"}`),
      p.origin === "named" ? "Remove" : "Take out"), "mini danger-quiet"));
  }
  const src = sTd("s-t-src dim small", "", sNoBreakDates(sPairSource(p)) + (p.list && p.list.name ? ` · listed as ${p.list.name}` : ""));
  tr.append(sTd("s-t-tog", ""), role, src, sTd("s-t-conf", "", status), sTd("s-t-n", "", ""),
    sTd("s-t-last t", "Last heard", p.last ? fmtTime(p.last) : "—"), sTd("s-t-n", "Heard", String(p.heard || 0)), acts);
  return tr;
}

function sPairEditRow(d, e, p) {
  const tr = el("tr", "s-prow s-tedit");
  tr.dataset.ent = e.key;
  tr.dataset.pair = p.id;
  const role = sInput(p.role, 40, "Role (All call, Chiefs, Station 1 …)", null, "s-name");
  const [confL, conf] = sCheck("Confirmed", p.confirmed, "Unticked: an estimate");
  const save = () => sPost("tones", { action: "set", a_hz: p.a_hz, b_hz: p.b_hz || null, places: e.places, service: e.service,
    role: role.value.trim(), confirmed: conf.checked, ...(p.origin === "named" ? {} : { list: p.id.split("@")[1] }) }, `${sPair(p.a_hz, p.b_hz)} saved`)
    .then((r) => { if (r) { S.toneEdit = null; sRender(true); } });
  role.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); save(); } });
  tr.append(sTd("s-t-tog", ""), sTd("s-t-name", "", el("span", "mono", sPair(p.a_hz, p.b_hz)), role), sTd("s-t-src dim small", "", sNoBreakDates(sPairSource(p))),
    sTd("s-t-conf", "", confL), sTd("s-t-n", "", ""), sTd("s-t-last", "", ""), sTd("s-t-n", "", ""),
    sTd("s-t-act", "", sBtn("Cancel", "", () => { S.toneEdit = null; sRender(true); }, "mini"), sBtn("Save", "", save, "mini primary")));
  setTimeout(() => role.focus(), 0);
  return tr;
}

// Move a pair under another entity (pick it), or split it into a new one (places + service).
function sMoveRow(d, e, p) {
  const tr = el("tr", "s-prow s-tedit");
  tr.dataset.ent = e.key;
  tr.dataset.pair = p.id;
  let what, value;
  if (S.toneMove.mode === "move") {
    const sel = sSelect((d.entities || []).filter((x) => x.key !== e.key).map((x) => [x.key, x.name]), "", null, "Move to");
    what = sel;
    value = () => { const x = sEntOf(d, sel.value); return x ? { places: x.places, service: x.service } : null; };
  } else {
    const pk = sPicker(d, { places: [], service: e.service });
    what = pk.box;
    value = () => (pk.places().length ? { places: pk.places(), service: pk.service() } : null);
  }
  const go = () => {
    const v = value();
    if (!v) { sMsg("Pick where it goes"); return; }
    sPost("tones", { action: "move", key: p.id, ...v }, `${sPair(p.a_hz, p.b_hz)} moved`).then((r) => { if (r) { S.toneMove = null; sRender(true); } });
  };
  const td = sTd("s-t-naming", "", el("span", "dim small", `${S.toneMove.mode === "move" ? "Move" : "Split"} ${sPair(p.a_hz, p.b_hz)} to`), what,
    sBtn("Cancel", "", () => { S.toneMove = null; sRender(true); }, "mini"), sBtn(S.toneMove.mode === "move" ? "Move" : "Split", "", go, "mini primary"));
  td.colSpan = 8;
  tr.append(td);
  return tr;
}

// Add a pair: A, B, the entity (places + service, or an existing one by name), role, confirmed.
function sToneAddForm(d) {
  const box = el("div", "s-add s-tone-add");
  const hz = (label) => {
    const i = el("input", "s-num");
    Object.assign(i, { type: "number", min: 250, max: 3500, step: 0.1, placeholder: label });
    i.setAttribute("aria-label", `${label} (Hz)`);
    return i;
  };
  const a = hz("A Hz"), b = hz("B Hz");
  const pk = sPicker(d, S.toneAdd || {});
  const [confL, conf] = sCheck("Confirmed", true, "Unticked: an estimate");
  const save = () => {
    const places = pk.places();
    if (!(Number(a.value) > 0)) { sMsg("A tone: 250 to 3500 Hz"); a.focus(); return; }
    if (!places.length) { sMsg("Pick a place"); return; }
    sPost("tones", { action: "set", a_hz: Number(a.value), b_hz: b.value ? Number(b.value) : null, places, service: pk.service(),
      role: pk.role(), ours: pk.ours(), confirmed: conf.checked }, "Pair added").then((r) => { if (r) { S.toneAdd = null; sRender(true); } });
  };
  const row = el("div", "row");
  row.append(a, b, confL, el("span", "spacer"), sBtn("Cancel", "", () => { S.toneAdd = null; sRender(true); }, "mini"), sBtn("Save", "", save, "mini primary"));
  box.append(el("b", "small", "New pair"), pk.box, row);
  setTimeout(() => a.focus(), 0);
  return box;
}

// Heard but unnamed: the pairs heard on the air nobody's code matches, newest first, each with its latest page to
// play (with the tones) and Name in place (the dialog's flow: that page is read again; ours: its event is queued).
function sUnnamed(d) {
  const rows = d.unnamed || [];
  const t = el("table", "evtable s-tones s-unnamed");
  const head = el("tr");
  for (const h of ["A", "B", "First heard", "Last heard", "Pages", "", ""]) head.append(el("th", "", h));
  t.append(el("thead"), el("tbody"));
  t.tHead.append(head);
  for (const u of rows) {
    const key = sPair(u.a_hz, u.b_hz);
    const tr = el("tr", "s-trow");
    tr.dataset.id = key;
    const playB = Icon.play(el("button", "play ro-ok"));
    playB.title = u.has_audio ? `Play its latest page (${fmtTime(u.last)} ${u.channel}), tones and all` : "Recording expired (12 h)";
    playB.disabled = !u.has_audio;
    playB.addEventListener("click", () => play(clipUrl({ id: u.page }, true)));
    const acts = sTd("s-t-act");
    if (S.unnamedEdit === key) {
      const pk = sPicker(d, {});
      const save = () => {
        const places = pk.places();
        if (!places.length) { sMsg("Pick a place"); return; }
        sPost("tones", { action: "set", a_hz: u.a_hz, b_hz: u.b_hz || null, places, service: pk.service(), role: pk.role(),
          ours: pk.ours(), page: u.page }, `${key} Hz named`).then((res) => { if (res) { S.unnamedEdit = null; sRender(true); } });
      };
      acts.append(pk.box, sBtn("Cancel", "", () => { S.unnamedEdit = null; sRender(true); }, "mini"), sBtn("Save", "", save, "mini primary"));
      acts.classList.add("s-t-naming");
    } else {
      acts.append(sBtn("Name", "Say whose tones these are: in the plan, and its latest page read again", () => { S.unnamedEdit = key; sRender(true); }, "mini"));
    }
    tr.append(sTd("s-t-hz mono", "A", sHz(u.a_hz)), sTd("s-t-hz mono", "B", u.b_hz ? sHz(u.b_hz) : "-"),
      sTd("s-t-last t", "First", fmtTime(u.first)), sTd("s-t-last t", "Last", fmtTime(u.last)),
      sTd("s-t-n", "Pages", String(u.count)), sTd("s-t-play", "", playB), acts);
    t.tBodies[0].append(tr);
  }
  if (!rows.length) {
    const tr = el("tr");
    tr.append(Object.assign(el("td", "empty", "None unnamed"), { colSpan: 7 }));
    t.tBodies[0].append(tr);
  }
  return sCard(sCardHead("Heard but unnamed", rows.length ? `${rows.length}` : "", sHelp("Pairs heard on the air that no "
    + "code in the plan matches (within 1.5 %), one row per pair, newest first: when it was first and last heard, on how "
    + "many pages, and its latest page to play. Name it here as from a page row (places, service, an optional role): the "
    + "pair goes in the tone book, every page row with it says the name, and its latest page is read again (an entity "
    + "of ours: its event is queued).")),
  el("div", "tscroll").appendChild(t).parentNode);
}

// When to read the settings again. Their own version (the state's settings_rev: any setting written, removed or
// audited, a user added or edited) at once. The CAD's rev moves with every call, unit and transmission: what moves with
// it (S_LIVE_KEYS: "used 3×", a unit's status, the audio kept) is read again at most every S_POLL.live ms in the
// sections that show it on every row (S_LIVE), every S_POLL.other ms in the rest (each read builds every section on
// the hub: read on every rev, an open Settings page asked about every 1.5 s, 1-2.5 s each, slowing every other request).
const S_POLL = { live: 5000, other: 30000 };
function sHook() {                 // every render (cad.js): keep up with edits made elsewhere
  sStationTab();
  if (S.section === "station" && cad.state && !S.stationShown && S.data) sRender(true);   // the first state: its tab
  if (!S.section) return;
  if (S.section === "runcards") sRunTools();
  if (!cad.state || !S.data || S.loading) return;
  const st = cad.state;
  if (st.settings_rev !== S.seenSettingsRev) { sLoad(); return; }          // (the state's, as at the last read)
  if (document.hidden) return;              // a tab not shown reads nothing on the clock (back: the next render does)
  const every = S_LIVE.has(S.section) ? S_POLL.live : S_POLL.other, now = Date.now();
  if (st.rev !== S.data.rev && now - (S.loadedAt || 0) >= every) { sLoad(); return; }
  // what System reads apart from the settings (the counts, the audit log, Emergency alarms, Intel's counters) keeps up
  // with the calls by itself, each card its own small read: at most every S_POLL.live ms, never the whole settings
  if ((S.section === "system" || S.section === "users") && st.rev !== S.refreshRev && now - (S.refreshedAt || 0) >= S_POLL.live) {
    S.refreshRev = st.rev;
    S.refreshedAt = now;
    sRefresh();
  }
}

// ---- lists ----------------------------------------------------------------------------------------

function sLists(panel) {
  const tabs = el("div", "s-tabs tabs");
  for (const [k, t] of S_LISTS) {
    const b = sBtn(t, "", () => { S.list = k; store("sizeup.cad.settings.list", k); sRender(true); }, "tab" + (S.list === k ? " on" : ""));
    b.setAttribute("aria-pressed", String(S.list === k));
    tabs.append(b);
  }
  let body;
  if (S.list === "event_class") body = sClassRules();
  else if (S.list === "geo_town_order") body = sTowns();
  else if (S.list === "place_names") body = sPlaceNames();
  else if (S.list === "type_subtypes") body = sTypeMap();
  else if (S.list === "feed_types") body = sFeedTypes();
  else body = sListTable(S.list);
  const flow = S.list === "unit_statuses" && S.data.status_flow ? sFlow() : null;
  const common = S.list === "place_names" ? sCommonPlaces() : null;
  const points = S.list === "place_names" ? sAddressPoints() : null;
  panel.replaceChildren(...sHead("Lists", "Codes don't change: they are on events and in the CSV exports. Names, colors "
    + "and order are yours. Retire what you no longer use: old events keep it, the pick lists drop it."), tabs, body,
  ...(flow ? [flow] : []), ...(points ? [points] : []), ...(common ? [common] : []));
  if (points) sAddrMapShown();
}

function sListTable(sec) {
  const d = S.data.lists[sec];
  const title = S.data.titles[sec];
  const items = d.items;
  const live = items.filter((x) => x.state !== "deleted");
  const reset = d.edited ? sBtn("Reset to default", "Data file items and order",
    () => { sAsk(`Reset ${title.toLowerCase()} to defaults?`, () => sPost(sec, { action: "reset_section" }, `${title} reset`), "Reset"); },
    "danger-quiet") : null;
  const ol = el("ol", "s-rows");
  items.forEach((x) => ol.append(sItemRow(sec, x, live.indexOf(x), live.length)));
  const todo = live.filter((x) => x.confirm && !x.retired).length;
  const info = `${live.filter((x) => !x.retired).length} in use` + (live.some((x) => x.retired) ? `, ${live.filter((x) => x.retired).length} retired` : "")
    + (todo ? `, ${todo} to confirm` : "");
  const about = {
    unit_statuses: "The department's status codes and names. What a status does (its role) is a best guess where marked "
      + "unconfirmed: change it, or Confirm. Available frees the unit and clears it off its call. Unavailable takes it out "
      + "of service and asks why. Dispatched, Responding and On location stamp the times. The rest keep it committed.",
    event_types: "The type's color on the queue, the board and the records.",
    location_qualifiers: "Where at a location, in front of the address. Name: the pick list. Short: the text (IFO 12 Sample St). "
      + "Spoken: what the dispatch voice says and what pages are heard as. Never moves a point on the map.",
  }[sec];
  const partial = (sec === "event_types" || sec === "event_subtypes") ? sSubtypeMap() : null;
  return sCard(sCardHead(title, info, sHelp(about), reset), partial, ol, sAddForm(sec));
}

// Pointer from the type and subtype lists to the map.
function sSubtypeMap() {
  const t = S.data.type_subtypes;
  if (!t) return null;
  const todo = t.rows.filter((r) => r.confirm && !r.retired).length;
  const p = el("p", "dim small", "Map: ");
  const a = sBtn("Subtypes per type", "", () => { S.list = "type_subtypes"; store("sizeup.cad.settings.list", S.list); sRender(true); }, "mini ro-ok");
  p.append(a, document.createTextNode(todo ? ` ${todo} to confirm` : ""));
  return p;
}

// The type -> subtype map (type_subtypes): a row per type, its subtypes ticked. Strict: a type offers only
// what is ticked; none ticked = no subtype. Department-given or proposed (to confirm until confirmed here).
function sTypeMap() {
  const d = S.data.type_subtypes;
  const subs = (L().event_subtypes || []);
  const reset = d.edited ? sBtn("Reset to default", "Data file map", () => {
    sAsk("Reset subtypes per type?", () => sPost("type_subtypes", { action: "reset_section" }, "Subtypes per type reset"), "Reset");
  }, "danger-quiet") : null;
  const rows = d.rows.filter((r) => !r.retired);
  const todo = rows.filter((r) => r.confirm).length;
  const ol = el("ol", "s-rows s-tsrows");
  for (const r of rows) {
    const li = el("li", "s-row s-tsrow");
    const open = S.tsOpen.has(r.type);
    const label = el("b", "s-rule", r.name);
    label.title = r.type;
    const src = el("span", `chip s-flag ${r.source === "given" ? "available" : ""}`, r.source === "given" ? "department" : "proposed");
    src.title = r.source === "given" ? "From the department's code sheet" : "Not on the department's code sheet: a proposal";
    const allowed = el("span", "s-tsnow small" + (r.subtypes.length ? "" : " dim"),
      r.subtypes.length ? r.subtypes.join(", ") : r.listed ? "None" : "Not mapped");
    li.append(label, el("code", "s-code", r.type), src, sState(r.state));
    if (r.confirm) {
      const c = el("span", "chip s-confirm", "to confirm");
      c.title = r.confirm;
      li.append(c);
      li.append(sBtn("Confirm", r.confirm,
        () => sPost("type_subtypes", { action: "set", type: r.type, subtypes: r.subtypes }, `${r.type} confirmed`), "mini"));
    }
    li.append(allowed, el("span", "spacer"));
    const tog = sBtn(open ? "Done" : "Edit", "", () => {
      if (open) S.tsOpen.delete(r.type); else S.tsOpen.add(r.type);
      sRender(true);
    });
    tog.setAttribute("aria-expanded", String(open));
    li.append(tog);
    if (r.state === "edited") li.append(sBtn("Reset", "Default", () => sPost("type_subtypes", { action: "reset", type: r.type }, `${r.type} reset`)));
    if (open) {
      const grid = el("div", "s-tsgrid");
      grid.setAttribute("role", "group");
      grid.setAttribute("aria-label", `Subtypes for ${r.name}`);
      for (const st of subs) {
        const on = r.subtypes.includes(st.code);
        if (st.retired && !on) continue;           // retired: only to untick
        const lab = el("label", "inline check small" + (on ? " on" : ""));
        const cb = el("input");
        cb.type = "checkbox";
        cb.checked = on;
        cb.dataset.code = st.code;
        cb.addEventListener("change", () => {
          const pick = [...grid.querySelectorAll("input:checked")].map((x) => x.dataset.code);
          sPost("type_subtypes", { action: "set", type: r.type, subtypes: pick }, `${r.type}: ${pick.length ? pick.join(", ") : "none"}`);
        });
        lab.title = st.code + (st.retired ? " (retired)" : "");
        lab.append(cb, el("code", "", st.code), el("span", "dim", st.name + (st.retired ? " (retired)" : "")));
        grid.append(lab);
      }
      li.append(grid);
    }
    ol.append(li);
  }
  const gaps = (d.gaps || []).length ? el("p", "s-note notice", "Check: " + d.gaps.join("; ")) : null;
  return sCard(sCardHead("Subtypes per type", `${rows.length} types` + (todo ? `, ${todo} to confirm` : ""),
    sHelp("A type offers only its ticked subtypes (no ALS / Bomb); no subtype is always allowed. None ticked: no subtype. "
      + "Department: from its code sheet (ALARM to MVA). Proposed: worked out from the subtype list, or a guess, to confirm "
      + "with the department (Confirm keeps it). Taking a subtype off a type leaves old events as they are."), reset), gaps, ol);
}

function sItemRow(sec, x, i, n) {
  const deleted = x.state === "deleted";
  const li = el("li", "s-row" + (x.retired ? " retired" : "") + (deleted ? " deleted" : ""));
  li.dataset.row = x.code;
  const ord = el("span", "s-ord");
  const up = sBtn("↑", "Move up", () => sPost(sec, { action: "move", code: x.code, dir: -1 }, `${x.code} up`), "mini");
  const down = sBtn("↓", "Move down", () => sPost(sec, { action: "move", code: x.code, dir: 1 }, `${x.code} down`), "mini");
  up.disabled = deleted || i <= 0;
  down.disabled = deleted || i >= n - 1;
  ord.append(up, down);
  const edit = (item, what) => sPost(sec, { action: "edit", code: x.code, item }, `${x.code} ${what}`);
  const name = sInput(x.name, 60, "Name", (v) => { if (v && v !== x.name) edit({ name: v }, "renamed"); }, "s-name");
  name.disabled = deleted || !!x.system;          // the hub's own (Expired): read-only, never on a pick list
  li.append(ord, el("code", "s-code", x.code), name);
  if (sec === "event_types") {
    li.append(sColor(x.color, (v) => edit({ color: v }, "color changed"), "Color"));
    const p = el("span", "qt s-sample", x.name);
    p.style.setProperty("--c", x.color);
    li.append(p);
  }
  if (sec === "unit_statuses") {
    li.append(sColor(x.bg, (v) => edit({ bg: v }, "color changed"), "Color"),
      sColor(x.fg || "#FFFFFF", (v) => edit({ fg: v }, "text color changed"), "Text color"),
      sSelect(S_ROLES, x.role || "", (v) => edit({ role: v }, "meaning changed"), "What it does"));
    const c = el("span", "chip st s-sample", x.name);
    c.style.setProperty("--c", x.bg || "#9AA3AD");
    li.append(c);
    const flag = x.role === "oos" ? "unavailable" : x.role === "free" ? "available" : "committed";
    li.append(el("span", `chip s-flag ${flag}`, flag));
  }
  if (sec === "location_qualifiers") {
    const short = sInput(x.short || "", 12, "Short", (v) => { if (v && v !== x.short) edit({ short: v }, "short form changed"); }, "s-qshort");
    short.title = "As the text shows it: IFO 12 Sample St";
    const spoken = sInput(x.spoken || "", 40, "Spoken", (v) => { if (v && v !== x.spoken) edit({ spoken: v }, "spoken form changed"); }, "s-qspoken");
    spoken.title = "As the dispatch voice says it, and pages are heard: in front of";
    short.disabled = spoken.disabled = deleted;
    li.append(short, spoken);
  }
  if (x.help) li.append(Help.button(x.help, `${x.name}: definition`));     // the FAA wording and the like (#105)
  li.append(sState(x.state, x.retired));
  if (x.confirm && !deleted) {
    li.append(sConfirm(x.confirm));
    if (!x.retired) li.append(sBtn("Confirm", x.confirm, () => edit({}, "confirmed"), "mini"));
  }
  const usedText = sec === "unit_statuses" && x.now ? `${x.now} now, ` : "";
  li.append(el("span", "dim small s-used", usedText + (x.used ? `used ${x.used}×` : "unused")), el("span", "spacer"));
  if (deleted) {
    li.append(sBtn("Restore", "Default", () => sPost(sec, { action: "reset", code: x.code }, `${x.code} restored`)));
    return li;
  }
  if (x.system) {
    const sys = el("span", "dim small", "set by the hub");
    sys.title = "A non-emergency event closes with this when its Until passes. Not on the pick lists; can't be edited, retired or deleted.";
    li.append(sys);
    if (x.state === "edited") li.append(sBtn("Reset", "Default", () => sPost(sec, { action: "reset", code: x.code }, `${x.code} reset`)));
    return li;
  }
  li.append(x.retired
    ? sBtn("Restore", "Back on the pick lists", () => sPost(sec, { action: "restore", code: x.code }, `${x.code} restored`))
    : sBtn("Retire", "Off the pick lists, kept on old events", () => sPost(sec, { action: "retire", code: x.code }, `${x.code} retired`)));
  if (!x.used) {
    li.append(sBtn("Delete", "Never used", () => {
      sAsk(`Delete ${x.name} (${x.code})?`, () => sPost(sec, { action: "delete", code: x.code }, `${x.code} deleted`), "Delete");
    }, "danger-quiet"));
  }
  if (x.state === "edited") li.append(sBtn("Reset", "Default", () => sPost(sec, { action: "reset", code: x.code }, `${x.code} reset`)));
  return li;
}

function sAddForm(sec) {
  const f = el("form", "row s-add");
  f.autocomplete = "off";
  const code = sInput("", sec === "event_types" || sec === "event_subtypes" ? 24 : 40, "Code", null, "s-code-in");
  code.addEventListener("input", () => { code.value = code.value.toUpperCase(); });
  const name = sInput("", 60, "Name", null, "s-name");
  const extra = {};
  f.append(code, name);
  if (sec === "event_types") { extra.color = sColor("#888888", null, "Color"); f.append(extra.color); }
  if (sec === "unit_statuses") {
    extra.bg = sColor("#888888", null, "Color");
    extra.fg = sColor("#ffffff", null, "Text color");
    extra.role = sSelect(S_ROLES, "", null, "What it does");
    f.append(extra.bg, extra.fg, extra.role);
  }
  if (sec === "location_qualifiers") {
    extra.short = sInput("", 12, "Short (IFO)", null, "s-qshort");
    extra.spoken = sInput("", 40, "Spoken (in front of)", null, "s-qspoken");
    f.append(extra.short, extra.spoken);
  }
  const add = el("button", "primary", "Add");
  add.type = "submit";
  f.append(add);
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const item = { name: name.value.trim() };
    for (const [k, inp] of Object.entries(extra)) {
      item[k] = k === "role" || k === "short" ? inp.value.trim() : k === "spoken" ? inp.value.trim().toLowerCase() : inp.value.toUpperCase();
    }
    await sPost(sec, { action: "add", code: code.value.trim(), item }, `${code.value.trim()} added`);
  });
  return f;
}

// The next-step buttons on a unit that is on an incident (status_flow).
function sFlow() {
  const d = S.data.status_flow;
  const statuses = (L().unit_statuses || []).filter(live);
  const label = (k) => (k === "*" ? "Any other status" : nameOf("unit_statuses", k));
  const ol = el("ol", "s-rows flow");
  for (const r of d.rows) {
    const li = el("li", "s-row" + (r.state === "deleted" ? " deleted" : ""));
    li.append(el("b", "s-rule", label(r.key)), el("span", "dim", "→"));
    if (r.state === "deleted") {
      li.append(el("span", "dim small", (r.default || []).map((c) => nameOf("unit_statuses", c)).join(", ")), sState("deleted"),
        el("span", "spacer"), sBtn("Restore", "", () => sPost("status_flow", { action: "reset", key: r.key }, `${label(r.key)} reset`)));
      ol.append(li);
      continue;
    }
    const checks = el("span", "s-checks");
    for (const st of statuses) {
      if (st.code === r.key) continue;
      const lab = el("label", "inline check small");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = r.next.includes(st.code);
      cb.dataset.code = st.code;
      cb.addEventListener("change", () => {
        const next = [...checks.querySelectorAll("input:checked")].map((x) => x.dataset.code);
        sPost("status_flow", { action: "set", key: r.key, next }, `${label(r.key)} saved`);
      });
      lab.append(cb, el("span", "", st.name));
      checks.append(lab);
    }
    li.append(checks, sState(r.state), el("span", "spacer"));
    if (r.key !== "*") li.append(sBtn("Remove", "Use Any other status", () => sPost("status_flow", { action: "remove", key: r.key }, `${label(r.key)} removed`), "danger-quiet"));
    if (r.state === "edited") li.append(sBtn("Reset", "", () => sPost("status_flow", { action: "reset", key: r.key }, `${label(r.key)} reset`)));
    ol.append(li);
  }
  const have = new Set(d.rows.map((r) => r.key));
  const free = statuses.filter((x) => !have.has(x.code));
  const f = el("form", "row s-add");
  if (free.length) {
    const pick = sSelect(free.map((x) => [x.code, x.name]), free[0].code, null, "Status");
    const add = el("button", "primary", "Add");
    add.type = "submit";
    f.append(el("span", "dim small", "Steps for"), pick, add);
    f.addEventListener("submit", (e) => {
      e.preventDefault();
      const any = (d.rows.find((r) => r.key === "*") || { next: [] }).next.filter((c) => c !== pick.value);
      sPost("status_flow", { action: "set", key: pick.value, next: any.length ? any : [free[0].code] }, `${label(pick.value)} added`);
    });
  }
  const reset = d.edited ? sBtn("Reset to default", "", () => {
    sAsk("Reset next steps?", () => sPost("status_flow", { action: "reset_section" }, "Next steps reset"), "Reset");
  }, "danger-quiet") : null;
  return sCard(sCardHead("Next steps", "", sHelp("The status buttons on an assigned unit. The board on the right always "
      + "offers every status. Available clears the unit off the event; Out of service asks why."), reset), ol, f);
}

function ruleLabel(k) {
  if (k.startsWith("*/")) return `Any type / ${nameOf("event_subtypes", k.slice(2))}`;
  const [t, s] = k.split("/");
  return typeLabel(t, s);
}

function sClassRules() {
  const d = S.data.event_class;
  const reset = d.edited ? sBtn("Reset to default", "", () => {
    sAsk("Reset emergency rules?", () => sPost("event_class", { action: "reset_section" }, "Emergency rules reset"), "Reset");
  }, "danger-quiet") : null;
  const ol = el("ol", "s-rows");
  for (const r of d.rules) {
    const li = el("li", "s-row" + (r.state === "deleted" ? " deleted" : ""));
    li.append(el("b", "s-rule", ruleLabel(r.key)), el("code", "s-code", r.key));
    if (r.state === "deleted") {
      li.append(el("span", "dim small", `was ${r.default}`), sState("deleted"), el("span", "spacer"),
        sBtn("Restore", "", () => sPost("event_class", { action: "reset", key: r.key }, `${r.key} reset`)));
    } else {
      li.append(sSelect([["emergency", "Emergency"], ["non-emergency", "Non-emergency"]], r.value,
        (v) => sPost("event_class", { action: "set", key: r.key, value: v }, `${r.key}: ${v}`), "Class"),
      sState(r.state), el("span", "spacer"),
      sBtn("Remove", "Next rule applies", () => sPost("event_class", { action: "remove", key: r.key }, `${r.key} removed`), "danger-quiet"));
      if (r.state === "edited") li.append(sBtn("Reset", "", () => sPost("event_class", { action: "reset", key: r.key }, `${r.key} reset`)));
    }
    ol.append(li);
  }
  const f = el("form", "row s-add");
  const types = [["*", "Any type"], ...(L().event_types || []).filter(live).map((t) => [t.code, t.name])];
  const t = sSelect(types, "", null, "Type");
  const s = sSelect([["", "No subtype"], ...(L().event_subtypes || []).filter(live).map((x) => [x.code, x.name])], "", null, "Subtype");
  const c = sSelect([["non-emergency", "Non-emergency"], ["emergency", "Emergency"]], "non-emergency", null, "Class");
  const add = el("button", "primary", "Add rule");
  add.type = "submit";
  f.append(t, s, c, add);
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    if (t.value === "*" && !s.value) return sMsg("Any type needs a subtype");
    const key = s.value ? `${t.value}/${s.value}` : t.value;
    sPost("event_class", { action: "set", key, value: c.value }, `${ruleLabel(key)}: ${c.value}`);
  });
  return sCard(sCardHead("Emergency rules", `${d.rules.filter((r) => r.state !== "deleted").length} rules`,
    sHelp("Non-emergency events (notifications, closures, hydrants) are logged and tracked, never dispatched. "
      + "The most specific rule wins: type and subtype, then any type with the subtype, then the type. Anything "
      + "not listed is an emergency. The operator can still change it on an event."), reset), ol, f);
}

// Feed types (sizeup/cad/feedtypes.py): a feed's own code -> our type and subtype, over the automatic mapping; the
// codes seen lately that nothing maps fully, each with Map... (the form filled in).
function sFeedTypes() {
  const d = S.data.feed_types || { rows: [], seen: [], feeds: [] };
  const feedName = (src) => (d.feeds.find((f) => f.source === src) || {}).label || src;
  const ol = el("ol", "s-rows");
  for (const r of d.rows) {
    const li = el("li", "s-row");
    const off = r.off ? el("span", "chip s-confirm", "not applied") : null;
    if (off) off.title = r.off;
    li.append(el("code", "s-code", r.code), el("span", "dim small", feedName(r.source)), el("b", "s-rule", `→ ${r.label}`),
      ...(off ? [off] : []), el("span", "spacer"),
      sBtn("Remove", "Back to automatic", () => sPost("feed_types", { action: "reset", key: r.key }, `${r.code} removed`), "danger-quiet"));
    ol.append(li);
  }
  if (!d.rows.length) ol.append(el("li", "s-row dim small", "None: automatic"));
  const draft = S.ftDraft || {};
  const f = el("form", "row s-add");
  const feeds = d.feeds.length ? d.feeds.map((x) => [x.source, x.label]) : [["", "No feed yet"]];
  const fd = sSelect(feeds, draft.source || feeds[0][0], null, "Feed");
  const code = sInput(draft.code || "", 80, "MAIN or MAIN,SUB");
  const types = (L().event_types || []).filter(live).map((t) => [t.code, t.name]);
  const t = sSelect(types, draft.type || (types[0] || [""])[0], null, "Type");
  const sub = sSelect([], "", null, "Subtype");
  const fillSubs = () => {
    const allowed = ((L().type_subtypes || {})[t.value]) || [];
    fillSelect(sub, [["", "No subtype"], ...allowed.map((c) => [c, nameOf("event_subtypes", c)])]);
  };
  fillSubs();
  t.addEventListener("change", fillSubs);
  const add = el("button", "primary", "Map");
  add.type = "submit";
  f.append(fd, code, t, sub, add);
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!code.value.trim()) return sMsg("County code: MAIN or MAIN,SUB");
    S.ftDraft = null;
    sPost("feed_types", { action: "set", feed: fd.value, code: code.value.trim(), type: t.value, subtype: sub.value },
      `${code.value.trim()} → ${typeLabel(t.value, sub.value)}`);
  });
  if (draft.code) setTimeout(() => code.focus(), 0);
  const rows = sCard(sCardHead("Feed types", `${d.rows.length} mapped`,
    sHelp("A feed's own type code mapped to our type and subtype; a row wins over the automatic mapping. MAIN alone "
      + "maps every MAIN,SUB: the SUB still picks the one subtype of that type it names.")), ol, f);
  const seen = el("ol", "s-rows");
  for (const r of d.seen) {
    const li = el("li", "s-row");
    const state = el("span", r.state === "partly" ? "chip s-confirm" : "chip s-state deleted", r.state === "partly" ? "no subtype" : "no type");
    const n = el("span", "dim small", `${r.n}×`);
    n.title = `Last seen ${new Date(r.last_at * 1000).toLocaleString()}`;
    li.append(el("code", "s-code", r.code), el("span", "dim small", feedName(r.source)), state,
      el("span", "small", r.shown || "—"), n, el("span", "spacer"),
      sBtn("Map…", "Fill in the form above", () => { S.ftDraft = { source: r.source, code: r.code, type: r.type }; sRender(true); }, "mini"));
    seen.append(li);
  }
  const seenCard = d.seen.length ? sCard(sCardHead("Seen, not mapped", `${d.seen.length}`,
    sHelp("Codes the feeds sent lately that give no type of ours, or our type with no subtype for the code's SUB. "
      + "Map one here, or add its SUB as a subtype of the type (Subtypes per type).")), seen) : null;
  const box = el("div");
  box.append(rows, ...(seenCard ? [seenCard] : []));
  return box;
}

function sTowns() {
  const d = S.data.geo_town_order;
  const towns = d.towns.slice();
  const save = (list, what) => sPost("geo_town_order", { action: "set", towns: list }, what);
  const ol = el("ol", "s-rows towns");
  towns.forEach((t, i) => {
    const li = el("li", "s-row");
    const up = sBtn("↑", "Move up", () => { const l = towns.slice(); l.splice(i - 1, 0, l.splice(i, 1)[0]); save(l, `${t} up`); }, "mini");
    const down = sBtn("↓", "Move down", () => { const l = towns.slice(); l.splice(i + 1, 0, l.splice(i, 1)[0]); save(l, `${t} down`); }, "mini");
    up.disabled = i === 0;
    down.disabled = i === towns.length - 1;
    const ord = el("span", "s-ord");
    ord.append(up, down);
    li.append(ord, el("b", "", `${i + 1}. ${t}`), el("span", "spacer"),
      sBtn("Remove", "", () => save(towns.filter((x) => x !== t), `${t} removed`), "danger-quiet"));
    ol.append(li);
  });
  const f = el("form", "row s-add");
  const inp = sInput("", 60, "Town", null, "s-name");
  const add = el("button", "primary", "Add town");
  add.type = "submit";
  f.append(inp, add);
  f.addEventListener("submit", (e) => { e.preventDefault(); if (inp.value.trim()) save([...towns, inp.value.trim()], `${inp.value.trim()} added`); });
  const reset = d.edited ? sBtn("Reset to default", "", () => sPost("geo_town_order", { action: "reset_section" }, "Town order reset"), "danger-quiet") : null;
  return sCard(sCardHead("Town order", "", sHelp("Location suggestions list these towns first, in this order. A town typed in the box wins."), reset),
    ol, f);
}

// The department's own names for places (#67): "the Waterfront", "Kohl's plaza". Each points at a place
// picked from the place search; a note's "@" and the location box offer them first.
function sPlaceNames() {
  const d = S.data.place_names || { items: [] };
  const ol = el("ol", "s-rows");
  for (const x of d.items) {
    const li = el("li", "s-row");
    const where = [x.label, x.muni && !x.label.includes(x.muni) ? x.muni : ""].filter(Boolean).join(", ");
    li.append(el("b", "s-name", x.name), el("span", "dim small", where + (x.aliases.length ? ` · also ${x.aliases.join(", ")}` : "")),
      el("span", "spacer"), sBtn("Remove", "", () => sAsk(`Remove the name "${x.name}"?`,
        () => sPost("places", { action: "delete", key: x.key }, `${x.name} removed`), "Remove"), "danger-quiet"));
    ol.append(li);
  }
  if (!d.items.length) ol.append(el("li", "s-row dim", "None yet"));
  const f = el("form", "row s-add");
  const name = sInput("", 60, "Name (the Waterfront)", null, "s-name");
  const aka = sInput("", 200, "Also called (commas)", null, "s-name");
  const box = el("div", "note-box");
  const where = sInput("", 120, "Where: a place or an address");
  where.setAttribute("role", "combobox");
  where.setAttribute("aria-autocomplete", "list");
  const list = el("ol", "sugg note-sugg");
  list.setAttribute("role", "listbox");
  list.hidden = true;
  box.append(where, list);
  let picked = null, timer = null, seq = 0;
  where.addEventListener("input", () => {
    picked = null;
    clearTimeout(timer);
    const q = where.value.trim(), my = ++seq;
    if (q.length < 2) return show(list, false);
    timer = setTimeout(async () => {
      let hits = [];
      try { hits = (await api("GET", `/api/cad/places?q=${encodeURIComponent(q)}&limit=6`)).places || []; } catch (e) { hits = []; }
      if (my !== seq) return;
      list.replaceChildren(...hits.map((p) => {
        const li = el("li", "");
        li.setAttribute("role", "option");
        li.append(...suggestionHead(p));                 // cad.js: a name at the address typed "120 Example Ave @ ..."
        if (p.sub) li.append(el("span", "town", p.sub));
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          picked = { label: p.label, lat: p.lat, lon: p.lon, sub: p.sub || "" };
          where.value = p.sub && !p.label.includes(p.sub) ? `${p.label}, ${p.sub}` : p.label;
          show(list, false);
        });
        return li;
      }));
      if (!hits.length) list.replaceChildren(el("li", "hint dim small", "No match"));
      show(list, true);
    }, 200);
  });
  where.addEventListener("blur", () => setTimeout(() => show(list, false), 150));
  const add = el("button", "primary", "Add name");
  add.type = "submit";
  f.append(name, box, aka, add);
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!name.value.trim()) return sMsg("Name: required");
    if (!picked) return sMsg("Where: pick the place from the suggestions");
    sPost("places", { action: "set", name: name.value.trim(), aliases: aka.value, place: picked }, `${name.value.trim()} added`);
  });
  return sCard(sCardHead("Place names", d.items.length ? `${d.items.length}` : "",
    sHelp("Your own names for places: \"the Waterfront\", \"Kohl's plaza\". An @ in a note and the location box "
      + "offer them first, at the place picked here.")), ol, f);
}

// The department's own address points (sizeup/geo/localaddr.py): an address the map data lacks ("189-1/2 Example St",
// a station with no point of its own), typed, then pinned on the map (a click, or drag the pin). Found like the map's
// own points: pages, the location box, a map click, the common places import. The map is made once (S.addrMap) and
// moved into each redraw of the card; what is being added or edited is S.addrForm {key?, lat?, lon?}.
function sAddressPoints() {
  const d = S.data.local_addresses || { items: [] };
  const F = S.addrForm || (S.addrForm = {});
  const ol = el("ol", "s-rows");
  for (const x of d.items) {
    const li = el("li", "s-row" + (F.key === x.key ? " on" : ""));
    li.dataset.row = x.key;
    li.append(el("b", "s-name", `${x.number} ${x.street}`), el("span", "dim small", [x.town, x.note].filter(Boolean).join(" · ")),
      el("span", "spacer"),
      sBtn("Edit", "Move the pin or change the address", () => sAddrEdit({ key: x.key, address: `${x.number} ${x.street}`,
        town: x.town, note: x.note, lat: x.lat, lon: x.lon })),
      sBtn("Remove", "", () => sAsk(`Remove ${x.number} ${x.street}?`, () => sPost("local_addresses",
        { action: "delete", key: x.key }, `${x.number} ${x.street} removed`).then((r) => { if (r && F.key === x.key) sAddrEdit(null); }),
      "Remove"), "danger-quiet"));
    ol.append(li);
  }
  if (!d.items.length) ol.append(el("li", "s-row dim", "None yet"));
  const f = el("form", "row s-add");
  f.id = "s-addr-form";
  const field = (k, max, ph, cls, title) => {
    const i = sInput(F[k] || "", max, ph, null, cls);
    i.dataset.key = `la-${k}`;
    if (title) i.title = title;
    i.addEventListener("input", () => { F[k] = i.value; });
    return i;
  };
  const addr = field("address", 120, "189-1/2 Example St", "s-name", "House number and street, as the map data names it");
  const town = field("town", 60, "Town", "", "Blank: the street's town at the pin");
  const note = field("note", 120, "Note", "s-name", "");
  addr.addEventListener("change", () => sAddrFind(addr.value.trim(), town.value.trim()));
  const at = el("span", "dim small", F.lat != null ? `${(+F.lat).toFixed(5)}, ${(+F.lon).toFixed(5)}` : "No pin");
  at.id = "s-addr-at";
  at.title = "Click the map or drag the pin";
  const save = el("button", "primary", F.key ? "Save" : "Add");
  save.type = "submit";
  f.append(addr, town, note, at, save);
  if (F.key || F.address || F.lat != null) f.append(sBtn("Cancel", "", () => sAddrEdit(null)));
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!addr.value.trim()) return sMsg("Address: required");
    if (F.lat == null) return sMsg("Pin: click the map where it is");
    const r = await sPost("local_addresses", { action: "set", address: addr.value.trim(), town: town.value.trim(),
      note: note.value.trim(), lat: F.lat, lon: F.lon, ...(F.key ? { key: F.key } : {}) }, `${addr.value.trim()} pinned`);
    if (r) sAddrEdit(null);
  });
  const box = el("div", "s-addr-map");
  box.id = "s-addr-mapbox";
  return sCard(sCardHead("Address points", d.items.length ? `${d.items.length}` : "",
    sHelp("Addresses the map data lacks: a rear building (189-1/2), a station with no point. Type the address, click "
      + "the map where it is (drag the pin to adjust). Pages, the location box, a map click and the common places "
      + "import then find it like any address point, ahead of a guess between two numbers.")), ol, f, box);
}
// What is being added or edited (null: a clean form), then the card redrawn with it.
function sAddrEdit(form) {
  S.addrForm = form ? { ...form } : {};
  const card = $("#s-addr-form");
  if (card) for (const i of card.querySelectorAll("input")) i._base = undefined;      // (not text typed: sKeep)
  sRender(true);
  if (form) {
    const f = $("#s-addr-form");
    if (f) { f.scrollIntoView({ block: "nearest" }); f.querySelector("input").focus({ preventScroll: true }); }
    if (form.lat != null) sAddrPin(form.lat, form.lon, true);
    else if (form.address) sAddrFind(form.address, form.town || "");
  }
}
// The address typed: the map goes to where the map data puts it (its street, a guess between two numbers); no pin yet:
// the pin there, to be moved.
async function sAddrFind(address, town) {
  if (!address) return;
  let m = null;
  try { m = ((await api("GET", `/api/cad/geocode?q=${encodeURIComponent(town ? `${address}, ${town}` : address)}`)).matches || [])[0]; }
  catch (e) { m = null; }
  if (!m || m.lat == null) return;
  const F = S.addrForm || (S.addrForm = {});
  if (F.lat == null) sAddrPin(m.lat, m.lon, true);
  else if (S.addrMap) S.addrMap.map.easeTo({ center: [m.lon, m.lat], zoom: Math.max(17, S.addrMap.map.getZoom()) });
}
function sAddrPin(lat, lon, ease) {
  const F = S.addrForm || (S.addrForm = {});
  F.lat = Math.round(lat * 1e6) / 1e6;
  F.lon = Math.round(lon * 1e6) / 1e6;
  const at = $("#s-addr-at");
  if (at) setText(at, `${F.lat.toFixed(5)}, ${F.lon.toFixed(5)}`);
  const M = S.addrMap;
  if (!M) return;
  if (!M.pin) {
    const dot = el("div", "nc-pin");
    dot.title = "Drag to move";
    M.pin = new maplibregl.Marker({ element: dot, anchor: "bottom", draggable: true }).setLngLat([F.lon, F.lat]).addTo(M.map);
    M.pin.on("dragend", () => { const ll = M.pin.getLngLat(); sAddrPin(ll.lat, ll.lng, false); });
  } else M.pin.setLngLat([F.lon, F.lat]);
  if (ease) M.map.easeTo({ center: [F.lon, F.lat], zoom: Math.max(17, M.map.getZoom()) });
}
// The map (made once, like the pre-plan search's: preplan.js) put into the card just drawn; the saved points as dots.
function sAddrMapShown() {
  const box = $("#s-addr-mapbox");
  if (!box || !window.maplibregl || typeof buildMapStyle !== "function" || !cad.scene || !cad.scene.home) {
    if (box) box.hidden = true;
    return;
  }
  let M = S.addrMap;
  if (!M) {
    const host = el("div", "sv-map");
    M = S.addrMap = { host, pin: null, dots: [] };
    box.append(host);
    M.map = new maplibregl.Map({ container: host, style: buildMapStyle(cad.scene), center: cad.scene.home.center,
                                 zoom: cad.scene.home.zoom, attributionControl: { compact: true }, maxPitch: 0, dragRotate: false });
    M.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    M.map.on("click", (e) => sAddrPin(e.lngLat.lat, e.lngLat.lng, false));
  } else {
    box.append(M.host);
    M.map.resize();
  }
  for (const d of M.dots) d.remove();
  M.dots = ((S.data.local_addresses || {}).items || []).filter((x) => x.key !== (S.addrForm || {}).key).map((x) => {
    const dot = el("div", "nc-probe");
    dot.title = `${x.number} ${x.street}`;
    return new maplibregl.Marker({ element: dot }).setLngLat([x.lon, x.lat]).addTo(M.map);
  });
  const F = S.addrForm || {};
  if (F.lat == null && M.pin) { M.pin.remove(); M.pin = null; }
  else if (F.lat != null) sAddrPin(F.lat, F.lon, false);
}

// The county's common place names (sizeup/cad/commonplaces.py): its .xlsx / .csv previewed (counts, the addresses not
// found, the columns), then applied over the last import. Names made above are never touched. Admins (the server).
function sCommonPlaces() {
  const im = (S.data.place_names || {}).imported || { names: 0 };
  const file = el("input");
  file.type = "file";
  file.accept = ".xlsx,.csv";
  file.hidden = true;
  file.addEventListener("change", () => sCommonFile(file));
  const admin = !cad.who || cad.who.may === "admin";          // the server refuses anyone else
  const pick = admin ? sBtn(im.names ? "Re-import" : "Import", "Address, Common Place, Mun (.xlsx or .csv)",
    () => file.click(), im.names ? "" : "primary") : null;
  const remove = admin && im.names ? sBtn("Remove", "", () => sAsk(`Remove the ${im.names} imported names?`,
    () => sPost("common_places", { action: "remove" }, "Imported names removed"), "Remove"), "danger-quiet") : null;
  const info = im.names ? `${im.located}/${im.names} located · ${im.date}` : "";
  const kids = [];
  if (im.unresolved && im.unresolved.length) {
    const det = el("details", "s-more");
    det.append(el("summary", "", `${im.names - im.located} not found`));
    const ol = el("ol", "s-rows");
    for (const u of im.unresolved) {
      const li = el("li", "s-row");
      li.append(el("b", "s-name", u.name), el("span", "dim small", [u.address, u.town].filter(Boolean).join(", ")),
        el("span", "spacer"), sAddrPinIt(u));
      ol.append(li);
    }
    det.append(ol);
    kids.push(det);
  }
  const pv = el("div", "");
  pv.id = "s-common";
  sShowCommon(pv);
  return sCard(sCardHead("Common places", info, sHelp("The county CAD's common place names. Re-import replaces the last "
    + "import; your own names stay. Kept in the CAD database only."), remove, pick), file, ...kids, pv);
}

// "Pin": a common place's address not found, into Address points' form (then a click on the map where it is).
function sAddrPinIt(u) {
  return sBtn("Pin", "Add as an address point", () => sAddrEdit({ address: u.address || "", town: u.town || "", note: u.name || "" }));
}

async function sCommonFile(input) {
  const f = input.files && input.files[0];
  input.value = "";
  if (!f) return;
  const data = await new Promise((ok) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result || ""));
    r.onerror = () => ok("");
    r.readAsDataURL(f);
  });
  if (!data) return sMsg(`${f.name}: can't read it`);
  S.common = { name: f.name, file: data, columns: null, r: null };
  await sCommonPreview();
}

async function sCommonPreview() {
  const c = S.common;
  if (!c) return;
  const my = c.seq = (c.seq || 0) + 1;                       // only the newest preview is shown
  let r;
  try {
    r = await api("POST", "/api/cad/settings/common_places", { action: "preview", file: c.file, filename: c.name,
      ...(c.columns ? { columns: c.columns } : {}), operator: operator() });
  } catch (e) {
    r = { error: e.message };
  }
  if (S.common !== c || c.seq !== my) return;
  c.r = r;
  if (r.columns) c.columns = r.columns;
  const box = $("#s-common");
  if (box) sShowCommon(box);
}

function sShowCommon(box) {
  const c = S.common;
  if (!c) { box.replaceChildren(); return; }
  const cancel = sBtn("Cancel", "", () => { S.common = null; sShowCommon(box); });
  const head = el("div", "row");
  head.append(el("b", "", c.name), el("span", "spacer"), cancel);
  if (!c.r) { box.replaceChildren(head, el("p", "dim small", "Reading…")); return; }
  if (c.r.error) { box.replaceChildren(head, el("div", "small warn", c.r.error)); return; }
  const n = c.r.counts;
  const cols = el("div", "row");
  const opts = [["-1", "—"], ...c.r.header.map((h, i) => [String(i), h || `Column ${i + 1}`])];
  for (const [k, label] of [["address", "Address"], ["name", "Name"], ["town", "Town"]]) {
    const s = sSelect(opts, String(c.columns[k]), (v) => { c.columns = { ...c.columns, [k]: Number(v) }; sCommonPreview(); }, label);
    const lab = el("label", "small");
    lab.append(el("span", "dim", `${label} `), s);
    cols.append(lab);
  }
  const counts = el("p", "small", `${n.rows} rows · ${n.names} names · ${n.new} new · ${n.present} already · `
    + `${n.removed} removed · ${n.unresolved} not found`
    + (n.unresolved_addresses !== n.unresolved ? ` (${n.unresolved_addresses} addresses)` : ""));
  const apply = sBtn(`Apply ${n.names} names`, "", async () => {
    const r = await sPost("common_places", { action: "apply", file: c.file, filename: c.name, columns: c.columns },
      `Imported ${n.names} names`);
    if (r) { S.common = null; const b = $("#s-common"); if (b) sShowCommon(b); }
  }, "primary");
  apply.disabled = !n.names;
  head.append(apply);
  const kids = [head, cols, counts];
  if ((c.r.unknown_towns || []).length) {
    kids.push(el("div", "small warn", `${n.unknown_towns} rows, unknown town: ${c.r.unknown_towns.join(", ")}`));
  }
  if (c.r.unresolved.length) {
    const det = el("details", "s-more");
    det.append(el("summary", "", `${n.unresolved} not found`));
    const ol = el("ol", "s-rows");
    for (const u of c.r.unresolved) {
      const li = el("li", "s-row");
      li.append(el("b", "s-name", u.name), el("span", "dim small", [u.address, u.town].filter(Boolean).join(", ")),
        el("span", "spacer"), sAddrPinIt(u));
      ol.append(li);
    }
    det.append(ol);
    kids.push(det);
  }
  box.replaceChildren(...kids);
}

// ---- run cards: the editor is cad.js's; reset buttons here -----------------------------------------

function sRunTools() {
  const box = $("#rc-tools");
  const editing = cad.rcEditing;
  const sig = JSON.stringify([editing, (cad.state && cad.state.run_cards || []).find((c) => c.type === editing)?.is_default]);
  if (box._sig === sig) return;
  box._sig = sig;
  const card = (cad.state && cad.state.run_cards || []).find((c) => c.type === editing);
  const kids = [];
  if (card && !card.is_default) {
    kids.push(sBtn("Reset card", "Default card for this type", async () => {
      const r = await sPost("runcards", { action: "reset", type: editing }, `${editing} reset`);
      if (r) editCard(r);
    }));
  }
  kids.push(sBtn("Reset all", "Data file cards; cards added here are removed", () => {
    sAsk("Reset all run cards? Cards added here are removed.",
      () => sPost("runcards", { action: "reset_section" }, "Run cards reset").then((r) => { if (r) editCard(null); }), "Reset");
  }, "danger-quiet"));
  box.replaceChildren(...kids);
}

// ---- units ----------------------------------------------------------------------------------------

function sUnits(panel) {
  const d = S.data.units;
  const rows = d.rows;
  const kinds = S.data.unit_kinds.map((k) => [k.id, k.label]);
  const boards = S.data.boards.map((b) => [b.id, b.id === "main" ? "Main board" : b.title]);
  const reset = d.edited ? sBtn("Reset all units", "Data file units", () => {
    sAsk("Reset all units? Units added here leave the boards (history kept).", () => sPost("units", { action: "reset_section" }, "Units reset"), "Reset");
  }, "danger-quiet") : null;
  const todo = rows.filter((r) => !r.retired && !r.named).length;
  const out = [...sHead("Units", "Main board: PCFD and Rye Brook fire units. Run cards pick them by kind, All PCFD ticks "
    + "PCFD's, the dispatch says them. Mutual aid departments and requests go only when named. Other agencies (EMS, PD, "
    + "utilities, resources): never on a run card, never dispatched or toned out; added from Assigned. Third party "
    + "(keyholders, utilities, police): added Dispatched, never Responding, Dispatch other theirs only. Codes are "
    + `the department's. A unit with no name shows as its code, marked unconfirmed${todo ? ` (${todo} now)` : ""}.`, reset)];
  S.boardsOpen = S.boardsOpen || new Set();
  for (const b of S.data.boards) {
    const mine = rows.filter((r) => r.board === b.id && !r.retired);
    const ol = el("ol", "s-rows units");
    mine.forEach((u, i) => ol.append(sUnitRow(u, i, mine.length, kinds, boards)));
    const unnamed = mine.filter((r) => !r.named).length;
    if (b.folded) {
      const det = el("details", "s-card s-fold");
      det.open = S.boardsOpen.has(b.id);
      det.addEventListener("toggle", () => { if (det.open) S.boardsOpen.add(b.id); else S.boardsOpen.delete(b.id); });
      const sum = el("summary");
      sum.append(el("h2", "", b.title), el("span", "dim small", ` ${mine.length} units`
        + (unnamed ? `, ${unnamed} to confirm` : "")));
      det.append(sum, ol);
      out.push(det);
    } else {
      out.push(sCard(sCardHead(b.id === "main" ? "Main board" : b.title,
        `${mine.length} units` + (unnamed ? `, ${unnamed} to confirm` : "")), ol));
    }
  }
  const retired = rows.filter((r) => r.retired);
  if (retired.length) {
    const ol = el("ol", "s-rows units");
    retired.forEach((u) => ol.append(sUnitRow(u, 0, 0, kinds, boards)));
    out.push(sCard(sCardHead("Retired", "history kept"), ol));
  }
  const deleted = rows.filter((r) => r.state === "deleted");
  if (deleted.length) {
    const ol = el("ol", "s-rows units");
    for (const u of deleted) {
      const li = el("li", "s-row deleted");
      li.append(el("code", "s-code", u.code), el("span", "", u.spoken || u.code), sState("deleted"), el("span", "spacer"),
        sBtn("Restore", "", () => sPost("units", { action: "reset", code: u.code }, `${u.code} restored`)));
      ol.append(li);
    }
    out.push(sCard(sCardHead("Deleted", ""), ol));
  }
  out.push(sCard(sCardHead("Add unit"), sAddUnit(kinds, boards)));
  panel.replaceChildren(...out);
}

function sUnitRow(u, i, n, kinds, boards) {
  const li = el("li", "s-row unit" + (u.retired ? " retired" : ""));
  li.dataset.row = u.code;          // (sFields: text typed in this row stays this unit's)
  const edit = (unit, what) => sPost("units", { action: "edit", code: u.code, unit }, `${u.code}: ${what}`);
  if (!u.retired) {
    const ord = el("span", "s-ord");
    const up = sBtn("↑", "Move up", () => sPost("units", { action: "move", code: u.code, dir: -1 }, `${u.code} up`), "mini");
    const down = sBtn("↓", "Move down", () => sPost("units", { action: "move", code: u.code, dir: 1 }, `${u.code} down`), "mini");
    up.disabled = i === 0;
    down.disabled = i === n - 1;
    ord.append(up, down);
    li.append(ord);
  }
  li.append(el("code", "s-code unit-code", u.code),
    sInput(u.spoken, 40, "Said as", (v) => edit({ spoken: v }, "said as " + (v || u.code)), "s-said"),
    sInput(u.title, 60, "Title", (v) => edit({ title: v }, "title changed"), "s-title"),
    sSelect(kinds, u.kind, (v) => edit({ kind: v }, `kind ${v}`), "Kind"),
    sInput(u.department, 40, "Department", (v) => edit({ department: v }, "department changed"), "s-dept"));
  const spare = el("label", "inline check small");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = !!u.spare;
  cb.addEventListener("change", () => edit({ spare: cb.checked }, cb.checked ? "spare" : "not spare"));
  spare.append(cb, el("span", "", "Spare"));
  const resp = sSelect(S_RESPONSES, u.response, (v) => edit({ response: v }, "answers as " + v.replace("_", " ")), "Answers as");
  resp.title = "Fire: run cards and dispatch. Mutual aid: named only. Other agency: attach only.";
  li.append(spare, resp);
  const sets = S.data.units.status_sets || [];
  if (u.response === "agency" && sets.length) {      // its class: EMS, Agency, Third party (keyholder ...): its statuses
    const cls = sSelect(sets.map((x) => [x.id, x.title]), u.status_set || "",
      (v) => edit({ status_set: v }, `statuses ${(sets.find((x) => x.id === v) || {}).title || v}`), "Statuses");
    const cur = sets.find((x) => x.id === u.status_set);
    cls.title = cur ? `${cur.statuses.join(" ")}${cur.third_party ? ". Third party: added Dispatched, never Responding; no automation" : ""}` : "";
    li.append(cls);
  }
  const homes = S.data.units.stations || [];
  if (u.response !== "agency" && homes.length) {             // home firehouse: where the maps draw it (cadmap.js)
    const home = sSelect([["", "Home: not set"], ...homes.map((n) => [n, n])], u.station || "",
      (v) => sPost("units", { action: "station", code: u.code, station: v }, `${u.code}: home ${v || "not set"}`), "Home station");
    home.title = "Firehouse: where the maps draw it";
    li.append(home);
  }
  if (!u.retired) li.append(sSelect(boards, u.board, (v) => edit({ board: v }, "board changed"), "Board"));
  const meta = el("span", "s-meta");
  if (u.status) meta.append(statusChip(u.status));
  if (u.incident_number) meta.append(el("span", "warn small", `On ${u.incident_number}`));
  meta.append(sState(u.state, u.retired));
  if (!u.named && !u.retired) meta.append(sConfirm(""));
  if (u.made_fire) {                    // an old edit saved another agency's unit as our engine: Reset puts it back
    const w = el("span", "warn small", "Made a fire unit by an old edit: Reset?");
    w.title = "Saved as Engine / Fire by a Settings edit made before this was fixed. Reset restores the data file's unit.";
    meta.append(w);
  }
  meta.append(el("span", "dim small", u.used ? `${u.used} records` : "no history"));
  li.append(meta, el("span", "spacer"));
  if (u.retired) li.append(sBtn("Restore", "Back on its board", () => sPost("units", { action: "restore", code: u.code }, `${u.code} restored`)));
  else li.append(sBtn("Retire", "Off the boards, history kept", () => {
    sAsk(`Retire ${u.code}? Off the boards, history kept.`, () => sPost("units", { action: "retire", code: u.code }, `${u.code} retired`), "Retire");
  }));
  if (!u.used) li.append(sBtn("Delete", "No history", () => {
    sAsk(`Delete ${u.code}?`, () => sPost("units", { action: "delete", code: u.code }, `${u.code} deleted`), "Delete");
  }, "danger-quiet"));
  if (u.state === "edited") li.append(sBtn("Reset", "Default", () => sPost("units", { action: "reset", code: u.code }, `${u.code} reset`)));
  return li;
}

function sAddUnit(kinds, boards) {
  const f = el("form", "row s-add");
  f.autocomplete = "off";
  const code = sInput("", 24, "Code (E65)", null, "s-code-in");
  code.addEventListener("input", () => { code.value = code.value.toUpperCase(); });
  const said = sInput("", 40, "Said as (Engine 65)", null, "s-said");
  const kind = sSelect(kinds, "engine", null, "Kind");
  const dept = sInput("", 40, "Department (blank: PCFD)", null, "s-dept");
  const resp = sSelect(S_RESPONSES, "fire", null, "Answers as");
  const board = sSelect(boards, "main", null, "Board");
  kind.addEventListener("change", () => {        // EMS / PD / agencies: attach only, on their own board
    const ag = (S.data.unit_kinds.find((k) => k.id === kind.value) || {}).attach_only;
    resp.value = ag ? "agency" : "fire";
    board.value = ag ? "agencies" : "main";
  });
  const add = el("button", "primary", "Add unit");
  add.type = "submit";
  f.append(code, said, kind, dept, resp, board, add);
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    sPost("units", { action: "add", code: code.value.trim(), unit: { spoken: said.value.trim(), kind: kind.value,
      department: dept.value.trim(), response: resp.value, board: board.value } }, `${code.value.trim()} added`);
  });
  return f;
}

// ---- dispatch -------------------------------------------------------------------------------------

function sDispatch(panel) {
  const d = S.data.dispatch;
  const reset = (d.wording.some((w) => w.state === "edited") || d.tones.edited || d.channels.edited || d.voice.edited
    || (d.dept_tones || []).some((t) => t.edited) || (d.lead_in_joint_only || []).some((t) => t.state === "edited")
    || (d.eta && (d.eta.numbers.some((n) => n.edited) || d.eta.order.edited || d.eta.quarters_status.edited)))
    ? sBtn("Reset all", "", () => {
      sAsk("Reset wording, tones, channels and arrival estimates?", () => sPost("dispatch", { action: "reset_section" }, "Dispatch reset"), "Reset");
    }, "danger-quiet") : null;
  const main = el("div", "s-wording");
  const more = el("details", "s-more");
  more.append(el("summary", "", "More templates"));
  const MAIN = ["new", "add_on", "upgrade", "full_response", "remarks", "lead_in", "lead_in_joint"];
  d.wording.forEach((w) => (MAIN.includes(w.key) ? main : more).append(sTemplate(w)));
  for (const j of d.lead_in_joint_only || []) main.append(sJointOnly(j));
  const legend = el("p", "dim small", d.placeholders.map((p) => `{${p}}`).join(" "));
  const pv = el("div", "s-preview");
  pv.id = "s-preview";
  panel.replaceChildren(...sHead("Dispatch", "What the dispatch says and how it sounds. Changes apply to the next dispatch.", reset),
    sCard(sCardHead("Wording", d.department, sHelp("Placeholders fill from the event. Empty parts drop out with their punctuation. "
      + "The more templates are pieces of the add-on and upgrade.")), legend, main, more),
    sCard(sCardHead("Preview", "", sHelp("A sample event, rendered as typed")), pv),
    sVoice(d.voice), sTones(d.tones), ...(d.dept_tones || []).map(sDeptTones), sChannels(d.channels),
    d.eta ? sEta(d.eta) : null,
    sCard(sCardHead("Transmit", "", sHelp(`${d.transmit.note}. Read-only here.`)),
      el("span", "pill tx-off", d.transmit.enabled ? "Transmit on" : "Transmit off"), el("p", "dim small", "No radio interface")),
    d.weather_speak ? sWeatherSpeak(d.weather_speak) : null,
    S.data.reminders ? sReminders(S.data.reminders) : null);
  sPreview();
}

// What flashes when a call or a unit is left too long (sizeup/cad/reminders.py, reminders.js): each rule on or off and its
// minutes, which units the unit rules watch, how long an OK quiets one. For everyone, by the hub's clock.
const REM_TIPS = {
  pending: "An emergency event accepted with no unit on it (or its last unit cleared)",
  dispatched: "A unit in the dispatched status that has not gone responding or on location",
  queued: "A page from 60 Control not yet accepted",
  onscene: "A unit on location with no status change (a welfare check)",
  until: "An open event past its Until",
};
function sReminders(r) {
  const rows = el("div", "s-eta");
  const reset = (item, label, shown) => {
    if (!item.edited) return null;
    const b = sBtn("Reset", `Default: ${shown}`, () => sPost("reminders", { action: "reset", key: item.key }, `${label} reset`));
    b.setAttribute("aria-label", `Reset ${label}: ${typeof item.default === "boolean" ? "on" : "minutes"}`);
    return b;
  };
  const line = (label, ctl, items, tip) => {
    const row = el("div", "row");
    const l = el("span", "s-eta-l", label);
    if (tip) l.title = tip;
    row.append(l, ...ctl, sState(items.some((x) => x.edited) ? "edited" : "default"), el("span", "spacer"),
      ...items.map((x) => reset(x, label, typeof x.default === "boolean" ? (x.default ? "on" : "off") : `${x.default} min`)).filter(Boolean));
    return row;
  };
  const minutes = (item, label) => {
    const i = el("input", "s-num");
    i.type = "number"; i.min = String(item.lo); i.max = String(item.hi); i.step = "1";
    i.value = String(item.value);
    i.setAttribute("aria-label", `${label}: minutes`);
    i.addEventListener("change", () => sPost("reminders", { action: "set", key: item.key, value: Number(i.value) }, `${label}: ${i.value} min`));
    const w = el("span", "row");
    w.append(i, el("span", "dim small", "min"));
    return w;
  };
  for (const x of r.rules) {
    const [lab, cb] = sCheck("On", x.on.value, REM_TIPS[x.rule] || "");
    cb.setAttribute("aria-label", `${x.label}: on`);
    cb.addEventListener("change", () => sPost("reminders", { action: "set", key: x.on.key, value: cb.checked },
      `${x.label}: ${cb.checked ? "on" : "off"}`));
    rows.append(line(x.label, [lab, minutes(x.min, x.label)], [x.on, x.min], REM_TIPS[x.rule]));
  }
  const u = r.units;
  rows.append(line("Units", [sSelect(u.choices, u.value, (v) => sPost("reminders", { action: "set", key: "units", value: v },
    `Units: ${(u.choices.find((c) => c[0] === v) || [v, v])[1]}`), "Units")], [u],
  "Which units the two unit rules watch. Third party: keyholders, utilities, police, with their Dispatch other (DO)"));
  rows.append(line("OK quiets", [minutes(r.snooze_min, "OK quiets")], [r.snooze_min],
    "An OK (a click on the reminder) quiets it this long, on every screen"));
  const on = r.rules.filter((x) => x.on.value).length;
  const edited = r.rules.some((x) => x.on.edited || x.min.edited) || u.edited || r.snooze_min.edited;
  return sCard(sCardHead("Reminders", on ? `${on} on` : "off",
    sHelp("A call or a unit left too long in one state flashes on every console, the Overview map and the wall board, "
      + "timed by the hub's clock from when the state began. Clicking it (or its chip) is an OK: quiet for everyone for the "
      + "set minutes, written on the event's timeline. It stops by itself when the state changes."),
    edited ? sBtn("Reset all", "", () => sAsk("Reset the reminders to their defaults?",
      () => sPost("reminders", { action: "reset_section" }, "Reminders reset"), "Reset"), "danger-quiet") : null),
  rows);
}

// A National Weather Service warning said once by the dispatch voice (weather.js, #132): played where a console is
// open (and the wall board with its Sound on), never keyed. Off by default.
function sWeatherSpeak(w) {
  const [lab, cb] = sCheck("Speak NWS warnings", w.value,
    "A warning for our zones (not a watch or advisory), once, played on the consoles: never transmitted");
  cb.addEventListener("change", () => sPost("dispatch", { action: "set", key: "weather_speak", value: cb.checked },
    cb.checked ? "Weather warnings will be spoken" : "Weather warnings: banner only"));
  return sCard(sCardHead("Weather warnings", w.value ? "spoken" : "banner only",
    sHelp("National Weather Service warnings for Southern Westchester show as a red banner on the console and the wall board. "
      + "With this on, each one is also said once by the dispatch voice where the console is open (the board: with its Sound on). "
      + "Played here only: nothing is keyed on a radio.")), lab);
}

function sJointOnly(j) {
  const box = el("label", "field s-tpl");
  const head = el("span", "row");
  head.append(el("b", "", `Lead-in, ${j.town} only`), el("code", "dim small", "lead_in_joint_only"), sState(j.state), el("span", "spacer"));
  if (j.state === "edited") head.append(sBtn("Reset", j.default, () => sPost("dispatch", { action: "reset", key: `lead_in_joint_only/${j.town}` }, "Lead-in reset")));
  const inp = sInput(j.value, 80, j.default, (v) => { if (v && v !== j.value) sPost("dispatch", { action: "set", key: `lead_in_joint_only/${j.town}`, value: v }, "Lead-in saved"); });
  inp.dataset.town = j.town;
  inp.addEventListener("input", () => { clearTimeout(S.previewT); S.previewT = setTimeout(sPreview, 250); });
  box.append(head, inp);
  return box;
}

// The voice: Kokoro's or Amazon Polly's voices to pick from (GET /api/cad/tts/voices), the speed, a
// Preview (a short sample said through the same render as a dispatch). The engine is the config's.
function sVoice(v) {
  const picked = v.engine === "kokoro" || v.engine === "polly";
  const info = v.engine === "kokoro" ? `Kokoro, fallback ${v.fallback || "macOS say"}`
    : v.engine === "polly" ? "Amazon Polly" : v.engine === "none" ? "None (text only)" : "macOS say";
  const box = sCard(sCardHead("Voice", info,
    picked ? sHelp("Speed 0.5 to 2 (0.92 is a touch slower than talking). Preview says a short sample in this voice.") : null));
  const row = el("div", "row");
  let pick;
  if (picked) {
    pick = sSelect(v.voices.map((x) => [x, x]), v.name, (x) => sPost("dispatch", { action: "set", key: "voice", value: x }, `Voice ${x}`), "Voice");
    api("GET", "/api/cad/tts/voices").then((r) => {     // readable names: "Heart (American, female)", "Matthew (male, generative/neural/standard)"
      const keep = pick.value;
      const list = (r.voices || []).map((x) => [x.id, `${x.name} (${[x.engines ? "" : x.accent, x.gender, (x.engines || []).join("/")].filter(Boolean).join(", ")})`]);
      if (list.length) { fillSelect(pick, list); pick.value = keep; }
    }).catch(() => {});
  } else {
    pick = sInput(v.name, 60, "Voice", (x) => { if (x) sPost("dispatch", { action: "set", key: "voice", value: x }, `Voice ${x}`); });
  }
  const speed = el("input", "s-num");
  speed.type = "number"; speed.min = "0.5"; speed.max = "2"; speed.step = "0.01"; speed.value = String(v.speed);
  speed.setAttribute("aria-label", "Speed");
  speed.addEventListener("change", () => sPost("dispatch", { action: "set", key: "speed", value: Number(speed.value) }, `Speed ${speed.value}`));
  const note = el("span", "dim small");
  const hear = sBtn("Preview", "Say a short sample in this voice", async () => {
    hear.disabled = true;
    setText(note, "Rendering…");
    try {
      const r = await api("POST", "/api/cad/settings/dispatch", { action: "voice_preview" });
      setText(note, r.audio_note || "");
      play(r.audio_url);
    } catch (e) {
      setText(note, e.message);
    }
    hear.disabled = false;
  });
  row.append(el("span", "dim small", "Voice"), pick, el("span", "dim small", "Speed"), speed, el("span", "dim small", `read ${v.repeat || 2}×`),
    hear, note, el("span", "spacer"));
  if (v.edited) row.append(sBtn("Reset", `Config: ${v.default}, ${v.default_speed}`, async () => {
    await sPost("dispatch", { action: "reset", key: "voice" }, "Voice reset");
    await sPost("dispatch", { action: "reset", key: "speed" }, "Voice and speed reset");
  }));
  box.append(row);
  return box;
}

// A joint-response department's own tones (played after PCFD's). Not set: PCFD's are played for them.
function sDeptTones(t) {
  const box = sCard(sCardHead(`${t.town} tones`, t.value ? (t.edited ? "edited" : "default") : "not set"));
  const ol = el("ol", "s-rows tones");
  const add = (hz, sec) => {
    const li = el("li", "s-row");
    const h = el("input"); h.type = "number"; h.min = "100"; h.max = "4000"; h.step = "0.1"; h.value = String(hz); h.className = "s-num";
    h.setAttribute("aria-label", "Hz");
    const d = el("input"); d.type = "number"; d.min = "0.1"; d.max = "10"; d.step = "0.1"; d.value = String(sec); d.className = "s-num";
    d.setAttribute("aria-label", "Seconds");
    li.append(h, el("span", "dim", "Hz for"), d, el("span", "dim", "s"), el("span", "spacer"), sBtn("✕", "Remove", () => li.remove(), "mini"));
    ol.append(li);
  };
  (t.value || []).forEach(([hz, sec]) => add(hz, sec));
  const tools = el("div", "row");
  const save = () => sPost("dispatch", { action: "set", key: `tones/${t.town}`,
    value: [...ol.querySelectorAll("li")].map((li) => [...li.querySelectorAll("input")].map((i) => Number(i.value))) }, `${t.town} tones saved`);
  if (!t.value) {
    box.append(el("p", "warn small", `Not set: PCFD tones play for ${t.town}`));
    tools.append(sBtn("Set tones", "", () => { add(1000, 1); tools.replaceChildren(sBtn("+ Tone", "", () => add(1000, 1), "mini"), el("span", "spacer"), sBtn("Save", "", save, "primary")); }));
  } else {
    tools.append(sBtn("+ Tone", "", () => add(1000, 1), "mini"), el("span", "spacer"),
      sBtn("Clear", "PCFD tones play instead", () => sPost("dispatch", { action: "set", key: `tones/${t.town}`, value: null }, `${t.town} tones cleared`), "danger-quiet"),
      sBtn("Save", "", save, "primary"));
  }
  if (t.edited) tools.append(sBtn("Reset", "Default", () => sPost("dispatch", { action: "reset", key: `tones/${t.town}` }, `${t.town} tones reset`)));
  box.append(ol, tools);
  return box;
}

function sTemplate(w) {
  const box = el("label", "field s-tpl");
  const head = el("span", "row");
  head.append(el("b", "", w.label), el("code", "dim small", w.key), sState(w.state), el("span", "spacer"));
  if (w.state === "edited") head.append(sBtn("Reset", w.default, () => sPost("dispatch", { action: "reset", key: w.key }, `${w.label} reset`)));
  const ta = el("textarea");
  ta.rows = ["new", "new_no_location", "add_on", "upgrade"].includes(w.key) ? 2 : 1;
  ta.maxLength = 400;
  ta.value = w.value;
  ta.dataset.key = w.key;
  ta.spellcheck = false;
  ta.addEventListener("input", () => { clearTimeout(S.previewT); S.previewT = setTimeout(sPreview, 250); });
  ta.addEventListener("change", () => { if (ta.value.trim() !== w.value) sPost("dispatch", { action: "set", key: w.key, value: ta.value.trim() }, `${w.label} saved`); });
  box.append(head, ta);
  return box;
}

async function sPreview() {
  const box = $("#s-preview");
  if (!box) return;
  const wording = {};
  for (const ta of document.querySelectorAll("#s-dispatch textarea[data-key]")) wording[ta.dataset.key] = ta.value;
  const only = {};
  for (const i of document.querySelectorAll("#s-dispatch input[data-town]")) only[i.dataset.town] = i.value;
  wording.lead_in_joint_only = only;
  const my = ++S.previewSeq;
  try {
    const r = await api("POST", "/api/cad/settings/dispatch", { action: "preview", wording });
    if (my !== S.previewSeq) return;
    const rows = [["new", "First dispatch"], ["add_on", "Add-on"], ["upgrade", "Upgrade"],
      ["full", "Full response"], ["joint", "With joint units"], ["joint_only", "Joint units only"]];
    const kids = rows.filter(([k]) => r.texts[k]).map(([k, t]) => {
      const d = el("div", "s-pv");
      d.append(el("div", "dim small", t), el("div", "ptext", r.texts[k]));
      return d;
    });
    for (const [k, e] of Object.entries(r.errors || {})) kids.push(el("div", "error", `${k}: ${e}`));
    kids.push(el("div", "dim small", r.sample));
    box.replaceChildren(...kids);
  } catch (e) {
    box.replaceChildren(el("div", "error", e.message));
  }
}

function sTones(t) {
  const groups = el("div", "s-tone-groups");
  const addTone = (ol, hz, sec) => {
    const li = el("li", "s-row");
    const h = el("input");
    h.type = "number"; h.min = "50"; h.max = "4000"; h.step = "0.1"; h.value = String(hz); h.className = "s-num";
    h.setAttribute("aria-label", "Hz");
    const d = el("input");
    d.type = "number"; d.min = "0.1"; d.max = "10"; d.step = "0.1"; d.value = String(sec); d.className = "s-num";
    d.setAttribute("aria-label", "Seconds");
    li.append(h, el("span", "dim", "Hz for"), d, el("span", "dim", "s"), el("span", "spacer"), sBtn("✕", "Remove", () => li.remove(), "mini"));
    ol.append(li);
  };
  const addGroup = (tones) => {
    const g = el("div", "s-tone-group");
    const ol = el("ol", "s-rows tones");
    const head = el("div", "row");
    head.append(el("b", "small", "Tone group"), el("span", "spacer"),
      sBtn("+ Tone", "", () => addTone(ol, 1000, 1), "mini"), sBtn("Remove group", "", () => g.remove(), "mini"));
    g.append(head, ol);
    tones.forEach(([hz, sec]) => addTone(ol, hz, sec));
    groups.append(g);
  };
  t.value.forEach(addGroup);
  const save = () => {
    const v = [...groups.querySelectorAll(".s-tone-group")].map((g) =>
      [...g.querySelectorAll("li")].map((li) => [...li.querySelectorAll("input")].map((i) => Number(i.value))))
      .filter((g) => g.length);
    sPost("dispatch", { action: "set", key: "tones", value: v }, "Tones saved");
  };
  const tools = el("div", "row");
  tools.append(sBtn("+ Group", "Another department's tones, played after", () => addGroup([[1000, 1]])), el("span", "spacer"),
    t.edited ? sBtn("Reset", "Config default", () => sPost("dispatch", { action: "reset", key: "tones" }, "Tones reset")) : null,
    sBtn("Save", "", save, "primary"));
  [...tools.childNodes].forEach((n) => { if (n.nodeType === 3) n.remove(); });
  return sCard(sCardHead("Port Chester Fire tones", t.edited ? "edited" : "config",
    sHelp("Played before the voice: each group in order (one per department paged, a short gap between). "
      + "50 to 4000 Hz, up to 10 s a tone, 20 s a group.")), groups, tools);
}

// Nearest first (sizeup/cad/eta.py): the recommendation's order and the numbers behind its estimates.
function sEta(e) {
  const rows = el("div", "s-eta");
  const order = sSelect([["nearest", "Nearest first (est.)"], ["card", "Run card / board order"]], e.order.value,
    (v) => sPost("dispatch", { action: "set", key: "order", value: v }, v === "nearest" ? "Order: nearest first" : "Order: run card"),
    "Recommend");
  const line = (label, ctl, item, key, shown) => {
    const r = el("div", "row");
    r.append(el("span", "s-eta-l", label), ctl, sState(item.edited ? "edited" : "default"), el("span", "spacer"));
    if (item.edited) r.append(sBtn("Reset", `Default: ${shown}`, () => sPost("dispatch", { action: "reset", key }, `${label} reset`)));
    return r;
  };
  rows.append(line("Recommend", order, e.order, "order", e.order.default === "nearest" ? "nearest first" : "run card order"));
  for (const n of e.numbers) {
    const i = el("input", "s-num");
    i.type = "number"; i.min = String(n.min); i.max = String(n.max); i.step = n.unit === "×" ? "0.05" : n.unit === "m" ? "5" : "0.5";
    i.value = String(n.value);
    i.setAttribute("aria-label", n.label);
    i.addEventListener("change", () => sPost("dispatch", { action: "set", key: n.key, value: Number(i.value) }, `${n.label}: ${i.value} ${n.unit}`));
    const ctl = el("span", "row");
    ctl.append(i, el("span", "dim small", n.unit));
    rows.append(line(n.label, ctl, n, n.key, `${n.default} ${n.unit}`));
  }
  const q = e.quarters_status;
  const qs = sSelect([["", "None"], ...q.choices.map((c) => [c.code, `${c.code} ${c.name}`])], q.value,
    (v) => sPost("dispatch", { action: "set", key: "quarters_status", value: v }, `In quarters: ${v || "none"}`), "In-quarters status");
  rows.append(line("In-quarters status", qs, q, "quarters_status", q.default || "none"));
  return sCard(sCardHead("Arrival estimates", e.order.value === "nearest" ? "nearest first" : "run card order",
    sHelp(`${e.note} A rig that cleared a call is taken to be driving home for the drive back plus the return margin `
      + "(no turnout from there); the in-quarters status (it ends a return) puts it home at once. Rigs with no home station "
      + "come after those with one.")),
    rows);
}

function sChannels(c) {
  const rows = el("ol", "s-rows chans");
  const add = (v) => {
    const li = el("li", "s-row");
    li.append(sInput(v, 40, "Channel", null, "s-name"), sBtn("✕", "Remove", () => li.remove(), "mini"));
    rows.append(li);
  };
  c.value.forEach(add);
  const tools = el("div", "row");
  tools.append(sBtn("+ Channel", "", () => add("")), el("span", "spacer"),
    c.edited ? sBtn("Reset", `Config: ${c.default.join(", ")}`, () => sPost("dispatch", { action: "reset", key: "channels" }, "Channels reset")) : null,
    sBtn("Save", "", () => sPost("dispatch", { action: "set", key: "channels",
      value: [...rows.querySelectorAll("input")].map((i) => i.value.trim()).filter(Boolean) }, "Channels saved"), "primary"));
  [...tools.childNodes].forEach((n) => { if (n.nodeType === 3) n.remove(); });
  return sCard(sCardHead("Channels", c.edited ? "edited" : "config",
    sHelp("The labels the dispatch box offers. Logged with each dispatch; nothing transmits.")), rows, tools);
}

// ---- users ----------------------------------------------------------------------------------------

function sUsers(panel) {
  const ops = S.data.operators;
  const ol = el("ol", "s-rows users");
  for (const o of ops) {
    // Signed in through Cloudflare Access and not named yet: added under their email (sizeup/cad/settings.py person())
    const unnamed = !!o.email && o.name.toLowerCase() === o.email;
    const who = unnamed ? o.email : o.name;
    const li = el("li", "s-row" + (o.active ? "" : " retired"));
    li.append(sInput(unnamed ? "" : o.name, 60, unnamed ? `Name for ${o.email}` : "Name",
        (v) => { if (v && v !== o.name) sPost("users", { action: "edit", id: o.id, name: v }, `${who} renamed ${v}`); }, "s-name"),
      sInput(o.email, 254, "Sign-in email", (v) => {
        if (v.toLowerCase() !== o.email) sPost("users", { action: "edit", id: o.id, email: v }, v ? `${who}: ${v}` : `${who}: email removed`);
      }, "s-email"),
      sInput(o.callsign, 20, "Callsign", (v) => {
        if (v !== o.callsign) sPost("users", { action: "edit", id: o.id, callsign: v }, `${who}: callsign ${v || "removed"}`);
      }, "s-callsign"),
      sSelect(S_OP_ROLES, o.role, (v) => sPost("users", { action: "edit", id: o.id, role: v }, `${who}: ${v}`), "Role"),
      unnamed ? el("span", "chip s-state pending", "no name") : null,
      el("span", `chip s-state ${o.active ? "added" : "retired"}`, o.active ? "active" : "inactive"),
      cad.who && o.email ? Object.assign(el("span", "s-seen dim small", sSeenText(o.email)), { title: sSeenTip(o.email) }) : null,
      el("span", "spacer"),
      cad.who && o.email && (sActAll() || sActMine(o.email)) ? sBtn("Activity", `${who}: sign-ins, actions, refusals`, () => {
        S.actEmail = o.email;
        sActivity(true);
        $("#s-activity").scrollIntoView({ block: "start", behavior: "smooth" });
      }, "ro-ok") : null,
      o.active ? sBtn("Deactivate", "Off the picker; name kept on records",
        () => sPost("users", { action: "deactivate", id: o.id }, `${who} deactivated`))
        : sBtn("Activate", "", () => sPost("users", { action: "activate", id: o.id }, `${who} activated`)));
    [...li.childNodes].forEach((n) => { if (n.nodeType === 3) n.remove(); });   // the nulls above
    for (const sp of li.querySelectorAll(".s-seen")) sp.dataset.email = o.email;
    ol.append(li);
  }
  if (!ops.length) ol.append(el("li", "empty", "None"));
  const f = el("form", "row s-add");
  const name = sInput("", 60, "Name", null, "s-name");
  const email = sInput("", 254, "Email (optional)", null, "s-email");
  const callsign = sInput("", 20, "Callsign", null, "s-callsign");
  const role = sSelect(S_OP_ROLES, "dispatcher", null, "Role");
  const add = el("button", "primary", "Add");
  add.type = "submit";
  f.append(name, email, callsign, role, add);
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    sPost("users", { action: "add", name: name.value.trim(), email: email.value.trim(), callsign: callsign.value.trim(),
      role: role.value }, `${name.value.trim()} added`);
  });
  const signedIn = cad.who
    ? `Signed in with Cloudflare Access as ${cad.who.email}: that is the operator on everything you do here, named from `
      + "this list. A first sign-in is added under its email, as a dispatcher: give them a name."
    : "Behind Cloudflare Access (the hub) the sign-in email picks the operator. Here anyone at the console picks a name.";
  const roles = cad.who && cad.who.enforced
    ? "Admin: everything. Dispatcher: runs calls, no Settings. Viewer or deactivated: read-only. Until there is an active "
      + "admin with a sign-in email, anyone signed in can change everything; after that the last admin can't be demoted or deactivated."
    : "Roles are recorded here, not enforced. On the hub (Cloudflare Access) they are: admin everything, dispatcher runs calls, "
      + "viewer read-only.";
  panel.replaceChildren(...sHead("Users", `${signedIn}\n${roles}\nOnce there is an operator, the Operator field becomes a picker of the active ones.`),
    sCard(sCardHead("Operators", `${ops.filter((o) => o.active).length} active`), ol, f));
  if (cad.who) {                   // signed in through Cloudflare Access: there is activity to show
    const card = sCard(null);
    card.id = "s-activity";
    panel.append(card);
    sActivityRender();
    sActivity();
  }
}

// Per-person activity on the hub (user, 2026-10-08: "lets add some per user logging too";
// sizeup/cad/activity.py): each sign-in (once per login), what they did, what they were refused,
// and when each was last seen. GET /api/cad/activity: an admin anyone's (or everyone's), anyone
// else only their own. Only behind Cloudflare Access (cad.who): all-in-one records none.
const S_ACT_KINDS = [["", "All"], ["signin", "Sign-ins"], ["action", "Actions"], ["refused", "Refused"], ["search", "Searches"]];
const S_ACT_LIMIT = 300;

function sActAll() { return !!(cad.who && cad.who.may === "admin"); }
function sActMine(email) { return !!(cad.who && email === cad.who.email); }
function sAgo(unix) {
  const s = Math.max(0, serverNow() - unix);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
function sSeen(email) { return ((S.act && S.act.seen) || []).find((x) => x.email === email); }
function sSeenText(email) {
  const v = sSeen(email);
  return v && v.last_seen ? `Seen ${sAgo(v.last_seen)}` + (v.last_ip ? ` ${v.last_ip}` : "") : "";
}
function sSeenTip(email) {
  const v = sSeen(email);
  return v && v.last_seen ? `${fmtRecv(v.last_seen)}${v.last_ua ? `, ${v.last_ua}` : ""}` : "";
}

async function sActivity(force) {
  if (!cad.who || S.section !== "users") return;
  const email = sActAll() ? S.actEmail : cad.who.email;
  const key = `${email}|${S.actKind}`;
  if (!force && S.actKey === key && Date.now() - S.actAt < 15000) return;
  S.actKey = key;
  S.actAt = Date.now();
  try {
    const r = await api("GET", "/api/cad/activity?" + new URLSearchParams({ email, kind: S.actKind, limit: S_ACT_LIMIT }));
    if (S.actKey !== key) return;          // asked again meanwhile
    S.act = { ...r, key };
  } catch (e) {
    const c = $("#s-activity");
    if (c) c.replaceChildren(el("div", "error", `Activity: ${e.message}`));
    return;
  }
  sActivityRender();
  for (const sp of document.querySelectorAll(".s-seen[data-email]")) {
    setText(sp, sSeenText(sp.dataset.email));
    sp.title = sSeenTip(sp.dataset.email);
  }
}

function sActivityRender() {
  const card = $("#s-activity");
  if (!card || !cad.who) return;
  const all = sActAll(), me = cad.who.email;
  const email = all ? S.actEmail : me;
  const op = S.data.operators.find((o) => o.email === email);
  const title = !email ? "Activity: all" : email === me ? "My activity" : `Activity: ${op ? op.name : email}`;
  const kind = sSelect(S_ACT_KINDS, S.actKind, (v) => { S.actKind = v; sActivity(true); }, "Show");
  kind.classList.add("ro-ok");
  const tools = [kind];
  if (all && email) tools.push(sBtn("All users", "", () => { S.actEmail = ""; sActivity(true); }, "ro-ok"));
  if (all && email !== me) tools.push(sBtn("Mine", "", () => { S.actEmail = me; sActivity(true); }, "ro-ok"));
  tools.push(sBtn("Refresh", "", () => sActivity(true), "ro-ok"));
  if (all) {
    const a = el("a", "nav ro-ok", "CSV");
    a.href = "/api/cad/export/activity.csv?" + new URLSearchParams({ email, kind: S.actKind });
    a.download = "";
    a.title = "This list as CSV (kept 90 days)";
    tools.push(a);
  }
  const fresh = S.act && S.act.key === `${email}|${S.actKind}`;
  const rows = fresh ? S.act.rows : [];
  const ol = el("ol", "s-rows s-act" + (email ? " one" : ""));
  for (const r of rows) {
    const li = el("li", "s-act-row" + (r.ok ? "" : " no"));
    const t = el("span", "s-act-t", fmtRecv(r.at));
    t.title = `${fmtTime(r.at, true)}`;
    li.append(t);
    if (!email) li.append(Object.assign(el("span", "s-act-who", r.name && r.name !== r.email ? r.name : r.email), { title: r.email }));
    li.append(el("span", `chip s-kind ${r.kind}`, { signin: "sign-in", action: "action", refused: "refused", search: "search" }[r.kind] || r.kind));
    li.append(Object.assign(el("span", "s-act-sum", r.summary), { title: r.route }));
    li.append(Object.assign(el("span", "s-act-ip dim", r.ip || "—"), { title: r.ua || "" }));
    ol.append(li);
  }
  if (!rows.length) ol.append(el("li", "empty", fresh ? "None" : "Loading"));
  const n = fresh ? `${rows.length}${rows.length >= S_ACT_LIMIT ? "+" : ""}` : "";
  card.replaceChildren(sCardHead(title, n, sHelp("Each sign-in (once per login), change and refusal, newest first, with the "
      + "address and browser it came from. Kept 90 days." + (all ? "" : " An admin sees everyone's.")), ...tools),
    ol);
}

// ---- system ---------------------------------------------------------------------------------------

function sSystem(panel) {
  const sy = S.data.system;
  const recs = el("div", "row csv-row");
  recs.append(sBtn("Open records", "Every numbered event", () => {
    openOverlay("#db-overlay", true); cad.dbAt = 0; renderRecords();
  }, "primary ro-ok"), el("span", "dim small", "CSV"));
  for (const [f, t] of [["incidents", "Incident database"], ["calls", "Call history"], ["unit-history", "Unit history"], ["event-log", "Event log"],
                        ...(cad.state && cad.state.prealert ? [["prealerts", "Phone pre-alerts"]] : [])]) {
    const a = el("a", "nav", t);
    a.href = `/api/cad/export/${f}.csv`;
    a.download = "";
    recs.append(a);
  }
  // What fills itself after a read (the counts, the audit log, the alarms, the pushes, intel) keeps its node and what
  // it shows across a redraw: the page keeps its height, so its scroll position, and the pushes their typed text.
  const kept = (id, make) => panel.querySelector(`#${id}`) || make();
  const counts = kept("s-counts", () => el("dl", "admin"));
  counts.id = "s-counts";
  const file = el("dl", "admin");
  const edits = Object.entries(sy.overrides).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ") || "None";
  for (const [k, v] of [["Data file", sy.data_file], ["Defaults version", sy.defaults_version || "—"],
    ["Content", `sha-256 ${sy.sha256}`], ["Database", sy.db], ["Schema", `v${sy.schema}`], ["Edits", edits]]) {
    const d = el("div");
    d.append(el("dt", "", k), el("dd", "", v));
    file.append(d);
  }
  const exp = sBtn("Export settings", "Edits, run cards and operators as one JSON file", sExport, "primary ro-ok");
  const fileIn = el("input");
  fileIn.type = "file";
  fileIn.accept = ".json,application/json";
  fileIn.setAttribute("aria-label", "Settings file to import");
  fileIn.addEventListener("change", () => sImportFile(fileIn));
  const imp = el("div", "s-import");
  imp.id = "s-import";
  const backup = sCard(sCardHead("Backup", "", sHelp("Export: a backup, or to set up another machine the same. "
    + "Import shows what would change first; nothing changes until you apply it.")), (() => { const r = el("div", "row"); r.append(exp, el("span", "dim small", "Import:"), fileIn); return r; })(), imp);
  const sel = sSelect([["", "All sections"], ...S.data.audit_sections.map((s) => [s, s.charAt(0).toUpperCase() + s.slice(1)])], S.audit,
    (v) => { S.audit = v; S.auditFor = ""; sAudit(); }, "Section");
  sel.classList.add("ro-ok");
  const audit = kept("s-audit", () => el("div", "tscroll"));
  audit.id = "s-audit";
  const alarms = kept("s-alarms", () => sCard(null));     // the emergency alarms' words, channels, drill (alarm.js, #128)
  alarms.id = "s-alarms";
  // the ntfy topics by kind (sPushes): admins only, the names are secret
  const pushes = kept("s-pushes", () => Object.assign(sCard(null), { hidden: true }));
  pushes.id = "s-pushes";
  // radio pre-alerts (intel.js IntelSettings, sizeup/intel): self-contained
  const intel = kept("s-intel", () => Object.assign(sCard(null), { hidden: true }));
  intel.id = "s-intel";
  // the department, with its patch (static/brand)
  const patch = el("img", "s-patch");
  patch.src = "/static/brand/pcfd-patch.svg";
  patch.alt = "Port Chester Fire Department patch";
  patch.width = 68;
  patch.height = 80;
  const dept = el("div", "s-dept-text");
  dept.append(el("b", "", "Port Chester Fire Department"), el("div", "dim small", "Port Chester, N.Y. · April 23, 1823"));
  const about = sCard(patch, dept);
  about.classList.add("s-dept");
  panel.replaceChildren(...sHead("System"),
    about, alarms, pushes, intel,
    ...(sy && S.data.atc ? [sAtcTalkers(S.data.atc)] : []),
    ...(sy && S.data.hub_errors && sIsHub() ? [sHubErrors(S.data.hub_errors)] : []),
    ...(sy && S.data.map_display ? [sMapDisplay(S.data.map_display)] : []),
    ...(sy && S.data.premise_visibility ? [sPremiseVisibility(S.data.premise_visibility)] : []),
    ...(sy && S.data.audio_retention && canDo("admin") ? [sAudioRetention(S.data.audio_retention)] : []),
    ...(sy && canDo("admin") && window.SttFix ? [SttFix.card()] : []),        // transcript corrections (stt-fix.js)
    sCard(sCardHead("Records"), recs, counts),
    ...(S.data.camera_wall ? [sCameraWall(S.data.camera_wall)] : []),
    sCard(sCardHead("Data file"), file),
    backup,
    sCard(sCardHead("Audit log", "", sel), audit),
    ...(canDo("admin") ? [sCard(sCardHead("Style guide", "", sHelp("Every component in every state, on the tokens: "
      + "check a change to the look here first.")), (() => {
      const a = el("a", "nav", "Open the style guide");
      a.href = "#styleguide";
      const r = el("div", "row");
      r.append(a, el("span", "dim small", "Admin"));
      return r;
    })())] : []));
  if (S.importing) sShowImport();
  sCounts();
  sAudit();
  if (window.Alarm) sCardMount(alarms, Alarm.mountSettings, S.saved === "alarms");
  sPushes(pushes);
  if (window.IntelSettings) sCardMount(intel, IntelSettings.mount, S.saved === "intel");
}

// Who is talking on HPN Tower (#252, sizeup/atc_talkers.py): the callsign heard on each airband row, matched to ADS-B,
// and the aircraft's "talking" ring on the map. Both on by default; off: the raw transmissions, as before. Admins.
function sAtcTalkers(a) {
  const card = sCard(sCardHead("ATC talkers", a.identify.value ? "on" : "off",
    sHelp("HPN Tower is AM voice: no radio IDs. With this on, each transmission's callsign is taken from the words "
      + "(JetBlue 1234, November 4 7 Delta Quebec) and matched to an aircraft on ADS-B near HPN within 2 minutes: "
      + "confirmed (that exact callsign), probable (one character off, the only such aircraft, doing what the words say), "
      + "or heard (not on ADS-B; two candidates are never picked between). The tower (TWR / GND) is told from its own "
      + "name, the word order and its steady carrier. Speech-derived: never a radio ID, and never in an airport "
      + "pre-alert's matched aircraft. No callsign heard: nothing is labelled.")));
  card.id = "s-atc";
  const hints = {
    identify: "A chip on each HPN Tower transmission: TWR, JBU1234 · A320 ✓, heard: N47DQ; and its exchanges folded together",
    map: "The aircraft talking gets a ring on the map, the tower a lit TWR; its box lists its transmissions; a chip click centres it",
    // #257 (sizeup/atc_vocab.py): Amazon Transcribe's sizeup-atc list, with the operators seen near HPN lately
    vocab: "Every few hours, when the operators seen near HPN in the last two days change, the sizeup-atc vocabulary is "
      + "sent again with their names (Moxy, Execjet ...) added to the fixed list. Streams open keep going; new ones use "
      + "it once Amazon has it ready. Off: only what Settings > Radios > Vocabulary > Push sends",
  };
  for (const key of ["identify", "map", "vocab"]) {
    const v = a[key];
    if (!v) continue;
    const [lab, cb] = sCheck(v.label, v.value, hints[key]);
    cb.disabled = !canDo("admin") || (key === "map" && !a.identify.value);
    cb.addEventListener("change", () => sPost("atc", { action: "set", key, value: cb.checked },
      `${v.label}: ${cb.checked ? "on" : "off"}`));
    card.append(lab);
  }
  if (!(a.channels || []).length) card.append(Object.assign(el("p", "dim small", "No airband channel"), { title: '[[radio.channels]] kind = "airband"' }));
  return card;
}

// The hub's own error spikes (#285, sizeup/errwatch.py): pushed to the status topic, at most one per error an hour,
// plus "back to normal". The thresholds count error lines in 5 min. Admins; the hub only.
function sHubErrors(e) {
  const card = sCard(sCardHead("Hub error alerts", e.on.value ? "on" : "off",
    sHelp(`Pushes a notice to the status topic when one error repeats more than ${e.per_signature.value} times in `
      + `${e.window_min} min, or all errors exceed ${e.total.value}. Names the module and type, never the message. `
      + `At most hourly per error; 'back to normal' after ${e.quiet_min} quiet min.`)));
  card.id = "s-hub-errors";
  const [lab, cb] = sCheck(e.on.label, e.on.value, "Off: still counted on Settings > Station > Health, nothing pushed");
  cb.disabled = !canDo("admin");
  cb.addEventListener("change", () => sPost("hub_errors", { action: "set", key: "on", value: cb.checked },
    `${e.on.label}: ${cb.checked ? "on" : "off"}`));
  card.append(lab);
  for (const key of ["per_signature", "total"]) {
    const v = e[key];
    const n = el("input", "s-num");
    n.type = "number"; n.min = String(v.min); n.max = String(v.max); n.step = "1";
    n.value = String(v.value);
    n.disabled = !canDo("admin");
    n.setAttribute("aria-label", v.label);
    n.addEventListener("change", () => sPost("hub_errors", { action: "set", key, value: Number(n.value) },
      `${v.label}: ${n.value}`));
    const row = el("div", "row");
    row.append(el("span", "s-eta-l", v.label), n, sState(v.edited ? "edited" : "default"), el("span", "spacer"));
    if (v.edited && canDo("admin")) row.append(sBtn("Reset", `Default: ${v.default}`,
      () => sPost("hub_errors", { action: "reset", key }, `${v.label}: ${v.default}`)));
    card.append(row);
  }
  return card;
}

// The rigs on the maps by zoom (#376, cadmap.js declutter; sizeup/cad/mapview.py DISPLAY_DEFAULTS): below each zoom a
// rig is a dot (idle at a firehouse: one badge). The console's maps and the wall board's. Admins.
// Who sees each category of address notes (#403, sizeup/cad/premise.py; Settings key premise_visibility). Admins.
function sPremiseVisibility(d) {
  const card = sCard(sCardHead("Address notes", "",
    sHelp("Who sees each category of address notes on the incident card and the Search page. Dispatchers: dispatchers "
      + "and admins; Admins: admins only. A restricted category is never in the screen's live push.")));
  card.id = "s-premise-vis";
  const names = { everyone: "Everyone", dispatcher: "Dispatchers", admin: "Admins" };
  for (const [key, v] of Object.entries(d)) {
    const s = el("select");
    for (const c of v.choices) s.append(new Option(names[c] || c, c));
    s.value = v.value;
    s.disabled = !canDo("admin");
    s.setAttribute("aria-label", `${v.label}: who sees it`);
    s.addEventListener("change", () => sPost("premise_visibility", { action: "set", key, value: s.value },
      `${v.label}: ${names[s.value]}`));
    const row = el("div", "row");
    row.append(el("span", "s-eta-l", v.label), s, sState(v.edited ? "edited" : "default"), el("span", "spacer"));
    card.append(row);
  }
  return card;
}

function sMapDisplay(d) {
  const card = sCard(sCardHead("Map display", "",
    sHelp("Below these zooms an idle rig is a dot (two or more at a firehouse: one badge, \u201cHQ \u00b7 6\u201d) and a rig on a "
      + "call a strong dot; a rig on a call stays full while the map keeps its own frame (not moved by hand), and for a minute after its status changes. Active never later than idle, nor than 12. Followed, picked, talking or hovered rigs always show in full. "
      + "Zoom 12: the whole village; 15: a few blocks.")));
  card.id = "s-map-display";
  for (const key of ["detail_zoom", "active_zoom", "board_detail_zoom", "board_active_zoom"]) {
    const v = d[key];
    if (!v) continue;
    const n = el("input", "s-num");
    n.type = "number"; n.min = String(v.min); n.max = String(v.max); n.step = "0.5";
    n.value = String(v.value);
    n.disabled = !canDo("admin");
    n.setAttribute("aria-label", v.label);
    n.addEventListener("change", () => sPost("map_display", { action: "set", key, value: Number(n.value) },
      `${v.label}: ${n.value}`));
    const row = el("div", "row");
    row.append(el("span", "s-eta-l", v.label), n, sState(v.edited ? "edited" : "default"), el("span", "spacer"));
    if (v.edited && canDo("admin")) row.append(sBtn("Reset", `Default: ${v.default}`,
      () => sPost("map_display", { action: "reset", key }, `${v.label}: ${v.default}`)));
    card.append(row);
  }
  return card;
}

// How long the hub keeps each channel's / talkgroup's audio, and its transcripts and metadata (#414,
// sizeup/audiostore.py). Days; a change applies to what is already kept at the next prune. A transcript is never kept
// shorter than its audio. The totals: what is kept now and its S3 storage a month. Admins; the hub only.
function sAudioRetention(r) {
  const mb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e7 ? `${Math.round(b / 1e6)} MB` : `${(b / 1e6).toFixed(1)} MB`);
  const usd = (v) => `$${v < 10 ? v.toFixed(2) : Math.round(v)}/mo`;
  const tt = r.totals || {};
  const card = sCard(sCardHead("Audio retention", `${r.default.value} d · transcripts ${r.default.t_value} d`,
    sHelp(`Days each channel's audio and transcripts are kept, then deleted; a change applies to what is already kept. `
      + `A transcript is never kept shorter than its audio. Talkgroup over channel over default. `
      + `Clips on an event, an alarm or an emergency: at least ${r.event_days ? `${r.event_days} d` : "forever"}; `
      + `attached ones are kept with the incident. Costs: S3 Standard storage at $${tt.usd_gb_month}/GB-month; `
      + `uploads add about $1/mo.`)));
  card.id = "s-audio-retention";
  if (tt.audio) {
    const t = el("div", "dim small", `Audio ${tt.audio.clips} · ${tt.audio.gb} GB · ${usd(tt.audio.usd_month)} · `
      + `Transcripts ${tt.transcripts.rows} · Incident audio ${tt.incident.clips} · ${tt.incident.gb} GB · ${usd(tt.incident.usd_month)}`);
    t.title = "Live audio: the clips kept now (S3 and audio.sqlite). Transcripts: rows in the radio log. "
      + "Incident audio: radio clips copied to incidents' attachments, kept until an admin deletes them.";
    card.append(t);
  }
  const num = (sec, key, label, v, lo, hi, what) => {
    const n = el("input", "s-num");
    n.type = "number"; n.min = String(lo); n.max = String(hi); n.step = "0.5";
    n.value = String(v);
    n.setAttribute("aria-label", `${label}: ${what} days kept`);
    n.addEventListener("change", () => sPost(sec, { action: "set", key, value: Number(n.value) }, `${label} ${what}: ${n.value} d`));
    return n;
  };
  const row = (key, label, a, aEdited, t, tEdited, info, title) => {
    const lab = el("span", "s-eta-l", label);
    if (title) lab.title = title;
    const x = el("div", "row");
    x.append(lab, num("audio_retention", key, label, a, r.min, r.max, "audio"), el("span", "dim small", "d audio"),
      num("transcript_retention", key, label, t, r.t_min, r.t_max, "transcripts"), el("span", "dim small", "d text"),
      sState(aEdited || tEdited ? "edited" : "default"), el("span", "dim small", info), el("span", "spacer"));
    if (aEdited || tEdited) x.append(sBtn("Reset", "Back to the defaults", () => {
      if (aEdited) sPost("audio_retention", { action: "reset", key }, `${label}: default`);
      if (tEdited) sPost("transcript_retention", { action: "reset", key }, `${label}: default`);
    }));
    return x;
  };
  const d = r.default;
  card.append(row("_default", "Default", d.value, d.edited, d.t_value, d.t_edited, "", "Every channel without its own"));
  for (const c of r.rows) {
    card.append(row(c.key, c.label, c.days, c.edited, c.t_days, c.t_edited, c.clips ? `${c.clips} · ${mb(c.bytes)}` : "",
      c.talkgroup != null ? `tg:${c.talkgroup}` : ""));
  }
  if (r.format) {                  // how new clips are kept: WAV only for now (FLAC / Opus would be chosen here)
    const f = r.format;
    const sel = sSelect(f.choices.map((v) => [v, v === "wav" ? "WAV (uncompressed)" : v]), f.value,
      (v) => sPost("audio_retention", { action: "set", key: "_format", value: v }, `Audio format: ${v}`), "Audio format");
    sel.disabled = f.choices.length < 2;
    const x = el("div", "row");
    x.append(el("span", "s-eta-l", "Format"), sel, el("span", "spacer"));
    card.append(x);
  }
  card._sig = sCardSig(card);          // (as drawn: sCardSwap)
  return card;
}

// ---- station: its health and alerts (health.js, #79), its logs (logs.js), its hub link (hublink.js) --------------------
// Health and Logs: admins only (#203), on the hub. Hub link: this machine's forwarding, not on the hub. One tab bar (the
// Radios page's); each tab's part shown, the rest hidden (health.js polls only while its part is on screen).

const sIsHub = () => !!(cad.state && cad.state.cloud && cad.state.cloud.role === "hub");
function sStationTabs() {
  const hubAdmin = canDo("admin") && sIsHub();
  return S_STATION_TABS.filter(([k]) => (k === "link" ? !sIsHub() : hubAdmin));
}
// The tab in effect, once the state says whether this is the hub (a cold open of #settings/station/health on the hub
// runs before cad.state: nothing is chosen or rewritten until then).
function sStationPick() {
  if (!cad.state) return false;
  const tabs = sStationTabs();
  if (!tabs.some(([k]) => k === S.stab)) S.stab = tabs.length ? tabs[0][0] : "";
  if (S.section === "station" && S.stab && location.hash !== `#settings/station/${S.stab}`) {
    history.replaceState(null, "", `#settings/station/${S.stab}`);
  }
  return true;
}
function sStationTab() {           // the nav item: shown while it has a tab (an admin on the hub; anyone off it)
  const a = document.querySelector('#s-sections a[data-s="station"]');
  if (a) show(a.parentNode, sStationTabs().length > 0);
}
function sStationPanel(panel) {
  const bar = $("#s-station-tabs");
  if (!sStationPick()) {                 // the state isn't in yet: sHook renders again when it is
    show(bar, false);
    for (const n of panel.querySelectorAll("[data-stab]")) show(n, false);
    return;
  }
  S.stationShown = true;
  const tabs = sStationTabs();
  bar.replaceChildren(...tabs.map(([k, t]) => {
    const b = sBtn(t, "", () => {
      S.stab = k;
      history.replaceState(null, "", `#settings/station/${k}`);
      sRender(true);
    }, "tab" + (S.stab === k ? " on" : ""));
    b.setAttribute("aria-pressed", String(S.stab === k));
    return b;
  }));
  show(bar, tabs.length > 1);
  for (const n of panel.querySelectorAll("[data-stab]")) show(n, n.dataset.stab === S.stab);
  if (!tabs.length) {
    $("#s-station-health").replaceChildren(...sHead("Station"), el("p", "empty", `Admins only. ${askAdmin()}`));
    show($("#s-station-health"), true);
    return;
  }
  if (S.stab === "health") sHealth($("#s-station-health"));
  if (S.stab === "logs" && window.StationLogsView) StationLogsView.mount($("#s-station-logs"));
  if (S.stab === "stations" && window.StationsCard) StationsCard.mount($("#s-station-stations"));
  if (S.stab === "workers" && window.SttWorkersCard) SttWorkersCard.mount($("#s-station-workers"));   // GPU workers (#639)
  if (S.stab === "adapter" && window.AdapterPasswordCard) AdapterPasswordCard.mount($("#s-station-adapter"));
  if (S.stab === "link") sStation();
}
function sHealth(panel) {
  if (!canDo("admin")) {
    panel.replaceChildren(...sHead("Health"), el("p", "empty", `Admins only. ${askAdmin()}`));
    return;
  }
  if (window.StationHealth) StationHealth.mount(panel);
}

// ---- system: push notifications (sizeup/notify.py Pushes, settings.py _notify_op) ----------------------------------
// The two ntfy topics by kind: status & health (station health, Amazon Transcribe and its budget) and alerts (the
// emergency buttons, mayday, aircraft squawks). Admins only: a topic's name is its only secret on ntfy.sh, so the
// server shows them to nobody else. Empty = the default (SSM / the config); Turn off = no pushes of that kind.

const S_PUSH_FROM = { settings: "Settings", ssm: "SSM", env: "Environment", config: "Config file", default: "Default",
  fallback: "Shared", off: "Turned off", none: "Not set", same: "Refused" };
// A refusal goes under its field and stays until that field is edited or saved again (S.pushErr); what was typed stays.
function sPushErr(card, key, text) {
  if (text) S.pushErr[key] = text; else delete S.pushErr[key];
  const line = card.querySelector(`[data-err="${key}"]`), f = card.querySelector(`[data-key="${key}"]`);
  if (line) setText(line, text || "");
  if (f) { if (text) f.setAttribute("aria-invalid", "true"); else f.removeAttribute("aria-invalid"); }
}
function sPushErrLine(key) {
  const e = el("div", "error", S.pushErr[key] || "");
  e.id = `s-push-err-${key}`;
  e.dataset.err = key;
  e.setAttribute("role", "alert");
  return e;
}
function sPushField(card, key, input) {
  input.dataset.key = key;
  input.setAttribute("aria-describedby", `s-push-err-${key}`);
  if (S.pushErr[key]) input.setAttribute("aria-invalid", "true");
  input.addEventListener("input", () => { if (S.pushErr[key]) sPushErr(card, key, ""); });
  return input;
}
// The server's reason without the field's own name in front (it is under that field); no answer at all: said so.
function sPushWhy(e, label) {
  if (e instanceof TypeError) return "no answer from the server";
  const m = String((e && e.message) || e);
  return label && m.toLowerCase().startsWith(`${label.toLowerCase()}: `) ? m.slice(label.length + 2) : m;
}
// One change to the topics (Save, Use default / Clear, Turn off). Refused or not sent: why, under the field (sPushErr);
// the card is not redrawn and the field keeps what was typed. Done: the card catches up in place.
async function sPushPost(card, key, label, body, done, input) {
  const op = operator();
  if (!op) {
    sPushErr(card, key, "Enter operator name (top right)");
    ($("#operator-pick").hidden ? $("#operator") : $("#operator-pick")).focus();
    return;
  }
  sPushErr(card, key, "");
  try {
    await api("POST", "/api/cad/settings/notify", { ...body, operator: op });
  } catch (e) {
    sPushErr(card, key, `${body.action === "set" || body.action === "server" ? "Not saved" : "Not changed"}: ${sPushWhy(e, label)}`);
    return;
  }
  sMsg(done, true);
  if (input) input._base = input.value;          // saved: no longer typed text to keep
  S.auditFor = "";
  await sPushes(card);
  sLoad({ must: true });
}
async function sPushes(card) {
  if (!card) return;
  if (!canDo("admin")) { card.hidden = true; return; }
  const seq = ++S.pushSeq;
  let d;
  try {
    d = await api("GET", "/api/cad/notify");
  } catch (e) {
    if (seq !== S.pushSeq || card.dataset.built) return;          // (a refresh that failed: what is shown stays)
    card.hidden = false;
    card.replaceChildren(sCardHead("Push notifications"), el("div", "error", `Push notifications: ${e.message}`));
    return;
  }
  if (seq !== S.pushSeq) return;          // (a newer read is on its way)
  if (!d || !d.configured) { card.hidden = true; return; }
  const when = (t) => (t ? fmtTime(t, true) : "");
  const act = (what, b) => { b.dataset.act = what; return b; };          // what the button does (sMorph: never another's click)
  const kids = [];
  for (const k of d.kinds) {
    const head = el("div", "row");
    head.append(el("b", "s-eta-l", k.label), el("span", `chip s-state ${k.topic ? "edited" : "default"}`, S_PUSH_FROM[k.source] || k.source));
    const now = el("div", "dim small");
    now.append(k.topic ? el("span", "", "In effect: ") : Object.assign(el("span", k.kind === "intel" && k.source !== "off" ? "warn" : "",
      k.source === "off" ? "Off" : k.source === "same" ? "Refused: same as alerts / status" : "Not set"),
      { title: k.kind === "intel" ? "No intel pushes: they need a topic of their own, never the alerts or status topic" : "No pushes of this kind" }),
      ...(k.topic ? [el("span", "mono", k.topic), el("span", "", ` (${k.source_text})`)] : []));
    const input = sPushField(card, k.kind, sInput(k.settings.topic, d.max_len, `${k.label} (empty: the default)`, null, "mono"));
    input.minLength = d.min_len;
    const row = el("div", "row");
    row.append(input,
      act("set", sBtn("Save", "This topic, from Settings", () => sPushPost(card, k.kind, k.label, { action: "set", key: k.kind, topic: input.value.trim() }, `${k.label}: saved`, input), "primary")),
      ...(k.settings.topic || k.settings.off ? [act("reset", sBtn("Use default", `The default: ${k.default.topic ? k.default.source_text : "not set"}`,
        () => sPushPost(card, k.kind, k.label, { action: "reset", key: k.kind }, `${k.label}: back to the default`)))] : []),
      ...(k.settings.off ? [] : [act("off", sBtn("Turn off", "No pushes of this kind until it is set again",
        () => sAsk(`Turn off ${k.label.toLowerCase()} pushes? None reach a phone until it is set again.`,
          () => sPushPost(card, k.kind, k.label, { action: "off", key: k.kind }, `${k.label}: off`), "Turn off"), "danger-quiet"))]),
      el("span", "spacer"),
      act("test", sBtn("Send test push", k.topic ? `“sizeup test: ${k.kind} topic” to the topic in effect` : "No topic in effect", () => sPushTest(card, k), k.topic ? "" : "quiet")));
    const last = el("div", "dim small");
    const lp = k.last;
    last.textContent = [lp ? `Last push ${when(lp.at)} ${lp.ok ? "sent" : `failed (${lp.error})`} · ${lp.sent} sent · ${lp.failed} failed`
      : "No pushes since restart",
    k.test ? `Test ${when(k.test.at)} ${k.test.ok ? "sent" : `failed (${k.test.error})`}` : ""].filter(Boolean).join(" · ");
    kids.push(head, now, row, sPushErrLine(k.kind), last);
  }
  // The feeds sizeup SUBSCRIBES to (Tapout's phone pre-alert) and the topics never published to: names only in Settings.
  const pa = d.prealert || {};
  for (const [k, key, label, hint] of [["ring", "prealert_ring", "Pre-alert ring topic", "The topic sizeup reads for a ringing station phone (read only; never published to). Empty: no pre-alerts. SIZEUP_PREALERT_RING_TOPIC overrides this."],
    ["status", "prealert_status", "Pre-alert status topic", "The topic sizeup reads for the ring detector's heartbeat (read only; never published to). Empty: no heartbeat watch. SIZEUP_PREALERT_STATUS_TOPIC overrides this."]]) {
    const f = pa[k];
    if (!f) continue;
    const head = el("div", "row");
    head.append(el("b", "s-eta-l", label), el("span", `chip s-state ${f.topic ? "edited" : "default"}`, S_PUSH_FROM[f.source] || f.source), sHelp(hint));
    const now = el("div", "dim small");
    if (f.topic) now.append(el("span", "", "Reading: "), el("span", "mono", f.topic), el("span", "", ` (${f.source_text})`));
    else now.append(Object.assign(el("span", f.source === "off" ? "" : "warn", f.source === "off" ? "Off" : "Not set: no pre-alerts yet"),
      { title: "Set it once here: the feed starts within seconds" }));
    const input = sPushField(card, key, sInput(f.settings, d.max_len, `${label} (read only)`, null, "mono"));
    const row = el("div", "row");
    row.append(input,
      act("set", sBtn("Save", "Start reading this topic (within seconds)", () => sPushPost(card, key, label, { action: "set", key, topic: input.value.trim() }, `${label}: saved`, input), "primary")),
      ...(f.settings || f.off ? [act("reset", sBtn("Clear", "Remove the Settings value", () => sPushPost(card, key, label, { action: "reset", key }, `${label}: cleared`)))] : []),
      ...(f.off ? [] : [act("off", sBtn("Turn off", "Stop reading it", () => sPushPost(card, key, label, { action: "off", key }, `${label}: off`), "danger-quiet"))]));
    kids.push(head, now, row, sPushErrLine(key));
  }
  const prot = (d.protected && d.protected.listed) || [];
  const protIn = sPushField(card, "protected", sInput(prot.join(", "), 1000, "Extra protected topics (comma separated)", null, "mono"));
  const protRow = el("div", "row");
  protRow.append(el("b", "s-eta-l", "Protected"), protIn,
    sBtn("Save", "Topics sizeup never publishes to, besides the ones it reads", () => sPushPost(card, "protected", "Protected", { action: "set", key: "protected", topics: protIn.value.trim() }, "Protected topics saved", protIn), "primary"),
    sHelp(`Never published to: every topic read above, plus these (${d.protected ? d.protected.count : 0} in all). A push topic can't be one of them.`));
  kids.push(protRow, sPushErrLine("protected"));
  const srv = sPushField(card, "server", sInput(d.server.settings, 200, `ntfy server (empty: ${d.server.default})`, null, "mono"));
  const sRow = el("div", "row");
  sRow.append(el("span", "s-eta-l", "ntfy server"), srv,
    sBtn("Save", "This server, from Settings (empty: the default)", () => sPushPost(card, "server", "ntfy server", { action: "server", key: "server", value: srv.value.trim() }, "ntfy server saved", srv), "primary"),
    el("span", "dim small", `In effect: ${d.server.value} (${S_PUSH_FROM[d.server.source] || d.server.source})`));
  kids.push(sRow, sPushErrLine("server"));
  if (S.data.alert_mutes) kids.push(...sMutes(S.data.alert_mutes));
  card.hidden = false;
  if (location.hash === "#settings/system/pushes" && S.pushScrolled !== location.hash) {     // from Settings > Station > Health
    S.pushScrolled = location.hash;
    requestAnimationFrame(() => card.scrollIntoView({ block: "start" }));
  }
  // In place (sMorph): a line that changed changes; a field with the focus or typed text is never replaced.
  const next = el("div");
  next.append(sCardHead("Push notifications", "", sHelp("Pushes go to our own ntfy topics, by kind. Status & health: "
    + "station health (silent, behind, a dongle or program down, the disk, the whisper queue), Amazon Transcribe failing and its "
    + "monthly limit, and their recoveries. Alerts: the emergency buttons, a mayday, an aircraft emergency, their acknowledgements "
    + "and all clears. Pre-alerts (intel): radio pre-alerts (police / fire chatter), on their own topic only: never the alerts "
    + "or status topic (refused); not set: no intel pushes. "
    + "Only one of alerts / status set: both go to it. A name set here wins over SSM and the config file; empty uses "
    + `them. Never a protected topic (the ones read for pre-alerts, and the extras listed): refused. ${d.min_len} to ${d.max_len} letters, digits, - and _; `
    + "ntfy topics are public by name, so make them long and random. Only admins see the names.")), ...kids);
  sStamp(next);
  if (card.dataset.built) { sMorph(card, next); return; }
  card.replaceChildren(...next.childNodes);
  card.dataset.built = "1";
}
// Health alerts muted (#340, health.py MUTE_KINDS): still on Settings > Station > Health, not pushed (nor their recovery).
const S_MUTE_FOR = [["", "Until unmuted"], ["1", "1 h"], ["24", "24 h"], ["168", "7 days"]];
function sMutes(m) {
  const head = el("div", "row");
  head.append(el("b", "s-eta-l", "Mute alerts"), el("span", "dim small", `${m.kinds.filter((k) => k.muted).length} muted`));
  if (window.Help && Help.info) head.append(Help.info("A muted alert still shows on Settings > Station > Health, not pushed. One pushed before the mute still gets its recovery."));
  const rows = m.kinds.map((k) => {
    const [lab, cb] = sCheck(k.label, k.muted, k.muted ? "Unmute: pushed again" : "Mute: Health only, no push");
    const sel = sSelect(k.timed_only ? S_MUTE_FOR.slice(1) : S_MUTE_FOR, "24", null, `${k.label}: mute for`);
    if (k.timed_only) sel.title = "Critical: muted for a time only";
    cb.disabled = sel.disabled = !canDo("admin");
    cb.addEventListener("change", () => sPost("alert_mutes", cb.checked
      ? { action: "mute", key: k.key, hours: sel.value ? Number(sel.value) : null }
      : { action: "unmute", key: k.key }, `${k.label}: ${cb.checked ? "muted" : "pushed"}`));
    const row = el("div", "row");
    row.append(lab, el("span", "spacer"), k.muted
      ? el("span", "chip s-state edited", k.until ? `Muted until ${fmtTime(k.until)}` : "Muted")
      : sel);
    return row;
  });
  return [head, ...rows];
}
async function sPushTest(card, k) {
  const op = operator();
  if (!op) { sPushErr(card, k.kind, "Enter operator name (top right)"); return; }
  try {
    const r = await api("POST", "/api/cad/settings/notify", { action: "test", key: k.kind, operator: op });
    sPushErr(card, k.kind, r.test.ok ? "" : `Test push not sent: ${r.test.error}`);
    if (r.test.ok) sMsg(`Test push sent to the ${k.label.toLowerCase()}`, true);
  } catch (e) {
    sPushErr(card, k.kind, `Test push not sent: ${sPushWhy(e, k.label)}`);
  }
  S.auditFor = "";
  sAudit();
  sPushes(card);
}

// ---- data sources (#186): every data pull, Refresh now, how often (datasources.js; sizeup/datasources.py) -------------

function sData(panel) {
  if (window.DataSourcesPanel) DataSourcesPanel.mount(panel, { msg: sMsg, focus: S.dsFocus });
}

// ---- system: the camera wall's area (#125; sizeup/roads.py Roads.area, settings.py _wall_op) ----------------
// The Overview's and the wall board's Cameras show every camera in our fire districts plus this margin; a camera
// can be put on (Always) or taken off (Never) by hand. Every camera we have is listed with where it falls.

const S_WALL_MODES = [["auto", "Auto"], ["include", "Always"], ["exclude", "Never"]];
const sDist = (m) => (m == null ? "—" : m < 1000 ? `${m} m` : `${(m / 1000).toFixed(1)} km`);
function sCameraWall(w) {
  const a = w.area || {};
  const margin = el("input", "s-num");
  margin.type = "number"; margin.min = "0"; margin.max = String(w.max_margin_m); margin.step = "50";
  margin.value = String(w.margin_m);
  margin.setAttribute("aria-label", "Margin around our fire districts (m)");
  margin.addEventListener("change", () => sPost("camera_wall", { action: "set", key: "margin_m", value: Number(margin.value) },
    `Camera wall margin: ${margin.value} m`));
  const row = el("div", "row");
  row.append(el("span", "s-eta-l", "Margin around our area"), margin, el("span", "dim small", "m"),
    sState(w.margin_m !== w.default_margin_m ? "edited" : "default"), el("span", "spacer"));
  if (w.edited) row.append(sBtn("Reset", `Default: ${w.default_margin_m} m, no cameras chosen by hand`,
    () => sAsk("Reset the camera wall to the default margin, with no cameras chosen by hand?", () => sPost("camera_wall", { action: "reset" }, "Camera wall reset"), "Reset")));
  const basis = { fire_districts: "Our fire districts", municipalities: "Our municipal boundaries", bbox: "A box around Port Chester and Rye Brook" }[a.basis] || "—";
  const facts = el("dl", "admin");
  const st = w.streams || {};
  for (const [k, v] of [["Area", `${basis}${a.names && a.names.length ? `: ${a.names.join(", ")}` : ""}`],
                        ["On the wall", w.n != null ? `${w.n} camera${w.n === 1 ? "" : "s"}${st.streaming != null ? `, ${st.streaming} with live video` : ""}` : "—"],
                        ["Live streams", st.max ? `${st.max} at once through the hub; ${st.per_viewer || st.streaming || 0} per screen` : "—"]]) {
    const d = el("div");
    d.append(el("dt", "", k), el("dd", "", v));
    facts.append(d);
  }
  const kids = [row, facts];
  if (a.note) kids.push(el("div", "notice warn", a.note));
  if (w.error) kids.push(el("div", "error", `Cameras not listed: ${w.error}`));
  if (w.cameras && w.cameras.length) {
    const t = el("table", "evtable s-wall");
    const head = el("tr");
    for (const h of ["Camera", "Road", "Source", "Where", "On the wall", "Choice"]) head.append(el("th", "", h));
    t.append(el("thead"), el("tbody"));
    t.tHead.append(head);
    for (const c of w.cameras) {
      const tr = el("tr");
      const name = el("td", "s-wall-name");
      name.append(el("span", "", c.name), el("span", "dim small mono", ` ${c.id}`));
      const where = c.auto === "inside" ? "Inside" : c.auto === "margin" ? `${sDist(c.dist_m)} out (in the margin)` : `${sDist(c.dist_m)} out`;
      const pick = sSelect(S_WALL_MODES, c.mode, (v) => sPost("camera_wall", { action: "camera", id: c.id, mode: v },
        `${c.name}: ${S_WALL_MODES.find(([k]) => k === v)[1]}`), `On the camera wall: ${c.name}`);
      const choice = el("td");
      choice.append(pick);
      tr.append(name, el("td", "", [c.road, c.dir].filter(Boolean).join(" ") || "—"), el("td", "", c.source || ""),
        el("td", c.auto === "outside" ? "dim" : "", where), el("td", c.in ? "" : "dim", c.in ? (c.has_video ? "Yes, live" : "Yes") : "No"), choice);
      t.tBodies[0].append(tr);
    }
    const sc = el("div", "tscroll");
    sc.append(t);
    kids.push(sc);
  } else if (w.cameras) kids.push(el("div", "empty", "No cameras from the highway sources yet"));
  const card = sCard(sCardHead("Camera wall", w.n != null ? `${w.n} on the wall` : "",
    sHelp("The Cameras button on the Overview and the wall board shows every camera in our area: inside our fire districts "
      + "(the county's, Port Chester FD's includes Rye Brook) or within the margin around them, so the I-95 and I-287 cameras "
      + "at our edges count. Always and Never put a camera on or take it off by hand. Down cameras stay on, folded into one tile.")),
    ...kids);
  card.id = "s-camera-wall";
  card._sig = sCardSig(card);          // (as drawn: sCardSwap)
  return card;
}

// ---- station: Hub link (forwarding to the hub, sizeup/cloud/stationmode.py) -------------------------
// This machine's own switch: on, what it hears also goes to the hub (the CAD of record then); off,
// all-in-one. Radios keep running either way. Not on the hub (409: hidden). The card is hublink.js's,
// the same as on the station page; secrets are shown only as where they came from.

function sStation() {
  const card = $("#s-hublink");
  if (!card) return;
  HubLink.mount(card, {
    title: "Hub link",
    operator,
    askOperator: () => ($("#operator-pick").hidden ? $("#operator") : $("#operator-pick")).focus(),
    msg: sMsg,
    changed: () => { S.auditFor = ""; sAudit(); },
    offText: "Stop forwarding to the hub? This console becomes the CAD of record again; anything still queued waits here.",
    time: (t) => fmtTime(t, true),
  });
}

async function sCounts() {
  try {
    const a = await api("GET", "/api/cad/admin");
    const rows = Object.entries(a.counts).map(([k, v]) => [k, String(v)]).concat([["Next number", a.next_number]]);
    const box = $("#s-counts");
    if (box) box.replaceChildren(...rows.map(([k, v]) => { const d = el("div"); d.append(el("dt", "", k), el("dd", "", v)); return d; }));
  } catch (e) { /* the records button still works */ }
}

async function sAudit() {
  const box = $("#s-audit");
  if (!box) return;
  const key = `${S.audit}|${S.data.rev}`;
  if (S.auditFor !== key) {
    try {
      S.auditRows = (await api("GET", `/api/cad/settings/audit?section=${encodeURIComponent(S.audit)}`)).audit;
      S.auditFor = key;
    } catch (e) {
      box.replaceChildren(el("div", "error", e.message));
      return;
    }
  }
  const sig = JSON.stringify(S.auditRows || []);
  if (box._sig === sig && box.firstElementChild && box.firstElementChild.matches("table")) return;
  box._sig = sig;
  const t = el("table", "evtable s-audit");
  const head = el("tr");
  for (const h of ["When", "Who", "Section", "What", "Was", "Now"]) head.append(el("th", "", h));
  t.append(el("thead"), el("tbody"));
  t.tHead.append(head);
  for (const r of S.auditRows || []) {
    const tr = el("tr");
    tr.append(el("td", "t", fmtRecv(r.at)), el("td", "", r.operator || "—"), el("td", "", r.section), el("td", "", r.key || ""),
      el("td", "dim", short(r.old)), el("td", "", short(r.new)));
    t.tBodies[0].append(tr);
  }
  if (!(S.auditRows || []).length) {
    const tr = el("tr");
    tr.append(Object.assign(el("td", "empty", "None"), { colSpan: 6 }));
    t.tBodies[0].append(tr);
  }
  box.replaceChildren(t);
}

async function sExport() {
  try {
    const data = await api("GET", "/api/cad/settings/export");
    const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], { type: "application/json" });
    const a = el("a");
    a.href = URL.createObjectURL(blob);
    a.download = `sizeup-cad-settings-${data.exported_at.slice(0, 16).replace(/[-:T]/g, "")}.json`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    sMsg(`Exported ${a.download}`, true);
  } catch (e) {
    sMsg(`Export failed: ${e.message}`);
  }
}

async function sImportFile(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  let data;
  try { data = JSON.parse(await f.text()); } catch (e) { sMsg(`${f.name}: not JSON (${e.message})`); return; }
  try {
    const r = await api("POST", "/api/cad/settings/import", { action: "dry_run", settings: data, operator: operator() });
    S.importing = { name: f.name, data, changes: r.changes, note: r.note, notes: r.notes || [] };
    sShowImport();
  } catch (e) {
    S.importing = null;
    sMsg(`${f.name}: ${e.message}`);
    sShowImport();
  }
}

function sShowImport() {
  const box = $("#s-import");
  if (!box) return;
  const im = S.importing;
  if (!im) { box.replaceChildren(); return; }
  const t = el("table", "evtable");
  const head = el("tr");
  for (const h of ["Section", "What", "Change", "Now", "From the file"]) head.append(el("th", "", h));
  t.append(el("thead"), el("tbody"));
  t.tHead.append(head);
  for (const c of im.changes) {
    const tr = el("tr");
    tr.append(el("td", "", c.section), el("td", "", c.key), el("td", `s-chg ${c.change}`, c.change), el("td", "dim", short(c.old)), el("td", "", short(c.new)));
    t.tBodies[0].append(tr);
  }
  const apply = sBtn(`Apply ${im.changes.length} change${im.changes.length === 1 ? "" : "s"}`, "", async () => {
    const r = await sPost("import", { action: "apply", settings: im.data }, `Imported ${im.name}: ${im.changes.length} changes`);
    if (r) { S.importing = null; sShowImport(); }
  }, "primary danger");
  apply.disabled = !im.changes.length;
  const cancel = sBtn("Cancel", "", () => { S.importing = null; sShowImport(); });
  const row = el("div", "row");
  row.append(el("b", "", `${im.name}: ${im.changes.length ? `${im.changes.length} change${im.changes.length === 1 ? "" : "s"}` : "no changes"}`),
    el("span", "spacer"), cancel, apply);
  box.replaceChildren(row, el("p", "dim small", im.note || ""), ...(im.notes || []).map((n) => el("div", "small warn", n)),
    ...(im.changes.length ? [el("div", "tscroll").appendChild(t).parentNode] : []));
}

// ---- wiring ---------------------------------------------------------------------------------------

(function sInit() {
  const nav = $("#s-sections");
  nav.replaceChildren(...S_SECTIONS.map(([k, t]) => {
    const li = el("li");
    const a = el("a", "tab", t);
    a.href = `#settings/${k}`;
    a.dataset.s = k;
    li.append(a);
    return li;
  }));
  for (const p of document.querySelectorAll(".s-panel")) {
    p.addEventListener("focusout", () => setTimeout(() => sDeferredNow(false), 0));
    p.addEventListener("pointerdown", () => {
      S.pressing = Date.now();
      clearTimeout(S.pressT);          // no pointerup ever comes: what waited for it is drawn all the same
      S.pressT = setTimeout(() => { S.pressing = 0; sDeferredNow(true); }, S_PRESS_MS + 50);
    });
  }
  for (const ev of ["pointerup", "pointercancel"]) {       // (the click comes right after the pointerup)
    window.addEventListener(ev, () => { if (S.pressing) setTimeout(() => { S.pressing = 0; sDeferredNow(true); }, 80); });
  }
  cad.hooks.push(sHook);
  window.addEventListener("hashchange", sRoute);
  sRoute();
})();
