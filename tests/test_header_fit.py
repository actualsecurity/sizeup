"""The header never runs off the right edge (admin, 2026-11: at the same window width it sometimes fit and sometimes
the brand folded, the clock was cut at "20:" and the profile button was pushed off the screen).

The cause: the header folded at fixed widths (container queries at 1700/1500/1340/1100 px) while what is in it changes
after load: the incident source's label (up to 60 characters) and state ("backing off · <backup> primary"), the
weather with the wind, the station's state, the dispatch mode, the sign-in chip ("9 min · Re-sign in"), and the zoom.
Now static/hdrfit.js measures it and folds items in a fixed order until it fits, and brings them back when there is
room (with HYST px to spare, so it never flips back and forth).

    search()        the order and the hysteresis (node, no browser: runs on CI)
    in a browser    (Playwright, when installed) widths 390-1920, zoom 110/125 %, quiet and busy content, content
                    changing after load, the clock's tick never measuring; SIZEUP_SHOTS=<dir> saves the PR's shots.

Everything shown is made up: the source labels, the weather, the person.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tests.test_access import jwks, jwt, keys, verifier  # noqa: F401 - fixtures

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")


# -- the order and the hysteresis (node) --------------------------------------------------------------------------------

SEARCH = r"""
const fs = require("fs"), vm = require("vm");
const ctx = { window: {}, document: { querySelector: () => null }, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const H = ctx.window.HeaderFit;
// a header that needs `need` px and gets back gain[i] px from step i (the steps are cumulative)
function run(level, need, gain, hyst) {
  const seen = [];
  const room = (n) => { seen.push(n); return gain.slice(0, n).reduce((a, b) => a + b, 0) - need; };
  const got = H.search(level, room, gain.length, hyst);
  return { got, last: seen[seen.length - 1], tries: seen.length };
}
const g = [10, 10, 10, 10];
console.log(JSON.stringify({
  steps: H.STEPS, hyst: H.HYST,
  fits: run(0, -5, g, 16),            // room to spare: nothing folds
  fold2: run(0, 15, g, 16),           // 15 px short: two steps
  never: run(0, 999, g, 16),          // can't fit: everything folded
  holdBack: run(2, 0, g, 16),         // fits at 2; at 1 only 10 px spare (< 16): stays at 2
  allBack: run(3, -40, g, 16),        // lots of room again: all the way back
  oneBack: run(3, -2, g, 16),         // room(2) = 18 >= 16, room(1) = 8 < 16: one step back
  clamp: run(99, 999, g, 16),
  back: fill(),
}));
// backfill: 9 steps needed (to "chips"); the room left is 60 px; each hidden item takes back what it gives
function fill() {
  const n = H.STEPS.indexOf("chips") + 1, size = { tools: 80, brand: 110, title: 40, resign: 70, wx: 120, date: 75, zulu: 45, tabs: 300 };
  const run = (spare, was) => {
    const seen = [];
    const room = (k, back) => { seen.push([...back].sort()); return spare - [...back].reduce((a, i) => a + (size[H.STEPS[i]] || 0), 0); };
    const back = H.backfill(n, room, new Set((was || []).map((x) => H.STEPS.indexOf(x))), 16);
    return { back: [...back].map((i) => H.STEPS[i]).sort(), last: seen[seen.length - 1].map((i) => H.STEPS[i]).sort() };
  };
  // 300 px: everything that hides fits back but the tools' labels (80 left after the rest... 300-45-75-120-40 = 20:
  // no room for resign 70, brand 110, tools 80); the earliest one (tools) is the last tried
  const squeezed = (() => {
    const calls = [];
    const back = H.backfill(H.STEPS.length, (k, b) => { calls.push(k); return 999; }, new Set(), 16);
    return { back: back.size, calls: calls.length };
  })();
  const tiny = [0, 1].map((k) => H.backfill(k, () => { throw new Error("measured"); }, new Set(), 16).size);
  return { s62: run(62), s140: run(140), s45: run(45), s45was: run(45, ["zulu"]), s0: run(0), s300: run(300),
           s999: run(999), squeezed, tiny };
}
"""


@pytest.fixture(scope="module")
def search(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    h = tmp_path_factory.mktemp("hdrfit") / "search.js"
    h.write_text(SEARCH)
    r = subprocess.run([NODE, str(h), str(STATIC / "hdrfit.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_steps_are_in_the_order_asked_for(search):
    s = search["steps"]
    # the brand text, the page name, the weather, the date (the time stays), Zulu, the tabs to icons, chips cut short,
    # chips to dots: in that order (the tool links' labels before them all, the last resort after)
    order = ["brand", "title", "wx", "date", "zulu", "tabs", "chips", "dots"]
    assert [x for x in s if x in order] == order
    assert s[0] == "tools" and s[-1] == "squeeze"
    assert s.index("resign") < s.index("tabs")                   # "9 min · Re-sign in" -> "9 min" (never gone)
    assert search["hyst"] >= 8


def test_it_folds_just_enough_and_comes_back_only_with_room_to_spare(search):
    assert search["fits"]["got"] == 0
    assert search["fold2"]["got"] == 2 and search["fold2"]["last"] == 2      # what is left applied is what it says
    assert search["never"]["got"] == 4 and search["never"]["last"] == 4
    # it fits at 2 already: back to 1 only if that leaves 16 px spare (10: no), so 2, as it was
    assert search["holdBack"]["got"] == 2 and search["holdBack"]["last"] == 2
    assert search["allBack"]["got"] == 0 and search["allBack"]["last"] == 0
    assert search["oneBack"]["got"] == 2 and search["oneBack"]["last"] == 2
    assert search["clamp"]["got"] == 4


def test_the_room_left_brings_back_the_most_important_items_that_fit(search):
    b = search["back"]
    # 62 px: Zulu (45) comes back first (17 spare); nothing else fits after it (the title, 40, would leave none)
    assert b["s62"] == {"back": ["zulu"], "last": ["zulu"]}
    # 140: Zulu, then the date (95 left, 75 of it: 20 spare); then nothing (the weather is 120, the title 40)
    assert b["s140"] == {"back": ["date", "zulu"], "last": ["date", "zulu"]}
    # 45 px: Zulu would leave 0 spare (< 16): not back; but if it was back already it stays (no flip-flop)
    assert b["s45"]["back"] == [] and b["s45"]["last"] == []
    assert b["s45was"]["back"] == ["zulu"] and b["s45was"]["last"] == ["zulu"]
    assert b["s0"] == {"back": [], "last": []}
    assert b["s300"]["back"] == ["date", "title", "wx", "zulu"] and b["s300"]["last"] == b["s300"]["back"]
    # all of it fits: all back, and the last tried (the tools' labels) was kept: no measuring again
    assert b["s999"]["back"] == ["brand", "date", "resign", "title", "tools", "wx", "zulu"]
    assert b["s999"]["last"] == b["s999"]["back"]
    assert b["squeezed"] == {"back": 0, "calls": 0}           # squeezed: the room is the texts', none given back
    assert b["tiny"] == [0, 0]                                 # nothing before the last step: nothing measured


def test_the_pages_load_it_with_the_header_and_the_old_breakpoints_are_gone():
    # before anything draws in the header (in /cad's bundle, after session.js: no round trip of its own)
    for page, first in (("cad.html", ["session.js", "hdrfit.js"]), ("station.html", ["hdrfit.js"])):
        html = (STATIC / page).read_text()
        scripts = [x.split('"')[0] for x in html.split('<script src="/static/')[1:] if not x.startswith("vendor/")]
        assert scripts[:len(first)] == first, page
    css = "".join((STATIC / f).read_text() for f in ("style.css", "cad.css", "station.css"))
    assert "@container hdr" not in css                              # no width breakpoints left for the header
    for step in ("tools", "brand", "title", "resign", "wx", "date", "zulu", "tabs", "chips", "dots", "links", "tight", "squeeze"):
        assert f"header.fold-{step})" in css or f"header.fold-{step} >" in css, step


# -- in a browser (Playwright, when installed: not on CI) ----------------------------------------------------------------

QUIET = {"src": ("County feed", "ok"), "wx": "50°F", "cloud": ("ok", ""), "chip": False}
BUSY = {"src": ("Sample County dispatch feed", "backing_off"), "wx": "50°F · NW 12 G 22 mph",
        "cloud": ("err", "unreachable"), "chip": True}
WIDTHS = (1024, 1280, 1285, 1366, 1440, 1536, 1920)


def sources(label, state):
    ok = state == "ok"
    return {"tiers": [{"source": "county", "label": label, "rank": 1, "state": state, "reason": "", "acting": ok, "ok_s": 3},
                      {"source": "paging", "label": "Paging backup", "rank": 1000, "state": "ok", "reason": "",
                       "acting": not ok, "ok_s": None}],
            "acting": label if ok else "Paging backup", "feeds": 1, "incoming": []}


def frame(pg):
    pg.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")


class Header:
    """A hub with Access (the profile button) whose header content the test sets: the incident source, the station's
    state (role hub), the weather (routed), the sign-in's end."""

    def __init__(self, pw, tmp_path, keys, jwks):  # noqa: F811
        from sizeup.cad.api import CadApi
        from sizeup.web.server import WebServer
        from tests.test_cad import make_cad
        self.cad = make_cad(tmp_path)
        self.keys = keys
        self.ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                            lambda iid: False, cad=CadApi(self.cad), access=verifier(jwks))
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.ws.httpd.server_address[1]}"
        self.browser = pw.chromium.launch()
        self.wx = None
        self.set(QUIET)

    def set(self, c):
        self.cad.sources_state = lambda now=None, s=sources(*c["src"]): s
        state, word = c["cloud"]
        self.cad.cloud = lambda: {"role": "hub", "state": state, "word": word, "detail": ""}
        self.wx = {"conditions": {"short": c["wx"], "wind_deg": 310 if "mph" in c["wx"] else None,
                                  "wind_mph": 12 if "mph" in c["wx"] else 0, "name": "Example Airport", "station": "KXYZ",
                                  "at": time.time(), "stale": False, "text": "Cloudy"}} if c["wx"] else {}
        self.chip = c["chip"]

    def page(self, width, zoom=1.0, height=800, path="/cad"):
        """A console at `width` CSS px of window: at a zoom, the page gets width / zoom CSS px at zoom x the DPR (as the
        browser's zoom does)."""
        ctx = self.browser.new_context(viewport={"width": round(width / zoom), "height": round(height / zoom)},
                                       device_scale_factor=2 * zoom)
        left = 540 - 30 if self.chip else 4 * 3600                      # "9 min" for half a minute either way
        ctx.add_cookies([{"name": "CF_Authorization", "value": jwt(self.keys[0], exp=int(time.time()) + left),
                          "url": self.base}])
        ctx.route(lambda url: not url.startswith(self.base), lambda route: route.abort())
        ctx.route("**/api/cad/weather", lambda route: route.fulfill(
            status=200 if self.wx else 404, content_type="application/json", body=json.dumps(self.wx or {})))
        pg = ctx.new_page()
        pg.set_default_timeout(20000)
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(self.base + path)
        pg.wait_for_function("document.querySelector('#conn').textContent === 'Live' && !document.querySelector('#src-pill').hidden"
                             " && !document.querySelector('#me').hidden")
        if self.chip:
            pg.locator("#me .me-chip").wait_for()
        if self.wx:
            pg.wait_for_function("document.getElementById('wx-now').childElementCount > 0")   # (maybe folded)
        frame(pg)
        return pg

    def close(self):
        self.browser.close()
        self.ws.shutdown()


@pytest.fixture
def header(tmp_path, keys, jwks):  # noqa: F811
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            h = Header(p, tmp_path, keys, jwks)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium: {e}")
        yield h
        h.close()


FITS = """() => {
  const h = document.querySelector('body > header'), vw = document.documentElement.clientWidth;
  const box = (s) => { const n = document.querySelector(s); if (!n || !n.offsetParent) return null;
                       const b = n.getBoundingClientRect(); return {l: b.left, r: b.right, w: b.width}; };
  const clock = document.getElementById('clock-local');
  return {vw, sw: h.scrollWidth, cw: h.clientWidth, page: document.documentElement.scrollWidth,
          me: box('#me .me-btn'), chip: box('#me .me-chip'), clock: box('#clock-local'),
          clockText: clock.textContent, clockCut: clock.scrollWidth > clock.clientWidth + 1,
          chipText: (document.querySelector('#me .me-chip:not([hidden])') || {}).innerText || '',
          steps: HeaderFit.state().steps, runs: HeaderFit.state().runs,
          // an item whose content spills out of it (over its neighbour): never; a state chip is cut on purpose
          spill: [...h.children].filter((c) => c.offsetWidth && !c.matches('.pill, .talking-strip, .spacer'))
            .filter((c) => c.scrollWidth > c.clientWidth + 1).map((c) => c.id || c.className)};
}"""


def check(m, what):
    assert m["sw"] <= m["cw"], f"{what}: the header overflows by {m['sw'] - m['cw']} px ({m['steps']})"
    assert m["page"] <= m["vw"], what
    assert m["me"] and m["me"]["l"] >= 0 and m["me"]["r"] <= m["vw"], f"{what}: the profile button {m['me']}"
    assert m["clock"] and m["clock"]["r"] <= m["vw"] and m["clock"]["w"] > 20 and not m["clockCut"], f"{what}: the clock"
    assert len(m["clockText"]) == 8, what
    assert m["spill"] == [], f"{what}: spills over its neighbour: {m['spill']} ({m['steps']})"                           # hh:mm:ss, whole


def shot(pg, name, width):
    d = os.environ.get("SIZEUP_SHOTS")
    if d:
        pg.screenshot(path=f"{d}/{name}.png", clip={"x": 0, "y": 0, "width": width, "height": 48})


@pytest.mark.parametrize("width", WIDTHS)
def test_in_a_browser_it_fits_at_every_width_quiet_or_busy(header, width):
    for name, c in (("quiet", QUIET), ("busy", BUSY)):
        header.set(c)
        pg = header.page(width)
        m = pg.evaluate(FITS)
        check(m, f"{width} {name}")
        if c["chip"]:
            assert m["chip"] and m["chip"]["r"] <= m["vw"] and m["chipText"].split()[:2] == ["9", "min"], m["chipText"]
        if width == 1920 and name == "quiet":
            assert m["steps"] == [], m["steps"]                      # a wide screen: nothing folded
        shot(pg, f"header-{width}-{name}", width)
        assert pg.errors == []
        pg.context.close()


@pytest.mark.parametrize("zoom", (1.1, 1.25))
def test_in_a_browser_it_fits_at_a_zoom(header, zoom):
    header.set(BUSY)
    for width in (1285, 1440, 1920):
        pg = header.page(width, zoom)
        check(pg.evaluate(FITS), f"{width} at {zoom}")
        if width == 1285:
            shot(pg, f"header-1285-busy-zoom{round(zoom * 100)}", round(width / zoom))
        pg.context.close()


@pytest.mark.parametrize("width", (390, 640, 641, 800, 1100))
def test_in_a_browser_it_fits_down_to_a_phone(header, width):
    header.set(BUSY)
    pg = header.page(width)
    check(pg.evaluate(FITS), f"{width}")
    if width in (390, 800):
        d = os.environ.get("SIZEUP_SHOTS")
        if d:
            hb = pg.locator("body > header").bounding_box()
            pg.screenshot(path=f"{d}/header-{width}-busy.png", clip={"x": 0, "y": 0, "width": width, "height": hb["height"] + 4})
    pg.context.close()


@pytest.mark.parametrize("width", (1024, 1285, 1920))
def test_in_a_browser_the_wall_board_fits_too(header, width):
    header.set(BUSY)
    pg = header.page(width, path="/monitor")
    check(pg.evaluate(FITS), f"/monitor {width}")
    assert pg.errors == []
    pg.context.close()


def test_in_a_browser_it_folds_when_the_content_grows_and_comes_back_after(header):
    """The bug itself: the same width, the content changing after load. Before: the header ran off the screen."""
    header.set(QUIET)
    pg = header.page(1366)
    m0 = pg.evaluate(FITS)
    check(m0, "quiet")
    # the incident source starts backing off, the weather gets its wind, the station drops, the sign-in is ending:
    # drawn by their own scripts, as a state push draws them
    header.set(BUSY)
    pg.evaluate("""(s) => {
      Incoming.pill(document.getElementById('src-pill'), s);
      cad.state.cloud = {role: 'hub', state: 'err', word: 'unreachable', detail: ''}; renderCloud();
    }""", sources(*BUSY["src"]))
    m1 = pg.evaluate(FITS)                                  # (after the observer's microtask: no frame in between)
    check(m1, "busy, same width")
    assert m1["runs"] - m0["runs"] <= 2, (m0["runs"], m1["runs"])                 # one change, one fit (two calls)
    assert len(m1["steps"]) > len(m0["steps"]), (m0["steps"], m1["steps"])
    # the source back to ok: what folded for it comes back
    header.set(QUIET)
    pg.evaluate("""(s) => {
      Incoming.pill(document.getElementById('src-pill'), s);
      cad.state.cloud = {role: 'hub', state: 'ok', word: '', detail: ''}; renderCloud();
    }""", sources(*QUIET["src"]))
    m2 = pg.evaluate(FITS)
    check(m2, "quiet again")
    assert len(m2["steps"]) < len(m1["steps"]), (m1["steps"], m2["steps"])
    # resizing narrower and back: never over, and back where it was
    for w in (1300, 1100, 900, 1100, 1366):
        pg.set_viewport_size({"width": w, "height": 800})
        frame(pg)                                           # (the resize event: in the next frame, before its paint)
        check(pg.evaluate(FITS), f"resized to {w}")
    back = pg.evaluate(FITS)["steps"]                       # about where it was (the hysteresis: maybe one more folded)
    assert len(back) <= len(m2["steps"]) + 1, (m2["steps"], back)
    assert pg.errors == []
    pg.context.close()


def test_in_a_browser_the_clock_tick_never_measures(header):
    header.set(QUIET)
    pg = header.page(1440)
    runs = pg.evaluate("HeaderFit.state().runs")
    t0 = pg.evaluate("document.getElementById('clock-local').textContent")
    pg.wait_for_timeout(3500)
    assert pg.evaluate("document.getElementById('clock-local').textContent") != t0      # it ticked
    assert pg.evaluate("HeaderFit.state().runs") == runs                                # and nothing was measured
    pg.context.close()


def test_in_a_browser_a_cut_chip_keeps_its_whole_text_on_hover(header):
    header.set(BUSY)
    pg = header.page(1024)
    steps = pg.evaluate("HeaderFit.state().steps")
    assert "chips" in steps, steps
    src = pg.locator("#src-pill")
    assert pg.evaluate("(n) => n.scrollWidth > n.clientWidth", src.element_handle())        # cut short
    assert src.text_content().startswith("Sample County dispatch feed · backing off")  # all there (screen readers)
    cloud = pg.locator("#cloud-pill")
    assert cloud.get_attribute("title") == "Station unreachable"      # its own script's (cad.js renderCloud)
    # a chip whose script gave it no title: the header's hover gives it its whole text
    pg.evaluate("document.getElementById('src-pill').removeAttribute('title')")
    src.hover()
    assert src.get_attribute("title") == "Sample County dispatch feed · backing off · Paging backup primary"
    # trouble and "off" keep their words (a dot is for a good state only); the ringing phone is never cut
    tx = pg.locator("#tx-pill")
    assert tx.bounding_box()["width"] > 40 and tx.text_content() == "Transmit off"
    assert "dots" in steps and pg.locator("#conn").bounding_box()["width"] < 16      # Live: a dot
    pg.context.close()
