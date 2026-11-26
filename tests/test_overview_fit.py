"""The console's Overview fits the window (GitHub #952; admin, 2026-11-26: "I shouldn't have to scroll inside of the map's
window like this", "that sub area needs to fit in the window, no reason for it to be scrollable").

Before: the map view was the screen's height less the header only (cadmap.css .ov-view), so with two weather banners and
the live player's strip (#895) the Overview ran ~100 px past its column; the column scrolled and the Overview's title
row and the map's top bar went out of sight. The Layers box (capped at 70 % of the screen, every group open, every
source listed) was taller than the map and scrolled inside it.

Now, on a desk (> 1100 px): the Overview is its column's height, whatever is above and below it; the map (or the map
and the cameras) fill what is left under its head; the Layers box is never taller than its map, each titled group folds
to one line with its "N on" (maplayers.js foldGroup; open or closed as the viewer left it), and the sources are one
Sources fold at the foot.

    the source      (everywhere, CI too)
    in a browser    (Playwright, when installed: not on CI) 2400 x 1500, 1600 x 1000 and 1280 x 800 with two long
                    weather banners, the player's strip and the Layers box open: no page scroll either way, the
                    Overview's column doesn't scroll, its title row and the map's top in view, the whole box in the
                    map, its last row reachable; the fold by keyboard, kept on a redraw and remembered.
                    SIZEUP_SHOTS=<dir> saves the PR's screenshots.

Everything shown is made up: the alerts' text, the operator, the person.
"""
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
SHOTS = os.environ.get("SIZEUP_SHOTS", "")


def text(name):
    return (STATIC / name).read_text()


# ---- the source ------------------------------------------------------------------------------------------------------


def test_the_overview_fills_its_column_on_a_desk():
    css = text("cadmap.css")
    desk = re.search(r"@media \(min-width: 1101px\) \{\n    html:not\(\.board\) \.overview \{(.*?)\n  \}\n", css, re.S)
    assert desk, "the desk's block"
    block = desk.group(0)
    assert "html:not(.board) .overview { flex: 1 1 auto; min-block-size: 0; }" in block
    assert "grid-template-rows: minmax(0, 1fr); align-items: stretch;" in block
    assert "html:not(.board) .ov-grid[data-layout] .ov-view { position: relative; block-size: auto;" in block
    # after every other rule that sizes the view (the same specificity: the later one wins)
    assert css.index(block) > css.rindex("block-size: calc(100dvh - var(--hdr-h)")
    assert css.index(block) > css.index("@media (max-width: 1100px) {\n    .ov-view")
    assert "html.board" not in block                                   # the wall board's Overview as it was


def test_the_layers_box_folds_its_groups_and_its_sources():
    ml = text("maplayers.js")
    body = re.search(r"    renderPanel\(\) \{(.*?)\n    \}\n", ml, re.S).group(1)
    assert "sources.push([s.label, line]);" in body and 'sources.push(["Airport",' in body
    assert "this.panel.append(line);" not in body                       # no source lines in the middle of the box
    assert body.index("if (sources.length) panel.append(this.sourcesFold(sources, folds));") > body.rindex("folded(el)")
    fold = re.search(r"    foldBox\(name, folds, summary, body\) \{(.*?)\n    \}\n", ml, re.S).group(1)
    assert 'const key = `layers:${name}`;' in fold and "sum.dataset.key = key;" in fold          # focus kept on a redraw
    assert 'typeof folds[key] === "boolean" ? folds[key] : fold(key);' in fold and "det.open = was === true;" in fold
    # only a person's toggle is kept: not the one the open above queues (it would put back a stale fold)
    assert 'let last = det.open;' in fold
    assert 'det.addEventListener("toggle", () => { if (det.open !== last) { last = det.open; setFold(key, det.open); } });' in fold
    assert 'if (!panel.hidden) for (const f of panel.querySelectorAll("details[data-fold]"))' in body
    group = re.search(r"    foldGroup\(el, folds, sources\) \{(.*?)\n    \}\n", ml, re.S).group(1)
    assert 'el.querySelector(":scope > .ml-ghead")' in group and "if (!title) return el;" in group
    assert '!x.closest(".ml-sub-check") && x.dataset.key !== "gis:3d"' in group and '`${on} on`' in group
    assert "checkbox\"]:checked:not(:disabled)" in group and 'el.querySelector(".ml-err")' in group      # trouble shows folded
    assert '[" · ", n]' in group                                       # two links in a head: apart
    css = text("cadmap.css")
    assert ".ml-gsum { display: flex; align-items: center; gap: var(--s-2); min-block-size: var(--ctl-sm);" in css
    assert '.ml-gsum::before { content: "▸"; content: "▸" / "";' in css          # the arrow isn't read out


# ---- in a browser ----------------------------------------------------------------------------------------------------

AREA = [-73.7067, 40.9866, -73.6552, 41.0739]          # sizeup/roads.py AREA_BBOX
LONG = ("issued for the example zones until tomorrow morning by the example Weather Service office; "
        "the rest of this headline runs on past any screen so the line is cut, never wrapped, never wider than the page") * 3


def data(now):
    st = {"on": True, "at": now, "n": 3, "error": "", "why": ""}
    roads = {"configured": True, "note": "", "rev": 1, "bbox": AREA, "cameras": [], "events": [], "signs": [],
             "highway_places": {"mileposts": [], "exits": []},
             "sources": [{"name": "511ny", "short": "511NY", "label": "511NY", "link": "https://511ny.org/", "configured": True,
                          "at": now - 300, "note": "", "status": {"cameras": st, "events": st, "signs": st}},
                         {"name": "thruway", "short": "Thruway", "label": "NYS Thruway Authority",
                          "link": "https://www.thruway.ny.gov/", "configured": True, "at": now - 200, "note": "",
                          "status": {"cameras": st, "events": st}}]}
    from sizeup.gis import DEFAULT_LAYERS
    gis = {"configured": True, "source": "Westchester County GIS", "attribution": "Westchester County GIS",
           "link": "https://gis.westchestercountyny.gov/", "terms": "https://gis.westchestercountyny.gov/", "rev": 1,
           "bbox": AREA, "layers": [l.public() | {"at": now - 3600, "n": 5, "bytes": 100, "error": "", "updated": now, "rev": 1,
                                                  "checked": now, "how": "", "loading": False} for l in DEFAULT_LAYERS]}
    wx = {"alerts": [{"id": "x1", "event": "Coastal Flood Warning", "level": "warning", "severity": "Moderate",
                      "urgency": "Expected", "certainty": "Likely", "headline": f"Coastal Flood Warning {LONG}", "until": now + 80000},
                     {"id": "x2", "event": "Wind Advisory", "level": "advisory", "severity": "Minor", "urgency": "Expected",
                      "certainty": "Likely", "headline": f"Wind Advisory {LONG}", "until": now + 70000}],
          "conditions": {"short": "50°F", "wind_mph": 0, "name": "Example Airport", "station": "KXYZ", "at": now,
                         "stale": False, "text": "Cloudy"}}
    trains = {"configured": True, "note": "", "trains": [], "stations": [], "line": [], "lanes": {}, "tracks": {},
              "railref": {}, "at": now, "label": "Metro-North", "disclaimer": "Estimated", "bbox": AREA, "now": now}
    cams = {"configured": True, "highway": None, "down": [],
            "cameras": [{"id": f"cam-{i}", "name": f"Example Ave at Sample St {i}", "road": "Example Pkwy", "dir": "Northbound",
                         "lat": 41.0 + i / 1000, "lon": -73.67, "source": "511NY", "rel": "nearby", "miles": None, "side": "",
                         "facing": False} for i in range(20)]}
    return {"/api/cad/roads": roads, "/api/cad/gis": gis, "/api/cad/weather": wx, "/api/cad/trains": trains,
            "/api/cad/cameras": cams}


class Console:
    """A real WebServer and CAD (the map's home: our area), the map's layers' data and the weather this test's own."""

    def __init__(self, pw):
        from sizeup.cad.api import CadApi
        from sizeup.web.server import WebServer
        from tests.test_cad import make_cad
        self.tmp = tempfile.TemporaryDirectory(prefix="ovfit-")
        cad = make_cad(Path(self.tmp.name))
        scene = {"timezone": "America/New_York", "home": {"center": [-73.664, 41.013], "zoom": 13.4, "bounds": AREA}}
        self.ws = WebServer("127.0.0.1", 0, scene, lambda: {"time": 0}, lambda cid: None, lambda iid: False, cad=CadApi(cad))
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.ws.httpd.server_address[1]}"
        self.browser = pw.chromium.launch()

    def page(self, width, height, layout):
        from tests.test_live_player_ui import SOURCES, Air
        from tests.test_reminders_ui import WHO
        now = time.time()
        air = Air()
        j = lambda body: (lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(body)))  # noqa: E731
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark")
        pg = ctx.new_page()
        pg.set_default_timeout(20000)
        pg.route(lambda url: not url.startswith(self.base), lambda route: route.abort())
        pg.route(self.base + "/api/cad/whoami", j(WHO))
        pg.route(lambda url: "/api/cad/comms?" in url, air.comms)
        pg.route(lambda url: "/api/cad/comms/sources" in url, j({**SOURCES, "now": now}))
        pg.route(lambda url: "/api/me/prefs" in url, air.me)
        for path, body in data(now).items():
            pg.route(lambda url, p=path: url.startswith(self.base + p) and url[len(self.base + p):][:1] in ("", "?"), j(body))
        pg.route(lambda url: "/api/cad/cameras/" in url, lambda route: route.fulfill(status=404, body="{}"))
        pg.route(lambda url: "/api/cad/gis/" in url, j({"type": "FeatureCollection", "features": []}))
        pg.route(lambda url: url.startswith(self.base + "/api/cad/trains/"), j(data(now)["/api/cad/trains"]))
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.add_init_script("try { localStorage.setItem('sizeup.cad.operator', 'JD'); localStorage.setItem('sizeup.overviewLayout', "
                           f"JSON.stringify({{overview: {{mode: '{layout}'}}}})); }} catch (e) {{}}")
        pg.goto(self.base + "/cad")
        pg.wait_for_function("typeof cad !== 'undefined' && cad.state && window.LivePlayer"
                             " && document.querySelectorAll('#wx-banner .wx-alert').length === 2")
        pg.click("#lp-on")                                          # the player's strip, docked under the console
        pg.wait_for_function("!document.getElementById('lp').hidden")
        if layout != "cameras":
            pg.wait_for_selector("#ov-map canvas.maplibregl-canvas", state="attached")
            pg.wait_for_function("window.GisLayers && GisLayers.catalog() && MapLayers.data()")
        return pg

    def close(self):
        self.browser.close()
        self.ws.shutdown()
        self.tmp.cleanup()


@pytest.fixture(scope="module")
def console():
    pw = pytest.importorskip("playwright.sync_api", reason="needs Playwright (pip install playwright; playwright install chromium)")
    with pw.sync_playwright() as p:
        try:
            c = Console(p)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
        yield c
        c.close()


def open_layers(pg):
    pg.locator(".ov-mapbox .cm-ctrls button", has_text="Layers").click()
    pg.wait_for_selector(".ov-mapbox .ml-panel:not([hidden]) .ml-sources")


MEASURE = """() => {
  const box = (s) => { const n = typeof s === 'string' ? document.querySelector(s) : s;
                       if (!n || !n.getClientRects().length) return null; const b = n.getBoundingClientRect();
                       return {t: b.top, b: b.bottom, l: b.left, r: b.right, sh: n.scrollHeight, ch: n.clientHeight}; };
  const se = document.scrollingElement, panel = document.querySelector('.ov-mapbox .ml-panel:not([hidden])');
  const last = panel ? [...panel.children].pop() : null;
  return {vw: innerWidth, vh: innerHeight, sw: se.scrollWidth, sh: se.scrollHeight, top: se.scrollTop, left: se.scrollLeft,
          banners: box('#alert-banners'), lp: box('#lp'), detail: box('#detail'), head: box('.ov-head'), view: box('#ov-view'),
          map: box('.ov-mapbox'), ctrls: box('.ov-mapbox .cm-ctrls'), wall: box('#cw-wall'), panel: box(panel), last: box(last),
          lastIsSources: !!(last && last.classList.contains('ml-sources'))};
}"""


def fits(m, what):
    """No page scroll, the Overview's column doesn't scroll, its head and the map's top bar below the banners, the view
    above the player's strip."""
    assert m["sw"] <= m["vw"] and m["sh"] <= m["vh"], f"{what}: the page scrolls ({m['sw']} x {m['sh']})"
    assert m["top"] == 0 and m["left"] == 0, what
    d = m["detail"]
    assert d["sh"] <= d["ch"] + 1, f"{what}: the Overview's column scrolls ({d['sh']} > {d['ch']})"
    assert m["banners"] and m["lp"], f"{what}: the banners and the player's strip are up"
    assert m["head"]["t"] >= m["banners"]["b"] and m["head"]["t"] >= d["t"], f"{what}: the title row is hidden"
    assert m["view"]["b"] <= d["b"] + 1 and d["b"] <= m["lp"]["t"] + 1, f"{what}: the view runs under the strip"
    if m["map"]:
        assert m["map"]["t"] >= m["head"]["b"] and m["ctrls"]["t"] >= m["map"]["t"], f"{what}: the map's top is cut"
        assert m["map"]["b"] <= d["b"] + 1, what


@pytest.mark.parametrize("width,height", [(2400, 1500), (1600, 1000), (1280, 800), (1200, 800)])
@pytest.mark.parametrize("layout", ["map", "side", "stacked", "cameras"])
def test_in_a_browser_the_overview_fits_with_banners_the_strip_and_the_layers_open(console, width, height, layout):
    pg = console.page(width, height, layout)
    try:
        what = f"{width} x {height} {layout}"
        if layout != "cameras":
            open_layers(pg)
        m = pg.evaluate(MEASURE)
        fits(m, what)
        if layout == "cameras":
            # twenty cameras: the wall scrolls inside itself, the column doesn't
            assert m["wall"]["b"] <= m["detail"]["b"] + 1 and m["wall"]["sh"] > m["wall"]["ch"], what
            assert pg.errors == []
            return
        p, mp = m["panel"], m["map"]
        assert p["t"] >= mp["t"] and p["b"] <= mp["b"] + 1, f"{what}: the Layers box runs out of the map"
        assert m["lastIsSources"], what
        if layout in ("map", "side"):
            # the map is the column's height: the whole box shows, nothing in it scrolls
            assert p["sh"] <= p["ch"] + 1, f"{what}: the Layers box scrolls ({p['sh']} > {p['ch']})"
            assert m["last"]["b"] <= p["b"] + 1, what
        else:
            # stacked: half the view; the box scrolls as one, never the page or the column, and its last row is reachable
            pg.evaluate("document.querySelector('.ov-mapbox .ml-panel').scrollTop = 1e6")
            m2 = pg.evaluate(MEASURE)
            fits(m2, what + " (the box scrolled)")
            assert m2["last"]["b"] <= m2["panel"]["b"] + 1 and m2["last"]["t"] >= m2["panel"]["t"], what
        if SHOTS and layout in ("side", "map"):
            pg.screenshot(path=f"{SHOTS}/overview-{width}x{height}-{layout}.png")
        assert pg.errors == []
    finally:
        pg.context.close()


def test_in_a_browser_a_group_opens_by_keyboard_stays_open_on_a_redraw_and_is_remembered(console):
    pg = console.page(1600, 1000, "map")
    try:
        open_layers(pg)
        gis = pg.locator('.ov-mapbox .ml-panel summary[data-key="layers:County GIS"]')
        assert pg.evaluate("document.querySelector('.ov-mapbox .ml-panel details[data-fold=\"layers:County GIS\"]').open") is False
        assert "1 on" in gis.inner_text()                           # the county's default (fire districts) on, folded
        assert pg.locator(".ov-mapbox .ml-panel .ml-sources .ml-source").count() >= 3       # 511NY, Thruway, the county, ...
        assert not pg.locator(".ov-mapbox .ml-panel .ml-sources .ml-source").first.is_visible()   # folded
        srcs = pg.locator(".ov-mapbox .ml-panel .ml-sources").text_content()
        assert "powered by 511NY" in srcs and "County GIS: Westchester County GIS" in srcs and "Trains: MTA" in srcs
        assert "Aircraft: adsb.lol · adsb.fi" in srcs                                   # a head's two links, apart
        assert pg.locator(".ov-mapbox .ml-panel .ml-sources > summary").get_attribute("title").startswith("511NY · ")
        gis.focus()
        pg.keyboard.press("Enter")
        pg.wait_for_function("document.querySelector('.ov-mapbox .ml-panel details[data-fold=\"layers:County GIS\"]').open")
        pg.wait_for_function("MapLayers.fold('layers:County GIS') === true")              # remembered for this viewer
        # a layer turned on from outside (another map, the hub's copy): the box redraws; the fold and the focus stay
        pg.evaluate("MapLayers.setLayer('gis:hydrants', true)")
        pg.wait_for_function("document.querySelector('.ov-mapbox .ml-panel input[data-key=\"gis:hydrants\"]').checked")
        assert pg.evaluate("document.querySelector('.ov-mapbox .ml-panel details[data-fold=\"layers:County GIS\"]').open")
        assert pg.evaluate("document.activeElement.dataset.key") == "layers:County GIS"
        # opened, the box may be taller than the map: it scrolls as one, inside the map; nothing else moves
        m = pg.evaluate(MEASURE)
        fits(m, "County GIS open")
        assert m["panel"]["b"] <= m["map"]["b"] + 1
        pg.evaluate("document.querySelector('.ov-mapbox .ml-panel').scrollTop = 1e6")
        m = pg.evaluate(MEASURE)
        assert m["last"]["b"] <= m["panel"]["b"] + 1 and m["lastIsSources"]
        if SHOTS:
            pg.evaluate("document.querySelector('.ov-mapbox .ml-panel').scrollTop = 0")
            pg.screenshot(path=f"{SHOTS}/overview-1600x1000-gis-open.png")
        # Escape closes the box; opened again, the group is as the viewer left it
        pg.keyboard.press("Escape")
        assert pg.locator(".ov-mapbox .ml-panel").is_hidden()
        open_layers(pg)
        assert pg.evaluate("document.querySelector('.ov-mapbox .ml-panel details[data-fold=\"layers:County GIS\"]').open")
        # folded again elsewhere (another map's box, the hub's copy) while this box is closed: opened, it follows that,
        # and never puts back what it showed before
        pg.keyboard.press("Escape")
        pg.evaluate("MapLayers.setFold('layers:County GIS', false)")
        open_layers(pg)
        pg.wait_for_timeout(100)                                       # (a queued toggle would have run by now)
        assert pg.evaluate("document.querySelector('.ov-mapbox .ml-panel details[data-fold=\"layers:County GIS\"]').open") is False
        assert pg.evaluate("MapLayers.fold('layers:County GIS')") is False
        assert pg.errors == []
    finally:
        pg.context.close()


@pytest.mark.parametrize("width", [2400, 1600, 1280, 1000])
def test_in_a_browser_nothing_is_wider_than_the_window(console, width):
    pg = console.page(width, 900, "side")
    try:
        m = pg.evaluate(MEASURE)
        assert m["sw"] <= m["vw"], f"{width}: {m['sw']} px wide"
        # the long banners are cut with an ellipsis, on one line
        cut = pg.evaluate("[...document.querySelectorAll('#wx-banner .wx-head')].map((n) => n.scrollWidth > n.clientWidth)")
        assert cut == [True, True]
    finally:
        pg.context.close()
