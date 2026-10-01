// sizeup browser UI. The server pushes state over SSE once a second; this file
// renders it: the map (home view, firehouses, the active incident), the incident
// card, dispatch transcripts, radio traffic, recent pages and history.
//
// Everything on screen comes from the server. Lists are diffed by id and only
// changed entries are touched, so the page never blinks, keeps its scroll
// position and never steals focus.
"use strict";

const $ = (sel) => document.querySelector(sel);
const LOST_AFTER_MS = 6000;          // no push for this long: say the display is stale
const RETURN_AFTER_MS = 120000;      // someone panned the map: go back to the incident/home after 2 min
const SOURCE_COLORS = { "Fire/EMS 15": "#3fd0e0", "FG1": "#ff8a3d", "South 2": "#e8d84a" };
const EXTRA_COLORS = ["#7ee07e", "#f27ad1", "#a3a9ff", "#ffcf7a", "#7fe3c8"];

const ui = {
  scene: null, state: null, map: null, mapReady: false,
  lastMsgAt: 0, skew: 0, error: null,
  inc: null, targetKey: null, marker: null, markerSig: "", userMovedAt: 0,
};

// ---- small helpers ------------------------------------------------------------

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function setText(node, text) {
  text = text ?? "";
  if (node.textContent !== text) node.textContent = text;
}
function show(node, on) {
  if (node.hidden === on) node.hidden = !on;
}
function setVar(node, name, value) {
  if (node.style.getPropertyValue(name) !== value) node.style.setProperty(name, value);
}
function setClass(node, cls) {
  if (node.className !== cls) node.className = cls;
}

const pad = (n) => String(n).padStart(2, "0");
const serverNow = () => Date.now() / 1000 + ui.skew;
// Times are shown in the station's timezone (scene.timezone), not whatever the TV is set to.
const fmts = {};
function fmt(kind) {
  if (!fmts[kind]) {
    const timeZone = (ui.scene && ui.scene.timezone) || undefined;
    const opts = {
      hm: { hour: "2-digit", minute: "2-digit", hourCycle: "h23" },
      hms: { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" },
      day: { weekday: "short", month: "short", day: "numeric" },
      short: { month: "short", day: "numeric" },
    }[kind];
    fmts[kind] = new Intl.DateTimeFormat("en-US", { ...opts, timeZone });
  }
  return fmts[kind];
}
const hhmm = (d) => fmt("hm").format(d);
const hhmmss = (d) => fmt("hms").format(d);
const DAY = { format: (d) => fmt("day").format(d) };
const SHORT_DAY = { format: (d) => fmt("short").format(d) };

function fmtTime(unix, secs = true) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  return secs ? hhmmss(d) : hhmm(d);
}
// "14:32" today, "Sep 30 14:32" otherwise.
function fmtWhen(unix) {
  if (!unix) return "";
  const d = new Date(unix * 1000), now = new Date(serverNow() * 1000);
  return SHORT_DAY.format(d) === SHORT_DAY.format(now) ? hhmm(d) : `${SHORT_DAY.format(d)} ${hhmm(d)}`;
}
function fmtDur(s) {
  if (s === null || s === undefined || !isFinite(s)) return "";
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}
function fmtShortDur(s) {
  if (!isFinite(s) || s < 0) return "";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}
const hz = (v) => (v === null || v === undefined ? "?" : Number(v).toFixed(1));

function catColor(cat) {
  const c = (ui.scene && ui.scene.categories) || {};
  return c[cat] || c.unknown || "#9aa0b4";
}

function sourceColor(name) {
  if (SOURCE_COLORS[name]) return SOURCE_COLORS[name];
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return EXTRA_COLORS[h % EXTRA_COLORS.length];
}

function chip(text, color, cls = "") {
  const c = el("span", "chip " + cls, text);
  if (color) c.style.setProperty("--c", color);
  return c;
}

function playButton(id, title, hasAudio = true) {
  if (hasAudio === false) player.missing.add(id);  // the server says there's no recording
  const b = el("button", "play", "▶");
  b.dataset.play = id;
  b.title = title || "Play";
  paintPlay(b);
  return b;
}

// What a clip's status means, in words (pages and transmissions share these).
const STATUS_TEXT = {
  pending: "transcribing…",
  no_voice: "Tones only — no voice",
  no_speech: "No speech recognized",
  error: "Transcription failed",
};

// ---- keyed list sync ------------------------------------------------------------
// Reuses each entry's element by key; refills it only when its signature changed;
// moves nodes only when order changed. Keeps the reader's scroll position.

function scrollAnchor(box) {
  if (box.scrollTop <= 4) return null;
  for (const c of box.children) {
    if (c.offsetTop + c.offsetHeight > box.scrollTop) return { c, top: c.offsetTop };
  }
  return null;
}

function syncList(box, items, { key, sig, make, fill, empty }) {
  if (!items.length) {
    if (box.dataset.empty !== empty || box.children.length !== 1) {
      box.replaceChildren(el(box.tagName === "OL" ? "li" : "span", "empty", empty));
      box.dataset.empty = empty;
    }
    return;
  }
  delete box.dataset.empty;
  const a = scrollAnchor(box);
  const old = new Map();
  for (const c of [...box.children]) {
    if (c.dataset.key) old.set(c.dataset.key, c);
    else c.remove();
  }
  items.forEach((it, i) => {
    const k = key(it);
    let node = old.get(k);
    if (node) old.delete(k);
    else {
      node = make(it);
      node.dataset.key = k;
    }
    const s = sig(it);
    if (node._sig !== s) {
      node._sig = s;
      fill(node, it);
    }
    if (box.children[i] !== node) box.insertBefore(node, box.children[i] || null);
  });
  for (const node of old.values()) node.remove();
  if (a && a.c.isConnected && a.c.offsetTop !== a.top) box.scrollTop += a.c.offsetTop - a.top;
}

// ---- audio clips ----------------------------------------------------------------

const player = { audio: null, id: null, missing: new Set() };

function paintPlay(b) {
  const id = b.dataset.play;
  const playing = player.id === id, missing = player.missing.has(id);
  setText(b, playing ? "■" : missing ? "–" : "▶");
  b.classList.toggle("playing", playing);
  b.classList.toggle("missing", missing);
  if (missing) b.title = "No recording for this one";
}
function paintAllPlay() {
  document.querySelectorAll("[data-play]").forEach(paintPlay);
}
function stopClip() {
  if (player.audio) player.audio.pause();
  player.audio = player.id = null;
  paintAllPlay();
}
function playClip(id) {
  if (player.id === id) return stopClip();
  stopClip();
  const a = new Audio(`/api/audio/${encodeURIComponent(id)}.wav`);
  player.audio = a;
  player.id = id;
  a.addEventListener("ended", () => { if (player.audio === a) stopClip(); });
  a.addEventListener("error", () => {
    player.missing.add(id);
    if (player.audio === a) stopClip();
    else paintAllPlay();
  });
  a.play().catch(() => { /* reported by the error event */ });
  paintAllPlay();
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-play]");
  if (b) playClip(b.dataset.play);
});

// ---- header ---------------------------------------------------------------------

function renderClock() {
  const d = new Date();
  setText($("#clock-date"), DAY.format(d));
  setText($("#clock-local"), hhmmss(d));
  setText($("#clock-z"), `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`);
}

function renderConn() {
  const pill = $("#conn");
  let cls = "pill ok", text = "live", title = "Receiving updates from the sizeup server";
  const age = Date.now() - ui.lastMsgAt;
  if (!ui.lastMsgAt) { cls = "pill warn"; text = "connecting…"; title = ""; }
  else if (age > LOST_AFTER_MS) {
    cls = "pill err"; text = `connection lost · ${fmtDur(age / 1000)}`;
    title = "No update from the server — what is on screen may be out of date";
  } else if (ui.error) { cls = "pill err"; text = "server error"; title = ui.error; }
  setClass(pill, cls);
  setText(pill, text);
  if (pill.title !== title) pill.title = title;
  document.body.classList.toggle("stale", !!ui.lastMsgAt && age > LOST_AFTER_MS);
}

const RADIO_STATE = { ok: "", off: "off", absent: "absent", down: "down", starting: "starting" };

function renderRadios() {
  const radios = (ui.state && ui.state.radios) || [];
  syncList($("#radios"), radios, {
    key: (r) => r.label,
    sig: (r) => JSON.stringify([r.state, r.open, r.detail, r.mhz, r.quieting_db, r.restarts]),
    make: () => el("span", "pill radio"),
    fill: (node, r) => {
      const state = r.state || "unknown";
      setClass(node, `pill radio ${state}${r.open && state === "ok" ? " open" : ""}`);
      const word = r.open && state === "ok" ? "● RX" : RADIO_STATE[state] ?? state;
      node.replaceChildren(el("b", null, r.label), ...(word ? [el("span", "st", word)] : []));
      const bits = [r.detail || r.label];
      if (r.mhz) bits.push(`${r.mhz} MHz`);
      if (r.quieting_db !== null && r.quieting_db !== undefined) bits.push(`quieting ${r.quieting_db} dB`);
      if (r.restarts) bits.push(`${r.restarts} restarts`);
      node.title = `${bits.join(" · ")}\nstate: ${state}${r.open ? " · squelch open" : ""}`;
    },
    empty: "no radios",
  });
}

// ---- active incident card -------------------------------------------------------

function activeIncident() {
  const list = (ui.state && ui.state.incidents) || [];
  return list.find((i) => i.status === "active") || null;
}

function renderIncident(inc) {
  const box = $("#incident");
  if (!inc) {
    setClass(box, "incident none");
    show($("#inc-empty"), true);
    show($("#inc-body"), false);
    show($("#inc-close"), false);
    setText($("#inc-status"), "");
    // Radios only: the speech-to-text service isn't something we listen on.
    const ok = ((ui.state && ui.state.radios) || []).filter((r) => r.state === "ok" && r.label !== "STT")
      .map((r) => r.label);
    setText($("#inc-listening"), ok.length ? `Listening · ${ok.join(" · ")}` : "No radio is receiving");
    const close = $("#inc-close");
    if (close.dataset.id) { close.dataset.id = ""; resetClose(); }
    return;
  }
  const color = catColor(inc.category);
  setClass(box, "incident active");
  setVar(box, "--c", color);
  show($("#inc-empty"), false);
  show($("#inc-body"), true);
  setText($("#inc-status"), "● Active");

  const close = $("#inc-close");
  show(close, true);
  if (close.dataset.id !== inc.id) { close.dataset.id = inc.id; resetClose(); }

  setText($("#inc-type"), (inc.incident_type || "Unknown") + (inc.code ? ` · ${inc.code}` : ""));
  const geo = inc.geo;
  const addr = $("#inc-addr");
  if (geo && geo.label) { setText(addr, geo.label); setClass(addr, "inc-addr"); }
  else if (inc.location) { setText(addr, inc.location); setClass(addr, "inc-addr heard"); }
  else { setText(addr, "Location not heard"); setClass(addr, "inc-addr missing"); }

  const note = $("#inc-geo-note");
  let noteText = "", warn = false;
  if (!geo) {
    warn = true;
    noteText = inc.location ? "⚠ Location not found on the map" : "⚠ No location in the page";
  } else if (geo.kind !== "address" || (geo.confidence ?? 1) < 0.8) {
    const pct = geo.confidence !== null && geo.confidence !== undefined ? ` · ${Math.round(geo.confidence * 100)}%` : "";
    noteText = `Map point approximate (${geo.kind || "match"}${pct})` + (inc.location ? ` — heard “${inc.location}”` : "");
  }
  setText(note, noteText);
  setClass(note, warn ? "inc-note warn" : "inc-note");
  show(note, !!noteText);

  const cross = $("#inc-cross");
  setText(cross, inc.cross_street ? `Cross: ${inc.cross_street}` : "");
  show(cross, !!inc.cross_street);
  setText($("#inc-timeout"), inc.time_out || "—");
  tickElapsed();

  // Departments, then the apparatus dispatched ("Engine 61", "Ladder 31").
  const who = [...(inc.departments || []).map((d) => ({ k: `d:${d}`, t: d, c: "chip dept" })),
               ...(inc.units || []).map((u) => ({ k: `u:${u}`, t: u, c: "chip unit" }))];
  syncList($("#inc-depts"), who, {
    key: (x) => x.k, sig: (x) => x.k,
    make: () => el("span", "chip"), fill: (n, x) => { setClass(n, x.c); setText(n, x.t); },
    empty: "Department not identified",
  });
}

function tickElapsed() {
  if (ui.inc) setText($("#inc-elapsed"), fmtDur(serverNow() - ui.inc.opened));
}

function resetClose() {
  const b = $("#inc-close");
  clearTimeout(ui.closeTimer);
  b.classList.remove("confirm");
  b.disabled = false;
  setText(b, "Close");
}

$("#inc-close").addEventListener("click", async (e) => {
  const b = e.currentTarget, id = b.dataset.id;
  if (!id) return;
  if (!b.classList.contains("confirm")) {   // two clicks: a stray tap must not close a working incident
    b.classList.add("confirm");
    setText(b, "Confirm close");
    clearTimeout(ui.closeTimer);
    ui.closeTimer = setTimeout(resetClose, 5000);
    return;
  }
  clearTimeout(ui.closeTimer);
  b.disabled = true;
  setText(b, "Closing…");
  try {
    const r = await fetch(`/api/incident/${encodeURIComponent(id)}/close`, { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  } catch (err) {
    setText(b, "Close failed");
    ui.closeTimer = setTimeout(resetClose, 4000);
    console.warn("close incident:", err);
    return;
  }
  b.disabled = false;   // the next push shows it closed
});

// ---- dispatch transcripts -------------------------------------------------------

function pagesById() {
  const m = new Map();
  for (const p of (ui.state && ui.state.pages) || []) m.set(p.id, p);
  return m;
}

const normText = (t) => (t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

function renderDispatch(inc) {
  const sec = $("#dispatch-section");
  show(sec, !!inc);
  if (!inc) return;
  const byId = pagesById();
  let items = (inc.pages || []).map((id) => byId.get(id)).filter(Boolean)
    .sort((a, b) => a.start - b.start)
    .map((p) => ({ key: p.id, clip: p.id, has: p.has_audio, start: p.start, text: p.raw, status: p.status, label: p.label }));
  if (!items.length) {
    // The pages fell out of the recent list: the incident carries its own page entries.
    items = (inc.page_entries || []).map((p) => ({ key: p.id, clip: p.id, has: p.has_audio, start: p.start,
                                                    text: p.text, status: "done", label: p.label }));
  }
  const seen = new Set();
  for (const it of items) {
    const n = normText(it.text);
    it.repeat = !!n && seen.has(n);
    if (n) seen.add(n);
  }
  syncList($("#dispatch"), items, {
    key: (it) => it.key,
    sig: (it) => JSON.stringify([it.text, it.status, it.repeat, it.start]),
    make: () => el("li"),
    fill: (li, it) => {
      const meta = el("div", "meta");
      if (it.clip) meta.append(playButton(it.clip, "Play the page", it.has));
      if (it.start) meta.append(el("span", "t", fmtTime(it.start)));
      if (it.label) meta.append(chip(it.label, sourceColor(it.label), "src"));
      if (it.repeat) meta.append(el("span", "dim", "repeat — same as above"));
      li.replaceChildren(meta);
      if (!it.repeat) {
        const done = it.status === "done" && it.text;
        li.append(el("div", done ? "txt" : "txt muted", done ? it.text : STATUS_TEXT[it.status] || it.status));
      }
    },
    empty: "No page text",
  });
}

// ---- radio traffic ----------------------------------------------------------------

function renderTraffic(inc) {
  const list = inc ? inc.traffic || [] : (ui.state && ui.state.loose_traffic) || [];
  setText($("#traffic-title"), inc ? "Radio traffic" : "Radio traffic · no active incident");
  const items = list.slice().sort((a, b) => b.start - a.start);
  syncList($("#traffic"), items, {
    key: (t) => t.id,
    sig: (t) => JSON.stringify([t.text, t.status, t.unit, t.talkgroup, t.source, t.end]),
    make: () => el("li"),
    fill: (li, t) => {
      const meta = el("div", "meta");
      meta.append(playButton(t.id, "Play this transmission", t.has_audio), el("span", "t", fmtTime(t.start)),
                  chip(t.source || "?", sourceColor(t.source), "src"));
      if (t.unit) meta.append(el("b", "unit", t.unit));
      if (t.talkgroup !== null && t.talkgroup !== undefined) meta.append(el("span", "dim", `TG ${t.talkgroup}`));
      if (t.end > t.start) meta.append(el("span", "dim dur", `${Math.round(t.end - t.start)} s`));
      const done = t.status === "done" && t.text;
      li.replaceChildren(meta, el("div", done ? "txt" : "txt muted",
        done ? t.text : t.status === "done" ? "(no words)" : STATUS_TEXT[t.status] || t.status));
    },
    empty: inc ? "No radio traffic yet"
      : ((ui.state && ui.state.radios) || []).some((r) => r.state === "ok" && r.label !== "STT")
        ? "Listening — no traffic" : "No radio is receiving",
  });
}

// ---- recent pages -----------------------------------------------------------------

function toneText(t) {
  if (t.b_hz === null || t.b_hz === undefined) return `${hz(t.a_hz)} Hz`;
  return `${hz(t.a_hz)} / ${hz(t.b_hz)} Hz`;
}

function renderPages() {
  const pages = ((ui.state && ui.state.pages) || []).slice().sort((a, b) => b.start - a.start);
  syncList($("#pages"), pages, {
    key: (p) => p.id,
    sig: (p) => JSON.stringify([p.status, p.raw, p.departments, p.tones, p.dispatch && p.dispatch.incident_type,
                                p.dispatch && p.dispatch.category, p.kind, p.label]),
    make: () => el("li"),
    fill: (li, p) => {
      const meta = el("div", "meta");
      meta.append(playButton(p.id, "Play this page", p.has_audio), el("span", "t", fmtTime(p.start)),
                  chip(p.label || "?", sourceColor(p.label), "src"),
                  el("span", "kind", p.kind === "voice" ? "VOICE" : "PAGE"));
      const d = p.dispatch;
      if (d && d.incident_type) {
        const ty = el("span", "ptype", d.incident_type);
        ty.style.color = catColor(d.category);
        meta.append(ty);
      }
      const who = el("div", "who");
      for (const name of p.departments || []) who.append(chip(name, null, "dept"));
      for (const t of p.tones || []) {
        if (t.names && t.names.length) {
          const known = (p.departments || []).length ? toneText(t) : `${t.names.join(" / ")} · ${toneText(t)}`;
          who.append(el("span", "dim tone", known));
        } else {
          who.append(chip(`Unknown tones ${toneText(t)}`, "#ffb020", "unknown"));
        }
      }
      if (!who.children.length) who.append(el("span", "dim", "No tones · department not identified"));
      const done = p.status === "done" && p.raw;
      // Two lines in the list; tap to read the whole page (stays open across updates).
      const open = (ui.expanded ||= new Set()).has(p.id);
      const txt = el("div", done ? (open ? "txt expandable" : "txt clamp expandable") : "txt muted",
                     done ? p.raw : STATUS_TEXT[p.status] || p.status);
      if (done) {
        txt.title = open ? "Tap to collapse" : "Tap to read the whole page";
        txt.addEventListener("click", () => {
          if (ui.expanded.has(p.id)) ui.expanded.delete(p.id); else ui.expanded.add(p.id);
          txt.classList.toggle("clamp", !ui.expanded.has(p.id));
          txt.title = ui.expanded.has(p.id) ? "Tap to collapse" : "Tap to read the whole page";
        });
      }
      li.replaceChildren(meta, who, txt);
      const q = [];
      if (p.mhz) q.push(`${p.mhz} MHz`);
      if (p.quieting_db !== null && p.quieting_db !== undefined) q.push(`quieting ${p.quieting_db} dB`);
      if (p.confidence !== null && p.confidence !== undefined) q.push(`speech confidence ${Math.round(p.confidence * 100)}%`);
      li.title = q.join(" · ");
    },
    empty: "No pages heard yet",
  });
}

// ---- earlier incidents --------------------------------------------------------------

function renderHistory(inc) {
  const list = ((ui.state && ui.state.incidents) || []).filter((i) => i !== inc);
  syncList($("#history"), list, {
    key: (i) => i.id,
    sig: (i) => JSON.stringify([i.status, i.incident_type, i.category, i.geo && i.geo.label, i.location,
                                i.departments, i.opened, i.updated, fmtWhen(i.opened)]),
    make: () => el("li"),
    fill: (li, i) => {
      const ty = el("span", "htype", i.incident_type || "Unknown");
      ty.style.color = catColor(i.category);
      const where = (i.geo && i.geo.label) || i.location;
      const loc = el("span", where ? "hloc" + (i.geo ? "" : " heard") : "hloc dim", where || "location not heard");
      const tail = i.status === "active" ? el("span", "chip live", "ACTIVE")
        : el("span", "dim", fmtShortDur(i.updated - i.opened));
      li.replaceChildren(el("span", "t", fmtWhen(i.opened)), ty, loc,
                         el("span", "dim hdept", (i.departments || []).join(", ")), tail);
    },
    empty: "None yet",
  });
}

// ---- map ------------------------------------------------------------------------------

function initMap() {
  const sc = ui.scene;
  const map = new maplibregl.Map({
    container: $("#map"),
    style: buildMapStyle(sc),
    center: sc.home.center,
    zoom: sc.home.zoom,
    attributionControl: { compact: true },
    maxPitch: 0,
    dragRotate: false,
    fadeDuration: 0,
  });
  map.touchZoomRotate.disableRotation();
  map.on("load", () => {
    map.addImage("firehouse", firehouseIcon(), { pixelRatio: 2 });
    map.getSource("firehouses").setData({
      type: "FeatureCollection",
      features: (sc.firehouses || []).map((f) => ({
        type: "Feature", properties: { name: f.name, address: f.address },
        geometry: { type: "Point", coordinates: f.ll },
      })),
    });
    ui.mapReady = true;
  });
  const touched = (e) => { if (e.originalEvent) ui.userMovedAt = Date.now(); };
  map.on("movestart", touched);
  map.on("moveend", touched);
  ui.map = map;
}

function mapTarget() {
  const inc = ui.inc, sc = ui.scene;
  if (inc && inc.geo) return { key: `inc:${inc.id}:${inc.geo.lat}:${inc.geo.lon}`,
                               center: [inc.geo.lon, inc.geo.lat], zoom: sc.incident_zoom };
  return { key: "home", center: sc.home.center, zoom: sc.home.zoom };
}

function goToTarget(instant) {
  const t = mapTarget();
  ui.userMovedAt = 0;
  if (instant) ui.map.jumpTo({ center: t.center, zoom: t.zoom });
  else ui.map.flyTo({ center: t.center, zoom: t.zoom, duration: 3500, essential: true });
}

function renderMap() {
  if (!ui.map) return;
  const t = mapTarget();
  if (t.key !== ui.targetKey) {          // only on change: never fight someone looking around
    const first = ui.targetKey === null;
    ui.targetKey = t.key;
    goToTarget(first);
  }
  const inc = ui.inc;
  const sig = inc && inc.geo ? JSON.stringify([inc.id, inc.geo.lat, inc.geo.lon, inc.category, inc.incident_type, inc.geo.label]) : "";
  if (sig === ui.markerSig) return;
  ui.markerSig = sig;
  if (ui.marker) { ui.marker.remove(); ui.marker = null; }
  if (!sig) return;
  const m = el("div", "inc-marker");
  m.style.setProperty("--c", catColor(inc.category));
  const tag = el("div", "inc-tag");
  tag.append(el("span", "inc-badge", inc.incident_type || "Unknown"), el("span", "inc-label", inc.geo.label || inc.location));
  m.append(tag, el("i", "ring"), el("i", "ring r2"), el("i", "dot"));
  ui.marker = new maplibregl.Marker({ element: m, anchor: "center" })
    .setLngLat([inc.geo.lon, inc.geo.lat]).addTo(ui.map);
}

function userZoom(fn) {
  ui.userMovedAt = Date.now();
  fn();
}
$("#zoom-in").addEventListener("click", () => userZoom(() => ui.map.zoomIn()));
$("#zoom-out").addEventListener("click", () => userZoom(() => ui.map.zoomOut()));
$("#recenter").addEventListener("click", () => goToTarget(false));
window.addEventListener("keydown", (e) => {
  if (!ui.map || e.target.closest("input, select, textarea")) return;
  if (e.key === "+" || e.key === "=") userZoom(() => ui.map.zoomIn());
  else if (e.key === "-" || e.key === "_") userZoom(() => ui.map.zoomOut());
  else if (e.key === "c") goToTarget(false);
});

// ---- state ----------------------------------------------------------------------------

function onState(data) {
  ui.lastMsgAt = Date.now();
  if (data.error) {                     // keep showing the last good state, and say so
    ui.error = data.error;
    renderConn();
    return;
  }
  ui.error = null;
  ui.state = data;
  if (typeof data.time === "number") ui.skew = data.time - Date.now() / 1000;
  ui.inc = activeIncident();
  renderConn();
  renderRadios();
  renderIncident(ui.inc);
  renderDispatch(ui.inc);
  renderTraffic(ui.inc);
  renderPages();
  renderHistory(ui.inc);
  renderMap();
}

function tick() {
  renderClock();
  renderConn();
  tickElapsed();
  if (ui.map && ui.userMovedAt && Date.now() - ui.userMovedAt > RETURN_AFTER_MS && !ui.map.isMoving()) {
    goToTarget(false);
  }
}

async function boot() {
  ui.scene = await (await fetch("/api/scene")).json();
  const sc = ui.scene;
  const name = (sc.station && sc.station.name) || "sizeup";
  document.title = `sizeup · ${name}`;
  setText($("#station"), name);
  initMap();
  const es = new EventSource("/api/stream");
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (err) { console.warn("bad state push:", err); return; }
    onState(data);
  };
  tick();
  setInterval(tick, 1000);
}

boot().catch((err) => {
  const pill = $("#conn");
  pill.className = "pill err";
  pill.textContent = "failed to load: " + err.message;
  console.error(err);
});
