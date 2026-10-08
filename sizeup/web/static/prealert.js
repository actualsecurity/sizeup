// The phone pre-alert (sizeup/prealert.py): Tapout heard the PCFD headquarters phone ring, a
// possible incoming call. The server's state carries "prealert": {open, items, tapout, expire_s}
// (null where there is none: [prealert] off, or a station forwarding to a hub).
//
//   PreAlert.column(list, health, pa, deps)  the console: the open ones at the top of the 60 Control column,
//                                            Tapout's health in its header; the wall board (/monitor): above the
//                                            60 Control feed, no Dismiss; deps: {api, operator, time, readonly}
//   PreAlert.badge(node, pa)                 the header (console and board): "Phone ringing 0:42"
//   PreAlert.notify(pa)                      a toast and the chime per new ring (radio.js alert settings)
//
// The ages tick here (every [data-pa-at]), on the server's clock.
"use strict";

window.PreAlert = (() => {
  const st = { skew: 0, seen: null, busy: new Set() };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function age(at) {
    const s = Math.max(0, Math.floor(Date.now() / 1000 + st.skew - at));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  }
  function ticking(node, at) {
    node.dataset.paAt = String(at);
    node.textContent = age(at);
    return node;
  }
  function clock(pa) {
    if (pa && typeof pa.time === "number") st.skew = pa.time - Date.now() / 1000;
  }
  setInterval(() => {
    for (const n of document.querySelectorAll("[data-pa-at]")) n.textContent = age(Number(n.dataset.paAt));
  }, 1000);

  const open = (pa) => (pa && pa.open) || [];

  function column(list, health, pa, deps) {
    clock(pa);
    if (health) {
      health.hidden = !pa;
      if (pa) {
        const t = pa.tapout || {};
        health.className = `tapout ${t.state || "unknown"}`;
        health.textContent = `Tapout: ${t.word || "no heartbeat yet"}`;
        health.title = [`Phone pre-alert: ${pa.source}, read from ntfy`, t.detail].filter(Boolean).join("\n");
      }
    }
    const items = open(pa);
    list.hidden = !items.length;
    const sig = JSON.stringify(items.map((p) => [p.id, p.test, st.busy.has(p.id)]));
    if (list._sig === sig) return;
    list._sig = sig;
    list.replaceChildren(...items.map((p) => row(p, deps)));
  }

  function row(p, deps) {
    const li = el("li", "pa" + (p.test ? " test" : ""));
    li.dataset.key = String(p.id);
    li.title = [p.title, p.message, `ntfy ${p.ntfy_id}`].filter(Boolean).join("\n");
    if (window.Icon) li.append(Icon("bell"));
    const what = el("span", "pa-what");
    what.append(el("b", "pa-t", p.test ? "Test ring" : "Phone ringing"),
                el("span", "pa-src", `${p.source} · ${deps.time(p.at)}`));
    li.append(what, ticking(el("span", "pa-age"), p.at));
    if (deps.readonly) return li;
    const b = el("button", "mini pa-dismiss", st.busy.has(p.id) ? "…" : "Dismiss");
    b.type = "button";
    b.disabled = st.busy.has(p.id);
    b.title = "Not a call: take it off the list (logged)";
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      st.busy.add(p.id);
      b.disabled = true;
      try {
        await deps.api("POST", `/api/cad/prealert/${p.id}/dismiss`, { operator: deps.operator() });
      } catch (err) {
        alert(`Dismiss failed: ${err.message}`);
      } finally {
        st.busy.delete(p.id);
      }
    });
    li.append(b);
    return li;
  }

  function badge(node, pa) {
    if (!node) return;
    clock(pa);
    const items = open(pa).filter((p) => !p.test);
    node.hidden = !items.length;
    const sig = JSON.stringify(items.map((p) => p.id));
    if (node._sig === sig) return;
    node._sig = sig;
    if (!items.length) { node.replaceChildren(); return; }
    const first = items[items.length - 1];                 // the oldest open one
    node.replaceChildren(el("b", "", items.length > 1 ? `Phone ringing ×${items.length}` : "Phone ringing"), " ",
                         ticking(el("span", "pa-age"), first.at));
    node.title = `${first.source}: ${first.message || first.title}`;
  }

  function notify(pa) {
    const items = open(pa);
    const first = st.seen === null;
    if (first) st.seen = new Set();
    for (const p of items) {
      if (st.seen.has(p.id)) continue;
      st.seen.add(p.id);
      if (!first && !p.test && window.Radio && Radio.prealert) Radio.prealert("Phone ringing", `${p.source}: possible incoming call`);
    }
    if (st.seen.size > 200) st.seen = new Set(items.map((p) => p.id));
  }

  return { column, badge, notify };
})();
