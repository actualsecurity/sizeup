// The console's Cloudflare Access sign-in (#818). Admin, 2026-11: "can we raise a 'reauth' button or warning when we
// are coming up on the end of an auth session?"
//
// When the sign-in ends, Cloudflare's edge answers every request with a redirect to the Access login: none reaches
// the hub. In the page a fetch fails as a CORS error (TypeError), /api/audio/*.wav fails (ORB-blocked) and the stream
// drops, so it looked like the hub was down ("Reconnecting…") until someone reloaded. Here:
//
//   ending   GET /api/session {expires_at, now}: the verified token's exp (the hub's clock; the HttpOnly cookie can't
//            be read here). Asked on load, every POLL_MS, when the page is looked at again (focus / visible) and
//            after a re-sign-in. WARN_S before it ends: the header's profile button (profile.js) gets an amber ring
//            and "9 min · Re-sign in" beside it.
//   ended    one failed same-origin fetch (a TypeError, a 401), an audio error or stall on /api/..., a dropped
//            stream (live.js) makes one check of /api/session with redirect: "manual" — an opaque redirect (the
//            edge sending it to the login) is signed out: a red ring, "Signed out · Re-sign in" (a 401 / 403 from the hub itself
//            is not: the edge let it through, e.g. a hub just started without the team's keys yet). Meanwhile
//            same-origin fetches fail at once without going out (no storm of redirects), live.js stops polling
//            and holds the stream, the "Reconnecting…" pill stays away, and the live player holds its queue.
//   again    "Re-sign in" opens a pop-up (blocked: the link opens it in a new tab): before the end /reauth/start
//            (the hub drops the still-good Access cookie, so the edge really signs in again: the token's exp is fixed,
//            nothing renews it), after it /reauth: opened in the click, straight at that address. After Access signs in
//            there, /reauth says so (BroadcastChannel, localStorage, postMessage: this origin only; none needs the
//            opener) and closes, and this page closes it too. A message only makes this page check /api/session again: nothing
//            in it is trusted. Signed in again: the stream resumes (window event sizeup:signed-in, whenIn()), and a
//            player that asks for it (element.sessionRetry, cad.js's) plays its failed clip again.
//
// Without Access (all-in-one, local dev) expires_at is null: no warning; the checks still find nothing wrong.
"use strict";

const SessionWatch = (() => {
  const SESSION_URL = "/api/session";
  const REAUTH_URL = "/reauth";             // signed out: Access asks for the sign-in on the way
  const NOT_OURS = /^\/api\/(v1|station)\//;  // the app's API (device tokens) and the station's: not this sign-in
  const RENEW_URL = "/reauth/start";        // still signed in: the hub drops the cookie first (server.py)
  const MSG = "sizeup:signed-in";
  const CHANNEL = "sizeup-session";       // BroadcastChannel: this origin's tabs only (reauth.js says so on it)
  const STORE_KEY = "sizeup:signed-in";   // the same through localStorage's storage event (no BroadcastChannel)
  const POPUP = "width=520,height=640";   // a small window (sized: a pop-up in every browser)
  // A pop-up shows its first, blank page until a page of the sign-in commits: a chain of redirects (all of Access's,
  // when its session is still good) looks blank all along. Still blank at RESEND_MS: sent to /reauth (never
  // /reauth/start again: the cookie may be dropped already, and the login carries on from /reauth); still blank at
  // GIVE_UP_MS and still signed in (no login going on there): closed, and Re-sign in opens a tab for STUCK_MS.
  const RESEND_MS = 8000, GIVE_UP_MS = 30000, STUCK_MS = 60 * 60 * 1000;
  const WARN_S = 600;                    // the warning shows this long before the sign-in ends
  const POLL_MS = 5 * 60 * 1000;         // asked again this often (a re-sign-in elsewhere moves exp)
  const OUT_POLL_MS = 60 * 1000;         // signed out: asked this often (signed in again in another tab)
  const TICK_MS = 15 * 1000;             // the minutes on the warning
  const SUSPECT_GAP_MS = 10 * 1000;      // failures check at most this often
  const LOOK_GAP_MS = 5 * 1000;          // focus / visible: at most this often
  const RETRY_MAX_MS = 10 * 60 * 1000;   // audio that failed this long ago is not tried again
  const hasWindow = typeof window !== "undefined";
  const nativeFetch = hasWindow && window.fetch ? window.fetch.bind(window) : null;
  const st = { deadline: 0, out: false, inflight: null, lastCheck: 0, lastSuspect: -Infinity, waiters: [], failed: new Map(),
               shown: "", popup: null, started: false };
  const seenEvents = new WeakSet();      // a media error seen once (the element's listener and the page's)
  const watched = new WeakSet();

  const now = () => Date.now();
  function url(x) {
    try {
      const s = typeof x === "string" ? x : (x && typeof x.url === "string") ? x.url : String(x);
      return new URL(s, location.href);
    } catch (_) { return null; }
  }
  // a request the console makes to its own hub (the app's /api/v1 and the station's /api/station are not the
  // console's sign-in)
  function ours(x) {
    const u = url(x);
    return !!u && u.origin === location.origin && !NOT_OURS.test(u.pathname);
  }
  function mediaOurs(m) {
    const u = url(m.currentSrc || m.src || "");
    return !!u && u.origin === location.origin && u.pathname.startsWith("/api/");
  }

  // -- the check ------------------------------------------------------------------------------------------------
  // "ok" (signed in: exp read), "out" (the edge redirected to the Access login), "unknown" (out of reach, an older
  // hub without /api/session, a 401 / 403 from the hub itself, an answer that isn't the one asked for: nothing
  // changes). fresh: a check sent after now (a re-sign-in just happened), not one already on its way.
  function check(fresh = false) {
    if (st.inflight) return fresh ? st.inflight.then(() => check()) : st.inflight;
    st.inflight = (async () => {
      let r;
      try {
        r = await nativeFetch(`${SESSION_URL}?t=${now()}`, { redirect: "manual", cache: "no-store", credentials: "same-origin" });
      } catch (_) {
        return "unknown";
      }
      if (r.type === "opaqueredirect") { signedOut(); return "out"; }
      if (!r.ok) return "unknown";
      let d = null;
      try { d = await r.json(); } catch (_) { /* not ours */ }
      if (!d || typeof d !== "object" || !("expires_at" in d)) return "unknown";
      signedIn(d);
      return "ok";
    })().finally(() => { st.inflight = null; st.lastCheck = now(); });
    return st.inflight;
  }
  // something failed in a way a signed-out page fails: check (not too often)
  function suspect() {
    if (st.out || st.inflight || now() - st.lastSuspect < SUSPECT_GAP_MS) return;
    st.lastSuspect = now();
    check();
  }

  function signedIn(d) {
    const exp = Number(d.expires_at), at = Number(d.now);
    if (d.expires_at != null && Number.isFinite(exp) && exp > 0 && Number.isFinite(at)) {
      st.deadline = now() + (exp - at) * 1000;        // on this clock, whatever this machine's clock says
    } else {
      st.deadline = 0;                                // no Access: nothing ends
    }
    const was = st.out;
    st.out = false;
    render();
    if (was) resume();
  }
  function signedOut() {
    if (st.out) return;
    st.out = true;
    if (hasWindow && window.LiveConn) window.LiveConn.up();  // not "Reconnecting…": signed out says what it is
    render();
    if (hasWindow) window.dispatchEvent(new CustomEvent("sizeup:signed-out"));
  }
  function resume() {
    const ws = st.waiters;
    st.waiters = [];
    for (const ok of ws) ok();
    if (hasWindow) window.dispatchEvent(new CustomEvent("sizeup:signed-in"));
    const t = now();
    for (const [m, f] of st.failed) {
      if (t - f.at > RETRY_MAX_MS || m.src !== f.src) continue;   // old, or something else plays there now
      try { m.sessionRetry(); } catch (_) { /* gone */ }
    }
    st.failed.clear();
  }
  // resolves when signed in (now, if it is)
  function whenIn() {
    return st.out ? new Promise((ok) => st.waiters.push(ok)) : Promise.resolve();
  }

  // -- the warning ----------------------------------------------------------------------------------------------
  function clock(ms) {
    const d = new Date(ms);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  // what it says now: "" (nothing), "Sign-in ends in N min", "Signed out"
  function label(t = now()) {
    if (st.out) return "Signed out";
    if (!st.deadline) return "";
    const left = st.deadline - t;
    if (left > WARN_S * 1000) return "";
    return `Sign-in ends in ${Math.max(1, Math.ceil(left / 60000))} min`;
  }
  // the state for the header's profile button (profile.js): level "" | "warn" (WARN_S or less left) | "out"; left:
  // whole minutes left (at least 1) while warning; until: when it ends, this machine's ms (0: not known / no Access);
  // access: the console is behind Access (an end is known, or it has ended)
  function state(t = now()) {
    const left = st.deadline ? st.deadline - t : Infinity;
    const level = st.out ? "out" : left <= WARN_S * 1000 ? "warn" : "";
    return { level, left: level === "warn" ? Math.max(1, Math.ceil(left / 60000)) : 0, until: st.out ? 0 : st.deadline,
             access: st.out || !!st.deadline, label: label(t) };
  }
  // the hover text: what it is, when it ends
  function tip() {
    if (st.out) return "Cloudflare Access sign-in ended. Re-sign in opens a small window; this page and its audio keep going.";
    if (!st.deadline) return "";
    return `Cloudflare Access sign-in ends at ${clock(st.deadline)}. Re-sign in opens a small window; this page and its audio keep going.`;
  }
  // where Re-sign in goes: before the end /reauth/start, after it /reauth
  const href = () => (st.out ? REAUTH_URL : RENEW_URL);
  // the header's profile (profile.js) draws it: told on every change (sizeup:session), and asked on each tick
  function render() {
    if (!hasWindow) return;
    const s = state(), key = `${s.level}|${s.left}|${s.until}`;
    if (key === st.shown) return;
    st.shown = key;
    window.dispatchEvent(new CustomEvent("sizeup:session"));
  }
  // Re-sign in (a click on a link to href()): a pop-up opened right here, in the click, straight at its address (no
  // blank window navigated later), each time a new one (_blank: never an earlier pop-up that got stuck, #826).
  // Blocked (null), or one already given up on (stuck): the link's own default opens it in a new tab. The pop-up
  // keeps its opener only so this page can close it; the way back never needs it (BroadcastChannel, storage).
  // true: opened.
  function reauth(ev) {
    if (now() < (st.stuckUntil || 0)) return false;   // a pop-up stuck on its blank page lately: a tab this time
    closePopup();
    const w = window.open(new URL(href(), location.href).href, "_blank", POPUP);
    if (!w || w.closed) return false;                 // blocked (some blockers hand back a closed window)
    if (ev && ev.preventDefault) ev.preventDefault();
    st.popup = w;
    watchPopup(w);
    return true;
  }
  // A pop-up that never leaves its blank first page (#826: a browser left it "Untitled", about:blank). While it is on
  // that page (or back on this hub) it is this origin and can be read; on the Access login it can't (moved on).
  function at(w) {
    try { return w && !w.closed ? w.location.href : ""; } catch (_) { return "elsewhere"; }
  }
  function watchPopup(w) {
    setTimeout(() => {
      if (st.popup !== w || at(w) !== "about:blank") return;
      try { w.location.replace(new URL(REAUTH_URL, location.href).href); } catch (_) { /* moved on meanwhile */ }
      setTimeout(async () => {
        if (st.popup !== w || at(w) !== "about:blank") return;
        if ((await check(true)) !== "ok") return;     // dropped (a login going on there) or not known: left alone
        if (st.popup !== w || at(w) !== "about:blank") return;
        st.stuckUntil = now() + STUCK_MS;
        closePopup();
      }, GIVE_UP_MS - RESEND_MS);
    }, RESEND_MS);
  }
  function closePopup() {
    const w = st.popup;
    st.popup = null;
    try { if (w && !w.closed) w.close(); } catch (_) { /* gone */ }
  }
  // signed in again (any /reauth of this origin): checked; the pop-up this page opened is closed from here too when it
  // is back on /reauth (its own close() can be refused), never while it is on the Access login (another tab's signal)
  function signal() {
    const w = st.popup;
    let home = "";
    try { home = w && !w.closed ? w.location.pathname : ""; } catch (_) { /* elsewhere: on the login */ }
    if (home === REAUTH_URL) closePopup();
    check(true);
  }
  function message(ev) {
    if (ev.origin !== location.origin || !ev.data || ev.data.type !== MSG) return;
    signal();
  }
  // localStorage's storage event: fired only in this origin's other pages, with the key reauth.js set
  function stored(ev) {
    if (ev.key !== STORE_KEY || !ev.newValue) return;
    signal();
  }

  // -- watching what fails --------------------------------------------------------------------------------------
  // Same-origin fetches: a TypeError (the edge's redirect to the login, cross-origin: no CORS) or a 401 checks;
  // signed out, they fail at once with a TypeError, as they would have, without going out.
  function wrapFetch() {
    if (!nativeFetch) return;
    window.fetch = function fetch(input, init) {
      if (!ours(input)) return nativeFetch(input, init);
      if (st.out) return Promise.reject(new TypeError("signed out (Cloudflare Access)"));
      return nativeFetch(input, init).then((r) => {
        if (r.status === 401 || r.type === "opaqueredirect") suspect();
        return r;
      }, (e) => {
        if (!(e && e.name === "AbortError")) suspect();
        throw e;
      });
    };
  }
  function mediaFailed(ev) {
    const m = ev.target;
    if (seenEvents.has(ev) || !m || typeof m.load !== "function" || !mediaOurs(m)) return;
    seenEvents.add(ev);
    if (ev.type === "stalled") return suspect();
    if (typeof m.sessionRetry === "function") st.failed.set(m, { src: m.src, at: now() });   // its own retry only
    check().then((s) => { if (s !== "out") st.failed.delete(m); });
  }
  function watch(m) {
    if (watched.has(m)) return;
    watched.add(m);
    m.addEventListener("error", mediaFailed);
    m.addEventListener("stalled", mediaFailed);
  }
  // every audio / video element the scripts play (most are new Audio(), never in the page: their errors don't
  // reach the document); those in the page with their own controls through the document's capture below
  function wrapPlay() {
    const P = hasWindow && window.HTMLMediaElement && window.HTMLMediaElement.prototype;
    if (!P || P.play.sessionWrapped) return;
    const nativePlay = P.play;
    const play = function play() {
      watch(this);
      return nativePlay.apply(this, arguments);
    };
    play.sessionWrapped = true;
    P.play = play;
  }

  function look() {
    if (document.visibilityState === "hidden" || now() - st.lastCheck < LOOK_GAP_MS) return;
    check();
  }
  function tick() {
    const t = now();
    if (st.out) {
      if (t - st.lastCheck >= OUT_POLL_MS) check();
    } else if ((st.deadline && t >= st.deadline && t - st.lastCheck >= SUSPECT_GAP_MS) || t - st.lastCheck >= POLL_MS) {
      check();
    }
    render();
  }

  function start() {
    if (st.started || !hasWindow || !nativeFetch) return;
    st.started = true;
    wrapFetch();
    wrapPlay();
    document.addEventListener("error", (ev) => { if (ev.target instanceof HTMLMediaElement) mediaFailed(ev); }, true);
    document.addEventListener("stalled", (ev) => { if (ev.target instanceof HTMLMediaElement) mediaFailed(ev); }, true);
    window.addEventListener("message", message);
    window.addEventListener("storage", stored);
    try {
      const ch = new BroadcastChannel(CHANNEL);
      ch.onmessage = (ev) => { if (ev.data && ev.data.type === MSG) signal(); };
    } catch (_) { /* no BroadcastChannel: focus checks */ }
    window.addEventListener("focus", look);
    document.addEventListener("visibilitychange", look);
    setInterval(tick, TICK_MS);
    check();
  }

  return {
    start, check, suspect, whenIn, label, state, tip, href, reauth, closePopup,
    get out() { return st.out; },
    // tests: the state, as-is
    _st: st,
  };
})();
if (typeof window !== "undefined") {
  window.SessionWatch = SessionWatch;
  SessionWatch.start();
}
