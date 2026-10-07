// The pinned "60 Control · Fire/EMS 15" feed at the top of the CAD's units column (sizeup/cad/feed.py):
// the last transmissions on the county's incident talkgroup(s), newest first, so the desk always
// sees what 60 Control said, even when it was talking to the internal dispatcher.
//
//   CadFeed.mount(box, deps)   once; deps: {state, selected, operator, api, radioRow, time}
//   CadFeed.render()           on every state push and selection change (cheap when nothing changed)
//
// A row "talking to us" (pc: a phrase from the data file, a PCFD / Rye Brook unit, or 60 Control's
// turn next to one) gets the accent border and a "PC" badge, and a toast when the talk alerts are on.
// "+ event" puts the transmission on the open event (a manual link, with the operator).
// Collapsed to a one-line ticker: remembered in this browser (default: collapsed on a phone).
"use strict";

window.CadFeed = (() => {
  const KEY = "sizeup.cad.feed.collapsed";
  const ALERT_KEY = "sizeup.alerts";                     // radio.js's talk alert settings
  const st = { box: null, deps: null, sig: "", headSig: "", seen: null, collapsed: false, busy: new Set() };

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
  const said = (t) => t.text || (t.status === "no_speech" ? "key-up · no speech" : t.status === "pending" ? "transcribing…" : "(no words)");
  const openEvent = () => {
    const inc = st.deps.selected();
    return inc && !["closed", "dismissed"].includes(inc.status) ? inc : null;
  };

  // ---- the panel ---------------------------------------------------------------------------------

  function mount(box, deps) {
    st.box = box;
    st.deps = deps;
    const v = load(KEY);
    st.collapsed = v === null ? window.matchMedia("(max-width: 640px)").matches : v === "1";
    box.className = "feed";
    box.setAttribute("aria-label", "60 Control feed");
    const head = el("div", "feed-head");
    const tog = el("button", "feed-tog");
    tog.type = "button";
    tog.addEventListener("click", () => { st.collapsed = !st.collapsed; save(KEY, st.collapsed ? "1" : "0"); st.sig = ""; render(); });
    const title = el("b", "feed-title");
    const talk = el("span", "feed-talk");
    talk.hidden = true;
    const more = el("a", "feed-more", "More →");
    more.title = "Every transmission on this talkgroup (/comms)";
    head.append(tog, title, talk, el("span", "spacer"), more);
    const ticker = el("div", "feed-ticker");
    const list = el("ol", "list feed-list");
    list.setAttribute("aria-live", "polite");
    box.replaceChildren(head, ticker, list);
    Object.assign(st, { tog, title, talk, more, ticker, list, head });
    render();
  }

  function render() {
    if (!st.box) return;
    const s = st.deps.state();
    const f = s && s.feed;
    st.box.hidden = !f;
    if (!f) return;
    renderHead(f, (s && s.talking) || []);
    const inc = openEvent();
    const rows = f.rows || [];
    const sig = JSON.stringify([st.collapsed, inc && inc.id, inc && inc.number,
      rows.map((t) => [t.id, t.text, t.status, t.rig, t.pc, t.has_audio, (t.events || []).map((e) => e.id)]),
      [...st.busy]]);
    if (sig !== st.sig) {
      st.sig = sig;
      st.box.classList.toggle("collapsed", st.collapsed);
      st.tog.textContent = st.collapsed ? "▸" : "▾";
      st.tog.title = st.collapsed ? "Show the last transmissions" : "Collapse to the latest line";
      st.tog.setAttribute("aria-expanded", String(!st.collapsed));
      st.list.hidden = st.collapsed;
      st.ticker.hidden = !st.collapsed;
      if (st.collapsed) renderTicker(rows[0]);
      else renderRows(rows, inc, f);
    }
    alertNew(rows);
  }

  function renderHead(f, talking) {
    const tgs = f.talkgroups || [];
    const live = talking.filter((e) => e.system === "p25" && tgs.includes(e.tg));
    const now = live.find((e) => !e.ended) || live[0] || null;
    const text = now ? `${now.ended ? "just transmitted" : "talking now"} · ${now.rig || now.alias || (now.radio_id ? `radio ${now.radio_id}` : "talker not known yet")}`
      + (tgs.length > 1 ? ` · ${now.channel}` : "") : "";
    const sig = JSON.stringify([f.title, f.channel, text, now && now.ended, now && now.dispatcher]);
    if (sig === st.headSig) return;
    st.headSig = sig;
    st.title.textContent = f.title;
    st.more.href = f.channel ? `/comms?channel=${encodeURIComponent(f.channel)}` : "/comms";
    st.talk.hidden = !now;
    st.talk.textContent = text;
    st.talk.className = "feed-talk talk-badge" + (now && now.ended ? " ended" : "") + (now && now.dispatcher ? " disp" : "");
    if (now) st.talk.title = [now.radio_id ? `radio ${now.radio_id}` : "", now.alias || "", `TG ${now.tg}`].filter(Boolean).join(" · ");
    st.box.classList.toggle("talking", !!now && !now.ended);
  }

  function pcBadge(t) {
    const b = el("span", "pc-badge", "PC");
    b.title = `Talking to us: ${t.pc}`;
    return b;
  }

  function renderTicker(t) {
    if (!t) { st.ticker.replaceChildren(el("span", "dim", "Nothing heard on this talkgroup yet")); return; }
    const line = el("span", "feed-line");
    line.append(el("span", "t", hhmmss(t.start)), el("b", t.dispatcher ? "disp" : "unit", who(t)),
                el("span", t.text ? "" : "dim", said(t)));
    st.ticker.replaceChildren(...(t.pc ? [pcBadge(t)] : []), line);
    st.ticker.title = `${hhmmss(t.start)} ${who(t)}: ${said(t)}`;
    st.ticker.classList.toggle("pc", !!t.pc);
    st.ticker.classList.toggle("from-dispatch", !!t.dispatcher);
  }

  function renderRows(rows, inc, f) {
    if (!rows.length) {
      st.list.replaceChildren(el("li", "empty", "Nothing heard on this talkgroup yet"));
      return;
    }
    const many = (f.talkgroups || []).length > 1;
    st.list.replaceChildren(...rows.map((t) => {
      const li = st.deps.radioRow(t);
      li.classList.add("feed-row");
      li.dataset.id = t.id;
      const meta = li.querySelector(".meta");
      if (!many) meta.querySelectorAll(".chip.src").forEach((n) => n.remove());   // one talkgroup: the title says it
      if (t.pc) { li.classList.add("pc"); meta.prepend(pcBadge(t)); }
      const acts = el("span", "feed-acts");
      for (const e of t.events || []) {
        const c = el("span", "feed-ev" + (e.why === "manual" ? " manual" : ""), e.number || `#${e.id}`);
        c.title = `On event ${e.number || e.id}${e.address ? ` · ${e.address}` : ""} (${e.why === "manual" ? `added by ${e.unit || "hand"}` : e.why})`;
        acts.append(c);
      }
      acts.append(addButton(t, inc));
      meta.append(acts);
      return li;
    }));
  }

  function addButton(t, inc) {
    const b = el("button", "mini feed-add", "+ event");
    b.type = "button";
    const on = inc && (t.events || []).some((e) => e.id === inc.id);
    if (!inc) {
      b.disabled = true;
      b.title = "Open an event first: this puts the transmission in its radio traffic";
    } else if (on) {
      b.disabled = true;
      b.textContent = "✓ event";
      b.title = `Already in ${inc.number || "this event"}'s radio traffic`;
    } else if (st.busy.has(t.id)) {
      b.disabled = true;
      b.textContent = "…";
    } else {
      b.title = `Put this transmission in ${inc.number || "the open event"}'s radio traffic`;
      b.addEventListener("click", () => link(t, inc, b));
    }
    return b;
  }

  async function link(t, inc, b) {
    st.busy.add(t.id);
    render();
    try {
      await st.deps.api("POST", `/api/cad/incident/${inc.id}/link`, { traffic_id: t.id, operator: st.deps.operator() });
    } catch (err) {
      b.title = err.message;
      alert(`Could not add it to the event: ${err.message}`);
    } finally {
      st.busy.delete(t.id);
      st.sig = "";
      render();
    }
  }

  // ---- "talking to us" alert ---------------------------------------------------------------------

  function alertNew(rows) {
    const first = st.seen === null;
    if (first) st.seen = new Set();
    for (const t of rows) {
      if (st.seen.has(t.id)) continue;
      st.seen.add(t.id);
      if (first || !t.pc || !alertsOn() || !window.Radio) continue;
      const head = t.dispatcher ? "60 Control → Port Chester" : `${who(t)} → 60 Control`;
      Radio.toast(`${head}: ${said(t)}`, `${hhmmss(t.start)} · ${t.channel} · ${t.pc}`);
    }
    if (st.seen.size > 500) st.seen = new Set(rows.map((t) => t.id));
  }

  return { mount, render };
})();
