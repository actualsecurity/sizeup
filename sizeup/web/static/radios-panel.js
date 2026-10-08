// The Radios editor: radio ID -> rig (sizeup/cad/radios.py), as a self-contained panel.
//   RadiosPanel.mount(box, { operator: () => "name" })   draws into box (any page: the CAD's
//   "📻 Radios" overlay now; Settings -> Radios later). Its API is /api/cad/radios; every edit is
//   kept in settings_audit (who, key, old -> new), shown at the bottom.
"use strict";

window.RadiosPanel = (() => {
  const SOURCE_TEXT = { confirmed: "confirmed", guessed: "guessed", heard: "heard on the air" };
  const p = { box: null, opts: {}, data: null, filter: "", show: "mapped", sys: "", editing: null, error: "" };

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
      if (!r.ok) throw new Error(r.status === 404 ? "this server has no radio registry yet (restart sizeup)" : `${r.status}`);
      p.data = await r.json();
      p.error = "";
    } catch (e) {
      p.error = `Could not load the radios: ${e.message}`;
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
    s.append(new Option("— no rig —", ""));
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
    return all.filter((r) => {
      if (p.sys && r.system !== p.sys) return false;
      if (p.show === "mapped" && !r.unit && r.source !== "confirmed" && !(r.system === "mdc")) return false;
      if (p.show === "unassigned" && r.unit) return false;
      if (p.show === "confirmed" && r.source !== "confirmed") return false;
      const hay = `${r.system} ${r.radio_id} ${r.unit} ${r.label} ${r.alias} ${r.source} ${r.notes}`.toLowerCase();
      return words.every((w) => hay.includes(w));
    });
  }

  function editRow(r) {
    const tr = el("tr", "rp-edit");
    const unit = unitSelect(r.unit), label = el("input"), src = sourceSelect(r.source), notes = el("input");
    label.value = r.label || "";
    label.maxLength = 60;
    label.placeholder = "Portable 1, Mobile…";
    notes.value = r.notes || "";
    notes.maxLength = 300;
    const save = el("button", "primary mini", "Save"), cancel = el("button", "mini", "Cancel"), del = el("button", "mini danger-quiet", "Remove");
    [save, cancel, del].forEach((b) => { b.type = "button"; });
    save.addEventListener("click", async () => {
      try {
        await post("/api/cad/radios", { system: r.system, radio_id: r.radio_id, unit: unit.value, label: label.value,
                                        source: src.value, notes: notes.value });
        p.editing = null;
        await load();
      } catch (e) { p.error = e.message; render(); }
    });
    cancel.addEventListener("click", () => { p.editing = null; render(); });
    del.addEventListener("click", async () => {
      if (!confirm(`Remove ${r.system.toUpperCase()} ${r.radio_id} from the registry?`)) return;
      try { await post("/api/cad/radios/delete", { system: r.system, radio_id: r.radio_id }); p.editing = null; await load(); }
      catch (e) { p.error = e.message; render(); }
    });
    const td = (n, cls) => { const c = el("td", cls); c.append(n); return c; };
    tr.append(td(el("span", "", r.system.toUpperCase())), td(el("b", "", r.radio_id)), td(unit), td(label), td(src),
              td(el("span", "dim", r.alias || "")), td(el("span", "dim small", fmt(r.last_heard))), td(notes));
    const act = el("td", "rp-act");
    act.append(save, cancel, del);
    tr.append(act);
    return tr;
  }

  function viewRow(r) {
    const tr = el("tr", r.source);
    const chip = el("span", `chip src-${r.source}`, SOURCE_TEXT[r.source] || r.source);
    const edit = el("button", "mini", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => { p.editing = `${r.system}:${r.radio_id}`; render(); });
    const confirmB = el("button", "mini", "✓ Confirm");
    confirmB.type = "button";
    confirmB.title = "Mark this mapping confirmed by the department";
    confirmB.hidden = r.source === "confirmed" || !r.unit;
    confirmB.addEventListener("click", async () => {
      try { await post("/api/cad/radios", { system: r.system, radio_id: r.radio_id, source: "confirmed" }); await load(); }
      catch (e) { p.error = e.message; render(); }
    });
    const cells = [r.system.toUpperCase(), r.radio_id, r.unit || "—", r.label, null, r.alias, fmt(r.last_heard), r.notes];
    cells.forEach((v, i) => {
      const c = el("td", i === 2 ? "rp-u" + (r.unit ? "" : " dim") : i >= 5 ? "dim small" : "");
      if (i === 4) c.append(chip);
      else if (i === 1) c.append(el("b", "", v));
      else c.textContent = v || "";
      tr.append(c);
    });
    const act = el("td", "rp-act");
    act.append(confirmB, edit);
    tr.append(act);
    if (r.edited) tr.title = "Edited by a person: never overwritten by what is heard";
    return tr;
  }

  function addForm() {
    const f = el("form", "rp-add row");
    const sys = el("select");
    sys.append(new Option("MDC (FG1)", "mdc"), new Option("P25 (county)", "p25"));
    const id = el("input");
    id.placeholder = "radio ID (0611 / 41228)";
    id.maxLength = 12;
    id.required = true;
    const unit = unitSelect(""), label = el("input"), src = sourceSelect("confirmed");
    label.placeholder = "label";
    label.maxLength = 60;
    const b = el("button", "primary", "Add radio");
    b.type = "submit";
    f.append(el("b", "", "Add"), sys, id, unit, label, src, b);
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await post("/api/cad/radios", { system: sys.value, radio_id: id.value.trim(), unit: unit.value, label: label.value,
                                        source: src.value });
        p.filter = id.value.trim();
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
    if (!d) { out.push(el("div", "dim", p.error ? "" : "Loading…")); box.replaceChildren(...out); return; }
    const all = d.radios || [];
    const n = (f) => all.filter(f).length;
    out.push(el("div", "dim small rp-sum",
      `${all.length} radio IDs · ${n((r) => r.source === "confirmed")} confirmed · ${n((r) => r.source === "guessed")} guessed · ` +
      `${n((r) => r.source === "heard")} heard · ${n((r) => !r.unit)} on no rig. Seeded from ${d.seed.file} v${d.seed.version}; ` +
      "a person's edit always wins over what is heard."));
    if ((d.conflicts || []).length || (d.unfit || []).length) {
      const warn = el("details", "rp-warn");
      warn.append(el("summary", "", `Left unmapped: ${d.conflicts.length} conflict(s), ${new Set(d.unfit.map((u) => u.unit)).size} unit(s) the ID pattern doesn't fit`));
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
    q.placeholder = "Filter: ID, unit, label, alias";
    q.value = p.filter;
    q.setAttribute("aria-label", "Filter radios");
    q.addEventListener("input", () => { p.filter = q.value; drawTable(); });
    const show = el("select");
    for (const [v, t] of [["mapped", "On a rig + all MDC"], ["all", "Everything (all P25 heard)"],
                          ["unassigned", "On no rig"], ["confirmed", "Confirmed only"]]) show.append(new Option(t, v));
    show.value = p.show;
    show.addEventListener("change", () => { p.show = show.value; drawTable(); });
    const sys = el("select");
    for (const [v, t] of [["", "MDC + P25"], ["mdc", "MDC (FG1)"], ["p25", "P25"]]) sys.append(new Option(t, v));
    sys.value = p.sys;
    sys.addEventListener("change", () => { p.sys = sys.value; drawTable(); });
    show.className = sys.className = "ro-ok";      // filters: still there when Settings are read-only (cad.js)
    tools.append(q, show, sys);
    out.push(tools, addForm());
    const wrap = el("div", "tscroll rp-scroll");
    const table = el("table", "evtable rp-table");
    const head = el("thead");
    const hr = el("tr");
    for (const h of ["Sys", "Radio ID", "Rig", "Label", "Source", "Alias (P25)", "Last heard", "Notes", ""]) hr.append(el("th", "", h));
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
      li.append(el("span", "dim", `${fmt(a.at)} ${a.operator || "—"} · `), el("b", "", a.key || ""), el("span", "",
        a.old || a.new ? `: ${a.old || "(none)"} → ${a.new || "(removed)"}` : ""));
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
      const td = el("td", "dim", "No radios match");
      td.colSpan = 9;
      tr.append(td);
      body.append(tr);
    } else if (list.length > shownRows.length) {
      const tr = el("tr");
      const td = el("td", "dim", `${list.length - shownRows.length} more: filter to see them`);
      td.colSpan = 9;
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
