// Each person's own console choices, kept on the hub (#823, sizeup/cad/prefs.py): the map layers' sets (maplayers.js)
// and the Pre-plan layer (preplan.js) follow the signed-in person to any browser or device.
//
//   Start      a script registers its namespace (MePrefs.register(ns, {get, apply, hadLocal})) with what it already
//              read from localStorage, which stays its instant copy (the map never flashes the defaults). One GET
//              /api/me/prefs for the page, then for each namespace:
//                - changed here before the hub answered: this page's choice is sent (it is the newer one);
//                - the hub has one: it wins (apply(value): the script takes it and writes its localStorage copy);
//                - the hub has none, and this browser's copy is the person's (or from before #823, nobody's): sent
//                  once (the migration);
//                - this browser's copy was someone else's (a shared screen, OWNER_KEY): the defaults (apply(null)).
//   A change   MePrefs.changed(ns) after the script wrote its localStorage copy: to the hub a second later (PUT
//              /api/me/prefs/<ns>, the whole namespace), as one write for a burst of clicks.
//   Failing    nothing waits on the hub. Signed out (#818), offline, the hub handing over: kept here and sent again
//              (backoff, and at once when the browser is online or signed in again); a value the hub refuses (400,
//              404, 413) is not sent again. Signed in as someone else meanwhile (409: X-Sizeup-Person): the pending
//              change is dropped and the new person's choices loaded.
//   No sign-in all-in-one or local dev (GET says signed_in false): this browser only, as before.
//   Other tabs and devices take a change on their next load (no live sync). /monitor, the wall board, is a profile
//              of its own (the "overview" set) of whoever is signed in on that screen.
"use strict";

window.MePrefs = (() => {
  const API = "/api/me/prefs";
  const OWNER_KEY = "sizeup.prefsOwner";        // whose choices this browser's copies are: the hub's id for them
  const DEBOUNCE_MS = 1000;
  const RETRY_MS = [5000, 15000, 60000, 300000];
  const REFUSED = new Set([400, 404, 413]);

  function stored(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function remember(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* this page view only */ } }
  // the same value whatever the order of its keys (the hub's are sorted)
  const canon = (v) => (v && typeof v === "object" && !Array.isArray(v)
    ? `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(",")}}` : JSON.stringify(v));

  // mode: idle (nothing registered yet) | loading | hub (signed in: synced) | local (nobody signed in) | retry
  const st = { mode: "idle", id: null, hub: {}, ownerWas: stored(OWNER_KEY), loading: null, tries: 0, timer: null };
  const spaces = new Map();       // ns -> {get, apply, hadLocal, dirty, timer, inflight, tries}

  function later(ms, fn) { return setTimeout(fn, ms); }
  const backoff = (n) => RETRY_MS[Math.min(Math.max(n, 1), RETRY_MS.length) - 1];

  function load() {
    if (st.loading) return st.loading;
    if (st.mode === "idle" || st.mode === "retry") st.mode = "loading";
    clearTimeout(st.timer);
    st.timer = null;
    st.loading = (async () => {
      try {
        const r = await fetch(API, { credentials: "same-origin", cache: "no-store", redirect: "manual" });
        if (!r.ok) throw new Error(`${r.status || r.type}`);
        const d = await r.json();
        if (!d || d.signed_in !== true) {            // nobody signed in: this browser only
          st.mode = "local";
          return;
        }
        if (d.id === null || d.id === undefined) throw new Error("not on the list yet");    // their first request
        st.id = String(d.id);
        st.hub = d.prefs && typeof d.prefs === "object" ? d.prefs : {};
        st.mode = "hub";
        st.tries = 0;
        for (const ns of spaces.keys()) reconcile(ns);
        remember(OWNER_KEY, st.id);
      } catch (_) {
        st.mode = "retry";                           // signed out, offline, handing over: as they are, and again
        st.tries++;
        st.timer = later(backoff(st.tries), load);
      } finally {
        st.loading = null;
      }
    })();
    return st.loading;
  }

  function reconcile(ns) {
    const s = spaces.get(ns);
    if (!s || st.mode !== "hub") return;
    if (s.dirty) { flush(ns); return; }                      // changed here meanwhile: the newer
    if (Object.prototype.hasOwnProperty.call(st.hub, ns)) {
      if (canon(st.hub[ns]) !== canon(s.get())) apply(s, st.hub[ns]);
      return;
    }
    const mine = st.ownerWas === null || st.ownerWas === st.id;
    if (!mine) { apply(s, null); return; }                   // someone else's copy: not theirs to inherit
    if (s.hadLocal) { s.dirty = true; flush(ns); }           // the first time: this browser's choices, once
  }
  function apply(s, v) {
    try { s.apply(v); } catch (e) { console.error("prefs:", e); }
  }

  // (the timer's first, then the request: a change while one is out goes in the next)
  function flush(ns, keepalive = false) {
    const s = spaces.get(ns);
    if (!s) return Promise.resolve();
    clearTimeout(s.timer);
    s.timer = null;
    if (!s.dirty || st.mode !== "hub") return Promise.resolve();
    if (s.inflight) return s.inflight;             // (its end sends this one)
    s.dirty = false;
    let value;
    try { value = s.get(); } catch (e) { console.error("prefs:", e); return Promise.resolve(); }
    const body = JSON.stringify(value);
    const id = st.id;
    s.inflight = fetch(`${API}/${encodeURIComponent(ns)}`, {
      method: "PUT", credentials: "same-origin", redirect: "manual", keepalive,
      headers: { "Content-Type": "application/json", "X-Sizeup-Person": id }, body })
      .then((r) => {
        if (r.ok) {
          s.tries = 0;
          st.hub[ns] = value;
          return;
        }
        if (r.status === 409) {                    // signed in as someone else now: theirs, not this change
          st.ownerWas = id;                        // this browser's copies are the last person's
          st.mode = "retry";
          for (const o of spaces.values()) { o.dirty = false; clearTimeout(o.timer); o.timer = null; }
          load();
          return;
        }
        if (REFUSED.has(r.status)) {               // not a value the hub keeps: here only, not sent again
          console.warn(`prefs: ${ns} not kept on the hub (${r.status})`);
          return;
        }
        throw new Error(`${r.status || r.type}`);
      })
      .catch(() => {
        s.dirty = true;                            // kept here; sent again
        s.tries++;
        clearTimeout(s.timer);
        s.timer = later(backoff(s.tries), () => flush(ns));
      })
      .finally(() => {
        s.inflight = null;
        if (s.dirty && !s.timer) s.timer = later(DEBOUNCE_MS, () => flush(ns));     // changed while it was out
      });
    return s.inflight;
  }

  // a script's namespace: get() its value now, apply(value | null) take the hub's (null: the defaults), hadLocal: this
  // browser had a copy of its own (sent once when the hub has none)
  function register(ns, opts) {
    spaces.set(ns, { get: opts.get, apply: opts.apply, hadLocal: !!opts.hadLocal, dirty: false, timer: null,
                     inflight: null, tries: 0 });
    if (st.mode === "idle") load();
    else if (st.mode === "hub") reconcile(ns);
  }

  // the script changed it (and wrote its own localStorage copy)
  function changed(ns) {
    const s = spaces.get(ns);
    if (!s) return;
    s.dirty = true;
    if (st.mode !== "hub") return;                 // loading / retry: sent once the hub answers; local: never
    clearTimeout(s.timer);
    s.timer = later(DEBOUNCE_MS, () => flush(ns));
  }

  function flushAll(keepalive = false) {
    return Promise.all([...spaces.keys()].map((ns) => flush(ns, keepalive)));
  }
  function again() {                               // online, or signed in again (#818): at once, not at the backoff
    if (st.mode === "retry") { st.tries = 0; load(); return; }
    for (const [ns, s] of spaces) if (s.dirty) { s.tries = 0; flush(ns); }
  }
  if (typeof window.addEventListener === "function") {
    window.addEventListener("online", again);
    window.addEventListener("sizeup:signed-in", again);
    window.addEventListener("pagehide", () => { flushAll(true); });     // a change in its last second still goes
  }

  return { register, changed, load, flushAll, again, _st: st, OWNER_KEY, DEBOUNCE_MS };
})();
