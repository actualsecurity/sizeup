// Station health (GitHub #79, sizeup/cloud/health.py): on the hub, the alert banner on /cad and /monitor
// (the CAD state's cloud.alerts, stacked, worst first, each with its start time and how long) and the
// Settings > System > Station health card (GET /api/cad/health: the station's health, the alerts, 24 h of
// their history). Self-contained: cad.js runs its hooks every state push; settings.js mounts the card.
const StationHealth = (() => {
  const POLL_MS = 5000;
  const PANEL_HASH = "#settings/system/health";
  let banner = null, card = null, timer = null, last = null, scrolled = "";

  const when = (t) => (t ? fmtTime(t, true) : "—");
  const longDur = (s) => {
    if (s === null || s === undefined || !isFinite(s)) return "—";
    s = Math.max(0, Math.round(s));
    if (s < 90) return `${s} s`;
    if (s < 5400) return `${Math.round(s / 60)} min`;
    if (s < 172800) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
    return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
  };
  const short = (b) => (b ? String(b).slice(0, 7) : "?");
  const gb = (n) => (n ? `${(n / 1e9).toFixed(1)} GB` : "—");
  const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v)}%`);
  const panelHref = () => (BOARD ? `/cad${PANEL_HASH}` : PANEL_HASH);

  // ---- the banner ---------------------------------------------------------------------------------

  function bannerNode() {
    if (banner) return banner;
    banner = document.getElementById("health-banner");      // cad.html's, first in #alert-banners
    if (banner) return banner;
    banner = el("div", "health-banner");
    banner.id = "health-banner";
    banner.setAttribute("role", "alert");
    banner.hidden = true;
    const box = document.getElementById("alert-banners"), head = document.querySelector("body > header");
    if (box) box.prepend(banner); else if (head) head.after(banner); else document.body.prepend(banner);
    return banner;
  }

  function renderBanner() {
    const c = cad.state && cad.state.cloud;
    const alerts = (c && c.role === "hub" && c.alerts) || [];
    const box = bannerNode();
    show(box, alerts.length > 0);
    if (!alerts.length) { box.dataset.sig = ""; return; }
    const sig = JSON.stringify(alerts.map((a) => [a.key, a.severity, a.title, a.text, a.since]));
    if (box.dataset.sig !== sig) {
      box.dataset.sig = sig;
      box.replaceChildren(...alerts.map((a) => {
        const row = el("div", `hb-row ${a.severity === "critical" ? "critical" : "warning"}`);
        row.dataset.key = a.key;
        const link = el("a", "hb-link", "Station health");
        link.href = panelHref();
        if (BOARD) { link.target = "sizeup-cad"; link.rel = "noopener"; }
        row.append(el("b", "hb-title", a.title), el("span", "hb-text", a.text),
          el("span", "hb-when dim", ""), link);
        row.title = `${a.title}: ${a.text}`;
        return row;
      }));
    }
    for (const row of box.children) {        // the durations tick with every state push
      const a = alerts.find((x) => x.key === row.dataset.key);
      if (a) setText(row.querySelector(".hb-when"), `since ${when(a.since)} (${longDur(serverNow() - a.since)})`);
    }
  }

  // ---- the panel ---------------------------------------------------------------------------------------

  function dl(pairs) {
    const d = el("dl", "admin");
    for (const [k, v, cls] of pairs) {
      const w = el("div");
      w.append(el("dt", "", k), el("dd", cls || "", v === null || v === undefined || v === "" ? "—" : String(v)));
      d.append(w);
    }
    return d;
  }

  function table(head, rows, empty) {
    const t = el("table", "evtable hl-table");
    t.append(el("thead"), el("tbody"));
    const tr = el("tr");
    for (const h of head) tr.append(el("th", "", h));
    t.tHead.append(tr);
    for (const r of rows) {
      const row = el("tr");
      for (const c of r) {
        if (c instanceof Node) { const td = el("td"); td.append(c); row.append(td); }
        else if (Array.isArray(c)) row.append(el("td", c[1] || "", c[0] ?? "—"));
        else row.append(el("td", "", c === null || c === undefined || c === "" ? "—" : String(c)));
      }
      t.tBodies[0].append(row);
    }
    if (!rows.length) {
      const row = el("tr");
      row.append(Object.assign(el("td", "empty", empty || "None"), { colSpan: head.length }));
      t.tBodies[0].append(row);
    }
    const wrap = el("div", "tscroll");
    wrap.append(t);
    return wrap;
  }

  const stateCls = (s) => (s === "ok" ? "hl-ok" : s === "off" || s === null || s === undefined ? "dim" : "hl-bad");
  const whoText = (w) => (w ? [w.channel, w.dongle, w.detail].filter(Boolean).join(" · ") : "");

  function alertRows(alerts) {
    return alerts.map((a) => {
      const r = el("div", `hb-row ${a.severity === "critical" ? "critical" : "warning"}`);
      r.append(el("b", "hb-title", a.title), el("span", "hb-text", a.text),
        el("span", "hb-when dim", `since ${when(a.since)} (${longDur(serverNow() - a.since)})`));
      return r;
    });
  }

  function render(d) {
    if (!card) return;
    const h = d.health || {}, st = h.station || {}, b = h.build || {}, ob = h.outbox || {}, sys = h.system || {};
    const pr = h.processes || {}, n = d.notify || {}, th = d.thresholds || {};
    const last = b.last || null;
    const push = n.on ? `Push on (${(n.server || "").replace(/^https?:\/\//, "")})` + (n.failed ? `, ${n.failed} failed: ${n.last_error}` : "")
      : "Push off: no status topic in effect (Settings > System > Push notifications, SSM /sizeup-hub/status-topic or [alerts])";
    const head = el("div", "card-head");
    head.append(el("h2", "", "Station health"),
      el("span", `chip ${d.alerts.length ? "hl-bad" : "hl-ok"}`, d.alerts.length ? `${d.alerts.length} alert${d.alerts.length === 1 ? "" : "s"}` : "No alerts"),
      el("span", "spacer"), el("span", "dim small", push));
    const kids = [head];
    if (!h.reported) kids.push(el("p", "notice", "No station has reported since the hub started."));
    else if (!h.health_reported) kids.push(el("p", "notice", "The station runs an older sizeup: CPU, memory, disk and its programs show once it updates."));
    kids.push(el("h3", "hl-sub", "Alerts now"),
      ...(d.alerts.length ? [el("div", "hl-alerts")] : [el("p", "dim small", "None")]));
    if (d.alerts.length) kids[kids.length - 1].append(...alertRows(d.alerts));
    if ((h.hints || []).length) {
      const box = el("div", "hl-hints");
      for (const x of h.hints) box.append(el("p", "notice info", `${x.channel}: ${x.text}`));
      kids.push(el("h3", "hl-sub", "Hints"), box);
    }
    kids.push(el("h3", "hl-sub", "Station"), dl([
      ["Station", st.name || st.id],
      ["Connection", st.connected ? "connected" : "offline", st.connected ? "hl-ok" : "hl-bad"],
      ["Last state post", st.last_state ? `${when(st.last_state)} (${longDur(serverNow() - st.last_state)} ago)` : "never"],
      ["Clock difference", st.skew_s !== undefined ? `${st.skew_s} s` : ""],
      ["sizeup up since", sys.started ? `${when(sys.started)} (${longDur(sys.uptime_s)})` : ""],
      ["Machine up since", sys.boot ? `${when(sys.boot)} (${longDur(serverNow() - sys.boot)})` : ""],
      ["Host", sys.host],
    ]));
    kids.push(el("h3", "hl-sub", "Version and updates"), dl([
      ["Station build", short(b.station)], ["Hub build", short(b.hub)],
      ["State", b.text, stateCls(b.state === "ok" ? "ok" : b.state === "unknown" ? null : "bad")],
      ["Behind since", b.behind_since ? when(b.behind_since) : ""],
      ["Last update", last ? `${when(last.at)} ${short(last.from)} → ${short(last.to)}: ${last.result || "?"}${last.why ? ` (${last.why})` : ""}` : ""],
      ["Timings", last && last.seconds !== undefined && last.seconds !== null ? [`${last.seconds} s in all`,
        last.staged_s !== undefined && last.staged_s !== null ? `built ${last.staged_s} s` : "",
        last.waited_s !== undefined && last.waited_s !== null ? `waited ${last.waited_s} s` : "",
        last.up_s !== undefined && last.up_s !== null ? `web back ${last.up_s} s` : "",
        last.radios_s !== undefined && last.radios_s !== null ? `radios ${last.radios_s} s` : ""].filter(Boolean).join(", ") : ""],
      ["Update log", last && last.line ? last.line : ""],
      ["Failed builds", Object.entries(b.failed || {}).map(([k, v]) => `${short(k)}: ${(v && v.why) || "?"}`).join("; ")],
    ]));
    kids.push(el("h3", "hl-sub", "Outbox and system"), dl([
      ["Events waiting", ob.pending || 0, ob.age_s >= (th.outbox_min || 5) * 60 ? "hl-bad" : ""],
      ["Oldest waiting", ob.oldest ? `${when(ob.oldest)} (${longDur(ob.age_s)})` : ""],
      ["Clips dropped", ob.dropped_audio || 0],
      ["CPU", sys.cpu_pct !== null && sys.cpu_pct !== undefined ? `${sys.cpu_pct}%` : (sys.load1 !== null && sys.load1 !== undefined ? `load ${sys.load1}` : "")],
      ["Load (1 min)", sys.load1 !== null && sys.load1 !== undefined ? `${sys.load1}${sys.cpus ? ` on ${sys.cpus} CPUs` : ""}` : ""],
      ["Memory", pct(sys.mem_pct)],
      ["Disk", sys.disk_pct !== null && sys.disk_pct !== undefined ? `${pct(sys.disk_pct)} full, ${gb(sys.disk_free)} free` : "",
        sys.disk_pct >= (th.disk_pct || 90) ? "hl-bad" : ""],
      ["Temperature", sys.temp_c !== null && sys.temp_c !== undefined ? `${sys.temp_c} °C` : ""],
    ]));
    kids.push(el("h3", "hl-sub", "Programs"), table(["Program", "State", "Restarts", "Down since", "Detail"],
      [["whisper", pr.whisper], ["trunk-recorder", pr.recorder]].filter(([, p]) => p).map(([k, p]) => [
        k, [p.state || "?", stateCls(p.state)], p.restarts ?? "", p.down_since ? when(p.down_since) : "", p.detail || ""]),
      "Not reported"));
    const sq = h.stt_queue;
    if (sq) {             // the station's whisper queue (#195): depth, the oldest live clip's wait, what was dropped
      kids.push(el("h3", "hl-sub", "Speech-to-text queue (station)"), dl([
        ["Waiting", `${sq.queued} of ${sq.max} (${sq.urgent} pages / Fire/EMS 15 / firegrounds, ${sq.live - sq.urgent} other live, ${sq.rerun} re-runs)`],
        ["Oldest live clip", sq.oldest_s !== null && sq.oldest_s !== undefined ? `waiting ${longDur(sq.oldest_s)}` : "none",
          (sq.oldest_s || 0) >= 600 ? "hl-bad" : ""],
        ["Dropped / skipped", `${sq.dropped || 0} / ${sq.skipped || 0}` + (sq.last_drop ? ` (last: ${sq.last_drop.why})` : ""),
          sq.dropped || sq.skipped ? "hl-warn" : ""],
      ]));
    }
    const tr = h.transcribe;
    if (tr) {             // Amazon Transcribe on the hub (#195): its health, AWS's reason, the breaker
      const br = tr.breaker || {};
      kids.push(el("h3", "hl-sub", "Amazon Transcribe (hub)"), dl([
        ["State", tr.text || tr.state, stateCls(tr.state === "ok" ? "ok" : "bad")],
        ["Last answer", tr.last_ok ? `${when(tr.last_ok)} (${longDur(serverNow() - tr.last_ok)} ago)` : "none since the hub started"],
        ["Failing since", tr.failing_since ? `${when(tr.failing_since)}: ${tr.failures} failure(s), ${tr.reason_text || tr.reason}` : ""],
        ["Breaker", br.state === "open" ? `open since ${when(br.since)}: new clips go to the station's whisper; AWS tried again ${when(br.retry_at)}`
          : br.state === "half-open" ? "half-open: trying AWS with the next clips" : "closed", br.state && br.state !== "closed" ? "hl-bad" : ""],
        ["Waiting for AWS", tr.open_jobs ? `${tr.open_jobs} (the oldest ${longDur(tr.oldest_s)})` : "none"],
        ["Counts", `${tr.done} answered, ${tr.timeouts} timed out, ${tr.late} late, ${tr.failed} failed over`],
        ["This month", tr.budget && tr.budget.limit_usd !== undefined ? `$${(tr.budget.usd || 0).toFixed(2)} of the $${tr.budget.limit_usd} limit (${tr.budget.pct}%)`
          + (tr.budget.reserve_text ? `; ${tr.budget.reserve_text}` : "") : "", tr.budget && tr.budget.pct >= 80 ? "hl-bad" : ""],
      ]));
      if ((tr.recent || []).length) kids.push(table(["When", "Channel", "Reason", "AWS said"],
        tr.recent.map((r) => [when(r.at), r.channel, r.reason, r.why]), ""));
    }
    kids.push(el("h3", "hl-sub", "Dongles"), table(["SDR", "Serial", "Plugged in", "Job", "Running", "Restarts", "Detail"],
      (h.dongles || []).map((x) => [x.name, x.serial, [x.present ? "yes" : "no", x.present ? "" : "hl-bad"], x.job,
        [x.job === "Off" ? "—" : x.running ? "yes" : (x.state || "no"), x.job === "Off" ? "dim" : x.running ? "hl-ok" : "hl-bad"],
        x.restarts || 0, x.held_by ? `held by ${x.held_by.app} (${x.held_by.mode})` : x.detail]),
      h.shared ? "No dongles" : "One dongle per channel (see Channels)"));
    kids.push(el("h3", "hl-sub", "Channels"), table(["Channel", "MHz", "State", "Last heard", "Quiet for", "Usually within", "Squelch", "Quieting", "Restarts", "Hint"],
      (h.channels || []).map((c) => [c.label, c.mhz || "", [c.word || c.state || "?", stateCls(c.state)],
        c.last_heard ? when(c.last_heard) : "never", c.quiet_s !== null && c.quiet_s !== undefined ? longDur(c.quiet_s) : "",
        c.typical_gap_s ? longDur(c.typical_gap_s) : (c.gaps ? `learning (${c.gaps})` : ""),
        [c.stuck ? `stuck open ${fmtDur(c.open_s)}` : c.open ? `open ${fmtDur(c.open_s)}` : "closed", c.stuck ? "hl-bad" : ""],
        c.quieting_db !== null && c.quieting_db !== undefined ? `${c.quieting_db} dB` : "", c.restarts || 0, [c.hint || "", c.hint ? "hl-warn" : ""]]),
      "No channels reported"));
    kids.push(el("h3", "hl-sub", "Alert history (24 h)"), table(["Started", "Cleared", "Lasted", "Severity", "Alert", "Where", "Pushed"],
      [...d.alerts.map((a) => [when(a.since), "active", longDur(serverNow() - a.since), [a.severity, a.severity === "critical" ? "hl-bad" : "hl-warn"],
        `${a.title}: ${a.text}`, whoText(a.who), ""]),
      ...(d.history || []).map((a) => [when(a.since), when(a.cleared), longDur((a.cleared || 0) - (a.since || 0)),
        [a.severity, a.severity === "critical" ? "hl-bad" : "hl-warn"], `${a.title}: ${a.text}`, whoText(a.who), a.pushed ? "yes" : "no"])],
      "No alerts in the last 24 h"));
    kids.push(el("p", "dim small", `Alerts: silent ${th.silent_min} min · behind ${th.behind_min} min · whisper / trunk-recorder down ${th.down_min} min · `
      + `a dongle's job down ${Math.round(th.job_grace_s || 60)} s · outbox ${th.outbox_min} min · disk ${th.disk_pct}% · at most one push per alert per ${th.repeat_min} min, plus its recovery.`));
    card.replaceChildren(...kids);
    card.hidden = false;
    if (location.hash.startsWith(PANEL_HASH) && scrolled !== location.hash) {
      scrolled = location.hash;
      card.scrollIntoView({ block: "start" });
    }
  }

  async function poll() {
    clearTimeout(timer);
    if (!card || !card.isConnected || card.closest("[hidden]:not(#s-health)")) { timer = null; return; }
    try {
      const r = await fetch("/api/cad/health", { headers: { Accept: "application/json" } });
      if (r.status === 404 || r.status === 409) { card.hidden = true; timer = null; return; }   // not the hub
      if (r.ok) { last = await r.json(); render(last); }
    } catch (_) { /* the next poll tries again */ }
    timer = setTimeout(poll, POLL_MS);
  }

  // Settings > System: the card (settings.js sSystem); polls while it is on screen.
  function mount(node) {
    card = node;
    card.id = "s-health";
    card.classList.add("hl-card");
    if (last) render(last);
    poll();
  }

  if (typeof cad !== "undefined" && cad.hooks) cad.hooks.push(renderBanner);
  window.addEventListener("hashchange", () => { if (!location.hash.startsWith(PANEL_HASH)) scrolled = ""; });
  return { mount, renderBanner, render };
})();
window.StationHealth = StationHealth;
