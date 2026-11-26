// The console's live player: live radio that follows you (admin, 2026-11-25: "bring it up to the general window not just
// the radio window so the live play out follows you"). One player for the whole console (Overview, Incident, Radio,
// Search, Settings): the page never reloads between views, so the audio is never restarted by a view change. /monitor
// (the wall board) keeps its own Sound (RadioLive, radio-view.js): a shared screen, not a person's.
//
//   Listen     the header's Listen button: one click turns it on and unlocks sound (the browser's autoplay rule); a
//              reload remembers it was on and waits for one click (Tap for sound) before it plays anything.
//   What       each person's own choices, kept on the hub (/api/me/prefs/listen, sizeup/cad/prefs.py; me-prefs.js):
//              a level per target, the most specific first: a channel or talkgroup (src:<key>), a frequency
//              (mhz:<MHz>), Our calls (ours: our dispatch, paging and firegrounds), a system (sys:), a service (svc:);
//              none: Off. A new person: Our calls Priority, the rest Off. Quick toggles: Our calls only, Mute police.
//   Levels     Priority plays first, is never dropped and, as chosen (mode): interrupts a Normal one (which resumes
//              after, unless it got too old), ducks it (plays over it, the Normal one quieter), or waits for it. Normal
//              ones queue, oldest first, and are dropped once older than max_age (60 s) or when more than
//              MAX_NORMAL wait. An emergency on a channel you listen to plays as Priority. Key-ups (no speech) never play.
//   Feed       the transmissions come from the state push, never a timer of its own: when the push's comms_rev moves,
//              one GET /api/cad/comms?view=all&type=radio&changed=<cursor> (only what changed since; the hub decides
//              what this person may hear, #419), only while it is on and armed. Nothing from before it was armed.
//   Dock       a strip under the console while on: now playing (service, channel, talker, elapsed), the queue,
//              play/pause, skip, mute, volume, the quick toggles, Listening (the choices) and Go to (the Radio view's
//              row). Keys: M mute, ] skip. Elapsed is drawn from the audio's own timeupdate (no ticking timer), and a
//              state push redraws nothing unless what it shows changed.
//   A clip played by hand (any row's ▶) holds the player until it ends or is paused. Signed out (#818): the clip
//   waits and plays once signed in again (as RadioLive did). Not allowed sound (autoplay): back in the queue, and the
//   header says Tap for sound.
//
// ListenCore is the pure part (levels and the queue), tested in node (tests/test_live_player.py).
"use strict";

window.ListenCore = (() => {
  const LEVELS = ["priority", "normal", "off"];
  const MODES = ["interrupt", "duck", "queue"];
  const SERVICES = ["fire", "police", "ems", "transit", "air", "other"];
  const DEFAULTS = Object.freeze({ rules: Object.freeze({ ours: "priority" }), mode: "interrupt", max_age: 60, ours_only: false,
                                   mute_police: false });
  const MAX_NORMAL = 15;              // Normal ones waiting: more, and the oldest goes
  const MAX_PRIORITY = 100;           // (never dropped for age; this only bounds a runaway)
  const AGE = [10, 600];

  // The choices as stored, every field there (a missing or bad one: the default)
  function norm(v) {
    const o = v && typeof v === "object" && !Array.isArray(v) ? v : {};
    const rules = {};
    const src = o.rules && typeof o.rules === "object" && !Array.isArray(o.rules) ? o.rules : DEFAULTS.rules;
    for (const [k, lvl] of Object.entries(src)) if (typeof k === "string" && LEVELS.includes(lvl)) rules[k] = lvl;
    const age = Number.isInteger(o.max_age) && o.max_age >= AGE[0] && o.max_age <= AGE[1] ? o.max_age : DEFAULTS.max_age;
    return { rules, mode: MODES.includes(o.mode) ? o.mode : DEFAULTS.mode, max_age: age,
             ours_only: o.ours_only === true, mute_police: o.mute_police === true };
  }
  // A row's source key (the hub's; an older row without one: from its channel / talkgroup, as sizeup/sourcekeys.py)
  function sourceOf(t) {
    if (t.source) return String(t.source);
    if (t.talkgroup !== undefined && t.talkgroup !== null) return `p25:${t.system || "westchester"}:${t.talkgroup}`;
    return (t.service === "air" || t.system === "airband" ? "air:" : "fm:") + String(t.channel || "");   // a key, not a label
  }
  const isOurs = (t) => !!(t.ours || t.main);
  const mhzKey = (t) => (+t.mhz > 0 ? `mhz:${+t.mhz}` : "");
  const sysOf = (t) => String(t.system || (sourceOf(t).startsWith("air:") ? "airband" : sourceOf(t).startsWith("fm:") ? "analog" : "westchester"));
  // The rule that decides a row, most specific first: [target, level] (no rule: ["", "off"])
  function rule(t, p) {
    const r = p.rules;
    for (const k of [`src:${sourceOf(t)}`, mhzKey(t), isOurs(t) ? "ours" : "", `sys:${sysOf(t)}`, t.service ? `svc:${t.service}` : ""]) {
      if (k && r[k]) return [k, r[k]];
    }
    return ["", "off"];
  }
  // priority | normal | off for a row, the quick toggles and an emergency included
  function level(t, p) {
    if (p.ours_only && !isOurs(t)) return "off";
    if (p.mute_police && t.service === "police") return "off";
    const lvl = rule(t, p)[1];
    if (lvl === "normal" && (t.emergency || t.alarm_level)) return "priority";
    return lvl;
  }
  // A channel's own level (src:) and what it gets without one (a source from GET /api/cad/comms/sources)
  function sourceLevel(s, p) {
    const row = { source: s.key, system: s.system, service: s.service, ours: s.ours, mhz: s.mhz };
    const own = p.rules[`src:${s.key}`] || "";
    const without = { ...p, rules: { ...p.rules } };
    delete without.rules[`src:${s.key}`];
    return { own, inherited: rule(row, without)[1], effective: level(row, p) };
  }
  // A new rules object with target set to level ("" removes it)
  function setRule(p, target, lvl) {
    const rules = { ...p.rules };
    if (lvl) rules[target] = lvl; else delete rules[target];
    return { ...p, rules };
  }

  // ---- the queue (q: make()): pure, the clock given ----------------------------------------------------------------
  function make() {
    return { pq: [], nq: [], playing: null, paused: null, dropped: 0, seen: new Set() };
  }
  const byStart = (a, b) => a.start - b.start;
  const stale = (it, now, p) => it.lvl !== "priority" && now - it.start > p.max_age;
  // A new transmission: queued by its level. What the player should do now: play (nothing playing), interrupt (a
  // Normal one is playing: pause it for this), duck (play this over it), queue (it waits) or none (not played).
  function add(q, it, now, p) {
    if (q.seen.has(it.id)) return "none";
    q.seen.add(it.id);
    if (q.seen.size > 4000) q.seen = new Set([...q.seen].slice(-2000));
    if (!it.lvl || it.lvl === "off") return "none";
    if (stale(it, now, p)) { q.dropped++; return "none"; }
    const into = it.lvl === "priority" ? q.pq : q.nq;
    into.push(it);
    into.sort(byStart);
    while (q.nq.length > MAX_NORMAL) { q.nq.shift(); q.dropped++; }
    while (q.pq.length > MAX_PRIORITY) q.pq.shift();
    if (!q.playing) return "play";
    if (it.lvl === "priority" && q.playing.lvl !== "priority") return p.mode === "queue" ? "queue" : p.mode;
    return "queue";
  }
  // The next to play: Priority first, then a Normal one that was interrupted (unless too old now), then the oldest
  // Normal one that isn't too old (the too old ones dropped on the way)
  function next(q, now, p) {
    if (q.pq.length) return q.pq.shift();
    if (q.paused) {
      const it = q.paused;
      q.paused = null;
      if (!stale(it, now, p)) return it;
      q.dropped++;
    }
    while (q.nq.length) {
      const it = q.nq.shift();
      if (!stale(it, now, p)) return it;
      q.dropped++;
    }
    return null;
  }
  // The level changed (a choice, a quick toggle): what waits is judged again; off now: out of the queue
  function rejudge(q, p, levelOf) {
    const all = [...q.pq, ...q.nq];
    q.pq = [];
    q.nq = [];
    for (const it of all) {
      it.lvl = levelOf(it.row);
      if (it.lvl === "priority") q.pq.push(it); else if (it.lvl === "normal") q.nq.push(it);
    }
    q.pq.sort(byStart);
    q.nq.sort(byStart);
    if (q.paused) {
      q.paused.lvl = levelOf(q.paused.row);
      if (q.paused.lvl === "off") q.paused = null;
    }
  }
  const waiting = (q) => ({ priority: q.pq.length, normal: q.nq.length + (q.paused ? 1 : 0) });
  return { LEVELS, MODES, SERVICES, DEFAULTS, MAX_NORMAL, AGE, norm, sourceOf, rule, level, sourceLevel, setRule,
           make, add, next, rejudge, waiting };
})();

window.LivePlayer = (() => {
  if (typeof document === "undefined" || !document.getElementById("lp")) return null;
  if (typeof BOARD !== "undefined" && BOARD) return null;          // the wall board: its own Sound
  const C = ListenCore;
  const NS = "listen";
  const LKEY = "sizeup.cad.listen";               // this browser's copy of the choices (me-prefs.js syncs it)
  const DKEY = "sizeup.cad.listen.device";        // this device's own: on, volume, muted (never on the hub)
  const DUCK = 0.25;                              // a Normal one under a Priority one (duck): this share of the volume
  const NEW_SLACK_S = 10;                         // a row this much older than when it was armed still counts as new
  const SILENT = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
  const LVL_TEXT = { priority: "Priority", normal: "Normal", off: "Off" };
  const LVL_MARK = { priority: "P", normal: "N", off: "–" };
  const SVC = { fire: "Fire", police: "Police", ems: "EMS", transit: "Transit", air: "Air", other: "Other" };
  const MARK = { fire: "F", police: "P", ems: "E", transit: "T", air: "A" };

  const read = (k) => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (_) { return null; } };
  const write = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* this page only */ } };
  const local = read(LKEY);
  const dev = read(DKEY) || {};
  const st = {
    p: C.norm(local), on: dev.on === true, armed: false, blocked: false, held: false, manual: false, paused: false,
    volume: Number.isFinite(dev.volume) ? Math.min(1, Math.max(0, dev.volume)) : 1, muted: dev.muted === true,
    q: C.make(), cur: null, over: null,              // cur: on the main element; over: a Priority one ducking it
    since: 0, cursor: null, rev: "", busy: false, again: false, gen: 0, sig: "", elapsed: "", sources: null, srcAt: 0, arming: false,
    panel: false,
  };
  const a = new Audio(), b = new Audio();          // a: the queue; b: a Priority one over a ducked Normal one
  const $id = (id) => document.getElementById(id);
  const now = () => (typeof serverNow === "function" ? serverNow() : Date.now() / 1000);

  function saveDevice() { write(DKEY, { on: st.on, volume: st.volume, muted: st.muted }); }
  function choicesChanged() {
    write(LKEY, st.p);
    if (window.MePrefs) MePrefs.changed(NS);
    C.rejudge(st.q, st.p, (row) => C.level(row, st.p));
    if (st.cur && C.level(st.cur.row, st.p) === "off") skip();     // what plays now was just turned off
    paint(true);
    if (st.panel) renderPanel();
    window.dispatchEvent(new CustomEvent("sizeup:listen-changed"));
  }
  if (window.MePrefs) {
    MePrefs.register(NS, {
      get: () => st.p,
      apply: (v) => { st.p = C.norm(v); write(LKEY, st.p); C.rejudge(st.q, st.p, (row) => C.level(row, st.p)); paint(true);
                       if (st.panel) renderPanel(); window.dispatchEvent(new CustomEvent("sizeup:listen-changed")); },
      hadLocal: !!local,
    });
  }

  // ---- playing -----------------------------------------------------------------------------------------------------
  function levels() {
    a.muted = b.muted = st.muted;
    b.volume = st.volume;
    a.volume = st.over ? st.volume * DUCK : st.volume;
  }
  function url(t) {
    const u = `/api/audio/${encodeURIComponent(t.id)}.wav`;
    return t.voice_at > 0 ? `${u}#t=${Number(t.voice_at).toFixed(2)}` : u;      // a page: from where the voice starts
  }
  const loaded = (el) => !!el.getAttribute("src") && !el.src.startsWith("data:");      // a clip (not the unlock, not none)
  const free = () => st.on && st.armed && !st.held && !st.manual && !st.paused;
  // Not allowed sound (a browser's autoplay rule, Safari after a long idle): what plays goes back in the queue where it
  // was (a Priority one first, a Normal one to resume), and it waits for the click (Tap for sound)
  function requeue() {
    if (st.over) { const it = st.over; it.at = b.currentTime || 0; b.pause(); st.over = null; st.q.pq.unshift(it); }
    if (st.cur) {
      const it = st.cur;
      it.at = a.currentTime || 0;
      it.stopped = false;
      a.pause();
      st.cur = null;
      st.q.playing = null;
      if (it.lvl === "priority") st.q.pq.unshift(it);
      else {
        if (st.q.paused) { st.q.nq.push(st.q.paused); st.q.nq.sort((x, y) => x.start - y.start); }
        st.q.paused = it;
      }
    }
    levels();
  }
  function block() {
    st.blocked = true;
    st.armed = false;
    requeue();
    paint();
  }
  // every play: a refusal that isn't the clip replaced (AbortError) or not loading (its error event) is the browser's
  function play(el) {
    const p = el.play();
    if (p && p.then) p.then(() => { if (st.blocked) { st.blocked = false; paint(); } }).catch((e) => {
      if (e && (e.name === "AbortError" || e.name === "NotSupportedError")) return;
      block();
    });
  }
  function start(el, it, at = 0) {
    el.src = url(it.row);
    if (at > 0) el.addEventListener("loadedmetadata", () => { try { el.currentTime = at; } catch (_) { /* from the start */ } }, { once: true });
    it.at = 0;
    levels();
    play(el);
  }
  function pump() {
    if (!free() || st.cur || st.over) return paint();
    const it = C.next(st.q, now(), st.p);
    if (!it) { st.q.playing = null; return paint(); }
    st.cur = it;
    st.q.playing = it;
    start(a, it, it.at || 0);
    return paint();
  }
  // A Priority one waiting while a Normal one plays: over it on the second element, the Normal one paused where it is
  // (interrupt) or on, quieter (duck); queue: it waits its turn
  function priorityOver() {
    if (!free() || !st.cur || st.cur.lvl === "priority" || st.over || !st.q.pq.length || st.p.mode === "queue") return false;
    if (st.p.mode === "interrupt") { a.pause(); st.cur.stopped = true; }
    st.over = st.q.pq.shift();
    start(b, st.over);
    paint();
    return true;
  }
  function ended(el) {
    if (!loaded(el)) return;
    if (el === b) return overDone();
    st.cur = null;
    st.q.playing = null;
    if (st.over) return paint();                  // the Priority one over it finishes first
    return pump();
  }
  // The Priority one over a Normal one is done (or skipped): the next Priority one over it, else the Normal one again
  // (an interrupted one resumes where it stopped, unless too old). Held, paused or signed out: nothing starts
  function overDone() {
    st.over = null;
    levels();
    if (!free()) return paint();
    if (st.cur && st.q.pq.length) { st.over = st.q.pq.shift(); start(b, st.over); return paint(); }
    if (!st.cur) return pump();
    if (st.cur.stopped) {
      st.cur.stopped = false;
      if (st.cur.lvl !== "priority" && now() - st.cur.start > st.p.max_age) {      // too old now: dropped
        a.pause();
        st.q.dropped++;
        st.cur = null;
        st.q.playing = null;
        return pump();
      }
      play(a);
    }
    return paint();
  }
  // After a hold, a pause or a sign-in: what was playing goes on (a Priority one that came meanwhile first, as chosen)
  function resume() {
    if (!free()) return paint();
    if (st.over) { play(b); if (st.cur && !st.cur.stopped) play(a); return paint(); }
    if (st.cur && st.cur.stopped) return overDone();
    if (st.cur) { if (!priorityOver()) play(a); return paint(); }
    return pump();
  }
  a.addEventListener("ended", () => ended(a));
  b.addEventListener("ended", () => ended(b));
  // A clip that won't load: the next one, unless the sign-in ended (#818): then it waits, first, until signed in again
  function failed(el) {
    if (!loaded(el)) return;
    const it = el === a ? st.cur : st.over, gen = st.gen;
    if (!it) return;
    if (!window.SessionWatch) { ended(el); return; }
    if (el === a) { st.cur = null; st.q.playing = null; } else st.over = null;
    st.held = true;
    paint();
    SessionWatch.check()
      .then((s) => {
        if (s !== "out") return null;
        if (st.gen === gen && st.on) st.q.pq.unshift(it.lvl === "priority" ? it : { ...it, lvl: "priority" });
        return SessionWatch.whenIn();
      })
      .catch(() => null)
      .then(() => { st.held = false; levels(); if (el === b) overDone(); else resume(); });
  }
  a.addEventListener("error", () => failed(a));
  b.addEventListener("error", () => failed(b));
  // elapsed: from the audio's own clock, drawn only when the second changes (no timer of our own)
  function tick() {
    const el = st.over ? b : a, it = st.over || st.cur;
    const t = it ? fmtS(el.currentTime - (it.row.voice_at > 0 ? +it.row.voice_at : 0)) : "";
    if (t === st.elapsed) return;
    st.elapsed = t;
    const n = $id("lp-elapsed");
    if (n) n.textContent = t;
  }
  a.addEventListener("timeupdate", tick);
  b.addEventListener("timeupdate", tick);
  const fmtS = (s) => { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

  // A clip played by hand (cad.js's player, any row's ▶): the live one holds until it ends or is paused
  function manualHold(on) {
    if (on === st.manual) return;
    st.manual = on;
    if (on) {                                     // both paused where they are
      if (st.cur) a.pause();
      if (st.over) b.pause();
      paint();
      return;
    }
    resume();
  }
  if (typeof player !== "undefined" && player && player.addEventListener) {
    player.addEventListener("play", () => manualHold(true));
    for (const ev of ["pause", "ended", "error", "emptied"]) player.addEventListener(ev, () => manualHold(false));
  }

  // ---- what arrives ------------------------------------------------------------------------------------------------
  function offer(rows) {
    const t0 = now();
    let act = "none";
    for (const t of [...rows].sort((x, y) => x.start - y.start)) {
      if (t.kind === "note" || !t.id) continue;
      if (t.start < st.since - NEW_SLACK_S) { st.q.seen.add(t.id); continue; }    // from before it was armed: never
      if (t.status === "no_speech") { st.q.seen.add(t.id); continue; }            // a key-up
      if (t.has_audio === false) continue;                                       // its audio isn't here yet: next time
      const r = C.add(st.q, { id: t.id, start: t.start, lvl: C.level(t, st.p), row: t }, t0, st.p);
      if (r === "interrupt" || r === "duck" || (r === "play" && act === "none")) act = r;
    }
    if (act === "interrupt" || act === "duck") priorityOver();
    pump();
  }
  async function get(path) {
    const r = await fetch(path, { credentials: "same-origin", cache: "no-store", redirect: "manual" });
    if (!r.ok) throw new Error(String(r.status || r.type));
    return r.json();
  }
  // From now on: the cursor of the newest page; what is on it is not new
  async function prime() {
    const gen = st.gen;
    st.since = now();
    st.cursor = null;
    try {
      const d = await get("/api/cad/comms?view=all&type=radio&limit=20");
      if (gen !== st.gen) return;
      for (const t of d.rows || []) st.q.seen.add(t.id);
      st.cursor = d.cursor || 0;
      st.rev = (typeof cad !== "undefined" && cad.state && cad.state.comms_rev) || "";
    } catch (_) { /* the next push asks again */ }
  }
  async function pull() {
    if (!st.on || !st.armed) return;
    if (st.busy) { st.again = true; return; }
    st.busy = true;
    const gen = st.gen;
    try {
      if (st.cursor === null) await prime();
      else {
        const d = await get(`/api/cad/comms?view=all&type=radio&limit=200&changed=${encodeURIComponent(st.cursor)}`);
        if (gen === st.gen) {
          if (d.reload) {                          // too much changed, or a new hub: the newest page, only what is new
            const f = await get("/api/cad/comms?view=all&type=radio&limit=50");
            if (gen === st.gen) { st.cursor = f.cursor || 0; offer(f.rows || []); }
          } else {
            st.cursor = d.cursor || st.cursor;
            offer(d.rows || []);
          }
        }
      }
    } catch (_) { /* the next push */ }
    st.busy = false;
    if (st.again) { st.again = false; pull(); }
  }
  function onPush() {
    if (!st.on || !st.armed) return;
    const rev = (cad.state && cad.state.comms_rev) || "";
    if (rev && rev !== st.rev) { st.rev = rev; pull(); }
  }

  // ---- on, off, the controls ---------------------------------------------------------------------------------------
  // This click allows sound: both elements play the unlock in the click itself (Safari allows each element on its own);
  // armed only once it did. What was playing (a reload, a refusal) waits in the queue meanwhile
  function arm() {
    if (st.arming) return;
    requeue();
    st.arming = true;
    st.blocked = false;
    a.src = SILENT;
    b.src = SILENT;
    Promise.all([a.play(), b.play()]).then(() => {
      st.arming = false;
      if (!st.on) return;
      st.armed = true;
      if (st.cursor === null) { st.gen++; prime().then(pump); } else resume();
    }).catch(() => { st.arming = false; st.blocked = true; st.armed = false; paint(); });
    paint();
  }
  function setOn(on) {
    st.on = on;
    saveDevice();
    if (on) return arm();
    st.armed = false;
    st.gen++;
    for (const el of [a, b]) { el.pause(); el.removeAttribute("src"); el.load(); }     // (load: let go of the clip)
    st.q = C.make();
    st.cur = st.over = null;
    st.cursor = null;                              // on again: from then, never what was heard while it was off
    st.paused = false;
    closePanel();
    paint(true);
    return null;
  }
  function toggleOn() {
    if (st.on && (st.blocked || !st.armed)) return arm();         // on but not allowed sound yet: this click allows it
    return setOn(!st.on);
  }
  function skip() {
    if (st.over) { b.pause(); return overDone(); }
    if (st.cur) { a.pause(); st.cur = null; st.q.playing = null; }
    return pump();
  }
  function playPause() {
    if (!st.on) return setOn(true);
    if (!st.armed) return arm();
    st.paused = !st.paused;
    if (!st.paused) return resume();
    if (st.cur) a.pause();
    if (st.over) b.pause();
    return paint();
  }
  function mute(on = !st.muted) {
    st.muted = on;
    levels();
    saveDevice();
    paint();
  }

  // ---- drawing (only what changed) ---------------------------------------------------------------------------------
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function btn(cls, text, label, data) {
    const n = el("button", cls, text);
    n.type = "button";
    if (label) { n.title = label; n.setAttribute("aria-label", label); }
    Object.assign(n.dataset, data || {});
    return n;
  }
  function mark(service) {
    const m = el("span", `rn-svc rn-${service || "any"}`, MARK[service] || "·");
    m.title = SVC[service] || "Other";
    return m;
  }
  const talker = (t) => t.rig || t.talker || t.radio_id || "";
  // the channel's name: the one the admins set (RadioNames, when the page has it), else the row's own
  const chName = (t) => (window.RadioNames && RadioNames.short(t)) || t.channel || C.sourceOf(t);
  const chTitle = (t) => (window.RadioNames && RadioNames.title(t)) || t.channel || "";
  function paint(force = false) {
    const dock = $id("lp");
    if (!dock) return;
    const it = st.over || st.cur;
    const w = C.waiting(st.q);
    if (st.over && st.cur) w.normal++;             // the Normal one under a Priority one waits too
    const sig = JSON.stringify([st.on, st.armed, st.blocked, st.held, st.manual, st.paused, st.muted, st.volume, it && it.id,
                                it && it.lvl, w, st.q.dropped, st.p.ours_only, st.p.mute_police, st.panel]);
    if (!force && sig === st.sig) return;
    st.sig = sig;
    for (const head of [$id("lp-on"), $id("rv-live")]) {          // the header's, and the Radio view's
      if (!head) continue;
      head.classList.toggle("active", st.on && st.armed);
      head.classList.toggle("blocked", st.on && !st.armed);
      head.setAttribute("aria-pressed", String(st.on));
      head.title = !st.on ? "Live audio: what you listen to (Listening), on every view"
        : !st.armed ? "Tap for sound (the browser needs a click)" : "Live audio on: click to stop";
      const lbl = head.querySelector(".lbl");      // the header's: an icon, words only while it needs the click
      if (lbl) { lbl.textContent = st.on && !st.armed ? "Tap for sound" : ""; lbl.hidden = !lbl.textContent; }
      else head.textContent = st.on && !st.armed ? "Tap for sound" : "Listen";
    }
    if (dock.hidden === st.on) dock.hidden = !st.on;
    if (!st.on) { closePanel(); return; }
    // now playing
    const np = $id("lp-now");
    if (it) {
      const t = it.row, who = talker(t);
      np.replaceChildren(mark(t.service), el("span", "lp-ch trunc", chName(t)),
                         ...(who ? [el("span", "lp-who trunc", who)] : []),
                         el("span", "lp-t", st.elapsed || "0:00"));
      np.lastChild.id = "lp-elapsed";
      if (it.lvl === "priority") np.prepend(el("span", "badge lp-p", "P"));
      np.title = `${chTitle(t)}${who ? ` · ${who}` : ""} · ${typeof fmtTime === "function" ? fmtTime(t.start, true) : ""}`
        + (it.lvl === "priority" ? " · Priority" : "")
        + (st.over && st.cur ? (st.cur.stopped ? " · a Normal one paused under it" : " · over a Normal one, quieter") : "");
    } else {
      st.elapsed = "";
      np.replaceChildren(el("span", "dim", !st.armed ? "Tap for sound" : st.held ? "Signed out: waiting" : st.manual
        ? "Held: a clip is playing" : st.paused ? "Paused" : "Listening"));
      np.title = "";
    }
    const qn = $id("lp-queue");
    const n = w.priority + w.normal;
    qn.textContent = n ? `+${n}` : "";
    qn.hidden = !n;
    qn.title = `${w.priority} Priority, ${w.normal} Normal waiting` + (st.q.dropped ? ` · ${st.q.dropped} dropped (too old)` : "");
    const go = $id("lp-go");
    go.hidden = !it;
    if (it) go.href = `#radio?source=${encodeURIComponent(C.sourceOf(it.row))}`;
    const pp = $id("lp-pp"), resume = st.paused || !st.armed;
    if (pp.dataset.face !== String(resume)) {
      pp.dataset.face = String(resume);
      pp.replaceChildren(resume ? Icon("play") : el("span", "lp-pause"));
    }
    pp.setAttribute("aria-label", resume ? "Play" : "Pause");
    pp.title = resume ? "Play" : "Pause";
    $id("lp-skip").disabled = !it;
    const m = $id("lp-mute");
    m.setAttribute("aria-pressed", String(st.muted));
    m.textContent = st.muted ? "🔇" : "🔊";
    m.title = st.muted ? "Unmute (M)" : "Mute (M)";
    m.setAttribute("aria-label", st.muted ? "Unmute" : "Mute");
    const v = $id("lp-vol");
    if (+v.value !== Math.round(st.volume * 100)) v.value = String(Math.round(st.volume * 100));
    for (const [id, on] of [["lp-ours", st.p.ours_only], ["lp-police", st.p.mute_police]]) {
      const q = $id(id);
      q.setAttribute("aria-pressed", String(on));
    }
    $id("lp-set").setAttribute("aria-expanded", String(st.panel));
  }

  // ---- Listening: the choices ----------------------------------------------------------------------------------------
  async function loadSources(force) {
    if (!force && st.sources && Date.now() - st.srcAt < 60000) return;
    try {
      const d = await get("/api/cad/comms/sources");
      st.sources = d.sources || [];
      st.srcAt = Date.now();
    } catch (_) { st.sources = st.sources || []; }
  }
  function lvlSelect(target, cur, label, inherit = true) {
    const s = el("select", "lp-lvl");
    s.dataset.target = target;
    s.setAttribute("aria-label", label);
    if (inherit) s.append(new Option("—", ""));
    for (const l of C.LEVELS) s.append(new Option(LVL_TEXT[l], l));
    s.value = cur || "";
    s.title = inherit ? "— : as a broader rule says" : "";
    return s;
  }
  function row(label, target, extra) {
    const r = el("div", "lp-rule");
    const name = el("span", "trunc", label);
    name.title = label;
    r.append(name, el("span", "spacer"), lvlSelect(target, st.p.rules[target] || "", `${label}: level`));
    if (extra) r.append(extra);
    return r;
  }
  // a control's selector, to find it again after a redraw
  function focusKey(n) {
    for (const k of ["target", "mode", "drop", "act"]) {
      if (n.dataset && n.dataset[k] !== undefined) return `[data-${k}="${CSS.escape(n.dataset[k])}"]`;
    }
    return n.id ? `#${CSS.escape(n.id)}` : "";
  }
  function renderPanel() {
    const box = $id("lp-panel");
    if (!box) return;
    const p = st.p, out = [];
    const head = el("div", "lp-phead");
    const h = el("h2", "", "Listening");
    h.id = "lp-panel-title";
    const x = btn("icon quiet sm", "✕", "Close", { act: "close" });
    head.append(h, el("span", "spacer"), x);
    out.push(head);
    // how a Priority one meets a Normal one; how old a Normal one may be
    const how = el("div", "lp-how");
    const seg = el("span", "seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Priority over Normal");
    const tips = { interrupt: "Priority stops a Normal one; it resumes after", duck: "Priority plays over a Normal one, quieter",
                   queue: "Priority waits for a Normal one to end" };
    for (const m of C.MODES) {
      const o = btn("sm", m[0].toUpperCase() + m.slice(1), "", { mode: m });
      o.title = tips[m];
      o.setAttribute("aria-pressed", String(p.mode === m));
      seg.append(o);
    }
    const age = el("label", "inline lp-age");
    age.title = "A Normal one older than this is dropped (Priority: never)";
    const ai = el("input");
    ai.type = "number";
    ai.min = String(C.AGE[0]);
    ai.max = String(C.AGE[1]);
    ai.step = "5";
    ai.value = String(p.max_age);
    ai.id = "lp-maxage";
    age.append(el("span", "dim", "Max age"), ai, el("span", "dim", "s"));
    how.append(seg, age);
    out.push(how);
    out.push(el("h3", "lp-sub", "Groups"));
    out.push(row("Our calls", "ours"));
    for (const s of C.SERVICES) out.push(row(SVC[s], `svc:${s}`));
    const srcs = st.sources || [];
    const systems = new Map();
    for (const s of srcs) if (!systems.has(s.system)) systems.set(s.system, s.system_label || s.system);
    for (const k of Object.keys(p.rules)) if (k.startsWith("sys:") && !systems.has(k.slice(4))) systems.set(k.slice(4), k.slice(4));
    if (systems.size) {
      out.push(el("h3", "lp-sub", "Systems"));
      for (const [k, label] of systems) out.push(row(label, `sys:${k}`));
    }
    // each channel / talkgroup / frequency with its own level
    out.push(el("h3", "lp-sub", "Channels"));
    const by = new Map(srcs.map((s) => [s.key, s]));
    const own = Object.keys(p.rules).filter((k) => k.startsWith("src:") || k.startsWith("mhz:")).sort();
    if (!own.length) out.push(el("div", "dim small", "None"));
    for (const k of own) {
      const s = k.startsWith("src:") && by.get(k.slice(4));
      const label = s ? `${s.name}${s.talkgroup !== undefined && s.talkgroup !== null ? ` (TG ${s.talkgroup})` : s.mhz ? ` (${s.mhz})` : ""}`
        : k.startsWith("mhz:") ? `${k.slice(4)} MHz` : k.slice(4);
      out.push(row(label, k, btn("icon quiet sm", "✕", `Remove ${label}`, { drop: k })));
    }
    const add = el("div", "lp-add");
    const pick = el("select", "lp-pick");
    pick.id = "lp-pick";
    pick.setAttribute("aria-label", "Add a channel or talkgroup");
    pick.append(new Option("+ Channel / talkgroup", ""));
    const groups = new Map();
    for (const s of srcs) {
      if (p.rules[`src:${s.key}`]) continue;
      const g = s.system_label || s.system || "";
      if (!groups.has(g)) { const og = document.createElement("optgroup"); og.label = g; groups.set(g, og); }
      groups.get(g).append(new Option(`${s.name}${s.talkgroup !== undefined && s.talkgroup !== null ? ` (TG ${s.talkgroup})` : ""}`, s.key));
    }
    pick.append(...groups.values());
    const mhz = el("input", "lp-mhz");
    mhz.id = "lp-mhz";
    mhz.type = "text";
    mhz.inputMode = "decimal";
    mhz.placeholder = "MHz";
    mhz.maxLength = 9;
    mhz.setAttribute("aria-label", "Add a frequency (MHz)");
    mhz.title = "A frequency (airband / FM), e.g. 118.6";
    add.append(pick, mhz, btn("sm", "Add", "Add the frequency", { act: "mhz" }));
    out.push(add);
    const foot = el("div", "lp-pfoot");
    foot.append(btn("sm quiet", "Reset", "Our calls Priority, the rest Off", { act: "reset" }), el("span", "spacer"),
                el("span", "dim small", window.MePrefs && MePrefs._st && MePrefs._st.mode === "hub" ? "Yours, on every device" : "This browser"));
    out.push(foot);
    // drawn again on each change: the focus stays on the same control (a keyboard user is never dropped to the page)
    const at = document.activeElement, keep = at && box.contains(at) ? focusKey(at) : "";
    box.replaceChildren(...out);
    if (keep) {
      const n = box.querySelector(keep) || box.querySelector('[data-act="close"]');
      if (n && n.focus) n.focus();
    }
  }
  function openPanel() {
    st.panel = true;
    const box = $id("lp-panel");
    box.hidden = false;
    renderPanel();
    paint(true);
    loadSources().then(() => { if (st.panel) renderPanel(); });
    const f = box.querySelector("select, button");
    if (f && f.focus) f.focus();
  }
  function closePanel(refocus = false) {
    if (!st.panel && $id("lp-panel") && $id("lp-panel").hidden) return;
    st.panel = false;
    const box = $id("lp-panel");
    if (box) box.hidden = true;
    if (refocus && $id("lp-set")) $id("lp-set").focus();
  }

  // ---- the Radio rail's per-channel control (radio-nav.js asks for it) -----------------------------------------------
  // A source's level: its own (src:) or, dimmed, what it gets from a broader rule; a menu to set it
  function sourceControl(s) {
    const lv = C.sourceLevel(s, st.p);
    const face = el("span", `lp-lv lp-${lv.effective}${lv.own ? " own" : ""}`, LVL_MARK[lv.effective]);
    const label = `Listen ${LVL_TEXT[lv.effective]}${lv.own ? "" : " (default)"}`;
    const items = () => {
      const cur = C.sourceLevel(s, st.p);
      return [...C.LEVELS.map((l) => ({ label: LVL_TEXT[l], checked: cur.own === l, run: () => setSource(s.key, l) })), "-",
              { label: "Default", checked: !cur.own, hint: LVL_TEXT[cur.inherited], run: () => setSource(s.key, "") }];
    };
    const b = UI.menu.button(`${s.name}: ${label.toLowerCase()}`, items, { cls: "rn-act lp-src", face });
    b.title = label;
    b.dataset.listen = s.key;
    return b;
  }
  function setSource(key, lvl) {
    st.p = C.setRule(st.p, `src:${key}`, lvl);
    choicesChanged();
  }

  // ---- wiring --------------------------------------------------------------------------------------------------------
  // each control of its own (cad.js's $)
  $("#lp-on").addEventListener("click", toggleOn);
  $("#rv-live").addEventListener("click", toggleOn);
  $("#lp-pp").addEventListener("click", playPause);
  $("#lp-skip").addEventListener("click", skip);
  $("#lp-mute").addEventListener("click", () => mute());
  $("#lp-ours").addEventListener("click", () => { st.p = { ...st.p, ours_only: !st.p.ours_only }; choicesChanged(); });
  $("#lp-police").addEventListener("click", () => { st.p = { ...st.p, mute_police: !st.p.mute_police }; choicesChanged(); });
  $("#lp-set").addEventListener("click", () => { if (st.panel) closePanel(); else openPanel(); });
  $("#lp-off").addEventListener("click", () => setOn(false));
  $("#lp-go").addEventListener("click", goToRow);
  // the Listening panel's controls (drawn again on a change): one handler for them all
  $id("lp-panel").addEventListener("click", (e) => {
    const t = e.target.closest("button, a");
    if (!t) return;
    if (t.dataset.mode) { st.p = { ...st.p, mode: t.dataset.mode }; choicesChanged(); }
    else if (t.dataset.drop) { st.p = C.setRule(st.p, t.dataset.drop, ""); choicesChanged(); }
    else if (t.dataset.act === "close") closePanel(true);
    else if (t.dataset.act === "reset") { st.p = C.norm(null); choicesChanged(); }
    else if (t.dataset.act === "mhz") {
      const v = $id("lp-mhz").value.trim();
      if (!/^\d{2,4}(\.\d{1,4})?$/.test(v)) { $id("lp-mhz").setCustomValidity("MHz, e.g. 118.6"); $id("lp-mhz").reportValidity(); return; }
      st.p = C.setRule(st.p, `mhz:${+v}`, "normal");
      choicesChanged();
    }
  });
  $id("lp").addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.target !== undefined) { st.p = C.setRule(st.p, t.dataset.target, t.value); choicesChanged(); }
    else if (t.id === "lp-pick" && t.value) { st.p = C.setRule(st.p, `src:${t.value}`, "normal"); choicesChanged(); }
    else if (t.id === "lp-maxage") {
      const v = Math.round(+t.value);
      if (v >= C.AGE[0] && v <= C.AGE[1]) { st.p = { ...st.p, max_age: v }; choicesChanged(); } else t.value = String(st.p.max_age);
    }
  });
  $id("lp").addEventListener("input", (e) => {
    if (e.target.id === "lp-mhz") e.target.setCustomValidity("");
    if (e.target.id !== "lp-vol") return;
    st.volume = Math.min(1, Math.max(0, +e.target.value / 100));
    if (st.muted && st.volume > 0) st.muted = false;
    levels();
    saveDevice();
    paint();
  });
  // the Listening panel: Escape (from anywhere while it is open) and a click outside it close it
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !st.panel || (UI.dialog && UI.dialog.isOpen && UI.dialog.isOpen())) return;
    e.stopPropagation();
    e.preventDefault();
    closePanel($id("lp-panel").contains(document.activeElement) || document.activeElement === document.body);
  }, true);
  document.addEventListener("mousedown", (e) => {
    if (st.panel && !e.target.closest("#lp-panel, #lp-set")) closePanel();
  }, true);
  // Go to: the Radio view on that channel, its row brought into view once it is drawn
  function goToRow() {
    const it = st.over || st.cur;
    if (!it) return;
    const id = it.row.id;
    let tries = 0;
    const find = () => {
      const n = document.querySelector(`#radio-view [data-tx="${CSS.escape(id)}"]`);
      if (n) {
        const li = n.closest("li") || n;
        li.scrollIntoView({ block: "center" });
        li.classList.add("lp-flash");
        setTimeout(() => li.classList.remove("lp-flash"), 2500);
      } else if (++tries < 20) setTimeout(find, 150);
    };
    setTimeout(find, 0);
  }
  if (window.UI && UI.keys) {
    UI.keys({
      m: () => { if (st.on) mute(); },
      "]": () => { if (st.on) skip(); },
    });
  }
  if (typeof cad !== "undefined" && cad.hooks) cad.hooks.push(onPush);
  paint(true);

  return {
    sourceControl, get on() { return st.on; }, get armed() { return st.armed; }, get prefs() { return st.p; },
    toggle: toggleOn, setOn,
    // for tests/test_live_player_ui.py: the queue's state as numbers
    get debug() { const w = C.waiting(st.q); return { ...w, playing: (st.over || st.cur || {}).id || null, under: st.over && st.cur ? st.cur.id : null, paused: (st.cur && st.cur.stopped && st.cur.id) || (st.q.paused && st.q.paused.id), dropped: st.q.dropped, armed: st.armed, cursor: st.cursor, held: st.manual,
                       t: st.over ? b.currentTime : a.currentTime, under_t: a.currentTime }; },
  };
})();
