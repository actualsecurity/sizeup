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
// on top of the tree, in every view) and muted ones (not in All, no count). One of Our calls or a fire channel of ours
// is never muted (a mute from before is dropped), and a mute never hides an emergency: the hub leaves neither out. An
// emergency (an alarm row not yet cleared, the tree's `emergency`; an emergency window alone marks nothing) shows over
// the list in every view (folded rail and phone too), and marks its channel, its view and the phone's Sources button;
// "Recording until" while a talkgroup's emergency window is open (#128).
// On a phone the rail is a sheet behind Sources (Escape closes it).
//
// Folders (#607): the tree is headings and folders. By default the automatic grouping: a heading per system (County,
// another P25 system, Analog, Airband), a folder per group (the talkgroup's Tag; Analog, Airband), the hub's order.
// An admin's folders for the department come with the tree (its `folders`, sizeup/cad/radiofolders.py): their names,
// headings, order and channels; a source in none (a talkgroup heard for the first time) goes to its default folder,
// that folder made again next to its heading if the admins' list doesn't have it: nothing ever drops off the rail.
// Edit (admins): rename, add, delete (an empty one), heading, move and reorder by drag and drop, or by ▲ ▼ and Move to
// (touch, keys); Reset goes back to the automatic grouping; nothing is saved until Save (POST
// /api/cad/settings/radio_folders, based on the rev it started from: someone else saved meanwhile -> refused).
// Each viewer folds folders (remembered here, never on the hub): a folded one shows its name, how many channels, the
// new calls, an emergency and a live dot; the channel on screen stays under its folded folder. The state push's
// radio_folders (their rev) asks the tree again when an admin saved.
// Each channel's service in Edit too (Auto, or Fire / Police / EMS / Transit / Air / Other: the settings section
// radio_services, sizeup/sourcekeys.py): sent with Save (POST /api/cad/settings/radio_services set_many), before the
// folders; the hub re-tags that channel's recent rows. The push's radio_services (their rev) asks the tree again.
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
  const SERVICES = Object.keys(SERVICE);                 // (sizeup/sourcekeys.py SERVICES)
  const OURS_SYSTEMS = ["westchester", "analog", "airband"];   // (sizeup/sourcekeys.py OURS_SYSTEMS): a fire one of ours is never muted
  const TIER = { alert: ["PA", "Transcribe + pre-alert"], text: ["TX", "Record + transcribe"], record: ["REC", "Record only"],
                 off: ["OFF", "Ignored"], activity: ["ENC", "Encrypted: activity only"] };
  const KEPT = ["event", "unit", "q", "channel", "emergency", "type", "pcfd", "since", "until"];   // in the address
  const EXTRA = ["channel", "emergency", "type", "pcfd", "since", "until"];   // an old link's: a chip each, gone with the view
  const FOLD_MAX = 300;                                  // folded folders remembered (their ids)
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
    const folds = Array.isArray(v.folds) ? v.folds.map(String).slice(-FOLD_MAX) : [];
    return { on: !!v.on, rail: v.rail !== false, pins: keys(v.pins), mutes: keys(v.mutes), seen, folds };
  }
  // The folders, in order (#607): the admins' (custom: {folders: [{id, name, section, items}]} | null), then every
  // source in none of them in its default folder ("d:<system>:<group>": there if listed, else made again after the
  // last folder under its heading). Each source once (the first folder that lists it wins). Empty folders kept (Edit
  // shows them; the tree leaves them out).
  const defaultId = (s) => `d:${s.system}:${s.group || "Other"}`;
  function folders(rows, custom) {
    const out = [], by = new Map(), placed = new Set(), row = new Map(rows.map((s) => [s.key, s]));
    for (const f of (custom && Array.isArray(custom.folders) ? custom.folders : [])) {
      if (!f || typeof f.id !== "string" || by.has(f.id)) continue;
      const g = { id: f.id, name: String(f.name || ""), section: String(f.section || ""), sources: [] };
      for (const k of Array.isArray(f.items) ? f.items : []) {
        const s = row.get(k);
        if (s && !placed.has(k)) { placed.add(k); g.sources.push(s); }
      }
      out.push(g);
      by.set(g.id, g);
    }
    for (const s of rows) {
      if (placed.has(s.key)) continue;
      const id = defaultId(s);
      let g = by.get(id);
      if (!g) {
        g = { id, name: s.group || "Other", section: s.system_label || s.system || "", sources: [] };
        let at = out.length;
        for (let i = out.length - 1; i >= 0; i--) if (out[i].section === g.section) { at = i + 1; break; }
        out.splice(at, 0, g);
        by.set(id, g);
      }
      g.sources.push(s);
    }
    return out;
  }
  // folders in a row under the same heading share it
  function sections(fs) {
    const out = [];
    for (const g of fs) {
      const last = out[out.length - 1];
      if (last && last.label === g.section) last.groups.push(g); else out.push({ label: g.section, groups: [g] });
    }
    return out;
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
  // ... nor a fire channel of ours, in any view (the hub says so: mutable false)
  const fixed = (s) => !!s.ours || s.mutable === false;
  const mutesFor = (mutes, sources) => { const ours = new Set(sources.filter(fixed).map((s) => s.key)); return mutes.filter((k) => !ours.has(k)); };
  const emText = (s) => (s.em === "ours" ? "EMERGENCY" : s.stuck ? `Emergency flag stuck on ${s.stuck}` : "Emergency (not ours)");
  // The services whose emergencies stand out when they aren't ours (Settings > Radios > Emergencies; cad.js emLoud):
  // anyone else's (a bus, DOT) is quiet: a muted EM on its channel only, never over the list, its view or Sources.
  const EM_LOUD = ["fire", "ems", "other"];
  const loudServices = () => {
    const v = typeof cad !== "undefined" && cad.state && cad.state.radio_emergencies;
    return v && Array.isArray(v.prominent) ? v.prominent : EM_LOUD;
  };
  // live: source keys transmitting now; windows: {source key: until} of the open emergency windows; loud: the services
  // whose emergencies (not ours) stand out
  function model(sources, p, now, live = new Set(), windows = {}, loud = loudServices(), custom = null) {
    const pins = new Set(p.pins), mutes = new Set(mutesFor(p.mutes, sources));
    const names = (custom && custom.names) || {};       // the admins' names for the rail (#607): display only
    const rows = sources.map((s) => {
      const mute = mutes.has(s.key);
      const t = TIER[s.tier] || null;
      const until = windows[s.key] || null;
      const em = s.emergency || "";
      return { ...s, name: names[s.key] || s.name, orig: s.name, mute, pin: pins.has(s.key), n: mute ? 0 : unread(s, p.seen[s.key], now),
               live: !!s.live || live.has(s.key), recent: !!s.last && now - s.last < RECENT_S,
               // quiet: someone else's emergency (or its window alone) on a service that is known and doesn't
               // stand out; a source with no service is never quiet
               em, quiet: !!(em || until) && em !== "ours" && !s.ours && !!s.service && !loud.includes(s.service),
               until, code: t ? t[0] : "", tip: t ? t[1] : "" };
    });
    const views = VIEWS.filter(([v]) => v === "ours" || v === "all" || rows.some((s) => inView(s, v))).map(([id, name, mark, service]) => {
      const mine = rows.filter((s) => inView(s, id));
      const em = mine.filter((s) => s.em && !s.quiet);     // an emergency shows on its views, muted or not (quiet: never)
      return { id, name, mark, service, n: id === "all" ? 0 : mine.reduce((a, s) => a + s.n, 0), live: mine.some((s) => s.live && !s.mute),
               em: em.some((s) => s.em === "ours") ? "ours" : em.length ? "other" : "" };
    });
    // each folder: one service (its mark) or none (mixed), its new calls, live (not muted), its emergency (what a
    // folded folder shows); the tree leaves out an empty one
    const fs = folders(rows, custom).filter((g) => g.sources.length).map((g) => {
      const svc = new Set(g.sources.map((s) => s.service || ""));
      const em = g.sources.filter((s) => s.em);
      return { ...g, service: svc.size === 1 ? [...svc][0] : "", n: g.sources.reduce((a, s) => a + s.n, 0),
               ours: g.sources.some((s) => s.ours), live: g.sources.some((s) => s.live && !s.mute),
               em: em.some((s) => s.em === "ours" && !s.quiet) ? "ours" : em.some((s) => !s.quiet) ? "other" : em.length ? "quiet" : "" };
    });
    return { views, sections: sections(fs), pinned: rows.filter((s) => s.pin), rows, flagged: rows.filter((s) => (s.em || s.until) && !s.quiet) };
  }

  // ---- pure: Edit's draft ([{id, name, section, items: [keys]}]) and what it does to it -------------------------
  // what a channel really is, for its hover: the talkgroup's number, an FM / airband channel's frequency
  const idOf = (s) => (s.talkgroup !== undefined && s.talkgroup !== null ? `TG ${s.talkgroup}` : s.mhz ? `${s.mhz} MHz` : "");
  const draftOf = (rows, custom) => folders(rows, custom).map((g) => ({ id: g.id, name: g.name, section: g.section, items: g.sources.map((s) => s.key) }));
  const holder = (d, key) => d.find((f) => f.items.includes(key));
  // Two stored layouts with the same folders (null: none)
  const sameFolders = (a, b) => JSON.stringify((a && a.folders) || []) === JSON.stringify((b && b.folders) || []);
  // The names saved meanwhile (theirs), with this edit's own changes from where it began (start -> mine) over them
  function mergedNames(start, mine, theirs) {
    const out = { ...theirs };
    for (const k of new Set([...Object.keys(start), ...Object.keys(mine)])) {
      const was = String(start[k] || "").trim(), now = String(mine[k] || "").trim();
      if (was === now) continue;
      if (now) out[k] = now; else delete out[k];
    }
    return out;
  }
  // a channel into folder `to`, before `before` (a key in it) or at its end
  function moveItem(d, key, to, before = "") {
    const from = holder(d, key), dest = d.find((f) => f.id === to);
    if (!from || !dest || key === before) return d;
    from.items.splice(from.items.indexOf(key), 1);
    const i = before ? dest.items.indexOf(before) : -1;
    dest.items.splice(i < 0 ? dest.items.length : i, 0, key);
    return d;
  }
  // a folder before folder `before` (or last); the heading of the folder it lands on top of (a folder dropped into
  // another heading's run joins it)
  function moveFolder(d, id, before = "") {
    const i = d.findIndex((f) => f.id === id);
    if (i < 0 || id === before) return d;
    const [f] = d.splice(i, 1);
    const j = before ? d.findIndex((x) => x.id === before) : -1;
    d.splice(j < 0 ? d.length : j, 0, f);
    const at = d.indexOf(f), next = d[at + 1], prev = d[at - 1];
    if (next && prev && next.section === prev.section) f.section = next.section;
    return d;
  }
  // ▲ ▼: a channel one up / down (across into the next folder at an end); a folder one up / down
  function step(d, kind, id, dir) {
    if (kind === "folder") {
      const i = d.findIndex((f) => f.id === id), j = i + dir;
      if (i < 0 || j < 0 || j >= d.length) return d;
      [d[i], d[j]] = [d[j], d[i]];
      const f = d[j], next = d[j + 1], prev = d[j - 1];      // into another heading's run: it joins it (as a drop does)
      if (next && prev && next.section === prev.section) f.section = next.section;
      return d;
    }
    const f = holder(d, id);
    if (!f) return d;
    const i = f.items.indexOf(id), j = i + dir;
    if (j >= 0 && j < f.items.length) { [f.items[i], f.items[j]] = [f.items[j], f.items[i]]; return d; }
    const n = d[d.indexOf(f) + dir];                  // past the end: into the next folder (its top) / the one before (its end)
    if (!n) return d;
    f.items.splice(i, 1);
    if (dir > 0) n.items.unshift(id); else n.items.push(id);
    return d;
  }
  const newId = () => `u:${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const sameDraft = (a, b) => JSON.stringify(a) === JSON.stringify(b);
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
               flagSig: "", sheet: false, qTimer: 0, facets: "", shown: null, hash: null, offered: [],
               layout: null, layoutAsked: null, edit: null, drag: null };
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
        st.layout = d.folders || null;                // the admins' folders (#607), null: the automatic grouping
        st.services = d.services || {};               // the admins' service per channel (none: automatic)
        st.retagWaiting = new Set(d.retag_waiting || []);   // ... a re-tag waiting for the station's talkgroups
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
    // an admin saved the folders (their rev in the push): the tree again, once for that rev
    const fr = (cad.state && typeof cad.state.radio_folders === "string") ? cad.state.radio_folders : null;
    if (fr !== null && fr !== (st.layout ? st.layout.rev : "") && fr !== st.layoutAsked && st.src.length) {
      st.layoutAsked = fr;
      sources(true);
    }
    // ... or a channel's service (here or Settings > Radios > Channels): the tree again, once for that rev
    const sr = (cad.state && typeof cad.state.radio_services === "string") ? cad.state.radio_services : null;
    if (sr !== null && st.svcAsked !== undefined && sr !== st.svcAsked && st.src.length) {
      st.svcAsked = sr;
      sources(true);
    } else if (sr !== null && st.svcAsked === undefined) st.svcAsked = sr;
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
    // a window is the county's talkgroup's (sizeup/cad/alarms.py Windows.state: {talkgroup, channel, until, ...})
    for (const w of (cad.state && cad.state.alarms && cad.state.alarms.windows) || []) {
      if (w.until && w.talkgroup !== undefined && w.talkgroup !== null) out[`p25:westchester:${w.talkgroup}`] = w.until;
    }
    return out;
  }
  function sourceRow(s) {
    const row = el("div", "rn-ch" + (st.f.source === s.key ? " on" : "") + (s.mute ? " mute" : ""));
    const b = el("button", "rn-name quiet");
    b.type = "button";
    b.dataset.source = s.key;
    b.title = [`${s.orig || s.name}${idOf(s) ? ` (${idOf(s)})` : ""}`, `${s.system_label || s.system} › ${s.group || "Other"}`, s.tip,
               s.department].filter(Boolean).join(" · ");
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
    if (!fixed(s)) {                                  // Our calls and our fire channels are never muted
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
  // A folder's head: a button that folds it (aria-expanded); folded, what is in it at a glance. solo: a heading with
  // one folder of its own name ("Analog > Analog"): the heading itself is the button
  const folded = (id) => st.p.folds.includes(id);
  function foldHead(g, solo) {
    const shut = folded(g.id);
    const b = el("button", solo ? "rn-fold rn-sys h2 quiet" : "rn-fold rn-grp quiet");
    b.type = "button";
    b.dataset.fold = g.id;
    b.setAttribute("aria-expanded", String(!shut));
    b.title = shut ? "Show all" : "Minimize";
    b.append(el("span", "caret", shut ? "▸" : "▾"));
    if (!solo) {
      const mk = mark(g.service);
      if (!g.service) mk.title = "Mixed";
      b.append(mk);
    }
    b.append(el("span", "trunc", g.name), el("span", "spacer"));
    if (shut) {
      if (g.em) b.append(emChip(g.em === "quiet" ? "other" : g.em, "EM", g.em === "ours" ? "Emergency" : "Emergency (not ours)", g.em === "quiet"));
      if (g.live) b.append(dot({ live: true }));
      const k = el("span", "rn-k", String(g.sources.length));
      k.title = `${g.sources.length} channel${g.sources.length === 1 ? "" : "s"}`;
      b.append(k);
    }
    if (g.n) b.append(count(g.n, g.ours));
    return b;
  }
  function renderTree(m) {
    const tree = [];
    if (m.pinned.length) tree.push(el("div", "rn-sys h2", "Pinned"), ...m.pinned.map(sourceRow));
    for (const sec of m.sections) {
      const solo = sec.groups.length === 1 && sec.groups[0].name.toLowerCase() === String(sec.label).toLowerCase();
      if (sec.label && !solo) tree.push(el("div", "rn-sys h2", sec.label));
      for (const g of sec.groups) {
        tree.push(foldHead(g, solo));
        // folded: only the channel on screen, so what is picked is never out of sight
        tree.push(...g.sources.filter((s) => !folded(g.id) || s.key === st.f.source).map(sourceRow));
      }
    }
    $("#rn-tree").replaceChildren(...tree);
    const ids = m.sections.flatMap((x) => x.groups.map((g) => g.id)), open = ids.some((id) => !folded(id));
    const all = $("#rn-folds");
    setText(all, open ? "⊟" : "⊞");
    all.title = open ? "Minimize all" : "Show all";
    all.setAttribute("aria-label", all.title);
    show(all, ids.length > 0);
  }
  function renderRail() {
    if (!dom || !st.open) return;
    const m = model(st.src, st.p, now(), liveKeys(), windowKeys(), loudServices(), st.layout);
    const here = (v) => v.id === st.f.view && !st.f.source;
    const sig = JSON.stringify([st.f.view, st.f.source, st.p.rail, st.sheet, st.p.folds, st.layout && st.layout.rev, !!st.edit,
                               m.sections.map((x) => [x.label, x.groups.map((g) => [g.id, g.name, g.n, g.live, g.em, g.sources.length])]), m.views,
                               m.rows.map((s) => [s.key, s.name, s.n, s.live, s.recent, s.pin, s.mute, s.em, s.quiet, s.stuck, s.until, s.code])]);
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
    show($("#rn-edit"), !st.edit && mayEdit());
    if (!st.edit) renderTree(m);                      // (Edit draws its own, only when the draft changes: never on a push)
    else show($("#rn-folds"), false);
    const s = st.f.source && m.rows.find((x) => x.key === st.f.source);
    const g = s && m.sections.flatMap((x) => x.groups).find((x) => x.sources.includes(s));
    setText($("#rn-crumb"), s ? `${g && g.section ? `${g.section} › ` : ""}${g ? g.name : s.group || "Other"} › ${s.name}` : st.f.source
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
  // ---- Edit (admins): the folders for the department --------------------------------------------------------

  const mayEdit = () => typeof canDo !== "function" || canDo("admin");
  const esc = (v) => (typeof CSS !== "undefined" && CSS.escape ? CSS.escape(v) : String(v).replace(/["\\]/g, "\\$&"));
  function iconBtn(text, label, data, off = false) {
    const b = el("button", "quiet sm icon", text);
    b.type = "button";
    Object.assign(b.dataset, data);
    b.title = label;
    b.setAttribute("aria-label", label);
    b.disabled = off;
    return b;
  }
  function editStart() {
    if (!mayEdit() || st.edit) return;
    st.edit = { folders: draftOf(st.src, st.layout), names: { ...((st.layout && st.layout.names) || {}) }, base: st.layout ? st.layout.rev : "", error: "", busy: false, focus: "",
                start: st.layout,                      // (what the edit began from: a name set meanwhile is merged at Save)
                services: { ...(st.services || {}) }, svcStart: { ...(st.services || {}) } };   // each channel's service
    st.railSig = "";
    renderRail();
    renderEdit();
  }
  function editStop() {
    st.edit = null;
    st.railSig = "";
    renderRail();
    if ($("#rn-edit").focus) $("#rn-edit").focus();
  }
  function renderEdit() {
    if (!dom || !st.edit) return;
    const e = st.edit, name = new Map(st.src.map((s) => [s.key, s])), d = e.folders;
    const bar = el("div", "rn-edbar");
    const add = el("button", "sm", "+ Folder");
    add.type = "button";
    add.dataset.ed = "add";
    add.title = "New folder";
    const reset = el("button", "sm quiet", "Reset");
    reset.type = "button";
    reset.dataset.ed = "reset";
    reset.title = "The automatic grouping (by system and talkgroup tag); Save to apply";
    const cancel = el("button", "sm quiet", "Cancel");
    cancel.type = "button";
    cancel.dataset.ed = "cancel";
    const save = el("button", "sm primary", e.busy ? "Saving…" : "Save");
    save.type = "button";
    save.dataset.ed = "save";
    save.title = "For everyone (department-wide)";
    save.disabled = e.busy;
    bar.append(add, reset, el("span", "spacer"), cancel, save);
    const out = [bar, el("div", "error", e.error)];
    const heads = [...new Set(d.map((f) => f.section).filter(Boolean))];
    const list = el("datalist");
    list.id = "rn-heads";
    list.append(...heads.map((h) => new Option(h, h)));
    out.push(list);
    d.forEach((f, i) => {
      const box = el("div", "rn-edf");
      box.dataset.f = f.id;
      const head = el("div", "rn-edh");
      const grip = el("span", "rn-grip", "⠿");
      grip.draggable = true;
      grip.dataset.drag = `f:${f.id}`;
      grip.title = "Drag to move the folder";
      const nm = el("input", "rn-edname");
      nm.type = "text";
      nm.value = f.name;
      nm.maxLength = 40;
      nm.dataset.name = f.id;
      nm.setAttribute("aria-label", "Folder name");
      head.append(grip, nm, iconBtn("▲", `Move ${f.name} up`, { ed: "fup", id: f.id }, i === 0),
                  iconBtn("▼", `Move ${f.name} down`, { ed: "fdown", id: f.id }, i === d.length - 1),
                  iconBtn("✕", f.items.length ? "Move its channels out first" : `Delete ${f.name}`, { ed: "fdel", id: f.id }, f.items.length > 0));
      const sec = el("input", "rn-edsec");
      sec.type = "text";
      sec.value = f.section;
      sec.maxLength = 40;
      sec.placeholder = "Heading";
      sec.dataset.sec = f.id;
      sec.setAttribute("list", "rn-heads");
      sec.setAttribute("aria-label", `Heading over ${f.name}`);
      sec.title = "The heading over this folder (folders in a row with the same heading share it; blank: none)";
      box.append(head, sec);
      if (!f.items.length) box.append(el("div", "rn-edempty dim", "Empty"));
      f.items.forEach((k, j) => {
        const s = name.get(k) || { name: k }, shown = e.names[k] || s.name;
        const nm = el("input", "rn-edch");                // its name on the rail (blank: its own)
        nm.type = "text";
        nm.value = e.names[k] || "";
        nm.placeholder = s.name;
        nm.maxLength = 40;
        nm.dataset.chname = k;
        nm.setAttribute("aria-label", `Name of ${s.name}`);
        nm.title = `${s.name}${idOf(s) ? ` (${idOf(s)})` : ""}: the name on the rail (blank: its own)`;
        // its service: Auto (its talkgroup file or name) or the admins' choice
        const auto = s.service_auto || s.service || "";
        const svc = el("select", "rn-edsvc");
        svc.dataset.chsvc = k;
        svc.append(new Option(auto ? `Auto (${SERVICE[auto] || auto})` : "Auto", ""), ...SERVICES.map((v) => new Option(SERVICE[v], v)));
        svc.value = e.services[k] || "";
        svc.setAttribute("aria-label", `Service of ${shown}`);
        svc.title = `Service: its view, filters and intel${auto ? `\nAuto: ${SERVICE[auto] || auto} (its talkgroup file or name)` : ""}`
          + "\nA change re-tags its last 30 days" + (OURS_SYSTEMS.includes(s.system) ? "\nFire here: never muted" : "")
          + (st.retagWaiting && st.retagWaiting.has(k) ? "\nRe-tag waits for the station" : "");
        const row = el("div", "rn-edi");
        row.dataset.k = k;
        const g = el("span", "rn-grip", "⠿");
        g.draggable = true;
        g.dataset.drag = `k:${k}`;
        g.title = "Drag to move";
        const to = el("select", "rn-edto");
        to.dataset.move = k;
        to.setAttribute("aria-label", `Move ${shown} to`);
        to.title = "Move to folder";
        to.append(...d.map((x) => new Option(x.name || "(no name)", x.id)));
        to.value = f.id;
        const first = i === 0 && j === 0, lastOne = i === d.length - 1 && j === f.items.length - 1;
        row.append(g, nm, iconBtn("▲", `Move ${shown} up`, { ed: "iup", id: k }, first),
                   iconBtn("▼", `Move ${shown} down`, { ed: "idown", id: k }, lastOne), to, svc);
        box.append(row);
      });
      out.push(box);
    });
    $("#rn-tree").replaceChildren(...out);
    if (e.focus) {                                    // the button pressed, again after the redraw (keys, touch)
      let n = $("#rn-tree").querySelector(e.focus);
      if ((!n || n.disabled) && e.focusAlt) n = $("#rn-tree").querySelector(e.focusAlt);    // at an end: the row's other control
      if (n && n.focus) n.focus();
      e.focus = e.focusAlt = "";
    }
  }
  function editDo(b) {
    const e = st.edit, d = e.folders, act = b.dataset.ed, id = b.dataset.id;
    if (e.busy) return null;                          // saving: the draft is what is being saved
    if (act === "cancel") return editStop();
    if (act === "save") return editSave();
    e.reset = act === "reset";                        // Reset and nothing after it: Save drops the setting
    if (act === "add") {
      const f = { id: newId(), name: "New folder", section: d.length ? d[0].section : "", items: [] };
      d.unshift(f);
      e.focus = `[data-name="${f.id}"]`;
    } else if (act === "reset") {
      e.folders = draftOf(st.src, null);
      e.names = {};
    } else if (act === "fdel") {
      const i = d.findIndex((f) => f.id === id);
      if (i >= 0 && !d[i].items.length) d.splice(i, 1);
      e.focus = '[data-ed="add"]';
    } else if (act === "fup" || act === "fdown" || act === "iup" || act === "idown") {
      step(d, act[0] === "f" ? "folder" : "item", id, act.endsWith("up") ? -1 : 1);
      e.focus = `[data-ed="${act}"][data-id="${esc(id)}"]`;
      e.focusAlt = act[0] === "f" ? `[data-name="${esc(id)}"]` : `[data-move="${esc(id)}"]`;
    }
    return renderEdit();
  }
  async function editSave() {
    const e = st.edit;
    if (!e || e.busy) return;
    const d = e.folders.map((f) => ({ ...f, name: f.name.trim() || "Folder", section: f.section.trim(), items: f.items.slice() }));
    // the same as the automatic grouping: the setting dropped, not written out
    let auto = sameDraft(d, draftOf(st.src, null));
    // a channel placed before but not in the tree now (not heard lately): kept in its folder, if that is still there
    // (and then it is a layout to keep, not the automatic grouping)
    const have = new Set(d.flatMap((f) => f.items));
    for (const f of (st.layout && st.layout.folders) || []) {
      const t = d.find((x) => x.id === f.id);
      for (const k of t ? f.items : []) if (!have.has(k)) { t.items.push(k); have.add(k); auto = false; }
    }
    auto = auto || !!e.reset;
    // the channels' names (those not in the tree now kept too, as their placings); blank: its own
    const names = {};
    for (const [k, v] of Object.entries(e.names)) if (String(v).trim()) names[k] = String(v).trim();
    if (Object.keys(names).length && !e.reset) auto = false;
    const body = auto ? { action: "reset", base: e.base } : { action: "set", base: e.base, folders: d, names };
    const op = typeof operator === "function" ? operator() : "";
    if (op) body.operator = op;
    // the channels' services changed in this edit (blank: back to automatic), all in one, before the folders
    const svc = {};
    for (const k of new Set([...Object.keys(e.svcStart), ...Object.keys(e.services)])) {
      if ((e.svcStart[k] || "") !== (e.services[k] || "")) svc[k] = e.services[k] || "automatic";
    }
    e.busy = true;
    e.error = "";
    renderEdit();
    try {
      if (Object.keys(svc).length) {
        const r = await api("POST", "/api/cad/settings/radio_services", { action: "set_many", services: svc, ...(op ? { operator: op } : {}) });
        st.services = r.radio_services || {};
        e.svcStart = { ...e.services };              // (saved: a refused folders save doesn't send them again)
      }
      const r = await api("POST", "/api/cad/settings/radio_folders", body);
      if (st.edit !== e) return;                      // (not this edit any more)
      st.layout = r.radio_folders || null;
      st.layoutAsked = st.layout ? st.layout.rev : "";
      st.edit = null;
      st.railSig = "";
      renderRail();
      sources(true);
    } catch (x) {
      e.busy = false;
      // Someone else saved meanwhile. Only names (Settings > Radios > Channels names one at a time) and not the
      // folders: theirs kept, this edit's own name changes over them, saved once more
      if (/changed meanwhile/.test(x.message) && !e.merged && st.edit === e && await mergeNames(e)) return editSave();
      e.error = x.message;
      if (st.edit === e) renderEdit();
    }
  }
  async function mergeNames(e) {
    let fresh;
    try { fresh = (await api("GET", "/api/cad/comms/sources")).folders || null; } catch (_) { return false; }
    if (st.edit !== e || !sameFolders(fresh, e.start)) return false;
    e.names = mergedNames((e.start && e.start.names) || {}, e.names, (fresh && fresh.names) || {});
    e.base = fresh ? fresh.rev : "";
    e.merged = true;
    return true;
  }
  // Drag and drop (desktop): a channel before another, or onto a folder (its end); a folder before another
  function dragTarget(t) {
    const row = t.closest ? t.closest(".rn-edi") : null, box = t.closest ? t.closest(".rn-edf") : null;
    return { row, box };
  }
  function onDrop(ev) {
    const dr = st.drag;
    st.drag = null;
    if (!st.edit || !dr || st.edit.busy) return;
    const { row, box } = dragTarget(ev.target);
    if (!box) return;
    ev.preventDefault();
    const d = st.edit.folders;
    st.edit.reset = false;
    // dropped further down than it was: after the target, else before it
    const after = (list, from, to) => { const i = list.indexOf(from), j = list.indexOf(to); return i >= 0 && i < j ? list[j + 1] || "" : to; };
    if (dr.kind === "k") {
      const f = d.find((x) => x.id === box.dataset.f), on = row ? row.dataset.k : "";
      moveItem(d, dr.id, box.dataset.f, on && f ? after(f.items, dr.id, on) : "");
    } else {
      moveFolder(d, dr.id, after(d.map((x) => x.id), dr.id, box.dataset.f));
    }
    renderEdit();
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
  // the admins' name for its channel (#607) on the row's channel chip: display only (filters, menus and links keep the
  // real one); the real one in the hover
  const shownName = (t) => (st.layout && st.layout.names && t.source && st.layout.names[t.source]) || "";
  function srcChip(n) {
    if (n.classList && n.classList.contains("chip") && n.classList.contains("src")) return n;
    for (const c of n.children || []) { const x = srcChip(c); if (x) return x; }
    return null;
  }
  function fillRow(li, t) {
    const n = RadioView.row(t), nm = shownName(t), chip = nm && srcChip(n);
    if (chip) {
      chip.textContent = nm;
      chip.title = `${t.channel}${t.talkgroup !== undefined && t.talkgroup !== null ? ` (TG ${t.talkgroup})` : ""}`;
    }
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
    syncList($("#rn-list"), st.rows, { key: (t) => t.id, sig: (t) => RadioView.rowSig(t) + shownName(t), make: () => el("li"), fill: fillRow,
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
      if (b.dataset.ed) return st.edit ? editDo(b) : null;
      if (b.dataset.fold) {
        const id = b.dataset.fold;
        toggle(st.p.folds, id);
        renderRail();
        const n = $("#rn-tree").querySelector(`[data-fold="${esc(id)}"]`);   // the redrawn head keeps the focus
        if (n && n.focus) n.focus();
        return null;
      }
      if (b.dataset.view) return set({ view: b.dataset.view, source: "" });
      if (b.dataset.source) return set({ source: st.f.source === b.dataset.source ? "" : b.dataset.source });
      if (b.dataset.pin) { toggle(st.p.pins, b.dataset.pin); return renderRail(); }
      if (b.dataset.mute) { toggle(st.p.mutes, b.dataset.mute); renderRail(); if (st.f.view === "all") page(); }
    });
    $("#rn-views").addEventListener("keydown", onKeys);
    $("#rn-edit").addEventListener("click", editStart);
    $("#rn-folds").addEventListener("click", () => {  // all folded / all shown
      const ids = model(st.src, st.p, now(), new Set(), {}, [], st.layout).sections.flatMap((x) => x.groups.map((g) => g.id));
      st.p.folds = ids.some((id) => !folded(id)) ? ids.slice(-FOLD_MAX) : [];
      save();
      st.railSig = "";
      renderRail();
    });
    // Edit: names and headings as typed (no redraw: the box keeps its caret), Move to, drag and drop
    const tree = $("#rn-tree"), marks = new Set();
    const unmark = () => { for (const n of marks) n.classList.remove("rn-drop"); marks.clear(); };
    tree.addEventListener("input", (e) => {
      const t = e.target;
      if (st.edit && !st.edit.busy && t.dataset.chname) {
        if (t.value.trim()) st.edit.names[t.dataset.chname] = t.value; else delete st.edit.names[t.dataset.chname];
        st.edit.reset = false;
        return;
      }
      const f = st.edit && !st.edit.busy && st.edit.folders.find((x) => x.id === (t.dataset.name || t.dataset.sec));
      if (!f) return;
      if (t.dataset.name) f.name = t.value; else f.section = t.value;
      st.edit.reset = false;
    });
    tree.addEventListener("change", (e) => {
      const t = e.target;
      if (st.edit && !st.edit.busy && t.dataset.chsvc) {     // a channel's service: kept for Save, no redraw
        if (t.value) st.edit.services[t.dataset.chsvc] = t.value; else delete st.edit.services[t.dataset.chsvc];
        return;
      }
      if (!st.edit || st.edit.busy || !t.dataset.move) return;
      moveItem(st.edit.folders, t.dataset.move, t.value);
      st.edit.reset = false;
      st.edit.focus = `[data-move="${esc(t.dataset.move)}"]`;
      renderEdit();
    });
    tree.addEventListener("dragstart", (e) => {
      const t = e.target, v = st.edit && t.dataset && t.dataset.drag;
      if (!v) return;
      st.drag = { kind: v[0], id: v.slice(2) };
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", st.drag.id);
        const n = t.closest(v[0] === "k" ? ".rn-edi" : ".rn-edf");
        if (n && e.dataTransfer.setDragImage) e.dataTransfer.setDragImage(n, 0, 0);
      }
    });
    tree.addEventListener("dragover", (e) => {
      if (!st.edit || !st.drag) return;
      const { row, box } = dragTarget(e.target);
      if (!box) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      const n = st.drag.kind === "k" && row ? row : box;
      if (!marks.has(n)) { unmark(); n.classList.add("rn-drop"); marks.add(n); }
    });
    tree.addEventListener("drop", (e) => { unmark(); onDrop(e); });
    tree.addEventListener("dragend", () => { unmark(); st.drag = null; });
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

  const out = { wants, open, close, prefs, inView, unread, mutesFor, model, merged, folders, draftOf, moveItem, moveFolder, step,
                get on() { return st.open; } };
  // its insides, for tests/test_radio_nav.py only (the harness sets this before the script runs): never on the page
  if (window.RADIO_NAV_TEST === true) out.test = { st, page, delta, older, sources, ours, set, onPush, query, fromHash, hashOf, render,
                                                  editStart, editDo, editSave, renderEdit };
  return out;
})();
