// The incident card's address parts (#403): the premise notes at its building, at the top of the card, and History
// here, under the facts. cad.js renderDetail calls AddrHistory.card(inc, {readonly}) on every render.
//
//   Notes        an open call's come in the state push (inc.premise: ranked by priority, then how much the category
//                matters to the call type, then the newest), so they show as soon as an address is set or changed; a
//                closed one's with History here. Each: category, the note, priority / who / when, and Seen (who
//                acknowledged it on this call; an edit asks again). ⋯ Edit / Expire for dispatchers and admins.
//   History here GET /api/cad/incident/<id>/here, again when the call's building, pin, status or disposition changes:
//                the last 5 calls at the same building (date · type · disposition · units, each opens the call), the
//                last 12 months by type, the repeat false alarms. Folded or open as last left (localStorage, try/catch).
//   Note form    noteForm(host, {note | incident_id | where, text}, done): add or edit a note; also the comment box's
//                "Save as address note" (what is typed there) and the Search page's Address notes.
// Uses cad.js's helpers (el, setText, show, api, operator, typeLabel, nameOf, openFromRecords, UI): loaded after it.
"use strict";

window.AddrHistory = (() => {
  const OPEN_KEY = "sizeup.cad.hereOpen";
  const CATS = { hazard: "Hazard", access: "Access/Knox", medical: "Medical/O2", animals: "Animals", hoarding: "Hoarding", other: "Other" };
  const PRIO = { 1: "High", 2: "Normal", 3: "Low" };
  const st = { iid: null, sig: "", here: null, seq: 0, open: remembered(), notesSig: "", hereSig: "", readonly: true, inc: null };

  function remembered() {
    try { return localStorage.getItem(OPEN_KEY) === "1"; } catch (_) { return false; }
  }
  function remember(on) {
    try { localStorage.setItem(OPEN_KEY, on ? "1" : "0"); } catch (_) { /* this page view only */ }
  }
  const tz = () => (cad.scene && cad.scene.timezone) || "America/New_York";
  const dayFmt = () => new Intl.DateTimeFormat("en-US", { timeZone: tz(), year: "2-digit", month: "2-digit", day: "2-digit" });
  const isoDay = (unix) => (unix ? new Date(unix * 1000).toLocaleDateString("en-CA", { timeZone: tz() }) : "");
  const day = (unix) => (unix ? dayFmt().format(new Date(unix * 1000)) : "");
  const who = (s) => (s || "").replace(/\s*<[^<>]*>\s*$/, "");
  const located = (inc) => !!(inc && (inc.address || inc.lat != null));

  // ---- the card ---------------------------------------------------------------------------------------------------
  function card(inc, opts = {}) {
    st.inc = inc;
    st.readonly = !!opts.readonly;
    if (inc.id !== st.iid) {
      st.iid = inc.id;
      st.here = null;
      st.allNotes = false;
      closeForm($("#d-premise-form"));
    }
    const sig = JSON.stringify([inc.id, inc.bldg_key || "", inc.addr_key || "", inc.lat, inc.lon, inc.status, inc.disposition]);
    if (sig !== st.sig) {
      st.sig = sig;
      if (located(inc)) load(inc.id);
      else st.here = null;
    }
    renderNotes(inc);
    renderHere(inc);
    show($("#note-premise"), !st.readonly && located(inc));
  }

  async function load(iid) {
    const seq = ++st.seq;
    try {
      const h = await api("GET", `/api/cad/incident/${iid}/here`);
      if (seq !== st.seq || st.iid !== iid) return;
      st.here = h;
    } catch (e) {
      if (seq !== st.seq) return;
      st.here = { error: e.message };
    }
    if (st.inc && st.inc.id === iid) { renderNotes(st.inc); renderHere(st.inc); }
  }
  function reload() {
    st.sig = "";
    if (st.inc) card(st.inc, { readonly: st.readonly });
  }

  // ---- premise notes on the card ------------------------------------------------------------------------------------
  // An open call's notes come in the push (what everyone may see, live); History here's add any of a category kept to
  // some roles (premise.VISIBLE_TO). The push's own copy of a note wins (its acknowledgement is the newer).
  function notesOf(inc) {
    const here = (st.here && st.here.notes) || [];
    if (!Array.isArray(inc.premise)) return here;
    const ids = new Set(inc.premise.map((n) => n.id));
    return inc.premise.concat(here.filter((n) => n.restricted && !ids.has(n.id)));
  }
  const FOLD_AT = 3;                          // more notes than this: the first ones, then "N more"
  function renderNotes(inc) {
    const box = $("#d-premise");
    const notes = notesOf(inc);
    const sig = JSON.stringify([inc.id, notes, st.readonly, st.allNotes]);
    show(box, notes.length > 0);
    if (sig === st.notesSig) return;
    st.notesSig = sig;
    const shown = st.allNotes || notes.length <= FOLD_AT + 1 ? notes : notes.slice(0, FOLD_AT);
    const rows = shown.map((n) => noteRow(n, inc));
    if (notes.length > shown.length || (st.allNotes && notes.length > FOLD_AT + 1)) {
      const b = el("button", "sm quiet", st.allNotes ? "Fewer" : `${notes.length - shown.length} more note${notes.length - shown.length > 1 ? "s" : ""}`);
      b.type = "button";
      b.addEventListener("click", () => { st.allNotes = !st.allNotes; renderNotes(inc); });
      rows.push(b);
    }
    box.replaceChildren(...rows);
  }
  function noteRow(n, inc) {
    const row = el("div", `pn p${n.priority}` + (n.ack ? " seen" : ""));
    const cat = el("span", `chip pn-cat ${n.category}`, CATS[n.category] || n.category);
    const text = el("span", "pn-text", n.text);
    const meta = [n.priority === 1 ? "High" : "", `${n.author_name || who(n.updated_by) || "?"} ${day(n.updated_at)}`].filter(Boolean).join(" · ");
    const m = el("span", "pn-meta dim small", meta);
    m.title = `Added ${day(n.created_at)}` + (n.updated_by ? `, last changed by ${who(n.updated_by)}` : "")
      + (n.review_at ? `; review by ${day(n.review_at)}` : "") + (n.expires_at ? `; expires ${day(n.expires_at)}` : "");
    row.append(cat, text, m);
    if (n.review_due) row.append(el("span", "chip pn-due", "Review due"));
    if (n.ack) {
      const a = el("span", "pn-acked dim small", `✓ ${n.ack.by || "Seen"} ${fmtTime(n.ack.at)}`);
      a.title = "Acknowledged on this call";
      row.append(a);
    } else if (!st.readonly && (inc.status === "queued" || inc.status === "pending" || inc.status === "active")) {
      const b = el("button", "sm", "Seen");
      b.type = "button";
      b.title = "Acknowledge on this call";
      b.addEventListener("click", () => ack(n, inc));
      row.append(b);
    }
    if (!st.readonly) row.append(UI.menu.button(`Address note: ${CATS[n.category] || ""}`, () => [
      { label: "Edit", run: () => noteForm($("#d-premise-form"), { note: n }, reload) },
      { label: "Expire", hint: "no longer shown", run: () => expire(n, reload) },
    ], { key: `pn:${n.id}` }));
    return row;
  }
  async function ack(n, inc) {
    try {
      await api("POST", `/api/cad/premise/${n.id}/ack`, { incident_id: inc.id, operator: operator() });
      if (!Array.isArray(inc.premise)) reload();             // a closed call's: no push carries it
    } catch (e) { UI.toast(e.message, "err"); }
  }
  async function expire(n, done) {
    try {
      await api("POST", `/api/cad/premise/${n.id}`, { action: "expire", operator: operator() });
      UI.toast("Address note expired");
      if (done) done();
    } catch (e) { UI.toast(e.message, "err"); }
  }

  // ---- History here -------------------------------------------------------------------------------------------------
  function renderHere(inc) {
    const sec = $("#d-here");
    show(sec, located(inc));
    if (!located(inc)) return;
    const h = st.here;
    const tog = $("#d-here-toggle");
    tog.setAttribute("aria-expanded", String(st.open));
    show($("#d-here-body"), st.open);
    show($("#d-here-note"), !st.readonly);
    const sig = JSON.stringify([inc.id, h, st.open]);
    if (sig === st.hereSig) return;
    st.hereSig = sig;
    let sum = "…";
    if (h && h.error) sum = "· not loaded";
    else if (h && !h.key.bldg_key && !h.key.addr_how) sum = "· no address";
    else if (h) {
      sum = h.total ? `· ${h.year.n} in 12 mo` + (h.total > h.year.n ? ` · ${h.total} all` : "") : "· none";
      if (h.false_alarms.n) sum += ` · ${h.false_alarms.n} false alarm${h.false_alarms.n > 1 ? "s" : ""}`;
      sum += { geo: " · by map point, 20 m", text: " · as typed" }[h.key.addr_how] || "";    // a weak match says so
    }
    setText($("#d-here-sum"), sum);
    tog.classList.toggle("warn", !!(h && h.false_alarms && h.false_alarms.n >= 2));
    tog.title = h && h.key ? `${h.key.addr_label || ""}` + ({ geo: " (by map point: calls within 20 m)", text: " (as typed)" }[h.key.addr_how] || "") : "";
    const body = $("#d-here-body");
    if (!h || h.error || !h.total) {
      body.replaceChildren(el("div", "dim small", h && h.error ? `Not loaded: ${h.error}` : h ? "No other calls here" : "Loading"));
      return;
    }
    const list = el("ol", "here-list");
    for (const r of h.recent) {
      const li = el("li");
      const b = el("button", "here-call", day(r.created_at));
      b.type = "button";
      b.title = `${r.number}: open`;
      b.addEventListener("click", () => openFromRecords(r.id));
      li.append(b, el("span", "", typeLabel(r.type, r.subtype) || "—"),
        el("span", r.disposition ? "" : "dim", r.disposition ? nameOf("dispositions", r.disposition) : (r.status === "closed" ? "—" : "Open")),
        el("span", "dim", (r.units || []).join(", ")));
      list.append(li);
    }
    const parts = [list];
    if (h.year.by_type.length) {
      const c = el("div", "here-counts small");
      c.append(el("span", "dim", "12 mo "), h.year.by_type.map((t) => `${nameOf("event_types", t.type)} ${t.n}`).join(" · "));
      parts.push(c);
    }
    if (h.false_alarms.n) {
      const f = el("div", "here-false small" + (h.false_alarms.n >= 2 ? " warn" : ""));
      f.append(el("span", "", `False alarms 12 mo: ${h.false_alarms.n}`), el("span", "dim", ` · ${h.false_alarms.calls.map((x) => day(x.created_at)).join(", ")}`));
      parts.push(f);
    }
    body.replaceChildren(...parts);
  }

  // ---- the note form ------------------------------------------------------------------------------------------------
  function closeForm(host) {
    if (!host) return;
    host.replaceChildren();
    show(host, false);
  }
  // opts: {note} edit | {incident_id, text} from a call | {where} from the Search page (an address to type)
  function noteForm(host, opts, done) {
    const n = opts.note || null;
    const f = el("form", "pn-form card");
    f.autocomplete = "off";
    const head = el("div", "pn-form-head");
    head.append(el("b", "", n ? "Edit address note" : "Address note"), el("span", "dim small", n ? n.label : opts.label || ""));
    f.append(head);
    let where = null;
    if (!n && opts.incident_id == null) {
      where = el("input");
      Object.assign(where, { type: "text", maxLength: 200, placeholder: "Address (123 Example Ave)", value: opts.where || "", spellcheck: false });
      where.setAttribute("aria-label", "Address");
      f.append(where);
    }
    const row = el("div", "row pn-fields");
    const cat = el("select");
    cat.setAttribute("aria-label", "Category");
    for (const [k, v] of Object.entries(CATS)) cat.append(new Option(v, k));
    cat.value = n ? n.category : "hazard";
    const prio = el("select");
    prio.setAttribute("aria-label", "Priority");
    for (const [k, v] of Object.entries(PRIO)) prio.append(new Option(v, k));
    prio.value = String(n ? n.priority : 2);
    const review = Object.assign(el("input"), { type: "date", value: n ? isoDay(n.review_at) : "" });
    const expires = Object.assign(el("input"), { type: "date", value: n ? isoDay(n.expires_at) : "" });
    const lab = (text, input, tip) => { const l = el("label", "inline"); l.append(el("span", "dim", text), " ", input); l.title = tip; return l; };
    row.append(cat, prio, lab("Review by", review, "Shown as due from this day"), lab("Expires", expires, "Not shown from this day (optional)"));
    const text = el("textarea");
    Object.assign(text, { rows: 2, maxLength: 500, placeholder: "What crews should know", value: n ? n.text : opts.text || "" });
    text.setAttribute("aria-label", "Note");
    const err = el("span", "error");
    const save = el("button", "primary", "Save");
    save.type = "submit";
    const cancel = el("button", "", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => closeForm(host));
    const foot = el("div", "row");
    foot.append(err, el("span", "spacer"), cancel, save);
    f.append(row, text, foot);
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { text: text.value, category: cat.value, priority: Number(prio.value), review_at: review.value || null,
                     expires_at: expires.value || null, operator: operator() };
      try {
        if (n) await api("POST", `/api/cad/premise/${n.id}`, body);
        else await api("POST", "/api/cad/premise", { ...body, ...(where ? { where: where.value } : { incident_id: opts.incident_id }) });
        closeForm(host);
        UI.toast(n ? "Address note saved" : "Address note added");
        if (done) done();
      } catch (ex) { setText(err, ex.message); }
    });
    host.replaceChildren(f);
    show(host, true);
    (where && !where.value ? where : text).focus();
  }

  function forIncident(text) {
    const inc = st.inc;
    if (!inc) return;
    noteForm($("#d-premise-form"), { incident_id: inc.id, text, label: inc.address }, () => {
      if (text && $("#note-text").value === text) $("#note-text").value = "";
      reload();
    });
  }

  $("#d-here-toggle").addEventListener("click", () => {
    st.open = !st.open;
    remember(st.open);
    if (st.inc) renderHere(st.inc);
  });
  $("#d-here-note").addEventListener("click", () => forIncident(""));
  $("#note-premise").addEventListener("click", () => forIncident($("#note-text").value.trim()));

  return { card, noteForm, expire, CATS, PRIO, day, isoDay };
})();
