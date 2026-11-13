// Settings > Station > Logs (sizeup/cloud/cwlogs.py): the station's logs (shipped by sizeup/cloud/logship.py, scrubbed)
// and the hub's own, in CloudWatch, through the hub: GET /api/cad/logs/status (set up or not, today's volume, the
// station's shipper) and GET /api/cad/logs (Logs Insights, bounded: 7 days, 500 rows, one query at a time). Admins
// with a sign-in only; nothing polls: a query runs when asked. The settings (on, level, units, retention) are the CAD
// settings' "station_logs" (S.data.station_logs, POST /api/cad/settings/station_logs). Mounted by settings.js.
"use strict";
const StationLogsView = (() => {
  const LAST = [["900", "15 min"], ["3600", "1 h"], ["21600", "6 h"], ["86400", "24 h"], ["604800", "7 d"]];
  const q = { source: "all", unit: "", level: "", text: "", last: "3600", around: null };
  let node = null, status = null, rows = null, said = "", busy = false;

  const kb = (n) => (n === null || n === undefined ? "—" : n < 1e6 ? `${Math.round(n / 1024)} KB` : `${(n / 1e6).toFixed(1)} MB`);
  const when = (t) => (t ? fmtTime(t, true) : "—");
  const day = (t) => new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" });

  async function get(path) {
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  function line() {                           // one line: CloudWatch, today, the station's shipper
    const box = el("div", "row lg-status small");
    if (!status) { box.append(el("span", "dim", said || "Loading…")); return box; }
    const ok = status.configured === true;
    const cw = el("span", `chip ${ok ? "hl-ok" : "hl-bad"}`, ok ? "CloudWatch" : "Not configured");
    cw.title = ok ? `${status.group} · kept ${status.retention_days} d` : `${status.reason || ""}`;
    box.append(cw);
    if (ok) box.append(el("span", "", `${status.retention_days} d`),
      Object.assign(el("span", "dim", `today ${(status.today.lines || 0).toLocaleString()} lines · ${kb(status.today.bytes)}`),
        { title: `UTC day · ${status.today.dropped || 0} dropped here · last write ${when(status.last_put)}` }));
    else box.append(el("span", "dim", "sizeup-aws step 25"));
    const st = status.station;
    if (st) {
      const c = el("span", `chip ${st.state === "ok" ? "hl-ok" : st.state === "off" ? "dim" : "hl-bad"}`, `Station ${st.state}`);
      c.title = st.reason || "";
      const n = el("span", "dim", `spool ${kb(st.spool_bytes)} · sent ${(st.sent_lines || 0).toLocaleString()}`);
      n.title = `Since the station started · dropped: ${st.dropped_secret || 0} credential, ${st.dropped_adapter || 0} adapter, `
        + `${st.dropped_level || 0} level, ${st.repeats || 0} repeats, ${st.dropped_error || 0} errors, `
        + `${st.dropped_spool || 0} batches over the spool cap, ${st.rejected || 0} refused by the hub`;
      box.append(c, n);
    } else box.append(el("span", "chip dim", "Station —"));
    if (status.error) box.append(Object.assign(el("span", "warn", "!"), { title: status.error }));
    return box;
  }

  function settingsCard() {
    const s = S.data && S.data.station_logs;
    if (!s) return null;
    const admin = canDo("admin");
    const card = sCard(sCardHead("Shipping", s.on.value ? `${s.level.value}+` : "off",
      sHelp("The station sends its sizeup units' journal every ~45 s, gzipped, over its hub link. Credentials are "
        + "redacted; a line that still looks like one is dropped. The adapter's own lines stay on the station unless turned on. The same line repeated is sent 5 times per 10 min, then "
        + "counted. Kept on the station's disk (50 MB) while the hub or CloudWatch is away.")));
    const [lab, cb] = sCheck(s.on.label, s.on.value, "Off: the station reads nothing");
    cb.disabled = !admin;
    cb.addEventListener("change", () => sPost("station_logs", { action: "set", key: "on", value: cb.checked },
      `Station logs: ${cb.checked ? "on" : "off"}`));
    const r1 = el("div", "row");
    r1.append(lab);
    card.append(r1);
    const [alab, acb] = sCheck(s.adapter.label, s.adapter.value,
      "The incident-source adapter's own lines (county CAD details). Off: left on the station");
    acb.disabled = !admin;
    acb.addEventListener("change", () => {
      if (!acb.checked) {
        sPost("station_logs", { action: "set", key: "adapter", value: false }, "Adapter lines: off");
        return;
      }
      acb.checked = false;
      sAsk("Ship the adapter's own lines? They can hold county CAD details. Credentials are scrubbed, but a line "
        + "the scrub misses would reach CloudWatch.", () => sPost("station_logs", { action: "set", key: "adapter", value: true },
        "Adapter lines: on"), "Ship them");
    });
    const r2 = el("div", "row");
    r2.append(alab);
    card.append(r2);
    if (s.adapter.value) card.append(el("div", "warn small", "Adapter lines are shipped"));
    const lv = sSelect(s.levels.map((v) => [v, v]), s.level.value,
      (v) => sPost("station_logs", { action: "set", key: "level", value: v }, `Least level: ${v}`), s.level.label);
    const keep = sSelect(s.retention_choices.map((v) => [String(v), `${v} d`]), String(s.retention_days.value),
      (v) => sPost("station_logs", { action: "set", key: "retention_days", value: Number(v) }, `Kept ${v} days`), s.retention_days.label);
    keep.title = "CloudWatch retention of /sizeup/station";
    for (const [label, ctl, key] of [[s.level.label, lv, "level"], [s.retention_days.label, keep, "retention_days"]]) {
      ctl.disabled = !admin;
      const row = el("div", "row");
      row.append(el("span", "s-eta-l", label), ctl, sState(s[key].edited ? "edited" : "default"));
      card.append(row);
    }
    for (const key of ["include", "exclude"]) {
      const v = s[key];
      const i = sInput(v.value.join(", "), 600, key === "include" ? "All units" : "None", (t) =>
        sPost("station_logs", { action: "set", key, value: t }, `${v.label}: ${t || "none"}`), "mono");
      i.disabled = !admin;
      i.title = "Unit names, comma-separated (sizeup-station, sizeup-whisper …)";
      const row = el("div", "row");
      row.append(el("span", "s-eta-l", v.label), i, sState(v.edited ? "edited" : "default"));
      card.append(row);
    }
    return card;
  }

  function bar() {
    const units = status ? [...status.units, ...status.hub_units] : [];
    const src = sSelect([["all", "All"], ["station", "Station"], ["hub", "Hub"]], q.source, (v) => { q.source = v; }, "Source");
    const unit = sSelect([["", "Any unit"], ...units.map((u) => [u, u])], q.unit, (v) => { q.unit = v; }, "Unit");
    const lv = sSelect([["", "Any level"], ["info", "info+"], ["warning", "warning+"], ["error", "error"]], q.level,
      (v) => { q.level = v; }, "Level");
    const text = sInput(q.text, 120, "Text", null, "lg-text");
    text.addEventListener("input", () => { q.text = text.value; });
    text.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
    const last = sSelect(LAST, q.last, (v) => { q.last = v; q.around = null; render(); }, "Time");
    const row = el("div", "row lg-bar");
    row.append(src, unit, lv, text);
    if (q.around) {
      const chip = el("span", "chip", `${day(q.around)} ${fmtTime(q.around, true)} ±5 min`);
      row.append(chip, sBtn("×", "Back to the last hour", () => { q.around = null; render(); }, "quiet"));
    } else row.append(last);
    const go = sBtn(busy ? "Running…" : "Run", "Logs Insights: newest first, at most 200 rows", run, "primary ro-ok");
    go.disabled = busy || !(status && status.configured);
    for (const c of [src, unit, lv, text, last]) c.classList.add("ro-ok");
    row.append(go);
    return row;
  }

  function table() {
    if (!rows) return el("p", "empty", status && status.configured === false ? "Not configured" : "Run a query");
    if (rows.error) return el("div", "error", rows.error);
    const t = el("table", "evtable lg-table");
    const head = el("tr");
    for (const h of ["Time", "Source", "Unit", "Level", "Message", ""]) head.append(el("th", "", h));
    t.append(el("thead"), el("tbody"));
    t.tHead.append(head);
    for (const r of rows.rows) {
      const tr = el("tr", `lg-${r.level}`);
      const tt = el("td", "mono nowrap", r.t ? fmtTime(r.t, true) : "—");
      if (r.t) tt.title = new Date(r.t * 1000).toString();
      const jump = sBtn("±5", "This time ±5 min, every source and unit", () => {
        Object.assign(q, { around: r.t, source: "all", unit: "", level: "", text: "" });
        run();
      }, "quiet ro-ok");
      jump.disabled = !r.t;
      tr.append(tt, el("td", "dim", r.source), el("td", "mono", r.unit), el("td", "", r.level),
        el("td", "mono lg-msg", r.msg));
      const td = el("td");
      td.append(jump);
      tr.append(td);
      t.tBodies[0].append(tr);
    }
    if (!rows.rows.length) {
      const tr = el("tr");
      tr.append(Object.assign(el("td", "empty", "No lines"), { colSpan: 6 }));
      t.tBodies[0].append(tr);
    }
    const wrap = el("div", "tscroll");
    wrap.append(t);
    const foot = el("div", "dim small", `${rows.rows.length}${rows.truncated ? "+" : ""} lines · scanned ${kb(rows.scanned_bytes)}`
      + ` · ${when(rows.start)}–${when(rows.end)}`);
    foot.title = "Logs Insights bills $0.005 per GB scanned";
    const box = el("div");
    box.append(wrap, foot);
    return box;
  }

  function render() {
    if (!node) return;
    node.replaceChildren(...sHead("Logs", "The station's logs (scrubbed) and the hub's, in CloudWatch in our own AWS "
      + "account. A query runs only when asked: at most 7 days and 200 rows. ±5: everything around that line."),
    line(), sCard(null, bar(), table()), settingsCard());
  }

  async function run() {
    if (busy) return;
    const now = Date.now() / 1000;
    const p = new URLSearchParams({ source: q.source, limit: "200" });
    if (q.unit) p.set("unit", q.unit);
    if (q.level) p.set("level", q.level);
    if (q.text.trim()) p.set("q", q.text.trim());
    if (q.around) { p.set("start", String(Math.floor(q.around - 300))); p.set("end", String(Math.ceil(q.around + 300))); }
    else { p.set("start", String(Math.floor(now - Number(q.last)))); p.set("end", String(Math.ceil(now))); }
    busy = true;
    render();
    try { rows = await get(`/api/cad/logs?${p}`); } catch (e) { rows = { error: e.message }; }
    busy = false;
    render();
  }

  async function mount(n) {
    node = n;
    render();
    try { status = await get("/api/cad/logs/status"); said = ""; } catch (e) { status = null; said = e.message; }
    render();
  }

  return { mount, render };
})();
window.StationLogsView = StationLogsView;
