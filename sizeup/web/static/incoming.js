// Incident sources (#297, sizeup/cad/sources.py). The state push carries "sources": {tiers, acting, feeds, incoming}.
//
//   Incoming.pill(node, s)          the header: the primary feed's health and the acting primary when it isn't it
//                                   ("County feed · backing off · Paging primary"); hidden with no feed
//   Incoming.column(list, s, deps)  "Possible incoming" at the top of the 60 Control column (the board: above its feed):
//                                   radio pre-alerts and reports held for a higher tier. Never an incident
//   Incoming.notify(s)              the pre-alert chime per new notice (radio.js alert settings)
"use strict";

window.Incoming = (() => {
  const st = { seen: null };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const word = (s) => String(s || "").replace(/_/g, " ");

  function pill(node, s) {
    if (!node) return;
    const tiers = (s && s.tiers) || [];
    const first = tiers[0];
    node.hidden = !s || !s.feeds || !first;
    if (node.hidden) return;
    const acting = tiers.find((t) => t.acting);
    const bad = !first.acting;
    const text = [first.label, word(first.state), bad ? (acting ? `${acting.label} primary` : "no source") : ""].filter(Boolean).join(" · ");
    if (node.textContent !== text) node.textContent = text;
    node.className = `pill ${bad ? "err" : "ok"}`;
    node.title = tiers.map((t) => `${t.rank >= 1000 ? "backup" : `tier ${t.rank}`}: ${t.label} · ${word(t.state)}`
      + (t.reason ? ` (${t.reason})` : "") + (t.acting ? " · primary" : "")).join("\n");
  }

  function column(list, s, deps) {
    if (!list) return;
    const items = (s && s.incoming) || [];
    list.hidden = !items.length;
    const sig = JSON.stringify(items.map((x) => [x.id, x.text, x.until]));
    if (list._sig === sig) return;
    list._sig = sig;
    list.replaceChildren(...items.map((x) => {
      const li = el("li", "pa");
      li.dataset.key = x.id;
      li.title = x.kind === "held" ? `${x.source}: held for a higher tier until ${deps.time(x.until)}` : `${x.source}: ${x.text}`;
      if (window.Icon) li.append(Icon("bell"));
      const what = el("span", "pa-what");
      what.append(el("b", "pa-t", "Possible incoming"), el("span", "pa-src", `${x.source} · ${deps.time(x.at)} · ${x.text}`));
      const age = el("span", "pa-age");
      age.dataset.paAt = String(x.at);         // prealert.js ticks every [data-pa-at]
      li.append(what, age);
      return li;
    }));
  }

  function notify(s) {
    const items = (s && s.incoming) || [];
    const first = st.seen === null;
    if (first) st.seen = new Set();
    for (const x of items) {
      if (st.seen.has(x.id)) continue;
      st.seen.add(x.id);
      if (!first && window.Radio && Radio.prealert) Radio.prealert("Possible incoming", `${x.source}: ${x.text}`);
    }
    if (st.seen.size > 200) st.seen = new Set(items.map((x) => x.id));
  }

  return { pill, column, notify };
})();
