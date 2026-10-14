"""The incident view's Timeline folded to start, with its count and the latest entry in the header (#107).

user, 2026-10-13: "let's default minimize the timeline on incident tabs"
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"


def test_the_timeline_starts_folded_with_its_count_and_latest_entry_and_the_note_box_outside_the_fold():
    html = (STATIC / "cad.html").read_text()
    js = (STATIC / "cad.js").read_text()
    css = (STATIC / "cad.css").read_text()
    # the card starts folded: a real button with aria-expanded / aria-controls, the caret, the count and the
    # latest entry's one-line preview next to the name, the list hidden
    card = re.search(r'<div id="d-timeline" class="card tl-fold">(.*?)<form id="note-form"', html, re.S).group(1)
    assert re.search(r'<button id="d-events-toggle" class="sec-toggle" type="button" aria-expanded="false" aria-controls="d-events"', card)
    assert '<span class="caret" aria-hidden="true">▸</span><span class="h2">Timeline</span><span id="d-events-n" class="dim sum">' in card
    assert '<span id="d-events-last" class="dim sum tl-last" hidden>' in card
    assert '<ol id="d-events" class="list events" hidden>' in card
    # the note box is after the folded list, in the same card, outside the folded region
    assert re.search(r'<ol id="d-events"[^>]*></ol>\s*<form id="note-form"', html)
    assert 'aria-controls="d-events"' in html and html.count('id="d-events"') == 1
    assert "note-form" not in card and 'id="note-text"' in html and 'id="note-add"' in html
    # the header shows the count and the latest entry only while folded, the list only while open; no unfold on render
    assert "setText($(\"#d-events-n\"), events.length ? `· ${events.length}` : \"\")" in js
    assert 'setText($("#d-events-last"), last ? eventLine(last) : "")' in js
    assert 'show($("#d-events-last"), !open && !!last)' in js and 'show($("#d-events"), open)' in js
    assert '$("#d-events-toggle").setAttribute("aria-expanded", String(open))' in js
    assert '$("#d-timeline").classList.toggle("open", open)' in js
    assert "`${fmtTime(e.at, true)} ${EVENT_KINDS[e.kind] || e.kind} · ${" in js
    # the choice is remembered per browser, through the try/catch store() like Closed 12h
    assert 'store("sizeup.cad.timelineOpen")' in js and 'store("sizeup.cad.timelineOpen", open ? "1" : "0")' in js
    assert '$("#d-events-toggle").addEventListener("click", () => timelineFold(!cad.timelineOpen))' in js
    assert "cad.timelineOpen = timelineFold();" in js
    assert re.search(r"function store\(key, value\) \{\s*try \{", js)
    # adding a note re-renders the incident (count and preview), never touching the fold
    note = re.search(r'\$\("#note-form"\)\.addEventListener\("submit", async \(e\) => \{(.*?)\n\}\);', js, re.S).group(1)
    assert "render();" in note and "timelineFold" not in note and "timelineOpen" not in note
    # the caret turns when open; the preview is one line, truncated
    assert ".tl-fold.open .sec-toggle .caret { transform: rotate(90deg); }" in css
    assert ".tl-fold .sec-toggle .tl-last { flex: 1 1 0; min-inline-size: 0; white-space: nowrap; }" in css
    assert ".sec-toggle .sum { font-size: var(--fs-xs); font-weight: 400; overflow: hidden; text-overflow: ellipsis; }" in css
    # the wall board never renders an incident's detail: nothing to fold there
    assert "function selectIncident(id) {\n  if (BOARD) return;" in js
