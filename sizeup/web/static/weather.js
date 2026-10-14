// Weather and water on the CAD and the wall board (GitHub #132). Everything comes from the hub
// (GET /api/cad/weather, sizeup/weather.py): the browser never talks to NOAA.
//
//   Banner       the National Weather Service's alerts for our zones (NYZ071, NYC119, NYZ211) under the header, in
//                the shared alert-banner box (#alert-banners: the station-health banner sits there too, #79): one line
//                per alert, most serious first, in its level's colour (a warning red, a watch or advisory amber, a
//                statement info), with the headline and when it ends; a click opens the full text (description,
//                what to do, onset, area). More than two: the rest behind "+N more". A marine-only alert (a Small
//                Craft Advisory) goes to the marine block instead.
//   Spoken       a Warning is said once through the dispatch voice where it is open: the console, and the wall board
//                with its Sound on (GET /api/cad/weather/speech: the hub renders it with the CAD's voice), only when
//                Settings > Dispatch > Weather warnings allows it (off by default). Played here, never keyed on a
//                radio: transmit stays off. Once per alert per browser (localStorage "sizeup.wxSpoken").
//   Conditions   "54°F · NW 12 G 22 mph" with a wind arrow in the header (the console and the board): KHPN's latest
//                observation; its time, humidity, visibility and sky in the tooltip; dimmed when it is old.
//   Maps         a "Weather" group in every map's Layers box (maplayers.js MapLayers.addGroup): Radar (NOAA/NWS MRMS
//                base reflectivity, tiles through the hub, off by default, key "wx:radar" in the viewer's or the
//                event's layer set), and Animate (the last hour, a frame every 10 min; per viewer). On an active or
//                pending event's map, a small wind indicator in the corner.
//   Tides        on a marine call (water rescue, an aircraft in the water, a boat fire, a marine unit on it: Marine 40)
//                and in the wall board's marine block: Rye Beach's predicted level now, rising or falling, the next
//                high and low with times; Kings Point's gauge and how far it runs above or below the table; marine
//                alerts. NOAA CO-OPS, MLLW, feet.
"use strict";

window.Weather = (() => {
  const POLL_MS = 60000;
  const SPOKEN_KEY = "sizeup.wxSpoken";
  const ANIM_KEY = "sizeup.wxRadarAnim";
  const SHOW = 2;                                   // banner lines before "+N more"
  const FRAME_MS = 700, HOLD_MS = 1800;             // the animation: each frame, and the latest held
  const TONE = { warning: "err", watch: "warn", advisory: "warn", statement: "info" };
  const st = { d: null, err: "", timer: null, loading: null, subs: new Set(), more: false, speaking: false };

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const $id = (id) => document.getElementById(id);
  const board = () => document.documentElement.classList.contains("board");
  const tz = () => { try { return (cad.scene && cad.scene.timezone) || "America/New_York"; } catch (_) { return "America/New_York"; } };
  function when(t, withDay) {                       // "14:13", "Thu 06:00" (another day)
    if (!t) return "";
    const d = new Date(t * 1000);
    const o = { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: tz() };
    const hm = new Intl.DateTimeFormat("en-US", o).format(d);
    const day = (x) => new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: tz() }).format(x);
    return withDay !== false && day(d) !== day(new Date()) ? `${new Intl.DateTimeFormat("en-US", { weekday: "short", timeZone: tz() }).format(d)} ${hm}` : hm;
  }
  function stored(key) { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch (_) { return null; } }
  function store(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (_) { /* this page view only */ } }

  // a wind arrow: where the wind blows to (it comes from `deg`)
  function arrow(deg, cls = "wx-arrow") {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", "M8 1.5 L12.5 9 H9.4 V14.5 H6.6 V9 H3.5 Z");
    p.setAttribute("fill", "currentColor");
    svg.append(p);
    svg.style.transform = `rotate(${(Number(deg) + 180) % 360}deg)`;
    return svg;
  }

  // ---- the data: one poll for the whole page ---------------------------------------------------------------------
  async function load() {
    if (st.loading) return st.loading;
    st.loading = (async () => {
      try {
        const r = await fetch("/api/cad/weather", { credentials: "same-origin", cache: "no-store" });
        if (!r.ok) throw new Error(`${r.status}`);
        st.d = await r.json();
        st.err = "";
      } catch (e) {
        st.err = e.message || "unavailable";          // an older hub, or offline: nothing shown, nothing breaks
      } finally {
        st.loading = null;
      }
      render();
      for (const fn of st.subs) { try { fn(st.d); } catch (e) { console.error(e); } }
      speakNew();
    })();
    return st.loading;
  }
  function start() {
    if (st.timer) return;
    load();
    st.timer = setInterval(() => { if (document.visibilityState !== "hidden") load(); }, POLL_MS);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(); });
  }
  function subscribe(fn) { st.subs.add(fn); if (st.d) fn(st.d); }

  function render() {
    renderBanner();
    renderNow();
    renderBoard();
    renderIncident();
  }

  // ---- the banner ------------------------------------------------------------------------------------------------
  function renderBanner() {
    const box = $id("wx-banner");
    if (!box) return;
    const alerts = (st.d && st.d.alerts) || [];
    const sig = JSON.stringify([alerts.map((a) => [a.id, a.until, a.headline]), st.more]);
    if (box._sig === sig) return;
    box._sig = sig;
    box.hidden = !alerts.length;
    box.replaceChildren();
    if (!alerts.length) return;
    const shown = st.more ? alerts : alerts.slice(0, SHOW);
    for (const a of shown) box.append(alertLine(a));
    if (alerts.length > SHOW) {
      const more = h("button", "quiet sm wx-more", st.more ? "Fewer" : `+${alerts.length - SHOW} more`);
      more.type = "button";
      more.setAttribute("aria-expanded", String(st.more));
      more.addEventListener("click", () => { st.more = !st.more; renderBanner(); });
      box.append(more);
    }
  }
  function alertLine(a) {
    const row = h("button", `notice ${TONE[a.level] || "info"} wx-alert`);
    row.type = "button";
    row.dataset.level = a.level;
    row.title = `${a.event}: the full text`;
    row.append(h("b", "wx-event", a.event), h("span", "wx-head", a.headline || ""),
               h("span", "wx-until", a.until ? `until ${when(a.until)}` : ""));
    row.addEventListener("click", () => openAlert(a, row));
    return row;
  }
  function openAlert(a, opener) {
    const ov = h("div", "overlay");
    const panel = h("div", "dialog wx-dialog");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", a.event);
    const head = h("div", "dialog-head");
    const title = h("h2", "dialog-title", a.event);
    const x = h("button", "icon quiet");
    x.type = "button";
    x.setAttribute("aria-label", "Close");
    x.append(window.Icon ? Icon("x") : "✕");
    head.append(title, h("span", "spacer"), x);
    const body = h("div", "dialog-body wx-text");
    body.append(h("p", `wx-level ${TONE[a.level] || "info"}`, `${a.level[0].toUpperCase()}${a.level.slice(1)} · ${a.severity} · ${a.urgency} · ${a.certainty}`));
    if (a.headline) body.append(h("p", "", a.headline));
    const facts = h("dl", "wx-facts");
    for (const [k, v] of [["Onset", when(a.onset)], ["Until", when(a.until)], ["Issued", when(a.sent)], ["Area", a.area], ["From", a.sender]]) {
      if (!v) continue;
      facts.append(h("dt", "dim", k), h("dd", "", v));
    }
    body.append(facts);
    if (a.nws_headline) body.append(h("p", "wx-nwshead", a.nws_headline));
    if (a.description) body.append(h("h3", "", "What"), h("pre", "wx-pre", a.description));
    if (a.instruction) body.append(h("h3", "", "What to do"), h("pre", "wx-pre", a.instruction));
    const foot = h("div", "dialog-foot");
    const link = h("a", "nav", "weather.gov");
    link.href = (st.d && st.d.link) || "https://www.weather.gov/okx/";
    link.target = "_blank";
    link.rel = "noopener";
    const close = h("button", "", "Close");
    close.type = "button";
    foot.append(h("span", "dim small", "National Weather Service"), h("span", "spacer"), link, close);
    panel.append(head, body, foot);
    ov.append(panel);
    document.body.append(ov);
    const done = () => UI.dialog.close(ov);
    x.addEventListener("click", done);
    close.addEventListener("click", done);
    UI.dialog.open(ov, { opener, focus: close, onClose: () => ov.remove() });
  }

  // ---- a warning, said once by the dispatch voice (played here; never keyed) --------------------------------------
  async function speakNew() {
    const d = st.d;
    if (!d || !d.speak || st.speaking) return;
    if (board() && !(window.RadioLive && RadioLive.on)) return;           // the board: only with its Sound on
    const said = stored(SPOKEN_KEY) || {};
    const now = Date.now() / 1000;
    for (const k of Object.keys(said)) if (now - said[k] > 7 * 86400) delete said[k];
    const next = (d.alerts || []).find((a) => a.level === "warning" && !said[a.id]);
    if (!next) return store(SPOKEN_KEY, said);
    said[next.id] = now;                                                    // once, whatever comes of it
    store(SPOKEN_KEY, said);
    st.speaking = true;
    try {
      const r = await fetch(`/api/cad/weather/speech?id=${encodeURIComponent(next.id)}`, { credentials: "same-origin" });
      const s = await r.json();
      if (r.ok && s.audio_url && typeof play === "function") play(s.audio_url);
      else if (r.ok && window.UI) UI.toast(`${next.event}: not spoken (${s.audio_note || "no voice"})`, "err");
    } catch (e) {
      console.warn("weather speech:", e);
    } finally {
      st.speaking = false;
    }
  }

  // ---- conditions in the header -----------------------------------------------------------------------------------
  function renderNow() {
    const n = $id("wx-now");
    if (!n) return;
    const c = st.d && st.d.conditions;
    const sig = JSON.stringify(c);
    if (n._sig === sig) return;
    n._sig = sig;
    n.hidden = !c || !c.short;
    n.replaceChildren();
    if (!c || !c.short) return;
    if (c.wind_deg !== null && c.wind_deg !== undefined && c.wind_mph) n.append(arrow(c.wind_deg));
    n.append(h("span", "", c.short));
    n.classList.toggle("stale", !!c.stale);
    n.title = [`${c.name || c.station} at ${when(c.at)}${c.stale ? " (old)" : ""}`, c.text, c.humidity != null ? `Humidity ${c.humidity}%` : "",
               c.visibility_mi != null ? `Visibility ${c.visibility_mi} mi` : "", "National Weather Service"].filter(Boolean).join(" · ");
  }

  // ---- tides: the marine block --------------------------------------------------------------------------------------
  const MARINE = [["RESCUE", "WATER"], ["AIRPORT", "ACFT-WATER"], ["MVF", "BOAT"]];
  function isMarine(inc) {
    if (!inc) return false;
    if (MARINE.some(([t, s]) => inc.type === t && inc.subtype === s) || inc.subtype === "BOAT") return true;
    try {
      return ((cad.state && cad.state.units) || []).some((u) => u.incident_id === inc.id && u.kind === "marine");   // Marine 40 on it
    } catch (_) { return false; }
  }
  function ft(v) { return v === null || v === undefined ? "?" : `${Number(v).toFixed(1)} ft`; }
  function tideBlock(d) {
    const t = d && d.tides;
    const box = h("div", "wx-tides-in");
    if (!t) {
      box.append(h("span", "dim small", st.err ? "Tides unavailable" : "Tides: waiting for NOAA"));
      return box;
    }
    const now = h("div", "wx-tide-now");
    const trend = h("span", `wx-trend ${t.trend}`, t.trend === "rising" ? "↑ rising" : "↓ falling");
    now.append(h("span", "dim", `${t.name} tide`), h("b", "", ft(t.ft)), trend);
    box.append(now);
    const nx = h("div", "wx-tide-next");
    for (const x of t.next || []) {
      const s = h("span", "wx-ext");
      s.append(h("span", "dim", x.type === "high" ? "High" : "Low"), h("b", "", when(x.t)), h("span", "", ft(x.ft)));
      nx.append(s);
    }
    box.append(nx);
    const g = t.gauge;
    if (g) {
      const off = g.offset_ft === null || g.offset_ft === undefined ? "" : `, ${g.offset_ft >= 0 ? "+" : ""}${g.offset_ft.toFixed(1)} ft vs. the table`;
      box.append(h("div", "small" + (g.stale ? " warn" : " dim"), `${g.name} gauge ${ft(g.ft)}${off} (${when(g.at)}${g.stale ? ", old" : ""})`));
    }
    for (const a of (d.marine_alerts || [])) {
      const m = h("button", `notice ${TONE[a.level] || "info"} wx-alert wx-marine`);
      m.type = "button";
      m.append(h("b", "wx-event", a.event), h("span", "wx-until", a.until ? `until ${when(a.until)}` : ""));
      m.addEventListener("click", () => openAlert(a, m));
      box.append(m);
    }
    const src = h("a", "dim small", `NOAA Tides & Currents, ${t.datum}`);
    src.href = t.link;
    src.target = "_blank";
    src.rel = "noopener";
    box.append(src);
    return box;
  }
  function fill(box, show) {
    if (!box) return;
    box.hidden = !show;
    if (!show) return;
    const sig = JSON.stringify([st.d && st.d.tides, st.d && st.d.marine_alerts, st.err]);
    if (box._sig === sig) return;
    box._sig = sig;
    const body = box.querySelector(".wx-tides-body") || box;
    body.replaceChildren(tideBlock(st.d));
  }
  function renderIncident() {
    let inc = null;
    try { inc = cad.mode === "view" && typeof selected === "function" ? selected() : null; } catch (_) { /* not the console */ }
    fill($id("d-tides"), isMarine(inc) && !!st.d);
  }
  function renderBoard() {
    if (board()) fill($id("b-marine"), !!st.d && st.d.configured !== false);
  }

  // ---- the maps: radar in the Layers box, the wind on an event's map ------------------------------------------------
  const RADAR = "wx:radar";
  const prefsAnim = () => stored(ANIM_KEY) === true;

  class WxMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.frames = [];
      this.frame = -1;
      this.anim = null;
      this.added = false;
      this.wind = null;
      subscribe(() => this.refresh());
    }
    keys() { return [[RADAR, false]]; }                 // for the event's own layer set (maplayers.js #111)
    on() { return this.control.on(RADAR, false); }
    radar() { return (st.d && st.d.radar) || null; }
    ready() { try { return this.map.isStyleLoaded() || this.map.loaded() || this.added; } catch (_) { return false; } }
    below() {                                           // under the labels and every mark
      const ls = (this.map.getStyle() && this.map.getStyle().layers) || [];
      const sym = ls.find((l) => l.type === "symbol" || l.id === "firehouse");
      return sym ? sym.id : undefined;
    }
    addRadar(id, frame) {
      const r = this.radar();
      if (this.map.getSource(id)) return;
      this.map.addSource(id, { type: "raster", tileSize: 256, minzoom: r.minzoom, maxzoom: r.maxzoom,
        tiles: [location.origin + r.tiles.replace("{frame}", frame)], attribution: r.attribution });
      this.map.addLayer({ id, type: "raster", source: id, layout: { visibility: "none" },
        paint: { "raster-opacity": 0, "raster-fade-duration": 0, "raster-resampling": "linear" } }, this.below());
      this.added = true;
    }
    refresh() {
      this.syncRadar();
      this.syncWind();
    }
    syncRadar() {
      const r = this.radar();
      if (!r || !r.on) return;
      if (!this.ready()) { this.map.once("idle", () => this.syncRadar()); return; }
      const on = this.on();
      try {
        if (on) this.addRadar("wx-radar", "now");
        const animate = on && prefsAnim();
        if (animate && !this.anim) this.startAnim();
        if (!animate && this.anim) this.stopAnim();
        if (this.map.getLayer("wx-radar")) {
          this.map.setLayoutProperty("wx-radar", "visibility", on ? "visible" : "none");
          this.map.setPaintProperty("wx-radar", "raster-opacity", on && !this.anim ? 0.62 : 0);
        }
        if (!on) for (const f of this.frames) if (this.map.getLayer(f.layer)) this.map.setLayoutProperty(f.layer, "visibility", "none");
      } catch (e) { console.error("radar", e); }
    }
    async startAnim() {
      this.anim = { timer: null, label: h("div", "wx-frame", "") };
      this.control.host.append(this.anim.label);
      let frames = [];
      try {
        const r = await fetch("/api/cad/weather/radar/frames", { credentials: "same-origin" });
        frames = ((await r.json()).frames) || [];
      } catch (_) { frames = []; }
      if (!this.anim) return;
      if (!frames.length) { this.stopAnim(); return; }
      this.frames = frames.map((f) => ({ ...f, layer: `wx-radar-${f.id}` }));
      for (const f of this.frames) {
        this.addRadar(f.layer, f.id);
        this.map.setLayoutProperty(f.layer, "visibility", "visible");
      }
      this.frame = -1;
      const step = () => {
        if (!this.anim) return;
        this.frame = (this.frame + 1) % this.frames.length;
        this.frames.forEach((f, i) => { if (this.map.getLayer(f.layer)) this.map.setPaintProperty(f.layer, "raster-opacity", i === this.frame ? 0.62 : 0); });
        this.anim.label.textContent = `Radar ${when(this.frames[this.frame].t, false)}`;
        this.anim.timer = setTimeout(step, this.frame === this.frames.length - 1 ? HOLD_MS : FRAME_MS);
      };
      this.syncRadar();
      step();
    }
    stopAnim() {
      if (!this.anim) return;
      clearTimeout(this.anim.timer);
      this.anim.label.remove();
      this.anim = null;
      for (const f of this.frames) {
        try { if (this.map.getLayer(f.layer)) this.map.removeLayer(f.layer); if (this.map.getSource(f.layer)) this.map.removeSource(f.layer); } catch (_) { /* gone */ }
      }
      this.frames = [];
      this.syncRadar();
    }
    // the wind on an active (or pending) event's map: KHPN's, an arrow and the words
    syncWind() {
      const inc = this.control.focused;
      const c = st.d && st.d.conditions;
      const show = !!inc && (inc.status === "active" || inc.status === "pending") && !!c && !!c.wind;
      if (!show) { if (this.wind) { this.wind.remove(); this.wind = null; } return; }
      if (!this.wind) {
        this.wind = h("div", "wx-wind");
        this.wind.setAttribute("role", "img");
        this.control.host.append(this.wind);
      }
      const sig = JSON.stringify([c.wind, c.wind_deg, c.at]);
      if (this.wind._sig === sig) return;
      this.wind._sig = sig;
      this.wind.replaceChildren();
      if (c.wind_deg !== null && c.wind_deg !== undefined && c.wind_mph) this.wind.append(arrow(c.wind_deg, "wx-arrow lg"));
      this.wind.append(h("span", "", c.wind));
      const label = `Wind ${c.wind}${c.wind_dir ? ` from the ${c.wind_dir}` : ""}, ${c.station} at ${when(c.at)}`;
      this.wind.title = label;
      this.wind.setAttribute("aria-label", label);
      this.wind.classList.toggle("stale", !!c.stale);
    }
    // the group in the Layers box
    panel() {
      const r = this.radar();
      const box = h("div", "ml-group");
      const head = h("div", "ml-ghead");
      head.append(h("b", "", "Weather"));
      const src = h("a", "dim small", "NOAA / NWS");
      src.href = (r && r.link) || "https://radar.weather.gov/";
      src.target = "_blank";
      src.rel = "noopener";
      head.append(h("span", "spacer"), src);
      box.append(head);
      if (!r || !r.on) { box.append(h("div", "dim small", st.err ? "Weather unavailable here" : "Radar off on the hub")); return box; }
      const lab = h("label", "check");
      const cb = h("input");
      cb.type = "checkbox";
      cb.checked = this.on();
      cb.dataset.key = RADAR;
      cb.addEventListener("change", () => this.control.setLayer(RADAR, cb.checked));
      lab.append(cb, h("span", "", "Radar"));
      box.append(lab);
      const an = h("label", "check ml-sub-check");
      const acb = h("input");
      acb.type = "checkbox";
      acb.checked = prefsAnim();
      acb.disabled = !this.on();
      acb.dataset.key = "wx:anim";
      acb.addEventListener("change", () => { store(ANIM_KEY, acb.checked); for (const m of maps) m.refresh(); });
      an.append(acb, h("span", "", "Animate the last hour"));
      box.append(an);
      box.append(h("div", "ml-note", `${r.label}, every 2 min. Public domain (NOAA).`));
      return box;
    }
  }
  const maps = new Set();
  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => { const m = new WxMap(control); maps.add(m); return m; } });

  // the console's renders (cad.js calls its hooks on every state push): the incident's tides follow the selection
  document.addEventListener("DOMContentLoaded", () => {
    try { if (typeof cad !== "undefined" && Array.isArray(cad.hooks)) cad.hooks.push(renderIncident); } catch (_) { /* not the CAD page */ }
    if ($id("wx-banner") || $id("wx-now")) start();
  });

  return { load, data: () => st.d, isMarine, tideBlock, openAlert, maps, TONE };
})();
