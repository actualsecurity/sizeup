/* A dongle swapped for a new one (sizeup/radio/replace.py; the CAD state's dongle_swaps): asked on the console, under the
   radio with the new radio IDs, as "New dongle <serial> seen — use it for Fire/EMS 15?". Use it moves the gone dongle's
   jobs (and its place in its antenna group) to the new one; Not now: asked again in 6 h. When the pairing can't tell
   which new dongle is the gone one's, a Use button per new dongle. Admins answer (POST /api/sdr-replace, audited); anyone
   else, and the wall board, see the question and nothing to press. Nothing ever moves without an answer (the optional
   auto-replace for Fire/EMS 15 and paging is Settings > Radios > Receivers). */
"use strict";
(function () {
  "use strict";
  const busy = new Set();                  // gone dongles being answered: not offered twice
  const hasCad = () => typeof cad !== "undefined";
  const board = () => typeof BOARD !== "undefined" && BOARD;
  const mayAnswer = () => !board() && (typeof canDo !== "function" || canDo("admin"));
  const all = () => ((hasCad() && cad.state && cad.state.dongle_swaps) || []).filter((o) => !busy.has(o.missing));
  let drawn = "";

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  const jobsOf = (o) => (o.jobs || []).join(", ") || (o.group ? `group ${o.group}` : "its place");

  async function answer(o, body, said) {
    busy.add(o.missing);
    draw(true);
    try {
      const r = await fetch("/api/sdr-replace", { method: "POST", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify(Object.assign({ missing: o.missing }, body)) });
      if (!r.ok) throw new Error(((await r.json().catch(() => ({}))).error) || `refused (${r.status})`);
      if (window.UI && UI.toast) UI.toast(said, "ok");
    } catch (e) {
      busy.delete(o.missing);
      if (window.UI && UI.toast) UI.toast(e.message, "err");
      draw(true);
    }
  }

  function btn(label, cls, title, fn) {
    const b = el("button", cls, label);
    b.type = "button";
    b.title = title;
    b.addEventListener("click", fn);
    return b;
  }

  function row(o) {
    const li = el("li", "nid");
    const pick = o.pick && (o.choices || []).find((c) => c.serial === o.pick);
    const head = el("div", "nid-head");
    const q = el("b", "nid-q", pick ? `New dongle ${pick.serial} seen — use it for ${jobsOf(o)}?` : `SDR ${o.name} gone: new dongle for ${jobsOf(o)}?`);
    q.title = `Replaces SDR ${o.name}` + (o.group ? ` (group ${o.group})` : "") + "\n"
      + (o.choices || []).map((c) => `${c.serial}: ${c.product || "?"}${c.tuner ? `, ${c.tuner}` : ""}`).join("\n");
    head.append(q);
    li.append(head);
    if (!mayAnswer()) return li;
    const form = el("div", "nid-form");
    for (const c of pick ? [pick] : o.choices || []) {
      form.append(btn(pick ? "Use it" : `Use ${c.name || c.serial}`, "mini primary", `${jobsOf(o)} to SDR ${c.serial}`,
        () => answer(o, { new: c.serial, jobs: o.jobs, group: o.group }, `${jobsOf(o)} → SDR ${c.name || c.serial}`)));
    }
    form.append(btn("Not now", "mini quiet", "Asked again in 6 h", () => answer(o, { action: "not_now" }, `SDR ${o.name}: not now`)));
    li.append(form);
    return li;
  }

  function draw(force) {
    const list = document.getElementById("q-dongleswaps"), sec = document.getElementById("dongleswaps");
    if (!list || !sec) return;
    const asked = board() ? [] : all();
    const sig = JSON.stringify([asked, mayAnswer()]);
    if (!force && sig === drawn) return;
    drawn = sig;
    sec.hidden = !asked.length;
    list.replaceChildren(...asked.map(row));
  }

  window.DongleSwaps = { draw };
  if (hasCad() && Array.isArray(cad.hooks)) cad.hooks.push(() => draw(false));
})();
