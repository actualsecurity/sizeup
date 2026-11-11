// Station health (GitHub #79, sizeup/cloud/health.py) on the hub: Settings > Health, an admins' tab (#203: no banner
// under the header for anyone, nothing of it in the state push). GET /api/cad/health (admins only: sizeup/cad/api.py
// ADMIN_READS) has the station's health, the alerts now and 24 h of their history; polled every POLL_MS while the tab is
// on screen, and not at all otherwise. Self-contained: settings.js mounts it on the section (sHealth).
const StationHealth = (() => {
  const POLL_MS = 5000;
  let panel = null, card = null, timer = null, last = null, busy = false;

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
  const isOpen = () => !!panel && panel.isConnected && !panel.closest("[hidden]") && !document.hidden;

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

  const sevChip = (s) => el("span", `chip ${s === "critical" ? "hl-bad" : "hl-warn"}`, s === "critical" ? "Critical" : "Warning");

  // The alerts now: severity, what, since when (and how long), where, and what happens at recovery.
  function alertRows(alerts, n, th = {}) {
    return alerts.map((a) => {
      const r = el("div", `hb-row ${a.severity === "critical" ? "critical" : "warning"}`);
      const where = whoText(a.who);
      const rec = a.muted ? (a.muted_until ? `Muted until ${when(a.muted_until)}` : "Muted")
        : !n.on ? "Not pushed (push off)" : a.pushed ? "Pushed" : "Not pushed yet";
      const flap = th.flap_n && a.flaps >= th.flap_n ? `flapping, back ${a.flaps}× in 1 h` : "";
      const det = el("span", "hb-detail dim small", [where, rec, flap].filter(Boolean).join(" · "));
      det.title = a.muted ? "Muted: shown here, not pushed; \"Recovered\" only if pushed before the mute (Settings > System > Push notifications)"
        : n.on ? `Pushed to the status topic; "Recovered" once clear ${th.settle_min || 15} min` : "Push off: it clears here and in the log only";
      r.append(sevChip(a.severity), el("b", "hb-title", a.title), el("span", "hb-text", a.text),
        el("span", "hb-when dim", `since ${when(a.since)} · ${longDur(serverNow() - a.since)}`), det);
      return r;
    });
  }

  function render(d) {
    if (!card) return;
    const h = d.health || {}, st = h.station || {}, b = h.build || {}, ob = h.outbox || {}, sys = h.system || {};
    const pr = h.processes || {}, n = d.notify || {}, th = d.thresholds || {};
    const last = b.last || null;
    const push = n.on ? `Push on · ${(n.server || "").replace(/^https?:\/\//, "")}` + (n.failed ? ` · ${n.failed} failed: ${n.last_error}` : "")
      : "Push off";
    const pushEl = el("span", "dim small", push);
    const muted = n.muted || [];                // alert kinds muted (#340): not pushed until unmuted or the time is up
    const mutedEl = muted.length ? el("span", "chip hl-warn", `Muted: ${muted.map((x) => x.label + (x.until ? ` until ${when(x.until)}` : "")).join(", ")}`) : null;
    if (mutedEl) mutedEl.title = "Shown here, not pushed (Settings > System > Push notifications)";
    // #278: why it is off and where to set it, behind an "i" (a tap on a tablet; a title alone never shows there)
    if (!n.on && window.Help && Help.info) pushEl.append(" ", Help.info("No status topic in effect. Set one in Settings > System > Push notifications, SSM /sizeup-hub/status-topic or [alerts]"));
    const head = el("div", "card-head");
    head.append(el("h2", "", "Now"),
      el("span", `chip ${d.alerts.length ? "hl-bad" : "hl-ok"}`, d.alerts.length ? `${d.alerts.length} alert${d.alerts.length === 1 ? "" : "s"}` : "No alerts"),
      el("span", "spacer"), ...(mutedEl ? [mutedEl] : []), pushEl);
    const kids = [head];
    if (!h.reported) kids.push(el("p", "notice", "No station report yet"));
    else if (!h.health_reported) {
      const old = el("p", "notice", "Station on an older sizeup: no CPU, memory, disk or programs");
      old.title = "They show once the station updates";
      kids.push(old);
    }
    kids.push(el("h3", "hl-sub", "Alerts now"),
      ...(d.alerts.length ? [el("div", "hl-alerts")] : [el("p", "dim small", "None")]));
    if (d.alerts.length) kids[kids.length - 1].append(...alertRows(d.alerts, n, th));
    if ((h.hints || []).length) {
      const box = el("div", "hl-hints");
      for (const x of h.hints) box.append(el("p", "notice info", `${x.channel}: ${x.text}`));
      kids.push(el("h3", "hl-sub", "Hints"), box);
    }
    const so = d.sources;
    if (so && so.tiers) {   // incident sources (#297, sizeup/cad/sources.py): tiers, the acting primary, the lead over paging
      const lead = (v) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${longDur(Math.abs(v))}`);
      kids.push(el("h3", "hl-sub", "Incident sources"), table(["Tier", "Source", "State", "Reason", "Last report", "Primary"],
        so.tiers.map((t) => [t.rank >= 1000 ? "backup" : t.rank, t.label, [t.state.replace(/_/g, " "), stateCls(t.state)], t.reason,
          t.source === "paging" ? "" : `${when(t.at)} (${longDur(serverNow() - t.at)} ago)`, [t.acting ? "acting" : "", t.acting ? "hl-ok" : ""]]),
        "None"));
      if ((so.lead || []).length) kids.push(table(["Source", "Lead over paging (median)", "Range", "Incidents"],
        so.lead.map((x) => [x.label, lead(x.median_s), `${lead(x.min_s)} … ${lead(x.max_s)}`, x.n]), ""));
      if ((so.incidents || []).length) kids.push(table(["Event", "Location", "First seen", "Lead over paging"],
        so.incidents.slice(0, 20).map((i) => [i.number || `#${i.id}`, i.address,
          i.seen.map((x) => `${x.label} ${when(x.first_seen)}` + (x.gone_at ? " · no longer listed" : "")).join(" · "),
          i.seen.filter((x) => x.lead_s !== null && x.lead_s !== undefined).map((x) => `${x.label} ${lead(x.lead_s)}`).join(" · ")]), ""));
    }
    if ((d.feeds || []).length) {   // map feeds with a fallback (#362: MTA TrainTime, else GTFS-realtime)
      const feedCls = (s) => (s === "ok" ? "hl-ok" : s === "fallback" || s === "error" ? "hl-bad" : "dim");
      kids.push(el("h3", "hl-sub", "Map feeds"), table(["Feed", "State", "Reason", "Last read", "Fallback"],
        d.feeds.map((f) => [f.label, [f.state, feedCls(f.state)], f.why || "",
          f.at ? `${when(f.at)} (${longDur(serverNow() - f.at)} ago)` : "never", f.fallback || ""]), ""));
    }
    kids.push(el("h3", "hl-sub", "Station"), dl([
      ["Station", st.name || st.id],
      ["Connection", st.connected ? "Connected" : st.last_state ? `Offline · ${longDur(serverNow() - st.last_state)}` : "Offline",
        st.connected ? "hl-ok" : "hl-bad"],
      ["Last state post", st.last_state ? `${when(st.last_state)} · ${longDur(serverNow() - st.last_state)} ago` : "never"],
      ["Clock skew", st.skew_s !== undefined ? `${st.skew_s} s` : ""],
      ["sizeup up", sys.started ? `${longDur(sys.uptime_s)} · since ${when(sys.started)}` : ""],
      ["Machine up", sys.boot ? `${longDur(serverNow() - sys.boot)} · since ${when(sys.boot)}` : ""],
      ["Host", sys.host],
    ]));
    kids.push(el("h3", "hl-sub", "Version"), dl([
      ["Station build", short(b.station)], ["Hub build", short(b.hub)],
      ["State", b.text, stateCls(b.state === "ok" ? "ok" : b.state === "unknown" ? null : "bad")],
      ["Behind since", b.behind_since ? when(b.behind_since) : ""],
      ["Last update", last ? `${when(last.at)} ${short(last.from)} → ${short(last.to)}: ${last.result || "?"}${last.why ? ` (${last.why})` : ""}` : ""],
      ["Timings", last && last.seconds !== undefined && last.seconds !== null ? [`${last.seconds} s total`,
        last.staged_s !== undefined && last.staged_s !== null ? `built ${last.staged_s} s` : "",
        last.waited_s !== undefined && last.waited_s !== null ? `waited ${last.waited_s} s` : "",
        last.up_s !== undefined && last.up_s !== null ? `web back ${last.up_s} s` : "",
        last.radios_s !== undefined && last.radios_s !== null ? `radios ${last.radios_s} s` : ""].filter(Boolean).join(" · ") : ""],
      ["Update log", last && last.line ? last.line : ""],
      ["Failed builds", Object.entries(b.failed || {}).map(([k, v]) => `${short(k)}: ${(v && v.why) || "?"}`).join("; ")],
      // the station's incident-source adapter (#301): what runs, and each source's own health
      ...(b.adapter ? [["Adapter", b.adapter.text, stateCls(["running", "installed"].includes(b.adapter.state) ? "ok" : "bad")]] : []),
    ]));
    const failing = ((b.adapter && b.adapter.health) || []).filter((x) => x.state === "failing");
    if (failing.length) kids.push(resetRow(failing));
    kids.push(el("h3", "hl-sub", "Outbox and system"), dl([
      ["Events waiting", ob.pending || 0, ob.age_s >= (th.outbox_min || 5) * 60 ? "hl-bad" : ""],
      ["Oldest waiting", ob.oldest ? `${when(ob.oldest)} · ${longDur(ob.age_s)}` : ""],
      ["Clips dropped", ob.dropped_audio || 0],
      ["CPU", sys.cpu_pct !== null && sys.cpu_pct !== undefined ? `${sys.cpu_pct}%` : (sys.load1 !== null && sys.load1 !== undefined ? `load ${sys.load1}` : "")],
      ["Load (1 min)", sys.load1 !== null && sys.load1 !== undefined ? `${sys.load1}${sys.cpus ? ` / ${sys.cpus} CPUs` : ""}` : ""],
      ["Memory", pct(sys.mem_pct)],
      ["Disk", sys.disk_pct !== null && sys.disk_pct !== undefined ? `${pct(sys.disk_pct)} · ${gb(sys.disk_free)} free` : "",
        sys.disk_pct >= (th.disk_pct || 90) ? "hl-bad" : ""],
      ["Temperature", sys.temp_c !== null && sys.temp_c !== undefined ? `${sys.temp_c} °C` : ""],
    ]));
    kids.push(el("h3", "hl-sub", "Programs"), table(["Program", "State", "Restarts", "Down since", "Detail"],
      [["whisper", pr.whisper], ["trunk-recorder", pr.recorder]].filter(([, p]) => p).map(([k, p]) => [
        k, [p.state || "?", stateCls(p.state)], p.restarts ?? "", p.down_since ? when(p.down_since) : "", p.detail || ""]),
      "Not reported"));
    const ad = h.adsb;
    if (ad) {             // our ADS-B receiver on the station (#235): its decoder, and its aircraft reaching the hub
      const fw = ad.forward || {}, hb = ad.hub || {};
      kids.push(el("h3", "hl-sub", "ADS-B receiver (station)"), dl([
        ["Dongle", `${ad.name || ad.sdr}${ad.name && ad.name !== ad.sdr ? ` (serial ${ad.sdr})` : ""}`
          + (ad.present === false ? " · unplugged" : ""), ad.present === false ? "hl-bad" : ""],
        ["Decoder", `${ad.state || "?"}${ad.detail ? `: ${ad.detail}` : ""}`, stateCls(ad.state === "ok" ? "ok" : ad.state === "off" ? "off" : "bad")],
        ["Cause", ad.cause && ad.state !== "ok" ? ad.cause.replace("_", " ") + (ad.recovery ? ` (${ad.recovery}, retrying)` : "") : "",
          ad.cause && ad.state !== "ok" ? "hl-bad" : ""],
        ["Held by", ad.holder || (ad.held_by ? `${ad.held_by.app} (${ad.held_by.mode})` : "")],
        ["readsb said", ad.state !== "ok" && Array.isArray(ad.tail) ? ad.tail.join(" | ") : ""],
        ["Messages", ad.messages_per_s !== null && ad.messages_per_s !== undefined ? `${ad.messages_per_s} msg/s` : ""],
        ["Aircraft", ad.aircraft !== null && ad.aircraft !== undefined ? `${ad.aircraft} · ${ad.with_pos ?? 0} with position` : ""],
        ["Last message", ad.last_message_at ? `${when(ad.last_message_at)} · ${longDur(serverNow() - ad.last_message_at)} ago` : "none yet"],
        ["Gain, bias-tee", `${ad.gain || "auto"} · bias-tee ${ad.bias_tee ? "ON" : "off"}`],
        ["USB memory", ad.usbfs_mb === null || ad.usbfs_mb === undefined ? "" : (ad.usbfs_mb === 0 ? "usbfs_memory_mb 0 (no limit)"
          : `usbfs_memory_mb ${ad.usbfs_mb} MB (shared by all dongles; want 0)`) + (ad.buffer_kib ? ` · readsb ${ad.buffer_kib} KiB transfers` : ""),
          ad.usbfs_mb ? "hl-bad" : ""],
        ["To the hub", fw.posts !== undefined ? `${fw.ok ? "OK" : "Not reaching it"}${fw.error ? ` (${fw.error})` : ""} · every ${fw.every_s} s · `
          + `${fw.posts} posts · ${((fw.bytes || 0) / 1e6).toFixed(1)} MB` : "", fw.ok === false ? "hl-bad" : ""],
        ["On the hub", hb.last ? `${hb.aircraft} aircraft · ${longDur(hb.age_s)} ago · ${hb.bytes_per_s} B/s` : "none yet"],
      ]));
    }
    const sq = h.stt_queue;
    if (sq) {             // the station's whisper queue (#195): depth, the oldest live clip's wait, what was dropped
      kids.push(el("h3", "hl-sub", "Speech-to-text queue (station)"), dl([
        ["Waiting", `${sq.queued}/${sq.max} · ${sq.urgent} urgent · ${sq.live - sq.urgent - (sq.high || 0)} other live`
          + `${sq.high ? ` · ${sq.high} airband` : ""} · ${sq.rerun} re-runs${sq.low !== undefined ? ` · ${sq.low} context talkgroups` : ""}`],
        ["Oldest live clip", sq.oldest_s !== null && sq.oldest_s !== undefined ? longDur(sq.oldest_s) : "none",
          (sq.oldest_s || 0) >= 600 ? "hl-bad" : ""],
        ["Pace", sq.service_s ? `${sq.service_s} s/clip (${Math.round(3600 / sq.service_s)}/h) · ${sq.in_h || 0}/h in`
          + (sq.in_fallback_h ? ` · ${sq.in_fallback_h}/h AWS fallback` : "") : "", sq.service_s && sq.in_h > 3600 / sq.service_s ? "hl-warn" : ""],
        ["Dropped / skipped / shed", `${sq.dropped || 0} / ${sq.skipped || 0} / ${sq.shed || 0}` + (sq.last_drop ? ` · last: ${sq.last_drop.why}` : ""),
          sq.dropped || sq.skipped ? "hl-warn" : ""],
      ]));
    }
    const tr = h.transcribe;
    if (tr) {             // Amazon Transcribe on the hub (#195): its health, AWS's reason, the breaker
      const br = tr.breaker || {};
      kids.push(el("h3", "hl-sub", "Amazon Transcribe (hub)"), dl([
        ["State", tr.text || tr.state, stateCls(tr.state === "ok" ? "ok" : "bad")],
        ["Last answer", tr.last_ok ? `${when(tr.last_ok)} · ${longDur(serverNow() - tr.last_ok)} ago` : "none yet"],
        ["Failing since", tr.failing_since ? `${when(tr.failing_since)} · ${tr.failures} failure(s) · ${tr.reason_text || tr.reason}` : ""],
        // open: new clips go to the station's whisper until the retry; half-open: AWS tried with the next clips
        ["Breaker", br.state === "open" ? `Open · whisper until ${when(br.retry_at)} · since ${when(br.since)}`
          : br.state === "half-open" ? "Half-open · trying AWS" : "Closed", br.state && br.state !== "closed" ? "hl-bad" : ""],
        ["Waiting for AWS", tr.open_jobs ? `${tr.open_jobs} · oldest ${longDur(tr.oldest_s)}` : "none"],
        ["Counts", `${tr.done} answered · ${tr.timeouts} timed out · ${tr.late} late · ${tr.failed} failed over`],
        ["This month", tr.budget && tr.budget.limit_usd !== undefined ? `$${(tr.budget.usd || 0).toFixed(2)} / $${tr.budget.limit_usd} (${tr.budget.pct}%)`
          + (tr.budget.reserve_text ? ` · ${tr.budget.reserve_text}` : "") : "", tr.budget && tr.budget.pct >= 80 ? "hl-bad" : ""],
      ]));
      if ((tr.recent || []).length) kids.push(table(["When", "Channel", "Reason", "AWS said"],
        tr.recent.map((r) => [when(r.at), r.channel, r.reason, r.why]), ""));
    }
    const er = d.errors;
    if (er) {             // the hub's own error lines (#285, sizeup/errwatch.py): the rate and the top errors
      kids.push(el("h3", "hl-sub", "Hub errors"), dl([
        ["Rate", `${er.last5} in the last ${er.window_min} min (${er.per_min}/min)`,
          er.last5 > er.total_limit ? "hl-bad" : er.last5 ? "hl-warn" : "hl-ok"],
        ["Since the hub started", `${er.total} since ${when(er.since)}, ${er.signatures} kind${er.signatures === 1 ? "" : "s"}`
          + (er.dropped ? ` (${er.dropped} lines not read: too many at once)` : "")],
        ["Push", (er.on ? `on: one error over ${er.per_signature} or all over ${er.total_limit} in ${er.window_min} min`
          : "off (Settings > System > Hub error alerts)") + (er.alerting ? "; alerting now" : ""), er.alerting ? "hl-bad" : ""],
        ["Last notices", (er.pushes || []).slice(0, 3).map((p) => `${when(p.at)} ${p.title}`).join("; ")],
      ]));
      if ((er.top || []).length) kids.push(table(["Source", "Type", "Last message (digits stripped)", "Last 5 min", "This spike", "Since", "Last", "Total"],
        er.top.map((s) => [s.source, s.type, s.message, [s.last5, s.last5 > er.per_signature ? "hl-bad" : ""], s.count,
          when(s.first), when(s.last), s.total]), ""));
    }
    kids.push(el("h3", "hl-sub", "Dongles"));
    for (const x of h.duplicates || []) {        // two dongles, one serial (#242): neither is opened by it
      const dupe = el("p", "notice err", `${x.text} · fix: sudo sizeup-station sdr-serial <index> <new-serial> --dry-run, then without --dry-run`);
      dupe.title = "deploy/station/README.md \"Two dongles, one serial\"";
      kids.push(dupe);
    }
    kids.push(table(["SDR", "Serial", "Plugged in", "Job", "Running", "Restarts", "Detail"],
      (h.dongles || []).map((x) => [x.name, x.serial, [x.present ? "yes" : "no", x.present ? "" : "hl-bad"], x.job,
        [x.job === "Off" ? "—" : x.running ? "yes" : (x.state || "no"), x.job === "Off" ? "dim" : x.running ? "hl-ok" : "hl-bad"],
        x.restarts || 0, [x.ambiguous || (x.held_by ? `held by ${x.held_by.app} (${x.held_by.mode})` : x.detail), x.ambiguous ? "hl-bad" : ""]]),
      h.shared ? "No dongles" : "One per channel (see Channels)"));
    if ((h.usb || []).length) {
      const dup = new Set((h.duplicates || []).map((x) => x.serial));
      kids.push(table(["Index", "Serial", "Product"], h.usb.map((u) => [`#${u.index}`, [u.serial || "(none)", dup.has(u.serial) ? "hl-bad" : ""],
        u.product || ""]), ""));
    }
    const tk = h.trunk;
    if (tk) {             // the P25 system as a whole (#258): its silence is the alert, one talkgroup's only a hint
      const cc = tk.cc || null;
      kids.push(el("h3", "hl-sub", "P25 trunk"), dl([
        ["Last call (any talkgroup)", tk.last_call ? `${when(tk.last_call)} · ${longDur(tk.quiet_s)} ago` : "none yet",
          tk.quiet_s > tk.window_s ? "hl-bad" : ""],
        ["Alert after", `${longDur(tk.window_s)} silent`
          + (tk.usual_s ? ` · usual gap now ${longDur(tk.usual_s)}` : ` · default (learning: ${tk.gaps} gaps)`)],
        ["Control channel", !cc ? "not reported" : cc.low_since ? `Lost · ${cc.rate ?? 0} msg/s since ${when(cc.low_since)}`
          : cc.rate !== null && cc.rate !== undefined ? `Low · ${cc.rate} msg/s` : "Decoding",
          cc && cc.low_since ? "hl-bad" : cc && cc.rate !== null && cc.rate !== undefined ? "hl-warn" : ""],
        ["Retunes", cc && cc.retunes ? `${cc.retunes} · last ${when(cc.last_retune)}` : ""],
      ]));
    }
    kids.push(el("h3", "hl-sub", "Channels"), table(["Channel", "MHz", "State", "Last heard", "Quiet for", "Usually within", "Squelch", "Quieting", "Restarts", "Hint"],
      (h.channels || []).map((c) => [c.name || c.label, c.mhz || "", [c.word || c.state || "?", stateCls(c.state)],
        c.last_heard ? when(c.last_heard) : "never", c.quiet_s !== null && c.quiet_s !== undefined ? longDur(c.quiet_s) : "",
        c.usual_now_s ? longDur(c.usual_now_s) : c.typical_gap_s ? longDur(c.typical_gap_s) : (c.gaps ? `learning (${c.gaps})` : ""),
        [c.stuck ? `stuck open ${fmtDur(c.open_s)}` : c.open ? `open ${fmtDur(c.open_s)}` : "closed", c.stuck ? "hl-bad" : ""],
        c.quieting_db !== null && c.quieting_db !== undefined ? `${c.quieting_db} dB` : "", c.restarts || 0, [c.hint || "", c.hint ? "dim" : ""]]),
      "No channels reported"));
    kids.push(el("h3", "hl-sub", "Alert history (24 h)"), table(["Started", "Recovered", "Lasted", "Severity", "Alert", "Where", "Pushed"],
      [...d.alerts.map((a) => [when(a.since), ["not yet", "hl-bad"], longDur(serverNow() - a.since), [a.severity, a.severity === "critical" ? "hl-bad" : "hl-warn"],
        `${a.title}: ${a.text}`, whoText(a.who), a.pushed ? "yes" : "no"]),
      ...(d.history || []).map((a) => [when(a.since), when(a.cleared), longDur((a.cleared || 0) - (a.since || 0)),
        [a.severity, a.severity === "critical" ? "hl-bad" : "hl-warn"], `${a.title}: ${a.text}`, whoText(a.who), a.pushed ? "yes" : "no"])],
      "None in 24 h"));
    // the thresholds, one line; how the trunk's and a channel's silence are judged, in its title
    const thr = el("p", "dim small", `Alert at: silent ${th.silent_min} min · behind ${th.behind_min} min · whisper / trunk-recorder down ${th.down_min} min · `
      + `dongle job down ${Math.round(th.job_grace_s || 60)} s · outbox ${th.outbox_min} min · disk ${th.disk_pct}%`
      + (th.trunk ? ` · P25 silent ${th.trunk.day_min} min (night ${th.trunk.night_min})` : "") + ` · 1 push / ${th.repeat_min} min`);
    const rules = "Each alert is pushed at most once per interval, plus its recovery"
      + (th.settle_min ? ` once clear ${th.settle_min} min (${th.settle_flapping_min} min if back ${th.flap_n}× in 1 h; back sooner: one episode, no push).` : ".")
      + (th.trunk ? ` P25 trunk silent (critical): no call on any talkgroup for ${th.trunk.day_min} min (${th.trunk.night_min} min into the night, 00-06), `
        + `longer when ${th.trunk.factor}× the system's usual gap for that time of day is (learned from ${th.trunk.gaps_min}+ gaps), `
        + `or the control channel lost ${th.trunk.cc_lost_min} min. One channel or talkgroup silent is only a hint` : "")
      + (th.hint ? ` (${th.hint.factor}× its usual gap, at least ${th.hint.min_min} min).` : ".")
      + " A serial two dongles share is critical.";
    if (window.Help && Help.info) thr.append(" ", Help.info(rules, "Alert rules")); else thr.title = rules;
    kids.push(thr);
    card.replaceChildren(...kids);
  }

  // An incident source stopped itself (#301, e.g. a login it won't retry): its reset() on the station, admins only
  // (POST /api/sources/reset: a sources_reset job; who pressed it is logged on the hub). Only while it is failing.
  function resetRow(failing) {
    const r = el("div", "row");
    for (const x of failing) {
      const name = x.label || x.source;
      const btn = el("button", "", `Reset ${name}`);
      btn.title = x.reason || "failing";
      btn.addEventListener("click", async () => {
        if (!confirm(`Reset ${name} on the station?\n${x.reason || "failing"}`)) return;
        btn.disabled = true;
        let msg;
        try {
          const res = await fetch("/api/sources/reset", { method: "POST",
                                                           headers: { "Content-Type": "application/json", "X-Sizeup-Action": "reset" },
                                                           body: JSON.stringify({ source: x.source }) });
          const d = await res.json().catch(() => ({}));
          msg = res.ok ? (d.result || "Reset asked") : (d.error || `HTTP ${res.status}`);
        } catch (e) { msg = String(e); }
        btn.disabled = false;
        btn.title = msg;
        r.append(el("span", "dim small", msg));
      });
      r.append(btn);
    }
    return r;
  }

  function say(text) {               // not the hub, or not an admin: one line, no poll
    if (card) card.replaceChildren(el("p", "empty", text));
  }

  async function poll() {
    clearTimeout(timer);
    timer = null;
    if (busy || !isOpen()) return;         // (one in flight schedules the next)
    busy = true;
    let again = true;
    try {
      const r = await fetch("/api/cad/health", { headers: { Accept: "application/json" } });
      if (r.status === 403) { again = false; say("Admins only"); }
      else if (r.status === 404 || r.status === 409) { again = false; say("On the hub only"); }
      else if (r.ok) { last = await r.json(); render(last); }
    } catch (_) { /* the next poll tries again */ }
    busy = false;
    if (again && isOpen()) timer = setTimeout(poll, POLL_MS);
  }

  // The links to what goes with it: the push topics, Amazon Transcribe's row in Data sources, the receivers.
  function links() {
    const r = el("div", "row");
    for (const [text, href, title] of [
      ["Push notifications", "#settings/system/pushes", "Where these alerts are pushed: the status topic (Settings > System)"],
      ["Amazon Transcribe", "#settings/data/transcribe", "Its row in Data sources: the last answer, the breaker, Try AWS now"],
      ["Data sources", "#settings/data", "Every data pull the hub keeps"],
      ["Receivers", "#settings/radios/receivers", "The Station row and the station's radios (Settings > Radios)"]]) {
      const a = el("a", "nav", text);
      a.href = href;
      a.title = title;
      r.append(a);
    }
    return r;
  }

// ---- the hub's own CPU (#425, sizeup/perf.py) ---------------------------------------------------------
  // GET /api/cad/perf (admins): CPU by thread over the last minute, 1 h and since the start, as shares of the task's
  // CPU; "Profile 30 s" (POST /api/cad/perf/profile) samples every thread's stack and lists the functions the CPU
  // went to. Its own card and poll (the numbers change once a minute), so a profile's answer stays on screen.
  const PERF_POLL_MS = 20000, PERF_ROWS = 12, PERF_FNS = 15;
  let perfCard = null, perfTimer = null, perfLast = null, perfBusy = false, profile = null, profiling = false, profileSaid = "";

  function renderPerf() {
    if (!perfCard || !perfLast) return;
    const d = perfLast, w = d.windows || {}, p = d.process || {}, v = d.vcpu, k = v ? 1 / v : 1;
    const share = (x) => (x === null || x === undefined ? "—" : `${(x * k).toFixed(1)}%`);
    const cell = (x) => { const td = el("td", `num${x * k >= 10 ? " hl-warn" : x ? "" : " dim"}`, share(x)); td.title = `${x}% of one core`; return td; };
    const now = w.now || {}, mb = (n) => (n ? `${Math.round(n / 1048576)} MB` : "—");
    const head = el("div", "card-head");
    const chip = el("span", `chip ${now.core * k >= 90 ? "hl-bad" : now.core * k >= 60 ? "hl-warn" : "hl-ok"}`, d.ticks ? share(now.core) : "—");
    chip.title = v ? `Last minute, of the task's ${v} vCPU (${now.core}% of one core)` : "Last minute, of one core";
    const facts = el("span", "dim small", [`up ${longDur(d.uptime_s)}`, mb(p.rss || p.rss_peak), `${p.threads} threads`,
      p.fds !== null && p.fds !== undefined ? `${p.fds} files` : "", `GC ${share((w.h1 || {}).gc)}`].filter(Boolean).join(" · "));
    facts.title = `Memory: resident${p.rss ? "" : " (peak)"}. Files: open descriptors. GC: the collector's share over 1 h `
      + `(${(p.gc || {}).collections || []} collections, longest ${(p.gc || {}).max_ms} ms)`;
    const btn = el("button", "", profiling ? "Profiling…" : "Profile 30 s");
    btn.disabled = profiling;
    btn.title = "Sample every thread's stack for 30 s, 25 times a second, weighted by the CPU each used. "
      + "Costs about a tenth of the task's CPU while it runs. One at a time";
    btn.addEventListener("click", runProfile);
    head.append(el("h2", "", "Hub CPU"), chip, el("span", "spacer"), facts, btn);
    const kids = [head];
    const t = table(["Thread", "Now", "1 h", "Since start"], [], d.ticks ? "No CPU used" : "First reading in a minute");
    const th = t.querySelectorAll("th");
    th[0].title = "Threads by name; request threads together as http, the state streams as http-stream";
    th[1].title = `Last minute. Share of ${v ? `the task's ${v} vCPU` : "one core"}; hover a value for its share of one core`;
    th[2].title = "Last hour";
    th[3].title = `Since the hub started (${when(d.started)}). Higher now than since the start: it grew with uptime`;
    const rows = (d.threads || []).filter((r) => r.now || r.h1 || r.boot >= 0.05).slice(0, PERF_ROWS);
    if (rows.length) {
      const body = t.querySelector("tbody");
      body.replaceChildren();
      const all = el("tr");
      all.append(el("td", "", "All"), cell(now.core), cell((w.h1 || {}).core), cell((w.boot || {}).core));
      body.append(all);
      for (const r of rows) {
        const tr = el("tr");
        tr.append(el("td", "mono", r.live > 1 ? `${r.name} ×${r.live}` : r.name), cell(r.now), cell(r.h1), cell(r.boot));
        body.append(tr);
      }
    }
    kids.push(t);
    if (profileSaid) kids.push(el("p", "notice", profileSaid));
    if (profile && !profile.error) {
      const o = profile.overhead || {};
      const sub = el("h3", "hl-sub", `Profile ${when(profile.started)} · ${Math.round(profile.seconds)} s · ${Math.round(profile.cpu_ms)} ms CPU`);
      sub.title = `${profile.samples} samples at ${profile.hz} Hz; the profiler itself used ${o.core_pct}% of one core`
        + (d.vcpu ? ` (${(o.core_pct / d.vcpu).toFixed(1)}% of the task's ${d.vcpu} vCPU)` : "");
      const fold = el("a", "nav", "Folded stacks");
      fold.href = "/api/cad/perf/profile?format=folded";
      fold.target = "_blank";
      fold.rel = "noopener";
      fold.title = "For a flame graph (flamegraph.pl, speedscope): thread;caller;…;function microseconds";
      const ft = table(["Function", "Self ms", "Self", "With callees"], [], "No CPU used");
      const cum = new Map((profile.cumulative || []).map((x) => [x.fn, x.pct]));
      const fr = (profile.self || []).slice(0, PERF_FNS);
      if (fr.length) {
        const body = ft.querySelector("tbody");
        body.replaceChildren();
        for (const x of fr) {
          const tr = el("tr");
          tr.append(el("td", "mono", x.fn), el("td", "num", x.ms.toFixed(0)), el("td", "num", `${x.pct}%`),
            el("td", "num dim", cum.has(x.fn) ? `${cum.get(x.fn)}%` : "—"));
          body.append(tr);
        }
      }
      const fth = ft.querySelectorAll("th");
      fth[1].title = "CPU in the function itself (and the C code it calls)";
      fth[3].title = "CPU in the function and everything it calls, of the profile's CPU";
      const by = el("p", "dim small", (profile.threads || []).slice(0, 8).map((x) => `${x.thread} ${x.pct}%`).join(" · "));
      by.title = "The profile's CPU by thread";
      kids.push(sub, by, ft, fold);
    }
    perfCard.replaceChildren(...kids);
  }

  async function runProfile() {
    if (profiling) return;
    profiling = true;
    profileSaid = "";
    renderPerf();
    try {
      const r = await fetch("/api/cad/perf/profile", { method: "POST", headers: { "Content-Type": "application/json" },
                                                       body: JSON.stringify({ seconds: 30 }) });
      const d = await r.json().catch(() => ({}));
      if (r.ok && r.status === 200 && !d.error) profile = d;
      else profileSaid = d.error || (d.running ? "Still running" : `HTTP ${r.status}`);
    } catch (e) { profileSaid = String(e); }
    profiling = false;
    renderPerf();
  }

  async function pollPerf() {
    clearTimeout(perfTimer);
    perfTimer = null;
    if (perfBusy || !isOpen()) return;
    perfBusy = true;
    let again = true;
    try {
      const r = await fetch("/api/cad/perf", { headers: { Accept: "application/json" } });
      if (r.status === 403 || r.status === 404 || r.status === 409) { again = false; if (perfCard) perfCard.hidden = true; }
      else if (r.ok) { perfLast = await r.json(); renderPerf(); }
    } catch (_) { /* the next poll tries again */ }
    perfBusy = false;
    if (again && isOpen()) perfTimer = setTimeout(pollPerf, PERF_POLL_MS);
  }

  // settings.js, every render of Settings > Health (admins): the section in place (once), then the poll.
  function mount(node) {
    if (panel !== node || !card || !node.contains(card)) {
      panel = node;
      card = el("div", "card s-card hl-card");
      card.append(el("div", "loading", "Loading…"));
      node.replaceChildren(...sHead("Health", "The station's health and the hub's own checks (Amazon Transcribe and its "
        + "monthly limit): what is wrong now, since when and where, then the station, its programs, dongles and channels, "
        + "and 24 h of alerts with when each recovered. Each alert and its recovery are pushed to the status topic "
        + "(Push notifications). Admins only: nobody else sees these, on the console or the wall board."), links(), card);
      perfCard = el("div", "card s-card hl-card");
      node.append(perfCard);
    }
    if (last) render(last);
    renderPerf();
    poll();
    pollPerf();
  }

  document.addEventListener("visibilitychange", () => { if (isOpen() && !timer) poll(); if (isOpen() && !perfTimer) pollPerf(); });
  return { mount, render };
})();
window.StationHealth = StationHealth;
