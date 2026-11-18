// Settings > Radios > Signal (#606): each channel's and each P25 system's signal over time, from the station's metrics
// (GET /api/cad/metrics, the hub's, admins only: sizeup/cloud/metricstore.py, metricsinsight.py). One row of controls
// above every chart: the range (6 h / 24 h / 7 d / 30 d), raw or hourly, which channel. Per FM channel: the idle
// quieting (p50-p90 band, the max), the squelch Open / Close as running and the suggested Open, each transmission (a
// paging channel's pages at open and close); the noise opens a minute; the transmissions by kind; the distribution the
// suggestion comes from. AM: the floor and each transmission's carrier. P25: the decode rate, control channel lost, the
// grants on frequencies no dongle covers. Every event (dongle, gain, squelch, unplug / back, stall, restart, a Settings
// change) a tick on top. Each channel's STT input arrival on the hub after its event (#677: p50 / p95; a talkgroup with
// no signal metrics of its own shows only that). A Health hint links here: #settings/radios/signal?s=FG1&at=<epoch>.
//   SignalPanel.mount(box)   when its tab opens (settings.js)
"use strict";

window.SignalPanel = (() => {
  const RANGES = [["6h", "6 h", 6 * 3600], ["24h", "24 h", 86400], ["7d", "7 d", 7 * 86400], ["30d", "30 d", 30 * 86400]];
  const RAW_MAX_S = 7 * 86400;            // the API's raw span (metricstore RAW_SPAN_S)
  const KINDS = [["voice", "Voice", "sg-voice"], ["page", "Page", "sg-page"], ["burst", "ID burst", "sg-burst"], ["noise", "Noise", "sg-noise"]];
  const HOUR_FM = "idle_q50,idle_q90,idle_qmax,noise_opens,sq_open,sq_close,tx_q,tx_q_open,tx_q_close,noise_q,floor,tx_dbfs,tx_snr,stt_delay";
  const HOUR_CC = "cc_rate,cc_lo,cc_uncovered,stt_delay";
  const HOUR_STT = "stt_delay";
  const st = { box: null, range: "24h", res: "", subject: "", at: null, index: null, sugg: {}, data: {}, error: "", gen: 0,
               loading: false, ro: null, focused: "", loadedAt: 0 };

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  };
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
  const r1 = (v) => (num(v) === null ? "—" : String(Math.round(v * 10) / 10));
  const now = () => (typeof serverNow === "function" ? serverNow() : Date.now() / 1000);
  const span = () => (RANGES.find((r) => r[0] === st.range) || RANGES[1])[2];
  const res = () => (st.res === "raw" && span() <= RAW_MAX_S ? "raw" : st.res === "hour" ? "hour" : span() <= 86400 ? "raw" : "hour");
  async function get(path) {
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(r.status === 403 ? "Admins only" : r.status === 404 ? "On the hub only" : d.error || `HTTP ${r.status}`); e.status = r.status; throw e; }
    return d;
  }

  // the focus a Health hint asked for: ?s=FG1&at=1800000000 (the 24 h around that time)
  function focus() {
    const m = (typeof location !== "undefined" && location.hash || "").match(/\?(.*)$/);
    if (!m || m[1] === st.focused) return false;
    st.focused = m[1];
    const q = new URLSearchParams(m[1]);
    if (q.get("s")) st.subject = q.get("s");
    const at = Number(q.get("at"));
    if (at > 0) { st.at = at; st.range = "24h"; }
    return true;
  }

  function window_() {
    const s = span();
    const to = st.at ? Math.min(now(), st.at + s * 0.75) : now();
    return [Math.floor(to - s), Math.ceil(to)];
  }

  function mount(box) {
    if (!box) return;
    const fresh = st.box !== box;
    st.box = box;
    const moved = focus();                // a Health hint's ?s=&at= (looked at every time: the tab may be open already)
    if (typeof ResizeObserver === "function" && !st.ro) {
      let w = 0;
      st.ro = new ResizeObserver(() => { const nw = Math.round(box.clientWidth / 40); if (nw !== w) { w = nw; render(); } });
      st.ro.observe(box);
    }
    // settings.js mounts it at every render of the tab: the charts load again only when asked for something else, or
    // once a minute (a range button, the channel, Raw / Hourly load at once)
    if (fresh || moved || !st.index || st.error || Date.now() - st.loadedAt > 60000) load();
  }

  // the subjects: each (station, subject) with its kinds, the station's own row left out
  function subjects() {
    const by = new Map();
    for (const s of (st.index && st.index.series) || []) {
      // (the station's own row; and a subject of none of these, e.g. the whisper queue's "stt" records, #639)
      if (s.subject === "station" || !["floor", "tx", "cc", "stt_in"].includes(s.kind)) continue;
      const k = `${s.station}\u0000${s.subject}`;
      if (!by.has(k)) by.set(k, { station: s.station, subject: s.subject, kinds: new Set() });
      by.get(k).kinds.add(s.kind);
    }
    const all = [...by.values()];
    const order = (x) => (x.kinds.has("cc") ? 1 : sttOnly(x) ? 2 : 0);
    return all.sort((a, b) => order(a) - order(b) || a.subject.localeCompare(b.subject));
  }

  async function load() {
    const gen = ++st.gen;
    st.loading = true;
    render();
    try {
      st.index = await get("/api/cad/metrics");
      try {
        const sq = await get("/api/cad/metrics?view=squelch");
        st.sugg = Object.fromEntries((sq.channels || []).map((c) => [`${c.station}\u0000${c.subject}`, c]));
      } catch (_) { st.sugg = {}; }
      const [t0, t1] = window_();
      const r = res();
      const out = {};
      await Promise.all(subjects().filter((x) => !st.subject || x.subject === st.subject).map(async (x) => {
        const q = new URLSearchParams({ subject: x.subject, station: x.station, from: t0, to: t1, res: r });
        if (r === "hour") q.set("metric", x.kinds.has("cc") ? HOUR_CC : sttOnly(x) ? HOUR_STT : HOUR_FM);
        try { out[x.subject] = await get(`/api/cad/metrics?${q}`); } catch (e) { out[x.subject] = { error: e.message }; }
      }));
      if (gen !== st.gen) return;
      st.data = out;
      st.error = "";
    } catch (e) {
      if (gen !== st.gen) return;
      st.error = e.message;
    }
    st.loading = false;
    st.loadedAt = Date.now();
    render();
  }

  // ---- the controls: one row, above everything ------------------------------------------------------------------
  function seg(items, cur, pick, label) {
    const g = el("div", "seg");
    g.setAttribute("role", "group");
    g.setAttribute("aria-label", label);
    for (const [k, text, title, off] of items) {
      const b = el("button", k === cur ? "active" : "", text);
      b.type = "button";
      b.setAttribute("aria-pressed", String(k === cur));
      if (title) b.title = title;
      if (off) b.disabled = true;
      b.addEventListener("click", () => pick(k));
      g.append(b);
    }
    return g;
  }
  function controls() {
    const row = el("div", "row sg-ctl");
    row.append(seg(RANGES.map(([k, t]) => [k, t]), st.range, (k) => { st.range = k; st.at = null; load(); }, "Range"));
    const big = span() > RAW_MAX_S;
    row.append(seg([["raw", "Raw", big ? "Raw: up to 7 d" : "Each minute and each transmission", big],
      ["hour", "Hourly", "Hourly rollups: p50 / p90 / range"]], res(), (k) => { st.res = k; load(); }, "Resolution"));
    const sel = el("select", "sg-pick");
    sel.setAttribute("aria-label", "Channel");
    sel.append(Object.assign(el("option", "", "All"), { value: "" }));
    for (const x of subjects()) sel.append(Object.assign(el("option", "", x.subject), { value: x.subject }));
    sel.value = st.subject;
    sel.addEventListener("change", () => { st.subject = sel.value; load(); });
    row.append(sel);
    if (st.at) {
      const t = el("span", "chip", `at ${typeof fmtTime === "function" ? fmtTime(st.at) : new Date(st.at * 1000).toISOString()}`);
      t.title = "From a Health hint: the 24 h around it. A range button goes back to now";
      row.append(t);
    }
    row.append(el("span", "spacer"), el("span", "dim small", st.loading ? "Loading…" : res() === "raw" ? "raw" : "hourly"));
    return row;
  }

  // ---- the charts ------------------------------------------------------------------------------------------------
  const evText = (e) => `${e.ev}${e.detail ? `: ${e.detail}` : ""}${e.by ? ` (${e.by})` : ""}`;
  const events = (d) => (d.events || []).filter((e) => e.ev !== "dropped" || e.subject === "station")
    .map((e) => ({ at: e.at, ev: e.ev, text: evText(e) }));

  // bucket width: about 120 bars over the range, whole minutes
  const bucket = (t0, t1) => Math.max(60, Math.round((t1 - t0) / 120 / 60) * 60);

  function steps(rows, key) {          // a setting's value over time: a point where it changes
    const out = [];
    for (const r of rows) {
      const v = num(r[key]);
      if (v === null) continue;
      if (!out.length || out[out.length - 1][1] !== v) out.push([r.at, v]);
    }
    return out;
  }

  function fmCharts(x, d, sug, t0, t1, box) {
    const rows = d.rows || [];
    const raw = d.res === "raw";
    const ev = events(d);
    const paging = (sug && sug.kind === "paging") || rows.some((r) => r.k === "floor" && r.kind === "paging");
    const am = (sug && sug.am) || rows.some((r) => r.am);
    const L = [], L2 = [], L3 = [];
    const bw = raw ? bucket(t0, t1) : 3600;
    if (raw) {
      const fl = rows.filter((r) => r.k === "floor");
      const tx = rows.filter((r) => r.k === "tx");
      if (am) {
        L.push({ type: "line", name: "Floor", cls: "sg-c1", gap: 180, pts: fl.filter((r) => num(r.floor) !== null).map((r) => [r.at, r.floor]) });
        L.push({ type: "dots", name: "Carrier", cls: "sg-voice", pts: tx.filter((r) => num(r.cf) !== null)
          .map((r) => [r.at, r.cf, `${r.kind} ${r1(r.dur)} s${num(r.snr) !== null ? ` · SNR ${r1(r.snr)} dB` : ""}`]) });
      } else {
        L.push({ type: "band", name: "Idle q50–q90", cls: "sg-idle", gap: 180, pts: fl.filter((r) => num(r.q90) !== null)
          .map((r) => [r.at, num(r.q50) ?? r.q90, r.q90]) });
        L.push({ type: "line", name: "Idle max", cls: "sg-max", gap: 180, pts: fl.filter((r) => num(r.qmax) !== null).map((r) => [r.at, r.qmax]) });
        L.push({ type: "step", name: "Open", cls: "sg-open", ref: true, pts: steps(fl, "open"), label: "" });
        L.push({ type: "step", name: "Close", cls: "sg-close", ref: true, pts: steps(fl, "close"), label: "" });
        for (const [k, name, cls] of KINDS) {
          const of = tx.filter((r) => r.kind === k);
          if (!of.length) continue;
          L.push({ type: "dots", name, cls, pts: of.map((r) => [r.at, paging ? num(r.qo) ?? r.q : r.q,
            `${r1(r.dur)} s · open ${r1(r.qo)} · close ${r1(r.qc)}${r.id ? " · ID" : ""}`]) });
        }
      }
      // the noise opens a minute and the transmissions by kind, per bucket
      const nb = new Map(), tb = new Map();
      for (const r of fl) {
        const k = Math.floor(r.at / bw) * bw;
        const a = nb.get(k) || [0, 0];
        a[0] += num(r.noise) || 0; a[1] += 1;
        nb.set(k, a);
      }
      for (const r of tx) {
        const k = Math.floor(r.at / bw) * bw;
        const a = tb.get(k) || [0, 0, 0, 0];
        const i = KINDS.findIndex((q) => q[0] === r.kind);
        if (i >= 0) a[i] += 1;
        tb.set(k, a);
      }
      L2.push({ type: "bars", name: "Noise opens/min", cls: "sg-noisebar", pts: [...nb].map(([k, [n, m]]) => [k, k + bw, m ? n / m : 0]) });
      L3.push({ type: "stack", names: KINDS.map((k) => k[1]), classes: KINDS.map((k) => k[2]), pts: [...tb].map(([k, v]) => [k, k + bw, v]) });
    } else {
      const m = (name) => rows.filter((r) => r.metric === name);
      const at = (name) => new Map(m(name).map((r) => [r.at, r]));
      if (am) {
        L.push({ type: "band", name: "Floor range", cls: "sg-idle", gap: 7200, pts: m("floor").map((r) => [r.at, r.lo, r.hi]) });
        L.push({ type: "line", name: "Floor p50", cls: "sg-c1", gap: 7200, pts: m("floor").map((r) => [r.at, r.p50]) });
        L.push({ type: "whisker", name: "Carrier p50", cls: "sg-voice", pts: m("tx_dbfs").map((r) => [r.at, r.lo, r.p50, r.hi]) });
      } else {
        const q50 = at("idle_q50");
        L.push({ type: "band", name: "Idle q50–q90", cls: "sg-idle", gap: 7200, pts: m("idle_q90").map((r) => [r.at, (q50.get(r.at) || r).p50, r.p50]) });
        L.push({ type: "line", name: "Idle max p90", cls: "sg-max", gap: 7200, pts: m("idle_qmax").map((r) => [r.at, r.p90]) });
        L.push({ type: "step", name: "Open", cls: "sg-open", ref: true, pts: steps(m("sq_open").map((r) => ({ at: r.at, v: r.p50 })), "v") });
        L.push({ type: "step", name: "Close", cls: "sg-close", ref: true, pts: steps(m("sq_close").map((r) => ({ at: r.at, v: r.p50 })), "v") });
        L.push({ type: "whisker", name: paging ? "Pages at open p50" : "Transmissions p50", cls: paging ? "sg-page" : "sg-voice",
          pts: m(paging ? "tx_q_open" : "tx_q").map((r) => [r.at, r.lo, r.p50, r.hi]) });
        if (paging) L.push({ type: "dots", name: "Pages at close p50", cls: "sg-close-dot", pts: m("tx_q_close").map((r) => [r.at, r.p50]) });
      }
      L2.push({ type: "bars", name: "Noise opens/min p90", cls: "sg-noisebar", pts: m("noise_opens").map((r) => [r.at, r.at + 3600, r.p90]) });
      const nq = at("noise_q"), kq = at("tx_q");
      L3.push({ type: "stack", names: ["Kept", "Noise"], classes: ["sg-voice", "sg-noise"],
        pts: [...new Set([...kq.keys(), ...nq.keys()])].sort((a, b) => a - b)
          .map((k) => [k, k + 3600, [(kq.get(k) || {}).n || 0, (nq.get(k) || {}).n || 0]]) });
    }
    if (sug && sug.state === "ok" && !am) L.push({ type: "hline", name: "Suggested Open", cls: "sg-sugg", y: sug.open, label: `Sugg. ${r1(sug.open)}`, ref: true });
    // direct labels on the running Open / Close: their latest values
    for (const l of L) if (l.type === "step" && l.pts.length) l.label = `${l.name} ${r1(l.pts[l.pts.length - 1][1])}`;
    const C = window.SignalChart;
    const legend = am ? [["Floor", "sg-c1", "line"], ["Carrier", "sg-voice", "dot"]]
      : [["Idle q50–q90", "sg-idle", "fill"], ["Idle max", "sg-max", "line"], ["Open", "sg-open", "line"],
        ...(sug && sug.state === "ok" ? [["Suggested", "sg-sugg", "line"]] : []),
        ...(raw ? KINDS.map(([, n, c]) => [n, c, "dot"]) : [[paging ? "Pages at open" : "Transmissions", paging ? "sg-page" : "sg-voice", "dot"]])];
    const out = [C.plot({ box, x0: t0, x1: t1, time: true, h: 150, unit: am ? "dBFS" : "dB", layers: L, events: ev, legend,
                          label: `${x.subject}: ${am ? "floor and carrier" : "idle quieting, squelch, transmissions"}` }),
      C.plot({ box, x0: t0, x1: t1, time: true, h: 44, unit: raw ? "/min" : "p90/min", zero: true, ymin_span: 1, layers: L2,
               events: ev, label: `${x.subject}: noise opens a minute`, empty: "No noise opens" }),
      C.plot({ box, x0: t0, x1: t1, time: true, h: 44, unit: raw ? `/${Math.round(bw / 60)} min` : "/h", zero: true, ymin_span: 2,
               yfmt: (v) => String(Math.round(v)), layers: L3, events: ev, label: `${x.subject}: transmissions by kind`,
               legend: raw ? KINDS.map(([, n, c]) => [n, c, "fill"]) : [["Kept", "sg-voice", "fill"], ["Noise", "sg-noise", "fill"]],
               empty: "No transmissions" })];
    if (sug && sug.hist && !am) out.push(distribution(x, sug, box));
    return out;
  }

  // the 7 days the suggestion comes from: the idle minutes' q90, the real transmissions at open, the noise key-ups,
  // each as a share of its own count (one axis: %), with the Open running and the suggested one
  function distribution(x, sug, box) {
    const H = sug.hist, L = [];
    let lo = Infinity, hi = -Infinity;
    const share = (h, name, cls) => {
      const tot = (h.n || []).reduce((a, b) => a + b, 0);
      if (!tot) return;
      const pts = h.n.map((n, i) => [h.lo + i * h.step, h.lo + (i + 1) * h.step, (n / tot) * 100]).filter((p) => p[2] > 0);
      lo = Math.min(lo, pts[0][0]); hi = Math.max(hi, pts[pts.length - 1][1]);
      L.push({ type: "bars", name: `${name} (${tot.toLocaleString()})`, cls, pts });
    };
    share(H.idle, "Idle q90 min", "sg-idle-bar");
    share(H.real, "Real tx at open", "sg-voice");
    share(H.noise, "Noise tx", "sg-noise");
    const refs = [["Open now", sug.cur_open, "sg-open"], ["Suggested", sug.state === "ok" ? sug.open : null, "sg-sugg"],
      ["Noise p90", sug.noise_p90, "sg-max"]];
    refs.forEach(([n, v, cls], i) => { if (num(v) !== null) { L.push({ type: "vline", name: n, x: v, cls, label: `${n} ${r1(v)}`, row: i, ref: true }); lo = Math.min(lo, v - 1); hi = Math.max(hi, v + 1); } });
    if (!(hi > lo)) { lo = 0; hi = 30; }
    const f = window.SignalChart.plot({ box, x0: Math.floor(lo), x1: Math.ceil(hi), xunit: "dB", h: 90, unit: "%", zero: true,
      yfmt: (v) => String(Math.round(v)), layers: L, label: `${x.subject}: quieting distribution, ${sug.days || 7} d`,
      legend: [["Idle q90", "sg-idle-bar", "fill"], ["Real at open", "sg-voice", "fill"], ["Noise tx", "sg-noise", "fill"]],
      empty: "Not enough data" });
    f.title = sug.why || "";
    return f;
  }

  function ccCharts(x, d, t0, t1, box) {
    const rows = d.rows || [], raw = d.res === "raw", ev = events(d);
    const L = [], L2 = [];
    // the control channel lost: from each cc_lost to its cc_back (or now)
    const spans = [];
    let lost = null;
    let first = true;
    for (const e of d.events || []) {
      if (e.ev === "cc_lost" && lost === null) lost = e.at;
      else if (e.ev === "cc_back" && lost !== null) { spans.push([lost, e.at]); lost = null; }
      else if (e.ev === "cc_back" && first) spans.push([t0, e.at]);     // lost before the range began
      if (e.ev === "cc_lost" || e.ev === "cc_back") first = false;
    }
    if (lost !== null) spans.push([lost, t1]);
    L.push({ type: "spans", name: "Control channel lost", cls: "sg-lost", spans });
    const bw = raw ? bucket(t0, t1) : 3600;
    if (raw) {
      const cc = rows.filter((r) => r.k === "cc");
      L.push({ type: "band", name: "Low–high", cls: "sg-idle", gap: 180, pts: cc.filter((r) => num(r.lo) !== null && num(r.hi) !== null).map((r) => [r.at, r.lo, r.hi]) });
      L.push({ type: "line", name: "Decode rate", cls: "sg-c1", gap: 180, pts: cc.filter((r) => num(r.rate) !== null).map((r) => [r.at, r.rate]) });
      const ub = new Map();
      for (const r of cc) {
        const n = Object.values(r.unc || {}).reduce((a, b) => a + b, 0);
        if (!n) continue;
        const k = Math.floor(r.at / bw) * bw;
        ub.set(k, (ub.get(k) || 0) + n);
      }
      L2.push({ type: "bars", name: "Uncovered grants", cls: "sg-page", pts: [...ub].map(([k, n]) => [k, k + bw, n]) });
    } else {
      const m = (name) => rows.filter((r) => r.metric === name);
      const lo = new Map(m("cc_lo").map((r) => [r.at, r.p50]));
      L.push({ type: "band", name: "Low p50–rate p50", cls: "sg-idle", gap: 7200, pts: m("cc_rate").filter((r) => lo.has(r.at)).map((r) => [r.at, lo.get(r.at), r.p50]) });
      L.push({ type: "line", name: "Decode rate p50", cls: "sg-c1", gap: 7200, pts: m("cc_rate").map((r) => [r.at, r.p50]) });
      L2.push({ type: "bars", name: "Uncovered grants, most a minute", cls: "sg-page", pts: m("cc_uncovered").map((r) => [r.at, r.at + 3600, r.hi]) });
    }
    const C = window.SignalChart;
    return [C.plot({ box, x0: t0, x1: t1, time: true, h: 120, unit: "msg/s", zero: true, layers: L, events: ev,
                     label: `${x.subject}: control channel decode rate`,
                     legend: [["Decode rate", "sg-c1", "line"], ["Low–high", "sg-idle", "fill"], ["Lost", "sg-lost", "fill"]] }),
      C.plot({ box, x0: t0, x1: t1, time: true, h: 44, unit: raw ? `/${Math.round(bw / 60)} min` : "max/min", zero: true, ymin_span: 2,
               yfmt: (v) => String(Math.round(v)), layers: L2, events: ev, label: `${x.subject}: grants on uncovered frequencies`,
               empty: "No uncovered grants" })];
  }

  // each STT input's arrival on the hub after its event (#677): raw, a dot each (negative: it came first); hourly,
  // the p50-p95 band and the p50. null without any
  const sttOnly = (x) => x.kinds.has("stt_in") && !x.kinds.has("floor") && !x.kinds.has("cc");
  function sttChart(x, d, t0, t1, box) {
    const rows = d.rows || [], raw = d.res === "raw", ev = events(d);
    const L = [];
    let legend;
    if (raw) {
      const got = rows.filter((r) => r.k === "stt_in" && num(r.delay) !== null);
      if (!got.length) return null;
      for (const [k, name, cls] of [["page", "Page", "sg-page"], ["traffic", "Traffic", "sg-voice"]]) {
        const of = got.filter((r) => (r.kind === "page" ? "page" : "traffic") === k);
        if (of.length) L.push({ type: "dots", name, cls, pts: of.map((r) => [r.at, r.delay, `${r1(r.delay)} s`]) });
      }
      legend = L.map((l) => [l.name, l.cls, "dot"]);
    } else {
      const m = rows.filter((r) => r.metric === "stt_delay");
      if (!m.length) return null;
      L.push({ type: "band", name: "p50–p95", cls: "sg-idle", gap: 7200, pts: m.map((r) => [r.at, r.p50, num(r.p95) ?? r.p90]) });
      L.push({ type: "line", name: "p50", cls: "sg-c1", gap: 7200, pts: m.map((r) => [r.at, r.p50]) });
      legend = [["p50", "sg-c1", "line"], ["p50–p95", "sg-idle", "fill"]];
    }
    const f = window.SignalChart.plot({ box, x0: t0, x1: t1, time: true, h: 70, unit: "s", zero: true, ymin_span: 1,
      layers: L, events: ev, legend, label: `${x.subject}: STT input arrival after its event`, empty: "No STT inputs" });
    f.title = "Event applied → its STT input stored on the hub. Negative: the STT input came first";
    return f;
  }

  // ---- the page --------------------------------------------------------------------------------------------------
  function suggLine(sug) {
    if (!sug) return null;
    if (sug.state !== "ok") { const s = el("span", "dim small", sug.why || "not enough data"); return s; }
    const txt = sug.am ? `Idle q90 p90 ${r1(sug.noise_p90)} dB` : `Suggested ${r1(sug.open)} / ${r1(sug.close)} dB`
      + (num(sug.cur_open) !== null ? ` · now ${r1(sug.cur_open)} / ${r1(sug.cur_close)}` : "") + (sug.info ? ` · ${sug.info}` : "")
      + (sug.warn ? ` · ${sug.warn.split(":")[0]}` : "");
    const s = el("span", `small${sug.capped || sug.warn ? " warn" : ""}`, txt);
    s.title = sug.why || "";
    return s;
  }

  function card(x) {
    const d = st.data[x.subject] || {};
    const sug = st.sugg[`${x.station}\u0000${x.subject}`];
    const c = el("section", "card sg-card");
    c.dataset.subject = x.subject;
    const head = el("div", "card-head");
    const cc = x.kinds.has("cc");
    const fl = (d.rows || []).find((r) => r.k === "floor" && (r.kind || r.mhz));
    const only = sttOnly(x);
    const kind = cc ? "P25" : only ? "STT input arrival" : [sug && sug.am ? "AM" : "FM", (sug && sug.kind) || (fl && fl.kind) || ""].filter(Boolean).join(" · ");
    head.append(el("h2", "", x.subject), el("span", "dim small", kind + (fl && fl.mhz ? ` · ${fl.mhz} MHz` : "")));
    head.append(el("span", "spacer"));
    const sl = !cc && !only && suggLine(sug);
    if (sl) head.append(sl);
    c.append(head);
    if (d.error) { c.append(el("p", "error small", d.error)); return c; }
    const [t0, t1] = [d.from ?? window_()[0], d.to ?? window_()[1]];
    const charts = only ? [] : cc ? ccCharts(x, d, t0, t1, st.box) : fmCharts(x, d, sug, t0, t1, st.box);
    const stt = x.kinds.has("stt_in") ? sttChart(x, d, t0, t1, st.box) : null;
    if (stt) charts.push(stt);
    else if (only) charts.push(el("p", "dim small", "No STT inputs in this range"));
    c.append(...charts);
    if (d.truncated) c.append(el("p", "dim small", "Cut at 20,000 rows: pick a shorter range or Hourly"));
    return c;
  }

  function render() {
    if (!st.box) return;
    if (st.error) {
      st.box.replaceChildren(el("p", "empty", st.error));
      return;
    }
    if (!st.index) { st.box.replaceChildren(el("div", "loading", "Loading…")); return; }
    const list = subjects().filter((x) => !st.subject || x.subject === st.subject);
    const kids = [controls()];
    if (!list.length) kids.push(el("p", "empty", st.subject ? `No metrics for ${st.subject}` : "No station metrics yet"));
    for (const x of list) if (st.data[x.subject]) kids.push(card(x));
    st.box.replaceChildren(...kids);
  }

  return { mount, _st: st, _fmCharts: fmCharts, _ccCharts: ccCharts, _distribution: distribution, _sttChart: sttChart, _render: render };
})();
