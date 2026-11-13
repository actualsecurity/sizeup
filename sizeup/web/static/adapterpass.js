// The station adapter's county password (sizeup/cloud/adapterpass.py): Settings > Health, admins, the hub only.
// Write-only: a new password typed twice goes to POST /api/cad/adapter-password, which saves it as the next SSM
// version; the hub can't read it back, and nothing here keeps it (no form element: never a GET with it in the URL; the
// fields are emptied as the request goes). GET /api/cad/adapter-password: the version, when and who saved it, the
// next save, the station adapter's state. Its own card and poll, so typing is never wiped by Health's 5 s render.
const AdapterPasswordCard = (() => {
  const POLL_MS = 15000;
  let card = null, status = null, form = null, timer = null, busy = false, saving = false, last = null, said = "";
  let pw1 = null, pw2 = null, confirmBox = null, confirmLabel = null, saveBtn = null;

  const when = (t) => (t ? fmtTime(t, true) : "—");
  const isOpen = () => !!card && card.isConnected && !(card.parentNode && card.parentNode.closest("[hidden]")) && !document.hidden;
  const mins = (s) => `${Math.max(1, Math.round(s / 60))} min`;

  function field(label) {
    const l = el("label", "field");
    const i = el("input");
    i.type = "password";
    i.autocomplete = "new-password";
    i.spellcheck = false;
    i.maxLength = 4096;
    l.append(label, i);
    return [l, i];
  }

  function build() {
    status = el("div");
    form = el("div");
    const [l1, i1] = field("New password");
    const [l2, i2] = field("Again");
    pw1 = i1; pw2 = i2;
    confirmLabel = el("label", "check");
    confirmBox = el("input");
    confirmBox.type = "checkbox";
    confirmLabel.append(confirmBox, el("span", "", "Stopped on refused log-ons: save anyway"));
    confirmLabel.title = "The adapter stopped after the county refused its log-ons. A new version lifts a login stop for "
      + "one attempt; a manual stop also needs Reset (Health above)";
    confirmLabel.hidden = true;
    saveBtn = el("button", "primary", "Save");
    saveBtn.type = "button";
    saveBtn.addEventListener("click", submit);
    for (const i of [pw1, pw2]) i.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    const warn = el("p", "notice", "Each save = 1 log-on attempt on the shared county account");
    warn.title = "The station tries each new version once, within about 5 minutes. 3 refused versions in 24 h stop "
      + "it until someone resets it. Saves are at least 5 min apart. Check the password before saving; don't re-save to retry";
    const row = el("div", "row");
    row.append(l1, l2, saveBtn);
    form.append(warn, row, confirmLabel);
  }

  function render() {
    if (!card) return;
    const d = last;
    if (!d) { status.replaceChildren(el("p", "dim small", "Loading…")); return; }
    const head = el("div", "card-head");
    head.append(el("h2", "", "Station adapter password"));
    if (d.version) head.append(el("span", "chip hl-ok", `v${d.version}`));
    head.append(el("span", "spacer"));
    const info = "Write-only: saved to AWS SSM as the next version; the hub can't read it back. The station reads it "
      + "within about 5 min. Fallback: the sizeup-aws password script";
    if (window.Help && Help.info) head.append(Help.info(info, "About"));
    const rows = [];
    if (!d.configured) rows.push(["Version", d.why_not || "Not configured", "dim"]);
    else if (d.version) rows.push(["Version", `v${d.version} · ${when(d.modified)}`
      + (d.saved_here ? ` · ${d.saved_by || "?"}` : " · not from the hub")]);
    else rows.push(["Version", d.error || "—", d.error ? "hl-bad" : ""]);
    for (const s of d.sources || []) {
      rows.push([s.label || s.source, `${(s.state || "?").replace(/_/g, " ")}${s.reason ? ` · ${s.reason}` : ""}`,
        s.state === "ok" ? "hl-ok" : s.stopped ? "hl-bad" : "hl-warn"]);
    }
    if (!(d.sources || []).length) rows.push(["Station", "No adapter report", "dim"]);
    if (d.configured && d.wait_s > 0) rows.push(["Next save", `in ${mins(d.wait_s)}`, "hl-warn"]);
    const dl = el("dl", "admin");
    for (const [k, v, cls] of rows) {
      const w = el("div");
      w.append(el("dt", "", k), el("dd", cls || "", v));
      dl.append(w);
    }
    const kids = [head, dl];
    const recent = (d.saves || []).filter((s) => !/^v\d+$/.test(s.result)).slice(0, 1);
    for (const s of recent) {
      const p = el("p", "notice", `${when(s.at)} · ${s.by}: ${s.result}`);
      p.title = "No answer from AWS on that save: it may have gone. Check the version before saving again";
      kids.push(p);
    }
    if (said) kids.push(el("p", "dim small", said));
    status.replaceChildren(...kids);
    form.hidden = !d.configured;
    confirmLabel.hidden = !d.stopped;
    if (!d.stopped) confirmBox.checked = false;
    saveBtn.disabled = saving || !d.configured || d.wait_s > 0 || d.busy;
    saveBtn.textContent = saving ? "Saving…" : "Save";
  }

  async function submit() {
    if (saving || !last || !last.configured) return;
    if (!pw1.value) { said = "Empty"; render(); return; }
    if (pw1.value !== pw2.value) { said = "They differ"; render(); return; }
    if (last.stopped && !confirmBox.checked) { said = "Tick the box first"; render(); return; }
    const body = JSON.stringify({ password: pw1.value, again: pw2.value, confirm: confirmBox.checked });
    pw1.value = ""; pw2.value = "";
    saving = true; said = ""; render();
    try {
      const r = await fetch("/api/cad/adapter-password", { method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-Sizeup-Action": "adapter-password" }, body });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.version) { said = `Saved v${d.version}`; if (window.UI && UI.toast) UI.toast(said); }
      else said = (d.error || `HTTP ${r.status}`) + (d.maybe_saved ? " · may have saved: check the version" : "");
    } catch (e) { said = "No answer: check the version before saving again"; }
    saving = false;
    confirmBox.checked = false;
    poll();
  }

  async function poll() {
    clearTimeout(timer);
    timer = null;
    if (busy || !isOpen()) return;
    busy = true;
    let again = true;
    try {
      const r = await fetch("/api/cad/adapter-password", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (r.status === 403 || r.status === 404 || r.status === 409) { again = false; card.hidden = true; }
      else if (r.ok) { last = await r.json(); card.hidden = false; render(); }
    } catch (_) { /* the next poll tries again */ }
    busy = false;
    if (again && isOpen()) timer = setTimeout(poll, POLL_MS);
  }

  // health.js, each mount of Settings > Health: the card (made once), then the poll.
  function mount(node, before) {
    if (!card || !node.contains(card)) {
      card = el("div", "card s-card hl-card");
      card.hidden = true;                    // until the hub answers (not the hub, not an admin: stays hidden)
      build();
      card.append(status, form);
      node.insertBefore(card, before || null);
    }
    render();
    poll();
  }

  document.addEventListener("visibilitychange", () => { if (isOpen() && !timer) poll(); });
  return { mount };
})();
window.AdapterPasswordCard = AdapterPasswordCard;
