// The emergency alarm (GitHub #128, sizeup/cad/alarms.py): the P25 emergency button, or a mayday / "emergency
// traffic" / "firefighter down" heard on our fire channels. The CAD state carries "alarms": {open, notices,
// recent, drill}; nothing here decides what is an emergency, it only shows what the server raised.
//
//   On /cad and /monitor: a full-screen red overlay over everything, large type, one card per open alarm
//   (further alarms stack): what (the button, the words), who (talker alias, rig, radio ID), the channel, the
//   time, the transcript with the words marked, the units it names, the event it is on, Replay (the clip), and
//   Acknowledge (dispatchers and admins: who and when go on record and into the event's chronology; a viewer
//   sees it and can't). It stays until acknowledged. A drill (Settings > System > Alarms) says DRILL, in amber.
//   A distinct repeating two-tone sound plays while one is open, whatever the talk alerts' chime is set to.
//   Browsers play sound only after a click or key on the page: any one unlocks it, and until then the header
//   and the overlay say "Tap to enable alarm sound".
//   Notices (the button on someone else's talkgroup): a strip under the header, no sound, until acknowledged.
//   The emergency window ("windows": the button's talkgroup and radio kept, Record on or off, until
//   [p25] emergency_window_min after its last key-up): "Recording until 20:09" on the card, the notice, and
//   after the acknowledgement its own line in the strip, each with All clear (dispatchers and admins: resolved,
//   not just seen; the window closes, here and on the station). Acknowledge leaves the window running.
//
//   Alarm.mountSettings(card)   Settings > System: the keywords, the fire channels, other talkgroups, drill
"use strict";

window.Alarm = (() => {
  const st = { ctx: null, timer: null, open: [], notices: [], windows: [], acked: new Set(), cleared: new Set(), seen: null,
               busy: new Set(), playing: null, audio: null, overlay: null, strip: null, pill: null };

  function mk(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function btn(text, cls, fn) {
    const b = mk("button", cls, text);
    b.type = "button";
    b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); fn(b); });
    return b;
  }
  const hms = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "");
  const hm = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : "");
  // the emergency window an alarm opened (its talkgroup's), and how it reads: "Recording until 20:09 (emergency)"
  const windowOf = (a) => st.windows.find((w) => w.alarm_id === a.id)
    || (a.emergency && a.talkgroup !== null && a.talkgroup !== undefined ? st.windows.find((w) => w.talkgroup === a.talkgroup) : null);
  const untilText = (w) => `Recording until ${hm(w.until)} (emergency)`;
  const windowTitle = (w) => `Everything on ${w.channel || `TG ${w.talkgroup}`}${(w.radios || []).length ? `, and radio `
    + `${w.radios.join(", ")} on any talkgroup,` : ""} is kept until ${hm(w.until)}, even with Record off: the emergency `
    + "window. All clear ends it now; Acknowledge doesn't.";
  const dispatcher = () => (typeof canDo === "function" ? canDo("dispatch") : true);
  const signedIn = () => !!(typeof cad !== "undefined" && cad.who && cad.who.email);
  const opName = () => (typeof operator === "function" ? operator() : "");

  // ---- the sound: Web Audio, a two-tone siren (high-low), repeating; unlocked by any click or key ----------

  function ctx() {
    if (!st.ctx) {
      const C = window.AudioContext || window.webkitAudioContext;
      if (C) { try { st.ctx = new C(); } catch (_) { st.ctx = null; } }
    }
    return st.ctx;
  }
  const soundOk = () => !!(st.ctx && st.ctx.state === "running");
  function unlock() {
    const c = ctx();
    if (!c || c.state === "running") return;
    c.resume().then(() => { draw(); sound(); }).catch(() => {});
  }
  document.addEventListener("pointerdown", unlock, true);
  document.addEventListener("keydown", unlock, true);

  function burst() {
    const c = st.ctx;
    if (!c || c.state !== "running") return;
    const t0 = c.currentTime + 0.02;
    for (let i = 0; i < 4; i++) {
      const o = c.createOscillator(), g = c.createGain();
      o.type = "square";
      o.frequency.value = i % 2 ? 740 : 1180;
      const a = t0 + i * 0.32;
      g.gain.setValueAtTime(0, a);
      g.gain.linearRampToValueAtTime(0.18, a + 0.02);
      g.gain.setValueAtTime(0.18, a + 0.27);
      g.gain.linearRampToValueAtTime(0, a + 0.3);
      o.connect(g).connect(c.destination);
      o.start(a);
      o.stop(a + 0.31);
    }
  }
  function sound() {
    const want = st.open.length > 0 && soundOk();
    if (want && !st.timer) {
      burst();
      st.timer = setInterval(burst, 2000);
    } else if (!want && st.timer) {
      clearInterval(st.timer);
      st.timer = null;
    }
  }

  // ---- the overlay ----------------------------------------------------------------------------------------

  function marked(text, spans) {
    const p = mk("p", "alarm-text");
    if (!text) { p.append(mk("span", "dim", "Transcript not in yet")); return p; }
    let k = 0;
    for (const [a, b] of spans || []) {
      if (a < k || b > text.length) continue;
      if (a > k) p.append(text.slice(k, a));
      p.append(mk("mark", "alarm-mark", text.slice(a, b)));
      k = b;
    }
    if (k < text.length) p.append(text.slice(k));
    return p;
  }

  async function ack(a, b) {
    if (!signedIn() && !opName()) {
      UI.toast("Enter your name (Operator, top right) to acknowledge", "alert");
      return;
    }
    st.busy.add(a.id);
    b.disabled = true;
    try {
      await api("POST", `/api/cad/alarm/${a.id}/ack`, { operator: opName() });
      st.acked.add(a.id);
      st.open = st.open.filter((x) => x.id !== a.id);
      st.notices = st.notices.filter((x) => x.id !== a.id);
      draw();
      sound();
    } catch (e) {
      UI.toast(`Not acknowledged: ${e.message}`, "alert");
      b.disabled = false;
    } finally {
      st.busy.delete(a.id);
    }
  }

  // All clear: resolved ("things are ok"), not just seen: the emergency window closes (acknowledged too)
  async function allClear(a, b) {
    if (!signedIn() && !opName()) {
      UI.toast("Enter your name (Operator, top right) to give the all clear", "alert");
      return;
    }
    st.busy.add(a.id);
    b.disabled = true;
    try {
      await api("POST", `/api/cad/alarm/${a.id}/clear`, { operator: opName() });
      st.acked.add(a.id);
      st.cleared.add(a.id);
      st.open = st.open.filter((x) => x.id !== a.id);
      st.notices = st.notices.filter((x) => x.id !== a.id);
      st.windows = st.windows.filter((w) => w.alarm_id !== a.id);
      draw();
      sound();
    } catch (e) {
      UI.toast(`No all clear: ${e.message}`, "alert");
      b.disabled = false;
    } finally {
      st.busy.delete(a.id);
    }
  }
  function clearButton(a, cls) {
    const b = btn("All clear", cls, (x) => allClear(a, x));
    b.title = "Resolved: ends the emergency window now (its talkgroup and radio are no longer kept with Record off). "
      + "Acknowledge only says it was seen.";
    b.disabled = st.busy.has(a.id);
    return b;
  }

  function replay(a, b) {
    if (st.playing === a.id && st.audio) {
      st.audio.pause();
      st.playing = null;
      b.textContent = "Replay";
      return;
    }
    if (st.audio) st.audio.pause();
    st.audio = new Audio(`/api/audio/${encodeURIComponent(a.traffic_id)}.wav`);
    st.playing = a.id;
    b.textContent = "Stop";
    st.audio.addEventListener("ended", () => { st.playing = null; b.textContent = "Replay"; });
    st.audio.play().catch((e) => { st.playing = null; b.textContent = "Replay"; UI.toast(`Can't play: ${e.message}`, "alert"); });
  }

  function card(a) {
    const c = mk("article", `alarm-card${a.drill ? " drill" : ""}`);
    c.setAttribute("aria-label", `${a.drill ? "Drill: " : ""}${a.what} ${a.who} on ${a.channel}`);
    const head = mk("div", "alarm-what");
    if (a.drill) head.append(mk("span", "alarm-drill", "DRILL"));
    head.append(mk("span", "", a.what.toUpperCase()));
    c.append(head);
    const who = mk("div", "alarm-who");
    who.append(mk("b", "", a.who));
    const tk = a.talker || {};
    const unit = (tk.units || [])[0];
    if (unit && unit[1]) who.append(mk("span", "", ` · ${unit[1]}`));
    if (a.radio_id) who.append(mk("span", "", ` · radio ${a.radio_id}`));
    c.append(who);
    c.append(mk("div", "alarm-where", `${a.channel}${a.talkgroup !== null && a.talkgroup !== undefined ? ` (TG ${a.talkgroup})` : ""} · ${hms(a.at)}`));
    const w = windowOf(a);
    if (w) {
      const r = mk("div", "alarm-meta alarm-window", untilText(w));
      r.title = windowTitle(w);
      c.append(r);
    }
    c.append(marked(a.text, a.spans));
    if ((a.named || []).length) c.append(mk("div", "alarm-meta", `Units named: ${a.named.join(", ")}`));
    if (a.incident_id) {
      const ev = `${a.incident_number || `Event ${a.incident_id}`}${a.incident_address ? ` · ${a.incident_address}` : ""}`;
      if (!BOARD && typeof selectIncident === "function") {
        c.append(btn(`On ${ev}`, "nav alarm-ev", () => selectIncident(a.incident_id)));
      } else {
        c.append(mk("div", "alarm-meta", `On ${ev}`));
      }
    } else {
      c.append(mk("div", "alarm-meta", "Not on an open event"));
    }
    const row = mk("div", "alarm-btns");
    if (a.has_clip) row.append(btn(st.playing === a.id ? "Stop" : "Replay", "lg", (b) => replay(a, b)));
    if (dispatcher()) {
      const b = btn("Acknowledge", "lg primary alarm-ack", (x) => ack(a, x));
      b.disabled = st.busy.has(a.id);
      row.append(b);
      if (a.emergency) row.append(clearButton(a, "lg alarm-clear"));
    } else {
      row.append(mk("span", "alarm-meta", "Dispatchers acknowledge it (you can't: viewer)"));
    }
    c.append(row);
    return c;
  }

  function soundButton() {
    return btn("Tap to enable alarm sound", "lg alarm-unlock", () => { unlock(); });
  }

  function draw() {
    if (!st.overlay) {
      st.overlay = mk("div", "alarm-overlay");
      st.overlay.setAttribute("role", "alertdialog");
      st.overlay.setAttribute("aria-modal", "true");
      st.overlay.setAttribute("aria-label", "Emergency alarm");
      st.overlay.hidden = true;
      document.body.append(st.overlay);
    }
    const open = st.open;
    st.overlay.hidden = !open.length;
    st.overlay.classList.toggle("drill", open.length > 0 && open.every((a) => a.drill));
    if (open.length) {
      const sig = JSON.stringify([open.map((a) => [a.id, a.text, a.incident_id, a.has_clip, a.what, a.named, st.busy.has(a.id),
        (windowOf(a) || {}).until]), soundOk(), dispatcher(), st.playing]);
      if (st.overlay._sig !== sig) {
        st.overlay._sig = sig;
        const top = mk("div", "alarm-head");
        const allDrill = open.every((a) => a.drill);
        top.append(mk("h1", "alarm-title", allDrill ? "DRILL — EMERGENCY" : "EMERGENCY"));
        if (open.length > 1) top.append(mk("span", "alarm-count", `${open.length} open`));
        if (allDrill) top.append(mk("span", "alarm-meta", "Training: not a real emergency"));
        if (!soundOk()) top.append(soundButton());
        const list = mk("div", "alarm-list");
        list.append(...open.slice().reverse().map(card));        // the newest on top
        st.overlay.replaceChildren(top, list);
      }
    }
    // the notices: a strip, no sound
    if (!st.strip) {
      st.strip = mk("div", "alarm-notices");
      st.strip.setAttribute("role", "status");
      st.strip.hidden = true;
      document.body.append(st.strip);
    }
    const ns = st.notices;
    // windows still running whose alarm isn't on screen any more (acknowledged): a line of their own
    const shownIds = new Set([...open, ...ns].map((a) => a.id));
    const ws = st.windows.filter((w) => !shownIds.has(w.alarm_id) && !ns.some((a) => windowOf(a) === w));
    st.strip.hidden = (!ns.length && !ws.length) || open.length > 0;
    const nsig = JSON.stringify([ns.map((a) => [a.id, a.text, a.stuck, st.busy.has(a.id), (windowOf(a) || {}).until]),
      ws.map((w) => [w.talkgroup, w.until, w.alarm_id, st.busy.has(w.alarm_id)]), dispatcher()]);
    if (st.strip._sig !== nsig) {
      st.strip._sig = nsig;
      st.strip.replaceChildren(...ns.map((a) => {
        const r = mk("div", "alarm-notice");
        r.append(mk("b", "", `${a.drill ? "DRILL · " : ""}Emergency button (not ours)`),
          mk("span", "", ` ${a.who} on ${a.channel} at ${hms(a.at)}${a.text ? `: “${a.text}”` : ""}`));
        if (a.stuck) {                // #190: it went on flagging routine calls: said once, its later flags not alarms
          const s = mk("span", "alarm-stuck", ` · emergency flag stuck on ${a.stuck}`);
          s.title = "The same flag on call after call of routine traffic: a radio left in emergency mode, or a talkgroup "
            + "the system holds in emergency. Its later flags are not emergencies: no new alarm, the window not renewed.";
          r.append(s);
        }
        const w = windowOf(a);
        if (w) {
          const u = mk("span", "alarm-window", ` · ${untilText(w)}`);
          u.title = windowTitle(w);
          r.append(u);
        }
        r.append(mk("span", "spacer"));
        if (dispatcher()) {
          r.append(btn("Acknowledge", "sm", (b) => ack(a, b)));
          if (a.emergency) r.append(clearButton(a, "sm"));
        }
        return r;
      }), ...ws.map((w) => {
        const r = mk("div", "alarm-notice alarm-window-row");
        r.title = windowTitle(w);
        r.append(mk("b", "alarm-window", "Emergency window"),
          mk("span", "", ` ${w.channel || `TG ${w.talkgroup}`}${w.radios && w.radios.length ? ` · radio ${w.radios.join(", ")}` : ""}`
            + ` · ${untilText(w)}`), mk("span", "spacer"));
        if (dispatcher() && w.alarm_id) r.append(clearButton({ id: w.alarm_id }, "sm"));
        return r;
      }));
    }
    // the header: sound not unlocked yet (the wall board especially: nobody has touched it)
    const hdr = document.querySelector("body > header");
    if (hdr && !st.pill) {
      st.pill = btn("Alarm sound: tap to enable", "pill warn alarm-pill", () => unlock());
      st.pill.title = "Browsers play sound only after a click: this enables the emergency alarm's sound on this screen";
      const conn = hdr.querySelector("#conn");
      if (conn) conn.after(st.pill); else hdr.append(st.pill);
    }
    if (st.pill) st.pill.hidden = soundOk() || !ctx();
  }

  function update() {
    const s = typeof cad !== "undefined" && cad.state && cad.state.alarms;
    if (!s) return;
    const open = (s.open || []).filter((a) => !st.acked.has(a.id));
    const fresh = st.seen ? open.filter((a) => !st.seen.has(a.id)) : [];
    st.open = open;
    st.notices = (s.notices || []).filter((a) => !st.acked.has(a.id));
    st.windows = (s.windows || []).filter((w) => !st.cleared.has(w.alarm_id));
    for (const a of s.recent || []) {
      if (st.seen && st.seen.has(a.id) && !st.acked.has(a.id)) {
        UI.toast(`Alarm acknowledged by ${(a.ack_by || "").replace(/\s*<[^>]*>\s*$/, "")}`, "ok",
          `${a.what} · ${a.who} on ${a.channel} at ${hms(a.at)}`);
      }
      st.acked.add(a.id);
    }
    for (const a of st.notices) {
      if (st.seen && !st.seen.has(a.id)) UI.toast(`Emergency button: ${a.who} on ${a.channel}`, "alert", "Not our channel or rig: a notice");
    }
    st.seen = new Set([...open, ...st.notices, ...(s.recent || [])].map((a) => a.id));
    if (fresh.length && st.overlay) st.overlay._sig = "";
    draw();
    sound();
  }

  if (typeof cad !== "undefined" && Array.isArray(cad.hooks)) cad.hooks.push(update);

  // ---- Settings > System > Alarms -------------------------------------------------------------------------

  async function mountSettings(card) {
    if (!card) return;
    let d;
    try {
      d = (await api("GET", "/api/cad/alarms")).settings;
    } catch (e) {
      card.replaceChildren(mk("p", "error", `Alarms: ${e.message}`));
      return;
    }
    // sPost (settings.js): the operator, the audit, then Settings reloads (and mounts this card again)
    const save = (key, value) => sPost("alarms", { action: "set", key, value }, "Saved: emergency alarms");
    const reset = (key) => sPost("alarms", { action: "reset", key }, "Emergency alarms: back to the default");
    const edited = (k) => (d.edited || []).includes(k);
    const head = mk("div", "card-head");
    head.append(mk("h2", "", "Emergency alarms"),
      ...(window.Help ? [Help.button("The P25 emergency button and the words below on our fire channels raise a full-screen "
        + "alarm with sound on every console and wall board until a dispatcher acknowledges it (who and when go on the "
        + "event's chronology). The button on another agency's talkgroup, by a radio not ours, is a notice. "
        + "\"...\" in a phrase: up to " + d.gap_words + " words in between that don't say no.")] : []),
      mk("span", "dim small", d.push ? "Pushed to the alerts topic (ntfy)" : "No push topic ([alerts] ntfy_topic)"));
    // drill
    const drill = mk("label", "check");
    const dIn = mk("input");
    dIn.type = "checkbox";
    dIn.checked = !!d.drill;
    dIn.addEventListener("change", () => save("drill", dIn.checked));
    drill.append(dIn, mk("span", "", "Drill mode: alarms say DRILL (training), pushes at normal priority"));
    // other talkgroups
    const other = mk("select", "field");
    other.setAttribute("aria-label", "Emergency button on other talkgroups");
    for (const [v, t] of [["notice", "A notice (no sound)"], ["alarm", "The full alarm"], ["off", "Nothing"]]) {
      const o = mk("option", "", t);
      o.value = v;
      other.append(o);
    }
    other.value = d.other;
    other.addEventListener("change", () => save("other", other.value));
    const oRow = mk("div", "row");
    oRow.append(mk("span", "", "Emergency button on another agency's talkgroup, radio not ours:"), other);
    // channels
    const ch = mk("input", "field alarm-chans");
    ch.type = "text";
    ch.value = (d.channels || []).join(", ");
    ch.setAttribute("aria-label", "Fire channels");
    const chRow = mk("div", "row");
    chRow.append(mk("span", "", "Fire channels (words checked here):"), ch,
      btn("Save", "sm", () => save("channels", ch.value.split(",").map((x) => x.trim()).filter(Boolean))),
      ...(edited("channels") ? [btn("Default", "sm quiet", () => reset("channels"))] : []),
      mk("span", "dim small", `Default ${(d.defaults.channels || []).join(", ")}`));
    // keywords
    const kw = mk("textarea", "field alarm-kw");
    kw.rows = 10;
    kw.value = (d.keywords || []).join("\n");
    kw.setAttribute("aria-label", "Alarm words, one phrase a line");
    const kRow = mk("div", "row");
    kRow.append(btn("Save words", "sm", () => save("keywords", kw.value.split("\n").map((x) => x.trim()).filter(Boolean))),
      ...(edited("keywords") ? [btn("Default", "sm quiet", () => reset("keywords"))] : []),
      mk("span", "dim small", "One phrase a line, whole words, any case"));
    card.replaceChildren(head, drill, oRow, chRow, kw, kRow);
  }

  return { mountSettings, update };
})();
