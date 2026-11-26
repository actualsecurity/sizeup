// The CAD's Search view (#403, /cad#search?where=&text=&type=&unit=&disposition=&from=&to=&page=): past calls by address,
// street or place, words in the remarks, comments and transcripts, type, unit, disposition and dates. Newest first, 50
// at a time (GET /api/cad/search: the first page of each search is in the person's activity), each row opens the call;
// the page's calls on a map (a pin each, a click opens it); CSV for admins (GET /api/cad/search.csv). Address notes:
// the premise notes (GET /api/cad/premise), to add, edit, expire and restore (dispatchers and admins). Hydrants (#840):
// the box's words (number, street, asset id, OOS, class C, untested, town) as typed (GET /api/cad/hydrants/search, the
// first HY_LIMIT), each row's Show on map (hydrants.js); #search?where=...&tab=hydrants.
//
// Intel events (#937): opt-in (the Intel button, off unless turned on; the person's, on the hub: me-prefs.js "search",
// localStorage IKEY this browser's copy), only for whoever the state gives intel to (the Intel page's readers). Search's
// time, area and words over the events kept (GET /api/cad/intel/search, sizeup/intel/eventlog.py: bounded and on its
// index); with a type, unit or disposition, none (calls only). Their own table, marked Intel with the level, category
// and why it didn't push; their pins (intel-map.js) on the same map; a row or pin opens its More info
// (/cad#prealert/intel/<id>). Drawn again only when the answer changed.
//
// Filters live in the URL hash, like the Radio view's, so Back goes to the last search. Uses cad.js's helpers (el,
// setText, show, api, operator, L, typeLabel, typeColor, nameOf, canDo, openFromRecords) and addr-history.js's
// noteForm: loaded after both.
"use strict";

window.SearchView = (() => {
  const KEYS = ["where", "text", "type", "unit", "disposition", "from", "to"];
  const INPUT = { where: "#sv-where", text: "#sv-text", type: "#sv-type", unit: "#sv-unit", disposition: "#sv-disp",
                  from: "#sv-from", to: "#sv-to" };
  const st = { q: {}, page: 0, tab: "calls", seq: 0, data: null, map: null, mapReady: false, listsSig: "", notes: null, hyTimer: null,
               intel: false, iseq: 0, idata: null, isig: "", ipins: null };
  const IKEY = "sizeup.searchIntel";
  const TABS = ["notes", "preplan", "hydrants"];
  const HY_LIMIT = 50;
  const WHERE_PH = { hydrants: "Number, street, asset id, OOS, class C, untested, town" };
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
    st.tab = TABS.includes(p.get("tab")) ? p.get("tab") : st.tab;
    fillLists();
    sizer();
    for (const k of KEYS) {
      const n = $(INPUT[k]);
      if (n.tagName === "SELECT" && st.q[k] && ![...n.options].some((o) => o.value === st.q[k])) n.append(new Option(st.q[k], st.q[k]));
      n.value = st.q[k];
    }
    tab(st.tab);
  }
  function close() { st.seq++; st.iseq++; if (window.Preplan) Preplan.closeSearch(); }

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
    show($("#sv-hydrants"), name === "hydrants");
    if (name !== "hydrants" && /[?&]tab=hydrants/.test(location.hash)) {       // (the hydrants' own address, #840: left)
      const p = new URLSearchParams(location.hash.slice("#search".length).replace(/^\?/, ""));
      p.delete("tab");
      history.replaceState(null, "", "#search" + (p.toString() ? `?${p}` : ""));
    }
    const where = $("#sv-where"), ph = WHERE_PH[name] || "Address, street or place";
    where.placeholder = ph;
    where.setAttribute("aria-label", ph);
    for (const id of ["#sv-text", "#sv-type", "#sv-unit", "#sv-disp", "#sv-from-l", "#sv-to-l", "#sv-csv"]) {
      if (name !== "calls") show($(id), false);         // the notes, the pre-plan points: by address only
      else if (id !== "#sv-csv") show($(id), true);
    }
    if (name !== "preplan" && window.Preplan) Preplan.closeSearch();
    intelButton();
    if (name === "calls") run(); else if (name === "notes") runNotes(); else if (name === "hydrants") runHydrants();
    else if (window.Preplan) Preplan.search(st.q.where);   // #456
  }

  // ---- calls ----------------------------------------------------------------------------------------------------
  async function run() {
    runIntel();
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

  // ---- hydrants (#840): by number, street, asset id, status, class, untested, town -----------------------------------
  async function runHydrants() {
    const seq = ++st.seq;
    const q = (st.q.where || "").trim();
    const p = params({ ...st.q, where: q }, 0);                       // (the calls' filters kept in the address)
    p.set("tab", "hydrants");
    const h = `#search?${p}`;
    if (location.hash !== h) history.replaceState(null, "", h);         // reload, Back from the map: here again
    const body = $("#sv-hy-rows");
    if (!q) { setText($("#sv-count"), ""); body.replaceChildren(row0(WHERE_PH.hydrants, 6)); return; }
    try {
      const d = await api("GET", `/api/cad/hydrants/search?q=${encodeURIComponent(q)}&limit=${HY_LIMIT}`);
      if (seq !== st.seq) return;
      const n = d.total;
      setText($("#sv-count"), [n ? `${n} hydrant${n === 1 ? "" : "s"}` + (n > d.rows.length ? ` · first ${d.rows.length}` : "") : "None",
                               ...(d.filters || [])].join(" · "));
      body.replaceChildren(...(d.rows.length && window.Hydrants ? d.rows.map((r) => Hydrants.resultRow(r)) : [row0("No hydrants", 6)]));
      if (window.Hydrants) Hydrants.refocus(body);                    // back from Show on map: its button again
    } catch (e) {
      if (seq !== st.seq) return;
      setText($("#sv-count"), "");
      body.replaceChildren(row0(`Search failed: ${e.message}`, 6));
    }
  }

  // ---- the map: the page's calls, a pin each -------------------------------------------------------------------
  function pins(rows) {
    const pts = rows.filter((r) => r.lat != null && r.lon != null);
    show($("#sv-nomap"), !pts.length);
    if (!window.maplibregl || typeof plainMap !== "function" || !cad.scene || !cad.scene.home) return;
    if (!st.map) {
      st.map = plainMap($("#sv-map"), cad.scene);
      st.map.on("load", () => {
        st.map.addSource("sv-calls", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        st.map.addLayer({ id: "sv-calls", type: "circle", source: "sv-calls", paint: {
          "circle-radius": 7, "circle-color": ["get", "c"], "circle-stroke-width": 2, "circle-stroke-color": "#0B0F14" } });
        st.map.on("click", "sv-calls", (e) => { const f = e.features && e.features[0]; if (f) openFromRecords(f.properties.id); });
        st.map.on("mouseenter", "sv-calls", () => { st.map.getCanvas().style.cursor = "pointer"; });
        st.map.on("mouseleave", "sv-calls", () => { st.map.getCanvas().style.cursor = ""; });
        if (window.IntelMap) st.ipins = IntelMap.attach(st.map, "sv-intel", { pick: openIntel });
        st.mapReady = true;
        intelPins();
        pins(st.data ? st.data.rows : []);
      });
      return;
    }
    if (!st.mapReady) return;
    st.map.resize();
    st.map.getSource("sv-calls").setData({ type: "FeatureCollection", features: pts.map((r) => ({ type: "Feature",
      properties: { id: r.id, c: typeColor(r.type) || "#9AA3AD" }, geometry: { type: "Point", coordinates: [r.lon, r.lat] } })) });
    fit();
  }
  // the calls' pins and the intel events' (when on) in view
  function fit() {
    const pts = [...(st.data ? st.data.rows : []), ...intelPts()].filter((r) => r.lat != null && r.lon != null);
    show($("#sv-nomap"), !pts.length);
    if (!st.mapReady || !pts.length) return;
    const b = new maplibregl.LngLatBounds();
    for (const r of pts) b.extend([r.lon, r.lat]);
    st.map.fitBounds(b, { padding: 40, maxZoom: 16.5, duration: 0 });
  }

  // ---- Intel events (#937): opt-in, for the Intel page's readers -------------------------------------------------------
  const intelOk = () => !!(cad.state && cad.state.intel);
  const intelOn = () => st.intel && intelOk();
  function intelButton() {
    const b = $("#sv-intel");
    show(b, intelOk() && st.tab === "calls");
    b.setAttribute("aria-pressed", String(st.intel));
  }
  function keepIntel(on) {
    st.intel = !!on;
    try { localStorage.setItem(IKEY, on ? "1" : "0"); } catch (_) { /* this page view only */ }
    intelButton();
  }
  function openIntel(id) { location.hash = window.PrealertInfo ? PrealertInfo.href("intel", id) : `#prealert/intel/${id}`; }
  const CALL_ONLY = ["type", "unit", "disposition"];
  async function runIntel() {
    const seq = ++st.iseq;
    const box = $("#sv-intel-box");
    if (!intelOn() || st.tab !== "calls") {
      show(box, false);
      st.idata = null;
      st.isig = "";
      intelPins();
      return;
    }
    show(box, true);
    if (CALL_ONLY.some((k) => st.q[k])) { renderIntel({ events: [], total: 0, calls_only: true }); return; }
    const p = new URLSearchParams();
    for (const k of ["where", "text", "from", "to"]) if (st.q[k]) p.set(k, st.q[k]);
    if (!st.idata) setText($("#sv-intel-n"), "Searching…");
    try {
      const d = await api("GET", `/api/cad/intel/search?${p}`);
      if (seq === st.iseq) renderIntel(d);
    } catch (e) {
      if (seq !== st.iseq) return;
      st.isig = "";
      setText($("#sv-intel-n"), "");
      $("#sv-intel-rows").replaceChildren(row0(`Intel: ${e.message}`, 5));
    }
  }
  function renderIntel(d) {
    const sig = JSON.stringify(d);
    st.idata = d;
    if (sig === st.isig) return;               // the same answer: nothing redrawn
    st.isig = sig;
    const n = $("#sv-intel-n");
    setText(n, d.calls_only ? "Not with a type, unit or disposition" : d.total
      ? `${d.total} event${d.total === 1 ? "" : "s"}` + (d.total > d.events.length ? ` · newest ${d.events.length}` : "") : "None");
    n.title = d.capped ? `The newest ${d.scanned} events of the range looked through` : "";
    const body = $("#sv-intel-rows");
    if (!d.events.length) body.replaceChildren(row0(d.calls_only ? "Calls only" : "No intel events", 5));
    else body.replaceChildren(...d.events.map(intelRow));
    intelPins();
  }
  function intelRow(e) {
    const tr = el("tr", "ev sv-irow");
    tr.tabIndex = 0;
    tr.dataset.id = String(e.id);
    if ((e.matched || []).length) tr.title = `Words in: ${e.matched.join(", ")}`;
    tr.addEventListener("click", () => openIntel(e.id));
    tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target === tr) openIntel(e.id); });
    const cat = el("td", "il-cat");
    cat.append(el("span", "chip sv-ichip", "Intel"), " ", e.label || e.category);
    cat.title = `${e.pack_label} · ${e.category}`;
    const lv = el("span", `chip il-lv il-lv-${e.level}`, e.level);
    const loc = e.place ? el("td", "loc", e.confirmed ? e.place : `${e.place} ?`) : el("td", "loc dim", "unplaced");
    if (e.place && !e.confirmed) loc.title = "Unconfirmed";
    const why = e.held ? el("td", "il-why") : el("td", "il-why dim", "—");
    if (e.held) { why.append(el("span", "chip il-held", e.held.label)); why.title = e.held.tip || ""; }
    const lvc = el("td", "il-level");
    lvc.append(lv);
    tr.append(el("td", "t", when(e.at)), cat, lvc, loc, why);
    return tr;
  }
  const intelPts = () => (st.idata && intelOn() ? st.idata.events.filter((e) => e.lat != null && e.lon != null) : []);
  function intelPins() {                       // drawn only when they differ (intel-map.js); the view fitted then
    if (!st.ipins || !st.mapReady) return;
    if (st.ipins.set(intelPts())) fit();
  }
  {
    try { st.intel = localStorage.getItem(IKEY) === "1"; } catch (_) { st.intel = false; }
    if (window.MePrefs) {
      let had = false;
      try { had = localStorage.getItem(IKEY) !== null; } catch (_) { /* none */ }
      MePrefs.register("search", { get: () => ({ intel: st.intel }), hadLocal: had, apply: (v) => {
        const was = intelOn();
        keepIntel(!!(v && v.intel));
        if (intelOn() !== was && cad.search && st.tab === "calls") runIntel();
      } });
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

  // ---- the map's height: dragged by its bottom edge, like the incident's map (incident-map.js's handle, Panels.separator)
  // Remembered per viewer: localStorage "sizeup.searchMap" {h} (px, in try/catch); null = the layout's own (cad.css).
  // Kept within H_MIN px and 85% of the window, again when the window changes. MapLibre's resize() once a frame while
  // dragging and once after. clampH() is the arithmetic (pure: tests/test_search_map.py).
  const H_KEY = "sizeup.searchMap", H_MIN = 160, H_MAX_VH = 0.85;
  function clampH(want, vh) {
    if (want == null || !Number.isFinite(want) || want <= 0) return null;
    const hi = Math.max(H_MIN, Math.floor(vh * H_MAX_VH));
    return Math.round(Math.max(H_MIN, Math.min(hi, want)));
  }
  if (typeof document === "undefined") return { clampH, H_KEY, H_MIN, H_MAX_VH };

  function savedH() {
    try {
      const v = (JSON.parse(localStorage.getItem(H_KEY) || "{}") || {}).h;
      return Number.isFinite(v) && v > 0 ? v : null;
    } catch (_) { return null; }
  }
  function rememberH() {
    try { localStorage.setItem(H_KEY, JSON.stringify({ h: sz.h })); } catch (_) { /* this page view only */ }
  }
  const sz = { h: savedH(), sep: null, frame: 0 };
  const box = () => $("#sv-mapbox");
  function sizeMap() {
    const h = clampH(sz.h, innerHeight);
    if (h == null) box().style.removeProperty("--sv-map-h"); else box().style.setProperty("--sv-map-h", `${h}px`);
    if (sz.sep) sz.sep.sync();
  }
  function mapResize() { if (st.map) { try { st.map.resize(); } catch (_) { /* gone */ } } }
  function sizer() {                         // panels.js loads after this file: made on the first open, or once loaded
    if (sz.sep || !window.Panels || !Panels.separator) return;
    sz.sep = Panels.separator($("#sv-map-h"), {
      orient: "horizontal", grow: 1, label: "Resize the map", controls: "sv-mapbox",
      min: () => H_MIN, max: () => Math.max(H_MIN, Math.floor(innerHeight * H_MAX_VH)),
      value: () => Math.round(box().getBoundingClientRect().height),
      set: (px, done) => {
        sz.h = clampH(px, innerHeight);
        sizeMap();
        if (done) { rememberH(); mapResize(); }
        else if (!sz.frame) sz.frame = requestAnimationFrame(() => { sz.frame = 0; mapResize(); });
      },
      reset: () => { sz.h = null; rememberH(); sizeMap(); mapResize(); },
    });
  }
  sizeMap();
  addEventListener("resize", sizeMap);
  document.addEventListener("DOMContentLoaded", sizer);

  // ---- wiring ------------------------------------------------------------------------------------------------------
  $("#sv-filters").addEventListener("submit", (e) => { e.preventDefault(); if (st.tab !== "calls") { st.q.where = $("#sv-where").value.trim(); tab(st.tab); } else submit(0); });
  $("#sv-where").addEventListener("input", () => {           // hydrants: as typed (one table on the hub, never the layer)
    if (st.tab !== "hydrants") return;
    clearTimeout(st.hyTimer);
    st.hyTimer = setTimeout(() => { if (st.tab !== "hydrants") return; st.q.where = $("#sv-where").value.trim(); runHydrants(); }, 200);
  });
  $("#sv-clear").addEventListener("click", () => { for (const k of KEYS) $(INPUT[k]).value = ""; submit(0); });
  $("#sv-intel").addEventListener("click", () => {
    keepIntel(!st.intel);
    if (window.MePrefs) MePrefs.changed("search");             // the person's, on the hub (#823)
    if (st.tab === "calls") runIntel();
  });
  $("#sv-prev").addEventListener("click", () => submit(Math.max(0, st.page - 1)));
  $("#sv-next").addEventListener("click", () => submit(st.page + 1));
  for (const b of document.querySelectorAll("#sv-tabs button")) b.addEventListener("click", () => tab(b.dataset.tab));
  $("#sv-notes-all").addEventListener("change", runNotes);
  $("#sv-note-add").addEventListener("click", () => window.AddrHistory.noteForm($("#sv-note-form"), { where: $("#sv-where").value.trim() }, runNotes));
  cad.hooks.push(() => {                                     // the lists and units, once the state has them
    if (cad.search) fillLists();
    if (!!$("#sv-intel").hidden === (intelOk() && st.tab === "calls")) {     // intel given or taken away: its button
      intelButton();
      if (cad.search && st.tab === "calls") runIntel();
    }
  });

  return { open, close, clampH, H_KEY, H_MIN, H_MAX_VH };
})();
