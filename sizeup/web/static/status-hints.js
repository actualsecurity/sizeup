/* Unit statuses suggested from the radio (GitHub #37; sizeup/cad/statushints.py): the state's status_hints, shown
   where the unit is (its row on the units board, its row in the event's Assigned panel) as a chip
   "E64 on location? ✓ ✕" with what was heard on hover, and one line under the transmission that said it
   ("Suggests E64 on location"). ✓ sets the status (dispatchers and admins; the chronology says it came from the radio),
   ✕ dismisses it. A viewer, and the wall board, see the suggestion and nothing to press. Nothing changes on its own.
   Drawn after every render (cad.hooks) over what cad.js drew: cad.js only marks each transmission row (data-tx). */
(function () {
  "use strict";
  const busy = new Set();                  // suggestions being accepted / dismissed: not offered twice

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  const hasCad = () => typeof cad !== "undefined";          // cad.js's console state (a const: not on window)
  const all = () => ((hasCad() && cad.state && cad.state.status_hints) || []).filter((h) => !busy.has(h.id));
  const board = () => typeof BOARD !== "undefined" && BOARD;
  const mayTake = () => !board() && (typeof canDo !== "function" || canDo("dispatch"));
  const hms = (t) => (typeof fmtTime === "function" ? fmtTime(t, true) : new Date(t * 1000).toLocaleTimeString());
  const label = (h) => `${h.unit} ${h.status_name}`;
  const onto = (h) => (h.onto && h.incident_number ? ` on ${h.incident_number}` : "");

  function heard(h) {
    return `Heard ${hms(h.heard_at)} on ${h.channel}${h.who === "talker" ? ` from ${h.unit}'s radio` : ""}: “${h.text}”`
      + (h.onto ? `\nAccepting puts ${h.unit} on ${h.incident_number || "that event"}` : "");
  }

  async function decide(h, action) {
    busy.add(h.id);
    draw();
    try {
      await api("POST", `/api/cad/hint/${h.id}/${action}`, { operator: typeof operator === "function" ? operator() : "" });
      if (action === "accept") UI.toast(`${label(h)}${onto(h)}`, "ok", "From the radio");
    } catch (e) {
      busy.delete(h.id);
      UI.toast(e.message, "err");
      draw();
    }
  }

  function chip(h) {
    const c = el("span", "hint-chip");
    c.dataset.hint = `${h.id}|${mayTake() ? 1 : 0}`;
    c.setAttribute("role", "group");
    c.setAttribute("aria-label", `Suggested from the radio: ${label(h)}${onto(h)}`);
    c.title = heard(h);
    // the unit's name is right before it on its row: the question alone ("On location?")
    const q = `${h.status_name}${onto(h)}?`;
    c.append(el("span", "hint-q", q.charAt(0).toUpperCase() + q.slice(1)));
    const play = el("button", "mini icon quiet");
    if (window.Icon && Icon.play) Icon.play(play); else play.textContent = "▶";
    play.type = "button";
    play.title = `Play: ${heard(h)}`;
    play.setAttribute("aria-label", `Play what was heard at ${hms(h.heard_at)}`);
    play.addEventListener("click", (e) => { e.stopPropagation(); if (typeof clipUrl === "function") window.play(clipUrl({ id: h.traffic_id }, false)); });
    c.append(play);
    if (mayTake()) {
      const ok = el("button", "mini hint-ok", "✓");
      ok.type = "button";
      ok.title = `Accept: ${label(h)}${onto(h)}\n${heard(h)}`;
      ok.setAttribute("aria-label", `Accept ${label(h)}${onto(h)}, heard on the radio at ${hms(h.heard_at)}: ${h.said}`);
      ok.addEventListener("click", (e) => { e.stopPropagation(); decide(h, "accept"); });
      const no = el("button", "mini hint-no", "✕");
      no.type = "button";
      no.title = "Dismiss: not so";
      no.setAttribute("aria-label", `Dismiss ${label(h)}`);
      no.addEventListener("click", (e) => { e.stopPropagation(); decide(h, "dismiss"); });
      c.append(ok, no);
    }
    return c;
  }

  // The chip after the unit's name in a row's head; replaced only when the suggestion (or who may take it) changes,
  // so a focused ✓ keeps its focus across the state push.
  function place(head, h) {
    if (!head) return;
    const have = head.querySelector(":scope > .hint-chip");
    const want = h ? `${h.id}|${mayTake() ? 1 : 0}` : "";
    if (have && have.dataset.hint === want) return;
    if (have) have.remove();
    if (!h) return;
    const name = head.querySelector(":scope > b");
    head.insertBefore(chip(h), name ? name.nextSibling : head.firstChild);
  }

  function draw() {
    const hs = all();
    const byUnit = new Map(), byTx = new Map();
    for (const h of hs) {
      if (!byUnit.has(h.unit)) byUnit.set(h.unit, h);                  // newest first: one per unit
      if (!byTx.has(h.traffic_id)) byTx.set(h.traffic_id, []);
      byTx.get(h.traffic_id).push(h);
    }
    // the units board: li.urow keyed by unit id
    for (const li of document.querySelectorAll("#units li.urow")) {
      const name = li.querySelector(".uh > b.un");
      place(li.querySelector(".uh"), name ? byUnit.get(name.textContent) : null);
    }
    // the event's Assigned panel: only a suggestion for this event (or one with no event)
    const inc = typeof selected === "function" ? selected() : null;
    for (const li of document.querySelectorAll("#d-units li.ou")) {
      const head = li.querySelector(".ouh"), name = head && head.querySelector(":scope > b");
      const h = name && !li.classList.contains("cleared") ? byUnit.get(name.textContent) : null;
      place(head, h && inc && (!h.incident_id || h.incident_id === inc.id) ? h : null);
    }
    // the transmission that said it: the feed, the event's radio list, the Radio view
    for (const meta of document.querySelectorAll(".meta[data-tx]")) {
      const li = meta.parentElement;
      const mine = byTx.get(meta.dataset.tx) || [];
      const sig = mine.map((h) => h.id).join(",");
      let line = li.querySelector(":scope > .hint-line");
      if ((line ? line.dataset.sig : "") === sig) continue;
      if (line) line.remove();
      if (!sig) continue;
      line = el("div", "hint-line small", `Suggests ${mine.map((h) => `${label(h)}${onto(h)}`).join(", ")}`);
      line.dataset.sig = sig;
      line.title = "A suggestion from what was heard: accept it on the unit (units board, Assigned)";
      li.append(line);
    }
  }

  window.StatusHints = { draw };
  if (hasCad() && Array.isArray(cad.hooks)) cad.hooks.push(draw);
})();
