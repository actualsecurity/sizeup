// The console's Cloudflare Access sign-in (#818). Admin, 2026-11: "can we raise a 'reauth' button or warning when we
// are coming up on the end of an auth session?"
//
// When the sign-in ends, Cloudflare's edge answers every request with a redirect to the Access login: none reaches
// the hub. In the page a fetch fails as a CORS error (TypeError), /api/audio/*.wav fails (ORB-blocked) and the stream
// drops, so it looked like the hub was down ("Reconnecting…") until someone reloaded. Here:
//
//   ending   GET /api/session {expires_at, now}: the verified token's exp (the hub's clock; the HttpOnly cookie can't
//            be read here). Asked on load, every POLL_MS, when the page is looked at again (focus / visible) and
//            after a re-sign-in. WARN_S before it ends: "Sign-in ends in 9 min · Re-sign in".
//   ended    one failed same-origin fetch (a TypeError, a 401), an audio error or stall on /api/..., a dropped
//            stream (live.js) makes one check of /api/session with redirect: "manual" — an opaque redirect (the
//            edge sending it to the login) or a 401 / 403 is signed out: "Signed out · Re-sign in". Meanwhile
//            same-origin fetches fail at once without going out (no storm of redirects), live.js stops polling
//            and holds the stream, the "Reconnecting…" pill stays away, and the live player holds its queue.
//   again    "Re-sign in" opens /reauth in a pop-up (blocked: the link opens it in a new tab); after Access signs
//            in there, it says so (postMessage to this origin, BroadcastChannel) and closes. A message only makes
//            this page check /api/session again: nothing in it is trusted. Signed in again: the stream resumes
//            (window event sizeup:signed-in, whenIn()), and audio that failed meanwhile is tried again.
//
// Without Access (all-in-one, local dev) expires_at is null: no warning; the checks still find nothing wrong.
"use strict";

const SessionWatch = (() => {
  const SESSION_URL = "/api/session";
  const REAUTH_URL = "/reauth";
  const MSG = "sizeup:signed-in";
  const CHANNEL = "sizeup-session";
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
               pill: null, text: null, popup: null, started: false };
  const wants = new WeakMap();           // a media element -> the src play() was last asked for
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
    return !!u && u.origin === location.origin && !u.pathname.startsWith("/api/v1/") && !u.pathname.startsWith("/api/station/");
  }
  function mediaOurs(m) {
    const u = url(m.currentSrc || m.src || "");
    return !!u && u.origin === location.origin && u.pathname.startsWith("/api/");
  }

  // -- the check ------------------------------------------------------------------------------------------------
  // "ok" (signed in: exp read), "out" (the edge redirected, or the hub said 401 / 403), "unknown" (out of reach,
  // an older hub without /api/session, an answer that isn't the one asked for: nothing changes)
  function check() {
    if (st.inflight) return st.inflight;
    st.inflight = (async () => {
      let r;
      try {
        r = await nativeFetch(`${SESSION_URL}?t=${now()}`, { redirect: "manual", cache: "no-store", credentials: "same-origin" });
      } catch (_) {
        return "unknown";
      }
      if (r.type === "opaqueredirect" || r.status === 401 || r.status === 403) { signedOut(); return "out"; }
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
      try {
        if (typeof m.sessionRetry === "function") m.sessionRetry();
        else {
          m.load();
          if (f.play) m.play().catch(() => {});
        }
      } catch (_) { /* gone */ }
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
  function render() {
    if (typeof document === "undefined" || !document.body) return;
    const text = label();
    if (!text) {
      if (st.pill && st.pill.isConnected) st.pill.remove();
      return;
    }
    if (!st.pill) {
      const p = document.createElement("div");
      p.className = "pill session-pill";
      p.setAttribute("role", "status");
      st.text = document.createElement("span");
      const a = document.createElement("a");
      a.href = REAUTH_URL;
      a.target = "_blank";
      a.textContent = "Re-sign in";
      a.addEventListener("click", reauth);
      p.append(st.text, " · ", a);
      st.pill = p;
    }
    st.pill.classList.toggle("err", st.out);
    if (st.text.textContent !== text) st.text.textContent = text;
    const tip = st.out
      ? "Cloudflare Access sign-in ended. Re-sign in opens a small window; this page and its audio keep going."
      : `Cloudflare Access sign-in ends at ${clock(st.deadline)}. Re-sign in opens a small window; this page and its audio keep going.`;
    if (st.pill.title !== tip) st.pill.title = tip;
    if (!st.pill.isConnected) document.body.append(st.pill);
  }
  // Re-sign in: /reauth in a pop-up; blocked (null), the link's own default opens it in a new tab
  function reauth(ev) {
    const w = window.open(REAUTH_URL, "sizeup-reauth", "popup,width=520,height=640");
    if (w) {
      ev.preventDefault();
      st.popup = w;
    }
  }
  function message(ev) {
    if (ev.origin !== location.origin || !ev.data || ev.data.type !== MSG) return;
    check();
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
    if (m.sessionRetry !== false) st.failed.set(m, { src: m.src, play: wants.get(m) === m.src, at: now() });
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
      wants.set(this, this.src);
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
    try {
      const ch = new BroadcastChannel(CHANNEL);
      ch.onmessage = (ev) => { if (ev.data && ev.data.type === MSG) check(); };
    } catch (_) { /* no BroadcastChannel: focus checks */ }
    window.addEventListener("focus", look);
    document.addEventListener("visibilitychange", look);
    setInterval(tick, TICK_MS);
    check();
  }

  return {
    start, check, suspect, whenIn, label,
    get out() { return st.out; },
    // tests: the state, as-is
    _st: st,
  };
})();
if (typeof window !== "undefined") {
  window.SessionWatch = SessionWatch;
  SessionWatch.start();
}
