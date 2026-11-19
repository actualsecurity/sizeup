// The incident's Size-Up view (GitHub #797): Details · Size-Up in the incident's head (cad.html #d-tabs).
//
//   Details      today's view, unchanged; every selectIncident() opens it (cad.tab = "details").
//   Size-Up      a two-column grid (#d-size) built from the detail's own nodes, MOVED into its slots, never copied: each
//                module (addr-history.js, preplan.js, preplan-docs.js, roadprojects.js, trains.js, vessels.js, maplayers.js,
//                weather.js, cadmap.js) keeps writing into the same ids with its own fetches and timers.
//   Moving       each node leaves a comment where it was (park); leaving Size-Up puts them back in reverse order (unpark),
//                so a node moved out of one already moved (the comments out of the info column) goes home first: exact.
//   Re-applied   a cad.hook, after every render; it moves only when the view changes. A new selection is Details again.
//   Empty        a slot with nothing shown says so (.sz-none), kept up by a MutationObserver: modules flip `hidden` late.
//   The map      its drag handles size Details' grid only (incident-map.js): hidden here, the layout's height.
//   History      folded each time Size-Up opens, not remembered (AddrHistory.refold).
//   Narrow       one column in the #797 order (cad.css: Incident, Address notes, Map, Hazards, Pre-plan, Building,
//                Street View, History).
"use strict";

window.SizeUp = (() => {
  // [what, slot]: in this order in, the reverse out. A string is an id; "info" and "assigned" have none (found once).
  const PLAN = [
    ["info", "sz-incident"], ["assigned", "sz-incident"], ["d-cmts", "sz-incident"],
    ["d-preplan-form", "sz-preplan"], ["d-preplan", "sz-preplan"], ["d-docs-form", "sz-preplan"], ["d-docs", "sz-preplan"],
    ["d-roads", "sz-hazards"], ["d-trains", "sz-hazards"], ["d-vessels", "sz-hazards"], ["d-cams", "sz-hazards"],
    ["d-tides", "sz-hazards"], ["d-airport", "sz-hazards"],
    ["d-mapbox", "sz-map"],
    ["d-premise", "sz-notes"], ["d-premise-form", "sz-notes"],
    ["d-here", "sz-history"],
  ];

  // node into slot, a comment left where it was; returns that mark
  function park(node, slot) {
    const mark = node.ownerDocument.createComment(`sz:${node.id || "node"}`);
    node.parentNode.insertBefore(mark, node);
    slot.appendChild(node);
    return mark;
  }
  function unpark(node, mark) {
    mark.parentNode.insertBefore(node, mark);
    mark.parentNode.removeChild(mark);
  }
  // items: [[node, slot]] -> parked [[node, mark]]; moveOut puts them back, last first
  const moveIn = (items) => items.map(([node, slot]) => [node, park(node, slot)]);
  function moveOut(parked) {
    for (let i = parked.length - 1; i >= 0; i--) unpark(parked[i][0], parked[i][1]);
  }

  const api = { PLAN, park, unpark, moveIn, moveOut, on: () => false, tab: () => "details", show() {}, apply() {} };
  if (typeof document === "undefined" || !document.getElementById("d-size") || typeof cad === "undefined") return api;

  const $id = (id) => document.getElementById(id);
  const grid = $id("d-grid"), size = $id("d-size");
  const named = { info: grid.firstElementChild, assigned: $id("d-units").closest(".card") };
  const items = PLAN.map(([what, slot]) => [named[what] || $id(what), $id(slot)]);
  const buttons = [...document.querySelectorAll("#d-tabs button")];
  let parked = null, lastSel = null;

  function wanted() {
    return !BOARD && cad.tab === "sizeup" && cad.mode === "view" && !cad.radio && !cad.search && !cad.pinfo && !!selected();
  }
  function empties() {
    for (const slot of size.querySelectorAll(".sz-slot")) {
      const none = slot.querySelector(".sz-none");
      if (!none) continue;
      const moved = [...slot.children].filter((c) => c !== none && !c.classList.contains("sz-head"));
      if (!moved.length) continue;
      const empty = moved.every((c) => c.hidden);
      show(none, empty);
      // a slot whose cards head themselves (Pre-plan: "Pre-plan", "Site docs"): its own head only over "None on file"
      const head = slot.querySelector(".sz-head");
      if (head && "ownHead" in slot.dataset) show(head, empty);
    }
  }
  function apply() {
    if (cad.sel !== lastSel) { lastSel = cad.sel; cad.tab = "details"; }    // a new selection: Details first
    const on = wanted();
    for (const b of buttons) b.setAttribute("aria-pressed", String(b.dataset.tab === (on ? "sizeup" : "details")));
    if (on === !!parked) return;
    if (on) parked = moveIn(items);
    else { moveOut(parked); parked = null; }
    show(grid, !on);
    show(size, on);
    empties();
    if (window.AddrHistory && AddrHistory.refold) AddrHistory.refold();
    requestAnimationFrame(() => {
      if (window.Panels) Panels.resizeMaps();
      if (window.IncidentMap && IncidentMap.apply) IncidentMap.apply();
    });
  }
  function choose(tab) {
    cad.tab = tab === "sizeup" ? "sizeup" : "details";
    apply();
  }

  buttons.forEach((b, i) => {
    b.addEventListener("click", () => choose(b.dataset.tab));
    b.addEventListener("keydown", (e) => {
      const d = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
      if (!d) return;
      e.preventDefault();
      const n = buttons[(i + d + buttons.length) % buttons.length];
      n.focus();
      choose(n.dataset.tab);
    });
  });
  new MutationObserver(empties).observe(size, { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden"] });
  cad.hooks.push(apply);

  return { ...api, apply, on: () => !!parked, tab: () => (parked ? "sizeup" : "details"), show: (on) => choose(on ? "sizeup" : "details") };
})();
