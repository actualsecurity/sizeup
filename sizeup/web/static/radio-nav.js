// The Radio page with many sources (GitHub #415, layout B): a rail on the left with the views (Our calls, Police, EMS,
// Transit, Air, All) and the tree system > group > channel, one list on the right.
//
//   /cad#radio?ui=2            turns it on for this browser (ui=1: the page as before, radio-view.js); the Sources
//                              button on the Radio page does the same. Remembered (localStorage "sizeup.cad.radio.nav").
//   &view=police &source=<key> &event= &unit= &q= &channel=    what is shown; an old link (#radio?event=26-000012,
//                              ?channel=Fire/EMS 15) opens filtered here too, in All
//
// Our calls is the view the page opens on, every time. Live (RadioLive) plays only Our calls, whatever is on screen;
// any other view keeps the newest of Our calls in one line on top (a click: back to Our calls).
// A view is a filter on the hub (GET /api/cad/comms?view=...): one page of 100, older ones by "Older" (?before=),
// and on each state push whose comms_rev moved only the rows new or changed since (?changed=), patched by id; the
// whole page again every SAFETY_MS and when a link changes (sizeup:tx-changed). The rail is GET
// /api/cad/comms/sources: each source with when it was last heard, whether it is live and its calls per minute over
// the last hour; the unread counts are worked out here from those and when this viewer last had the source on screen
// (no per-viewer state on the hub).
//
// Per viewer, remembered: the rail folded (a strip of view marks with their counts), pinned channels (a Pinned group
// on top of the tree, in every view) and muted ones (not in All, no count; one of Our calls can't be muted). On a
// phone the rail is a sheet behind Sources.
// A service is told by a letter in a shape (F P E T A), never by colour alone. The tier is a short code (PA TX REC OFF
// ENC), its meaning in the hover. A police row's words come only to someone signed in (the hub leaves them out).
//
// Uses cad.js (el, $, show, setText, syncList, api, fmtTime, cad) and radio-view.js (RadioView.row / rowSig, RadioLive):
// loaded after both. The pure part (prefs, inView, unread, model) is tested in node (tests/test_radio_nav.py).
"use strict";

window.RadioNav = (() => {
  const KEY = "sizeup.cad.radio.nav";
  const SAFETY_MS = 30000, SOURCES_MS = 5000, RECENT_S = 300, PAGE = 100, MAX_KEYS = 100;
  const VIEWS = [["ours", "Our calls", "F", "fire"], ["police", "Police", "P", "police"], ["ems", "EMS", "E", "ems"],
                 ["transit", "Transit", "T", "transit"], ["air", "Air", "A", "air"], ["all", "All", "∗", ""]];
  const MARK = { fire: "F", police: "P", ems: "E", transit: "T", air: "A" };
  const SERVICE = { fire: "Fire", police: "Police", ems: "EMS", transit: "Transit", air: "Air", other: "Other" };
  const TIER = { alert: ["PA", "Transcribe + pre-alert"], text: ["TX", "Record + transcribe"], record: ["REC", "Record only"],
                 off: ["OFF", "Ignored"], activity: ["ENC", "Encrypted: activity only"] };

  // ---- pure: what the rail shows -----------------------------------------------------------------------

  function prefs(raw) {
    let v = null;
    try { v = JSON.parse(raw || "null"); } catch (_) { /* as new */ }
    v = v && typeof v === "object" ? v : {};
    const keys = (x) => (Array.isArray(x) ? x.map(String).slice(-MAX_KEYS) : []);
    const seen = {};
    for (const [k, t] of Object.entries(v.seen && typeof v.seen === "object" ? v.seen : {}).slice(-3 * MAX_KEYS)) if (t > 0) seen[k] = +t;
    return { on: !!v.on, rail: v.rail !== false, pins: keys(v.pins), mutes: keys(v.mutes), seen };
  }
  // ours: our fire channels; a service's view: its sources that aren't ours; all: every source
  const inView = (s, view) => (view === "all" ? true : view === "ours" ? !!s.ours : !s.ours && s.service === view);
  // Calls on a source in the minutes after this viewer last had it on screen (counts: per minute, the last one the
  // minute of `now`). Never seen: nothing is new (a first visit doesn't start with an hour of counts).
  function unread(s, seen, now) {
    const c = s.counts || [];
    if (!seen || !c.length) return 0;
    const end = Math.floor(now / 60), from = Math.floor(seen / 60);
    let n = 0;
    for (let i = 0; i < c.length; i++) if (end - (c.length - 1 - i) > from) n += c[i];
    return n;
  }
  function model(sources, p, now, live = new Set(), flagged = new Set()) {
    const pins = new Set(p.pins), mutes = new Set(p.mutes);
    const rows = sources.map((s) => {
      const mute = mutes.has(s.key) && !s.ours;
      const t = TIER[s.tier] || null;
      return { ...s, mute, pin: pins.has(s.key), n: mute ? 0 : unread(s, p.seen[s.key], now),
               live: !!s.live || live.has(s.key), recent: !!s.last && now - s.last < RECENT_S, em: flagged.has(s.key),
               code: t ? t[0] : "", tip: t ? t[1] : "" };
    });
    const views = VIEWS.filter(([v]) => v === "ours" || v === "all" || rows.some((s) => inView(s, v))).map(([id, name, mark, service]) => {
      const mine = rows.filter((s) => inView(s, id) && !s.mute);
      return { id, name, mark, service, n: id === "all" ? 0 : mine.reduce((a, s) => a + s.n, 0), live: mine.some((s) => s.live),
               em: mine.some((s) => s.em) };
    });
    const systems = [];
    for (const s of rows) {
      let sys = systems.find((x) => x.id === s.system);
      if (!sys) systems.push(sys = { id: s.system, label: s.system_label || s.system, groups: [] });
      const gname = s.group || "Other";
      let g = sys.groups.find((x) => x.name === gname);
      if (!g) sys.groups.push(g = { name: gname, service: s.service, n: 0, sources: [] });
      g.sources.push(s);
      g.n += s.n;
    }
    return { views, systems, pinned: rows.filter((s) => s.pin), rows };
  }

  // ---- state -------------------------------------------------------------------------------------------

  const st = { open: false, p: prefs(null), src: [], srcAt: 0, rows: [], cursor: 0, next: "", older: false, rev: "", busy: false,
               again: false, timer: 0, error: "", f: { view: "ours", source: "", event: "", unit: "", q: "", channel: "" },
               ours: [], railSig: "", sheet: false, qTimer: 0, facets: "", shown: "" };
  const dom = typeof document !== "undefined" && typeof $ === "function";
  const load = () => { try { st.p = prefs(localStorage.getItem(KEY)); } catch (_) { st.p = prefs(null); } };
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(st.p)); } catch (_) { /* not remembered */ } };
  load();
  const now = () => Date.now() / 1000;
  const narrowed = () => !!(st.f.source || st.f.event || st.f.unit || st.f.q || st.f.channel);

  // on / off: ?ui=2 / ?ui=1 in the address, else what this browser chose
  function wants(qs) {
    const ui = new URLSearchParams(qs || "").get("ui");
    return ui === "2" || (ui !== "1" && st.p.on);
  }
  function query(extra = {}) {
    const q = new URLSearchParams(), f = st.f;
    q.set("view", f.source ? "all" : f.view);
    for (const k of ["source", "event", "unit", "q", "channel"]) if (f[k]) q.set(k, f[k]);
    if (f.view === "all" && !f.source && st.p.mutes.length) q.set("mute", st.p.mutes.join(","));
    q.set("limit", String(PAGE));
    for (const [k, v] of Object.entries(extra)) q.set(k, String(v));
    return q.toString();
  }
  function toHash() {
    const q = new URLSearchParams(), f = st.f;
    if (f.view !== "ours") q.set("view", f.view);
    for (const k of ["source", "event", "unit", "q", "channel"]) if (f[k]) q.set(k, f[k]);
    const s = q.toString();
    history.replaceState(null, "", "#radio" + (s ? `?${s}` : ""));
  }
  function fromHash(qs) {
    const q = new URLSearchParams(qs || "");
    const f = { view: "ours", source: "", event: "", unit: "", q: "", channel: "" };
    for (const k of ["source", "event", "unit", "q", "channel"]) f[k] = q.get(k) || "";
    const v = q.get("view") || "";
    // an old link (an incident's "All", the feed's More): its filter, over every source
    f.view = VIEWS.some(([id]) => id === v) ? v : f.event || f.unit || f.channel || f.q ? "all" : "ours";
    st.f = f;
  }

  // ---- the hub -----------------------------------------------------------------------------------------

  const shown = () => st.open && location.hash.startsWith("#radio") && document.visibilityState !== "hidden";
  const pushed = () => (typeof cad !== "undefined" && cad.state && cad.state.comms_rev) || "";

  async function page() {                             // the first page of what is asked
    clearTimeout(st.timer);
    st.timer = 0;
    if (!shown()) return;
    if (st.busy) { st.again = true; return; }
    st.busy = true;
    st.again = false;
    const asked = query();
    try {
      const d = await api("GET", `/api/cad/comms?${asked}`);
      st.error = "";
      if (asked !== query()) st.again = true;         // the filters moved on meanwhile
      else {
        Object.assign(st, { rows: d.rows || [], cursor: d.cursor || 0, next: d.next || "", older: !!d.older, rev: d.rev || "", shown: asked });
        fillFacets(d);
        seenNow();
      }
    } catch (e) { st.error = e.message; }
    st.busy = false;
    render();
    if (st.again) return page();
    if (shown()) st.timer = setTimeout(page, SAFETY_MS);
    sources();
    ours();
  }
  async function delta() {                            // only what is new or changed since the page on screen
    if (!shown() || st.busy || st.shown !== query()) return;
    st.busy = true;
    const asked = st.shown;
    try {
      const d = await api("GET", `/api/cad/comms?${query({ changed: st.cursor })}`);
      st.busy = false;
      if (asked !== query()) return;
      st.rev = pushed();
      if (d.reload) return page();
      if ((d.rows || []).length) {
        const by = new Map(st.rows.map((t) => [t.id, t]));
        for (const t of d.rows) by.set(t.id, t);
        st.rows = [...by.values()].sort((a, b) => (b.start - a.start) || (a.id < b.id ? 1 : -1));
        st.cursor = d.cursor || st.cursor;
        seenNow();
        render();
      }
    } catch (_) { st.busy = false; }
  }
  async function older() {
    if (st.busy || !st.older) return;
    st.busy = true;
    try {
      const d = await api("GET", `/api/cad/comms?${query({ before: st.next })}`);
      const have = new Set(st.rows.map((t) => t.id));
      st.rows = st.rows.concat((d.rows || []).filter((t) => !have.has(t.id)));
      st.next = d.next || st.next;
      st.older = !!d.older;
    } catch (e) { st.error = e.message; }
    st.busy = false;
    render();
  }
  async function sources(force) {
    if (!shown() || (!force && Date.now() - st.srcAt < SOURCES_MS)) return;
    st.srcAt = Date.now();
    try {
      const d = await api("GET", "/api/cad/comms/sources");
      st.src = d.sources || [];
      let fresh = false;                              // a source first met: nothing of it is unread
      for (const s of st.src) if (!st.p.seen[s.key]) { st.p.seen[s.key] = now(); fresh = true; }
      if (fresh) save();
      seenNow();
      renderRail();
    } catch (_) { /* the rail as it was */ }
  }
  // Our calls, whatever is on screen: what Live plays, and the line on top of any other view
  async function ours() {
    if (!shown()) return;
    let rows = st.rows;
    if (st.f.view !== "ours" || narrowed()) {
      try { rows = (await api("GET", "/api/cad/comms?view=ours&limit=15")).rows || []; } catch (_) { return; }
    }
    st.ours = rows.filter((t) => t.kind !== "note" && t.main).slice(0, 15);
    RadioLive.offer(st.ours.map((t) => ({ id: t.id, start: t.start, has: t.has_audio, keyup: t.status === "no_speech" })));
    renderStrip();
  }
  // What is on screen has been seen: its sources' counts start again from now
  function seenNow() {
    if (!st.src.length || document.visibilityState === "hidden") return;
    const t = now();
    for (const s of st.src) if (st.f.source ? s.key === st.f.source : inView(s, st.f.view)) st.p.seen[s.key] = t;
    save();
  }
  function onPush() {
    if (!st.open) return;
    renderRail();                                     // who is talking now: each push
    const rev = pushed();
    if (!rev || rev === st.rev || st.busy) return;
    if (st.shown !== query()) return void page();
    delta().then(() => { sources(); ours(); });
  }

  // ---- drawing -----------------------------------------------------------------------------------------

  function mark(service, text) {
    const m = el("span", `rn-svc rn-${service || "any"}`, text || MARK[service] || "·");
    m.title = SERVICE[service] || "All";
    return m;
  }
  function dot(s) {
    const d = el("span", "rn-dot" + (s.live ? " live" : s.recent ? " recent" : ""));
    d.title = s.live ? "Transmitting now" : s.recent ? "Heard in the last 5 min" : s.last ? `Last heard ${fmtTime(s.last)}` : "Not heard";
    return d;
  }
  function count(n, ours) {
    const c = el("span", ours ? "badge rn-n" : "chip rn-n", String(n));
    c.title = `${n} new`;
    return c;
  }
  function emChip() {
    const c = el("span", "chip emergency", "EM");
    c.title = "Emergency: everything on this talkgroup is kept until it is cleared";
    return c;
  }
  // talking now (the state push) and open emergency windows, as source keys
  function liveKeys() {
    const out = new Set();
    for (const e of (cad.state && cad.state.talking) || []) {
      if (e.ended) continue;
      if (e.source) out.add(String(e.source));
      else if (e.tg !== undefined && e.tg !== null) out.add(`p25:${e.p25_system || "westchester"}:${e.tg}`);
      else if (e.channel) { out.add(`fm:${e.channel}`); out.add(`air:${e.channel}`); }
    }
    return out;
  }
  function flaggedKeys() {
    const wins = (cad.state && cad.state.alarms && cad.state.alarms.windows) || [];
    return new Set(wins.map((w) => w.source || `p25:${w.system || "westchester"}:${w.talkgroup}`));
  }
  function sourceRow(s) {
    const row = el("div", "rn-ch" + (st.f.source === s.key ? " on" : "") + (s.mute ? " mute" : ""));
    const b = el("button", "rn-name quiet");
    b.type = "button";
    b.dataset.source = s.key;
    b.title = [`${s.system_label || s.system} › ${s.group || "Other"}`, s.tip, s.department].filter(Boolean).join(" · ");
    b.append(dot(s), el("span", "trunc", s.name));
    if (s.code) b.append(el("span", "rn-tier", s.code));
    if (s.em) b.append(emChip());
    b.append(el("span", "spacer"));
    if (s.n) b.append(count(s.n, s.ours));
    const pin = el("button", "rn-act quiet sm icon", "★");
    pin.type = "button";
    pin.dataset.pin = s.key;
    pin.title = s.pin ? "Pinned: on top of the tree" : "Pin";
    pin.setAttribute("aria-pressed", String(s.pin));
    pin.setAttribute("aria-label", `Pin ${s.name}`);
    row.append(b, pin);
    if (!s.ours) {                                    // Our calls are never muted
      const mute = el("button", "rn-act quiet sm icon", s.mute ? "✕" : "♪");
      mute.type = "button";
      mute.dataset.mute = s.key;
      mute.title = s.mute ? "Muted: not in All, no count" : "Mute";
      mute.setAttribute("aria-pressed", String(s.mute));
      mute.setAttribute("aria-label", `Mute ${s.name}`);
      row.append(mute);
    }
    return row;
  }
  function renderRail() {
    if (!dom || !st.open) return;
    const m = model(st.src, st.p, now(), liveKeys(), flaggedKeys());
    const sig = JSON.stringify([st.f.view, st.f.source, st.p.rail, st.sheet, m.views, m.rows.map((s) => [s.key, s.name, s.n, s.live, s.recent, s.pin, s.mute, s.em, s.code])]);
    if (sig === st.railSig) return;
    st.railSig = sig;
    $("#rn").classList.toggle("mini", !st.p.rail);
    $("#rn-rail").classList.toggle("sheet", st.sheet);
    setText($("#rn-fold"), st.p.rail ? "«" : "»");
    $("#rn-fold").title = st.p.rail ? "Fold" : "Views and channels";
    $("#rn-fold").setAttribute("aria-expanded", String(st.p.rail));
    $("#rn-views").replaceChildren(...m.views.map((v) => {
      const b = el("button", "tab");
      b.type = "button";
      b.dataset.view = v.id;
      b.title = v.name;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(v.id === st.f.view && !st.f.source));
      b.append(mark(v.service, v.mark), el("span", "lbl", v.name), el("span", "spacer"));
      if (v.em) b.append(emChip());
      if (v.n && !(v.id === st.f.view && !st.f.source)) b.append(count(v.n, v.id === "ours"));
      if (v.live) b.append(dot({ live: true }));
      return b;
    }));
    const tree = [];
    if (m.pinned.length) tree.push(el("h3", "rn-sys", "Pinned"), ...m.pinned.map(sourceRow));
    for (const sys of m.systems) {
      tree.push(el("h3", "rn-sys", sys.label));
      for (const g of sys.groups) {
        if (sys.groups.length === 1 && g.name.toLowerCase() === String(sys.label).toLowerCase()) {   // "Analog > Analog": once
          tree.push(...g.sources.map(sourceRow));
          continue;
        }
        const head = el("div", "rn-grp");
        head.append(mark(g.service), el("span", "trunc", g.name), el("span", "spacer"));
        if (g.n) head.append(count(g.n, g.sources.some((s) => s.ours)));
        tree.push(head, ...g.sources.map(sourceRow));
      }
    }
    $("#rn-tree").replaceChildren(...tree);
    const s = st.f.source && m.rows.find((x) => x.key === st.f.source);
    setText($("#rn-crumb"), s ? `${s.system_label || s.system} › ${s.group || "Other"} › ${s.name}` : st.f.source
      || (VIEWS.find(([id]) => id === st.f.view) || VIEWS[0])[1]);
    show($("#rn-back"), !!st.f.source);
  }
  function renderStrip() {
    if (!dom) return;
    const box = $("#rn-ours"), t = st.ours[0];
    const on = st.open && !!t && (st.f.view !== "ours" || narrowed());
    show(box, on);
    if (!on) return;
    const who = t.rig || t.talker || t.radio_id || t.channel;
    const n = st.src.filter((s) => s.ours).reduce((a, s) => a + unread(s, st.p.seen[s.key], now()), 0);
    box.replaceChildren(mark("fire"), el("b", "", "Our calls"), ...(n ? [count(n, true)] : []),
                        el("span", "t", fmtTime(t.start)), el("span", "trunc", `${who}: ${t.text || "—"}`));
  }
  function fillFacets(d) {
    if (!d.rigs) return;
    const key = JSON.stringify([d.rigs, d.events]);
    if (key === st.facets) return;
    st.facets = key;
    const fill = (sel, first, opts, v) => {
      sel.replaceChildren(new Option(first, ""), ...opts.map(([val, text]) => new Option(text, val)));
      if (v && !opts.some(([val]) => val === v)) sel.append(new Option(v, v));
      sel.value = v || "";
    };
    fill($("#rn-unit"), "Any unit", (d.rigs || []).map((u) => [u, u]), st.f.unit);
    fill($("#rn-event"), "Any incident", (d.events || []).map((e) => [e.number || String(e.id), `${e.number || "#" + e.id} ${e.type || ""} ${e.address || ""}`.trim()]),
         st.f.event);
  }
  function fillRow(li, t) {
    const n = RadioView.row(t);
    if (t.redacted) {                                 // a police row, nobody signed in: the hub sent no words
      const txt = n.querySelector(".txt");
      if (txt) { txt.textContent = "Sign in to read"; txt.className = "txt muted"; }
    }
    li.className = n.className;
    li.replaceChildren(...n.childNodes);
  }
  function render() {
    if (!dom || !st.open) return;
    renderRail();
    renderStrip();
    $("#rn-q").value = document.activeElement === $("#rn-q") ? $("#rn-q").value : st.f.q;
    setText($("#rv-count"), st.error ? `Load failed: ${st.error}` : `${st.rows.filter((t) => t.kind !== "note").length} TX${st.older ? "+" : ""}`);
    syncList($("#rn-list"), st.rows, { key: (t) => t.id, sig: (t) => RadioView.rowSig(t) + (t.redacted ? "r" : ""), make: () => el("li"),
                                       fill: fillRow, empty: narrowed() ? "None match" : "None" });
    show($("#rn-older"), st.older);
  }

  // ---- in and out --------------------------------------------------------------------------------------

  function set(patch) {
    Object.assign(st.f, patch);
    st.sheet = false;
    st.railSig = "";
    toHash();
    $("#rn-event").value = st.f.event;
    $("#rn-unit").value = st.f.unit;
    page();
  }
  function open(qs) {
    const ui = new URLSearchParams(qs || "").get("ui");
    if (ui && st.p.on !== (ui === "2")) { st.p.on = ui === "2"; save(); }
    st.open = true;
    st.railSig = "";
    fromHash(qs);
    $("#radio-view").classList.add("nav-on");
    $("#rv-nav").setAttribute("aria-pressed", "true");
    show($("#rn"), true);
    render();
    page();
  }
  function close() {
    if (!dom) return;
    st.open = false;
    clearTimeout(st.timer);
    st.timer = 0;
    $("#radio-view").classList.remove("nav-on");
    $("#rv-nav").setAttribute("aria-pressed", "false");
    show($("#rn"), false);
  }
  function toggle(list, key) {
    const i = list.indexOf(key);
    if (i >= 0) list.splice(i, 1); else list.push(key);
    save();
    st.railSig = "";
  }

  if (dom) {
    $("#rv-nav").addEventListener("click", () => {    // on / off: the page as before stays one click away
      st.p.on = !st.p.on;
      save();
      history.replaceState(null, "", "#radio");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    $("#rn-fold").addEventListener("click", () => {
      if (st.sheet) st.sheet = false; else { st.p.rail = !st.p.rail; save(); }
      renderRail();
    });
    $("#rn-open").addEventListener("click", () => { st.sheet = !st.sheet; renderRail(); });
    $("#rn-back").addEventListener("click", () => set({ source: "" }));
    $("#rn-older").addEventListener("click", older);
    $("#rn-ours").addEventListener("click", () => set({ view: "ours", source: "", event: "", unit: "", q: "", channel: "" }));
    $("#rn-rail").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.view) return set({ view: b.dataset.view, source: "" });
      if (b.dataset.source) return set({ source: st.f.source === b.dataset.source ? "" : b.dataset.source });
      if (b.dataset.pin) { toggle(st.p.pins, b.dataset.pin); return renderRail(); }
      if (b.dataset.mute) { toggle(st.p.mutes, b.dataset.mute); renderRail(); if (st.f.view === "all") page(); }
    });
    $("#rn-event").addEventListener("change", () => set({ event: $("#rn-event").value, view: $("#rn-event").value && st.f.view === "ours" ? "all" : st.f.view }));
    $("#rn-unit").addEventListener("change", () => set({ unit: $("#rn-unit").value }));
    $("#rn-q").addEventListener("input", () => { clearTimeout(st.qTimer); st.qTimer = setTimeout(() => set({ q: $("#rn-q").value.trim() }), 350); });
    $("#rn-list").addEventListener("click", (e) => {  // an incident chip on a row: just that call's traffic
      const b = e.target.closest("[data-event]");
      if (b) set({ event: b.dataset.event, view: st.f.view === "ours" ? "all" : st.f.view });
    });
    cad.hooks.push(onPush);
    let drawn = -1;
    cad.hooks.push(() => { if (st.open && drawn !== cad.txRev) { drawn = cad.txRev; render(); } });
    window.addEventListener("sizeup:tx-changed", () => { if (st.open) page(); });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") { clearTimeout(st.timer); st.timer = 0; } else if (st.open) page();
    });
  }

  return { wants, open, close, prefs, inView, unread, model, get on() { return st.open; } };
})();
