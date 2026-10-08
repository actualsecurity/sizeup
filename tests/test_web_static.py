"""The web page (static/cad.html: the console's views and the /monitor wall board) only loads what
exists, only calls what is defined, every button does something, the look comes from the tokens,
and every POST a script makes is in the permission map with the role it should need."""
import re
from pathlib import Path

from sizeup.cad.api import needs

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
PAGES = ("cad.html",)                      # the only page: /cad and /monitor (comms.html, index.html are gone)
JS_FILES = sorted(p.name for p in STATIC.glob("*.js"))


def js(name):
    return (STATIC / name).read_text()


def test_every_function_called_on_update_is_defined():
    """An SDR rewrite once deleted functions the update loop still called, and every update threw."""
    src = js("cad.js")
    defined = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(", src, re.M))
    for fn in ("onState", "render"):
        body = re.search(rf"^function {fn}\(\w*\) \{{(.*?)^\}}", src, re.S | re.M).group(1)
        called = set(re.findall(r"^\s+([A-Za-z_]\w*)\(", body, re.M))
        assert called and not (called - defined), f"{fn}: called but not defined: {sorted(called - defined)}"


def test_buttons_in_the_page_have_handlers():
    html = (STATIC / "cad.html").read_text()
    scripts = "".join(js(n) for n in JS_FILES)
    for bid in re.findall(r'<button id="([\w-]+)"(?! type="submit")', html):     # a submit button: its form's handler
        assert re.search(rf'\$\("#{bid}"\)\.addEventListener|RadioLive\.button\(\$\("#{bid}"\)', scripts), f"#{bid} has no handler"


def test_pages_load_only_scripts_that_exist_and_nothing_points_at_the_old_pages():
    for gone in ("comms.html", "comms.js", "index.html", "app.js"):
        assert not (STATIC / gone).exists(), gone
    for page in PAGES:
        html = (STATIC / page).read_text()
        for src in re.findall(r'<script src="/static/([\w./-]+)"', html):
            assert (STATIC / src).is_file(), f"{page}: {src} missing"
        for href in re.findall(r'href="/static/([\w./-]+)"', html):
            assert (STATIC / href).is_file(), f"{page}: {href} missing"
        for new in ("radio-view.js", "radio-controls.js", "board.js"):
            assert f'<script src="/static/{new}">' in html
    for name in [*JS_FILES, *PAGES]:
        text = (STATIC / name).read_text()
        assert "comms.js" not in text and "comms.html" not in text and "app.js" not in text, name
        assert not re.search(r"""["'`]/comms\b""", text), f"{name} still links to /comms (now /cad#radio)"
    # the Radios editor and the receivers / talkgroup switches live in Settings -> Radios
    html = (STATIC / "cad.html").read_text()
    assert 'id="settings-radios"' in html and 'id="settings-radio-controls"' in html
    assert 'RadiosPanel.mount($("#settings-radios")' in js("settings.js")
    assert 'RadioControls.mount($("#settings-radio-controls"))' in js("settings.js")
    # one header: the console's tabs and the board's links on the same bar
    for tab in ("dispatch-open", "overview-open", "radio-open", "settings-open"):
        assert f'id="{tab}" class="tab' in html
    assert 'href="/monitor"' in html and 'class="nav board-only" href="/cad"' in html


def test_one_palette_from_tokens_with_readable_text():
    """The look (style.css :root): graphite, PCFD red, no leftover navy/purple/cyan; text tokens AA on every surface."""
    css = "".join((STATIC / f).read_text() for f in ("style.css", "cad.css", "cadmap.css", "settings.css"))
    for old in ("#18172e", "#141329", "#1c1b38", "#3fd0e0", "#a3a9ff", "#cfcdea", "rgba(20, 19, 40", "rgba(12, 11, 26", "gradient("):
        assert old not in css.lower(), old
    tok = dict(re.findall(r"^\s+--([\w-]+):\s*(#[0-9A-Fa-f]{6});", css, re.M))

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    for fg in ("text", "text-2", "dim", "accent-text", "ok", "ok-text", "warn", "err", "info"):
        for bg in ("bg", "panel", "raised", "hover"):
            hi, lo = sorted((lum(tok[fg]), lum(tok[bg])), reverse=True)
            assert (hi + 0.05) / (lo + 0.05) >= 4.5, (fg, bg)
    for page in PAGES:
        html = (STATIC / page).read_text()
        assert 'rel="icon" type="image/svg+xml" href="/static/brand/pcfd-mark.svg"' in html and "<title>PCFD " in html
    for f in ("brand/pcfd-mark.svg", "brand/pcfd-patch.svg", "brand/pcfd-mark-32.png", "icons.svg"):
        assert (STATIC / f).is_file(), f
    sprite = (STATIC / "icons.svg").read_text()
    used = "".join((STATIC / p).read_text() for p in PAGES) + "".join(js(n) for n in JS_FILES)
    for use in set(re.findall(r'icons\.svg#(i-[\w-]+)', used)) | {f"i-{n}" for n in re.findall(r'Icon\("([\w-]+)"', used)}:
        assert f'id="{use}"' in sprite, use


def test_one_type_scale_and_header_for_console_and_board():
    """Sizes come from style.css's tokens; the views don't set their own base size or header (the old
    monitor scaled with the screen and its header was taller than the CAD's). The board is one step up,
    in its main area only."""
    style, cad = (STATIC / "style.css").read_text(), (STATIC / "cad.css").read_text()
    for tok in ("--fs: 14px", "--hdr-h:", "--ctl-h:", "--side-w:", "--t-xs:", "--t-s:", "--sp-2:"):
        assert tok in style, tok
    for other in ("cad.css", "cadmap.css", "settings.css"):
        text = (STATIC / other).read_text()
        assert "--fs:" not in text and not re.search(r"^\s*header\s*\{", text, re.M), other
    assert "var(--side-w)" in cad and "html.board main { font-size: 1.25em; }" in cad
    assert "clamp(" not in style.split("}")[0]                                  # no screen-height scaling


def test_every_post_the_scripts_make_is_in_the_permission_map():
    """The POSTs the page makes (the Radio view's "+ Event", re-transcribe, Settings > Radios' switches,
    closing the paged incident) need what the server enforces (sizeup/cad/api.py WRITES)."""
    scripts = "".join(js(n) for n in JS_FILES)
    for path, literal, role in (("/api/retranscribe", '"/api/retranscribe"', "admin"),
                                ("/api/talkgroup", '"/api/talkgroup"', "admin"),
                                ("/api/talkgroup-group", '"/api/talkgroup-group"', "admin"),
                                ("/api/mode", '"/api/mode"', "admin"),
                                ("/api/cad/incident/12/link", "`/api/cad/incident/${iid}/link`", "dispatch"),
                                ("/api/incident/abc/close", "`/api/incident/${encodeURIComponent(id)}/close`", "dispatch")):
        assert literal in scripts, f"no script posts to {path}"
        assert needs(path) == role, path
