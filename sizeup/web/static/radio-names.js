// One name per radio source on the page (sizeup/radionames.py is the hub's half): every chip and line that names a
// channel, talkgroup or frequency asks here, never t.channel itself.
//
//   RadioNames.short(row)   the chip: the admins' short label (Settings > Radios > Channels, beside the name), else the
//                           name
//   RadioNames.name(row)    the full name: the admins' display name (the rail's Edit and Settings > Radios > Channels:
//                           one store), else the source's own label (its Alpha Tag, the channel's label)
//   RadioNames.title(row)   the hover: "MRRS · <name> (TG 1234)", "Analog · FG1 (154.1 MHz)": never ambiguous
//
// row: a radio-log row or anything shaped like one ({source, channel, talkgroup | tg, system | p25_system, mhz}); a bare
// source key works too. Display only: filters, links, menus and every comparison keep the real label.
// Fed by GET /api/cad/radio-names, asked again when the state push's radio_names (their rev) moves; the rows are drawn
// again then (cad.txRev). Before it answers: the real labels, as before.
"use strict";

window.RadioNames = (() => {
  const COUNTY = "westchester";
  const SYS_DEFAULT = { analog: "Analog", airband: "Airband", [COUNTY]: "County" };
  const st = { rev: null, asked: null, names: {}, shorts: {}, systems: {}, freqs: {} };

  // "tg:307" is the county's p25:westchester:307 (names saved before sources were qualified)
  function qualified(k) {
    const s = String(k || ""), m = s.match(/^tg:(\d{1,9})$/);
    return m ? `p25:${COUNTY}:${m[1]}` : s;
  }
  function keyOf(t) {
    if (t === null || t === undefined) return "";
    if (typeof t !== "object") return qualified(t);
    if (t.source) return qualified(t.source);
    const tg = t.talkgroup !== undefined && t.talkgroup !== null ? t.talkgroup : t.tg;
    if (tg !== undefined && tg !== null && /^\d+$/.test(String(tg))) {
      const sys = String(t.p25_system || t.system || "");
      return `p25:${!sys || ["analog", "airband", "fm"].includes(sys) ? COUNTY : sys}:${tg}`;
    }
    const ch = String(t.channel || "");
    if (!ch) return "";
    const air = t.system === "airband" || t.service === "air" || `air:${ch}` in st.names || `air:${ch}` in st.shorts;
    return `${air ? "air" : "fm"}:${ch}`;
  }
  function real(t) {
    if (t && typeof t === "object" && t.channel) return String(t.channel);
    const k = keyOf(t), m = k.match(/^p25:[^:]+:(\d+)$/);
    return m ? `TG ${m[1]}` : k.split(":").slice(1).join(":");
  }
  const name = (t) => st.names[keyOf(t)] || real(t);
  const short = (t) => st.shorts[keyOf(t)] || name(t);
  function systemLabel(t) {
    const k = keyOf(t), m = k.match(/^p25:([^:]+):/);
    const sys = m ? m[1] : k.startsWith("air:") ? "airband" : "analog";
    return st.systems[sys] || SYS_DEFAULT[sys] || sys;
  }
  function title(t) {
    const k = keyOf(t), m = k.match(/^p25:[^:]+:(\d+)$/);
    const mhz = t && typeof t === "object" && +t.mhz > 0 ? +t.mhz : st.freqs[k.split(":").slice(1).join(":")];
    let num = m ? `TG ${m[1]}` : mhz ? `${+mhz} MHz` : "";
    const full = name(t);
    if (full !== real(t)) num = [real(t), num].filter(Boolean).join(", ");     // its own label too, when renamed
    return `${systemLabel(t)} · ${full}${num ? ` (${num})` : ""}`;
  }
  // a chip (or any element) labelled for a source: the short label, the title on hover
  function label(node, t) {
    node.textContent = short(t);
    node.title = title(t);
    return node;
  }

  function set(d) {
    const q = (m) => Object.fromEntries(Object.entries(m && typeof m === "object" ? m : {}).map(([k, v]) => [qualified(k), String(v)]));
    st.names = q(d && d.names);
    st.shorts = q(d && d.shorts);
    st.systems = (d && d.systems) || {};
    st.freqs = (d && d.freqs) || {};
    st.rev = (d && typeof d.rev === "string") ? d.rev : "";
  }
  async function load() {
    try {
      const r = await fetch("/api/cad/radio-names", { credentials: "same-origin", cache: "no-store", redirect: "manual" });
      if (!r.ok) return;
      set(await r.json());
    } catch (_) { st.asked = null; return; }     // the next push asks again
    if (typeof cad !== "undefined") {            // every row and chip drawn again with the names
      cad.txRev++;
      if (cad.state && typeof render === "function") render();
    }
  }
  function onPush() {
    const rev = typeof cad !== "undefined" && cad.state ? cad.state.radio_names : undefined;
    if (typeof rev !== "string" || rev === st.rev || rev === st.asked) return;
    st.asked = rev;
    load();
  }
  if (typeof cad !== "undefined" && cad.hooks) cad.hooks.push(onPush);

  return { short, name, title, label, keyOf, systemLabel, set, get rev() { return st.rev; } };
})();
