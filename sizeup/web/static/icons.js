// Line icons from /static/icons.svg (one sprite, shared by the CAD and the wall board).
//
//   Icon("bell")               <svg class="ico"><use href="/static/icons.svg#i-bell"></svg>, aria-hidden
//   Icon.play(button, state)   a play button's face: "play", "stop" or "none" (no recording: a dash)
"use strict";

window.Icon = (() => {
  const NS = "http://www.w3.org/2000/svg";
  function Icon(name, cls = "") {
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("class", cls ? `ico ${cls}` : "ico");
    s.setAttribute("aria-hidden", "true");
    s.setAttribute("focusable", "false");
    const u = document.createElementNS(NS, "use");
    u.setAttribute("href", `/static/icons.svg#i-${name}`);
    s.append(u);
    return s;
  }
  const LABEL = { play: "Play", stop: "Stop", none: "No recording" };
  Icon.play = (b, state = "play") => {
    if (b.dataset.icon === state) return b;
    b.dataset.icon = state;
    b.setAttribute("aria-label", LABEL[state] || state);
    if (state === "none") b.replaceChildren("–");
    else b.replaceChildren(Icon(state));
    return b;
  };
  return Icon;
})();
