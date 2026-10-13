// The station's own page ([cloud] role = "station": the PC with the RTL-SDRs, forwarding to the hub). No CAD
// here (user, 2026-10-13: "the local system doesnt show the cad bits just things relevant to the sdr station
// data pulling/pushing"): the CAD of record is on the hub, and this server refuses the CAD's API (409).
//
// Views (the header's tabs, in the URL hash; the CAD's old links still land: #settings/radios, #radio ...):
//   Radios    the receivers, each dongle's job, the paged incident, channels (record / transcribe / engine),
//             vocabulary, speech-to-text: the CAD's Settings > Radios (radio-controls.js)
//   Monitor   pages and transmissions this station heard, newest first, with audio and transcript (/monitor
//             opens here)
//   Settings  the hub link (hublink.js: forward on / off, hub URL, test, last contact, the outbox, where each
//             secret came from) and this install (version, role, station id; the commands on the PC)
// The header says it is the station: its id, the hub link's state and a link to the CAD on the hub.
// Live state: /api/stream (the monitor state, polling when streams are blocked); the hub link: /api/station-mode.
"use strict";

const $ = (sel) => document.querySelector(sel);
const LOST_AFTER_MS = 6000;
const MONITOR_ROWS = 150;              // the newest transmissions drawn (the state carries the last 300 + incidents')
const VIEWS = [["radios", "Radios"], ["monitor", "Monitor"], ["settings", "Settings"]];

const sta = {
  scene: null, state: null, health: null, hub: null, live: null,
  lastMsgAt: 0, skew: 0, view: "", q: "", channel: "", monSig: "",
  fromCad: false,                      // opened through /cad (redirected here): say where the CAD is
};

// ---- helpers (radio-controls.js uses these too, as it does cad.js's on the CAD) --------------------

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
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { /* private window / blocked storage: just not remembered */ }
  return null;
}
function api(method, path, body) {
  const p = apiCall(method, path, body);
  return method === "GET" ? p : UI.busy(p);
}
async function apiCall(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}
// A keyed list redrawn in place: only rows whose signature changed are filled again (cad.js has the same).
function syncList(box, items, { key, sig, make, fill, empty }) {
  if (!items.length) {
    if (box.dataset.empty !== empty || box.children.length !== 1) {
      box.replaceChildren(el(box.tagName === "OL" || box.tagName === "UL" ? "li" : "div", "empty", empty));
      box.dataset.empty = empty;
    }
    return;
  }
  delete box.dataset.empty;
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
}
const pad = (n) => String(n).padStart(2, "0");
const serverNow = () => Date.now() / 1000 + sta.skew;
const fmts = {};
function fmt(kind) {
  if (!fmts[kind]) {
    const timeZone = (sta.scene && sta.scene.timezone) || "America/New_York";
    const opts = {
      hm: { hour: "2-digit", minute: "2-digit", hourCycle: "h23" },
      hms: { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" },
      short: { month: "short", day: "numeric" },
      day: { weekday: "short", month: "short", day: "numeric" },
    }[kind];
    fmts[kind] = new Intl.DateTimeFormat("en-US", { ...opts, timeZone });
  }
  return fmts[kind];
}
function fmtTime(unix, secs = false) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  const t = fmt(secs ? "hms" : "hm").format(d);
  return fmt("short").format(d) === fmt("short").format(new Date(serverNow() * 1000)) ? t : `${fmt("short").format(d)} ${t}`;
}
function operator() {
  return $("#operator").value.trim();
}

// ---- views -------------------------------------------------------------------------------------------

// The hash, or what an old CAD link meant: #settings/radios -> Radios, #settings/... -> Settings, #radio -> Monitor.
function viewOf(hash) {
  const h = (hash || "").replace(/\?.*$/, "").slice(1);
  if (VIEWS.some(([k]) => k === h)) return h;
  if (h === "radio" || h === "comms") return "monitor";
  if (h.startsWith("settings/radios")) return "radios";
  if (h.startsWith("settings")) return "settings";
  return location.pathname.replace(/\/+$/, "") === "/monitor" ? "monitor" : "radios";
}

function route() {
  const v = viewOf(location.hash);
  if (location.hash && location.hash !== `#${v}`) history.replaceState(null, "", `${location.pathname}#${v}`);
  sta.view = v;
  for (const [k, label] of VIEWS) {
    show($(`#v-${k}`), k === v);
    const tab = $(`#${k}-open`);
    if (k === v) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    if (k === v) document.title = `PCFD Station · ${label}`;
  }
  if (v === "radios") RadioControls.mount($("#radio-controls"));
  if (v === "monitor") { sta.monSig = ""; renderMonitor(); }
  if (v === "settings") {
    HubLink.mount($("#hub-card"), {
      operator,
      askOperator: () => $("#operator").focus(),
      msg: sMsg,
      changed: refreshHub,
      offText: "Stop forwarding to the hub? Radios and speech-to-text keep running; what is heard meanwhile isn't sent. "
        + "Anything already queued waits here.",
      time: (t) => fmtTime(t, true),
      onState: (d) => { sta.hub = d; renderHub(); },
    });
    renderSystem();
  }
}

// Settings' messages: an error stays at the top until the next change; a save is a toast.
function sMsg(text, ok) {
  const m = $("#s-msg");
  if (ok) { setText(m, ""); if (text) UI.toast(text); return; }
  setText(m, text || "");
}

// ---- the header: the station, the hub link, the connection, the clock --------------------------------

async function refreshHub() {
  try {
    const r = await fetch("/api/station-mode", { cache: "no-store" });
    if (r.ok) { sta.hub = await r.json(); renderHub(); }
  } catch (_) { /* the next look tries again */ }
}

function renderHub() {
  const d = sta.hub, pill = $("#hub-pill"), link = $("#hub-cad");
  show(pill, !!d);
  if (!d) return;
  const s = d.status || {};
  const n = s.waiting || 0;
  if (!d.forward) {
    pill.className = "pill off";
    setText(pill, "Not forwarding");
  } else {
    pill.className = "pill " + ({ ok: "ok", starting: "warn" }[s.state] || "err");
    setText(pill, s.state === "ok" ? "Hub connected" : s.state === "starting" ? "Hub connecting"
      : s.state === "blocked" ? "Hub not sending" : "Hub unreachable");
  }
  pill.title = [s.text, s.detail, n ? `${n} waiting to go up` : ""].filter(Boolean).join("\n");
  const url = d.hub_url || "";
  show(link, !!url);
  if (url && link.dataset.url !== url) {
    link.href = `${url}/cad`;
    link.dataset.url = url;
    link.title = `The CAD of record: ${url}/cad`;
  }
  renderFromCad();
}

// /cad here redirects to /?from=cad: say where the CAD went, once
function renderFromCad() {
  const box = $("#from-cad");
  if (!sta.fromCad) return;
  const url = (sta.hub && sta.hub.hub_url) || "";
  box.replaceChildren("This is the station. The CAD is on the hub", ...(url
    ? [": ", Object.assign(el("a", null, `${url}/cad`), { href: `${url}/cad`, target: "_blank", rel: "noopener" })] : ["."]));
  show(box, true);
}

function renderConn() {
  const pill = $("#conn");
  const age = Date.now() - sta.lastMsgAt;
  if (!sta.lastMsgAt) { pill.className = "pill warn"; setText(pill, "Connecting"); }
  else if (age > LOST_AFTER_MS) { pill.className = "pill err"; setText(pill, "No updates"); }
  else if (sta.live && sta.live.mode() === "polling") {
    pill.className = "pill ok"; setText(pill, "Polling");
    pill.title = "Stream blocked on this network: polling every 2 s";
  } else { pill.className = "pill ok"; setText(pill, "Live"); pill.title = ""; }
  document.body.classList.toggle("stale", !!sta.lastMsgAt && age > LOST_AFTER_MS);
}
function renderClock() {
  const d = new Date(serverNow() * 1000);
  setText($("#clock-date"), fmt("day").format(d));
  setText($("#clock-local"), fmt("hms").format(d));
  setText($("#clock-z"), `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`);
}

// ---- Monitor: pages and transmissions heard here ------------------------------------------------------

const STT_SAID = { no_speech: "Key-up, no speech", pending: "Transcribing", remote: "Transcribed on the hub (AWS)",
                   off: "Not transcribed", error: "Transcription failed" };
const said = (x) => STT_SAID[x.status] || "—";
const player = new Audio();
let playing = null;                  // the play button of what is playing now
player.addEventListener("ended", () => stopPlay());
function stopPlay() {
  if (playing) Icon.play(playing, "play");
  playing = null;
}
// A page plays from where its voice starts (voice_offset_s: the tones skipped, #80); "Play with tones" is in its menu.
function play(btn, id, at = 0) {
  if (playing === btn) { player.pause(); return stopPlay(); }
  stopPlay();
  player.src = `/api/audio/${encodeURIComponent(id)}.wav` + (at > 0 ? `#t=${Number(at).toFixed(2)}` : "");
  player.play().then(() => { playing = btn; Icon.play(btn, "stop"); }).catch((e) => console.warn("audio:", e));
}
function pageMenu(p) {
  return UI.menu.button(`Actions: page ${fmtTime(p.start, true)}`, () => [
    { label: "Play with tones", disabled: p.has_audio === false, run: () => play(playButton(p), p.id, 0) },
  ], { key: `page:${p.id}` });
}
function playButton(x) {
  const b = Icon.play(el("button", "play"), x.has_audio === false ? "none" : "play");
  b.type = "button";
  b.disabled = x.has_audio === false;
  const skip = (x.tones || []).length && x.voice_offset_s > 0;
  b.title = x.has_audio === false ? "Recording expired (12 h)" : skip ? "Play (tones skipped)" : "Play";
  b.addEventListener("click", () => play(b, x.id, skip ? x.voice_offset_s : 0));
  return b;
}

const modelName = (m) => (m === "aws-transcribe" ? "AWS" : m.replace(/^ggml-/, "").replace(/\.bin$/, ""));

function traffic(s) {
  const all = [...(s.loose_traffic || []), ...(s.incidents || []).flatMap((i) => i.traffic || [])];
  return all.sort((a, b) => b.start - a.start);
}
function fireground(t) {                  // an FM traffic channel (FG1): a green edge, as on the CAD's Radio
  const fm = ((sta.state && sta.state.channels) || {}).fm || [];
  const ch = fm.find((c) => c.key === t.source);
  return !!ch && ch.kind === "traffic";
}
function matches(x, text) {
  if (sta.channel && (x.source || x.label) !== sta.channel) return false;
  return !sta.q || (text || "").toLowerCase().includes(sta.q) || (x.unit || x.rig || "").toLowerCase().includes(sta.q);
}

function renderMonitor() {
  const s = sta.state;
  if (sta.view !== "monitor" || !s) return;
  const pages = (s.pages || []).filter((p) => matches(p, p.raw));
  // a page going out now (South 2, state.paging): over the Pages list until its finished row is in it
  Radio.fillPaging($("#m-paging"), s.paging, new Set((s.pages || []).map((p) => p.id)));
  const all = traffic(s);
  const rows = all.filter((t) => matches(t, t.text)).slice(0, MONITOR_ROWS);
  const sig = JSON.stringify([sta.q, sta.channel, pages.map((p) => [p.id, p.status, p.raw, p.has_audio, p.tones]),
                              rows.map((t) => [t.id, t.status, t.text, t.has_audio, t.rig, t.alt_text])]);
  fillChannels(all, s.pages || []);
  if (sig === sta.monSig) return;
  sta.monSig = sig;
  setText($("#m-count"), `${all.length} transmission${all.length === 1 ? "" : "s"}, ${(s.pages || []).length} page${(s.pages || []).length === 1 ? "" : "s"}`);
  setText($("#m-pages-sum"), pages.length ? `${pages.length}` : "");
  setText($("#m-traffic-sum"), rows.length < all.length ? `${rows.length} of ${all.length}` : `${rows.length}`);
  const filtered = !!(sta.q || sta.channel);
  syncList($("#m-pages"), pages.slice(0, 20), {
    key: (p) => p.id,
    sig: (p) => JSON.stringify([p.status, p.raw, p.has_audio, p.tones]),
    make: () => el("li", "rt"),
    fill: (li, p) => {
      const meta = el("div", "meta");
      meta.append(playButton(p), el("span", "t", fmtTime(p.start, true)), el("span", "chip src", p.label));
      const d = p.dispatch;
      if (d && d.incident_type) meta.append(el("span", "ptype", d.incident_type));
      if ((p.tones || []).length) meta.append(...Radio.pageTags(p.tones));   // who it was for; an unknown pair as its tones
      else if (p.kind === "voice") meta.append(el("span", "dim small", "no tones"));
      if ((p.tones || []).some((x) => !(x.names || []).length)) meta.append(el("span", "dim small", "unknown tones"));
      if ((p.tones || []).length && p.voice_offset_s > 0) meta.append(pageMenu(p));
      li.replaceChildren(meta, el("div", "txt" + (p.raw ? "" : " muted"), p.raw || said(p)));
    },
    empty: filtered ? "No pages match" : "No pages yet",
  });
  syncList($("#m-traffic"), rows, {
    key: (t) => t.id,
    sig: (t) => JSON.stringify([t.status, t.text, t.has_audio, t.rig, t.radio_id, t.unit, t.alt_text]),
    make: () => el("li", "rt"),
    fill: (li, t) => {
      const fg = fireground(t);
      li.className = "rt" + (fg ? " fg" : "");
      const meta = el("div", "meta");
      meta.append(playButton(t), el("span", "t", fmtTime(t.start, true)));
      const tag = Radio.rigTag({ rig: t.rig, radio_id: t.radio_id, unit: t.unit });
      if (tag) meta.append(tag);
      meta.append(el("span", "chip src" + (fg ? " fg" : ""), t.source));
      if (t.end > t.start) meta.append(el("span", "dim", `${Math.round(t.end - t.start)} s`));
      if (t.model && t.status !== "remote") meta.append(Object.assign(el("span", "dim small", modelName(t.model)), { title: `Speech-to-text: ${t.model}` }));
      li.replaceChildren(meta, el("div", "txt" + (t.text ? "" : " muted"), t.text || said(t)));
      if (t.alt_model) {
        const alt = el("div", "txt alt-txt");
        alt.append(el("span", "dim", `${t.alt_model}: `), document.createTextNode(t.alt_text || "(nothing)"));
        li.append(alt);
      }
    },
    empty: filtered ? "No transmissions match" : "Nothing heard yet",
  });
}

// The channel filter: every channel and talkgroup heard (and the configured FM channels), sorted
function fillChannels(all, pages) {
  const sel = $("#m-channel");
  const fm = (((sta.state && sta.state.channels) || {}).fm || []).map((c) => c.key);
  const names = [...new Set([...fm, ...all.map((t) => t.source), ...pages.map((p) => p.label)].filter(Boolean))].sort();
  const sig = names.join("\n");
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.replaceChildren(new Option("Any channel", ""), ...names.map((n) => new Option(n, n)));
  sel.value = names.includes(sta.channel) ? sta.channel : "";
}

// ---- Settings: this install -----------------------------------------------------------------------------

function renderSystem() {
  const h = sta.health || {}, sc = (sta.scene && sta.scene.station) || {};
  const rows = [["Station", sc.id || h.station || "—"], ["Name", sc.name || "—"], ["Role", h.role || "—"],
                ["Version", h.version || "—"], ["Build", h.build || "—"]];
  $("#sys-facts").replaceChildren(...rows.map(([k, v]) => {
    const d = el("div");
    d.append(el("dt", "", k), el("dd", v === "—" ? "dim" : "", v));
    return d;
  }));
}

// ---- state -------------------------------------------------------------------------------------------------

function onState(s) {
  if (!s || s.error) return;
  sta.state = s;
  sta.lastMsgAt = Date.now();
  if (s.time) sta.skew = s.time - Date.now() / 1000;
  Radio.update(s.talking || [], s.paging || []);     // the header chip: who is keying up, a page going out
  renderMonitor();
}

function tick() {
  renderConn();
  renderClock();
}

$("#operator").value = store("sizeup.cad.operator") || "";
$("#operator").addEventListener("input", () => store("sizeup.cad.operator", operator()));
$("#m-q").addEventListener("input", () => { sta.q = $("#m-q").value.trim().toLowerCase(); renderMonitor(); });
$("#m-channel").addEventListener("change", () => { sta.channel = $("#m-channel").value; renderMonitor(); });
$("#m-filters").addEventListener("submit", (e) => e.preventDefault());
window.addEventListener("hashchange", route);

async function boot() {
  const q = new URLSearchParams(location.search);
  if (q.get("from") === "cad") {
    sta.fromCad = true;
    history.replaceState(null, "", "/" + location.hash);
  }
  try {
    sta.scene = await (await fetch("/api/scene")).json();
    const s = sta.scene.station || {};
    setText($("#station"), s.id || "");
    $("#station").title = s.name ? `${s.name} (station id ${s.id})` : "";
  } catch (e) {
    console.warn("no scene:", e);
  }
  try {
    sta.health = await (await fetch("/healthz", { cache: "no-store" })).json();
  } catch (_) { /* the Settings card says "—" */ }
  route();
  renderFromCad();
  sta.live = liveState("/api/stream", "/api/state", onState);
  await refreshHub();
  setInterval(() => { if (!document.hidden && sta.view !== "settings") refreshHub(); }, 5000);   // (Settings: HubLink's own)
  tick();
  setInterval(tick, 1000);
}

boot().catch((err) => {
  const pill = $("#conn");
  pill.className = "pill err";
  pill.textContent = "Load failed: " + err.message;
  console.error(err);
});
