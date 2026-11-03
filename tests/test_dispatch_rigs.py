"""Dispatch: the radio feed gone, the right column the rigs, big (GitHub #205). User: "lets also get rid of the radio
on the top left on the dispatch page, the overview page we can leave it in, on the dispatch page we just want to
show the rigs in big on the right and their statuses".

    the feed     the pinned 60 Control feed (cad-feed.js) is hidden on Dispatch and nothing in it is drawn (rows,
                 head, paging); the Overview and Radio keep it as it was; its toasts (a page naming our units, the
                 talk alerts) still go
    the rigs     on Dispatch (body[data-view="dispatch"]) the units column is tiles: the ID large, the status a band
                 in the data file's own colors with its time, edged in the status color; the groups, RB tags,
                 click for the status buttons as before; as many a row as fit (the resizable column, stacked, a phone)
    New radio IDs  the question stays in the units column: with no feed on Dispatch, the top of it, above the rigs
    no churn     nothing new by the second (#146): the tiles redraw on the same signature, the timers on the tick

#209, from a screenshot of the column ("can we slightly make these smaller? or scaled better?"): a notch smaller (the ID
22 px, the band 13 px bold, tighter padding); the status band sized to its whole label, never cut ("Availabl", "Out of
se"), the time what wraps when the tile is narrow; the time compact (m:ss, then h:mm, then "4d 6h"), exact on its
title; the tag, the band and the time on the ID's line, the incident link or a note under it only when there is one.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
D = 'body[data-view="dispatch"]'


def text(name):
    return (STATIC / name).read_text()


def rules(css, prefix):
    """{selector: declarations} for the one-line rules under prefix."""
    return {m.group(1).strip(): m.group(2) for m in re.finditer(r"^\s*(" + re.escape(prefix) + r"[^{]*)\{([^}]*)\}", css, re.M)}


def side():
    html = text("cad.html")
    s = html[html.index('<aside class="units">'):]
    return s[:s.index("</aside>")]


# -- which view: body[data-view] ---------------------------------------------------------------------------------

VIEW_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (re) => { const m = src.match(re); if (!m) throw new Error("not found: " + re); return m[0]; };
const shown = { "#settings": false, "#styleguide": false };
global.$ = (id) => ({ hidden: !shown[id] });
global.cad = { radio: false, mode: "none" };
eval(grab(/^function viewTab\(\) \{[\s\S]*?^\}/m) + "\nglobal.viewTab = viewTab;");
const out = {};
for (const [k, mode, radio, settings] of [["none", "none", false, false], ["view", "view", false, false],
    ["idle", "idle", false, false], ["new", "new", false, false], ["radio", "view", true, false], ["settings", "none", false, true]]) {
  cad.mode = mode; cad.radio = radio; shown["#settings"] = settings;
  out[k] = viewTab();
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_view_is_dispatch_for_an_incident_the_form_or_none_open(tmp_path):
    h = tmp_path / "view.js"
    h.write_text(VIEW_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == {"none": "overview", "view": "dispatch", "idle": "dispatch", "new": "dispatch", "radio": "radio",
                   "settings": "settings"}
    cad = text("cad.js")
    # the tabs and the page's own mark come from the one answer; the wall board has no tabs (and no mark)
    assert "const tab = viewTab();" in cad
    assert "if (!BOARD && document.body.dataset.view !== tab) document.body.dataset.view = tab;" in cad


# -- the feed: not on Dispatch, as it was on the Overview --------------------------------------------------------

FEED_HARNESS = r"""
const fs = require("fs");
class N {
  constructor(tag) { this.tag = tag; this.children = []; this.hidden = false; this.className = ""; this.textContent = "";
    this.title = ""; this.dataset = {}; this.attrs = {};
    this.classList = { toggle: () => {}, add: () => {}, remove: () => {} }; }
  append(...c) { this.children.push(...c); } prepend(...c) { this.children.unshift(...c); }
  replaceChildren(...c) { this.children = c; } setAttribute(k, v) { this.attrs[k] = v; } addEventListener() {}
  querySelector() { return new N("span"); } querySelectorAll() { return []; } remove() {}
}
global.document = { createElement: (t) => new N(t) };
global.window = global;
global.matchMedia = () => ({ matches: false });
global.localStorage = { getItem: () => null, setItem() {} };
const calls = { rows: 0, paging: 0, toasts: 0 };
global.Radio = { fillPaging() { calls.paging++; }, toast() { calls.toasts++; } };
global.UI = { toast() { calls.toasts++; } };
global.oursNamedText = () => "E60 named";
global.emSig = () => "";
eval(fs.readFileSync(process.argv[2], "utf8"));
const now = Date.now() / 1000;
let state = { feed: { title: "60 Control · Fire/EMS 15", channel: "Fire/EMS 15", talkgroups: [307], rows: [
  { id: "a", start: now - 5, text: "Engine 60 respond", pc: "E60" }] }, talking: [], paging: null };
let off = true;
const box = new N("div");
CadFeed.mount(box, { state: () => state, radioRow: () => { calls.rows++; const li = new N("li"); return li; },
                     time: () => "19:00:00", rev: () => 0, off: () => off });
const out = {};
CadFeed.render();
out.dispatch = { hidden: box.hidden, rows: calls.rows, paging: calls.paging, title: box.children[0].children[1].textContent };
// a page not ours naming our units comes in while on Dispatch: still a toast (an alert, not the panel)
state = { ...state, feed: { ...state.feed, rows: [{ id: "b", start: now - 1, text: "Engine 60", page: "not ours",
                                                    names_ours: ["E60"] }, ...state.feed.rows] } };
CadFeed.render();
out.toastOnDispatch = calls.toasts;
out.rowsOnDispatch = calls.rows;
off = false;                                  // the Overview
CadFeed.render();
out.overview = { hidden: box.hidden, rows: calls.rows, paging: calls.paging, title: box.children[0].children[1].textContent };
off = true;                                   // back to Dispatch
CadFeed.render();
out.again = { hidden: box.hidden, rows: calls.rows };
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dispatch_hides_the_feed_and_draws_nothing_of_it_the_overview_keeps_it(tmp_path):
    h = tmp_path / "feed.js"
    h.write_text(FEED_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad-feed.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["dispatch"] == {"hidden": True, "rows": 0, "paging": 0, "title": ""}         # nothing drawn
    assert out["toastOnDispatch"] == 1 and out["rowsOnDispatch"] == 0                       # the safety net still rings
    assert out["overview"]["hidden"] is False and out["overview"]["rows"] == 2 and out["overview"]["paging"] == 1
    assert out["overview"]["title"] == "60 Control · Fire/EMS 15"
    assert out["again"] == {"hidden": True, "rows": 2}


def test_the_page_passes_dispatch_as_off_and_the_feed_has_no_timer_of_its_own():
    cad = text("cad.js")
    assert re.search(r'CadFeed\.mount\(\$\("#feed"\), \{[^}]*off: \(\) => !BOARD && viewTab\(\) === "dispatch" \}\);', cad)
    feed = text("cad-feed.js")
    assert "st.box.hidden = !f || off;" in feed
    assert "if (off) { alertNew(f.rows || []); return; }" in feed
    assert not re.search(r"setInterval|setTimeout|fetch\(|api\(", feed)           # it never polls: the state push only
    # still on the page, in the units column, for the Overview and Radio; the board hides it as before
    assert '<div id="feed" hidden></div>' in side()
    css = text("cad.css")
    assert "html.board #feed { display: none !important; }" in css
    assert not re.search(r'data-view="(overview|radio)"', css)                       # the Overview is untouched


# -- the rigs, big, on Dispatch ----------------------------------------------------------------------------------


def test_the_rigs_are_big_tiles_in_their_status_colors_on_dispatch_only():
    css = text("cad.css")
    r = rules(css, D)
    grid = r[f"{D} .units .ulist"]
    assert "display: grid" in grid and "repeat(auto-fill, minmax(min(100%, 15rem), 1fr))" in grid   # as many as fit
    tile = re.search(re.escape(D) + r" \.units \.ulist > li\.urow \{([^}]*)\}", css).group(1)
    assert "var(--st)" in tile and "border-inline-start: calc(2 * var(--bw-mark)) solid var(--st)" in tile
    assert r[f"{D} .units .un"].strip().startswith("font-size: calc(var(--fs-xl) * 1.1)")         # the ID: 22 px (#209)
    band = re.search(re.escape(D) + r" \.units \.ust > \.chip\.st \{([^}]*)\}", css).group(1)
    assert "background: var(--c)" in band and "color: var(--fg, var(--text))" in band and "font-size: var(--fs-sm)" in band
    ut = re.search(re.escape(D) + r" \.units \.ut \{([^}]*)\}", css).group(1)
    assert "font-size: var(--fs-sm)" in ut and "var(--mono)" in ut                               # the time, readable
    assert "grid-column: 1 / -1" in r[f"{D} .units .ulist > li.urow.open"]                     # the status buttons: full width
    assert "display: none" in r[f"{D} .units .uh > :is(.spacer, .utitle)"]
    # every rule for it is Dispatch's own: the Overview's compact board is as it was
    assert re.search(r"^\s*\.ulist > li\.urow \{ padding: var\(--s-1\) var\(--s-2\);", css, re.M)
    assert re.search(r"^\s*\.ust \{ display: inline-flex;", css, re.M)


def test_the_tile_takes_its_color_from_the_status_and_keeps_the_board_as_it_was():
    cad = text("cad.js")
    body = cad[cad.index("function renderUnits() {"):cad.index("function renderUnitLog() {")]
    assert 'li.style.setProperty("--st", unitStatus(u.status).bg || "#9AA3AD");' in body
    assert ('ust.append(statusChip(u.status, agencySet(u) ? statusLabel(u, u.status) : undefined), '   # compact (#209),
            'since(el("span", "ut"), u.status_at, true));') in body                    # an agency's own label (#219)
    assert 'head.append(el("span", "spacer"), ust);' in body
    assert 'el("span", "chip joint", j.tag)' in body                     # the RB tag
    assert 'statusButtons(u, "board")' in body and "cad.openUnit = cad.openUnit === id ? null : id;" in body
    chip = cad[cad.index("function statusChip("):cad.index("function statusColors(")]
    assert 'if (st.fg) c.style.setProperty("--fg", st.fg);' in chip       # the data file's text color on its fill
    # #146: the redraw signature is what it was (no clock in it); the time ticks on the one tick ([data-since])
    sig = re.search(r"sig: \(u\) => JSON\.stringify\(\[([^\]]*)\]", body).group(1)
    assert "serverNow" not in sig and "Date.now" not in sig and "u.status_at" in sig
    assert "since(" in body


# -- #209: a notch smaller, the whole status, one line, compact times ---------------------------------------------


def test_the_status_band_is_never_cut_it_sizes_to_its_label():
    css = text("cad.css")
    band = re.search(re.escape(D) + r" \.units \.ust > \.chip\.st \{([^}]*)\}", css).group(1)
    assert "flex: none" in band and "white-space: nowrap" in band                 # its own width, one line
    assert not re.search(r"ellipsis|overflow|min-inline-size: 0|(?<![-\w])(inline-size|width|max-inline-size|max-width):", band)
    # the band and its time are laid out on the ID's line itself, so the time (last) is what wraps, never the label
    r = rules(css, D)
    assert "display: contents" in r[f"{D} .units .uh > .ust"]
    assert "flex-wrap: wrap" in r[f"{D} .units .uh"]
    ut = re.search(re.escape(D) + r" \.units \.ut \{([^}]*)\}", css).group(1)
    assert "margin-inline-start: auto" in ut and "flex: none" in ut              # at the line's end
    # nothing on Dispatch cuts the status: no rule for its tiles hides or clips the band
    for sel, decl in r.items():
        if ".chip.st" in sel or sel.endswith(".ust"):
            assert "ellipsis" not in decl and "overflow" not in decl, sel
    # even tiles: the ID's line one height whatever the tags; a note's line the incident link's height
    assert "min-block-size: var(--lh-xl)" in r[f"{D} .units .uh"]
    assert "line-height: var(--lh-xl)" in r[f"{D} .units .un"]
    assert "min-block-size: var(--ctl-sm)" in r[f"{D} .units .usub"]
    why = re.search(re.escape(D) + r" \.units \.usub \.why \{([^}]*)\}", css).group(1)
    assert "text-overflow: ellipsis" in why                                     # a long note: one line, whole on its title
    cad = text("cad.js")
    assert 'const w = el("span", "why", u.out_of_service); w.title = u.out_of_service;' in cad
    # a notch smaller than #207: tighter tiles, the old 30 px ID and 14 px band gone
    tile = re.search(re.escape(D) + r" \.units \.ulist > li\.urow \{([^}]*)\}", css).group(1)
    assert "padding: var(--s-1) var(--s-2)" in tile and "gap: var(--s-0)" in tile
    assert "* 1.25" not in css[css.index("Dispatch's rigs"):css.index("the pinned 60 Control feed")]


AGE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (name) => { const m = src.match(new RegExp("^function " + name + "\\([^)]*\\) \\{[\\s\\S]*?^\\}", "m"));
  if (!m) throw new Error("not found: " + name); return m[0]; };
const pad = (n) => String(n).padStart(2, "0");
let now = 1000000;
const serverNow = () => now;
const fmtTime = (unix, secs) => "AT" + unix + (secs ? "s" : "");
function setText(node, text) { text = text ?? ""; if (node.textContent !== text) node.textContent = text; }
eval([grab("fmtDur"), grab("fmtAge"), grab("since"), grab("sinceText")].join("\n"));
const cases = {};
for (const s of [0, 5, 59, 60, 3599, 3600, 3660, 23 * 3600 + 59 * 60, 86399, 86400, 102 * 3600 + 4 * 60 + 14,
                 623 * 3600 + 55 * 60 + 41, -3, null, NaN]) cases[String(s)] = fmtAge(s);
// a node as the board makes it: counts its writes
const node = () => { const n = { dataset: {}, writes: 0, _t: "", _title: "", removeAttribute(k) { if (k === "title") this._title = ""; } };
  Object.defineProperty(n, "textContent", { get() { return this._t; }, set(v) { this._t = v; this.writes++; } });
  Object.defineProperty(n, "title", { get() { return this._title; }, set(v) { this._title = v; this.writes++; } });
  return n; };
const at = now - (102 * 3600 + 4 * 60 + 14);
const u = since(node(), at, true);
const first = { text: u.textContent, title: u.title, age: "age" in u.dataset, since: u.dataset.since };
u.writes = 0;
for (let k = 1; k <= 45; k++) { now += 1; sinceText(u, now); }        // the tick, 45 s: still "4d 6h", nothing written
const quiet = u.writes;
const hoverBefore = u.title;
sinceText(u, now, true);                                              // pointed at: the title exact to the second
const hover = { text: u.textContent, title: u.title, changed: hoverBefore !== u.title };
// under an hour: every second, the title with it
now = 2000000;
const s = since(node(), now - 125, true);
now += 1; sinceText(s, now);
const short = { text: s.textContent, title: s.title };
// an incident's elapsed: as it was (fmtDur, no title)
const inc = since(node(), now - 3725);
const incident = { text: inc.textContent, title: inc.title, age: "age" in inc.dataset };
const cleared = since(u, null, true);
console.log(JSON.stringify({ cases, first, quiet, hover, short, incident, cleared: { text: cleared.textContent, title: cleared.title,
  since: cleared.dataset.since ?? null } }));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_unit_timers_are_compact_with_the_exact_time_on_the_title(tmp_path):
    h = tmp_path / "age.js"
    h.write_text(AGE_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["cases"] == {
        "0": "0:00", "5": "0:05", "59": "0:59", "60": "1:00", "3599": "59:59",        # under an hour: m:ss
        "3600": "1:00", "3660": "1:01", "86340": "23:59", "86399": "23:59",           # under a day: h:mm
        "86400": "1d 0h", "367454": "4d 6h", "2246141": "25d 23h",   # then days and hours: 102:04:14, 623:55:41
        "-3": "0:00", "null": "", "NaN": "",
    }
    # the tooltip: the exact duration and when it began
    assert out["first"] == {"text": "4d 6h", "title": "102:04:14 in this status, since AT632546s", "age": True,
                            "since": "632546"}
    assert out["quiet"] == 0                                    # past an hour the tick writes nothing until it changes
    assert out["hover"] == {"text": "4d 6h", "title": "102:04:59 in this status, since AT632546s", "changed": True}
    assert out["short"] == {"text": "2:06", "title": "02:06 in this status, since AT1999875s"}
    assert out["incident"] == {"text": "1:02:05", "title": "", "age": False}  # an incident's clock: exact, as it was
    assert out["cleared"] == {"text": "", "title": "", "since": None}


def test_one_formatter_for_every_unit_timer_on_the_one_tick():
    cad = text("cad.js")
    # the units board is the one place a unit's time is drawn: the Overview's compact list, Dispatch's tiles and the
    # wall board all come from renderUnits, and its one timer is the compact one
    assert cad.count("u.status_at, true)") == 1 and cad.count("fmtAge(") == 2          # defined once, used by sinceText
    tick = cad[cad.index("function tick() {"):cad.index("// ---- incidents: queue")]
    assert 'for (const n of document.querySelectorAll("[data-since]")) sinceText(n, now);' in tick
    assert cad.count("setInterval(tick, 1000)") == 1 and "setInterval(" not in cad[cad.index("function sinceText("):cad.index("function toLocalInput(")]
    # pointed at: the exact title then, no timer of its own
    assert 'e.target.closest("[data-since][data-age]")' in cad and "sinceText(n, serverNow(), true);" in cad


# -- New radio IDs: the top of the column on Dispatch -------------------------------------------------------------


def test_new_radio_ids_sit_at_the_top_of_the_column_above_the_rigs_on_dispatch():
    s = side()
    # in the column: the feed (hidden on Dispatch), the question, then the units; nothing shown on the console between
    assert s.index('id="feed"') < s.index('id="newids"') < s.index('<h2 class="console-only">Units</h2>') < s.index('id="units"')
    between = s[s.index('id="feed"'):s.index('<section id="newids"')]
    assert "<section" not in between and "<ol" not in between
    assert '<section id="newids" class="nid-sec console-only" hidden>' in s     # only while there is something to ask
    css = text("cad.css")
    assert not re.search(re.escape(D) + r"[^{]*(newids|nid-)", css)          # Dispatch never hides it
