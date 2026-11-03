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
    assert r[f"{D} .units .un"].strip().startswith("font-size: calc(var(--fs-2xl) * 1.25)")      # the ID: 30 px
    band = re.search(re.escape(D) + r" \.units \.ust > \.chip\.st \{([^}]*)\}", css).group(1)
    assert "background: var(--c)" in band and "color: var(--fg, var(--text))" in band and "font-size: var(--fs-md)" in band
    assert "font-size: var(--fs-md)" in r[f"{D} .units .ut"]                                     # the time, readable
    assert "grid-column: 1 / -1" in r[f"{D} .units .ulist > li.urow.open"]                     # the status buttons: full width
    assert "display: none" in r[f"{D} .units .uh > :is(.spacer, .utitle)"]
    # every rule for it is Dispatch's own: the Overview's compact board is as it was
    assert re.search(r"^\s*\.ulist > li\.urow \{ padding: var\(--s-1\) var\(--s-2\);", css, re.M)
    assert re.search(r"^\s*\.ust \{ display: inline-flex;", css, re.M)


def test_the_tile_takes_its_color_from_the_status_and_keeps_the_board_as_it_was():
    cad = text("cad.js")
    body = cad[cad.index("function renderUnits() {"):cad.index("function renderUnitLog() {")]
    assert 'li.style.setProperty("--st", unitStatus(u.status).bg || "#9AA3AD");' in body
    assert 'ust.append(statusChip(u.status), since(el("span", "ut"), u.status_at));' in body
    assert 'head.append(el("span", "spacer"), ust);' in body
    assert 'el("span", "chip joint", j.tag)' in body                     # the RB tag
    assert 'statusButtons(u, "board")' in body and "cad.openUnit = cad.openUnit === id ? null : id;" in body
    chip = cad[cad.index("function statusChip("):cad.index("function statusColors(")]
    assert 'if (st.fg) c.style.setProperty("--fg", st.fg);' in chip       # the data file's text color on its fill
    # #146: the redraw signature is what it was (no clock in it); the time ticks on the one tick ([data-since])
    sig = re.search(r"sig: \(u\) => JSON\.stringify\(\[([^\]]*)\]", body).group(1)
    assert "serverNow" not in sig and "Date.now" not in sig and "u.status_at" in sig
    assert "since(" in body


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
