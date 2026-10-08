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
  data: null, loading: false, again: false, deferred: false, msgT: null,
  boardsOpen: null, audit: "", auditRows: null, auditFor: "", admin: null, importing: null,
  previewT: null, previewSeq: 0, tsOpen: new Set(),
  act: null, actEmail: "", actKind: "", actKey: "", actAt: 0,
  station: null, stationBusy: false, stationTest: null,
};
const S_SECTIONS = [["lists", "Lists"], ["runcards", "Run cards"], ["units", "Units"], ["dispatch", "Dispatch"],
  ["users", "Users"], ["system", "System"], ["radios", "Radios"]];
const S_LISTS = [["event_types", "Event types"], ["event_subtypes", "Event subtypes"], ["type_subtypes", "Subtypes per type"],
  ["dispositions", "Dispositions"],
  ["received_via", "Received via"], ["unit_statuses", "Unit statuses"], ["event_class", "Emergency rules"],
  ["geo_town_order", "Town order"]];
const S_ROLES = [["", "Committed (on a call)"], ["free", "Available (in service)"], ["dispatched", "Dispatched (set on dispatch)"],
  ["enroute", "Responding (first en route)"], ["onscene", "On location (first on scene)"], ["oos", "Unavailable (out of service)"]];
const S_RESPONSES = [["fire", "Fire"], ["mutual_aid", "Mutual aid"], ["agency", "Other agency"]];
const S_OP_ROLES = [["admin", "Admin"], ["dispatcher", "Dispatcher"], ["viewer", "Viewer"]];

// ---- small builders -----------------------------------------------------------------------------

function sMsg(text, ok) {
  const m = $("#s-msg");
  m.className = "s-msg " + (ok ? "ok" : "err");
  setText(m, text || "");
  clearTimeout(S.msgT);
  if (ok && text) S.msgT = setTimeout(() => setText(m, ""), 5000);
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
  const c = el("span", "chip s-confirm", role ? "role to confirm" : "meaning to confirm");
  c.title = why || "No name yet: shown as its code";
  return c;
}
function sCard(title, ...kids) {
  const c = el("div", "s-card");
  if (title) c.append(title instanceof Node ? title : el("h3", "", title));
  c.append(...kids.filter(Boolean));
  return c;
}
function sHead(title, about, ...tools) {
  const h = el("div", "s-head");
  h.append(el("h2", "", title), el("span", "spacer"), ...tools.filter(Boolean));
  const out = [h];
  if (about) out.push(el("p", "s-about dim", about));
  return out;
}
function sCardHead(title, info, ...tools) {
  const h = el("div", "s-card-head");
  h.append(el("h3", "", title));
  if (info) h.append(el("span", "dim small", info));
  h.append(el("span", "spacer"), ...tools.filter(Boolean));
  return h;
}
function sAsk(text) {
  return window.confirm(text);
}
function short(v) {
  if (v === null || v === undefined || v === "") return "—";
  const t = typeof v === "string" ? v : JSON.stringify(v);
  return t.length > 140 ? t.slice(0, 137) + "…" : t;
}

// ---- load / save ----------------------------------------------------------------------------------

async function sLoad() {
  if (S.loading) { S.again = true; return; }
  S.loading = true;
  try {
    S.data = await api("GET", "/api/cad/settings");
    sRender();
  } catch (e) {
    sMsg(`Could not load the settings: ${e.message}`);
  } finally {
    S.loading = false;
    if (S.again) { S.again = false; sLoad(); }
  }
}

async function sPost(section, body, done) {
  const op = operator();
  if (!op) {
    sMsg("Enter your name (Operator, top right) first: every settings change records who made it.");
    ($("#operator-pick").hidden ? $("#operator") : $("#operator-pick")).focus();
    sRender(true);
    return null;
  }
  try {
    const r = await api("POST", `/api/cad/settings/${section}`, { ...body, operator: op });
    sMsg(done || "Saved.", true);
    S.auditFor = "";
    await sLoad();
    return r;
  } catch (e) {
    sMsg(e.message);
    sRender(true);
    return null;
  }
}

// ---- routing --------------------------------------------------------------------------------------

function sRoute() {
  const m = location.hash.match(/^#settings(?:\/([a-z]*))?/);
  const open = !!m;
  show($("#settings"), open);
  show(document.querySelector(".cad-main"), !open);
  document.body.classList.toggle("in-settings", open);
  $("#settings-open").classList.toggle("on", open);
  if (!open) {
    S.section = null;
    if (cad.map) setTimeout(() => cad.map.resize(), 0);
    return;
  }
  let sec = m[1];
  if (!S_SECTIONS.some(([k]) => k === sec)) {
    sec = store("sizeup.cad.settings") || "lists";
    if (!S_SECTIONS.some(([k]) => k === sec)) sec = "lists";
    history.replaceState(null, "", `#settings/${sec}`);
  }
  S.section = sec;
  store("sizeup.cad.settings", sec);
  for (const a of document.querySelectorAll("#s-sections a")) a.classList.toggle("on", a.dataset.s === sec);
  for (const p of document.querySelectorAll(".s-panel")) show(p, p.dataset.s === sec);
  sMsg("");
  if (sec === "runcards") { showRunCards(); sRunTools(); }
  if (sec === "radios" && window.RadiosPanel) {        // the radio-ID work's editor (radios-panel.js)
    show($("#s-radios .s-note"), false);
    RadiosPanel.mount($("#settings-radios"), { operator });
  }
  if (S.data) sRender(true);
  sLoad();
}

function sRender(force) {
  if (!S.data || !S.section) return;
  const panel = $(`#s-${S.section}`);
  const a = document.activeElement;
  if (!force && panel.contains(a) && a.matches("input:not([type=file]):not([type=checkbox]), textarea, select")) {
    S.deferred = true;          // don't pull the field out from under someone typing
    return;
  }
  S.deferred = false;
  const fn = { lists: sLists, units: sUnits, dispatch: sDispatch, users: sUsers, system: sSystem }[S.section];
  if (fn) fn(panel);
}

function sHook() {                 // every render (cad.js): keep up with edits made elsewhere
  if (!S.section) return;
  if (S.section === "runcards") sRunTools();
  if (cad.state && S.data && cad.state.rev !== S.data.rev && !S.loading) sLoad();
}

// ---- lists ----------------------------------------------------------------------------------------

function sLists(panel) {
  const tabs = el("div", "s-tabs");
  for (const [k, t] of S_LISTS) {
    const b = sBtn(t, "", () => { S.list = k; store("sizeup.cad.settings.list", k); sRender(true); }, "tab" + (S.list === k ? " on" : ""));
    b.setAttribute("aria-pressed", String(S.list === k));
    tabs.append(b);
  }
  let body;
  if (S.list === "event_class") body = sClassRules();
  else if (S.list === "geo_town_order") body = sTowns();
  else if (S.list === "type_subtypes") body = sTypeMap();
  else body = sListTable(S.list);
  const flow = S.list === "unit_statuses" && S.data.status_flow ? sFlow() : null;
  panel.replaceChildren(...sHead("Lists", "Codes stay as they are: they are on events and in the CSV exports. Names, colors "
    + "and order are yours. Retire what you no longer use: old events keep showing it, the pick lists don't."), tabs, body,
  ...(flow ? [flow] : []));
}

function sListTable(sec) {
  const d = S.data.lists[sec];
  const title = S.data.titles[sec];
  const items = d.items;
  const live = items.filter((x) => x.state !== "deleted");
  const reset = d.edited ? sBtn("Reset to default", "Every item and the order as in the data file",
    () => { if (sAsk(`Put ${title.toLowerCase()} back to the data file's defaults?`)) sPost(sec, { action: "reset_section" }, `${title}: back to the defaults.`); },
    "danger-quiet") : null;
  const ol = el("ol", "s-rows");
  items.forEach((x) => ol.append(sItemRow(sec, x, live.indexOf(x), live.length)));
  const todo = live.filter((x) => x.confirm && !x.retired).length;
  const info = `${live.filter((x) => !x.retired).length} in use` + (live.some((x) => x.retired) ? `, ${live.filter((x) => x.retired).length} retired` : "")
    + (todo ? ` · ${todo} to confirm` : "");
  const about = {
    unit_statuses: "The department's status codes and names. What a status does (its role) is a best guess where marked "
      + "“role to confirm”: change it, or press Confirm when it is right. Available (in service) frees the unit and clears it "
      + "off its call; Unavailable takes it out of service (asks why); Dispatched, Responding and On location stamp the "
      + "times. The rest keep it committed.",
    event_types: "The color is the type's color on the queue, the board and the records.",
  }[sec];
  const partial = (sec === "event_types" || sec === "event_subtypes") ? sSubtypeMap() : null;
  return sCard(sCardHead(title, info, reset), about ? el("p", "dim small", about) : null, partial, ol, sAddForm(sec));
}

// Pointer from the type and subtype lists to the map.
function sSubtypeMap() {
  const t = S.data.type_subtypes;
  if (!t) return null;
  const todo = t.rows.filter((r) => r.confirm && !r.retired).length;
  const p = el("p", "dim small", "Which subtypes each type offers: ");
  const a = sBtn("Subtypes per type", "", () => { S.list = "type_subtypes"; store("sizeup.cad.settings.list", S.list); sRender(true); }, "mini ro-ok");
  p.append(a, document.createTextNode(todo ? ` (${todo} type${todo === 1 ? "" : "s"} to confirm with the department).` : "."));
  return p;
}

// The type -> subtype map (type_subtypes): a row per type, its subtypes ticked. Strict: a type offers only
// what is ticked; none ticked = no subtype. Department-given or proposed (to confirm until confirmed here).
function sTypeMap() {
  const d = S.data.type_subtypes;
  const subs = (L().event_subtypes || []);
  const reset = d.edited ? sBtn("Reset to default", "Every type's subtypes as in the data file", () => {
    if (sAsk("Put every type's subtypes back to the data file's?")) sPost("type_subtypes", { action: "reset_section" }, "Subtypes per type: back to the defaults.");
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
      r.subtypes.length ? r.subtypes.join(", ") : r.listed ? "no subtype" : "not in the map: no subtype");
    li.append(label, el("code", "s-code", r.type), src, sState(r.state));
    if (r.confirm) {
      const c = el("span", "chip s-confirm", "to confirm");
      c.title = r.confirm;
      li.append(c);
      li.append(sBtn("Confirm", `Right as it is: ${r.confirm}`,
        () => sPost("type_subtypes", { action: "set", type: r.type, subtypes: r.subtypes }, `${r.type}: confirmed.`), "mini"));
    }
    li.append(allowed, el("span", "spacer"));
    const tog = sBtn(open ? "Done" : "Edit", "Tick the subtypes this type offers", () => {
      if (open) S.tsOpen.delete(r.type); else S.tsOpen.add(r.type);
      sRender(true);
    });
    tog.setAttribute("aria-expanded", String(open));
    li.append(tog);
    if (r.state === "edited") li.append(sBtn("Reset", "Back to the data file's", () => sPost("type_subtypes", { action: "reset", type: r.type }, `${r.type}: default again.`)));
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
          sPost("type_subtypes", { action: "set", type: r.type, subtypes: pick }, `${r.type}: ${pick.length ? pick.join(", ") : "no subtype"}.`);
        });
        lab.title = st.code + (st.retired ? " (retired)" : "");
        lab.append(cb, el("code", "", st.code), el("span", "dim", st.name + (st.retired ? " (retired)" : "")));
        grid.append(lab);
      }
      li.append(grid);
    }
    ol.append(li);
  }
  const gaps = (d.gaps || []).length ? el("p", "s-note small", "Check: " + d.gaps.join("; ") + ".") : null;
  return sCard(sCardHead("Subtypes per type", `${rows.length} types` + (todo ? ` · ${todo} to confirm` : ""), reset),
    el("p", "dim small", "After the type is picked, only its subtypes are offered (there is no ALS / Bomb); no subtype "
      + "is always allowed. A type with none ticked takes no subtype. Department: from its code sheet (ALARM to MVA). "
      + "Proposed: worked out from the order of its subtype list, or a guess: to confirm with the department (Confirm "
      + "keeps it as it is). Taking a subtype off a type leaves old events as they are."), gaps, ol);
}

function sItemRow(sec, x, i, n) {
  const deleted = x.state === "deleted";
  const li = el("li", "s-row" + (x.retired ? " retired" : "") + (deleted ? " deleted" : ""));
  const ord = el("span", "s-ord");
  const up = sBtn("↑", "Move up", () => sPost(sec, { action: "move", code: x.code, dir: -1 }, `${x.code} moved up.`), "mini");
  const down = sBtn("↓", "Move down", () => sPost(sec, { action: "move", code: x.code, dir: 1 }, `${x.code} moved down.`), "mini");
  up.disabled = deleted || i <= 0;
  down.disabled = deleted || i >= n - 1;
  ord.append(up, down);
  const edit = (item, what) => sPost(sec, { action: "edit", code: x.code, item }, `${x.code}: ${what}.`);
  const name = sInput(x.name, 60, "Name", (v) => { if (v && v !== x.name) edit({ name: v }, "renamed"); }, "s-name");
  name.disabled = deleted;
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
    c.style.setProperty("--c", x.bg || "#9aa0b4");
    li.append(c);
    const flag = x.role === "oos" ? "unavailable" : x.role === "free" ? "available" : "committed";
    li.append(el("span", `chip s-flag ${flag}`, flag));
  }
  li.append(sState(x.state, x.retired));
  if (x.confirm && !deleted) {
    li.append(sConfirm(x.confirm));
    if (!x.retired) li.append(sBtn("Confirm", `Right as it is: ${x.confirm}`, () => edit({}, "confirmed"), "mini"));
  }
  const usedText = sec === "unit_statuses" && x.now ? `${x.now} unit${x.now === 1 ? "" : "s"} now · ` : "";
  li.append(el("span", "dim small s-used", usedText + (x.used ? `used ${x.used}×` : "unused")), el("span", "spacer"));
  if (deleted) {
    li.append(sBtn("Restore", "Bring the default back", () => sPost(sec, { action: "reset", code: x.code }, `${x.code} restored.`)));
    return li;
  }
  li.append(x.retired
    ? sBtn("Restore", "Back on the pick lists", () => sPost(sec, { action: "restore", code: x.code }, `${x.code} restored.`))
    : sBtn("Retire", "Off the pick lists; old events keep showing it", () => sPost(sec, { action: "retire", code: x.code }, `${x.code} retired.`)));
  if (!x.used) {
    li.append(sBtn("Delete", "Only for what was never used", () => {
      if (sAsk(`Delete ${x.name} (${x.code})?`)) sPost(sec, { action: "delete", code: x.code }, `${x.code} deleted.`);
    }, "danger-quiet"));
  }
  if (x.state === "edited") li.append(sBtn("Reset", "Back to the data file's default", () => sPost(sec, { action: "reset", code: x.code }, `${x.code}: default again.`)));
  return li;
}

function sAddForm(sec) {
  const f = el("form", "row s-add");
  f.autocomplete = "off";
  const code = sInput("", sec === "event_types" || sec === "event_subtypes" ? 24 : 40, "Code (new)", null, "s-code-in");
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
  const add = el("button", "primary", "Add");
  add.type = "submit";
  f.append(add);
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const item = { name: name.value.trim() };
    for (const [k, inp] of Object.entries(extra)) item[k] = k === "role" ? inp.value : inp.value.toUpperCase();
    await sPost(sec, { action: "add", code: code.value.trim(), item }, `${code.value.trim()} added.`);
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
        el("span", "spacer"), sBtn("Restore", "", () => sPost("status_flow", { action: "reset", key: r.key }, `${label(r.key)}: default again.`)));
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
        sPost("status_flow", { action: "set", key: r.key, next }, `${label(r.key)}: next steps saved.`);
      });
      lab.append(cb, el("span", "", st.name));
      checks.append(lab);
    }
    li.append(checks, sState(r.state), el("span", "spacer"));
    if (r.key !== "*") li.append(sBtn("Remove", "Use “any other status” for it", () => sPost("status_flow", { action: "remove", key: r.key }, `${label(r.key)}: uses any other.`), "danger-quiet"));
    if (r.state === "edited") li.append(sBtn("Reset", "", () => sPost("status_flow", { action: "reset", key: r.key }, `${label(r.key)}: default again.`)));
    ol.append(li);
  }
  const have = new Set(d.rows.map((r) => r.key));
  const free = statuses.filter((x) => !have.has(x.code));
  const f = el("form", "row s-add");
  if (free.length) {
    const pick = sSelect(free.map((x) => [x.code, x.name]), free[0].code, null, "Status");
    const add = el("button", "primary", "Add next steps");
    add.type = "submit";
    f.append(el("span", "dim small", "Own steps for"), pick, add);
    f.addEventListener("submit", (e) => {
      e.preventDefault();
      const any = (d.rows.find((r) => r.key === "*") || { next: [] }).next.filter((c) => c !== pick.value);
      sPost("status_flow", { action: "set", key: pick.value, next: any.length ? any : [free[0].code] }, `${label(pick.value)}: own next steps.`);
    });
  }
  const reset = d.edited ? sBtn("Reset to default", "", () => {
    if (sAsk("Put the next-step buttons back to the data file's?")) sPost("status_flow", { action: "reset_section" }, "Next steps: back to the defaults.");
  }, "danger-quiet") : null;
  return sCard(sCardHead("Next steps on an incident", "the buttons on a unit in “Units on this incident”", reset),
    el("p", "dim small", "The board on the right always offers every status. Available clears the unit off the event; "
      + "Out of service asks why."), ol, f);
}

function ruleLabel(k) {
  if (k.startsWith("*/")) return `Any type / ${nameOf("event_subtypes", k.slice(2))}`;
  const [t, s] = k.split("/");
  return typeLabel(t, s);
}

function sClassRules() {
  const d = S.data.event_class;
  const reset = d.edited ? sBtn("Reset to default", "", () => {
    if (sAsk("Put the emergency rules back to the data file's?")) sPost("event_class", { action: "reset_section" }, "Emergency rules: back to the defaults.");
  }, "danger-quiet") : null;
  const ol = el("ol", "s-rows");
  for (const r of d.rules) {
    const li = el("li", "s-row" + (r.state === "deleted" ? " deleted" : ""));
    li.append(el("b", "s-rule", ruleLabel(r.key)), el("code", "s-code", r.key));
    if (r.state === "deleted") {
      li.append(el("span", "dim small", `was ${r.default}`), sState("deleted"), el("span", "spacer"),
        sBtn("Restore", "", () => sPost("event_class", { action: "reset", key: r.key }, `${r.key}: default again.`)));
    } else {
      li.append(sSelect([["emergency", "Emergency"], ["non-emergency", "Non-emergency"]], r.value,
        (v) => sPost("event_class", { action: "set", key: r.key, value: v }, `${r.key}: ${v}.`), "Class"),
      sState(r.state), el("span", "spacer"),
      sBtn("Remove", "Back to the next rule (or emergency)", () => sPost("event_class", { action: "remove", key: r.key }, `${r.key} removed.`), "danger-quiet"));
      if (r.state === "edited") li.append(sBtn("Reset", "", () => sPost("event_class", { action: "reset", key: r.key }, `${r.key}: default again.`)));
    }
    ol.append(li);
  }
  const f = el("form", "row s-add");
  const types = [["*", "Any type"], ...(L().event_types || []).filter(live).map((t) => [t.code, t.name])];
  const t = sSelect(types, "", null, "Type");
  const s = sSelect([["", "— no subtype"], ...(L().event_subtypes || []).filter(live).map((x) => [x.code, x.name])], "", null, "Subtype");
  const c = sSelect([["non-emergency", "Non-emergency"], ["emergency", "Emergency"]], "non-emergency", null, "Class");
  const add = el("button", "primary", "Add rule");
  add.type = "submit";
  f.append(t, s, c, add);
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    if (t.value === "*" && !s.value) return sMsg("Any type needs a subtype.");
    const key = s.value ? `${t.value}/${s.value}` : t.value;
    sPost("event_class", { action: "set", key, value: c.value }, `${ruleLabel(key)}: ${c.value}.`);
  });
  return sCard(sCardHead("Emergency or non-emergency", `${d.rules.filter((r) => r.state !== "deleted").length} rules`, reset),
    el("p", "dim small", "Non-emergency events (notifications, closures, hydrants) are logged and tracked, never dispatched. "
      + "The most specific rule wins: type and subtype, then any type with the subtype, then the type. Anything "
      + "not listed is an emergency. The operator can still change it on an event."), ol, f);
}

function sTowns() {
  const d = S.data.geo_town_order;
  const towns = d.towns.slice();
  const save = (list, what) => sPost("geo_town_order", { action: "set", towns: list }, what);
  const ol = el("ol", "s-rows towns");
  towns.forEach((t, i) => {
    const li = el("li", "s-row");
    const up = sBtn("↑", "Move up", () => { const l = towns.slice(); l.splice(i - 1, 0, l.splice(i, 1)[0]); save(l, `${t} moved up.`); }, "mini");
    const down = sBtn("↓", "Move down", () => { const l = towns.slice(); l.splice(i + 1, 0, l.splice(i, 1)[0]); save(l, `${t} moved down.`); }, "mini");
    up.disabled = i === 0;
    down.disabled = i === towns.length - 1;
    const ord = el("span", "s-ord");
    ord.append(up, down);
    li.append(ord, el("b", "", `${i + 1}. ${t}`), el("span", "spacer"),
      sBtn("Remove", "", () => save(towns.filter((x) => x !== t), `${t} removed.`), "danger-quiet"));
    ol.append(li);
  });
  const f = el("form", "row s-add");
  const inp = sInput("", 60, "Town", null, "s-name");
  const add = el("button", "primary", "Add town");
  add.type = "submit";
  f.append(inp, add);
  f.addEventListener("submit", (e) => { e.preventDefault(); if (inp.value.trim()) save([...towns, inp.value.trim()], `${inp.value.trim()} added.`); });
  const reset = d.edited ? sBtn("Reset to default", "", () => sPost("geo_town_order", { action: "reset_section" }, "Town order: back to the defaults."), "danger-quiet") : null;
  return sCard(sCardHead("Town order", "location suggestions", reset),
    el("p", "dim small", "Location suggestions list these towns first, in this order (a town typed in the box always wins)."), ol, f);
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
    kids.push(sBtn("Reset this card", "Back to the data file's default card for this type", async () => {
      const r = await sPost("runcards", { action: "reset", type: editing }, `${editing}: default card again.`);
      if (r) editCard(r);
    }));
  }
  kids.push(sBtn("Reset all to defaults", "Every card as in the data file: edited cards restored, added ones removed", () => {
    if (sAsk("Put every run card back to the defaults? Cards added here are removed.")) {
      sPost("runcards", { action: "reset_section" }, "Run cards: back to the defaults.").then((r) => { if (r) editCard(null); });
    }
  }, "danger-quiet"));
  box.replaceChildren(...kids);
}

// ---- units ----------------------------------------------------------------------------------------

function sUnits(panel) {
  const d = S.data.units;
  const rows = d.rows;
  const kinds = S.data.unit_kinds.map((k) => [k.id, k.label]);
  const boards = S.data.boards.map((b) => [b.id, b.id === "main" ? "Main board" : b.title]);
  const reset = d.edited ? sBtn("Reset all units", "Every unit as in the data file", () => {
    if (sAsk("Put every unit back as in the data file? Units added here go off the boards (their history is kept).")) sPost("units", { action: "reset_section" }, "Units: back to the defaults.");
  }, "danger-quiet") : null;
  const todo = rows.filter((r) => !r.retired && !r.named).length;
  const out = [...sHead("Units", "PCFD's and Rye Brook's fire units are the main board: run cards pick them by kind, "
    + "“tick all” ticks PCFD's, and the dispatch says them. Mutual aid departments and requests go only when named. "
    + "Other agencies (EMS, PD, utilities, resources) are never on a run card, never dispatched or toned out: attach them "
    + "to a call when they show up. Codes are the department's; a unit with no name (said as, title) shows as its code, "
    + `marked “meaning to confirm”${todo ? ` (${todo} now)` : ""}: name it when its meaning is known.`, reset)];
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
      sum.append(el("h3", "", b.title), el("span", "dim small", ` ${mine.length} units`
        + (unnamed ? ` · ${unnamed} to confirm` : "") + " · folded on the board too"));
      det.append(sum, ol);
      out.push(det);
    } else {
      out.push(sCard(sCardHead(b.id === "main" ? "Main board — PCFD and Rye Brook fire units" : b.title,
        `${mine.length} units` + (unnamed ? ` · ${unnamed} to confirm` : "")), ol));
    }
  }
  const retired = rows.filter((r) => r.retired);
  if (retired.length) {
    const ol = el("ol", "s-rows units");
    retired.forEach((u) => ol.append(sUnitRow(u, 0, 0, kinds, boards)));
    out.push(sCard(sCardHead("Retired", "off the boards; their history is kept"), ol));
  }
  const deleted = rows.filter((r) => r.state === "deleted");
  if (deleted.length) {
    const ol = el("ol", "s-rows units");
    for (const u of deleted) {
      const li = el("li", "s-row deleted");
      li.append(el("code", "s-code", u.code), el("span", "", u.spoken || u.code), sState("deleted"), el("span", "spacer"),
        sBtn("Restore", "", () => sPost("units", { action: "reset", code: u.code }, `${u.code} restored.`)));
      ol.append(li);
    }
    out.push(sCard(sCardHead("Deleted", "defaults removed here"), ol));
  }
  out.push(sCard(sCardHead("Add a unit"), sAddUnit(kinds, boards)));
  panel.replaceChildren(...out);
}

function sUnitRow(u, i, n, kinds, boards) {
  const li = el("li", "s-row unit" + (u.retired ? " retired" : ""));
  const edit = (unit, what) => sPost("units", { action: "edit", code: u.code, unit }, `${u.code}: ${what}.`);
  if (!u.retired) {
    const ord = el("span", "s-ord");
    const up = sBtn("↑", "Move up", () => sPost("units", { action: "move", code: u.code, dir: -1 }, `${u.code} moved up.`), "mini");
    const down = sBtn("↓", "Move down", () => sPost("units", { action: "move", code: u.code, dir: 1 }, `${u.code} moved down.`), "mini");
    up.disabled = i === 0;
    down.disabled = i === n - 1;
    ord.append(up, down);
    li.append(ord);
  }
  li.append(el("code", "s-code unit-code", u.code),
    sInput(u.spoken, 40, "Said as", (v) => edit({ spoken: v }, "now said as " + (v || u.code)), "s-said"),
    sInput(u.title, 60, "Title", (v) => edit({ title: v }, "title changed"), "s-title"),
    sSelect(kinds, u.kind, (v) => edit({ kind: v }, `kind ${v}`), "Kind"),
    sInput(u.department, 40, "Department", (v) => edit({ department: v }, "department changed"), "s-dept"));
  const spare = el("label", "inline check small");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = !!u.spare;
  cb.addEventListener("change", () => edit({ spare: cb.checked }, cb.checked ? "a spare" : "not a spare"));
  spare.append(cb, el("span", "", "Spare"));
  const resp = sSelect(S_RESPONSES, u.response, (v) => edit({ response: v }, "answers as " + v.replace("_", " ")), "Answers as");
  resp.title = "Fire: run cards, tick all, dispatch. Mutual aid: only when named. Other agency: attach only, never dispatched.";
  li.append(spare, resp);
  const homes = S.data.units.stations || [];
  if (u.response !== "agency" && homes.length) {             // home firehouse: where the maps draw it (cadmap.js)
    const home = sSelect([["", "Home: not set"], ...homes.map((n) => [n, n])], u.station || "",
      (v) => sPost("units", { action: "station", code: u.code, station: v }, `${u.code}: home station ${v || "not set"}.`), "Home station");
    home.title = "The firehouse it is housed in: the maps draw it there (not set until the department says)";
    li.append(home);
  }
  if (!u.retired) li.append(sSelect(boards, u.board, (v) => edit({ board: v }, "moved to another board"), "Board"));
  const meta = el("span", "s-meta");
  if (u.status) meta.append(statusChip(u.status));
  if (u.incident_number) meta.append(el("span", "warn small", `on ${u.incident_number}`));
  meta.append(sState(u.state, u.retired));
  if (!u.named && !u.retired) meta.append(sConfirm(""));
  meta.append(el("span", "dim small", u.used ? `${u.used} records` : "no history"));
  li.append(meta, el("span", "spacer"));
  if (u.retired) li.append(sBtn("Restore", "Back on its board", () => sPost("units", { action: "restore", code: u.code }, `${u.code} back on the board.`)));
  else li.append(sBtn("Retire", "Off the boards; history kept", () => {
    if (sAsk(`Retire ${u.code}? It goes off the boards; old events keep it.`)) sPost("units", { action: "retire", code: u.code }, `${u.code} retired.`);
  }));
  if (!u.used) li.append(sBtn("Delete", "Only a unit with no history", () => {
    if (sAsk(`Delete ${u.code}?`)) sPost("units", { action: "delete", code: u.code }, `${u.code} deleted.`);
  }, "danger-quiet"));
  if (u.state === "edited") li.append(sBtn("Reset", "As in the data file", () => sPost("units", { action: "reset", code: u.code }, `${u.code}: default again.`)));
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
      department: dept.value.trim(), response: resp.value, board: board.value } }, `${code.value.trim()} added to the board.`);
  });
  return f;
}

// ---- dispatch -------------------------------------------------------------------------------------

function sDispatch(panel) {
  const d = S.data.dispatch;
  const reset = (d.wording.some((w) => w.state === "edited") || d.tones.edited || d.channels.edited || d.voice.edited
    || (d.dept_tones || []).some((t) => t.edited) || (d.lead_in_joint_only || []).some((t) => t.state === "edited")
    || (d.eta && (d.eta.numbers.some((n) => n.edited) || d.eta.order.edited || d.eta.quarters_status.edited)))
    ? sBtn("Reset all dispatch settings", "", () => {
      if (sAsk("Put the wording, tones, channels and arrival estimates back to the defaults?")) sPost("dispatch", { action: "reset_section" }, "Dispatch: back to the defaults.");
    }, "danger-quiet") : null;
  const main = el("div", "s-wording");
  const more = el("details", "s-more");
  more.append(el("summary", "", "More templates (pieces of the add-on and upgrade)"));
  const MAIN = ["new", "add_on", "upgrade", "full_response", "remarks", "lead_in", "lead_in_joint"];
  d.wording.forEach((w) => (MAIN.includes(w.key) ? main : more).append(sTemplate(w)));
  for (const j of d.lead_in_joint_only || []) main.append(sJointOnly(j));
  const legend = el("p", "dim small", "Placeholders: " + d.placeholders.map((p) => `{${p}}`).join(" ")
    + ". Empty parts drop out with their punctuation.");
  const pv = el("div", "s-preview");
  pv.id = "s-preview";
  panel.replaceChildren(...sHead("Dispatch", "What the dispatch says and how it sounds. Changes apply to the next dispatch.", reset),
    sCard(sCardHead("Wording", d.department), legend, main, more),
    sCard(sCardHead("Live preview", "a sample event, rendered as typed"), pv),
    sVoice(d.voice), sTones(d.tones), ...(d.dept_tones || []).map(sDeptTones), sChannels(d.channels),
    d.eta ? sEta(d.eta) : null,
    sCard(sCardHead("Transmit"), el("span", "pill tx-off", d.transmit.enabled ? "Transmit: on" : "Transmit: off"),
      el("p", "dim small", `${d.transmit.note}. Read-only: there is no radio interface in this version.`)));
  sPreview();
}

function sJointOnly(j) {
  const box = el("label", "s-tpl");
  const head = el("span", "row");
  head.append(el("b", "", `Lead-in when only ${j.town}'s units go`), el("code", "dim small", "lead_in_joint_only"), sState(j.state), el("span", "spacer"));
  if (j.state === "edited") head.append(sBtn("Reset", j.default, () => sPost("dispatch", { action: "reset", key: `lead_in_joint_only/${j.town}` }, "Lead-in: default again.")));
  const inp = sInput(j.value, 80, j.default, (v) => { if (v && v !== j.value) sPost("dispatch", { action: "set", key: `lead_in_joint_only/${j.town}`, value: v }, "Lead-in saved."); });
  inp.dataset.town = j.town;
  inp.addEventListener("input", () => { clearTimeout(S.previewT); S.previewT = setTimeout(sPreview, 250); });
  box.append(head, inp);
  return box;
}

// The voice: Kokoro voices to pick from (GET /api/cad/tts/voices), the speed. The engine is the config's.
function sVoice(v) {
  const box = sCard(sCardHead("Voice", v.engine === "kokoro" ? `Kokoro · fallback ${v.fallback || "macOS say"}` : "macOS say"));
  const row = el("div", "row");
  let pick;
  if (v.engine === "kokoro") {
    pick = sSelect(v.voices.map((x) => [x, x]), v.name, (x) => sPost("dispatch", { action: "set", key: "voice", value: x }, `Voice: ${x}.`), "Voice");
    api("GET", "/api/cad/tts/voices").then((r) => {     // readable names: "Heart (American, female)"
      const keep = pick.value;
      const list = (r.voices || []).map((x) => [x.id, `${x.name} (${[x.accent, x.gender].filter(Boolean).join(", ")})`]);
      if (list.length) { fillSelect(pick, list); pick.value = keep; }
    }).catch(() => {});
  } else {
    pick = sInput(v.name, 60, "say voice", (x) => { if (x) sPost("dispatch", { action: "set", key: "voice", value: x }, `Voice: ${x}.`); });
  }
  const speed = el("input", "s-num");
  speed.type = "number"; speed.min = "0.5"; speed.max = "2"; speed.step = "0.01"; speed.value = String(v.speed);
  speed.setAttribute("aria-label", "Speed");
  speed.addEventListener("change", () => sPost("dispatch", { action: "set", key: "speed", value: Number(speed.value) }, `Speed ${speed.value}.`));
  row.append(el("span", "dim small", "Voice"), pick, el("span", "dim small", "Speed"), speed, el("span", "dim small", `read ${v.repeat || 2}×`),
    el("span", "spacer"));
  if (v.edited) row.append(sBtn("Reset", `Config: ${v.default}, ${v.default_speed}`, async () => {
    await sPost("dispatch", { action: "reset", key: "voice" }, "Voice: from the config again.");
    await sPost("dispatch", { action: "reset", key: "speed" }, "Voice and speed: from the config again.");
  }));
  box.append(row, el("p", "dim small", "Kokoro speed: 0.5 to 2 (0.92 is a touch slower than talking). Preview a dispatch on an event to hear it."));
  return box;
}

// A joint-response department's own tones (played after PCFD's). Not set: PCFD's are played for them.
function sDeptTones(t) {
  const box = sCard(sCardHead(`${t.town} tones`, t.value ? (t.edited ? "edited here" : "from the data file") : "not set"));
  const ol = el("ol", "s-rows tones");
  const add = (hz, sec) => {
    const li = el("li", "s-row");
    const h = el("input"); h.type = "number"; h.min = "100"; h.max = "4000"; h.step = "0.1"; h.value = String(hz); h.className = "s-num";
    h.setAttribute("aria-label", "Hz");
    const d = el("input"); d.type = "number"; d.min = "0.1"; d.max = "10"; d.step = "0.1"; d.value = String(sec); d.className = "s-num";
    d.setAttribute("aria-label", "Seconds");
    li.append(h, el("span", "dim", "Hz for"), d, el("span", "dim", "s"), el("span", "spacer"), sBtn("✕", "Remove this tone", () => li.remove(), "mini"));
    ol.append(li);
  };
  (t.value || []).forEach(([hz, sec]) => add(hz, sec));
  const tools = el("div", "row");
  const save = () => sPost("dispatch", { action: "set", key: `tones/${t.town}`,
    value: [...ol.querySelectorAll("li")].map((li) => [...li.querySelectorAll("input")].map((i) => Number(i.value))) }, `${t.town} tones saved.`);
  if (!t.value) {
    box.append(el("p", "dim small", `Not set: ${t.town}'s tones aren't known yet, so PCFD's are played when their units go.`));
    tools.append(sBtn("Set tones", "", () => { add(1000, 1); tools.replaceChildren(sBtn("+ Tone", "", () => add(1000, 1), "mini"), el("span", "spacer"), sBtn("Save tones", "", save, "primary")); }));
  } else {
    tools.append(sBtn("+ Tone", "", () => add(1000, 1), "mini"), el("span", "spacer"),
      sBtn("Not set", "Forget them: PCFD's are played", () => sPost("dispatch", { action: "set", key: `tones/${t.town}`, value: null }, `${t.town} tones: not set.`), "danger-quiet"),
      sBtn("Save tones", "", save, "primary"));
  }
  if (t.edited) tools.append(sBtn("Reset", "As in the data file", () => sPost("dispatch", { action: "reset", key: `tones/${t.town}` }, `${t.town} tones: default again.`)));
  box.append(ol, tools);
  return box;
}

function sTemplate(w) {
  const box = el("label", "s-tpl");
  const head = el("span", "row");
  head.append(el("b", "", w.label), el("code", "dim small", w.key), sState(w.state), el("span", "spacer"));
  if (w.state === "edited") head.append(sBtn("Reset", w.default, () => sPost("dispatch", { action: "reset", key: w.key }, `${w.label}: default again.`)));
  const ta = el("textarea");
  ta.rows = ["new", "new_no_location", "add_on", "upgrade"].includes(w.key) ? 2 : 1;
  ta.maxLength = 400;
  ta.value = w.value;
  ta.dataset.key = w.key;
  ta.spellcheck = false;
  ta.addEventListener("input", () => { clearTimeout(S.previewT); S.previewT = setTimeout(sPreview, 250); });
  ta.addEventListener("change", () => { if (ta.value.trim() !== w.value) sPost("dispatch", { action: "set", key: w.key, value: ta.value.trim() }, `${w.label}: saved.`); });
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
    const rows = [["new", "First dispatch, with remarks"], ["add_on", "Added to the assignment"], ["upgrade", "Upgrade"],
      ["full", "Full department response"], ["joint", "With a joint department's units"], ["joint_only", "Only the joint department's units"]];
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
    li.append(h, el("span", "dim", "Hz for"), d, el("span", "dim", "s"), el("span", "spacer"), sBtn("✕", "Remove this tone", () => li.remove(), "mini"));
    ol.append(li);
  };
  const addGroup = (tones) => {
    const g = el("div", "s-tone-group");
    const ol = el("ol", "s-rows tones");
    const head = el("div", "row");
    head.append(el("b", "small", "Tone group"), el("span", "dim small", "one department's tones"), el("span", "spacer"),
      sBtn("+ Tone", "", () => addTone(ol, 1000, 1), "mini"), sBtn("✕ Group", "Remove this group", () => g.remove(), "mini"));
    g.append(head, ol);
    tones.forEach(([hz, sec]) => addTone(ol, hz, sec));
    groups.append(g);
  };
  t.value.forEach(addGroup);
  const save = () => {
    const v = [...groups.querySelectorAll(".s-tone-group")].map((g) =>
      [...g.querySelectorAll("li")].map((li) => [...li.querySelectorAll("input")].map((i) => Number(i.value))))
      .filter((g) => g.length);
    sPost("dispatch", { action: "set", key: "tones", value: v }, "Tones saved.");
  };
  const tools = el("div", "row");
  tools.append(sBtn("+ Group", "Another department's tones, played after", () => addGroup([[1000, 1]])), el("span", "spacer"),
    t.edited ? sBtn("Reset", "From the config", () => sPost("dispatch", { action: "reset", key: "tones" }, "Tones: from the config again.")) : null,
    sBtn("Save tones", "", save, "primary"));
  [...tools.childNodes].forEach((n) => { if (n.nodeType === 3) n.remove(); });
  return sCard(sCardHead("Port Chester Fire tones", t.edited ? "edited here" : "from the config"),
    el("p", "dim small", "Played before the voice: each group in order (one per department paged, a short gap between). "
      + "50 to 4000 Hz, up to 10 s a tone, 20 s a group."), groups, tools);
}

// Nearest first (sizeup/cad/eta.py): the recommendation's order and the numbers behind its estimates.
function sEta(e) {
  const rows = el("div", "s-eta");
  const order = sSelect([["nearest", "Nearest first (by estimated arrival)"], ["card", "Run card / board order"]], e.order.value,
    (v) => sPost("dispatch", { action: "set", key: "order", value: v }, v === "nearest" ? "Recommend: nearest first." : "Recommend: run card order."),
    "Recommend");
  const line = (label, ctl, item, key, shown) => {
    const r = el("div", "row");
    r.append(el("span", "s-eta-l", label), ctl, sState(item.edited ? "edited" : "default"), el("span", "spacer"));
    if (item.edited) r.append(sBtn("Reset", `Default: ${shown}`, () => sPost("dispatch", { action: "reset", key }, `${label}: default again.`)));
    return r;
  };
  rows.append(line("Recommend", order, e.order, "order", e.order.default === "nearest" ? "nearest first" : "run card order"));
  for (const n of e.numbers) {
    const i = el("input", "s-num");
    i.type = "number"; i.min = String(n.min); i.max = String(n.max); i.step = n.key === "road_factor" ? "0.05" : "0.5";
    i.value = String(n.value);
    i.setAttribute("aria-label", n.label);
    i.addEventListener("change", () => sPost("dispatch", { action: "set", key: n.key, value: Number(i.value) }, `${n.label}: ${i.value} ${n.unit}.`));
    const ctl = el("span", "row");
    ctl.append(i, el("span", "dim small", n.unit));
    rows.append(line(n.label, ctl, n, n.key, `${n.default} ${n.unit}`));
  }
  const q = e.quarters_status;
  const qs = sSelect([["", "None"], ...q.choices.map((c) => [c.code, `${c.code} · ${c.name}`])], q.value,
    (v) => sPost("dispatch", { action: "set", key: "quarters_status", value: v }, `Back in quarters: ${v || "none"}.`), "Back in quarters status");
  rows.append(line("“Back in quarters” status (ends a return)", qs, q, "quarters_status", q.default || "none"));
  return sCard(sCardHead("Arrival estimates", e.order.value === "nearest" ? "recommend nearest first" : "recommend in run card order"),
    el("p", "dim small", `${e.note} A rig that cleared a call is taken to be driving home for the drive back plus the return margin `
      + "(no turnout from there); the “back in quarters” status puts it home at once. Rigs with no home station come after those with one."),
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
    c.edited ? sBtn("Reset", `Config: ${c.default.join(", ")}`, () => sPost("dispatch", { action: "reset", key: "channels" }, "Channels: from the config again.")) : null,
    sBtn("Save channels", "", () => sPost("dispatch", { action: "set", key: "channels",
      value: [...rows.querySelectorAll("input")].map((i) => i.value.trim()).filter(Boolean) }, "Channels saved."), "primary"));
  [...tools.childNodes].forEach((n) => { if (n.nodeType === 3) n.remove(); });
  return sCard(sCardHead("Channels", c.edited ? "edited here" : "from the config"),
    el("p", "dim small", "The labels the dispatch box offers (logged with each dispatch; nothing transmits)."), rows, tools);
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
        (v) => { if (v && v !== o.name) sPost("users", { action: "edit", id: o.id, name: v }, `${who} is now ${v}.`); }, "s-name"),
      sInput(o.email, 254, "Sign-in email", (v) => {
        if (v.toLowerCase() !== o.email) sPost("users", { action: "edit", id: o.id, email: v }, v ? `${who}: signs in as ${v}.` : `${who}: no sign-in email.`);
      }, "s-email"),
      sInput(o.callsign, 20, "Callsign", (v) => {
        if (v !== o.callsign) sPost("users", { action: "edit", id: o.id, callsign: v }, `${who}: callsign ${v || "removed"}.`);
      }, "s-callsign"),
      sSelect(S_OP_ROLES, o.role, (v) => sPost("users", { action: "edit", id: o.id, role: v }, `${who}: ${v}.`), "Role"),
      unnamed ? el("span", "chip s-state pending", "not named yet") : null,
      el("span", `chip s-state ${o.active ? "added" : "retired"}`, o.active ? "active" : "inactive"),
      cad.who && o.email ? Object.assign(el("span", "s-seen dim small", sSeenText(o.email)), { title: sSeenTip(o.email) }) : null,
      el("span", "spacer"),
      cad.who && o.email && (sActAll() || sActMine(o.email)) ? sBtn("Activity", `What ${who} signed in, did and was refused`, () => {
        S.actEmail = o.email;
        sActivity(true);
        $("#s-activity").scrollIntoView({ block: "start", behavior: "smooth" });
      }, "ro-ok") : null,
      o.active ? sBtn("Deactivate", "Off the operator picker; their name stays on what they did",
        () => sPost("users", { action: "deactivate", id: o.id }, `${who} deactivated.`))
        : sBtn("Activate", "", () => sPost("users", { action: "activate", id: o.id }, `${who} active again.`)));
    [...li.childNodes].forEach((n) => { if (n.nodeType === 3) n.remove(); });   // the nulls above
    for (const sp of li.querySelectorAll(".s-seen")) sp.dataset.email = o.email;
    ol.append(li);
  }
  if (!ops.length) ol.append(el("li", "empty", "No operators yet: the Operator field (top right) is free text until there is one."));
  const f = el("form", "row s-add");
  const name = sInput("", 60, "Name", null, "s-name");
  const email = sInput("", 254, "Sign-in email (optional)", null, "s-email");
  const callsign = sInput("", 20, "Callsign", null, "s-callsign");
  const role = sSelect(S_OP_ROLES, "dispatcher", null, "Role");
  const add = el("button", "primary", "Add operator");
  add.type = "submit";
  f.append(name, email, callsign, role, add);
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    sPost("users", { action: "add", name: name.value.trim(), email: email.value.trim(), callsign: callsign.value.trim(),
      role: role.value }, `${name.value.trim()} added.`);
  });
  const signedIn = cad.who
    ? `You are signed in with Cloudflare Access as ${cad.who.email}: that is the operator on everything you do here, `
      + "named from this list. Someone who signs in for the first time is added under their email, as a dispatcher: give them a name. "
    : "Behind Cloudflare Access (the hub), the email someone signs in with picks their row here and they become the "
      + "operator automatically; here, anyone at the console picks a name. ";
  const roles = cad.who && cad.who.enforced
    ? "Roles are enforced: an admin changes anything, a dispatcher runs calls but can’t change Settings, a viewer (or "
      + "someone deactivated) only looks. Until there is an active admin with a sign-in email, anyone signed in can change "
      + "everything; after that the last admin can’t be demoted or deactivated."
    : "The role is recorded here, not enforced: anyone at the console can change anything. On the hub, with Cloudflare "
      + "Access, it is: an admin changes anything, a dispatcher runs calls, a viewer only looks.";
  panel.replaceChildren(...sHead("Users", "The people at the console. Once there is one, the Operator field becomes a picker of the active ones."),
    el("div", "s-note", signedIn + roles),
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
const S_ACT_KINDS = [["", "Everything"], ["signin", "Sign-ins"], ["action", "Actions"], ["refused", "Refused"]];
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
  return v && v.last_seen ? `last seen ${sAgo(v.last_seen)}` + (v.last_ip ? ` · ${v.last_ip}` : "") : "";
}
function sSeenTip(email) {
  const v = sSeen(email);
  return v && v.last_seen ? `${fmtRecv(v.last_seen)}${v.last_ua ? ` · ${v.last_ua}` : ""}` : "";
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
  const title = !email ? "Activity · all users" : email === me ? "My activity" : `Activity · ${op ? op.name : email}`;
  const kind = sSelect(S_ACT_KINDS, S.actKind, (v) => { S.actKind = v; sActivity(true); }, "Show");
  kind.classList.add("ro-ok");
  const tools = [kind];
  if (all && email) tools.push(sBtn("All users", "Everyone's activity", () => { S.actEmail = ""; sActivity(true); }, "ro-ok"));
  if (all && email !== me) tools.push(sBtn("Mine", "Your own activity", () => { S.actEmail = me; sActivity(true); }, "ro-ok"));
  tools.push(sBtn("Refresh", "", () => sActivity(true), "ro-ok"));
  if (all) {
    const a = el("a", "nav ro-ok", "CSV");
    a.href = "/api/cad/export/activity.csv?" + new URLSearchParams({ email, kind: S.actKind });
    a.download = "";
    a.title = "This list as a spreadsheet (everything kept: 90 days)";
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
    li.append(el("span", `chip s-kind ${r.kind}`, { signin: "sign-in", action: "action", refused: "refused" }[r.kind] || r.kind));
    li.append(Object.assign(el("span", "s-act-sum", r.summary), { title: r.route }));
    li.append(Object.assign(el("span", "s-act-ip dim", r.ip || "—"), { title: r.ua || "" }));
    ol.append(li);
  }
  if (!rows.length) ol.append(el("li", "empty", fresh ? "Nothing here yet." : "Loading…"));
  const n = fresh ? `${rows.length}${rows.length >= S_ACT_LIMIT ? "+" : ""} newest first` : "";
  card.replaceChildren(sCardHead(title, n, ...tools),
    el("p", "dim small", (email ? "Each" : "Everyone signed in: each") + " sign-in (once per login), every change and every "
      + "refusal, with the address and browser it came from. Kept 90 days." + (all ? "" : " An admin sees everyone's.")),
    ol);
}

// ---- system ---------------------------------------------------------------------------------------

function sSystem(panel) {
  const sy = S.data.system;
  const recs = el("div", "row csv-row");
  recs.append(sBtn("Open records", "Every numbered event; click one to open it", () => {
    openOverlay("#db-overlay", true); cad.dbAt = 0; renderRecords();
  }, "primary ro-ok"), el("span", "dim small", "CSV, the sheet's columns:"));
  for (const [f, t] of [["incidents", "Incident database"], ["calls", "Call history"], ["unit-history", "Unit history"], ["event-log", "Event log"]]) {
    const a = el("a", "nav", t);
    a.href = `/api/cad/export/${f}.csv`;
    a.download = "";
    recs.append(a);
  }
  const counts = el("dl", "admin");
  counts.id = "s-counts";
  const file = el("dl", "admin");
  const edits = Object.entries(sy.overrides).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ") || "none";
  for (const [k, v] of [["Data file (defaults)", sy.data_file], ["Defaults version", sy.defaults_version || "—"],
    ["Content", `sha-256 ${sy.sha256}`], ["Database", sy.db], ["Schema", `v${sy.schema}`], ["Your edits", edits]]) {
    const d = el("div");
    d.append(el("dt", "", k), el("dd", "", v));
    file.append(d);
  }
  const exp = sBtn("Export settings", "Every edit, run card and operator as one JSON file", sExport, "primary ro-ok");
  const fileIn = el("input");
  fileIn.type = "file";
  fileIn.accept = ".json,application/json";
  fileIn.setAttribute("aria-label", "Settings file to import");
  fileIn.addEventListener("change", () => sImportFile(fileIn));
  const imp = el("div", "s-import");
  imp.id = "s-import";
  const backup = sCard(sCardHead("Backup and copy"), el("p", "dim small", "Export: a backup, or to set up another machine the same. "
    + "Import shows what would change first; nothing changes until you apply it."), (() => { const r = el("div", "row"); r.append(exp, el("span", "dim small", "Import:"), fileIn); return r; })(), imp);
  const sel = sSelect([["", "All sections"], ...S.data.audit_sections.map((s) => [s, s.charAt(0).toUpperCase() + s.slice(1)])], S.audit,
    (v) => { S.audit = v; S.auditFor = ""; sAudit(); }, "Section");
  sel.classList.add("ro-ok");
  const audit = el("div", "tscroll");
  audit.id = "s-audit";
  const station = sCard(null);
  station.id = "s-station";
  station.hidden = true;
  panel.replaceChildren(...sHead("System"), station,
    sCard(sCardHead("Records", "moved here from the header"), recs, counts, el("p", "dim small", "There is no “clear all data” here on purpose.")),
    sCard(sCardHead("Data file"), file),
    backup,
    sCard(sCardHead("Audit log", "who changed what in Settings", sel), audit));
  if (S.importing) sShowImport();
  sCounts();
  sAudit();
  sStation();
}

// ---- system: Station (forwarding to the hub, sizeup/cloud/stationmode.py) ---------------------------
// This machine's own switch: on, what it hears also goes to the hub (the CAD of record then); off,
// all-in-one. Radios keep running either way. Not on the hub (409: hidden). Secrets are shown only
// as where they came from.

const SM_SECRETS = [["token", "Token"], ["access_id", "Access id"], ["access_secret", "Access secret"]];

async function sStation() {
  const card = $("#s-station");
  if (!card) return;
  let r = null, d = null;
  try {
    r = await fetch("/api/station-mode", { cache: "no-store" });
    d = await r.json();
  } catch (e) { /* shown below */ }
  if (r && (r.status === 409 || r.status === 404)) { card.hidden = true; return; }   // the hub, or no switch here
  card.hidden = false;
  if (!r || !r.ok || !d) {
    card.replaceChildren(sCardHead("Station"), el("div", "error", (d && d.error) || "Could not load the station switch"));
    delete card.dataset.built;
    return;
  }
  S.station = d;
  if (!card.dataset.built) sStationBuild(card);
  sStationFill(card, d);
}

function sStationBuild(card) {
  const sw = el("button", "sw sm-sw");
  sw.type = "button";
  sw.setAttribute("role", "switch");
  sw.append(el("i", "knob"), el("span", "lbl"));
  sw.title = "On: what this station hears also goes to the hub, and the hub is the CAD of record. "
    + "Off: all-in-one. Radios, speech-to-text and the voice keep running either way; nothing queued is lost.";
  sw.addEventListener("click", () => sStationSet({ forward: !S.station.forward }));
  const status = el("span", "sm-status");
  const url = sInput("", 300, "https://hub.example.org", null, "sm-url ro-ok");
  url.type = "url";
  url.setAttribute("aria-label", "Hub URL");
  url.title = "The hub's address: https:// (plain http only to this machine)";
  const save = sBtn("Save", "Use this hub URL", () => sStationSet({ hub_url: url.value.trim() }));
  save.classList.add("sm-save");
  url.addEventListener("input", () => { save.disabled = url.value.trim() === (S.station.hub_url || ""); });
  url.addEventListener("keydown", (e) => { if (e.key === "Enter" && !save.disabled) { e.preventDefault(); save.click(); } });
  const test = sBtn("Test connection", "The hub's /healthz, then its station path with this station's token "
    + "and Access headers. Nothing is sent.", sStationTest, "ro-ok sm-test");
  const result = el("span", "sm-result small");
  const row1 = el("div", "row sm-row");
  row1.append(sw, status);
  const row2 = el("div", "row sm-row");
  row2.append(el("label", "dim small sm-lab", "Hub URL"), url, save, test, result);
  const secrets = el("dl", "admin sm-secrets");
  const hint = el("p", "dim small sm-hint");
  card.replaceChildren(sCardHead("Station", "forward to the hub"), row1, row2, secrets, hint);
  card.dataset.built = "1";
}

function sStationFill(card, d) {
  const sw = card.querySelector(".sm-sw");
  sw.className = "sw sm-sw" + (d.forward ? " on" : "");
  sw.setAttribute("aria-checked", String(!!d.forward));
  sw.disabled = S.stationBusy;
  setText(sw.lastChild, "Forward to hub");
  const st = d.status || {};
  const status = card.querySelector(".sm-status");
  status.className = "sm-status " + ({ ok: "ok", down: "err", blocked: "err", starting: "warn" }[st.state] || "dim");
  setText(status, st.text || "");
  status.title = [st.detail, st.last_error && `Last error: ${st.last_error}`].filter(Boolean).join("\n");
  const url = card.querySelector(".sm-url");
  if (document.activeElement !== url && url.dataset.saved !== (d.hub_url || "")) {
    url.value = d.hub_url || "";
    url.dataset.saved = d.hub_url || "";
  }
  card.querySelector(".sm-save").disabled = S.stationBusy || url.value.trim() === (d.hub_url || "");
  card.querySelector(".sm-test").disabled = S.stationBusy;
  const res = card.querySelector(".sm-result");
  const t = S.stationTest;
  res.className = "sm-result small " + (t ? (t.busy ? "dim" : t.ok ? "ok" : "err") : "");
  setText(res, t ? (t.busy ? "Testing…" : (!t.ok && t.status ? `HTTP ${t.status}: ` : "") + t.text) : "");
  const sec = d.secrets || {};
  const rows = SM_SECRETS.map(([k, label]) => {
    const x = sec[k] || {};
    const v = x.set ? `set (${x.source})` : x.short ? `too short (${x.source})` : k === "token" ? "missing" : "not set";
    const dd = el("dd", x.set ? "" : k === "token" ? "err" : "dim", v);
    dd.title = (x.set ? `From ${x.source === "env" ? x.env : x.source === "Keychain" ? `the Keychain (${x.keychain})` : "config.toml"}`
      : `Env ${x.env}` + (d.keychain ? `, or Keychain service ${x.keychain}` : "")) + (k === "token" ? "" : ". Only needed with a Cloudflare Access service token.");
    const div = el("div");
    div.append(el("dt", "", label), dd);
    return div;
  });
  card.querySelector(".sm-secrets").replaceChildren(...rows);
  const hint = card.querySelector(".sm-hint");
  const tok = sec.token || {};
  hint.replaceChildren();
  if (!tok.set) {
    if (d.keychain) {
      hint.append("Add the token to the Keychain: ",
        el("code", "", `security add-generic-password -a sizeup -s ${tok.keychain || "sizeup-station-token"} -w`), ", then Test connection.");
    } else {
      hint.append("No Keychain on this system: set ", el("code", "", tok.env || "SIZEUP_STATION_TOKEN"), " and restart.");
    }
  } else if (d.from === "config") {
    hint.append(`From config.toml (${d.config_role}) until changed here.`);
  }
  show(hint, hint.childNodes.length > 0);
}

async function sStationSet(body) {
  const op = operator();
  if (!op) {
    sMsg("Enter your name (Operator, top right) first: every settings change records who made it.");
    ($("#operator-pick").hidden ? $("#operator") : $("#operator-pick")).focus();
    return;
  }
  if (body.forward === true && !$("#s-station .sm-url").value.trim() && !S.station.hub_url) {
    sMsg("Enter the hub URL first.");
    $("#s-station .sm-url").focus();
    return;
  }
  if (body.forward === true) {
    const typed = $("#s-station .sm-url").value.trim();
    if (typed) body.hub_url = typed;
  }
  if (body.forward === false && !sAsk("Stop forwarding to the hub? This console becomes the CAD of record again; "
    + "anything still queued waits here.")) return;
  S.stationBusy = true;
  sStationFill($("#s-station"), S.station);
  try {
    const r = await fetch("/api/station-mode", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, operator: op }) });
    const d = await r.json().catch(() => null);
    if (!r.ok) throw new Error((d && d.error) || `${r.status} ${r.statusText}`);
    S.station = d;
    S.auditFor = "";
    sMsg("forward" in body ? (d.forward ? "Forwarding to the hub: on." : "Forwarding to the hub: off.") : "Hub URL saved.", true);
  } catch (e) {
    sMsg(e.message);
  } finally {
    S.stationBusy = false;
    const card = $("#s-station");
    if (card && S.station) sStationFill(card, S.station);
    sAudit();
  }
}

async function sStationTest() {
  const url = $("#s-station .sm-url").value.trim();
  S.stationTest = { busy: true };
  sStationFill($("#s-station"), S.station);
  try {
    const r = await fetch("/api/station-mode/test", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(url ? { hub_url: url } : {}) });
    const d = await r.json().catch(() => null);
    S.stationTest = r.ok && d ? d : { ok: false, status: r.status, text: (d && d.error) || r.statusText };
  } catch (e) {
    S.stationTest = { ok: false, status: 0, text: e.message };
  }
  sStation();
}

setInterval(() => {           // the status line (Connected / Offline since ...) while System is open
  if (S.section === "system" && !document.hidden && !S.stationBusy) sStation();
}, 5000);

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
    tr.append(Object.assign(el("td", "empty", "No changes yet"), { colSpan: 6 }));
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
    sMsg(`Exported ${a.download}.`, true);
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
    S.importing = { name: f.name, data, changes: r.changes, note: r.note };
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
    const r = await sPost("import", { action: "apply", settings: im.data }, `Imported ${im.name}: ${im.changes.length} changes.`);
    if (r) { S.importing = null; sShowImport(); }
  }, "primary danger");
  apply.disabled = !im.changes.length;
  const cancel = sBtn("Cancel", "", () => { S.importing = null; sShowImport(); });
  const row = el("div", "row");
  row.append(el("b", "", `${im.name}: ${im.changes.length ? `${im.changes.length} change${im.changes.length === 1 ? "" : "s"}` : "nothing would change"}`),
    el("span", "spacer"), cancel, apply);
  box.replaceChildren(row, el("p", "dim small", im.note || ""), ...(im.changes.length ? [el("div", "tscroll").appendChild(t).parentNode] : []));
}

// ---- wiring ---------------------------------------------------------------------------------------

(function sInit() {
  const nav = $("#s-sections");
  nav.replaceChildren(...S_SECTIONS.map(([k, t]) => {
    const li = el("li");
    const a = el("a", "", t);
    a.href = `#settings/${k}`;
    a.dataset.s = k;
    li.append(a);
    return li;
  }));
  for (const p of document.querySelectorAll(".s-panel")) {
    p.addEventListener("focusout", () => setTimeout(() => {
      if (S.deferred && !p.contains(document.activeElement)) sRender();
    }, 0));
  }
  cad.hooks.push(sHook);
  window.addEventListener("hashchange", sRoute);
  sRoute();
})();
