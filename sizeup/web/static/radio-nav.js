// The Radio page with many sources (GitHub #415, layout B): a rail on the left with the views (Our calls, Police, EMS,
// Transit, Air, All) and the tree system > group > channel, one list on the right.
//
//   /cad#radio?ui=2            turns it on for this browser (ui=1: the page as before, radio-view.js); the Rail
//                              button on the Radio page does the same. Remembered (localStorage "sizeup.cad.radio.nav").
//   &event= &unit= &q= &channel= &emergency=1 &type= &pcfd=1 &since= &until=    the filters, kept in the address; an old
//                              link (#radio?event=26-000012, ?channel=Fire/EMS 15) opens filtered here too, in All
//   &view=police &source=<key>  read from a link, never written back: a reload or the Radio tab opens Our calls
//
// Our calls is the view the page opens on, every time. Live (RadioLive) plays only Our calls, whatever is on screen;
// any other view keeps the newest of Our calls in one line on top (a click: back to Our calls). A page going out now
// (South 2) shows over the list as on the page before.
// A view is a filter on the hub (GET /api/cad/comms?view=...): one page of 100, older ones by "Older" (?before=),
// and on each state push whose comms_rev moved only what changed since (?changed=: rows patched by id, `gone` taken
// off, `reload` asks afresh); the first page again every SAFETY_MS and when a link changes (sizeup:tx-changed), the
// older pages already loaded kept under it. One request at a time: what is asked meanwhile (a view picked, a change
// pushed) is done right after, never dropped.
// The rail is GET /api/cad/comms/sources (?rev=: unchanged while nothing was logged): each source with when it was
// last heard, its calls in the minutes of the last hour that had any, and an emergency nobody has acknowledged; the
// unread counts are worked out here from those and when this viewer last had the source on screen (the hub's clock:
// no per-viewer state on the hub).
//
// Per viewer, remembered: the rail folded (a strip of view marks with their counts), pinned channels (a Pinned group
// on top of the tree, in every view) and muted ones (not in All, no count). One of Our calls is never muted (a mute
// from before it became Main is dropped), and a mute never hides an emergency: the hub leaves neither out. An
// emergency on any channel shows over the list in every view (folded rail and phone too), with "Recording until"
// while its window is open (#128), and marks its channel, its view and the phone's Sources button.
// On a phone the rail is a sheet behind Sources (Escape closes it).
// A service is told by a letter in a shape (F P E T A), never by colour alone. The tier is a short code (PA TX REC OFF
// ENC), its meaning in the hover. Another system's traffic comes only to a viewer who may see it (the hub decides).
//
// Uses cad.js (el, $, show, setText, syncList, api, fmtTime, cad), radio.js (Radio.fillPaging) and radio-view.js
// (RadioView.row / rowSig, RadioLive): loaded after them. Tested in node (tests/test_radio_nav.py).
"use strict";

window.RadioNav = (() => {
  const KEY = "sizeup.cad.radio.nav";
  const SAFETY_MS = 30000, SOURCES_MS = 5000, RECENT_S = 300, PAGE = 100, MAX_KEYS = 100, SEEN_MAX = 1000;
  const VIEWS = [["ours", "Our calls", "F", "fire"], ["police", "Police", "P", "police"], ["ems", "EMS", "E", "ems"],
                 ["transit", "Transit", "T", "transit"], ["air", "Air", "A", "air"], ["all", "All", "∗", ""]];
  const MARK = { fire: "F", police: "P", ems: "E", transit: "T", air: "A" };
  const SERVICE = { fire: "Fire", police: "Police", ems: "EMS", transit: "Transit", air: "Air", other: "Other" };
  const TIER = { alert: ["PA", "Transcribe + pre-alert"], text: ["TX", "Record + transcribe"], record: ["REC", "Record only"],
                 off: ["OFF", "Ignored"], activity: ["ENC", "Encrypted: activity only"] };
  const KEPT = ["event", "unit", "q", "channel", "emergency", "type", "pcfd", "since", "until"];   // in the address
  const EXTRA = ["channel", "emergency", "type", "pcfd", "since", "until"];   // an old link's: a chip each, gone with the view
  const BLANK = () => ({ view: "ours", source: "", event: "", unit: "", q: "", channel: "", emergency: "", type: "", pcfd: "", since: "", until: "" });

  // ---- pure: what the rail shows -----------------------------------------------------------------------

  function prefs(raw) {
    let v = null;
    try { v = JSON.parse(raw || "null"); } catch (_) { /* as new */ }
    v = v && typeof v === "object" ? v : {};
    const keys = (x) => (Array.isArray(x) ? x.map(String).slice(-MAX_KEYS) : []);
    // when each source was last on screen: the newest SEEN_MAX (a source dropped here would read as never seen)
    const seen = Object.fromEntries(Object.entries(v.seen && typeof v.seen === "object" ? v.seen : {}).filter(([, t]) => t > 0)
      .sort((a, b) => b[1] - a[1]).slice(0, SEEN_MAX).map(([k, t]) => [k, +t]));
    return { on: !!v.on, rail: v.rail !== false, pins: keys(v.pins), mutes: keys(v.mutes), seen };
  }
  // ours: our fire channels; a service's view: its sources that aren't ours; all: every source
  const inView = (s, view) => (view === "all" ? true : view === "ours" ? !!s.ours : !s.ours && s.service === view);
  // Calls on a source in the minutes after this viewer last had it on screen. counts: [[minutes ago, calls]] as of
  // `now` (the hub's clock). Never seen: nothing is new (a first visit doesn't start with an hour of counts).
  function unread(s, seen, now) {
    if (!seen) return 0;
    const end = Math.floor(now / 60), from = Math.floor(seen / 60);
    let n = 0;
    for (const [ago, calls] of s.counts || []) if (end - ago > from) n += calls;
    return n;
  }
  // The mutes that count: never one of Our calls (a channel muted, then made Main: its mute is dropped)
  const mutesFor = (mutes, sources) => { const ours = new Set(sources.filter((s) => s.ours).map((s) => s.key)); return mutes.filter((k) => !ours.has(k)); };
  const emText = (s) => (s.em === "ours" ? "EMERGENCY" : s.stuck ? `Emergency flag stuck on ${s.stuck}` : "Emergency (not ours)");
  // The services whose emergencies stand out when they aren't ours (Settings > Radios > Emergencies; cad.js emLoud):
  // anyone else's (a bus, DOT) is quiet: a muted EM on its channel only, never over the list, its view or Sources.
  const EM_LOUD = ["fire", "ems"];
  const loudServices = () => {
    const v = typeof cad !== "undefined" && cad.state && cad.state.radio_emergencies;
    return v && Array.isArray(v.prominent) ? v.prominent : EM_LOUD;
  };
  // live: source keys transmitting now; windows: {source key: until} of the open emergency windows; loud: the services
  // whose emergencies (not ours) stand out
  function model(sources, p, now, live = new Set(), windows = {}, loud = loudServices()) {
    const pins = new Set(p.pins), mutes = new Set(mutesFor(p.mutes, sources));
    const rows = sources.map((s) => {
      const mute = mutes.has(s.key);
      const t = TIER[s.tier] || null;
      const until = windows[s.key] || null;
      const em = s.emergency || (until ? "other" : "");
      return { ...s, mute, pin: pins.has(s.key), n: mute ? 0 : unread(s, p.seen[s.key], now),
               live: !!s.live || live.has(s.key), recent: !!s.last && now - s.last < RECENT_S,
               em, quiet: !!em && em !== "ours" && !s.ours && !loud.includes(s.service || "other"),
               until, code: t ? t[0] : "", tip: t ? t[1] : "" };
    });
    const views = VIEWS.filter(([v]) => v === "ours" || v === "all" || rows.some((s) => inView(s, v))).map(([id, name, mark, service]) => {
      const mine = rows.filter((s) => inView(s, id));
      const em = mine.filter((s) => s.em && !s.quiet);     // an emergency shows on its views, muted or not (quiet: never)
      return { id, name, mark, service, n: id === "all" ? 0 : mine.reduce((a, s) => a + s.n, 0), live: mine.some((s) => s.live && !s.mute),
               em: em.some((s) => s.em === "ours") ? "ours" : em.length ? "other" : "" };
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
    return { views, systems, pinned: rows.filter((s) => s.pin), rows, flagged: rows.filter((s) => (s.em || s.until) && !s.quiet) };
  }
  // The first page asked again while older pages are on screen: the fresh page, then what was loaded under it
  function merged(fresh, have) {
    if (!fresh.length) return [];
    const last = fresh[fresh.length - 1], ids = new Set(fresh.map((t) => t.id));
    const older = (t) => t.start < last.start || (t.start === last.start && t.id < last.id);
    return fresh.concat(have.filter((t) => !ids.has(t.id) && older(t)));
  }
  const byNewest = (a, b) => (b.start - a.start) || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0);

  // ---- state -------------------------------------------------------------------------------------------

  const st = { open: false, p: prefs(null), src: [], srcRev: "", srcLive: [], srcAt: 0, clock: null, rows: [], cursor: 0, next: "",
               older: false, deep: false, rev: "", busy: false, want: "", timer: 0, error: "", f: BLANK(), ours: [], railSig: "",
               flagSig: "", sheet: false, qTimer: 0, facets: "", shown: null, hash: null, offered: [] };
  const dom = typeof document !== "undefined" && typeof $ === "function";
  const load = () => { try { st.p = prefs(localStorage.getItem(KEY)); } catch (_) { st.p = prefs(null); } };
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(st.p)); } catch (_) { /* not remembered */ } };
  load();
  // the hub's clock, carried on from the last tree (the counts and "seen" are both in it)
  const now = () => (st.clock ? st.clock.hub + (Date.now() - st.clock.at) / 1000 : Date.now() / 1000);
  const narrowed = () => !!(st.f.source || KEPT.some((k) => st.f[k]));

  // on / off: ?ui=2 / ?ui=1 in the address, else what this browser chose
  function wants(qs) {
    const ui = new URLSearchParams(qs || "").get("ui");
    return ui === "2" || (ui !== "1" && st.p.on);
  }
  function query(extra = {}) {
    const q = new URLSearchParams(), f = st.f;
    q.set("view", f.source ? "all" : f.view);
    if (f.source) q.set("source", f.source);
    for (const k of KEPT) if (f[k]) q.set(k, f[k]);
    const mutes = mutesFor(st.p.mutes, st.src);
    if (f.view === "all" && !f.source && mutes.length) q.set("mute", mutes.join(","));
    q.set("limit", String(PAGE));
    for (const [k, v] of Object.entries(extra)) q.set(k, String(v));
    return q.toString();
  }
  function hashOf() {                                 // the filters only: never the view or the source
    const q = new URLSearchParams();
    for (const k of KEPT) if (st.f[k]) q.set(k, st.f[k]);
    return q.toString();
  }
  function toHash() {
    st.hash = hashOf();
    history.replaceState(null, "", "#radio" + (st.hash ? `?${st.hash}` : ""));
  }
  function fromHash(qs) {
    const q = new URLSearchParams(qs || ""), f = BLANK();
    for (const k of ["source", ...KEPT]) f[k] = q.get(k) || "";
    const v = q.get("view") || "";
    // an old link (an incident's "All", the feed's More): its filter, over every source
    f.view = VIEWS.some(([id]) => id === v) ? v : KEPT.some((k) => f[k]) ? "all" : "ours";
    st.f = f;
  }

  // ---- the hub: one request at a time, what is asked meanwhile done right after ------------------------------

  const shown = () => st.open && location.hash.startsWith("#radio") && document.visibilityState !== "hidden";
  const pushed = () => (typeof cad !== "undefined" && cad.state && cad.state.comms_rev) || "";
  function settle() {                                 // a request is back: what was asked while it was out
    st.busy = false;
    const want = st.want;
    st.want = "";
    if (!shown()) return;
    if (want === "page" || st.shown !== query()) return page();
    if (want === "delta") return delta();
    if (want === "older") return older();
    return null;
  }

  async function page() {                             // the first page of what is asked
    clearTimeout(st.timer);
    st.timer = 0;
    if (!shown()) return;
    if (st.busy) { st.want = "page"; return; }
    st.busy = true;
    const asked = query();
    try {
      const d = await api("GET", `/api/cad/comms?${asked}`);
      st.error = "";
      if (asked === query()) {
        const same = st.shown === asked && st.deep;   // older pages on screen: kept under the fresh first page
        st.rows = same ? merged(d.rows || [], st.rows) : d.rows || [];
        if (!same) Object.assign(st, { next: d.next || "", older: !!d.older, deep: false });
        Object.assign(st, { cursor: d.cursor || 0, rev: d.rev || "", shown: asked });
        fillFacets(d);
        seenNow();
      }
    } catch (e) { st.error = e.message; }
    render();
    if (shown()) st.timer = setTimeout(page, SAFETY_MS);
    await settle();
    sources();
    ours();
  }
  async function delta() {                            // only what changed since the page on screen
    if (!shown()) return;
    if (st.busy) { st.want = st.want || "delta"; return; }
    if (st.shown !== query()) { page(); return; }
    st.busy = true;
    const asked = st.shown;
    try {
      const d = await api("GET", `/api/cad/comms?${query({ changed: st.cursor })}`);
      if (asked === query()) {
        st.rev = pushed() || st.rev;
        if (d.reload) st.want = "page";
        else {
          const by = new Map(st.rows.map((t) => [t.id, t]));
          for (const id of d.gone || []) by.delete(id);
          for (const t of d.rows || []) by.set(t.id, t);
          st.rows = [...by.values()].sort(byNewest);
          st.cursor = d.cursor || st.cursor;
          if ((d.rows || []).length) seenNow();
          render();
        }
      }
    } catch (_) { /* the next push, or the safety page */ }
    await settle();
  }
  async function older() {
    if (!st.older) return;
    if (st.busy) { st.want = st.want || "older"; return; }    // a click while a request is out: done right after it
    st.busy = true;
    const asked = query();
    try {
      const d = await api("GET", `/api/cad/comms?${query({ before: st.next })}`);
      if (asked === query() && asked === st.shown) {
        const have = new Set(st.rows.map((t) => t.id));
        st.rows = st.rows.concat((d.rows || []).filter((t) => !have.has(t.id)));
        st.next = d.next || st.next;
        st.older = !!d.older;
        st.deep = true;
      }
    } catch (e) { st.error = e.message; }
    render();
    await settle();
  }
  async function sources(force) {
    if (!shown() || (!force && Date.now() - st.srcAt < SOURCES_MS)) return;
    st.srcAt = Date.now();
    try {
      const d = await api("GET", "/api/cad/comms/sources" + (st.srcRev ? `?rev=${encodeURIComponent(st.srcRev)}` : ""));
      st.srcLive = d.live || [];
      if (!d.unchanged) {
        st.src = d.sources || [];
        st.srcRev = d.rev || "";
        st.clock = { hub: d.now || Date.now() / 1000, at: Date.now() };
        let changed = false;                          // a source first met: nothing of it is unread
        for (const s of st.src) if (!st.p.seen[s.key]) { st.p.seen[s.key] = now(); changed = true; }
        const mutes = mutesFor(st.p.mutes, st.src);   // muted, then made one of Our calls: not muted any more
        if (mutes.length !== st.p.mutes.length) { st.p.mutes = mutes; changed = true; }
        if (changed) save();
        seenNow();
      }
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
    st.offered = st.ours.map((t) => ({ id: t.id, start: t.start, has: t.has_audio, keyup: t.status === "no_speech" }));
    RadioLive.offer(st.offered);
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
    renderRail();                                     // who is talking now, an emergency window: each push
    renderPaging();
    const rev = pushed();
    if (!rev || rev === st.rev) return;
    if (st.busy) { st.want = st.want || "delta"; return; }
    Promise.resolve(delta()).then(() => { sources(); ours(); });
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
  function emChip(em, text, title, quiet = false) {
    const c = el("span", quiet ? "chip em-quiet" : "chip emergency" + (em === "ours" ? "" : " em-other"), text || "EM");
    c.title = title || (em === "ours" ? "Emergency" : "Emergency (not ours)");
    return c;
  }
  function keptChip(until) {
    const u = el("span", "chip kept", `Recording until ${fmtTime(until)} (emergency)`);
    u.title = "The emergency window: everything on this talkgroup is kept until then, even with Record off "
      + "(the alarm's All clear ends it)";
    return u;
  }
  // talking now (the state push, and the tree's own list) and the open emergency windows, by source key
  function liveKeys() {
    const out = new Set(st.srcLive);
    for (const e of (cad.state && cad.state.talking) || []) {
      if (e.ended) continue;
      if (e.source) out.add(String(e.source));
      else if (e.tg !== undefined && e.tg !== null) out.add(`p25:${e.p25_system || "westchester"}:${e.tg}`);
      else if (e.channel) { out.add(`fm:${e.channel}`); out.add(`air:${e.channel}`); }
    }
    return out;
  }
  function windowKeys() {
    const out = {};
    for (const w of (cad.state && cad.state.alarms && cad.state.alarms.windows) || []) {
      if (w.until) out[w.source || `p25:${w.system || "westchester"}:${w.talkgroup}`] = w.until;
    }
    return out;
  }
  function sourceRow(s) {
    const row = el("div", "rn-ch" + (st.f.source === s.key ? " on" : "") + (s.mute ? " mute" : ""));
    const b = el("button", "rn-name quiet");
    b.type = "button";
    b.dataset.source = s.key;
    b.title = [`${s.system_label || s.system} › ${s.group || "Other"}`, s.tip, s.department].filter(Boolean).join(" · ");
    b.append(dot(s), el("span", "trunc", s.name));
    if (s.code) b.append(el("span", "rn-tier", s.code));
    if (s.em) b.append(emChip(s.em, "EM", emText(s), s.quiet));
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
      mute.title = s.mute ? "Muted: not in All, no count (an emergency still shows)" : "Mute";
      mute.setAttribute("aria-pressed", String(s.mute));
      mute.setAttribute("aria-label", `Mute ${s.name}`);
      row.append(mute);
    }
    return row;
  }
  function renderRail() {
    if (!dom || !st.open) return;
    const m = model(st.src, st.p, now(), liveKeys(), windowKeys());
    const here = (v) => v.id === st.f.view && !st.f.source;
    const sig = JSON.stringify([st.f.view, st.f.source, st.p.rail, st.sheet, m.views, m.rows.map((s) => [s.key, s.name, s.n, s.live, s.recent, s.pin, s.mute, s.em, s.quiet, s.stuck, s.until, s.code])]);
    if (sig === st.railSig) return;
    st.railSig = sig;
    $("#rn").classList.toggle("mini", !st.p.rail);
    $("#rn-rail").classList.toggle("sheet", st.sheet);
    setText($("#rn-fold"), st.sheet ? "✕" : st.p.rail ? "«" : "»");
    $("#rn-fold").title = st.sheet ? "Close" : st.p.rail ? "Fold" : "Views and channels";
    $("#rn-fold").setAttribute("aria-expanded", String(st.sheet || st.p.rail));
    $("#rn-views").replaceChildren(...m.views.map((v) => {
      const b = el("button", "tab");
      b.type = "button";
      b.dataset.view = v.id;
      b.title = v.name;
      b.tabIndex = here(v) || (st.f.source && v.id === st.f.view) ? 0 : -1;      // one stop; the arrows move between them
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(here(v)));
      b.append(mark(v.service, v.mark), el("span", "lbl", v.name), el("span", "spacer"));
      if (v.em) b.append(emChip(v.em));
      if (v.n && !here(v)) b.append(count(v.n, v.id === "ours"));
      if (v.live) b.append(dot({ live: true }));
      return b;
    }));
    const tree = [];
    if (m.pinned.length) tree.push(el("div", "rn-sys h2", "Pinned"), ...m.pinned.map(sourceRow));
    for (const sys of m.systems) {
      tree.push(el("div", "rn-sys h2", sys.label));
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
    // the phone's Sources button: what the other views hold (the rail is behind it)
    const away = m.views.filter((v) => !here(v) && v.id !== "all");
    // (an emergency on any channel not on screen, also one that is in no view but All)
    const off = m.rows.filter((x) => x.em && !x.quiet && !(st.f.source ? x.key === st.f.source : inView(x, st.f.view)));
    const n = away.reduce((a, v) => a + v.n, 0), em = off.some((x) => x.em === "ours") ? "ours" : off.length ? "other" : "";
    $("#rn-open").replaceChildren(el("span", "", "Sources"), ...(em ? [emChip(em)] : []),
                                  ...(n ? [count(n, away.some((v) => v.id === "ours" && v.n))] : []));
    renderFlags(m);
  }
  // An emergency on any channel, over the list in every view: what it is, where, and its window (#128)
  function renderFlags(m) {
    const sig = JSON.stringify(m.flagged.map((s) => [s.key, s.name, s.em, s.stuck, s.until]));
    if (sig === st.flagSig) return;
    st.flagSig = sig;
    const box = $("#rn-flags");
    show(box, m.flagged.length > 0);
    box.replaceChildren(...m.flagged.flatMap((s) => {
      const out = [];
      if (s.em) {
        const b = el("button", "chip emergency" + (s.em === "ours" ? "" : " em-other") + (s.stuck ? " em-stuck" : ""), `${emText(s)}: ${s.name}`);
        b.type = "button";
        b.dataset.source = s.key;
        b.title = "Show this channel";
        out.push(b);
      }
      if (s.until) out.push(keptChip(s.until));
      return out;
    }));
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
  // A page going out now (South 2), over the list until its finished row is in it
  function renderPaging() {
    if (!dom || !st.open || !window.Radio) return;
    Radio.fillPaging($("#rn-paging"), cad.state && cad.state.paging, new Set(st.rows.map((t) => t.id)));
  }
  const CHIP = { channel: (v) => v, emergency: () => "Emergency", type: (v) => (v === "notes" ? "Notes" : "Transmissions"), pcfd: () => "PCFD only",
                 since: (v) => `From ${fmtTime(+v)}`, until: (v) => `To ${fmtTime(+v)}` };
  function renderChips() {
    $("#rn-chips").replaceChildren(...EXTRA.filter((k) => st.f[k]).map((k) => {
      const b = el("button", "chip rn-chip", `${CHIP[k](st.f[k])} ✕`);
      b.type = "button";
      b.dataset.clear = k;
      b.title = "Clear";
      return b;
    }));
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
    li.className = n.className;
    li.replaceChildren(...n.childNodes);
  }
  function render() {
    if (!dom || !st.open) return;
    renderRail();
    renderStrip();
    renderPaging();
    renderChips();
    if (document.activeElement !== $("#rn-q")) $("#rn-q").value = st.f.q;
    setText($("#rv-count"), st.error ? `Load failed: ${st.error}` : `${st.rows.filter((t) => t.kind !== "note").length} TX${st.older ? "+" : ""}`);
    syncList($("#rn-list"), st.rows, { key: (t) => t.id, sig: RadioView.rowSig, make: () => el("li"), fill: fillRow,
                                       empty: narrowed() ? "None match" : "None" });
    show($("#rn-older"), st.older);
  }

  // ---- in and out --------------------------------------------------------------------------------------

  function set(patch) {
    if ("view" in patch && patch.view !== st.f.view) for (const k of EXTRA) st.f[k] = "";   // an old link's filters: gone with the view
    Object.assign(st.f, patch);
    st.sheet = false;
    st.railSig = "";
    toHash();
    $("#rn-event").value = st.f.event;
    $("#rn-unit").value = st.f.unit;
    render();
    page();
  }
  function open(qs) {
    const q = new URLSearchParams(qs || ""), ui = q.get("ui");
    if (ui && st.p.on !== (ui === "2")) { st.p.on = ui === "2"; save(); }
    const was = st.open;
    st.open = true;
    st.railSig = st.flagSig = "";
    if (!was || (qs || "") !== st.hash) {             // not our own address coming back: a link, the Radio tab, a reload
      fromHash(qs);
      toHash();                                       // the view and the source are never kept in the address
    }
    $("#radio-view").classList.add("nav-on");
    $("#rv-nav").setAttribute("aria-pressed", "true");
    show($("#rn"), true);
    render();
    page();
  }
  function close() {
    if (!dom) return;
    st.open = false;
    st.sheet = false;
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
  function sheet(on) {
    st.sheet = on;
    renderRail();
    if (on) { const t = $("#rn-views").querySelector('[tabindex="0"]'); if (t && t.focus) t.focus(); }
    else if ($("#rn-open").focus) $("#rn-open").focus();
  }
  function onKeys(e) {                                // the views are a tablist: the arrows, Home and End move between them
    const tabs = [...$("#rn-views").children];
    const i = tabs.indexOf(e.target.closest ? e.target.closest(".tab") : null);
    const to = { ArrowDown: i + 1, ArrowRight: i + 1, ArrowUp: i - 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (i < 0 || to === undefined) return;
    e.preventDefault();
    const n = tabs[(to + tabs.length) % tabs.length];
    for (const t of tabs) t.tabIndex = t === n ? 0 : -1;
    n.focus();
  }

  if (dom) {
    $("#rv-nav").addEventListener("click", () => {    // on / off: the page as before stays one click away
      st.p.on = !st.p.on;
      save();
      history.replaceState(null, "", "#radio");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    $("#rn-fold").addEventListener("click", () => {
      if (st.sheet) return sheet(false);
      st.p.rail = !st.p.rail;
      save();
      return renderRail();
    });
    $("#rn-open").addEventListener("click", () => sheet(!st.sheet));
    $("#rn-back").addEventListener("click", () => set({ source: "" }));
    $("#rn-older").addEventListener("click", older);
    $("#rn-ours").addEventListener("click", () => set({ ...BLANK() }));
    $("#rn-rail").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.view) return set({ view: b.dataset.view, source: "" });
      if (b.dataset.source) return set({ source: st.f.source === b.dataset.source ? "" : b.dataset.source });
      if (b.dataset.pin) { toggle(st.p.pins, b.dataset.pin); return renderRail(); }
      if (b.dataset.mute) { toggle(st.p.mutes, b.dataset.mute); renderRail(); if (st.f.view === "all") page(); }
    });
    $("#rn-views").addEventListener("keydown", onKeys);
    $("#rn-flags").addEventListener("click", (e) => { const b = e.target.closest("[data-source]"); if (b) set({ source: b.dataset.source }); });
    $("#rn-chips").addEventListener("click", (e) => { const b = e.target.closest("[data-clear]"); if (b) set({ [b.dataset.clear]: "" }); });
    $("#rn-event").addEventListener("change", () => set({ event: $("#rn-event").value, view: $("#rn-event").value && st.f.view === "ours" ? "all" : st.f.view }));
    $("#rn-unit").addEventListener("change", () => set({ unit: $("#rn-unit").value }));
    $("#rn-q").addEventListener("input", () => { clearTimeout(st.qTimer); st.qTimer = setTimeout(() => set({ q: $("#rn-q").value.trim() }), 350); });
    $("#rn-list").addEventListener("click", (e) => {  // an incident chip on a row: just that call's traffic (once: radio-view.js stands down)
      const b = e.target.closest("[data-event]");
      if (b) set({ event: b.dataset.event, view: st.f.view === "ours" ? "all" : st.f.view });
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && st.open && st.sheet) { e.stopPropagation(); sheet(false); } }, true);
    cad.hooks.push(onPush);
    let drawn = -1;
    cad.hooks.push(() => { if (st.open && drawn !== cad.txRev) { drawn = cad.txRev; render(); } });
    window.addEventListener("sizeup:tx-changed", () => { if (st.open) page(); });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") { clearTimeout(st.timer); st.timer = 0; } else if (st.open) page();
    });
  }

  const out = { wants, open, close, prefs, inView, unread, mutesFor, model, merged, get on() { return st.open; } };
  // its insides, for tests/test_radio_nav.py only (the harness sets this before the script runs): never on the page
  if (window.RADIO_NAV_TEST === true) out.test = { st, page, delta, older, sources, ours, set, onPush, query, fromHash, hashOf, render };
  return out;
})();
