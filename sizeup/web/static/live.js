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
  let lastAt = 0, poller = null, errors = 0, polls = 0, state = null, id = "";
  const got = (msg, newId) => {
    const next = liveMerge(state, msg);
    if (next === null) { id = ""; return; }                // a delta with nothing under it: ask for the whole state
    state = next;
    if (newId) id = newId;
    lastAt = Date.now();
    onState(state);
  };
  const poll = async () => {
    try {
      const r = await fetch(stateUrl + (id ? `?since=${encodeURIComponent(id)}` : ""), { cache: "no-store", credentials: "same-origin" });
      if (r.ok) { polls += 1; got(await r.json(), r.headers.get("X-State-Id") || ""); }
    } catch (err) {
      console.warn("state poll:", err);
    }
  };
  const startPolling = () => { if (!poller) poller = setInterval(poll, LIVE_POLL_MS); };
  const stopPolling = () => { if (poller) { clearInterval(poller); poller = null; } };

  // First paint from a GET without waiting for the stream; the stream then starts from what that had
  // (?since=: not the whole state a second time), or after 3 s whatever the GET is doing.
  Promise.race([poll(), new Promise((ok) => setTimeout(ok, 3000))]).then(() => {
    const es = new EventSource(streamUrl + (id ? `?since=${encodeURIComponent(id)}` : ""));
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (err) { console.warn("bad state push:", err); return; }
      errors = 0;
      stopPolling();
      got(data, ev.lastEventId || "");
    };
    es.onerror = () => { errors += 1; if (errors >= 2) startPolling(); };   // EventSource retries by itself
  });
  setInterval(() => { if (Date.now() - lastAt > LIVE_STALL_MS) startPolling(); }, 1000);
  return { mode: () => (poller ? "polling" : "stream"), polls: () => polls };
}
