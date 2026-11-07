// Dispatch modes (#311, sizeup/cad/automode.py). The state push carries "dispatch_mode": {mode, label, short, modes,
// at, by}; each incident "auto": {tracked, paged_at, steps: [{id, at, kind, source, unit, status, text, event_id, undone,
// undo}]}.
//
//   DispatchMode.header(node, st, deps)        the header's switch: 60 Control | PCFD (dispatchers and admins)
//   DispatchMode.eventChip(inc, e, deps)       a Timeline entry an automatic step made: its source, ✕ undoes it
//   DispatchMode.unitChip(inc, a, deps)        a unit on the incident whose status automation set: the same
//   DispatchMode.sig(inc, key)                 what to redraw on (the step's undo state)
"use strict";

window.DispatchMode = (() => {
  const TIPS = {
    "60control": "60 Control dispatching: County feed incidents accepted; units, statuses and closing follow the feed and the radio",
    pcfd: "PCFD dispatching: County feed incidents queued; dispatch and progress by hand",
  };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function header(node, st, deps) {
    if (!node) return;
    node.hidden = !st;
    if (!st) return;
    const may = deps.canDo("dispatch");
    const sig = JSON.stringify([st.mode, st.by, st.at, may]);
    if (node._sig === sig) return;
    node._sig = sig;
    const who = st.by ? `\nSet by ${st.by.replace(/\s*<[^<>]*>\s*$/, "")}${st.at ? ` ${deps.time(st.at)}` : ""}` : "";
    node.replaceChildren(...st.modes.map((m) => {
      const b = el("button", "", m.short);
      b.type = "button";
      b.setAttribute("aria-pressed", String(m.mode === st.mode));
      b.disabled = !may && m.mode !== st.mode;
      b.title = TIPS[m.mode] + (m.mode === st.mode ? who : "") + (may ? "" : "\nDispatchers and admins switch it");
      b.addEventListener("click", () => set(m, st, deps));
      return b;
    }));
  }

  async function set(m, st, deps) {
    if (m.mode === st.mode || !deps.canDo("dispatch")) return;
    if (!await deps.confirm(`Switch to ${m.label}?`, { ok: m.short, danger: false })) return;
    try {
      await deps.api("POST", "/api/cad/dispatch-mode", { mode: m.mode, operator: deps.operator() });
      deps.toast(m.label, "ok");
    } catch (err) {
      deps.toast(`Mode not changed: ${err.message}`, "err");
    }
  }

  const steps = (inc) => (inc && inc.auto && inc.auto.steps) || [];

  function chip(inc, s, deps) {
    if (s.ask_close) return askClose(inc, s, deps);
    const c = el("span", `auto-src ${s.source}${s.undone ? " undone" : ""}`, s.source);
    c.title = `${s.undone ? "Undone: " : "Automatic: "}${s.text} (${s.source === "county" ? "County feed" : "radio"}, ${deps.time(s.at)})`;
    if (!s.undo || deps.readonly || !deps.canDo("dispatch")) return c;
    const x = el("button", "mini auto-undo", "✕");
    x.type = "button";
    x.title = `Undo: ${s.text}`;
    x.setAttribute("aria-label", `Undo ${s.text}`);
    x.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      x.disabled = true;
      try {
        await deps.api("POST", `/api/cad/incident/${inc.id}/auto-undo`, { step: s.id, operator: deps.operator() });
        deps.toast(`Undone: ${s.text}`, "ok");
      } catch (err) {
        x.disabled = false;
        deps.toast(`Not undone: ${err.message}`, "err");
      }
    });
    const w = el("span", "auto-step");
    w.append(c, x);
    return w;
  }

  // "County in service: close?" (a unit still on it): one click closes it, Complete
  function askClose(inc, s, deps) {
    const c = el("span", `auto-src ${s.source}`, s.source);
    c.title = `${s.text} (County feed, ${deps.time(s.at)})`;
    const open = inc.status === "pending" || inc.status === "active";
    if (!open || deps.readonly || !deps.canDo("dispatch")) return c;
    const b = el("button", "mini", "Close");
    b.type = "button";
    b.title = "Close: Complete";
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      b.disabled = true;
      try {
        await deps.api("POST", `/api/cad/incident/${inc.id}/close`, { disposition: "COMPLETE", operator: deps.operator() });
        deps.toast("Closed", "ok");
      } catch (err) {
        b.disabled = false;
        deps.toast(`Not closed: ${err.message}`, "err");
      }
    });
    const w = el("span", "auto-step");
    w.append(c, b);
    return w;
  }

  function eventChip(inc, e, deps) {
    const s = steps(inc).find((x) => x.event_id === e.id);
    return s ? chip(inc, s, deps) : null;
  }

  // The step that set this unit's current status on the incident (the latest, not undone).
  function unitStep(inc, a) {
    if (a.cleared_at) return null;
    const mine = steps(inc).filter((s) => s.unit === a.name && !s.undone && (s.kind === "status" || s.kind === "assign"));
    const last = mine[mine.length - 1];
    return last && last.status === a.status ? last : null;
  }

  function unitChip(inc, a, deps) {
    const s = unitStep(inc, a);
    return s ? chip(inc, s, deps) : null;
  }

  function sig(inc, key) {
    const s = typeof key === "number" ? steps(inc).find((x) => x.event_id === key) : unitStep(inc, key);
    return s ? `${s.id}${s.undone ? "u" : ""}${inc.status}` : "";
  }

  return { header, eventChip, unitChip, sig };
})();
