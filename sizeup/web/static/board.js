// The wall board (/monitor): this same page read-only, for a second screen and for those not running
// the console (user, 2026-10-08: "it could show the overview on another screen of what is going on
// for those not operating the dispatch cad"). The CAD's own Overview (cadmap.js: the big map with
// the rigs and calls, active and pending cards, who is talking), and here: the main channels' radio (FG1,
// Fire/EMS 15, every talkgroup marked Main: the Radio view's Main, from /api/cad/comms; the console's pinned
// 60 Control feed is not shown here), the recent 60 Control pages and every unit's status in one strip. One scale step larger (cad.css
// html.board). Nothing to press but Sound (RadioLive: each new Main transmission and page as it
// comes in) and Full screen; a card opens nothing. Pages and Sound come from the monitor state
// (/api/stream, polling when streams are blocked), everything else from the CAD state.
"use strict";

(() => {
  if (!BOARD) return;
  const PAGES = 8;
  const b = { state: null };

  // ---- the main channels' radio: FG1 (fireground, marked), Fire/EMS 15, every Main talkgroup -----------------

  // Asked again when the CAD state's comms_rev moves (#137), else every SAFETY_MS; an older hub without
  // it: every RADIO_POLL_MS. Not while the page is hidden.
  const RADIO_N = 8, RADIO_POLL_MS = 2000, SAFETY_MS = 30000, RETRY_MS = 5000;   // the newest 8: pages and units stay on the screen below
  const r = { rev: "", rows: [], error: "", busy: false, timer: 0, failAt: 0 };
  const pushed = () => (cad.state && cad.state.comms_rev) || "";
  const fireground = (t) => {           // an FM traffic channel (FG1), not a talkgroup or the paging channel
    const fm = (((b.state && b.state.channels) || {}).fm || []);
    const ch = fm.find((c) => c.key === t.channel);
    return ch ? ch.kind === "traffic" : (t.talkgroup === null || t.talkgroup === undefined) && /^FG/i.test(t.channel || "");
  };
  async function pollRadio() {
    clearTimeout(r.timer);
    r.timer = 0;
    if (r.busy || document.visibilityState === "hidden") return;
    r.busy = true;
    try {
      const q = new URLSearchParams({ type: "radio" });
      if (r.rev) q.set("rev", r.rev);
      const d = await api("GET", `/api/cad/comms?${q}`);
      if (!d.unchanged) {
        r.rev = d.rev;
        // pages have their own list below (with the one going out now)
        r.rows = (d.rows || []).filter((t) => t.main !== false && t.kind !== "note" && !(t.tones && t.tones.length)).slice(0, RADIO_N);
      }
      r.error = "";
    } catch (e) {
      r.error = e.message;
      r.failAt = Date.now();
    }
    r.busy = false;
    renderRadio();
    if (document.visibilityState !== "hidden") r.timer = setTimeout(pollRadio, pushed() ? SAFETY_MS : RADIO_POLL_MS);
  }
  function renderRadio() {
    setText($("#b-radio-sum"), [...new Set(r.rows.map((t) => t.channel))].join(" · ") || "Main channels");
    syncList($("#b-radio"), r.rows, {
      key: (t) => t.id,
      sig: (t) => JSON.stringify([t.text, t.status, t.rig, t.talker, t.has_audio, t.model, t.alt_text, fireground(t), talkSig(t.rig)]),
      make: () => el("li"),
      fill: (li, t) => {
        const n = radioRow(t);                     // cad.js: play, time, rig, channel, duration, the transcript
        const fg = fireground(t);
        li.className = n.className + (fg ? " fg" : "");
        li.replaceChildren(...n.childNodes);
        const chip = li.querySelector(".chip.src");
        if (chip) { chip.classList.toggle("fg", fg); if (fg) chip.title = "Fireground"; }
      },
      empty: r.error ? `Load failed: ${r.error}` : "None",
    });
  }

  // ---- pages ----------------------------------------------------------------------------------------

  function renderPages() {
    const pages = ((b.state && b.state.pages) || []).slice().sort((x, y) => y.start - x.start).slice(0, PAGES);
    // a page going out now (South 2): over the list, until its finished row is in it
    Radio.fillPaging($("#b-paging"), b.state && b.state.paging, new Set(pages.map((p) => p.id)));
    syncList($("#b-pages"), pages, {
      key: (p) => p.id,
      sig: (p) => JSON.stringify([p.status, p.raw, p.departments, p.tones, p.label, p.has_audio, p.dispatch && p.dispatch.incident_type]),
      make: () => el("li"),
      fill: (li, p) => {
        const meta = el("div", "meta");
        const play = Icon.play(el("button", "play"));
        play.type = "button";
        play.title = "Play";
        play.disabled = p.has_audio === false;
        const url = `/api/audio/${encodeURIComponent(p.id)}.wav`;
        if (window.playBtn) window.playBtn(play, url); else play.addEventListener("click", () => window.play(url));
        meta.append(play, el("span", "t", fmtTime(p.start, true)), el("span", "chip src", p.label || "?"));
        const d = p.dispatch;
        if (d && d.incident_type) meta.append(el("b", "ptype", d.incident_type));
        if (p.tones && p.tones.length) meta.append(...Radio.pageTags(p.tones));   // unknown pairs as their tones
        else for (const name of p.departments || []) meta.append(el("span", "chip dept", name));
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
  cad.hooks.push(() => {                         // the state push: a transmission logged or changed
    const rev = pushed();
    if (rev && rev !== r.rev && !r.busy && !(r.failAt && Date.now() - r.failAt < RETRY_MS)) pollRadio();
  });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState !== "hidden") pollRadio(); });
  liveState("/api/stream", "/api/state", onMonitor);
  pollRadio();
})();
