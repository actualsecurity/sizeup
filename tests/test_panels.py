"""Resizable side columns on /cad (GitHub #126, static/panels.js): a handle on the inner edge of the queue and of the
radio / units column, with limits, remembered per screen, keyboard-accessible, the maps following, hidden when the
columns stack. Static checks; the drag itself is checked in a browser at 1366 and 1920 (the PR has the numbers)."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"


def js():
    return (STATIC / "panels.js").read_text()


def test_the_page_loads_it_after_the_maps():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/panels.js"') > html.index('src="/static/cadmap.js"')
    assert html.index('src="/static/panels.js"') > html.index('src="/static/maplayers.js"')


def test_limits_left_right_and_the_middle():
    src = js()
    assert re.search(r'left: \{[^}]*min: 220, max: 480, prop: "--rail-w"', src)
    assert re.search(r'right: \{[^}]*min: 280, max: 560, prop: "--side-w"', src)
    assert "MID_MIN = 560" in src and "main.clientWidth - other - MID_MIN" in src
    assert 'matchMedia("(max-width: 1100px)")' in src                       # the stacked layout: widths not applied
    assert "if (BOARD) delete SIDES.left" in src                             # the wall board: no queue, the right only


def test_remembered_per_screen_in_try_catch():
    src = js()
    assert 'const KEY = "sizeup.panels"' in src and 'BOARD ? "monitor" : "cad"' in src
    for m in re.finditer(r"localStorage\.\w+Item", src):
        before = src[:m.start()]
        assert before.rfind("try {") > before.rfind("} catch"), m.group(0)


def test_a_keyboard_separator_with_its_value():
    src = js()
    for attr in ('"role", "separator"', '"aria-orientation", "vertical"', '"aria-valuemin"', '"aria-valuemax"', '"aria-valuenow"',
                 '"aria-controls"', '"aria-label"'):
        assert f"setAttribute({attr}" in src, attr
    assert "hd.tabIndex = 0" in src
    assert "ArrowLeft: -1, ArrowRight: 1" in src and "STEP = 16" in src and 'e.key === "Home"' in src
    assert 'addEventListener("dblclick", () => reset(side))' in src


def test_drag_with_pointer_capture_and_the_maps_follow():
    src = js()
    assert "setPointerCapture(e.pointerId)" in src and "releasePointerCapture" in src and '"pointercancel"' in src
    assert "requestAnimationFrame(() => { frame = 0; resizeMaps(); })" in src             # during the drag, once a frame
    end = src[src.index("const end = (ev) =>"):src.index('hd.addEventListener("pointermove", move)')]
    assert "resizeMaps()" in end and "remember(" in end                                 # and after it, then kept
    assert "m.map.resize()" in src
    assert "new ResizeObserver(() => apply()).observe(main)" in src                      # the window changed: re-clamped


def test_the_handles_on_the_tokens_and_hidden_when_stacked():
    css = (STATIC / "cad.css").read_text()
    rule = re.search(r"\.cad-main > \.col-handle \{(.*?)\}", css, re.S).group(1)
    assert "position: absolute" in rule and "cursor: col-resize" in rule and "touch-action: none" in rule
    assert "inline-size: var(--s-6)" in rule                                             # a 24 px target
    narrow = css[css.index("@media (max-width: 1100px) {"):]
    assert ".cad-main > .col-handle { display: none; }" in narrow
    assert ".col-handle:is(:hover, :focus-visible, .on)::before" in css
