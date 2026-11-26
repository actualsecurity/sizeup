// Notifications (sizeup/cad/notif.py): a Notification's sub-type and the hydrants out of service it names.
//
//   The type     "Notification / Fire · Hydrants OOS" on the queue card and the event (inc.notif.kind_name), as an
//                Airport event shows its alert; set by the admins' rules or by hand here (dispatchers; "Rules" puts
//                the rules' back).
//   The card     #d-notif under the event's facts: each number the rules read, matched to the hydrant list:
//                Mark OOS (one near the call, in service), Link (already out of service), Confirm (found elsewhere:
//                where and how far, Show on map), + Add (not in the hydrant list: place it on the call's map). Apply
//                does every one that needs no choice. Linked ones: Back in service. Admins only (hydrant edits);
//                everyone sees it.
//   Held open    "2 hydrants OOS" on the queue card (and the Non-emergency list); the county's in service and an Until
//                never close it; Close asks first. All back: "All back in service" with Close.
// Uses cad.js's helpers (el, $, show, setText, api, operator, canDo, UI, cad, BOARD): loaded after it and hydrants.js.
"use strict";

window.Notif = (() => {
  const TYPE = "NOTIF";                                  // sizeup/cad/lists.py NOTIFICATION
  const DOWN = ["oos", "dry"];
  const NEXT = { mark: "Mark OOS", link: "Link", back: "Back in service", add: "+ Add" };
  const TIPS = {
    mark: "Mark this hydrant out of service, the notification as the reason",
    link: "Already out of service: keep this notification open until it is back",
    back: "Mark it in service again",
    confirm: "This hydrant: mark it (or link it)",
    add: "Not in the hydrant list: place it on the call's map",
    dismiss: "Set aside (the hydrant itself is unchanged)",
  };
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const dist = (m) => (m == null ? "" : m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`);
  const may = (need) => typeof canDo === "function" && canDo(need) && !(typeof BOARD !== "undefined" && BOARD);

  // the type with the sub-type after it: "Notification / Fire · Hydrants OOS"
  function label(inc, base) {
    const k = inc && inc.notif && inc.notif.kind_name;
    return k ? (base ? `${base} · ${k}` : k) : base;
  }
  // the queue card's line: "2 hydrants OOS" while held, "All back in service" once every one is
  function line(inc) {
    const n = inc && inc.notif;
    if (!n || !["queued", "pending", "active"].includes(inc.status)) return null;
    if (n.oos) {
      const d = el("div", "qs warn nf-line", plural(n.oos, "hydrant") + " OOS");
      d.title = "Held open until back in service";
      return d;
    }
    const todo = (n.rows || []).filter((r) => r.state === "proposed" && r.action === "oos").length;
    if (n.all_back) return Object.assign(el("div", "qs dim nf-line", "All hydrants back in service"), { title: "Close it" });
    if (todo) return Object.assign(el("div", "qs dim nf-line", `${plural(todo, "hydrant")} OOS proposed`), { title: "Open it to apply" });
    return null;
  }
  const sig = (inc) => JSON.stringify(inc.notif ? [inc.notif.kind_name, inc.notif.oos, inc.notif.all_back,
    (inc.notif.rows || []).map((r) => [r.state, r.action])] : null);

  async function post(inc, path, body, ok) {
    try {
      const r = await api("POST", `/api/cad/incident/${inc.id}/${path}`, { operator: operator(), ...body });
      if (ok) UI.toast(ok);
      if (window.Hydrants && path === "hydrants" && window.GisLayers) GisLayers.load(true);   // the marks again
      return r;
    } catch (e) { UI.toast(e.message, "err"); return null; }
  }
  const hyd = (inc, body, ok) => post(inc, "hydrants", body, ok);

  function btn(text, tip, fn, cls = "sm") {
    const b = el("button", cls, text);
    b.type = "button";
    b.title = tip || "";
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    return b;
  }
  function mapBtn(h) { return window.Hydrants && h && h.key ? Hydrants.mapBtn(h, true) : null; }
  function where(h) {
    const loc = typeof qualText === "function" ? qualText(h.location_qualifier, h.location) : h.location || "";
    return [dist(h.dist_m), h.town, loc].filter(Boolean).join(" · ");
  }
  function stateText(r) {
    const h = r.h;
    if (r.state === "linked") return h && !DOWN.includes(h.status) ? "in service" : "OOS · linked";
    if (r.state === "back") return "back in service";
    if (r.state === "done") return "back in service";
    if (r.state === "dismissed") return "set aside";
    if (r.match === "none") return "not in the hydrant list";
    if (r.match === "gone") return "removed from the list";
    if (r.match === "elsewhere" || r.match === "ambiguous") return r.match === "elsewhere" ? "not near: confirm" : "two near: pick";
    if (!h) return "";
    if (r.action === "in") return DOWN.includes(h.status) ? "OOS now" : "in service";
    return DOWN.includes(h.status) ? "already OOS" : "in service";
  }

  function addHere(inc, r) {
    const gm = window.GisLayers && [...GisLayers.maps].find((x) => x.map.getContainer().closest("#d-map"));
    if (!gm || !window.Hydrants || !Hydrants.placeNew) { UI.toast("Open the call's map to place it", "err"); return; }
    Hydrants.placeNew(gm, r.number);
  }

  function rowEl(inc, r, admin) {
    const li = el("li", `pp-row nf-row nf-${r.state}` + (r.state === "linked" ? " hy-down" : ""));
    const h = r.h;
    const what = el("span", "nf-what");
    if (h && window.Hydrants) what.append(Hydrants.mark(h.cls, h.status, h.cls_assumed));
    what.append(el("b", "", r.number), el("span", "nf-st" + (r.state === "proposed" && r.next === "" ? " dim" : ""), stateText(r)));
    if (h && r.state === "proposed" && r.match !== "elsewhere") what.append(el("span", "dim", `· ${where(h)}`));
    what.title = [r.action === "in" ? "Back in service" : "Out of service", r.rule ? `rule: ${r.rule}` : "",
                  r.by ? `by ${r.by}` : "", h ? `hydrant ${h.number || h.key}` : ""].filter(Boolean).join(" · ");
    li.append(what, el("span", "spacer"));
    const m = mapBtn(h);
    if (m && r.state !== "dismissed") li.append(m);
    if (admin && ["mark", "link", "back"].includes(r.next)) {
      const t = r.state === "linked" ? "Back in service" : r.action === "in" ? "Mark in service" : NEXT[r.next];
      li.append(btn(t, TIPS[r.next], () => (r.state === "linked"
        ? hyd(inc, { action: "back", ids: [r.id] }, `Hydrant ${r.number}: back in service`)
        : hyd(inc, { action: "apply", items: [{ id: r.id }] }, `Hydrant ${r.number}: ${t}`)), "sm primary"));
    }
    if (admin && r.next === "add") li.append(btn(NEXT.add, TIPS.add, () => addHere(inc, r)));
    if (admin && (r.state === "proposed" || r.state === "linked")) {
      const x = btn("✕", TIPS.dismiss, () => hyd(inc, { action: "dismiss", id: r.id }), "icon quiet sm");
      x.setAttribute("aria-label", `Set aside ${r.number}`);
      li.append(x);
    }
    const out = [li];
    if (r.next === "confirm") {                          // found elsewhere: where each is, a person picks
      for (const c of r.cands) {
        const ci = el("li", "pp-row nf-cand");
        const t = el("span", "nf-what");
        if (window.Hydrants) t.append(Hydrants.mark(c.cls, c.status, c.cls_assumed));
        t.append(el("b", "", c.number), el("span", "", `· ${where(c)}`));
        if (DOWN.includes(c.status)) t.append(el("span", "err nf-st", "· OOS"));
        ci.append(t);
        const mb = mapBtn(c);
        if (mb) ci.append(mb);
        if (admin) ci.append(btn("Confirm", TIPS.confirm, () => hyd(inc, { action: "apply", items: [{ id: r.id, key: c.key }] },
          `Hydrant ${r.number}: ${DOWN.includes(c.status) ? "linked" : "marked OOS"}`)));
        out.push(ci);
      }
      if (r.more) out.push(el("li", "pp-row dim small nf-cand", `+${r.more} more with this number`));
    }
    return out;
  }

  function kindPicker(inc) {
    const n = inc.notif || {};
    const kinds = ((L().notification_kinds) || []).filter((k) => !k.retired || k.code === n.kind);
    const sel = el("select", "sm nf-kind");
    sel.setAttribute("aria-label", "Sub-type");
    sel.append(new Option(n.by === "hand" ? "Rules" : "—", ""));
    for (const k of kinds) sel.append(new Option(k.name, k.code));
    sel.value = n.kind || "";
    sel.title = n.by === "hand" ? "Set by hand (Rules: the rules' again)" : n.rule ? `By rule: ${n.rule}` : "Sub-type";
    sel.addEventListener("change", () => post(inc, "notif", { kind: sel.value }));
    return sel;
  }

  // the event's card (#d-notif): its sub-type and its hydrants; hidden on anything but a Notification
  function card(inc, open) {
    const box = $("#d-notif");
    if (!box) return;
    const n = inc && inc.notif;
    const on = !!inc && (inc.type === TYPE || !!n);
    show(box, on);
    if (!on) return;
    const admin = open && may("admin"), disp = open && may("dispatch");
    const s = JSON.stringify([inc.id, n, open, admin, disp, (L().notification_kinds || []).length]);
    if (box._sig === s) return;
    box._sig = s;
    const head = el("div", "pp-head");
    head.append(el("span", "pp-title", "Sub-type"));
    if (disp) head.append(kindPicker(inc));
    else head.append(el("span", n && n.kind_name ? "" : "dim", (n && n.kind_name) || "—"));
    head.append(el("span", "spacer"));
    if (n && n.oos) {
      const c = el("span", "chip warn", plural(n.oos, "hydrant") + " OOS");
      c.title = "Held open until back in service";
      head.append(c);
    }
    const parts = [head];
    const rs = (n && n.rows) || [];
    if (rs.length) {
      const list = el("ol", "pp-list nf-list");
      for (const r of rs) list.append(...rowEl(inc, r, admin));
      parts.push(list);
      const foot = el("div", "row nf-foot");
      const ready = rs.filter((r) => r.state === "proposed" && ["mark", "link", "back"].includes(r.next));
      if (admin && ready.length > 1) {
        foot.append(btn(`Apply (${ready.length})`, "Every one that needs no choice", () => hyd(inc, { action: "apply" },
          `${plural(ready.length, "hydrant")} applied`), "sm primary"));
      }
      if (admin && n.oos > 1) {
        foot.append(btn("All back in service", "Every linked hydrant in service again", async () => {
          if (await UI.confirm(`Mark ${plural(n.oos, "hydrant")} back in service?`, { ok: "Back in service", danger: false })) {
            hyd(inc, { action: "back" }, "Back in service");
          }
        }));
      }
      if (foot.children.length) parts.push(foot);
    }
    if (n && n.all_back) {
      const done = el("div", "row nf-foot");
      done.append(el("span", "ok", "All hydrants back in service"), el("span", "spacer"));
      if (disp && !$("#d-close").hidden) done.append(btn("Close", "Close this notification", () => $("#d-close").click(), "sm primary"));
      parts.push(done);
    }
    box.replaceChildren(...parts);
  }

  // Close on a notification still holding hydrants: asked first (the hub refuses it without confirm)
  async function confirmClose(inc) {
    const n = inc && inc.notif && inc.notif.oos;
    if (!n) return {};
    return (await UI.confirm(`${plural(n, "hydrant")} still OOS on this notification. Close anyway?`, { ok: "Close" }))
      ? { confirm: true } : null;
  }

  return { label, line, sig, card, confirmClose };
})();
