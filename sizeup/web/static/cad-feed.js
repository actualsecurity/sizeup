// The pinned "60 Control · Fire/EMS 15" feed at the top of the CAD's units column (sizeup/cad/feed.py):
// the last transmissions on the county's incident talkgroup(s), newest first, so the desk always
// sees what 60 Control said, even when it was talking to the internal dispatcher.
//
//   CadFeed.mount(box, deps)   once; deps: {state, radioRow, time, rev (cad.txRev: rows' own state)}
//   CadFeed.render()           on every state push and selection change (cheap when nothing changed)
//
// A row "talking to us" (pc: a phrase from the data file, a PCFD / Rye Brook unit, or 60 Control's
// turn next to one) gets the accent border and a "PC" badge, and a toast when the talk alerts are on.
// Every page on South 2 is a row too (#80): one with our tones is an event (its chip is on the row, pc = the
// department); any other page says what it was ("not ours", "unknown tones" with Name these tones for an admin,
// "no tones") and is never an event: the row is quieter, nothing is lost.
// A page that isn't ours whose text names our units (#115's safety net, rows[i].names_ours): flagged on the row
// (with the phone pre-alert that rang before it) and a toast whatever the talk alerts say; still not an event.
// Each row: the incidents it is on as chips (a click: that incident) and its "⋯" menu (cad.js txMenu: Add to
// event ▸, Remove from ..., Re-transcribe, Copy text).
// Collapsed to a one-line ticker: remembered in this browser (default: collapsed on a phone).
// A page going out now on South 2 (60 Control's paging channel, state.paging) shows under the head while it
// goes out, with who is being paged (radio.js fillPaging), collapsed or not.
"use strict";

window.CadFeed = (() => {
  const KEY = "sizeup.cad.feed.collapsed";
  const ALERT_KEY = "sizeup.alerts";                     // radio.js's talk alert settings
  const st = { box: null, deps: null, sig: "", headSig: "", seen: null, collapsed: false };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function load(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }
  function save(key, v) {
    try { localStorage.setItem(key, v); } catch (_) { /* not remembered */ }
  }
  function alertsOn() {
    try { return !!(JSON.parse(load(ALERT_KEY) || "null") || {}).on; } catch (_) { return false; }
  }
  const hhmmss = (unix) => st.deps.time(unix);
  const who = (t) => t.rig || t.talker || t.radio_id || "radio";
  const said = (t) => t.text || (typeof sttSaid === "function" ? sttSaid(t) : "—");

  // ---- the panel ---------------------------------------------------------------------------------

  function mount(box, deps) {
    st.box = box;
    st.deps = deps;
    const v = load(KEY);
    st.collapsed = v === null ? window.matchMedia("(max-width: 640px)").matches : v === "1";
    box.className = "feed";
    box.setAttribute("aria-label", "60 Control feed");
    const head = el("div", "feed-head");
    const tog = el("button", "feed-tog sm icon quiet");
    tog.type = "button";
    tog.addEventListener("click", () => { st.collapsed = !st.collapsed; save(KEY, st.collapsed ? "1" : "0"); st.sig = ""; render(); });
    const title = el("b", "feed-title");
    const talk = el("span", "feed-talk");
    talk.hidden = true;
    const more = el("a", "nav sm feed-more", "More");
    more.title = "All on this talkgroup";
    head.append(tog, title, talk, el("span", "spacer"), more);
    const paging = el("div", "paging-live");
    paging.hidden = true;
    paging.setAttribute("aria-live", "polite");
    const ticker = el("div", "feed-ticker");
    const list = el("ol", "list feed-list");
    list.setAttribute("aria-live", "polite");
    box.replaceChildren(head, paging, ticker, list);
    Object.assign(st, { tog, title, talk, more, paging, ticker, list, head });
    render();
  }

  function render() {
    if (!st.box) return;
    const s = st.deps.state();
    const f = s && s.feed;
    st.box.hidden = !f;
    if (!f) return;
    renderHead(f, (s && s.talking) || []);
    if (window.Radio) Radio.fillPaging(st.paging, s.paging, null);
    const rows = f.rows || [];
    const sig = JSON.stringify([st.collapsed, st.deps.rev(),
      rows.map((t) => [t.id, t.text, t.status, t.rig, t.pc, t.has_audio, t.alt_text, t.rerun_at, t.page, t.tones,
                       t.names_ours, t.tone_seq, t.prealert && t.prealert.status,
                       (t.events || []).map((e) => [e.id, e.why]), emSig(t)])]);
    if (sig !== st.sig) {
      st.sig = sig;
      st.box.classList.toggle("collapsed", st.collapsed);
      st.tog.textContent = st.collapsed ? "▸" : "▾";
      st.tog.title = st.collapsed ? "Expand" : "Collapse";
      st.tog.setAttribute("aria-expanded", String(!st.collapsed));
      st.list.hidden = st.collapsed;
      st.ticker.hidden = !st.collapsed;
      if (st.collapsed) renderTicker(rows[0]);
      else renderRows(rows, f);
    }
    alertNew(rows);
  }

  function renderHead(f, talking) {
    const tgs = f.talkgroups || [];
    const live = talking.filter((e) => e.system === "p25" && tgs.includes(e.tg));
    const now = live.find((e) => !e.ended) || live[0] || null;
    const text = now ? `${now.ended ? "Ended" : "TX"} ${now.rig || now.alias || (now.radio_id ? `radio ${now.radio_id}` : "unknown")}`
      + (tgs.length > 1 ? ` ${now.channel}` : "") : "";
    const sig = JSON.stringify([f.title, f.channel, text, now && now.ended, now && now.dispatcher]);
    if (sig === st.headSig) return;
    st.headSig = sig;
    st.title.textContent = f.title;
    st.more.href = f.channel ? `#radio?channel=${encodeURIComponent(f.channel)}` : "#radio";
    st.talk.hidden = !now;
    st.talk.textContent = text;
    st.talk.className = "feed-talk talk-badge" + (now && now.ended ? " ended" : "") + (now && now.dispatcher ? " disp" : "");
    if (now) st.talk.title = [now.radio_id ? `radio ${now.radio_id}` : "", now.alias || "", `TG ${now.tg}`].filter(Boolean).join(", ");
    st.box.classList.toggle("talking", !!now && !now.ended);
  }

  function pcBadge(t) {
    const b = el("span", "pc-badge", "PC");
    b.title = `To us: ${t.pc}`;
    return b;
  }

  function renderTicker(t) {
    if (!t) { st.ticker.replaceChildren(el("span", "dim", "None")); return; }
    const line = el("span", "feed-line");
    line.append(el("span", "t", hhmmss(t.start)), el("b", t.dispatcher ? "disp" : "unit", t.page ? "Page" : who(t)),
                el("span", t.text ? "" : "dim", said(t)));
    st.ticker.replaceChildren(...(t.pc ? [pcBadge(t)] : []), line);
    st.ticker.title = `${hhmmss(t.start)} ${who(t)}: ${said(t)}`;
    st.ticker.classList.toggle("pc", !!t.pc);
    st.ticker.classList.toggle("from-dispatch", !!t.dispatcher);
  }

  function renderRows(rows, f) {
    if (!rows.length) {
      st.list.replaceChildren(el("li", "empty", "None"));
      return;
    }
    const many = (f.talkgroups || []).length > 1;
    st.list.replaceChildren(...rows.map((t) => {
      const li = st.deps.radioRow(t, { scope: "feed" });
      li.classList.add("feed-row");
      li.dataset.id = t.id;
      const meta = li.querySelector(".meta");
      if (!many) meta.querySelectorAll(".chip.src").forEach((n) => n.remove());   // one talkgroup: the title says it
      if (t.pc) { li.classList.add("pc"); meta.prepend(pcBadge(t)); }
      if (t.page && t.page !== "ours") li.classList.add("skipped");       // a page that isn't an event
      return li;
    }));
  }

  // ---- "talking to us" alert ---------------------------------------------------------------------

  function alertNew(rows) {
    const first = st.seen === null;
    if (first) st.seen = new Set();
    for (const t of rows) {
      const flag = (t.names_ours || []).length && t.page && t.page !== "ours";
      const key = flag ? `${t.id}!` : t.id;          // a page flagged once its text is in: its own alert
      if (st.seen.has(key)) continue;
      st.seen.add(key);
      if (flag && (!first || Date.now() / 1000 - t.start < 600) && typeof oursNamedText === "function") {
        UI.toast(oursNamedText(t) + ": not an event", "err", `${hhmmss(t.start)} ${t.channel}`
          + (t.prealert ? `, phone rang ${hhmmss(t.prealert.at)}` : ""), 30000);
        continue;
      }
      if (first || !t.pc || !alertsOn() || !window.Radio) continue;
      const head = t.dispatcher ? "60 Control → Port Chester" : `${who(t)} → 60 Control`;
      Radio.toast(`${head}: ${said(t)}`, `${hhmmss(t.start)} ${t.channel}, ${t.pc}`);
    }
    if (st.seen.size > 500) st.seen = new Set(rows.map((t) => t.id));
  }

  return { mount, render };
})();
