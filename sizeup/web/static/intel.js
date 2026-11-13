// Radio pre-alerts (sizeup/intel, #411): police / fire chatter rules -> events. Two self-contained parts, so either can
// move (Settings is being regrouped):
//
//   IntelCard.column(list, state, deps)   the console's 60 Control column: one row per open event at map or push
//                                         (the state's "intel": {open}; only for a signed-in viewer). Category · town,
//                                         the place (hollow "?" when unconfirmed), WHAT / WHERE, play its clips,
//                                         Useful / Noise, Dismiss (dispatchers). deps: {api, operator, time, readonly}
//   IntelSettings.mount(card)             Settings > System > Intel: the kill switch, caps, each pack's mode and
//                                         threshold, each category's mode, each rule's mode and weight with its
//                                         7-day hits. Terse; the why is in the hovers. Admins edit (sPost, settings.js)
"use strict";

window.IntelCard = (() => {
  const st = { busy: new Set(), audio: null, playing: "" };
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const pct = (x) => `${Math.round((x || 0) * 100)}%`;
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
  function where(e) {
    if (!e.place || e.precision === "none") return "Location not heard";
    return e.confirmed ? e.place : `${e.place} ?`;
  }
  function card(e, deps) {
    const li = el("li", `aa aa-${e.level}`);
    li.dataset.key = String(e.id);
    const head = el("div", "aa-head");
    const conf = el("span", `aa-conf ${e.level}`, `${e.level} ${pct(e.what)}`);
    conf.title = `What ${pct(e.what)} · where ${pct(e.where)} (${e.precision})\nRules: ${(e.rules || []).map((r) => r[0]).join(", ")}`
      + (e.suppressed ? `\nNot pushed: ${e.suppressed}` : e.pushed_at ? `\nPushed ${deps.time(e.pushed_at)}` : "");
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
    const src = el("div", "aa-src", `${deps.time(e.first_at)} · ${(e.traffic_ids || []).length} tx`);
    for (const id of (e.traffic_ids || []).slice(-4)) {
      const b = el("button", "mini play", st.playing === id ? "■" : "▶");
      b.type = "button";
      b.title = "Play (while its audio is kept)";
      b.addEventListener("click", (ev) => { ev.stopPropagation(); play(id, b); });
      src.append(" ", b);
    }
    li.append(src);
    if (deps.readonly) return li;
    const row = el("div", "aa-btns");
    const busy = st.busy.has(e.id);
    const act = (path, body, ok) => async (ev) => {
      ev.stopPropagation();
      st.busy.add(e.id);
      try {
        await deps.api("POST", `/api/cad/intel/${e.id}/${path}`, { ...body, operator: deps.operator() });
        if (ok) UI.toast(ok, "ok");
      } catch (err) {
        UI.toast(`${path}: ${err.message}`, "err");
      } finally {
        st.busy.delete(e.id);
      }
    };
    const b = (text, title, fn, cls = "mini") => {
      const x = el("button", cls, busy ? "…" : text);
      x.type = "button";
      x.title = title;
      x.disabled = busy;
      x.addEventListener("click", fn);
      return x;
    };
    row.append(b("Useful", "It was real", act("feedback", { verdict: "useful" }, "Noted: useful")),
      b("Noise", "Nothing to it (the rule's numbers)", act("feedback", { verdict: "noise" }, "Noted: noise")),
      b("Wrong place", "The place is wrong", act("feedback", { verdict: "wrong_place" }, "Noted: wrong place")));
    if (typeof canDo !== "function" || canDo("dispatch")) {
      row.append(b("Dismiss", "Off the list (logged as noise)", act("dismiss", {}, ""), "mini pa-dismiss"));
    }
    li.append(row);
    return li;
  }
  function column(list, s, deps) {
    if (!list) return;
    const items = (s && s.open) || [];
    list.hidden = !items.length;
    const sig = JSON.stringify(items.map((e) => [e.id, e.level, e.what, e.where, e.place, e.confirmed, e.traffic_ids,
      e.suppressed, e.pushed_at, st.busy.has(e.id), st.playing]));
    if (list._sig === sig) return;
    list._sig = sig;
    list.replaceChildren(...items.map((e) => card(e, deps)));
  }
  return { column };
})();

window.IntelSettings = (() => {
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const post = (body, done) => sPost("intel", body, done || "Saved: intel");
  function num(value, min, max, step, onSave, label) {
    const i = el("input", "s-num");
    i.type = "number";
    Object.assign(i, { min: String(min), max: String(max), step: String(step), value: String(value) });
    i.setAttribute("aria-label", label);
    i.title = label;
    i.addEventListener("change", () => { const v = Number(i.value); if (!Number.isNaN(v)) onSave(v); });
    return i;
  }
  function modeSel(modes, value, onSave, label) {
    const s = sSelect(modes.map((m) => [m, m]), value, onSave, label);
    s.title = `${label}: off · shadow (counted only) · log · map · push`;
    return s;
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
    const kids = [];
    // the kill switch and the caps
    const [lab, cb] = sCheck("Push", s.push, "Off: detection and the map go on, nothing is pushed");
    cb.disabled = !admin;
    cb.addEventListener("change", () => post({ action: "set", key: "push", value: cb.checked }, cb.checked ? "Intel pushes on" : "Intel pushes off"));
    const caps = el("div", "row");
    caps.append(lab,
      el("span", "dim small", "/h"), num(s.per_hour, 1, 1000, 1, (v) => post({ action: "set", key: "per_hour", value: v }), "Pushes an hour"),
      el("span", "dim small", "/day"), num(s.per_day, 1, 10000, 1, (v) => post({ action: "set", key: "per_day", value: v }), "Pushes a day"),
      el("span", "dim small", "/cat/h"), num(s.per_category_hour, 1, 1000, 1, (v) => post({ action: "set", key: "per_category_hour", value: v }), "Pushes an hour, one category"),
      el("span", "dim small", "repush min"), num(s.repush_min, 0, 1440, 1, (v) => post({ action: "set", key: "repush_min", value: v }), "Minutes between pushes of one event"),
      el("span", "dim small", "max age min"), num(s.max_age_min, 1, 1440, 1, (v) => post({ action: "set", key: "max_age_min", value: v }), "A transmission older than this is never a pre-alert (a restart replays nothing)"),
      el("span", "spacer"),
      el("span", "dim small", `24 h: ${c24.events || 0} events · ${c24.pushed || 0} pushed · ${c24.suppressed || 0} held`));
    for (const x of caps.querySelectorAll("input")) x.disabled = !admin;
    kids.push(caps);
    // the model behind the rules (sizeup/intel/infer.py): off until sizeup-aws 26 has run; capped
    const inf = d.infer || {};
    const [ilab, icb] = sCheck("Inference", !!s.infer, "An LLM (Bedrock, the department's account) only where the rules are unsure; its place is checked against the map data");
    icb.disabled = !admin;
    icb.addEventListener("change", () => post({ action: "set", key: "infer", value: icb.checked }, icb.checked ? "Inference on" : "Inference off"));
    const irow = el("div", "row");
    const inum = (key, min, max, step, label) => num(s[key] ?? "", min, max, step, (v) => post({ action: "set", key, value: v }), label);
    const model = sInput(s.infer_model || "", 120, "Model id", (v) => post({ action: "set", key: "infer_model", value: v }), "mono");
    model.title = "Bedrock model id (default anthropic.claude-haiku-5-5)";
    const spent = "$" + (inf.usd_month || 0).toFixed(2);
    irow.append(ilab, model,
      el("span", "dim small", "/day"), inum("infer_daily_cap", 0, 10000, 1, "Calls a day at most"),
      el("span", "dim small", "$/month"), inum("infer_budget_usd", 0, 500, 1, "Budget a month (from the tokens each call reports)"),
      el("span", "dim small", "sample %"), inum("infer_sample_pct", 0, 100, 1, "Of rows with a hit: asked and recorded only (evaluation)"),
      el("span", "spacer"),
      el("span", "dim small", (inf.calls_today || 0) + " today · " + spent + " this month"));
    const ist = el("span", inf.ready ? "chip s-state default" : "chip s-state edited", inf.ready ? "ready" : (inf.on ? "held" : "off"));
    if (inf.why) ist.title = inf.why;
    irow.append(ist);
    for (const x of irow.querySelectorAll("input")) x.disabled = !admin;
    kids.push(irow);
    // detection on/off and quiet hours (no pushes in between; the card and the map go on)
    const [elab, ecb] = sCheck("Detection", s.enabled, "Off: no transmission is read for pre-alerts at all");
    ecb.disabled = !admin;
    ecb.addEventListener("change", () => post({ action: "set", key: "enabled", value: ecb.checked }, ecb.checked ? "Intel on" : "Intel off"));
    const hm = (key, label) => {
      const i = el("input", "s-num");
      i.type = "time";
      i.value = s[key] || "";
      i.title = label;
      i.setAttribute("aria-label", label);
      i.disabled = !admin;
      i.addEventListener("change", () => post({ action: "set", key, value: i.value }));
      return i;
    };
    const qrow = el("div", "row");
    qrow.append(elab, el("span", "dim small", "quiet"), hm("quiet_from", "Quiet from (no pushes; empty: none)"),
      el("span", "dim small", "to"), hm("quiet_to", "Quiet until"));
    kids.push(qrow);
    for (const p of d.packs) {
      const ph = el("div", "row");
      ph.append(el("b", "s-eta-l", `${p.label} (${p.name})`),
        modeSel(d.modes, p.mode, (v) => post({ action: "pack", pack: p.name, mode: v }), `${p.label} pack mode`),
        el("span", "dim small", "threshold"), num(p.threshold, 0, 1, 0.05, (v) => post({ action: "pack", pack: p.name, threshold: v }), "Threshold: WHAT at least this"),
        el("span", "dim small", "window s"), num(p.window_s, 10, 1800, 10, (v) => post({ action: "pack", pack: p.name, window_s: v }), "Seconds of one source merged into one candidate"));
      kids.push(ph);
      const cats = el("div", "chips");
      for (const c of p.categories) {
        const box = el("span", "chip");
        box.append(el("span", "", c.label + " "),
          modeSel(d.modes, c.mode, (v) => post({ action: "category", pack: p.name, category: c.name, mode: v }), c.label));
        if (c.edited) box.append(sBtn("↺", "Back to the default", () => post({ action: "reset", what: "category", pack: p.name, category: c.name })));
        cats.append(box);
      }
      kids.push(cats);
      const tbl = el("table", "table small");
      const hr = el("tr");
      for (const h of ["Rule", "Category", "Mode", "Weight", "Hits 7 d", "Events"]) hr.append(el("th", "", h));
      tbl.append(hr);
      for (const r of p.rules) {
        const tr = el("tr");
        const name = el("td", "mono", r.id);
        name.title = `${r.any.join("\n")}${r.not.length ? `\nnot: ${r.not.join(", ")}` : ""}`;
        const w = num(r.w, -1, 1, 0.05, (v) => post({ action: "rule", rule: r.id, w: v }), `${r.id} weight`);
        const td = (...xs) => { const n = el("td"); n.append(...xs); return n; };
        tr.append(name, el("td", "", r.category),
          td(modeSel(d.modes, r.mode, (v) => post({ action: "rule", rule: r.id, mode: v }), r.id)), td(w),
          el("td", "num", String(r.hits || 0)), el("td", "num", String(r.events || 0)));
        tbl.append(tr);
      }
      for (const x of tbl.querySelectorAll("input, select")) x.disabled = !admin;
      for (const x of cats.querySelectorAll("select, button")) x.disabled = !admin;
      for (const x of ph.querySelectorAll("input, select")) x.disabled = !admin;
      kids.push(tbl);
    }
    card.replaceChildren(sCardHead("Intel", s.push ? (s.enabled ? "on" : "detection off") : "pushes off",
      sHelp("Radio pre-alerts: rules over police (and later fire) transcripts. Each event goes off · shadow (counted "
        + "only) · log · map · push, the lowest of its pack, category and rule. Pushes go to the pre-alert (intel) topic "
        + "in Push notifications, else the alerts topic. Push off: detection and the map go on. Hits and events: 7 days. "
        + `Rule set ${d.ruleset_hash}.`)), ...kids);
  }
  return { mount };
})();
