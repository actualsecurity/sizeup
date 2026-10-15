/* New radio IDs heard on FG1 (GitHub #163; sizeup/cad/newids.py): the state's radio_ids, in the units column
   under the radio and above the units (#177), as "New radio IDs": "Radio 1234 (MDC) on FG1: who is this?", when and how often it was heard, the call it was
   heard during, and a pick: a unit (the call's units first), or who it is with no unit, or Ignore. Dispatchers and
   admins answer (POST /api/cad/radio-id); a viewer, and the wall board, see the question and nothing to press.
   Naming it puts its transmissions on that unit's calls, the ones already heard too; nothing is ever guessed.
   Each question keeps its own row across the state push (keyed by radio), so a pick being made isn't redrawn away. */
(function () {
  "use strict";
  const busy = new Set();                  // radios being answered: not offered twice
  const rows = new Map();                  // key -> {li, sig}

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  const hasCad = () => typeof cad !== "undefined";
  const all = () => ((hasCad() && cad.state && cad.state.radio_ids) || []).filter((a) => !busy.has(a.key));
  const board = () => typeof BOARD !== "undefined" && BOARD;
  const mayAnswer = () => !board() && (typeof canDo !== "function" || canDo("dispatch"));
  const hms = (t) => (typeof fmtTime === "function" ? fmtTime(t, true) : new Date(t * 1000).toLocaleTimeString());
  const question = (a) => `Radio ${a.radio_id} (${a.kind}) on ${a.channel}: who is this?`;

  function heard(a) {
    const n = a.count === 1 ? "once" : `${a.count} times`;
    const when = a.count === 1 ? hms(a.last_at) : `${hms(a.first_at)}–${hms(a.last_at)}`;
    const during = (a.incidents || []).map((i) => i.number || `#${i.id}`).join(", ");
    return `Heard ${n}, ${when}` + (during ? ` · during ${during}` : "");
  }

  async function answer(a, body, what) {
    busy.add(a.key);
    draw();
    try {
      const r = await api("POST", "/api/cad/radio-id", Object.assign({ system: a.system, radio_id: a.radio_id,
                                                                       operator: typeof operator === "function" ? operator() : "" }, body));
      UI.toast(`${a.kind} ${a.radio_id}: ${what}`, "ok", (r.linked || []).length ? `Its transmissions are on ${r.linked.join(", ")}` : "");
    } catch (e) {
      busy.delete(a.key);
      UI.toast(e.message, "err");
      draw();
    }
  }

  function unitPick(a) {
    const s = el("select", "nid-unit");
    s.setAttribute("aria-label", `Unit for radio ${a.radio_id}`);
    s.append(new Option("Unit…", ""));
    const seen = new Set();
    for (const i of a.incidents || []) {
      const g = document.createElement("optgroup");
      g.label = `On ${i.number || "#" + i.id}`;
      for (const u of i.units || []) if (!seen.has(u)) { seen.add(u); g.append(new Option(u, u)); }
      if (g.children.length) s.append(g);
    }
    const g = document.createElement("optgroup");
    g.label = "All units";
    for (const u of (hasCad() && cad.state && cad.state.units) || []) if (!seen.has(u.name)) g.append(new Option(u.name, u.name));
    s.append(g);
    return s;
  }

  function row(a) {
    const li = el("li", "nid");
    li.dataset.radio = a.key;
    const head = el("div", "nid-head");
    const play = el("button", "mini icon quiet");
    if (window.Icon && Icon.play) Icon.play(play); else play.textContent = "▶";
    play.type = "button";
    play.title = `Play its last transmission (${hms(a.last_at)})`;
    play.setAttribute("aria-label", `Play radio ${a.radio_id}'s last transmission`);
    play.addEventListener("click", () => { if (typeof clipUrl === "function") window.play(clipUrl({ id: a.traffic_id }, false), play); });
    head.append(play, el("b", "nid-q", question(a)));
    li.append(head, el("div", "nid-when dim small", heard(a)));
    if (!mayAnswer()) return li;
    const form = el("form", "nid-form");
    const unit = unitPick(a);
    const who = el("input", "nid-who");
    who.placeholder = "or who (no unit)";
    who.maxLength = 60;
    who.setAttribute("aria-label", `Who radio ${a.radio_id} is, with no unit`);
    const save = el("button", "mini primary", "Save");
    save.type = "submit";
    const ign = el("button", "mini quiet", "Ignore");
    ign.type = "button";
    ign.title = "Not one of ours: stop asking (Settings > Radios can change it later)";
    form.append(unit, who, save, ign);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const u = unit.value, w = who.value.trim();
      if (!u && !w) { UI.toast("Pick a unit, or say who it is", "err"); return; }
      answer(a, u ? { unit: u, label: w } : { label: w }, u ? `${u}${w ? ` (${w})` : ""}` : w);
    });
    ign.addEventListener("click", () => answer(a, { ignore: true }, "ignored"));
    li.append(form);
    return li;
  }

  function draw() {
    const list = document.getElementById("q-newids"), sec = document.getElementById("newids");
    if (!list || !sec) return;
    const asked = board() ? [] : all();
    sec.hidden = !asked.length;
    const want = new Set(asked.map((a) => a.key));
    for (const [k, r] of rows) if (!want.has(k)) { r.li.remove(); rows.delete(k); }
    let prev = null;
    for (const a of asked) {
      const sig = JSON.stringify([a.count, a.last_at, a.traffic_id, (a.incidents || []).map((i) => [i.id, i.units]), mayAnswer()]);
      let r = rows.get(a.key);
      if (r && r.sig !== sig) {
        const busyHere = r.li.contains(document.activeElement) && r.li.querySelector("form");
        if (busyHere) {                       // a pick being made: only the line that says when is brought up to date
          r.li.querySelector(".nid-when").textContent = heard(a);
        } else {
          const li = row(a);
          r.li.replaceWith(li);
          r = { li, sig };
          rows.set(a.key, r);
        }
      } else if (!r) {
        r = { li: row(a), sig };
        rows.set(a.key, r);
      }
      const at = prev ? prev.nextSibling : list.firstChild;
      if (r.li !== at) list.insertBefore(r.li, at);
      prev = r.li;
    }
  }

  window.RadioIds = { draw };
  if (hasCad() && Array.isArray(cad.hooks)) cad.hooks.push(draw);
})();
