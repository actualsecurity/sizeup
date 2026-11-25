// Reminders (sizeup/cad/reminders.py; Settings > Dispatch > Reminders): a call or a unit left too long in one state flashes
// ("hey, are you forgetting something"): an event Pending with no unit, a unit Dispatched and not moving, and the optional
// rules (a page not accepted, on location with no change, past its Until).
//
//   Timing   the state's reminders.items: {key, kind, iid, unit, status, since, due}, times from the hub. Due when the hub's
//            clock (serverNow: the state's time, cad.js) reaches due, so a wrong PC clock neither starts nor stops one.
//            Nothing redraws for it: tick() (once a second, cad.js) works out what is due and touches the page only
//            when that changes (a new one, an OK, a minute more on a chip) or something was drawn since (dirty()).
//   Where    whatever is drawn with data-rem-inc="<incident id>" (the queue's cards, the incident's head, the wall
//            board's cards) or data-rem-unit="<code>" (the unit board's rows, a call's unit rows, the cards' unit chips,
//            the maps' rigs, the wall board's strip). A due one gets data-rem="<kind>" (the flash: cad.css, ≤ 1 Hz,
//            steady with reduced motion); one with data-rem-chip also its chip ("Pending 6 min", "DP 6 min"; "age":
//            "6 min" only, beside a code that says it already) in its .rem-slot.
//   OK       a click on the flashing item (not on another of its buttons) or on its chip: POST /api/cad/reminders/ack,
//            quiet for everyone for the set minutes (the hub's snooze, in the event's timeline). Dispatchers and admins;
//            on /monitor only someone signed in as one. Quiet here at once; the next push confirms.
//   Sound    off unless turned on: the profile menu's "Reminder sound", the person's own (/api/me/prefs "reminders":
//            sound on the console, board_sound on /monitor, so the wall board stays silent unless set there).
"use strict";

window.Reminders = (() => {
  const INC_KINDS = new Set(["pending", "queued", "until"]);
  const RANK = { pending: 0, queued: 1, until: 2, dispatched: 0, onscene: 1 };
  const HEAD = { pending: "Pending", queued: "Not accepted", until: "Past Until" };
  const WHAT = { pending: "Pending with no unit", queued: "Page not accepted", until: "Past its Until",
                 dispatched: "Dispatched, not responding", onscene: "On location, no status change" };
  const PREF_KEY = "sizeup.reminders";
  const st = { sig: "", dirty: true, painted: false, seen: null, quiet: new Map(), prefs: readPrefs(), audio: null, lastSound: 0 };

  const items = () => ((cad.state && cad.state.reminders) || {}).items || [];
  const snoozeMin = () => ((cad.state && cad.state.reminders) || {}).snooze_min || 5;
  function mayAck() {                 // the console: dispatchers and admins; the wall board: only someone signed in as one
    if (!BOARD) return canDo("dispatch");
    const w = cad.who;
    return !!(w && w.active !== false && (w.may === "admin" || w.may === "dispatcher"));
  }
  const mins = (s) => Math.max(0, Math.floor(s / 60));
  function label(it, now, short) {
    const m = mins(now - it.since);
    const t = m < 1 ? "<1 min" : `${m} min`;
    if (INC_KINDS.has(it.kind)) return `${HEAD[it.kind]} ${t}`;
    return short ? t : `${it.status} ${t}`;
  }
  function tip(it, now) {
    const who = it.kind === "dispatched" || it.kind === "onscene" ? `${it.unit} ` : "";
    return `${who}${WHAT[it.kind]}: ${label(it, now)} (since ${fmtTime(it.since, true)})`
      + (it.by ? `\nOK'd earlier by ${String(it.by).replace(/\s*<[^<>]*>\s*$/, "")}` : "")
      + (mayAck() ? `\nClick: OK, quiet ${snoozeMin()} min for everyone` : "");
  }

  // What is due now, by where it shows: "i:<incident id>" / "u:<unit code>" -> the item (an event's most pressing one).
  function dueNow(now) {
    const out = new Map();
    for (const it of items()) {
      const q = st.quiet.get(it.key);
      if (q && q > it.due && now < q) continue;          // OK'd here, the push not back yet
      if (now < it.due) continue;
      const k = INC_KINDS.has(it.kind) ? `i:${it.iid}` : `u:${it.unit}`;
      const had = out.get(k);
      if (!had || RANK[it.kind] < RANK[had.kind]) out.set(k, it);
    }
    for (const [key, q] of st.quiet) if (now >= q || !items().some((it) => it.key === key)) st.quiet.delete(key);
    return out;
  }

  function paint(host, it, now, ack) {
    const kind = it ? it.kind : "";
    if ((host.dataset.rem || "") !== kind) {
      if (kind) host.dataset.rem = kind; else delete host.dataset.rem;
    }
    if (!("remChip" in host.dataset)) return;
    const slot = host.querySelector(".rem-slot") || host;
    let chip = slot.querySelector(":scope > .rem-chip");
    if (!it) { if (chip) chip.remove(); return; }
    // a button where it can be one (never inside another button: a map's rig); a plain chip without the right to OK
    const asBtn = ack && host.tagName !== "BUTTON" && !host.closest("button");
    if (chip && (chip.tagName === "BUTTON") !== asBtn) { chip.remove(); chip = null; }
    if (!chip) {
      chip = el(asBtn ? "button" : "span", "rem-chip");
      if (asBtn) chip.type = "button";
      slot.append(chip);
    }
    const text = label(it, now, host.dataset.remChip === "age");
    if (chip.textContent !== text) chip.textContent = text;
    chip.dataset.key = it.key;
    const t = tip(it, now);
    if (chip.title !== t) chip.title = t;
    const aria = (it.unit ? `${it.unit} ` : "") + `${WHAT[it.kind]}, ${label(it, now)}` + (asBtn ? `. OK for ${snoozeMin()} min` : "");
    if (chip.getAttribute("aria-label") !== aria) chip.setAttribute("aria-label", aria);
  }

  // Once a second (cad.js tick) and after every render: the page touched only when what is due, or a chip's words,
  // changed, or something was drawn since.
  function apply(force) {
    const now = serverNow(), due = dueNow(now), ack = mayAck();
    const sig = `${ack}|` + [...due].map(([k, it]) => `${k}=${it.key}:${label(it, now)}`).join(",");
    if (!force && !st.dirty && sig === st.sig) return;
    if (!due.size && !st.painted && !force) { st.sig = sig; st.dirty = false; return; }
    st.sig = sig;
    st.dirty = false;
    let any = false;
    for (const host of document.querySelectorAll("[data-rem-inc], [data-rem-unit]")) {
      const k = host.dataset.remInc ? `i:${host.dataset.remInc}` : `u:${host.dataset.remUnit}`;
      const it = due.get(k);
      if (it || "rem" in host.dataset || host.querySelector(".rem-chip")) paint(host, it, now, ack);
      any = any || !!it;
    }
    st.painted = any;
    announce(due, now);
  }

  // A new one: said to a screen reader (polite) and, when the person turned it on, a short chime. Not for the ones
  // already due when the page loaded.
  function announce(due, now) {
    const keys = new Set([...due.values()].map((it) => it.key));
    const fresh = st.seen ? [...due.values()].filter((it) => !st.seen.has(it.key)) : [];
    st.seen = keys;
    if (!fresh.length) return;
    let live = document.getElementById("rem-live");
    if (!live) {
      live = el("div", "sr-only");
      live.id = "rem-live";
      live.setAttribute("aria-live", "polite");
      document.body.append(live);
    }
    live.textContent = "Reminder: " + fresh.map((it) => {
      const inc = incidents().find((i) => i.id === it.iid);
      return `${it.unit || (inc ? incTitle(inc) : "")} ${WHAT[it.kind].toLowerCase()}, ${label(it, now)}`.trim();
    }).join("; ");
    if (soundOn() && Date.now() - st.lastSound > 20000) { st.lastSound = Date.now(); chime(); }
  }

  // ---- OK ---------------------------------------------------------------------------------------------------------
  async function ack(key) {
    const it = items().find((x) => x.key === key);
    if (!it || !mayAck()) return;
    st.quiet.set(key, serverNow() + snoozeMin() * 60);
    apply(true);
    try {
      await api("POST", "/api/cad/reminders/ack", { key, operator: operator() });
    } catch (e) {
      st.quiet.delete(key);
      apply(true);
      UI.toast(e.message, "err");
    }
  }
  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const host = t && t.closest("[data-rem]");
    if (!host || !mayAck()) return;
    const chip = t.closest(".rem-chip");
    const other = t.closest("button, a, input, select, textarea, summary, label");
    if (!chip && other && other !== host) return;        // another of its controls (a status, Follow): that only
    if (chip && chip.tagName === "BUTTON") e.stopPropagation();   // the chip's OK alone, not the card's own click
    const key = (chip && chip.dataset.key) || (host.querySelector(".rem-chip") || {}).dataset?.key || keyFor(host);
    if (key) ack(key);
  }, true);       // capture: before the item's own click, which may draw it again (a card selected)
  function keyFor(host) {
    const now = serverNow(), due = dueNow(now);
    const it = due.get(host.dataset.remInc ? `i:${host.dataset.remInc}` : `u:${host.dataset.remUnit}`);
    return it ? it.key : "";
  }

  // ---- sound (the person's own; off unless turned on) -------------------------------------------------------------------
  function readPrefs() {
    try { const v = JSON.parse(localStorage.getItem(PREF_KEY) || "null"); return v && typeof v === "object" ? v : {}; } catch (_) { return {}; }
  }
  const prefKey = () => (BOARD ? "board_sound" : "sound");
  const soundOn = () => st.prefs[prefKey()] === true;
  function keepPrefs(v) {
    st.prefs = { ...(v && typeof v === "object" ? v : {}) };
    for (const k of Object.keys(st.prefs)) if (!["sound", "board_sound"].includes(k) || typeof st.prefs[k] !== "boolean") delete st.prefs[k];
    try { localStorage.setItem(PREF_KEY, JSON.stringify(st.prefs)); } catch (_) { /* this page view only */ }
  }
  function setSound(on) {
    keepPrefs({ ...st.prefs, [prefKey()]: on });
    if (window.MePrefs) MePrefs.changed("reminders");
    if (on) chime();                                     // the click unlocks the browser's sound, and says what it is
  }
  if (window.MePrefs) {
    let had = false;
    try { had = localStorage.getItem(PREF_KEY) !== null; } catch (_) { /* none */ }
    MePrefs.register("reminders", { get: () => ({ ...st.prefs }), apply: (v) => keepPrefs(v), hadLocal: had });
  }
  function chime() {
    try {
      const C = window.AudioContext || window.webkitAudioContext;
      if (!C) return;
      const ctx = st.audio || (st.audio = new C());
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      const t0 = ctx.currentTime + 0.02;
      [[660, 0], [880, 0.22]].forEach(([hz, at]) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = "sine";
        o.frequency.value = hz;
        g.gain.setValueAtTime(0, t0 + at);
        g.gain.linearRampToValueAtTime(0.08, t0 + at + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.2);
        o.connect(g).connect(ctx.destination);
        o.start(t0 + at);
        o.stop(t0 + at + 0.22);
      });
    } catch (_) { /* no sound here */ }
  }
  if (window.Profile) {
    Profile.add({
      get label() { return BOARD ? "Reminder sound (this board)" : "Reminder sound"; },
      get checked() { return soundOn(); },
      get hint() { return soundOn() ? "On" : "Off"; },
      run: () => setSound(!soundOn()),
    });
  }

  return { apply, tick: () => apply(false), dirty: () => { st.dirty = true; }, label, dueNow, ack, soundOn };
})();
