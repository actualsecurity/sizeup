// sizeup communications (/comms): every transmission the monitor transcribed, newest first, with
// the rig its radio is on (the radio registry), the raw radio ID, channel / talkgroup, the CAD
// events it is linked to, the transcript and its audio (while kept: 12 h); and the notes typed on
// an event with a unit tag ("E63 stated that ..."). Filters live in the URL, so a link from an
// event (/comms?event=26-000012) opens filtered. Polls /api/cad/comms every 2 s: unchanged = cheap.
"use strict";

const $ = (s) => document.querySelector(s);
const POLL_MS = 2000;
const LOST_AFTER_MS = 8000;
const c = { rev: "", rows: [], lastOk: 0, player: new Audio(), playing: null, facets: null, busy: false };

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
const fmtT = new Intl.DateTimeFormat([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
                                           second: "2-digit", hour12: false, timeZone: "America/New_York" });
const toLocal = (unix) => {
  if (!unix) return "";
  const d = new Date(unix * 1000), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fromLocal = (v) => (v ? Date.parse(v) / 1000 : "");

const FIELDS = ["unit", "channel", "event", "type", "pcfd", "since", "until"];
function filters() {
  return { unit: $("#f-unit").value, channel: $("#f-channel").value, event: $("#f-event").value, type: $("#f-type").value,
           pcfd: $("#f-pcfd").checked ? "1" : "", since: fromLocal($("#f-since").value), until: fromLocal($("#f-until").value) };
}
function fromUrl() {
  const q = new URLSearchParams(location.search);
  const sel = (id, v) => {
    const s = $(id);
    if (v && ![...s.options].some((o) => o.value === v)) s.append(new Option(v, v));
    s.value = v || "";
  };
  sel("#f-unit", q.get("unit"));
  sel("#f-channel", q.get("channel"));
  sel("#f-event", q.get("event"));
  $("#f-type").value = q.get("type") || "";
  $("#f-pcfd").checked = q.get("pcfd") === "1";
  $("#f-since").value = toLocal(Number(q.get("since")) || 0);
  $("#f-until").value = toLocal(Number(q.get("until")) || 0);
}
function query(withRev) {
  const f = filters(), q = new URLSearchParams();
  for (const k of FIELDS) if (f[k] !== "" && f[k] !== undefined) q.set(k, f[k]);
  if (withRev && c.rev) q.set("rev", c.rev);
  return q;
}
function changed() {
  const q = query(false).toString();
  history.replaceState(null, "", q ? `?${q}` : location.pathname);
  c.rev = "";
  poll();
}

function fillFacets(d) {
  const key = JSON.stringify([d.rigs, d.channels, d.events, d.pcfd]);
  if (c.facets === key) return;
  c.facets = key;
  const keep = (id, opts, first) => {
    const s = $(id), v = s.value;
    s.replaceChildren(new Option(first, ""), ...opts.map(([val, text]) => new Option(text, val)));
    if (v && !opts.some(([val]) => val === v)) s.append(new Option(v, v));
    s.value = v;
  };
  const pcfd = new Set(d.pcfd || []);
  keep("#f-unit", (d.rigs || []).sort((a, b) => (pcfd.has(b) - pcfd.has(a)) || a.localeCompare(b, undefined, { numeric: true }))
    .map((u) => [u, pcfd.has(u) ? u : `${u} (not PCFD)`]), "Any unit");
  keep("#f-channel", (d.channels || []).map((x) => [x, x]), "Any channel");
  keep("#f-event", (d.events || []).map((e) => [e.number || String(e.id), `${e.number || "#" + e.id} · ${e.type || ""} ${e.address || ""}`.trim()]),
       "Any event");
}

function play(id, b) {
  if (c.playing === id) { c.player.pause(); c.playing = null; paint(); return; }
  c.player.src = `/api/audio/${encodeURIComponent(id)}.wav`;
  c.playing = id;
  c.player.play().catch(() => { c.playing = null; if (b) { b.textContent = "–"; b.title = "No recording (kept 12 h)"; } paint(); });
  paint();
}
c.player.addEventListener("ended", () => { c.playing = null; paint(); });
function paint() {
  for (const b of document.querySelectorAll("[data-play]")) {
    const on = b.dataset.play === c.playing;
    b.classList.toggle("playing", on);
    if (b.textContent !== "–") b.textContent = on ? "■" : "▶";
  }
}

function row(t) {
  const li = el("li", t.kind === "note" ? "rt note" : "rt");
  const meta = el("div", "meta");
  if (t.kind === "radio") {
    const b = el("button", "play", "▶");
    b.dataset.play = t.id;
    b.disabled = !t.has_audio;
    b.title = t.has_audio ? "Play" : "Recording no longer kept (12 h)";
    if (!t.has_audio) b.textContent = "–";
    b.addEventListener("click", () => play(t.id, b));
    const rr = el("button", "rerun", "↻");
    rr.title = "Run speech-to-text again on this recording";
    rr.disabled = !t.has_audio;
    rr.addEventListener("click", async () => {
      rr.disabled = true;
      rr.textContent = "…";
      try {
        const r = await fetch("/api/retranscribe", { method: "POST", headers: { "Content-Type": "application/json" },
                                                     body: JSON.stringify({ ids: [t.id] }) });
        if (!r.ok || !(await r.json()).queued) { rr.textContent = "↻"; rr.disabled = false; }
      } catch (_) { rr.textContent = "↻"; rr.disabled = false; }
    });
    meta.append(b, rr);
  } else meta.append(el("span", "ek note", "note"));
  meta.append(el("span", "t", fmtT.format(new Date(t.start * 1000))));
  const tag = Radio.rigTag({ rig: t.rig, radio_id: t.radio_id, unit: t.kind === "note" ? "" : t.talker });
  if (tag) meta.append(tag);
  meta.append(el("span", "chip src", t.channel + (t.talkgroup ? ` · TG ${t.talkgroup}` : "")));
  for (const e of t.events || []) {
    const a = el("a", "inc-link", e.number || `#${e.id}`);
    a.href = `/comms?event=${encodeURIComponent(e.number || e.id)}`;
    a.title = `${e.type || ""} ${e.address || ""} · linked by ${e.why}${e.unit ? ` (${e.unit})` : ""}`.trim();
    meta.append(a);
  }
  if (t.kind === "note" && t.operator) {          // "Corey <corey@...>": signed in through Cloudflare Access
    const m = /^(.*\S) <([^<>\s]+@[^<>\s]+)>$/.exec(t.operator);
    const op = el("span", "", `· ${m ? m[1] : t.operator}`);
    if (m) op.title = `Signed in with Cloudflare Access as ${m[2]}`;
    meta.append(op);
  }
  if (t.kind === "radio" && t.end > t.start) meta.append(el("span", "dim", `${Math.round(t.end - t.start)} s`));
  const said = t.status === "no_speech" ? "key-up · no speech" : t.status === "pending" ? "transcribing…" : "(no words)";
  li.append(meta, el("div", "txt" + (t.text ? "" : " muted"), t.text || said));
  if (t.dispatcher) li.classList.add("from-dispatch");
  return li;
}

function render() {
  const box = $("#rows");
  const sig = JSON.stringify(c.rows.map((t) => [t.id, t.text, t.status, t.rig, (t.events || []).length, t.has_audio]));
  if (box._sig !== sig) {
    box._sig = sig;
    box.replaceChildren(...(c.rows.length ? c.rows.map(row) : [el("li", "empty", "Nothing matches these filters")]));
    paint();
  }
  $("#count").textContent = `${c.rows.length}${c.rows.length >= 300 ? "+" : ""} shown · newest first · live`;
}

async function poll() {
  if (c.busy) { c.again = true; return; }      // a filter changed mid-request: ask again when it's back
  c.busy = true;
  c.again = false;
  const asked = query(false).toString();
  try {
    const r = await fetch(`/api/cad/comms?${query(true)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    c.lastOk = Date.now();
    Radio.update(d.talking || []);
    if (asked !== query(false).toString()) c.again = true;   // stale: the filters moved on
    else if (!d.unchanged) {
      c.rev = d.rev;
      c.rows = d.rows || [];
      fillFacets(d);
      render();
    }
  } catch (e) {
    $("#count").textContent = `could not load: ${e.message}`;
  } finally {
    c.busy = false;
    conn();
    if (c.again) { c.rev = ""; poll(); }
  }
}
function conn() {
  const p = $("#conn"), age = Date.now() - c.lastOk;
  p.className = "pill " + (!c.lastOk ? "warn" : age > LOST_AFTER_MS ? "err" : "ok");
  p.textContent = !c.lastOk ? "connecting…" : age > LOST_AFTER_MS ? "not updating" : "live";
}

for (const id of ["#f-unit", "#f-channel", "#f-event", "#f-type", "#f-pcfd", "#f-since", "#f-until"]) $(id).addEventListener("change", changed);
$("#f-clear").addEventListener("click", () => {
  $("#filters").reset();
  for (const id of ["#f-unit", "#f-channel", "#f-event"]) $(id).value = "";
  changed();
});
$("#filters").addEventListener("submit", (e) => e.preventDefault());
Radio.mountAlerts($("#alerts"));
fetch("/api/scene").then((r) => r.json()).then((s) => {
  const name = (s.station && s.station.name) || "sizeup";
  $("#station").textContent = name;
  document.title = `Comms · ${name}`;
}).catch(() => {});
fromUrl();
poll();
setInterval(poll, POLL_MS);
setInterval(conn, 1000);
