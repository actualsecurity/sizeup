// Live state for a page: the server-sent event stream when it works, polling when it doesn't.
// Some networks cut or hold event streams: an ECC workstation behind browser isolation (2026-10-07)
// sat on "connecting…" with empty lists while the hub saw each /api/cad/stream cancelled after a
// few seconds. So the page paints from a plain GET first, then follows the stream; if the stream
// errors or goes quiet it polls the same state every POLL_MS until the stream delivers again.
//
// Only what changed (#137): the server sends the whole state once, then each push only the sections
// (top-level keys) that changed, {"_delta": 1, ...those, "_gone": [keys]?}; idle, that is the clock alone.
// They are merged here, so onState always gets the whole state (a new object; a section that didn't
// change is the same object as before). Each push has an id: the stream's SSE id (EventSource sends it
// back when it reconnects, so a reconnect costs only what changed meanwhile) and X-State-Id on a GET,
// which the next poll, and the stream when it opens, send as ?since=.
"use strict";

const LIVE_POLL_MS = 2000;
const LIVE_STALL_MS = 5000;          // the server pushes every second: this long without one = stalled
const LIVE_PILL_MS = 1500;           // out of reach this long: the "Reconnecting…" pill (a blip shows nothing)
const LIVE_REOPEN_MS = [1000, 2000, 4000, 5000];   // a stream the browser gave up on (a 502 / 503 / 530): reopened

// The hub out of reach for a moment (#247: a deploy hands over from the old task to the new one in seconds;
// Cloudflare answers 502 / 530 meanwhile, the old task 503 + Retry-After): the page keeps what it shows, retries
// by itself, and says so in a small pill instead of an error. LiveConn.down(why) / LiveConn.up(): live.js's
// own stream and polls, and cad.js's api calls.
const LiveConn = (() => {
  let since = null, pill = null, timer = null;          // since: when it went out of reach (null: it is in reach)
  const TEXT = "Reconnecting\u2026 (update in progress)";
  function show() {
    if (since === null || Date.now() - since < LIVE_PILL_MS || typeof document === "undefined" || !document.body) return;
    if (!pill) {
      pill = document.createElement("div");
      pill.className = "pill reconnect-pill";
      pill.setAttribute("role", "status");
      pill.textContent = TEXT;
    }
    if (!pill.isConnected) document.body.append(pill);
  }
  function down() {
    if (since === null) since = Date.now();
    if (!timer) timer = setInterval(show, 500);
    show();
  }
  function up() {
    since = null;
    if (timer) { clearInterval(timer); timer = null; }
    if (pill && pill.isConnected) pill.remove();
  }
  // a failure that is the hub being out of reach (not a refusal of what was asked)
  const unreachable = (status) => status === 0 || status >= 500;
  return { down, up, unreachable, shown: () => !!(pill && pill.isConnected), text: TEXT };
})();
if (typeof window !== "undefined") window.LiveConn = LiveConn;

// liveMerge(have, msg): the whole state after msg (a whole state, or a delta onto have); null when
// msg is a delta and there is nothing to put it on (ask for the whole state again).
function liveMerge(have, msg) {
  if (!msg || !msg._delta) return msg;
  if (!have) return null;
  const out = Object.assign({}, have, msg);
  delete out._delta;
  delete out._gone;
  for (const k of msg._gone || []) delete out[k];
  return out;
}

function liveState(streamUrl, stateUrl, onState) {
  let lastAt = 0, poller = null, errors = 0, polls = 0, state = null, id = "", es = null, reopens = 0, reopenTimer = null;
  const got = (msg, newId) => {
    const next = liveMerge(state, msg);
    if (next === null) { id = ""; return; }                // a delta with nothing under it: ask for the whole state
    state = next;
    if (newId) id = newId;
    lastAt = Date.now();
    LiveConn.up();
    onState(state);
  };
  const poll = async () => {
    try {
      const r = await fetch(stateUrl + (id ? `?since=${encodeURIComponent(id)}` : ""), { cache: "no-store", credentials: "same-origin" });
      if (r.ok) { polls += 1; got(await r.json(), r.headers.get("X-State-Id") || ""); }
      else if (LiveConn.unreachable(r.status)) LiveConn.down(r.status);
    } catch (err) {
      LiveConn.down(0);                                     // the network, or Cloudflare's error page (no CORS)
      console.warn("state poll:", err);
    }
  };
  const startPolling = () => { if (!poller) poller = setInterval(poll, LIVE_POLL_MS); };
  const stopPolling = () => { if (poller) { clearInterval(poller); poller = null; } };
  const open = () => {
    reopenTimer = null;
    es = new EventSource(streamUrl + (id ? `?since=${encodeURIComponent(id)}` : ""));
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (err) { console.warn("bad state push:", err); return; }
      errors = 0;
      reopens = 0;
      stopPolling();
      got(data, ev.lastEventId || "");
    };
    es.onerror = () => {
      errors += 1;
      if (errors >= 2) startPolling();
      // EventSource retries a dropped stream by itself, but gives up for good on an answer that isn't the stream
      // (a 502 / 503 / 530 while a deploy hands over): open it again, soon.
      if (es.readyState === 2 && !reopenTimer) {
        startPolling();
        LiveConn.down(0);
        reopenTimer = setTimeout(open, LIVE_REOPEN_MS[Math.min(reopens++, LIVE_REOPEN_MS.length - 1)]);
      }
    };
  };

  // First paint from a GET without waiting for the stream; the stream then starts from what that had
  // (?since=: not the whole state a second time), or after 3 s whatever the GET is doing.
  Promise.race([poll(), new Promise((ok) => setTimeout(ok, 3000))]).then(open);
  setInterval(() => { if (Date.now() - lastAt > LIVE_STALL_MS) startPolling(); }, 1000);
  return { mode: () => (poller ? "polling" : "stream"), polls: () => polls };
}
