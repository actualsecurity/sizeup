// The wall board (/monitor): this same page read-only, for a second screen and for those not running
// the console (user, 2026-10-08: "it could show the overview on another screen of what is going on
// for those not operating the dispatch cad"). The CAD's own Overview (cadmap.js: the big map with
// the rigs and calls, active and pending cards, who is talking), the pinned 60 Control feed, and here:
// the recent 60 Control pages and every unit's status in one strip. One scale step larger (cad.css
// html.board). Nothing to press but Sound (RadioLive: each new Main transmission and page as it
// comes in) and Full screen; a card opens nothing. Pages and Sound come from the monitor state
// (/api/stream, polling when streams are blocked), everything else from the CAD state.
"use strict";

(() => {
  if (!BOARD) return;
  const PAGES = 8;
  const b = { state: null };

  // ---- pages ----------------------------------------------------------------------------------------

  function renderPages() {
    const pages = ((b.state && b.state.pages) || []).slice().sort((x, y) => y.start - x.start).slice(0, PAGES);
    syncList($("#b-pages"), pages, {
      key: (p) => p.id,
      sig: (p) => JSON.stringify([p.status, p.raw, p.departments, p.label, p.has_audio, p.dispatch && p.dispatch.incident_type]),
      make: () => el("li"),
      fill: (li, p) => {
        const meta = el("div", "meta");
        const play = Icon.play(el("button", "play"));
        play.type = "button";
        play.title = "Play";
        play.disabled = p.has_audio === false;
        play.addEventListener("click", () => window.play(`/api/audio/${encodeURIComponent(p.id)}.wav`));
        meta.append(play, el("span", "t", fmtTime(p.start, true)), el("span", "chip src", p.label || "?"));
        const d = p.dispatch;
        if (d && d.incident_type) meta.append(el("b", "ptype", d.incident_type));
        for (const name of p.departments || []) meta.append(el("span", "chip dept", name));
        const done = p.status === "done" && p.raw;
        li.replaceChildren(meta, el("div", done ? "txt clamp" : "txt muted", done ? p.raw
          : { pending: "Transcribing", no_voice: "Tones only", no_speech: "No speech", error: "Transcription failed" }[p.status] || p.status));
      },
      empty: "None",
    });
  }

  // Sound: what the old monitor's Live played. Main traffic (FG1, Main talkgroups) and the pages.
  function offerSound() {
    const s = b.state;
    if (!s) return;
    const main = new Map((s.talkgroups || []).map((r) => [r.tg, !!r.main]));
    const isMain = (t) => t.talkgroup === null || t.talkgroup === undefined || main.get(t.talkgroup);
    const inc = (s.incidents || []).find((i) => i.status === "active");
    const traffic = [...(s.loose_traffic || []), ...((inc && inc.traffic) || [])].filter(isMain);
    RadioLive.offer([...traffic.map((t) => ({ id: t.id, start: t.start, has: t.has_audio, keyup: t.status === "no_speech" })),
                     ...(s.pages || []).map((p) => ({ id: p.id, start: p.start, has: p.has_audio, keyup: false }))]);
  }

  function onMonitor(s) {
    if (!s || s.error) return;
    b.state = s;
    renderPages();
    offerSound();
  }

  // ---- every unit's status in one strip (the console's board, without its buttons) --------------------

  function renderStrip() {
    const units = ((cad.state && cad.state.units) || []).filter((u) => {
      const board = (L().unit_boards || []).find((x) => x.id === u.board);
      return (board && board.group_by === "kind" && !u.mutual_aid && u.response !== "agency") || u.incident_id;
    });
    const busy = (u) => (u.incident_id ? 0 : roleOf(u.status) === "free" ? 2 : 1);   // on a call, out, then free
    const list = units.slice().sort((x, y) => busy(x) - busy(y));
    syncList($("#b-units"), list, {
      key: (u) => String(u.id),
      sig: (u) => JSON.stringify([u.name, u.status, u.incident_id, talkSig(u.name), cad.listSig.length]),
      make: () => el("span"),
      fill: (node, u) => {
        const inc = u.incident_id && incidents().find((i) => i.id === u.incident_id);
        const c = statusChip(u.status, u.name);
        node.className = c.className + " b-unit" + (Radio.talkingFor(u.name) ? " talking" : "");
        node.style.cssText = c.style.cssText;
        node.textContent = u.name;
        node.title = `${u.name}: ${unitStatus(u.status).name}` + (inc ? `, ${incTitle(inc)} ${inc.address || ""}` : "");
      },
      empty: "None",
    });
  }

  // ---- full screen --------------------------------------------------------------------------------------

  function fullscreen() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else document.documentElement.requestFullscreen().catch(() => {});
  }
  document.addEventListener("fullscreenchange", () => $("#fullscreen").classList.toggle("on", !!document.fullscreenElement));
  $("#fullscreen").addEventListener("click", fullscreen);
  window.addEventListener("keydown", (e) => {
    if (e.key === "f" && !e.target.closest("input, select, textarea")) fullscreen();
  });

  RadioLive.button($("#sound"), "Sound");
  cad.hooks.push(renderStrip);
  liveState("/api/stream", "/api/state", onMonitor);
})();
