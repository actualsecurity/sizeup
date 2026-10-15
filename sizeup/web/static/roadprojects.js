// Road projects & closures on the CAD maps and in the incident view (GitHub #185). Everything comes from the hub
// (GET /api/cad/roadprojects, sizeup/roadprojects.py): the browser never talks to a source.
//
//   Layer        "Road projects & closures" in every map's Layers box (maplayers.js MapLayers.addGroup). Key "roadwork":
//                the old Roadwork layer's, so each viewer's and each event's choice carries over (the Thruway's
//                roadwork is one of this layer's sources now, drawn once). Off by default. Lines where the source
//                gives a stretch of road, a mark where it gives a point. Active now: solid, in the closure's colour
//                (a full closure --err, lanes and the rest --warn); upcoming: dashed / hollow, --info. A filter:
//                Today (active now, or starting before midnight), This week (the next 7 days), All; per viewer.
//                Polled every POLL_MS only while a map has it on and the page is visible; the state (active /
//                upcoming) is worked out here from the hub's windows every minute, in the console's timezone.
//   Click        the item's box: what, where, active now or when it starts, its dates and daily hours exactly as
//                the source gives them ("no end time given" when it gives none), the closure and lanes, the detour,
//                the source's own text, the source and when it updated the item.
//   Panel        sources and when each read; notices that name no place we can find (a village's road notices):
//                listed with their date and link, never put on the map by guess.
//   Incident     #d-roads under the event's address: a line per closure or project in effect near it (the hub's
//                near(): 300 m, or on its street): "Road closed: Westchester Ave between X and Y until 3:00 PM
//                (Port Chester)". Asked once per event and every NOTE_MS while it is shown.
"use strict";

window.RoadProjects = (() => {
  const URL = "/api/cad/roadprojects", NEAR = "/api/cad/roadprojects/near";
  const KEY = "roadwork";
  const FILTER_KEY = "sizeup.roadFilter";
  const POLL_MS = 300000, TICK_MS = 60000, NOTE_MS = 300000;
  const FILTERS = [["today", "Today"], ["week", "This week"], ["all", "All"]];
  const SRC = "rw-items";
  const IDS = ["rw-fill", "rw-outline", "rw-line-case", "rw-line", "rw-line-next", "rw-point"];
  const HALO = "#15181B";
  const CLOSURE = { full: "Road closed", lanes: "Lanes closed", shoulder: "Shoulder closed", ramp: "Ramp closed",
                    alternating: "Alternating one-way traffic", detour: "Detour" };
  const KIND = { roadwork: "Roadwork", closure: "Closure", detour: "Detour", event: "Event", notice: "Notice", project: "Construction project" };

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const $id = (id) => document.getElementById(id);
  function token(name, fallback) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch (_) { return fallback; }
  }
  // a remembered value for this viewer (the filter), or null when storage is missing or refuses
  function stored(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
  function store(key, v) { try { localStorage.setItem(key, v); } catch (_) { /* this page view only */ } }
  const tz = () => { try { return (cad.scene && cad.scene.timezone) || "America/New_York"; } catch (_) { return "America/New_York"; } };
  function clock(t) {
    return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: tz() }).format(new Date(t * 1000));
  }
  const dayOf = (t) => new Intl.DateTimeFormat("en-US", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: tz() }).format(new Date(t * 1000));
  function dayClock(t, withDay) {                    // "3:00 PM" today, "Sat Oct 18, 3:00 PM" another day
    if (t == null) return "";
    const d = new Date(t * 1000);
    if (!withDay && dayOf(t) === dayOf(Date.now() / 1000)) return clock(t);
    return `${new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: tz() }).format(d)}, ${clock(t)}`;
  }
  function dayText(day) {                             // "2027-10-26" -> "Oct 26, 2027" (a date, no time)
    const [y, m, d] = String(day).split("-").map(Number);
    if (!y || !m || !d) return String(day);
    return `${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1]} ${d}, ${y}`;
  }
  function endOfToday(now) {                          // midnight tonight in the console's timezone (unix s)
    const parts = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "numeric", second: "numeric", hourCycle: "h23", timeZone: tz() })
      .formatToParts(new Date(now * 1000));
    const n = (t) => Number((parts.find((p) => p.type === t) || {}).value || 0);
    return now - (n("hour") * 3600 + n("minute") * 60 + n("second")) + 86400;
  }

  // ---- the state of an item from its windows (the hub's): active now, upcoming, ended ---------------------------
  function stateOf(it, now) {
    for (const [s, e] of it.windows || []) if ((s == null || s <= now) && (e == null || now < e)) return "active";
    return (it.windows || []).length ? "upcoming" : "ended";
  }
  function nextStart(it, now) {
    const w = (it.windows || []).find(([s]) => s != null && s > now);
    return w ? w[0] : null;
  }
  function current(it, now) {
    return (it.windows || []).find(([s, e]) => (s == null || s <= now) && (e == null || now < e)) || null;
  }
  function shown(it, filter, now) {
    const st = stateOf(it, now);
    if (st === "ended") return false;
    if (filter === "all") return true;
    if (it.kind === "notice" && !(it.windows || []).some(([s, e]) => s != null || e != null)) return false;   // no dates: All only
    if (st === "active") return true;
    const s = nextStart(it, now);
    return s != null && s < (filter === "today" ? endOfToday(now) : now + 7 * 86400);
  }
  const filter = () => { const f = stored(FILTER_KEY); return FILTERS.some(([k]) => k === f) ? f : "week"; };

  // ---- the data: one copy for every map, fetched only while one has the layer on ---------------------------------
  const st = { data: null, timer: null, tick: null, loading: null, err: "" };
  const maps = new Set();
  const anyOn = () => { for (const m of maps) if (m.isOn()) return true; return false; };
  async function load() {
    if (st.loading) return st.loading;
    st.loading = (async () => {
      try {
        const r = await fetch(URL, { credentials: "same-origin" });      // the browser revalidates (ETag): a 304 is the same body
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        st.data = await r.json();
        st.err = "";
      } catch (e) {
        st.err = String(e.message || e);
        if (!st.data) st.data = null;
      } finally {
        st.loading = null;
        for (const m of maps) m.sync();
      }
    })();
    return st.loading;
  }
  function kick() {                                  // a map turned it on (or the page came back)
    clearInterval(st.timer);
    clearInterval(st.tick);
    st.timer = st.tick = null;
    if (!anyOn()) return;
    load();
    st.timer = setInterval(() => { if (document.visibilityState !== "hidden" && anyOn()) load(); }, POLL_MS);
    st.tick = setInterval(() => { if (document.visibilityState !== "hidden") for (const m of maps) m.draw(); }, TICK_MS);
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && anyOn()) kick(); });

  // ---- the marks: drawn once, in the design's colours ------------------------------------------------------------------
  function canvas(w, ht, draw) {
    const c = document.createElement("canvas");
    c.width = w; c.height = ht;
    const g = c.getContext("2d");
    draw(g);
    return g.getImageData(0, 0, w, ht);
  }
  const DARK = "rgba(12, 14, 16, 0.94)";
  function icons() {
    const warn = token("--warn", "#E0A030"), err = token("--err", "#EE6475"), info = token("--info", "#6AA6E8");
    const diamond = (fill, ring, hollow) => canvas(42, 42, (g) => {          // a work-zone diamond
      g.translate(21, 21); g.rotate(Math.PI / 4);
      g.fillStyle = hollow ? DARK : fill; g.strokeStyle = hollow ? ring : DARK; g.lineWidth = 3;
      g.beginPath(); g.roundRect(-12.5, -12.5, 25, 25, 3); g.fill(); g.stroke();
      g.rotate(-Math.PI / 4); g.fillStyle = hollow ? ring : DARK;
      g.beginPath(); g.arc(-1, -6, 2.6, 0, 2 * Math.PI); g.fill();
      g.fillRect(-3.5, -2.5, 5, 7.5); g.fillRect(-8, 6, 16, 2.4);
    });
    const closed = (fill, hollow) => canvas(40, 40, (g) => {                 // no entry: a full closure
      g.fillStyle = hollow ? DARK : fill; g.strokeStyle = hollow ? fill : DARK; g.lineWidth = 3;
      g.beginPath(); g.arc(20, 20, 17, 0, 2 * Math.PI); g.fill(); g.stroke();
      g.fillStyle = hollow ? fill : "#F2F3F5"; g.fillRect(9, 16.5, 22, 7);
    });
    const notice = canvas(40, 40, (g) => {                                    // a notice: a sheet with lines
      g.fillStyle = DARK; g.strokeStyle = warn; g.lineWidth = 3;
      g.beginPath(); g.roundRect(8, 4, 24, 32, 3); g.fill(); g.stroke();
      g.fillStyle = warn; for (const y of [12, 18, 24]) g.fillRect(13, y, 14, 2.4);
    });
    return { "rw-work": diamond(warn), "rw-work-next": diamond(info, info, true), "rw-closed": closed(err),
             "rw-closed-next": closed(info, true), "rw-notice": notice };
  }
  const iconFor = (it, active) => (it.kind === "notice" && !it.closure ? "rw-notice"
    : it.closure === "full" ? (active ? "rw-closed" : "rw-closed-next") : (active ? "rw-work" : "rw-work-next"));
  const fc = (features) => ({ type: "FeatureCollection", features });

  class RoadMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.added = false;
      this.sig = "";
      maps.add(this);
      this.map.on("click", (e) => this.clicked(e));
      if (this.isOn()) kick();
    }
    isOn() { return this.control.on(KEY, false); }
    keys() { return [[KEY, false]]; }
    refresh() { this.sync(); kick(); }
    ready() { try { return this.added || this.map.isStyleLoaded() || this.map.loaded(); } catch (_) { return false; } }

    add() {
      const map = this.map;
      if (map.getSource(SRC)) { this.added = true; return; }
      for (const [name, img] of Object.entries(icons())) if (!map.hasImage(name)) map.addImage(name, img, { pixelRatio: 2 });
      const ids = (map.getStyle().layers || []).map((x) => x.id);
      const before = ids.find((id) => id.startsWith("ml-") || id === "cm-lines" || id === "firehouse");
      map.addSource(SRC, { type: "geojson", data: fc([]) });
      const vis = { visibility: "none" };
      const line = ["==", ["geometry-type"], "LineString"];
      const color = ["case", ["!", ["get", "active"]], token("--info", "#6AA6E8"),
                     ["==", ["get", "closure"], "full"], token("--err", "#EE6475"), token("--warn", "#E0A030")];
      const width = ["interpolate", ["linear"], ["zoom"], 11, 3, 15, 6, 18, 10];
      const casing = ["interpolate", ["linear"], ["zoom"], 11, 6, 15, 9, 18, 13];
      const now = ["==", ["get", "active"], true];
      const area = ["==", ["geometry-type"], "Polygon"];          // a project's footprint: faint, a dashed edge
      map.addLayer({ id: "rw-fill", type: "fill", source: SRC, filter: area, layout: vis,
                     paint: { "fill-color": color, "fill-opacity": 0.1 } }, before);
      map.addLayer({ id: "rw-outline", type: "line", source: SRC, filter: area, layout: vis,
                     paint: { "line-color": color, "line-width": 1.5, "line-dasharray": [2, 1.5], "line-opacity": 0.8 } }, before);
      map.addLayer({ id: "rw-line-case", type: "line", source: SRC, filter: line, layout: { ...vis, "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": HALO, "line-width": casing, "line-opacity": 0.85 } }, before);
      map.addLayer({ id: "rw-line", type: "line", source: SRC, filter: ["all", line, now], layout: { ...vis, "line-join": "round" },
                     paint: { "line-color": color, "line-width": width } }, before);
      map.addLayer({ id: "rw-line-next", type: "line", source: SRC, filter: ["all", line, ["!", now]], layout: { ...vis, "line-join": "round" },
                     paint: { "line-color": color, "line-width": width, "line-dasharray": [1.2, 1.2] } }, before);     // upcoming: dashed
      map.addLayer({ id: "rw-point", type: "symbol", source: SRC, filter: ["==", ["get", "mark"], true],
                     layout: { ...vis, "icon-image": ["get", "icon"], "icon-allow-overlap": true, "icon-ignore-placement": true,
                               "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.55, 14, 0.8, 18, 1] } }, before);
      for (const id of IDS) {
        map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
      }
      this.added = true;
    }

    sync() {
      if (!this.isOn() && !this.added) { this.panelChanged(); return; }
      if (!this.ready()) { this.map.once("idle", () => this.sync()); return; }
      try { this.add(); } catch (e) { console.error("road projects layer", e); return; }
      const on = this.isOn();
      for (const id of IDS) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
      this.draw();
      this.panelChanged();
    }
    draw() {
      const d = st.data;
      if (!this.added || !this.isOn() || !d) return;
      const now = Date.now() / 1000, f = filter();
      const feats = [];
      const order = [...(d.items || [])].sort((a, b) => (b.kind === "notice") - (a.kind === "notice"));   // notices underneath
      for (const it of order) {
        if (!it.geometry || !shown(it, f, now)) continue;
        const active = stateOf(it, now) === "active";
        const props = { id: it.id, active, closure: it.closure || "", icon: iconFor(it, active) };
        const g = it.geometry;
        if (g.type === "Point") feats.push({ type: "Feature", geometry: g, properties: { ...props, mark: true } });
        else if (g.type === "Polygon" || g.type === "MultiPolygon") {
          for (const c of g.type === "Polygon" ? [g.coordinates] : g.coordinates) feats.push({ type: "Feature", geometry: { type: "Polygon", coordinates: c }, properties: props });
          if (it.lat != null) feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [it.lon, it.lat] }, properties: { ...props, mark: true } });
        } else {
          const lines = g.type === "MultiLineString" ? g.coordinates : [g.coordinates];
          for (const c of lines) feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: c }, properties: props });
          if (it.lat != null) feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [it.lon, it.lat] }, properties: { ...props, mark: true } });
        }
      }
      const sig = JSON.stringify(feats.map((x) => [x.properties.id, x.properties.active, x.properties.icon, x.geometry.type]));
      if (sig === this.sig && this.data === d) return;
      this.sig = sig;
      this.data = d;
      const src = this.map.getSource(SRC);
      if (src) src.setData(fc(feats));
    }
    panelChanged() { if (!this.control.panel.hidden) this.control.renderPanel(); }

    clicked(e) {
      if (!this.isOn() || !this.added || !st.data) return;
      const layers = IDS.filter((id) => this.map.getLayer(id));
      const hits = this.map.queryRenderedFeatures(e.point, { layers });
      if (!hits.length) return;
      e.originalEvent && e.originalEvent.stopPropagation();
      const id = (hits.find((x) => x.layer.id === "rw-point") || hits[0]).properties.id;
      setTimeout(() => this.show(id), 0);             // after the map's own click (it closes the box)
    }

    show(id) {
      const it = ((st.data && st.data.items) || []).find((x) => x.id === id);
      if (!it) return;
      this.control.close();
      const [title, sub, lines, foot] = describe(it, st.data, Date.now() / 1000);
      this.control.showInfo(title, sub, lines, foot, "rw-info");
    }

    panel() {
      const d = st.data;
      const box = h("div", "ml-group rw-group");
      const head = h("div", "ml-ghead");
      head.append(h("b", "", "Road projects & closures"));
      box.append(head);
      const lab = h("label", "check");
      const cb = h("input");
      cb.type = "checkbox";
      cb.checked = this.isOn();
      cb.dataset.key = KEY;
      cb.addEventListener("change", () => { this.control.setLayer(KEY, cb.checked); kick(); });
      lab.append(cb, h("span", "", "Road projects & closures"));
      box.append(lab);
      if (!this.isOn()) return box;
      const seg = h("div", "seg rw-filter");
      seg.setAttribute("role", "group");
      seg.setAttribute("aria-label", "Show");
      const f = filter();
      for (const [k, label] of FILTERS) {
        const b = h("button", "sm", label);
        b.type = "button";
        b.dataset.key = `rw:${k}`;
        b.setAttribute("aria-pressed", String(k === f));
        b.addEventListener("click", () => { store(FILTER_KEY, k); for (const m of maps) { m.draw(); m.panelChanged(); } });
        seg.append(b);
      }
      box.append(seg);
      if (!d) { box.append(h("div", "ml-note", st.err ? `Unavailable here (${st.err})` : "Loading…")); return box; }
      const now = Date.now() / 1000;
      const items = d.items || [];
      const nAct = items.filter((it) => it.geometry && shown(it, f, now) && stateOf(it, now) === "active").length;
      const nNext = items.filter((it) => it.geometry && shown(it, f, now) && stateOf(it, now) === "upcoming").length;
      const legend = h("div", "ml-note rw-legend");
      const sw = (cls, text) => { const s = h("span", `rw-key ${cls}`); legend.append(s, ` ${text}  `); };
      sw("rw-key-closed", `closed now`);
      sw("rw-key-work", `lanes / work now`);
      sw("rw-key-next", `upcoming`);
      box.append(h("div", "ml-note", `${nAct} in effect now, ${nNext} upcoming${f === "all" ? "" : f === "today" ? " today" : " this week"}.`), legend);
      for (const s of d.sources || []) {
        const line = h("div", "ml-note rw-source");
        const a = h("a", "", s.label);
        a.href = s.link;
        a.target = "_blank";
        a.rel = "noopener";
        const what = !s.on ? `: ${s.why || "off"}` : s.error ? `: ${s.error}` : s.at ? `, updated ${dayClock(s.at)} (${s.n})` : ", waiting for the first read";
        line.append(a, what);
        if (s.on && s.error) line.classList.add("rw-err");
        box.append(line);
      }
      const unplaced = d.notices || [];
      if (unplaced.length) {
        const fold = h("details", "ml-fold rw-notices");
        const sum = h("summary", "ml-sub", "Notices not on the map");
        sum.append(h("span", "ml-count", String(unplaced.length)));
        fold.append(sum);
        for (const n of unplaced) {
          const row = h("div", "rw-notice");
          const a = h("a", "", n.title);
          a.href = n.link;
          a.target = "_blank";
          a.rel = "noopener";
          row.append(a, h("span", "dim small", ` ${n.source}${n.updated ? `, posted ${dayClock(n.updated, true)}` : ""}`));
          if (n.place_how) row.append(h("div", "ml-note", n.place_how));
          fold.append(row);
        }
        box.append(fold);
      }
      if (d.note) box.append(h("div", "ml-note", d.note));
      return box;
    }
  }

  // the click box: [title, sub, lines, foot]
  function describe(it, d, now) {
    const stt = stateOf(it, now), cur = current(it, now), next = nextStart(it, now);
    const title = (CLOSURE[it.closure] || KIND[it.kind] || "Roadwork") + (it.road ? `: ${it.road}` : "");
    const where = [it.cross_from && it.cross_to ? `between ${it.cross_from} and ${it.cross_to}` : it.cross_from ? `at ${it.cross_from}` : "",
                   it.dir].filter(Boolean).join(", ");
    const lines = [];
    if (stt === "active" && it.kind === "project") lines.push("In construction");
    else if (stt === "active") lines.push(cur && cur[1] != null ? `In effect now, until ${dayClock(cur[1])}` : "In effect now, no end time given");
    else if (stt === "upcoming" && next != null) lines.push(`Upcoming: starts ${dayClock(next, true)}`);
    else if (it.kind === "notice") lines.push("No dates given as data: see the notice");
    const endText = it.end_day ? `${it.end_estimated ? "estimated completion" : "completion date"} ${dayText(it.end_day)}`
      : it.end != null ? dayClock(it.end, true) : `no end time given${it.end_text ? ` (${it.end_text})` : ""}`;
    const dates = `${it.start != null ? dayClock(it.start, true) : "start not given"} to ${endText}`;
    if (it.kind !== "notice" || it.start != null || it.end != null) lines.push(`Dates: ${dates}`);
    if (it.kind === "project") lines.push("No daily hours, lanes or closures given by the source");
    if ((it.facts || []).length) lines.push(it.facts.join(" · "));
    if (it.start_text && it.start == null) lines.push(`Start, as the source says: ${it.start_text}`);
    for (const hr of it.hours || []) {
      const days = (hr.days || []).length ? hr.days.map((x) => ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][x]).join(", ") : "every day";
      lines.push(`Hours: ${hr.from} to ${hr.to}, ${days}`);
    }
    if (it.hours_text) lines.push(`Hours, as the source says: ${it.hours_text}`);
    if (it.lanes) lines.push(`Lanes: ${it.lanes}`);
    if (it.detour) lines.push(`Detour: ${it.detour}`);
    if (it.description && it.description !== it.title) lines.push(it.description);
    else if (it.title && it.title !== title) lines.push(it.title);
    if (it.place_how) lines.push(`On the map: ${it.place_how}`);
    const src = ((d && d.sources) || []).find((s) => s.short === it.source);
    const also = (it.also || []).length ? ` Also from ${it.also.join(", ")}.` : "";
    if (it.link) {
      const row = h("div", "rw-link");
      const a = h("a", "", "The source's page");
      a.href = it.link;
      a.target = "_blank";
      a.rel = "noopener";
      row.append(a);
      if (it.flyer) {
        const f = h("a", "", "the flyer");
        f.href = it.flyer;
        f.target = "_blank";
        f.rel = "noopener";
        row.append(" · ", f);
      }
      lines.push(row);
    }
    const foot = `${src ? src.label : it.source}${it.updated ? `, updated ${dayClock(it.updated, true)}` : ""}.${also} Times in ${tz().replace("_", " ")}.`;
    return [title, where, lines, foot];
  }

  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach: (control) => new RoadMap(control) });

  // ---- the incident view: a line per closure or project in effect near the event ----------------------------------
  const note = { key: "", data: null, seq: 0, timer: null, inc: null };
  function ask(host) {
    const inc = note.inc, key = note.key, seq = ++note.seq;
    if (!inc || !key) return;
    const p = new URLSearchParams({ lat: String(inc.lat), lon: String(inc.lon), address: inc.address || "" });
    if (inc.created_at) p.set("from", String(Math.floor(inc.created_at)));
    if (inc.closed_at) p.set("to", String(Math.floor(inc.closed_at)));
    fetch(`${NEAR}?${p}`, { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (note.key !== key || seq !== note.seq) return;
      note.data = d;
      drawNote(host);
    }).catch(() => { /* keep the last answer; the next turn asks again */ });
  }
  function drawNote(host) {
    const items = (note.data && note.data.items) || [];
    host.hidden = !items.length;
    const sig = JSON.stringify(items.map((x) => [x.id, x.text]));
    if (host._sig === sig) return;
    host._sig = sig;
    host.replaceChildren(...items.map((x) => {
      const now = x.active_now && x.kind !== "project" && x.kind !== "notice";      // a closure in effect: in its colour
      const row = h("div", `rw-line ${now ? (x.closure === "full" ? "rw-now rw-closed" : "rw-now") : "rw-later"}`);
      row.append(h("b", "", x.text));
      if (x.how) row.append(h("span", "dim small", ` · ${x.how}`));
      return row;
    }));
  }
  function renderIncident() {
    const host = $id("d-roads");
    if (!host) return;
    let inc = null;
    try { inc = cad.mode === "view" && typeof selected === "function" ? selected() : null; } catch (_) { /* not the console */ }
    const ok = inc && inc.lat != null && inc.lon != null;
    const key = ok ? JSON.stringify([inc.id, inc.lat, inc.lon, inc.address, inc.closed_at || 0]) : "";
    note.inc = ok ? inc : null;
    if (!ok) { note.key = ""; note.data = null; host.hidden = true; host.replaceChildren(); host._sig = ""; return; }
    if (key === note.key) return;
    note.key = key;
    note.data = null;
    host.hidden = true;
    host._sig = "";
    ask(host);
    clearInterval(note.timer);
    note.timer = setInterval(() => { if (note.key && document.visibilityState !== "hidden") ask(host); }, NOTE_MS);
  }
  document.addEventListener("DOMContentLoaded", () => {
    try { if (typeof cad !== "undefined" && Array.isArray(cad.hooks)) cad.hooks.push(renderIncident); } catch (_) { /* not the CAD page */ }
  });

  return { load, maps, data: () => st.data, KEY, stateOf, shown, describe, renderIncident };
})();
