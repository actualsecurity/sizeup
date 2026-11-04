// Airport pre-alerts (sizeup/cad/airportalert.py): something at or near HPN that could become a call for us before
// anyone calls it in. One kind, one component, for every trigger: an emergency squawk on ADS-B (#235), HPN ATC (#233).
// The server's state carries "airport_prealert": {kind, time, open, items} (null on a station: the hub has them).
//
//   AirportPreAlert.column(list, ap, alarms, deps)  the open ones at the top of the queue (console) or above the feed
//                                                   (wall board, no Dismiss); alarms: the state's alarms, so an item
//                                                   with an alarm says whether it is still open (one event, shown
//                                                   once); deps: {api, operator, time, readonly}
//   AirportPreAlert.notify(ap)                      a toast and the chime per new one (radio.js alert settings)
"use strict";

window.AirportPreAlert = (() => {
  const st = { skew: 0, seen: null, busy: new Set(), open: new Set() };
  const TRIGGER = { adsb: "ADS-B", atc: "HPN ATC" };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function age(at) {
    const s = Math.max(0, Math.floor(Date.now() / 1000 + st.skew - at));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  }
  setInterval(() => {
    for (const n of document.querySelectorAll("[data-ap-at]")) n.textContent = age(Number(n.dataset.apAt));
  }, 1000);

  const openOf = (ap) => (ap && ap.open) || [];

  function alarmWord(p, alarms) {
    if (!p.alarm_id || !alarms) return "";
    if ((alarms.open || []).some((a) => a.id === p.alarm_id)) return "Alarm open: acknowledge it on the alarm";
    const done = (alarms.recent || []).find((a) => a.id === p.alarm_id);
    return done && done.ack_by ? `Alarm acknowledged by ${done.ack_by}` : "Alarm acknowledged";
  }

  function column(list, ap, alarms, deps) {
    if (!list) return;
    if (ap && typeof ap.time === "number") st.skew = ap.time - Date.now() / 1000;
    const items = openOf(ap);
    list.hidden = !items.length;
    const sig = JSON.stringify(items.map((p) => [p.id, p.updated_at, p.title, p.lines, alarmWord(p, alarms), st.busy.has(p.id),
                                                 st.open.has(p.id)]));
    if (list._sig === sig) return;
    list._sig = sig;
    list.replaceChildren(...items.map((p) => row(p, alarms, deps)));
  }

  function row(p, alarms, deps) {
    const li = el("li", "pa ap" + (p.drill ? " drill" : ""));
    li.dataset.key = String(p.id);
    li.title = [p.summary, p.incident_number ? `On ${p.incident_number}` : ""].filter(Boolean).join("\n");
    if (window.Icon) li.append(Icon(p.trigger === "adsb" ? "aircraft" : "bell"));
    const what = el("div", "pa-what ap-what");
    what.append(el("b", "pa-t ap-title", (p.drill ? "DRILL · " : "") + p.title),
                el("span", "pa-src", [`${TRIGGER[p.trigger] || p.trigger} pre-alert`, p.source, deps.time(p.at),
                                      p.incident_number ? `on ${p.incident_number}` : ""].filter(Boolean).join(" · ")));
    const aw = alarmWord(p, alarms);
    if (aw) what.append(el("span", "pa-src ap-alarm", aw));
    const lines = el("dl", "ap-lines");
    lines.hidden = !st.open.has(p.id) && !deps.readonly;
    for (const [k, v] of p.lines || []) lines.append(el("dt", "", k), el("dd", "", v));
    what.append(lines);
    const ageN = el("span", "pa-age");
    ageN.dataset.apAt = String(p.at);
    ageN.textContent = age(p.at);
    li.append(what, ageN);
    if (deps.readonly) return li;
    const more = el("button", "mini ap-more", st.open.has(p.id) ? "Less" : "Details");
    more.type = "button";
    more.setAttribute("aria-expanded", st.open.has(p.id) ? "true" : "false");
    more.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (st.open.has(p.id)) st.open.delete(p.id); else st.open.add(p.id);
      lines.hidden = !st.open.has(p.id);
      more.textContent = st.open.has(p.id) ? "Less" : "Details";
      more.setAttribute("aria-expanded", st.open.has(p.id) ? "true" : "false");
    });
    const b = el("button", "mini pa-dismiss", st.busy.has(p.id) ? "…" : "Dismiss");
    b.type = "button";
    b.disabled = st.busy.has(p.id);
    b.title = "Nothing for us: take it off the list (logged)";
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      st.busy.add(p.id);
      b.disabled = true;
      try {
        await deps.api("POST", `/api/cad/airport-prealert/${p.id}/dismiss`, { operator: deps.operator() });
      } catch (err) {
        UI.toast(`Dismiss failed: ${err.message}`, "err");
      } finally {
        st.busy.delete(p.id);
      }
    });
    const tools = el("div", "ap-tools");
    tools.append(more, b);
    li.append(tools);
    return li;
  }

  function notify(ap) {
    const items = openOf(ap);
    const first = st.seen === null;
    if (first) st.seen = new Set();
    for (const p of items) {
      if (st.seen.has(p.id)) continue;
      st.seen.add(p.id);
      if (!first && window.Radio && Radio.prealert) Radio.prealert((p.drill ? "DRILL: " : "") + "Airport pre-alert", p.title);
    }
    if (st.seen.size > 200) st.seen = new Set(items.map((p) => p.id));
  }

  return { column, notify };
})();
