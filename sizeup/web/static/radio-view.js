// The CAD's Radio view (/cad#radio?unit=&channel=&event=&type=&pcfd=1&since=&until=&q=): every
// transmission the station transcribed and every note tagged with a unit, newest first, with its rig,
// radio ID, channel, the incidents it is on, the transcript and its audio (kept 12 h). It replaced the
// separate Comms page (user, 2026-10-08: "i dont think we need two comms and monitor pages, but the
// format of the monitor page on the right with how certain channels are up front and obvious and the
// others are hidden feels better"): Main (FG1, Fire/EMS 15, every talkgroup marked Main in Settings >
// Radios, and the notes) up front; every other talkgroup folded into one row each under Other
// talkgroups, opened on a click. The pinned 60 Control feed stays at the top of the units column.
//
// Filters live in the URL hash, so a link from an event (#radio?event=26-000012) or the feed's More
// (#radio?channel=Fire/EMS 15) opens filtered. Polls /api/cad/comms every 2 s while open (unchanged:
// cheap, ?rev). Playing, re-transcribing (admin) and "+ Event" (dispatchers: POST
// /api/cad/incident/<id>/link) are the same calls as the incident's Radio card and the feed. Uses
// cad.js's helpers (el, syncList, radioRow, api, operator ...): loaded after it.
//
//   RadioLive: the Live / Sound button (this view, and the wall board): plays each new Main
//   transmission (and, on the board, each new page) as it arrives, oldest first.
"use strict";

window.RadioLive = (() => {
  const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
  const MAX_QUEUE = 15;
  const st = { on: false, audio: new Audio(), seen: null, queue: [], dropped: 0, blocked: false, playing: null, buttons: [], label: "Live" };

  function paint() {
    for (const b of st.buttons) {
      b.classList.toggle("active", st.on);
      b.classList.toggle("blocked", st.on && st.blocked);
      let t = st.on ? `${b.dataset.label} on` : b.dataset.label;
      if (st.on && st.blocked) t += ": tap for sound";
      else if (st.on && st.queue.length) t += ` (${st.queue.length})`;
      if (b.textContent !== t) b.textContent = t;
      b.setAttribute("aria-pressed", String(st.on));
      b.title = st.on ? "Playing each new transmission as it comes in (a few seconds behind the air)."
        + (st.dropped ? ` Skipped ${st.dropped} to catch up.` : "") + " Click to stop."
        : "Play each new transmission as it comes in";
    }
  }
  function pump() {
    if (!st.on || st.playing || !st.queue.length) return paint();
    st.playing = st.queue.shift();
    st.audio.src = `/api/audio/${encodeURIComponent(st.playing)}.wav`;
    st.audio.play().then(() => { st.blocked = false; paint(); })
      .catch(() => { st.blocked = true; st.playing = null; paint(); });
    paint();
  }
  function done() {
    if (st.audio.src.startsWith("data:")) return;
    st.playing = null;
    pump();
  }
  st.audio.addEventListener("ended", done);
  st.audio.addEventListener("error", done);

  function toggle() {
    if (st.on && !st.blocked) {                    // off
      st.on = false;
      st.queue = [];
      st.audio.pause();
      st.playing = null;
      return paint();
    }
    if (!st.on) { st.on = true; st.dropped = 0; st.seen = null; }   // from now on, not the backlog
    st.audio.src = SILENT_WAV;                     // unlock sound with this click (Safari)
    st.audio.play().then(() => { st.blocked = false; pump(); }).catch(() => { st.blocked = true; paint(); });
    paint();
  }

  // button(b, label): one more button that turns it on and off
  function button(b, label) {
    b.dataset.label = label;
    st.buttons.push(b);
    b.addEventListener("click", toggle);
    paint();
  }
  // offer(items): what is on screen now [{id, start, has, keyup}]; the new ones are queued
  function offer(items) {
    if (!st.on) return;
    if (st.seen === null) { st.seen = new Set(items.map((x) => x.id)); return; }
    const fresh = items.filter((x) => !st.seen.has(x.id));
    for (const x of fresh) st.seen.add(x.id);
    const add = fresh.filter((x) => x.has !== false && !x.keyup).sort((a, b) => a.start - b.start);   // key-ups not played
    if (!add.length) return;
    st.queue.push(...add.map((x) => x.id));
    while (st.queue.length > MAX_QUEUE) { st.queue.shift(); st.dropped++; }
    if (st.seen.size > 2000) st.seen = new Set(items.map((x) => x.id));
    pump();
  }
  return { button, offer, get on() { return st.on; } };
})();

window.RadioView = (() => {
  const POLL_MS = 2000;
  const OTHER_KEY = "sizeup.cad.radio.other";
  const FIELDS = ["q", "unit", "channel", "event", "type", "pcfd", "since", "until"];
  const st = { open: false, rev: "", rows: [], facets: "", busy: false, again: false, timer: 0, lastOk: 0, error: "",
               other: { open: false, tgs: new Set() }, pick: null, linking: new Set(), qTimer: 0 };
  try {
    const v = JSON.parse(localStorage.getItem(OTHER_KEY) || "null");
    if (v) { st.other.open = !!v.open; st.other.tgs = new Set((v.tgs || []).map(String)); }
  } catch (_) { /* start folded */ }
  const saveOther = () => {
    try { localStorage.setItem(OTHER_KEY, JSON.stringify({ open: st.other.open, tgs: [...st.other.tgs] })); } catch (_) { /* fine */ }
  };
  const f = (k) => $(`#rv-${k}`);

  // ---- filters <-> the hash -----------------------------------------------------------------------

  function filters() {
    const out = {};
    for (const k of FIELDS) {
      const n = f(k);
      out[k] = k === "pcfd" ? (n.checked ? "1" : "") : k === "since" || k === "until" ? (fromLocalInput(n.value) || "") : n.value.trim();
    }
    return out;
  }
  const active = () => Object.values(filters()).some((v) => v !== "" && v !== null);
  function query(withRev) {
    const q = new URLSearchParams(), v = filters();
    for (const k of FIELDS) if (v[k] !== "" && v[k] !== null) q.set(k, String(v[k]));
    if (withRev && st.rev) q.set("rev", st.rev);
    return q;
  }
  function pickOption(sel, v) {                    // a value not in the list yet (a link): add it
    if (v && ![...sel.options].some((o) => o.value === v)) sel.append(new Option(v, v));
    sel.value = v || "";
  }
  function fromHash(qs) {
    const q = new URLSearchParams(qs);
    f("q").value = q.get("q") || "";
    for (const k of ["unit", "channel", "event"]) pickOption(f(k), q.get(k) || "");
    f("type").value = q.get("type") || "";
    f("pcfd").checked = q.get("pcfd") === "1";
    f("since").value = toLocalInput(Number(q.get("since")) || 0);
    f("until").value = toLocalInput(Number(q.get("until")) || 0);
  }
  function changed() {
    const q = query(false).toString();
    history.replaceState(null, "", "#radio" + (q ? `?${q}` : ""));
    st.rev = "";
    poll();
  }

  function fillFacets(d) {
    const key = JSON.stringify([d.rigs, d.channels, d.events, d.pcfd]);
    if (st.facets === key) return;
    st.facets = key;
    const keep = (sel, opts, first) => {
      const v = sel.value;
      sel.replaceChildren(new Option(first, ""), ...opts.map(([val, text]) => new Option(text, val)));
      pickOption(sel, v);
    };
    const pcfd = new Set(d.pcfd || []);
    keep(f("unit"), (d.rigs || []).slice().sort((a, b) => (pcfd.has(b) - pcfd.has(a)) || a.localeCompare(b, undefined, { numeric: true }))
      .map((u) => [u, pcfd.has(u) ? u : `${u} (not PCFD)`]), "Any unit");
    keep(f("channel"), (d.channels || []).map((x) => [x, x]), "Any channel");
    keep(f("event"), (d.events || []).map((e) => [e.number || String(e.id), `${e.number || "#" + e.id} ${e.type || ""} ${e.address || ""}`.trim()]),
         "Any incident");
  }

  // ---- rows ---------------------------------------------------------------------------------------

  const openEvents = () => incidents().filter((i) => ["queued", "pending", "active"].includes(i.status));

  function evChip(e) {                             // an incident it is on: click to see only that one
    const b = el("button", "feed-ev rv-ev" + (e.why === "manual" ? " manual" : ""), e.number || `#${e.id}`);
    b.type = "button";
    b.dataset.event = e.number || String(e.id);
    b.title = `${typeLabel(e.type, e.subtype) || e.type || ""} ${e.address || ""}`.trim()
      + (e.why ? `, ${e.why === "manual" ? `added${e.unit ? ` by ${e.unit}` : ""}` : `linked by ${e.why}${e.unit ? ` (${e.unit})` : ""}`}` : "");
    return b;
  }
  function addBox(t) {                             // "+ Event": which open incident it goes on
    const span = el("span", "feed-acts");
    if (st.pick === t.id) {
      const form = el("form", "rv-pick row");
      const sel = el("select");
      const on = new Set((t.events || []).map((e) => e.id));
      const opts = openEvents().filter((i) => !on.has(i.id));
      const cur = selected();
      for (const i of opts) sel.append(new Option(`${incTitle(i)} ${typeLabel(i.type, i.subtype) || "No type"}, ${i.address || ""}`, String(i.id)));
      if (cur && opts.includes(cur)) sel.value = String(cur.id);
      const ok = el("button", "mini primary", "Add");
      ok.type = "submit";
      ok.disabled = !opts.length;
      const no = el("button", "mini", "Cancel");
      no.type = "button";
      no.addEventListener("click", () => { st.pick = null; render(); });
      form.addEventListener("submit", (e) => { e.preventDefault(); link(t, Number(sel.value)); });
      form.append(opts.length ? sel : el("span", "dim small", "No open incident"), ok, no);
      span.append(form);
      return span;
    }
    const b = el("button", "mini rv-add", st.linking.has(t.id) ? "…" : "+ Event");
    b.type = "button";
    b.title = "Put it on an incident";
    b.disabled = st.linking.has(t.id) || !openEvents().length;
    b.addEventListener("click", () => { st.pick = t.id; render(); });
    span.append(b);
    return span;
  }
  async function link(t, iid) {
    if (!iid) return;
    st.pick = null;
    st.linking.add(t.id);
    render();
    try {
      await api("POST", `/api/cad/incident/${iid}/link`, { traffic_id: t.id, operator: operator() });
    } catch (err) {
      UI.toast(`Add failed: ${err.message}`, "err");
    } finally {
      st.linking.delete(t.id);
      st.rev = "";
      poll();
    }
  }

  function noteRow(t) {
    const li = el("li", "rt note");
    const meta = el("div", "meta");
    meta.append(el("span", "t", fmtTime(t.start, true)), el("span", "ek note", "note"));
    for (const u of (t.rig || "").split(/,\s*/).filter(Boolean)) meta.append(el("span", "unit-tag", u));
    if (t.operator) meta.append(opSpan(t.operator));
    const acts = el("span", "feed-acts");
    for (const e of t.events || []) acts.append(evChip(e));
    meta.append(acts);
    li.append(meta, el("div", "txt", t.text || ""));
    return li;
  }
  function row(t) {
    if (t.kind === "note") return noteRow(t);
    const li = radioRow(t);                        // cad.js: play, re-transcribe, time, rig, channel, text
    const meta = li.querySelector(".meta");
    if (t.talkgroup !== null && t.talkgroup !== undefined) meta.querySelector(".chip.src").title = `TG ${t.talkgroup}`;
    const acts = addBox(t);
    for (const e of (t.events || []).slice().reverse()) acts.prepend(evChip(e));
    meta.append(acts);
    return li;
  }
  const rowSig = (t) => JSON.stringify([t.text, t.status, t.model, t.alt_text, t.alt_model, t.rig, t.has_audio, (t.events || []).map((e) => e.id), st.pick === t.id,
                                       st.linking.has(t.id), talkSig(t.rig), openEvents().length]);
  const fillRow = (li, t) => {
    const n = row(t);
    li.className = n.className;
    li.replaceChildren(...n.childNodes);
  };

  // ---- Main up front, the other talkgroups folded -------------------------------------------------

  function otherGroups(rows) {
    const by = new Map();
    for (const t of rows) {
      const k = String(t.talkgroup ?? t.channel);
      if (!by.has(k)) by.set(k, { key: k, name: t.channel, tg: t.talkgroup, calls: [] });
      by.get(k).calls.push(t);
    }
    const groups = [...by.values()];
    for (const g of groups) {
      g.last = g.calls[0].start;
      const said = g.calls.find((t) => t.text);
      g.snippet = said ? said.text : g.calls[0].status === "no_speech" ? "Key-up, no speech" : "—";
      g.muted = !said;
    }
    return groups.sort((a, b) => b.last - a.last);
  }

  function render() {
    if (!st.open) return;
    const rows = st.rows;
    const main = rows.filter((t) => t.main !== false);
    const rest = rows.filter((t) => t.main === false);
    const nr = rows.filter((t) => t.kind !== "note").length;
    setText($("#rv-count"), st.error ? `Load failed: ${st.error}`
      : `${nr} TX${rows.length - nr ? `, ${rows.length - nr} note${rows.length - nr === 1 ? "" : "s"}` : ""}${rows.length >= 300 ? " (newest 300)" : ""}`);
    const chans = [...new Set(main.filter((t) => t.kind !== "note").map((t) => t.channel))];
    setText($("#rv-main-sum"), chans.join(" · "));
    syncList($("#rv-main"), main, { key: (t) => t.id, sig: rowSig, make: () => el("li"), fill: fillRow,
                                    empty: active() ? "None match" : "None" });
    RadioLive.offer(main.filter((t) => t.kind !== "note").map((t) => ({ id: t.id, start: t.start, has: t.has_audio, keyup: t.status === "no_speech" })));

    const groups = otherGroups(rest);
    const open = st.other.open || (active() && rest.length > 0);
    const sec = $("#rv-other-sec"), box = $("#rv-other"), tog = $("#rv-other-toggle");
    sec.classList.toggle("open", open);
    tog.setAttribute("aria-expanded", String(open));
    setText($("#rv-other-sum"), rest.length ? `${rest.length} TX, ${groups.length} TG${groups.length === 1 ? "" : "s"}` : "None");
    show(box, open);
    if (!open) return;
    const few = groups.length <= 2 && active();    // filtered down to a talkgroup or two: open them
    syncList(box, groups, {
      key: (g) => g.key,
      sig: (g) => JSON.stringify([st.other.tgs.has(g.key) || few, g.name, g.calls.length, g.last, g.snippet,
                                  (st.other.tgs.has(g.key) || few) ? g.calls.map(rowSig) : 0, g.calls.map((t) => t.id)]),
      make: () => {
        const li = el("li", "og");
        const head = el("button", "og-head");
        head.type = "button";
        head.append(el("span", "caret"), el("b", "og-name"), el("span", "og-n"), el("span", "spacer"), el("span", "og-last"),
                    el("span", "og-snip"));
        li.append(head, el("ol", "list og-calls radio-log"));
        return li;
      },
      fill: (li, g) => {
        const isOpen = st.other.tgs.has(g.key) || few;
        const [head, calls] = li.children;
        const [caret, name, num, , last, snip] = head.children;
        li.className = isOpen ? "og open" : "og";
        head.setAttribute("aria-expanded", String(isOpen));
        setText(caret, isOpen ? "▾" : "▸");
        setText(name, g.name);
        setText(num, `${g.calls.length} TX` + (g.tg !== null && g.tg !== undefined ? ` · TG ${g.tg}` : ""));
        setText(last, fmtTime(g.last));
        setText(snip, g.snippet);
        snip.className = g.muted ? "og-snip muted" : "og-snip";
        show(calls, isOpen);
        if (isOpen) syncList(calls, g.calls, { key: (t) => t.id, sig: rowSig, make: () => el("li"), fill: fillRow, empty: "None" });
        else if (calls.children.length) calls.replaceChildren();
      },
      empty: "None",
    });
  }

  // ---- polling ------------------------------------------------------------------------------------

  async function poll() {
    clearTimeout(st.timer);
    if (!st.open) return;
    if (st.busy) { st.again = true; return; }      // a filter changed mid-request: ask again when it's back
    st.busy = true;
    st.again = false;
    const asked = query(false).toString();
    try {
      const d = await api("GET", `/api/cad/comms?${query(true)}`);
      st.lastOk = Date.now();
      st.error = "";
      if (asked !== query(false).toString()) st.again = true;   // stale: the filters moved on
      else if (!d.unchanged) {
        st.rev = d.rev;
        st.rows = d.rows || [];
        fillFacets(d);
      }
    } catch (e) {
      st.error = e.message;
    } finally {
      st.busy = false;
      render();
      if (st.again) { st.rev = ""; poll(); } else if (st.open) st.timer = setTimeout(poll, POLL_MS);
    }
  }

  function open(qs) {
    const was = st.open;
    st.open = true;
    if (!was || qs !== query(false).toString()) { fromHash(qs); st.rev = ""; }
    poll();
  }
  function close() {
    st.open = false;
    clearTimeout(st.timer);
  }

  // ---- wiring -------------------------------------------------------------------------------------

  for (const k of ["unit", "channel", "event", "type", "pcfd", "since", "until"]) f(k).addEventListener("change", changed);
  f("q").addEventListener("input", () => { clearTimeout(st.qTimer); st.qTimer = setTimeout(changed, 350); });
  $("#rv-filters").addEventListener("submit", (e) => { e.preventDefault(); clearTimeout(st.qTimer); changed(); });
  $("#rv-clear").addEventListener("click", () => {
    $("#rv-filters").reset();
    for (const k of ["unit", "channel", "event"]) f(k).value = "";
    changed();
  });
  $("#rv-other-toggle").addEventListener("click", () => { st.other.open = !$("#rv-other-sec").classList.contains("open"); saveOther(); render(); });
  $("#rv-other").addEventListener("click", (e) => {
    const head = e.target.closest(".og-head");
    if (!head) return;
    const k = head.parentElement.dataset.key;
    if (st.other.tgs.has(k)) st.other.tgs.delete(k); else st.other.tgs.add(k);
    saveOther();
    render();
  });
  $("#radio-view").addEventListener("click", (e) => {
    const b = e.target.closest("[data-event]");
    if (!b) return;
    pickOption(f("event"), b.dataset.event);
    changed();
  });
  RadioLive.button($("#rv-live"), "Live");
  cad.hooks.push(() => { if (st.open && (st.pick !== null || st.linking.size)) render(); });   // the open incidents changed

  return { open, close, render };
})();
