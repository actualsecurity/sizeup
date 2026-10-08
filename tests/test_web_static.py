"""The page's update loop must only call functions that exist (an SDR rewrite once deleted
renderStorage/renderRerun, and every update then threw before drawing the traffic list)."""
import re
from pathlib import Path

JS = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "app.js").read_text()


def test_every_function_called_on_update_is_defined():
    body = re.search(r"^function onState\(data\) \{(.*?)^\}", JS, re.S | re.M).group(1)
    called = set(re.findall(r"^\s+([A-Za-z_]\w*)\(", body, re.M))
    defined = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(", JS, re.M))
    assert called and not (called - defined), f"called but not defined: {sorted(called - defined)}"


def test_buttons_in_the_page_have_handlers():
    html = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "index.html").read_text()
    for bid in re.findall(r'<button id="([\w-]+)"', html):
        assert f'$("#{bid}").addEventListener' in JS, f"#{bid} has no click handler"


def test_pages_load_only_scripts_that_exist_and_cad_buttons_have_handlers():
    static = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
    for page in ("index.html", "cad.html", "comms.html"):
        html = (static / page).read_text()
        for src in re.findall(r'<script src="/static/([\w./-]+)"', html):
            assert (static / src).is_file(), f"{page}: {src} missing"
    # the Radios editor lives in Settings -> Radios
    assert 'id="settings-radios"' in (static / "cad.html").read_text()
    assert 'RadiosPanel.mount($("#settings-radios")' in (static / "settings.js").read_text()


def test_one_palette_from_tokens_with_readable_text():
    """The look (style.css :root): graphite, PCFD red, no leftover navy/purple/cyan; text tokens AA on every surface."""
    static = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
    css = "".join((static / f).read_text() for f in ("style.css", "cad.css", "cadmap.css", "settings.css"))
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
    for page in ("index.html", "cad.html", "comms.html"):
        html = (static / page).read_text()
        assert 'rel="icon" type="image/svg+xml" href="/static/brand/pcfd-mark.svg"' in html and "<title>PCFD " in html
    for f in ("brand/pcfd-mark.svg", "brand/pcfd-patch.svg", "brand/pcfd-mark-32.png", "icons.svg"):
        assert (static / f).is_file(), f
    sprite = (static / "icons.svg").read_text()
    for use in set(re.findall(r'icons\.svg#(i-[\w-]+)', "".join((static / p).read_text() for p in ("index.html", "cad.html", "comms.html")))):
        assert f'id="{use}"' in sprite, use
