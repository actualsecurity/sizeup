// The living style guide (/cad#styleguide; Settings > System links it, admins only, not in the main nav):
// the tokens and every component of style.css in every state, drawn with the same classes and helpers
// the views use, so a change to the look shows here first. Uses cad.js's helpers (el, statusChip, L ...).
"use strict";

(() => {
  if (BOARD) return;
  const box = $("#styleguide");
  const SECTIONS = [["sg-tokens", "Tokens"], ["sg-buttons", "Buttons"], ["sg-fields", "Fields"], ["sg-tags", "Tags"],
    ["sg-cards", "Cards & heads"], ["sg-lists", "Lists & rows"], ["sg-nav", "Tabs & segmented"], ["sg-feedback", "Feedback"],
    ["sg-table", "Table"], ["sg-keys", "Shortcuts"]];
  let drawnFor = "";

  const row = (label, ...kids) => {
    const r = el("div", "sg-row");
    r.append(el("span", "sg-l", label), ...kids);
    return r;
  };
  const btn = (text, cls = "", attrs = {}) => {
    const b = el("button", cls, text);
    b.type = "button";
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "disabled") b.disabled = true; else b.setAttribute(k, v);
    }
    return b;
  };
  const card = (id, title, help, ...kids) => {
    const c = el("section", "card");
    c.id = id;
    const h = el("div", "card-head");
    h.append(el("h2", "", title));
    if (help) h.append(Help.button(help));
    c.append(h, ...kids);
    return c;
  };
  // a token's value as the browser resolves it (a length in px, a color as written)
  const tokenValue = (name) => {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (raw.startsWith("#")) return raw;
    const probe = el("div");
    probe.style.cssText = `position:absolute;visibility:hidden;inline-size:${raw}`;
    document.body.append(probe);
    const v = getComputedStyle(probe).width;
    probe.remove();
    return v === "0px" && !/^0/.test(raw) ? raw : `${Math.round(parseFloat(v) * 100) / 100}px`;
  };

  function tokens() {
    const type = el("div");
    for (const [t, use] of [["2xs", "Tags, overlines"], ["xs", "Meta lines, small buttons, labels"], ["sm", "Controls, secondary text"],
      ["md", "Body"], ["lg", "View titles, lead lines"], ["xl", "The incident's address"], ["2xl", "The incident's type"]]) {
      const s = el("span", "", `${use}`);
      s.style.fontSize = `var(--fs-${t})`;
      s.style.lineHeight = `var(--lh-${t})`;
      type.append(row(`--fs-${t} ${tokenValue(`--fs-${t}`)}`, s));
    }
    const space = el("div");
    for (let i = 0; i <= 8; i++) {
      const bar = el("div", "sg-space");
      bar.style.inlineSize = `var(--s-${i})`;
      space.append(row(`--s-${i} ${tokenValue(`--s-${i}`)}`, bar));
    }
    const ctl = el("div");
    for (const t of ["ctl-sm", "ctl", "ctl-lg", "tag-h", "ico-sm", "ico", "ico-lg", "radius-s", "radius", "hdr-h"]) {
      ctl.append(row(`--${t}`, el("code", "", tokenValue(`--${t}`))));
    }
    const colors = el("div");
    for (const t of ["bg", "panel", "raised", "hover", "line", "line-strong", "text", "text-2", "dim", "accent", "accent-text",
      "focus", "ok", "ok-text", "warn", "err", "info"]) {
      const sw = el("span", "sg-swatch");
      sw.style.background = `var(--${t})`;
      colors.append(row(`--${t}`, sw, el("code", "dim", tokenValue(`--${t}`))));
    }
    const grid = el("div", "sg-grid");
    grid.append(type, space, ctl, colors);
    return card("sg-tokens", "Tokens", "style.css @layer tokens: rem type on a 4 px grid, three control heights (44 px on a "
      + "touch screen), two radii. The wall board re-reads every size with --zoom 1.25.", grid);
  }

  function buttons() {
    const states = (cls) => [btn("Default", cls), btn("Pressed", cls, { "aria-pressed": "true" }), btn("Busy", cls, { "aria-busy": "true" }),
      btn("Disabled", cls, { disabled: true }), (() => { const b = btn("Focus", cls); b.style.outline = "var(--focus-w) solid var(--focus)";
        b.style.outlineOffset = "var(--focus-off)"; return b; })()];
    const ico = (name, cls, label) => { const b = btn("", cls); b.append(Icon(name)); b.setAttribute("aria-label", label); b.title = label; return b; };
    const withIco = (name, text, cls) => { const b = btn("", cls); b.append(Icon(name), text); return b; };
    const st = (L().unit_statuses || []).slice(0, 5);
    const stat = st.map((s, i) => statusColors(btn(s.name, "mini" + (i === 0 ? " on" : "")), s));
    const link = el("a", "nav", "Link as a button");
    link.href = "#styleguide";
    return card("sg-buttons", "Buttons", "<button> is a secondary button; .primary / .danger fill with PCFD red; .quiet has no "
      + "frame. .sm (24 px), default (28 px), .lg (36 px), .icon square. Busy while its request runs.",
    row("Secondary", ...states("")), row("Primary", ...states("primary")), row("Danger", ...states("danger")),
    row("Quiet", ...states("quiet")), row("Danger quiet", btn("Delete", "danger-quiet"), btn("Remove", "danger-quiet sm")),
    row("Sizes", btn("Small", "sm"), btn("Default"), btn("Large", "lg primary"), withIco("plus", "With icon", "primary")),
    row("Icon only", ico("x", "icon quiet", "Close"), ico("expand", "icon", "Expand"), ico("radio", "icon sm", "Radio"), Icon.play(btn("", "play"))),
    row("Status", ...stat), row("Link", link),
    row("Toggle", (() => { const b = btn("Live", "live-btn"); RadioLive.button(b, "Live"); return b; })()));
  }

  function fields() {
    const f = (label, ctl, extra) => { const l = el("label", "field"); l.append(label, ctl); if (extra) l.append(extra); return l; };
    const input = (attrs = {}) => { const i = el("input"); i.type = "text"; Object.assign(i, attrs); return i; };
    const bad = input({ value: "12 Nowhere Rd" });
    bad.setAttribute("aria-invalid", "true");
    const sel = el("select");
    fillSelect(sel, [["a", "Alarm"], ["s", "Structure fire"]]);
    const ta = el("textarea");
    ta.rows = 2;
    ta.placeholder = "Remarks";
    const check = (text, on, dis) => { const l = el("label", "check"); const c = el("input"); c.type = "checkbox"; c.checked = on; c.disabled = !!dis;
      l.append(c, el("span", "", text)); return l; };
    const sw = (text, on, mixed) => { const b = btn("", "sw" + (on ? " on" : "") + (mixed ? " mixed" : "")); b.setAttribute("role", "switch");
      b.setAttribute("aria-checked", String(!!on)); b.append(el("i", "knob"), el("span", "lbl", text));
      b.addEventListener("click", () => { const v = b.getAttribute("aria-checked") !== "true"; b.classList.toggle("on", v); b.setAttribute("aria-checked", String(v)); });
      return b; };
    const grid = el("div", "form");
    grid.append(f("Location", input({ placeholder: "Address, place or intersection" })), f("Type", sel),
      f("Invalid", bad, el("span", "error", "No match picked: saved as typed")), f("Disabled", input({ value: "Locked", disabled: true })),
      Object.assign(f("Remarks", ta), { className: "field span2" }));
    return card("sg-fields", "Fields", "Fields are 28 px (44 on touch), 14 px text; a .field stacks its label over it, an error under it. "
      + "A .check label is the whole target.", grid,
    row("Checks", check("Emergency", true), check("Full department response", false), check("Disabled", false, true)),
    row("Switch", sw("Record", true), sw("Main", false), sw("Mixed", false, true)),
    row("Color", Object.assign(el("input"), { type: "color", value: "#c8102e" })));
  }

  function tags() {
    const chip = (text, cls, c) => { const s = el("span", cls, text); if (c) s.style.setProperty("--c", c); return s; };
    const st = (L().unit_statuses || []).slice(0, 6).map((s) => statusChip(s.code, `E60 ${s.code}`));
    const ub = el("button", "unit-tag", "E63");
    ub.type = "button";
    const ev = el("button", "feed-ev", "26-000002");
    ev.type = "button";
    return card("sg-tags", "Tags", "One height (18 px), 11 px bold. Chips are outlined in their color, pills say a state, "
      + "badges are filled. As a button a tag is a 24 px target.",
    row("Status chips", ...st),
    row("Chips", chip("spare", "chip spare"), chip("default", "chip default"), chip("edited", "chip edited"), chip("RB", "chip joint"),
      chip("unconfirmed", "chip s-confirm")),
    row("Pills", chip("Live", "pill ok"), chip("Connecting", "pill"), chip("No updates", "pill err"), chip("Transmit off", "pill off"),
      chip("Transmit on", "pill on")),
    row("Badges", chip("PC", "badge"), chip("TX FG1", "talk-badge"), chip("Ended", "talk-badge ended"), chip("MAIN", "tg-badge")),
    row("Why / link", chip("Unit", "why unit"), chip("60 Control", "why dispatcher"), chip("Added", "why manual"), ev, ub),
    row("Talking", chip("TX E60 FG1", "talk-chip rig"), chip("60 Control", "talk-chip disp"), chip("E61 ended", "talk-chip ended")));
  }

  function cards() {
    const inner = el("div", "card");
    const h = el("div", "card-head");
    h.append(el("h2", "", "Assigned"), Help.button("A card's head: its overline, its \"?\", a count, tools at the end."),
      el("span", "dim small", "2 units"), el("span", "spacer"), btn("Recommend"));
    inner.append(h, el("p", "dim", "Card body. Cards are 12 px in, 4 px round, on --panel; the columns 12 px apart."));
    const vh = el("div", "view-head");
    vh.append(el("h2", "view-title", "Radio"), el("span", "dim small", "13 TX, 1 note"), el("span", "spacer"), btn("Live", "live-btn"));
    return card("sg-cards", "Cards & heads", "A view's head (Overview, Radio, a Settings section) has its title at 16 px; "
      + "a card's head an 11 px overline.", vh, inner);
  }

  function lists() {
    const ol = el("ol", "list");
    for (const [t, who, said] of [["14:48:43", "E60", "Engine 60 on location, nothing showing"], ["14:44:23", "60 Control", "Received"]]) {
      const li = el("li");
      const m = el("div", "meta");
      m.append(Icon.play(btn("", "play")), el("span", "t", t), el("b", "unit", who), el("span", "chip src", "FG1"));
      m.style.setProperty("--c", "var(--dim)");
      li.append(m, el("div", "txt", said));
      ol.append(li);
    }
    const q = el("ol", "list q");
    q.style.display = "flex";
    for (const [n, sel] of [["26-000002", true], ["26-000003", false]]) {
      const li = el("li", "qi item" + (sel ? " sel" : ""));
      li.tabIndex = 0;
      const head = el("div", "qh");
      const t = el("span", "qt", "Alarm / Commercial");
      t.style.setProperty("--c", "#E0362C");
      head.append(el("b", "qn", n), t, el("span", "spacer"), el("span", "qe", "02:41"));
      li.append(head, el("div", "qa", "1 Gateway Plz, Port Chester"));
      q.append(li);
    }
    const states = el("div");
    states.append(row("Empty", el("div", "empty", "None")), row("Loading", el("div", "loading", "Loading")),
      row("Error", el("div", "error", "Load failed: 503 Service Unavailable")));
    return card("sg-lists", "Lists & rows", "A .list splits rows with a line; an .item row opens something: hover, selected "
      + "(the red edge), keyboard focus. Every list says None, Loading or what failed the same way.", ol, q, states);
  }

  function nav() {
    const tabs = el("div", "tabs");
    ["Event types", "Dispositions", "Unit statuses"].forEach((t, i) => tabs.append(btn(t, "tab" + (i === 0 ? " on" : ""), { "aria-pressed": String(i === 0) })));
    const vt = el("div", "tabs vertical");
    vt.style.maxInlineSize = "14rem";
    ["Lists", "Run cards", "Units"].forEach((t, i) => { const a = el("a", "tab" + (i === 1 ? " on" : ""), t); a.href = "#styleguide"; vt.append(a); });
    const seg = el("span", "seg");
    seg.append(el("span", "dim sdr-label", "SDR 1"));
    ["Off", "Trunk", "Paging"].forEach((t, i) => seg.append(btn(t, i === 1 ? "active" : "")));
    return card("sg-nav", "Tabs & segmented", "Tabs: the header's views, Settings' sections and lists (the red underline or edge "
      + "is the one you're on). Segmented: one choice of a few.", row("Tabs", tabs), row("Vertical", vt), row("Segmented", seg));
  }

  function feedback() {
    const n = (cls, text) => el("div", cls, text);
    return card("sg-feedback", "Feedback", "Saved: a short toast. An error: in place (a field's, a form's) and it stays. "
      + "Anything that can't be undone asks first.",
    row("Toasts", btn("Saved", "", { "data-toast": "ok" }), btn("Failed", "", { "data-toast": "err" }), btn("Info", "", { "data-toast": "info" })),
    row("Confirm", btn("Delete…", "danger-quiet")),
    n("notice", "Default run cards: not PCFD policy"), n("notice err", "Load failed: the hub is unreachable"),
    n("notice ok", "No admin yet: anyone signed in can change settings."), n("notice info", "Station console, not the CAD of record."),
    row("Help", (() => { const s = el("span", "h2", "Section"); return s; })(), Help.button("The explanation, kept off the screen until asked for.")),
    row("Keys", el("kbd", "", "G"), el("span", "dim small", "then"), el("kbd", "", "D")));
  }

  function table() {
    const t = el("table", "evtable");
    const head = el("tr");
    for (const h of ["Number", "Type", "Location", "Status"]) head.append(el("th", "", h));
    const thead = el("thead");
    thead.append(head);
    const body = el("tbody");
    for (const [n, ty, loc] of [["26-000002", "Alarm", "1 Gateway Plz"], ["26-000001", "MVA", "100 King St"]]) {
      const tr = el("tr", "ev");
      tr.tabIndex = 0;
      const st = el("td");
      st.append(stBadge(el("span"), "Active", "#3FB86B"));
      tr.append(el("td", "num", n), el("td", "", ty), el("td", "", loc), st);
      body.append(tr);
    }
    t.append(thead, body);
    const wrap = el("div", "tscroll");
    wrap.append(t);
    return card("sg-table", "Table", "Records, radio IDs, the audit log: 13 px cells, an 11 px header that stays in sight.", wrap);
  }

  function keys() {
    const b = btn("Show the shortcut sheet", "primary");
    b.addEventListener("click", () => UI.sheet());
    return card("sg-keys", "Shortcuts", "Never while typing in a field. The sheet is ? anywhere.", b);
  }

  function draw() {
    const sig = String(!!cad.state);
    if (drawnFor === sig && box.childElementCount) return;
    drawnFor = sig;
    const navEl = el("nav", "s-nav");
    navEl.setAttribute("aria-label", "Style guide sections");
    const list = el("ol", "s-sections tabs vertical");
    for (const [id, t] of SECTIONS) {
      const li = el("li");
      const a = el("a", "tab", t);
      a.href = `#styleguide`;
      a.addEventListener("click", (e) => { e.preventDefault(); $(`#${id}`).scrollIntoView({ block: "start" }); });
      li.append(a);
      list.append(li);
    }
    navEl.append(list);
    const content = el("div", "s-content");
    const panel = el("div", "s-panel");
    const head = el("div", "s-head view-head");
    head.append(el("h2", "view-title", "Style guide"), el("span", "dim small", "style.css components on the tokens"), el("span", "spacer"));
    const back = el("a", "nav", "Settings");
    back.href = "#settings/system";
    head.append(back);
    const kids = [head];
    if (!canDo("admin")) kids.push(el("div", "notice", "Admins only"));
    else kids.push(tokens(), buttons(), fields(), tags(), cards(), lists(), nav(), feedback(), table(), keys());
    panel.append(...kids);
    content.append(panel);
    box.replaceChildren(navEl, content);
    const said = { ok: "Run card saved", err: "Save failed: 409 Conflict", info: "Forwarding to the hub: on." };
    for (const b of box.querySelectorAll("[data-toast]")) b.addEventListener("click", () => UI.toast(said[b.dataset.toast], b.dataset.toast));
    const del = box.querySelector("#sg-feedback .danger-quiet");
    if (del) del.addEventListener("click",
      () => UI.confirm("Delete run card Alarm / Commercial?", { ok: "Delete" }).then((y) => UI.toast(y ? "Deleted (not really)" : "Kept", y ? "ok" : "info")));
  }

  function route() {
    const on = location.hash === "#styleguide";
    show(box, on);
    if (!on) return;
    show(document.querySelector(".cad-main"), false);
    show($("#settings"), false);
    draw();
    if (window.renderTabs) renderTabs();
  }
  window.addEventListener("hashchange", route);
  cad.hooks.push(() => { if (!box.hidden) draw(); });
  route();
})();
