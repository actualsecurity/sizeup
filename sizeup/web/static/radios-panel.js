// The Radios editor: radio ID -> rig (sizeup/cad/radios.py), as a self-contained panel.
//   RadiosPanel.mount(box, { operator: () => "name" })   draws into box (any page: the CAD's
//   "📻 Radios" overlay now; Settings -> Radios later). Its API is /api/cad/radios; every edit is
//   kept in settings_audit (who, key, old -> new), shown at the bottom. The fireground's IDs (FG1, FG2: MDC,
//   FleetSync) are heard like P25's (#163): a new one is a "heard" row, every one has its last-heard time and
//   channel; the list puts the radios heard most recently first in each system, and says how many transmissions
//   on each fireground channel had an ID (id_stats).
//   Another P25 system's radios (MRRS, data.other_systems) are another agency's: never on a rig, named by a label
//   (an agency or role) or a range of IDs (data.ranges; an ID's own label wins); each heard one may carry a "likely
//   agency" hint (data.hints: the talkgroup it's heard on most), accepted into its label with one click, never applied.
"use strict";

window.RadiosPanel = (() => {
  const SOURCE_TEXT = { confirmed: "confirmed", guessed: "guessed", heard: "heard" };
  const FM = new Set(["mdc", "fleetsync"]);         // the fireground's (FG1, FG2): every one listed by default, named or not
  const SYS_TEXT = { mdc: "MDC (fireground)", fleetsync: "FleetSync (fireground, Kenwood)", p25: "P25 (county)" };
  const p = { box: null, opts: {}, data: null, filter: "", show: "mapped", sys: "", sort: "last", editing: null,
              rangeEdit: null, error: "", drafts: {} };
  // a form's typed values kept across a redraw (an error redraws the panel): key -> {field: value}; gone once saved
  function keep(key, fields) {
    const d = p.drafts[key] || {};
    for (const [k, n] of Object.entries(fields)) {
      if (d[k] !== undefined) n.value = d[k];
      n.addEventListener(n.tagName === "SELECT" ? "change" : "input", () => { (p.drafts[key] ||= {})[k] = n.value; });
    }
  }
  const other = (sys) => !!(p.data && p.data.other_systems && p.data.other_systems[sys]);   // MRRS: names, no rigs
  const sysText = (sys) => SYS_TEXT[sys] || (other(sys) ? `${p.data.other_systems[sys]} (P25, names only)` : sys);
  const inRange = (r) => ((p.data && p.data.ranges) || []).find((g) => g.system === r.system && /^\d+$/.test(r.radio_id)
                                                                && g.lo <= +r.radio_id && +r.radio_id <= g.hi) || null;
  const hintOf = (r) => (((p.data && p.data.hints) || {})[r.system] || {})[r.radio_id] || null;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const op = () => (p.opts.operator ? p.opts.operator() : "");
  const fmt = (t) => (t ? new Date(t * 1000).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit",
                                                                    minute: "2-digit", hour12: false }) : "");

  async function load() {
    try {
      const r = await fetch("/api/cad/radios");
      if (!r.ok) throw new Error(r.status === 404 ? "no radio registry (restart sizeup)" : `${r.status}`);
      p.data = await r.json();
      p.error = "";
    } catch (e) {
      p.error = `Load failed: ${e.message}`;
    }
    render();
  }

  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify(Object.assign({ operator: op() }, body)) });
    let d = null;
    try { d = await r.json(); } catch (_) { /* not JSON */ }
    if (!r.ok) throw new Error((d && d.error) || `${r.status}`);
    return d;
  }

  function unitSelect(value) {
    const s = el("select", "rp-unit");
    s.append(new Option("No rig", ""));
    const units = (p.data && p.data.units) || [];
    for (const [title, rows] of [["PCFD", units.filter((u) => u.pcfd)], ["Others", units.filter((u) => !u.pcfd)]]) {
      const g = document.createElement("optgroup");
      g.label = title;
      for (const u of rows) g.append(new Option(u.name, u.name));
      s.append(g);
    }
    s.value = value || "";
    return s;
  }
  function sourceSelect(value) {
    const s = el("select", "rp-source");
    for (const k of (p.data && p.data.sources) || ["confirmed", "guessed", "heard"]) s.append(new Option(SOURCE_TEXT[k] || k, k));
    s.value = value || "confirmed";
    return s;
  }

  function rows() {
    const all = (p.data && p.data.radios) || [];
    const words = p.filter.toLowerCase().split(/\s+/).filter(Boolean);
    const order = new Map(all.map((r, i) => [r, i]));
    // in each system (as the server sorts them), the radios heard most recently first, then the rest as they were
    const key = p.sort === "count" ? (r) => r.heard || 0 : (r) => r.last_heard || 0;
    const sorted = all.slice().sort((a, b) => (a.system === b.system ? key(b) - key(a) : 0) || order.get(a) - order.get(b));
    return sorted.filter((r) => {
      if (p.sys && r.system !== p.sys) return false;
      // (another system's: its named ones, every one when that system is picked)
      if (p.show === "mapped" && !r.unit && r.source !== "confirmed" && !FM.has(r.system)
          && !(other(r.system) && (r.label || p.sys === r.system))) return false;
      if (p.show === "unassigned" && r.unit) return false;
      if (p.show === "confirmed" && r.source !== "confirmed") return false;
      const g = inRange(r), h = hintOf(r);
      const hay = `${r.system} ${r.radio_id} ${r.unit} ${r.label} ${r.alias} ${r.source} ${r.notes} ${g ? g.label : ""} `
        + `${h ? h.agency : ""}`.toLowerCase();
      return words.every((w) => hay.includes(w));
    });
  }

  function editRow(r) {
    const tr = el("tr", "rp-edit");
    const unit = unitSelect(r.unit), label = el("input"), src = sourceSelect(r.source), notes = el("input");
    const named = other(r.system);
    label.value = r.label || "";
    label.maxLength = 60;
    label.placeholder = named ? "Agency / role" : "Portable 1";
    notes.value = r.notes || "";
    notes.maxLength = 300;
    const save = el("button", "primary mini", "Save"), cancel = el("button", "mini", "Cancel"), del = el("button", "mini danger-quiet", "Remove");
    [save, cancel, del].forEach((b) => { b.type = "button"; });
    // "Also fix past transmissions" (#977): asked each time, off by default (the admins' choice): ticked, a unit changed
    // or cleared takes the radio's recent rows with it
    const past = el("label", "rp-past small");
    const pastBox = el("input");
    pastBox.type = "checkbox";
    pastBox.checked = false;
    past.append(pastBox, " Past too");
    past.title = "Also fix past transmissions: tick it to move this radio's rows of the last 30 days (and their links) "
      + "off the old unit when you change or clear its unit. Unticked, past rows keep the unit they were logged with.";
    save.addEventListener("click", async () => {
      try {
        await post("/api/cad/radios", Object.assign({ system: r.system, radio_id: r.radio_id, label: label.value,
                                                      source: src.value, notes: notes.value },
                                                    named ? {} : { unit: unit.value, fix_past: pastBox.checked }));
        p.editing = null;
        await load();
      } catch (e) { p.error = e.message; render(); }
    });
    cancel.addEventListener("click", () => { p.editing = null; render(); });
    del.addEventListener("click", async () => {
      if (!(await UI.confirm(`Remove ${r.system.toUpperCase()} ${r.radio_id}?`, { ok: "Remove" }))) return;
      try { await post("/api/cad/radios/delete", { system: r.system, radio_id: r.radio_id }); p.editing = null; await load(); }
      catch (e) { p.error = e.message; render(); }
    });
    const td = (n, cls) => { const c = el("td", cls); c.append(n); return c; };
    tr.append(td(el("span", "", r.system.toUpperCase())), td(el("b", "", r.radio_id)),
              td(named ? el("span", "dim", "—") : unit), td(label), td(src),
              td(el("span", "dim", r.alias || "")), td(el("span", "dim small", fmt(r.last_heard))),
              td(el("span", "dim small", r.last_channel || "")), td(notes));
    const act = el("td", "rp-act");
    act.append(...(named ? [] : [past]), save, cancel, del);
    tr.append(act);
    return tr;
  }

  function viewRow(r) {
    const tr = el("tr", r.source);
    const chip = el("span", `chip src-${r.source}`, SOURCE_TEXT[r.source] || r.source);
    const edit = el("button", "mini", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => { p.editing = `${r.system}:${r.radio_id}`; render(); });
    const confirmB = el("button", "mini", "Confirm");
    confirmB.type = "button";
    confirmB.title = "Confirmed by the department";
    confirmB.hidden = r.source === "confirmed" || !r.unit;
    confirmB.addEventListener("click", async () => {
      try { await post("/api/cad/radios", { system: r.system, radio_id: r.radio_id, source: "confirmed" }); await load(); }
      catch (e) { p.error = e.message; render(); }
    });
    const cells = [r.system.toUpperCase(), r.radio_id, r.unit || "—", r.label, null, r.alias,
                   fmt(r.last_heard) + (r.heard ? ` ×${r.heard}` : ""), r.last_channel || "", r.notes];
    cells.forEach((v, i) => {
      const c = el("td", i === 2 ? "rp-u" + (r.unit ? "" : " dim") : i >= 5 ? "dim small" : "");
      if (i === 4) c.append(chip);
      else if (i === 1) c.append(el("b", "", v));
      else if (i === 3 && !v && other(r.system)) c.append(...otherLabel(r));
      else c.textContent = v || "";
      if (i === 6 && r.heard) c.title = `Heard ${r.heard}× (at most once a minute counted)`;
      tr.append(c);
    });
    const act = el("td", "rp-act");
    act.append(confirmB, edit);
    tr.append(act);
    if (r.edited) tr.title = "Edited: never overwritten by what is heard";
    return tr;
  }

  // An unnamed radio of another system: its range's label (dim), else the likely agency (a hint: Use takes it)
  function otherLabel(r) {
    const g = inRange(r);
    if (g) {
      const s = el("span", "dim", g.label);
      s.title = `Range ${g.lo}–${g.hi}`;
      return [s];
    }
    const h = hintOf(r);
    if (!h) return [el("span", "dim", "unknown")];
    const pct = `${Math.round(h.share * 100)}%`;
    const s = el("span", "dim small", h.shared ? `shared · ${(typeof chName === "function" ? chName(h) : h.channel)} ${pct}` : `${h.agency}? ${pct}`);
    s.title = `Likely agency: heard on ${(typeof chName === "function" ? chName(h) : h.channel)} in ${pct} of ${h.n} transmissions (last 14 days)`
      + (h.shared ? ". Several agencies share it: no guess" : ". A hint only: never applied");
    if (h.shared) return [s];
    const use = el("button", "mini", "Use");
    use.type = "button";
    use.title = `Label it ${h.agency}`;
    use.addEventListener("click", async () => {
      try {
        await post("/api/cad/radios", { system: r.system, radio_id: r.radio_id, label: h.agency, source: "confirmed" });
        await load();
      }
      catch (e) { p.error = e.message; render(); }
    });
    return [s, " ", use];
  }

  // Another system's ID ranges: lo–hi -> a label, an admin's (never overlapping; an ID's own label wins)
  function rangesBox(sys) {
    const box = el("div", "rp-ranges");
    const list = ((p.data && p.data.ranges) || []).filter((g) => g.system === sys);
    box.append(el("b", "small", `${p.data.other_systems[sys]} ranges`));
    const ol = el("ul", "list rp-range-list");
    for (const g of list) {
      const li = el("li", "small row");
      if (p.rangeEdit === g.id) {
        li.append(rangeForm(sys, g));
      } else {
        const ed = el("button", "mini", "Edit"), del = el("button", "mini danger-quiet", "Remove");
        [ed, del].forEach((b) => { b.type = "button"; });
        ed.addEventListener("click", () => { p.rangeEdit = g.id; render(); });
        del.addEventListener("click", async () => {
          if (!(await UI.confirm(`Remove range ${g.lo}–${g.hi}?`, { ok: "Remove" }))) return;
          try { await post("/api/cad/radios/range/delete", { id: g.id }); await load(); }
          catch (e) { p.error = e.message; render(); }
        });
        const t = el("span", "", `${g.lo}–${g.hi} `);
        if (g.notes) t.title = g.notes;
        li.append(t, el("b", "", g.label), ed, del);
      }
      ol.append(li);
    }
    box.append(ol, rangeForm(sys, null));
    return box;
  }

  function rangeForm(sys, g) {
    const f = el("form", "rp-add row");
    const lo = el("input"), hi = el("input"), label = el("input"), notes = el("input");
    lo.placeholder = "From ID";
    hi.placeholder = "To ID";
    label.placeholder = "Agency / role";
    notes.placeholder = "Notes";
    [lo, hi].forEach((x) => { x.inputMode = "numeric"; x.maxLength = 8; x.required = true; });
    label.maxLength = 60;
    label.required = true;
    notes.maxLength = 300;
    if (g) { lo.value = g.lo; hi.value = g.hi; label.value = g.label; notes.value = g.notes || ""; }
    const key = `range:${sys}:${g ? g.id : "new"}`;
    keep(key, { lo, hi, label, notes });
    const b = el("button", "primary mini", g ? "Save" : "Add range");
    b.type = "submit";
    f.append(lo, hi, label, notes, b);
    if (g) {
      const c = el("button", "mini", "Cancel");
      c.type = "button";
      c.addEventListener("click", () => { p.rangeEdit = null; delete p.drafts[key]; render(); });
      f.append(c);
    }
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await post("/api/cad/radios/range", Object.assign({ system: sys, lo: lo.value.trim(), hi: hi.value.trim(),
                                                            label: label.value, notes: notes.value }, g ? { id: g.id } : {}));
        p.rangeEdit = null;
        delete p.drafts[key];
        await load();
      } catch (err) { p.error = err.message; render(); }
    });
    return f;
  }

  function addForm() {
    const f = el("form", "rp-add row");
    const sys = el("select");
    for (const k of (p.data && p.data.systems) || ["mdc", "fleetsync", "p25"]) sys.append(new Option(sysText(k), k));
    if (p.sys) sys.value = p.sys;
    const id = el("input");
    id.placeholder = "Radio ID";
    id.maxLength = 12;
    id.required = true;
    const unit = unitSelect(""), label = el("input"), src = sourceSelect("confirmed");
    label.placeholder = "Label";
    label.maxLength = 60;
    const b = el("button", "primary", "Add");
    b.type = "submit";
    const named = () => other(sys.value);
    const fit = () => { unit.hidden = named(); label.placeholder = named() ? "Agency / role" : "Label"; };
    keep("add", { sys, id, unit, label, src });
    sys.addEventListener("change", fit);
    fit();
    f.append(el("b", "", "Add"), sys, id, unit, label, src, b);
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await post("/api/cad/radios", Object.assign({ system: sys.value, radio_id: id.value.trim(), label: label.value,
                                                      source: src.value }, named() ? {} : { unit: unit.value }));
        p.filter = id.value.trim();
        delete p.drafts.add;
        await load();
      } catch (err) { p.error = err.message; render(); }
    });
    return f;
  }

  function render() {
    const box = p.box;
    if (!box) return;
    const d = p.data;
    const out = [];
    if (p.error) out.push(el("div", "error rp-error", p.error));
    if (!d) { out.push(el("div", "dim", p.error ? "" : "Loading")); box.replaceChildren(...out); return; }
    const all = d.radios || [];
    const n = (f) => all.filter(f).length;
    const sum = el("div", "dim small rp-sum");
    sum.append(...[[all.length, "IDs"], [n((r) => r.source === "confirmed"), "confirmed"], [n((r) => r.source === "guessed"), "guessed"],
      [n((r) => r.source === "heard"), "heard"], [n((r) => !r.unit), "no rig"]].map(([k, t]) => el("span", "", `${k} ${t}`)));
    sum.title = `Seeded from ${d.seed.file} v${d.seed.version}. A person's edit always wins over what is heard.`;
    out.push(sum);
    for (const x of d.id_stats || []) {           // each fireground channel in the last 24 h: how many key-ups had an ID, and why not (#163)
      if (!x.n) continue;
      const parts = [`${x.with_id} with an ID`, x.none && `${x.none} no ID sent`, x.undecoded && `${x.undecoded} ID not decoded`,
                     x.weak && `${x.weak} too weak to tell`, x.unknown && `${x.unknown} not checked`].filter(Boolean);
      const line = el("div", "dim small rp-ids", `${(typeof chName === "function" ? chName(x) : x.channel)}, last 24 h: ${x.n} transmission${x.n === 1 ? "" : "s"}, ${parts.join(", ")}`);
      line.title = "No ID sent: no MDC / FleetSync burst on a strong carrier (the radio sends none). ID not decoded: a burst "
        + "was there but didn't decode. Too weak to tell: no burst found on a weak carrier. Not checked: from before this was kept.";
      out.push(line);
    }
    if ((d.conflicts || []).length || (d.unfit || []).length) {
      const warn = el("details", "rp-warn");
      warn.append(el("summary", "", `Unmapped: ${d.conflicts.length} conflicts, ${new Set(d.unfit.map((u) => u.unit)).size} units off pattern`));
      const ul = el("ul");
      for (const c of d.conflicts) ul.append(el("li", "", `${c.ids}: ${c.reason}`));
      const seen = new Set();
      for (const u of d.unfit) {
        if (seen.has(u.unit + u.reason)) continue;
        seen.add(u.unit + u.reason);
        ul.append(el("li", "dim", `${u.unit}: ${u.reason}`));
      }
      warn.append(ul);
      out.push(warn);
    }
    const tools = el("div", "row rp-tools");
    const q = el("input", "tg-filter");
    q.type = "search";
    q.placeholder = "Filter";
    q.value = p.filter;
    q.setAttribute("aria-label", "Filter radios");
    q.addEventListener("input", () => { p.filter = q.value; drawTable(); });
    const show = el("select");
    for (const [v, t] of [["mapped", "On a rig, named + fireground"], ["all", "All (incl. P25 heard)"],
                          ["unassigned", "On no rig"], ["confirmed", "Confirmed only"]]) show.append(new Option(t, v));
    show.value = p.show;
    show.addEventListener("change", () => { p.show = show.value; drawTable(); });
    const sys = el("select");
    sys.append(new Option("All", ""));
    for (const k of d.systems || ["mdc", "fleetsync", "p25"]) sys.append(new Option(sysText(k).replace(", Kenwood", ""), k));
    sys.value = p.sys;
    sys.addEventListener("change", () => { p.sys = sys.value; render(); });
    const sort = el("select");
    for (const [v, t] of [["last", "Last heard"], ["count", "Most heard"]]) sort.append(new Option(t, v));
    sort.value = p.sort;
    sort.setAttribute("aria-label", "Sort radios");
    sort.addEventListener("change", () => { p.sort = sort.value; drawTable(); });
    show.className = sys.className = sort.className = "ro-ok";      // filters: still there when Settings are read-only (cad.js)
    tools.append(q, show, sys, sort);
    out.push(tools, addForm());
    if (other(p.sys) && d.context === false) out.push(el("div", "dim small", `${d.other_systems[p.sys]}: sign-in needed`));
    else if (other(p.sys)) out.push(rangesBox(p.sys));
    const wrap = el("div", "tscroll rp-scroll");
    const table = el("table", "evtable rp-table");
    const head = el("thead");
    const hr = el("tr");
    for (const h of ["Sys", "Radio ID", "Rig", "Label", "Source", "Alias (P25)", "Last heard", "Heard on", "Notes", ""]) hr.append(el("th", "", h));
    head.append(hr);
    const body = el("tbody");
    table.append(head, body);
    wrap.append(table);
    out.push(wrap);
    const logBox = el("details", "rp-log");
    logBox.append(el("summary", "", `Changes (${(d.log || []).length})`));
    const ol = el("ol", "list");
    for (const a of d.log || []) {
      const li = el("li", "small");
      li.append(el("span", "dim", `${fmt(a.at)} ${a.operator || "—"} `), el("b", "", a.key || ""), el("span", "",
        a.old || a.new ? `: ${a.old || "—"} → ${a.new || "removed"}` : ""));
      ol.append(li);
    }
    logBox.append(ol);
    out.push(logBox);
    box.replaceChildren(...out);
    p.tbody = body;
    drawTable();
  }

  function drawTable() {
    const body = p.tbody;
    if (!body) return;
    const list = rows();
    const shownRows = list.slice(0, 400);
    body.replaceChildren(...shownRows.map((r) => (p.editing === `${r.system}:${r.radio_id}` ? editRow(r) : viewRow(r))));
    if (!list.length) {
      const tr = el("tr");
      const td = el("td", "dim", "None");
      td.colSpan = 10;
      tr.append(td);
      body.append(tr);
    } else if (list.length > shownRows.length) {
      const tr = el("tr");
      const td = el("td", "dim", `${list.length - shownRows.length} more: filter`);
      td.colSpan = 10;
      tr.append(td);
      body.append(tr);
    }
  }

  function mount(box, opts = {}) {
    p.box = box;
    p.opts = opts;
    box.classList.add("radios-panel");
    render();
    load();
  }

  return { mount, reload: load };
})();
