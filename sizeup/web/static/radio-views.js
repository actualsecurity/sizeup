// The Radio rail's views (Our calls, Police, ...), set in the app (sizeup/cad/radioviews.py), not in code.
//
//   Department   the admins' set for everyone (GET /api/cad/comms/sources `views`; POST /api/cad/settings/radio_views,
//                audited). None set: the views that were coded (SEED), so nothing disappears.
//   Personal     each person's own views ("p:..."), and which views they hide and in what order: MePrefs namespace
//                "radio_views" (the hub's copy wins; this browser's in localStorage KEY). Every view hidden: no VIEWS.
//   A filter     ours, services, systems, sources (channels, added), units, linked: any within a dimension, all across.
//                resolve() turns it into the hub's query: the old fast paths where they are exact (ours, one service,
//                everything), else the sources it names (from the tree) as ?source=.
//   Editing      editor(box, {mode}): "mine" (the rail's ✎: show / hide, order, own views; admins also the
//                department's), "dept" (Settings > Radios > Views: the department's set and its order).
//
// Uses cad.js's el, api, operator, canDo (when there); loaded before radio-nav.js. Tested in node
// (tests/test_radio_views.py) and in the browser (tests/test_radio_views_ui.py).
"use strict";

window.RadioViews = (() => {
  const KEY = "sizeup.cad.radio.views";
  const SERVICES = ["fire", "police", "ems", "transit", "air", "other"];
  const SERVICE = { fire: "Fire", police: "Police", ems: "EMS", transit: "Transit", air: "Air", other: "Other" };
  const SERVICE_VIEWS = ["police", "ems", "transit", "air"];       // (sizeup/cad/comms.py SERVICE_VIEWS)
  const SOURCES_MAX = 200;                                         // (sizeup/cad/comms.py SOURCES_MAX)
  const NAME_MAX = 24, MAX_MINE = 20;
  const MARK_RE = /^(?:[A-Z0-9]|∗)$/;
  const COLOR_RE = /^#[0-9a-f]{6}$/i;
  const SEED = [
    { id: "ours", name: "Our calls", mark: "F", color: "", filter: { ours: true } },
    { id: "police", name: "Police", mark: "P", color: "", filter: { services: ["police"] } },
    { id: "ems", name: "EMS", mark: "E", color: "", filter: { services: ["ems"] } },
    { id: "transit", name: "Transit", mark: "T", color: "", filter: { services: ["transit"] } },
    { id: "air", name: "Air", mark: "A", color: "", filter: { services: ["air"] } },
    { id: "all", name: "All", mark: "∗", color: "", filter: {} },
  ];
  const copy = (v) => JSON.parse(JSON.stringify(v));
  const len = (x) => (Array.isArray(x) ? x.length : 0);

  // ---- pure ------------------------------------------------------------------------------------------------------

  // the filter names channels (else: every source)
  const dims = (f) => !!(f && (f.ours || len(f.services) || len(f.systems) || len(f.sources)));
  // a source of the tree ({key, system, service, ours}) is in a view's filter (units and linked: rows, not sources)
  function matches(s, f) {
    if (!dims(f)) return true;
    if (f.ours && s.ours) return true;
    if (len(f.sources) && f.sources.includes(s.key)) return true;
    const sv = f.services || [], sy = f.systems || [];
    if (!sv.length && !sy.length) return false;
    return (!sv.length || sv.includes(s.service)) && (!sy.length || sy.includes(s.system));
  }
  // the badge's service (its shape and colour): Our calls fire, one service that one, else none
  function service(v) {
    const f = (v && v.filter) || {};
    if (f.ours && !len(f.services)) return "fire";
    return !f.ours && len(f.services) === 1 ? f.services[0] : "";
  }
  // shown on the rail: one that was coded only while a source of it is in the tree (no EMS source: no EMS view), as
  // before; Our calls, a view of everything and every view made here always
  const coded = (id) => SEED.some((s) => s.id === id);
  const shows = (v, rows) => v.id === "ours" || !dims(v.filter) || !coded(v.id) || rows.some((s) => matches(s, v.filter));
  // The hub's query for a view: {view, service?, system?, source?, unit?, linked?, all?}, null: nothing can match.
  // sources: the tree (what services and systems resolve to)
  function resolve(v, sources = []) {
    const f = (v && v.filter) || {}, out = {};
    if (len(f.units)) out.unit = f.units.join(",");
    if (f.linked) out.linked = "1";
    const sv = f.services || [], sy = f.systems || [], src = f.sources || [];
    if (!dims(f)) return { view: "all", ...out, all: true };
    if (f.ours && !sv.length && !sy.length && !src.length) return { view: "ours", ...out };
    if (!f.ours && !src.length && !sy.length && sv.length === 1 && SERVICE_VIEWS.includes(sv[0])) return { view: sv[0], ...out };
    if (!f.ours && !src.length && sv.length <= 1 && sy.length <= 1) {
      return { view: "all", ...(sv.length ? { service: sv[0] } : {}), ...(sy.length ? { system: sy[0] } : {}), ...out };
    }
    const rest = { ...f, sources: [] };
    const keys = [...new Set([...src, ...sources.filter((s) => s && s.key && matches(s, rest)).map((s) => s.key)])];
    if (!keys.length) return null;
    return { view: "all", source: keys.slice(0, SOURCES_MAX).join(","), ...out };
  }
  // a person's own as kept: {hidden, order, mine}
  function prefsOf(v) {
    v = v && typeof v === "object" && !Array.isArray(v) ? v : {};
    const ids = (x) => (Array.isArray(x) ? [...new Set(x.filter((i) => typeof i === "string"))] : []);
    const mine = (Array.isArray(v.mine) ? v.mine : []).filter((x) => x && typeof x.id === "string" && x.id.startsWith("p:"))
      .slice(0, MAX_MINE).map((x) => ({ id: x.id, name: String(x.name || ""), mark: String(x.mark || "·"), color: x.color || "",
                                         filter: x.filter && typeof x.filter === "object" ? x.filter : {} }));
    return { hidden: ids(v.hidden), order: ids(v.order), mine };
  }
  // Every view in this person's order: the department's then their own (scope dept | mine), hidden marked. Their order
  // first; a view not in it (new since) after, in its own place
  function all(shared, p) {
    const out = [...shared.map((v) => ({ ...v, scope: "dept" })), ...p.mine.map((v) => ({ ...v, scope: "mine" }))];
    const rank = new Map(out.map((v, i) => [v.id, p.order.includes(v.id) ? p.order.indexOf(v.id) : p.order.length + i]));
    return out.sort((a, b) => rank.get(a.id) - rank.get(b.id)).map((v) => ({ ...v, hidden: p.hidden.includes(v.id) }));
  }
  const list = (shared, p) => all(shared, p).filter((v) => !v.hidden);
  // a view one up / down in this person's order (dir -1 / +1): the order of every view, written whole
  function moved(ids, id, dir) {
    const i = ids.indexOf(id), j = i + dir;
    if (i < 0 || j < 0 || j >= ids.length) return ids;
    const out = ids.slice();
    [out[i], out[j]] = [out[j], out[i]];
    return out;
  }
  // a view as typed in the form, checked as the hub will: [view, error]
  function checked(d) {
    const name = String(d.name || "").split(/\s+/).filter(Boolean).join(" ");
    if (!name) return [null, "Name required"];
    if (name.length > NAME_MAX) return [null, `Name: at most ${NAME_MAX} characters`];
    const mark = String(d.mark || "").trim().toUpperCase().replace("*", "∗");
    if (!MARK_RE.test(mark)) return [null, "Badge: one letter or digit"];
    const color = d.color && COLOR_RE.test(d.color) ? d.color.toLowerCase() : "";
    const f = {}, src = d.filter || {};
    if (src.ours) f.ours = true;
    for (const k of ["services", "systems", "sources", "units"]) {
      const v = [...new Set((src[k] || []).map((x) => String(x).trim()).filter(Boolean))];
      if (v.length) f[k] = v;
    }
    if (len(f.units) > 20) return [null, "Units: at most 20"];
    if (len(f.sources) > 100) return [null, "Channels: at most 100"];
    if (src.linked) f.linked = true;
    return [{ id: d.id, name, mark, color, filter: f }, ""];
  }
  const newId = (scope) => `${scope === "mine" ? "p" : "u"}:${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

  // ---- state: the department's (from the tree), this person's (MePrefs) --------------------------------------------

  const st = { shared: copy(SEED), rev: "", custom: false, p: prefsOf(null), subs: [], sources: [], editors: new Set() };
  let hadLocal = false;
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) { st.p = prefsOf(JSON.parse(raw)); hadLocal = true; }
  } catch (_) { /* the defaults */ }
  const keep = () => { try { localStorage.setItem(KEY, JSON.stringify(st.p)); } catch (_) { /* this page view only */ } };
  function changed() {
    for (const fn of st.subs) { try { fn(); } catch (e) { console.error("views:", e); } }
    for (const ed of st.editors) if (!ed.form) draw(ed);
  }
  function setMine(p) {
    st.p = prefsOf(p);
    keep();
    if (window.MePrefs) MePrefs.changed("radio_views");
    changed();
  }
  if (window.MePrefs) {
    MePrefs.register("radio_views", { get: () => st.p, hadLocal,
                                      apply: (v) => { st.p = prefsOf(v); keep(); changed(); } });
  }
  // the department's, as the tree brought them ({rev, custom, views}); sources: the tree (a form's channels)
  function setShared(d, sources) {
    if (sources) st.sources = sources;
    if (!d || !Array.isArray(d.views)) return;
    const same = d.rev === st.rev && JSON.stringify(d.views) === JSON.stringify(st.shared);
    st.shared = d.views;
    st.rev = d.rev || "";
    st.custom = !!d.custom;
    if (!same) changed();
  }
  async function saveShared(views, action = "set") {
    const body = action === "reset" ? { action, base: st.rev } : { action, views, base: st.rev };
    const d = await api("POST", "/api/cad/settings/radio_views", { ...body, operator: typeof operator === "function" ? operator() : "" });
    setShared(d.radio_views);
    return d;
  }
  const find = (id) => list(st.shared, st.p).find((v) => v.id === id) || null;

  // ---- the editor -------------------------------------------------------------------------------------------------

  const isAdmin = () => typeof canDo !== "function" || canDo("admin");
  function badge(v) {
    const svc = service(v);
    const b = el("span", `rn-svc rn-${svc || "any"}`, v.mark || "·");
    if (v.color) b.style.setProperty("--c", v.color);
    b.title = v.name;
    return b;
  }
  function btn(text, label, act, id = "", cls = "quiet sm icon", off = false) {
    const b = el("button", cls, text);
    b.type = "button";
    b.dataset.act = act;
    if (id) b.dataset.id = id;
    b.title = label;
    b.setAttribute("aria-label", label);
    b.disabled = off;
    return b;
  }
  function check(label, on, data, title = "") {
    const l = el("label", "inline check");
    const c = el("input");
    c.type = "checkbox";
    c.checked = !!on;
    Object.assign(c.dataset, data);
    l.append(c);
    if (label) l.append(el("span", "", label));
    if (title) l.title = title;
    l._input = c;
    return l;
  }
  const rows = (ed) => (ed.mode === "dept" ? st.shared.map((v) => ({ ...v, scope: "dept" })) : all(st.shared, st.p));
  function draw(ed) {
    const admin = isAdmin(), out = [];
    if (ed.form) out.push(form(ed, admin));
    else {
      const items = rows(ed);
      const ul = el("ul", "rv-ed-list");
      ul.setAttribute("aria-label", ed.mode === "dept" ? "Department views" : "Views");
      items.forEach((v, i) => {
        const li = el("li", "rv-ed-row" + (v.hidden ? " off" : ""));
        li.dataset.id = v.id;
        const may = v.scope === "mine" || admin;
        if (ed.mode === "mine") {
          const c = check("", !v.hidden, { show: v.id }, v.hidden ? "Hidden: show" : "Shown: hide");
          c._input.setAttribute("aria-label", `Show ${v.name}`);
          li.append(c);
        }
        li.append(badge(v), el("span", "trunc rv-ed-name", v.name));
        if (ed.mode === "mine") {
          const tag = el("span", "dim small", v.scope === "mine" ? "Mine" : "Dept");
          tag.title = v.scope === "mine" ? "Yours only" : "The department's (admins edit)";
          li.append(tag);
        }
        li.append(el("span", "spacer"));
        const mayMove = ed.mode === "mine" || admin;
        li.append(btn("▲", `Move ${v.name} up`, "up", v.id, "quiet sm icon", !mayMove || i === 0),
                  btn("▼", `Move ${v.name} down`, "down", v.id, "quiet sm icon", !mayMove || i === items.length - 1));
        if (may) li.append(btn("✎", `Edit ${v.name}`, "edit", v.id), btn("✕", `Delete ${v.name}`, "del", v.id));
        ul.append(li);
      });
      if (!items.length) out.push(el("div", "dim small", "None"));
      out.push(ul);
      const bar = el("div", "rv-ed-bar");
      if (ed.mode === "mine" || admin) bar.append(btn("+ View", "New view", "add", "", "sm"));
      if (ed.mode === "dept" && admin && st.custom) bar.append(btn("Reset", "The views that were coded (Our calls, Police, EMS, Transit, Air, All)", "reset", "", "sm quiet"));
      bar.append(el("span", "spacer"));
      if (ed.close) bar.append(btn("Done", "Done", "done", "", "sm primary"));
      out.push(bar, el("div", "error", ed.error || ""));
    }
    ed.box.replaceChildren(...out);
    if (ed.focus) {
      const n = ed.box.querySelector(ed.focus);
      if (n && !n.disabled && n.focus) n.focus();
      ed.focus = "";
    }
  }
  function form(ed, admin) {
    const d = ed.form.draft, f = d.filter, box = el("form", "rv-ed-form");
    box.setAttribute("aria-label", ed.form.isNew ? "New view" : `Edit ${d.name}`);
    const field = (label, input, title = "") => { const l = el("label", "field", label); l.append(input); if (title) l.title = title; return l; };
    const input = (key, value, max, ph = "") => { const i = el("input"); i.type = "text"; i.value = value || ""; i.maxLength = max; i.dataset.f = key; if (ph) i.placeholder = ph; return i; };
    const top = el("div", "row rv-ed-top");
    const color = el("input");
    color.type = "color";
    color.value = d.color || "#888888";
    color.dataset.f = "color";
    color.setAttribute("aria-label", "Badge colour");
    top.append(field("Name", input("name", d.name, NAME_MAX)), field("Badge", input("mark", d.mark, 1, "A"), "One letter or digit"),
               field("Colour", color), check("Auto", !d.color, { f: "autocolor" }, "The service's colour"));
    box.append(top, check("Our calls", f.ours, { f: "ours" }, "Our dispatch, paging and firegrounds, with unit notes"));
    const group = (title, items) => { const g = el("fieldset", "rv-ed-set"); g.append(el("legend", "", title), ...items); return g; };
    box.append(group("Services", SERVICES.map((s) => check(SERVICE[s], (f.services || []).includes(s), { list: "services", v: s }))));
    const systems = [...new Map(st.sources.map((s) => [s.system, s.system_label || s.system])).entries()];
    for (const s of f.systems || []) if (!systems.some(([k]) => k === s)) systems.push([s, s]);
    if (systems.length) box.append(group("Systems", systems.map(([k, lab]) => check(lab, (f.systems || []).includes(k), { list: "systems", v: k }))));
    const sel = el("select", "rv-ed-src");
    sel.multiple = true;
    sel.size = 6;
    sel.dataset.f = "sources";
    sel.setAttribute("aria-label", "Channels");
    const known = new Set();
    for (const s of st.sources) {
      known.add(s.key);
      const o = new Option(`${s.system_label || s.system} › ${s.name}`, s.key);
      o.selected = (f.sources || []).includes(s.key);
      sel.append(o);
    }
    for (const k of f.sources || []) if (!known.has(k)) { const o = new Option(k, k); o.selected = true; sel.append(o); }
    box.append(field("Channels", sel, "Added to the services and systems (Ctrl/⌘-click for more)"));
    box.append(field("Units", input("units", (f.units || []).join(", "), 200, "E60, L31"), "Only these units' transmissions"));
    box.append(check("On an incident", f.linked, { f: "linked" }, "Only transmissions linked to an incident"));
    if (ed.mode === "mine" && admin) box.append(check("For everyone", ed.form.scope === "dept", { f: "dept" }, "A department view (admins)"));
    const bar = el("div", "rv-ed-bar");
    const save = btn(ed.busy ? "Saving…" : "Save", "Save", "save", "", "sm primary", ed.busy);
    bar.append(el("span", "spacer"), btn("Cancel", "Cancel", "cancel", "", "sm quiet"), save);
    box.append(bar, el("div", "error", ed.error || ""));
    return box;
  }
  function read(ed) {                                  // the form's fields into its draft
    const d = ed.form.draft, q = (s) => ed.box.querySelector(s);
    const val = (k) => { const n = q(`[data-f="${k}"]`); return n ? n : null; };
    if (val("name")) d.name = val("name").value;
    if (val("mark")) d.mark = val("mark").value;
    const auto = val("autocolor");
    d.color = auto && auto.checked ? "" : val("color") ? val("color").value : d.color;
    d.filter.ours = !!(val("ours") && val("ours").checked);
    d.filter.linked = !!(val("linked") && val("linked").checked);
    for (const k of ["services", "systems"]) d.filter[k] = [...ed.box.querySelectorAll(`[data-list="${k}"]`)].filter((c) => c.checked).map((c) => c.dataset.v);
    const sel = val("sources");
    if (sel) d.filter.sources = [...sel.options].filter((o) => o.selected).map((o) => o.value);
    if (val("units")) d.filter.units = val("units").value.split(",").map((x) => x.trim()).filter(Boolean);
    const dept = val("dept");
    if (dept) ed.form.scope = dept.checked ? "dept" : "mine";
  }
  async function act(ed, b) {
    const a = b.dataset.act, id = b.dataset.id;
    ed.error = "";
    try {
      if (a === "done") { if (ed.close) ed.close(); return; }
      if (a === "add") {
        ed.form = { isNew: true, scope: ed.mode === "dept" ? "dept" : "mine", from: "", draft: { id: "", name: "", mark: "", color: "", filter: {} } };
        ed.focus = '[data-f="name"]';
        return draw(ed);
      }
      if (a === "edit") {
        const v = rows(ed).find((x) => x.id === id);
        if (!v) return;
        ed.form = { isNew: false, scope: v.scope, from: v.scope, draft: copy({ id: v.id, name: v.name, mark: v.mark, color: v.color || "", filter: v.filter || {} }) };
        ed.focus = '[data-f="name"]';
        return draw(ed);
      }
      if (a === "cancel") { ed.form = null; ed.focus = '[data-act="add"]'; return draw(ed); }
      if (a === "save") return await saveForm(ed);
      if (a === "up" || a === "down") {
        const dir = a === "up" ? -1 : 1;
        ed.focus = `[data-act="${a}"][data-id="${CSS.escape ? CSS.escape(id) : id}"]`;
        if (ed.mode === "mine") {
          const ids = all(st.shared, st.p).map((v) => v.id);
          setMine({ ...st.p, order: moved(ids, id, dir) });
        } else {
          const ids = moved(st.shared.map((v) => v.id), id, dir);
          await saveShared(ids.map((x) => st.shared.find((v) => v.id === x)));
        }
        return draw(ed);
      }
      if (a === "del") {
        const v = rows(ed).find((x) => x.id === id);
        if (!v) return;
        if (v.scope === "mine") {
          setMine({ ...st.p, mine: st.p.mine.filter((x) => x.id !== id), order: st.p.order.filter((x) => x !== id),
                    hidden: st.p.hidden.filter((x) => x !== id) });
        } else {
          if (window.UI && UI.confirm && !(await UI.confirm(`Delete ${v.name} for everyone?`, { ok: "Delete" }))) return;
          await saveShared(st.shared.filter((x) => x.id !== id));
        }
        ed.focus = '[data-act="add"]';
        return draw(ed);
      }
      if (a === "reset") {
        if (window.UI && UI.confirm && !(await UI.confirm("Back to Our calls, Police, EMS, Transit, Air, All for everyone?", { ok: "Reset" }))) return;
        await saveShared(null, "reset");
        return draw(ed);
      }
    } catch (e) {
      ed.error = e.message || String(e);
      draw(ed);
    }
  }
  async function saveForm(ed) {
    read(ed);
    const fm = ed.form;
    const [v, err] = checked(fm.draft);
    if (!v) { ed.error = err; return draw(ed); }
    const scope = fm.scope, moving = !fm.isNew && fm.from !== scope;
    if (!v.id || moving) v.id = newId(scope);
    ed.busy = true;
    draw(ed);
    try {
      if (scope === "mine") {
        const mine = st.p.mine.slice(), i = mine.findIndex((x) => x.id === v.id);
        if (i >= 0) mine[i] = v; else mine.push(v);
        if (mine.length > MAX_MINE) throw new Error(`At most ${MAX_MINE} views of your own`);
        if (moving) await saveShared(st.shared.filter((x) => x.id !== fm.draft.id));
        const order = moving ? st.p.order.map((x) => (x === fm.draft.id ? v.id : x)) : st.p.order;
        setMine({ ...st.p, mine, order });
      } else {
        const views = st.shared.slice(), i = views.findIndex((x) => x.id === v.id);
        if (i >= 0) views[i] = v; else views.push(v);
        await saveShared(views);
        if (moving) {
          setMine({ ...st.p, mine: st.p.mine.filter((x) => x.id !== fm.draft.id),
                    order: st.p.order.map((x) => (x === fm.draft.id ? v.id : x)) });
        }
      }
      ed.form = null;
      ed.focus = `[data-act="edit"][data-id="${v.id}"]`;
    } catch (e) {
      ed.error = e.message || String(e);
    } finally {
      ed.busy = false;
    }
    draw(ed);
  }
  // editor(box, {mode: "mine" | "dept", close}): drawn now and again when the views change (not while a form is open)
  function editor(box, opts = {}) {
    const ed = { box, mode: opts.mode === "dept" ? "dept" : "mine", close: opts.close || null, form: null, error: "", busy: false, focus: "" };
    if (!box.dataset.rvEd) {                           // (one set of listeners per box: a remount draws again)
      box.dataset.rvEd = "1";
      box.addEventListener("click", (e) => {
        const b = e.target.closest("button[data-act]");
        if (b && box._ed && !b.disabled) { e.preventDefault(); act(box._ed, b); }
      });
      box.addEventListener("change", (e) => {
        const c = e.target, ed2 = box._ed;
        if (!ed2 || !c.dataset) return;
        if (c.dataset.show) {
          const id = c.dataset.show, hidden = st.p.hidden.filter((x) => x !== id);
          if (!c.checked) hidden.push(id);
          ed2.focus = `[data-show="${id}"]`;
          setMine({ ...st.p, hidden });
        } else if (c.dataset.f === "color" && ed2.form) {
          const auto = box.querySelector('[data-f="autocolor"]');
          if (auto) auto.checked = false;
        }
      });
      box.addEventListener("submit", (e) => { e.preventDefault(); if (box._ed && box._ed.form) saveForm(box._ed); });
    }
    if (box._ed) st.editors.delete(box._ed);
    box._ed = ed;
    st.editors.add(ed);
    draw(ed);
    return { stop: () => st.editors.delete(ed), get editing() { return !!ed.form; } };
  }
  // Settings > Radios > Views: the department's set, with the tree's channels for the form
  async function mountSettings(box) {
    try {
      const d = await api("GET", "/api/cad/comms/sources");
      setShared(d.views, d.sources || []);
    } catch (e) { box.replaceChildren(el("div", "error", e.message || String(e))); return; }
    editor(box, { mode: "dept" });
  }

  return {
    SEED, SERVICES, dims, matches, service, shows, resolve, prefsOf, all, list, moved, checked,
    setShared, saveShared, setMine, find, editor, mountSettings, badge,
    views: () => list(st.shared, st.p),
    every: () => all(st.shared, st.p),
    rev: () => st.rev,
    mine: () => st.p,
    onChange: (fn) => { st.subs.push(fn); },
    _st: st,
  };
})();
