// Settings > Radios, the station's receivers (moved here from the old monitor page, user 2026-10-08:
// the console is where things are done, the monitor only shows): each receiver's state, the SDR
// switch (one dongle hears one thing at a time), the incident the station opened from the last page
// (closed by hand here), the Channels table (FG1, South 2, then every talkgroup): Record, Main (up
// front in Radio, attached to that incident), Transcribe and the speech-to-text Engine (Local =
// whisper on the station, AWS = Amazon Transcribe from the hub, Both = compare; sizeup/channels.py),
// with whether the station applied them and what AWS cost this month; the AWS custom vocabulary;
// re-running speech-to-text on every recording, and the disk. All from the monitor state (/api/state,
// polled every 2 s while this section is open) and /api/cad/stt (the vocabulary);
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
  const st = { box: null, state: null, timer: 0, pending: new Map(), cpending: new Map(), error: "", sdrBusy: false, sdrError: "",
               rerunBusy: false, rerunMsg: "", confirm: null, confirmTimer: 0, closing: null, closeTimer: 0, n: {},
               stt: null, sttTimer: 0, vocabMsg: "", vocabBusy: false, vocabDirty: false };
  const ENGINES = [["local", "Local"], ["aws", "AWS"], ["both", "Both"]];
  const ENGINE_HELP = "Local: whisper.cpp large-v3 on the station (whisper-server, port 8179).\n"
    + "AWS: Amazon Transcribe streaming, called from the hub (its task role): $0.024 a minute, at least 15 s a stream; "
    + "the station skips whisper and sends the clip.\nBoth: whisper on the station and AWS on the hub; AWS's text is "
    + "shown, whisper's kept to compare (Radio view: \u21c4).\nWithout AWS (not set up, no credentials, the monthly "
    + "limit reached) AWS channels use Local until it's back.";
  const ro = () => typeof canDo === "function" && !canDo("admin");
  const groupName = (g) => g || "Unknown";
  const groupColor = (g) => GROUP_COLORS[g || ""] || GROUP_COLORS[""];

  // ---- the section ----------------------------------------------------------------------------------

  function card(title, help, ...body) {     // tab: Settings > Radios's tab it goes under (settings.js; the station shows all)
    const c = el("div", "card s-card");
    const h = el("div", "card-head");
    h.append(el("h2", "", title));
    if (help) h.append(Help.button(help));
    c.append(h, ...body);
    return c;
  }

  function tab(k, c) {
    c.dataset.rtab = k;
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
      n.filter.setAttribute("aria-label", "Filter channels and talkgroups");
      n.filter.addEventListener("input", renderTalkgroups);
      n.tgErr = el("div", "tg-error");
      n.tgErr.hidden = true;
      n.groups = el("div", "tg-groups rc-groups");
      n.groups.addEventListener("click", onSwitch);
      n.groups.addEventListener("change", onEngine);
      n.sync = el("div", "small rc-sync");
      n.cost = el("div", "dim small rc-cost");
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
        tab("receivers", card("Receivers", "Each radio, speech-to-text and the dispatch voice.\nSDR: one dongle hears one of these at a time; "
             + "pick what it listens to. Plug in another dongle to run two at once.", n.radios, n.sdr, n.storage)),
        tab("receivers", card("Paged incident", "The incident the station opened from the last page: Main traffic attaches to it until it "
             + "goes quiet. Close it when the call is over.", n.inc)),
        tab("channels", card("Channels", "Record: keeps its transmissions (off for FG1 / South 2: the receiver keeps running, squelch, "
             + "MDC IDs and Talking now stay live, nothing is kept).\nMain: up front in Radio and on our incident; "
             + "the rest are folded under Other talkgroups.\nTranscribe: speech-to-text at all.\nEngine:\n" + ENGINE_HELP,
             n.sync, n.cost, tools, n.tgErr, n.groups)),
        tab("vocabulary", card("Vocabulary", "Words Amazon Transcribe should expect: unit names, places, streets. AWS takes letters only, "
             + "so numbers go spelled out (Engine 60 \u2192 Engine-sixty) and come back as written. Saved here, sent to "
             + "AWS with Push (the hub's vocabulary sizeup-pcfd), used by new streams once it is Ready.", ...vocabCard())),
        tab("stt", card("Speech-to-text", "Runs every recording still on disk through speech-to-text again (newest first, behind live "
             + "traffic), with the local model.\n" + ENGINE_HELP, rerunRow)));
    }
    loadStt();
    poll();
  }

  async function poll() {
    clearTimeout(st.timer);
    if (!st.box || !st.box.isConnected || st.box.offsetParent === null) return;   // the section closed: stop
    if (document.visibilityState === "hidden") { st.timer = setTimeout(poll, POLL_MS); return; }   // nobody looking
    try {
      // only what changed since the last answer (#137: live.js liveMerge, the server's ?since=)
      const r = await fetch("/api/state" + (st.stateId ? `?since=${encodeURIComponent(st.stateId)}` : ""), { cache: "no-store" });
      if (r.ok) {
        const s = liveMerge(st.state, await r.json());
        st.stateId = s ? r.headers.get("X-State-Id") || "" : "";
        if (s && !s.error) { st.state = s; settle(); render(); }
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

  // m:ss for how long a receiver's squelch has been open
  const mmss = (s) => { const n = Math.max(0, Math.floor(s || 0)); return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`; };

  function renderRadios() {
    const radios = (st.state && st.state.radios) || [];
    syncList(st.n.radios, radios, {
      key: (r) => r.label,
      sig: (r) => JSON.stringify([r.state, r.open, r.detail, r.mhz, r.quieting_db, r.restarts, r.word, r.open_s, r.stuck,
                                  r.hiss_db, r.floor_db, r.open_db, r.close_db]),
      make: () => el("span", "pill radio"),
      fill: (node, r) => {
        const state = r.state || "unknown";
        const on = !!r.open && state === "ok";
        // Open without a break for over 2 minutes (the station's STUCK_S): longer than any page, so most likely
        // noise holding the squelch (GitHub #116). Amber, with how long; the station logged it once.
        const stuck = on && !!r.stuck;
        node.className = `pill radio ${state}${on ? " open" : ""}${stuck ? " stuck" : ""}`;
        const word = stuck ? `open ${mmss(r.open_s)} (stuck?)` : on ? "● RX" : r.word || (RADIO_STATE[state] ?? state);
        node.replaceChildren(el("b", null, r.label), ...(word ? [el("span", "st", word)] : []));
        const bits = [r.detail || r.label];
        if (r.mhz) bits.push(`${r.mhz} MHz`);
        if (r.restarts) bits.push(`${r.restarts} restarts`);
        const lines = [bits.join(" · ")];
        if (on && r.open_s !== null && r.open_s !== undefined) {
          lines.push(`open ${mmss(r.open_s)}` + (stuck ? " without a break: longer than any page, likely noise holding the squelch" : ""));
        }
        const has = (v) => v !== null && v !== undefined;
        if (has(r.quieting_db) || has(r.open_db)) {
          // quieting: how far a carrier pushes the hiss down from its idle level (the floor); RX opens at open_db
          const lv = [`quieting ${has(r.quieting_db) ? r.quieting_db : "?"} dB`];
          if (has(r.open_db)) lv.push(`opens at ${r.open_db} dB, closes under ${r.close_db} dB`);
          if (has(r.hiss_db)) lv.push(`idle hiss (floor) ${r.hiss_db} dB`);
          if (has(r.floor_db)) lv.push(`background carrier ${r.floor_db} dB`);
          lines.push(lv.join(" · "));
        }
        lines.push(`state: ${state}${r.open ? " · squelch open" : ""}`);
        node.title = lines.join("\n");
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
              + (x.held_by ? `: ${x.held_by.app} ${x.held_by.mode}` : "")
              + (x.set_by === "auto" && x.mode !== "Off" ? ` · ${x.mode} · auto` : ""));   // given by [radio] auto_assign
      setText(row.querySelector(".sdr-err"), st.sdrError);
      const nm = (x.name && x.name !== x.serial ? `${x.name} (serial ${x.serial})` : x.serial) + (x.product ? `, ${x.product}` : "");
      const who = x.set_by === "auto" ? `${x.mode} set automatically when it was plugged in`
                : x.set_by === "person" ? `${x.mode} picked by a person` : "";
      row.title = (x.present ? `SDR ${nm}` : `SDR ${nm}: not plugged in`) + (who ? `\n${who}` : "");
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
      UI.toast(`Close failed: ${err.message}`, "err");
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
    if (!(await UI.confirm(`Re-transcribe every recording on disk (last ${hours} h)? Live traffic goes first.`,
      { ok: "Re-run all", danger: false }))) return;
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
    const rows = ((st.state && st.state.talkgroups) || []).map((r) => ({ ...r, ...chan(`tg:${r.tg}`), record: r.record }));
    return st.pending.size ? rows.map((r) => (st.pending.has(r.tg) ? { ...r, ...st.pending.get(r.tg).vals, pending: true } : r)) : rows;
  }
  // Channel choices (sizeup/channels.py): the default (AWS for [stt] aws_channels), what is saved, what was just clicked.
  function chan(key) {
    const c = (st.state && st.state.channels) || {};
    const p = { record: true, transcribe: true, engine: (c.aws_channels || []).includes(key) ? "aws" : "local",
                ...((c.prefs || {})[key] || {}) };
    const q = st.cpending.get(key);
    if (q) Object.assign(p, q.vals, { cpending: true });
    p.effective = !p.transcribe ? "off" : p.engine === "local" || c.aws_ok ? p.engine : "local";
    return p;
  }
  function fmRows() {
    const c = (st.state && st.state.channels) || {};
    return (c.fm || []).map((f) => ({ key: `ch:${f.key}`, chan: f.key, name: f.key, kind: f.kind, mhz: f.mhz, fm: true, ...chan(f.key) }));
  }
  function settle() {
    const now = Date.now(), by = new Map(((st.state && st.state.talkgroups) || []).map((r) => [r.tg, r]));
    for (const [tg, p] of st.pending) {
      const r = by.get(tg);
      if (!r || now > p.until || Object.entries(p.vals).every(([k, v]) => r[k] === v)) st.pending.delete(tg);
    }
    const c = (st.state && st.state.channels) || {};
    for (const [key, p] of st.cpending) {
      const saved = { record: true, transcribe: true, engine: (c.aws_channels || []).includes(key) ? "aws" : "local",
                      ...((c.prefs || {})[key] || {}) };
      if (now > p.until || Object.entries(p.vals).every(([k, v]) => saved[k] === v)) st.cpending.delete(key);
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
  async function setChannel(key, field, value, label) {
    const q = st.cpending.get(key) || { vals: {} };
    q.vals[field] = value;
    q.until = Date.now() + PENDING_MS;
    st.cpending.set(key, q);
    st.error = "";
    renderTalkgroups();
    try {
      await api("POST", "/api/cad/settings/channels", { action: "set", key, [field]: value,
                                                        operator: typeof operator === "function" ? operator() : "" });
      poll();
    } catch (err) {
      const p = st.cpending.get(key);
      if (p) { delete p.vals[field]; if (!Object.keys(p.vals).length) st.cpending.delete(key); }
      st.error = `Couldn't change ${field === "engine" ? "Engine" : field === "transcribe" ? "Transcribe" : "Record"} for ${label}: ${err.message}`;
    }
    renderTalkgroups();
  }
  function onEngine(e) {
    const s = e.target.closest("select.tg-eng");
    if (!s || s.disabled || ro()) return;
    const li = s.closest(".tg-row"), key = li.dataset.ckey;
    setChannel(key, "engine", s.value, li.dataset.label || key);
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
    const li = b.closest(".tg-row");
    if (b.dataset.field === "transcribe" || li.dataset.fm === "1") {
      const key = li.dataset.ckey, cur = li.dataset.fm === "1" ? fmRows().find((x) => x.chan === key) : chan(key);
      if (!cur) return;
      return setChannel(key, b.dataset.field, !cur[b.dataset.field], li.dataset.label || key);
    }
    const tg = Number(li.dataset.key);
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

  function engineSelect() {
    const sel = el("select", "tg-eng sm");
    for (const [v, label] of ENGINES) sel.append(new Option(label, v));
    sel.setAttribute("aria-label", "Speech-to-text engine");
    return sel;
  }
  function channelNote(r) {
    const c = (st.state && st.state.channels) || {};
    if (!r.record) return "";
    if (!r.transcribe) return "not transcribed";
    if (r.engine !== "local" && r.effective === "local") return `AWS off: local${c.aws_why ? ` (${c.aws_why})` : ""}`;
    return "";
  }

  function renderSync() {
    const c = (st.state && st.state.channels) || {}, n = st.n;
    const sy = c.sync || {}, a = c.aws || {};
    setText(n.sync, sy.detail || "");
    n.sync.className = `small rc-sync ${sy.state || ""}`;
    show(n.sync, !!sy.detail);
    let cost = "";
    if (a.enabled) {
      cost = `AWS this month: ${Math.round(a.minutes || 0)} min ≈ $${(a.usd || 0).toFixed(2)} of $${a.limit_usd} limit`;
      if (!a.ok && a.why) cost += ` · ${a.why}`;
      n.cost.title = `${a.streams || 0} streams, ${Math.round((a.audio_s || 0) / 60)} min of audio sent in ${a.month}; `
        + `billed at least 15 s a stream at $${a.price_per_min}/min. Open now: ${a.streams_open || 0}.`
        + (a.last_error ? `\nLast AWS error: ${a.last_error}` : "");
    } else if (a.where === "hub") {
      cost = "AWS runs on the hub: its cost and limit are shown there";
      n.cost.title = c.aws_ok ? "The hub can take AWS audio now" : `The hub can't take AWS audio now: ${c.aws_why || "?"}`;
    } else {
      cost = "AWS isn't set up here: every channel uses the local model";
      n.cost.title = c.aws_why || "";
    }
    setText(n.cost, cost);
    n.cost.classList.toggle("warn", !!(a.enabled && !a.ok));
  }

  function renderTalkgroups() {
    if (!st.box) return;
    renderSync();
    const n = st.n, rows = talkgroups(), lock = ro();
    const words = n.filter.value.toLowerCase().split(/\s+/).filter(Boolean);
    const match = (r) => !words.length || words.every((w) =>
      `${r.name} ${r.tg ?? ""} ${r.fm ? "fm channels" : groupName(r.group)} ${r.main ? "main" : ""} ${r.encrypted ? "encrypted" : ""} `
      .concat(`${r.engine} ${r.transcribe ? "" : "off"}`).toLowerCase().includes(w));
    const shown = rows.filter(match);
    const fm = fmRows().filter(match);
    const count = (p) => rows.filter(p).length;
    const aws = rows.concat(fmRows()).filter((r) => r.record && r.transcribe && r.engine !== "local").length;
    setText(n.tgSum, (rows.length || fm.length) ? `${fmRows().length} FM, ${rows.length} TGs, ${count((r) => r.main)} main, `
      + `${count((r) => r.record)} recording, ${aws} on AWS, ${count((r) => r.count)} heard, ${count((r) => r.encrypted)} encrypted`
      + (shown.length !== rows.length ? `, ${shown.length} shown` : "") : "");
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
    if (fm.length) groups.unshift({ g: "\u0000fm", fm: true, list: fm });
    const rowSig = (r) => JSON.stringify([r.name, r.record, r.main, r.encrypted, r.count, r.last, r.dropped, !!r.pending, fmtTime(r.last),
                                         st.confirm === r.tg, lock, r.transcribe, r.engine, r.effective, !!r.cpending, channelNote(r),
                                         (((st.state && st.state.channels) || {}).sync || {}).state]);
    syncList(n.groups, groups, {
      key: (x) => `g:${x.g}`,
      sig: (x) => JSON.stringify([x.list.map(rowSig), st.confirm, lock, (((st.state && st.state.channels) || {}).sync || {}).state]),
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
        if (x.fm) {
          setText(gchip, "FM channels");
          gchip.style.setProperty("--c", "var(--accent)");
          setText(gcount, `${x.list.length} channel${x.list.length === 1 ? "" : "s"}, squelch + MDC IDs`);
          show(gsw, false);
        } else {
          show(gsw, true);
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
        }
        syncList(ol, x.list, {
          key: (r) => (r.fm ? r.key : String(r.tg)),
          sig: rowSig,
          make: () => {
            const li = el("li", "tg-row");
            const id = el("div", "tg-id");
            id.append(el("span", "tg-mark"), el("b", "tg-name"), el("span", "tg-num"), el("span", "tg-badge", "MAIN"));
            const info = el("div", "tg-info");
            info.append(el("span", "tg-heard"), el("span", "tg-note"));
            const sw = el("div", "tg-sw");
            sw.append(switchButton("record"), switchButton("main"), switchButton("transcribe"), engineSelect());
            li.append(id, info, sw);
            return li;
          },
          fill: (li, r) => {
            const [id, info, sw] = li.children;
            const [mk, name, num, badge] = id.children;
            const [heard, note] = info.children;
            const confirming = !r.fm && st.confirm === r.tg;
            li.dataset.fm = r.fm ? "1" : "";
            li.dataset.ckey = r.fm ? r.chan : `tg:${r.tg}`;
            li.dataset.label = r.name;
            li.className = "tg-row" + (r.main ? " main" : "") + (r.encrypted ? " enc" : "") + (r.record ? "" : " norec")
              + (r.pending || r.cpending ? " pending" : "") + (confirming ? " confirm" : "") + (r.effective === "aws" || r.effective === "both" ? " aws" : "");
            setText(mk, r.encrypted ? "🔒" : "");
            show(mk, !!r.encrypted);
            setText(name, r.name);
            setText(num, r.fm ? `${r.mhz} MHz${r.kind === "paging" ? " · paging" : ""}` : `TG ${r.tg}`);
            show(badge, !!r.main);
            setText(heard, r.fm ? (r.kind === "paging" ? "60 Control pages" : "Fireground") : heardText(r));
            heard.className = r.fm || r.count ? "tg-heard" : "tg-heard dim";
            let t = confirming ? "Encrypted: noise only. Tap Record again to record anyway." : r.encrypted ? "Encrypted" : "";
            if (r.dropped && !confirming) t += `${t ? ", " : ""}${r.dropped} dropped`;
            const cn = channelNote(r);
            if (cn && !confirming) t += `${t ? ", " : ""}${cn}`;
            setText(note, t);
            note.className = "tg-note" + (confirming ? " warn" : r.encrypted ? " enc" : "");
            show(note, !!t);
            const [recB, mainB, sttB, engS] = sw.children;
            setSwitch(recB, !!r.record, confirming ? "Record?" : "Record");
            recB.title = r.record ? "Recording: transmissions kept (and transcribed if Transcribe is on)"
              : r.fm ? "Not recording: the receiver runs (squelch, MDC IDs, Talking now), nothing is kept"
              : r.encrypted ? "Encrypted: can't be decoded" : "Not recording: calls dropped";
            setSwitch(mainB, !!r.main, "Main");
            mainB.title = r.main ? "Main: up front in Radio, on our incident" : "Other: folded in Radio";
            show(mainB, !r.fm);
            setSwitch(sttB, !!r.transcribe, "Transcribe");
            sttB.title = r.transcribe ? "Speech-to-text on" : "Kept without a transcript";
            if (engS.value !== r.engine) engS.value = r.engine;
            engS.title = `Speech-to-text engine: ${r.engine}${r.effective !== r.engine && r.transcribe ? ` (running: ${r.effective})` : ""}\n${ENGINE_HELP}`;
            const onHub = (((st.state && st.state.channels) || {}).sync || {}).state === "hub";   // set on the hub, not here
            recB.disabled = mainB.disabled = sttB.disabled = engS.disabled = lock;
            if (r.fm) recB.disabled = recB.disabled || onHub;
            sttB.disabled = sttB.disabled || !r.record || onHub;
            engS.disabled = engS.disabled || !r.record || !r.transcribe || onHub;
            if (onHub) sttB.title = engS.title = "Set on the hub (the CAD of record) while this station forwards to it";
          },
          empty: "",
        });
      },
      empty: rows.length ? "None" : "No channels",
    });
  }

  // ---- the AWS custom vocabulary (GET /api/cad/stt; POST /api/cad/settings/stt) ------------------------------

  function vocabCard() {
    const n = st.n;
    n.vocab = el("textarea", "vocab-text");
    Object.assign(n.vocab, { rows: 8, spellcheck: false, placeholder: "One phrase a line: Engine 60, Rye Brook, King Street" });
    n.vocab.setAttribute("aria-label", "Vocabulary phrases, one a line");
    n.vocab.addEventListener("input", () => { st.vocabDirty = true; renderVocab(); });
    const btn = (label, fn, title) => {
      const b = el("button", "", label);
      b.type = "button";
      if (title) b.title = title;
      b.addEventListener("click", fn);
      return b;
    };
    n.vocabSave = btn("Save", saveVocab, "Save the list (it goes to AWS with Push)");
    n.vocabSuggest = btn("Suggestions", () => { const v = st.stt && st.stt.vocabulary; if (!v) return;
      n.vocab.value = v.suggestions.join("\n"); st.vocabDirty = true; renderVocab(); },
    "Unit names from the boards, PCFD words and the busiest Port Chester / Rye Brook streets");
    n.vocabPush = btn("Push to AWS", pushVocab, "Send the saved list to Amazon Transcribe (vocabulary sizeup-pcfd)");
    n.vocabState = el("span", "small vocab-state");
    n.vocabMsg = el("div", "dim small");
    n.vocabSkip = el("details", "small vocab-skip");
    n.vocabSkip.append(el("summary", "", "As sent to AWS"), el("div", "dim vocab-aws"));
    n.limit = el("input", "vocab-limit");
    Object.assign(n.limit, { type: "number", min: 0, max: 10000, step: 1 });
    n.limit.setAttribute("aria-label", "Monthly AWS limit in dollars");
    n.limitSave = btn("Save limit", saveLimit, "When this month's AWS cost reaches it, AWS channels use the local model until next month");
    const row = el("div", "row");
    row.append(n.vocabSave, n.vocabSuggest, n.vocabPush, n.vocabState);
    const lim = el("div", "row");
    lim.append(el("span", "small", "Monthly AWS limit $"), n.limit, n.limitSave);
    return [n.vocab, row, n.vocabMsg, n.vocabSkip, lim];
  }

  async function loadStt() {
    clearTimeout(st.sttTimer);
    try {
      st.stt = await api("GET", "/api/cad/stt");
    } catch (e) {
      st.vocabMsg = `Couldn't load the vocabulary: ${e.message}`;
    }
    renderVocab(true);
    const s = st.stt && st.stt.vocabulary && st.stt.vocabulary.state;
    if (s && s.state === "PENDING" && st.box && st.box.isConnected) st.sttTimer = setTimeout(loadStt, 5000);
  }

  function renderVocab(fresh) {
    const n = st.n, d = st.stt;
    if (!d || !n.vocab) return;
    const v = d.vocabulary, lock = ro();
    if (fresh && !st.vocabDirty) n.vocab.value = v.phrases.join("\n");
    if (fresh && document.activeElement !== n.limit) n.limit.value = d.limit.value;
    const s = v.state || {};
    const when = s.updated ? ` (sent ${fmtTime(s.updated)})` : "";
    const word = { READY: "Ready", PENDING: "Being built by AWS…", FAILED: "Failed", error: "Error", none: "Not on AWS yet" }[s.state] || s.state;
    setText(n.vocabState, `${s.name || "sizeup-pcfd"}: ${word}${when}` + (s.state === "READY" && s.sig && s.sig !== v.sig ? " · the saved list changed: Push again" : "")
      + (s.reason ? ` · ${s.reason}` : ""));
    n.vocabState.className = `small vocab-state ${s.state === "READY" ? "ok" : s.state === "FAILED" || s.state === "error" ? "bad" : ""}`;
    setText(n.vocabMsg, st.vocabMsg || `${v.phrases.length} phrases${v.edited ? "" : " (the suggestions)"}`
      + (v.skipped.length ? `, ${v.skipped.length} can't go to AWS` : "") + (st.vocabDirty ? " · not saved" : ""));
    const awsBox = n.vocabSkip.lastChild;
    setText(awsBox, v.aws.join(", ") + (v.skipped.length ? `\nNot sent: ${v.skipped.map((x) => `${x.phrase} (${x.why})`).join(", ")}` : ""));
    const enabled = d.aws && d.aws.enabled;
    n.vocabSave.disabled = lock || st.vocabBusy || !st.vocabDirty;
    n.vocabSuggest.disabled = lock || st.vocabBusy;
    n.vocabPush.disabled = lock || st.vocabBusy || !enabled || st.vocabDirty;
    n.vocabPush.title = !enabled ? "Amazon Transcribe runs on the hub: push from there" : st.vocabDirty ? "Save the list first" : "Send the saved list to Amazon Transcribe";
    n.vocab.disabled = n.limit.disabled = n.limitSave.disabled = lock;
  }

  async function saveVocab() {
    if (ro()) return;
    st.vocabBusy = true;
    st.vocabMsg = "";
    renderVocab();
    const phrases = st.n.vocab.value.split("\n").map((x) => x.trim()).filter(Boolean);
    try {
      await api("POST", "/api/cad/settings/stt", { action: "set", key: "vocabulary", value: phrases,
                                                   operator: typeof operator === "function" ? operator() : "" });
      st.vocabDirty = false;
    } catch (e) {
      st.vocabMsg = `Not saved: ${e.message}`;
    }
    st.vocabBusy = false;
    loadStt();
  }

  async function pushVocab() {
    if (ro()) return;
    st.vocabBusy = true;
    st.vocabMsg = "Sending to AWS…";
    renderVocab();
    try {
      const r = await api("POST", "/api/cad/settings/stt", { action: "push", operator: typeof operator === "function" ? operator() : "" });
      st.vocabMsg = r.ok ? "" : `AWS: ${r.error || "refused"}`;
    } catch (e) {
      st.vocabMsg = `Not sent: ${e.message}`;
    }
    st.vocabBusy = false;
    loadStt();
  }

  async function saveLimit() {
    if (ro()) return;
    const v = Number(st.n.limit.value);
    try {
      await api("POST", "/api/cad/settings/stt", { action: "set", key: "aws_monthly_limit_usd", value: v,
                                                   operator: typeof operator === "function" ? operator() : "" });
      st.vocabMsg = "";
    } catch (e) {
      st.vocabMsg = `Limit not saved: ${e.message}`;
    }
    loadStt();
    poll();
  }

  return { mount };
})();
