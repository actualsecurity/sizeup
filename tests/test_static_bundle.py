"""/cad's scripts and stylesheets as one file each (sizeup/web/bundle.py), built with the hub image: every script of the
page in its order, versioned by content, served like any static file, the separate files on ?bundle=0 or when no
current build is there, and a page that still works when the task answering has another bundle or none."""
import gzip
import http.client
import json
import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad import dispatcher
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad, Settings
from sizeup.web import bundle as B
from sizeup.web import server

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
IMMUTABLE = "private, max-age=31536000, immutable"


@pytest.fixture
def static(tmp_path):
    """A copy of the static folder (the build writes into it), with no bundle yet."""
    d = tmp_path / "static"
    shutil.copytree(STATIC, d, ignore=shutil.ignore_patterns("bundle"))
    return d


@pytest.fixture
def hub(static, monkeypatch):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    monkeypatch.setattr(server, "STATIC_DIR", static)
    ws = server.WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": time.time()},
                          lambda cid: None, lambda iid: False, cad=CadApi(Cad(Settings(), log=lambda m: None)))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    port = ws.httpd.server_address[1]

    def get(path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body
    yield get
    ws.shutdown()


def page_tags(html: str):
    scripts = re.findall(r'<script src="/static/([^"]+)"></script>', html)
    sheets = re.findall(r'<link rel="stylesheet" href="/static/([^"]+)"[^>]*>', html)
    return scripts, sheets


def test_the_bundles_hold_every_script_and_stylesheet_of_the_page_in_order(static):
    html = (static / "cad.html").read_text()
    scripts, sheets = page_tags(html)
    assert scripts[0] == "vendor/maplibre-gl.js"            # the vendor library stays its own file (kept across deploys)
    blocks = {b.kind: b for b in B.blocks(html)}
    assert blocks["js"].files == scripts[1:] and len(scripts) > 40
    assert blocks["css"].files == sheets
    m = B.build(static)
    assert set(m) == {"js:cad", "css:cad"}
    js = (static / "bundle/cad.js").read_text()
    assert js.startswith('"use strict";\n')                  # strict throughout, as each file was
    at = 0
    for rel in scripts[1:]:                                  # each file whole, marked, in the page's order
        marker = js.index(f"/* file: {rel} */\n", at)
        body = (static / rel).read_text().rstrip("\n")
        assert js.index(body, marker) == marker + len(f"/* file: {rel} */\n"), rel
        at = marker + len(body)
    css = (static / "bundle/cad.css").read_text()
    assert [m.group(1) for m in re.finditer(r"^/\* file: ([\w./-]+) \*/$", css, re.M)] == sheets


def test_every_bundled_script_is_strict_and_the_build_refuses_one_that_is_not(static):
    for rel in B.blocks((static / "cad.html").read_text())[1].files:
        assert B.STRICT.match((static / rel).read_text()), f"{rel}: no \"use strict\" directive at the top"
    (static / "logs.js").write_text("// no directive\nvar x = 1;\n")
    with pytest.raises(ValueError, match="logs.js"):
        B.build(static)


def test_the_source_map_points_each_line_at_its_file(static):
    B.build(static)
    js = (static / "bundle/cad.js").read_text().split("\n")
    smap = json.loads((static / "bundle/cad.js.map").read_text())
    lines = smap["mappings"].split(";")
    assert len(lines) == len(js) - 1                          # (the last line ends the file)
    src = line = 0
    digits = {c: i for i, c in enumerate(B.B64)}
    for n, seg in enumerate(lines):
        if not seg:
            continue
        vals, v, shift = [], 0, 0
        for c in seg:
            d = digits[c]
            v |= (d & 31) << shift
            shift += 5
            if not d & 32:
                vals.append(-(v >> 1) if v & 1 else v >> 1)
                v = shift = 0
        src, line = src + vals[1], line + vals[2]
        rel = smap["sources"][src].removeprefix("/static/")
        assert js[n] == (static / rel).read_text().split("\n")[line], (n, rel, line)


def test_the_version_changes_when_any_input_changes_and_a_stale_build_is_never_served(static, hub):
    B.build(static)
    page = hub("/cad")[2].decode()
    v1 = re.search(r'/static/bundle/cad\.js\?v=([0-9a-f]{10})', page).group(1)
    c1 = re.search(r'/static/bundle/cad\.css\?v=([0-9a-f]{10})', page).group(1)
    with open(static / "panels.js", "a") as f:               # an input edited after the build: the files, not the bundle
        f.write("\n// changed\n")
    page = hub("/cad")[2].decode()
    assert "/static/bundle/cad.js" not in page and '<script src="/static/panels.js?v=' in page
    assert "/static/bundle/cad.css" in page                  # each bundle on its own: the stylesheets are current
    B.build(static)
    page = hub("/cad")[2].decode()
    v2 = re.search(r'/static/bundle/cad\.js\?v=([0-9a-f]{10})', page).group(1)
    assert v2 != v1 and f"/static/bundle/cad.css?v={c1}" in page        # the stylesheets didn't change
    with open(static / "health.css", "a") as f:
        f.write("\n/* changed */\n")
    B.build(static)
    page = hub("/cad")[2].decode()
    assert f"/static/bundle/cad.js?v={v2}" in page and f"/static/bundle/cad.css?v={c1}" not in page


def test_the_page_names_the_bundles_with_a_fallback_and_debug_gives_the_files(static, hub):
    files_page = hub("/cad")[2].decode()                     # no build (a checkout, the tests): the separate files
    assert "/static/bundle/" not in files_page
    B.build(static)
    code, h, body = hub("/cad")
    page = body.decode()
    assert code == 200 and h["cache-control"] == "no-cache"
    scripts, sheets = page_tags(page)
    assert [s.split("?")[0] for s in scripts] == ["vendor/maplibre-gl.js", "bundle/cad.js"]
    assert len(sheets) == 1 and sheets[0].startswith("bundle/cad.css?v=")
    assert re.search(r'<link rel="preload" as="style" href="/static/vendor/maplibre-gl\.css\?v=[0-9a-f]{10}">', page)
    # the fallback, should the answering task not have this bundle: every file, versioned, in order
    names = B.blocks((static / "cad.html").read_text())[1].files
    fb = re.search(r'<script>window\.SIZEUP_BUNDLE \|\| document\.write\("(.*?)"\)</script>', page).group(1)
    assert re.findall(r"<script src='/static/([\w./-]+)\?v=[0-9a-f]{10}'><\\/script>", fb) == names
    onerr = re.search(r'href="/static/bundle/cad\.css\?v=\w+" onerror="([^"]+)"', page).group(1)
    assert re.findall(r"'/static/([\w./-]+)\?v=[0-9a-f]{10}'", onerr) == B.blocks((static / "cad.html").read_text())[0].files
    assert hub("/monitor")[2] == body                        # the wall board: the same page
    debug = hub("/cad?bundle=0")[2].decode()                 # debugging: the separate tags, as before
    assert debug == files_page and "/static/bundle/" not in debug
    assert [s.split("?")[0] for s in page_tags(debug)[0]][1:] == names


def test_the_bundles_are_served_like_every_static_file(static, hub):
    B.build(static)
    page = hub("/cad")[2].decode()
    for ref in re.findall(r'/static/(bundle/cad\.(?:js|css)\?v=[0-9a-f]{10})', page):
        code, h, body = hub(f"/static/{ref}")
        assert code == 200 and h["cache-control"] == IMMUTABLE and h["etag"], ref
        assert h["content-type"] == ("text/css; charset=utf-8" if ".css" in ref else "text/javascript; charset=utf-8")
        assert ref.split("?v=")[1] == h["etag"].strip('"')[:10]        # the URL's version is the content's hash
        code, hz, gz = hub(f"/static/{ref}", {"Accept-Encoding": "gzip"})
        assert hz["content-encoding"] == "gzip" and gzip.decompress(gz) == body and hz["vary"] == "Accept-Encoding"
        assert hub(f"/static/{ref}", {"If-None-Match": h["etag"]})[0] == 304
    css = hub("/static/bundle/cad.css")[2]                   # its @import versioned, like the stylesheet's own
    assert re.search(rb'@import url\("/static/vendor/maplibre-gl\.css\?v=[0-9a-f]{10}"\)', css)
    code, h, smap = hub("/static/bundle/cad.js.map")
    assert code == 200 and h["content-type"] == "application/json" and json.loads(smap)["version"] == 3


def test_a_handover_never_breaks_the_page(static, hub):
    """Two tasks may answer one page load. A bundle version this task doesn't have: its own, not kept (the page from
    the other task revalidates, like any static file). A task with no bundle: 404, and the page's fallback loads the
    separate files, which every task has."""
    B.build(static)
    body = hub("/static/bundle/cad.js")[2]
    code, h, other = hub("/static/bundle/cad.js?v=0123456789")
    assert code == 200 and other == body and h["cache-control"] == "no-cache"
    shutil.rmtree(static / "bundle")
    assert hub("/static/bundle/cad.js?v=0123456789")[0] == 404
    assert "/static/bundle/" not in hub("/cad")[2].decode()


def test_other_pages_are_unchanged(static, hub):
    B.build(static)
    page = hub("/static/station.html")[2].decode()
    assert "/static/bundle/" not in page and '<script src="/static/station.js?v=' in page


def test_a_block_with_anything_but_plain_tags_is_refused():
    for inner in ('<script src="/static/a.js" defer></script>\n', "<script>x()</script>\n",
                  '<script type="module" src="/static/a.js"></script>\n'):
        with pytest.raises(ValueError):
            B.blocks(f"<!-- bundle js x -->\n{inner}<!-- /bundle -->\n")
