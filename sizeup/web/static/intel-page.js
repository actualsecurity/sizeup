// The Intel page (/cad#intel?level=&category=&source=&system=&decided=&held=&status=&q=&tag=&review=&from=&to=&page=): every radio intel
// event at every level and status, pushed or not, and why one wasn't pushed (GET /api/cad/intel/list,
// sizeup/intel/eventlog.py: filtered and paged on the hub, 50 a page, newest first; the last 24 h unless a date is
// set). Admin decision, 2026-11-25: "a page in the app where we can view these intel events even if they do not
// trigger". Anyone signed in (the tab shows only when the state carries intel).
//
// A row: time · category · level (→ the level it would have had if not held) · source · place + precision ·
// score and its parts (rule weights; the model's as "model 95%") · why it didn't push · Useful / Noise / Should
// have pushed (POST /api/cad/intel/<id>/feedback: intel_feedback, what tuning reads). A click opens its More info
// (/cad#prealert/intel/<id>, prealert-info.js). Terse: what a label means is in its hover.
// Notes (intel-notes.js, sizeup/intel/notes.py): a count chip on the row; filters by the notes' words (q), a tag, and
// the review queue (review=1: tagged for a rule or model change, not done; every date kept). Admins: the export of
// what the filters select, CSV or JSONL (GET /api/cad/intel/export.*: notes, scores, rule hits, transcripts).
//
// Light on the console: nothing per second. The list is read on open, on a filter or page change, after a vote, and
// every POLL_MS only while it shows the newest page of the default view (no filter, no end date, at most POLL_SPAN_S
// back) and the tab is visible (?poll=1: the hub reuses its count unless the events changed); else a Refresh button.
// render() draws only what changed (a row is rebuilt only when its own data changed; the same answer touches nothing).
// The map (#937, intel-map.js on Search's map, map-style.js plainMap): every placed event of the filters, all pages
// (GET /api/cad/intel/list?map=1, the newest 2,000), the unplaced counted. Read only when the list's answer changed
// (render()'s signature) and its filters, the events' revision (rev) or the total moved; the pins redrawn only when
// they differ. A pin's click selects its row (scrolled to); a placed row's click selects and centres its pin, a
// second click (or Enter) its More info. The Map button hides it (and reads nothing); a phone starts with it hidden;
// remembered per viewer (localStorage "sizeup.intelMap", in try/catch).
// A vote is one per person (Useful / Noise one of the two; the same one again takes it back): its row's buttons are
// disabled from the click until the hub answers.
//
// Uses cad.js's helpers (el, $, show, setText, api, canDo) and icons.js's Icon: loaded after them.
"use strict";

window.IntelPage = (() => {
  const KEYS = ["level", "category", "source", "system", "decided", "held", "status", "q", "tag", "review", "from", "to"];
  const POLL_MS = 30000;
  const POLL_SPAN_S = 7 * 86400;
  const st = { q: {}, page: 0, seq: 0, data: null, sig: "", rows: new Map(), facetSig: "", timer: null, busy: new Set(),
               open: false, sel: null };
  // the map: m its MapLibre map, pins its IntelMap layer, key what it last read for, fit the filters it last fitted
  const mp = { on: null, m: null, pins: null, ready: false, key: "", seq: 0, fit: null, data: null, qs: "" };
  const MAP_KEY = "sizeup.intelMap";
  const LEVELS = { push: "pushed", held: "at push, not pushed", map: "on the card and the map", log: "logged only",
                   shadow: "counted only" };
  const DECIDED = { rules: "Rules", model: "Model", both: "Rules + model" };
  const STATUS = { open: "open", ended: "ended", expired: "expired", dismissed: "dismissed" };
  const PREC = { address: "address", intersection: "corner", block: "block", landmark: "place", street: "street", town: "town" };
  const tz = () => (window.cad && cad.scene && cad.scene.timezone) || "America/New_York";
  const when = (unix) => (unix ? new Intl.DateTimeFormat("en-US", { timeZone: tz(), month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(unix * 1000)).replace(",", "") : "");
  const pct = (x) => `${Math.round((x || 0) * 100)}%`;
  const isModel = (id) => /^(model|infer)\./.test(String(id));
  const w2 = (w) => (w === null || w === undefined ? "" : Number(w).toFixed(2).replace(/^(-?)0\./, "$1."));

  function params(q, page) {
    const p = new URLSearchParams();
    for (const k of KEYS) if (q[k]) p.set(k, q[k]);
    if (page) p.set("page", String(page));
    return p;
  }

  // ---- open / close (cad.js route) -------------------------------------------------------------------------------
  function open(qs) {
    const p = new URLSearchParams(qs || "");
    st.q = Object.fromEntries(KEYS.map((k) => [k, p.get(k) || ""]));
    st.page = Math.max(0, Number(p.get("page")) || 0);
    for (const k of ["level", "decided", "held", "status", "q", "tag", "from", "to"]) {
      const n = $(`#il-${k}`);
      if (n.tagName === "SELECT" && st.q[k] && ![...n.options].some((o) => o.value === st.q[k])) n.append(new Option(st.q[k], st.q[k]));
      n.value = st.q[k];
    }
    srcValue();
    $("#il-review").setAttribute("aria-pressed", String(st.q.review === "1"));
    exports();
    const c = $("#il-category");
    if (st.q.category && ![...c.options].some((o) => o.value === st.q.category)) c.append(new Option(st.q.category, st.q.category));
    c.value = st.q.category;
    st.open = true;
    show($("#il-refresh"), !live());
    mapShow(mapOn());
    run();
    timer();
  }
  function close() {
    st.seq++;
    mp.seq++;
    st.open = false;
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
  }
  // polled: the newest page of the default view, a short span, no filter (a long or filtered view: Refresh)
  function live() {
    if (st.page || st.q.to) return false;
    if (KEYS.some((k) => k !== "from" && st.q[k])) return false;
    if (!st.q.from) return true;
    const t = Date.parse(st.q.from);
    const since = Number.isNaN(t) ? Number(st.q.from) * 1000 : t;
    return !Number.isNaN(since) && Date.now() - since <= POLL_SPAN_S * 1000;
  }
  function timer() {
    const want = st.open && live() && !document.hidden;
    if (want && !st.timer) st.timer = setInterval(poll, POLL_MS);
    if (!want && st.timer) { clearInterval(st.timer); st.timer = null; }
  }
  function poll() {
    if (document.hidden || $("#intel-view").hidden || !live()) return;
    run(true);
  }
  // hidden: no timer; back: read at once, then the timer again
  function visibility() {
    if (!st.open) return;
    timer();
    if (!document.hidden && live()) run(true);
  }
  function srcValue() {
    const s = $("#il-source");
    const v = st.q.source ? `s:${st.q.source}` : st.q.system ? `y:${st.q.system}` : "";
    if (v && ![...s.options].some((o) => o.value === v)) s.append(new Option(st.q.source || st.q.system, v));
    s.value = v;
  }

  // the export of what the filters select (admins: ADMIN_READS on the hub; the links show only for an admin)
  function exports() {
    const admin = typeof canDo !== "function" || canDo("admin");
    const qs = params(st.q, 0).toString();
    for (const f of ["csv", "jsonl"]) {
      const a = $(`#il-${f}`);
      show(a, admin);
      a.href = `/api/cad/intel/export.${f}${qs ? `?${qs}` : ""}`;
    }
  }
  function submit(page = 0) {
    const q = {};
    for (const k of ["level", "category", "decided", "held", "status", "q", "tag", "from", "to"]) q[k] = $(`#il-${k}`).value.trim();
    q.review = $("#il-review").getAttribute("aria-pressed") === "true" ? "1" : "";
    const s = $("#il-source").value;
    q.source = s.startsWith("s:") ? s.slice(2) : "";
    q.system = s.startsWith("y:") ? s.slice(2) : "";
    const qs = params(q, page).toString();
    const h = "#intel" + (qs ? `?${qs}` : "");
    if (location.hash === h) { st.q = q; st.page = page; show($("#il-refresh"), !live()); timer(); return run(); }
    location.hash = h;                         // route() -> open() -> run()
  }

  async function run(quiet = false) {
    const seq = ++st.seq;
    if (!quiet && !st.data) setText($("#il-count"), "Loading…");
    try {
      const p = params(st.q, st.page);
      if (quiet) p.set("poll", "1");
      const d = await api("GET", `/api/cad/intel/list?${p}`);
      if (seq !== st.seq) return;
      render(d);
    } catch (e) {
      if (seq !== st.seq) return;
      st.sig = "";
      setText($("#il-count"), "");
      $("#il-rows").replaceChildren(row0(`Intel: ${e.message}`));
      st.rows.clear();
    }
  }
  function row0(text) {
    const tr = el("tr");
    tr.append(Object.assign(el("td", "empty", text), { colSpan: 8 }));
    return tr;
  }

  // ---- the filters' choices (the range's categories and sources), redrawn only when they change --------------------
  function facets(d) {
    const f = d.facets || {};
    const sig = JSON.stringify([f, d.held]);
    if (sig === st.facetSig) return;
    st.facetSig = sig;
    const fill = (sel, first, items) => {
      const v = sel.value;
      sel.replaceChildren(new Option(first, ""), ...items.map(([val, text, tip]) => {
        const o = new Option(text, val);
        if (tip) o.title = tip;
        return o;
      }));
      if (v && ![...sel.options].some((o) => o.value === v)) sel.append(new Option(v.replace(/^[sy]:/, ""), v));
      sel.value = v;
    };
    fill($("#il-category"), "Any category", (f.categories || []).map((c) => [c.key, `${c.label} (${c.n})`]));
    fill($("#il-source"), "Any source", [...(f.systems || []).map((s) => [`y:${s.key}`, `${s.key}: all (${s.n})`]),
      ...(f.sources || []).map((s) => [`s:${s.key}`, `${s.name} (${s.n})`, `${s.key}`])]);
    fill($("#il-held"), "Any reason", (d.held || []).map((h) => [h.key, h.label, h.tip]));
    const tl = window.IntelNotes ? IntelNotes.TAGS : {};
    fill($("#il-tag"), "Any tag", (d.tags || []).map((t) => [t, tl[t] || t]));
  }

  // ---- the rows ----------------------------------------------------------------------------------------------------
  function render(d) {
    const sig = JSON.stringify([d.events, d.total, d.page, d.size]);
    st.data = d;
    facets(d);
    if (sig === st.sig) return;                // the same answer (a poll): nothing redrawn
    st.sig = sig;
    mapRead(d);
    const from = d.page * d.size;
    setText($("#il-count"), d.total ? `${d.total} event${d.total === 1 ? "" : "s"}` + (d.total > d.size ? ` · ${from + 1}–${from + d.events.length}` : "") : "None");
    $("#il-count").title = `${when(d.since)} – ${when(Math.min(d.until, d.now || d.until))}`;
    const body = $("#il-rows");
    const keep = new Map();
    const trs = d.events.map((e) => {
      const rs = JSON.stringify([e, st.busy.has(e.id)]);
      const had = st.rows.get(e.id);
      const tr = had && had.sig === rs ? had.tr : row(e);
      keep.set(e.id, { sig: rs, tr });
      return tr;
    });
    st.rows = keep;
    if (st.sel != null) selLink();
    if (!trs.length) body.replaceChildren(row0("No intel events"));
    else if (trs.length !== body.children.length || trs.some((tr, i) => body.children[i] !== tr)) body.replaceChildren(...trs);
    const pages = Math.max(1, Math.ceil(d.total / d.size));
    setText($("#il-page"), d.total > d.size ? `Page ${d.page + 1} of ${pages}` : "");
    $("#il-prev").disabled = d.page <= 0;
    $("#il-next").disabled = d.page + 1 >= pages;
    show($("#il-prev").parentElement, d.total > d.size);
  }

  function cell(cls, ...kids) {
    const td = el("td", cls);
    td.append(...kids.filter((k) => k !== null && k !== undefined && k !== ""));
    return td;
  }
  function row(e) {
    const tr = el("tr", "ev il-row");
    tr.dataset.id = String(e.id);
    tr.tabIndex = 0;
    const go = () => { location.hash = window.PrealertInfo ? PrealertInfo.href("intel", e.id) : `#prealert/intel/${e.id}`; };
    if (e.id === st.sel) tr.classList.add("il-sel");
    if (e.lat != null) tr.title = "Click: its pin · again: More info";
    tr.addEventListener("click", () => {        // on the map first (when it shows and the event is placed)
      if (st.sel !== e.id && e.lat != null && mapShown()) { select(e.id); if (mp.pins) mp.pins.center(e.lat, e.lon); } else go();
    });
    tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target === tr) go(); });
    // time (and the status, when it is no longer open)
    const t = cell("t", when(e.at));
    t.title = e.last_at && e.last_at !== e.at ? `Last heard ${when(e.last_at)} · ${e.n} tx` : `${e.n} tx`;
    if (e.status !== "open") t.append(el("div", "dim small", STATUS[e.status] || e.status));
    // category
    const c = cell("il-cat", e.label || e.category);
    c.title = `${e.pack_label} · ${e.category}`;
    // level, and the one it would have had
    const lv = el("span", `chip il-lv il-lv-${e.level}`, e.level);
    lv.title = (LEVELS[e.level] || e.level) + (e.pushed_at ? ` ${when(e.pushed_at)}` : "");
    const lvc = cell("il-level", lv);
    if (e.would) {
      const w = el("span", "dim small il-would", `→${e.would}`);
      w.title = `Would have been ${e.would} if not held`;
      lvc.append(" ", w);
    }
    // source
    const s = cell("il-src", e.source || "—");
    s.title = [e.source_key, e.system].filter(Boolean).join(" · ");
    // place + precision, or unplaced
    const pl = e.place
      ? cell("loc", e.confirmed ? e.place : `${e.place} ?`, el("span", "dim small", ` · ${PREC[e.precision] || e.precision}`))
      : cell("loc dim", "unplaced");
    if (e.place && !e.confirmed) pl.title = "Unconfirmed";
    // the score and its parts
    const sc = cell("il-score", el("span", `il-sl ${e.score_level}`, `${e.score_level} ${pct(e.what)}`));
    sc.title = `${DECIDED[e.decided] || "—"}`;
    if ((e.parts || []).length) {
      const parts = el("div", "dim small il-parts");             // each part whole (.il-part), the line wraps at " · "
      e.parts.forEach(([id, w], i) => {
        if (i) parts.append(" · ");
        parts.append(el("span", "il-part", isModel(id) ? `model ${pct(w)}` : `${id} ${w2(w)}`));
      });
      sc.append(parts);
    }
    // why it didn't push
    const h = e.held;
    const why = h ? cell("il-why", el("span", "chip il-held", h.label)) : cell("il-why dim", "—");
    if (h) {
      why.title = [h.tip, h.detail].filter(Boolean).join("\n");
      if (h.detail) why.append(el("div", "dim small", h.detail));
    }
    const nc = window.IntelNotes ? IntelNotes.chip(e.notes) : null;
    tr.append(t, c, lvc, s, pl, sc, why, cell("act il-acts", nc, ...votes(e)));
    return tr;
  }

  // Useful / Noise / Should have pushed: the intel feedback store (what the rules' and the model's tuning read)
  function votes(e) {
    if (typeof canDo === "function" && !canDo("active")) return [];     // a deactivated account: no votes
    const busy = st.busy.has(e.id);
    const mine = new Set(e.mine || []);
    const n = e.votes || {};
    const btns = [];
    const b = (icon, verdict, name, tip) => {
      const x = window.Icon ? Icon.button(icon, name, tip, "il-vote") : el("button", "mini il-vote", name);
      x.type = "button";
      x.setAttribute("aria-pressed", String(mine.has(verdict)));
      if (mine.has(verdict)) x.title += " (yours: again takes it back)";
      if (n[verdict]) x.append(el("span", "il-n", String(n[verdict])));
      x.disabled = busy;
      btns.push(x);
      x.addEventListener("click", (ev) => {
        ev.stopPropagation();
        for (const o of btns) o.disabled = true;           // at once: a second click never reaches the hub
        vote(e, verdict, name);
      });
      return x;
    };
    const out = [b("thumbs-up", "useful", "Useful", "it was real"), b("thumbs-down", "noise", "Noise", "nothing to it")];
    if (!e.pushed_at) out.push(b("bell-plus", "should_push", "Should have pushed", "worth a pre-alert"));
    return out;
  }
  async function vote(e, verdict, name) {
    if (st.busy.has(e.id)) return;
    st.busy.add(e.id);
    try {
      const r = await api("POST", `/api/cad/intel/${e.id}/feedback`, { verdict, operator: typeof operator === "function" ? operator() : "" });
      if (window.UI) UI.toast(r && r.removed ? `Taken back: ${name.toLowerCase()}` : `Noted: ${name.toLowerCase()}`, "ok");
    } catch (err) {
      if (window.UI) UI.toast(`Feedback: ${err.message}`, "err");
    } finally {
      st.busy.delete(e.id);
      run(true);
    }
  }

  // ---- the map (#937) ----------------------------------------------------------------------------------------------
  function mapOn() {
    if (mp.on !== null) return mp.on;
    let v = null;
    try { v = localStorage.getItem(MAP_KEY); } catch (_) { /* this page view only */ }
    const phone = typeof matchMedia === "function" && matchMedia("(max-width: 640px)").matches;
    mp.on = v === null ? !phone : v === "1";
    return mp.on;
  }
  const mapShown = () => !!(mp.on && mp.ready);
  function mapShow(on) {
    mp.on = on;
    const b = $("#il-map-btn");
    b.setAttribute("aria-pressed", String(on));
    show($("#il-mapbox"), on);
    if (!on) { mp.seq++; mp.key = ""; return; }           // hidden: nothing read (shown again: read once)
    if (st.data) mapRead(st.data);
  }
  function mapToggle() {
    mapShow(!mapOn());
    try { localStorage.setItem(MAP_KEY, mp.on ? "1" : "0"); } catch (_) { /* this page view only */ }
    if (mp.on && mp.m) { try { mp.m.resize(); } catch (_) { /* gone */ } }
  }
  // the filters as the map reads them (every page: no page)
  const mapQs = () => params(st.q, 0).toString();
  async function mapRead(d) {
    if (!mapOn() || !st.open) return;
    const qs = mapQs();
    const key = JSON.stringify([qs, d.rev, d.total]);
    if (key === mp.key) return;                // the same filters, events and total: the pins as they are
    mp.key = key;
    const seq = ++mp.seq;
    try {
      const m = await api("GET", `/api/cad/intel/list?map=1${qs ? `&${qs}` : ""}`);
      if (seq !== mp.seq) return;
      mp.data = m;
      mp.qs = qs;
      mapDraw();
    } catch (e) {
      if (seq !== mp.seq) return;
      mp.key = "";
      setText($("#il-map-n"), `Map: ${e.message}`);
    }
  }
  function mapDraw() {
    const m = mp.data;
    if (!m) return;
    const n = [];
    if (m.placed > m.pins.length) n.push(`newest ${m.pins.length} of ${m.placed}`);
    if (m.unplaced) n.push(`${m.unplaced} unplaced`);
    setText($("#il-map-n"), n.join(" · "));
    show($("#il-nomap"), !m.pins.length);
    if (!window.maplibregl || !window.IntelMap || typeof plainMap !== "function" || typeof cad === "undefined" || !cad.scene
        || !cad.scene.home) return;
    if (!mp.m) {
      mp.m = plainMap($("#il-map"), cad.scene);
      mp.m.on("load", () => {
        mp.pins = IntelMap.attach(mp.m, "il", { pick: picked });
        mp.ready = true;
        mapDraw();
      });
      return;
    }
    if (!mp.ready) return;
    mp.pins.set(m.pins);
    mp.pins.select(st.sel);
    if (mp.qs !== mp.fit) {                    // new filters: fitted to them; the same filters read again keep the view
      mp.fit = mp.qs;
      const b = mp.pins.bounds();
      if (b) mp.m.fitBounds(b, { padding: 40, maxZoom: 16, duration: 0 });
    }
  }
  // a pin's click: its row selected and scrolled to (on this page), else its More info a click away
  function picked(id) {
    select(id);
    const r = st.rows.get(id);
    if (r) { try { r.tr.scrollIntoView({ block: "nearest" }); r.tr.focus({ preventScroll: true }); } catch (_) { /* gone */ } }
  }
  function select(id) {
    const was = st.rows.get(st.sel);
    if (was) was.tr.classList.remove("il-sel");
    st.sel = id;
    const r = st.rows.get(id);
    if (r) r.tr.classList.add("il-sel");
    if (mp.pins) mp.pins.select(id);
    selLink();
  }
  function selLink() {                         // a pin of another page selected: its More info from here
    const off = $("#il-map-sel"), id = st.sel, on = id != null && mp.on && !st.rows.has(id);
    show(off, on);
    if (on) off.href = window.PrealertInfo ? PrealertInfo.href("intel", id) : `#prealert/intel/${id}`;
  }

  function wire() {
    if (!$("#il-filters")) return;
    $("#il-map-btn").addEventListener("click", mapToggle);
    $("#il-filters").addEventListener("submit", (ev) => { ev.preventDefault(); submit(0); });
    for (const k of ["level", "category", "source", "decided", "held", "status", "q", "tag", "from", "to"]) {
      $(`#il-${k}`).addEventListener("change", () => submit(0));
    }
    $("#il-review").addEventListener("click", () => {
      const b = $("#il-review");
      b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true"));
      submit(0);
    });
    $("#il-clear").addEventListener("click", () => { location.hash = "#intel"; });
    $("#il-prev").addEventListener("click", () => submit(Math.max(0, st.page - 1)));
    $("#il-next").addEventListener("click", () => submit(st.page + 1));
    $("#il-refresh").addEventListener("click", () => run());
    document.addEventListener("visibilitychange", visibility);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();

  return { open, close, render, run, row, _mp: mp, href: (q) => "#intel" + (q ? `?${params(q, 0)}` : "") };
})();
