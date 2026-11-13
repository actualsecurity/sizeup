// Street view on the CAD maps: 360-degree street imagery in the app (Google Maps JavaScript API, StreetViewPanorama).
//
//   Ways in      Right-click (a long press on a touch screen) anywhere on a map: "Street view here". The person
//                button in a map's controls: on a call's map, at the call, facing the building; on a map with no
//                call, the next click on the map picks the spot (Esc cancels).
//   The viewer   A dialog: the panorama beside a small map on a wide screen, the panorama full-screen with the map
//                inset on a phone or an iPad. Click or the arrows to move along the street, drag to look. The map
//                shows where the viewer stands and which way it faces, and follows each move; a click on that map
//                jumps there. The head says when the imagery was taken ("Imagery Jun 2024").
//   Facing       From the nearest panorama (within RADIUS_M) to the building: the centroid of the footprint drawn
//                on the map under the call's point (the county's buildings, the base map's), else the point itself.
//   The key      GET /api/cad/streetview, once, when the first map is attached ({provider: "google", key} or
//                {provider: ""}): a browser key restricted by HTTP referrer to the hub's address and to the Maps
//                JavaScript API, from the hub's config / SSM (sizeup/config.py [streetview]). No key: nothing shows,
//                except to an admin, whose button and menu item say "Street view: key not set".
//   Privacy      Nothing goes to Google until someone opens the viewer: the script loads then, once per page, with
//                an origin-only referrer, and Google is sent coordinates and panorama ids only (never a call's
//                number, address or notes). One StreetViewPanorama per page, reused (Google bills each one made).
// Pure helpers (bearing, centroid, inRing, dateLabel, mode, scriptUrl, menuItems) are exported for tests/test_streetview.py.
"use strict";

window.StreetView = (() => {
  const RADIUS_M = 50;                    // the nearest panorama to a point, no farther
  const LONG_MS = 550, MOVE_PX = 10;      // a long press: held this long, moved no more than this
  const CB = "__sizeupStreetView";        // the SDK's ready callback
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const BUILDING_LAYER = /^(building|gis-buildings)/;   // map-style.js's base buildings, gislayers.js's county footprints

  // ---- pure helpers ---------------------------------------------------------------------------------------------------
  const rad = (d) => (d * Math.PI) / 180;
  // the initial compass bearing from a to b ({lat, lon}), 0 to 360, to 0.1 degree
  function bearing(a, b) {
    const f1 = rad(a.lat), f2 = rad(b.lat), dl = rad(b.lon - a.lon);
    const y = Math.sin(dl) * Math.cos(f2);
    const x = Math.cos(f1) * Math.sin(f2) - Math.sin(f1) * Math.cos(f2) * Math.cos(dl);
    const deg = (Math.atan2(y, x) * 180) / Math.PI;
    const r = Math.round(((deg % 360) + 360) % 360 * 10) / 10;
    return r >= 360 ? r - 360 : r;                           // [0, 360): 359.96 rounds to 0, not 360
  }
  // a polygon ring's centroid [lon, lat] (by area; a degenerate ring: the mean of its points)
  function centroid(ring) {
    const pts = ring.length > 1 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1]
      ? ring.slice(0, -1) : ring;
    if (!pts.length) return null;
    const [ox, oy] = pts[0];                 // relative to the first point: no cancellation at -73 x 41
    let a = 0, cx = 0, cy = 0;
    for (let i = 0; i < pts.length; i++) {
      const x0 = pts[i][0] - ox, y0 = pts[i][1] - oy, x1 = pts[(i + 1) % pts.length][0] - ox, y1 = pts[(i + 1) % pts.length][1] - oy;
      const c = x0 * y1 - x1 * y0;
      a += c; cx += (x0 + x1) * c; cy += (y0 + y1) * c;
    }
    if (Math.abs(a) < 1e-18) return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
    return [ox + cx / (3 * a), oy + cy / (3 * a)];
  }
  // is [lon, lat] inside the ring (even-odd)
  function inRing(pt, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }
  // the footprint under the point ([GeoJSON geometry, ...]): its centroid {lat, lon}, else the point
  function facePoint(pt, geoms) {
    for (const g of geoms || []) {
      const rings = !g ? [] : g.type === "Polygon" ? [g.coordinates[0]] : g.type === "MultiPolygon" ? g.coordinates.map((p) => p[0]) : [];
      for (const r of rings) {
        if (r && r.length > 2 && inRing([pt.lon, pt.lat], r)) {
          const c = centroid(r);
          if (c) return { lat: c[1], lon: c[0] };
        }
      }
    }
    return { lat: pt.lat, lon: pt.lon };
  }
  // Google's imageDate "2024-06" -> "Imagery Jun 2024"; nothing usable -> ""
  function dateLabel(d) {
    const m = /^(\d{4})-(\d{1,2})/.exec(String(d || ""));
    if (!m || +m[2] < 1 || +m[2] > 12) return "";
    return `Imagery ${MONTHS[+m[2] - 1]} ${m[1]}`;
  }
  // what a map offers: "on" (a key), "notice" (no key, an admin: says so), "off" (no key, anyone else; or not known yet)
  function mode(cfg, admin) {
    if (!cfg) return "off";
    if (cfg.provider === "google" && cfg.key) return "on";
    return admin ? "notice" : "off";
  }
  const scriptUrl = (key, cb) =>
    `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly&loading=async&callback=${encodeURIComponent(cb)}`;
  // the map's right-click / long-press menu
  function menuItems(m) {
    if (m === "on") return [{ label: "Street view here", icon: "streetview" }];
    if (m === "notice") return [{ label: "Street view here", icon: "streetview", disabled: true, hint: "key not set" }];
    return [];
  }

  // ---- small DOM helpers ----------------------------------------------------------------------------------------------
  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  // an admin (or no sign-in at all: local); nobody until cad.js knows who it is
  const isAdmin = () => typeof canDo === "function" && typeof cad !== "undefined" && !!cad.who && canDo("admin");
  const scene = () => (typeof cad !== "undefined" && cad.scene) || null;
  const toast = (t) => { if (window.UI && UI.toast) UI.toast(t, "info"); };

  // ---- the key (once per page) ----------------------------------------------------------------------------------------
  let cfg = null, cfgP = null;
  const maps = new Set();
  function config() {
    if (!cfgP) {
      cfgP = fetch("/api/cad/streetview", { cache: "no-store", credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : { provider: "" }))
        .catch(() => ({ provider: "" }))
        .then((c) => { cfg = c || { provider: "" }; for (const m of maps) m.sync(); return cfg; });
    }
    return cfgP;
  }
  const current = () => mode(cfg, isAdmin());

  // ---- Google's SDK: loaded the first time the viewer opens -----------------------------------------------------------
  let sdkP = null, refused = false;
  function sdk() {
    if (sdkP) return sdkP;
    sdkP = new Promise((ok, no) => {
      window[CB] = () => { try { delete window[CB]; } catch (_) { window[CB] = undefined; } ok(); };
      window.gm_authFailure = () => {          // a key Google refuses (its referrer or its APIs)
        refused = true;
        V.empty(isAdmin() ? "Street view: key refused" : "Street view unavailable");
      };
      const s = document.createElement("script");
      s.src = scriptUrl(cfg.key, CB);
      s.async = true;
      s.referrerPolicy = "strict-origin";        // the origin only: what the key's referrer rule needs, no more
      s.onerror = () => { sdkP = null; s.remove(); no(new Error("Street view unavailable")); };
      document.head.append(s);
    }).then(() => google.maps.importLibrary("streetView"));
    return sdkP;
  }

  // ---- the viewer (one per page) --------------------------------------------------------------------------------------
  const V = {
    ov: null, pano: null, svc: null, lib: null, map: null, mark: null, tgt: null, dates: new Map(), seq: 0, target: null,
    build() {
      if (this.ov) return;
      const ov = h("div", "overlay sv-overlay");
      ov.hidden = true;
      const dlg = h("div", "dialog sv-dialog");
      dlg.setAttribute("role", "dialog");
      dlg.setAttribute("aria-modal", "true");
      dlg.setAttribute("aria-label", "Street view");
      const head = h("div", "sv-head");
      this.date = h("span", "sv-date");
      this.facing = h("span", "sv-facing dim");
      const close = h("button", "btn sm icon quiet");
      close.type = "button";
      close.title = "Close (Esc)";
      close.setAttribute("aria-label", "Close street view");
      close.append(window.Icon ? Icon("x") : "×");
      close.addEventListener("click", () => this.close());
      head.append(this.date, this.facing, h("span", "spacer"), close);
      const body = h("div", "sv-body");
      this.panoEl = h("div", "sv-pano");
      this.msg = h("div", "sv-empty");
      this.msg.hidden = true;
      this.mapEl = h("div", "sv-map");
      body.append(this.panoEl, this.msg, this.mapEl);
      dlg.append(head, body);
      ov.append(dlg);
      document.body.append(ov);
      this.ov = ov;
    },
    open(at, opts = {}) {
      this.build();
      this.lastAt = at;
      const seq = ++this.seq;
      this.target = opts.face || null;
      this.setDate("");
      this.facing.textContent = "";
      this.empty("");
      if (window.UI && UI.dialog) UI.dialog.open(this.ov, { onClose: () => this.closed(), opener: opts.opener });
      else this.ov.hidden = false;
      this.ensureMap(at);
      sdk().then((lib) => {
        if (seq !== this.seq) return null;
        this.lib = lib;
        if (!this.svc) this.svc = new lib.StreetViewService();
        return this.nearest(at).then((d) => {
          if (seq !== this.seq) return;
          if (!d) { this.empty(`No street imagery within ${RADIUS_M} m`); this.place(at, null); return; }
          const from = { lat: d.location.latLng.lat(), lon: d.location.latLng.lng() };
          const heading = bearing(from, this.target || at);
          this.dates.set(d.location.pano, d.imageDate || "");
          this.show(d.location.pano, heading);
        });
      }).catch(() => { if (seq === this.seq && !refused) this.empty("Street view unavailable"); });
    },
    nearest(at) {
      const L = this.lib;
      return this.svc.getPanorama({ location: { lat: at.lat, lng: at.lon }, radius: RADIUS_M,
                                    source: L.StreetViewSource.OUTDOOR, preference: L.StreetViewPreference.NEAREST })
        .then((r) => (r && r.data) || null).catch(() => null);
    },
    show(pano, heading) {
      this.empty("");
      const pov = { heading, pitch: 0 };
      if (!this.pano) {
        this.pano = new this.lib.StreetViewPanorama(this.panoEl, {
          pano, pov, zoom: 0, visible: true, addressControl: false, fullscreenControl: false, enableCloseButton: false,
          motionTracking: false, motionTrackingControl: false, showRoadLabels: true, linksControl: true, panControl: true,
          zoomControl: true, clickToGo: true, imageDateControl: false,
        });
        this.pano.addListener("pano_changed", () => this.panoMoved());
        this.pano.addListener("position_changed", () => this.moved());
        this.pano.addListener("pov_changed", () => this.turned());
      } else {
        this.pano.setPano(pano);
        this.pano.setPov(pov);
        this.pano.setVisible(true);
      }
      this.panoMoved();
    },
    panoMoved() {
      const id = this.pano && this.pano.getPano();
      if (!id) return;
      if (this.dates.has(id)) { this.setDate(this.dates.get(id)); return; }
      this.setDate("");
      this.svc.getPanorama({ pano: id }).then((r) => {
        const d = (r && r.data && r.data.imageDate) || "";
        this.dates.set(id, d);
        if (this.pano.getPano() === id) this.setDate(d);
      }).catch(() => {});
    },
    setDate(d) {
      const t = dateLabel(d);
      this.date.textContent = t;
      this.date.title = t ? `Google Street View, captured ${t.replace("Imagery ", "")}` : "";
    },
    moved() {
      const p = this.pano && this.pano.getPosition();
      if (p) this.place({ lat: p.lat(), lon: p.lng() }, this.pano.getPov().heading);
    },
    turned() {
      const pov = this.pano && this.pano.getPov();
      if (!pov) return;
      if (this.mark) this.mark.setRotation(pov.heading);
      this.facing.textContent = degLabel(pov.heading);
      this.facing.title = "Facing (compass degrees)";
    },
    // the viewer's small map: built on the first open (the dialog must be showing to size it)
    ensureMap(at) {
      if (this.map || !window.maplibregl || typeof buildMapStyle !== "function" || !scene()) {
        if (this.map) { this.map.resize(); this.map.jumpTo({ center: [at.lon, at.lat], zoom: 18 }); }
        this.drawTarget();
        return;
      }
      this.map = new maplibregl.Map({ container: this.mapEl, style: buildMapStyle(scene()), center: [at.lon, at.lat], zoom: 18,
                                      attributionControl: { compact: true }, maxPitch: 0, dragRotate: false, fadeDuration: 0 });
      this.map.touchZoomRotate.disableRotation();
      this.map.once("load", () => {             // the credit folded (it covers the inset map on a phone)
        const a = this.mapEl.querySelector(".maplibregl-ctrl-attrib");
        if (a) a.classList.remove("maplibregl-compact-show");
      });
      this.map.on("click", (e) => this.jump({ lat: e.lngLat.lat, lon: e.lngLat.lng }));
      this.drawTarget();
    },
    drawTarget() {
      if (!this.map) return;
      if (this.tgt) { this.tgt.remove(); this.tgt = null; }
      if (!this.target) return;
      const el = h("div", "sv-target");
      el.title = "The call";
      this.tgt = new maplibregl.Marker({ element: el }).setLngLat([this.target.lon, this.target.lat]).addTo(this.map);
    },
    place(at, heading) {
      if (!this.map) return;
      if (!this.mark) {
        const el = h("div", "sv-mark");
        el.title = "Street view";
        this.mark = new maplibregl.Marker({ element: el, rotationAlignment: "map" }).setLngLat([at.lon, at.lat]).addTo(this.map);
      }
      this.mark.setLngLat([at.lon, at.lat]);
      this.mark.getElement().classList.toggle("sv-nohead", heading == null);
      if (heading != null) this.mark.setRotation(heading);
      this.map.easeTo({ center: [at.lon, at.lat], duration: 300 });
    },
    // a click on the viewer's map: the nearest panorama there, facing the same way
    jump(at) {
      if (!this.svc) return;
      const seq = ++this.seq;
      const heading = this.pano ? this.pano.getPov().heading : 0;
      this.nearest(at).then((d) => {
        if (seq !== this.seq) return;
        if (!d) { toast(`No street imagery within ${RADIUS_M} m`); return; }
        this.dates.set(d.location.pano, d.imageDate || "");
        this.show(d.location.pano, heading);
      });
    },
    empty(text) {
      if (!this.msg) return;
      this.msg.textContent = text;
      this.msg.hidden = !text;
      this.panoEl.classList.toggle("sv-dim", !!text);
      if (text && this.pano) this.pano.setVisible(false);
    },
    close() {
      if (window.UI && UI.dialog) UI.dialog.close(this.ov);     // its onClose: closed()
      else { this.ov.hidden = true; this.closed(); }
    },
    closed() {
      this.seq++;
      if (this.pano) this.pano.setVisible(false);
    },
  };

  // ---- the menu at a point (right-click, long press) ------------------------------------------------------------------
  const MENU = { el: null, at: 0, swallow: 0, down: null };
  const SWALLOW_MS = 400;                 // the click a touch screen makes when the long press lifts: not a map click
  function menuClose() {
    if (MENU.el) { MENU.el.remove(); MENU.el = null; }
  }
  // one menu per gesture: Android sends contextmenu on a long press too, and our own timer would open a second
  const justOpened = () => !!MENU.el && Date.now() - MENU.at < 800;
  function menuOpen(x, y, lngLat, sm) {
    if (justOpened()) return false;
    const items = menuItems(current());
    if (!items.length) return false;
    menuClose();
    const el = h("div", "menu popover sv-menu");
    el.setAttribute("role", "menu");
    el.setAttribute("aria-label", "Map");
    for (const it of items) {
      const b = h("button", "menu-item");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      const mark = h("span", "menu-mark");
      if (window.Icon) mark.append(Icon(it.icon, "sm"));
      b.append(mark, h("span", "menu-text", it.label));
      if (it.hint) { b.append(h("span", "menu-hint", it.hint)); b.title = `Street view: ${it.hint}`; }
      if (it.disabled) b.setAttribute("aria-disabled", "true");
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if (it.disabled) return;
        menuClose();
        sm.openAt({ lat: lngLat.lat, lon: lngLat.lng }, null);
      });
      el.append(b);
    }
    el.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); menuClose(); } });
    document.body.append(el);
    const vw = document.documentElement.clientWidth, vh = window.innerHeight;
    el.style.left = `${Math.max(8, Math.min(x, vw - el.offsetWidth - 8))}px`;
    el.style.top = `${Math.max(8, Math.min(y, vh - el.offsetHeight - 8))}px`;
    MENU.el = el;
    MENU.at = Date.now();
    const first = el.querySelector(".menu-item");
    if (first) first.focus({ preventScroll: true });
    return true;
  }
  document.addEventListener("pointerdown", (e) => {          // anywhere else closes it (not the lift of the long press)
    MENU.down = { x: e.clientX, y: e.clientY };
    if (MENU.el && !MENU.el.contains(e.target) && Date.now() - MENU.at > SWALLOW_MS) menuClose();
  }, true);
  // Before anything else sees a click (the window, capturing): the one a long press's lift makes, and the armed pick's,
  // so the new call's map, a pre-plan placement, a pin or a firehouse's rig list never take it as theirs too.
  window.addEventListener("click", (e) => {
    if (Date.now() < MENU.swallow && !(MENU.el && MENU.el.contains(e.target))) {
      MENU.swallow = 0;
      e.stopPropagation(); e.preventDefault();
      return;
    }
    for (const m of maps) if (m.armed && m.takeClick(e)) return;
  }, true);
  // Esc: one thing at a time. Whatever took it first (a dialog, the pre-plan editor, #464's alignment) keeps it.
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    if (MENU.el) { menuClose(); e.preventDefault(); return; }
    for (const m of maps) if (m.armed) { m.disarm(); e.preventDefault(); return; }
  });
  window.addEventListener("hashchange", () => { for (const m of maps) m.disarm(); });
  // the pre-plan editor (preplan.js) is placing or moving a point: its form is on the page while it is
  const ppEditing = () => !!document.querySelector("form.pp-form");
  // the view on screen and its call (cad.js viewTab, cad.sel): a pick belongs to the view it was armed in
  const where = () => `${typeof viewTab === "function" ? viewTab() : ""}|${typeof cad !== "undefined" ? cad.sel : ""}`;
  // a degree heading as shown: 0 to 359 (359.6 is 0, never "360")
  const degLabel = (deg) => `${Math.round(((Number(deg) % 360) + 360) % 360) % 360}°`;

  // ---- one map: its button, its menu, its long press ------------------------------------------------------------------
  class SVMap {
    constructor(control) {
      this.control = control;
      this.map = control.map;
      this.armed = false;
      // the new call's map (newcall-map.js, manual): its clicks pick the call's location; no pick mode there
      this.manual = !!(control.host && control.host.id === "nc-map");
      const ctrls = control.btn && control.btn.parentNode;
      this.btn = h("button", "cm-btn sm icon sv-btn");
      this.btn.type = "button";
      this.btn.hidden = true;
      this.btn.setAttribute("aria-pressed", "false");
      this.btn.append(window.Icon ? Icon("streetview") : "SV");
      this.btn.addEventListener("click", (e) => { e.stopPropagation(); this.press(); });
      if (ctrls) ctrls.append(this.btn);
      const map = this.map;
      map.on("contextmenu", (e) => {
        if (e.originalEvent) e.originalEvent.preventDefault();
        const oe = e.originalEvent || {};
        const r = map.getCanvas().getBoundingClientRect();
        menuOpen(oe.clientX != null ? oe.clientX : r.left + e.point.x, oe.clientY != null ? oe.clientY : r.top + e.point.y, e.lngLat, this);
      });
      map.on("movestart", (e) => { if (e.originalEvent && Date.now() - MENU.at > SWALLOW_MS) menuClose(); });
      this.longPress(map.getCanvasContainer());
      maps.add(this);
      this.sync();
    }
    // a long press on a touch screen (iOS Safari sends no contextmenu); not on a mark (a rig, a dragged pre-plan point)
    longPress(el) {
      let t = null, x0 = 0, y0 = 0, fired = false;
      const cancel = () => { if (t) { clearTimeout(t); t = null; } };
      el.addEventListener("touchstart", (e) => {
        cancel();
        fired = false;
        if (e.touches.length !== 1) return;
        if (e.target && e.target.closest && e.target.closest(".maplibregl-marker")) return;
        x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
        t = setTimeout(() => {
          t = null;
          const r = this.map.getCanvas().getBoundingClientRect();
          fired = menuOpen(x0, y0, this.map.unproject([x0 - r.left, y0 - r.top]), this) || justOpened();
        }, LONG_MS);
      }, { passive: true });
      el.addEventListener("touchmove", (e) => {
        const p = e.touches[0];
        if (!p || Math.hypot(p.clientX - x0, p.clientY - y0) > MOVE_PX) cancel();
      }, { passive: true });
      el.addEventListener("touchend", () => {
        cancel();
        if (fired || justOpened()) MENU.swallow = Date.now() + SWALLOW_MS;   // the lift's click is not a tap on the map
        fired = false;
      }, { passive: true });
      el.addEventListener("touchcancel", () => { cancel(); fired = false; }, { passive: true });
    }
    sync() {
      const m = current();
      this.btn.hidden = m === "off" || (this.manual && !this.call());
      this.btn.title = m === "notice" ? "Street view: key not set" : "Street view";
      this.btn.setAttribute("aria-label", this.btn.title);
      if (this.armed && where() !== this.armedIn) this.disarm();        // the view or its call changed
    }
    call() {
      const inc = this.control.focused;
      return inc && inc.lat != null && inc.lon != null ? { lat: +inc.lat, lon: +inc.lon } : null;
    }
    press() {
      const m = current();
      this.sync();
      if (m === "notice") { toast("Street view: key not set"); return; }
      if (m !== "on") return;
      if (this.armed) { this.disarm(); return; }
      const at = this.call();
      if (at) { this.openAt(at, at); return; }
      this.arm();
    }
    // no call here: the next click on the map picks the spot
    arm() {
      if (this.manual || ppEditing()) return false;    // the map's clicks are someone else's just now
      for (const m of maps) if (m !== this) m.disarm();
      this.armed = true;
      this.armedIn = where();
      this.btn.setAttribute("aria-pressed", "true");
      try { const c = this.map.getCanvas(); this.cursor = c.style.cursor; c.style.cursor = "crosshair"; } catch (_) { /* */ }
      return true;
    }
    disarm() {
      if (!this.armed) return;
      this.armed = false;
      this.btn.setAttribute("aria-pressed", "false");
      try { this.map.getCanvas().style.cursor = this.cursor || ""; } catch (_) { /* */ }
      this.cursor = "";
    }
    // an armed map's click (the window's capturing listener): on this map's canvas, it is the pick and nobody else's
    takeClick(e) {
      let canvas;
      try { canvas = this.map.getCanvas(); } catch (_) { return false; }
      const t = e.target;
      const onMap = t === canvas || !!(t && t.closest && t.closest(".maplibregl-marker") && this.map.getContainer().contains(t));
      if (!onMap) return false;                        // its controls, the page: as ever
      const d = MENU.down;
      if (d && Math.hypot(e.clientX - d.x, e.clientY - d.y) > 4) return false;   // the end of a pan: not a pick
      e.stopPropagation(); e.preventDefault();
      if (ppEditing() || where() !== this.armedIn) { this.disarm(); return true; }
      const r = canvas.getBoundingClientRect(), ll = this.map.unproject([e.clientX - r.left, e.clientY - r.top]);
      this.disarm();
      this.openAt({ lat: ll.lat, lon: ll.lng }, null);
      return true;
    }
    // at {lat, lon}; face: the call's point (or null: a spot picked on the map, faced from the panorama towards it)
    openAt(at, face) {
      if (current() !== "on") return;
      V.open(at, { face: face ? facePoint(face, this.footprints(face)) : null, opener: this.btn });
    }
    // the footprints drawn on this map under the point (the county's or the base map's buildings)
    footprints(pt) {
      try {
        const map = this.map, ids = (map.getStyle().layers || []).map((l) => l.id).filter((id) => BUILDING_LAYER.test(id));
        if (!ids.length) return [];
        return map.queryRenderedFeatures(map.project([pt.lon, pt.lat]), { layers: ids }).map((f) => f.geometry);
      } catch (_) {
        return [];
      }
    }
  }

  let subscribed = false;
  function attach(control) {
    if (typeof BOARD !== "undefined" && BOARD) return {};       // the wall board: nobody at it
    if (!control || !control.map) return {};
    const m = new SVMap(control);
    config();
    // who is signed in arrives after the first map: look again whenever the maps' data does (they poll it anyway)
    if (!subscribed && MapLayers.subscribe) { subscribed = true; setTimeout(() => MapLayers.subscribe(syncAll), 0); }
    return { panel: () => null, keys: () => [], refresh: () => m.sync() };
  }
  const syncAll = () => { for (const m of maps) m.sync(); };
  if (window.MapLayers && MapLayers.addGroup) MapLayers.addGroup({ attach });


  if (typeof cad !== "undefined" && cad.hooks) cad.hooks.push(syncAll);   // every render: a pick left behind by a view change
  return { RADIUS_M, LONG_MS, SWALLOW_MS, degLabel, bearing, centroid, inRing, facePoint, dateLabel, mode, scriptUrl, menuItems, config,
           lastAt: () => V.lastAt || null,
           open: (at, face) => (current() === "on" ? V.open(at, { face }) : null) };
})();
