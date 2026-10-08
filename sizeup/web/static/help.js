// "?" help: the longer explanation of a section, kept off the screen until asked for.
//   In a page:   <button type="button" class="help" data-help="...">?</button>
//   In a script: Help.button("...")  (the same button)
// Click, Enter or Space opens it under the button; the button again, a click elsewhere, Esc, a scroll
// or a resize closes it (Esc puts the focus back on the button).
"use strict";

window.Help = (() => {
  let pop = null, from = null;

  function button(text, label = "Help") {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "help";
    b.textContent = "?";
    b.dataset.help = text;
    b.setAttribute("aria-label", label);
    b.setAttribute("aria-expanded", "false");
    return b;
  }

  function close(focus) {
    if (!pop || pop.hidden) return;
    pop.hidden = true;
    if (from) {
      from.setAttribute("aria-expanded", "false");
      if (focus && from.isConnected) from.focus();
    }
    from = null;
  }

  function open(b) {
    if (!pop) {
      pop = document.createElement("div");
      pop.id = "help-pop";
      pop.className = "help-pop";
      pop.setAttribute("role", "note");
      pop.hidden = true;
      document.body.append(pop);
    }
    close(false);
    from = b;
    pop.textContent = b.dataset.help;
    pop.hidden = false;
    b.setAttribute("aria-expanded", "true");
    b.setAttribute("aria-controls", "help-pop");
    const r = b.getBoundingClientRect(), vw = document.documentElement.clientWidth, vh = window.innerHeight;
    const w = pop.offsetWidth, h = pop.offsetHeight;
    pop.style.left = `${Math.max(8, Math.min(r.left, vw - w - 8))}px`;
    pop.style.top = `${r.bottom + 6 + h > vh && r.top - 6 - h > 0 ? r.top - 6 - h : r.bottom + 6}px`;
  }

  // Capture: a "?" inside a heading that does something on click (a folded board) only opens the help.
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("button.help[data-help]");
    if (b) {
      e.preventDefault();
      e.stopPropagation();
      if (from === b) close(false); else open(b);
      return;
    }
    if (pop && !pop.hidden && !pop.contains(e.target)) close(false);
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && pop && !pop.hidden) { e.stopPropagation(); close(true); }
  }, true);
  window.addEventListener("resize", () => close(false));
  document.addEventListener("scroll", () => close(false), true);

  return { button, close };
})();
