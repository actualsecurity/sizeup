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
// (#radio?channel=Fire/EMS 15) opens filtered. Asks /api/cad/comms only while the view is on screen
// (#137: it went on polling every 2 s after the tab was left), and then only when the state push's
// comms_rev moves (a transmission or note logged or changed), with a slow safety poll (SAFETY_MS) and at
// once on sizeup:tx-changed; an older hub without comms_rev: every POLL_MS (?rev: cheap). Each row is cad.js's radioRow, as on the incident's Radio card and the feed: play, the
// incidents it is on (a click: only that one's traffic), its "⋯" menu (Add to event ▸, Remove from ...,
// Re-transcribe, Copy text). Uses cad.js's helpers (el, syncList, radioRow, evChip, api ...): loaded after it.
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
  const POLL_MS = 2000;                 // a hub without comms_rev in its state
  const SAFETY_MS = 30000;              // with it: in case a push was missed
  const RETRY_MS = 5000;                // after a failed ask, before the push may ask again
  const OTHER_KEY = "sizeup.cad.radio.other";
  const FIELDS = ["q", "unit", "channel", "event", "type", "pcfd", "emergency", "since", "until"];
  const CHECKS = new Set(["pcfd", "emergency"]);   // checkboxes: "1" or nothing
  const st = { open: false, rev: "", rows: [], facets: "", busy: false, again: false, timer: 0, lastOk: 0, error: "", failAt: 0,
               other: { open: false, tgs: new Set() }, qTimer: 0 };
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
      out[k] = CHECKS.has(k) ? (n.checked ? "1" : "") : k === "since" || k === "until" ? (fromLocalInput(n.value) || "") : n.value.trim();
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
    f("emergency").checked = q.get("emergency") === "1";
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
    const li = radioRow(t, { scope: "rv" });       // cad.js: play, time, rig, channel, chips, the ⋯ menu, text
    const meta = li.querySelector(".meta");
    if (t.talkgroup !== null && t.talkgroup !== undefined) meta.querySelector(".chip.src").title = `TG ${t.talkgroup}`;
    return li;
  }
  const rowSig = (t) => JSON.stringify([t.text, t.status, t.model, t.alt_text, t.alt_model, t.rig, t.has_audio,
                                       (t.events || []).map((e) => [e.id, e.why]), t.rerun_at, talkSig(t.rig), cad.txRev,
                                       emSig(t)]);
  const fillRow = (li, t) => {
    const n = row(t);
    li.className = n.className;
    li.replaceChildren(...n.childNodes);
  };

  // ---- Main up front, the other talkgroups folded -------------------------------------------------

  // The emergency on a folded talkgroup can't hide (#128, user 2026-10-14: "where is the bus emergency here?"):
  // a group with the emergency button or an alarm on it (emergency: "ours" | "other") goes first and its header says
  // so; one whose emergency window is open (the CAD state's alarms.windows) says "Recording until 20:09 (emergency)".
  const windows = () => (typeof cad !== "undefined" && cad.state && cad.state.alarms && cad.state.alarms.windows) || [];
  const isEm = (t) => !!(t.emergency || t.alarm_id);
  function otherGroups(rows, wins = windows()) {
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
      const em = g.calls.filter(isEm);
      g.emergency = !em.length ? "" : em.some((t) => t.alarm_level === "alarm") ? "ours" : "other";
      if (em.length) {                              // the snippet: the emergency, not whatever came last
        const e = em[0];
        g.snippet = `${e.rig || e.talker || e.radio_id || "Radio"}: ${e.text || "emergency button, no speech"}`;
        g.muted = false;
      }
      const w = g.tg !== null && g.tg !== undefined ? wins.find((x) => x.talkgroup === g.tg) : null;
      g.until = w ? w.until : null;
    }
    const rank = (g) => (g.emergency === "ours" ? 3 : g.emergency ? 2 : g.until ? 1 : 0);
    return groups.sort((a, b) => (rank(b) - rank(a)) || (b.last - a.last));
  }
  const emText = (g) => (g.emergency === "ours" ? "EMERGENCY" : "Emergency (not ours)");

  // A page going out now (South 2), over Main until its finished row is in the list.
  function renderPaging() {
    Radio.fillPaging($("#rv-paging"), cad.state && cad.state.paging, new Set(st.rows.map((t) => t.id)));
  }

  function render() {
    if (!st.open) return;
    renderPaging();
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
    const emg = groups.filter((g) => g.emergency);       // the whole section folded: its head says so too
    const badge = $("#rv-other-em");
    if (badge) {
      show(badge, emg.length > 0);
      badge.classList.toggle("em-other", emg.length > 0 && !emg.some((g) => g.emergency === "ours"));
      setText(badge, emg.length ? `${emText(emg[0])}: ${emg.map((g) => g.name).join(", ")}` : "");
    }
    show(box, open);
    if (!open) return;
    const few = groups.length <= 2 && active();    // filtered down to a talkgroup or two: open them
    syncList(box, groups, {
      key: (g) => g.key,
      sig: (g) => JSON.stringify([st.other.tgs.has(g.key) || few, g.name, g.calls.length, g.last, g.snippet, g.emergency, g.until,
                                  (st.other.tgs.has(g.key) || few) ? g.calls.map(rowSig) : 0, g.calls.map((t) => t.id)]),
      make: () => {
        const li = el("li", "og");
        const head = el("button", "og-head");
        head.type = "button";
        head.append(el("span", "caret"), el("b", "og-name"), el("span", "og-n"), el("span", "og-flags"), el("span", "og-last"),
                    el("span", "og-snip"));
        li.append(head, el("ol", "list og-calls radio-log"));
        return li;
      },
      fill: (li, g) => {
        const isOpen = st.other.tgs.has(g.key) || few;
        const [head, calls] = li.children;
        const [caret, name, num, flags, last, snip] = head.children;
        const fl = [];
        if (g.emergency) fl.push(el("span", `chip emergency${g.emergency === "ours" ? "" : " em-other"}`, emText(g)));
        if (g.until) {
          const u = el("span", "chip kept", `Recording until ${fmtTime(g.until)} (emergency)`);
          u.title = "The emergency window: everything on this talkgroup is kept until then, even with Record off "
            + "(the alarm's All clear ends it)";
          fl.push(u);
        }
        flags.replaceChildren(...fl);
        li.className = (isOpen ? "og open" : "og") + (g.emergency ? ` emergency em-${g.emergency}` : "");
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

  // On screen: open, the page visible, the view shown and the address still #radio. Anything else stops
  // the asking (and an open view whose address moved on is closed: however it was left).
  function shown() {
    if (!st.open) return false;
    if (!location.hash.startsWith("#radio") || $("#radio-view").hidden) { close(); return false; }
    return document.visibilityState !== "hidden";
  }
  const pushed = () => (typeof cad !== "undefined" && cad.state && cad.state.comms_rev) || "";

  async function poll() {
    clearTimeout(st.timer);
    st.timer = 0;
    if (!shown()) return;
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
      st.failAt = Date.now();
    } finally {
      st.busy = false;
      render();
      if (st.again) { st.rev = ""; poll(); } else if (shown()) st.timer = setTimeout(poll, pushed() ? SAFETY_MS : POLL_MS);
    }
  }
  // Each state push: ask only if the hub's comms_rev moved past what is on screen.
  function onPush() {
    const ws = JSON.stringify(windows().map((w) => [w.talkgroup, w.until]));
    if (ws !== st.ws) { st.ws = ws; if (st.open) render(); }      // an emergency window opened, moved or ended
    const rev = pushed();
    if (!rev || rev === st.rev || st.busy || (st.failAt && Date.now() - st.failAt < RETRY_MS)) return;
    if (shown()) poll();
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
    st.timer = 0;
  }

  // ---- wiring -------------------------------------------------------------------------------------

  for (const k of ["unit", "channel", "event", "type", "pcfd", "emergency", "since", "until"]) f(k).addEventListener("change", changed);
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
  let drawnRev = -1;
  cad.hooks.push(() => { if (st.open && drawnRev !== cad.txRev) { drawnRev = cad.txRev; render(); } });   // a row's own state
  cad.hooks.push(() => { if (st.open) renderPaging(); });          // each tone pair as it decodes (the state push)
  cad.hooks.push(onPush);
  window.addEventListener("sizeup:tx-changed", () => { st.rev = ""; poll(); });   // a link added or removed: ask now
  document.addEventListener("visibilitychange", () => {                            // hidden: stop; back: ask at once
    if (document.visibilityState === "hidden") { clearTimeout(st.timer); st.timer = 0; }
    else if (st.open) poll();
  });

  return { open, close, render, otherGroups, get polling() { return !!st.timer || st.busy; } };
})();
