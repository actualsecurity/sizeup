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
    cad_js = (static / "cad.js").read_text()
    for bid in ("radios-open", "radios-close", "note-add"):
        assert f'$("#{bid}")' in cad_js or bid == "note-add", f"#{bid} has no handler"
