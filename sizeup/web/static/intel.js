// Radio pre-alerts (sizeup/intel, #411): police / fire chatter rules -> events. Two self-contained parts, so either can
// move (Settings is being regrouped):
//
//   IntelCard.column(list, state, deps)   the console's 60 Control column: one row per open event at map or push
//                                         (the state's "intel": {open}; only for a signed-in viewer). Category · town,
//                                         the place (hollow "?" when unconfirmed), WHAT / WHERE, play its clips,
//                                         Useful / Noise / Wrong place, Dismiss (dispatchers): icon buttons, named
//                                         in their hovers; More info (its page,
//                                         /cad#prealert/intel/<id>: prealert-info.js). deps: {api, operator, time,
//                                         readonly, more}
//   IntelSettings.mount(card)             Settings > System > Intel: the kill switch, caps, each pack's mode and
//                                         threshold, each category's mode, each rule's mode and weight with its
//                                         7-day hits. Terse; the why is in the hovers. Admins edit (sPost, settings.js)
"use strict";

window.IntelCard = (() => {
  // busy: the cards with a POST in flight (their buttons disabled); mine: the votes made here, per event (the state is
  // everyone's, so a person's own votes are what this page sent: pressed, and the same one again takes it back)
  const st = { busy: new Set(), audio: null, playing: "", mine: new Map() };
  const PAIR = { useful: ["useful", "noise"], noise: ["useful", "noise"] };
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const pct = (x) => `${Math.round((x || 0) * 100)}%`;
  // a card's icon action (icons.js Icon.button; its name as text where there are no icons): "…" while busy
  function iconBtn(icon, name, title, busy, fn, cls = "") {
    const x = window.Icon ? Icon.button(icon, name, title, cls) : el("button", `mini ${cls}`, name);
    x.type = "button";
    if (!window.Icon) x.title = title;
    if (busy) x.replaceChildren("…");
    x.disabled = busy;
    x.addEventListener("click", fn);
    return x;
  }
  function play(id, b) {
    if (st.audio) st.audio.pause();
    if (st.playing === id) { st.playing = ""; b.textContent = "▶"; return; }
    st.audio = new Audio(`/api/audio/${encodeURIComponent(id)}.wav`);
    st.playing = id;
    b.textContent = "■";
    const done = () => { st.playing = ""; b.textContent = "▶"; };
    st.audio.addEventListener("ended", done);
    st.audio.play().catch((e) => { done(); UI.toast(`Can't play: ${e.message}`, "alert"); });
  }
  // the last push at ntfy (sizeup/intel/events.py delivery): "sent 16:21:27" / "not sent: 429" / "queued"
  function delivered(e, time) {
    const d = e.delivery;
    if (!d || !(d.at >= e.pushed_at)) return "queued";
    return d.ok ? `sent ${time(d.at)}` : `not sent: ${d.status || d.error || "?"}`;
  }
  function where(e) {
    if (!e.place || e.precision === "none") return "Location not heard";
    return e.confirmed ? e.place : `${e.place} ?`;
  }
  function card(e, deps, list) {
    const li = el("li", `aa aa-${e.level}`);
    li.dataset.key = String(e.id);
    const head = el("div", "aa-head");
    const conf = el("span", `aa-conf ${e.level}`, `${e.level} ${pct(e.what)}`);
    conf.title = `What ${pct(e.what)} · where ${pct(e.where)} (${e.precision})\nRules: ${(e.rules || []).map((r) => r[0]).join(", ")}`
      + (e.suppressed ? `\nNot pushed: ${e.suppressed}` : e.pushed_at ? `\nPushed ${deps.time(e.pushed_at)} · ${delivered(e, deps.time)}` : "");
    head.append(el("b", "aa-title", [e.pack_label, e.label, e.town].filter(Boolean).join(" · ")), conf);
    li.append(head);
    const loc = el("div", "aa-loc" + (e.confirmed ? "" : " approx"), where(e));
    if (!e.confirmed && e.place) loc.title = ["Unconfirmed", e.basis, ...(e.alts || []).map((a) => `or ${a.label}`)].filter(Boolean).join("\n");
    li.append(loc);
    if (e.suggested) {
      const sug = el("div", "aa-loc approx", `model: ${e.suggested}?`);
      sug.title = "The model's reading, not the rules': on the map only, never pushed";
      li.append(sug);
    }
    if (e.note) li.append(el("div", "aa-loc approx", e.note));          // "possibly cancelled", "moved", "re-opened", "de-escalated"
    if (e.fd_shown) {                    // the FD already there (sizeup/intel/engine.py _fire): "FD on scene · #26-000123"
      const fd = el("div", "aa-loc", e.fd_shown);
      fd.title = e.fd === "cleared" ? `${e.fd_shown.split(" · ")[0]} (cleared): pre-alert`
        : "The FD was already there when PD put this out: an FYI, not a pre-alert";
      li.append(fd);
    }
    const src = el("div", "aa-src", `${deps.time(e.first_at)} · ${(e.traffic_ids || []).length} tx`);
    for (const id of (e.traffic_ids || []).slice(-4).reverse()) {     // the latest four, newest first
      const b = el("button", "mini play", st.playing === id ? "■" : "▶");
      b.type = "button";
      b.title = "Play (while its audio is kept)";
      b.addEventListener("click", (ev) => { ev.stopPropagation(); play(id, b); });
      src.append(" ", b);
    }
    li.append(src);
    const more = () => {
      const m = el("a", "nav mini quiet aa-more", "More info");
      m.href = window.PrealertInfo ? PrealertInfo.href("intel", e.id) : `#prealert/intel/${e.id}`;
      m.title = "Its page: the full transcripts and their audio, the place, site history, the outcome";
      m.addEventListener("click", (ev) => ev.stopPropagation());
      return m;
    };
    if (deps.readonly || (typeof canDo === "function" && !canDo("active"))) {     // (a deactivated account: no votes)
      if (deps.more) { const r = el("div", "aa-btns"); r.append(more()); li.append(r); }
      return li;
    }
    const row = el("div", "aa-btns");
    if (deps.more) row.append(more());
    const busy = st.busy.has(e.id);
    const mine = st.mine.get(e.id) || new Set();
    const btns = [];
    const act = (path, body, ok) => async (ev) => {
      ev.stopPropagation();
      if (st.busy.has(e.id)) return;
      st.busy.add(e.id);
      for (const x of btns) x.disabled = true;      // at once: a double click is one vote, never one taken back
      try {
        const r = await deps.api("POST", `/api/cad/intel/${e.id}/${path}`, { ...body, operator: deps.operator() });
        const v = body.verdict || (path === "dismiss" ? "noise" : "");
        if (PAIR[v]) {                               // (a dismiss is a Noise vote: events.ensure_vote)
          const m = new Set(st.mine.get(e.id) || []);
          for (const x of PAIR[v]) m.delete(x);
          if (!(r && r.removed)) m.add(v);
          st.mine.set(e.id, m);
        }
        if (ok) UI.toast(r && r.removed ? "Vote taken back" : ok, "ok");      // a vote again takes it back (events.vote)
      } catch (err) {
        UI.toast(`${path}: ${err.message}`, "err");
      } finally {
        st.busy.delete(e.id);
        for (const x of btns) x.disabled = false;
        if (list && list._args) { list._sig = ""; column(list, ...list._args); }   // drawn again: the vote pressed
      }
    };
    // icon buttons (a narrow column never cuts their names short): the name is the aria-label, the hover says it all
    const b = (icon, name, title, fn, cls = "", vote = "") => {
      const x = iconBtn(icon, name, title, busy, fn, cls);
      if (vote) x.setAttribute("aria-pressed", String(mine.has(vote)));
      btns.push(x);
      return x;
    };
    const acts = el("span", "aa-acts");             // (they wrap together, under More info, in a narrow column)
    acts.append(b("thumbs-up", "Useful", "it was real", act("feedback", { verdict: "useful" }, "Noted: useful"), "", "useful"),
      b("thumbs-down", "Noise", "nothing to it (the rule's numbers)", act("feedback", { verdict: "noise" }, "Noted: noise"), "", "noise"),
      b("map-pin-off", "Wrong place", "the place is wrong", act("feedback", { verdict: "wrong_place" }, "Noted: wrong place")));
    if (typeof canDo !== "function" || canDo("dispatch")) {
      acts.append(b("x", "Dismiss", "off the list (logged as noise)", act("dismiss", {}, ""), "pa-dismiss"));
    }
    row.append(acts);
    li.append(row);
    return li;
  }
  function column(list, s, deps) {
    if (!list) return;
    list._args = [s, deps];
    const items = (s && s.open) || [];
    list.hidden = !items.length;
    const sig = JSON.stringify(items.map((e) => [e.id, e.level, e.what, e.where, e.place, e.confirmed, e.traffic_ids,
      e.suppressed, e.pushed_at, e.delivery, e.fd_shown, st.busy.has(e.id), st.playing, [...(st.mine.get(e.id) || [])]]));
    if (list._sig === sig) return;
    list._sig = sig;
    list.replaceChildren(...items.map((e) => card(e, deps, list)));
  }
  return { column, delivered };
})();

window.IntelSettings = (() => {
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const post = (body, done) => sPost("intel", body, done || "Saved: intel");
  // label: the field's name (aria-label); tip: its hover, what it is and its unit (the name, when that says it)
  function num(value, min, max, step, onSave, label, tip) {
    const i = el("input", "in-num");
    i.type = "number";
    Object.assign(i, { min: String(min), max: String(max), step: String(step), value: String(value) });
    i.setAttribute("aria-label", label);
    i.title = tip || label;
    i.addEventListener("change", () => { const v = Number(i.value); if (!Number.isNaN(v)) onSave(v); });
    return i;
  }
  function modeSel(modes, value, onSave, label) {
    const s = sSelect(modes.map((m) => [m, m]), value, onSave, label);
    s.title = `${label}: off · shadow (counted only) · log · map · push`;
    return s;
  }
  // A field under its terse label (style.css .field): the label carries the field's hover. cls: its width (in-w-*).
  function field(text, ctl, cls) {
    const l = el("label", "field in-f" + (cls ? ` ${cls}` : ""));
    l.title = ctl.title;
    l.append(el("span", "in-l", text), ctl);
    return l;
  }
  // One line of the card's grid (settings.css .in-rows): what it is, its fields, what it reports at the end.
  function line(lead, fields, ...stat) {
    const r = el("div", "in-row"), f = el("div", "in-fields"), st = el("div", "in-stat");
    lead.classList.add("in-name");
    f.append(...fields);
    st.append(...stat);
    r.append(lead, f, st);
    return r;
  }
  async function mount(card) {
    if (!card) return;
    let d;
    try {
      d = await api("GET", "/api/cad/intel");
    } catch (e) {
      card.hidden = /sign-in|403|admins only/i.test(e.message);
      card.replaceChildren(sCardHead("Intel"), el("div", "error", `Intel: ${e.message}`));
      return;
    }
    card.hidden = false;
    const admin = typeof canDo !== "function" || canDo("admin");
    const s = d.settings;
    const c24 = d.counts_24h || {};
    const set = (key) => (v) => post({ action: "set", key, value: v });
    const top = el("div", "in-rows");
    // the kill switch and the caps
    const [lab, cb] = sCheck("Push", s.push, "Off: detection and the map go on, nothing is pushed");
    cb.addEventListener("change", () => post({ action: "set", key: "push", value: cb.checked }, cb.checked ? "Intel pushes on" : "Intel pushes off"));
    // PII in intel pushes (policy.py push_pii; admin decision 2026-11-17): on, no scrub; off, the scrub as before
    const [plab, pcb] = sCheck("PII", s.push_pii, "On: intel pushes are not scrubbed (names, plates may appear). "
      + "Off: a push the PII scrub catches is withheld. Push topics are readable by anyone who knows the topic name");
    pcb.addEventListener("change", () => post({ action: "set", key: "push_pii", value: pcb.checked }, pcb.checked ? "PII in pushes on" : "PII in pushes off"));
    const counts = el("span", "dim small", `24 h: ${c24.events || 0} events · ${c24.pushed || 0} pushed · ${c24.suppressed || 0} held`);
    counts.dataset.live = "counts";          // (settings.js sCardSwap: patched in place, never a reason to swap the card)
    counts.title = "The last 24 hours: events made, pushed, and held: not pushed for any reason (shadow mode, pushes off, "
      + "quiet hours, a cap, merged into an earlier push, withheld by the scrub, or unsent)";
    top.append(line(lab, [
      field("/h", num(s.per_hour, 1, 1000, 1, set("per_hour"), "Pushes an hour", "Pushes an hour, at most")),
      field("/day", num(s.per_day, 1, 10000, 1, set("per_day"), "Pushes a day", "Pushes a day, at most")),
      field("/cat/h", num(s.per_category_hour, 1, 1000, 1, set("per_category_hour"), "Pushes an hour, one category", "Pushes an hour for one category, at most")),
      field("repush min", num(s.repush_min, 0, 1440, 1, set("repush_min"), "Minutes between pushes of one event")),
      field("max age min", num(s.max_age_min, 1, 1440, 1, set("max_age_min"), "A transmission older than this is never a pre-alert (a restart replays nothing)",
        "Minutes: a transmission older than this is never a pre-alert (a restart replays nothing)")),
    ], plab, counts));
    // the push's "Listen" link (sizeup/intel/listen.py): the audio around the alert, behind a short-lived link
    const ls = d.listen || {};
    const [llab, lcb] = sCheck("Listen link", s.listen, "A link in each push that plays the transmissions around it (no sign-in; it expires)");
    lcb.addEventListener("change", () => post({ action: "set", key: "listen", value: lcb.checked }, lcb.checked ? "Listen links on" : "Listen links off"));
    const lnum = (key, min, max, label, tip) => num(s[key], min, max, 1, set(key), label, tip);
    // ready / not configured; a pushed link refused by S3 (the self-test, sizeup/intel/listen.py) reads "refused"
    const lk = ls.links || {};
    const lbad = ls.configured && lk.state === "failed";
    const lst = el("span", ls.configured && !lbad ? "chip s-state default" : "chip s-state edited",
      !ls.configured ? "not configured" : lbad ? "refused" : "ready");
    if (!ls.configured) lst.title = ls.why || "";
    else if (lbad) lst.title = `A pushed link was refused: HTTP ${lk.status}${lk.code ? " " + lk.code : ""}. Phones can't play it`;
    else if (lk.state === "ok") lst.title = "A pushed link played";
    top.append(line(llab, [
      field("expiry min", lnum("listen_expiry_min", 1, 720, "Minutes the link plays", "Minutes the link plays, then it expires")),
      field("before s", lnum("listen_before_s", 0, 600, "Seconds of the same source before the first transmission")),
      field("after s", lnum("listen_after_s", 0, 600, "Seconds after the last (stitched again as they arrive)", "Seconds after the last transmission (stitched again as they arrive)")),
      field("kbps", lnum("listen_kbps", 16, 64, "Bitrate (AAC when ffmpeg is there, else 8 kHz WAV)", "Bitrate, kbit/s (AAC when ffmpeg is there, else 8 kHz WAV)")),
    ], lst));
    // the model behind the rules (sizeup/intel/infer.py): off until sizeup-aws 26 has run; capped
    const inf = d.infer || {};
    const owned = !!(d.model || {}).on && ((d.model || {}).cfg || {}).infer_pd !== false;     // backing.py owns the police pack
    const [ilab, icb] = sCheck("Inference", !!s.infer, "An LLM (Bedrock, the department's account) only where the rules are unsure; its place is checked against the map data"
      + (owned ? ". Not for the police pack while Local model is on (one model per row)" : ""));
    icb.addEventListener("change", () => post({ action: "set", key: "infer", value: icb.checked }, icb.checked ? "Inference on" : "Inference off"));
    const inum = (key, min, max, step, label, tip) => num(s[key] ?? "", min, max, step, set(key), label, tip);
    const model = sInput(s.infer_model || "", 120, "Model id", set("infer_model"), "mono");
    model.title = "Bedrock model id (default anthropic.claude-haiku-5-5)";
    const spent = "$" + (inf.usd_month || 0).toFixed(2);
    const used = el("span", "dim small", (inf.calls_today || 0) + " today · " + spent + " this month");
    used.title = "Calls today · what they cost this month (US dollars, from the tokens each call reports)";
    const ist = el("span", inf.ready ? "chip s-state default" : "chip s-state edited", inf.ready ? "ready" : (inf.on ? "held" : "off"));
    if (inf.why) ist.title = inf.why;
    top.append(line(ilab, [
      field("model", model, "in-w-text"),
      field("/day", inum("infer_daily_cap", 0, 10000, 1, "Calls a day at most")),
      field("$/month", inum("infer_budget_usd", 0, 500, 1, "Budget a month (from the tokens each call reports)", "Budget, US dollars a month (from the tokens each call reports)")),
      field("sample %", inum("infer_sample_pct", 0, 100, 1, "Of rows with a hit: asked and recorded only (evaluation)", "Percent of rows with a hit: asked and recorded only (evaluation)")),
    ], used, ist));
    // the local model behind the police rules (sizeup/intel/backing.py): the PC worker when it is there, else rules only
    const mo = d.model || {};
    const mt = mo.today || {};
    const mc = mo.cfg || {};
    const mnum = (key, min, max, step, label, tip) => num(mc[key] ?? s[key] ?? "", min, max, step, set(key), label, tip);
    const [mlab, mcb] = sCheck("Local model", (mc.infer_pd ?? s.infer_pd) !== false, "The PC worker's model classifies police rows the rules left alone; none available: rules only");
    mcb.addEventListener("change", () => post({ action: "set", key: "infer_pd", value: mcb.checked }, mcb.checked ? "Local model on" : "Local model off"));
    const mused = el("span", "dim small", `${mt.asked || 0} asked · ${mt.events || 0} events today`);
    mused.title = "Today: " + ["asked", "ok", "no_worker", "gaming", "deadline", "invalid", "events", "evidence", "capped", "unavailable"]
      .map((k) => `${mt[k] || 0} ${k}`).join(" · ") + (mo.model ? `\nModel ${mo.model} ${mo.model_version || ""}` : "");
    const mst = el("span", mo.ready ? "chip s-state default" : "chip s-state edited", mo.ready ? "ready" : (mo.on ? "rules only" : "off"));
    mst.title = mo.ready ? `Backend: ${mo.backend}` : `PC worker: ${mo.pc || "unavailable"}`;
    top.append(line(mlab, [
      field("min conf", mnum("infer_pd_threshold", 0, 1, 0.05, "Confidence at least this makes an event")),
      field("/h", mnum("infer_pd_hour_cap", 0, 1000, 1, "Questions an hour at most")),
      field("/day", mnum("infer_pd_day_cap", 0, 20000, 1, "Questions a day at most")),
      field("wait s", mnum("infer_pd_after_s", 0, 60, 1, "Seconds to wait for the transmissions after it (0: ask at once)")),
    ], mused, mst));
    // its events push only when the transmission backs them (backing.py infer_pd_corroborate): else card and map
    const [clab, ccb] = sCheck("Corroborate", (mc.infer_pd_corroborate ?? s.infer_pd_corroborate) !== false,
      "On: a local model's event pushes only when its transmission also has a dispatch cue, a rule word of its category or a confirmed address; otherwise card and map only");
    ccb.addEventListener("change", () => post({ action: "set", key: "infer_pd_corroborate", value: ccb.checked }, ccb.checked ? "Model events need corroboration" : "Model events push alone"));
    top.append(line(clab, []));
    // (Bedrock for it: a stub, off by default; no control here until it is built: backing.py BedrockBackend)
    // detection on/off and quiet hours (no pushes in between; the card and the map go on)
    const [elab, ecb] = sCheck("Detection", s.enabled, "Off: no transmission is read for pre-alerts at all");
    ecb.addEventListener("change", () => post({ action: "set", key: "enabled", value: ecb.checked }, ecb.checked ? "Intel on" : "Intel off"));
    const hm = (key, label, tip) => {
      const i = el("input");
      i.type = "time";
      i.value = s[key] || "";
      i.title = tip;
      i.setAttribute("aria-label", label);
      i.addEventListener("change", () => post({ action: "set", key, value: i.value }));
      return i;
    };
    top.append(line(elab, [
      field("quiet from", hm("quiet_from", "Quiet from (no pushes; empty: none)", "Quiet hours from, local time (no pushes; empty: none)"), "in-w-time"),
      field("to", hm("quiet_to", "Quiet until", "Quiet hours until, local time"), "in-w-time"),
    ]));
    // a fire-related PD call the FD is already at (sizeup/intel/engine.py _fire): logged, an FYI only when it adds something
    const fdl = el("span", "", "FD there");
    fdl.title = "A fire-related PD call at one of our fire incidents (ours or the county feed's) with the FD there: no pre-alert; an FYI only when the words add something";
    top.append(line(fdl, [
      field("min", lnum("fd_window_min", 5, 720, "A closed incident counts this many minutes", "Minutes: an incident closed this long before the call still counts (open ones: up to 12 h)")),
      field("m", lnum("fd_radius_m", 25, 2000, "Within this many metres", "Metres: a confirmed address this close is the incident's (or the same address or block)")),
    ]));
    const kids = [top];
    const ran = new Map((((d.counters || {}).packs) || []).map((x) => [x.pack, x]));     // what each pack was handed
    for (const p of d.packs) {
      const pset = (key) => (v) => post({ action: "pack", pack: p.name, [key]: v });
      const c = ran.get(p.name);
      const seen = c ? el("span", c.level === "warn" ? "warn small" : "dim small", c.text) : "";
      if (c) seen.title = c.title;
      if (c) seen.dataset.live = `pack:${p.name}`;          // moves with every transcript: patched in place
      const head = el("div", "in-rows in-pack");
      head.append(line(el("b", "", `${p.label} (${p.name})`), [
        field("mode", modeSel(d.modes, p.mode, pset("mode"), `${p.label} pack mode`), "in-w-mode"),
        field("threshold", num(p.threshold, 0, 1, 0.05, pset("threshold"), "Threshold: WHAT at least this", "Threshold, 0 to 1: WHAT at least this")),
        field("window s", num(p.window_s, 10, 1800, 10, pset("window_s"), "Seconds of one source merged into one candidate")),
      ], seen));
      // each category: its name, then its mode (and, once changed, the way back), in columns of one width
      const cats = el("div", "in-cats");
      cats.setAttribute("role", "group");
      cats.setAttribute("aria-label", `${p.label} categories`);
      for (const c of p.categories) {
        const box = el("div", "in-cat");
        box.append(el("span", "in-cat-l", c.label));
        if (c.edited) {
          const back = sBtn("↺", "Back to the default", () => post({ action: "reset", what: "category", pack: p.name, category: c.name }), "icon quiet");
          back.setAttribute("aria-label", `${c.label}: back to the default`);
          box.append(back);
        }
        box.append(modeSel(d.modes, c.mode, (v) => post({ action: "category", pack: p.name, category: c.name, mode: v }), c.label));
        cats.append(box);
      }
      const tbl = el("table", "table small in-rules");
      const hr = el("tr");
      for (const [h, cls] of [["Rule"], ["Category"], ["Mode", "in-c"], ["Weight", "in-c"], ["Hits 7 d", "in-n"], ["Events", "in-n"]]) hr.append(el("th", cls || "", h));
      tbl.append(hr);
      for (const r of p.rules) {
        const tr = el("tr");
        const name = el("td", "mono in-rule", r.id);
        name.title = `${r.any.join("\n")}${r.not.length ? `\nnot: ${r.not.join(", ")}` : ""}`;
        const w = num(r.w, -1, 1, 0.05, (v) => post({ action: "rule", rule: r.id, w: v }), `${r.id} weight`);
        const td = (...xs) => { const n = el("td", "in-c"); n.append(...xs); return n; };
        const n = (head, v) => { const x = el("td", "num in-n", String(v || 0)); x.dataset.l = head; return x; };      // (its head, on a phone)
        tr.append(name, el("td", "in-rcat", r.category),
          td(modeSel(d.modes, r.mode, (v) => post({ action: "rule", rule: r.id, mode: v }), r.id)), td(w),
          n("hits 7 d", r.hits), n("events", r.events));
        tbl.append(tr);
      }
      const scroll = el("div", "tscroll");
      scroll.append(tbl);
      kids.push(head, cats, scroll);
    }
    if (!admin) for (const k of kids) for (const x of k.querySelectorAll("input, select, button")) x.disabled = true;
    // intel pushes go to their own topic only (sizeup/notify.py): none in effect, they are held (counted, not sent)
    const pt = d.push_topic, held = !!(pt && !pt.set);
    const pushState = held ? (pt.source === "same" ? "pushes held: intel topic refused" : pt.source === "off" ? "pushes held: intel topic off"
      : "pushes held: no intel topic") : "on";
    card.replaceChildren(sCardHead("Intel", s.push ? (s.enabled ? pushState : "detection off") : "pushes off",
      sHelp("Radio pre-alerts: rules over police (and later fire) transcripts. Each event goes off · shadow (counted "
        + "only) · log · map · push, the lowest of its pack, category and rule. Pushes go to the pre-alert (intel) topic "
        + "in Push notifications only, never the alerts or status topic: none set, pushes are held (counted, not sent). "
        + "Push off: detection and the map go on. Hits and events: 7 days. "
        + `Rule set ${d.ruleset_hash}.`)), ...kids);
    const info = card.querySelector(".card-head > .dim.small");
    if (info && held && s.push && s.enabled) { info.className = "warn small"; info.title = "Set one in Push notifications"; }
  }
  return { mount };
})();
