// Shared behaviour for the design system's components (style.css): the same feedback, dialogs and keys on
// every view and on the wall board.
//
//   UI.toast(text, kind, sub, ms)   short feedback, bottom right (kind: "ok" | "err" | "info"; role=status)
//   UI.confirm(text, {ok, danger})  a promise of true / false: every destructive action asks the same way
//   UI.busy(promise[, button])      the button that started a request shows busy (aria-busy) until it settles
//   UI.dialog.open(overlay, opts)   an .overlay's dialog: focus moves in, Tab stays inside, Esc and a click on
//   UI.dialog.close(overlay)        the scrim close it, the focus goes back where it was (opts: onClose, opener)
//   UI.keys(handlers)               the dispatcher's shortcuts (cad.js gives the handlers); "?" shows them
//   UI.menu.button(label, items, {key})   a "⋯" button (aria-haspopup=menu) whose menu lists items() (asked each
//                                   time it opens): [{label, run, disabled, checked, hint, danger, sub: () => items}, "-"]
//                                   ("-": a line; sub: "▸", a second page in the same popover, with Back)
//   UI.menu.close()                 closes the open one
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

  // ---- menu: a "⋯" button and its actions (a transmission's: cad.js txMenu) --------------------------------
  // Mouse: a click opens and closes it, a click anywhere else (or a scroll, a resize) closes it. Keys on the
  // button: Enter / Space / Down open it on the first item, Up on the last. In it: Up / Down (wrapping),
  // Home / End, Enter / Space choose, Right (or Enter) opens a "▸" page, Left / Backspace go back, Esc and Tab
  // close it and put the focus back on the button. Items are role=menuitem (menuitemcheckbox when checked
  // is given), one tab stop: the menu holds the focus while open. key: the button's identity across redraws
  // (a row redrawn while its menu is open: the focus goes back to the new button).
  const M = { el: null, btn: null, key: "", pages: [], items: [] };
  function menuButton(label, items, { key = "", cls = "" } = {}) {
    const b = make("button", `menu-btn sm icon quiet${cls ? ` ${cls}` : ""}`);
    b.type = "button";
    b.setAttribute("aria-haspopup", "menu");
    b.setAttribute("aria-expanded", "false");
    b.setAttribute("aria-label", label);
    b.title = label;
    if (key) b.dataset.menuKey = key;
    b.append(window.Icon ? Icon("more") : "⋯");
    b._menu = items;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      if (M.el && (M.btn === b || (key && M.key === key))) menuClose(false);
      else menuOpen(b, 0);
    });
    b.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); menuOpen(b, e.key === "ArrowUp" ? -1 : 0); }
    });
    return b;
  }
  function menuOpen(b, at) {
    menuClose(false);
    const el = make("div", "menu popover");
    el.id = "ui-menu";
    el.setAttribute("role", "menu");
    el.setAttribute("aria-label", b.getAttribute("aria-label") || "Actions");
    el.addEventListener("click", (e) => {
      const n = e.target.closest(".menu-item");
      if (n) { e.stopPropagation(); choose(M.items[Number(n.dataset.i)]); }
    });
    (b.closest(".overlay") || document.body).append(el);
    Object.assign(M, { el, btn: b, key: b.dataset.menuKey || "", pages: [] });
    b.setAttribute("aria-expanded", "true");
    b.setAttribute("aria-controls", "ui-menu");
    page(b._menu(), "", at);
  }
  function page(items, title, at) {
    const el = M.el;
    M.cur = { items, title };
    M.items = title ? [{ label: title, back: true }, "-", ...items] : items;
    el.replaceChildren(...M.items.map((it, i) => {
      if (it === "-") {
        const sep = make("div", "menu-sep");
        sep.setAttribute("role", "separator");
        return sep;
      }
      const n = make("button", "menu-item" + (it.danger ? " danger-quiet" : "") + (it.back ? " menu-back" : ""));
      n.type = "button";
      n.tabIndex = -1;
      n.dataset.i = String(i);
      n.setAttribute("role", it.checked === undefined ? "menuitem" : "menuitemcheckbox");
      if (it.checked !== undefined) n.setAttribute("aria-checked", String(!!it.checked));
      if (it.disabled) n.setAttribute("aria-disabled", "true");
      if (it.sub) n.setAttribute("aria-haspopup", "menu");
      if (it.back) n.setAttribute("aria-label", `Back from ${it.label}`);
      const mark = make("span", "menu-mark");
      if (it.back) mark.append(window.Icon ? Icon("back", "sm") : "‹");
      else if (it.checked && window.Icon) mark.append(Icon("check", "sm"));
      else if (it.checked) mark.textContent = "✓";
      n.append(mark, make("span", "menu-text", it.label));
      if (it.hint) n.append(make("span", "menu-hint", it.hint));
      if (it.sub) n.append(window.Icon ? Icon("chevron", "sm menu-hint") : make("span", "menu-hint", "▸"));
      return n;
    }));
    place();
    const f = focusItems();
    if (f.length) f[at < 0 ? f.length - 1 : Math.min(at || 0, f.length - 1)].focus({ preventScroll: true });
    else { el.tabIndex = -1; el.focus({ preventScroll: true }); }
  }
  const focusItems = () => (M.el ? [...M.el.querySelectorAll(".menu-item")] : []);
  function place() {
    const el = M.el, b = M.btn && M.btn.isConnected ? M.btn : null;
    const r = b ? b.getBoundingClientRect() : M.rect || { left: 8, right: 8, top: 8, bottom: 8 };
    M.rect = r;
    const vw = document.documentElement.clientWidth, vh = window.innerHeight, gap = 4;
    const w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = `${Math.max(8, Math.min(r.right - w, vw - w - 8))}px`;
    const below = r.bottom + gap, above = r.top - gap - h;
    el.style.top = `${below + h <= vh - 8 || above < 8 ? Math.max(8, Math.min(below, vh - h - 8)) : above}px`;
  }
  function choose(it) {
    if (!it || it === "-" || it.disabled) return;
    if (it.back) {
      const prev = M.pages.pop();
      if (prev) page(prev.items, prev.title, prev.at);
      return;
    }
    if (it.sub) {
      M.pages.push({ items: M.cur.items, title: M.cur.title, at: Math.max(0, focusItems().indexOf(document.activeElement)) });
      page(it.sub(), it.label, 1);                              // past Back, on the first choice
      return;
    }
    menuClose(true);
    if (it.run) it.run();
  }
  function menuClose(focus) {
    if (!M.el) return;
    M.el.remove();
    const b = M.btn && M.btn.isConnected ? M.btn
      : M.key ? document.querySelector(`.menu-btn[data-menu-key="${CSS.escape(M.key)}"]`) : null;
    if (M.btn) M.btn.setAttribute("aria-expanded", "false");
    if (b) b.setAttribute("aria-expanded", "false");
    Object.assign(M, { el: null, btn: null, key: "", pages: [], items: [] });
    if (focus && b) b.focus({ preventScroll: true });
  }
  window.addEventListener("keydown", (e) => {       // the window, before a dialog's own keys (the document's)
    if (!M.el) return;
    const f = focusItems(), i = f.indexOf(document.activeElement);
    const move = (to) => { if (f.length) f[(to + f.length) % f.length].focus({ preventScroll: true }); };
    const cur = i >= 0 ? M.items[Number(f[i].dataset.i)] : null;
    let done = true;
    if (e.key === "ArrowDown") move(i + 1);
    else if (e.key === "ArrowUp") move(i < 0 ? -1 : i - 1);
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(-1);
    else if (e.key === "Enter" || e.key === " ") choose(cur);
    else if (e.key === "ArrowRight") { if (cur && cur.sub) choose(cur); }
    else if (e.key === "ArrowLeft" || e.key === "Backspace") { if (M.pages.length) choose({ back: true }); }
    else if (e.key === "Escape" || e.key === "Tab") menuClose(true);
    else done = M.el.contains(document.activeElement);   // no shortcut fires behind an open menu
    if (done) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  document.addEventListener("mousedown", (e) => {
    if (M.el && !M.el.contains(e.target) && !(e.target.closest && e.target.closest(".menu-btn") === M.btn)) menuClose(false);
  }, true);
  document.addEventListener("scroll", (e) => { if (M.el && !M.el.contains(e.target)) menuClose(false); }, true);
  window.addEventListener("resize", () => menuClose(false));

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

  return { toast, confirm, busy, dialog: { open, close, isOpen }, keys, sheet,
           menu: { button: menuButton, close: menuClose, get open() { return !!M.el; } } };
})();
