// Settings > Radios > Squelch: each FM channel's noise squelch (sizeup/radio/fm.py), set per channel and applied
// live on the station (sizeup/channels.py: sent with the channel choices; audited as Radios).
//   Open      quieting (dB under the idle hiss) that opens the squelch
//   Close     ... it stays open above (hysteresis)
//   Min       carrier shorter than this: nothing kept, never shown as talking
// With each: the noise floor (idle hiss, relative dB), the quieting now, and the short opens a minute (opens that
// closed with nothing kept, last 5 min): many means the squelch is chattering on noise, so raise Open.
// GET /api/cad/squelch (polled while the tab is on screen), POST /api/cad/settings/squelch.
//   SquelchPanel.mount(box)   when its tab opens (settings.js)
"use strict";

window.SquelchPanel = (() => {
  const POLL_MS = 3000;
  const st = { box: null, timer: null, data: null, error: "", sig: "" };
  const FIELDS = [["open_db", "Open", "dB", "Quieting under the idle hiss that opens the squelch (dB). Higher: fewer noise opens, weak carriers missed."],
    ["close_db", "Close", "dB", "Stays open above this quieting (dB), under Open."],
    ["min_open_s", "Min", "s", "Carrier shorter than this: nothing kept, never shown as TX (s)."]];
  const ro = () => typeof canDo === "function" && !canDo("admin");
  const num = (v, d = 1) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));

  function mount(box) {
    st.box = box;
    st.sig = "";
    poll();
  }

  async function poll() {
    clearTimeout(st.timer);
    if (!st.box || !st.box.isConnected || st.box.offsetParent === null) return;   // the tab closed: stop
    if (document.visibilityState !== "hidden") {
      try {
        st.data = await api("GET", "/api/cad/squelch");
        st.error = "";
      } catch (e) {
        st.error = `Couldn't load: ${e.message}`;
      }
      render();
    }
    st.timer = setTimeout(poll, POLL_MS);
  }

  async function save(key, field, raw) {
    const v = raw === "" ? null : Number(raw);
    try {
      await api("POST", "/api/cad/settings/squelch", { action: "set", key, [field]: v,
        operator: typeof operator === "function" ? operator() : "" });
      st.error = "";
    } catch (e) {
      st.error = `${key}: ${e.message}`;
    }
    st.sig = "";
    poll();
  }

  async function reset(key) {
    try {
      await api("POST", "/api/cad/settings/squelch", { action: "reset", key,
        operator: typeof operator === "function" ? operator() : "" });
      st.error = "";
    } catch (e) {
      st.error = `${key}: ${e.message}`;
    }
    st.sig = "";
    poll();
  }

  function input(c, f, ranges) {
    const [key, , unit, help] = f;
    const [lo, hi, step] = ranges[key] || [0, 100, 0.1];
    const i = el("input", "sq-in");
    const locked = !c.editable;            // paging (South 2): read-only, a high Open would mute the pages
    Object.assign(i, { type: "number", min: lo, max: hi, step, disabled: ro() || locked });
    const set = c.set[key];
    const dflt = c.default[key];
    i.value = set === undefined ? "" : set;
    i.placeholder = dflt === undefined || dflt === null ? "" : String(dflt);
    const run = c.running[key];
    i.title = `${help}\nDefault ${dflt ?? "—"} ${unit}` + (run !== undefined && run !== null ? ` · running ${run} ${unit}` : "")
      + (locked ? `\nRead-only: ${c.kind || "not a traffic"} channel. A high Open would mute its pages; stays at the default.`
        : "\nEmpty: the default");
    i.setAttribute("aria-label", `${c.key} ${f[1]}`);
    i.addEventListener("change", () => save(c.key, key, i.value.trim()));
    return i;
  }

  function render() {
    if (!st.box) return;
    const d = st.data || { channels: [], sync: {}, ranges: {} };
    const editing = st.box.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
    const sig = JSON.stringify([d, st.error, ro()]);
    if (sig === st.sig || editing) return;          // never under someone typing
    st.sig = sig;
    const chatter = d.chatter_per_min || 2;
    const t = el("table", "evtable sq-table");
    const head = el("tr");
    for (const [h, tip] of [["Channel", ""], ...FIELDS.map((f) => [`${f[1]} ${f[2]}`, f[3]]),
      ["Floor dB", "Idle hiss the quieting is measured from (relative dB)"], ["Now dB", "Quieting now"],
      ["Short/min", `Opens with nothing kept, per minute (last 5 min). ${chatter}+: chattering on noise, raise Open`],
      ["Key-ups 1 h", "Transmissions kept in the last hour · bare: no speech, no ID"],
      ["PL est.", "Estimated CTCSS tone per key-up (last hour). none: no tone line heard; unsure: too short, speech or hum. "
        + "FG1 runs PL 100: a key-up without it may be another user or interference"], ["", ""]]) {
      const th = el("th", "", h);
      if (tip) th.title = tip;
      head.append(th);
    }
    t.append(el("thead"), el("tbody"));
    t.tHead.append(head);
    for (const c of d.channels || []) {
      const tr = el("tr");
      const name = el("td", "", c.key);
      name.dataset.l = "Channel";
      if (c.mhz) name.title = `${c.mhz} MHz · ${c.state || ""}`;
      tr.append(name);
      for (const f of FIELDS) {
        const td = el("td");
        td.dataset.l = f[1];
        td.append(input(c, f, d.ranges || {}));
        tr.append(td);
      }
      const short = c.short_per_min;
      const hot = typeof short === "number" && short >= chatter;
      for (const [l, v, cls] of [["Floor dB", num(c.hiss_db), ""], ["Now dB", c.open ? `${num(c.quieting_db)} open` : num(c.quieting_db), ""],
        ["Short/min", num(short), hot ? "warn" : ""]]) {
        const td = el("td", cls, v);
        td.dataset.l = l;
        if (hot && l === "Short/min") td.title = "Chattering on noise: raise Open";
        tr.append(td);
      }
      const k = c.keyups;
      const ku = el("td", "", k ? `${k.n}${k.no_speech ? ` · ${k.no_speech} bare` : ""}` : "—");
      ku.dataset.l = "Key-ups 1 h";
      if (k && k.no_speech) {
        ku.title = `Bare key-ups: ${k.bare_s.join(", ")} s · quieting ${k.bare_q.join(", ")} dB`
          + "\nLast: " + k.recent.map((r) => `${new Date(r[0] * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
            + ` ${r[1]} s ${r[2]} dB PL est. ${r[3] === "tone" ? r[4] : r[3]}${r[5] ? " ID" : ""}${r[6] ? "" : " bare"}`).join("; ");
      }
      const pl = el("td", k && k.tones.none ? "warn" : "", k ? Object.entries(k.tones).map(([t, n]) => `${t}×${n}`).join(" · ") || "—" : "—");
      pl.dataset.l = "PL est.";
      tr.append(ku, pl);
      const act = el("td");
      if (!c.editable) {
        const lk = el("span", "dim small", "Read-only");
        lk.title = "Paging channel: its squelch stays at the receiver's default (a high Open would mute pages)";
        act.append(lk);
      } else if (Object.keys(c.set || {}).length && !ro()) {
        const b = el("button", "mini", "Default");
        b.type = "button";
        b.title = `${c.key}: back to the defaults`;
        b.addEventListener("click", () => reset(c.key));
        act.append(b);
      }
      tr.append(act);
      t.tBodies[0].append(tr);
    }
    const kids = [];
    if (!(d.channels || []).length) kids.push(el("div", "empty", "No FM channels reported"));
    else kids.push(t);
    for (const o of d.orphans || []) {     // set for a channel that is gone, renamed or paging: not applied
      const r = el("div", "row small warn");
      const what = FIELDS.filter((f) => o.set[f[0]] !== undefined).map((f) => `${f[1]} ${o.set[f[0]]}`).join(" · ");
      r.append(el("span", "", `${o.key}: ${what} · not applied`));
      r.title = "No traffic channel by this name on the station: left out of what it gets";
      if (!ro()) {
        const b = el("button", "mini", "Delete");
        b.type = "button";
        b.addEventListener("click", () => reset(o.key));
        r.append(b);
      }
      kids.push(r);
    }
    const sync = d.sync || {};
    if (sync.state && sync.state !== "local") {
      const s = el("div", `dim small${sync.state === "pending" || sync.state === "offline" ? " warn" : ""}`,
        sync.state === "applied" ? "Applied" : sync.state === "hub" ? "Set on the hub" : sync.detail || sync.state);
      s.title = sync.detail || "";
      kids.push(s);
    }
    if (st.error) kids.push(el("div", "error small", st.error));
    st.box.replaceChildren(...kids);
  }

  return { mount };
})();
