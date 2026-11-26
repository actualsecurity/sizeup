// A pre-alert's More info page (/cad#prealert/<kind>/<id>; kind airport | intel): its context bundle (GET
// /api/cad/prealerts/<kind>/<id>, sizeup/context_bundle.py), the same data the inference layer reads. Why it fired,
// the transmissions with their audio (cad.js radioRow: the CAD's own row and player; "Audio expired" once the hub
// let it go), the channels around it, the place on a small map, site history, what is open near it now, the outcome.
// Terse: the reasons are in the hovers; a section with nothing says why in one line.
//
//   PrealertInfo.href(kind, id)   "#prealert/intel/12" (the cards' More info; the push's link: context_bundle.page_url)
//   PrealertInfo.open(rest)       route() with what follows "#prealert/"; close() when the hash moves on; reload()
//                                 its Refresh (cad.js)
//   PrealertInfo.render(bundle)   idempotent: a section is redrawn only when its data changed (scroll, the map's view
//                                 and what is playing are kept), so a later PR can poll the bundle's cursor or stream
//                                 the event's updates into #pi-updates (above the bundle) and call it again
//
// Uses cad.js's helpers (el, $, show, api, radioRow, fmtTime, openFromRecords, cad.scene) and map-style.js's
// buildMapStyle: loaded after them. Its classes are .pi-* (.sv-* is Search's, .stv-* street view's).
"use strict";

window.PrealertInfo = (() => {
  const KINDS = ["airport", "intel"];
  const st = { key: "", seq: 0, data: null, sigs: {}, map: null, mapReady: false, framed: "", waiting: 0 };
  const href = (kind, id) => `#prealert/${kind}/${id}`;
  function parse(rest) {
    const m = /^(airport|intel)\/(\d{1,9})$/.exec(String(rest || "").replace(/[?#].*$/, ""));
    return m ? { kind: m[1], id: Number(m[2]) } : null;
  }
  const tz = () => (window.cad && cad.scene && cad.scene.timezone) || "America/New_York";
  const day = (unix) => (unix ? new Intl.DateTimeFormat("en-US", { timeZone: tz(), year: "2-digit", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(unix * 1000)).replace(",", "") : "");
  const tm = (unix) => (typeof fmtTime === "function" ? fmtTime(unix, true) : day(unix));
  const pct = (x) => `${Math.round((x || 0) * 100)}%`;
  const STATUS = { open: "Open", accepted: "Accepted", dismissed: "Dismissed", expired: "Expired", ended: "Ended" };

  // ---- open / close (cad.js route) ---------------------------------------------------------------------------------
  async function open(rest) {
    const p = parse(rest);
    const key = p ? `${p.kind}/${p.id}` : "";
    if (key !== st.key) { st.key = key; st.data = null; st.sigs = {}; st.framed = ""; clear(); }
    if (window.IntelNotes) IntelNotes.mount($("#pi-notes"), p && p.kind === "intel" ? p.id : null);   // read on a new id only
    if (!p) return fail("No such pre-alert");
    const seq = ++st.seq;
    try {
      const d = await api("GET", `/api/cad/prealerts/${p.kind}/${p.id}`);
      if (seq === st.seq) render(d);
    } catch (e) {
      if (seq === st.seq) fail(e.message);
    }
  }
  function close() { st.seq++; }
  function reload() { if (st.key) open(st.key); }

  function fail(msg) {
    const n = $("#pi-error");
    n.textContent = msg;
    show(n, true);
  }
  function clear() {
    for (const id of SECTIONS) { const n = $(`#${id}`); if (n) { n.replaceChildren(); show(n, false); } }
    setTextIf($("#pi-title"), "");
    $("#pi-chips").replaceChildren();
    show($("#pi-error"), false);
  }
  function setTextIf(n, t) { if (n && n.textContent !== t) n.textContent = t; }

  // ---- building blocks ---------------------------------------------------------------------------------------------
  function head(title, extra, tip) {
    const h = el("div", "sec-head");
    const t = el("h3", "", title);
    if (tip) t.title = tip;
    h.append(t);
    if (extra) h.append(el("span", "dim small", extra));
    return h;
  }
  const none = (why) => el("div", "pi-none dim small", why || "None");
  function dl(rows) {
    const d = el("dl", "pi-dl");
    for (const [k, v, tip] of rows) {
      if (v === "" || v === null || v === undefined) continue;
      const dt = el("dt", "", k), dd = el("dd");
      if (tip) dd.title = tip;
      if (Array.isArray(v)) dd.append(...v); else dd.append(v);
      d.append(dt, dd);
    }
    return d;
  }
  function chips(words, cls = "chip") { return words.map((w) => el("span", cls, w)); }
  function incLink(inc, tip) {
    const b = el("button", "mini pi-inc", `${inc.number || `#${inc.id}`} · ${inc.type || "?"}${inc.subtype ? `/${inc.subtype}` : ""}`);
    b.type = "button";
    b.title = tip || `${inc.address || ""}\nOpen the event`.trim();
    b.addEventListener("click", () => { if (typeof openFromRecords === "function") openFromRecords(inc.id); });
    return b;
  }
  // a transmission: the CAD's own row (play, or "Audio expired"; time, unit, channel, the whole transcript)
  // scope: the list's own ("pi-heard", "pi-around"): one transmission in both lists keeps a menu each (txMenu's key)
  function tx(t, scope) {
    const row = { kind: "radio", start: t.start, end: t.end, ...t };
    const li = typeof radioRow === "function" ? radioRow(row, { scope }) : el("li", "rt", t.shown || t.text);
    li.classList.add("pi-tx");
    if (t.evidence) li.classList.add("pi-ev");
    if (t.context) { li.classList.add("pi-ctx"); li.title = "Heard just before the one that matched"; }
    if ((t.matched || []).length) {
      const m = el("div", "pi-hit small", `Matched: ${t.matched.join(" · ")}`);
      m.title = "The words (or the rule's phrases) that matched in this transmission";
      li.append(m);
    }
    if ((t.rules || []).length) {
      const r = el("div", "pi-rules dim small", t.rules.map((x) => `${x.id} ${x.weight}`).join(" · "));
      r.title = "Rules that hit this transmission, with their weights";
      li.append(r);
    }
    if (t.kept) li.append(el("div", "pi-kept dim small", t.kept));
    return li;
  }
  // newest at the top, as every console list (the bundle's own order is oldest first): by air start; a row without
  // one (gone from the radio log) stays next to the one it followed
  function newestFirst(items) {
    let last = -Infinity;
    const keyed = items.map((t, i) => {
      if (typeof t.start === "number") last = t.start;
      return { t, i, at: last };
    });
    return keyed.sort((a, b) => b.at - a.at || b.i - a.i).map((x) => x.t);
  }
  function txList(items, scope) {
    const ol = el("ol", "list log radio-log pi-txs");
    ol.append(...newestFirst(items).map((t) => tx(t, scope)));
    return ol;
  }

  // ---- the sections ------------------------------------------------------------------------------------------------
  const SECTIONS = ["pi-why", "pi-trigger", "pi-around", "pi-site", "pi-place", "pi-now", "pi-outcome"];

  // the local model's answer (sizeup/intel/backing.py) when it raised or noted this event: what, how sure, why
  function localModel(m) {
    if (!m) return "";
    // the reason may be cut at its maxLength (120) mid-word by the hub: shown with an ellipsis, never parsed
    const reason = m.reason && m.reason.length >= 120 ? `${m.reason}…` : m.reason;
    return [m.category, pct(m.confidence), m.used === "evidence" ? "evidence only" : "", reason].filter((x) => x).join(" · ");
  }
  function why(d) {
    const w = d.why, s = d.summary;
    const rules = (w.rules || []).map((r) => {
      const c = el("span", "chip", r.weight !== undefined && r.weight !== null ? `${r.id} ${r.weight}` : r.id);
      if (r.rev) c.title = `Rule ${r.id}, revision ${r.rev}, weight ${r.weight}`;
      return c;
    });
    const score = d.event.kind === "intel"
      ? `what ${pct(w.score.what)} · where ${pct(w.score.where)} (${w.score.precision}${w.score.confirmed ? "" : ", unconfirmed"})`
      : `${w.score.level} ${pct(w.score.confidence)}` + (w.score.pushed !== undefined && w.score.pushed !== null ? ` · pushed at ${pct(w.score.pushed)}` : "");
    const kids = [head("Why"), dl([
      [d.event.kind === "intel" ? "Rules" : "Kinds", rules.length ? rules : "", w.ruleset_hash ? `Rule set ${w.ruleset_hash}` : ""],
      ["Matched", (w.matched || []).length ? [...chips(w.matched, "chip pi-word"), ...(w.rules_changed ? [el("span", "warn small", "rules changed")] : [])]
        : (w.phrases_hidden ? "rule phrases: admins" : ""),
        w.phrases_hidden ? "The rules' phrases are for admins (Settings > System > Intel)"
          : w.matched_from ? `Phrases found again with the ${w.matched_from}` + (w.rules_changed ? `; the rules changed since it fired (rule set ${w.ruleset_hash})` : "")
          : "Words heard that the rules matched"],
      ["Score", score, d.event.kind === "intel" ? "WHAT: how sure it is this kind of call; WHERE: how sure the place is" : "The pre-alert's own confidence (not the aircraft match's)"],
      ["Mode", s.mode || "", "off · shadow (counted only) · log · map · push"],
      ["Model", s.suggested ? `${s.suggested}?` : "", "The model's reading, not the rules': never pushed"],
      ["Local model", localModel(w.model), w.model ? `${w.model.backend}: ${w.model.model || "?"} ${w.model.model_version || ""}`.trim()
        + (w.model.weapon && w.model.weapon !== "none" ? ` · weapon ${w.model.weapon}` : "") + (w.model.in_progress ? " · in progress" : "") : ""],
    ])];
    if (!rules.length && !(w.matched || []).length && w.empty) kids.push(none(w.empty));
    if ((s.lines || []).length) kids.push(dl(s.lines.map(([k, v]) => [k, v])));
    return kids;
  }
  function trigger(d) {
    const t = d.trigger;
    return [head("Heard", `${t.items.length} tx`, "The transmissions that raised it"), t.items.length ? txList(t.items, "pi-heard") : none(t.empty)];
  }
  function around(d) {
    const b = d.before_after;
    const span = `±${Math.round((b.window_s || 0) / 60)} min` + ((b.channels || []).length ? ` · ${b.channels.join(", ")}` : "")
      + ((b.related_channels || []).length ? ` + ${b.related_channels.join(", ")}` : "")
      + (b.capped && (b.capped.before || b.capped.after) ? " · nearest 30 each side" : "");
    return [head("Timeline", span, "Everything on the same channels (and their talkgroup group) around it; the ones that raised it marked"),
      ...others(d.siblings), b.items.length ? txList(b.items, "pi-around") : none(b.empty)];
  }
  // the other calls on its source in its window (intel: context_bundle siblings): its events, each a link to its own
  // More info, and the calls that are no event (a dispatch console's, in the Timeline below)
  function others(sb) {
    const items = (sb && sb.items) || [], calls = (sb && sb.calls) || [];
    const n = items.length + calls.length;
    if (!n) return [];
    const line = el("div", "pi-sibs small", `${n} other call${n === 1 ? "" : "s"} in this window: `);
    line.title = `Other calls on this channel within ±${Math.round((sb.window_s || 0) / 60)} min of this one`;
    const parts = [...items.map((x) => [x.at, () => {
      const a = el("a", "pi-sib", `${x.label || x.category} ${tm(x.at)}`);
      a.href = href("intel", x.id);
      a.title = `#${x.id}: its More info`;
      return a;
    }]), ...calls.map((c) => [c.at, () => {
      const t = el("span", "pi-sib", `${c.consoles.join(" · ")} ${tm(c.at)}`);
      t.title = `${c.n} console tx, no event (in the Timeline)`;
      return t;
    }])].sort((a, b) => a[0] - b[0]);
    parts.forEach(([, make], i) => { if (i) line.append(" · "); line.append(make()); });
    return [line];
  }
  function place(d) {
    const p = d.place;
    const kids = [head("Place")];
    if (!p.point) kids.push(none(p.empty));
    const pt = p.point;
    kids.push(dl([
      ["Where", p.label ? `${p.label}${pt && pt.approx ? " ?" : ""}` : "", pt ? `${pt.lat}, ${pt.lon}${pt.radius_m ? ` within ${Math.round(pt.radius_m)} m` : ""}${pt.approx ? ": approximate" : ""}` : ""],
      ["Basis", p.basis && p.basis !== p.label ? p.basis : ""],
      ["Heard", (p.phrases || []).join("; ")],
      ["Or", (p.alts || []).map((a) => a.label).join("; "), "Other readings of the words"],
    ]));
    const nb = p.nearby || {};
    const pp = (nb.preplan || {}).items || [];
    if (pp.length) kids.push(dl([["Pre-plan", pp.map((x) => `${x.kind}${x.label ? `: ${x.label}` : ""}`).join(" · "), "Pre-plan points at this building"]]));
    const cams = (nb.cameras || {}).items || [];
    if (cams.length) kids.push(dl([["Cameras", cams.map((c) => `${c.name} (${c.km} km)`).join(" · ")]]));
    return kids;
  }
  function site(d) {
    const s = d.site_history;
    const kids = [head("Site history", s.site ? `${s.site}${s.unconfirmed ? " · unconfirmed place" : ""}` : "",
      "Earlier calls at this address or site (5 years), its earlier pre-alerts and its address notes"
      + (s.unconfirmed ? "\nUnconfirmed place: the words didn't confirm it, but it was the only reading and a sure one" : ""))];
    const calls = s.calls || { items: [] };
    if (calls.items.length) {
      const ol = el("ol", "list pi-calls");
      for (const c of calls.items) {
        const li = el("li", "pi-call");
        const top = el("div", "pi-call-head");
        top.append(incLink(c), el("span", "dim small", [day(c.created_at), c.disposition, (c.units || []).join(" ")].filter(Boolean).join(" · ")));
        li.append(top);
        if (c.remarks) li.append(el("div", "pi-remarks small", c.remarks));
        for (const m of c.comments || []) li.append(el("div", "pi-cmt small", `${day(m.at)} ${m.by ? `${m.by}: ` : ""}${m.text}`));
        ol.append(li);
      }
      kids.push(ol);
      if (calls.capped) kids.push(el("div", "dim small", `${calls.items.length} of ${calls.total}`));
    } else kids.push(none(calls.empty));
    const pa = s.prealerts || { items: [] };
    if (pa.items.length) {
      kids.push(el("h4", "pi-sub", "Earlier pre-alerts"));
      const ol = el("ol", "list pi-prior");
      for (const x of pa.items) {
        const a = el("a", "", `${day(x.at || x.first_at)} · ${x.title || x.category} · ${x.status}`);
        a.href = href(d.event.kind, x.id);
        const li = el("li");
        li.append(a);
        ol.append(li);
      }
      kids.push(ol);
    }
    const notes = s.notes || { items: [] };
    if (notes.items.length) {
      kids.push(el("h4", "pi-sub", "Address notes"));
      for (const n of notes.items) kids.push(el("div", "pi-note small", `${n.category ? `${n.category}: ` : ""}${n.text}`));
    }
    return kids;
  }
  function now(d) {
    const n = d.now;
    const kids = [head("Now", "", "What is open near it as of this page's read")];
    const calls = n.calls || { items: [] };
    if (calls.items.length) {
      for (const c of calls.items) {
        const row = el("div", "pi-now-row");
        row.append(incLink(c), el("span", "dim small", [c.status, c.m !== null && c.m !== undefined ? `${c.m} m` : "",
          (c.units || []).map((u) => `${u.name} ${u.status}`).join(", ")].filter(Boolean).join(" · ")));
        kids.push(row);
      }
    } else kids.push(none(calls.empty));
    const ac = n.aircraft || { items: [] };
    if (ac.items.length) {
      const h = el("h4", "pi-sub", "Aircraft");
      h.title = ac.note || "";
      kids.push(h);
      for (const x of ac.items) {
        const parts = [x.callsign || x.spoken || "aircraft", x.type || x.model, typeof x.alt === "number" ? `${x.alt.toLocaleString()} ft` : x.alt,
          x.dist !== undefined && x.dist !== null ? `${x.dist} NM` : "", x.phase, x.pos_at ? `fix ${tm(x.pos_at)}` : (x.hex ? "" : "heard, not on ADS-B")];
        kids.push(el("div", "pi-acft small", parts.filter(Boolean).join(" · ")));
      }
    } else if (d.event.kind === "airport") kids.push(none(ac.empty));
    return kids;
  }
  function outcome(d) {
    const o = d.outcome;
    const kids = [head("Outcome")];
    const fb = (o.feedback || []).map((f) => `${f.verdict}${f.operator ? ` · ${f.operator}` : ""} · ${tm(f.at)}`);
    kids.push(dl([
      ["Status", [STATUS[o.status] || o.status, o.by ? ` · ${o.by}` : "", o.at ? ` · ${tm(o.at)}` : ""].join("")],
      ["Note", o.note || ""],
      ["Ack", o.acknowledged ? `${o.acknowledged.by || ""} · ${tm(o.acknowledged.at)}` : ""],
      ["Event", o.incident ? incLink(o.incident) : ""],
      ["Near", o.near_incident ? incLink(o.near_incident, "Our call at its place (linked, never a radio link)") : ""],
      ["Handoff", (o.handoff || []).length >= 2 ? `${o.handoff[0]} → ${o.handoff[1]}${o.handoff[2] ? ` · ${o.handoff[2]}` : ""}` : "",
        "One call another department's console took over: its town is the one that took it"],
      ["FD", o.fd || "", o.fd_state === "cleared" ? `${(o.fd || "").split(" · ")[0]} (cleared): pre-alert`
        : "The FD was already there: no pre-alert (an FYI only when the words added something)"],
      ["Push", o.pushed_at ? tm(o.pushed_at) : o.held && o.held.key ? `held: ${o.held.label}` : o.suppressed ? `held: ${o.suppressed}`
        : o.pushed ? "pushed" : "", o.held && o.held.key ? [o.held.tip, o.held.detail].filter(Boolean).join("\n")
        : o.suppressed ? "Not pushed, and why" : ""],
      // the last push at ntfy: "sent 16:21:27" / "not sent: 429" (sizeup/intel/events.py delivery)
      ["Delivery", o.pushed_at && window.IntelCard ? IntelCard.delivered(o, tm) : "",
        o.delivery ? (o.delivery.ok ? `ntfy took it (HTTP ${o.delivery.status}${o.delivery.id ? `, message ${o.delivery.id}` : ""})`
          : `ntfy didn't take it: ${o.delivery.error || o.delivery.status}`) : "Queued: no answer from ntfy yet"],
      ["Votes", fb.join("; ")],
      ["Tags", Object.entries(o.note_tags || {}).map(([t, n]) => `${(window.IntelNotes && IntelNotes.TAGS[t]) || t}${n > 1 ? ` ×${n}` : ""}`).join(" · "),
        "Its notes' tags (Notes)"],
    ]));
    return kids;
  }
  const BUILD = { "pi-why": why, "pi-trigger": trigger, "pi-around": around, "pi-site": site, "pi-place": place,
                  "pi-now": now, "pi-outcome": outcome };
  const DATA = { "pi-why": (d) => [d.why, d.summary], "pi-trigger": (d) => d.trigger, "pi-around": (d) => [d.before_after, d.siblings],
                 "pi-site": (d) => d.site_history, "pi-place": (d) => d.place,
                 "pi-now": (d) => ({ ...d.now, as_of: 0 }), "pi-outcome": (d) => d.outcome };

  // ---- the page ----------------------------------------------------------------------------------------------------
  function render(d) {
    if (!d || !d.event) return;
    st.data = d;
    show($("#pi-error"), false);
    const s = d.summary;
    setTextIf($("#pi-title"), s.title || "");
    const csig = JSON.stringify([s.level, s.source_name, s.at, d.outcome.status, s.drill]);
    const cbox = $("#pi-chips");
    if (cbox._sig !== csig) {
      cbox._sig = csig;
      const lv = el("span", `chip pi-level ${s.level || ""}`, s.level || "");
      lv.title = "The pre-alert's level";
      cbox.replaceChildren(...[s.drill ? el("span", "chip", "DRILL") : null, lv, el("span", "chip", STATUS[d.outcome.status] || d.outcome.status),
        el("span", "dim small", `${s.source_name || ""} · ${tm(s.at)}${s.count > 1 ? ` · ${s.count} tx` : ""}`)].filter(Boolean));
    }
    const gen = $("#pi-gen");
    setTextIf(gen, d.generated_at ? `as of ${tm(d.generated_at)}` : "");
    gen.title = `Bundle v${d.schema} · ${d.cursor}`;
    for (const id of SECTIONS) {
      const n = $(`#${id}`);
      if (!n) continue;
      const sig = JSON.stringify(DATA[id](d));
      if (st.sigs[id] === sig) continue;              // unchanged: left as it is (what plays, where it's scrolled)
      st.sigs[id] = sig;
      n.replaceChildren(...BUILD[id](d));
      show(n, true);
    }
    try { mapPoints(d); } catch (e) {                 // no WebGL here: the page without its map
      console.warn("pre-alert map:", e);
      show($("#pi-mapbox"), false);
    }
  }

  // ---- the map: the place (its radius when approximate), other readings, the aircraft, the events -----------------
  function features(d) {
    const f = [];
    const pt = (lon, lat, k, props = {}) => f.push({ type: "Feature", properties: { k, ...props }, geometry: { type: "Point", coordinates: [lon, lat] } });
    const p = d.place.point;
    if (p) {
      if (p.radius_m) {
        const ring = [];
        for (let i = 0; i <= 32; i++) {
          const a = (i / 32) * 2 * Math.PI, dy = (p.radius_m / 111320) * Math.sin(a);
          ring.push([p.lon + (p.radius_m / (111320 * Math.cos((p.lat * Math.PI) / 180))) * Math.cos(a), p.lat + dy]);
        }
        f.push({ type: "Feature", properties: { k: "area" }, geometry: { type: "Polygon", coordinates: [ring] } });
      }
      pt(p.lon, p.lat, p.approx ? "approx" : "place");
    }
    for (const a of d.place.alts || []) if (a.lat !== undefined && a.lat !== null) pt(a.lon, a.lat, "alt");
    for (const x of (d.now.aircraft || {}).items || []) if (x.lat !== undefined && x.lat !== null) pt(x.lon, x.lat, "acft");
    for (const i of [d.outcome.incident, d.outcome.near_incident, ...((d.now.calls || {}).items || [])]) {
      if (i && i.lat !== null && i.lat !== undefined) pt(i.lon, i.lat, "inc", { id: i.id });
    }
    return f;
  }
  function mapPoints(d) {
    const f = features(d);
    show($("#pi-mapbox"), f.length > 0);
    if (!f.length || !window.maplibregl || typeof buildMapStyle !== "function") return;
    if (!cad.scene || !cad.scene.home) {             // the scene not read yet (the page opened first): once it is
      if (!st.waiting) st.waiting = setTimeout(() => { st.waiting = 0; if (st.data) render(st.data); }, 500);
      return;
    }
    if (!st.map) {
      st.map = new maplibregl.Map({ container: $("#pi-map"), style: buildMapStyle(cad.scene), center: cad.scene.home.center,
                                    zoom: cad.scene.home.zoom, attributionControl: { compact: true }, maxPitch: 0, dragRotate: false });
      st.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      st.map.on("load", () => {
        st.map.addSource("pi", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        st.map.addLayer({ id: "pi-area", type: "fill", source: "pi", filter: ["==", ["get", "k"], "area"],
                          paint: { "fill-color": "#F2B33D", "fill-opacity": 0.15, "fill-outline-color": "#F2B33D" } });
        st.map.addLayer({ id: "pi-pts", type: "circle", source: "pi", filter: ["!=", ["get", "k"], "area"], paint: {
          "circle-radius": ["match", ["get", "k"], "place", 8, "approx", 8, "inc", 7, 6],
          "circle-color": ["match", ["get", "k"], "place", "#EE6475", "approx", "#F2B33D", "acft", "#6AA6E8", "inc", "#9AA3AD", "rgba(0,0,0,0)"],
          "circle-stroke-width": 2,
          "circle-stroke-color": ["match", ["get", "k"], "alt", "#F2B33D", "#0B0F14"] } });
        st.map.on("click", "pi-pts", (e) => { const x = e.features && e.features[0]; if (x && x.properties.id && typeof openFromRecords === "function") openFromRecords(x.properties.id); });
        st.mapReady = true;
        if (st.data) mapPoints(st.data);
      });
      return;
    }
    if (!st.mapReady) return;
    st.map.resize();
    st.map.getSource("pi").setData({ type: "FeatureCollection", features: f });
    if (st.framed !== st.key) {                       // framed once per pre-alert: a re-render keeps the viewer's view
      st.framed = st.key;
      const b = new maplibregl.LngLatBounds();
      for (const x of f) for (const c of x.geometry.type === "Point" ? [x.geometry.coordinates] : x.geometry.coordinates[0]) b.extend(c);
      st.map.fitBounds(b, { padding: 40, maxZoom: 16, duration: 0 });
    }
  }

  return { KINDS, href, parse, open, close, render, reload, features, newestFirst };
})();
