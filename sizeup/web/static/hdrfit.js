// The header's fold, measured (admin, 2026-11: at the same window width the header sometimes fit and sometimes ran off
// the right edge, the profile button and the clock with it). Width breakpoints cannot know what is in it: the incident
// source's label and state, the weather, the station's state, the sign-in chip, the zoom. So it is measured: whenever
// its width or what is in it changes, items fold in a fixed order until it fits, and come back when there is room again
// (HYST px more than they need, so a few px either way never flips one back and forth).
//
//   STEPS     cumulative, each a class on the header (fold-<step>); what each one hides or condenses is in style.css
//             and cad.css (".fold-"): tool labels, the brand name, the page name, "· Re-sign in", the weather, the
//             date, Zulu, the tabs' labels, long state chips cut short, good-state chips as dots, the Run cards and
//             Monitor links (as on a phone), tighter gaps, and last the state texts may shrink (never a control). The
//             profile button, the local time and the sign-in chip ("9 min") never go. A cut chip or a dot keeps its
//             whole text on hover and for a screen reader (it is only clipped). Who is talking takes only the room
//             left: it never folds anything (the header would reshuffle on every key-up).
//   room      the spacers' width; any overflow (the header's scrollWidth past its clientWidth), less than none.
//   when      the window resizes (zoom too); the header's content changes (a MutationObserver: a microtask, so before
//             the paint; a value set to what it already was is no change); a ticking text (the clock, a ringing phone's
//             age) only when its width changes (a ResizeObserver, then the next frame); the header's own width (a
//             scrollbar); the fonts loaded.
"use strict";

window.HeaderFit = (() => {
  const STEPS = ["tools", "brand", "title", "resign", "wx", "date", "zulu", "tabs", "chips", "dots", "links", "tight", "squeeze"];
  const HYST = 16;                         // px of room beyond its own that a folded item needs to come back
  const TICKS = "#clock, .pa-age";         // text that changes every second: watched by its size, not its mutations
  const TICKING = "#clock, #prealert";     // (what holds it: the ResizeObserver's)

  // The fewest steps that fit, from `level`: fold while there is no room; unfold one step at a time while the step below
  // leaves HYST px spare. room(n) applies n steps and measures; the result is the last one applied.
  function search(level, room, max = STEPS.length, hyst = HYST) {
    let n = Math.max(0, Math.min(level, max));
    if (room(n) < 0) {
      while (n < max) if (room(++n) >= 0) break;
      return n;
    }
    while (n > 0) {
      if (room(n - 1) >= hyst) n--;
      else { room(n); break; }           // that one doesn't come back yet: as it was
    }
    return n;
  }
  // Then the room left over: a step folded before the last one needed comes back on its own when it fits (the most
  // important first: the time's Zulu before the date, the date before the weather...), so a wide tab row folded to
  // icons doesn't leave a gap where the date would fit. Only items that just hide (BACK); a step already back needs
  // no spare room to stay, one coming back HYST px. room(n, back) applies n steps less those in back and measures;
  // the result is the last one applied.
  const BACK = new Set(["tools", "brand", "title", "resign", "wx", "date", "zulu"]);
  function backfill(n, room, was = new Set(), hyst = HYST) {
    const back = new Set();
    if (STEPS[n - 1] === "squeeze") return back;     // (squeezed, the room is the texts': none to give)
    let ok = true;
    for (let i = n - 2; i >= 0; i--) {
      if (!BACK.has(STEPS[i])) continue;
      back.add(i);
      ok = room(n, back) >= (was.has(i) ? 0 : hyst);
      if (!ok) back.delete(i);
    }
    if (!ok) room(n, back);
    return back;
  }

  const hdr = document.querySelector("body > header");
  if (!hdr || !window.MutationObserver || !window.ResizeObserver) return { search, backfill, STEPS, HYST };

  const st = { level: 0, back: new Set(), rows: 0, w: -1, frame: 0, runs: 0 };
  const NONE = new Set();
  function apply(n, back) {
    STEPS.forEach((s, i) => {
      const on = i < n && !back.has(i);
      if (hdr.classList.contains(`fold-${s}`) !== on) hdr.classList.toggle(`fold-${s}`, on);
    });
  }
  // a phone's header wraps, to the rows its CSS says (--hdr-rows, and one more for who is talking): a row more is
  // no room, as an overflow is on a desk
  function rows() {
    let n = 0, bottom = -Infinity;
    const boxes = [...hdr.children].filter((c) => !c.classList.contains("talking-strip")).map((c) => c.getBoundingClientRect())
      .filter((b) => b.width || b.height).sort((a, b) => a.top - b.top);
    for (const b of boxes) {
      if (b.top >= bottom - 1) { n++; bottom = b.bottom; } else bottom = Math.max(bottom, b.bottom);
    }
    return n;
  }
  function room(n, back = NONE) {
    apply(n, back);
    const over = hdr.scrollWidth - hdr.clientWidth;
    if (over > 0) return -over;
    if (st.rows && rows() > st.rows) return -1;
    let w = 0;                             // (on a phone, the first row's room)
    for (const c of hdr.children) if (c.classList.contains("spacer")) w += c.getBoundingClientRect().width;
    return w;
  }
  function fit() {
    if (st.frame) cancelAnimationFrame(st.frame);
    st.frame = 0;
    if (!hdr.clientWidth) st.w = -1;       // (not laid out: nothing to measure; measured again once it is)
    else {
      st.runs++;
      st.rows = Number(getComputedStyle(hdr).getPropertyValue("--hdr-rows")) || 0;   // 0: one row, no wrapping
      // as it is fits, and nothing folded or with less than HYST to spare and nothing out of order: nothing can change
      // (one layout, not ~10)
      const now = room(st.level, st.back);
      if (!(now >= 0 && (st.level === 0 || (now < HYST && !st.back.size)))) {
        // (from the steps as they were, the ones back folded again: the search counts in whole steps)
        st.level = search(st.level, (n) => room(n));
        st.back = backfill(st.level, room, st.back);
      }
      st.w = hdr.clientWidth;
    }
    mo.takeRecords();                      // its own changes are not content
  }
  const later = () => { if (!st.frame) st.frame = requestAnimationFrame(fit); };

  const ticking = (n) => !!(n.nodeType === 1 ? n : n.parentElement)?.closest(TICKS);
  function changed(r) {
    if (r.type === "attributes") return r.target !== hdr && r.oldValue !== r.target.getAttribute(r.attributeName);
    if (r.type === "characterData") return r.oldValue !== r.target.data && !ticking(r.target);
    return !ticking(r.target);
  }
  const mo = new MutationObserver((rs) => { if (rs.some(changed)) fit(); });
  mo.observe(hdr, { subtree: true, childList: true, characterData: true, characterDataOldValue: true,
                    attributes: true, attributeOldValue: true, attributeFilter: ["hidden", "class", "style"] });
  // never a fold in the observer's own callback (it would resize what it watches): the next frame
  const ro = new ResizeObserver((es) => { if (es.some((e) => e.target !== hdr || hdr.clientWidth !== st.w)) later(); });
  ro.observe(hdr);
  for (const n of hdr.querySelectorAll(TICKING)) ro.observe(n);
  // runs before the frame's paint; a zoom is a resize too; only the height: nothing to do
  window.addEventListener("resize", () => { if (hdr.clientWidth !== st.w) fit(); });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
  window.addEventListener("load", fit);

  // a chip cut short or shown as a dot: its whole text on hover, when its own script gave it no title
  hdr.addEventListener("pointerover", (e) => {
    const c = e.target instanceof Element && e.target.closest(".pill, .station");
    if (!c || c.parentElement !== hdr || (c.title && c.title !== c.dataset.fitTip)) return;
    const text = c.textContent.replace(/\s+/g, " ").trim();
    if (c.title !== text) c.title = c.dataset.fitTip = text;
  });

  fit();
  return { fit, search, backfill, STEPS, HYST,
           state: () => ({ level: st.level, steps: STEPS.filter((_, i) => i < st.level && !st.back.has(i)), runs: st.runs }) };
})();
