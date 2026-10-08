// sizeup CAD maps: the working event's map (bigger, with an Expand overlay), the Overview page
// (every open call: a large map, a card each, who is talking) and the rigs on the monitor's map.
//
// Every map shows everything (the open incidents' pins, every rig the server placed) and focuses on
// the current call. Where a rig goes is decided on the server (sizeup/cad/mapview.py, state.map):
//
//   available / out of service      at its home firehouse (greyed when out of service)
//   dispatched / responding         at its firehouse, with a dashed line to its call
//   on location                     fanned out around the call's pin
//   on a call, no home station      the outer ring at the call (outlined: not there yet)
//   live position (future AVL)      where the feed says (state.map marker place "position")
//   cleared a call, on its way home  faded, part way from the call to its firehouse (an estimate: place
//                                    "returning"), with a thin line home
//   no home station, not on a call  not drawn: listed in "Station not set" (click one to set it)
//
// This file only lays the server's markers out: chips fanned around their anchor in screen pixels
// (so they stay neat at every zoom), the dashed lines, the pins, the station picker. A live AVL
// position needs nothing here: it arrives as a marker with place "position".
//
// RigLayer and RigMap use nothing from cad.js (the monitor loads this file too); the CAD glue at the
// bottom (CadMap.detail, CadMap.overview) uses cad.js's helpers and state.
"use strict";

window.CadMap = (() => {
  const STATION_R = 34, INC_R = 30, RING_GAP = 24, CHIP_W = 48;   // px: the fan-out around an anchor
  const FAR_ZOOM = 13.6;               // below this: smaller chips, tighter rings
  const FOCUS_MAX_ZOOM = 16.3;
  const LINE = "#ffb020", HOME_LINE = "#c3cbff";      // to a call | a returning rig's way home (thin)
  const LOOK_NAMES = { available: "available", oos: "out of service", dispatched: "dispatched",
                       responding: "responding", onscene: "on location" };

  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const statusOf = (lists, code) => ((lists && lists.unit_statuses) || []).find((s) => s.code === code) || { code, name: code };
  const typeOf = (lists, code) => ((lists && lists.event_types) || []).find((t) => t.code === code) || null;
  function talkState(name) {
    const t = window.Radio && Radio.talkingFor(name);
    return t ? (t.ended ? "talked" : "talking") : "";
  }

  // ---- RigLayer: pins, rig chips, lines and the station box on a MapLibre map -------------------

  class RigLayer {
    // opts: {host (the map's box, for the overlays), pins, unset, editable, operator(), onPick(id)}
    constructor(map, opts) {
      this.map = map;
      this.opts = opts;
      this.pins = new Map();
      this.chips = new Map();
      this.data = null;
      this.focus = null;
      this.lineSig = "";
      this.far = false;
      this.unsetOpen = false;
      if (opts.unset) {
        this.unsetBox = h("div", "cm-unset");
        this.unsetBox.hidden = true;
        opts.host.append(this.unsetBox);
      }
      this.pop = h("div", "cm-pop");
      this.pop.hidden = true;
      this.pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); this.closePicker(); } });
      opts.host.append(this.pop);
      const init = () => {
        if (!map.getSource("cm-lines")) {
          map.addSource("cm-lines", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
          map.addLayer({
            id: "cm-lines", type: "line", source: "cm-lines",
            layout: { "line-cap": "round", "line-join": "round" },
            paint: { "line-color": ["get", "color"], "line-width": ["case", ["get", "home"], 1.6, 2.5],
                     "line-opacity": ["case", ["get", "home"], 0.7, 0.9], "line-dasharray": [1.6, 1.6] },
          }, map.getLayer("firehouse") ? "firehouse" : undefined);
        }
        this.ready = true;
        this.draw();
      };
      // A map created here waits for its style; one already up (the monitor's) may be busy loading
      // tiles (isStyleLoaded() false, "load" long gone): try now, else when it's idle.
      const tryInit = () => {
        if (this.ready) return;
        try { init(); } catch (e) { map.once("idle", tryInit); }
      };
      if (map.isStyleLoaded() || map.loaded()) tryInit();
      else { map.once("load", tryInit); map.once("idle", tryInit); }
      map.on("zoom", () => {
        const far = map.getZoom() < FAR_ZOOM;
        if (far !== this.far) { this.far = far; opts.host.classList.toggle("cm-far", far); this.draw(); }
      });
    }

    update(data, focus) {
      this.data = data;
      this.focus = focus || null;
      this.draw();
    }

    draw() {
      if (!this.ready || !this.data || !this.data.map) return;
      const M = this.data.map, lists = this.data.lists;
      if (this.opts.pins) this.drawPins(M.incidents || [], lists);
      this.drawChips(M.markers || [], lists);
      this.drawLines(M.markers || [], lists);
      if (this.unsetBox) this.drawUnset(M.unset || []);
    }

    drawPins(incidents, lists) {
      const want = new Map(incidents.map((p) => [p.id, p]));
      const f = this.focus;
      if (f && f.lat != null && f.lon != null) want.set(f.id, f);
      for (const [id, p] of want) {
        const isFocus = !!f && f.id === id;
        const t = typeOf(lists, p.type);
        const color = t ? t.color : "#ff4d3d";
        const label = [p.number || (p.status === "queued" ? "Page" : ""), t ? t.name : ""].filter(Boolean).join(" · ");
        const sig = JSON.stringify([p.lat, p.lon, color, label, isFocus, !!f, p.status, p.address]);
        let rec = this.pins.get(id);
        if (rec && rec.sig === sig) continue;
        if (rec) rec.marker.remove();
        let e;
        if (isFocus) {                                  // the open call: the pulsing pin (its rigs fan out around it)
          e = h("div", "inc-marker cm-focus");
          e.title = label;
          e.append(h("i", "ring"), h("i", "ring r2"), h("i", "dot"));
        } else {
          e = h("button", "cm-pin" + (f ? " dim" : "") + (p.status === "queued" ? " queued" : ""));
          e.type = "button";
          e.append(h("i", "dot"), h("span", "lbl", label || "Incident"));
          e.title = `${label}${p.address ? " · " + p.address : ""}: open it`;
          e.addEventListener("click", (ev) => { ev.stopPropagation(); if (this.opts.onPick) this.opts.onPick(id); });
        }
        e.style.setProperty("--c", color);
        const marker = new maplibregl.Marker({ element: e, anchor: "center" }).setLngLat([p.lon, p.lat]).addTo(this.map);
        this.pins.set(id, { marker, sig });
      }
      for (const [id, rec] of this.pins) if (!want.has(id)) { rec.marker.remove(); this.pins.delete(id); }
    }

    // Each anchor's chips on rings around it: on location (ring 0) close in, not-there-yet (ring 1) outside.
    // At a firehouse a full ring from the bottom (its name is drawn to the right); at a call an arc over
    // the top and sides, leaving the bottom for the call's label.
    offsets(markers) {
      const k = this.far ? 0.72 : 1, w = CHIP_W * k;
      const groups = new Map();
      for (const m of markers) {
        if (!groups.has(m.group)) groups.set(m.group, [[], []]);
        groups.get(m.group)[m.ring ? 1 : 0].push(m);
      }
      const out = new Map();
      for (const [, rings] of groups) {
        let r = 0;
        rings.forEach((list, ring) => {
          if (!list.length) return;
          const m0 = list[0];
          if (m0.place === "position" || m0.place === "returning") { list.forEach((m) => out.set(m.unit, [0, 0])); return; }
          const n = list.length, deg = Math.PI / 180;
          if (m0.place === "station") {
            r = Math.max(ring ? r + RING_GAP * k : STATION_R * k, (n * w) / (2 * Math.PI));
            list.forEach((m, i) => {
              const a = Math.PI / 2 + (2 * Math.PI * i) / n + (ring ? Math.PI / n : 0);
              out.set(m.unit, [Math.round(r * Math.cos(a)), Math.round(r * Math.sin(a) * 0.82)]);
            });
            return;
          }
          const step = n > 1 ? Math.min(270 / (n - 1), 62) * deg : 0;       // centred on the top, 270° at most
          const need = step ? w / (2 * Math.sin(step / 2)) : 0;                // neighbours a chip apart
          r = Math.max(ring ? r + RING_GAP * k : INC_R * k, need);
          list.forEach((m, i) => {
            const a = -Math.PI / 2 + (i - (n - 1) / 2) * step;
            out.set(m.unit, [Math.round(r * Math.cos(a)), Math.round(r * Math.sin(a) * 0.82)]);
          });
        });
      }
      return out;
    }

    drawChips(markers, lists) {
      const offs = this.offsets(markers);
      const seen = new Set();
      for (const m of markers) {
        seen.add(m.unit);
        const st = statusOf(lists, m.status);
        const talk = talkState(m.unit);
        const off = offs.get(m.unit) || [0, 0];
        const onFocus = !!this.focus && m.incident_id === this.focus.id;
        const sig = JSON.stringify([m.lon, m.lat, off, m.look, m.ring, m.place, st.bg, talk, m.tag, m.station, m.incident_number,
          m.returning_from, onFocus, !!this.focus, this.far]);
        let rec = this.chips.get(m.unit);
        if (!rec) {
          const e = h("button", "cm-rig");
          e.type = "button";
          e.addEventListener("click", (ev) => { ev.stopPropagation(); this.chipClick(this.chips.get(m.unit).m); });
          const marker = new maplibregl.Marker({ element: e, anchor: "center" }).setLngLat([m.lon, m.lat]).addTo(this.map);
          rec = { marker, el: e, sig: "" };
          this.chips.set(m.unit, rec);
        }
        rec.m = m;
        if (rec.sig === sig) continue;
        rec.sig = sig;
        const e = rec.el;
        const keep = [...e.classList].filter((c) => c.startsWith("maplibregl-")).join(" ");   // the marker's own
        e.className = `${keep} cm-rig look-${m.look} at-${m.place}` + (m.ring ? " outer" : "") + (talk ? ` ${talk}` : "")
          + (this.focus && m.incident_id && !onFocus ? " other" : "") + (onFocus ? " mine" : "");
        e.style.setProperty("--c", st.bg || "#9aa0b4");
        e.replaceChildren(h("span", "n", m.unit));
        if (m.tag) e.append(h("span", "tag", m.tag));
        e.title = [m.unit, st.name, m.place === "position" ? "live position" : "",
                   m.place === "returning" ? `returning from ${m.returning_from} (estimated position)` : "",
                   m.station ? `home: ${m.station}` : (m.fire ? "home station not set" : ""),
                   m.incident_number ? `on ${m.incident_number}` : "",
                   talk === "talking" ? "talking now" : talk ? "just transmitted" : ""].filter(Boolean).join(" · ");
        e.setAttribute("aria-label", e.title);
        rec.marker.setLngLat([m.lon, m.lat]).setOffset(off);
      }
      for (const [name, rec] of this.chips) if (!seen.has(name)) { rec.marker.remove(); this.chips.delete(name); }
    }

    drawLines(markers, lists) {
      const feats = new Map();
      for (const m of markers) {
        if (!m.line_to) continue;
        const k = JSON.stringify([m.lon, m.lat, m.line_to]);
        const color = m.home_line ? HOME_LINE : statusOf(lists, m.status).bg || LINE;
        if (!feats.has(k)) feats.set(k, { type: "Feature", properties: { color, home: !!m.home_line },
          geometry: { type: "LineString", coordinates: [[m.lon, m.lat], m.line_to] } });
      }
      const fc = { type: "FeatureCollection", features: [...feats.values()] };
      const sig = JSON.stringify(fc);
      if (sig === this.lineSig) return;
      this.lineSig = sig;
      const src = this.map.getSource("cm-lines");
      if (src) src.setData(fc);
    }

    drawUnset(unset) {
      const box = this.unsetBox;
      const sig = JSON.stringify([unset, this.unsetOpen, !!this.editable()]);
      if (box._sig === sig) return;
      box._sig = sig;
      box.hidden = !unset.length;
      if (!unset.length) return;
      const names = unset.map((u) => u.unit);
      const head = h("button", "cm-unset-head");
      head.type = "button";
      head.setAttribute("aria-expanded", String(this.unsetOpen));
      const shown = names.slice(0, 4).join(", ") + (names.length > 4 ? ` +${names.length - 4}` : "");
      head.append(h("b", "", "Station not set: "), h("span", "", this.unsetOpen ? `${names.length} rigs` : shown),
                  h("span", "car", this.unsetOpen ? "▾" : "▸"));
      head.title = "Rigs with no home firehouse are not drawn until they are on a call. Click to list them and set one.";
      head.addEventListener("click", (e) => { e.stopPropagation(); this.unsetOpen = !this.unsetOpen; this.draw(); });
      box.replaceChildren(head);
      if (!this.unsetOpen) return;
      const list = h("div", "cm-unset-list");
      for (const u of unset) {
        const b = h("button", "cm-rig look-unset", u.unit);
        b.type = "button";
        b.title = (u.station ? `"${u.station}" is not a firehouse on the map. ` : "") + (u.on ? `On ${u.on}. ` : "")
          + (this.editable() ? "Click to set its home station." : "");
        b.disabled = !this.editable();
        b.addEventListener("click", (e) => { e.stopPropagation(); this.openPicker(u.id, u.unit, ""); });
        list.append(b);
      }
      if (this.editable()) list.append(h("div", "cm-hint", "Click a rig to set its home firehouse."));
      box.append(list);
    }

    editable() {             // opts.editable: true, or a function (cad.js canDo: only an admin sets a home station)
      const e = this.opts.editable;
      return typeof e === "function" ? !!e() : !!e;
    }

    chipClick(m) {
      if (this.editable() && m.fire && m.id != null) return this.openPicker(m.id, m.unit, m.station, m);
      if (m.incident_id && this.opts.onPick) this.opts.onPick(m.incident_id);
    }

    openPicker(id, name, current, m) {
      const stations = (this.data && this.data.map && this.data.map.stations) || [];
      const pop = this.pop;
      const sel = h("select");
      const none = h("option", "", "— not set —");
      none.value = "";
      sel.append(none, ...stations.map((s) => { const o = h("option", "", s.name); o.value = s.name; return o; }));
      sel.value = current || "";
      const save = h("button", "primary", "Save");
      const cancel = h("button", "", "Cancel");
      const err = h("div", "error");
      save.type = cancel.type = "button";
      const head = h("div", "cm-pop-head");
      head.append(h("b", "", `${name} · home station`));
      pop.replaceChildren(head);
      if (m) {
        const st = statusOf(this.data.lists, m.status);
        const line = h("div", "dim small", [st.name, m.incident_number ? `on ${m.incident_number}` : ""].filter(Boolean).join(" · "));
        pop.append(line);
        if (m.incident_id && this.opts.onPick && (!this.focus || this.focus.id !== m.incident_id)) {
          const go = h("button", "mini", `Open ${m.incident_number || "its call"}`);
          go.type = "button";
          go.addEventListener("click", () => { this.closePicker(); this.opts.onPick(m.incident_id); });
          line.append(" ", go);
        }
      }
      const row = h("div", "row");
      row.append(sel, save, cancel);
      pop.append(row, err, h("div", "dim small", "Where the rig is housed (the department's list, when it has one). "
        + "Stored as an override of the data file and logged with your operator name."));
      pop.hidden = false;
      sel.focus();
      cancel.addEventListener("click", () => this.closePicker());
      save.addEventListener("click", async () => {
        save.disabled = true;
        err.textContent = "";
        try {
          const r = await fetch(`/api/cad/unit/${id}/station`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ station: sel.value, operator: this.opts.operator ? this.opts.operator() : "" }),
          });
          const d = await r.json().catch(() => null);
          if (!r.ok) throw new Error((d && d.error) || `${r.status} ${r.statusText}`);
          this.closePicker();
        } catch (e) {
          err.textContent = e.message;
          save.disabled = false;
        }
      });
    }

    closePicker() {
      this.pop.hidden = true;
      this.pop.replaceChildren();
    }

    // Where the map should look: the focused call and the stations its rigs come from (live positions
    // too); no focus: every open call and the firehouses.
    focusPoints() {
      const M = (this.data && this.data.map) || {};
      const f = this.focus, pts = [];
      if (f) {
        if (f.lat != null && f.lon != null) pts.push([f.lon, f.lat]);
        for (const m of M.markers || []) if (m.incident_id === f.id && m.place !== "incident") pts.push([m.lon, m.lat]);
        if (!pts.length) for (const s of M.stations || []) pts.push(s.ll);
      } else {
        for (const p of M.incidents || []) pts.push([p.lon, p.lat]);
        for (const s of M.stations || []) pts.push(s.ll);
      }
      return pts;
    }
  }

  // ---- RigMap: a map of its own with the layer, Recenter / Expand / Close and a note ---------------

  class RigMap {
    // opts: {scene, pins, unset, editable, operator, onPick, onExpand, onClose}
    constructor(host, opts) {
      this.host = host;
      this.opts = opts;
      host.classList.add("cm-host");
      this.canvas = h("div", "cm-canvas");
      this.note = h("div", "cm-note");
      this.note.hidden = true;
      const ctrls = h("div", "cm-ctrls");
      const btn = (text, title, fn) => {
        const b = h("button", "cm-btn", text);
        b.type = "button";
        b.title = title;
        b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
        ctrls.append(b);
        return b;
      };
      btn("⌖ Recenter", "Back to the call (and where its rigs come from)", () => { this.userMoved = false; this.fit(true); });
      if (opts.onExpand) btn("⤢ Expand", "A large map (Esc closes it)", opts.onExpand);
      if (opts.onClose) btn("✕ Close", "Close the large map (Esc)", opts.onClose);
      host.append(this.canvas, ctrls, this.note);
      const sc = opts.scene;
      this.map = new maplibregl.Map({
        container: this.canvas, style: buildMapStyle(sc), center: sc.home.center, zoom: sc.home.zoom,
        attributionControl: { compact: true }, maxPitch: 0, dragRotate: false, fadeDuration: 0,
      });
      this.map.touchZoomRotate.disableRotation();
      this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      this.map.on("load", () => {
        this.map.addImage("firehouse", firehouseIcon(), { pixelRatio: 2 });
        this.map.getSource("firehouses").setData({
          type: "FeatureCollection",
          features: (sc.firehouses || []).map((f) => ({ type: "Feature", properties: { name: f.name, address: f.address },
            geometry: { type: "Point", coordinates: f.ll } })),
        });
      });
      this.map.on("movestart", (e) => { if (e.originalEvent) this.userMoved = true; });
      this.layer = new RigLayer(this.map, { ...opts, host });
      this.focusId = undefined;
      this.fitKey = null;
      this.userMoved = false;
      this.size = "";
    }

    update(data, focus, note) {
      const size = `${this.canvas.clientWidth}x${this.canvas.clientHeight}`;
      if (size !== this.size) {                       // shown, or the box changed size
        this.size = size;
        this.map.resize();
        if (this.canvas.clientWidth) this.fitKey = null;
      }
      this.layer.update(data, focus);
      this.note.textContent = note || "";
      this.note.hidden = !note;
      const fid = focus ? focus.id : null;
      if (fid !== this.focusId) {                     // another call: look at it whatever was done before
        this.focusId = fid;
        this.userMoved = false;
        this.fitKey = null;
        this.layer.closePicker();
      }
      const key = JSON.stringify(this.layer.focusPoints());
      if (key !== this.fitKey && this.canvas.clientWidth) {
        const first = this.fitKey === null;
        this.fitKey = key;
        if (!this.userMoved) this.fit(!first);
      }
    }

    fit(animate) {
      const pts = this.layer.focusPoints();
      const sc = this.opts.scene;
      const w = this.canvas.clientWidth, ht = this.canvas.clientHeight;
      if (!w || !ht) return;
      const dur = animate ? 600 : 0;
      if (!pts.length) return this.map.jumpTo({ center: sc.home.center, zoom: sc.home.zoom });
      const b = new maplibregl.LngLatBounds(pts[0], pts[0]);
      pts.forEach((p) => b.extend(p));
      const pad = Math.max(10, Math.min(90, Math.floor(Math.min(w, ht) / 3.5)));   // room for the chips around the edge
      if (pts.length === 1 || (b.getEast() - b.getWest() < 1e-4 && b.getNorth() - b.getSouth() < 1e-4)) {
        return this.map.easeTo({ center: b.getCenter(), zoom: this.focusId != null ? 16 : sc.home.zoom, duration: dur });
      }
      const top = Math.min(pad + 34, Math.floor(ht / 2.5));        // below the Recenter / Expand buttons
      this.map.fitBounds(b, { padding: { top, bottom: pad, left: pad, right: pad }, maxZoom: FOCUS_MAX_ZOOM, duration: dur });
    }
  }

  // ---- the CAD console's maps (uses cad.js: cad, $, el, show, setText, selectIncident, ...) ----------

  const views = { detail: null, big: null, overview: null, bigOpen: false };
  const mapData = () => ({ map: cad.state && cad.state.map, lists: cad.state && cad.state.lists });
  const mapsOk = () => !!(window.maplibregl && cad.scene && cad.scene.home);
  const noPoint = (inc) => (inc && (inc.lat == null || inc.lon == null) ? "No map point for this incident" : "");

  function detail(inc) {
    const box = $("#d-map"), nomap = $("#d-nomap");
    if (!mapsOk()) {
      show(box, false);
      show(nomap, true);
      setText(nomap, "Map unavailable");
      return;
    }
    show(nomap, false);
    show(box, true);
    if (!views.detail) {
      views.detail = new RigMap(box, { scene: cad.scene, pins: true, unset: true, editable: () => canDo("admin"), operator,
                                       onPick: (id) => selectIncident(id), onExpand: openBig });
    }
    views.detail.update(mapData(), inc, noPoint(inc));
    if (views.bigOpen) views.big.update(mapData(), inc, noPoint(inc));
  }

  function openBig() {
    let ov = $("#cm-overlay");
    if (!ov) {
      ov = el("div", "overlay cm-overlay");
      ov.id = "cm-overlay";
      const panel = el("div", "cm-big");
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-label", "Map");
      ov.append(panel);
      ov.addEventListener("click", (e) => { if (e.target === ov) closeBig(); });
      document.body.append(ov);
      ov.hidden = false;
      views.big = new RigMap(panel, { scene: cad.scene, pins: true, unset: true, editable: () => canDo("admin"), operator,
                                      onPick: (id) => { closeBig(); selectIncident(id); }, onClose: closeBig });
    }
    ov.hidden = false;
    views.bigOpen = true;
    const inc = cad.mode === "view" ? selected() : null;
    views.big.size = "";
    views.big.focusId = undefined;                     // look at the call again
    views.big.update(mapData(), inc, noPoint(inc));
  }
  function closeBig() {
    const ov = $("#cm-overlay");
    if (ov) ov.hidden = true;
    views.bigOpen = false;
  }

  // ---- Overview: every open call at once ---------------------------------------------------------

  const ACTIVE_FIRST = { active: 0, pending: 1, queued: 2 };

  function liveFor(o) {               // who is talking now on this call: one of its rigs, or its channel
    const names = new Set(o.units.map((u) => u.name));
    return ((cad.state && cad.state.talking) || []).filter((e) => !e.ended && (
      (e.rig && e.rig.split(/,\s*/).some((r) => names.has(r))) || (!e.rig && o.channels.includes(e.channel))));
  }

  function cardFill(li, o) {
    const live = liveFor(o);
    const color = typeColor(o.type) || "#9aa0b4";
    li.className = `ov-card ${o.status}` + (live.length ? " live" : "");
    li.style.setProperty("--c", color);
    const head = el("div", "ov-h");
    const es = eventStatus(o);
    head.append(el("b", "ov-n", incTitle(o)), stBadge(el("span"), es.name, es.bg));
    if (o.joint_call) head.append(el("span", "chip joint", `${o.joint_call} call`));
    head.append(el("span", "spacer"), since(el("span", "ov-el", ""), o.created_at));
    const type = el("div", "ov-type", typeLabel(o.type, o.subtype) || "Type not set");
    const addr = el("div", "ov-addr" + (o.address ? "" : " dim"), o.address || "no location");
    li.replaceChildren(head, type, addr);
    const chips = el("div", "chips ov-units");
    if (o.units.length) {
      for (const u of o.units) {
        const c = statusChip(u.status, `${u.name} · ${unitStatus(u.status).name}`);
        const t = Radio.talkingFor(u.name);
        if (t && !t.ended) c.classList.add("talking");
        chips.append(c);
      }
    } else chips.append(el("span", "dim small", o.status === "queued" ? "page waiting to be accepted" : "no units yet"));
    li.append(chips);
    if (live.length) {
      const l = el("div", "ov-live");
      l.append(el("span", "ico", "📻"), el("b", "", "Talking now: "),
               el("span", "", live.map((e) => `${Radio.who(e) || "radio"} · ${e.channel}`).join(", ")));
      li.append(l);
    }
    if (o.radio) {
      const r = el("div", "ov-radio");
      const who = o.radio.rig || o.radio.talker || (o.radio.dispatcher ? "60 Control" : "radio");
      r.append(el("span", "t", fmtTime(o.radio.start)), el("b", "", who), el("span", "dim", ` · ${o.radio.channel}`));
      if (o.radio.text) r.append(el("span", "q", ` “${o.radio.text}”`));
      else r.append(el("span", "dim", " (no transcript yet)"));
      li.append(r);
    }
    if (o.note) {
      const n = el("div", "ov-note");
      n.append(el("span", "t", fmtTime(o.note.at)), el("span", "k", o.note.kind === "note" ? "note" : o.note.kind),
               el("span", "", o.note.text));
      li.append(n);
    }
  }

  function overview(on) {
    const box = $("#overview");
    show(box, on);
    if (on) show($("#d-empty"), false);             // the overview is the landing view
    $("#overview-open").classList.toggle("on", on);
    if (!on || !cad.state) return;
    const ov = cad.state.overview || [];
    const emerg = ov.filter((o) => o.emergency && o.status !== "queued")
      .sort((a, b) => (ACTIVE_FIRST[a.status] - ACTIVE_FIRST[b.status]) || (a.created_at - b.created_at));
    const pages = ov.filter((o) => o.status === "queued");
    const ne = ov.filter((o) => !o.emergency && o.status !== "queued");
    const n = (k) => emerg.filter((o) => o.status === k).length;
    setText($("#ov-counts"), `${n("active")} active · ${n("pending")} pending · ${ne.length} non-emergency`
      + (pages.length ? ` · ${pages.length} page${pages.length > 1 ? "s" : ""} to accept` : ""));
    // talking now, with the call each one belongs to
    const talk = ((cad.state.talking || []).filter((e) => Radio.shown(e))).slice(0, 8);
    const tbox = $("#ov-talk");
    const tsig = JSON.stringify(talk.map((e) => [e.key, e.rig, e.ended, e.channel, e.alias]).concat(ov.map((o) => [o.id, o.units.length])));
    if (tbox._sig !== tsig) {
      tbox._sig = tsig;
      tbox.replaceChildren(el("span", "dim small", talk.length ? "Talking now" : "Nobody talking"));
      for (const e of talk) {
        const c = el("span", "talk-chip" + (e.ended ? " ended" : "") + (e.rig ? " rig" : "") + (e.dispatcher ? " disp" : ""));
        c.append(el("span", "ico", "📻"), el("span", "", Radio.label(e)));
        const on = ov.filter((o) => liveFor(o).includes(e) || (e.rig && o.units.some((u) => e.rig.split(/,\s*/).includes(u.name))));
        if (on.length) c.append(el("span", "on", `→ ${on.map((o) => incTitle(o)).join(", ")}`));
        tbox.append(c);
      }
    }
    // pages from 60 Control
    const pbox = $("#ov-pages");
    show(pbox, !!pages.length);
    syncList(pbox, pages, {
      key: (o) => String(o.id), sig: (o) => JSON.stringify([o.type, o.subtype, o.address]),
      make: () => { const b = el("button", "ov-page"); b.type = "button"; b.addEventListener("click", () => selectIncident(Number(b.dataset.key))); return b; },
      fill: (b, o) => b.replaceChildren(el("b", "", "From 60 Control: "), el("span", "", `${typeLabel(o.type, o.subtype) || "type not set"} · ${o.address || "no location"}`),
                                        el("span", "dim", " · accept or dismiss →")),
      empty: "",
    });
    syncList($("#ov-cards"), emerg, {
      key: (o) => String(o.id),
      sig: (o) => JSON.stringify([o, liveFor(o).map((e) => [e.key, e.rig]), o.units.map((u) => talkState(u.name)), cad.listSig.length]),
      make: () => {
        const li = el("li", "ov-card");
        li.tabIndex = 0;
        li.setAttribute("role", "button");
        li.addEventListener("click", () => selectIncident(Number(li.dataset.key)));
        li.addEventListener("keydown", (e) => { if (e.key === "Enter") selectIncident(Number(li.dataset.key)); });
        return li;
      },
      fill: cardFill,
      empty: "No active or pending calls",
    });
    syncList($("#ov-nonemerg"), ne, {
      key: (o) => String(o.id), sig: (o) => JSON.stringify([o.number, o.type, o.subtype, o.address, o.units.map((u) => u.name)]),
      make: () => {
        const li = el("li", "ov-ne");
        li.tabIndex = 0;
        li.addEventListener("click", () => selectIncident(Number(li.dataset.key)));
        li.addEventListener("keydown", (e) => { if (e.key === "Enter") selectIncident(Number(li.dataset.key)); });
        return li;
      },
      fill: (li, o) => {
        const t = el("span", "qt", typeLabel(o.type, o.subtype));
        t.style.setProperty("--c", typeColor(o.type) || "#8fb8d8");
        li.replaceChildren(el("b", "", incTitle(o)), t, el("span", "ov-addr-s", o.address || "no location"),
                           el("span", "spacer"), since(el("span", "dim small"), o.created_at));
        if (o.units.length) li.append(el("span", "dim small", o.units.map((u) => u.name).join(", ")));
      },
      empty: "None open",
    });
    if (mapsOk()) {
      if (!views.overview) {
        views.overview = new RigMap($("#ov-map"), { scene: cad.scene, pins: true, unset: true, editable: () => canDo("admin"), operator,
                                                    onPick: (id) => selectIncident(id) });
      }
      views.overview.update(mapData(), null, "");
    } else setText($("#ov-map"), "Map unavailable");
  }

  function goOverview() {
    cad.mode = "none";
    cad.sel = null;
    cad.rec = null;
    render();
    if (window.innerWidth <= 1100) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---- the monitor (/): the same rigs on its map, from the CAD state (polled) --------------------

  function monitor(map, host) {
    let layer = null, stop = false;
    async function poll() {
      if (stop) return;
      try {
        const r = await fetch("/api/cad/state");
        if (r.status === 404) { stop = true; return; }            // no CAD on this station
        const s = await r.json();
        if (!layer) layer = new RigLayer(map, { host, pins: true, unset: false, editable: false,
                                                onPick: () => { location.href = "/cad"; } });
        layer.update({ map: s.map, lists: s.lists }, null);
      } catch (e) { /* the next poll tries again */ }
      setTimeout(poll, 4000);
    }
    poll();
  }

  if (document.getElementById("overview") && typeof cad !== "undefined") {    // the CAD console (loaded after cad.js)
    cad.hooks.push(() => overview(cad.mode === "none"));
    $("#overview-open").addEventListener("click", () => {
      if (location.hash.startsWith("#settings")) history.pushState(null, "", location.pathname);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      goOverview();
    });
    $("#d-back").addEventListener("click", goOverview);
    window.addEventListener("keydown", (e) => { if (e.key === "Escape" && views.bigOpen) closeBig(); });
  }

  return { RigLayer, RigMap, detail, overview, goOverview, monitor, openBig, closeBig };
})();
