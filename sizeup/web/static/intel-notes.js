// Notes and tags on radio intel events (sizeup/intel/notes.py). The admin, 2026-11-25: "a way to add notes, so we can
// take those notes per incident and modify in the future how we look at or pull in that info and respond to it".
//
//   IntelNotes.mount(host, id)   More info (#prealert/intel/<id>, prealert-info.js: #pi-notes): every note in full, the
//                                form (text, tags, corrected category / place / level), Edit / Delete (its author or an
//                                admin), Done / PR # (admins: the review queue), "+ Alias of <common place>" (admins: the
//                                heard words another name of the place picked). Read on a new id or after a change
//                                (GET /api/cad/intel/<id>/notes), never on a poll of the bundle; More info read again
//                                after a change (its Place card shows the corrected place)
//
// The corrected place is the incident editor's location box (cad.js addressBox: an address, an intersection, "@" a
// common place, geocoded as it is typed; qualSelect / qualTyped: the qualifier in front, "IFO") and Heard: the words the
// transcript has for it. A suggestion taken is sent as corrected_pick (sizeup/intel/notes.py); text not picked: as typed.
//   IntelNotes.incident(inc)     the incident view (#d-intel): the notes of the intel events linked to it (GET
//                                /api/cad/incident/<id>/intel), folded with a count; read when the incident changes and
//                                at most every STALE_MS after, drawn only when it changed
//   IntelNotes.chip(n)           the Intel page row's count chip ({n, tags, review}); null for none
//   IntelNotes.TAGS              tag -> its short label
//
// Terse: what a tag means is in its hover. Uses cad.js's helpers (el, $, show, setText, api, canDo, fmtTime, addressBox,
// qualSelect, qualTyped, qualText).
"use strict";

window.IntelNotes = (() => {
  const TAGS = { should_have_pushed: "should push", false_garbled_audio: "garbled audio", wrong_place: "wrong place",
                 wrong_category: "wrong category", out_of_area: "out of area", late: "late", rule_to_add: "rule to add" };
  const TIPS = { should_have_pushed: "Worth a pre-alert: it should have pushed",
                 false_garbled_audio: "The audio or its transcript was garbled (speech-to-text)",
                 wrong_place: "The place is wrong", wrong_category: "The category is wrong",
                 out_of_area: "Not our area", late: "Too late to help", rule_to_add: "A rule should catch this" };
  const STALE_MS = 120000;
  const st = { id: null, seq: 0, data: null, sig: "", editing: null, boxes: 0,
               inc: { id: null, at: 0, data: null, sig: "", open: false } };
  const when = (u) => (u ? (typeof fmtTime === "function" ? fmtTime(u) : new Date(u * 1000).toISOString().slice(5, 16)) : "");

  function tagChip(t) {
    const c = el("span", "chip nt-tag", TAGS[t] || t);
    c.title = TIPS[t] || t;
    return c;
  }
  const placeText = (n) => (typeof qualText === "function" ? qualText(n.corrected_qualifier, n.corrected_place)
    : [n.corrected_qualifier, n.corrected_place].filter(Boolean).join(" "));
  function fixes(n) {
    const place = n.corrected_place ? `${placeText(n)}${n.corrected ? " ◆" : ""}${n.heard ? ` ← "${n.heard}"` : ""}` : "";
    const parts = [["category", n.corrected_category], ["place", place], ["level", n.corrected_level]]
      .filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`);
    if (!parts.length) return null;
    const d = el("div", "dim small nt-fix", `→ ${parts.join(" · ")}`);
    const c = n.corrected;
    if (c) d.title = `◆ on the map: ${c.label} · ${c.lat}, ${c.lon} · ${c.precision}${n.place_name ? ` · ${n.place_name}` : ""}`
      + (n.heard ? `\n← heard "${n.heard}"` : "");
    return d;
  }
  function meta(n) {
    const m = el("span", "dim small", `${n.author || "?"} ${when(n.created_at)}${n.edited ? " (edited)" : ""}`);
    if (n.edited) m.title = `Changed ${when(n.updated_at)}`;
    return m;
  }

  // ---- More info ---------------------------------------------------------------------------------------------------
  async function mount(host, id, force = false) {
    if (!host) return;
    if (!id) { st.id = null; host.replaceChildren(); show(host, false); return; }
    if (id === st.id && !force && st.data) return;
    if (id !== st.id) { st.data = null; st.sig = ""; st.editing = null; }
    st.id = id;
    const seq = ++st.seq;
    try {
      const d = await api("GET", `/api/cad/intel/${id}/notes`);
      if (seq !== st.seq) return;
      st.data = d;
      draw(host);
    } catch (e) {
      if (seq !== st.seq) return;
      show(host, true);
      host.replaceChildren(el("div", "error", `Notes: ${e.message}`));
    }
  }
  function draw(host) {
    const d = st.data;
    const sig = JSON.stringify([st.id, d, st.editing]);
    if (sig === st.sig) return;
    st.sig = sig;
    show(host, true);
    const head = el("div", "sec-head");
    const h = el("h3", "", "Notes");
    h.title = "Notes and tags: what tuning reads (rules and the model); the review queue on the Intel page";
    head.append(h, el("span", "dim small", d.notes.length ? String(d.notes.length) : ""));
    const list = el("ol", "list nt-notes");
    for (const n of d.notes) list.append(st.editing === n.id ? form(host, n) : noteRow(host, n));
    const kids = [head, list];
    const may = typeof canDo !== "function" || canDo("active");          // a deactivated account: reads only
    if (may && (st.editing === null || st.editing === "new")) kids.push(st.editing === "new" ? form(host, null) : addBtn(host));
    if (d.audit && d.audit.length) kids.push(audit(d.audit));
    host.replaceChildren(...kids);
  }
  // admins: every change, folded (who, when, which fields; the words are in the notes and the export)
  function audit(rows) {
    const box = el("details", "nt-audit");
    const sum = el("summary", "dim small", `Changes · ${rows.length}`);
    sum.title = "Every add, edit, delete and done of these notes (admins)";
    const ol = el("ol", "list nt-audit-list");
    for (const a of rows) {
      let what = "";
      try {
        const b = a.before ? JSON.parse(a.before) : {}, f = a.after ? JSON.parse(a.after) : {};
        if (a.action === "edit") what = Object.keys(f).filter((k) => JSON.stringify(f[k]) !== JSON.stringify(b[k])).join(", ");
        if (a.action === "done") what = f.done || "not yet";
      } catch (e) { what = ""; }
      ol.append(el("li", "dim small", [`note ${a.note_id}`, a.action, what, a.operator, when(a.at)].filter(Boolean).join(" · ")));
    }
    box.append(sum, ol);
    return box;
  }
  function addBtn(host) {
    const b = el("button", "sm", "+ Note");
    b.type = "button";
    b.title = "A note with tags (anyone signed in)";
    b.addEventListener("click", () => { st.editing = "new"; draw(host); });
    return b;
  }
  function noteRow(host, n) {
    const li = el("li", "nt-note");
    li.dataset.id = String(n.id);
    const top = el("div", "nt-note-head");
    top.append(...n.tags.map(tagChip));
    if (n.done) {
      const dn = el("span", "chip nt-done", `done: ${n.done}`);
      dn.title = `${n.done_by} ${when(n.done_at)}`;
      top.append(dn);
    } else if (n.review) {
      const r = el("span", "chip nt-review", "review");
      r.title = "Tagged for a rule or model change, not done yet";
      top.append(r);
    }
    top.append(meta(n));
    li.append(top);
    if (n.text) li.append(el("div", "nt-text", n.text));
    const fx = fixes(n);
    if (fx) li.append(fx);
    const acts = el("div", "nt-acts");
    if (n.may_edit && (typeof canDo !== "function" || canDo("active"))) {
      const e = el("button", "sm quiet", "Edit");
      e.type = "button";
      e.addEventListener("click", () => { st.editing = n.id; draw(host); });
      const x = el("button", "sm quiet danger-quiet", "Delete");
      x.type = "button";
      x.addEventListener("click", async () => {
        if (window.UI && UI.confirm && !(await UI.confirm("Delete this note? Its tags leave the feedback too.", { ok: "Delete" }))) return;
        write(host, `/api/cad/intel/notes/${n.id}`, { delete: true }, "Note deleted");
      });
      acts.append(e, x);
    }
    if (n.place_name && (n.alias_ok || n.alias_why)) {   // admins: the heard words another name of the common place
      const a = el("button", "sm quiet", `+ Alias of ${n.place_name}`);
      a.type = "button";
      a.disabled = !n.alias_ok;                       // not allowed: why, in its hover (the hub says the same: 409 / 400)
      a.title = n.alias_ok ? `Add "${n.heard}" as another name of ${n.place_name} (Settings > Lists > Place names): the next transmission saying it resolves`
        : n.alias_why;
      a.addEventListener("click", async () => {
        if (window.UI && UI.confirm && !(await UI.confirm(`Add "${n.heard}" as a name of ${n.place_name}?`, { ok: "Add", danger: false }))) return;
        write(host, `/api/cad/intel/notes/${n.id}/alias`, {}, `Alias added to ${n.place_name}`);
      });
      acts.append(a);
    }
    if (n.place_name && n.alias_on) {                 // ... and off again (as Settings' Common places "−")
      const a = el("button", "sm quiet danger-quiet", "− Alias");
      a.type = "button";
      a.title = `Take "${n.heard}" off ${n.place_name}'s names`;
      a.addEventListener("click", async () => {
        if (window.UI && UI.confirm && !(await UI.confirm(`Take "${n.heard}" off ${n.place_name}'s names?`, { ok: "Remove" }))) return;
        write(host, `/api/cad/intel/notes/${n.id}/alias`, { remove: true }, `Alias removed from ${n.place_name}`);
      });
      acts.append(a);
    }
    if (st.data && st.data.admin) {                   // the review queue's "done / PR #" (admins)
      const i = el("input", "nt-done-in");
      Object.assign(i, { type: "text", maxLength: 120, value: n.done || "", placeholder: "Done / PR #" });
      i.setAttribute("aria-label", "Done: the PR or change that acted on it");
      i.title = "What acted on it (a PR, a rule): off the review queue; empty: not yet";
      i.addEventListener("change", () => write(host, `/api/cad/intel/notes/${n.id}/done`, { done: i.value.trim() },
        i.value.trim() ? "Marked done" : "Back in review"));
      acts.append(i);
    }
    if (acts.children.length) li.append(acts);
    return li;
  }
  function form(host, n) {
    const d = st.data;
    const f = el("form", "nt-form");
    const ta = el("textarea", "nt-ta");
    Object.assign(ta, { rows: 3, maxLength: 4000, value: n ? n.text : "", placeholder: "Note" });
    ta.setAttribute("aria-label", "Note");
    const tags = new Set(n ? n.tags : []);
    const tg = el("div", "chips nt-tags");
    tg.setAttribute("role", "group");
    tg.setAttribute("aria-label", "Tags");
    for (const t of d.tags) {
      const b = el("button", "chip nt-tag", TAGS[t] || t);
      b.type = "button";
      b.title = TIPS[t] || t;
      b.setAttribute("aria-pressed", String(tags.has(t)));
      b.addEventListener("click", () => { if (tags.has(t)) tags.delete(t); else tags.add(t); b.setAttribute("aria-pressed", String(tags.has(t))); });
      tg.append(b);
    }
    const cat = el("select");
    cat.setAttribute("aria-label", "Corrected category");
    cat.append(new Option("Category ok", ""), ...d.categories.map((c) => new Option(c.label, c.key)));
    cat.value = n ? n.corrected_category : "";
    const lv = el("select");
    lv.setAttribute("aria-label", "Corrected level");
    lv.append(new Option("Level ok", ""), ...d.levels.map((x) => new Option(x, x)));
    lv.value = n ? n.corrected_level : "";
    const fx = el("div", "row nt-fixrow");
    fx.append(cat, lv);
    const pl = placeBox(n);
    const save = el("button", "sm primary", "Save");
    save.type = "submit";
    const cancel = el("button", "sm quiet", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => { st.editing = null; draw(host); });
    const btns = el("div", "row nt-acts");
    btns.append(save, cancel);
    f.append(ta, tg, fx, pl.row, pl.geo, pl.sugg, btns);
    f.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const body = { text: ta.value.trim(), tags: [...tags], corrected_category: cat.value, corrected_level: lv.value,
                     ...pl.body() };
      write(host, n ? `/api/cad/intel/notes/${n.id}` : `/api/cad/intel/${st.id}/notes`, body, n ? "Note saved" : "Note added");
    });
    return f;
  }
  // The corrected place: [qualifier ▾] [the incident editor's location box] [Heard], its note and suggestions below.
  function placeBox(n) {
    const row = el("div", "locq nt-place");
    const qual = typeof qualSelect === "function" ? qualSelect(el("select", "loc-qual"), n ? n.corrected_qualifier : "") : null;
    if (qual) {
      qual.setAttribute("aria-label", "Where at the corrected place");
      qual.title = "Where at the place (or type IFO, across from … in front)";
      row.append(qual);
    }
    const input = el("input", "nt-place-in");
    Object.assign(input, { type: "text", maxLength: 200, spellcheck: false, placeholder: "Corrected place" });
    input.setAttribute("aria-label", "Corrected place");
    input.title = "Address, intersection or @ a common place: pick a suggestion to put it on the map";
    const heard = el("input", "nt-heard");
    Object.assign(heard, { type: "text", maxLength: 60, spellcheck: false, value: n ? n.heard || "" : "", placeholder: "Heard as" });
    heard.setAttribute("aria-label", "Heard as");
    heard.title = "The words the transcript has for the place (a misheard name): a labelled example, and a correction for speech-to-text";
    row.append(input, heard);
    const geo = el("div", "geo");                   // (addressBox sets its class: "geo span2", "warn")
    const sugg = el("ol", "sugg nt-sugg");
    sugg.id = `nt-sugg-${++st.boxes}`;
    sugg.hidden = true;
    sugg.setAttribute("role", "listbox");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", sugg.id);
    input.setAttribute("aria-expanded", "false");
    const c = n && n.corrected;
    const was = c ? { lat: c.lat, lon: c.lon, label: c.label, kind: "current", precision: c.precision, confidence: 1,
                      place_key: c.place_key } : null;
    const box = typeof addressBox === "function" ? addressBox(input, geo, sugg) : null;
    if (box) box.set(n ? n.corrected_place : "", was);
    else input.value = n ? n.corrected_place : "";
    if (qual && typeof qualTyped === "function") qualTyped(input, qual);
    function body() {
      const out = { corrected_place: input.value.trim(), corrected_qualifier: qual ? qual.value : "", heard: heard.value.trim() };
      const p = box ? box.picked : null;
      if (box && p !== was) {                         // taken, changed or cleared: else the note's pick as it was
        out.corrected_pick = p && p.lat != null
          ? { lat: p.lat, lon: p.lon, label: p.label, kind: p.kind, place_key: p.place_key || "" } : null;
      }
      return out;
    }
    return { row, geo, sugg, body };
  }
  async function write(host, path, body, ok) {
    try {
      await api("POST", path, { ...body, operator: typeof operator === "function" ? operator() : "" });
      if (window.UI) UI.toast(ok, "ok");
      st.editing = null;
      mount(host, st.id, true);
      if (window.PrealertInfo && PrealertInfo.reload) PrealertInfo.reload();     // its Place card: the corrected place
    } catch (e) {
      if (window.UI) UI.toast(e.message, "err");
    }
  }

  // ---- the incident view --------------------------------------------------------------------------------------------
  function incident(inc) {
    const sec = $("#d-intel");
    if (!sec) return;
    const s = st.inc;
    if (!inc || (typeof cad !== "undefined" && cad.state && !cad.state.intel)) {        // not this viewer's
      show(sec, false);
      return;
    }
    if (inc.id !== s.id) { s.id = inc.id; s.data = null; s.sig = ""; s.at = 0; }
    if (!s.loading && Date.now() - s.at > STALE_MS) load(inc.id);
    drawInc(sec);
  }
  async function load(id) {
    const s = st.inc;
    s.loading = true;
    s.at = Date.now();
    try {
      const d = await api("GET", `/api/cad/incident/${id}/intel`);
      if (s.id === id) { s.data = d; drawInc($("#d-intel")); }
    } catch (e) {
      if (s.id === id) s.data = { events: [], notes: [], error: e.message };
    } finally {
      s.loading = false;
    }
  }
  function drawInc(sec) {
    const s = st.inc;
    const d = s.data;
    const has = !!(d && d.notes && d.notes.length);
    show(sec, has);
    const sig = JSON.stringify([s.id, d, s.open]);
    if (!has || sig === s.sig) return;
    s.sig = sig;
    const tog = el("button", "sec-toggle", "");
    tog.type = "button";
    tog.setAttribute("aria-expanded", String(s.open));
    tog.append(el("span", "caret", s.open ? "▾" : "▸"), el("span", "h2", "Intel notes"),
      el("span", "dim sum", ` · ${d.notes.length}`));
    tog.title = "Notes on the radio intel events at this call";
    tog.addEventListener("click", () => { s.open = !s.open; drawInc(sec); });
    const kids = [tog];
    if (s.open) {
      const byEv = new Map(d.events.map((e) => [e.id, e]));
      const ol = el("ol", "list nt-notes");
      for (const n of d.notes) {
        const e = byEv.get(n.event_id) || {};
        const li = el("li", "nt-note");
        const top = el("div", "nt-note-head");
        const a = el("a", "nav mini quiet", `${e.label || "Intel"} ${when(e.at)}`);
        a.href = window.PrealertInfo ? PrealertInfo.href("intel", n.event_id) : `#prealert/intel/${n.event_id}`;
        a.title = `${e.level || ""} · ${e.status || ""}${e.place ? ` · ${e.place}` : ""}`;
        top.append(a, ...n.tags.map(tagChip), meta(n));
        li.append(top);
        if (n.text) li.append(el("div", "nt-text", n.text));
        const fx = fixes(n);
        if (fx) li.append(fx);
        ol.append(li);
      }
      kids.push(ol);
    }
    sec.replaceChildren(...kids);
  }

  // ---- the Intel page's chip ---------------------------------------------------------------------------------------
  function chip(n) {
    if (!n || !n.n) return null;
    const c = el("span", "chip nt-count" + (n.review ? " nt-review" : ""), `✎${n.n}`);
    c.title = `${n.n} note${n.n === 1 ? "" : "s"}` + (n.tags.length ? `: ${n.tags.map((t) => TAGS[t] || t).join(", ")}` : "")
      + (n.review ? ` · ${n.review} in review` : "");
    return c;
  }

  return { mount, incident, chip, TAGS };
})();
