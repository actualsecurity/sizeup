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
  function alertRows(alerts, n) {
    return alerts.map((a) => {
      const r = el("div", `hb-row ${a.severity === "critical" ? "critical" : "warning"}`);
      const where = whoText(a.who);
      const rec = !n.on ? "Not pushed (push off)" : a.pushed ? "Pushed" : "Not pushed yet";
      const det = el("span", "hb-detail dim small", [where, rec].filter(Boolean).join(" · "));
      det.title = n.on ? "Pushed to the status topic; a recovery notice follows when it clears" : "Push off: it clears here and in the log only";
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
    // #278: why it is off and where to set it, behind an "i" (a tap on a tablet; a title alone never shows there)
    if (!n.on && window.Help && Help.info) pushEl.append(" ", Help.info("No status topic in effect. Set one in Settings > System > Push notifications, SSM /sizeup-hub/status-topic or [alerts]"));
    const head = el("div", "card-head");
    head.append(el("h2", "", "Now"),
      el("span", `chip ${d.alerts.length ? "hl-bad" : "hl-ok"}`, d.alerts.length ? `${d.alerts.length} alert${d.alerts.length === 1 ? "" : "s"}` : "No alerts"),
      el("span", "spacer"), pushEl);
    const kids = [head];
    if (!h.reported) kids.push(el("p", "notice", "No station report yet"));
    else if (!h.health_reported) {
      const old = el("p", "notice", "Station on an older sizeup: no CPU, memory, disk or programs");
      old.title = "They show once the station updates";
      kids.push(old);
    }
    kids.push(el("h3", "hl-sub", "Alerts now"),
      ...(d.alerts.length ? [el("div", "hl-alerts")] : [el("p", "dim small", "None")]));
    if (d.alerts.length) kids[kids.length - 1].append(...alertRows(d.alerts, n));
    if ((h.hints || []).length) {
      const box = el("div", "hl-hints");
      for (const x of h.hints) box.append(el("p", "notice info", `${x.channel}: ${x.text}`));
      kids.push(el("h3", "hl-sub", "Hints"), box);
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
    ]));
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
        ["Waiting", `${sq.queued}/${sq.max} · ${sq.urgent} urgent · ${sq.live - sq.urgent} other live · ${sq.rerun} re-runs`],
        ["Oldest live clip", sq.oldest_s !== null && sq.oldest_s !== undefined ? longDur(sq.oldest_s) : "none",
          (sq.oldest_s || 0) >= 600 ? "hl-bad" : ""],
        ["Dropped / skipped", `${sq.dropped || 0} / ${sq.skipped || 0}` + (sq.last_drop ? ` · last: ${sq.last_drop.why}` : ""),
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
    const rules = "Each alert is pushed at most once per interval, plus its recovery."
      + (th.trunk ? ` P25 trunk silent (critical): no call on any talkgroup for ${th.trunk.day_min} min (${th.trunk.night_min} min into the night, 00-06), `
        + `longer when ${th.trunk.factor}× the system's usual gap for that time of day is (learned from ${th.trunk.gaps_min}+ gaps), `
        + `or the control channel lost ${th.trunk.cc_lost_min} min. One channel or talkgroup silent is only a hint` : "")
      + (th.hint ? ` (${th.hint.factor}× its usual gap, at least ${th.hint.min_min} min).` : ".")
      + " A serial two dongles share is critical.";
    if (window.Help && Help.info) thr.append(" ", Help.info(rules, "Alert rules")); else thr.title = rules;
    kids.push(thr);
    card.replaceChildren(...kids);
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
    }
    if (last) render(last);
    poll();
  }

  document.addEventListener("visibilitychange", () => { if (isOpen() && !timer) poll(); });
  return { mount, render };
})();
window.StationHealth = StationHealth;
