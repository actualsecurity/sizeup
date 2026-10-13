#!/usr/bin/env python3
"""Measure the rendered UI on every view and check it is one system (the browser half of tests/test_design.py).

Needs a running sizeup with some data (a dev instance: incidents, a page, transmissions; never the live
station's) and Playwright with Chromium (`pip install playwright && playwright install chromium`). Not part of
pytest. It loads every view at 1920, 1366 and 390 px (the phone with touch, so pointer: coarse applies):

  Dispatch (an incident open), New incident, Overview, Radio, each Settings section, the style guide, the
  Monitor board, the dialogs (Records, Talk alerts, "?" help, the large map, the shortcut sheet), and a
  transmission's "⋯" menu open (ui.js UI.menu)

and checks, per width:
  - the header is the same height on every view (the board's included)
  - all primary buttons of a size share height and font size; secondary and small buttons too
  - every input and select has one height; every tag one height and font size
  - list rows of a kind (queue, units, transmissions, settings rows, tables ...) share font size and padding
  - hit targets are at least 24 px (44 px on the phone), map marks excepted
  - nothing scrolls sideways at 390 px; no page errors
The board's main area is one deliberate step larger (--zoom), so it is compared with itself.

Run:  python tools/ui_consistency.py --base http://127.0.0.1:8111 [--shots DIR] [--json OUT] [--views a,b]
Exit status 1 when a check fails; each failure is printed with the values found.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

WIDTHS = (1920, 1366, 390)
SECTIONS = ("lists", "runcards", "units", "dispatch", "users", "system", "radios")
VIEWS = ([("dispatch", "/cad", "incident"), ("new", "/cad", "new"), ("overview", "/cad", None), ("radio", "/cad#radio", None)]
         + [(f"settings-{s}", f"/cad#settings/{s}", None) for s in SECTIONS]
         + [("styleguide", "/cad#styleguide", None), ("monitor", "/monitor", None), ("records", "/cad#settings/system", "records"),
            ("alerts", "/cad", "alerts"), ("help", "/cad#radio", "help"), ("bigmap", "/cad", "bigmap"), ("shortcuts", "/cad", "shortcuts"),
            ("menu", "/cad#radio", "menu")])

MEASURE = r"""() => {
  const vis = (e) => {
    if (!e.isConnected || e.closest("[hidden]")) return false;
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return r.width >= 1 && r.height >= 1 && cs.visibility !== "hidden" && cs.display !== "none";
  };
  const px = (v) => Math.round(parseFloat(v) * 10) / 10;
  const sig = (e, props) => {
    const cs = getComputedStyle(e), r = e.getBoundingClientRect(), o = {};
    for (const p of props) {
      if (p === "h") o.h = px(r.height);
      else if (p === "w") o.w = px(r.width);
      else if (p === "pad") o.pad = [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].map(px).join("/");
      else if (p === "fs") o.fs = px(cs.fontSize);
      else if (p === "lh") o.lh = cs.lineHeight === "normal" ? "normal" : px(cs.lineHeight);
      else if (p === "fw") o.fw = cs.fontWeight;
      else if (p === "rad") o.rad = px(cs.borderTopLeftRadius);
    }
    return JSON.stringify(o);
  };
  const board = (e) => !!e.closest("html.board .cad-main");
  const btnRole = (b) => {
    if ([...b.classList].some((c) => c.startsWith("maplibregl"))) return null;
    if (b.matches(".help")) return "help";
    if (b.matches(".tab, [role=tab]")) return b.closest("header") ? "tab-header" : "tab";
    if (b.matches(".sw")) return "switch";
    if (b.matches(".seg button")) return "segmented";
    if (b.matches(".cm-rig, .cm-pin, .unit-tag, .ov-page, .og-head, .sec-toggle, .cm-unset-head, .feed-ev, .chip, .ml-thumb, .ml-cams-head, .ml-cams-down, .ml-down-item")) return null;   // .ml-*: the Cameras panel (a picture, a fold, a one-line row)
    if (b.closest("header")) return "button-header";
    const size = b.matches(".sm, .mini, .play") ? "-sm" : b.matches(".lg") ? "-lg" : "";
    const icon = b.matches(".icon, .play") ? "-icon" : "";
    const kind = b.matches(".primary, .danger") ? "primary" : "button";
    return `${kind}${size}${icon}`;
  };
  const roles = {};
  const add = (role, e, props) => {
    if (!role || !vis(e)) return;
    if (board(e)) role = `board:${role}`;
    const s = sig(e, props), r = (roles[role] = roles[role] || {});
    const k = r[s] || (r[s] = { n: 0, ex: [] });
    k.n++;
    if (k.ex.length < 3) k.ex.push((e.id ? "#" + e.id : e.tagName.toLowerCase()) + "." + [...e.classList].join("."));
  };
  const all = (sel) => [...document.querySelectorAll(sel)];
  for (const b of all("button, a.nav, a.btn, a.tab")) add(btnRole(b), b, ["h", "fs", "pad", "rad"]);
  for (const i of all("input:not([type=checkbox]):not([type=radio]):not([type=color]):not([type=range]):not([type=file]), select"))
    add(i.matches(".sm") || i.closest(".ubtns .ask, .rv-pick") ? "input-sm" : "input", i, ["h", "fs"]);
  for (const t of all(".chip, .pill, .badge, .st-badge, .why, .talk-badge, .feed-ev, .tg-badge, .pc-badge, .town"))
    add(t.tagName === "BUTTON" ? "tag-button" : "tag", t, ["h", "fs", "fw"]);
  for (const h of all("h2:not(.view-title):not(.dialog-title), .h2")) add(h.closest("header") ? null : "overline", h, ["fs", "fw"]);
  for (const h of all(".view-title")) add("view-title", h, ["fs", "fw"]);
  for (const h of all(".dialog-title")) add("dialog-title", h, ["fs", "fw"]);
  for (const e of all("label.field")) add("field-label", e, ["fs"]);
  for (const e of all(".card, .s-card, .rv-sec")) add("card", e, ["pad", "rad"]);
  for (const e of all("svg.ico")) add("icon", e, ["h"]);
  const rows = {
    "row-queue": ".q > li.qi", "row-unit": ".ulist > li.urow", "row-transmission": ".radio-log > li.rt", "row-settings": ".s-row",
    "row-talkgroup": ".tg-row", "row-ovcard": ".ov-card", "row-nonemerg": ".ov-nelist > li.ov-ne", "row-runcard": ".rc-list > li.rc",
    "row-feed": ".feed-list > li.feed-row", "row-table": ".evtable tbody tr:not(.ev) td:not(.small):not(.empty)", "row-table-open": ".evtable tbody tr.ev td", "row-event": "#d-events > li.ev",
  };
  for (const [role, sel] of Object.entries(rows)) {      // (a table laid out as cards on a phone is not a table row)
    for (const e of all(sel)) if (e.tagName !== "TD" || getComputedStyle(e).display === "table-cell") add(role, e, ["fs", "pad"]);
  }
  const hdr = document.querySelector("body > header");
  const coarse = matchMedia("(pointer: coarse)").matches, min = coarse ? 44 : 24;
  const small = all("button, a[href], input, select, textarea, summary, [tabindex='0']").filter(vis).filter((e) => {
    if (e.closest(".maplibregl-marker, .maplibregl-ctrl-attrib, .cm-unset-list") || [...e.classList].some((c) => c.startsWith("maplibregl"))) return false;
    if (getComputedStyle(e).display === "inline" || e.closest(".help-pop, .txt, p, .notice, .dd, dd")) return false;   // links in running text
    const r = e.getBoundingClientRect();
    if (r.width >= min - 0.5 && r.height >= min - 0.5) return false;
    if (e.matches("input[type=checkbox], input[type=radio]")) {           // the label around it is the target
      const l = e.closest("label");
      if (l) { const lr = l.getBoundingClientRect(); return lr.height < min - 0.5; }
    }
    return true;
  });
  const scrollers = all("*").filter((e) => { const cs = getComputedStyle(e); return /(auto|scroll)/.test(cs.overflowY) && e.scrollHeight > e.clientHeight + 1 && vis(e); });
  return {
    roles, coarse,
    header: hdr ? px(hdr.getBoundingClientRect().height) : null,
    body_fs: px(getComputedStyle(document.body).fontSize),
    hscroll: document.documentElement.scrollWidth > window.innerWidth + 1,
    small_targets: small.map((e) => { const r = e.getBoundingClientRect();
      return `${e.id ? "#" + e.id : e.tagName.toLowerCase()}.${[...e.classList].join(".")} ${px(r.width)}x${px(r.height)}`; }),
    nested_scrollers: scrollers.filter((e) => scrollers.some((o) => o !== e && o.contains(e))).map((e) => e.id || e.className),
  };
}"""


def setup(pg, how):
    if how == "incident":
        pg.locator("#q-active li.qi").first.click()
        pg.wait_for_timeout(1500)
    elif how == "new":
        pg.click("#new-incident")
        pg.wait_for_timeout(600)
    elif how == "records":
        pg.locator("#s-system button", has_text="Open records").first.click()
        pg.wait_for_timeout(1500)
    elif how == "alerts":
        pg.locator(".alerts-btn").first.click()
        pg.wait_for_timeout(1200)
    elif how == "help":
        pg.locator("#radio-view button.help").first.click()
        pg.wait_for_timeout(400)
    elif how == "bigmap":
        pg.locator("#q-active li.qi").first.click()
        pg.wait_for_timeout(1500)
        pg.locator("#d-body button", has_text="Expand").first.click()
        pg.wait_for_timeout(1500)
    elif how == "menu":
        pg.locator("#rv-main .menu-btn").first.click()
        pg.wait_for_timeout(400)
    elif how == "shortcuts":
        pg.keyboard.press("?")
        pg.wait_for_timeout(400)


def shot(pg, path, full):
    if full:
        h = pg.evaluate("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")
        if h > 6000:                       # Settings > Units on a phone: thousands of pixels
            pg.screenshot(path=path, clip={"x": 0, "y": 0, "width": pg.viewport_size["width"], "height": 6000}, full_page=True)
            return
    pg.screenshot(path=path, full_page=full)


def measure(base, views, shots=None, prefix="after"):
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        for name, path, how in VIEWS:
            if views and name not in views:
                continue
            for w in WIDTHS:
                phone = w < 600
                ctx = b.new_context(viewport={"width": w, "height": 1080 if w == 1920 else 768 if w == 1366 else 844},
                                    is_mobile=phone, has_touch=phone)
                ctx.add_init_script("try { localStorage.setItem('sizeup.cad.operator', 'Corey'); } catch (e) {}")
                pg = ctx.new_page()
                errs = []
                pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
                pg.on("console", lambda m, errs=errs: errs.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
                pg.on("dialog", lambda d: d.dismiss())
                pg.goto(base + path)
                pg.wait_for_timeout(2500)
                try:
                    setup(pg, how)
                except Exception as e:      # noqa: BLE001
                    errs.append(f"setup: {e}")
                m = pg.evaluate(MEASURE)
                m["errors"] = errs
                out[f"{name}@{w}"] = m
                if shots:
                    shot(pg, f"{shots}/{prefix}-{name}-{w}.png", phone)
                print(f"measured {name} @{w}", file=sys.stderr, flush=True)
                ctx.close()
        b.close()
    return out


SAME = {   # role: the fields that must be one value across every view at a width
    "primary": ("h", "fs"), "primary-lg": ("h", "fs"), "primary-sm": ("h", "fs"), "button": ("h", "fs"), "button-sm": ("h", "fs"),
    "button-icon": ("h",), "button-sm-icon": ("h",), "button-header": ("h", "fs"), "tab": ("h", "fs"), "tab-header": ("h", "fs"),
    "help": ("h",), "switch": ("h", "fs"), "segmented": ("h",), "input": ("h", "fs"), "input-header": ("h",), "input-sm": ("h",),
    "tag": ("h", "fs"), "tag-button": ("fs",), "overline": ("fs", "fw"), "view-title": ("fs", "fw"), "dialog-title": ("fs",),
    "field-label": ("fs",), "card": ("pad", "rad"),
    "row-queue": ("fs", "pad"), "row-unit": ("fs", "pad"), "row-transmission": ("fs",), "row-settings": ("fs", "pad"),
    "row-talkgroup": ("fs", "pad"), "row-ovcard": ("fs", "pad"), "row-nonemerg": ("fs", "pad"), "row-runcard": ("fs", "pad"),
    "row-feed": ("fs", "pad"), "row-table": ("fs", "pad"), "row-table-open": ("fs", "pad"), "row-event": ("fs", "pad"),
}


def check(data):
    fails, summary = [], defaultdict(dict)
    for w in WIDTHS:
        at = {k.split("@")[0]: v for k, v in data.items() if k.endswith(f"@{w}")}
        if not at:
            continue
        hs = {v["header"] for v in at.values()}
        summary[w]["header"] = sorted(hs)
        if len(hs) > 1:
            fails.append(f"{w}px header height differs: " + ", ".join(f"{k} {v['header']}" for k, v in at.items()))
        for scope in ("", "board:"):
            for role, fields in SAME.items():
                key = scope + role
                for f in fields:
                    vals = defaultdict(set)
                    for vn, m in at.items():
                        for s, info in m["roles"].get(key, {}).items():
                            vals[json.loads(s)[f]].add(vn)
                    if vals:
                        summary[w][f"{key}.{f}"] = sorted(vals)
                    if len(vals) > 1:
                        fails.append(f"{w}px {key} {f}: " + "; ".join(f"{v} ({', '.join(sorted(vs))})" for v, vs in vals.items()))
        for vn, m in at.items():
            if m["errors"]:
                fails.append(f"{w}px {vn}: page errors {m['errors']}")
            if w < 600 and m["hscroll"]:
                fails.append(f"{w}px {vn}: scrolls sideways")
            if m["small_targets"]:
                fails.append(f"{w}px {vn}: {len(m['small_targets'])} targets under {44 if m['coarse'] else 24}px: {m['small_targets'][:6]}")
    return fails, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8111")
    ap.add_argument("--shots", help="directory for <prefix>-<view>-<width>.png")
    ap.add_argument("--prefix", default="after")
    ap.add_argument("--json", help="write the measurements here")
    ap.add_argument("--views", help="comma-separated subset")
    a = ap.parse_args(argv)
    data = measure(a.base.rstrip("/"), set(a.views.split(",")) if a.views else None, a.shots, a.prefix)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(data, f, indent=1)
    fails, summary = check(data)
    for w, s in summary.items():
        print(f"== {w}px")
        for k, v in s.items():
            print(f"  {k}: {v}")
    print(f"\n{len(data)} view×width measured, {len(fails)} failed checks")
    for f in fails:
        print("FAIL", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
