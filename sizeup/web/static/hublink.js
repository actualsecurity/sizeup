// The hub link card (sizeup/cloud/stationmode.py): Forward to hub on / off, the hub URL, Test connection,
// the link's status, its last contact and what waits in the outbox, and where each secret came from (never
// a value). One card for the CAD's Settings > System > Station (settings.js) and the station page's
// Settings (station.js). Not on the hub: GET /api/station-mode answers 409 there and the card stays hidden.
//
//   HubLink.mount(card, deps)   card: an empty .card; deps: {title, operator() -> name, askOperator(), msg(text, ok),
//                               changed() (after a switch or a URL save), offText (the confirm when turning
//                               it off), time(unix) -> "14:03:21", onState(d) (every load)}
//   HubLink.refresh()           GET /api/station-mode again (every 5 s while the card is on screen)
"use strict";
window.HubLink = (() => {
  const SECRETS = [["token", "Token"], ["access_id", "Access id"], ["access_secret", "Access secret"]];
  const st = { card: null, deps: {}, data: null, busy: false, test: null, timer: 0 };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function setText(node, text) {
    text = text ?? "";
    if (node.textContent !== text) node.textContent = text;
  }
  function button(text, title, fn, cls = "") {
    const b = el("button", cls, text);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", (e) => { e.preventDefault(); fn(); });
    return b;
  }
  const msg = (text, ok) => (st.deps.msg ? st.deps.msg(text, ok) : UI.toast(text, ok ? "ok" : "err"));
  const hhmmss = (t) => (st.deps.time ? st.deps.time(t)
    : new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }));
  function ago(t) {
    const s = Math.max(0, Date.now() / 1000 - t);
    return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`;
  }

  function mount(card, deps = {}) {
    st.card = card;
    st.deps = deps;
    delete card.dataset.built;
    refresh();
  }

  async function refresh() {
    clearTimeout(st.timer);
    const card = st.card;
    if (!card || !card.isConnected) return;
    let r = null, d = null;
    try {
      r = await fetch("/api/station-mode", { cache: "no-store" });
      d = await r.json();
    } catch (e) { /* said below */ }
    if (r && (r.status === 409 || r.status === 404)) { card.hidden = true; return; }   // the hub, or no switch here
    card.hidden = false;
    if (!r || !r.ok || !d) {
      card.replaceChildren(head(), el("div", "error", (d && d.error) || "Could not load the hub link"));
      delete card.dataset.built;
    } else {
      st.data = d;
      if (!card.dataset.built) build(card);
      fill(card, d);
      if (st.deps.onState) st.deps.onState(d);
    }
    schedule();
  }
  function schedule() {           // every 5 s while the card is on screen; off screen, nothing is asked
    clearTimeout(st.timer);
    st.timer = setTimeout(() => {
      if (!st.card || !st.card.isConnected) return;
      if (st.card.offsetParent !== null && !document.hidden && !st.busy) refresh();
      else schedule();
    }, 5000);
  }

  function head() {
    const h = el("div", "card-head");
    h.append(el("h2", "", st.deps.title || "Hub link"), Help.button("On: what this station hears also goes to the hub, which is the CAD "
      + "of record. Off: nothing is sent; radios and speech-to-text keep running, and what is already queued waits "
      + "here until it is back on.\nSecrets are shown only as where they came from."));
    return h;
  }

  function build(card) {
    const sw = el("button", "sw sm-sw");
    sw.type = "button";
    sw.setAttribute("role", "switch");
    sw.append(el("i", "knob"), el("span", "lbl", "Forward to hub"));
    sw.addEventListener("click", () => set({ forward: !st.data.forward }));
    const status = el("span", "sm-status");
    const url = el("input", "sm-url ro-ok");
    Object.assign(url, { type: "url", maxLength: 300, placeholder: "https://hub.example.org", spellcheck: false });
    url.setAttribute("aria-label", "Hub URL");
    url.title = "The hub's address: https:// (plain http only to this machine)";
    const save = button("Save", "Use this hub URL", () => set({ hub_url: url.value.trim() }), "sm-save");
    url.addEventListener("input", () => { save.disabled = url.value.trim() === (st.data.hub_url || ""); });
    url.addEventListener("keydown", (e) => { if (e.key === "Enter" && !save.disabled) { e.preventDefault(); save.click(); } });
    const test = button("Test connection", "The hub's /healthz, then its station path with this station's token "
      + "and Access headers. Nothing is sent.", runTest, "ro-ok sm-test");
    const result = el("span", "sm-result small");
    const row1 = el("div", "row sm-row");
    row1.append(sw, status);
    const row2 = el("div", "row sm-row");
    row2.append(el("label", "dim small sm-lab", "Hub URL"), url, save, test, result);
    const facts = el("dl", "admin sm-facts");
    const secrets = el("dl", "admin sm-secrets");
    const hint = el("p", "dim small sm-hint");
    card.replaceChildren(head(), row1, row2, facts, secrets, hint);
    card.dataset.built = "1";
  }

  function fact(label, value, cls = "", title = "") {
    const div = el("div");
    const dd = el("dd", cls, value);
    if (title) dd.title = title;
    div.append(el("dt", "", label), dd);
    return div;
  }

  function fill(card, d) {
    const sw = card.querySelector(".sm-sw");
    sw.className = "sw sm-sw" + (d.forward ? " on" : "");
    sw.setAttribute("aria-checked", String(!!d.forward));
    sw.disabled = st.busy;
    const s = d.status || {};
    const status = card.querySelector(".sm-status");
    status.className = "sm-status " + ({ ok: "ok", down: "err", blocked: "err", starting: "warn" }[s.state] || "dim");
    setText(status, s.text || "");
    status.title = [s.detail, s.last_error && `Last error: ${s.last_error}`].filter(Boolean).join("\n");
    const url = card.querySelector(".sm-url");
    if (document.activeElement !== url && url.dataset.saved !== (d.hub_url || "")) {
      url.value = d.hub_url || "";
      url.dataset.saved = d.hub_url || "";
    }
    card.querySelector(".sm-save").disabled = st.busy || url.value.trim() === (d.hub_url || "");
    card.querySelector(".sm-test").disabled = st.busy;
    const res = card.querySelector(".sm-result");
    const t = st.test;
    res.className = "sm-result small " + (t ? (t.busy ? "dim" : t.ok ? "ok" : "err") : "");
    setText(res, t ? (t.busy ? "Testing…" : (!t.ok && t.status ? `HTTP ${t.status}: ` : "") + t.text) : "");
    // last contact, the outbox, the last error: what `sizeup-station status` says
    const n = s.waiting || 0;
    card.querySelector(".sm-facts").replaceChildren(
      fact("Last contact", s.last_ok ? `${hhmmss(s.last_ok)} (${ago(s.last_ok)})` : d.forward ? "Not yet" : "—",
           s.last_ok ? "" : "dim"),
      fact("Waiting to go up", n ? `${n} event${n === 1 ? "" : "s"}` + (s.oldest ? `, oldest ${ago(s.oldest)}` : "") : "None",
           n ? "warn" : "dim", "Kept in state/outbox.sqlite until the hub has them, also across restarts"),
      ...(s.last_error ? [fact("Last error", s.last_error, "err", s.error_since ? `Since ${hhmmss(s.error_since)}` : "")] : []));
    const sec = d.secrets || {};
    card.querySelector(".sm-secrets").replaceChildren(...SECRETS.map(([k, label]) => {
      const x = sec[k] || {};
      const v = x.set ? `set (${x.source})` : x.short ? `too short (${x.source})` : k === "token" ? "missing" : "not set";
      return fact(label, v, x.set ? "" : k === "token" ? "err" : "dim",
        (x.set ? `From ${x.source === "env" ? x.env : x.source === "Keychain" ? `the Keychain (${x.keychain})` : "config.toml"}`
          : `Env ${x.env}` + (d.keychain ? `, or Keychain service ${x.keychain}` : ""))
        + (k === "token" ? "" : ". Only needed with a Cloudflare Access service token."));
    }));
    const hint = card.querySelector(".sm-hint");
    const tok = sec.token || {};
    hint.replaceChildren();
    if (!tok.set) {
      if (d.keychain) {
        hint.append("Add the token to the Keychain: ",
          el("code", "", `security add-generic-password -a sizeup -s ${tok.keychain || "sizeup-station-token"} -w`), ", then Test connection.");
      } else {
        hint.append("No Keychain on this system: set ", el("code", "", tok.env || "SIZEUP_STATION_TOKEN"),
          " (Linux: sudo sizeup-station secrets) and restart.");
      }
    } else if (d.from === "config") {
      hint.append(`From config.toml (${d.config_role}) until changed here.`);
    }
    hint.hidden = !hint.childNodes.length;
  }

  async function set(body) {
    const card = st.card;
    const op = st.deps.operator ? st.deps.operator() : "";
    if (!op) {
      msg("Enter your name (Operator, top right) first: every change records who made it.");
      if (st.deps.askOperator) st.deps.askOperator();
      return;
    }
    const url = card.querySelector(".sm-url");
    if (body.forward === true && !url.value.trim() && !st.data.hub_url) {
      msg("Enter the hub URL first.");
      url.focus();
      return;
    }
    if (body.forward === true && url.value.trim()) body.hub_url = url.value.trim();
    if (body.forward === false && !(await UI.confirm(st.deps.offText || "Stop forwarding to the hub? Anything still queued waits here.",
      { ok: "Stop forwarding" }))) return;
    st.busy = true;
    fill(card, st.data);
    try {
      const r = await fetch("/api/station-mode", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, operator: op }) });
      const d = await r.json().catch(() => null);
      if (!r.ok) throw new Error((d && d.error) || `${r.status} ${r.statusText}`);
      st.data = d;
      msg("forward" in body ? (d.forward ? "Forwarding to the hub: on." : "Forwarding to the hub: off.") : "Hub URL saved.", true);
      if (st.deps.onState) st.deps.onState(d);
    } catch (e) {
      msg(e.message);
    } finally {
      st.busy = false;
      if (card.isConnected && st.data) fill(card, st.data);
      if (st.deps.changed) st.deps.changed();
    }
  }

  async function runTest() {
    const url = st.card.querySelector(".sm-url").value.trim();
    st.test = { busy: true };
    fill(st.card, st.data);
    try {
      const r = await fetch("/api/station-mode/test", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(url ? { hub_url: url } : {}) });
      const d = await r.json().catch(() => null);
      st.test = r.ok && d ? d : { ok: false, status: r.status, text: (d && d.error) || r.statusText };
    } catch (e) {
      st.test = { ok: false, status: 0, text: e.message };
    }
    refresh();
  }

  return { mount, refresh, state: () => st.data };
})();
