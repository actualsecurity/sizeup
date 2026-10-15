// Settings > Data sources (GitHub #186, sizeup/datasources.py): every data pull the hub keeps (the county GIS layers,
// the camera lists and highway feeds, Metro-North, weather, aircraft and the FAA registry, the airport layer, the tone
// list), each with when it was last fetched, when it is due next, its status and size, how often it is refreshed
// (an admin picks it for the slow ones; the rest keep the provider's own) and Refresh now (admins; rate-limited by
// the hub). GET /api/cad/datasources is polled only while this section is on screen (every POLL_MS, every RUN_MS
// while a refresh runs): nothing goes in the state push. Self-contained: settings.js mounts it (sRender).
const DataSourcesPanel = (() => {
  const POLL_MS = 15000, RUN_MS = 2000;
  const EVERY = [["off", "Off"], ["hourly", "Hourly"], ["daily", "Daily"], ["weekly", "Weekly"], ["monthly", "Monthly"]];
  let panel = null, box = null, data = null, timer = null, busy = false, opts = {}, err = "", watching = new Set();

  const isOpen = () => !!panel && !panel.hidden && !document.hidden && document.body.contains(panel);

  function ago(t, now) {
    const s = Math.round(now - t), a = Math.abs(s);
    const v = a < 60 ? `${a} s` : a < 5400 ? `${Math.round(a / 60)} min` : a < 172800 ? `${Math.round(a / 3600)} h` : `${Math.round(a / 86400)} d`;
    if (a < 5) return "now";
    return s >= 0 ? `${v} ago` : `in ${v}`;
  }
  function when(t, now) {           // relative, then the time in the console's zone (fmtTime: cad.js)
    const w = el("div", "ds-when");
    if (!t) { w.append(el("span", "dim", "—")); return w; }
    w.append(el("span", "", ago(t, now)), el("span", "dim small", fmtTime(t, false)));
    w.title = fmtTime(t, true);
    return w;
  }
  function size(s) {
    const parts = [];
    if (s.items != null) parts.push(`${s.items.toLocaleString("en-US")} item${s.items === 1 ? "" : "s"}`);
    if (s.bytes) parts.push(s.bytes < 1e6 ? `${Math.max(1, Math.round(s.bytes / 1e3))} KB` : `${(s.bytes / 1e6).toFixed(1)} MB`);
    return parts.join(" · ") || "—";
  }

  function statusCell(s, now) {
    const td = el("td", "ds-status");
    let chip;
    if (s.running) chip = el("span", "chip ds-st run", "Running");
    else if (!s.on) chip = el("span", "chip ds-st off", "Not configured");
    else if (!s.ok) chip = el("span", "chip ds-st err", "Error");
    else if (!s.last_fetched) chip = el("span", "chip ds-st off", "Not fetched yet");
    else chip = el("span", "chip ds-st ok", "OK");
    td.append(chip);
    if (s.running && s.started) td.append(el("div", "dim small", `since ${fmtTime(s.started, true)}`));
    else if (!s.on && s.why) td.append(el("div", "dim small", s.why));
    else if (s.error) td.append(el("div", "error ds-err", s.error));
    const r = s.last_run;
    if (r && r.at && !s.running) {
      const who = r.by ? opParts(r.by)[0] : (r.why === "schedule" ? "on schedule" : "");
      td.append(el("div", "dim small", `${r.ok ? "Refreshed" : "Tried"} ${ago(r.at, now)}${who ? ` (${who})` : ""}`));
    }
    return td;
  }

  function scheduleCell(s) {
    const td = el("td", "ds-sched");
    if (s.schedule === "managed") {
      const items = EVERY.map(([k, t]) => [k, k === s.default_every ? `${t} (default)` : t]);
      const pick = sSelect(items, s.every, (v) => sPost("datasources", { action: "set", key: s.key, value: v },
        `${s.name}: ${EVERY.find(([k]) => k === v)[1].toLowerCase()}`), `How often ${s.name} is refreshed`);
      td.append(pick);
      if (s.every !== s.default_every) td.append(el("span", "chip s-state edited", "edited"));
      if (s.quiet && s.every !== "off") td.append(el("div", "dim small", `overnight (${(data.quiet_hours || []).map((h) => `${String(h).padStart(2, "0")}:00`).join("–")})`));
    } else if (s.schedule === "builtin") {
      td.append(el("span", "small", s.builtin));
    } else {
      td.append(el("span", "dim small", s.note || "—"));
    }
    return td;
  }

  function actionCell(s) {
    const td = el("td", "ds-act");
    if (!s.refreshable) return td;
    const b = sBtn(s.running ? "Running…" : s.action, s.min_gap_s >= 3600 ? "At most once an hour" : "At most once every 2 minutes",
      () => refresh(s), "sm");
    b.disabled = s.running || !canDo("admin");
    if (!canDo("admin")) b.title = askAdmin();
    td.append(b);
    return td;
  }

  function row(s, now) {
    const tr = el("tr", s.running ? "ds-row run" : "ds-row");
    tr.dataset.key = s.key;
    const name = el("td", "ds-name");
    name.append(el("b", "", s.name));
    const src = el("div", "dim small");
    if (s.link) {
      const a = el("a", "", s.source || s.link);
      a.href = s.link; a.target = "_blank"; a.rel = "noopener";
      src.append(a);
    } else src.append(s.source || "");
    name.append(src);
    if (s.note && s.schedule !== "none") name.append(el("div", "dim small", s.note));
    let next;
    if (s.schedule === "managed" && s.every === "off") next = el("span", "dim", "By hand");
    else if (s.schedule === "builtin" && !s.next_due) next = el("span", "dim", s.on ? "While viewed" : "—");
    else next = when(s.next_due, now);
    const nextTd = el("td");
    nextTd.append(next);
    const lastTd = el("td");
    lastTd.append(when(s.last_fetched, now));
    tr.append(name, lastTd, nextTd, statusCell(s, now), el("td", "ds-size", size(s)), scheduleCell(s), actionCell(s));
    return tr;
  }

  function render() {
    if (!panel) return;
    const a = document.activeElement;
    if (box && box.contains(a) && a.matches("select")) return;     // not from under someone picking an interval
    const now = serverNow();
    const kids = [];
    if (err) kids.push(el("div", "notice err", err));
    if (data && !data.configured) kids.push(el("div", "empty", "No data sources here: the CAD of record (the hub) keeps them"));
    if (data && data.sources) {
      const running = data.sources.filter((s) => s.running).length, bad = data.sources.filter((s) => s.on && !s.ok).length;
      const sum = el("div", "row ds-sum");
      sum.append(el("span", "dim small", `${data.sources.length} sources`
        + (running ? ` · ${running} refreshing` : "") + (bad ? ` · ${bad} with an error` : "")), el("span", "spacer"));
      const edited = data.sources.filter((s) => s.schedule === "managed" && s.every !== s.default_every).length;
      if (edited && canDo("admin")) sum.append(sBtn("Reset schedules", "Every data source back to its default schedule",
        () => sAsk(`Put ${edited} data source${edited === 1 ? "" : "s"} back on the default schedule?`,
          () => sPost("datasources", { action: "reset" }, "Data sources: default schedules"), "Reset"), "sm"));
      kids.push(sum);
      for (const g of data.groups) {
        const rows = data.sources.filter((s) => s.group === g);
        if (!rows.length) continue;
        const t = el("table", "evtable ds-table");
        t.append(el("thead"), el("tbody"));
        const head = el("tr");
        for (const h of ["Source", "Last fetched", "Next due", "Status", "Size", "Schedule", ""]) head.append(el("th", "", h));
        t.tHead.append(head);
        for (const s of rows) t.tBodies[0].append(row(s, now));
        const sc = el("div", "tscroll");
        sc.append(t);
        const errs = rows.filter((s) => s.on && !s.ok).length;
        kids.push(sCard(sCardHead(g, `${rows.length}${errs ? ` · ${errs} error${errs === 1 ? "" : "s"}` : ""}`), sc));
      }
    } else if (!err) kids.push(el("div", "loading", "Loading…"));
    box.replaceChildren(...kids);
  }

  function finished() {             // a refresh started from this page has ended: say how it went
    for (const key of [...watching]) {
      const s = data && data.sources && data.sources.find((x) => x.key === key);
      if (!s || s.running) continue;
      watching.delete(key);
      if (s.last_run && s.last_run.ok) UI.toast(`${s.name}: refreshed`);
      else if (opts.msg) opts.msg(`${s.name}: ${(s.last_run && s.last_run.error) || s.error || "not refreshed"}`);
    }
  }

  async function load() {
    if (busy || !panel) return;
    busy = true;
    try {
      const q = data && data.rev ? `?rev=${encodeURIComponent(data.rev)}` : "";
      const r = await api("GET", `/api/cad/datasources${q}`);
      err = "";
      if (!r.same) { data = r; finished(); }
    } catch (e) {
      err = `Data sources: ${e.message}`;
    } finally {
      busy = false;
    }
    render();
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (!isOpen()) return;
    const running = data && data.sources && data.sources.some((s) => s.running);
    timer = setTimeout(load, running || watching.size ? RUN_MS : POLL_MS);
  }

  async function refresh(s) {
    const op = operator();
    if (!op) {
      opts.msg && opts.msg("Enter operator name (top right)");
      ($("#operator-pick").hidden ? $("#operator") : $("#operator-pick")).focus();
      return;
    }
    try {
      await api("POST", `/api/cad/datasources/${encodeURIComponent(s.key)}/refresh`, { operator: op });
      watching.add(s.key);
      UI.toast(`${s.name}: refreshing`);
    } catch (e) {
      opts.msg && opts.msg(e.message);
    }
    if (data) data.rev = "";                 // the whole list next, not "same"
    load();
  }

  // settings.js, every render of the section: the list in place (once), then fresh data and the poll.
  function mount(node, o = {}) {
    opts = o;
    if (panel !== node || !box || !node.contains(box)) {
      panel = node;
      box = el("div", "ds-body");
      node.replaceChildren(...sHead("Data sources", "Every data pull the hub keeps: when it was last fetched, when it is due "
        + "next, and how it went. The slow ones (map layers, camera lists, the stations and line, the FAA registry) are "
        + "refreshed on the schedule picked here, the heavy ones overnight; the live feeds keep their own. Refresh now "
        + "fetches one again at once (admins; at most once every 2 minutes per source, the FAA registry once an hour). "
        + "Every refresh and schedule change is in the audit log (System)."), box);
    }
    if (data) render();
    if (data) data.rev = "";
    load();
  }

  document.addEventListener("visibilitychange", () => { if (isOpen()) load(); });
  return { mount };
})();
window.DataSourcesPanel = DataSourcesPanel;
