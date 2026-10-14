// The camera wall (GitHub #125) and the Overview's layouts (#147, #168): every camera in our area at once, on the
// Overview (/cad) and the wall board (/monitor).
//
// User, 2026-10-14: "on the monitor page and the overview page, can we add a way to look at all of the cameras
// in our jurisdiction live? somehow turn that on and off so we can visually see all of them"; then "we should
// show all the live feeds we can, more is better"; then (#147) "I'd like to be able to show them side by side:
// map/cameras/incidents, or map/cameras on the left above/below and incidents on the right... or overlay the
// cameras on the map too".
//
//   Layouts       a picker in the Overview's head (a segmented control, the same on the wall board): Map · Cameras ·
//                 Side by side (map | cameras | events) · Stacked (map over cameras, or under them: the swap button;
//                 events on the right). Remembered per screen (localStorage "sizeup.overviewLayout" {overview,
//                 monitor: {mode, swap, side, stacked}}; the wall board and the dispatch screen can differ; #125's
//                 "sizeup.cameraWall" on is read once as Cameras). On a phone (640 px and under) Map and Cameras
//                 only: the others show as the map.
//   The overlay   the cameras on the map (camoverlay.js) is a toggle in the map's Layers box, not a layout (#168:
//                 "overlay should only be an option in layers, not here"). A screen whose saved layout was the old
//                 Overlay (#147) opens as Map with that toggle on (MapLayers.setLayer(OVERLAY, true)), once.
//   The divider   between the map and the cameras in Side by side and Stacked: panels.js's separator (#139: drag,
//                 the arrow keys, Home or a double-click resets to half; the map resizes live), its share of the
//                 view remembered per layout, each pane at least SPLIT_MIN px.
//   The cameras   GET /api/cad/cameras?area=ours[&for=lat,lon&label=&address=] (sizeup/roads.py Roads.area): inside
//                 our fire districts plus a margin (Settings > System > Camera wall), by road then mileage; an
//                 active highway event's camera just before / at / just after it pinned first and highlighted (in
//                 every layout: the wall's tiles, the overlay's, a ring on the map without it).
//   A tile        live video where the source streams (the Thruway's HLS, through the hub, muted, hls.js: the
//                 camera viewer's own loader in maplayers.js), else the snapshot at the source's cadence, fetched
//                 only when newer (If-None-Match against the proxy's ETag). Its name, road, direction and mileage,
//                 the picture's age, "Live" only while the video plays, a small note when the source's frame rate
//                 is low. A click opens the camera viewer.
//   Down          cameras the hub calls down (or the source marks out of service) fold into one "N cameras down"
//                 tile, opened in place (on the overlay: the dim "!" mark only).
//   Bandwidth     only tiles on screen play or refresh (IntersectionObserver, the wall's and the overlay's alike); a
//                 tile off screen for STOP_MS stops its stream at the hub too (GET .../stream/close?v=<this page>),
//                 so the hub's prefetch stops; everything stops while the page or the Overview is hidden. At most
//                 the hub's per-screen budget of live tiles at once (streams.per_viewer), across the wall and the
//                 overlay; a camera live in one set is a snapshot in the other (Side by side or Stacked with the
//                 overlay on: one stream, one slot); a stream the hub refuses (its cap: 429) or the source can't
//                 give (503) shows snapshots and tries again later.
//
// CamWall.engine is the tiles for camoverlay.js: list() (the area's cameras, as above), subscribe(fn) (the list or
// the layout changed), tile(c, set, opts) / drop(t) (a tile in a set: "wall" | "overlay"), layout() (the mode now),
// OVERLAY (the overlay's layer key in the Layers box).
"use strict";

window.CamWall = (() => {
  if (!document.getElementById("cw-wall") || !window.MapLayers) return null;
  const KEY = "sizeup.overviewLayout", OLD_KEY = "sizeup.cameraWall";
  const SCREEN = BOARD ? "monitor" : "overview";
  const MODES = ["map", "cameras", "side", "stacked"];
  const OVERLAY = "camoverlay";       // the overlay's toggle in the Layers box (MapLayers, the "overview" set; camoverlay.js)
  const PHONE = matchMedia("(max-width: 640px)");
  const SPLIT_MIN = 200;
  const LIST_MS = 60000, TICK_MS = 1000, STATUS_MS = 20000, RETRY_MS = 60000, START_MS = 25000, STOP_MS = 2500, MAX_FETCHES = 4;
  const TOKEN = Array.from(crypto.getRandomValues(new Uint8Array(9)), (b) => b.toString(36).padStart(2, "0")).join("").slice(0, 16);
  const w = {
    list: null, error: "", tiles: new Map(), downOpen: false, sig: "", key: "", loading: false,
    listAt: 0, timer: null, statusAt: 0, fetching: 0, running: false, subs: new Set(),
  };
  const wall = $("#cw-wall"), view = $("#ov-view"), split = $("#ov-split"), mapbox = view.querySelector(".ov-mapbox");
  const grid = view.closest(".ov-grid"), picker = $("#ov-layout"), swapBtn = $("#ov-swap");

  // ---- the layout, remembered per screen (storage may be missing or refuse: then this page view only) -------------
  function load() {
    let s = {};
    try {
      s = (JSON.parse(localStorage.getItem(KEY) || "{}") || {})[SCREEN] || {};
      if (!s.mode && (JSON.parse(localStorage.getItem(OLD_KEY) || "{}") || {})[SCREEN]) s = { mode: "cameras" };   // #125's switch
    } catch (_) { /* the default */ }
    if (s.mode === "overlay") { s = { ...s, mode: "map" }; wasOverlay = true; }     // #147's Overlay layout: Map, the toggle on (#168)
    return { mode: MODES.includes(s.mode) ? s.mode : "map", swap: !!s.swap,
             side: Number.isFinite(s.side) ? s.side : 0.5, stacked: Number.isFinite(s.stacked) ? s.stacked : 0.5 };
  }
  function save() {
    try {
      const all = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
      all[SCREEN] = { ...L };
      localStorage.setItem(KEY, JSON.stringify(all));
    } catch (_) { /* this page view only */ }
  }
  let wasOverlay = false;
  const L = load();
  if (wasOverlay) { MapLayers.setLayer(OVERLAY, true, "overview"); save(); }    // once: the layout saved as Map from now on
  // what shows now: a phone has Map and Cameras only
  const mode = () => (PHONE.matches && !["map", "cameras"].includes(L.mode) ? "map" : L.mode);
  const split2 = () => mode() === "side" || mode() === "stacked";
  const wallShown = () => ["cameras", "side", "stacked"].includes(mode());

  function choose(m) {
    if (!MODES.includes(m)) return;
    L.mode = m;
    save();
    layout();
  }
  for (const b of picker.querySelectorAll("button[data-layout]")) b.addEventListener("click", () => choose(b.dataset.layout));
  $("#ov-swap").addEventListener("click", () => { L.swap = !L.swap; save(); layout(); });

  // the divider: the map's share of the view (side: its width, stacked: its height), each pane at least SPLIT_MIN
  const along = () => (mode() === "side" ? view.clientWidth : view.clientHeight) - (mode() === "side" ? split.offsetWidth : split.offsetHeight);
  const mapPx = () => Math.round((mode() === "side" ? mapbox.offsetWidth : mapbox.offsetHeight));
  function setShare(px) {
    const total = along();
    if (total <= 0) return;
    const lo = Math.min(SPLIT_MIN, total / 2), hi = Math.max(total - SPLIT_MIN, total / 2);
    L[mode()] = Math.round((Math.max(lo, Math.min(hi, px)) / total) * 1000) / 1000;
    grid.style.setProperty("--ov-split", `${L[mode()] * 100}%`);
  }
  // stacked with the cameras on top: dragging down grows the cameras, so the map shrinks
  const sep = Panels.separator(split, {
    label: "Resize the map and the cameras", controls: "cw-wall",
    orient: () => (mode() === "side" ? "vertical" : "horizontal"), grow: () => (mode() === "stacked" && L.swap ? -1 : 1),
    min: () => Math.min(SPLIT_MIN, along() / 2), max: () => Math.max(along() - SPLIT_MIN, along() / 2),
    value: mapPx, enabled: split2,
    set: (px, done) => { setShare(px); if (done) save(); },
    reset: () => { L[mode()] = 0.5; grid.style.setProperty("--ov-split", "50%"); save(); },
  });
  function layout() {
    const m = mode();
    grid.dataset.layout = m;
    grid.dataset.swap = String(m === "stacked" && L.swap);
    grid.style.setProperty("--ov-split", `${(L[m] || 0.5) * 100}%`);
    for (const b of picker.querySelectorAll("button[data-layout]")) b.setAttribute("aria-pressed", String(b.dataset.layout === m));
    show(swapBtn, m === "stacked");
    swapBtn.setAttribute("aria-pressed", String(L.swap));
    swapBtn.title = L.swap ? "Map above the cameras" : "Cameras above the map";
    show(wall, wallShown());
    if (!wallShown()) for (const t of w.tiles.values()) if (t.set === "wall") stopLive(t);    // its live slots to the overlay, if on
    show(mapbox, m !== "cameras");
    show(split, split2());
    split.dataset.orient = m === "side" ? "vertical" : "horizontal";
    sep.sync();
    w.sig = "";
    render();
    requestAnimationFrame(() => { Panels.resizeMaps(); window.dispatchEvent(new Event("resize")); });
    for (const fn of w.subs) { try { fn(); } catch (e) { console.error(e); } }
    sync();
  }
  PHONE.addEventListener("change", layout);

  // Running: the Overview on screen and the page visible (the list feeds the rings and the overlay in every layout).
  const shown = () => !$("#overview").hidden && document.visibilityState !== "hidden";
  function sync() { if (shown()) start(); else stop(); }

  // ---- the list --------------------------------------------------------------------------------------------
  function highwayEvent() {          // the event whose cameras are pinned: an active one with a point, else a pending one
    const ov = ((cad.state && cad.state.overview) || []).filter((o) => o.emergency && o.lat != null && o.lon != null);
    return ov.find((o) => o.status === "active") || ov.find((o) => o.status === "pending") || null;
  }
  async function loadList(force) {
    const ev = highwayEvent();
    const p = new URLSearchParams({ area: "ours" });
    if (ev) { p.set("for", `${ev.lat},${ev.lon}`); p.set("label", ev.geo_label || ""); p.set("address", ev.address || ""); }
    const key = p.toString();
    if (w.loading || (!force && key === w.key && Date.now() - w.listAt < LIST_MS)) return;
    w.loading = true;
    try {
      const r = await fetch(`/api/cad/cameras?${p}`, { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error(`${r.status}`);
      w.list = await r.json();
      w.error = "";
    } catch (e) {
      w.error = `Cameras not loaded (${e.message})`;
    } finally {
      w.loading = false;
      w.key = key;
      w.listAt = Date.now();
    }
    render();
    for (const fn of w.subs) { try { fn(); } catch (e) { console.error(e); } }
  }

  // ---- the tiles -------------------------------------------------------------------------------------------
  const isDown = (c) => !!(c.down || c.off);
  const PIN = { before: "Before", after: "After", at: "At the event" };
  const ago = (t) => { const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.floor(s / 60)} min ago` : `${Math.floor(s / 3600)} h ago`; };
  const whereText = (c) => [c.road, c.dir, c.mile ? `MP ${c.mile}` : ""].filter(Boolean).join(" · ");
  const pinText = (c) => `${PIN[c.pin] || c.pin}${c.miles != null && c.pin !== "at" ? ` · ${c.miles.toFixed(1)} mi` : ""}`;

  function render() {
    if (!wallShown()) return;
    const d = w.list;
    const cams = (d && d.cameras) || [];
    const up = cams.filter((c) => !isDown(c)), down = cams.filter(isDown);
    const sig = JSON.stringify([up.map((c) => [c.id, c.pin, c.miles, c.has_video, c.name]), down.map((c) => c.id), w.downOpen, w.error,
                                d && d.area && d.area.note]);
    if (sig === w.sig) return;
    w.sig = sig;
    const head = el("div", "cw-head");
    const area = d && d.area;
    const live = up.filter((c) => c.has_video).length;
    head.append(el("b", "", "Cameras in our area"),
                el("span", "dim small", d ? `${up.length} up${live ? `, ${live} live` : ""}${down.length ? `, ${down.length} down` : ""}` : ""));
    if (area && area.basis !== "fire_districts" && area.note) head.append(el("span", "cw-basis small", area.note));
    const g = el("div", "cw-grid");
    const keep = new Set();
    for (const c of up) {
      const t = tile(c, "wall");
      keep.add(t.key);
      g.append(t.el);
    }
    if (down.length) g.append(downTile(down));
    for (const [k, t] of w.tiles) if (t.set === "wall" && !keep.has(k)) drop(t);
    const kids = [head];
    if (w.error) kids.push(el("div", "error", w.error));
    if (d && !d.configured) kids.push(el("div", "empty", "No camera source is on at the hub"));
    else if (d && !cams.length) kids.push(el("div", "empty", "No cameras in our area"));
    kids.push(g);
    if (d && d.cameras && cams.some((c) => c.has_video)) kids.push(el("div", "dim small cw-foot", "Live video through the hub, muted; only the tiles on screen play."));
    wall.replaceChildren(...kids);
    for (const t of w.tiles.values()) if (t.set === "wall" && (t.live || t.starting)) t.video.play().catch(() => {});   // a moved video pauses
  }

  // A tile in a set ("wall": with its caption; "overlay": opts.compact, the name only). The same camera can be a tile
  // in both sets (Side by side or Stacked with the overlay on): each is its own element, but only one of the two plays
  // live (twin(): the other shows snapshots), so a camera is one stream use and one budget slot.
  function tile(c, set, opts = {}) {
    const key = `${set}:${c.id}`;
    let t = w.tiles.get(key);
    if (!t) {
      const b = el("button", opts.compact ? "cw-tile co-tile" : "cw-tile");
      b.type = "button";
      b.dataset.camera = c.id;                               // a click opens the viewer (maplayers.js: any [data-camera])
      const frame = el("span", "cw-frame");
      const img = el("img", "cw-img");
      img.alt = "";
      img.hidden = true;
      const video = el("video", "cw-video");
      video.muted = true;
      video.autoplay = true;
      video.playsInline = true;
      video.setAttribute("muted", "");
      video.setAttribute("playsinline", "");
      video.hidden = true;
      const badge = el("span", "ml-live", "Live");
      badge.hidden = true;
      const pin = el("span", "cw-pin");
      pin.hidden = true;
      const note = el("span", "cw-note");
      note.hidden = true;
      frame.append(img, video, badge, pin, note);
      const name = el("span", "cw-name");
      const where = el("span", "cw-where");
      const age = el("span", "cw-age");
      const cap = el("span", "cw-cap");
      cap.append(name, ...(opts.compact ? [] : [where, age]));
      b.append(frame, cap);
      t = { key, set, id: c.id, el: b, img, video, badge, pin, note, name, where, age, cam: c, visible: false, etag: null, taken: 0, url: null,
            next: 0, busy: false, hls: null, live: false, starting: false, blockedUntil: 0, fps: null, away: 0 };
      w.tiles.set(key, t);
      observer.observe(b);
    }
    t.cam = c;
    t.name.textContent = c.name;
    t.where.textContent = whereText(c);
    t.el.title = `${c.name}${c.road ? `, ${c.road}` : ""}${c.dir ? ` ${c.dir}` : ""} (${c.source || "511NY"}). Open the viewer`;
    t.el.classList.toggle("pinned", !!c.pin);
    t.pin.hidden = !c.pin;
    if (c.pin) t.pin.textContent = pinText(c);
    t.img.alt = `Traffic camera: ${c.name}`;
    return t;
  }
  const twin = (t) => { for (const x of w.tiles.values()) if (x !== t && x.id === t.id && (x.live || x.starting)) return true; return false; };
  function drop(t) {
    observer.unobserve(t.el);
    stopLive(t);
    if (t.url) URL.revokeObjectURL(t.url);
    w.tiles.delete(t.key);
    t.el.remove();
  }

  function downTile(down) {
    const box = el("div", "cw-down" + (w.downOpen ? " open" : ""));
    const head = el("button", "cw-down-head");
    head.type = "button";
    head.setAttribute("aria-expanded", String(w.downOpen));
    head.append(el("span", "ml-down-badge", "!"), el("span", "", down.length === 1 ? "1 camera down" : `${down.length} cameras down`),
                el("span", "car dim", w.downOpen ? "▾" : "▸"));
    head.addEventListener("click", () => { w.downOpen = !w.downOpen; w.sig = ""; render(); });
    box.append(head);
    if (w.downOpen) {
      const ul = el("div", "ml-down-list");
      for (const c of down) {
        const b = el("button", "ml-down-item");
        b.type = "button";
        b.dataset.camera = c.id;
        b.setAttribute("aria-label", `camera down: ${c.name}`);
        const why = c.down ? (c.down_since ? `down since ${fmtTime(c.down_since)}` : "down") : `out of service (${c.source || "source"})`;
        b.append(el("span", "", c.name), el("span", "dim small", [whereText(c), why].filter(Boolean).join(" · ")));
        ul.append(b);
      }
      box.append(ul);
    }
    return box;
  }

  // ---- on screen: IntersectionObserver (a tile off screen a moment stops; a map pan past it doesn't churn) --------
  const observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const t = [...w.tiles.values()].find((x) => x.el === e.target);
      if (!t) continue;
      t.visible = e.isIntersecting;
      if (t.visible) { t.away = 0; if (!t.taken) t.next = 0; }
      else t.away = Date.now();
    }
    if (w.running) tick();
  }, { rootMargin: "100px 0px" });

  // ---- the loop: one ticker for every tile (snapshots due, live tiles to start, the ages) -----------------------
  function start() {
    if (w.running) return;
    w.running = true;
    loadList(true);
    w.timer = setInterval(tick, TICK_MS);
    tick();
  }
  function stop() {
    if (!w.running) return;
    w.running = false;
    clearInterval(w.timer);
    w.timer = null;
    for (const t of w.tiles.values()) stopLive(t);
  }
  function budget() {
    const s = (w.list && w.list.streams) || {};
    return Math.max(0, Number(s.per_viewer ?? s.streaming ?? 0));
  }
  function tick() {
    if (!w.running) return;
    if (Date.now() - w.listAt >= LIST_MS) loadList(false);
    const now = Date.now();
    let live = 0;
    for (const t of w.tiles.values()) {
      if ((t.live || t.starting) && !t.visible && t.away && now - t.away >= STOP_MS) stopLive(t);
      if (t.live || t.starting) live++;
    }
    for (const t of w.tiles.values()) {
      if (!t.visible || (t.set === "wall" && !wallShown())) continue;    // the wall just hidden: before the observer says so
      const c = t.cam;
      if (t.starting && now - t.startedAt > START_MS) {      // never got going: snapshots, and its live slot back
        stopLive(t);
        t.blockedUntil = now + RETRY_MS;
        live--;
      }
      const canLive = c.has_video && !isDown(c) && now >= t.blockedUntil;
      if (canLive && !t.live && !t.starting && live < budget() && !twin(t)) { live++; startLive(t); }
      if (!t.live && now >= t.next) snapshot(t);
      showAge(t);
    }
    if (now - w.statusAt >= STATUS_MS) { w.statusAt = now; for (const t of w.tiles.values()) if (t.live && t.visible) liveStatus(t); }
  }
  function showAge(t) {
    if (t.live) {
      const low = t.fps != null && t.fps < 5;
      t.age.textContent = low ? `Live · ~${t.fps} fps at the source` : "Live";
      if (!t.age.isConnected) {                              // a compact tile (the overlay): the note on the picture
        t.note.hidden = !low;
        if (low) t.note.textContent = `~${t.fps} fps at the source`;
      }
      return;
    }
    const cad_ = t.cam.cadence || 60;
    t.age.textContent = t.taken ? `Image ${ago(t.taken)}` : t.failed ? t.failed : "Loading…";
    t.age.title = cad_ >= 60 ? "Snapshot, updated about every minute" : `Snapshot, updated every ${cad_} s`;
  }

  // ---- snapshots: at the source's cadence, only when newer ---------------------------------------------------
  async function snapshot(t) {
    if (t.busy || w.fetching >= MAX_FETCHES) return;
    t.busy = true;
    w.fetching++;
    const every = Math.min(60, Math.max(4, t.cam.cadence || 60)) * 1000;
    t.next = Date.now() + every;
    try {
      const r = await fetch(`/api/cad/cameras/${encodeURIComponent(t.id)}/image`,
                            { credentials: "same-origin", cache: "no-store", headers: t.etag ? { "If-None-Match": t.etag } : {} });
      if (r.status === 304) return;                       // the hub's picture is the one shown
      if (!r.ok) {
        const e = await r.json().catch(() => null);
        t.failed = e && e.down ? "Camera down" : "No image";
        if (e && e.down) loadList(true);                  // it went down: into the folded tile
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      if (t.url) { const old = t.url; setTimeout(() => URL.revokeObjectURL(old), 1000); }
      t.url = url;
      t.img.src = url;
      t.img.hidden = t.live;
      t.taken = Number(r.headers.get("X-Image-Time")) || Date.now() / 1000;
      t.etag = r.headers.get("ETag") || null;
      t.failed = "";
    } catch (_) {
      t.failed = "No image";
    } finally {
      t.busy = false;
      w.fetching--;
      showAge(t);
    }
  }

  // ---- live video: hls.js (maplayers.js loads it once), through the hub with this page's token -----------------
  const streamUrl = (t, name) => `/api/cad/cameras/${encodeURIComponent(t.id)}/stream/${name}?v=${TOKEN}`;
  function startLive(t) {
    t.starting = true;
    t.startedAt = Date.now();
    t.fps = null;
    const v = t.video;
    const failed = (code, retryS, why) => {
      stopLive(t);
      t.blockedUntil = Date.now() + (retryS ? retryS * 1000 : RETRY_MS);
      t.note.hidden = false;
      t.note.title = why || "";
      t.note.textContent = code === 429 ? "Snapshots: the hub's live streams are all in use" : code === 503 ? "Snapshots: no live video from the source now" : "Snapshots: the stream stopped; trying again in a minute";
      t.next = 0;
    };
    v.onplaying = () => {
      if (!t.starting && !t.live) return;
      t.starting = false;
      t.live = true;
      v.hidden = false;
      t.img.hidden = true;
      t.badge.hidden = false;
      t.note.hidden = true;
      showAge(t);
    };
    v.onerror = () => failed(0, 0, "video error " + (v.error && v.error.message));
    const native = () => {                                  // Safari on iPhone: its own HLS
      if (!v.canPlayType("application/vnd.apple.mpegurl")) return failed(0, 3600);
      v.src = streamUrl(t, "playlist.m3u8");
      v.play().catch(() => failed(0));
    };
    MapLayers.loadHls().then((Hls) => {
      if (!t.starting) return;
      if (!Hls.isSupported()) return native();
      // A tile sits a little further behind the edge with less buffered than the viewer: many at once.
      const hls = new Hls({ enableWorker: false, lowLatencyMode: false, liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8,
                            maxBufferLength: 18, backBufferLength: 0, manifestLoadingTimeOut: 8000, manifestLoadingMaxRetry: 0,
                            levelLoadingTimeOut: 12000, levelLoadingMaxRetry: 2, fragLoadingTimeOut: 15000, fragLoadingMaxRetry: 2 });
      t.hls = hls;
      hls.on(Hls.Events.ERROR, (_, data) => {
        if (!data || !data.fatal) return;
        const code = data.response && data.response.code;
        failed(code, code === 503 ? 300 : 0, data.details);
      });
      hls.loadSource(streamUrl(t, "playlist.m3u8"));
      hls.attachMedia(v);
      hls.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch((e) => failed(0, 0, "play " + e)));
    }).catch(() => native());
  }
  function stopLive(t) {
    const was = t.live || t.starting;
    t.starting = false;
    t.live = false;
    if (t.hls) { try { t.hls.destroy(); } catch (_) { /* gone */ } t.hls = null; }
    try { t.video.pause(); t.video.removeAttribute("src"); t.video.load(); } catch (_) { /* gone */ }
    t.video.hidden = true;
    t.badge.hidden = true;
    t.img.hidden = !t.url;
    t.next = 0;
    if (was && !twin(t)) fetch(streamUrl(t, "close"), { credentials: "same-origin", cache: "no-store", keepalive: true }).catch(() => {});
  }
  async function liveStatus(t) {
    try {
      const r = await fetch(`/api/cad/cameras/${encodeURIComponent(t.id)}/stream/status`, { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) return;
      t.fps = (await r.json()).fps;
      showAge(t);
    } catch (_) { /* next time */ }
  }

  // ---- what starts and stops it ----------------------------------------------------------------------------------
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", stop);
  cad.hooks.push(() => {                                     // the Overview shown or not; the pinned event changed
    if (shown() !== w.running) sync();
    if (w.running) loadList(false);
  });
  MapLayers.subscribe(() => { if (w.running) loadList(true); });     // the hub's camera list or health changed
  new ResizeObserver(() => { if (split2()) { setShare(along() * L[mode()]); sep.sync(); } }).observe(view);

  layout();
  const engine = {
    list: () => w.list, subscribe: (fn) => w.subs.add(fn), tile, drop, layout: mode, isDown, pinText, OVERLAY,
    tick: () => { if (w.running) tick(); },
  };
  return { choose, layout: mode, state: w, prefs: L, token: TOKEN, engine };
})();
