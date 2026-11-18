// Settings > Station > Stations (sizeup/cloud/stations.py): each station's own token, admins, the hub only.
// GET /api/cad/stations: the stations (id, name, last used, revoked) and the shared token's state; never a token or a
// hash. POST /api/cad/stations {action: add | rename | rotate | revoke | retire_shared | restore_shared | shared_id}:
// shared_id while its station is live answers 409 {error: the question, confirm: true}: asked, then sent with confirm
// (audited as confirmed). The shared token's former ids ("aliases") are still taken from it. add and rotate
// answer the new token and its status token, shown here ONCE (kept in this page's memory until Done, never stored) with
// a copy button. Its own card and poll, so typing is never wiped by another panel's render.
"use strict";
const StationsCard = (() => {
  const POLL_MS = 15000;
  const LIVE_S = 30;                       // used this recently: "now"
  let card = null, status = null, form = null, reveal = null, timer = null, busy = false, last = null, said = "";
  let idIn = null, nameIn = null, addBtn = null, hint = null, fresh = null, editing = "";

  const when = (t) => (t ? fmtTime(t, true) : "—");
  const isOpen = () => !!card && card.isConnected && !(card.parentNode && card.parentNode.closest("[hidden]")) && !document.hidden;
  const ago = (t) => (t && last && last.at - t < LIVE_S ? "now" : when(t));

  async function post(body) {
    const r = await fetch("/api/cad/stations", { method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(d.error || `HTTP ${r.status}`), { confirm: d.confirm === true });
    return d;
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); } catch (_) {
      const ta = el("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.className = "sr-only";
      document.body.append(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (__) { /* nothing more to try */ }
      ta.remove();
    }
    if (window.UI && UI.toast) UI.toast("Copied", "ok");
  }

  function btn(label, cls, fn) {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
  }

  async function act(body, done) {
    said = "";
    try {
      const d = await post(body);
      if (d.token) fresh = { id: d.id, token: d.token, status: d.status_token, rotated: body.action === "rotate", grace: d.grace_s };
      if (done) said = done;
    } catch (e) { said = e.message; }
    poll(true);
  }

  function build() {
    status = el("div");
    reveal = el("div");
    form = el("div", "row");
    const l1 = el("label", "field");
    idIn = el("input");
    idIn.maxLength = 40;
    idIn.spellcheck = false;
    idIn.title = "[station] id in the station's config: lower case, digits, - and _. Until multi-station, the hub takes "
      + "only the shared token's station: add that id; another station's posts are refused";
    l1.append("Id", idIn);
    const l2 = el("label", "field");
    nameIn = el("input");
    nameIn.maxLength = 60;
    l2.append("Name", nameIn);
    addBtn = btn("Add", "primary", () => {
      const id = idIn.value.trim();
      if (!id) { said = "Id?"; render(); return; }
      act({ action: "add", id, name: nameIn.value.trim() }, "");
      idIn.value = ""; nameIn.value = "";
    });
    form.append(l1, l2, addBtn);
    hint = el("p", "dim small", "One station until multi-station: another id is refused");
    hint.title = "Per-station state, outbox and jobs come with the multi-station work. Until then the hub takes only the "
      + "shared token's station; Add is for that station's own token";
  }

  function renderReveal() {
    if (!fresh) { reveal.replaceChildren(); return; }
    const f = fresh;
    const box = el("div", "notice ok");
    const head = el("p", "", `${f.id}: new token · shown once`);
    head.title = "Not stored on the hub (only its hash). Put it in the station's /etc/sizeup/station.env as SIZEUP_STATION_TOKEN"
      + " (sudo sizeup-station secrets), then restart the station" + (f.rotated && f.grace ? `. The old one works ${Math.round(f.grace / 60)} min more` : "");
    const tok = el("p", "mono", f.token);
    const st = el("p", "mono dim small", f.status);
    st.title = "Status token: read only (GET /api/station/status, the deploy check's SIZEUP_STATUS_TOKEN)";
    const row = el("div", "row");
    row.append(btn("Copy token", "primary", () => copy(f.token)), btn("Copy status token", "", () => copy(f.status)),
      btn("Done", "", () => { fresh = null; render(); }));
    box.append(head, tok, st);
    if (f.rotated && f.grace) {
      box.append(el("p", "small", `New status token too: update GitHub SIZEUP_STATUS_TOKEN within ${Math.round(f.grace / 60)} min`));
    }
    box.append(row);
    reveal.replaceChildren(box);
  }

  function nameCell(s) {
    const td = el("td", "");
    if (editing !== s.id) { td.textContent = s.name || "—"; return td; }
    const i = el("input");
    i.value = s.name || "";
    i.maxLength = 60;
    const save = () => { editing = ""; act({ action: "rename", id: s.id, name: i.value.trim() }, ""); };
    i.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); save(); }
      if (e.key === "Escape") { editing = ""; render(); }
    });
    td.append(i, btn("Save", "sm", save));
    setTimeout(() => i.focus(), 0);
    return td;
  }

  function render() {
    if (!card) return;
    const d = last;
    renderReveal();
    if (!d) { status.replaceChildren(el("p", "dim small", "Loading…")); return; }
    const head = el("div", "card-head");
    head.append(el("h2", "", "Stations"));
    head.append(el("span", "spacer"));
    if (window.Help && Help.info) {
      head.append(Help.info("Each station has its own token (sz1.<id>.…); the hub keeps only its hash. Rotate: a new token, "
        + `the old one works ${Math.round((d.grace_s || 600) / 60)} min more. Revoke: refused from its next request. `
        + "The shared token is one station until it is retired. Set id renames it: the station takes the new id from the "
        + "hub, its old id is still taken and its history moves; asked first while it's connected. "
        + "docs/cloud.md: How station authentication works", "About"));
    }
    const t = el("table", "evtable");
    const hr = el("tr");
    for (const h of ["Name", "Id", "Token", "Last seen", ""]) hr.append(el("th", "", h));
    t.append(el("thead"), el("tbody"));
    t.tHead.append(hr);
    for (const s of d.stations || []) {
      const tr = el("tr");
      const tokenWord = s.revoked ? `revoked ${when(s.revoked)}` : s.grace_until ? `rotated · old until ${when(s.grace_until)}` : "own";
      const acts = el("td", "");
      if (!s.revoked && s.pool_whisper !== undefined) {   // its whisper in the STT pool (#639 phase 2), off by default
        const pw = btn(s.pool_whisper ? "Whisper in pool: on" : "Whisper in pool: off", "sm", () =>
          act({ action: "pool_whisper", id: s.id, on: !s.pool_whisper }, ""));
        pw.title = "This station's whisper takes other stations' clips while it has none of its own";
        acts.append(pw);
      }
      if (!s.revoked) {
        acts.append(btn("Rename", "sm", () => { editing = s.id; render(); }));
        acts.append(btn("Rotate", "sm", async () => {
          if (await UI.confirm(`New token and status token for ${s.id}? The old ones work ${Math.round((d.grace_s || 600) / 60)} more min.`,
            { ok: "Rotate", danger: false })) act({ action: "rotate", id: s.id }, "");
        }));
        acts.append(btn("Revoke", "sm danger", async () => {
          if (await UI.confirm(`Revoke ${s.id}? Refused from its next request.`, { ok: "Revoke" })) act({ action: "revoke", id: s.id }, `${s.id} revoked`);
        }));
      } else {
        acts.append(btn("New token", "sm", () => act({ action: "rotate", id: s.id }, "")));
      }
      tr.append(nameCell(s), el("td", "mono", s.id), el("td", s.revoked ? "hl-bad" : "", tokenWord),
        el("td", "t", ago(s.last_used)), acts);
      t.tBodies[0].append(tr);
    }
    const sh = d.shared || {};
    if (sh.configured) {
      const tr = el("tr");
      const word = sh.retired ? `retired ${when(sh.retired)}` : "shared";
      const acts = el("td", "");
      if (sh.retired) {
        acts.append(btn("Restore", "sm", () => act({ action: "restore_shared" }, "Shared token restored")));
      } else {
        acts.append(btn("Retire", "sm danger", async () => {
          const ask = sh.own_token ? `Retire the shared token? ${sh.station || "Its station"} must be on its own token.`
            : `Retire the shared token? ${sh.station || "Its station"} has no own token yet: it goes offline.`;
          if (!await UI.confirm(ask, { ok: "Retire" })) return;
          said = "";
          try { await post({ action: "retire_shared" }); said = "Shared token retired"; } catch (e) {
            if (/used \d+ s ago/.test(e.message) && await UI.confirm(`${e.message}. Retire anyway?`, { ok: "Retire" })) {
              try { await post({ action: "retire_shared", force: true }); said = "Shared token retired"; } catch (e2) { said = e2.message; }
            } else said = e.message;
          }
          poll(true);
        }));
      }
      const tw = el("td", sh.retired ? "dim" : "hl-warn", word);
      tw.title = "SIZEUP_STATION_TOKEN on the hub (SSM). " + (sh.pinned_by === "config" ? "Station from [cloud] legacy_station_id"
        : sh.pinned_by === "hub.sqlite" ? "Station set here (or learned from its first post)" : "No station has used it yet");
      // the shared token's station id: fixed here when the config's (or a learned one) is wrong. Empty = back to the
      // config's, or learned from the next post
      const idCell = el("td", "mono");
      if (editing === "\0shared") {
        const i = el("input");
        i.value = sh.station || "";
        i.maxLength = 64;
        i.spellcheck = false;
        i.title = "The station's [station] id. Empty: back to [cloud] legacy_station_id" + (sh.config ? ` (${sh.config})` : "");
        const save = async () => {
          editing = "";
          const body = { action: "shared_id", id: i.value.trim() };
          said = "";
          try { await post(body); said = "Shared token's station set"; } catch (e) {
            // its station is connected: the hub's question, asked here, then sent again with confirm (audited)
            if (e.confirm && await UI.confirm(e.message, { ok: "Change", danger: true })) {
              try { await post({ ...body, confirm: true }); said = "Shared token's station set"; } catch (e2) { said = e2.message; }
            } else if (!e.confirm) said = e.message;
          }
          poll(true);
        };
        i.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") { editing = ""; render(); }
        });
        idCell.append(i, btn("Save", "sm", save));
        setTimeout(() => i.focus(), 0);
      } else {
        idCell.textContent = sh.station || "—";
        if ((sh.aliases || []).length) {
          const al = el("span", "dim small", ` · was ${sh.aliases.join(", ")}`);
          al.title = "Former ids, still taken from the shared token as this one (a station not yet reconfigured)";
          idCell.append(al);
        }
        acts.prepend(btn("Set id", "sm", () => { editing = "\0shared"; render(); }));
      }
      tr.append(el("td", "dim", "Shared token"), idCell, tw, el("td", "t", ago(sh.last_used)), acts);
      t.tBodies[0].append(tr);
    }
    if (d.mismatch) {
      const m = d.mismatch;
      const tr = el("tr");
      const w = el("td", "hl-bad", "rejected");
      w.title = `id mismatch: the station says ${m.said || "?"}, its token is ${m.token}. Set id here, or its [station] id`;
      tr.append(el("td", "dim", "Id mismatch"), el("td", "mono", m.said || "?"), w, el("td", "t", ago(m.at)), el("td", ""));
      t.tBodies[0].append(tr);
    }
    for (const [id, at] of Object.entries(d.others || {})) {
      const tr = el("tr");
      const w = el("td", "hl-bad", "refused");
      w.title = "Multi-station isn't on yet: this hub takes one station (the shared token's)";
      tr.append(el("td", "dim", "Other station"), el("td", "mono", id), w, el("td", "t", ago(at)), el("td", ""));
      t.tBodies[0].append(tr);
    }
    if (!(d.stations || []).length && !sh.configured) {
      const tr = el("tr");
      tr.append(Object.assign(el("td", "empty", "None"), { colSpan: 5 }));
      t.tBodies[0].append(tr);
    }
    const kids = [head, t];
    if (said) kids.push(el("p", "dim small", said));
    kids.push(el("p", "dim small", `hub.sqlite · ${when(d.at)}`));
    status.replaceChildren(...kids);
    // the shared token's station has no own token yet: its id is the one to add (it must be exactly that); until
    // multi-station another id is refused, so nothing else is suggested
    idIn.placeholder = sh.station && !sh.own_token && /^[a-z0-9][a-z0-9_-]{0,39}$/.test(sh.station) ? sh.station : "";
  }

  async function poll(now) {
    clearTimeout(timer);
    timer = null;
    if ((busy && !now) || !isOpen()) return;
    busy = true;
    let again = true;
    try {
      const r = await fetch("/api/cad/stations", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (r.status === 403 || r.status === 404 || r.status === 409) { again = false; card.hidden = true; }
      else if (r.ok) { last = await r.json(); card.hidden = false; if (!editing) render(); }
    } catch (_) { /* the next poll tries again */ }
    busy = false;
    if (again && isOpen()) timer = setTimeout(poll, POLL_MS);
  }

  // settings.js, each render of Settings > Station > Stations: the card (made once), then the poll.
  function mount(node) {
    if (!card || !node.contains(card)) {
      card = el("div", "card s-card");
      card.hidden = true;                    // until the hub answers (not the hub, not an admin: stays hidden)
      build();
      card.append(status, reveal, form, hint);
      node.replaceChildren(card);
    }
    render();
    poll();
  }

  document.addEventListener("visibilitychange", () => { if (isOpen() && !timer) poll(); });
  return { mount };
})();
window.StationsCard = StationsCard;
