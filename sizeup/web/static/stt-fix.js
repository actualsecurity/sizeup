// Hand-corrected transcripts (sizeup/cad/sttfix.py): the private reference set for speech-to-text evaluation and #428's
// training bundles. Admins only.
//
//   SttFix.open(t)   a transmission's "Correct transcript" dialog (its "⋯" menu, cad.js): the words as said, spans tagged
//                    (address, callsign, category: select the words, then the kind), Reference, Save / Delete.
//                    GET /api/cad/stt/correction/<id>; POST the same {corrected, domain, tags, reference, based_on} |
//                    {delete}. The box starts from the text of the engine that produced the row (prefill), and the
//                    correction records which engine that was (based_on).
//   SttFix.card()    Settings > System: the counts, "Keep corrected clips" (POST /api/cad/settings/stt_corrections) and
//                    the JSONL export (GET /api/cad/stt/corrections, .jsonl)
//
// Every node is built with textContent: a transcript is never parsed as markup.
"use strict";

window.SttFix = (() => {
  const KINDS = [["address", "Address"], ["callsign", "Callsign"], ["category", "Category"]];
  const engine = (m) => (/^aws/i.test(m || "") ? "AWS" : m ? "Whisper" : "STT");
  const enc = (id) => encodeURIComponent(id);

  // Tags keep their words through an edit, by offset: the edit is the one span that changed between the text before
  // and after (the common prefix and suffix stay), and its length change moves every tag behind it. A tag in front of
  // the edit stays; one whose own words were edited keeps what is left of them (dropped when nothing is). Never by
  // looking the words up: "61" is in "Engine 61 to 61 Example Ave" twice.
  function refit(tags, text, before) {
    if (before === undefined || before === text) {
      return tags.filter((g) => g.start >= 0 && g.end <= text.length && g.start < g.end)
        .map((g) => ({ ...g, text: text.slice(g.start, g.end) }));
    }
    const max = Math.min(before.length, text.length);
    let a = 0;
    while (a < max && before[a] === text[a]) a++;
    let z = 0;
    while (z < max - a && before[before.length - 1 - z] === text[text.length - 1 - z]) z++;
    const cut = before.length - z;              // the edit replaced before[a, cut) ...
    const put = text.length - z;                // ... with text[a, put)
    const delta = put - cut;
    const typed = text.slice(a, put);
    const out = [];
    for (const g of tags) {
      let s = g.start, e = g.end;
      if (e < a || (e === a && (cut > a || !typed || /\s/.test(typed)))) {
        // in front of the edit: as it was
      } else if (e === a) {
        e = put;                                 // typed on at the end of its last word: the word grew
      } else if (s >= cut) {
        s += delta;                              // behind the edit: moved by its length change
        e += delta;
      } else {                                   // the edit touched its words: what is left of them, with what was typed
        s = s <= a ? s : put;                    // in them (starting inside what was replaced: from after the new words)
        e = e >= cut ? e + delta : a;
      }
      while (s < e && /\s/.test(text[s])) s++;
      while (e > s && /\s/.test(text[e - 1])) e--;
      if (s < e) out.push({ ...g, start: s, end: e, text: text.slice(s, e) });
    }
    return out;
  }

  async function open(t) {
    let d;
    try {
      d = await api("GET", `/api/cad/stt/correction/${enc(t.id)}`);
    } catch (e) {
      UI.toast(`Can't open: ${e.message}`, "err");
      return;
    }
    const c = d.correction;
    const ov = el("div", "overlay");
    const panel = el("div", "dialog narrow");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-labelledby", "sttfix-title");
    const head = el("div", "dialog-head");
    const h = el("h2", "dialog-title", "Correct transcript");
    h.id = "sttfix-title";
    head.append(h);
    const body = el("form", "dialog-body");
    body.append(el("p", "dim small", `${fmtTime(d.start, true)} · ${d.channel}${d.talker ? ` · ${d.talker}` : ""}`));
    if (c && c.stale) body.append(el("p", "warnbox small", "Re-transcribed since"));
    for (const [text, model] of [[d.text, d.model], [d.alt_text, d.alt_model]]) {
      if (!text) continue;
      const p = el("p", "small");
      const tag = el("b", "", `${engine(model)} `);
      tag.title = model || "";
      p.append(tag, el("span", "dim", text));
      body.append(p);
    }
    const area = el("textarea");
    // a new correction starts from the text of the engine that produced the row, and says which engine that was
    const first = d.prefill || { text: d.text || "", engine: d.model || "" };
    Object.assign(area, { rows: 3, maxLength: 2000, value: c ? c.corrected : first.text, spellcheck: true });
    area.setAttribute("aria-label", "As said");
    let tags = ((c && c.tags) || []).map((g) => ({ ...g, text: (c.corrected || "").slice(g.start, g.end) }));
    const chips = el("div", "row");
    const drawTags = () => {
      chips.replaceChildren(...tags.map((g, i) => {
        const x = el("button", "chip", `${g.kind}${g.value ? ` (${g.value})` : ""}: ${g.text} ×`);
        x.type = "button";
        x.title = "Remove this tag";
        x.addEventListener("click", () => { tags.splice(i, 1); drawTags(); });
        return x;
      }));
    };
    let was = area.value;
    area.addEventListener("input", () => { tags = refit(tags, area.value, was); was = area.value; drawTags(); });
    const catVal = el("input");
    Object.assign(catVal, { type: "text", maxLength: 40, placeholder: "category", spellcheck: false });
    catVal.setAttribute("aria-label", "Category name for a category tag");
    const tagRow = el("div", "row");
    tagRow.append(el("span", "dim small", "Tag"));
    for (const [k, label] of KINDS) {
      const b = el("button", "mini", label);
      b.type = "button";
      b.title = `Select words above, then tag them as ${label.toLowerCase()}`;
      b.addEventListener("click", () => {
        let s = area.selectionStart, e = area.selectionEnd;
        while (s < e && /\s/.test(area.value[s])) s++;
        while (e > s && /\s/.test(area.value[e - 1])) e--;
        if (s >= e) { UI.toast("Select the words first", "info"); return; }
        const g = { kind: k, start: s, end: e, text: area.value.slice(s, e) };
        if (k === "category" && catVal.value.trim()) g.value = catVal.value.trim();
        tags = tags.filter((x) => !(x.kind === k && x.start === s && x.end === e)).concat(g)
          .sort((a, b2) => a.start - b2.start || a.end - b2.end);
        drawTags();
      });
      tagRow.append(b);
    }
    tagRow.append(catVal);
    const dom = el("select");
    dom.setAttribute("aria-label", "Domain");
    for (const k of d.domains) dom.append(new Option(k, k));
    dom.value = c ? c.domain : d.domain;
    const ref = el("input");
    ref.type = "checkbox";
    ref.checked = !!(c && c.reference);
    const refL = el("label", "inline");
    refL.append(ref, " Reference");
    refL.title = "Checked word for word: in the evaluation's reference set";
    const opts = el("div", "row");
    opts.append(dom, refL, el("span", "spacer"), el("span", "dim small", c ? `${c.by || "?"} · ${fmtTime(c.at, true)}` : ""));
    const err = el("p", "error small");
    err.hidden = true;
    const foot = el("div", "dialog-foot");
    if (c) {
      const del = el("button", "danger-quiet", "Delete");
      del.type = "button";
      del.addEventListener("click", async () => {
        if (!(await UI.confirm("Delete this correction?", { ok: "Delete" }))) return;
        try {
          await api("POST", `/api/cad/stt/correction/${enc(t.id)}`, { delete: true, operator: operator() });
          UI.toast("Correction deleted", "ok");
          UI.dialog.close(ov);
        } catch (ex) { err.textContent = ex.message; err.hidden = false; }
      });
      foot.append(del, el("span", "spacer"));
    }
    const no = el("button", "", "Cancel");
    no.type = "button";
    no.addEventListener("click", () => UI.dialog.close(ov));
    const yes = el("button", "primary", "Save");
    yes.type = "submit";
    foot.append(no, yes);
    body.append(area, tagRow, chips, opts, err, foot);
    body.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = area.value.trim();
      if (!text) { err.textContent = "Type the words as said"; err.hidden = false; return; }
      const lead = area.value.length - area.value.trimStart().length;      // the server trims: the spans move with it
      const out = refit(tags, area.value, was).map((g) => ({ kind: g.kind, start: g.start - lead, end: g.end - lead,
                                                        ...(g.value ? { value: g.value } : {}) }))
        .filter((g) => g.start >= 0 && g.end <= text.length);
      try {
        await UI.busy(api("POST", `/api/cad/stt/correction/${enc(t.id)}`, { corrected: text, domain: dom.value, tags: out,
                                                                           reference: ref.checked, operator: operator(),
                                                                           ...(c ? {} : { based_on: first.engine || "" }) }), yes);
        UI.toast(ref.checked ? "Saved · reference" : "Saved", "ok", out.length ? `${out.length} tag${out.length > 1 ? "s" : ""}` : "");
        UI.dialog.close(ov);
      } catch (ex) { err.textContent = ex.message; err.hidden = false; }
    });
    panel.append(head, body);
    ov.append(panel);
    document.body.append(ov);
    drawTags();
    UI.dialog.open(ov, { focus: area, onClose: () => ov.remove() });
  }

  // Settings > System (admins): counts and the export; the corrections themselves open from a transmission's menu
  function card() {
    const info = el("span", "dim small", "…");
    const a = el("a", "nav", "Export JSONL");
    a.href = "/api/cad/stt/corrections.jsonl";
    a.setAttribute("download", "");
    a.title = "Every corrected clip, #428's format. Private: never into the repo";
    const refOnly = el("a", "nav", "References only");
    refOnly.href = "/api/cad/stt/corrections.jsonl?reference=1";
    refOnly.setAttribute("download", "");
    const keep = el("input");
    keep.type = "checkbox";
    keep.checked = true;
    const keepL = el("label", "inline");
    keepL.append(keep, " Keep corrected clips");
    keepL.title = "A corrected transmission and its audio are never pruned (the training set). Off: they go by their channel's retention";
    const show = (n) => {
      const doms = Object.entries(n.domains || {}).map(([k, v]) => `${k} ${v.n}`).join(" · ");
      keep.checked = n.keep !== false;
      info.textContent = `${n.n} · ${n.reference} reference${doms ? ` · ${doms}` : ""} · ${n.kept} corrected clips kept`;
    };
    keep.addEventListener("change", async () => {
      try {
        const out = await UI.busy(api("POST", "/api/cad/settings/stt_corrections",
                                      { action: "set", key: "keep", value: keep.checked, operator: operator() }), keep);
        show(out.stt_corrections);
      } catch (ex) {
        keep.checked = !keep.checked;
        UI.toast(`Can't save: ${ex.message}`, "err");
      }
    });
    const row = el("div", "row");
    row.append(info, el("span", "spacer"), keepL, a, refOnly);
    const box = sCard(sCardHead("Transcript corrections"), row);
    box.id = "s-stt-fix";
    api("GET", "/api/cad/stt/corrections").then(show).catch(() => { info.textContent = "—"; });
    return box;
  }

  return { open, card, refit };
})();
