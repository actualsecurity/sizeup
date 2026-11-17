/* Out of service is a person's to end (sizeup/cad/oos.py): the state's oos_notices, what automation was refused on an
   out-of-service unit ("radio heard “responding”"), shown under the unit's row of the units board as a chip
   "Radio heard “responding” · In service? ✓ ✕" with the whole notice on hover (a status suggestion's chip is left
   off that row: status-hints.js). ✓ puts the unit in service (its free status;
   dispatchers and admins), ✕ leaves it out of service. A viewer, and the wall board, see it and nothing to press.
   Drawn after every render (cad.hooks) over what cad.js drew. */
(function () {
  "use strict";
  const busy = new Set();                  // notices being acted on: not offered twice

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  const hasCad = () => typeof cad !== "undefined";
  const all = () => ((hasCad() && cad.state && cad.state.oos_notices) || []).filter((n) => !busy.has(n.id));
  const board = () => typeof BOARD !== "undefined" && BOARD;
  const mayTake = () => !board() && (typeof canDo !== "function" || canDo("dispatch"));
  const hms = (t) => (typeof fmtTime === "function" ? fmtTime(t, true) : new Date(t * 1000).toLocaleTimeString());
  const short = (n) => n.what.charAt(0).toUpperCase() + n.what.slice(1);    // "Radio heard “responding”"

  function full(n) {
    return `${n.unit} ${n.status_name}: ${n.what} ${hms(n.last_at)}${n.count > 1 ? ` (${n.count}×)` : ""}`
      + (n.incident_number ? ` · ${n.incident_number}` : "") + "\nNot changed: automation never ends out of service";
  }

  async function act(n, action) {
    busy.add(n.id);
    draw();
    try {
      await api("POST", `/api/cad/oos-notice/${n.id}/${action}`, { operator: typeof operator === "function" ? operator() : "" });
      if (action === "service") UI.toast(`${n.unit} in service`, "ok");
    } catch (e) {
      busy.delete(n.id);
      UI.toast(e.message, "err");
      draw();
    }
  }

  function chip(n) {
    const c = el("span", "hint-chip oos-chip");
    c.setAttribute("role", "group");
    c.setAttribute("aria-label", `${n.unit} ${n.status_name}: ${n.what}`);
    c.title = full(n);
    c.append(el("span", "hint-q", `${short(n)} · In service?`));
    if (n.traffic_id) {
      const play = el("button", "mini icon quiet");
      if (window.Icon && Icon.play) Icon.play(play); else play.textContent = "▶";
      play.type = "button";
      play.title = `Play: ${n.what}`;
      play.setAttribute("aria-label", `Play what was heard at ${hms(n.last_at)}`);
      play.addEventListener("click", (e) => { e.stopPropagation(); if (typeof clipUrl === "function") window.play(clipUrl({ id: n.traffic_id }, false)); });
      c.append(play);
    }
    if (mayTake()) {
      const ok = el("button", "mini hint-ok", "✓");
      ok.type = "button";
      ok.title = `${n.unit} in service`;
      ok.setAttribute("aria-label", `Put ${n.unit} in service`);
      ok.addEventListener("click", (e) => { e.stopPropagation(); act(n, "service"); });
      const no = el("button", "mini hint-no", "✕");
      no.type = "button";
      no.title = `Keep ${n.unit} ${n.status_name}`;
      no.setAttribute("aria-label", `Keep ${n.unit} ${n.status_name}`);
      no.addEventListener("click", (e) => { e.stopPropagation(); act(n, "dismiss"); });
      c.append(ok, no);
    }
    return c;
  }

  // its own line at the foot of the row; replaced only when the notice changes, so a focused ✓ keeps its focus across
  // the state push (cad.js redraws a row only when the unit changes: the line is put back after)
  function place(li, n) {
    const have = li.querySelector(":scope > .oos-line");
    const want = n ? `${n.id}|${n.count}|${mayTake() ? 1 : 0}` : "";
    if (have && have.dataset.oos === want) return;
    if (have) have.remove();
    if (!n) return;
    const line = el("div", "oos-line");
    line.dataset.oos = want;
    line.append(chip(n));
    const buttons = li.querySelector(":scope > .ubtns");               // above the open row's status buttons
    li.insertBefore(line, buttons);
  }

  function draw() {
    const byUnit = new Map();
    for (const n of all()) if (!byUnit.has(n.unit)) byUnit.set(n.unit, n);     // newest first: one per unit
    for (const li of document.querySelectorAll("#units li.urow")) {
      const name = li.querySelector(".uh > b.un");
      place(li, name ? byUnit.get(name.textContent) : null);
    }
  }

  // the units with a notice shown now (status-hints.js draws no chip on their board row)
  const units = () => new Set(all().map((n) => n.unit));

  window.OosNotices = { draw, units };
  if (hasCad() && Array.isArray(cad.hooks)) cad.hooks.push(draw);
})();
