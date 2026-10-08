// Shared behaviour for the design system's components (style.css): the same feedback, dialogs and keys on
// every view and on the wall board.
//
//   UI.toast(text, kind, sub, ms)   short feedback, bottom right (kind: "ok" | "err" | "info"; role=status)
//   UI.confirm(text, {ok, danger})  a promise of true / false: every destructive action asks the same way
//   UI.busy(promise[, button])      the button that started a request shows busy (aria-busy) until it settles
//   UI.dialog.open(overlay, opts)   an .overlay's dialog: focus moves in, Tab stays inside, Esc and a click on
//   UI.dialog.close(overlay)        the scrim close it, the focus goes back where it was (opts: onClose, opener)
//   UI.keys(handlers)               the dispatcher's shortcuts (cad.js gives the handlers); "?" shows them
//
// Shortcuts never fire while typing in a field (Esc there leaves the field first), with a modifier held,
// or behind an open dialog.
"use strict";

window.UI = (() => {
  const TOAST_MS = 3500;

  function make(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  // ---- toast ---------------------------------------------------------------------------------------
  let toasts = null;
  function toast(text, kind = "ok", sub = "", ms = TOAST_MS) {
    if (!toasts) {
      toasts = make("div", "toasts");
      toasts.setAttribute("role", "status");
      toasts.setAttribute("aria-live", "polite");
      document.body.append(toasts);
    }
    const t = make("div", `toast ${kind}`);
    t.append(make("b", "", text));
    if (sub) t.append(make("span", "dim", sub));
    t.title = "Click to dismiss";
    t.addEventListener("click", () => t.remove());
    toasts.prepend(t);
    while (toasts.children.length > 4) toasts.lastChild.remove();
    setTimeout(() => t.remove(), kind === "err" ? Math.max(ms, 6000) : ms);
    return t;
  }

  // ---- busy buttons ----------------------------------------------------------------------------------
  let last = null;                       // the button pressed last (a click or a form's submitter)
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("button, .btn");
    if (b) last = { b, t: performance.now() };
  }, true);
  document.addEventListener("submit", (e) => {
    const b = e.submitter || (e.target.querySelector && e.target.querySelector("[type=submit]"));
    if (b) last = { b, t: performance.now() };
  }, true);
  function busy(promise, button) {
    const b = button || (last && performance.now() - last.t < 800 ? last.b : null);
    if (!b || !b.isConnected || !promise || typeof promise.then !== "function") return promise;
    b.setAttribute("aria-busy", "true");
    const done = () => b.removeAttribute("aria-busy");
    promise.then(done, done);
    return promise;
  }

  // ---- dialogs: focus in, kept in, back out ------------------------------------------------------------
  const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), "
    + "textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])";
  const stack = [];
  const focusables = (root) => [...root.querySelectorAll(FOCUSABLE)].filter((n) => n.getClientRects().length && !n.closest("[hidden]"));
  const panelOf = (ov) => ov.querySelector("[role=dialog], [role=alertdialog]") || ov;

  function open(ov, opts = {}) {
    if (stack.some((r) => r.el === ov)) return;
    const rec = { el: ov, opener: opts.opener || document.activeElement, onClose: opts.onClose || null };
    stack.push(rec);
    ov.hidden = false;
    document.body.classList.add("modal");
    if (!ov._uiScrim) {                   // a click on the scrim (not the panel) closes
      ov._uiScrim = true;
      ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(ov); });
    }
    const panel = panelOf(ov);
    const first = opts.focus || panel.querySelector("[autofocus]") || focusables(panel)[0] || panel;
    if (first === panel && !panel.hasAttribute("tabindex")) panel.tabIndex = -1;
    requestAnimationFrame(() => { if (!ov.hidden) first.focus({ preventScroll: true }); });
  }
  function close(ov) {
    const i = stack.findIndex((r) => r.el === ov);
    ov.hidden = true;
    if (i < 0) return;
    const [rec] = stack.splice(i, 1);
    if (!stack.length) document.body.classList.remove("modal");
    if (rec.opener && rec.opener.isConnected && rec.opener.focus) rec.opener.focus({ preventScroll: true });
    if (rec.onClose) rec.onClose();
  }
  const isOpen = () => stack.length > 0;
  document.addEventListener("keydown", (e) => {
    const top = stack[stack.length - 1];
    if (!top) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopImmediatePropagation();
      close(top.el);
    } else if (e.key === "Tab") {         // keep the focus inside the dialog
      const f = focusables(panelOf(top.el));
      if (!f.length) { e.preventDefault(); return; }
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && (i === -1 || i === f.length - 1)) { e.preventDefault(); f[0].focus(); }
    }
  }, true);

  // ---- confirm: the destructive-action question ------------------------------------------------------
  function confirm(text, { ok = "OK", cancel = "Cancel", danger = true, title = "" } = {}) {
    return new Promise((resolve) => {
      let answer = false;
      const ov = make("div", "overlay");
      const panel = make("div", "dialog narrow");
      panel.setAttribute("role", "alertdialog");
      panel.setAttribute("aria-modal", "true");
      const id = `ui-confirm-${Date.now()}`;
      panel.setAttribute("aria-describedby", id);
      if (title) {
        const head = make("div", "dialog-head");
        head.append(make("h2", "dialog-title", title));
        panel.append(head);
      }
      const body = make("div", "dialog-body");
      const p = make("p", "", text);
      p.id = id;
      body.append(p);
      const foot = make("div", "dialog-foot");
      const no = make("button", "", cancel);
      no.type = "button";
      const yes = make("button", danger ? "danger" : "primary", ok);
      yes.type = "button";
      foot.append(no, yes);
      panel.append(body, foot);
      ov.append(panel);
      document.body.append(ov);
      no.addEventListener("click", () => close(ov));
      yes.addEventListener("click", () => { answer = true; close(ov); });
      open(ov, { focus: danger ? no : yes, onClose: () => { ov.remove(); resolve(answer); } });
    });
  }

  // ---- keyboard shortcuts ----------------------------------------------------------------------------
  const SHEET = [
    [["N"], "New incident"],
    [["/"], "Search (the Radio view, or this page's filter)"],
    [["G", "D"], "Go to Dispatch"],
    [["G", "O"], "Go to Overview"],
    [["G", "R"], "Go to Radio"],
    [["G", "S"], "Go to Settings"],
    [["J"], "Next incident in the queue"],
    [["K"], "Previous incident in the queue"],
    [["Esc"], "Close or cancel; leave a field"],
    [["?"], "This sheet"],
  ];
  const BOARD_SHEET = [[["F"], "Full screen"], [["?"], "This sheet"]];
  let handlers = {}, prefix = null, prefixAt = 0, sheetOv = null;
  const typing = (n) => !!(n && n.closest && n.closest("input, textarea, select, [contenteditable=''], [contenteditable=true]"));

  function sheet() {
    if (sheetOv && !sheetOv.hidden) { close(sheetOv); return; }
    const rows = handlers.board ? BOARD_SHEET : SHEET;
    if (!sheetOv) {
      sheetOv = make("div", "overlay");
      sheetOv.id = "shortcuts";
      const panel = make("div", "dialog narrow");
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-labelledby", "shortcuts-title");
      const head = make("div", "dialog-head");
      const h = make("h2", "dialog-title", "Keyboard shortcuts");
      h.id = "shortcuts-title";
      const x = make("button", "icon quiet");
      x.type = "button";
      x.setAttribute("aria-label", "Close");
      x.title = "Close (Esc)";
      x.append(window.Icon ? Icon("x") : "✕");
      x.addEventListener("click", () => close(sheetOv));
      head.append(h, make("span", "spacer"), x);
      const dl = make("dl", "sg-shortcuts");
      for (const [keys, what] of rows) {
        const dt = make("dt");
        keys.forEach((k, i) => { if (i) dt.append(make("span", "dim small", "then")); dt.append(make("kbd", "", k)); });
        dl.append(dt, make("dd", "", what));
      }
      const body = make("div", "dialog-body");
      body.append(dl, make("p", "dim small", "Not while typing in a field: Esc leaves the field first."));
      panel.append(head, body);
      sheetOv.append(panel);
      document.body.append(sheetOv);
    }
    open(sheetOv);
  }

  function onKey(e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape" && typing(e.target)) {          // leave the field (a suggestion list closes itself first)
      if (!isOpen()) { e.target.blur(); if (handlers.escape) handlers.escape(true); }
      return;
    }
    if (typing(e.target)) return;
    if (e.key === "?") { e.preventDefault(); sheet(); return; }
    if (isOpen()) return;
    if (e.key === "Escape") { if (handlers.escape) handlers.escape(false); return; }
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (prefix === "g" && performance.now() - prefixAt < 1500) {
      prefix = null;
      const fn = handlers[`g${k}`];
      if (fn) { e.preventDefault(); fn(); }
      return;
    }
    prefix = null;
    if (k === "g" && !handlers.board) { prefix = "g"; prefixAt = performance.now(); return; }
    const fn = handlers[k];
    if (fn) { e.preventDefault(); fn(e); }
  }
  document.addEventListener("keydown", onKey);
  function keys(h) { handlers = { ...handlers, ...h }; }

  return { toast, confirm, busy, dialog: { open, close, isOpen }, keys, sheet };
})();
