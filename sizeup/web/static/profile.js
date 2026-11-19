// The profile button, top right of the header (#818; admin, 2026-11: "move Re-sign in to the top right ... next to a
// profile icon that will later hold user profile settings"). Every page with the console's header: /cad and /monitor.
//
//   the button   a round avatar: the signed-in person's initials (whoami's name), else a person icon. Its ring says
//                how the Cloudflare Access sign-in stands (session.js): amber from 10 min before the end, red once
//                it has ended; beside it then a chip, "9 min · Re-sign in" / "Signed out · Re-sign in" (a link: the
//                re-sign-in pop-up, blocked: a new tab). What it means is in the hover text only.
//   its menu     UI.menu (keys, Esc, a click outside, fits a phone): who (name, role), "Sign-in until 14:32" (not
//                without Access), Re-sign in (the same pop-up), then ITEMS: what comes later (Preferences: the
//                per-user settings, GET/PUT /api/me/prefs, not built yet: hidden).
"use strict";

window.Profile = (() => {
  const box = document.getElementById("me");
  if (!box || !window.UI || !window.SessionWatch) return null;
  // later entries, each {label, run, hidden?}: Profile.add() puts one here; hidden ones are left out
  const ITEMS = [
    { label: "Preferences", hidden: true, run: () => {} },   // the per-user settings (/api/me/prefs): not yet
  ];

  const who = () => (typeof cad !== "undefined" && cad.who) || null;
  function initials(name) {
    const n = String(name || "").trim().split("@")[0];
    const words = n.split(/[\s._-]+/).filter((w) => /^[A-Za-z0-9]/.test(w));
    return words.slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  }
  const clock = (ms) => {
    const d = new Date(ms);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  const chip = document.createElement("a");
  chip.className = "pill me-chip";
  chip.target = "_blank";
  chip.rel = "noopener";
  chip.hidden = true;
  chip.addEventListener("click", (e) => SessionWatch.reauth(e));
  const more = document.createElement("span");     // folded on a narrow header (style.css .me-more)
  more.className = "me-more";
  more.textContent = " · Re-sign in";
  const face = document.createElement("span");
  face.className = "me-av";
  const btn = UI.menu.button("Profile", items, { key: "profile", cls: "me-btn", face });
  box.append(chip, btn);

  function items() {
    const w = who(), s = SessionWatch.state(), out = [];
    const op = document.getElementById("operator");
    out.push({ info: true, label: w ? (w.callsign ? `${w.name} ${w.callsign}` : w.name) : (op && op.value) || "Not signed in",
               hint: w ? w.role || "" : "",
               title: w ? w.email : "" });
    if (s.level === "out") out.push({ info: true, label: "Signed out", title: SessionWatch.tip() });
    else if (s.until) out.push({ info: true, label: `Sign-in until ${clock(s.until)}`, title: SessionWatch.tip() });
    if (s.access) out.push({ label: "Re-sign in", href: SessionWatch.href(), run: (e) => SessionWatch.reauth(e) });
    const more = ITEMS.filter((x) => !x.hidden);
    if (more.length) out.push("-", ...more);
    return out;
  }

  function render() {
    const w = who(), s = SessionWatch.state();
    const ini = w ? initials(w.name || w.email) : "";
    const sig = ini || "?";
    if (face.dataset.sig !== sig) {
      face.dataset.sig = sig;
      face.replaceChildren(ini || (window.Icon ? Icon("user") : "?"));
    }
    btn.classList.toggle("warn", s.level === "warn");
    btn.classList.toggle("err", s.level === "out");
    const name = w ? (w.name === w.email ? w.email : `${w.name} (${w.email})`) : "Profile";
    const tip = SessionWatch.tip();
    btn.title = tip ? `${name}\n${tip}` : name;
    btn.setAttribute("aria-label", s.label ? `Profile: ${s.label}` : "Profile");
    chip.hidden = !s.level;
    if (s.level) {
      const text = s.level === "out" ? "Signed out" : `${s.left} min`;
      if (chip.firstChild?.textContent !== text) chip.replaceChildren(text, more);
      chip.setAttribute("aria-label", `${text}: Re-sign in`);
      chip.classList.toggle("err", s.level === "out");
      if (chip.getAttribute("href") !== SessionWatch.href()) chip.setAttribute("href", SessionWatch.href());
      chip.title = tip;
    }
  }
  window.addEventListener("sizeup:session", render);
  window.addEventListener("sizeup:who", render);
  render();
  return { render, add: (it) => { ITEMS.push(it); }, items };
})();
