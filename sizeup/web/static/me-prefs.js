// Each person's own console choices, kept on the hub (#823, sizeup/cad/prefs.py): the map layers' sets (maplayers.js)
// and the Pre-plan layer (preplan.js) follow the signed-in person to any browser or device.
//
//   Start      a script registers its namespace (MePrefs.register(ns, {get, apply, hadLocal})) with what it already
//              read from localStorage, which stays its instant copy (the map never flashes the defaults). One GET
//              /api/me/prefs for the page, then for each namespace:
//                - the hub has one: it wins (apply(value): the script takes it and writes its localStorage copy);
//                - the hub has none, and this browser's copy is the person's (or from before #823, nobody's): sent
//                  once (the migration);
//                - this browser's copy was someone else's (a shared screen, OWNER_KEY): the defaults (apply(null));
//                - a click before the hub answered: just that click (its leaves, edits()) on top of the value above,
//                  then sent: never this browser's whole copy over a newer one, never the last person's set.
//   A change   MePrefs.changed(ns) after the script wrote its localStorage copy: to the hub a second later (PUT
//              /api/me/prefs/<ns>, the whole namespace), as one write for a burst of clicks.
//   Failing    nothing waits on the hub. Signed out (#818), offline, the hub handing over: kept here and sent again
//              (backoff, and at once when the browser is online); a value the hub refuses (400, 404, 413) is not sent
//              again. Signed in again (#818's sizeup:signed-in): loaded again, maybe as someone else. Someone else
//              signed in meanwhile (409: X-Sizeup-Person): the last person's pending change is dropped and the new
//              person's choices loaded. Leaving the page: what is pending goes with it (keepalive).
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
  const isObj = (v) => !!v && typeof v === "object" && !Array.isArray(v);
  // the same value whatever the order of its keys (the hub's are sorted)
  const canon = (v) => (isObj(v) ? `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(",")}}`
    : JSON.stringify(v));
  const copy = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
  // a value's leaves: path (JSON) -> value
  function leaves(v, path = [], out = new Map()) {
    if (isObj(v)) for (const k of Object.keys(v)) leaves(v[k], [...path, k], out);
    else if (path.length) out.set(JSON.stringify(path), v);
    return out;
  }
  // what changed from base to now, leaf by leaf ([path, value | undefined: gone])
  function edits(base, now) {
    const a = leaves(base), b = leaves(now), out = [];
    for (const [k, v] of b) if (!a.has(k) || canon(a.get(k)) !== canon(v)) out.push([JSON.parse(k), v]);
    for (const k of a.keys()) if (!b.has(k)) out.push([JSON.parse(k), undefined]);
    return out;
  }
  function patch(target, eds) {
    const t = isObj(target) ? copy(target) : {};
    for (const [path, v] of eds) {
      let o = t;
      for (const k of path.slice(0, -1)) { if (!isObj(o[k])) o[k] = {}; o = o[k]; }
      if (v === undefined) delete o[path[path.length - 1]];
      else o[path[path.length - 1]] = v;
    }
    return t;
  }

  // mode: idle (nothing registered yet) | loading | hub (signed in: synced) | local (nobody signed in) | retry
  const st = { mode: "idle", id: null, hub: {}, ownerWas: stored(OWNER_KEY), loading: null, tries: 0, timer: null };
  // ns -> {get, apply, hadLocal, base (the value last synced, or as read here: what a click is measured from), dirty,
  // timer, inflight, tries}
  const spaces = new Map();

  function later(ms, fn) { return setTimeout(fn, ms); }
  const backoff = (n) => RETRY_MS[Math.min(Math.max(n, 1), RETRY_MS.length) - 1];
  function value(s) { try { return copy(s.get()); } catch (e) { console.error("prefs:", e); return undefined; } }

  function load() {
    if (st.loading) return st.loading;
    if (st.mode !== "hub") st.mode = "loading";
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
        const id = String(d.id);
        if (st.id !== null && st.id !== id) st.ownerWas = st.id;     // signed in again as someone else
        st.id = id;
        st.hub = isObj(d.prefs) ? d.prefs : {};
        st.mode = "hub";
        st.tries = 0;
        for (const ns of spaces.keys()) reconcile(ns);
        st.ownerWas = id;
        remember(OWNER_KEY, id);
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
    const has = Object.prototype.hasOwnProperty.call(st.hub, ns);
    const mine = st.ownerWas === null || st.ownerWas === st.id;
    if (s.dirty) {                                   // clicked meanwhile: those clicks, on top of what is the person's
      const eds = edits(s.base, value(s));
      const merged = patch(has ? st.hub[ns] : mine ? s.base : null, eds);
      if (canon(merged) !== canon(value(s))) apply(s, merged);
      flush(ns);
      return;
    }
    if (has) {
      if (canon(st.hub[ns]) !== canon(value(s))) apply(s, st.hub[ns]);
      s.base = copy(st.hub[ns]);
      return;
    }
    if (!mine) { apply(s, null); s.base = value(s); return; }      // someone else's copy: not theirs to inherit
    if (s.hadLocal) { s.dirty = true; flush(ns); }   // the first time: this browser's choices, once
  }
  function apply(s, v) {
    try { s.apply(copy(v === undefined ? null : v)); } catch (e) { console.error("prefs:", e); }
  }

  function flush(ns, keepalive = false) {
    const s = spaces.get(ns);
    if (!s) return Promise.resolve();
    clearTimeout(s.timer);
    s.timer = null;
    if (!s.dirty || st.mode !== "hub") return Promise.resolve();
    if (s.inflight && !keepalive) return s.inflight;  // (its end sends this one; leaving the page: now)
    const v = value(s);
    if (v === undefined) return Promise.resolve();
    s.dirty = false;
    const id = st.id;
    const req = fetch(`${API}/${encodeURIComponent(ns)}`, {
      method: "PUT", credentials: "same-origin", redirect: "manual", keepalive,
      headers: { "Content-Type": "application/json", "X-Sizeup-Person": id }, body: JSON.stringify(v) })
      .then((r) => {
        if (r.ok) {
          s.tries = 0;
          st.hub[ns] = v;
          s.base = copy(v);
          return;
        }
        if (r.status === 409) {                    // someone else signed in on this browser: theirs, not this change
          st.ownerWas = id;                        // this page's values are the last person's
          for (const o of spaces.values()) {
            o.dirty = false;
            o.base = value(o);                     // a click from now on: the new person's, measured from here
            clearTimeout(o.timer);
            o.timer = null;
          }
          st.mode = "retry";
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
        if (s.inflight === req) s.inflight = null;
        if (s.dirty && !s.timer && st.mode === "hub") s.timer = later(DEBOUNCE_MS, () => flush(ns));   // changed meanwhile
      });
    s.inflight = req;
    return req;
  }

  // a script's namespace: get() its value now, apply(value | null) take the hub's (null: the defaults), hadLocal: this
  // browser had a copy of its own (sent once when the hub has none)
  function register(ns, opts) {
    const s = { get: opts.get, apply: opts.apply, hadLocal: !!opts.hadLocal, base: undefined, dirty: false, timer: null,
                inflight: null, tries: 0 };
    spaces.set(ns, s);
    s.base = value(s);
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
  function online() {                              // at once, not at the backoff
    if (st.mode === "retry") { st.tries = 0; load(); return; }
    for (const [ns, s] of spaces) if (s.dirty) { s.tries = 0; flush(ns); }
  }
  function signedIn() {                            // signed in again (#818): maybe as someone else: ask who first
    if (st.mode === "local") return;
    st.tries = 0;
    for (const s of spaces.values()) { clearTimeout(s.timer); s.timer = null; }
    load();
  }
  if (typeof window.addEventListener === "function") {
    window.addEventListener("online", online);
    window.addEventListener("sizeup:signed-in", signedIn);
    window.addEventListener("pagehide", () => { flushAll(true); });     // a change in its last second still goes
  }

  return { register, changed, load, flushAll, _st: st, OWNER_KEY, DEBOUNCE_MS };
})();
