// The Intel page (/cad#intel?level=&category=&source=&system=&decided=&held=&status=&q=&tag=&review=&from=&to=&range=&page=): every radio intel
// event at every level and status, pushed or not, and why one wasn't pushed (GET /api/cad/intel/list,
// sizeup/intel/eventlog.py: filtered and paged on the hub, 50 a page, newest first). Admin decision, 2026-11-25: "a
// page in the app where we can view these intel events even if they do not trigger". Anyone signed in (the tab shows
// only when the state carries intel).
//
// The range (admin, 2026-11-26: "why am I only seeing back to the 25th?"): a chip by the count says what the list
// covers ("Last 24 h", or the dates), and the quick picks 24 h / 7 d / 30 d / All (every event kept) set it with no
// date. The pick is the person's (me-prefs.js "intel", localStorage RANGE_KEY this browser's copy); the hash's range=
// wins. The hub counts the filters' events before the range (older): the list's last row says how many, with All.
// Paging holds the first page's newest id (upto): an event saved while paging never pushes a row onto the next page
// twice.
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
// (render()'s signature) and its filters, the events' map revision (map_rev: saved or dismissed, never a vote) or the
// total moved, and on Refresh; the pins redrawn only when
// they differ. A pin's click selects its row (scrolled to); a placed row's click selects and centres its pin, a
// second click (or Enter) its More info. The Map button hides it (and reads nothing); a phone starts with it hidden;
// remembered per viewer (localStorage "sizeup.intelMap", in try/catch). Fitted to the pins on new filters, and again
// when the pins change while the viewer hasn't moved the map (a drag, a zoom, a row's centring) since. Pins at the
// same point (one street's, one corner's) carry a count badge (intel-map.js); a click there steps through them.
// Its height: dragged by its bottom edge (Panels.separator, the incident's and Search's maps' handle), the arrow keys
// on the focused handle, Home or a double-click resets; H_MIN px to 85% of the window; remembered per viewer
// (localStorage H_KEY {h}, like Search's map).
// A vote is one per person (Useful / Noise one of the two; the same one again takes it back): its row's buttons are
// disabled from the click until the hub answers.
//
// Uses cad.js's helpers (el, $, show, setText, api, canDo) and icons.js's Icon: loaded after them.
"use strict";

window.IntelPage = (() => {
  const KEYS = ["level", "category", "source", "system", "decided", "held", "status", "q", "tag", "review", "from", "to", "range"];
  const POLL_MS = 30000;
  const POLL_SPAN_S = 7 * 86400;
  // the quick picks (eventlog.py RANGES): seconds back, the chip's words; "all": every event kept (90 days)
  const RANGES = { "24h": [86400, "Last 24 h"], "7d": [7 * 86400, "Last 7 d"], "30d": [30 * 86400, "Last 30 d"],
                   all: [92 * 86400, "All"] };
  const RANGE_KEY = "sizeup.intelRange";
  const H_KEY = "sizeup.intelMapH", H_MIN = 160, H_MAX_VH = 0.85;
  const st = { q: {}, page: 0, seq: 0, data: null, sig: "", rows: new Map(), facetSig: "", timer: null, busy: new Set(),
               open: false, sel: null, hashRange: "", upto: null, uptoQs: null };
  // the map: m its MapLibre map, pins its IntelMap layer, key what it last read for, fit the filters it last fitted
  // moved: the viewer moved the map since it was last fitted (the pins changing then keep the view)
  const mp = { on: null, m: null, pins: null, ready: false, key: "", seq: 0, fit: null, data: null, qs: "", pending: false,
               moved: false };
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

  // ---- the range: the person's pick (the hash's wins) ------------------------------------------------------------
  const okRange = (r) => (Object.prototype.hasOwnProperty.call(RANGES, r) ? r : "");
  let pref = "24h";
  try { pref = okRange(localStorage.getItem(RANGE_KEY)) || "24h"; } catch (_) { /* this page view only */ }
  function keepPref(r) {
    pref = okRange(r) || "24h";
    try { localStorage.setItem(RANGE_KEY, pref); } catch (_) { /* this page view only */ }
  }
  if (window.MePrefs) {
    let had = false;
    try { had = localStorage.getItem(RANGE_KEY) !== null; } catch (_) { /* none */ }
    MePrefs.register("intel", { get: () => ({ range: pref }), hadLocal: had, apply: (v) => {
      const was = pref;
      keepPref(v && v.range);
      // open on the person's range (no date, none in the hash): read again on theirs
      // (the first page, and Refresh shown or not for it: submit())
      if (st.open && pref !== was && !st.hashRange && !st.q.from && !st.q.to) { st.q.range = pref; submit(0, pref); }
    } });
  }
  // the range a query reads: none with a date (the dates are the range) or the notes' filters (the hub's own)
  const rangeOf = (q) => (q.from || q.to || q.q || q.tag || q.review ? "" : okRange(q.range) || "24h");

  // ---- open / close (cad.js route) -------------------------------------------------------------------------------
  function open(qs) {
    const p = new URLSearchParams(qs || "");
    const was = mapQs();
    st.q = Object.fromEntries(KEYS.map((k) => [k, p.get(k) || ""]));
    st.hashRange = okRange(st.q.range);
    st.q.range = st.q.from ? "" : st.hashRange || pref;
    if (mapQs() !== was && st.sel != null) select(null);       // other filters: nothing selected
    st.sig = "";                                 // render() reaches the map with the answer to these filters
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
    picks();
    mapShow(mapOn(), false);
    run();
    timer();
  }
  function close() {
    st.seq++;
    if (mp.pending) { mp.seq++; mp.pending = false; mp.key = ""; }    // a read cut short: back on the page, again
    st.open = false;
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
  }
  // polled: the newest page of the default view, a short span, no filter (a long or filtered view: Refresh)
  function live() {
    if (st.page || st.q.to) return false;
    if (KEYS.some((k) => k !== "from" && k !== "range" && st.q[k])) return false;
    if (!st.q.from) return RANGES[rangeOf(st.q)][0] <= POLL_SPAN_S;
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
  // range: a quick pick (the dates cleared); else the one shown, kept in the hash only when it isn't the person's own
  function submit(page = 0, range = "") {
    const q = {};
    if (range) { $("#il-from").value = ""; $("#il-to").value = ""; }
    for (const k of ["level", "category", "decided", "held", "status", "q", "tag", "from", "to"]) q[k] = $(`#il-${k}`).value.trim();
    q.review = $("#il-review").getAttribute("aria-pressed") === "true" ? "1" : "";
    const s = $("#il-source").value;
    q.source = s.startsWith("s:") ? s.slice(2) : "";
    q.system = s.startsWith("y:") ? s.slice(2) : "";
    const r = q.from ? "" : range || st.q.range || pref;
    q.range = r && r !== pref ? r : "";
    const qs = params(q, page).toString();
    const h = "#intel" + (qs ? `?${qs}` : "");
    if (location.hash === h) {
      st.q = { ...q, range: r }; st.hashRange = q.range; st.page = page;
      show($("#il-refresh"), !live()); picks(); timer(); return run();
    }
    location.hash = h;                         // route() -> open() -> run()
  }
  function pick(r) {
    if (!okRange(r)) return;
    keepPref(r);
    if (window.MePrefs) MePrefs.changed("intel");              // the person's, on the hub (#823)
    submit(0, r);
  }
  // the quick picks: the range shown pressed (none with dates or the notes' filters)
  function picks() {
    const r = rangeOf(st.q);
    for (const b of $("#il-picks").children || []) if (b.dataset) b.setAttribute("aria-pressed", String(b.dataset.range === r));
  }

  async function run(quiet = false) {
    const seq = ++st.seq;
    if (!quiet && !st.data) setText($("#il-count"), "Loading…");
    try {
      const p = params(st.q, st.page);
      if (quiet) p.set("poll", "1");
      if (st.page && st.upto != null && st.uptoQs === mapQs()) p.set("upto", String(st.upto));   // the first page's newest
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
    const sig = JSON.stringify([d.events, d.total, d.page, d.size, d.older, d.older_more]);
    st.data = d;
    if (d.upto != null && (!d.page || st.uptoQs !== mapQs())) { st.upto = d.upto; st.uptoQs = mapQs(); }
    facets(d);
    chip(d);                                   // (its text only when it changed)
    mapRead(d);                                // (its own key: an event moved on another page reads it; a vote not)
    if (sig === st.sig) return;                // the same answer (a poll): nothing redrawn
    st.sig = sig;
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
    const pages = Math.max(1, Math.ceil(d.total / d.size));
    if (!trs.length) trs.push(row0("No intel events"));
    if (d.older > 0 && d.page + 1 >= pages) trs.push(olderRow(d.older_more ? `${d.older}+` : d.older));      // the list's end: what a longer range adds
    if (trs.length !== body.children.length || trs.some((tr, i) => body.children[i] !== tr)) body.replaceChildren(...trs);
    setText($("#il-page"), d.total > d.size ? `Page ${d.page + 1} of ${pages}` : "");
    $("#il-prev").disabled = d.page <= 0;
    $("#il-next").disabled = d.page + 1 >= pages;
    show($("#il-prev").parentElement, d.total > d.size);
  }

  // the range chip: what the list covers (the pick's words, or the dates), its exact span on hover
  // a day (and its year, yr: a span over a new year or a long one, the notes' year)
  const day = (unix, yr) => new Intl.DateTimeFormat("en-US", { timeZone: tz(), month: "2-digit", day: "2-digit",
    ...(yr ? { year: "numeric" } : {}) }).format(new Date(unix * 1000));
  const whenY = (unix, yr) => (yr ? `${day(unix, true)} ${when(unix).split(" ")[1] || ""}`.trim() : when(unix));
  const yearOf = (unix) => new Intl.DateTimeFormat("en-US", { timeZone: tz(), year: "numeric" }).format(new Date(unix * 1000));
  const LONG_S = 300 * 86400;
  function chip(d) {
    const c = $("#il-range");
    const r = okRange(d.range);
    const end = Math.min(d.until, d.now || d.until);
    const yr = end - d.since > LONG_S || yearOf(d.since) !== yearOf(end - 1);
    const text = r ? RANGES[r][1] : st.q.review === "1" ? "Every date" : `${day(d.since, yr)} – ${day(end - 1, yr)}`;
    setText(c, text);
    c.title = `${whenY(d.since, yr)} – ${whenY(end, yr)}` + (r === "all" ? " (every event kept)" : "");
  }
  function olderRow(n) {
    const tr = el("tr", "il-older");
    const td = Object.assign(el("td", "dim small", `Older: ${n}`), { colSpan: 8 });
    td.title = "These filters' events before this range";
    if (rangeOf(st.q) !== "all" || st.q.from || st.q.to) {
      const b = el("button", "sm quiet", "All");
      b.type = "button";
      b.title = "Every event kept (90 days)";
      b.addEventListener("click", () => pick("all"));
      td.append(" ", b);
    }
    tr.append(td);
    return tr;
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
    if (e.lat != null) tr.title = "Click: More info (its pin first, while the map shows)";
    tr.addEventListener("click", () => {        // on the map first (when it shows and the event has a pin)
      if (st.sel !== e.id && mapShown() && pinned(e.id)) { select(e.id); mp.moved = true; mp.pins.center(e.lat, e.lon); } else go();
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
  const pinned = (id) => !!(mp.data && (mp.data.pins.some((p) => p.id === id) || (mp.data.corrected || []).some((p) => p.id === id)));
  function mapShow(on, read = true) {
    mp.on = on;
    const b = $("#il-map-btn");
    b.setAttribute("aria-pressed", String(on));
    show($("#il-mapbox"), on);
    if (!on) {                                 // hidden: nothing read (shown again: once), nothing selected
      mp.seq++; mp.pending = false; mp.key = "";
      if (st.sel != null) select(null);
      return;
    }
    if (read && st.data) mapRead(st.data);
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
    const key = JSON.stringify([qs, d.map_rev, d.total]);
    if (key === mp.key) return;                // the same filters, events and total: the pins as they are
    mp.key = key;
    const seq = ++mp.seq;
    mp.pending = true;
    try {
      const m = await api("GET", `/api/cad/intel/list?map=1${qs ? `&${qs}` : ""}`);
      if (seq !== mp.seq) return;
      mp.pending = false;
      mp.data = m;
      mp.qs = qs;
      if (st.sel != null && !pinned(st.sel)) select(null);    // its pin gone (dismissed, unplaced, filtered out)
      mapDraw();
    } catch (e) {
      if (seq !== mp.seq) return;
      mp.pending = false;
      mp.key = "";                             // (Refresh reads it again)
      mp.data = null;
      if (mp.pins) mp.pins.set([]);            // never the last filters' pins under the error
      setText($("#il-map-n"), `Map: ${e.message}`);
    }
  }
  function mapDraw() {
    const m = mp.data;
    if (!m) return;
    const n = [];
    if (m.placed > m.pins.length) n.push(`newest ${m.pins.length} of ${m.placed}`);
    if (m.unplaced) n.push(`${m.unplaced} unplaced`);
    if ((m.corrected || []).length) n.push(`${m.corrected.length} corrected ◆`);     // a note's picked place (intel-map.js)
    setText($("#il-map-n"), n.join(" · "));
    show($("#il-nomap"), !m.pins.length && !(m.corrected || []).length);
    if (!window.maplibregl || !window.IntelMap || typeof plainMap !== "function" || typeof cad === "undefined" || !cad.scene
        || !cad.scene.home) return;
    if (!mp.m) {
      mp.m = plainMap($("#il-map"), cad.scene);
      mp.m.on("movestart", (e) => { if (e && e.originalEvent) mp.moved = true; });     // the viewer's own drag or zoom
      mp.m.on("load", () => {
        mp.pins = IntelMap.attach(mp.m, "il", { pick: picked });
        mp.ready = true;
        mapDraw();
      });
      return;
    }
    if (!mp.ready) return;
    const drawn = mp.pins.set(m.pins, m.corrected || []);     // a note's corrected place: its own mark (intel-map.js)
    mp.pins.select(st.sel);
    // new filters: fitted to their pins; the same filters' pins changed: fitted again unless the viewer moved the map
    if (mp.qs !== mp.fit || (drawn && !mp.moved)) mapFit();
  }
  function mapFit() {
    const b = mp.pins && mp.pins.bounds();
    if (!b) return;
    mp.fit = mp.qs;
    mp.moved = false;
    mp.m.fitBounds(b, { padding: 40, maxZoom: 16, duration: 0 });
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

  // ---- the map's height: dragged by its bottom edge (Panels.separator, as Search's and the incident's maps) ----------
  function clampH(want, vh) {
    if (want == null || !Number.isFinite(want) || want <= 0) return null;
    const hi = Math.max(H_MIN, Math.floor(vh * H_MAX_VH));
    return Math.round(Math.max(H_MIN, Math.min(hi, want)));
  }
  const sz = { h: null, sep: null, frame: 0 };
  try {
    const v = (JSON.parse(localStorage.getItem(H_KEY) || "{}") || {}).h;
    sz.h = Number.isFinite(v) && v > 0 ? v : null;
  } catch (_) { /* the layout's own */ }
  function sizeMap() {
    const box = $("#il-mapbox-in");
    if (!box || !box.style) return;
    const h = clampH(sz.h, innerHeight);
    if (h == null) box.style.removeProperty("--il-map-h"); else box.style.setProperty("--il-map-h", `${h}px`);
    if (sz.sep) sz.sep.sync();
  }
  function mapResize() { if (mp.m) { try { mp.m.resize(); } catch (_) { /* gone */ } } }
  // the size settled (a drag ended, a key, a reset): remembered; the pins fitted again unless the viewer moved the map
  function settled() {
    try { localStorage.setItem(H_KEY, JSON.stringify({ h: sz.h })); } catch (_) { /* this page view only */ }
    mapResize();
    if (mp.ready && !mp.moved) mapFit();
  }
  function sizer() {                         // panels.js loads after this file: made once it has
    const hd = $("#il-map-h");
    if (sz.sep || !window.Panels || !Panels.separator || !hd || !hd.style) return;
    const box = $("#il-mapbox-in");
    sz.sep = Panels.separator(hd, {
      orient: "horizontal", grow: 1, label: "Resize the map", controls: "il-mapbox-in",
      min: () => H_MIN, max: () => Math.max(H_MIN, Math.floor(innerHeight * H_MAX_VH)),
      value: () => Math.round(box.getBoundingClientRect().height),
      set: (px, done) => {
        sz.h = clampH(Math.max(H_MIN, px), innerHeight);       // (a drag past the top: the least, never the layout's)
        sizeMap();
        if (done) settled();
        else if (!sz.frame) sz.frame = requestAnimationFrame(() => { sz.frame = 0; mapResize(); });
      },
      reset: () => { sz.h = null; sizeMap(); settled(); },
    });
  }

  function wire() {
    if (!$("#il-filters")) return;
    sizeMap();
    sizer();
    if (typeof addEventListener === "function") addEventListener("resize", sizeMap);     // (85% of the window: again)
    for (const b of $("#il-picks").children || []) if (b.dataset && b.dataset.range) b.addEventListener("click", () => pick(b.dataset.range));
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
    $("#il-refresh").addEventListener("click", () => { mp.key = ""; st.sig = ""; run(); });     // the map too
    document.addEventListener("visibilitychange", visibility);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();

  return { open, close, render, run, row, clampH, H_MIN, H_MAX_VH, _mp: mp, href: (q) => "#intel" + (q ? `?${params(q, 0)}` : "") };
})();
