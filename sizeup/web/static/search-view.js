// The CAD's Search view (#403, /cad#search?where=&text=&type=&unit=&disposition=&from=&to=&page=): past calls by address,
// street or place, words in the remarks, comments and transcripts, type, unit, disposition and dates. Newest first, 50
// at a time (GET /api/cad/search: the first page of each search is in the person's activity), each row opens the call;
// the page's calls on a map (a pin each, a click opens it); CSV for admins (GET /api/cad/search.csv). Address notes:
// the premise notes (GET /api/cad/premise), to add, edit, expire and restore (dispatchers and admins).
//
// Filters live in the URL hash, like the Radio view's, so Back goes to the last search. Uses cad.js's helpers (el,
// setText, show, api, operator, L, typeLabel, typeColor, nameOf, canDo, openFromRecords) and addr-history.js's
// noteForm: loaded after both.
"use strict";

window.SearchView = (() => {
  const KEYS = ["where", "text", "type", "unit", "disposition", "from", "to"];
  const INPUT = { where: "#sv-where", text: "#sv-text", type: "#sv-type", unit: "#sv-unit", disposition: "#sv-disp",
                  from: "#sv-from", to: "#sv-to" };
  const st = { q: {}, page: 0, tab: "calls", seq: 0, data: null, map: null, mapReady: false, listsSig: "", notes: null };
  const tz = () => (cad.scene && cad.scene.timezone) || "America/New_York";
  const when = (unix) => (unix ? new Intl.DateTimeFormat("en-US", { timeZone: tz(), year: "2-digit", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(unix * 1000)).replace(",", "") : "");

  function params(q, page) {
    const p = new URLSearchParams();
    for (const k of KEYS) if (q[k]) p.set(k, q[k]);
    if (page) p.set("page", String(page));
    return p;
  }

  // ---- the pick lists, from the state's lists and units ---------------------------------------------------------
  function fillLists() {
    const lists = L();
    const units = ((cad.state && cad.state.units) || []).map((u) => u.name).sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
    const sig = JSON.stringify([(lists.event_types || []).length, (lists.dispositions || []).length, units]);
    if (sig === st.listsSig) return;
    st.listsSig = sig;
    const fill = (sel, first, items) => {
      const v = sel.value;
      sel.replaceChildren(new Option(first, ""), ...items.map(([val, text]) => new Option(text, val)));
      sel.value = v;
    };
    fill($("#sv-type"), "Any type", (lists.event_types || []).filter((t) => !t.retired).map((t) => [t.code, t.name]));
    fill($("#sv-unit"), "Any unit", units.map((u) => [u, u]));
    fill($("#sv-disp"), "Any disposition", (lists.dispositions || []).filter((d) => !d.retired).map((d) => [d.code, d.name]));
  }

  // ---- open / close (cad.js route) -------------------------------------------------------------------------------
  function open(qs) {
    const p = new URLSearchParams(qs || "");
    st.q = Object.fromEntries(KEYS.map((k) => [k, p.get(k) || ""]));
    st.page = Math.max(0, Number(p.get("page")) || 0);
    st.tab = ["notes", "preplan"].includes(p.get("tab")) ? p.get("tab") : st.tab;
    fillLists();
    for (const k of KEYS) {
      const n = $(INPUT[k]);
      if (n.tagName === "SELECT" && st.q[k] && ![...n.options].some((o) => o.value === st.q[k])) n.append(new Option(st.q[k], st.q[k]));
      n.value = st.q[k];
    }
    tab(st.tab);
  }
  function close() { st.seq++; if (window.Preplan) Preplan.closeSearch(); }

  function submit(page = 0) {
    const q = Object.fromEntries(KEYS.map((k) => [k, $(INPUT[k]).value.trim()]));
    const h = "#search" + (params(q, page).toString() ? `?${params(q, page)}` : "");
    if (location.hash === h) { st.q = q; st.page = page; return run(); }
    location.hash = h;                         // route() -> open() -> run()
  }

  function tab(name) {
    st.tab = name;
    for (const b of document.querySelectorAll("#sv-tabs button")) b.setAttribute("aria-pressed", String(b.dataset.tab === name));
    show($("#sv-calls"), name === "calls");
    show($("#sv-notes"), name === "notes");
    show($("#sv-preplan"), name === "preplan");
    for (const id of ["#sv-text", "#sv-type", "#sv-unit", "#sv-disp", "#sv-from-l", "#sv-to-l", "#sv-csv"]) {
      if (name !== "calls") show($(id), false);         // the notes, the pre-plan points: by address only
      else if (id !== "#sv-csv") show($(id), true);
    }
    if (name !== "preplan" && window.Preplan) Preplan.closeSearch();
    if (name === "calls") run(); else if (name === "notes") runNotes(); else if (window.Preplan) Preplan.search(st.q.where);   // #456
  }

  // ---- calls ----------------------------------------------------------------------------------------------------
  async function run() {
    const seq = ++st.seq;
    const p = params(st.q, st.page);
    setText($("#sv-count"), "Searching…");
    try {
      const d = await api("GET", `/api/cad/search?${p}`);
      if (seq !== st.seq) return;
      st.data = d;
      render();
    } catch (e) {
      if (seq !== st.seq) return;
      setText($("#sv-count"), "");
      $("#sv-rows").replaceChildren(row0(`Search failed: ${e.message}`, 6));
    }
    const csv = $("#sv-csv");
    show(csv, canDo("admin"));
    csv.href = `/api/cad/search.csv?${params(st.q, 0)}`;
  }
  function row0(text, cols) {
    const tr = el("tr");
    tr.append(Object.assign(el("td", "empty", text), { colSpan: cols }));
    return tr;
  }
  function hit(text) {                       // "…smell of [smoke] in…": the words found, in bold
    const out = el("div", "sv-hit dim small");
    text.split(/\[([^\]]*)\]/).forEach((s, i) => out.append(i % 2 ? el("b", "", s) : s));
    return out;
  }
  function render() {
    const d = st.data;
    const from = d.page * d.size;
    setText($("#sv-count"), d.total ? `${d.total} call${d.total === 1 ? "" : "s"}` + (d.total > d.size ? ` · ${from + 1}–${from + d.rows.length}` : "")
      + (d.pending && d.pending.keys + d.pending.transcripts > 0 ? " · indexing" : "") : "None");
    $("#sv-count").title = d.pending && d.pending.keys + d.pending.transcripts > 0
      ? `Still indexing older records: ${d.pending.keys} addresses, ${d.pending.transcripts} transcripts` : "";
    const body = $("#sv-rows");
    if (!d.rows.length) body.replaceChildren(row0("No calls", 6));
    else body.replaceChildren(...d.rows.map((r) => {
      const tr = el("tr", "ev");
      tr.tabIndex = 0;
      tr.addEventListener("click", () => openFromRecords(r.id));
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === tr) openFromRecords(r.id); });
      const type = el("td");
      const t = el("span", "qt", typeLabel(r.type, r.subtype) || "—");
      if (r.type) t.style.setProperty("--c", typeColor(r.type) || "#9AA3AD");
      type.append(t);
      const loc = el("td", "loc", r.address || "—");
      if (r.addr_label && r.addr_label !== r.address) loc.title = r.addr_label;
      if (r.hit) loc.append(hit(r.hit));
      tr.append(el("td", "t", when(r.created_at)), el("td", "num", r.number), type, loc,
        el("td", "", r.disposition ? nameOf("dispositions", r.disposition) : r.status === "closed" ? "" : "Open"),
        el("td", "", (r.units || []).join(", ")));
      return tr;
    }));
    const pages = Math.max(1, Math.ceil(d.total / d.size));
    setText($("#sv-page"), d.total > d.size ? `Page ${d.page + 1} of ${pages}` : "");
    $("#sv-prev").disabled = d.page <= 0;
    $("#sv-next").disabled = d.page + 1 >= pages;
    show($("#sv-prev").parentElement, d.total > d.size);
    pins(d.rows);
  }

  // ---- the map: the page's calls, a pin each -------------------------------------------------------------------
  function pins(rows) {
    const pts = rows.filter((r) => r.lat != null && r.lon != null);
    show($("#sv-nomap"), !pts.length);
    if (!window.maplibregl || typeof buildMapStyle !== "function" || !cad.scene || !cad.scene.home) return;
    if (!st.map) {
      st.map = new maplibregl.Map({ container: $("#sv-map"), style: buildMapStyle(cad.scene), center: cad.scene.home.center,
                                    zoom: cad.scene.home.zoom, attributionControl: { compact: true }, maxPitch: 0, dragRotate: false });
      st.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      st.map.on("load", () => {
        st.map.addSource("sv-calls", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        st.map.addLayer({ id: "sv-calls", type: "circle", source: "sv-calls", paint: {
          "circle-radius": 7, "circle-color": ["get", "c"], "circle-stroke-width": 2, "circle-stroke-color": "#0B0F14" } });
        st.map.on("click", "sv-calls", (e) => { const f = e.features && e.features[0]; if (f) openFromRecords(f.properties.id); });
        st.map.on("mouseenter", "sv-calls", () => { st.map.getCanvas().style.cursor = "pointer"; });
        st.map.on("mouseleave", "sv-calls", () => { st.map.getCanvas().style.cursor = ""; });
        st.mapReady = true;
        pins(st.data ? st.data.rows : []);
      });
      return;
    }
    if (!st.mapReady) return;
    st.map.resize();
    st.map.getSource("sv-calls").setData({ type: "FeatureCollection", features: pts.map((r) => ({ type: "Feature",
      properties: { id: r.id, c: typeColor(r.type) || "#9AA3AD" }, geometry: { type: "Point", coordinates: [r.lon, r.lat] } })) });
    if (pts.length) {
      const b = new maplibregl.LngLatBounds();
      for (const r of pts) b.extend([r.lon, r.lat]);
      st.map.fitBounds(b, { padding: 40, maxZoom: 16.5, duration: 0 });
    }
  }

  // ---- address notes -----------------------------------------------------------------------------------------------
  async function runNotes() {
    const seq = ++st.seq;
    const p = new URLSearchParams();
    if (st.q.where) p.set("where", st.q.where);
    if ($("#sv-notes-all").checked) p.set("all", "1");
    show($("#sv-note-add"), canDo("dispatch"));
    try {
      const d = await api("GET", `/api/cad/premise?${p}`);
      if (seq !== st.seq) return;
      st.notes = d.notes;
      setText($("#sv-count"), `${d.notes.length} note${d.notes.length === 1 ? "" : "s"}`);
      renderNotes();
    } catch (e) {
      if (seq === st.seq) $("#sv-note-rows").replaceChildren(row0(`Not loaded: ${e.message}`, 6));
    }
  }
  function renderNotes() {
    const A = window.AddrHistory;
    const body = $("#sv-note-rows");
    if (!st.notes.length) return body.replaceChildren(row0("No address notes", 6));
    body.replaceChildren(...st.notes.map((n) => {
      const tr = el("tr", "ev" + (n.active ? "" : " retired"));
      const act = el("td", "act");
      if (canDo("dispatch")) act.append(UI.menu.button(`Address note: ${n.label}`, () => [
        { label: "Edit", run: () => A.noteForm($("#sv-note-form"), { note: n }, runNotes) },
        n.active ? { label: "Expire", hint: "no longer shown", run: () => A.expire(n, runNotes) }
          : { label: "Restore", run: () => restore(n) },
      ], { key: `svn:${n.id}` }));
      const note = el("td", "pn-text");
      note.append(el("span", `chip pn-cat ${n.category}`, A.CATS[n.category] || n.category), " ", n.text);
      const review = el("td", n.review_due ? "warn" : "", A.day(n.review_at));
      tr.append(el("td", "loc", n.label), note, el("td", "", A.PRIO[n.priority] || ""), review,
        el("td", "dim", `${n.author_name || ""} ${A.day(n.updated_at)}`.trim()), act);
      return tr;
    }));
  }
  async function restore(n) {
    try {
      await api("POST", `/api/cad/premise/${n.id}`, { action: "restore", operator: operator() });
      runNotes();
    } catch (e) { UI.toast(e.message, "err"); }
  }

  // ---- wiring ------------------------------------------------------------------------------------------------------
  $("#sv-filters").addEventListener("submit", (e) => { e.preventDefault(); if (st.tab !== "calls") { st.q.where = $("#sv-where").value.trim(); tab(st.tab); } else submit(0); });
  $("#sv-clear").addEventListener("click", () => { for (const k of KEYS) $(INPUT[k]).value = ""; submit(0); });
  $("#sv-prev").addEventListener("click", () => submit(Math.max(0, st.page - 1)));
  $("#sv-next").addEventListener("click", () => submit(st.page + 1));
  for (const b of document.querySelectorAll("#sv-tabs button")) b.addEventListener("click", () => tab(b.dataset.tab));
  $("#sv-notes-all").addEventListener("change", runNotes);
  $("#sv-note-add").addEventListener("click", () => window.AddrHistory.noteForm($("#sv-note-form"), { where: $("#sv-where").value.trim() }, runNotes));
  cad.hooks.push(() => { if (cad.search) fillLists(); });     // the lists and units, once the state has them

  return { open, close };
})();
