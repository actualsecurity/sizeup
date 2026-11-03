"""The incident view's comments box, up top where the remarks sat (#221).

Dispatcher, 2026-11: "id also like to move timeline up higher to see all the comments. Like where it says Zone 1 -
General at the top. Putting the comments there would be great. That way i can just scroll down in a box there with a
typing area".

    the box      under the caller line, in the details column (beside the map, which may be dragged wider: #210):
                 the event's remarks first, then every comment in time order, the newest at the bottom; each with
                 its time, who and the text (unit tags and "@places" as chips). Its own scroll, a fixed most height.
    follows      the newest as one comes in, unless the reader scrolled up (then a "New" button); a new event, a new
                 choice or your own comment starts at the newest.
    typing       a field right under it: Enter adds, Shift+Enter a new line; an open "@" pick list takes Enter first.
                 A viewer sees the box, the field locked (and the server refuses: test_roles).
    everything   a "Comments / Everything" switch mixes in the Timeline's other entries; the Timeline itself stays
                 below, folded as before (test_timeline_fold).
    no churn     the rows sync on the state push like the Timeline's; nothing by the second (#146).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")


def text(name):
    return (STATIC / name).read_text()


def js_function(src, name):
    """`function name(...) { ... }` (or `const name = ... ;` on one line) out of cad.js, braces balanced."""
    m = re.search(rf"^function {name}\(", src, re.M)
    if not m:
        m = re.search(rf"^const {name} = [^;\n]*;", src, re.M)
        assert m, name
        return m.group(0)
    i = src.index("{", src.index(")", m.end()))
    depth, j = 1, i + 1
    while depth:
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        j += 1
    return src[m.start():j]


# -- the page -------------------------------------------------------------------------------------------------------

def test_the_box_sits_under_the_caller_line_in_the_details_column_with_the_field_under_it():
    html = text("cad.html")
    grid = html[html.index('<div id="d-grid" class="inc-grid">'):html.index('<div id="d-mapbox"')]
    # where the remarks' box was: after the caller line (and the airport / roads / tides / page rows), before the map
    assert grid.index('id="d-caller"') < grid.index('id="d-pages"') < grid.index('<section id="d-cmts" class="cmts"')
    assert 'id="d-narr"' not in html and not re.search(r"\.narr\b", text("cad.css"))
    box = html[html.index('<section id="d-cmts"'):html.index("</section>", html.index('<section id="d-cmts"'))]
    # the list, then the field right under it: a textarea (Shift+Enter a new line), the @-tag picker with it
    assert box.index('<ol id="d-cmts-list" class="cmts-list"') < box.index('<form id="note-form"')
    assert re.search(r'<textarea id="note-text" rows="2" maxlength="2000"', box)
    assert 'role="combobox"' in box and 'aria-controls="note-sugg"' in box and 'id="note-sugg"' in box
    assert 'id="note-add" type="submit"' in box and 'id="note-tags"' in box
    assert "Enter adds, Shift+Enter a new line" in box
    # the switch, and the "New" button over the list
    assert 'id="d-cmts-only" type="button" class="sm" aria-pressed="true"' in box
    assert 'id="d-cmts-all" type="button" class="sm" aria-pressed="false"' in box
    assert '<button id="d-cmts-new" type="button" class="sm cmts-new" hidden>' in box
    # one note form on the page, no longer in the Timeline
    assert html.count('id="note-form"') == 1 and html.count('id="note-text"') == 1


def test_the_box_has_a_fixed_most_height_and_scrolls_on_its_own():
    css = text("cad.css")
    rule = re.search(r"\.cmts-list \{(.*?)\}", css, re.S).group(1)
    assert "max-block-size: calc(var(--map-h) * 0.6)" in rule          # ~216 px: about a third of the column
    assert "overflow-y: auto" in rule and "overscroll-behavior: contain" in rule
    assert ".cm .txt { white-space: pre-wrap;" in css                 # a Shift+Enter line stays a line


# -- what it shows (node) -------------------------------------------------------------------------------------------

ITEMS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
eval(process.argv[3]);
const inc = { id: 7, remarks: "ZONE 1 - GENERAL", created_at: 100, source: "manual", events: [
  { id: 1, kind: "created", at: 100, text: "Event created", operator: "Corey" },
  { id: 2, kind: "note", at: 110, text: "first", operator: "Corey" },
  { id: 3, kind: "status", at: 120, text: "E63 dispatched", operator: "Corey" },
  { id: 4, kind: "note", at: 130, text: "E63 stated that the alarm is resetting", units: "E63", operator: "Sam" },
  { id: 5, kind: "system", at: 140, text: "Caller info", operator: "" },
]};
const page = { id: 8, remarks: "SMOKE IN THE BUILDING", created_at: 50, source: "60 Control page", events: [
  { id: 9, kind: "created", at: 50, text: "From 60 Control page", operator: "" },
  { id: 10, kind: "accepted", at: 60, text: "Accepted", operator: "Corey" },
]};
const pick = (xs) => xs.map((e) => [e.id, e.kind, e.at, e.operator || "", e.text]);
console.log(JSON.stringify({
  only: pick(commentItems(inc, false)), all: pick(commentItems(inc, true)),
  none: pick(commentItems({ ...inc, remarks: "" }, false)), page: pick(commentItems(page, false)),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_remarks_first_then_the_comments_in_time_order(tmp_path):
    js = text("cad.js")
    h = tmp_path / "items.js"
    h.write_text(ITEMS)
    fn = js_function(js, "commentItems")
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js"), fn], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    # the remarks first, at the call's time, by who took it; then the comments only, oldest first (newest at the bottom)
    assert out["only"] == [["remarks", "remarks", 100, "Corey", "ZONE 1 - GENERAL"],
                           [2, "note", 110, "Corey", "first"],
                           [4, "note", 130, "Sam", "E63 stated that the alarm is resetting"]]
    # Everything: every entry in the Timeline's order, the remarks still first
    assert [x[0] for x in out["all"]] == ["remarks", 1, 2, 3, 4, 5]
    assert out["none"] == [[2, "note", 110, "Corey", "first"], [4, "note", 130, "Sam", "E63 stated that the alarm is resetting"]]
    # a page's remarks are the page's: by 60 Control, not by who accepted it
    assert out["page"] == [["remarks", "remarks", 50, "60 Control page", "SMOKE IN THE BUILDING"]]


def test_each_row_shows_time_who_and_the_text_with_its_chips():
    js = text("cad.js")
    rc = js_function(js, "renderComments")
    assert "renderComments(inc);" in js_function(js, "renderDetail")
    assert 'meta.append(el("span", "t", fmtTime(e.at, true)));' in rc        # the console's zone (fmt: the station's)
    assert "if (e.operator) meta.append(opSpan(e.operator));" in rc
    # a comment's text is the Timeline's: unit tags (they show that unit's radio) and "@places" (the spot on the map)
    assert 'e.kind === "remarks" ? el("div", "txt", e.text) : eventTxt(inc, e)' in rc
    et = js_function(js, "eventTxt")
    assert "unitChip(u)" in et and "noteNodes(e, noteText(e))" in et and "noteNodes(e, e.text)" in et
    assert "li.replaceChildren(meta, eventTxt(inc, e));" in js_function(js, "renderEvents")
    # synced like the Timeline: a row redrawn only when it changes, no clock in its signature (#146)
    assert "syncList(list, items, {" in rc and "key: (e) => String(e.id)," in rc
    sig = re.search(r"sig: \(e\) => (.*?),\n", rc).group(1)
    assert "now" not in sig.lower() and "Date" not in sig
    # the switch: remembered per browser through store(), like the Timeline's fold
    assert 'store("sizeup.cad.commentsAll") === "1"' in js and 'store("sizeup.cad.commentsAll", on ? "1" : "0")' in js
    assert "cad.commentsAll = commentsAll();" in js
    assert '$("#d-cmts-all").addEventListener("click", () => commentsAll(true));' in js
    assert '$("#d-cmts-only").addEventListener("click", () => commentsAll(false));' in js


# -- adding ---------------------------------------------------------------------------------------------------------

KEYS = r"""
const src = require("fs").readFileSync(process.argv[2], "utf8");
const m = src.match(/\$\("#note-text"\)\.addEventListener\("keydown", (\(e\) => \{[\s\S]*?\n\})\);/);
let submits = 0;
const $ = (sel) => { if (sel !== "#note-form") throw new Error(sel); return { requestSubmit() { submits++; } }; };
const handler = eval(m[1]);
const press = (o) => { const e = { key: "Enter", shiftKey: false, isComposing: false, defaultPrevented: false,
  prevented: false, preventDefault() { this.prevented = true; }, ...o }; const before = submits; handler(e);
  return { submitted: submits > before, prevented: e.prevented }; };
console.log(JSON.stringify({
  enter: press({}), shift: press({ shiftKey: true }), ime: press({ isComposing: true }),
  pick: press({ defaultPrevented: true }), other: press({ key: "a" }),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_enter_adds_and_shift_enter_makes_a_new_line(tmp_path):
    h = tmp_path / "keys.js"
    h.write_text(KEYS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["enter"] == {"submitted": True, "prevented": True}           # Enter: added, no new line
    assert out["shift"] == {"submitted": False, "prevented": False}         # Shift+Enter: the textarea's own new line
    assert out["ime"] == {"submitted": False, "prevented": False}           # composing a character: not yet
    assert out["pick"]["submitted"] is False                                # the @ pick list took Enter (noteKeys)
    assert out["other"] == {"submitted": False, "prevented": False}
    js = text("cad.js")
    # the pick list's handler runs first (registered first), so its Enter takes the item
    assert js.index('$("#note-text").addEventListener("keydown", noteKeys);') < js.index("e.key !== \"Enter\" || e.shiftKey")
    # the usual note API, the text as typed (new lines kept), the tags and places with it
    sub = re.search(r'\$\("#note-form"\)\.addEventListener\("submit", async \(e\) => \{(.*?)\n\}\);', js, re.S).group(1)
    assert 'api("POST", `/api/cad/incident/${inc.id}/note`' in sub and "text: input.value.trim()" in sub
    # your own comment: to the newest, set before the answer (the state push with it may come first)
    assert sub.index('$("#d-cmts-list")._follow = true;') < sub.index("await api(")


def test_viewers_see_the_box_but_cannot_add():
    js = text("cad.js")
    ro = re.search(r"const RO_DISPATCH = (.*?);\n", js, re.S).group(1)
    assert "#note-text, #note-add" in ro
    # the switch and the list stay theirs (reading); only the field and its button are locked
    assert "d-cmts" not in ro
    assert 'const ph = canDo("dispatch") ?' in js and "View only: comments need a dispatcher" in js
    assert 'if ($("#note-text").placeholder !== ph) $("#note-text").placeholder = ph;' in js      # set only on a change


# -- following the newest (node) ------------------------------------------------------------------------------------

FOLLOW = r"""
const { cmtsFollow, cmtsAtEnd } = new Function(process.argv[2] + "\nreturn { cmtsFollow, cmtsAtEnd };")();
const box = (o) => ({ scrollHeight: 1000, clientHeight: 200, scrollTop: 800, ...o });
const out = {};
let b = box({ scrollTop: 0 });                     // a new event (fresh): to the newest
out.fresh = [cmtsFollow(b, true, false), b.scrollTop, b._follow];
b = box({ scrollTop: 800, _follow: true });       // at the bottom, one more came in (taller now): follows it
b.scrollHeight = 1100;
out.grewAtEnd = [cmtsFollow(b, false, true), b.scrollTop];
b = box({ scrollTop: 300, _follow: false });      // scrolled up, one came in: stays, says "New"
b.scrollHeight = 1100;
out.grewUp = [cmtsFollow(b, false, true), b.scrollTop];
b = box({ scrollTop: 300, _follow: false });      // scrolled up, nothing new: stays, no "New"
out.sameUp = [cmtsFollow(b, false, false), b.scrollTop];
b = box({ scrollTop: 790, _follow: true });       // within the slack of the bottom: left alone (no churn)
out.nearEnd = [cmtsFollow(b, false, false), b.scrollTop];
out.atEnd = [cmtsAtEnd(box({ scrollTop: 780 })), cmtsAtEnd(box({ scrollTop: 700 }))];
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_it_follows_the_newest_unless_scrolled_up(tmp_path):
    js = text("cad.js")
    code = "\n".join(js_function(js, n) for n in ("CMTS_SLACK", "cmtsAtEnd", "cmtsFollow"))
    h = tmp_path / "follow.js"
    h.write_text(FOLLOW)
    out = json.loads(subprocess.run([NODE, str(h), code], capture_output=True, text=True, check=True, timeout=30).stdout)
    assert out["fresh"] == [False, 1000, True]
    assert out["grewAtEnd"] == [False, 1100]
    assert out["grewUp"] == [True, 300]
    assert out["sameUp"] == [False, 300]
    assert out["nearEnd"] == [False, 790]
    assert out["atEnd"] == [True, False]


def test_the_reader_scrolling_decides_and_a_hidden_box_never_does():
    js = text("cad.js")
    rc = js_function(js, "renderComments")
    # fresh: another event or the other choice; grew: a new last row
    assert "const fresh = list._view !== view, grew = !fresh && last !== list._last;" in rc
    assert 'if (cmtsFollow(list, fresh, grew)) show($("#d-cmts-new"), true);' in rc
    scroll = js[js.index('$("#d-cmts-list").addEventListener("scroll"'):js.index('$("#d-cmts-new").addEventListener("click"')]
    assert "if (!list.clientHeight) return;" in scroll and "list._follow = cmtsAtEnd(list);" in scroll
    assert "{ passive: true }" in scroll
    new = js[js.index('$("#d-cmts-new").addEventListener("click"'):]
    assert "list._follow = true;" in new[:300] and "list.scrollTop = list.scrollHeight;" in new[:300]

