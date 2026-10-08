// Live state for a page: the server-sent event stream when it works, polling when it doesn't.
// Some networks cut or hold event streams: an ECC workstation behind browser isolation (2026-10-07)
// sat on "connecting…" with empty lists while the hub saw each /api/cad/stream cancelled after a
// few seconds. So the page paints from a plain GET first, then follows the stream; if the stream
// errors or goes quiet it polls the same state every POLL_MS until the stream delivers again.
"use strict";

const LIVE_POLL_MS = 2000;
const LIVE_STALL_MS = 5000;          // the server pushes every second: this long without one = stalled

function liveState(streamUrl, stateUrl, onState) {
  let lastAt = 0, poller = null, errors = 0, polls = 0;
  const got = (data) => { lastAt = Date.now(); onState(data); };
  const poll = async () => {
    try {
      const r = await fetch(stateUrl, { cache: "no-store", credentials: "same-origin" });
      if (r.ok) { polls += 1; got(await r.json()); }
    } catch (err) {
      console.warn("state poll:", err);
    }
  };
  const startPolling = () => { if (!poller) poller = setInterval(poll, LIVE_POLL_MS); };
  const stopPolling = () => { if (poller) { clearInterval(poller); poller = null; } };

  poll();                                        // first paint without waiting for the stream
  const es = new EventSource(streamUrl);
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (err) { console.warn("bad state push:", err); return; }
    errors = 0;
    stopPolling();
    got(data);
  };
  es.onerror = () => { errors += 1; if (errors >= 2) startPolling(); };   // EventSource retries by itself
  setInterval(() => { if (Date.now() - lastAt > LIVE_STALL_MS) startPolling(); }, 1000);
  return { mode: () => (poller ? "polling" : "stream"), polls: () => polls };
}
