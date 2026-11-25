// Settings > Station > STT workers (sizeup/cloud/sttworkers.py, #639): the GPU speech-to-text workers' tokens and the
// pool's switches; admins, the hub only. GET /api/cad/stt-workers: each worker (id, name, last used, revoked, its live
// state) and the pool {on, classes}; never a token or a hash. POST {action: add | rename | rotate | revoke | llm | update | pool}: add
// and rotate answer the new token, shown here ONCE (this page's memory until Done, never stored) with a copy button.
"use strict";
const SttWorkersCard = (() => {
  const POLL_MS = 10000;
  const CLASS_LABELS = { urgent: "Pages, Fire/EMS 15, firegrounds", high: "Airband", live: "Other main channels",
    rerun: "Re-runs", "context-fallback": "Police alert (AWS budget out)", context: "Police alert", low: "Context talkgroups",
    catchup: "Shed clips (catch-up)" };
  let card = null, status = null, form = null, reveal = null, timer = null, busy = false, last = null, said = "";
  let idIn = null, nameIn = null, fresh = null;
  let pinForm = null, pinHead = null, pinCommit = null, pinSha = null, pinning = "";

  const when = (t) => (t ? fmtTime(t, true) : "—");
  const isOpen = () => !!card && card.isConnected && !(card.parentNode && card.parentNode.closest("[hidden]")) && !document.hidden;

  async function post(body) {
    const r = await fetch("/api/cad/stt-workers", { method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  function btn(label, cls, fn) {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
  }

  async function act(body, done) {
    said = "";
    let ok = false;
    try {
      const d = await post(body);
      if (d.token) fresh = { id: d.id, token: d.token, rotated: body.action === "rotate", grace: d.grace_s };
      if (done) said = done;
      ok = true;
    } catch (e) { said = e.message; }
    poll(true);
    return ok;
  }

  function build() {
    status = el("div");
    reveal = el("div");
    form = el("div", "row");
    const l1 = el("label", "field");
    idIn = el("input");
    idIn.maxLength = 40;
    idIn.spellcheck = false;
    idIn.title = "Lower case, digits, - and _ (e.g. gpu-1). It is part of the token";
    l1.append("Id", idIn);
    const l2 = el("label", "field");
    nameIn = el("input");
    nameIn.maxLength = 60;
    l2.append("Name", nameIn);
    const add = btn("Add", "primary", () => {
      const id = idIn.value.trim();
      if (!id) { said = "Id?"; render(); return; }
      act({ action: "add", id, name: nameIn.value.trim() }, "");
      idIn.value = ""; nameIn.value = "";
    });
    form.append(l1, l2, add);
    // a worker's self-update pinned: a commit on main and worker.py's SHA-256 at it (the worker checks both)
    pinForm = el("div", "row");
    pinForm.hidden = true;
    pinHead = el("span", "mono");
    const l3 = el("label", "field");
    pinCommit = el("input");
    pinCommit.maxLength = 40;
    pinCommit.spellcheck = false;
    pinCommit.title = "The full merge commit on main (as the hub's builds are)";
    l3.append("Commit", pinCommit);
    const l4 = el("label", "field");
    pinSha = el("input");
    pinSha.maxLength = 64;
    pinSha.spellcheck = false;
    pinSha.title = "SHA-256 of tools/stt_worker/worker.py at that commit";
    l4.append("SHA-256", pinSha);
    const pin = btn("Pin", "primary", async () => {
      const id = pinning;
      if (await act({ action: "update", id, mode: "pin", commit: pinCommit.value.trim(), sha256: pinSha.value.trim() },
        `${id}: pinned`)) { pinning = ""; pinCommit.value = ""; pinSha.value = ""; render(); }
    });
    pinForm.append(pinHead, l3, l4, pin, btn("Cancel", "", () => { pinning = ""; render(); }));
  }

  function renderReveal() {
    if (!fresh) { reveal.replaceChildren(); return; }
    const f = fresh;
    const box = el("div", "notice ok");
    const head = el("p", "", `${f.id}: new token · shown once`);
    head.title = "Not stored on the hub (only its hash). Put it in the worker's config (SIZEUP_STT_WORKER_TOKEN)"
      + (f.rotated && f.grace ? `. The old one works ${Math.round(f.grace / 60)} min more` : "");
    const row = el("div", "row");
    row.append(btn("Copy token", "primary", async () => {
      try { await navigator.clipboard.writeText(f.token); if (window.UI && UI.toast) UI.toast("Copied", "ok"); } catch (_) { /* select it by hand */ }
    }), btn("Done", "", () => { fresh = null; render(); }));
    box.append(head, el("p", "mono", f.token), row);
    reveal.replaceChildren(box);
  }

  function poolRow(d) {
    const p = d.pool || { on: false, classes: {} };
    const wrap = el("div", "row");
    const on = el("label", "check");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!p.on;
    cb.addEventListener("change", () => act({ action: "pool", on: cb.checked }, cb.checked ? "Pool on" : "Pool off"));
    on.append(cb, " Pool on");
    on.title = "Off: every clip goes the way it went before (AWS, the station's whisper)";
    wrap.append(on);

    for (const [k, v] of Object.entries(p.classes || {})) {
      const l = el("label", "check");
      const c = el("input");
      c.type = "checkbox";
      c.checked = !!v;
      c.disabled = !p.on;
      c.addEventListener("change", () => act({ action: "pool", classes: { [k]: c.checked } }, ""));
      l.append(c, ` ${CLASS_LABELS[k] || k}`);
      l.title = "Clips of this class go to a ready GPU worker first";
      wrap.append(l);
    }
    return wrap;
  }

  // a GPU worker's local LLM (sizeup/cloud/infer.py): its "LLM jobs" switch, once it has an LLM (or was switched off)
  function llmCell(w) {
    const td = el("td", "");
    if (w.revoked || (!w.llm && w.llm_on !== false)) { td.append("—"); return td; }
    const l = el("label", "check");
    const c = el("input");
    c.type = "checkbox";
    c.checked = w.llm_on !== false;
    c.addEventListener("change", () => act({ action: "llm", id: w.id, on: c.checked },
      `${w.id}: LLM jobs ${c.checked ? "on" : "off"}`));
    l.append(c, ` ${w.llm || "off"}`);
    l.title = "The intel engine's classifications on this worker's local model. Off: it gets no prompts";
    td.append(l);
    return td;
  }

  // a GPU worker's self-update (tools/stt_worker/worker.py): the worker.py it runs (green: the hub's), auto / hold / pin
  function versionCell(w) {
    const td = el("td", "");
    if (w.revoked) { td.append("—"); return td; }
    const u = w.update || {}, m = w.update_mode || { mode: "auto" };
    const bad = /^(refused|rolled back)/.test(u.state || "");
    const run = el("span", w.update_current ? "mono hl-ok" : bad || w.update_current === false ? "mono hl-warn" : "mono",
      u.sha256 ? u.sha256.slice(0, 12) : "—");
    run.title = [u.commit ? `Commit ${u.commit.slice(0, 12)}` : "", u.state || "",
      w.update_wanted && !w.update_current ? `Hub: ${w.update_wanted.slice(0, 12)}` : "",
      u.auto === false ? "auto_update off (worker.toml)" : ""].filter(Boolean).join("\n");
    const sel = el("select", "sm");
    for (const [v, label] of [["auto", "Auto"], ["hold", "Hold"], ["pin", m.mode === "pin" ? `Pin ${m.commit.slice(0, 7)}` : "Pin…"]]) {
      const o = el("option", "", label);
      o.value = v;
      sel.append(o);
    }
    sel.value = m.mode;
    sel.title = "Auto: the hub's build · Hold: no updates · Pin: a commit on main and its SHA-256";
    sel.addEventListener("change", () => {
      if (sel.value === "pin") { pinning = w.id; render(); return; }
      pinning = "";
      act({ action: "update", id: w.id, mode: sel.value }, `${w.id}: update ${sel.value}`);
    });
    td.append(run, " ", sel);
    return td;
  }

  function render() {
    if (!card) return;
    const d = last;
    renderReveal();
    if (!d) { status.replaceChildren(el("p", "dim small", "Loading…")); return; }
    const head = el("div", "card-head");
    head.append(el("h2", "", "STT workers"), el("span", "spacer"));
    if (window.Help && Help.info) {
      head.append(Help.info("GPU speech-to-text workers (sw1.<id>.…) connect out to the hub and take clips first; a clip "
        + "no worker answers in time goes to AWS, then the station's whisper. The hub keeps only each token's hash. "
        + `Rotate: the old token works ${Math.round((d.grace_s || 600) / 60)} min more. Revoke: refused from its next request`, "About"));
    }
    const t = el("table", "evtable");
    const hr = el("tr");
    for (const h of ["Name", "Id", "State", "Model", "Version", "LLM jobs", "Last seen", ""]) hr.append(el("th", "", h));
    t.append(el("thead"), el("tbody"));
    t.tHead.append(hr);
    for (const w of d.workers || []) {
      const tr = el("tr");
      const acts = el("td", "");
      if (!w.revoked) {
        acts.append(btn("Rotate", "sm", async () => {
          if (await UI.confirm(`New token for ${w.id}? The old one works ${Math.round((d.grace_s || 600) / 60)} more min.`,
            { ok: "Rotate", danger: false })) act({ action: "rotate", id: w.id }, "");
        }));
        acts.append(btn("Revoke", "sm danger", async () => {
          if (await UI.confirm(`Revoke ${w.id}? Refused from its next request; its clips go to AWS.`, { ok: "Revoke" })) {
            act({ action: "revoke", id: w.id }, `${w.id} revoked`);
          }
        }));
      } else {
        acts.append(btn("New token", "sm", () => act({ action: "rotate", id: w.id }, "")));
      }
      const st = w.revoked ? `revoked ${when(w.revoked)}` : (w.state || "offline");
      tr.append(el("td", "", w.name || "—"), el("td", "mono", w.id),
        el("td", w.revoked || st === "offline" ? "hl-bad" : st === "online" ? "hl-ok" : "dim", st),
        el("td", "", w.model || "—"), versionCell(w), llmCell(w), el("td", "t", when(w.seen || w.last_used)), acts);
      t.tBodies[0].append(tr);
    }
    if (!(d.workers || []).length) {
      const tr = el("tr");
      tr.append(Object.assign(el("td", "empty", "None"), { colSpan: 8 }));
      t.tBodies[0].append(tr);
    }
    pinForm.hidden = !pinning;
    pinHead.textContent = pinning ? `${pinning}:` : "";
    const kids = [head, t, pinForm, poolRow(d)];
    if (said) kids.push(el("p", "dim small", said));
    kids.push(el("p", "dim small", `hub.sqlite · ${when(d.at)}`));
    status.replaceChildren(...kids);
  }

  async function poll(now) {
    clearTimeout(timer);
    timer = null;
    if ((busy && !now) || !isOpen()) return;
    busy = true;
    let again = true;
    try {
      const r = await fetch("/api/cad/stt-workers", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (r.status === 403 || r.status === 404 || r.status === 409) { again = false; card.hidden = true; }
      else if (r.ok) { last = await r.json(); card.hidden = false; render(); }
    } catch (_) { /* the next poll tries again */ }
    busy = false;
    if (again && isOpen()) timer = setTimeout(poll, POLL_MS);
  }

  // settings.js, each render of Settings > Station > STT workers: the card (made once), then the poll.
  function mount(node) {
    if (!card || !node.contains(card)) {
      card = el("div", "card s-card");
      card.hidden = true;
      build();
      card.append(status, reveal, form);
      node.replaceChildren(card);
    }
    render();
    poll();
  }

  document.addEventListener("visibilitychange", () => { if (isOpen() && !timer) poll(); });
  return { mount };
})();
window.SttWorkersCard = SttWorkersCard;
