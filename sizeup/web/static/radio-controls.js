// Settings > Radios, the station's receivers (moved here from the old monitor page, user 2026-10-08:
// the console is where things are done, the monitor only shows): each receiver's state, the SDR
// switch (one dongle hears one thing at a time), the incident the station opened from the last page
// (closed by hand here), which talkgroups are recorded and which are Main (up front in Radio, attached
// to that incident), re-running speech-to-text on every recording, and the disk. All from the monitor state (/api/state, polled every 2 s while this section is open);
// every switch is a POST the server allows admins only (sizeup/cad/api.py WRITES): for anyone else the
// controls are locked here too (cad.js roLock). Closing the paged incident is a dispatcher's.
//
//   RadioControls.mount(box)   when Settings > Radios opens (settings.js)
"use strict";

window.RadioControls = (() => {
  const POLL_MS = 2000;
  const PENDING_MS = 8000;
  const GROUP_ORDER = ["Fire/EMS", "County Police", "DOT", "Hospital", "Operations", "Interop/Events", "OEM", ""];
  const GROUP_COLORS = { "Fire/EMS": "#ff7a59", "County Police": "#6aa8ff", "DOT": "#e8d84a", "Hospital": "#f27ad1",
                         "Operations": "#7ee07e", "Interop/Events": "#B8C0CA", "OEM": "#ffcf7a", "": "#9AA3AD" };
  const RADIO_STATE = { ok: "", off: "off", absent: "absent", down: "down", starting: "starting" };
  const st = { box: null, state: null, timer: 0, pending: new Map(), error: "", sdrBusy: false, sdrError: "", rerunBusy: false,
               rerunMsg: "", confirm: null, confirmTimer: 0, closing: null, closeTimer: 0, n: {} };
  const ro = () => typeof canDo === "function" && !canDo("admin");
  const groupName = (g) => g || "Unknown";
  const groupColor = (g) => GROUP_COLORS[g || ""] || GROUP_COLORS[""];

  // ---- the section ----------------------------------------------------------------------------------

  function card(title, help, ...body) {
    const c = el("div", "s-card");
    const h = el("div", "s-card-head");
    h.append(el("h3", "", title));
    if (help) h.append(Help.button(help));
    c.append(h, ...body);
    return c;
  }

  function mount(box) {
    if (st.box !== box) {
      st.box = box;
      const n = st.n;
      n.radios = el("div", "radios");
      n.storage = el("div", "dim small");
      n.sdr = el("div", "sdrs");
      n.tgSum = el("span", "dim small");
      n.filter = el("input", "tg-filter");
      Object.assign(n.filter, { type: "search", placeholder: "Filter", autocomplete: "off", spellcheck: false });
      n.filter.setAttribute("aria-label", "Filter talkgroups");
      n.filter.addEventListener("input", renderTalkgroups);
      n.tgErr = el("div", "tg-error");
      n.tgErr.hidden = true;
      n.groups = el("div", "tg-groups rc-groups");
      n.groups.addEventListener("click", onSwitch);
      n.rerun = el("button", "", "Re-run all");
      n.rerun.type = "button";
      n.rerun.title = "Re-transcribe every recording still on disk";
      n.rerun.addEventListener("click", rerunAll);
      n.rerunMsg = el("span", "dim small");
      const tools = el("div", "row");
      tools.append(n.filter, n.tgSum);
      n.inc = el("div", "row");
      n.close = el("button", "ro-ok", "Close");        // a dispatcher's (ro-ok: not locked with the admin switches)
      n.close.type = "button";
      n.close.addEventListener("click", closeIncident);
      const rerunRow = el("div", "row");
      rerunRow.append(n.rerun, n.rerunMsg);
      box.replaceChildren(
        card("Receivers", "Each radio, speech-to-text and the dispatch voice.\nSDR: one dongle hears one of these at a time; "
             + "pick what it listens to. Plug in another dongle to run two at once.", n.radios, n.sdr, n.storage),
        card("Paged incident", "The incident the station opened from the last page: Main traffic attaches to it until it "
             + "goes quiet. Close it when the call is over.", n.inc),
        card("Talkgroups", "Record: keeps its calls and transcribes them.\nMain: up front in Radio and on our incident; "
             + "the rest are folded under Other talkgroups.", tools, n.tgErr, n.groups),
        card("Speech-to-text", "Runs every recording still on disk through speech-to-text again (newest first, behind live traffic).",
             rerunRow));
    }
    poll();
  }

  async function poll() {
    clearTimeout(st.timer);
    if (!st.box || !st.box.isConnected || st.box.offsetParent === null) return;   // the section closed: stop
    try {
      const r = await fetch("/api/state");
      if (r.ok) {
        const s = await r.json();
        if (!s.error) { st.state = s; settle(); render(); }
      }
    } catch (_) { /* the next poll tries again */ }
    st.timer = setTimeout(poll, POLL_MS);
  }

  function render() {
    renderRadios();
    renderIncident();
    renderSdr();
    renderStorage();
    renderTalkgroups();
    renderRerun();
  }

  // ---- receivers, SDR, disk -------------------------------------------------------------------------

  function renderRadios() {
    const radios = (st.state && st.state.radios) || [];
    syncList(st.n.radios, radios, {
      key: (r) => r.label,
      sig: (r) => JSON.stringify([r.state, r.open, r.detail, r.mhz, r.quieting_db, r.restarts, r.word]),
      make: () => el("span", "pill radio"),
      fill: (node, r) => {
        const state = r.state || "unknown";
        node.className = `pill radio ${state}${r.open && state === "ok" ? " open" : ""}`;
        const word = r.open && state === "ok" ? "● RX" : r.word || (RADIO_STATE[state] ?? state);
        node.replaceChildren(el("b", null, r.label), ...(word ? [el("span", "st", word)] : []));
        const bits = [r.detail || r.label];
        if (r.mhz) bits.push(`${r.mhz} MHz`);
        if (r.quieting_db !== null && r.quieting_db !== undefined) bits.push(`quieting ${r.quieting_db} dB`);
        if (r.restarts) bits.push(`${r.restarts} restarts`);
        node.title = `${bits.join(" · ")}\nstate: ${state}${r.open ? " · squelch open" : ""}`;
      },
      empty: "No radios",
    });
  }

  function renderSdr() {
    const sdr = st.state && st.state.sdr, box = st.n.sdr;
    show(box, !!(sdr && sdr.shared));
    if (!sdr || !sdr.shared) return;
    const list = sdr.sdrs || [{ serial: "", mode: sdr.mode, present: true }];
    const modes = sdr.modes || [];
    const key = list.map((x) => x.serial).join("|") + "#" + modes.join("|");
    if (box.dataset.key !== key) {
      box.replaceChildren();
      for (const x of list) {
        const row = el("span", "seg");
        row.dataset.sdr = x.serial;
        row.append(el("span", "dim sdr-label", ""));
        for (const m of modes) {
          const b = el("button", "", m);
          b.type = "button";
          b.dataset.mode = m;
          b.addEventListener("click", () => setSdrMode(m, x.serial));
          row.append(b);
        }
        row.append(el("span", "sdr-err"));
        box.append(row);
      }
      box.dataset.key = key;
    }
    for (const x of list) {
      const row = [...box.children].find((r) => r.dataset.sdr === x.serial);
      if (!row) continue;
      row.classList.toggle("absent", !x.present);
      row.classList.toggle("held", !!x.held_by);
      setText(row.querySelector(".sdr-label"), (list.length > 1 ? `SDR ${x.name || x.serial}` : "SDR") + (x.present ? "" : " unplugged")
              + (x.held_by ? `: ${x.held_by.app} ${x.held_by.mode}` : ""));
      setText(row.querySelector(".sdr-err"), st.sdrError);
      const nm = (x.name && x.name !== x.serial ? `${x.name} (serial ${x.serial})` : x.serial) + (x.product ? `, ${x.product}` : "");
      row.title = x.present ? `SDR ${nm}` : `SDR ${nm}: not plugged in`;
      for (const b of row.querySelectorAll("button")) {
        const m = b.dataset.mode;
        const there = m !== "Off" && list.find((o) => o.serial !== x.serial && o.mode === m);
        b.classList.toggle("active", m === x.mode);
        b.classList.toggle("elsewhere", !!there);
        b.title = there ? `${m} is on SDR ${there.name || there.serial}: click to move it here` : "";
        b.disabled = !!sdr.switching || st.sdrBusy || ro();
      }
    }
  }

  async function setSdrMode(mode, serial) {
    if (st.sdrBusy || ro()) return;
    st.sdrBusy = true;
    st.sdrError = "";
    renderSdr();
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 15000);      // never leave the switches disabled
    try {
      const r = await fetch("/api/mode", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify(serial ? { mode, sdr: serial } : { mode }), signal: ctl.signal });
      if (!r.ok) st.sdrError = ((await r.json().catch(() => ({}))).error) || `switch refused (${r.status})`;
    } catch (e) {
      st.sdrError = e.name === "AbortError" ? "no answer from the server, try again" : "couldn't reach the server";
    } finally {
      clearTimeout(timer);
      st.sdrBusy = false;
      renderSdr();
    }
  }

  function renderStorage() {
    const s = st.state && st.state.storage;
    if (!s) return setText(st.n.storage, "");
    const mb = s.bytes / 1e6;
    const size = mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
    const age = s.oldest_audio ? (Date.now() / 1000 - s.oldest_audio) / 3600 : null;
    const oldest = age == null ? "no audio" : `oldest ${age < 1 ? Math.round(age * 60) + " min" : age.toFixed(1) + " h"}`;
    setText(st.n.storage, `Disk: ${size}, ${oldest}, audio kept ${s.audio_hours} h, text ${s.history_hours} h`);
    st.n.storage.title = `${s.audio_files} audio files` + (s.deleted ? `, ${s.deleted} removed at the last cleanup` : "");
  }

  // ---- the paged incident: two clicks to close (a stray tap must not close a working incident) -----------

  function renderIncident() {
    const inc = ((st.state && st.state.incidents) || []).find((i) => i.status === "active");
    const n = st.n;
    if (!inc) {
      if (n.inc.dataset.id !== "") { n.inc.dataset.id = ""; n.inc.replaceChildren(el("span", "dim", "None")); }
      st.closing = null;
      return;
    }
    const where = (inc.geo && inc.geo.label) || inc.location || "No location";
    const sig = JSON.stringify([inc.id, inc.incident_type, where, inc.opened, st.closing === inc.id]);
    if (n.inc.dataset.id !== sig) {
      n.inc.dataset.id = sig;
      setText(n.close, st.closing === inc.id ? "Confirm close" : "Close");
      n.close.classList.toggle("danger", st.closing === inc.id);
      n.inc.replaceChildren(el("b", "", inc.incident_type || "Unknown"), el("span", "", where),
                            el("span", "dim", `since ${fmtTime(inc.opened)}`), el("span", "spacer"), n.close);
    }
    n.close.dataset.id = inc.id;
    n.close.disabled = typeof canDo === "function" && !canDo("dispatch");
  }
  async function closeIncident() {
    const id = st.n.close.dataset.id;
    if (!id || st.n.close.disabled) return;
    if (st.closing !== id) {
      st.closing = id;
      clearTimeout(st.closeTimer);
      st.closeTimer = setTimeout(() => { st.closing = null; renderIncident(); }, 5000);
      return renderIncident();
    }
    clearTimeout(st.closeTimer);
    st.closing = null;
    st.n.close.disabled = true;
    try {
      const r = await fetch(`/api/incident/${encodeURIComponent(id)}/close`, { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (err) {
      alert(`Close failed: ${err.message}`);
    }
    poll();
  }

  // ---- re-run speech-to-text ----------------------------------------------------------------------------

  function renderRerun() {
    const r = st.state && st.state.rerun;
    st.n.rerun.disabled = (r && r.pending > 0) || st.rerunBusy || ro();
    setText(st.n.rerunMsg, st.rerunMsg || (!r ? "" : r.pending > 0 ? `Re-run ${r.done}/${r.total}` + (r.changed ? `, ${r.changed} changed` : "")
      : r.total ? `Last: ${r.done} done, ${r.changed} changed` + (r.failed ? `, ${r.failed} failed` : "") : ""));
  }

  async function rerunAll() {
    if (ro()) return;
    const hours = (st.state && st.state.storage && st.state.storage.audio_hours) || 12;
    if (!confirm(`Re-transcribe every recording on disk (last ${hours} h)? Live traffic goes first.`)) return;
    st.rerunBusy = true;
    st.rerunMsg = "";
    renderRerun();
    let n = null;
    try {
      const r = await fetch("/api/retranscribe", { method: "POST", headers: { "Content-Type": "application/json" },
                                                   body: JSON.stringify({ all: true }) });
      n = r.ok ? (await r.json()).queued : null;
    } catch (_) { /* n stays null */ }
    st.rerunBusy = false;
    st.rerunMsg = n === null ? "Re-run failed" : n === 0 ? "Nothing to re-run" : "";
    renderRerun();
  }

  // ---- talkgroups: Record / Main, one at a time or a whole section --------------------------------------
  // state.talkgroups is the truth. A switch shows at once (pending) until a push agrees, or 8 s.

  function talkgroups() {
    const rows = (st.state && st.state.talkgroups) || [];
    return st.pending.size ? rows.map((r) => (st.pending.has(r.tg) ? { ...r, ...st.pending.get(r.tg).vals, pending: true } : r)) : rows;
  }
  function settle() {
    const now = Date.now(), by = new Map(((st.state && st.state.talkgroups) || []).map((r) => [r.tg, r]));
    for (const [tg, p] of st.pending) {
      const r = by.get(tg);
      if (!r || now > p.until || Object.entries(p.vals).every(([k, v]) => r[k] === v)) st.pending.delete(tg);
    }
  }
  function mark(tg, field, value) {
    const p = st.pending.get(tg) || { vals: {} };
    p.vals[field] = value;
    p.until = Date.now() + PENDING_MS;
    st.pending.set(tg, p);
  }
  function unmark(tg, field) {
    const q = st.pending.get(tg);
    if (q) { delete q.vals[field]; if (!Object.keys(q.vals).length) st.pending.delete(tg); }
  }
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let j = null;
    try { j = await r.json(); } catch (_) { /* not JSON */ }
    if (!r.ok || !j || j.ok === false) throw new Error((j && j.error) || `HTTP ${r.status}`);
  }
  async function setTalkgroup(r, field, value) {
    mark(r.tg, field, value);
    st.error = "";
    renderTalkgroups();
    try {
      await post("/api/talkgroup", { tg: r.tg, [field]: value });
    } catch (err) {
      unmark(r.tg, field);
      st.error = `Couldn't change ${field === "main" ? "Main" : "Record"} for ${r.name}: ${err.message}`;
    }
    renderTalkgroups();
  }
  async function setGroup(group, record) {
    const rows = ((st.state && st.state.talkgroups) || []).filter((r) => (r.group || "") === group && !(record && r.encrypted));
    for (const r of rows) mark(r.tg, "record", record);
    st.error = "";
    renderTalkgroups();
    try {
      await post("/api/talkgroup-group", { group: group || "Unknown", record });
    } catch (err) {
      for (const r of rows) unmark(r.tg, "record");
      st.error = `Couldn't change Record for ${groupName(group)}: ${err.message}`;
    }
    renderTalkgroups();
  }
  function onSwitch(e) {
    const b = e.target.closest("button.sw");
    if (!b || b.disabled || ro()) return;
    if (b.classList.contains("gsw")) return setGroup(b.dataset.group, !b.classList.contains("on"));
    const tg = Number(b.closest(".tg-row").dataset.key);
    const r = talkgroups().find((x) => x.tg === tg);
    if (!r) return;
    const field = b.dataset.field, next = !r[field];
    if (field === "record" && next && r.encrypted && st.confirm !== tg) {   // two taps: an encrypted talkgroup records noise
      clearTimeout(st.confirmTimer);
      st.confirm = tg;
      st.confirmTimer = setTimeout(() => { st.confirm = null; renderTalkgroups(); }, 6000);
      return renderTalkgroups();
    }
    clearTimeout(st.confirmTimer);
    st.confirm = null;
    setTalkgroup(r, field, next);
  }

  function heardText(r) {
    if (!r.count && !r.last) return "Not heard";
    const n = `${r.count} call${r.count === 1 ? "" : "s"}`;
    return r.last ? `${n}, last ${fmtTime(r.last)}` : n;
  }
  function setSwitch(b, on, label) {
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
    setText(b.lastChild, label);
  }
  function switchButton(field) {
    const b = el("button", "sw");
    b.type = "button";
    if (field) b.dataset.field = field;
    b.setAttribute("role", "switch");
    b.append(el("i", "knob"), el("span", "lbl"));
    return b;
  }

  function renderTalkgroups() {
    if (!st.box) return;
    const n = st.n, rows = talkgroups(), lock = ro();
    const words = n.filter.value.toLowerCase().split(/\s+/).filter(Boolean);
    const shown = rows.filter((r) => !words.length || words.every((w) =>
      `${r.name} ${r.tg} ${groupName(r.group)} ${r.main ? "main" : ""} ${r.encrypted ? "encrypted" : ""}`.toLowerCase().includes(w)));
    const count = (p) => rows.filter(p).length;
    setText(n.tgSum, rows.length ? `${rows.length} TGs, ${count((r) => r.main)} main, ${count((r) => r.record)} recording, `
      + `${count((r) => r.count)} heard, ${count((r) => r.encrypted)} encrypted` + (shown.length !== rows.length ? `, ${shown.length} shown` : "") : "");
    setText(n.tgErr, st.error);
    show(n.tgErr, !!st.error);
    const by = new Map();
    for (const r of shown) {
      if (!by.has(r.group || "")) by.set(r.group || "", []);
      by.get(r.group || "").push(r);
    }
    const rank = (g) => { const i = GROUP_ORDER.indexOf(g); return i < 0 ? GROUP_ORDER.length - 1 : i; };
    const groups = [...by.entries()].sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
      .map(([g, list]) => ({ g, list: list.slice().sort((a, b) => (b.main - a.main) || ((b.last || 0) - (a.last || 0))
                                                         || a.name.localeCompare(b.name, "en", { numeric: true })) }));
    syncList(n.groups, groups, {
      key: (x) => `g:${x.g}`,
      sig: (x) => JSON.stringify([x.list.map((r) => [r.tg, r.name, r.record, r.main, r.encrypted, r.count, r.last, r.dropped, !!r.pending,
                                                   fmtTime(r.last)]), st.confirm, lock]),
      make: () => {
        const sec = el("section", "tg-group");
        const h = el("h3", "tg-ghead");
        h.append(el("span", "chip tg-gchip"), el("span", "dim tg-gcount"), el("span", "tg-gspacer"), switchButton(""));
        h.lastChild.classList.add("gsw");
        sec.append(h, el("ol", "tg-rows"));
        return sec;
      },
      fill: (sec, x) => {
        const [h, ol] = sec.children;
        const [gchip, gcount, , gsw] = h.children;
        const dec = x.list.filter((r) => !r.encrypted), on = dec.filter((r) => r.record).length;
        const all = dec.length > 0 && on === dec.length, mixed = on > 0 && !all;
        gsw.dataset.group = x.g;
        setSwitch(gsw, all, mixed ? "Record: some" : all ? "Record: all" : "Record: none");
        gsw.classList.toggle("mixed", mixed);
        gsw.disabled = !dec.length || lock;
        gsw.title = !dec.length ? "All encrypted" : all ? `Recording all ${dec.length}: tap to stop` : `Record all ${dec.length} (encrypted stay off)`;
        setText(gchip, groupName(x.g));
        gchip.style.setProperty("--c", groupColor(x.g));
        const m = x.list.filter((r) => r.main).length, hd = x.list.filter((r) => r.count).length;
        setText(gcount, `${x.list.length} TG${x.list.length === 1 ? "" : "s"}` + (m ? `, ${m} main` : "") + (hd ? `, ${hd} heard` : ""));
        syncList(ol, x.list, {
          key: (r) => String(r.tg),
          sig: (r) => JSON.stringify([r.name, r.record, r.main, r.encrypted, r.count, r.last, r.dropped, !!r.pending, fmtTime(r.last),
                                      st.confirm === r.tg, lock]),
          make: () => {
            const li = el("li", "tg-row");
            const id = el("div", "tg-id");
            id.append(el("span", "tg-mark"), el("b", "tg-name"), el("span", "tg-num"), el("span", "tg-badge", "MAIN"));
            const info = el("div", "tg-info");
            info.append(el("span", "tg-heard"), el("span", "tg-note"));
            const sw = el("div", "tg-sw");
            sw.append(switchButton("record"), switchButton("main"));
            li.append(id, info, sw);
            return li;
          },
          fill: (li, r) => {
            const [id, info, sw] = li.children;
            const [mk, name, num, badge] = id.children;
            const [heard, note] = info.children;
            const confirming = st.confirm === r.tg;
            li.className = "tg-row" + (r.main ? " main" : "") + (r.encrypted ? " enc" : "") + (r.record ? "" : " norec")
              + (r.pending ? " pending" : "") + (confirming ? " confirm" : "");
            setText(mk, r.encrypted ? "🔒" : "");
            show(mk, !!r.encrypted);
            setText(name, r.name);
            setText(num, `TG ${r.tg}`);
            show(badge, !!r.main);
            setText(heard, heardText(r));
            heard.className = r.count ? "tg-heard" : "tg-heard dim";
            let t = confirming ? "Encrypted: noise only. Tap Record again to record anyway." : r.encrypted ? "Encrypted" : "";
            if (r.dropped && !confirming) t += `${t ? ", " : ""}${r.dropped} dropped`;
            setText(note, t);
            note.className = "tg-note" + (confirming ? " warn" : r.encrypted ? " enc" : "");
            show(note, !!t);
            const [recB, mainB] = sw.children;
            setSwitch(recB, !!r.record, confirming ? "Record?" : "Record");
            recB.title = r.record ? "Recording: calls kept and transcribed" : r.encrypted ? "Encrypted: can't be decoded" : "Not recording: calls dropped";
            setSwitch(mainB, !!r.main, "Main");
            mainB.title = r.main ? "Main: up front in Radio, on our incident" : "Other: folded in Radio";
            recB.disabled = mainB.disabled = lock;
          },
          empty: "",
        });
      },
      empty: rows.length ? "None" : "No talkgroup list",
    });
  }

  return { mount };
})();
