// Airport pre-alerts (sizeup/cad/airport_alerts.py, #233): something heard on HPN ATC (sizeup/atc.py), or seen in an
// aircraft's own data (#235), that could become a PCFD response. The server's state carries "airport_alerts":
// {open, items, expire_s}. One card per open pre-alert at the top of the 60 Control column (the wall board:
// above the feed, nothing to press):
//
//   title      "HPN ATC: possible brush fire reported off runway 16 departure", the confidence (high / medium / low)
//   heard      each transmission ("context": heard just before the one that matched), with ▶ for its audio
//   aircraft   the callsign, type, distance and phase from the Aircraft layer (or "heard, not on ADS-B")
//   location   the words heard, and the approximate point's reason (always marked approximate) or "not placed"
//   Accept     a new PENDING event (never a dispatch: a person dispatches from it); Dismiss: nothing to it
//
//   details    (#235, an aircraft emergency on ADS-B) what is known: aircraft, operator, emergency, altitude,
//              position, phase, source, time; and its emergency alarm's state (open / acknowledged by): one event,
//              shown once; DRILL in drill mode
//
//   AirportAlerts.column(list, state, deps)   deps: {api, operator, time, readonly, select(id), alarms: the state's}
//   AirportAlerts.notify(state)               a toast (and the chime) per new pre-alert (radio.js's prealert setting)
"use strict";

window.AirportAlerts = (() => {
  const st = { seen: null, busy: new Set(), audio: null, playing: "" };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const open = (s) => (s && s.open) || [];

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

  function aircraftLine(x) {
    const parts = [x.callsign || x.spoken || "aircraft"];
    if (x.type || x.model) parts[0] += ` (${x.type || x.model})`;
    if (x.dist !== undefined && x.dist !== null) parts.push(`${x.dist} NM`);
    if (typeof x.alt === "number") parts.push(`${x.alt.toLocaleString()} ft`);
    if (x.phase) parts.push(x.phase + (x.rwy ? ` ${x.rwy}` : ""));
    parts.push(x.hex ? `ADS-B ${Math.round((x.match || 0) * 100)}%` : "heard, not on ADS-B");
    return parts.join(" · ");
  }

  function alarmWord(d, alarms) {
    if (!d.alarm_id || !alarms) return "";
    if ((alarms.open || []).some((x) => x.id === d.alarm_id)) return "Alarm open · ack on the alarm";
    const done = (alarms.recent || []).find((x) => x.id === d.alarm_id);
    return done && done.ack_by ? `Alarm acknowledged by ${done.ack_by}` : "Alarm acknowledged";
  }

  function card(a, deps) {
    const d = a.data || {};
    const li = el("li", `aa aa-${a.level}` + (d.drill ? " aa-drill" : ""));
    li.dataset.key = String(a.id);
    const head = el("div", "aa-head");
    if (window.Icon) head.append(Icon("aircraft"));
    head.append(el("b", "aa-title", (d.drill ? "DRILL · " : "") + a.title),
                el("span", `aa-conf ${a.level}`, a.source === "adsb" ? "ADS-B" : `${a.level} ${Math.round(a.confidence * 100)}%`));
    li.append(head);
    const aw = alarmWord(d, deps.alarms);
    if (aw) li.append(el("div", "aa-alarm", aw));
    if ((d.lines || []).length) {
      const dl = el("dl", "aa-lines");
      for (const [k, v] of d.lines) dl.append(el("dt", "", k), el("dd", "", v));
      li.append(dl);
    }
    li.append(el("div", "aa-src", `${a.source_name}${a.channel ? ` · ${a.channel}` : ""} · ${deps.time(a.at)}`
                 + (a.count > 1 ? ` · ${a.count} tx` : "")));
    const heard = el("ol", "aa-heard");
    for (const s of d.snippets || []) {
      const r = el("li", s.context ? "aa-snip ctx" : "aa-snip");
      if (s.id) {
        const b = el("button", "mini play", st.playing === s.id ? "■" : "▶");
        b.type = "button";
        b.title = "Play this transmission (while its audio is kept)";
        b.addEventListener("click", (ev) => { ev.stopPropagation(); play(s.id, b); });
        r.append(b);
      }
      r.append(el("span", "aa-t", deps.time(s.at)), el("q", "aa-q", s.text));
      heard.append(r);
    }
    if (heard.children.length) li.append(heard);
    if (!(d.lines || []).length) for (const x of d.aircraft || []) li.append(el("div", "aa-acft", aircraftLine(x)));
    const loc = d.location;
    if (loc && loc.basis) {
      const placed = loc.lat !== undefined && loc.lat !== null;
      const n = el("div", "aa-loc" + (placed ? " approx" : ""), (placed ? "Approximate: " : "Location: ") + loc.basis);
      n.title = placed ? `About ${loc.lat}, ${loc.lon} within ${loc.radius_m} m: from the words, not a fix` : "Not placed on the map";
      li.append(n);
    } else if ((d.phrases || []).length) {
      li.append(el("div", "aa-loc", "Heard: " + d.phrases.join("; ")));
    }
    if (deps.readonly) return li;
    const row = el("div", "aa-btns");
    const busy = st.busy.has(a.id);
    const acc = el("button", "mini primary", busy ? "…" : "Accept");
    acc.type = "button";
    acc.title = "Make a pending event from it (nothing is dispatched)";
    const dis = el("button", "mini pa-dismiss", busy ? "…" : "Dismiss");
    dis.type = "button";
    dis.title = "Nothing to it: take it off the list (logged)";
    acc.disabled = dis.disabled = busy;
    const act = (what) => async (ev) => {
      ev.stopPropagation();
      st.busy.add(a.id);
      acc.disabled = dis.disabled = true;
      try {
        const r = await deps.api("POST", `/api/cad/airport-alert/${a.id}/${what}`, { operator: deps.operator() });
        if (what === "accept" && r && r.incident) {
          UI.toast(`Event ${r.incident.number || r.incident.id} pending · not dispatched`, "ok");
          if (deps.select) deps.select(r.incident.id);
        }
      } catch (err) {
        UI.toast(`${what === "accept" ? "Accept" : "Dismiss"} failed: ${err.message}`, "err");
      } finally {
        st.busy.delete(a.id);
      }
    };
    acc.addEventListener("click", act("accept"));
    dis.addEventListener("click", act("dismiss"));
    row.append(acc, dis);
    li.append(row);
    return li;
  }

  function column(list, s, deps) {
    if (!list) return;
    const items = open(s);
    list.hidden = !items.length;
    const sig = JSON.stringify(items.map((a) => [a.id, a.title, a.count, a.confidence, st.busy.has(a.id), st.playing,
                                                 (a.data || {}).lines, alarmWord(a.data || {}, deps.alarms)]));
    if (list._sig === sig) return;
    list._sig = sig;
    list.replaceChildren(...items.map((a) => card(a, deps)));
  }

  function notify(s) {
    const items = open(s);
    const first = st.seen === null;
    if (first) st.seen = new Set();
    for (const a of items) {
      if (st.seen.has(a.id)) continue;
      st.seen.add(a.id);
      if (!first && window.Radio && Radio.prealert) Radio.prealert(a.title, "Airport pre-alert · Accept or Dismiss");
    }
    if (st.seen.size > 200) st.seen = new Set(items.map((a) => a.id));
  }

  return { column, notify };
})();
