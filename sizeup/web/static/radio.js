// Shared by the CAD console (/cad, its Radio view too) and the wall board (/monitor):
// who is transmitting right now ("talking", pushed in each page's state), the talk alerts
// (a toast and an optional chime when chosen units key up), and the rig + radio ID tag on a
// transmission. The server says who is talking (sizeup/radio/live.py); nothing here guesses.
//
//   Radio.update(talking, paging)  every state push: redraws #talking (a page going out first), raises alerts
//   Radio.fillPaging(box, paging, done)  a page going out now as a row (the pinned feed, Radio, the board's
//                               Pages); `done`: finished page ids already listed there (the row hands over)
//   Radio.pageTags(tones)       one tag per tone pair: the department(s), or the tones when nobody's code
//   Radio.talkingFor(rig)       the entry for a rig transmitting now (unit board highlight), or null
//   Radio.rigTag(t)             "E63 · 6301" for a transmission row
//   Radio.mountAlerts(box)      the "🔔 Alerts" button + its settings (kept in this browser)
"use strict";

window.Radio = (() => {
  const ALERT_KEY = "sizeup.alerts";
  const DEFAULT_KINDS = ["engine", "rescue"];          // "all PCFD engines and rescues" when first switched on
  const TOAST_MS = 7000;
  const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
  const st = { talking: [], paging: [], seen: null, rigs: null, alerts: loadAlerts(), panel: null, btn: null,
               chime: new Audio(), chimeOk: false, toasts: null };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  // ---- what an entry says --------------------------------------------------------------------

  function who(e) {
    if (e.rig) return e.rig;
    if (e.dispatcher) return (e.alias || "").replace(/^(\d+) Ctrl\b.*$/i, "$1 Control") || "Dispatcher";
    if (e.alias) return e.alias;
    return "";
  }
  function label(e) {
    const w = who(e);
    if (e.ended) return `${w || e.radio_id || "Radio"} ${e.channel} ended`;
    if (w) return `TX ${w} ${e.channel}`;
    if (e.system === "fm" && e.id_on_release) return `${e.channel} · ID on release`;   // MDC post-ID: known when the PTT is let go
    if (e.system === "fm") return `${e.channel} open`;
    return `TX ${e.channel}` + (e.radio_id ? ` radio ${e.radio_id}` : "");
  }
  function chipClass(e) {          // the strip's and the Overview's chip: rig, 60 Control, ended, no ID yet
    return "talk-chip" + (e.ended ? " ended" : "") + (e.rig ? " rig" : "") + (e.dispatcher ? " disp" : "")
      + (e.id_on_release && !e.rig ? " unid" : "");
  }
  function shown(e) {             // the strip: our rigs, FG1, and the Main talkgroups
    return !!e.rig || e.system === "fm" || e.main;
  }

  function talkingFor(rig) {
    return st.talking.find((e) => e.rig && e.rig.split(/,\s*/).includes(rig)) || null;
  }

  // ---- a page going out now (South 2, sizeup/radio/livepage.py) ------------------------------------
  // {channel, started, pairs: [{a, b, names, ours}], phase: "tones" | "voice", ours, ended, page_id}. A pair
  // nobody's code matches is shown as its tones ("741.3/623.7"), never a guess. "(heard)" is dropped (confirmed
  // on the air); "(est.)" stays (an estimated code).

  const hz = (x) => Number(x).toFixed(1);
  // The tone book's names (#122): "Places · Service[ · Role]"; several places shortened to the first one and "+N" in a
  // tag ("Port Chester +2 · EMS"), the whole name in its title. Two names on one pair: a conflict in the tone book.
  const SERVICES = ["Fire", "EMS", "Police", "Other"];
  function shortName(n) {
    const parts = n.split(" · ");
    const i = parts.findIndex((p, k) => k > 0 && SERVICES.includes(p.replace(/ \((heard|est\.)\)$/, "")));
    return i > 1 ? [`${parts[0]} +${i - 1}`, ...parts.slice(i)].join(" · ") : n;
  }
  const pairName = (p, short = false) => (p.names || []).map((n) => n.replace(/ \(heard\)$/, ""))
    .map((n) => (short ? shortName(n) : n)).join(" / ");
  const pairTones = (p) => hz(p.a ?? p.a_hz) + ((p.b ?? p.b_hz) ? `/${hz(p.b ?? p.b_hz)}` : "");
  function pageWords(p) {
    return [p.channel, p.ended ? "Paged" : "Paging", ...(p.pairs || []).map((x) => pairName(x, true) || pairTones(x)),
            ...(p.phase === "voice" ? ["voice"] : [])].join(" · ");
  }
  function pageTags(tones) {
    return (tones || []).map((x) => {
      const name = pairName(x), conflict = (x.names || []).length > 1;
      const t = el("span", "page-dept" + (x.ours ? " ours" : "") + (name ? "" : " unk") + (conflict ? " conflict" : ""),
        pairName(x, true) || pairTones(x));
      t.title = name ? `${name}: ${pairTones(x)} Hz` : `${pairTones(x)} Hz: no department has this code yet`;
      if (conflict) t.title += "\nConflict: this pair is under each of these in the tone book (Settings > Radios > Tones)";
      return t;
    });
  }
  function pagingChip(p) {
    const c = el("span", "talk-chip paging" + (p.ours ? " ours" : "") + (p.ended ? " ended" : ""));
    if (window.Icon) c.append(Icon("page"));
    c.append(el("span", "", pageWords(p)));
    c.title = (p.pairs || []).map((x) => `${pairName(x) || "unknown"} ${pairTones(x)} Hz`).join(", ");
    return c;
  }
  function pagingRow(p) {
    const r = el("div", "paging-row" + (p.ours ? " ours" : "") + (p.ended ? " ended" : ""));
    if (window.Icon) r.append(Icon("page"));
    r.append(el("b", "", p.channel), el("span", "pg-what", p.ended ? "Paged" : "Paging"), ...pageTags(p.pairs));
    if (p.phase === "voice") r.append(el("span", "dim", "voice"));
    r.title = pageWords(p);
    return r;
  }
  function fillPaging(box, paging, done) {
    if (!box) return;
    const items = (Array.isArray(paging) ? paging : []).filter((p) => !(p.ended && done && done.has(p.page_id)));
    const sig = JSON.stringify(items.map((p) => [p.key, p.pairs, p.phase, p.ended]));
    if (box._sig === sig) return;
    box._sig = sig;
    box.replaceChildren(...items.map(pagingRow));
    box.hidden = !items.length;
  }

  // ---- the strip ----------------------------------------------------------------------------------

  function renderStrip() {
    const box = document.getElementById("talking");
    if (!box) return;
    const items = st.talking.filter(shown).slice(0, 6);
    const sig = JSON.stringify([st.paging.map((p) => [p.key, p.pairs, p.phase, p.ended]),
                                items.map((e) => [e.key, e.rig, e.alias, e.ended, e.channel, e.id_on_release])]);
    if (box._sig === sig) return;
    box._sig = sig;
    box.replaceChildren(...st.paging.map(pagingChip), ...items.map((e) => {
      const c = el("span", chipClass(e));
      c.append(el("span", "", label(e)));
      c.title = [e.radio_id ? `radio ${e.radio_id}` : "", e.alias && e.alias !== e.rig ? `alias ${e.alias}` : "",
                 e.label && e.label !== e.alias ? e.label : "", e.tg ? `TG ${e.tg}` : ""].filter(Boolean).join(", ")
        || "Transmitting now";
      return c;
    }));
    box.hidden = !items.length && !st.paging.length;
  }

  // ---- alerts -------------------------------------------------------------------------------------

  function loadAlerts() {
    try {
      const a = JSON.parse(localStorage.getItem(ALERT_KEY) || "null");
      if (a && typeof a === "object") {
        return { on: !!a.on, chime: !!a.chime, units: Array.isArray(a.units) ? a.units : null, prealert: a.prealert !== false };
      }
    } catch (_) { /* no storage: defaults */ }
    return { on: false, chime: false, units: null, prealert: true };
  }
  function saveAlerts() {
    try { localStorage.setItem(ALERT_KEY, JSON.stringify(st.alerts)); } catch (_) { /* not remembered */ }
  }
  function defaultUnits() {
    return (st.rigs || []).filter((u) => u.pcfd && DEFAULT_KINDS.includes(u.kind)).map((u) => u.name);
  }
  function chosen() {
    return new Set(st.alerts.units || defaultUnits());
  }

  async function loadRigs() {
    if (st.rigs) return st.rigs;
    try {
      const r = await fetch("/api/cad/rigs");
      st.rigs = r.ok ? (await r.json()).units || [] : [];
    } catch (_) {
      st.rigs = [];
    }
    return st.rigs;
  }

  // A short two-note chime, made here (no file to fetch): 16-bit mono WAV.
  function chimeWav() {
    const rate = 16000, notes = [[880, 0.12], [1320, 0.2]];
    const n = notes.reduce((s, [, d]) => s + Math.round(d * rate), 0);
    const buf = new DataView(new ArrayBuffer(44 + n * 2));
    const w = (o, s) => [...s].forEach((ch, i) => buf.setUint8(o + i, ch.charCodeAt(0)));
    w(0, "RIFF"); buf.setUint32(4, 36 + n * 2, true); w(8, "WAVEfmt "); buf.setUint32(16, 16, true);
    buf.setUint16(20, 1, true); buf.setUint16(22, 1, true); buf.setUint32(24, rate, true); buf.setUint32(28, rate * 2, true);
    buf.setUint16(32, 2, true); buf.setUint16(34, 16, true); w(36, "data"); buf.setUint32(40, n * 2, true);
    let k = 0;
    for (const [hz, d] of notes) {
      const m = Math.round(d * rate);
      for (let i = 0; i < m; i++, k++) {
        const env = Math.min(1, i / 80) * Math.exp(-3 * i / m);
        buf.setInt16(44 + k * 2, Math.round(9000 * env * Math.sin(2 * Math.PI * hz * i / rate)), true);
      }
    }
    let bin = "";
    new Uint8Array(buf.buffer).forEach((b) => { bin += String.fromCharCode(b); });
    return "data:audio/wav;base64," + btoa(bin);
  }
  const CHIME = chimeWav();

  // Browsers allow sound only after a click: the click that turns the chime on unlocks it
  // (the same trick as the Live and Sound buttons, radio-view.js).
  function unlockChime() {
    st.chime.src = SILENT_WAV;
    st.chime.play().then(() => { st.chimeOk = true; renderButton(); })
      .catch(() => { st.chimeOk = false; renderButton(); });
  }
  function ring() {
    if (!st.alerts.chime) return;
    st.chime.src = CHIME;
    st.chime.play().then(() => { st.chimeOk = true; }).catch(() => { st.chimeOk = false; renderButton(); });
  }

  function toast(text, sub) {          // ui.js's toast, held longer: a unit keyed up
    UI.toast(text, "ok", sub, TOAST_MS);
  }

  // The phone pre-alert (prealert.js): a toast, and the chime when it is on, unless turned off here.
  function prealert(text, sub) {
    if (!st.alerts.prealert) return;
    UI.toast(text, "alert", sub, TOAST_MS);
    ring();
  }

  function alertNew(entries) {
    const fresh = [];
    const now = new Set();
    for (const e of entries) {
      for (const rig of (e.rig || "").split(/,\s*/).filter(Boolean)) {
        const k = `${e.key}|${rig}`;
        now.add(k);
        if (st.seen && !st.seen.has(k)) fresh.push([rig, e]);
      }
    }
    // keep keys a little while so a flicker doesn't alert twice
    st.seen = new Set([...now, ...[...(st.seen || [])].slice(-200)]);
    if (!st.alerts.on || !fresh.length) return;
    const want = chosen();
    let rang = false;
    for (const [rig, e] of fresh) {
      if (!want.has(rig)) continue;
      toast(`${e.ended ? "" : "TX "}${rig} ${e.channel}${e.ended ? " ended" : ""}`,
            [e.radio_id ? `radio ${e.radio_id}` : "", e.label || ""].filter(Boolean).join(", "));
      if (!rang) { ring(); rang = true; }
    }
  }

  function update(talking, paging) {
    st.talking = Array.isArray(talking) ? talking : [];
    st.paging = Array.isArray(paging) ? paging : [];
    renderStrip();
    if (st.alerts.on && !st.rigs) loadRigs();
    alertNew(st.talking);
  }

  // ---- the alert settings ------------------------------------------------------------------------

  function renderButton() {
    if (!st.btn) return;
    const a = st.alerts;
    st.btn.replaceChildren(window.Icon ? Icon("bell") : "",
      el("span", "lbl", a.on ? (a.chime && !st.chimeOk ? "Alerts: tap for sound" : "Alerts on") : "Alerts"));
    st.btn.classList.toggle("active", a.on);
    st.btn.title = a.on ? `Talk alerts: ${(a.units || ["PCFD engines and rescues"]).length > 6 ? "chosen units"
      : (a.units || ["PCFD engines and rescues"]).join(", ")}${a.chime ? ", chime" : ""}`
      : "Talk alerts off";
  }

  function mountAlerts(box) {
    if (!box) return;
    st.btn = el("button", "nav alerts-btn");
    st.btn.type = "button";
    st.btn.setAttribute("aria-label", "Talk alerts");
    st.btn.setAttribute("aria-haspopup", "dialog");
    st.btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (st.alerts.on && st.alerts.chime && !st.chimeOk) unlockChime();
      togglePanel();
    });
    box.replaceChildren(st.btn);
    renderButton();
    document.addEventListener("click", (ev) => {
      if (st.panel && !st.panel.hidden && !st.panel.contains(ev.target) && ev.target !== st.btn) hidePanel();
    });
    document.addEventListener("keydown", (ev) => {         // Esc: closed, and the focus back on the button
      if (ev.key !== "Escape" || !st.panel || st.panel.hidden) return;
      const inside = st.panel.contains(document.activeElement);
      hidePanel();
      if (inside) st.btn.focus();
    });
  }

  function hidePanel() {
    st.panel.hidden = true;
    st.btn.setAttribute("aria-expanded", "false");
  }
  async function togglePanel() {
    if (st.panel && !st.panel.hidden) { hidePanel(); return; }
    st.btn.setAttribute("aria-expanded", "true");
    if (!st.panel) {
      st.panel = el("div", "alerts-panel");
      st.panel.setAttribute("role", "dialog");
      st.panel.setAttribute("aria-label", "Talk alerts");
      document.body.append(st.panel);
    }
    st.panel.hidden = false;
    st.panel.replaceChildren(el("div", "loading", "Loading"));
    await loadRigs();
    renderPanel();
    const first = st.panel.querySelector("input, button");
    if (first) first.focus();
  }

  function check(text, on, onChange, cls = "") {
    const l = el("label", "check " + cls);
    const i = el("input");
    i.type = "checkbox";
    i.checked = on;
    i.addEventListener("change", () => onChange(i.checked));
    l.append(i, el("span", "", text));
    return l;
  }

  function renderPanel() {
    const p = st.panel, a = st.alerts;
    const head = el("div", "ap-head");
    head.append(el("b", "", "Talk alerts"), el("span", "spacer"));
    const x = el("button", "icon quiet sm");
    x.append(window.Icon ? Icon("x") : "✕");
    x.type = "button";
    x.setAttribute("aria-label", "Close");
    x.title = "Close (Esc)";
    x.addEventListener("click", () => { hidePanel(); st.btn.focus(); });
    head.append(x);
    const opts = el("div", "ap-opts");
    opts.append(check("Alerts on", a.on, (v) => { a.on = v; saveAlerts(); renderButton(); renderPanel(); }),
                check("Chime", a.chime, (v) => { a.chime = v; saveAlerts(); if (v) unlockChime(); renderButton(); }),
                check("Phone pre-alert", a.prealert, (v) => { a.prealert = v; saveAlerts(); }));
    const want = chosen();
    const set = (names) => { a.units = [...names]; saveAlerts(); renderButton(); renderPanel(); };
    const quick = el("div", "ap-quick");
    for (const [t, fn] of [["Engines & rescues", () => defaultUnits()],
                           ["All PCFD", () => st.rigs.filter((u) => u.pcfd).map((u) => u.name)],
                           ["None", () => []]]) {
      const b = el("button", "mini", t);
      b.type = "button";
      b.addEventListener("click", () => set(fn()));
      quick.append(b);
    }
    const grid = el("div", "ap-units");
    const groups = [["PCFD", st.rigs.filter((u) => u.pcfd)], ["Others", st.rigs.filter((u) => !u.pcfd)]];
    for (const [title, rows] of groups) {
      if (!rows.length) continue;
      grid.append(el("div", "ap-gtitle dim", title));
      const g = el("div", "ap-grid");
      for (const u of rows) {
        g.append(check(u.name, want.has(u.name), (v) => {
          const s = chosen();
          if (v) s.add(u.name); else s.delete(u.name);
          set(s);
        }, "ap-unit"));
      }
      grid.append(g);
    }
    if (!st.rigs.length) grid.append(el("div", "dim", "No unit list"));
    const note = el("div", "dim small", "This browser only");
    note.title = "A toast when a chosen unit keys up (P25: when the radio is named; FG1: when its MDC ID decodes). "
      + "Phone pre-alert: a toast (and the chime, when on) when Tapout hears the HQ phone ring, even with talk alerts off.";
    p.replaceChildren(head, opts, quick, grid, note);
    if (!a.on) grid.classList.add("off");
  }

  // ---- transmission rows ---------------------------------------------------------------------------

  // "E63 · 6301": the rig and the raw radio ID (and the talker alias when it says more).
  function rigTag(t) {
    const box = el("span", "rigtag");
    if (t.rig) {
      const b = el("b", "unit rig", t.rig);
      if (talkingFor(t.rig.split(/,\s*/)[0])) b.classList.add("talking");
      box.append(b);
      const extra = [t.radio_id, t.unit && t.unit !== t.rig && t.unit !== t.radio_id ? t.unit : ""].filter(Boolean);
      if (extra.length) box.append(el("span", "dim rid", extra.join(" ")));
    } else if (t.unit) {
      box.append(el("b", "unit", t.unit));
      if (t.radio_id && t.radio_id !== t.unit) box.append(el("span", "dim rid", t.radio_id));
    } else if (t.radio_id) {
      box.append(el("b", "unit", t.radio_id));
    }
    return box.childNodes.length ? box : null;
  }

  return { update, talkingFor, rigTag, mountAlerts, label, chipClass, who, shown, toast, prealert, fillPaging, pageTags, shortName,
           get talking() { return st.talking; }, get paging() { return st.paging; } };
})();
